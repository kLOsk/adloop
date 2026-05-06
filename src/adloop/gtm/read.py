"""GTM read helpers — fetch the live (published) container version and parse tags."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    return {"accounts": accounts, "count": len(accounts)}


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


def get_live_container(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
) -> dict:
    """Fetch the LIVE (published) container version with parsed tags + triggers.

    Returns a normalized dict — each tag has its event name extracted (for GA4
    event tags), firing triggers resolved to names + types, and pause status
    surfaced. Custom HTML tags are flagged separately because their event
    semantics can't be inferred without parsing the JS body.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    parent = f"accounts/{account_id}/containers/{container_id}"

    live = (
        client.accounts()
        .containers()
        .versions()
        .live(parent=parent)
        .execute()
    )

    tags = live.get("tag", [])
    triggers = live.get("trigger", [])
    variables = live.get("variable", [])

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

    return {
        "account_id": account_id,
        "container_id": container_id,
        "container_version_id": live.get("containerVersionId"),
        "container_version_name": live.get("name"),
        "fingerprint": live.get("fingerprint"),
        "tags": parsed_tags,
        "trigger_count": len(triggers),
        "variable_count": len(variables),
    }


# ---------------------------------------------------------------------------
# Per-resource read helpers — operate on the LIVE container by default
# ---------------------------------------------------------------------------


def _fetch_live(client, account_id: str, container_id: str) -> dict:
    parent = f"accounts/{account_id}/containers/{container_id}"
    return (
        client.accounts()
        .containers()
        .versions()
        .live(parent=parent)
        .execute()
    )


def list_tags(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
) -> dict:
    """List every tag in the LIVE container with parsed event names + triggers."""
    container = get_live_container(
        config, account_id=account_id, container_id=container_id
    )
    return {
        "account_id": account_id,
        "container_id": container_id,
        "container_version_id": container["container_version_id"],
        "tags": container["tags"],
        "count": len(container["tags"]),
    }


def get_tag(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    tag_id: str,
) -> dict:
    """Return the full RAW config for a single tag from the live container.

    Includes every parameter, firing/blocking trigger references, priority,
    pause status, and tag-specific settings (sampling, monitoring, etc.).
    Use after audit_event_coverage flags a tag for inspection.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    live = _fetch_live(client, account_id, container_id)
    triggers_by_id = {t.get("triggerId"): t for t in live.get("trigger", [])}

    for tag in live.get("tag", []):
        if str(tag.get("tagId")) == str(tag_id):
            params = _params_dict(tag)
            return {
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
        "error": f"Tag {tag_id} not found in live container {container_id}",
        "available_tag_ids": [t.get("tagId") for t in live.get("tag", [])],
    }


def list_triggers(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
) -> dict:
    """List every trigger in the LIVE container with filters parsed to text."""
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    live = _fetch_live(client, account_id, container_id)
    triggers = [_parse_trigger(t) for t in live.get("trigger", [])]
    return {
        "account_id": account_id,
        "container_id": container_id,
        "container_version_id": live.get("containerVersionId"),
        "triggers": triggers,
        "count": len(triggers),
    }


def get_trigger(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    trigger_id: str,
) -> dict:
    """Return the full RAW config for a single trigger from the live container."""
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    live = _fetch_live(client, account_id, container_id)

    for trigger in live.get("trigger", []):
        if str(trigger.get("triggerId")) == str(trigger_id):
            parsed = _parse_trigger(trigger)
            parsed["raw"] = trigger
            tags_using = [
                {"tag_id": t.get("tagId"), "name": t.get("name")}
                for t in live.get("tag", [])
                if str(trigger_id) in [str(x) for x in t.get("firingTriggerId", [])]
            ]
            parsed["used_by_tags"] = tags_using
            return parsed

    return {
        "error": f"Trigger {trigger_id} not found in live container {container_id}",
        "available_trigger_ids": [t.get("triggerId") for t in live.get("trigger", [])],
    }


def list_variables(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
) -> dict:
    """List custom variables in the LIVE container plus enabled built-in variables.

    Custom variables come from the live container version. Built-in variables
    (Page URL, Click Element, etc.) come from a separate API endpoint and are
    listed under `built_in`.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    live = _fetch_live(client, account_id, container_id)

    custom = [_parse_variable(v) for v in live.get("variable", [])]

    built_in: list[dict] = []
    workspaces = (
        client.accounts()
        .containers()
        .workspaces()
        .list(parent=f"accounts/{account_id}/containers/{container_id}")
        .execute()
        .get("workspace", [])
    )
    if workspaces:
        wid = workspaces[0].get("workspaceId")
        try:
            biv = (
                client.accounts()
                .containers()
                .workspaces()
                .built_in_variables()
                .list(
                    parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{wid}"
                )
                .execute()
                .get("builtInVariable", [])
            )
            built_in = [
                {"name": v.get("name"), "type": v.get("type")} for v in biv
            ]
        except Exception:
            built_in = []

    return {
        "account_id": account_id,
        "container_id": container_id,
        "container_version_id": live.get("containerVersionId"),
        "custom_variables": custom,
        "custom_count": len(custom),
        "built_in": built_in,
        "built_in_count": len(built_in),
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

        for kind in ("tag", "trigger", "variable", "folder", "client", "transformation", "zone"):
            if kind in change:
                entity = change[kind]
                parsed_changes.append({
                    "change_status": change_status,
                    "entity_kind": kind,
                    "entity_id": entity.get(f"{kind}Id"),
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


def list_versions(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    page_size: int = 50,
) -> dict:
    """List published version history (newest first) with author + notes.

    Use this to correlate a metric drop with a recent publish — fetch the
    last few versions, look at created/updated timestamps and notes, and
    cross-reference with the date the conversion / session drop began.
    """
    from adloop.gtm.client import get_gtm_client

    client = get_gtm_client(config)
    parent = f"accounts/{account_id}/containers/{container_id}"
    resp = (
        client.accounts()
        .containers()
        .version_headers()
        .list(parent=parent)
        .execute()
    )
    headers = resp.get("containerVersionHeader", [])[:page_size]
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
            "Version headers do not include createdAt/author. Call "
            "get_gtm_version on a specific version_id for full metadata."
        ),
    }


def get_version(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    container_version_id: str,
) -> dict:
    """Get full metadata + content for a single container version.

    Includes created/updated timestamps, fingerprint, full tag/trigger/variable
    lists at that point in time. Useful for correlating a metric drop with
    what changed in a specific publish.
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
