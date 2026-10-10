"""GTM read helpers — fetch the live (published) container version and parse tags."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.gtm.entities import ENTITY_ID_FIELDS, ENTITY_KINDS

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


GA4_EVENT_TAG = "gaawe"
GA4_CONFIG_TAG = "googtag"
ADS_CONVERSION_TAG = "awct"
ADS_CONVERSION_LINKER = "gclidw"
ADS_REMARKETING_TAG = "sp"
CUSTOM_HTML = "html"


# Built-in trigger IDs are >= 2147479553. They aren't returned in the
# container's trigger[] list, but tags reference them by ID. Names are
# stable per GTM docs.
_BUILT_IN_TRIGGERS = {
    "2147479553": ("All Pages", "pageview"),
    "2147479572": ("Consent Initialization - All Pages", "consentInit"),
    "2147479573": ("Initialization - All Pages", "init"),
}


def _resolve_trigger(trigger_by_id: dict, tid: str) -> dict:
    """Resolve a trigger ID to a {id, name, type} dict, handling built-ins."""
    if tid in trigger_by_id:
        t = trigger_by_id[tid]
        return {"id": tid, "name": t.get("name"), "type": t.get("type")}
    if tid in _BUILT_IN_TRIGGERS:
        name, ttype = _BUILT_IN_TRIGGERS[tid]
        return {"id": tid, "name": f"(built-in) {name}", "type": ttype}
    return {"id": tid, "name": "(unknown — possibly built-in)", "type": None}


def _params_dict(tag: dict) -> dict:
    """Flatten a tag's parameter list to a {key: value} dict for simple lookups."""
    out = {}
    for p in tag.get("parameter", []):
        key = p.get("key")
        if key is None:
            continue
        if "value" in p:
            out[key] = p["value"]
        elif "list" in p:
            out[key] = p["list"]
        elif "map" in p:
            out[key] = p["map"]
    return out


def _summarize_filter(filter_obj: dict) -> str:
    """Render a single GTM trigger filter as 'variable [NOT] OP value'.

    GTM stores negation as a `negate: "true"` boolean parameter alongside
    arg0/arg1, NOT as a separate operator. Surface it explicitly because
    a missed negate flag inverts the meaning of the trigger.
    """
    op = filter_obj.get("type", "?")
    parameter_map = {p.get("key"): p for p in filter_obj.get("parameter", [])}
    arg0 = parameter_map.get("arg0", {}).get("value", "?")
    arg1 = parameter_map.get("arg1", {}).get("value", "?")
    negate_param = parameter_map.get("negate", {})
    is_negated = str(negate_param.get("value", "")).lower() == "true"
    prefix = "NOT " if is_negated else ""
    return f"{arg0} {prefix}{op} {arg1}"


def _trigger_group_member_ids(trigger: dict) -> list[str]:
    """Extract child trigger IDs from a triggerGroup's parameters.

    Stored as parameter `triggerIds` of type `list` containing items of type
    `triggerReference` whose value is the child trigger_id.
    """
    members: list[str] = []
    for p in trigger.get("parameter", []):
        if p.get("key") != "triggerIds":
            continue
        for item in p.get("list", []):
            v = item.get("value")
            if v:
                members.append(str(v))
    return members


def _element_visibility_summary(trigger: dict) -> dict:
    """Extract selector + timing config from an elementVisibility trigger.

    Most actionable fields: selectorType (id vs cssSelector), the selector
    itself, and firingFrequency (oncePerEvent/oncePerElement/many) — these
    determine which DOM element the trigger watches.
    """
    params = {}
    for p in trigger.get("parameter", []):
        key = p.get("key")
        if key:
            params[key] = p.get("value")

    selector_type = params.get("selectorType")
    if str(selector_type).upper() == "ID":
        selector = params.get("elementId")
    else:
        selector = params.get("elementSelector")

    return {
        "selector_type": selector_type,
        "selector": selector,
        "firing_frequency": params.get("firingFrequency"),
        "on_screen_ratio": params.get("onScreenRatio"),
        "use_dom_change_listener": params.get("useDomChangeListener"),
        "use_on_screen_duration": params.get("useOnScreenDuration"),
    }


def _parse_trigger(trigger: dict) -> dict:
    """Normalize a trigger to its key fields plus human-readable filter list.

    Adds type-specific fields when relevant: triggerGroup member IDs,
    elementVisibility selector + timing.
    """
    out = {
        "trigger_id": trigger.get("triggerId"),
        "name": trigger.get("name"),
        "type": trigger.get("type"),
        "filters": [_summarize_filter(f) for f in trigger.get("filter", [])],
        "auto_event_filters": [
            _summarize_filter(f)
            for group in trigger.get("autoEventFilter", [])
            for f in group.get("filter", [])
        ],
        "custom_event_filters": [
            _summarize_filter(f) for f in trigger.get("customEventFilter", [])
        ],
        "wait_for_tags": trigger.get("waitForTags", {}).get("value")
        if isinstance(trigger.get("waitForTags"), dict)
        else None,
        "check_validation": trigger.get("checkValidation", {}).get("value")
        if isinstance(trigger.get("checkValidation"), dict)
        else None,
    }

    if trigger.get("type") == "triggerGroup":
        out["group_member_trigger_ids"] = _trigger_group_member_ids(trigger)

    if trigger.get("type") == "elementVisibility":
        out["element_visibility"] = _element_visibility_summary(trigger)

    return out


def _parse_variable(variable: dict) -> dict:
    """Normalize a custom variable to its key fields."""
    params = _params_dict(variable)
    return {
        "variable_id": variable.get("variableId"),
        "name": variable.get("name"),
        "type": variable.get("type"),
        "parameters": params,
        "format_value": variable.get("formatValue"),
    }


def list_accounts(config: AdLoopConfig) -> dict:
    """List all GTM accounts the service account / OAuth user can read."""
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    resp = client.accounts().list().execute()
    accounts = []
    for acct in resp.get("account", []):
        accounts.append({
            "account_id": acct.get("accountId"),
            "name": acct.get("name"),
            "path": acct.get("path"),
        })
    result = {"accounts": accounts, "count": len(accounts)}
    if not accounts:
        # Guard against the "empty list = all healthy" misreading: a
        # model asked to audit tags once reported "Tag Manager is ok"
        # for an account that has no GTM at all.
        result["insights"] = [
            "This Google account has NO Tag Manager accounts. Do not "
            "report Tag Manager as healthy or audited — report that GTM "
            "is not in use. Sites can track perfectly well without GTM "
            "(gtag.js installed directly); use GA4 tools like "
            "validate_tracking to assess tracking health instead."
        ]
    return result


def list_containers(config: AdLoopConfig, *, account_id: str) -> dict:
    """List all containers under a GTM account."""
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    parent = f"accounts/{account_id}"
    resp = client.accounts().containers().list(parent=parent).execute()
    containers = []
    for c in resp.get("container", []):
        containers.append({
            "container_id": c.get("containerId"),
            "public_id": c.get("publicId"),
            "name": c.get("name"),
            "usage_context": c.get("usageContext", []),
            "path": c.get("path"),
        })
    return {"account_id": account_id, "containers": containers, "count": len(containers)}


def _parse_tags(tags: list[dict], triggers: list[dict]) -> list[dict]:
    """Normalize tags: GA4 event name, resolved firing triggers, pause state."""
    trigger_by_id = {t.get("triggerId"): t for t in triggers}
    parsed_tags = []
    for tag in tags:
        params = _params_dict(tag)
        firing_trigger_ids = tag.get("firingTriggerId", [])
        firing_triggers = [_resolve_trigger(trigger_by_id, tid) for tid in firing_trigger_ids]

        event_name = None
        if tag.get("type") == GA4_EVENT_TAG:
            ev = params.get("eventName")
            if isinstance(ev, str):
                event_name = ev

        parsed_tags.append({
            "tag_id": tag.get("tagId"),
            "name": tag.get("name"),
            "type": tag.get("type"),
            "event_name": event_name,
            "paused": tag.get("paused", False),
            "firing_triggers": firing_triggers,
            "blocking_triggers": tag.get("blockingTriggerId", []),
            "parameters": params,
        })
    return parsed_tags


def get_live_container(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
) -> dict:
    """Fetch the LIVE (published) container version with parsed tags + triggers.

    Returns a normalized dict: each tag has its event name extracted (for GA4
    event tags), firing triggers resolved to names + types, and pause status
    surfaced. Custom HTML tags are flagged separately because their event
    semantics can't be inferred without parsing the JS body.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    live = _fetch_live(client, account_id, container_id)

    tags = live.get("tag", [])
    triggers = live.get("trigger", [])
    variables = live.get("variable", [])

    return {
        "account_id": account_id,
        "container_id": container_id,
        "container_version_id": live.get("containerVersionId"),
        "container_version_name": live.get("name"),
        "fingerprint": live.get("fingerprint"),
        "tags": _parse_tags(tags, triggers),
        "trigger_count": len(triggers),
        "variable_count": len(variables),
    }


# ---------------------------------------------------------------------------
# Per-resource read helpers: LIVE container by default, or one workspace
# ---------------------------------------------------------------------------
#
# Tag Manager allows 25 requests per 100 seconds per Google Cloud project, so
# every read here makes as few calls as it can: the live version carries every
# entity in ONE call, and a workspace read lists only the kinds it needs (a
# list returns full entities, so there are no per-entity follow-up calls).


def _fetch_live(client, account_id: str, container_id: str) -> dict:
    parent = f"accounts/{account_id}/containers/{container_id}"
    return (
        client.accounts()
        .containers()
        .versions()
        .live(parent=parent)
        .execute()
    )


def _workspace_path(account_id: str, container_id: str, workspace_id: str) -> str:
    return f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"


def _list_all(list_method, parent: str, key: str) -> list[dict]:
    """Collect every page of a Tag Manager list call (usually one page)."""
    items: list[dict] = []
    token = None
    while True:
        kwargs = {"parent": parent}
        if token:
            kwargs["pageToken"] = token
        resp = list_method(**kwargs).execute() or {}
        items.extend(resp.get(key, []) or [])
        token = resp.get("nextPageToken")
        if not token:
            return items


def _fetch_entities(
    client,
    account_id: str,
    container_id: str,
    workspace_id: str,
    kinds: tuple[str, ...],
) -> dict:
    """Entities of the given kinds from the live version or a workspace.

    Returns ``{"tag": [...], "trigger": [...], "variable": [...],
    "builtInVariable": [...] (live only), "source": {...}}``. The live path is
    one ``versions.live`` call whatever ``kinds`` asks for; the workspace path
    is one list call per kind.
    """
    if not workspace_id:
        live = _fetch_live(client, account_id, container_id)
        out = {k: live.get(k, []) or [] for k in kinds}
        out["builtInVariable"] = live.get("builtInVariable", []) or []
        out["source"] = {
            "source": "live",
            "container_version_id": live.get("containerVersionId"),
        }
        return out

    ws = client.accounts().containers().workspaces()
    parent = _workspace_path(account_id, container_id, workspace_id)
    apis = {"tag": ws.tags, "trigger": ws.triggers, "variable": ws.variables}
    out = {k: _list_all(apis[k]().list, parent, k) for k in kinds}
    out["source"] = {
        "source": "workspace",
        "workspace_id": str(workspace_id),
        "container_version_id": None,
        "note": (
            "Workspace state: includes drafted changes that are not published, "
            "so it can differ from what runs on the site."
        ),
    }
    return out


def _where(account_id: str, container_id: str, workspace_id: str) -> str:
    if workspace_id:
        return f"workspace {workspace_id} of container {container_id}"
    return f"live container {container_id}"


def list_tags(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
) -> dict:
    """List every tag (live container, or one workspace) with parsed triggers.

    Live: 1 API call. Workspace: 2 (tags + triggers, for trigger names).
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    data = _fetch_entities(
        client, account_id, container_id, workspace_id, ("tag", "trigger")
    )
    tags = _parse_tags(data["tag"], data["trigger"])
    return {
        "account_id": account_id,
        "container_id": container_id,
        **data["source"],
        "tags": tags,
        "count": len(tags),
    }


def get_tag(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    tag_id: str,
    workspace_id: str = "",
) -> dict:
    """Return the full RAW config for a single tag (live, or one workspace).

    Includes every parameter, firing/blocking trigger references, priority,
    pause status, and tag-specific settings (sampling, monitoring, etc.).
    Use after audit_event_coverage flags a tag for inspection.
    Live: 1 API call. Workspace: 2 (tags + triggers lists).
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    data = _fetch_entities(
        client, account_id, container_id, workspace_id, ("tag", "trigger")
    )
    triggers_by_id = {t.get("triggerId"): t for t in data["trigger"]}

    for tag in data["tag"]:
        if str(tag.get("tagId")) == str(tag_id):
            params = _params_dict(tag)
            return {
                **data["source"],
                "tag_id": tag.get("tagId"),
                "name": tag.get("name"),
                "type": tag.get("type"),
                "paused": tag.get("paused", False),
                "priority": tag.get("priority"),
                "tag_firing_option": tag.get("tagFiringOption"),
                "monitoring_metadata": tag.get("monitoringMetadata"),
                "live_only": tag.get("liveOnly"),
                "parameters": params,
                "firing_triggers": [
                    {
                        **_resolve_trigger(triggers_by_id, tid),
                        "filters": [
                            _summarize_filter(f)
                            for f in triggers_by_id.get(tid, {}).get("filter", [])
                        ],
                    }
                    for tid in tag.get("firingTriggerId", [])
                ],
                "blocking_triggers": [
                    _resolve_trigger(triggers_by_id, tid)
                    for tid in tag.get("blockingTriggerId", [])
                ],
                "raw": tag,
            }

    return {
        "error": (
            f"Tag {tag_id} not found in "
            f"{_where(account_id, container_id, workspace_id)}"
        ),
        "available_tag_ids": [t.get("tagId") for t in data["tag"]],
    }


def list_triggers(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
) -> dict:
    """List every trigger (live, or one workspace) with filters parsed to text.

    1 API call either way.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    data = _fetch_entities(client, account_id, container_id, workspace_id, ("trigger",))
    triggers = [_parse_trigger(t) for t in data["trigger"]]
    return {
        "account_id": account_id,
        "container_id": container_id,
        **data["source"],
        "triggers": triggers,
        "count": len(triggers),
    }


def get_trigger(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    trigger_id: str,
    workspace_id: str = "",
) -> dict:
    """Return the full RAW config for a single trigger (live, or one workspace).

    Live: 1 API call. Workspace: 2 (triggers + tags, for ``used_by_tags``).
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    data = _fetch_entities(
        client, account_id, container_id, workspace_id, ("trigger", "tag")
    )

    for trigger in data["trigger"]:
        if str(trigger.get("triggerId")) == str(trigger_id):
            parsed = {**data["source"], **_parse_trigger(trigger)}
            parsed["raw"] = trigger
            tags_using = [
                {"tag_id": t.get("tagId"), "name": t.get("name")}
                for t in data["tag"]
                if str(trigger_id) in [str(x) for x in t.get("firingTriggerId", [])]
            ]
            parsed["used_by_tags"] = tags_using
            return parsed

    return {
        "error": (
            f"Trigger {trigger_id} not found in "
            f"{_where(account_id, container_id, workspace_id)}"
        ),
        "available_trigger_ids": [t.get("triggerId") for t in data["trigger"]],
    }


def _default_workspace_id(client, account_id: str, container_id: str) -> str:
    """The Default Workspace (or the only one); empty when ambiguous or none."""
    workspaces = _list_all(
        client.accounts().containers().workspaces().list,
        f"accounts/{account_id}/containers/{container_id}",
        "workspace",
    )
    for w in workspaces:
        if w.get("name") == "Default Workspace":
            return str(w.get("workspaceId"))
    if len(workspaces) == 1:
        return str(workspaces[0].get("workspaceId"))
    return ""


def list_variables(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
) -> dict:
    """List custom variables plus enabled built-in variables.

    Live (no ``workspace_id``): both come from the live version itself, one
    API call. Only when the live version carries no built-in list are the
    built-ins read from the Default Workspace (two more calls), and the
    result says so. With ``workspace_id``: custom variables and built-ins of
    that workspace, two calls.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    data = _fetch_entities(client, account_id, container_id, workspace_id, ("variable",))
    custom = [_parse_variable(v) for v in data["variable"]]

    built_in_source = "live_version" if not workspace_id else f"workspace {workspace_id}"
    raw_built_in = data.get("builtInVariable") or []
    built_in_ws = workspace_id
    if not workspace_id and not raw_built_in:
        built_in_ws = _default_workspace_id(client, account_id, container_id)
        built_in_source = (
            f"workspace {built_in_ws} (Default Workspace; the live version "
            "listed no built-in variables)"
            if built_in_ws
            else "unavailable (no Default Workspace to read built-ins from)"
        )
    if built_in_ws:
        try:
            raw_built_in = _list_all(
                client.accounts().containers().workspaces().built_in_variables().list,
                _workspace_path(account_id, container_id, built_in_ws),
                "builtInVariable",
            )
        except Exception:  # noqa: BLE001 (built-ins are supplementary)
            raw_built_in = []
            built_in_source = f"unavailable (reading workspace {built_in_ws} failed)"

    built_in = [{"name": v.get("name"), "type": v.get("type")} for v in raw_built_in]

    return {
        "account_id": account_id,
        "container_id": container_id,
        **data["source"],
        "custom_variables": custom,
        "custom_count": len(custom),
        "built_in": built_in,
        "built_in_count": len(built_in),
        "built_in_source": built_in_source,
    }


def list_workspaces(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
) -> dict:
    """List workspaces (drafts) under a container.

    Most containers have a single Default Workspace. Multiple workspaces appear
    when the team uses parallel drafts. Workspace IDs are needed for diff +
    future write operations.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    parent = f"accounts/{account_id}/containers/{container_id}"
    resp = (
        client.accounts()
        .containers()
        .workspaces()
        .list(parent=parent)
        .execute()
    )
    workspaces = []
    for w in resp.get("workspace", []):
        workspaces.append({
            "workspace_id": w.get("workspaceId"),
            "name": w.get("name"),
            "description": w.get("description"),
            "path": w.get("path"),
        })
    return {
        "account_id": account_id,
        "container_id": container_id,
        "workspaces": workspaces,
        "count": len(workspaces),
    }


def get_workspace_diff(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str,
) -> dict:
    """Show drafted-but-not-published changes in a workspace.

    Calls workspaces.getStatus, which returns the list of entities (tags,
    triggers, variables) that have been added, modified, or deleted relative
    to the live published version. Common cause of "I edited a tag in GTM
    but nothing happened" — the workspace was never published.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    path = (
        f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
    )
    status = (
        client.accounts()
        .containers()
        .workspaces()
        .getStatus(path=path)
        .execute()
    )

    changes = status.get("workspaceChange", [])
    summary: dict[str, int] = {}
    parsed_changes = []
    for change in changes:
        change_status = change.get("changeStatus", "unknown")
        summary[change_status] = summary.get(change_status, 0) + 1

        for kind in ENTITY_KINDS:
            if kind in change:
                entity = change[kind]
                parsed_changes.append({
                    "change_status": change_status,
                    "entity_kind": kind,
                    "entity_id": entity.get(
                        ENTITY_ID_FIELDS.get(kind, f"{kind}Id")
                    ),
                    "name": entity.get("name"),
                    "type": entity.get("type"),
                })

    return {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": workspace_id,
        "merge_conflict": status.get("mergeConflict", []),
        "change_count": len(changes),
        "change_summary_by_status": summary,
        "changes": parsed_changes,
        "is_clean": len(changes) == 0 and not status.get("mergeConflict"),
    }


def _version_headers(client, account_id: str, container_id: str) -> list[dict]:
    """Every non-archived version header, newest (highest id) first."""
    headers = _list_all(
        client.accounts().containers().version_headers().list,
        f"accounts/{account_id}/containers/{container_id}",
        "containerVersionHeader",
    )
    return sorted(headers, key=lambda h: _version_number(h.get("containerVersionId")),
                  reverse=True)


def _version_number(version_id) -> int:
    try:
        return int(version_id)
    except (TypeError, ValueError):
        return -1


def list_versions(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    page_size: int = 50,
) -> dict:
    """List container version headers (newest first) with entity counts.

    Each header carries the version id, name, archived flag and entity
    counts. The Tag Manager API returns no creation time, publish time or
    author for a version, and no flag for which versions were ever
    published; only the live one is identifiable (``versions.live``). To
    correlate a metric drop with a change, compare version names and notes
    (get_version returns the notes) or diff two versions with diff_versions.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    headers = _version_headers(client, account_id, container_id)[:page_size]
    versions = []
    for v in headers:
        versions.append({
            "container_version_id": v.get("containerVersionId"),
            "name": v.get("name"),
            "deleted": v.get("deleted", False),
            "num_tags": v.get("numTags"),
            "num_triggers": v.get("numTriggers"),
            "num_variables": v.get("numVariables"),
            "num_macros": v.get("numMacros"),
            "num_rules": v.get("numRules"),
        })
    return {
        "account_id": account_id,
        "container_id": container_id,
        "versions": versions,
        "count": len(versions),
        "note": (
            "The Tag Manager API exposes no creation/publish time and no author "
            "for container versions, and does not mark which versions were "
            "published. get_gtm_version returns a version's notes; "
            "get_gtm_version_diff shows what changed between two versions."
        ),
    }


def get_version(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    container_version_id: str,
) -> dict:
    """Get metadata + content for a single container version.

    Includes the name, notes (description), fingerprint and the
    tag/trigger/variable names at that point in time. The API returns no
    timestamps or author for a version.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    path = (
        f"accounts/{account_id}/containers/{container_id}/versions/"
        f"{container_version_id}"
    )
    v = client.accounts().containers().versions().get(path=path).execute()
    return {
        "container_version_id": v.get("containerVersionId"),
        "name": v.get("name"),
        "description": v.get("description"),
        "fingerprint": v.get("fingerprint"),
        "deleted": v.get("deleted", False),
        "tag_count": len(v.get("tag", [])),
        "trigger_count": len(v.get("trigger", [])),
        "variable_count": len(v.get("variable", [])),
        "tag_names": [t.get("name") for t in v.get("tag", [])],
        "trigger_names": [t.get("name") for t in v.get("trigger", [])],
    }


# ---------------------------------------------------------------------------
# Version diff: what changed between two container versions
# ---------------------------------------------------------------------------

# Fields that differ between two copies of the same entity without meaning a
# configuration change: storage fingerprints, paths and URLs.
_VOLATILE_FIELDS = frozenset({
    "fingerprint", "path", "tagManagerUrl", "accountId", "containerId",
    "workspaceId",
})

_DIFF_KINDS = (
    # (version field, id field, output key)
    ("tag", "tagId", "tags"),
    ("trigger", "triggerId", "triggers"),
    ("variable", "variableId", "variables"),
)

# Trigger condition lists, rendered as text in a diff ("{{Page Path}} contains
# /thanks") rather than as raw nested parameter dicts.
_CONDITION_FIELDS = frozenset({"filter", "customEventFilter", "autoEventFilter"})


def _diff_field(field: str, old, new) -> dict:
    if field == "parameter":
        old_params = _params_dict({"parameter": old or []})
        new_params = _params_dict({"parameter": new or []})
        keys = sorted(set(old_params) | set(new_params))
        return {
            "parameters": {
                k: {"from": old_params.get(k), "to": new_params.get(k)}
                for k in keys
                if old_params.get(k) != new_params.get(k)
            }
        }
    if field in _CONDITION_FIELDS:
        return {
            field: {
                "from": [_summarize_filter(f) for f in old or []],
                "to": [_summarize_filter(f) for f in new or []],
            }
        }
    return {field: {"from": old, "to": new}}


def _entity_changes(old: dict, new: dict) -> tuple[list[str], dict]:
    fields = sorted(
        (set(old) | set(new)) - _VOLATILE_FIELDS,
    )
    changed = [f for f in fields if old.get(f) != new.get(f)]
    details: dict = {}
    for f in changed:
        details.update(_diff_field(f, old.get(f), new.get(f)))
    return changed, details


def _brief(entity: dict, id_field: str) -> dict:
    return {
        "id": entity.get(id_field),
        "name": entity.get("name"),
        "type": entity.get("type"),
    }


def diff_container_versions(old: dict, new: dict) -> dict:
    """Compare two ContainerVersion bodies; pure, no API calls.

    Entities are matched by id. Returns added / removed / changed per kind
    (tags, triggers, variables) with names, types and the changed fields,
    plus built-in variables enabled or disabled between the two.
    """
    out: dict = {}
    summary: dict = {}
    for kind, id_field, key in _DIFF_KINDS:
        old_by_id = {str(e.get(id_field)): e for e in old.get(kind, []) or []}
        new_by_id = {str(e.get(id_field)): e for e in new.get(kind, []) or []}
        added = [_brief(new_by_id[i], id_field) for i in new_by_id if i not in old_by_id]
        removed = [_brief(old_by_id[i], id_field) for i in old_by_id if i not in new_by_id]
        changed = []
        for i in new_by_id:
            if i not in old_by_id:
                continue
            fields, details = _entity_changes(old_by_id[i], new_by_id[i])
            if fields:
                entry = _brief(new_by_id[i], id_field)
                if old_by_id[i].get("name") != new_by_id[i].get("name"):
                    entry["previous_name"] = old_by_id[i].get("name")
                entry["changed_fields"] = fields
                entry["changes"] = details
                changed.append(entry)
        out[key] = {"added": added, "removed": removed, "changed": changed}
        summary[key] = {
            "added": len(added), "removed": len(removed), "changed": len(changed),
        }

    old_bi = {b.get("type") or b.get("name") for b in old.get("builtInVariable", []) or []}
    new_bi = {b.get("type") or b.get("name") for b in new.get("builtInVariable", []) or []}
    out["built_in_variables"] = {
        "enabled": sorted(t for t in new_bi - old_bi if t),
        "disabled": sorted(t for t in old_bi - new_bi if t),
    }
    out["summary"] = summary
    out["is_identical"] = not any(
        any(counts.values()) for counts in summary.values()
    ) and not (new_bi ^ old_bi)
    return out


def _version_ref(version: dict, *, live_id: str | None = None) -> dict:
    vid = version.get("containerVersionId")
    ref = {"container_version_id": vid, "name": version.get("name")}
    if live_id is not None:
        ref["is_live"] = str(vid) == str(live_id)
    return ref


def diff_versions(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    from_version_id: str = "",
    to_version_id: str = "",
) -> dict:
    """Diff two container versions (default: the one before live → live).

    API calls: 2 when both ids are given (two ``versions.get``) or only
    ``from_version_id`` (live + one get); 3 when ``from_version_id`` is empty
    (one ``version_headers.list`` to find the version before the target).
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    versions = client.accounts().containers().versions()
    base = f"accounts/{account_id}/containers/{container_id}"
    calls = 0

    def _get(vid: str) -> dict:
        nonlocal calls
        calls += 1
        return versions.get(path=f"{base}/versions/{vid}").execute()

    live_id = None
    if to_version_id:
        new = _get(to_version_id)
    else:
        calls += 1
        new = _fetch_live(client, account_id, container_id)
        live_id = new.get("containerVersionId")
        if not live_id:
            return {"error": f"Container {container_id} has no live (published) version."}

    note = None
    if from_version_id:
        old = _get(from_version_id)
    else:
        calls += 1
        target = _version_number(new.get("containerVersionId"))
        older = [
            h for h in _version_headers(client, account_id, container_id)
            if not h.get("deleted")
            and 0 <= _version_number(h.get("containerVersionId")) < target
        ]
        if not older:
            return {
                "error": (
                    f"No version older than {new.get('containerVersionId')} "
                    "exists to compare against."
                ),
            }
        old = _get(older[0]["containerVersionId"])
        note = (
            "from_version is the newest version created before to_version. The "
            "Tag Manager API does not mark which versions were published, so it "
            "may be a version that was never live."
        )

    diff = diff_container_versions(old, new)
    result = {
        "account_id": account_id,
        "container_id": container_id,
        "from_version": _version_ref(old, live_id=live_id),
        "to_version": _version_ref(new, live_id=live_id),
        **diff,
        "api_calls": calls,
    }
    if note:
        result["note"] = note
    return result
