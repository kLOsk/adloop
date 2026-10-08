"""Tests for Google Ads write planning and mutate helpers."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads import assets, read, write
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


class _FakeCampaignCriterionService:
    def __init__(self):
        self.operations = None

    def mutate_campaign_criteria(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/campaignCriteria/{i}"
                )
                for i, _ in enumerate(operations)
            ]
        )


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
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


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


def test_update_ad_group_max_cpc_target_spend_message(config, monkeypatch):
    """TARGET_SPEND (Maximize Clicks) refusal must point at update_campaign.

    Regression for issue #31: the old message said "max_cpc requires an ad
    group in a MANUAL_CPC campaign" which sounded like a tool limitation.
    Maximize Clicks just ignores ad-group bids — the real next step is to
    set the campaign-level CPC ceiling via update_campaign.
    """
    monkeypatch.setattr(
        write,
        "_ad_group_campaign_bidding_strategy",
        lambda *_args: "TARGET_SPEND",
    )

    result = write.update_ad_group(
        config,
        customer_id="123-456-7890",
        ad_group_id="2002",
        max_cpc=1.50,
    )

    assert result["error"] == "Validation failed"
    detail = result["details"][0]
    assert "Maximize Clicks" in detail
    assert "TARGET_SPEND" in detail
    assert "update_campaign" in detail
    assert "No change made" in detail


def test_update_ad_group_max_cpc_automated_strategy_message(config, monkeypatch):
    """Automated bidding strategies should name the strategy in the error."""
    monkeypatch.setattr(
        write,
        "_ad_group_campaign_bidding_strategy",
        lambda *_args: "MAXIMIZE_CONVERSIONS",
    )

    result = write.update_ad_group(
        config,
        customer_id="123-456-7890",
        ad_group_id="2002",
        max_cpc=1.50,
    )

    assert result["error"] == "Validation failed"
    detail = result["details"][0]
    assert "MAXIMIZE_CONVERSIONS" in detail
    assert "no-op" in detail.lower() or "effective_cpc_bid_micros" in detail
    assert "No change made" in detail


def test_update_ad_group_max_cpc_allowed_for_manual_cpc(config, monkeypatch):
    """Manual CPC keeps the existing happy path — bid passes validation."""
    monkeypatch.setattr(
        write,
        "_ad_group_campaign_bidding_strategy",
        lambda *_args: "MANUAL_CPC",
    )
    monkeypatch.setattr(write, "_current_ad_group_cpc_bid", lambda *_args: 1.00)

    result = write.update_ad_group(
        config,
        customer_id="123-456-7890",
        ad_group_id="2002",
        max_cpc=1.50,
    )

    assert result.get("error") is None, result
    assert result["operation"] == "update_ad_group"
    assert result["changes"]["max_cpc"] == 1.50


def test_update_ad_group_refuses_a_bid_raise_beyond_the_cap(config, monkeypatch):
    """max_bid_increase_pct (default 100%) applies to Google Ads bids too."""
    monkeypatch.setattr(
        write, "_ad_group_campaign_bidding_strategy", lambda *_args: "MANUAL_CPC"
    )
    monkeypatch.setattr(write, "_current_ad_group_cpc_bid", lambda *_args: 0.50)

    result = write.update_ad_group(
        config, customer_id="123-456-7890", ad_group_id="2002", max_cpc=1.50
    )

    assert result["error"] == "Validation failed"
    assert "Bid increase 200% exceeds maximum 100%" in result["details"][0]


def test_current_ad_group_cpc_bid_reads_micros(config, monkeypatch):
    monkeypatch.setattr(
        "adloop.ads.gaql.execute_query",
        lambda *_a, **_kw: [{"ad_group.cpc_bid_micros": 1_500_000}],
    )
    assert write._current_ad_group_cpc_bid(config, "1234567890", "2002") == 1.5

    monkeypatch.setattr("adloop.ads.gaql.execute_query", lambda *_a, **_kw: [])
    assert write._current_ad_group_cpc_bid(config, "1234567890", "2002") is None


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
    monkeypatch.setattr(write, "_current_campaign_cpc_ceiling", lambda *_args: 1.00)

    result = write.update_campaign(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        max_cpc=1.25,
    )

    assert result["changes"]["max_cpc"] == 1.25


def test_update_campaign_refuses_a_ceiling_raise_beyond_the_cap(config, monkeypatch):
    monkeypatch.setattr(write, "_campaign_bidding_strategy", lambda *_args: "TARGET_SPEND")
    monkeypatch.setattr(write, "_current_campaign_cpc_ceiling", lambda *_args: 0.40)

    result = write.update_campaign(
        config, customer_id="123-456-7890", campaign_id="1001", max_cpc=1.25
    )

    assert result["error"] == "Validation failed"
    assert any("exceeds maximum 100%" in d for d in result["details"])


def test_update_campaign_first_ceiling_has_no_baseline(config, monkeypatch):
    """Switching to Maximize Clicks sets a ceiling where none existed."""
    monkeypatch.setattr(write, "_campaign_bidding_strategy", lambda *_args: "TARGET_SPEND")
    monkeypatch.setattr(write, "_current_campaign_cpc_ceiling", lambda *_args: None)

    result = write.update_campaign(
        config, customer_id="123-456-7890", campaign_id="1001", max_cpc=5.00
    )

    assert result["changes"]["max_cpc"] == 5.00


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
    result = assets.draft_structured_snippets(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        snippets=[{"header": "Invalid", "values": ["A", "B", "C"]}],
    )

    assert result["error"] == "Validation failed"
    assert "header must be one of" in result["details"][0]


def test_draft_callouts_returns_preview(config):
    result = assets.draft_callouts(
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


def test_apply_update_campaign_geo_replace_excludes_negative_criteria():
    """Regression for issue #32: positive-geo replacement must not touch
    negative location criteria. The GAQL query used to find criteria to
    remove must filter on ``campaign_criterion.negative = FALSE``, otherwise
    negative geo exclusions get swept up and silently deleted.

    Previously, adding a new country to ``geo_target_ids`` would remove an
    unrelated USA exclusion (criterion 2840, negative=True); users only
    noticed when US traffic started showing up in reports.
    """
    captured_queries: list[str] = []
    positive_geo_resource = (
        "customers/1234567890/campaignCriteria/1001~aaa"
    )

    class _FakeSearchableService(_FakeGoogleAdsService):
        def search(self, customer_id: str, query: str):
            captured_queries.append(query)
            return [
                SimpleNamespace(
                    campaign_criterion=SimpleNamespace(
                        resource_name=positive_geo_resource
                    )
                )
            ]

    google_ads_service = _FakeSearchableService(
        [
            _FakeMutateOperationResponse(
                "campaign_criterion_result",
                "customers/1234567890/campaignCriteria/x",
            )
        ]
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
        {"campaign_id": "1001", "geo_target_ids": ["2276"]},
    )

    # The remove-existing query MUST scope on negative=FALSE so existing
    # negative exclusions survive. If this assertion ever flips, issue #32
    # has regressed and users will lose their geo exclusions silently.
    assert len(captured_queries) == 1
    query = captured_queries[0]
    assert "campaign_criterion.negative = FALSE" in query
    assert "campaign_criterion.type = 'LOCATION'" in query

    # One remove op for the existing positive, one create op for the new
    # geo — and crucially, no remove op for any negative criterion. We
    # check the underlying protobuf oneof selector via `_pb.WhichOneof`
    # because proto-plus wrappers don't expose `WhichOneof` directly.
    ops = google_ads_service.operations
    remove_ops = [
        op for op in ops
        if op.campaign_criterion_operation._pb.WhichOneof("operation") == "remove"
    ]
    create_ops = [
        op for op in ops
        if op.campaign_criterion_operation._pb.WhichOneof("operation") == "create"
    ]
    assert len(remove_ops) == 1
    assert remove_ops[0].campaign_criterion_operation.remove == positive_geo_resource
    assert len(create_ops) == 1
    assert (
        create_ops[0].campaign_criterion_operation.create.location.geo_target_constant
        == "geoTargetConstants/2276"
    )


def test_update_campaign_preview_surfaces_preserved_negative_geo_exclusions(
    config, monkeypatch
):
    """Preview must surface preserved negative geo exclusions when
    geo_target_ids is being changed (issue #32).

    Lists the IDs that will survive the replacement so the change is
    explicit in the preview rather than silently happening behind the
    scenes.
    """
    monkeypatch.setattr(
        write,
        "_existing_negative_geo_exclusions",
        lambda *_args: ["2840", "2826"],
    )

    result = write.update_campaign(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        geo_target_ids=["2276"],
    )

    assert "warnings" in result
    assert any("PRESERVED" in w for w in result["warnings"])
    # Surface the preserved set programmatically too, sorted for stability.
    assert result.get("preserved_negative_geo_target_ids") == ["2826", "2840"]


def test_update_campaign_preview_no_preservation_key_when_no_negatives(
    config, monkeypatch
):
    """The preserved-set key only appears when there's something to preserve."""
    monkeypatch.setattr(
        write, "_existing_negative_geo_exclusions", lambda *_args: []
    )

    result = write.update_campaign(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        geo_target_ids=["2276"],
    )

    assert "preserved_negative_geo_target_ids" not in result
    assert not any(
        "PRESERVED" in w for w in result.get("warnings", [])
    )


def test_add_negative_locations_returns_preview_with_deduped_ids(config):
    result = write.add_negative_locations(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        geo_target_ids=["2840", "2840", "1023191"],
    )

    assert result["operation"] == "add_negative_locations"
    assert result["entity_type"] == "negative_location"
    assert result["entity_id"] == "1001"
    assert result["changes"]["geo_target_ids"] == ["2840", "1023191"]
    assert result["status"] == "PENDING_CONFIRMATION"


def test_add_negative_locations_requires_campaign_and_geo_ids(config):
    result = write.add_negative_locations(
        config,
        customer_id="123-456-7890",
    )

    assert result["error"] == "Validation failed"
    assert any("campaign_id" in d for d in result["details"])
    assert any("geo_target_id" in d for d in result["details"])


def test_add_negative_locations_rejects_non_numeric_geo_ids(config):
    result = write.add_negative_locations(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        geo_target_ids=["Berlin"],
    )

    assert result["error"] == "Validation failed"
    assert any("numeric" in d for d in result["details"])


def test_apply_add_negative_locations_builds_negative_criteria():
    criterion_service = _FakeCampaignCriterionService()
    client = _FakeClient(
        {
            "CampaignCriterionService": criterion_service,
            "CampaignService": _FakePathService("campaigns"),
        }
    )

    result = write._apply_add_negative_locations(
        client,
        "1234567890",
        {"campaign_id": "1001", "geo_target_ids": ["2840", "1023191"]},
    )

    assert len(criterion_service.operations) == 2
    for operation, geo_id in zip(
        criterion_service.operations, ["2840", "1023191"]
    ):
        criterion = operation.create
        assert criterion.campaign == "customers/1234567890/campaigns/1001"
        assert criterion.negative is True
        assert (
            criterion.location.geo_target_constant
            == f"geoTargetConstants/{geo_id}"
        )
    assert len(result["resource_names"]) == 2


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

    assets._apply_create_callouts(
        client,
        "1234567890",
        {"campaign_id": "1001", "callouts": ["Free Shipping"]},
    )
    callout_link = google_ads_service.operations[1].campaign_asset_operation.create
    assert callout_link.field_type == client.enums.AssetFieldTypeEnum.CALLOUT

    google_ads_service._responses = responses
    assets._apply_create_structured_snippets(
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


def test_removals_ask_for_double_confirmation(config):
    """Removal is irreversible on Google Ads too, not only on Reddit."""
    from adloop.ads.conversion_actions import draft_remove_conversion_action

    entity = write.remove_entity(
        config, customer_id="123-456-7890", entity_type="campaign", entity_id="42"
    )
    conversion = draft_remove_conversion_action(
        config, customer_id="123-456-7890", conversion_action_id="77"
    )

    assert entity["requires_double_confirm"] is True
    assert conversion["requires_double_confirm"] is True


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
        assert preview_store.get_plan(plan_id) is not None


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
        assert preview_store.get_plan(result["plan_id"]) is not None


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


class TestDraftDemographicTargeting:
    def test_returns_preview_for_age_exclusion_on_ad_group(self, config):
        result = write.draft_demographic_targeting(
            config,
            customer_id="123-456-7890",
            ad_group_id="2002",
            age_ranges=["25-34", "35-44"],
        )
        assert result["operation"] == "add_demographic_criteria"
        assert result["entity_type"] == "ad_group_criterion"
        assert result["entity_id"] == "2002"
        assert result["changes"]["age_ranges"] == [
            "AGE_RANGE_25_34",
            "AGE_RANGE_35_44",
        ]
        assert result["changes"]["negative"] is True
        assert result["status"] == "PENDING_CONFIRMATION"

    def test_accepts_canonical_enum_values(self, config):
        result = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
            genders=["MALE"],
            parental_statuses=["NOT_A_PARENT"],
        )
        assert result["changes"]["genders"] == ["MALE"]
        assert result["changes"]["parental_statuses"] == ["NOT_A_PARENT"]

    def test_accepts_human_readable_aliases(self, config):
        result = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
            genders=["female"],
            parental_statuses=["parent"],
            income_ranges=["top-10"],
        )
        assert result["changes"]["genders"] == ["FEMALE"]
        assert result["changes"]["parental_statuses"] == ["PARENT"]
        assert result["changes"]["income_ranges"] == ["INCOME_RANGE_90_UP"]

    def test_supports_campaign_level(self, config):
        result = write.draft_demographic_targeting(
            config,
            campaign_id="1001",
            age_ranges=["18-24"],
        )
        assert result["entity_type"] == "campaign_criterion"
        assert result["entity_id"] == "1001"
        assert result["changes"]["campaign_id"] == "1001"

    def test_requires_exactly_one_parent(self, config):
        both = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
            campaign_id="1001",
            age_ranges=["25-34"],
        )
        assert both["error"] == "Validation failed"
        assert any(
            "exactly one of ad_group_id or campaign_id" in d
            for d in both["details"]
        )

        neither = write.draft_demographic_targeting(
            config,
            age_ranges=["25-34"],
        )
        assert neither["error"] == "Validation failed"

    def test_requires_at_least_one_dimension_value(self, config):
        result = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
        )
        assert result["error"] == "Validation failed"
        assert any("at least one" in d.lower() for d in result["details"])

    def test_rejects_invalid_value(self, config):
        result = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
            age_ranges=["23-35"],
        )
        assert result["error"] == "Validation failed"
        assert any("23-35" in d for d in result["details"])

    def test_deduplicates_values(self, config):
        result = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
            age_ranges=["25-34", "AGE_RANGE_25_34", "25-34"],
        )
        assert result["changes"]["age_ranges"] == ["AGE_RANGE_25_34"]

    def test_positive_targeting_emits_warning(self, config):
        result = write.draft_demographic_targeting(
            config,
            ad_group_id="2002",
            genders=["female"],
            negative=False,
        )
        assert result["changes"]["negative"] is False
        assert "warnings" in result
        assert any("narrows" in w.lower() for w in result["warnings"])


def test_draft_responsive_search_ad_accepts_json_string_list(config, monkeypatch):
    """Regression: headlines/descriptions accept JSON-encoded list strings.

    Same coercion contract as every other list-accepting MCP tool — covered
    here because draft_responsive_search_ad uses the mixed-element
    _StrOrDictList alias rather than _StrList.
    """
    import asyncio
    import json
    from adloop.server import mcp

    # Bypass URL reachability check so the test doesn't make a network call.
    monkeypatch.setattr(
        "adloop.ads.write._validate_urls",
        lambda urls, timeout=10: ({u: None for u in urls}, {}),
    )

    headlines = [
        "Quality Service Today",
        "Trusted by Thousands",
        "Free Quote in 60 Sec",
    ]
    descriptions = [
        "Get a personalized quote in under a minute. No commitment required.",
        "Top-rated service with a satisfaction guarantee. Try us today.",
    ]

    async def run():
        tool = await mcp.get_tool("draft_responsive_search_ad")
        return await tool.run({
            "customer_id": "123-456-7890",
            "ad_group_id": "2002",
            "headlines": json.dumps(headlines),
            "descriptions": json.dumps(descriptions),
            "final_url": "https://example.com/landing",
        })

    result = asyncio.run(run())
    payload = result.structured_content
    # If the coercion failed, we'd hit a Pydantic list_type error and never
    # reach this point — so a clean preview is the regression check.
    assert payload["operation"] == "create_responsive_search_ad"
    stored_headlines = payload["changes"]["headlines"]
    stored_descriptions = payload["changes"]["descriptions"]
    assert len(stored_headlines) == len(headlines)
    assert len(stored_descriptions) == len(descriptions)
    # Each entry should round-trip the text — internal normalization may wrap
    # strings as {"text": ..., "pinned_field": None}, which is fine.
    for original, stored in zip(headlines, stored_headlines):
        text = stored["text"] if isinstance(stored, dict) else stored
        assert text == original
    for original, stored in zip(descriptions, stored_descriptions):
        text = stored["text"] if isinstance(stored, dict) else stored
        assert text == original


def test_draft_demographic_targeting_accepts_json_string_list(config):
    """The MCP server's _StrListOpt validator must coerce JSON-encoded list strings.

    Some MCP clients/harnesses pre-serialize list params as JSON strings.
    The BeforeValidator decodes them back to a list before Pydantic checks
    the type, so the tool keeps working without client-side workarounds.
    """
    import asyncio
    import json
    from adloop.server import mcp

    async def run():
        tool = await mcp.get_tool("draft_demographic_targeting")
        return await tool.run({
            "customer_id": "123-456-7890",
            "ad_group_id": "2002",
            # JSON-encoded list — what a misbehaving client might send.
            "age_ranges": json.dumps(["25-34", "35-44"]),
        })

    result = asyncio.run(run())
    # ToolResult exposes structured_content for tools that return a dict.
    payload = result.structured_content
    assert payload["operation"] == "add_demographic_criteria"
    assert payload["changes"]["age_ranges"] == ["AGE_RANGE_25_34", "AGE_RANGE_35_44"]


class _FakeAdGroupCriterionService:
    def __init__(self):
        self.operations = None

    def mutate_ad_group_criteria(self, customer_id, operations):
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/adGroupCriteria/2002~{i}"
                )
                for i, _ in enumerate(operations)
            ]
        )


def test_apply_add_demographic_criteria_at_ad_group_level():
    crit_service = _FakeAdGroupCriterionService()
    client = _FakeClient({
        "AdGroupCriterionService": crit_service,
        "AdGroupService": _FakePathService("adGroups"),
    })

    write._apply_add_demographic_criteria(
        client,
        "1234567890",
        {
            "ad_group_id": "2002",
            "campaign_id": "",
            "age_ranges": ["AGE_RANGE_25_34"],
            "genders": ["FEMALE"],
            "parental_statuses": [],
            "income_ranges": [],
            "negative": True,
        },
    )

    assert len(crit_service.operations) == 2
    age_op = crit_service.operations[0].create
    gender_op = crit_service.operations[1].create
    assert age_op.negative is True
    assert age_op.ad_group == "customers/1234567890/adGroups/2002"
    # AgeRangeTypeEnum.AGE_RANGE_25_34 → 503000
    assert age_op.age_range.type_ == client.enums.AgeRangeTypeEnum.AGE_RANGE_25_34
    assert gender_op.gender.type_ == client.enums.GenderTypeEnum.FEMALE


def test_apply_add_demographic_criteria_at_campaign_level():
    crit_service = _FakeCampaignCriterionService()
    client = _FakeClient({
        "CampaignCriterionService": crit_service,
        "CampaignService": _FakePathService("campaigns"),
    })

    write._apply_add_demographic_criteria(
        client,
        "1234567890",
        {
            "ad_group_id": "",
            "campaign_id": "1001",
            "age_ranges": [],
            "genders": [],
            "parental_statuses": [],
            "income_ranges": ["INCOME_RANGE_90_UP"],
            "negative": True,
        },
    )

    assert len(crit_service.operations) == 1
    op = crit_service.operations[0].create
    assert op.campaign == "customers/1234567890/campaigns/1001"
    assert op.negative is True
    assert op.income_range.type_ == client.enums.IncomeRangeTypeEnum.INCOME_RANGE_90_UP


def test_draft_demographic_targeting_warns_on_undetermined_exclusion(config):
    """Excluding UNDETERMINED silently drops every unclassifiable user —
    often the single largest 'segment'. The preview must call it out."""
    result = write.draft_demographic_targeting(
        config,
        customer_id="123-456-7890",
        ad_group_id="2002",
        genders=["undetermined"],
    )

    assert result["operation"] == "add_demographic_criteria"
    assert any("UNDETERMINED" in w for w in result["warnings"])


def test_draft_demographic_targeting_rejects_campaign_level_positive(config):
    """The API only supports demographic EXCLUSIONS at campaign level."""
    result = write.draft_demographic_targeting(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        genders=["female"],
        negative=False,
    )

    assert result["error"] == "Validation failed"
    assert any("ad_group_id instead" in d for d in result["details"])


def test_get_demographic_targeting_rejects_non_numeric_ids(config):
    from adloop.ads import read

    result = read.get_demographic_targeting(
        config,
        customer_id="123-456-7890",
        ad_group_id="2002 OR campaign.id > 0",
    )

    assert "numeric" in result["error"]


def test_remove_entity_accepts_ad_group_criterion_alias(config):
    """Demographic criteria should be removable via the semantic alias."""
    result = write.remove_entity(
        config,
        customer_id="123-456-7890",
        entity_type="ad_group_criterion",
        entity_id="2002~99",
    )
    assert result["operation"] == "remove_entity"
    assert result["entity_type"] == "ad_group_criterion"
    assert result["entity_id"] == "2002~99"


def test_remove_entity_accepts_campaign_criterion_alias(config):
    result = write.remove_entity(
        config,
        customer_id="123-456-7890",
        entity_type="campaign_criterion",
        entity_id="1001~99",
    )
    assert result["entity_type"] == "campaign_criterion"


class TestGetDemographicTargeting:
    def test_requires_one_parent(self, config):
        no_parent = read.get_demographic_targeting(config)
        assert "error" in no_parent
        both = read.get_demographic_targeting(
            config, ad_group_id="2002", campaign_id="1001"
        )
        assert "error" in both

    def test_enriches_rows_with_remove_id(self, config, monkeypatch):
        fake_rows = [
            {
                "ad_group_criterion.criterion_id": "99",
                "ad_group_criterion.type": "AGE_RANGE",
                "ad_group_criterion.negative": True,
                "ad_group_criterion.status": "ENABLED",
                "ad_group_criterion.age_range.type": "AGE_RANGE_25_34",
                "ad_group.id": "2002",
                "ad_group.name": "Brand Terms",
                "campaign.id": "1001",
                "campaign.name": "Search Launch",
            }
        ]
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: fake_rows
        )
        result = read.get_demographic_targeting(
            config, customer_id="123-456-7890", ad_group_id="2002"
        )
        assert result["level"] == "ad_group"
        assert result["total_criteria"] == 1
        assert result["criteria"][0]["remove_id"] == "2002~99"
        assert any("exclusion" in i.lower() for i in result["insights"])

    def test_empty_emits_default_insight(self, config, monkeypatch):
        monkeypatch.setattr("adloop.ads.gaql.execute_query", lambda *_a, **_kw: [])
        result = read.get_demographic_targeting(
            config, ad_group_id="2002"
        )
        assert result["total_criteria"] == 0
        assert any("no demographic criteria" in i.lower() for i in result["insights"])


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


class TestListAccounts:
    """Regression tests for the list_accounts limit / truncation behaviour.

    The default cap was raised from 50 (v0.6.3) to 200 in v0.6.5 because
    the original 50 was triggering "I can't see all my accounts" on
    perfectly normal agency MCCs (50-150 clients). The cap is kept to
    protect very large MCCs from per-response size caps on MCP hosts —
    for those, the caller raises the limit explicitly.
    """

    @staticmethod
    def _make_config(login_customer_id: str = "999-999-9999") -> AdLoopConfig:
        return AdLoopConfig(
            ads=AdsConfig(
                customer_id="123-456-7890",
                login_customer_id=login_customer_id,
            ),
            safety=SafetyConfig(require_dry_run=True),
        )

    @staticmethod
    def _fake_accounts(n: int) -> list[dict]:
        return [
            {
                "customer_client.id": str(1000 + i),
                "customer_client.descriptive_name": f"Account {i}",
                "customer_client.status": "ENABLED",
                "customer_client.manager": False,
            }
            for i in range(n)
        ]

    def test_returns_full_list_when_under_default(self, monkeypatch):
        rows = self._fake_accounts(120)
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: rows
        )

        result = read.list_accounts(self._make_config())

        assert result["total_accounts"] == 120
        assert "truncated" not in result
        assert "note" not in result

    def test_truncates_above_default_with_actionable_note(self, monkeypatch):
        # 201 rows mimics what the LIMIT {limit + 1} probe would return
        # when the underlying MCC actually has more than 200 accounts.
        rows = self._fake_accounts(201)
        monkeypatch.setattr(
            "adloop.ads.gaql.execute_query", lambda *_a, **_kw: rows
        )

        result = read.list_accounts(self._make_config())

        assert result["total_accounts"] == 200
        assert result["truncated"] is True
        assert "list_accounts(limit=1000)" in result["note"]
        assert "all of their accounts" in result["note"]

    def test_explicit_limit_overrides_default(self, monkeypatch):
        captured: dict = {}

        def fake_execute(_config, _customer_id, query, *_a, **_kw):
            captured["query"] = query
            return self._fake_accounts(500)

        monkeypatch.setattr("adloop.ads.gaql.execute_query", fake_execute)

        result = read.list_accounts(self._make_config(), limit=1000)

        # When caller asks for 1000 and we got back 500, no truncation.
        assert result["total_accounts"] == 500
        assert "truncated" not in result
        # The probe should request limit + 1 to detect truncation cleanly.
        assert "LIMIT 1001" in captured["query"]


class TestConfirmAndApplyDryRunOverride:
    """Regression tests for GitHub issue #19.

    When safety.require_dry_run is true, calling confirm_and_apply with
    dry_run=false must tell the caller EXACTLY why the override happened
    and how to unlock real writes, so agents (Claude Code, Cursor, etc.)
    do not get stuck in retry loops.
    """

    def _stage_plan(self) -> str:
        plan = preview_store.ChangePlan(
            operation="add_keywords",
            entity_type="keyword",
            customer_id="123-456-7890",
            changes={"ad_group_id": "2002", "keywords": []},
        )
        preview_store.store_plan(plan)
        return plan.plan_id

    def test_forced_dry_run_includes_remediation_fields(self, tmp_path):
        config = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=True,
                log_file=str(tmp_path / "audit.log"),
            ),
            source_path=str(tmp_path / "config.yaml"),
        )
        plan_id = self._stage_plan()

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["dry_run_forced_by"] == "config.safety.require_dry_run"
        assert result["config_path"] == str(tmp_path / "config.yaml")
        assert "require_dry_run: false" in result["remediation"]
        assert "restart" in result["remediation"].lower()
        assert "IGNORED" in result["message"]
        assert "restart" in result["message"].lower()

    def test_forced_dry_run_on_a_hosted_server_points_at_the_dashboard(self, tmp_path):
        """Hosted workspaces have no config file and no server to restart:
        live changes are a dashboard setting."""
        from adloop import runtime

        config = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=True,
                log_file=str(tmp_path / "audit.log"),
            ),
        )
        plan_id = self._stage_plan()

        runtime.set_deployment_mode("server")
        try:
            result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)
        finally:
            runtime.set_deployment_mode("local")

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["dry_run_forced_by"] == "config.safety.require_dry_run"
        assert "config_path" not in result
        assert "Settings → Safety" in result["remediation"]
        for text in (result["remediation"], result["message"]):
            assert "config.yaml" not in text
            assert "restart" not in text.lower()
        assert "IGNORED" in result["message"]

    def test_requested_dry_run_does_not_mention_config_override(self, tmp_path):
        """When the caller intentionally asked for a dry run, the response
        should NOT mislead them by claiming a config override happened."""
        config = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False,
                log_file=str(tmp_path / "audit.log"),
            ),
            source_path=str(tmp_path / "config.yaml"),
        )
        plan_id = self._stage_plan()

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert "dry_run_forced_by" not in result
        assert "config_path" not in result
        assert "remediation" not in result

    def test_forced_dry_run_falls_back_when_source_path_missing(self, tmp_path):
        """Defensive default: if load_config didn't capture a path (legacy
        callers constructing AdLoopConfig directly), we still surface a
        path-shaped hint rather than an empty string."""
        config = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=True,
                log_file=str(tmp_path / "audit.log"),
            ),
            source_path="",
        )
        plan_id = self._stage_plan()

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["config_path"] == "~/.adloop/config.yaml"
        assert "~/.adloop/config.yaml" in result["remediation"]

    def test_forced_dry_run_is_audit_logged(self, tmp_path):
        """The audit log must still reflect that a dry run happened."""
        log_path = tmp_path / "audit.log"
        config = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(require_dry_run=True, log_file=str(log_path)),
            source_path=str(tmp_path / "config.yaml"),
        )
        plan_id = self._stage_plan()

        write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        contents = log_path.read_text()
        assert "dry_run_success" in contents


class TestTwoPhaseApply:
    """safety.two_phase_apply enforces the order: one dry-run pass per
    plan before dry_run=false is accepted. This makes the preview→confirm
    flow a protocol requirement the calling agent cannot skip (the hosted
    runtime turns it on for all tenants)."""

    def _stage_plan(self) -> str:
        plan = preview_store.ChangePlan(
            operation="add_keywords",
            entity_type="keyword",
            customer_id="123-456-7890",
            changes={"ad_group_id": "2002", "keywords": []},
        )
        preview_store.store_plan(plan)
        return plan.plan_id

    def _config(self, tmp_path, *, two_phase: bool = True) -> AdLoopConfig:
        return AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=False,
                two_phase_apply=two_phase,
                log_file=str(tmp_path / "audit.log"),
            ),
            source_path=str(tmp_path / "config.yaml"),
        )

    def test_apply_without_dry_run_pass_is_refused(self, tmp_path):
        config = self._config(tmp_path)
        plan_id = self._stage_plan()

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["status"] == "DRY_RUN_REQUIRED"
        assert result["plan_id"] == plan_id
        assert "dry_run=true" in result["message"]
        assert "dry_run=false" in result["message"]
        # The plan must survive the refusal so the retry flow works.
        assert preview_store.get_plan(plan_id) is not None

    def test_dry_run_pass_is_persisted_on_the_plan(self, tmp_path):
        config = self._config(tmp_path)
        plan_id = self._stage_plan()

        write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)

        stored = preview_store.get_plan(plan_id)
        assert stored is not None
        assert stored.dry_run_result is not None
        assert stored.dry_run_result["status"] == "DRY_RUN_SUCCESS"
        assert stored.dry_run_result["at"]

    def test_apply_succeeds_after_dry_run_pass(self, tmp_path, monkeypatch):
        config = self._config(tmp_path)
        plan_id = self._stage_plan()
        monkeypatch.setattr(
            write, "_execute_plan", lambda *_: {"resource_name": "ok"}
        )

        write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)
        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["status"] == "APPLIED"
        assert preview_store.get_plan(plan_id) is None

    def test_flag_off_keeps_direct_apply_working(self, tmp_path, monkeypatch):
        """Backward compatibility: without the flag, dry_run=false straight
        after the draft keeps working as before."""
        config = self._config(tmp_path, two_phase=False)
        plan_id = self._stage_plan()
        monkeypatch.setattr(
            write, "_execute_plan", lambda *_: {"resource_name": "ok"}
        )

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["status"] == "APPLIED"

    def test_refusal_is_audit_logged(self, tmp_path):
        config = self._config(tmp_path)
        plan_id = self._stage_plan()

        write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        contents = (tmp_path / "audit.log").read_text()
        assert "refused_two_phase" in contents

    def test_require_dry_run_override_wins_over_two_phase(self, tmp_path):
        """With require_dry_run on, the forced-dry-run contract (incl. its
        remediation fields) must be returned — not DRY_RUN_REQUIRED."""
        config = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(
                require_dry_run=True,
                two_phase_apply=True,
                log_file=str(tmp_path / "audit.log"),
            ),
            source_path=str(tmp_path / "config.yaml"),
        )
        plan_id = self._stage_plan()

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["dry_run_forced_by"] == "config.safety.require_dry_run"


# ---------------------------------------------------------------------------
# RSA pinning — _normalize_rsa_assets + _validate_rsa
# ---------------------------------------------------------------------------


class TestNormalizeRsaAssets:
    def test_accepts_plain_strings(self):
        out = write._normalize_rsa_assets(["a", "b", "c"])
        assert out == [
            {"text": "a", "pinned_field": None},
            {"text": "b", "pinned_field": None},
            {"text": "c", "pinned_field": None},
        ]

    def test_accepts_dicts(self):
        out = write._normalize_rsa_assets(
            [
                {"text": "Brand", "pinned_field": "HEADLINE_1"},
                {"text": "Phone", "pinned_field": "HEADLINE_2"},
            ]
        )
        assert out[0] == {"text": "Brand", "pinned_field": "HEADLINE_1"}
        assert out[1] == {"text": "Phone", "pinned_field": "HEADLINE_2"}

    def test_accepts_mixed(self):
        out = write._normalize_rsa_assets(
            [
                {"text": "Brand", "pinned_field": "HEADLINE_1"},
                "Free shipping",
                "Same-day quotes",
            ]
        )
        assert out[0]["pinned_field"] == "HEADLINE_1"
        assert out[1] == {"text": "Free shipping", "pinned_field": None}
        assert out[2] == {"text": "Same-day quotes", "pinned_field": None}

    def test_dict_without_pin_defaults_to_none(self):
        out = write._normalize_rsa_assets([{"text": "Hello"}])
        assert out == [{"text": "Hello", "pinned_field": None}]

    def test_rejects_non_str_non_dict(self):
        with pytest.raises(ValueError, match="must be str or dict"):
            write._normalize_rsa_assets([123])


class TestValidateRsaPinning:
    @staticmethod
    def _hs(*items):
        return write._normalize_rsa_assets(list(items))

    @staticmethod
    def _ds(*items):
        return write._normalize_rsa_assets(list(items))

    def test_canonical_3_pin_pattern_passes(self):
        errs = write._validate_rsa(
            "123",
            self._hs(
                {"text": "SandBagsToGo", "pinned_field": "HEADLINE_1"},
                {"text": "866-550-2247", "pinned_field": "HEADLINE_2"},
                "Free shipping",
                "Same-day quotes",
            ),
            self._ds(
                {"text": "Trusted by FEMA & Caltrans", "pinned_field": "DESCRIPTION_1"},
                "Quick quotes, same-day shipping, USA-wide.",
            ),
            "https://sandbagstogo.com/",
        )
        assert errs == []

    def test_rejects_invalid_headline_pin(self):
        errs = write._validate_rsa(
            "123",
            self._hs({"text": "Brand", "pinned_field": "HEADLINE_99"}, "x", "y"),
            self._ds("desc one", "desc two"),
            "https://example.com/",
        )
        assert any("HEADLINE_99" in e for e in errs)

    def test_rejects_invalid_description_pin(self):
        errs = write._validate_rsa(
            "123",
            self._hs("a", "b", "c"),
            self._ds(
                {"text": "d1", "pinned_field": "DESCRIPTION_5"}, "d2"
            ),
            "https://example.com/",
        )
        assert any("DESCRIPTION_5" in e for e in errs)

    def test_rejects_three_headlines_pinned_to_same_slot(self):
        errs = write._validate_rsa(
            "123",
            self._hs(
                {"text": "Brand A", "pinned_field": "HEADLINE_1"},
                {"text": "Brand B", "pinned_field": "HEADLINE_1"},
                {"text": "Brand C", "pinned_field": "HEADLINE_1"},
            ),
            self._ds("d1", "d2"),
            "https://example.com/",
        )
        assert any("At most 2 headlines may pin to HEADLINE_1" in e for e in errs)

    def test_rejects_two_descriptions_pinned_to_same_slot(self):
        errs = write._validate_rsa(
            "123",
            self._hs("a", "b", "c"),
            self._ds(
                {"text": "d1", "pinned_field": "DESCRIPTION_1"},
                {"text": "d2", "pinned_field": "DESCRIPTION_1"},
            ),
            "https://example.com/",
        )
        assert any(
            "At most 1 description may pin to DESCRIPTION_1" in e for e in errs
        )

    def test_two_headlines_pinned_to_same_slot_is_allowed(self):
        # Google permits up to 2 per pin slot
        errs = write._validate_rsa(
            "123",
            self._hs(
                {"text": "Brand A", "pinned_field": "HEADLINE_1"},
                {"text": "Brand B", "pinned_field": "HEADLINE_1"},
                "x",
            ),
            self._ds("d1", "d2"),
            "https://example.com/",
        )
        assert errs == []

    def test_length_check_still_enforced_on_dict_entries(self):
        errs = write._validate_rsa(
            "123",
            self._hs({"text": "a" * 31, "pinned_field": "HEADLINE_1"}, "b", "c"),
            self._ds("d1", "d2"),
            "https://example.com/",
        )
        assert any("exceeds 30 chars" in e for e in errs)

    def test_description_length_check_still_enforced_on_dict_entries(self):
        errs = write._validate_rsa(
            "123",
            self._hs("a", "b", "c"),
            self._ds({"text": "d" * 91, "pinned_field": "DESCRIPTION_1"}, "d2"),
            "https://example.com/",
        )
        assert any("exceeds 90 chars" in e for e in errs)


# ---------------------------------------------------------------------------
# attach_shared_set_to_campaigns / detach_shared_set_from_campaigns
# ---------------------------------------------------------------------------


class TestAttachSharedSetValidation:
    def test_rejects_missing_shared_set_id(self, config):
        result = write.attach_shared_set_to_campaigns(
            config, customer_id="123-456-7890", campaign_ids=["1001"]
        )
        assert result["error"] == "Validation failed"
        assert "shared_set_id is required" in result["details"]

    def test_rejects_non_numeric_shared_set_id(self, config):
        result = write.attach_shared_set_to_campaigns(
            config,
            customer_id="123-456-7890",
            shared_set_id="abc",
            campaign_ids=["1001"],
        )
        assert result["error"] == "Validation failed"
        assert any("shared_set_id" in d and "numeric" in d for d in result["details"])

    def test_rejects_empty_campaign_ids(self, config):
        result = write.attach_shared_set_to_campaigns(
            config,
            customer_id="123-456-7890",
            shared_set_id="9001",
            campaign_ids=[],
        )
        assert result["error"] == "Validation failed"
        assert "At least one campaign_id is required" in result["details"]

    def test_rejects_non_numeric_campaign_id(self, config):
        result = write.attach_shared_set_to_campaigns(
            config,
            customer_id="123-456-7890",
            shared_set_id="9001",
            campaign_ids=["1001", "abc"],
        )
        assert result["error"] == "Validation failed"
        assert any("'abc'" in d and "numeric" in d for d in result["details"])

    def test_dedups_campaign_ids(self, config):
        result = write.attach_shared_set_to_campaigns(
            config,
            customer_id="123-456-7890",
            shared_set_id="9001",
            campaign_ids=["1001", "1002", "1001", " 1003 "],
        )
        assert result["operation"] == "attach_shared_set_to_campaigns"
        assert result["changes"]["campaign_ids"] == ["1001", "1002", "1003"]

    def test_returns_preview_with_plan_id(self, config):
        result = write.attach_shared_set_to_campaigns(
            config,
            customer_id="123-456-7890",
            shared_set_id="9001",
            campaign_ids=["1001"],
        )
        assert "plan_id" in result
        assert result["entity_type"] == "campaign_shared_set"
        assert result["entity_id"] == "9001"
        assert result["changes"]["shared_set_id"] == "9001"


class TestDetachSharedSetValidation:
    def test_rejects_missing_shared_set_id(self, config):
        result = write.detach_shared_set_from_campaigns(
            config, customer_id="123-456-7890", campaign_ids=["1001"]
        )
        assert result["error"] == "Validation failed"
        assert "shared_set_id is required" in result["details"]

    def test_returns_preview_with_plan_id(self, config):
        result = write.detach_shared_set_from_campaigns(
            config,
            customer_id="123-456-7890",
            shared_set_id="9001",
            campaign_ids=["1001", "1002"],
        )
        assert "plan_id" in result
        assert result["operation"] == "detach_shared_set_from_campaigns"
        assert result["entity_type"] == "campaign_shared_set"
        assert result["changes"]["campaign_ids"] == ["1001", "1002"]


def test_apply_attach_shared_set_to_campaigns_creates_one_op_per_campaign():
    captured: dict = {}

    # Real SDK signature: mutate_campaign_shared_sets(request=...) — the
    # flattened partial_failure kwarg is NOT accepted, so partial_failure
    # rides on the request object.
    def _mutate(request):
        captured["customer_id"] = request.customer_id
        captured["operations"] = list(request.operations)
        captured["partial_failure"] = request.partial_failure
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{request.customer_id}/campaignSharedSets/{i}~9001"
                )
                for i, _ in enumerate(request.operations, start=1001)
            ],
            partial_failure_error=SimpleNamespace(code=0, message="", details=[]),
        )

    css_service = SimpleNamespace(mutate_campaign_shared_sets=_mutate)
    campaign_service = _FakePathService("campaigns")
    shared_set_service = SimpleNamespace(
        shared_set_path=lambda cid, ssid: f"customers/{cid}/sharedSets/{ssid}"
    )

    client = _FakeClient(
        {
            "CampaignSharedSetService": css_service,
            "CampaignService": campaign_service,
            "SharedSetService": shared_set_service,
        }
    )

    result = write._apply_attach_shared_set_to_campaigns(
        client,
        "1234567890",
        {"shared_set_id": "9001", "campaign_ids": ["1001", "1002", "1003"]},
    )

    assert captured["customer_id"] == "1234567890"
    assert captured["partial_failure"] is True
    assert len(captured["operations"]) == 3
    op0 = captured["operations"][0]
    assert op0.create.campaign == "customers/1234567890/campaigns/1001"
    assert op0.create.shared_set == "customers/1234567890/sharedSets/9001"
    assert result["campaign_count"] == 3
    assert len(result["resource_names"]) == 3
    assert "partial_failure" not in result
    assert "failed_campaigns" not in result


def test_apply_attach_shared_set_to_campaigns_surfaces_partial_failure():
    """When a subset of attach ops fail, succeeded ones return resource_names
    and failed ones surface in failed_campaigns with their campaign_id."""

    def _mutate(request):
        # Op #1 (index 1, campaign_id "1002") fails — empty resource_name.
        customer_id = request.customer_id
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/campaignSharedSets/1001~9001"
                ),
                SimpleNamespace(resource_name=""),
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/campaignSharedSets/1003~9001"
                ),
            ],
            partial_failure_error=SimpleNamespace(
                code=3,
                message="Some operations failed",
                details=[],
            ),
        )

    css_service = SimpleNamespace(mutate_campaign_shared_sets=_mutate)
    campaign_service = _FakePathService("campaigns")
    shared_set_service = SimpleNamespace(
        shared_set_path=lambda cid, ssid: f"customers/{cid}/sharedSets/{ssid}"
    )

    client = _FakeClient(
        {
            "CampaignSharedSetService": css_service,
            "CampaignService": campaign_service,
            "SharedSetService": shared_set_service,
        }
    )

    result = write._apply_attach_shared_set_to_campaigns(
        client,
        "1234567890",
        {"shared_set_id": "9001", "campaign_ids": ["1001", "1002", "1003"]},
    )

    assert result["campaign_count"] == 2
    assert result["resource_names"] == [
        "customers/1234567890/campaignSharedSets/1001~9001",
        "customers/1234567890/campaignSharedSets/1003~9001",
    ]
    assert result["partial_failure"] is True
    assert result["partial_failure_message"] == "Some operations failed"
    assert len(result["failed_campaigns"]) == 1
    failed = result["failed_campaigns"][0]
    assert failed["campaign_id"] == "1002"
    assert failed["operation_index"] == 1
    assert "error" in failed


def test_apply_detach_shared_set_from_campaigns_uses_composite_resource_name():
    captured: dict = {}

    def _mutate(request):
        captured["customer_id"] = request.customer_id
        captured["operations"] = list(request.operations)
        captured["partial_failure"] = request.partial_failure
        return SimpleNamespace(
            results=[
                SimpleNamespace(resource_name=op.remove)
                for op in request.operations
            ],
            partial_failure_error=SimpleNamespace(code=0, message="", details=[]),
        )

    css_service = SimpleNamespace(mutate_campaign_shared_sets=_mutate)
    client = _FakeClient({"CampaignSharedSetService": css_service})

    result = write._apply_detach_shared_set_from_campaigns(
        client,
        "1234567890",
        {"shared_set_id": "9001", "campaign_ids": ["1001", "1002"]},
    )

    assert captured["customer_id"] == "1234567890"
    assert captured["partial_failure"] is True
    ops = captured["operations"]
    assert len(ops) == 2
    assert ops[0].remove == "customers/1234567890/campaignSharedSets/1001~9001"
    assert ops[1].remove == "customers/1234567890/campaignSharedSets/1002~9001"
    assert result["shared_set_id"] == "9001"
    assert result["campaign_count"] == 2
    assert "partial_failure" not in result
    assert "failed_campaigns" not in result


def test_apply_detach_shared_set_from_campaigns_surfaces_partial_failure():
    """When a detach op targets a non-existent linkage, that op fails but
    the others succeed and the result surfaces both."""

    def _mutate(request):
        # Op #0 (campaign_id "1001") fails — empty resource_name.
        operations = list(request.operations)
        return SimpleNamespace(
            results=[
                SimpleNamespace(resource_name=""),
                SimpleNamespace(resource_name=operations[1].remove),
            ],
            partial_failure_error=SimpleNamespace(
                code=3,
                message="Some operations failed",
                details=[],
            ),
        )

    css_service = SimpleNamespace(mutate_campaign_shared_sets=_mutate)
    client = _FakeClient({"CampaignSharedSetService": css_service})

    result = write._apply_detach_shared_set_from_campaigns(
        client,
        "1234567890",
        {"shared_set_id": "9001", "campaign_ids": ["1001", "1002"]},
    )

    assert result["campaign_count"] == 1
    assert result["removed_resource_names"] == [
        "customers/1234567890/campaignSharedSets/1002~9001",
    ]
    assert result["partial_failure"] is True
    assert result["partial_failure_message"] == "Some operations failed"
    assert len(result["failed_campaigns"]) == 1
    failed = result["failed_campaigns"][0]
    assert failed["campaign_id"] == "1001"
    assert failed["operation_index"] == 0


# --- URL check: transient failures must not block drafting -----------------
#
# A beta user's storefront returned 429 to our reachability checks — partly
# because drafting several ads at once fires several HEADs at one origin —
# and every draft was refused as "not reachable". Rate limiting says nothing
# about whether the landing page is good.


def _fake_opener(status_by_url):
    """Opener stub returning a canned status per URL."""

    class _Resp:
        def __init__(self, status):
            self.status = status

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp(status_by_url[req.full_url])

    return _Opener()


def test_rate_limited_url_warns_instead_of_blocking(monkeypatch):
    from adloop.ads import write

    url = "https://shop.example.com/collections/x"
    monkeypatch.setattr(write, "_ssrf_error", lambda u: None)
    monkeypatch.setattr(
        write, "_build_public_only_opener", lambda: _fake_opener({url: 429})
    )

    errors, warnings = write._validate_urls([url])

    assert errors[url] is None, "429 must not block the draft"
    assert "429" in warnings[url]


def test_server_errors_are_also_treated_as_inconclusive(monkeypatch):
    from adloop.ads import write

    url = "https://shop.example.com/down"
    monkeypatch.setattr(write, "_ssrf_error", lambda u: None)
    monkeypatch.setattr(
        write, "_build_public_only_opener", lambda: _fake_opener({url: 503})
    )

    errors, warnings = write._validate_urls([url])

    assert errors[url] is None
    assert "503" in warnings[url]


def test_a_dead_page_still_blocks(monkeypatch):
    """The check exists for 404s — those do not resolve on a retry."""
    from adloop.ads import write

    url = "https://shop.example.com/gone"
    monkeypatch.setattr(write, "_ssrf_error", lambda u: None)
    monkeypatch.setattr(
        write, "_build_public_only_opener", lambda: _fake_opener({url: 404})
    )

    errors, warnings = write._validate_urls([url])

    assert errors[url] == "HTTP 404"
    assert url not in warnings


def test_a_healthy_page_passes_clean(monkeypatch):
    from adloop.ads import write

    url = "https://shop.example.com/live"
    monkeypatch.setattr(write, "_ssrf_error", lambda u: None)
    monkeypatch.setattr(
        write, "_build_public_only_opener", lambda: _fake_opener({url: 200})
    )

    errors, warnings = write._validate_urls([url])

    assert errors[url] is None
    assert warnings == {}


# --- Performance Max: refuse rather than create something that cannot serve ---
#
# A customer asked whether PMax creation was blocked by Google. It is not;
# the API supports it. But PMax has no ad groups, and Google requires the
# asset group and all its assets in one atomic mutate, including images we
# cannot supply. Accepting the channel type produced a campaign shell that
# could never serve while reporting success.


def test_performance_max_is_refused_with_a_reason(monkeypatch):
    from adloop.ads import write

    errors, warnings = write._validate_campaign(
        None,
        campaign_name="PMax Test",
        daily_budget=50.0,
        bidding_strategy="MAXIMIZE_CONVERSIONS",
        target_cpa=0,
        target_roas=0,
        channel_type="PERFORMANCE_MAX",
        keywords=None,
        geo_target_ids=["2276"],
        language_ids=["1001"],
        customer_id="1234567890",
    )

    joined = " ".join(errors)
    assert "Performance Max" in joined
    # The refusal has to explain the cause and the way forward, not just say no.
    assert "asset group" in joined
    assert "Google Ads interface" in joined


def test_performance_max_refusal_is_case_insensitive():
    from adloop.ads import write

    errors, _ = write._validate_campaign(
        None,
        campaign_name="PMax Test",
        daily_budget=50.0,
        bidding_strategy="MAXIMIZE_CONVERSIONS",
        target_cpa=0,
        target_roas=0,
        channel_type="performance_max",
        keywords=None,
        geo_target_ids=["2276"],
        language_ids=["1001"],
        customer_id="1234567890",
    )

    assert any("Performance Max" in e for e in errors)


def test_other_non_search_types_warn_but_are_allowed():
    """DISPLAY, SHOPPING and VIDEO do produce a usable campaign.

    Nothing here can populate them, since draft_ad_group is SEARCH-only,
    but a shell finished in the UI is a legitimate workflow. Only PMax is
    refused, because a PMax shell can never serve at all.
    """
    from adloop.ads import write

    for channel in ("DISPLAY", "SHOPPING", "VIDEO"):
        errors, warnings = write._validate_campaign(
            None,
            campaign_name=f"{channel} Test",
            daily_budget=50.0,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            target_cpa=0,
            target_roas=0,
            channel_type=channel,
            keywords=None,
            geo_target_ids=["2276"],
            language_ids=["1001"],
            customer_id="1234567890",
        )

        assert not any("Performance Max" in e for e in errors), channel
        assert any("shell only" in w for w in warnings), channel


def test_search_campaigns_get_neither_the_refusal_nor_the_shell_warning():
    from adloop.ads import write

    errors, warnings = write._validate_campaign(
        None,
        campaign_name="Search Test",
        daily_budget=50.0,
        bidding_strategy="MAXIMIZE_CONVERSIONS",
        target_cpa=0,
        target_roas=0,
        channel_type="SEARCH",
        keywords=None,
        geo_target_ids=["2276"],
        language_ids=["1001"],
        customer_id="1234567890",
    )

    assert not any("Performance Max" in e for e in errors)
    assert not any("shell only" in w for w in warnings)


def test_extract_error_message_reads_proto_plus_failures():
    """The Ads client returns proto-plus failures; WhichOneof is not on them.

    Seen live: a validate-only dry run against a missing ad group surfaced a
    raw gRPC dump instead of the error code and message.
    """
    from google.ads.googleads.errors import GoogleAdsException
    from google.ads.googleads.v25.errors.types import errors as E
    from google.ads.googleads.v25.errors.types import mutate_error as M

    error = E.GoogleAdsError(
        error_code=E.ErrorCode(
            mutate_error=M.MutateErrorEnum.MutateError.RESOURCE_NOT_FOUND
        ),
        message="Resource was not found.",
        trigger={"int64_value": 999999999999},
    )
    exc = GoogleAdsException(None, None, E.GoogleAdsFailure(errors=[error]), "req123")

    assert write._extract_error_message(exc) == (
        "[mutate_error=RESOURCE_NOT_FOUND] Resource was not found. "
        "(trigger: 999999999999) [request_id=req123]"
    )
