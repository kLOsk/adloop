"""Reddit Ads read tools — accounts, structure, performance, pixels, targeting.

Every function takes ``config`` first and an explicit ``ad_account_id``
that falls back to ``config.reddit.ad_account_id``. Output mirrors the
Google Ads read tools: money is pre-computed in currency units (never
microcurrency), ``insights[]`` carries computed warnings, and ``compact``
mode returns totals plus offender lists instead of every row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from adloop.reddit.client import (
    data_of,
    from_micro,
    reddit_get,
    reddit_get_all,
    reddit_post,
    reddit_request,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Reddit's reporting group allows 60 requests/min per user; the report
# itself allows at most three breakdowns (four with COUNTRY + REGION).
_MAX_BREAKDOWNS = 3

# Base metric set for get_reddit_performance. KEY_CONVERSION_* is whatever
# the account has configured as its key conversion event, which makes it
# the platform-neutral "conversions" column; purchase value feeds ROAS.
_BASE_FIELDS = [
    "SPEND",
    "IMPRESSIONS",
    "CLICKS",
    "CTR",
    "CPC",
    "ECPM",
    "KEY_CONVERSION_TOTAL_COUNT",
    "KEY_CONVERSION_CLICKS",
    "KEY_CONVERSION_VIEWS",
    "KEY_CONVERSION_ECPA",
    "CONVERSION_PURCHASE_TOTAL_VALUE",
]

_LEVEL_BREAKDOWNS = {
    "account": [],
    "campaign": ["CAMPAIGN_ID"],
    "ad_group": ["AD_GROUP_ID", "CAMPAIGN_ID"],
    # AD_ID + AD_GROUP_ID leaves one slot for an optional extra breakdown;
    # the campaign is joined from the ad list instead.
    "ad": ["AD_ID", "AD_GROUP_ID"],
}

_EXTRA_BREAKDOWNS = {
    "date": "DATE",
    "hour": "HOUR",
    "country": "COUNTRY",
    "region": "REGION",
    "community": "COMMUNITY",
    "keyword": "KEYWORD",
    "interest": "INTEREST",
    "placement": "PLACEMENT",
    "gender": "GENDER",
    "os_type": "OS_TYPE",
}

# Report fields Reddit returns in microcurrency (divide by 1e6) and those
# it returns in cents (divide by 100).
_MICRO_FIELDS = {"spend", "cpc", "cpv", "ecpm", "key_conversion_ecpa"}
_MICRO_SUFFIXES = ("_ecpa", "_ecpm")
_CENT_SUFFIXES = ("_total_value", "_avg_value")

_TARGETING_KINDS = ("communities", "interests", "geolocations", "languages", "keywords")

_STANDARD_PIXEL_EVENTS = (
    "page_visit",
    "view_content",
    "search",
    "add_to_cart",
    "add_to_wishlist",
    "purchase",
    "lead",
    "sign_up",
    "custom",
)

_account_meta_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_account(config: AdLoopConfig, ad_account_id: str = "") -> str:
    """Explicit id wins, then ``reddit.ad_account_id`` from the config."""
    account = (ad_account_id or config.reddit.ad_account_id or "").strip()
    if not account:
        raise ValueError(
            "ad_account_id is required — pass it explicitly (see "
            "list_reddit_accounts) or set reddit.ad_account_id in the config."
        )
    return account


def account_meta(config: AdLoopConfig, ad_account_id: str) -> dict:
    """Currency + time zone of an ad account, cached per process."""
    cached = _account_meta_cache.get(ad_account_id)
    if cached is not None:
        return cached
    data = data_of(reddit_get(config, f"ad_accounts/{ad_account_id}"))
    meta = {
        "currency": str(data.get("currency") or ""),
        "time_zone_id": str(data.get("time_zone_id") or "UTC"),
        "name": str(data.get("name") or ""),
        "business_id": str(data.get("business_id") or ""),
        "admin_approval": str(data.get("admin_approval") or ""),
    }
    _account_meta_cache[ad_account_id] = meta
    return meta


def reset_account_meta_cache() -> None:
    _account_meta_cache.clear()


def _parse_date(value: str, *, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"Invalid date '{value}' — use YYYY-MM-DD (e.g. 2026-09-01)."
        ) from exc


def report_window(date_range_start: str = "", date_range_end: str = "") -> tuple[str, str, str, str]:
    """Default last 30 days; returns (starts_at, ends_at, start_date, end_date).

    Reddit wants hour-aligned ISO timestamps. ``ends_at`` is the midnight
    after the requested end day so the end day is included in full.
    """
    today = datetime.now(timezone.utc).date()
    end = _parse_date(date_range_end, default=today)
    start = _parse_date(date_range_start, default=end - timedelta(days=29))
    if start > end:
        raise ValueError("date_range_start must not be after date_range_end.")
    starts_at = f"{start.isoformat()}T00:00:00Z"
    ends_at = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    return starts_at, ends_at, start.isoformat(), end.isoformat()


def _normalise_money(row: dict) -> None:
    """Convert Reddit's micro/cent report values into currency amounts in place."""
    for key in list(row.keys()):
        value = row.get(key)
        if value is None or isinstance(value, (str, bool)):
            continue
        lowered = key.lower()
        if lowered in _MICRO_FIELDS or lowered.endswith(_MICRO_SUFFIXES):
            row[key] = from_micro(value)
        elif lowered.endswith(_CENT_SUFFIXES):
            try:
                row[key] = round(float(value) / 100, 2)
            except (TypeError, ValueError):
                pass


def _enrich_performance(row: dict, currency: str) -> None:
    """Add the pre-computed columns the orchestration rules promise."""
    spend = row.get("spend") or 0.0
    clicks = row.get("clicks") or 0
    impressions = row.get("impressions") or 0
    conversions = row.get("key_conversion_total_count")
    if conversions is None:
        conversions = (row.get("key_conversion_clicks") or 0) + (
            row.get("key_conversion_views") or 0
        )
    row["conversions"] = conversions
    row["currency"] = currency
    if row.get("ctr") is None and impressions:
        row["ctr"] = round(clicks / impressions, 4)
    if row.get("cpc") is None and clicks:
        row["cpc"] = round(spend / clicks, 2)
    row["cpa"] = round(spend / conversions, 2) if conversions else None
    purchase_value = row.get("conversion_purchase_total_value")
    row["roas"] = round(purchase_value / spend, 2) if purchase_value and spend else None


def _campaign_summary(c: dict) -> dict:
    return {
        "campaign_id": c.get("id"),
        "name": c.get("name"),
        "configured_status": c.get("configured_status"),
        "effective_status": c.get("effective_status"),
        "objective": c.get("objective"),
        "type": c.get("type"),
        "is_campaign_budget_optimization": bool(c.get("is_campaign_budget_optimization")),
        "goal_type": c.get("goal_type"),
        "goal_value": from_micro(c.get("goal_value")),
        "spend_cap": from_micro(c.get("spend_cap")),
        "bid_strategy": c.get("bid_strategy"),
        "bid_type": c.get("bid_type"),
        "bid_value": from_micro(c.get("bid_value")),
        "optimization_goal": c.get("optimization_goal"),
        "conversion_pixel_id": c.get("conversion_pixel_id"),
        "funding_instrument_id": c.get("funding_instrument_id"),
        "start_time": c.get("start_time"),
        "end_time": c.get("end_time"),
        "created_at": c.get("created_at"),
        "modified_at": c.get("modified_at"),
    }


def _targeting_summary(t: dict | None) -> dict:
    t = t or {}

    def _names(items: Any) -> list:
        out = []
        for item in items or []:
            if isinstance(item, dict):
                out.append(item.get("name") or item.get("id") or item)
            else:
                out.append(item)
        return out

    return {
        "communities": _names(t.get("communities")),
        "excluded_communities": _names(t.get("excluded_communities")),
        "interests": _names(t.get("interests")),
        "keywords": _names(t.get("keywords")),
        "excluded_keywords": _names(t.get("excluded_keywords")),
        "geolocations": _names(t.get("geolocations")),
        "excluded_geolocations": _names(t.get("excluded_geolocations")),
        "languages": list(t.get("languages") or []),
        "gender": t.get("gender"),
        "platforms": list(t.get("platforms") or []),
        "locations": list(t.get("locations") or []),
        "expand_targeting": t.get("expand_targeting"),
        "custom_audience_ids": list(t.get("custom_audience_ids") or []),
    }


def _ad_group_summary(g: dict) -> dict:
    return {
        "ad_group_id": g.get("id"),
        "campaign_id": g.get("campaign_id"),
        "name": g.get("name"),
        "configured_status": g.get("configured_status"),
        "effective_status": g.get("effective_status"),
        "is_campaign_budget_optimization": bool(g.get("is_campaign_budget_optimization")),
        "goal_type": g.get("goal_type"),
        "goal_value": from_micro(g.get("goal_value")),
        "bid_strategy": g.get("bid_strategy"),
        "bid_type": g.get("bid_type"),
        "bid_value": from_micro(g.get("bid_value")),
        "optimization_goal": g.get("optimization_goal"),
        "conversion_pixel_id": g.get("conversion_pixel_id"),
        "start_time": g.get("start_time"),
        "end_time": g.get("end_time"),
        "targeting": _targeting_summary(g.get("targeting")),
        "delivery_status": g.get("delivery_status"),
    }


def _ad_summary(a: dict) -> dict:
    return {
        "ad_id": a.get("id"),
        "ad_group_id": a.get("ad_group_id"),
        "campaign_id": a.get("campaign_id"),
        "name": a.get("name"),
        "type": a.get("type"),
        "configured_status": a.get("configured_status"),
        "effective_status": a.get("effective_status"),
        "rejection_reason": a.get("rejection_reason"),
        "post_id": a.get("post_id"),
        "post_url": a.get("post_url"),
        "click_url": a.get("click_url"),
        "profile_id": a.get("profile_id"),
        "preview_url": a.get("preview_url"),
        "delivery_status": a.get("delivery_status"),
        "created_at": a.get("created_at"),
        "modified_at": a.get("modified_at"),
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def list_reddit_accounts(config: AdLoopConfig) -> dict:
    """Businesses and ad accounts the authorized Reddit user can reach."""
    me = data_of(reddit_get(config, "me"))
    businesses = reddit_get_all(config, "me/businesses")

    accounts: list[dict] = []
    errors: list[str] = []
    for business in businesses:
        business_id = str(business.get("id") or "")
        if not business_id:
            continue
        try:
            rows = reddit_get_all(config, f"businesses/{business_id}/ad_accounts")
        except Exception as exc:  # one broken business must not hide the rest
            errors.append(f"business {business_id}: {exc}")
            continue
        for row in rows:
            accounts.append(
                {
                    "ad_account_id": row.get("id"),
                    "name": row.get("name"),
                    "business_id": business_id,
                    "business_name": business.get("name"),
                    "currency": row.get("currency"),
                    "time_zone_id": row.get("time_zone_id"),
                    "type": row.get("type"),
                    "admin_approval": row.get("admin_approval"),
                    "suspension_reason": row.get("suspension_reason"),
                }
            )

    result = {
        "reddit_username": me.get("reddit_username"),
        "member_id": me.get("id"),
        "businesses": [
            {"business_id": b.get("id"), "name": b.get("name"), "country": b.get("country")}
            for b in businesses
        ],
        "accounts": accounts,
        "total": len(accounts),
    }
    if errors:
        result["errors"] = errors
    if not accounts:
        result["note"] = (
            "No ad accounts found. The authorizing Reddit user must be a member "
            "of a Reddit Business with at least one ad account (Reddit Ads "
            "Manager → Business Manager)."
        )
    return result


def list_reddit_funding_instruments(config: AdLoopConfig, *, ad_account_id: str = "") -> dict:
    """Funding instruments (billing) and posting profiles of an ad account.

    Both are prerequisites for creation: campaigns need a servable funding
    instrument, ads need the profile that will author the post.
    """
    account = resolve_account(config, ad_account_id)
    instruments = reddit_get_all(
        config, f"ad_accounts/{account}/funding_instruments", {"mode": "ALL"}
    )
    profiles = reddit_get_all(config, f"ad_accounts/{account}/profiles")
    return {
        "ad_account_id": account,
        "funding_instruments": [
            {
                "funding_instrument_id": fi.get("id"),
                "name": fi.get("name"),
                "currency": fi.get("currency"),
                "is_servable": fi.get("is_servable"),
                "reasons_not_servable": fi.get("reasons_not_servable") or [],
                "credit_limit": from_micro(fi.get("credit_limit")),
                "billable_amount": from_micro(fi.get("billable_amount")),
                "start_time": fi.get("start_time"),
                "end_time": fi.get("end_time"),
            }
            for fi in instruments
        ],
        "profiles": [
            {
                "profile_id": p.get("id"),
                "username": p.get("name"),
                "reddit_user_id": p.get("reddit_user_id"),
                "business_id": p.get("business_id"),
            }
            for p in profiles
        ],
    }


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def get_reddit_campaigns(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    include_archived: bool = False,
) -> dict:
    account = resolve_account(config, ad_account_id)
    rows = reddit_get_all(config, f"ad_accounts/{account}/campaigns")
    campaigns = [_campaign_summary(c) for c in rows]
    if not include_archived:
        campaigns = [
            c for c in campaigns
            if c["configured_status"] not in ("ARCHIVED", "DELETED")
        ]
    meta = account_meta(config, account)
    insights: list[str] = []
    attention = [
        c for c in campaigns
        if c["effective_status"] in ("PENDING_BILLING_INFO", "PENDING_ID_VERIFICATION", "REJECTED")
    ]
    if attention:
        names = ", ".join(f"{c['name']} ({c['effective_status']})" for c in attention[:5])
        insights.append(
            f"{len(attention)} campaign(s) are blocked by account state, not by "
            f"settings: {names}. Fix billing / verification / policy first."
        )
    return {
        "ad_account_id": account,
        "currency": meta["currency"],
        "campaigns": campaigns,
        "total_campaigns": len(campaigns),
        "insights": insights,
    }


def get_reddit_ad_groups(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
) -> dict:
    account = resolve_account(config, ad_account_id)
    params = {"campaign_id": campaign_id} if campaign_id else {}
    rows = reddit_get_all(config, f"ad_accounts/{account}/ad_groups", params)
    ad_groups = [_ad_group_summary(g) for g in rows]
    meta = account_meta(config, account)
    insights: list[str] = []
    no_pixel = [g for g in ad_groups if not g["conversion_pixel_id"]]
    if no_pixel:
        insights.append(
            f"{len(no_pixel)} ad group(s) have no conversion_pixel_id. Reddit "
            "requires one on every ad group since 2026-07-13; delivery may stop."
        )
    return {
        "ad_account_id": account,
        "currency": meta["currency"],
        "ad_groups": ad_groups,
        "total_ad_groups": len(ad_groups),
        "insights": insights,
    }


def get_reddit_ads(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_group_id: str = "",
    campaign_id: str = "",
) -> dict:
    account = resolve_account(config, ad_account_id)
    params: dict = {}
    if ad_group_id:
        params["ad_group_id"] = ad_group_id
    if campaign_id:
        params["campaign_id"] = campaign_id
    rows = reddit_get_all(config, f"ad_accounts/{account}/ads", params)
    ads = [_ad_summary(a) for a in rows]
    insights: list[str] = []
    rejected = [a for a in ads if a["effective_status"] == "REJECTED"]
    if rejected:
        names = ", ".join(
            f"{a['name']} ({a['rejection_reason'] or 'no reason given'})" for a in rejected[:5]
        )
        insights.append(
            f"{len(rejected)} ad(s) were REJECTED by Reddit policy review: {names}. "
            "They never serve until fixed and resubmitted."
        )
    pending = [a for a in ads if a["effective_status"] == "PENDING_APPROVAL"]
    if pending:
        insights.append(
            f"{len(pending)} ad(s) are still in policy review (PENDING_APPROVAL); "
            "Reddit usually decides within 24 hours."
        )
    return {
        "ad_account_id": account,
        "ads": ads,
        "total_ads": len(ads),
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def _run_report(
    config: AdLoopConfig,
    account: str,
    *,
    fields: list[str],
    breakdowns: list[str],
    starts_at: str,
    ends_at: str,
    time_zone_id: str = "",
    filter_expr: str = "",
) -> list[dict]:
    body: dict[str, Any] = {
        "data": {
            "fields": fields,
            "breakdowns": breakdowns,
            "starts_at": starts_at,
            "ends_at": ends_at,
        }
    }
    if time_zone_id:
        body["data"]["time_zone_id"] = time_zone_id
    if filter_expr:
        body["data"]["filter"] = filter_expr
    payload = reddit_post(config, f"ad_accounts/{account}/reports", body)
    rows = list(data_of(payload).get("metrics") or [])
    pages = 1
    while pages < 20:
        next_url = (payload.get("pagination") or {}).get("next_url")
        if not next_url:
            break
        payload = reddit_request(config, "POST", "", json_body=body, absolute_url=next_url)
        rows.extend(data_of(payload).get("metrics") or [])
        pages += 1
    for row in rows:
        _normalise_money(row)
    return rows


def _name_maps(config: AdLoopConfig, account: str, level: str) -> dict[str, dict[str, dict]]:
    """Entity names for the ids the report returns (it returns ids only)."""
    maps: dict[str, dict[str, dict]] = {"campaign": {}, "ad_group": {}, "ad": {}}
    if level in ("campaign", "ad_group", "ad"):
        for c in reddit_get_all(config, f"ad_accounts/{account}/campaigns"):
            maps["campaign"][str(c.get("id"))] = _campaign_summary(c)
    if level in ("ad_group", "ad"):
        for g in reddit_get_all(config, f"ad_accounts/{account}/ad_groups"):
            maps["ad_group"][str(g.get("id"))] = _ad_group_summary(g)
    if level == "ad":
        for a in reddit_get_all(config, f"ad_accounts/{account}/ads"):
            maps["ad"][str(a.get("id"))] = _ad_summary(a)
    return maps


def get_reddit_performance(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    level: str = "campaign",
    date_range_start: str = "",
    date_range_end: str = "",
    breakdown: str = "",
    compact: bool = False,
) -> dict:
    """Spend, clicks, conversions, CPA and ROAS per entity for a date range."""
    account = resolve_account(config, ad_account_id)
    level = (level or "campaign").strip().lower()
    if level not in _LEVEL_BREAKDOWNS:
        raise ValueError(
            f"level must be one of {sorted(_LEVEL_BREAKDOWNS)}, got '{level}'."
        )
    breakdown = (breakdown or "").strip().lower()
    if breakdown and breakdown not in _EXTRA_BREAKDOWNS:
        raise ValueError(
            f"breakdown must be one of {sorted(_EXTRA_BREAKDOWNS)}, got '{breakdown}'."
        )

    breakdowns = list(_LEVEL_BREAKDOWNS[level])
    if breakdown:
        breakdowns.append(_EXTRA_BREAKDOWNS[breakdown])
    starts_at, ends_at, start_date, end_date = report_window(date_range_start, date_range_end)
    meta = account_meta(config, account)
    currency = meta["currency"]

    rows = _run_report(
        config,
        account,
        fields=_BASE_FIELDS,
        breakdowns=breakdowns,
        starts_at=starts_at,
        ends_at=ends_at,
        time_zone_id=meta["time_zone_id"],
    )
    names = _name_maps(config, account, level)
    for row in rows:
        _enrich_performance(row, currency)
        cid = str(row.get("campaign_id") or "")
        gid = str(row.get("ad_group_id") or "")
        aid = str(row.get("ad_id") or "")
        if aid and aid in names["ad"]:
            ad = names["ad"][aid]
            row["ad_name"] = ad["name"]
            row["ad_status"] = ad["effective_status"]
            row["rejection_reason"] = ad["rejection_reason"]
            gid = gid or str(ad.get("ad_group_id") or "")
            cid = cid or str(ad.get("campaign_id") or "")
            row.setdefault("ad_group_id", gid)
            row.setdefault("campaign_id", cid)
        if gid and gid in names["ad_group"]:
            group = names["ad_group"][gid]
            row["ad_group_name"] = group["name"]
            row["ad_group_status"] = group["effective_status"]
            row["daily_budget"] = group["goal_value"] if group["goal_type"] == "DAILY_SPEND" else None
            cid = cid or str(group.get("campaign_id") or "")
            row.setdefault("campaign_id", cid)
        if cid and cid in names["campaign"]:
            campaign = names["campaign"][cid]
            row["campaign_name"] = campaign["name"]
            row["campaign_status"] = campaign["effective_status"]
            row["objective"] = campaign["objective"]

    rows.sort(key=lambda r: r.get("spend") or 0, reverse=True)

    insights: list[str] = []
    zero_conv = [r for r in rows if (r.get("spend") or 0) > 0 and not r.get("conversions")]
    if zero_conv and level != "account" and not breakdown:
        wasted = round(sum(r.get("spend") or 0 for r in zero_conv), 2)
        label = {"campaign": "campaign_name", "ad_group": "ad_group_name", "ad": "ad_name"}[level]
        names_txt = ", ".join(str(r.get(label) or r.get(f"{level}_id")) for r in zero_conv[:5])
        insights.append(
            f"{len(zero_conv)} {level.replace('_', ' ')}(s) spent {wasted} {currency} "
            f"with ZERO key conversions: {names_txt}. Check the pixel "
            f"(get_reddit_pixels) and the optimization goal before adding budget."
        )
    paused_with_spend = [
        r for r in rows
        if (r.get("spend") or 0) > 0
        and str(r.get(f"{level}_status" if level != "ad" else "ad_status") or "").endswith("PAUSED")
    ]
    if paused_with_spend and level != "account":
        insights.append(
            f"{len(paused_with_spend)} {level.replace('_', ' ')}(s) are paused now but "
            "spent in this window — expected if they were paused mid-period."
        )
    rejected = [r for r in rows if r.get("rejection_reason")]
    if rejected:
        insights.append(
            f"{len(rejected)} ad(s) in this report were rejected by policy review; "
            "their spend stopped at rejection."
        )
    if not rows:
        insights.append(
            "No rows for this window. Reddit report data stabilises within about "
            "6 hours; a window ending today may still be empty."
        )

    totals = {
        "spend": round(sum(r.get("spend") or 0 for r in rows), 2),
        "impressions": sum(r.get("impressions") or 0 for r in rows),
        "clicks": sum(r.get("clicks") or 0 for r in rows),
        "conversions": sum(r.get("conversions") or 0 for r in rows),
        "currency": currency,
    }
    if totals["clicks"]:
        totals["cpc"] = round(totals["spend"] / totals["clicks"], 2)
    if totals["impressions"]:
        totals["ctr"] = round(totals["clicks"] / totals["impressions"], 4)
    if totals["conversions"]:
        totals["cpa"] = round(totals["spend"] / totals["conversions"], 2)

    result: dict[str, Any] = {
        "ad_account_id": account,
        "level": level,
        "breakdown": breakdown or None,
        "date_range": {"start": start_date, "end": end_date, "time_zone_id": meta["time_zone_id"]},
        "currency": currency,
        "totals": totals,
        "insights": insights,
        "note": (
            "Money fields are in account currency (Reddit's microcurrency already "
            "divided). 'conversions' is the account's key conversion event. "
            "Data lags up to 6 hours."
        ),
    }
    if compact:
        top = rows[:10]
        result["compact"] = True
        result["total_rows"] = len(rows)
        result["rows_top_spend"] = top
        result["zero_conversion_spenders"] = [
            {
                "campaign_id": r.get("campaign_id"),
                "ad_group_id": r.get("ad_group_id"),
                "ad_id": r.get("ad_id"),
                "name": r.get("ad_name") or r.get("ad_group_name") or r.get("campaign_name"),
                "spend": r.get("spend"),
                "clicks": r.get("clicks"),
            }
            for r in zero_conv[:5]
        ]
        result["note"] += (
            f" Compact mode: showing {len(top)} of {len(rows)} rows; call "
            "get_reddit_performance without compact=true for every row."
        )
    else:
        result["rows"] = rows
        result["total_rows"] = len(rows)
    return result


def run_reddit_report(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    fields: list[str] | None = None,
    breakdowns: list[str] | None = None,
    date_range_start: str = "",
    date_range_end: str = "",
    filter: str = "",
    time_zone_id: str = "",
) -> dict:
    """Raw access to the reports endpoint for metrics the curated tool omits."""
    account = resolve_account(config, ad_account_id)
    fields = [str(f).strip().upper() for f in (fields or []) if str(f).strip()]
    breakdowns = [str(b).strip().upper() for b in (breakdowns or []) if str(b).strip()]
    if not fields:
        raise ValueError(
            "fields is required — e.g. [\"SPEND\", \"CLICKS\", \"KEY_CONVERSION_TOTAL_COUNT\"]. "
            "Use get_reddit_performance for the curated set."
        )
    if len(breakdowns) > _MAX_BREAKDOWNS + 1:
        raise ValueError("Reddit allows at most 3 breakdowns (4 with COUNTRY and REGION).")
    starts_at, ends_at, start_date, end_date = report_window(date_range_start, date_range_end)
    meta = account_meta(config, account)
    rows = _run_report(
        config,
        account,
        fields=fields,
        breakdowns=breakdowns,
        starts_at=starts_at,
        ends_at=ends_at,
        time_zone_id=time_zone_id or meta["time_zone_id"],
        filter_expr=filter,
    )
    return {
        "ad_account_id": account,
        "date_range": {"start": start_date, "end": end_date},
        "currency": meta["currency"],
        "fields": fields,
        "breakdowns": breakdowns,
        "rows": rows,
        "total_rows": len(rows),
        "note": "Micro/cent money fields were converted to currency amounts.",
    }


# ---------------------------------------------------------------------------
# Pixels (the tracking bridge)
# ---------------------------------------------------------------------------


def get_reddit_pixels(config: AdLoopConfig, *, ad_account_id: str = "") -> dict:
    """Pixels of the account plus when each standard event last fired."""
    account = resolve_account(config, ad_account_id)
    pixels = reddit_get_all(config, f"ad_accounts/{account}/pixels")
    result_pixels: list[dict] = []
    for pixel in pixels:
        pixel_id = str(pixel.get("id") or "")
        entry: dict[str, Any] = {
            "pixel_id": pixel_id,
            "name": pixel.get("name"),
            "business_id": pixel.get("business_id"),
            "last_fired_at": {},
            "custom_events": [],
        }
        try:
            fired = data_of(reddit_get(config, f"pixels/{pixel_id}/last_fired_at"))
        except Exception as exc:
            entry["last_fired_error"] = str(exc)
            fired = {}
        for event in _STANDARD_PIXEL_EVENTS:
            entry["last_fired_at"][event] = fired.get(event)
        entry["custom_events"] = list(fired.get("custom_events") or [])
        entry["never_fired"] = not any(entry["last_fired_at"].values()) and not entry["custom_events"]
        result_pixels.append(entry)

    insights: list[str] = []
    if not result_pixels:
        insights.append(
            "This ad account has no pixel. Reddit requires conversion_pixel_id on "
            "every ad group, so no new ad group can be created until one exists "
            "(Reddit Ads Manager → Events Manager)."
        )
    dead = [p for p in result_pixels if p["never_fired"] and "last_fired_error" not in p]
    if dead:
        insights.append(
            f"{len(dead)} pixel(s) have never fired any event: "
            + ", ".join(p["name"] or p["pixel_id"] for p in dead)
            + ". Conversion-optimized ad groups on them cannot learn."
        )

    # Cross-check: ad groups optimizing for an event the pixel never sent.
    try:
        ad_groups = reddit_get_all(config, f"ad_accounts/{account}/ad_groups")
    except Exception:
        ad_groups = []
    by_id = {p["pixel_id"]: p for p in result_pixels}
    for g in ad_groups:
        if g.get("configured_status") in ("ARCHIVED", "DELETED"):
            continue
        pixel = by_id.get(str(g.get("conversion_pixel_id") or ""))
        # optimization_goal values (PURCHASE, SIGN_UP, LEAD, PAGE_VISIT, ...)
        # match the last_fired_at keys once lower-cased.
        goal = str(g.get("optimization_goal") or "").lower()
        if not pixel or not goal:
            continue
        if goal in pixel["last_fired_at"] and not pixel["last_fired_at"][goal]:
            insights.append(
                f"Ad group '{g.get('name')}' optimizes for {goal.upper()} but pixel "
                f"'{pixel['name'] or pixel['pixel_id']}' has never fired that event."
            )

    return {
        "ad_account_id": account,
        "pixels": result_pixels,
        "total": len(result_pixels),
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Targeting lookups
# ---------------------------------------------------------------------------


def search_reddit_targeting(
    config: AdLoopConfig,
    *,
    kind: str,
    query: str = "",
    country: str = "",
    limit: int = 25,
) -> dict:
    """Look up ids for ad-group targeting (communities, interests, geos, languages, keywords)."""
    kind = (kind or "").strip().lower()
    if kind not in _TARGETING_KINDS:
        raise ValueError(f"kind must be one of {list(_TARGETING_KINDS)}, got '{kind}'.")
    limit = max(1, min(int(limit or 25), 100))
    query = (query or "").strip()

    if kind == "communities":
        if not query:
            raise ValueError("query is required for communities (e.g. 'python').")
        rows = list(
            (reddit_get(config, "targeting/communities/search", {"query": query, "page.size": limit}).get("data") or [])
        )
        items = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "subscribers": r.get("subscriber_count"),
                "categories": r.get("categories") or [],
                "description": (r.get("description") or "")[:160],
            }
            for r in rows
        ]
    elif kind == "interests":
        rows = list(reddit_get(config, "targeting/interests").get("data") or [])
        q = query.lower()
        items = [
            {"id": r.get("id"), "name": r.get("name"), "category": r.get("category")}
            for r in rows
            if not q or q in str(r.get("name") or "").lower() or q in str(r.get("category") or "").lower()
        ]
    elif kind == "geolocations":
        params: dict = {}
        if country:
            params["country"] = country.upper()
        if query:
            params["cities_search"] = query
        if not params:
            raise ValueError(
                "Pass country (ISO code, e.g. 'DE') and/or query (city name) for geolocations."
            )
        rows = list(reddit_get(config, "targeting/geolocations", params).get("data") or [])
        items = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "country": r.get("country"),
                "region": r.get("region"),
                "city": r.get("city"),
                "postal_code": r.get("postal_code"),
            }
            for r in rows
        ]
    elif kind == "languages":
        rows = reddit_get_all(config, "targeting/languages")
        q = query.lower()
        items = [
            r for r in rows
            if not q or q in str(r.get("name") or "").lower() or q == str(r.get("id") or "").lower()
        ]
    else:  # keywords
        if not query:
            raise ValueError("query is required for keywords (comma-separated seed terms).")
        seeds = [s.strip() for s in query.split(",") if s.strip()]
        payload = reddit_post(
            config, "targeting/keyword_suggestions", {"data": {"seed_keywords": seeds}}
        )
        data = payload.get("data")
        rows = data if isinstance(data, list) else (data or {}).get("keywords") or []
        items = [
            r if isinstance(r, dict) else {"keyword": r}
            for r in rows
        ]

    return {
        "kind": kind,
        "query": query,
        "results": items[:limit],
        "total": len(items),
        "note": (
            "Use the returned ids/names in draft_reddit_ad_group targeting. "
            "Communities and interests target by name/id, geolocations by id, "
            "languages by ISO 639-1 code."
        ),
    }
