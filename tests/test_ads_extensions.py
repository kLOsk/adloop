"""Tests for asset-extension write tools (call/price/promotion/location/etc.).

Covers preview validation and the apply-layer mutate operations for the
asset-extension tools, including the campaign/ad-group/customer multi-scope
paths. All data here is synthetic — placeholder phone numbers (555-01xx),
placeholder business names, and example.com URLs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


# ---------------------------------------------------------------------------
# Fakes — mirror the lightweight harness in tests/test_ads_write.py
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, resource_name: str = ""):
        self.resource_name = resource_name


class _FakeMutateOperationResponse:
    def __init__(self, response_type: str | None = None, resource_name: str = ""):
        self.asset_result = _FakeResult()
        self.campaign_asset_result = _FakeResult()
        self.ad_group_asset_result = _FakeResult()
        self.customer_asset_result = _FakeResult()
        if response_type:
            getattr(self, response_type).resource_name = resource_name


class _FakePathService:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def campaign_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/campaigns/{entity_id}"

    def ad_group_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/adGroups/{entity_id}"

    def asset_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/assets/{entity_id}"

    def conversion_action_path(self, customer_id: str, entity_id: str) -> str:
        return f"customers/{customer_id}/conversionActions/{entity_id}"


class _FakeGoogleAdsService(_FakePathService):
    def __init__(self, responses: list[_FakeMutateOperationResponse] | None = None):
        super().__init__("googleAds")
        self.operations = None
        self._responses = responses or []
        self.search_queries: list[str] = []
        self.search_rows: list[object] = []

    def mutate(self, customer_id: str, mutate_operations: list[object]) -> object:
        self.operations = mutate_operations
        return SimpleNamespace(mutate_operation_responses=self._responses)

    def search(self, customer_id: str, query: str) -> list[object]:
        self.search_queries.append(query)
        return self.search_rows


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


class _FakeAssetService(_FakePathService):
    def __init__(self):
        super().__init__("assets")
        self.operations = None

    def mutate_assets(self, customer_id: str, operations: list[object]) -> object:
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/assets/{i}"
                )
                for i, _ in enumerate(operations)
            ]
        )


class _FakeLinkService:
    """Generic fake for CampaignAsset / CustomerAsset / AdGroupAsset services."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.operations = None

    def _mutate(self, customer_id, operations):
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/{self.prefix}/{i}"
                )
                for i, _ in enumerate(operations)
            ]
        )

    mutate_campaign_assets = _mutate
    mutate_customer_assets = _mutate
    mutate_ad_group_assets = _mutate


class _FakeCustomerAssetService:
    def __init__(self):
        self.operations = None

    def mutate_customer_assets(
        self, customer_id: str, operations: list[object]
    ) -> object:
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/customerAssets/{i}"
                )
                for i, _ in enumerate(operations)
            ]
        )


class _FakeAssetSetService:
    def __init__(self):
        self.operations = None

    def mutate_asset_sets(self, customer_id: str, operations: list[object]) -> object:
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/assetSets/1"
                )
            ]
        )


class _FakeAssetSetLinkService:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.operations = None

    def _mutate(self, customer_id, operations):
        self.operations = operations
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    resource_name=f"customers/{customer_id}/{self.prefix}/1"
                )
            ]
        )

    mutate_customer_asset_sets = _mutate
    mutate_campaign_asset_sets = _mutate


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


def _asset_link_client(responses):
    google_ads_service = _FakeGoogleAdsService(responses)
    return google_ads_service, _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "AssetService": _FakePathService("assets"),
            "CampaignService": _FakePathService("campaigns"),
            "ConversionActionService": _FakePathService("conversionActions"),
        }
    )


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


def test_normalize_phone_us_national():
    normalized, err = write._normalize_phone_e164("(555) 555-0142", "US")
    assert err is None
    assert normalized == "+15555550142"


def test_normalize_phone_strips_leading_country_code():
    normalized, err = write._normalize_phone_e164("1-555-555-0143", "US")
    assert err is None
    assert normalized == "+15555550143"


def test_normalize_phone_already_e164_passthrough():
    normalized, err = write._normalize_phone_e164("+445555550144", "GB")
    assert err is None
    assert normalized == "+445555550144"


def test_normalize_phone_strips_european_trunk_zero():
    normalized, err = write._normalize_phone_e164("020 5550 0145", "GB")
    assert err is None
    # exactly one trunk "0" removed (not every leading zero)
    assert normalized == "+442055500145"


def test_normalize_phone_double_zero_international_prefix():
    # "00" is the international access prefix; the following digits already
    # carry the country code — must become "+44...", not "+44 00...".
    normalized, err = write._normalize_phone_e164("0044 20 5550 0145", "GB")
    assert err is None
    assert normalized == "+442055500145"


def test_normalize_phone_italy_keeps_leading_zero():
    # Italy is the exception — the trunk "0" is part of the E.164 number and
    # must NOT be stripped.
    normalized, err = write._normalize_phone_e164("06 5550 0145", "IT")
    assert err is None
    assert normalized == "+390655500145"


def test_normalize_phone_unknown_country_code_errors():
    normalized, err = write._normalize_phone_e164("5555550146", "ZZ")
    assert normalized == ""
    assert "dial-code map" in err


# ---------------------------------------------------------------------------
# draft_call_asset — preview + scope
# ---------------------------------------------------------------------------


def test_draft_call_asset_requires_phone(config):
    result = write.draft_call_asset(config, customer_id="123-456-7890")
    assert result["error"] == "phone_number is required"


def test_draft_call_asset_campaign_scope(config):
    result = write.draft_call_asset(
        config,
        customer_id="123-456-7890",
        phone_number="(555) 555-0142",
        campaign_id="1001",
    )
    assert result["operation"] == "create_call_asset"
    assert result["changes"]["scope"] == "campaign"
    assert result["changes"]["phone_number"] == "+15555550142"
    assert result["warnings"]


def test_draft_call_asset_ad_group_scope_wins(config):
    result = write.draft_call_asset(
        config,
        customer_id="123-456-7890",
        phone_number="+15555550142",
        campaign_id="1001",
        ad_group_id="2002",
    )
    assert result["changes"]["scope"] == "ad_group"
    assert result["entity_type"] == "ad_group_asset"
    assert result["entity_id"] == "2002"


def test_draft_call_asset_customer_scope_default(config):
    result = write.draft_call_asset(
        config,
        customer_id="123-456-7890",
        phone_number="+15555550142",
    )
    assert result["changes"]["scope"] == "customer"
    assert result["entity_type"] == "customer_asset"


def test_draft_call_asset_rejects_bad_schedule(config):
    result = write.draft_call_asset(
        config,
        customer_id="123-456-7890",
        phone_number="+15555550142",
        ad_schedule=[{"day_of_week": "FUNDAY", "start_hour": 9, "end_hour": 17}],
    )
    assert result["error"] == "Ad schedule validation failed"


def test_apply_create_call_asset_campaign_scope_links_call_field():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
        _FakeMutateOperationResponse(
            "campaign_asset_result", "customers/1234567890/campaignAssets/1"
        ),
    ]
    google_ads_service, client = _asset_link_client(responses)

    write._apply_create_call_asset(
        client,
        "1234567890",
        {
            "scope": "campaign",
            "campaign_id": "1001",
            "ad_group_id": "",
            "phone_number": "+15555550142",
            "country_code": "US",
            "call_conversion_action_id": "",
            "ad_schedule": [],
        },
    )
    create = google_ads_service.operations[0].asset_operation.create
    assert create.call_asset.phone_number == "+15555550142"
    link = google_ads_service.operations[1].campaign_asset_operation.create
    assert link.field_type == client.enums.AssetFieldTypeEnum.CALL


def test_apply_create_call_asset_ad_group_scope_and_conversion_action():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
        _FakeMutateOperationResponse(
            "ad_group_asset_result", "customers/1234567890/adGroupAssets/1"
        ),
    ]
    google_ads_service, client = _asset_link_client(responses)

    write._apply_create_call_asset(
        client,
        "1234567890",
        {
            "scope": "ad_group",
            "campaign_id": "",
            "ad_group_id": "2002",
            "phone_number": "+15555550142",
            "country_code": "US",
            "call_conversion_action_id": "555999",
            "ad_schedule": [
                {
                    "day_of_week": "MONDAY",
                    "start_hour": 9,
                    "start_minute": 0,
                    "end_hour": 17,
                    "end_minute": 30,
                }
            ],
        },
    )
    create = google_ads_service.operations[0].asset_operation.create
    assert create.call_asset.call_conversion_action.endswith("conversionActions/555999")
    assert (
        create.call_asset.call_conversion_reporting_state
        == client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
    )
    assert len(create.call_asset.ad_schedule_targets) == 1
    link = google_ads_service.operations[1].ad_group_asset_operation.create
    assert link.field_type == client.enums.AssetFieldTypeEnum.CALL


# ---------------------------------------------------------------------------
# add_ad_schedule
# ---------------------------------------------------------------------------


def test_add_ad_schedule_requires_campaign(config):
    result = write.add_ad_schedule(
        config,
        customer_id="123-456-7890",
        schedule=[{"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 17}],
    )
    assert result["error"] == "campaign_id is required"


def test_add_ad_schedule_rejects_bad_minute(config):
    result = write.add_ad_schedule(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        schedule=[
            {
                "day_of_week": "MONDAY",
                "start_hour": 9,
                "start_minute": 10,
                "end_hour": 17,
            }
        ],
    )
    assert result["error"] == "Validation failed"
    assert any("start_minute" in d for d in result["details"])


def test_add_ad_schedule_rejects_end_before_start(config):
    result = write.add_ad_schedule(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        schedule=[
            {"day_of_week": "MONDAY", "start_hour": 17, "end_hour": 9}
        ],
    )
    assert result["error"] == "Validation failed"
    assert any("must be after" in d for d in result["details"])


def test_add_ad_schedule_preview_ok(config):
    result = write.add_ad_schedule(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        schedule=[
            {
                "day_of_week": "monday",
                "start_hour": 9,
                "start_minute": 15,
                "end_hour": 17,
                "end_minute": 45,
            }
        ],
    )
    assert result["operation"] == "add_ad_schedule"
    assert result["changes"]["schedule"][0]["day_of_week"] == "MONDAY"


def test_apply_add_ad_schedule_builds_criteria():
    crit_service = _FakeCampaignCriterionService()
    client = _FakeClient(
        {
            "CampaignService": _FakePathService("campaigns"),
            "CampaignCriterionService": crit_service,
        }
    )
    result = write._apply_add_ad_schedule(
        client,
        "1234567890",
        {
            "campaign_id": "1001",
            "schedule": [
                {
                    "day_of_week": "MONDAY",
                    "start_hour": 9,
                    "start_minute": 0,
                    "end_hour": 17,
                    "end_minute": 0,
                }
            ],
        },
    )
    assert len(result["campaign_criteria"]) == 1
    crit = crit_service.operations[0].create
    assert crit.ad_schedule.day_of_week == client.enums.DayOfWeekEnum.MONDAY
    assert crit.ad_schedule.start_hour == 9


# ---------------------------------------------------------------------------
# draft_promotion
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_urls(monkeypatch):
    monkeypatch.setattr(
        "adloop.ads.write._validate_urls",
        lambda urls, timeout=10: {u: None for u in urls},
    )


def test_draft_promotion_requires_one_discount(config, stub_urls):
    result = write.draft_promotion(
        config,
        customer_id="123-456-7890",
        promotion_target="Spring Sale",
        final_url="https://example.com/promo",
        campaign_id="1001",
    )
    assert result["error"] == "Validation failed"
    assert any("money_off or percent_off" in d for d in result["details"])


def test_draft_promotion_rejects_both_discounts(config, stub_urls):
    result = write.draft_promotion(
        config,
        customer_id="123-456-7890",
        promotion_target="Spring Sale",
        final_url="https://example.com/promo",
        money_off=10,
        percent_off=15,
        campaign_id="1001",
    )
    assert result["error"] == "Validation failed"
    assert any("not both" in d for d in result["details"])


def test_draft_promotion_rejects_long_target(config, stub_urls):
    result = write.draft_promotion(
        config,
        customer_id="123-456-7890",
        promotion_target="This target is far too long for a promo",
        final_url="https://example.com/promo",
        percent_off=20,
        campaign_id="1001",
    )
    assert result["error"] == "Validation failed"
    assert any("max 20" in d for d in result["details"])


def test_draft_promotion_rejects_code_and_orders_over(config, stub_urls):
    result = write.draft_promotion(
        config,
        customer_id="123-456-7890",
        promotion_target="Spring Sale",
        final_url="https://example.com/promo",
        percent_off=20,
        promotion_code="SAVE20",
        orders_over_amount=50,
        campaign_id="1001",
    )
    assert result["error"] == "Validation failed"
    assert any("mutually exclusive" in d for d in result["details"])


def test_draft_promotion_ad_group_scope(config, stub_urls):
    result = write.draft_promotion(
        config,
        customer_id="123-456-7890",
        promotion_target="Spring Sale",
        final_url="https://example.com/promo",
        percent_off=20,
        campaign_id="1001",
        ad_group_id="2002",
    )
    assert result["operation"] == "create_promotion"
    assert result["changes"]["scope"] == "ad_group"
    assert result["changes"]["promotion"]["percent_off"] == 20.0


def test_apply_create_promotion_money_off_campaign_scope():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
        _FakeMutateOperationResponse(
            "campaign_asset_result", "customers/1234567890/campaignAssets/1"
        ),
    ]
    google_ads_service, client = _asset_link_client(responses)

    write._apply_create_promotion(
        client,
        "1234567890",
        {
            "scope": "campaign",
            "campaign_id": "1001",
            "ad_group_id": "",
            "promotion": {
                "promotion_target": "Spring Sale",
                "final_url": "https://example.com/promo",
                "currency_code": "USD",
                "money_off": 25.0,
                "percent_off": 0.0,
                "promotion_code": "",
                "orders_over_amount": 0.0,
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
    create = google_ads_service.operations[0].asset_operation.create
    assert create.promotion_asset.promotion_target == "Spring Sale"
    assert create.promotion_asset.money_amount_off.amount_micros == 25_000_000
    assert create.final_urls[0] == "https://example.com/promo"
    link = google_ads_service.operations[1].campaign_asset_operation.create
    assert link.field_type == client.enums.AssetFieldTypeEnum.PROMOTION


def test_apply_update_promotion_swaps_and_unlinks_old():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/9"),
        _FakeMutateOperationResponse(
            "campaign_asset_result", "customers/1234567890/campaignAssets/9"
        ),
    ]
    google_ads_service = _FakeGoogleAdsService(responses)
    google_ads_service.search_rows = [
        SimpleNamespace(
            campaign_asset=SimpleNamespace(
                resource_name="customers/1234567890/campaignAssets/OLD"
            )
        )
    ]
    ca_link_service = _FakeLinkService("campaignAssets")
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "AssetService": _FakePathService("assets"),
            "CampaignService": _FakePathService("campaigns"),
            "CampaignAssetService": ca_link_service,
        }
    )

    result = write._apply_update_promotion(
        client,
        "1234567890",
        {
            "scope": "campaign",
            "campaign_id": "1001",
            "old_asset_id": "555",
            "promotion": {
                "promotion_target": "Summer Sale",
                "final_url": "https://example.com/promo",
                "currency_code": "USD",
                "money_off": 0.0,
                "percent_off": 30.0,
                "promotion_code": "",
                "orders_over_amount": 0.0,
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
    assert result["new_asset"].endswith("assets/9")
    assert result["old_link_removed"].endswith("campaignAssets/OLD")
    assert ca_link_service.operations[0].remove.endswith("campaignAssets/OLD")


# ---------------------------------------------------------------------------
# draft_price_asset
# ---------------------------------------------------------------------------


def _offerings(n=3):
    return [
        {
            "header": f"Service {i}",
            "description": f"Desc {i}",
            "price": 100 + i,
            "final_url": f"https://example.com/service-{i}",
        }
        for i in range(n)
    ]


def test_draft_price_asset_requires_three_offerings(config, stub_urls):
    result = write.draft_price_asset(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        offerings=_offerings(2),
    )
    assert result["error"] == "Validation failed"
    assert any("between 3 and 8" in d for d in result["details"])


def test_draft_price_asset_rejects_duplicate_headers(config, stub_urls):
    rows = _offerings(3)
    rows[1]["header"] = rows[0]["header"]
    result = write.draft_price_asset(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        offerings=rows,
    )
    assert result["error"] == "Validation failed"
    assert any("duplicate header" in d for d in result["details"])


def test_draft_price_asset_rejects_bad_type(config, stub_urls):
    result = write.draft_price_asset(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        price_type="NOPE",
        offerings=_offerings(3),
    )
    assert result["error"] == "Validation failed"
    assert any("price_type" in d for d in result["details"])


def test_draft_price_asset_customer_scope(config, stub_urls):
    result = write.draft_price_asset(
        config,
        customer_id="123-456-7890",
        offerings=_offerings(3),
    )
    assert result["operation"] == "create_price_asset"
    assert result["changes"]["scope"] == "customer"
    assert len(result["changes"]["price"]["offerings"]) == 3


def test_apply_create_price_asset_builds_offerings():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
        _FakeMutateOperationResponse(
            "ad_group_asset_result", "customers/1234567890/adGroupAssets/1"
        ),
    ]
    google_ads_service, client = _asset_link_client(responses)

    write._apply_create_price_asset(
        client,
        "1234567890",
        {
            "scope": "ad_group",
            "campaign_id": "",
            "ad_group_id": "2002",
            "price": {
                "price_type": "SERVICES",
                "price_qualifier": "FROM",
                "language_code": "en",
                "currency_code": "USD",
                "offerings": [
                    {
                        "header": "Service A",
                        "description": "Desc A",
                        "price": 149.0,
                        "final_url": "https://example.com/a",
                        "final_mobile_url": "",
                        "unit": "",
                    },
                    {
                        "header": "Service B",
                        "description": "Desc B",
                        "price": 199.0,
                        "final_url": "https://example.com/b",
                        "final_mobile_url": "",
                        "unit": "PER_HOUR",
                    },
                    {
                        "header": "Service C",
                        "description": "Desc C",
                        "price": 249.0,
                        "final_url": "https://example.com/c",
                        "final_mobile_url": "",
                        "unit": "",
                    },
                ],
            },
        },
    )
    create = google_ads_service.operations[0].asset_operation.create
    assert create.price_asset.type_ == client.enums.PriceExtensionTypeEnum.SERVICES
    assert len(create.price_asset.price_offerings) == 3
    assert create.price_asset.price_offerings[0].price.amount_micros == 149_000_000
    assert (
        create.price_asset.price_offerings[1].unit
        == client.enums.PriceExtensionPriceUnitEnum.PER_HOUR
    )
    link = google_ads_service.operations[1].ad_group_asset_operation.create
    assert link.field_type == client.enums.AssetFieldTypeEnum.PRICE


# ---------------------------------------------------------------------------
# In-place update tools: callout / sitelink / call asset
# ---------------------------------------------------------------------------


def test_update_callout_requires_asset_id(config):
    result = write.update_callout(config, customer_id="123-456-7890", callout_text="Hi")
    assert result["error"] == "asset_id is required"


def test_update_callout_rejects_long_text(config):
    result = write.update_callout(
        config,
        customer_id="123-456-7890",
        asset_id="55",
        callout_text="This callout text is definitely far too long",
    )
    assert result["error"] == "Validation failed"


def test_update_callout_preview(config):
    result = write.update_callout(
        config, customer_id="123-456-7890", asset_id="55", callout_text="Free Wi-Fi"
    )
    assert result["operation"] == "update_callout"
    assert result["changes"]["callout_text"] == "Free Wi-Fi"


def test_apply_update_callout_sets_field_mask():
    asset_service = _FakeAssetService()
    client = _FakeClient({"AssetService": asset_service})
    result = write._apply_update_callout(
        client, "1234567890", {"asset_id": "55", "callout_text": "Free Wi-Fi"}
    )
    assert result["resource_name"].endswith("assets/0")
    op = asset_service.operations[0]
    assert op.update.callout_asset.callout_text == "Free Wi-Fi"
    assert list(op.update_mask.paths) == ["callout_asset.callout_text"]


def test_update_sitelink_rejects_empty_change(config):
    result = write.update_sitelink(config, customer_id="123-456-7890", asset_id="55")
    assert result["error"] == "No fields to update"


def test_update_sitelink_preview(config, stub_urls):
    result = write.update_sitelink(
        config,
        customer_id="123-456-7890",
        asset_id="55",
        link_text="Pricing",
        final_url="https://example.com/pricing",
    )
    assert result["operation"] == "update_sitelink"
    assert result["changes"]["link_text"] == "Pricing"


def test_apply_update_sitelink_partial_mask():
    asset_service = _FakeAssetService()
    client = _FakeClient({"AssetService": asset_service})
    write._apply_update_sitelink(
        client,
        "1234567890",
        {
            "asset_id": "55",
            "link_text": "Pricing",
            "final_url": "https://example.com/pricing",
        },
    )
    op = asset_service.operations[0]
    assert op.update.sitelink_asset.link_text == "Pricing"
    assert op.update.final_urls[0] == "https://example.com/pricing"
    assert set(op.update_mask.paths) == {"sitelink_asset.link_text", "final_urls"}


def test_update_call_asset_rejects_empty_change(config):
    result = write.update_call_asset(config, customer_id="123-456-7890", asset_id="55")
    assert result["error"] == "No fields to update"


def test_update_call_asset_rejects_bad_reporting_state(config):
    result = write.update_call_asset(
        config,
        customer_id="123-456-7890",
        asset_id="55",
        call_conversion_reporting_state="NONSENSE",
    )
    assert result["error"] == "Validation failed"


def test_update_call_asset_preview_normalizes_phone(config):
    result = write.update_call_asset(
        config,
        customer_id="123-456-7890",
        asset_id="55",
        phone_number="(555) 555-0142",
        country_code="US",
    )
    assert result["changes"]["phone_number"] == "+15555550142"
    assert result["changes"]["country_code"] == "US"


def test_apply_update_call_asset_builds_field_mask():
    asset_service = _FakeAssetService()
    client = _FakeClient(
        {
            "AssetService": asset_service,
            "ConversionActionService": _FakePathService("conversionActions"),
        }
    )
    write._apply_update_call_asset(
        client,
        "1234567890",
        {
            "asset_id": "55",
            "phone_number": "+15555550142",
            "call_conversion_action_id": "999",
            "call_conversion_reporting_state": "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
        },
    )
    op = asset_service.operations[0]
    assert op.update.call_asset.phone_number == "+15555550142"
    assert op.update.call_asset.call_conversion_action.endswith(
        "conversionActions/999"
    )
    assert set(op.update_mask.paths) == {
        "call_asset.phone_number",
        "call_asset.call_conversion_action",
        "call_asset.call_conversion_reporting_state",
    }


# ---------------------------------------------------------------------------
# update_structured_snippet (swap)
# ---------------------------------------------------------------------------


def test_update_structured_snippet_requires_asset_id(config):
    result = write.update_structured_snippet(
        config,
        customer_id="123-456-7890",
        header="Brands",
        values=["A", "B", "C"],
    )
    assert "asset_id is required" in result["error"]


def test_update_structured_snippet_rejects_bad_header(config):
    result = write.update_structured_snippet(
        config,
        customer_id="123-456-7890",
        asset_id="55",
        header="Nope",
        values=["A", "B", "C"],
    )
    assert result["error"] == "Validation failed"


def test_update_structured_snippet_ad_group_scope_preview(config):
    result = write.update_structured_snippet(
        config,
        customer_id="123-456-7890",
        asset_id="55",
        campaign_id="1001",
        ad_group_id="2002",
        header="Services",
        values=["Repair", "Install", "Maintain"],
    )
    assert result["operation"] == "update_structured_snippet"
    assert result["changes"]["scope"] == "ad_group"
    assert result["changes"]["old_asset_id"] == "55"


def test_apply_update_structured_snippet_swaps_ad_group_link():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/9"),
        _FakeMutateOperationResponse(
            "ad_group_asset_result", "customers/1234567890/adGroupAssets/9"
        ),
    ]
    google_ads_service = _FakeGoogleAdsService(responses)
    google_ads_service.search_rows = [
        SimpleNamespace(
            ad_group_asset=SimpleNamespace(
                resource_name="customers/1234567890/adGroupAssets/OLD"
            )
        )
    ]
    aga_service = _FakeLinkService("adGroupAssets")
    client = _FakeClient(
        {
            "GoogleAdsService": google_ads_service,
            "AssetService": _FakePathService("assets"),
            "CampaignService": _FakePathService("campaigns"),
            "AdGroupAssetService": aga_service,
        }
    )

    result = write._apply_update_structured_snippet(
        client,
        "1234567890",
        {
            "scope": "ad_group",
            "campaign_id": "1001",
            "ad_group_id": "2002",
            "old_asset_id": "55",
            "snippet": {"header": "Services", "values": ["Repair", "Install", "Maintain"]},
        },
    )
    create = google_ads_service.operations[0].asset_operation.create
    assert create.structured_snippet_asset.header == "Services"
    assert list(create.structured_snippet_asset.values) == [
        "Repair",
        "Install",
        "Maintain",
    ]
    assert result["old_link_removed"].endswith("adGroupAssets/OLD")
    assert aga_service.operations[0].remove.endswith("adGroupAssets/OLD")


# ---------------------------------------------------------------------------
# draft_business_name_asset
# ---------------------------------------------------------------------------


def test_draft_business_name_requires_name(config):
    result = write.draft_business_name_asset(config, customer_id="123-456-7890")
    assert result["error"] == "business_name is required"


def test_draft_business_name_rejects_long_name(config):
    result = write.draft_business_name_asset(
        config,
        customer_id="123-456-7890",
        business_name="A Business Name That Is Way Too Long",
    )
    assert result["error"] == "Validation failed"


def test_draft_business_name_campaign_scope(config):
    result = write.draft_business_name_asset(
        config,
        customer_id="123-456-7890",
        campaign_id="1001",
        business_name="Example Plumbing",
    )
    assert result["operation"] == "create_business_name_asset"
    assert result["changes"]["scope"] == "campaign"


def test_apply_create_business_name_asset_customer_scope():
    responses = [
        _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
        _FakeMutateOperationResponse(
            "customer_asset_result", "customers/1234567890/customerAssets/1"
        ),
    ]
    google_ads_service, client = _asset_link_client(responses)

    result = write._apply_create_business_name_asset(
        client,
        "1234567890",
        {"scope": "customer", "campaign_id": "", "business_name": "Example Plumbing"},
    )
    create = google_ads_service.operations[0].asset_operation.create
    assert create.text_asset.text == "Example Plumbing"
    assert create.type_ == client.enums.AssetTypeEnum.TEXT
    link = google_ads_service.operations[1].customer_asset_operation.create
    assert link.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_NAME
    assert result["link"].endswith("customerAssets/1")


# ---------------------------------------------------------------------------
# draft_location_asset
# ---------------------------------------------------------------------------


def test_draft_location_asset_requires_gbp_id(config):
    result = write.draft_location_asset(config, customer_id="123-456-7890")
    assert "business_profile_account_id is required" in result["error"]


def test_draft_location_asset_default_name_and_warning(config):
    result = write.draft_location_asset(
        config,
        customer_id="123-456-7890",
        business_profile_account_id="9988776655",
    )
    assert result["operation"] == "create_location_asset"
    assert result["changes"]["asset_set_name"] == "GBP Locations - 9988776655"
    assert result["changes"]["scope"] == "customer"
    assert result["warnings"]


def test_apply_create_location_asset_customer_scope():
    asset_set_service = _FakeAssetSetService()
    cas_service = _FakeAssetSetLinkService("customerAssetSets")
    client = _FakeClient(
        {
            "AssetSetService": asset_set_service,
            "CustomerAssetSetService": cas_service,
        }
    )
    result = write._apply_create_location_asset(
        client,
        "1234567890",
        {
            "scope": "customer",
            "campaign_id": "",
            "business_profile_account_id": "9988776655",
            "asset_set_name": "GBP Locations - 9988776655",
            "label_filters": ["storefront"],
            "listing_id_filters": ["12345"],
        },
    )
    set_create = asset_set_service.operations[0].create
    assert set_create.type_ == client.enums.AssetSetTypeEnum.LOCATION_SYNC
    bpls = set_create.location_set.business_profile_location_set
    assert bpls.business_account_id == "9988776655"
    assert list(bpls.label_filters) == ["storefront"]
    assert list(bpls.listing_id_filters) == [12345]
    assert result["asset_set"].endswith("assetSets/1")
    assert result["customer_asset_set"].endswith("customerAssetSets/1")


def test_apply_create_location_asset_campaign_scope():
    asset_set_service = _FakeAssetSetService()
    cas_service = _FakeAssetSetLinkService("campaignAssetSets")
    client = _FakeClient(
        {
            "AssetSetService": asset_set_service,
            "CampaignAssetSetService": cas_service,
            "CampaignService": _FakePathService("campaigns"),
        }
    )
    result = write._apply_create_location_asset(
        client,
        "1234567890",
        {
            "scope": "campaign",
            "campaign_id": "1001",
            "business_profile_account_id": "9988776655",
            "asset_set_name": "GBP Locations",
            "label_filters": [],
            "listing_id_filters": [],
        },
    )
    assert result["campaign_asset_set"].endswith("campaignAssetSets/1")
    link = cas_service.operations[0].create
    assert link.campaign.endswith("campaigns/1001")


# ---------------------------------------------------------------------------
# link_asset_to_customer
# ---------------------------------------------------------------------------


def test_link_asset_requires_links(config):
    result = write.link_asset_to_customer(config, customer_id="123-456-7890")
    assert result["error"] == "At least one link is required"


def test_link_asset_rejects_non_numeric_id(config):
    result = write.link_asset_to_customer(
        config,
        customer_id="123-456-7890",
        links=[{"asset_id": "abc", "field_type": "BUSINESS_LOGO"}],
    )
    assert result["error"] == "Validation failed"
    assert any("must be numeric" in d for d in result["details"])


def test_link_asset_rejects_bad_field_type(config):
    result = write.link_asset_to_customer(
        config,
        customer_id="123-456-7890",
        links=[{"asset_id": "555", "field_type": "NOT_A_TYPE"}],
    )
    assert result["error"] == "Validation failed"
    assert any("not valid for" in d for d in result["details"])


def test_link_asset_preview_ok(config):
    result = write.link_asset_to_customer(
        config,
        customer_id="123-456-7890",
        links=[{"asset_id": "555", "field_type": "business_logo"}],
    )
    assert result["operation"] == "link_asset_to_customer"
    assert result["changes"]["links"][0]["field_type"] == "BUSINESS_LOGO"


def test_apply_link_asset_to_customer_builds_links():
    cust_service = _FakeCustomerAssetService()
    client = _FakeClient(
        {
            "AssetService": _FakePathService("assets"),
            "CustomerAssetService": cust_service,
        }
    )
    result = write._apply_link_asset_to_customer(
        client,
        "1234567890",
        {
            "links": [
                {"asset_id": "555", "field_type": "BUSINESS_LOGO"},
                {"asset_id": "666", "field_type": "MARKETING_IMAGE"},
            ]
        },
    )
    assert result["linked_count"] == 2
    op0 = cust_service.operations[0].create
    assert op0.asset.endswith("assets/555")
    assert op0.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_LOGO
