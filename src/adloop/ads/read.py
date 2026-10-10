"""Google Ads read tools — campaign, ad, keyword, and search term performance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.ads.currency import get_currency_code

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def list_accounts(config: AdLoopConfig, *, limit: int = 200) -> dict:
    """List accessible Google Ads accounts, up to *limit* entries.

    The default of 200 covers the vast majority of agency MCCs in a single
    response. Larger MCCs (300+ accounts) can still hit per-response size
    caps or per-tool-call timeouts on some MCP hosts, so the cap is kept —
    when the user genuinely wants the full list, raise *limit* explicitly
    (e.g. ``list_accounts(limit=1000)``) or pass ``customer_id`` directly
    to other tools (``get_campaign_performance``, ``run_gaql``, etc.) to
    query a specific account without enumerating the whole MCC.
    """
    from adloop.ads.gaql import execute_query

    mcc_id = config.ads.login_customer_id
    if mcc_id:
        query = f"""
            SELECT customer_client.id, customer_client.descriptive_name,
                   customer_client.status, customer_client.manager
            FROM customer_client
            LIMIT {int(limit) + 1}
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

    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]

    result: dict = {"accounts": rows, "total_accounts": len(rows)}
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Returned the first {limit} accounts but more exist on this MCC. "
            f"If the user asked to see all of their accounts, call this tool "
            f"again with a much higher limit (e.g. list_accounts(limit=1000)). "
            f"If you only need one specific account, skip listing entirely "
            f"and pass customer_id directly to get_campaign_performance, "
            f"run_gaql, or whichever tool you actually need."
        )
    return result


def get_campaign_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
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
    share_error = _attach_impression_share(config, customer_id, rows, date_clause)
    budget_limited = _budget_limited_converters(rows)

    share_insights: list[str] = []
    if budget_limited:
        names = ", ".join(
            f"{r['name']} ({r['search_budget_lost_impression_share_pct']}% lost to budget)"
            for r in budget_limited[:5]
        )
        share_insights.append(
            f"{len(budget_limited)} converting campaign(s) lose at least "
            f"{int(_BUDGET_LOST_FLAG * 100)}% of Search impression share to "
            f"budget: {names}. They run out of budget while converting; "
            f"compare their CPA with the account before moving budget to them."
        )

    if not compact:
        result: dict = {
            "campaigns": rows,
            "total_campaigns": len(rows),
            "impression_share_note": _IMPRESSION_SHARE_NOTE,
        }
        if share_error:
            result["impression_share_error"] = share_error
        if share_insights:
            result["budget_limited_converters"] = budget_limited[:10]
            result["insights"] = share_insights
        return result

    zero_conv = [
        r for r in rows
        if (r.get("metrics.cost_micros") or 0) > 0
        and not (r.get("metrics.conversions") or 0)
    ]
    insights: list[str] = []
    if zero_conv:
        wasted = round(
            sum((r.get("metrics.cost_micros") or 0) for r in zero_conv) / 1_000_000, 2
        )
        names = ", ".join(str(r.get("campaign.name")) for r in zero_conv[:5])
        insights.append(
            f"{len(zero_conv)} campaign(s) spent {wasted} {currency_code} with "
            f"ZERO conversions: {names}. Investigate tracking and relevance "
            f"before optimizing anything else."
        )
    insights.extend(share_insights)

    top = rows[:10]
    result = {
        "compact": True,
        "total_campaigns": len(rows),
        "totals": _compact_totals(rows, currency_code),
        "by_status": _status_counts(rows, "campaign.status"),
        "by_channel_type": _status_counts(rows, "campaign.advertising_channel_type"),
        "campaigns_top_spend": top,
        "zero_conversion_spenders": [
            {
                "name": r.get("campaign.name"),
                "id": r.get("campaign.id"),
                "cost": r.get("metrics.cost"),
                "clicks": r.get("metrics.clicks"),
            }
            for r in zero_conv[:5]
        ],
        "budget_limited_converters": budget_limited[:5],
        "insights": insights,
        "impression_share_note": _IMPRESSION_SHARE_NOTE,
        "note": _compact_note(len(top), len(rows), "get_campaign_performance"),
    }
    if share_error:
        result["impression_share_error"] = share_error
    return result


# Search impression share only exists for campaigns that serve on the Search
# Network. For every other channel the API has nothing to report, and a
# proto3 double that was never set reads back as 0.0, which would look like
# "zero share" rather than "does not apply".
_IMPRESSION_SHARE_CHANNELS = ("SEARCH", "SHOPPING")
_IMPRESSION_SHARE_FIELDS = (
    "metrics.search_impression_share",
    "metrics.search_budget_lost_impression_share",
    "metrics.search_rank_lost_impression_share",
)
_BUDGET_LOST_FLAG = 0.2
_IMPRESSION_SHARE_NOTE = (
    "Search impression share fields are fractions from the API (0.25 = 25%); "
    "the matching *_pct fields carry the same value as a percentage. Google "
    "reports impression share below 10% as 0.0999 and lost share above 90% as "
    "0.9001, so read those as '<10%' and '>90%'. Null means the metric does "
    "not apply: the campaign is not a Search or Shopping campaign, or it had "
    "no impressions in the range."
)


def _attach_impression_share(
    config: AdLoopConfig, customer_id: str, rows: list[dict], date_clause: str
) -> str | None:
    """Merge Search impression share onto campaign rows, null where it does not apply.

    Runs as its own query, restricted to the channels that report it, so the
    main performance query (and every non-Search campaign in it) stays exactly
    as it was. A failure here costs the share columns, never the report:
    the error comes back as a string for the caller to surface.
    """
    from adloop.ads.gaql import _parse_gaql_error, execute_query

    channels = ", ".join(f"'{c}'" for c in _IMPRESSION_SHARE_CHANNELS)
    query = f"""
        SELECT campaign.id, metrics.impressions,
               {", ".join(_IMPRESSION_SHARE_FIELDS)}
        FROM campaign
        WHERE campaign.status != 'REMOVED'
          AND campaign.advertising_channel_type IN ({channels})
          {date_clause}
    """

    error: str | None = None
    share_by_id: dict[str, dict] = {}
    try:
        for r in execute_query(config, customer_id, query):
            share_by_id[str(r.get("campaign.id"))] = r
    except Exception as exc:  # noqa: BLE001 - report it, keep the main rows
        error = (
            "Search impression share could not be loaded: "
            f"{_parse_gaql_error(exc)}"
        )

    for row in rows:
        share = share_by_id.get(str(row.get("campaign.id")))
        applies = (
            share is not None
            and str(row.get("campaign.advertising_channel_type"))
            in _IMPRESSION_SHARE_CHANNELS
            and (share.get("metrics.impressions") or 0) > 0
        )
        for field in _IMPRESSION_SHARE_FIELDS:
            value = share.get(field) if applies and share else None
            value = round(float(value), 4) if value is not None else None
            row[field] = value
            row[f"{field}_pct"] = round(value * 100, 1) if value is not None else None

    return error


def _budget_limited_converters(rows: list[dict]) -> list[dict]:
    """Converting campaigns that lose a significant share of Search to budget."""
    limited = [
        r for r in rows
        if (r.get("metrics.search_budget_lost_impression_share") or 0)
        >= _BUDGET_LOST_FLAG
        and (r.get("metrics.conversions") or 0) > 0
    ]
    limited.sort(
        key=lambda r: r.get("metrics.search_budget_lost_impression_share") or 0,
        reverse=True,
    )
    return [
        {
            "name": r.get("campaign.name"),
            "id": r.get("campaign.id"),
            "conversions": r.get("metrics.conversions"),
            "cost": r.get("metrics.cost"),
            "cpa": r.get("metrics.cpa"),
            "search_impression_share_pct": r.get(
                "metrics.search_impression_share_pct"
            ),
            "search_budget_lost_impression_share_pct": r.get(
                "metrics.search_budget_lost_impression_share_pct"
            ),
        }
        for r in limited
    ]


def get_ad_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
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
               ad_group_ad.policy_summary.approval_status,
               ad_group_ad.policy_summary.review_status,
               ad_group_ad.policy_summary.policy_topic_entries,
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
    for r in rows:
        _compact_policy_topics(r)
    policy_issues = _policy_issues(rows)

    policy_insights: list[str] = []
    for status, label in (
        ("DISAPPROVED", "are DISAPPROVED and cannot serve"),
        ("APPROVED_LIMITED", "are APPROVED_LIMITED (serving restricted by policy)"),
    ):
        flagged = [p for p in policy_issues if p["approval_status"] == status]
        if not flagged:
            continue
        spend = round(sum(p["cost"] or 0 for p in flagged), 2)
        topics = sorted({t for p in flagged for t in p["policy_topics"]})
        topic_text = f" Policy topics: {', '.join(topics[:8])}." if topics else ""
        policy_insights.append(
            f"{len(flagged)} ad(s) {label}; they spent {spend} {currency_code} "
            f"in this range.{topic_text}"
        )

    if not compact:
        result: dict = {"ads": rows, "total_ads": len(rows)}
        if policy_issues:
            result["policy_issues"] = policy_issues
            result["insights"] = policy_insights
        return result

    def _asset_count(row: dict, field: str) -> int:
        assets = row.get(field) or []
        return len(assets) if isinstance(assets, list) else 0

    incomplete_rsas: list[dict] = []
    ads_per_group: dict[str, dict] = {}
    for r in rows:
        group_key = str(r.get("ad_group.id"))
        entry = ads_per_group.setdefault(
            group_key, {"name": r.get("ad_group.name"), "enabled_ads": 0}
        )
        if str(r.get("ad_group_ad.status")) == "ENABLED":
            entry["enabled_ads"] += 1

        if str(r.get("ad_group_ad.ad.type")) == "RESPONSIVE_SEARCH_AD":
            headlines = _asset_count(r, "ad_group_ad.ad.responsive_search_ad.headlines")
            descriptions = _asset_count(
                r, "ad_group_ad.ad.responsive_search_ad.descriptions"
            )
            if headlines < 8 or descriptions < 3:
                incomplete_rsas.append({
                    "ad_id": r.get("ad_group_ad.ad.id"),
                    "ad_group": r.get("ad_group.name"),
                    "headlines": headlines,
                    "descriptions": descriptions,
                    "final_urls": r.get("ad_group_ad.ad.final_urls") or [],
                })

    single_ad_groups = [
        {"ad_group": v["name"], "enabled_ads": v["enabled_ads"]}
        for v in ads_per_group.values()
        if v["enabled_ads"] == 1
    ]

    def _slim(row: dict) -> dict:
        slim = {
            k: v
            for k, v in row.items()
            if k not in (
                "ad_group_ad.ad.responsive_search_ad.headlines",
                "ad_group_ad.ad.responsive_search_ad.descriptions",
            )
        }
        slim["headline_count"] = _asset_count(
            row, "ad_group_ad.ad.responsive_search_ad.headlines"
        )
        slim["description_count"] = _asset_count(
            row, "ad_group_ad.ad.responsive_search_ad.descriptions"
        )
        return slim

    insights: list[str] = []
    if incomplete_rsas:
        insights.append(
            f"{len(incomplete_rsas)} RSA(s) are below best practice (8+ headlines, "
            f"3+ descriptions) — thin RSAs depress ad strength and CTR."
        )
    if single_ad_groups:
        insights.append(
            f"{len(single_ad_groups)} ad group(s) have only one enabled ad — "
            f"Google cannot rotate or optimize with a single creative."
        )
    insights.extend(policy_insights)

    # Landing-page work needs every ad's URL, not just the top ten rows'.
    landing_pages: dict[str, int] = {}
    for r in rows:
        for url in r.get("ad_group_ad.ad.final_urls") or []:
            landing_pages[url] = landing_pages.get(url, 0) + 1

    top = [_slim(r) for r in rows[:10]]
    return {
        "compact": True,
        "total_ads": len(rows),
        "totals": _compact_totals(rows, currency_code),
        "by_status": _status_counts(rows, "ad_group_ad.status"),
        "by_approval_status": _status_counts(
            rows, "ad_group_ad.policy_summary.approval_status"
        ),
        "policy_issues": policy_issues[:10],
        "ads_top_spend": top,
        "incomplete_rsas": incomplete_rsas[:10],
        "single_ad_ad_groups": single_ad_groups[:10],
        "landing_pages": [
            {"final_url": url, "ads": count}
            for url, count in sorted(landing_pages.items(), key=lambda item: -item[1])
        ],
        "insights": insights,
        "note": _compact_note(len(top), len(rows), "get_ad_performance")
        + " Compact rows replace full headline/description lists with counts;"
        " landing_pages lists every final URL across all ads.",
    }


_POLICY_TOPICS_RAW = "ad_group_ad.policy_summary.policy_topic_entries"
_POLICY_TOPICS = "ad_group_ad.policy_summary.policy_topics"
_FLAGGED_APPROVAL = ("DISAPPROVED", "APPROVED_LIMITED")


def _compact_policy_topics(row: dict) -> None:
    """Replace the raw policy findings with their topic names.

    Each PolicyTopicEntry carries evidences and constraints (country lists,
    matched text) that can run to kilobytes per ad. The topic name is what
    a reader needs to know why an ad is restricted.
    """
    entries = row.pop(_POLICY_TOPICS_RAW, None) or []
    topics: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        topic = entry.get("topic") if isinstance(entry, dict) else None
        if topic and topic not in topics:
            topics.append(str(topic))
    row[_POLICY_TOPICS] = topics


def _policy_issues(rows: list[dict]) -> list[dict]:
    """Disapproved and limited ads, highest spend first (rows arrive cost-sorted)."""
    return [
        {
            "ad_id": r.get("ad_group_ad.ad.id"),
            "campaign": r.get("campaign.name"),
            "ad_group": r.get("ad_group.name"),
            "status": r.get("ad_group_ad.status"),
            "approval_status": str(r.get("ad_group_ad.policy_summary.approval_status")),
            "review_status": r.get("ad_group_ad.policy_summary.review_status"),
            "policy_topics": r.get(_POLICY_TOPICS) or [],
            "cost": r.get("metrics.cost"),
        }
        for r in rows
        if str(r.get("ad_group_ad.policy_summary.approval_status")) in _FLAGGED_APPROVAL
    ]


def get_keyword_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
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

    if not compact:
        return {"keywords": rows, "total_keywords": len(rows)}

    qs_field = "ad_group_criterion.quality_info.quality_score"

    def _rated_score(r: dict) -> int | None:
        """The Quality Score, or None when Google has not assigned one.

        Google never issues a real score of 0: both null and 0 mean "not
        enough impressions to rate". Counting those as "< 5" reports an
        ordinary unrated long tail as an account-wide relevance problem and
        sends the assistant off fixing ads and landing pages that are fine.
        """
        score = r.get(qs_field)

        return score if isinstance(score, int) and score > 0 else None

    low_quality = [
        r for r in rows
        if (score := _rated_score(r)) is not None and score < 5
    ]
    unrated_count = sum(1 for r in rows if _rated_score(r) is None)
    zero_conv_spenders = [
        r for r in rows
        if (r.get("metrics.cost_micros") or 0) > 0
        and not (r.get("metrics.conversions") or 0)
    ]

    def _kw(r: dict) -> dict:
        return {
            "keyword": r.get("ad_group_criterion.keyword.text"),
            "match_type": r.get("ad_group_criterion.keyword.match_type"),
            "quality_score": r.get(qs_field),
            "cost": r.get("metrics.cost"),
            "clicks": r.get("metrics.clicks"),
            "conversions": r.get("metrics.conversions"),
        }

    insights: list[str] = []
    if low_quality:
        insights.append(
            f"{len(low_quality)} keyword(s) have quality score < 5 — fix ad "
            f"relevance and landing pages before adding keywords or budget."
        )
    if unrated_count:
        insights.append(
            f"{unrated_count} keyword(s) have no quality score yet (too few "
            f"impressions to rate). They are excluded from the count above."
        )
    if zero_conv_spenders:
        wasted = round(
            sum((r.get("metrics.cost_micros") or 0) for r in zero_conv_spenders)
            / 1_000_000,
            2,
        )
        insights.append(
            f"{len(zero_conv_spenders)} keyword(s) spent {wasted} {currency_code} "
            f"without converting."
        )

    top = rows[:10]
    return {
        "compact": True,
        "total_keywords": len(rows),
        "totals": _compact_totals(rows, currency_code),
        "by_match_type": _status_counts(
            rows, "ad_group_criterion.keyword.match_type"
        ),
        "keywords_top_spend": top,
        "low_quality_score": [_kw(r) for r in low_quality[:10]],
        "unrated_quality_score": unrated_count,
        "zero_conversion_spenders": [_kw(r) for r in zero_conv_spenders[:10]],
        "insights": insights,
        "note": _compact_note(len(top), len(rows), "get_keyword_performance"),
    }


def get_search_terms(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
) -> dict:
    """Get search terms report — what users actually typed before clicking ads."""
    from adloop.ads.gaql import execute_query

    # search_term_view requires an explicit date segment, so the default is
    # DURING LAST_30_DAYS and a given range replaces it. ``_date_clause``
    # prefixes its fragment with AND (the other read tools append it to a
    # WHERE); here it is the only predicate, so the AND comes off.
    where = _date_clause(date_range_start, date_range_end).removeprefix("AND ")

    query = f"""
        SELECT search_term_view.search_term,
               campaign.name, ad_group.name,
               metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE {where}
        ORDER BY metrics.clicks DESC
        LIMIT 200
    """

    rows = execute_query(config, customer_id, query)
    currency_code = get_currency_code(config, customer_id)
    _enrich_cost_fields(rows, currency_code)

    if not compact:
        return {"search_terms": rows, "total_search_terms": len(rows)}

    def _term(r: dict) -> dict:
        return {
            "search_term": r.get("search_term_view.search_term"),
            "campaign": r.get("campaign.name"),
            "clicks": r.get("metrics.clicks"),
            "cost": r.get("metrics.cost"),
            "conversions": r.get("metrics.conversions"),
        }

    waste = sorted(
        (
            r for r in rows
            if (r.get("metrics.clicks") or 0) >= 5
            and not (r.get("metrics.conversions") or 0)
        ),
        key=lambda r: r.get("metrics.cost_micros") or 0,
        reverse=True,
    )
    converters = sorted(
        (r for r in rows if (r.get("metrics.conversions") or 0) > 0),
        key=lambda r: r.get("metrics.conversions") or 0,
        reverse=True,
    )

    insights: list[str] = []
    if waste:
        wasted_cost = round(
            sum((r.get("metrics.cost_micros") or 0) for r in waste) / 1_000_000, 2
        )
        insights.append(
            f"{len(waste)} search term(s) with 5+ clicks and zero conversions "
            f"cost {wasted_cost} {currency_code} — negative-keyword candidates."
        )
    if converters:
        insights.append(
            f"{len(converters)} search term(s) converted — check whether the top "
            f"converters exist as exact-match keywords yet."
        )

    top = rows[:10]
    return {
        "compact": True,
        "total_search_terms": len(rows),
        "totals": _compact_totals(rows, currency_code, row_limit=200),
        "search_terms_top_clicks": top,
        "waste_candidates": [_term(r) for r in waste[:10]],
        "top_converters": [_term(r) for r in converters[:5]],
        "insights": insights,
        "note": _compact_note(len(top), len(rows), "get_search_terms"),
    }


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
          AND campaign_criterion.type = 'KEYWORD'
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
            "in this report. Also note: custom segments (custom_audience) can never "
            "be attached to Search campaigns — an empty result for a Search campaign "
            "is expected, not missing data."
        )

    search_campaigns = sorted({
        str(r.get("campaign.name") or r.get("campaign.id") or "")
        for r in rows
        if str(r.get("campaign.advertising_channel_type") or "") == "SEARCH"
    })
    if search_campaigns:
        shown = ", ".join(search_campaigns[:5])
        insights.append(
            f"{len(search_campaigns)} of these campaigns are SEARCH campaigns "
            f"({shown}{', …' if len(search_campaigns) > 5 else ''}). Custom "
            "segments (custom_audience) CANNOT be attached to Search campaigns — "
            "they only work in Display, Video, Demand Gen, and as PMax signals. "
            "Do not propose custom-segment targeting for these campaigns; use "
            "remarketing lists, in-market, or affinity segments instead (see the "
            "audience compatibility matrix in the orchestration rules)."
        )

    return {"audiences": rows, "total_audiences": len(rows), "insights": insights}


def get_demographic_targeting(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List current demographic targeting (AGE_RANGE, GENDER, PARENTAL_STATUS, INCOME_RANGE).

    Provide either `ad_group_id` or `campaign_id`. Returns criteria with their
    `criterion_id` (needed to remove a criterion), the demographic value, and
    whether the criterion is negative (excluded) or positive (narrowed targeting).

    By default, Google Ads serves ads to all demographic segments — a criterion
    only appears here when the user has explicitly added an exclusion or
    positive targeting refinement.
    """
    from adloop.ads.gaql import execute_query

    if not ad_group_id and not campaign_id:
        return {
            "error": "Provide either ad_group_id or campaign_id",
        }
    if ad_group_id and campaign_id:
        return {
            "error": "Provide only one of ad_group_id or campaign_id, not both",
        }
    if (ad_group_id or campaign_id) and not (ad_group_id or campaign_id).isdigit():
        return {
            "error": "ad_group_id / campaign_id must be a numeric Google Ads ID",
        }

    demographic_types = (
        "'AGE_RANGE', 'GENDER', 'PARENTAL_STATUS', 'INCOME_RANGE'"
    )

    if ad_group_id:
        query = f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.type,
                   ad_group_criterion.negative,
                   ad_group_criterion.status,
                   ad_group_criterion.age_range.type,
                   ad_group_criterion.gender.type,
                   ad_group_criterion.parental_status.type,
                   ad_group_criterion.income_range.type,
                   ad_group.id, ad_group.name,
                   campaign.id, campaign.name
            FROM ad_group_criterion
            WHERE ad_group.id = {ad_group_id}
              AND ad_group_criterion.type IN ({demographic_types})
            ORDER BY ad_group_criterion.type
        """
        level = "ad_group"
    else:
        query = f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.type,
                   campaign_criterion.negative,
                   campaign_criterion.status,
                   campaign_criterion.age_range.type,
                   campaign_criterion.gender.type,
                   campaign_criterion.parental_status.type,
                   campaign_criterion.income_range.type,
                   campaign.id, campaign.name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type IN ({demographic_types})
            ORDER BY campaign_criterion.type
        """
        level = "campaign"

    rows = execute_query(config, customer_id, query)

    # Surface the composite resource ID for each criterion so the AI can pass
    # it straight to remove_entity (which expects 'parentId~criterionId').
    for row in rows:
        criterion_id = row.get(f"{level}_criterion.criterion_id")
        if criterion_id is None:
            continue
        if level == "ad_group":
            parent_id = row.get("ad_group.id")
        else:
            parent_id = row.get("campaign.id")
        if parent_id is not None:
            row["remove_id"] = f"{parent_id}~{criterion_id}"

    insights: list[str] = []
    if not rows:
        insights.append(
            f"No demographic criteria found on this {level}. By default, Google "
            f"Ads serves ads to all age/gender/parental/income segments — "
            f"criteria only appear here once you actively exclude or narrow them."
        )
    else:
        negatives = sum(
            1 for r in rows
            if r.get(f"{level}_criterion.negative") is True
        )
        if negatives:
            insights.append(
                f"{negatives} demographic exclusion(s) active on this {level}. "
                f"Excluded segments will not see ads."
            )

    return {
        "level": level,
        "criteria": rows,
        "total_criteria": len(rows),
        "insights": insights,
    }


CHANGE_RESOURCE_TYPES = (
    "AD", "AD_GROUP", "AD_GROUP_AD", "AD_GROUP_ASSET", "AD_GROUP_BID_MODIFIER",
    "AD_GROUP_CRITERION", "AD_GROUP_FEED", "ASSET", "ASSET_SET",
    "ASSET_SET_ASSET", "CAMPAIGN", "CAMPAIGN_ASSET", "CAMPAIGN_ASSET_SET",
    "CAMPAIGN_BUDGET", "CAMPAIGN_CRITERION", "CAMPAIGN_FEED", "CUSTOMER_ASSET",
    "FEED", "FEED_ITEM",
)

# ChangeClientType values, as the Google Ads UI's change history names them.
_CHANGE_CLIENT_LABELS = {
    "GOOGLE_ADS_WEB_CLIENT": "Google Ads UI",
    "GOOGLE_ADS_AUTOMATED_RULE": "Automated rule",
    "GOOGLE_ADS_SCRIPTS": "Google Ads scripts",
    "GOOGLE_ADS_BULK_UPLOAD": "Bulk upload",
    "GOOGLE_ADS_API": "Google Ads API",
    "GOOGLE_ADS_EDITOR": "Google Ads Editor",
    "GOOGLE_ADS_MOBILE_APP": "Google Ads mobile app",
    "GOOGLE_ADS_RECOMMENDATIONS": "Applied recommendation",
    "GOOGLE_ADS_RECOMMENDATIONS_SUBSCRIPTION": "Auto-applied recommendation",
    "SEARCH_ADS_360_SYNC": "Search Ads 360 sync",
    "SEARCH_ADS_360_POST": "Search Ads 360 post",
    "INTERNAL_TOOL": "Google internal tool",
    "OTHER": "Other",
}

# change_event keeps 30 days. The window is counted back from today in the
# server's clock while Google counts in the account's time zone, so one day
# of margin keeps a "last 30 days" request from tipping over the edge.
_CHANGE_HISTORY_DAYS = 29
_CHANGE_HISTORY_MAX_LIMIT = 10_000
# Substrings of changed field paths that mean a bidding change: strategy
# type and targets on campaigns, bids on ad groups and keywords.
_BIDDING_FIELD_MARKERS = (
    "bidding", "target_cpa", "target_roas", "maximize_", "manual_cpc",
    "cpc_bid", "bid_modifier",
)


def get_change_history(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
    resource_types: list[str] | None = None,
    limit: int = 1000,
) -> dict:
    """List account changes (who changed what, where, and how) from change_event."""
    from datetime import date, timedelta

    from adloop.ads.gaql import execute_query

    today = date.today()
    earliest = today - timedelta(days=_CHANGE_HISTORY_DAYS)

    def _parse(value: str, name: str) -> date | dict:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return {"error": f"{name} must be a date as YYYY-MM-DD, got {value!r}"}

    start = earliest
    if date_range_start:
        parsed = _parse(date_range_start, "date_range_start")
        if isinstance(parsed, dict):
            return parsed
        start = parsed
    end = today
    if date_range_end:
        parsed = _parse(date_range_end, "date_range_end")
        if isinstance(parsed, dict):
            return parsed
        end = parsed

    notes: list[str] = []
    if start < earliest:
        notes.append(
            f"Google Ads keeps change history for 30 days; the start date "
            f"{start.isoformat()} was moved to {earliest.isoformat()}."
        )
        start = earliest
    if end > today:
        end = today
    if end < start:
        return {
            "error": (
                f"The date range {start.isoformat()} to {end.isoformat()} is "
                f"empty or lies entirely outside the 30 days of change history "
                f"Google Ads keeps (from {earliest.isoformat()})."
            )
        }

    campaign_filter = ""
    if campaign_id:
        if not str(campaign_id).isdigit():
            return {"error": "campaign_id must be a numeric Google Ads ID"}
        campaign_filter = f"AND campaign.id = {campaign_id}"

    type_filter = ""
    if resource_types:
        wanted = [str(t).strip().upper() for t in resource_types if str(t).strip()]
        unknown = [t for t in wanted if t not in CHANGE_RESOURCE_TYPES]
        if unknown:
            return {
                "error": (
                    f"Unknown resource type(s): {', '.join(unknown)}. Valid "
                    f"types: {', '.join(CHANGE_RESOURCE_TYPES)}"
                )
            }
        if wanted:
            type_filter = (
                "AND change_event.change_resource_type IN ("
                + ", ".join(f"'{t}'" for t in wanted)
                + ")"
            )

    limit = max(1, min(int(limit or 1000), _CHANGE_HISTORY_MAX_LIMIT))

    # change_date_time is a timestamp; a bare date compares as its midnight,
    # so the day after the end date is the bound that keeps the whole end day.
    query = f"""
        SELECT change_event.change_date_time,
               change_event.user_email,
               change_event.client_type,
               change_event.change_resource_type,
               change_event.resource_change_operation,
               change_event.changed_fields,
               change_event.change_resource_name,
               campaign.id, campaign.name,
               ad_group.id, ad_group.name
        FROM change_event
        WHERE change_event.change_date_time >= '{start.isoformat()}'
          AND change_event.change_date_time <= '{(end + timedelta(days=1)).isoformat()}'
          {campaign_filter}
          {type_filter}
        ORDER BY change_event.change_date_time DESC
        LIMIT {limit}
    """

    rows = execute_query(config, customer_id, query)

    changes: list[dict] = []
    for r in rows:
        client_type = str(r.get("change_event.client_type") or "UNKNOWN")
        fields = r.get("change_event.changed_fields") or []
        if isinstance(fields, str):
            fields = [f for f in fields.split(",") if f]
        changes.append({
            "change_time": r.get("change_event.change_date_time"),
            "user_email": r.get("change_event.user_email") or None,
            "client_type": client_type,
            "client": _CHANGE_CLIENT_LABELS.get(client_type, client_type),
            "resource_type": r.get("change_event.change_resource_type"),
            "operation": r.get("change_event.resource_change_operation"),
            "changed_fields": list(fields),
            "campaign": r.get("campaign.name") or None,
            "campaign_id": r.get("campaign.id") or None,
            "ad_group": r.get("ad_group.name") or None,
            "ad_group_id": r.get("ad_group.id") or None,
            "resource_name": r.get("change_event.change_resource_name"),
        })

    by_day: dict[str, int] = {}
    for c in changes:
        day = str(c["change_time"] or "")[:10] or "UNKNOWN"
        by_day[day] = by_day.get(day, 0) + 1

    insights: list[str] = []
    if not changes:
        insights.append(
            f"No changes recorded between {start.isoformat()} and "
            f"{end.isoformat()} for this filter."
        )
    auto_applied = [
        c for c in changes
        if c["client_type"] == "GOOGLE_ADS_RECOMMENDATIONS_SUBSCRIPTION"
    ]
    if auto_applied:
        insights.append(
            f"{len(auto_applied)} change(s) came from auto-applied "
            f"recommendations, made by Google without a person approving each one."
        )
    budget_or_bidding = [
        c for c in changes
        if c["resource_type"] == "CAMPAIGN_BUDGET"
        or any(
            marker in f
            for f in c["changed_fields"]
            for marker in _BIDDING_FIELD_MARKERS
        )
    ]
    if budget_or_bidding:
        insights.append(
            f"{len(budget_or_bidding)} change(s) touched budgets or bidding; "
            f"those move spend and conversions within days."
        )
    if campaign_id:
        notes.append(
            "The campaign filter keeps only changes Google attributes to that "
            "campaign; changes without a campaign (account-level assets, for "
            "example) are left out."
        )

    result: dict = {
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "changes": changes,
        "total_changes": len(changes),
        "by_resource_type": _status_counts(changes, "resource_type"),
        "by_client": _status_counts(changes, "client"),
        "by_user": _status_counts(changes, "user_email"),
        "by_day": dict(sorted(by_day.items(), reverse=True)),
        "insights": insights,
    }
    if len(changes) >= limit:
        result["truncated"] = True
        notes.append(
            f"Reached the limit of {limit} changes (newest first); older "
            f"changes in the range are not included. A narrower date range, "
            f"a campaign or resource type filter, or a higher limit (up to "
            f"{_CHANGE_HISTORY_MAX_LIMIT}) returns the rest."
        )
    if notes:
        result["notes"] = notes
    return result


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


def _compact_totals(
    rows: list[dict], currency_code: str, *, row_limit: int | None = None
) -> dict:
    """Deterministic aggregates over the rows given.

    Not account-level whenever the query behind them carries a LIMIT: pass
    ``row_limit`` for those reports so a truncated total is marked partial
    instead of being read as the account's real cost and conversions.
    """
    cost = sum((r.get("metrics.cost_micros") or 0) for r in rows) / 1_000_000
    clicks = sum((r.get("metrics.clicks") or 0) for r in rows)
    impressions = sum((r.get("metrics.impressions") or 0) for r in rows)
    conversions = sum((r.get("metrics.conversions") or 0) for r in rows)
    totals: dict = {
        "cost": round(cost, 2),
        "clicks": clicks,
        "impressions": impressions,
        "conversions": round(conversions, 1),
        "currency": currency_code,
    }
    if conversions > 0:
        totals["cpa"] = round(cost / conversions, 2)
    if impressions > 0:
        totals["ctr_pct"] = round(clicks / impressions * 100, 2)

    # Hitting the ceiling exactly is indistinguishable from stopping there, so
    # this errs toward declaring partial. Silently under-reporting cost is the
    # worse failure: these numbers are labelled "totals" and get compared
    # against campaign-level truth.
    if row_limit is not None and len(rows) >= row_limit:
        totals["partial"] = True
        totals["rows_counted"] = len(rows)
        totals["partial_reason"] = (
            f"The underlying query returns at most {row_limit} rows, ranked by "
            f"the report's sort metric, and this account reached that ceiling. "
            f"These totals cover those rows only, not the whole account. Use "
            f"get_campaign_performance for account-level figures."
        )

    return totals


def _compact_note(shown: int, total: int, tool_name: str) -> str:
    return (
        f"Compact mode: aggregates plus the top {shown} of {total} rows. "
        f"Call {tool_name} with compact=false when you need every row "
        f"(e.g. before proposing changes to a specific low-spend entity)."
    )


def _status_counts(rows: list[dict], field: str) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        status = str(r.get(field) or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return counts


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
