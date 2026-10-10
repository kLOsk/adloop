"""Reddit write tools: drafts, safety guards, preflight dry run, executors, gate integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop import runtime
from adloop.config import AdLoopConfig, RedditConfig, SafetyConfig
from adloop.reddit import read, write
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


_LOG_FILE = ""


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path):
    global _LOG_FILE
    _LOG_FILE = str(tmp_path / "audit.log")
    preview_store.set_plan_store(InMemoryPlanStore())
    read.reset_account_meta_cache()
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(**safety) -> AdLoopConfig:
    kwargs = dict(
        max_daily_budget=50.0, max_bid_increase_pct=100, require_dry_run=False, log_file=_LOG_FILE,
    )
    kwargs.update(safety)
    return AdLoopConfig(
        reddit=RedditConfig(client_id="app", client_secret="s", ad_account_id="a2_acct"),
        safety=SafetyConfig(**kwargs),
    )


_ACCOUNT = {"data": {"id": "a2_acct", "name": "Acme", "currency": "EUR", "time_zone_id": "Europe/Berlin"}}


def _fake_api(routes: dict):
    calls: list[tuple[str, str, dict | None]] = []

    def fake(config, method, path, *, params=None, json_body=None, absolute_url=""):
        target = absolute_url or path
        calls.append((method, target, json_body))
        for (m, suffix), payload in routes.items():
            if m == method and target.rstrip("/").endswith(suffix):
                return payload(json_body) if callable(payload) else payload
        raise AssertionError(f"unexpected Reddit call {method} {target}")

    return calls, patch("adloop.reddit.client.reddit_request", side_effect=fake)


def _no_scope_check():
    return patch("adloop.reddit.write._require_scope_for_writes", lambda config: None)


_CAMPAIGN = {"data": {"id": "c1", "name": "Launch", "configured_status": "ACTIVE",
                       "effective_status": "ACTIVE", "is_campaign_budget_optimization": False,
                       "objective": "CONVERSIONS"}}
_CBO_CAMPAIGN = {"data": {"id": "c9", "name": "CBO", "configured_status": "ACTIVE",
                           "effective_status": "ACTIVE", "is_campaign_budget_optimization": True,
                           "goal_type": "DAILY_SPEND", "goal_value": 20_000_000,
                           "bid_strategy": "MAXIMIZE_VOLUME", "optimization_goal": "PURCHASE"}}
_AD_GROUP = {"data": {"id": "g1", "name": "DE", "campaign_id": "c1", "configured_status": "ACTIVE",
                       "goal_type": "DAILY_SPEND", "goal_value": 20_000_000, "bid_value": 1_000_000,
                       "is_campaign_budget_optimization": False,
                       "targeting": {"geolocations": ["DE"], "communities": ["r/python"]}}}


class TestStatusDrafts:
    def test_pause_draft_previews_current_and_target(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN})
        with ctx:
            preview = write.pause_reddit_entity(_config(), entity_type="campaign", entity_id="c1")
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert preview["platform"] == "reddit"
        assert preview["operation"] == "reddit_set_status"
        assert preview["customer_id"] == "a2_acct"
        assert preview["changes"]["current_status"] == "ACTIVE"
        assert preview["changes"]["target_status"] == "PAUSED"
        assert preview_store.get_plan(preview["plan_id"]) is not None

    def test_enable_warns_when_no_op(self):
        paused = {"data": dict(_CAMPAIGN["data"], configured_status="ACTIVE")}
        _, ctx = _fake_api({("GET", "campaigns/c1"): paused})
        with ctx:
            preview = write.enable_reddit_entity(_config(), entity_type="campaign", entity_id="c1")
        assert any("no-op" in w for w in preview["warnings"])

    def test_remove_is_archive_with_double_confirm(self):
        _, ctx = _fake_api({("GET", "ads/ad1"): {"data": {"id": "ad1", "name": "Hero", "configured_status": "ACTIVE"}}})
        with ctx:
            preview = write.remove_reddit_entity(_config(), entity_type="ad", entity_id="ad1")
        assert preview["operation"] == "reddit_archive_entity"
        assert preview["requires_double_confirm"] is True
        assert preview["changes"]["target_status"] == "ARCHIVED"

    def test_validation_errors(self):
        result = write.pause_reddit_entity(_config(), entity_type="keyword", entity_id="")
        assert result["error"] == "Validation failed"
        assert any("entity_type" in d for d in result["details"])
        assert any("entity_id" in d for d in result["details"])

    def test_missing_entity(self):
        _, ctx = _fake_api({("GET", "campaigns/nope"): {"data": {}}})
        with ctx:
            result = write.pause_reddit_entity(_config(), entity_type="campaign", entity_id="nope")
        assert "not found" in result["error"]

    def test_blocked_operation(self):
        cfg = _config(blocked_operations=["reddit_set_status"])
        result = write.pause_reddit_entity(cfg, entity_type="campaign", entity_id="c1")
        assert "blocked" in result["error"]


class TestUpdateDrafts:
    def test_ad_group_budget_within_cap_previews_old_new(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(_config(), ad_group_id="g1", daily_budget=40)
        assert preview["operation"] == "reddit_update_ad_group"
        assert preview["changes"]["patch"] == {"goal_value": 40_000_000}
        assert preview["changes"]["display"]["budget"] == {
            "from": 20.0, "to": 40.0, "goal_type": "DAILY_SPEND", "currency": "EUR",
        }
        assert any("exceeds 50%" in w for w in preview.get("warnings", []))

    def test_ad_group_budget_over_cap_is_refused(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_ad_group(_config(), ad_group_id="g1", daily_budget=80)
        assert "exceeds maximum 50.00" in result["error"]
        assert "EUR" in result["error"]

    def test_lifetime_budget_needs_end_time_and_uses_daily_equivalent(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            missing = write.update_reddit_ad_group(_config(), ad_group_id="g1", lifetime_budget=300)
            assert any("end_time" in d for d in missing["details"])
            # 300 over 10 days = 30/day, under the 50 cap, but goal_type differs from DAILY_SPEND.
            result = write.update_reddit_ad_group(
                _config(), ad_group_id="g1", lifetime_budget=300,
                start_time="2026-10-01", end_time="2026-10-11",
            )
        assert any("goal_type cannot change" in d for d in result["details"])

    def test_bid_increase_guard(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            ok = write.update_reddit_ad_group(_config(), ad_group_id="g1", bid_value=1.8)
            too_much = write.update_reddit_ad_group(_config(), ad_group_id="g1", bid_value=2.5)
        assert ok["changes"]["patch"] == {"bid_value": 1_800_000}
        assert "Bid increase 150%" in too_much["error"]

    def test_targeting_replaces_named_keys_and_preserves_others(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(
                _config(), ad_group_id="g1", communities=["r/django", "r/flask"], gender="female",
            )
        patch_targeting = preview["changes"]["patch"]["targeting"]
        assert patch_targeting["communities"] == ["r/django", "r/flask"]
        assert patch_targeting["geolocations"] == ["DE"]
        assert patch_targeting["gender"] == "FEMALE"
        assert any("REPLACE" in w for w in preview["warnings"])

    def test_locations_are_validated_and_replace_placements(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(_config(), ad_group_id="g1", locations=["feed"])
        assert preview["changes"]["patch"]["targeting"]["locations"] == ["FEED"]
        assert preview["changes"]["patch"]["targeting"]["geolocations"] == ["DE"]
        bad = write.update_reddit_ad_group(_config(), ad_group_id="g1", locations=["SEARCH"])
        assert any("COMMENTS_PAGE" in d for d in bad["details"])
        empty = write.update_reddit_ad_group(_config(), ad_group_id="g1", locations=[])
        assert any("cannot be empty" in d for d in empty["details"])

    def test_schedule_patch_previews_readable_windows(self):
        scheduled = {"data": dict(_AD_GROUP["data"], schedule=[
            {"start_day": d, "start_hour": 13, "end_day": d, "end_hour": 23} for d in range(5)
        ])}
        _, ctx = _fake_api({("GET", "ad_groups/g1"): scheduled, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(
                _config(), ad_group_id="g1",
                schedule=[{"days": "MON-SUN", "start_hour": 8, "end_hour": 22}],
            )
        assert len(preview["changes"]["patch"]["schedule"]) == 7
        assert preview["changes"]["patch"]["schedule"][6] == {"start_day": 6, "start_hour": 8, "end_day": 6, "end_hour": 22}
        assert preview["changes"]["display"]["schedule"] == {"from": "Mon–Fri 13:00–23:59", "to": "Mon–Sun 08:00–22:59"}
        assert any("viewer's local time" in w for w in preview["warnings"])

    def test_empty_schedule_clears_it(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(_config(), ad_group_id="g1", schedule=[])
        assert preview["changes"]["patch"] == {"schedule": []}
        assert preview["changes"]["display"]["schedule"]["to"] == "any time"

    def test_bad_schedule_is_a_validation_error(self):
        result = write.update_reddit_ad_group(
            _config(), ad_group_id="g1", schedule=[{"days": "MON", "start_hour": 25, "end_hour": 3}]
        )
        assert result["error"] and any("between 0 and 23" in d for d in result["details"])

    def test_schedule_on_cbo_ad_group_points_to_campaign(self):
        cbo_group = {"data": dict(_AD_GROUP["data"], is_campaign_budget_optimization=True)}
        _, ctx = _fake_api({("GET", "ad_groups/g1"): cbo_group, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_ad_group(
                _config(), ad_group_id="g1", schedule=[{"days": "MON", "start_hour": 1, "end_hour": 2}]
            )
        assert any("update_reddit_campaign" in d for d in result["details"])

    def test_campaign_schedule_only_for_cbo(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_campaign(
                _config(), campaign_id="c1", schedule=[{"days": "MON", "start_hour": 1, "end_hour": 2}]
            )
        assert any("update_reddit_ad_group" in d for d in result["details"])

    def test_cbo_campaign_schedule_change_replaces_ad_groups(self):
        _, ctx = _fake_api({("GET", "campaigns/c9"): _CBO_CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_campaign(
                _config(), campaign_id="c9", schedule=[{"days": "SAT-SUN", "start_hour": 10, "end_hour": 20}]
            )
        assert [b["start_day"] for b in preview["changes"]["patch"]["schedule"]] == [5, 6]
        assert preview["changes"]["display"]["schedule"]["from"] == "any time"
        assert any("replaces every ad group" in w for w in preview["warnings"])

    def test_budget_on_cbo_ad_group_points_to_campaign(self):
        cbo_group = {"data": dict(_AD_GROUP["data"], is_campaign_budget_optimization=True)}
        _, ctx = _fake_api({("GET", "ad_groups/g1"): cbo_group, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_ad_group(_config(), ad_group_id="g1", daily_budget=10)
        assert any("update_reddit_campaign" in d for d in result["details"])

    def test_campaign_budget_only_for_cbo(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_campaign(_config(), campaign_id="c1", daily_budget=10)
        assert any("update_reddit_ad_group" in d for d in result["details"])

    def test_cbo_campaign_budget_change(self):
        _, ctx = _fake_api({("GET", "campaigns/c9"): _CBO_CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_campaign(_config(), campaign_id="c9", daily_budget=25, name="CBO v2")
        assert preview["changes"]["patch"] == {"name": "CBO v2", "goal_value": 25_000_000}
        assert preview["changes"]["display"]["name"] == {"from": "CBO", "to": "CBO v2"}

    def test_nothing_to_change(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_campaign(_config(), campaign_id="c1", name="Launch")
        assert "Nothing to change" in result["details"][0]


_CARRIERS = {"data": [
    {"id": "O2_DEUTSCHLAND", "name": "O2 Deutschland", "country_code": "DE"},
    {"id": "VODAFONE_GERMANY", "name": "Vodafone", "country_code": "DE"},
], "pagination": {}}


class TestDeviceCarrierTargeting:
    """devices, carriers, excluded_interests and view_modes on ad group drafts."""

    def test_update_replaces_new_keys_and_preserves_others(self):
        _, ctx = _fake_api({
            ("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("GET", "targeting/carriers"): _CARRIERS,
        })
        with ctx:
            preview = write.update_reddit_ad_group(
                _config(), ad_group_id="g1",
                devices=[{"type": "mobile", "os": "ios", "min_version": 16, "label_map": {"Apple": ["Iphone 12"]}}],
                carriers=["o2_deutschland"], view_modes=["card", "classic"], excluded_interests=["i9"],
            )
        targeting = preview["changes"]["patch"]["targeting"]
        assert targeting["devices"] == [
            {"type": "MOBILE", "os": "IOS", "min_version": "16", "label_map": {"Apple": ["Iphone 12"]}}
        ]
        assert targeting["carriers"] == ["O2_DEUTSCHLAND"]
        assert targeting["view_modes"] == ["CARD", "CLASSIC"]
        assert targeting["excluded_interests"] == ["i9"]
        assert targeting["geolocations"] == ["DE"]
        assert targeting["communities"] == ["r/python"]
        assert preview["changes"]["display"]["targeting"]["carriers"] == {"from": None, "to": ["O2_DEUTSCHLAND"]}
        assert any("deprecated" in w for w in preview["warnings"])
        assert any("REPLACE" in w for w in preview["warnings"])

    def test_update_empty_lists_clear_the_keys(self):
        current = {"data": dict(_AD_GROUP["data"], targeting={
            "geolocations": ["DE"], "devices": [{"type": "MOBILE"}], "carriers": ["O2_DEUTSCHLAND"],
        })}
        calls, ctx = _fake_api({("GET", "ad_groups/g1"): current, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(_config(), ad_group_id="g1", devices=[], carriers=[])
        targeting = preview["changes"]["patch"]["targeting"]
        assert targeting["devices"] == [] and targeting["carriers"] == []
        assert targeting["geolocations"] == ["DE"]
        # Nothing to validate, so the carrier list is not fetched.
        assert not any(c[1].endswith("targeting/carriers") for c in calls)

    def test_update_refuses_unknown_carriers(self):
        _, ctx = _fake_api({
            ("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("GET", "targeting/carriers"): _CARRIERS,
        })
        with ctx:
            result = write.update_reddit_ad_group(_config(), ad_group_id="g1", carriers=["O2_DEUTSCHLAND", "ACME_MOBILE"])
        assert result["error"] == "Validation failed"
        assert any("Unknown carrier ids: ACME_MOBILE" in d for d in result["details"])

    def test_update_warns_when_carrier_lookup_is_unavailable(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(_config(), ad_group_id="g1", carriers=["O2_DEUTSCHLAND"])
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert any("Carriers could not be pre-validated" in w for w in preview["warnings"])

    @pytest.mark.parametrize(
        ("devices", "expected"),
        [
            ([{"os": "IOS"}], "devices[0].type must be one of"),
            ([{"type": "TABLET"}], "devices[0].type must be one of"),
            ([{"type": "MOBILE", "os": "WINDOWS"}], "devices[0].os must be one of"),
            ([{"type": "MOBILE", "os": "IOS", "min_version": "12"}], "at least 14 for iOS"),
            ([{"type": "MOBILE", "min_version": "10", "max_version": "9"}], "cannot be above max_version"),
            ([{"type": "MOBILE", "min_version": "v9"}], "major OS version number"),
            ([{"type": "MOBILE", "brand": "Apple"}], "unsupported keys ['brand']"),
            ([{"type": "MOBILE", "label_map": ["Galaxy S9"]}], "label_map must map a make"),
            (["MOBILE"], "devices[0] must be an object"),
        ],
    )
    def test_device_validation(self, devices, expected):
        result = write.update_reddit_ad_group(_config(), ad_group_id="g1", devices=devices)
        assert result["error"] == "Validation failed"
        assert any(expected in d for d in result["details"]), result["details"]

    def test_view_modes_are_validated(self):
        result = write.update_reddit_ad_group(_config(), ad_group_id="g1", view_modes=["CARD", "GRID"])
        assert any("view_modes contains unsupported values ['GRID']" in d for d in result["details"])

    def test_draft_carries_new_keys_and_validates_carriers(self):
        calls, ctx = _fake_api({
            ("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "targeting/keyword_validations"): {"data": []},
            ("POST", "targeting/geolocations_validations"): {"data": [{"geolocation": {"id": "DE"}, "error_message": ""}]},
            ("GET", "targeting/carriers"): _CARRIERS,
        })
        with ctx:
            preview = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="DE mobile", conversion_pixel_id="px1",
                daily_budget=20, bid_strategy="bidless", bid_type="cpc", geolocations=["DE"], languages=["DE"],
                devices=[{"type": "MOBILE", "os": "ANDROID"}], carriers=["vodafone_germany"], view_modes=["CARD"],
            )
        targeting = preview["changes"]["payload"]["targeting"]
        assert targeting["devices"] == [{"type": "MOBILE", "os": "ANDROID"}]
        assert targeting["carriers"] == ["VODAFONE_GERMANY"]
        assert targeting["view_modes"] == ["CARD"]
        assert preview["changes"]["display"]["targeting"]["carriers"] == ["VODAFONE_GERMANY"]
        assert any(c[1].endswith("targeting/carriers") for c in calls)
        assert not any("pre-validated" in w for w in preview["warnings"])
        assert not any("deprecated" in w for w in preview["warnings"])

    def test_draft_refuses_unknown_carriers_and_warns_on_excluded_interests(self):
        _, ctx = _fake_api({
            ("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "targeting/keyword_validations"): {"data": []},
            ("POST", "targeting/geolocations_validations"): {"data": [{"geolocation": {"id": "DE"}, "error_message": ""}]},
            ("GET", "targeting/carriers"): _CARRIERS,
        })
        with ctx:
            refused = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="X", conversion_pixel_id="px1",
                daily_budget=5, bid_strategy="MAXIMIZE_VOLUME", bid_type="CPC", geolocations=["DE"],
                carriers=["TELEKOM_MARS"],
            )
            preview = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="X", conversion_pixel_id="px1",
                daily_budget=5, bid_strategy="MAXIMIZE_VOLUME", bid_type="CPC", geolocations=["DE"],
                languages=["DE"], interests=["i1"], excluded_interests=["i2"],
            )
        assert any("Unknown carrier ids: TELEKOM_MARS" in d for d in refused["details"])
        assert preview["changes"]["payload"]["targeting"]["excluded_interests"] == ["i2"]
        assert any("deprecated" in w for w in preview["warnings"])

    def test_draft_app_installs_needs_exactly_one_device(self):
        app_campaign = {"data": dict(_CAMPAIGN["data"], objective="APP_INSTALLS")}
        routes = {
            ("GET", "campaigns/c1"): app_campaign, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "targeting/keyword_validations"): {"data": []},
            ("POST", "targeting/geolocations_validations"): {"data": [{"geolocation": {"id": "DE"}, "error_message": ""}]},
        }
        common = dict(
            campaign_id="c1", ad_group_name="App", conversion_pixel_id="px1", daily_budget=5,
            bid_strategy="MAXIMIZE_VOLUME", bid_type="CPC", geolocations=["DE"], languages=["DE"],
        )
        _, ctx = _fake_api(routes)
        with ctx:
            none = write.draft_reddit_ad_group(_config(), **common)
            two = write.draft_reddit_ad_group(
                _config(), **common, devices=[{"type": "MOBILE", "os": "IOS"}, {"type": "MOBILE", "os": "ANDROID"}],
            )
            one = write.draft_reddit_ad_group(_config(), **common, devices=[{"type": "MOBILE", "os": "IOS"}])
        assert any("exactly one device" in d for d in none["details"])
        assert any("exactly one device" in d for d in two["details"])
        assert one["status"] == "PENDING_CONFIRMATION"


_AD = {"data": {"id": "ad1", "name": "Hero", "configured_status": "ACTIVE", "post_id": "post1",
                 "click_url": "https://example.com/old", "ad_group_id": "g1"}}
# The post behind _AD: an image post, so a click_url is legitimate.
_AD_POST = {"data": {"id": "post1", "type": "IMAGE", "headline": "Hero",
                     "content": [{"destination_url": "https://example.com/old"}]}}


class TestUpdateAd:
    def test_update_ad_previews_url_name_and_comments(self):
        _, ctx = _fake_api({("GET", "ads/ad1"): _AD, ("GET", "posts/post1"): _AD_POST})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/new": None}, {})):
            preview = write.update_reddit_ad(
                _config(), ad_id="ad1", click_url="https://example.com/new", name="Hero v2", allow_comments=False,
            )
        assert preview["operation"] == "reddit_update_ad"
        assert preview["changes"]["patch"] == {"name": "Hero v2", "click_url": "https://example.com/new"}
        assert preview["changes"]["post_patch"] == {"allow_comments": False}
        assert preview["changes"]["display"]["click_url"] == {"from": "https://example.com/old", "to": "https://example.com/new"}

    def test_update_ad_rejects_unreachable_url_and_empty_change(self):
        with patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/404": "HTTP 404"}, {})):
            result = write.update_reddit_ad(_config(), ad_id="ad1", click_url="https://example.com/404")
        assert any("not reachable" in d for d in result["details"])
        _, ctx = _fake_api({("GET", "ads/ad1"): _AD})
        with ctx:
            nothing = write.update_reddit_ad(_config(), ad_id="ad1", name="Hero")
        assert any("cannot be edited" in d for d in nothing["details"])

    def test_update_ad_refuses_click_url_on_text_posts_before_the_dry_run(self):
        text_post = {"data": {"id": "post1", "type": "TEXT", "headline": "h", "body": "see https://example.com"}}
        _, ctx = _fake_api({("GET", "ads/ad1"): _AD, ("GET", "posts/post1"): text_post})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/new": None}, {})):
            result = write.update_reddit_ad(_config(), ad_id="ad1", click_url="https://example.com/new")
        assert result["error"]
        assert any("free-form" in d and "draft_reddit_ad" in d for d in result["details"])

    def test_update_ad_keeps_click_url_on_image_posts(self):
        image_post = {"data": {"id": "post1", "type": "IMAGE", "headline": "h", "content": [{"destination_url": "https://example.com/old"}]}}
        _, ctx = _fake_api({("GET", "ads/ad1"): _AD, ("GET", "posts/post1"): image_post})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/new": None}, {})):
            preview = write.update_reddit_ad(_config(), ad_id="ad1", click_url="https://example.com/new")
        assert preview["changes"]["patch"] == {"click_url": "https://example.com/new"}

    def test_update_ad_apply_patches_ad_then_post(self):
        from adloop.ads import write as ads_write

        _, ctx = _fake_api({("GET", "ads/ad1"): _AD, ("GET", "posts/post1"): _AD_POST})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/new": None}, {})):
            plan_id = write.update_reddit_ad(
                _config(), ad_id="ad1", click_url="https://example.com/new", allow_comments=False,
            )["plan_id"]
        calls, ctx = _fake_api({
            ("GET", "ads/ad1"): _AD,
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("PATCH", "ads/ad1"): lambda body: {"data": {"id": "ad1", "name": "Hero", "click_url": body["data"]["click_url"]}},
            ("PATCH", "posts/post1"): lambda body: {"data": {"id": "post1", "allow_comments": body["data"]["allow_comments"]}},
        })
        with ctx, _no_scope_check():
            dry = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
            assert dry["status"] == "DRY_RUN_SUCCESS" and dry["checks"]["post_id"] == "post1"
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert result["status"] == "APPLIED"
        assert result["result"]["click_url"] == "https://example.com/new"
        assert result["result"]["allow_comments"] is False
        assert sorted(result["result"]["updated_fields"]) == ["allow_comments", "click_url"]
        assert [c[:2] for c in calls if c[0] == "PATCH"] == [("PATCH", "ads/ad1"), ("PATCH", "posts/post1")]


class TestCreationDrafts:
    def test_campaign_draft_defaults_to_paused_and_ad_group_budgets(self):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="conversions", funding_instrument_id="fi1",
            )
        payload = preview["changes"]["payload"]
        assert payload["configured_status"] == "PAUSED"
        assert payload["objective"] == "CONVERSIONS"
        assert payload["is_campaign_budget_optimization"] is False
        assert "goal_value" not in payload
        assert preview["changes"]["display"]["status_on_create"] == "PAUSED"
        assert any("firing pixel" in w for w in preview["warnings"])

    def test_campaign_draft_requires_objective_and_funding(self):
        result = write.draft_reddit_campaign(_config(), campaign_name="Q4")
        assert any("objective is required" in d for d in result["details"])
        assert any("funding_instrument_id" in d for d in result["details"])

    def test_cbo_campaign_requires_bid_pixel_and_checks_cap(self):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            missing = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
                campaign_budget_optimization=True, daily_budget=10,
            )
            over = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
                campaign_budget_optimization=True, daily_budget=99, bid_strategy="MAXIMIZE_VOLUME",
                bid_type="CPC", conversion_pixel_id="px1",
            )
            ok = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
                campaign_budget_optimization=True, daily_budget=30, bid_strategy="MAXIMIZE_VOLUME",
                bid_type="CPC", conversion_pixel_id="px1",
            )
        assert any("bid_strategy" in d for d in missing["details"])
        assert any("conversion_pixel_id" in d for d in missing["details"])
        assert "exceeds maximum" in over["error"]
        assert ok["changes"]["payload"]["goal_value"] == 30_000_000
        assert ok["changes"]["payload"]["goal_type"] == "DAILY_SPEND"

    def test_unknown_objective_warns_instead_of_failing(self):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="TRAFFIC", funding_instrument_id="fi1",
            )
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert any("not in AdLoop's known list" in w for w in preview["warnings"])

    def test_ad_group_draft_requires_pixel_targeting_budget_bid(self):
        result = write.draft_reddit_ad_group(_config(), campaign_id="c1", ad_group_name="DE")
        details = " ".join(result["details"])
        assert "conversion_pixel_id" in details
        assert "Targeting is required" in details

    def test_ad_group_draft_builds_paused_payload(self):
        _, ctx = _fake_api({
            ("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "targeting/keyword_validations"): {"data": []},
            ("POST", "targeting/geolocations_validations"): {"data": [{"geolocation": {"id": "DE"}, "error_message": ""}]},
        })
        with ctx:
            preview = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="DE devs", conversion_pixel_id="px1",
                daily_budget=20, bid_strategy="maximize_volume", bid_type="cpc",
                optimization_goal="sign_up", geolocations=["DE"], communities=["r/python"],
                languages=["DE"],
            )
        payload = preview["changes"]["payload"]
        assert payload["configured_status"] == "PAUSED"
        assert payload["goal_value"] == 20_000_000
        assert payload["bid_strategy"] == "MAXIMIZE_VOLUME"
        assert payload["optimization_goal"] == "SIGN_UP"
        assert payload["targeting"]["languages"] == ["DE"]
        assert payload["targeting"]["communities"] == ["r/python"]
        # Reddit requires it; verified live 2026-09-11.
        assert payload["start_time"].endswith("Z") and len(payload["start_time"]) == 20
        assert not any("No language targeting" in w for w in preview["warnings"])
        assert not any("pre-validated" in w for w in preview["warnings"])

    def test_ad_group_draft_carries_schedule(self):
        _, ctx = _fake_api({
            ("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "targeting/keyword_validations"): {"data": []},
            ("POST", "targeting/geolocations_validations"): {"data": [{"geolocation": {"id": "DE"}, "error_message": ""}]},
        })
        with ctx:
            preview = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="DE devs", conversion_pixel_id="px1",
                daily_budget=20, bid_strategy="bidless", bid_type="cpc", geolocations=["DE"],
                languages=["DE"], schedule=[{"days": "MON-FRI", "start_hour": 13, "end_hour": 23}],
            )
        assert len(preview["changes"]["payload"]["schedule"]) == 5
        assert preview["changes"]["display"]["schedule"] == "Mon–Fri 13:00–23:59"
        assert any("viewer's local time" in w for w in preview["warnings"])

    def test_ad_group_draft_on_cbo_campaign_rejects_schedule(self):
        _, ctx = _fake_api({("GET", "campaigns/c9"): _CBO_CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.draft_reddit_ad_group(
                _config(), campaign_id="c9", ad_group_name="X", conversion_pixel_id="px1",
                geolocations=["DE"], schedule=[{"days": "MON", "start_hour": 1, "end_hour": 2}],
            )
        assert any("update_reddit_campaign" in d for d in result["details"])

    def test_campaign_draft_schedule_needs_cbo(self):
        result = write.draft_reddit_campaign(
            _config(), campaign_name="X", objective="CLICKS", funding_instrument_id="f1",
            schedule=[{"days": "MON", "start_hour": 1, "end_hour": 2}],
        )
        assert any("campaign_budget_optimization=true" in d for d in result["details"])

    def test_ad_group_draft_refuses_unsafe_keywords_and_unknown_geos(self):
        _, ctx = _fake_api({
            ("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "targeting/keyword_validations"): {"data": [
                {"keyword": "python", "is_brand_safe": True}, {"keyword": "gore", "is_brand_safe": False},
            ]},
            ("POST", "targeting/geolocations_validations"): {"data": [
                {"geolocation": {"id": "DE"}, "error_message": ""},
                {"geolocation": {"id": "XX"}, "error_message": "XX is an unknown country code"},
            ]},
        })
        with ctx:
            result = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="X", conversion_pixel_id="px1",
                daily_budget=5, bid_strategy="MAXIMIZE_VOLUME", bid_type="CPC",
                geolocations=["DE", "XX"], keywords=["python", "gore"],
            )
        assert result["error"] == "Validation failed"
        assert any("not brand-safe: gore" in d for d in result["details"])
        assert any("geolocation 'XX': XX is an unknown country code" in d for d in result["details"])

    def test_ad_group_draft_warns_when_validation_is_unavailable(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="X", conversion_pixel_id="px1",
                daily_budget=5, bid_strategy="MAXIMIZE_VOLUME", bid_type="CPC", geolocations=["DE"],
            )
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert any("pre-validated" in w for w in preview["warnings"])

    def test_ad_group_draft_on_cbo_campaign_rejects_budget(self):
        _, ctx = _fake_api({("GET", "campaigns/c9"): _CBO_CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.draft_reddit_ad_group(
                _config(), campaign_id="c9", ad_group_name="X", conversion_pixel_id="px1",
                daily_budget=5, geolocations=["DE"],
            )
        assert any("campaign budget optimization" in d for d in result["details"])

    def test_ad_group_manual_bidding_needs_bid_value(self):
        result = write.draft_reddit_ad_group(
            _config(), campaign_id="c1", ad_group_name="X", conversion_pixel_id="px1",
            daily_budget=5, bid_strategy="MANUAL_BIDDING", bid_type="CPC", geolocations=["DE"],
        )
        assert any("MANUAL_BIDDING needs bid_value" in d for d in result["details"])

    def test_ad_draft_verifies_click_url_and_builds_post_and_ad(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/x": None}, {})):
            preview = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="Ship faster",
                click_url="https://example.com/x", call_to_action="Learn More",
                body="Try it: https://example.com/x",
            )
        changes = preview["changes"]
        assert changes["post"]["type"] == "TEXT"
        assert changes["post"]["headline"] == "Ship faster"
        assert changes["post"]["content"] == []  # Reddit refuses content on TEXT posts
        assert changes["post"]["body"] == "Try it: https://example.com/x"
        assert "click_url" not in changes["ad"]  # free-form ads open the post
        assert any("open the Reddit post" in w for w in preview["warnings"])
        assert changes["ad"]["configured_status"] == "PAUSED"

    def test_existing_text_post_is_promoted_without_creating_a_post(self):
        from adloop.ads import write as ads_write

        text_post = {"data": {"id": "t3_x", "type": "TEXT", "profile_id": "p1", "headline": "Old post",
                              "body": "read more at https://example.com", "allow_comments": True,
                              "post_url": "https://reddit.com/p/t3_x"}}
        _, ctx = _fake_api({("GET", "posts/t3_x"): text_post, ("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx:
            preview = write.draft_reddit_ad(_config(), ad_group_id="g1", post_id="t3_x", headline="ignored")
        assert preview["operation"] == "reddit_create_ad"
        assert preview["changes"]["post"] is None
        assert preview["changes"]["existing_post_id"] == "t3_x"
        ad = preview["changes"]["ad"]
        assert ad["post_id"] == "t3_x" and ad["profile_id"] == "p1" and ad["name"] == "Old post"
        assert "click_url" not in ad
        assert any("were ignored" in w for w in preview["warnings"])
        assert any("Comments are enabled" in w for w in preview["warnings"])

        calls, ctx = _fake_api({
            ("GET", "posts/t3_x"): text_post, ("GET", "ad_groups/g1"): _AD_GROUP,
            ("GET", "ad_accounts/a2_acct"): _ACCOUNT,
            ("POST", "ad_accounts/a2_acct/ads"): lambda body: {"data": {"id": "ad9", "post_id": body["data"]["post_id"],
                                                                        "configured_status": "PAUSED"}},
        })
        with ctx, _no_scope_check():
            dry = ads_write.confirm_and_apply(_config(), plan_id=preview["plan_id"], dry_run=True)
            assert dry["checks"]["existing_post_id"] == "t3_x"
            result = ads_write.confirm_and_apply(_config(), plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "APPLIED"
        assert result["result"]["ad_id"] == "ad9" and result["result"]["post_id"] == "t3_x"
        assert not any(c[0] == "POST" and c[1].endswith("/posts") for c in calls)

    def test_existing_text_post_refuses_click_url_and_flags_missing_link(self):
        text_post = {"data": {"id": "t3_x", "type": "TEXT", "profile_id": "p1", "headline": "h", "body": "no link here"}}
        _, ctx = _fake_api({("GET", "posts/t3_x"): text_post, ("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx:
            refused = write.draft_reddit_ad(_config(), ad_group_id="g1", post_id="t3_x", click_url="https://example.com")
            assert any("refuses a click_url" in d for d in refused["details"])
            preview = write.draft_reddit_ad(_config(), ad_group_id="g1", post_id="t3_x")
        assert any("contains no link" in w for w in preview["warnings"])

    def test_existing_image_post_defaults_click_url_to_its_destination(self):
        image_post = {"data": {"id": "t3_i", "type": "IMAGE", "profile_id": "p1", "headline": "Pic",
                               "content": [{"destination_url": "https://example.com/land", "media_url": "https://i.redd.it/x.jpg"}]}}
        _, ctx = _fake_api({("GET", "posts/t3_i"): image_post, ("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/land": None}, {})):
            preview = write.draft_reddit_ad(_config(), ad_group_id="g1", post_id="t3_i", ad_name="Pic again")
        assert preview["changes"]["ad"]["click_url"] == "https://example.com/land"
        assert preview["changes"]["display"]["post_type"] == "IMAGE"

    def test_existing_post_must_belong_to_the_profile_and_exist(self):
        image_post = {"data": {"id": "t3_i", "type": "IMAGE", "profile_id": "p1", "content": [{"destination_url": "https://example.com"}]}}
        _, ctx = _fake_api({("GET", "posts/t3_i"): image_post})
        with ctx:
            wrong = write.draft_reddit_ad(_config(), ad_group_id="g1", post_id="t3_i", profile_id="p2")
        assert any("belongs to profile p1" in d for d in wrong["details"])

        def missing(*args, **kwargs):
            from adloop.reddit.auth import RedditApiError

            raise RedditApiError("Reddit Ads API returned 404", status=404)

        with patch("adloop.reddit.client.reddit_request", side_effect=missing):
            gone = write.draft_reddit_ad(_config(), ad_group_id="g1", post_id="t3_nope")
        assert "was not found" in gone["error"]

    def test_text_ad_requires_the_link_in_the_body(self):
        with patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/x": None}, {})):
            result = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="x",
                click_url="https://example.com/x", body="no link here",
            )
        assert any("put the full click_url into body" in d for d in result["details"])

    def test_image_ad_draft_carries_content_block(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/x": None, "https://img.example.com/a.jpg": None}, {})):
            preview = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="Look", post_type="image",
                click_url="https://example.com/x", image_url="https://img.example.com/a.jpg",
                call_to_action="Learn More", display_url="example.com",
            )
        assert preview["changes"]["post"]["content"] == [{
            "destination_url": "https://example.com/x", "media_url": "https://img.example.com/a.jpg",
            "call_to_action": "Learn More", "display_url": "example.com",
        }]

    def test_ad_draft_rejects_unreachable_url(self):
        with patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/404": "HTTP 404"}, {})):
            result = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="x", click_url="https://example.com/404",
                body="see https://example.com/404",
            )
        assert any("not reachable" in d for d in result["details"])

    def test_ad_draft_image_needs_url(self):
        result = write.draft_reddit_ad(
            _config(), ad_group_id="g1", profile_id="p1", headline="x",
            click_url="https://example.com", post_type="image",
        )
        assert any("image_url" in d for d in result["details"])
        video = write.draft_reddit_ad(
            _config(), ad_group_id="g1", profile_id="p1", headline="x",
            click_url="https://example.com", post_type="video",
        )
        assert any("post_type" in d for d in video["details"])


class TestConfirmAndApplyIntegration:
    """The shared gate must route reddit_* plans away from the Google client."""

    def _paused_plan(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN})
        with ctx:
            preview = write.pause_reddit_entity(_config(), entity_type="campaign", entity_id="c1")
        return preview["plan_id"]

    def test_dry_run_runs_preflight_and_marks_plan(self):
        from adloop.ads import write as ads_write

        plan_id = self._paused_plan()
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx, _no_scope_check(), patch("adloop.ads.client.get_ads_client", side_effect=AssertionError("google touched")):
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"]["entity"] == "Launch"
        assert "validate-only" in result["note"]
        assert "Reddit Ads" in result["message"]
        assert preview_store.get_plan(plan_id).dry_run_result is not None

    def test_failed_preflight_keeps_two_phase_gate_closed(self):
        from adloop.ads import write as ads_write

        plan_id = self._paused_plan()
        cfg = _config(two_phase_apply=True)
        gone = {("GET", "campaigns/c1"): {"data": {}}, ("GET", "ad_accounts/a2_acct"): _ACCOUNT}
        _, ctx = _fake_api(gone)
        with ctx, _no_scope_check():
            result = ads_write.confirm_and_apply(cfg, plan_id=plan_id, dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "no longer exists" in result["error"]
        assert preview_store.get_plan(plan_id).dry_run_result is None
        with _no_scope_check():
            refused = ads_write.confirm_and_apply(cfg, plan_id=plan_id, dry_run=False)
        assert refused["status"] == "DRY_RUN_REQUIRED"

    def test_real_apply_patches_status_without_google_client(self):
        from adloop.ads import write as ads_write

        plan_id = self._paused_plan()
        calls, ctx = _fake_api({
            ("PATCH", "campaigns/c1"): lambda body: {"data": {"id": "c1", "configured_status": body["data"]["configured_status"], "effective_status": "PAUSED"}},
        })
        with ctx, _no_scope_check(), patch("adloop.ads.client.get_ads_client", side_effect=AssertionError("google touched")):
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert result["status"] == "APPLIED"
        assert result["result"]["configured_status"] == "PAUSED"
        assert calls[0][2] == {"data": {"configured_status": "PAUSED"}}
        assert preview_store.get_plan(plan_id) is None

    def test_create_ad_reports_orphan_post_when_ad_fails(self):
        from adloop.ads import write as ads_write
        from adloop.reddit.auth import RedditApiError

        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com": None}, {})):
            plan_id = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="x", click_url="https://example.com",
                body="see https://example.com",
            )["plan_id"]

        def failing_ad(body):
            raise RedditApiError("Reddit Ads API returned 400: bad ad", status=400)

        _, ctx = _fake_api({
            ("POST", "profiles/p1/posts"): {"data": {"id": "post1", "post_url": "https://reddit.com/p/post1"}},
            ("POST", "ad_accounts/a2_acct/ads"): failing_ad,
        })
        with ctx, _no_scope_check():
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert "post_id post1" in result["error"]
        assert "Reuse the post_id" in result["error"]

    def test_create_campaign_forces_paused(self):
        from adloop.ads import write as ads_write

        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            plan_id = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
            )["plan_id"]
        plan = preview_store.get_plan(plan_id)
        plan.changes["payload"]["configured_status"] = "ACTIVE"  # tampered plan
        preview_store.store_plan(plan)

        posted = {}

        def create(body):
            posted.update(body["data"])
            return {"data": {"id": "c-new", "name": "Q4", "configured_status": "PAUSED"}}

        _, ctx = _fake_api({("POST", "ad_accounts/a2_acct/campaigns"): create})
        with ctx, _no_scope_check():
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert posted["configured_status"] == "PAUSED"
        assert result["result"]["campaign_id"] == "c-new"

    def test_scope_check_blocks_read_only_grant(self):
        from adloop import auth as adloop_auth
        from adloop.reddit.auth import RedditCredentials

        class _ReadOnlyCreds(RedditCredentials):
            granted_scopes = ["adsread"]

        class _Provider:
            def ga4_credentials(self, config):  # pragma: no cover
                raise AssertionError

            def ads_credentials(self, config):  # pragma: no cover
                raise AssertionError

            def reddit_credentials(self, config):
                return _ReadOnlyCreds(client_id="a", client_secret="b", refresh_token="rt", user_agent="ua")

        original = adloop_auth.get_credentials_provider()
        adloop_auth.set_credentials_provider(_Provider())
        try:
            from adloop.ads import write as ads_write

            plan_id = self._paused_plan()
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
        finally:
            adloop_auth.set_credentials_provider(original)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "adsedit" in result["error"]


class TestServerWrappers:
    """Tool registration: tags, annotations, structured errors."""

    @pytest.mark.asyncio
    async def test_reddit_tools_are_tagged_and_annotated(self):
        from adloop.server import mcp

        tools = await mcp.list_tools()
        reddit = [t for t in tools if "reddit" in t.name]
        assert len(reddit) >= 20
        for tool in reddit:
            assert set(tool.tags) == {"reddit"}, tool.name
        writes = {t.name for t in reddit if not t.annotations.readOnlyHint}
        assert writes == {
            "pause_reddit_entity", "enable_reddit_entity", "remove_reddit_entity",
            "update_reddit_campaign", "update_reddit_ad_group", "update_reddit_ad",
            "draft_reddit_campaign", "draft_reddit_ad_group", "draft_reddit_ad",
        }
        destructive = {t.name for t in reddit if t.annotations.destructiveHint}
        assert destructive == {"remove_reddit_entity"}

    @pytest.mark.asyncio
    async def test_ad_group_tools_expose_device_and_carrier_targeting(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("draft_reddit_ad_group", "update_reddit_ad_group"):
            params = tools[name].parameters["properties"]
            for key in ("devices", "carriers", "excluded_interests", "view_modes"):
                assert key in params, f"{name}.{key}"
        estimate = tools["estimate_reddit_ad_group"].parameters["properties"]
        assert "devices" in estimate and "carriers" in estimate
        kind = tools["search_reddit_targeting"].parameters["properties"]["kind"]["description"]
        assert '"devices"' in kind and '"carriers"' in kind

    @pytest.mark.asyncio
    async def test_draft_tool_passes_devices_and_carriers_through(self):
        from adloop.server import mcp

        seen = {}

        def fake_impl(config, **kwargs):
            seen.update(kwargs)
            return {"status": "PENDING_CONFIRMATION"}

        with patch("adloop.reddit.write.draft_reddit_ad_group", side_effect=fake_impl):
            await mcp.call_tool("draft_reddit_ad_group", {
                "campaign_id": "c1", "ad_group_name": "X", "conversion_pixel_id": "px1",
                "devices": '[{"type": "MOBILE", "os": "IOS"}]', "carriers": ["O2_DEUTSCHLAND"],
                "view_modes": ["CARD"], "excluded_interests": ["i2"],
            })
        assert seen["devices"] == [{"type": "MOBILE", "os": "IOS"}]
        assert seen["carriers"] == ["O2_DEUTSCHLAND"]
        assert seen["view_modes"] == ["CARD"]
        assert seen["excluded_interests"] == ["i2"]

    @pytest.mark.asyncio
    async def test_reddit_tools_never_use_account_id_parameter(self):
        """Merchant tools own ``account_id``; Reddit must always say ``ad_account_id``."""
        from adloop.server import mcp

        for tool in await mcp.list_tools():
            if "reddit" in tool.name:
                params = tool.parameters.get("properties", {})
                assert "account_id" not in params, tool.name

    def test_structured_error_reddit_invalid_grant_is_not_google_advice(self):
        from adloop.reddit.auth import RedditAuthError
        from adloop.server import _structured_error

        runtime.set_deployment_mode("local")
        parsed = _structured_error("get_reddit_campaigns", RedditAuthError("invalid_grant", error_code="invalid_grant"))
        assert parsed["auth_error"] == "REDDIT_INVALID_GRANT"
        assert "reddit_token.json" in parsed["hint"]
        assert "Google" not in parsed["hint"]

    def test_structured_error_reddit_rate_limit(self):
        from adloop.reddit.auth import RedditApiError
        from adloop.server import _structured_error

        parsed = _structured_error("x", RedditApiError("429", status=429, reset_seconds=30))
        assert parsed["auth_error"] == "REDDIT_RATE_LIMITED"
        assert parsed["reset_seconds"] == 30

    def test_safe_wrapper_returns_hint_for_reddit_errors(self):
        from adloop.reddit.auth import RedditApiError
        from adloop.server import _safe

        @_safe
        def boom():
            raise RedditApiError("forbidden", status=403)

        result = boom()
        assert result["auth_error"] == "REDDIT_FORBIDDEN"
        assert "hint" in result

    @pytest.mark.asyncio
    async def test_health_check_reports_not_configured_without_reddit(self):
        from adloop.server import mcp

        tool = await mcp.get_tool("health_check")
        runtime.set_default_config(AdLoopConfig())
        try:
            with patch("adloop.ga4.reports.get_account_summaries", return_value={"total_properties": 0}), \
                 patch("adloop.ads.gaql.execute_query", return_value=[]):
                status = tool.fn()
        finally:
            runtime.set_default_config(None)
        assert status["reddit"] == "not_configured"
