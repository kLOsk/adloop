"""The Performance Max read tools, and the lazy imports the tool wrappers rely on."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from adloop.config import AdLoopConfig


def _lazy_imports() -> list[tuple[str, str, int]]:
    """Every ``from adloop... import name`` inside a function in server.py."""
    server = Path(__file__).resolve().parents[1] / "src" / "adloop" / "server.py"
    tree = ast.parse(server.read_text(encoding="utf-8"))
    found = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("adloop."):
                found.extend((node.module, alias.name, node.lineno) for alias in node.names)
    return sorted(set(found))


def test_every_tool_wrapper_import_resolves():
    """A tool wrapper imports its implementation only when called, so a missing
    function stays invisible until someone calls that tool."""
    missing = []
    for module, name, line in _lazy_imports():
        target = importlib.import_module(module)
        if hasattr(target, name):
            continue
        try:
            importlib.import_module(f"{module}.{name}")
        except ImportError:
            missing.append(f"server.py:{line} {module}.{name}")
    assert not missing, missing


@pytest.fixture
def stub_ads(monkeypatch):
    calls: list[str] = []
    rows: dict[str, list[dict]] = {}

    def execute_query(_config, _customer_id, query):
        calls.append(query)
        return next((value for key, value in rows.items() if key in query), [])

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)
    monkeypatch.setattr("adloop.ads.pmax.get_currency_code", lambda *_a, **_k: "USD")
    return calls, rows


def test_get_pmax_performance_returns_campaigns_and_asset_groups(stub_ads):
    from adloop.ads.pmax import get_pmax_performance

    calls, rows = stub_ads
    rows["FROM campaign"] = [{
        "campaign.id": 1, "campaign.name": "PMax", "segments.ad_network_type": "MIXED",
        "metrics.cost_micros": 5_000_000, "metrics.conversions": 1,
    }]
    rows["FROM asset_group"] = [{
        "asset_group.id": 2, "asset_group.name": "Group", "asset_group.ad_strength": "POOR",
        "metrics.cost_micros": 5_000_000, "metrics.conversions": 0,
    }]
    result = get_pmax_performance(AdLoopConfig(), customer_id="123")
    assert len(calls) == 2
    assert result["campaigns"][0]["metrics.cost"] == 5.0
    assert result["asset_groups"][0]["asset_group.ad_strength"] == "POOR"


def test_get_asset_performance_filters_by_campaign(stub_ads):
    from adloop.ads.pmax import get_asset_performance

    calls, rows = stub_ads
    rows["asset_group_asset"] = [{"asset_group_asset.field_type": "HEADLINE", "asset.id": 9}]
    result = get_asset_performance(AdLoopConfig(), customer_id="123", campaign_id="42")
    assert "42" in calls[0]
    assert "error" not in result


def test_get_detailed_asset_performance_reads_top_combinations(monkeypatch):
    from types import SimpleNamespace

    from adloop.ads import pmax

    service = SimpleNamespace(search=lambda **_kwargs: [])
    client = SimpleNamespace(get_service=lambda _name: service)
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: client)
    result = pmax.get_detailed_asset_performance(AdLoopConfig(), customer_id="123")
    assert result["top_combinations"] == []
    assert result["total_combinations"] == 0
