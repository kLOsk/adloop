"""Cross-reference tools request GA4 key events (keyEvents), not the legacy
``conversions`` alias, and keep their output field names."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop import crossref
from adloop.config import AdLoopConfig, GA4Config

CAMPAIGNS = {
    "campaigns": [{
        "campaign.name": "Brand",
        "campaign.status": "ENABLED",
        "metrics.clicks": 40,
        "metrics.cost": 100.0,
        "metrics.conversions": 4,
    }],
}
ADS = {
    "ads": [{
        "ad_group_ad.ad.id": 1,
        "ad_group_ad.ad.final_urls": ["https://example.com/pricing"],
        "campaign.name": "Brand",
        "ad_group.name": "Core",
        "metrics.clicks": 40,
        "metrics.cost": 100.0,
    }],
}


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ga4=GA4Config(property_id="properties/123456"))


class _FakeReport:
    """Records every run_ga4_report call and answers by requested dimensions."""

    def __init__(self, rows_by_dims: dict[tuple, list[dict]]):
        self.rows_by_dims = rows_by_dims
        self.calls: list[dict] = []

    def __call__(self, _config, **kwargs):
        self.calls.append(kwargs)
        return {"rows": self.rows_by_dims.get(tuple(kwargs["dimensions"]), [])}


def _patched(report, **ads):
    return (
        patch("adloop.ga4.reports.run_ga4_report", report),
        patch("adloop.ads.read.get_campaign_performance", return_value=ads.get("campaigns", CAMPAIGNS)),
        patch("adloop.ads.read.get_ad_performance", return_value=ads.get("ads", ADS)),
        patch("adloop.ads.currency.get_currency_code", return_value="EUR"),
    )


def _assert_no_legacy_metric(report):
    for call in report.calls:
        assert "conversions" not in call["metrics"], call
    assert any("keyEvents" in call["metrics"] for call in report.calls)


def test_analyze_campaign_conversions_reads_key_events(config):
    report = _FakeReport({
        ("sessionCampaignName", "sessionSource", "sessionMedium"): [
            {"sessionCampaignName": "Brand", "sessionSource": "google",
             "sessionMedium": "cpc", "sessions": "20", "keyEvents": "3.0",
             "engagedSessions": "12", "totalUsers": "18"},
            {"sessionCampaignName": "(organic)", "sessionSource": "google",
             "sessionMedium": "organic", "sessions": "50", "keyEvents": "5",
             "engagedSessions": "30", "totalUsers": "45"},
        ],
    })
    p1, p2, p3, p4 = _patched(report)
    with p1, p2, p3, p4:
        result = crossref.analyze_campaign_conversions(config, property_id="properties/123456")

    _assert_no_legacy_metric(report)
    campaign = result["campaigns"][0]
    # "3.0" must count as 3 (int("3.0") would have read it as 0).
    assert campaign["ga4_paid_conversions"] == 3
    assert campaign["cost_per_ga4_conversion"] == pytest.approx(33.3333)
    assert result["non_paid_channels"][0]["conversions"] == 5


def test_landing_page_analysis_reads_key_events(config):
    report = _FakeReport({
        ("pagePath", "sessionSource", "sessionMedium"): [
            {"pagePath": "/pricing", "sessionSource": "google", "sessionMedium": "cpc",
             "sessions": "15", "keyEvents": "2", "engagedSessions": "9",
             "bounceRate": "0.4"},
        ],
    })
    p1, p2, p3, p4 = _patched(report)
    with p1, p2, p3, p4:
        result = crossref.landing_page_analysis(config, property_id="properties/123456")

    _assert_no_legacy_metric(report)
    page = next(p for p in result["landing_pages"] if p["page_path"] == "/pricing")
    assert page["ga4_paid_conversions"] == 2


def test_attribution_check_reads_key_events(config):
    report = _FakeReport({
        ("eventName",): [{"eventName": "sign_up", "eventCount": "9"}],
        ("sessionSource", "sessionMedium"): [
            {"sessionSource": "google", "sessionMedium": "cpc",
             "sessions": "20", "keyEvents": "4"},
            {"sessionSource": "(direct)", "sessionMedium": "(none)",
             "sessions": "30", "keyEvents": "1.5"},
        ],
    })
    p1, p2, p3, p4 = _patched(report)
    with p1, p2, p3, p4:
        result = crossref.attribution_check(
            config, property_id="properties/123456", conversion_events=["sign_up"],
        )

    _assert_no_legacy_metric(report)
    assert result["ga4_paid_conversions"] == 4
    assert result["ga4_all_conversions"] == 5.5
    assert result["by_source"][0]["conversions"] == 1.5
    assert result["discrepancy_pct"] == 0.0


def test_key_events_helper_tolerates_missing_and_garbage_values():
    assert crossref._key_events({}) == 0
    assert crossref._key_events({"keyEvents": "n/a"}) == 0
    assert crossref._key_events({"keyEvents": "7"}) == 7
    assert crossref._key_events({"keyEvents": "2.345"}) == 2.35
