"""Reddit read tools against a fake API: shapes, money, joins, insights."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig, RedditConfig
from adloop.reddit import read


@pytest.fixture
def config():
    read.reset_account_meta_cache()
    return AdLoopConfig(reddit=RedditConfig(client_id="app", client_secret="s", ad_account_id="a2_acct"))


def _fake_api(routes: dict):
    """Route (METHOD, path-suffix) → payload for adloop.reddit.client.reddit_request."""
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    def fake(config, method, path, *, params=None, json_body=None, absolute_url=""):
        target = absolute_url or path
        calls.append((method, target, params, json_body))
        for (m, suffix), payload in routes.items():
            if m == method and target.rstrip("/").endswith(suffix):
                return payload if not callable(payload) else payload(params, json_body)
        raise AssertionError(f"unexpected Reddit call {method} {target}")

    return calls, patch("adloop.reddit.client.reddit_request", side_effect=fake)


_ACCOUNT = {"data": {"id": "a2_acct", "name": "Acme", "currency": "EUR", "time_zone_id": "Europe/Berlin"}}
_CAMPAIGNS = {
    "data": [
        {"id": "c1", "name": "Launch", "configured_status": "ACTIVE", "effective_status": "ACTIVE",
         "objective": "CONVERSIONS", "is_campaign_budget_optimization": False, "goal_value": None},
        {"id": "c2", "name": "Old", "configured_status": "ARCHIVED", "effective_status": "ARCHIVED",
         "objective": "CLICKS"},
        {"id": "c3", "name": "Blocked", "configured_status": "ACTIVE", "effective_status": "PENDING_BILLING_INFO",
         "objective": "CLICKS"},
    ]
}


class TestAccounts:
    def test_lists_accounts_across_businesses(self, config):
        calls, ctx = _fake_api({
            ("GET", "me"): {"data": {"reddit_username": "daniel", "id": "m1"}},
            ("GET", "me/businesses"): {"data": [{"id": "b1", "name": "Acme GmbH", "country": "DE"}]},
            ("GET", "businesses/b1/ad_accounts"): {"data": [
                {"id": "a2_acct", "name": "Acme", "currency": "EUR", "time_zone_id": "Europe/Berlin",
                 "type": "SELF_SERVE", "admin_approval": "VALID"},
            ]},
        })
        with ctx:
            result = read.list_reddit_accounts(config)
        assert result["reddit_username"] == "daniel"
        assert result["total"] == 1
        assert result["accounts"][0]["ad_account_id"] == "a2_acct"
        assert result["accounts"][0]["business_name"] == "Acme GmbH"
        assert result["accounts"][0]["currency"] == "EUR"

    def test_no_accounts_yields_note(self, config):
        _, ctx = _fake_api({
            ("GET", "me"): {"data": {"reddit_username": "x"}},
            ("GET", "me/businesses"): {"data": []},
        })
        with ctx:
            result = read.list_reddit_accounts(config)
        assert result["total"] == 0
        assert "Business Manager" in result["note"]


class TestStructure:
    def test_campaigns_hide_archived_and_flag_blocked(self, config):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct/campaigns"): _CAMPAIGNS, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = read.get_reddit_campaigns(config)
        names = [c["name"] for c in result["campaigns"]]
        assert names == ["Launch", "Blocked"]
        assert result["currency"] == "EUR"
        assert any("PENDING_BILLING_INFO" in i for i in result["insights"])

    def test_campaigns_include_archived_when_asked(self, config):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct/campaigns"): _CAMPAIGNS, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = read.get_reddit_campaigns(config, include_archived=True)
        assert result["total_campaigns"] == 3

    def test_ad_groups_convert_micro_budget_and_flag_missing_pixel(self, config):
        calls, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct/ad_groups"): {"data": [
                {"id": "g1", "campaign_id": "c1", "name": "DE", "goal_type": "DAILY_SPEND",
                 "goal_value": 25_000_000, "bid_value": 1_500_000, "conversion_pixel_id": "px1",
                 "targeting": {"geolocations": [{"id": "DE", "name": "Germany"}], "communities": ["r/python"]}},
                {"id": "g2", "campaign_id": "c1", "name": "No pixel", "goal_value": 5_000_000},
            ]},
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
        })
        with ctx:
            result = read.get_reddit_ad_groups(config, campaign_id="c1")
        assert calls[0][2] == {"campaign_id": "c1", "page.size": 100}
        g1 = result["ad_groups"][0]
        assert g1["goal_value"] == 25.0
        assert g1["bid_value"] == 1.5
        assert g1["targeting"]["geolocations"] == ["Germany"]
        assert g1["targeting"]["communities"] == ["r/python"]
        assert any("conversion_pixel_id" in i for i in result["insights"])

    def test_ads_flag_rejections(self, config):
        _, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct/ads"): {"data": [
                {"id": "ad1", "name": "Hero", "effective_status": "REJECTED", "rejection_reason": "BROKEN_URL"},
                {"id": "ad2", "name": "Wait", "effective_status": "PENDING_APPROVAL"},
            ]},
        })
        with ctx:
            result = read.get_reddit_ads(config, ad_group_id="g1")
        assert result["total_ads"] == 2
        assert any("REJECTED" in i and "BROKEN_URL" in i for i in result["insights"])
        assert any("PENDING_APPROVAL" in i for i in result["insights"])


class TestPerformance:
    def _routes(self, report_rows):
        return {
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("GET", "ad_accounts/a2_acct/campaigns"): _CAMPAIGNS,
            ("POST", "ad_accounts/a2_acct/reports"): {"data": {"metrics": report_rows}, "pagination": {}},
        }

    def test_campaign_level_joins_names_and_computes_money(self, config):
        rows = [
            {"campaign_id": "c1", "spend": 12_500_000, "impressions": 10000, "clicks": 250,
             "cpc": 50_000, "ctr": 0.025, "key_conversion_total_count": 5, "conversion_purchase_total_value": 30000},
            {"campaign_id": "c3", "spend": 4_000_000, "impressions": 800, "clicks": 20, "key_conversion_total_count": 0},
        ]
        calls, ctx = _fake_api(self._routes(rows))
        with ctx:
            result = read.get_reddit_performance(
                config, date_range_start="2026-09-01", date_range_end="2026-09-07"
            )
        body = [c for c in calls if c[0] == "POST"][0][3]["data"]
        assert body["breakdowns"] == ["CAMPAIGN_ID"]
        # Account-local days (Europe/Berlin is UTC+2 in September) and
        # ends_at = local midnight OF the end day: Reddit covers
        # [starts_at, ends_at + 24h), verified live 2026-09-11.
        assert body["starts_at"] == "2026-08-31T22:00:00Z"
        assert body["ends_at"] == "2026-09-06T22:00:00Z"
        assert body["time_zone_id"] == "Europe/Berlin"
        assert "SPEND" in body["fields"] and "KEY_CONVERSION_TOTAL_COUNT" in body["fields"]

        top = result["rows"][0]
        assert top["campaign_name"] == "Launch"
        assert top["spend"] == 12.5
        assert top["cpc"] == 0.05
        assert top["conversions"] == 5
        assert top["cpa"] == 2.5
        assert top["roas"] == 24.0  # 300.00 value / 12.50 spend
        assert top["currency"] == "EUR"
        assert result["totals"]["spend"] == 16.5
        assert result["totals"]["conversions"] == 5
        assert any("ZERO key conversions" in i and "Blocked" in i for i in result["insights"])

    def test_compact_mode_trims_rows(self, config):
        rows = [{"campaign_id": f"c{i}", "spend": (20 - i) * 1_000_000, "clicks": 1} for i in range(15)]
        _, ctx = _fake_api(self._routes(rows))
        with ctx:
            result = read.get_reddit_performance(config, compact=True)
        assert result["compact"] is True
        assert "rows" not in result
        assert len(result["rows_top_spend"]) == 10
        assert result["total_rows"] == 15
        assert "Compact mode" in result["note"]

    def test_breakdown_and_level_validation(self, config):
        with pytest.raises(ValueError, match="level must be"):
            read.get_reddit_performance(config, level="keyword")
        with pytest.raises(ValueError, match="breakdown must be"):
            read.get_reddit_performance(config, breakdown="planet")

    def test_ad_level_keeps_three_breakdowns(self, config):
        routes = self._routes([])
        routes[("GET", "ad_accounts/a2_acct/ad_groups")] = {"data": []}
        routes[("GET", "ad_accounts/a2_acct/ads")] = {"data": []}
        calls, ctx = _fake_api(routes)
        with ctx:
            result = read.get_reddit_performance(config, level="ad", breakdown="date")
        body = [c for c in calls if c[0] == "POST"][0][3]["data"]
        assert body["breakdowns"] == ["AD_ID", "AD_GROUP_ID", "DATE"]
        assert any("stabilises" in i for i in result["insights"])

    def test_missing_account_is_a_clear_error(self):
        cfg = AdLoopConfig()
        with pytest.raises(ValueError, match="ad_account_id is required"):
            read.get_reddit_performance(cfg)

    def test_raw_report_requires_fields_and_normalises_money(self, config):
        calls, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "ad_accounts/a2_acct/reports"): {
                "data": {"metrics": [{"date": "2026-09-01", "spend": 1_000_000, "reach": 40,
                                      "conversion_purchase_ecpa": 2_000_000, "conversion_lead_total_value": 1234}]},
            },
        })
        with ctx:
            result = read.run_reddit_report(
                config, fields=["spend", "REACH", "CONVERSION_PURCHASE_ECPA"], breakdowns=["date"],
                filter="campaign_id==c1",
            )
        body = calls[-1][3]["data"]
        assert body["fields"] == ["SPEND", "REACH", "CONVERSION_PURCHASE_ECPA"]
        assert body["breakdowns"] == ["DATE"]
        assert body["filter"] == "campaign_id==c1"
        row = result["rows"][0]
        assert row["spend"] == 1.0
        assert row["reach"] == 40
        assert row["conversion_purchase_ecpa"] == 2.0
        assert row["conversion_lead_total_value"] == 12.34
        with pytest.raises(ValueError, match="fields is required"):
            read.run_reddit_report(config, fields=[])


class TestReportWindow:
    def test_local_days_become_utc_hours(self):
        assert read.report_window("2026-09-08", "2026-09-10", "Europe/Amsterdam") == (
            "2026-09-07T22:00:00Z", "2026-09-09T22:00:00Z", "2026-09-08", "2026-09-10",
        )

    def test_single_day_window(self):
        starts, ends, *_ = read.report_window("2026-09-09", "2026-09-09", "Europe/Amsterdam")
        assert (starts, ends) == ("2026-09-08T22:00:00Z", "2026-09-08T22:00:00Z")

    def test_utc_and_unknown_zone(self):
        assert read.report_window("2026-01-05", "2026-01-06", "UTC")[:2] == (
            "2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z",
        )
        assert read.report_window("2026-01-05", "2026-01-06", "Not/AZone")[:2] == (
            "2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z",
        )

    def test_half_hour_zone_floors_to_the_hour(self):
        # Asia/Kolkata is UTC+5:30; Reddit only takes hour-aligned timestamps.
        assert read.report_window("2026-03-01", "2026-03-01", "Asia/Kolkata")[0] == "2026-02-28T18:00:00Z"

    def test_start_after_end_is_refused(self):
        with pytest.raises(ValueError, match="must not be after"):
            read.report_window("2026-09-10", "2026-09-08")


class TestPixels:
    def test_pixels_report_last_fired_and_cross_check_ad_groups(self, config):
        _, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct/pixels"): {"data": [
                {"id": "px1", "name": "Main"}, {"id": "px2", "name": "Dead"},
            ]},
            ("GET", "pixels/px1/last_fired_at"): {"data": {"page_visit": "2026-09-09T10:00:00Z", "purchase": None, "sign_up": "2026-09-08T00:00:00Z"}},
            ("GET", "pixels/px2/last_fired_at"): {"data": {}},
            ("GET", "ad_accounts/a2_acct/ad_groups"): {"data": [
                {"id": "g1", "name": "Buyers", "conversion_pixel_id": "px1", "optimization_goal": "PURCHASE"},
                {"id": "g2", "name": "Signups", "conversion_pixel_id": "px1", "optimization_goal": "SIGN_UP"},
            ]},
        })
        with ctx:
            result = read.get_reddit_pixels(config)
        assert result["total"] == 2
        main = result["pixels"][0]
        assert main["last_fired_at"]["page_visit"] == "2026-09-09T10:00:00Z"
        assert main["never_fired"] is False
        assert result["pixels"][1]["never_fired"] is True
        assert any("never fired any event" in i and "Dead" in i for i in result["insights"])
        assert any("Buyers" in i and "PURCHASE" in i for i in result["insights"])
        assert not any("Signups" in i for i in result["insights"])

    def test_no_pixels_explains_ad_group_rule(self, config):
        _, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct/pixels"): {"data": []},
            ("GET", "ad_accounts/a2_acct/ad_groups"): {"data": []},
        })
        with ctx:
            result = read.get_reddit_pixels(config)
        assert any("no pixel" in i for i in result["insights"])


class TestTargeting:
    def test_communities_search(self, config):
        calls, ctx = _fake_api({
            ("GET", "targeting/communities/search"): {"data": [
                {"id": "t5_1", "name": "r/python", "subscriber_count": 1000, "categories": ["Tech"]},
            ]},
        })
        with ctx:
            result = read.search_reddit_targeting(config, kind="communities", query="python", limit=5)
        assert calls[0][2] == {"query": "python", "page.size": 5}
        assert result["results"][0]["name"] == "r/python"

    def test_interests_filtered_locally(self, config):
        _, ctx = _fake_api({
            ("GET", "targeting/interests"): {"data": [
                {"id": "i1", "name": "Programming", "category": "Tech"},
                {"id": "i2", "name": "Gardening", "category": "Home"},
            ]},
        })
        with ctx:
            result = read.search_reddit_targeting(config, kind="interests", query="tech")
        assert [r["id"] for r in result["results"]] == ["i1"]

    def test_geolocations_need_country_or_query(self, config):
        with pytest.raises(ValueError, match="country"):
            read.search_reddit_targeting(config, kind="geolocations")

    def test_keyword_suggestions_post_seeds(self, config):
        calls, ctx = _fake_api({
            ("POST", "targeting/keyword_suggestions"): {"data": {"keyword_suggestions": [
                {"keyword": "python hosting", "monthly_views": 10},
                {"keyword": "django", "monthly_views": 500},
            ]}},
        })
        with ctx:
            result = read.search_reddit_targeting(config, kind="keywords", query="python, hosting")
        assert calls[0][3] == {"data": {"seed_keywords": ["python", "hosting"]}}
        assert [r["keyword"] for r in result["results"]] == ["django", "python hosting"]

    def test_languages_use_code(self, config):
        _, ctx = _fake_api({
            ("GET", "targeting/languages"): {"data": [{"code": "DE", "name": "German"}, {"code": "EN", "name": "English"}]},
        })
        with ctx:
            result = read.search_reddit_targeting(config, kind="languages", query="de")
        assert result["results"] == [{"code": "DE", "name": "German"}]

    def test_unknown_kind(self, config):
        with pytest.raises(ValueError, match="kind must be"):
            read.search_reddit_targeting(config, kind="planets")


class TestFundingInstruments:
    def test_lists_instruments_and_profiles(self, config):
        _, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct/funding_instruments"): {"data": [
                {"id": "fi1", "name": "Card", "currency": "EUR", "is_servable": True, "credit_limit": 100_000_000},
            ]},
            ("GET", "ad_accounts/a2_acct/profiles"): {"data": [
                {"id": "p1", "name": "acme_official", "reddit_user_id": "t2_1", "business_id": "b1"},
            ]},
        })
        with ctx:
            result = read.list_reddit_funding_instruments(config)
        assert result["funding_instruments"][0]["credit_limit"] == 100.0
        assert result["profiles"][0]["username"] == "acme_official"


class TestHistoryAndForecast:
    def test_account_history_flattens_and_sorts(self, config):
        calls, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "ad_accounts/a2_acct/history"): {"data": [
                {"change": {"after_value": "ACTIVE", "before_value": "PAUSED", "entity_id": "g1", "entity_name": "DE",
                            "entity_type": "AD_GROUP", "field_name": "configured_status"},
                 "cause": {"reddit_username": "daniel", "changed_at": "2026-09-11T05:39:55+00:00"}},
                {"change": {"after_value": "20000000", "before_value": "10000000", "entity_id": "g1", "entity_name": "DE",
                            "entity_type": "AD_GROUP", "field_name": "goal_value"},
                 "cause": {"reddit_username": "daniel", "changed_at": "2026-09-11T06:00:00+00:00"}},
            ], "pagination": {}},
        })
        with ctx:
            result = read.get_reddit_account_history(
                config, date_range_start="2026-09-10", date_range_end="2026-09-11", entity_type="ad_group", entity_ids=["g1"],
            )
        body = [c for c in calls if c[0] == "POST"][0][3]["data"]
        assert body["start_time"] == "2026-09-09T22:00:00Z"
        assert body["end_time"] == "2026-09-11T22:00:00Z"
        assert body["entity_id_filters"] == [{"entity_type": "AD_GROUP", "entity_ids": ["g1"], "include_child_entities": True}]
        assert result["total"] == 2
        assert result["changes"][0]["field"] == "goal_value"
        assert (result["changes"][0]["before"], result["changes"][0]["after"]) == (10.0, 20.0)
        assert result["changes"][1]["by"] == "daniel"
        with pytest.raises(ValueError, match="entity_ids is required"):
            read.get_reddit_account_history(config, entity_type="campaign")

    def test_estimate_calls_both_forecasting_endpoints(self, config):
        calls, ctx = _fake_api({
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "forecasting/audience_and_delivery_estimates"): {"data": {
                "total_audience_size": 792880400, "target_audience_range": {"min": 118836, "max": 148546},
                "delivery_estimates": {"impressions": {"min": 2844, "max": 5283}, "clicks": {"min": 39, "max": 72}},
            }},
            ("POST", "forecasting/bid_suggestions"): {"data": {
                "min_bid_value": 116000, "bid_suggestion_median": 752455, "bid_suggestion_min": 284529, "bid_suggestion_max": 1220382,
            }},
        })
        with ctx:
            result = read.estimate_reddit_ad_group(
                config, daily_budget=10, bid_type="cpc", bid_value=0.2,
                targeting={"communities": ["PPC"], "geolocations": ["DE"], "languages": ["en"], "gender": None},
            )
        est_body = [c for c in calls if c[1].endswith("audience_and_delivery_estimates")][0][3]["data"]
        assert est_body["objective"] == "CLICKS"
        assert est_body["ad_group_configs"][0]["goal_value"] == 10_000_000
        assert est_body["ad_group_configs"][0]["targeting"]["languages"] == ["EN"]
        bid_body = [c for c in calls if c[1].endswith("bid_suggestions")][0][3]["data"]
        assert bid_body["bid_strategy"] == "MANUAL_BIDDING" and bid_body["currency"] == "EUR"
        assert result["audience"]["target_audience_30_days"] == {"min": 118836, "max": 148546}
        assert result["delivery_estimates"]["clicks"] == {"min": 39, "max": 72}
        assert result["bid_suggestion"]["suggested_median"] == 0.75
        assert result["bid_suggestion"]["minimum_allowed"] == 0.12
        assert any("below Reddit's suggested minimum" in i for i in result["insights"])

    def test_estimate_requires_targeting_and_budget(self, config):
        with pytest.raises(ValueError, match="targeting needs"):
            read.estimate_reddit_ad_group(config, daily_budget=5, targeting={})
        with pytest.raises(ValueError, match="daily_budget or lifetime_budget"):
            read.estimate_reddit_ad_group(config, targeting={"geolocations": ["DE"]})

    def test_community_suggestions_by_names_and_url(self, config):
        calls, ctx = _fake_api({
            ("GET", "targeting/communities/suggestions"): {"data": [
                {"id": "t5_1", "name": "digital_marketing", "subscriber_count": 374657, "categories": ["Education"]},
            ]},
        })
        with ctx:
            result = read.search_reddit_targeting(
                config, kind="community_suggestions", query="r/PPC, googleads", website_url="https://getadloop.com", limit=5,
            )
        assert calls[0][2] == {"page.size": 5, "names": "PPC,googleads", "website_url": "https://getadloop.com"}
        assert result["results"][0]["name"] == "digital_marketing"
        with pytest.raises(ValueError, match="community_suggestions needs"):
            read.search_reddit_targeting(config, kind="community_suggestions")
