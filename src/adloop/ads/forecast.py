"""Budget estimation via Google Ads Keyword Planner forecast metrics."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


_DEFAULT_MAX_CPC_MICROS = 1_000_000  # 1.00 in account currency


def estimate_budget(
    config: AdLoopConfig,
    *,
    keywords: list[dict],
    daily_budget: float = 0,
    geo_target_id: str = "2276",
    language_id: str = "1000",
    forecast_days: int = 30,
    customer_id: str = "",
) -> dict:
    """Forecast clicks, impressions, and cost for a set of keywords.

    Uses KeywordPlanIdeaService.GenerateKeywordForecastMetrics to estimate
    campaign performance without creating anything. Useful for budget planning
    before launching a new campaign.

    keywords: list of {"text": str, "match_type": "EXACT|PHRASE|BROAD", "max_cpc": float (optional)}
    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK, 2250=France)
    language_id: language constant (1000=English, 1001=German, 1002=French, 1003=Spanish)
    forecast_days: number of days to forecast (default 30)
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    if not keywords:
        return {"error": "At least one keyword is required"}

    client = get_ads_client(config)
    cid = normalize_customer_id(customer_id or config.ads.customer_id)

    googleads_service = client.get_service("GoogleAdsService")
    kp_service = client.get_service("KeywordPlanIdeaService")

    campaign = client.get_type("CampaignToForecast")
    campaign.keyword_plan_network = (
        client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    )

    max_bid = max(
        (int(kw.get("max_cpc", 0) * 1_000_000) for kw in keywords),
        default=_DEFAULT_MAX_CPC_MICROS,
    )
    if max_bid <= 0:
        max_bid = _DEFAULT_MAX_CPC_MICROS
    campaign.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = max_bid

    geo_modifier = client.get_type("CriterionBidModifier")
    geo_modifier.geo_target_constant = googleads_service.geo_target_constant_path(
        geo_target_id
    )
    campaign.geo_modifiers.append(geo_modifier)

    campaign.language_constants.append(
        googleads_service.language_constant_path(language_id)
    )

    ad_group = client.get_type("ForecastAdGroup")

    for kw in keywords:
        text = kw.get("text", "")
        if not text:
            continue
        match_type = kw.get("match_type", "BROAD").upper()
        cpc_micros = int(kw.get("max_cpc", 0) * 1_000_000) or _DEFAULT_MAX_CPC_MICROS

        biddable = client.get_type("BiddableKeyword")
        biddable.max_cpc_bid_micros = cpc_micros
        biddable.keyword.text = text
        biddable.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, match_type, client.enums.KeywordMatchTypeEnum.BROAD
        )
        ad_group.biddable_keywords.append(biddable)

    campaign.ad_groups.append(ad_group)

    request = client.get_type("GenerateKeywordForecastMetricsRequest")
    request.customer_id = cid
    request.campaign = campaign

    tomorrow = date.today() + timedelta(days=1)
    end_date = date.today() + timedelta(days=forecast_days)
    request.forecast_period.start_date = tomorrow.isoformat()
    request.forecast_period.end_date = end_date.isoformat()

    response = kp_service.generate_keyword_forecast_metrics(request=request)
    metrics = response.campaign_forecast_metrics

    clicks = getattr(metrics, "clicks", None)
    impressions = getattr(metrics, "impressions", None)
    avg_cpc_micros = getattr(metrics, "average_cpc_micros", None)
    cost_micros = getattr(metrics, "cost_micros", None)
    ctr = getattr(metrics, "click_through_rate", None)

    total_cost = round(cost_micros / 1_000_000, 2) if cost_micros else None
    avg_cpc = round(avg_cpc_micros / 1_000_000, 2) if avg_cpc_micros else None

    days = max(forecast_days, 1)
    daily = {
        "clicks": round(clicks / days, 1) if clicks else None,
        "impressions": round(impressions / days, 1) if impressions else None,
        "cost": round(total_cost / days, 2) if total_cost else None,
    }

    insights = []
    if total_cost is not None and clicks is not None and clicks > 0:
        effective_cpa_budget = total_cost / clicks * 10
        insights.append(
            f"Estimated {clicks:.0f} clicks over {forecast_days} days at "
            f"~{avg_cpc} avg CPC. Total estimated cost: {total_cost:.2f}."
        )
    if daily_budget > 0 and daily["cost"] is not None:
        if daily_budget < daily["cost"]:
            capture_pct = round(daily_budget / daily["cost"] * 100)
            insights.append(
                f"Daily budget of {daily_budget:.2f} would capture ~{capture_pct}% "
                f"of available traffic (estimated daily cost: {daily['cost']:.2f})."
            )
        else:
            insights.append(
                f"Daily budget of {daily_budget:.2f} is sufficient to capture "
                f"most available traffic (estimated daily cost: {daily['cost']:.2f})."
            )

    if impressions is not None and clicks is not None and impressions > 0 and clicks == 0:
        insights.append(
            "Forecast shows impressions but zero clicks — keywords may be too "
            "generic or CPCs too low for competitive positions."
        )

    return {
        "forecast_period": {
            "start": tomorrow.isoformat(),
            "end": end_date.isoformat(),
        },
        "estimated_clicks": clicks,
        "estimated_impressions": impressions,
        "estimated_cost": total_cost,
        "estimated_avg_cpc": avg_cpc,
        "estimated_ctr": round(ctr, 4) if ctr else None,
        "daily_estimates": daily,
        "keywords_used": len([kw for kw in keywords if kw.get("text")]),
        "insights": insights,
    }
