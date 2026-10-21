"""Approval cards: a change preview that only a human click applies.

AI apps that support MCP Apps (the ``io.modelcontextprotocol/ui``
extension) render every write tool's preview as AdLoop's approval card,
with "Test run" and "Apply" buttons. The buttons call ``approve_change``,
an app-only tool: the MCP Apps spec requires the app to keep it out of the
model's tool list, AdLoop lists and runs it only for card-capable clients,
and every call needs the plan's approval code. That code travels in the
preview result's ``_meta``, which goes to the card and is not meant for the
model's context, and it is an HMAC the model cannot compute.

While a card can be shown, ``confirm_and_apply(dry_run=false)`` from the
model is refused unless ``safety.allow_chat_approval`` is on, so "the user
said yes" in the chat is not enough to change an account.

Apps without cards that support elicitation (confirmation dialogs) get a
dialog instead: before ``confirm_and_apply(dry_run=false)`` runs, the server
asks the user directly, and a decline changes nothing. On the 2026-07-28
protocol the dialog is an ``InputRequiredResult`` round trip, which works on
a stateless server; on older protocols it is a server-initiated request,
used only self-hosted, where the connection can carry it. Apps with neither
keep the chat flow unchanged.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adloop.safety.preview import ChangePlan

UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
CARD_URI = "ui://adloop/approval-card.html"
CARD_MIME_TYPE = "text/html;profile=mcp-app"
META_KEY = "adloop/approval"
APPROVE_TOOL = "approve_change"

_secret: bytes = secrets.token_bytes(32)
_card_click: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "adloop_card_click", default=False
)


def set_approval_secret(secret: bytes) -> None:
    """Key the approval codes.

    Self-hosted, plans live in memory, so a random key per process is
    enough. A hosted runtime keeps plans across restarts and workers and
    sets a stable key derived from its own secrets.
    """
    global _secret
    if len(secret) < 32:
        raise ValueError("approval secret must be at least 32 bytes")
    _secret = bytes(secret)


def client_shows_cards() -> bool:
    """Whether the calling AI app declared MCP Apps support.

    Read per request: on the 2026-07-28 protocol the capabilities ride every
    request, so this also works on a stateless server. Anything that cannot
    be read counts as "no cards", which keeps the chat flow.
    """
    try:
        from fastmcp.server.dependencies import get_context

        return bool(get_context().client_supports_extension(UI_EXTENSION_ID))
    except Exception:
        return False


def approval_code(plan: ChangePlan) -> str:
    """The code the card must present to run or apply ``plan``."""
    from adloop.runtime import current_tenant

    message = "\x00".join((current_tenant(), plan.plan_id, plan.created_at))
    return hmac.new(_secret, message.encode(), hashlib.sha256).hexdigest()[:32]


def code_matches(plan: ChangePlan, code: str) -> bool:
    return hmac.compare_digest(approval_code(plan), str(code or ""))


UPLOAD_META_KEY = "adloop/upload"
# The upload card's code is good for this long after upload_images opened it.
_UPLOAD_CODE_SECONDS = 2 * 3600


def upload_code(now: float | None = None) -> str:
    """The code an upload card presents with each file: "<expiry>.<signature>".

    Some apps send a card's own calls without declaring MCP Apps (the Claude
    iOS app), so the upload tool cannot rely on that declaration. The code
    goes to the card in the result's _meta, never into the model's context,
    and is bound to the workspace and an expiry.
    """
    import time

    from adloop.runtime import current_tenant

    expiry = int((now if now is not None else time.time()) + _UPLOAD_CODE_SECONDS)
    message = "\x00".join(("upload", current_tenant(), str(expiry)))
    return f"{expiry}.{hmac.new(_secret, message.encode(), hashlib.sha256).hexdigest()[:32]}"


def upload_code_valid(code: str, now: float | None = None) -> bool:
    import time

    from adloop.runtime import current_tenant

    expiry, _, signature = str(code or "").partition(".")
    if not expiry.isdigit() or int(expiry) < (now if now is not None else time.time()):
        return False
    message = "\x00".join(("upload", current_tenant(), expiry))
    expected = hmac.new(_secret, message.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, signature)


def card_click_active() -> bool:
    """True while ``approve_change`` runs a click from the card."""
    return _card_click.get()


@contextmanager
def card_click() -> Iterator[None]:
    token = _card_click.set(True)
    try:
        yield
    finally:
        _card_click.reset(token)


def chat_apply_refusal(plan_id: str) -> dict | None:
    """The refusal for a model-issued real apply, or None when it may run."""
    from adloop.runtime import current_config

    if card_click_active() or not client_shows_cards():
        return None
    if current_config().safety.allow_chat_approval:
        return None
    return {
        "status": "APPROVE_ON_CARD",
        "plan_id": plan_id,
        "message": (
            "Nothing was changed. In this app, changes are applied only by the "
            "user's click on the AdLoop approval card shown with the preview "
            "(Test run, then Apply). Ask the user to approve it there; do not "
            "retry confirm_and_apply with dry_run=false. If the card is not "
            "visible in their app, the workspace owner can allow approval in "
            "chat (AdLoop Cloud: Settings → Safety; self-hosted: "
            "safety.allow_chat_approval in the config)."
        ),
    }


def _instructions(plan_id: str) -> str:
    from adloop.runtime import current_config

    text = (
        "The user sees this preview as an AdLoop approval card with Test run "
        "and Apply buttons, and approves it there. Summarize the change and "
        "any warnings, then let the user decide on the card."
    )
    if current_config().safety.allow_chat_approval:
        return text + (
            " Approval in chat is also allowed: only if the user explicitly "
            f"says so, call confirm_and_apply with plan_id='{plan_id}'."
        )
    return text + (
        " Do not call confirm_and_apply with dry_run=false: in this app it "
        "is refused, only the click applies. A dry run "
        f"(confirm_and_apply plan_id='{plan_id}', dry_run=true) is allowed."
    )


def with_card(result: Any) -> Any:
    """Return a write tool's preview as a card result for card-capable apps.

    Anything that is not a pending preview (errors, refusals, notes) and
    every result for an app without cards passes through unchanged.
    """
    if not isinstance(result, dict) or result.get("status") != "PENDING_CONFIRMATION":
        return result
    if not client_shows_cards():
        return result
    from adloop.safety.preview import get_plan

    plan = get_plan(str(result.get("plan_id") or ""))
    if plan is None:
        return result

    from fastmcp.tools import ToolResult
    from mcp.types import TextContent

    preview = {**result, "approval": "card", "instructions": _instructions(plan.plan_id)}
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(preview, default=str))],
        structured_content=preview,
        meta={
            META_KEY: {
                "plan_id": plan.plan_id,
                "code": approval_code(plan),
                "tool": APPROVE_TOOL,
            }
        },
    )


@lru_cache(maxsize=1)
def card_html() -> str:
    return (
        resources.files("adloop.safety")
        .joinpath("approval_card.html")
        .read_text(encoding="utf-8")
    )


def is_app_only(meta: dict | None) -> bool:
    """True for tools the MCP Apps spec keeps out of the model's tool list."""
    ui = (meta or {}).get("ui")
    if not isinstance(ui, dict):
        return False
    visibility = ui.get("visibility")
    return isinstance(visibility, list) and "model" not in visibility


# ---------------------------------------------------------------------------
# Confirmation dialogs (elicitation) for apps without cards
# ---------------------------------------------------------------------------

DIALOG_KEY = "adloop_confirm"
_MAX_DIALOG_FIELDS = 10


@dataclass
class ApplyConfirmation:
    apply: bool = False


@dataclass
class IrreversibleConfirmation:
    apply: bool = False
    cannot_be_undone: bool = False


def _confirmation_type(plan: ChangePlan) -> type:
    return IrreversibleConfirmation if plan.requires_double_confirm else ApplyConfirmation


def client_can_elicit() -> bool:
    """Whether the calling app declared form elicitation (confirmation dialogs)."""
    try:
        from fastmcp.server.dependencies import get_context

        caps = get_context().session.client_capabilities
    except Exception:
        return False
    elicitation = getattr(caps, "elicitation", None) if caps is not None else None
    if elicitation is None:
        return False
    # An empty capability object means form mode (the spec's default).
    form = getattr(elicitation, "form", None)
    url = getattr(elicitation, "url", None)
    return form is not None or url is None


def _platform_label(plan: ChangePlan) -> str:
    op = plan.operation
    if op.startswith("reddit_"):
        return "Reddit Ads"
    if op.startswith("gtm_"):
        return "Google Tag Manager"
    if "key_event" in op:
        return "Google Analytics"
    return "Google Ads"


def confirmation_message(plan: ChangePlan) -> str:
    """The dialog text: what changes, where, and what confirming does."""
    title = plan.operation.replace("reddit_", "").replace("gtm_", "").replace("_", " ")
    target = " ".join(
        part for part in (plan.entity_type.replace("_", " "), plan.entity_id) if part
    )
    lines = [f"AdLoop: apply this change in {_platform_label(plan)}?", ""]
    lines.append(title.capitalize() + (f" ({target})" if target else ""))
    for key, value in list(plan.changes.items())[:_MAX_DIALOG_FIELDS]:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(f"- {key.replace('_', ' ')}: {text}")
    if len(plan.changes) > _MAX_DIALOG_FIELDS:
        lines.append(f"- ... {len(plan.changes) - _MAX_DIALOG_FIELDS} more fields in the preview")
    lines.append("")
    if plan.requires_double_confirm:
        lines.append("This cannot be undone. Tick both boxes to apply it for real.")
    else:
        lines.append("Tick the box to apply it for real. Declining changes nothing.")
    return "\n".join(lines)


def _accepted(plan: ChangePlan, action: str | None, content: Any) -> bool:
    if action != "accept":
        return False
    if content is None:
        data: dict = {}
    elif isinstance(content, dict):
        data = content
    else:
        data = vars(content)
    if not data.get("apply"):
        return False
    return not plan.requires_double_confirm or bool(data.get("cannot_be_undone"))


def _not_confirmed(plan: ChangePlan) -> dict:
    return {
        "status": "NOT_CONFIRMED",
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "message": (
            "The user did not confirm the change in the confirmation dialog, so "
            "nothing was changed. Do not retry on your own; ask the user what "
            "they want to do. The preview stays pending."
        ),
    }


def _dialog_unanswered(plan: ChangePlan) -> dict:
    return {
        "status": "CONFIRMATION_UNANSWERED",
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "message": (
            "AdLoop asked the user to confirm this change in a dialog of their app, "
            "but the app never returned an answer, so nothing was changed and the "
            "plan stays pending. Ask the user whether a confirmation dialog appeared. "
            "If it did, call confirm_and_apply again and have them answer it. If their "
            "app cannot show dialogs (for example a headless agent or a script), the "
            "workspace owner can allow approval in chat (AdLoop Cloud: Settings -> "
            "Safety -> 'Also allow approval in chat'; self-hosted: "
            "safety.allow_chat_approval: true), or the change can be applied from an "
            "app that shows AdLoop's approval card, such as Claude or ChatGPT. Calling "
            "confirm_and_apply again sends the dialog once more."
        ),
    }


def _mark_dialog(plan: ChangePlan, open_: bool) -> None:
    """Record (or clear) that a dialog for this plan awaits an answer."""
    from datetime import datetime, timezone

    from adloop.safety.preview import store_plan

    plan.dialog_requested_at = datetime.now(timezone.utc).isoformat() if open_ else ""
    store_plan(plan)


def dialog_expected(plan_id: str) -> bool:
    """Whether a real apply of this plan will ask the user in a dialog.

    Used after a successful dry run, so the AI can tell the user to look out
    for it instead of letting an unanswered dialog look like a hang.
    """
    if _dialog_applies(plan_id) is None:
        return False
    try:
        from fastmcp.server.dependencies import get_context

        if get_context()._is_modern_protocol():
            return True
    except Exception:
        return False
    from adloop.runtime import deployment_mode

    # Older protocols get the dialog only self-hosted (see confirm_in_dialog).
    return deployment_mode() != "server"


DIALOG_NOTE = (
    "The real apply (dry_run=false) asks the user to confirm in a dialog shown by "
    "their app; tell them to look out for it and answer it. If no dialog appears, "
    "the call returns CONFIRMATION_UNANSWERED on the next attempt with the ways out."
)


def _dialog_applies(plan_id: str) -> ChangePlan | None:
    """The plan when a real apply must be confirmed in a dialog, else None."""
    from adloop.runtime import current_config
    from adloop.safety.preview import get_plan

    if card_click_active() or client_shows_cards() or not client_can_elicit():
        return None
    safety = current_config().safety
    if safety.allow_chat_approval or safety.require_dry_run:
        # Chat approval is allowed, or the apply turns into a dry run anyway.
        return None
    plan = get_plan(plan_id)
    if plan is None:
        return None
    if safety.two_phase_apply and plan.dry_run_result is None:
        # confirm_and_apply refuses with DRY_RUN_REQUIRED; don't ask first.
        return None
    return plan


async def confirm_in_dialog(context: Any, plan_id: str) -> Any | None:
    """Ask the user before a real apply; None means "go ahead".

    Otherwise returns an ``InputRequiredToolResult`` (2026-07-28: the app
    shows the dialog and repeats the call with the answer) or a
    NOT_CONFIRMED result when the user declined.
    """
    plan = _dialog_applies(plan_id)
    if plan is None:
        return None

    from fastmcp.server.elicitation import parse_elicit_response_type
    from fastmcp.tools import ToolResult

    response_type = _confirmation_type(plan)
    message = confirmation_message(plan)

    if context._is_modern_protocol():
        responses = context.input_responses
        if responses is None:
            if plan.dialog_requested_at:
                # The last dialog for this plan was sent and this is a fresh
                # call, not its answer: the app received the dialog and never
                # returned one. Say so instead of sending another dialog the
                # app will sit on until its timeout. Cleared, so asking again
                # sends a new dialog.
                _mark_dialog(plan, False)
                return ToolResult(structured_content=_dialog_unanswered(plan))
            import mcp_types
            from fastmcp.tools.base import InputRequiredToolResult

            schema = parse_elicit_response_type(response_type).schema
            request = mcp_types.ElicitRequest(
                params=mcp_types.ElicitRequestFormParams(
                    mode="form", message=message, requested_schema=schema
                )
            )
            state = json.dumps({"plan_id": plan.plan_id, "code": approval_code(plan)})
            _mark_dialog(plan, True)
            return InputRequiredToolResult(
                mcp_types.InputRequiredResult(
                    input_requests={DIALOG_KEY: request}, request_state=state
                )
            )
        if plan.dialog_requested_at:
            _mark_dialog(plan, False)  # answered, whatever the answer is
        try:
            state = json.loads(context.request_state or "{}")
        except ValueError:
            state = {}
        if state.get("plan_id") != plan.plan_id or not code_matches(plan, state.get("code", "")):
            return ToolResult(structured_content=_not_confirmed(plan))
        answer = responses.get(DIALOG_KEY) if hasattr(responses, "get") else None
        action = getattr(answer, "action", None)
        content = getattr(answer, "content", None)
    else:
        from adloop.runtime import deployment_mode

        if deployment_mode() == "server":
            # A stateless hosted server cannot push a request to the app on
            # the older protocol; that app keeps the chat flow.
            return None
        answer = await context.elicit(message, response_type)
        action = getattr(answer, "action", None)
        content = getattr(answer, "data", None)

    if _accepted(plan, action, content):
        return None
    return ToolResult(structured_content=_not_confirmed(plan))
