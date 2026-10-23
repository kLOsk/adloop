"""update_campaign on a shared budget or a portfolio bid strategy.

A budget can fund several campaigns and a portfolio strategy can bid for
several: the preview names the others and needs a double confirmation, and
apply refuses when that state moved after the preview.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_assets import _Client

BUDGET = "customers/1/campaignBudgets/55"
PORTFOLIO = "customers/1/biddingStrategies/77"


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="1234567890"),
                        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=500))


def _reads(*, found=True, budget_users=(), portfolio="", portfolio_users=()):
    """GAQL answers for the campaign, its budget's and its portfolio's campaigns."""
    def execute_query(_config, _cid, query):
        if "FROM bidding_strategy" in query:
            return [{"bidding_strategy.name": "Portfolio tCPA"}]
        if "campaign.campaign_budget = " in query:
            return [{"campaign.id": i, "campaign.name": n} for i, n in [("1001", "Brand"), *budget_users]]
        if "campaign.bidding_strategy = " in query:
            return [{"campaign.id": i, "campaign.name": n} for i, n in [("1001", "Brand"), *portfolio_users]]
        if "FROM campaign WHERE campaign.id = 1001" in query:
            return [{"campaign.id": 1001, "campaign.campaign_budget": BUDGET, "campaign.bidding_strategy": portfolio,
                     "campaign_budget.name": "Gemeinsames Budget", "campaign_budget.amount_micros": 20_000_000}] if found else []
        raise AssertionError(f"unexpected query: {query}")
    return patch("adloop.ads.gaql.execute_query", side_effect=execute_query)


def test_a_shared_budget_names_the_other_campaigns_and_needs_a_double_confirmation(config):
    with _reads(budget_users=[("1003", "Generic"), ("1002", "Competitors")]):
        preview = write.update_campaign(config, customer_id="1", campaign_id="1001", daily_budget=30)
    changes = preview["changes"]
    assert preview["requires_double_confirm"] is True
    assert (changes["current_daily_budget"], changes["daily_budget"]) == (20.0, 30)
    assert changes["budget_resource"] == BUDGET and changes["budget_shared_with"] == ["1002", "1003"]
    warning = preview["warnings"][0]
    assert "'Generic' (1003)" in warning and "'Competitors' (1002)" in warning and "ALL of them together" in warning


def test_an_own_budget_shows_the_current_amount_without_extra_confirmation(config):
    with _reads():
        preview = write.update_campaign(config, customer_id="1", campaign_id="1001", daily_budget=30)
    assert preview["requires_double_confirm"] is False and "warnings" not in preview
    assert preview["changes"]["current_daily_budget"] == 20.0 and preview["changes"]["budget_shared_with"] == []


def test_a_bidding_change_on_a_portfolio_campaign_says_it_leaves_the_portfolio(config):
    with _reads(portfolio=PORTFOLIO, portfolio_users=[("1004", "Shop")]):
        preview = write.update_campaign(config, customer_id="1", campaign_id="1001",
                                        bidding_strategy="TARGET_CPA", target_cpa=25)
    assert preview["requires_double_confirm"] is True
    assert preview["changes"]["bidding_strategy_resource"] == PORTFOLIO
    assert "'Portfolio tCPA'" in preview["warnings"][0] and "'Shop'" in preview["warnings"][0]
    assert "budget_resource" not in preview["changes"]  # the budget is not touched

    with _reads():
        standard = write.update_campaign(config, customer_id="1", campaign_id="1001", bidding_strategy="MAXIMIZE_CONVERSIONS")
    assert standard["requires_double_confirm"] is False and standard["changes"]["bidding_strategy_resource"] == ""


def test_unknown_and_non_numeric_campaigns_are_refused(config):
    with _reads(found=False):
        assert "was not found" in write.update_campaign(config, customer_id="1", campaign_id="1001", daily_budget=30)["error"]
    with _reads() as read:
        bad = write.update_campaign(config, customer_id="1", campaign_id="1001 OR 1=1", daily_budget=30)
    assert "must be numeric" in bad["details"][0]
    read.assert_not_called()


# ---------------------------------------------------------------------------
# Apply re-checks the pinned state
# ---------------------------------------------------------------------------


def _client(budget=BUDGET, portfolio="", sharing=("1001", "1002")):
    client = _Client()

    def search(customer_id, query):
        if "campaign.campaign_budget = " in query:
            return iter(SimpleNamespace(campaign=SimpleNamespace(id=int(i))) for i in sharing)
        return iter([SimpleNamespace(campaign=SimpleNamespace(campaign_budget=budget, bidding_strategy=portfolio))])

    client.ads.search = search
    return client


PINNED = {"campaign_id": "1001", "daily_budget": 30, "budget_resource": BUDGET, "budget_shared_with": ["1002"]}


def test_apply_sends_the_budget_change_when_nothing_moved():
    client = _client()
    write._apply_update_campaign(client, "1", PINNED)
    budget = client.ads.operations[0].campaign_budget_operation.update
    assert (budget.resource_name, budget.amount_micros) == (BUDGET, 30_000_000)


@pytest.mark.parametrize("client_kwargs,reason", [
    ({"budget": "customers/1/campaignBudgets/66"}, "uses a different budget"),
    ({"sharing": ("1001", "1002", "1009")}, "joined or left its budget"),
    ({"sharing": ("1001",)}, "joined or left its budget"),
])
def test_apply_refuses_when_the_budget_moved(client_kwargs, reason):
    client = _client(**client_kwargs)
    with pytest.raises(RuntimeError, match=reason):
        write._apply_update_campaign(client, "1", PINNED)
    assert client.ads.operations is None


def test_apply_refuses_when_the_campaign_joined_or_left_a_portfolio():
    plan = {"campaign_id": "1001", "bidding_strategy": "MAXIMIZE_CONVERSIONS", "bidding_strategy_resource": ""}
    joined = _client(portfolio=PORTFOLIO)
    with pytest.raises(RuntimeError, match="portfolio bid strategy changed"):
        write._apply_update_campaign(joined, "1", plan)
    standalone = _client()
    write._apply_update_campaign(standalone, "1", plan)
    assert standalone.ads.operations[0].campaign_operation.update.resource_name.endswith("/1001")


def test_plans_from_before_the_pins_apply_as_they_did():
    client = _client(budget="customers/1/campaignBudgets/99")
    write._apply_update_campaign(client, "1", {"campaign_id": "1001", "daily_budget": 30})
    assert client.ads.operations[0].campaign_budget_operation.update.resource_name == "customers/1/campaignBudgets/99"
