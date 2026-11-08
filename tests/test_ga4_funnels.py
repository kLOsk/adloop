"""Tests for GA4 funnels and conversion attribution (Data API v1alpha)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, GA4Config
from adloop.ga4 import attribution, funnels


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ga4=GA4Config(property_id="properties/123456"))


def _sub(dims, mets, rows):
    return SimpleNamespace(
        dimension_headers=[SimpleNamespace(name=d) for d in dims],
        metric_headers=[SimpleNamespace(name=m) for m in mets],
        rows=[
            SimpleNamespace(
                dimension_values=[SimpleNamespace(value=str(v)) for v in r[: len(dims)]],
                metric_values=[SimpleNamespace(value=str(v)) for v in r[len(dims):]],
            )
            for r in rows
        ],
        metadata=SimpleNamespace(sampling_metadatas=[]),
    )


_METS = ["activeUsers", "funnelStepCompletionRate", "funnelStepAbandonments", "funnelStepAbandonmentRate"]


def _funnel_response(table_rows, dims=("funnelStepName",), visualization=None):
    return SimpleNamespace(
        funnel_table=_sub(list(dims), _METS, table_rows),
        funnel_visualization=visualization or _sub([], [], []),
        property_quota=None,
    )


def _run_funnel(config, response, **kwargs):
    client = MagicMock()
    client.run_funnel_report.return_value = response
    with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
        result = funnels.run_ga4_funnel(config, property_id="123456", **kwargs)
    request = client.run_funnel_report.call_args.args[0] if client.run_funnel_report.called else None
    return result, request


SHOP = [
    ["1. view_item", 1000, 0.4, 600, 0.6],
    ["2. add_to_cart", 400, 0.5, 200, 0.5],
    ["3. purchase", 200, 0, 0, 0],
]


class TestFunnel:
    def test_steps_are_computed_with_the_biggest_drop(self, config):
        result, request = _run_funnel(config, _funnel_response(SHOP), steps=["view_item", "add_to_cart", "purchase"])
        assert [s["users"] for s in result["steps"]] == [1000, 400, 200]
        assert result["started"] == 1000 and result["finished"] == 200
        assert result["overall_completion_rate"] == 0.2
        assert result["biggest_drop"] == {
            "after_step": "1. view_item", "before_step": "2. add_to_cart",
            "abandonment_rate": 0.6, "users_lost": 600,
        }
        assert "200 of 1,000 users (20.0%)" in result["insights"][0]
        assert request.funnel.is_open_funnel is False
        assert [s.filter_expression.funnel_event_filter.event_name for s in request.funnel.steps] == [
            "view_item", "add_to_cart", "purchase",
        ]

    def test_page_steps_match_the_url_and_timing_options_are_sent(self, config):
        _, request = _run_funnel(
            config, _funnel_response(SHOP),
            steps=[{"page": "/pricing", "name": "Pricing"}, {"event": "sign_up", "directly_followed": True, "within_minutes": 30}],
            open_funnel=True,
        )
        first, second = request.funnel.steps
        event_filter = first.filter_expression.funnel_event_filter
        assert first.name == "Pricing"
        assert event_filter.event_name == "page_view"
        param = event_filter.funnel_parameter_filter_expression.funnel_parameter_filter
        assert param.event_parameter_name == "page_location"
        assert param.string_filter.value == "/pricing"
        assert second.is_directly_followed_by is True
        assert second.within_duration_from_prior_step.total_seconds() == 1800
        assert request.funnel.is_open_funnel is True

    def test_breakdown_groups_rows_and_keeps_the_total(self, config):
        rows = [
            ["1. view_item", "RESERVED_TOTAL", 1000, 0.4, 600, 0.6],
            ["2. purchase", "RESERVED_TOTAL", 400, 0, 0, 0],
            ["1. view_item", "mobile", 700, 0.3, 490, 0.7],
            ["2. purchase", "mobile", 210, 0, 0, 0],
        ]
        result, request = _run_funnel(
            config, _funnel_response(rows, dims=("funnelStepName", "deviceCategory")),
            steps=["view_item", "purchase"], breakdown="deviceCategory",
        )
        assert result["started"] == 1000
        assert result["breakdown"]["groups"]["mobile"]["overall_completion_rate"] == 0.3
        assert request.funnel_breakdown.breakdown_dimension.name == "deviceCategory"

    def test_next_actions_come_from_the_visualization(self, config):
        # GA4 names the column funnelStepNextAction, not after the dimension asked for.
        visualization = _sub(
            ["funnelStepName", "funnelStepNextAction"], ["activeUsers"],
            [["1. view_item", "scroll", 300], ["1. view_item", "view_item_list", 120], ["1. view_item", "RESERVED_TOTAL", 600]],
        )
        result, request = _run_funnel(
            config, _funnel_response(SHOP, visualization=visualization),
            steps=["view_item", "add_to_cart", "purchase"], next_action="eventName",
        )
        assert result["next_actions"]["by_step"]["1. view_item"] == [
            {"value": "scroll", "users": 300}, {"value": "view_item_list", "users": 120},
        ]
        assert request.funnel_next_action.next_action_dimension.name == "eventName"

    def test_dimension_filter_is_carried_into_the_alpha_request(self, config):
        _, request = _run_funnel(
            config, _funnel_response(SHOP), steps=["view_item", "purchase"],
            dimension_filter=[{"any_of": [{"field": "deviceCategory", "value": "mobile"},
                                          {"field": "deviceCategory", "value": "tablet"}]}],
        )
        values = [e.filter.string_filter.value for e in request.dimension_filter.or_group.expressions]
        assert values == ["mobile", "tablet"]

    def test_nobody_at_step_one_says_what_to_check(self, config):
        result, _ = _run_funnel(config, _funnel_response([["1. viewitem", 0, 0, 0, 0], ["2. purchase", 0, 0, 0, 0]]),
                                steps=["viewitem", "purchase"])
        assert "get_tracking_events" in result["insights"][0]

    @pytest.mark.parametrize("steps, message", [
        (["only_one"], "2 to 10 steps"),
        ([f"e{i}" for i in range(11)], "2 to 10 steps"),
        (["a", {"event": "b", "page": "/x"}], "either 'event'"),
        (["a", {"evnt": "b"}], "unknown key"),
        ([{"event": "a", "directly_followed": True}, "b"], "from the second step"),
        (["a", {"event": "b", "within_minutes": 0}], "within_minutes"),
    ])
    def test_invalid_steps_are_refused_before_sending(self, config, steps, message):
        result, request = _run_funnel(config, _funnel_response(SHOP), steps=steps)
        assert message in " ".join(result["details"])
        assert request is None

    def test_api_errors_come_back_as_a_message(self, config):
        client = MagicMock()
        client.run_funnel_report.side_effect = RuntimeError("400 Field xyz is not a valid dimension")
        with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
            result = funnels.run_ga4_funnel(config, property_id="123456", steps=["a", "b"], breakdown="xyz")
        assert result == {"error": "GA4 funnel API (v1alpha) error: 400 Field xyz is not a valid dimension"}


def _report(dims, mets, rows):
    response = _sub(dims, mets, rows)
    response.row_count = len(rows)
    response.property_quota = None
    return response


def _alpha(*reports, conversions=()):
    client = MagicMock()
    client.run_report.side_effect = list(reports)
    client.get_metadata.return_value = SimpleNamespace(
        conversions=[SimpleNamespace(conversion_action=a, display_name=n) for a, n in conversions],
    )
    return client


class TestAttribution:
    def test_one_model(self, config):
        client = _alpha(_report(["defaultChannelGroup"], ["allConversionsByConversionDate"],
                                [["Paid Search", 40], ["Organic Search", 60]]))
        with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
            result = attribution.run_ga4_attribution(
                config, property_id="123456", metrics=["allConversionsByConversionDate"], attribution_model="last_click",
            )
        request = client.run_report.call_args.args[0]
        assert request.conversion_spec.attribution_model.name == "LAST_CLICK"
        assert list(request.conversion_spec.conversion_actions) == []
        assert [r["defaultChannelGroup"] for r in result["rows"]] == ["Organic Search", "Paid Search"]
        assert result["conversions"] == "all"

    def test_key_event_names_are_resolved_to_conversion_actions(self, config):
        client = _alpha(_report(["defaultChannelGroup"], ["allConversionsByConversionDate"], []),
                        conversions=[("conversionActions/7", "purchase"), ("conversionActions/8", "sign_up")])
        with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
            attribution.run_ga4_attribution(config, property_id="123456", conversions=["purchase"],
                                            metrics=["allConversionsByConversionDate"])
        assert list(client.run_report.call_args.args[0].conversion_spec.conversion_actions) == ["conversionActions/7"]

        with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
            result = attribution.run_ga4_attribution(config, property_id="123456", conversions=["purchased"])
        assert "No conversion action called 'purchased'" in result["error"]
        assert result["conversions"] == ["purchase", "sign_up"]

    def test_compare_shows_where_credit_moves(self, config):
        dims, mets = ["defaultChannelGroup"], ["allConversionsByConversionDate"]
        client = _alpha(
            _report(dims, mets, [["Paid Search", 50], ["Organic Search", 30], ["Direct", 20]]),
            _report(dims, mets, [["Paid Search", 35], ["Organic Search", 30], ["Direct", 35]]),
        )
        with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
            result = attribution.run_ga4_attribution(config, property_id="123456", metrics=mets, attribution_model="COMPARE")
        models = [c.args[0].conversion_spec.attribution_model.name for c in client.run_report.call_args_list]
        assert models == ["DATA_DRIVEN", "LAST_CLICK"]
        top = result["rows"][0]
        assert top["defaultChannelGroup"] in ("Paid Search", "Direct")
        assert abs(top["credit_shift_points"]) == 15.0
        assert "Paid Search gets 15.0 points more of the credit" in " ".join(result["insights"])

    @pytest.mark.parametrize("kwargs, message", [
        ({"dimensions": ["sessionSource"]}, "not available in the attribution report"),
        ({"metrics": ["sessions"]}, "not available in the attribution report"),
        ({"attribution_model": "FIRST_CLICK"}, "DATA_DRIVEN, LAST_CLICK or COMPARE"),
    ])
    def test_validation(self, config, kwargs, message):
        result = attribution.run_ga4_attribution(config, property_id="123456", **kwargs)
        assert message in " ".join(result["details"])

    def test_property_without_the_feature_gets_the_fallback(self, config):
        client = MagicMock()
        client.run_report.side_effect = RuntimeError("403 This property is not eligible")
        with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
            result = attribution.run_ga4_attribution(config, property_id="123456")
        assert result["error"].endswith("403 This property is not eligible")
        assert "run_ga4_report with keyEvents" in result["hint"]


def test_attribution_without_rows_says_so(config):
    client = _alpha(_report(["defaultChannelGroup"], ["allConversionsByConversionDate"], []))
    with patch("adloop.ga4.client.get_data_alpha_client", return_value=client):
        result = attribution.run_ga4_attribution(config, property_id="123456", metrics=["allConversionsByConversionDate"])
    assert "attributed no conversions" in result["insights"][0]
