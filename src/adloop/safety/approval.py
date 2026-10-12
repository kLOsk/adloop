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
said yes" in the chat is not enough to change an account. Clients without
the extension keep the chat flow unchanged.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import secrets
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
