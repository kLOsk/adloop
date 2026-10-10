"""Tests for GAQL error fidelity and nested-message serialization (issue #47)."""

from types import SimpleNamespace

from adloop.ads.gaql import _parse_gaql_error, _to_python


class TestParseGaqlError:
    def test_structured_failure_message_is_surfaced(self):
        exc = Exception("400 Bad Request")
        exc.failure = SimpleNamespace(
            errors=[
                SimpleNamespace(
                    error_code="query_error: PROHIBITED_FIELD_IN_SELECT_CLAUSE",
                    message="Field 'ad_group_criterion.quality_info.quality_score' is prohibited in SELECT.",
                )
            ]
        )
        result = _parse_gaql_error(exc)
        assert "PROHIBITED_FIELD_IN_SELECT_CLAUSE" in result
        assert "quality_score" in result

    def test_known_code_in_structured_error_gets_hint_suffix(self):
        exc = Exception("boom")
        exc.failure = SimpleNamespace(
            errors=[
                SimpleNamespace(
                    error_code="query_error: UNRECOGNIZED_FIELD",
                    message="Unrecognized field 'campaign.nam'.",
                )
            ]
        )
        result = _parse_gaql_error(exc)
        assert "campaign.nam" in result
        assert "hint:" in result

    def test_multiple_errors_joined(self):
        exc = Exception("boom")
        exc.failure = SimpleNamespace(
            errors=[
                SimpleNamespace(error_code="a", message="first"),
                SimpleNamespace(error_code="b", message="second"),
            ]
        )
        result = _parse_gaql_error(exc)
        assert "first" in result and "second" in result

    def test_fallback_to_hint_without_structured_failure(self):
        result = _parse_gaql_error(Exception("INVALID_ARGUMENT: something"))
        assert result.startswith("INVALID_ARGUMENT:")

    def test_fallback_truncates_long_raw_errors(self):
        result = _parse_gaql_error(Exception("x" * 900))
        assert len(result) < 600


class TestToPythonNestedMessages:
    def test_proto_plus_message_serializes_to_dict(self):
        from google.ads.googleads.v25.common.types import LocationInfo

        info = LocationInfo(geo_target_constant="geoTargetConstants/2276")
        result = _to_python(info)
        assert isinstance(result, dict)
        assert result["geo_target_constant"] == "geoTargetConstants/2276"

    def test_text_asset_shortcut_still_returns_plain_string(self):
        from google.ads.googleads.v25.common.types import AdTextAsset

        asset = AdTextAsset(text="Headline one")
        assert _to_python(asset) == "Headline one"

    def test_scalars_pass_through(self):
        assert _to_python("x") == "x"
        assert _to_python(3) == 3
        assert _to_python(None) is None


class TestToPythonFieldMask:
    def test_field_mask_returns_snake_case_paths(self):
        # change_event.changed_fields is a FieldMask; its JSON form is one
        # comma-joined camelCase string, which hides the real field paths.
        from google.ads.googleads.v25.services.types.google_ads_service import (
            GoogleAdsRow,
        )

        from adloop.ads.gaql import _extract_field

        row = GoogleAdsRow()
        row.change_event.changed_fields.paths.extend(
            ["amount_micros", "maximize_conversions.target_cpa_micros"]
        )

        assert _extract_field(row, "change_event.changed_fields") == [
            "amount_micros",
            "maximize_conversions.target_cpa_micros",
        ]
