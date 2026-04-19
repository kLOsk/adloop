"""Google Ads read tools — campaign, ad, keyword, and search term performance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.ads.currency import get_currency_code

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def list_accounts(config: AdLoopConfig) -> dict:
    """List all accessible Google Ads accounts."""
    from adloop.ads.gaql import execute_query

    mcc_id = config.ads.login_customer_id
    if mcc_id:
        query = """
            SELECT customer_client.id, customer_client.descriptive_name,
                   customer_client.status, customer_client.manager
            FROM customer_client
        """
        rows = execute_query(config, mcc_id, query)
    else:
        query = """
            SELECT customer.id, customer.descriptive_name,
                   customer.status, customer.manager
            FROM customer
            LIMIT 1
        """
        rows = execute_query(config, config.ads.customer_id, query)

    return {"accounts": rows, "total_accounts": len(rows)}


def get_campaign_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get campaign-level performance metrics for the given date range."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type, campaign.bidding_strategy_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value,
               metrics.ctr, metrics.average_cpc
        FROM campaign
        WHERE campaign.status != 'REMOVED'
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(rows, currency_code)

    return {"campaigns": rows, "total_campaigns": len(rows)}


def get_ad_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad-level performance data including headlines, descriptions, and metrics."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.name, campaign.id, ad_group.name, ad_group.id,
               ad_group_ad.ad.id, ad_group_ad.ad.type,
               ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad.responsive_search_ad.descriptions,
               ad_group_ad.ad.final_urls,
               ad_group_ad.status,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.conversions, metrics.cost_micros
        FROM ad_group_ad
        WHERE ad_group_ad.status != 'REMOVED'
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(rows, currency_code)

    return {"ads": rows, "total_ads": len(rows)}


def get_keyword_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get keyword metrics including quality scores and competitive data."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.name, ad_group.name,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               ad_group_criterion.quality_info.quality_score,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.average_cpc, metrics.cost_micros,
               metrics.conversions
        FROM keyword_view
        WHERE ad_group_criterion.status != 'REMOVED'
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(rows, currency_code)

    return {"keywords": rows, "total_keywords": len(rows)}


def get_search_terms(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get search terms report — what users actually typed before clicking ads."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT search_term_view.search_term,
               campaign.name, ad_group.name,
               metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE segments.date DURING LAST_30_DAYS
          {f"AND segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'" if date_range_start and date_range_end else ""}
        ORDER BY metrics.clicks DESC
        LIMIT 200
    """
    # search_term_view requires an explicit date segment, so we always
    # include DURING LAST_30_DAYS as baseline and override if dates given.
    if date_range_start and date_range_end:
        query = f"""
            SELECT search_term_view.search_term,
                   campaign.name, ad_group.name,
                   metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'
            ORDER BY metrics.clicks DESC
            LIMIT 200
        """
    else:
        query = """
            SELECT search_term_view.search_term,
                   campaign.name, ad_group.name,
                   metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE segments.date DURING LAST_30_DAYS
            ORDER BY metrics.clicks DESC
            LIMIT 200
        """

    rows = execute_query(config, customer_id, query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(rows, currency_code)

    return {"search_terms": rows, "total_search_terms": len(rows)}


def get_negative_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List negative keywords for a campaign or all campaigns."""
    from adloop.ads.gaql import execute_query

    campaign_filter = ""
    if campaign_id:
        campaign_filter = f"AND campaign.id = {campaign_id}"

    query = f"""
        SELECT campaign.id, campaign.name,
               campaign_criterion.keyword.text,
               campaign_criterion.keyword.match_type,
               campaign_criterion.negative,
               campaign_criterion.criterion_id
        FROM campaign_criterion
        WHERE campaign_criterion.negative = TRUE
          AND campaign_criterion.status != 'REMOVED'
          {campaign_filter}
        ORDER BY campaign.name
    """

    rows = execute_query(config, customer_id, query)
    for row in rows:
        cid = row.get("campaign.id")
        crit_id = row.get("campaign_criterion.criterion_id")
        if cid and crit_id:
            row["resource_id"] = f"{cid}~{crit_id}"
    return {"negative_keywords": rows, "total_negative_keywords": len(rows)}


def get_negative_keyword_lists(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
) -> dict:
    """List all shared negative keyword lists (SharedSets) in the account.

    Returns each list's ID, name, status, and keyword count. Use this before
    calling propose_negative_keyword_list to check whether a suitable list
    already exists and only needs attaching to a new campaign.
    """
    from adloop.ads.gaql import execute_query

    query = """
        SELECT shared_set.id, shared_set.name, shared_set.status,
               shared_set.member_count, shared_set.resource_name
        FROM shared_set
        WHERE shared_set.type = 'NEGATIVE_KEYWORDS'
          AND shared_set.status != 'REMOVED'
        ORDER BY shared_set.name
    """

    rows = execute_query(config, customer_id, query)
    return {"negative_keyword_lists": rows, "total_lists": len(rows)}


def get_negative_keyword_list_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
) -> dict:
    """List the keywords inside a shared negative keyword list.

    shared_set_id: the numeric ID from get_negative_keyword_lists
    (shared_set.id field).
    """
    from adloop.ads.gaql import execute_query

    if not shared_set_id:
        return {"error": "shared_set_id is required"}
    if not shared_set_id.isdigit():
        return {"error": "shared_set_id must be a numeric ID"}

    query = f"""
        SELECT shared_criterion.criterion_id,
               shared_criterion.keyword.text,
               shared_criterion.keyword.match_type,
               shared_criterion.type,
               shared_set.id, shared_set.name
        FROM shared_criterion
        WHERE shared_set.id = {shared_set_id}
        ORDER BY shared_criterion.keyword.text
    """

    rows = execute_query(config, customer_id, query)
    for row in rows:
        ssid = row.get("shared_set.id")
        crit_id = row.get("shared_criterion.criterion_id")
        if ssid and crit_id:
            row["resource_id"] = f"{ssid}~{crit_id}"
    return {
        "keywords": rows,
        "total_keywords": len(rows),
        "shared_set_id": shared_set_id,
    }


def get_negative_keyword_list_campaigns(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
) -> dict:
    """List which campaigns a shared negative keyword list is attached to.

    shared_set_id: the numeric ID from get_negative_keyword_lists
    (shared_set.id field). Omit to return all list-to-campaign attachments.
    """
    from adloop.ads.gaql import execute_query

    shared_set_filter = ""
    if shared_set_id:
        if not shared_set_id.isdigit():
            return {"error": "shared_set_id must be a numeric ID"}
        shared_set_filter = f"AND shared_set.id = {shared_set_id}"

    query = f"""
        SELECT campaign.id, campaign.name, campaign.status,
               shared_set.id, shared_set.name
        FROM campaign_shared_set
        WHERE campaign_shared_set.status != 'REMOVED'
          {shared_set_filter}
        ORDER BY shared_set.name, campaign.name
    """

    rows = execute_query(config, customer_id, query)
    return {"attachments": rows, "total_attachments": len(rows)}


def get_recommendations(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    recommendation_types: list[str] | None = None,
    campaign_id: str = "",
) -> dict:
    """Retrieve Google's auto-generated recommendations with estimated impact.

    Uses the service directly (not ``execute_query``) because
    ``recommendation.impact`` sub-fields are not individually selectable
    in GAQL — the impact object must be extracted from the proto.
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    client = get_ads_client(config)
    service = client.get_service("GoogleAdsService")
    cid = normalize_customer_id(customer_id)

    type_filter = ""
    if recommendation_types:
        types_str = ", ".join(f"'{t}'" for t in recommendation_types)
        type_filter = f"AND recommendation.type IN ({types_str})"

    query = f"""
        SELECT recommendation.resource_name,
               recommendation.type,
               recommendation.campaign,
               recommendation.ad_group,
               recommendation.dismissed,
               recommendation.impact
        FROM recommendation
        WHERE recommendation.dismissed = FALSE
          {type_filter}
    """

    rows: list[dict] = []
    for row in service.search(customer_id=cid, query=query):
        rec = row.recommendation
        rec_type = rec.type_.name if hasattr(rec.type_, "name") else str(rec.type_)

        impact = rec.impact
        base = impact.base_metrics
        pot = impact.potential_metrics

        base_impressions = _round_metric(getattr(base, "impressions", 0))
        base_clicks = _round_metric(getattr(base, "clicks", 0))
        base_cost_micros = getattr(base, "cost_micros", 0) or 0
        base_conversions = _round_metric(getattr(base, "conversions", 0))

        pot_impressions = _round_metric(getattr(pot, "impressions", 0))
        pot_clicks = _round_metric(getattr(pot, "clicks", 0))
        pot_cost_micros = getattr(pot, "cost_micros", 0) or 0
        pot_conversions = _round_metric(getattr(pot, "conversions", 0))

        entry = {
            "recommendation.type": rec_type,
            "recommendation.campaign": rec.campaign,
            "recommendation.ad_group": rec.ad_group or "",
            "recommendation.dismissed": rec.dismissed,
            "impact.base": {
                "impressions": base_impressions,
                "clicks": base_clicks,
                "cost_micros": base_cost_micros,
                "cost": round(base_cost_micros / 1_000_000, 2),
                "conversions": base_conversions,
            },
            "impact.potential": {
                "impressions": pot_impressions,
                "clicks": pot_clicks,
                "cost_micros": pot_cost_micros,
                "cost": round(pot_cost_micros / 1_000_000, 2),
                "conversions": pot_conversions,
            },
            "estimated_improvement": {
                "impressions": _improvement(base_impressions, pot_impressions),
                "clicks": _improvement(base_clicks, pot_clicks),
                "cost": _improvement(
                    round(base_cost_micros / 1_000_000, 2),
                    round(pot_cost_micros / 1_000_000, 2),
                ),
                "conversions": _improvement(base_conversions, pot_conversions),
            },
        }
        rows.append(entry)

    if campaign_id:
        rows = [
            r for r in rows
            if str(campaign_id) in str(r.get("recommendation.campaign", ""))
        ]

    type_counts: dict[str, int] = {}
    for row in rows:
        rtype = row.get("recommendation.type", "UNKNOWN")
        type_counts[rtype] = type_counts.get(rtype, 0) + 1

    _BUDGET_TYPES = {
        "CAMPAIGN_BUDGET", "MOVE_UNUSED_BUDGET",
        "FORECASTING_CAMPAIGN_BUDGET", "MARGINAL_ROI_CAMPAIGN_BUDGET",
    }

    insights: list[str] = []
    if not rows:
        insights.append("No active recommendations found.")
    else:
        insights.append(
            f"{len(rows)} active recommendation(s) across {len(type_counts)} type(s): "
            f"{dict(sorted(type_counts.items(), key=lambda x: -x[1]))}"
        )

        budget_recs = [r for r in rows if r.get("recommendation.type") in _BUDGET_TYPES]
        if budget_recs:
            insights.append(
                f"{len(budget_recs)} recommendation(s) are budget-related. "
                f"Google often suggests spending more — cross-reference with actual "
                f"conversion data before accepting."
            )

        high_impact = [
            r for r in rows
            if (r.get("estimated_improvement", {}).get("conversions", 0) or 0) > 1
        ]
        if high_impact:
            types = set(r.get("recommendation.type") for r in high_impact)
            insights.append(
                f"{len(high_impact)} recommendation(s) estimate >1 additional conversion: "
                f"types {types}. Validate against your actual CPA before acting."
            )

    return {
        "recommendations": rows,
        "total_recommendations": len(rows),
        "by_type": type_counts,
        "insights": insights,
    }


def get_audience_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
) -> dict:
    """Get audience segment performance metrics (remarketing, in-market, affinity, demographics)."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    campaign_filter = ""
    if campaign_id:
        campaign_filter = f"AND campaign.id = {campaign_id}"

    query = f"""
        SELECT campaign.id, campaign.name,
               campaign.advertising_channel_type,
               ad_group.id, ad_group.name,
               ad_group_criterion.display_name,
               ad_group_criterion.type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.ctr, metrics.average_cpc
        FROM ad_group_audience_view
        WHERE campaign.status != 'REMOVED'
          {date_clause}
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT 200
    """

    rows = execute_query(config, customer_id, query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(rows, currency_code)

    insights: list[str] = []
    if not rows:
        insights.append(
            "No audience performance data found. This account's campaigns may not "
            "have explicit audience targeting (remarketing lists, in-market segments, "
            "demographics). PMax audience targeting is automatic and does not appear "
            "in this report."
        )

    return {"audiences": rows, "total_audiences": len(rows), "insights": insights}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _round_metric(value: object) -> float:
    """Round an API metric to 4 decimal places, collapsing near-zero to 0."""
    v = float(value or 0)
    r = round(v, 4)
    return 0.0 if abs(r) < 0.0001 else r


def _improvement(base: float, potential: float) -> float | None:
    """Compute estimated improvement, returning None when Google has no estimate.

    The API returns 0 for potential when it doesn't have a projection for that
    metric. Naively subtracting would produce a misleading negative number.
    """
    if potential == 0 and base != 0:
        return None
    return round(potential - base, 4)


def _date_clause(start: str, end: str) -> str:
    """Build a GAQL date WHERE fragment."""
    if start and end:
        return f"AND segments.date BETWEEN '{start}' AND '{end}'"
    return "AND segments.date DURING LAST_30_DAYS"


def _enrich_cost_fields(rows: list[dict], currency_code: str = "EUR") -> None:
    """Add human-readable cost and CPA fields computed from cost_micros."""
    for row in rows:
        cost_micros = row.get("metrics.cost_micros", 0) or 0
        row["metrics.cost"] = round(cost_micros / 1_000_000, 2)

        conversions = row.get("metrics.conversions", 0) or 0
        if conversions > 0:
            row["metrics.cpa"] = round(cost_micros / 1_000_000 / conversions, 2)

        avg_cpc_micros = row.get("metrics.average_cpc", 0) or 0
        if avg_cpc_micros:
            row["metrics.average_cpc_amount"] = round(avg_cpc_micros / 1_000_000, 2)

        row["metrics.currency"] = currency_code
