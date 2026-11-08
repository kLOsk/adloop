"""Tests for GA4 custom dimensions and metrics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, GA4Config, SafetyConfig
from adloop.ga4 import definitions
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ga4=GA4Config(property_id="properties/123456"), safety=SafetyConfig())


def _dimension(n, parameter, scope="EVENT", display=None, description=""):
    return SimpleNamespace(
        name=f"properties/123456/customDimensions/{n}", parameter_name=parameter,
        display_name=display or parameter, scope=SimpleNamespace(name=scope), description=description,
        disallow_ads_personalization=False,
    )


def _metric(n, parameter, unit="STANDARD", restricted=()):
    return SimpleNamespace(
        name=f"properties/123456/customMetrics/{n}", parameter_name=parameter, display_name=parameter,
        scope=SimpleNamespace(name="EVENT"), measurement_unit=SimpleNamespace(name=unit), description="",
        restricted_metric_type=[SimpleNamespace(name=r) for r in restricted],
    )


def _client(dimensions=(), metrics=(), level="GOOGLE_ANALYTICS_STANDARD"):
    client = MagicMock()
    client.list_custom_dimensions.return_value = list(dimensions)
    client.list_custom_metrics.return_value = list(metrics)
    client.get_property.return_value = SimpleNamespace(service_level=SimpleNamespace(name=level))
    dims = {d.name: d for d in dimensions}
    mets = {m.name: m for m in metrics}
    client.get_custom_dimension.side_effect = lambda name: dims[name]
    client.get_custom_metric.side_effect = lambda name: mets[name]
    return client


def _with(client):
    return patch("adloop.ga4.client.get_admin_client", return_value=client)


def test_list_shows_definitions_and_slots(config):
    client = _client([_dimension(1, "plan_type"), _dimension(2, "customer_tier", "USER")], [_metric(1, "discount")])
    with _with(client):
        result = definitions.list_ga4_custom_definitions(config, property_id="123456")
    assert result["usage"] == {
        "event_dimensions": {"used": 1, "limit": 50},
        "user_dimensions": {"used": 1, "limit": 25},
        "item_dimensions": {"used": 0, "limit": 10},
        "metrics": {"used": 1, "limit": 50},
    }
    assert result["custom_dimensions"][0]["parameter_name"] == "plan_type"
    assert "insights" not in result


def test_360_properties_get_the_higher_limits(config):
    with _with(_client(level="GOOGLE_ANALYTICS_360")):
        result = definitions.list_ga4_custom_definitions(config, property_id="123456")
    assert result["service_level"] == "360"
    assert result["usage"]["event_dimensions"]["limit"] == 125


class TestCreate:
    def test_preview_warns_about_no_backfill_and_immutable_fields(self, config):
        with _with(_client()):
            preview = definitions.draft_ga4_custom_dimension(config, property_id="123456", parameter_name="plan_type")
        assert preview["operation"] == "ga4_create_custom_dimension"
        assert preview["platform"] == "ga4"
        assert preview["changes"] == {
            "property_id": "123456", "parameter_name": "plan_type", "display_name": "plan_type", "scope": "EVENT",
        }
        text = " ".join(preview["warnings"])
        assert "from now on" in text and "cannot change after creation" in text

    def test_already_registered_parameter_is_refused(self, config):
        with _with(_client([_dimension(1, "plan_type", display="Plan")])):
            result = definitions.draft_ga4_custom_dimension(config, property_id="123456", parameter_name="plan_type")
        assert "already registered as the custom dimension 'Plan'" in result["error"]

    def test_same_parameter_in_another_scope_is_fine(self, config):
        with _with(_client([_dimension(1, "plan_type")])):
            preview = definitions.draft_ga4_custom_dimension(
                config, property_id="123456", parameter_name="plan_type", scope="USER", display_name="Plan user",
            )
        assert preview["changes"]["scope"] == "USER"

    def test_full_property_is_refused_before_google_is_asked(self, config):
        items = [_dimension(i, f"p{i}", "ITEM") for i in range(10)]
        with _with(_client(items)):
            result = definitions.draft_ga4_custom_dimension(
                config, property_id="123456", parameter_name="color", scope="ITEM",
            )
        assert "All 10 item custom dimension slots" in result["error"]

    @pytest.mark.parametrize("kwargs, message", [
        ({"parameter_name": "1bad"}, "start with a letter"),
        ({"parameter_name": "x" * 41}, "allows 40"),
        ({"parameter_name": "x" * 25, "scope": "USER"}, "allows 24 for user scope"),
        ({"parameter_name": "ok", "display_name": "Bad-Name"}, "display_name must start"),
        ({"parameter_name": "ok", "scope": "SESSION"}, "scope must be one of"),
        ({}, "parameter_name is required"),
    ])
    def test_validation(self, config, kwargs, message):
        result = definitions.draft_ga4_custom_dimension(config, property_id="123456", **kwargs)
        assert message in " ".join(result["details"])

    def test_currency_metric_needs_a_restricted_type(self, config):
        with _with(_client()):
            result = definitions.draft_ga4_custom_metric(
                config, property_id="123456", parameter_name="refund", measurement_unit="CURRENCY",
            )
            assert "COST_DATA or REVENUE_DATA" in result["error"]
            preview = definitions.draft_ga4_custom_metric(
                config, property_id="123456", parameter_name="refund", measurement_unit="CURRENCY",
                restricted_metric_type=["revenue_data"],
            )
        assert preview["changes"]["restricted_metric_type"] == ["REVENUE_DATA"]

    def test_apply_creates_via_admin_api(self, config):
        client = _client()
        client.create_custom_dimension.return_value = _dimension(9, "plan_type")
        with _with(client):
            result = definitions.APPLIERS["ga4_create_custom_dimension"](config, {
                "property_id": "123456", "parameter_name": "plan_type", "display_name": "Plan", "scope": "EVENT",
            })
        sent = client.create_custom_dimension.call_args.kwargs["custom_dimension"]
        assert (sent.parameter_name, sent.display_name, sent.scope.name) == ("plan_type", "Plan", "EVENT")
        assert result["resource_names"] == ["properties/123456/customDimensions/9"]

    def test_dry_run_rechecks_duplicates(self, config):
        from adloop.ads import write as ads_write

        with _with(_client()):
            preview = definitions.draft_ga4_custom_dimension(config, property_id="123456", parameter_name="plan_type")
        with _with(_client([_dimension(1, "plan_type")])):
            result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "registered in the meantime" in str(result)


class TestUpdateAndArchive:
    def test_rename_shows_before_and_after(self, config):
        with _with(_client([_dimension(1, "plan_type", display="Plan")])):
            preview = definitions.draft_ga4_custom_dimension(
                config, property_id="123456", action="update", parameter_name="plan_type", display_name="Plan type",
            )
        assert preview["changes"]["display_name"] == "Plan type"
        assert preview["changes"]["current_display_name"] == "Plan"

    def test_archive_needs_double_confirmation_and_warns(self, config):
        with _with(_client([], [_metric(1, "discount")])):
            preview = definitions.draft_ga4_custom_metric(
                config, property_id="123456", action="archive", parameter_name="discount",
            )
        assert preview["requires_double_confirm"] is True
        assert "cannot be undone" in preview["warnings"][0]

    def test_ambiguous_parameter_asks_for_scope(self, config):
        client = _client([_dimension(1, "plan_type"), _dimension(2, "plan_type", "USER", display="Plan user")])
        with _with(client):
            result = definitions.draft_ga4_custom_dimension(
                config, property_id="123456", action="archive", parameter_name="plan_type", scope="",
            )
        assert "more than one scope" in result["error"]

    def test_update_masks_changed_fields_and_refuses_stale(self, config):
        client = _client([_dimension(1, "plan_type", display="Plan")])
        client.update_custom_dimension.return_value = _dimension(1, "plan_type", display="Plan type")
        with _with(client):
            preview = definitions.draft_ga4_custom_dimension(
                config, property_id="123456", action="update", parameter_name="plan_type", display_name="Plan type",
            )
            definitions.APPLIERS["ga4_update_custom_dimension"](config, preview["changes"])
        assert list(client.update_custom_dimension.call_args.kwargs["update_mask"].paths) == ["display_name"]

        with _with(_client([_dimension(1, "plan_type", display="Renamed in GA4")])):
            with pytest.raises(RuntimeError, match="changed in GA4 after the preview"):
                definitions.APPLIERS["ga4_update_custom_dimension"](config, preview["changes"])

    def test_archive_calls_the_archive_endpoint(self, config):
        client = _client([], [_metric(1, "discount")])
        with _with(client):
            preview = definitions.draft_ga4_custom_metric(
                config, property_id="123456", action="archive", parameter_name="discount",
            )
            result = definitions.APPLIERS["ga4_archive_custom_metric"](config, preview["changes"])
        client.archive_custom_metric.assert_called_once_with(name="properties/123456/customMetrics/1")
        assert result["archived"] is True
