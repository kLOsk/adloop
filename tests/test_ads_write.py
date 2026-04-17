"""Tests for Google Ads write planning and mutate helpers."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads import read, write
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


def test_apply_remove_campaign_asset_preserves_tildes_in_resource_name():
    """The resource name for campaign_asset removal must use ~ separators."""
    google_ads_service = _FakeGoogleAdsService(
        [_FakeMutateOperationResponse("campaign_asset_result",
            "customers/1234567890/campaignAssets/1001~99~SITELINK")]
    )
    client = _FakeClient({"GoogleAdsService": google_ads_service})

    write._apply_remove(client, "1234567890", "campaign_asset", "1001~99~SITELINK")

    op = google_ads_service.operations[0]
    resource_name = op.campaign_asset_operation.remove
    assert resource_name == "customers/1234567890/campaignAssets/1001~99~SITELINK"
    assert "," not in resource_name


def test_apply_remove_customer_asset_preserves_tildes_in_resource_name():
    """The resource name for customer_asset removal must use ~ separators."""

    class _CustomerAssetResponse:
        customer_asset_result = _FakeResult(
            "customers/1234567890/customerAssets/99~SITELINK"
        )

    google_ads_service = _FakeGoogleAdsService()
    original_mutate = google_ads_service.mutate

    def capturing_mutate(customer_id, mutate_operations):
        google_ads_service.operations = mutate_operations
        return SimpleNamespace(
            mutate_operation_responses=[_CustomerAssetResponse()]
        )

    google_ads_service.mutate = capturing_mutate
    client = _FakeClient({"GoogleAdsService": google_ads_service})

    write._apply_remove(client, "1234567890", "customer_asset", "99~SITELINK")

    op = google_ads_service.operations[0]
    resource_name = op.customer_asset_operation.remove
    assert resource_name == "customers/1234567890/customerAssets/99~SITELINK"
    assert "," not in resource_name


def test_apply_remove_shared_criterion_uses_shared_criterion_service():
    """Removing a shared-list keyword must go through SharedCriterionService."""
    captured: dict = {}

    def _mutate(customer_id, operations):
        captured["customer_id"] = customer_id
        captured["operations"] = list(operations)
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/sharedCriteria/555~9001"
                )
            ]
        )

    shared_criterion_service = SimpleNamespace(mutate_shared_criteria=_mutate)
    client = _FakeClient({"SharedCriterionService": shared_criterion_service})

    result = write._apply_remove(client, "1234567890", "shared_criterion", "555~9001")

    assert captured["customer_id"] == "1234567890"
    assert captured["operations"][0].remove == "customers/1234567890/sharedCriteria/555~9001"
    assert result["resource_name"] == "customers/1234567890/sharedCriteria/555~9001"


def test_apply_remove_shared_criterion_rejects_bare_id():
    """shared_criterion entity_id without a ~ separator is a caller error."""
    client = _FakeClient({})
    with pytest.raises(ValueError, match="sharedSetId~criterionId"):
        write._apply_remove(client, "1234567890", "shared_criterion", "9001")


def test_remove_entity_accepts_shared_criterion_type(config):
    """remove_entity should accept shared_criterion as a valid entity_type."""
    result = write.remove_entity(
        config,
        customer_id="123-456-7890",
        entity_type="shared_criterion",
        entity_id="555~9001",
    )
    assert result["entity_type"] == "shared_criterion"
    assert result["entity_id"] == "555~9001"
    assert result["status"] == "PENDING_CONFIRMATION"


def test_remove_entity_normalizes_commas_to_tildes(config):
    """remove_entity should accept commas and normalize to tildes in the stored plan."""
    result = write.remove_entity(
        config,
        customer_id="123-456-7890",
        entity_type="campaign_asset",
        entity_id="1001,99,SITELINK",
    )

    assert result["entity_id"] == "1001~99~SITELINK"


class TestProposeNegativeKeywordList:
    def test_returns_preview_with_correct_operation(self, config):
        result = write.propose_negative_keyword_list(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            list_name="Irrelevant Terms",
            keywords=["free", "cheap diy"],
            match_type="EXACT",
        )
        assert result["operation"] == "create_negative_keyword_list"
        assert result["entity_type"] == "negative_keyword_list"
        assert result["entity_id"] == "1001"
        assert result["changes"]["list_name"] == "Irrelevant Terms"
        assert result["changes"]["keywords"] == ["free", "cheap diy"]
        assert result["changes"]["match_type"] == "EXACT"
        assert result["status"] == "PENDING_CONFIRMATION"
        assert "plan_id" in result

    def test_normalises_match_type_to_uppercase(self, config):
        result = write.propose_negative_keyword_list(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            list_name="My List",
            keywords=["discount"],
            match_type="phrase",
        )
        assert result["changes"]["match_type"] == "PHRASE"

    def test_requires_campaign_id(self, config):
        result = write.propose_negative_keyword_list(
            config,
            list_name="My List",
            keywords=["free"],
        )
        assert result["error"] == "Validation failed"
        assert any("campaign_id" in d for d in result["details"])

    def test_requires_list_name(self, config):
        result = write.propose_negative_keyword_list(
            config,
            campaign_id="1001",
            keywords=["free"],
        )
        assert result["error"] == "Validation failed"
        assert any("list_name" in d for d in result["details"])

    def test_requires_at_least_one_keyword(self, config):
        result = write.propose_negative_keyword_list(
            config,
            campaign_id="1001",
            list_name="My List",
            keywords=[],
        )
        assert result["error"] == "Validation failed"
        assert any("keyword" in d for d in result["details"])

    def test_rejects_invalid_match_type(self, config):
        result = write.propose_negative_keyword_list(
            config,
            campaign_id="1001",
            list_name="My List",
            keywords=["free"],
            match_type="INVALID",
        )
        assert result["error"] == "Validation failed"
        assert any("match_type" in d for d in result["details"])

    def test_plan_is_stored_and_retrievable(self, config):
        result = write.propose_negative_keyword_list(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            list_name="Budget Wasters",
            keywords=["free trial"],
        )
        plan_id = result["plan_id"]
        from adloop.safety import preview as preview_store
        assert plan_id in preview_store._pending_plans


class TestAddToNegativeKeywordList:
    def test_returns_preview_with_correct_operation(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            customer_id="123-456-7890",
            shared_set_id="555",
            keywords=["free trial", "crack"],
            match_type="EXACT",
        )
        assert result["operation"] == "add_to_negative_keyword_list"
        assert result["entity_type"] == "negative_keyword_list"
        assert result["entity_id"] == "555"
        assert result["changes"]["shared_set_id"] == "555"
        assert result["changes"]["keywords"] == ["free trial", "crack"]
        assert result["changes"]["match_type"] == "EXACT"
        assert result["status"] == "PENDING_CONFIRMATION"
        assert "plan_id" in result

    def test_normalises_match_type_to_uppercase(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=["discount"],
            match_type="phrase",
        )
        assert result["changes"]["match_type"] == "PHRASE"

    def test_requires_shared_set_id(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            keywords=["free"],
        )
        assert result["error"] == "Validation failed"
        assert any("shared_set_id" in d for d in result["details"])

    def test_rejects_non_numeric_shared_set_id(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="abc; DROP TABLE",
            keywords=["free"],
        )
        assert result["error"] == "Validation failed"
        assert any("numeric" in d for d in result["details"])

    def test_requires_at_least_one_keyword(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=[],
        )
        assert result["error"] == "Validation failed"
        assert any("keyword" in d.lower() for d in result["details"])

    def test_rejects_invalid_match_type(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=["free"],
            match_type="INVALID",
        )
        assert result["error"] == "Validation failed"
        assert any("match_type" in d for d in result["details"])

    def test_collapses_duplicate_keywords_case_insensitively(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=["Free Trial", "free trial", "FREE TRIAL", "crack"],
        )
        assert result["changes"]["keywords"] == ["Free Trial", "crack"]

    def test_ignores_whitespace_only_keywords(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=["   ", "real term", ""],
        )
        assert result["changes"]["keywords"] == ["real term"]

    def test_all_empty_keywords_rejected(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=["", "   "],
        )
        assert result["error"] == "Validation failed"

    def test_plan_is_stored_and_retrievable(self, config):
        result = write.add_to_negative_keyword_list(
            config,
            shared_set_id="555",
            keywords=["free trial"],
        )
        assert result["plan_id"] in preview_store._pending_plans


class TestApplyAddToNegativeKeywordList:
    """Tests for _apply_add_to_negative_keyword_list — the single-step append mutate."""

    def _make_client(self):
        captured: dict = {}

        def _mutate(customer_id, operations):
            captured["customer_id"] = customer_id
            captured["operations"] = list(operations)
            return SimpleNamespace(
                results=[
                    SimpleNamespace(resource_name=f"customers/{customer_id}/sharedCriteria/555~{i}")
                    for i, _ in enumerate(operations)
                ]
            )

        shared_set_service = SimpleNamespace(
            shared_set_path=lambda cid, ssid: f"customers/{cid}/sharedSets/{ssid}",
        )
        shared_criterion_service = SimpleNamespace(
            mutate_shared_criteria=_mutate,
        )
        services = {
            "SharedSetService": shared_set_service,
            "SharedCriterionService": shared_criterion_service,
        }
        return _FakeClient(services), captured

    def test_returns_resource_names_and_shared_set(self):
        client, captured = self._make_client()
        result = write._apply_add_to_negative_keyword_list(
            client,
            "1234567890",
            {
                "shared_set_id": "555",
                "keywords": ["free", "crack"],
                "match_type": "EXACT",
            },
        )
        assert result["shared_set_resource"] == "customers/1234567890/sharedSets/555"
        assert result["keyword_count"] == 2
        assert len(result["resource_names"]) == 2
        assert captured["customer_id"] == "1234567890"
        assert len(captured["operations"]) == 2

    def test_each_operation_references_shared_set(self):
        client, captured = self._make_client()
        write._apply_add_to_negative_keyword_list(
            client,
            "1234567890",
            {
                "shared_set_id": "555",
                "keywords": ["one"],
                "match_type": "PHRASE",
            },
        )
        op = captured["operations"][0]
        assert op.create.shared_set == "customers/1234567890/sharedSets/555"
        assert op.create.keyword.text == "one"


class TestGetNegativeKeywordLists:
    def test_returns_list_of_shared_sets(self, config, monkeypatch):
        fake_rows = [
            {
                "shared_set.id": "111",
                "shared_set.name": "Brand Exclusions",
                "shared_set.status": "ENABLED",
                "shared_set.member_count": 5,
                "shared_set.resource_name": "customers/123/sharedSets/111",
            }
        ]
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: fake_rows
        )
        result = read.get_negative_keyword_lists(config, customer_id="123-456-7890")
        assert result["total_lists"] == 1
        assert result["negative_keyword_lists"][0]["shared_set.name"] == "Brand Exclusions"

    def test_empty_account_returns_zero(self, config, monkeypatch):
        monkeypatch.setattr("adloop.ads.gaql.execute_query", lambda *_a, **_kw: [])
        result = read.get_negative_keyword_lists(config)
        assert result["total_lists"] == 0
        assert result["negative_keyword_lists"] == []


class TestGetNegativeKeywordListKeywords:
    def test_requires_shared_set_id(self, config):
        result = read.get_negative_keyword_list_keywords(config)
        assert result["error"] == "shared_set_id is required"

    def test_returns_keywords_for_list(self, config, monkeypatch):
        fake_rows = [
            {
                "shared_criterion.keyword.text": "free",
                "shared_criterion.keyword.match_type": "EXACT",
                "shared_set.id": "111",
                "shared_set.name": "Brand Exclusions",
            }
        ]
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: fake_rows
        )
        result = read.get_negative_keyword_list_keywords(
            config, customer_id="123-456-7890", shared_set_id="111"
        )
        assert result["total_keywords"] == 1
        assert result["shared_set_id"] == "111"
        assert result["keywords"][0]["shared_criterion.keyword.text"] == "free"

    def test_emits_resource_id_for_remove_entity(self, config, monkeypatch):
        """Each keyword should carry a resource_id for feeding into remove_entity."""
        fake_rows = [
            {
                "shared_criterion.criterion_id": "9001",
                "shared_criterion.keyword.text": "free",
                "shared_criterion.keyword.match_type": "EXACT",
                "shared_set.id": "111",
                "shared_set.name": "Brand Exclusions",
            }
        ]
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: fake_rows
        )
        result = read.get_negative_keyword_list_keywords(
            config, customer_id="123-456-7890", shared_set_id="111"
        )
        assert result["keywords"][0]["resource_id"] == "111~9001"


class TestGetNegativeKeywordListCampaigns:
    def test_returns_attachments(self, config, monkeypatch):
        fake_rows = [
            {
                "campaign.id": "1001",
                "campaign.name": "Summer Sale",
                "campaign.status": "ENABLED",
                "shared_set.id": "111",
                "shared_set.name": "Brand Exclusions",
            }
        ]
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: fake_rows
        )
        result = read.get_negative_keyword_list_campaigns(
            config, customer_id="123-456-7890", shared_set_id="111"
        )
        assert result["total_attachments"] == 1
        assert result["attachments"][0]["campaign.name"] == "Summer Sale"

    def test_no_shared_set_id_returns_all_attachments(self, config, monkeypatch):
        monkeypatch.setattr("adloop.ads.gaql.execute_query", lambda *_a, **_kw: [])
        result = read.get_negative_keyword_list_campaigns(config)
        assert result["total_attachments"] == 0


class TestApplyCreateNegativeKeywordList:
    """Tests for _apply_create_negative_keyword_list — the 3-step mutate."""

    def _make_client(self, *, fail_on_step: str | None = None):
        """Build a _FakeClient wired for SharedSet, SharedCriterion, CampaignSharedSet."""
        shared_set_service = SimpleNamespace(
            mutate_shared_sets=lambda customer_id, operations: SimpleNamespace(
                results=[SimpleNamespace(resource_name=f"customers/{customer_id}/sharedSets/999")]
            ),
        )
        shared_criterion_service = SimpleNamespace(
            mutate_shared_criteria=lambda customer_id, operations: SimpleNamespace(
                results=[SimpleNamespace(resource_name="criterion/1")]
            ),
        )
        campaign_shared_set_service = SimpleNamespace(
            mutate_campaign_shared_sets=lambda customer_id, operations: SimpleNamespace(
                results=[SimpleNamespace(resource_name=f"customers/{customer_id}/campaignSharedSets/888")]
            ),
        )

        if fail_on_step == "create_shared_set":
            shared_set_service.mutate_shared_sets = lambda **_kw: (_ for _ in ()).throw(
                ValueError("duplicate name")
            )
        elif fail_on_step == "add_keywords":
            shared_criterion_service.mutate_shared_criteria = lambda **_kw: (_ for _ in ()).throw(
                ValueError("quota exceeded")
            )
        elif fail_on_step == "attach_to_campaign":
            campaign_shared_set_service.mutate_campaign_shared_sets = lambda **_kw: (_ for _ in ()).throw(
                ValueError("invalid campaign")
            )

        return _FakeClient({
            "SharedSetService": shared_set_service,
            "SharedCriterionService": shared_criterion_service,
            "CampaignSharedSetService": campaign_shared_set_service,
            "CampaignService": _FakePathService("campaigns"),
        })

    def _changes(self) -> dict:
        return {
            "campaign_id": "1001",
            "list_name": "Brand Exclusions",
            "keywords": ["free", "cheap"],
            "match_type": "EXACT",
        }

    def test_success_returns_all_resources(self):
        client = self._make_client()
        result = write._apply_create_negative_keyword_list(client, "1234567890", self._changes())
        assert result["shared_set_resource"] == "customers/1234567890/sharedSets/999"
        assert result["campaign_shared_set_resource"] == "customers/1234567890/campaignSharedSets/888"
        assert result["keyword_count"] == 2
        assert "partial_failure" not in result

    def test_step1_failure_returns_partial_with_no_resource(self):
        client = self._make_client(fail_on_step="create_shared_set")
        result = write._apply_create_negative_keyword_list(client, "1234567890", self._changes())
        assert result["partial_failure"] is True
        assert result["failed_step"] == "create_shared_set"
        assert result["shared_set_resource"] is None
        assert result["completed_steps"] == []

    def test_step2_failure_returns_partial_with_shared_set_resource(self):
        client = self._make_client(fail_on_step="add_keywords")
        result = write._apply_create_negative_keyword_list(client, "1234567890", self._changes())
        assert result["partial_failure"] is True
        assert result["failed_step"] == "add_keywords"
        assert result["shared_set_resource"] == "customers/1234567890/sharedSets/999"
        assert result["completed_steps"] == ["create_shared_set"]

    def test_step3_failure_returns_partial_with_keyword_count(self):
        client = self._make_client(fail_on_step="attach_to_campaign")
        result = write._apply_create_negative_keyword_list(client, "1234567890", self._changes())
        assert result["partial_failure"] is True
        assert result["failed_step"] == "attach_to_campaign"
        assert result["keyword_count"] == 2
        assert result["completed_steps"] == ["create_shared_set", "add_keywords"]


class TestGetNegativeKeywordListInputValidation:
    """Tests for shared_set_id validation in read tools."""

    def test_non_numeric_shared_set_id_rejected_for_keywords(self, config):
        result = read.get_negative_keyword_list_keywords(
            config, shared_set_id="abc; DROP TABLE"
        )
        assert "error" in result
        assert "numeric" in result["error"]

    def test_non_numeric_shared_set_id_rejected_for_campaigns(self, config):
        result = read.get_negative_keyword_list_campaigns(
            config, shared_set_id="abc"
        )
        assert "error" in result
        assert "numeric" in result["error"]


def test_extract_error_message_handles_plain_exceptions():
    assert write._extract_error_message(ValueError("something broke")) == "something broke"


def test_extract_error_message_handles_empty_str_exceptions():
    """Exceptions with empty str() (like GoogleAdsException) should return repr."""
    class SilentException(Exception):
        def __init__(self):
            self.data = "hidden"
    e = SilentException()
    result = write._extract_error_message(e)
    assert result != ""
