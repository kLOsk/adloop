"""Tests for AdLoop write-tool extensions:

- Customer-level scope for sitelinks/callouts/structured_snippets
- draft_call_asset
- draft_location_asset
- add_ad_schedule (+ ad_schedule integration in draft_campaign / update_campaign)
- add_geo_exclusions (+ geo_exclude_ids integration in draft_campaign / update_campaign)
- _validate_ad_schedule + _normalize_phone_e164
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


# ---------------------------------------------------------------------------
# Shared fakes (mirror test_ads_write.py to stay consistent with existing
# patterns)
# ---------------------------------------------------------------------------


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
        self.customer_asset_result = _FakeResult()
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

    def conversion_action_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/{self.prefix}/{entity_id}"


class _FakeGoogleAdsService(_FakePathService):
    def __init__(
        self,
        responses: list[_FakeMutateOperationResponse] | None = None,
        search_rows: list[object] | None = None,
    ):
        super().__init__("campaigns")
        self.operations = None
        self._responses = responses or []
        self._search_rows = search_rows or []
        self.search_calls: list[str] = []

    def mutate(self, customer_id: str, mutate_operations: list[object]) -> object:
        self.operations = mutate_operations
        return SimpleNamespace(mutate_operation_responses=self._responses)

    def search(self, customer_id: str, query: str) -> list[object]:
        self.search_calls.append(query)
        return list(self._search_rows)


class _FakeCampaignCriterionService(_FakePathService):
    def __init__(self, responses: list[_FakeResult] | None = None):
        super().__init__("campaignCriteria")
        self.operations = None
        self._responses = responses or []

    def mutate_campaign_criteria(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


class _FakeAssetSetService(_FakePathService):
    def __init__(self, responses: list[_FakeResult] | None = None):
        super().__init__("assetSets")
        self.operations = None
        self._responses = responses or []

    def mutate_asset_sets(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


class _FakeCustomerAssetSetService(_FakePathService):
    def __init__(self, responses: list[_FakeResult] | None = None):
        super().__init__("customerAssetSets")
        self.operations = None
        self._responses = responses or []

    def mutate_customer_asset_sets(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


class _FakeCampaignAssetSetService(_FakePathService):
    def __init__(self, responses: list[_FakeResult] | None = None):
        super().__init__("campaignAssetSets")
        self.operations = None
        self._responses = responses or []

    def mutate_campaign_asset_sets(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


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


# ---------------------------------------------------------------------------
# _validate_ad_schedule
# ---------------------------------------------------------------------------


class TestValidateAdSchedule:
    def test_valid_entry_normalizes_day_to_uppercase(self):
        validated, errors = write._validate_ad_schedule(
            [{"day_of_week": "monday", "start_hour": 7, "end_hour": 18}]
        )
        assert errors == []
        assert validated == [{
            "day_of_week": "MONDAY",
            "start_hour": 7,
            "start_minute": 0,
            "end_hour": 18,
            "end_minute": 0,
        }]

    def test_invalid_day_of_week_raises_error(self):
        _, errors = write._validate_ad_schedule(
            [{"day_of_week": "TOMORROW", "start_hour": 7, "end_hour": 18}]
        )
        assert any("day_of_week" in e for e in errors)

    def test_invalid_minutes_value_rejected(self):
        _, errors = write._validate_ad_schedule(
            [{
                "day_of_week": "MONDAY",
                "start_hour": 7,
                "start_minute": 7,
                "end_hour": 18,
            }]
        )
        assert any("start_minute" in e for e in errors)

    def test_end_must_be_after_start(self):
        _, errors = write._validate_ad_schedule(
            [{"day_of_week": "MONDAY", "start_hour": 18, "end_hour": 7}]
        )
        assert any("end" in e and "after" in e for e in errors)

    def test_non_dict_entry_rejected(self):
        _, errors = write._validate_ad_schedule(["MONDAY 7-18"])
        assert any("must be a dict" in e for e in errors)

    def test_hour_out_of_range_rejected(self):
        _, errors = write._validate_ad_schedule(
            [{"day_of_week": "MONDAY", "start_hour": 25, "end_hour": 26}]
        )
        assert any("start_hour" in e for e in errors)
        assert any("end_hour" in e for e in errors)

    def test_minute_increments_accepted(self):
        for minute in (0, 15, 30, 45):
            validated, errors = write._validate_ad_schedule(
                [{
                    "day_of_week": "WEDNESDAY",
                    "start_hour": 9,
                    "start_minute": minute,
                    "end_hour": 17,
                    "end_minute": minute,
                }]
            )
            assert errors == []
            assert validated[0]["start_minute"] == minute
            assert validated[0]["end_minute"] == minute


# ---------------------------------------------------------------------------
# _normalize_phone_e164
# ---------------------------------------------------------------------------


class TestNormalizePhoneE164:
    @pytest.mark.parametrize(
        "phone,country,expected",
        [
            ("(916) 339-3676", "US", "+19163393676"),
            ("9163393676", "US", "+19163393676"),
            ("19163393676", "US", "+19163393676"),
            ("+19163393676", "US", "+19163393676"),
            ("020 7946 0958", "GB", "+442079460958"),
        ],
    )
    def test_normalizes_to_e164(self, phone, country, expected):
        normalized, err = write._normalize_phone_e164(phone, country)
        assert err is None
        assert normalized == expected

    def test_empty_phone_errors(self):
        normalized, err = write._normalize_phone_e164("", "US")
        assert "empty" in err
        assert normalized == ""

    def test_unknown_country_without_plus_prefix_errors(self):
        normalized, err = write._normalize_phone_e164("123456789", "ZZ")
        assert "country_code" in err
        assert normalized == ""

    def test_already_e164_with_unknown_country_passes(self):
        normalized, err = write._normalize_phone_e164("+99000111222", "ZZ")
        assert err is None
        assert normalized == "+99000111222"


# ---------------------------------------------------------------------------
# Customer-level scope on sitelinks / callouts / structured_snippets
# ---------------------------------------------------------------------------


class TestCustomerScopeAssets:
    def test_draft_callouts_without_campaign_id_uses_customer_scope(self, config):
        result = write.draft_callouts(
            config,
            customer_id="1234567890",
            callouts=["Free Pickup", "R2 Certified"],
        )
        assert "error" not in result
        assert result["entity_type"] == "customer_asset"
        plan_id = result["plan_id"]
        plan = preview_store._pending_plans[plan_id]
        assert plan.changes["scope"] == "customer"
        assert plan.changes["campaign_id"] == ""

    def test_draft_callouts_with_campaign_id_uses_campaign_scope(self, config):
        result = write.draft_callouts(
            config,
            customer_id="1234567890",
            campaign_id="1001",
            callouts=["Free Pickup"],
        )
        assert result["entity_type"] == "campaign_asset"
        plan_id = result["plan_id"]
        plan = preview_store._pending_plans[plan_id]
        assert plan.changes["scope"] == "campaign"
        assert plan.changes["campaign_id"] == "1001"

    def test_draft_structured_snippets_customer_scope(self, config):
        result = write.draft_structured_snippets(
            config,
            customer_id="1234567890",
            snippets=[{"header": "Services", "values": ["A", "B", "C"]}],
        )
        assert result["entity_type"] == "customer_asset"
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "customer"

    def test_draft_sitelinks_customer_scope(self, config, monkeypatch):
        # _validate_urls performs HTTP — stub it out for unit tests
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: {u: None for u in urls},
        )
        result = write.draft_sitelinks(
            config,
            customer_id="1234567890",
            sitelinks=[
                {
                    "link_text": "Get a Quote",
                    "final_url": "https://example.com/quote",
                },
                {
                    "link_text": "Contact Us",
                    "final_url": "https://example.com/contact",
                },
            ],
        )
        assert result["entity_type"] == "customer_asset"
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "customer"

    def test_apply_create_callouts_customer_scope_emits_customer_asset_op(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/1~CALLOUT"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
            }
        )

        write._apply_create_callouts(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "callouts": ["Free Shipping"],
            },
        )

        assert len(google_ads.operations) == 2
        # Op 0: asset create
        asset_op = google_ads.operations[0].asset_operation.create
        assert asset_op.callout_asset.callout_text == "Free Shipping"
        # Op 1: customer asset link (NOT campaign asset)
        link_op = google_ads.operations[1].customer_asset_operation.create
        assert link_op.field_type == client.enums.AssetFieldTypeEnum.CALLOUT
        # Must NOT have populated campaign_asset_operation
        assert (
            google_ads.operations[1].campaign_asset_operation.create.field_type
            == client.enums.AssetFieldTypeEnum.UNSPECIFIED
        )

    def test_apply_create_sitelinks_customer_scope_emits_customer_asset_op(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/1~SITELINK"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
            }
        )

        write._apply_create_sitelinks(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "sitelinks": [
                    {
                        "link_text": "Quote",
                        "final_url": "https://example.com/quote",
                        "description1": "Quick quote",
                        "description2": "Sacramento-based",
                    }
                ],
            },
        )
        link_op = google_ads.operations[1].customer_asset_operation.create
        assert link_op.field_type == client.enums.AssetFieldTypeEnum.SITELINK

    def test_apply_create_structured_snippets_customer_scope(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result",
                "customers/1/customerAssets/1~STRUCTURED_SNIPPET",
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
            }
        )

        write._apply_create_structured_snippets(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "snippets": [
                    {"header": "Services", "values": ["A", "B", "C"]}
                ],
            },
        )
        link_op = google_ads.operations[1].customer_asset_operation.create
        assert link_op.field_type == client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET

    def test_apply_assets_rejects_unknown_scope(self):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
            }
        )
        with pytest.raises(ValueError, match="Unknown asset scope"):
            write._apply_assets(
                client,
                "1",
                [{"callout_text": "X"}],
                client.enums.AssetFieldTypeEnum.CALLOUT,
                lambda asset, p: None,
                scope="bogus",
                campaign_id="",
            )

    def test_apply_assets_campaign_scope_requires_campaign_id(self):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
            }
        )
        with pytest.raises(ValueError, match="campaign_id is required"):
            write._apply_assets(
                client,
                "1",
                [{"callout_text": "X"}],
                client.enums.AssetFieldTypeEnum.CALLOUT,
                lambda asset, p: None,
                scope="campaign",
                campaign_id="",
            )


# ---------------------------------------------------------------------------
# draft_call_asset
# ---------------------------------------------------------------------------


class TestDraftCallAsset:
    def test_requires_phone_number(self, config):
        result = write.draft_call_asset(config, customer_id="1234567890")
        assert "phone_number is required" in result["error"]

    def test_normalizes_us_phone_and_picks_customer_scope(self, config):
        result = write.draft_call_asset(
            config,
            customer_id="1234567890",
            phone_number="(916) 339-3676",
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["phone_number"] == "+19163393676"
        assert plan.changes["scope"] == "customer"
        assert plan.changes["country_code"] == "US"
        assert "warnings" in result
        assert any("verification" in w.lower() for w in result["warnings"])

    def test_campaign_scope_when_campaign_id_provided(self, config):
        result = write.draft_call_asset(
            config,
            customer_id="1234567890",
            campaign_id="42",
            phone_number="+19163393676",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "campaign"
        assert plan.changes["campaign_id"] == "42"

    def test_invalid_ad_schedule_short_circuits(self, config):
        result = write.draft_call_asset(
            config,
            customer_id="1234567890",
            phone_number="+19163393676",
            ad_schedule=[{"day_of_week": "BAD", "start_hour": 7, "end_hour": 18}],
        )
        assert result["error"] == "Ad schedule validation failed"


class TestApplyCreateCallAsset:
    def test_customer_scope_creates_customer_asset_link(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/1~CALL"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "ConversionActionService": _FakePathService("conversionActions"),
            }
        )

        write._apply_create_call_asset(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "phone_number": "+19163393676",
                "country_code": "US",
                "call_conversion_action_id": "",
                "ad_schedule": [
                    {
                        "day_of_week": "MONDAY",
                        "start_hour": 7,
                        "start_minute": 0,
                        "end_hour": 18,
                        "end_minute": 0,
                    }
                ],
            },
        )

        assert len(google_ads.operations) == 2
        asset_op = google_ads.operations[0].asset_operation.create
        assert asset_op.call_asset.country_code == "US"
        assert asset_op.call_asset.phone_number == "+19163393676"
        # Schedule embedded on call asset
        assert len(asset_op.call_asset.ad_schedule_targets) == 1
        sched = asset_op.call_asset.ad_schedule_targets[0]
        assert sched.day_of_week == client.enums.DayOfWeekEnum.MONDAY
        assert sched.start_hour == 7
        assert sched.end_hour == 18
        # Customer-scope link
        link = google_ads.operations[1].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.CALL

    def test_campaign_scope_creates_campaign_asset_link(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result", "customers/1/campaignAssets/42~CALL"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "ConversionActionService": _FakePathService("conversionActions"),
            }
        )

        write._apply_create_call_asset(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "phone_number": "+442079460958",
                "country_code": "GB",
                "call_conversion_action_id": "",
                "ad_schedule": [],
            },
        )
        link = google_ads.operations[1].campaign_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.CALL
        assert link.campaign == "customers/1/campaigns/42"

    def test_with_call_conversion_action(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/1~CALL"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "ConversionActionService": _FakePathService("conversionActions"),
            }
        )

        write._apply_create_call_asset(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "phone_number": "+19163393676",
                "country_code": "US",
                "call_conversion_action_id": "777",
                "ad_schedule": [],
            },
        )
        asset_op = google_ads.operations[0].asset_operation.create
        assert asset_op.call_asset.call_conversion_action == (
            "customers/1/conversionActions/777"
        )
        assert (
            asset_op.call_asset.call_conversion_reporting_state
            == client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )

    def test_unknown_scope_raises(self):
        google_ads = _FakeGoogleAdsService([])
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "ConversionActionService": _FakePathService("conversionActions"),
            }
        )
        with pytest.raises(ValueError, match="Unknown scope"):
            write._apply_create_call_asset(
                client,
                "1",
                {
                    "scope": "bogus",
                    "campaign_id": "",
                    "phone_number": "+19163393676",
                    "country_code": "US",
                    "call_conversion_action_id": "",
                    "ad_schedule": [],
                },
            )


# ---------------------------------------------------------------------------
# draft_location_asset
# ---------------------------------------------------------------------------


class TestDraftLocationAsset:
    def test_requires_business_profile_account_id(self, config):
        result = write.draft_location_asset(config, customer_id="1234567890")
        assert "business_profile_account_id is required" in result["error"]

    def test_default_asset_set_name_uses_id(self, config):
        result = write.draft_location_asset(
            config,
            customer_id="1234567890",
            business_profile_account_id="987654321",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["asset_set_name"] == "GBP Locations - 987654321"
        assert plan.changes["scope"] == "customer"
        assert plan.changes["business_profile_account_id"] == "987654321"

    def test_campaign_scope_when_campaign_id_provided(self, config):
        result = write.draft_location_asset(
            config,
            customer_id="1234567890",
            business_profile_account_id="987654321",
            campaign_id="42",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "campaign"

    def test_warnings_mention_gbp_link_requirement(self, config):
        result = write.draft_location_asset(
            config,
            customer_id="1234567890",
            business_profile_account_id="987654321",
        )
        assert "warnings" in result
        assert any("Business Profile" in w for w in result["warnings"])


class TestApplyCreateLocationAsset:
    def test_customer_scope_creates_asset_set_and_customer_link(self):
        asset_set_service = _FakeAssetSetService(
            [_FakeResult("customers/1/assetSets/9001")]
        )
        customer_link_service = _FakeCustomerAssetSetService(
            [_FakeResult("customers/1/customerAssetSets/9001")]
        )
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetSetService": asset_set_service,
                "CustomerAssetSetService": customer_link_service,
            }
        )

        result = write._apply_create_location_asset(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "business_profile_account_id": "987",
                "asset_set_name": "GBP Locations - 987",
                "label_filters": [],
                "listing_id_filters": [],
            },
        )
        assert result["asset_set"] == "customers/1/assetSets/9001"
        assert result["customer_asset_set"] == "customers/1/customerAssetSets/9001"

        op = asset_set_service.operations[0].create
        assert op.name == "GBP Locations - 987"
        assert op.type_ == client.enums.AssetSetTypeEnum.LOCATION_SYNC
        assert (
            op.location_set.business_profile_location_set.business_account_id
            == "987"
        )

    def test_campaign_scope_creates_campaign_asset_set(self):
        asset_set_service = _FakeAssetSetService(
            [_FakeResult("customers/1/assetSets/9001")]
        )
        campaign_link_service = _FakeCampaignAssetSetService(
            [_FakeResult("customers/1/campaignAssetSets/42~9001")]
        )
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetSetService": asset_set_service,
                "CampaignService": _FakePathService("campaigns"),
                "CampaignAssetSetService": campaign_link_service,
            }
        )

        result = write._apply_create_location_asset(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "business_profile_account_id": "987",
                "asset_set_name": "GBP",
                "label_filters": [],
                "listing_id_filters": [],
            },
        )
        assert (
            result["campaign_asset_set"] == "customers/1/campaignAssetSets/42~9001"
        )
        link_op = campaign_link_service.operations[0].create
        assert link_op.campaign == "customers/1/campaigns/42"
        assert link_op.asset_set == "customers/1/assetSets/9001"

    def test_label_filters_propagate(self):
        asset_set_service = _FakeAssetSetService(
            [_FakeResult("customers/1/assetSets/9001")]
        )
        customer_link_service = _FakeCustomerAssetSetService(
            [_FakeResult("customers/1/customerAssetSets/9001")]
        )
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetSetService": asset_set_service,
                "CustomerAssetSetService": customer_link_service,
            }
        )

        write._apply_create_location_asset(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "business_profile_account_id": "987",
                "asset_set_name": "GBP",
                "label_filters": ["Storefront", "Warehouse"],
                "listing_id_filters": [],
            },
        )
        op = asset_set_service.operations[0].create
        assert list(op.location_set.business_profile_location_set.label_filters) == [
            "Storefront",
            "Warehouse",
        ]

    def test_campaign_scope_requires_campaign_id(self):
        asset_set_service = _FakeAssetSetService(
            [_FakeResult("customers/1/assetSets/9001")]
        )
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetSetService": asset_set_service,
                "CampaignService": _FakePathService("campaigns"),
                "CampaignAssetSetService": _FakeCampaignAssetSetService(),
            }
        )
        with pytest.raises(ValueError, match="campaign_id required"):
            write._apply_create_location_asset(
                client,
                "1",
                {
                    "scope": "campaign",
                    "campaign_id": "",
                    "business_profile_account_id": "987",
                    "asset_set_name": "GBP",
                    "label_filters": [],
                    "listing_id_filters": [],
                },
            )


# ---------------------------------------------------------------------------
# add_ad_schedule
# ---------------------------------------------------------------------------


class TestAddAdSchedule:
    def test_requires_campaign_id(self, config):
        result = write.add_ad_schedule(
            config,
            customer_id="1234567890",
            schedule=[
                {"day_of_week": "MONDAY", "start_hour": 7, "end_hour": 18}
            ],
        )
        assert "campaign_id is required" in result["error"]

    def test_requires_at_least_one_entry(self, config):
        result = write.add_ad_schedule(
            config,
            customer_id="1234567890",
            campaign_id="42",
            schedule=[],
        )
        assert "At least one schedule entry" in result["error"]

    def test_invalid_schedule_returns_validation_failure(self, config):
        result = write.add_ad_schedule(
            config,
            customer_id="1234567890",
            campaign_id="42",
            schedule=[{"day_of_week": "BAD", "start_hour": 7, "end_hour": 18}],
        )
        assert result["error"] == "Validation failed"

    def test_valid_schedule_stores_plan(self, config):
        result = write.add_ad_schedule(
            config,
            customer_id="1234567890",
            campaign_id="42",
            schedule=[
                {"day_of_week": "Monday", "start_hour": 7, "end_hour": 18},
                {"day_of_week": "TUESDAY", "start_hour": 7, "end_hour": 18},
            ],
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "add_ad_schedule"
        assert plan.changes["campaign_id"] == "42"
        assert len(plan.changes["schedule"]) == 2
        assert plan.changes["schedule"][0]["day_of_week"] == "MONDAY"


class TestApplyAddAdSchedule:
    def test_creates_one_criterion_per_day(self):
        crit_service = _FakeCampaignCriterionService(
            [
                _FakeResult("customers/1/campaignCriteria/42~1001"),
                _FakeResult("customers/1/campaignCriteria/42~1002"),
            ]
        )
        client = _FakeClient(
            {
                "CampaignService": _FakePathService("campaigns"),
                "CampaignCriterionService": crit_service,
            }
        )

        result = write._apply_add_ad_schedule(
            client,
            "1",
            {
                "campaign_id": "42",
                "schedule": [
                    {
                        "day_of_week": "MONDAY",
                        "start_hour": 7,
                        "start_minute": 0,
                        "end_hour": 18,
                        "end_minute": 30,
                    },
                    {
                        "day_of_week": "TUESDAY",
                        "start_hour": 9,
                        "start_minute": 15,
                        "end_hour": 17,
                        "end_minute": 0,
                    },
                ],
            },
        )

        assert len(crit_service.operations) == 2
        first = crit_service.operations[0].create
        assert first.campaign == "customers/1/campaigns/42"
        assert first.ad_schedule.day_of_week == client.enums.DayOfWeekEnum.MONDAY
        assert first.ad_schedule.start_hour == 7
        assert first.ad_schedule.end_hour == 18
        assert first.ad_schedule.end_minute == client.enums.MinuteOfHourEnum.THIRTY

        second = crit_service.operations[1].create
        assert second.ad_schedule.day_of_week == client.enums.DayOfWeekEnum.TUESDAY
        assert second.ad_schedule.start_minute == client.enums.MinuteOfHourEnum.FIFTEEN

        assert len(result["campaign_criteria"]) == 2


# ---------------------------------------------------------------------------
# add_geo_exclusions
# ---------------------------------------------------------------------------


class TestAddGeoExclusions:
    def test_requires_campaign_id(self, config):
        result = write.add_geo_exclusions(
            config,
            customer_id="1234567890",
            geo_target_ids=["1014962"],
        )
        assert "campaign_id is required" in result["error"]

    def test_requires_at_least_one_geo(self, config):
        result = write.add_geo_exclusions(
            config,
            customer_id="1234567890",
            campaign_id="42",
            geo_target_ids=[],
        )
        assert "At least one geo_target_id" in result["error"]

    def test_strips_blank_entries_and_stores_plan(self, config):
        result = write.add_geo_exclusions(
            config,
            customer_id="1234567890",
            campaign_id="42",
            geo_target_ids=[" 1014962 ", "", "1013570"],
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "add_geo_exclusions"
        assert plan.changes["geo_target_ids"] == ["1014962", "1013570"]


class TestApplyAddGeoExclusions:
    def test_creates_negative_location_criteria(self):
        crit_service = _FakeCampaignCriterionService(
            [
                _FakeResult("customers/1/campaignCriteria/42~1014962"),
                _FakeResult("customers/1/campaignCriteria/42~1013570"),
            ]
        )
        client = _FakeClient(
            {
                "CampaignService": _FakePathService("campaigns"),
                "CampaignCriterionService": crit_service,
            }
        )

        write._apply_add_geo_exclusions(
            client,
            "1",
            {"campaign_id": "42", "geo_target_ids": ["1014962", "1013570"]},
        )

        assert len(crit_service.operations) == 2
        first = crit_service.operations[0].create
        assert first.campaign == "customers/1/campaigns/42"
        assert first.location.geo_target_constant == "geoTargetConstants/1014962"
        assert first.negative is True

        second = crit_service.operations[1].create
        assert second.location.geo_target_constant == "geoTargetConstants/1013570"
        assert second.negative is True


# ---------------------------------------------------------------------------
# draft_campaign integration with new params
# ---------------------------------------------------------------------------


class TestDraftCampaignNewParams:
    def test_geo_exclude_overlap_returns_error(self, config):
        result = write.draft_campaign(
            config,
            customer_id="1234567890",
            campaign_name="X",
            daily_budget=10,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            geo_target_ids=["2840"],
            geo_exclude_ids=["2840"],
            language_ids=["1000"],
        )
        assert result["error"] == "geo_exclude_ids overlap with geo_target_ids"

    def test_invalid_ad_schedule_returns_error(self, config):
        result = write.draft_campaign(
            config,
            customer_id="1234567890",
            campaign_name="X",
            daily_budget=10,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            geo_target_ids=["2840"],
            language_ids=["1000"],
            ad_schedule=[
                {"day_of_week": "BOGUS", "start_hour": 7, "end_hour": 18},
            ],
        )
        assert result["error"] == "Ad schedule validation failed"

    def test_valid_new_params_stored_in_plan(self, config):
        result = write.draft_campaign(
            config,
            customer_id="1234567890",
            campaign_name="X",
            daily_budget=10,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
            geo_target_ids=["2840"],
            geo_exclude_ids=["1014962", "1013570"],
            language_ids=["1000"],
            ad_schedule=[
                {"day_of_week": "monday", "start_hour": 7, "end_hour": 18},
            ],
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["geo_exclude_ids"] == ["1014962", "1013570"]
        assert plan.changes["ad_schedule"][0]["day_of_week"] == "MONDAY"


class TestApplyCreateCampaignNewParams:
    def _build_client(self, num_extra_responses: int):
        responses = [
            _FakeMutateOperationResponse(
                "campaign_budget_result", "customers/1/campaignBudgets/1"
            ),
            _FakeMutateOperationResponse(
                "campaign_result", "customers/1/campaigns/2"
            ),
            _FakeMutateOperationResponse(
                "ad_group_result", "customers/1/adGroups/3"
            ),
        ] + [
            _FakeMutateOperationResponse(
                "campaign_criterion_result",
                f"customers/1/campaignCriteria/2~{i}",
            )
            for i in range(num_extra_responses)
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "CampaignService": _FakePathService("campaigns"),
                "CampaignBudgetService": _FakePathService("campaignBudgets"),
                "AdGroupService": _FakePathService("adGroups"),
            }
        )
        return client, google_ads

    def test_geo_exclusions_emit_negative_criteria(self):
        client, google_ads = self._build_client(num_extra_responses=4)
        write._apply_create_campaign(
            client,
            "1",
            {
                "campaign_name": "X",
                "daily_budget": 10,
                "bidding_strategy": "MAXIMIZE_CONVERSIONS",
                "channel_type": "SEARCH",
                "ad_group_name": "Default",
                "geo_target_ids": ["2840"],
                "language_ids": ["1000"],
                "geo_exclude_ids": ["1014962", "1013570"],
            },
        )
        # Operations: budget, campaign, ad_group, geo (1), lang (1), excl (2) = 7
        assert len(google_ads.operations) == 7
        excl_ops = google_ads.operations[5:7]
        for op, geo_id in zip(excl_ops, ["1014962", "1013570"]):
            crit = op.campaign_criterion_operation.create
            assert crit.location.geo_target_constant == f"geoTargetConstants/{geo_id}"
            assert crit.negative is True

    def test_ad_schedule_entries_emit_schedule_criteria(self):
        client, google_ads = self._build_client(num_extra_responses=4)
        write._apply_create_campaign(
            client,
            "1",
            {
                "campaign_name": "X",
                "daily_budget": 10,
                "bidding_strategy": "MAXIMIZE_CONVERSIONS",
                "channel_type": "SEARCH",
                "ad_group_name": "Default",
                "geo_target_ids": ["2840"],
                "language_ids": ["1000"],
                "ad_schedule": [
                    {
                        "day_of_week": "MONDAY",
                        "start_hour": 7,
                        "start_minute": 0,
                        "end_hour": 18,
                        "end_minute": 0,
                    },
                    {
                        "day_of_week": "FRIDAY",
                        "start_hour": 7,
                        "start_minute": 0,
                        "end_hour": 18,
                        "end_minute": 0,
                    },
                ],
            },
        )
        # budget, campaign, ad_group, geo (1), lang (1), schedule (2) = 7
        assert len(google_ads.operations) == 7
        for op, day in zip(
            google_ads.operations[5:7],
            [client.enums.DayOfWeekEnum.MONDAY, client.enums.DayOfWeekEnum.FRIDAY],
        ):
            crit = op.campaign_criterion_operation.create
            assert crit.ad_schedule.day_of_week == day


# ---------------------------------------------------------------------------
# update_campaign integration with new params
# ---------------------------------------------------------------------------


class TestUpdateCampaignNewParams:
    def test_geo_exclude_overlap_returns_error(self, config):
        result = write.update_campaign(
            config,
            customer_id="1234567890",
            campaign_id="42",
            geo_target_ids=["2840"],
            geo_exclude_ids=["2840"],
        )
        assert result["error"] == "Validation failed"
        assert any("overlap" in d for d in result["details"])

    def test_invalid_ad_schedule_returns_validation_error(self, config):
        result = write.update_campaign(
            config,
            customer_id="1234567890",
            campaign_id="42",
            ad_schedule=[
                {"day_of_week": "BOGUS", "start_hour": 7, "end_hour": 18}
            ],
        )
        assert result["error"] == "Validation failed"

    def test_setting_only_geo_exclusions_passes_validation(self, config):
        result = write.update_campaign(
            config,
            customer_id="1234567890",
            campaign_id="42",
            geo_exclude_ids=["1014962"],
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["geo_exclude_ids"] == ["1014962"]

    def test_setting_only_ad_schedule_passes_validation(self, config):
        result = write.update_campaign(
            config,
            customer_id="1234567890",
            campaign_id="42",
            ad_schedule=[
                {"day_of_week": "monday", "start_hour": 7, "end_hour": 18},
            ],
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["ad_schedule"][0]["day_of_week"] == "MONDAY"

    def test_empty_list_clears_field(self, config):
        result = write.update_campaign(
            config,
            customer_id="1234567890",
            campaign_id="42",
            geo_exclude_ids=[],
        )
        # An empty list is a valid "clear" instruction — should not error.
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["geo_exclude_ids"] == []


class TestApplyUpdateCampaignNewParams:
    def _build_client(self, search_results: list[object] | None = None):
        google_ads = _FakeGoogleAdsService(
            responses=[
                _FakeMutateOperationResponse(
                    "campaign_criterion_result",
                    "customers/1/campaignCriteria/42~rep",
                )
            ]
            * 8,
            search_rows=search_results or [],
        )
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "CampaignService": _FakePathService("campaigns"),
            }
        )
        return client, google_ads

    def test_geo_exclusions_replace_semantics(self):
        existing_excl = SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="customers/1/campaignCriteria/42~old"
            )
        )
        client, google_ads = self._build_client([existing_excl])

        write._apply_update_campaign(
            client,
            "1",
            {
                "campaign_id": "42",
                "geo_exclude_ids": ["1014962", "1013570"],
            },
        )

        # Should have queried for existing negative-location criteria
        assert any(
            "campaign_criterion.negative = TRUE" in q
            for q in google_ads.search_calls
        )
        # First op = remove old, then 2 adds
        assert google_ads.operations[0].campaign_criterion_operation.remove == (
            "customers/1/campaignCriteria/42~old"
        )
        for op, geo_id in zip(
            google_ads.operations[1:3],
            ["1014962", "1013570"],
        ):
            crit = op.campaign_criterion_operation.create
            assert crit.location.geo_target_constant == (
                f"geoTargetConstants/{geo_id}"
            )
            assert crit.negative is True

    def test_ad_schedule_replace_semantics(self):
        existing_sched = SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="customers/1/campaignCriteria/42~old"
            )
        )
        client, google_ads = self._build_client([existing_sched])

        write._apply_update_campaign(
            client,
            "1",
            {
                "campaign_id": "42",
                "ad_schedule": [
                    {
                        "day_of_week": "MONDAY",
                        "start_hour": 9,
                        "start_minute": 0,
                        "end_hour": 17,
                        "end_minute": 0,
                    },
                ],
            },
        )

        assert any(
            "AD_SCHEDULE" in q for q in google_ads.search_calls
        )
        assert google_ads.operations[0].campaign_criterion_operation.remove == (
            "customers/1/campaignCriteria/42~old"
        )
        new = google_ads.operations[1].campaign_criterion_operation.create
        assert new.ad_schedule.day_of_week == client.enums.DayOfWeekEnum.MONDAY
        assert new.ad_schedule.start_hour == 9

    def test_empty_geo_exclude_ids_only_removes(self):
        existing = SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="customers/1/campaignCriteria/42~old"
            )
        )
        client, google_ads = self._build_client([existing])

        write._apply_update_campaign(
            client,
            "1",
            {"campaign_id": "42", "geo_exclude_ids": []},
        )
        # Only the remove op should be present
        assert len(google_ads.operations) == 1
        assert google_ads.operations[0].campaign_criterion_operation.remove == (
            "customers/1/campaignCriteria/42~old"
        )


# ---------------------------------------------------------------------------
# _populate_ad_schedule_info direct test
# ---------------------------------------------------------------------------


class TestPopulateAdScheduleInfo:
    def test_populates_all_fields_on_proto(self):
        client = _FakeClient(
            {"GoogleAdsService": _FakeGoogleAdsService()}
        )
        info = client.get_type("AdScheduleInfo")
        write._populate_ad_schedule_info(
            client,
            info,
            {
                "day_of_week": "FRIDAY",
                "start_hour": 9,
                "start_minute": 30,
                "end_hour": 17,
                "end_minute": 45,
            },
        )
        assert info.day_of_week == client.enums.DayOfWeekEnum.FRIDAY
        assert info.start_hour == 9
        assert info.end_hour == 17
        assert info.start_minute == client.enums.MinuteOfHourEnum.THIRTY
        assert info.end_minute == client.enums.MinuteOfHourEnum.FORTY_FIVE


# ---------------------------------------------------------------------------
# _execute_plan dispatch coverage for new operations
# ---------------------------------------------------------------------------


class TestExecutePlanDispatch:
    def test_new_operations_present_in_dispatch(self):
        # Static read of source code rather than monkey-patching internals
        import inspect

        source = inspect.getsource(write._execute_plan)
        for op in (
            "create_call_asset",
            "create_location_asset",
            "add_ad_schedule",
            "add_geo_exclusions",
        ):
            assert f'"{op}"' in source, f"{op} missing from dispatch"


# ---------------------------------------------------------------------------
# Server-tool registration smoke check
# ---------------------------------------------------------------------------


class TestServerToolRegistration:
    @pytest.fixture
    def tools_by_name(self):
        import asyncio

        from adloop.server import mcp

        async def _list():
            return await mcp.list_tools()

        tools = asyncio.run(_list())
        return {t.name: t for t in tools}

    def test_new_tools_are_registered(self, tools_by_name):
        for name in (
            "draft_call_asset",
            "draft_location_asset",
            "add_ad_schedule",
            "add_geo_exclusions",
        ):
            assert name in tools_by_name, f"{name} not registered"

    def test_sitelinks_callouts_snippets_make_campaign_id_optional(
        self, tools_by_name
    ):
        for name in ("draft_sitelinks", "draft_callouts", "draft_structured_snippets"):
            tool = tools_by_name[name]
            required = tool.parameters.get("required", [])
            assert "campaign_id" not in required, (
                f"{name} should not require campaign_id"
            )

    def test_draft_campaign_exposes_new_optional_params(self, tools_by_name):
        params = tools_by_name["draft_campaign"].parameters["properties"]
        assert "geo_exclude_ids" in params
        assert "ad_schedule" in params

    def test_update_campaign_exposes_new_optional_params(self, tools_by_name):
        params = tools_by_name["update_campaign"].parameters["properties"]
        assert "geo_exclude_ids" in params
        assert "ad_schedule" in params

    def test_promotion_tools_are_registered(self, tools_by_name):
        for name in ("draft_promotion", "update_promotion"):
            assert name in tools_by_name, f"{name} not registered"

    def test_link_asset_to_customer_registered(self, tools_by_name):
        assert "link_asset_to_customer" in tools_by_name
        required = (
            tools_by_name["link_asset_to_customer"].parameters.get("required", [])
        )
        assert "links" in required

    def test_draft_promotion_required_params(self, tools_by_name):
        required = tools_by_name["draft_promotion"].parameters.get("required", [])
        assert "promotion_target" in required
        assert "final_url" in required
        # money_off / percent_off are mutually exclusive at validation time,
        # but neither is required at the schema level (default 0)
        assert "money_off" not in required
        assert "percent_off" not in required

    def test_update_promotion_requires_asset_id(self, tools_by_name):
        required = tools_by_name["update_promotion"].parameters.get("required", [])
        assert "asset_id" in required


# ---------------------------------------------------------------------------
# _validate_promotion_inputs
# ---------------------------------------------------------------------------


class TestValidatePromotionInputs:
    def _ok_kwargs(self, **overrides):
        defaults = dict(
            promotion_target="Window Tint",
            final_url="https://example.com/tint",
            money_off=100.0,
            percent_off=0,
            currency_code="USD",
            promotion_code="",
            orders_over_amount=0,
            occasion="",
            discount_modifier="",
            language_code="en",
            start_date="",
            end_date="",
            redemption_start_date="",
            redemption_end_date="",
            ad_schedule=None,
        )
        defaults.update(overrides)
        return defaults

    def _patched(self, monkeypatch):
        # Stub URL validation so unit tests don't hit the network
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: {u: None for u in urls},
        )

    def test_happy_path_money_off(self, monkeypatch):
        self._patched(monkeypatch)
        normalized, errors = write._validate_promotion_inputs(**self._ok_kwargs())
        assert errors == []
        assert normalized["money_off"] == 100.0
        assert normalized["percent_off"] == 0.0
        assert normalized["currency_code"] == "USD"
        assert normalized["language_code"] == "en"

    def test_happy_path_percent_off(self, monkeypatch):
        self._patched(monkeypatch)
        normalized, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(money_off=0, percent_off=15.0)
        )
        assert errors == []
        assert normalized["percent_off"] == 15.0
        assert normalized["money_off"] == 0.0

    def test_promotion_target_required(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(promotion_target="")
        )
        assert any("promotion_target is required" in e for e in errors)

    def test_promotion_target_max_20_chars(self, monkeypatch):
        self._patched(monkeypatch)
        long_target = "A" * 21
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(promotion_target=long_target)
        )
        assert any("max 20" in e for e in errors)

    def test_final_url_required(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(final_url="")
        )
        assert any("final_url is required" in e for e in errors)

    def test_money_and_percent_both_set_rejected(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(money_off=10, percent_off=5)
        )
        assert any("exactly one of money_off or percent_off" in e for e in errors)

    def test_neither_money_nor_percent_rejected(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(money_off=0, percent_off=0)
        )
        assert any("One of money_off or percent_off" in e for e in errors)

    def test_percent_off_out_of_range_rejected(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(money_off=0, percent_off=150)
        )
        assert any("must be in (0, 100]" in e for e in errors)

    def test_promotion_code_max_15_chars(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(promotion_code="A" * 16)
        )
        assert any("max 15" in e for e in errors)

    def test_invalid_occasion_rejected(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(occasion="MARDI_GRAS")
        )
        assert any("occasion 'MARDI_GRAS' invalid" in e for e in errors)

    def test_valid_occasion_normalized_uppercase(self, monkeypatch):
        self._patched(monkeypatch)
        normalized, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(occasion="black_friday")
        )
        assert errors == []
        assert normalized["occasion"] == "BLACK_FRIDAY"

    def test_invalid_discount_modifier_rejected(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(discount_modifier="MORE_THAN")
        )
        assert any("discount_modifier 'MORE_THAN' invalid" in e for e in errors)

    def test_valid_discount_modifier_up_to(self, monkeypatch):
        self._patched(monkeypatch)
        normalized, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(discount_modifier="up_to")
        )
        assert errors == []
        assert normalized["discount_modifier"] == "UP_TO"

    def test_bad_date_format_rejected(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(start_date="01/01/2026")
        )
        assert any("start_date '01/01/2026' must be YYYY-MM-DD" in e for e in errors)

    def test_iso_date_accepted(self, monkeypatch):
        self._patched(monkeypatch)
        normalized, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(start_date="2026-01-01", end_date="2026-12-31")
        )
        assert errors == []
        assert normalized["start_date"] == "2026-01-01"
        assert normalized["end_date"] == "2026-12-31"

    def test_unreachable_url_rejected(self, monkeypatch):
        # Force URL check to fail
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: {u: "Connection refused" for u in urls},
        )
        _, errors = write._validate_promotion_inputs(**self._ok_kwargs())
        assert any("not reachable" in e for e in errors)

    def test_promotion_code_and_orders_over_amount_mutually_exclusive(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(promotion_code="SAVE10", orders_over_amount=500)
        )
        assert any("mutually exclusive" in e for e in errors)

    def test_orders_over_amount_alone_is_fine(self, monkeypatch):
        self._patched(monkeypatch)
        normalized, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(orders_over_amount=500)
        )
        assert errors == []
        assert normalized["orders_over_amount"] == 500.0

    def test_ad_schedule_validation_propagates(self, monkeypatch):
        self._patched(monkeypatch)
        _, errors = write._validate_promotion_inputs(
            **self._ok_kwargs(
                ad_schedule=[{"day_of_week": "MARTES", "start_hour": 8, "end_hour": 17}]
            )
        )
        assert any("day_of_week" in e for e in errors)


# ---------------------------------------------------------------------------
# draft_promotion
# ---------------------------------------------------------------------------


class TestDraftPromotion:
    @pytest.fixture(autouse=True)
    def _stub_urls(self, monkeypatch):
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: {u: None for u in urls},
        )

    def test_customer_scope_when_no_campaign_id(self, config):
        result = write.draft_promotion(
            config,
            customer_id="1234567890",
            promotion_target="Window Tint",
            final_url="https://example.com/tint",
            money_off=100,
        )
        assert "error" not in result
        assert result["entity_type"] == "customer_asset"
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "customer"
        assert plan.changes["campaign_id"] == ""
        assert plan.changes["promotion"]["money_off"] == 100.0
        assert plan.changes["promotion"]["promotion_target"] == "Window Tint"

    def test_campaign_scope_when_campaign_id(self, config):
        result = write.draft_promotion(
            config,
            customer_id="1234567890",
            campaign_id="42",
            promotion_target="Full Front PPF",
            final_url="https://example.com/ppf",
            money_off=301,
        )
        assert result["entity_type"] == "campaign_asset"
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "campaign"
        assert plan.changes["campaign_id"] == "42"

    def test_validation_failure_returns_error_dict(self, config):
        result = write.draft_promotion(
            config,
            customer_id="1234567890",
            promotion_target="",  # required
            final_url="https://example.com/tint",
            money_off=100,
        )
        assert result.get("error") == "Validation failed"
        assert any("promotion_target" in d for d in result["details"])

    def test_percent_off_path(self, config):
        result = write.draft_promotion(
            config,
            customer_id="1234567890",
            promotion_target="Spring Sale",
            final_url="https://example.com/sale",
            percent_off=20,
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["promotion"]["percent_off"] == 20.0
        assert plan.changes["promotion"]["money_off"] == 0.0


# ---------------------------------------------------------------------------
# update_promotion
# ---------------------------------------------------------------------------


class TestUpdatePromotion:
    @pytest.fixture(autouse=True)
    def _stub_urls(self, monkeypatch):
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: {u: None for u in urls},
        )

    def test_requires_asset_id(self, config):
        result = write.update_promotion(
            config,
            customer_id="1234567890",
            asset_id="",
            promotion_target="Window Tint",
            final_url="https://example.com/tint",
            money_off=100,
        )
        assert "asset_id is required" in result["error"]

    def test_emits_swap_plan_with_old_asset_id(self, config):
        result = write.update_promotion(
            config,
            customer_id="1234567890",
            campaign_id="42",
            asset_id="55555",
            promotion_target="Full Front PPF",
            final_url="https://example.com/ppf",
            money_off=399,
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "update_promotion"
        assert plan.changes["old_asset_id"] == "55555"
        assert plan.changes["scope"] == "campaign"

    def test_swap_warning_explains_orphaned_asset(self, config):
        result = write.update_promotion(
            config,
            customer_id="1234567890",
            asset_id="55555",
            promotion_target="Tint",
            final_url="https://example.com/x",
            money_off=10,
        )
        warnings = result.get("warnings", [])
        assert any("orphaned" in w.lower() for w in warnings)
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["old_asset_id"] == "55555"
        # remove_old_asset is no longer a supported field
        assert "remove_old_asset" not in plan.changes


# ---------------------------------------------------------------------------
# _apply_create_promotion / _populate_promotion_asset
# ---------------------------------------------------------------------------


class TestApplyCreatePromotion:
    def test_customer_scope_emits_customer_asset_with_promotion_field_type(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/1~PROMOTION"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        write._apply_create_promotion(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "promotion": {
                    "promotion_target": "Tint",
                    "final_url": "https://example.com/tint",
                    "money_off": 100.0,
                    "percent_off": 0.0,
                    "currency_code": "USD",
                    "promotion_code": "",
                    "orders_over_amount": 0,
                    "occasion": "",
                    "discount_modifier": "",
                    "language_code": "en",
                    "start_date": "",
                    "end_date": "",
                    "redemption_start_date": "",
                    "redemption_end_date": "",
                    "ad_schedule": [],
                },
            },
        )

        assert len(google_ads.operations) == 2
        asset_op = google_ads.operations[0].asset_operation.create
        assert asset_op.promotion_asset.promotion_target == "Tint"
        assert asset_op.promotion_asset.money_amount_off.amount_micros == 100_000_000
        assert asset_op.promotion_asset.money_amount_off.currency_code == "USD"
        assert "https://example.com/tint" in list(asset_op.final_urls)

        link_op = google_ads.operations[1].customer_asset_operation.create
        assert link_op.field_type == client.enums.AssetFieldTypeEnum.PROMOTION

    def test_campaign_scope_emits_campaign_asset(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result", "customers/1/campaignAssets/42~PROMOTION"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        write._apply_create_promotion(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "promotion": {
                    "promotion_target": "PPF",
                    "final_url": "https://example.com/ppf",
                    "money_off": 0,
                    "percent_off": 25.0,
                    "currency_code": "USD",
                    "promotion_code": "BGI25",
                    "orders_over_amount": 0,
                    "occasion": "BLACK_FRIDAY",
                    "discount_modifier": "UP_TO",
                    "language_code": "en",
                    "start_date": "2026-11-25",
                    "end_date": "2026-11-30",
                    "redemption_start_date": "",
                    "redemption_end_date": "",
                    "ad_schedule": [],
                },
            },
        )

        asset_op = google_ads.operations[0].asset_operation.create
        # percent_off path — micros encoded
        assert asset_op.promotion_asset.percent_off == 25_000_000
        assert asset_op.promotion_asset.promotion_code == "BGI25"
        # orders_over_amount and promotion_code are a oneof — only code set here
        assert asset_op.promotion_asset.start_date == "2026-11-25"
        assert asset_op.promotion_asset.end_date == "2026-11-30"
        assert (
            asset_op.promotion_asset.occasion
            == client.enums.PromotionExtensionOccasionEnum.BLACK_FRIDAY
        )
        assert (
            asset_op.promotion_asset.discount_modifier
            == client.enums.PromotionExtensionDiscountModifierEnum.UP_TO
        )

        link_op = google_ads.operations[1].campaign_asset_operation.create
        assert link_op.field_type == client.enums.AssetFieldTypeEnum.PROMOTION


# ---------------------------------------------------------------------------
# _apply_update_promotion (swap)
# ---------------------------------------------------------------------------


class _FakeCampaignAssetService(_FakePathService):
    def __init__(self, responses: list[_FakeResult] | None = None):
        super().__init__("campaignAssets")
        self.operations = None
        self._responses = responses or []

    def mutate_campaign_assets(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


class _FakeCustomerAssetService(_FakePathService):
    def __init__(self, responses: list[_FakeResult] | None = None):
        super().__init__("customerAssets")
        self.operations = None
        self._responses = responses or []

    def mutate_customer_assets(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


class TestApplyUpdatePromotion:
    def _promo(self, **overrides):
        base = {
            "promotion_target": "PPF",
            "final_url": "https://example.com/ppf",
            "money_off": 200.0,
            "percent_off": 0,
            "currency_code": "USD",
            "promotion_code": "",
            "orders_over_amount": 0,
            "occasion": "",
            "discount_modifier": "",
            "language_code": "en",
            "start_date": "",
            "end_date": "",
            "redemption_start_date": "",
            "redemption_end_date": "",
            "ad_schedule": [],
        }
        base.update(overrides)
        return base

    def test_campaign_swap_creates_new_links_unlinks_old(self):
        # Old link found via search
        search_row = SimpleNamespace(
            campaign_asset=SimpleNamespace(
                resource_name="customers/1/campaignAssets/42~99~PROMOTION"
            )
        )
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result", "customers/1/campaignAssets/42~999~PROMOTION"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses, search_rows=[search_row])
        ca_service = _FakeCampaignAssetService([_FakeResult("removed")])
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "CampaignAssetService": ca_service,
            }
        )

        result = write._apply_update_promotion(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "old_asset_id": "99",
                "promotion": self._promo(),
            },
        )

        # Step 1+2: create + link in one mutate call
        assert len(google_ads.operations) == 2
        # Step 3: unlink old
        assert ca_service.operations is not None
        assert len(ca_service.operations) == 1
        assert (
            ca_service.operations[0].remove
            == "customers/1/campaignAssets/42~99~PROMOTION"
        )
        assert result["new_asset"] == "customers/1/assets/-1"
        assert result["old_link_removed"] == "customers/1/campaignAssets/42~99~PROMOTION"

    def test_customer_swap_uses_customer_asset_service(self):
        search_row = SimpleNamespace(
            customer_asset=SimpleNamespace(
                resource_name="customers/1/customerAssets/99~PROMOTION"
            )
        )
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/-1~PROMOTION"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses, search_rows=[search_row])
        cust_service = _FakeCustomerAssetService([_FakeResult("removed")])
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "CustomerAssetService": cust_service,
            }
        )

        write._apply_update_promotion(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "old_asset_id": "99",
                "promotion": self._promo(),
            },
        )

        # Customer-level link in step 2
        link_op = google_ads.operations[1].customer_asset_operation.create
        assert link_op.field_type == client.enums.AssetFieldTypeEnum.PROMOTION
        # Old customer-level link removed
        assert cust_service.operations is not None
        assert (
            cust_service.operations[0].remove
            == "customers/1/customerAssets/99~PROMOTION"
        )

    def test_swap_when_old_link_not_found_skips_unlink(self):
        # Empty search rows = no old link found
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result", "customers/1/campaignAssets/42~999~PROMOTION"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses, search_rows=[])
        ca_service = _FakeCampaignAssetService()
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
                "CampaignAssetService": ca_service,
            }
        )

        result = write._apply_update_promotion(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "old_asset_id": "99",
                "promotion": self._promo(),
            },
        )

        # Step 3 is no-op when old link not found
        assert ca_service.operations is None
        assert result["old_link_removed"] == ""
        assert result["new_asset"] == "customers/1/assets/-1"

    def test_unknown_scope_raises(self):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )
        with pytest.raises(ValueError, match="Unknown scope"):
            write._apply_update_promotion(
                client,
                "1",
                {
                    "scope": "bogus",
                    "campaign_id": "",
                    "old_asset_id": "99",
                    "promotion": self._promo(),
                },
            )


# ---------------------------------------------------------------------------
# Dispatch wiring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# link_asset_to_customer
# ---------------------------------------------------------------------------


class TestLinkAssetToCustomer:
    def test_validation_rejects_unknown_field_type(self, config):
        result = write.link_asset_to_customer(
            config,
            customer_id="1234567890",
            links=[{"asset_id": "12345", "field_type": "NONSENSE"}],
        )
        assert result.get("error") == "Validation failed"
        assert any("NONSENSE" in d for d in result["details"])

    def test_validation_rejects_non_numeric_asset_id(self, config):
        result = write.link_asset_to_customer(
            config,
            customer_id="1234567890",
            links=[{"asset_id": "abc", "field_type": "BUSINESS_LOGO"}],
        )
        assert result.get("error") == "Validation failed"
        assert any("must be numeric" in d for d in result["details"])

    def test_validation_requires_asset_id(self, config):
        result = write.link_asset_to_customer(
            config,
            customer_id="1234567890",
            links=[{"asset_id": "", "field_type": "AD_IMAGE"}],
        )
        assert result.get("error") == "Validation failed"
        assert any("asset_id is required" in d for d in result["details"])

    def test_validation_requires_field_type(self, config):
        result = write.link_asset_to_customer(
            config,
            customer_id="1234567890",
            links=[{"asset_id": "12345", "field_type": ""}],
        )
        assert result.get("error") == "Validation failed"
        assert any("field_type is required" in d for d in result["details"])

    def test_empty_links_rejected(self, config):
        result = write.link_asset_to_customer(
            config, customer_id="1234567890", links=[]
        )
        assert "At least one link is required" in result["error"]

    def test_happy_path_emits_customer_asset_plan(self, config):
        result = write.link_asset_to_customer(
            config,
            customer_id="1234567890",
            links=[
                {"asset_id": "120726490775", "field_type": "BUSINESS_LOGO"},
                {"asset_id": "200848497279", "field_type": "AD_IMAGE"},
            ],
        )
        assert "error" not in result
        assert result["entity_type"] == "customer_asset"
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "link_asset_to_customer"
        assert len(plan.changes["links"]) == 2
        assert plan.changes["links"][0]["field_type"] == "BUSINESS_LOGO"

    def test_apply_creates_customer_asset_operations(self):
        cust_service = _FakeCustomerAssetService(
            [_FakeResult("customers/1/customerAssets/120726490775~BUSINESS_LOGO"),
             _FakeResult("customers/1/customerAssets/200848497279~AD_IMAGE")]
        )
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
                "CustomerAssetService": cust_service,
            }
        )

        result = write._apply_link_asset_to_customer(
            client,
            "1",
            {
                "links": [
                    {"asset_id": "120726490775", "field_type": "BUSINESS_LOGO"},
                    {"asset_id": "200848497279", "field_type": "AD_IMAGE"},
                ]
            },
        )

        assert cust_service.operations is not None
        assert len(cust_service.operations) == 2
        # First op: BUSINESS_LOGO link
        op0 = cust_service.operations[0].create
        assert op0.asset == "customers/1/assets/120726490775"
        assert op0.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_LOGO
        # Second op: AD_IMAGE link
        op1 = cust_service.operations[1].create
        assert op1.asset == "customers/1/assets/200848497279"
        assert op1.field_type == client.enums.AssetFieldTypeEnum.AD_IMAGE

        assert result["linked_count"] == 2
        assert len(result["customer_assets"]) == 2


class TestPromotionDispatchWired:
    def test_create_and_update_promotion_in_dispatch(self):
        # confirm_and_apply uses an internal dispatch dict — exercise it via
        # a dry-run roundtrip on each operation. The handlers themselves
        # are tested above; here we verify the names are wired.
        from adloop.safety import preview as ps

        # Build a fake plan and put it in the store, then ensure
        # _execute_plan finds the correct dispatch entry. We can't easily
        # invoke confirm_and_apply (needs a real Ads client), but we can
        # introspect the dispatch mapping by reaching into _execute_plan's
        # source to confirm the keys exist.
        import inspect

        src = inspect.getsource(write._execute_plan)
        assert '"create_promotion": _apply_create_promotion' in src
        assert '"update_promotion": _apply_update_promotion' in src


# ---------------------------------------------------------------------------
# Asset in-place updates: update_call_asset, update_sitelink, update_callout
# ---------------------------------------------------------------------------


class TestUpdateCallAsset:
    def test_asset_id_required(self, config):
        result = write.update_call_asset(config, customer_id="1", asset_id="")
        assert "asset_id is required" in result["error"]

    def test_no_fields_to_update_rejected(self, config):
        result = write.update_call_asset(
            config, customer_id="1", asset_id="357825439813"
        )
        assert "No fields to update" in result["error"]

    def test_invalid_reporting_state(self, config):
        result = write.update_call_asset(
            config,
            customer_id="1",
            asset_id="357825439813",
            call_conversion_reporting_state="WRONG",
        )
        assert result["error"] == "Validation failed"

    def test_phone_normalized(self, config):
        result = write.update_call_asset(
            config,
            customer_id="1",
            asset_id="357825439813",
            phone_number="(916) 460-9257",
            country_code="US",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["phone_number"] == "+19164609257"
        assert plan.changes["country_code"] == "US"

    def test_repoint_to_conversion_action(self, config):
        result = write.update_call_asset(
            config,
            customer_id="1",
            asset_id="357825439813",
            call_conversion_action_id="6797442210",
            call_conversion_reporting_state="USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["call_conversion_action_id"] == "6797442210"
        assert (
            plan.changes["call_conversion_reporting_state"]
            == "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
        )


class _FakeAssetService(_FakePathService):
    def __init__(self, responses=None):
        super().__init__("assets")
        self.operations = None
        self._responses = responses or []

    def mutate_assets(self, customer_id: str, operations: list) -> object:
        self.operations = operations
        return SimpleNamespace(results=self._responses)


class _FakeConversionActionService(_FakePathService):
    def __init__(self):
        super().__init__("conversionActions")


class TestApplyUpdateCallAsset:
    def test_emits_field_mask_for_specified_fields_only(self):
        a_svc = _FakeAssetService(
            [_FakeResult("customers/1/assets/357825439813")]
        )
        client = _FakeClient(
            {
                "AssetService": a_svc,
                "ConversionActionService": _FakeConversionActionService(),
            }
        )
        write._apply_update_call_asset(
            client,
            "1",
            {
                "asset_id": "357825439813",
                "call_conversion_action_id": "6797442210",
                "call_conversion_reporting_state": "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
            },
        )

        op = a_svc.operations[0]
        asset = op.update
        assert asset.resource_name == "customers/1/assets/357825439813"
        assert asset.call_asset.call_conversion_action == "customers/1/conversionActions/6797442210"
        assert (
            asset.call_asset.call_conversion_reporting_state
            == client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
        mask = list(op.update_mask.paths)
        assert "call_asset.call_conversion_action" in mask
        assert "call_asset.call_conversion_reporting_state" in mask
        assert "call_asset.phone_number" not in mask


class TestUpdateSitelink:
    @pytest.fixture(autouse=True)
    def _stub_urls(self, monkeypatch):
        monkeypatch.setattr(
            write, "_validate_urls", lambda urls, timeout=10: {u: None for u in urls}
        )

    def test_asset_id_required(self, config):
        result = write.update_sitelink(config, customer_id="1", asset_id="")
        assert "asset_id is required" in result["error"]

    def test_link_text_max_25_chars(self, config):
        result = write.update_sitelink(
            config,
            customer_id="1",
            asset_id="357825455476",
            link_text="A" * 26,
        )
        assert result["error"] == "Validation failed"
        assert any("max 25" in d for d in result["details"])

    def test_description1_max_35(self, config):
        result = write.update_sitelink(
            config,
            customer_id="1",
            asset_id="357825455476",
            description1="X" * 36,
        )
        assert result["error"] == "Validation failed"
        assert any("description1" in d for d in result["details"])

    def test_no_fields_to_update_rejected(self, config):
        result = write.update_sitelink(
            config, customer_id="1", asset_id="357825455476"
        )
        assert "No fields to update" in result["error"]

    def test_partial_update_persists(self, config):
        result = write.update_sitelink(
            config,
            customer_id="1",
            asset_id="357825455476",
            description1="Premium ceramic from $299",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["description1"] == "Premium ceramic from $299"
        assert "link_text" not in plan.changes


class TestApplyUpdateSitelink:
    def test_emits_field_mask(self):
        a_svc = _FakeAssetService(
            [_FakeResult("customers/1/assets/357825455476")]
        )
        client = _FakeClient({"AssetService": a_svc})
        write._apply_update_sitelink(
            client,
            "1",
            {
                "asset_id": "357825455476",
                "link_text": "Auto Window Tint",
                "description1": "Premium ceramic from $299",
            },
        )
        op = a_svc.operations[0]
        asset = op.update
        assert asset.sitelink_asset.link_text == "Auto Window Tint"
        assert asset.sitelink_asset.description1 == "Premium ceramic from $299"
        mask = list(op.update_mask.paths)
        assert "sitelink_asset.link_text" in mask
        assert "sitelink_asset.description1" in mask
        assert "sitelink_asset.description2" not in mask


class TestUpdateCallout:
    def test_asset_id_required(self, config):
        result = write.update_callout(
            config, customer_id="1", asset_id="", callout_text="Free Snacks"
        )
        assert "asset_id is required" in result["error"]

    def test_callout_text_required(self, config):
        result = write.update_callout(
            config, customer_id="1", asset_id="123", callout_text="   "
        )
        assert "callout_text is required" in result["error"]

    def test_max_25_chars(self, config):
        result = write.update_callout(
            config,
            customer_id="1",
            asset_id="123",
            callout_text="A" * 26,
        )
        assert result["error"] == "Validation failed"

    def test_happy_path(self, config):
        result = write.update_callout(
            config,
            customer_id="1",
            asset_id="357825439780",
            callout_text="Free Snacks & Lounge",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["callout_text"] == "Free Snacks & Lounge"


class TestApplyUpdateCallout:
    def test_emits_minimal_field_mask(self):
        a_svc = _FakeAssetService(
            [_FakeResult("customers/1/assets/357825439780")]
        )
        client = _FakeClient({"AssetService": a_svc})
        write._apply_update_callout(
            client,
            "1",
            {"asset_id": "357825439780", "callout_text": "Free Snacks & Lounge"},
        )
        op = a_svc.operations[0]
        asset = op.update
        assert asset.callout_asset.callout_text == "Free Snacks & Lounge"
        assert list(op.update_mask.paths) == ["callout_asset.callout_text"]


# ---------------------------------------------------------------------------
# MCP registration + dispatch wiring (asset updates + conversion actions)
# ---------------------------------------------------------------------------


class TestNewToolsRegistered:
    @pytest.fixture(scope="class")
    def tools_by_name(self):
        import asyncio
        from adloop.server import mcp

        async def _list():
            return await mcp.list_tools()

        tools = asyncio.run(_list())
        return {t.name: t for t in tools}

    def test_asset_update_and_conversion_tools_registered(self, tools_by_name):
        for name in (
            # conversion actions
            "draft_create_conversion_action",
            "draft_update_conversion_action",
            "draft_remove_conversion_action",
            # asset updates
            "update_call_asset",
            "update_sitelink",
            "update_callout",
        ):
            assert name in tools_by_name, f"{name} not registered"

    def test_dispatch_routes_asset_and_conversion_ops(self):
        import inspect

        src = inspect.getsource(write._execute_plan)
        # asset update ops
        assert '"update_call_asset": _apply_update_call_asset' in src
        assert '"update_sitelink": _apply_update_sitelink' in src
        assert '"update_callout": _apply_update_callout' in src
        # conversion-action ops
        assert '"create_conversion_action"' in src
        assert '"update_conversion_action"' in src
        assert '"remove_conversion_action"' in src


# ---------------------------------------------------------------------------
# Image asset field-type detection + customer-scope refactor
# ---------------------------------------------------------------------------


class TestDetectImageFieldType:
    @pytest.mark.parametrize(
        "width,height,name,expected",
        [
            (1200, 1200, "team-square", "SQUARE_MARKETING_IMAGE"),
            (1088, 1088, "team-square", "SQUARE_MARKETING_IMAGE"),
            (1024, 1024, "logo-square", "BUSINESS_LOGO"),
            (1200, 1200, "company-logo", "BUSINESS_LOGO"),
            (1200, 628, "marketing-hero", "MARKETING_IMAGE"),
            (1200, 627, "marketing-hero", "MARKETING_IMAGE"),
            (1200, 300, "wordmark-logo", "LANDSCAPE_LOGO"),
            (480, 600, "vertical-photo", "PORTRAIT_MARKETING_IMAGE"),
            (480, 800, "tall-portrait", "TALL_PORTRAIT_MARKETING_IMAGE"),
            (1500, 800, "wide-photo", "MARKETING_IMAGE"),
            (1300, 1000, "near-square", "MARKETING_IMAGE"),
        ],
    )
    def test_aspect_ratio_picks_field_type(self, width, height, name, expected):
        result = write._detect_image_field_type({
            "width": width, "height": height, "name": name, "path": f"{name}.jpg",
        })
        assert result == expected

    def test_explicit_field_type_overrides_detection(self):
        result = write._detect_image_field_type({
            "width": 1200, "height": 628, "name": "x",
            "field_type": "SQUARE_MARKETING_IMAGE",
        })
        assert result == "SQUARE_MARKETING_IMAGE"

    def test_explicit_invalid_field_type_raises(self):
        with pytest.raises(ValueError, match="not a supported"):
            write._detect_image_field_type({
                "width": 1200, "height": 628, "field_type": "AD_IMAGE",
            })

    def test_zero_dims_returns_marketing_image(self):
        assert (
            write._detect_image_field_type({"width": 0, "height": 0})
            == "MARKETING_IMAGE"
        )

    def test_filename_logo_hint_only_applies_to_logo_friendly_ratios(self):
        # Wide non-4:1 image with 'logo' in name should NOT become LANDSCAPE_LOGO
        result = write._detect_image_field_type({
            "width": 1200, "height": 628, "name": "company-logo-hero",
        })
        assert result == "MARKETING_IMAGE"


class TestApplyCreateImageAssets:
    def _png_path(self, tmp_path):
        # Tiny 1x1 PNG so the apply layer can read bytes
        import base64

        p = tmp_path / "tiny.png"
        p.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2ZfZ0AAAAASUVORK5CYII="
            )
        )
        return str(p)

    def test_campaign_scope_uses_marketing_image_for_landscape(self, tmp_path):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result",
                "customers/1/campaignAssets/42~MARKETING_IMAGE",
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        write._apply_create_image_assets(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "images": [
                    {
                        "path": self._png_path(tmp_path),
                        "name": "marketing-hero",
                        "mime_type": "image/png",
                        "width": 1200,
                        "height": 628,
                    }
                ],
            },
        )
        link = google_ads.operations[1].campaign_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.MARKETING_IMAGE
        assert link.campaign == "customers/1/campaigns/42"

    def test_customer_scope_uses_business_logo_for_logo_named_square(self, tmp_path):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/-1~BUSINESS_LOGO"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        write._apply_create_image_assets(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "images": [
                    {
                        "path": self._png_path(tmp_path),
                        "name": "logo-square",
                        "mime_type": "image/png",
                        "width": 1024,
                        "height": 1024,
                    }
                ],
            },
        )
        link = google_ads.operations[1].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_LOGO
        # Should not have populated campaign_asset_operation
        assert (
            google_ads.operations[1].campaign_asset_operation.create.field_type
            == client.enums.AssetFieldTypeEnum.UNSPECIFIED
        )

    def test_explicit_field_type_override(self, tmp_path):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/-1~LOGO"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        write._apply_create_image_assets(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "images": [
                    {
                        "path": self._png_path(tmp_path),
                        "name": "weird-asset",
                        "mime_type": "image/png",
                        "width": 1200,
                        "height": 628,
                        "field_type": "LOGO",
                    }
                ],
            },
        )
        link = google_ads.operations[1].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.LOGO

    def test_apply_rejects_unknown_scope(self, tmp_path):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )
        with pytest.raises(ValueError, match="Unknown asset scope"):
            write._apply_create_image_assets(
                client,
                "1",
                {
                    "scope": "bogus",
                    "campaign_id": "",
                    "images": [
                        {
                            "path": self._png_path(tmp_path),
                            "name": "x",
                            "mime_type": "image/png",
                            "width": 1200,
                            "height": 628,
                        }
                    ],
                },
            )

    def test_apply_campaign_scope_requires_campaign_id(self, tmp_path):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )
        with pytest.raises(ValueError, match="campaign_id is required"):
            write._apply_create_image_assets(
                client,
                "1",
                {
                    "scope": "campaign",
                    "campaign_id": "",
                    "images": [
                        {
                            "path": self._png_path(tmp_path),
                            "name": "x",
                            "mime_type": "image/png",
                            "width": 1200,
                            "height": 628,
                        }
                    ],
                },
            )


class TestDraftImageAssets:
    def _png_path(self, tmp_path):
        import base64

        p = tmp_path / "tiny.png"
        p.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2ZfZ0AAAAASUVORK5CYII="
            )
        )
        return str(p)

    def test_no_campaign_id_uses_customer_scope(self, config, tmp_path):
        result = write.draft_image_assets(
            config,
            customer_id="1234567890",
            image_paths=[self._png_path(tmp_path)],
        )
        assert "error" not in result
        assert result["entity_type"] == "customer_asset"
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "customer"
        # Detection ran and stored the resolved field type in the preview
        assert plan.changes["images"][0]["resolved_field_type"] in {
            "BUSINESS_LOGO",
            "SQUARE_MARKETING_IMAGE",
            "MARKETING_IMAGE",
            "PORTRAIT_MARKETING_IMAGE",
            "TALL_PORTRAIT_MARKETING_IMAGE",
            "LOGO",
            "LANDSCAPE_LOGO",
        }

    def test_field_types_length_mismatch(self, config, tmp_path):
        result = write.draft_image_assets(
            config,
            customer_id="1234567890",
            image_paths=[self._png_path(tmp_path)],
            field_types=["MARKETING_IMAGE", "BUSINESS_LOGO"],
        )
        assert result["error"] == "Validation failed"
        assert any("field_types has 2" in d for d in result["details"])

    def test_invalid_override_field_type_rejected(self, config, tmp_path):
        result = write.draft_image_assets(
            config,
            customer_id="1234567890",
            image_paths=[self._png_path(tmp_path)],
            field_types=["AD_IMAGE"],  # AD_IMAGE not allowed for direct linking
        )
        assert result["error"] == "Validation failed"
        assert any("not a supported" in d for d in result["details"])


# ---------------------------------------------------------------------------
# draft_business_name_asset
# ---------------------------------------------------------------------------


class TestDraftBusinessNameAsset:
    def test_requires_business_name(self, config):
        result = write.draft_business_name_asset(config, customer_id="1")
        assert "business_name is required" in result["error"]

    def test_max_25_chars(self, config):
        result = write.draft_business_name_asset(
            config,
            customer_id="1",
            business_name="X" * 26,
        )
        assert result["error"] == "Validation failed"
        assert any("25" in d for d in result["details"])

    def test_customer_scope_default(self, config):
        result = write.draft_business_name_asset(
            config,
            customer_id="1",
            business_name="Modern Waste Solutions",  # 22 chars
        )
        assert "error" not in result
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "customer"
        assert plan.changes["business_name"] == "Modern Waste Solutions"

    def test_campaign_scope_when_campaign_id_provided(self, config):
        result = write.draft_business_name_asset(
            config,
            customer_id="1",
            campaign_id="42",
            business_name="MWS",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["scope"] == "campaign"
        assert plan.changes["campaign_id"] == "42"


class TestApplyCreateBusinessNameAsset:
    def test_customer_scope_creates_text_asset_and_customer_link(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "customer_asset_result", "customers/1/customerAssets/-1~BUSINESS_NAME"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        result = write._apply_create_business_name_asset(
            client,
            "1",
            {
                "scope": "customer",
                "campaign_id": "",
                "business_name": "Modern Waste Solutions",
            },
        )

        # Asset op: TEXT type with the business name
        asset_op = google_ads.operations[0].asset_operation.create
        assert asset_op.type_ == client.enums.AssetTypeEnum.TEXT
        assert asset_op.text_asset.text == "Modern Waste Solutions"
        # Link op: BUSINESS_NAME at customer scope
        link = google_ads.operations[1].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_NAME
        assert result["asset"] == "customers/1/assets/-1"

    def test_campaign_scope_creates_campaign_asset_link(self):
        responses = [
            _FakeMutateOperationResponse("asset_result", "customers/1/assets/-1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result", "customers/1/campaignAssets/42~BUSINESS_NAME"
            ),
        ]
        google_ads = _FakeGoogleAdsService(responses)
        client = _FakeClient(
            {
                "GoogleAdsService": google_ads,
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )

        write._apply_create_business_name_asset(
            client,
            "1",
            {
                "scope": "campaign",
                "campaign_id": "42",
                "business_name": "MWS",
            },
        )
        link = google_ads.operations[1].campaign_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_NAME
        assert link.campaign == "customers/1/campaigns/42"

    def test_campaign_scope_requires_campaign_id(self):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )
        with pytest.raises(ValueError, match="campaign_id required"):
            write._apply_create_business_name_asset(
                client,
                "1",
                {
                    "scope": "campaign",
                    "campaign_id": "",
                    "business_name": "MWS",
                },
            )

    def test_unknown_scope_raises(self):
        client = _FakeClient(
            {
                "GoogleAdsService": _FakeGoogleAdsService(),
                "AssetService": _FakePathService("assets"),
                "CampaignService": _FakePathService("campaigns"),
            }
        )
        with pytest.raises(ValueError, match="Unknown scope"):
            write._apply_create_business_name_asset(
                client,
                "1",
                {
                    "scope": "bogus",
                    "campaign_id": "",
                    "business_name": "MWS",
                },
            )


class TestServerToolRegistrationsImageAndBusinessName:
    @pytest.fixture
    def tools_by_name(self):
        import asyncio

        from adloop.server import mcp

        async def _list():
            return await mcp.list_tools()

        tools = asyncio.run(_list())
        return {t.name: t for t in tools}

    def test_draft_image_assets_now_optional_campaign_id(self, tools_by_name):
        tool = tools_by_name["draft_image_assets"]
        required = tool.parameters.get("required", [])
        assert "campaign_id" not in required
        assert "image_paths" in required
        assert "field_types" in tool.parameters["properties"]

    def test_draft_business_name_asset_registered(self, tools_by_name):
        assert "draft_business_name_asset" in tools_by_name
        tool = tools_by_name["draft_business_name_asset"]
        required = tool.parameters.get("required", [])
        assert "business_name" in required
        assert "campaign_id" not in required

    def test_dispatch_includes_business_name_op(self):
        import inspect

        src = inspect.getsource(write._execute_plan)
        assert (
            '"create_business_name_asset": _apply_create_business_name_asset' in src
        )
