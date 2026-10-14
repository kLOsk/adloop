"""Asset write tools — call assets and campaign ad schedules.

Every operation follows the AdLoop safety pattern:
    1. a draft function validates the input, stores a ChangePlan and returns
       a preview with its plan_id
    2. confirm_and_apply(plan_id) executes it through ``_dispatch_ads_plan``
       (a dry run sends the same mutates with validate_only=True)

Call assets (CallAsset) are created and linked in one batched
GoogleAdsService.Mutate, at campaign or ad group scope by default; the
account-wide CustomerAsset link is only built when ``scope="account"`` is
passed explicitly. Campaign ad schedules are AD_SCHEDULE CampaignCriterion
records.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import TYPE_CHECKING

from adloop.ads.enums import enum_names

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------

# Dialing codes for normalising a national number to E.164. Numbers for
# other countries are accepted when they are already in E.164 form.
_COUNTRY_DIAL_CODES = {
    "US": "+1", "CA": "+1", "GB": "+44", "DE": "+49", "FR": "+33",
    "IT": "+39", "ES": "+34", "NL": "+31", "BE": "+32", "AT": "+43",
    "CH": "+41", "AU": "+61", "NZ": "+64", "IE": "+353", "PT": "+351",
}

_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


def _normalize_phone_e164(phone: str, country_code: str) -> tuple[str, str | None]:
    """Return ``(e164_number, error_or_None)``.

    Formatting characters are stripped, then:
      - "+..." is already E.164 and kept as is.
      - "00..." is the international access prefix: the digits after it
        already carry the country code, so "0044 20..." becomes "+44 20...".
      - US/CA: an 11-digit number starting with "1" already includes the
        country code, which is dropped before "+1" is added.
      - Elsewhere exactly ONE domestic trunk "0" is dropped ("020 7946 0958"
        in GB becomes "+44 20 7946 0958"). Italy keeps its leading 0 in
        E.164, so IT numbers are not stripped.

    ``country_code`` is required and must agree with the number's dialing
    code when the country is in ``_COUNTRY_DIAL_CODES``.
    """
    cc = (country_code or "").strip().upper()
    if not cc:
        return "", (
            "country_code is required with phone_number (ISO 3166-1 alpha-2, "
            "e.g. 'US', 'GB', 'DE'); it is stored on the call asset and used "
            "to normalise national numbers"
        )
    if not _COUNTRY_CODE_RE.match(cc):
        return "", f"country_code '{country_code}' must be a 2-letter ISO code"

    raw = "".join(ch for ch in phone or "" if ch.isdigit() or ch == "+")
    if not raw.lstrip("+"):
        return "", "phone_number is empty after stripping formatting"
    if "+" in raw[1:]:
        return "", f"phone_number '{phone}' has a '+' after the first character"

    dial = _COUNTRY_DIAL_CODES.get(cc)
    if raw.startswith("+"):
        normalized = raw
    elif raw.startswith("00"):
        normalized = "+" + raw[2:]
    elif not dial:
        return "", (
            f"country_code '{cc}' has no national-number rule here; pass "
            f"phone_number in E.164 form (leading '+' and country code)"
        )
    else:
        north_american_with_country_code = (
            cc in ("US", "CA") and len(raw) == 11 and raw.startswith("1")
        )
        trunk_zero = cc not in ("US", "CA", "IT") and raw.startswith("0")
        digits = raw[1:] if north_american_with_country_code or trunk_zero else raw
        normalized = f"{dial}{digits}"

    digit_count = len(normalized) - 1
    if not 7 <= digit_count <= 15:
        return "", (
            f"phone_number '{phone}' normalises to {normalized}, which has "
            f"{digit_count} digits; E.164 numbers have 7 to 15"
        )
    if dial and not normalized.startswith(dial):
        return "", (
            f"phone_number {normalized} does not match country_code {cc} "
            f"(dialing code {dial})"
        )
    if dial == "+1" and digit_count != 11:
        return "", (
            f"phone_number '{phone}' normalises to {normalized}; {cc} numbers "
            "have 10 digits after +1 (area code included)"
        )
    return normalized, None


# ---------------------------------------------------------------------------
# Ad schedules
# ---------------------------------------------------------------------------

_VALID_DAYS_OF_WEEK = frozenset({
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
})
_MINUTE_TO_ENUM = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}
_ENUM_TO_MINUTE = {name: minute for minute, name in _MINUTE_TO_ENUM.items()}
# Google allows at most six ad schedule intervals per day on a campaign
# (CriterionError.AD_SCHEDULE_EXCEEDED_INTERVALS_PER_DAY_LIMIT).
_MAX_INTERVALS_PER_DAY = 6


def _validate_ad_schedule(schedule: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate ad schedule entries. Returns ``(validated, errors)``.

    Each entry: ``{day_of_week, start_hour, end_hour, start_minute=0,
    end_minute=0}``. Google accepts minutes in {0, 15, 30, 45}, start hours
    0-23 and end hours 0-24 (24 only with minute 0), requires end after
    start, and rejects overlapping intervals on the same day.
    """
    errors: list[str] = []
    validated: list[dict] = []
    for i, entry in enumerate(schedule or []):
        where = f"ad_schedule[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        day = str(entry.get("day_of_week", "")).strip().upper()
        if day not in _VALID_DAYS_OF_WEEK:
            errors.append(
                f"{where}: day_of_week must be one of {sorted(_VALID_DAYS_OF_WEEK)}"
            )
            continue
        try:
            start_hour = int(entry.get("start_hour", -1))
            end_hour = int(entry.get("end_hour", -1))
            start_minute = int(entry.get("start_minute", 0) or 0)
            end_minute = int(entry.get("end_minute", 0) or 0)
        except (TypeError, ValueError):
            errors.append(f"{where}: hour and minute values must be integers")
            continue
        entry_errors = []
        if not 0 <= start_hour <= 23:
            entry_errors.append(f"{where}: start_hour must be in 0..23")
        if not 0 <= end_hour <= 24:
            entry_errors.append(f"{where}: end_hour must be in 0..24")
        if start_minute not in _MINUTE_TO_ENUM:
            entry_errors.append(f"{where}: start_minute must be one of 0, 15, 30, 45")
        if end_minute not in _MINUTE_TO_ENUM:
            entry_errors.append(f"{where}: end_minute must be one of 0, 15, 30, 45")
        if end_hour == 24 and end_minute != 0:
            entry_errors.append(f"{where}: end_hour 24 only allows end_minute 0")
        if (end_hour, end_minute) <= (start_hour, start_minute):
            entry_errors.append(
                f"{where}: end ({end_hour}:{end_minute:02d}) must be after "
                f"start ({start_hour}:{start_minute:02d})"
            )
        if entry_errors:
            errors.extend(entry_errors)
            continue
        validated.append({
            "day_of_week": day,
            "start_hour": start_hour,
            "start_minute": start_minute,
            "end_hour": end_hour,
            "end_minute": end_minute,
        })
    if not errors:
        errors.extend(_schedule_conflicts(validated))
    return validated, errors


def _schedule_conflicts(entries: list[dict], existing: list[dict] | None = None) -> list[str]:
    """Overlaps and per-day limits across ``entries`` (and ``existing`` ones)."""
    by_day: dict[str, list[tuple[int, int, str]]] = {}
    for label, items in (("existing", existing or []), ("new", entries)):
        for entry in items:
            start = entry["start_hour"] * 60 + entry["start_minute"]
            end = entry["end_hour"] * 60 + entry["end_minute"]
            by_day.setdefault(entry["day_of_week"], []).append((start, end, label))

    errors: list[str] = []
    for day, intervals in sorted(by_day.items()):
        if len(intervals) > _MAX_INTERVALS_PER_DAY:
            errors.append(
                f"{day}: {len(intervals)} intervals; Google allows at most "
                f"{_MAX_INTERVALS_PER_DAY} per day"
            )
        intervals.sort()
        for (s1, e1, l1), (s2, e2, l2) in pairwise(intervals):
            if s2 < e1:
                errors.append(
                    f"{day}: {l1} interval {_hhmm(s1)}-{_hhmm(e1)} overlaps "
                    f"{l2} interval {_hhmm(s2)}-{_hhmm(e2)}"
                )
    return errors


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _populate_ad_schedule_info(client: object, info: object, entry: dict) -> None:
    """Set the fields of an AdScheduleInfo from a validated entry."""
    info.day_of_week = getattr(client.enums.DayOfWeekEnum, entry["day_of_week"])
    info.start_hour = int(entry["start_hour"])
    info.end_hour = int(entry["end_hour"])
    info.start_minute = getattr(
        client.enums.MinuteOfHourEnum, _MINUTE_TO_ENUM[int(entry["start_minute"])]
    )
    info.end_minute = getattr(
        client.enums.MinuteOfHourEnum, _MINUTE_TO_ENUM[int(entry["end_minute"])]
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _check_numeric(name: str, value: str, errors: list[str]) -> None:
    """Numeric IDs only: they are interpolated into GAQL and resource names."""
    if value and not str(value).isdigit():
        errors.append(f"{name} must be a numeric ID (got '{value}')")


def _account_time_zone(config: AdLoopConfig, customer_id: str) -> str:
    """The account's time zone, or "" if it can't be read (informational)."""
    from adloop.ads.gaql import execute_query

    try:
        rows = execute_query(config, customer_id, "SELECT customer.time_zone FROM customer LIMIT 1")
    except Exception:  # noqa: BLE001 — the time zone only annotates the preview
        return ""
    return str(rows[0].get("customer.time_zone") or "") if rows else ""


def _time_zone_note(time_zone: str) -> str:
    zone = time_zone or "the account's time zone (could not be read)"
    return (
        f"Ad schedule hours are in the account time zone, {zone}, not in "
        "the searcher's local time."
    )


# ---------------------------------------------------------------------------
# Call assets
# ---------------------------------------------------------------------------

_CALL_ASSET_SCOPES = ("campaign", "ad_group", "account")
_VALID_CALL_REPORTING_STATES = enum_names("CallConversionReportingStateEnum")
_LINK_ENTITY_TYPES = {
    "campaign": "campaign_asset",
    "ad_group": "ad_group_asset",
    "account": "customer_asset",
}


def _resolve_call_asset_scope(
    scope: str, campaign_id: str, ad_group_id: str, errors: list[str]
) -> str:
    """Pick the link scope. The account-wide link is never inferred."""
    scope = (scope or "").strip().lower()
    if scope and scope not in _CALL_ASSET_SCOPES:
        errors.append(f"scope must be one of {list(_CALL_ASSET_SCOPES)} (got '{scope}')")
        return ""
    if campaign_id and ad_group_id:
        errors.append("pass campaign_id or ad_group_id, not both")
        return ""
    if scope == "account":
        if campaign_id or ad_group_id:
            errors.append("scope='account' links the asset account-wide; omit campaign_id and ad_group_id")
            return ""
        return "account"
    if ad_group_id:
        if scope == "campaign":
            errors.append("scope='campaign' needs campaign_id, not ad_group_id")
            return ""
        return "ad_group"
    if campaign_id:
        if scope == "ad_group":
            errors.append("scope='ad_group' needs ad_group_id, not campaign_id")
            return ""
        return "campaign"
    errors.append(
        "campaign_id or ad_group_id is required; an account-wide call asset "
        "needs scope='account'"
    )
    return ""


def draft_call_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    phone_number: str = "",
    country_code: str = "",
    scope: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    call_conversion_action_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft a call asset and its link — returns a PREVIEW.

    The asset links to the campaign (``campaign_id``) or the ad group
    (``ad_group_id``). An account-wide CustomerAsset link is built only for
    ``scope="account"``; it is never inferred from missing IDs.
    ``country_code`` is required: it is stored on the asset and decides how a
    national number is normalised to E.164.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_call_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    campaign_id = str(campaign_id or "").strip()
    ad_group_id = str(ad_group_id or "").strip()
    call_conversion_action_id = str(call_conversion_action_id or "").strip()
    _check_numeric("campaign_id", campaign_id, errors)
    _check_numeric("ad_group_id", ad_group_id, errors)
    _check_numeric("call_conversion_action_id", call_conversion_action_id, errors)
    resolved_scope = _resolve_call_asset_scope(scope, campaign_id, ad_group_id, errors)

    normalized_phone = ""
    if not phone_number:
        errors.append("phone_number is required")
    else:
        normalized_phone, phone_err = _normalize_phone_e164(phone_number, country_code)
        if phone_err:
            errors.append(phone_err)

    schedule, schedule_errors = _validate_ad_schedule(ad_schedule or [])
    errors.extend(schedule_errors)

    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = [
        (
            "Google reviews the phone number before a call asset serves; it can "
            "stay under review (or need verification in the Ads UI under "
            "Assets → Calls) after it is created."
        ),
    ]
    if resolved_scope == "account":
        warnings.append(
            "scope='account' links the number to every eligible campaign in "
            "the account; campaign- and ad-group-level call assets take "
            "precedence where they exist."
        )
    if schedule:
        warnings.append(_time_zone_note(_account_time_zone(config, customer_id)))

    entity_id = {"campaign": campaign_id, "ad_group": ad_group_id}.get(
        resolved_scope, customer_id
    )
    plan = ChangePlan(
        operation="create_call_asset",
        entity_type=_LINK_ENTITY_TYPES[resolved_scope],
        entity_id=entity_id,
        customer_id=customer_id,
        changes={
            "scope": resolved_scope,
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "phone_number": normalized_phone,
            "country_code": country_code.strip().upper(),
            "call_conversion_action_id": call_conversion_action_id,
            "ad_schedule": schedule,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def _apply_create_call_asset(client: object, cid: str, changes: dict) -> dict:
    """Create the CallAsset and its link in one batched Mutate."""
    scope = changes.get("scope")
    link_id = {"campaign": changes.get("campaign_id"), "ad_group": changes.get("ad_group_id")}
    if scope not in _CALL_ASSET_SCOPES:
        raise ValueError(f"Unknown call asset scope: {scope!r}")
    if scope != "account" and not str(link_id[scope] or "").isdigit():
        raise ValueError(f"{scope}_id is required for a {scope}-scope call asset")

    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    temp_asset = asset_service.asset_path(cid, "-1")

    asset_op = client.get_type("MutateOperation")
    asset = asset_op.asset_operation.create
    asset.resource_name = temp_asset
    asset.call_asset.country_code = changes["country_code"]
    asset.call_asset.phone_number = changes["phone_number"]
    if changes.get("call_conversion_action_id"):
        ca_service = client.get_service("ConversionActionService")
        asset.call_asset.call_conversion_action = ca_service.conversion_action_path(
            cid, str(changes["call_conversion_action_id"])
        )
        asset.call_asset.call_conversion_reporting_state = (
            client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
    for entry in changes.get("ad_schedule") or []:
        info = client.get_type("AdScheduleInfo")
        _populate_ad_schedule_info(client, info, entry)
        asset.call_asset.ad_schedule_targets.append(info)

    link_op = client.get_type("MutateOperation")
    if scope == "ad_group":
        link = link_op.ad_group_asset_operation.create
        link.ad_group = googleads_service.ad_group_path(cid, changes["ad_group_id"])
    elif scope == "campaign":
        link = link_op.campaign_asset_operation.create
        link.campaign = googleads_service.campaign_path(cid, changes["campaign_id"])
    else:
        link = link_op.customer_asset_operation.create
    link.asset = temp_asset
    link.field_type = client.enums.AssetFieldTypeEnum.CALL

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=[asset_op, link_op]
    )
    asset_resp, link_resp = list(response.mutate_operation_responses)[:2]
    link_result = {
        "ad_group": "ad_group_asset_result",
        "campaign": "campaign_asset_result",
        "account": "customer_asset_result",
    }[scope]
    return {
        "asset": asset_resp.asset_result.resource_name,
        "link": getattr(link_resp, link_result).resource_name,
        "scope": scope,
    }


def _current_call_asset(config: AdLoopConfig, customer_id: str, asset_id: str) -> dict | None:
    """The asset's current call fields, or None if no asset has that ID."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(config, customer_id, f"""
        SELECT asset.id, asset.type, asset.call_asset.phone_number,
               asset.call_asset.country_code,
               asset.call_asset.call_conversion_action,
               asset.call_asset.call_conversion_reporting_state
        FROM asset
        WHERE asset.id = {asset_id}
        LIMIT 1
    """)
    if not rows:
        return None
    row = rows[0]
    return {
        "type": row.get("asset.type"),
        "phone_number": row.get("asset.call_asset.phone_number") or "",
        "country_code": row.get("asset.call_asset.country_code") or "",
        "call_conversion_action": row.get("asset.call_asset.call_conversion_action") or "",
        "call_conversion_reporting_state": (
            row.get("asset.call_asset.call_conversion_reporting_state") or ""
        ),
    }


def update_call_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    phone_number: str = "",
    country_code: str = "",
    call_conversion_action_id: str = "",
    call_conversion_reporting_state: str = "",
    ad_schedule: list[dict] | None = None,
    clear_ad_schedule: bool = False,
) -> dict:
    """Draft an in-place update of an existing CallAsset — returns a PREVIEW.

    Only the fields passed change. The asset keeps its ID and its links;
    nothing is linked or unlinked. ``country_code`` is required whenever
    ``phone_number`` is passed (no implicit default country), and only
    changes together with it. A non-empty ``ad_schedule`` replaces the
    asset's schedule; ``clear_ad_schedule`` removes it.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_call_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    asset_id = str(asset_id or "").strip()
    call_conversion_action_id = str(call_conversion_action_id or "").strip()
    reporting_state = (call_conversion_reporting_state or "").strip().upper()
    if not asset_id:
        errors.append("asset_id is required")
    _check_numeric("asset_id", asset_id, errors)
    _check_numeric("call_conversion_action_id", call_conversion_action_id, errors)

    normalized_phone = ""
    if phone_number:
        normalized_phone, phone_err = _normalize_phone_e164(phone_number, country_code)
        if phone_err:
            errors.append(phone_err)
    elif country_code:
        errors.append(
            "country_code only changes together with phone_number; pass both"
        )

    if reporting_state and reporting_state not in _VALID_CALL_REPORTING_STATES:
        errors.append(
            f"call_conversion_reporting_state '{call_conversion_reporting_state}' "
            f"invalid; valid: {sorted(_VALID_CALL_REPORTING_STATES)}"
        )
    resource_level = "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
    if call_conversion_action_id:
        if reporting_state and reporting_state != resource_level:
            errors.append(
                f"call_conversion_action_id only counts with "
                f"call_conversion_reporting_state={resource_level}"
            )
        reporting_state = resource_level

    schedule, schedule_errors = _validate_ad_schedule(ad_schedule or [])
    errors.extend(schedule_errors)
    if clear_ad_schedule and schedule:
        errors.append("pass ad_schedule or clear_ad_schedule, not both")

    if not errors and not (
        normalized_phone or call_conversion_action_id or reporting_state
        or schedule or clear_ad_schedule
    ):
        errors.append(
            "No changes specified: pass phone_number (with country_code), "
            "call_conversion_action_id, call_conversion_reporting_state, "
            "ad_schedule or clear_ad_schedule"
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    current = _current_call_asset(config, customer_id, asset_id)
    if current is None:
        return {"error": f"No asset with ID {asset_id} in this account"}
    if current["type"] != "CALL":
        return {"error": f"Asset {asset_id} is a {current['type']} asset, not a CALL asset"}
    if (
        reporting_state == resource_level
        and not call_conversion_action_id
        and not current["call_conversion_action"]
    ):
        return {
            "error": "Validation failed",
            "details": [
                (
                    f"{resource_level} needs a conversion action and the asset "
                    "has none; pass call_conversion_action_id"
                ),
            ],
        }

    changes: dict = {"asset_id": asset_id, "current": current}
    if normalized_phone:
        changes["phone_number"] = normalized_phone
        changes["country_code"] = country_code.strip().upper()
    if call_conversion_action_id:
        changes["call_conversion_action_id"] = call_conversion_action_id
    if reporting_state:
        changes["call_conversion_reporting_state"] = reporting_state
    if schedule or clear_ad_schedule:
        changes["ad_schedule"] = schedule

    warnings = [
        (
            "Assets are shared: the update applies wherever this call asset is "
            "linked (campaigns, ad groups or the account)."
        ),
    ]
    if normalized_phone:
        warnings.append(
            "A new phone number goes back through Google's review before the "
            "asset serves again."
        )
    if schedule:
        warnings.append(_time_zone_note(_account_time_zone(config, customer_id)))
    if clear_ad_schedule:
        warnings.append("Clearing the schedule lets the call asset show at all hours.")

    plan = ChangePlan(
        operation="update_call_asset",
        entity_type="asset",
        entity_id=asset_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def _apply_update_call_asset(client: object, cid: str, changes: dict) -> dict:
    """Update the CallAsset's fields in place (AssetService, field mask)."""
    from google.protobuf import field_mask_pb2

    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.update
    asset.resource_name = asset_service.asset_path(cid, changes["asset_id"])

    paths: list[str] = []
    if "phone_number" in changes:
        asset.call_asset.phone_number = changes["phone_number"]
        asset.call_asset.country_code = changes["country_code"]
        paths += ["call_asset.phone_number", "call_asset.country_code"]
    if changes.get("call_conversion_action_id"):
        ca_service = client.get_service("ConversionActionService")
        asset.call_asset.call_conversion_action = ca_service.conversion_action_path(
            cid, changes["call_conversion_action_id"]
        )
        paths.append("call_asset.call_conversion_action")
    if changes.get("call_conversion_reporting_state"):
        asset.call_asset.call_conversion_reporting_state = getattr(
            client.enums.CallConversionReportingStateEnum,
            changes["call_conversion_reporting_state"],
        )
        paths.append("call_asset.call_conversion_reporting_state")
    if "ad_schedule" in changes:
        # Replace semantics: the masked list becomes exactly these entries
        # (none at all clears the schedule).
        for entry in changes["ad_schedule"]:
            info = client.get_type("AdScheduleInfo")
            _populate_ad_schedule_info(client, info, entry)
            asset.call_asset.ad_schedule_targets.append(info)
        paths.append("call_asset.ad_schedule_targets")
    if not paths:
        raise ValueError("update_call_asset plan has no fields to update")

    op.update_mask = field_mask_pb2.FieldMask(paths=paths)
    response = asset_service.mutate_assets(customer_id=cid, operations=[op])
    return {"resource_name": response.results[0].resource_name}


# ---------------------------------------------------------------------------
# Campaign ad schedules
# ---------------------------------------------------------------------------


def _campaign_schedule_context(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> tuple[dict | None, list[dict]]:
    """The campaign (name, status, account time zone) and its live schedule."""
    from adloop.ads.gaql import execute_query

    campaign_rows = execute_query(config, customer_id, f"""
        SELECT campaign.id, campaign.name, campaign.status, customer.time_zone
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """)
    if not campaign_rows:
        return None, []
    row = campaign_rows[0]
    campaign = {
        "name": row.get("campaign.name") or "",
        "status": row.get("campaign.status") or "",
        "time_zone": row.get("customer.time_zone") or "",
    }

    schedule_rows = execute_query(config, customer_id, f"""
        SELECT campaign_criterion.criterion_id,
               campaign_criterion.ad_schedule.day_of_week,
               campaign_criterion.ad_schedule.start_hour,
               campaign_criterion.ad_schedule.start_minute,
               campaign_criterion.ad_schedule.end_hour,
               campaign_criterion.ad_schedule.end_minute
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
          AND campaign_criterion.type = 'AD_SCHEDULE'
          AND campaign_criterion.status != 'REMOVED'
    """)
    existing = []
    for r in schedule_rows:
        criterion_id = r.get("campaign_criterion.criterion_id")
        existing.append({
            "day_of_week": str(r.get("campaign_criterion.ad_schedule.day_of_week") or ""),
            "start_hour": int(r.get("campaign_criterion.ad_schedule.start_hour") or 0),
            "start_minute": _ENUM_TO_MINUTE.get(
                str(r.get("campaign_criterion.ad_schedule.start_minute") or "ZERO"), 0
            ),
            "end_hour": int(r.get("campaign_criterion.ad_schedule.end_hour") or 0),
            "end_minute": _ENUM_TO_MINUTE.get(
                str(r.get("campaign_criterion.ad_schedule.end_minute") or "ZERO"), 0
            ),
            "remove_id": f"{campaign_id}~{criterion_id}",
        })
    return campaign, existing


def add_ad_schedule(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    schedule: list[dict] | None = None,
) -> dict:
    """Draft AD_SCHEDULE criteria for a campaign — returns a PREVIEW.

    Additive: existing schedule criteria stay. The preview lists them (with
    the ``remove_id`` remove_entity takes for a ``campaign_criterion``) and
    the account time zone the hours are in; new windows that overlap an
    existing one, or push a day past Google's six-interval limit, are refused.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_ad_schedule", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    _check_numeric("campaign_id", campaign_id, errors)
    validated, schedule_errors = _validate_ad_schedule(schedule or [])
    errors.extend(schedule_errors)
    if not schedule_errors and not validated:
        errors.append("schedule needs at least one entry")
    if errors:
        return {"error": "Validation failed", "details": errors}

    campaign, existing = _campaign_schedule_context(config, customer_id, campaign_id)
    if campaign is None:
        return {"error": f"No campaign with ID {campaign_id} in this account"}
    if campaign["status"] == "REMOVED":
        return {"error": f"Campaign {campaign_id} is REMOVED"}

    conflicts = _schedule_conflicts(validated, existing)
    if conflicts:
        return {
            "error": "Validation failed",
            "details": conflicts,
            "existing_schedule": existing,
        }

    warnings = [_time_zone_note(campaign["time_zone"])]
    if not existing:
        warnings.append(
            f"Campaign '{campaign['name']}' has no ad schedule today, so it can "
            "serve at any hour. After this change it serves only inside the "
            "windows above; days without a window stop serving."
        )
    if campaign["status"] != "ENABLED":
        warnings.append(
            f"Campaign status is {campaign['status']}; the schedule takes "
            "effect once the campaign is enabled."
        )

    plan = ChangePlan(
        operation="add_ad_schedule",
        entity_type="campaign_criterion",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "campaign_name": campaign["name"],
            "account_time_zone": campaign["time_zone"],
            "schedule": validated,
            "existing_schedule": existing,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def _apply_add_ad_schedule(client: object, cid: str, changes: dict) -> dict:
    """Create one AD_SCHEDULE CampaignCriterion per schedule entry."""
    campaign_service = client.get_service("CampaignService")
    criterion_service = client.get_service("CampaignCriterionService")
    campaign = campaign_service.campaign_path(cid, changes["campaign_id"])

    operations = []
    for entry in changes["schedule"]:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign
        _populate_ad_schedule_info(client, criterion.ad_schedule, entry)
        operations.append(op)

    response = criterion_service.mutate_campaign_criteria(
        customer_id=cid, operations=operations
    )
    return {"campaign_criteria": [r.resource_name for r in response.results]}
