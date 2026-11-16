"""GA4 audiences: list them, create, rename or archive one.

An audience is a group of users defined by what they did: the events they
sent, the pages they viewed, or a sequence of both in order. GA4 shares its audiences with every linked Google Ads account that
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
# A page condition is a page_view event whose page_location (the full URL)
# contains the text. GA4 names an event parameter by its plain name here.
_PAGE_EVENT, _PAGE_FIELD = "page_view", "page_location"
MAX_PAGE_LENGTH = 100
MAX_SEQUENCE_STEPS = 10
_STEP_KEYS = {"event", "page", "directly_followed", "within_minutes"}
OPERATIONS = {action: f"ga4_{action}_audience" for action in _ACTIONS}


def _enum(value) -> str:
    return getattr(value, "name", "") or str(value or "")


def _condition(expression) -> dict | None:
    """``{"event": name}`` or ``{"page": text}`` for a filter this module writes, else None."""
    event = getattr(expression, "event_filter", None)
    name = getattr(event, "event_name", "")
    if not name:
        return None
    params = getattr(getattr(event, "event_parameter_filter_expression", None), "and_group", None)
    groups = list(getattr(params, "filter_expressions", None) or [])
    if not groups:
        return {"event": name}
    inner = list(getattr(getattr(groups[0], "or_group", None), "filter_expressions", None) or [])
    if name != _PAGE_EVENT or len(groups) != 1 or len(inner) != 1:
        return None
    field = getattr(inner[0], "dimension_or_metric_filter", None)
    text = getattr(field, "string_filter", None)
    if getattr(field, "field_name", "") != _PAGE_FIELD or _enum(getattr(text, "match_type", "")) != "CONTAINS":
        return None
    return {"page": text.value} if text.value else None


def _any_of(expression) -> list[dict] | None:
    """The conditions of one AND group holding one OR group, else None."""
    groups = list(getattr(getattr(expression, "and_group", None), "filter_expressions", None) or [])
    if len(groups) != 1:
        return None
    items = list(getattr(getattr(groups[0], "or_group", None), "filter_expressions", None) or [])
    conditions = [_condition(item) for item in items]
    return conditions if conditions and all(conditions) else None


def _clause_sequence(clause) -> list[dict] | None:
    """The steps of a sequence clause this module could have written, else None."""
    steps = []
    for position, step in enumerate(getattr(getattr(clause, "sequence_filter", None), "sequence_steps", None) or []):
        conditions = _any_of(getattr(step, "filter_expression", None))
        if not conditions or len(conditions) != 1:
            return None
        entry = dict(conditions[0])
        if position and getattr(step, "immediately_follows", False):
            entry["directly_followed"] = True
        within = getattr(step, "constraint_duration", None)
        seconds = within.total_seconds() if hasattr(within, "total_seconds") else getattr(within, "seconds", 0)
        if position and seconds:
            entry["within_minutes"] = int(seconds // 60)
        steps.append(entry)
    return steps or None


def _is_sequence(clause) -> bool:
    return bool(list(getattr(getattr(clause, "sequence_filter", None), "sequence_steps", None) or []))


def _split(conditions: list[dict]) -> tuple[list[str], list[str]]:
    return [c["event"] for c in conditions if "event" in c], [c["page"] for c in conditions if "page" in c]


def audience_entry(audience) -> dict:
    entry = {
        "audience": audience.name,
        "display_name": audience.display_name,
        "description": audience.description,
        "membership_duration_days": int(audience.membership_duration_days or 0),
        "ads_personalization_enabled": bool(audience.ads_personalization_enabled),
    }
    include: list[dict] | None = None
    sequence: list[dict] | None = None
    exclude: list[dict] = []
    custom = False
    for clause in audience.filter_clauses or []:
        excluded = _enum(clause.clause_type) == "EXCLUDE"
        if _is_sequence(clause):
            steps = _clause_sequence(clause)
            # This module writes one sequence, as the include clause.
            if steps is None or excluded or sequence is not None or include is not None:
                custom = True
            else:
                sequence = steps
            continue
        conditions = _any_of(getattr(getattr(clause, "simple_filter", None), "filter_expression", None))
        if conditions is None:
            custom = True
        elif excluded:
            exclude += conditions
        elif include is not None or sequence is not None:
            # Clauses are AND'ed; this module only ever writes one include clause.
            custom = True
        else:
            include = conditions
    if not audience.filter_clauses:
        # "All Users" and "Purchasers", which GA4 creates itself: the API
        # returns no conditions for them.
        entry["predefined"] = True
    elif custom or (include is None and sequence is None):
        # Built in GA4's own editor with conditions this module does not write.
        entry["custom_conditions"] = True
        try:
            entry["conditions"] = [type(c).to_dict(c, use_integers_for_enums=False) for c in audience.filter_clauses]
        except Exception:  # noqa: BLE001 - only there to show what the audience matches
            pass
    else:
        if sequence is not None:
            entry["sequence"] = sequence
        else:
            events, pages = _split(include)
            if events:
                entry["include_events"] = events
            if pages:
                entry["include_pages"] = pages
        if exclude:
            events, pages = _split(exclude)
            if events:
                entry["exclude_events"] = events
            if pages:
                entry["exclude_pages"] = pages
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


def _pages(label: str, values: list[str] | None, errors: list[str]) -> list[str]:
    pages = list(dict.fromkeys(str(v).strip() for v in values or [] if str(v).strip()))
    for page in pages:
        if len(page) > MAX_PAGE_LENGTH:
            errors.append(f"{label} '{page[:40]}...' is longer than {MAX_PAGE_LENGTH} characters")
        elif page == "/":
            errors.append(f"{label} '/' is part of every URL; use include_events=['session_start'] for every visitor")
    return pages


def _steps(values: list | None, errors: list[str]) -> list[dict]:
    """Sequence steps as ``{"event" | "page", directly_followed?, within_minutes?}``."""
    steps: list[dict] = []
    for position, raw in enumerate(values or []):
        where = f"sequence step {position + 1}"
        step = {"event": raw} if isinstance(raw, str) else raw
        if not isinstance(step, dict):
            errors.append(f"{where}: an event name, or an object with event or page")
            continue
        unknown = set(step) - _STEP_KEYS
        if unknown:
            errors.append(f"{where}: unknown key(s) {sorted(unknown)}; use event, page, directly_followed, within_minutes")
            continue
        event, page = str(step.get("event") or "").strip(), str(step.get("page") or "").strip()
        if bool(event) == bool(page):
            errors.append(f"{where}: give exactly one of event or page")
            continue
        entry = {"event": _events(f"{where}:", [event], errors)[0]} if event else {"page": page}
        if page:
            _pages(f"{where}:", [page], errors)
        if step.get("directly_followed"):
            if position == 0:
                errors.append(f"{where}: directly_followed applies from the second step on")
            entry["directly_followed"] = True
        within = step.get("within_minutes")
        if within is not None:
            if position == 0 or isinstance(within, bool) or not isinstance(within, int) or within < 1:
                errors.append(f"{where}: within_minutes is a whole number of minutes, from the second step on")
            else:
                entry["within_minutes"] = within
        steps.append(entry)
    if values and not errors and not 2 <= len(steps) <= MAX_SEQUENCE_STEPS:
        errors.append(f"sequence needs 2 to {MAX_SEQUENCE_STEPS} steps")
    return steps


def _label(condition: dict) -> str:
    return f"viewed a page containing '{condition['page']}'" if "page" in condition else f"sent {condition['event']}"


def _conditions(events: list[str], pages: list[str]) -> list[dict]:
    return [{"event": e} for e in events] + [{"page": p} for p in pages]


def _find(audiences: list[dict], audience: str) -> list[dict]:
    wanted = audience.strip()
    by_resource = [a for a in audiences if a["audience"] == wanted or a["audience"].rsplit("/", 1)[-1] == wanted]
    return by_resource or [a for a in audiences if a["display_name"] == wanted]


def _editable(entry: dict) -> dict:
    return {"display_name": entry["display_name"], "description": entry["description"]}


def _phrase(conditions: list[dict]) -> str:
    events, pages = _split(conditions)
    parts = []
    if events:
        parts.append(f"sent {' or '.join(events)}")
    if pages:
        quoted = " or ".join(f"'{page}'" for page in pages)
        parts.append(f"viewed a page containing {quoted}")
    return " or ".join(parts)


def _generated_description(include: list[dict], exclude: list[dict], sequence: list[dict], days: int) -> str:
    if sequence:
        text = "Users who " + ", then ".join(_label(step) for step in sequence)
    else:
        text = f"Users who {_phrase(include)}"
    if exclude and not _split(exclude)[1]:
        text += f", without {' or '.join(_split(exclude)[0])}"
    elif exclude:
        text += f"; not those who {_phrase(exclude)}"
    # GA4 caps the description at 150 characters.
    return f"{text[:138].rstrip()}; {days} days."


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
    include_pages: list[str] | None = None,
    exclude_pages: list[str] | None = None,
    sequence: list | None = None,
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
    in_pages = _pages("include_pages:", include_pages, errors)
    out_pages = _pages("exclude_pages:", exclude_pages, errors)
    steps = _steps(sequence, errors)
    has_exclusion = bool(exclude or out_pages)

    if action == "create":
        if not display_name:
            errors.append("display_name is required")
        if steps and (include or in_pages):
            errors.append("pass either sequence or include_events / include_pages, not both")
        elif not (include or in_pages or sequence):
            errors.append(
                "say who joins: include_events (e.g. ['session_start'] for every visitor, ['purchase'] for "
                "buyers), include_pages (e.g. ['/pricing']) or a sequence of steps"
            )
        overlap = sorted(set(include) & set(exclude)) + sorted(set(in_pages) & set(out_pages))
        if overlap:
            errors.append(f"{', '.join(overlap)} cannot be both included and excluded")
        try:
            days = int(membership_duration_days)
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= MAX_MEMBERSHIP_DAYS:
            errors.append(f"membership_duration_days must be between 1 and {MAX_MEMBERSHIP_DAYS}")
        if exclude_permanently and not has_exclusion:
            errors.append("exclude_permanently needs exclude_events or exclude_pages")
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
            "description": description or _generated_description(
                _conditions(include, in_pages), _conditions(exclude, out_pages), steps, days),
            "membership_duration_days": days,
        }
        for key, value in (("sequence", steps), ("include_events", include), ("include_pages", in_pages),
                           ("exclude_events", exclude), ("exclude_pages", out_pages)):
            if value:
                changes[key] = value
        if has_exclusion:
            changes["exclude_permanently"] = bool(exclude_permanently)
        warnings += [
            "The conditions and the membership duration cannot be changed after creation; only the name and "
            "description can. A wrong audience has to be archived and created again.",
            "GA4 fills it from now on, and adds users who met the conditions in the last 30 days for its use "
            "in Google Ads. It takes one to two days to appear in a linked Google Ads account.",
        ]
        if in_pages or out_pages or any("page" in step for step in steps):
            warnings.append(
                "A page matches when its full URL contains the text, without regard to upper and lower case: "
                "'/pricing' also matches '/pricing/enterprise' and '/en/pricing'. Page views are counted from "
                "GA4's page_view event."
            )
        if steps:
            warnings.append(
                "The steps have to happen in this order; a user who did them in another order does not join."
            )
        if has_exclusion and exclude_permanently:
            kept_out = " or ".join(_label(c) for c in _conditions(exclude, out_pages))
            warnings.append(f"Users who ever {kept_out} are kept out for good, also after they stop.")
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
            if include or exclude or in_pages or out_pages or steps:
                return {"error": "GA4 fixes an audience's conditions at creation. Only display_name and description "
                                 "can change; archive the audience and create a new one for other conditions."}
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


def _expression(types, conditions: list[dict]):
    """Users who met any of ``conditions``, at any time.

    GA4 wants an AND group at the top that holds OR groups, and event
    filters only inside an OR group.
    """
    def group(kind: str, items: list):
        return types.AudienceFilterExpression(**{kind: types.AudienceFilterExpressionList(filter_expressions=items)})

    def event_filter(condition: dict):
        if "event" in condition:
            return types.AudienceEventFilter(event_name=condition["event"])
        field = types.AudienceDimensionOrMetricFilter(
            field_name=_PAGE_FIELD,
            string_filter=types.AudienceDimensionOrMetricFilter.StringFilter(
                match_type=types.AudienceDimensionOrMetricFilter.StringFilter.MatchType.CONTAINS,
                value=condition["page"],
            ),
            at_any_point_in_time=True,
        )
        return types.AudienceEventFilter(
            event_name=_PAGE_EVENT,
            event_parameter_filter_expression=group("and_group", [
                group("or_group", [types.AudienceFilterExpression(dimension_or_metric_filter=field)])
            ]),
        )

    return group("and_group", [
        group("or_group", [types.AudienceFilterExpression(event_filter=event_filter(c)) for c in conditions])
    ])


def _clause(types, clause_type: str, conditions: list[dict]):
    return types.AudienceFilterClause(
        clause_type=types.AudienceFilterClause.AudienceClauseType[clause_type],
        simple_filter=types.AudienceSimpleFilter(
            scope=types.AudienceFilterScope[_SCOPE], filter_expression=_expression(types, conditions)),
    )


def _sequence_clause(types, steps: list[dict]):
    from datetime import timedelta

    scope = types.AudienceFilterScope[_SCOPE]
    built = []
    for step in steps:
        kwargs = {}
        if step.get("directly_followed"):
            kwargs["immediately_follows"] = True
        if step.get("within_minutes"):
            kwargs["constraint_duration"] = timedelta(minutes=step["within_minutes"])
        built.append(types.AudienceSequenceFilter.AudienceSequenceStep(
            scope=scope, filter_expression=_expression(types, [step]), **kwargs))
    return types.AudienceFilterClause(
        clause_type=types.AudienceFilterClause.AudienceClauseType["INCLUDE"],
        sequence_filter=types.AudienceSequenceFilter(scope=scope, sequence_steps=built),
    )


def build_audience(changes: dict):
    """The Audience message for a create plan."""
    from google.analytics.admin_v1alpha import types

    if changes.get("sequence"):
        clauses = [_sequence_clause(types, changes["sequence"])]
    else:
        clauses = [_clause(types, "INCLUDE", _conditions(changes.get("include_events") or [],
                                                         changes.get("include_pages") or []))]
    kwargs = {}
    excluded = _conditions(changes.get("exclude_events") or [], changes.get("exclude_pages") or [])
    if excluded:
        clauses.append(_clause(types, "EXCLUDE", excluded))
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
