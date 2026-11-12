"""OpenAI Advertiser API client: key resolution, requests, pagination, errors.

Plain REST over ``requests``. Every call goes through :func:`api_request`,
which owns the behaviours the rest of the package relies on:

- **Auth**: one Advertiser API key per ad account, sent as a bearer token.
  :func:`local_api_key` picks the key for an ``ad_account_id`` from the
  config (``chatgpt_ads.accounts``, then ``chatgpt_ads.api_key``, then the
  ``OPENAI_ADS_API_KEY`` environment variable).
- **Rate limits**: 600 requests/min per endpoint and 1,200/min overall, per
  ad account and per IP. A 429 is retried when the wait is short; a long
  one surfaces as ``ChatGPTAdsApiError(status=429)`` so the calling agent
  can tell the user instead of looping.
- **Money**: budgets and bids are micros (one million per currency unit);
  reporting metrics (spend, CPC, CPM) are already currency units. The
  conversions are Reddit's (:func:`adloop.reddit.client.from_micro` and
  ``to_micro``), which work the same way.
- **Unknown outcomes**: a write whose request may have reached OpenAI
  without an answer coming back (a timeout, a dropped connection, a 5xx)
  raises :class:`ChatGPTAdsOutcomeUnknown`. Reads and requests that never
  left this machine say that nothing changed.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

# Re-exported: the same micros conversions as Reddit's API.
from adloop.reddit.client import from_micro as from_micros  # noqa: F401
from adloop.reddit.client import to_micro  # noqa: F401

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

API_BASE = "https://api.ads.openai.com/v1"
API_KEY_ENV = "OPENAI_ADS_API_KEY"
SETTINGS_URL = "https://ads.openai.com/settings"

_REQUEST_TIMEOUT_SECONDS = 30
_MAX_RATE_LIMIT_RETRIES = 3
# Waits above this are not worth blocking a tool call for.
_MAX_RATE_LIMIT_WAIT_SECONDS = 8
# List endpoints accept up to 500 per page (audit logs up to 100).
PAGE_SIZE = 500
# Safety valve for pagination on very large accounts.
_MAX_PAGES = 40
_MAX_MESSAGE = 400


class ChatGPTAdsAuthError(Exception):
    """No usable Advertiser API key, or the API rejected it.

    Not a ``RuntimeError``: the server's ``_safe`` wrapper turns those into
    bare ``{"error"}`` dicts, while other exceptions reach
    ``_structured_error`` and get an actionable hint.
    """

    def __init__(self, message: str, *, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


class ChatGPTAdsApiError(Exception):
    """The Advertiser API answered with a non-success status."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        url: str = "",
        code: str = "",
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.url = url
        self.code = code
        self.retry_after = retry_after


class ChatGPTAdsOutcomeUnknown(ChatGPTAdsApiError):
    """A write may have reached OpenAI, but no answer came back.

    The change may or may not be in the account. The caller reads the
    entity back before anything else; an update or status change can be
    retried once the dry run sees what is live, a create relies on its
    ``Idempotency-Key``.
    """


# A 5xx on a write can come after OpenAI processed it.
_UNKNOWN_OUTCOME_STATUSES = (500, 502, 503, 504)


def redact(text: Any, secrets: list[str]) -> Any:
    """``text`` with every secret (8+ characters) replaced by ``[redacted]``."""
    if not isinstance(text, str):
        return text
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def scrub(value: Any, secrets: list[str]) -> Any:
    """A parsed response with every secret redacted from every string in it."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    return value


def exact_from_micros(value: Any) -> float | None:
    """Micros → currency amount, unrounded: impression bids go below a cent."""
    if value is None or value == "":
        return None
    try:
        return int(value) / 1_000_000
    except (TypeError, ValueError):
        return None


def local_api_key(config: AdLoopConfig, ad_account_id: str = "") -> str:
    """The Advertiser API key for ``ad_account_id`` from config or environment.

    An id listed under ``chatgpt_ads.accounts`` uses its own key. Anything
    else uses the default key (``chatgpt_ads.api_key``, else
    ``OPENAI_ADS_API_KEY``), unless the config names the default key's
    account (``chatgpt_ads.ad_account_id``) and the request asks for a
    different one: that would silently read the wrong account.
    """
    cfg = config.chatgpt_ads
    account = (ad_account_id or "").strip()
    if account and account in cfg.accounts:
        return cfg.accounts[account]

    default_key = (cfg.api_key or os.environ.get(API_KEY_ENV, "")).strip()
    if account and cfg.ad_account_id and account != cfg.ad_account_id:
        raise ChatGPTAdsAuthError(
            f"No ChatGPT Ads API key is configured for ad account {account}. "
            f"The default key belongs to {cfg.ad_account_id}; add "
            f"chatgpt_ads.accounts.{account}: <key> to the config.",
            error_code="unknown_account",
        )
    if default_key:
        return default_key
    if not account and len(cfg.accounts) == 1:
        return next(iter(cfg.accounts.values()))
    if not account and cfg.accounts:
        raise ChatGPTAdsAuthError(
            "Several ChatGPT Ads accounts are configured and none is the "
            "default: pass ad_account_id (one of "
            + ", ".join(sorted(cfg.accounts))
            + ") or set chatgpt_ads.api_key.",
            error_code="account_required",
        )
    raise ChatGPTAdsAuthError(
        "No ChatGPT Ads API key. Create one in OpenAI Ads Manager → Settings → "
        f"API keys ({SETTINGS_URL}), then set chatgpt_ads.api_key in "
        f"~/.adloop/config.yaml or {API_KEY_ENV} in the environment. It is an "
        "Ads key, not a platform.openai.com key.",
        error_code="missing_key",
    )


def _api_key(config: AdLoopConfig, ad_account_id: str) -> str:
    from adloop.auth import get_chatgpt_ads_api_key

    return get_chatgpt_ads_api_key(config, ad_account_id)


def _user_agent() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"adloop/{version('adloop')}"
    except PackageNotFoundError:
        return "adloop/dev"


def _trim(message: str) -> str:
    message = " ".join(str(message).split())
    return message if len(message) <= _MAX_MESSAGE else message[:_MAX_MESSAGE] + "…"


def _error_detail(payload: Any) -> tuple[str, str]:
    """(message, code) from the API's error shapes."""
    if not isinstance(payload, dict):
        return "", ""
    err = payload.get("error")
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("detail") or "")
        code = str(err.get("code") or err.get("type") or "")
        param = err.get("param")
        if param and message and str(param) not in message:
            message = f"{message} (param: {param})"
        return _trim(message), code
    if isinstance(err, str):
        return _trim(err), str(payload.get("code") or "")
    detail = payload.get("detail")
    if isinstance(detail, list):
        # FastAPI-style validation errors: [{"loc": [...], "msg": ...}]
        parts = []
        for item in detail[:4]:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc") or [] if x != "body")
                parts.append(f"{loc}: {item.get('msg')}" if loc else str(item.get("msg")))
        return _trim("; ".join(parts)), "validation_error"
    message = payload.get("message") or detail or ""
    return _trim(str(message)), str(payload.get("code") or "")


def _never_sent(exc: Exception) -> bool:
    """True when the request provably never left this machine."""
    import requests

    if isinstance(exc, (requests.ConnectTimeout, requests.exceptions.InvalidURL)):
        return True
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    try:
        from urllib3.exceptions import NewConnectionError
    except ImportError:  # pragma: no cover - urllib3 ships with requests
        return False
    return isinstance(reason, NewConnectionError)


def _retry_after(headers: Any) -> int | None:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(int(float(raw)), 0)
    except (TypeError, ValueError):
        return None


def api_request(
    config: AdLoopConfig,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    ad_account_id: str = "",
    headers: dict | None = None,
    files: dict | None = None,
    mutation: bool = False,
) -> dict:
    """Perform one authenticated call and return the decoded JSON body.

    ``path`` is relative to :data:`API_BASE`. List-valued ``params`` go out
    as repeated keys, so pass bracketed names (``"fields[]"``) the way the
    API documents them. ``headers`` adds e.g. an ``Idempotency-Key``;
    ``files`` sends a multipart upload instead of a JSON body. ``mutation``
    marks a request that changes the account, so a lost answer is reported
    as an unknown outcome instead of "nothing changed".
    """
    import requests

    url = f"{API_BASE}/{path.lstrip('/')}"
    extra_headers = headers or {}
    api_key = _api_key(config, ad_account_id)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": _user_agent(),
        **extra_headers,
    }
    method = method.upper()

    tries = 0
    while True:
        try:
            response = requests.request(
                method,
                url,
                params=params or None,
                json=None if files else json_body,
                files=files,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            where = f"{method} /{path.lstrip('/')} ({type(exc).__name__})"
            if mutation and not _never_sent(exc):
                raise ChatGPTAdsOutcomeUnknown(
                    f"No answer from the ChatGPT Ads API for {where}: the request may have "
                    "reached OpenAI, so whether it changed anything is unknown.",
                    status=0,
                ) from None
            raise ChatGPTAdsApiError(
                f"Could not reach the ChatGPT Ads API for {where}; "
                + ("the request was not sent, so nothing changed." if mutation
                   else "nothing was changed (a read)."),
                status=0,
            ) from None
        status = response.status_code

        if status == 429 or (status in (502, 503, 504) and method == "GET"):
            tries += 1
            wait = _retry_after(response.headers)
            wait = 2 if wait is None else wait
            if tries <= _MAX_RATE_LIMIT_RETRIES and wait <= _MAX_RATE_LIMIT_WAIT_SECONDS:
                time.sleep(max(wait, 1))
                continue

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}

        if 200 <= status < 300:
            return payload if isinstance(payload, dict) else {"data": payload}

        # An error body may echo the request; never let the key reach a tool
        # result or the audit log (scrubbed before any trimming cuts it).
        detail, code = _error_detail(scrub(payload, [api_key]))
        if status == 401:
            raise ChatGPTAdsAuthError(
                "OpenAI rejected the ChatGPT Ads API key (401)"
                + (f": {detail}" if detail else "")
                + ". Check that it is an Advertiser API key from Ads Manager → "
                f"Settings → API keys ({SETTINGS_URL}) and that it was not revoked.",
                error_code="invalid_key",
            )
        if status == 429:
            retry_after = _retry_after(response.headers)
            raise ChatGPTAdsApiError(
                "ChatGPT Ads API rate limit reached"
                + (f"; retry after {retry_after}s" if retry_after is not None else "")
                + ". Wait before calling again; do not retry in a loop.",
                status=429,
                url=url,
                code=code,
                retry_after=retry_after,
            )
        message = f"ChatGPT Ads API returned {status} for {method} /{path.lstrip('/')}" + (
            f": {detail}" if detail else ""
        )
        if mutation and status in _UNKNOWN_OUTCOME_STATUSES:
            raise ChatGPTAdsOutcomeUnknown(
                message + ". OpenAI may have carried out the request before failing, so "
                "whether it changed anything is unknown.",
                status=status, url=url, code=code,
            )
        raise ChatGPTAdsApiError(message, status=status, url=url, code=code)


def api_get(
    config: AdLoopConfig, path: str, params: dict | None = None, *, ad_account_id: str = ""
) -> dict:
    return api_request(config, "GET", path, params=params, ad_account_id=ad_account_id)


def api_post(
    config: AdLoopConfig,
    path: str,
    body: dict | None = None,
    *,
    ad_account_id: str = "",
    idempotency_key: str = "",
    mutation: bool = True,
) -> dict:
    """POST a write (or, with ``mutation=False``, a report query such as conversions/insights)."""
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    return api_request(
        config, "POST", path, json_body=body or {}, ad_account_id=ad_account_id,
        headers=headers, mutation=mutation,
    )


def api_upload(config: AdLoopConfig, filename: str, content: bytes, mime: str, *, ad_account_id: str = "") -> dict:
    """Multipart ``POST /upload`` of one image's bytes; returns ``{"file_id": ...}``."""
    return api_request(
        config, "POST", "upload", ad_account_id=ad_account_id,
        files={"file": (filename, content, mime)}, mutation=True,
    )


def api_get_all(
    config: AdLoopConfig,
    path: str,
    params: dict | None = None,
    *,
    ad_account_id: str = "",
    page_size: int = PAGE_SIZE,
    max_pages: int = _MAX_PAGES,
) -> list[dict]:
    """GET every page of a cursor-paginated list (``after`` = previous ``last_id``).

    Refuses rather than return a partial list when the page cap is reached:
    an inventory that silently stops would make missing entities look absent.
    """
    query = dict(params or {})
    query.setdefault("limit", page_size)
    items: list[dict] = []
    for _ in range(max_pages):
        payload = api_get(config, path, query, ad_account_id=ad_account_id)
        page = payload.get("data") or []
        items.extend(item for item in page if isinstance(item, dict))
        last_id = payload.get("last_id")
        if not payload.get("has_more") or not last_id or not page:
            return items
        query["after"] = last_id
    raise ChatGPTAdsApiError(
        f"/{path.lstrip('/')} has more than {len(items)} items, beyond what one call reads; "
        "narrow it (for example by campaign_id).",
        status=0,
    )
