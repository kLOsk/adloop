"""Google API authentication — OAuth 2.0 and service account support.

Credential acquisition is pluggable: :class:`LocalFileCredentialsProvider`
implements the OSS behavior (credentials.json chain, ``~/.adloop/token.json``,
interactive browser flow), and a hosted deployment swaps in its own provider
via :func:`set_credentials_provider` (e.g. tokens from an encrypted database,
refreshed out-of-band). All client construction goes through the module-level
:func:`get_ga4_credentials` / :func:`get_ads_credentials`, which delegate to
the active provider.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from adloop.config import AdLoopConfig
    from adloop.reddit.auth import RedditCredentials

# Request all scopes in a single OAuth flow so one token works for both
# GA4 and Google Ads. Without this, separate tokens would constantly
# overwrite each other at the same token_path.
_ALL_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/content",
]

_GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    # Required for GA4 writes (key events). The Admin API has no
    # narrower write scope.
    "https://www.googleapis.com/auth/analytics.edit",
]

_ADS_SCOPES = [
    "https://www.googleapis.com/auth/adwords",
]

_GTM_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
]

_GSC_SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
]

# The Merchant API (Content API successor) has no read-only variant —
# this is its only scope. AdLoop's merchant tools use it read-only.
_MERCHANT_SCOPES = [
    "https://www.googleapis.com/auth/content",
]


class CredentialsProvider(Protocol):
    """Source of authenticated Google credentials for the current context.

    ``gtm_credentials`` is optional: providers that don't implement it
    (e.g. hosted deployments that haven't rolled out Tag Manager support)
    cause GTM tools to fail with a clear capability error instead of a
    credentials leak or an AttributeError.
    """

    def ga4_credentials(self, config: AdLoopConfig) -> Credentials: ...

    def ads_credentials(self, config: AdLoopConfig) -> Credentials: ...

    def gtm_credentials(self, config: AdLoopConfig) -> Credentials: ...

    def gsc_credentials(self, config: AdLoopConfig) -> Credentials: ...

    def merchant_credentials(self, config: AdLoopConfig) -> Credentials: ...

    def reddit_credentials(self, config: AdLoopConfig) -> RedditCredentials: ...


class LocalFileCredentialsProvider:
    """OSS default: local credential files + interactive OAuth.

    Refuses to run in server mode — falling back to the operator's own
    ``~/.adloop`` tokens in a multi-tenant process would silently serve
    one tenant's request with another identity's credentials.
    """

    def _guard_local_only(self) -> None:
        from adloop.runtime import deployment_mode

        if deployment_mode() == "server":
            raise RuntimeError(
                "LocalFileCredentialsProvider cannot be used in server mode. "
                "The hosted deployment must install its own provider via "
                "adloop.auth.set_credentials_provider() at startup."
            )

    def ga4_credentials(self, config: AdLoopConfig) -> Credentials:
        self._guard_local_only()
        return _local_credentials(config, _GA4_SCOPES)

    def ads_credentials(self, config: AdLoopConfig) -> Credentials:
        self._guard_local_only()
        return _local_credentials(config, _ADS_SCOPES)

    def gtm_credentials(self, config: AdLoopConfig) -> Credentials:
        self._guard_local_only()
        return _local_credentials(config, _GTM_SCOPES)

    def gsc_credentials(self, config: AdLoopConfig) -> Credentials:
        self._guard_local_only()
        return _local_credentials(config, _GSC_SCOPES)

    def merchant_credentials(self, config: AdLoopConfig) -> Credentials:
        self._guard_local_only()
        return _local_credentials(config, _MERCHANT_SCOPES)

    def reddit_credentials(self, config: AdLoopConfig) -> RedditCredentials:
        # Reddit is a separate OAuth provider: its own app, its own token
        # file (reddit.token_path), nothing shared with the Google token.
        self._guard_local_only()
        from adloop.reddit.auth import local_credentials

        return local_credentials(config)


_active_provider: CredentialsProvider = LocalFileCredentialsProvider()


def set_credentials_provider(provider: CredentialsProvider) -> None:
    """Swap the credentials provider (hosted deployments call this once)."""
    global _active_provider
    _active_provider = provider


def get_credentials_provider() -> CredentialsProvider:
    return _active_provider


def get_ga4_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for GA4 APIs."""
    return _active_provider.ga4_credentials(config)


def get_ads_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for Google Ads API."""
    return _active_provider.ads_credentials(config)


def _get_credentials_path(config: AdLoopConfig) -> Path | None:
    """Resolve OAuth client credentials using a priority chain.

    1. User-provided credentials_path in config (if non-empty and file exists)
    2. ~/.adloop/credentials.json (if file exists — the wizard's default spot)
    3. None (caller falls back to Application Default Credentials)
    """
    if config.google.credentials_path:
        user_path = Path(config.google.credentials_path).expanduser()
        if user_path.exists():
            return user_path

    local_path = Path("~/.adloop/credentials.json").expanduser()
    if local_path.exists():
        return local_path

    return None


def _local_credentials(config: AdLoopConfig, scopes: list[str]) -> Credentials:
    """Resolve credentials from local files (service account or OAuth)."""
    creds_path = _get_credentials_path(config)

    if creds_path is not None:
        import json

        with open(creds_path) as f:
            creds_info = json.load(f)

        if creds_info.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=scopes,
            )

        return _oauth_flow(config, creds_path)

    import google.auth

    try:
        credentials, _ = google.auth.default(scopes=scopes)
    except Exception as exc:
        raise RuntimeError(
            "No Google OAuth credentials found. Run `adloop init` to set up "
            "your own Google Cloud project, or place your OAuth client JSON "
            "at ~/.adloop/credentials.json. Prefer zero setup? AdLoop Cloud "
            "handles credentials for you: https://getadloop.com"
        ) from exc
    return credentials


def get_gtm_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for the Google Tag Manager API."""
    provider = _active_provider
    if not hasattr(provider, "gtm_credentials"):
        raise RuntimeError(
            "This deployment's credentials provider does not support "
            "Google Tag Manager. GTM tools are only available where the "
            "provider implements gtm_credentials()."
        )
    return provider.gtm_credentials(config)


def get_gsc_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for the Search Console API."""
    provider = _active_provider
    if not hasattr(provider, "gsc_credentials"):
        raise RuntimeError(
            "This deployment's credentials provider does not support "
            "Google Search Console. GSC tools are only available where "
            "the provider implements gsc_credentials()."
        )
    return provider.gsc_credentials(config)


def get_merchant_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for the Merchant API (Merchant Center)."""
    provider = _active_provider
    if not hasattr(provider, "merchant_credentials"):
        raise RuntimeError(
            "This deployment's credentials provider does not support "
            "Google Merchant Center. Merchant tools are only available "
            "where the provider implements merchant_credentials()."
        )
    return provider.merchant_credentials(config)


def get_reddit_credentials(config: AdLoopConfig) -> RedditCredentials:
    """Return refreshable bearer credentials for the Reddit Ads API."""
    provider = _active_provider
    if not hasattr(provider, "reddit_credentials"):
        raise RuntimeError(
            "This deployment's credentials provider does not support "
            "Reddit Ads. Reddit tools are only available where the "
            "provider implements reddit_credentials()."
        )
    return provider.reddit_credentials(config)


def _oauth_flow(
    config: AdLoopConfig, creds_path: Path | None = None
) -> Credentials:
    """Run OAuth Desktop flow requesting all scopes (GA4 + Ads).

    Uses a single token file for all scopes to avoid conflicts between
    GA4 and Ads auth sharing the same token_path.

    Falls back to a manual copy-paste flow when no browser is available
    (headless servers, Docker containers, SSH sessions) — but only when
    attached to a real terminal; under a stdio MCP server stdin/stdout
    belong to the JSON-RPC stream and must not be touched.
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
        stored_scopes = _stored_token_scopes(token_path)
        if stored_scopes is None or set(_ALL_SCOPES) - set(stored_scopes):
            # Token predates newer scopes (or is unreadable). Google
            # rejects scope expansion at refresh with invalid_scope, so
            # discard it and fall through to a fresh consent flow.
            token_path.unlink(missing_ok=True)
        else:
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
            if "invalid_scope" in err_str:
                token_path.unlink(missing_ok=True)
                raise RuntimeError(
                    "OAuth token was granted with an older set of scopes and "
                    "Google refused to refresh it (invalid_scope). AdLoop has "
                    "added new Google API scopes since this token was created. "
                    "Fix: re-run any AdLoop tool to trigger re-authorization "
                    "and approve the updated consent screen."
                ) from exc
            raise
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path), _ALL_SCOPES
        )
        creds = _run_oauth_with_fallback(flow)
        _verify_granted_scopes(creds)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    return creds


def _verify_granted_scopes(creds: Credentials) -> None:
    """Reject a consent that granted fewer scopes than AdLoop requested.

    Google's consent screen lets users uncheck individual scopes. Local
    mode stores one token for all services, so a partial grant would be
    discarded as stale on the next load and consent would reopen every
    call — fail loudly once instead of looping.
    """
    granted = set(getattr(creds, "scopes", None) or [])
    missing = set(_ALL_SCOPES) - granted
    if missing:
        raise RuntimeError(
            "Google authorization completed, but these scopes were not "
            f"granted: {', '.join(sorted(missing))}. AdLoop's local mode "
            "needs every requested scope in a single grant — re-run any "
            "AdLoop tool and leave all permissions checked on the consent "
            "screen. Need per-scope control? AdLoop Cloud supports "
            "selective scope grants: https://getadloop.com"
        )


def _stored_token_scopes(token_path: Path) -> list[str] | None:
    """Scopes recorded in the stored token file, or None if unreadable."""
    import json

    try:
        stored = json.loads(token_path.read_text())
    except (OSError, ValueError):
        return None
    scopes = stored.get("scopes")
    return scopes if isinstance(scopes, list) else None


def _is_interactive_terminal() -> bool:
    """True when stdin AND stdout are real TTYs (a human at a terminal).

    Under a stdio MCP server both are pipes carrying JSON-RPC frames —
    printing prompts or reading input there corrupts the protocol stream.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _run_oauth_with_fallback(flow: object) -> Credentials:
    """Try browser-based OAuth; fall back to manual URL copy-paste for headless."""
    interactive = _is_interactive_terminal()
    try:
        # Suppress the library's stdout prompt unless a human terminal is
        # attached — under stdio transport, stdout is the JSON-RPC stream.
        kwargs = {} if interactive else {"authorization_prompt_message": ""}
        return flow.run_local_server(port=0, **kwargs)  # type: ignore[union-attr]
    except Exception:
        pass

    if not interactive:
        raise RuntimeError(
            "OAuth authorization is required but no browser could be opened "
            "and no interactive terminal is attached. Run 'adloop init' in a "
            "terminal to complete authorization, then retry."
        )

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
