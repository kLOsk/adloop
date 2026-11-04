"""Roku Ads OAuth — authorization-code sign-in and refreshable credentials.

Roku's Ads API uses OAuth 2.0 authorization code. An organization admin
creates a developer app in Ads Manager (Organization → Ads API → Add
developer app: name, logo, website, redirect URI); the tab then shows the
client id, the client secret (once) and the **authorization URL**, which
already carries the app's parameters. Roku documents no scopes and no PKCE;
an optional ``state`` is appended to that URL and comes back on the
redirect together with the authorization ``code``.

The token endpoint (``POST /v1/developer/token``) takes JSON with
``client_id`` and ``client_secret`` in the body. Access tokens last 24
hours; the refresh grant returns a new access token (a refresh token only
if Roku rotates it, which it does not document, so the old one is kept).

``adloop roku-login`` opens the authorization URL, receives the redirect on
a local listener when the redirect URI is ``http://localhost:<port>/...``
(otherwise the user pastes the address the browser landed on), exchanges
the code and stores the tokens in ``roku_ads.token_path``.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

API_BASE = "https://api.ads.roku.com/v1"
TOKEN_URL = f"{API_BASE}/developer/token"
ADS_MANAGER_URL = "https://ads.roku.com/campaigns/"
_EXPIRY_MARGIN_SECONDS = 300
_TIMEOUT_SECONDS = 30
_LOGIN_TIMEOUT_SECONDS = 300


class RokuAdsAuthError(Exception):
    """No usable sign-in, or Roku refused the app or the tokens.

    Not a ``RuntimeError``, so ``_structured_error`` gives it a hint.
    """

    def __init__(self, message: str, *, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


class RokuAdsApiError(Exception):
    """The Roku Ads API answered with an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "",
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def error_detail(payload: Any) -> tuple[str, str]:
    """(message, code) from Roku's two error shapes.

    Most endpoints answer ``{"errors": [{uid, code, status, title, meta}]}``;
    rate limits and the metrics endpoint answer ``{"error": {code, message,
    details}}``.
    """
    if not isinstance(payload, dict):
        return "", ""
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        parts, codes = [], []
        for e in errors[:4]:
            if not isinstance(e, dict):
                continue
            title = str(e.get("title") or e.get("detail") or e.get("status") or e.get("code") or "")
            field = (e.get("meta") or {}).get("field") if isinstance(e.get("meta"), dict) else None
            parts.append(f"{title} ({field})" if field and title else title)
            codes.append(str(e.get("status") or e.get("code") or ""))
        return "; ".join(p for p in parts if p)[:500], next((c for c in codes if c), "")
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        details = error.get("details")
        if isinstance(details, list) and details:
            extra = "; ".join(
                f"{d.get('field')}: {d.get('issue')}" for d in details[:4] if isinstance(d, dict)
            )
            message = f"{message} ({extra})" if message else extra
        return message[:500], str(error.get("code") or "")
    if isinstance(error, str):
        return error[:500], str(payload.get("error_description") or "")
    return str(payload.get("message") or "")[:500], ""


def redact(text: str, secrets: list[str]) -> str:
    """Remove credential values an upstream error message might echo back."""
    for secret in sorted((x for x in secrets if x and len(x) >= 4), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def _post_token(body: dict) -> tuple[int, dict]:
    import requests

    try:
        response = requests.post(
            TOKEN_URL, json=body, headers={"Accept": "application/json"}, timeout=_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RokuAdsAuthError(
            f"Could not reach Roku's token endpoint ({type(exc).__name__}); try again.",
            error_code="network",
        ) from None
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


def _token_error(payload: dict, status: int, *, refreshing: bool, secrets: list[str] | None = None) -> RokuAdsAuthError:
    message, code = error_detail(payload)
    message = redact(message, secrets or [])
    if status in (401, 403) and refreshing:
        text = (
            "Roku refused the stored refresh token (it expired or the app's access was revoked). "
            "Run `adloop roku-login` again."
        )
        error_code = "invalid_grant"
    elif status in (401, 403):
        text = (
            "Roku refused the app (client id or secret) or the authorization code. Check "
            "roku_ads.client_id and client_secret, and that the code was used within minutes."
        )
        error_code = "invalid_client"
    else:
        text = f"Roku token request failed (HTTP {status})"
        error_code = code or f"http_{status}"
    if message:
        text += f" — {message}"
    return RokuAdsAuthError(text, error_code=error_code)


class RokuCredentials:
    """Refreshable bearer credentials for one Roku Ads Manager user."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        access_token: str = "",
        expires_at: float = 0.0,
    ):
        if not client_id or not client_secret:
            raise RokuAdsAuthError(
                "roku_ads.client_id and roku_ads.client_secret are not set: the developer app an "
                "organization admin creates in Roku Ads Manager → Organization → Ads API.",
                error_code="missing_client",
            )
        if not refresh_token and not access_token:
            raise RokuAdsAuthError(
                "No Roku Ads sign-in yet. Run `adloop roku-login` and approve access with a user "
                "who can see the ad accounts (the Viewer role is enough).",
                error_code="not_signed_in",
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.expires_at = expires_at
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if not self.access_token or time.time() >= self.expires_at - _EXPIRY_MARGIN_SECONDS:
                self._refresh_locked()
            return self.access_token

    def invalidate(self) -> None:
        with self._lock:
            self.access_token = ""
            self.expires_at = 0.0

    def _refresh_locked(self) -> None:
        if not self.refresh_token:
            raise RokuAdsAuthError(
                "The Roku access token expired and no refresh token is stored. Run `adloop roku-login`.",
                error_code="not_signed_in",
            )
        status, payload = _post_token({
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        })
        if status not in (200, 201) or not payload.get("access_token"):
            raise _token_error(payload, status, refreshing=True,
                               secrets=[self.client_secret, self.refresh_token, self.access_token])
        self.access_token = str(payload["access_token"])
        self.expires_at = time.time() + float(payload.get("expires_in") or 86400)
        if payload.get("refresh_token"):
            self.refresh_token = str(payload["refresh_token"])
        self.persist()

    def persist(self) -> None:
        """Store refreshed tokens. Subclasses decide where."""


class FileRokuCredentials(RokuCredentials):
    """Credentials backed by the local token file (self-hosted)."""

    def __init__(self, *, token_path: Path, **kwargs: Any):
        super().__init__(**kwargs)
        self.token_path = token_path

    def persist(self) -> None:
        write_token_file(self.token_path, {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        })


def write_token_file(path: Path, data: dict) -> None:
    """Write the token file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _token_path(config: AdLoopConfig) -> Path:
    from adloop.config import _resolve_path

    return _resolve_path(config.roku_ads.token_path or "~/.adloop/roku_token.json")


_cache: dict[str, FileRokuCredentials] = {}
_cache_lock = threading.Lock()


def local_credentials(config: AdLoopConfig) -> RokuCredentials:
    """Credentials from the token file, shared per path within the process."""
    cfg = config.roku_ads
    path = _token_path(config)
    with _cache_lock:
        cached = _cache.get(str(path))
        if cached is not None and cached.client_id == cfg.client_id:
            return cached
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise RokuAdsAuthError(
                    f"Cannot read the Roku token file {path}: {exc}. Run `adloop roku-login`.",
                    error_code="not_signed_in",
                ) from exc
        creds = FileRokuCredentials(
            token_path=path,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            refresh_token=str(data.get("refresh_token") or ""),
            access_token=str(data.get("access_token") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
        )
        _cache[str(path)] = creds
        return creds


def reset_credentials_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


def with_state(authorization_url: str, state: str) -> str:
    """The authorization URL with ``state`` set (replacing any there)."""
    parts = urlsplit(authorization_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "state"]
    query.append(("state", state))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _is_local(redirect_uri: str) -> bool:
    parts = urlsplit(redirect_uri)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def listen_once(redirect_uri: str, timeout: float = _LOGIN_TIMEOUT_SECONDS) -> dict:
    """Serve the redirect URI on localhost until the browser arrives; return its query."""
    from adloop.oauth_redirect import RedirectTimeout
    from adloop.oauth_redirect import listen_once as _listen

    try:
        return _listen(redirect_uri, timeout, provider="Roku")
    except RedirectTimeout:
        raise RokuAdsAuthError("No sign-in arrived before the timeout. Run the command again.",
                               error_code="timeout") from None


def authorization_login(
    config: AdLoopConfig,
    *,
    say: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
    open_browser: Callable[[str], Any] | None = None,
    listen: Callable[[str], dict] = listen_once,
) -> Path:
    """Run the authorization-code sign-in once and store the tokens."""
    cfg = config.roku_ads
    missing = [
        name for name, value in (
            ("client_id", cfg.client_id), ("client_secret", cfg.client_secret),
            ("authorization_url", cfg.authorization_url), ("redirect_uri", cfg.redirect_uri),
        ) if not value
    ]
    if missing:
        raise RokuAdsAuthError(
            "Set roku_ads." + ", roku_ads.".join(missing) + " in ~/.adloop/config.yaml first. "
            "They come from Roku Ads Manager → Organization → Ads API (an organization admin "
            "adds the developer app; the redirect URI must match the one registered there).",
            error_code="missing_client",
        )
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open

    state = secrets.token_urlsafe(16)
    url = with_state(cfg.authorization_url, state)
    say("Open this address, sign in to Roku Ads Manager and approve access:")
    say(f"  {url}")
    try:
        open_browser(url)
    except Exception:  # noqa: BLE001 — a missing browser is fine, the URL is printed
        pass

    if _is_local(cfg.redirect_uri):
        say(f"Waiting for the redirect on {cfg.redirect_uri} ...")
        query = listen(cfg.redirect_uri)
    else:
        pasted = ask("After approving, paste the full address the browser was sent to: ").strip()
        query = dict(parse_qsl(urlsplit(pasted).query))

    if query.get("error"):
        raise RokuAdsAuthError(
            f"Roku did not grant access: {query.get('error_description') or query.get('error')}.",
            error_code="access_denied",
        )
    if query.get("state") != state:
        raise RokuAdsAuthError(
            "The redirect's state does not match this sign-in; start again with `adloop roku-login`.",
            error_code="state_mismatch",
        )
    code = query.get("code")
    if not code:
        raise RokuAdsAuthError("The redirect carried no authorization code.", error_code="no_code")

    status, payload = _post_token({
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code": code,
        "redirect_uri": cfg.redirect_uri,
    })
    if status not in (200, 201) or not payload.get("access_token"):
        raise _token_error(payload, status, refreshing=False, secrets=[cfg.client_secret, code])
    path = _token_path(config)
    write_token_file(path, {
        "refresh_token": payload.get("refresh_token", ""),
        "access_token": payload["access_token"],
        "expires_at": time.time() + float(payload.get("expires_in") or 86400),
    })
    reset_credentials_cache()
    return path
