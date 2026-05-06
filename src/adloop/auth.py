"""Google API authentication — OAuth 2.0 and service account support."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from adloop.config import AdLoopConfig

# Request all scopes in a single OAuth flow so one token works for both
# GA4 and Google Ads. Without this, separate tokens would constantly
# overwrite each other at the same token_path.
_ALL_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
]

_GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
]

_ADS_SCOPES = [
    "https://www.googleapis.com/auth/adwords",
]

_GTM_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
]


def _get_credentials_path(config: AdLoopConfig) -> Path | None:
    """Resolve OAuth client credentials using a priority chain.

    1. User-provided credentials_path in config (if non-empty and file exists)
    2. ~/.adloop/credentials.json (if file exists — legacy or manually placed)
    3. Bundled credentials shipped with the package
    4. None (caller falls back to Application Default Credentials)
    """
    if config.google.credentials_path:
        user_path = Path(config.google.credentials_path).expanduser()
        if user_path.exists():
            return user_path

    local_path = Path("~/.adloop/credentials.json").expanduser()
    if local_path.exists():
        return local_path

    try:
        ref = importlib.resources.files("adloop").joinpath("bundled_credentials.json")
        with importlib.resources.as_file(ref) as bundled:
            if bundled.exists():
                return Path(bundled)
    except (FileNotFoundError, TypeError):
        pass

    return None


def get_ga4_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for GA4 APIs."""
    creds_path = _get_credentials_path(config)

    if creds_path is not None:
        import json

        with open(creds_path) as f:
            creds_info = json.load(f)

        if creds_info.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=_GA4_SCOPES,
            )

        return _oauth_flow(config, creds_path)

    import google.auth

    credentials, _ = google.auth.default(scopes=_GA4_SCOPES)
    return credentials


def get_ads_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for Google Ads API."""
    creds_path = _get_credentials_path(config)

    if creds_path is not None:
        import json

        with open(creds_path) as f:
            creds_info = json.load(f)

        if creds_info.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=_ADS_SCOPES,
            )

        return _oauth_flow(config, creds_path)

    import google.auth

    credentials, _ = google.auth.default(scopes=_ADS_SCOPES)
    return credentials


def get_gtm_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for Google Tag Manager API."""
    creds_path = _get_credentials_path(config)

    if creds_path is not None:
        import json

        with open(creds_path) as f:
            creds_info = json.load(f)

        if creds_info.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=_GTM_SCOPES,
            )

        return _oauth_flow(config, creds_path)

    import google.auth

    credentials, _ = google.auth.default(scopes=_GTM_SCOPES)
    return credentials


def _oauth_flow(
    config: AdLoopConfig, creds_path: Path | None = None
) -> Credentials:
    """Run OAuth Desktop flow requesting all scopes (GA4 + Ads).

    Uses a single token file for all scopes to avoid conflicts between
    GA4 and Ads auth sharing the same token_path.

    Falls back to a manual copy-paste flow when no browser is available
    (headless servers, Docker containers, SSH sessions).
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(config.google.token_path).expanduser()
    if creds_path is None:
        creds_path = _get_credentials_path(config)
    if creds_path is None:
        raise RuntimeError(
            "No OAuth credentials found. Run 'adloop init' or place "
            "credentials.json at ~/.adloop/credentials.json"
        )

    creds = None
    if token_path.exists():
        creds = OAuthCredentials.from_authorized_user_file(
            str(token_path), _ALL_SCOPES
        )

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            err_str = str(exc).lower()
            if "revoked" in err_str or "invalid_grant" in err_str:
                token_path.unlink(missing_ok=True)
                raise RuntimeError(
                    "OAuth token has been revoked or expired. "
                    "This typically happens when the Google Cloud consent screen "
                    "is in 'Testing' mode (tokens expire after 7 days). "
                    "Fix: (1) re-run any AdLoop tool to trigger re-authorization, "
                    "(2) publish the consent screen to 'In production' in Google "
                    "Cloud Console to prevent future expiry."
                ) from exc
            raise
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path), _ALL_SCOPES
        )
        creds = _run_oauth_with_fallback(flow)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    return creds


def _run_oauth_with_fallback(flow: object) -> Credentials:
    """Try browser-based OAuth; fall back to manual URL copy-paste for headless."""
    try:
        return flow.run_local_server(port=0)  # type: ignore[union-attr]
    except Exception:
        pass

    auth_url, _ = flow.authorization_url(prompt="consent")  # type: ignore[union-attr]
    print()
    print("  No browser detected — using manual authorization.")
    print()
    print("  Open this URL in a browser on any device:")
    print()
    print(f"    {auth_url}")
    print()
    print("  Sign in and grant access. Your browser will redirect to a")
    print("  localhost URL that won't load — that's expected.")
    print("  Copy the FULL URL from your browser's address bar.")
    print()
    redirect_url = input("  Paste the redirect URL here: ").strip()
    flow.fetch_token(authorization_response=redirect_url)  # type: ignore[union-attr]
    return flow.credentials  # type: ignore[union-attr]
