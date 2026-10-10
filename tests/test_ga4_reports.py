"""Tests for GA4 report building, metadata lookup and key-event listing."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.analytics.data_v1beta.types import Filter

from adloop.config import AdLoopConfig, GA4Config
from adloop.ga4 import reports
from adloop.ga4.tracking import list_key_events


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ga4=GA4Config(property_id="properties/123456"))


def _response(dims, mets, rows, row_count=None):
    """A stand-in RunReportResponse."""
    return SimpleNamespace(
        dimension_headers=[SimpleNamespace(name=d) for d in dims],
        metric_headers=[SimpleNamespace(name=m) for m in mets],
        rows=[
            SimpleNamespace(
                dimension_values=[SimpleNamespace(value=v) for v in r[: len(dims)]],
                metric_values=[SimpleNamespace(value=v) for v in r[len(dims):]],
            )
            for r in rows
        ],
        row_count=len(rows) if row_count is None else row_count,
    )


def _run(config, response=None, **kwargs):
    """Run the report against a mocked Data API client; return (result, request)."""
    client = MagicMock()
    client.run_report.return_value = response or _response([], [], [])
    with patch("adloop.ga4.client.get_data_client", return_value=client):
        result = reports.run_ga4_report(config, property_id="properties/123456", **kwargs)
    request = client.run_report.call_args.args[0] if client.run_report.called else None
    return result, request


class TestBackwardCompatibility:
    def test_plain_report_sends_one_unnamed_range_and_no_extras(self, config):
        response = _response(["country"], ["sessions"], [["DE", "10"], ["AT", "4"]])
        result, request = _run(
            config, response, dimensions=["country"], metrics=["sessions"],
        )

        assert len(request.date_ranges) == 1
        assert request.date_ranges[0].name == ""
        assert request.offset == 0
        assert not request.order_bys
        assert "dimension_filter" not in request
        assert "metric_filter" not in request
        assert result["rows"] == [
            {"country": "DE", "sessions": "10"},
            {"country": "AT", "sessions": "4"},
        ]
        assert result["row_count"] == 2
        assert "next_offset" not in result
        assert "comparison_date_range" not in result

    def test_requires_a_dimension_or_metric(self, config):
        result, request = _run(config)
        assert "At least one dimension or metric" in result["error"]
        assert request is None


class TestDimensionFilter:
    def test_single_condition_maps_to_a_string_filter(self, config):
        _, request = _run(
            config,
            dimensions=["sessionDefaultChannelGroup"],
            metrics=["sessions"],
            dimension_filter=[{
                "field": "sessionDefaultChannelGroup",
                "op": "EXACT",
                "value": "Paid Search",
            }],
        )

        flt = request.dimension_filter.filter
        assert flt.field_name == "sessionDefaultChannelGroup"
        assert flt.string_filter.match_type == Filter.StringFilter.MatchType.EXACT
        assert flt.string_filter.value == "Paid Search"
        assert flt.string_filter.case_sensitive is False

    def test_several_conditions_are_anded(self, config):
        _, request = _run(
            config,
            dimensions=["pagePath", "country"],
            metrics=["sessions"],
            dimension_filter=[
                {"field": "pagePath", "op": "begins_with", "value": "/blog"},
                {"field": "country", "op": "IN_LIST", "values": ["Germany", "Austria"]},
                {"field": "pagePath", "op": "CONTAINS", "value": "draft", "not": True},
            ],
        )

        group = request.dimension_filter.and_group.expressions
        assert len(group) == 3
        assert group[0].filter.string_filter.match_type == Filter.StringFilter.MatchType.BEGINS_WITH
        assert list(group[1].filter.in_list_filter.values) == ["Germany", "Austria"]
        negated = group[2].not_expression.filter
        assert negated.string_filter.match_type == Filter.StringFilter.MatchType.CONTAINS
        assert negated.string_filter.value == "draft"

    @pytest.mark.parametrize("op", ["ENDS_WITH", "FULL_REGEXP", "PARTIAL_REGEXP"])
    def test_other_string_match_types(self, config, op):
        _, request = _run(
            config, dimensions=["pagePath"], metrics=["sessions"],
            dimension_filter=[{"field": "pagePath", "op": op, "value": "x",
                               "case_sensitive": True}],
        )
        flt = request.dimension_filter.filter.string_filter
        assert flt.match_type == Filter.StringFilter.MatchType[op]
        assert flt.case_sensitive is True

    def test_op_defaults_to_exact(self, config):
        _, request = _run(
            config, dimensions=["country"], metrics=["sessions"],
            dimension_filter=[{"field": "country", "value": "Germany"}],
        )
        assert request.dimension_filter.filter.string_filter.match_type == (
            Filter.StringFilter.MatchType.EXACT
        )

    @pytest.mark.parametrize("bad, fragment", [
        ([{"op": "EXACT", "value": "x"}], "'field' is required"),
        ([{"field": "country", "op": "GREATER_THAN", "value": "x"}], "not valid for dimensions"),
        ([{"field": "country", "op": "IN_LIST", "value": "Germany"}], "IN_LIST needs"),
        ([{"field": "country", "op": "EXACT", "value": ""}], "non-empty string"),
        ([{"field": "country", "value": "x", "match": "EXACT"}], "unknown key"),
        (["country=Germany"], "must be an object"),
    ])
    def test_invalid_conditions_fail_before_any_request(self, config, bad, fragment):
        result, request = _run(
            config, dimensions=["country"], metrics=["sessions"], dimension_filter=bad,
        )
        assert result["error"] == "Validation failed"
        assert any(fragment in d for d in result["details"]), result["details"]
        assert request is None


class TestMetricFilter:
    def test_numeric_comparisons(self, config):
        _, request = _run(
            config, dimensions=["pagePath"], metrics=["sessions", "bounceRate"],
            metric_filter=[
                {"field": "sessions", "op": ">", "value": 100},
                {"field": "bounceRate", "op": "LESS_THAN_OR_EQUAL", "value": "0.5"},
            ],
        )

        first, second = request.metric_filter.and_group.expressions
        assert first.filter.numeric_filter.operation == Filter.NumericFilter.Operation.GREATER_THAN
        assert first.filter.numeric_filter.value.int64_value == 100
        assert second.filter.numeric_filter.operation == (
            Filter.NumericFilter.Operation.LESS_THAN_OR_EQUAL
        )
        assert second.filter.numeric_filter.value.double_value == 0.5

    def test_between(self, config):
        _, request = _run(
            config, dimensions=["pagePath"], metrics=["sessions"],
            metric_filter=[{"field": "sessions", "op": "BETWEEN", "value": [10, 20]}],
        )
        between = request.metric_filter.filter.between_filter
        assert between.from_value.int64_value == 10
        assert between.to_value.int64_value == 20

    @pytest.mark.parametrize("bad, fragment", [
        ([{"field": "sessions", "op": "EXACT", "value": 1}], "not valid for metrics"),
        ([{"field": "sessions", "op": ">", "value": "lots"}], "must be a number"),
        ([{"field": "sessions", "op": ">", "value": True}], "must be a number"),
        ([{"field": "sessions", "op": "BETWEEN", "value": [1]}], "[from, to]"),
        ([{"field": "sessions"}], "not valid for metrics"),
    ])
    def test_invalid_conditions(self, config, bad, fragment):
        result, request = _run(
            config, dimensions=["pagePath"], metrics=["sessions"], metric_filter=bad,
        )
        assert any(fragment in d for d in result["details"]), result["details"]
        assert request is None


class TestOrderingAndPaging:
    def test_order_by_metric_and_dimension(self, config):
        _, request = _run(
            config, dimensions=["date"], metrics=["sessions"],
            order_by=[{"field": "sessions", "desc": True}, {"field": "date"}],
        )

        first, second = request.order_bys
        assert first.metric.metric_name == "sessions" and first.desc is True
        assert second.dimension.dimension_name == "date" and second.desc is False

    def test_order_by_unrequested_field_is_refused(self, config):
        result, request = _run(
            config, dimensions=["date"], metrics=["sessions"],
            order_by=[{"field": "totalUsers", "desc": True}],
        )
        assert "must be one of the requested" in " ".join(result["details"])
        assert request is None

    def test_offset_is_sent_and_next_offset_reported(self, config):
        response = _response(["date"], ["sessions"], [["20261001", "5"]] * 2, row_count=7)
        result, request = _run(
            config, response, dimensions=["date"], metrics=["sessions"],
            limit=2, offset=2,
        )

        assert request.offset == 2
        assert request.limit == 2
        assert result["offset"] == 2
        assert result["next_offset"] == 4

    def test_last_page_has_no_next_offset(self, config):
        response = _response(["date"], ["sessions"], [["20261001", "5"]], row_count=3)
        result, _ = _run(
            config, response, dimensions=["date"], metrics=["sessions"],
            limit=2, offset=2,
        )
        assert "next_offset" not in result

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"offset": -1}, "offset"),
        ({"limit": 0}, "limit"),
    ])
    def test_invalid_paging(self, config, kwargs, fragment):
        result, request = _run(config, dimensions=["date"], **kwargs)
        assert any(fragment in d for d in result["details"])
        assert request is None


class TestComparison:
    def test_sends_two_named_ranges_and_labels_rows(self, config):
        response = _response(
            ["country", "dateRange"], ["sessions"],
            [["DE", "current", "10"], ["DE", "comparison", "8"]],
        )
        result, request = _run(
            config, response, dimensions=["country"], metrics=["sessions"],
            date_range_start="2026-09-01", date_range_end="2026-09-30",
            compare_start="2026-08-01", compare_end="2026-08-31",
        )

        current, previous = request.date_ranges
        assert (current.start_date, current.end_date, current.name) == (
            "2026-09-01", "2026-09-30", "current",
        )
        assert (previous.start_date, previous.end_date, previous.name) == (
            "2026-08-01", "2026-08-31", "comparison",
        )
        assert [r["dateRange"] for r in result["rows"]] == ["current", "comparison"]
        assert result["date_ranges"] == {
            "current": {"start": "2026-09-01", "end": "2026-09-30"},
            "comparison": {"start": "2026-08-01", "end": "2026-08-31"},
        }

    def test_half_a_comparison_is_refused(self, config):
        result, request = _run(
            config, dimensions=["country"], compare_start="2026-08-01",
        )
        assert "compare_start and compare_end" in " ".join(result["details"])
        assert request is None


class TestMetadata:
    @staticmethod
    def _metadata():
        def item(api, ui, category, description="", custom=False, deprecated=(), **extra):
            return SimpleNamespace(
                api_name=api, ui_name=ui, category=category, description=description,
                custom_definition=custom, deprecated_api_names=list(deprecated), **extra,
            )

        metric_type = SimpleNamespace(name="TYPE_FLOAT")
        return SimpleNamespace(
            dimensions=[
                item("sessionDefaultChannelGroup", "Session default channel group", "Traffic source"),
                item("customEvent:plan", "Plan", "Custom", "Plan tier", custom=True),
            ],
            metrics=[
                item("keyEvents", "Key events", "Event", "Count of key events",
                     deprecated=["conversions"], type_=metric_type, expression="",
                     blocked_reasons=[]),
                item("sessions", "Sessions", "Session", "Sessions",
                     type_=SimpleNamespace(name="TYPE_INTEGER"), expression="",
                     blocked_reasons=[]),
            ],
        )

    def _get(self, config, **kwargs):
        client = MagicMock()
        client.get_metadata.return_value = self._metadata()
        with patch("adloop.ga4.client.get_data_client", return_value=client):
            result = reports.get_ga4_metadata(config, **kwargs)
        return result, client

    def test_lists_everything_compactly_by_default(self, config):
        result, client = self._get(config, property_id="123456")

        client.get_metadata.assert_called_once_with(name="properties/123456/metadata")
        assert result["dimension_count"] == 2 and result["metric_count"] == 2
        assert "description" not in result["dimensions"][0]
        key_events = result["metrics"][0]
        assert key_events["type"] == "TYPE_FLOAT"
        assert key_events["deprecated_api_names"] == ["conversions"]

    def test_search_matches_deprecated_names_and_adds_descriptions(self, config):
        result, _ = self._get(config, property_id="properties/123456", search="Conversions")

        assert [m["api_name"] for m in result["metrics"]] == ["keyEvents"]
        assert result["metrics"][0]["description"] == "Count of key events"
        assert result["dimensions"] == []

    def test_custom_only_and_kind(self, config):
        result, _ = self._get(
            config, property_id="123456", custom_only=True, kind="dimensions",
        )
        assert [d["api_name"] for d in result["dimensions"]] == ["customEvent:plan"]
        assert result["dimensions"][0]["custom"] is True
        assert "metrics" not in result

    def test_rejects_unknown_kind_and_missing_property(self, config):
        assert "kind must be" in self._get(config, property_id="1", kind="events")[0]["error"]
        assert "property_id is required" in self._get(config, property_id="")[0]["error"]


class TestListKeyEvents:
    def test_lists_key_events_via_admin_api(self, config):
        def key_event(name, method, deletable, custom=True):
            ke = MagicMock()
            ke.event_name = name
            ke.counting_method.name = method
            ke.create_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
            ke.deletable = deletable
            ke.custom = custom
            ke.name = f"properties/123456/keyEvents/{name}-id"
            ke.default_value = None
            return ke

        admin = MagicMock()
        admin.list_key_events.return_value = [
            key_event("sign_up", "ONCE_PER_SESSION", True),
            key_event("purchase", "ONCE_PER_EVENT", False, custom=False),
        ]
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            result = list_key_events(config, property_id="properties/123456")

        admin.list_key_events.assert_called_once_with(parent="properties/123456")
        assert result["total"] == 2
        purchase, sign_up = result["key_events"]
        assert purchase == {
            "event_name": "purchase",
            "counting_method": "ONCE_PER_EVENT",
            "create_time": "2026-09-01T00:00:00+00:00",
            "deletable": False,
            "custom": False,
            "resource_name": "properties/123456/keyEvents/purchase-id",
        }
        assert sign_up["deletable"] is True

    def test_accepts_bare_numeric_id_and_validates(self, config):
        admin = MagicMock()
        admin.list_key_events.return_value = []
        with patch("adloop.ga4.client.get_admin_client", return_value=admin):
            assert list_key_events(config, property_id="123456")["total"] == 0
            assert "numeric" in list_key_events(config, property_id="abc")["error"]
            assert "required" in list_key_events(config, property_id="")["error"]
