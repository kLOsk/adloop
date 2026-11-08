"""ChatGPT Ads management: drafts, dry runs and applies against a stateful fake Advertiser API.

Every write goes draft → preview → confirm_and_apply. These tests drive that
whole gate: what the preview shows, what the dry run re-checks, that an apply
re-checks again and refuses a target edited in between, and the exact
requests that reach OpenAI.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from adloop.ads import write as ads_write
from adloop.chatgpt_ads import read, write
from adloop.chatgpt_ads.client import ChatGPTAdsApiError
from adloop.config import AdLoopConfig, ChatGPTAdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore

TZ = "America/Los_Angeles"


def _text(value) -> str:
    """A result as readable text (no \\u escapes), for message assertions."""
    return json.dumps(value, ensure_ascii=False, default=str)


class FakeOpenAIAds:
    """In-memory Advertiser API: lists, single-resource updates, status endpoints, creates, uploads."""

    def __init__(self):
        self.account = {"id": "adacct_1", "name": "Example Plumbing", "timezone": TZ, "currency_code": "USD", "status": "active"}
        self.campaigns = {
            "cmpn_1": {
                "id": "cmpn_1", "name": "Springfield Core", "status": "active", "bidding_type": "clicks",
                "budget": {"daily_spend_limit_micros": 35_000_000},
                "conversion_event_setting_ids": ["ces_lead"],
                "targeting": {
                    "locations": {"include": [{"id": "3000194", "type": "market", "name": "Springfield", "country_code": "US"}]},
                    "custom_audiences": {"ids": ["aud_1"]},
                    "platforms": {"included": ["ios_app", "web"]},
                },
                "landing_page_configuration": {"query_string_template": "utm_source=chatgpt&utm_medium=cpc"},
                "start_time": None, "end_time": None, "serving_issues": [],
            },
            "cmpn_2": {
                "id": "cmpn_2", "name": "Water Heaters", "status": "paused", "bidding_type": "conversions",
                "budget": {"lifetime_spend_limit_micros": 500_000_000},
                "conversion_event_setting_ids": ["ces_lead"], "targeting": {}, "serving_issues": [],
                "start_time": 1793606400, "end_time": 1796284800,
            },
            "cmpn_3": {"id": "cmpn_3", "name": "Old", "status": "archived", "bidding_type": "clicks",
                       "budget": {"daily_spend_limit_micros": 1_000_000}, "targeting": {}},
        }
        self.ad_groups = {
            "adgrp_1": {"id": "adgrp_1", "name": "Drain cleaning", "status": "active", "_campaign": "cmpn_1",
                        "context_hints": ["clogged drain", "sewer backup"],
                        "bidding_config": {"billing_event_type": "click", "strategy": "fixed_bid",
                                           "max_bid_micros": 8_000_000,
                                           "custom_audience_bid_multipliers": [{"custom_audience_id": "aud_1", "bid_multiplier_micros": 1_500_000}]},
                        "serving_issues": []},
            "adgrp_2": {"id": "adgrp_2", "name": "Tankless", "status": "paused", "_campaign": "cmpn_2",
                        "context_hints": [], "bidding_config": {"billing_event_type": "click", "strategy": "fixed_bid",
                                                                "max_bid_micros": 90_000_000},
                        "serving_issues": [{"code": "campaign_not_active"}]},
        }
        self.ads = {
            "ad_1": {"id": "ad_1", "name": "Drain $69", "status": "active", "_group": "adgrp_1",
                     "review_status": "approved",
                     "creative": {"type": "chat_card", "title": "Drain cleaning $69", "body": "Same-day drain service.",
                                  "target_url": "https://example.com/drain", "file_id": "file_1"},
                     "serving_issues": []},
            "ad_2": {"id": "ad_2", "name": "Tankless", "status": "paused", "_group": "adgrp_2",
                     "review_status": "rejected",
                     "creative": {"type": "chat_card", "title": "Tankless", "body": "x",
                                  "target_url": "https://example.com/tankless", "file_id": "file_2"},
                     "serving_issues": [{"code": "review_not_approved"}]},
            "ad_3": {"id": "ad_3", "name": "Carousel", "status": "paused", "_group": "adgrp_1",
                     "review_status": "approved",
                     "creative": {"type": "chat_card", "title": "Plumbing", "body": "y",
                                  "target_url": "https://example.com/", "file_ids": ["f1", "f2", "f3", "f4"]},
                     "serving_issues": []},
        }
        self.settings = [
            {"id": "ces_lead", "name": "Web Form Lead", "event_type": "lead_created", "archived": False},
            {"id": "ces_custom", "name": "Quote", "event_type": "custom", "custom_event_name": "quote", "archived": False},
            {"id": "ces_old", "name": "Old", "event_type": "page_viewed", "archived": True},
        ]
        self.calls: list[dict] = []
        self.created: dict[str, dict] = {}
        self.created_bodies: dict[str, dict] = {}
        self.lose_next_response = False
        self._next = 100

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _public(obj: dict) -> dict:
        return {k: copy.deepcopy(v) for k, v in obj.items() if not k.startswith("_")}

    def _store(self, collection: str) -> dict:
        return {"campaigns": self.campaigns, "ad_groups": self.ad_groups, "ads": self.ads}[collection]

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]

    # -- the patched api_request ----------------------------------------------
    def __call__(self, config, method, path, *, params=None, json_body=None, ad_account_id="", headers=None, files=None):
        params = dict(params or {})
        self.calls.append({"method": method, "path": path, "params": params, "body": copy.deepcopy(json_body),
                           "headers": dict(headers or {}), "files": files})
        parts = path.strip("/").split("/")
        page = lambda items: {"object": "list", "data": items, "has_more": False, "last_id": None}  # noqa: E731
        if method == "GET":
            if path == "ad_account":
                return dict(self.account)
            if path == "campaigns":
                return page([self._public(c) for c in self.campaigns.values()])
            if path == "ad_groups":
                return page([self._public(g) for g in self.ad_groups.values() if g["_campaign"] == params.get("campaign_id")])
            if path == "ads":
                return page([self._public(a) for a in self.ads.values() if a["_group"] == params.get("ad_group_id")])
            if path == "conversions/event_settings":
                return page(copy.deepcopy(self.settings))
            if path == "geo_lookup/search":
                return {"data": [{"id": "3000194", "name": "Springfield", "canonical_name": "Springfield,Illinois,US",
                                  "type": "market", "country_code": "US", "region_code": "IL", "parent_id": "2000006"}]}
        if method == "POST":
            if path == "upload":
                self._next += 1
                return {"file_id": f"file_up{self._next}"}
            if len(parts) == 1 and parts[0] in ("campaigns", "ad_groups", "ads"):
                key = (headers or {}).get("Idempotency-Key")
                if key and key in self.created:
                    # OpenAI: reuse the key AND the same request when retrying.
                    if self.created_bodies[key] != json_body:
                        raise ChatGPTAdsApiError("ChatGPT Ads API returned 409: idempotency key reused with a different request", status=409)
                    return self._public(self.created[key])
                self._next += 1
                prefix = {"campaigns": "cmpn", "ad_groups": "adgrp", "ads": "ad"}[parts[0]]
                obj = {"id": f"{prefix}_{self._next}", **copy.deepcopy(json_body)}
                if parts[0] == "ad_groups":
                    obj["_campaign"] = obj["campaign_id"]
                if parts[0] == "ads":
                    obj["_group"] = obj["ad_group_id"]
                    obj["review_status"] = "in_review"
                self._store(parts[0])[obj["id"]] = obj
                if self.lose_next_response:
                    self.lose_next_response = False
                    if key:
                        self.created[key] = obj
                        self.created_bodies[key] = copy.deepcopy(json_body)
                    raise ChatGPTAdsApiError("Could not reach the ChatGPT Ads API for POST /ads (ReadTimeout)")
                if key:
                    self.created[key] = obj
                    self.created_bodies[key] = copy.deepcopy(json_body)
                return self._public(obj)
            if len(parts) == 3 and parts[2] in ("pause", "activate", "archive"):
                obj = self._store(parts[0])[parts[1]]
                obj["status"] = {"pause": "paused", "activate": "active", "archive": "archived"}[parts[2]]
                return self._public(obj)
            if len(parts) == 2:
                obj = self._store(parts[0])[parts[1]]
                for k, v in (json_body or {}).items():
                    obj[k] = copy.deepcopy(v)
                if parts[0] == "ads" and "creative" in (json_body or {}):
                    obj["review_status"] = "in_review"
                return self._public(obj)
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def api():
    fake = FakeOpenAIAds()
    with patch("adloop.chatgpt_ads.client.api_request", side_effect=fake):
        yield fake


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    preview_store.set_plan_store(InMemoryPlanStore())
    read.reset_account_cache()
    monkeypatch.setattr("adloop.ads.write._validate_urls", lambda urls, timeout=10: ({u: None for u in urls}, {}))
    yield
    preview_store.set_plan_store(InMemoryPlanStore())
    read.reset_account_cache()


@pytest.fixture
def config(tmp_path):
    return AdLoopConfig(
        chatgpt_ads=ChatGPTAdsConfig(api_key="sk-ads"),
        safety=SafetyConfig(max_daily_budget=100.0, max_bid_increase_pct=100, require_dry_run=False,
                            log_file=str(tmp_path / "audit.log")),
    )


class _Image:
    def __init__(self, status=200, mime="image/png"):
        self.status_code = status
        self.headers = {"Content-Type": mime}

    def close(self):
        return None


class _Fetched:
    def __init__(self, data: bytes):
        self.data = data
        self.url = self.final_url = "https://cdn.example/image"


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _fetch(data: bytes = _PNG):
    """Stub AdLoop's public-only image fetcher (the real one is tested for SSRF below)."""
    return patch("adloop.ads.image_fetch.fetch_image_bytes", return_value=_Fetched(data))


def _apply(config, preview):
    """Dry run, then the real apply, through the actual gate."""
    dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
    assert dry["status"] == "DRY_RUN_SUCCESS", dry
    return ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_pause_campaign_end_to_end(self, api, config):
        preview = write.pause_chatgpt_entity(config, entity_type="campaign", entity_id="cmpn_1")
        assert preview["platform"] == "chatgpt" and preview["operation"] == "chatgpt_set_status"
        assert preview["changes"]["entity_name"] == "Springfield Core"
        assert preview["changes"]["current_status"] == "active" and preview["changes"]["target_status"] == "paused"
        assert any("stops every ad" in w for w in preview["warnings"])
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_SUCCESS" and dry["checks"]["status_now"] == "active"
        assert "ChatGPT Ads has no validate-only mode" in dry["note"]
        assert not api.writes()
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "APPLIED"
        assert [c["path"] for c in api.writes()] == ["campaigns/cmpn_1/pause"]
        assert api.campaigns["cmpn_1"]["status"] == "paused"

    def test_enable_names_what_still_blocks_serving(self, api, config):
        preview = write.enable_chatgpt_entity(config, entity_type="ad", entity_id="ad_2")
        joined = " ".join(preview["warnings"])
        assert "campaign 'Water Heaters' is paused" in joined
        assert "ad group 'Tankless' is paused" in joined
        assert "review status is rejected" in joined

    def test_archive_is_double_confirmed_and_refused_for_archived(self, api, config):
        preview = write.archive_chatgpt_entity(config, entity_type="ad_group", entity_id="adgrp_2")
        assert preview["requires_double_confirm"] is True
        assert preview["operation"] == "chatgpt_archive_entity"
        assert any("cannot be undone" in w for w in preview["warnings"])
        result = write.pause_chatgpt_entity(config, entity_type="campaign", entity_id="cmpn_3")
        assert "archived" in result["error"]

    def test_status_changed_after_the_draft_is_refused_at_apply(self, api, config):
        preview = write.pause_chatgpt_entity(config, entity_type="ad", entity_id="ad_1")
        api.ads["ad_1"]["status"] = "paused"  # someone paused it in Ads Manager
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "status changed since the draft" in result["error"]
        assert not api.writes()
        assert preview_store.get_plan(preview["plan_id"]) is not None

    def test_validation_and_unknown_entities(self, api, config):
        assert "entity_type" in _text(write.pause_chatgpt_entity(config, entity_type="keyword", entity_id="x"))
        assert "was not found" in write.pause_chatgpt_entity(config, entity_type="ad", entity_id="ad_404")["error"]

    def test_two_phase_apply_needs_a_dry_run_first(self, api, config):
        config.safety.two_phase_apply = True
        preview = write.pause_chatgpt_entity(config, entity_type="campaign", entity_id="cmpn_1")
        refused = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert refused["status"] == "DRY_RUN_REQUIRED" and not api.writes()
        assert _apply(config, preview)["status"] == "APPLIED"

    def test_blocked_operation(self, api, config):
        config.safety.blocked_operations = ["chatgpt_set_status"]
        assert "blocked" in write.pause_chatgpt_entity(config, entity_type="campaign", entity_id="cmpn_1")["error"]


# ---------------------------------------------------------------------------
# Campaign updates
# ---------------------------------------------------------------------------


class TestCampaignUpdate:
    def test_budget_change_sends_the_whole_budget(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=45)
        assert preview["changes"]["changes"]["daily_budget"] == {"from": 35.0, "to": 45.0, "currency": "USD"}
        assert preview["requires_double_confirm"] is False
        assert _apply(config, preview)["status"] == "APPLIED"
        call = api.writes()[-1]
        assert call["path"] == "campaigns/cmpn_1" and call["body"] == {"budget": {"daily_spend_limit_micros": 45_000_000}}

    def test_budget_cap_and_double_confirmation(self, api, config):
        assert "exceeds maximum" in write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=150)["error"]
        big = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=80)
        assert big["requires_double_confirm"] is True
        assert any("more than doubles" in w for w in big["warnings"])

    def test_daily_to_lifetime_is_refused_lifetime_to_daily_allowed(self, api, config):
        refused = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", lifetime_budget=500)
        assert "daily budget to a lifetime budget" in _text(refused)
        allowed = write.update_chatgpt_campaign(config, campaign_id="cmpn_2", daily_budget=20)
        assert any("lifetime to a daily budget" in w for w in allowed["warnings"])

    def test_lifetime_budget_capped_by_its_daily_equivalent(self, api, config):
        # cmpn_2 runs 31 days; 3,500 over 31 days is ~113/day, above the 100 cap.
        assert "exceeds maximum" in write.update_chatgpt_campaign(config, campaign_id="cmpn_2", lifetime_budget=3500)["error"]

    def test_location_change_keeps_audiences_and_platforms(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", location_ids=["3000194", "3000200"],
                                                excluded_location_ids=["4000001"])
        targeting = preview["changes"]["changes"]["targeting"]
        assert targeting["locations"]["from"] == ["Springfield (market)"]
        assert targeting["locations"]["to"] == ["id 3000194", "id 3000200"]
        _apply(config, preview)
        sent = api.writes()[-1]["body"]["targeting"]
        assert sent == {
            "locations": {"include": [{"id": "3000194"}, {"id": "3000200"}]},
            "excluded_locations": {"include": [{"id": "4000001"}]},
            "custom_audiences": {"ids": ["aud_1"]},
            "platforms": {"included": ["ios_app", "web"]},
        }

    def test_platforms_replace_and_clear(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", platforms=["desktop_web"])
        _apply(config, preview)
        assert api.writes()[-1]["body"]["targeting"]["platforms"] == {"included": ["desktop_web"]}
        cleared = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", clear_fields=["platforms"])
        _apply(config, cleared)
        assert "platforms" not in api.writes()[-1]["body"]["targeting"]

    def test_targeting_guards(self, api, config):
        assert "not both" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_1", location_ids=["1"], countries=["US"]))
        assert "platforms cannot be empty" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_1", platforms=[]))
        assert "smart_tv" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_1", platforms=["smart_tv"]))
        assert "target everywhere" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_1", location_ids=[]))

    def test_tracking_template_without_paid_medium_warns(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", query_string_template="utm_campaign={campaign_id}")
        assert any("paid utm_medium" in w for w in preview["warnings"])
        assert preview["changes"]["body"]["landing_page_configuration"] == {"query_string_template": "utm_campaign={campaign_id}"}

    def test_dates_are_account_local_days(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", start_date="2026-11-10", end_date="2026-11-30")
        body = preview["changes"]["body"]
        from datetime import datetime
        from zoneinfo import ZoneInfo

        local = ZoneInfo(TZ)
        assert body["start_time"] == int(datetime(2026, 11, 10, tzinfo=local).timestamp())
        # The campaign stops at the end of the 30th: midnight starting December 1st.
        assert body["end_time"] == int(datetime(2026, 12, 1, tzinfo=local).timestamp())
        assert preview["changes"]["changes"]["end_date"]["to"] == "2026-11-30"
        assert "after end_date" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_1", start_date="2026-12-01", end_date="2026-11-01"))

    def test_conversion_events(self, api, config):
        assert "ces_nope" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_1", conversion_event_setting_ids=["ces_nope"]))
        assert "cannot change" in _text(write.update_chatgpt_campaign(config, campaign_id="cmpn_2", conversion_event_setting_ids=["ces_lead"]))
        ok = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", conversion_event_setting_ids=["ces_lead", "ces_custom"])
        shown = ok["changes"]["changes"]["conversion_events"]
        assert shown["from"] == ["ces_lead"] and len(shown["to"]) == 2
        assert shown["to"][0] == "Web Form Lead (lead_created)" and shown["to"][1].startswith("Quote (")
        assert ok["changes"]["body"] == {"conversion_event_setting_ids": ["ces_lead", "ces_custom"]}

    def test_edit_in_ads_manager_after_the_draft_blocks_the_apply(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=40)
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_SUCCESS" and dry["checks"]["unchanged_since_draft"] is True
        api.campaigns["cmpn_1"]["budget"] = {"daily_spend_limit_micros": 60_000_000}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "budget changed since the draft" in result["error"]
        assert not api.writes()

    def test_unchanged_budget_is_nothing_to_change(self, api, config):
        assert "Nothing to change" in write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=35)["error"]
        assert "Nothing to change" in write.update_chatgpt_campaign(config, campaign_id="cmpn_2", lifetime_budget=500)["error"]

    def test_nothing_to_change_and_archived(self, api, config):
        assert "Nothing to change" in write.update_chatgpt_campaign(config, campaign_id="cmpn_1", name="Springfield Core")["error"]
        assert "archived" in write.update_chatgpt_campaign(config, campaign_id="cmpn_3", daily_budget=5)["error"]


# ---------------------------------------------------------------------------
# Ad group updates
# ---------------------------------------------------------------------------


class TestAdGroupUpdate:
    def test_bid_change_sends_the_whole_bidding_config(self, api, config):
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=10)
        assert preview["changes"]["changes"]["bid"]["from"] == "fixed 8.00 per click"
        assert preview["changes"]["changes"]["bid"]["to"] == "fixed 10.00 per click"
        _apply(config, preview)
        assert api.writes()[-1]["body"]["bidding_config"] == {
            "billing_event_type": "click", "strategy": "fixed_bid", "max_bid_micros": 10_000_000,
            "custom_audience_bid_multipliers": [{"custom_audience_id": "aud_1", "bid_multiplier_micros": 1_500_000}],
        }

    def test_bid_raise_cap(self, api, config):
        assert "exceeds maximum" in write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=17)["error"]

    def test_automatic_strategy_rules(self, api, config):
        auto = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", strategy="maximize_clicks")
        config_sent = auto["changes"]["body"]["bidding_config"]
        assert config_sent["strategy"] == "maximize_clicks" and "max_bid_micros" not in config_sent
        assert config_sent["custom_audience_bid_multipliers"] == []
        assert any("drops the audience bid multipliers" in w for w in auto["warnings"])
        wrong = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", strategy="maximize_conversions")
        assert "clicks campaign takes strategy" in _text(wrong)
        # adgrp_2's campaign has a lifetime budget: automatic bidding needs a daily one.
        no_daily = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_2", strategy="maximize_conversions")
        assert "needs a daily campaign budget" in _text(no_daily)
        both = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", strategy="maximize_clicks", max_bid=5)
        assert "leave max_bid empty" in _text(both)

    def test_conversion_campaign_bid_is_a_target_cpa(self, api, config):
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_2", max_bid=100)
        assert preview["changes"]["changes"]["bid"]["to"] == "fixed 100.00 target CPA"

    def test_context_hints_add_remove_and_replace(self, api, config):
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", add_context_hints=["hydro jetting", "Clogged Drain"],
                                                remove_context_hints=["sewer backup", "not there"])
        hints = preview["changes"]["changes"]["context_hints"]
        assert hints["added"] == ["hydro jetting"] and hints["removed"] == ["sewer backup"]
        assert any("nothing to remove" in w for w in preview["warnings"])
        _apply(config, preview)
        assert api.writes()[-1]["body"] == {"context_hints": ["clogged drain", "hydro jetting"]}
        replaced = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", context_hints=["drain repair"])
        assert replaced["changes"]["body"]["context_hints"] == ["drain repair"]
        mixed = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", context_hints=["a"], add_context_hints=["b"])
        assert "not both" in _text(mixed)

    def test_hints_edited_after_the_draft_block_the_apply(self, api, config):
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", add_context_hints=["water line"])
        api.ad_groups["adgrp_1"]["context_hints"] = ["something else"]
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED" and "context_hints changed since the draft" in result["error"]


# ---------------------------------------------------------------------------
# Ad updates
# ---------------------------------------------------------------------------


class TestAdUpdate:
    def test_copy_edit_sends_the_full_creative_and_goes_to_review(self, api, config):
        preview = write.update_chatgpt_ad(config, ad_id="ad_1", title="Drain cleaning from $69")
        assert preview["changes"]["changes"]["title"] == {"from": "Drain cleaning $69", "to": "Drain cleaning from $69"}
        assert any("new version for review" in w for w in preview["warnings"])
        result = _apply(config, preview)
        assert result["status"] == "APPLIED" and result["result"]["review_status"] == "in_review"
        assert result["result"]["updated_fields"] == ["creative"]
        assert api.writes()[-1]["body"] == {"creative": {
            "type": "chat_card", "title": "Drain cleaning from $69", "body": "Same-day drain service.",
            "target_url": "https://example.com/drain", "file_id": "file_1"}}

    def test_new_image_is_uploaded_at_apply_not_at_draft(self, api, config):
        with _fetch():
            preview = write.update_chatgpt_ad(config, ad_id="ad_1", image_url="https://cdn.example/drain.png")
            assert not api.writes()
            _apply(config, preview)
        paths = [c["path"] for c in api.writes()]
        assert paths == ["upload", "ads/ad_1"]
        assert api.writes()[0]["body"] == {"image_url": "https://cdn.example/drain.png"}
        assert api.writes()[1]["body"]["creative"]["file_id"].startswith("file_up")

    def test_limits_and_guards(self, api, config):
        assert "3–50" in _text(write.update_chatgpt_ad(config, ad_id="ad_1", title="x" * 51))
        assert "body must be 1–100 characters (got 101)" in _text(write.update_chatgpt_ad(config, ad_id="ad_1", body="y" * 101))
        with _fetch():
            multi = write.update_chatgpt_ad(config, ad_id="ad_3", image_url="https://cdn.example/x.png")
        assert "multi-image" in _text(multi)
        with _fetch(b"<html><body>page</body></html>"):
            assert "does not serve a JPEG" in _text(write.update_chatgpt_ad(config, ad_id="ad_1", image_url="https://example.com/page"))

    def test_unreachable_landing_page_is_refused(self, api, config, monkeypatch):
        monkeypatch.setattr("adloop.ads.write._validate_urls",
                            lambda urls, timeout=10: ({u: "HTTP 404" for u in urls}, {}))
        result = write.update_chatgpt_ad(config, ad_id="ad_1", target_url="https://example.com/gone")
        assert "not reachable" in _text(result)

    def test_multi_image_copy_edit_keeps_the_images(self, api, config):
        preview = write.update_chatgpt_ad(config, ad_id="ad_3", body="Licensed plumbers")
        creative = preview["changes"]["body"]["creative"]
        assert "file_id" not in creative and "file_ids" not in creative


# ---------------------------------------------------------------------------
# Creates
# ---------------------------------------------------------------------------


class TestCreates:
    def test_campaign_is_created_paused_with_tracking_and_an_idempotency_key(self, api, config):
        preview = write.draft_chatgpt_campaign(config, name="Springfield Water Heaters", daily_budget=25,
                                               location_ids=["3000194"], conversion_event_setting_ids=["ces_lead"])
        changes = preview["changes"]
        assert changes["status_on_create"] == "paused" and changes["query_string_template"] == write.DEFAULT_QUERY_TEMPLATE
        assert changes["conversion_events"] == ["Web Form Lead (lead_created)"]
        result = _apply(config, preview)
        call = api.writes()[-1]
        assert call["path"] == "campaigns" and call["headers"]["Idempotency-Key"] == f"adloop-{preview['plan_id']}"
        assert call["body"]["status"] == "paused" and call["body"]["budget"] == {"daily_spend_limit_micros": 25_000_000}
        assert call["body"]["targeting"] == {"locations": {"include": [{"id": "3000194"}]}}
        assert result["status"] == "APPLIED"

    def test_campaign_guards(self, api, config):
        assert "Location targeting is required" in _text(write.draft_chatgpt_campaign(config, name="No geo", daily_budget=10))
        assert "exactly one of daily_budget" in _text(write.draft_chatgpt_campaign(config, name="Two", daily_budget=10, lifetime_budget=100, countries=["US"]))
        assert "exceeds maximum" in write.draft_chatgpt_campaign(config, name="Big", daily_budget=500, countries=["US"])["error"]
        assert "needs an end_date" in _text(write.draft_chatgpt_campaign(config, name="Life", lifetime_budget=300, countries=["US"]))
        assert "exactly one conversion_event_setting_id" in _text(
            write.draft_chatgpt_campaign(config, name="Conv", bidding_type="conversions", daily_budget=20, countries=["US"]))
        assert "standard event" in _text(
            write.draft_chatgpt_campaign(config, name="Conv", bidding_type="conversions", daily_budget=20, countries=["US"],
                                         conversion_event_setting_ids=["ces_custom"]))
        warn = write.draft_chatgpt_campaign(config, name="Clicks only", daily_budget=20, countries=["us"])
        assert any("report 0 conversions" in w for w in warn["warnings"])
        assert warn["changes"]["body"]["targeting"]["locations"] == {"countries": ["US"]}

    def test_retried_create_does_not_duplicate(self, api, config):
        preview = write.draft_chatgpt_campaign(config, name="Retry me", daily_budget=10, countries=["US"])
        plan = preview_store.get_plan(preview["plan_id"])
        first = write.apply_plan(config, plan)
        second = write.apply_plan(config, plan)
        assert first["campaign_id"] == second["campaign_id"]
        assert sum(1 for c in api.campaigns.values() if c["name"] == "Retry me") == 1

    def test_ad_group_defaults_to_automatic_bidding_on_a_daily_budget(self, api, config):
        preview = write.draft_chatgpt_ad_group(config, campaign_id="cmpn_1", name="Water heaters",
                                               context_hints=["water heater repair"])
        assert preview["changes"]["bid"] == "maximize_clicks"
        _apply(config, preview)
        body = api.writes()[-1]["body"]
        assert body["status"] == "paused" and body["bidding_config"] == {
            "billing_event_type": "click", "strategy": "maximize_clicks", "custom_audience_bid_multipliers": []}
        fixed = write.draft_chatgpt_ad_group(config, campaign_id="cmpn_2", name="Fixed", max_bid=80)
        assert fixed["changes"]["bid"] == "fixed 80.00 target CPA"
        assert "archived" in write.draft_chatgpt_ad_group(config, campaign_id="cmpn_3", name="Nope")["error"]

    def test_ad_uploads_the_image_then_creates_paused(self, api, config):
        with _fetch():
            preview = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Water heater install",
                                             body="Same-day installs across Springfield.",
                                             target_url="https://example.com/water-heaters",
                                             image_url="https://cdn.example/wh.png")
            assert preview["changes"]["status_on_create"] == "paused" and not api.writes()
            _apply(config, preview)
        upload, create = api.writes()
        assert upload["path"] == "upload" and create["path"] == "ads"
        assert create["body"]["creative"]["file_id"].startswith("file_up")
        assert create["body"]["status"] == "paused" and create["headers"]["Idempotency-Key"].startswith("adloop-")

    def test_ad_from_a_local_file(self, api, config, tmp_path):
        image = tmp_path / "card.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        preview = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                         target_url="https://example.com/drain", image_path=str(image))
        _apply(config, preview)
        upload = api.writes()[0]
        assert upload["path"] == "upload" and upload["files"]["file"][0] == "card.png"
        assert upload["files"]["file"][2] == "image/png"

    def test_ad_guards(self, api, config, monkeypatch):
        no_image = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                          target_url="https://example.com/drain")
        assert "An image is required" in _text(no_image)
        two = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                     target_url="https://example.com/drain", file_id="file_1", image_url="https://x/y.png")
        assert "one image source" in _text(two)
        monkeypatch.setattr("adloop.runtime.deployment_mode", lambda: "server")
        hosted = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                        target_url="https://example.com/drain", image_path="/tmp/x.png")
        assert "hosted server" in _text(hosted)

    def test_reused_file_id_skips_the_upload(self, api, config):
        preview = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                         target_url="https://example.com/drain", file_id="file_1")
        _apply(config, preview)
        assert [c["path"] for c in api.writes()] == ["ads"]
        assert api.writes()[0]["body"]["creative"]["file_id"] == "file_1"

    def test_parent_archived_after_the_draft_fails_the_dry_run(self, api, config):
        preview = write.draft_chatgpt_ad_group(config, campaign_id="cmpn_1", name="Later")
        api.campaigns["cmpn_1"]["status"] = "archived"
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_FAILED" and "archived" in dry["error"]


# ---------------------------------------------------------------------------
# Location lookup, server wrappers, approval surfaces
# ---------------------------------------------------------------------------


class TestSurroundings:
    def test_location_search(self, api, config):
        result = read.search_chatgpt_locations(config, query="Springfield")
        assert result["locations"][0]["location_id"] == "3000194"
        assert api.calls[-1]["params"] == {"q": "Springfield", "limit": 20}
        with pytest.raises(ValueError):
            read.search_chatgpt_locations(config, query=" ")

    def test_server_tools_return_previews(self, api, config):
        from adloop import runtime, server

        runtime.set_default_config(config)
        try:
            preview = server.update_chatgpt_campaign(campaign_id="cmpn_1", daily_budget=40)
            assert preview["status"] == "PENDING_CONFIRMATION" and preview["platform"] == "chatgpt"
            assert server.search_chatgpt_locations(query="Springfield")["total"] == 1
        finally:
            runtime.set_default_config(None)

    def test_dialog_and_card_name_the_platform(self, api, config):
        from pathlib import Path

        import adloop.safety as safety_pkg
        from adloop.safety.approval import _platform_label, confirmation_message

        preview = write.pause_chatgpt_entity(config, entity_type="campaign", entity_id="cmpn_1")
        plan = preview_store.get_plan(preview["plan_id"])
        assert _platform_label(plan) == "ChatGPT Ads"
        message = confirmation_message(plan)
        assert message.splitlines()[0].endswith(" in ChatGPT Ads?")
        html = (Path(safety_pkg.__file__).parent / "approval_card.html").read_text()
        assert 'chatgpt: "ChatGPT Ads"' in html and 'op.startsWith("chatgpt_")' in html

    def test_api_error_at_apply_keeps_the_plan(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=40)
        original = api.__call__

        def failing(*args, **kwargs):
            if args[1] == "POST":
                raise ChatGPTAdsApiError("ChatGPT Ads API returned 400: budget below bid", status=400)
            return original(*args, **kwargs)

        with patch("adloop.chatgpt_ads.client.api_request", side_effect=failing):
            result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "budget below bid" in result["error"]
        assert preview_store.get_plan(preview["plan_id"]) is not None


class TestGateFindings:
    def test_operation_blocked_after_the_dry_run_cannot_apply(self, api, config):
        # Codex round two, finding 1: the blocklist is checked again at confirm_and_apply.
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_1", daily_budget=40)
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        config.safety.blocked_operations = ["chatgpt_update_campaign"]
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "BLOCKED" and not api.writes()
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "BLOCKED"


class TestRoundThreeFindings:
    """Codex review round three (adloop-audit-2026-11-04.md), ChatGPT findings 1, 2, 3 and 9."""

    def test_key_moved_to_another_account_after_the_dry_run_sends_nothing(self, api, config):
        # Finding 1: the preview named account A; the key now answers for B.
        preview = write.draft_chatgpt_campaign(config, name="Pinned", daily_budget=10, countries=["US"])
        assert preview["customer_id"] == "adacct_1"
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_SUCCESS"
        api.account["id"] = "adacct_2"
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "now belongs to account adacct_2" in _text(result)
        assert not api.writes()

    @pytest.mark.parametrize("url", ["http://127.0.0.1:9876/private-action", "http://[::1]/x.png",
                                     "http://169.254.169.254/latest/meta-data"])
    def test_private_image_urls_are_refused_before_any_request(self, api, config, url):
        # Finding 2: the real public-only fetcher is used, not a bare GET.
        with patch("adloop.ads.image_fetch._open_connection") as connect:
            result = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                            target_url="https://example.com/drain", image_url=url)
        assert "image_url cannot be used" in _text(result) and "plan_id" not in result
        connect.assert_not_called()

    def test_a_hostname_resolving_privately_is_refused(self, api, config, monkeypatch):
        import socket

        monkeypatch.setattr("adloop.ads.image_fetch.socket.getaddrinfo",
                            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80))])
        with patch("adloop.ads.image_fetch._open_connection") as connect:
            result = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                            target_url="https://example.com/drain",
                                            image_url="http://images.internal.example/card.png")
        assert "non-public address" in _text(result)
        connect.assert_not_called()

    def test_a_page_that_is_not_an_image_is_refused(self, api, config):
        with _fetch(b"<html>not an image</html>"):
            result = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Drain help", body="Fast.",
                                            target_url="https://example.com/drain",
                                            image_url="https://cdn.example/card.png")
        assert "does not serve a JPEG, PNG or WebP image" in _text(result)

    def test_shortening_a_lifetime_flight_over_the_cap_is_refused_at_draft(self, api, config):
        # Finding 3: $500 lifetime from Nov 2; ending Nov 3 is about $290 a day against a $100 cap.
        result = write.update_chatgpt_campaign(config, campaign_id="cmpn_2", end_date="2026-11-03")
        assert "plan_id" not in result and "Daily budget 250.00 exceeds maximum 100.00" in _text(result)

    def test_flight_change_rechecked_against_the_cap_at_apply(self, api, config):
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_2", end_date="2026-12-20")
        assert "plan_id" in preview, preview
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        config.safety.max_daily_budget = 5.0
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "exceeds maximum 5.00" in _text(result)
        assert not api.writes()

    def test_flight_start_moved_in_ads_manager_is_rechecked_at_apply(self, api, config):
        # The plan changes only the end; a later start in Ads Manager squeezes the same $500.
        preview = write.update_chatgpt_campaign(config, campaign_id="cmpn_2", end_date="2026-12-20")
        api.campaigns["cmpn_2"]["start_time"] = preview["changes"]["body"]["end_time"] - 2 * 86400
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "start_time changed since the draft" in _text(result)
        assert not api.writes()

    def test_lost_ad_response_retries_with_the_same_file_and_body(self, api, config):
        # Finding 9: the retry reuses the uploaded file, so the body under the key is identical.
        with _fetch():
            preview = write.draft_chatgpt_ad(config, ad_group_id="adgrp_1", title="Water heater install",
                                             body="Same-day installs across Springfield.",
                                             target_url="https://example.com/water-heaters",
                                             image_url="https://cdn.example/wh.png")
            assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
            api.lose_next_response = True
            first = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
            assert first.get("status") != "APPLIED" and "ReadTimeout" in _text(first)
            second = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert second["status"] == "APPLIED", second
        uploads = [c for c in api.writes() if c["path"] == "upload"]
        creates = [c for c in api.writes() if c["path"] == "ads"]
        assert len(uploads) == 1 and len(creates) == 2
        assert creates[0]["body"] == creates[1]["body"]
        assert creates[0]["headers"]["Idempotency-Key"] == creates[1]["headers"]["Idempotency-Key"]
        assert sum(1 for a in api.ads.values() if a.get("name") == creates[0]["body"].get("name")) == 1


class TestUpstreamAuditFindings:
    """Codex audit of the upstream ports (adloop-audit-2026-11-04-upstream.md)."""

    def _impressions(self, api, micros=4000):
        api.campaigns["cmpn_1"]["bidding_type"] = "impressions"
        api.ad_groups["adgrp_1"]["bidding_config"] = {"billing_event_type": "impression", "strategy": "fixed_bid",
                                                      "max_bid_micros": micros, "custom_audience_bid_multipliers": []}

    def test_sub_cent_bid_raise_is_capped_at_the_draft(self, api, config):
        # Finding 1: $0.004 → $0.04 per impression is +900%, not "0.0 → 0.04".
        self._impressions(api)
        config.safety.max_bid_increase_pct = 50
        result = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=0.04)
        assert "plan_id" not in result and "Bid increase 900% exceeds maximum 50%" in _text(result)

    def test_sub_cent_bids_are_shown_exactly(self, api, config):
        self._impressions(api)
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=0.005)
        assert preview["changes"]["changes"]["bid"]["from"] == "fixed 0.004 per impression"
        assert preview["changes"]["changes"]["bid"]["to"] == "fixed 0.005 per impression"

    def test_sub_cent_bid_raise_is_capped_again_at_apply(self, api, config):
        self._impressions(api)
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=0.005)  # +25%
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        config.safety.max_bid_increase_pct = 10
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "Bid increase 25% exceeds maximum 10%" in _text(result)
        assert not api.writes()

    # -- delta audit finding 6: the cap boundary in whole micros
    def test_an_increase_of_exactly_the_cap_passes(self, api, config):
        self._impressions(api, micros=1004)
        config.safety.max_bid_increase_pct = 50
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=0.001506)  # exactly +50%
        assert "plan_id" in preview, preview
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"

    def test_one_micro_over_the_cap_is_refused(self, api, config):
        self._impressions(api, micros=1004)
        config.safety.max_bid_increase_pct = 50
        result = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=0.001507)
        assert "plan_id" not in result and "exceeds maximum 50%" in _text(result)

    def test_inventory_and_preview_show_the_same_sub_cent_bid(self, api, config):
        self._impressions(api)
        group = next(g for g in read.get_chatgpt_ad_groups(config, campaign_id="cmpn_1")["ad_groups"]
                     if g["ad_group_id"] == "adgrp_1")
        preview = write.update_chatgpt_ad_group(config, ad_group_id="adgrp_1", max_bid=0.005)
        assert group["bidding"]["max_bid"] == 0.004
        assert preview["changes"]["changes"]["bid"]["from"] == "fixed 0.004 per impression"
