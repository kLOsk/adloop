"""ChatGPT Ads write tools — draft → preview → (dry run) → apply.

Same gate as Google Ads and Reddit writes: a draft validates the
request, runs the safety guards, stores a :class:`ChangePlan` (platform
``chatgpt``, operations prefixed ``chatgpt_``) and returns its preview;
``confirm_and_apply`` hands the plan to :func:`preflight` (dry run) or
:func:`apply_plan`.

The Advertiser API has no validate-only mode for single resources, so the
dry run re-reads the target, confirms that every field the plan changes
still holds the value the preview showed, and re-runs the budget cap, the
bid-increase cap and the landing-page check. Nothing is sent. The real
apply runs the same preflight first, so a change made in Ads Manager in
between is refused instead of overwritten.

API rules this module follows (developers.openai.com/ads):

- Updates are ``POST /<collection>/<id>`` with only the fields that change.
  ``budget`` and ``bidding_config`` are sent whole; ``context_hints`` and
  geo/audience targeting replace what was there, so the full intended
  value is built from the current one.
- Status changes use ``/pause``, ``/activate`` and ``/archive``; archive
  cannot be undone.
- New campaigns, ad groups and ads are created **paused**, each with an
  ``Idempotency-Key`` derived from the plan, so a retried apply cannot
  create a duplicate.
- Money is micros on the wire, account currency in every preview.
"""

from __future__ import annotations

import copy
import json
import mimetypes
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adloop.chatgpt_ads.client import (
    ChatGPTAdsApiError,
    api_get_all,
    api_post,
    api_upload,
    from_micros,
)
from adloop.chatgpt_ads.read import (
    _event_settings,
    _locations,
    _midnight,
    _zone,
    account_meta,
    event_key,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

PLATFORM = "chatgpt"
OPERATION_PREFIX = "chatgpt_"
ENTITY_TYPES = ("campaign", "ad_group", "ad")
_COLLECTIONS = {"campaign": "campaigns", "ad_group": "ad_groups", "ad": "ads"}
_BIDDING_TYPES = ("impressions", "clicks", "conversions")
_STRATEGIES = {
    "impressions": ("fixed_bid",),
    "clicks": ("fixed_bid", "maximize_clicks"),
    "conversions": ("fixed_bid", "maximize_conversions"),
}
_BILLING = {"impressions": "impression", "clicks": "click", "conversions": "click"}
PLATFORMS = ("android_app", "android_web", "desktop_web", "ios_app", "ios_web", "web")
_CLEARABLE = ("start_date", "end_date", "platforms", "excluded_locations", "query_string_template")
# Distinctive paid tagging, so chatgpt_attribution_check can tell ad clicks
# from links in ChatGPT answers.
DEFAULT_QUERY_TEMPLATE = (
    "utm_source=chatgpt&utm_medium=cpc&utm_campaign={campaign_id}&utm_content={ad_id}"
)
_TITLE_LIMITS = (3, 50)
_BODY_LIMIT = 100
_NAME_LIMITS = (3, 1000)
_MAX_CONTEXT_HINTS = 2000
_MAX_LOCATIONS = 2500
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
_MIN_IMAGE_SIDE = 640


class _Refusal(Exception):
    """A draft that cannot proceed; carries the user-facing details."""

    def __init__(self, details: list[str]):
        super().__init__("; ".join(details))
        self.details = details


def _validation_error(details: list[str]) -> dict:
    return {"error": "Validation failed", "details": details}


def _guard(operation: str, config: AdLoopConfig) -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def _store(plan_kwargs: dict, *, warnings: list[str] | None = None) -> dict:
    """Store the plan; ``customer_id`` is always the resolved ad account id.

    The account is pinned: the preflight re-reads which account the key in
    use belongs to and refuses when it is not this one (a key or default
    changed after the preview), so a plan can never land in another account.
    """
    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(platform=PLATFORM, **plan_kwargs)
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _micros(amount: float) -> int:
    return int(round(float(amount) * 1_000_000))


def _money(value: Any) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _check_name(name: str, errors: list[str], field: str = "name") -> None:
    text = (name or "").strip()
    if not (_NAME_LIMITS[0] <= len(text) <= _NAME_LIMITS[1]):
        errors.append(f"{field} must be {_NAME_LIMITS[0]}–{_NAME_LIMITS[1]} characters.")


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in values if str(v).strip()]


def _parse_day(value: str, field: str, errors: list[str]) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"{field} must be YYYY-MM-DD, got '{value}'.")
        return None


def _day_of(ts: Any, time_zone: str) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), _zone(time_zone)).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _check_urls(urls: list[str], errors: list[str], warnings: list[str]) -> None:
    from adloop.ads.write import _validate_urls

    bad, inconclusive = _validate_urls(urls)
    for url, problem in bad.items():
        if problem:
            errors.append(f"'{url}' is not reachable: {problem}")
    warnings.extend(inconclusive.values())


# ---------------------------------------------------------------------------
# Locating entities (ad groups and ads do not carry their parent's id)
# ---------------------------------------------------------------------------


def find_entity(config: AdLoopConfig, account: str, entity_type: str, entity_id: str) -> dict | None:
    """The entity with its campaign (and ad group), archived ones included."""
    entity_id = str(entity_id)
    campaigns = api_get_all(config, "campaigns", {"include[]": ["serving_issues"]}, ad_account_id=account)
    for campaign in campaigns:
        if entity_type == "campaign":
            if str(campaign.get("id")) == entity_id:
                return {"entity": campaign, "campaign": campaign}
            continue
        groups = api_get_all(
            config, "ad_groups", {"campaign_id": campaign["id"], "include[]": ["serving_issues"]},
            ad_account_id=account,
        )
        for group in groups:
            if entity_type == "ad_group":
                if str(group.get("id")) == entity_id:
                    return {"entity": group, "campaign": campaign, "ad_group": group}
                continue
            for ad in api_get_all(
                config, "ads", {"ad_group_id": group["id"], "include[]": ["serving_issues"]},
                ad_account_id=account,
            ):
                if str(ad.get("id")) == entity_id:
                    return {"entity": ad, "campaign": campaign, "ad_group": group}
    return None


def _snapshot(entity: dict, keys: list[str]) -> dict:
    """The fields a plan changes, as the API returned them (for change detection)."""
    return {k: copy.deepcopy(entity.get(k)) for k in keys}


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Status: pause, enable, archive
# ---------------------------------------------------------------------------


_STATUS_ACTIONS = {"pause": "paused", "activate": "active", "archive": "archived"}


def _draft_status(
    config: AdLoopConfig, *, action: str, ad_account_id: str, entity_type: str, entity_id: str,
) -> dict:
    operation = "chatgpt_archive_entity" if action == "archive" else "chatgpt_set_status"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    entity_type = (entity_type or "").strip().lower()
    entity_id = (entity_id or "").strip()
    errors = []
    if entity_type not in ENTITY_TYPES:
        errors.append(f"entity_type must be one of {list(ENTITY_TYPES)}, got '{entity_type}'")
    if not entity_id:
        errors.append("entity_id is required")
    if errors:
        return _validation_error(errors)
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    found = find_entity(config, account, entity_type, entity_id)
    if not found:
        return {"error": f"ChatGPT Ads {entity_type.replace('_', ' ')} '{entity_id}' was not found in {meta['ad_account_id']}."}
    entity = found["entity"]
    current = entity.get("status")
    target = _STATUS_ACTIONS[action]
    warnings: list[str] = []
    if current == "archived":
        return {"error": f"'{entity.get('name')}' is archived; archived entities cannot change status."}
    if current == target:
        warnings.append(f"'{entity.get('name')}' is already {target}; applying changes nothing.")
    if target == "active":
        for level in ("campaign", "ad_group"):
            parent = found.get(level)
            if parent is not None and parent is not entity and parent.get("status") != "active":
                warnings.append(
                    f"Its {level.replace('_', ' ')} '{parent.get('name')}' is {parent.get('status')}, "
                    "so it still will not serve."
                )
        review = entity.get("review_status") or (entity.get("review") or {}).get("status")
        if entity_type == "ad" and review in ("rejected", "in_review"):
            warnings.append(f"The ad's review status is {review}: it serves only once approved.")
        codes = {str(i.get("code")) for i in entity.get("serving_issues") or [] if isinstance(i, dict)}
        if codes - {"campaign_not_active", "ad_group_not_active"}:
            warnings.append("Other serving issues remain: " + ", ".join(sorted(codes)) + ".")
    if target == "paused" and entity_type == "campaign":
        warnings.append("Pausing the campaign stops every ad in it.")
    if target == "archived":
        warnings.append("Archiving cannot be undone: the entity can never serve again. Pausing keeps it reusable.")
    return _store(
        {
            "operation": operation,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "customer_id": meta["ad_account_id"],
            "requires_double_confirm": action == "archive",
            "changes": {
                "ad_account_id": account,
                "entity_name": entity.get("name"),
                "campaign_name": found["campaign"].get("name"),
                "ad_group_name": (found.get("ad_group") or {}).get("name"),
                "current_status": current,
                "target_status": target,
                "action": action,
            },
        },
        warnings=warnings,
    )


def pause_chatgpt_entity(config, *, ad_account_id="", entity_type="", entity_id="") -> dict:
    return _draft_status(config, action="pause", ad_account_id=ad_account_id,
                         entity_type=entity_type, entity_id=entity_id)


def enable_chatgpt_entity(config, *, ad_account_id="", entity_type="", entity_id="") -> dict:
    return _draft_status(config, action="activate", ad_account_id=ad_account_id,
                         entity_type=entity_type, entity_id=entity_id)


def archive_chatgpt_entity(config, *, ad_account_id="", entity_type="", entity_id="") -> dict:
    return _draft_status(config, action="archive", ad_account_id=ad_account_id,
                         entity_type=entity_type, entity_id=entity_id)


# ---------------------------------------------------------------------------
# Targeting helpers
# ---------------------------------------------------------------------------


def _geo_block(block: Any) -> dict | None:
    if not isinstance(block, dict):
        return None
    out: dict[str, Any] = {}
    include = [{"id": str(e.get("id"))} for e in block.get("include") or [] if isinstance(e, dict) and e.get("id")]
    if include:
        out["include"] = include
    if block.get("countries"):
        out["countries"] = [str(c).upper() for c in block["countries"]]
    return out or None


def writable_targeting(campaign: dict) -> dict:
    """The campaign's targeting in the shape an update sends back."""
    targeting = campaign.get("targeting") or {}
    out: dict[str, Any] = {}
    for key in ("locations", "excluded_locations"):
        block = _geo_block(targeting.get(key))
        if block:
            out[key] = block
    for key in ("custom_audiences", "excluded_custom_audiences"):
        ids = (targeting.get(key) or {}).get("ids") or []
        if ids:
            out[key] = {"ids": [str(i) for i in ids]}
    platforms = (targeting.get("platforms") or {}).get("included") or []
    if platforms:
        out["platforms"] = {"included": list(platforms)}
    return out


def _geo_from_args(location_ids: list[str], countries: list[str], errors: list[str], field: str) -> dict | None:
    if location_ids and countries:
        errors.append(f"Give {field} either as location ids or as countries, not both.")
        return None
    if countries:
        bad = [c for c in countries if len(c) != 2 or not c.isalpha()]
        if bad:
            errors.append(f"countries are ISO 3166-1 alpha-2 codes such as US; got {bad}.")
        return {"countries": [c.upper() for c in countries]}
    if location_ids:
        return {"include": [{"id": i} for i in location_ids]}
    return None


def _platforms(values: list[str], errors: list[str]) -> list[str]:
    bad = [p for p in values if p not in PLATFORMS]
    if bad:
        errors.append(f"platforms must be among {list(PLATFORMS)}; got {bad}.")
    return values


def _geo_label(block: dict | None) -> list[str]:
    if not block:
        return []
    return [str(c) for c in block.get("countries") or []] + [f"id {e['id']}" for e in block.get("include") or []]


def _daily_equivalent(budget: dict, start: Any, end: Any) -> float | None:
    """The daily amount a budget allows: the daily budget, or a lifetime
    budget spread over its flight (from the start, or from now when it has no
    start). None when a lifetime budget has no end, so it cannot be judged."""
    import time as _time

    if budget.get("daily_spend_limit_micros") is not None:
        return from_micros(budget["daily_spend_limit_micros"])
    lifetime = from_micros(budget.get("lifetime_spend_limit_micros"))
    if lifetime is None or not end:
        return None
    begin = int(start) if start else int(_time.time())
    days = max((int(end) - begin) / 86400, 1)
    return round(lifetime / days, 2)


def _check_flight_budget(budget: dict, start: Any, end: Any, config: AdLoopConfig) -> float | None:
    """Cap check for a budget over its (proposed) flight; refuses what cannot be checked."""
    from adloop.safety.guards import SafetyViolation

    daily = _daily_equivalent(budget, start, end)
    if daily is None and budget.get("lifetime_spend_limit_micros") is not None:
        raise SafetyViolation(
            "A lifetime budget without an end date cannot be checked against max_daily_budget; "
            "set an end_date."
        )
    _budget_cap(daily, config)
    return daily


def _budget_cap(daily: float | None, config: AdLoopConfig) -> None:
    from adloop.safety.guards import check_budget_cap

    if daily is not None:
        check_budget_cap(daily, config.safety)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def update_chatgpt_campaign(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    name: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    start_date: str = "",
    end_date: str = "",
    location_ids: list[str] | None = None,
    countries: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    excluded_countries: list[str] | None = None,
    platforms: list[str] | None = None,
    query_string_template: str = "",
    conversion_event_setting_ids: list[str] | None = None,
    clear_fields: list[str] | None = None,
) -> dict:
    """Draft campaign changes: name, budget, dates, locations, platforms, tracking, conversion events."""
    operation = "chatgpt_update_campaign"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation, requires_double_confirmation

    errors: list[str] = []
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    clear = _clean_list(clear_fields)
    bad_clear = [c for c in clear if c not in _CLEARABLE]
    if bad_clear:
        errors.append(f"clear_fields can hold {list(_CLEARABLE)}; got {bad_clear}.")
    if name:
        _check_name(name, errors)
    if daily_budget is not None and lifetime_budget is not None:
        errors.append("Pass daily_budget or lifetime_budget, not both.")
    for label, amount in (("daily_budget", daily_budget), ("lifetime_budget", lifetime_budget)):
        if amount is not None and float(amount) <= 0:
            errors.append(f"{label} must be a positive amount in the account currency.")
    start = _parse_day(start_date, "start_date", errors)
    end = _parse_day(end_date, "end_date", errors)
    loc_ids, ctry = _clean_list(location_ids), _clean_list(countries)
    ex_ids, ex_ctry = _clean_list(excluded_location_ids), _clean_list(excluded_countries)
    new_locations = _geo_from_args(loc_ids, ctry, errors, "locations") if (location_ids is not None or countries is not None) else None
    new_excluded = _geo_from_args(ex_ids, ex_ctry, errors, "excluded locations") if (excluded_location_ids is not None or excluded_countries is not None) else None
    plat = _platforms(_clean_list(platforms), errors) if platforms is not None else None
    if platforms is not None and not plat:
        errors.append("platforms cannot be empty; put 'platforms' in clear_fields to target every platform.")
    if (location_ids is not None or countries is not None) and not new_locations:
        errors.append("An empty location list would target everywhere; pass at least one location or country.")
    if errors:
        return _validation_error(errors)

    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    tz = meta["time_zone"]
    found = find_entity(config, account, "campaign", campaign_id)
    if not found:
        return {"error": f"ChatGPT Ads campaign '{campaign_id}' was not found in {meta['ad_account_id']}."}
    campaign = found["entity"]
    if campaign.get("status") == "archived":
        return {"error": f"Campaign '{campaign.get('name')}' is archived and cannot be changed."}
    body: dict[str, Any] = {}
    shown: dict[str, Any] = {}
    warnings: list[str] = []
    touched: list[str] = []

    if name and name.strip() != campaign.get("name"):
        body["name"] = name.strip()
        shown["name"] = {"from": campaign.get("name"), "to": name.strip()}
        touched.append("name")

    budget = campaign.get("budget") or {}
    current_daily = from_micros(budget.get("daily_spend_limit_micros"))
    current_lifetime = from_micros(budget.get("lifetime_spend_limit_micros"))
    new_budget: dict | None = None
    if daily_budget is not None and current_daily is not None and _money(daily_budget) == current_daily:
        daily_budget = None  # already this amount
    if lifetime_budget is not None and current_lifetime is not None and _money(lifetime_budget) == current_lifetime:
        lifetime_budget = None
    if daily_budget is not None:
        new_budget = {"daily_spend_limit_micros": _micros(daily_budget)}
        shown["daily_budget"] = {"from": current_daily, "to": _money(daily_budget), "currency": meta["currency"]}
        if current_lifetime is not None and current_daily is None:
            warnings.append("This switches the campaign from a lifetime to a daily budget (allowed one way only).")
    elif lifetime_budget is not None:
        if current_daily is not None:
            errors.append("OpenAI does not allow switching a daily budget to a lifetime budget; create a new campaign instead.")
        new_budget = {"lifetime_spend_limit_micros": _micros(lifetime_budget)}
        shown["lifetime_budget"] = {"from": current_lifetime, "to": _money(lifetime_budget), "currency": meta["currency"]}
    if new_budget:
        body["budget"] = new_budget
        touched.append("budget")

    start_ts = campaign.get("start_time")
    end_ts = campaign.get("end_time")
    if start:
        start_ts = _midnight(start, tz)
        body["start_time"] = start_ts
        shown["start_date"] = {"from": _day_of(campaign.get("start_time"), tz), "to": start.isoformat()}
    if end:
        end_ts = _midnight(end + timedelta(days=1), tz)
        body["end_time"] = end_ts
        shown["end_date"] = {"from": _day_of(campaign.get("end_time") and int(campaign["end_time"]) - 1, tz), "to": end.isoformat()}
    for field, key in (("start_date", "start_time"), ("end_date", "end_time")):
        if field in clear:
            body[key] = None
            shown[field] = {"from": _day_of(campaign.get(key), tz), "to": None}
            if key == "start_time":
                start_ts = None
            else:
                end_ts = None
    if start and end and start > end:
        errors.append("start_date must not be after end_date.")
    if "start_time" in body or "end_time" in body:
        touched += ["start_time", "end_time"]

    targeting_changed = any(v is not None for v in (new_locations, new_excluded, plat)) or (
        {"platforms", "excluded_locations"} & set(clear)
    )
    if targeting_changed:
        current_t = writable_targeting(campaign)
        new_t = copy.deepcopy(current_t)
        if new_locations is not None:
            new_t["locations"] = new_locations
        if new_excluded is not None:
            new_t["excluded_locations"] = new_excluded
        if "excluded_locations" in clear:
            new_t.pop("excluded_locations", None)
        if plat is not None:
            new_t["platforms"] = {"included": plat}
        if "platforms" in clear:
            new_t.pop("platforms", None)
        geo_count = sum(len(new_t.get(k, {}).get("include", [])) for k in ("locations", "excluded_locations"))
        if geo_count > _MAX_LOCATIONS:
            errors.append(f"At most {_MAX_LOCATIONS} location ids across inclusions and exclusions.")
        if not new_t.get("locations"):
            warnings.append("The campaign has no location targeting, so it can serve everywhere OpenAI sells ads.")
        body["targeting"] = new_t or None
        touched.append("targeting")
        current_view = campaign.get("targeting") or {}
        shown["targeting"] = {
            "locations": {"from": _locations(current_view.get("locations")), "to": _geo_label(new_t.get("locations"))},
            "excluded_locations": {"from": _locations(current_view.get("excluded_locations")),
                                   "to": _geo_label(new_t.get("excluded_locations"))},
            "platforms": {"from": (current_t.get("platforms") or {}).get("included") or "all",
                          "to": (new_t.get("platforms") or {}).get("included") or "all"},
            "custom_audiences": "kept as they are",
        }

    if query_string_template or "query_string_template" in clear:
        current_lp = copy.deepcopy(campaign.get("landing_page_configuration") or {})
        new_lp = dict(current_lp)
        new_lp["query_string_template"] = None if "query_string_template" in clear else query_string_template.strip()
        body["landing_page_configuration"] = new_lp
        touched.append("landing_page_configuration")
        shown["query_string_template"] = {"from": current_lp.get("query_string_template"), "to": new_lp["query_string_template"]}
        template = new_lp["query_string_template"] or ""
        if "utm_medium=" not in template or "utm_source=" not in template:
            warnings.append(
                "Without utm_source and a paid utm_medium, Google Analytics cannot tell these clicks "
                "from unpaid ChatGPT referrals (chatgpt_attribution_check counts them as ambiguous)."
            )

    if conversion_event_setting_ids is not None:
        if campaign.get("bidding_type") == "conversions":
            errors.append("The conversion event of a conversion-optimized campaign cannot change.")
        ids = _clean_list(conversion_event_setting_ids)
        settings, _err = _event_settings(config, account)
        live = {str(s.get("id")): s for s in settings if not s.get("archived")}
        unknown = [i for i in ids if i not in live]
        if unknown:
            errors.append(f"Unknown or archived conversion event setting(s): {unknown}.")
        body["conversion_event_setting_ids"] = ids
        touched.append("conversion_event_setting_ids")
        before = [str(i) for i in campaign.get("conversion_event_setting_ids") or []]
        shown["conversion_events"] = {
            "from": before, "to": [f"{live[i].get('name')} ({event_key(live[i])})" for i in ids if i in live],
        }
    if errors:
        return _validation_error(errors)
    if not body:
        return {"error": "Nothing to change: pass at least one field that differs from the campaign."}

    # Budget and flight dates together decide what a lifetime budget spends
    # per day: check whenever either changes.
    daily = _daily_equivalent(new_budget or budget, start_ts, end_ts)
    if new_budget or "start_time" in body or "end_time" in body:
        try:
            daily = _check_flight_budget(new_budget or budget, start_ts, end_ts, config)
        except SafetyViolation as e:
            return {"error": str(e)}
    old_amount = current_daily if current_daily is not None else current_lifetime
    new_amount = from_micros(next(iter(new_budget.values()))) if new_budget else None
    double = bool(new_budget) and requires_double_confirmation(
        operation, current_budget=old_amount, proposed_budget=new_amount,
    )
    if new_budget and old_amount and new_amount and new_amount > old_amount * 2:
        warnings.append(f"This more than doubles the budget ({old_amount} → {new_amount} {meta['currency']}).")
    if "targeting" in body:
        warnings.append("Location and audience targeting is replaced as a whole; the preview shows the full new set.")

    return _store(
        {
            "operation": operation,
            "entity_type": "campaign",
            "entity_id": campaign_id,
            "customer_id": meta["ad_account_id"],
            "requires_double_confirm": double,
            "changes": {
                "ad_account_id": account,
                "entity_name": campaign.get("name"),
                "campaign_status": campaign.get("status"),
                "changes": shown,
                "daily_equivalent": daily,
                "body": body,
                "before": _snapshot(campaign, sorted(set(touched))),
            },
        },
        warnings=warnings,
    )


def draft_chatgpt_campaign(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    name: str = "",
    bidding_type: str = "clicks",
    daily_budget: float = 0.0,
    lifetime_budget: float = 0.0,
    location_ids: list[str] | None = None,
    countries: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    platforms: list[str] | None = None,
    start_date: str = "",
    end_date: str = "",
    query_string_template: str = "",
    conversion_event_setting_ids: list[str] | None = None,
    description: str = "",
) -> dict:
    """Draft a new campaign, created PAUSED."""
    operation = "chatgpt_create_campaign"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation

    errors: list[str] = []
    _check_name(name, errors)
    bidding_type = (bidding_type or "clicks").strip().lower()
    if bidding_type not in _BIDDING_TYPES:
        errors.append(f"bidding_type must be one of {list(_BIDDING_TYPES)}.")
    daily = float(daily_budget or 0)
    lifetime = float(lifetime_budget or 0)
    if bool(daily > 0) == bool(lifetime > 0):
        errors.append("Pass exactly one of daily_budget or lifetime_budget (a positive amount in the account currency).")
    if lifetime and lifetime < 1:
        errors.append("lifetime_budget must be at least 1 in the account currency.")
    locations = _geo_from_args(_clean_list(location_ids), _clean_list(countries), errors, "locations")
    if not locations:
        errors.append("Location targeting is required: pass location_ids (search_chatgpt_locations) or countries.")
    excluded = {"include": [{"id": i} for i in _clean_list(excluded_location_ids)]} if excluded_location_ids else None
    plat = _platforms(_clean_list(platforms), errors) if platforms else []
    start = _parse_day(start_date, "start_date", errors)
    end = _parse_day(end_date, "end_date", errors)
    if start and end and start > end:
        errors.append("start_date must not be after end_date.")
    if lifetime and not end:
        errors.append("A lifetime budget needs an end_date.")
    event_ids = _clean_list(conversion_event_setting_ids)
    if bidding_type == "conversions" and len(event_ids) != 1:
        errors.append("A conversions campaign needs exactly one conversion_event_setting_id (a standard event).")
    if errors:
        return _validation_error(errors)

    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    tz = meta["time_zone"]
    warnings: list[str] = []
    events_shown: list[str] = []
    if event_ids:
        settings, _err = _event_settings(config, account)
        live = {str(s.get("id")): s for s in settings if not s.get("archived")}
        unknown = [i for i in event_ids if i not in live]
        if unknown:
            return _validation_error([f"Unknown or archived conversion event setting(s): {unknown}."])
        if bidding_type == "conversions" and live[event_ids[0]].get("event_type") == "custom":
            return _validation_error(["Conversion bidding needs a standard event; custom events cannot be the goal."])
        events_shown = [f"{live[i].get('name')} ({event_key(live[i])})" for i in event_ids]
    elif bidding_type == "clicks":
        warnings.append("No conversion event is attached, so this campaign will report 0 conversions.")

    template = (query_string_template or "").strip() or DEFAULT_QUERY_TEMPLATE
    if not query_string_template:
        warnings.append(f"Using the default tracking template {DEFAULT_QUERY_TEMPLATE}.")
    body: dict[str, Any] = {
        "name": name.strip(),
        "status": "paused",
        "bidding_type": bidding_type,
        "budget": {"daily_spend_limit_micros": _micros(daily)} if daily else {"lifetime_spend_limit_micros": _micros(lifetime)},
        "targeting": {"locations": locations},
        "landing_page_configuration": {"query_string_template": template},
    }
    if description:
        body["description"] = description.strip()
    if excluded:
        body["targeting"]["excluded_locations"] = excluded
    if plat:
        body["targeting"]["platforms"] = {"included": plat}
    if event_ids:
        body["conversion_event_setting_ids"] = event_ids
    if start:
        body["start_time"] = _midnight(start, tz)
    if end:
        body["end_time"] = _midnight(end + timedelta(days=1), tz)
    try:
        daily_eq = _check_flight_budget(body["budget"], body.get("start_time"), body.get("end_time"), config)
    except SafetyViolation as e:
        return {"error": str(e)}
    if bidding_type == "impressions":
        warnings.append("Impression bidding pays per view; clicks or conversions suit lead generation better.")
    return _store(
        {
            "operation": operation,
            "entity_type": "campaign",
            "entity_id": "",
            "customer_id": meta["ad_account_id"],
            "changes": {
                "ad_account_id": account,
                "entity_name": name.strip(),
                "status_on_create": "paused",
                "bidding_type": bidding_type,
                "daily_budget": daily or None,
                "lifetime_budget": lifetime or None,
                "currency": meta["currency"],
                "locations": _geo_label(locations),
                "excluded_locations": _geo_label(excluded),
                "platforms": plat or "all",
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
                "query_string_template": template,
                "conversion_events": events_shown,
                "daily_equivalent": daily_eq,
                "body": body,
            },
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Ad groups
# ---------------------------------------------------------------------------


def _bidding_config(
    current: dict, campaign: dict, *, strategy: str, max_bid: float | None, errors: list[str],
) -> dict:
    bidding_type = campaign.get("bidding_type") or "impressions"
    new = copy.deepcopy(current) if current else {}
    new["billing_event_type"] = _BILLING.get(bidding_type, "impression")
    if strategy:
        if strategy not in _STRATEGIES.get(bidding_type, ()):
            errors.append(f"A {bidding_type} campaign takes strategy {list(_STRATEGIES.get(bidding_type, ()))}.")
        new["strategy"] = strategy
    chosen = new.get("strategy") or "fixed_bid"
    new["strategy"] = chosen
    if chosen == "fixed_bid":
        if max_bid is not None:
            if float(max_bid) <= 0:
                errors.append("max_bid must be positive.")
            new["max_bid_micros"] = _micros(max_bid)
        if not new.get("max_bid_micros"):
            errors.append("A fixed bid needs max_bid.")
    else:
        if max_bid is not None:
            errors.append(f"{chosen} sets bids automatically; leave max_bid empty.")
        new.pop("max_bid_micros", None)
        new["custom_audience_bid_multipliers"] = []
        if (campaign.get("budget") or {}).get("daily_spend_limit_micros") is None:
            errors.append(f"{chosen} needs a daily campaign budget.")
    return new


def _exact_amount(micros: Any) -> float | None:
    """Micros → currency amount, unrounded: impression bids go below a cent,
    so cap checks must not compare two-decimal amounts."""
    try:
        return int(micros) / 1_000_000 if micros not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _amount_text(micros: Any) -> str:
    """A bid for display: cents when whole, else every significant digit."""
    exact = _exact_amount(micros)
    if exact is None:
        return "None"
    return f"{exact:.2f}" if int(micros) % 10_000 == 0 else f"{exact:.6f}".rstrip("0")


def _bid_label(config_: dict, bidding_type: str | None) -> str:
    strategy = config_.get("strategy") or "fixed_bid"
    amount = _amount_text(config_.get("max_bid_micros"))
    if strategy != "fixed_bid":
        return strategy
    unit = "per impression" if config_.get("billing_event_type") == "impression" else (
        "target CPA" if bidding_type == "conversions" else "per click")
    return f"fixed {amount} {unit}"


def update_chatgpt_ad_group(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_group_id: str = "",
    name: str = "",
    strategy: str = "",
    max_bid: float | None = None,
    context_hints: list[str] | None = None,
    add_context_hints: list[str] | None = None,
    remove_context_hints: list[str] | None = None,
) -> dict:
    """Draft ad group changes: name, bid strategy and max bid, context hints."""
    operation = "chatgpt_update_ad_group"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation, check_bid_increase

    errors: list[str] = []
    ad_group_id = (ad_group_id or "").strip()
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if name:
        _check_name(name, errors)
    if context_hints is not None and (add_context_hints or remove_context_hints):
        errors.append("Pass context_hints (the full new list) or add/remove lists, not both.")
    if errors:
        return _validation_error(errors)
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    found = find_entity(config, account, "ad_group", ad_group_id)
    if not found:
        return {"error": f"ChatGPT Ads ad group '{ad_group_id}' was not found in {meta['ad_account_id']}."}
    group, campaign = found["entity"], found["campaign"]
    if group.get("status") == "archived":
        return {"error": f"Ad group '{group.get('name')}' is archived and cannot be changed."}
    body: dict[str, Any] = {}
    shown: dict[str, Any] = {}
    warnings: list[str] = []
    touched: list[str] = []
    if name and name.strip() != group.get("name"):
        body["name"] = name.strip()
        shown["name"] = {"from": group.get("name"), "to": name.strip()}
        touched.append("name")

    strategy = (strategy or "").strip().lower()
    if strategy or max_bid is not None:
        current = group.get("bidding_config") or {}
        new = _bidding_config(current, campaign, strategy=strategy, max_bid=max_bid, errors=errors)
        body["bidding_config"] = new
        touched.append("bidding_config")
        shown["bid"] = {"from": _bid_label(current, campaign.get("bidding_type")),
                        "to": _bid_label(new, campaign.get("bidding_type")), "currency": meta["currency"]}
        old_bid = _exact_amount(current.get("max_bid_micros"))
        new_bid = _exact_amount(new.get("max_bid_micros"))
        if old_bid and new_bid and new.get("strategy") == "fixed_bid":
            try:
                check_bid_increase(old_bid, new_bid, config.safety)
            except SafetyViolation as e:
                return {"error": str(e)}
        if current.get("custom_audience_bid_multipliers") and new.get("strategy") != "fixed_bid":
            warnings.append("Automatic bidding drops the audience bid multipliers.")
        if new.get("strategy") != current.get("strategy"):
            warnings.append("Changing the bid strategy restarts learning; expect a few unsettled days.")

    hints_now = [str(h) for h in group.get("context_hints") or []]
    if context_hints is not None or add_context_hints or remove_context_hints:
        if context_hints is not None:
            new_hints = _clean_list(context_hints)
        else:
            removing = {h.lower() for h in _clean_list(remove_context_hints)}
            missing = sorted(removing - {h.lower() for h in hints_now})
            if missing:
                warnings.append(f"Not in the ad group, nothing to remove: {missing}.")
            new_hints = [h for h in hints_now if h.lower() not in removing]
            seen = {h.lower() for h in new_hints}
            for h in _clean_list(add_context_hints):
                if h.lower() not in seen:
                    new_hints.append(h)
                    seen.add(h.lower())
        if len(new_hints) > _MAX_CONTEXT_HINTS:
            errors.append(f"At most {_MAX_CONTEXT_HINTS} context hints per ad group.")
        if not new_hints:
            warnings.append("No context hints left: the ad group relies on OpenAI's matching alone.")
        body["context_hints"] = new_hints
        touched.append("context_hints")
        shown["context_hints"] = {
            "added": [h for h in new_hints if h not in hints_now],
            "removed": [h for h in hints_now if h not in new_hints],
            "count": {"from": len(hints_now), "to": len(new_hints)},
        }
    if errors:
        return _validation_error(errors)
    if not body:
        return {"error": "Nothing to change: pass at least one field that differs from the ad group."}
    return _store(
        {
            "operation": operation,
            "entity_type": "ad_group",
            "entity_id": ad_group_id,
            "customer_id": meta["ad_account_id"],
            "changes": {
                "ad_account_id": account,
                "entity_name": group.get("name"),
                "campaign_name": campaign.get("name"),
                "ad_group_status": group.get("status"),
                "changes": shown,
                "body": body,
                "before": _snapshot(group, sorted(set(touched))),
            },
        },
        warnings=warnings,
    )


def draft_chatgpt_ad_group(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    name: str = "",
    context_hints: list[str] | None = None,
    strategy: str = "",
    max_bid: float | None = None,
) -> dict:
    """Draft a new ad group in a campaign, created PAUSED."""
    operation = "chatgpt_create_ad_group"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    _check_name(name, errors)
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    hints = _clean_list(context_hints)
    if len(hints) > _MAX_CONTEXT_HINTS:
        errors.append(f"At most {_MAX_CONTEXT_HINTS} context hints per ad group.")
    if errors:
        return _validation_error(errors)
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    found = find_entity(config, account, "campaign", campaign_id)
    if not found:
        return {"error": f"ChatGPT Ads campaign '{campaign_id}' was not found in {meta['ad_account_id']}."}
    campaign = found["entity"]
    if campaign.get("status") == "archived":
        return {"error": f"Campaign '{campaign.get('name')}' is archived."}
    strategy = (strategy or "").strip().lower()
    if not strategy:
        has_daily = (campaign.get("budget") or {}).get("daily_spend_limit_micros") is not None
        auto = {"clicks": "maximize_clicks", "conversions": "maximize_conversions"}.get(campaign.get("bidding_type") or "")
        strategy = "fixed_bid" if max_bid is not None or not auto or not has_daily else auto
    bidding = _bidding_config({}, campaign, strategy=strategy, max_bid=max_bid, errors=errors)
    if errors:
        return _validation_error(errors)
    warnings: list[str] = []
    if not hints:
        warnings.append("No context hints: OpenAI matches conversations on the ads alone.")
    body = {"campaign_id": campaign_id, "name": name.strip(), "status": "paused",
            "context_hints": hints, "bidding_config": bidding}
    return _store(
        {
            "operation": operation,
            "entity_type": "ad_group",
            "entity_id": "",
            "customer_id": meta["ad_account_id"],
            "changes": {
                "ad_account_id": account,
                "entity_name": name.strip(),
                "campaign_name": campaign.get("name"),
                "campaign_status": campaign.get("status"),
                "status_on_create": "paused",
                "bid": _bid_label(bidding, campaign.get("bidding_type")),
                "currency": meta["currency"],
                "context_hints": hints,
                "body": body,
            },
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------


def _check_copy(title: str | None, body: str | None, errors: list[str]) -> None:
    if title is not None and not (_TITLE_LIMITS[0] <= len(title.strip()) <= _TITLE_LIMITS[1]):
        errors.append(f"title must be {_TITLE_LIMITS[0]}–{_TITLE_LIMITS[1]} characters (got {len(title.strip())}).")
    if body is not None and not (1 <= len(body.strip()) <= _BODY_LIMIT):
        errors.append(f"body must be 1–{_BODY_LIMIT} characters (got {len(body.strip())}).")


_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
)


def _image_kind(data: bytes) -> str | None:
    for signature, kind in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    return None


def _image_problem(image_url: str = "", image_path: str = "") -> str | None:
    """Why an image cannot be used, or None.

    Remote images go through AdLoop's public-only fetcher: the host must
    resolve to a public address, every redirect is checked again, the
    connection is pinned to the checked address and the download is capped,
    so a draft cannot be used to reach the server's private network.
    """
    if image_path:
        from adloop.runtime import deployment_mode

        if deployment_mode() == "server":
            return "image_path reads a local file, which a hosted server cannot; pass image_url."
        path = Path(image_path).expanduser()
        if not path.is_file():
            return f"{path} does not exist."
        with path.open("rb") as handle:
            head = handle.read(16)
        if _image_kind(head) is None:
            return f"{path.name} is not a JPEG, PNG or WebP image."
        return None
    if not image_url.startswith(("http://", "https://")):
        return "image_url must be a public http(s) link."
    from adloop.ads.image_fetch import ImageFetchError, fetch_image_bytes

    try:
        fetched = fetch_image_bytes(image_url)
    except ImageFetchError as exc:
        return f"image_url cannot be used: {exc}"
    data = fetched.data or b""
    if _image_kind(bytes(data[:16])) is None:
        return "image_url does not serve a JPEG, PNG or WebP image."
    return None


def _image_source(image_url: str, image_path: str, file_id: str, errors: list[str]) -> dict | None:
    given = [k for k, v in (("image_url", image_url), ("image_path", image_path), ("file_id", file_id)) if v]
    if len(given) > 1:
        errors.append(f"Pass one image source, not {given}.")
        return None
    if not given:
        return None
    if file_id:
        return {"file_id": file_id.strip()}
    problem = _image_problem(image_url=image_url.strip(), image_path=image_path.strip())
    if problem:
        errors.append(problem)
        return None
    return {"image_url": image_url.strip()} if image_url else {"image_path": str(Path(image_path).expanduser())}


def update_chatgpt_ad(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_id: str = "",
    name: str = "",
    title: str = "",
    body: str = "",
    target_url: str = "",
    image_url: str = "",
    image_path: str = "",
) -> dict:
    """Draft ad changes: internal name, title, body, landing page, image (copy edits go back to review)."""
    operation = "chatgpt_update_ad"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    ad_id = (ad_id or "").strip()
    if not ad_id:
        errors.append("ad_id is required")
    if name:
        _check_name(name, errors)
    _check_copy(title or None, body or None, errors)
    if target_url:
        if not target_url.startswith(("http://", "https://")):
            errors.append("target_url must start with http:// or https://")
        else:
            _check_urls([target_url.strip()], errors, warnings)
    image = _image_source(image_url, image_path, "", errors)
    if errors:
        return _validation_error(errors)
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    found = find_entity(config, account, "ad", ad_id)
    if not found:
        return {"error": f"ChatGPT Ads ad '{ad_id}' was not found in {meta['ad_account_id']}."}
    ad = found["entity"]
    if ad.get("status") == "archived":
        return {"error": f"Ad '{ad.get('name')}' is archived and cannot be changed."}
    creative = ad.get("creative") or {}
    multi = bool(creative.get("file_ids") or len(creative.get("images") or []) > 1)
    payload: dict[str, Any] = {}
    shown: dict[str, Any] = {}
    touched: list[str] = []
    if name and name.strip() != ad.get("name"):
        payload["name"] = name.strip()
        shown["name"] = {"from": ad.get("name"), "to": name.strip()}
        touched.append("name")
    creative_changes = {
        k: v.strip() for k, v in (("title", title), ("body", body), ("target_url", target_url)) if v
        and v.strip() != (creative.get(k) or "")
    }
    if image and multi:
        return _validation_error(["This is a multi-image ad; replace its images in Ads Manager or create a new ad."])
    if creative.get("type") not in (None, "chat_card"):
        if creative_changes or image:
            return _validation_error([f"Only chat_card creatives can be edited here (this is {creative.get('type')})."])
    if creative_changes or image:
        new_creative: dict[str, Any] = {
            "type": "chat_card",
            "title": creative_changes.get("title", creative.get("title")),
            "body": creative_changes.get("body", creative.get("body")),
            "target_url": creative_changes.get("target_url", creative.get("target_url")),
        }
        if not multi:
            if not image and not creative.get("file_id"):
                return _validation_error(["The ad's current image id is not available; pass image_url to set the image."])
            if not image:
                new_creative["file_id"] = creative.get("file_id")
        payload["creative"] = new_creative
        touched.append("creative")
        for key, value in creative_changes.items():
            shown[key] = {"from": creative.get(key), "to": value}
        if image:
            shown["image"] = {"from": creative.get("file_id"), "to": image.get("image_url") or Path(image["image_path"]).name}
        warnings.append("Editing the creative submits a new version for review; it serves again once approved.")
    if not payload and not image:
        return {"error": "Nothing to change: pass at least one field that differs from the ad."}
    return _store(
        {
            "operation": operation,
            "entity_type": "ad",
            "entity_id": ad_id,
            "customer_id": meta["ad_account_id"],
            "changes": {
                "ad_account_id": account,
                "entity_name": ad.get("name"),
                "ad_group_name": found["ad_group"].get("name"),
                "campaign_name": found["campaign"].get("name"),
                "ad_status": ad.get("status"),
                "changes": shown,
                "body": payload,
                "image": image,
                "before": _snapshot(ad, sorted(set(touched))),
            },
        },
        warnings=warnings,
    )


def draft_chatgpt_ad(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_group_id: str = "",
    name: str = "",
    title: str = "",
    body: str = "",
    target_url: str = "",
    image_url: str = "",
    image_path: str = "",
    file_id: str = "",
) -> dict:
    """Draft a new chat-card ad in an ad group, created PAUSED."""
    operation = "chatgpt_create_ad"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    ad_group_id = (ad_group_id or "").strip()
    if not ad_group_id:
        errors.append("ad_group_id is required")
    _check_copy(title, body, errors)
    name = (name or title or "").strip()
    _check_name(name, errors)
    target_url = (target_url or "").strip()
    if not target_url.startswith(("http://", "https://")):
        errors.append("target_url is required and must start with http:// or https://")
    else:
        _check_urls([target_url], errors, warnings)
    image = _image_source(image_url, image_path, file_id, errors)
    if not image and not errors:
        errors.append("An image is required: pass image_url, image_path or file_id (from another ad).")
    if errors:
        return _validation_error(errors)
    meta = account_meta(config, ad_account_id)
    account = (ad_account_id or "").strip()
    found = find_entity(config, account, "ad_group", ad_group_id)
    if not found:
        return {"error": f"ChatGPT Ads ad group '{ad_group_id}' was not found in {meta['ad_account_id']}."}
    group, campaign = found["entity"], found["campaign"]
    if group.get("status") == "archived" or campaign.get("status") == "archived":
        return {"error": f"Ad group '{group.get('name')}' or its campaign is archived."}
    creative = {"type": "chat_card", "title": title.strip(), "body": body.strip(), "target_url": target_url}
    if image.get("file_id"):
        creative["file_id"] = image["file_id"]
    payload = {"ad_group_id": ad_group_id, "name": name, "status": "paused", "creative": creative}
    warnings.append(f"Images should be at least {_MIN_IMAGE_SIDE}×{_MIN_IMAGE_SIDE} px; OpenAI reviews every new ad.")
    return _store(
        {
            "operation": operation,
            "entity_type": "ad",
            "entity_id": "",
            "customer_id": meta["ad_account_id"],
            "changes": {
                "ad_account_id": account,
                "entity_name": name,
                "ad_group_name": group.get("name"),
                "campaign_name": campaign.get("name"),
                "status_on_create": "paused",
                "title": creative["title"],
                "body_text": creative["body"],
                "target_url": target_url,
                "image": image.get("image_url") or (Path(image["image_path"]).name if image.get("image_path") else image.get("file_id")),
                "body": payload,
                "image_source": image,
            },
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Dry run and apply
# ---------------------------------------------------------------------------


def is_chatgpt_plan(plan: ChangePlan) -> bool:
    return str(plan.operation or "").startswith(OPERATION_PREFIX)


def _require_unchanged(entity: dict, before: dict, what: str) -> None:
    for key, value in (before or {}).items():
        if not _same(entity.get(key), value):
            raise ValueError(f"The {what}'s {key} changed since the draft; draft again.")


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read the target and re-check the gates; sends nothing."""
    from adloop.safety.guards import check_bid_increase

    changes = plan.changes or {}
    account = changes.get("ad_account_id") or ""
    op = plan.operation
    checks: dict[str, Any] = {"ad_account_id": plan.customer_id}
    # The key in use must still belong to the previewed account.
    current = account_meta(config, account, fresh=True)["ad_account_id"]
    if plan.customer_id and current != plan.customer_id:
        raise ValueError(
            f"The ChatGPT Ads key now belongs to account {current}, not {plan.customer_id} which the "
            "preview showed; nothing was sent. Draft again for the intended account."
        )

    if op in ("chatgpt_set_status", "chatgpt_archive_entity"):
        found = find_entity(config, account, plan.entity_type, plan.entity_id)
        if not found:
            raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
        now = found["entity"].get("status")
        if now != changes.get("current_status"):
            raise ValueError(f"The status changed since the draft ({changes.get('current_status')} → {now}); draft again.")
        checks.update(entity=found["entity"].get("name"), status_now=now)
    elif op in ("chatgpt_update_campaign", "chatgpt_update_ad_group", "chatgpt_update_ad"):
        found = find_entity(config, account, plan.entity_type, plan.entity_id)
        if not found:
            raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
        entity = found["entity"]
        if entity.get("status") == "archived":
            raise ValueError(f"{plan.entity_type} '{entity.get('name')}' was archived since the draft.")
        _require_unchanged(entity, changes.get("before") or {}, plan.entity_type.replace("_", " "))
        body = changes.get("body") or {}
        if op == "chatgpt_update_campaign" and {"budget", "start_time", "end_time"} & set(body):
            checks["daily_equivalent"] = _check_flight_budget(
                body.get("budget") or entity.get("budget") or {},
                body["start_time"] if "start_time" in body else entity.get("start_time"),
                body["end_time"] if "end_time" in body else entity.get("end_time"),
                config,
            )
            checks["budget_cap_ok"] = True
        if op == "chatgpt_update_ad_group" and "bidding_config" in body:
            old = _exact_amount((entity.get("bidding_config") or {}).get("max_bid_micros"))
            new = _exact_amount(body["bidding_config"].get("max_bid_micros"))
            if old and new:
                check_bid_increase(old, new, config.safety)
            checks["bid_increase_ok"] = True
        if op == "chatgpt_update_ad":
            url = ((body.get("creative") or {}).get("target_url")) if "target_url" in (changes.get("changes") or {}) else None
            if url:
                errors: list[str] = []
                _check_urls([url], errors, [])
                if errors:
                    raise ValueError("; ".join(errors))
            image = changes.get("image") or {}
            if image.get("image_url") or image.get("image_path"):
                problem = _image_problem(image.get("image_url", ""), image.get("image_path", ""))
                if problem:
                    raise ValueError(problem)
        checks["entity"] = entity.get("name")
        checks["unchanged_since_draft"] = True
    elif op == "chatgpt_create_campaign":
        body = changes.get("body") or {}
        checks["daily_equivalent"] = _check_flight_budget(
            body.get("budget") or {}, body.get("start_time"), body.get("end_time"), config,
        )
        checks["budget_cap_ok"] = True
        event_ids = (changes.get("body") or {}).get("conversion_event_setting_ids") or []
        if event_ids:
            settings, _err = _event_settings(config, account)
            live = {str(s.get("id")) for s in settings if not s.get("archived")}
            missing = [i for i in event_ids if i not in live]
            if missing:
                raise ValueError(f"Conversion event setting(s) {missing} are gone or archived.")
        checks["status_on_create"] = "paused"
    elif op == "chatgpt_create_ad_group":
        campaign_id = (changes.get("body") or {}).get("campaign_id", "")
        found = find_entity(config, account, "campaign", campaign_id)
        if not found or found["entity"].get("status") == "archived":
            raise ValueError(f"campaign '{campaign_id}' no longer exists or was archived.")
        strategy = ((changes.get("body") or {}).get("bidding_config") or {}).get("strategy")
        if strategy != "fixed_bid" and (found["entity"].get("budget") or {}).get("daily_spend_limit_micros") is None:
            raise ValueError(f"{strategy} needs a daily campaign budget, which the campaign no longer has.")
        checks.update(campaign=found["entity"].get("name"), status_on_create="paused")
    elif op == "chatgpt_create_ad":
        payload = changes.get("body") or {}
        found = find_entity(config, account, "ad_group", payload.get("ad_group_id", ""))
        if not found or found["entity"].get("status") == "archived":
            raise ValueError(f"ad group '{payload.get('ad_group_id')}' no longer exists or was archived.")
        errors = []
        _check_urls([payload["creative"]["target_url"]], errors, [])
        if errors:
            raise ValueError("; ".join(errors))
        image = changes.get("image_source") or {}
        if image.get("image_url") or image.get("image_path"):
            problem = _image_problem(image.get("image_url", ""), image.get("image_path", ""))
            if problem:
                raise ValueError(problem)
        checks.update(ad_group=found["entity"].get("name"), landing_page_ok=True, status_on_create="paused")
    else:
        raise ValueError(f"Unknown ChatGPT Ads operation: {op}")
    return checks


def _upload(config: AdLoopConfig, account: str, image: dict) -> str:
    if image.get("file_id"):
        return str(image["file_id"])
    if image.get("image_url"):
        result = api_post(config, "upload", {"image_url": image["image_url"]}, ad_account_id=account)
    else:
        path = Path(image["image_path"])
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result = api_upload(config, path.name, path.read_bytes(), mime, ad_account_id=account)
    file_id = result.get("file_id") or result.get("id")
    if not file_id:
        raise ChatGPTAdsApiError("OpenAI accepted the image but returned no file id.")
    return str(file_id)


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a ChatGPT Ads plan. Called by ``confirm_and_apply`` after the gate.

    Re-runs :func:`preflight` first: a dry run can be old, and the plan must
    still describe the live entity when it is applied.
    """
    preflight(config, plan)
    changes = plan.changes or {}
    account = changes.get("ad_account_id") or ""
    op = plan.operation
    key = f"adloop-{plan.plan_id}"

    if op in ("chatgpt_set_status", "chatgpt_archive_entity"):
        collection = _COLLECTIONS[plan.entity_type]
        result = api_post(config, f"{collection}/{plan.entity_id}/{changes['action']}", {}, ad_account_id=account)
        return {"entity_type": plan.entity_type, "entity_id": plan.entity_id,
                "name": changes.get("entity_name"), "status": result.get("status") or changes["target_status"]}

    if op in ("chatgpt_update_campaign", "chatgpt_update_ad_group", "chatgpt_update_ad"):
        body = copy.deepcopy(changes.get("body") or {})
        if op == "chatgpt_update_ad" and (changes.get("image") or {}):
            file_id = _upload(config, account, changes["image"])
            # Kept on the (claimed) plan: if the update's response is lost, a
            # retry sends the same file instead of uploading another copy.
            changes["image"] = {"file_id": file_id}
            body.setdefault("creative", {})["file_id"] = file_id
        collection = _COLLECTIONS[plan.entity_type]
        result = api_post(config, f"{collection}/{plan.entity_id}", body, ad_account_id=account)
        out = {"entity_type": plan.entity_type, "entity_id": plan.entity_id,
               "name": result.get("name") or changes.get("entity_name"), "updated_fields": sorted(body)}
        if op == "chatgpt_update_ad" and "creative" in body:
            out["review_status"] = result.get("review_status") or (result.get("review") or {}).get("status")
        return out

    if op == "chatgpt_create_campaign":
        result = api_post(config, "campaigns", changes["body"], ad_account_id=account, idempotency_key=key)
        return {"entity_type": "campaign", "campaign_id": result.get("id"), "name": result.get("name"),
                "status": result.get("status"), "next": "Add ad groups and ads, then enable it."}
    if op == "chatgpt_create_ad_group":
        result = api_post(config, "ad_groups", changes["body"], ad_account_id=account, idempotency_key=key)
        return {"entity_type": "ad_group", "ad_group_id": result.get("id"), "name": result.get("name"),
                "status": result.get("status")}
    if op == "chatgpt_create_ad":
        body = copy.deepcopy(changes["body"])
        file_id = _upload(config, account, changes.get("image_source") or {})
        # The idempotency key only protects a retry that sends the same request:
        # keep the uploaded file on the (claimed) plan so a retry after a lost
        # response reuses it instead of uploading a new file.
        changes["image_source"] = {"file_id": file_id}
        body["creative"]["file_id"] = file_id
        result = api_post(config, "ads", body, ad_account_id=account, idempotency_key=key)
        return {"entity_type": "ad", "ad_id": result.get("id"), "name": result.get("name"),
                "status": result.get("status"),
                "review_status": result.get("review_status") or (result.get("review") or {}).get("status")}
    raise ValueError(f"Unknown ChatGPT Ads operation: {op}")
