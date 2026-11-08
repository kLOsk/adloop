"""ChatGPT Ads read and diagnostic tools.

Every function takes ``config`` first and an ``ad_account_id`` that picks
the API key (empty = the default key's account). Output follows the Google
Ads and Reddit read tools: money in currency units, statuses at every level
of the hierarchy, ``insights[]`` with computed findings, and ``compact``
mode for audits.

Two facts about the API shape most of this module:

- **Serving issues are the effective status.** A campaign, ad group or ad
  can be ``active`` and still not serve (paused parent, ad in review,
  payment missing, ...). ``include[]=serving_issues`` returns the blocking
  reasons per entity, so every entity summary carries them and a
  ``serving_state`` derived from them.
- **Conversions need three things.** A conversion source (pixel or
  Conversions API), an event setting that names the event, and the event
  setting attached to the campaign. Without the attachment a campaign
  reports zero goal conversions however many events fire, which is why
  :func:`get_chatgpt_conversion_setup` checks each link of that chain.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from adloop.chatgpt_ads.client import (
    ChatGPTAdsApiError,
    api_get,
    api_get_all,
    api_post,
    exact_from_micros,
    from_micros,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

LEVELS = ("account", "campaign", "ad_group", "ad")
_AGGREGATION = {"account": "ad_account", "campaign": "campaign", "ad_group": "ad_group", "ad": "ad"}

# Delivery and outcome metrics requested from GET insights.
_DELIVERY_METRICS = ["impressions", "clicks", "spend", "ctr", "cpc", "cpm"]
_OUTCOME_METRICS = [
    "conversions",
    "cpa",
    "post_click_cvr",
    "order_created_attributed_sales",
    "order_created_roas",
]
_ID_FIELDS = {
    "account": [],
    "campaign": ["campaign_id", "campaign_name"],
    "ad_group": ["campaign_id", "ad_group_id", "ad_group_name"],
    "ad": ["campaign_id", "ad_group_id", "ad_id", "ad_name"],
}
# Segments of GET insights. Conversions are not available per segment there;
# country and device conversions come from POST /conversions/insights.
_SEGMENT_FIELDS = {
    "country": ["country.name"],
    "device": ["device.type"],
    "platform": ["platform"],
}
_PERFORMANCE_BREAKDOWNS = ("date", "country", "device", "platform")
_CONVERSION_BREAKDOWNS = ("date", "country", "device")
_CLICK_WINDOWS = (7, 14, 30)
_VIEW_WINDOWS = (0, 1)
_TIME_BASES = ("ad_event_time", "conversion_time")

# Lead and purchase style events: what a business usually means by a
# conversion. Page and content views are useful signals but weak goals.
_SOFT_EVENTS = {"page_viewed", "contents_viewed"}
# utm_medium values that unpaid traffic also carries: a tag using one of
# these cannot tell an ad click from a link in a ChatGPT answer.
_UNPAID_MEDIUMS = {"", "referral", "organic", "(none)", "(not set)"}

# Serving issue codes the API returns, in plain words. Codes that only limit
# delivery (some ads still serve) are not blocking.
SERVING_ISSUES = {
    "ad_account_suspended": "the ad account is suspended",
    "ad_account_brand_review_missing_favicon": "the account's brand review needs a brand icon (favicon)",
    "ad_account_brand_review_escalated_review": "the account's brand review was escalated to manual review",
    "ad_account_brand_review_rejected": "the account's brand review was rejected",
    "ad_account_brand_review_in_progress": "the account's brand review is still in progress",
    "ad_account_payment_method_missing_or_unauthorized": "the account has no authorized payment method",
    "ad_account_threshold_payment_failed": "a threshold payment failed",
    "ad_account_persona_verification_not_approved": "advertiser identity verification is not approved",
    "ad_account_persona_verification_step_up_required": "advertiser identity verification needs another step",
    "campaign_not_active": "the campaign is paused or archived",
    "campaign_not_started": "the campaign's start time is in the future",
    "campaign_ended": "the campaign's end time has passed",
    "campaign_has_no_ad_groups": "the campaign has no ad groups",
    "campaign_has_no_serving_ready_ad_groups": "no ad group in the campaign is ready to serve",
    "ad_group_not_active": "the ad group is paused or archived",
    "ad_group_has_no_ads": "the ad group has no ads",
    "ad_group_has_no_serving_ready_ads": "no ad in the ad group is ready to serve",
    "campaign_budget_exhausted": "the campaign budget is spent",
    "ad_account_budget_exhausted": "the account spending limit is reached",
    "ad_not_active": "the ad is paused or archived",
    "review_not_approved": "the creative review was rejected",
    "landing_page_crawl_issue": "OpenAI could not crawl the landing page",
    "ad_in_review": "the ad is still in review",
    "partial_serving_review_not_approved": "some ads were rejected (the others serve)",
    "partial_serving_landing_page_crawl_issue": "some ads have landing-page crawl problems (the others serve)",
    "partial_serving_ad_in_review": "some ads are still in review (the others serve)",
    "ad_over_18_only": "the ad only shows to adults",
    "policy_country_targeting_blocked": "policy blocks the ad in the targeted countries",
    "policy_country_targeting_limited": "policy limits the ad in some targeted countries",
    "title_missing": "the ad has no title",
    "advertiser_name_missing": "the advertiser name is missing",
    "image_or_favicon_missing": "the image or brand icon is missing",
    "target_url_invalid": "the destination URL is invalid",
    "reserved_query_params_present": "the destination URL uses reserved query parameters",
    "product_feed_id_missing": "the product feed is missing",
}
_NON_BLOCKING_ISSUES = {
    "partial_serving_review_not_approved",
    "partial_serving_landing_page_crawl_issue",
    "partial_serving_ad_in_review",
    "policy_country_targeting_limited",
    "ad_over_18_only",
}

_account_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Account, time and money helpers
# ---------------------------------------------------------------------------


def reset_account_cache() -> None:
    _account_cache.clear()


def _cache_key(config: AdLoopConfig, requested: str) -> str:
    """Cache per API key, not per requested id: the default key is requested
    as "" by every tenant of a hosted server, and must never share an entry."""
    import hashlib

    from adloop.chatgpt_ads.client import _api_key

    digest = hashlib.sha256(_api_key(config, requested).encode()).hexdigest()[:16]
    return f"{digest}|{requested}"


def account_meta(config: AdLoopConfig, ad_account_id: str = "", *, fresh: bool = False) -> dict:
    """Name, currency, time zone and reviews of the key's ad account.

    Cached per API key for the currency and time zone the reports need;
    ``fresh`` re-reads it, for callers that report status and reviews.
    Refuses when the key answers for a different account than the one
    asked for, so a wrong key in the config never reports another
    client's numbers under this name.
    """
    requested = (ad_account_id or "").strip()
    key = _cache_key(config, requested)
    cached = _account_cache.get(key)
    if cached is not None and not fresh:
        return cached
    data = api_get(config, "ad_account", ad_account_id=requested)
    actual = str(data.get("id") or "")
    if requested and actual and requested != actual:
        raise ValueError(
            f"The API key configured for {requested} belongs to ad account {actual}. "
            "Fix chatgpt_ads.accounts in the config."
        )
    meta = {
        "ad_account_id": actual or requested,
        "name": data.get("account_name") or data.get("name") or "",
        "brand_name": data.get("brand_name") or data.get("name") or "",
        "legal_name": data.get("legal_name") or data.get("name") or "",
        "url": data.get("url") or "",
        "status": data.get("status"),
        "time_zone": data.get("timezone") or "UTC",
        "currency": data.get("currency_code") or "",
        "review": data.get("review") or {},
        "account_integrity_review": data.get("account_integrity_review") or None,
    }
    _account_cache[key] = meta
    return meta


def _zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _today(time_zone: str) -> date:
    return datetime.now(_zone(time_zone)).date()


def _midnight(day: date, time_zone: str) -> int:
    """Unix seconds of local midnight starting ``day`` in the account zone."""
    return int(datetime(day.year, day.month, day.day, tzinfo=_zone(time_zone)).timestamp())


def _local_time(ts: Any, time_zone: str) -> str | None:
    try:
        moment = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return moment.astimezone(_zone(time_zone)).strftime("%Y-%m-%d %H:%M")


def _local_date(ts: Any, time_zone: str) -> str | None:
    stamp = _local_time(ts, time_zone)
    return stamp[:10] if stamp else None


def _parse_date(value: str, *, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}' — use YYYY-MM-DD (e.g. 2026-10-01).") from exc


def report_window(date_range_start: str, date_range_end: str, time_zone: str) -> dict:
    """Whole account-local days, ending yesterday by default (30 days).

    Conversion and sales metrics need full days, so a window reaching into
    today ends yesterday instead, with a note. Returns the dates and the
    Unix bounds (start inclusive, end exclusive) the API takes.
    """
    today = _today(time_zone)
    yesterday = today - timedelta(days=1)
    notes: list[str] = []
    end = _parse_date(date_range_end, default=yesterday)
    if end >= today:
        end = yesterday
        notes.append(
            "The window ends yesterday: today is a partial day, and conversion "
            "metrics need whole days in the account's time zone."
        )
    start = _parse_date(date_range_start, default=end - timedelta(days=29))
    if start > end:
        raise ValueError(
            f"date_range_start {start} is after the last full day {end}. "
            "Pick a window that ends yesterday or earlier."
        )
    if (end - start).days + 1 > 365:
        raise ValueError("The window can cover at most 365 days.")
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
        "start_ts": _midnight(start, time_zone),
        "end_ts": _midnight(end + timedelta(days=1), time_zone),
        "time_zone": time_zone,
        "notes": notes,
    }


def _unix_range(window: dict, *, as_strings: bool = False) -> str:
    start, end = window["start_ts"], window["end_ts"]
    if as_strings:
        start, end = str(start), str(end)
    return json.dumps({"type": "unix_range", "start": start, "end": end}, separators=(",", ":"))


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _check_choice(name: str, value: Any, allowed: tuple) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {list(allowed)}, got {value!r}.")


def _optional(fn, *args, **kwargs) -> tuple[Any, str | None]:
    """Run a read that some accounts cannot use; (result, reason it failed)."""
    try:
        return fn(*args, **kwargs), None
    except ChatGPTAdsApiError as exc:
        if exc.status in (403, 404):
            return None, str(exc)
        raise


# ---------------------------------------------------------------------------
# Entity summaries
# ---------------------------------------------------------------------------


def _issues(entity: dict) -> list[dict]:
    out = []
    for item in entity.get("serving_issues") or []:
        code = str(item.get("code") if isinstance(item, dict) else item)
        out.append({
            "code": code,
            "meaning": SERVING_ISSUES.get(code, code.replace("_", " ")),
            "blocking": code not in _NON_BLOCKING_ISSUES,
        })
    return out


def serving_state(status: str | None, issues: list[dict]) -> str:
    """One readable state from the configured status and the serving issues."""
    status = (status or "").lower()
    if status == "archived":
        return "archived"
    if status == "paused":
        return "paused"
    blocking = [i for i in issues if i["blocking"]]
    if blocking:
        return "not serving: " + blocking[0]["meaning"]
    if issues:
        return "serving with limits: " + issues[0]["meaning"]
    return "eligible" if status == "active" else (status or "unknown")


def _query_template(entity: dict) -> str:
    config = entity.get("landing_page_configuration") or {}
    return str(config.get("query_string_template") or "")


def _template_params(template: str) -> dict[str, str]:
    return {k: v for k, v in parse_qsl(template.lstrip("?&"), keep_blank_values=True)}


def effective_tracking(target_url: str, *templates: str) -> dict[str, str]:
    """Query parameters an ad's click carries: URL params, then templates.

    Templates combine from campaign to ad group to ad, and the more
    specific level wins on a shared key, as Ads Manager documents.
    """
    params = dict(parse_qsl(urlsplit(target_url or "").query, keep_blank_values=True))
    for template in templates:
        params.update(_template_params(template))
    return params


def _budget(campaign: dict) -> dict:
    budget = campaign.get("budget") or {}
    return {
        "daily": from_micros(budget.get("daily_spend_limit_micros")),
        "lifetime": from_micros(budget.get("lifetime_spend_limit_micros")),
    }


def _locations(block: dict | None) -> list[str]:
    if not block:
        return []
    names = [str(c) for c in block.get("countries") or []]
    for loc in block.get("include") or []:
        if isinstance(loc, dict):
            label = loc.get("name") or loc.get("id")
            kind = loc.get("type")
            names.append(f"{label} ({kind})" if kind else str(label))
    return names


def _targeting(campaign: dict) -> dict:
    targeting = campaign.get("targeting") or {}
    platforms = (targeting.get("platforms") or {}).get("included") or []
    return {
        "locations": _locations(targeting.get("locations")),
        "excluded_locations": _locations(targeting.get("excluded_locations")),
        "platforms": platforms or "all",
        "custom_audiences": len((targeting.get("custom_audiences") or {}).get("ids") or []),
        "excluded_custom_audiences": len(
            (targeting.get("excluded_custom_audiences") or {}).get("ids") or []
        ),
    }


def campaign_summary(campaign: dict, settings_by_id: dict[str, dict] | None = None) -> dict:
    issues = _issues(campaign)
    event_ids = [str(i) for i in campaign.get("conversion_event_setting_ids") or []]
    settings_by_id = settings_by_id or {}
    return {
        "campaign_id": campaign.get("id"),
        "name": campaign.get("name"),
        "status": campaign.get("status"),
        "serving_state": serving_state(campaign.get("status"), issues),
        "serving_issues": issues,
        "bidding_type": campaign.get("bidding_type"),
        "budget": _budget(campaign),
        "start": campaign.get("start_time"),
        "end": campaign.get("end_time"),
        "targeting": _targeting(campaign),
        "conversion_event_setting_ids": event_ids,
        "conversion_events": [
            _event_label(settings_by_id[i]) if i in settings_by_id else i for i in event_ids
        ],
        "tracking_template": _query_template(campaign),
        "mode": campaign.get("mode"),
    }


def _bid(group: dict, bidding_type: str | None) -> dict:
    config = group.get("bidding_config") or {}
    # Unrounded: an impression bid of 4000 micros is 0.004, not 0.0.
    amount = exact_from_micros(config.get("max_bid_micros"))
    billing = config.get("billing_event_type")
    if bidding_type == "conversions":
        meaning = "target cost per conversion (oCPC; billed per click)"
    elif billing == "impression":
        meaning = "maximum per impression (x1000 = CPM)"
    else:
        meaning = "maximum per click"
    return {
        "strategy": config.get("strategy"),
        "billing_event": billing,
        "max_bid": amount,
        "max_bid_meaning": meaning if amount is not None else None,
        "audience_bid_multipliers": len(config.get("custom_audience_bid_multipliers") or []),
    }


# An ad group can hold up to 2,000 context hints; a read lists this many.
MAX_LISTED_HINTS = 50


def ad_group_summary(group: dict, campaign: dict | None = None, *, compact: bool = False) -> dict:
    """One ad group for a read. Context hints are capped at ``MAX_LISTED_HINTS``
    (compact: none listed, only counted)."""
    issues = _issues(group)
    campaign = campaign or {}
    hints = [str(h) for h in group.get("context_hints") or []]
    listed = [] if compact else hints[:MAX_LISTED_HINTS]
    summary = {
        "ad_group_id": group.get("id"),
        "name": group.get("name"),
        "campaign_id": campaign.get("id"),
        "campaign_name": campaign.get("name"),
        "campaign_status": campaign.get("status"),
        "status": group.get("status"),
        "serving_state": serving_state(group.get("status"), issues),
        "serving_issues": issues,
        "bidding": _bid(group, campaign.get("bidding_type")),
        "context_hint_count": len(hints),
        "tracking_template": _query_template(group),
    }
    if not compact:
        summary["context_hints"] = listed
        if len(hints) > len(listed):
            summary["context_hints_truncated"] = True
    return summary


def ad_summary(ad: dict, group: dict | None = None, campaign: dict | None = None) -> dict:
    issues = _issues(ad)
    creative = ad.get("creative") or {}
    review = ad.get("review") or {}
    group = group or {}
    campaign = campaign or {}
    target_url = creative.get("target_url") or ""
    tracking = effective_tracking(
        target_url, _query_template(campaign), _query_template(group), _query_template(ad)
    )
    images = creative.get("images") or []
    return {
        "ad_id": ad.get("id"),
        "name": ad.get("name"),
        "ad_group_id": group.get("id"),
        "ad_group_name": group.get("name"),
        "ad_group_status": group.get("status"),
        "campaign_id": campaign.get("id"),
        "campaign_name": campaign.get("name"),
        "campaign_status": campaign.get("status"),
        "status": ad.get("status"),
        "review_status": ad.get("review_status") or review.get("status"),
        "review_reason": review.get("reason"),
        "serving_state": serving_state(ad.get("status"), issues),
        "serving_issues": issues,
        "creative_type": creative.get("type"),
        "title": creative.get("title"),
        "body": creative.get("body"),
        "target_url": target_url,
        # Lists return only the uploaded file id; the URL resolves on a single read.
        "image_url": creative.get("image_url"),
        "image_count": len(images) if images else (
            1 if creative.get("file_id") or creative.get("image_url") else 0
        ),
        "click_parameters": {k: v for k, v in tracking.items() if k.startswith("utm_") or "{" in v},
    }


def _event_label(setting: dict) -> str:
    name = setting.get("name") or setting.get("id")
    return f"{name} ({event_key(setting)})"


def event_key(setting: dict) -> str:
    """The event name a setting counts: the standard type or the custom name."""
    event_type = str(setting.get("event_type") or "")
    if event_type == "custom":
        return str(setting.get("custom_event_name") or "custom")
    return event_type


# ---------------------------------------------------------------------------
# Structure loading
# ---------------------------------------------------------------------------


def _list_campaigns(config: AdLoopConfig, account: str) -> list[dict]:
    return api_get_all(config, "campaigns", {"include[]": ["serving_issues"]}, ad_account_id=account)


def load_structure(
    config: AdLoopConfig,
    account: str,
    *,
    with_ad_groups: bool = True,
    with_ads: bool = False,
    include_archived: bool = False,
    campaign_id: str = "",
) -> dict:
    """Campaigns, their ad groups and ads, each with its parent attached.

    Ad groups and ads do not carry their parent's id, so they are listed
    per parent. Archived entities are skipped (and their children not
    fetched) unless ``include_archived``.
    """
    campaigns = [
        c for c in _list_campaigns(config, account)
        if (include_archived or c.get("status") != "archived")
        and (not campaign_id or c.get("id") == campaign_id)
    ]
    ad_groups: list[tuple[dict, dict]] = []
    ads: list[tuple[dict, dict, dict]] = []
    if with_ad_groups or with_ads:
        for campaign in campaigns:
            groups = api_get_all(
                config,
                "ad_groups",
                {"campaign_id": campaign["id"], "include[]": ["serving_issues"]},
                ad_account_id=account,
            )
            for group in groups:
                if not include_archived and group.get("status") == "archived":
                    continue
                ad_groups.append((group, campaign))
                if with_ads:
                    for ad in api_get_all(
                        config,
                        "ads",
                        {"ad_group_id": group["id"], "include[]": ["serving_issues"]},
                        ad_account_id=account,
                    ):
                        if not include_archived and ad.get("status") == "archived":
                            continue
                        ads.append((ad, group, campaign))
    return {"campaigns": campaigns, "ad_groups": ad_groups, "ads": ads}


def _event_settings(config: AdLoopConfig, account: str) -> tuple[list[dict], str | None]:
    result, failure = _optional(
        api_get_all, config, "conversions/event_settings", ad_account_id=account
    )
    return result or [], failure


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def get_chatgpt_ads_account(config: AdLoopConfig, *, ad_account_id: str = "") -> dict:
    """Account details, reviews, spending limits and account-level blockers."""
    meta = account_meta(config, ad_account_id, fresh=True)
    account = (ad_account_id or "").strip()
    insights: list[str] = []

    review = meta["review"] or {}
    if review.get("status") and review.get("status") != "approved":
        insights.append(
            f"Brand review is {review.get('status')}"
            + (f" ({review.get('reason')})" if review.get("reason") else "")
            + ": the account cannot serve ads until it is approved."
        )
    integrity = (meta["account_integrity_review"] or {}).get("review") or {}
    if integrity.get("status") and integrity.get("status") != "approved":
        insights.append(
            f"Account integrity review is {integrity.get('status')}"
            + (f" ({integrity.get('reason')})" if integrity.get("reason") else "")
            + "."
        )
    if meta["status"] and meta["status"] != "active":
        insights.append(f"Account status is {meta['status']}, not active.")

    campaigns = _list_campaigns(config, account)
    by_status: dict[str, int] = {}
    account_blockers: dict[str, str] = {}
    for campaign in campaigns:
        status = str(campaign.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        for issue in _issues(campaign):
            if issue["code"].startswith("ad_account_"):
                account_blockers[issue["code"]] = issue["meaning"]
    for code, meaning in account_blockers.items():
        insights.append(f"Account-level blocker on campaigns: {meaning} ({code}).")

    limits, limits_failure = _optional(
        api_get, config, "ad_account/spend_limit_windows", ad_account_id=account
    )
    spend_limits: dict[str, Any] | None = None
    if limits:
        daily = limits.get("daily_limit") or None
        spend_limits = {
            "daily_limit": {
                "amount": from_micros(daily.get("amount_micros")),
                "spent_today": from_micros(daily.get("spent_micros")),
                "remaining_today": from_micros(daily.get("remaining_micros")),
                "start_date": daily.get("start_date"),
                "end_date": daily.get("end_date"),
                "status": daily.get("status"),
            } if daily else None,
            "date_range_limits": [
                {
                    "name": w.get("name"),
                    "start_date": w.get("start_date"),
                    "end_date_exclusive": w.get("end_date"),
                    "amount": from_micros(w.get("amount_micros")),
                    "spent": from_micros(w.get("spent_micros")),
                    "status": w.get("status"),
                }
                for w in limits.get("data") or []
            ],
        }
        if daily and daily.get("remaining_micros") == 0:
            insights.append("Today's account spending limit is used up; ads stop until midnight.")

    accounts, _ = _optional(api_get, config, "ad_accounts", {"limit": 100}, ad_account_id=account)

    return {
        "ad_account_id": meta["ad_account_id"],
        "name": meta["name"],
        "brand_name": meta["brand_name"],
        "legal_name": meta["legal_name"],
        "url": meta["url"],
        "status": meta["status"],
        "time_zone": meta["time_zone"],
        "currency": meta["currency"],
        "brand_review": review,
        "account_integrity_review": meta["account_integrity_review"],
        "campaigns_by_status": by_status,
        "spend_limits": spend_limits,
        "spend_limits_note": (
            "Spending limits need billing permission on the account and are only "
            "available on invoice billing."
            if limits_failure else None
        ),
        "accessible_ad_accounts": [
            {"ad_account_id": a.get("id"), "name": a.get("account_name") or a.get("name")}
            for a in (accounts or {}).get("data") or []
        ] or None,
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Structure tools
# ---------------------------------------------------------------------------


def get_chatgpt_campaigns(
    config: AdLoopConfig, *, ad_account_id: str = "", include_archived: bool = False
) -> dict:
    """Campaigns with budget, bidding, targeting, conversion events and serving state."""
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    settings, _ = _event_settings(config, account)
    settings_by_id = {str(s.get("id")): s for s in settings}
    campaigns = [
        campaign_summary(c, settings_by_id)
        for c in _list_campaigns(config, account)
        if include_archived or c.get("status") != "archived"
    ]

    insights: list[str] = []
    for c in campaigns:
        if c["status"] == "active" and c["serving_state"].startswith("not serving"):
            insights.append(f"Campaign '{c['name']}' is active but {c['serving_state']}.")
        if c["status"] != "archived" and not c["conversion_event_setting_ids"]:
            insights.append(
                f"Campaign '{c['name']}' has no conversion event attached: it reports "
                "0 conversions whatever the site sends. See get_chatgpt_conversion_setup."
            )
        if c["status"] != "archived" and not c["targeting"]["locations"]:
            insights.append(
                f"Campaign '{c['name']}' has no location targeting, so it can serve "
                "in every available location."
            )
    return {
        "ad_account_id": meta["ad_account_id"],
        "currency": meta["currency"],
        "campaigns": campaigns,
        "total_campaigns": len(campaigns),
        "insights": insights,
        "note": (
            "Budgets and bids are in account currency. 'status' is what was set; "
            "'serving_state' adds what OpenAI reports as blocking delivery."
        ),
    }


def get_chatgpt_ad_groups(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    include_archived: bool = False,
    compact: bool = False,
) -> dict:
    """Ad groups with bids, context hints and serving state, with their campaign's status.

    Each ad group lists at most ``MAX_LISTED_HINTS`` context hints and counts
    them all; ``compact`` lists none (counts only).
    """
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    structure = load_structure(
        config, account, include_archived=include_archived, campaign_id=campaign_id.strip()
    )
    groups = [ad_group_summary(g, c, compact=compact) for g, c in structure["ad_groups"]]

    insights: list[str] = []
    for g in groups:
        codes = {i["code"] for i in g["serving_issues"]}
        if "ad_group_has_no_ads" in codes and g["status"] != "archived":
            insights.append(f"Ad group '{g['name']}' has no ads.")
        elif g["status"] == "active" and g["serving_state"].startswith("not serving"):
            insights.append(f"Ad group '{g['name']}' is active but {g['serving_state']}.")
        if not g["context_hint_count"] and g["status"] == "active":
            insights.append(
                f"Ad group '{g['name']}' has no context hints; OpenAI decides alone "
                "which conversations it fits."
            )
    result = {
        "ad_account_id": meta["ad_account_id"],
        "currency": meta["currency"],
        "ad_groups": groups,
        "total_ad_groups": len(groups),
        "insights": insights,
    }
    if compact:
        result["compact"] = True
    elif any(g.get("context_hints_truncated") for g in groups):
        result["note"] = (
            f"Ad groups with more than {MAX_LISTED_HINTS} context hints list the first "
            f"{MAX_LISTED_HINTS}; context_hint_count has the total."
        )
    return result


def get_chatgpt_ads(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    include_archived: bool = False,
) -> dict:
    """Ads with copy, destination, review state, serving state and click parameters."""
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    structure = load_structure(
        config, account, with_ads=True, include_archived=include_archived,
        campaign_id=campaign_id.strip(),
    )
    ads = [
        ad_summary(a, g, c) for a, g, c in structure["ads"]
        if not ad_group_id or g.get("id") == ad_group_id
    ]

    insights: list[str] = []
    rejected = [a for a in ads if a["review_status"] == "rejected"]
    if rejected:
        insights.append(
            f"{len(rejected)} ad(s) were rejected in review: "
            + ", ".join(
                f"'{a['name']}'" + (f" ({a['review_reason']})" if a["review_reason"] else "")
                for a in rejected[:5]
            )
            + ". Edit the creative or landing page to resubmit."
        )
    in_review = [a for a in ads if a["review_status"] == "in_review" and a["status"] == "active"]
    if in_review:
        insights.append(f"{len(in_review)} active ad(s) are still in review.")
    blocked = [
        a for a in ads
        if a["status"] == "active" and a["review_status"] == "approved"
        and a["serving_state"].startswith("not serving")
    ]
    for a in blocked[:5]:
        insights.append(f"Approved, active ad '{a['name']}' is {a['serving_state']}.")
    untagged = [a for a in ads if a["status"] != "archived" and "utm_source" not in a["click_parameters"]]
    if untagged:
        insights.append(
            f"{len(untagged)} ad(s) send clicks without utm_source, so Google Analytics "
            "files them under the chatgpt.com referral with unpaid ChatGPT traffic. "
            "Add utm_source/utm_medium to the URL or a tracking template."
        )
    return {
        "ad_account_id": meta["ad_account_id"],
        "ads": ads,
        "total_ads": len(ads),
        "insights": insights,
        "note": (
            "click_parameters merges the destination URL's utm_* parameters with the "
            "campaign, ad group and ad tracking templates (the more specific level "
            "wins). Account-level default templates are not visible through the API."
        ),
    }


# ---------------------------------------------------------------------------
# Performance (GET insights)
# ---------------------------------------------------------------------------


_MAX_INSIGHT_PAGES = 25


def _insights(config: AdLoopConfig, account: str, path: str, params: dict) -> tuple[list[dict], bool]:
    """Every page of an insights report; True when it stopped at the page cap."""
    query = dict(params)
    rows: list[dict] = []
    for _ in range(_MAX_INSIGHT_PAGES):
        payload = api_get(config, path, query, ad_account_id=account)
        page = payload.get("data") or []
        rows.extend(r for r in page if isinstance(r, dict))
        if not payload.get("has_more") or not payload.get("last_id") or not page:
            return rows, False
        query["after"] = payload["last_id"]
    return rows, True


def _truncation_note(rows: list[dict]) -> str:
    return (
        f"The report stopped at {len(rows)} rows (page limit), so totals cover only those "
        "rows. Narrow the date range, the level or the breakdown."
    )


def _metric(row: dict, name: str, prefixes: tuple[str, ...]) -> Any:
    if name in row:
        return row.get(name)
    for prefix in prefixes:
        key = f"{prefix}_{name}"
        if key in row:
            return row.get(key)
    return None


def _performance_row(row: dict, level: str, segment: str, currency: str, time_zone: str) -> dict:
    prefixes = tuple(p for p in (segment, _AGGREGATION[level]) if p)
    out: dict[str, Any] = {}
    for key in _ID_FIELDS[level]:
        if row.get(key) is not None:
            out[key] = row.get(key)
    if segment == "date":
        out["date"] = row.get("readable_time") or _local_date(row.get("start_time"), time_zone)
    elif segment == "country":
        out["country"] = row.get("country_name") or row.get("country")
    elif segment == "device":
        out["device"] = row.get("device_type") or row.get("device")
    elif segment == "platform":
        out["platform"] = row.get("platform")
    impressions = int(_num(_metric(row, "impressions", prefixes)) or 0)
    clicks = int(_num(_metric(row, "clicks", prefixes)) or 0)
    spend = _num(_metric(row, "spend", prefixes)) or 0.0
    out.update({
        "impressions": impressions,
        "clicks": clicks,
        "spend": round(spend, 2),
        "ctr": _round(_num(_metric(row, "ctr", prefixes)), 4),
        "cpc": _round(_num(_metric(row, "cpc", prefixes))),
        "cpm": _round(_num(_metric(row, "cpm", prefixes))),
    })
    if segment in ("", "date"):
        conversions = _num(row.get("conversions"))
        sales = _num(row.get("order_created_attributed_sales"))
        out.update({
            "conversions": conversions if conversions is None else int(conversions),
            "cpa": _round(_num(row.get("cpa"))),
            "post_click_cvr": _round(_num(row.get("post_click_cvr")), 4),
            "attributed_sales": _round(sales),
            "roas": _round(_num(row.get("order_created_roas"))),
        })
        if out["cpa"] is None and conversions:
            out["cpa"] = round(spend / conversions, 2)
    out["currency"] = currency
    return out


def _attach_status(rows: list[dict], level: str, structure: dict, settings_ids: set[str]) -> None:
    campaigns = {str(c.get("id")): c for c in structure["campaigns"]}
    groups = {str(g.get("id")): (g, c) for g, c in structure["ad_groups"]}
    ads = {str(a.get("id")): (a, g, c) for a, g, c in structure["ads"]}
    for row in rows:
        campaign = None
        if level == "ad" and str(row.get("ad_id")) in ads:
            ad, group, campaign = ads[str(row["ad_id"])]
            row["ad_status"] = ad.get("status")
            row["ad_review_status"] = ad.get("review_status")
            row["ad_group_name"] = group.get("name")
            row["ad_group_status"] = group.get("status")
            row["serving_state"] = serving_state(ad.get("status"), _issues(ad))
        elif level == "ad_group" and str(row.get("ad_group_id")) in groups:
            group, campaign = groups[str(row["ad_group_id"])]
            row["ad_group_status"] = group.get("status")
            row["serving_state"] = serving_state(group.get("status"), _issues(group))
        elif level == "campaign" and str(row.get("campaign_id")) in campaigns:
            campaign = campaigns[str(row["campaign_id"])]
            row["serving_state"] = serving_state(campaign.get("status"), _issues(campaign))
        if campaign is None and str(row.get("campaign_id")) in campaigns:
            campaign = campaigns[str(row["campaign_id"])]
        if campaign is not None:
            row.setdefault("campaign_name", campaign.get("name"))
            row["campaign_status"] = campaign.get("status")
            ids = {str(i) for i in campaign.get("conversion_event_setting_ids") or []}
            row["conversion_events_attached"] = bool(ids & settings_ids) if settings_ids else bool(ids)


def _totals(rows: list[dict], currency: str, with_outcomes: bool) -> dict:
    spend = round(sum(r.get("spend") or 0 for r in rows), 2)
    clicks = sum(r.get("clicks") or 0 for r in rows)
    impressions = sum(r.get("impressions") or 0 for r in rows)
    totals: dict[str, Any] = {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions, 4) if impressions else None,
        "cpc": round(spend / clicks, 2) if clicks else None,
        "currency": currency,
    }
    if with_outcomes:
        conversions = sum(r.get("conversions") or 0 for r in rows)
        sales = sum(r.get("attributed_sales") or 0 for r in rows)
        totals.update({
            "conversions": conversions,
            "cpa": round(spend / conversions, 2) if conversions else None,
            "conversion_rate": round(conversions / clicks, 4) if clicks else None,
            "attributed_sales": round(sales, 2) if sales else None,
            "roas": round(sales / spend, 2) if sales and spend else None,
        })
    return totals


def get_chatgpt_performance(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    level: str = "campaign",
    date_range_start: str = "",
    date_range_end: str = "",
    breakdown: str = "",
    compact: bool = False,
    click_window_days: int = 30,
    view_window_days: int = 1,
) -> dict:
    """Spend, delivery and conversions per entity, with statuses at every level."""
    level = (level or "campaign").strip().lower()
    breakdown = (breakdown or "").strip().lower()
    _check_choice("level", level, LEVELS)
    if breakdown:
        _check_choice("breakdown", breakdown, _PERFORMANCE_BREAKDOWNS)
    _check_choice("click_window_days", click_window_days, _CLICK_WINDOWS)
    _check_choice("view_window_days", view_window_days, _VIEW_WINDOWS)

    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    currency = meta["currency"]
    window = report_window(date_range_start, date_range_end, meta["time_zone"])
    segment = breakdown if breakdown in _SEGMENT_FIELDS else ""
    with_outcomes = not segment

    fields = list(_ID_FIELDS[level])
    if breakdown == "date":
        fields.append("readable_time")
    if segment:
        fields += _SEGMENT_FIELDS[segment] + [f"{segment}.{m}" for m in _DELIVERY_METRICS]
    else:
        fields += _DELIVERY_METRICS + _OUTCOME_METRICS
    params: dict[str, Any] = {
        "aggregation_level": _AGGREGATION[level],
        "time_granularity": "daily" if breakdown == "date" else "none",
        "time_ranges[]": [_unix_range(window)],
        "fields[]": fields,
        "limit": 2000,
    }
    if segment:
        params["segments[]"] = [segment]
    else:
        params["attribution_window_days"] = click_window_days
        params["view_through_attribution_window_days"] = view_window_days

    raw, truncated = _insights(config, account, "ad_account/insights", params)
    rows = [_performance_row(r, level, breakdown, currency, meta["time_zone"]) for r in raw]

    structure = {"campaigns": [], "ad_groups": [], "ads": []}
    settings_ids: set[str] = set()
    if level != "account":
        structure = load_structure(
            config, account,
            with_ad_groups=level in ("ad_group", "ad"),
            with_ads=level == "ad",
            include_archived=True,
        )
        settings, _ = _event_settings(config, account)
        settings_ids = {str(s.get("id")) for s in settings if not s.get("archived")}
        _attach_status(rows, level, structure, settings_ids)

    rows.sort(key=lambda r: r.get("spend") or 0, reverse=True)
    insights: list[str] = list(window["notes"])
    if truncated:
        insights.insert(0, _truncation_note(rows))
    name_key = {"campaign": "campaign_name", "ad_group": "ad_group_name", "ad": "ad_name"}.get(level)

    zero_conv: list[dict] = []
    if with_outcomes and name_key and breakdown != "date":
        zero_conv = [r for r in rows if (r.get("spend") or 0) > 0 and not r.get("conversions")]
        if zero_conv:
            wasted = round(sum(r["spend"] for r in zero_conv), 2)
            insights.append(
                f"{len(zero_conv)} {level.replace('_', ' ')}(s) spent {wasted} {currency} "
                "with zero conversions: "
                + ", ".join(str(r.get(name_key)) for r in zero_conv[:5]) + "."
            )
        unmeasured = [r for r in zero_conv if r.get("conversion_events_attached") is False]
        if unmeasured:
            insights.append(
                f"{len(unmeasured)} of them belong to campaigns with no conversion event "
                "attached, so 0 is guaranteed rather than measured. Run "
                "get_chatgpt_conversion_setup."
            )
        paused = [
            r for r in rows
            if (r.get("spend") or 0) > 0
            and {r.get("ad_status"), r.get("ad_group_status"), r.get("campaign_status")}
            & {"paused", "archived"}
        ]
        if paused:
            insights.append(
                f"{len(paused)} {level.replace('_', ' ')}(s) spent in this window but are "
                "paused or archived now (or sit under a paused campaign)."
            )
    if segment:
        insights.append(
            f"Conversions are not reported per {segment} here. For conversions by "
            "country or device use get_chatgpt_conversions with that breakdown."
        )
    if not rows:
        insights.append("No delivery in this window.")

    result: dict[str, Any] = {
        "ad_account_id": meta["ad_account_id"],
        "level": level,
        "breakdown": breakdown or None,
        "date_range": {k: window[k] for k in ("start", "end", "days", "time_zone")},
        "attribution": (
            {"click_window_days": click_window_days, "view_window_days": view_window_days}
            if with_outcomes else None
        ),
        "currency": currency,
        "truncated": truncated,
        "totals": _totals(rows, currency, with_outcomes),
        "insights": insights,
        "note": (
            "Spend, CPC and CPM are in account currency. 'conversions' counts goal "
            "conversions from clicks plus views inside the attribution windows; it only "
            "counts events attached to the campaign as conversion events. Conversion "
            "data settles over about a day."
        ),
    }
    if compact:
        result["compact"] = True
        result["total_rows"] = len(rows)
        result["rows_top_spend"] = rows[:10]
        result["zero_conversion_spenders"] = [
            {k: r.get(k) for k in (*_ID_FIELDS[level], "spend", "clicks")} for r in zero_conv[:10]
        ]
    else:
        result["rows"] = rows
        result["total_rows"] = len(rows)
    return result


# ---------------------------------------------------------------------------
# Conversions (POST /conversions/insights)
# ---------------------------------------------------------------------------


def _conversion_rows(
    config: AdLoopConfig,
    account: str,
    *,
    level: str,
    window: dict,
    entity_ids: list[str] | None,
    breakdown: str,
    include_events: bool,
    event_names: list[str] | None,
    click_window_days: int,
    view_window_days: int,
    time_basis: str,
) -> tuple[list[dict], str]:
    """Run the conversion report, splitting entity lists to stay under 2,000 rows."""
    base: dict[str, Any] = {
        "aggregation_level": "campaign" if level == "account" else level,
        "time_ranges": [_unix_range(window, as_strings=True)],
        "time_granularity": "daily" if breakdown == "date" else "none",
        "attribution_window_days": click_window_days,
        "view_through_attribution_window_days": view_window_days,
        "attribution_time_basis": time_basis,
    }
    if breakdown in ("country", "device"):
        base["breakdown"] = breakdown
    if include_events:
        base["include"] = ["attributed_events"]
        if event_names:
            base["event_names"] = event_names

    if level == "account" or not entity_ids:
        base["group_by_entity"] = False
        return _post_conversions(config, account, base, window, None)

    per_entity_rows = window["days"] if breakdown == "date" else (60 if breakdown else 1)
    chunk = max(1, 1500 // per_entity_rows)
    rows: list[dict] = []
    currency = ""
    for i in range(0, len(entity_ids), chunk):
        part, part_currency = _post_conversions(config, account, base, window, entity_ids[i : i + chunk])
        rows.extend(part)
        currency = currency or part_currency
    return rows, currency


def _split_window(window: dict) -> tuple[dict, dict]:
    """Two halves of a window, on whole account-local days."""
    start = date.fromisoformat(window["start"])
    end = date.fromisoformat(window["end"])
    middle = start + timedelta(days=(end - start).days // 2)
    halves = []
    for first, last in ((start, middle), (middle + timedelta(days=1), end)):
        halves.append({
            **window,
            "start": first.isoformat(),
            "end": last.isoformat(),
            "days": (last - first).days + 1,
            "start_ts": _midnight(first, window["time_zone"]),
            "end_ts": _midnight(last + timedelta(days=1), window["time_zone"]),
        })
    return halves[0], halves[1]


def _post_conversions(
    config: AdLoopConfig, account: str, base: dict, window: dict, entity_ids: list[str] | None,
    depth: int = 0,
) -> tuple[list[dict], str]:
    """POST one conversion report; on 413 split the entities, then the dates.

    The endpoint caps a response at 2,000 summary rows and, separately, at
    2,000 expanded event rows, and answers 413 instead of a partial report.
    The event count is unknown in advance, so the request is split on demand.
    """
    body = dict(base, time_ranges=[_unix_range(window, as_strings=True)])
    if entity_ids is not None:
        body["entity_ids"] = entity_ids
    try:
        payload = api_post(config, "conversions/insights", body, ad_account_id=account, mutation=False)
        return list(payload.get("data") or []), str(payload.get("account_currency") or "")
    except ChatGPTAdsApiError as exc:
        if exc.status != 413 or depth >= 12:
            raise
    split_dates = False
    if entity_ids is not None and len(entity_ids) > 1:
        half = len(entity_ids) // 2
        parts = [entity_ids[:half], entity_ids[half:]]
        results = [_post_conversions(config, account, base, window, p, depth + 1) for p in parts]
    elif window["days"] > 1:
        split_dates = True
        results = [
            _post_conversions(config, account, base, w, entity_ids, depth + 1) for w in _split_window(window)
        ]
    else:
        raise ChatGPTAdsApiError(
            "The conversion report for one entity and one day is still over OpenAI's 2,000-row "
            "limit; pass event_names to select fewer events.",
            status=413,
        )
    rows = [row for part, _ in results for row in part]
    if split_dates:
        # The same entity (and segment) now comes back once per half of the
        # window and has to be added up again.
        rows = _merge_conversion_rows(rows)
    return rows, next((cur for _, cur in results if cur), "")


_ROW_KEYS = ("entity_id", "date", "start_time", "readable_time", "country", "device")
_ROW_SUMS = (
    "conversions", "click_through_conversions", "view_through_conversions",
    "order_created_attributed_sales",
)
_EVENT_SUMS = (
    "attributed_event_count", "click_through_conversions", "view_through_conversions",
    "attributed_event_value_amount", "attributed_event_value_count",
)
_BREAKDOWN_SUMS = ("conversions", "click_through_conversions", "view_through_conversions")


_MONEY_SUMS = {"order_created_attributed_sales", "attributed_event_value_amount"}


def _decimal(value: Any):
    from decimal import Decimal, InvalidOperation

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _add(target: dict, source: dict, fields: tuple[str, ...]) -> None:
    """Add counts as integers and money as exact decimals (OpenAI sends
    unrounded decimal strings); rounding happens only when shown."""
    for field in fields:
        if field in _MONEY_SUMS:
            a, b = _decimal(target.get(field)), _decimal(source.get(field))
            if a is None and b is None:
                continue
            target[field] = str((a or 0) + (b or 0))
            continue
        a, b = _num(target.get(field)), _num(source.get(field))
        if a is None and b is None:
            continue
        target[field] = int((a or 0) + (b or 0))


def _merge_conversion_rows(rows: list[dict]) -> list[dict]:
    """Add up conversion rows that describe the same entity and segment."""
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row.get(k) for k in _ROW_KEYS)
        if key not in merged:
            merged[key] = {
                **row,
                "attributed_events": [
                    {**e, "conversion_event_setting_breakdowns": [
                        dict(b) for b in e.get("conversion_event_setting_breakdowns") or []
                    ]}
                    for e in row.get("attributed_events") or []
                ],
            }
            continue
        target = merged[key]
        _add(target, row, _ROW_SUMS)
        events = {(e.get("event_name"), e.get("event_kind")): e for e in target["attributed_events"]}
        for event in row.get("attributed_events") or []:
            ekey = (event.get("event_name"), event.get("event_kind"))
            if ekey not in events:
                copy = {**event, "conversion_event_setting_breakdowns": [
                    dict(b) for b in event.get("conversion_event_setting_breakdowns") or []
                ]}
                target["attributed_events"].append(copy)
                events[ekey] = copy
                continue
            _add(events[ekey], event, _EVENT_SUMS)
            by_setting = {
                b.get("conversion_event_setting_id"): b
                for b in events[ekey]["conversion_event_setting_breakdowns"]
            }
            for b in event.get("conversion_event_setting_breakdowns") or []:
                sid = b.get("conversion_event_setting_id")
                if sid in by_setting:
                    _add(by_setting[sid], b, _BREAKDOWN_SUMS)
                else:
                    by_setting[sid] = dict(b)
                    events[ekey]["conversion_event_setting_breakdowns"].append(by_setting[sid])
    return list(merged.values())


def _event_row(event: dict, setting_names: dict[str, str]) -> dict:
    goal_settings = [
        setting_names.get(str(b.get("conversion_event_setting_id")), str(b.get("conversion_event_setting_id")))
        for b in event.get("conversion_event_setting_breakdowns") or []
    ]
    return {
        "event_name": event.get("event_name"),
        "kind": event.get("event_kind"),
        "count": event.get("attributed_event_count") or 0,
        "click_through": event.get("click_through_conversions"),
        "view_through": event.get("view_through_conversions"),
        "value": _round(_num(event.get("attributed_event_value_amount"))),
        "value_currency": event.get("attributed_event_value_currency"),
        "counted_as_goal_by": goal_settings,
    }


def get_chatgpt_conversions(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    level: str = "campaign",
    date_range_start: str = "",
    date_range_end: str = "",
    breakdown: str = "",
    include_events: bool = True,
    event_names: list[str] | None = None,
    click_window_days: int = 30,
    view_window_days: int = 1,
    time_basis: str = "ad_event_time",
) -> dict:
    """Goal conversions (click- and view-through), sales and every attributed event."""
    level = (level or "campaign").strip().lower()
    breakdown = (breakdown or "").strip().lower()
    _check_choice("level", level, LEVELS)
    if breakdown:
        _check_choice("breakdown", breakdown, _CONVERSION_BREAKDOWNS)
    _check_choice("click_window_days", click_window_days, _CLICK_WINDOWS)
    _check_choice("view_window_days", view_window_days, _VIEW_WINDOWS)
    _check_choice("time_basis", time_basis, _TIME_BASES)
    if event_names and not include_events:
        raise ValueError("event_names needs include_events=true.")

    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    window = report_window(date_range_start, date_range_end, meta["time_zone"])

    structure = load_structure(
        config, account,
        with_ad_groups=level in ("ad_group", "ad"),
        with_ads=level == "ad",
        include_archived=True,
    )
    settings, _ = _event_settings(config, account)
    setting_names = {str(s.get("id")): _event_label(s) for s in settings}
    names: dict[str, dict] = {}
    for c in structure["campaigns"]:
        names[str(c.get("id"))] = {"name": c.get("name"), "status": c.get("status"),
                                   "campaign_id": c.get("id"), "campaign": c}
    if level == "ad_group":
        names = {
            str(g.get("id")): {"name": g.get("name"), "status": g.get("status"),
                               "campaign_id": c.get("id"), "campaign": c}
            for g, c in structure["ad_groups"]
        }
    elif level == "ad":
        names = {
            str(a.get("id")): {"name": a.get("name"), "status": a.get("status"),
                               "campaign_id": c.get("id"), "campaign": c, "ad_group": g}
            for a, g, c in structure["ads"]
        }

    entity_ids = None if level == "account" else sorted(names)
    if level != "account" and not entity_ids:
        return {
            "ad_account_id": meta["ad_account_id"],
            "level": level,
            "date_range": {k: window[k] for k in ("start", "end", "days", "time_zone")},
            "rows": [],
            "insights": [f"The account has no {level.replace('_', ' ')}s."],
        }

    raw, currency = _conversion_rows(
        config, account, level=level, window=window, entity_ids=entity_ids,
        breakdown=breakdown, include_events=include_events, event_names=event_names,
        click_window_days=click_window_days, view_window_days=view_window_days,
        time_basis=time_basis,
    )
    currency = currency or meta["currency"]

    rows: list[dict] = []
    for r in raw:
        entity = names.get(str(r.get("entity_id")), {})
        campaign = entity.get("campaign") or {}
        row: dict[str, Any] = {
            "entity_id": r.get("entity_id"),
            "name": entity.get("name") if level != "account" else meta["name"],
            "status": entity.get("status"),
        }
        if level == "ad" and entity.get("ad_group"):
            group = entity["ad_group"]
            row["ad_group_id"] = group.get("id")
            row["ad_group_name"] = group.get("name")
            row["ad_group_status"] = group.get("status")
        if level in ("ad_group", "ad"):
            row["campaign_name"] = campaign.get("name")
            row["campaign_status"] = campaign.get("status")
        for key in ("date", "country", "device"):
            if r.get(key) is not None:
                row[key] = r.get(key)
        row.update({
            "conversions": r.get("conversions") or 0,
            "click_through": r.get("click_through_conversions"),
            "view_through": r.get("view_through_conversions"),
            "purchase_value": _round(_num(r.get("order_created_attributed_sales"))),
        })
        if level != "account":
            row["conversion_events_attached"] = [
                setting_names.get(str(i), str(i))
                for i in campaign.get("conversion_event_setting_ids") or []
            ]
        if include_events:
            row["events"] = [_event_row(e, setting_names) for e in r.get("attributed_events") or []]
        rows.append(row)

    rows.sort(key=lambda x: (x.get("conversions") or 0), reverse=True)
    total = sum(r["conversions"] for r in rows)
    clicks_through = sum(r.get("click_through") or 0 for r in rows)
    views_through = sum(r.get("view_through") or 0 for r in rows)
    event_totals: dict[str, int] = {}
    for r in rows:
        for e in r.get("events") or []:
            key = str(e["event_name"])
            event_totals[key] = event_totals.get(key, 0) + (e["count"] or 0)

    insights: list[str] = list(window["notes"])
    if level != "account":
        no_goal = [r for r in rows if not r.get("conversion_events_attached")]
        if no_goal:
            insights.append(
                f"{len(no_goal)} row(s) belong to campaigns without a conversion event, "
                "so their goal conversions stay 0 even when events fire."
            )
        events_no_goal = [
            r for r in rows if not r["conversions"] and any(e["count"] for e in r.get("events") or [])
        ]
        if events_no_goal:
            insights.append(
                f"{len(events_no_goal)} row(s) have attributed events but no goal "
                "conversions: the site sends events, but not the one the campaign counts."
            )
    if total and views_through / total > 0.5:
        insights.append(
            f"{views_through} of {total} conversions are view-through (an impression, no "
            "click). Compare with view_window_days=0 before judging cost per conversion."
        )
    if include_events and not event_totals:
        insights.append(
            "No attributed events at all in this window: either no clicks led to a "
            "measured visit, or the pixel/Conversions API is not sending. Run "
            "get_chatgpt_conversion_setup."
        )

    return {
        "ad_account_id": meta["ad_account_id"],
        "level": level,
        "breakdown": breakdown or None,
        "date_range": {k: window[k] for k in ("start", "end", "days", "time_zone")},
        "attribution": {
            "click_window_days": click_window_days,
            "view_window_days": view_window_days,
            "time_basis": time_basis,
        },
        "currency": currency,
        "totals": {
            "conversions": total,
            "click_through": clicks_through,
            "view_through": views_through,
            "attributed_events": event_totals,
        },
        "rows": rows,
        "insights": insights,
        "note": (
            "'conversions' counts the campaign's goal events only. 'events' lists every "
            "attributed event (goal or not), so a campaign can show purchases or leads "
            "there while its goal count stays 0. Conversion data settles over about a day."
        ),
    }


# ---------------------------------------------------------------------------
# Conversion setup diagnosis
# ---------------------------------------------------------------------------


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def _recent_events(config: AdLoopConfig, account: str, pixel_id: str) -> tuple[list[dict] | None, str | None]:
    payload, failure = _optional(
        api_get, config, "conversions/events", {"pid": pixel_id, "limit": 50}, ad_account_id=account
    )
    if payload is None:
        return None, failure
    return [e for e in payload.get("data") or [] if isinstance(e, dict)], None


def get_chatgpt_conversion_setup(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    check_recent_events: bool = True,
    min_clicks: int = 30,
) -> dict:
    """Audit every link of the conversion chain: source → event setting → campaign → events."""
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    window = report_window(date_range_start, date_range_end, meta["time_zone"])
    checks: list[dict] = []
    insights: list[str] = list(window["notes"])

    pixels, pixels_failure = _optional(
        api_get_all, config, "conversions/pixels", ad_account_id=account
    )
    settings, settings_failure = _event_settings(config, account)
    live_settings = [s for s in settings if not s.get("archived")]
    structure = load_structure(config, account, with_ads=True, include_archived=False)
    campaigns = structure["campaigns"]

    # 1. Conversion source
    if pixels:
        checks.append(_check(
            "conversion source", "pass",
            f"{len(pixels)} pixel(s): " + ", ".join(
                f"{p.get('name')} (pixel_id {p.get('pixel_id')})" for p in pixels
            ) + ".",
        ))
    elif pixels_failure:
        sources = {str(src.get("id")): src.get("name") for s in settings for src in s.get("sources") or []}
        checks.append(_check(
            "conversion source", "info" if sources else "warn",
            "The pixel list is not available to this API key ("
            + pixels_failure + "). "
            + (f"Event settings name these sources: {', '.join(str(v) for v in sources.values())}."
               if sources else "No event setting names a source either."),
        ))
    else:
        checks.append(_check(
            "conversion source", "fail",
            "No pixel or Conversions API source exists. Create one in Ads Manager "
            "(Conversions) and install the Measurement Pixel on the site.",
        ))

    # 2. Event settings
    if settings_failure:
        checks.append(_check("conversion events", "info", f"Event settings unavailable: {settings_failure}"))
    elif not live_settings:
        checks.append(_check(
            "conversion events", "fail",
            "No conversion event is defined, so nothing can count as a conversion. "
            "Create one (e.g. lead_created for form leads) under Conversions in Ads Manager.",
        ))
    else:
        soft = [s for s in live_settings if event_key(s) in _SOFT_EVENTS]
        detail = ", ".join(_event_label(s) for s in live_settings) + "."
        if soft and len(soft) == len(live_settings):
            checks.append(_check(
                "conversion events", "warn",
                detail + " All of them are page or content views, which count visits "
                "rather than leads or sales.",
            ))
        else:
            checks.append(_check("conversion events", "pass", detail))
        unsourced = [s for s in live_settings if not s.get("source_ids")]
        if unsourced:
            checks.append(_check(
                "event sources", "fail",
                "Event setting(s) without a source: "
                + ", ".join(_event_label(s) for s in unsourced) + ".",
            ))
        unused = [s for s in live_settings if not s.get("campaigns")]
        if unused:
            checks.append(_check(
                "unused events", "info",
                "Defined but not attached to any campaign: "
                + ", ".join(_event_label(s) for s in unused) + ".",
            ))

    # 3. Attributed activity per campaign (clicks, spend, events in the window)
    activity: dict[str, dict] = {}
    if campaigns:
        delivery, _ = _insights(config, account, "ad_account/insights", {
            "aggregation_level": "campaign",
            "time_granularity": "none",
            "time_ranges[]": [_unix_range(window)],
            "fields[]": ["campaign_id", "campaign_name", "impressions", "clicks", "spend", "conversions"],
            "limit": 2000,
        })
        for r in delivery:
            activity[str(r.get("campaign_id"))] = {
                "clicks": int(_num(_metric(r, "clicks", ("campaign",))) or 0),
                "spend": round(_num(_metric(r, "spend", ("campaign",))) or 0.0, 2),
                "goal_conversions": int(_num(r.get("conversions")) or 0),
                "events": {},
            }
        conv_rows, _ = _conversion_rows(
            config, account, level="campaign", window=window,
            entity_ids=[str(c.get("id")) for c in campaigns], breakdown="",
            include_events=True, event_names=None, click_window_days=30,
            view_window_days=1, time_basis="ad_event_time",
        )
        for r in conv_rows:
            entry = activity.setdefault(str(r.get("entity_id")), {
                "clicks": 0, "spend": 0.0, "goal_conversions": 0, "events": {},
            })
            entry["goal_conversions"] = r.get("conversions") or entry["goal_conversions"]
            for e in r.get("attributed_events") or []:
                entry["events"][str(e.get("event_name"))] = e.get("attributed_event_count") or 0

    # 4. Campaign attachment: a running campaign without an event is a
    # failure; a paused one that did not spend only needs fixing before reuse.
    live_ids = {str(s.get("id")) for s in live_settings}
    settings_by_id = {str(s.get("id")): s for s in settings}
    for campaign in campaigns:
        attached = [str(i) for i in campaign.get("conversion_event_setting_ids") or []]
        good = [i for i in attached if i in live_ids] if live_ids else attached
        name = campaign.get("name")
        running = campaign.get("status") == "active" or (
            (activity.get(str(campaign.get("id"))) or {}).get("clicks", 0) > 0
        )
        severity = "fail" if running else "warn"
        if not attached:
            checks.append(_check(
                f"campaign '{name}': conversion event", severity,
                "No conversion event attached: this campaign reports 0 conversions "
                "whatever fires on the site. Attach the lead/purchase event in the "
                "campaign's settings."
                + ("" if running else " (Paused and no clicks in the window.)"),
            ))
        elif not good:
            checks.append(_check(
                f"campaign '{name}': conversion event", severity,
                "Only archived conversion events are attached.",
            ))
        else:
            labels = [_event_label(settings_by_id[i]) for i in good if i in settings_by_id] or good
            checks.append(_check(
                f"campaign '{name}': conversion event", "pass", "Counts " + ", ".join(labels) + ".",
            ))
        if campaign.get("bidding_type") == "conversions":
            goal = settings_by_id.get(good[0]) if good else None
            ok = bool(goal) and goal.get("event_type") != "custom" and not goal.get("archived")
            checks.append(_check(
                f"campaign '{name}': conversion bidding", "pass" if ok else "fail",
                "Optimizes for " + (_event_label(goal) if goal else "an event that is missing")
                + (". The goal must be an active standard event." if not ok else "."),
            ))

    # 5. Recent raw events (last ~15 minutes)
    recent_summary: list[dict] = []
    if check_recent_events and pixels:
        for pixel in pixels:
            pid = str(pixel.get("pixel_id") or "")
            if not pid:
                continue
            recent, failure = _recent_events(config, account, pid)
            if recent is None:
                recent_summary.append({"pixel_id": pid, "available": False, "reason": failure})
                continue
            counts: dict[str, int] = {}
            channels: dict[str, int] = {}
            for e in recent:
                key = str(e.get("custom_event_name") or e.get("event_type"))
                counts[key] = counts.get(key, 0) + 1
                channel = str(e.get("api_channel") or "unknown")
                channels[channel] = channels.get(channel, 0) + 1
            recent_summary.append({
                "pixel_id": pid,
                "available": True,
                "events_last_15_min": counts,
                "channels": channels,
            })
        seen_any = any(s.get("events_last_15_min") for s in recent_summary)
        if any(s.get("available") for s in recent_summary):
            if seen_any:
                counts_all: dict[str, int] = {}
                for s in recent_summary:
                    for key, value in (s.get("events_last_15_min") or {}).items():
                        counts_all[key] = counts_all.get(key, 0) + value
                checks.append(_check(
                    "recent browser events", "pass",
                    "The pixel delivered events in the last 15 minutes: "
                    + ", ".join(f"{k} {v}" for k, v in sorted(counts_all.items())) + ". "
                    "This stream samples Pixel SDK (browser) events only; Conversions API "
                    "(server) events never appear in it.",
                ))
            else:
                checks.append(_check(
                    "recent browser events", "info",
                    "No browser event in the last 15 minutes. That proves nothing on a quiet "
                    "site; load a tracked page or submit a test lead and run this again. "
                    "Server-side events never appear in this stream.",
                ))

    # Configuration (an event setting) is not delivery. Only page views seen
    # arriving count as evidence that the site sends them, and even that is
    # evidence about the account, not about one campaign's landing pages.
    page_views_seen = [
        where for where, found in (
            ("in the last 15 minutes",
             any(set(s.get("events_last_15_min") or {}) & _SOFT_EVENTS for s in recent_summary)),
            ("attributed to other campaigns",
             any(set(a.get("events") or {}) & _SOFT_EVENTS for a in activity.values())),
        ) if found
    ]
    page_views_defined = any(event_key(s) in _SOFT_EVENTS for s in live_settings)
    measured = sorted({event_key(s) for s in live_settings}) or ["no event"]
    calls_note = (
        " Phone calls count only if call tracking sends them through the Conversions API "
        "(action_source phone_call)."
    )

    # 6. Do clicks lead to attributed events, and does the goal event fire?
    # These are observations, not proof: zero events can also mean nobody
    # converted, so they warn and never make the verdict "broken".
    for campaign in campaigns:
        cid = str(campaign.get("id"))
        name = campaign.get("name")
        entry = activity.get(cid) or {"clicks": 0, "events": {}, "goal_conversions": 0}
        clicks, events = entry["clicks"], entry["events"]
        if not clicks:
            continue
        if not any(events.values()):
            if page_views_seen:
                detail = (
                    f"{clicks} clicks and no attributed event of any kind, although the pixel "
                    f"does deliver page views ({' and '.join(page_views_seen)}). Check that the "
                    "pixel loads on this campaign's landing pages, that consent does not block "
                    "it, and that no redirect drops the oppref click parameter."
                )
            elif page_views_defined:
                detail = (
                    f"{clicks} clicks and no attributed event. page_viewed is defined but no page "
                    "view was seen arriving, so this cannot tell missing tracking from visitors "
                    "who did nothing measurable. Load a landing page and run this again."
                )
            else:
                detail = (
                    f"{clicks} clicks and no attributed event. The site reports only "
                    f"{', '.join(measured)} (no page_viewed), so this cannot tell 'nobody from "
                    "these clicks converted' from 'the pixel misses them'. Sending page_viewed "
                    "on every page (oaiq('measure', 'page_viewed')) makes the check conclusive."
                )
            if clicks < min_clicks:
                detail += f" With fewer than {min_clicks} clicks this can easily be chance."
            checks.append(_check(f"campaign '{name}': events from ad clicks", "warn", detail + calls_note))
            continue
        attached = [settings_by_id[i] for i in (str(x) for x in campaign.get("conversion_event_setting_ids") or []) if i in settings_by_id]
        goal_names = {event_key(s) for s in attached}
        firing_goals = {g for g in goal_names if events.get(g)}
        seen = ", ".join(f"{k} {v}" for k, v in sorted(events.items(), key=lambda kv: -kv[1]))
        if goal_names and not firing_goals:
            checks.append(_check(
                f"campaign '{name}': goal event fires", "warn",
                f"{clicks} clicks; attributed events: {seen}; none of the goal events "
                f"({', '.join(sorted(goal_names))}). Either nobody from these clicks converted, "
                "or the goal event does not fire on the form or thank-you page or uses another "
                "name: submit a test lead and check the recent browser events." + calls_note,
            ))
        elif goal_names:
            checks.append(_check(
                f"campaign '{name}': goal event fires", "pass",
                f"{clicks} clicks; attributed events: {seen}.",
            ))
        not_goals = sorted(set(events) - goal_names - _SOFT_EVENTS)
        if not_goals:
            insights.append(
                f"Campaign '{name}' has attributed {', '.join(not_goals)} that it does not "
                "count as a conversion; attach that event if it is a real outcome."
            )

    # 7. Click tagging for analytics
    click_params = [ad_summary(a, g, c)["click_parameters"] for a, g, c in structure["ads"]]
    untagged = [p for p in click_params if "utm_source" not in p]
    if click_params:
        if untagged:
            checks.append(_check(
                "analytics tagging", "warn",
                f"{len(untagged)} of {len(click_params)} ads send clicks without "
                "utm_source; Google Analytics mixes them with unpaid chatgpt.com referrals.",
            ))
        else:
            sources = sorted({
                f"{p.get('utm_source')}/{p.get('utm_medium', '(no medium)')}" for p in click_params
            })
            checks.append(_check("analytics tagging", "pass", "Clicks carry " + ", ".join(sources) + "."))

    order = {"fail": 0, "warn": 1, "info": 2, "pass": 3}
    checks.sort(key=lambda c: order.get(c["status"], 4))
    verdict = (
        "broken" if any(c["status"] == "fail" for c in checks)
        else "needs attention" if any(c["status"] == "warn" for c in checks)
        else "ok"
    )
    insights.append(
        "If the site has a cookie or privacy banner, visitors who decline are not "
        "measured when the pixel respects it, so OpenAI conversions run below "
        "real leads; that is expected, not a fault."
    )

    return {
        "ad_account_id": meta["ad_account_id"],
        "date_range": {k: window[k] for k in ("start", "end", "days", "time_zone")},
        "verdict": verdict,
        "checks": checks,
        "pixels": [
            {"source_id": p.get("id"), "name": p.get("name"), "pixel_id": p.get("pixel_id"),
             "type": p.get("client_type")}
            for p in pixels or []
        ],
        "event_settings": [
            {
                "event_setting_id": s.get("id"),
                "name": s.get("name"),
                "event": event_key(s),
                "standard": s.get("event_type") != "custom",
                "archived": s.get("archived"),
                "sources": [src.get("name") or src.get("id") for src in s.get("sources") or []],
                "campaigns": [c.get("name") or c.get("id") for c in s.get("campaigns") or []],
            }
            for s in settings
        ],
        "campaign_activity": [
            {
                "campaign_id": c.get("id"),
                "name": c.get("name"),
                "status": c.get("status"),
                **(activity.get(str(c.get("id"))) or {}),
            }
            for c in campaigns
        ],
        "recent_events": recent_summary or None,
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Change history (audit logs)
# ---------------------------------------------------------------------------


def _changes(raw: Any) -> dict:
    """Only the fields that changed: audit entries list every field, mostly null."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "obj_type" or value is None:
            continue
        if isinstance(value, dict) and ("before" in value or "after" in value):
            before, after = value.get("before"), value.get("after")
            if before != after:
                out[key] = {"before": before, "after": after}
        elif value not in ({}, [], ""):
            out[key] = value
    return out


def get_chatgpt_change_history(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    ad_id: str = "",
    limit: int = 200,
) -> dict:
    """Who changed what and when, newest first (Ads Manager, API and OpenAI system changes)."""
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    tz = meta["time_zone"]
    today = _today(tz)
    end = _parse_date(date_range_end, default=today)
    start = _parse_date(date_range_start, default=end - timedelta(days=29))
    if start > end:
        raise ValueError("date_range_start must not be after date_range_end.")
    limit = max(1, min(int(limit or 200), 2000))

    params: dict[str, Any] = {
        "start_time": _midnight(start, tz),
        "end_time": _midnight(end + timedelta(days=1), tz) - 1,
        "order": "desc",
        "limit": min(limit, 100),
    }
    for key, value in (("campaign_id", campaign_id), ("ad_group_id", ad_group_id), ("ad_id", ad_id)):
        if value:
            params[key] = value.strip()

    entries: list[dict] = []
    truncated = False
    while True:
        payload = api_get(config, "audit_logs", params, ad_account_id=account)
        page = [e for e in payload.get("data") or [] if isinstance(e, dict)]
        entries.extend(page)
        if len(entries) >= limit:
            truncated = bool(payload.get("has_more")) or len(entries) > limit
            entries = entries[:limit]
            break
        if not payload.get("has_more") or not page or not payload.get("last_id"):
            break
        params["after"] = payload["last_id"]

    changes = [
        {
            "time": _local_time(e.get("ts"), tz),
            "type": e.get("audit_log_type"),
            "object_type": e.get("obj_type"),
            "object_id": e.get("obj_id"),
            "object_name": e.get("item_name"),
            "actor_type": e.get("actor_type"),
            "actor_id": e.get("actor_id"),
            "campaign_id": e.get("campaign_id"),
            "ad_group_id": e.get("ad_group_id"),
            "changes": _changes(e.get("changes")),
        }
        for e in entries
    ]
    by_type: dict[str, int] = {}
    by_actor: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for c in changes:
        by_type[str(c["type"])] = by_type.get(str(c["type"]), 0) + 1
        actor = f"{c['actor_type']}:{c['actor_id']}" if c["actor_id"] else str(c["actor_type"])
        by_actor[actor] = by_actor.get(actor, 0) + 1
        if c["time"]:
            by_day[c["time"][:10]] = by_day.get(c["time"][:10], 0) + 1

    insights: list[str] = []
    sensitive = ("budget", "bidding_config", "status", "targeting", "conversion_event_setting_ids")
    # Creations "set" every field for the first time; only edits move performance.
    flagged = [
        c for c in changes
        if not str(c["type"] or "").startswith("create_")
        and any(k.startswith(sensitive) for k in c["changes"])
    ]
    if flagged:
        insights.append(
            f"{len(flagged)} edit(s) touched budget, bids, status, targeting or "
            "conversion events: "
            + "; ".join(f"{c['time']} {c['type']} '{c['object_name']}'" for c in flagged[:5])
            + "."
        )
    if not changes:
        insights.append("No recorded changes in this window.")

    return {
        "ad_account_id": meta["ad_account_id"],
        "date_range": {"start": start.isoformat(), "end": end.isoformat(), "time_zone": tz},
        "total": len(changes),
        "truncated": truncated,
        "by_type": by_type,
        "by_actor": by_actor,
        "by_day": dict(sorted(by_day.items())),
        "changes": changes,
        "insights": insights,
        "note": "Fields ending in _micros are millionths of the account currency.",
    }


# ---------------------------------------------------------------------------
# ChatGPT Ads ↔ Google Analytics
# ---------------------------------------------------------------------------


def _ga4_rows(result: dict) -> list[dict]:
    if "error" in result:
        raise RuntimeError(f"Google Analytics report failed: {result.get('error')} {result.get('details') or ''}".strip())
    return result.get("rows") or []


def chatgpt_attribution_check(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    property_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """ChatGPT Ads clicks and conversions against GA4 sessions and key events from them."""
    import re

    from adloop.crossref import GA4_KEY_EVENTS_METRIC
    from adloop.ga4.reports import run_ga4_report

    if not property_id:
        raise ValueError("property_id is required (or set ga4.property_id in the config).")
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    window = report_window(date_range_start, date_range_end, meta["time_zone"])
    currency = meta["currency"]

    delivery, _ = _insights(config, account, "ad_account/insights", {
        "aggregation_level": "ad_account",
        "time_granularity": "none",
        "time_ranges[]": [_unix_range(window)],
        "fields[]": ["impressions", "clicks", "spend", "conversions"],
        "limit": 10,
    })
    clicks = sum(int(_num(_metric(r, "clicks", ("ad_account",))) or 0) for r in delivery)
    spend = round(sum(_num(_metric(r, "spend", ("ad_account",))) or 0.0 for r in delivery), 2)
    conversions = sum(int(_num(r.get("conversions")) or 0) for r in delivery)

    structure = load_structure(config, account, with_ads=True, include_archived=True)
    # Ads that actually got clicks in the window: an untagged one among them
    # sends paid visits that look exactly like unpaid chatgpt.com referrals.
    ad_rows, ad_rows_truncated = _insights(config, account, "ad_account/insights", {
        "aggregation_level": "ad",
        "time_granularity": "none",
        "time_ranges[]": [_unix_range(window)],
        "fields[]": ["ad_id", "clicks"],
        "limit": 2000,
    })
    clicked_ads = {
        str(r.get("ad_id")) for r in ad_rows if (_num(_metric(r, "clicks", ("ad",))) or 0) > 0
    }
    pairs: set[tuple[str, str]] = set()
    sources: set[str] = set()
    untagged_clicked = 0
    for a, g, c in structure["ads"]:
        params = ad_summary(a, g, c)["click_parameters"]
        source = (params.get("utm_source") or "").strip()
        if source and "{" not in source:
            sources.add(source.lower())
            pairs.add((source.lower(), (params.get("utm_medium") or "").strip().lower()))
        elif str(a.get("id")) in clicked_ads:
            untagged_clicked += 1
    # Paid only when the tag cannot also describe unpaid traffic: a utm_source
    # with a paid medium. chatgpt.com with no medium matches every referral.
    distinct = {(s, m) for s, m in pairs if m not in _UNPAID_MEDIUMS}
    weak_sources = {s for s, m in pairs if m in _UNPAID_MEDIUMS}

    pattern = "|".join(["chatgpt", "openai"] + [re.escape(s) for s in sorted(sources)])
    source_filter = [{"field": "sessionSource", "op": "PARTIAL_REGEXP", "value": pattern}]
    sessions_rows = _ga4_rows(run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["sessionSource", "sessionMedium"],
        metrics=["sessions", "engagedSessions", GA4_KEY_EVENTS_METRIC],
        date_range_start=window["start"],
        date_range_end=window["end"],
        dimension_filter=source_filter,
        limit=200,
    ))
    event_rows = _ga4_rows(run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["sessionSource", "sessionMedium", "eventName"],
        metrics=[GA4_KEY_EVENTS_METRIC],
        date_range_start=window["start"],
        date_range_end=window["end"],
        dimension_filter=source_filter,
        metric_filter=[{"field": GA4_KEY_EVENTS_METRIC, "op": ">", "value": 0}],
        limit=500,
    ))

    # Without every clicked ad in view, an untagged one may be among those
    # not read: then no session without the paid tag can be called unpaid.
    clicks_incomplete = ad_rows_truncated

    def classify(row: dict) -> str:
        source = str(row.get("sessionSource") or "").lower()
        medium = str(row.get("sessionMedium") or "").lower()
        if (source, medium) in distinct:
            return "paid"
        if source in weak_sources or untagged_clicked or clicks_incomplete:
            return "ambiguous"
        return "unpaid"

    def bucket(rows: list[dict], kind: str) -> dict:
        picked = [r for r in rows if classify(r) == kind]
        return {
            "sessions": int(sum(_num(r.get("sessions")) or 0 for r in picked)),
            "engaged_sessions": int(sum(_num(r.get("engagedSessions")) or 0 for r in picked)),
            "key_events": round(sum(_num(r.get(GA4_KEY_EVENTS_METRIC)) or 0 for r in picked), 1),
            "sources": sorted({f"{r.get('sessionSource')} / {r.get('sessionMedium')}" for r in picked}),
        }

    paid = bucket(sessions_rows, "paid")
    unpaid = bucket(sessions_rows, "unpaid")
    ambiguous = bucket(sessions_rows, "ambiguous")
    paid_events: dict[str, float] = {}
    for r in event_rows:
        if classify(r) == "paid":
            name = str(r.get("eventName"))
            paid_events[name] = paid_events.get(name, 0) + (_num(r.get(GA4_KEY_EVENTS_METRIC)) or 0)
    paid["key_events_by_event"] = {k: round(v, 1) for k, v in sorted(paid_events.items(), key=lambda kv: -kv[1])}

    insights: list[str] = list(window["notes"])
    if not distinct:
        insights.append(
            "No ad carries distinctive paid tagging (a utm_source with a paid utm_medium such "
            "as cpc), so Google Analytics cannot separate paid ChatGPT clicks from unpaid "
            "chatgpt.com referrals and no session is counted as paid. Tag every ad, e.g. "
            "utm_source=chatgpt&utm_medium=cpc."
        )
    elif clicks and not paid["sessions"]:
        insights.append(
            f"{clicks} ChatGPT Ads clicks but no GA4 session with "
            + ", ".join(f"{s}/{m}" for s, m in sorted(distinct))
            + ". Check that the landing page keeps the utm parameters (redirects can drop "
            "them) and that GA4 loads for these visitors."
        )
    elif clicks and paid["sessions"] and not untagged_clicked:
        ratio = clicks / paid["sessions"]
        if ratio > 2:
            insights.append(
                f"{clicks} clicks for {paid['sessions']} GA4 sessions ({ratio:.1f}:1). "
                "A consent banner, ad blockers and quick back-outs explain some gap; "
                "above 3:1 also check for redirects that drop the utm parameters."
            )
    if clicks_incomplete:
        insights.append(
            "The per-ad click report stopped at its page limit, so it is unknown whether every clicked "
            "ad was tagged; ChatGPT sessions without the paid tag are counted as ambiguous."
        )
    if untagged_clicked:
        insights.append(
            f"{untagged_clicked} ad(s) without utm_source got clicks in this window; their visits "
            "look like unpaid ChatGPT referrals, so ChatGPT sessions without the paid tag are "
            "counted as ambiguous."
        )
    if paid["key_events"] and not conversions:
        settings, _ = _event_settings(config, account)
        by_id = {str(s.get("id")): s for s in settings}
        goals = sorted({
            event_key(by_id[str(i)])
            for c in structure["campaigns"]
            for i in c.get("conversion_event_setting_ids") or []
            if str(i) in by_id
        })
        listed = ", ".join(f"{k} {v:g}" for k, v in paid["key_events_by_event"].items())
        insights.append(
            f"GA4 recorded key events from paid ChatGPT sessions ({listed}) while ChatGPT "
            "Ads reports 0 conversions"
            + (f" and counts only {', '.join(goals)}" if goals else " and counts no event")
            + ". If one of these is the same action as a goal (a form lead, say), the OpenAI "
            "setup may be missing it: get_chatgpt_conversion_setup shows where. Other actions, "
            "such as call clicks, never reach OpenAI unless sent through the Conversions API."
        )
    elif conversions and paid["key_events"] and conversions > 2 * paid["key_events"]:
        insights.append(
            f"ChatGPT Ads reports {conversions} conversions against {paid['key_events']:g} GA4 "
            "key events. OpenAI counts view-through and a 30-day click window; GA4 credits "
            "the last session's source."
        )
    if unpaid["sessions"]:
        insights.append(
            f"{unpaid['sessions']} sessions arrived from ChatGPT without paid tagging while every "
            f"clicked ad was tagged ({unpaid['key_events']:g} key events): people following links "
            "in ChatGPT answers. That traffic exists whether or not ads run."
        )
    if ambiguous["sessions"]:
        insights.append(
            f"{ambiguous['sessions']} ChatGPT sessions ({ambiguous['key_events']:g} key events) "
            "could be paid or unpaid; they are left out of both."
        )

    return {
        "ad_account_id": meta["ad_account_id"],
        "property_id": property_id,
        "date_range": {k: window[k] for k in ("start", "end", "days", "time_zone")},
        "chatgpt_ads": {"clicks": clicks, "spend": spend, "conversions": conversions, "currency": currency},
        "paid_tagging": sorted(f"{s} / {m or '(no medium)'}" for s, m in pairs),
        "ga4_paid_chatgpt": paid,
        "ga4_unpaid_chatgpt": unpaid,
        "ga4_ambiguous_chatgpt": ambiguous,
        "insights": insights,
        "note": (
            "GA4 dates are the property's time zone, ChatGPT Ads dates the ad account's. "
            "GA4 only records visitors its consent setup lets it record."
        ),
    }


# ---------------------------------------------------------------------------
# Location lookup
# ---------------------------------------------------------------------------


def search_chatgpt_locations(config: AdLoopConfig, *, ad_account_id: str = "", query: str = "", limit: int = 20) -> dict:
    """Location ids for campaign targeting (cities, regions, markets, countries)."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required, e.g. 'Chicago' or 'Illinois'.")
    account = (ad_account_id or "").strip()
    payload = api_get(
        config, "geo_lookup/search", {"q": query, "limit": max(1, min(int(limit or 20), 100))},
        ad_account_id=account,
    )
    items = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("results") or []
    rows = [
        {
            "location_id": str(i.get("id")),
            "name": i.get("name"),
            "canonical_name": i.get("canonical_name"),
            "type": i.get("type"),
            "country_code": i.get("country_code"),
            "region_code": i.get("region_code"),
            "parent_id": i.get("parent_id"),
        }
        for i in items if isinstance(i, dict) and i.get("id") is not None
    ]
    return {
        "query": query,
        "locations": rows,
        "total": len(rows),
        "note": (
            "Pass location_id values as location_ids or excluded_location_ids to "
            "update_chatgpt_campaign or draft_chatgpt_campaign. Countries can also go in as ISO "
            "codes (countries=['US'])."
        ),
    }
