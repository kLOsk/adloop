"""GA4 custom dimensions and metrics: list them, create, change or archive one.

Every write is previewed and approved. Before anything reaches Google the
draft checks what GA4 would otherwise only reject later (duplicates, the
per-property limit) or never tell you (data is not filled in for the past).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Per-property limits (GA4 help: "Custom dimensions and metrics limits").
LIMITS = {
    "standard": {"EVENT": 50, "USER": 25, "ITEM": 10, "METRIC": 50},
    "360": {"EVENT": 125, "USER": 100, "ITEM": 25, "METRIC": 125},
}
_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_DISPLAY = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")
MAX_PARAMETER = 40
MAX_USER_PARAMETER = 24
MAX_DISPLAY = 82
MAX_DESCRIPTION = 150
DIMENSION_SCOPES = ("EVENT", "USER", "ITEM")
UNITS = ("STANDARD", "CURRENCY", "FEET", "METERS", "KILOMETERS", "MILES", "MILLISECONDS", "SECONDS", "MINUTES", "HOURS")
RESTRICTED_TYPES = ("COST_DATA", "REVENUE_DATA")
_ACTIONS = {"dimension": ("create", "update", "archive"), "metric": ("create", "update", "archive")}


def _name(value) -> str:
    return getattr(value, "name", "") or str(value or "")


def dimension_entry(d) -> dict:
    entry = {
        "custom_dimension": d.name,
        "parameter_name": d.parameter_name,
        "display_name": d.display_name,
        "scope": _name(d.scope),
        "description": d.description,
    }
    if _name(d.scope) == "USER":
        entry["disallow_ads_personalization"] = bool(d.disallow_ads_personalization)
    return entry


def metric_entry(m) -> dict:
    entry = {
        "custom_metric": m.name,
        "parameter_name": m.parameter_name,
        "display_name": m.display_name,
        "scope": _name(m.scope),
        "measurement_unit": _name(m.measurement_unit),
        "description": m.description,
    }
    restricted = [_name(t) for t in (m.restricted_metric_type or [])]
    if restricted:
        entry["restricted_metric_type"] = restricted
    return entry


def _read(config: AdLoopConfig, pid: str) -> tuple[list[dict], list[dict]]:
    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    parent = f"properties/{pid}"
    dimensions = [dimension_entry(d) for d in client.list_custom_dimensions(parent=parent)]
    metrics = [metric_entry(m) for m in client.list_custom_metrics(parent=parent)]
    return dimensions, metrics


def _usage(dimensions: list[dict], metrics: list[dict], level: str) -> dict:
    limits = LIMITS.get(level, LIMITS["standard"])
    usage = {}
    for scope in DIMENSION_SCOPES:
        used = sum(1 for d in dimensions if d["scope"] == scope)
        usage[f"{scope.lower()}_dimensions"] = {"used": used, "limit": limits[scope]}
    usage["metrics"] = {"used": len(metrics), "limit": limits["METRIC"]}
    return usage


def list_ga4_custom_definitions(config: AdLoopConfig, *, property_id: str = "") -> dict:
    """Custom dimensions and metrics with their parameters and the slots left."""
    from adloop.ga4.admin import check_property, service_level

    pid, error = check_property(property_id)
    if error:
        return {"error": error}
    dimensions, metrics = _read(config, pid)
    level = service_level(config, pid)
    usage = _usage(dimensions, metrics, level)
    insights = [
        f"{key.replace('_', ' ')}: {v['used']} of {v['limit']} slots used"
        for key, v in usage.items()
        if v["limit"] and v["used"] >= v["limit"] * 0.9
    ]
    result = {
        "property": f"properties/{pid}",
        "service_level": level,
        "custom_dimensions": sorted(dimensions, key=lambda d: (d["scope"], d["parameter_name"])),
        "custom_metrics": sorted(metrics, key=lambda m: m["parameter_name"]),
        "usage": usage,
    }
    if insights:
        result["insights"] = insights
    return result


def _validate_names(errors: list[str], *, parameter_name: str, display_name: str, description: str | None, user_scope: bool):
    if parameter_name:
        limit = MAX_USER_PARAMETER if user_scope else MAX_PARAMETER
        if not _PARAMETER.match(parameter_name):
            errors.append("parameter_name must start with a letter and contain only letters, digits and underscores")
        elif len(parameter_name) > limit:
            errors.append(f"parameter_name is {len(parameter_name)} characters; GA4 allows {limit}{' for user scope' if user_scope else ''}")
    if display_name:
        if not _DISPLAY.match(display_name):
            errors.append("display_name must start with a letter and contain only letters, digits, spaces and underscores")
        elif len(display_name) > MAX_DISPLAY:
            errors.append(f"display_name is {len(display_name)} characters; GA4 allows {MAX_DISPLAY}")
    if description and len(description) > MAX_DESCRIPTION:
        errors.append(f"description is {len(description)} characters; GA4 allows {MAX_DESCRIPTION}")


def _find(items: list[dict], key: str, resource: str, parameter_name: str, scope: str = "") -> list[dict]:
    if resource:
        return [i for i in items if i[key] == resource or i[key].rsplit("/", 1)[-1] == resource]
    return [i for i in items if i["parameter_name"] == parameter_name and (not scope or i["scope"] == scope)]


def draft_ga4_custom_dimension(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    action: str = "create",
    parameter_name: str = "",
    scope: str = "EVENT",
    display_name: str = "",
    description: str | None = None,
    disallow_ads_personalization: bool | None = None,
    custom_dimension: str = "",
) -> dict:
    """Draft creating, renaming or archiving a GA4 custom dimension — returns a PREVIEW."""
    return _draft(
        config, kind="dimension", property_id=property_id, action=action, parameter_name=parameter_name,
        scope=scope, display_name=display_name, description=description,
        disallow_ads_personalization=disallow_ads_personalization, resource=custom_dimension,
    )


def draft_ga4_custom_metric(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    action: str = "create",
    parameter_name: str = "",
    display_name: str = "",
    description: str | None = None,
    measurement_unit: str = "",
    restricted_metric_type: list[str] | None = None,
    custom_metric: str = "",
) -> dict:
    """Draft creating, changing or archiving a GA4 custom metric — returns a PREVIEW."""
    return _draft(
        config, kind="metric", property_id=property_id, action=action, parameter_name=parameter_name,
        scope="EVENT", display_name=display_name, description=description,
        measurement_unit=measurement_unit, restricted_metric_type=restricted_metric_type, resource=custom_metric,
    )


def _editable(kind: str, entry: dict) -> dict:
    """The fields an update may change, for before/after and fingerprints."""
    values = {"display_name": entry["display_name"], "description": entry.get("description", "")}
    if kind == "dimension":
        values["disallow_ads_personalization"] = entry.get("disallow_ads_personalization", False)
    else:
        values["measurement_unit"] = entry["measurement_unit"]
        values["restricted_metric_type"] = list(entry.get("restricted_metric_type", []))
    return values


def _draft(
    config: AdLoopConfig,
    *,
    kind: str,
    property_id: str,
    action: str,
    parameter_name: str,
    scope: str,
    display_name: str,
    description: str | None,
    resource: str,
    disallow_ads_personalization: bool | None = None,
    measurement_unit: str = "",
    restricted_metric_type: list[str] | None = None,
) -> dict:
    from adloop.ga4.admin import check_property, fingerprint, service_level
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    action = (action or "").strip().lower()
    if action not in _ACTIONS[kind]:
        return {"error": f"action must be one of {', '.join(_ACTIONS[kind])}"}
    operation = f"ga4_{action}_custom_{kind}"
    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    pid, error = check_property(property_id)
    errors = [error] if error else []
    parameter_name = (parameter_name or "").strip()
    display_name = (display_name or "").strip()
    scope = (scope or "EVENT").strip().upper()
    resource = (resource or "").strip()
    unit = (measurement_unit or "").strip().upper()
    restricted = [str(t).strip().upper() for t in (restricted_metric_type or []) if str(t).strip()]
    if description is not None:
        description = description.strip()

    if kind == "dimension" and scope not in DIMENSION_SCOPES:
        errors.append(f"scope must be one of {', '.join(DIMENSION_SCOPES)}")
    if unit and unit not in UNITS:
        errors.append(f"measurement_unit must be one of {', '.join(UNITS)}")
    if any(t not in RESTRICTED_TYPES for t in restricted):
        errors.append(f"restricted_metric_type takes {', '.join(RESTRICTED_TYPES)}")
    _validate_names(errors, parameter_name=parameter_name, display_name=display_name,
                    description=description, user_scope=scope == "USER")
    if action == "create" and not parameter_name:
        errors.append("parameter_name is required: the event parameter (or user property) GA4 should report on")
    if action != "create" and not (resource or parameter_name):
        errors.append(f"name the custom {kind} to {action}: its parameter_name or its resource name")
    if errors:
        return {"error": "Validation failed", "details": errors}

    dimensions, metrics = _read(config, pid)
    items, key = (dimensions, "custom_dimension") if kind == "dimension" else (metrics, "custom_metric")
    noun = f"custom {kind}"
    warnings: list[str] = []

    if action == "create":
        if kind == "dimension" and disallow_ads_personalization and scope != "USER":
            return {"error": "disallow_ads_personalization only applies to user-scoped dimensions"}
        unit = unit or "STANDARD"
        if kind == "metric" and unit == "CURRENCY" and not restricted:
            return {"error": "A CURRENCY metric needs restricted_metric_type: COST_DATA or REVENUE_DATA (GA4 requires it)"}
        if kind == "metric" and unit != "CURRENCY" and restricted:
            return {"error": "restricted_metric_type only applies to CURRENCY metrics"}
        if existing := _find(items, key, "", parameter_name, scope if kind == "dimension" else ""):
            return {"error": f"'{parameter_name}' is already registered as the {noun} '{existing[0]['display_name']}'.", key: existing[0]}
        display_name = display_name or parameter_name
        if any(i["display_name"] == display_name for i in items):
            return {"error": f"Another {noun} is already called '{display_name}'; pick a different display_name."}
        level = service_level(config, pid)
        limit_key = scope if kind == "dimension" else "METRIC"
        limit = LIMITS[level][limit_key]
        used = sum(1 for i in items if kind == "metric" or i["scope"] == scope)
        if used >= limit:
            return {"error": f"All {limit} {scope.lower() + ' ' if kind == 'dimension' else ''}{noun} slots of this {level} property are used. Archive one first (list_ga4_custom_definitions shows them)."}
        if used + 1 >= limit * 0.9:
            warnings.append(f"This uses slot {used + 1} of {limit}.")
        changes = {"property_id": pid, "parameter_name": parameter_name, "display_name": display_name}
        if kind == "dimension":
            changes["scope"] = scope
            if scope == "USER":
                changes["disallow_ads_personalization"] = bool(disallow_ads_personalization)
        else:
            changes["measurement_unit"] = unit
            if restricted:
                changes["restricted_metric_type"] = restricted
        if description:
            changes["description"] = description
        warnings += [
            "GA4 fills it from now on: events sent before the registration stay without this "
            f"{kind}, and new data can take 24 to 48 hours to show in reports.",
            f"parameter_name{' and scope' if kind == 'dimension' else ''} cannot change after creation.",
        ]
        plan = ChangePlan(operation=operation, entity_type=f"custom_{kind}", entity_id=parameter_name, changes=changes)
    else:
        matches = _find(items, key, resource, parameter_name, scope if (kind == "dimension" and not resource and scope != "EVENT") else "")
        if not matches:
            return {"error": f"No {noun} matches.", f"custom_{kind}s": [{"parameter_name": i["parameter_name"], "scope": i["scope"], "display_name": i["display_name"]} for i in items[:50]]}
        if len(matches) > 1:
            return {"error": f"'{parameter_name}' is registered in more than one scope; pass scope or the resource name.", "matches": matches}
        current = matches[0]
        before = _editable(kind, current)
        if action == "archive":
            changes = {"property_id": pid, key: current[key], "parameter_name": current["parameter_name"],
                       "display_name": current["display_name"], "fingerprint": fingerprint(before)}
            if kind == "dimension":
                changes["scope"] = current["scope"]
            plan = ChangePlan(operation=operation, entity_type=f"custom_{kind}", entity_id=current["parameter_name"],
                              changes=changes, requires_double_confirm=True)
            warnings.append(
                f"Archiving removes '{current['display_name']}' from reports and explorations and frees its slot. "
                "It cannot be undone."
            )
        else:
            after = dict(before)
            if display_name:
                if display_name != before["display_name"] and any(i["display_name"] == display_name for i in items):
                    return {"error": f"Another {noun} is already called '{display_name}'."}
                after["display_name"] = display_name
            if description is not None:
                after["description"] = description
            if kind == "dimension" and disallow_ads_personalization is not None:
                if current["scope"] != "USER":
                    return {"error": "disallow_ads_personalization only applies to user-scoped dimensions"}
                after["disallow_ads_personalization"] = bool(disallow_ads_personalization)
            if kind == "metric":
                if unit:
                    after["measurement_unit"] = unit
                if restricted_metric_type is not None:
                    after["restricted_metric_type"] = restricted
                if after["measurement_unit"] == "CURRENCY" and not after["restricted_metric_type"]:
                    return {"error": "A CURRENCY metric needs restricted_metric_type: COST_DATA or REVENUE_DATA"}
                if after["measurement_unit"] != "CURRENCY" and after["restricted_metric_type"]:
                    after["restricted_metric_type"] = []
            changed = [k for k in after if after[k] != before[k]]
            if not changed:
                return {"status": "NO_CHANGE", "message": f"The {noun} '{current['display_name']}' already has these values.", key: current}
            changes = {"property_id": pid, key: current[key], "parameter_name": current["parameter_name"]}
            for field in changed:
                changes[field] = after[field]
                changes[f"current_{field}"] = before[field]
            changes["after"] = after
            changes["fingerprint"] = fingerprint(before)
            plan = ChangePlan(operation=operation, entity_type=f"custom_{kind}", entity_id=current["parameter_name"], changes=changes)
            if "measurement_unit" in changed:
                warnings.append("A new measurement unit changes how GA4 shows the metric in every report.")

    plan.platform = "ga4"
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _current(config: AdLoopConfig, kind: str, name: str) -> dict:
    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    if kind == "dimension":
        return dimension_entry(client.get_custom_dimension(name=name))
    return metric_entry(client.get_custom_metric(name=name))


def _check_unchanged(config: AdLoopConfig, kind: str, changes: dict) -> dict:
    from adloop.ga4.admin import fingerprint

    key = f"custom_{kind}"
    current = _current(config, kind, changes[key])
    if fingerprint(_editable(kind, current)) != changes["fingerprint"]:
        raise RuntimeError(
            f"The custom {kind} '{current['display_name']}' was changed in GA4 after the preview; draft the change again."
        )
    return current


def _kind(operation: str) -> str:
    return "dimension" if operation.endswith("_dimension") else "metric"


def preflight(config: AdLoopConfig, plan) -> dict:
    """Dry run: re-check duplicates and the limit (create) or that nothing changed (update, archive)."""
    from adloop.ga4.admin import service_level

    kind = _kind(plan.operation)
    changes = plan.changes
    if plan.operation.startswith("ga4_create_"):
        dimensions, metrics = _read(config, changes["property_id"])
        items = dimensions if kind == "dimension" else metrics
        scope = changes.get("scope", "EVENT")
        if _find(items, f"custom_{kind}", "", changes["parameter_name"], scope if kind == "dimension" else ""):
            raise RuntimeError(f"'{changes['parameter_name']}' was registered in the meantime.")
        level = service_level(config, changes["property_id"])
        limit = LIMITS[level][scope if kind == "dimension" else "METRIC"]
        used = sum(1 for i in items if kind == "metric" or i["scope"] == scope)
        if used >= limit:
            raise RuntimeError(f"All {limit} slots are used now.")
        return {"slots_left_after_apply": limit - used - 1}
    _check_unchanged(config, kind, changes)
    return {f"custom_{kind}_unchanged": True}


def _apply_create(config: AdLoopConfig, changes: dict, kind: str) -> dict:
    from google.analytics.admin_v1beta import types

    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    parent = f"properties/{changes['property_id']}"
    if kind == "dimension":
        created = client.create_custom_dimension(parent=parent, custom_dimension=types.CustomDimension(
            parameter_name=changes["parameter_name"],
            display_name=changes["display_name"],
            description=changes.get("description", ""),
            scope=types.CustomDimension.DimensionScope[changes["scope"]],
            disallow_ads_personalization=bool(changes.get("disallow_ads_personalization", False)),
        ))
        return {"resource_names": [created.name], **dimension_entry(created)}
    created = client.create_custom_metric(parent=parent, custom_metric=types.CustomMetric(
        parameter_name=changes["parameter_name"],
        display_name=changes["display_name"],
        description=changes.get("description", ""),
        scope=types.CustomMetric.MetricScope.EVENT,
        measurement_unit=types.CustomMetric.MeasurementUnit[changes["measurement_unit"]],
        restricted_metric_type=[types.CustomMetric.RestrictedMetricType[t] for t in changes.get("restricted_metric_type", [])],
    ))
    return {"resource_names": [created.name], **metric_entry(created)}


def _apply_update(config: AdLoopConfig, changes: dict, kind: str) -> dict:
    from google.analytics.admin_v1beta import types
    from google.protobuf import field_mask_pb2

    from adloop.ga4.client import get_admin_client

    current = _check_unchanged(config, kind, changes)
    after, before = changes["after"], _editable(kind, current)
    paths = [k for k in after if after[k] != before[k]]
    client = get_admin_client(config)
    mask = field_mask_pb2.FieldMask(paths=paths)
    if kind == "dimension":
        updated = client.update_custom_dimension(custom_dimension=types.CustomDimension(
            name=changes["custom_dimension"], display_name=after["display_name"], description=after["description"],
            disallow_ads_personalization=bool(after["disallow_ads_personalization"]),
        ), update_mask=mask)
        return {"resource_names": [updated.name], **dimension_entry(updated)}
    updated = client.update_custom_metric(custom_metric=types.CustomMetric(
        name=changes["custom_metric"], display_name=after["display_name"], description=after["description"],
        measurement_unit=types.CustomMetric.MeasurementUnit[after["measurement_unit"]],
        restricted_metric_type=[types.CustomMetric.RestrictedMetricType[t] for t in after["restricted_metric_type"]],
    ), update_mask=mask)
    return {"resource_names": [updated.name], **metric_entry(updated)}


def _apply_archive(config: AdLoopConfig, changes: dict, kind: str) -> dict:
    from adloop.ga4.client import get_admin_client

    _check_unchanged(config, kind, changes)
    client = get_admin_client(config)
    key = f"custom_{kind}"
    if kind == "dimension":
        client.archive_custom_dimension(name=changes[key])
    else:
        client.archive_custom_metric(name=changes[key])
    return {"resource_names": [changes[key]], "parameter_name": changes["parameter_name"], "archived": True}


APPLIERS = {
    f"ga4_{action}_custom_{kind}": (lambda config, changes, _a=action, _k=kind: {
        "create": _apply_create, "update": _apply_update, "archive": _apply_archive,
    }[_a](config, changes, _k))
    for kind in ("dimension", "metric")
    for action in ("create", "update", "archive")
}
