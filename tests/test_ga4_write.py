"""Tests for GA4 key-event drafting and execution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, GA4Config, SafetyConfig
from adloop.ga4 import write as ga4_write
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(
        ga4=GA4Config(property_id="properties/123456"),
        safety=SafetyConfig(require_dry_run=True),
    )


class TestDraft:
    def test_returns_preview_with_future_data_warnings(self, config):
        result = ga4_write.draft_key_event(
            config, property_id="123456", event_name="sign_up",
            counting_method="ONCE_PER_SESSION",
        )

        assert result["operation"] == "create_key_event"
        assert result["entity_id"] == "sign_up"
        assert result["changes"]["counting_method"] == "ONCE_PER_SESSION"
        assert any("FUTURE data" in w for w in result["warnings"])
        assert any("linked" in w for w in result["warnings"])

    def test_strips_properties_prefix(self, config):
        result = ga4_write.draft_key_event(
            config, property_id="properties/123456", event_name="purchase",
        )
        assert result["changes"]["property_id"] == "123456"

    def test_validates_inputs(self, config):
        result = ga4_write.draft_key_event(
            config, property_id="", event_name="", counting_method="ALWAYS",
        )
        details = " ".join(result["details"])
        assert "property_id is required" in details
        assert "event_name is required" in details
        assert "ONCE_PER_EVENT" in details

    def test_respects_blocked_operations(self, config):
        config.safety.blocked_operations = ["create_key_event"]
        result = ga4_write.draft_key_event(
            config, property_id="123456", event_name="sign_up",
        )
        assert "error" in result and "blocked" in result["error"].lower()


class TestApply:
    def test_creates_key_event_via_admin_api(self, config):
        created = MagicMock()
        created.name = "properties/123456/keyEvents/999"
        created.event_name = "sign_up"
        created.counting_method.name = "ONCE_PER_SESSION"
        admin = MagicMock()
        admin.create_key_event.return_value = created

        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = ga4_write._apply_create_key_event(config, {
                "property_id": "123456",
                "event_name": "sign_up",
                "counting_method": "ONCE_PER_SESSION",
            })

        call = admin.create_key_event.call_args
        assert call.kwargs["parent"] == "properties/123456"
        assert call.kwargs["key_event"].event_name == "sign_up"
        assert result["resource_names"] == ["properties/123456/keyEvents/999"]

    def test_execute_plan_routes_ga4_ops_without_ads_client(self, config):
        """GA4-only setups (no developer token) must be able to apply
        key-event plans — the dispatch must not build an Ads client."""
        from adloop.ads import write as ads_write
        from adloop.safety.preview import ChangePlan

        plan = ChangePlan(
            operation="create_key_event",
            entity_type="key_event",
            entity_id="sign_up",
            customer_id="",
            changes={"property_id": "123456", "event_name": "sign_up",
                     "counting_method": "ONCE_PER_EVENT"},
        )

        created = MagicMock()
        created.name = "properties/123456/keyEvents/1"
        created.event_name = "sign_up"
        created.counting_method.name = "ONCE_PER_EVENT"
        admin = MagicMock()
        admin.create_key_event.return_value = created

        def _no_ads_client(*_a, **_k):
            raise AssertionError("Ads client must not be built for GA4 plans")

        with (
            patch("adloop.ads.client.get_ads_client", _no_ads_client),
            patch("adloop.ga4.client.get_admin_client", return_value=admin),
        ):
            result = ads_write._execute_plan(config, plan)

        assert result["event_name"] == "sign_up"


# ---------------------------------------------------------------------------
# Removing a key event
# ---------------------------------------------------------------------------


def _key_event(name="sign_up", deletable=True, method="ONCE_PER_SESSION"):
    ke = MagicMock()
    ke.event_name = name
    ke.counting_method.name = method
    ke.create_time = None
    ke.deletable = deletable
    ke.custom = True
    ke.name = f"properties/123456/keyEvents/{name}-1"
    ke.default_value = None
    return ke


def _admin(*key_events):
    admin = MagicMock()
    admin.list_key_events.return_value = list(key_events)
    by_name = {ke.name: ke for ke in key_events}
    admin.get_key_event.side_effect = lambda name: by_name[name]
    return admin


class TestDraftDelete:
    def test_preview_names_the_event_and_warns_future_only(self, config):
        admin = _admin(_key_event("sign_up"), _key_event("purchase"))
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = ga4_write.draft_delete_key_event(
                config, property_id="properties/123456", event_name="sign_up",
            )

        admin.list_key_events.assert_called_once_with(parent="properties/123456")
        assert result["operation"] == "delete_key_event"
        assert result["entity_id"] == "sign_up"
        assert result["changes"]["resource_name"] == "properties/123456/keyEvents/sign_up-1"
        assert result["changes"]["counting_method"] == "ONCE_PER_SESSION"
        assert result["plan_id"]
        warnings = " ".join(result["warnings"])
        assert "'sign_up'" in warnings
        assert "FUTURE reporting only" in warnings
        assert "keeps firing" in warnings
        admin.delete_key_event.assert_not_called()

    def test_unknown_event_lists_the_existing_key_events(self, config):
        admin = _admin(_key_event("purchase"), _key_event("generate_lead"))
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = ga4_write.draft_delete_key_event(
                config, property_id="123456", event_name="sign_up",
            )
        assert "not a key event" in result["error"]
        assert result["key_events"] == ["generate_lead", "purchase"]

    def test_refuses_undeletable_key_events(self, config):
        admin = _admin(_key_event("purchase", deletable=False))
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = ga4_write.draft_delete_key_event(
                config, property_id="123456", event_name="purchase",
            )
        assert "not deletable" in result["error"]

    def test_validates_inputs_without_calling_the_api(self, config):
        with patch("adloop.ga4.client.get_admin_client") as get_admin:
            result = ga4_write.draft_delete_key_event(
                config, property_id="abc", event_name="",
            )
        details = " ".join(result["details"])
        assert "numeric" in details and "event_name is required" in details
        get_admin.assert_not_called()

    def test_respects_blocked_operations(self, config):
        config.safety.blocked_operations = ["delete_key_event"]
        result = ga4_write.draft_delete_key_event(
            config, property_id="123456", event_name="sign_up",
        )
        assert "blocked" in result["error"].lower()


class TestApplyDelete:
    def _plan(self, config):
        admin = _admin(_key_event("sign_up"))
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            preview = ga4_write.draft_delete_key_event(
                config, property_id="123456", event_name="sign_up",
            )
        return preview["plan_id"], admin

    def test_dry_run_rechecks_the_key_event_and_deletes_nothing(self, config):
        from adloop.ads import write as ads_write

        plan_id, admin = self._plan(config)

        def _no_ads_client(*_a, **_k):
            raise AssertionError("Ads client must not be built for GA4 plans")

        with (
            patch("adloop.ads.client.get_ads_client", _no_ads_client),
            patch("adloop.ga4.client.get_admin_client", return_value=admin),
        ):
            result = ads_write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"] == {"key_event_exists": True, "deletable": True}
        assert "Google Analytics" in result["note"]
        admin.get_key_event.assert_called_once_with(
            name="properties/123456/keyEvents/sign_up-1"
        )
        admin.delete_key_event.assert_not_called()

    def test_dry_run_fails_when_the_key_event_became_undeletable(self, config):
        from adloop.ads import write as ads_write

        plan_id, _ = self._plan(config)
        admin = _admin(_key_event("sign_up", deletable=False))
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = ads_write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_FAILED"
        assert "not deletable" in result["error"]
        assert "Google Analytics" in result["message"]

    def test_apply_deletes_via_admin_api(self, config):
        from adloop.ads import write as ads_write

        config.safety.require_dry_run = False
        plan_id, admin = self._plan(config)
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = ads_write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["status"] == "APPLIED", result
        admin.delete_key_event.assert_called_once_with(
            name="properties/123456/keyEvents/sign_up-1"
        )
        assert result["result"]["deleted"] is True
        assert result["result"]["event_name"] == "sign_up"

    def test_create_key_event_dry_run_still_runs_no_checks(self, config):
        from adloop.ads import write as ads_write

        preview = ga4_write.draft_key_event(
            config, property_id="123456", event_name="sign_up",
        )
        with patch("adloop.ga4.client.get_admin_client") as get_admin:
            result = ads_write.confirm_and_apply(
                config, plan_id=preview["plan_id"], dry_run=True,
            )
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert "checks" not in result
        assert "Google Analytics" in result["message"]
        get_admin.assert_not_called()
