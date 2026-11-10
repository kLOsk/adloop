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


def test_hidden_platforms_say_how_to_set_them_up(monkeypatch, stub_other_services):
    monkeypatch.setattr(server, "_HIDDEN_TAGS", {"reddit"})

    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries(),
        probe=lambda _c, _p: None,
    )

    assert status["reddit"] == "not_configured"
    assert "adloop init" in status["hidden_toolsets"]["reddit"]
    assert "restart" in status["hidden_toolsets_note"]
    assert not [n for n in status["tools_offered"] if "reddit" in n]


def test_hidden_chatgpt_ads_names_the_key_to_add(monkeypatch, stub_other_services):
    monkeypatch.delenv("OPENAI_ADS_API_KEY", raising=False)
    monkeypatch.setattr(server, "_HIDDEN_TAGS", {"chatgpt_ads"})

    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries(),
        probe=lambda _c, _p: None,
    )

    how = status["hidden_toolsets"]["chatgpt_ads"]
    assert "chatgpt_ads.api_key" in how and "OPENAI_ADS_API_KEY" in how
    # chatgpt_attribution_check included: it needs the key as much as the rest.
    assert not [n for n in status["tools_offered"] if "chatgpt" in n]


def test_nothing_hidden_means_no_note(monkeypatch, stub_other_services):
    status = _health(
        monkeypatch,
        config=AdLoopConfig(),
        summaries=_summaries(),
        probe=lambda _c, _p: None,
    )

    assert "hidden_toolsets" not in status


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
    assert seen["get_campaign_performance"] == frozenset({"ads", "ads:read"})


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


def test_hosted_provider_without_an_integration_reports_not_configured(monkeypatch, stub_other_services):
    """Hosted, a provider that does not support an ad platform means the
    platform is not offered, even when shared config names its app."""
    import adloop.ga4.reports as reports
    from adloop import auth
    from adloop.config import MicrosoftAdsConfig

    class OlderProvider:
        pass

    def no_local_files(*_a, **_k):
        raise AssertionError("hosted mode must not read local credential files")

    monkeypatch.setattr("adloop.runtime.deployment_mode", lambda: "server")
    monkeypatch.setattr("adloop.microsoft_ads.auth.local_credentials", no_local_files)
    monkeypatch.setattr(reports, "get_account_summaries", lambda _c: _summaries())
    monkeypatch.setattr(reports, "probe_data_api", lambda _c, _p: None)
    previous = auth.get_credentials_provider()
    auth.set_credentials_provider(OlderProvider())
    runtime.set_default_config(AdLoopConfig(
        microsoft_ads=MicrosoftAdsConfig(developer_token="dev-token", client_id="app-id"),
    ))
    try:
        status = server.health_check()
    finally:
        auth.set_credentials_provider(previous)
    assert status["microsoft_ads"] == "not_configured"


def test_a_host_that_does_not_offer_microsoft_does_not_report_it(monkeypatch, stub_other_services):
    """A hosted runtime hiding the Microsoft tools must not send its users
    to set it up; where the tools are offered, not_configured stays."""
    hide_microsoft = lambda tools: {n: t for n, t in tools.items() if "microsoft_ads" not in t}  # noqa: E731
    runtime.set_tool_visibility(hide_microsoft)
    try:
        status = _health(monkeypatch, config=AdLoopConfig(), summaries=_summaries(), probe=lambda _c, _p: None)
    finally:
        runtime.set_tool_visibility(None)
    assert "microsoft_ads" not in status
    assert "list_microsoft_ads_accounts" not in status["tools_offered"]

    status = _health(monkeypatch, config=AdLoopConfig(), summaries=_summaries(), probe=lambda _c, _p: None)
    assert status["microsoft_ads"] == "not_configured"


def test_a_hosted_workspace_without_microsoft_is_not_probed(monkeypatch):
    """Hosted runtimes may offer Microsoft only to workspaces that connected
    it. The others must not get a Microsoft error in their health check."""
    import adloop.ga4.reports as reports
    from adloop import auth

    class MicrosoftProvider:
        def microsoft_ads_credentials(self, config):
            raise AssertionError("a workspace without Microsoft must not be probed")

    monkeypatch.setattr("adloop.runtime.deployment_mode", lambda: "server")
    monkeypatch.setattr(reports, "get_account_summaries", lambda _c: _summaries())
    monkeypatch.setattr(reports, "probe_data_api", lambda _c, _p: None)
    previous = auth.get_credentials_provider()
    auth.set_credentials_provider(MicrosoftProvider())
    runtime.set_default_config(AdLoopConfig())
    runtime.set_tool_visibility(lambda tools: {n: t for n, t in tools.items() if "microsoft_ads" not in t})
    try:
        status = server.health_check()
    finally:
        runtime.set_tool_visibility(None)
        auth.set_credentials_provider(previous)
    assert "microsoft_ads" not in status


def test_hosted_provider_without_chatgpt_ads_reports_not_configured(monkeypatch, stub_other_services):
    """Hosted, a provider without ChatGPT Ads support means the platform is not
    offered, even when shared config holds a key; no local key is read."""
    import adloop.ga4.reports as reports
    from adloop import auth
    from adloop.config import ChatGPTAdsConfig

    class OlderProvider:
        pass

    def no_local_keys(*_a, **_k):
        raise AssertionError("hosted mode must not read local ChatGPT Ads keys")

    monkeypatch.setattr("adloop.runtime.deployment_mode", lambda: "server")
    monkeypatch.setattr("adloop.chatgpt_ads.client.local_api_key", no_local_keys)
    monkeypatch.setenv("OPENAI_ADS_API_KEY", "sk-ads-from-env")
    monkeypatch.setattr(reports, "get_account_summaries", lambda _c: _summaries())
    monkeypatch.setattr(reports, "probe_data_api", lambda _c, _p: None)
    previous = auth.get_credentials_provider()
    auth.set_credentials_provider(OlderProvider())
    runtime.set_default_config(AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="sk-ads-shared")))
    try:
        status = server.health_check()
    finally:
        auth.set_credentials_provider(previous)
    assert status["chatgpt_ads"] == "not_configured"
