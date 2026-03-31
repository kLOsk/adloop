"""Performance Max read tools — campaign, asset group, and asset performance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.ads.currency import get_currency_code
from adloop.ads.read import _date_clause, _enrich_cost_fields

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def get_pmax_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get Performance Max campaign metrics with network breakdown and asset group ad strength."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    campaign_query = f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.bidding_strategy_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value,
               metrics.ctr, metrics.average_cpc,
               segments.ad_network_type
        FROM campaign
        WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
          AND campaign.status != 'REMOVED'
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    campaigns = execute_query(config, customer_id, campaign_query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(campaigns, currency_code)

    asset_group_query = f"""
        SELECT asset_group.id, asset_group.name, asset_group.status,
               asset_group.ad_strength, asset_group.campaign,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM asset_group
        WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    asset_groups = execute_query(config, customer_id, asset_group_query)
    _enrich_cost_fields(asset_groups, currency_code)

    insights: list[str] = []

    poor_strength = [
        ag for ag in asset_groups
        if ag.get("asset_group.ad_strength") in ("POOR", "AVERAGE")
    ]
    if poor_strength:
        names = ", ".join(
            ag.get("asset_group.name", "?") for ag in poor_strength[:5]
        )
        insights.append(
            f"{len(poor_strength)} asset group(s) have POOR or AVERAGE ad strength: "
            f"{names}. Add more diverse assets (headlines, descriptions, images) to improve."
        )

    zero_conv_groups = [
        ag for ag in asset_groups
        if (ag.get("metrics.clicks", 0) or 0) > 10
        and (ag.get("metrics.conversions", 0) or 0) == 0
    ]
    if zero_conv_groups:
        names = ", ".join(
            ag.get("asset_group.name", "?") for ag in zero_conv_groups[:5]
        )
        insights.append(
            f"{len(zero_conv_groups)} asset group(s) have clicks but zero conversions: {names}."
        )

    network_counts: dict[str, int] = {}
    for c in campaigns:
        net = c.get("segments.ad_network_type", "UNKNOWN")
        network_counts[net] = network_counts.get(net, 0) + (
            c.get("metrics.impressions", 0) or 0
        )
    if network_counts:
        total_impr = sum(network_counts.values()) or 1
        dist = {
            k: f"{round(v / total_impr * 100, 1)}%"
            for k, v in sorted(network_counts.items(), key=lambda x: -x[1])
        }
        insights.append(f"PMax network distribution by impressions: {dist}")

    if not campaigns:
        insights.append("No Performance Max campaigns found in this account.")

    return {
        "campaigns": campaigns,
        "asset_groups": asset_groups,
        "total_campaigns": len(campaigns),
        "total_asset_groups": len(asset_groups),
        "insights": insights,
    }


def get_asset_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Get per-asset details for Performance Max asset groups.

    Returns each asset's field type, status, primary serving status, and
    content. ``primary_status`` shows whether the asset is eligible to serve
    (ELIGIBLE, NOT_ELIGIBLE, PAUSED, PENDING).

    Note: the Google Ads API v23 does not expose per-asset performance labels
    (BEST/GOOD/LOW) for PMax assets via GAQL. Use ``get_detailed_asset_performance``
    to see which asset *combinations* Google selects most — that's the closest
    proxy for individual asset quality.
    """
    from adloop.ads.gaql import execute_query

    campaign_filter = ""
    if campaign_id:
        campaign_filter = f"AND campaign.id = {campaign_id}"

    query = f"""
        SELECT asset_group_asset.asset, asset_group_asset.field_type,
               asset_group_asset.status, asset_group_asset.primary_status,
               asset_group.id, asset_group.name,
               campaign.id, campaign.name,
               asset.name, asset.type,
               asset.text_asset.text,
               asset.image_asset.full_size.url
        FROM asset_group_asset
        WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
          {campaign_filter}
    """

    rows = execute_query(config, customer_id, query)

    status_counts: dict[str, int] = {}
    field_type_counts: dict[str, int] = {}
    for row in rows:
        status = row.get("asset_group_asset.primary_status", "UNSPECIFIED")
        status_counts[status] = status_counts.get(status, 0) + 1
        ftype = row.get("asset_group_asset.field_type", "UNSPECIFIED")
        field_type_counts[ftype] = field_type_counts.get(ftype, 0) + 1

    insights: list[str] = []
    if not rows:
        insights.append(
            "No PMax assets found. This account has no Performance Max campaigns, "
            "or the specified campaign_id does not match a PMax campaign."
        )
    else:
        not_eligible = [
            r for r in rows
            if r.get("asset_group_asset.primary_status") == "NOT_ELIGIBLE"
        ]
        if not_eligible:
            insights.append(
                f"{len(not_eligible)} asset(s) are NOT_ELIGIBLE to serve — "
                f"check policy status or asset quality."
            )

    return {
        "assets": rows,
        "total_assets": len(rows),
        "by_status": status_counts,
        "by_field_type": field_type_counts,
        "insights": insights,
    }


def get_detailed_asset_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Get top-performing asset combinations for Performance Max campaigns.

    Uses the ``asset_group_top_combination_view`` resource directly (not
    ``execute_query``) because the nested proto structure requires custom
    extraction.
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    client = get_ads_client(config)
    service = client.get_service("GoogleAdsService")
    cid = normalize_customer_id(customer_id)

    campaign_filter = ""
    if campaign_id:
        campaign_filter = f"AND campaign.id = {campaign_id}"

    query = f"""
        SELECT asset_group_top_combination_view.asset_group_top_combinations,
               campaign.id, campaign.name,
               asset_group.id, asset_group.name
        FROM asset_group_top_combination_view
        WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
          {campaign_filter}
    """

    combinations: list[dict] = []
    for row in service.search(customer_id=cid, query=query):
        campaign_name = row.campaign.name
        campaign_id_val = row.campaign.id
        ag_name = row.asset_group.name
        ag_id = row.asset_group.id

        top_combos = row.asset_group_top_combination_view.asset_group_top_combinations
        for combo in top_combos:
            assets_in_combo: list[dict] = []
            for served_info in getattr(combo, "asset_combination_serving_infos", []):
                for asset_usage in getattr(served_info, "served_assets", []):
                    field_type = getattr(asset_usage, "served_asset_field_type", None)
                    assets_in_combo.append({
                        "asset": getattr(asset_usage, "asset", ""),
                        "field_type": (
                            field_type.name
                            if hasattr(field_type, "name")
                            else str(field_type)
                        ),
                    })

            if assets_in_combo:
                combinations.append({
                    "campaign.id": campaign_id_val,
                    "campaign.name": campaign_name,
                    "asset_group.id": ag_id,
                    "asset_group.name": ag_name,
                    "assets": assets_in_combo,
                })

    insights: list[str] = []
    if not combinations:
        insights.append(
            "No top asset combinations found. This account has no Performance Max "
            "campaigns, or the specified campaign_id does not match a PMax campaign."
        )

    return {
        "top_combinations": combinations,
        "total_combinations": len(combinations),
        "insights": insights,
    }
