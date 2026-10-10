"""Reddit Ads write tools — draft → preview → (dry run) → apply.

Every mutation goes through the same gate as Google Ads writes: a
``draft_*`` / ``update_*`` / ``pause_*`` function validates the request,
runs the safety guards, stores a :class:`ChangePlan` and returns its
preview. ``confirm_and_apply`` (in ``adloop.ads.write``) recognises the
``reddit_`` operation prefix and hands the plan to :func:`preflight`
(dry run) or :func:`apply_plan` (real write) here, so nothing in this
module ever needs a Google client.

Dry run is a *read-side preflight*: Reddit has no validate-only mode, so
the dry run re-reads the target entity, confirms it still exists, and
re-runs the budget/bid checks against its live values. Nothing is sent.

Creation always sets ``configured_status = PAUSED``; the user enables the
entity after review, exactly like new Google campaigns and RSAs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from adloop.reddit.client import (
    data_of,
    from_micro,
    reddit_get,
    reddit_patch,
    reddit_post,
    to_micro,
)
from adloop.reddit.read import account_meta, get_reddit_post, resolve_account
from adloop.reddit.schedule import describe_schedule, parse_schedule

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

PLATFORM = "reddit"
OPERATION_PREFIX = "reddit_"

# entity_type → API collection for GET/PATCH by id.
_COLLECTIONS = {"campaign": "campaigns", "ad_group": "ad_groups", "ad": "ads"}

# Reddit rolls out new objective enums on 2026-09-21 and keeps accepting the
# old ones, so unknown values warn instead of failing.
_KNOWN_OBJECTIVES = {
    "APP_INSTALLS",
    "CATALOG_SALES",
    "CLICKS",
    "CONVERSIONS",
    "IMPRESSIONS",
    "LEAD_GENERATION",
    "VIDEO_VIEWABLE_IMPRESSIONS",
}
_CAMPAIGN_BID_STRATEGIES = {"BIDLESS", "MAXIMIZE_VOLUME", "TARGET_CPX"}
_AD_GROUP_BID_STRATEGIES = {"BIDLESS", "MANUAL_BIDDING", "MAXIMIZE_VOLUME", "TARGET_CPX"}
_BID_TYPES = {"CPC", "CPM", "CPV", "CPV6", "CPV15"}
_GOAL_TYPES = {"DAILY_SPEND", "LIFETIME_SPEND"}
_POST_TYPES = {"TEXT", "IMAGE"}
_GENDERS = {"FEMALE", "MALE"}
_PLATFORMS = {"ALL", "DESKTOP", "DESKTOP_LEGACY", "MOBILE_NATIVE", "MOBILE_WEB", "MOBILE_WEB_3X", "SHREDTOP"}
# Reddit calls placements "locations": the feed, or conversation (comments) pages.
_LOCATIONS = {"FEED", "COMMENTS_PAGE"}
# Feed layouts an ad can be placed in (targeting.view_modes).
_VIEW_MODES = {"ALL", "CARD", "CLASSIC", "COMPACT", "IMMERSIVE"}
# Device targets (targeting.devices[]): {type, os, min_version, max_version, label_map}.
_DEVICE_TYPES = {"DESKTOP", "MOBILE"}
_DEVICE_OS = {"ANDROID", "IOS"}
_DEVICE_KEYS = {"type", "os", "min_version", "max_version", "label_map"}
_MIN_IOS_VERSION = 14
_MAX_DEVICES = 100
_MAX_CARRIERS = 100
_CALL_TO_ACTIONS = {
    "Apply Now", "Contact Us", "Download", "Get a Quote", "Get Showtimes",
    "Install", "Learn More", "Order Now", "Play Now", "Pre-order Now",
    "See Menu", "Shop Now", "Sign Up", "View More", "Watch Now", "Book Now",
}
# Reddit truncates titles around 300 characters; headlines beyond that
# are almost always a mistake.
_MAX_HEADLINE_CHARS = 300
_SCHEDULE_NOTE = (
    "Schedule hours apply in each viewer's local time, not the account time "
    "zone, and end_hour is inclusive (23 runs to 23:59). Days and hours outside "
    "the schedule get no delivery."
)
_MAX_TEXT_BODY_CHARS = 40_000


class _Invalid(Exception):
    """Collected validation errors → ``{"error": "Validation failed", "details": [...]}``."""

    def __init__(self, details: list[str]):
        super().__init__("Validation failed")
        self.details = details


def _validation_error(details: list[str]) -> dict:
    return {"error": "Validation failed", "details": details}


def _iso_time(value: str, field: str, errors: list[str]) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    candidate = value
    if len(candidate) == 10:  # bare date → start of day UTC
        candidate = f"{candidate}T00:00:00Z"
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field} must be ISO 8601 (e.g. 2026-10-01T00:00:00Z), got '{value}'")
        return ""
    return candidate


def _days_between(start: str, end: str) -> int:
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else datetime.now(timezone.utc)
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 1
    return max(int((e - s).total_seconds() // 86400), 1)


def _daily_equivalent(
    *, daily_budget: float | None, lifetime_budget: float | None, start_time: str, end_time: str,
    errors: list[str],
) -> tuple[str, float | None, float | None]:
    """Resolve the budget the user gave into (goal_type, goal_value, daily-equivalent).

    Lifetime budgets are compared to the safety cap by their per-day share
    across the schedule, which is why they require an end time.
    """
    if daily_budget is not None and lifetime_budget is not None:
        errors.append("Pass either daily_budget or lifetime_budget, not both.")
        return "", None, None
    if daily_budget is not None:
        if daily_budget <= 0:
            errors.append("daily_budget must be positive.")
            return "", None, None
        return "DAILY_SPEND", float(daily_budget), float(daily_budget)
    if lifetime_budget is not None:
        if lifetime_budget <= 0:
            errors.append("lifetime_budget must be positive.")
            return "", None, None
        if not end_time:
            errors.append("lifetime_budget requires end_time (Reddit needs a schedule to pace it).")
            return "", None, None
        days = _days_between(start_time, end_time)
        return "LIFETIME_SPEND", float(lifetime_budget), round(float(lifetime_budget) / days, 2)
    return "", None, None


def _guard(operation: str, config: AdLoopConfig) -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def _budget_cap(daily_equivalent: float | None, config: AdLoopConfig, currency: str) -> None:
    if daily_equivalent is None:
        return
    from adloop.safety.guards import SafetyViolation, check_budget_cap

    try:
        check_budget_cap(daily_equivalent, config.safety)
    except SafetyViolation as e:
        raise SafetyViolation(
            f"{e} (cap is compared as a plain number; this Reddit account bills in {currency})"
        ) from e


def _store(plan_kwargs: dict, *, warnings: list[str] | None = None) -> dict:
    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(platform=PLATFORM, **plan_kwargs)
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _fetch(config: AdLoopConfig, entity_type: str, entity_id: str) -> dict:
    collection = _COLLECTIONS[entity_type]
    return data_of(reddit_get(config, f"{collection}/{entity_id}"))


def _require_scope_for_writes(config: AdLoopConfig) -> None:
    """Fail early with a useful message when adsedit was never granted."""
    from adloop.auth import get_reddit_credentials

    creds = get_reddit_credentials(config)
    granted = getattr(creds, "granted_scopes", None)
    if granted is not None and "adsedit" not in granted:
        from adloop.reddit.auth import RedditAuthError

        raise RedditAuthError(
            "The Reddit connection was granted read access only (adsread). "
            "Reconnect Reddit Ads and approve write access (adsedit) to "
            "change campaigns.",
            error_code="insufficient_scope",
            status=403,
        )


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------


def _draft_status(
    config: AdLoopConfig,
    *,
    operation: str,
    ad_account_id: str,
    entity_type: str,
    entity_id: str,
    target_status: str,
    double_confirm: bool = False,
) -> dict:
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    entity_type = (entity_type or "").strip().lower()
    if entity_type not in _COLLECTIONS:
        errors.append(f"entity_type must be one of {sorted(_COLLECTIONS)}, got '{entity_type}'")
    entity_id = (entity_id or "").strip()
    if not entity_id:
        errors.append("entity_id is required")
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    if errors:
        return _validation_error(errors)

    current = _fetch(config, entity_type, entity_id)
    if not current:
        return {"error": f"Reddit {entity_type} '{entity_id}' was not found."}
    warnings: list[str] = []
    if current.get("configured_status") == target_status:
        warnings.append(
            f"{entity_type} '{current.get('name')}' already has configured_status "
            f"{target_status}; applying is a no-op."
        )
    if target_status == "ACTIVE" and current.get("effective_status") in (
        "REJECTED", "PENDING_BILLING_INFO", "PENDING_ID_VERIFICATION",
    ):
        warnings.append(
            f"effective_status is {current.get('effective_status')}: enabling will "
            "not make it serve until that is resolved."
        )
    return _store(
        {
            "operation": operation,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "customer_id": account,
            "requires_double_confirm": double_confirm,
            "changes": {
                "ad_account_id": account,
                "name": current.get("name"),
                "current_status": current.get("configured_status"),
                "effective_status": current.get("effective_status"),
                "target_status": target_status,
            },
        },
        warnings=warnings,
    )


def pause_reddit_entity(config, *, ad_account_id="", entity_type="", entity_id="") -> dict:
    return _draft_status(
        config, operation="reddit_set_status", ad_account_id=ad_account_id,
        entity_type=entity_type, entity_id=entity_id, target_status="PAUSED",
    )


def enable_reddit_entity(config, *, ad_account_id="", entity_type="", entity_id="") -> dict:
    return _draft_status(
        config, operation="reddit_set_status", ad_account_id=ad_account_id,
        entity_type=entity_type, entity_id=entity_id, target_status="ACTIVE",
    )


def remove_reddit_entity(config, *, ad_account_id="", entity_type="", entity_id="") -> dict:
    """Archive an entity. Reddit has no hard delete for entities that ran;
    ARCHIVED is the irreversible state, so it gets the double confirmation."""
    return _draft_status(
        config, operation="reddit_archive_entity", ad_account_id=ad_account_id,
        entity_type=entity_type, entity_id=entity_id, target_status="ARCHIVED",
        double_confirm=True,
    )


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


def update_reddit_campaign(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    name: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    spend_cap: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    bid_value: float | None = None,
    start_time: str = "",
    end_time: str = "",
    schedule: list | None = None,
) -> dict:
    """Draft campaign setting changes. Budget/bid/schedule fields only apply to CBO campaigns."""
    blocked = _guard("update_reddit_campaign", config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation

    errors: list[str] = []
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    start_time = _iso_time(start_time, "start_time", errors)
    end_time = _iso_time(end_time, "end_time", errors)
    bid_strategy = (bid_strategy or "").strip().upper()
    bid_type = (bid_type or "").strip().upper()
    if bid_strategy and bid_strategy not in _CAMPAIGN_BID_STRATEGIES:
        errors.append(f"bid_strategy must be one of {sorted(_CAMPAIGN_BID_STRATEGIES)}")
    if bid_type and bid_type not in _BID_TYPES:
        errors.append(f"bid_type must be one of {sorted(_BID_TYPES)}")
    schedule_blocks = parse_schedule(schedule, errors)
    if errors:
        return _validation_error(errors)

    current = _fetch(config, "campaign", campaign_id)
    if not current:
        return {"error": f"Reddit campaign '{campaign_id}' was not found."}
    meta = account_meta(config, account)
    is_cbo = bool(current.get("is_campaign_budget_optimization"))
    if schedule_blocks is not None and not is_cbo:
        errors.append(
            "A campaign schedule only exists with campaign budget optimization; "
            "set the schedule on each ad group with update_reddit_ad_group."
        )

    goal_type, goal_value, daily_equivalent = _daily_equivalent(
        daily_budget=daily_budget, lifetime_budget=lifetime_budget,
        start_time=start_time or str(current.get("start_time") or ""),
        end_time=end_time or str(current.get("end_time") or ""), errors=errors,
    )
    if goal_value is not None and not is_cbo:
        errors.append(
            "This campaign does not use campaign budget optimization; its budget "
            "lives on the ad groups. Use update_reddit_ad_group instead."
        )
    if (bid_strategy or bid_type or bid_value is not None) and not is_cbo:
        errors.append("Bid settings live on the ad groups of a non-CBO campaign.")
    if errors:
        return _validation_error(errors)

    patch: dict[str, Any] = {}
    display: dict[str, Any] = {}
    if name and name != current.get("name"):
        patch["name"] = name
        display["name"] = {"from": current.get("name"), "to": name}
    if goal_value is not None:
        current_goal = from_micro(current.get("goal_value"))
        if goal_type != current.get("goal_type") and current.get("goal_type"):
            errors.append(
                f"goal_type cannot change after publishing (campaign is {current.get('goal_type')})."
            )
        patch["goal_value"] = to_micro(goal_value)
        display["budget"] = {
            "from": current_goal, "to": goal_value, "goal_type": goal_type, "currency": meta["currency"],
        }
        try:
            _budget_cap(daily_equivalent, config, meta["currency"])
        except SafetyViolation as e:
            return {"error": str(e)}
    if spend_cap is not None:
        patch["spend_cap"] = to_micro(spend_cap) if spend_cap > 0 else None
        display["spend_cap"] = {"from": from_micro(current.get("spend_cap")), "to": spend_cap or None}
    if bid_strategy:
        patch["bid_strategy"] = bid_strategy
        display["bid_strategy"] = {"from": current.get("bid_strategy"), "to": bid_strategy}
    if bid_type:
        patch["bid_type"] = bid_type
        display["bid_type"] = {"from": current.get("bid_type"), "to": bid_type}
    if bid_value is not None:
        current_bid = from_micro(current.get("bid_value")) or 0.0
        from adloop.safety.guards import check_bid_increase

        try:
            check_bid_increase(current_bid, float(bid_value), config.safety)
        except SafetyViolation as e:
            return {"error": str(e)}
        patch["bid_value"] = to_micro(bid_value)
        display["bid_value"] = {"from": current_bid, "to": bid_value}
    if start_time:
        patch["start_time"] = start_time
        display["start_time"] = {"from": current.get("start_time"), "to": start_time}
    if end_time:
        patch["end_time"] = end_time
        display["end_time"] = {"from": current.get("end_time"), "to": end_time}
    if schedule_blocks is not None:
        patch["schedule"] = schedule_blocks
        display["schedule"] = {
            "from": describe_schedule(current.get("schedule")),
            "to": describe_schedule(schedule_blocks),
        }
    if errors:
        return _validation_error(errors)
    if not patch:
        return _validation_error(["Nothing to change — pass at least one field."])

    warnings: list[str] = []
    if "goal_value" in patch and display["budget"]["from"]:
        from adloop.safety.guards import requires_double_confirmation

        if requires_double_confirmation(
            "update", current_budget=display["budget"]["from"], proposed_budget=goal_value
        ):
            warnings.append("Budget increase exceeds 50% — confirm the new amount with the user.")
    if schedule_blocks is not None:
        warnings.append(_SCHEDULE_NOTE + " A campaign schedule replaces every ad group's.")
    return _store(
        {
            "operation": "reddit_update_campaign",
            "entity_type": "campaign",
            "entity_id": campaign_id,
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "campaign_name": current.get("name"),
                "is_campaign_budget_optimization": is_cbo,
                "patch": patch,
                "display": display,
                "daily_equivalent": daily_equivalent,
            },
        },
        warnings=warnings,
    )


def _targeting_payload(
    *,
    geolocations: list | None,
    excluded_geolocations: list | None,
    communities: list | None,
    excluded_communities: list | None,
    interests: list | None,
    keywords: list | None,
    excluded_keywords: list | None,
    languages: list | None,
    gender: str,
    platforms: list | None,
    expand_targeting: bool | None,
    errors: list[str],
    locations: list | None = None,
    excluded_interests: list | None = None,
    devices: list | None = None,
    carriers: list | None = None,
    view_modes: list | None = None,
) -> dict:
    targeting: dict[str, Any] = {}

    def _clean(items: list | None) -> list[str]:
        return [str(x).strip() for x in (items or []) if str(x).strip()]

    if geolocations is not None:
        targeting["geolocations"] = _clean(geolocations)
    if excluded_geolocations is not None:
        targeting["excluded_geolocations"] = _clean(excluded_geolocations)
    if communities is not None:
        targeting["communities"] = _clean(communities)
    if excluded_communities is not None:
        targeting["excluded_communities"] = _clean(excluded_communities)
    if interests is not None:
        targeting["interests"] = _clean(interests)
    if keywords is not None:
        targeting["keywords"] = _clean(keywords)
    if excluded_keywords is not None:
        targeting["excluded_keywords"] = _clean(excluded_keywords)
    if languages is not None:
        # Reddit validates against upper-case ISO 639-1 codes ('EN', 'DE').
        targeting["languages"] = [x.upper() for x in _clean(languages)]
    if gender:
        gender = gender.strip().upper()
        if gender not in _GENDERS:
            errors.append(f"gender must be one of {sorted(_GENDERS)} (or empty for all)")
        targeting["gender"] = gender
    if platforms is not None:
        cleaned = [x.upper() for x in _clean(platforms)]
        bad = sorted(set(cleaned) - _PLATFORMS)
        if bad:
            errors.append(f"platforms contains unsupported values {bad}; use {sorted(_PLATFORMS)}")
        targeting["platforms"] = cleaned
    if expand_targeting is not None:
        targeting["expand_targeting"] = bool(expand_targeting)
    if locations is not None:
        cleaned = [x.upper() for x in _clean(locations)]
        bad = sorted(set(cleaned) - _LOCATIONS)
        if bad:
            errors.append(f"locations contains unsupported values {bad}; use {sorted(_LOCATIONS)}")
        if not cleaned:
            errors.append("locations cannot be empty: an ad needs at least one placement (FEED, COMMENTS_PAGE)")
        targeting["locations"] = cleaned
    if excluded_interests is not None:
        targeting["excluded_interests"] = _clean(excluded_interests)
    if devices is not None:
        targeting["devices"] = normalize_devices(devices, errors)
    if carriers is not None:
        cleaned = [x.upper() for x in _clean(carriers)]
        if len(cleaned) > _MAX_CARRIERS:
            errors.append(f"carriers takes at most {_MAX_CARRIERS} entries")
        targeting["carriers"] = cleaned
    if view_modes is not None:
        cleaned = [x.upper() for x in _clean(view_modes)]
        bad = sorted(set(cleaned) - _VIEW_MODES)
        if bad:
            errors.append(f"view_modes contains unsupported values {bad}; use {sorted(_VIEW_MODES)}")
        targeting["view_modes"] = cleaned
    return targeting


_EXCLUDED_INTERESTS_NOTE = (
    "Reddit marks targeting.excluded_interests as deprecated: it is still "
    "accepted but may stop having an effect. Excluded communities and "
    "excluded keywords are the supported exclusions."
)


def _targeting_notes(config: AdLoopConfig, targeting: dict, errors: list[str], warnings: list[str]) -> None:
    """Checks for the device/carrier/interest-exclusion keys shared by draft and update.

    Carrier ids are checked against Reddit's carrier list (Reddit itself only
    validates targeting at apply time); a lookup failure warns instead of
    blocking.
    """
    if targeting.get("excluded_interests"):
        warnings.append(_EXCLUDED_INTERESTS_NOTE)
    carriers = targeting.get("carriers") or []
    if carriers:
        from adloop.reddit.read import validate_reddit_carriers

        try:
            unknown = validate_reddit_carriers(config, carriers)
        except Exception as exc:  # taxonomy endpoint down: warn, don't block
            warnings.append(
                f"Carriers could not be pre-validated with Reddit ({exc}); the apply may still refuse them."
            )
        else:
            if unknown:
                errors.append(
                    f"Unknown carrier ids: {', '.join(unknown)} (use ids from search_reddit_targeting kind \"carriers\")"
                )


def normalize_devices(devices: list | None, errors: list[str]) -> list[dict]:
    """Validate device targets and normalise enums (upper case) and versions (strings).

    Each entry is ``{"type": "DESKTOP"|"MOBILE", "os": "ANDROID"|"IOS",
    "min_version": "14", "max_version": "17", "label_map": {make: [models]}}``;
    only ``type`` is required. Problems are appended to ``errors``.
    """
    out: list[dict] = []
    items = list(devices or [])
    if len(items) > _MAX_DEVICES:
        errors.append(f"devices takes at most {_MAX_DEVICES} entries")
    for i, item in enumerate(items):
        label = f"devices[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object like {{\"type\": \"MOBILE\", \"os\": \"IOS\"}}")
            continue
        unknown = sorted(set(item) - _DEVICE_KEYS)
        if unknown:
            errors.append(f"{label} has unsupported keys {unknown}; use {sorted(_DEVICE_KEYS)}")
        device: dict[str, Any] = {}
        dtype = str(item.get("type") or "").strip().upper()
        if dtype not in _DEVICE_TYPES:
            errors.append(f"{label}.type must be one of {sorted(_DEVICE_TYPES)}")
        device["type"] = dtype
        os_name = str(item.get("os") or "").strip().upper()
        if os_name:
            if os_name not in _DEVICE_OS:
                errors.append(f"{label}.os must be one of {sorted(_DEVICE_OS)} (or omitted for any)")
            device["os"] = os_name
        versions: dict[str, int] = {}
        for key in ("min_version", "max_version"):
            raw = item.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            text = str(raw).strip()
            if not text.isdigit():
                errors.append(f"{label}.{key} must be a major OS version number like \"14\"")
                continue
            versions[key] = int(text)
            device[key] = text
        if os_name == "IOS" and versions.get("min_version", _MIN_IOS_VERSION) < _MIN_IOS_VERSION:
            errors.append(f"{label}.min_version must be at least {_MIN_IOS_VERSION} for iOS devices")
        if "min_version" in versions and "max_version" in versions and versions["min_version"] > versions["max_version"]:
            errors.append(f"{label}.min_version cannot be above max_version")
        label_map = item.get("label_map")
        if label_map is not None:
            if not isinstance(label_map, dict) or not all(
                isinstance(models, list) for models in label_map.values()
            ):
                errors.append(
                    f"{label}.label_map must map a make to a list of models, e.g. "
                    f"{{\"Samsung\": [\"Galaxy S10+\"]}} (makes/models from search_reddit_targeting kind \"devices\")"
                )
            else:
                device["label_map"] = {
                    str(make).strip(): [str(m).strip() for m in models if str(m).strip()]
                    for make, models in label_map.items()
                    if str(make).strip()
                }
        out.append(device)
    return out


def update_reddit_ad_group(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_group_id: str = "",
    name: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    bid_value: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    start_time: str = "",
    end_time: str = "",
    geolocations: list | None = None,
    excluded_geolocations: list | None = None,
    communities: list | None = None,
    excluded_communities: list | None = None,
    interests: list | None = None,
    keywords: list | None = None,
    excluded_keywords: list | None = None,
    languages: list | None = None,
    gender: str = "",
    platforms: list | None = None,
    expand_targeting: bool | None = None,
    schedule: list | None = None,
    locations: list | None = None,
    excluded_interests: list | None = None,
    devices: list | None = None,
    carriers: list | None = None,
    view_modes: list | None = None,
) -> dict:
    """Draft ad group changes: budget, bid, run dates, weekly schedule, targeting (lists REPLACE)."""
    blocked = _guard("update_reddit_ad_group", config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation, check_bid_increase

    errors: list[str] = []
    ad_group_id = (ad_group_id or "").strip()
    if not ad_group_id:
        errors.append("ad_group_id is required")
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    start_time = _iso_time(start_time, "start_time", errors)
    end_time = _iso_time(end_time, "end_time", errors)
    bid_strategy = (bid_strategy or "").strip().upper()
    bid_type = (bid_type or "").strip().upper()
    if bid_strategy and bid_strategy not in _AD_GROUP_BID_STRATEGIES:
        errors.append(f"bid_strategy must be one of {sorted(_AD_GROUP_BID_STRATEGIES)}")
    if bid_type and bid_type not in _BID_TYPES:
        errors.append(f"bid_type must be one of {sorted(_BID_TYPES)}")
    schedule_blocks = parse_schedule(schedule, errors)
    targeting = _targeting_payload(
        geolocations=geolocations, excluded_geolocations=excluded_geolocations,
        communities=communities, excluded_communities=excluded_communities,
        interests=interests, keywords=keywords, excluded_keywords=excluded_keywords,
        languages=languages, gender=gender, platforms=platforms,
        expand_targeting=expand_targeting, errors=errors, locations=locations,
        excluded_interests=excluded_interests, devices=devices, carriers=carriers,
        view_modes=view_modes,
    )
    if errors:
        return _validation_error(errors)
    targeting_warnings: list[str] = []
    _targeting_notes(config, targeting, errors, targeting_warnings)
    if errors:
        return _validation_error(errors)

    current = _fetch(config, "ad_group", ad_group_id)
    if not current:
        return {"error": f"Reddit ad group '{ad_group_id}' was not found."}
    meta = account_meta(config, account)
    is_cbo = bool(current.get("is_campaign_budget_optimization"))

    goal_type, goal_value, daily_equivalent = _daily_equivalent(
        daily_budget=daily_budget, lifetime_budget=lifetime_budget,
        start_time=start_time or str(current.get("start_time") or ""),
        end_time=end_time or str(current.get("end_time") or ""), errors=errors,
    )
    if goal_value is not None and is_cbo:
        errors.append(
            "This ad group belongs to a campaign-budget-optimization campaign; "
            "set the budget with update_reddit_campaign instead."
        )
    if schedule_blocks is not None and is_cbo:
        errors.append(
            "This ad group belongs to a campaign-budget-optimization campaign, "
            "whose schedule overrides every ad group; set it with update_reddit_campaign."
        )
    if errors:
        return _validation_error(errors)

    patch: dict[str, Any] = {}
    display: dict[str, Any] = {}
    if name and name != current.get("name"):
        patch["name"] = name
        display["name"] = {"from": current.get("name"), "to": name}
    if goal_value is not None:
        if current.get("goal_type") and goal_type != current.get("goal_type"):
            errors.append(
                f"goal_type cannot change after publishing (ad group is {current.get('goal_type')})."
            )
        patch["goal_value"] = to_micro(goal_value)
        display["budget"] = {
            "from": from_micro(current.get("goal_value")), "to": goal_value,
            "goal_type": goal_type, "currency": meta["currency"],
        }
        try:
            _budget_cap(daily_equivalent, config, meta["currency"])
        except SafetyViolation as e:
            return {"error": str(e)}
    if bid_value is not None:
        current_bid = from_micro(current.get("bid_value")) or 0.0
        try:
            check_bid_increase(current_bid, float(bid_value), config.safety)
        except SafetyViolation as e:
            return {"error": str(e)}
        patch["bid_value"] = to_micro(bid_value)
        display["bid_value"] = {"from": current_bid, "to": bid_value, "currency": meta["currency"]}
    if bid_strategy:
        patch["bid_strategy"] = bid_strategy
        display["bid_strategy"] = {"from": current.get("bid_strategy"), "to": bid_strategy}
    if bid_type:
        patch["bid_type"] = bid_type
        display["bid_type"] = {"from": current.get("bid_type"), "to": bid_type}
    if start_time:
        patch["start_time"] = start_time
        display["start_time"] = {"from": current.get("start_time"), "to": start_time}
    if end_time:
        patch["end_time"] = end_time
        display["end_time"] = {"from": current.get("end_time"), "to": end_time}
    if schedule_blocks is not None:
        patch["schedule"] = schedule_blocks
        display["schedule"] = {
            "from": describe_schedule(current.get("schedule")),
            "to": describe_schedule(schedule_blocks),
        }
    if targeting:
        merged = dict(current.get("targeting") or {})
        merged.update(targeting)
        patch["targeting"] = merged
        display["targeting"] = {
            key: {"from": (current.get("targeting") or {}).get(key), "to": value}
            for key, value in targeting.items()
        }
    if errors:
        return _validation_error(errors)
    if not patch:
        return _validation_error(["Nothing to change — pass at least one field."])

    warnings: list[str] = list(targeting_warnings)
    if "goal_value" in patch and display["budget"]["from"]:
        from adloop.safety.guards import requires_double_confirmation

        if requires_double_confirmation(
            "update", current_budget=display["budget"]["from"], proposed_budget=goal_value
        ):
            warnings.append("Budget increase exceeds 50% — confirm the new amount with the user.")
    if targeting:
        warnings.append(
            "Targeting lists REPLACE the current values for the keys you passed "
            "(other keys are preserved). Pass the full desired list."
        )
    if schedule_blocks is not None:
        warnings.append(_SCHEDULE_NOTE)
    return _store(
        {
            "operation": "reddit_update_ad_group",
            "entity_type": "ad_group",
            "entity_id": ad_group_id,
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "ad_group_name": current.get("name"),
                "campaign_id": current.get("campaign_id"),
                "is_campaign_budget_optimization": is_cbo,
                "patch": patch,
                "display": display,
                "daily_equivalent": daily_equivalent,
            },
        },
        warnings=warnings,
    )


def update_reddit_ad(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_id: str = "",
    name: str = "",
    click_url: str = "",
    allow_comments: bool | None = None,
) -> dict:
    """Draft ad changes: name, landing URL, comments on/off. Copy cannot change
    (a live Reddit post's title and body are immutable); draft a new ad for that."""
    blocked = _guard("update_reddit_ad", config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    ad_id = (ad_id or "").strip()
    if not ad_id:
        errors.append("ad_id is required (see get_reddit_ads)")
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    click_url = (click_url or "").strip()
    if click_url and not click_url.startswith(("http://", "https://")):
        errors.append("click_url must start with http:// or https://")
    if errors:
        return _validation_error(errors)
    if click_url:
        from adloop.ads.write import _validate_urls

        url_errors, url_warnings = _validate_urls([click_url])
        for url, problem in url_errors.items():
            if problem:
                errors.append(f"'{url}' is not reachable: {problem}")
        warnings.extend(url_warnings.values())
        if errors:
            return _validation_error(errors)

    current = _fetch(config, "ad", ad_id)
    if not current:
        return {"error": f"Reddit ad '{ad_id}' was not found."}
    if click_url and current.get("post_id"):
        # Text ("free form") ads open the post; Reddit refuses a click_url on
        # them at apply time ("Free form ads cannot have a click url"), and
        # the dry run cannot catch it because it sends nothing. Ask now.
        post = get_reddit_post(config, str(current["post_id"]))
        if post and str(post.get("type") or "").upper() == "TEXT":
            return _validation_error(
                [
                    "This is a TEXT (free-form) ad: it opens the post itself, and Reddit "
                    "refuses a click_url on it. The link readers follow is in the post body, "
                    "which cannot be edited; for a new link, draft a new post with draft_reddit_ad."
                ]
            )

    patch: dict[str, Any] = {}
    display: dict[str, Any] = {}
    if name and name != current.get("name"):
        patch["name"] = name
        display["name"] = {"from": current.get("name"), "to": name}
    if click_url and click_url != current.get("click_url"):
        patch["click_url"] = click_url
        display["click_url"] = {"from": current.get("click_url"), "to": click_url}
    post_patch: dict[str, Any] = {}
    if allow_comments is not None:
        if not current.get("post_id"):
            errors.append("This ad has no post to toggle comments on.")
        else:
            post_patch["allow_comments"] = bool(allow_comments)
            display["allow_comments"] = {"to": bool(allow_comments)}
    if errors:
        return _validation_error(errors)
    if not patch and not post_patch:
        return _validation_error(
            ["Nothing to change — pass name, click_url and/or allow_comments. "
             "Headline and body of a live Reddit post cannot be edited: use draft_reddit_ad for new copy."]
        )
    if "click_url" in patch:
        warnings.append("Changing the landing URL resets nothing on Reddit's side, but check UTM parameters match your analytics.")
    return _store(
        {
            "operation": "reddit_update_ad",
            "entity_type": "ad",
            "entity_id": ad_id,
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "ad_name": current.get("name"),
                "post_id": current.get("post_id"),
                "patch": patch,
                "post_patch": post_patch,
                "display": display,
            },
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def draft_reddit_campaign(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_name: str = "",
    objective: str = "",
    funding_instrument_id: str = "",
    campaign_budget_optimization: bool = False,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    bid_value: float | None = None,
    optimization_goal: str = "",
    conversion_pixel_id: str = "",
    spend_cap: float | None = None,
    start_time: str = "",
    end_time: str = "",
    schedule: list | None = None,
) -> dict:
    """Draft a new campaign (created PAUSED). Budget and schedule live on ad groups unless CBO."""
    blocked = _guard("draft_reddit_campaign", config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation

    errors: list[str] = []
    warnings: list[str] = []
    schedule_blocks = parse_schedule(schedule, errors)
    if schedule_blocks and not campaign_budget_optimization:
        errors.append(
            "A campaign schedule only exists with campaign_budget_optimization=true; "
            "otherwise set the schedule on each ad group (draft_reddit_ad_group)."
        )
    campaign_name = (campaign_name or "").strip()
    if not campaign_name:
        errors.append("campaign_name is required")
    objective = (objective or "").strip().upper()
    if not objective:
        errors.append(
            "objective is required: CLICKS (traffic), CONVERSIONS (needs a pixel), "
            "IMPRESSIONS (awareness), LEAD_GENERATION, APP_INSTALLS, CATALOG_SALES, "
            "VIDEO_VIEWABLE_IMPRESSIONS"
        )
    elif objective not in _KNOWN_OBJECTIVES:
        warnings.append(
            f"objective '{objective}' is not in AdLoop's known list "
            f"{sorted(_KNOWN_OBJECTIVES)}; Reddit may reject it (new enums roll out 2026-09-21)."
        )
    funding_instrument_id = (funding_instrument_id or "").strip()
    if not funding_instrument_id:
        errors.append(
            "funding_instrument_id is required — take it from list_reddit_funding_instruments"
        )
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    start_time = _iso_time(start_time, "start_time", errors)
    end_time = _iso_time(end_time, "end_time", errors)
    bid_strategy = (bid_strategy or "").strip().upper()
    bid_type = (bid_type or "").strip().upper()
    optimization_goal = (optimization_goal or "").strip().upper()
    conversion_pixel_id = (conversion_pixel_id or "").strip()

    goal_type, goal_value, daily_equivalent = _daily_equivalent(
        daily_budget=daily_budget, lifetime_budget=lifetime_budget,
        start_time=start_time, end_time=end_time, errors=errors,
    )
    if campaign_budget_optimization:
        if goal_value is None:
            errors.append("campaign_budget_optimization needs daily_budget or lifetime_budget")
        if not bid_strategy:
            errors.append(f"CBO campaigns need bid_strategy ({sorted(_CAMPAIGN_BID_STRATEGIES)})")
        elif bid_strategy not in _CAMPAIGN_BID_STRATEGIES:
            errors.append(f"bid_strategy must be one of {sorted(_CAMPAIGN_BID_STRATEGIES)}")
        if not bid_type:
            errors.append(f"CBO campaigns need bid_type ({sorted(_BID_TYPES)})")
        elif bid_type not in _BID_TYPES:
            errors.append(f"bid_type must be one of {sorted(_BID_TYPES)}")
        if not conversion_pixel_id:
            errors.append(
                "CBO campaigns require conversion_pixel_id (Reddit rule since 2026-07-13); "
                "find it with get_reddit_pixels"
            )
    else:
        if goal_value is not None or bid_strategy or bid_type or bid_value is not None:
            errors.append(
                "Budget and bid settings on a non-CBO campaign belong to its ad groups: "
                "leave them off here and pass them to draft_reddit_ad_group, or set "
                "campaign_budget_optimization=true."
            )
    if errors:
        return _validation_error(errors)

    meta = account_meta(config, account)
    if daily_equivalent is not None:
        try:
            _budget_cap(daily_equivalent, config, meta["currency"])
        except SafetyViolation as e:
            return {"error": str(e)}

    payload: dict[str, Any] = {
        "name": campaign_name,
        "configured_status": "PAUSED",
        "objective": objective,
        "funding_instrument_id": funding_instrument_id,
    }
    if campaign_budget_optimization:
        payload.update(
            {
                "is_campaign_budget_optimization": True,
                "goal_type": goal_type,
                "goal_value": to_micro(goal_value),
                "bid_strategy": bid_strategy,
                "bid_type": bid_type,
                "conversion_pixel_id": conversion_pixel_id,
            }
        )
        if bid_value is not None:
            payload["bid_value"] = to_micro(bid_value)
        if optimization_goal:
            payload["optimization_goal"] = optimization_goal
    else:
        payload["is_campaign_budget_optimization"] = False
        if conversion_pixel_id:
            payload["conversion_pixel_id"] = conversion_pixel_id
    if spend_cap is not None and spend_cap > 0:
        payload["spend_cap"] = to_micro(spend_cap)
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if schedule_blocks:
        payload["schedule"] = schedule_blocks
        warnings.append(_SCHEDULE_NOTE + " A campaign schedule replaces every ad group's.")

    if objective == "CONVERSIONS":
        warnings.append(
            "CONVERSIONS campaigns only learn from a firing pixel. Check "
            "get_reddit_pixels: the optimization event must have fired recently."
        )
    warnings.append("The campaign is created PAUSED; enable it after ad groups and ads exist.")
    return _store(
        {
            "operation": "reddit_create_campaign",
            "entity_type": "campaign",
            "entity_id": "",
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "payload": payload,
                "display": {
                    "campaign_name": campaign_name,
                    "objective": objective,
                    "campaign_budget_optimization": campaign_budget_optimization,
                    "budget": goal_value,
                    "goal_type": goal_type or None,
                    "currency": meta["currency"],
                    "status_on_create": "PAUSED",
                },
                "daily_equivalent": daily_equivalent,
            },
        },
        warnings=warnings,
    )


def draft_reddit_ad_group(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    ad_group_name: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    bid_value: float | None = None,
    optimization_goal: str = "",
    conversion_pixel_id: str = "",
    geolocations: list | None = None,
    excluded_geolocations: list | None = None,
    communities: list | None = None,
    excluded_communities: list | None = None,
    interests: list | None = None,
    keywords: list | None = None,
    excluded_keywords: list | None = None,
    languages: list | None = None,
    gender: str = "",
    platforms: list | None = None,
    expand_targeting: bool | None = None,
    start_time: str = "",
    end_time: str = "",
    schedule: list | None = None,
    locations: list | None = None,
    excluded_interests: list | None = None,
    devices: list | None = None,
    carriers: list | None = None,
    view_modes: list | None = None,
) -> dict:
    """Draft a new ad group (created PAUSED) with budget, bid, pixel, schedule and targeting."""
    blocked = _guard("draft_reddit_ad_group", config)
    if blocked:
        return blocked
    from adloop.safety.guards import SafetyViolation

    errors: list[str] = []
    warnings: list[str] = []
    schedule_blocks = parse_schedule(schedule, errors)
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required (see get_reddit_campaigns)")
    ad_group_name = (ad_group_name or "").strip()
    if not ad_group_name:
        errors.append("ad_group_name is required")
    conversion_pixel_id = (conversion_pixel_id or "").strip()
    if not conversion_pixel_id:
        errors.append(
            "conversion_pixel_id is required on every ad group (Reddit rule since "
            "2026-07-13); find it with get_reddit_pixels"
        )
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    start_time = _iso_time(start_time, "start_time", errors)
    end_time = _iso_time(end_time, "end_time", errors)
    bid_strategy = (bid_strategy or "").strip().upper()
    bid_type = (bid_type or "").strip().upper()
    optimization_goal = (optimization_goal or "").strip().upper()
    if bid_strategy and bid_strategy not in _AD_GROUP_BID_STRATEGIES:
        errors.append(f"bid_strategy must be one of {sorted(_AD_GROUP_BID_STRATEGIES)}")
    if bid_type and bid_type not in _BID_TYPES:
        errors.append(f"bid_type must be one of {sorted(_BID_TYPES)}")
    if bid_strategy == "MANUAL_BIDDING" and bid_value is None:
        errors.append("MANUAL_BIDDING needs bid_value")
    if bid_strategy == "TARGET_CPX" and bid_value is None:
        errors.append("TARGET_CPX needs bid_value (the target cost per result)")
    targeting = _targeting_payload(
        geolocations=geolocations, excluded_geolocations=excluded_geolocations,
        communities=communities, excluded_communities=excluded_communities,
        interests=interests, keywords=keywords, excluded_keywords=excluded_keywords,
        languages=languages, gender=gender, platforms=platforms,
        expand_targeting=expand_targeting, errors=errors, locations=locations,
        excluded_interests=excluded_interests, devices=devices, carriers=carriers,
        view_modes=view_modes,
    )
    if not any(targeting.get(k) for k in ("geolocations", "communities", "interests", "keywords")):
        errors.append(
            "Targeting is required: pass at least one of geolocations, communities, "
            "interests or keywords (ids/names from search_reddit_targeting). "
            "Untargeted ad groups waste budget."
        )
    if errors:
        return _validation_error(errors)

    campaign = _fetch(config, "campaign", campaign_id)
    if not campaign:
        return {"error": f"Reddit campaign '{campaign_id}' was not found."}
    is_cbo = bool(campaign.get("is_campaign_budget_optimization"))
    meta = account_meta(config, account)

    goal_type, goal_value, daily_equivalent = _daily_equivalent(
        daily_budget=daily_budget, lifetime_budget=lifetime_budget,
        start_time=start_time, end_time=end_time, errors=errors,
    )
    if is_cbo:
        if goal_value is not None:
            errors.append(
                "The campaign uses campaign budget optimization; the budget is set "
                "on the campaign, not the ad group."
            )
        if schedule_blocks:
            errors.append(
                "The campaign uses campaign budget optimization; its schedule "
                "overrides every ad group. Set it with update_reddit_campaign."
            )
        if bid_strategy and bid_strategy != campaign.get("bid_strategy"):
            errors.append(
                f"CBO ad groups must match the campaign bid_strategy ({campaign.get('bid_strategy')})."
            )
    else:
        if goal_value is None:
            errors.append("daily_budget (or lifetime_budget + end_time) is required")
        if not bid_strategy:
            errors.append(f"bid_strategy is required ({sorted(_AD_GROUP_BID_STRATEGIES)})")
        if not bid_type:
            errors.append(f"bid_type is required ({sorted(_BID_TYPES)})")
    if errors:
        return _validation_error(errors)
    if daily_equivalent is not None:
        try:
            _budget_cap(daily_equivalent, config, meta["currency"])
        except SafetyViolation as e:
            return {"error": str(e)}

    # Reddit validates targeting only at apply time; ask it now so the
    # preview already reflects what would be refused.
    from adloop.reddit.read import validate_reddit_geolocations, validate_reddit_keywords

    try:
        unsafe = validate_reddit_keywords(
            config, list(targeting.get("keywords") or []) + list(targeting.get("excluded_keywords") or [])
        )
        if unsafe:
            errors.append(
                f"Reddit refuses these keywords as not brand-safe: {', '.join(unsafe)}. Remove them."
            )
        bad_geos = validate_reddit_geolocations(
            config, list(targeting.get("geolocations") or []) + list(targeting.get("excluded_geolocations") or [])
        )
        for geo_id, problem in bad_geos.items():
            errors.append(f"geolocation '{geo_id}': {problem} (use ids from search_reddit_targeting)")
    except Exception as exc:  # validation endpoints down: warn, don't block
        warnings.append(f"Targeting could not be pre-validated with Reddit ({exc}); the apply may still refuse it.")
    _targeting_notes(config, targeting, errors, warnings)
    if campaign.get("objective") == "APP_INSTALLS" and len(targeting.get("devices") or []) != 1:
        errors.append(
            "APP_INSTALLS campaigns need exactly one device target (Reddit rule), e.g. "
            "devices=[{\"type\": \"MOBILE\", \"os\": \"IOS\"}]."
        )
    if errors:
        return _validation_error(errors)

    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": ad_group_name,
        "configured_status": "PAUSED",
        "conversion_pixel_id": conversion_pixel_id,
        "targeting": targeting,
    }
    if not is_cbo:
        payload.update(
            {
                "goal_type": goal_type,
                "goal_value": to_micro(goal_value),
                "bid_strategy": bid_strategy,
                "bid_type": bid_type,
            }
        )
    elif bid_strategy:
        payload["bid_strategy"] = bid_strategy
    if bid_value is not None:
        payload["bid_value"] = to_micro(bid_value)
    if optimization_goal:
        payload["optimization_goal"] = optimization_goal
    elif is_cbo and campaign.get("optimization_goal"):
        payload["optimization_goal"] = campaign["optimization_goal"]
    # Reddit requires start_time on ad group creation (the spec marks it
    # optional; the API answers "Input should be a valid datetime").
    payload["start_time"] = start_time or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if end_time:
        payload["end_time"] = end_time
    if schedule_blocks:
        payload["schedule"] = schedule_blocks
        warnings.append(_SCHEDULE_NOTE)

    if targeting.get("geolocations") and not targeting.get("languages"):
        warnings.append(
            "No language targeting: ads show to every language in the targeted "
            "locations. Pass languages (ISO 639-1 codes) when the copy is not universal."
        )
    if campaign.get("objective") == "CONVERSIONS" and not optimization_goal and not is_cbo:
        warnings.append(
            "CONVERSIONS campaign without optimization_goal: Reddit will pick a "
            "default event. Pass the pixel event you actually want (e.g. PURCHASE, SIGN_UP)."
        )
    warnings.append("The ad group is created PAUSED; add an ad, then enable.")
    return _store(
        {
            "operation": "reddit_create_ad_group",
            "entity_type": "ad_group",
            "entity_id": "",
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "campaign_name": campaign.get("name"),
                "payload": payload,
                "display": {
                    "ad_group_name": ad_group_name,
                    "campaign_id": campaign_id,
                    "budget": goal_value,
                    "goal_type": goal_type or None,
                    "bid": {
                        "strategy": payload.get("bid_strategy"),
                        "type": payload.get("bid_type"),
                        "value": bid_value,
                    },
                    "targeting": targeting,
                    "schedule": describe_schedule(schedule_blocks),
                    "currency": meta["currency"],
                    "status_on_create": "PAUSED",
                },
                "daily_equivalent": daily_equivalent,
            },
        },
        warnings=warnings,
    )


def _draft_ad_from_post(
    config: AdLoopConfig,
    *,
    errors: list[str],
    warnings: list[str],
    ad_account_id: str,
    ad_group_id: str,
    ad_name: str,
    profile_id: str,
    post_id: str,
    click_url: str,
    copy_given: bool,
) -> dict:
    """Promote an existing post: an ad on a post that already exists.

    The usual reason is a post that already has upvotes and comments, which a
    freshly created copy would not. Text posts open themselves on click and
    refuse a click_url; media posts click through to the ad's click_url,
    which defaults to the destination stored on the post.
    """
    click_url = (click_url or "").strip()
    if click_url and not click_url.startswith(("http://", "https://")):
        errors.append("click_url must start with http:// or https://")
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    if copy_given:
        warnings.append(
            "post_id promotes an existing post: headline, body, image_url, "
            "call_to_action and display_url were ignored."
        )
    if errors:
        return _validation_error(errors)

    post = get_reddit_post(config, post_id)
    if not post:
        return {"error": f"Reddit post '{post_id}' was not found (ids look like t3_abc123)."}
    post_type = str(post.get("type") or "").upper()
    post_profile = str(post.get("profile_id") or "")
    profile_id = (profile_id or "").strip() or post_profile
    if post_profile and profile_id != post_profile:
        errors.append(
            f"post {post_id} belongs to profile {post_profile}, not {profile_id}; "
            "an ad can only promote a post of its own profile."
        )
    content = post.get("content") or []
    first = content[0] if content and isinstance(content[0], dict) else {}
    body = str(post.get("body") or "")
    if post_type == "TEXT":
        if click_url:
            errors.append(
                "This is a TEXT (free-form) post: it opens itself on click and Reddit "
                "refuses a click_url on the ad. Leave click_url empty; the link readers "
                "follow has to be in the post body."
            )
        elif "http://" not in body and "https://" not in body:
            warnings.append(
                "The post body contains no link, so the ad leads nowhere beyond the post "
                "itself. Expect post views and comments, not landing-page clicks."
            )
    else:
        click_url = click_url or str(first.get("destination_url") or "")
        if not click_url:
            errors.append(
                f"{post_type} posts need a click_url and this post stores no destination; "
                "pass click_url."
            )
    if errors:
        return _validation_error(errors)
    if click_url:
        from adloop.ads.write import _validate_urls

        url_errors, url_warnings = _validate_urls([click_url])
        for url, problem in url_errors.items():
            if problem:
                errors.append(f"'{url}' is not reachable: {problem}")
        warnings.extend(url_warnings.values())
        if errors:
            return _validation_error(errors)

    ad_group = _fetch(config, "ad_group", ad_group_id)
    if not ad_group:
        return {"error": f"Reddit ad group '{ad_group_id}' was not found."}

    headline = str(post.get("headline") or "")
    ad_payload: dict[str, Any] = {
        "ad_group_id": ad_group_id,
        "name": (ad_name or headline or post_id)[:200],
        "configured_status": "PAUSED",
        "profile_id": profile_id,
        "post_id": post_id,
    }
    if post_type != "TEXT" and click_url:
        ad_payload["click_url"] = click_url
    if post.get("allow_comments"):
        warnings.append(
            "Comments are enabled on this post: Redditors will reply publicly on the ad. "
            "update_reddit_ad(allow_comments=false) turns them off after creation."
        )
    warnings.append(
        "The ad is created PAUSED and then goes through Reddit policy review "
        "(PENDING_APPROVAL) once enabled."
    )
    return _store(
        {
            "operation": "reddit_create_ad",
            "entity_type": "ad",
            "entity_id": "",
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "ad_group_name": ad_group.get("name"),
                "campaign_id": ad_group.get("campaign_id"),
                "profile_id": profile_id,
                # No post payload: the apply step promotes the existing post.
                "post": None,
                "existing_post_id": post_id,
                "ad": ad_payload,
                "display": {
                    "existing_post": True,
                    "post_id": post_id,
                    "post_url": post.get("post_url"),
                    "headline": headline,
                    "post_type": post_type,
                    "click_url": ad_payload.get("click_url"),
                    "status_on_create": "PAUSED",
                },
            },
        },
        warnings=warnings,
    )


def draft_reddit_ad(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_group_id: str = "",
    ad_name: str = "",
    profile_id: str = "",
    headline: str = "",
    post_type: str = "TEXT",
    click_url: str = "",
    body: str = "",
    image_url: str = "",
    call_to_action: str = "",
    display_url: str = "",
    allow_comments: bool = True,
    post_id: str = "",
) -> dict:
    """Draft a post + ad pair (created PAUSED). IMAGE posts take a public image_url.

    With ``post_id`` an existing post is promoted instead: no post is created,
    the copy arguments are ignored, and the ad points at that post.
    """
    blocked = _guard("draft_reddit_ad", config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    ad_group_id = (ad_group_id or "").strip()
    if not ad_group_id:
        errors.append("ad_group_id is required (see get_reddit_ad_groups)")
    post_id = (post_id or "").strip()
    if post_id:
        return _draft_ad_from_post(
            config, errors=errors, warnings=warnings, ad_account_id=ad_account_id,
            ad_group_id=ad_group_id, ad_name=ad_name, profile_id=profile_id,
            post_id=post_id, click_url=click_url,
            copy_given=bool(headline or body or image_url or call_to_action or display_url),
        )
    profile_id = (profile_id or "").strip()
    if not profile_id:
        errors.append(
            "profile_id is required — the Reddit profile that authors the post "
            "(list_reddit_funding_instruments → profiles)"
        )
    headline = (headline or "").strip()
    if not headline:
        errors.append("headline is required")
    elif len(headline) > _MAX_HEADLINE_CHARS:
        errors.append(f"headline is {len(headline)} characters; keep it under {_MAX_HEADLINE_CHARS}")
    post_type = (post_type or "TEXT").strip().upper()
    if post_type not in _POST_TYPES:
        errors.append(
            f"post_type must be one of {sorted(_POST_TYPES)} (VIDEO/CAROUSEL are not supported yet)"
        )
    click_url = (click_url or "").strip()
    if not click_url:
        errors.append("click_url is required — the landing page users should reach")
    elif not click_url.startswith(("http://", "https://")):
        errors.append("click_url must start with http:// or https://")
    image_url = (image_url or "").strip()
    if post_type == "IMAGE" and not image_url:
        errors.append("IMAGE posts need image_url (a publicly reachable image)")
    if body and len(body) > _MAX_TEXT_BODY_CHARS:
        errors.append("body is too long")
    if post_type == "TEXT" and click_url and click_url not in (body or ""):
        # Free-form (text) ads click through to the post, never to a URL
        # ("Free form ads cannot have a click url", verified live), so the
        # landing link must be part of the body or the ad leads nowhere.
        errors.append(
            "TEXT ads open the post itself, not click_url: put the full click_url into "
            "body (e.g. as the last line) so readers can reach the site, or use post_type IMAGE."
        )
    call_to_action = (call_to_action or "").strip()
    if call_to_action and call_to_action not in _CALL_TO_ACTIONS:
        errors.append(f"call_to_action must be one of {sorted(_CALL_TO_ACTIONS)}")
    try:
        account = resolve_account(config, ad_account_id)
    except ValueError as e:
        errors.append(str(e))
        account = ""
    if errors:
        return _validation_error(errors)

    # Never send traffic to a page nobody verified: same rule as RSAs.
    from adloop.ads.write import _validate_urls

    url_errors, url_warnings = _validate_urls([click_url] + ([image_url] if image_url else []))
    for url, problem in url_errors.items():
        if problem:
            errors.append(f"'{url}' is not reachable: {problem}")
    warnings.extend(url_warnings.values())
    if errors:
        return _validation_error(errors)

    ad_group = _fetch(config, "ad_group", ad_group_id)
    if not ad_group:
        return {"error": f"Reddit ad group '{ad_group_id}' was not found."}

    post_payload: dict[str, Any] = {
        "type": post_type,
        "headline": headline,
        "allow_comments": bool(allow_comments),
    }
    if post_type == "IMAGE":
        content_item: dict[str, Any] = {"destination_url": click_url, "media_url": image_url}
        if call_to_action:
            content_item["call_to_action"] = call_to_action
        if display_url:
            content_item["display_url"] = display_url.strip()
        post_payload["content"] = [content_item]
    else:
        # TEXT posts must not carry a content block ("Post content must be
        # empty for this post type", verified live); the click_url lives on
        # the ad, and the CTA does not apply.
        post_payload["content"] = []
    if body:
        post_payload["body"] = body
    ad_payload: dict[str, Any] = {
        "ad_group_id": ad_group_id,
        "name": (ad_name or headline)[:200],
        "configured_status": "PAUSED",
        "profile_id": profile_id,
    }
    if post_type == "IMAGE":
        ad_payload["click_url"] = click_url
    else:
        warnings.append(
            "Text ads open the Reddit post on click; the link in the body is the way to your site. "
            "Expect post views and comments, not landing-page clicks, in the report."
        )
    if allow_comments:
        warnings.append(
            "Comments are enabled: Redditors will reply publicly on the ad. "
            "Plan to moderate, or pass allow_comments=false."
        )
    warnings.append(
        "The ad is created PAUSED and then goes through Reddit policy review "
        "(PENDING_APPROVAL) once enabled."
    )
    return _store(
        {
            "operation": "reddit_create_ad",
            "entity_type": "ad",
            "entity_id": "",
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "ad_group_name": ad_group.get("name"),
                "campaign_id": ad_group.get("campaign_id"),
                "profile_id": profile_id,
                "post": post_payload,
                "ad": ad_payload,
                "display": {
                    "headline": headline,
                    "post_type": post_type,
                    "click_url": click_url,
                    "image_url": image_url or None,
                    "call_to_action": call_to_action or None,
                    "status_on_create": "PAUSED",
                },
            },
        },
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Preflight (dry run) and apply — called from adloop.ads.write.confirm_and_apply
# ---------------------------------------------------------------------------


def is_reddit_plan(plan: ChangePlan) -> bool:
    return str(plan.operation or "").startswith(OPERATION_PREFIX)


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read the target and re-run the safety checks; sends nothing.

    Raises on a hard failure (entity gone, cap exceeded, scope missing) so
    ``confirm_and_apply`` can report ``DRY_RUN_FAILED`` and keep the
    two-phase gate closed.
    """
    from adloop.safety.guards import check_bid_increase

    _require_scope_for_writes(config)
    changes = plan.changes or {}
    account = changes.get("ad_account_id") or plan.customer_id
    meta = account_meta(config, account)
    checks: dict[str, Any] = {"ad_account_id": account, "currency": meta["currency"]}

    if plan.operation in ("reddit_set_status", "reddit_archive_entity"):
        current = _fetch(config, plan.entity_type, plan.entity_id)
        if not current:
            raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
        checks["entity"] = current.get("name")
        checks["configured_status_now"] = current.get("configured_status")
        checks["already_in_target_status"] = current.get("configured_status") == changes.get("target_status")
    elif plan.operation == "reddit_update_ad":
        current = _fetch(config, "ad", plan.entity_id)
        if not current:
            raise ValueError(f"ad '{plan.entity_id}' no longer exists.")
        checks["entity"] = current.get("name")
        checks["post_id"] = current.get("post_id")
    elif plan.operation in ("reddit_update_campaign", "reddit_update_ad_group"):
        current = _fetch(config, plan.entity_type, plan.entity_id)
        if not current:
            raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
        checks["entity"] = current.get("name")
        patch = changes.get("patch") or {}
        if changes.get("daily_equivalent") is not None:
            _budget_cap(float(changes["daily_equivalent"]), config, meta["currency"])
            checks["budget_cap_ok"] = True
        if "bid_value" in patch:
            current_bid = from_micro(current.get("bid_value")) or 0.0
            check_bid_increase(current_bid, from_micro(patch["bid_value"]) or 0.0, config.safety)
            checks["bid_increase_ok"] = True
        if "goal_value" in patch and changes.get("is_campaign_budget_optimization") != bool(
            current.get("is_campaign_budget_optimization")
        ):
            raise ValueError("The entity's budget mode changed since the draft; redraft.")
    elif plan.operation == "reddit_create_campaign":
        if changes.get("daily_equivalent") is not None:
            _budget_cap(float(changes["daily_equivalent"]), config, meta["currency"])
            checks["budget_cap_ok"] = True
        checks["funding_instrument_id"] = (changes.get("payload") or {}).get("funding_instrument_id")
    elif plan.operation == "reddit_create_ad_group":
        campaign_id = (changes.get("payload") or {}).get("campaign_id", "")
        campaign = _fetch(config, "campaign", campaign_id)
        if not campaign:
            raise ValueError(f"campaign '{campaign_id}' no longer exists.")
        checks["campaign"] = campaign.get("name")
        if changes.get("daily_equivalent") is not None:
            _budget_cap(float(changes["daily_equivalent"]), config, meta["currency"])
            checks["budget_cap_ok"] = True
    elif plan.operation == "reddit_create_ad":
        ad_group_id = (changes.get("ad") or {}).get("ad_group_id", "")
        group = _fetch(config, "ad_group", ad_group_id)
        if not group:
            raise ValueError(f"ad group '{ad_group_id}' no longer exists.")
        checks["ad_group"] = group.get("name")
        if changes.get("existing_post_id"):
            if not get_reddit_post(config, str(changes["existing_post_id"])):
                raise ValueError(f"post '{changes['existing_post_id']}' no longer exists.")
            checks["existing_post_id"] = changes["existing_post_id"]
    else:
        raise ValueError(f"Unknown Reddit operation: {plan.operation}")
    checks["status_on_create"] = "PAUSED" if plan.operation.startswith("reddit_create_") else None
    return checks


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a Reddit plan. Called by ``confirm_and_apply`` after the gate."""
    _require_scope_for_writes(config)
    changes = plan.changes or {}
    account = changes.get("ad_account_id") or plan.customer_id
    op = plan.operation

    if op in ("reddit_set_status", "reddit_archive_entity"):
        collection = _COLLECTIONS[plan.entity_type]
        result = reddit_patch(
            config,
            f"{collection}/{plan.entity_id}",
            {"data": {"configured_status": changes["target_status"]}},
        )
        data = data_of(result)
        return {
            "entity_type": plan.entity_type,
            "entity_id": plan.entity_id,
            "configured_status": data.get("configured_status", changes["target_status"]),
            "effective_status": data.get("effective_status"),
        }

    if op == "reddit_update_ad":
        result: dict[str, Any] = {"entity_type": "ad", "entity_id": plan.entity_id, "updated_fields": []}
        if changes.get("patch"):
            data = data_of(reddit_patch(config, f"ads/{plan.entity_id}", {"data": changes["patch"]}))
            result["name"] = data.get("name")
            result["click_url"] = data.get("click_url")
            result["updated_fields"] += sorted(changes["patch"].keys())
        if changes.get("post_patch") and changes.get("post_id"):
            post = data_of(
                reddit_patch(config, f"posts/{changes['post_id']}", {"data": changes["post_patch"]})
            )
            result["allow_comments"] = post.get("allow_comments", changes["post_patch"].get("allow_comments"))
            result["updated_fields"].append("allow_comments")
        return result

    if op in ("reddit_update_campaign", "reddit_update_ad_group"):
        collection = _COLLECTIONS[plan.entity_type]
        result = reddit_patch(config, f"{collection}/{plan.entity_id}", {"data": changes["patch"]})
        data = data_of(result)
        return {
            "entity_type": plan.entity_type,
            "entity_id": plan.entity_id,
            "name": data.get("name"),
            "updated_fields": sorted(changes["patch"].keys()),
            "configured_status": data.get("configured_status"),
        }

    if op == "reddit_create_campaign":
        payload = dict(changes["payload"])
        payload["configured_status"] = "PAUSED"
        data = data_of(reddit_post(config, f"ad_accounts/{account}/campaigns", {"data": payload}))
        return {
            "campaign_id": data.get("id"),
            "name": data.get("name"),
            "configured_status": data.get("configured_status", "PAUSED"),
            "effective_status": data.get("effective_status"),
            "next_steps": "Create an ad group (draft_reddit_ad_group), then an ad, then enable.",
        }

    if op == "reddit_create_ad_group":
        payload = dict(changes["payload"])
        payload["configured_status"] = "PAUSED"
        data = data_of(reddit_post(config, f"ad_accounts/{account}/ad_groups", {"data": payload}))
        return {
            "ad_group_id": data.get("id"),
            "campaign_id": data.get("campaign_id", payload.get("campaign_id")),
            "name": data.get("name"),
            "configured_status": data.get("configured_status", "PAUSED"),
            "effective_status": data.get("effective_status"),
            "next_steps": "Create an ad with draft_reddit_ad, then enable the ad group and campaign.",
        }

    if op == "reddit_create_ad":
        profile_id = changes["profile_id"]
        if changes.get("existing_post_id"):
            post_id = str(changes["existing_post_id"])
            post = get_reddit_post(config, post_id) or {"id": post_id}
        else:
            post = data_of(reddit_post(config, f"profiles/{profile_id}/posts", {"data": changes["post"]}))
            post_id = post.get("id")
            if not post_id:
                raise ValueError("Reddit created the post but returned no post id; check Ads Manager.")
        ad_payload = dict(changes["ad"])
        ad_payload["post_id"] = post_id
        ad_payload["configured_status"] = "PAUSED"
        try:
            ad = data_of(reddit_post(config, f"ad_accounts/{account}/ads", {"data": ad_payload}))
        except Exception as exc:
            if changes.get("existing_post_id"):
                raise
            # The post exists now; say so instead of leaving an orphan the
            # user cannot find.
            raise ValueError(
                f"The post was created (post_id {post_id}, {post.get('post_url') or 'no url'}) "
                f"but the ad could not be: {exc}. Reuse the post_id when retrying "
                "(draft_reddit_ad accepts post_id)."
            ) from exc
        return {
            "ad_id": ad.get("id"),
            "post_id": post_id,
            "post_url": post.get("post_url"),
            "ad_group_id": ad.get("ad_group_id", ad_payload["ad_group_id"]),
            "configured_status": ad.get("configured_status", "PAUSED"),
            "effective_status": ad.get("effective_status"),
            "preview_url": ad.get("preview_url"),
            "next_steps": "Enable the ad with enable_reddit_entity(entity_type='ad'); Reddit then reviews it.",
        }

    raise ValueError(f"Unknown Reddit operation: {op}")
