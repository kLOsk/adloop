"""Tests for conversion-action write tools (create / update / remove)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

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
    # v0.12 runtime: plans live in a swappable store, scoped per tenant.
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(require_dry_run=True),
    )


def _stored_plan(result: dict):
    """Fetch the stored ChangePlan for a draft result (current tenant)."""
    plan = preview_store.get_plan(result["plan_id"])
    assert plan is not None, "plan was not stored"
    return plan


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
        plan = _stored_plan(result)
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
        plan = _stored_plan(result)
        assert plan.changes["phone_call_duration_seconds"] == 90

    def test_default_value_with_fallback_flag_warns_not_flips(self, config):
        """Maintainer fix #2: a positive default_value paired with
        always_use_default_value=False is a LEGAL "tag value with fallback"
        config. The draft must emit a PREVIEW WARNING and leave the flag
        exactly as the caller set it — NOT silently force it to True (which
        would turn a fallback into an unconditional override)."""
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(default_value=400, always_use_default_value=False),
        )
        assert "error" not in result
        # The flag is NOT force-set.
        plan = _stored_plan(result)
        assert plan.changes["default_value"] == 400.0
        assert plan.changes["always_use_default_value"] is False
        # A warning surfaces the fallback-vs-override behavior.
        assert "warnings" in result
        assert any(
            "fallback" in w.lower() and "always_use_default_value" in w
            for w in result["warnings"]
        )

    def test_zero_default_value_no_warning(self, config):
        """No warning when default_value is 0 — callers may legitimately
        want to use snippet/import-provided values."""
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(default_value=0, always_use_default_value=False),
        )
        assert "error" not in result
        plan = _stored_plan(result)
        assert plan.changes["always_use_default_value"] is False
        assert "warnings" not in result

    def test_explicit_always_use_default_value_true_no_warning(self, config):
        """Explicit True is preserved and produces no fallback warning."""
        result = conversion_actions.draft_create_conversion_action(
            config,
            **self._ok_args(default_value=500, always_use_default_value=True),
        )
        assert "error" not in result
        plan = _stored_plan(result)
        assert plan.changes["always_use_default_value"] is True
        assert "warnings" not in result


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
        plan = _stored_plan(result)
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
        plan = _stored_plan(result)
        assert plan.changes["primary_for_goal"] is True

    def test_demote_to_secondary(self, config):
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            primary_for_goal=False,
        )
        plan = _stored_plan(result)
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
        plan = _stored_plan(result)
        assert plan.changes["phone_call_duration_seconds"] == 90

    def test_include_in_conversions_metric_is_mutable_on_update(self, config):
        """Unlike create (where it's IMMUTABLE), the update path accepts
        include_in_conversions_metric and passes it through."""
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            include_in_conversions_metric=False,
        )
        plan = _stored_plan(result)
        assert plan.changes["include_in_conversions_metric"] is False

    def test_fallback_flag_warns_on_update(self, config):
        """Fix #2 also applies on update: positive default_value with the
        flag explicitly False warns rather than overriding intent."""
        result = conversion_actions.draft_update_conversion_action(
            config,
            customer_id="1",
            conversion_action_id="6797442210",
            default_value=300,
            always_use_default_value=False,
        )
        plan = _stored_plan(result)
        assert plan.changes["always_use_default_value"] is False
        assert plan.changes["default_value"] == 300.0
        assert "warnings" in result
        assert any("fallback" in w.lower() for w in result["warnings"])


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
        plan = _stored_plan(result)
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
                "name": "Example Co - Call from Ad",
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

    def test_update_include_in_conversions_metric_fieldmask(self):
        ca_svc = _FakeConversionActionService(
            [_FakeResult("customers/1/conversionActions/6797442210")]
        )
        client = _FakeClient({"ConversionActionService": ca_svc})

        conversion_actions._apply_update_conversion_action(
            client,
            "1",
            {
                "conversion_action_id": "6797442210",
                "include_in_conversions_metric": False,
            },
        )

        op = ca_svc.operations[0]
        ca = op.update
        assert ca.include_in_conversions_metric is False
        assert list(op.update_mask.paths) == ["include_in_conversions_metric"]


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
    @classmethod
    def tools_by_name(cls):
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
        """_execute_plan must map the three CRUD operations to the module's
        apply handlers (same dispatch-dict style as the other Ads writes)."""
        import inspect
        src = inspect.getsource(write._execute_plan)
        assert '"create_conversion_action": _apply_create_conversion_action' in src
        assert '"update_conversion_action": _apply_update_conversion_action' in src
        assert '"remove_conversion_action": _apply_remove_conversion_action' in src


# ===========================================================================
# Offline conversion uploads — helpers, hashing, redaction, GAQL escape
# ===========================================================================
#
# Fixtures use only FAKE PII: emails user@example.com, phones +15555550142,
# names Test User, order ids ORD-001. Hash assertions are pinned to the
# SHA-256 hex of those known fakes.

import hashlib


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Known-fake canonical hashes (normalize THEN sha256).
_EMAIL_HASH = _sha("user@example.com")
_PHONE_HASH = _sha("+15555550142")
_FIRST_HASH = _sha("test")
_LAST_HASH = _sha("user")


class TestSha256Hashing:
    def test_email_normalized_then_hashed(self):
        # Trim + lowercase, then SHA-256.
        h = conversion_actions._sha256_hex(
            conversion_actions._normalize_email("  User@Example.COM ")
        )
        assert h == _EMAIL_HASH

    def test_phone_normalized_then_hashed(self):
        h = conversion_actions._sha256_hex(
            conversion_actions._normalize_phone_e164("+1 (555) 555-0142")
        )
        assert h == _PHONE_HASH

    def test_name_normalized_then_hashed(self):
        assert conversion_actions._sha256_hex(
            conversion_actions._normalize_name(" Test ")
        ) == _FIRST_HASH
        assert conversion_actions._sha256_hex(
            conversion_actions._normalize_name("USER")
        ) == _LAST_HASH

    def test_empty_hashes_to_empty(self):
        assert conversion_actions._sha256_hex("") == ""


class TestNormalizePhoneE164:
    def test_us_number_preserved(self):
        assert (
            conversion_actions._normalize_phone_e164("+1 (555) 555-0142")
            == "+15555550142"
        )

    def test_double_zero_international_prefix_becomes_plus(self):
        assert (
            conversion_actions._normalize_phone_e164("0044 20 7946 0018")
            == "+442079460018"
        )

    def test_strips_exactly_one_domestic_trunk_zero(self):
        # UK national format "020 …" -> the single leading 0 is dropped.
        assert (
            conversion_actions._normalize_phone_e164("020 7946 0018")
            == "2079460018"
        )

    def test_does_not_strip_multiple_leading_zeros_as_trunk(self):
        # A leading "00" is the international prefix, NOT two trunk zeros:
        # only the "00"->"+" rule fires, no extra zero-stripping.
        assert conversion_actions._normalize_phone_e164("0012025550000") == (
            "+12025550000"
        )

    def test_italy_leading_zero_preserved_when_plus_present(self):
        # Italian fixed-line numbers KEEP the leading 0 in E.164.
        assert (
            conversion_actions._normalize_phone_e164("+39 06 6982 1234")
            == "+390669821234"
        )

    def test_empty(self):
        assert conversion_actions._normalize_phone_e164("") == ""
        assert conversion_actions._normalize_phone_e164("   ") == ""


class TestGaqlEscape:
    def test_apostrophe_escaped_with_backslash(self):
        # GAQL uses backslash escaping, NOT SQL-style doubled quotes.
        assert conversion_actions._gaql_escape("O'Brien Lead") == (
            "O\\'Brien Lead"
        )

    def test_backslash_escaped_first(self):
        assert conversion_actions._gaql_escape("a\\b") == "a\\\\b"

    def test_used_in_resolve_query(self):
        # The resolver must interpolate the escaped name into the WHERE.
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "O'Brien Lead", "customers/1/conversionActions/1",
                type_name="UPLOAD_CALLS",
            )
        ])
        conversion_actions._resolve_conversion_action_ids(
            _client_with(upload_service=_FakeUploadService(), ads_service=ads),
            "1",
            ["O'Brien Lead"],
        )
        assert "O\\'Brien Lead" in ads.last_query
        # Must NOT contain the SQL-style doubled-quote form.
        assert "O''Brien" not in ads.last_query


class TestRedactCallerId:
    def test_masks_middle(self):
        assert conversion_actions._redact_caller_id("+15555550142") == (
            "+155***0142"
        )

    def test_short_number_fully_masked(self):
        assert conversion_actions._redact_caller_id("12345") == "***"

    def test_empty(self):
        assert conversion_actions._redact_caller_id("") == ""


class TestConsentParam:
    def test_none_returns_none(self):
        assert conversion_actions._consent_from_param(None) is None
        assert conversion_actions._consent_from_param({}) is None

    def test_defaults_missing_keys_to_unspecified(self):
        out = conversion_actions._consent_from_param(
            {"ad_user_data": "GRANTED"}
        )
        assert out == {
            "ad_user_data": "GRANTED",
            "ad_personalization": "UNSPECIFIED",
        }

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            conversion_actions._consent_from_param({"ad_user_data": "YES"})


# ---------------------------------------------------------------------------
# Fakes for the upload paths
# ---------------------------------------------------------------------------


class _FakeUploadService:
    def __init__(self, results_count: int = 0, error_message: str = ""):
        self.called_with: dict | None = None
        self._results_count = results_count
        self._error_message = error_message

    def upload_call_conversions(
        self, *, customer_id, conversions, partial_failure
    ):
        self.called_with = {
            "customer_id": customer_id,
            "conversions": list(conversions),
            "partial_failure": partial_failure,
        }
        # Mark the first N results as accepted (conversion_action populated).
        results = []
        for i, c in enumerate(conversions):
            results.append(SimpleNamespace(
                conversion_action=(
                    c.conversion_action if i < self._results_count else ""
                ),
                caller_id=c.caller_id,  # API echoes this back even on failure
            ))
        partial = (
            SimpleNamespace(message=self._error_message, code=0)
            if self._error_message
            else SimpleNamespace(message="", code=0)
        )
        return SimpleNamespace(results=results, partial_failure_error=partial)


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
        results = []
        for i, c in enumerate(conversions):
            results.append(SimpleNamespace(
                conversion_action=(
                    c.conversion_action if i < self._results_count else ""
                ),
                gclid="",
                # API echoes user_identifiers back even for FAILED rows.
                user_identifiers=list(c.user_identifiers),
            ))
        partial = (
            SimpleNamespace(message=self._error_message, code=0)
            if self._error_message
            else SimpleNamespace(message="", code=0)
        )
        return SimpleNamespace(results=results, partial_failure_error=partial)


class _FakeSearchRow:
    def __init__(
        self, name: str, resource_name: str, type_name: str = "UPLOAD_CALLS"
    ):
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
    return _FakeClient({
        "ConversionUploadService": upload_service,
        "GoogleAdsService": ads_service,
    })


def _ec_client_with(*, upload, ads):
    return _FakeClient({
        "ConversionUploadService": upload,
        "GoogleAdsService": ads,
    })


# ---------------------------------------------------------------------------
# Call conversions — parse, draft (redaction/consent), apply-from-rows
# ---------------------------------------------------------------------------

_CALL_HEADER = (
    "Caller's Phone Number,Call Start Time,Conversion Name,"
    "Conversion Time,Conversion Value,Conversion Currency\n"
)


class TestParseCallConversionCsv:
    def test_missing_file(self, tmp_path):
        rows, errors = conversion_actions._parse_call_conversion_csv(
            str(tmp_path / "nope.csv")
        )
        assert rows == []
        assert any("not found" in e for e in errors)

    def test_skips_parameters_row_and_normalizes(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            "Parameters:TimeZone=America/Los_Angeles,,,,,\n"
            + _CALL_HEADER
            + "+15555550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,250.00,usd\n"
        )
        rows, errors = conversion_actions._parse_call_conversion_csv(str(p))
        assert errors == []
        assert len(rows) == 1
        assert rows[0]["caller_id"] == "+15555550142"
        assert rows[0]["call_start_time"] == "2026-03-01 12:00:00+00:00"
        assert rows[0]["currency_code"] == "USD"

    def test_missing_required_column(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            "Caller's Phone Number,Conversion Name,Conversion Time,"
            "Conversion Value,Conversion Currency\n"
            "+15555550142,X,2026-03-01T13:00:00Z,10,USD\n"
        )
        rows, errors = conversion_actions._parse_call_conversion_csv(str(p))
        assert rows == []
        assert any("Call Start Time" in e for e in errors)


class TestDraftUploadCallConversions:
    def _write(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            _CALL_HEADER
            + "+15555550142,2026-03-01T12:00:00Z,A,"
            "2026-03-01T13:00:00Z,250.00,USD\n"
            "+15555550143,2026-03-02T12:00:00Z,A,"
            "2026-03-02T13:00:00Z,500.00,USD\n"
            "+15555550144,2026-03-03T12:00:00Z,B,"
            "2026-03-03T13:00:00Z,75.00,USD\n"
        )
        return str(p)

    def test_missing_csv_returns_error(self, config, tmp_path):
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890",
            csv_path=str(tmp_path / "missing.csv"),
        )
        assert "error" in result

    def test_happy_path_preview(self, config, tmp_path):
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
        )
        assert result["operation"] == "upload_call_conversions"
        assert result["entity_type"] == "call_conversion_batch"
        c = result["changes"]
        assert c["row_count"] == 3
        assert c["total_value"] == 825.00
        assert c["distinct_conversion_actions"] == ["A", "B"]

    def test_preview_never_contains_csv_path(self, config, tmp_path):
        # Apply reads from plan rows, not the CSV — path is not persisted.
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
        )
        assert "csv_path" not in result["changes"]

    def test_sample_rows_redact_caller_id(self, config, tmp_path):
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
        )
        for s in result["changes"]["sample_rows"]:
            assert "***" in s["caller_id"]
            assert s["caller_id"] != "+15555550142"

    def test_plan_rows_keep_raw_caller_id(self, config, tmp_path):
        # Rows in the plan MUST keep the raw caller_id (apply needs it).
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
        )
        plan = _stored_plan(result)
        assert plan.changes["rows"][0]["caller_id"] == "+15555550142"

    def test_consent_stored_in_plan(self, config, tmp_path):
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
            consent={"ad_user_data": "GRANTED",
                     "ad_personalization": "DENIED"},
        )
        plan = _stored_plan(result)
        assert plan.changes["consent"] == {
            "ad_user_data": "GRANTED", "ad_personalization": "DENIED",
        }

    def test_safety_blocked_operation(self, tmp_path):
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                blocked_operations=["upload_call_conversions"]
            ),
        )
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            cfg, customer_id="1234567890", csv_path=path,
        )
        assert "error" in result


class TestApplyUploadCallConversions:
    def _changes(self, consent=None):
        rows = [
            {
                "caller_id": "+15555550142",
                "call_start_time": "2026-03-01 12:00:00+00:00",
                "conversion_name": "My Action",
                "conversion_time": "2026-03-01 13:00:00+00:00",
                "conversion_value": 250.0,
                "currency_code": "USD",
            },
            {
                "caller_id": "+15555550143",
                "call_start_time": "2026-03-02 12:00:00+00:00",
                "conversion_name": "My Action",
                "conversion_time": "2026-03-02 13:00:00+00:00",
                "conversion_value": 500.0,
                "currency_code": "USD",
            },
        ]
        changes = {"rows": rows, "partial_failure": True}
        if consent is not None:
            changes["consent"] = consent
        return changes

    def test_builds_protos_from_rows_no_csv(self, tmp_path):
        upload = _FakeUploadService(results_count=2)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)

        result = conversion_actions._apply_upload_call_conversions(
            client, "1", self._changes()
        )
        assert result["uploaded_total"] == 2
        assert result["success_count"] == 2
        assert result["failure_count"] == 0
        sent = upload.called_with["conversions"]
        assert sent[0].caller_id == "+15555550142"
        assert sent[0].conversion_action == (
            "customers/1/conversionActions/777"
        )
        assert sent[0].conversion_value == 250.0
        assert sent[0].call_start_date_time == "2026-03-01 12:00:00+00:00"

    def test_success_count_keys_off_conversion_action_not_caller_id(
        self, tmp_path
    ):
        # Only 1 of 2 rows accepted — caller_id is echoed back on BOTH, so a
        # caller_id-based count would wrongly report 2. We must report 1.
        upload = _FakeUploadService(results_count=1)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)
        result = conversion_actions._apply_upload_call_conversions(
            client, "1", self._changes()
        )
        assert result["success_count"] == 1
        assert result["failure_count"] == 1

    def test_zero_matched_reports_zero_success(self, tmp_path):
        upload = _FakeUploadService(results_count=0)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)
        result = conversion_actions._apply_upload_call_conversions(
            client, "1", self._changes()
        )
        assert result["success_count"] == 0
        assert result["failure_count"] == 2

    def test_consent_applied_to_protos(self, tmp_path):
        upload = _FakeUploadService(results_count=2)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)
        conversion_actions._apply_upload_call_conversions(
            client, "1",
            self._changes(consent={
                "ad_user_data": "GRANTED",
                "ad_personalization": "DENIED",
            }),
        )
        sent = upload.called_with["conversions"]
        enums = client.enums.ConsentStatusEnum
        assert sent[0].consent.ad_user_data == enums.GRANTED
        assert sent[0].consent.ad_personalization == enums.DENIED

    def test_wrong_type_raises(self, tmp_path):
        upload = _FakeUploadService()
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Action", "customers/1/conversionActions/777",
                type_name="UPLOAD_CLICKS",
            )
        ])
        client = _client_with(upload_service=upload, ads_service=ads)
        with pytest.raises(ValueError) as exc:
            conversion_actions._apply_upload_call_conversions(
                client, "1", self._changes()
            )
        assert "UPLOAD_CALLS" in str(exc.value)

    def test_action_not_found_raises(self, tmp_path):
        upload = _FakeUploadService()
        ads = _FakeGoogleAdsService([])
        client = _client_with(upload_service=upload, ads_service=ads)
        with pytest.raises(ValueError) as exc:
            conversion_actions._apply_upload_call_conversions(
                client, "1", self._changes()
            )
        assert "not found" in str(exc.value)

    def test_empty_rows_returns_error(self, tmp_path):
        upload = _FakeUploadService()
        ads = _FakeGoogleAdsService([])
        client = _client_with(upload_service=upload, ads_service=ads)
        result = conversion_actions._apply_upload_call_conversions(
            client, "1", {"rows": [], "partial_failure": True}
        )
        assert "error" in result
        assert upload.called_with is None


# ---------------------------------------------------------------------------
# EC for Leads — parse+hash, draft, apply-from-rows, order_id, success_count
# ---------------------------------------------------------------------------

_EC_HEADER = (
    "Email,Phone Number,First Name,Last Name,Conversion Name,"
    "Conversion Time,Conversion Value,Conversion Currency"
)


class TestParseEcForLeadsCsvHashesPii:
    def _write(self, tmp_path, content):
        p = tmp_path / "ec.csv"
        p.write_text(content)
        return str(p)

    def test_raw_pii_is_normalized_then_hashed(self, tmp_path):
        # Raw fakes in the CSV; the parser must return hashes only.
        path = self._write(
            tmp_path,
            _EC_HEADER + "\n"
            "User@Example.com,+1 (555) 555-0142,Test,User,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert errors == []
        r = rows[0]
        assert r["email_sha256"] == _EMAIL_HASH
        assert r["phone_sha256"] == _PHONE_HASH
        assert r["first_name_sha256"] == _FIRST_HASH
        assert r["last_name_sha256"] == _LAST_HASH
        # No raw-PII keys leak out of the parser.
        assert "email" not in r and "phone" not in r
        assert "first_name" not in r and "last_name" not in r

    def test_blank_pii_hashes_to_empty(self, tmp_path):
        path = self._write(
            tmp_path,
            _EC_HEADER + "\n"
            ",+15555550142,,,My Action,2026-03-01T12:00:00Z,200.00,USD\n",
        )
        rows, _ = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows[0]["email_sha256"] == ""
        assert rows[0]["first_name_sha256"] == ""
        assert rows[0]["phone_sha256"] == _PHONE_HASH

    def test_order_id_optional_column(self, tmp_path):
        path = self._write(
            tmp_path,
            _EC_HEADER + ",Order ID\n"
            "user@example.com,+15555550142,Test,User,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD,ORD-001\n",
        )
        rows, _ = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows[0]["order_id"] == "ORD-001"

    def test_order_id_defaults_empty_when_absent(self, tmp_path):
        path = self._write(
            tmp_path,
            _EC_HEADER + "\n"
            "user@example.com,+15555550142,Test,User,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD\n",
        )
        rows, _ = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows[0]["order_id"] == ""

    def test_missing_required_column(self, tmp_path):
        path = self._write(
            tmp_path,
            "Email,Phone Number,Conversion Name,Conversion Time,"
            "Conversion Value,Conversion Currency\n"
            "user@example.com,+15555550142,X,2026-03-01T12:00:00Z,10,USD\n",
        )
        rows, errors = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows == []
        assert any("First Name" in e or "Last Name" in e for e in errors)


class TestDraftUploadEcForLeads:
    def _write(self, tmp_path, *, order_id=False):
        header = _EC_HEADER + (",Order ID" if order_id else "")
        r1 = ("user@example.com,+15555550142,Test,User,Job Close,"
              "2026-03-01T12:00:00Z,500.00,USD")
        r2 = (",+15555550143,,,Job Close,"
              "2026-03-02T12:00:00Z,1500.00,USD")
        if order_id:
            r1 += ",ORD-001"
            r2 += ",ORD-002"
        p = tmp_path / "ec.csv"
        p.write_text(header + "\n" + r1 + "\n" + r2 + "\n")
        return str(p)

    def test_happy_path_preview(self, config, tmp_path):
        path = self._write(tmp_path)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
            )
        )
        assert result["operation"] == "upload_enhanced_conversions_for_leads"
        c = result["changes"]
        assert c["row_count"] == 2
        assert c["total_value"] == 2000.00
        assert c["rows_with_email"] == 1
        assert c["rows_with_phone"] == 2
        assert c["distinct_conversion_actions"] == ["Job Close"]

    def test_no_raw_pii_in_plan_changes(self, config, tmp_path):
        # The stored plan must contain ONLY hashes for PII, never raw values.
        path = self._write(tmp_path)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
            )
        )
        plan = _stored_plan(result)
        blob = repr(plan.changes)
        assert "user@example.com" not in blob
        assert "+15555550142" not in blob
        assert "Test" not in blob and "User" not in blob
        # But the hashes ARE present in the frozen rows.
        assert plan.changes["rows"][0]["email_sha256"] == _EMAIL_HASH

    def test_sample_rows_truncate_hashes(self, config, tmp_path):
        path = self._write(tmp_path)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
            )
        )
        assert "..." in result["changes"]["sample_rows"][0]["email_sha256"]

    def test_dedup_warning_when_no_order_id(self, config, tmp_path):
        path = self._write(tmp_path, order_id=False)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
            )
        )
        c = result["changes"]
        assert c["rows_with_order_id"] == 0
        assert len(c["dedup_warnings"]) == 1
        assert "double-count" in c["dedup_warnings"][0]

    def test_dedup_no_warning_full_coverage(self, config, tmp_path):
        path = self._write(tmp_path, order_id=True)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
            )
        )
        c = result["changes"]
        assert c["rows_with_order_id"] == 2
        assert c["dedup_warnings"] == []

    def test_dedup_partial_coverage_warns(self, config, tmp_path):
        p = tmp_path / "ec.csv"
        p.write_text(
            _EC_HEADER + ",Order ID\n"
            "user@example.com,+15555550142,Test,User,Job Close,"
            "2026-03-01T12:00:00Z,500.00,USD,ORD-001\n"
            "user2@example.com,+15555550143,Test,User,Job Close,"
            "2026-03-02T12:00:00Z,1500.00,USD,\n"
        )
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=str(p),
            )
        )
        c = result["changes"]
        assert c["rows_with_order_id"] == 1
        assert any("1 of 2" in w for w in c["dedup_warnings"])

    def test_consent_stored(self, config, tmp_path):
        path = self._write(tmp_path)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
                consent={"ad_user_data": "DENIED",
                         "ad_personalization": "GRANTED"},
            )
        )
        plan = _stored_plan(result)
        assert plan.changes["consent"] == {
            "ad_user_data": "DENIED", "ad_personalization": "GRANTED",
        }


class TestApplyUploadEcForLeads:
    def _changes(self, *, order_id=False, consent=None):
        rows = [
            {
                "email_sha256": _EMAIL_HASH,
                "phone_sha256": _PHONE_HASH,
                "first_name_sha256": _FIRST_HASH,
                "last_name_sha256": _LAST_HASH,
                "conversion_name": "My Job",
                "conversion_time": "2026-03-01 12:00:00+00:00",
                "conversion_value": 500.0,
                "currency_code": "USD",
                "order_id": "ORD-001" if order_id else "",
            },
            {
                "email_sha256": "",
                "phone_sha256": _PHONE_HASH,
                "first_name_sha256": "",
                "last_name_sha256": "",
                "conversion_name": "My Job",
                "conversion_time": "2026-03-02 12:00:00+00:00",
                "conversion_value": 1500.0,
                "currency_code": "USD",
                "order_id": "ORD-002" if order_id else "",
            },
        ]
        changes = {"rows": rows, "partial_failure": True}
        if consent is not None:
            changes["consent"] = consent
        return changes

    def _client(self, results_count, type_name="UPLOAD_CLICKS"):
        upload = _FakeClickUploadService(results_count=results_count)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "My Job", "customers/1/conversionActions/999",
                type_name=type_name,
            )
        ])
        return _ec_client_with(upload=upload, ads=ads), upload

    def test_builds_user_identifiers_from_hashes_no_csv(self, tmp_path):
        client, upload = self._client(results_count=2)
        result = (
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._changes()
            )
        )
        assert result["uploaded_total"] == 2
        assert result["success_count"] == 2
        sent = upload.called_with["conversions"]
        # Row 1: email + phone + name = 3 identifiers.
        assert len(sent[0].user_identifiers) == 3
        assert sent[0].user_identifiers[0].hashed_email == _EMAIL_HASH
        assert sent[0].user_identifiers[1].hashed_phone_number == _PHONE_HASH
        assert (
            sent[0].user_identifiers[2].address_info.hashed_first_name
            == _FIRST_HASH
        )
        # Row 2: phone only = 1 identifier.
        assert len(sent[1].user_identifiers) == 1

    def test_success_count_keys_off_conversion_action(self, tmp_path):
        # user_identifiers are echoed back on ALL rows; only 1 matched.
        client, _ = self._client(results_count=1)
        result = (
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._changes()
            )
        )
        assert result["success_count"] == 1
        assert result["failure_count"] == 1

    def test_zero_matched_reports_zero_success(self, tmp_path):
        client, _ = self._client(results_count=0)
        result = (
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._changes()
            )
        )
        assert result["success_count"] == 0
        assert result["failure_count"] == 2

    def test_order_id_propagated_to_proto(self, tmp_path):
        client, upload = self._client(results_count=2)
        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes(order_id=True)
        )
        sent = upload.called_with["conversions"]
        assert sent[0].order_id == "ORD-001"
        assert sent[1].order_id == "ORD-002"

    def test_order_id_unset_when_absent(self, tmp_path):
        client, upload = self._client(results_count=2)
        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1", self._changes(order_id=False)
        )
        sent = upload.called_with["conversions"]
        assert sent[0].order_id == ""

    def test_consent_applied(self, tmp_path):
        client, upload = self._client(results_count=2)
        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client, "1",
            self._changes(consent={
                "ad_user_data": "GRANTED",
                "ad_personalization": "DENIED",
            }),
        )
        sent = upload.called_with["conversions"]
        enums = client.enums.ConsentStatusEnum
        assert sent[0].consent.ad_user_data == enums.GRANTED
        assert sent[0].consent.ad_personalization == enums.DENIED

    def test_wrong_type_rejected(self, tmp_path):
        client, _ = self._client(results_count=0, type_name="UPLOAD_CALLS")
        with pytest.raises(ValueError) as exc:
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._changes()
            )
        assert "UPLOAD_CLICKS" in str(exc.value)

    def test_gaql_escape_used_for_ec_resolver(self, tmp_path):
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "O'Brien Lead", "customers/1/conversionActions/9",
                type_name="UPLOAD_CLICKS",
            )
        ])
        conversion_actions._resolve_upload_clicks_action(
            _ec_client_with(upload=_FakeClickUploadService(), ads=ads),
            "1", ["O'Brien Lead"],
        )
        assert "O\\'Brien Lead" in ads.last_query
        assert "O''Brien" not in ads.last_query


# ---------------------------------------------------------------------------
# Audit-log redaction (write._redact_changes_for_audit)
# ---------------------------------------------------------------------------


class TestAuditRedaction:
    def test_call_conversion_caller_id_redacted_in_audit(self):
        changes = {
            "row_count": 1,
            "rows": [{
                "caller_id": "+15555550142",
                "call_start_time": "2026-03-01 12:00:00+00:00",
                "conversion_name": "A",
                "conversion_time": "2026-03-01 13:00:00+00:00",
                "conversion_value": 250.0,
                "currency_code": "USD",
            }],
        }
        redacted = write._redact_changes_for_audit(
            "upload_call_conversions", changes
        )
        blob = repr(redacted)
        assert "+15555550142" not in blob
        assert "+155***0142" in blob
        # Original object is NOT mutated.
        assert changes["rows"][0]["caller_id"] == "+15555550142"

    def test_ec_rows_dropped_from_audit(self):
        changes = {
            "row_count": 1,
            "rows_with_email": 1,
            "rows": [{
                "email_sha256": _EMAIL_HASH,
                "phone_sha256": _PHONE_HASH,
                "first_name_sha256": _FIRST_HASH,
                "last_name_sha256": _LAST_HASH,
                "conversion_name": "Job",
                "conversion_time": "2026-03-01 12:00:00+00:00",
                "conversion_value": 500.0,
                "currency_code": "USD",
                "order_id": "",
            }],
        }
        redacted = write._redact_changes_for_audit(
            "upload_enhanced_conversions_for_leads", changes
        )
        assert "rows" not in redacted
        assert "rows_redacted" in redacted
        # Summary counters survive.
        assert redacted["rows_with_email"] == 1
        # Original object is NOT mutated.
        assert "rows" in changes

    def test_non_upload_ops_pass_through(self):
        changes = {"campaign_name": "X"}
        assert (
            write._redact_changes_for_audit("create_campaign", changes)
            is changes
        )

    def test_refused_two_phase_does_not_leak_caller_id(self, tmp_path):
        """Two-phase apply (upstream commit 4e3d314) added a log_mutation site
        that logs the refusal with result='refused_two_phase'. That site MUST
        also run changes through _redact_changes_for_audit — otherwise a
        refused call-conversion upload writes the raw caller_id to the audit
        log. This exercises the full confirm_and_apply refusal path against the
        real file audit sink and asserts the raw number never lands on disk.
        """
        from adloop.safety import audit
        from adloop.safety import preview as preview_store

        log_path = tmp_path / "audit.log"
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False,
                two_phase_apply=True,
                log_file=str(log_path),
            ),
        )
        plan = preview_store.ChangePlan(
            operation="upload_call_conversions",
            entity_type="conversion_upload",
            entity_id="",
            customer_id="1234567890",
            changes={
                "row_count": 1,
                "rows": [{
                    "caller_id": "+15555550142",
                    "call_start_time": "2026-03-01 12:00:00+00:00",
                    "conversion_name": "A",
                    "conversion_time": "2026-03-01 13:00:00+00:00",
                    "conversion_value": 250.0,
                    "currency_code": "USD",
                }],
            },
        )
        preview_store.store_plan(plan)

        prev_sink = audit.get_audit_sink()
        audit.set_audit_sink(audit.FileAuditSink())
        try:
            resp = write.confirm_and_apply(
                cfg, plan_id=plan.plan_id, dry_run=False
            )
        finally:
            audit.set_audit_sink(prev_sink)

        # No dry run happened yet, so the real upload is refused.
        assert resp["status"] == "DRY_RUN_REQUIRED"
        logged = log_path.read_text()
        assert '"result": "refused_two_phase"' in logged
        # The raw caller_id must NOT appear anywhere in the audit log.
        assert "+15555550142" not in logged
        # The redacted form is what got logged instead.
        assert "+155***0142" in logged


# ---------------------------------------------------------------------------
# Upload tool registration + dispatch wiring
# ---------------------------------------------------------------------------


class TestUploadToolRegistration:
    @pytest.fixture(scope="class")
    @classmethod
    def tools_by_name(cls):
        import asyncio
        from adloop.server import mcp

        async def _list():
            return await mcp.list_tools()

        tools = asyncio.run(_list())
        return {t.name: t for t in tools}

    def test_upload_tools_registered(self, tools_by_name):
        assert "draft_upload_call_conversions" in tools_by_name
        assert (
            "draft_upload_enhanced_conversions_for_leads" in tools_by_name
        )

    def test_call_upload_requires_csv_path(self, tools_by_name):
        required = (
            tools_by_name["draft_upload_call_conversions"]
            .parameters.get("required", [])
        )
        assert "csv_path" in required

    def test_ec_upload_requires_csv_path(self, tools_by_name):
        required = (
            tools_by_name["draft_upload_enhanced_conversions_for_leads"]
            .parameters.get("required", [])
        )
        assert "csv_path" in required

    def test_upload_dispatch_wired(self):
        import inspect
        src = inspect.getsource(write._execute_plan)
        assert (
            '"upload_call_conversions": _apply_upload_call_conversions'
            in src
        )
        assert "upload_enhanced_conversions_for_leads" in src
