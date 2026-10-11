"""health_check probes the GA4 Data API, not just the Admin API (issue #60)."""

import pytest

from adloop import runtime, server
from adloop.config import AdLoopConfig, GA4Config


@pytest.fixture
def stub_other_services(monkeypatch):
    """Keep the Ads and Reddit probes out of the way."""
    import adloop.ads.gaql as gaql

    monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [])
    yield
    runtime.set_default_config(None)


def _summaries(*properties):
    return {
        "accounts": [{"properties": [{"property": p} for p in properties]}],
        "total_properties": len(properties),
    }


def _health(monkeypatch, *, config, summaries, probe):
    import adloop.ga4.reports as reports

    runtime.set_default_config(config)
    monkeypatch.setattr(reports, "get_account_summaries", lambda _c: summaries)
    monkeypatch.setattr(reports, "probe_data_api", probe)
    return server.health_check()


def test_data_api_disabled_is_not_ok(monkeypatch, stub_other_services):
    def disabled(_config, _prop):
        raise RuntimeError("403 SERVICE_DISABLED: Google Analytics Data API has not been used")

    status = _health(
        monkeypatch,
        config=AdLoopConfig(ga4=GA4Config(property_id="123")),
        summaries=_summaries("properties/123"),
        probe=disabled,
    )

    assert status["ga4"] == "error"
    assert status["ga4_admin"] == "ok"
    assert status["ga4_data"] == "error"
    assert "SERVICE_DISABLED" in status["ga4_error"]


def test_both_surfaces_ok(monkeypatch, stub_other_services):
    probed = []
    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries("properties/987"),
        probe=lambda _c, prop: probed.append(prop),
    )

    assert (status["ga4"], status["ga4_admin"], status["ga4_data"]) == ("ok", "ok", "ok")
    # No configured property: the first accessible one is probed.
    assert probed == ["properties/987"]


def test_no_property_to_probe(monkeypatch, stub_other_services):
    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries(),
        probe=lambda *_a: pytest.fail("nothing to probe"),
    )

    assert status["ga4"] == "ok"
    assert status["ga4_data"] == "not_checked"


def test_health_check_reports_version_and_offered_tools(stub_other_services):
    # The AI compares tools_offered with its own tool list: a mismatch means
    # the client cached an older list (claude.ai, ChatGPT and Perplexity do).
    result = server.health_check()

    assert result["adloop_version"]
    assert "health_check" in result["tools_offered"]
    assert "run_ga4_report" in result["tools_offered"]
    assert result["tools_offered"] == sorted(result["tools_offered"])
    assert "refresh" in result["tools_note"]


def test_offered_tools_follow_the_runtime_visibility_hook():
    seen = {}

    def only_ga4(tools):
        seen.update(tools)
        return [name for name, tags in tools.items() if tags & {"ga4", "core"}]

    runtime.set_tool_visibility(only_ga4)
    try:
        names = server._offered_tool_names()
    finally:
        runtime.set_tool_visibility(None)

    assert "run_ga4_report" in names and "health_check" in names
    assert "get_campaign_performance" not in names
    # The hook gets every enabled tool with its tags.
    assert seen["get_campaign_performance"] == frozenset({"ads"})


def test_offered_tools_respect_adloop_toolsets(monkeypatch):
    monkeypatch.setattr(server, "_ENABLED_TAGS", {"gsc", "core"})

    names = server._offered_tool_names()

    assert "run_gsc_report" in names and "health_check" in names
    assert "run_ga4_report" not in names


@pytest.mark.asyncio
async def test_server_announces_its_icons_and_website():
    from fastmcp import Client

    async with Client(server.mcp, mode="legacy") as client:
        info = client.initialize_result.server_info

    assert str(info.website_url).startswith("https://getadloop.com")
    assert [icon.src for icon in info.icons] == [
        "https://getadloop.com/icon-512.png",
        "https://getadloop.com/favicon.svg",
    ]
