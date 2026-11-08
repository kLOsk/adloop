"""GA4 annotations: notes on a date or a date range, shown on GA4's charts.

Reads list them and attach the ones inside a report's dates to the report.
Writes (create, update, delete) are previewed and approved like every other
change. The API is v1alpha (Google's own Analytics MCP server uses the same
endpoint); its errors come back as a plain message.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# ORANGE exists in the API but is reserved for annotations Google creates.
COLORS = ("BLUE", "GREEN", "RED", "PURPLE", "BROWN", "CYAN")
MAX_TITLE = 60
MAX_DESCRIPTION = 150
_ACTIONS = ("create", "update", "delete")
_RELATIVE = re.compile(r"^(\d+)daysAgo$")


def _date_text(value) -> str:
    if value is None:
        return ""
    year = getattr(value, "year", 0)
    if not year:
        return ""
    return f"{year:04d}-{value.month:02d}-{value.day:02d}"


def _to_proto_date(text: str):
    from google.type import date_pb2

    d = date.fromisoformat(text)
    return date_pb2.Date(year=d.year, month=d.month, day=d.day)


def annotation_entry(annotation) -> dict:
    entry = {
        "annotation": annotation.name,
        "title": annotation.title,
        "color": getattr(annotation.color, "name", str(annotation.color)),
    }
    if annotation.description:
        entry["description"] = annotation.description
    single = _date_text(getattr(annotation, "annotation_date", None))
    if single:
        entry["date"] = single
    else:
        span = getattr(annotation, "annotation_date_range", None)
        entry["start_date"] = _date_text(getattr(span, "start_date", None))
        entry["end_date"] = _date_text(getattr(span, "end_date", None))
    if getattr(annotation, "system_generated", False):
        entry["system_generated"] = True
    return entry


def _span(entry: dict) -> tuple[str, str]:
    if entry.get("date"):
        return entry["date"], entry["date"]
    return entry.get("start_date", ""), entry.get("end_date", "")


def _api_error(exc: Exception) -> str:
    message = (getattr(exc, "message", "") or str(exc)).splitlines()[0][:300]
    return f"GA4 annotations API (v1alpha) error: {message}"


def _list(config: AdLoopConfig, property_id: str) -> list[dict]:
    from adloop.ga4.client import get_admin_alpha_client

    client = get_admin_alpha_client(config)
    return [annotation_entry(a) for a in client.list_reporting_data_annotations(parent=f"properties/{property_id}")]


def resolve_date(text: str, today: date) -> date | None:
    """GA4 date text ('today', 'yesterday', 'NdaysAgo', 'YYYY-MM-DD') -> date."""
    text = str(text or "").strip()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    if match := _RELATIVE.match(text):
        return today - timedelta(days=int(match.group(1)))
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _today(time_zone: str = "") -> date:
    if time_zone:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(time_zone)).date()
        except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
            pass
    return datetime.now(timezone.utc).date()


def _overlaps(entry: dict, ranges: list[tuple[date, date]]) -> bool:
    start_text, end_text = _span(entry)
    try:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        return False
    return any(start <= r_end and end >= r_start for r_start, r_end in ranges)


def list_ga4_annotations(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """The property's annotations, newest first, optionally only those in a date range."""
    from adloop.ga4.admin import check_property

    pid, error = check_property(property_id)
    if error:
        return {"error": error}
    if bool(date_range_start) != bool(date_range_end):
        return {"error": "date_range_start and date_range_end must be given together"}
    ranges = []
    if date_range_start:
        today = _today()
        start, end = resolve_date(date_range_start, today), resolve_date(date_range_end, today)
        if start is None or end is None or start > end:
            return {"error": "date range must be 'today', 'yesterday', 'NdaysAgo' or YYYY-MM-DD, start before end"}
        ranges = [(start, end)]
    try:
        annotations = _list(config, pid)
    except Exception as e:  # noqa: BLE001 - v1alpha: say what failed instead of raising
        return {"error": _api_error(e)}
    if ranges:
        annotations = [a for a in annotations if _overlaps(a, ranges)]
    annotations.sort(key=lambda a: _span(a)[0], reverse=True)
    return {"property": f"properties/{pid}", "annotations": annotations, "total": len(annotations)}


def annotations_for_report(
    config: AdLoopConfig, property_id: str, date_texts: list[tuple[str, str]], time_zone: str = ""
) -> list[dict]:
    """Annotations inside a report's date ranges, oldest first. Never raises."""
    pid = str(property_id or "").strip().removeprefix("properties/")
    if not pid.isdigit():
        return []
    today = _today(time_zone)
    ranges = []
    for start_text, end_text in date_texts:
        start, end = resolve_date(start_text, today), resolve_date(end_text, today)
        if start and end:
            ranges.append((start, end))
    if not ranges:
        return []
    try:
        annotations = [a for a in _list(config, pid) if _overlaps(a, ranges)]
    except Exception:  # noqa: BLE001 - a report must not fail because annotations could not be read
        return []
    annotations.sort(key=lambda a: _span(a)[0])
    return [{k: v for k, v in a.items() if k != "annotation"} for a in annotations]


def _resolve_annotation_name(property_id: str, annotation: str) -> str:
    text = str(annotation or "").strip()
    if text.isdigit():
        return f"properties/{property_id}/reportingDataAnnotations/{text}"
    return text


def _fields(entry: dict) -> dict:
    """The values an update can change, for before/after and fingerprints."""
    return {
        "title": entry["title"],
        "description": entry.get("description", ""),
        "color": entry["color"],
        "start_date": _span(entry)[0],
        "end_date": _span(entry)[1],
    }


def draft_ga4_annotation(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    action: str = "create",
    annotation: str = "",
    title: str = "",
    date_: str = "",
    end_date: str = "",
    description: str | None = None,
    color: str = "",
) -> dict:
    """Draft creating, changing or deleting a GA4 annotation — returns a PREVIEW."""
    from adloop.ga4.admin import check_property, fingerprint
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return {"error": f"action must be one of {', '.join(_ACTIONS)}"}
    operation = f"ga4_{action}_annotation"
    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    pid, error = check_property(property_id)
    errors = [error] if error else []
    title = (title or "").strip()
    color = (color or "").strip().upper()
    date_, end_date = (date_ or "").strip(), (end_date or "").strip()
    if description is not None:
        description = description.strip()

    if title and len(title) > MAX_TITLE:
        errors.append(f"title is {len(title)} characters; GA4 allows {MAX_TITLE}")
    if description and len(description) > MAX_DESCRIPTION:
        errors.append(f"description is {len(description)} characters; GA4 allows {MAX_DESCRIPTION}")
    if color and color not in COLORS:
        errors.append(f"color must be one of {', '.join(COLORS)} (ORANGE is reserved for Google's own annotations)")
    for label, value in (("date", date_), ("end_date", end_date)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{label} must be YYYY-MM-DD, got {value!r}")
    if end_date and not date_:
        errors.append("end_date needs date (the start of the range)")
    if action == "create":
        if not title:
            errors.append("title is required")
        if not date_:
            errors.append("date is required (YYYY-MM-DD; with end_date it starts a range)")
    elif not annotation:
        errors.append(f"annotation is required to {action} one (its ID or resource name, see list_ga4_annotations)")
    if not errors and date_ and end_date and end_date < date_:
        errors.append("end_date must not be before date")
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        existing = _list(config, pid)
    except Exception as e:  # noqa: BLE001 - v1alpha
        return {"error": _api_error(e)}

    warnings: list[str] = []
    if action == "create":
        new = {
            "title": title, "description": description or "", "color": color or "BLUE",
            "start_date": date_, "end_date": end_date or date_,
        }
        if any(a["title"] == title and _span(a) == (new["start_date"], new["end_date"]) for a in existing):
            warnings.append("An annotation with this title already exists on these dates.")
        changes = {"property_id": pid, **new}
        plan = ChangePlan(operation=operation, entity_type="annotation", entity_id=title, changes=changes)
    else:
        name = _resolve_annotation_name(pid, annotation)
        current = next((a for a in existing if a["annotation"] == name), None)
        if current is None:
            return {
                "error": f"No annotation {name} on properties/{pid}.",
                "annotations": [{"annotation": a["annotation"], "title": a["title"], "dates": "–".join(dict.fromkeys(_span(a)))} for a in existing[:20]],
            }
        if current.get("system_generated"):
            return {"error": f"'{current['title']}' was created by Google; GA4 does not allow changing or deleting it."}
        before = _fields(current)
        if action == "delete":
            changes = {"property_id": pid, "annotation": name, **before, "fingerprint": fingerprint(before)}
            plan = ChangePlan(
                operation=operation, entity_type="annotation", entity_id=current["title"],
                changes=changes, requires_double_confirm=True,
            )
            warnings.append("Deleting an annotation cannot be undone; the note disappears from every GA4 chart.")
        else:
            after = dict(before)
            if title:
                after["title"] = title
            if description is not None:
                after["description"] = description
            if color:
                after["color"] = color
            if date_:
                after["start_date"], after["end_date"] = date_, end_date or date_
            changed = [k for k in after if after[k] != before[k]]
            if not changed:
                return {"status": "NO_CHANGE", "message": f"'{current['title']}' already has these values.", "annotation": current}
            changes = {"property_id": pid, "annotation": name}
            for key in changed:
                changes[key] = after[key]
                changes[f"current_{key}"] = before[key]
            changes["after"] = after
            changes["fingerprint"] = fingerprint(before)
            plan = ChangePlan(operation=operation, entity_type="annotation", entity_id=current["title"], changes=changes)

    plan.platform = "ga4"
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _current(config: AdLoopConfig, name: str) -> dict:
    from adloop.ga4.client import get_admin_alpha_client

    return annotation_entry(get_admin_alpha_client(config).get_reporting_data_annotation(name=name))


def _check_unchanged(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.admin import fingerprint

    current = _current(config, changes["annotation"])
    if fingerprint(_fields(current)) != changes["fingerprint"]:
        raise RuntimeError(
            f"The annotation '{current['title']}' was changed in GA4 after the preview; draft the change again."
        )
    return current


def preflight(config: AdLoopConfig, plan) -> dict:
    """Dry run: the Admin API has no validate-only mode, so re-read instead."""
    if plan.operation == "ga4_create_annotation":
        existing = _list(config, plan.changes["property_id"])
        return {"annotations_readable": True, "existing_annotations": len(existing)}
    _check_unchanged(config, plan.changes)
    return {"annotation_unchanged": True}


def _annotation_proto(values: dict, name: str = ""):
    from google.analytics.admin_v1alpha import types

    kwargs = {
        "title": values["title"],
        "color": types.ReportingDataAnnotation.Color[values["color"]],
    }
    if name:
        kwargs["name"] = name
    if values.get("description"):
        kwargs["description"] = values["description"]
    if values["start_date"] == values["end_date"]:
        kwargs["annotation_date"] = _to_proto_date(values["start_date"])
    else:
        kwargs["annotation_date_range"] = types.ReportingDataAnnotation.DateRange(
            start_date=_to_proto_date(values["start_date"]), end_date=_to_proto_date(values["end_date"]),
        )
    return types.ReportingDataAnnotation(**kwargs)


def _apply_create(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.client import get_admin_alpha_client

    created = get_admin_alpha_client(config).create_reporting_data_annotation(
        parent=f"properties/{changes['property_id']}", reporting_data_annotation=_annotation_proto(changes),
    )
    return {"resource_names": [created.name], **annotation_entry(created)}


def _apply_update(config: AdLoopConfig, changes: dict) -> dict:
    from google.protobuf import field_mask_pb2

    from adloop.ga4.client import get_admin_alpha_client

    current = _check_unchanged(config, changes)
    after = changes["after"]
    before = _fields(current)
    paths = [k for k in ("title", "description", "color") if after[k] != before[k]]
    if (after["start_date"], after["end_date"]) != (before["start_date"], before["end_date"]):
        # Switching between one day and a range changes which oneof member is set.
        paths += ["annotation_date", "annotation_date_range"]
    updated = get_admin_alpha_client(config).update_reporting_data_annotation(
        reporting_data_annotation=_annotation_proto(after, name=changes["annotation"]),
        update_mask=field_mask_pb2.FieldMask(paths=paths),
    )
    return {"resource_names": [updated.name], **annotation_entry(updated)}


def _apply_delete(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.client import get_admin_alpha_client

    _check_unchanged(config, changes)
    get_admin_alpha_client(config).delete_reporting_data_annotation(name=changes["annotation"])
    return {"resource_names": [changes["annotation"]], "title": changes["title"], "deleted": True}


APPLIERS = {
    "ga4_create_annotation": _apply_create,
    "ga4_update_annotation": _apply_update,
    "ga4_delete_annotation": _apply_delete,
}
