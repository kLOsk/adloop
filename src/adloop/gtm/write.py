"""GTM API write tools — Google Tag Manager API v2.

All write operations follow the AdLoop safety pattern:
    1. draft_gtm_*  → creates a ChangePlan, stores it, returns plan_id
    2. confirm_and_apply(plan_id) → executes via the GTM API

Supported tag types (tag_type):
    googtag   — Google Tag (gtag.js config) — use for AW-... and G-...
    awct      — Google Ads Conversion Tracking
    gclidw    — Google Ads Conversion Linker
    html      — Custom HTML
    gaawe     — GA4 Event tag

Supported trigger types (trigger_type):
    pageview, dom_ready, window_loaded
    click            — clicks on any element
    linkClick        — clicks on links only
    formSubmission   — form submits
    customEvent      — dataLayer.push({event: name})

Workspaces: leave workspace_id empty to auto-pick the Default Workspace.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_TAG_TYPES = {
    # Google-platform native templates
    "googtag", "gclidw",
    "awct",      # Google Ads Conversion Tracking (page-load conversions)
    "awcc",      # Google Ads Calls from Website Conversion (GFN swap)
    "awcr",      # Google Ads Remarketing
    "awud",      # Google Ads User-Provided Data
    "ua",        # Universal Analytics (legacy)
    "gaawe",     # GA4 Event
    "gaawc",     # GA4 Configuration (legacy alias)
    "fls", "flc",  # Floodlight Sales / Counter

    # Verbose long-form aliases — same templates, different names
    "google_analytics_4_event",
    "google_ads_conversion_tracking",
    "google_ads_calls_from_website",
    "google_ads_remarketing",
    "google_ads_user_provided_data",

    # Generic / open-ended
    "html",      # Custom HTML
    "img",       # Custom Image
}

_VALID_TRIGGER_TYPES = {
    "pageview", "dom_ready", "window_loaded",
    "click", "linkClick", "formSubmission",
    "customEvent", "elementVisibility",
    "scroll_depth", "youtube_video", "history_change",
    "timer", "javascript_error",
}

# How GTM stores key/value parameter structures
def _param(key: str, value, type_: str = "TEMPLATE") -> dict:
    """Build a GTM parameter dict.

    GTM parameter shape:
        {"type": "TEMPLATE"|"BOOLEAN"|"INTEGER"|"LIST"|"MAP", "key": ..., "value": ...}
    """
    out = {"type": type_, "key": key}
    if type_ == "BOOLEAN":
        out["value"] = "true" if bool(value) else "false"
    elif type_ == "INTEGER":
        out["value"] = str(int(value))
    elif type_ == "LIST":
        out["list"] = value
    elif type_ == "MAP":
        out["map"] = value
    else:
        out["value"] = str(value)
    return out


def _resolve_workspace(client, account_id: str, container_id: str,
                      workspace_id: str = "") -> str:
    """Resolve workspace_id, defaulting to the Default Workspace."""
    if workspace_id:
        return workspace_id
    parent = f"accounts/{account_id}/containers/{container_id}"
    resp = (
        client.accounts()
        .containers()
        .workspaces()
        .list(parent=parent)
        .execute()
    )
    for w in resp.get("workspace", []):
        if w.get("name") == "Default Workspace":
            return w["workspaceId"]
    # Fall back to first workspace
    workspaces = resp.get("workspace", [])
    if not workspaces:
        raise ValueError(
            f"No workspaces found under {parent}. Container may be misconfigured."
        )
    return workspaces[0]["workspaceId"]


# ---------------------------------------------------------------------------
# draft_gtm_tag
# ---------------------------------------------------------------------------

def draft_gtm_tag(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
    name: str,
    tag_type: str,
    parameters: list[dict] | None = None,
    firing_trigger_ids: list[str] | None = None,
    blocking_trigger_ids: list[str] | None = None,
    paused: bool = False,
    notes: str = "",
) -> dict:
    """Draft a new GTM tag in a workspace — returns a PREVIEW.

    name: human-readable tag name shown in GTM UI
    tag_type: one of: googtag, awct, gclidw, html, gaawe (see module docstring)
    parameters: list of {type, key, value} dicts. Common patterns:
        - Google Tag (googtag):
              [{"type": "TEMPLATE", "key": "tagId", "value": "AW-11437481610"}]
        - Google Ads Conversion (awct):
              [{"type": "TEMPLATE", "key": "conversionId", "value": "11437481610"},
               {"type": "TEMPLATE", "key": "conversionLabel", "value": "_qxp..."},
               {"type": "TEMPLATE", "key": "conversionValue", "value": "250"},
               {"type": "TEMPLATE", "key": "conversionCurrency", "value": "USD"}]
        - Custom HTML (html):
              [{"type": "TEMPLATE", "key": "html", "value": "<script>...</script>"},
               {"type": "BOOLEAN", "key": "supportDocumentWrite", "value": "false"}]
        - Conversion Linker (gclidw): no parameters required
        - GA4 Event (gaawe):
              [{"type": "TEMPLATE", "key": "eventName", "value": "form_submit"},
               {"type": "TEMPLATE", "key": "measurementIdOverride", "value": "G-..."}]
    firing_trigger_ids: list of trigger IDs (string) that fire this tag.
        For init-on-all-pages, use ["2147479573"] (built-in).
    blocking_trigger_ids: optional list of trigger IDs that block the tag.
    paused: pause the tag on creation (default False).
    notes: optional notes shown in GTM UI.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_gtm_tag", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if not name or len(name) > 200:
        errors.append("name is required (1-200 chars)")
    if tag_type not in _VALID_TAG_TYPES:
        errors.append(
            f"tag_type '{tag_type}' invalid; valid: {sorted(_VALID_TAG_TYPES)}"
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_gtm_tag",
        entity_type="gtm_tag",
        entity_id=container_id,
        customer_id="",  # GTM doesn't use customer_id
        changes={
            "account_id": account_id,
            "container_id": container_id,
            "workspace_id": workspace_id,
            "name": name,
            "type": tag_type,
            "parameters": parameters or [],
            "firing_trigger_ids": firing_trigger_ids or [],
            "blocking_trigger_ids": blocking_trigger_ids or [],
            "paused": bool(paused),
            "notes": notes,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_gtm_trigger(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
    name: str,
    trigger_type: str,
    filters: list[dict] | None = None,
    custom_event_filters: list[dict] | None = None,
    auto_event_filters: list[dict] | None = None,
    custom_event_name: str = "",
    parameters: list[dict] | None = None,
    notes: str = "",
) -> dict:
    """Draft a new GTM trigger in a workspace — returns a PREVIEW.

    trigger_type: pageview | click | linkClick | formSubmission | customEvent | ...
    filters: list of GTM filter dicts {parameter: [...], type: "EQUALS"|"CONTAINS"|...}.
        Used for "fire on these conditions" — e.g. {{Click URL}} contains "tel:"
    custom_event_filters: same shape, applied as additional conditions on
        custom event triggers.
    custom_event_name: required when trigger_type=customEvent (the dataLayer
        event name to listen for).

    Common shapes:
        Click on any tel: link:
            trigger_type="click", filters=[{
                "type": "CONTAINS",
                "parameter": [
                    {"type": "TEMPLATE", "key": "arg0", "value": "{{Click URL}}"},
                    {"type": "TEMPLATE", "key": "arg1", "value": "tel:"},
                ],
            }]

        Form submit on contact page:
            trigger_type="formSubmission", filters=[{
                "type": "CONTAINS",
                "parameter": [
                    {"type": "TEMPLATE", "key": "arg0", "value": "{{Page Path}}"},
                    {"type": "TEMPLATE", "key": "arg1", "value": "/contacts"},
                ],
            }]

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_gtm_trigger", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if not name or len(name) > 200:
        errors.append("name is required (1-200 chars)")
    if trigger_type not in _VALID_TRIGGER_TYPES:
        errors.append(
            f"trigger_type '{trigger_type}' invalid; valid: "
            f"{sorted(_VALID_TRIGGER_TYPES)}"
        )
    if trigger_type == "customEvent" and not custom_event_name:
        errors.append("custom_event_name is required when trigger_type=customEvent")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan_changes = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": workspace_id,
        "name": name,
        "type": trigger_type,
        "filters": filters or [],
        "custom_event_filters": custom_event_filters or [],
        "auto_event_filters": auto_event_filters or [],
        "parameters": parameters or [],
        "notes": notes,
    }
    if custom_event_name:
        plan_changes["custom_event_name"] = custom_event_name

    plan = ChangePlan(
        operation="create_gtm_trigger",
        entity_type="gtm_trigger",
        entity_id=container_id,
        customer_id="",
        changes=plan_changes,
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# update + delete for tags + triggers
# ---------------------------------------------------------------------------


def draft_update_gtm_tag(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    tag_id: str,
    workspace_id: str = "",
    name: str = "",
    parameters: list[dict] | None = None,
    firing_trigger_ids: list[str] | None = None,
    blocking_trigger_ids: list[str] | None = None,
    paused: bool | None = None,
    notes: str = "",
    tag_type: str = "",
) -> dict:
    """Draft an UPDATE to an existing GTM tag — returns a PREVIEW.

    Only the fields you pass non-empty/non-None will be modified. Pass
    ``parameters`` to fully replace the parameter list (GTM doesn't support
    partial parameter updates — read the existing tag first if you need to
    merge).

    tag_type: only set if you want to change the tag type, which is
        unusual (most GTM tags can't change type — delete + create instead).

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_gtm_tag", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not tag_id:
        return {"error": "tag_id is required"}

    if tag_type and tag_type not in _VALID_TAG_TYPES:
        return {
            "error": "Validation failed",
            "details": [
                f"tag_type '{tag_type}' invalid; valid: "
                f"{sorted(_VALID_TAG_TYPES)}"
            ],
        }

    changes: dict = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": workspace_id,
        "tag_id": str(tag_id),
    }
    if name:
        changes["name"] = name
    if tag_type:
        changes["type"] = tag_type
    if parameters is not None:
        changes["parameters"] = parameters
    if firing_trigger_ids is not None:
        changes["firing_trigger_ids"] = firing_trigger_ids
    if blocking_trigger_ids is not None:
        changes["blocking_trigger_ids"] = blocking_trigger_ids
    if paused is not None:
        changes["paused"] = bool(paused)
    if notes:
        changes["notes"] = notes

    if len(changes) <= 4:  # only the routing fields
        return {"error": "No fields to update"}

    plan = ChangePlan(
        operation="update_gtm_tag",
        entity_type="gtm_tag",
        entity_id=str(tag_id),
        customer_id="",
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def draft_update_gtm_trigger(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    trigger_id: str,
    workspace_id: str = "",
    name: str = "",
    filters: list[dict] | None = None,
    custom_event_filters: list[dict] | None = None,
    auto_event_filters: list[dict] | None = None,
    parameters: list[dict] | None = None,
    notes: str = "",
) -> dict:
    """Draft an UPDATE to an existing GTM trigger — returns a PREVIEW.

    Trigger type cannot be changed (delete + create instead).

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_gtm_trigger", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not trigger_id:
        return {"error": "trigger_id is required"}

    changes: dict = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": workspace_id,
        "trigger_id": str(trigger_id),
    }
    if name:
        changes["name"] = name
    if filters is not None:
        changes["filters"] = filters
    if custom_event_filters is not None:
        changes["custom_event_filters"] = custom_event_filters
    if auto_event_filters is not None:
        changes["auto_event_filters"] = auto_event_filters
    if parameters is not None:
        changes["parameters"] = parameters
    if notes:
        changes["notes"] = notes

    if len(changes) <= 4:
        return {"error": "No fields to update"}

    plan = ChangePlan(
        operation="update_gtm_trigger",
        entity_type="gtm_trigger",
        entity_id=str(trigger_id),
        customer_id="",
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def draft_delete_gtm_tag(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    tag_id: str,
    workspace_id: str = "",
) -> dict:
    """Draft a deletion of a GTM tag — returns a PREVIEW.

    Deleting a tag also deletes its firing-trigger references. Triggers
    themselves are not deleted; use draft_delete_gtm_trigger for that.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("delete_gtm_tag", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not tag_id:
        return {"error": "tag_id is required"}

    plan = ChangePlan(
        operation="delete_gtm_tag",
        entity_type="gtm_tag",
        entity_id=str(tag_id),
        customer_id="",
        changes={
            "account_id": account_id,
            "container_id": container_id,
            "workspace_id": workspace_id,
            "tag_id": str(tag_id),
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Deleting a GTM tag is irreversible. Publish the workspace to make "
        "the deletion live; until then it stays as a workspace draft."
    ]
    return preview


def draft_delete_gtm_trigger(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    trigger_id: str,
    workspace_id: str = "",
) -> dict:
    """Draft a deletion of a GTM trigger — returns a PREVIEW.

    GTM blocks the deletion if the trigger is referenced by any tag
    (firing or blocking). Remove or update those tags first.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("delete_gtm_trigger", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not trigger_id:
        return {"error": "trigger_id is required"}

    plan = ChangePlan(
        operation="delete_gtm_trigger",
        entity_type="gtm_trigger",
        entity_id=str(trigger_id),
        customer_id="",
        changes={
            "account_id": account_id,
            "container_id": container_id,
            "workspace_id": workspace_id,
            "trigger_id": str(trigger_id),
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Deleting a GTM trigger is irreversible. GTM rejects the deletion "
        "if any tag references this trigger — delete or unhook those tags "
        "first."
    ]
    return preview


def publish_gtm_workspace(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
    version_name: str = "",
    version_notes: str = "",
) -> dict:
    """Draft a publish of the workspace — returns a PREVIEW.

    Publishing creates a new version from the workspace and sets it live.
    Until you call confirm_and_apply, no live change happens — the tags and
    triggers you've drafted into the workspace stay as drafts.

    version_name: optional friendly name for the version
    version_notes: optional release notes

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("publish_gtm_workspace", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    plan = ChangePlan(
        operation="publish_gtm_workspace",
        entity_type="gtm_container_version",
        entity_id=container_id,
        customer_id="",
        changes={
            "account_id": account_id,
            "container_id": container_id,
            "workspace_id": workspace_id,
            "version_name": version_name or "AdLoop publish",
            "version_notes": version_notes,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Publishing this workspace will set its drafted changes LIVE on the "
        "container. All visitors with the GTM snippet installed will start "
        "receiving the new tags + triggers within minutes of publish."
    ]
    return preview


# ---------------------------------------------------------------------------
# Apply handlers — invoked via _execute_plan in ads/write.py
# ---------------------------------------------------------------------------

def _apply_create_gtm_tag(_unused_client, _unused_cid, changes: dict) -> dict:
    """POST a new tag to GTM workspace."""
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    parent = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}"
    )
    body = {
        "name": changes["name"],
        "type": changes["type"],
        "parameter": changes.get("parameters", []) or [],
        "firingTriggerId": changes.get("firing_trigger_ids", []) or [],
        "blockingTriggerId": changes.get("blocking_trigger_ids", []) or [],
        "paused": changes.get("paused", False),
    }
    if changes.get("notes"):
        body["notes"] = changes["notes"]

    resp = (
        client.accounts()
        .containers()
        .workspaces()
        .tags()
        .create(parent=parent, body=body)
        .execute()
    )
    return {
        "tag_id": resp.get("tagId"),
        "name": resp.get("name"),
        "type": resp.get("type"),
        "workspace_id": ws_id,
        "path": resp.get("path"),
    }


def _apply_create_gtm_trigger(_unused_client, _unused_cid, changes: dict) -> dict:
    """POST a new trigger to GTM workspace."""
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    parent = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}"
    )
    body = {
        "name": changes["name"],
        "type": changes["type"],
    }
    if changes.get("filters"):
        body["filter"] = changes["filters"]
    if changes.get("custom_event_filters"):
        body["customEventFilter"] = changes["custom_event_filters"]
    if changes.get("auto_event_filters"):
        body["autoEventFilter"] = changes["auto_event_filters"]
    if changes.get("parameters"):
        body["parameter"] = changes["parameters"]
    if changes.get("notes"):
        body["notes"] = changes["notes"]
    if changes.get("custom_event_name"):
        # Custom event triggers use a customEventFilter on {{Event}}
        body.setdefault("customEventFilter", []).append({
            "type": "EQUALS",
            "parameter": [
                {"type": "TEMPLATE", "key": "arg0", "value": "{{_event}}"},
                {"type": "TEMPLATE", "key": "arg1", "value": changes["custom_event_name"]},
            ],
        })

    resp = (
        client.accounts()
        .containers()
        .workspaces()
        .triggers()
        .create(parent=parent, body=body)
        .execute()
    )
    return {
        "trigger_id": resp.get("triggerId"),
        "name": resp.get("name"),
        "type": resp.get("type"),
        "workspace_id": ws_id,
        "path": resp.get("path"),
    }


def _apply_publish_gtm_workspace(_unused_client, _unused_cid, changes: dict) -> dict:
    """Create a version from the workspace and publish it."""
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    ws_path = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}"
    )
    body = {}
    if changes.get("version_name"):
        body["name"] = changes["version_name"]
    if changes.get("version_notes"):
        body["notes"] = changes["version_notes"]

    # Step 1: create the version (compiles the workspace into a versioned snapshot)
    create_resp = (
        client.accounts()
        .containers()
        .workspaces()
        .create_version(path=ws_path, body=body)
        .execute()
    )
    if create_resp.get("compilerError"):
        return {
            "error": "GTM compiler errors",
            "details": create_resp.get("compilerError"),
        }
    version = create_resp.get("containerVersion") or {}
    version_path = version.get("path")
    if not version_path:
        return {
            "error": "GTM did not return a version path",
            "details": create_resp,
        }

    # Step 2: publish the version
    publish_resp = (
        client.accounts()
        .containers()
        .versions()
        .publish(path=version_path)
        .execute()
    )
    return {
        "version_id": version.get("containerVersionId"),
        "version_path": version_path,
        "compiler_warnings": create_resp.get("compilerError", []),
        "published": True,
        "publish_response": publish_resp,
    }


def _apply_update_gtm_tag(_unused_client, _unused_cid, changes: dict) -> dict:
    """Update an existing GTM tag in the workspace.

    Reads the existing tag first to preserve fingerprint + any unspecified
    fields, then writes the merged version back.
    """
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    tag_path = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}/tags/{changes['tag_id']}"
    )
    tags_api = client.accounts().containers().workspaces().tags()
    existing = tags_api.get(path=tag_path).execute()

    body = {
        "name": changes.get("name", existing.get("name")),
        "type": changes.get("type", existing.get("type")),
        "parameter": (
            changes["parameters"] if "parameters" in changes
            else existing.get("parameter", [])
        ),
        "firingTriggerId": (
            changes["firing_trigger_ids"]
            if "firing_trigger_ids" in changes
            else existing.get("firingTriggerId", [])
        ),
        "blockingTriggerId": (
            changes["blocking_trigger_ids"]
            if "blocking_trigger_ids" in changes
            else existing.get("blockingTriggerId", [])
        ),
        "paused": (
            changes["paused"] if "paused" in changes
            else existing.get("paused", False)
        ),
        "fingerprint": existing.get("fingerprint"),
    }
    if "notes" in changes:
        body["notes"] = changes["notes"]
    elif existing.get("notes"):
        body["notes"] = existing["notes"]

    resp = tags_api.update(path=tag_path, body=body).execute()
    return {
        "tag_id": resp.get("tagId"),
        "name": resp.get("name"),
        "type": resp.get("type"),
        "workspace_id": ws_id,
        "path": resp.get("path"),
    }


def _apply_update_gtm_trigger(_unused_client, _unused_cid, changes: dict) -> dict:
    """Update an existing GTM trigger in the workspace."""
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    trig_path = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}/triggers/{changes['trigger_id']}"
    )
    triggers_api = client.accounts().containers().workspaces().triggers()
    existing = triggers_api.get(path=trig_path).execute()

    body = {
        "name": changes.get("name", existing.get("name")),
        "type": existing.get("type"),  # immutable
        "fingerprint": existing.get("fingerprint"),
    }
    # Preserve fields the caller didn't ask to change.
    for src_key, body_key in (
        ("filters", "filter"),
        ("custom_event_filters", "customEventFilter"),
        ("auto_event_filters", "autoEventFilter"),
        ("parameters", "parameter"),
    ):
        if src_key in changes:
            body[body_key] = changes[src_key]
        elif body_key in existing:
            body[body_key] = existing[body_key]

    for keep in ("waitForTags", "checkValidation", "waitForTagsTimeout",
                 "uniqueTriggerId", "parentFolderId"):
        if keep in existing:
            body[keep] = existing[keep]

    if "notes" in changes:
        body["notes"] = changes["notes"]
    elif existing.get("notes"):
        body["notes"] = existing["notes"]

    resp = triggers_api.update(path=trig_path, body=body).execute()
    return {
        "trigger_id": resp.get("triggerId"),
        "name": resp.get("name"),
        "type": resp.get("type"),
        "workspace_id": ws_id,
        "path": resp.get("path"),
    }


def _apply_delete_gtm_tag(_unused_client, _unused_cid, changes: dict) -> dict:
    """Delete a GTM tag from the workspace."""
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    tag_path = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}/tags/{changes['tag_id']}"
    )
    tags_api = client.accounts().containers().workspaces().tags()
    tags_api.delete(path=tag_path).execute()
    return {"deleted_tag_id": changes["tag_id"], "workspace_id": ws_id}


def _apply_delete_gtm_trigger(_unused_client, _unused_cid, changes: dict) -> dict:
    """Delete a GTM trigger from the workspace."""
    from adloop.config import load_config
    from adloop.gtm.client import get_gtm_client

    cfg = load_config()
    client = get_gtm_client(cfg)
    ws_id = _resolve_workspace(
        client, changes["account_id"], changes["container_id"],
        changes.get("workspace_id", "")
    )
    trig_path = (
        f"accounts/{changes['account_id']}"
        f"/containers/{changes['container_id']}"
        f"/workspaces/{ws_id}/triggers/{changes['trigger_id']}"
    )
    triggers_api = client.accounts().containers().workspaces().triggers()
    triggers_api.delete(path=trig_path).execute()
    return {"deleted_trigger_id": changes["trigger_id"], "workspace_id": ws_id}
