"""Reddit OAuth2 — access-token refresh and the local authorization flow.

Reddit's OAuth is plain RFC 6749 with two quirks that matter here:

- ``duration=permanent`` on the authorize URL is what produces a refresh
  token; without it the grant dies after an hour and cannot be refreshed.
- The token endpoint wants HTTP Basic auth (app id : secret) AND a
  descriptive ``User-Agent`` — Reddit throttles default user agents on
  every host, including the token endpoint.

Access tokens live for one hour (sometimes a day); refresh tokens are
permanent but MAY be rotated in a refresh response, so ``refresh()`` hands
the raw response back and subclasses decide where a rotated token goes
(the local token file here, an encrypted DB row in AdLoop Cloud).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

OAUTH_AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_REVOKE_URL = "https://www.reddit.com/api/v1/revoke_token"

# adsread covers every GET plus reports; adsedit every create/update.
SCOPES = ["adsread", "adsedit"]

# Reddit requires an exact-match redirect URI on the developer app, so the
# local flow cannot pick a random free port the way Google's does. The
# wizard tells the user to register exactly this URL.
LOCAL_REDIRECT_PORT = 8765
LOCAL_REDIRECT_URI = f"http://localhost:{LOCAL_REDIRECT_PORT}/callback"

DEFAULT_TOKEN_PATH = "~/.adloop/reddit_token.json"

# Refresh a minute early so a token never expires mid-request.
_EXPIRY_MARGIN_SECONDS = 60
_TOKEN_TIMEOUT_SECONDS = 30


class RedditAuthError(Exception):
    """The Reddit OAuth server refused a token operation.

    Deliberately NOT a ``RuntimeError``: the server's ``_safe`` wrapper
    turns RuntimeErrors into bare ``{"error"}`` dicts, while other
    exceptions reach ``_structured_error`` and get an actionable hint.
    """

    def __init__(self, message: str, *, error_code: str = "", status: int = 0):
        super().__init__(message)
        self.error_code = error_code
        self.status = status


class RedditApiError(Exception):
    """The Reddit Ads API answered with a non-success status."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        url: str = "",
        reset_seconds: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.url = url
        self.reset_seconds = reset_seconds


def build_user_agent(config: AdLoopConfig) -> str:
    """Reddit's recommended ``platform:app-id:version (by /u/name)`` format.

    Reddit throttles generic user agents ("python-requests", "Java") hard
    and asks that the string names the app, its version and the developer's
    Reddit username. A configured ``reddit.user_agent`` wins; otherwise one
    is assembled from the app id and, when known, the username.
    """
    explicit = (config.reddit.user_agent or "").strip()
    if explicit:
        return explicit

    from importlib.metadata import PackageNotFoundError, version

    try:
        ver = version("adloop")
    except PackageNotFoundError:
        ver = "dev"
    app = config.reddit.client_id or "adloop"
    ua = f"python:adloop.{app}:v{ver}"
    username = (config.reddit.username or "").strip().lstrip("/").removeprefix("u/")
    if username:
        ua += f" (by /u/{username})"
    return ua


def _token_error(payload: dict, status: int) -> RedditAuthError:
    code = str(payload.get("error") or "").strip()
    detail = str(payload.get("error_description") or payload.get("message") or "")
    if code == "invalid_grant":
        message = (
            "Reddit refused the refresh token (invalid_grant): the grant was "
            "revoked, the app was deleted, or the user removed AdLoop under "
            "reddit.com/prefs/apps."
        )
    elif status == 401 or code == "invalid_client":
        message = (
            "Reddit rejected the app credentials (client id / secret). Check "
            "the developer application in Reddit Business Manager."
        )
    else:
        message = f"Reddit token request failed ({status or code or 'unknown'})"
        if detail:
            message += f": {detail}"
    return RedditAuthError(message, error_code=code or f"http_{status}", status=status)


def post_token_request(
    *,
    client_id: str,
    client_secret: str,
    user_agent: str,
    data: dict,
) -> dict:
    """POST to Reddit's token endpoint; shared by code exchange and refresh.

    Reddit answers some failures with HTTP 200 and an ``error`` field in
    the body, so both the status and the body are inspected.
    """
    import requests

    response = requests.post(
        OAUTH_TOKEN_URL,
        auth=(client_id, client_secret),
        data=data,
        headers={"User-Agent": user_agent},
        timeout=_TOKEN_TIMEOUT_SECONDS,
    )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code != 200 or payload.get("error"):
        raise _token_error(payload, response.status_code)
    if not payload.get("access_token"):
        raise RedditAuthError(
            "Reddit token response carried no access_token",
            error_code="malformed_response",
            status=response.status_code,
        )
    return payload


class RedditCredentials:
    """Refreshable bearer credentials for one Reddit user.

    Holds the permanent refresh token plus a cached access token. ``token()``
    is what the HTTP client calls; ``refresh()`` is the single seam a hosted
    deployment overrides to persist rotations and stamp connection health.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        user_agent: str,
        access_token: str = "",
        expires_at: float = 0.0,
    ):
        if not client_id or not client_secret:
            raise RedditAuthError(
                "Reddit app credentials are missing (reddit.client_id / "
                "reddit.client_secret). Run `adloop init` or set them in the config.",
                error_code="missing_client",
            )
        if not refresh_token:
            raise RedditAuthError(
                "No Reddit refresh token. Run `adloop init` to authorize Reddit Ads.",
                error_code="missing_refresh_token",
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.user_agent = user_agent
        self.access_token = access_token
        self.expires_at = expires_at
        self._lock = threading.Lock()

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at

    def token(self) -> str:
        """Return a usable access token, refreshing when expired."""
        if self.valid:
            return self.access_token
        with self._lock:
            if not self.valid:
                self.refresh()
        return self.access_token

    def invalidate(self) -> None:
        """Drop the cached access token (after a 401) so the next call refreshes."""
        self.access_token = ""
        self.expires_at = 0.0

    def refresh(self) -> dict:
        """Exchange the refresh token for a new access token.

        Returns Reddit's raw token response. When it carries a ``refresh_token``
        that differs from the current one, the in-memory token is updated;
        subclasses persist it.
        """
        payload = post_token_request(
            client_id=self.client_id,
            client_secret=self.client_secret,
            user_agent=self.user_agent,
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
        )
        self._absorb(payload)
        return payload

    def _absorb(self, payload: dict) -> None:
        self.access_token = str(payload["access_token"])
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self.expires_at = time.time() + max(expires_in - _EXPIRY_MARGIN_SECONDS, 30)
        rotated = str(payload.get("refresh_token") or "")
        if rotated:
            self.refresh_token = rotated


# ---------------------------------------------------------------------------
# Local (OSS) flow: token file + loopback authorization
# ---------------------------------------------------------------------------


def token_file_path(config: AdLoopConfig) -> Path:
    return Path(config.reddit.token_path or DEFAULT_TOKEN_PATH).expanduser()


def load_token_file(path: Path) -> dict:
    with open(path) as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise RedditAuthError(
            f"Reddit token file {path} is not a JSON object", error_code="bad_token_file"
        )
    return payload


def save_token_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass


class LocalRedditCredentials(RedditCredentials):
    """OSS credentials backed by ``~/.adloop/reddit_token.json``.

    A rotated refresh token is written straight back to the file so the
    next process start does not hand Reddit a token it already retired.
    """

    def __init__(self, *, token_path: Path, **kwargs):
        super().__init__(**kwargs)
        self.token_path = token_path

    def refresh(self) -> dict:
        before = self.refresh_token
        try:
            payload = super().refresh()
        except RedditAuthError as exc:
            if exc.error_code == "invalid_grant":
                # Same treatment as the Google token: a dead grant is
                # discarded so the next wizard run starts clean.
                self.token_path.unlink(missing_ok=True)
            raise
        if self.refresh_token != before:
            try:
                stored = load_token_file(self.token_path)
            except (OSError, RedditAuthError):
                stored = {}
            stored["refresh_token"] = self.refresh_token
            save_token_file(self.token_path, stored)
        return payload


_local_cache: dict[str, LocalRedditCredentials] = {}
_local_cache_lock = threading.Lock()


def local_credentials(config: AdLoopConfig) -> RedditCredentials:
    """Credentials for the local provider, cached per token file.

    Caching matters: every tool call asks the provider for credentials, and
    rebuilding the object would throw away the cached access token and hit
    Reddit's token endpoint on each call.
    """
    path = token_file_path(config)
    key = str(path)
    with _local_cache_lock:
        cached = _local_cache.get(key)
        if cached is not None and cached.client_id == config.reddit.client_id:
            return cached
        if not path.exists():
            raise RedditAuthError(
                f"No Reddit token found at {path}. Run `adloop init` and complete "
                "the Reddit Ads step to authorize AdLoop, or set reddit.token_path "
                "to an existing token file.",
                error_code="missing_refresh_token",
            )
        stored = load_token_file(path)
        creds = LocalRedditCredentials(
            token_path=path,
            client_id=config.reddit.client_id,
            client_secret=config.reddit.client_secret,
            refresh_token=str(stored.get("refresh_token") or ""),
            user_agent=build_user_agent(config),
        )
        _local_cache[key] = creds
        return creds


def reset_local_cache() -> None:
    """Forget cached local credentials (tests, and after `adloop init`)."""
    with _local_cache_lock:
        _local_cache.clear()


def authorize_url(client_id: str, state: str, redirect_uri: str = LOCAL_REDIRECT_URI) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": redirect_uri,
            "duration": "permanent",
            "scope": ",".join(SCOPES),
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    user_agent: str,
) -> dict:
    """Trade an authorization code for tokens (single use, ten-minute life)."""
    return post_token_request(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        data={
            "grant_type": "authorization_code",
            "code": code.strip().removesuffix("#_"),
            "redirect_uri": redirect_uri,
        },
    )


def run_local_authorization(
    config: AdLoopConfig,
    *,
    open_browser: bool = True,
    timeout_seconds: int = 300,
) -> dict:
    """Interactive loopback flow for `adloop init`.

    Starts a one-shot HTTP server on ``LOCAL_REDIRECT_PORT``, opens the
    consent page, waits for Reddit to redirect back with the code, exchanges
    it and writes the token file. Falls back to pasting the redirect URL
    when the port is taken or no browser is available.
    """
    import secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    client_id = config.reddit.client_id
    client_secret = config.reddit.client_secret
    if not client_id or not client_secret:
        raise RedditAuthError(
            "reddit.client_id and reddit.client_secret are required before "
            "authorizing.",
            error_code="missing_client",
        )

    state = secrets.token_urlsafe(16)
    url = authorize_url(client_id, state)
    received: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            params = parse_qs(urlparse(self.path).query)
            received["code"] = (params.get("code") or [""])[0]
            received["state"] = (params.get("state") or [""])[0]
            received["error"] = (params.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>AdLoop: Reddit authorized.</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )

        def log_message(self, *_args):  # silence request logging
            return

    server: HTTPServer | None = None
    try:
        server = HTTPServer(("localhost", LOCAL_REDIRECT_PORT), _Handler)
    except OSError:
        server = None

    print(f"\nOpen this URL to authorize AdLoop with Reddit:\n\n  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if server is not None:
        server.timeout = timeout_seconds
        print(f"Waiting for Reddit to redirect to {LOCAL_REDIRECT_URI} ...")
        server.handle_request()
        server.server_close()
    else:
        print(
            f"Port {LOCAL_REDIRECT_PORT} is busy, so the redirect cannot be "
            "captured automatically."
        )

    if not received.get("code"):
        pasted = input(
            "Paste the full redirect URL from the browser address bar: "
        ).strip()
        params = parse_qs(urlparse(pasted).query)
        received["code"] = (params.get("code") or [""])[0]
        received["state"] = (params.get("state") or [""])[0]
        received["error"] = (params.get("error") or [""])[0]

    if received.get("error"):
        raise RedditAuthError(
            f"Reddit denied authorization: {received['error']}",
            error_code=received["error"],
        )
    if not received.get("code"):
        raise RedditAuthError("No authorization code received", error_code="no_code")
    if received.get("state") != state:
        raise RedditAuthError(
            "OAuth state mismatch — the redirect did not come from this "
            "authorization attempt. Try again.",
            error_code="state_mismatch",
        )

    payload = exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=received["code"],
        redirect_uri=LOCAL_REDIRECT_URI,
        user_agent=build_user_agent(config),
    )
    if not payload.get("refresh_token"):
        raise RedditAuthError(
            "Reddit returned no refresh token. The authorize URL must carry "
            "duration=permanent.",
            error_code="no_refresh_token",
        )
    save_token_file(
        token_file_path(config),
        {
            "refresh_token": payload["refresh_token"],
            "scope": payload.get("scope", ""),
            "obtained_at": int(time.time()),
        },
    )
    reset_local_cache()
    return payload
