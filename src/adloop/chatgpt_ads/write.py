"""ChatGPT Ads write tools — draft → preview → (dry run) → apply.

Same gate as Google Ads and Reddit writes: a draft validates the
request, runs the safety guards, stores a :class:`ChangePlan` (platform
``chatgpt``, operations prefixed ``chatgpt_``) and returns its preview;
``confirm_and_apply`` hands the plan to :func:`preflight` (dry run) or
:func:`apply_plan`.

A plan's ``changes`` hold only what the person approves. The request the
applier sends, the snapshot the preflight compares and the pinned image
live in ``ChangePlan.apply_only_payload``, which no preview, dialog, card or
audit entry shows.

The Advertiser API has no validate-only mode for single resources, so the
dry run re-reads the target by id, confirms that every subfield the plan
sends still holds the value the preview showed (or already holds the
planned value, after a lost answer), and re-runs the budget cap, the
bid-increase cap, the landing-page check and the image's checksum.
Nothing is sent. The real apply runs the same preflight first, so a change
made in Ads Manager in between is refused instead of overwritten.

API rules this module follows (developers.openai.com/ads):

- Updates are ``POST /<collection>/<id>`` with only the fields that change.
  ``budget`` and ``bidding_config`` are sent whole; ``context_hints`` and
  geo/audience targeting replace what was there, so the full intended
  value is built from the current one.
- Status changes use ``/pause``, ``/activate`` and ``/archive``; archive
  cannot be undone.
- New campaigns, ad groups and ads are created **paused**, each with an
  ``Idempotency-Key`` derived from the plan. OpenAI documents the header on
  all three create endpoints: "If a matching prior create is visible, the
  existing object is returned instead of creating a duplicate." A retry of
  the same plan sends the same key and body; a retry that comes before
  OpenAI can see the first create can still duplicate it.
- Images are uploaded as the bytes AdLoop checked (multipart
  ``POST /upload``), never as a URL for OpenAI to fetch again.
- Money is micros on the wire, account currency in every preview.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from adloop.chatgpt_ads.client import (
    ChatGPTAdsApiError,
    ChatGPTAdsOutcomeUnknown,
    api_get,
    api_get_all,
    api_post,
    api_upload,
    exact_from_micros,
    from_micros,
    to_micro,
)
from adloop.chatgpt_ads.read import (
    _event_settings,
    _locations,
    _midnight,
    _zone,
    account_meta,
    event_key,
)
from adloop.safety.guards import blocked_operation_error, store_draft
from adloop.safety.preview import ChangePlan

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

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
# A preview lists this many hints; the rest are counted.
_SHOWN_HINTS = 20
_MAX_LOCATIONS = 2500
# OpenAI's spec allows daily_spend_limit_micros from 1,000,000, but the API
# refuses less: "Daily campaign budget must be at least 25000000 micros"
# (seen live, USD account, 2026-11-08). Refusing at the draft keeps a dry run
# from passing a create the apply cannot make.
_MIN_DAILY_BUDGET = 25.0
_MIN_IMAGE_SIDE = 640


def _validation_error(details: list[str]) -> dict:
    return {"error": "Validation failed", "details": details}


def _store(*, warnings: list[str] | None = None, **plan_fields: Any) -> dict:
    """Store a ChatGPT Ads plan; ``customer_id`` is always the resolved ad account id.

    The account is pinned: the preflight re-reads which account the key in
    use belongs to and refuses when it is not this one (a key or default
    changed after the preview), so a plan can never land in another account.
    """
    plan_fields["changes"] = _shown(plan_fields.get("changes") or {})
    return store_draft(ChangePlan(platform=PLATFORM, **plan_fields), warnings=warnings)


def _shown(changes: dict) -> dict:
    """What the person approves, without empty values ("- ad account id: ")."""
    return {k: v for k, v in changes.items() if v not in (None, "", [], {})}


def _micros(amount: float) -> int:
    return to_micro(amount)


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


def _hint_sample(hints: list[str]) -> list[str]:
    """Hints for a preview: the first few, then a count of the rest."""
    if len(hints) <= _SHOWN_HINTS:
        return list(hints)
    return hints[:_SHOWN_HINTS] + [f"… {len(hints) - _SHOWN_HINTS} more"]


# ---------------------------------------------------------------------------
# Locating entities
# ---------------------------------------------------------------------------


def find_entity(config: AdLoopConfig, account: str, entity_type: str, entity_id: str) -> dict | None:
    """The entity (read by id) with its campaign (and ad group), archived ones included.

    Ad groups and ads do not carry their parent's id, so the parents come
    from a scan of the account; drafts use it for the names a preview shows.
    The list endpoints lag a few minutes behind a create (seen live: a new
    campaign was missing from GET /campaigns right after POST /campaigns), so
    the entity itself is read by id and a scan that misses it only leaves
    the parents unknown (``parents_unknown``). The dry run and the apply
    read by id only (:func:`_get`).
    """
    entity = _get(config, account, entity_type, entity_id)
    if entity is None:
        return None
    if entity_type == "campaign":
        return {"entity": entity, "campaign": entity}
    found = _scan_for(config, account, entity_type, str(entity_id))
    if found is None:
        return {"entity": entity, "campaign": {}, "ad_group": entity if entity_type == "ad_group" else {},
                "parents_unknown": True}
    found["entity"] = entity
    return found


def _scan_for(config: AdLoopConfig, account: str, entity_type: str, entity_id: str) -> dict | None:
    """Find an ad group or ad in the account lists, with its parents."""
    campaigns = api_get_all(config, "campaigns", {"include[]": ["serving_issues"]}, ad_account_id=account)
    for campaign in campaigns:
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


def _get(config: AdLoopConfig, account: str, entity_type: str, entity_id: str) -> dict | None:
    """``GET /<collection>/<id>``: one request; None when it no longer exists."""
    path = f"{_COLLECTIONS[entity_type]}/{entity_id}"
    try:
        return api_get(config, path, {"include[]": ["serving_issues"]}, ad_account_id=account)
    except ChatGPTAdsApiError as exc:
        if exc.status == 404:
            return None
        raise


# Subfields an update sends back for each dict-valued field. Anything else in
# the API's answer (resolved image URLs, location names) is response-only and
# never counts as an edit made in Ads Manager.
_SENT_KEYS = {
    "budget": ("daily_spend_limit_micros", "lifetime_spend_limit_micros"),
}


def _compare_keys(body: dict, touched: list[str], *, extra: dict | None = None) -> dict:
    """For each touched dict-valued field, the subfields this plan sends."""
    keys: dict[str, list[str]] = {}
    for field in touched:
        value = body.get(field)
        if field == "targeting":
            continue  # compared in its writable form as a whole
        if field in _SENT_KEYS:
            keys[field] = list(_SENT_KEYS[field])
        elif isinstance(value, dict):
            keys[field] = sorted(set(value) | set((extra or {}).get(field, ())))
    return keys


def _project(field: str, value: Any, keys: list[str] | None) -> Any:
    """``value`` reduced to what the plan sends, in a comparable form."""
    if field == "targeting":
        return writable_targeting({"targeting": value})
    if keys is None:
        return copy.deepcopy(value)
    source = value if isinstance(value, dict) else {}
    out = {}
    for key in keys:
        sub = source.get(key)
        if key == "custom_audience_bid_multipliers":
            sub = sorted(
                [str(m.get("custom_audience_id")), m.get("bid_multiplier_micros")]
                for m in sub or [] if isinstance(m, dict)
            )
        out[key] = copy.deepcopy(sub)
    return out


def _snapshot(entity: dict, touched: list[str], keys: dict) -> dict:
    """The touched fields as the API returned them, reduced to what the plan sends."""
    return {f: _project(f, entity.get(f), keys.get(f)) for f in touched}


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
    blocked = blocked_operation_error(operation, config.safety)
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
        if found.get("parents_unknown"):
            warnings.append(
                "OpenAI's account lists do not show it yet (they lag a few minutes behind a create), so "
                "the status of its campaign and ad group could not be checked."
            )
        for level in ("campaign", "ad_group"):
            parent = found.get(level)
            if parent and parent is not entity and parent.get("status") != "active":
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
        warnings=warnings,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=meta["ad_account_id"],
        requires_double_confirm=action == "archive",
        changes={
            "entity_name": entity.get("name"),
            "campaign_name": found["campaign"].get("name") if entity_type != "campaign" else None,
            "ad_group_name": (found.get("ad_group") or {}).get("name") if entity_type == "ad" else None,
            "current_status": current,
            "target_status": target,
        },
        apply_only_payload={"ad_account_id": account, "action": action},
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


def _min_daily_text(amount: Any) -> str:
    return (
        f"daily_budget {float(amount):g} is below OpenAI's minimum daily campaign budget of "
        f"{_MIN_DAILY_BUDGET:g} (in the account currency); OpenAI refuses the create or update."
    )


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
    blocked = blocked_operation_error(operation, config.safety)
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
    if daily_budget is not None and 0 < float(daily_budget) < _MIN_DAILY_BUDGET:
        errors.append(_min_daily_text(daily_budget))
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
    # per day. The cap and the size of the change are both judged on daily
    # equivalents: a lifetime 500 over 31 days (about 16 a day) becoming a
    # daily 100 is about 6x the spend, not a fifth of it.
    old_daily = _daily_equivalent(budget, campaign.get("start_time"), campaign.get("end_time"))
    daily = _daily_equivalent(new_budget or budget, start_ts, end_ts)
    spend_changes = bool(new_budget) or "start_time" in body or "end_time" in body
    if spend_changes:
        try:
            daily = _check_flight_budget(new_budget or budget, start_ts, end_ts, config)
        except SafetyViolation as e:
            return {"error": str(e)}
    currency = meta["currency"]
    double = False
    if spend_changes and daily != old_daily:
        if old_daily is None:
            # The current spend cannot be worked out (a lifetime budget without
            # an end), so the change cannot be sized: confirm it twice.
            double = True
            warnings.append("The current budget's daily spend cannot be worked out (no end date), so this change cannot be sized.")
        else:
            double = requires_double_confirmation(operation, current_budget=old_daily, proposed_budget=daily)
            if daily is not None and daily > old_daily * 2:
                warnings.append(
                    f"This more than doubles the daily spend ({old_daily} → {daily} {currency} a day, "
                    f"about {daily / old_daily:.1f}x)." if old_daily else f"The daily spend goes from 0 to {daily} {currency}."
                )
        shown["daily_spend"] = {"from": old_daily, "to": daily, "currency": currency}
    if new_budget and "daily_spend_limit_micros" in new_budget and current_daily is None and current_lifetime is not None:
        if old_daily:
            warnings.append(
                f"This switches the campaign from a lifetime budget of {current_lifetime} {currency} "
                f"(about {old_daily} a day over its flight) to a daily budget of {daily} {currency}, "
                f"about {daily / old_daily:.1f}x the daily spend. OpenAI allows this switch one way only."
            )
        else:
            warnings.append(
                "This switches the campaign from a lifetime to a daily budget (allowed one way only)."
            )
    if "targeting" in body:
        warnings.append("Location and audience targeting is replaced as a whole; the preview shows the full new set.")

    touched = sorted(set(touched))
    keys = _compare_keys(body, touched)
    return _store(
        warnings=warnings,
        operation=operation,
        entity_type="campaign",
        entity_id=campaign_id,
        customer_id=meta["ad_account_id"],
        requires_double_confirm=double,
        changes={
            "entity_name": campaign.get("name"),
            "campaign_status": campaign.get("status"),
            "display": shown,
        },
        apply_only_payload={
            "ad_account_id": account,
            "body": body,
            "before": _snapshot(campaign, touched, keys),
            "compare_keys": keys,
        },
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
    blocked = blocked_operation_error(operation, config.safety)
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
    if 0 < daily < _MIN_DAILY_BUDGET:
        errors.append(_min_daily_text(daily))
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
        warnings=warnings,
        operation=operation,
        entity_type="campaign",
        entity_id="",
        customer_id=meta["ad_account_id"],
        changes={
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
            "daily_equivalent": daily_eq if lifetime else None,
        },
        apply_only_payload={"ad_account_id": account, "body": body},
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
        # Audience multipliers work with fixed bids only. An update clears
        # existing ones with an empty list, as OpenAI's bidding guide does;
        # otherwise the field is left out.
        if new.get("custom_audience_bid_multipliers"):
            new["custom_audience_bid_multipliers"] = []
        else:
            new.pop("custom_audience_bid_multipliers", None)
        if (campaign.get("budget") or {}).get("daily_spend_limit_micros") is None:
            errors.append(f"{chosen} needs a daily campaign budget.")
    return new


def _exact_amount(micros: Any) -> float | None:
    """Micros → currency amount, unrounded (see client.exact_from_micros)."""
    return exact_from_micros(micros)


def _check_bid_micros(old_micros: Any, new_micros: Any, config: AdLoopConfig) -> None:
    """The bid-increase cap in whole micros, so an increase of exactly the cap
    passes and one micro more does not (floats get the boundary wrong)."""
    from adloop.safety.guards import SafetyViolation

    try:
        old, new = int(old_micros), int(new_micros)
    except (TypeError, ValueError):
        return
    if old <= 0:
        return
    cap = config.safety.max_bid_increase_pct
    if new * 100 > old * (100 + cap):
        raise SafetyViolation(f"Bid increase {(new - old) * 100 / old:.0f}% exceeds maximum {cap}%")


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
    blocked = blocked_operation_error(operation, config.safety)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation

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
    if found.get("parents_unknown"):
        return {"error": (
            f"Ad group '{ad_group_id}' exists, but OpenAI's account lists do not show it yet (they lag a "
            "few minutes behind a create), and its campaign's bidding is needed to check the change. "
            "Try again in a few minutes."
        )}
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
                _check_bid_micros(current.get("max_bid_micros"), new.get("max_bid_micros"), config)
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
            "added": _hint_sample([h for h in new_hints if h not in hints_now]),
            "removed": _hint_sample([h for h in hints_now if h not in new_hints]),
            "count": {"from": len(hints_now), "to": len(new_hints)},
        }
    if errors:
        return _validation_error(errors)
    if not body:
        return {"error": "Nothing to change: pass at least one field that differs from the ad group."}
    touched = sorted(set(touched))
    keys = _compare_keys(body, touched)
    return _store(
        warnings=warnings,
        operation=operation,
        entity_type="ad_group",
        entity_id=ad_group_id,
        customer_id=meta["ad_account_id"],
        changes={
            "entity_name": group.get("name"),
            "campaign_name": campaign.get("name"),
            "ad_group_status": group.get("status"),
            "display": shown,
        },
        apply_only_payload={
            "ad_account_id": account,
            "body": body,
            "before": _snapshot(group, touched, keys),
            "compare_keys": keys,
        },
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
    blocked = blocked_operation_error(operation, config.safety)
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
        warnings=warnings,
        operation=operation,
        entity_type="ad_group",
        entity_id="",
        customer_id=meta["ad_account_id"],
        changes={
            "entity_name": name.strip(),
            "campaign_name": campaign.get("name"),
            "campaign_status": campaign.get("status"),
            "status_on_create": "paused",
            "bid": _bid_label(bidding, campaign.get("bidding_type")),
            "currency": meta["currency"],
            "context_hints": _hint_sample(hints),
            "context_hint_count": len(hints) if len(hints) > _SHOWN_HINTS else None,
        },
        apply_only_payload={"ad_account_id": account, "body": body},
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
_IMAGE_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WebP": "image/webp"}
_IMAGE_EXT = {"PNG": "png", "JPEG": "jpg", "WebP": "webp"}


def _image_kind(data: bytes) -> str | None:
    for signature, kind in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    return None


def _read_image(image_url: str = "", image_path: str = "") -> tuple[bytes, str]:
    """The image's bytes and kind; ValueError says why it cannot be used.

    Remote images go through AdLoop's public-only fetcher: the host must
    resolve to a public address, every redirect is checked again, the
    connection is pinned to the checked address and the download is capped,
    so a draft cannot be used to reach the server's private network.
    """
    from adloop.ads.image_fetch import MAX_IMAGE_BYTES

    if image_path:
        from adloop.runtime import deployment_mode

        if deployment_mode() == "server":
            raise ValueError("image_path reads a local file, which a hosted server cannot; pass image_url.")
        path = Path(image_path).expanduser()
        if not path.is_file():
            raise ValueError(f"{path} does not exist.")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"{path.name} is larger than {MAX_IMAGE_BYTES // 1024} KB.")
        data = path.read_bytes()
        kind = _image_kind(data[:16])
        if kind is None:
            raise ValueError(f"{path.name} is not a JPEG, PNG or WebP image.")
        return data, kind
    if not image_url.startswith(("http://", "https://")):
        raise ValueError("image_url must be a public http(s) link.")
    from adloop.ads.image_fetch import ImageFetchError, fetch_image_bytes

    try:
        fetched = fetch_image_bytes(image_url)
    except ImageFetchError as exc:
        raise ValueError(f"image_url cannot be used: {exc}") from None
    data = bytes(fetched.data or b"")
    kind = _image_kind(data[:16])
    if kind is None:
        raise ValueError("image_url does not serve a JPEG, PNG or WebP image.")
    return data, kind


def _image_source(image_url: str, image_path: str, file_id: str, errors: list[str]) -> dict | None:
    """The image a plan uses: a file id, or the source pinned to the bytes checked now."""
    given = [k for k, v in (("image_url", image_url), ("image_path", image_path), ("file_id", file_id)) if v]
    if len(given) > 1:
        errors.append(f"Pass one image source, not {given}.")
        return None
    if not given:
        return None
    if file_id:
        return {"file_id": file_id.strip()}
    try:
        data, kind = _read_image(image_url=image_url.strip(), image_path=image_path.strip())
    except ValueError as exc:
        errors.append(str(exc))
        return None
    source = {"image_url": image_url.strip()} if image_url else {"image_path": str(Path(image_path).expanduser())}
    # The checksum pins the image the person approved: the dry run and the
    # apply read it again and refuse different bytes, and the apply uploads
    # exactly the bytes it checked.
    return {**source, "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, "size": len(data)}


def _verified_image(image: dict | None) -> bytes | None:
    """The pinned image's bytes, read again and checked against the draft; None for a file id."""
    if not image or image.get("file_id"):
        return None
    data, _kind = _read_image(image.get("image_url", ""), image.get("image_path", ""))
    if hashlib.sha256(data).hexdigest() != image.get("sha256"):
        where = image.get("image_url") or Path(image.get("image_path", "")).name
        raise ValueError(
            f"The image at {where} changed since the draft (different bytes); nothing was sent. "
            "Draft again to approve the new image."
        )
    return data


def _image_label(image: dict) -> str:
    if image.get("file_id"):
        return str(image["file_id"])
    where = image.get("image_url") or Path(image.get("image_path", "")).name
    return f"{where} ({image.get('kind')}, {int(image.get('size') or 0) / 1024:.1f} KB)"


def _upload_name(image: dict) -> str:
    if image.get("image_path"):
        return Path(image["image_path"]).name
    name = Path(urlparse(image.get("image_url", "")).path).name
    return name if "." in name else f"image.{_IMAGE_EXT.get(image.get('kind'), 'png')}"


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
    blocked = blocked_operation_error(operation, config.safety)
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
            shown["image"] = {"from": creative.get("file_id"), "to": _image_label(image)}
        warnings.append("Editing the creative submits a new version for review; it serves again once approved.")
    if not payload and not image:
        return {"error": "Nothing to change: pass at least one field that differs from the ad."}
    touched = sorted(set(touched))
    # A new image's file id is added to the creative at apply, so it is one of
    # the subfields this plan sends.
    keys = _compare_keys(payload, touched, extra={"creative": ["file_id"]} if image else None)
    return _store(
        warnings=warnings,
        operation=operation,
        entity_type="ad",
        entity_id=ad_id,
        customer_id=meta["ad_account_id"],
        changes={
            "entity_name": ad.get("name"),
            "ad_group_name": found["ad_group"].get("name"),
            "campaign_name": found["campaign"].get("name"),
            "ad_status": ad.get("status"),
            "display": shown,
        },
        apply_only_payload={
            "ad_account_id": account,
            "body": payload,
            "image": image or {},
            "before": _snapshot(ad, touched, keys),
            "compare_keys": keys,
        },
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
    blocked = blocked_operation_error(operation, config.safety)
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
        warnings=warnings,
        operation=operation,
        entity_type="ad",
        entity_id="",
        customer_id=meta["ad_account_id"],
        changes={
            "entity_name": name,
            "ad_group_name": group.get("name"),
            "campaign_name": campaign.get("name"),
            "status_on_create": "paused",
            "title": creative["title"],
            "body_text": creative["body"],
            "target_url": target_url,
            "image": _image_label(image),
        },
        apply_only_payload={"ad_account_id": account, "body": payload, "image": image},
    )


# ---------------------------------------------------------------------------
# Dry run and apply
# ---------------------------------------------------------------------------


def _payload(plan: ChangePlan) -> dict:
    payload = plan.apply_only_payload or {}
    if not payload:
        # A plan store that drops the field must not turn into an empty request.
        raise ValueError(
            "This plan's request was not kept by the plan store (apply_only_payload is empty); "
            "nothing was sent. Draft the change again."
        )
    return payload


def _require_current(entity: dict, payload: dict, what: str) -> list[str]:
    """Refuse when a field the plan sends was edited since the draft.

    Compares only the subfields the plan sends. A field that already holds
    the planned value counts as done (an earlier apply whose answer was
    lost); the fields still to change are returned.
    """
    body = payload.get("body") or {}
    keys = payload.get("compare_keys") or {}
    pending = []
    for field, then in (payload.get("before") or {}).items():
        now = _project(field, entity.get(field), keys.get(field))
        planned = _project(field, body[field], keys.get(field)) if field in body else then
        if _same(now, planned):
            continue
        if not _same(now, then):
            raise ValueError(f"The {what}'s {field} changed since the draft; draft again.")
        pending.append(field)
    return pending


def _check(config: AdLoopConfig, plan: ChangePlan) -> tuple[dict, bytes | None]:
    """Re-read the target by id and re-check the gates; sends nothing.

    Returns the checks and, for an ad with a new image, the image bytes that
    matched the draft's checksum (the apply uploads exactly those).
    """
    changes = plan.changes or {}
    payload = _payload(plan)
    account = payload.get("ad_account_id") or ""
    op = plan.operation
    checks: dict[str, Any] = {"ad_account_id": plan.customer_id}
    image_data: bytes | None = None
    # The key in use must still belong to the previewed account.
    current = account_meta(config, account, fresh=True)["ad_account_id"]
    if plan.customer_id and current != plan.customer_id:
        raise ValueError(
            f"The ChatGPT Ads key now belongs to account {current}, not {plan.customer_id} which the "
            "preview showed; nothing was sent. Draft again for the intended account."
        )

    if op in ("chatgpt_set_status", "chatgpt_archive_entity"):
        entity = _get(config, account, plan.entity_type, plan.entity_id)
        if entity is None:
            raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
        now = entity.get("status")
        target, then = changes.get("target_status"), changes.get("current_status")
        if now == target and now != then:
            checks["already_applied"] = True
        elif now != then:
            raise ValueError(f"The status changed since the draft ({then} → {now}); draft again.")
        checks.update(entity=entity.get("name"), status_now=now)
    elif op in ("chatgpt_update_campaign", "chatgpt_update_ad_group", "chatgpt_update_ad"):
        entity = _get(config, account, plan.entity_type, plan.entity_id)
        if entity is None:
            raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
        if entity.get("status") == "archived":
            raise ValueError(f"{plan.entity_type} '{entity.get('name')}' was archived since the draft.")
        pending = _require_current(entity, payload, plan.entity_type.replace("_", " "))
        if not pending:
            checks["already_applied"] = True
        body = payload.get("body") or {}
        if op == "chatgpt_update_campaign" and {"budget", "start_time", "end_time"} & set(body):
            checks["daily_equivalent"] = _check_flight_budget(
                body.get("budget") or entity.get("budget") or {},
                body["start_time"] if "start_time" in body else entity.get("start_time"),
                body["end_time"] if "end_time" in body else entity.get("end_time"),
                config,
            )
            checks["budget_cap_ok"] = True
        if op == "chatgpt_update_ad_group" and "bidding_config" in pending:
            old = (entity.get("bidding_config") or {}).get("max_bid_micros")
            new = body["bidding_config"].get("max_bid_micros")
            if old and new:
                _check_bid_micros(old, new, config)
            checks["bid_increase_ok"] = True
        if op == "chatgpt_update_ad":
            url = ((body.get("creative") or {}).get("target_url")) if "target_url" in (changes.get("display") or {}) else None
            if url:
                errors: list[str] = []
                _check_urls([url], errors, [])
                if errors:
                    raise ValueError("; ".join(errors))
            image_data = _verified_image(payload.get("image"))
            if image_data is not None:
                checks["image_unchanged_since_draft"] = True
        checks["entity"] = entity.get("name")
        if pending:
            checks["unchanged_since_draft"] = True
    elif op == "chatgpt_create_campaign":
        body = payload.get("body") or {}
        checks["daily_equivalent"] = _check_flight_budget(
            body.get("budget") or {}, body.get("start_time"), body.get("end_time"), config,
        )
        checks["budget_cap_ok"] = True
        event_ids = body.get("conversion_event_setting_ids") or []
        if event_ids:
            settings, _err = _event_settings(config, account)
            live = {str(s.get("id")) for s in settings if not s.get("archived")}
            missing = [i for i in event_ids if i not in live]
            if missing:
                raise ValueError(f"Conversion event setting(s) {missing} are gone or archived.")
        checks["status_on_create"] = "paused"
    elif op == "chatgpt_create_ad_group":
        body = payload.get("body") or {}
        campaign_id = body.get("campaign_id", "")
        campaign = _get(config, account, "campaign", campaign_id)
        if campaign is None or campaign.get("status") == "archived":
            raise ValueError(f"campaign '{campaign_id}' no longer exists or was archived.")
        strategy = (body.get("bidding_config") or {}).get("strategy")
        if strategy != "fixed_bid" and (campaign.get("budget") or {}).get("daily_spend_limit_micros") is None:
            raise ValueError(f"{strategy} needs a daily campaign budget, which the campaign no longer has.")
        checks.update(campaign=campaign.get("name"), status_on_create="paused")
    elif op == "chatgpt_create_ad":
        body = payload.get("body") or {}
        group = _get(config, account, "ad_group", body.get("ad_group_id", ""))
        if group is None or group.get("status") == "archived":
            raise ValueError(f"ad group '{body.get('ad_group_id')}' no longer exists or was archived.")
        errors = []
        _check_urls([body["creative"]["target_url"]], errors, [])
        if errors:
            raise ValueError("; ".join(errors))
        image_data = _verified_image(payload.get("image"))
        if image_data is not None:
            checks["image_unchanged_since_draft"] = True
        checks.update(ad_group=group.get("name"), landing_page_ok=True, status_on_create="paused")
    else:
        raise ValueError(f"Unknown ChatGPT Ads operation: {op}")
    return checks, image_data


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read the target and re-check the gates; sends nothing."""
    return _check(config, plan)[0]


def _upload(config: AdLoopConfig, account: str, image: dict, data: bytes | None) -> str:
    """The image's file id: given, or from uploading the bytes the preflight checked."""
    if image.get("file_id"):
        return str(image["file_id"])
    if data is None:
        raise ValueError("The image was not read before the upload; nothing was sent.")
    result = api_upload(
        config, _upload_name(image), data, _IMAGE_MIME.get(image.get("kind"), "image/png"),
        ad_account_id=account,
    )
    file_id = result.get("file_id") or result.get("id")
    if not file_id:
        raise ChatGPTAdsApiError("OpenAI accepted the image but returned no file id.")
    return str(file_id)


def _lost_answer(exc: ChatGPTAdsOutcomeUnknown, plan: ChangePlan) -> ChatGPTAdsOutcomeUnknown:
    """The unknown outcome, with what to do about it for this plan."""
    noun = plan.entity_type.replace("_", " ")
    if plan.operation.startswith("chatgpt_create_"):
        advice = (
            f" OpenAI may have created the {noun}: look for it in the account before anything else. "
            "Confirming this plan again sends the same Idempotency-Key and request; OpenAI returns "
            f"the existing {noun} instead of a second one when it can already see the first create, "
            "so give it a minute before retrying."
        )
    else:
        advice = (
            f" Read the {noun} back before anything else. Confirming this plan again is safe: the "
            "dry run sees whether the change is already in place, and then nothing is sent."
        )
    return ChatGPTAdsOutcomeUnknown(str(exc) + advice, status=exc.status, url=exc.url, code=exc.code)


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a ChatGPT Ads plan. Called by ``confirm_and_apply`` after the gate.

    Re-runs the preflight first: a dry run can be old, and the plan must
    still describe the live entity when it is applied.
    """
    checks, image_data = _check(config, plan)
    changes = plan.changes or {}
    payload = _payload(plan)
    account = payload.get("ad_account_id") or ""
    op = plan.operation
    key = f"adloop-{plan.plan_id}"
    already = "Already in place (an earlier apply went through); nothing was sent."

    try:
        if op in ("chatgpt_set_status", "chatgpt_archive_entity"):
            out = {"entity_type": plan.entity_type, "entity_id": plan.entity_id,
                   "name": changes.get("entity_name"), "status": changes["target_status"]}
            if checks.get("already_applied"):
                return {**out, "note": already}
            collection = _COLLECTIONS[plan.entity_type]
            result = api_post(config, f"{collection}/{plan.entity_id}/{payload['action']}", {}, ad_account_id=account)
            return {**out, "status": result.get("status") or changes["target_status"]}

        if op in ("chatgpt_update_campaign", "chatgpt_update_ad_group", "chatgpt_update_ad"):
            body = copy.deepcopy(payload.get("body") or {})
            out = {"entity_type": plan.entity_type, "entity_id": plan.entity_id,
                   "name": changes.get("entity_name"), "updated_fields": sorted(body)}
            if checks.get("already_applied"):
                return {**out, "note": already}
            if op == "chatgpt_update_ad" and payload.get("image"):
                file_id = _upload(config, account, payload["image"], image_data)
                # Kept on the (claimed) plan: if the update's answer is lost, a
                # retry sends the same file, and its dry run recognises it.
                payload["image"] = {"file_id": file_id}
                payload["body"].setdefault("creative", {})["file_id"] = file_id
                body.setdefault("creative", {})["file_id"] = file_id
            collection = _COLLECTIONS[plan.entity_type]
            result = api_post(config, f"{collection}/{plan.entity_id}", body, ad_account_id=account)
            out["name"] = result.get("name") or out["name"]
            if op == "chatgpt_update_ad" and "creative" in body:
                out["review_status"] = result.get("review_status") or (result.get("review") or {}).get("status")
            return out

        if op == "chatgpt_create_campaign":
            result = api_post(config, "campaigns", payload["body"], ad_account_id=account, idempotency_key=key)
            return {"entity_type": "campaign", "campaign_id": result.get("id"), "name": result.get("name"),
                    "status": result.get("status"), "next": "Add ad groups and ads, then enable it."}
        if op == "chatgpt_create_ad_group":
            result = api_post(config, "ad_groups", payload["body"], ad_account_id=account, idempotency_key=key)
            return {"entity_type": "ad_group", "ad_group_id": result.get("id"), "name": result.get("name"),
                    "status": result.get("status")}
        if op == "chatgpt_create_ad":
            file_id = _upload(config, account, payload.get("image") or {}, image_data)
            # The idempotency key only protects a retry that sends the same
            # request: keep the uploaded file on the (claimed) plan so a retry
            # after a lost answer reuses it instead of uploading a new file.
            payload["image"] = {"file_id": file_id}
            payload["body"]["creative"]["file_id"] = file_id
            result = api_post(config, "ads", payload["body"], ad_account_id=account, idempotency_key=key)
            return {"entity_type": "ad", "ad_id": result.get("id"), "name": result.get("name"),
                    "status": result.get("status"),
                    "review_status": result.get("review_status") or (result.get("review") or {}).get("status")}
    except ChatGPTAdsOutcomeUnknown as exc:
        raise _lost_answer(exc, plan) from None
    raise ValueError(f"Unknown ChatGPT Ads operation: {op}")
