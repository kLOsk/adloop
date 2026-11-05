"""Roku Ads API client: requests, pagination, errors, report downloads.

Every call goes through :func:`api_request`:

- **Auth**: ``Authorization: Bearer`` from the stored sign-in; a 401 refreshes
  the access token once and retries.
- **Parameters**: list-valued query parameters travel comma-separated
  (Roku's ``explode: false``), e.g. ``account_uid=a,b``.
- **Errors**: ``{"errors": [...]}`` and ``{"error": {...}}`` bodies are both
  folded into one :class:`RokuAdsApiError`. A uid the user cannot see, or
  that does not exist, answers 403.
- **Rate limits** (per app, organization, endpoint group, read/write; no
  published numbers): a 429 with a short ``Retry-After`` is retried, a long
  one surfaces with ``retry_after`` so the caller can wait.

Lists paginate with ``limit``/``offset`` and ``meta.total_count``;
synchronous metrics use a ``cursor``. Report files come from a pre-signed
URL that must never appear in an error.
"""

from __future__ import annotations

import csv
import gzip
import io
import time
import zipfile
from typing import TYPE_CHECKING, Any

from adloop.roku_ads.auth import API_BASE, RokuAdsApiError, RokuAdsAuthError, error_detail

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_TIMEOUT_SECONDS = 60
_MAX_RETRIES = 3
_MAX_WAIT_SECONDS = 10
PAGE_SIZE = 100
_MAX_ITEMS = 5000


def _credentials(config: AdLoopConfig):
    from adloop.auth import get_roku_ads_credentials

    return get_roku_ads_credentials(config)


def _user_agent() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"adloop/{version('adloop')}"
    except PackageNotFoundError:
        return "adloop/dev"


def _retry_after(headers: Any) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return max(int(float(raw)), 0) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _query(params: dict | None) -> dict:
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None or value == "" or value == []:
            continue
        out[key] = ",".join(str(v) for v in value) if isinstance(value, (list, tuple, set)) else value
    return out


def api_request(
    config: AdLoopConfig,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """One authenticated call; ``path`` is relative to ``/v1/developer/``."""
    import requests

    creds = _credentials(config)
    url = f"{API_BASE}/developer/{path.lstrip('/')}"
    method = method.upper()
    refreshed = False
    tries = 0
    while True:
        headers = {
            "Authorization": f"Bearer {creds.token()}",
            "Accept": "application/json",
            "User-Agent": _user_agent(),
        }
        try:
            response = requests.request(
                method, url, params=_query(params) or None, json=json_body, headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise RokuAdsApiError(
                f"Could not reach the Roku Ads API for /{path.lstrip('/')} ({type(exc).__name__}); try again."
            ) from None
        status = response.status_code
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if 200 <= status < 300:
            return payload if isinstance(payload, dict) else {"data": payload}

        message, code = error_detail(payload)
        from adloop.roku_ads.auth import redact

        message = redact(message, [getattr(creds, "access_token", ""), getattr(creds, "refresh_token", ""),
                                   getattr(creds, "client_secret", "")])
        if status == 401 and not refreshed:
            refreshed = True
            creds.invalidate()
            continue
        if status == 429 or (status in (502, 503, 504) and method == "GET"):
            tries += 1
            wait = _retry_after(response.headers)
            wait = 3 if wait is None else wait
            if tries <= _MAX_RETRIES and wait <= _MAX_WAIT_SECONDS:
                time.sleep(max(wait, 1))
                continue
            if status == 429:
                raise RokuAdsApiError(
                    "Roku Ads API rate limit reached"
                    + (f"; retry after {wait}s" if wait else "")
                    + ". Wait before calling again; do not retry in a loop.",
                    status=429, code=code or "RATE_LIMIT_EXCEEDED", retry_after=wait,
                )
        if status == 401:
            raise RokuAdsAuthError(
                "Roku rejected the sign-in even after a refresh"
                + (f": {message}" if message else "")
                + ". Run `adloop roku-login` again.",
                error_code="invalid_grant",
            )
        raise RokuAdsApiError(
            f"Roku Ads API returned {status} for {method} /{path.lstrip('/')}"
            + (f": {message}" if message else ""),
            status=status, code=code,
        )


def api_get(config: AdLoopConfig, path: str, params: dict | None = None) -> dict:
    return api_request(config, "GET", path, params=params)


def api_post(config: AdLoopConfig, path: str, body: dict) -> dict:
    return api_request(config, "POST", path, json_body=body)


def api_get_all(
    config: AdLoopConfig,
    path: str,
    params: dict | None = None,
    *,
    page_size: int = PAGE_SIZE,
    max_items: int = _MAX_ITEMS,
) -> tuple[list[dict], dict]:
    """Every page of a ``limit``/``offset`` list; returns (items, merged ``included``).

    ``included`` lists (side-loaded objects such as CRM files) are joined
    across pages. Refuses rather than return part of a list when it holds
    more than ``max_items``: a silently cut inventory makes entities look absent.
    """
    query = dict(params or {})
    query["limit"] = page_size
    offset = 0
    items: list[dict] = []
    included: dict[str, list] = {}
    while True:
        query["offset"] = offset
        payload = api_get(config, path, query)
        page = [item for item in payload.get("data") or [] if isinstance(item, dict)]
        side = payload.get("included")
        if isinstance(side, list):
            side = {"items": side}
        if isinstance(side, dict):
            for key, value in side.items():
                if isinstance(value, list):
                    included.setdefault(key, []).extend(v for v in value if isinstance(v, dict))
        items.extend(page)
        total = (payload.get("meta") or {}).get("total_count")
        offset += len(page)
        if not page or len(page) < page_size or (isinstance(total, int) and offset >= total):
            return items, included
        if len(items) >= max_items:
            raise RokuAdsApiError(
                f"/{path} has more than {len(items)} items, beyond what one call reads; narrow it.",
            )


# ---------------------------------------------------------------------------
# Report files
# ---------------------------------------------------------------------------


def download(url: str, *, what: str = "report") -> bytes:
    """Fetch a pre-signed file URL without ever repeating it in an error."""
    import requests

    try:
        response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RokuAdsApiError(f"Downloading the {what} failed ({type(exc).__name__}); try again.") from None
    if response.status_code >= 400:
        raise RokuAdsApiError(
            f"Downloading the {what} failed with HTTP {response.status_code}; the link may have "
            "expired, fetch the report again.",
            status=response.status_code,
        )
    return response.content


def parse_csv(data: bytes) -> list[dict]:
    """Rows of a report file (plain, gzipped or zipped CSV), keyed by header."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                return []
            data = archive.read(names[0])
    text = data.decode("utf-8-sig", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if not row or all(v in (None, "") for v in row.values()):
            continue
        rows.append({str(k).strip(): (v if v is not None else "") for k, v in row.items() if k is not None})
    return rows
