"""GA4 audiences: list them, create, rename or archive one.

An audience is a group of users defined by what they did (the events they
sent). GA4 shares its audiences with every linked Google Ads account that
allows personalized advertising, where they show up as remarketing lists
(get_audience_lists) and can be attached to campaigns
(draft_audience_attachment). That makes a GA4 audience the way to build a
remarketing list without another tag on the site.

The API is v1alpha; its errors come back as a plain message. GA4 fixes an
audience's conditions and membership duration at creation: only the name
and description can change afterwards, and archiving is permanent.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Per-property limits (GA4 help: "Create, edit, and archive audiences").
LIMITS = {"standard": 100, "360": 400}
MAX_MEMBERSHIP_DAYS = 540
# Google Ads serves a list from 100 active users in the last 30 days.
ADS_MIN_ACTIVE_USERS = 100
_EVENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_ACTIONS = ("create", "update", "archive")
_SCOPE = "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS"
OPERATIONS = {action: f"ga4_{action}_audience" for action in _ACTIONS}


def _enum(value) -> str:
    return getattr(value, "name", "") or str(value or "")


def _clause_events(clause) -> list[str] | None:
    """Event names of a clause this module could have written, else None.

    The shape is one AND group of one OR group of plain event filters.
    Anything else (sequences, parameter or dimension filters) is an
    audience built elsewhere, reported as ``custom_conditions``.
    """
    simple = getattr(clause, "simple_filter", None)
    expression = getattr(simple, "filter_expression", None)
    groups = list(getattr(getattr(expression, "and_group", None), "filter_expressions", None) or [])
    if len(groups) != 1:
        return None
    events = []
    for item in list(getattr(getattr(groups[0], "or_group", None), "filter_expressions", None) or []):
        event = getattr(item, "event_filter", None)
        name = getattr(event, "event_name", "")
        if not name or getattr(getattr(event, "event_parameter_filter_expression", None), "and_group", None):
            return None
        events.append(name)
    return events or None


def audience_entry(audience) -> dict:
    entry = {
        "audience": audience.name,
        "display_name": audience.display_name,
        "description": audience.description,
        "membership_duration_days": int(audience.membership_duration_days or 0),
        "ads_personalization_enabled": bool(audience.ads_personalization_enabled),
    }
    include: list[str] = []
    exclude: list[str] = []
    custom = False
    for clause in audience.filter_clauses or []:
        events = _clause_events(clause)
        if events is None:
            custom = True
        elif _enum(clause.clause_type) == "EXCLUDE":
            exclude += events
        else:
            # Clauses are AND'ed; this module only ever writes one include clause.
            custom = custom or bool(include)
            include += events
    if not audience.filter_clauses:
        # "All Users" and "Purchasers", which GA4 creates itself: the API
        # returns no conditions for them.
        entry["predefined"] = True
    elif custom or not include:
        # Built in GA4's own editor with conditions this module does not write.
        entry["custom_conditions"] = True
        try:
            entry["conditions"] = [type(c).to_dict(c, use_integers_for_enums=False) for c in audience.filter_clauses]
        except Exception:  # noqa: BLE001 - only there to show what the audience matches
            pass
    else:
        entry["include_events"] = include
        if exclude:
            entry["exclude_events"] = exclude
            entry["exclude_permanently"] = _enum(audience.exclusion_duration_mode) == "EXCLUDE_PERMANENTLY"
    return entry


def _read(config: AdLoopConfig, pid: str) -> list[dict]:
    from adloop.ga4.client import get_admin_alpha_client

    return [audience_entry(a) for a in get_admin_alpha_client(config).list_audiences(parent=f"properties/{pid}")]


def ads_readiness(config: AdLoopConfig, pid: str) -> list[dict]:
    """What decides whether this property's audiences reach Google Ads.

    Each check is ``{"check", "status" (ok / warn / info), "detail"}``. A
    setting that cannot be read is reported as info, never as a pass.
    """
    from adloop.ga4.client import get_admin_alpha_client, get_admin_client

    checks: list[dict] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    try:
        links = list(get_admin_client(config).list_google_ads_links(parent=f"properties/{pid}"))
    except Exception as e:  # noqa: BLE001
        links = None
        add("google_ads_link", "info", f"The Google Ads links could not be read: {str(e)[:160]}")
    if links == []:
        add("google_ads_link", "warn",
            "No Google Ads account is linked to this property, so its audiences reach no ad account "
            "(draft_conversion_tracking_setup links one).")
    elif links:
        on = [link.customer_id for link in links if link.ads_personalization_enabled is not False]
        off = [link.customer_id for link in links if link.ads_personalization_enabled is False]
        if on:
            add("google_ads_link", "ok", f"Audiences are shared with Google Ads {', '.join(on)}.")
        if off:
            add("ads_personalization", "warn",
                f"The link to Google Ads {', '.join(off)} has personalized advertising switched off, so that "
                "account receives no audiences. Switch it on in GA4 under Admin > Product links > Google Ads links.")

    try:
        signals = get_admin_alpha_client(config).get_google_signals_settings(
            name=f"properties/{pid}/googleSignalsSettings")
        state = _enum(signals.state)
    except Exception as e:  # noqa: BLE001 - v1alpha
        state = ""
        add("google_signals", "info", f"The Google signals setting could not be read: {str(e)[:160]}")
    if state == "GOOGLE_SIGNALS_ENABLED":
        add("google_signals", "ok", "Google signals are on.")
    elif state == "GOOGLE_SIGNALS_DISABLED":
        add("google_signals", "warn",
            "Google signals are off. For remarketing GA4 needs Google signals or user-provided data collection "
            "switched on (Admin > Data collection); whether user-provided data is on cannot be read here.")

    add("consent", "info",
        "Visitors only join an audience used for ads when they allowed it: a site with a consent banner has to "
        "pass that choice to Google (consent mode), otherwise the list stays small or empty.")
    add("list_size", "info",
        f"Google Ads serves a list from {ADS_MIN_ACTIVE_USERS} active users in the last 30 days. A new audience "
        "appears in Google Ads within about two days; get_audience_lists shows its size there.")
    return checks


def list_ga4_audiences(config: AdLoopConfig, *, property_id: str = "") -> dict:
    """The property's audiences, the slots left and whether they reach Google Ads."""
    from adloop.ga4.admin import check_property, service_level

    pid, error = check_property(property_id)
    if error:
        return {"error": error}
    audiences = _read(config, pid)
    level = service_level(config, pid)
    readiness = ads_readiness(config, pid)
    return {
        "property": f"properties/{pid}",
        "service_level": level,
        "audiences": sorted(audiences, key=lambda a: a["display_name"].lower()),
        "usage": {"used": len(audiences), "limit": LIMITS[level]},
        "google_ads_readiness": readiness,
        "insights": [c["detail"] for c in readiness if c["status"] == "warn"],
    }


def _events(label: str, values: list[str] | None, errors: list[str]) -> list[str]:
    names = list(dict.fromkeys(str(v).strip() for v in values or [] if str(v).strip()))
    for name in names:
        if not _EVENT.match(name):
            errors.append(
                f"{label} '{name}' is not a GA4 event name (a letter first, then letters, digits and "
                "underscores, at most 40 characters)"
            )
    return names


def _find(audiences: list[dict], audience: str) -> list[dict]:
    wanted = audience.strip()
    by_resource = [a for a in audiences if a["audience"] == wanted or a["audience"].rsplit("/", 1)[-1] == wanted]
    return by_resource or [a for a in audiences if a["display_name"] == wanted]


def _editable(entry: dict) -> dict:
    return {"display_name": entry["display_name"], "description": entry["description"]}


def _generated_description(include: list[str], exclude: list[str], days: int) -> str:
    text = f"Users who sent {' or '.join(include)}"
    if exclude:
        text += f", without {' or '.join(exclude)}"
    return f"{text}; {days} days."


def draft_ga4_audience(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    action: str = "create",
    display_name: str = "",
    description: str | None = None,
    include_events: list[str] | None = None,
    exclude_events: list[str] | None = None,
    membership_duration_days: int = 30,
    exclude_permanently: bool = False,
    audience: str = "",
) -> dict:
    """Draft creating, renaming or archiving a GA4 audience: returns a PREVIEW."""
    from adloop.ga4.admin import check_property, fingerprint, service_level
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return {"error": f"action must be one of {', '.join(_ACTIONS)}"}
    operation = OPERATIONS[action]
    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    pid, error = check_property(property_id)
    errors = [error] if error else []
    display_name = (display_name or "").strip()
    audience = (audience or "").strip()
    if description is not None:
        description = description.strip()
    include = _events("include_events:", include_events, errors)
    exclude = _events("exclude_events:", exclude_events, errors)

    if action == "create":
        if not display_name:
            errors.append("display_name is required")
        if not include:
            errors.append(
                "include_events is required: the events that put a user in the audience, e.g. "
                "['session_start'] for every visitor or ['purchase'] for buyers"
            )
        overlap = sorted(set(include) & set(exclude))
        if overlap:
            errors.append(f"{', '.join(overlap)} cannot be both included and excluded")
        try:
            days = int(membership_duration_days)
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= MAX_MEMBERSHIP_DAYS:
            errors.append(f"membership_duration_days must be between 1 and {MAX_MEMBERSHIP_DAYS}")
        if exclude_permanently and not exclude:
            errors.append("exclude_permanently needs exclude_events")
    elif not audience:
        errors.append(f"name the audience to {action}: its display name or its resource name (list_ga4_audiences)")
    if errors:
        return {"error": "Validation failed", "details": errors}

    audiences = _read(config, pid)
    warnings: list[str] = []

    if action == "create":
        if any(a["display_name"] == display_name for a in audiences):
            return {"error": f"An audience called '{display_name}' already exists; pick a different display_name."}
        level = service_level(config, pid)
        limit = LIMITS[level]
        if len(audiences) >= limit:
            return {"error": f"All {limit} audience slots of this {level} property are used. Archive one first "
                             "(list_ga4_audiences shows them)."}
        changes = {
            "property_id": pid,
            "display_name": display_name,
            "description": description or _generated_description(include, exclude, days),
            "include_events": include,
            "membership_duration_days": days,
        }
        if exclude:
            changes["exclude_events"] = exclude
            changes["exclude_permanently"] = bool(exclude_permanently)
        warnings += [
            "The events and the membership duration cannot be changed after creation; only the name and "
            "description can. A wrong audience has to be archived and created again.",
            "GA4 fills it from now on, and adds users who met the conditions in the last 30 days for its use "
            "in Google Ads. It takes one to two days to appear in a linked Google Ads account.",
        ]
        if exclude and exclude_permanently:
            warnings.append(
                f"Users who ever sent {' or '.join(exclude)} are kept out for good, also after they stop."
            )
        warnings += [c["detail"] for c in ads_readiness(config, pid) if c["status"] == "warn" or c["check"] == "consent"]
        plan = ChangePlan(operation=operation, entity_type="ga4_audience", entity_id=display_name, changes=changes)
    else:
        matches = _find(audiences, audience)
        if not matches:
            return {"error": "No audience matches.",
                    "audiences": [{"audience": a["audience"], "display_name": a["display_name"]} for a in audiences[:50]]}
        if len(matches) > 1:
            return {"error": f"More than one audience is called '{audience}'; pass the resource name.",
                    "matches": matches}
        current = matches[0]
        before = _editable(current)
        changes = {"property_id": pid, "audience": current["audience"], "fingerprint": fingerprint(before)}
        if action == "archive":
            changes["display_name"] = current["display_name"]
            plan = ChangePlan(operation=operation, entity_type="ga4_audience", entity_id=current["display_name"],
                              changes=changes, requires_double_confirm=True)
            warnings.append(
                f"Archiving removes '{current['display_name']}' for good: it cannot be restored, and Google Ads "
                "campaigns that use the list lose it. Check get_audience_targeting on the campaigns first."
            )
        else:
            after = dict(before)
            if display_name:
                if display_name != before["display_name"] and any(a["display_name"] == display_name for a in audiences):
                    return {"error": f"An audience called '{display_name}' already exists."}
                after["display_name"] = display_name
            if description is not None:
                if not description:
                    return {"error": "GA4 requires a description; it cannot be emptied."}
                after["description"] = description
            if include or exclude:
                return {"error": "GA4 fixes an audience's events at creation. Only display_name and description "
                                 "can change; archive the audience and create a new one for other events."}
            changed = [k for k in after if after[k] != before[k]]
            if not changed:
                return {"status": "NO_CHANGE", "message": f"The audience '{current['display_name']}' already has these values.",
                        "audience": current}
            for field in changed:
                changes[field] = after[field]
                changes[f"current_{field}"] = before[field]
            changes["after"] = after
            plan = ChangePlan(operation=operation, entity_type="ga4_audience", entity_id=current["display_name"],
                              changes=changes)

    plan.platform = "ga4"
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _check_unchanged(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.admin import fingerprint
    from adloop.ga4.client import get_admin_alpha_client

    current = audience_entry(get_admin_alpha_client(config).get_audience(name=changes["audience"]))
    if fingerprint(_editable(current)) != changes["fingerprint"]:
        raise RuntimeError(
            f"The audience '{current['display_name']}' was changed in GA4 after the preview; draft the change again."
        )
    return current


def preflight(config: AdLoopConfig, plan) -> dict:
    """Dry run: re-check the name and the limit (create) or that nothing changed (update, archive)."""
    from adloop.ga4.admin import service_level

    changes = plan.changes
    if plan.operation != OPERATIONS["create"]:
        _check_unchanged(config, changes)
        return {"audience_unchanged": True}
    audiences = _read(config, changes["property_id"])
    if any(a["display_name"] == changes["display_name"] for a in audiences):
        raise RuntimeError(f"An audience called '{changes['display_name']}' was created in the meantime.")
    limit = LIMITS[service_level(config, changes["property_id"])]
    if len(audiences) >= limit:
        raise RuntimeError(f"All {limit} audience slots are used now.")
    return {"slots_left_after_apply": limit - len(audiences) - 1}


def _clause(types, clause_type: str, events: list[str]):
    """One clause: users who sent any of ``events``, at any time.

    GA4 wants an AND group at the top that holds OR groups, and event
    filters only inside an OR group.
    """
    return types.AudienceFilterClause(
        clause_type=types.AudienceFilterClause.AudienceClauseType[clause_type],
        simple_filter=types.AudienceSimpleFilter(
            scope=types.AudienceFilterScope[_SCOPE],
            filter_expression=types.AudienceFilterExpression(
                and_group=types.AudienceFilterExpressionList(filter_expressions=[
                    types.AudienceFilterExpression(or_group=types.AudienceFilterExpressionList(filter_expressions=[
                        types.AudienceFilterExpression(event_filter=types.AudienceEventFilter(event_name=name))
                        for name in events
                    ]))
                ])
            ),
        ),
    )


def build_audience(changes: dict):
    """The Audience message for a create plan."""
    from google.analytics.admin_v1alpha import types

    clauses = [_clause(types, "INCLUDE", changes["include_events"])]
    kwargs = {}
    if changes.get("exclude_events"):
        clauses.append(_clause(types, "EXCLUDE", changes["exclude_events"]))
        mode = "EXCLUDE_PERMANENTLY" if changes.get("exclude_permanently") else "EXCLUDE_TEMPORARILY"
        kwargs["exclusion_duration_mode"] = types.Audience.AudienceExclusionDurationMode[mode]
    return types.Audience(
        display_name=changes["display_name"],
        description=changes["description"],
        membership_duration_days=changes["membership_duration_days"],
        filter_clauses=clauses,
        **kwargs,
    )


def _apply_create(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.client import get_admin_alpha_client

    created = get_admin_alpha_client(config).create_audience(
        parent=f"properties/{changes['property_id']}", audience=build_audience(changes))
    return {"resource_names": [created.name], **audience_entry(created)}


def _apply_update(config: AdLoopConfig, changes: dict) -> dict:
    from google.analytics.admin_v1alpha import types
    from google.protobuf import field_mask_pb2

    from adloop.ga4.client import get_admin_alpha_client

    current = _check_unchanged(config, changes)
    after, before = changes["after"], _editable(current)
    paths = [k for k in after if after[k] != before[k]]
    updated = get_admin_alpha_client(config).update_audience(
        audience=types.Audience(name=changes["audience"], display_name=after["display_name"],
                                description=after["description"]),
        update_mask=field_mask_pb2.FieldMask(paths=paths),
    )
    return {"resource_names": [updated.name], **audience_entry(updated)}


def _apply_archive(config: AdLoopConfig, changes: dict) -> dict:
    from adloop.ga4.client import get_admin_alpha_client

    _check_unchanged(config, changes)
    # archive_audience takes the request only, no flattened name argument.
    get_admin_alpha_client(config).archive_audience(request={"name": changes["audience"]})
    return {"resource_names": [changes["audience"]], "display_name": changes["display_name"], "archived": True}


APPLIERS = {
    OPERATIONS["create"]: _apply_create,
    OPERATIONS["update"]: _apply_update,
    OPERATIONS["archive"]: _apply_archive,
}
