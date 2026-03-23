"""Cross-reference tools — combine Google Ads and GA4 data for unified insights."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def _default_date_range(
    start: str, end: str
) -> tuple[str, str]:
    """Return (start, end) as YYYY-MM-DD strings, defaulting to last 30 days."""
    if not start or not end:
        today = date.today()
        return (today - timedelta(days=30)).isoformat(), today.isoformat()
    return start, end


def _safe_div(numerator: float, denominator: float) -> float | None:
    """Divide or return None when denominator is zero."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _safe_int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _safe_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Tool 1: analyze_campaign_conversions
# ---------------------------------------------------------------------------


def analyze_campaign_conversions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    property_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_name: str = "",
) -> dict:
    """Campaign clicks -> GA4 conversions mapping.

    Combines Google Ads campaign metrics with GA4 session/conversion data to
    reveal the real cost-per-conversion (using GA4 as source of truth) and
    detect GDPR consent gaps via click-to-session ratios.
    """
    from adloop.ads.currency import format_currency, get_currency_code
    from adloop.ads.read import get_campaign_performance
    from adloop.ga4.reports import run_ga4_report

    start, end = _default_date_range(date_range_start, date_range_end)
    currency_code = get_currency_code(config, customer_id)

    ads_result = get_campaign_performance(
        config, customer_id=customer_id, date_range_start=start, date_range_end=end
    )
    if "error" in ads_result:
        return ads_result

    ga4_result = run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["sessionCampaignName", "sessionSource", "sessionMedium"],
        metrics=["sessions", "conversions", "engagedSessions", "totalUsers"],
        date_range_start=start,
        date_range_end=end,
        limit=1000,
    )
    if "error" in ga4_result:
        return ga4_result

    # Index GA4 rows by (campaign, source, medium)
    paid_by_campaign: dict[str, dict] = {}
    non_paid: dict[tuple[str, str], dict] = {}

    for row in ga4_result.get("rows", []):
        campaign = row.get("sessionCampaignName", "(not set)")
        source = row.get("sessionSource", "")
        medium = row.get("sessionMedium", "")
        sessions = _safe_int(row.get("sessions", 0))
        conversions = _safe_int(row.get("conversions", 0))
        engaged = _safe_int(row.get("engagedSessions", 0))

        is_paid = source == "google" and medium == "cpc"

        if is_paid:
            bucket = paid_by_campaign.setdefault(campaign, {
                "sessions": 0, "conversions": 0, "engaged": 0,
            })
            bucket["sessions"] += sessions
            bucket["conversions"] += conversions
            bucket["engaged"] += engaged
        else:
            key = (source, medium)
            bucket = non_paid.setdefault(key, {"sessions": 0, "conversions": 0})
            bucket["sessions"] += sessions
            bucket["conversions"] += conversions

    campaigns = []
    insights = []

    for camp in ads_result.get("campaigns", []):
        name = camp.get("campaign.name", "")
        if campaign_name and campaign_name.lower() not in name.lower():
            continue

        ads_clicks = _safe_int(camp.get("metrics.clicks", 0))
        ads_cost = _safe_float(camp.get("metrics.cost", 0))
        ads_conversions = _safe_float(camp.get("metrics.conversions", 0))

        ga4 = paid_by_campaign.get(name, {"sessions": 0, "conversions": 0, "engaged": 0})
        ga4_sessions = ga4["sessions"]
        ga4_conversions = ga4["conversions"]

        ratio = _safe_div(ads_clicks, ga4_sessions)
        conv_rate = _safe_div(ga4_conversions, ga4_sessions)
        cost_per_conv = _safe_div(ads_cost, ga4_conversions)

        entry = {
            "campaign_name": name,
            "campaign_status": camp.get("campaign.status", ""),
            "ads_clicks": ads_clicks,
            "ads_cost": ads_cost,
            "ads_conversions": ads_conversions,
            "ga4_paid_sessions": ga4_sessions,
            "ga4_paid_conversions": ga4_conversions,
            "click_to_session_ratio": ratio,
            "ga4_conversion_rate": conv_rate,
            "cost_per_ga4_conversion": cost_per_conv,
        }
        campaigns.append(entry)

        if ratio is not None and ratio > 2.0 and ads_clicks > 5:
            lost_pct = round((1 - 1 / ratio) * 100)
            insights.append(
                f"GDPR: click-to-session ratio is {ratio:.1f}:1 for '{name}' "
                f"— ~{lost_pct}% of clicks not tracked in GA4 (likely consent rejection)"
            )

        if ads_cost > 0 and ga4_conversions == 0 and ads_conversions == 0:
            insights.append(
                f"Zero conversions for '{name}' despite {format_currency(ads_cost, currency_code)} spend "
                f"— check conversion tracking setup in both Google Ads and GA4"
            )

        if ads_conversions > 0 and ga4_conversions == 0:
            insights.append(
                f"Ads reports {ads_conversions} conversions for '{name}' but GA4 shows 0 "
                f"from paid traffic — possible attribution model mismatch"
            )

    non_paid_channels = []
    for (source, medium), data in sorted(non_paid.items(), key=lambda x: -x[1]["sessions"]):
        s = data["sessions"]
        c = data["conversions"]
        non_paid_channels.append({
            "source": source,
            "medium": medium,
            "sessions": s,
            "conversions": c,
            "conversion_rate": _safe_div(c, s),
        })

    return {
        "campaigns": campaigns,
        "non_paid_channels": non_paid_channels,
        "insights": insights,
        "date_range": {"start": start, "end": end},
    }


# ---------------------------------------------------------------------------
# Tool 2: landing_page_analysis
# ---------------------------------------------------------------------------


def landing_page_analysis(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    property_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Analyze landing page performance by combining ad final URLs with GA4 page data.

    Shows which landing pages receive ad traffic, their conversion rates, bounce
    rates, and identifies pages that get clicks but don't convert.
    """
    from adloop.ads.read import get_ad_performance
    from adloop.ga4.reports import run_ga4_report

    start, end = _default_date_range(date_range_start, date_range_end)

    ads_result = get_ad_performance(
        config, customer_id=customer_id, date_range_start=start, date_range_end=end
    )
    if "error" in ads_result:
        return ads_result

    ga4_result = run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["pagePath", "sessionSource", "sessionMedium"],
        metrics=["sessions", "conversions", "engagedSessions", "bounceRate"],
        date_range_start=start,
        date_range_end=end,
        limit=1000,
    )
    if "error" in ga4_result:
        return ga4_result

    # Build map: path -> list of ads pointing there
    ads_by_path: dict[str, list[dict]] = {}
    for ad in ads_result.get("ads", []):
        urls = ad.get("ad_group_ad.ad.final_urls", [])
        if isinstance(urls, str):
            urls = [urls]
        for url in urls:
            path = urlparse(url).path or "/"
            path = path.rstrip("/") or "/"
            ads_by_path.setdefault(path, []).append({
                "ad_id": str(ad.get("ad_group_ad.ad.id", "")),
                "campaign": ad.get("campaign.name", ""),
                "ad_group": ad.get("ad_group.name", ""),
                "clicks": _safe_int(ad.get("metrics.clicks", 0)),
                "cost": _safe_float(ad.get("metrics.cost", 0)),
            })

    # Build map: path -> GA4 paid metrics
    ga4_by_path: dict[str, dict] = {}
    for row in ga4_result.get("rows", []):
        source = row.get("sessionSource", "")
        medium = row.get("sessionMedium", "")
        if source != "google" or medium != "cpc":
            continue
        path = row.get("pagePath", "/")
        bucket = ga4_by_path.setdefault(path, {
            "sessions": 0, "conversions": 0, "engaged": 0, "bounce_rate_sum": 0.0, "count": 0,
        })
        bucket["sessions"] += _safe_int(row.get("sessions", 0))
        bucket["conversions"] += _safe_int(row.get("conversions", 0))
        bucket["engaged"] += _safe_int(row.get("engagedSessions", 0))
        bucket["bounce_rate_sum"] += _safe_float(row.get("bounceRate", 0))
        bucket["count"] += 1

    all_paths = set(ads_by_path.keys()) | set(ga4_by_path.keys())

    landing_pages = []
    orphaned = []
    insights = []

    for path in sorted(all_paths):
        ads_list = ads_by_path.get(path, [])
        ga4 = ga4_by_path.get(path, {"sessions": 0, "conversions": 0, "engaged": 0, "bounce_rate_sum": 0.0, "count": 0})

        total_ad_clicks = sum(a["clicks"] for a in ads_list)
        total_ad_cost = sum(a["cost"] for a in ads_list)
        ga4_sessions = ga4["sessions"]
        ga4_conversions = ga4["conversions"]

        conv_rate = _safe_div(ga4_conversions, ga4_sessions)
        bounce = round(ga4["bounce_rate_sum"] / ga4["count"], 4) if ga4["count"] else None
        engagement = _safe_div(ga4["engaged"], ga4_sessions)

        entry = {
            "page_path": path,
            "ads_pointing_here": ads_list if ads_list else None,
            "total_ad_clicks": total_ad_clicks,
            "total_ad_cost": round(total_ad_cost, 2),
            "ga4_paid_sessions": ga4_sessions,
            "ga4_paid_conversions": ga4_conversions,
            "conversion_rate": conv_rate,
            "bounce_rate": bounce,
            "engagement_rate": engagement,
        }
        landing_pages.append(entry)

        if ads_list and ga4_sessions == 0 and total_ad_clicks > 0:
            orphaned.append(path)
            insights.append(
                f"'{path}' receives ad clicks ({total_ad_clicks}) but has 0 GA4 paid sessions "
                f"— GDPR consent may be blocking all tracking, or the page redirects"
            )

        if ga4_sessions > 10 and ga4_conversions == 0:
            insights.append(
                f"'{path}' has {ga4_sessions} paid sessions but 0 conversions "
                f"— landing page conversion problem"
            )

        if bounce is not None and bounce > 0.70 and ga4_sessions > 5:
            insights.append(
                f"'{path}' has {bounce:.0%} bounce rate from paid traffic "
                f"— ad message may not match page content"
            )

    landing_pages.sort(key=lambda p: -(p["ga4_paid_sessions"] or 0))

    return {
        "landing_pages": landing_pages,
        "orphaned_ad_urls": orphaned,
        "insights": insights,
        "date_range": {"start": start, "end": end},
    }


# ---------------------------------------------------------------------------
# Tool 3: attribution_check
# ---------------------------------------------------------------------------


def attribution_check(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    property_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    conversion_events: list[str] | None = None,
) -> dict:
    """Compare Ads-reported conversions vs GA4 conversion events.

    Identifies discrepancies between the two systems and diagnoses whether
    they're caused by GDPR consent, attribution model differences, or
    misconfigured conversion actions.
    """
    from adloop.ads.currency import format_currency, get_currency_code
    from adloop.ads.read import get_campaign_performance
    from adloop.ga4.reports import run_ga4_report

    start, end = _default_date_range(date_range_start, date_range_end)
    currency_code = get_currency_code(config, customer_id)

    ads_result = get_campaign_performance(
        config, customer_id=customer_id, date_range_start=start, date_range_end=end
    )
    if "error" in ads_result:
        return ads_result

    ga4_events_result = run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["eventName"],
        metrics=["eventCount"],
        date_range_start=start,
        date_range_end=end,
        limit=500,
    )
    if "error" in ga4_events_result:
        return ga4_events_result

    ga4_source_result = run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["sessionSource", "sessionMedium"],
        metrics=["sessions", "conversions"],
        date_range_start=start,
        date_range_end=end,
        limit=200,
    )
    if "error" in ga4_source_result:
        return ga4_source_result

    # Ads totals
    ads_total_conversions = sum(
        _safe_float(c.get("metrics.conversions", 0))
        for c in ads_result.get("campaigns", [])
    )
    ads_total_cost = sum(
        _safe_float(c.get("metrics.cost", 0))
        for c in ads_result.get("campaigns", [])
    )
    ads_total_clicks = sum(
        _safe_int(c.get("metrics.clicks", 0))
        for c in ads_result.get("campaigns", [])
    )

    # GA4 events index
    event_index: dict[str, int] = {}
    for row in ga4_events_result.get("rows", []):
        name = row.get("eventName", "")
        count = _safe_int(row.get("eventCount", 0))
        event_index[name] = event_index.get(name, 0) + count

    # GA4 conversions by source
    ga4_paid_conversions = 0
    ga4_paid_sessions = 0
    ga4_all_conversions = 0
    by_source = []

    for row in ga4_source_result.get("rows", []):
        source = row.get("sessionSource", "")
        medium = row.get("sessionMedium", "")
        sessions = _safe_int(row.get("sessions", 0))
        conversions = _safe_int(row.get("conversions", 0))

        ga4_all_conversions += conversions

        if source == "google" and medium == "cpc":
            ga4_paid_conversions += conversions
            ga4_paid_sessions += sessions

        by_source.append({
            "source": source,
            "medium": medium,
            "sessions": sessions,
            "conversions": conversions,
        })

    by_source.sort(key=lambda x: -x["sessions"])

    # Check requested conversion events
    events_to_check = conversion_events or []
    conversion_event_details = []
    for ev in events_to_check:
        count = event_index.get(ev, 0)
        conversion_event_details.append({
            "event_name": ev,
            "total_count": count,
            "exists": count > 0,
        })

    # Discrepancy
    denom = max(ads_total_conversions, ga4_paid_conversions, 1)
    discrepancy_pct = round(
        abs(ads_total_conversions - ga4_paid_conversions) / denom * 100, 1
    )

    # Insights
    insights = []

    if ads_total_conversions == 0 and ga4_paid_conversions == 0:
        if ads_total_cost > 0:
            insights.append(
                f"Zero conversions in both Google Ads and GA4 despite "
                f"{format_currency(ads_total_cost, currency_code)} ad spend — conversion tracking is likely "
                f"not configured or conversion actions are not linked to campaigns"
            )
    elif ads_total_conversions > 0 and ga4_paid_conversions == 0:
        insights.append(
            f"Google Ads reports {ads_total_conversions} conversions but GA4 shows 0 "
            f"from paid traffic — possible causes: GDPR consent blocking GA4, "
            f"different attribution models, or GA4 conversion events not marked as conversions"
        )
    elif ads_total_conversions == 0 and ga4_paid_conversions > 0:
        insights.append(
            f"GA4 shows {ga4_paid_conversions} conversions from paid traffic but "
            f"Google Ads reports 0 — conversion actions may not be imported into Google Ads"
        )
    elif discrepancy_pct > 20:
        insights.append(
            f"Attribution discrepancy: Ads reports {ads_total_conversions} conversions "
            f"vs GA4 {ga4_paid_conversions} from paid ({discrepancy_pct}% difference) "
            f"— expected causes: GDPR consent gaps, attribution window differences "
            f"(Ads: 30-day click, GA4: data-driven)"
        )

    click_session_ratio = _safe_div(ads_total_clicks, ga4_paid_sessions)
    if click_session_ratio is not None and click_session_ratio > 2.0 and ads_total_clicks > 10:
        lost_pct = round((1 - 1 / click_session_ratio) * 100)
        insights.append(
            f"Overall click-to-session ratio is {click_session_ratio:.1f}:1 "
            f"— ~{lost_pct}% of ad clicks are not tracked in GA4 (GDPR consent)"
        )

    for ev_detail in conversion_event_details:
        if not ev_detail["exists"]:
            insights.append(
                f"Conversion event '{ev_detail['event_name']}' has zero occurrences "
                f"in GA4 for this period — the event may not be firing or is misconfigured"
            )
        elif ev_detail["total_count"] > 0 and ga4_paid_conversions == 0:
            insights.append(
                f"Event '{ev_detail['event_name']}' fires {ev_detail['total_count']}x "
                f"but none from paid traffic — users may convert through other channels, "
                f"or the event is not marked as a conversion in GA4"
            )

    return {
        "ads_total_conversions": ads_total_conversions,
        "ads_total_cost": ads_total_cost,
        "ads_total_clicks": ads_total_clicks,
        "ga4_paid_conversions": ga4_paid_conversions,
        "ga4_paid_sessions": ga4_paid_sessions,
        "ga4_all_conversions": ga4_all_conversions,
        "discrepancy_pct": discrepancy_pct,
        "conversion_events": conversion_event_details if conversion_event_details else None,
        "all_ga4_events": [
            {"event_name": k, "count": v}
            for k, v in sorted(event_index.items(), key=lambda x: -x[1])
        ],
        "by_source": by_source,
        "insights": insights,
        "date_range": {"start": start, "end": end},
    }
