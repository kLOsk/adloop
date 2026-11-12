"""ChatGPT Ads (OpenAI Advertiser API): config, client, reads and diagnostics against a fake API."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from adloop.chatgpt_ads import client, read
from adloop.chatgpt_ads.client import ChatGPTAdsApiError, ChatGPTAdsAuthError
from adloop.config import AdLoopConfig, ChatGPTAdsConfig, load_config

TZ = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(client.API_KEY_ENV, raising=False)
    read.reset_account_cache()
    yield
    read.reset_account_cache()


@pytest.fixture
def config():
    return AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="sk-ads-default"))


# ---------------------------------------------------------------------------
# Fake API
# ---------------------------------------------------------------------------

_ACCOUNT = {
    "id": "adacct_1", "name": "Example Plumbing Inc", "account_name": "Example Plumbing",
    "brand_name": "Example Plumbing", "url": "https://example.com", "status": "active",
    "timezone": TZ, "currency_code": "USD", "review": {"status": "approved"},
}

_CAMPAIGNS = [
    {
        "id": "cmpn_1", "name": "Springfield Core", "status": "active", "bidding_type": "clicks",
        "budget": {"daily_spend_limit_micros": 25_000_000},
        "conversion_event_setting_ids": ["ces_lead"],
        "targeting": {"locations": {"include": [
            {"id": "62704", "name": "62704", "type": "postal_code", "country_code": "US", "region_code": None},
        ]}},
        "landing_page_configuration": {"query_string_template": "utm_campaign={campaign_id}"},
        "serving_issues": [{"code": "partial_serving_ad_in_review"}],
    },
    {
        "id": "cmpn_2", "name": "Water Heaters", "status": "paused", "bidding_type": "clicks",
        "budget": {"lifetime_spend_limit_micros": 500_000_000},
        "conversion_event_setting_ids": [], "targeting": {},
        "serving_issues": [{"code": "campaign_not_active"}],
    },
    {"id": "cmpn_3", "name": "Old test", "status": "archived", "bidding_type": "impressions",
     "budget": {}, "conversion_event_setting_ids": []},
]

_AD_GROUPS = {
    "cmpn_1": [
        {"id": "adgrp_1", "name": "Drain cleaning", "status": "active",
         "context_hints": ["clogged drain Springfield"],
         "bidding_config": {"billing_event_type": "click", "strategy": "fixed_bid", "max_bid_micros": 8_000_000},
         "serving_issues": []},
        {"id": "adgrp_2", "name": "Water heater flush", "status": "active", "context_hints": [],
         "bidding_config": {"billing_event_type": "click", "max_bid_micros": 5_000_000},
         "serving_issues": [{"code": "ad_group_has_no_ads"}]},
    ],
    "cmpn_2": [
        {"id": "adgrp_3", "name": "Tankless", "status": "active", "context_hints": ["tankless"],
         "bidding_config": {"billing_event_type": "click", "max_bid_micros": 6_000_000},
         "serving_issues": [{"code": "campaign_not_active"}]},
    ],
}

_ADS = {
    "adgrp_1": [
        {"id": "ad_1", "name": "Drain $69", "status": "active", "review_status": "approved",
         "creative": {"type": "chat_card", "title": "Drain cleaning", "body": "Sewer drains",
                      "target_url": "https://example.com/drain?utm_source=chatgpt&utm_medium=cpc",
                      "image_url": "https://cdn/x.png"},
         "landing_page_configuration": {"query_string_template": "utm_content={ad_id}"},
         "serving_issues": []},
        {"id": "ad_2", "name": "Drain v2", "status": "active", "review_status": "rejected",
         "review": {"status": "rejected", "reason": "crawler_404"},
         "creative": {"type": "chat_card", "title": "Drains", "body": "x",
                      "target_url": "https://example.com/missing"},
         "serving_issues": [{"code": "review_not_approved"}]},
    ],
    "adgrp_2": [],
    "adgrp_3": [
        {"id": "ad_3", "name": "Tankless $1000", "status": "active", "review_status": "approved",
         "creative": {"type": "chat_card", "title": "Tankless", "body": "x",
                      "target_url": "https://example.com/tankless?utm_source=chatgpt&utm_medium=cpc"},
         "serving_issues": [{"code": "campaign_not_active"}]},
    ],
}

_SETTINGS = [
    {"id": "ces_lead", "name": "Web Form Lead", "event_type": "lead_created", "custom_event_name": None,
     "attribution_window_days": 30, "source_ids": ["clidsrc_1"], "sources": [{"id": "clidsrc_1", "name": "Site"}],
     "campaigns": [{"id": "cmpn_1", "name": "Springfield Core"}], "archived": False, "version": 1},
    {"id": "ces_pv", "name": "Page views", "event_type": "page_viewed", "custom_event_name": None,
     "attribution_window_days": 30, "source_ids": ["clidsrc_1"], "sources": [{"id": "clidsrc_1", "name": "Site"}],
     "campaigns": [], "archived": False, "version": 1},
]

_PIXELS = [{"id": "clidsrc_1", "name": "Site", "client_type": "web", "pixel_id": "px1"}]


extras: list[dict] = []


def _page(items):
    return {"object": "list", "data": items, "first_id": None, "last_id": None, "has_more": False}


def _fake_api(extra: dict | None = None):
    """Route (METHOD, path) → payload or callable(params, body) for client.api_request.

    ``extras`` (module level) records each call's headers and multipart files.
    """
    calls: list[tuple[str, str, dict | None, dict | None, str]] = []
    extras.clear()

    def default_insights(params, body):
        level = params.get("aggregation_level")
        if level == "campaign":
            return _page([
                {"campaign_id": "cmpn_1", "campaign_name": "Springfield Core", "impressions": 4000,
                 "clicks": 40, "spend": 320.5, "ctr": 0.01, "cpc": 8.01, "cpm": 80.1,
                 "conversions": 0, "cpa": None},
                {"campaign_id": "cmpn_2", "campaign_name": "Water Heaters", "campaign_impressions": 500,
                 "campaign_clicks": 5, "campaign_spend": 41.0, "conversions": 1, "cpa": 41.0},
            ])
        if level == "ad_account":
            return _page([{"impressions": 4500, "clicks": 45, "spend": 361.5, "conversions": 1}])
        return _page([])

    def default_conversions(params, body):
        return {"object": "list", "account_currency": "USD", "count": 2, "data": [
            {"entity_id": "cmpn_1", "conversions": 0, "click_through_conversions": 0,
             "view_through_conversions": 0, "order_created_attributed_sales": None,
             "attributed_events": [
                 {"entity_id": "cmpn_1", "event_name": "page_viewed", "event_kind": "standard",
                  "attributed_event_count": 31, "click_through_conversions": 31, "view_through_conversions": 0,
                  "attributed_event_value_amount": None, "attributed_event_value_count": 0,
                  "attributed_event_value_currency": None, "conversion_event_setting_breakdowns": []},
             ]},
            {"entity_id": "cmpn_2", "conversions": 1, "click_through_conversions": 0,
             "view_through_conversions": 1, "order_created_attributed_sales": "0",
             "attributed_events": []},
        ]}

    routes: dict = {
        ("GET", "ad_account"): _ACCOUNT,
        ("GET", "ad_accounts"): _page([_ACCOUNT]),
        ("GET", "campaigns"): _page(_CAMPAIGNS),
        ("GET", "ad_groups"): lambda p, b: _page(_AD_GROUPS.get(p["campaign_id"], [])),
        ("GET", "ads"): lambda p, b: _page(_ADS.get(p["ad_group_id"], [])),
        ("GET", "conversions/event_settings"): _page(_SETTINGS),
        ("GET", "conversions/pixels"): _page(_PIXELS),
        ("GET", "conversions/events"): {"object": "list", "data": [
            {"data_source_id": "clidsrc_1", "event_type": "page_viewed", "custom_event_name": None,
             "action_source": "web", "event_timestamp_ms": 1, "received_at_ms": 2,
             "api_channel": "pixel_sdk", "event_data_json": "{}"},
        ]},
        ("GET", "ad_account/spend_limit_windows"): ChatGPTAdsApiError("forbidden", status=403),
        ("GET", "ad_account/insights"): default_insights,
        ("POST", "conversions/insights"): default_conversions,
    }
    routes.update(extra or {})

    def fake(config, method, path, *, params=None, json_body=None, ad_account_id="", headers=None, files=None,
             mutation=False):
        calls.append((method, path, params, json_body, ad_account_id))
        extras.append({"path": path, "headers": headers or {}, "files": files})
        handler = routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected ChatGPT Ads call {method} {path}")
        if isinstance(handler, Exception):
            raise handler
        return handler(params or {}, json_body) if callable(handler) else handler

    return calls, patch("adloop.chatgpt_ads.client.api_request", side_effect=fake)


# ---------------------------------------------------------------------------
# Config and key resolution
# ---------------------------------------------------------------------------


class TestConfig:
    def test_block_parses_default_key_and_accounts(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "chatgpt_ads:\n"
            "  api_key: sk-default\n"
            "  ad_account_id: adacct_1\n"
            "  accounts:\n"
            "    adacct_2: sk-two\n"
            "    adacct_3: ''\n"
        )
        cfg = load_config(str(path)).chatgpt_ads
        assert cfg.api_key == "sk-default"
        assert cfg.ad_account_id == "adacct_1"
        assert cfg.accounts == {"adacct_2": "sk-two"}

    def test_missing_block_is_empty(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ga4:\n  property_id: '1'\n")
        cfg = load_config(str(path)).chatgpt_ads
        assert cfg.api_key == "" and cfg.accounts == {}


class TestKeyResolution:
    def test_named_account_uses_its_own_key(self):
        cfg = AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="d", accounts={"adacct_2": "two"}))
        assert client.local_api_key(cfg, "adacct_2") == "two"
        assert client.local_api_key(cfg, "") == "d"

    def test_environment_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv(client.API_KEY_ENV, "from-env")
        assert client.local_api_key(AdLoopConfig(), "") == "from-env"

    def test_unknown_account_is_refused_when_the_default_names_another(self):
        cfg = AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="d", ad_account_id="adacct_1"))
        with pytest.raises(ChatGPTAdsAuthError) as err:
            client.local_api_key(cfg, "adacct_9")
        assert err.value.error_code == "unknown_account"

    def test_single_named_account_is_the_default(self):
        cfg = AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(accounts={"adacct_2": "two"}))
        assert client.local_api_key(cfg, "") == "two"

    def test_several_named_accounts_need_a_choice(self):
        cfg = AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(accounts={"adacct_2": "a", "adacct_3": "b"}))
        with pytest.raises(ChatGPTAdsAuthError) as err:
            client.local_api_key(cfg, "")
        assert err.value.error_code == "account_required"

    def test_no_key_explains_where_to_create_one(self):
        with pytest.raises(ChatGPTAdsAuthError) as err:
            client.local_api_key(AdLoopConfig(), "")
        assert err.value.error_code == "missing_key"
        assert "Settings" in str(err.value) and "not a platform.openai.com key" in str(err.value)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, payload=None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"x" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class TestClient:
    def test_sends_bearer_key_and_list_params(self, config):
        with patch("requests.request", return_value=_Resp(200, {"id": "adacct_1"})) as req:
            client.api_get(config, "ad_account/insights", {"fields[]": ["clicks", "spend"]})
        args, kwargs = req.call_args
        assert args == ("GET", "https://api.ads.openai.com/v1/ad_account/insights")
        assert kwargs["headers"]["Authorization"] == "Bearer sk-ads-default"
        assert kwargs["params"] == {"fields[]": ["clicks", "spend"]}

    def test_401_is_an_auth_error(self, config):
        with patch("requests.request", return_value=_Resp(401, {"error": {"message": "bad key"}})):
            with pytest.raises(ChatGPTAdsAuthError) as err:
                client.api_get(config, "ad_account")
        assert err.value.error_code == "invalid_key"
        assert "bad key" in str(err.value)

    def test_short_rate_limit_is_retried(self, config):
        responses = [_Resp(429, {}, {"Retry-After": "1"}), _Resp(200, {"ok": True})]
        with patch("requests.request", side_effect=responses), patch("time.sleep") as sleep:
            assert client.api_get(config, "campaigns") == {"ok": True}
        sleep.assert_called_once_with(1)

    def test_long_rate_limit_surfaces_with_retry_after(self, config):
        with patch("requests.request", return_value=_Resp(429, {}, {"Retry-After": "60"})):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_get(config, "campaigns")
        assert err.value.status == 429 and err.value.retry_after == 60

    def test_error_detail_is_extracted(self, config):
        payload = {"error": {"message": "Invalid value", "param": "fields", "code": "invalid_request"}}
        with patch("requests.request", return_value=_Resp(400, payload)):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_get(config, "ad_account/insights")
        assert err.value.status == 400
        assert "Invalid value (param: fields)" in str(err.value)

    def test_pagination_follows_last_id(self, config):
        pages = [
            {"data": [{"id": "a"}], "has_more": True, "last_id": "a"},
            {"data": [{"id": "b"}], "has_more": False, "last_id": "b"},
        ]
        seen = []

        def fake(config, method, path, *, params=None, json_body=None, ad_account_id=""):
            seen.append(dict(params))
            return pages[len(seen) - 1]

        with patch("adloop.chatgpt_ads.client.api_request", side_effect=fake):
            items = client.api_get_all(config, "campaigns")
        assert [i["id"] for i in items] == ["a", "b"]
        assert "after" not in seen[0] and seen[1]["after"] == "a"
        assert seen[0]["limit"] == 500

    def test_micros(self):
        assert client.from_micros(25_000_000) == 25.0
        assert client.from_micros(None) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_window_defaults_to_30_days_ending_yesterday(self):
        window = read.report_window("", "", TZ)
        today = read._today(TZ)
        assert window["end"] == (today - timedelta(days=1)).isoformat()
        assert window["days"] == 30
        assert window["end_ts"] - window["start_ts"] in (30 * 86400, 30 * 86400 + 3600, 30 * 86400 - 3600)

    def test_window_reaching_today_ends_yesterday_with_a_note(self):
        today = read._today(TZ)
        window = read.report_window((today - timedelta(days=3)).isoformat(), today.isoformat(), TZ)
        assert window["end"] == (today - timedelta(days=1)).isoformat()
        assert window["notes"]

    def test_window_rejects_bad_ranges(self):
        with pytest.raises(ValueError):
            read.report_window("2026-10-10", "2026-10-01", TZ)
        with pytest.raises(ValueError):
            read.report_window("2025-01-01", "2026-06-01", TZ)
        with pytest.raises(ValueError):
            read.report_window("10/01/2026", "", TZ)

    def test_unix_range_uses_local_midnight(self):
        window = read.report_window("2026-10-01", "2026-10-01", TZ)
        payload = json.loads(read._unix_range(window))
        assert payload == {"type": "unix_range", "start": 1790838000, "end": 1790924400}
        assert json.loads(read._unix_range(window, as_strings=True))["start"] == "1790838000"

    def test_tracking_templates_override_the_url_from_campaign_to_ad(self):
        params = read.effective_tracking(
            "https://x.com/p?utm_source=url&utm_medium=cpc&keep=1",
            "utm_source=campaign&utm_campaign=c",
            "utm_source=group",
            "utm_content={ad_id}",
        )
        assert params == {
            "utm_source": "group", "utm_medium": "cpc", "keep": "1",
            "utm_campaign": "c", "utm_content": "{ad_id}",
        }

    def test_serving_state(self):
        assert read.serving_state("paused", []) == "paused"
        assert read.serving_state("active", []) == "eligible"
        blocking = read._issues({"serving_issues": [{"code": "ad_account_payment_method_missing_or_unauthorized"}]})
        assert read.serving_state("active", blocking).startswith("not serving: the account has no authorized")
        partial = read._issues({"serving_issues": [{"code": "partial_serving_ad_in_review"}]})
        assert read.serving_state("active", partial).startswith("serving with limits")

    def test_key_answering_for_another_account_is_refused(self, config):
        _, ctx = _fake_api()
        with ctx, pytest.raises(ValueError, match="belongs to ad account adacct_1"):
            read.account_meta(config, "adacct_other")


# ---------------------------------------------------------------------------
# Account and structure
# ---------------------------------------------------------------------------


class TestAccountAndStructure:
    def test_account_reports_reviews_counts_and_hides_unreadable_limits(self, config):
        _, ctx = _fake_api({
            ("GET", "ad_account"): {**_ACCOUNT, "review": {"status": "in_review"}},
            ("GET", "campaigns"): _page([{**_CAMPAIGNS[0], "serving_issues": [
                {"code": "ad_account_persona_verification_not_approved"}]}]),
        })
        with ctx:
            result = read.get_chatgpt_ads_account(config)
        assert result["currency"] == "USD" and result["time_zone"] == TZ
        assert result["campaigns_by_status"] == {"active": 1}
        assert result["spend_limits"] is None and result["spend_limits_note"]
        joined = " ".join(result["insights"])
        assert "Brand review is in_review" in joined
        assert "identity verification" in joined

    def test_campaigns_flag_missing_events_and_hide_archived(self, config):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_campaigns(config)
        names = [c["name"] for c in result["campaigns"]]
        assert names == ["Springfield Core", "Water Heaters"]
        core = result["campaigns"][0]
        assert core["budget"] == {"daily": 25.0, "lifetime": None}
        assert core["conversion_events"] == ["Web Form Lead (lead_created)"]
        assert core["targeting"]["locations"] == ["62704 (postal_code)"]
        assert core["serving_state"].startswith("serving with limits")
        joined = " ".join(result["insights"])
        assert "'Water Heaters' has no conversion event" in joined
        assert "'Water Heaters' has no location targeting" in joined

    def test_ad_groups_carry_campaign_status_and_bid_meaning(self, config):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_ad_groups(config)
        by_name = {g["name"]: g for g in result["ad_groups"]}
        assert by_name["Drain cleaning"]["bidding"]["max_bid"] == 8.0
        assert by_name["Drain cleaning"]["bidding"]["max_bid_meaning"] == "maximum per click"
        assert by_name["Tankless"]["campaign_status"] == "paused"
        assert by_name["Tankless"]["serving_state"].startswith("not serving: the campaign is paused")
        assert any("'Water heater flush' has no ads" in i for i in result["insights"])

    def test_ad_groups_of_one_campaign(self, config):
        calls, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_ad_groups(config, campaign_id="cmpn_2")
        assert [g["name"] for g in result["ad_groups"]] == ["Tankless"]
        assert [c[2]["campaign_id"] for c in calls if c[1] == "ad_groups"] == ["cmpn_2"]

    def test_ads_show_review_state_and_click_parameters(self, config):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_ads(config, campaign_id="cmpn_1")
        by_id = {a["ad_id"]: a for a in result["ads"]}
        assert by_id["ad_1"]["click_parameters"] == {
            "utm_source": "chatgpt", "utm_medium": "cpc",
            "utm_campaign": "{campaign_id}", "utm_content": "{ad_id}",
        }
        assert by_id["ad_2"]["review_reason"] == "crawler_404"
        joined = " ".join(result["insights"])
        assert "rejected in review" in joined and "crawler_404" in joined
        assert "1 ad(s) send clicks without utm_source" in joined


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_campaign_rows_request_conversions_and_join_status(self, config):
        calls, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_performance(config, date_range_start="2026-10-01", date_range_end="2026-10-30")
        params = next(c[2] for c in calls if c[1] == "ad_account/insights")
        assert params["aggregation_level"] == "campaign"
        assert params["time_granularity"] == "none"
        assert "conversions" in params["fields[]"] and "campaign_id" in params["fields[]"]
        assert json.loads(params["time_ranges[]"][0])["type"] == "unix_range"
        assert params["attribution_window_days"] == 30

        rows = {r["campaign_id"]: r for r in result["rows"]}
        assert rows["cmpn_1"]["spend"] == 320.5 and rows["cmpn_1"]["campaign_status"] == "active"
        # Prefixed wire keys (campaign_clicks) are read too.
        assert rows["cmpn_2"]["clicks"] == 5 and rows["cmpn_2"]["spend"] == 41.0
        assert rows["cmpn_2"]["conversion_events_attached"] is False
        assert result["totals"]["spend"] == 361.5 and result["totals"]["conversions"] == 1
        joined = " ".join(result["insights"])
        assert "1 campaign(s) spent 320.5 USD with zero conversions: Springfield Core" in joined
        assert "paused or archived now" in joined

    def test_zero_conversions_without_an_attached_event_are_called_out(self, config):
        _, ctx = _fake_api({("GET", "ad_account/insights"): _page([
            {"campaign_id": "cmpn_2", "campaign_name": "Water Heaters", "clicks": 5, "spend": 41.0,
             "impressions": 500, "conversions": 0},
        ])})
        with ctx:
            result = read.get_chatgpt_performance(config)
        assert any("0 is guaranteed rather than measured" in i for i in result["insights"])

    def test_country_breakdown_requests_segment_metrics_only(self, config):
        calls, ctx = _fake_api({("GET", "ad_account/insights"): _page([
            {"campaign_id": "cmpn_1", "country_name": "United States", "country_impressions": 10,
             "country_clicks": 2, "country_spend": 16.0},
        ])})
        with ctx:
            result = read.get_chatgpt_performance(config, breakdown="country")
        params = next(c[2] for c in calls if c[1] == "ad_account/insights")
        assert params["segments[]"] == ["country"]
        assert "conversions" not in params["fields[]"] and "country.clicks" in params["fields[]"]
        assert "attribution_window_days" not in params
        row = result["rows"][0]
        assert row["country"] == "United States" and row["clicks"] == 2 and "conversions" not in row
        assert result["attribution"] is None

    def test_compact_mode(self, config):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_performance(config, compact=True)
        assert result["compact"] and "rows" not in result
        assert result["zero_conversion_spenders"][0]["campaign_id"] == "cmpn_1"

    def test_bad_arguments(self, config):
        with pytest.raises(ValueError, match="level"):
            read.get_chatgpt_performance(config, level="keyword")
        with pytest.raises(ValueError, match="click_window_days"):
            read.get_chatgpt_performance(config, click_window_days=10)


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


class TestConversions:
    def test_events_without_goal_and_view_through_share(self, config):
        calls, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_conversions(config)
        body = next(c[3] for c in calls if c[1] == "conversions/insights")
        assert body["aggregation_level"] == "campaign"
        assert sorted(body["entity_ids"]) == ["cmpn_1", "cmpn_2", "cmpn_3"]
        assert body["include"] == ["attributed_events"]
        assert json.loads(body["time_ranges"][0])["start"].isdigit()

        rows = {r["entity_id"]: r for r in result["rows"]}
        assert rows["cmpn_1"]["events"][0]["event_name"] == "page_viewed"
        assert rows["cmpn_1"]["conversion_events_attached"] == ["Web Form Lead (lead_created)"]
        assert result["totals"]["attributed_events"] == {"page_viewed": 31}
        joined = " ".join(result["insights"])
        assert "attributed events but no goal conversions" in joined
        assert "view-through" in joined
        assert "campaigns without a conversion event" in joined

    def test_account_level_reports_across_the_account(self, config):
        calls, ctx = _fake_api({("POST", "conversions/insights"): {
            "object": "list", "account_currency": "USD", "count": 1,
            "data": [{"entity_id": "adacct_1", "conversions": 3, "click_through_conversions": 3,
                      "view_through_conversions": 0, "attributed_events": []}],
        }})
        with ctx:
            result = read.get_chatgpt_conversions(config, level="account", include_events=False)
        body = next(c[3] for c in calls if c[1] == "conversions/insights")
        assert body["group_by_entity"] is False and "entity_ids" not in body
        assert result["totals"]["conversions"] == 3

    def test_event_names_need_events(self, config):
        with pytest.raises(ValueError):
            read.get_chatgpt_conversions(config, include_events=False, event_names=["lead_created"])


# ---------------------------------------------------------------------------
# Conversion setup audit
# ---------------------------------------------------------------------------


class TestConversionSetup:
    def test_finds_the_broken_links(self, config):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_conversion_setup(config)
        assert result["verdict"] == "broken"
        by_check = {c["check"]: c for c in result["checks"]}
        assert by_check["conversion source"]["status"] == "pass"
        assert by_check["campaign 'Water Heaters': conversion event"]["status"] == "fail"
        assert by_check["campaign 'Springfield Core': conversion event"]["status"] == "pass"
        goal = by_check["campaign 'Springfield Core': goal event fires"]
        # An observation, not proof: nobody may have converted.
        assert goal["status"] == "warn" and "lead_created" in goal["detail"]
        assert "page_viewed 31" in goal["detail"] and "nobody from these clicks converted" in goal["detail"]
        assert "Conversions API" in goal["detail"]
        recent = by_check["recent browser events"]
        assert recent["status"] == "pass" and "Pixel SDK (browser) events only" in recent["detail"]
        assert by_check["unused events"]["status"] == "info"
        # Failures sort first.
        assert result["checks"][0]["status"] == "fail"
        activity = {c["campaign_id"]: c for c in result["campaign_activity"]}
        assert activity["cmpn_1"]["clicks"] == 40 and activity["cmpn_1"]["events"] == {"page_viewed": 31}

    def test_paused_campaign_without_clicks_is_only_a_warning(self, config):
        _, ctx = _fake_api({("GET", "ad_account/insights"): _page([
            {"campaign_id": "cmpn_1", "campaign_name": "Springfield Core", "clicks": 40, "spend": 320.5,
             "conversions": 0},
        ])})
        with ctx:
            result = read.get_chatgpt_conversion_setup(config, check_recent_events=False)
        check = next(c for c in result["checks"] if c["check"] == "campaign 'Water Heaters': conversion event")
        assert check["status"] == "warn" and "Paused and no clicks" in check["detail"]

    def test_no_pixel_and_no_events_defined(self, config):
        _, ctx = _fake_api({
            ("GET", "conversions/pixels"): _page([]),
            ("GET", "conversions/event_settings"): _page([]),
            ("GET", "campaigns"): _page([]),
        })
        with ctx:
            result = read.get_chatgpt_conversion_setup(config)
        statuses = {c["check"]: c["status"] for c in result["checks"]}
        assert statuses["conversion source"] == "fail"
        assert statuses["conversion events"] == "fail"
        assert result["verdict"] == "broken"

    def test_clicks_without_any_attributed_event(self, config):
        _, ctx = _fake_api({("POST", "conversions/insights"): {
            "object": "list", "account_currency": "USD", "count": 1,
            "data": [{"entity_id": "cmpn_1", "conversions": 0, "attributed_events": []}],
        }})
        with ctx:
            result = read.get_chatgpt_conversion_setup(config, check_recent_events=False)
        check = next(c for c in result["checks"] if c["check"] == "campaign 'Springfield Core': events from ad clicks")
        assert check["status"] == "warn"
        assert "page_viewed is defined but no page view was seen arriving" in check["detail"]

    def test_lead_only_site_without_events_is_inconclusive_not_broken(self, config):
        _, ctx = _fake_api({
            ("GET", "conversions/event_settings"): _page([_SETTINGS[0]]),
            ("GET", "conversions/events"): {"object": "list", "data": []},
            ("POST", "conversions/insights"): {
                "object": "list", "account_currency": "USD", "count": 1,
                "data": [{"entity_id": "cmpn_1", "conversions": 0, "attributed_events": []}],
            },
        })
        with ctx:
            result = read.get_chatgpt_conversion_setup(config)
        check = next(c for c in result["checks"] if c["check"] == "campaign 'Springfield Core': events from ad clicks")
        assert check["status"] == "warn"
        assert "reports only lead_created (no page_viewed)" in check["detail"]
        assert "action_source phone_call" in check["detail"]
        # Only configuration problems fail; observations about this campaign do not.
        assert not any(c["status"] == "fail" and "Springfield Core" in c["check"] for c in result["checks"])

    def test_pixel_list_unavailable_falls_back_to_event_sources(self, config):
        _, ctx = _fake_api({("GET", "conversions/pixels"): ChatGPTAdsApiError("Not found", status=404)})
        with ctx:
            result = read.get_chatgpt_conversion_setup(config)
        source = next(c for c in result["checks"] if c["check"] == "conversion source")
        assert source["status"] == "info" and "Site" in source["detail"]
        assert result["recent_events"] is None


# ---------------------------------------------------------------------------
# Change history
# ---------------------------------------------------------------------------


class TestChangeHistory:
    def test_pages_counts_and_flags(self, config):
        pages = [
            {"data": [{"id": "alog_2", "audit_log_type": "update_campaign", "obj_type": "Campaign",
                       "obj_id": "cmpn_1", "item_name": "Springfield Core", "actor_type": "user",
                       "actor_id": "user_1", "campaign_id": "cmpn_1", "ad_group_id": None,
                       "changes": {"obj_type": "Campaign", "budget": {"before": {"daily_spend_limit_micros": 20000000},
                                                                     "after": {"daily_spend_limit_micros": 25000000}}},
                       "hierarchy_info": {}, "ts": 1790900000}],
             "has_more": True, "last_id": "alog_2"},
            {"data": [{"id": "alog_1", "audit_log_type": "create_ad", "obj_type": "Ad", "obj_id": "ad_1",
                       "item_name": "Drain $69", "actor_type": "user", "actor_id": "user_1",
                       "campaign_id": "cmpn_1", "ad_group_id": "adgrp_1", "changes": {}, "hierarchy_info": {},
                       "ts": 1790800000}],
             "has_more": False, "last_id": "alog_1"},
        ]
        seen: list[dict] = []

        def audit(params, body):
            seen.append(dict(params))
            return pages[len(seen) - 1]

        _, ctx = _fake_api({("GET", "audit_logs"): audit})
        with ctx:
            result = read.get_chatgpt_change_history(
                config, date_range_start="2026-10-01", date_range_end="2026-10-31", campaign_id="cmpn_1"
            )
        assert seen[0]["campaign_id"] == "cmpn_1" and seen[0]["order"] == "desc"
        assert seen[0]["start_time"] == 1790838000
        assert seen[1]["after"] == "alog_2"
        assert result["total"] == 2
        assert result["by_type"] == {"update_campaign": 1, "create_ad": 1}
        assert result["changes"][0]["changes"]["budget"]["after"] == {"daily_spend_limit_micros": 25000000}
        assert "obj_type" not in result["changes"][0]["changes"]
        assert result["changes"][0]["time"].startswith("2026-10-01")
        assert any("budget, bids, status" in i for i in result["insights"])


# ---------------------------------------------------------------------------
# ChatGPT Ads ↔ Google Analytics
# ---------------------------------------------------------------------------


class TestAttributionCheck:
    def _ga4(self, sessions, events):
        def fake(config, **kwargs):
            if "eventName" in kwargs["dimensions"]:
                return {"rows": events}
            return {"rows": sessions}

        return patch("adloop.ga4.reports.run_ga4_report", side_effect=fake)

    def test_ga4_key_events_while_chatgpt_reports_none(self, config):
        _, ctx = _fake_api({("GET", "ad_account/insights"): _page([
            {"impressions": 4500, "clicks": 45, "spend": 361.5, "conversions": 0},
        ])})
        sessions = [
            {"sessionSource": "chatgpt", "sessionMedium": "cpc", "sessions": "30", "engagedSessions": "18",
             "keyEvents": "4"},
            {"sessionSource": "chatgpt.com", "sessionMedium": "referral", "sessions": "12",
             "engagedSessions": "9", "keyEvents": "2"},
        ]
        events = [
            {"sessionSource": "chatgpt", "sessionMedium": "cpc", "eventName": "generate_lead", "keyEvents": "3"},
            {"sessionSource": "chatgpt", "sessionMedium": "cpc", "eventName": "phone_call", "keyEvents": "1"},
            {"sessionSource": "chatgpt.com", "sessionMedium": "referral", "eventName": "generate_lead",
             "keyEvents": "2"},
        ]
        with ctx, self._ga4(sessions, events) as ga4:
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert result["paid_tagging"] == ["chatgpt / cpc"]
        assert result["ga4_paid_chatgpt"]["sessions"] == 30
        assert result["ga4_paid_chatgpt"]["key_events_by_event"] == {"generate_lead": 3.0, "phone_call": 1.0}
        assert result["ga4_unpaid_chatgpt"]["sessions"] == 12
        joined = " ".join(result["insights"])
        assert "ChatGPT Ads reports 0 conversions" in joined
        assert "generate_lead 3, phone_call 1" in joined
        assert "counts only lead_created" in joined
        assert "sessions arrived from ChatGPT without paid tagging" in joined
        filt = ga4.call_args_list[0].kwargs["dimension_filter"][0]
        assert filt["op"] == "PARTIAL_REGEXP" and "chatgpt" in filt["value"]

    def test_property_is_required(self, config):
        with pytest.raises(ValueError, match="property_id"):
            read.chatgpt_attribution_check(config, property_id="")


# ---------------------------------------------------------------------------
# Audit additions: caching, retries, joins, chunking, edge cases
# ---------------------------------------------------------------------------


class TestAccountCache:
    def test_cache_is_per_api_key_not_per_requested_id(self):
        """A hosted server asks for "" on behalf of every tenant: two keys must
        never share an entry."""
        first = AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="key-tenant-a"))
        second = AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="key-tenant-b"))
        accounts = {
            "key-tenant-a": {**_ACCOUNT, "id": "adacct_a", "currency_code": "USD"},
            "key-tenant-b": {**_ACCOUNT, "id": "adacct_b", "currency_code": "EUR"},
        }

        def fake(config, method, path, *, params=None, json_body=None, ad_account_id=""):
            return accounts[config.chatgpt_ads.api_key]

        with patch("adloop.chatgpt_ads.client.api_request", side_effect=fake) as api:
            assert read.account_meta(first)["currency"] == "USD"
            assert read.account_meta(second)["currency"] == "EUR"
            assert read.account_meta(first)["ad_account_id"] == "adacct_a"
        assert api.call_count == 2

    def test_fresh_rereads_the_account(self, config):
        calls, ctx = _fake_api()
        with ctx:
            read.account_meta(config)
            read.account_meta(config)
            read.account_meta(config, fresh=True)
        assert [c[1] for c in calls] == ["ad_account", "ad_account"]


class TestClientRetries:
    def test_server_errors_on_reads_are_retried(self, config):
        responses = [_Resp(503, {}), _Resp(200, {"ok": True})]
        with patch("requests.request", side_effect=responses), patch("time.sleep"):
            assert client.api_get(config, "campaigns") == {"ok": True}

    def test_server_errors_on_posts_are_not_retried(self, config):
        with patch("requests.request", return_value=_Resp(503, {"error": {"message": "busy"}})) as req:
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_post(config, "conversions/insights", {"aggregation_level": "campaign"})
        assert req.call_count == 1 and err.value.status == 503

    def test_forbidden_and_validation_detail(self, config):
        payload = {"detail": [{"loc": ["body", "time_ranges"], "msg": "field required"}]}
        with patch("requests.request", return_value=_Resp(422, payload)):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_post(config, "conversions/insights", {})
        assert "time_ranges: field required" in str(err.value)
        with patch("requests.request", return_value=_Resp(403, {"error": {"message": "no access"}})):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_get(config, "ad_account/spend_limit_windows")
        assert err.value.status == 403


class TestPerformanceLevels:
    def test_ad_level_joins_parents_and_serving_state(self, config):
        _, ctx = _fake_api({("GET", "ad_account/insights"): _page([
            {"campaign_id": "cmpn_1", "ad_group_id": "adgrp_1", "ad_id": "ad_2", "ad_name": "Drain v2",
             "impressions": 100, "clicks": 3, "spend": 21.0, "conversions": 0},
        ])})
        with ctx:
            result = read.get_chatgpt_performance(config, level="ad")
        row = result["rows"][0]
        assert row["ad_group_name"] == "Drain cleaning"
        assert row["ad_review_status"] == "rejected"
        assert row["campaign_status"] == "active"
        assert row["serving_state"].startswith("not serving: the creative review was rejected")

    def test_daily_rows_carry_the_date(self, config):
        calls, ctx = _fake_api({("GET", "ad_account/insights"): _page([
            {"campaign_id": "cmpn_1", "readable_time": "2026-10-20", "clicks": 2, "spend": 15.0, "conversions": 0},
            {"campaign_id": "cmpn_1", "readable_time": "2026-10-21", "clicks": 1, "spend": 8.0, "conversions": 1},
        ])})
        with ctx:
            result = read.get_chatgpt_performance(config, breakdown="date")
        params = next(c[2] for c in calls if c[1] == "ad_account/insights")
        assert params["time_granularity"] == "daily" and "readable_time" in params["fields[]"]
        assert sorted(r["date"] for r in result["rows"]) == ["2026-10-20", "2026-10-21"]
        assert result["totals"]["conversions"] == 1

    def test_pagination_follows_the_cursor(self, config):
        pages = [
            {"data": [{"campaign_id": "cmpn_1", "clicks": 1, "spend": 5.0}], "has_more": True, "last_id": "c1"},
            {"data": [{"campaign_id": "cmpn_2", "clicks": 2, "spend": 9.0}], "has_more": False, "last_id": "c2"},
        ]
        seen: list[dict] = []

        def insights(params, body):
            seen.append(dict(params))
            return pages[len(seen) - 1]

        _, ctx = _fake_api({("GET", "ad_account/insights"): insights})
        with ctx:
            result = read.get_chatgpt_performance(config)
        assert result["total_rows"] == 2 and seen[1]["after"] == "c1"


class TestConversionChunking:
    def test_large_entity_lists_are_split(self, config):
        many = [
            {"id": f"cmpn_{i}", "name": f"C{i}", "status": "active", "bidding_type": "clicks",
             "budget": {}, "conversion_event_setting_ids": []}
            for i in range(120)
        ]
        bodies: list[dict] = []

        def conversions(params, body):
            bodies.append(body)
            return {"object": "list", "account_currency": "USD", "count": 0, "data": []}

        _, ctx = _fake_api({
            ("GET", "campaigns"): _page(many),
            ("POST", "conversions/insights"): conversions,
        })
        with ctx:
            read.get_chatgpt_conversions(
                config, breakdown="date", date_range_start="2026-10-01", date_range_end="2026-10-30"
            )
        # 30 daily rows per campaign: at most 1,500 rows per request -> 50 campaigns per call.
        assert [len(b["entity_ids"]) for b in bodies] == [50, 50, 20]
        assert all(b["time_granularity"] == "daily" for b in bodies)


class TestAdsFilterAndAttributionEdges:
    def test_ads_of_one_ad_group(self, config):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_chatgpt_ads(config, ad_group_id="adgrp_3")
        assert [a["ad_id"] for a in result["ads"]] == ["ad_3"]
        assert result["ads"][0]["serving_state"].startswith("not serving: the campaign is paused")

    def test_untagged_ads_are_called_out(self, config):
        untagged = {k: [dict(a, creative=dict(a["creative"], target_url="https://x.com/"))
                        for a in v] for k, v in _ADS.items()}
        _, ctx = _fake_api({("GET", "ads"): lambda p, b: _page(untagged.get(p["ad_group_id"], []))})
        with ctx, patch("adloop.ga4.reports.run_ga4_report", return_value={"rows": []}):
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert result["paid_tagging"] == []
        assert any("No ad carries distinctive paid tagging" in i for i in result["insights"])

    def test_clicks_without_paid_sessions(self, config):
        _, ctx = _fake_api()
        with ctx, patch("adloop.ga4.reports.run_ga4_report", return_value={"rows": []}):
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert any("no GA4 session with chatgpt/cpc" in i for i in result["insights"])


class TestSpendLimits:
    def test_used_up_daily_limit_is_flagged(self, config):
        _, ctx = _fake_api({("GET", "ad_account/spend_limit_windows"): {
            "object": "list", "data": [],
            "daily_limit": {"amount_micros": 50_000_000, "start_date": "2026-11-01", "end_date": None,
                            "timezone": TZ, "status": "active", "spent_micros": 50_000_000,
                            "remaining_micros": 0, "spend_as_of": None, "next_reset": None},
        }})
        with ctx:
            result = read.get_chatgpt_ads_account(config)
        assert result["spend_limits"]["daily_limit"]["amount"] == 50.0
        assert any("spending limit is used up" in i for i in result["insights"])


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------


class TestServer:
    def test_missing_key_gets_a_structured_hint(self):
        from adloop import runtime, server

        runtime.set_default_config(AdLoopConfig())
        try:
            result = server.get_chatgpt_campaigns()
        finally:
            runtime.set_default_config(None)
        assert result["auth_error"] == "CHATGPT_ADS_NOT_CONNECTED"
        assert "Settings → API keys" in result["hint"]
        assert result["tool"] == "get_chatgpt_campaigns"

    def test_health_check_reports_the_account(self, monkeypatch):
        import adloop.ads.gaql as gaql
        import adloop.ga4.reports as reports
        from adloop import runtime, server

        monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [])
        monkeypatch.setattr(reports, "get_account_summaries", lambda _c: {"accounts": [], "total_properties": 0})
        runtime.set_default_config(AdLoopConfig(chatgpt_ads=ChatGPTAdsConfig(api_key="k")))
        _, ctx = _fake_api()
        try:
            with ctx:
                status = server.health_check()
        finally:
            runtime.set_default_config(None)
        assert status["chatgpt_ads"] == "ok"
        assert status["chatgpt_ads_account"] == "adacct_1"
        assert "get_chatgpt_conversion_setup" in status["tools_offered"]

    def test_health_check_without_a_key(self, monkeypatch):
        import adloop.ads.gaql as gaql
        import adloop.ga4.reports as reports
        from adloop import runtime, server

        monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [])
        monkeypatch.setattr(reports, "get_account_summaries", lambda _c: {"accounts": [], "total_properties": 0})
        runtime.set_default_config(AdLoopConfig())
        try:
            status = server.health_check()
        finally:
            runtime.set_default_config(None)
        assert status["chatgpt_ads"] == "not_configured"


# ---------------------------------------------------------------------------
# Review fixes: 413 splitting, truncation, ad-group status, attribution buckets
# ---------------------------------------------------------------------------


def _days_in(body: dict) -> int:
    span = json.loads(body["time_ranges"][0])
    return round((int(span["end"]) - int(span["start"])) / 86400)


class TestConversionReportSplitting:
    def test_413_splits_entities_then_dates_and_adds_the_halves_up(self, config):
        seen: list[tuple[list, int]] = []

        def conversions(params, body):
            ids = body.get("entity_ids") or []
            seen.append((list(ids), _days_in(body)))
            if len(ids) > 1 or _days_in(body) > 2:
                raise ChatGPTAdsApiError("Too many rows", status=413)
            return {"object": "list", "account_currency": "USD", "data": [{
                "entity_id": ids[0], "conversions": 1, "click_through_conversions": 1,
                "view_through_conversions": 0, "order_created_attributed_sales": "10.5",
                "attributed_events": [{
                    "event_name": "lead_created", "event_kind": "standard", "attributed_event_count": 2,
                    "click_through_conversions": 2, "view_through_conversions": 0,
                    "conversion_event_setting_breakdowns": [
                        {"conversion_event_setting_id": "ces_lead", "conversions": 1},
                    ],
                }],
            }]}

        _, ctx = _fake_api({("POST", "conversions/insights"): conversions})
        with ctx:
            result = read.get_chatgpt_conversions(
                config, level="campaign", date_range_start="2026-10-01", date_range_end="2026-10-04",
            )
        # Entities are split first, then each campaign's four days into two halves.
        answered = [(ids[0], days) for ids, days in seen if len(ids) == 1 and days <= 2]
        assert seen[0] == (["cmpn_1", "cmpn_2", "cmpn_3"], 4) or len(seen[0][0]) > 1
        assert {days for _, days in answered} == {2}
        assert sorted(answered).count(("cmpn_1", 2)) == 2
        by_id = {r["entity_id"]: r for r in result["rows"]}
        assert by_id["cmpn_1"]["conversions"] == 2
        assert by_id["cmpn_1"]["purchase_value"] == 21.0
        event = by_id["cmpn_1"]["events"][0]
        assert event["count"] == 4
        assert result["totals"]["conversions"] == 2 * len(by_id)

    def test_one_entity_one_day_still_too_big_says_so(self, config):
        _, ctx = _fake_api({("POST", "conversions/insights"): ChatGPTAdsApiError("big", status=413)})
        with ctx, pytest.raises(ChatGPTAdsApiError, match="event_names"):
            read.get_chatgpt_conversions(
                config, level="campaign", date_range_start="2026-10-01", date_range_end="2026-10-01",
            )

    def test_other_errors_are_not_split(self, config):
        calls, ctx = _fake_api({("POST", "conversions/insights"): ChatGPTAdsApiError("bad", status=400)})
        with ctx, pytest.raises(ChatGPTAdsApiError):
            read.get_chatgpt_conversions(config, level="campaign")
        assert sum(1 for c in calls if c[1] == "conversions/insights") == 1


class TestAdLevelConversions:
    def test_ad_rows_carry_the_ad_group_status(self, config):
        def conversions(params, body):
            return {"object": "list", "account_currency": "USD", "data": [
                {"entity_id": "ad_3", "conversions": 1, "attributed_events": []},
            ]}

        groups = {"cmpn_2": [dict(_AD_GROUPS["cmpn_2"][0], status="paused")]}
        _, ctx = _fake_api({
            ("POST", "conversions/insights"): conversions,
            ("GET", "ad_groups"): lambda p, b: _page(groups.get(p["campaign_id"], _AD_GROUPS.get(p["campaign_id"], []))),
        })
        with ctx:
            result = read.get_chatgpt_conversions(config, level="ad", include_events=False)
        row = result["rows"][0]
        assert row["ad_group_id"] == "adgrp_3" and row["ad_group_status"] == "paused"
        assert row["campaign_status"] == "paused"


class TestTruncation:
    def test_insights_report_at_the_page_cap_is_flagged(self, config, monkeypatch):
        monkeypatch.setattr(read, "_MAX_INSIGHT_PAGES", 2)

        def endless(params, body):
            return {"object": "list", "has_more": True, "last_id": f"row_{params.get('after', 0)}x",
                    "data": [{"campaign_id": "cmpn_1", "campaign_name": "Springfield Core", "clicks": 1,
                              "impressions": 10, "spend": 1.0, "conversions": 0}]}

        _, ctx = _fake_api({("GET", "ad_account/insights"): endless})
        with ctx:
            result = read.get_chatgpt_performance(config, level="campaign")
        assert result["truncated"] is True
        assert any("page limit" in i for i in result["insights"])

    def test_list_at_the_page_cap_raises_instead_of_returning_part(self, config):
        def endless(*args, **kwargs):
            return {"data": [{"id": "x"}], "has_more": True, "last_id": "x"}

        with patch("adloop.chatgpt_ads.client.api_get", side_effect=endless):
            with pytest.raises(ChatGPTAdsApiError, match="narrow it"):
                client.api_get_all(config, "campaigns", max_pages=3)


class TestAttributionBuckets:
    def _ga4(self, sessions, events=()):
        def fake(config, **kwargs):
            return {"rows": list(events) if "eventName" in kwargs["dimensions"] else sessions}

        return patch("adloop.ga4.reports.run_ga4_report", side_effect=fake)

    def _insights_with_clicked_ads(self, *ad_ids):
        def handler(params, body):
            if params.get("aggregation_level") == "ad":
                return _page([{"ad_id": a, "clicks": 5} for a in ad_ids])
            return _page([{"impressions": 900, "clicks": 5 * len(ad_ids), "spend": 40.0, "conversions": 0}])

        return handler

    def test_untagged_ad_with_clicks_makes_referrals_ambiguous(self, config):
        # ad_2 has no utm tags; give it clicks.
        _, ctx = _fake_api({("GET", "ad_account/insights"): self._insights_with_clicked_ads("ad_1", "ad_2")})
        sessions = [
            {"sessionSource": "chatgpt", "sessionMedium": "cpc", "sessions": "8", "engagedSessions": "5",
             "keyEvents": "1"},
            {"sessionSource": "chatgpt.com", "sessionMedium": "referral", "sessions": "6",
             "engagedSessions": "3", "keyEvents": "1"},
        ]
        with ctx, self._ga4(sessions):
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert result["ga4_paid_chatgpt"]["sessions"] == 8
        assert result["ga4_ambiguous_chatgpt"]["sessions"] == 6
        assert result["ga4_unpaid_chatgpt"]["sessions"] == 0
        joined = " ".join(result["insights"])
        assert "1 ad(s) without utm_source got clicks" in joined
        assert "links in ChatGPT answers" not in joined

    def test_untagged_ad_without_clicks_keeps_referrals_unpaid(self, config):
        _, ctx = _fake_api({("GET", "ad_account/insights"): self._insights_with_clicked_ads("ad_1")})
        sessions = [{"sessionSource": "chatgpt.com", "sessionMedium": "referral", "sessions": "6",
                     "engagedSessions": "3", "keyEvents": "0"}]
        with ctx, self._ga4(sessions):
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert result["ga4_unpaid_chatgpt"]["sessions"] == 6
        assert result["ga4_ambiguous_chatgpt"]["sessions"] == 0

    def test_source_without_a_medium_is_never_paid(self, config):
        medium_less = {k: [dict(a, creative=dict(
            a["creative"], target_url="https://example.com/drain?utm_source=chatgpt.com"))
            for a in v] for k, v in _ADS.items()}
        _, ctx = _fake_api({
            ("GET", "ads"): lambda p, b: _page(medium_less.get(p["ad_group_id"], [])),
            ("GET", "ad_account/insights"): self._insights_with_clicked_ads("ad_1"),
        })
        sessions = [{"sessionSource": "chatgpt.com", "sessionMedium": "referral", "sessions": "9",
                     "engagedSessions": "4", "keyEvents": "2"}]
        events = [{"sessionSource": "chatgpt.com", "sessionMedium": "referral", "eventName": "generate_lead",
                   "keyEvents": "2"}]
        with ctx, self._ga4(sessions, events):
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert result["paid_tagging"] == ["chatgpt.com / (no medium)"]
        assert result["ga4_paid_chatgpt"]["sessions"] == 0
        assert result["ga4_ambiguous_chatgpt"]["sessions"] == 9
        joined = " ".join(result["insights"])
        assert "No ad carries distinctive paid tagging" in joined
        # Unattributable key events must not be presented as a broken OpenAI setup.
        assert "reports 0 conversions" not in joined


# ---------------------------------------------------------------------------
# Review findings (Codex, second round)
# ---------------------------------------------------------------------------


class TestReviewFindingsRoundTwo:
    def test_truncated_click_report_makes_untagged_sessions_ambiguous(self, config, monkeypatch):
        # Finding 10: if clicked ads were cut off, "every clicked ad was tagged" cannot be claimed.
        monkeypatch.setattr(read, "_MAX_INSIGHT_PAGES", 1)

        def insights(params, body):
            if params.get("aggregation_level") == "ad":
                return {"object": "list", "has_more": True, "last_id": "x",
                        "data": [{"ad_id": "ad_1", "clicks": 5}]}
            return _page([{"impressions": 900, "clicks": 40, "spend": 300.0, "conversions": 0}])

        sessions = [{"sessionSource": "chatgpt.com", "sessionMedium": "referral", "sessions": "6",
                     "engagedSessions": "2", "keyEvents": "0"}]
        _, ctx = _fake_api({("GET", "ad_account/insights"): insights})
        with ctx, patch("adloop.ga4.reports.run_ga4_report", side_effect=lambda c, **k: {"rows": [] if "eventName" in k["dimensions"] else sessions}):
            result = read.chatgpt_attribution_check(config, property_id="properties/1")
        assert result["ga4_unpaid_chatgpt"]["sessions"] == 0 and result["ga4_ambiguous_chatgpt"]["sessions"] == 6
        joined = " ".join(result["insights"])
        assert "page limit" in joined and "while every clicked ad was tagged" not in joined

    def test_money_merges_exactly(self):
        # Finding 11: eight daily 0.004 sales add up to 0.032, not 0.04.
        rows = [{"entity_id": "cmpn_1", "conversions": 0, "order_created_attributed_sales": "0.004",
                 "attributed_events": [{"event_name": "order_created", "event_kind": "standard",
                                        "attributed_event_count": 1, "attributed_event_value_amount": "0.004"}]}
                for _ in range(8)]
        merged = read._merge_conversion_rows(rows)
        assert len(merged) == 1
        assert merged[0]["order_created_attributed_sales"] == "0.032"
        assert merged[0]["attributed_events"][0]["attributed_event_value_amount"] == "0.032"
        assert merged[0]["attributed_events"][0]["attributed_event_count"] == 8


class TestUpstreamAuditFindings:
    """Codex audit of the upstream ports (adloop-audit-2026-11-04-upstream.md)."""

    KEY = "sk-ads-default"

    def test_an_error_echoing_the_key_is_redacted(self, config):
        # Finding 10: an error body that repeats the request must not leak the key.
        payload = {"error": {"message": f"Bad header Bearer {self.KEY}", "code": self.KEY}}
        with patch("requests.request", return_value=_Resp(400, payload)):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_get(config, "campaigns")
        assert self.KEY not in str(err.value) and self.KEY not in str(err.value.code)
        assert "Bearer [redacted]" in str(err.value)

    def test_a_rejected_key_is_not_echoed_back(self, config):
        with patch("requests.request", return_value=_Resp(401, {"error": {"message": f"invalid key {self.KEY}"}})):
            with pytest.raises(ChatGPTAdsAuthError) as err:
                client.api_get(config, "ad_account")
        assert self.KEY not in str(err.value)

    def test_the_key_is_redacted_before_a_long_message_is_cut(self, config):
        # Trimming first would cut the key and leave its prefix unmatched.
        message = "x" * (client._MAX_MESSAGE - 6) + f" {self.KEY} tail"
        with patch("requests.request", return_value=_Resp(400, {"error": {"message": message}})):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_get(config, "campaigns")
        assert "sk-ad" not in str(err.value)

    def test_tool_results_carry_no_key(self, config):
        from adloop import runtime, server

        runtime.set_default_config(config)
        try:
            with patch("requests.request", return_value=_Resp(400, {"error": {"message": f"echo {self.KEY}"}})):
                result = server.get_chatgpt_campaigns()
        finally:
            runtime.set_default_config(None)
        assert "error" in result and self.KEY not in json.dumps(result)


class TestUnknownOutcomes:
    """A write whose answer was lost says so; reads and unsent requests say nothing changed."""

    def test_a_timed_out_write_is_an_unknown_outcome(self, config):
        import requests

        from adloop.chatgpt_ads.client import ChatGPTAdsOutcomeUnknown

        with patch("requests.request", side_effect=requests.ReadTimeout("slow")):
            with pytest.raises(ChatGPTAdsOutcomeUnknown) as err:
                client.api_post(config, "campaigns/cmpn_1", {"name": "x"})
        assert "whether it changed anything is unknown" in str(err.value)
        assert "nothing" not in str(err.value)

    def test_a_write_that_never_connected_changed_nothing(self, config):
        import requests

        from adloop.chatgpt_ads.client import ChatGPTAdsOutcomeUnknown

        with patch("requests.request", side_effect=requests.ConnectTimeout("no route")):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_post(config, "campaigns/cmpn_1", {"name": "x"})
        assert not isinstance(err.value, ChatGPTAdsOutcomeUnknown)
        assert "was not sent, so nothing changed" in str(err.value)

    def test_a_failed_read_changed_nothing(self, config):
        import requests

        with patch("requests.request", side_effect=requests.ReadTimeout("slow")):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_get(config, "campaigns")
        assert "nothing was changed (a read)" in str(err.value)

    def test_a_gateway_error_on_a_write_is_an_unknown_outcome(self, config):
        from adloop.chatgpt_ads.client import ChatGPTAdsOutcomeUnknown

        with patch("requests.request", return_value=_Resp(502, {"error": {"message": "bad gateway"}})):
            with pytest.raises(ChatGPTAdsOutcomeUnknown) as err:
                client.api_post(config, "ads", {"name": "x"}, idempotency_key="adloop-1")
        assert err.value.status == 502

    def test_a_report_query_is_not_a_write(self, config):
        from adloop.chatgpt_ads.client import ChatGPTAdsOutcomeUnknown

        with patch("requests.request", return_value=_Resp(503, {"error": {"message": "busy"}})):
            with pytest.raises(ChatGPTAdsApiError) as err:
                client.api_post(config, "conversions/insights", {}, mutation=False)
        assert not isinstance(err.value, ChatGPTAdsOutcomeUnknown)
