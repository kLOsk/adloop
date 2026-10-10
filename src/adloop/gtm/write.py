"""GTM write tools — Google Tag Manager API v2.

Same safety model as every other AdLoop write:

    1. ``draft_*`` validates, reads the current state, stores a ChangePlan
       and returns a PREVIEW — nothing is sent to Tag Manager.
    2. ``confirm_and_apply(plan_id, dry_run=true)`` runs :func:`preflight`:
       Tag Manager has no validate-only mode, so the dry run re-reads the
       target and re-checks every gate. For a publish it also runs a quick
       preview (a POST that stores a preview version, publishes nothing).
    3. ``confirm_and_apply(plan_id, dry_run=false)`` runs :func:`apply_plan`.

Three gates sit on top of that, all enforced here rather than advised:

* ``gtm.write_enabled`` — writes are opt-in. The edit + publish OAuth scopes
  are only requested when it is on (see ``adloop.auth._requested_scopes``).
* ``gtm.allow_custom_html`` — Custom HTML tags run arbitrary JavaScript on
  every page they fire on. Creating or editing one, or publishing a
  workspace (or rolling back to a version) that adds or changes one, is
  refused unless this is on. Pausing
  or deleting one is always allowed (both reduce what runs). The gate matches
  the tag type ``html``: a Custom JavaScript variable or a custom template
  tag (``cvt_…``), which can inject a script, is not covered and has to be
  reviewed in the GTM UI.
* Concurrency — every update/delete pins the entity fingerprint read at draft
  time, and publish pins a digest of the workspace's pending changes. A
  rollback pins the live version it replaces and the target's fingerprint. If
  a human edits the container between preview and apply, apply refuses instead
  of writing (or publishing) something nobody reviewed.

Tag and trigger edits land in a workspace (a draft) and do nothing on the site
until the workspace is published with ``draft_publish_gtm_workspace``. A
rollback (``draft_rollback_gtm_version``) republishes an existing version and
leaves every workspace untouched.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from adloop.gtm.entities import ENTITY_ID_FIELDS, ENTITY_KINDS, entity_id

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

PLATFORM = "gtm"

# Canonical GTM template IDs. Anything else is rejected at draft time — an
# unknown type previews clean and then fails at apply with an opaque 400.
# Community Gallery templates use ``cvt_<id>`` and are accepted by prefix.
KNOWN_TAG_TYPES: dict[str, str] = {
    "googtag": "Google Tag (gtag.js config — G-… / AW-… IDs)",
    "gaawe": "GA4 Event",
    "awct": "Google Ads Conversion Tracking",
    "awcc": "Google Ads Calls from Website Conversion",
    "awud": "Google Ads User-Provided Data Event",
    "sp": "Google Ads Remarketing",
    "gclidw": "Conversion Linker",
    "flc": "Floodlight Counter",
    "fls": "Floodlight Sales",
    "img": "Custom Image",
    "html": "Custom HTML (gated by gtm.allow_custom_html)",
}
CUSTOM_HTML = "html"
_CUSTOM_TEMPLATE_PREFIX = "cvt_"

# GTM API trigger type enum values (camelCase — the API rejects snake_case).
KNOWN_TRIGGER_TYPES = {
    "pageview", "domReady", "windowLoaded",
    "click", "linkClick", "formSubmission",
    "customEvent", "elementVisibility", "scrollDepth",
    "youTubeVideo", "historyChange", "timer", "jsError",
    "triggerGroup",
}

# Trigger request fields the draft tool can set, keyed by the tool's
# snake_case argument name.
_TRIGGER_FIELDS = {
    "filters": "filter",
    "custom_event_filters": "customEventFilter",
    "auto_event_filters": "autoEventFilter",
    "parameters": "parameter",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_writes(config: AdLoopConfig) -> None:
    if not config.gtm.write_enabled:
        from adloop.runtime import deployment_mode

        if deployment_mode() == "server":
            # Hosted: there is no config file for the user to edit and no
            # server for them to restart — the flag is set on the AdLoop Cloud
            # side, by whoever runs that deployment.
            raise RuntimeError(
                "GTM writes are disabled for this workspace. They are enabled "
                "on the AdLoop Cloud side; turning them on also asks for a "
                "re-consent with the Tag Manager edit + publish scopes."
            )
        raise RuntimeError(
            "GTM writes are disabled. Set 'write_enabled: true' under 'gtm:' "
            "in the AdLoop config and restart the MCP server; the next call "
            "asks you to re-consent with the Tag Manager edit + publish scopes."
        )


def _custom_html_refusal(what: str) -> dict:
    return {
        "error": (
            f"Refused: {what} involves a Custom HTML tag, which runs arbitrary "
            "JavaScript on every page it fires on."
        ),
        "hint": (
            "If this is intended, set 'allow_custom_html: true' under 'gtm:' "
            "in the AdLoop config and restart the MCP server. Prefer a "
            "built-in template (googtag, gaawe, awct, …) where one exists."
        ),
        "gate": "gtm.allow_custom_html",
    }


def _blocked(operation: str, config: AdLoopConfig) -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def _client(config: AdLoopConfig):
    from adloop.gtm.client import get_gtm_write_client

    return get_gtm_write_client(config)


def _workspaces(client):
    return client.accounts().containers().workspaces()


def _container_path(account_id: str, container_id: str) -> str:
    return f"accounts/{account_id}/containers/{container_id}"


def _live_version(client, account_id: str, container_id: str) -> dict:
    """The version that is live right now — the rollback target.

    Best effort: a container that was never published has no live version, and
    a missing live version must not stop a publish. If the call fails for any
    other reason the surrounding read would have failed too.

    ``accounts.containers.versions.live`` answers with a bare ``ContainerVersion``
    (checked against the v2 discovery document and the REST reference) — not
    with a ``containerVersion`` wrapper like ``create_version`` does. Both
    shapes are accepted so a wrapper can never turn the rollback id into
    ``None`` without anybody noticing.
    """
    try:
        response = (
            client.accounts()
            .containers()
            .versions()
            .live(parent=_container_path(account_id, container_id))
            .execute()
        )
    except Exception:  # noqa: BLE001 — bookkeeping, never a reason to abort
        return {"version_id": None, "version_name": None}
    version = response.get("containerVersion") or response
    return {
        "version_id": version.get("containerVersionId"),
        "version_name": version.get("name"),
    }


def _resolve_workspace(
    client, account_id: str, container_id: str, workspace_id: str = ""
) -> tuple[str, str]:
    """Return ``(workspace_id, workspace_name)``.

    Resolved at DRAFT time and pinned in the plan, so the preview names the
    exact workspace apply will write to. Empty ``workspace_id`` picks the
    Default Workspace (or the only one).
    """
    parent = _container_path(account_id, container_id)
    resp = _workspaces(client).list(parent=parent).execute()
    workspaces = resp.get("workspace", []) or []
    if workspace_id:
        for w in workspaces:
            if str(w.get("workspaceId")) == str(workspace_id):
                return str(w["workspaceId"]), w.get("name", "")
        raise ValueError(
            f"Workspace '{workspace_id}' not found under {parent}. "
            "Call list_gtm_workspaces for valid IDs."
        )
    for w in workspaces:
        if w.get("name") == "Default Workspace":
            return str(w["workspaceId"]), w["name"]
    if len(workspaces) == 1:
        return str(workspaces[0]["workspaceId"]), workspaces[0].get("name", "")
    if not workspaces:
        raise ValueError(f"No workspaces found under {parent}.")
    raise ValueError(
        f"{parent} has {len(workspaces)} workspaces and none is named "
        "'Default Workspace' — pass workspace_id explicitly "
        "(see list_gtm_workspaces)."
    )


def _ws_path(changes: dict) -> str:
    return (
        f"{_container_path(changes['account_id'], changes['container_id'])}"
        f"/workspaces/{changes['workspace_id']}"
    )


def _validate_tag_type(tag_type: str) -> str | None:
    if tag_type in KNOWN_TAG_TYPES or tag_type.startswith(_CUSTOM_TEMPLATE_PREFIX):
        return None
    return (
        f"tag_type '{tag_type}' is not a GTM template ID. Valid: "
        f"{', '.join(sorted(KNOWN_TAG_TYPES))}, or cvt_<id> for a Community "
        "Gallery template."
    )


def _validate_parameters(parameters: list[dict] | None, label: str) -> list[str]:
    errors = []
    for i, p in enumerate(parameters or []):
        if not isinstance(p, dict) or not p.get("key") or not p.get("type"):
            errors.append(
                f"{label}[{i}] must be a dict with at least 'type' and 'key' "
                "(e.g. {'type': 'TEMPLATE', 'key': 'eventName', 'value': 'x'})"
            )
    return errors


def _merge_parameters(existing: list[dict], updates: list[dict]) -> list[dict]:
    """Merge by ``key``: passed keys replace, new keys append, others kept."""
    merged = [copy.deepcopy(p) for p in existing or []]
    index = {p.get("key"): i for i, p in enumerate(merged)}
    for p in updates:
        if p.get("key") in index:
            merged[index[p["key"]]] = copy.deepcopy(p)
        else:
            index[p.get("key")] = len(merged)
            merged.append(copy.deepcopy(p))
    return merged


def _store(operation: str, entity_type: str, entity_id: str, changes: dict,
           *, warnings: list[str] | None = None,
           double_confirm: bool = False) -> dict:
    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(
        operation=operation,
        entity_type=entity_type,
        entity_id=str(entity_id),
        customer_id="",
        changes=changes,
        requires_double_confirm=double_confirm,
        platform=PLATFORM,
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _workspace_digest(status: dict) -> str:
    """Stable digest of a workspace's pending changes (for publish pinning)."""
    items = []
    for change in status.get("workspaceChange", []) or []:
        for kind in ENTITY_KINDS:
            if kind in change:
                entity = change[kind]
                items.append([
                    kind,
                    entity_id(kind, entity),
                    change.get("changeStatus", ""),
                    str(entity.get("fingerprint", "")),
                ])
    items.sort()
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def _summarize_changes(status: dict) -> list[dict]:
    out = []
    for change in status.get("workspaceChange", []) or []:
        for kind in ENTITY_KINDS:
            if kind in change:
                entity = change[kind]
                out.append({
                    "change_status": change.get("changeStatus"),
                    "entity_kind": kind,
                    "entity_id": entity.get(
                        ENTITY_ID_FIELDS.get(kind, f"{kind}Id")
                    ),
                    "name": entity.get("name"),
                    "type": entity.get("type"),
                    "paused": entity.get("paused"),
                })
    return out


def _custom_html_changes(status: dict) -> list[dict]:
    """Custom HTML tags the publish would add or change (deletes are fine)."""
    return [
        {"tag_id": c["tag"].get("tagId"), "name": c["tag"].get("name"),
         "change_status": c.get("changeStatus")}
        for c in status.get("workspaceChange", []) or []
        if "tag" in c
        and c["tag"].get("type") == CUSTOM_HTML
        and c.get("changeStatus") != "deleted"
    ]


def _custom_html_pending_changes(pending: list[dict]) -> list[dict]:
    """The same check against a stored preview's summarized pending changes."""
    return [
        {"tag_id": c.get("entity_id"), "name": c.get("name"),
         "change_status": c.get("change_status")}
        for c in pending
        if c.get("entity_kind") == "tag"
        and c.get("type") == CUSTOM_HTML
        and c.get("change_status") != "deleted"
    ]


def _translate_http_error(exc: Exception) -> Exception:
    """Turn a missing-scope 403 into an actionable error; pass others through."""
    text = str(exc).lower()
    if "insufficient" in text and ("scope" in text or "permission" in text):
        return RuntimeError(
            "Tag Manager rejected the write: the stored OAuth token lacks the "
            "edit/publish scopes. Confirm 'write_enabled: true' under 'gtm:', "
            "delete ~/.adloop/token.json and re-run any tool to re-consent. "
            "The Google account also needs Edit (and, to publish, Publish) "
            "permission on the container in GTM → Admin → User Management."
        )
    return exc


# ---------------------------------------------------------------------------
# Draft tools
# ---------------------------------------------------------------------------


def draft_gtm_tag(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    tag_id: str = "",
    workspace_id: str = "",
    name: str = "",
    tag_type: str = "",
    parameters: list[dict] | None = None,
    firing_trigger_ids: list[str] | None = None,
    blocking_trigger_ids: list[str] | None = None,
    paused: bool | None = None,
    notes: str | None = None,
) -> dict:
    """Create (no ``tag_id``) or update (``tag_id`` set) a workspace tag."""
    _require_writes(config)
    operation = "gtm_update_tag" if tag_id else "gtm_create_tag"
    blocked = _blocked(operation, config)
    if blocked:
        return blocked

    errors = _validate_parameters(parameters, "parameters")
    if name and len(name) > 200:
        errors.append("name must be 1-200 characters")
    if tag_type:
        type_error = _validate_tag_type(tag_type)
        if type_error:
            errors.append(type_error)
    if not tag_id:
        if not name:
            errors.append("name is required when creating a tag")
        if not tag_type:
            errors.append("tag_type is required when creating a tag")
    if errors:
        return {"error": "Validation failed", "details": errors}

    client = _client(config)
    ws_id, ws_name = _resolve_workspace(client, account_id, container_id, workspace_id)
    base = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": ws_id,
        "workspace_name": ws_name,
    }
    warnings = [
        f"This edits workspace '{ws_name}' only. Nothing changes on the site "
        "until the workspace is published (draft_publish_gtm_workspace)."
    ]

    if not tag_id:
        if tag_type == CUSTOM_HTML and not config.gtm.allow_custom_html:
            return _custom_html_refusal("creating this tag")
        body: dict[str, Any] = {
            "name": name,
            "type": tag_type,
            "parameter": parameters or [],
            "firingTriggerId": [str(t) for t in firing_trigger_ids or []],
        }
        if blocking_trigger_ids:
            body["blockingTriggerId"] = [str(t) for t in blocking_trigger_ids]
        if paused is not None:
            body["paused"] = bool(paused)
        if notes:
            body["notes"] = notes
        if not body["firingTriggerId"]:
            warnings.append(
                "No firing_trigger_ids — the tag will never fire. The built-in "
                "'All Pages' trigger is 2147479553."
            )
        return _store(operation, "gtm_tag", ws_id, {**base, "tag": body},
                      warnings=warnings)

    # Update: read the tag now so the preview shows before/after and the
    # plan pins the fingerprint that apply will insist on.
    tag_path = f"{_ws_path(base)}/tags/{tag_id}"
    existing = _workspaces(client).tags().get(path=tag_path).execute()
    if tag_type and tag_type != existing.get("type"):
        return {
            "error": (
                f"Tag {tag_id} is type '{existing.get('type')}'; GTM cannot "
                "change a tag's type. Delete it and create a new tag instead."
            ),
        }

    patch: dict[str, Any] = {}
    if name:
        patch["name"] = name
    if parameters is not None:
        patch["parameter"] = _merge_parameters(existing.get("parameter", []), parameters)
    if firing_trigger_ids is not None:
        patch["firingTriggerId"] = [str(t) for t in firing_trigger_ids]
    if blocking_trigger_ids is not None:
        patch["blockingTriggerId"] = [str(t) for t in blocking_trigger_ids]
    if paused is not None:
        patch["paused"] = bool(paused)
    if notes is not None:
        patch["notes"] = notes
    patch = {k: v for k, v in patch.items() if existing.get(k) != v}
    if not patch:
        return {"error": "No changes — every field passed already matches the tag."}

    only_pausing = patch == {"paused": True}
    if (existing.get("type") == CUSTOM_HTML and not only_pausing
            and not config.gtm.allow_custom_html):
        return _custom_html_refusal("editing this tag")

    if firing_trigger_ids is not None and not patch.get("firingTriggerId", True):
        warnings.append("firing_trigger_ids is empty — the tag will never fire.")

    changes = {
        **base,
        "tag_id": str(tag_id),
        "tag_name": existing.get("name"),
        "tag_type": existing.get("type"),
        "fingerprint": existing.get("fingerprint"),
        "patch": patch,
        "before": {k: existing.get(k) for k in patch},
    }
    return _store(operation, "gtm_tag", tag_id, changes, warnings=warnings)


def draft_gtm_trigger(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    trigger_id: str = "",
    workspace_id: str = "",
    name: str = "",
    trigger_type: str = "",
    custom_event_name: str = "",
    filters: list[dict] | None = None,
    custom_event_filters: list[dict] | None = None,
    auto_event_filters: list[dict] | None = None,
    parameters: list[dict] | None = None,
    notes: str | None = None,
) -> dict:
    """Create (no ``trigger_id``) or update (``trigger_id`` set) a trigger."""
    _require_writes(config)
    operation = "gtm_update_trigger" if trigger_id else "gtm_create_trigger"
    blocked = _blocked(operation, config)
    if blocked:
        return blocked

    field_args = {
        "filters": filters,
        "custom_event_filters": custom_event_filters,
        "auto_event_filters": auto_event_filters,
        "parameters": parameters,
    }
    errors: list[str] = []
    for arg in ("filters", "custom_event_filters", "auto_event_filters"):
        for i, f in enumerate(field_args[arg] or []):
            if not isinstance(f, dict) or not f.get("type") or "parameter" not in f:
                errors.append(
                    f"{arg}[{i}] must be a GTM condition dict with 'type' "
                    "(EQUALS, CONTAINS, MATCH_REGEX, …) and 'parameter' "
                    "(arg0 = variable, arg1 = value)"
                )
    errors += _validate_parameters(parameters, "parameters")
    if name and len(name) > 200:
        errors.append("name must be 1-200 characters")
    if trigger_id and trigger_type:
        errors.append(
            "trigger_type cannot be changed on an existing trigger — delete it "
            "and create a new one instead"
        )
    if not trigger_id:
        if not name:
            errors.append("name is required when creating a trigger")
        if trigger_type not in KNOWN_TRIGGER_TYPES:
            errors.append(
                f"trigger_type '{trigger_type}' invalid; valid: "
                f"{', '.join(sorted(KNOWN_TRIGGER_TYPES))}"
            )
        if trigger_type == "customEvent" and not custom_event_name:
            errors.append("custom_event_name is required when trigger_type=customEvent")
    elif custom_event_name:
        errors.append(
            "custom_event_name cannot be changed on an existing trigger — the "
            "event name lives in the trigger's custom event filter. Edit it in "
            "the GTM UI, or delete the trigger and create a new one."
        )
    if custom_event_name and trigger_type and trigger_type != "customEvent":
        errors.append("custom_event_name only applies to trigger_type=customEvent")
    if errors:
        return {"error": "Validation failed", "details": errors}

    client = _client(config)
    ws_id, ws_name = _resolve_workspace(client, account_id, container_id, workspace_id)
    base = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": ws_id,
        "workspace_name": ws_name,
    }
    warnings = [
        f"This edits workspace '{ws_name}' only. Nothing changes on the site "
        "until the workspace is published (draft_publish_gtm_workspace)."
    ]

    if not trigger_id:
        body: dict[str, Any] = {"name": name, "type": trigger_type}
        for arg, api_field in _TRIGGER_FIELDS.items():
            if field_args[arg]:
                body[api_field] = list(field_args[arg])
        if custom_event_name:
            # Custom-event triggers match the dataLayer event via {{_event}}.
            body.setdefault("customEventFilter", []).append({
                "type": "EQUALS",
                "parameter": [
                    {"type": "TEMPLATE", "key": "arg0", "value": "{{_event}}"},
                    {"type": "TEMPLATE", "key": "arg1", "value": custom_event_name},
                ],
            })
        if notes:
            body["notes"] = notes
        return _store(operation, "gtm_trigger", ws_id, {**base, "trigger": body},
                      warnings=warnings)

    trig_path = f"{_ws_path(base)}/triggers/{trigger_id}"
    existing = _workspaces(client).triggers().get(path=trig_path).execute()
    patch: dict[str, Any] = {}
    if name:
        patch["name"] = name
    for arg, api_field in _TRIGGER_FIELDS.items():
        if field_args[arg] is not None:
            patch[api_field] = (
                _merge_parameters(existing.get("parameter", []), field_args[arg])
                if arg == "parameters" else list(field_args[arg])
            )
    if notes is not None:
        patch["notes"] = notes
    patch = {k: v for k, v in patch.items() if existing.get(k) != v}
    if not patch:
        return {"error": "No changes — every field passed already matches the trigger."}

    changes = {
        **base,
        "trigger_id": str(trigger_id),
        "trigger_name": existing.get("name"),
        "trigger_type": existing.get("type"),
        "fingerprint": existing.get("fingerprint"),
        "patch": patch,
        "before": {k: existing.get(k) for k in patch},
    }
    warnings.append(
        "Every tag that fires on (or is blocked by) this trigger picks up the "
        "new conditions."
    )
    return _store(operation, "gtm_trigger", trigger_id, changes, warnings=warnings)


def draft_delete_gtm_entity(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    entity_type: str,
    entity_id: str,
    workspace_id: str = "",
) -> dict:
    """Delete a workspace tag or trigger (``entity_type`` = tag | trigger)."""
    _require_writes(config)
    if entity_type not in ("tag", "trigger"):
        return {"error": "entity_type must be 'tag' or 'trigger'"}
    operation = f"gtm_delete_{entity_type}"
    blocked = _blocked(operation, config)
    if blocked:
        return blocked
    if not entity_id:
        return {"error": "entity_id is required"}

    client = _client(config)
    ws_id, ws_name = _resolve_workspace(client, account_id, container_id, workspace_id)
    base = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": ws_id,
        "workspace_name": ws_name,
    }
    ws = _workspaces(client)
    api = ws.tags() if entity_type == "tag" else ws.triggers()
    existing = api.get(path=f"{_ws_path(base)}/{entity_type}s/{entity_id}").execute()

    if entity_type == "trigger":
        # GTM refuses to delete a referenced trigger; say which tags up front
        # instead of letting apply fail with a bare 400.
        tags = ws.tags().list(parent=_ws_path(base)).execute().get("tag", []) or []
        users = [
            {"tag_id": t.get("tagId"), "name": t.get("name")}
            for t in tags
            if str(entity_id) in (t.get("firingTriggerId") or [])
            or str(entity_id) in (t.get("blockingTriggerId") or [])
        ]
        if users:
            return {
                "error": (
                    f"Trigger {entity_id} is used by {len(users)} tag(s); GTM "
                    "will not delete it. Remove it from those tags first "
                    "(draft_gtm_tag with new firing/blocking_trigger_ids)."
                ),
                "referenced_by": users,
            }

    changes = {
        **base,
        f"{entity_type}_id": str(entity_id),
        "name": existing.get("name"),
        "type": existing.get("type"),
        "fingerprint": existing.get("fingerprint"),
    }
    warnings = [
        f"Deletes {entity_type} '{existing.get('name')}' from workspace "
        f"'{ws_name}'. It stays live on the site until the workspace is "
        "published; once published, restoring it means re-creating it.",
    ]
    return _store(operation, f"gtm_{entity_type}", entity_id, changes,
                  warnings=warnings, double_confirm=True)


def draft_publish_gtm_workspace(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    workspace_id: str = "",
    version_name: str = "",
    version_notes: str = "",
) -> dict:
    """Preview publishing a workspace: every pending change goes LIVE."""
    _require_writes(config)
    blocked = _blocked("gtm_publish_workspace", config)
    if blocked:
        return blocked

    client = _client(config)
    ws_id, ws_name = _resolve_workspace(client, account_id, container_id, workspace_id)
    base = {
        "account_id": account_id,
        "container_id": container_id,
        "workspace_id": ws_id,
        "workspace_name": ws_name,
    }
    status = _workspaces(client).getStatus(path=_ws_path(base)).execute()

    if status.get("mergeConflict"):
        return {
            "error": (
                f"Workspace '{ws_name}' has merge conflicts with the live "
                "version. Resolve them in the GTM UI, then draft again."
            ),
            "merge_conflict": status["mergeConflict"],
        }
    pending = _summarize_changes(status)
    if not pending:
        return {"error": f"Workspace '{ws_name}' has no pending changes to publish."}
    html = _custom_html_changes(status)
    if html and not config.gtm.allow_custom_html:
        refusal = _custom_html_refusal("publishing this workspace")
        refusal["custom_html_tags"] = html
        return refusal

    changes = {
        **base,
        "version_name": version_name
        or f"AdLoop publish {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        "version_notes": version_notes,
        "pending_changes": pending,
        "workspace_digest": _workspace_digest(status),
    }
    live = _live_version(client, account_id, container_id)
    changes["previous_live_version_id"] = live["version_id"]
    changes["previous_live_version_name"] = live["version_name"]
    warnings = [
        f"Publishing sets all {len(pending)} pending change(s) in workspace "
        f"'{ws_name}' LIVE for every visitor — including changes made by "
        "other people in the GTM UI, all listed in pending_changes.",
        "If the workspace changes after this preview, apply refuses and you "
        "must draft the publish again.",
    ]
    if live["version_id"]:
        warnings.append(
            f"This replaces live version {live['version_id']} "
            f"('{live['version_name']}'); draft_rollback_gtm_version with that "
            "id is the rollback."
        )
    if html:
        warnings.append(
            f"{len(html)} Custom HTML tag(s) will go live (allowed by "
            "gtm.allow_custom_html)."
        )
    return _store("gtm_publish_workspace", "gtm_workspace", ws_id, changes,
                  warnings=warnings, double_confirm=True)


def _custom_html_in_diff(diff: dict) -> list[dict]:
    """Custom HTML tags a version change would add or change (removals are fine)."""
    tags = diff.get("tags", {})
    return [
        {"tag_id": t.get("id"), "name": t.get("name"), "change_status": status}
        for status in ("added", "changed")
        for t in tags.get(status, [])
        if t.get("type") == CUSTOM_HTML
    ]


def draft_rollback_gtm_version(
    config: AdLoopConfig,
    *,
    account_id: str,
    container_id: str,
    version_id: str,
) -> dict:
    """Preview republishing an older container version LIVE (a rollback).

    Two API calls: ``versions.live`` (the version being replaced, pinned in
    the plan) and ``versions.get`` (the target). The preview is the diff from
    the live version to the target, so it shows exactly what changes on the
    site.
    """
    _require_writes(config)
    blocked = _blocked("gtm_rollback_version", config)
    if blocked:
        return blocked
    version_id = str(version_id or "").strip()
    if not version_id:
        return {"error": "version_id is required (see list_gtm_versions)."}

    from adloop.gtm.read import diff_container_versions

    client = _client(config)
    versions = client.accounts().containers().versions()
    base = _container_path(account_id, container_id)
    try:
        live = versions.live(parent=base).execute()
        live = live.get("containerVersion") or live
        target = versions.get(path=f"{base}/versions/{version_id}").execute()
    except Exception as exc:
        raise _translate_http_error(exc) from exc

    live_id = live.get("containerVersionId")
    if not live_id:
        return {
            "error": (
                f"Container {container_id} has no live version to roll back "
                "from. Publish a workspace instead (draft_publish_gtm_workspace)."
            ),
        }
    if str(live_id) == version_id:
        return {"error": f"Version {version_id} is already the live version."}
    if target.get("deleted"):
        return {
            "error": (
                f"Version {version_id} is archived (deleted). Restore it in the "
                "GTM UI before rolling back to it."
            ),
        }
    if not target.get("path"):
        return {"error": f"Tag Manager returned no version {version_id}."}

    diff = diff_container_versions(live, target)
    html = _custom_html_in_diff(diff)
    if html and not config.gtm.allow_custom_html:
        refusal = _custom_html_refusal("rolling back to this version")
        refusal["custom_html_tags"] = html
        return refusal

    changes = {
        "account_id": account_id,
        "container_id": container_id,
        "target_version_id": version_id,
        "target_version_name": target.get("name"),
        "target_version_path": target["path"],
        "target_fingerprint": target.get("fingerprint"),
        "previous_live_version_id": str(live_id),
        "previous_live_version_name": live.get("name"),
        "diff_from_live": {
            "summary": diff["summary"],
            "tags": diff["tags"],
            "triggers": diff["triggers"],
            "variables": diff["variables"],
            "built_in_variables": diff["built_in_variables"],
        },
        "custom_html_tags": html,
    }
    warnings = [
        f"Publishing version {version_id} ('{target.get('name')}') replaces live "
        f"version {live_id} ('{live.get('name')}') for every visitor. The diff "
        "lists every tag, trigger and variable that changes on the site.",
        f"Reversible: rolling back to version {live_id} restores today's state.",
        "If another version goes live after this preview, apply refuses and you "
        "must draft the rollback again.",
        "Workspaces are not changed: they still hold the newer configuration, "
        "so publishing a workspace later brings the rolled-back changes back "
        "unless they are reverted there first.",
    ]
    if diff["is_identical"]:
        warnings.append(
            "No tag, trigger, variable or built-in variable differs from the live "
            "version; other entity kinds (folders, templates, clients) may."
        )
    if html:
        warnings.append(
            f"{len(html)} Custom HTML tag(s) will go live (allowed by "
            "gtm.allow_custom_html)."
        )
    return _store("gtm_rollback_version", "gtm_version", version_id, changes,
                  warnings=warnings, double_confirm=True)


# ---------------------------------------------------------------------------
# Dry run + apply — called by confirm_and_apply in ads/write.py
# ---------------------------------------------------------------------------


def _check_fingerprint(api, path: str, expected: str | None, label: str) -> dict:
    current = api.get(path=path).execute()
    if expected and current.get("fingerprint") != expected:
        raise RuntimeError(
            f"{label} was modified in Tag Manager after this plan was "
            "previewed; nothing was sent. Draft the change again so the "
            "preview reflects the current state."
        )
    return current


def _check_workspace_unchanged(client, plan: ChangePlan) -> dict:
    changes = plan.changes
    status = _workspaces(client).getStatus(path=_ws_path(changes)).execute()
    if status.get("mergeConflict"):
        raise RuntimeError(
            "The workspace now has merge conflicts with the live version; "
            "nothing was published. Resolve them in the GTM UI and draft again."
        )
    if _workspace_digest(status) != changes["workspace_digest"]:
        raise RuntimeError(
            "The workspace's pending changes differ from what was previewed "
            "(someone edited it since); nothing was published. Draft the "
            "publish again and review the new pending_changes."
        )
    return status


def _check_live_unchanged(client, plan: ChangePlan) -> dict:
    """Refuse a rollback when the live version moved since the preview."""
    changes = plan.changes
    live = (
        client.accounts()
        .containers()
        .versions()
        .live(parent=_container_path(changes["account_id"], changes["container_id"]))
        .execute()
    )
    live = live.get("containerVersion") or live
    if str(live.get("containerVersionId")) != str(changes["previous_live_version_id"]):
        raise RuntimeError(
            f"The live version is now {live.get('containerVersionId')}, not "
            f"{changes['previous_live_version_id']} as previewed (someone "
            "published in between); nothing was published. Draft the rollback "
            "again so the preview shows the current diff."
        )
    return live


def _check_target_unchanged(client, plan: ChangePlan) -> dict:
    changes = plan.changes
    target = (
        client.accounts()
        .containers()
        .versions()
        .get(path=changes["target_version_path"])
        .execute()
    )
    if target.get("deleted"):
        raise RuntimeError(
            f"Version {changes['target_version_id']} was archived after the "
            "preview; nothing was published."
        )
    expected = changes.get("target_fingerprint")
    if expected and target.get("fingerprint") != expected:
        raise RuntimeError(
            f"Version {changes['target_version_id']} was modified after the "
            "preview; nothing was published. Draft the rollback again."
        )
    return target


def _entity_api(client, plan: ChangePlan):
    ws = _workspaces(client)
    return ws.tags() if plan.entity_type == "gtm_tag" else ws.triggers()


def _entity_path(plan: ChangePlan) -> str:
    kind = "tag" if plan.entity_type == "gtm_tag" else "trigger"
    return f"{_ws_path(plan.changes)}/{kind}s/{plan.changes[f'{kind}_id']}"


def _check_gates(config: AdLoopConfig, plan: ChangePlan) -> None:
    """Re-run the config gates at apply time (config may have changed)."""
    _require_writes(config)
    if config.gtm.allow_custom_html:
        return
    changes = plan.changes
    op = plan.operation
    if op == "gtm_rollback_version":
        html = bool(changes.get("custom_html_tags"))
    elif op == "gtm_publish_workspace":
        # The publish draft checked the live workspace, but the flag can be
        # switched off in between and an apply needs no dry run, so the stored
        # preview is checked here as well.
        html = bool(
            _custom_html_pending_changes(changes.get("pending_changes") or [])
        )
    elif op == "gtm_update_tag":
        html = (changes.get("tag_type") == CUSTOM_HTML
                and changes["patch"] != {"paused": True})
    else:
        html = op == "gtm_create_tag" and changes["tag"].get("type") == CUSTOM_HTML
    if html:
        what = {
            "gtm_publish_workspace": "publishing this workspace",
            "gtm_rollback_version": "rolling back to this version",
        }.get(op, "this plan")
        raise RuntimeError(_custom_html_refusal(what)["error"])


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read the target and re-check every gate; nothing goes live.

    The publish preflight additionally runs ``workspaces.quick_preview`` — a
    POST that compiles the workspace and stores a preview version, so compiler
    errors are caught before ``create_version`` runs. It publishes nothing and
    changes no live tag.
    """
    _check_gates(config, plan)
    client = _client(config)
    changes = plan.changes
    checks: dict[str, Any] = {"workspace": changes.get("workspace_name")}
    try:
        if plan.operation in ("gtm_create_tag", "gtm_create_trigger"):
            _workspaces(client).get(path=_ws_path(changes)).execute()
            checks["workspace_exists"] = True
        elif plan.operation == "gtm_publish_workspace":
            status = _check_workspace_unchanged(client, plan)
            html = _custom_html_changes(status)
            if html and not config.gtm.allow_custom_html:
                raise RuntimeError(_custom_html_refusal("publishing this workspace")["error"])
            # Tag Manager has no validate-only mode, but quick_preview compiles
            # the workspace and reports compiler errors — exactly what would
            # make create_version produce a version that cannot be published.
            # It creates a preview version; nothing goes live.
            quick = (
                _workspaces(client)
                .quick_preview(path=_ws_path(changes))
                .execute()
            )
            if quick.get("compilerError"):
                raise RuntimeError(
                    "Tag Manager's quick preview reports compiler errors in "
                    "this workspace, so the publish would fail after a version "
                    "was created. Nothing was versioned and nothing was "
                    "published. Open the workspace in the GTM UI, fix the "
                    "errors, and draft the publish again."
                )
            live = _live_version(
                client, changes["account_id"], changes["container_id"]
            )
            checks["pending_changes_unchanged"] = True
            checks["pending_change_count"] = len(changes["pending_changes"])
            checks["quick_preview"] = "compiles"
            checks["previous_live_version_id"] = live["version_id"]
        elif plan.operation == "gtm_rollback_version":
            # versions.publish has no validate-only mode either; re-read the
            # live version (the pin) and the target (archived / modified).
            _check_live_unchanged(client, plan)
            _check_target_unchanged(client, plan)
            checks["live_version_unchanged"] = True
            checks["target_version_unchanged"] = True
            checks["previous_live_version_id"] = changes["previous_live_version_id"]
        else:
            current = _check_fingerprint(
                _entity_api(client, plan), _entity_path(plan),
                changes.get("fingerprint"), plan.entity_type.replace("gtm_", ""),
            )
            checks["entity"] = current.get("name")
            checks["fingerprint_unchanged"] = True
    except RuntimeError:
        raise
    except Exception as exc:
        raise _translate_http_error(exc) from exc
    return checks


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a GTM plan. Called by ``confirm_and_apply`` after the gates."""
    _check_gates(config, plan)
    try:
        return _apply(config, plan)
    except RuntimeError:
        raise
    except Exception as exc:
        raise _translate_http_error(exc) from exc


def _apply(config: AdLoopConfig, plan: ChangePlan) -> dict:
    client = _client(config)
    changes = plan.changes
    ws = _workspaces(client)
    op = plan.operation

    if op == "gtm_create_tag":
        resp = ws.tags().create(parent=_ws_path(changes), body=changes["tag"]).execute()
        return {"tag_id": resp.get("tagId"), "name": resp.get("name"),
                "type": resp.get("type"), "workspace_id": changes["workspace_id"],
                "published": False}

    if op == "gtm_create_trigger":
        resp = ws.triggers().create(
            parent=_ws_path(changes), body=changes["trigger"]
        ).execute()
        return {"trigger_id": resp.get("triggerId"), "name": resp.get("name"),
                "type": resp.get("type"), "workspace_id": changes["workspace_id"],
                "published": False}

    if op in ("gtm_update_tag", "gtm_update_trigger"):
        api = _entity_api(client, plan)
        path = _entity_path(plan)
        current = _check_fingerprint(api, path, changes.get("fingerprint"),
                                     plan.entity_type.replace("gtm_", ""))
        # Full read-modify-write: start from the live resource so every field
        # this tool doesn't manage (priority, consentSettings, tagFiringOption,
        # schedule, parentFolderId, monitoringMetadata, …) round-trips intact.
        body = copy.deepcopy(current)
        body.update(copy.deepcopy(changes["patch"]))
        resp = api.update(path=path, body=body,
                          fingerprint=current.get("fingerprint")).execute()
        kind = "tag" if plan.entity_type == "gtm_tag" else "trigger"
        return {f"{kind}_id": resp.get(f"{kind}Id"), "name": resp.get("name"),
                "updated_fields": sorted(changes["patch"]),
                "workspace_id": changes["workspace_id"], "published": False}

    if op in ("gtm_delete_tag", "gtm_delete_trigger"):
        api = _entity_api(client, plan)
        path = _entity_path(plan)
        _check_fingerprint(api, path, changes.get("fingerprint"),
                           plan.entity_type.replace("gtm_", ""))
        api.delete(path=path).execute()
        kind = "tag" if plan.entity_type == "gtm_tag" else "trigger"
        return {f"deleted_{kind}_id": changes[f"{kind}_id"],
                "workspace_id": changes["workspace_id"], "published": False}

    if op == "gtm_publish_workspace":
        _check_workspace_unchanged(client, plan)
        # The live version at the moment of the publish, not at draft time: the
        # rollback target must be what this publish actually replaces, and a
        # failed publish has to say which version could not go live.
        live_before = _live_version(
            client, changes["account_id"], changes["container_id"]
        )
        body = {"name": changes["version_name"]}
        if changes.get("version_notes"):
            body["notes"] = changes["version_notes"]
        created = ws.create_version(path=_ws_path(changes), body=body).execute()
        # compilerError is a boolean on CreateContainerVersionResponse.
        if created.get("compilerError"):
            raise RuntimeError(
                "Tag Manager reported compiler errors for this workspace; "
                "nothing was published. Open the workspace in the GTM UI to "
                "see the errors, fix them, and draft the publish again."
            )
        version = created.get("containerVersion") or {}
        if not version.get("path"):
            raise RuntimeError(
                "Tag Manager created no container version; nothing was published."
            )
        try:
            client.accounts().containers().versions().publish(
                path=version["path"]
            ).execute()
        except Exception as exc:
            # The version exists but is not live. Say which one, so it can be
            # published from the UI instead of drafting the whole change again.
            raise RuntimeError(
                "Tag Manager created version "
                f"{version.get('containerVersionId')} ({version['path']}) but "
                f"publishing it failed: {exc} Nothing went live — the live "
                f"version is still {live_before['version_id']} "
                f"('{live_before['version_name']}'). Publish the created "
                "version from the GTM UI (Versions → Publish) once the cause "
                "is fixed, or draft the publish again."
            ) from exc
        return {
            "published": True,
            "version_id": version.get("containerVersionId"),
            "version_name": version.get("name"),
            "new_workspace_path": created.get("newWorkspacePath"),
            "previous_live_version_id": live_before["version_id"],
            "previous_live_version_name": live_before["version_name"],
        }

    if op == "gtm_rollback_version":
        live_before = _check_live_unchanged(client, plan)
        kwargs = {"path": changes["target_version_path"]}
        if changes.get("target_fingerprint"):
            # versions.publish refuses when the stored version's fingerprint
            # differs, so the version published is the one previewed.
            kwargs["fingerprint"] = changes["target_fingerprint"]
        resp = client.accounts().containers().versions().publish(**kwargs).execute()
        if resp.get("compilerError"):
            raise RuntimeError(
                f"Tag Manager reported compiler errors publishing version "
                f"{changes['target_version_id']}; the live version is still "
                f"{live_before.get('containerVersionId')}."
            )
        version = resp.get("containerVersion") or {}
        return {
            "published": True,
            "rolled_back": True,
            "version_id": version.get("containerVersionId")
            or changes["target_version_id"],
            "version_name": version.get("name") or changes["target_version_name"],
            "previous_live_version_id": live_before.get("containerVersionId"),
            "previous_live_version_name": live_before.get("name"),
            "undo": (
                "Draft a rollback to previous_live_version_id to restore the "
                "state before this rollback."
            ),
        }

    raise ValueError(f"Unknown GTM operation: {op}")
