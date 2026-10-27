"""Location options on draft_campaign and update_campaign (#104).

campaign.geo_target_type_setting decides whether the geo targets reach only
people in the locations (PRESENCE) or also people elsewhere who show interest
in them (PRESENCE_OR_INTEREST, Google's default). Empty leaves it as it is.
"""

from __future__ import annotations

import pytest

from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_ads_write import (
    _FakeClient,
    _FakeGoogleAdsService,
    _FakeMutateOperationResponse,
    _FakePathService,
)

CAMPAIGN = "1001"


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="1234567890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=500),
    )


def _current(monkeypatch, *, positive="PRESENCE_OR_INTEREST", negative="PRESENCE_OR_INTEREST",
             found=True):
    queries: list[str] = []

    def execute_query(_config, _cid, query):
        queries.append(query)
        if not found:
            return []
        return [{
            "campaign.geo_target_type_setting.positive_geo_target_type": positive,
            "campaign.geo_target_type_setting.negative_geo_target_type": negative,
        }]

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)
    return queries


def _draft(config, **kwargs):
    return write.draft_campaign(
        config,
        customer_id="1234567890",
        campaign_name="Search",
        daily_budget=20,
        bidding_strategy="TARGET_SPEND",
        geo_target_ids=["2840"],
        language_ids=["1000"],
        **kwargs,
    )


class TestDraftCampaign:
    def test_omitted_leaves_googles_default_and_the_preview_unchanged(self, config):
        result = _draft(config)

        assert "location_option" not in result["changes"]
        assert "negative_location_option" not in result["changes"]
        assert not any("Location option" in w for w in result.get("warnings", []))

    def test_presence_is_planned_without_a_warning(self, config):
        result = _draft(config, location_option="presence", negative_location_option="PRESENCE_OR_INTEREST")

        assert result["changes"]["location_option"] == "PRESENCE"
        assert result["changes"]["negative_location_option"] == "PRESENCE_OR_INTEREST"
        assert not any("widens reach" in w for w in result.get("warnings", []))
        assert result["requires_double_confirm"] is False

    @pytest.mark.parametrize("option", ["PRESENCE_OR_INTEREST", "SEARCH_INTEREST"])
    def test_a_wider_option_warns(self, config, option):
        result = _draft(config, location_option=option)

        assert any(f"Location option {option} widens reach" in w for w in result["warnings"])
        assert result["requires_double_confirm"] is False

    def test_invalid_values_are_refused(self, config):
        result = _draft(config, location_option="EVERYWHERE", negative_location_option="SEARCH_INTEREST")

        assert result["error"] == "Validation failed"
        assert any("location_option must be one of" in d for d in result["details"])
        assert any("negative_location_option must be one of" in d for d in result["details"])


class TestUpdateCampaign:
    def test_preview_shows_old_and_new(self, config, monkeypatch):
        _current(monkeypatch)

        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN,
            location_option="PRESENCE", negative_location_option="PRESENCE_OR_INTEREST",
        )

        assert result["changes"] == {
            "campaign_id": CAMPAIGN,
            "current_location_option": "PRESENCE_OR_INTEREST",
            "location_option": "PRESENCE",
            "current_negative_location_option": "PRESENCE_OR_INTEREST",
            "negative_location_option": "PRESENCE_OR_INTEREST",
        }
        assert result["requires_double_confirm"] is False
        assert "warnings" not in result

    def test_widening_warns_with_a_single_confirmation(self, config, monkeypatch):
        _current(monkeypatch, positive="PRESENCE")

        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN,
            location_option="PRESENCE_OR_INTEREST",
        )

        assert result["changes"]["current_location_option"] == "PRESENCE"
        assert any("widens reach" in w for w in result["warnings"])
        assert result["requires_double_confirm"] is False

    def test_negative_presence_says_it_excludes_fewer_people(self, config, monkeypatch):
        _current(monkeypatch)

        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN,
            negative_location_option="PRESENCE",
        )

        assert "location_option" not in result["changes"]
        assert any("excludes only people in the excluded" in w for w in result["warnings"])

    def test_no_warning_for_a_value_the_campaign_already_has(self, config, monkeypatch):
        _current(monkeypatch, positive="SEARCH_INTEREST", negative="PRESENCE")

        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN,
            location_option="SEARCH_INTEREST", negative_location_option="PRESENCE",
        )

        assert "warnings" not in result

    def test_a_location_option_alone_is_a_change(self, config, monkeypatch):
        queries = _current(monkeypatch)

        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN, location_option="PRESENCE",
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert f"campaign.id = {CAMPAIGN}" in queries[0]

    def test_unknown_campaign(self, config, monkeypatch):
        _current(monkeypatch, found=False)

        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN, location_option="PRESENCE",
        )

        assert result == {"error": f"Campaign {CAMPAIGN} was not found in this account"}

    def test_invalid_value_never_reads_the_account(self, config):
        # The suite-wide no_live_gaql fixture raises if anything is read.
        result = write.update_campaign(
            config, customer_id="1234567890", campaign_id=CAMPAIGN, location_option="NEARBY",
        )

        assert result["error"] == "Validation failed"


def _client(service):
    return _FakeClient({
        "GoogleAdsService": service,
        "CampaignService": _FakePathService("campaigns"),
        "CampaignBudgetService": _FakePathService("campaignBudgets"),
        "AdGroupService": _FakePathService("adGroups"),
    })


class TestApply:
    def test_create_sets_both_options(self):
        service = _FakeGoogleAdsService([
            _FakeMutateOperationResponse("campaign_budget_result", "customers/1/campaignBudgets/1"),
            _FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/2"),
            _FakeMutateOperationResponse("ad_group_result", "customers/1/adGroups/3"),
        ])
        client = _client(service)

        write._apply_create_campaign(client, "1234567890", {
            "campaign_name": "Search",
            "daily_budget": 20,
            "bidding_strategy": "TARGET_SPEND",
            "channel_type": "SEARCH",
            "geo_target_ids": [],
            "language_ids": [],
            "location_option": "PRESENCE",
            "negative_location_option": "PRESENCE",
        })

        setting = service.operations[1].campaign_operation.create.geo_target_type_setting
        assert setting.positive_geo_target_type == client.enums.PositiveGeoTargetTypeEnum.PRESENCE
        assert setting.negative_geo_target_type == client.enums.NegativeGeoTargetTypeEnum.PRESENCE

    def test_create_without_options_leaves_them_unset(self):
        service = _FakeGoogleAdsService()
        client = _client(service)

        write._apply_create_campaign(client, "1234567890", {
            "campaign_name": "Search",
            "daily_budget": 20,
            "bidding_strategy": "TARGET_SPEND",
            "channel_type": "SEARCH",
            "geo_target_ids": [],
            "language_ids": [],
        })

        campaign = service.operations[1].campaign_operation.create
        assert "geo_target_type_setting" not in campaign  # Google applies its default

    def test_update_masks_only_the_option_given(self):
        service = _FakeGoogleAdsService(
            [_FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/1")]
        )
        client = _client(service)

        write._apply_update_campaign(client, "1234567890", {
            "campaign_id": CAMPAIGN,
            "current_location_option": "PRESENCE_OR_INTEREST",
            "location_option": "PRESENCE",
        })

        operation = service.operations[0].campaign_operation
        assert list(operation.update_mask.paths) == [
            "geo_target_type_setting.positive_geo_target_type"
        ]
        assert (
            operation.update.geo_target_type_setting.positive_geo_target_type
            == client.enums.PositiveGeoTargetTypeEnum.PRESENCE
        )


@pytest.mark.asyncio
async def test_both_tools_document_the_parameters():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("draft_campaign", "update_campaign"):
        properties = tools[name].parameters["properties"]
        for param in ("location_option", "negative_location_option"):
            assert properties[param].get("description"), (name, param)
