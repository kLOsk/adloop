"""GA4 setup audit: the settings that quietly cost data, checked in one call.

What a consultant checks by hand in the GA4 admin: data retention, the
Google Ads link, streams that stopped sending, key events that never fire,
Google signals, enhanced measurement, the custom definition limits. Reads
only GA4 (the Ads side of key events is check_key_event_imports), so it is
unmetered on AdLoop Cloud and works for Analytics-only workspaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_ORDER = {"warn": 0, "info": 1, "ok": 2}
_ENHANCED = {
    "scrolls_enabled": "scrolls",
    "outbound_clicks_enabled": "outbound clicks",
    "site_search_enabled": "site search",
    "video_engagement_enabled": "video engagement",
    "file_downloads_enabled": "file downloads",
    "page_changes_enabled": "page changes (single-page apps)",
    "form_interactions_enabled": "form interactions",
}


def _check(checks: list, name: str, status: str, detail: str) -> None:
    checks.append({"check": name, "status": status, "detail": detail})


def _short(exc: Exception) -> str:
    return ((getattr(exc, "message", "") or str(exc)).splitlines() or [type(exc).__name__])[0][:300]


def audit_ga4_property(config: AdLoopConfig, *, property_id: str = "") -> dict:
    """Run every setup check on one property; warnings first."""
    from adloop.ga4.admin import get_ga4_property
    from adloop.ga4.client import get_admin_alpha_client
    from adloop.ga4.definitions import _read as read_definitions
    from adloop.ga4.definitions import _usage
    from adloop.ga4.reports import run_ga4_report
    from adloop.ga4.tracking import list_key_events

    overview = get_ga4_property(config, property_id=property_id)
    if "error" in overview:
        return overview
    name = overview["property"]
    pid = name.removeprefix("properties/")
    details = overview["details"]
    checks: list[dict] = []
    unavailable = dict(overview.get("unavailable", {}))

    if details.get("delete_time"):
        _check(checks, "property_status", "warn", f"The property is in the trash (deleted {details['delete_time']}).")

    retention = overview.get("data_retention")
    if retention:
        months = retention.get("event_data_months")
        if months == 2:
            _check(checks, "data_retention", "warn",
                   "Event data is kept 2 months (GA4's default): explorations and funnels cannot look further "
                   "back. Standard properties allow 14 months (draft_ga4_data_retention); the change only helps from "
                   "now on.")
        else:
            _check(checks, "data_retention", "ok", f"Event data is kept {months} months.")

    links = overview.get("google_ads_links")
    if links is not None:
        if links:
            ids = ", ".join(link["customer_id"] for link in links)
            _check(checks, "google_ads_link", "ok", f"Linked to Google Ads {ids}.")
        else:
            _check(checks, "google_ads_link", "warn" if overview.get("key_event_count") else "info",
                   "No Google Ads account is linked: Google Ads cannot import key events or audiences from it "
                   "(draft_conversion_tracking_setup links it).")
    for insight in overview.get("insights", []):
        if "is not linked to this" in insight:
            _check(checks, "configured_ads_account", "warn", insight)

    streams = overview.get("data_streams")
    if streams == []:
        _check(checks, "data_streams", "warn", "The property has no data stream, so it receives no data.")
    elif streams:
        try:
            activity = run_ga4_report(config, property_id=name, dimensions=["streamId"], metrics=["eventCount"],
                                      date_range_start="7daysAgo", date_range_end="today", limit=100)
            active = {row.get("streamId") for row in activity.get("rows", []) if float(row.get("eventCount") or 0) > 0}
            for stream in streams:
                stream_id = stream["stream"].rsplit("/", 1)[-1]
                label = stream.get("measurement_id") or stream.get("package_name") or stream.get("bundle_id") or stream_id
                if stream_id in active:
                    _check(checks, "stream_activity", "ok", f"{stream['display_name']} ({label}) sent events in the last 7 days.")
                else:
                    _check(checks, "stream_activity", "warn",
                           f"{stream['display_name']} ({label}) sent no events in the last 7 days: the tag may be "
                           "missing from the site or app, or blocked.")
        except Exception as e:  # noqa: BLE001
            unavailable["stream_activity"] = _short(e)

        alpha = None
        for stream in streams:
            if stream.get("type") != "web":
                continue
            try:
                alpha = alpha or get_admin_alpha_client(config)
                settings = alpha.get_enhanced_measurement_settings(name=f"{stream['stream']}/enhancedMeasurementSettings")
            except Exception as e:  # noqa: BLE001 - v1alpha
                unavailable["enhanced_measurement"] = _short(e)
                break
            if not settings.stream_enabled:
                _check(checks, "enhanced_measurement", "info",
                       f"Enhanced measurement is off on {stream['display_name']}: scrolls, outbound clicks, site "
                       "search, video and file downloads are only tracked if the site sends them itself.")
                continue
            off = [label for field, label in _ENHANCED.items() if not getattr(settings, field, True)]
            if off:
                _check(checks, "enhanced_measurement", "info",
                       f"Enhanced measurement on {stream['display_name']} skips: {', '.join(off)}.")
            else:
                _check(checks, "enhanced_measurement", "ok", f"Enhanced measurement is fully on for {stream['display_name']}.")

    try:
        signals = get_admin_alpha_client(config).get_google_signals_settings(name=f"{name}/googleSignalsSettings")
        state = getattr(signals.state, "name", "")
        if state == "GOOGLE_SIGNALS_ENABLED":
            _check(checks, "google_signals", "info",
                   "Google signals are on: cross-device and demographic data from signed-in users, but GA4 "
                   "applies thresholds and withholds small rows in reports that use them.")
        elif state == "GOOGLE_SIGNALS_DISABLED":
            _check(checks, "google_signals", "info",
                   "Google signals are off: no cross-device or demographic data from signed-in users, and no "
                   "thresholding from them.")
    except Exception as e:  # noqa: BLE001 - v1alpha
        unavailable["google_signals"] = _short(e)

    try:
        key_events = [k["event_name"] for k in list_key_events(config, property_id=name).get("key_events", [])]
        if not key_events:
            _check(checks, "key_events", "warn",
                   "No key events: GA4 counts no conversions, and Google Ads has nothing from GA4 to optimize for.")
        else:
            fired = run_ga4_report(
                config, property_id=name, dimensions=["eventName"], metrics=["eventCount"],
                date_range_start="28daysAgo", date_range_end="today", limit=len(key_events),
                dimension_filter=[{"field": "eventName", "op": "IN_LIST", "values": key_events}],
            )
            counts = {row.get("eventName"): int(float(row.get("eventCount") or 0)) for row in fired.get("rows", [])}
            silent = [k for k in key_events if not counts.get(k)]
            if silent:
                _check(checks, "key_events", "warn",
                       f"Key event(s) with no events in the last 28 days: {', '.join(silent)}. Either the tracking "
                       "broke or the key event is obsolete (draft_delete_key_event).")
            firing = [k for k in key_events if counts.get(k)]
            if firing:
                _check(checks, "key_events", "ok", f"{len(firing)} key event(s) fired in the last 28 days.")
    except Exception as e:  # noqa: BLE001
        unavailable["key_events"] = _short(e)

    try:
        dimensions, metrics = read_definitions(config, pid)
        usage = _usage(dimensions, metrics, details.get("service_level", "standard"))
        full = [f"{k.replace('_', ' ')} {v['used']} of {v['limit']}" for k, v in usage.items() if v["used"] >= v["limit"] * 0.9]
        if full:
            _check(checks, "custom_definitions", "warn", f"Almost no slots left: {'; '.join(full)}.")
        else:
            _check(checks, "custom_definitions", "ok", "Custom dimension and metric slots are available.")
    except Exception as e:  # noqa: BLE001
        unavailable["custom_definitions"] = _short(e)

    if links:
        _check(checks, "key_event_imports", "info",
               "check_key_event_imports compares these key events with the conversion actions Google Ads "
               "imports from this property.")

    checks.sort(key=lambda c: _ORDER[c["status"]])
    result = {
        "property": name,
        "display_name": details.get("display_name"),
        "service_level": details.get("service_level"),
        "time_zone": details.get("time_zone"),
        "currency_code": details.get("currency_code"),
        "summary": {status: sum(1 for c in checks if c["status"] == status) for status in ("warn", "info", "ok")},
        "checks": checks,
        "insights": [c["detail"] for c in checks if c["status"] == "warn"],
    }
    if unavailable:
        result["unavailable"] = unavailable
    return result
