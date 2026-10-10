"""Tests for conversion-action write tools (create / update / remove)."""
from __future__ import annotations

import json
import re

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
        """The Ads dispatch must map the three CRUD operations to the module's
        apply handlers (same dispatch-dict style as the other Ads writes)."""
        import inspect
        src = inspect.getsource(write._dispatch_ads_plan)
        assert '"create_conversion_action": _apply_create_conversion_action' in src
        assert '"update_conversion_action": _apply_update_conversion_action' in src
        assert '"remove_conversion_action": _apply_remove_conversion_action' in src


# ===========================================================================
# Offline conversion uploads — helpers, hashing, redaction, GAQL escape
# ===========================================================================
#
# Fixtures use only FAKE PII: emails user@example.com, phones +14155550142,
# names Test User, order ids ORD-001. Hash assertions are pinned to the
# SHA-256 hex of those known fakes.

import hashlib


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Known-fake canonical hashes (normalize THEN sha256).
_EMAIL_HASH = _sha("user@example.com")
_PHONE_HASH = _sha("+14155550142")
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
            conversion_actions._normalize_phone_e164("+1 415 555 0142")
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
    """libphonenumber semantics via `phonenumbers` — not a hand-rolled dialect."""

    def test_german_trunk_marker_is_handled(self):
        # The "(0)" is standard in German exports; trusting anything that
        # starts with "+" produced +49089123456, a number that never matches.
        assert (
            conversion_actions._normalize_phone_e164("+49 (0)89 123456")
            == "+4989123456"
        )

    def test_a_national_format_needs_a_default_region(self):
        assert conversion_actions._normalize_phone_e164("0151 12345678") == ""
        assert (
            conversion_actions._normalize_phone_e164("0151 12345678", "DE")
            == "+4915112345678"
        )
        assert (
            conversion_actions._normalize_phone_e164("089 123456", "DE")
            == "+4989123456"
        )

    def test_international_prefix_and_italian_leading_zero(self):
        # A leading "00" is an international access prefix, but which one it is
        # depends on the region it was dialled from — hence the region here.
        assert conversion_actions._normalize_phone_e164("0049 89 123456") == ""
        assert (
            conversion_actions._normalize_phone_e164("0049 89 123456", "DE")
            == "+4989123456"
        )
        # Italian fixed-line numbers keep their leading zero in E.164.
        assert (
            conversion_actions._normalize_phone_e164("+39 06 6982 1234")
            == "+390669821234"
        )

    def test_an_extension_is_not_appended_to_the_number(self):
        assert (
            conversion_actions._normalize_phone_e164("+1 415 555 0132 ext 12")
            == "+14155550132"
        )

    def test_unusable_numbers_return_empty(self):
        for raw in ("", "   ", "not a number", "+1 555 0100", "+49 12345"):
            assert conversion_actions._normalize_phone_e164(raw) == "", raw

    def test_a_region_name_instead_of_a_code_is_unusable(self):
        # "Germany" is not a region code; the draft refuses that value outright.
        assert conversion_actions._normalize_phone_e164("0151 12345678", "Germany") == ""


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
        conversion_actions._resolve_upload_action(
            _client_with(upload_service=_FakeUploadService(), ads_service=ads),
            "1",
            ["O'Brien Lead"],
            expected_type="UPLOAD_CALLS",
        )
        assert "O\\'Brien Lead" in ads.last_query
        # Must NOT contain the SQL-style doubled-quote form.
        assert "O''Brien" not in ads.last_query


class TestRedactCallerId:
    def test_masks_middle(self):
        assert conversion_actions._redact_caller_id("+14155550142") == "+1***42"

    def test_keeps_the_whole_dialling_code(self):
        """+49 is the country code; a fixed slice would show '+491' and hide
        the last digits behind it."""
        assert conversion_actions._redact_caller_id("+4915112345678") == "+49***78"

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

    def test_an_unknown_key_raises_instead_of_being_ignored(self):
        """`adPersonalization` would leave the field at UNSPECIFIED unnoticed."""
        with pytest.raises(ValueError, match="adPersonalization"):
            conversion_actions._consent_from_param({"adPersonalization": "GRANTED"})

    def test_the_error_text_lists_every_accepted_value(self):
        with pytest.raises(ValueError) as info:
            conversion_actions._consent_from_param({"ad_user_data": "YES"})

        text = str(info.value)
        for value in ("GRANTED", "DENIED", "UNSPECIFIED", "UNKNOWN"):
            assert value in text

    def test_unknown_is_a_legal_value(self):
        # The ConsentStatus enum has UNKNOWN next to UNSPECIFIED.
        normalized = conversion_actions._consent_from_param(
            {"ad_user_data": "UNKNOWN"}
        )
        assert normalized["ad_user_data"] == "UNKNOWN"

    def test_a_consent_that_is_not_an_object_is_refused(self):
        with pytest.raises(ValueError, match="consent must be an object"):
            conversion_actions._consent_from_param("GRANTED")

    def test_both_drafts_refuse_a_misspelled_key(
        self, config, tmp_path, monkeypatch
    ):
        cases = (
            (
                conversion_actions.draft_upload_call_conversions,
                "UPLOAD_CALLS",
                _CALL_HEADER,
                "+14155550142,2026-03-01T12:00:00Z,My Action,"
                "2026-03-01T13:00:00Z,10,USD\n",
            ),
            (
                conversion_actions.draft_upload_enhanced_conversions_for_leads,
                "UPLOAD_CLICKS",
                _EC_HEADER,
                "user@example.com,+14155550142,Anna,Lena,My Action,"
                "2026-03-01T12:00:00Z,10,USD\n",
            ),
        )
        for draft, type_name, header, row in cases:
            _patch_drafts_client(monkeypatch, type_name)
            path = tmp_path / f"{type_name}.csv"
            path.write_text(header.rstrip("\n") + "\n" + row)

            result = draft(
                config,
                customer_id="1234567890",
                csv_path=str(path),
                consent={"adPersonalization": "GRANTED"},
            )

            assert "adPersonalization" in result["error"], type_name


# ---------------------------------------------------------------------------
# Fakes for the upload paths
# ---------------------------------------------------------------------------


class _FakeUploadService:
    def __init__(
        self,
        results_count: int = 0,
        error_message: str = "",
        fail_on_call: int = 0,
        fail_exception: Exception | None = None,
        results_error: Exception | None = None,
    ):
        self.called_with: dict | None = None
        self.calls: list[dict] = []
        self._results_count = results_count
        self._error_message = error_message
        self._fail_on_call = fail_on_call
        self._fail_exception = fail_exception
        self._results_error = results_error
        # Optional real proto, set by tests that need per-index detail.
        self.partial_failure = None

    def upload_call_conversions(
        self, request=None, *, customer_id=None, conversions=None, partial_failure=None
    ):
        # Two call shapes: the applier passes kwargs, the validate-only wrapper
        # passes a built request. Both are recorded the same way.
        validate_only = False
        if request is not None:
            customer_id = request.customer_id
            conversions = list(request.conversions)
            partial_failure = request.partial_failure
            validate_only = bool(getattr(request, "validate_only", False))
        self.called_with = {
            "customer_id": customer_id,
            "conversions": list(conversions or []),
            "partial_failure": partial_failure,
            "validate_only": validate_only,
        }
        self.calls.append(dict(self.called_with))
        if self._fail_on_call and len(self.calls) == self._fail_on_call:
            raise self._fail_exception or RuntimeError("batch rejected by Google")
        # Mark the first N results as accepted (conversion_action populated).
        results = []
        for i, c in enumerate(conversions):
            results.append(SimpleNamespace(
                conversion_action=(
                    c.conversion_action if i < self._results_count else ""
                ),
                caller_id=c.caller_id,  # API echoes this back even on failure
            ))
        if self._results_error is not None:
            return _UnreadableResponse(self._results_error)
        if self.partial_failure is not None:
            return SimpleNamespace(results=results, partial_failure_error=self.partial_failure)
        partial = (
            SimpleNamespace(message=self._error_message, code=0)
            if self._error_message
            else SimpleNamespace(message="", code=0)
        )
        return SimpleNamespace(results=results, partial_failure_error=partial)


class _FakeClickUploadService:
    def __init__(
        self, results_count: int = 0, error_message: str = "", fail_on_call: int = 0
    ):
        self.called_with: dict | None = None
        self.calls: list[dict] = []
        self._results_count = results_count
        self._error_message = error_message
        self._fail_on_call = fail_on_call

    def upload_click_conversions(
        self, request=None, *, customer_id=None, conversions=None, partial_failure=None
    ):
        validate_only = False
        if request is not None:
            customer_id = request.customer_id
            conversions = list(request.conversions)
            partial_failure = request.partial_failure
            validate_only = bool(getattr(request, "validate_only", False))
        self.called_with = {
            "customer_id": customer_id,
            "conversions": list(conversions or []),
            "partial_failure": partial_failure,
            "validate_only": validate_only,
        }
        self.calls.append(dict(self.called_with))
        if self._fail_on_call and len(self.calls) == self._fail_on_call:
            raise self._fail_exception or RuntimeError("batch rejected by Google")
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
        rows, errors, _advisories, _skipped = conversion_actions._parse_call_conversion_csv(
            str(tmp_path / "nope.csv")
        )
        assert rows == []
        assert any("not found" in e for e in errors)

    def test_skips_parameters_row_and_normalizes(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            "Parameters:TimeZone=America/Los_Angeles,,,,,\n"
            + _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,250.00,usd\n"
        )
        rows, errors, _advisories, _skipped = conversion_actions._parse_call_conversion_csv(str(p))
        assert errors == []
        assert len(rows) == 1
        assert rows[0]["caller_id"] == "+14155550142"
        assert rows[0]["call_start_time"] == "2026-03-01 12:00:00+00:00"
        assert rows[0]["currency_code"] == "USD"

    def test_missing_required_column(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            "Caller's Phone Number,Conversion Name,Conversion Time,"
            "Conversion Value,Conversion Currency\n"
            "+14155550142,X,2026-03-01T13:00:00Z,10,USD\n"
        )
        rows, errors, _advisories, _skipped = conversion_actions._parse_call_conversion_csv(str(p))
        assert rows == []
        assert any("Call Start Time" in e for e in errors)



class _EchoActionRows:
    """GoogleAdsService stand-in: answers with the names the query asked for.

    The drafts validate the conversion-action names against the account, so a
    draft test needs an ads service. Parsing the names out of the query keeps
    the stub independent of which names a test happens to use.
    """

    def __init__(self, type_name: str = "UPLOAD_CALLS"):
        self.type_name = type_name
        self.queries: list[str] = []

    def search(self, *, customer_id, query):
        self.queries.append(query)
        # Only the IN (...) list holds names; the rest of the query is schema.
        match = re.search(r"IN \((.*?)\)", query, re.S)
        literal = match.group(1) if match else ""
        names = [m.replace("''", "'") for m in re.findall(r"'((?:[^']|'')*)'", literal)]
        return iter([
            _FakeSearchRow(
                name, f"customers/1/conversionActions/{index}", type_name=self.type_name
            )
            for index, name in enumerate(names, start=1)
        ])


def _patch_drafts_client(monkeypatch, type_name: str = "UPLOAD_CALLS") -> _EchoActionRows:
    """Let drafts reach an account: names in the CSV resolve to resource names."""
    ads = _EchoActionRows(type_name)
    client = _client_with(upload_service=_FakeUploadService(), ads_service=ads)
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
    return ads


class TestDraftUploadCallConversions:
    @pytest.fixture(autouse=True)
    def _ads(self, monkeypatch):
        return _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")

    def _write(self, tmp_path):
        p = tmp_path / "phone.csv"
        p.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,A,"
            "2026-03-01T13:00:00Z,250.00,USD\n"
            "+14155550143,2026-03-02T12:00:00Z,A,"
            "2026-03-02T13:00:00Z,500.00,USD\n"
            "+14155550144,2026-03-03T12:00:00Z,B,"
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
            assert s["caller_id"] != "+14155550142"

    def test_raw_rows_live_outside_plan_changes(self, config, tmp_path):
        """The applier needs the raw caller_id; `changes` must never carry it.

        ``changes`` is what a preview and the dry-run response return, so the
        rows go into ``apply_only_payload`` — one field, one decision.
        """
        path = self._write(tmp_path)
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path,
        )
        plan = _stored_plan(result)
        assert plan.apply_only_payload["rows"][0]["caller_id"] == "+14155550142"
        assert "rows" not in plan.changes
        assert "+14155550142" not in repr(plan.changes)
        assert "+14155550142" not in repr(plan.to_preview())

    def test_a_wrong_action_type_is_refused_at_draft_time(
        self, config, tmp_path, monkeypatch
    ):
        """Call uploads need UPLOAD_CALLS; the check runs before confirmation."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = self._write(tmp_path)

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path
        )

        assert "UPLOAD_CALLS" in result["error"]
        assert "plan_id" not in result

    def test_an_unknown_action_is_refused_at_draft_time(
        self, config, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client",
            lambda _cfg: _client_with(
                upload_service=_FakeUploadService(),
                ads_service=_FakeGoogleAdsService([]),
            ),
        )
        path = self._write(tmp_path)

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path
        )

        assert "not found" in result["error"]
        assert "plan_id" not in result

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
                "caller_id": "+14155550142",
                "call_start_time": "2026-03-01 12:00:00+00:00",
                "conversion_name": "My Action",
                "conversion_time": "2026-03-01 13:00:00+00:00",
                "conversion_value": 250.0,
                "currency_code": "USD",
            },
            {
                "caller_id": "+14155550143",
                "call_start_time": "2026-03-02 12:00:00+00:00",
                "conversion_name": "My Action",
                "conversion_time": "2026-03-02 13:00:00+00:00",
                "conversion_value": 500.0,
                "currency_code": "USD",
            },
        ]
        changes = {
            "rows": rows,
            "partial_failure": True,
            # Resolved at draft time now, so a hand-built plan carries it too.
            "conversion_actions": {"My Action": "customers/1/conversionActions/777"},
        }
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
        assert result["sent_total"] == 2
        assert result["accepted_total"] == 2
        assert result["rejected_total"] == 0
        sent = upload.called_with["conversions"]
        assert sent[0].caller_id == "+14155550142"
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
        assert result["accepted_total"] == 1
        assert result["rejected_total"] == 1

    def test_zero_matched_reports_zero_success(self, tmp_path):
        upload = _FakeUploadService(results_count=0)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow("My Action", "customers/1/conversionActions/777")
        ])
        client = _client_with(upload_service=upload, ads_service=ads)
        result = conversion_actions._apply_upload_call_conversions(
            client, "1", self._changes()
        )
        assert result["accepted_total"] == 0
        assert result["rejected_total"] == 2

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
            "User@Example.com,+1 415 555 0142,Test,User,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD\n",
        )
        rows, errors, _advisories, _skipped = conversion_actions._parse_ec_for_leads_csv(path)
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
            ",+14155550142,,,My Action,2026-03-01T12:00:00Z,200.00,USD\n",
        )
        rows, _errors, _advisories, _skipped = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows[0]["email_sha256"] == ""
        assert rows[0]["first_name_sha256"] == ""
        assert rows[0]["phone_sha256"] == _PHONE_HASH

    def test_order_id_optional_column(self, tmp_path):
        path = self._write(
            tmp_path,
            _EC_HEADER + ",Order ID\n"
            "user@example.com,+14155550142,Test,User,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD,ORD-001\n",
        )
        rows, _errors, _advisories, _skipped = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows[0]["order_id"] == "ORD-001"

    def test_order_id_defaults_empty_when_absent(self, tmp_path):
        path = self._write(
            tmp_path,
            _EC_HEADER + "\n"
            "user@example.com,+14155550142,Test,User,My Action,"
            "2026-03-01T12:00:00Z,250.00,USD\n",
        )
        rows, _errors, _advisories, _skipped = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows[0]["order_id"] == ""

    def test_missing_required_column(self, tmp_path):
        path = self._write(
            tmp_path,
            "Email,Phone Number,Conversion Name,Conversion Time,"
            "Conversion Value,Conversion Currency\n"
            "user@example.com,+14155550142,X,2026-03-01T12:00:00Z,10,USD\n",
        )
        rows, errors, _advisories, _skipped = conversion_actions._parse_ec_for_leads_csv(path)
        assert rows == []
        assert any("First Name" in e or "Last Name" in e for e in errors)


class TestDraftUploadEcForLeads:
    @pytest.fixture(autouse=True)
    def _ads(self, monkeypatch):
        return _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")

    def _write(self, tmp_path, *, order_id=False):
        header = _EC_HEADER + (",Order ID" if order_id else "")
        r1 = ("user@example.com,+14155550142,Test,User,Job Close,"
              "2026-03-01T12:00:00Z,500.00,USD")
        r2 = (",+14155550143,,,Job Close,"
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
        assert "+14155550142" not in blob
        assert "Test" not in blob and "User" not in blob
        # The hashed rows are apply-only payload, not part of the summary.
        assert "rows" not in plan.changes
        assert plan.apply_only_payload["rows"][0]["email_sha256"] == _EMAIL_HASH

    def test_sample_rows_mark_hashes_without_showing_them(self, config, tmp_path):
        """A 16-character prefix is 64 bits: enough to confirm a guessed
        address with one hash, in a preview that reaches the model and the log."""
        path = self._write(tmp_path)
        result = (
            conversion_actions.draft_upload_enhanced_conversions_for_leads(
                config, customer_id="1234567890", csv_path=path,
            )
        )
        sample = result["changes"]["sample_rows"]
        assert sample[0]["email_sha256"] == "sha256:set"
        # Nothing in the preview may carry part of a hash.
        blob = repr(result["changes"])
        assert _EMAIL_HASH[:16] not in blob
        assert "..." not in blob

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
            "user@example.com,+14155550142,Test,User,Job Close,"
            "2026-03-01T12:00:00Z,500.00,USD,ORD-001\n"
            "user2@example.com,+14155550143,Test,User,Job Close,"
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
                "postal_code": "",
                "country_code": "",
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
                "postal_code": "",
                "country_code": "",
                "conversion_name": "My Job",
                "conversion_time": "2026-03-02 12:00:00+00:00",
                "conversion_value": 1500.0,
                "currency_code": "USD",
                "order_id": "ORD-002" if order_id else "",
            },
        ]
        changes = {
            "rows": rows,
            "conversion_actions": {"My Job": "customers/1/conversionActions/778"},
            "partial_failure": True,
        }
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
        assert result["sent_total"] == 2
        assert result["accepted_total"] == 2
        sent = upload.called_with["conversions"]
        # Row 1: email + phone. The row also holds hashed names, but names
        # without country and postal code are not an address identifier, so no
        # half fragment is attached.
        assert len(sent[0].user_identifiers) == 2
        assert sent[0].user_identifiers[0].hashed_email == _EMAIL_HASH
        assert sent[0].user_identifiers[1].hashed_phone_number == _PHONE_HASH
        assert not any(
            uid.address_info.hashed_first_name for uid in sent[0].user_identifiers
        )
        # Row 2: phone only = 1 identifier.
        assert len(sent[1].user_identifiers) == 1

    def test_the_address_is_sent_only_as_the_complete_unit(self, tmp_path):
        """Names + country + postal together; anything less is left out."""
        client, upload = self._client(results_count=1)
        rows = [{
            "email_sha256": _EMAIL_HASH,
            "phone_sha256": "",
            "first_name_sha256": _FIRST_HASH,
            "last_name_sha256": _LAST_HASH,
            "postal_code": "85521",
            "country_code": "DE",
            "conversion_name": "My Job",
            "conversion_time": "2026-03-01 12:00:00+00:00",
            "conversion_value": 10.0,
            "currency_code": "EUR",
            "order_id": "o-1",
        }]

        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            client,
            "1",
            {
                "row_count": 1,
                "rows": rows,
                "conversion_actions": {"My Job": "customers/1/conversionActions/778"},
            },
        )

        sent = upload.called_with["conversions"][0]
        assert len(sent.user_identifiers) == 2      # email + complete address
        address = sent.user_identifiers[1].address_info
        assert address.hashed_first_name == _FIRST_HASH
        assert address.hashed_last_name == _LAST_HASH
        assert address.postal_code == "85521"
        assert address.country_code == "DE"

    def test_success_count_keys_off_conversion_action(self, tmp_path):
        # user_identifiers are echoed back on ALL rows; only 1 matched.
        client, _ = self._client(results_count=1)
        result = (
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._changes()
            )
        )
        assert result["accepted_total"] == 1
        assert result["rejected_total"] == 1

    def test_zero_matched_reports_zero_success(self, tmp_path):
        client, _ = self._client(results_count=0)
        result = (
            conversion_actions._apply_upload_enhanced_conversions_for_leads(
                client, "1", self._changes()
            )
        )
        assert result["accepted_total"] == 0
        assert result["rejected_total"] == 2

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

    def test_gaql_escape_used_for_ec_resolver(self, tmp_path):
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "O'Brien Lead", "customers/1/conversionActions/9",
                type_name="UPLOAD_CLICKS",
            )
        ])
        conversion_actions._resolve_upload_action(
            _ec_client_with(upload=_FakeClickUploadService(), ads=ads),
            "1", ["O'Brien Lead"], expected_type="UPLOAD_CLICKS",
        )
        assert "O\\'Brien Lead" in ads.last_query
        assert "O''Brien" not in ads.last_query


# ---------------------------------------------------------------------------
# Audit-log redaction (write._redact_changes_for_audit)
# ---------------------------------------------------------------------------


class TestPiiNeverReachesAPreviewSurface:
    """Raw caller ids live in ``apply_only_payload``; nothing else may see them.

    ``plan.changes`` is what ``to_preview()`` and the dry-run response return,
    which is why the rows had to leave it entirely — redacting each surface
    was the approach that let the raw number through in the first place.
    """

    def _upload_plan(self, changes_rows: list[dict], **extra) -> object:
        from adloop.safety.preview import ChangePlan

        changes = {"row_count": len(changes_rows), "total_value": 250.0}
        changes.update(extra)
        return ChangePlan(
            operation="upload_call_conversions",
            entity_type="call_conversion_batch",
            entity_id=str(len(changes_rows)),
            customer_id="1234567890",
            changes=changes,
            apply_only_payload={"rows": changes_rows},
        )

    def _row(self) -> dict:
        return {
            "caller_id": "+14155550142",
            "call_start_time": "2026-03-01 12:00:00+00:00",
            "conversion_name": "A",
            "conversion_time": "2026-03-01 13:00:00+00:00",
            "conversion_value": 250.0,
            "currency_code": "USD",
        }

    def test_preview_and_apply_payload_are_disjoint(self):
        plan = self._upload_plan([self._row()])

        assert "+14155550142" not in repr(plan.to_preview())
        assert "+14155550142" not in repr(plan.changes)
        # …and the applier still gets what it needs.
        assert plan.apply_payload()["rows"][0]["caller_id"] == "+14155550142"

    def test_changes_win_over_apply_only_payload_on_collision(self):
        plan = self._upload_plan([self._row()], total_value=999.0)

        assert plan.apply_payload()["total_value"] == 999.0

    def test_a_store_that_round_trips_the_dataclass_keeps_the_rows(self):
        """What kLOsk's persistent store has to support, pinned as a test."""
        import dataclasses

        from adloop.safety import preview as preview_store

        plan = self._upload_plan([self._row()])
        preview_store.store_plan(plan)

        # A JSON/dict-shaped round trip, the way a hosted store persists a plan.
        stored = preview_store.get_plan(plan.plan_id)
        revived = preview_store.ChangePlan(**dataclasses.asdict(stored))

        assert revived.apply_payload()["rows"][0]["caller_id"] == "+14155550142"

    def test_a_store_that_drops_the_field_fails_loudly(self, tmp_path):
        """Silence is the dangerous outcome: an empty upload that reports success."""
        from adloop.ads.conversion_actions import _apply_upload_call_conversions

        with pytest.raises(RuntimeError, match="apply_only_payload"):
            _apply_upload_call_conversions(
                SimpleNamespace(), "1234567890", {"row_count": 3}
            )

    def test_refused_two_phase_does_not_leak_caller_id(self, tmp_path):
        """The refusal path logs ``plan.changes`` — which no longer holds rows."""
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
        plan = self._upload_plan([self._row()])
        preview_store.store_plan(plan)

        prev_sink = audit.get_audit_sink()
        audit.set_audit_sink(audit.FileAuditSink())
        try:
            resp = write.confirm_and_apply(cfg, plan_id=plan.plan_id, dry_run=False)
        finally:
            audit.set_audit_sink(prev_sink)

        assert resp["status"] == "DRY_RUN_REQUIRED"
        logged = log_path.read_text()
        assert '"result": "refused_two_phase"' in logged
        # The logged plan is the summary: no rows, no caller id.
        assert "+14155550142" not in logged
        assert '"row_count": 1' in logged


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

    def test_upload_ops_are_wired_into_the_dispatch_table(self, monkeypatch):
        """Behaviour, not source text: both operations reach their applier.

        The old test grepped ``_execute_plan``'s source; the dispatch table
        moved to ``_dispatch_ads_plan`` when the dry run became a validate-only
        wrapper, so it passed for the wrong reason and then failed for the
        wrong reason. Dispatching for real cannot drift like that.
        """
        import adloop.ads.conversion_actions as ca
        from adloop.safety.preview import ChangePlan

        seen: list[tuple[str, str, int]] = []

        def _recorder(kind):
            def _applier(client, cid, changes):
                seen.append((kind, cid, int(changes.get("row_count") or 0)))
                return {"applied": kind}
            return _applier

        monkeypatch.setattr(
            ca, "_apply_upload_call_conversions", _recorder("call")
        )
        monkeypatch.setattr(
            ca,
            "_apply_upload_enhanced_conversions_for_leads",
            _recorder("ec"),
        )

        for operation, kind in (
            ("upload_call_conversions", "call"),
            ("upload_enhanced_conversions_for_leads", "ec"),
        ):
            plan = ChangePlan(
                operation=operation,
                customer_id="1234567890",
                changes={"row_count": 2},
                apply_only_payload={"rows": [{"a": 1}, {"a": 2}]},
            )
            assert write._dispatch_ads_plan(object(), "1234567890", plan) == {
                "applied": kind
            }

        assert seen == [("call", "1234567890", 2), ("ec", "1234567890", 2)]


class TestCsvInputHardening:

    """The upload CSV is a local file; on a hosted runtime it must not be read."""

    def _write(self, tmp_path, body: str, name: str = "upload.csv"):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_server_mode_refuses_before_touching_the_filesystem(
        self, config, tmp_path, monkeypatch
    ):
        from adloop.runtime import set_deployment_mode

        monkeypatch.setattr(
            conversion_actions, "_read_upload_csv", lambda _p: (_ for _ in ()).throw(
                AssertionError("no filesystem access in server mode")
            )
        )
        set_deployment_mode("server")
        try:
            result = conversion_actions.draft_upload_call_conversions(
                config, customer_id="1234567890",
                csv_path=str(tmp_path / "missing.csv"),
            )
        finally:
            set_deployment_mode("local")

        assert "not available on the hosted server" in result["error"]

    def test_errors_never_echo_the_file_content(self, tmp_path):
        # A header the caller sent must not come back in the error text: on a
        # hosted runtime that first line can be any readable file's first line.
        path = self._write(tmp_path, "SECRET-COLUMN-NAME\n1,2,3\n")

        rows, errors, _tz = conversion_actions._read_upload_csv(path)
        assert rows                      # readable
        _, errors = conversion_actions._column_map(rows[0][1], ["Expected A"])
        assert errors and "SECRET-COLUMN-NAME" not in " ".join(errors)
        assert "Expected A" in errors[0]

    def test_non_csv_suffix_is_refused(self, tmp_path):
        path = self._write(tmp_path, "a,b\n", name="upload.txt")
        _, errors, _tz = conversion_actions._read_upload_csv(path)
        assert "must be a .csv file" in errors[0]

    def test_directory_is_refused(self, tmp_path):
        _, errors, _tz = conversion_actions._read_upload_csv(str(tmp_path))
        assert "not a regular file" in errors[0]

    def test_oversized_file_is_refused_before_reading(self, tmp_path, monkeypatch):
        path = self._write(tmp_path, "a,b\n")
        monkeypatch.setattr(conversion_actions, "_MAX_CSV_BYTES", 2)
        _, errors, _tz = conversion_actions._read_upload_csv(path)
        assert "MB" in errors[0] and "2,000 rows" in errors[0]

    def test_bom_and_comment_rows_are_tolerated(self, tmp_path):
        body = (
            "\ufeffParameters:TimeZone=Europe/Berlin\n"
            "# comment\n"
            + _EC_HEADER + "\n"
            + "user@example.com,+14155550142,Test,User,My Action,"
              "2026-03-01T12:00:00Z,200.00,USD\n"
        )
        rows, errors, _advisories, _skipped = conversion_actions._parse_ec_for_leads_csv(
            self._write(tmp_path, body)
        )
        assert errors == []
        assert len(rows) == 1


class _OnlyKnownActions:
    """GoogleAdsService stub that knows one action name and nothing else."""

    def __init__(self, known=("My Action",), type_name: str = "UPLOAD_CALLS"):
        self.known = set(known)
        self.type_name = type_name

    def search(self, *, customer_id, query):
        literal = query.split("IN (", 1)[1].split(")", 1)[0]
        names = [
            m.replace("''", "'")
            for m in re.findall(r"'((?:[^']|'')*)'", literal)
        ]
        return iter([
            _FakeSearchRow(
                name, f"customers/1/conversionActions/{index}", type_name=self.type_name
            )
            for index, name in enumerate(
                [n for n in names if n in self.known], start=1
            )
        ])


class TestErrorsNeverEchoCells:
    """A shifted column puts anything in a cell — an error must not repeat it."""

    SENTINEL = "MAX.SCHMIDT@WEB.DE"

    def test_a_bad_timezone_row_names_only_the_line(self, tmp_path):
        path = tmp_path / "phone.csv"
        path.write_text(
            f"Parameters:TimeZone={self.SENTINEL},,,,,\n" + _CALL_HEADER
        )

        _rows, errors, _tz = conversion_actions._read_upload_csv(str(path))

        text = " ".join(errors)
        assert "Line 1" in text
        assert "IANA" in text
        assert self.SENTINEL not in text

    def test_an_unknown_default_zone_names_no_value(self):
        _value, problem = conversion_actions._parse_timestamp(
            "2026-03-01 12:00", self.SENTINEL
        )

        assert problem
        assert "IANA" in problem
        assert self.SENTINEL not in problem

    def test_a_missing_conversion_action_is_reported_by_line(
        self, config, tmp_path, monkeypatch
    ):
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + f"+14155550142,2026-03-01T12:00:00Z,{self.SENTINEL},"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        client = _client_with(
            upload_service=_FakeUploadService(),
            ads_service=_OnlyKnownActions(known=("My Action",)),
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )

        error = result["error"]
        assert self.SENTINEL not in error
        assert "CSV lines: [2]" in error


class TestWarningsNeverEchoCells:
    """A shifted column turns a neighbouring cell into the warning text.

    That text travels into ``changes``, the preview and the audit log, so a
    column that is off by one must not print an address or a name.
    """

    def _write(self, tmp_path, body: str):
        path = tmp_path / "upload.csv"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_a_bad_currency_names_the_column_not_the_cell(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = self._write(
            tmp_path,
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,max.schmidt@web.de\n",
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path
        )

        # The row is left out with its line, and the cell is never echoed.
        assert "zero conversion rows" in result["error"]
        assert result["skipped_rows"][0]["row"] == 2
        text = result["skipped_rows"][0]["reason"]
        assert "Conversion Currency" in text
        assert "max.schmidt" not in text.lower()

    def test_a_bad_country_code_keeps_the_cell_out_of_the_plan(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = self._write(
            tmp_path,
            _EC_HEADER_ADDRESS + "\n"
            "user@example.com,,Anna,Lena,My Action,2026-03-01T12:00:00Z,10,USD,"
            "max.schmidt@web.de,85521\n",
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path
        )

        changes = _stored_plan(result).changes
        text = " ".join(changes["parse_warnings"])
        assert "Row 2" in text
        assert "Country Code" in text
        # Not in the warning, and not anywhere else in the plan summary either.
        assert "max.schmidt" not in text.lower()
        assert "max.schmidt" not in json.dumps(changes, default=str).lower()


class TestShortRowsAreSkippedNotFatal:
    """A record that stops early is a skipped row, not an IndexError."""

    def _write(self, tmp_path, body: str):
        path = tmp_path / "upload.csv"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_a_truncated_call_row_is_skipped_with_its_line(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = self._write(
            tmp_path,
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action\n"
            + "+14155550143,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,USD\n",
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=path
        )

        changes = _stored_plan(result).changes
        assert changes["row_count"] == 1
        assert changes["skipped_rows"] == [
            {"row": 2, "reason": changes["skipped_rows"][0]["reason"]}
        ]
        assert "short row" in changes["skipped_rows"][0]["reason"]

    def test_a_truncated_ec_row_is_skipped_with_its_line(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = self._write(
            tmp_path,
            _EC_HEADER + "\n"
            "user@example.com,,Anna,Lena,My Action\n"
            "second@example.com,,Ben,Lena,My Action,2026-03-01T12:00:00Z,10,USD\n",
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path
        )

        changes = _stored_plan(result).changes
        assert changes["row_count"] == 1
        assert [entry["row"] for entry in changes["skipped_rows"]] == [2]
        assert "short row" in changes["skipped_rows"][0]["reason"]

    def test_missing_optional_columns_do_not_skip_the_row(
        self, config, tmp_path, monkeypatch
    ):
        """Country Code and Postal Code may be absent — that used to raise."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = self._write(
            tmp_path,
            _EC_HEADER_ADDRESS + "\n"
            "user@example.com,,Anna,Lena,My Action,2026-03-01T12:00:00Z,10,USD\n",
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=path
        )

        changes = _stored_plan(result).changes
        assert changes["row_count"] == 1
        assert changes["skipped_rows"] == []


class TestUploadBatching:
    """One request per 2,000 rows, with a ledger — and a resume point on failure."""

    def _changes(self, rows: int) -> dict:
        return {
            "row_count": rows,
            "conversion_actions": {"A": "customers/1/conversionActions/7"},
            "rows": [
                {
                    "caller_id": f"+1415555{i:04d}",
                    "call_start_time": "2026-03-01 12:00:00+00:00",
                    "conversion_name": "A",
                    "conversion_time": "2026-03-01 13:00:00+00:00",
                    "conversion_value": 1.0,
                    "currency_code": "USD",
                }
                for i in range(rows)
            ],
        }

    def _ads(self):
        return _FakeGoogleAdsService([
            _FakeSearchRow("A", "customers/1/conversionActions/7")
        ])

    def test_rows_are_split_at_the_api_limit(self):
        upload = _FakeUploadService(results_count=10_000)
        client = _client_with(upload_service=upload, ads_service=self._ads())

        result = conversion_actions._apply_upload_call_conversions(
            client, "1234567890", self._changes(2501)
        )

        assert [len(c["conversions"]) for c in upload.calls] == [2000, 501]
        assert result["batch_count"] == 2
        assert result["sent_total"] == 2501
        # Hand-built rows have no source_line, so the ledger falls back to
        # positions; a real draft reports the CSV lines (see the test below).
        assert result["batches"] == [
            {"batch": 1, "first_source_line": 1, "last_source_line": 2000,
             "sent": 2000, "accepted": 2000, "rejected": 0},
            {"batch": 2, "first_source_line": 2001, "last_source_line": 2501,
             "sent": 501, "accepted": 501, "rejected": 0},
        ]

    def test_the_ledger_separates_sent_from_accepted(self, monkeypatch):
        """Rows Google rejected per row were sent, not accepted."""
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", 5)
        upload = _FakeUploadService(results_count=2)   # 2 of 3 carry a match
        client = _client_with(upload_service=upload, ads_service=self._ads())

        result = conversion_actions._apply_upload_call_conversions(
            client, "1234567890", self._changes(3)
        )

        assert result["sent_total"] == 3
        assert result["accepted_total"] == 2
        assert result["rejected_total"] == 1
        assert result["batches"][0]["sent"] == 3
        assert result["batches"][0]["accepted"] == 2

    def test_a_dry_run_admits_it_cannot_count_matches(self, monkeypatch):
        """Validate-only answers carry no results, so those counts are unknown."""
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", 5)
        client = _client_with(
            upload_service=_FakeUploadService(results_count=0), ads_service=self._ads()
        )
        from adloop.ads.validate_only import ValidateOnlyClient

        validator = ValidateOnlyClient(client)

        result = conversion_actions._apply_upload_call_conversions(
            validator, "1234567890", self._changes(3)
        )

        assert result["sent_total"] == 3
        assert result["accepted_total"] is None
        assert result["rejected_total"] is None
        assert result["batches"][0]["accepted"] is None

    def test_partial_failure_is_always_switched_on(self):
        upload = _FakeUploadService(results_count=1)
        client = _client_with(upload_service=upload, ads_service=self._ads())

        conversion_actions._apply_upload_call_conversions(
            client, "1234567890", self._changes(1)
        )

        assert upload.calls[0]["partial_failure"] is True

    def test_a_failed_batch_reports_what_is_already_uploaded(self):
        # Batch 1 goes through, batch 2 does not. A blind retry would
        # double-count the first 2,000 calls — there is no dedup key for them.
        upload = _FakeUploadService(
            results_count=10_000,
            fail_on_call=2,
            fail_exception=_google_rejection("Invalid conversion action"),
        )
        client = _client_with(upload_service=upload, ads_service=self._ads())

        with pytest.raises(RuntimeError) as excinfo:
            conversion_actions._apply_upload_call_conversions(
                client, "1234567890", self._changes(2501)
            )

        message = str(excinfo.value)
        assert "batch 2 of 2" in message
        assert "CSV lines 2001-2501" in message
        assert "2000 row(s) from 1 batch(es) were already sent" in message
        assert "resume the CSV at line 2001" in message
        # The ledger travels as data, not only inside the text.
        assert excinfo.value.sent_total == 2000
        assert excinfo.value.resume_from_line == 2001
        assert excinfo.value.batches[0]["sent"] == 2000

    def test_ec_upload_batches_too(self):
        upload = _FakeClickUploadService(results_count=10_000)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "A", "customers/1/conversionActions/8", type_name="UPLOAD_CLICKS"
            )
        ])
        rows = [
            {
                "email_sha256": _EMAIL_HASH,
                "phone_sha256": "",
                "first_name_sha256": "",
                "last_name_sha256": "",
                "postal_code": "",
                "country_code": "",
                "conversion_name": "A",
                "conversion_time": "2026-03-01 13:00:00+00:00",
                "conversion_value": 1.0,
                "currency_code": "USD",
                "order_id": f"order-{i}",
            }
            for i in range(2001)
        ]

        result = conversion_actions._apply_upload_enhanced_conversions_for_leads(
            _ec_client_with(upload=upload, ads=ads),
            "1234567890",
            {
                "row_count": 2001,
                "rows": rows,
                "conversion_actions": {"A": "customers/1/conversionActions/8"},
            },
        )

        assert [len(c["conversions"]) for c in upload.calls] == [2000, 1]
        assert result["batch_count"] == 2

    def test_upload_plans_ask_for_a_second_confirmation(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        """A signal to the model — the brake that enforces anything is
        safety.two_phase_apply, which the Google path does not read."""
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,A,"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert result["requires_double_confirm"] is True


_EC_HEADER_ADDRESS = _EC_HEADER.replace(
    "Conversion Currency", "Conversion Currency,Country Code,Postal Code"
)


class TestSkippedAndUnmatchableRows:

    """Rows that cannot match are reported, not uploaded and not silently counted."""

    def test_call_rows_without_a_usable_caller_id_are_reported(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"
            + ",2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"
            + "02079460018,2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 2
        # Physical CSV lines: header is line 1, so the two broken rows are 3 and 4.
        assert [s["row"] for s in plan.changes["skipped_rows"]] == [3, 4]
        assert "empty" in plan.changes["skipped_rows"][0]["reason"]
        assert "valid E.164" in plan.changes["skipped_rows"][1]["reason"]
        assert "default_region" in plan.changes["skipped_rows"][1]["reason"]
        assert len(plan.apply_only_payload["rows"]) == 1

    def test_an_email_that_is_not_an_address_does_not_make_a_row_usable(
        self, config, tmp_path, monkeypatch
    ):
        """`n/a` in the Email column used to hash to a truthy value."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            "n/a,,,,My Action,2026-03-01T12:00:00Z,10,USD\n"
            "user@example.com,,Anna,Lena,My Action,2026-03-01T13:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 1
        skipped = plan.changes["skipped_rows"][0]
        assert skipped["row"] == 2
        assert "not an address" in skipped["reason"]

    def test_a_row_that_fails_to_parse_counts_as_skipped_not_as_a_warning(
        self, config, tmp_path, monkeypatch
    ):
        """`skipped_count` has to equal the rows that are really not uploaded."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
            + "+14155550143,2026-03-01T12:00:00Z,My Action,"
              "not-a-time,10,USD\n"
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 1
        assert plan.changes["skipped_rows"][0]["row"] == 3
        assert "not a recognized timestamp" in plan.changes["skipped_rows"][0]["reason"]
        # Nothing advisory happened, so there is nothing in the warnings.
        assert plan.changes["parse_warnings"] == []
        assert "1 row(s) from the CSV are not uploaded" in plan.changes["skipped_note"]

    def test_an_unusable_email_is_dropped_and_warned_about(
        self, config, tmp_path, monkeypatch
    ):
        """The phone still identifies the row, so it uploads without the email."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            "n/a,+14155550142,Anna,Lena,My Action,2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["rows_with_email"] == 0
        assert plan.changes["rows_with_phone"] == 1
        row = plan.apply_only_payload["rows"][0]
        assert row["email_sha256"] == ""
        assert row["phone_sha256"]
        warnings = " ".join(plan.changes["match_warnings"])
        assert "not an address" in warnings
        assert "[2]" in warnings   # the affected CSV line

    def test_a_csv_of_only_unusable_call_rows_plans_nothing(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + ",2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "Nothing was planned" in result["error"]
        assert result["skipped_rows"][0]["row"] == 2   # header is line 1

    def test_ec_rows_without_any_identifier_are_reported(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            + "user@example.com,+14155550142,Test,User,A,"
              "2026-03-01T12:00:00Z,10,USD\n"
            + ",,,,A,2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 1
        assert "no usable identifier" in plan.changes["skipped_rows"][0]["reason"]

    def test_name_only_rows_are_skipped(self, config, tmp_path, monkeypatch):
        """Google needs country + postal next to hashed names, not instead of them."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "Nothing was planned" in result["error"]
        assert "country code and postal code" in result["skipped_rows"][0]["reason"]

    def test_names_with_a_partial_address_are_skipped(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER_ADDRESS + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD,DE,\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "Nothing was planned" in result["error"]
        assert "incomplete" in result["skipped_rows"][0]["reason"]

    def test_an_address_without_names_is_skipped(self, config, tmp_path, monkeypatch):
        """Country + postal alone identify nobody."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER_ADDRESS + "\n"
            + ",,,,A,2026-03-01T12:00:00Z,10,USD,DE,85521\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "Nothing was planned" in result["error"]
        assert "no names" in result["skipped_rows"][0]["reason"]

    def test_names_plus_a_complete_address_are_usable(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER_ADDRESS + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD,de, 855 21 \n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 0
        row = plan.apply_only_payload["rows"][0]
        assert row["country_code"] == "DE"      # upper-cased
        # Only surrounding whitespace is trimmed: Google documents no
        # canonical form for postal codes, so inner spaces are left alone.
        assert row["postal_code"] == "855 21"

    def test_an_invalid_country_code_drops_the_address_not_the_row(
        self, config, tmp_path, monkeypatch
    ):
        """A typo in the country column must not cost a lead its email match."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER_ADDRESS + "\n"
            + "user@example.com,,Test,User,A,2026-03-01T12:00:00Z,10,USD,"
              "Germany,85521\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 0
        row = plan.apply_only_payload["rows"][0]
        assert row["email_sha256"]                     # kept
        assert row["country_code"] == ""               # address dropped
        assert row["postal_code"] == ""
        assert any(
            "two-letter ISO code" in w and "row 2" in w.lower()
            for w in plan.changes["parse_warnings"]
        )

    def test_a_row_left_without_identifier_is_skipped_not_a_parse_error(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER_ADDRESS + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD,Germany,85521\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "Nothing was planned" in result["error"]
        assert result["skipped_rows"][0]["row"] == 2
        assert "country code and postal code" in result["skipped_rows"][0]["reason"]

    def test_a_non_e164_phone_drops_the_identifier_and_warns(self, config, tmp_path, monkeypatch):
        """The row survives on its email; only the unusable phone is dropped."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            + "user@example.com,02079460018,,,A,"
              "2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        row = plan.apply_only_payload["rows"][0]
        assert row["phone_sha256"] == ""           # not a matchable hash
        assert row["email_sha256"]                 # but the row is still useful
        assert any("not a valid E.164" in w for w in plan.changes["match_warnings"])

    def test_a_row_with_only_an_unusable_phone_is_skipped(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            + ",02079460018,,,A,2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "Nothing was planned" in result["error"]
        assert "not a valid E.164" in result["skipped_rows"][0]["reason"]

    def test_a_row_with_two_broken_identifiers_names_both(
        self, config, tmp_path, monkeypatch
    ):
        """Fixing only the first reason would leave the row skipped again."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n" + "n/a,02079460018,,,A,2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        reason = result["skipped_rows"][0]["reason"]
        assert "not a valid E.164" in reason
        assert "not an address" in reason
        assert "no other identifier" in reason

    def test_country_and_postal_reach_the_address_info(self):
        upload = _FakeClickUploadService(results_count=1)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "A", "customers/1/conversionActions/8", type_name="UPLOAD_CLICKS"
            )
        ])
        rows = [{
            "email_sha256": _EMAIL_HASH,
            "phone_sha256": "",
            "first_name_sha256": _FIRST_HASH,
            "last_name_sha256": _LAST_HASH,
            "postal_code": "85521",
            "country_code": "DE",
            "conversion_name": "A",
            "conversion_time": "2026-03-01 13:00:00+00:00",
            "conversion_value": 1.0,
            "currency_code": "EUR",
            "order_id": "o-1",
        }]

        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            _ec_client_with(upload=upload, ads=ads),
            "1234567890",
            {
                "row_count": 1,
                "rows": rows,
                "conversion_actions": {"A": "customers/1/conversionActions/8"},
            },
        )

        conversion = upload.calls[0]["conversions"][0]
        address = [
            uid.address_info for uid in conversion.user_identifiers
            if uid.address_info.postal_code
        ][0]
        assert address.postal_code == "85521"
        assert address.country_code == "DE"
        assert address.hashed_first_name == _FIRST_HASH

    def test_csv_with_address_columns_counts_them(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER_ADDRESS + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD,DE,85521\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.changes["rows_with_address"] == 1
        assert not any(
            "only hashed names" in w for w in plan.changes["match_warnings"]
        )


class TestUploadFlowThroughConfirmAndApply:
    """Preview → dry run → apply, which is where the raw rows leaked.

    The dry-run response returns ``plan.changes``, so a raw caller id in
    ``changes`` reached the model even though the preview itself looked fine.
    These tests walk the whole flow through the real entry point.
    """

    def _config(self, tmp_path, **safety):
        # The apply half of the flow needs a config that does not force dry runs.
        safety.setdefault("require_dry_run", False)
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), **safety),
        )

    def _draft(self, config, tmp_path, monkeypatch):
        client = _client_with(
            upload_service=_FakeUploadService(results_count=1),
            ads_service=_EchoActionRows("UPLOAD_CALLS"),
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        return preview, client

    def test_dry_run_response_and_audit_log_carry_no_raw_number(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, _client = self._draft(config, tmp_path, monkeypatch)
        assert "+14155550142" not in repr(preview)

        # The dry run talks to Google in validate-only mode; that request is not
        # what this test is about, so it is stubbed out.
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda _cfg, _plan: {"validated_calls": 1, "skipped_calls": 0},
        )
        dry = write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)

        assert dry["status"] == "DRY_RUN_SUCCESS"
        assert "+14155550142" not in repr(dry)
        assert "+14155550142" not in (tmp_path / "audit.log").read_text()

    def test_apply_uploads_the_raw_number_once_and_logs_none(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, client = self._draft(config, tmp_path, monkeypatch)

        applied = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert applied["status"] == "APPLIED", applied
        assert "+14155550142" not in repr(applied)
        assert len(client._services["ConversionUploadService"].calls) == 1
        # The upload itself must carry it — Google cannot match a hashed number.
        sent = client._services["ConversionUploadService"].calls[0]["conversions"]
        assert sent[0].caller_id == "+14155550142"
        assert "+14155550142" not in (tmp_path / "audit.log").read_text()


class TestDryRunUsesValidateOnly:
    """kLOsk's 0.16.1 fix: a dry run must reach Google with validate_only=True.

    Before that fix the upload methods slipped past the wrapper (it only
    intercepted ``mutate*``), so a "dry run" of this tool uploaded for real.
    The suite-wide fixture replaces ``_validate_with_google`` with an offline
    stub, so this class restores the real path for its own tests.
    """

    @pytest.fixture(autouse=True)
    def _real_validation(self, monkeypatch):
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda config, plan: write._execute_plan(config, plan, validate_only=True),
        )

    def _config(self, tmp_path):
        # A log file inside tmp_path: the default would append to the
        # developer's own ~/.adloop/audit.log.
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=True, log_file=str(tmp_path / "audit.log")
            ),
        )

    def _call_draft(self, config, tmp_path, monkeypatch):
        client = _client_with(
            upload_service=_FakeUploadService(results_count=1),
            ads_service=_EchoActionRows("UPLOAD_CALLS"),
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        return preview, client

    def test_call_upload_dry_run_is_validate_only(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        preview, client = self._call_draft(config, tmp_path, monkeypatch)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=True
        )

        assert result["status"] == "DRY_RUN_SUCCESS", result
        assert result["checks"] == {"validated_calls": 1, "skipped_calls": 0}, result
        service = client._services["ConversionUploadService"]
        assert len(service.calls) == 1
        assert service.calls[0]["validate_only"] is True
        assert len(service.calls[0]["conversions"]) == 1

    def test_ec_upload_dry_run_is_validate_only(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        upload = _FakeClickUploadService(results_count=1)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CLICKS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            + "user@example.com,+14155550142,Test,User,Job Close,"
              "2026-03-01T12:00:00Z,500.00,USD\n"
        )
        preview = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=True
        )

        assert result["status"] == "DRY_RUN_SUCCESS", result
        assert upload.calls[0]["validate_only"] is True


class TestNormalizationMatchesGoogleDocs:
    """Google's upload-identifiers page spells out the canonicalization.

    Getting this wrong does not raise an error — it just never matches, which
    is why each rule has a test of its own.
    """

    def test_gmail_dots_and_plus_tags_are_removed(self):
        assert (
            conversion_actions._normalize_email("Jane.Doe+Shopping@googlemail.com")
            == "janedoe@googlemail.com"
        )
        assert (
            conversion_actions._normalize_email("jane.doe+shopping@gmail.com")
            == "janedoe@gmail.com"
        )

    def test_other_domains_keep_dots_and_plus_tags(self):
        assert (
            conversion_actions._normalize_email("user.name+NYC@Example.com")
            == "user.name+nyc@example.com"
        )

    def test_email_whitespace_is_removed_but_names_are_only_trimmed(self):
        assert (
            conversion_actions._normalize_email(" User@Example.com ")
            == "user@example.com"
        )
        # Google's own example hashes names with plain ``strip().lower()``:
        # inner spaces survive, so "Anna Lena" stays "anna lena".
        assert conversion_actions._normalize_name("  Anna   Maria ") == "anna   maria"
        assert conversion_actions._normalize_name("von der Berg") == "von der berg"

    def test_a_malformed_value_is_not_an_address(self):
        # No "@" (or an empty side) is not an address. A CRM export writes
        # "n/a" into empty columns, and hashing that would look like a usable
        # identifier while matching nobody.
        assert conversion_actions._normalize_email("Not An Email") == ""
        assert conversion_actions._normalize_email("n/a") == ""
        assert conversion_actions._normalize_email("@example.com") == ""
        assert conversion_actions._normalize_email("user@") == ""
        assert conversion_actions._normalize_email("") == ""


class TestPartialUploadRetiresThePlan:
    """A half-finished upload must not be run again from the same plan.

    ``confirm_and_apply`` kept the plan after a failed apply, so the reflex
    "confirm again" resent every batch that had already gone through — and call
    conversions have no dedup key, so those rows would be counted twice.
    """

    def _config(self, tmp_path, **safety):
        safety.setdefault("require_dry_run", False)
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), **safety),
        )

    def _plan(self, tmp_path, monkeypatch, *, rows=2501, fail_on_call=2):
        upload = _FakeUploadService(
            results_count=10_000,
            fail_on_call=fail_on_call,
            fail_exception=_google_rejection("Invalid conversion action"),
        )
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        path = tmp_path / "phone.csv"
        body = "".join(
            f"+1415555{i:04d},2026-03-01T12:00:00Z,My Action,"
            f"2026-03-01T13:00:00Z,10,USD\n"
            for i in range(rows)
        )
        path.write_text(_CALL_HEADER + body)
        preview = conversion_actions.draft_upload_call_conversions(
            self._config(tmp_path), customer_id="1234567890", csv_path=str(path)
        )
        return preview, upload

    def test_the_response_carries_the_ledger_and_the_resume_line(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, _upload = self._plan(tmp_path, monkeypatch)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "PARTIAL_UPLOAD", result
        assert result["sent_total"] == 2000
        # The ledger's counts are summed for the caller, the same way the
        # apply result reports them: batch 1 sent 2,000 rows, all matched.
        assert result["accepted_total"] == 2000
        assert result["rejected_total"] == 0
        assert result["batches"][0]["first_source_line"] == 2   # header is line 1
        assert result["batches"][0]["last_source_line"] == 2001
        assert result["resume_from_line"] == 2002                # first line of batch 2

    def test_a_second_confirm_cannot_resend_the_first_batches(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, upload = self._plan(tmp_path, monkeypatch)

        first = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )
        assert first["status"] == "PARTIAL_UPLOAD"
        assert len(upload.calls) == 2          # batch 1 sent, batch 2 failed

        again = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "No pending plan found" in again["error"]
        assert len(upload.calls) == 2          # nothing was sent a second time

    def test_the_audit_log_records_the_partial_upload(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        preview, _upload = self._plan(tmp_path, monkeypatch)

        write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)

        logged = (tmp_path / "audit.log").read_text()
        assert '"result": "partial_upload"' in logged
        assert "15555550000" not in logged     # no raw caller ids in the log

    def test_a_failed_dry_run_does_not_claim_an_upload_happened(
        self, tmp_path, monkeypatch
    ):
        """In validate-only mode nothing was written, so the wording must differ."""
        config = self._config(tmp_path, require_dry_run=True)
        preview, _upload = self._plan(tmp_path, monkeypatch)
        # Restore the real validate-only path the suite's fixture stubs out.
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=True
        )

        assert result["status"] == "DRY_RUN_FAILED", result
        assert "Nothing was uploaded" in result["error"]
        assert "already uploaded" not in result["error"]
        # The plan stays pending: nothing happened, so it is still the caller's
        # to fix or discard (unlike a real partial upload, which retires it).
        assert preview_store.get_plan(preview["plan_id"]) is not None


class TestSourceLinesSurviveCommentsAndSkips:
    """Only one numbering scheme may be used in messages: the physical line."""

    def _draft(self, config, tmp_path, monkeypatch, body: str):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(body)
        return conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )

    def test_skipped_rows_name_the_physical_line(self, config, tmp_path, monkeypatch):
        body = (
            "Parameters:TimeZone=Europe/Berlin\n"          # physical line 1
            "# comment\n"                                   # physical line 2
            + _CALL_HEADER                                  # physical line 3
            + "+14155550142,2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"  # 4
            + ",2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"              # 5
        )

        result = self._draft(config, tmp_path, monkeypatch, body)
        plan = _stored_plan(result)

        assert [s["row"] for s in plan.changes["skipped_rows"]] == [5]

    def test_the_ledger_names_physical_lines(self, config, tmp_path, monkeypatch):
        body = (
            "# comment\n"                                   # physical line 1
            + _CALL_HEADER                                  # physical line 2
            + "+14155550142,2026-03-01T12:00:00Z,A,"
              "2026-03-01T13:00:00Z,10,USD\n"               # physical line 3
        )
        result = self._draft(config, tmp_path, monkeypatch, body)
        plan = _stored_plan(result)

        upload = _FakeUploadService(results_count=1)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        out = conversion_actions._apply_upload_call_conversions(
            client, "1234567890", plan.apply_payload()
        )

        assert out["batches"][0]["first_source_line"] == 3
        assert out["batches"][0]["last_source_line"] == 3


class TestRecordStartLine:
    """The resume hint must point at the record, not into the middle of it."""

    def test_a_quoted_multi_line_field_reports_its_first_line(self, tmp_path):
        path = tmp_path / "upload.csv"
        path.write_text(
            _CALL_HEADER  # line 1
            + '+14155550142,2026-03-01T12:00:00Z,"Multi\nline",'
              "2026-03-01T13:00:00Z,10,USD\n"      # starts line 2, ends line 3
            + "+14155550143,2026-03-01T12:00:00Z,A,"
              "2026-03-01T13:00:00Z,10,USD\n"      # line 4
        )

        records, errors, _tz = conversion_actions._read_upload_csv(str(path))

        assert errors == []
        assert [line for line, _ in records] == [1, 2, 4]

    def test_the_batch_ledger_uses_the_parsed_line(self, tmp_path):
        path = tmp_path / "upload.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,A,2026-03-01T13:00:00Z,10,USD\n"
        )

        rows, _errors, _advisories, _skipped = conversion_actions._parse_call_conversion_csv(
            str(path)
        )
        assert rows[0]["source_line"] == 2      # header is line 1
        upload = _FakeUploadService(results_count=1)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )

        out = conversion_actions._apply_upload_call_conversions(
            client,
            "1234567890",
            {
                "row_count": 1,
                "rows": rows,
                "conversion_actions": {"A": "customers/1/conversionActions/1"},
            },
        )

        assert out["batches"][0]["first_source_line"] == 2


class TestAddressCountingMatchesWhatIsSent:
    """The preview must not count an address the applier leaves out."""

    def _draft(self, config, tmp_path, monkeypatch, body: str):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(body)
        return conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

    def test_a_half_address_is_not_counted(self, config, tmp_path, monkeypatch):
        # Country + postal, but no names: usable only through the email, and the
        # address never goes out.
        result = self._draft(
            config, tmp_path, monkeypatch,
            _EC_HEADER_ADDRESS + "\n"
            + "user@example.com,,, ,A,2026-03-01T12:00:00Z,10,USD,DE,85521\n",
        )
        plan = _stored_plan(result)

        assert plan.changes["rows_with_address"] == 0
        assert plan.changes["skipped_count"] == 0
        warning = " ".join(plan.changes["match_warnings"])
        assert "no first and last name" in warning
        assert "first affected rows: [2]" in warning

    def test_names_without_address_columns_say_what_to_add(
        self, config, tmp_path, monkeypatch
    ):
        """The common lead export: names and an email, no address columns."""
        result = self._draft(
            config, tmp_path, monkeypatch,
            _EC_HEADER + "\n"
            + "user@example.com,+14155550142,Test,User,A,"
              "2026-03-01T12:00:00Z,10,USD\n",
        )
        plan = _stored_plan(result)

        assert plan.changes["rows_with_address"] == 0
        warning = " ".join(plan.changes["match_warnings"])
        assert "add a 'Country Code' and a 'Postal Code' column" in warning
        assert "first affected rows: [2]" in warning

    def test_a_complete_address_is_counted_and_not_warned(
        self, config, tmp_path, monkeypatch
    ):
        result = self._draft(
            config, tmp_path, monkeypatch,
            _EC_HEADER_ADDRESS + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD,DE,85521\n",
        )
        plan = _stored_plan(result)

        assert plan.changes["rows_with_address"] == 1
        assert not plan.changes["match_warnings"]

    def test_the_applier_sends_exactly_what_was_counted(
        self, config, tmp_path, monkeypatch
    ):
        """One row with a complete address, one with a half one."""
        result = self._draft(
            config, tmp_path, monkeypatch,
            _EC_HEADER_ADDRESS + "\n"
            + ",,Test,User,A,2026-03-01T12:00:00Z,10,USD,DE,85521\n"
            + "second@example.com,,, ,A,2026-03-01T12:00:00Z,10,USD,DE,85521\n",
        )
        plan = _stored_plan(result)

        upload = _FakeClickUploadService(results_count=10)
        ads = _FakeGoogleAdsService([
            _FakeSearchRow(
                "A", "customers/1/conversionActions/8", type_name="UPLOAD_CLICKS"
            )
        ])
        conversion_actions._apply_upload_enhanced_conversions_for_leads(
            _ec_client_with(upload=upload, ads=ads),
            "1234567890",
            plan.apply_payload(),
        )

        sent = upload.calls[0]["conversions"]
        addresses = [
            uid.address_info
            for cc in sent for uid in cc.user_identifiers
            if uid.address_info.hashed_first_name
        ]
        assert len(addresses) == plan.changes["rows_with_address"] == 1


class TestPhoneNormalizationInTheDraft:
    """The upload tools take a `default_region` for national-format numbers."""

    def _call_draft(self, config, tmp_path, monkeypatch, number: str, **kwargs):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + f"{number},2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        return conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path), **kwargs
        )

    def test_a_national_number_becomes_usable_with_a_region(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch, "0151 12345678", default_region="de"
        )
        plan = _stored_plan(result)

        assert plan.changes["row_count"] == 1
        assert plan.changes["skipped_count"] == 0
        assert plan.apply_only_payload["rows"][0]["caller_id"] == "+4915112345678"

    def test_without_the_region_the_same_number_is_reported(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(config, tmp_path, monkeypatch, "0151 12345678")

        assert "Nothing was planned" in result["error"]
        assert "default_region" in result["skipped_rows"][0]["reason"]

    def test_the_german_trunk_marker_is_stripped(self, config, tmp_path, monkeypatch):
        """`+49 (0)89 …` used to become +49089123456 — a number that never matches."""
        result = self._call_draft(
            config, tmp_path, monkeypatch, "+49 (0)89 123456"
        )
        plan = _stored_plan(result)

        assert plan.apply_only_payload["rows"][0]["caller_id"] == "+4989123456"

    def test_a_region_name_is_refused(self, config, tmp_path, monkeypatch):
        result = self._call_draft(
            config, tmp_path, monkeypatch, "0151 12345678",
            default_region="Germany",
        )

        assert "two-letter ISO country code" in result["error"]

    def test_ec_hashes_the_normalized_number(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            _EC_HEADER + "\n"
            + "user@example.com,+49 (0)89 123456,Test,User,My Action,"
              "2026-03-01T12:00:00Z,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["phone_sha256"] == conversion_actions._sha256_hex("+4989123456")


class TestTimestampsAndTimeZones:
    """Google wants `yyyy-mm-dd hh:mm:ss±hh:mm`; the draft has to produce it."""

    def _call_draft(self, config, tmp_path, monkeypatch, body: str, **kwargs):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(body)
        return conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path), **kwargs
        )

    def _row(self, start: str, converted: str) -> str:
        return f"+14155550142,{start},My Action,{converted},10,USD\n"

    def test_iso_with_offset_is_converted_to_the_api_format(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            _CALL_HEADER + self._row("2026-03-01T12:00:00Z", "2026-03-01T13:00:00Z"),
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["call_start_time"] == "2026-03-01 12:00:00+00:00"
        assert row["conversion_time"] == "2026-03-01 13:00:00+00:00"

    def test_a_timezone_row_resolves_german_timestamps(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "Parameters:TimeZone=Europe/Berlin,,,,,\n"
            + _CALL_HEADER + self._row("01.03.2026 12:00", "01.03.2026 13:30"),
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["call_start_time"] == "2026-03-01 12:00:00+01:00"
        assert row["conversion_time"] == "2026-03-01 13:30:00+01:00"

    def test_a_24_hour_slash_timestamp_is_accepted(
        self, config, tmp_path, monkeypatch
    ):
        """Google documents `MM/dd/yyyy HH:mm:ss` — accept it next to AM/PM."""
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "Parameters:TimeZone=Europe/Berlin,,,,,\n"
            + _CALL_HEADER + self._row("3/1/2026 13:30:00", "3/1/2026 14:00:00"),
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["call_start_time"] == "2026-03-01 13:30:00+01:00"
        assert row["conversion_time"] == "2026-03-01 14:00:00+01:00"

    def test_a_bare_date_says_that_midnight_was_assumed(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "Parameters:TimeZone=Europe/Berlin,,,,,\n"
            + _CALL_HEADER + self._row("2026-03-01", "2026-03-01"),
        )
        plan = _stored_plan(result)
        row = plan.apply_only_payload["rows"][0]

        # The row goes through (Google accepts midnight), but it is not silent
        # about the assumption.
        assert row["call_start_time"] == "2026-03-01 00:00:00+01:00"
        warnings = " ".join(plan.changes["parse_warnings"])
        assert warnings.count("midnight was assumed") == 2  # both time columns
        # Line 1 is the Parameters row, line 2 the header, so the row is line 3.
        assert "Row 3" in warnings

    def test_an_ec_row_without_a_time_says_the_same(
        self, config, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICKS")
        path = tmp_path / "leads.csv"
        path.write_text(
            "Parameters:TimeZone=Europe/Berlin,,,,,,,,\n"
            + _EC_HEADER + "\n"
            "user@example.com,,Anna,Lena,My Action,2026-03-01,10,USD\n"
        )

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )
        plan = _stored_plan(result)

        assert plan.apply_only_payload["rows"][0]["conversion_time"] == (
            "2026-03-01 00:00:00+01:00"
        )
        assert "midnight was assumed" in " ".join(plan.changes["parse_warnings"])

    def test_without_an_offset_or_a_timezone_row_the_row_is_refused(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            _CALL_HEADER + self._row("01.03.2026 12:00", "01.03.2026 13:00"),
        )

        # A row whose time cannot be resolved is a parsing problem, so it is
        # reported as one — with the physical line.
        assert "zero conversion rows" in result["error"]
        skipped = result["skipped_rows"]
        assert [entry["row"] for entry in skipped] == [2]
        assert "time zone" in skipped[0]["reason"]

    def test_a_us_date_format_is_recognized(self, config, tmp_path, monkeypatch):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "Parameters:TimeZone=UTC,,,,,\n"
            + _CALL_HEADER + self._row("3/1/2026 12:00 PM", "3/1/2026 1:00 PM"),
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["call_start_time"] == "2026-03-01 12:00:00+00:00"

    def test_a_conversion_before_the_call_is_refused(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            _CALL_HEADER + self._row("2026-03-01T13:00:00Z", "2026-03-01T12:00:00Z"),
        )

        assert "zero conversion rows" in result["error"]
        assert "before Call Start Time" in result["skipped_rows"][0]["reason"]

    def test_a_conversion_in_the_future_is_refused(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            _CALL_HEADER + self._row("2099-03-01T12:00:00Z", "2099-03-01T13:00:00Z"),
        )

        assert "future" in result["skipped_rows"][0]["reason"]


class TestValueAndCurrencyAreOptional:
    """A blank cell must stay blank — not become 0.0 or "USD"."""

    def _call_draft(self, config, tmp_path, monkeypatch, row: str):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(_CALL_HEADER + row + "\n")
        return conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )

    def test_an_empty_value_stays_unset(self, config, tmp_path, monkeypatch):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,,USD",
        )
        plan = _stored_plan(result)

        assert plan.apply_only_payload["rows"][0]["conversion_value"] is None
        assert plan.changes["rows_without_value"] == 1
        assert plan.changes["total_value"] == 0

    def test_an_empty_currency_is_not_invented(self, config, tmp_path, monkeypatch):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,",
        )
        plan = _stored_plan(result)

        assert plan.apply_only_payload["rows"][0]["currency_code"] == ""
        assert plan.changes["currency_hint"] == ""
        assert plan.changes["rows_without_currency"] == 1
        # The breakdown still names where the value went: the action's own
        # currency, not an invented one.
        assert plan.changes["total_value_by_currency"] == {"account default": 10.0}
        assert plan.changes["value_warnings"] == []

    def test_mixed_currencies_are_not_summed_into_one_number(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,EUR\n"
            "+14155550143,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,20,USD\n",
        )
        plan = _stored_plan(result)

        assert plan.changes["total_value_by_currency"] == {"EUR": 10.0, "USD": 20.0}
        # No hint, because there is no single answer to give.
        assert plan.changes["currency_hint"] == ""
        warnings = " ".join(plan.changes["value_warnings"])
        assert "2 different currencies" in warnings
        assert "not a meaningful amount" in warnings

    def test_one_shared_currency_keeps_the_hint(self, config, tmp_path, monkeypatch):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,EUR\n"
            "+14155550143,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,20,EUR\n",
        )
        plan = _stored_plan(result)

        assert plan.changes["currency_hint"] == "EUR"
        assert plan.changes["total_value_by_currency"] == {"EUR": 30.0}
        assert plan.changes["value_warnings"] == []

    def test_the_applier_leaves_the_fields_alone_when_they_are_empty(
        self, config, tmp_path, monkeypatch
    ):
        result = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,,",
        )
        plan = _stored_plan(result)

        upload = _FakeUploadService(results_count=1)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        conversion_actions._apply_upload_call_conversions(
            client, "1234567890", plan.apply_payload()
        )

        # Nothing was assigned, so the conversion action's own defaults apply.
        assert upload.calls[0]["conversions"][0].conversion_value == 0.0
        assert upload.calls[0]["conversions"][0].currency_code == ""

    def test_non_finite_and_negative_values_are_refused(
        self, config, tmp_path, monkeypatch
    ):
        for bad in ("nan", "inf", "-5"):
            result = self._call_draft(
                config, tmp_path, monkeypatch,
                "+14155550142,2026-03-01T12:00:00Z,My Action,"
                f"2026-03-01T13:00:00Z,{bad},USD",
            )
            # A row that cannot be parsed is left out, not silently uploaded.
            assert "zero conversion rows" in result["error"], bad
            assert "finite number" in " ".join(
                entry["reason"] for entry in result["skipped_rows"]
            ), bad

    def test_the_currency_must_be_a_three_letter_code(
        self, config, tmp_path, monkeypatch
    ):
        good = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,usd",
        )
        assert _stored_plan(good).apply_only_payload["rows"][0]["currency_code"] == "USD"

        bad = self._call_draft(
            config, tmp_path, monkeypatch,
            "+14155550142,2026-03-01T12:00:00Z,My Action,"
            "2026-03-01T13:00:00Z,10,US",
        )
        assert "3-letter ISO code" in " ".join(
            entry["reason"] for entry in bad["skipped_rows"]
        )


class _UnreadableResponse:
    """A response whose results cannot be read — the batch is already sent."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def results(self):
        raise self._exc


def _google_rejection(message: str):
    """A GoogleAdsException as Google raises it for a rejected request."""
    from google.ads.googleads.errors import GoogleAdsException
    from google.ads.googleads.v25.errors.types import errors as err_types

    failure = err_types.GoogleAdsFailure(
        errors=[err_types.GoogleAdsError(message=message)]
    )
    return GoogleAdsException(None, None, failure, "req-1")


def _partial_failure_with_index(client, index: int, message: str):
    """A partial_failure_error naming conversions[index], as Google sends it."""
    from google.protobuf.any_pb2 import Any as AnyProto
    from google.ads.googleads.v25.errors.types import errors as err_types

    failure = err_types.GoogleAdsFailure(
        errors=[
            err_types.GoogleAdsError(
                message=message,
                location=err_types.ErrorLocation(
                    field_path_elements=[
                        err_types.ErrorLocation.FieldPathElement(index=index)
                    ]
                ),
            )
        ]
    )
    detail = AnyProto()
    # proto-plus wraps the protobuf message; serialize the underlying proto.
    detail.value = type(failure).pb(failure).SerializeToString()
    return SimpleNamespace(code=3, message="partial failure", details=[detail])


class TestPerLineErrors:
    """Google reports `conversions[i]`; the caller needs the CSV line."""

    def _config(self, tmp_path):
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False, log_file=str(tmp_path / "audit.log")
            ),
        )

    def _plan(self, config, tmp_path, monkeypatch, upload=None):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
            + "+14155550143,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        upload = upload or _FakeUploadService(results_count=1)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        return preview, upload, client

    def test_a_rejected_row_is_named_by_its_csv_line(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        preview, upload, client = self._plan(config, tmp_path, monkeypatch)
        upload.partial_failure = _partial_failure_with_index(
            client, 1, "Conversion action is invalid"
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "APPLIED", result
        # Row index 1 is the second data row, i.e. line 3 of the file.
        assert {"batch": 1, "line": 3, "error": "Conversion action is invalid"} in (
            result["result"]["row_errors"]
        )

    def test_a_dry_run_reports_the_rows_instead_of_failing(
        self, tmp_path, monkeypatch
    ):
        """A row that cannot match does not invalidate the whole request.

        The real apply would upload the other row and report this one again, so
        the dry run has to say the same thing instead of DRY_RUN_FAILED.
        """
        config = self._config(tmp_path, )
        preview, upload, client = self._plan(config, tmp_path, monkeypatch)
        upload.partial_failure = _partial_failure_with_index(
            client, 1, "Conversion action is invalid"
        )
        # The suite stubs the real validate-only path; restore it for this test.
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=True
        )

        assert result["status"] == "DRY_RUN_SUCCESS", result
        # Same shape the apply reports: a summary entry plus the CSV line.
        assert {
            "batch": 1, "line": 3, "error": "Conversion action is invalid"
        } in result["row_errors"]
        assert any(
            entry.get("type") == "partial_failure" for entry in result["row_errors"]
        )
        assert result["checks"]["partial_failures"] == 1
        # Only the entry with a CSV line is a row — the batch summary is not.
        assert "problems for 1 row(s)" in result["note"]
        assert "carry out every other row" in result["note"]
        # The dry-run marker is set, so two-phase apply lets the real run go.
        assert preview_store.get_plan(preview["plan_id"]).dry_run_result is not None

    def test_a_dry_run_google_rejects_outright_still_fails(
        self, tmp_path, monkeypatch
    ):
        """A rejected request is a failure — that is what DRY_RUN_FAILED means."""
        config = self._config(tmp_path)
        upload = _FakeUploadService(
            results_count=1,
            fail_on_call=1,
            fail_exception=_google_rejection("INVALID_ARGUMENT"),
        )
        preview, _upload, _client = self._plan(
            config, tmp_path, monkeypatch, upload=upload
        )
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=True
        )

        assert result["status"] == "DRY_RUN_FAILED", result
        assert "The real apply would fail the same way." in result["message"]


class TestThePlanIsClaimedBeforeUploading:
    """One plan, one upload — even if the client times out and confirms again."""

    def _config(self, tmp_path):
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False, log_file=str(tmp_path / "audit.log")
            ),
        )

    def _plan(self, config, tmp_path, monkeypatch, *, rows=1, upload=None):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "".join(
                f"+1415555{i:04d},2026-03-01T12:00:00Z,My Action,"
                f"2026-03-01T13:00:00Z,10,USD\n"
                for i in range(rows)
            )
        )
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        upload = upload or _FakeUploadService(results_count=10_000)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        return preview, upload

    def test_a_successful_apply_leaves_no_pending_plan(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        preview, _upload = self._plan(config, tmp_path, monkeypatch)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "APPLIED"
        assert preview_store.get_plan(preview["plan_id"]) is None

    def test_a_second_confirm_while_the_first_is_running_finds_nothing(
        self, tmp_path, monkeypatch
    ):
        """Once the plan is claimed, a second confirm cannot start an upload."""
        config = self._config(tmp_path)
        preview, upload = self._plan(config, tmp_path, monkeypatch)
        # Simulate the first apply having claimed the plan already.
        assert preview_store.claim_plan(preview["plan_id"]) is not None

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "No pending plan found" in result["error"]
        assert upload.calls == []

    def test_a_failure_before_the_first_request_gives_the_plan_back(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, _upload = self._plan(config, tmp_path, monkeypatch)
        plan = preview_store.get_plan(preview["plan_id"])
        # No payload: the applier refuses before it builds a request.
        plan.apply_only_payload = {}
        preview_store.store_plan(plan)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "apply_only_payload" in result["error"]
        assert preview_store.get_plan(preview["plan_id"]) is not None

    def test_a_transport_failure_reports_an_unknown_outcome(
        self, tmp_path, monkeypatch
    ):
        """A dropped connection is not a rejection: the batch may have arrived."""
        config = self._config(tmp_path)
        upload = _FakeUploadService(
            results_count=10_000,
            fail_on_call=2,
            fail_exception=RuntimeError("DEADLINE_EXCEEDED"),
        )
        preview, _upload = self._plan(
            config, tmp_path, monkeypatch, rows=2501, upload=upload
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "PARTIAL_UPLOAD", result
        assert result["unknown_status"] is True
        assert "unknown outcome" in result["error"]
        assert "may or may not have been received" in result["error"]
        # The uncertain batch is named inclusively, like the ledger's
        # first/last source lines.
        assert result["uncertain_lines"] == [2002, 2502]
        assert result["sent_total"] == 2000
        # This batch is the last one, so there is nothing left to draft — a
        # number here would point past the end of the file.
        assert result["resume_from_line"] is None
        # The message must not contradict the error.
        assert "may or may not have been received" in result["message"]
        # 2000 rows went in batch 1, so the uncertain batch holds 501 of them.
        assert "501 row(s) in lines 2002-2502" in result["message"]
        assert "No rows remain after these." in result["message"]
        # The plan is retired either way: those rows cannot be sent again safely.
        assert preview_store.get_plan(preview["plan_id"]) is None
        logged = (tmp_path / "audit.log").read_text()
        assert '"result": "unknown_status"' in logged

    def test_an_uncertain_batch_that_is_not_last_names_the_next_line(
        self, tmp_path, monkeypatch
    ):
        """Three batches: the uncertain middle one must not eat the third's rows."""
        config = self._config(tmp_path)
        upload = _FakeUploadService(
            results_count=10_000,
            fail_on_call=2,
            fail_exception=RuntimeError("DEADLINE_EXCEEDED"),
        )
        preview, _upload = self._plan(
            config, tmp_path, monkeypatch, rows=4501, upload=upload
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "PARTIAL_UPLOAD", result
        assert result["uncertain_lines"] == [2002, 4001]
        assert result["sent_total"] == 2000
        # Batch 3 starts on source line 4002: the header is line 1, so row n is
        # on line n + 1.
        assert result["resume_from_line"] == 4002
        assert "2000 row(s) in lines 2002-4001" in result["message"]
        assert "from line 4002" in result["message"]
        assert "No rows remain" not in result["message"]

    def test_a_rejection_resumes_at_the_failed_batch(
        self, tmp_path, monkeypatch
    ):
        """A rejection is an answer, so the batch can be sent again as-is."""
        config = self._config(tmp_path)
        upload = _FakeUploadService(
            results_count=10_000,
            fail_on_call=2,
            fail_exception=_google_rejection("INVALID_ARGUMENT"),
        )
        preview, _upload = self._plan(
            config, tmp_path, monkeypatch, rows=2501, upload=upload
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "PARTIAL_UPLOAD", result
        assert result["unknown_status"] is False
        # Nothing is uncertain here: the batch was answered, so the fix belongs
        # inside that batch and the resume hint points at its first row.
        assert "uncertain_lines" not in result
        assert result["resume_from_line"] == 2002
        assert result["sent_total"] == 2000
        assert "2000 row(s) were sent" in result["message"]
        assert "Resume the CSV at line 2002" in result["message"]


class _NoUploadService:
    """A client whose ConversionUploadService cannot be reached."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def get_service(self, name, *args, **kwargs):
        if name == "ConversionUploadService":
            raise RuntimeError("service unavailable")
        return self._inner.get_service(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestAPreSendFailureKeepsThePlan:
    """Nothing left the process, so the plan must stay usable.

    Client construction, credentials, the upload service and the payload
    lookup all run before the first request. Retiring the plan for a failure
    there forces a whole new draft for a retry that cannot duplicate anything,
    and the caller has no way to tell it apart from a real partial upload.
    """

    def _config(self, tmp_path):
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False, log_file=str(tmp_path / "audit.log")
            ),
        )

    def _plan(self, config, tmp_path, monkeypatch, *, rows=1):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "".join(
                f"+1415555{i:04d},2026-03-01T12:00:00Z,My Action,"
                f"2026-03-01T13:00:00Z,10,USD\n"
                for i in range(rows)
            )
        )
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        upload = _FakeUploadService(results_count=10_000)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        return preview, upload, client

    def test_a_client_that_cannot_be_built_keeps_the_plan(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, upload, client = self._plan(config, tmp_path, monkeypatch)

        def boom(_config):
            raise RuntimeError("token expired")

        monkeypatch.setattr("adloop.ads.client.get_ads_client", boom)
        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "still usable" in result["error"], result
        assert preview_store.get_plan(preview["plan_id"]) is not None
        assert upload.calls == []

        # The retry works once the client can be built again — no re-draft.
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        again = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )
        assert again["status"] == "APPLIED", again
        assert len(upload.calls) == 1

    def test_an_unknown_failure_before_the_first_request_keeps_the_plan(
        self, tmp_path, monkeypatch
    ):
        """The decision must not depend on which exception type came out.

        Only the applier knows when a request goes out, and it marks that
        moment; anything raised before it keeps the plan, whatever the type.
        """
        config = self._config(tmp_path)
        preview, upload, client = self._plan(config, tmp_path, monkeypatch)

        def boom(_client, _cid, _changes):
            raise RuntimeError("something broke before the request")

        monkeypatch.setattr(
            conversion_actions, "_apply_upload_call_conversions", boom
        )
        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "something broke before the request" in result["error"]
        assert preview_store.get_plan(preview["plan_id"]) is not None
        assert upload.calls == []

        monkeypatch.undo()
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        again = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )
        assert again["status"] == "APPLIED", again
        assert len(upload.calls) == 1

    def test_the_marker_flips_when_the_request_goes_out(self, monkeypatch):
        """`sent_anything()` is the fact the retention rule is built on."""
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", 5)
        rows = [{"source_line": 2, "caller_id": "+14155550142"}]
        seen: list[bool] = []

        def send(payload):
            seen.append(conversion_actions.sent_anything())
            return SimpleNamespace(results=[])

        conversion_actions.reset_send_state()
        assert conversion_actions.sent_anything() is False
        conversion_actions._upload_in_batches(rows, lambda chunk: list(chunk), send)

        assert seen == [True]        # marked before the request
        assert conversion_actions.sent_anything() is True

    def test_an_unreachable_upload_service_keeps_the_plan(
        self, tmp_path, monkeypatch
    ):
        config = self._config(tmp_path)
        preview, upload, client = self._plan(config, tmp_path, monkeypatch)
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client", lambda _cfg: _NoUploadService(client)
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "still usable" in result["error"], result
        assert preview_store.get_plan(preview["plan_id"]) is not None
        assert upload.calls == []

    def test_a_payload_the_plan_does_not_carry_keeps_the_plan(
        self, tmp_path, monkeypatch
    ):
        """A KeyError before the first request is not a partial upload."""
        config = self._config(tmp_path)
        preview, upload, _client = self._plan(config, tmp_path, monkeypatch)
        plan = preview_store.get_plan(preview["plan_id"])
        plan.changes.pop("conversion_actions")   # what a half-migrated store looks like
        preview_store.store_plan(plan)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "still usable" in result["error"], result
        assert preview_store.get_plan(preview["plan_id"]) is not None
        assert upload.calls == []


class TestNothingSentIsClassifiedHonestly:
    """Phase 1 sends nothing — but only the *first* batch means "nothing at all".

    A build failure in batch 2 used to be reported as "Nothing was sent" and
    the plan stayed retryable. Batch 1 was already in the account, so the
    obvious retry re-sent it, and call conversions have no dedup key to absorb
    the duplicates.
    """

    def _config(self, tmp_path):
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False, log_file=str(tmp_path / "audit.log")
            ),
        )

    def _rows(self, lines):
        return [
            {"source_line": line, "caller_id": f"+1415555{index:04d}"}
            for index, line in enumerate(lines)
        ]

    def _plan(self, config, tmp_path, monkeypatch, *, rows=3, upload=None):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "".join(
                f"+1415555{i:04d},2026-03-01T12:00:00Z,My Action,"
                f"2026-03-01T13:00:00Z,10,USD\n"
                for i in range(rows)
            )
        )
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", 2)
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        upload = upload or _FakeUploadService(results_count=10_000)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        return preview, upload

    def test_the_first_batch_means_nothing_was_sent(self, monkeypatch):
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", 2)
        sent: list[int] = []

        def build(chunk):
            raise ValueError("cannot set caller_id")

        def send(payload):
            sent.append(len(payload))
            return SimpleNamespace(results=[])

        with pytest.raises(conversion_actions.UploadNotSentError) as info:
            conversion_actions._upload_in_batches(self._rows([2, 3]), build, send)

        assert "Nothing was uploaded" in str(info.value)
        assert sent == []

    def test_a_later_batch_build_failure_is_a_partial_upload(self, monkeypatch):
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", 2)
        built: list[int] = []
        sent: list[int] = []

        def build(chunk):
            built.append(len(chunk))
            if len(built) == 2:
                raise ValueError("cannot set caller_id")
            return list(chunk)

        def send(payload):
            sent.append(len(payload))
            return SimpleNamespace(results=[])

        with pytest.raises(conversion_actions.PartialUploadError) as info:
            conversion_actions._upload_in_batches(
                self._rows([2, 3, 4]), build, send
            )

        error = info.value
        assert error.unknown_status is False
        assert error.sent_total == 2
        assert error.uncertain_lines == []
        # Provably not sent, so its own first line is the one to resume at.
        assert error.resume_from_line == 4
        assert sent == [2]
        assert "must not be sent again" in str(error)

    def test_a_broken_row_in_batch_two_retires_the_plan(
        self, tmp_path, monkeypatch
    ):
        """End to end: the plan must not stay usable after batch 1 went out."""
        config = self._config(tmp_path)
        preview, upload = self._plan(config, tmp_path, monkeypatch)

        # Row 3 sits in the second batch; an action name that is not in the
        # plan's resolved map makes the proto builder raise before the request.
        plan = preview_store.get_plan(preview["plan_id"])
        rows = list(plan.apply_only_payload["rows"])
        rows[2]["conversion_name"] = "Not In This Plan"
        plan.apply_only_payload["rows"] = rows
        preview_store.store_plan(plan)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "PARTIAL_UPLOAD", result
        assert result["sent_total"] == 2
        assert result["resume_from_line"] == 4
        assert "uncertain_lines" not in result
        assert len(upload.calls) == 1  # only batch 1 was in a request
        # Retired, so the reflex to confirm again cannot resend batch 1.
        assert preview_store.get_plan(preview["plan_id"]) is None

        again = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )
        assert "No pending plan found" in again["error"]
        assert len(upload.calls) == 1

    def test_a_rejected_first_batch_keeps_the_plan(self, tmp_path, monkeypatch):
        """Google answered for the whole request, so nothing was written."""
        config = self._config(tmp_path)
        upload = _FakeUploadService(
            results_count=10_000,
            fail_on_call=1,
            fail_exception=_google_rejection("INVALID_ARGUMENT"),
        )
        preview, _upload = self._plan(
            config, tmp_path, monkeypatch, rows=2, upload=upload
        )

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert "Nothing was uploaded" in result["error"]
        # Still there: re-drafting the whole file to retry one rejection is
        # needless, and nothing was written that a retry could duplicate.
        assert preview_store.get_plan(preview["plan_id"]) is not None


class TestClaimFallbackAndUnknownStatus:
    """The store hook is a requirement, not a hard dependency."""

    def _config(self, tmp_path):
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False, log_file=str(tmp_path / "audit.log")
            ),
        )

    def _draft(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
        )
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        upload = _FakeUploadService(results_count=1)
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)
        return preview, upload

    def test_a_store_without_claim_still_applies(self, tmp_path, monkeypatch):
        """Only atomicity is lost — the hosted store without `claim` must work."""

        class PlainStore:
            def __init__(self):
                self.plans = {}

            def store(self, tenant, plan):
                self.plans[(tenant, plan.plan_id)] = plan

            def get(self, tenant, plan_id):
                return self.plans.get((tenant, plan_id))

            def remove(self, tenant, plan_id):
                self.plans.pop((tenant, plan_id), None)

        config = self._config(tmp_path)
        store = PlainStore()
        # The draft has to store into the plain store: it has no `claim`.
        preview_store.set_plan_store(store)
        try:
            preview, upload = self._draft(config, tmp_path, monkeypatch)
            result = write.confirm_and_apply(
                config, plan_id=preview["plan_id"], dry_run=False
            )
        finally:
            preview_store.set_plan_store(preview_store.InMemoryPlanStore())

        assert result["status"] == "APPLIED", result
        assert len(upload.calls) == 1
        assert store.plans == {}

    def test_the_non_atomic_fallback_says_so_once(self, caplog, monkeypatch):
        """A store without `claim()` cannot stop overlapping confirms."""

        class PlainStore:
            def __init__(self):
                self.plans = {}

            def store(self, tenant, plan):
                self.plans[(tenant, plan.plan_id)] = plan

            def get(self, tenant, plan_id):
                return self.plans.get((tenant, plan_id))

            def remove(self, tenant, plan_id):
                self.plans.pop((tenant, plan_id), None)

        monkeypatch.setattr(preview_store, "_FALLBACK_WARNED", False)
        preview_store.set_plan_store(PlainStore())
        try:
            with caplog.at_level("WARNING"):
                assert preview_store.claim_plan("nope") is None
                assert preview_store.claim_plan("nope") is None
        finally:
            preview_store.set_plan_store(preview_store.InMemoryPlanStore())

        warnings = [r.message for r in caplog.records if "claim()" in r.message]
        assert len(warnings) == 1, warnings
        assert "cannot stop two overlapping confirmations" in warnings[0]

    def test_a_plan_store_without_claim_is_not_fatal_for_other_operations(self):
        """ `claim_plan` must not raise AttributeError for budgets and keywords. """

        class PlainStore:
            def __init__(self):
                self.plans = {}

            def store(self, tenant, plan):
                self.plans[(tenant, plan.plan_id)] = plan

            def get(self, tenant, plan_id):
                return self.plans.get((tenant, plan_id))

            def remove(self, tenant, plan_id):
                self.plans.pop((tenant, plan_id), None)

        store = PlainStore()
        preview_store.set_plan_store(store)
        try:
            plan = preview_store.ChangePlan(operation="update_campaign")
            store.store("local", plan)
            assert preview_store.claim_plan(plan.plan_id) is plan
            assert store.plans == {}
        finally:
            preview_store.set_plan_store(preview_store.InMemoryPlanStore())


class TestOffsetTimeZoneRows:
    def test_an_offset_time_zone_row_is_accepted(self, config, tmp_path, monkeypatch):
        """Google's template allows `+0100` as well as an IANA id."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            "Parameters:TimeZone=+0100,,,,,\n"
            + _CALL_HEADER
            + "+14155550142,2026-03-01T12:00:00Z,My Action,"
              "2026-03-01T13:00:00Z,10,USD\n"
            + "+14155550143,01.03.2026 12:00,My Action,01.03.2026 13:00,10,USD\n"
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        rows = _stored_plan(result).apply_only_payload["rows"]

        assert rows[1]["call_start_time"] == "2026-03-01 12:00:00+01:00"

    def test_a_negative_offset_works_too(self, config, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            "Parameters:TimeZone=-0500,,,,,\n"
            + _CALL_HEADER
            + "+14155550143,01.03.2026 12:00,My Action,01.03.2026 13:00,10,USD\n"
        )

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["call_start_time"] == "2026-03-01 12:00:00-05:00"


class TestUnreadableResponseAndTimeZoneRows:
    """Two ways an upload can end without a usable answer."""

    def _config(self, tmp_path):
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False, log_file=str(tmp_path / "audit.log")
            ),
        )

    def test_an_unreadable_response_names_the_uncertain_batch(
        self, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            _CALL_HEADER
            + "".join(
                f"+1415555{i:04d},2026-03-01T12:00:00Z,My Action,"
                f"2026-03-01T13:00:00Z,10,USD\n"
                for i in range(3)
            )
        )
        config = self._config(tmp_path)
        preview = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        upload = _FakeUploadService(
            results_count=10_000,
            results_error=RuntimeError("response parsing failed"),
        )
        client = _client_with(
            upload_service=upload, ads_service=_EchoActionRows("UPLOAD_CALLS")
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: client)

        result = write.confirm_and_apply(
            config, plan_id=preview["plan_id"], dry_run=False
        )

        assert result["status"] == "PARTIAL_UPLOAD", result
        assert result["unknown_status"] is True
        # The request went out but its answer could not be read, so the batch
        # is uncertain, not uploaded: ``sent_total`` counts only what is
        # proven to be in.
        assert result["sent_total"] == 0
        assert result["uncertain_lines"] == [2, 4]
        assert result["resume_from_line"] is None
        assert "could not be read" in result["error"]
        # The message names the same lines and does not ask for a resend.
        assert "3 row(s) in lines 2-4" in result["message"]
        assert "No rows remain after these." in result["message"]
        assert preview_store.get_plan(preview["plan_id"]) is None

    def test_a_bad_offset_row_is_reported_without_a_crash(
        self, tmp_path, monkeypatch
    ):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            "Parameters:TimeZone=+2500,,,,,\n"
            + _CALL_HEADER
            + "+14155550142,01.03.2026 12:00,My Action,01.03.2026 13:00,10,USD\n"
        )
        config = self._config(tmp_path)

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )

        # An unusable zone is a row-level problem, not a Python traceback.
        assert "CSV parse failed" in result["error"]
        details = " ".join(result["details"])
        assert "Line 1" in details
        assert "not a valid IANA zone id or ±HHMM offset" in details
        # The cell is never echoed: a shifted column could put anything here.
        assert "+2500" not in details

    def test_a_bad_offset_row_stops_an_ec_file_too(self, tmp_path, monkeypatch):
        """The time-zone row is read by the shared reader, so both files see it."""
        _patch_drafts_client(monkeypatch, "UPLOAD_CLICK_CONVERSIONS")
        path = tmp_path / "ec.csv"
        path.write_text(
            "Parameters:TimeZone=+0270,,,,,,,\n"
            + _EC_HEADER
            + "\nuser@example.com,,Anna,Lena,My Action,01.03.2026 13:00,10,USD\n"
        )
        config = self._config(tmp_path)

        result = conversion_actions.draft_upload_enhanced_conversions_for_leads(
            config, customer_id="1234567890", csv_path=str(path)
        )

        assert "CSV parse failed" in result["error"]
        details = " ".join(result["details"])
        assert "IANA" in details
        assert "+0270" not in details

    def test_an_iso_offset_with_a_colon_is_accepted(self, tmp_path, monkeypatch):
        _patch_drafts_client(monkeypatch, "UPLOAD_CALLS")
        path = tmp_path / "phone.csv"
        path.write_text(
            "Parameters:TimeZone=+01:00,,,,,\n"
            + _CALL_HEADER
            + "+14155550142,01.03.2026 12:00,My Action,01.03.2026 13:00,10,USD\n"
        )
        config = self._config(tmp_path)

        result = conversion_actions.draft_upload_call_conversions(
            config, customer_id="1234567890", csv_path=str(path)
        )
        row = _stored_plan(result).apply_only_payload["rows"][0]

        assert row["call_start_time"] == "2026-03-01 12:00:00+01:00"


class TestResumeLineNamesTheNextBatch:
    """The resume hint must name a row that is really still to be sent.

    Adding one to the failing batch's last line would land inside a record
    whose quoted field spans several lines, on a skipped or comment line, or
    past the end of the file when the batch was the last one.
    """

    def _run(self, monkeypatch, source_lines, *, fail_on_call, exc, batch_size=2):
        monkeypatch.setattr(conversion_actions, "_MAX_ROWS_PER_REQUEST", batch_size)
        rows = [
            {"source_line": line, "caller_id": f"+1415555{index:04d}"}
            for index, line in enumerate(source_lines)
        ]
        calls: list[int] = []

        def send(payload):
            calls.append(len(payload))
            if len(calls) == fail_on_call:
                raise exc
            return SimpleNamespace(results=[])

        with pytest.raises(conversion_actions.PartialUploadError) as info:
            conversion_actions._upload_in_batches(
                rows, lambda chunk: list(chunk), send
            )
        return info.value

    def test_the_next_batch_supplies_the_resume_line(self, monkeypatch):
        # Record 2 spans lines 3-9, so "last line + 1" would point into it.
        error = self._run(
            monkeypatch,
            [2, 10, 20, 21, 30],
            fail_on_call=1,
            exc=RuntimeError("DEADLINE_EXCEEDED"),
        )

        assert error.unknown_status is True
        assert error.uncertain_lines == [2, 10]
        assert error.uncertain_rows == 2
        assert error.sent_total == 0
        assert error.resume_from_line == 20

    def test_the_last_batch_has_nothing_to_resume(self, monkeypatch):
        error = self._run(
            monkeypatch,
            [2, 3, 4],
            fail_on_call=2,
            exc=RuntimeError("DEADLINE_EXCEEDED"),
        )

        # Only the first batch is provably in; the second may or may not be.
        assert error.sent_total == 2
        assert error.uncertain_lines == [4, 4]
        assert error.uncertain_rows == 1
        assert error.resume_from_line is None
        assert "no rows remain after these" in str(error)

    def test_a_rejection_keeps_the_failed_batch_first_line(self, monkeypatch):
        error = self._run(
            monkeypatch,
            [2, 3, 4],
            fail_on_call=2,
            exc=_google_rejection("INVALID_ARGUMENT"),
        )

        assert error.unknown_status is False
        assert error.uncertain_lines == []
        assert error.resume_from_line == 4
