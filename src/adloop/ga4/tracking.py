"""GA4 tracking/event tools — list custom events and their volume."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def get_tracking_events(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
) -> dict:
    """List all GA4 events and their event count for the given date range."""
    from adloop.ga4.reports import run_ga4_report

    result = run_ga4_report(
        config,
        property_id=property_id,
        dimensions=["eventName"],
        metrics=["eventCount"],
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        limit=500,
    )

    if "rows" in result:
        result["rows"].sort(
            key=lambda r: int(r.get("eventCount", "0")), reverse=True
        )

    return result


def normalize_property(property_id: str) -> str:
    """'123' or 'properties/123' -> 'properties/123' ('' stays '')."""
    pid = str(property_id or "").strip().removeprefix("properties/")
    return f"properties/{pid}" if pid else ""


def key_event_entry(key_event) -> dict:
    """One Admin API KeyEvent as a plain dict."""
    create_time = key_event.create_time
    if hasattr(create_time, "isoformat"):
        create_time = create_time.isoformat()
    entry = {
        "event_name": key_event.event_name,
        "counting_method": key_event.counting_method.name,
        "create_time": str(create_time) if create_time else "",
        "deletable": bool(key_event.deletable),
        "custom": bool(key_event.custom),
        "resource_name": key_event.name,
    }
    default_value = getattr(key_event, "default_value", None)
    if default_value and getattr(default_value, "currency_code", ""):
        entry["default_value"] = {
            "numeric_value": default_value.numeric_value,
            "currency_code": default_value.currency_code,
        }
    return entry


def list_key_events(config: AdLoopConfig, *, property_id: str = "") -> dict:
    """List the key events (conversions) configured on a GA4 property."""
    from adloop.ga4.client import get_admin_client

    parent = normalize_property(property_id)
    if not parent:
        return {
            "error": "property_id is required (falls back to the configured "
            "GA4 property when set)"
        }
    if not parent.removeprefix("properties/").isdigit():
        return {"error": "property_id must be a numeric GA4 property ID"}

    key_events = [
        key_event_entry(ke)
        for ke in get_admin_client(config).list_key_events(parent=parent)
    ]
    key_events.sort(key=lambda e: e["event_name"])
    return {
        "property": parent,
        "key_events": key_events,
        "total": len(key_events),
    }
