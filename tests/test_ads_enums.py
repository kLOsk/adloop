"""Tests for the dynamic Google Ads enum introspection helper."""
from __future__ import annotations

import pytest

from adloop.ads.enums import enum_names, _enum_introspection_client


class TestEnumNames:
    def test_returns_frozenset(self):
        result = enum_names("ConversionActionTypeEnum")
        assert isinstance(result, frozenset)

    def test_excludes_unspecified_and_unknown_by_default(self):
        result = enum_names("ConversionActionTypeEnum")
        assert "UNSPECIFIED" not in result
        assert "UNKNOWN" not in result

    def test_can_include_unspecified_when_requested(self):
        result = enum_names(
            "ConversionActionTypeEnum", exclude_unspecified=False
        )
        assert "UNSPECIFIED" in result
        assert "UNKNOWN" in result

    def test_known_conversion_action_types_present(self):
        result = enum_names("ConversionActionTypeEnum")
        # The members AdLoop has built tooling around must always exist.
        for required in (
            "AD_CALL", "WEBSITE_CALL", "WEBPAGE", "WEBPAGE_CODELESS",
            "GOOGLE_ANALYTICS_4_CUSTOM",
        ):
            assert required in result, f"missing {required} from SDK enum"

    def test_call_conversion_reporting_state_complete(self):
        result = enum_names("CallConversionReportingStateEnum")
        assert "DISABLED" in result
        assert "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION" in result
        assert "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION" in result

    def test_attribution_models_complete(self):
        result = enum_names("AttributionModelEnum")
        # The two attribution models AdLoop documents in conversion-action
        # docstrings must always be valid.
        assert "GOOGLE_ADS_LAST_CLICK" in result
        assert "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN" in result

    def test_counting_types_minimal_pair(self):
        result = enum_names("ConversionActionCountingTypeEnum")
        assert result == frozenset({"ONE_PER_CLICK", "MANY_PER_CLICK"})

    def test_promotion_extension_occasion_complete(self):
        result = enum_names("PromotionExtensionOccasionEnum")
        # Common occasions BGI / users will reach for
        for required in (
            "BLACK_FRIDAY", "CYBER_MONDAY", "CHRISTMAS",
            "MOTHERS_DAY", "FATHERS_DAY", "BACK_TO_SCHOOL",
        ):
            assert required in result, f"missing {required}"

    def test_promotion_discount_modifier(self):
        result = enum_names("PromotionExtensionDiscountModifierEnum")
        assert "UP_TO" in result

    def test_unknown_enum_raises(self):
        with pytest.raises(AttributeError):
            enum_names("ThisEnumDoesNotExist")

    def test_lru_cache_returns_same_instance(self):
        a = enum_names("ConversionActionCountingTypeEnum")
        b = enum_names("ConversionActionCountingTypeEnum")
        assert a is b, "expected memoized identical frozenset"

    def test_introspection_client_memoized(self):
        c1 = _enum_introspection_client()
        c2 = _enum_introspection_client()
        assert c1 is c2


class TestModulesUseDynamicEnums:
    """Confirm the validators we refactored actually pull from the SDK."""

    def test_conversion_actions_uses_sdk_types(self):
        from adloop.ads import conversion_actions

        sdk_types = enum_names("ConversionActionTypeEnum")
        # The module-level constant should BE the dynamic frozenset.
        assert conversion_actions._VALID_TYPES == sdk_types
        # Should include members the old hardcoded list missed
        # (sanity check: the dynamic list is a strict superset).
        assert "CLICK_TO_CALL" in conversion_actions._VALID_TYPES

    def test_conversion_actions_uses_sdk_categories(self):
        from adloop.ads import conversion_actions

        assert (
            conversion_actions._VALID_CATEGORIES
            == enum_names("ConversionActionCategoryEnum")
        )

    def test_write_promotion_occasions_dynamic(self):
        from adloop.ads import write

        assert (
            write._VALID_PROMOTION_OCCASIONS
            == enum_names("PromotionExtensionOccasionEnum")
        )

    def test_write_call_reporting_states_dynamic(self):
        from adloop.ads import write

        assert (
            write._VALID_CALL_REPORTING_STATES
            == enum_names("CallConversionReportingStateEnum")
        )
