"""Approval cards: only a click on the card applies a change (MCP Apps)."""

import pytest

pytestmark = pytest.mark.asyncio
from fastmcp import Client
from mcp.client import advertise

from adloop import runtime, server
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety.approval import (
    APPROVE_TOOL,
    CARD_URI,
    META_KEY,
    UI_EXTENSION_ID,
    approval_code,
    card_html,
)
from adloop.safety.preview import ChangePlan, get_plan, store_plan

CARDS = [advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})]


@pytest.fixture(autouse=True)
def config(tmp_path):
    runtime.set_default_config(_config(tmp_path))
    yield
    runtime.set_default_config(None)


def _config(tmp_path, **safety) -> AdLoopConfig:
    return AdLoopConfig(
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False, **safety)
    )


def _plan(**kwargs) -> ChangePlan:
    plan = ChangePlan(operation="pause_entity", entity_type="campaign", entity_id="123", **kwargs)
    store_plan(plan)
    return plan


async def _draft(client) -> tuple[str, str, object]:
    result = await client.call_tool(
        "pause_entity",
        {"entity_type": "campaign", "entity_id": "123", "customer_id": "1234567890"},
        raise_on_error=False,
    )
    meta = (result.meta or {}).get(META_KEY) or {}
    return meta.get("plan_id"), meta.get("code"), result


async def test_write_tools_declare_the_card_and_the_resource_serves_it():
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        resource = (await client.read_resource(CARD_URI))[0]

    assert tools["pause_entity"].meta["ui"]["resourceUri"] == CARD_URI
    assert "ui" not in (tools["get_campaign_performance"].meta or {})
    assert "ui" not in (tools["confirm_and_apply"].meta or {})
    assert resource.mimeType == "text/html;profile=mcp-app"
    assert resource.text == card_html()


async def test_apps_without_cards_keep_the_chat_flow():
    async with Client(server.mcp) as client:
        names = [tool.name for tool in await client.list_tools()]
        plan_id, code, result = await _draft(client)
        preview = result.structured_content

        assert APPROVE_TOOL not in names
        assert code is None
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert "approval" not in preview

        applied = await client.call_tool(
            "confirm_and_apply", {"plan_id": preview["plan_id"], "dry_run": False}, raise_on_error=False
        )
        assert applied.structured_content.get("status") != "APPROVE_ON_CARD"

        # Not listed, and without a card's code a call does nothing.
        refused = await client.call_tool(APPROVE_TOOL, {"plan_id": preview["plan_id"], "code": "y"}, raise_on_error=False)
        assert "does not match" in refused.structured_content["error"]


async def test_card_apps_get_the_code_only_in_meta_and_the_model_cannot_apply():
    async with Client(server.mcp, extensions=CARDS) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        plan_id, code, result = await _draft(client)

        assert tools[APPROVE_TOOL].meta["ui"]["visibility"] == ["app"]
        assert result.structured_content["approval"] == "card"
        assert code == approval_code(get_plan(plan_id))
        assert code not in result.content[0].text
        assert code not in str(result.structured_content)

        refused = await client.call_tool(
            "confirm_and_apply", {"plan_id": plan_id, "dry_run": False}, raise_on_error=False
        )
        assert refused.structured_content["status"] == "APPROVE_ON_CARD"
        assert get_plan(plan_id) is not None

        dry_run = await client.call_tool(
            "confirm_and_apply", {"plan_id": plan_id, "dry_run": True}, raise_on_error=False
        )
        assert dry_run.structured_content.get("status") != "APPROVE_ON_CARD"


async def test_chat_approval_switch_lets_the_model_apply_again(tmp_path):
    runtime.set_default_config(_config(tmp_path, allow_chat_approval=True))
    async with Client(server.mcp, extensions=CARDS) as client:
        plan_id, _code, result = await _draft(client)
        assert "Approval in chat is also allowed" in result.structured_content["instructions"]

        applied = await client.call_tool(
            "confirm_and_apply", {"plan_id": plan_id, "dry_run": False}, raise_on_error=False
        )
    assert applied.structured_content.get("status") != "APPROVE_ON_CARD"


async def test_a_card_click_without_the_extension_declaration_still_works():
    """The Claude iOS app sends the card's click without declaring MCP Apps.
    The click carries the card's code, which is what authorises it."""
    async with Client(server.mcp, extensions=CARDS) as card_app:
        plan_id, code, _ = await _draft(card_app)
    async with Client(server.mcp) as same_app_without_declaration:
        tested = await same_app_without_declaration.call_tool(
            APPROVE_TOOL, {"plan_id": plan_id, "code": code, "action": "test"}, raise_on_error=False
        )
    assert not tested.is_error
    assert "does not match" not in str(tested.structured_content)
    assert "only be called" not in str(tested.content)


async def test_the_card_needs_the_matching_code():
    async with Client(server.mcp, extensions=CARDS) as client:
        plan_id, code, _ = await _draft(client)
        wrong = await client.call_tool(
            APPROVE_TOOL, {"plan_id": plan_id, "code": "0" * 32, "action": "apply"}, raise_on_error=False
        )
        other = _plan()
        swapped = await client.call_tool(
            APPROVE_TOOL, {"plan_id": other.plan_id, "code": code, "action": "apply"}, raise_on_error=False
        )

    assert "does not match" in wrong.structured_content["error"]
    assert "does not match" in swapped.structured_content["error"]
    assert get_plan(plan_id) is not None


async def test_the_card_runs_a_click_through_confirm_and_apply(monkeypatch):
    import adloop.ads.write as write

    calls = []

    def fake_apply(_config, *, plan_id, dry_run):
        from adloop.safety.approval import card_click_active

        calls.append((plan_id, dry_run, card_click_active()))
        return {"status": "DRY_RUN_SUCCESS" if dry_run else "APPLIED", "plan_id": plan_id}

    monkeypatch.setattr(write, "confirm_and_apply", fake_apply)
    async with Client(server.mcp, extensions=CARDS) as client:
        plan_id, code, _ = await _draft(client)
        test = await client.call_tool(APPROVE_TOOL, {"plan_id": plan_id, "code": code, "action": "test"})
        apply = await client.call_tool(APPROVE_TOOL, {"plan_id": plan_id, "code": code, "action": "apply"})

    assert test.structured_content["status"] == "DRY_RUN_SUCCESS"
    assert apply.structured_content["status"] == "APPLIED"
    assert calls == [(plan_id, True, True), (plan_id, False, True)]


async def test_irreversible_changes_need_the_tick_on_the_card(monkeypatch):
    import adloop.ads.write as write

    monkeypatch.setattr(write, "confirm_and_apply", lambda *_a, **k: {"status": "APPLIED"})
    plan = _plan(requires_double_confirm=True)
    code = approval_code(plan)
    async with Client(server.mcp, extensions=CARDS) as client:
        unticked = await client.call_tool(
            APPROVE_TOOL, {"plan_id": plan.plan_id, "code": code, "action": "apply"}, raise_on_error=False
        )
        ticked = await client.call_tool(
            APPROVE_TOOL,
            {"plan_id": plan.plan_id, "code": code, "action": "apply", "acknowledged": True},
        )

    assert "cannot be undone" in unticked.structured_content["error"]
    assert ticked.structured_content["status"] == "APPLIED"


async def test_discard_removes_the_plan():
    async with Client(server.mcp, extensions=CARDS) as client:
        plan_id, code, _ = await _draft(client)
        result = await client.call_tool(APPROVE_TOOL, {"plan_id": plan_id, "code": code, "action": "discard"})

    assert result.structured_content.get("status") == "DISCARDED", result.structured_content
    assert get_plan(plan_id) is None


def test_health_check_does_not_offer_the_app_only_tool():
    assert APPROVE_TOOL not in server._offered_tool_names()
    assert "confirm_and_apply" in server._offered_tool_names()


def test_codes_are_bound_to_the_tenant():
    plan = ChangePlan(operation="pause_entity")
    mine = approval_code(plan)
    with runtime.use_runtime(AdLoopConfig(), tenant="someone-else"):
        theirs = approval_code(plan)
    assert mine != theirs
