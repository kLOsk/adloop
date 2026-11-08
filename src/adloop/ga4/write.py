"""GA4 write tools — key events (conversions), preview-first like all writes.

The tracking loop's missing end: attribution_check can diagnose "this
event fires but isn't a key event, so Ads can't optimize on it" — this
lets the user fix that in-chat instead of clicking through the GA4 UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_COUNTING_METHODS = ("ONCE_PER_EVENT", "ONCE_PER_SESSION")


def _default_value_entry(value: float | None, currency: str) -> dict | None:
    if value is None:
        return None
    return {"numeric_value": float(value), "currency_code": currency}


def draft_key_event(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    event_name: str = "",
    counting_method: str = "ONCE_PER_EVENT",
    default_value: float | None = None,
    currency_code: str = "",
) -> dict:
    """Draft marking a GA4 event as a key event (conversion), or changing one — returns PREVIEW.

    When the event already is a key event, the plan changes its counting
    method or default value instead (before → after in the preview), so a
    create no longer fails at apply time with "already exists".
    """
    from adloop.ga4.admin import fingerprint
    from adloop.ga4.client import get_admin_client
    from adloop.ga4.tracking import key_event_entry
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_key_event", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    property_id = str(property_id or "").strip().removeprefix("properties/")
    event_name = (event_name or "").strip()
    counting_method = (counting_method or "").strip().upper()
    currency_code = (currency_code or "").strip().upper()

    errors = []
    if not property_id:
        errors.append(
            "property_id is required (falls back to the configured GA4 "
            "property when set)"
        )
    elif not property_id.isdigit():
        errors.append("property_id must be a numeric GA4 property ID")
    if not event_name:
        errors.append("event_name is required (e.g. 'sign_up')")
    if counting_method not in _COUNTING_METHODS:
        errors.append(
            "counting_method must be ONCE_PER_EVENT (recommended for e.g. "
            "purchases) or ONCE_PER_SESSION (recommended for e.g. sign-ups)"
        )
    if default_value is not None:
        if isinstance(default_value, bool) or not isinstance(default_value, (int, float)) or default_value < 0:
            errors.append("default_value must be a number of 0 or more")
        if not (len(currency_code) == 3 and currency_code.isalpha()):
            errors.append("default_value needs currency_code, a three-letter code such as EUR (GA4 requires both)")
    elif currency_code:
        errors.append("currency_code only applies together with default_value")
    if errors:
        return {"error": "Validation failed", "details": errors}

    wanted_default = _default_value_entry(default_value, currency_code)
    warnings: list[str] = []
    existing = None
    try:
        existing = next(
            (
                key_event_entry(ke)
                for ke in get_admin_client(config).list_key_events(parent=f"properties/{property_id}")
                if ke.event_name == event_name
            ),
            None,
        )
    except Exception as e:  # noqa: BLE001 - the check is a convenience, the dry run checks again
        warnings.append(
            f"Could not check the existing key events ({e}); if '{event_name}' already is one, the dry run says so."
        )

    if existing is not None:
        try:
            check_blocked_operation("ga4_update_key_event", config.safety)
        except SafetyViolation as e:
            return {"error": str(e)}
        before = {"counting_method": existing["counting_method"], "default_value": existing.get("default_value")}
        after = {"counting_method": counting_method, "default_value": wanted_default or before["default_value"]}
        changed = [k for k in after if after[k] != before[k]]
        if not changed:
            return {
                "status": "NO_CHANGE",
                "message": f"'{event_name}' already is a key event with these settings.",
                "key_event": existing,
            }
        changes = {"property_id": property_id, "event_name": event_name, "resource_name": existing["resource_name"]}
        for key in changed:
            changes[key] = after[key]
            changes[f"current_{key}"] = before[key]
        changes["after"] = after
        changes["fingerprint"] = fingerprint(before)
        plan = ChangePlan(
            operation="ga4_update_key_event", entity_type="key_event", entity_id=event_name,
            customer_id="", changes=changes, platform="ga4",
        )
        store_plan(plan)
        preview = plan.to_preview()
        preview["warnings"] = warnings + [
            "Applies to future data only: key events already recorded keep their old counting and value.",
        ]
        return preview

    changes = {
        "property_id": property_id,
        "event_name": event_name,
        "counting_method": counting_method,
    }
    if wanted_default:
        changes["default_value"] = wanted_default
    plan = ChangePlan(
        operation="create_key_event",
        entity_type="key_event",
        entity_id=event_name,
        customer_id="",
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings + [
        "Key-event status applies to FUTURE data only — historical events "
        "are not reclassified.",
        "For Google Ads to optimize on it, the GA4 property must be linked "
        "to the Ads account and the conversion imported there — check with "
        "attribution_check after a few days.",
    ]
    return preview


def _key_event_proto(event_name: str, counting_method: str, default_value: dict | None, name: str = ""):
    from google.analytics.admin_v1beta import types

    kwargs = {
        "event_name": event_name,
        "counting_method": types.KeyEvent.CountingMethod[counting_method],
    }
    if name:
        kwargs["name"] = name
    if default_value:
        kwargs["default_value"] = types.KeyEvent.DefaultValue(
            numeric_value=default_value["numeric_value"], currency_code=default_value["currency_code"],
        )
    return types.KeyEvent(**kwargs)


def _apply_create_key_event(config: AdLoopConfig, changes: dict) -> dict:
    """Execute a previewed key-event creation via the GA4 Admin API."""
    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    created = client.create_key_event(
        parent=f"properties/{changes['property_id']}",
        key_event=_key_event_proto(changes["event_name"], changes["counting_method"], changes.get("default_value")),
    )
    result = {
        "resource_names": [created.name],
        "event_name": created.event_name,
        "counting_method": created.counting_method.name,
    }
    if changes.get("default_value"):
        result["default_value"] = changes["default_value"]
    return result


def _current_key_event(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.admin import fingerprint
    from adloop.ga4.client import get_admin_client
    from adloop.ga4.tracking import key_event_entry

    current = key_event_entry(get_admin_client(config).get_key_event(name=changes["resource_name"]))
    before = {"counting_method": current["counting_method"], "default_value": current.get("default_value")}
    if fingerprint(before) != changes["fingerprint"]:
        raise RuntimeError(
            f"The key event '{changes['event_name']}' was changed in GA4 after the preview; draft the change again."
        )
    return current


def _apply_update_key_event(config: AdLoopConfig, changes: dict) -> dict:
    from google.protobuf import field_mask_pb2

    from adloop.ga4.client import get_admin_client

    _current_key_event(config, changes)
    after = changes["after"]
    paths = [k for k in ("counting_method", "default_value") if f"current_{k}" in changes]
    updated = get_admin_client(config).update_key_event(
        key_event=_key_event_proto(
            changes["event_name"], after["counting_method"], after.get("default_value"), name=changes["resource_name"],
        ),
        update_mask=field_mask_pb2.FieldMask(paths=paths),
    )
    return {
        "resource_names": [updated.name],
        "event_name": updated.event_name,
        "counting_method": updated.counting_method.name,
        "updated_fields": paths,
    }


def draft_delete_key_event(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    event_name: str = "",
) -> dict:
    """Draft removing a GA4 key event (the event keeps firing) — returns PREVIEW.

    Resolves the event name to its key-event resource at draft time, so the
    preview names exactly what goes, and refuses key events GA4 marks as
    not deletable.
    """
    from adloop.ga4.client import get_admin_client
    from adloop.ga4.tracking import key_event_entry
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("delete_key_event", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    property_id = str(property_id or "").strip().removeprefix("properties/")
    event_name = (event_name or "").strip()

    errors = []
    if not property_id:
        errors.append(
            "property_id is required (falls back to the configured GA4 "
            "property when set)"
        )
    elif not property_id.isdigit():
        errors.append("property_id must be a numeric GA4 property ID")
    if not event_name:
        errors.append("event_name is required (e.g. 'sign_up')")
    if errors:
        return {"error": "Validation failed", "details": errors}

    client = get_admin_client(config)
    existing = [
        key_event_entry(ke)
        for ke in client.list_key_events(parent=f"properties/{property_id}")
    ]
    match = next((e for e in existing if e["event_name"] == event_name), None)
    if match is None:
        return {
            "error": f"'{event_name}' is not a key event on properties/{property_id}.",
            "key_events": sorted(e["event_name"] for e in existing),
        }
    if not match["deletable"]:
        return {
            "error": (
                f"GA4 marks the key event '{event_name}' as not deletable, so "
                "the Admin API cannot remove it."
            ),
            "key_event": match,
        }

    plan = ChangePlan(
        operation="delete_key_event",
        entity_type="key_event",
        entity_id=event_name,
        customer_id="",
        changes={
            "property_id": property_id,
            "event_name": event_name,
            "resource_name": match["resource_name"],
            "counting_method": match["counting_method"],
            "create_time": match["create_time"],
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        f"'{event_name}' stops being a key event: it keeps firing and stays "
        "in event reports, but no longer counts in key-event (conversion) "
        "metrics.",
        "This affects FUTURE reporting only: key events already recorded "
        "are not reclassified.",
        "If Google Ads imports this key event as a conversion, that "
        "conversion action stops receiving new conversions, which can "
        "starve Smart Bidding of its signal.",
    ]
    return preview


def preflight(config: AdLoopConfig, plan) -> dict | None:
    """Dry-run checks for GA4 plans (the Admin API has no validate-only mode).

    Each plan re-reads what it would change and raises when the apply would
    fail, or would act on something that changed since the preview. Nothing
    is changed.
    """
    from adloop.ga4 import annotations, definitions

    if plan.operation in annotations.APPLIERS:
        return annotations.preflight(config, plan)
    if plan.operation in definitions.APPLIERS:
        return definitions.preflight(config, plan)
    if plan.operation == "ga4_update_key_event":
        _current_key_event(config, plan.changes)
        return {"key_event_unchanged": True}
    if plan.operation == "create_key_event":
        return _preflight_create_key_event(config, plan)
    if plan.operation != "delete_key_event":
        return None

    from adloop.ga4.client import get_admin_client

    key_event = get_admin_client(config).get_key_event(
        name=plan.changes["resource_name"]
    )
    if key_event.event_name != plan.changes["event_name"]:
        raise RuntimeError(
            f"{plan.changes['resource_name']} now belongs to "
            f"'{key_event.event_name}', not '{plan.changes['event_name']}'."
        )
    if not key_event.deletable:
        raise RuntimeError(
            f"GA4 marks the key event '{key_event.event_name}' as not deletable."
        )
    return {"key_event_exists": True, "deletable": True}


def _preflight_create_key_event(config: AdLoopConfig, plan) -> dict:
    from adloop.ga4.client import get_admin_client

    names = {
        ke.event_name
        for ke in get_admin_client(config).list_key_events(parent=f"properties/{plan.changes['property_id']}")
    }
    if plan.changes["event_name"] in names:
        raise RuntimeError(f"'{plan.changes['event_name']}' already is a key event; draft it again to change it.")
    return {"key_event_exists": False}


def _apply_delete_key_event(config: AdLoopConfig, changes: dict) -> dict:
    """Execute a previewed key-event deletion via the GA4 Admin API."""
    from adloop.ga4.client import get_admin_client

    get_admin_client(config).delete_key_event(name=changes["resource_name"])
    return {
        "resource_names": [changes["resource_name"]],
        "event_name": changes["event_name"],
        "deleted": True,
    }


def _all_appliers() -> dict:
    from adloop.ga4 import annotations, definitions

    return {
        "create_key_event": _apply_create_key_event,
        "ga4_update_key_event": _apply_update_key_event,
        "delete_key_event": _apply_delete_key_event,
        **annotations.APPLIERS,
        **definitions.APPLIERS,
    }


_APPLIERS = _all_appliers()

# Plan operations that belong to GA4 (confirm_and_apply routes them here).
GA4_OPERATIONS = frozenset(_APPLIERS)


def apply_plan(config: AdLoopConfig, plan) -> dict:
    """Execute a previewed GA4 plan."""
    return _APPLIERS[plan.operation](config, plan.changes)
