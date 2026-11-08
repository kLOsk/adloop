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


class TestFilterGroups:
    def test_any_of_becomes_an_or_group(self, config):
        _, request = _run(
            config, dimensions=["sessionSource"], metrics=["sessions"],
            dimension_filter=[{"any_of": [
                {"field": "sessionSource", "value": "google"},
                {"field": "sessionSource", "value": "bing"},
            ]}],
        )
        group = request.dimension_filter.or_group.expressions
        assert [e.filter.string_filter.value for e in group] == ["google", "bing"]

    def test_groups_nest_and_negate_next_to_plain_conditions(self, config):
        _, request = _run(
            config, dimensions=["country", "deviceCategory"], metrics=["sessions"],
            dimension_filter=[
                {"field": "country", "value": "Germany"},
                {"not": True, "any_of": [
                    {"field": "deviceCategory", "value": "tablet"},
                    {"all_of": [
                        {"field": "deviceCategory", "value": "mobile"},
                        {"field": "country", "op": "CONTAINS", "value": "x"},
                    ]},
                ]},
            ],
        )
        top = request.dimension_filter.and_group.expressions
        assert top[0].filter.field_name == "country"
        negated = top[1].not_expression.or_group.expressions
        assert negated[0].filter.string_filter.value == "tablet"
        assert len(negated[1].and_group.expressions) == 2

    def test_single_item_group_is_just_the_condition(self, config):
        _, request = _run(
            config, metrics=["sessions"],
            metric_filter=[{"any_of": [{"field": "sessions", "op": ">", "value": 5}]}],
        )
        assert request.metric_filter.filter.field_name == "sessions"

    @pytest.mark.parametrize("bad, message", [
        ({"any_of": [], }, "non-empty list"),
        ({"any_of": [{"field": "country", "value": "DE"}], "all_of": []}, "not both"),
        ({"any_of": [{"field": "country", "value": "DE"}], "or": True}, "unknown key"),
        ({"any_of": [{"any_of": [{"any_of": [{"any_of": [{"field": "country", "value": "DE"}]}]}]}]}, "at most 3 levels"),
        ({"any_of": [{"field": "country", "op": "NOPE", "value": "DE"}]}, "dimension_filter[0].any_of[0]"),
    ])
    def test_invalid_groups_are_refused_before_sending(self, config, bad, message):
        result, request = _run(config, dimensions=["country"], metrics=["sessions"], dimension_filter=[bad])
        assert result["error"] == "Validation failed"
        assert message in " ".join(result["details"])
        assert request is None


class TestDateRangesAndCurrency:
    def test_named_ranges_are_sent_and_listed(self, config):
        result, request = _run(
            config, dimensions=["country"], metrics=["sessions"],
            date_ranges=[
                {"start": "2026-10-01", "end": "2026-10-31", "name": "october"},
                {"start": "2025-10-01", "end": "2025-10-31"},
            ],
        )
        assert [(r.name, r.start_date) for r in request.date_ranges] == [
            ("october", "2026-10-01"), ("range_2", "2025-10-01"),
        ]
        assert result["date_ranges"] == {
            "october": {"start": "2026-10-01", "end": "2026-10-31"},
            "range_2": {"start": "2025-10-01", "end": "2025-10-31"},
        }
        assert result["date_range"] == {"start": "2026-10-01", "end": "2026-10-31"}

    @pytest.mark.parametrize("ranges, extra, message", [
        ([{"start": "1daysAgo", "end": "today"}] * 5, {}, "at most 4"),
        ([{"start": "1daysAgo", "end": "today", "name": "date_range_x"}], {}, "reserved"),
        ([{"start": "1daysAgo", "end": "today", "name": "a"}, {"start": "2daysAgo", "end": "today", "name": "a"}], {}, "different name"),
        ([{"start": "1daysAgo"}], {}, "needs both"),
        ([{"start": "1daysAgo", "end": "today"}], {"compare_start": "9daysAgo", "compare_end": "8daysAgo"}, "not both"),
    ])
    def test_invalid_ranges_are_refused(self, config, ranges, extra, message):
        result, request = _run(config, metrics=["sessions"], date_ranges=ranges, **extra)
        assert message in " ".join(result["details"])
        assert request is None

    def test_currency_code_is_sent_and_checked(self, config):
        result, request = _run(config, metrics=["totalRevenue"], currency_code="eur")
        assert request.currency_code == "EUR"
        assert result["currency_code"] == "EUR"

        result, request = _run(config, metrics=["totalRevenue"], currency_code="EURO")
        assert "three-letter" in " ".join(result["details"])

    def test_every_report_asks_for_the_quota(self, config):
        _, request = _run(config, metrics=["sessions"])
        assert request.return_property_quota is True


class TestDataQualityInsights:
    def _with(self, metadata=None, quota=None):
        response = _response(["country"], ["sessions"], [["DE", "10"]])
        response.metadata = metadata
        response.property_quota = quota
        return response

    def test_clean_report_has_no_insights(self, config):
        meta = SimpleNamespace(subject_to_thresholding=False, data_loss_from_other_row=False,
                               sampling_metadatas=[], empty_reason="")
        result, _ = _run(config, self._with(meta), dimensions=["country"], metrics=["sessions"])
        assert "insights" not in result

    def test_thresholding_other_row_and_sampling_are_named(self, config):
        meta = SimpleNamespace(
            subject_to_thresholding=True, data_loss_from_other_row=True, empty_reason="",
            sampling_metadatas=[SimpleNamespace(samples_read_count=250_000, sampling_space_size=1_000_000)],
        )
        result, _ = _run(config, self._with(meta), dimensions=["country"], metrics=["sessions"])
        text = " ".join(result["insights"])
        assert "thresholds" in text
        assert '"(other)"' in text
        assert "250,000 of 1,000,000 events read (25%)" in text

    def test_low_quota_is_flagged(self, config):
        quota = SimpleNamespace(
            tokens_per_day=SimpleNamespace(consumed=190_000, remaining=10_000),
            tokens_per_hour=SimpleNamespace(consumed=10, remaining=39_990),
            tokens_per_project_per_hour=None,
        )
        result, _ = _run(config, self._with(None, quota), dimensions=["country"], metrics=["sessions"])
        assert result["insights"] == [
            "This property is close to its GA4 API quota (daily: 10,000 of 200,000 tokens left). "
            "Fewer dimensions, shorter date ranges and smaller limits use fewer tokens."
        ]


def _run_realtime(config, response=None, **kwargs):
    client = MagicMock()
    client.run_realtime_report.return_value = response or _response([], [], [])
    with patch("adloop.ga4.client.get_data_client", return_value=client):
        result = reports.run_realtime_report(config, property_id="properties/123456", **kwargs)
    request = client.run_realtime_report.call_args.args[0] if client.run_realtime_report.called else None
    return result, request


class TestRealtime:
    def test_plain_call_keeps_the_old_shape(self, config):
        response = _response(["country"], ["activeUsers"], [["DE", "3"]])
        result, request = _run_realtime(config, response, dimensions=["country"])
        assert [m.name for m in request.metrics] == ["activeUsers"]
        assert not request.minute_ranges and not request.order_bys and request.limit == 0
        assert result["rows"] == [{"country": "DE", "activeUsers": "3"}]
        assert "insights" not in result

    def test_filter_order_limit_and_minute_ranges_are_sent(self, config):
        result, request = _run_realtime(
            config, dimensions=["eventName"], metrics=["eventCount"],
            dimension_filter=[{"field": "eventName", "value": "purchase"}],
            order_by=[{"field": "eventCount", "desc": True}], limit=5,
            minute_ranges=[
                {"start_minutes_ago": 4, "end_minutes_ago": 0, "name": "last_5"},
                {"start_minutes_ago": 29, "end_minutes_ago": 5},
            ],
        )
        assert request.dimension_filter.filter.string_filter.value == "purchase"
        assert request.order_bys[0].metric.metric_name == "eventCount"
        assert request.limit == 5
        assert [(r.start_minutes_ago, r.end_minutes_ago, r.name) for r in request.minute_ranges] == [
            (4, 0, "last_5"), (29, 5, ""),
        ]
        assert result["minute_ranges"][0] == {"start_minutes_ago": 4, "end_minutes_ago": 0, "name": "last_5"}

    def test_empty_filtered_result_explains_the_usual_causes(self, config):
        result, _ = _run_realtime(
            config, dimensions=["eventName"], metrics=["eventCount"],
            dimension_filter=[{"field": "eventName", "value": "purchase"}],
        )
        assert "get_tracking_events" in result["insights"][0]
        assert "cookies" in result["insights"][0]

    @pytest.mark.parametrize("ranges, message", [
        ([{"start_minutes_ago": 3, "end_minutes_ago": 5}], "end_minutes_ago <= start_minutes_ago"),
        ([{"start_minutes_ago": 60}], "<= 59"),
        ([{"start_minutes_ago": 1}] * 3, "at most 2"),
        ([{"start_minutes_ago": "5"}], "whole numbers"),
    ])
    def test_invalid_minute_ranges_are_refused(self, config, ranges, message):
        result, request = _run_realtime(config, minute_ranges=ranges)
        assert message in " ".join(result["details"])
        assert request is None


def test_paging_with_several_ranges_counts_dimension_combinations(config):
    # GA4: limit 2 returns 2 channels x 2 ranges = 4 rows; row_count counts the 5 channels.
    response = _response(
        ["sessionDefaultChannelGroup", "dateRange"], ["sessions"],
        [["Direct", "recent", "9"], ["Direct", "before", "5"], ["Organic", "recent", "4"], ["Organic", "before", "3"]],
        row_count=5,
    )
    result, _ = _run(
        config, response, dimensions=["sessionDefaultChannelGroup"], metrics=["sessions"], limit=2,
        date_ranges=[{"start": "28daysAgo", "end": "today", "name": "recent"},
                     {"start": "56daysAgo", "end": "29daysAgo", "name": "before"}],
    )
    assert result["next_offset"] == 2
