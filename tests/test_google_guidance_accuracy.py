"""Advice that has to match Google's own documentation.

Four statements AdLoop used to make are corrected here, each against a Google
page:

- Search campaigns no longer take manual language targeting (retired late
  September 2026), so ``language_ids`` cannot be required for them.
  https://developers.google.com/google-ads/api/docs/deprecations
  https://developers.google.com/google-ads/api/docs/targeting/criteria
  https://support.google.com/google-ads/answer/1722078
- The "at least 5x target CPA" budget warning is AdLoop's rule of thumb. The
  warning used to say "Google recommends"; Google's own guideline is 2 to 3
  times the target CPA.
  https://support.google.com/google-ads/answer/12092760
- The Lost IS (budget) share is not a reliable signal for Maximize conversions
  and Maximize conversion value run without a target.
  https://support.google.com/google-ads/answer/7381968
- "Learning" status is not triggered by budget, target, creative or targeting
  changes (rules text only, checked in ``TestRulesText``).
  https://support.google.com/google-ads/answer/13020501
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from adloop.ads import read, write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.views import performance as performance_view
from tests.test_ads_write import (
    _FakeClient,
    _FakeGoogleAdsService,
    _FakeMutateOperationResponse,
    _FakePathService,
)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="1234567890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=500),
    )


@pytest.fixture
def no_queries(monkeypatch):
    """Drafts that only touch targeting must not need the API in these tests."""
    monkeypatch.setattr("adloop.ads.gaql.execute_query", lambda *_a, **_k: [])


def _draft(config, **kwargs):
    base = dict(
        customer_id="1234567890",
        campaign_name="Search",
        daily_budget=20,
        bidding_strategy="TARGET_SPEND",
        geo_target_ids=["2840"],
    )
    base.update(kwargs)
    return write.draft_campaign(config, **base)


def _criterion_ops(operations, kind):
    return [
        op.campaign_criterion_operation
        for op in operations
        if op.campaign_criterion_operation._pb.WhichOneof("operation") == kind
    ]


def _language_creates(operations):
    return [
        op.create.language.language_constant
        for op in _criterion_ops(operations, "create")
        if op.create.language.language_constant
    ]


class TestSearchLanguageDraft:
    def test_draft_campaign_no_longer_needs_language_ids(self, config):
        result = _draft(config)

        assert "error" not in result
        assert result["changes"]["language_ids"] == []
        assert not any("language" in w.lower() for w in result.get("warnings", []))

    def test_draft_preview_keeps_language_ids_and_adds_googles_notice(self, config):
        result = _draft(config, language_ids=["1001"])

        assert "error" not in result
        assert result["changes"]["language_ids"] == ["1001"]
        notice = [w for w in result["warnings"] if "language targeting" in w]
        assert len(notice) == 1
        assert "late September 2026" in notice[0]
        assert "OPERATION_NOT_PERMITTED_FOR_CONTEXT" in notice[0]

    def test_geo_targets_stay_required(self, config):
        result = _draft(config, geo_target_ids=[])

        assert result["error"] == "Validation failed"
        assert any("geo_target_ids is required" in d for d in result["details"])
        assert not any("language_ids" in d for d in result["details"])

    def test_server_tool_makes_language_ids_optional(self):
        from adloop import server

        tool = getattr(server.draft_campaign, "fn", server.draft_campaign)
        params = inspect.signature(tool).parameters

        assert params["language_ids"].default is None
        assert params["geo_target_ids"].default is inspect.Parameter.empty

    def test_update_draft_accepts_an_empty_language_list(self, config, no_queries):
        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id="1001", language_ids=[],
        )

        assert "error" not in result
        assert result["changes"]["language_ids"] == []
        warning = [w for w in result["warnings"] if "language" in w.lower()]
        assert len(warning) == 1
        assert "removes all campaign-level language criteria" in warning[0]
        assert "can widen who sees the ads" in warning[0]

    def test_update_draft_warns_when_setting_languages(self, config, no_queries):
        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id="1001", language_ids=["1000"],
        )

        assert "error" not in result
        notice = [w for w in result["warnings"] if "language targeting" in w]
        assert len(notice) == 1
        assert notice[0].startswith("On a Search campaign:")

    def test_update_draft_without_languages_says_nothing_about_them(
        self, config, no_queries
    ):
        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id="1001",
            search_partners_enabled=False,
        )

        assert "error" not in result
        assert not any("language" in w.lower() for w in result.get("warnings", []))


class TestSearchLanguageApply:
    """What actually reaches Google, not what the preview says."""

    @staticmethod
    def _create(language_ids):
        service = _FakeGoogleAdsService([
            _FakeMutateOperationResponse("campaign_budget_result", "customers/1/campaignBudgets/1"),
            _FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/2"),
            _FakeMutateOperationResponse("ad_group_result", "customers/1/adGroups/3"),
        ])
        client = _FakeClient({
            "GoogleAdsService": service,
            "CampaignService": _FakePathService("campaigns"),
            "CampaignBudgetService": _FakePathService("campaignBudgets"),
            "AdGroupService": _FakePathService("adGroups"),
        })
        write._apply_create_campaign(client, "1234567890", {
            "campaign_name": "Search Launch",
            "daily_budget": 50,
            "bidding_strategy": "MAXIMIZE_CONVERSIONS",
            "channel_type": "SEARCH",
            "ad_group_name": "Main",
            "geo_target_ids": ["2840"],
            "language_ids": language_ids,
        })
        return service.operations

    def test_create_without_languages_sends_no_language_criterion(self):
        operations = self._create([])

        assert _language_creates(operations) == []
        geo = [
            op.create.location.geo_target_constant
            for op in _criterion_ops(operations, "create")
            if op.create.location.geo_target_constant
        ]
        assert geo == ["geoTargetConstants/2840"]

    def test_create_passes_given_languages_through_unchanged(self):
        operations = self._create(["1001", "1000"])

        assert _language_creates(operations) == [
            "languageConstants/1001", "languageConstants/1000",
        ]

    @staticmethod
    def _update(language_ids):
        queries: list[str] = []
        existing = [
            "customers/1234567890/campaignCriteria/1001~1000",
            "customers/1234567890/campaignCriteria/1001~1003",
        ]

        class _Searchable(_FakeGoogleAdsService):
            def search(self, customer_id: str, query: str):
                queries.append(query)
                return [
                    SimpleNamespace(campaign_criterion=SimpleNamespace(resource_name=rn))
                    for rn in existing
                ]

        service = _Searchable([
            _FakeMutateOperationResponse("campaign_criterion_result", "customers/1/campaignCriteria/x"),
        ])
        client = _FakeClient({
            "GoogleAdsService": service,
            "CampaignService": _FakePathService("campaigns"),
        })
        write._apply_update_campaign(
            client, "1234567890", {"campaign_id": "1001", "language_ids": language_ids},
        )
        return service.operations, queries, existing

    def test_update_with_empty_list_removes_and_creates_nothing(self):
        operations, queries, existing = self._update([])

        assert len(queries) == 1
        assert "campaign_criterion.type = 'LANGUAGE'" in queries[0]
        assert [op.remove for op in _criterion_ops(operations, "remove")] == existing
        assert _criterion_ops(operations, "create") == []

    def test_update_with_languages_replaces_the_criteria(self):
        operations, _queries, existing = self._update(["1001"])

        assert [op.remove for op in _criterion_ops(operations, "remove")] == existing
        assert _language_creates(operations) == ["languageConstants/1001"]


class TestBudgetRuleOfThumb:
    def test_draft_warning_does_not_cite_google_for_5x(self, config, monkeypatch):
        monkeypatch.setattr("adloop.ads.currency.get_currency_code", lambda *_a, **_k: "USD")

        result = _draft(
            config, bidding_strategy="TARGET_CPA", target_cpa=50, daily_budget=100,
        )

        warning = [w for w in result["warnings"] if "5x target CPA" in w]
        assert len(warning) == 1
        assert "Google recommends" not in warning[0]
        assert "AdLoop's rule of thumb, not a Google requirement" in warning[0]
        assert "2 to 3 times the target CPA" in warning[0]

    def test_no_message_in_the_write_module_says_google_recommends_5x(self):
        source = (REPO / "src" / "adloop" / "ads" / "write.py").read_text()

        assert "Google recommends at least 5x" not in source
        assert "publishes no fixed multiple" not in source
        # draft_campaign and update_campaign each carry the corrected wording.
        assert source.count("5x is AdLoop's rule of thumb") == 2
        assert source.count("2 to 3 times the target CPA") == 2


def _campaign(cid, name, strategy):
    return {
        "campaign.id": cid, "campaign.name": name,
        "campaign.advertising_channel_type": "SEARCH",
        "campaign.bidding_strategy_type": strategy,
        "metrics.impressions": 1000, "metrics.clicks": 100,
        "metrics.cost_micros": 50_000_000, "metrics.conversions": 5,
    }


def _share(cid, lost=0.35):
    return {
        "campaign.id": cid, "metrics.impressions": 1000,
        "metrics.search_impression_share": 0.4,
        "metrics.search_budget_lost_impression_share": lost,
        "metrics.search_rank_lost_impression_share": 0.25,
    }


def _target(cid, *, portfolio="", portfolio_type="UNSPECIFIED", cpa=0, roas=0.0,
            portfolio_cpa=0, portfolio_roas=0.0):
    return {
        "campaign.id": cid, "campaign.bidding_strategy": portfolio,
        "campaign.maximize_conversions.target_cpa_micros": cpa,
        "campaign.maximize_conversion_value.target_roas": roas,
        "bidding_strategy.type": portfolio_type,
        "bidding_strategy.maximize_conversions.target_cpa_micros": portfolio_cpa,
        "bidding_strategy.maximize_conversion_value.target_roas": portfolio_roas,
    }


_PORTFOLIO = "customers/123/biddingStrategies/9"


def _performance(monkeypatch, campaigns, targets, *, compact=False):
    """Run get_campaign_performance with the three queries answered from lists.

    ``targets`` may be an exception to simulate the target lookup failing.
    """
    import adloop.ads.gaql as gaql

    seen: list[str] = []

    def _query(_config, _cid, query):
        seen.append(query)
        if "target_cpa_micros" in query:
            if isinstance(targets, Exception):
                raise targets
            return [dict(t) for t in targets]
        if "search_impression_share" in query:
            return [_share(c["campaign.id"]) for c in campaigns]
        return [dict(c) for c in campaigns]

    monkeypatch.setattr(gaql, "execute_query", _query)
    monkeypatch.setattr("adloop.ads.currency.get_currency_code", lambda *_a, **_k: "USD")
    config = AdLoopConfig(ads=AdsConfig(customer_id="123"))
    result = read.get_campaign_performance(config, customer_id="123", compact=compact)
    return result, seen


def _caveats(result):
    return [i for i in result.get("insights", []) if "Do not read that as room" in i]


def _unconfirmed(result):
    return [i for i in result.get("insights", []) if "unconfirmed" in i]


def _plain(result):
    return [i for i in result.get("insights", []) if "run out of budget" in i]


KEY = "budget_share_reliability"


class TestBudgetLostShareOnFullBudgetStrategies:
    @pytest.mark.parametrize("strategy", ["MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"])
    @pytest.mark.parametrize("compact", [False, True])
    def test_untargeted_strategy_gets_the_caveat_and_not_the_plain_claim(
        self, monkeypatch, strategy, compact
    ):
        result, _ = _performance(
            monkeypatch, [_campaign(1, "Search Converting", strategy)], [_target(1)],
            compact=compact,
        )

        flagged = result["budget_limited_converters"]
        assert flagged[0]["bidding_strategy_type"] == strategy
        assert flagged[0][KEY] == "unreliable"
        caveat = _caveats(result)
        assert len(caveat) == 1
        assert "Search Converting" in caveat[0]
        assert "without a target" in caveat[0]
        assert "incompatible" in caveat[0]
        assert "budget simulator" in caveat[0]
        assert _plain(result) == []
        assert _unconfirmed(result) == []

    @pytest.mark.parametrize("strategy", ["MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"])
    def test_an_untargeted_portfolio_strategy_gets_the_caveat_too(self, monkeypatch, strategy):
        target = _target(1, portfolio=_PORTFOLIO, portfolio_type=strategy)

        result, _ = _performance(
            monkeypatch, [_campaign(1, "Search Converting", strategy)], [target],
        )

        assert result["budget_limited_converters"][0][KEY] == "unreliable"
        assert len(_caveats(result)) == 1
        assert _plain(result) == []

    @pytest.mark.parametrize("strategy,target", [
        ("MAXIMIZE_CONVERSIONS", _target(1, cpa=40_000_000)),
        ("MAXIMIZE_CONVERSION_VALUE", _target(1, roas=4.0)),
        ("MAXIMIZE_CONVERSIONS",
         _target(1, portfolio=_PORTFOLIO, portfolio_type="MAXIMIZE_CONVERSIONS",
                 portfolio_cpa=40_000_000)),
        ("MAXIMIZE_CONVERSION_VALUE",
         _target(1, portfolio=_PORTFOLIO, portfolio_type="MAXIMIZE_CONVERSION_VALUE",
                 portfolio_roas=4.0)),
    ], ids=["target-cpa", "target-roas", "portfolio-target-cpa", "portfolio-target-roas"])
    def test_a_target_keeps_the_plain_reading(self, monkeypatch, strategy, target):
        result, _ = _performance(
            monkeypatch, [_campaign(1, "Search Converting", strategy)], [target],
        )

        assert KEY not in result["budget_limited_converters"][0]
        assert _caveats(result) == []
        assert _unconfirmed(result) == []
        assert len(_plain(result)) == 1

    @pytest.mark.parametrize("targets", [
        [],
        [_target(1, portfolio=_PORTFOLIO)],
        [_target(1, portfolio=_PORTFOLIO, portfolio_type="UNKNOWN")],
    ], ids=["no-row", "unreadable-portfolio", "unknown-portfolio-type"])
    def test_an_unread_target_is_unconfirmed_not_a_budget_claim(self, monkeypatch, targets):
        result, _ = _performance(
            monkeypatch, [_campaign(1, "Search Converting", "MAXIMIZE_CONVERSIONS")], targets,
        )

        assert result["budget_limited_converters"][0][KEY] == "unknown"
        note = _unconfirmed(result)
        assert len(note) == 1
        assert "Search Converting" in note[0]
        assert "could not read whether a target" in note[0]
        assert _plain(result) == []
        assert _caveats(result) == []

    def test_the_lookup_reads_campaign_and_portfolio_targets_in_one_query(self, monkeypatch):
        campaigns = [
            _campaign(1, "One", "MAXIMIZE_CONVERSIONS"),
            _campaign(2, "Two", "MAXIMIZE_CONVERSION_VALUE"),
            _campaign(3, "Three", "TARGET_CPA"),
        ]

        _, seen = _performance(monkeypatch, campaigns, [_target(1), _target(2)])

        lookups = [q for q in seen if "target_cpa_micros" in q]
        assert len(lookups) == 1
        for field in (
            "campaign.id",
            "campaign.bidding_strategy",
            "campaign.maximize_conversions.target_cpa_micros",
            "campaign.maximize_conversion_value.target_roas",
            "bidding_strategy.type",
            "bidding_strategy.maximize_conversions.target_cpa_micros",
            "bidding_strategy.maximize_conversion_value.target_roas",
        ):
            assert field in lookups[0], field
        assert "campaign.id IN (1, 2)" in lookups[0]

    def test_a_failed_lookup_is_reported_not_swallowed(self, monkeypatch):
        result, _ = _performance(
            monkeypatch, [_campaign(1, "Search Converting", "MAXIMIZE_CONVERSIONS")],
            RuntimeError("quota exhausted"),
        )

        assert result["budget_limited_converters"][0][KEY] == "unknown"
        note = _unconfirmed(result)
        assert len(note) == 1
        assert "the target lookup failed" in note[0]
        assert "quota exhausted" in note[0]
        assert _plain(result) == []

    @pytest.mark.parametrize("strategy", ["TARGET_CPA", "TARGET_ROAS", "MANUAL_CPC", "TARGET_SPEND"])
    def test_other_strategies_never_trigger_the_target_lookup(self, monkeypatch, strategy):
        result, seen = _performance(
            monkeypatch, [_campaign(1, "Search Converting", strategy)], [_target(1)],
        )

        assert not any("target_cpa_micros" in q for q in seen)
        assert KEY not in result["budget_limited_converters"][0]
        assert _caveats(result) == []
        assert len(_plain(result)) == 1

    def test_an_affected_campaign_beyond_the_first_five_is_still_named(self, monkeypatch):
        campaigns = [_campaign(i, f"Target {i}", "TARGET_CPA") for i in range(1, 6)]
        campaigns.append(_campaign(6, "Untargeted Six", "MAXIMIZE_CONVERSIONS"))

        result, _ = _performance(monkeypatch, campaigns, [_target(6)])

        assert len(result["budget_limited_converters"]) == 6
        caveat = _caveats(result)
        assert len(caveat) == 1 and "Untargeted Six" in caveat[0]
        plain = _plain(result)
        assert len(plain) == 1
        assert plain[0].startswith("5 converting campaign(s)")
        assert "Untargeted Six" not in plain[0]

    def test_long_lists_say_how_many_names_were_left_out(self, monkeypatch):
        campaigns = [_campaign(i, f"Target {i}", "TARGET_CPA") for i in range(1, 8)]

        result, _ = _performance(monkeypatch, campaigns, [])

        assert "and 2 more" in _plain(result)[0]

    def test_campaigns_the_column_works_with_are_listed_first(self, monkeypatch):
        campaigns = [
            _campaign(1, "Untargeted", "MAXIMIZE_CONVERSIONS"),
            _campaign(2, "Unread", "MAXIMIZE_CONVERSIONS"),
            _campaign(3, "On Target CPA", "TARGET_CPA"),
        ]

        result, _ = _performance(monkeypatch, campaigns, [_target(1)])

        order = [(f["name"], f.get(KEY)) for f in result["budget_limited_converters"]]
        assert order == [
            ("On Target CPA", None), ("Unread", "unknown"), ("Untargeted", "unreliable"),
        ]


class TestDashboardBudgetAlert:
    @staticmethod
    def _rows(*names):
        return [
            {"name": n, "cost": 50.0, "conversions": 5, "lost_to_budget": 0.35}
            for n in names
        ]

    @pytest.mark.parametrize("state", ["unreliable", "unknown"])
    def test_unconfirmed_campaigns_are_not_shown_as_held_back_by_budget(self, state):
        result = {"budget_limited_converters": [
            {"name": "Maximize", "search_budget_lost_impression_share_pct": 35.0, KEY: state},
        ]}

        alerts = performance_view._alerts(self._rows("Maximize"), result)

        assert "limited" not in alerts

    def test_reliable_campaigns_still_raise_the_alert(self):
        result = {"budget_limited_converters": [
            {"name": "On Target CPA", "search_budget_lost_impression_share_pct": 35.0},
            {"name": "Untargeted", "search_budget_lost_impression_share_pct": 40.0,
             KEY: "unreliable"},
        ]}

        alerts = performance_view._alerts(self._rows("Untargeted", "On Target CPA"), result)

        assert alerts["limited"]["title"].startswith("1 campaign")
        assert "On Target CPA" in alerts["limited"]["detail"]
        assert "Untargeted" not in alerts["limited"]["detail"]

    @pytest.mark.parametrize("compact,untargeted", [(True, 5), (False, 10)])
    def test_a_real_alert_survives_the_truncated_list(self, monkeypatch, compact, untargeted):
        """Enough unconfirmed campaigns to fill the returned list must not hide
        the one campaign the budget alert is about."""
        campaigns = [
            _campaign(i, f"Untargeted {i}", "MAXIMIZE_CONVERSIONS")
            for i in range(1, untargeted + 1)
        ]
        campaigns.append(_campaign(99, "On Target CPA", "TARGET_CPA"))
        targets = [_target(i) for i in range(1, untargeted + 1)]

        result, _ = _performance(monkeypatch, campaigns, targets, compact=compact)

        returned = result["budget_limited_converters"]
        assert len(returned) == untargeted
        assert returned[0]["name"] == "On Target CPA"
        alerts = performance_view._alerts(self._rows("On Target CPA"), result)
        assert alerts["limited"]["title"].startswith("1 campaign")
        assert "On Target CPA" in alerts["limited"]["detail"]


class TestRulesText:
    """The three copies of the rules must carry the corrected statements."""

    _COPIES = (
        REPO / ".cursor" / "rules" / "adloop.mdc",
        REPO / ".claude" / "rules" / "adloop.md",
        REPO / "src" / "adloop" / "rules" / "adloop.md",
    )

    @pytest.mark.parametrize("path", _COPIES, ids=lambda p: p.parent.parent.name + "/" + p.name)
    def test_outdated_statements_are_gone(self, path):
        text = path.read_text()

        assert "Language targeting is mandatory" not in text
        assert "`language_ids` (REQUIRED)" not in text
        assert "Don't edit campaigns that are in an active learning phase" not in text
        assert "changes will restart the learning phase" not in text
        assert "take effect at once" not in text
        assert "publishes no fixed multiple" not in text

    @pytest.mark.parametrize("path", _COPIES, ids=lambda p: p.parent.parent.name + "/" + p.name)
    def test_corrected_statements_are_present(self, path):
        text = path.read_text()

        assert "Search campaigns take no language targeting" in text
        assert "never trigger it" in text
        assert "pass policy review before they serve" in text
        assert "AdLoop's rule of thumb" in text
        assert "2 to 3 times the target CPA" in text
        assert "Lost IS (budget) column is incompatible" in text
        assert "with no target CPA and no target ROAS" in text
        assert '`budget_share_reliability: "unreliable"`' in text
        assert '`"unknown"`' in text

    def test_the_three_copies_have_the_same_body(self):
        def body(path):
            text = path.read_text()
            if text.startswith("---"):
                text = text.split("---", 2)[2]
            return text.strip()

        cursor, claude, packaged = (body(p) for p in self._COPIES)
        assert cursor == claude == packaged
