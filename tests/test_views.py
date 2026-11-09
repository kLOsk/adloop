"""Views: a read result drawn as a dashboard, on top of the unchanged answer."""

import copy
import json

import pytest
from fastmcp import Client
from mcp.client import advertise

from adloop import runtime, server
from adloop.config import AdLoopConfig, ViewsConfig
from adloop.safety.approval import UI_EXTENSION_ID
from adloop.views import VIEW_NOTE, VIEW_URI, VIEWS, renderer_html

pytestmark = pytest.mark.asyncio
pytest.importorskip("prefab_ui")

CARDS = [advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})]


def _row(cid, name, cost, conversions, clicks=100, impressions=2000, lost=None):
    row = {
        "campaign.id": cid,
        "campaign.name": name,
        "campaign.status": "ENABLED",
        "campaign.advertising_channel_type": "SEARCH",
        "metrics.impressions": impressions,
        "metrics.clicks": clicks,
        "metrics.cost_micros": int(cost * 1_000_000),
        "metrics.conversions": conversions,
        "metrics.ctr": clicks / impressions,
        "metrics.cost": cost,
        "metrics.currency": "EUR",
        "metrics.search_budget_lost_impression_share": lost,
    }
    if conversions:
        row["metrics.cpa"] = round(cost / conversions, 2)
    return row


RESULT = {
    "campaigns": [
        _row("1", "Brand", 300.0, 30.0, lost=0.02),
        _row("2", "Generic", 900.0, 10.0, lost=0.35),
        _row("3", "Competitors", 150.0, 0.0),
    ],
    "total_campaigns": 3,
    "insights": ["1 converting campaign(s) lose at least 20% of Search impression share to budget."],
}


@pytest.fixture
def views_on():
    runtime.set_default_config(AdLoopConfig(views=ViewsConfig(enabled=True)))
    yield
    runtime.set_default_config(None)


@pytest.fixture
def views_off():
    runtime.set_default_config(AdLoopConfig())
    yield
    runtime.set_default_config(None)


@pytest.fixture(autouse=True)
def performance(monkeypatch):
    import adloop.ads.read as read

    monkeypatch.setattr(read, "get_campaign_performance", lambda config, **kw: copy.deepcopy(RESULT))


async def _call(extensions=None):
    async with Client(server.mcp, extensions=extensions) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        result = await client.call_tool(
            "get_campaign_performance", {"customer_id": "1234567890"}, raise_on_error=False
        )
    return tools["get_campaign_performance"], result


def _plain_text() -> str:
    # What the tool answers without views: the dict, converted by FastMCP.
    from fastmcp.tools import ToolResult

    return ToolResult(content=copy.deepcopy(RESULT)).content[0].text


async def test_views_are_off_by_default_and_change_nothing(views_off):
    tool, result = await _call(CARDS)

    assert "ui" not in (tool.meta or {})
    assert result.structured_content == RESULT
    assert result.content[0].text == _plain_text()


async def test_apps_without_mcp_apps_get_the_plain_answer(views_on):
    tool, result = await _call()

    assert tool.meta["ui"]["resourceUri"] == VIEW_URI
    assert result.structured_content == RESULT


async def test_apps_with_mcp_apps_get_the_view_and_the_same_answer(views_on):
    _tool, result = await _call(CARDS)

    # The AI reads the same result, plus a note that the user sees the view.
    assert json.loads(result.content[0].text) == {**json.loads(_plain_text()), "view_note": VIEW_NOTE}
    view = result.structured_content
    assert "$prefab" in view
    # The view shows the tool's own numbers.
    assert view["state"]["totals"]["cost"] == 1350.0
    assert view["state"]["totals"]["conversions"] == 40.0
    assert [bar["name"] for bar in view["state"]["bars"]] == ["Brand", "Generic", "Competitors"]
    assert "Generic" in json.dumps(view)


async def test_an_error_is_never_drawn(views_on, monkeypatch):
    import adloop.ads.read as read

    def boom(config, **kw):
        raise RuntimeError("Google Ads is not set up")

    monkeypatch.setattr(read, "get_campaign_performance", boom)
    _tool, result = await _call(CARDS)

    assert result.structured_content == {"error": "Google Ads is not set up"}


async def test_a_broken_view_costs_only_the_view(views_on, monkeypatch):
    monkeypatch.setitem(VIEWS, "get_campaign_performance", lambda result, kwargs: 1 / 0)
    _tool, result = await _call(CARDS)

    assert result.structured_content == RESULT


async def test_the_view_resource_serves_the_bundled_renderer():
    async with Client(server.mcp) as client:
        resource = (await client.read_resource(VIEW_URI))[0]

    assert resource.mime_type == "text/html;profile=mcp-app"
    assert resource.text == renderer_html()
    # Bundled: nothing is fetched from a CDN when the view opens.
    assert 'src="https://cdn.jsdelivr.net' not in resource.text


async def test_the_view_handles_compact_and_empty_results():
    from adloop.views.performance import campaign_performance_view

    compact = {
        "compact": True,
        "total_campaigns": 40,
        "totals": {"cost": 5000.0, "clicks": 900, "impressions": 30000, "conversions": 50.0, "currency": "EUR", "cpa": 100.0, "ctr_pct": 3.0},
        "campaigns_top_spend": RESULT["campaigns"],
    }
    wire = campaign_performance_view(compact).to_json()
    assert wire["state"]["totals"]["cost"] == 5000.0
    assert wire["state"]["totals"]["ctr"] == pytest.approx(0.03)
    assert "Showing the 3 campaigns with the most spend of 40." in json.dumps(wire)

    empty = json.dumps(campaign_performance_view({"campaigns": [], "total_campaigns": 0}).to_json())
    assert "No campaign had impressions or spend in this period." in empty
    assert "DataTable" not in empty
