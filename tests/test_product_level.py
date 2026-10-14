"""Product level: Merchant Center product issues and Shopping/PMax product performance."""

from unittest.mock import patch

from adloop.ads import shopping
from adloop.config import AdLoopConfig, MerchantConfig
from adloop.merchant import read as merchant_read
from adloop.merchant.client import MerchantNotRegistered


def _issue(code, attribute="", severity="DISAPPROVED", resolution="MERCHANT_ACTION", countries=("DE",)):
    return {
        "type": {"code": code, "canonicalAttribute": attribute},
        "severity": {
            "severityPerReportingContext": [
                {"reportingContext": "SHOPPING_ADS", "disapprovedCountries": list(countries), "demotedCountries": []}
            ],
            "aggregatedSeverity": severity,
        },
        "resolution": resolution,
    }


def _row(offer, title, click, issues, status="NOT_ELIGIBLE_OR_DISAPPROVED"):
    return {"productView": {
        "id": f"online~de~DE~{offer}", "offerId": offer, "title": title, "brand": "Bohne", "feedLabel": "DE",
        "availability": "in stock", "clickPotential": click, "aggregatedReportingContextStatus": status,
        "itemIssues": issues,
    }}


_ROWS = [
    _row("sku-low", "Filter papers", "LOW", [_issue("image_link_broken", "image_link")]),
    _row("sku-high", "Espresso grinder", "HIGH", [_issue("missing_gtin", "gtin"), _issue("image_link_broken", "image_link")]),
    _row("sku-wait", "Coffee beans", "MEDIUM", [_issue("pending_initial_policy_review", resolution="PENDING_PROCESSING", severity="PENDING")]),
]


def _call(rows=_ROWS, side_effect=None, **kwargs):
    sent = []

    def fake(config, path, body, *, api):
        sent.append((path, body, api))
        if side_effect:
            return side_effect(len(sent))
        return {"results": rows}

    with patch("adloop.merchant.client.merchant_post", side_effect=fake):
        result = merchant_read.get_merchant_product_issues(AdLoopConfig(merchant=MerchantConfig(account_id="555")), **kwargs)
    return result, sent


def test_disapproved_products_come_with_their_issues_high_click_potential_first():
    result, sent = _call()
    path, body, api = sent[0]
    assert path == "accounts/555/reports:search" and api == "reports/v1"
    assert "FROM product_view WHERE aggregated_reporting_context_status = 'NOT_ELIGIBLE_OR_DISAPPROVED'" in body["query"]
    assert [p["offer_id"] for p in result["products"]] == ["sku-high", "sku-wait", "sku-low"]
    first = result["products"][0]["issues"][0]
    assert first == {"code": "missing_gtin", "attribute": "gtin", "severity": "DISAPPROVED", "resolution": "MERCHANT_ACTION",
                     "where": [{"reporting_context": "SHOPPING_ADS", "disapproved_countries": ["DE"], "demoted_countries": []}]}
    assert result["issues_by_code"][0] == {"code": "image_link_broken", "attribute": "image_link", "severity": "DISAPPROVED",
                                          "merchant_action": True, "products": 2}
    assert any("HIGH click potential" in i for i in result["insights"])
    assert any("pending_initial_policy_review" in i for i in result["insights"])


def test_issue_code_and_status_filters():
    result, sent = _call(issue_code="missing_gtin", status="limited")
    assert "= 'ELIGIBLE_LIMITED'" in sent[0][1]["query"]
    assert [p["offer_id"] for p in result["products"]] == ["sku-high"]
    any_status, sent = _call(status="any", rows=_ROWS + [_row("sku-ok", "Mug", "LOW", [], status="ELIGIBLE")])
    assert "WHERE" not in sent[0][1]["query"]
    assert "sku-ok" not in [p["offer_id"] for p in any_status["products"]]
    assert "error" in _call(status="broken")[0]


def test_account_id_is_required_and_numeric():
    with patch("adloop.merchant.client.merchant_post") as post:
        assert "account_id is required" in merchant_read.get_merchant_product_issues(AdLoopConfig())["error"]
        assert "numeric" in merchant_read.get_merchant_product_issues(AdLoopConfig(), account_id="abc")["error"]
    post.assert_not_called()


def test_unregistered_project_registers_once_then_asks_to_retry():
    def always_unregistered(_n):
        raise MerchantNotRegistered("not registered")

    with patch("adloop.merchant.client.register_gcp") as register:
        result, sent = _call(side_effect=always_unregistered)
    register.assert_called_once()
    assert result["status"] == "DEVELOPER_REGISTRATION_COMPLETED" and len(sent) == 2


def _perf_rows():
    return [
        {"segments.product_item_id": "sku-1", "segments.product_title": "Grinder", "segments.product_brand": "Bohne",
         "campaign.id": 1, "campaign.name": "Shopping DE", "campaign.advertising_channel_type": "SHOPPING",
         "metrics.impressions": 1000, "metrics.clicks": 50, "metrics.cost_micros": 40_000_000,
         "metrics.conversions": 2.0, "metrics.conversions_value": 200.0},
        {"segments.product_item_id": "sku-1", "segments.product_title": "Grinder", "segments.product_brand": "Bohne",
         "campaign.id": 2, "campaign.name": "PMax DE", "campaign.advertising_channel_type": "PERFORMANCE_MAX",
         "metrics.impressions": 500, "metrics.clicks": 10, "metrics.cost_micros": 10_000_000,
         "metrics.conversions": 0.0, "metrics.conversions_value": 0.0},
        {"segments.product_item_id": "sku-2", "segments.product_title": "Filters", "segments.product_brand": "Bohne",
         "campaign.id": 1, "campaign.name": "Shopping DE", "campaign.advertising_channel_type": "SHOPPING",
         "metrics.impressions": 800, "metrics.clicks": 30, "metrics.cost_micros": 25_000_000,
         "metrics.conversions": 0.0, "metrics.conversions_value": 0.0},
    ]


def _perf(rows=None, **kwargs):
    sent = []

    def fake(config, customer_id, query):
        sent.append(query)
        return _perf_rows() if rows is None else rows

    with patch("adloop.ads.gaql.execute_query", side_effect=fake), \
            patch("adloop.ads.currency.get_currency_code", return_value="EUR"):
        result = shopping.get_shopping_product_performance(AdLoopConfig(), customer_id="1", **kwargs)
    return result, sent


def test_products_are_summed_across_campaigns_with_cpa_and_roas():
    result, sent = _perf()
    assert "FROM shopping_performance_view" in sent[0] and "DURING LAST_30_DAYS" in sent[0]
    grinder, filters = result["products"]
    assert grinder["item_id"] == "sku-1" and grinder["cost"] == 50.0 and grinder["clicks"] == 60
    assert grinder["campaigns"] == ["PMax DE", "Shopping DE"]
    assert grinder["channels"] == ["PERFORMANCE_MAX", "SHOPPING"]
    assert grinder["cpa"] == 25.0 and grinder["roas"] == 4.0
    assert filters["cpa"] is None and filters["roas"] is None
    assert result["totals"] == {"products": 2, "cost": 75.0, "clicks": 90, "conversions": 2.0,
                                "conversions_value": 200.0, "roas": 2.67, "currency": "EUR"}
    assert [p["item_id"] for p in result["no_conversion_spenders"]] == ["sku-2"]
    assert any("25.0 EUR without a conversion" in i for i in result["insights"])


def test_campaign_filter_dates_and_empty_accounts():
    _result, sent = _perf(campaign_id="2", date_range_start="2026-09-01", date_range_end="2026-09-30")
    assert "AND campaign.id = 2" in sent[0] and "BETWEEN '2026-09-01' AND '2026-09-30'" in sent[0]
    empty, _ = _perf(rows=[])
    assert empty["products"] == [] and "No Shopping or Performance Max product data" in empty["insights"][0]
    assert "numeric" in _perf(campaign_id="x")[0]["error"]
