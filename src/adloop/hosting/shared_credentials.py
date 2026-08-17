"""Shared Google reporting credentials provider (GA4 / GTM / GSC via reporting@).

GA4, GTM, and GSC use ONE shared refresh token per service (the
reporting@motiventmarketing.com identity), each minted against its own Web OAuth
client + a single read-only scope, so one refresh token can touch only one
Google product. The token is fixed and shared -- it does NOT depend on which
tenant (employee) is calling; the ClientBrain session is the only per-user gate
(enforced upstream by the auth middleware).

Ads stays per-user: ``ads_credentials`` is delegated to the inner
:class:`~adloop.hosting.credentials.SupabaseCredentialsProvider`, which keys off
``current_tenant()``. That flow predates the shared model and is unchanged.

Installed via ``set_credentials_provider`` in ``asgi._prepare_server``. Because
this provider implements ``gtm_credentials`` / ``gsc_credentials``, upstream's
``hasattr`` capability check lets the GTM and GSC tools run in server mode (the
per-user provider deliberately omits them).

Per-service Web OAuth clients (server-side secrets) from env -- these MUST be the
same three clients ClientBrain used to capture the tokens, or refresh fails:
  ADLOOP_GA4_CLIENT_ID / ADLOOP_GA4_CLIENT_SECRET
  ADLOOP_GTM_CLIENT_ID / ADLOOP_GTM_CLIENT_SECRET
  ADLOOP_GSC_CLIENT_ID / ADLOOP_GSC_CLIENT_SECRET
"""

from __future__ import annotations

import os

from google.oauth2.credentials import Credentials

from adloop.config import AdLoopConfig
from adloop.hosting.credentials import (
    MissingGoogleConnection,
    SupabaseCredentialsProvider,
    TokenLookup,
)
from adloop.hosting.shared_token_lookup import SharedTokenLookupFn

GA4_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GTM_READONLY_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
GSC_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

_SHARED_SCOPES: dict[str, str] = {
    "ga4": GA4_READONLY_SCOPE,
    "gtm": GTM_READONLY_SCOPE,
    "gsc": GSC_READONLY_SCOPE,
}

_SHARED_CLIENT_ENV: dict[str, tuple[str, str]] = {
    "ga4": ("ADLOOP_GA4_CLIENT_ID", "ADLOOP_GA4_CLIENT_SECRET"),
    "gtm": ("ADLOOP_GTM_CLIENT_ID", "ADLOOP_GTM_CLIENT_SECRET"),
    "gsc": ("ADLOOP_GSC_CLIENT_ID", "ADLOOP_GSC_CLIENT_SECRET"),
}


def _shared_client(service: str) -> tuple[str, str]:
    id_var, secret_var = _SHARED_CLIENT_ENV[service]
    client_id = os.environ.get(id_var, "").strip()
    client_secret = os.environ.get(secret_var, "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            f"Server is missing {id_var} / {secret_var} for the shared "
            f"{service.upper()} Web OAuth client."
        )
    return client_id, client_secret


class SharedGoogleCredentialsProvider:
    """GA4/GTM/GSC from the shared reporting@ tokens; Ads delegated per-user."""

    def __init__(
        self,
        shared_lookup: SharedTokenLookupFn | None,
        ads_provider: SupabaseCredentialsProvider,
    ) -> None:
        self._shared_lookup = shared_lookup
        self._ads = ads_provider

    # --- CredentialsProvider protocol ----------------------------------
    def ads_credentials(self, config: AdLoopConfig) -> Credentials:
        # Ads remains per-user (keyed by current_tenant()).
        return self._ads.ads_credentials(config)

    def ga4_credentials(self, config: AdLoopConfig) -> Credentials:
        return self._build_shared("ga4")

    def gtm_credentials(self, config: AdLoopConfig) -> Credentials:
        return self._build_shared("gtm")

    def gsc_credentials(self, config: AdLoopConfig) -> Credentials:
        return self._build_shared("gsc")

    # merchant_credentials intentionally omitted -- there is no shared Merchant
    # connection, so upstream's hasattr check reports it unsupported.

    # --- internals ------------------------------------------------------
    def _build_shared(self, service: str) -> Credentials:
        refresh_token = self._shared_lookup(service) if self._shared_lookup else None
        if not refresh_token:
            raise MissingGoogleConnection(
                f"The shared Google {service.upper()} reporting connection isn't "
                "set up. An admin connects it once in MotiventOS "
                "(Team → Integrations), then every employee can use it."
            )
        client_id, client_secret = _shared_client(service)
        # token=None -> refreshed from refresh_token on first use.
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[_SHARED_SCOPES[service]],
        )


def install_shared_credentials_provider(
    shared_lookup: SharedTokenLookupFn | None = None,
    ads_lookup: TokenLookup | None = None,
) -> None:
    """Install the shared reporting provider (GA4/GTM/GSC) + per-user Ads.

    ``shared_lookup`` reads the shared per-service refresh token; ``ads_lookup``
    is the existing per-user Ads lookup. Both may be ``None`` in local dev, in
    which case the respective env-var / dev fallbacks apply.
    """
    from adloop.auth import set_credentials_provider

    ads_provider = SupabaseCredentialsProvider(ads_lookup)
    set_credentials_provider(SharedGoogleCredentialsProvider(shared_lookup, ads_provider))
