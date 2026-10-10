"""Tests for read-tool compact mode (issue #45) and audience insights (issue #44)."""

from __future__ import annotations

import pytest

from adloop.ads import read
from adloop.config import AdLoopConfig, AdsConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="123-456-7890"))


@pytest.fixture(autouse=True)
def fixed_currency(monkeypatch):
    monkeypatch.setattr(read, "get_currency_code", lambda *_a, **_k: "EUR")


def _patch_rows(monkeypatch, rows):
    import adloop.ads.gaql as gaql

    monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: rows)


def _campaign_row(
    name, *, cost_micros=0, conversions=0, status="ENABLED",
    channel="SEARCH", clicks=0, impressions=0,
):
    return {
        "campaign.id": hash(name) % 10_000,
        "campaign.name": name,
        "campaign.status": status,
        "campaign.advertising_channel_type": channel,
        "metrics.impressions": impressions,
        "metrics.clicks": clicks,
        "metrics.cost_micros": cost_micros,
        "metrics.conversions": conversions,
    }


class TestAudiencePerformanceInsights:
    def test_search_campaigns_trigger_custom_segment_warning(
        self, config, monkeypatch
    ):
        _patch_rows(monkeypatch, [
            {
                "campaign.name": "Brand DE",
                "campaign.advertising_channel_type": "SEARCH",
                "ad_group_criterion.type": "USER_LIST",
                "metrics.cost_micros": 1_000_000,
            },
            {
                "campaign.name": "Display Reach",
                "campaign.advertising_channel_type": "DISPLAY",
                "ad_group_criterion.type": "CUSTOM_AUDIENCE",
                "metrics.cost_micros": 2_000_000,
            },
        ])

        result = read.get_audience_performance(config, customer_id="123")

        warning = [i for i in result["insights"] if "CANNOT be attached" in i]
        assert len(warning) == 1
        assert "Brand DE" in warning[0]
        assert "Display Reach" not in warning[0]

    def test_no_search_campaigns_no_warning(self, config, monkeypatch):
        _patch_rows(monkeypatch, [
            {
                "campaign.name": "Display Reach",
                "campaign.advertising_channel_type": "DISPLAY",
                "metrics.cost_micros": 2_000_000,
            },
        ])

        result = read.get_audience_performance(config, customer_id="123")

        assert not any("CANNOT be attached" in i for i in result["insights"])

    def test_empty_result_mentions_custom_segment_search_incompatibility(
        self, config, monkeypatch
    ):
        _patch_rows(monkeypatch, [])

        result = read.get_audience_performance(config, customer_id="123")

        assert any("custom_audience" in i for i in result["insights"])


class TestCampaignPerformanceCompact:
    def test_default_returns_full_rows_unchanged(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_campaign_row("A", cost_micros=5_000_000)])

        result = read.get_campaign_performance(config, customer_id="123")

        assert "campaigns" in result and "compact" not in result

    def test_compact_aggregates_and_flags_zero_conversion_spend(
        self, config, monkeypatch
    ):
        rows = [
            _campaign_row(
                f"C{i}", cost_micros=(20 - i) * 1_000_000,
                conversions=(2 if i % 2 else 0), clicks=100, impressions=1000,
            )
            for i in range(20)
        ]
        _patch_rows(monkeypatch, rows)

        result = read.get_campaign_performance(
            config, customer_id="123", compact=True
        )

        assert result["compact"] is True
        assert result["total_campaigns"] == 20
        assert len(result["campaigns_top_spend"]) == 10
        assert result["totals"]["cost"] == 210.0
        assert result["totals"]["currency"] == "EUR"
        assert result["by_status"] == {"ENABLED": 20}
        # 10 even-indexed campaigns have zero conversions and nonzero spend
        assert any("ZERO conversions" in i for i in result["insights"])
        assert len(result["zero_conversion_spenders"]) == 5
        assert "compact=false" in result["note"]


class TestKeywordPerformanceCompact:
    def test_compact_surfaces_low_qs_and_zero_conv(self, config, monkeypatch):
        rows = [
            {
                "ad_group_criterion.keyword.text": f"kw{i}",
                "ad_group_criterion.keyword.match_type": "EXACT" if i % 2 else "PHRASE",
                "ad_group_criterion.quality_info.quality_score": 3 if i < 4 else 8,
                "metrics.cost_micros": (30 - i) * 1_000_000,
                "metrics.clicks": 10,
                "metrics.impressions": 100,
                "metrics.conversions": 0 if i < 6 else 1,
            }
            for i in range(30)
        ]
        _patch_rows(monkeypatch, rows)

        result = read.get_keyword_performance(
            config, customer_id="123", compact=True
        )

        assert result["compact"] is True
        assert result["total_keywords"] == 30
        assert len(result["keywords_top_spend"]) == 10
        assert len(result["low_quality_score"]) == 4
        assert all(k["quality_score"] == 3 for k in result["low_quality_score"])
        assert len(result["zero_conversion_spenders"]) == 6
        assert result["by_match_type"] == {"PHRASE": 15, "EXACT": 15}
        assert any("quality score" in i for i in result["insights"])

    def test_compact_ignores_missing_quality_scores(self, config, monkeypatch):
        _patch_rows(monkeypatch, [
            {
                "ad_group_criterion.keyword.text": "kw",
                "ad_group_criterion.keyword.match_type": "EXACT",
                "ad_group_criterion.quality_info.quality_score": None,
                "metrics.cost_micros": 1_000_000,
                "metrics.conversions": 1,
            }
        ])

        result = read.get_keyword_performance(
            config, customer_id="123", compact=True
        )

        assert result["low_quality_score"] == []

    def test_compact_treats_zero_quality_score_as_unrated(
        self, config, monkeypatch
    ):
        # Google never assigns a real score of 0; 0 and null both mean "too
        # few impressions to rate". Counting them as "< 5" reported a normal
        # unrated long tail as an account-wide relevance crisis (issue #62).
        _patch_rows(monkeypatch, [
            {
                "ad_group_criterion.keyword.text": f"kw{i}",
                "ad_group_criterion.keyword.match_type": "EXACT",
                "ad_group_criterion.quality_info.quality_score": score,
                "metrics.cost_micros": 1_000_000,
                "metrics.conversions": 1,
            }
            for i, score in enumerate([0, 0, None, 3, 7])
        ])

        result = read.get_keyword_performance(
            config, customer_id="123", compact=True
        )

        # Only the genuine 3 counts as low quality.
        assert len(result["low_quality_score"]) == 1
        assert result["low_quality_score"][0]["quality_score"] == 3
        assert result["unrated_quality_score"] == 3
        assert any("no quality score yet" in i for i in result["insights"])


class TestSearchTermsCompact:
    def test_compact_ranks_waste_by_cost_and_converters_by_conversions(
        self, config, monkeypatch
    ):
        rows = [
            # waste: 5+ clicks, zero conversions
            {"search_term_view.search_term": "cheap junk", "campaign.name": "C",
             "metrics.clicks": 9, "metrics.cost_micros": 3_000_000,
             "metrics.conversions": 0},
            {"search_term_view.search_term": "expensive junk", "campaign.name": "C",
             "metrics.clicks": 5, "metrics.cost_micros": 9_000_000,
             "metrics.conversions": 0},
            # below click threshold — not waste
            {"search_term_view.search_term": "rare miss", "campaign.name": "C",
             "metrics.clicks": 2, "metrics.cost_micros": 1_000_000,
             "metrics.conversions": 0},
            # converter
            {"search_term_view.search_term": "buy adloop", "campaign.name": "C",
             "metrics.clicks": 20, "metrics.cost_micros": 4_000_000,
             "metrics.conversions": 3},
        ]
        _patch_rows(monkeypatch, rows)

        result = read.get_search_terms(config, customer_id="123", compact=True)

        assert result["compact"] is True
        waste_terms = [w["search_term"] for w in result["waste_candidates"]]
        assert waste_terms == ["expensive junk", "cheap junk"]
        assert result["top_converters"][0]["search_term"] == "buy adloop"
        assert any("negative-keyword" in i for i in result["insights"])


class TestAdPerformanceCompact:
    def _ad_row(self, ad_id, group, *, headlines=10, descriptions=4,
                status="ENABLED", ad_type="RESPONSIVE_SEARCH_AD", cost=1):
        return {
            "campaign.name": "C",
            "ad_group.id": group,
            "ad_group.name": f"AG {group}",
            "ad_group_ad.ad.id": ad_id,
            "ad_group_ad.ad.type": ad_type,
            "ad_group_ad.status": status,
            "ad_group_ad.ad.responsive_search_ad.headlines": [
                {"text": f"H{i}"} for i in range(headlines)
            ],
            "ad_group_ad.ad.responsive_search_ad.descriptions": [
                {"text": f"D{i}"} for i in range(descriptions)
            ],
            "ad_group_ad.ad.final_urls": ["https://example.com"],
            "metrics.cost_micros": cost * 1_000_000,
            "metrics.clicks": 5,
            "metrics.impressions": 50,
            "metrics.conversions": 0,
        }

    def test_compact_strips_asset_lists_and_finds_thin_spots(
        self, config, monkeypatch
    ):
        rows = [
            self._ad_row(1, "g1", headlines=10, descriptions=4, cost=9),
            self._ad_row(2, "g1", headlines=4, descriptions=2, cost=5),  # thin RSA
            self._ad_row(3, "g2", headlines=12, descriptions=4, cost=2),  # single-ad group
        ]
        _patch_rows(monkeypatch, rows)

        result = read.get_ad_performance(config, customer_id="123", compact=True)

        assert result["compact"] is True
        top = result["ads_top_spend"][0]
        assert "ad_group_ad.ad.responsive_search_ad.headlines" not in top
        assert top["headline_count"] == 10
        assert top["description_count"] == 4

        assert len(result["incomplete_rsas"]) == 1
        assert result["incomplete_rsas"][0]["ad_id"] == 2

        single = [g["ad_group"] for g in result["single_ad_ad_groups"]]
        assert single == ["AG g2"]
        assert any("below best practice" in i for i in result["insights"])
        assert any("one enabled ad" in i for i in result["insights"])


class TestCompactKeepsLandingPages:
    """Issue #63: landing-page work needs the URLs compact mode used to drop."""

    def test_compact_rows_and_summary_carry_final_urls(self, config, monkeypatch):
        def ad(ad_id, url, headlines=10):
            return {
                "ad_group.id": "g1",
                "ad_group.name": "AG",
                "ad_group_ad.ad.id": ad_id,
                "ad_group_ad.ad.type": "RESPONSIVE_SEARCH_AD",
                "ad_group_ad.status": "ENABLED",
                "ad_group_ad.ad.responsive_search_ad.headlines": [{"text": "H"}] * headlines,
                "ad_group_ad.ad.responsive_search_ad.descriptions": [{"text": "D"}] * 4,
                "ad_group_ad.ad.final_urls": [url],
                "metrics.cost_micros": 1_000_000,
            }

        rows = [ad(1, "https://a.example/"), ad(2, "https://a.example/"), ad(3, "https://b.example/", 4)]
        _patch_rows(monkeypatch, rows)

        result = read.get_ad_performance(config, customer_id="123", compact=True)

        assert result["ads_top_spend"][0]["ad_group_ad.ad.final_urls"] == ["https://a.example/"]
        assert result["incomplete_rsas"][0]["final_urls"] == ["https://b.example/"]
        assert result["landing_pages"] == [
            {"final_url": "https://a.example/", "ads": 2},
            {"final_url": "https://b.example/", "ads": 1},
        ]


class TestNegativeKeywordsAreKeywordsOnly:
    """Issue #61: other negative campaign criteria came back as blank rows."""

    def test_query_filters_to_keyword_criteria(self, config, monkeypatch):
        import adloop.ads.gaql as gaql

        seen = {}

        def capture(_config, _customer_id, query):
            seen["query"] = query
            return []

        monkeypatch.setattr(gaql, "execute_query", capture)
        read.get_negative_keywords(config, customer_id="123")

        assert "campaign_criterion.type = 'KEYWORD'" in seen["query"]


class TestCompactTotalsDisclosure:
    def test_search_term_totals_declare_themselves_partial_at_the_limit(
        self, config, monkeypatch
    ):
        # The query behind search terms is LIMIT 200, so totals over the rows
        # returned are not account totals. Labelling them "totals" without
        # saying so silently under-reports cost and conversions (issue #64).
        _patch_rows(monkeypatch, [
            {
                "search_term_view.search_term": f"term {i}",
                "campaign.name": "C",
                "metrics.clicks": 1,
                "metrics.cost_micros": 1_000_000,
                "metrics.conversions": 0,
                "metrics.impressions": 10,
            }
            for i in range(200)
        ])

        result = read.get_search_terms(config, customer_id="123", compact=True)

        assert result["totals"]["partial"] is True
        assert result["totals"]["rows_counted"] == 200
        assert "not the whole account" in result["totals"]["partial_reason"]

    def test_totals_below_the_limit_are_not_flagged(self, config, monkeypatch):
        _patch_rows(monkeypatch, [
            {
                "search_term_view.search_term": "term",
                "campaign.name": "C",
                "metrics.clicks": 1,
                "metrics.cost_micros": 1_000_000,
                "metrics.conversions": 0,
                "metrics.impressions": 10,
            }
        ])

        result = read.get_search_terms(config, customer_id="123", compact=True)

        assert "partial" not in result["totals"]


class TestSearchTermsDateWindow:
    """``search_term_view`` needs an explicit date segment; the range wins."""

    def _capture(self, monkeypatch, rows=None):
        from adloop.ads import gaql

        seen = {}

        def _query(_config, _cid, query):
            seen["query"] = query
            return list(rows or [])

        monkeypatch.setattr(gaql, "execute_query", _query)
        return seen

    def test_the_default_window_is_the_last_thirty_days(self, config, monkeypatch):
        seen = self._capture(monkeypatch)

        read.get_search_terms(config, customer_id="123")

        assert "segments.date DURING LAST_30_DAYS" in seen["query"]

    def test_a_given_range_replaces_the_default(self, config, monkeypatch):
        seen = self._capture(monkeypatch)

        read.get_search_terms(
            config, customer_id="123",
            date_range_start="2026-03-01", date_range_end="2026-03-31",
        )

        assert "segments.date BETWEEN '2026-03-01' AND '2026-03-31'" in seen["query"]
        assert "LAST_30_DAYS" not in seen["query"]


def _routing_query(monkeypatch, responder):
    """Patch execute_query with a function of the query text; record queries."""
    import adloop.ads.gaql as gaql

    seen: list[str] = []

    def _query(_config, _cid, query):
        seen.append(query)
        return responder(query)

    monkeypatch.setattr(gaql, "execute_query", _query)
    return seen


class TestCampaignImpressionShare:
    _MAIN = [
        {
            "campaign.id": 1, "campaign.name": "Search Converting",
            "campaign.advertising_channel_type": "SEARCH",
            "metrics.impressions": 1000, "metrics.clicks": 100,
            "metrics.cost_micros": 50_000_000, "metrics.conversions": 5,
        },
        {
            "campaign.id": 2, "campaign.name": "Search Rank Limited",
            "campaign.advertising_channel_type": "SEARCH",
            "metrics.impressions": 800, "metrics.clicks": 40,
            "metrics.cost_micros": 20_000_000, "metrics.conversions": 2,
        },
        {
            "campaign.id": 3, "campaign.name": "Display Reach",
            "campaign.advertising_channel_type": "DISPLAY",
            "metrics.impressions": 9000, "metrics.clicks": 30,
            "metrics.cost_micros": 10_000_000, "metrics.conversions": 1,
        },
        {
            "campaign.id": 4, "campaign.name": "Search Idle",
            "campaign.advertising_channel_type": "SEARCH",
            "metrics.impressions": 0, "metrics.clicks": 0,
            "metrics.cost_micros": 0, "metrics.conversions": 0,
        },
    ]
    _SHARE = [
        {
            "campaign.id": 1, "metrics.impressions": 1000,
            "metrics.search_impression_share": 0.41234,
            "metrics.search_budget_lost_impression_share": 0.35,
            "metrics.search_rank_lost_impression_share": 0.23766,
        },
        {
            "campaign.id": 2, "metrics.impressions": 800,
            "metrics.search_impression_share": 0.0999,
            "metrics.search_budget_lost_impression_share": 0.0,
            "metrics.search_rank_lost_impression_share": 0.9001,
        },
        # Unset proto3 doubles read back as 0.0 for an idle campaign.
        {
            "campaign.id": 4, "metrics.impressions": 0,
            "metrics.search_impression_share": 0.0,
            "metrics.search_budget_lost_impression_share": 0.0,
            "metrics.search_rank_lost_impression_share": 0.0,
        },
    ]

    def _respond(self, query):
        if "search_impression_share" in query:
            return [dict(r) for r in self._SHARE]
        return [dict(r) for r in self._MAIN]

    def test_share_runs_as_its_own_query_limited_to_search_channels(
        self, config, monkeypatch
    ):
        seen = _routing_query(monkeypatch, self._respond)

        read.get_campaign_performance(config, customer_id="123")

        main, share = seen
        assert "search_impression_share" not in main
        assert "metrics.search_budget_lost_impression_share" in share
        assert "metrics.search_rank_lost_impression_share" in share
        assert "advertising_channel_type IN ('SEARCH', 'SHOPPING')" in share
        assert "segments.date DURING LAST_30_DAYS" in share

    def test_share_is_merged_as_fraction_and_percentage(self, config, monkeypatch):
        _routing_query(monkeypatch, self._respond)

        result = read.get_campaign_performance(config, customer_id="123")
        rows = {r["campaign.id"]: r for r in result["campaigns"]}

        assert rows[1]["metrics.search_impression_share"] == 0.4123
        assert rows[1]["metrics.search_impression_share_pct"] == 41.2
        assert rows[1]["metrics.search_budget_lost_impression_share_pct"] == 35.0
        assert rows[2]["metrics.search_rank_lost_impression_share"] == 0.9001
        assert "0.9001" in result["impression_share_note"]

    def test_share_is_null_where_it_does_not_apply(self, config, monkeypatch):
        _routing_query(monkeypatch, self._respond)

        result = read.get_campaign_performance(config, customer_id="123")
        rows = {r["campaign.id"]: r for r in result["campaigns"]}

        for cid in (3, 4):  # Display campaign; Search campaign without impressions
            assert rows[cid]["metrics.search_impression_share"] is None
            assert rows[cid]["metrics.search_impression_share_pct"] is None
            assert rows[cid]["metrics.search_budget_lost_impression_share"] is None
        assert len(result["campaigns"]) == 4

    def test_budget_limited_converters_are_flagged(self, config, monkeypatch):
        _routing_query(monkeypatch, self._respond)

        full = read.get_campaign_performance(config, customer_id="123")
        compact = read.get_campaign_performance(
            config, customer_id="123", compact=True
        )

        for result in (full, compact):
            flagged = result["budget_limited_converters"]
            assert [f["name"] for f in flagged] == ["Search Converting"]
            assert flagged[0]["search_budget_lost_impression_share_pct"] == 35.0
            insight = [i for i in result["insights"] if "to budget" in i]
            assert len(insight) == 1 and "Search Converting" in insight[0]

    def test_non_converting_budget_loss_is_not_flagged(self, config, monkeypatch):
        main = [dict(self._MAIN[0], **{"metrics.conversions": 0})]
        _routing_query(
            monkeypatch,
            lambda q: [dict(self._SHARE[0])] if "search_impression_share" in q else main,
        )

        result = read.get_campaign_performance(config, customer_id="123")

        assert "budget_limited_converters" not in result
        assert "insights" not in result

    def test_share_query_failure_keeps_the_report(self, config, monkeypatch):
        def respond(query):
            if "search_impression_share" in query:
                raise RuntimeError("PROHIBITED_FIELD_COMBINATION")
            return [dict(r) for r in self._MAIN]

        _routing_query(monkeypatch, respond)

        result = read.get_campaign_performance(config, customer_id="123")

        assert result["total_campaigns"] == 4
        assert "PROHIBITED_FIELD_COMBINATION" in result["impression_share_error"]
        assert all(
            r["metrics.search_impression_share"] is None for r in result["campaigns"]
        )


class TestAdPolicyStatus:
    def _ad(self, ad_id, approval, *, cost=1, topics=(), review="REVIEWED"):
        return {
            "campaign.name": "C",
            "ad_group.id": "g1",
            "ad_group.name": "AG",
            "ad_group_ad.ad.id": ad_id,
            "ad_group_ad.ad.type": "RESPONSIVE_SEARCH_AD",
            "ad_group_ad.status": "ENABLED",
            "ad_group_ad.ad.responsive_search_ad.headlines": [{"text": "H"}] * 10,
            "ad_group_ad.ad.responsive_search_ad.descriptions": [{"text": "D"}] * 4,
            "ad_group_ad.ad.final_urls": ["https://example.com"],
            "ad_group_ad.policy_summary.approval_status": approval,
            "ad_group_ad.policy_summary.review_status": review,
            "ad_group_ad.policy_summary.policy_topic_entries": [
                {"topic": t, "type_": "LIMITED", "evidences": [{"text_list": {"texts": ["x" * 200]}}]}
                for t in topics
            ],
            "metrics.cost_micros": cost * 1_000_000,
            "metrics.clicks": 1,
            "metrics.impressions": 10,
            "metrics.conversions": 0,
        }

    def _rows(self):
        return [
            self._ad(1, "DISAPPROVED", cost=12, topics=["DESTINATION_NOT_WORKING"]),
            self._ad(2, "APPROVED_LIMITED", cost=8,
                     topics=["TRADEMARKS_IN_AD_TEXT", "TRADEMARKS_IN_AD_TEXT"]),
            self._ad(3, "APPROVED", cost=5),
            self._ad(4, "DISAPPROVED", cost=0, topics=["MISLEADING_CONTENT"],
                     review="UNDER_APPEAL"),
        ]

    def test_query_selects_policy_summary(self, config, monkeypatch):
        seen = _routing_query(monkeypatch, lambda _q: [])

        read.get_ad_performance(config, customer_id="123")

        for field in (
            "ad_group_ad.policy_summary.approval_status",
            "ad_group_ad.policy_summary.review_status",
            "ad_group_ad.policy_summary.policy_topic_entries",
        ):
            assert field in seen[0]

    def test_rows_carry_topic_names_instead_of_raw_entries(self, config, monkeypatch):
        _patch_rows(monkeypatch, self._rows())

        result = read.get_ad_performance(config, customer_id="123")
        ad = {a["ad_group_ad.ad.id"]: a for a in result["ads"]}

        assert "ad_group_ad.policy_summary.policy_topic_entries" not in ad[2]
        assert ad[2]["ad_group_ad.policy_summary.policy_topics"] == [
            "TRADEMARKS_IN_AD_TEXT"
        ]
        assert ad[3]["ad_group_ad.policy_summary.policy_topics"] == []
        assert ad[1]["ad_group_ad.policy_summary.approval_status"] == "DISAPPROVED"

    def test_full_mode_lists_policy_issues_with_spend(self, config, monkeypatch):
        _patch_rows(monkeypatch, self._rows())

        result = read.get_ad_performance(config, customer_id="123")

        issues = result["policy_issues"]
        assert [i["ad_id"] for i in issues] == [1, 2, 4]
        assert issues[0]["cost"] == 12.0
        assert issues[2]["review_status"] == "UNDER_APPEAL"
        disapproved = [i for i in result["insights"] if "DISAPPROVED" in i]
        assert len(disapproved) == 1
        assert "2 ad(s)" in disapproved[0] and "12.0 EUR" in disapproved[0]
        assert "DESTINATION_NOT_WORKING" in disapproved[0]
        limited = [i for i in result["insights"] if "APPROVED_LIMITED" in i]
        assert len(limited) == 1 and "8.0 EUR" in limited[0]

    def test_compact_mode_counts_approval_statuses(self, config, monkeypatch):
        _patch_rows(monkeypatch, self._rows())

        result = read.get_ad_performance(config, customer_id="123", compact=True)

        assert result["by_approval_status"] == {
            "DISAPPROVED": 2, "APPROVED_LIMITED": 1, "APPROVED": 1,
        }
        assert len(result["policy_issues"]) == 3
        assert any("DISAPPROVED" in i for i in result["insights"])
        top = result["ads_top_spend"][0]
        assert top["ad_group_ad.policy_summary.policy_topics"] == [
            "DESTINATION_NOT_WORKING"
        ]

    def test_clean_account_adds_no_policy_noise(self, config, monkeypatch):
        _patch_rows(monkeypatch, [self._ad(1, "APPROVED")])

        full = read.get_ad_performance(config, customer_id="123")
        compact = read.get_ad_performance(config, customer_id="123", compact=True)

        assert "policy_issues" not in full and "insights" not in full
        assert compact["policy_issues"] == []
        assert not any("APPROVED" in i for i in compact["insights"])


class TestChangeHistory:
    def _row(self, **overrides):
        row = {
            "change_event.change_date_time": "2026-10-01 14:03:22.123456",
            "change_event.user_email": "owner@example.com",
            "change_event.client_type": "GOOGLE_ADS_WEB_CLIENT",
            "change_event.change_resource_type": "CAMPAIGN_BUDGET",
            "change_event.resource_change_operation": "UPDATE",
            "change_event.changed_fields": ["amount_micros"],
            "change_event.change_resource_name": "customers/123/campaignBudgets/9",
            "campaign.id": 11,
            "campaign.name": "Brand",
            "ad_group.id": None,
            "ad_group.name": "",
        }
        row.update(overrides)
        return row

    @staticmethod
    def _window():
        from datetime import date, timedelta

        today = date.today()
        return today, today - timedelta(days=29)

    def test_default_query_has_date_window_order_and_limit(self, config, monkeypatch):
        from datetime import timedelta

        seen = _routing_query(monkeypatch, lambda _q: [])
        today, earliest = self._window()

        result = read.get_change_history(config, customer_id="123")

        query = seen[0]
        assert "FROM change_event" in query
        assert f"change_event.change_date_time >= '{earliest.isoformat()}'" in query
        tomorrow = (today + timedelta(days=1)).isoformat()
        assert f"change_event.change_date_time <= '{tomorrow}'" in query
        assert "ORDER BY change_event.change_date_time DESC" in query
        assert "LIMIT 1000" in query
        for field in (
            "change_event.user_email", "change_event.client_type",
            "change_event.changed_fields", "campaign.name", "ad_group.name",
        ):
            assert field in query
        assert result["date_range"] == {
            "start": earliest.isoformat(), "end": today.isoformat(),
        }
        assert result["total_changes"] == 0
        assert any("No changes recorded" in i for i in result["insights"])

    def test_explicit_range_includes_the_whole_end_day(self, config, monkeypatch):
        from datetime import timedelta

        seen = _routing_query(monkeypatch, lambda _q: [])
        today, _ = self._window()
        start = today - timedelta(days=10)
        end = today - timedelta(days=3)

        read.get_change_history(
            config, customer_id="123",
            date_range_start=start.isoformat(), date_range_end=end.isoformat(),
        )

        assert f">= '{start.isoformat()}'" in seen[0]
        assert f"<= '{(end + timedelta(days=1)).isoformat()}'" in seen[0]

    def test_start_older_than_retention_is_clamped_with_a_note(
        self, config, monkeypatch
    ):
        seen = _routing_query(monkeypatch, lambda _q: [])
        _, earliest = self._window()

        result = read.get_change_history(
            config, customer_id="123", date_range_start="2020-01-01"
        )

        assert f">= '{earliest.isoformat()}'" in seen[0]
        assert any("30 days" in n for n in result["notes"])

    def test_range_entirely_outside_retention_is_an_error(self, config, monkeypatch):
        seen = _routing_query(monkeypatch, lambda _q: [])

        result = read.get_change_history(
            config, customer_id="123",
            date_range_start="2020-01-01", date_range_end="2020-01-31",
        )

        assert "error" in result and not seen

    @pytest.mark.parametrize("kwargs, message", [
        ({"date_range_start": "01.10.2026"}, "YYYY-MM-DD"),
        ({"date_range_end": "yesterday"}, "YYYY-MM-DD"),
        ({"campaign_id": "12; DROP"}, "numeric"),
        ({"resource_types": ["CAMPAIGN", "KEYWORD"]}, "KEYWORD"),
    ])
    def test_invalid_input_is_refused_before_querying(
        self, config, monkeypatch, kwargs, message
    ):
        seen = _routing_query(monkeypatch, lambda _q: [])

        result = read.get_change_history(config, customer_id="123", **kwargs)

        assert message in result["error"] and not seen

    def test_campaign_and_resource_type_filters(self, config, monkeypatch):
        seen = _routing_query(monkeypatch, lambda _q: [])

        result = read.get_change_history(
            config, customer_id="123", campaign_id="42",
            resource_types=["campaign_budget", "CAMPAIGN"],
        )

        assert "AND campaign.id = 42" in seen[0]
        assert (
            "change_event.change_resource_type IN ('CAMPAIGN_BUDGET', 'CAMPAIGN')"
            in seen[0]
        )
        assert any("campaign filter" in n for n in result["notes"])

    def test_limit_is_capped_at_the_api_maximum(self, config, monkeypatch):
        seen = _routing_query(monkeypatch, lambda _q: [])

        read.get_change_history(config, customer_id="123", limit=50_000)

        assert "LIMIT 10000" in seen[0]

    def test_rows_are_mapped_with_client_labels_and_counts(self, config, monkeypatch):
        rows = [
            self._row(),
            self._row(**{
                "change_event.change_date_time": "2026-09-30 09:00:00",
                "change_event.client_type": "GOOGLE_ADS_RECOMMENDATIONS_SUBSCRIPTION",
                "change_event.user_email": "",
                "change_event.change_resource_type": "AD_GROUP_CRITERION",
                "change_event.resource_change_operation": "CREATE",
                "change_event.changed_fields": ["keyword.text", "keyword.match_type"],
                "ad_group.id": 7,
                "ad_group.name": "Generic",
            }),
            self._row(**{
                "change_event.client_type": "GOOGLE_ADS_SCRIPTS",
                "change_event.change_resource_type": "CAMPAIGN",
                "change_event.changed_fields": ["maximize_conversions.target_cpa_micros"],
            }),
        ]
        _routing_query(monkeypatch, lambda _q: rows)

        result = read.get_change_history(config, customer_id="123")

        first, second, third = result["changes"]
        assert first == {
            "change_time": "2026-10-01 14:03:22.123456",
            "user_email": "owner@example.com",
            "client_type": "GOOGLE_ADS_WEB_CLIENT",
            "client": "Google Ads UI",
            "resource_type": "CAMPAIGN_BUDGET",
            "operation": "UPDATE",
            "changed_fields": ["amount_micros"],
            "campaign": "Brand",
            "campaign_id": 11,
            "ad_group": None,
            "ad_group_id": None,
            "resource_name": "customers/123/campaignBudgets/9",
        }
        assert second["client"] == "Auto-applied recommendation"
        assert second["user_email"] is None
        assert second["ad_group"] == "Generic"
        assert third["client"] == "Google Ads scripts"
        assert result["by_client"] == {
            "Google Ads UI": 1, "Auto-applied recommendation": 1,
            "Google Ads scripts": 1,
        }
        assert result["by_day"] == {"2026-10-01": 2, "2026-09-30": 1}
        assert any("auto-applied" in i for i in result["insights"])
        budget = [i for i in result["insights"] if "budgets or bidding" in i]
        assert budget and budget[0].startswith("2 change(s)")
        assert "truncated" not in result

    def test_reaching_the_limit_marks_the_result_truncated(self, config, monkeypatch):
        _routing_query(monkeypatch, lambda _q: [self._row(), self._row()])

        result = read.get_change_history(config, customer_id="123", limit=2)

        assert result["truncated"] is True
        assert any("limit of 2" in n for n in result["notes"])


class TestChangeHistoryRegistration:
    @pytest.mark.asyncio
    async def test_tool_is_read_only_ads_and_exposes_filters(self):
        from adloop.server import mcp

        tool = {t.name: t for t in await mcp.list_tools()}["get_change_history"]
        assert tool.annotations.read_only_hint is True
        assert set(tool.tags) == {"ads"}
        assert {
            "customer_id", "date_range_start", "date_range_end",
            "campaign_id", "resource_types", "limit",
        } <= set(tool.parameters["properties"])
        assert tool.description.splitlines()[0].endswith(".")
