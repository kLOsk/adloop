"""Promotion and price assets.

Based on the design and tests of PR #97 (Illia Sapryga).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.ads import assets
from adloop.ads.gaql import _extract_field
from adloop.ads.validate_only import ValidateOnlyClient
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_assets import _Client


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="1234567890"),
                        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")))


@pytest.fixture(autouse=True)
def _reachable():
    with patch("adloop.ads.write._validate_urls", return_value=({}, {})) as check:
        yield check


ACCOUNT = [{"customer.currency_code": "EUR", "customer.time_zone": "Europe/Berlin"}]


def _account(rows=None):
    return patch("adloop.ads.gaql.execute_query", return_value=rows if rows is not None else ACCOUNT)


PROMO = {"promotion_target": "Winterreifen", "final_url": "https://example.com/winter", "language_code": "de"}


# ---------------------------------------------------------------------------
# draft_promotion (review on #97: no USD and no "en" by default)
# ---------------------------------------------------------------------------


def test_money_amounts_use_the_account_currency_and_say_so(config):
    with _account() as read:
        preview = assets.draft_promotion(config, customer_id="1", campaign_id="1001", money_off=20, **PROMO)
    read.assert_called_once()
    assert preview["changes"]["promotion"]["currency_code"] == "EUR"
    assert "Amounts are in EUR, the account currency." in preview["warnings"]

    with _account():
        dollars = assets.draft_promotion(config, customer_id="1", campaign_id="1001", money_off=20,
                                         currency_code="usd", **PROMO)
    assert dollars["changes"]["promotion"]["currency_code"] == "USD"
    assert "the account bills in EUR" in dollars["warnings"][0]

    with _account() as read:
        percent = assets.draft_promotion(config, customer_id="1", ad_group_id="2002", percent_off=15, **PROMO)
    read.assert_not_called()  # no amount, no currency needed
    assert percent["entity_type"] == "ad_group_asset" and percent["changes"]["promotion"]["currency_code"] == ""


@pytest.mark.parametrize("extra,needle", [
    ({"language_code": ""}, "language_code is required"),
    ({"language_code": "German"}, "not a language tag"),
    ({"money_off": 5, "percent_off": 10}, "not both"),
    ({}, "money_off or percent_off is required"),
    ({"percent_off": 120}, "at most 100"),
    ({"percent_off": 10, "promotion_code": "WINTER", "orders_over_amount": 50}, "one trigger"),
    ({"percent_off": 10, "promotion_target": "Winterreifen und Felgen"}, "max 20"),
    ({"percent_off": 10, "promotion_code": "WINTERREIFEN2026"}, "max 15"),
    ({"percent_off": 10, "occasion": "OKTOBERFEST"}, "is not one of"),
    ({"percent_off": 10, "discount_modifier": "AT_LEAST"}, "UP_TO"),
    ({"percent_off": 10, "start_date": "2026-12-01", "end_date": "2026-11-01"}, "is before"),
    ({"percent_off": 10, "end_date": "1.12.2026"}, "YYYY-MM-DD"),
])
def test_promotion_refusals(config, extra, needle):
    result = assets.draft_promotion(config, customer_id="1", campaign_id="1001", **{**PROMO, **extra})
    assert needle in " ".join(result["details"])


def test_past_end_dates_warn_and_unreachable_pages_refuse(config, _reachable):
    preview = assets.draft_promotion(config, customer_id="1", campaign_id="1001", percent_off=10,
                                     end_date="2020-01-31", **PROMO)
    assert any("in the past" in w for w in preview["warnings"])
    _reachable.return_value = ({"https://example.com/winter": "404"}, {})
    dead = assets.draft_promotion(config, customer_id="1", campaign_id="1001", percent_off=10, **PROMO)
    assert "not reachable" in dead["details"][0]


@pytest.mark.parametrize("percent,expected", [(10, 100_000), (15.5, 155_000), (100, 1_000_000)])
def test_percent_off_units_on_the_real_proto(percent, expected):
    client = _Client()
    promo = {**PROMO, "percent_off": percent, "money_off": 0, "currency_code": "", "promotion_code": "",
             "orders_over_amount": 0, "occasion": "", "discount_modifier": "",
             **dict.fromkeys(assets._DATE_FIELDS, "")}
    assets._apply_create_promotion(client, "1", {"scope": "campaign", "campaign_id": "1001", "promotion": promo})
    created = client.ads.operations[0].asset_operation.create
    assert created.promotion_asset.percent_off == expected
    assert created.promotion_asset.language_code == "de"
    assert list(created.final_urls) == ["https://example.com/winter"]


def test_money_is_sent_as_rounded_micros_in_the_plans_currency():
    client = _Client()
    promo = {**PROMO, "percent_off": 0, "money_off": 19.99, "currency_code": "EUR", "promotion_code": "",
             "orders_over_amount": 100, "occasion": "BLACK_FRIDAY", "discount_modifier": "UP_TO",
             **dict.fromkeys(assets._DATE_FIELDS, "")}
    assets._apply_create_promotion(client, "1", {"scope": "account", "promotion": promo})
    p = client.ads.operations[0].asset_operation.create.promotion_asset
    assert (p.money_amount_off.amount_micros, p.money_amount_off.currency_code) == (19_990_000, "EUR")
    assert p.orders_over_amount.amount_micros == 100_000_000
    assert p.occasion == client.enums.PromotionExtensionOccasionEnum.BLACK_FRIDAY
    assert client.ads.operations[1]._pb.WhichOneof("operation") == "customer_asset_operation"


# ---------------------------------------------------------------------------
# update_promotion (review on #97: in place, nothing dropped)
# ---------------------------------------------------------------------------


def _promo_row(client, *, percent=None, money=None, code="", end="", occasion=None):
    row = client.get_type("GoogleAdsRow")
    row.asset.id = 7
    p = row.asset.promotion_asset
    p.promotion_target = "Winterreifen"
    if percent is not None:
        p.percent_off = percent
    if money is not None:
        p.money_amount_off.amount_micros = money
        p.money_amount_off.currency_code = "EUR"
    if code:
        p.promotion_code = code
    if occasion:
        p.occasion = getattr(client.enums.PromotionExtensionOccasionEnum, occasion)
    p.language_code = "de"
    p.end_date = end
    row.asset.final_urls.append("https://example.com/winter")
    return row


def _as_gaql(row):
    """The row as the draft's GAQL read returns it."""
    return [{"asset.id": 7, "asset.type": "PROMOTION",
             **{field: _extract_field(row, field) for field in assets._PROMOTION_FIELDS.values()}}]


def test_an_update_keeps_every_field_it_is_not_given(config):
    client = _Client()
    row = _promo_row(client, percent=100_000, code="WINTER", end="2026-12-31", occasion="WINTER_SALE")
    with patch("adloop.ads.gaql.execute_query", return_value=_as_gaql(row)) as read:
        preview = assets.update_promotion(config, customer_id="1", asset_id="7", percent_off=15)
    read.assert_called_once()  # the asset only; no currency needed for a percentage
    changes = preview["changes"]
    assert changes["set"] == {"percent_off": 15.0}
    assert changes["previous"] == {"percent_off": 10.0}
    kept = changes["promotion"]
    assert (kept["promotion_code"], kept["end_date"], kept["occasion"]) == ("WINTER", "2026-12-31", "WINTER_SALE")
    assert preview["requires_double_confirm"] is False and "In-place edit" in preview["warnings"][0]
    assert set(changes["pinned"]) == set(assets._PROMOTION_FIELDS)

    client.ads.rows = [row]
    assets._apply_update_promotion(client, "1", changes)
    op = client.assets.operations[0]
    assert list(op.update_mask.paths) == ["promotion_asset.percent_off"]
    assert op.update.promotion_asset.percent_off == 150_000


def test_switching_the_discount_and_clearing_the_code(config):
    client = _Client()
    row = _promo_row(client, percent=100_000, code="WINTER")
    with patch("adloop.ads.gaql.execute_query", side_effect=[_as_gaql(row), ACCOUNT]):
        preview = assets.update_promotion(config, customer_id="1", asset_id="7", money_off=25,
                                          clear_fields=["promotion_code"])
    changes = preview["changes"]
    assert changes["set"] == {"money_off": 25.0, "percent_off": 0.0, "currency_code": "EUR", "promotion_code": ""}

    client.ads.rows = [row]
    assets._apply_update_promotion(client, "1", changes)
    op = client.assets.operations[0]
    assert set(op.update_mask.paths) == {"promotion_asset.money_amount_off", "promotion_asset.promotion_code"}
    assert op.update.promotion_asset.money_amount_off.amount_micros == 25_000_000
    assert op.update.promotion_asset.promotion_code == ""  # masked and unset: removed


def test_update_refusals_and_the_moved_asset_check(config):
    client = _Client()
    row = _promo_row(client, percent=100_000)
    with patch("adloop.ads.gaql.execute_query", return_value=_as_gaql(row)):
        same = assets.update_promotion(config, customer_id="1", asset_id="7", percent_off=10)
        wrong_clear = assets.update_promotion(config, customer_id="1", asset_id="7", clear_fields=["promotion_target"])
        both = assets.update_promotion(config, customer_id="1", asset_id="7", promotion_code="X",
                                       clear_fields=["promotion_code"])
        preview = assets.update_promotion(config, customer_id="1", asset_id="7", end_date="2026-12-24")
    assert "nothing to change" in same["error"]
    assert "cannot be cleared" in wrong_clear["details"][0]
    assert "also passed" in both["details"][0]
    assert assets.update_promotion(config, customer_id="1", asset_id="7")["details"][0].startswith("No changes")

    edited = _Client(rows=[_promo_row(client, percent=200_000)])
    with pytest.raises(RuntimeError, match="changed in Google Ads after this preview"):
        assets._apply_update_promotion(edited, "1", preview["changes"])
    assert edited.assets.operations is None


# ---------------------------------------------------------------------------
# draft_price_asset (review on #97: no currency, language or qualifier defaults)
# ---------------------------------------------------------------------------


def _offerings(n=3, **over):
    return [{"header": f"Paket {i}", "description": "Pro Monat", "price": 49 + i,
             "final_url": "https://example.com/preise", "unit": "PER_MONTH", **over} for i in range(n)]


def test_price_asset_uses_the_account_currency_and_no_default_qualifier(config):
    with _account():
        preview = assets.draft_price_asset(config, customer_id="1", campaign_id="1001", price_type="services",
                                           language_code="de", offerings=_offerings())
    price = preview["changes"]["price"]
    assert (price["currency_code"], price["price_qualifier"], price["price_type"]) == ("EUR", "", "SERVICES")
    assert any("differ from the landing page" in w for w in preview["warnings"])

    client = _Client()
    assets._apply_create_price_asset(client, "1", preview["changes"])
    p = client.ads.operations[0].asset_operation.create.price_asset
    assert p.price_qualifier == client.enums.PriceExtensionPriceQualifierEnum.UNSPECIFIED
    assert [o.price.amount_micros for o in p.price_offerings] == [49_000_000, 50_000_000, 51_000_000]
    assert {o.price.currency_code for o in p.price_offerings} == {"EUR"}
    assert p.price_offerings[0].unit == client.enums.PriceExtensionPriceUnitEnum.PER_MONTH


@pytest.mark.parametrize("kwargs,needle", [
    ({"price_type": ""}, "price_type is required"),
    ({"language_code": ""}, "language_code is required"),
    ({"price_qualifier": "ABOUT"}, "FROM, UP_TO, AVERAGE"),
    ({"offerings": _offerings(2)}, "3-8 offerings"),
    ({"offerings": _offerings(9)}, "3-8 offerings"),
    ({"offerings": _offerings(header="Basis")}, "used twice"),
    ({"offerings": _offerings(price=0)}, "greater than 0"),
    ({"offerings": _offerings(unit="PER_DECADE")}, "unit must be one of"),
    ({"offerings": _offerings(description="Monatlich kündbar, ohne Einrichtung")}, "max 25"),
])
def test_price_asset_refusals(config, kwargs, needle):
    args = {"price_type": "SERVICES", "language_code": "de", "offerings": _offerings(), **kwargs}
    result = assets.draft_price_asset(config, customer_id="1", campaign_id="1001", **args)
    assert needle in " ".join(result["details"])


# ---------------------------------------------------------------------------
# Dry runs
# ---------------------------------------------------------------------------


def test_promotion_and_price_applies_run_validate_only(config):
    client = _Client()
    row = _promo_row(client, percent=100_000)
    with patch("adloop.ads.gaql.execute_query", return_value=_as_gaql(row)):
        update = assets.update_promotion(config, customer_id="1", asset_id="7", percent_off=20)["changes"]
    with _account():
        price = assets.draft_price_asset(config, customer_id="1", ad_group_id="2002", price_type="BRANDS",
                                         language_code="de", offerings=_offerings())["changes"]
        create = assets.draft_promotion(config, customer_id="1", scope="account", money_off=5, **PROMO)["changes"]
    for apply, changes in ((assets._apply_update_promotion, update), (assets._apply_create_price_asset, price),
                           (assets._apply_create_promotion, create)):
        fake = _Client(rows=[row])
        apply(ValidateOnlyClient(fake), "1", changes)
        assert fake.sent() and all(request.validate_only for request in fake.sent()), apply.__name__
