"""Tests for conversion-action write tools (create / update / remove)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2

from adloop.ads import conversion_actions, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, resource_name: str = ""):
        self.resource_name = resource_name


class _FakeConversionActionService:
    def __init__(self, results: list[_FakeResult] | None = None):
        self.operations: list = []
        self.results_to_return = results or []

    def conversion_action_path(self, customer_id: str, ca_id: str) -> str:
        return f"customers/{customer_id}/conversionActions/{ca_id}"

    def mutate_conversion_actions(
        self, customer_id: str, operations: list
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self.results_to_return)


class _FakeClient:
    def __init__(self, services: dict[str, object] | None = None):
        self._base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = self._base.enums
        self.get_type = self._base.get_type
        self._services = services or {}

    def get_service(self, name: str) -> object:
        return self._services[name]


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store._pending_plans.clear()
    yield
    preview_store._pending_plans.clear()


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(require_dry_run=True),
    )


# ---------------------------------------------------------------------------
# Validation tests for draft_create_conversion_action
# ---------------------------------------------------------------------------


class TestDraftCreateConversionActionValidation:
    def _ok_args(self, **overrides):
        defaults = dict(
            customer_id="1234567890",
            name="Calls from Ads",
            type_="AD_CALL",
            category="PHONE_CALL_LEAD",
            default_value=250,
            currency_code="USD",
        )
        defaults.update(overrides)
        return defaults

    def test_happy_path(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args()
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["name"] == "Calls from Ads"
        assert plan.changes["type"] == "AD_CALL"
        assert plan.changes["default_value"] == 250.0
        assert plan.changes["currency_code"] == "USD"
        assert plan.changes["counting_type"] == "ONE_PER_CLICK"
        assert plan.changes["primary_for_goal"] is True

    def test_name_required(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(name="")
        )
        assert result["error"] == "Validation failed"
        assert any("name is required" in d for d in result["details"])

    def test_invalid_type(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(type_="MADE_UP_TYPE")
        )
        assert result["error"] == "Validation failed"
        assert any("MADE_UP_TYPE" in d for d in result["details"])

    def test_invalid_category(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(category="WRONG_CATEGORY")
        )
        assert result["error"] == "Validation failed"
        assert any("WRONG_CATEGORY" in d for d in result["details"])

    def test_invalid_counting_type(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(counting_type="WRONG")
        )
        assert result["error"] == "Validation failed"
        assert any("counting_type" in d for d in result["details"])

    def test_negative_default_value_rejected(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(default_value=-1)
        )
        assert result["error"] == "Validation failed"
        assert any("default_value" in d for d in result["details"])

    def test_invalid_currency_length(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(currency_code="USDX")
        )
        assert result["error"] == "Validation failed"
        assert any("currency_code" in d for d in result["details"])

    def test_invalid_click_through_window(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(click_through_window_days=120)
        )
        assert result["error"] == "Validation failed"
        assert any("click_through_window_days" in d for d in result["details"])

    def test_invalid_view_through_window(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(view_through_window_days=60)
        )
        assert result["error"] == "Validation failed"
        assert any("view_through_window_days" in d for d in result["details"])

    def test_invalid_attribution_model(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config, **self._ok_args(attribution_model="MAGIC")
        )
        assert result["error"] == "Validation failed"
        assert any("attribution_model" in d for d in result["details"])

    def test_phone_call_duration_threshold_persisted(self, config):
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(
                type_="WEBSITE_CALL",
                phone_call_duration_seconds=90,
            ),
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["phone_call_duration_seconds"] == 90


# ---------------------------------------------------------------------------
# draft_update_conversion_action
# ---------------------------------------------------------------------------


class TestDraftUpdateConversionAction:
    def test_id_required(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config, customer_id="1", conversion_action_id=""
        )
        assert "conversion_action_id is required" in result["error"]

    def test_no_fields_to_update_rejected(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
        )
        assert "No fields to update" in result["error"]

    def test_partial_update_only_includes_specified(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            name="Calls from Ads (>=90s)",
            primary_for_goal=False,
            default_value=250,
            currency_code="USD",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        # specified fields present
        assert plan.changes["name"] == "Calls from Ads (>=90s)"
        assert plan.changes["primary_for_goal"] is False
        assert plan.changes["default_value"] == 250.0
        assert plan.changes["currency_code"] == "USD"
        # unspecified fields absent
        assert "counting_type" not in plan.changes
        assert "click_through_window_days" not in plan.changes

    def test_promote_to_primary(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            primary_for_goal=True,
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["primary_for_goal"] is True

    def test_demote_to_secondary(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            primary_for_goal=False,
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["primary_for_goal"] is False

    def test_invalid_counting_type_rejected(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            counting_type="BAD",
        )
        assert result["error"] == "Validation failed"

    def test_phone_duration_persisted(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            phone_call_duration_seconds=90,
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["phone_call_duration_seconds"] == 90


# ---------------------------------------------------------------------------
# draft_remove_conversion_action
# ---------------------------------------------------------------------------


class TestDraftRemoveConversionAction:
    def test_id_required(self, config):
        result = conversion_actions.draft_remove_conversion_action(
            config, customer_id="1", conversion_action_id=""
        )
        assert "conversion_action_id is required" in result["error"]

    def test_emits_irreversible_warning(self, config):
        result = conversion_actions.draft_remove_conversion_action(
            config, customer_id="1", conversion_action_id="6797442210"
        )
        assert "warnings" in result
        assert any("irreversible" in w.lower() for w in result["warnings"])
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "remove_conversion_action"
        assert plan.entity_id == "6797442210"


# ---------------------------------------------------------------------------
# Apply handlers — exercised against fake services
# ---------------------------------------------------------------------------


class TestApplyCreateConversionAction:
    def test_websitecall_with_duration_threshold(self):
        ca_svc = _FakeConversionActionService(
            [_FakeResult("customers/1/conversionActions/100")]
        )
        client = _FakeClient({"ConversionActionService": ca_svc})

        conversion_actions._apply_create_conversion_action(
            client,
            "1",
            {
                "name": "Website Call (GFN >=90s)",
                "type": "WEBSITE_CALL",
                "category": "PHONE_CALL_LEAD",
                "default_value": 250.0,
                "currency_code": "USD",
                "always_use_default_value": True,
                "counting_type": "ONE_PER_CLICK",
                "phone_call_duration_seconds": 90,
                "primary_for_goal": True,
                "include_in_conversions_metric": True,
                "click_through_window_days": 30,
                "view_through_window_days": 1,
                "attribution_model": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
            },
        )

        assert len(ca_svc.operations) == 1
        ca = ca_svc.operations[0].create
        assert ca.name == "Website Call (GFN >=90s)"
        assert ca.type_ == client.enums.ConversionActionTypeEnum.WEBSITE_CALL
        assert ca.category == client.enums.ConversionActionCategoryEnum.PHONE_CALL_LEAD
        assert ca.value_settings.default_value == 250.0
        assert ca.value_settings.default_currency_code == "USD"
        assert ca.value_settings.always_use_default_value is True
        assert ca.counting_type == client.enums.ConversionActionCountingTypeEnum.ONE_PER_CLICK
        assert ca.primary_for_goal is True
        assert ca.phone_call_duration_seconds == 90
        assert ca.click_through_lookback_window_days == 30
        assert ca.view_through_lookback_window_days == 1


class TestApplyUpdateConversionAction:
    def test_partial_update_fieldmask(self):
        ca_svc = _FakeConversionActionService(
            [_FakeResult("customers/1/conversionActions/6797442210")]
        )
        client = _FakeClient({"ConversionActionService": ca_svc})

        conversion_actions._apply_update_conversion_action(
            client,
            "1",
            {
                "conversion_action_id": "6797442210",
                "name": "Calls from Ads (>=90s)",
                "default_value": 250.0,
                "currency_code": "USD",
                "always_use_default_value": True,
                "counting_type": "ONE_PER_CLICK",
                "primary_for_goal": True,
            },
        )

        op = ca_svc.operations[0]
        ca = op.update
        assert ca.resource_name == "customers/1/conversionActions/6797442210"
        assert ca.name == "Calls from Ads (>=90s)"
        assert ca.value_settings.default_value == 250.0
        assert ca.counting_type == client.enums.ConversionActionCountingTypeEnum.ONE_PER_CLICK
        assert ca.primary_for_goal is True
        # Field mask reflects exactly the keys we set
        mask_paths = list(op.update_mask.paths)
        assert "name" in mask_paths
        assert "value_settings.default_value" in mask_paths
        assert "value_settings.default_currency_code" in mask_paths
        assert "value_settings.always_use_default_value" in mask_paths
        assert "counting_type" in mask_paths
        assert "primary_for_goal" in mask_paths
        # Fields we didn't pass shouldn't be in the mask
        assert "phone_call_duration_seconds" not in mask_paths

    def test_update_only_phone_duration(self):
        ca_svc = _FakeConversionActionService(
            [_FakeResult("customers/1/conversionActions/6797442210")]
        )
        client = _FakeClient({"ConversionActionService": ca_svc})

        conversion_actions._apply_update_conversion_action(
            client,
            "1",
            {
                "conversion_action_id": "6797442210",
                "phone_call_duration_seconds": 90,
            },
        )

        op = ca_svc.operations[0]
        ca = op.update
        assert ca.phone_call_duration_seconds == 90
        mask_paths = list(op.update_mask.paths)
        assert mask_paths == ["phone_call_duration_seconds"]


class TestApplyRemoveConversionAction:
    def test_remove_sets_resource_name(self):
        ca_svc = _FakeConversionActionService(
            [_FakeResult("customers/1/conversionActions/6797442210")]
        )
        client = _FakeClient({"ConversionActionService": ca_svc})

        conversion_actions._apply_remove_conversion_action(
            client,
            "1",
            {"conversion_action_id": "6797442210"},
        )

        op = ca_svc.operations[0]
        assert op.remove == "customers/1/conversionActions/6797442210"


# ---------------------------------------------------------------------------
# MCP tool registration + dispatch wiring
# ---------------------------------------------------------------------------


class TestMCPRegistration:
    @pytest.fixture(scope="class")
    def tools_by_name(self):
        import asyncio
        from adloop.server import mcp

        async def _list():
            return await mcp.list_tools()

        tools = asyncio.run(_list())
        return {t.name: t for t in tools}

    def test_three_conversion_action_tools_registered(self, tools_by_name):
        for name in (
            "draft_create_conversion_action",
            "draft_update_conversion_action",
            "draft_remove_conversion_action",
        ):
            assert name in tools_by_name, f"{name} not registered"

    def test_create_required_params(self, tools_by_name):
        required = (
            tools_by_name["draft_create_conversion_action"]
            .parameters.get("required", [])
        )
        assert "name" in required
        assert "type_" in required

    def test_update_requires_id(self, tools_by_name):
        required = (
            tools_by_name["draft_update_conversion_action"]
            .parameters.get("required", [])
        )
        assert "conversion_action_id" in required

    def test_remove_requires_id(self, tools_by_name):
        required = (
            tools_by_name["draft_remove_conversion_action"]
            .parameters.get("required", [])
        )
        assert "conversion_action_id" in required

    def test_dispatch_routes(self):
        import inspect
        src = inspect.getsource(write._execute_plan)
        assert '"create_conversion_action": _apply_create_conversion_action_route' in src
        assert '"update_conversion_action": _apply_update_conversion_action_route' in src
        assert '"remove_conversion_action": _apply_remove_conversion_action_route' in src
