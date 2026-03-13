"""Tracking tools — validate GA4 event tracking and generate code snippets."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_GA4_AUTO_EVENTS = {
    "page_view",
    "session_start",
    "first_visit",
    "user_engagement",
    "scroll",
    "click",
    "file_download",
    "video_start",
    "video_progress",
    "video_complete",
    "view_search_results",
    "form_start",
    "form_submit",
}

_GA4_RECOMMENDED_PARAMS = {
    "sign_up": {"method": "string"},
    "login": {"method": "string"},
    "purchase": {
        "transaction_id": "string",
        "value": "number",
        "currency": "string",
        "items": "array",
    },
    "begin_checkout": {"value": "number", "currency": "string", "items": "array"},
    "add_to_cart": {"value": "number", "currency": "string", "items": "array"},
    "generate_lead": {"value": "number", "currency": "string"},
    "select_content": {"content_type": "string", "content_id": "string"},
    "share": {"method": "string", "content_type": "string", "item_id": "string"},
    "search": {"search_term": "string"},
    "view_item": {"items": "array"},
}

_TRIGGER_TEMPLATES = {
    "form_submit": (
        "document.querySelector('{selector}').addEventListener('submit', function(e) {{\n"
        "  {gtag_call}\n"
        "}});"
    ),
    "button_click": (
        "document.querySelector('{selector}').addEventListener('click', function() {{\n"
        "  {gtag_call}\n"
        "}});"
    ),
    "page_load": (
        "document.addEventListener('DOMContentLoaded', function() {{\n"
        "  {gtag_call}\n"
        "}});"
    ),
}


# ---------------------------------------------------------------------------
# Tool 1: validate_tracking
# ---------------------------------------------------------------------------


def validate_tracking(
    config: AdLoopConfig,
    *,
    expected_events: list[str],
    property_id: str = "",
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
) -> dict:
    """Compare expected tracking events (from codebase) against actual GA4 data.

    The AI searches the user's codebase for gtag/dataLayer event calls, extracts
    event names, and passes them here. This tool queries GA4 for actual events
    and returns a structured comparison.
    """
    from adloop.ga4.tracking import get_tracking_events

    ga4_result = get_tracking_events(
        config,
        property_id=property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )
    if "error" in ga4_result:
        return ga4_result

    ga4_events: dict[str, int] = {}
    for row in ga4_result.get("rows", []):
        name = row.get("eventName", "")
        count = int(row.get("eventCount", 0))
        ga4_events[name] = ga4_events.get(name, 0) + count

    expected_set = set(expected_events)
    ga4_set = set(ga4_events.keys())

    matched = []
    for ev in sorted(expected_set & ga4_set):
        matched.append({"event_name": ev, "ga4_count": ga4_events[ev]})

    missing_from_ga4 = sorted(expected_set - ga4_set)

    auto_collected = sorted(
        (ga4_set - expected_set) & _GA4_AUTO_EVENTS
    )
    unexpected = []
    for ev in sorted(ga4_set - expected_set - _GA4_AUTO_EVENTS):
        unexpected.append({"event_name": ev, "ga4_count": ga4_events[ev]})

    insights = []
    if missing_from_ga4:
        insights.append(
            f"{len(missing_from_ga4)} event(s) found in code but NOT firing in GA4: "
            f"{', '.join(missing_from_ga4)}. "
            f"Possible causes: code not deployed, event behind a condition that "
            f"hasn't triggered, GDPR consent blocking, or wrong GA4 property ID."
        )
    if unexpected:
        names = [e["event_name"] for e in unexpected]
        insights.append(
            f"{len(unexpected)} event(s) in GA4 not found in the provided code list: "
            f"{', '.join(names)}. "
            f"These may come from third-party scripts, tag managers, or code "
            f"paths the search missed."
        )
    for ev in matched:
        if ev["ga4_count"] == 0:
            insights.append(
                f"Event '{ev['event_name']}' exists in both code and GA4 but has "
                f"zero count — it may not be triggering for real users."
            )

    return {
        "matched": matched,
        "missing_from_ga4": missing_from_ga4,
        "unexpected_in_ga4": unexpected,
        "auto_collected": auto_collected,
        "insights": insights,
        "date_range": ga4_result.get("date_range", {
            "start": date_range_start, "end": date_range_end,
        }),
    }


# ---------------------------------------------------------------------------
# Tool 2: generate_tracking_code
# ---------------------------------------------------------------------------


def generate_tracking_code(
    config: AdLoopConfig,
    *,
    event_name: str,
    event_params: dict | None = None,
    trigger: str = "",
    property_id: str = "",
    check_existing: bool = True,
) -> dict:
    """Generate a GA4 event tracking JavaScript snippet.

    Optionally checks GA4 to see if the event already fires, and includes
    recommended parameters for well-known GA4 events.
    """
    already_exists = None
    existing_count = None

    if check_existing:
        from adloop.ga4.tracking import get_tracking_events

        ga4_result = get_tracking_events(
            config,
            property_id=property_id,
            date_range_start="28daysAgo",
            date_range_end="today",
        )
        if "error" not in ga4_result:
            for row in ga4_result.get("rows", []):
                if row.get("eventName") == event_name:
                    already_exists = True
                    existing_count = int(row.get("eventCount", 0))
                    break
            else:
                already_exists = False

    params = event_params or {}
    recommended = _GA4_RECOMMENDED_PARAMS.get(event_name, {})
    for key, ptype in recommended.items():
        if key not in params:
            params[key] = f"<{ptype}>"

    if params:
        param_lines = []
        for k, v in params.items():
            if isinstance(v, str) and not v.startswith("<"):
                param_lines.append(f"  '{k}': '{v}'")
            elif isinstance(v, (int, float)):
                param_lines.append(f"  '{k}': {v}")
            else:
                param_lines.append(f"  '{k}': {v}")
        params_str = ",\n".join(param_lines)
        gtag_call = f"gtag('event', '{event_name}', {{\n{params_str}\n}});"
    else:
        gtag_call = f"gtag('event', '{event_name}');"

    javascript = gtag_call
    if trigger and trigger in _TRIGGER_TEMPLATES:
        template = _TRIGGER_TEMPLATES[trigger]
        selector = "YOUR_SELECTOR"
        indented_gtag = gtag_call.replace("\n", "\n  ")
        javascript = template.format(selector=selector, gtag_call=indented_gtag)

    notes = []
    if already_exists:
        notes.append(
            f"Event '{event_name}' already fires in GA4 "
            f"({existing_count} occurrences in the last 28 days). "
            f"Adding this code may create duplicates — verify first."
        )
    notes.append(
        f"To use '{event_name}' as a conversion, mark it as a conversion event "
        f"in GA4: Admin > Events > toggle 'Mark as conversion' next to the event."
    )
    if event_name in _GA4_AUTO_EVENTS:
        notes.append(
            f"'{event_name}' is an auto-collected GA4 event. You typically "
            f"don't need to implement it manually — GA4 tracks it automatically."
        )
    notes.append(
        "If your site uses a GDPR consent banner, this event will only fire "
        "for users who accept analytics cookies (unless Consent Mode v2 is active)."
    )

    return {
        "event_name": event_name,
        "javascript": javascript,
        "already_exists": already_exists,
        "existing_count": existing_count,
        "notes": notes,
    }
