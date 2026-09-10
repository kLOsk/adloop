"""Reddit Ads API v3 client — authenticated requests, pagination, errors.

Plain REST over ``requests``; Reddit ships no Python SDK. Every call goes
through :func:`reddit_request`, which owns the three behaviours the rest
of the package relies on:

- **Auth**: bearer token from the active credentials provider, refreshed
  once on a 401 before giving up.
- **Rate limits**: Reddit's limits are per authorizing user and per
  endpoint group (reporting is 60/min). A 429 carries no Retry-After, only
  a ``RateLimit`` header with ``t=<seconds until reset>``; short waits are
  retried, long ones surface as ``RedditApiError(status=429)`` with the
  reset so the calling agent can tell the user instead of looping.
- **Money**: the API speaks microcurrency (``spend``, ``goal_value``,
  ``bid_value``). :func:`from_micro` / :func:`to_micro` are the only
  places that divide or multiply by a million.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from adloop.reddit.auth import RedditApiError, RedditAuthError

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

API_BASE = "https://ads-api.reddit.com/api/v3"

_REQUEST_TIMEOUT_SECONDS = 30
_MAX_RATE_LIMIT_RETRIES = 3
# Waits above this are not worth blocking a tool call for; the agent gets
# the reset time instead.
_MAX_RATE_LIMIT_WAIT_SECONDS = 8
_PAGE_SIZE = 100
# Safety valve for pagination on accounts with thousands of entities.
_MAX_PAGES = 50

_RATE_LIMIT_RESET_RE = re.compile(r"t=(\d+)")


def from_micro(value: Any) -> float | None:
    """Microcurrency integer → currency amount (2 dp); ``None`` stays ``None``."""
    if value is None or value == "":
        return None
    try:
        return round(int(value) / 1_000_000, 2)
    except (TypeError, ValueError):
        try:
            return round(float(value) / 1_000_000, 2)
        except (TypeError, ValueError):
            return None


def to_micro(value: float | int) -> int:
    """Currency amount → microcurrency integer."""
    return int(round(float(value) * 1_000_000))


def _rate_limit_reset(headers: Any) -> int | None:
    """Seconds until the exhausted policy resets, from the ``RateLimit`` header.

    Format: ``RateLimit: "ads-reporting";r=0;t=42`` — possibly several
    policies comma-separated; the smallest positive ``t`` with ``r=0`` is
    the one that blocked us, but any ``t`` is a usable wait.
    """
    raw = headers.get("RateLimit") or headers.get("ratelimit") or ""
    values = [int(m) for m in _RATE_LIMIT_RESET_RE.findall(str(raw))]
    return min(values) if values else None


def _error_detail(payload: Any) -> str:
    """Pull a human-readable message out of Reddit's several error shapes."""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err.get("code") or "")
    if isinstance(err, str) and err:
        return err
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        parts = []
        for item in errors[:3]:
            if isinstance(item, dict):
                parts.append(
                    str(item.get("message") or item.get("detail") or item.get("code") or item)
                )
            else:
                parts.append(str(item))
        return "; ".join(p for p in parts if p)
    return str(payload.get("message") or payload.get("detail") or "")


def _credentials(config: AdLoopConfig):
    from adloop.auth import get_reddit_credentials

    return get_reddit_credentials(config)


def reddit_request(
    config: AdLoopConfig,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    absolute_url: str = "",
) -> dict:
    """Perform one authenticated call and return the decoded JSON body.

    ``path`` is relative to :data:`API_BASE`; ``absolute_url`` is for
    following ``pagination.next_url`` verbatim, as Reddit asks.
    """
    import requests

    creds = _credentials(config)
    url = absolute_url or f"{API_BASE}/{path.lstrip('/')}"
    headers = {
        "User-Agent": creds.user_agent,
        "Accept": "application/json",
    }

    refreshed_once = False
    rate_limit_tries = 0
    while True:
        headers["Authorization"] = f"Bearer {creds.token()}"
        response = requests.request(
            method.upper(),
            url,
            params=params or None,
            json=json_body,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        status = response.status_code

        if status == 401 and not refreshed_once:
            # The cached access token expired early or was revoked; one
            # refresh is allowed before the failure is reported.
            refreshed_once = True
            creds.invalidate()
            continue

        if status == 429:
            rate_limit_tries += 1
            reset = _rate_limit_reset(response.headers)
            wait = reset if reset is not None else 2
            if (
                rate_limit_tries <= _MAX_RATE_LIMIT_RETRIES
                and wait <= _MAX_RATE_LIMIT_WAIT_SECONDS
            ):
                time.sleep(max(wait, 1))
                continue
            raise RedditApiError(
                f"Reddit Ads API rate limit reached for {url}"
                + (f"; resets in {reset}s" if reset is not None else "")
                + ". Wait before retrying — do not retry in a loop.",
                status=429,
                url=url,
                reset_seconds=reset,
            )

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}

        if 200 <= status < 300:
            return payload if isinstance(payload, dict) else {"data": payload}

        detail = _error_detail(payload)
        if status == 401:
            raise RedditAuthError(
                "Reddit rejected the access token even after a refresh"
                + (f": {detail}" if detail else "")
                + ". Reconnect Reddit Ads.",
                error_code="unauthorized",
                status=401,
            )
        if status == 403:
            raise RedditApiError(
                "Reddit Ads API refused the request (403): "
                + (detail or "missing scope or no permission on this ad account")
                + ". Writes need the adsedit scope and an account role that "
                "allows edits.",
                status=403,
                url=url,
            )
        raise RedditApiError(
            f"Reddit Ads API returned {status} for {url}"
            + (f": {detail}" if detail else ""),
            status=status,
            url=url,
        )


def reddit_get(config: AdLoopConfig, path: str, params: dict | None = None) -> dict:
    return reddit_request(config, "GET", path, params=params)


def reddit_post(config: AdLoopConfig, path: str, body: dict | None = None) -> dict:
    return reddit_request(config, "POST", path, json_body=body or {})


def reddit_patch(config: AdLoopConfig, path: str, body: dict | None = None) -> dict:
    return reddit_request(config, "PATCH", path, json_body=body or {})


def reddit_get_all(
    config: AdLoopConfig,
    path: str,
    params: dict | None = None,
    *,
    max_pages: int = _MAX_PAGES,
) -> list[dict]:
    """GET every page of a list endpoint by following ``pagination.next_url``.

    Reddit's docs are explicit: follow the URL, do not reconstruct it from
    the query parameters.
    """
    query = dict(params or {})
    query.setdefault("page.size", _PAGE_SIZE)
    payload = reddit_get(config, path, query)
    items = list(payload.get("data") or [])
    pages = 1
    while pages < max_pages:
        next_url = (payload.get("pagination") or {}).get("next_url")
        if not next_url:
            break
        payload = reddit_request(config, "GET", "", absolute_url=next_url)
        items.extend(payload.get("data") or [])
        pages += 1
    return items


def data_of(payload: dict) -> dict:
    """Unwrap Reddit's ``{"data": {...}}`` envelope for single-entity calls."""
    data = payload.get("data")
    return data if isinstance(data, dict) else {}
