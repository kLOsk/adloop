"""Tests for Google Ads write planning and mutate helpers."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


class _FakeResult:
    def __init__(self, resource_name: str = ""):
        self.resource_name = resource_name


class _FakeMutateOperationResponse:
    def __init__(self, response_type: str | None = None, resource_name: str = ""):
        self.campaign_budget_result = _FakeResult()
        self.campaign_result = _FakeResult()
        self.ad_group_result = _FakeResult()
        self.campaign_criterion_result = _FakeResult()
        self.asset_result = _FakeResult()
        self.campaign_asset_result = _FakeResult()
        self._response_type = response_type
        if response_type:
            getattr(self, response_type).resource_name = resource_name

    def WhichOneof(self, _: str) -> str | None:
        return self._response_type


class _FakePathService:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def campaign_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/{self.prefix}/{entity_id}"

    def campaign_budget_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/{self.prefix}/{entity_id}"

    def ad_group_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/{self.prefix}/{entity_id}"

    def asset_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/{self.prefix}/{entity_id}"


class _FakeAdGroupService(_FakePathService):
    def __init__(self):
        super().__init__("adGroups")
        self.operations = None

    def mutate_ad_groups(self, customer_id: str, operations: list[object]) -> object:
        self.operations = operations
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=f"customers/{customer_id}/adGroups/1")]
        )


class _FakeGoogleAdsService(_FakePathService):
    def __init__(self, responses: list[_FakeMutateOperationResponse] | None = None):
        super().__init__("campaigns")
        self.operations = None
        self._responses = responses or []

    def mutate(self, customer_id: str, mutate_operations: list[object]) -> object:
        self.operations = mutate_operations
        return SimpleNamespace(mutate_operation_responses=self._responses)

    def search(self, customer_id: str, query: str) -> list[object]:
        raise AssertionError(f"Unexpected search call for customer {customer_id}: {query}")


class _FakeClient:
    def __init__(self, services: dict[str, object]):
        self._base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = self._base.enums
        self.get_type = self._base.get_type
        self._services = services

    def get_service(self, name: str) -> object:
        return self._services[name]


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store._pending_plans.clear()
    yield
    preview_store._pending_plans.clear()


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(require_dry_run=True),
    )


def test_update_ad_group_requires_a_change(config):
    result = write.update_ad_group(
        config,
        customer_id="123-456-7890",
        ad_group_id="2002",
    )

    assert result["error"] == "Validation failed"
    assert "No changes specified" in result["details"][0]


def test_draft_campaign_normalizes_display_expansion_alias(config):
    result = write.draft_campaign(
        config,
        customer_id="123-456-7890",
        campaign_name="Search Launch",
        daily_budget=50,
        bidding_strategy="MANUAL_CPC",
        geo_target_ids=["2840"],
        language_ids=["1000"],
        display_expansion_enabled=True,
        search_partners_enabled=True,
        max_cpc=1.75,
    )

    assert result["changes"]["display_network_enabled"] is True
    assert result["changes"]["search_partners_enabled"] is True
    assert result["changes"]["max_cpc"] == 1.75


def test_draft_campaign_allows_target_spend_cpc_cap(config):
    result = write.draft_campaign(
        config,
        customer_id="123-456-7890",
        campaign_name="Traffic Launch",
        daily_budget=50,
        bidding_strategy="TARGET_SPEND",
        geo_target_ids=["2840"],
        language_ids=["1000"],
        max_cpc=1.75,
    )

    assert result["operation"] == "create_campaign"
    assert result["changes"]["bidding_strategy"] == "TARGET_SPEND"
    assert result["changes"]["max_cpc"] == 1.75


def test_draft_campaign_rejects_conflicting_display_flags(config):
    result = write.draft_campaign(
        config,
        customer_id="123-456-7890",
        campaign_name="Search Launch",
        daily_budget=50,
        bidding_strategy="MANUAL_CPC",
        geo_target_ids=["2840"],
        language_ids=["1000"],
        display_network_enabled=False,
        display_expansion_enabled=True,
    )

    assert result["error"] == "Validation failed"
    assert "must match" in result["details"][0]


def test_update_campaign_normalizes_display_alias(config):
    result = write.update_campaign(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        display_expansion_enabled=True,
        search_partners_enabled=False,
    )

    assert result["changes"]["display_network_enabled"] is True
    assert result["changes"]["search_partners_enabled"] is False


def test_update_campaign_allows_target_spend_cpc_cap(config, monkeypatch):
    monkeypatch.setattr(write, "_campaign_bidding_strategy", lambda *_args: "TARGET_SPEND")

    result = write.update_campaign(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        max_cpc=1.25,
    )

    assert result["changes"]["max_cpc"] == 1.25


def test_update_campaign_rejects_max_cpc_for_non_target_spend(config, monkeypatch):
    monkeypatch.setattr(write, "_campaign_bidding_strategy", lambda *_args: "MANUAL_CPC")

    result = write.update_campaign(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        max_cpc=1.25,
    )

    assert result["error"] == "Validation failed"
    assert "TARGET_SPEND" in result["details"][0]


def test_draft_structured_snippets_rejects_invalid_header(config):
    result = write.draft_structured_snippets(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        snippets=[{"header": "Invalid", "values": ["A", "B", "C"]}],
    )

    assert result["error"] == "Validation failed"
    assert "header must be one of" in result["details"][0]


def test_draft_callouts_returns_preview(config):
    result = write.draft_callouts(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        callouts=["Free Shipping", "24/7 Support"],
    )

    assert result["operation"] == "create_callouts"
    assert result["changes"]["callouts"] == ["Free Shipping", "24/7 Support"]


def test_draft_image_assets_validates_local_png(config, tmp_path):
    image_path = tmp_path / "square.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2ZfZ0AAAAASUVORK5CYII="
        )
    )

    result = write.draft_image_assets(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        image_paths=[str(image_path)],
    )

    assert result["operation"] == "create_image_assets"
    assert result["changes"]["images"][0]["name"].startswith("AdLoop image square ")
    assert result["changes"]["images"][0]["mime_type"] == "image/png"
    assert result["changes"]["images"][0]["width"] == 1
    assert result["changes"]["images"][0]["height"] == 1


def test_draft_image_assets_rejects_missing_file(config):
    result = write.draft_image_assets(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        image_paths=["/tmp/does-not-exist.png"],
    )

    assert result["error"] == "Validation failed"
    assert "does not exist" in result["details"][0]


def test_pause_and_enable_entity_still_support_ad_groups(config):
    pause_result = write.pause_entity(
        config,
        customer_id="123-456-7890",
        entity_type="ad_group",
        entity_id="2002",
    )
    enable_result = write.enable_entity(
        config,
        customer_id="123-456-7890",
        entity_type="ad_group",
        entity_id="2002",
    )

    assert pause_result["changes"]["target_status"] == "PAUSED"
    assert enable_result["changes"]["target_status"] == "ENABLED"


def test_apply_update_ad_group_sets_field_mask():
    ad_group_service = _FakeAdGroupService()
    client = _FakeClient({"AdGroupService": ad_group_service})

    write._apply_update_ad_group(
        client,
        "1234567890",
        {"ad_group_id": "2002", "ad_group_name": "Updated Name", "max_cpc": 1.1},
    )

    operation = ad_group_service.operations[0]
    assert set(operation.update_mask.paths) == {"name", "cpc_bid_micros"}
    assert operation.update.name == "Updated Name"
    assert operation.update.cpc_bid_micros == 1_100_000


def test_apply_create_campaign_sets_network_flags_and_initial_cpc():
    google_ads_service = _FakeGoogleAdsService(
        [
            _FakeMutateOperationResponse(
                "campaign_budget_result",
                "customers/1234567890/campaignBudgets/1",
            ),
            _FakeMutateOperationResponse(
                "campaign_result",
                "customers/1234567890/campaigns/2",
            ),
            _FakeMutateOperationResponse(
                "ad_group_result",
                "customers/1234567890/adGroups/3",
            ),
        ]
    )
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "CampaignService": _FakePathService("campaigns"),
            "CampaignBudgetService": _FakePathService("campaignBudgets"),
            "AdGroupService": _FakePathService("adGroups"),
        }
    )

    write._apply_create_campaign(
        client,
        "1234567890",
        {
            "campaign_name": "Search Launch",
            "daily_budget": 50,
            "bidding_strategy": "MANUAL_CPC",
            "channel_type": "SEARCH",
            "ad_group_name": "Brand Terms",
            "geo_target_ids": [],
            "language_ids": [],
            "search_partners_enabled": True,
            "display_network_enabled": True,
            "max_cpc": 1.75,
        },
    )

    campaign = google_ads_service.operations[1].campaign_operation.create
    ad_group = google_ads_service.operations[2].ad_group_operation.create
    assert campaign.network_settings.target_search_network is True
    assert campaign.network_settings.target_content_network is True
    assert ad_group.cpc_bid_micros == 1_750_000


def test_apply_create_campaign_sets_target_spend_cpc_cap():
    google_ads_service = _FakeGoogleAdsService(
        [
            _FakeMutateOperationResponse(
                "campaign_budget_result",
                "customers/1234567890/campaignBudgets/1",
            ),
            _FakeMutateOperationResponse(
                "campaign_result",
                "customers/1234567890/campaigns/2",
            ),
            _FakeMutateOperationResponse(
                "ad_group_result",
                "customers/1234567890/adGroups/3",
            ),
        ]
    )
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "CampaignService": _FakePathService("campaigns"),
            "CampaignBudgetService": _FakePathService("campaignBudgets"),
            "AdGroupService": _FakePathService("adGroups"),
        }
    )

    write._apply_create_campaign(
        client,
        "1234567890",
        {
            "campaign_name": "Traffic Launch",
            "daily_budget": 50,
            "bidding_strategy": "TARGET_SPEND",
            "channel_type": "SEARCH",
            "ad_group_name": "Traffic Terms",
            "geo_target_ids": [],
            "language_ids": [],
            "max_cpc": 1.4,
        },
    )

    campaign = google_ads_service.operations[1].campaign_operation.create
    ad_group = google_ads_service.operations[2].ad_group_operation.create
    assert campaign.target_spend.cpc_bid_ceiling_micros == 1_400_000
    assert ad_group.cpc_bid_micros == 0


def test_apply_update_campaign_sets_network_field_masks():
    google_ads_service = _FakeGoogleAdsService(
        [_FakeMutateOperationResponse("campaign_result", "customers/1234567890/campaigns/1")]
    )
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "CampaignService": _FakePathService("campaigns"),
        }
    )

    write._apply_update_campaign(
        client,
        "1234567890",
        {
            "campaign_id": "1001",
            "search_partners_enabled": True,
            "display_network_enabled": False,
        },
    )

    operation = google_ads_service.operations[0].campaign_operation
    assert set(operation.update_mask.paths) == {
        "network_settings.target_content_network",
        "network_settings.target_search_network",
    }
    assert operation.update.network_settings.target_search_network is True
    assert operation.update.network_settings.target_content_network is False


def test_apply_update_campaign_sets_target_spend_cpc_cap():
    google_ads_service = _FakeGoogleAdsService(
        [_FakeMutateOperationResponse("campaign_result", "customers/1234567890/campaigns/1")]
    )
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "CampaignService": _FakePathService("campaigns"),
        }
    )

    write._apply_update_campaign(
        client,
        "1234567890",
        {
            "campaign_id": "1001",
            "max_cpc": 1.3,
        },
    )

    operation = google_ads_service.operations[0].campaign_operation
    assert set(operation.update_mask.paths) == {"target_spend.cpc_bid_ceiling_micros"}
    assert operation.update.target_spend.cpc_bid_ceiling_micros == 1_300_000


def test_apply_campaign_asset_variants_create_asset_and_link_operations(tmp_path):
    image_path = tmp_path / "square.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2ZfZ0AAAAASUVORK5CYII="
        )
    )

    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
        _FakeMutateOperationResponse(
            "campaign_asset_result",
            "customers/1234567890/campaignAssets/1001~1~CALLOUT",
        ),
    ]

    google_ads_service = _FakeGoogleAdsService(responses)
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "AssetService": _FakePathService("assets"),
        }
    )

    write._apply_create_callouts(
        client,
        "1234567890",
        {"campaign_id": "1001", "callouts": ["Free Shipping"]},
    )
    callout_link = google_ads_service.operations[1].campaign_asset_operation.create
    assert callout_link.field_type == client.enums.AssetFieldTypeEnum.CALLOUT

    google_ads_service._responses = responses
    write._apply_create_structured_snippets(
        client,
        "1234567890",
        {
            "campaign_id": "1001",
            "snippets": [{"header": "Brands", "values": ["A", "B", "C"]}],
        },
    )
    snippet_link = google_ads_service.operations[1].campaign_asset_operation.create
    assert snippet_link.field_type == client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET

    google_ads_service._responses = responses
    write._apply_create_image_assets(
        client,
        "1234567890",
        {
            "campaign_id": "1001",
            "images": [
                {
                    "path": str(image_path),
                    "name": "AdLoop image square deadbeefcafe",
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                }
            ],
        },
    )
    image_asset = google_ads_service.operations[0].asset_operation.create
    image_link = google_ads_service.operations[1].campaign_asset_operation.create
    assert image_asset.name == "AdLoop image square deadbeefcafe"
    assert image_asset.type_ == client.enums.AssetTypeEnum.IMAGE
    assert image_asset.image_asset.mime_type == client.enums.MimeTypeEnum.IMAGE_PNG
    assert image_link.field_type == client.enums.AssetFieldTypeEnum.AD_IMAGE

    google_ads_service._responses = responses
    write._apply_create_image_assets(
        client,
        "1234567890",
        {
            "campaign_id": "1001",
            "images": [
                {
                    "path": str(image_path),
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                }
            ],
        },
    )
    fallback_image_asset = google_ads_service.operations[0].asset_operation.create
    assert fallback_image_asset.name.startswith("AdLoop image square ")
