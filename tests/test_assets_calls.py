"""Call assets and campaign ad schedules.

Based on the design and tests of PR #96 (Illia Sapryga).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.ads import assets, write
from adloop.ads.gaql import _to_python
from adloop.ads.validate_only import ValidateOnlyClient
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_assets import _Client, _gaql


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="1234567890"),
                        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")))


def _window(day="MONDAY", sh=9, eh=17, sm=0, em=0):
    return {"day_of_week": day, "start_hour": sh, "end_hour": eh, "start_minute": sm, "end_minute": em}


def _google_windows(client, *windows):
    """Windows as Google returns them (AdScheduleInfo protos)."""
    out = []
    for w in windows:
        info = client.get_type("AdScheduleInfo")
        assets._populate_schedule(client, info, w)
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Phone numbers (review on #96: the "(0)" form and mismatched countries)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,cc,expected", [
    ("+44 (0)20 7946 0018", "GB", "+442079460018"),
    ("020 7946 0018", "gb", "+442079460018"),
    ("0044 20 7946 0018", "GB", "+442079460018"),
    ("030 12345678", "DE", "+493012345678"),
    ("+49 (0)30 12345678", "DE", "+493012345678"),
    ("06 1234 5678", "IT", "+390612345678"),  # Italy keeps its leading 0
    ("(916) 555-0142", "US", "+19165550142"),
])
def test_phone_numbers_are_read_like_google_reads_them(raw, cc, expected):
    assert assets._phone_e164(raw, cc) == (expected, None)


@pytest.mark.parametrize("raw,cc,needle", [
    ("+49 30 1234567", "SE", "country_code SE dials +46"),
    ("030 1234567", "", "country_code is required"),
    ("030 1234567", "Germany", "not a 2-letter ISO country code"),
    ("916 555 014", "US", "not a valid US number"),
    ("call us", "DE", "cannot be read"),
])
def test_phone_number_refusals(raw, cc, needle):
    assert needle in assets._phone_e164(raw, cc)[1]


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window,needle", [
    ({**_window(), "start_hour": 9.7}, "whole numbers"),
    ({**_window(), "start_hour": True}, "whole numbers"),
    ({"day_of_week": "MONDAY", "end_hour": 17}, "whole numbers"),
    ({**_window(), "start_minute": 10}, "0, 15, 30 or 45"),
    ({**_window(), "end_hour": 24, "end_minute": 15}, "end_hour 24 takes end_minute 0"),
    (_window(sh=17, eh=9), "must be after start"),
    ({**_window(), "day_of_week": "Montag"}, "day_of_week must be one of"),
])
def test_schedule_windows_are_refused_rather_than_rounded(window, needle):
    assert needle in " ".join(assets._validate_schedule([window])[1])


def test_schedule_accepts_whole_floats_and_digit_strings_and_checks_overlaps():
    ok, errors = assets._validate_schedule([{**_window(), "start_hour": 9.0, "end_hour": "17"}])
    assert errors == [] and ok[0]["start_hour"] == 9 and ok[0]["end_hour"] == 17
    _, overlap = assets._validate_schedule([_window(), _window(sh=16, eh=20)])
    assert "overlaps" in overlap[0]
    _, too_many = assets._validate_schedule([_window(sh=h, eh=h + 1) for h in range(7)])
    assert "at most 6 a day" in too_many[0]


# ---------------------------------------------------------------------------
# draft_call_asset
# ---------------------------------------------------------------------------


def test_call_asset_follows_the_scope_rule_and_normalises_the_number(config):
    preview = assets.draft_call_asset(config, customer_id="1", phone_number="030 12345678",
                                      country_code="de", ad_group_id="2002")
    assert (preview["entity_type"], preview["entity_id"]) == ("ad_group_asset", "2002")
    assert preview["changes"]["phone_number"] == "+493012345678"
    assert preview["changes"]["country_code"] == "DE"
    assert "reviews the phone number" in preview["warnings"][0]

    nowhere = assets.draft_call_asset(config, customer_id="1", phone_number="030 12345678", country_code="DE")
    assert "scope='account'" in nowhere["details"][0]
    with _gaql([{"customer.currency_code": "EUR", "customer.time_zone": "Europe/Berlin"}]):
        account = assets.draft_call_asset(config, customer_id="1", phone_number="030 12345678",
                                          country_code="DE", scope="account", ad_schedule=[_window()])
    assert account["entity_type"] == "customer_asset"
    assert any("Europe/Berlin" in w for w in account["warnings"])


def test_call_asset_apply_creates_and_links_in_one_request():
    client = _Client()
    assets._apply_create_call_asset(client, "1", {
        "scope": "campaign", "campaign_id": "1001", "phone_number": "+493012345678", "country_code": "DE",
        "call_conversion_action_id": "55", "ad_schedule": [_window(), _window("FRIDAY", 9, 13, 30, 45)]})
    create, link = client.ads.operations
    call = create.asset_operation.create.call_asset
    assert (call.phone_number, call.country_code) == ("+493012345678", "DE")
    assert call.call_conversion_action.endswith("/55")
    assert call.call_conversion_reporting_state == client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
    assert call.ad_schedule_targets[1].start_minute == client.enums.MinuteOfHourEnum.THIRTY
    assert link.campaign_asset_operation.create.field_type == client.enums.AssetFieldTypeEnum.CALL


# ---------------------------------------------------------------------------
# update_call_asset: in place, pinned, re-checked at apply
# ---------------------------------------------------------------------------


def _call_row(client, phone="+493012345678", cc="DE", action="", state="UNSPECIFIED", windows=()):
    return {"asset.id": 7, "asset.type": "CALL", "asset.call_asset.phone_number": phone,
            "asset.call_asset.country_code": cc, "asset.call_asset.call_conversion_action": action,
            "asset.call_asset.call_conversion_reporting_state": state,
            "asset.call_asset.ad_schedule_targets": _to_python(_google_windows(client, *windows))}


def test_call_update_changes_only_what_differs_and_shows_the_current_values(config):
    client = _Client()
    with _gaql([_call_row(client, windows=[_window()])]):
        preview = assets.update_call_asset(config, customer_id="1", asset_id="7",
                                           phone_number="+49 30 7654321", country_code="DE")
        same = assets.update_call_asset(config, customer_id="1", asset_id="7",
                                        phone_number="030 12345678", country_code="DE")
        lone_country = assets.update_call_asset(config, customer_id="1", asset_id="7", country_code="AT")
        no_action = assets.update_call_asset(config, customer_id="1", asset_id="7",
                                             call_conversion_reporting_state="USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION")
    changes = preview["changes"]
    assert (changes["phone_number"], changes["country_code"]) == ("+49307654321", "DE")
    assert changes["previous"]["ad_schedule"] == [_window()]  # shown in the draft's shape
    assert set(changes["pinned"]) == {"phone_number", "country_code"}
    assert preview["requires_double_confirm"] is False
    assert "nothing to change" in same["error"]
    assert "only changes together with phone_number" in lone_country["details"][0]
    assert "has none" in no_action["details"][0]
    with _gaql([{"asset.id": 7, "asset.type": "SITELINK"}]):
        assert "not a CALL" in assets.update_call_asset(config, customer_id="1", asset_id="7", clear_ad_schedule=True)["error"]


def test_call_update_replaces_or_clears_the_schedule_and_refuses_a_moved_asset(config):
    client = _Client()
    current = _google_windows(client, _window())
    with _gaql([_call_row(client, windows=[_window()])]):
        cleared = assets.update_call_asset(config, customer_id="1", asset_id="7", clear_ad_schedule=True)
    assert cleared["changes"]["ad_schedule"] == []
    assert any("all hours" in w for w in cleared["warnings"])

    from types import SimpleNamespace

    def row(windows):
        return SimpleNamespace(asset=SimpleNamespace(call_asset=SimpleNamespace(ad_schedule_targets=windows)))

    client.ads.rows = [row(current)]
    assets._apply_update_call_asset(client, "1", cleared["changes"])
    op = client.assets.operations[0]
    assert list(op.update_mask.paths) == ["call_asset.ad_schedule_targets"]
    assert len(op.update.call_asset.ad_schedule_targets) == 0

    moved = _Client(rows=[row(_google_windows(client, _window(eh=18)))])
    with pytest.raises(RuntimeError, match="changed in Google Ads after this preview"):
        assets._apply_update_call_asset(moved, "1", cleared["changes"])
    assert moved.assets.operations is None


# ---------------------------------------------------------------------------
# add_ad_schedule (review on #96: a first schedule needs a double confirmation)
# ---------------------------------------------------------------------------


def _campaign_reads(existing=()):
    campaign = [{"campaign.id": 1001, "campaign.name": "Brand", "campaign.status": "PAUSED",
                 "customer.time_zone": "Europe/Berlin"}]
    windows = [{"campaign_criterion.criterion_id": 900 + i,
                **{f"campaign_criterion.ad_schedule.{k}": v for k, v in {
                    "day_of_week": w["day_of_week"], "start_hour": w["start_hour"], "start_minute": "ZERO",
                    "end_hour": w["end_hour"], "end_minute": "ZERO"}.items()}}
               for i, w in enumerate(existing)]
    return patch("adloop.ads.gaql.execute_query", side_effect=[campaign, windows])


def test_a_campaigns_first_schedule_needs_a_double_confirmation(config):
    with _campaign_reads():
        first = assets.add_ad_schedule(config, customer_id="1", campaign_id="1001", schedule=[_window()])
    assert first["requires_double_confirm"] is True
    assert any("serves at all hours" in w for w in first["warnings"])
    assert any("Europe/Berlin" in w for w in first["warnings"])

    with _campaign_reads(existing=[_window("TUESDAY")]):
        more = assets.add_ad_schedule(config, customer_id="1", campaign_id="1001", schedule=[_window()])
    assert more["requires_double_confirm"] is False
    assert more["changes"]["existing_schedule"][0]["remove_id"] == "1001~900"

    with _campaign_reads(existing=[_window(sh=8, eh=12)]):
        clash = assets.add_ad_schedule(config, customer_id="1", campaign_id="1001", schedule=[_window()])
    assert "existing window 08:00-12:00 overlaps new window 09:00-17:00" in clash["details"][0]


def test_schedule_apply_refuses_when_the_existing_windows_vanished():
    changes = {"campaign_id": "1001", "schedule": [_window()], "existing_schedule": [{**_window("TUESDAY"), "remove_id": "1001~900"}]}
    gone = _Client(rows=[])
    with pytest.raises(RuntimeError, match="no longer has the ad schedule"):
        assets._apply_add_ad_schedule(gone, "1", changes)
    assert gone.criteria.operations is None

    still = _Client(rows=[object()])
    assets._apply_add_ad_schedule(still, "1", changes)
    criterion = still.criteria.operations[0].create
    assert criterion.campaign == "customers/1/campaigns/1001"
    assert criterion.ad_schedule.day_of_week == still.enums.DayOfWeekEnum.MONDAY


# ---------------------------------------------------------------------------
# Dry runs: every new apply passes the validate-only wrapper
# ---------------------------------------------------------------------------


def test_every_asset_apply_runs_validate_only(tmp_path):
    from types import SimpleNamespace

    client = _Client()
    windows = _google_windows(client, _window())
    plans = [
        (assets._apply_create_call_asset, {"scope": "ad_group", "ad_group_id": "2002", "phone_number": "+493012345678",
                                           "country_code": "DE", "ad_schedule": [_window()]}),
        (assets._apply_add_ad_schedule, {"campaign_id": "1001", "schedule": [_window()], "existing_schedule": []}),
        (assets._apply_update_call_asset, {"asset_id": "7", "ad_schedule": [], "previous": {},
                                           "pinned": {"ad_schedule": _to_python(windows)}}),
        (assets._apply_create_callouts, {"scope": "account", "callouts": ["Support auf Deutsch"]}),
    ]
    for apply, changes in plans:
        fake = _Client(rows=[SimpleNamespace(asset=SimpleNamespace(call_asset=SimpleNamespace(ad_schedule_targets=windows)))])
        dry = ValidateOnlyClient(fake)
        apply(dry, "1", changes)
        assert fake.sent() and all(request.validate_only for request in fake.sent()), apply.__name__


def test_the_new_operations_are_dispatched():
    table = write._dispatch_ads_plan.__code__.co_consts
    assert {"create_call_asset", "update_call_asset", "add_ad_schedule"} <= set(table)
