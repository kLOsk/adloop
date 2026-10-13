"""Standard Shopping campaigns: draft checks and the atomic mutate (#68)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient

from adloop.ads import shopping
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def _plans(tmp_path):
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(tmp_path, **safety) -> AdLoopConfig:
    return AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=100, **safety))


def _draft(tmp_path, linked=("123456",), **overrides):
    kwargs = dict(
        customer_id="1234567890", campaign_name="Shop DE", daily_budget=20, merchant_id="123456",
        product_bid=0.6, geo_target_ids=["2276"],
    )
    kwargs.update(overrides)
    with patch.object(shopping, "linked_merchant_ids", return_value=set(linked)):
        return shopping.draft_shopping_campaign(_config(tmp_path), **kwargs)


class TestDraft:
    def test_preview_is_paused_and_carries_the_shopping_settings(self, tmp_path):
        preview = _draft(tmp_path, bidding_strategy="target_roas", target_roas=4.0, campaign_priority="high")
        changes = preview["changes"]
        assert preview["operation"] == "create_shopping_campaign"
        assert changes["merchant_id"] == "123456"
        assert changes["campaign_priority"] == "HIGH"
        assert changes["bidding_strategy"] == "TARGET_ROAS"
        assert changes["status_on_create"] == "PAUSED"
        assert changes["ad_group_name"] == "Shop DE - All products"
        assert "language_ids" not in changes
        assert any("get_merchant_feed_health" in w for w in preview["warnings"])

    def test_inputs_are_validated(self, tmp_path):
        result = _draft(
            tmp_path, merchant_id="abc", product_bid=0, geo_target_ids=[], bidding_strategy="MAXIMIZE_CONVERSIONS",
            campaign_priority="URGENT",
        )
        details = " ".join(result["details"])
        for expected in ("merchant_id is required", "product_bid is required", "geo_target_ids is required",
                         "bidding_strategy must be one of", "campaign_priority must be one of"):
            assert expected in details
        roas = _draft(tmp_path, bidding_strategy="TARGET_ROAS")
        assert any("target_roas is required" in d for d in roas["details"])

    def test_merchant_must_be_linked_to_the_ads_account(self, tmp_path):
        result = _draft(tmp_path, linked=("999",))
        assert "not linked to this Google Ads account (linked: 999)" in result["details"][0]
        none_linked = _draft(tmp_path, linked=())
        assert "no account is linked" in none_linked["details"][0]

    def test_budget_cap_applies(self, tmp_path):
        result = _draft(tmp_path, daily_budget=500)
        assert "error" in result and "500" in result["error"]


class _FakeClient:
    """Real v25 proto types and path helpers, a recording GoogleAdsService."""

    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
        self.enums = base.enums
        self.get_type = base.get_type
        self.copy_from = base.copy_from
        self.sent = []

        def mutate(customer_id, mutate_operations):
            self.sent = list(mutate_operations)
            return SimpleNamespace(mutate_operation_responses=[])

        self._service = SimpleNamespace(
            campaign_budget_path=GoogleAdsServiceClient.campaign_budget_path,
            campaign_path=GoogleAdsServiceClient.campaign_path,
            ad_group_path=GoogleAdsServiceClient.ad_group_path,
            ad_group_criterion_path=GoogleAdsServiceClient.ad_group_criterion_path,
            mutate=mutate,
        )

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._service


def _changes(**overrides):
    changes = {
        "campaign_name": "Shop DE", "daily_budget": 20, "merchant_id": "123456", "feed_label": None,
        "campaign_priority": "LOW", "bidding_strategy": "MANUAL_CPC", "target_roas": None, "max_cpc": None,
        "product_bid": 0.6, "ad_group_name": "Shop DE - All products", "geo_target_ids": ["2276", "2040"],
        "search_partners_enabled": False,
    }
    changes.update(overrides)
    return changes


class TestMutate:
    def test_operations_follow_the_validated_flow(self):
        client = _FakeClient()
        ops = shopping.build_shopping_operations(client, "1234567890", _changes())
        kinds = [op._pb.WhichOneof("operation") for op in ops]
        assert kinds == [
            "campaign_budget_operation", "campaign_operation", "ad_group_operation", "ad_group_ad_operation",
            "ad_group_criterion_operation", "ad_group_criterion_operation",
            "campaign_criterion_operation", "campaign_criterion_operation",
        ]

        campaign = ops[1].campaign_operation.create
        assert campaign.advertising_channel_type == client.enums.AdvertisingChannelTypeEnum.SHOPPING
        assert campaign.status == client.enums.CampaignStatusEnum.PAUSED
        assert campaign.shopping_setting.merchant_id == 123456
        assert campaign.shopping_setting.campaign_priority == 0
        assert campaign.manual_cpc.enhanced_cpc_enabled is False

        assert ops[2].ad_group_operation.create.type_ == client.enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS
        # Without the (empty) product ad the campaign serves nothing.
        assert ops[3].ad_group_ad_operation.create.ad._pb.HasField("shopping_product_ad")

        root, others = ops[4].ad_group_criterion_operation.create, ops[5].ad_group_criterion_operation.create
        assert root.listing_group.type_ == client.enums.ListingGroupTypeEnum.SUBDIVISION
        assert others.listing_group.type_ == client.enums.ListingGroupTypeEnum.UNIT
        assert others.listing_group.parent_ad_group_criterion == root.resource_name
        assert others.listing_group.case_value._pb.HasField("product_condition")
        assert others.cpc_bid_micros == 600_000

        geos = [op.campaign_criterion_operation.create for op in ops[6:]]
        assert [g.location.geo_target_constant for g in geos] == ["geoTargetConstants/2276", "geoTargetConstants/2040"]
        assert not any(g._pb.HasField("language") for g in geos)

    def test_temp_ids_are_unique_across_the_request(self):
        ops = shopping.build_shopping_operations(_FakeClient(), "1234567890", _changes())
        names = [
            getattr(op, op._pb.WhichOneof("operation")).create.resource_name
            for op in ops
            if getattr(op, op._pb.WhichOneof("operation")).create.resource_name
        ]
        temp_ids = [name.rsplit("/", 1)[1].split("~")[-1] for name in names]
        assert len(temp_ids) == len(set(temp_ids)) == 5

    def test_bid_strategies_and_options(self):
        client = _FakeClient()
        roas = shopping.build_shopping_operations(
            client, "1", _changes(bidding_strategy="TARGET_ROAS", target_roas=4.0, campaign_priority="HIGH",
                                 feed_label="DE", search_partners_enabled=True),
        )[1].campaign_operation.create
        assert roas.target_roas.target_roas == 4.0
        assert roas.shopping_setting.campaign_priority == 2
        assert roas.shopping_setting.feed_label == "DE"
        assert roas.network_settings.target_search_network is True

        clicks = shopping.build_shopping_operations(
            client, "1", _changes(bidding_strategy="TARGET_SPEND", max_cpc=1.2),
        )[1].campaign_operation.create
        assert clicks.target_spend.cpc_bid_ceiling_micros == 1_200_000

    def test_apply_sends_one_atomic_mutate(self):
        client = _FakeClient()
        with patch("adloop.ads.write._extract_resource_name", return_value=None):
            result = shopping.apply_create_shopping_campaign(client, "1234567890", _changes())
        assert len(client.sent) == 8
        assert result["status"] == "PAUSED"

    def test_dispatch_knows_the_operation(self):
        from adloop.ads.write import _dispatch_ads_plan
        from adloop.safety.preview import ChangePlan

        plan = ChangePlan(operation="create_shopping_campaign", changes=_changes())
        client = _FakeClient()
        with patch("adloop.ads.write._extract_resource_name", return_value=None):
            _dispatch_ads_plan(client, "1234567890", plan)
        assert len(client.sent) == 8
