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
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

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


# The tick stays even though the dialog has an Accept button: without a field,
# apps put the cursor on Accept, and one reflexive Enter would apply. The titles
# are what the dialog shows next to each box.
_APPLY_TITLE = "Yes, apply this change"
_UNDO_TITLE = "I understand it cannot be undone"


class ApplyConfirmation(BaseModel):
    apply: bool = Field(False, title=_APPLY_TITLE)


class IrreversibleConfirmation(BaseModel):
    apply: bool = Field(False, title=_APPLY_TITLE)
    cannot_be_undone: bool = Field(False, title=_UNDO_TITLE)


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


_PLATFORM_LABELS = {
    "google": "Google Ads",
    "reddit": "Reddit Ads",
    "gtm": "Google Tag Manager",
    "ga4": "Google Analytics",
    "microsoft": "Microsoft Advertising",
    "chatgpt": "ChatGPT Ads",
}
_OPERATION_PREFIX = re.compile(r"^(?:%s)_" % "|".join(p for p in _PLATFORM_LABELS if p != "google"))
# Where a plan keeps the name of what it changes, most specific first.
_NAME_KEYS = ("entity_name", "name", "campaign_name", "ad_group_name", "list_name",
              "tag_name", "trigger_name", "variable_name")
# Shown in the dialog's first two lines already, or noise in a summary.
_HEADER_KEYS = frozenset({*_NAME_KEYS, "account", "account_name", "customer_id",
                          "current_status", "target_status", "status_on_create"})
_SUMMARY_SKIP = frozenset({"currency", "time_zone", "fingerprint", "warnings"})
_MAX_SUMMARY_FACTS = 3


def _platform_label(plan: ChangePlan) -> str:
    if plan.platform in _PLATFORM_LABELS:
        return _PLATFORM_LABELS[plan.platform]
    op = plan.operation
    for prefix, label in _PLATFORM_LABELS.items():
        if op.startswith(prefix + "_"):
            return label
    if "key_event" in op:
        return "Google Analytics"
    return "Google Ads"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _headline(plan: ChangePlan) -> str:
    """'create campaign "Spring sale"', 'pause campaign 123': the action and its target."""
    action = _OPERATION_PREFIX.sub("", plan.operation).replace("_", " ")
    noun = plan.entity_type.replace("_", " ")
    if noun and action.endswith(" entity"):
        action = action[: -len("entity")] + noun
    name = next(
        (v.strip() for k in _NAME_KEYS
         if isinstance(v := plan.changes.get(k), str) and v.strip()),
        "",
    )
    if name:
        return f'{action} "{_clip(name, 60)}"'
    if plan.entity_id:
        return f"{action} {plan.entity_id}" if action.endswith(noun) else f"{action} ({noun} {plan.entity_id})"
    return action


def _fact(key: str, value: Any) -> str:
    """One short 'key: value' for the summary line, or '' when it isn't one."""
    if isinstance(value, dict) and len(value) == 1:
        value = next(iter(value.values()))
    if isinstance(value, list) and 0 < len(value) <= 3 and all(
        isinstance(v, (str, int, float)) for v in value
    ):
        value = ", ".join(str(v) for v in value)
    if isinstance(value, bool) or value in (None, "", []) or not isinstance(value, (str, int, float)):
        return ""
    text = str(value)
    return f"{key.replace('_', ' ')}: {text}" if len(text) <= 40 else ""


def _summary(plan: ChangePlan) -> str:
    """The dialog's second line: account, status change and the first few facts.

    Apps such as Claude Code show only the first lines of the message and fold
    the rest, so this line has to carry what the person is approving.
    """
    changes = plan.changes
    parts = []
    account = next(
        (a for a in (changes.get("account"), changes.get("account_name"), plan.customer_id)
         if isinstance(a, (str, int)) and not isinstance(a, bool) and str(a).strip()),
        "",
    )
    if account:
        parts.append(f"account {account}")
    current, target = changes.get("current_status"), changes.get("target_status")
    if target:
        parts.append(f"{current} → {target}" if current else f"→ {target}")
    elif created := changes.get("status_on_create"):
        parts.append(f"created {str(created).lower()}")
    facts = 0
    for key, value in changes.items():
        if facts == _MAX_SUMMARY_FACTS:
            break
        if key in _HEADER_KEYS or key in _SUMMARY_SKIP or key.endswith(("_id", "_ids")):
            continue
        if fact := _fact(key, value):
            parts.append(fact)
            facts += 1
    return _clip(" · ".join(parts), 160)


def confirmation_message(plan: ChangePlan) -> str:
    """The dialog text: the question and a one-line summary first, then the details.

    Some apps show only the first two lines until the person expands the
    dialog, so those two lines name the change, the platform, the account and
    the key values on their own.
    """
    lines = [f"AdLoop: {_headline(plan)} in {_platform_label(plan)}?"]
    if summary := _summary(plan):
        lines.append(summary[0].upper() + summary[1:])
    details = [(k, v) for k, v in plan.changes.items() if k not in _HEADER_KEYS]
    if details:
        lines.append("")
    for key, value in details[:_MAX_DIALOG_FIELDS]:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        lines.append(f"- {key.replace('_', ' ')}: {_clip(text, 160)}")
    if (more := len(details) - _MAX_DIALOG_FIELDS) > 0:
        lines.append(f"- … {more} more field{'s' if more > 1 else ''} in the preview")
    lines.append("")
    if plan.requires_double_confirm:
        lines.append(
            f'This cannot be undone. Tick "{_APPLY_TITLE}" and "{_UNDO_TITLE}", then Accept.'
        )
    else:
        lines.append(f'Tick "{_APPLY_TITLE}", then Accept. Decline changes nothing.')
    return "\n".join(lines)


def _refusal(plan: ChangePlan, action: str | None, content: Any) -> str:
    """Why the dialog's answer does not allow the apply; "" when it does."""
    if action != "accept":
        return "declined"
    if content is None:
        data: dict = {}
    elif isinstance(content, dict):
        data = content
    else:
        data = vars(content)
    if not data.get("apply"):
        return "apply_not_ticked"
    if plan.requires_double_confirm and not data.get("cannot_be_undone"):
        return "undo_not_ticked"
    return ""


def _not_confirmed(plan: ChangePlan, reason: str = "declined") -> dict:
    if reason == "apply_not_ticked":
        message = (
            f'The user chose Accept in the confirmation dialog without ticking "{_APPLY_TITLE}", '
            "so nothing was changed. Tell them the box has to be ticked, and ask whether "
            "to try again. The preview stays pending."
        )
    elif reason == "undo_not_ticked":
        message = (
            f'The user chose Accept without ticking "{_UNDO_TITLE}", which this irreversible '
            "change requires, so nothing was changed. Tell them both boxes have to be "
            "ticked, and ask whether to try again. The preview stays pending."
        )
    else:
        message = (
            "The user did not confirm the change in the confirmation dialog, so "
            "nothing was changed. Do not retry on your own; ask the user what "
            "they want to do. The preview stays pending."
        )
    return {
        "status": "NOT_CONFIRMED",
        "reason": reason,
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "message": message,
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

    if not (reason := _refusal(plan, action, content)):
        return None
    return ToolResult(structured_content=_not_confirmed(plan, reason))
