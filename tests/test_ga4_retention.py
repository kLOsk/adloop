"""Tests for the GA4 data retention draft."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.ads import write as ads_write
from adloop.config import AdLoopConfig, GA4Config, SafetyConfig
from adloop.ga4 import retention
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ga4=GA4Config(property_id="properties/123456"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False, two_phase_apply=True),
    )


def _settings(event="TWO_MONTHS", user="TWO_MONTHS", reset=True):
    return SimpleNamespace(
        name="properties/123456/dataRetentionSettings",
        event_data_retention=SimpleNamespace(name=event),
        user_data_retention=SimpleNamespace(name=user),
        reset_user_data_on_new_activity=reset,
    )


def _client(settings=None, level="GOOGLE_ANALYTICS_STANDARD"):
    client = MagicMock()
    state = {"settings": settings or _settings()}
    client.get_data_retention_settings.side_effect = lambda name: state["settings"]
    client.get_property.return_value = SimpleNamespace(service_level=SimpleNamespace(name=level))
    client.update_data_retention_settings.side_effect = lambda data_retention_settings, update_mask: SimpleNamespace(
        name=data_retention_settings.name,
        event_data_retention=data_retention_settings.event_data_retention,
        user_data_retention=data_retention_settings.user_data_retention,
        reset_user_data_on_new_activity=data_retention_settings.reset_user_data_on_new_activity,
    )
    client.state = state
    return client


def _with(client):
    return patch("adloop.ga4.client.get_admin_client", return_value=client)


@pytest.mark.parametrize("value", [14, "14", "14 months", "fourteen_months", "FOURTEEN_MONTHS"])
def test_retention_accepts_months_and_enum_names(value):
    assert retention.parse_retention(value) == ("FOURTEEN_MONTHS", "")


def test_retention_refuses_other_periods():
    name, error = retention.parse_retention(3)
    assert name == "" and "2, 14, 26, 38 or 50" in error


def test_draft_shows_before_and_after(config):
    with _with(_client()):
        preview = retention.draft_ga4_data_retention(config, property_id="123456", event_data_retention="14")
    assert preview["operation"] == "ga4_update_data_retention"
    assert preview["platform"] == "ga4"
    assert preview["changes"]["current_event_data_retention"] == "2 months"
    assert preview["changes"]["event_data_retention"] == "14 months"
    assert "user_data_retention" not in preview["changes"]
    assert preview["requires_double_confirm"] is False
    assert any("privacy policy" in w for w in preview["warnings"])
    assert any("from now on" in w for w in preview["warnings"])


def test_draft_without_a_change_is_no_change(config):
    with _with(_client(_settings(event="FOURTEEN_MONTHS"))):
        result = retention.draft_ga4_data_retention(config, property_id="123456", event_data_retention=14)
    assert result["status"] == "NO_CHANGE"
    assert result["data_retention"]["event_data_retention"] == "14 months"


def test_draft_needs_something_to_change(config):
    result = retention.draft_ga4_data_retention(config, property_id="123456")
    assert result["error"] == "Validation failed"


def test_long_periods_need_360(config):
    with _with(_client()):
        refused = retention.draft_ga4_data_retention(config, property_id="123456", event_data_retention=26)
    assert "360" in refused["error"]
    with _with(_client(level="GOOGLE_ANALYTICS_360")):
        preview = retention.draft_ga4_data_retention(config, property_id="123456", event_data_retention=26)
    assert preview["changes"]["event_data_retention"] == "26 months"


def test_shortening_deletes_data_and_needs_double_confirmation(config):
    with _with(_client(_settings(event="FOURTEEN_MONTHS", user="FOURTEEN_MONTHS"))):
        preview = retention.draft_ga4_data_retention(config, property_id="123456", user_data_retention=2)
    assert preview["requires_double_confirm"] is True
    assert any("cannot be restored" in w for w in preview["warnings"])


def test_apply_writes_only_the_changed_field(config):
    client = _client()
    with _with(client):
        preview = retention.draft_ga4_data_retention(config, property_id="123456", event_data_retention=14)
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        applied = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    assert dry["status"] == "DRY_RUN_SUCCESS"
    assert dry["checks"] == {"data_retention_unchanged": True}
    assert applied["status"] == "APPLIED"
    kwargs = client.update_data_retention_settings.call_args.kwargs
    assert list(kwargs["update_mask"].paths) == ["event_data_retention"]
    assert kwargs["data_retention_settings"].event_data_retention.name == "FOURTEEN_MONTHS"
    assert kwargs["data_retention_settings"].user_data_retention.name == "TWO_MONTHS"
    assert applied["result"]["event_data_months"] == 14


def test_dry_run_refuses_settings_changed_after_the_preview(config):
    client = _client()
    with _with(client):
        preview = retention.draft_ga4_data_retention(config, property_id="123456", event_data_retention=14)
        client.state["settings"] = _settings(event="TWO_MONTHS", reset=False)
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
    assert dry["status"] == "DRY_RUN_FAILED"
    assert "changed in GA4 after the preview" in dry["error"]
    client.update_data_retention_settings.assert_not_called()
