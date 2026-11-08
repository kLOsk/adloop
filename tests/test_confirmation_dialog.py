"""Confirmation dialogs: apps without cards ask the user before a real apply."""

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp.client import advertise

from adloop import runtime, server
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety.approval import (
    UI_EXTENSION_ID,
    ApplyConfirmation,
    IrreversibleConfirmation,
    confirmation_message,
)
from adloop.safety.preview import ChangePlan, store_plan

pytestmark = pytest.mark.asyncio

CARDS = [advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})]


@pytest.fixture(autouse=True)
def fake_apply(monkeypatch, tmp_path):
    import adloop.ads.write as write

    applied = []

    def apply(_config, *, plan_id, dry_run):
        applied.append((plan_id, dry_run))
        return {"status": "DRY_RUN_SUCCESS" if dry_run else "APPLIED", "plan_id": plan_id}

    monkeypatch.setattr(write, "confirm_and_apply", apply)
    runtime.set_default_config(_config(tmp_path))
    yield applied
    runtime.set_default_config(None)
    runtime.set_deployment_mode("local")


def _config(tmp_path, **safety) -> AdLoopConfig:
    return AdLoopConfig(
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False, **safety)
    )


def _plan(**kwargs) -> str:
    plan = ChangePlan(
        operation="pause_entity", entity_type="campaign", entity_id="123",
        changes={"target_status": "PAUSED"}, **kwargs,
    )
    store_plan(plan)
    return plan.plan_id


def _answering(answer, asked):
    async def handler(message, response_type, params, ctx):
        asked.append(message)
        return answer

    return handler


async def _call(plan_id, *, answer=None, mode="auto", extensions=None, dry_run=False):
    asked: list[str] = []
    kwargs = {"mode": mode}
    if answer is not None:
        kwargs["elicitation_handler"] = _answering(answer, asked)
    if extensions:
        kwargs["extensions"] = extensions
    async with Client(server.mcp, **kwargs) as client:
        result = await client.call_tool(
            "confirm_and_apply", {"plan_id": plan_id, "dry_run": dry_run}, raise_on_error=False
        )
    return result.structured_content or {}, asked


async def _apply(plan_id, **kwargs):
    result, asked = await _call(plan_id, **kwargs)
    return result.get("status"), asked


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_confirmed_dialog_applies(fake_apply, mode):
    plan_id = _plan()
    status, asked = await _apply(plan_id, answer={"apply": True}, mode=mode)

    assert status == "APPLIED"
    assert asked and asked[0].startswith("AdLoop: pause campaign 123 in Google Ads?")
    assert fake_apply == [(plan_id, False)]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_declined_dialog_changes_nothing(fake_apply, mode):
    result, asked = await _call(_plan(), answer=ElicitResult(action="decline"), mode=mode)

    assert result["status"] == "NOT_CONFIRMED"
    assert result["reason"] == "declined"
    assert asked
    assert fake_apply == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_accept_without_the_tick_says_so(fake_apply, mode):
    """Seen live 2026-11-08: Accept with the box unticked looked like a decline."""
    result, _ = await _call(_plan(), answer={"apply": False}, mode=mode)

    assert result["status"] == "NOT_CONFIRMED"
    assert result["reason"] == "apply_not_ticked"
    assert '"Yes, apply this change"' in result["message"]
    assert fake_apply == []


async def test_irreversible_changes_need_both_ticks(fake_apply):
    result, _ = await _call(_plan(requires_double_confirm=True), answer={"apply": True})
    assert result["status"] == "NOT_CONFIRMED"
    assert result["reason"] == "undo_not_ticked"

    status, asked = await _apply(
        _plan(requires_double_confirm=True), answer={"apply": True, "cannot_be_undone": True}
    )
    assert status == "APPLIED"
    assert "cannot be undone" in asked[0]


async def test_apps_without_dialogs_keep_the_chat_flow(fake_apply):
    status, _ = await _apply(_plan())
    assert status == "APPLIED"


async def test_dry_runs_are_never_asked(fake_apply):
    status, asked = await _apply(_plan(), answer={"apply": False}, dry_run=True)
    assert status == "DRY_RUN_SUCCESS"
    assert asked == []


async def test_card_apps_get_the_card_not_a_dialog(fake_apply):
    status, asked = await _apply(_plan(), answer={"apply": True}, extensions=CARDS)
    assert status == "APPROVE_ON_CARD"
    assert asked == []


async def test_chat_approval_switch_skips_the_dialog(fake_apply, tmp_path):
    runtime.set_default_config(_config(tmp_path, allow_chat_approval=True))
    status, asked = await _apply(_plan(), answer={"apply": False})
    assert status == "APPLIED"
    assert asked == []


async def test_no_dialog_before_the_required_dry_run(fake_apply, tmp_path):
    runtime.set_default_config(_config(tmp_path, two_phase_apply=True))
    _status, asked = await _apply(_plan(), answer={"apply": True})
    assert asked == []


async def test_hosted_servers_skip_the_dialog_on_the_older_protocol(fake_apply):
    runtime.set_deployment_mode("server")
    status, asked = await _apply(_plan(), answer={"apply": False}, mode="legacy")
    assert status == "APPLIED"
    assert asked == []


async def test_stable_keys_still_seal_the_round_trip(fake_apply):
    server.set_request_state_keys([b"k" * 32])
    status, asked = await _apply(_plan(), answer={"apply": True})
    assert status == "APPLIED"
    assert asked


def test_the_first_two_lines_carry_the_change():
    """Claude Code shows two lines and folds the rest ("+16 more lines"), so the
    question, the target, the account and the status change come first."""
    plan = ChangePlan(
        operation="pause_entity", entity_type="campaign", entity_id="23659847975",
        changes={
            "entity_name": "NH - Subunternehmer v2", "account": "NachweisHub (700-552-6737)",
            "current_status": "ENABLED", "target_status": "PAUSED",
        },
    )
    first, second = confirmation_message(plan).splitlines()[:2]
    assert first == 'AdLoop: pause campaign "NH - Subunternehmer v2" in Google Ads?'
    assert second == "Account NachweisHub (700-552-6737) · ENABLED → PAUSED"


def test_a_create_names_the_platform_and_the_key_values():
    plan = ChangePlan(
        operation="reddit_create_campaign", entity_type="campaign", platform="reddit",
        customer_id="a2_x", changes={
            "entity_name": "Spring", "status_on_create": "PAUSED", "objective": "CLICKS",
            "daily_budget": 20, "currency": "EUR",
        },
    )
    first, second = confirmation_message(plan).splitlines()[:2]
    assert first == 'AdLoop: create campaign "Spring" in Reddit Ads?'
    assert second == "Account a2_x · created paused · objective: CLICKS · daily budget: 20"


def test_an_update_shows_before_and_after_in_the_summary():
    """Seen live 2026-11-08: a Reddit rename showed only the account on line two."""
    plan = ChangePlan(
        operation="reddit_update_campaign", entity_type="campaign", platform="reddit",
        customer_id="a2_x", changes={
            "campaign_name": "Spring", "is_campaign_budget_optimization": False,
            "patch": {"name": "Spring sale"},
            "display": {"name": {"from": "Spring", "to": "Spring sale"},
                        "spend_cap": {"from": None, "to": 50}},
        },
    )
    second = confirmation_message(plan).splitlines()[1]
    assert second == "Account a2_x · name: Spring → Spring sale · spend cap: None → 50"


def test_the_message_truncates_long_values_and_counts_the_rest():
    plan = ChangePlan(
        operation="reddit_update_ad_group", entity_type="ad_group", entity_id="9",
        changes={"name": "x" * 400, "note": "y" * 400, **{f"field_{i}": i for i in range(12)}},
    )
    message = confirmation_message(plan)
    assert message.startswith('AdLoop: update ad group "' + "x" * 59 + '…" in Reddit Ads?')
    assert "y" * 159 + "…" in message
    assert "3 more fields in the preview" in message


def test_the_boxes_carry_their_meaning():
    schema = IrreversibleConfirmation.model_json_schema()["properties"]
    assert schema["apply"]["title"] == "Yes, apply this change"
    assert schema["cannot_be_undone"]["title"] == "I understand it cannot be undone"
    assert ApplyConfirmation.model_json_schema()["properties"].keys() == {"apply"}


async def test_an_unanswered_dialog_does_not_hang_the_next_call(fake_apply):
    """An app that declares dialogs, receives one and never answers it (seen
    with a customer's agent, 2026-10-21) used to wait until its own timeout on
    every retry. The next call now says what happened instead."""
    import asyncio

    from adloop.safety.preview import get_plan

    plan_id = _plan()

    async def never(message, response_type, params, ctx):
        await asyncio.sleep(3600)

    async with Client(server.mcp, elicitation_handler=never) as client:
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(
                client.call_tool("confirm_and_apply", {"plan_id": plan_id, "dry_run": False}), 2
            )
        retry = await asyncio.wait_for(
            client.call_tool("confirm_and_apply", {"plan_id": plan_id, "dry_run": False},
                             raise_on_error=False), 5
        )

    assert retry.structured_content["status"] == "CONFIRMATION_UNANSWERED"
    assert "Also allow approval in chat" in retry.structured_content["message"]
    assert fake_apply == []
    plan = get_plan(plan_id)
    assert plan is not None and plan.dialog_requested_at == ""

    # Asking again sends a fresh dialog, and an answer applies the plan.
    status, asked = await _apply(plan_id, answer={"apply": True})
    assert status == "APPLIED" and asked


async def test_a_declined_dialog_is_asked_again_next_time(fake_apply):
    plan_id = _plan()
    status, _ = await _apply(plan_id, answer={"apply": False})
    assert status == "NOT_CONFIRMED"
    status, asked = await _apply(plan_id, answer={"apply": True})
    assert status == "APPLIED" and asked


async def test_the_dry_run_announces_the_dialog_only_where_one_follows(fake_apply):
    plan_id = _plan()
    async with Client(server.mcp, elicitation_handler=_answering({"apply": True}, [])) as client:
        with_dialog = await client.call_tool("confirm_and_apply", {"plan_id": plan_id, "dry_run": True})
    assert "dialog" in with_dialog.structured_content["next_step"]

    async with Client(server.mcp) as client:
        without = await client.call_tool("confirm_and_apply", {"plan_id": plan_id, "dry_run": True})
    assert "next_step" not in without.structured_content

    async with Client(server.mcp, extensions=CARDS, elicitation_handler=_answering({"apply": True}, [])) as client:
        cards = await client.call_tool("confirm_and_apply", {"plan_id": plan_id, "dry_run": True})
    assert "next_step" not in cards.structured_content
