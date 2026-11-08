"""GA4 data retention: how long GA4 keeps event- and user-level data.

The audit flags GA4's two-month default because it cuts explorations and
funnels short. This module changes the setting, previewed and approved like
every other GA4 write. The Admin API has no validate-only mode, so the dry
run re-reads the settings and refuses when someone changed them in GA4 after
the preview.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

OPERATION = "ga4_update_data_retention"

MONTHS = {
    "TWO_MONTHS": 2,
    "FOURTEEN_MONTHS": 14,
    "TWENTY_SIX_MONTHS": 26,
    "THIRTY_EIGHT_MONTHS": 38,
    "FIFTY_MONTHS": 50,
}
_BY_MONTHS = {months: name for name, months in MONTHS.items()}
# Standard properties offer 2 and 14 months; the longer periods are GA4 360.
_STANDARD = frozenset({"TWO_MONTHS", "FOURTEEN_MONTHS"})


def parse_retention(value: object) -> tuple[str, str]:
    """('FOURTEEN_MONTHS', '') from 14, '14', '14 months' or the enum name; ('', error) otherwise."""
    if value is None or value == "":
        return "", ""
    text = str(value).strip().upper().replace("-", "_")
    if text in MONTHS:
        return text, ""
    digits = text.removesuffix("MONTHS").removesuffix("MONTH").strip(" _")
    if digits.isdigit() and int(digits) in _BY_MONTHS:
        return _BY_MONTHS[int(digits)], ""
    return "", f"'{value}' is not a GA4 retention period: use 2, 14, 26, 38 or 50 (months)"


def _settings_name(property_id: str) -> str:
    return f"properties/{property_id}/dataRetentionSettings"


def read_settings(config: AdLoopConfig, property_id: str) -> dict:
    """The property's retention as enum names plus the reset flag."""
    from adloop.ga4.client import get_admin_client

    settings = get_admin_client(config).get_data_retention_settings(name=_settings_name(property_id))
    return {
        "event_data_retention": settings.event_data_retention.name,
        "user_data_retention": settings.user_data_retention.name,
        "reset_user_data_on_new_activity": bool(settings.reset_user_data_on_new_activity),
    }


def _shown(field: str, value: object) -> object:
    return f"{MONTHS[value]} months" if field != "reset_user_data_on_new_activity" and value in MONTHS else value


def plan_retention_change(
    current: dict, wanted: dict, level: str,
) -> tuple[dict, list[str], bool, str]:
    """(after, changed fields, shortens, error) for a wanted change of the current settings."""
    after = {**current, **{k: v for k, v in wanted.items() if v not in ("", None)}}
    for field in ("event_data_retention", "user_data_retention"):
        if level != "360" and after[field] not in _STANDARD and after[field] != current[field]:
            return {}, [], False, (
                f"{MONTHS[after[field]]} months needs Google Analytics 360; a standard property keeps "
                "data for 2 or 14 months."
            )
    changed = [k for k in after if after[k] != current[k]]
    shortens = any(
        MONTHS.get(after[f], 0) < MONTHS.get(current[f], 0)
        for f in ("event_data_retention", "user_data_retention") if f in changed
    )
    return after, changed, shortens, ""


def retention_warnings(changed: list[str], shortens: bool) -> list[str]:
    warnings = [
        "Data retention limits explorations, funnels and other user-level reports; GA4's standard "
        "reports are aggregated and not affected.",
    ]
    if shortens:
        warnings.append(
            "Shorter retention: GA4 deletes event and user data older than the new period, and it "
            "cannot be restored."
        )
    elif any(f != "reset_user_data_on_new_activity" for f in changed):
        warnings.append(
            "Longer retention applies from now on: data GA4 already deleted under the old setting "
            "does not come back."
        )
    warnings.append(
        "If your privacy policy names how long Google Analytics data is kept, update it to match."
    )
    return warnings


def draft_ga4_data_retention(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    event_data_retention: str = "",
    user_data_retention: str = "",
    reset_user_data_on_new_activity: bool | None = None,
) -> dict:
    """Draft a change to how long GA4 keeps event- and user-level data — returns PREVIEW."""
    from adloop.ga4.admin import check_property, fingerprint, service_level
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(OPERATION, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    pid, error = check_property(property_id)
    errors = [error] if error else []
    event, event_error = parse_retention(event_data_retention)
    user, user_error = parse_retention(user_data_retention)
    errors += [e for e in (event_error, user_error) if e]
    if not (event or user or reset_user_data_on_new_activity is not None) and not errors:
        errors.append(
            "Pass at least one of event_data_retention, user_data_retention or "
            "reset_user_data_on_new_activity (e.g. event_data_retention=14)."
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        current = read_settings(config, pid)
    except Exception as e:  # noqa: BLE001 - nothing to plan against without the current values
        return {"error": f"Could not read the data retention of properties/{pid}: {e}"}

    level = service_level(config, pid)
    after, changed, shortens, refusal = plan_retention_change(
        current,
        {
            "event_data_retention": event,
            "user_data_retention": user,
            "reset_user_data_on_new_activity": reset_user_data_on_new_activity,
        },
        level,
    )
    if refusal:
        return {"error": refusal, "service_level": level}
    if not changed:
        return {
            "status": "NO_CHANGE",
            "message": f"properties/{pid} already has these retention settings.",
            "data_retention": {k: _shown(k, v) for k, v in current.items()},
        }

    changes: dict = {"property_id": pid}
    for field in changed:
        changes[f"current_{field}"] = _shown(field, current[field])
        changes[field] = _shown(field, after[field])
    changes["after"] = after
    changes["fingerprint"] = fingerprint(current)
    plan = ChangePlan(
        operation=OPERATION, entity_type="data_retention", entity_id=f"properties/{pid}",
        customer_id="", changes=changes, platform="ga4", requires_double_confirm=shortens,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = retention_warnings(changed, shortens)
    return preview


def check_unchanged(config: AdLoopConfig, changes: dict) -> dict:
    """Re-read the settings; raise when they changed since the preview."""
    from adloop.ga4.admin import fingerprint

    current = read_settings(config, changes["property_id"])
    if fingerprint(current) != changes["fingerprint"]:
        raise RuntimeError(
            f"The data retention of properties/{changes['property_id']} was changed in GA4 after the "
            "preview; draft the change again."
        )
    return current


def write_settings(config: AdLoopConfig, property_id: str, after: dict, fields: list[str]) -> dict:
    from google.analytics.admin_v1beta import types
    from google.protobuf import field_mask_pb2

    from adloop.ga4.client import get_admin_client

    duration = types.DataRetentionSettings.RetentionDuration
    updated = get_admin_client(config).update_data_retention_settings(
        data_retention_settings=types.DataRetentionSettings(
            name=_settings_name(property_id),
            event_data_retention=duration[after["event_data_retention"]],
            user_data_retention=duration[after["user_data_retention"]],
            reset_user_data_on_new_activity=after["reset_user_data_on_new_activity"],
        ),
        update_mask=field_mask_pb2.FieldMask(paths=fields),
    )
    return {
        "resource_names": [updated.name],
        "event_data_months": MONTHS.get(updated.event_data_retention.name),
        "user_data_months": MONTHS.get(updated.user_data_retention.name),
        "reset_user_data_on_new_activity": bool(updated.reset_user_data_on_new_activity),
        "updated_fields": fields,
    }


def preflight(config: AdLoopConfig, plan) -> dict:
    check_unchanged(config, plan.changes)
    return {"data_retention_unchanged": True}


def _apply(config: AdLoopConfig, changes: dict) -> dict:
    check_unchanged(config, changes)
    after = changes["after"]
    fields = [k for k in after if f"current_{k}" in changes]
    return write_settings(config, changes["property_id"], after, fields)


APPLIERS = {OPERATION: _apply}
