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


def draft_key_event(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    event_name: str = "",
    counting_method: str = "ONCE_PER_EVENT",
) -> dict:
    """Draft marking a GA4 event as a key event (conversion) — returns PREVIEW."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_key_event", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    property_id = str(property_id or "").strip().removeprefix("properties/")
    event_name = (event_name or "").strip()
    counting_method = (counting_method or "").strip().upper()

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
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_key_event",
        entity_type="key_event",
        entity_id=event_name,
        customer_id="",
        changes={
            "property_id": property_id,
            "event_name": event_name,
            "counting_method": counting_method,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Key-event status applies to FUTURE data only — historical events "
        "are not reclassified.",
        "For Google Ads to optimize on it, the GA4 property must be linked "
        "to the Ads account and the conversion imported there — check with "
        "attribution_check after a few days.",
    ]
    return preview


def _apply_create_key_event(config: AdLoopConfig, changes: dict) -> dict:
    """Execute a previewed key-event creation via the GA4 Admin API."""
    from google.analytics.admin_v1beta import types

    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    key_event = types.KeyEvent(
        event_name=changes["event_name"],
        counting_method=types.KeyEvent.CountingMethod[
            changes["counting_method"]
        ],
    )
    created = client.create_key_event(
        parent=f"properties/{changes['property_id']}",
        key_event=key_event,
    )
    return {
        "resource_names": [created.name],
        "event_name": created.event_name,
        "counting_method": created.counting_method.name,
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

    For a key-event deletion, re-reads the key event and raises if it no
    longer exists or has become undeletable; nothing is changed. Key-event
    creation keeps its plain dry run (no checks).
    """
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


def _apply_delete_key_event(config: AdLoopConfig, changes: dict) -> dict:
    """Execute a previewed key-event deletion via the GA4 Admin API."""
    from adloop.ga4.client import get_admin_client

    get_admin_client(config).delete_key_event(name=changes["resource_name"])
    return {
        "resource_names": [changes["resource_name"]],
        "event_name": changes["event_name"],
        "deleted": True,
    }


_APPLIERS = {
    "create_key_event": _apply_create_key_event,
    "delete_key_event": _apply_delete_key_event,
}

# Plan operations that belong to GA4 (confirm_and_apply routes them here).
GA4_OPERATIONS = frozenset(_APPLIERS)


def apply_plan(config: AdLoopConfig, plan) -> dict:
    """Execute a previewed GA4 plan."""
    return _APPLIERS[plan.operation](config, plan.changes)
