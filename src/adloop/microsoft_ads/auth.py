"""Microsoft Advertising OAuth — refresh-token credentials and device-code sign-in.

Microsoft Advertising authenticates through the Microsoft identity platform
with the ``https://ads.microsoft.com/msads.manage`` scope. ``offline_access``
returns a refresh token, which lasts up to 90 days and is rotated on every
refresh, so the new one must be stored each time (the local token file
here; a hosted deployment overrides :meth:`MicrosoftCredentials.persist`).

``adloop microsoft-login`` signs in once in the browser (authorization code
with PKCE; the redirect lands on ``http://localhost:8080/callback``, which
the Entra app must list as a mobile/desktop redirect URI) and stores the
refresh token in ``token_path``. Directories with security defaults block
the device-code flow (sign-in error 530035), so it is only the fallback:
``adloop microsoft-login --device-code``. The Entra app must allow public
client flows.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

SCOPE = "https://ads.microsoft.com/msads.manage offline_access"
_AUTHORITY = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
_EXPIRY_MARGIN_SECONDS = 120
_TIMEOUT_SECONDS = 30
BROWSER_REDIRECT_URI = "http://localhost:8080/callback"
_BROWSER_LOGIN_TIMEOUT_SECONDS = 300


class MicrosoftAdsAuthError(Exception):
    """No usable credentials, or the identity platform refused them.

    Deliberately not a ``RuntimeError`` (see the Reddit and ChatGPT errors):
    ``_structured_error`` gives it an actionable hint.
    """

    def __init__(self, message: str, *, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


class MicrosoftAdsApiError(Exception):
    """The Microsoft Advertising API answered with a fault."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        url: str = "",
        codes: list[str] | None = None,
        tracking_id: str = "",
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.url = url
        self.codes = codes or []
        self.tracking_id = tracking_id
        self.retry_after = retry_after


def _token_url(tenant: str) -> str:
    return _AUTHORITY.format(tenant=tenant or "common") + "/token"


def _device_code_url(tenant: str) -> str:
    return _AUTHORITY.format(tenant=tenant or "common") + "/devicecode"


def _post_form(url: str, data: dict) -> tuple[int, dict]:
    import requests

    response = requests.post(url, data=data, timeout=_TIMEOUT_SECONDS)
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


def _token_error(payload: dict, status: int, secrets: list[str] | None = None) -> MicrosoftAdsAuthError:
    code = str(payload.get("error") or f"http_{status}")
    description = str(payload.get("error_description") or "").split("\r\n")[0].strip()
    for secret in sorted((x for x in secrets or [] if x and len(x) >= 4), key=len, reverse=True):
        description = description.replace(secret, "[redacted]")
    if code == "invalid_grant":
        message = (
            "Microsoft refused the refresh token (invalid_grant): it expired (90 days "
            "unused), was revoked, or the password changed. Run `adloop microsoft-login` again."
        )
    elif code in ("invalid_client", "unauthorized_client"):
        message = (
            "Microsoft rejected the app (client id or secret). Check microsoft_ads.client_id; "
            "a public client app needs 'Allow public client flows' enabled."
        )
    else:
        message = f"Microsoft token request failed ({code})"
    if description:
        message += f" — {description}"
    return MicrosoftAdsAuthError(message, error_code=code)


class MicrosoftCredentials:
    """Refreshable bearer credentials for one Microsoft Advertising user."""

    def __init__(
        self,
        *,
        client_id: str,
        refresh_token: str,
        tenant: str = "common",
        client_secret: str = "",
        access_token: str = "",
        expires_at: float = 0.0,
    ):
        if not client_id:
            raise MicrosoftAdsAuthError(
                "microsoft_ads.client_id is not set: the Entra app (public client with the "
                "msads.manage permission) that AdLoop signs in with.",
                error_code="missing_client",
            )
        if not refresh_token:
            raise MicrosoftAdsAuthError(
                "No Microsoft Advertising sign-in yet. Run `adloop microsoft-login` and sign "
                "in with the account that has access to the ad accounts.",
                error_code="missing_refresh_token",
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant = tenant or "common"
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
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": SCOPE,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        status, payload = _post_form(_token_url(self.tenant), data)
        if status != 200 or not payload.get("access_token"):
            raise _token_error(payload, status, [self.client_secret, self.refresh_token, self.access_token])
        self.access_token = str(payload["access_token"])
        self.expires_at = time.time() + float(payload.get("expires_in") or 3600)
        if payload.get("refresh_token"):
            self.refresh_token = str(payload["refresh_token"])
        self.persist(payload)

    def persist(self, payload: dict) -> None:
        """Store a rotated refresh token. Subclasses decide where."""


class FileMicrosoftCredentials(MicrosoftCredentials):
    """Credentials backed by the local token file (self-hosted)."""

    def __init__(self, *, token_path: Path, **kwargs: Any):
        super().__init__(**kwargs)
        self.token_path = token_path

    def persist(self, payload: dict) -> None:
        write_token_file(
            self.token_path,
            {
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": self.expires_at,
                "scope": payload.get("scope") or SCOPE,
            },
        )


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

    return _resolve_path(config.microsoft_ads.token_path or "~/.adloop/microsoft_token.json")


_cache: dict[str, FileMicrosoftCredentials] = {}
_cache_lock = threading.Lock()


def local_credentials(config: AdLoopConfig) -> MicrosoftCredentials:
    """Credentials from the token file, shared per path within the process."""
    cfg = config.microsoft_ads
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
                raise MicrosoftAdsAuthError(
                    f"Cannot read the Microsoft token file {path}: {exc}. Run `adloop microsoft-login`.",
                    error_code="missing_refresh_token",
                ) from exc
        creds = FileMicrosoftCredentials(
            token_path=path,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            tenant=cfg.tenant,
            refresh_token=str(data.get("refresh_token") or ""),
            access_token=str(data.get("access_token") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
        )
        _cache[str(path)] = creds
        return creds


def reset_credentials_cache() -> None:
    with _cache_lock:
        _cache.clear()


def device_code_login(
    config: AdLoopConfig,
    *,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Sign in once with the device-code flow and store the refresh token."""
    cfg = config.microsoft_ads
    if not cfg.client_id:
        raise MicrosoftAdsAuthError(
            "Set microsoft_ads.client_id in ~/.adloop/config.yaml first (the Entra app id).",
            error_code="missing_client",
        )
    status, start = _post_form(
        _device_code_url(cfg.tenant), {"client_id": cfg.client_id, "scope": SCOPE}
    )
    if status != 200 or not start.get("device_code"):
        raise _token_error(start, status)
    say(
        str(start.get("message") or "")
        or f"Open {start.get('verification_uri')} and enter the code {start.get('user_code')}."
    )
    say("Sign in with the Microsoft account that has access to the ad accounts.")

    interval = float(start.get("interval") or 5)
    deadline = time.time() + float(start.get("expires_in") or 900)
    while time.time() < deadline:
        sleep(interval)
        data = {
            "client_id": cfg.client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": start["device_code"],
        }
        if cfg.client_secret:
            data["client_secret"] = cfg.client_secret
        status, payload = _post_form(_token_url(cfg.tenant), data)
        if status == 200 and payload.get("refresh_token"):
            path = _token_path(config)
            write_token_file(
                path,
                {
                    "refresh_token": payload["refresh_token"],
                    "access_token": payload.get("access_token", ""),
                    "expires_at": time.time() + float(payload.get("expires_in") or 3600),
                    "scope": payload.get("scope") or SCOPE,
                },
            )
            reset_credentials_cache()
            return path
        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise _token_error(payload, status)
    raise MicrosoftAdsAuthError("The sign-in code expired before it was used. Run the command again.",
                                error_code="expired_token")


def _pkce_pair() -> tuple[str, str]:
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(config: AdLoopConfig, *, state: str, challenge: str, redirect_uri: str, login_hint: str = "") -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": config.microsoft_ads.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return _AUTHORITY.format(tenant=config.microsoft_ads.tenant or "common") + "/authorize?" + urlencode(params)


def browser_login(
    config: AdLoopConfig,
    *,
    login_hint: str = "",
    redirect_uri: str = BROWSER_REDIRECT_URI,
    say: Callable[[str], None] = print,
    open_browser: Callable[[str], Any] | None = None,
    listen: Callable[[str], dict] | None = None,
) -> Path:
    """Sign in once in the browser (authorization code + PKCE) and store the refresh token."""
    import secrets

    cfg = config.microsoft_ads
    if not cfg.client_id:
        raise MicrosoftAdsAuthError(
            "Set microsoft_ads.client_id in ~/.adloop/config.yaml first (the Entra app id).",
            error_code="missing_client",
        )
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open
    if listen is None:
        from adloop.oauth_redirect import RedirectTimeout, listen_once

        def listen(uri: str) -> dict:
            try:
                return listen_once(uri, _BROWSER_LOGIN_TIMEOUT_SECONDS, provider="Microsoft Advertising")
            except RedirectTimeout:
                raise MicrosoftAdsAuthError(
                    "No sign-in arrived within 5 minutes. Run the command again.", error_code="timeout",
                ) from None
            except OSError as exc:
                raise MicrosoftAdsAuthError(
                    f"Cannot listen on {uri} ({exc}); close whatever uses that port, or run "
                    "`adloop microsoft-login --device-code`.", error_code="port_in_use",
                ) from None

    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    url = authorize_url(config, state=state, challenge=challenge, redirect_uri=redirect_uri, login_hint=login_hint)
    say("Opening Microsoft sign-in in your browser. Sign in with the user that has access to the")
    say("Microsoft Advertising accounts. If the browser does not open, visit:")
    say(f"  {url}")
    try:
        open_browser(url)
    except Exception:  # noqa: BLE001 — the URL is printed
        pass
    query = listen(redirect_uri)
    if query.get("error"):
        description = str(query.get("error_description") or query["error"]).split("\r\n")[0]
        raise MicrosoftAdsAuthError(f"Microsoft did not complete the sign-in: {description}", error_code=str(query["error"]))
    if query.get("state") != state:
        raise MicrosoftAdsAuthError("The sign-in response does not belong to this sign-in; run the command again.",
                                    error_code="state_mismatch")
    if not query.get("code"):
        raise MicrosoftAdsAuthError("The sign-in response carried no authorization code.", error_code="no_code")
    data = {
        "client_id": cfg.client_id,
        "grant_type": "authorization_code",
        "code": query["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": SCOPE,
    }
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    status, payload = _post_form(_token_url(cfg.tenant), data)
    if status != 200 or not payload.get("refresh_token"):
        raise _token_error(payload, status, [cfg.client_secret, query["code"]])
    path = _token_path(config)
    write_token_file(path, {
        "refresh_token": payload["refresh_token"],
        "access_token": payload.get("access_token", ""),
        "expires_at": time.time() + float(payload.get("expires_in") or 3600),
        "scope": payload.get("scope") or SCOPE,
    })
    reset_credentials_cache()
    return path
