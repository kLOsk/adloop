"""Tests for the GA4 annotation suggested after applied ad changes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig, GA4Config, SafetyConfig
from adloop.ga4.suggest import suggested_annotation
from adloop.safety import preview as preview_store
from adloop.safety.preview import ChangePlan, InMemoryPlanStore, store_plan


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(property_id="properties/123456"):
    return AdLoopConfig(ga4=GA4Config(property_id=property_id), safety=SafetyConfig(require_dry_run=False))


def _plan(operation, entity_type="campaign", entity_id="111", **changes):
    return ChangePlan(plan_id="ab12cd34", operation=operation, entity_type=entity_type,
                      entity_id=entity_id, customer_id="1234567890", changes=changes)


@pytest.mark.parametrize("plan, title", [
    (_plan("pause_entity", entity_name="Brand DE"), "Ads: campaign Brand DE paused"),
    (_plan("enable_entity", entity_name="Brand DE"), "Ads: campaign Brand DE enabled"),
    (_plan("update_campaign", campaign_name="Brand DE", daily_budget=80.0, current_daily_budget=50.0),
     "Ads: Brand DE: budget 50 → 80"),
    (_plan("update_campaign", bidding_strategy="MAXIMIZE_CONVERSIONS", geo_target_ids=["2276"]),
     "Ads: 111: bidding maximize conversions, targeting"),
    (_plan("reddit_set_status", entity_type="ad_group", name="Founders", target_status="PAUSED"),
     "Reddit: ad group Founders paused"),
    (_plan("reddit_update_campaign", campaign_name="Launch",
           display={"budget": {"from": 20, "to": 35}}), "Reddit: Launch: budget 20 → 35"),
])
def test_traffic_changes_get_a_suggestion(plan, title):
    suggestion = suggested_annotation(_config(), plan)
    assert suggestion["title"] == title
    assert suggestion["property_id"] == "properties/123456"
    assert "plan ab12cd34" in suggestion["description"]
    assert "needs the user's approval" in suggestion["next_step"]


@pytest.mark.parametrize("plan", [
    _plan("pause_entity", entity_type="keyword"),
    _plan("update_campaign", tracking_url_template="{lpurl}?x=1"),
    _plan("add_negative_keywords"),
    _plan("ga4_create_annotation"),
    _plan("reddit_update_ad_group", display={"name": {"from": "a", "to": "b"}}),
])
def test_changes_that_do_not_move_traffic_get_none(plan):
    assert suggested_annotation(_config(), plan) is None


def test_no_ga4_property_means_no_suggestion():
    assert suggested_annotation(_config(""), _plan("pause_entity", entity_name="Brand")) is None


def test_long_titles_are_clipped_to_gas_limit():
    plan = _plan("pause_entity", entity_name="A campaign with a really very extremely long name that goes on")
    title = suggested_annotation(_config(), plan)["title"]
    assert len(title) <= 60 and title.endswith("…")


def test_confirm_and_apply_returns_the_suggestion():
    from adloop.ads import write as ads_write

    plan = _plan("pause_entity", entity_name="Brand DE")
    store_plan(plan)
    with patch.object(ads_write, "_execute_plan", return_value={"resource_names": ["x"]}):
        result = ads_write.confirm_and_apply(_config(), plan_id=plan.plan_id, dry_run=False)
    assert result["status"] == "APPLIED"
    assert result["suggested_annotation"]["title"] == "Ads: campaign Brand DE paused"


def test_a_broken_suggestion_never_spoils_the_apply():
    from adloop.ads import write as ads_write

    plan = _plan("pause_entity", entity_name="Brand DE")
    store_plan(plan)
    with (
        patch.object(ads_write, "_execute_plan", return_value={"resource_names": ["x"]}),
        patch("adloop.ga4.suggest.suggested_annotation", side_effect=RuntimeError("boom")),
    ):
        result = ads_write.confirm_and_apply(_config(), plan_id=plan.plan_id, dry_run=False)
    assert result["status"] == "APPLIED"
    assert "suggested_annotation" not in result
