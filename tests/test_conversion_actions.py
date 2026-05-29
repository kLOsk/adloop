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

    def test_default_value_auto_corrects_always_use_default_value(self, config):
        """Regression: Google's API rejects (INVALID_VALUE) a create where
        default_value > 0 is paired with always_use_default_value=False.
        The draft tool must auto-correct to True so callers don't have to
        remember the flag every time they pass a default_value."""
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(default_value=400, always_use_default_value=False),
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["default_value"] == 400.0
        assert plan.changes["always_use_default_value"] is True

    def test_zero_default_value_keeps_always_use_default_value_false(
        self, config
    ):
        """The auto-correct must NOT trigger when default_value is 0 —
        callers may legitimately want to use snippet-provided values."""
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(
                default_value=0, always_use_default_value=False
            ),
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["always_use_default_value"] is False

    def test_explicit_always_use_default_value_true_preserved(self, config):
        """Sanity: explicit True is preserved (no regression for callers
        who already set it correctly)."""
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(default_value=500, always_use_default_value=True),
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["always_use_default_value"] is True


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

    def test_does_not_set_include_in_conversions_metric_on_create(self):
        """Regression: Google's API treats include_in_conversions_metric
        as IMMUTABLE on create (derived from category). Setting it in the
        create mutate raises IMMUTABLE_FIELD. The apply function must
        leave the proto field unset; callers who need to change it must
        use draft_update_conversion_action after the create succeeds."""
        ca_svc = _FakeConversionActionService(
            [_FakeResult("customers/1/conversionActions/100")]
        )
        client = _FakeClient({"ConversionActionService": ca_svc})

        conversion_actions._apply_create_conversion_action(
            client,
            "1",
            {
                "name": "BGI - Tint - Call from Ad",
                "type": "AD_CALL",
                "category": "PHONE_CALL_LEAD",
                "default_value": 400.0,
                "currency_code": "USD",
                "always_use_default_value": True,
                "counting_type": "ONE_PER_CLICK",
                "phone_call_duration_seconds": 0,
                "primary_for_goal": False,
                # Caller passes True (the tool's default), but the apply
                # function must NOT propagate it into the proto on create.
                "include_in_conversions_metric": True,
                "click_through_window_days": 30,
                "view_through_window_days": 0,
                "attribution_model": "",
            },
        )

        assert len(ca_svc.operations) == 1
        ca = ca_svc.operations[0].create
        # The proto3-optional field must not be explicitly set, otherwise
        # Google rejects the mutate with IMMUTABLE_FIELD.
        assert not ca._pb.HasField("include_in_conversions_metric")


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
        assert '"upload_call_conversions": _apply_upload_call_conversions_route' in src

    def test_upload_call_conversions_tool_registered(self, tools_by_name):
        assert "draft_upload_call_conversions" in tools_by_name

    def test_upload_call_conversions_requires_csv_path(self, tools_by_name):
        required = (
            tools_by_name["draft_upload_call_conversions"]
            .parameters.get("required", [])
        )
        assert "csv_path" in required


# ---------------------------------------------------------------------------
# Call-conversion CSV parsing helpers
# ---------------------------------------------------------------------------


class TestNormalizeCallTimestamp:
    def test_strips_fractional_seconds_and_z(self):
        out = conversion_actions._normalize_call_timestamp(
            "2026-02-26T16:49:44.5679977Z"
        )
        assert out == "2026-02-26 16:49:44+00:00"

    def test_replaces_t_only(self):
        out = conversion_actions._normalize_call_timestamp(
            "2026-02-26T16:49:44Z"
        )
        assert out == "2026-02-26 16:49:44+00:00"

    def test_preserves_offset(self):
        out = conversion_actions._normalize_call_timestamp(
            "2026-02-26T16:49:44.123-08:00"
        )
        assert out == "2026-02-26 16:49:44-08:00"

    def test_empty(self):
        assert conversion_actions._normalize_call_timestamp("") == ""


class TestParseCallConversionCsv:
    def _write_csv(self, tmp_path, content):
        p = tmp_path / "phone.csv"
        p.write_text(content)
        return str(p)

    def test_missing_file(self, tmp_path):
        rows, errors = conversion_actions._parse_call_conversion_csv(
            str(tmp_path / "does-not-exist.csv")
        )
        assert rows == []
        assert any("not found" in e for e in errors)

    def test_skips_parameters_and_comments(self, tmp_path):
        csv_text = (
            "Parameters:TimeZone=America/Los_Angeles,,,,,,\n"
            "Caller's Phone Number,Call Start Time,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,"
            "Ad User Data,Ad Personalization\n"
            "+19165550100,2026-03-01T12:00:00Z,Test Action,"
            "2026-03-01T13:00:00Z,250.00,USD,,\n"
        )
        path = self._write_csv(tmp_path, csv_text)
        rows, errors = conversion_actions._parse_call_conversion_csv(path)
        assert errors == []
        assert len(rows) == 1
        r = rows[0]
        assert r["caller_id"] == "+19165550100"
        assert r["call_start_time"] == "2026-03-01 12:00:00+00:00"
        assert r["conversion_name"] == "Test Action"
        assert r["conversion_value"] == 250.0
        assert r["currency_code"] == "USD"

    def test_missing_required_column(self, tmp_path):
        csv_text = (
            "Caller's Phone Number,Call Start Time,Conversion Name,"
            "Conversion Time,Conversion Value\n"  # missing Currency
            "+19165550100,2026-03-01T12:00:00Z,X,2026-03-01T13:00:00Z,1\n"
        )
        path = self._write_csv(tmp_path, csv_text)
        rows, errors = conversion_actions._parse_call_conversion_csv(path)
        assert rows == []
        assert any("Conversion Currency" in e for e in errors)

    def test_invalid_value_row_skipped(self, tmp_path):
        csv_text = (
            "Caller's Phone Number,Call Start Time,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "+19165550100,2026-03-01T12:00:00Z,X,2026-03-01T13:00:00Z,not-a-num,USD\n"
            "+19165550101,2026-03-01T12:01:00Z,X,2026-03-01T13:01:00Z,99.0,USD\n"
        )
        path = self._write_csv(tmp_path, csv_text)
        rows, errors = conversion_actions._parse_call_conversion_csv(path)
        assert len(rows) == 1
        assert rows[0]["caller_id"] == "+19165550101"
        assert any("Conversion Value" in e for e in errors)


# ---------------------------------------------------------------------------
# Draft validation for upload_call_conversions
# ---------------------------------------------------------------------------


class TestDraftUploadCallConversions:
    def _write_valid_csv(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            "Parameters:TimeZone=America/Los_Angeles,,,,,,\n"
            "Caller's Phone Number,Call Start Time,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,"
            "Ad User Data,Ad Personalization\n"
            "+19165550100,2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,250.00,USD,,\n"
            "+19165550101,2026-03-02T12:00:00Z,A,2026-03-02T13:00:00Z,500.00,USD,,\n"
            "+19165550102,2026-03-03T12:00:00Z,B,2026-03-03T13:00:00Z,75.00,USD,,\n"
        )
        return str(p)

    def test_missing_csv_returns_error(self, config, tmp_path):
        result = conversion_actions.draft_upload_call_conversions(
            config,
            customer_id="1234567890",
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert "error" in result

    def test_happy_path_preview(self, config, tmp_path):
        path = self._write_valid_csv(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config,
            customer_id="1234567890",
            csv_path=path,
        )
        assert "plan_id" in result
        assert result["operation"] == "upload_call_conversions"
        assert result["entity_type"] == "call_conversion_batch"
        c = result["changes"]
        assert c["row_count"] == 3
        assert c["total_value"] == 825.00
        assert c["distinct_conversion_actions"] == ["A", "B"]
        assert c["partial_failure"] is True
        assert len(c["sample_rows"]) == 3
        assert c["sample_rows"][0]["caller_id"] == "+19165550100"

    def test_plan_stored(self, config, tmp_path):
        path = self._write_valid_csv(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
        )
        plan = preview_store.get_plan(result["plan_id"])
        assert plan is not None
        assert plan.operation == "upload_call_conversions"
        assert plan.changes["csv_path"] == path

    def test_safety_blocked_operation(self, tmp_path):
        from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(blocked_operations=["upload_call_conversions"]),
        )
        path = self._write_valid_csv(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            cfg, customer_id="1234567890", csv_path=path,
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Apply (mock upload + GAQL action-name lookup)
# ---------------------------------------------------------------------------


class _FakeUploadService:
    def __init__(self, results_count: int = 0, error_message: str = ""):
        self.called_with: dict | None = None
        self._results_count = results_count
        self._error_message = error_message

    def upload_call_conversions(self, *, customer_id, conversions, partial_failure):
        self.called_with = {
            "customer_id": customer_id,
            "conversions": list(conversions),
            "partial_failure": partial_failure,
        }
        results = [
            SimpleNamespace(caller_id=c.caller_id) for c in conversions[:self._results_count]
        ] + [SimpleNamespace(caller_id="") for _ in conversions[self._results_count:]]
        partial = SimpleNamespace(
            message=self._error_message, code=0
        ) if self._error_message else SimpleNamespace(message="", code=0)
        return SimpleNamespace(results=results, partial_failure_error=partial)


class _FakeSearchRow:
    def __init__(self, name: str, resource_name: str, type_name: str = "UPLOAD_CALLS"):
        self.conversion_action = SimpleNamespace(
            name=name,
            resource_name=resource_name,
            type_=SimpleNamespace(name=type_name),
            status=SimpleNamespace(name="ENABLED"),
            id=int(resource_name.split("/")[-1]),
        )


class _FakeGoogleAdsService:
    def __init__(self, rows: list):
        self._rows = rows
        self.last_query = ""

    def search(self, *, customer_id, query):
        self.last_query = query
        return iter(self._rows)


def _client_with(*, upload_service, ads_service):
    fake = _FakeClient({
        "ConversionUploadService": upload_service,
        "GoogleAdsService": ads_service,
    })
    return fake


class TestApplyUploadCallConversions:
    def _make_changes(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            "Parameters:TimeZone=America/Los_Angeles,,,,,,\n"
            "Caller's Phone Number,Call Start Time,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,"
            "Ad User Data,Ad Personalization\n"
            "+19165550100,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,250.00,USD,,\n"
            "+19165550101,2026-03-02T12:00:00Z,My Action,"
            "2026-03-02T13:00:00Z,500.00,USD,,\n"
        )
        return {
            "csv_path": str(p),
            "row_count": 2,
            "partial_failure": True,
        }

    def test_full_success(self, tmp_path):
        upload = _FakeUploadService(results_count=2)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)

        result = conversion_actions._apply_upload_call_conversions(
            client, "1", self._make_changes(tmp_path)
        )

        assert result["uploaded_total"] == 2
        assert result["success_count"] == 2
        assert result["failure_count"] == 0
        assert result["conversion_actions_used"] == {
            "My Action": "customers/1/conversionActions/777"
        }
        # Verify the API got the right shape
        assert upload.called_with["customer_id"] == "1"
        assert upload.called_with["partial_failure"] is True
        sent = upload.called_with["conversions"]
        assert len(sent) == 2
        assert sent[0].caller_id == "+19165550100"
        assert sent[0].conversion_action == "customers/1/conversionActions/777"
        assert sent[0].conversion_value == 250.0
        assert sent[0].currency_code == "USD"
        # Timestamp normalized
        assert sent[0].call_start_date_time == "2026-03-01 12:00:00+00:00"

    def test_partial_failure_message_surfaced(self, tmp_path):
        upload = _FakeUploadService(
            results_count=1, error_message="one row was bad"
        )
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)

        result = conversion_actions._apply_upload_call_conversions(
            client, "1", self._make_changes(tmp_path)
        )

        assert result["success_count"] == 1
        assert result["failure_count"] == 1
        assert any(
            e["type"] == "partial_failure" and e["message"] == "one row was bad"
            for e in result["row_errors"]
        )

    def test_action_not_found_raises(self, tmp_path):
        upload = _FakeUploadService(results_count=0)
        ads = _FakeGoogleAdsService([])  # No matching action
        client = _client_with(upload_service=upload, ads_service=ads)

        with pytest.raises(ValueError) as exc:
            conversion_actions._apply_upload_call_conversions(
                client, "1", self._make_changes(tmp_path)
            )
        assert "not found" in str(exc.value)

    def test_wrong_type_raises(self, tmp_path):
        upload = _FakeUploadService(results_count=0)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Action",
                "customers/1/conversionActions/777",
                type_name="UPLOAD_CLICKS",  # Wrong — should be UPLOAD_CALLS
            )
        ])
        client = _client_with(upload_service=upload, ads_service=ads)

        with pytest.raises(ValueError) as exc:
            conversion_actions._apply_upload_call_conversions(
                client, "1", self._make_changes(tmp_path)
            )
        assert "UPLOAD_CALLS" in str(exc.value)

    def test_empty_csv_returns_error(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("")
        upload = _FakeUploadService()
        ads = _FakeGoogleAdsService([])
        client = _client_with(upload_service=upload, ads_service=ads)

        result = conversion_actions._apply_upload_call_conversions(
            client, "1", {"csv_path": str(p), "partial_failure": True}
        )
        assert "error" in result
        assert upload.called_with is None  # Never called


# ---------------------------------------------------------------------------
# Enhanced Conversions for Leads — parsing + draft + apply
# ---------------------------------------------------------------------------


class TestParseEcForLeadsCsv:
    def _write(self, tmp_path, content):
        p = tmp_path / "ec.csv"
        p.write_text(content)
        return str(p)

    def test_missing_file(self, tmp_path):
        rows, errors = conversion_actions._parse_ec_for_leads_csv(
            str(tmp_path / "missing.csv")
        )
        assert rows == []
        assert any("not found" in e for e in errors)

    def test_happy_path(self, tmp_path):
        path = self._write(
            tmp_path,
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "aaaa,bbbb,cccc,dddd,My Action,2026-03-01T12:00:00Z,250.00,USD\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert errors == []
        assert len(rows) == 1
        r = rows[0]
        assert r["email_sha256"] == "aaaa"
        assert r["phone_sha256"] == "bbbb"
        assert r["conversion_name"] == "My Action"
        assert r["conversion_value"] == 250.0
        assert r["conversion_time"] == "2026-03-01 12:00:00+00:00"

    def test_missing_required_column(self, tmp_path):
        path = self._write(
            tmp_path,
            "Email,Phone Number,Conversion Name,Conversion Time,"
            "Conversion Value,Conversion Currency\n"  # missing First/Last
            "aaaa,bbbb,X,2026-03-01T12:00:00Z,100,USD\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows == []
        assert any("First Name" in e or "Last Name" in e for e in errors)


class TestDraftUploadEnhancedConversionsForLeads:
    def _write(self, tmp_path):
        p = tmp_path / "ec.csv"
        p.write_text(
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "aaa,bbb,ccc,ddd,Job Close,2026-03-01T12:00:00Z,500.00,USD\n"
            "eee,fff,ggg,hhh,Job Close,2026-03-02T12:00:00Z,1500.00,USD\n"
            ",zzz,,,Job Close,2026-03-03T12:00:00Z,200.00,USD\n"
        )
        return str(p)

    def test_happy_path_preview(self, config, tmp_path):
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path,
        )
        assert "plan_id" in result
        assert result["operation"] == "upload_enhanced_conversions_for_leads"
        c = result["changes"]
        assert c["row_count"] == 3
        assert c["total_value"] == 2200.00
        assert c["rows_with_email"] == 2
        assert c["rows_with_phone"] == 3
        assert c["distinct_conversion_actions"] == ["Job Close"]
        # PII in sample is truncated
        assert "..." in c["sample_rows"][0]["email_sha256"]


class _FakeClickUploadService:
    def __init__(self, results_count: int = 0, error_message: str = ""):
        self.called_with: dict | None = None
        self._results_count = results_count
        self._error_message = error_message

    def upload_click_conversions(
        self, *, customer_id, conversions, partial_failure
    ):
        self.called_with = {
            "customer_id": customer_id,
            "conversions": list(conversions),
            "partial_failure": partial_failure,
        }
        # Mark first N results as having conversion_action set (= success)
        results = []
        for i, c in enumerate(conversions):
            r = SimpleNamespace(
                conversion_action=c.conversion_action if i < self._results_count else "",
                gclid="",
                user_identifiers=[],
            )
            results.append(r)
        partial = SimpleNamespace(
            message=self._error_message, code=0
        ) if self._error_message else SimpleNamespace(message="", code=0)
        return SimpleNamespace(results=results, partial_failure_error=partial)


def _ec_client_with(*, upload, ads):
    return _FakeClient({
        "ConversionUploadService": upload,
        "GoogleAdsService": ads,
    })


class TestApplyUploadEcForLeads:
    def _make_changes(self, tmp_path):
        p = tmp_path / "ec.csv"
        p.write_text(
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "aaa,bbb,ccc,ddd,My Job,2026-03-01T12:00:00Z,500.00,USD\n"
            "eee,fff,ggg,hhh,My Job,2026-03-02T12:00:00Z,1500.00,USD\n"
        )
        return {"csv_path": str(p), "partial_failure": True}

    def test_full_success_with_user_identifiers(self, tmp_path):
        upload = _FakeClickUploadService(results_count=2)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Job", "customers/1/conversionActions/999",
                type_name="UPLOAD_CLICKS",
            )
        ])
        client = _ec_client_with(upload=upload, ads=ads)

        result = conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._make_changes(tmp_path)
        )

        assert result["uploaded_total"] == 2
        assert result["success_count"] == 2
        assert result["failure_count"] == 0
        sent = upload.called_with["conversions"]
        assert len(sent) == 2
        # Each row should have multiple user_identifiers (email, phone, name)
        assert len(sent[0].user_identifiers) == 3
        assert sent[0].user_identifiers[0].hashed_email == "aaa"
        assert sent[0].user_identifiers[1].hashed_phone_number == "bbb"
        assert sent[0].user_identifiers[2].address_info.hashed_first_name == "ccc"
        assert sent[0].conversion_action == "customers/1/conversionActions/999"
        assert sent[0].conversion_date_time == "2026-03-01 12:00:00+00:00"
        assert sent[0].conversion_value == 500.0

    def test_wrong_type_rejected(self, tmp_path):
        upload = _FakeClickUploadService()
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Job", "customers/1/conversionActions/999",
                type_name="UPLOAD_CALLS",  # Wrong — needs UPLOAD_CLICKS
            )
        ])
        client = _ec_client_with(upload=upload, ads=ads)

        with pytest.raises(ValueError) as exc:
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._make_changes(tmp_path)
            )
        assert "UPLOAD_CLICKS" in str(exc.value)


# ---------------------------------------------------------------------------
# Order ID — dedup-key support added so re-uploads are idempotent.
# Google's ClickConversion dedup is keyed on (conversion_action, order_id);
# without an order_id, every upload counts as a fresh conversion event.
# ---------------------------------------------------------------------------


class TestOrderIdParsing:
    def _write(self, tmp_path, content):
        p = tmp_path / "ec.csv"
        p.write_text(content)
        return str(p)

    def test_parser_reads_order_id_when_present(self, tmp_path):
        path = self._write(
            tmp_path,
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,Order ID\n"
            "aaaa,bbbb,cccc,dddd,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD,job-12345\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert errors == []
        assert rows[0]["order_id"] == "job-12345"

    def test_parser_defaults_order_id_to_empty_when_column_absent(
        self, tmp_path
    ):
        # Backwards compat: existing CSVs without Order ID still parse fine.
        path = self._write(
            tmp_path,
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "aaaa,bbbb,cccc,dddd,My Action,2026-03-01T12:00:00Z,250.00,USD\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert errors == []
        assert rows[0]["order_id"] == ""

    def test_parser_handles_missing_order_id_value_in_row(self, tmp_path):
        # Order ID column declared but specific row leaves it blank — parse
        # should accept the row and treat order_id as empty.
        path = self._write(
            tmp_path,
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,Order ID\n"
            "aaaa,bbbb,cccc,dddd,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD,\n"
            "eeee,ffff,gggg,hhhh,My Action,"
            "2026-03-02T12:00:00Z,300.00,USD,job-42\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert errors == []
        assert rows[0]["order_id"] == ""
        assert rows[1]["order_id"] == "job-42"


class TestDraftSurfacesOrderIdStats:
    def _write(self, tmp_path, *, with_order_id: bool):
        header = (
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency"
        )
        rows = [
            "aaa,bbb,ccc,ddd,Job Close,2026-03-01T12:00:00Z,500.00,USD",
            "eee,fff,ggg,hhh,Job Close,2026-03-02T12:00:00Z,1500.00,USD",
        ]
        if with_order_id:
            header += ",Order ID"
            rows = [f"{r},job-{i}" for i, r in enumerate(rows, start=1)]
        p = tmp_path / "ec.csv"
        p.write_text(header + "\n" + "\n".join(rows) + "\n")
        return str(p)

    def test_preview_includes_rows_with_order_id_count(self, config, tmp_path):
        path = self._write(tmp_path, with_order_id=True)
        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path,
        )
        c = result["changes"]
        assert c["rows_with_order_id"] == 2

    def test_preview_warns_when_no_order_id_present(self, config, tmp_path):
        path = self._write(tmp_path, with_order_id=False)
        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path,
        )
        c = result["changes"]
        assert c["rows_with_order_id"] == 0
        warnings = c["dedup_warnings"]
        assert len(warnings) == 1
        assert "double-count" in warnings[0]

    def test_preview_warns_on_partial_order_id_coverage(
        self, config, tmp_path
    ):
        # 1 of 2 rows has an Order ID — the other will double-count.
        p = tmp_path / "ec.csv"
        p.write_text(
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,Order ID\n"
            "aaa,bbb,ccc,ddd,Job Close,2026-03-01T12:00:00Z,500.00,USD,job-1\n"
            "eee,fff,ggg,hhh,Job Close,2026-03-02T12:00:00Z,1500.00,USD,\n"
        )
        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(p),
        )
        c = result["changes"]
        assert c["rows_with_order_id"] == 1
        assert any("1 of 2" in w for w in c["dedup_warnings"])

    def test_sample_rows_include_order_id_field(self, config, tmp_path):
        path = self._write(tmp_path, with_order_id=True)
        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path,
        )
        sample = result["changes"]["sample_rows"][0]
        assert sample["order_id"] == "job-1"


class TestApplySetsOrderIdOnProto:
    def _changes_with_order_ids(self, tmp_path):
        p = tmp_path / "ec.csv"
        p.write_text(
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency,Order ID\n"
            "aaa,bbb,ccc,ddd,My Job,2026-03-01T12:00:00Z,500.00,USD,job-1\n"
            "eee,fff,ggg,hhh,My Job,2026-03-02T12:00:00Z,1500.00,USD,job-2\n"
        )
        return {"csv_path": str(p), "partial_failure": True}

    def _changes_without_order_ids(self, tmp_path):
        p = tmp_path / "ec.csv"
        p.write_text(
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "aaa,bbb,ccc,ddd,My Job,2026-03-01T12:00:00Z,500.00,USD\n"
        )
        return {"csv_path": str(p), "partial_failure": True}

    def test_order_id_propagated_to_click_conversion(self, tmp_path):
        upload = _FakeClickUploadService(results_count=2)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Job", "customers/1/conversionActions/999",
                type_name="UPLOAD_CLICKS",
            )
        ])
        client = _ec_client_with(upload=upload, ads=ads)

        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes_with_order_ids(tmp_path)
        )

        sent = upload.called_with["conversions"]
        assert sent[0].order_id == "job-1"
        assert sent[1].order_id == "job-2"

    def test_order_id_left_unset_when_row_lacks_it(self, tmp_path):
        upload = _FakeClickUploadService(results_count=1)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Job", "customers/1/conversionActions/999",
                type_name="UPLOAD_CLICKS",
            )
        ])
        client = _ec_client_with(upload=upload, ads=ads)

        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes_without_order_ids(tmp_path)
        )

        sent = upload.called_with["conversions"]
        # proto default for unset string is "" — assert we did not populate it
        assert sent[0].order_id == ""


class TestApplySuccessCountIsRealistic:
    """The previous implementation treated echoed user_identifiers as a
    success signal, which silently overcounted because the API echoes them
    back even for rows that failed to match. We now key success off the
    response row's conversion_action being populated.
    """

    def _changes(self, tmp_path):
        p = tmp_path / "ec.csv"
        p.write_text(
            "Email,Phone Number,First Name,Last Name,Conversion Name,"
            "Conversion Time,Conversion Value,Conversion Currency\n"
            "aaa,bbb,ccc,ddd,My Job,2026-03-01T12:00:00Z,500.00,USD\n"
            "eee,fff,ggg,hhh,My Job,2026-03-02T12:00:00Z,1500.00,USD\n"
            "iii,jjj,kkk,lll,My Job,2026-03-03T12:00:00Z,250.00,USD\n"
        )
        return {"csv_path": str(p), "partial_failure": True}

    def _client(self, results_count: int):
        upload = _FakeClickUploadService(results_count=results_count)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Job", "customers/1/conversionActions/999",
                type_name="UPLOAD_CLICKS",
            )
        ])
        return _ec_client_with(upload=upload, ads=ads)

    def test_zero_matched_reports_zero_success(self, tmp_path):
        client = self._client(results_count=0)
        result = conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes(tmp_path)
        )
        assert result["uploaded_total"] == 3
        assert result["success_count"] == 0
        assert result["failure_count"] == 3

    def test_partial_match_reports_partial_counts(self, tmp_path):
        # 1 of 3 rows matched at the API — the other 2 came back empty.
        client = self._client(results_count=1)
        result = conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes(tmp_path)
        )
        assert result["uploaded_total"] == 3
        assert result["success_count"] == 1
        assert result["failure_count"] == 2

    def test_full_match_reports_full_success(self, tmp_path):
        client = self._client(results_count=3)
        result = conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes(tmp_path)
        )
        assert result["uploaded_total"] == 3
        assert result["success_count"] == 3
        assert result["failure_count"] == 0
