"""Promotion and price assets (adloop.ads.assets).

Covers the unit scales Google expects (percent_off in 1/10,000 of a percent,
money in micros), field validation, the explicit account-scope opt-in, the
batched update swap, dispatch registration and the validate-only dry run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import assets, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import ChangePlan, store_plan

CID = "1234567890"


# ---------------------------------------------------------------------------
# Fakes: a real client for enums and proto types, fake services for I/O
# ---------------------------------------------------------------------------


class _AnyResult:
    """A MutateOperationResponse stand-in answering every ``*_result`` field."""

    def __init__(self, index: int):
        self._index = index

    def __getattr__(self, attr):
        if attr.endswith("_result"):
            return SimpleNamespace(resource_name=f"customers/{CID}/{attr}/{self._index}")
        raise AttributeError(attr)


class FakeGoogleAdsService:
    def __init__(self, search_rows=None):
        self.search_rows = list(search_rows or [])
        self.queries: list[str] = []
        self.mutations: list[list] = []
        self.requests: list = []

    def campaign_path(self, cid, campaign_id):
        return f"customers/{cid}/campaigns/{campaign_id}"

    def ad_group_path(self, cid, ad_group_id):
        return f"customers/{cid}/adGroups/{ad_group_id}"

    def search(self, customer_id, query):
        self.queries.append(query)
        return list(self.search_rows)

    def mutate(self, request=None, *, customer_id=None, mutate_operations=None):
        if request is not None:
            self.requests.append(request)
            ops = list(request.mutate_operations)
        else:
            ops = list(mutate_operations)
        self.mutations.append(ops)
        return SimpleNamespace(
            mutate_operation_responses=[_AnyResult(i) for i in range(len(ops))],
            partial_failure_error=None,
        )


class FakeAssetService:
    def asset_path(self, cid, asset_id):
        return f"customers/{cid}/assets/{asset_id}"


class FakeClient:
    def __init__(self, search_rows=None):
        base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.googleads = FakeGoogleAdsService(search_rows)
        self._services = {
            "GoogleAdsService": self.googleads,
            "AssetService": FakeAssetService(),
        }

    def get_service(self, name, *args, **kwargs):
        return self._services[name]


@pytest.fixture(autouse=True)
def fresh_plan_store():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture(autouse=True)
def reachable_urls(monkeypatch):
    """URL checks are network calls; every URL is reachable unless a test says so."""
    monkeypatch.setattr(
        write, "_validate_urls", lambda urls, timeout=10: ({u: None for u in urls}, {})
    )


@pytest.fixture
def config(tmp_path):
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False),
    )


def _promo(**overrides):
    base = {
        "promotion_target": "Window Tint",
        "final_url": "https://example.com/tint",
        "money_off": 0,
        "percent_off": 15,
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
    }
    base.update(overrides)
    return base


def _offerings(n=3, **overrides):
    rows = []
    for i in range(n):
        row = {
            "header": f"Service {i}",
            "description": "Same-day visit",
            "price": 99 + i,
            "final_url": f"https://example.com/s{i}",
        }
        row.update(overrides)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Unit scales (reviewer blocker: percent_off)
# ---------------------------------------------------------------------------


class TestPromotionScales:
    """v25 PromotionAsset.percent_off: 1,000,000 = 100%. Money is micros."""

    @pytest.mark.parametrize(
        "pct,expected", [(10, 100_000), (15.5, 155_000), (100, 1_000_000), (0.01, 100)]
    )
    def test_percent_off_is_sent_in_ten_thousandths_of_a_percent(self, pct, expected):
        from google.ads.googleads.v25.resources.types.asset import Asset

        asset = Asset()
        assets._populate_promotion_asset(
            object(), asset, {**_promo(), "percent_off": pct, "money_off": 0}
        )
        assert asset.promotion_asset.percent_off == expected
        assert asset.promotion_asset.money_amount_off.amount_micros == 0

    @pytest.mark.parametrize(
        "amount,expected", [(100, 100_000_000), (19.99, 19_990_000), (0.5, 500_000)]
    )
    def test_money_off_is_sent_in_micros(self, amount, expected):
        from google.ads.googleads.v25.resources.types.asset import Asset

        asset = Asset()
        assets._populate_promotion_asset(
            object(), asset, {**_promo(), "money_off": amount, "percent_off": 0, "currency_code": "EUR"}
        )
        assert asset.promotion_asset.money_amount_off.amount_micros == expected
        assert asset.promotion_asset.money_amount_off.currency_code == "EUR"
        assert asset.promotion_asset.percent_off == 0

    def test_orders_over_amount_is_micros_and_other_fields_are_set(self):
        client = FakeClient()
        asset = client.get_type("Asset")
        assets._populate_promotion_asset(
            client,
            asset,
            _promo(
                orders_over_amount=250,
                occasion="BLACK_FRIDAY",
                discount_modifier="UP_TO",
                start_date="2026-11-20",
                end_date="2026-11-30",
            ),
        )
        p = asset.promotion_asset
        assert p.orders_over_amount.amount_micros == 250_000_000
        assert p.occasion == client.enums.PromotionExtensionOccasionEnum.BLACK_FRIDAY
        assert p.discount_modifier == client.enums.PromotionExtensionDiscountModifierEnum.UP_TO
        assert (p.start_date, p.end_date) == ("2026-11-20", "2026-11-30")
        assert list(asset.final_urls) == ["https://example.com/tint"]

    def test_price_offerings_are_micros_with_enums(self):
        client = FakeClient()
        asset = client.get_type("Asset")
        assets._populate_price_asset(
            client,
            asset,
            {
                "price_type": "SERVICES",
                "price_qualifier": "FROM",
                "language_code": "en",
                "currency_code": "USD",
                "offerings": [
                    {"header": "Drain", "description": "Any drain", "price": 49.99,
                     "final_url": "https://example.com/d", "final_mobile_url": "", "unit": "PER_HOUR"},
                ],
            },
        )
        p = asset.price_asset
        assert p.type_ == client.enums.PriceExtensionTypeEnum.SERVICES
        assert p.price_qualifier == client.enums.PriceExtensionPriceQualifierEnum.FROM
        [offering] = list(p.price_offerings)
        assert offering.price.amount_micros == 49_990_000
        assert offering.price.currency_code == "USD"
        assert offering.unit == client.enums.PriceExtensionPriceUnitEnum.PER_HOUR


# ---------------------------------------------------------------------------
# Promotion validation
# ---------------------------------------------------------------------------


def _validate_promo(**overrides):
    return assets._validate_promotion_inputs(**_promo(**overrides))


class TestPromotionValidation:
    def test_percent_happy_path(self):
        normalized, errors, _ = _validate_promo()
        assert errors == []
        assert (normalized["percent_off"], normalized["money_off"]) == (15.0, 0.0)

    def test_money_happy_path(self):
        normalized, errors, _ = _validate_promo(money_off=50, percent_off=0, currency_code="eur")
        assert errors == []
        assert (normalized["money_off"], normalized["percent_off"]) == (50.0, 0.0)
        assert normalized["currency_code"] == "EUR"

    @pytest.mark.parametrize(
        "overrides,message",
        [
            ({"money_off": 10, "percent_off": 10}, "exactly one of money_off or percent_off"),
            ({"money_off": 0, "percent_off": 0}, "One of money_off or percent_off is required"),
            ({"percent_off": 120}, "percent_off must be in (0, 100]"),
            ({"percent_off": -5}, "percent_off must not be negative"),
            ({"percent_off": "lots"}, "percent_off must be a number"),
            ({"promotion_target": ""}, "promotion_target is required"),
            ({"promotion_target": "x" * 21}, "(max 20)"),
            ({"final_url": ""}, "final_url is required"),
            ({"promotion_code": "C" * 16}, "(max 15)"),
            ({"promotion_code": "SAVE", "orders_over_amount": 100}, "mutually exclusive"),
            ({"occasion": "TAX_DAY"}, "occasion 'TAX_DAY' invalid"),
            ({"discount_modifier": "AT_LEAST"}, "discount_modifier 'AT_LEAST' invalid"),
            ({"currency_code": "US"}, "3-letter ISO 4217"),
            ({"start_date": "11/20/2026"}, "start_date '11/20/2026' must be YYYY-MM-DD"),
            ({"redemption_end_date": "2026-13-01"}, "must be YYYY-MM-DD"),
            ({"start_date": "2026-12-01", "end_date": "2026-11-01"}, "end_date (2026-11-01) is before start_date"),
            (
                {"redemption_start_date": "2026-12-01", "redemption_end_date": "2026-11-01"},
                "redemption_end_date (2026-11-01) is before redemption_start_date",
            ),
        ],
    )
    def test_rejections(self, overrides, message):
        normalized, errors, _ = _validate_promo(**overrides)
        assert normalized == {}
        assert any(message in e for e in errors), errors

    def test_occasion_and_modifier_are_normalized(self):
        normalized, errors, _ = _validate_promo(occasion="black_friday", discount_modifier="up_to")
        assert errors == []
        assert (normalized["occasion"], normalized["discount_modifier"]) == ("BLACK_FRIDAY", "UP_TO")

    def test_past_end_date_warns(self):
        _, errors, warnings = _validate_promo(end_date="2020-01-01")
        assert errors == []
        assert any("in the past" in w for w in warnings)

    def test_unreachable_url_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            write, "_validate_urls", lambda urls, timeout=10: ({u: "HTTP 404" for u in urls}, {})
        )
        _, errors, _ = _validate_promo()
        assert errors == ["final_url 'https://example.com/tint' is not reachable: HTTP 404"]

    def test_inconclusive_url_check_is_a_warning(self, monkeypatch):
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: ({u: None for u in urls}, {u: "HTTP 503" for u in urls}),
        )
        _, errors, warnings = _validate_promo()
        assert errors == []
        assert any("HTTP 503" in w for w in warnings)


# ---------------------------------------------------------------------------
# Price validation
# ---------------------------------------------------------------------------


def _validate_price(offerings, **overrides):
    kwargs = {
        "price_type": "SERVICES",
        "price_qualifier": "FROM",
        "language_code": "en",
        "currency_code": "USD",
        "offerings": offerings,
    }
    kwargs.update(overrides)
    return assets._validate_price_inputs(**kwargs)


class TestPriceValidation:
    def test_happy_path(self):
        normalized, errors, _ = _validate_price(_offerings(3, unit="per_hour"))
        assert errors == []
        assert len(normalized["offerings"]) == 3
        assert normalized["offerings"][0]["unit"] == "PER_HOUR"

    @pytest.mark.parametrize("n", [0, 2, 9])
    def test_offering_count_limits(self, n):
        _, errors, _ = _validate_price(_offerings(n))
        assert any("3-8 offerings" in e for e in errors), errors

    def test_eight_offerings_allowed(self):
        _, errors, _ = _validate_price(_offerings(8))
        assert errors == []

    @pytest.mark.parametrize(
        "row_overrides,message",
        [
            ({"header": "H" * 26}, "header"),
            ({"description": "D" * 26}, "description"),
            ({"description": ""}, "description is required"),
            ({"price": 0}, "price must be > 0"),
            ({"price": "free"}, "price must be a number"),
            ({"final_url": ""}, "final_url is required"),
            ({"unit": "PER_MINUTE"}, "unit 'PER_MINUTE' invalid"),
        ],
    )
    def test_offering_rejections(self, row_overrides, message):
        rows = _offerings(3)
        rows[1].update(row_overrides)
        _, errors, _ = _validate_price(rows)
        assert any(message in e for e in errors), errors

    def test_duplicate_headers_rejected_case_insensitively(self):
        rows = _offerings(3)
        rows[2]["header"] = rows[0]["header"].upper()
        _, errors, _ = _validate_price(rows)
        assert any("duplicate header" in e for e in errors), errors

    def test_invalid_type_and_qualifier(self):
        _, errors, _ = _validate_price(_offerings(3), price_type="CARS", price_qualifier="ABOUT")
        assert any("price_type 'CARS' invalid" in e for e in errors)
        assert any("price_qualifier 'ABOUT' invalid" in e for e in errors)

    def test_empty_qualifier_means_none(self):
        normalized, errors, _ = _validate_price(_offerings(3), price_qualifier="")
        assert errors == []
        assert normalized["price_qualifier"] == ""


# ---------------------------------------------------------------------------
# Scope: ad group / campaign, account only by explicit opt-in
# ---------------------------------------------------------------------------


def _draft(kind, config, **scope_kwargs):
    if kind == "promotion":
        return assets.draft_promotion(config, customer_id=CID, **_promo(), **scope_kwargs)
    return assets.draft_price_asset(config, customer_id=CID, offerings=_offerings(3), **scope_kwargs)


@pytest.mark.parametrize("kind", ["promotion", "price"])
class TestScope:
    def test_campaign_link(self, kind, config):
        preview = _draft(kind, config, campaign_id="42")
        assert preview["entity_type"] == "campaign_asset"
        assert preview["entity_id"] == "42"
        assert preview["changes"]["scope"] == "campaign"

    def test_ad_group_link(self, kind, config):
        preview = _draft(kind, config, ad_group_id="77")
        assert preview["entity_type"] == "ad_group_asset"
        assert preview["entity_id"] == "77"
        assert preview["changes"]["scope"] == "ad_group"

    def test_empty_ids_are_not_account_level(self, kind, config):
        preview = _draft(kind, config)
        assert "plan_id" not in preview
        assert any("scope='account'" in d for d in preview["details"])

    def test_account_level_needs_the_explicit_opt_in(self, kind, config):
        preview = _draft(kind, config, scope="account")
        assert preview["entity_type"] == "customer_asset"
        assert preview["entity_id"] == CID
        assert preview["changes"]["scope"] == "account"

    @pytest.mark.parametrize(
        "scope_kwargs,message",
        [
            ({"scope": "account", "campaign_id": "42"}, "leave campaign_id and ad_group_id empty"),
            ({"campaign_id": "42", "ad_group_id": "77"}, "not both"),
            ({"campaign_id": "42 OR 1=1"}, "campaign_id must be a numeric ID"),
            ({"ad_group_id": "abc"}, "ad_group_id must be a numeric ID"),
            ({"scope": "customer"}, "scope 'customer' invalid"),
            ({"scope": "campaign", "ad_group_id": "77"}, "does not match"),
        ],
    )
    def test_rejections(self, kind, config, scope_kwargs, message):
        preview = _draft(kind, config, **scope_kwargs)
        assert "plan_id" not in preview
        assert any(message in d for d in preview["details"]), preview

    def test_blocked_operation(self, kind, config):
        op = "create_promotion" if kind == "promotion" else "create_price_asset"
        config.safety.blocked_operations = [op]
        preview = _draft(kind, config, campaign_id="42")
        assert "blocked" in preview["error"]


def test_price_preview_warns_about_landing_page_prices(config):
    preview = _draft("price", config, campaign_id="42")
    assert any("landing page" in w for w in preview["warnings"])


# ---------------------------------------------------------------------------
# update_promotion draft
# ---------------------------------------------------------------------------


class TestUpdatePromotionDraft:
    def _patch_rows(self, monkeypatch, rows):
        queries = []

        def fake(_config, _cid, query):
            queries.append(query)
            return rows

        monkeypatch.setattr("adloop.ads.gaql.execute_query", fake)
        return queries

    def test_found_link_produces_a_double_confirm_swap_plan(self, config, monkeypatch):
        queries = self._patch_rows(monkeypatch, [{
            "campaign_asset.resource_name": f"customers/{CID}/campaignAssets/42~99~PROMOTION",
            "asset.promotion_asset.promotion_target": "Old Tint",
        }])
        preview = assets.update_promotion(
            config, customer_id=CID, asset_id="99", campaign_id="42", **_promo()
        )
        assert preview["requires_double_confirm"] is True
        assert preview["operation"] == "update_promotion"
        changes = preview["changes"]
        assert changes["old_asset_id"] == "99"
        assert changes["old_link"].endswith("42~99~PROMOTION")
        assert changes["old_promotion_target"] == "Old Tint"
        assert changes["promotion"]["percent_off"] == 15.0
        assert any("Swap" in w for w in preview["warnings"])
        [query] = queries
        assert "asset.id = 99" in query and "campaign.id = 42" in query
        assert "campaign_asset.status != 'REMOVED'" in query

    @pytest.mark.parametrize(
        "scope_kwargs,resource",
        [({"ad_group_id": "77"}, "FROM ad_group_asset"), ({"scope": "account"}, "FROM customer_asset")],
    )
    def test_query_follows_the_scope(self, config, monkeypatch, scope_kwargs, resource):
        queries = self._patch_rows(monkeypatch, [])
        assets.update_promotion(config, customer_id=CID, asset_id="99", **scope_kwargs, **_promo())
        assert resource in queries[0]

    def test_missing_link_is_refused(self, config, monkeypatch):
        self._patch_rows(monkeypatch, [])
        preview = assets.update_promotion(
            config, customer_id=CID, asset_id="99", campaign_id="42", **_promo()
        )
        assert "plan_id" not in preview
        assert "no active PROMOTION link on campaign 42" in preview["error"]

    @pytest.mark.parametrize(
        "kwargs,message",
        [
            ({"asset_id": ""}, "asset_id is required"),
            ({"asset_id": "99 OR 1=1"}, "asset_id must be a numeric ID"),
            ({"asset_id": "99", "ad_group_id": "7 OR 1=1"}, "ad_group_id must be a numeric ID"),
            ({"asset_id": "99"}, "scope='account'"),
        ],
    )
    def test_bad_ids_never_reach_gaql(self, config, monkeypatch, kwargs, message):
        queries = self._patch_rows(monkeypatch, [])
        preview = assets.update_promotion(config, customer_id=CID, **kwargs, **_promo())
        assert "plan_id" not in preview
        text = preview["error"] + " ".join(preview.get("details", []))
        assert message in text
        assert queries == []


# ---------------------------------------------------------------------------
# Apply paths
# ---------------------------------------------------------------------------


def _changes(scope, **extra):
    base = {"scope": scope, "campaign_id": "", "ad_group_id": ""}
    if scope == "campaign":
        base["campaign_id"] = "42"
    elif scope == "ad_group":
        base["ad_group_id"] = "77"
    base.update(extra)
    return base


class TestApplyCreate:
    @pytest.mark.parametrize(
        "scope,operation_field,target_field,target",
        [
            ("campaign", "campaign_asset_operation", "campaign", f"customers/{CID}/campaigns/42"),
            ("ad_group", "ad_group_asset_operation", "ad_group", f"customers/{CID}/adGroups/77"),
            ("account", "customer_asset_operation", None, None),
        ],
    )
    def test_promotion_is_created_and_linked_in_one_mutate(
        self, scope, operation_field, target_field, target
    ):
        client = FakeClient()
        promo, _, _ = _validate_promo()
        result = assets._apply_create_promotion(client, CID, _changes(scope, promotion=promo))

        [ops] = client.googleads.mutations
        assert len(ops) == 2
        created = ops[0].asset_operation.create
        assert created.resource_name == f"customers/{CID}/assets/-1"
        assert created.promotion_asset.percent_off == 150_000
        link = getattr(ops[1], operation_field).create
        assert link.asset == f"customers/{CID}/assets/-1"
        assert link.field_type == client.enums.AssetFieldTypeEnum.PROMOTION
        if target_field:
            assert getattr(link, target_field) == target
        assert result["scope"] == scope
        assert result["asset"] and result["link"]

    def test_price_asset_is_created_and_linked(self):
        client = FakeClient()
        price, _, _ = _validate_price(_offerings(3))
        assets._apply_create_price_asset(client, CID, _changes("ad_group", price=price))

        [ops] = client.googleads.mutations
        created = ops[0].asset_operation.create
        assert len(created.price_asset.price_offerings) == 3
        assert created.price_asset.price_offerings[0].price.amount_micros == 99_000_000
        link = ops[1].ad_group_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.PRICE


def _old_link_row(scope):
    field = {"campaign": "campaign_asset", "ad_group": "ad_group_asset", "account": "customer_asset"}[scope]
    return SimpleNamespace(**{field: SimpleNamespace(resource_name=f"customers/{CID}/{field}/old")})


class TestApplyUpdate:
    @pytest.mark.parametrize(
        "scope,operation_field",
        [
            ("campaign", "campaign_asset_operation"),
            ("ad_group", "ad_group_asset_operation"),
            ("account", "customer_asset_operation"),
        ],
    )
    def test_create_link_and_unlink_go_in_one_mutate(self, scope, operation_field):
        client = FakeClient(search_rows=[_old_link_row(scope)])
        promo, _, _ = _validate_promo(money_off=40, percent_off=0)
        result = assets._apply_update_promotion(
            client, CID, _changes(scope, old_asset_id="99", promotion=promo)
        )

        [ops] = client.googleads.mutations
        assert len(ops) == 3
        assert ops[0].asset_operation.create.promotion_asset.money_amount_off.amount_micros == 40_000_000
        assert getattr(ops[1], operation_field).create.asset == f"customers/{CID}/assets/-1"
        old = f"customers/{CID}/{operation_field.removesuffix('_operation')}/old"
        assert getattr(ops[2], operation_field).remove == old
        assert result["old_link_removed"] == old
        assert result["new_asset"] and result["new_link"]
        [query] = client.googleads.queries
        assert "asset.id = 99" in query

    def test_missing_old_link_sends_nothing(self):
        client = FakeClient(search_rows=[])
        promo, _, _ = _validate_promo()
        with pytest.raises(ValueError, match="no longer has an active PROMOTION link"):
            assets._apply_update_promotion(
                client, CID, _changes("campaign", old_asset_id="99", promotion=promo)
            )
        assert client.googleads.mutations == []


# ---------------------------------------------------------------------------
# Dispatch + validate-only dry run
# ---------------------------------------------------------------------------


def _stored_plan(operation, scope="campaign", **extra):
    entity_type = {"campaign": "campaign_asset", "ad_group": "ad_group_asset", "account": "customer_asset"}[scope]
    plan = ChangePlan(
        operation=operation,
        entity_type=entity_type,
        entity_id="42",
        customer_id=CID,
        changes=_changes(scope, **extra),
    )
    store_plan(plan)
    return plan


def _promotion_plan():
    promo, _, _ = _validate_promo()
    return _stored_plan("create_promotion", promotion=promo)


def _price_plan():
    price, _, _ = _validate_price(_offerings(3))
    return _stored_plan("create_price_asset", scope="ad_group", price=price)


def _update_plan():
    promo, _, _ = _validate_promo()
    return _stored_plan("update_promotion", old_asset_id="99", promotion=promo)


@pytest.mark.parametrize("make_plan", [_promotion_plan, _price_plan, _update_plan])
def test_operations_are_registered_in_dispatch(make_plan):
    client = FakeClient(search_rows=[_old_link_row("campaign")])
    write._dispatch_ads_plan(client, CID, make_plan())
    assert len(client.googleads.mutations) == 1


@pytest.fixture
def real_validation(monkeypatch):
    """Undo the suite's offline stub; the dry run builds a ValidateOnlyClient
    around the fake client handed in."""
    monkeypatch.setattr(
        write,
        "_validate_with_google",
        lambda config, plan: write._execute_plan(config, plan, validate_only=True),
    )

    def use(fake):
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: fake)

    return use


@pytest.mark.parametrize(
    "make_plan,op_count",
    [(_promotion_plan, 2), (_price_plan, 2), (_update_plan, 3)],
)
def test_dry_run_sends_one_validate_only_mutate(config, real_validation, make_plan, op_count):
    fake = FakeClient(search_rows=[_old_link_row("campaign")])
    real_validation(fake)
    plan = make_plan()

    result = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=True)

    assert result["status"] == "DRY_RUN_SUCCESS", result
    # Temp resource names (-1) stay inside one request, so Google validates
    # the whole batch — nothing is skipped.
    assert result["checks"] == {"validated_calls": 1, "skipped_calls": 0}
    [request] = fake.googleads.requests
    assert request.validate_only is True
    assert len(request.mutate_operations) == op_count


def test_update_dry_run_fails_when_the_old_link_is_gone(config, real_validation):
    fake = FakeClient(search_rows=[])
    real_validation(fake)
    plan = _update_plan()

    result = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=True)

    assert result["status"] == "DRY_RUN_FAILED"
    assert "no longer has an active PROMOTION link" in result["error"]
    assert fake.googleads.requests == []


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_are_registered_with_scope_opt_in():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    for name, required in {
        "draft_promotion": {"promotion_target", "final_url"},
        "update_promotion": {"asset_id", "promotion_target", "final_url"},
        "draft_price_asset": {"offerings"},
    }.items():
        tool = tools[name]
        assert set(tool.parameters.get("required", [])) == required, name
        props = tool.parameters["properties"]
        assert {"scope", "campaign_id", "ad_group_id"} <= set(props), name
        assert "account" in props["scope"]["description"], name
    assert tools["update_promotion"].annotations.destructive_hint is True
    assert tools["draft_promotion"].annotations.destructive_hint is False
