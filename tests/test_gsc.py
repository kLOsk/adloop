"""Tests for the Google Search Console read tools (PR #20 rework)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, GscConfig
from adloop.gsc import reports


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(gsc=GscConfig(site_url="sc-domain:example.com"))


def _fake_client(query_response=None, sites_response=None):
    client = MagicMock()
    client.sites.return_value.list.return_value.execute.return_value = (
        sites_response or {}
    )
    client.searchanalytics.return_value.query.return_value.execute.return_value = (
        query_response or {}
    )
    return client


class TestListSites:
    def test_lists_sites_with_permission_levels(self, config):
        client = _fake_client(sites_response={"siteEntry": [
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
            {"siteUrl": "https://shop.example.com/"},
        ]})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.list_gsc_sites(config)

        assert result["total"] == 2
        assert result["sites"][0]["permission_level"] == "siteOwner"
        assert result["sites"][1]["permission_level"] == "unknown"
        assert "insights" not in result

    def test_empty_site_list_warns_against_healthy_misreading(self, config):
        """An empty list must not be spun into 'checked and fine' by the
        calling model — regression for the 'all Tag Manager is ok' report
        on an account without GTM/GSC at all."""
        client = _fake_client(sites_response={})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.list_gsc_sites(config)

        assert result["total"] == 0
        assert any("NO Search Console" in i for i in result["insights"])


class TestRunReport:
    def test_uses_configured_site_when_omitted(self, config):
        client = _fake_client(query_response={"rows": []})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            reports.run_gsc_report(config, dimensions=["query"])

        call = client.searchanalytics.return_value.query.call_args
        assert call.kwargs["siteUrl"] == "sc-domain:example.com"

    def test_requires_a_site_when_none_configured(self):
        result = reports.run_gsc_report(AdLoopConfig(), dimensions=["query"])
        assert "site_url is required" in result["error"]

    def test_passes_dimensions_filters_and_caps_limit(self, config):
        client = _fake_client(query_response={"rows": []})
        filters = [{"filters": [{"dimension": "query", "operator": "contains",
                                 "expression": "adloop"}]}]
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            reports.run_gsc_report(
                config,
                dimensions=["query", "page"],
                dimension_filter_groups=filters,
                limit=99_999,
            )

        body = client.searchanalytics.return_value.query.call_args.kwargs["body"]
        assert body["dimensions"] == ["query", "page"]
        assert body["dimensionFilterGroups"] == filters
        assert body["rowLimit"] == 25_000

    def test_rows_are_flattened_with_metrics(self, config):
        client = _fake_client(query_response={"rows": [
            {"keys": ["adloop mcp"], "clicks": 12, "impressions": 340,
             "ctr": 0.0353, "position": 4.2},
        ]})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(config, dimensions=["query"])

        row = result["rows"][0]
        assert row["query"] == "adloop mcp"
        assert row["clicks"] == 12
        assert row["position"] == 4.2


def _body(client):
    return client.searchanalytics.return_value.query.call_args.kwargs["body"]


class TestReportOptions:
    def test_defaults_are_final_auto_and_first_row(self, config):
        client = _fake_client(query_response={"rows": []})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(config)

        body = _body(client)
        assert body["dataState"] == "final"
        assert body["aggregationType"] == "auto"
        assert body["startRow"] == 0
        assert result["data_state"] == "final"
        assert "next_start_row" not in result

    def test_fresh_data_by_page_from_an_offset(self, config):
        client = _fake_client(query_response={
            "rows": [], "responseAggregationType": "byPage",
            "metadata": {"firstIncompleteDate": "2026-10-09"},
        })
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(
                config, data_state="ALL", aggregation_type="bypage",
                start_row=25_000,
            )

        body = _body(client)
        assert body["dataState"] == "all"
        assert body["aggregationType"] == "byPage"
        assert body["startRow"] == 25_000
        assert result["response_aggregation_type"] == "byPage"
        assert result["metadata"]["firstIncompleteDate"] == "2026-10-09"

    def test_a_full_page_points_at_the_next_start_row(self, config):
        client = _fake_client(query_response={"rows": [
            {"keys": [f"q{i}"], "clicks": 1, "impressions": 2, "ctr": 0.5,
             "position": 1.0}
            for i in range(3)
        ]})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(config, limit=3, start_row=6)
        assert result["next_start_row"] == 9

    @pytest.mark.parametrize("kwargs,message", [
        ({"data_state": "fresh"}, "data_state"),
        ({"aggregation_type": "byNewsShowcasePanel"}, "aggregation_type"),
        ({"start_row": -1}, "start_row"),
    ])
    def test_invalid_options_are_refused_before_any_call(self, config, kwargs, message):
        client = _fake_client()
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(config, **kwargs)
        assert message in result["error"]
        client.searchanalytics.assert_not_called()

    def test_by_property_with_page_is_refused(self, config):
        client = _fake_client()
        page_filter = [{"filters": [{"dimension": "page", "operator": "contains",
                                     "expression": "/blog/"}]}]
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            grouped = reports.run_gsc_report(
                config, dimensions=["page"], aggregation_type="byProperty")
            filtered = reports.run_gsc_report(
                config, dimensions=["query"], aggregation_type="byProperty",
                dimension_filter_groups=page_filter)
        assert "byProperty" in grouped["error"]
        assert "byProperty" in filtered["error"]
        client.searchanalytics.assert_not_called()

    def test_hour_dimension_switches_to_hourly_data(self, config):
        client = _fake_client(query_response={"rows": [
            {"keys": ["2026-10-09T13:00:00-07:00"], "clicks": 4,
             "impressions": 40, "ctr": 0.1, "position": 3.0},
        ]})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(config, dimensions=["HOUR"])

        body = _body(client)
        assert body["dimensions"] == ["hour"]
        assert body["dataState"] == "hourly_all"
        assert result["rows"][0]["hour"] == "2026-10-09T13:00:00-07:00"
        assert any("hourly_all" in n for n in result["notes"])

    def test_search_appearance_passes_through_under_one_name(self, config):
        client = _fake_client(query_response={"rows": [
            {"keys": ["RICHCARD"], "clicks": 1, "impressions": 9, "ctr": 0.11,
             "position": 2.0},
        ]})
        with patch("adloop.gsc.client.get_gsc_client", return_value=client):
            result = reports.run_gsc_report(config, dimensions=["search_appearance"])

        assert _body(client)["dimensions"] == ["searchAppearance"]
        assert result["rows"][0]["searchAppearance"] == "RICHCARD"
