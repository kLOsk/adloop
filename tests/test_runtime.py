"""Tests for the runtime context — config resolution, tenant scoping, and
the multi-tenant safety boundaries built on top of it."""

from unittest.mock import MagicMock, patch

import pytest

from adloop import runtime
from adloop.config import AdLoopConfig
from adloop.safety import audit, preview
from adloop.safety.preview import ChangePlan, InMemoryPlanStore


@pytest.fixture(autouse=True)
def _clean_runtime():
    """Reset all process-global runtime state around every test."""
    runtime.set_deployment_mode("local")
    runtime.set_default_config(None)
    preview.set_plan_store(InMemoryPlanStore())
    audit.set_audit_sink(audit.FileAuditSink())
    yield
    runtime.set_deployment_mode("local")
    runtime.set_default_config(None)
    preview.set_plan_store(InMemoryPlanStore())
    audit.set_audit_sink(audit.FileAuditSink())


class TestConfigResolution:
    def test_bound_config_wins_over_default(self):
        bound = AdLoopConfig()
        fallback = AdLoopConfig()
        runtime.set_default_config(fallback)
        with runtime.use_runtime(bound):
            assert runtime.current_config() is bound
        assert runtime.current_config() is fallback

    def test_lazy_load_in_local_mode(self):
        cfg = AdLoopConfig()
        with patch("adloop.config.load_config", return_value=cfg) as loader:
            assert runtime.current_config() is cfg
            # Second call reuses the cached default — no reload.
            assert runtime.current_config() is cfg
            loader.assert_called_once()

    def test_server_mode_requires_bound_config(self):
        runtime.set_deployment_mode("server")
        with pytest.raises(RuntimeError, match="use_runtime"):
            runtime.current_config()

    def test_server_mode_with_bound_config_works(self):
        runtime.set_deployment_mode("server")
        bound = AdLoopConfig()
        with runtime.use_runtime(bound, tenant="acme"):
            assert runtime.current_config() is bound

    def test_use_runtime_resets_on_exit(self):
        bound = AdLoopConfig()
        with runtime.use_runtime(bound, tenant="acme"):
            assert runtime.current_tenant() == "acme"
        assert runtime.current_tenant() == runtime.LOCAL_TENANT

    def test_invalid_deployment_mode_rejected(self):
        with pytest.raises(ValueError):
            runtime.set_deployment_mode("cloud")


class TestPlanStoreTenantIsolation:
    def test_tenant_cannot_read_other_tenants_plan(self):
        cfg = AdLoopConfig()
        plan = ChangePlan(operation="pause")
        with runtime.use_runtime(cfg, tenant="tenant_a"):
            preview.store_plan(plan)
            assert preview.get_plan(plan.plan_id) is plan
        with runtime.use_runtime(cfg, tenant="tenant_b"):
            assert preview.get_plan(plan.plan_id) is None

    def test_tenant_cannot_remove_other_tenants_plan(self):
        cfg = AdLoopConfig()
        plan = ChangePlan(operation="pause")
        with runtime.use_runtime(cfg, tenant="tenant_a"):
            preview.store_plan(plan)
        with runtime.use_runtime(cfg, tenant="tenant_b"):
            preview.remove_plan(plan.plan_id)
        with runtime.use_runtime(cfg, tenant="tenant_a"):
            assert preview.get_plan(plan.plan_id) is plan

    def test_local_mode_roundtrip_unchanged(self):
        plan = ChangePlan(operation="pause")
        preview.store_plan(plan)
        assert preview.get_plan(plan.plan_id) is plan
        preview.remove_plan(plan.plan_id)
        assert preview.get_plan(plan.plan_id) is None


class TestAuditSink:
    def test_file_sink_writes_json_line_with_tenant(self, tmp_path):
        log_file = tmp_path / "audit.log"
        audit.log_mutation(
            str(log_file),
            operation="pause_entity",
            customer_id="123",
            dry_run=True,
        )
        import json

        record = json.loads(log_file.read_text().strip())
        assert record["operation"] == "pause_entity"
        assert record["tenant"] == runtime.LOCAL_TENANT
        assert record["dry_run"] is True

    def test_custom_sink_receives_records(self):
        captured = []

        class _Sink:
            def record(self, entry, *, log_file):
                captured.append((entry, log_file))

        audit.set_audit_sink(_Sink())
        cfg = AdLoopConfig()
        with runtime.use_runtime(cfg, tenant="acme"):
            audit.log_mutation("ignored.log", operation="update_campaign")
        assert len(captured) == 1
        entry, log_file = captured[0]
        assert entry["tenant"] == "acme"
        assert log_file == "ignored.log"


class TestSsrfGuard:
    def _err(self, url):
        from adloop.ads.write import _ssrf_error

        return _ssrf_error(url)

    def test_rejects_loopback(self):
        assert "non-public" in self._err("http://127.0.0.1/page")

    def test_rejects_private_range(self):
        assert "non-public" in self._err("https://10.0.0.5/admin")

    def test_rejects_metadata_endpoint(self):
        assert "non-public" in self._err("http://169.254.169.254/latest/meta-data")

    def test_rejects_non_http_scheme(self):
        assert "scheme" in self._err("file:///etc/passwd")

    def test_rejects_missing_hostname(self):
        assert self._err("http:///nohost") is not None

    def test_allows_public_ip(self):
        assert self._err("https://93.184.216.34/") is None

    def test_hostname_resolving_to_private_rejected(self):
        fake = [(2, 1, 6, "", ("192.168.1.10", 0))]
        with patch("socket.getaddrinfo", return_value=fake):
            assert "non-public" in self._err("https://internal.example.com/")

    def test_hostname_resolving_to_public_allowed(self):
        fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with patch("socket.getaddrinfo", return_value=fake):
            assert self._err("https://example.com/") is None

    def test_unresolvable_hostname_rejected(self):
        import socket as socket_mod

        with patch(
            "socket.getaddrinfo", side_effect=socket_mod.gaierror("NXDOMAIN")
        ):
            assert "does not resolve" in self._err("https://nope.invalid/")


class TestCrossTenantConfirmBlocked:
    """The attack the tenant-scoped plan store exists to prevent: tenant B
    obtaining tenant A's plan_id and applying A's pending mutation."""

    def test_confirm_and_apply_rejects_foreign_plan(self, tmp_path):
        from adloop.ads import write
        from adloop.config import SafetyConfig

        def _cfg():
            return AdLoopConfig(
                safety=SafetyConfig(log_file=str(tmp_path / "audit.log"))
            )

        with runtime.use_runtime(_cfg(), tenant="tenant_a"):
            drafted = write.add_negative_keywords(
                _cfg(), campaign_id="1001", keywords=["free"], match_type="EXACT"
            )
            plan_id = drafted["plan_id"]

        with runtime.use_runtime(_cfg(), tenant="tenant_b"):
            result = write.confirm_and_apply(_cfg(), plan_id=plan_id, dry_run=True)
            assert "No pending plan found" in result["error"]

        with runtime.use_runtime(_cfg(), tenant="tenant_a"):
            result = write.confirm_and_apply(_cfg(), plan_id=plan_id, dry_run=True)
            assert result["status"] == "DRY_RUN_SUCCESS"


class TestServerModeGates:
    def test_local_credentials_provider_refuses_server_mode(self):
        from adloop.auth import get_ads_credentials

        runtime.set_deployment_mode("server")
        with pytest.raises(RuntimeError, match="server mode"):
            get_ads_credentials(AdLoopConfig())

    def test_draft_image_assets_gated_in_server_mode(self):
        from adloop.ads.write import draft_image_assets

        runtime.set_deployment_mode("server")
        result = draft_image_assets(MagicMock(), campaign_id="1", image_paths=["x.png"])
        assert "not available on the hosted server" in result["error"]

    def test_local_gtm_credentials_refuse_server_mode(self):
        from adloop.auth import get_gtm_credentials

        runtime.set_deployment_mode("server")
        with pytest.raises(RuntimeError, match="server mode"):
            get_gtm_credentials(AdLoopConfig())

    def test_local_gsc_credentials_refuse_server_mode(self):
        from adloop.auth import get_gsc_credentials

        runtime.set_deployment_mode("server")
        with pytest.raises(RuntimeError, match="server mode"):
            get_gsc_credentials(AdLoopConfig())

    def test_provider_without_gsc_support_fails_with_capability_error(self):
        from adloop import auth

        class _NoGscProvider:
            def ga4_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

            def ads_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

        original = auth.get_credentials_provider()
        auth.set_credentials_provider(_NoGscProvider())
        try:
            with pytest.raises(RuntimeError, match="does not support"):
                auth.get_gsc_credentials(AdLoopConfig())
        finally:
            auth.set_credentials_provider(original)

    def test_local_merchant_credentials_refuse_server_mode(self):
        from adloop.auth import get_merchant_credentials

        runtime.set_deployment_mode("server")
        with pytest.raises(RuntimeError, match="server mode"):
            get_merchant_credentials(AdLoopConfig())

    def test_provider_without_merchant_support_fails_with_capability_error(self):
        from adloop import auth

        class _NoMerchantProvider:
            def ga4_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

            def ads_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

        original = auth.get_credentials_provider()
        auth.set_credentials_provider(_NoMerchantProvider())
        try:
            with pytest.raises(RuntimeError, match="does not support"):
                auth.get_merchant_credentials(AdLoopConfig())
        finally:
            auth.set_credentials_provider(original)

    def test_local_reddit_credentials_refuse_server_mode(self):
        from adloop.auth import get_reddit_credentials

        runtime.set_deployment_mode("server")
        with pytest.raises(RuntimeError, match="server mode"):
            get_reddit_credentials(AdLoopConfig())

    def test_provider_without_reddit_support_fails_with_capability_error(self):
        from adloop import auth

        class _NoRedditProvider:
            def ga4_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

            def ads_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

        original = auth.get_credentials_provider()
        auth.set_credentials_provider(_NoRedditProvider())
        try:
            with pytest.raises(RuntimeError, match="does not support"):
                auth.get_reddit_credentials(AdLoopConfig())
        finally:
            auth.set_credentials_provider(original)

    def test_provider_without_gtm_support_fails_with_capability_error(self):
        """A provider that predates GTM (e.g. a hosted deployment that
        hasn't rolled it out) must produce a clear capability error, not an
        AttributeError or a fallback to the wrong credentials."""
        from adloop import auth

        class _AdsGa4OnlyProvider:
            def ga4_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

            def ads_credentials(self, config):  # pragma: no cover
                raise AssertionError("not exercised")

        original = auth.get_credentials_provider()
        auth.set_credentials_provider(_AdsGa4OnlyProvider())
        try:
            with pytest.raises(RuntimeError, match="does not support"):
                auth.get_gtm_credentials(AdLoopConfig())
        finally:
            auth.set_credentials_provider(original)
