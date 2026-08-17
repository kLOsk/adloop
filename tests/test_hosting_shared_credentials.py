"""Tests for the shared Google reporting credentials provider (GA4/GTM/GSC)."""

import pytest

from adloop.config import AdLoopConfig
from adloop.hosting.credentials import (
    MissingGoogleConnection,
    SupabaseCredentialsProvider,
)
from adloop.hosting.shared_credentials import (
    GA4_READONLY_SCOPE,
    GSC_READONLY_SCOPE,
    GTM_READONLY_SCOPE,
    SharedGoogleCredentialsProvider,
    install_shared_credentials_provider,
)
from adloop.runtime import use_runtime


def _with_shared_client_env(monkeypatch):
    for svc in ("GA4", "GTM", "GSC"):
        monkeypatch.setenv(f"ADLOOP_{svc}_CLIENT_ID", f"{svc.lower()}-client-id")
        monkeypatch.setenv(f"ADLOOP_{svc}_CLIENT_SECRET", f"{svc.lower()}-secret")


def _provider(shared_lookup):
    ads = SupabaseCredentialsProvider(token_lookup=lambda t: f"ads-{t}")
    return SharedGoogleCredentialsProvider(shared_lookup, ads)


def test_ga4_credentials_from_shared_token(monkeypatch):
    _with_shared_client_env(monkeypatch)
    prov = _provider(lambda s: f"rt-{s}")
    creds = prov.ga4_credentials(AdLoopConfig())
    # Shared (service-keyed), not tenant-keyed: no use_runtime binding needed.
    assert creds.refresh_token == "rt-ga4"
    assert creds.client_id == "ga4-client-id"
    assert list(creds.scopes or []) == [GA4_READONLY_SCOPE]


def test_each_service_uses_its_own_isolated_scope(monkeypatch):
    _with_shared_client_env(monkeypatch)
    prov = _provider(lambda s: f"rt-{s}")
    assert list(prov.gtm_credentials(AdLoopConfig()).scopes or []) == [GTM_READONLY_SCOPE]
    assert list(prov.gsc_credentials(AdLoopConfig()).scopes or []) == [GSC_READONLY_SCOPE]
    # One token never carries more than its single product scope.
    assert GTM_READONLY_SCOPE not in (prov.ga4_credentials(AdLoopConfig()).scopes or [])


def test_missing_shared_connection_raises(monkeypatch):
    _with_shared_client_env(monkeypatch)
    prov = _provider(lambda s: None)
    with pytest.raises(MissingGoogleConnection):
        prov.ga4_credentials(AdLoopConfig())


def test_missing_client_env_raises(monkeypatch):
    monkeypatch.delenv("ADLOOP_GA4_CLIENT_ID", raising=False)
    monkeypatch.delenv("ADLOOP_GA4_CLIENT_SECRET", raising=False)
    prov = _provider(lambda s: "rt")
    with pytest.raises(RuntimeError):
        prov.ga4_credentials(AdLoopConfig())


def test_ads_delegated_to_per_user_provider(monkeypatch):
    # Ads stays per-user: keyed by the bound tenant, using the Ads Web client.
    monkeypatch.setenv("ADLOOP_GOOGLE_CLIENT_ID", "ads-web-id")
    monkeypatch.setenv("ADLOOP_GOOGLE_CLIENT_SECRET", "ads-web-secret")
    prov = _provider(lambda s: "rt-shared")
    with use_runtime(AdLoopConfig(), tenant="user-1"):
        creds = prov.ads_credentials(AdLoopConfig())
    assert creds.refresh_token == "ads-user-1"


def test_supports_gtm_gsc_but_not_merchant():
    prov = _provider(lambda s: "rt")
    assert hasattr(prov, "gtm_credentials")
    assert hasattr(prov, "gsc_credentials")
    # No shared Merchant connection -> upstream reports it unsupported.
    assert not hasattr(prov, "merchant_credentials")


def test_install_sets_shared_provider():
    from adloop import auth as gauth

    prior = gauth.get_credentials_provider()
    try:
        install_shared_credentials_provider(shared_lookup=lambda s: "rt", ads_lookup=lambda t: "ads")
        assert isinstance(gauth.get_credentials_provider(), SharedGoogleCredentialsProvider)
    finally:
        gauth.set_credentials_provider(prior)
