"""Confirmation dialogs: apps without cards ask the user before a real apply."""

import pytest
from fastmcp import Client
from mcp.client import advertise

from adloop import runtime, server
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety.approval import UI_EXTENSION_ID, confirmation_message
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


async def _apply(plan_id, *, answer=None, mode="auto", extensions=None, dry_run=False):
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
    return (result.structured_content or {}).get("status"), asked


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_confirmed_dialog_applies(fake_apply, mode):
    plan_id = _plan()
    status, asked = await _apply(plan_id, answer={"apply": True}, mode=mode)

    assert status == "APPLIED"
    assert asked and "AdLoop: apply this change in Google Ads?" in asked[0]
    assert fake_apply == [(plan_id, False)]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_declined_dialog_changes_nothing(fake_apply, mode):
    status, asked = await _apply(_plan(), answer={"apply": False}, mode=mode)

    assert status == "NOT_CONFIRMED"
    assert asked
    assert fake_apply == []


async def test_irreversible_changes_need_both_ticks(fake_apply):
    status, _ = await _apply(_plan(requires_double_confirm=True), answer={"apply": True})
    assert status == "NOT_CONFIRMED"

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


def test_the_message_names_the_change_and_truncates_long_values():
    plan = ChangePlan(
        operation="reddit_update_ad_group", entity_type="ad_group", entity_id="9",
        changes={"name": "x" * 400, **{f"field_{i}": i for i in range(12)}},
    )
    message = confirmation_message(plan)
    assert message.startswith("AdLoop: apply this change in Reddit Ads?")
    assert "Update ad group (ad group 9)" in message
    assert "x" * 157 + "..." in message
    assert "3 more fields in the preview" in message


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
