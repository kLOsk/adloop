"""Date-ranged reports must include spend from entities removed since (#120).

Status is a snapshot of today; metrics belong to the period. So the queries
carry no status predicate, and REMOVED rows with zero impressions are dropped
in Python (GAQL has no OR, and ``impressions > 0`` alone would also hide
enabled campaigns with no impressions).
"""

from __future__ import annotations

import pytest

from adloop.ads import pmax, read
from adloop.config import AdLoopConfig, AdsConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="123-456-7890"))


@pytest.fixture(autouse=True)
def fixed_currency(monkeypatch):
    monkeypatch.setattr(read, "get_currency_code", lambda *_a, **_k: "EUR")
    monkeypatch.setattr(pmax, "get_currency_code", lambda *_a, **_k: "EUR")


def _capture(monkeypatch, respond):
    import adloop.ads.gaql as gaql

    seen: list[str] = []

    def execute_query(_c, _cid, query):
        seen.append(query)
        return [dict(r) for r in respond(query)]

    monkeypatch.setattr(gaql, "execute_query", execute_query)
    return seen


def _no_status_predicate(query: str) -> bool:
    return "!= 'REMOVED'" not in query


# (status field, row builder) per tool
def _campaign(name, status, impressions, cost_micros):
    return {
        "campaign.id": abs(hash(name)) % 10_000, "campaign.name": name,
        "campaign.status": status, "campaign.advertising_channel_type": "SEARCH",
        "metrics.impressions": impressions, "metrics.clicks": impressions // 10,
        "metrics.cost_micros": cost_micros, "metrics.conversions": 1,
    }


def _ad(name, status, impressions, cost_micros):
    return {
        "campaign.name": "C", "ad_group.id": "g", "ad_group.name": "AG",
        "ad_group_ad.ad.id": name, "ad_group_ad.ad.type": "RESPONSIVE_SEARCH_AD",
        "ad_group_ad.status": status,
        "metrics.impressions": impressions, "metrics.clicks": 1,
        "metrics.cost_micros": cost_micros, "metrics.conversions": 0,
    }


def _keyword(name, status, impressions, cost_micros):
    return {
        "campaign.name": "C", "ad_group.name": "AG",
        "ad_group_criterion.keyword.text": name,
        "ad_group_criterion.status": status,
        "metrics.impressions": impressions, "metrics.clicks": 1,
        "metrics.cost_micros": cost_micros, "metrics.conversions": 0,
    }


def _audience(name, status, impressions, cost_micros):
    return {
        "campaign.id": 1, "campaign.name": name, "campaign.status": status,
        "campaign.advertising_channel_type": "DISPLAY",
        "ad_group_criterion.type": "USER_LIST",
        "metrics.impressions": impressions, "metrics.clicks": 1,
        "metrics.cost_micros": cost_micros, "metrics.conversions": 0,
    }


def _pmax_campaign(name, status, impressions, cost_micros):
    return {
        "campaign.id": 1, "campaign.name": name, "campaign.status": status,
        "metrics.impressions": impressions, "metrics.clicks": 1,
        "metrics.cost_micros": cost_micros, "metrics.conversions": 0,
        "metrics.conversions_value": 0, "segments.ad_network_type": "SEARCH",
    }


def _names(rows, key):
    return [r[key] for r in rows]


CASES = [
    # id, call, builder, result key, name field, status field in query
    ("campaign", lambda c: read.get_campaign_performance(c, customer_id="1"),
     _campaign, "campaigns", "campaign.name", "campaign.status"),
    ("ad", lambda c: read.get_ad_performance(c, customer_id="1"),
     _ad, "ads", "ad_group_ad.ad.id", "ad_group_ad.status"),
    ("keyword", lambda c: read.get_keyword_performance(c, customer_id="1"),
     _keyword, "keywords", "ad_group_criterion.keyword.text",
     "ad_group_criterion.status"),
    ("audience", lambda c: read.get_audience_performance(c, customer_id="1"),
     _audience, "audiences", "campaign.name", "campaign.status"),
    ("pmax", lambda c: pmax.get_pmax_performance(c, customer_id="1"),
     _pmax_campaign, "campaigns", "campaign.name", "campaign.status"),
]


@pytest.mark.parametrize(
    "call,build,key,name_field,status_field",
    [c[1:] for c in CASES], ids=[c[0] for c in CASES],
)
class TestDateRangedReportsKeepRemovedSpend:
    def _rows(self, build):
        return [
            build("live", "ENABLED", 100, 5_000_000),
            build("idle-enabled", "ENABLED", 0, 0),
            build("removed-with-spend", "REMOVED", 400, 9_000_000),
            build("removed-idle", "REMOVED", 0, 0),
        ]

    def test_queries_have_no_status_predicate_and_select_status(
        self, config, monkeypatch, call, build, key, name_field, status_field
    ):
        rows = self._rows(build)
        seen = _capture(monkeypatch, lambda _q: rows)

        call(config)

        assert seen
        assert all(_no_status_predicate(q) for q in seen), seen
        assert status_field in seen[0].split("FROM")[0]

    def test_removed_row_with_spend_is_kept(
        self, config, monkeypatch, call, build, key, name_field, status_field
    ):
        rows = self._rows(build)
        _capture(monkeypatch, lambda _q: rows if "metrics.cost_micros" in _q else [])

        result = call(config)

        assert "removed-with-spend" in _names(result[key], name_field)

    def test_removed_idle_row_is_dropped_but_enabled_idle_is_kept(
        self, config, monkeypatch, call, build, key, name_field, status_field
    ):
        rows = self._rows(build)
        _capture(monkeypatch, lambda _q: rows if "metrics.cost_micros" in _q else [])

        names = _names(call(config)[key], name_field)

        assert "removed-idle" not in names
        assert "idle-enabled" in names
        assert "live" in names


def test_compact_campaign_totals_include_removed_spend(config, monkeypatch):
    rows = [
        _campaign("live", "ENABLED", 100, 5_000_000),
        _campaign("removed-with-spend", "REMOVED", 400, 9_000_000),
    ]
    _capture(monkeypatch, lambda q: rows if "search_impression_share" not in q else [])

    result = read.get_campaign_performance(config, customer_id="1", compact=True)

    assert result["totals"]["cost"] == 14.0
    assert result["total_campaigns"] == 2


def test_impression_share_query_has_no_status_predicate(config, monkeypatch):
    seen = _capture(monkeypatch, lambda _q: [])

    read.get_campaign_performance(config, customer_id="1")

    share = [q for q in seen if "search_impression_share" in q]
    assert share and all(_no_status_predicate(q) for q in share)
