"""Pause, enable and remove previews name the entity and the account (#103)."""

from unittest.mock import patch

import pytest

from adloop.ads import write
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def _store():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(tmp_path):
    return AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "audit.log")))


def _with_rows(rows, fn):
    sent = []

    def fake(config, customer_id, query):
        sent.append(query)
        return rows

    with patch("adloop.ads.gaql.execute_query", side_effect=fake):
        return fn(), sent


def test_pause_names_the_campaign_and_the_account(tmp_path):
    rows = [{"customer.descriptive_name": "Example GmbH", "customer.id": 1234567890,
             "campaign.name": "Brand - DE", "campaign.status": "ENABLED"}]
    preview, sent = _with_rows(rows, lambda: write.pause_entity(
        _config(tmp_path), customer_id="123-456-7890", entity_type="campaign", entity_id="555"))
    assert "FROM campaign WHERE campaign.id = 555" in sent[0]
    assert preview["changes"] == {"target_status": "PAUSED", "account": "Example GmbH (123-456-7890)",
                                  "entity_name": "Brand - DE", "current_status": "ENABLED"}


def test_keyword_removal_names_keyword_ad_group_and_campaign(tmp_path):
    rows = [{"customer.descriptive_name": "Example GmbH", "customer.id": 1234567890,
             "ad_group_criterion.keyword.text": "nachweis software",
             "ad_group_criterion.keyword.match_type": "PHRASE", "ad_group.name": "Software",
             "campaign.name": "Generic", "ad_group_criterion.status": "ENABLED"}]
    preview, sent = _with_rows(rows, lambda: write.remove_entity(
        _config(tmp_path), entity_type="keyword", entity_id="11~22"))
    assert "WHERE ad_group.id = 11 AND ad_group_criterion.criterion_id = 22" in sent[0]
    assert preview["changes"]["entity_name"] == "nachweis software"
    assert preview["changes"]["ad_group_name"] == "Software"
    assert preview["requires_double_confirm"] is True


def test_unknown_id_is_refused_at_draft_time(tmp_path):
    result, _ = _with_rows([], lambda: write.pause_entity(
        _config(tmp_path), customer_id="1234567890", entity_type="ad_group", entity_id="999"))
    assert result["error"].startswith("No ad group with ID 999 in account 123-456-7890")


def test_already_paused_is_flagged(tmp_path):
    rows = [{"customer.descriptive_name": "X", "customer.id": 1, "campaign.name": "C",
             "campaign.status": "PAUSED"}]
    preview, _ = _with_rows(rows, lambda: write.pause_entity(
        _config(tmp_path), entity_type="campaign", entity_id="5"))
    assert "already PAUSED" in preview["changes"]["warnings"][0]


def test_lookup_failure_falls_back_to_ids(tmp_path):
    # conftest makes every GAQL query raise: the preview keeps working.
    preview = write.enable_entity(_config(tmp_path), entity_type="campaign", entity_id="5")
    assert preview["changes"] == {"target_status": "ENABLED"}


def test_types_without_a_lookup_still_get_the_account_and_odd_ids_are_not_queried(tmp_path):
    rows = [{"customer.descriptive_name": "Example GmbH", "customer.id": 1234567890}]
    preview, sent = _with_rows(rows, lambda: write.remove_entity(
        _config(tmp_path), entity_type="asset", entity_id="77"))
    assert "FROM customer" in sent[0] and preview["changes"]["account"] == "Example GmbH (123-456-7890)"
    preview, sent = _with_rows(rows, lambda: write.remove_entity(
        _config(tmp_path), entity_type="keyword", entity_id="11 OR 1=1"))
    assert "FROM customer" in sent[0] and "entity_name" not in preview["changes"]


def test_ad_ids_in_composite_form_query_the_ad_id():
    with patch("adloop.ads.gaql.execute_query", return_value=[]) as q:
        _names, error = write._describe_target(AdLoopConfig(), "", "ad", "11~22")
    assert "WHERE ad_group_ad.ad.id = 22" in q.call_args.args[2]
    assert "No ad with ID 11~22" in error


def test_removing_an_already_removed_entity_is_flagged(tmp_path):
    rows = [{"customer.descriptive_name": "X", "customer.id": 1, "campaign.name": "Old",
             "campaign.status": "REMOVED"}]
    preview, _ = _with_rows(rows, lambda: write.remove_entity(
        _config(tmp_path), entity_type="campaign", entity_id="5"))
    assert "already REMOVED" in preview["changes"]["warnings"][0]
