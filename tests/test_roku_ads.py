"""Roku Ads (Ads API beta): config, sign-in, client, reads and diagnostics against a fake API."""

from __future__ import annotations

import gzip
import io
import json
import re
import os
import stat
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from adloop.config import AdLoopConfig, RokuAdsConfig, load_config
from adloop.roku_ads import auth, client, read
from adloop.roku_ads.auth import RokuAdsApiError, RokuAdsAuthError, RokuCredentials

ACCOUNT = "PaccTest1"
TZ = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _fresh():
    read.reset_account_cache()
    auth.reset_credentials_cache()
    yield
    read.reset_account_cache()
    auth.reset_credentials_cache()


def _config(tmp_path=None, **overrides) -> AdLoopConfig:
    kwargs = dict(
        client_id="roku-app", client_secret="roku-secret",
        authorization_url="https://ads.roku.com/oauth/authorize?client_id=roku-app&response_type=code",
        redirect_uri="http://localhost:8765/roku/callback", account_uid=ACCOUNT,
    )
    if tmp_path is not None:
        kwargs["token_path"] = str(tmp_path / "roku_token.json")
    kwargs.update(overrides)
    return AdLoopConfig(roku_ads=RokuAdsConfig(**kwargs))


# ---------------------------------------------------------------------------
# Fake API (read layer)
# ---------------------------------------------------------------------------

_ACCOUNT = {"data": {"type": "account", "uid": ACCOUNT, "attributes": {
    "name": "Acme Auto Body", "organization_uid": "Porg1", "currency": "United States Dollar",
    "country": "United States", "timezone": TZ, "website_url": "https://example.com",
}}}

_LIVE_GROUP = {
    "uid": "PadGrp1", "status": "active", "budget": 1000, "bid_strategy": {"type": "auto_bid", "value": None},
    "start_at": "2026-10-20T00:00:00Z", "end_at": "2026-12-31T00:00:00Z",
    "dayparts": {"timezone_source": "account", "schedule": [{"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 11}]},
    "location_targeting": {"included": [{"type": "metro_market", "name": "Springfield-Decatur IL"}],
                           "excluded": []},
    # Roku's schema: included / excluded are arrays of objects.
    "audience_targeting": {"included": [{"age": ["25-34"]}, {"custom_audiences": [
        {"uid": "Paud1", "name": "Site visitors", "type": "event_audiences"}]}], "excluded": []},
    "placement": {"apps": [], "auto_placement": True},
    "impression_frequency_caps": {"frequency_unit_of_measure": "days", "max_impressions": 3, "frequency_value": 1},
    "content": {"exclude_mature_content": True},
    "event_group_uid": "Pevt1", "optimization_goal": "Leads",
}
_CAMPAIGNS = [
    {"type": "campaign", "uid": "Pcmp1", "attributes": {
        "name": "Acme TV - Springfield", "account_uid": ACCOUNT, "objective": "Conversion", "status": "active",
        "is_enabled": True, "is_archived": False, "ad_groups": [_LIVE_GROUP],
    }},
    {"type": "campaign", "uid": "Pcmp2", "attributes": {
        "name": "Acme TV - Brand", "account_uid": ACCOUNT, "objective": "Awareness", "status": "creative_needed",
        "is_enabled": True, "is_archived": False, "ad_groups": [{
            "uid": "PadGrp2", "status": "creative_needed", "budget": 300, "location_targeting": {"included": []},
            "optimization_goal": "Reach", "event_group_uid": None,
        }],
    }},
    {"type": "campaign", "uid": "Pcmp3", "attributes": {
        "name": "Old test", "account_uid": ACCOUNT, "objective": "Awareness", "status": "ended",
        "is_enabled": False, "is_archived": True, "ad_groups": [],
    }},
]
_EVENT_GROUPS = [{"type": "event_group", "uid": "Pevt1", "attributes": {
    "name": "Website", "account_uid": ACCOUNT, "is_default": True, "status": "active", "is_archived": False,
}}]


def _list_page(items, params, included=None):
    limit = int(params.get("limit") or 20)
    offset = int(params.get("offset") or 0)
    page = items[offset : offset + limit]
    out = {"data": page, "meta": {"count": len(page), "total_count": len(items)}}
    if included:
        out["included"] = included
    return out


def _default_metrics(params, body):
    entity = params["entity"]
    uids = str(params["entity_uids"]).split(",")
    fields = str(params["fields"]).split(",")
    rows = []
    for uid in uids:
        values = {
            "billed_local_micro_spend": [50_000_000], "impressions": [2500], "total_video_start": [2450],
            "total_video_one_quarter": [2400], "total_video_midpoint": [2350],
            "total_video_three_quarter": [2300], "total_video_complete": [2250],
            "action_lead": [1], "action_page_view": [12],
        }
        rows.append({"dimensions": {f"{entity}_uid": uid}, "fields": {f: values.get(f, [0]) for f in fields}})
    return {"data": rows, "included": {"default_optimizations": [
        {"ad_group_uid": "PadGrp1", "event_group_uid": "Pevt1", "attribution_window": ["click_14d", "view_14d"]},
    ]}, "meta": {"pagination": {"has_more": False, "next_cursor": None}, "request": {}, "warnings": []}}


def _fake_api(extra: dict | None = None):
    calls: list[tuple] = []
    routes = {
        ("GET", f"accounts/{ACCOUNT}"): _ACCOUNT,
        ("GET", "campaigns"): lambda p, b: _list_page(_CAMPAIGNS, p),
        ("GET", "event_groups"): lambda p, b: _list_page(_EVENT_GROUPS, p),
        ("GET", "metrics"): _default_metrics,
    }
    routes.update(extra or {})

    def fake(config, method, path, *, params=None, json_body=None):
        calls.append((method, path, dict(params or {}), json_body))
        handler = routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected Roku call {method} {path}")
        if isinstance(handler, Exception):
            raise handler
        # Handlers see the query as the client sends it: lists comma-joined.
        sent = {k: str(v) for k, v in client._query(params).items()}
        return handler(sent, json_body) if callable(handler) else handler

    return calls, patch("adloop.roku_ads.client.api_request", side_effect=fake)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_block_parses(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "roku_ads:\n  client_id: app\n  client_secret: sec\n"
            "  authorization_url: https://ads.roku.com/auth?x=1\n"
            "  redirect_uri: http://localhost:8765/cb\n  account_uid: Pacc1\n"
        )
        cfg = load_config(str(path)).roku_ads
        assert (cfg.client_id, cfg.account_uid, cfg.redirect_uri) == ("app", "Pacc1", "http://localhost:8765/cb")
        assert cfg.token_path == "~/.adloop/roku_token.json"

    def test_defaults_when_absent(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ga4:\n  property_id: '1'\n")
        assert load_config(str(path)).roku_ads == RokuAdsConfig()


# ---------------------------------------------------------------------------
# Sign-in and credentials
# ---------------------------------------------------------------------------


class _TokenResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class TestLogin:
    def test_state_is_set_once_and_other_parameters_stay(self):
        url = auth.with_state("https://ads.roku.com/auth?client_id=a&state=old&redirect_uri=x", "new")
        query = parse_qs(urlsplit(url).query)
        assert query["state"] == ["new"] and query["client_id"] == ["a"] and query["redirect_uri"] == ["x"]

    def test_local_redirect_exchanges_the_code(self, tmp_path):
        cfg = _config(tmp_path)
        opened: list[str] = []

        def listen(redirect_uri):
            state = parse_qs(urlsplit(opened[0]).query)["state"][0]
            return {"code": "auth-code", "state": state}

        with patch("requests.post", return_value=_TokenResponse(200, {
            "access_token": "at", "refresh_token": "rt", "expires_in": 86400, "token_type": "Bearer",
        })) as post:
            path = auth.authorization_login(cfg, say=lambda _m: None, open_browser=opened.append, listen=listen)
        body = post.call_args.kwargs["json"]
        assert post.call_args.args[0] == "https://api.ads.roku.com/v1/developer/token"
        assert body == {"grant_type": "authorization_code", "client_id": "roku-app", "client_secret": "roku-secret",
                        "code": "auth-code", "redirect_uri": "http://localhost:8765/roku/callback"}
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "rt" and saved["access_token"] == "at"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_pasted_redirect_for_a_non_local_uri(self, tmp_path):
        cfg = _config(tmp_path, redirect_uri="https://example.com/roku/callback")
        opened: list[str] = []

        def ask(_prompt):
            state = parse_qs(urlsplit(opened[0]).query)["state"][0]
            return f"https://example.com/roku/callback?code=c1&state={state}"

        with patch("requests.post", return_value=_TokenResponse(200, {"access_token": "at", "refresh_token": "rt"})):
            path = auth.authorization_login(cfg, say=lambda _m: None, ask=ask, open_browser=opened.append,
                                            listen=lambda _u: pytest.fail("no listener for https"))
        assert path.exists()

    def test_state_mismatch_is_refused(self, tmp_path):
        cfg = _config(tmp_path)
        with patch("requests.post") as post, pytest.raises(RokuAdsAuthError, match="state"):
            auth.authorization_login(cfg, say=lambda _m: None, open_browser=lambda _u: None,
                                     listen=lambda _u: {"code": "c", "state": "forged"})
        post.assert_not_called()

    def test_missing_app_settings_are_named(self, tmp_path):
        with pytest.raises(RokuAdsAuthError, match="authorization_url"):
            auth.authorization_login(_config(tmp_path, authorization_url=""), say=lambda _m: None)

    def test_rejected_code(self, tmp_path):
        opened: list[str] = []

        def listen(_uri):
            return {"code": "c", "state": parse_qs(urlsplit(opened[0]).query)["state"][0]}

        refused = _TokenResponse(403, {"errors": [{"status": "authError", "title": "bad code"}]})
        with patch("requests.post", return_value=refused), pytest.raises(RokuAdsAuthError) as err:
            auth.authorization_login(_config(tmp_path), say=lambda _m: None, open_browser=opened.append,
                                     listen=listen)
        assert err.value.error_code == "invalid_client" and "bad code" in str(err.value)
        assert not (tmp_path / "roku_token.json").exists()


class TestListener:
    def test_listener_captures_the_redirect_query(self):
        import socket
        import threading

        import requests

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        uri = f"http://127.0.0.1:{port}/roku/callback"
        captured: dict = {}
        worker = threading.Thread(target=lambda: captured.update(auth.listen_once(uri, timeout=10)))
        worker.start()
        for _ in range(50):
            try:
                assert requests.get(f"{uri.replace('/roku/callback', '/favicon.ico')}", timeout=2).status_code == 404
                response = requests.get(f"{uri}?code=abc&state=xyz", timeout=2)
                break
            except requests.ConnectionError:
                import time as _t

                _t.sleep(0.1)
        worker.join(timeout=10)
        assert response.status_code == 200 and "close this tab" in response.text
        assert captured == {"code": "abc", "state": "xyz"}


class TestCredentials:
    def test_refresh_sends_json_and_keeps_the_refresh_token(self, tmp_path):
        path = tmp_path / "roku_token.json"
        creds = auth.FileRokuCredentials(token_path=path, client_id="app", client_secret="sec", refresh_token="rt-1")
        with patch("requests.post", return_value=_TokenResponse(200, {"access_token": "at-2", "expires_in": 86400})) as post:
            assert creds.token() == "at-2"
        assert post.call_args.kwargs["json"] == {
            "grant_type": "refresh_token", "client_id": "app", "client_secret": "sec", "refresh_token": "rt-1",
        }
        assert json.loads(path.read_text())["refresh_token"] == "rt-1"

    def test_refused_refresh_asks_for_a_new_login(self):
        creds = RokuCredentials(client_id="app", client_secret="sec", refresh_token="rt")
        with patch("requests.post", return_value=_TokenResponse(401, {"errors": [{"status": "authError"}]})):
            with pytest.raises(RokuAdsAuthError, match="roku-login") as err:
                creds.token()
        assert err.value.error_code == "invalid_grant"

    def test_no_sign_in_yet(self, tmp_path):
        with pytest.raises(RokuAdsAuthError) as err:
            auth.local_credentials(_config(tmp_path))
        assert err.value.error_code == "not_signed_in"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class _Creds(RokuCredentials):
    def __init__(self):
        super().__init__(client_id="app", client_secret="sec", refresh_token="rt", access_token="at-1",
                         expires_at=10**12)
        self.refreshes = 0

    def _refresh_locked(self):
        self.refreshes += 1
        self.access_token = f"at-{self.refreshes + 1}"
        self.expires_at = 10**12


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode() if payload is not None else b""
        self.headers = headers or {}

    def json(self):
        return self._payload


class TestClient:
    def test_lists_go_comma_separated_and_401_refreshes_once(self):
        creds = _Creds()
        responses = [_Resp(401, {"errors": [{"status": "authError"}]}), _Resp(200, {"data": []})]
        with patch("adloop.roku_ads.client._credentials", return_value=creds), \
                patch("requests.request", side_effect=responses) as req:
            client.api_get(_config(), "campaigns", {"account_uid": ["a", "b"], "filter": ""})
        assert creds.refreshes == 1
        second = req.call_args_list[1]
        assert second.kwargs["params"] == {"account_uid": "a,b"}
        assert second.kwargs["headers"]["Authorization"] == "Bearer at-2"
        assert req.call_args_list[0].args[1] == "https://api.ads.roku.com/v1/developer/campaigns"

    def test_rate_limit_with_a_long_wait_surfaces(self):
        body = {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "Rate limit exceeded. Retry after 42 seconds."}}
        with patch("adloop.roku_ads.client._credentials", return_value=_Creds()), \
                patch("requests.request", return_value=_Resp(429, body, {"Retry-After": "42"})):
            with pytest.raises(RokuAdsApiError) as err:
                client.api_get(_config(), "campaigns")
        assert err.value.status == 429 and err.value.retry_after == 42

    def test_both_error_shapes(self):
        assert auth.error_detail({"errors": [{"status": "invalidRequest", "title": "Bad filter",
                                              "meta": {"field": "filter"}}]}) == ("Bad filter (filter)", "invalidRequest")
        message, code = auth.error_detail({"error": {"code": "VALIDATION_ERROR", "message": "Invalid",
                                                     "details": [{"field": "end_date", "issue": "max 7 days"}]}})
        assert code == "VALIDATION_ERROR" and "end_date: max 7 days" in message

    def test_transport_error_is_wrapped(self):
        import requests

        with patch("adloop.roku_ads.client._credentials", return_value=_Creds()), \
                patch("requests.request", side_effect=requests.ConnectionError("boom")):
            with pytest.raises(RokuAdsApiError, match="Could not reach"):
                client.api_get(_config(), "campaigns")

    def test_offset_pagination_merges_included(self):
        items = [{"uid": str(i)} for i in range(250)]
        pages = []

        def fake_get(config, path, params):
            pages.append(dict(params))
            out = _list_page(items, params)
            out["included"] = {"audiences_config_items": [{"type": "crm_file", "uid": f"f{params['offset']}"}]}
            return out

        with patch("adloop.roku_ads.client.api_get", side_effect=fake_get):
            got, included = client.api_get_all(_config(), "audiences/custom")
        assert len(got) == 250 and [p["offset"] for p in pages] == [0, 100, 200]
        assert len(included["audiences_config_items"]) == 3

    def test_list_beyond_the_cap_raises(self):
        with patch("adloop.roku_ads.client.api_get",
                   side_effect=lambda c, p, q: {"data": [{"uid": "x"}] * 100, "meta": {"total_count": 10**6}}):
            with pytest.raises(RokuAdsApiError, match="narrow it"):
                client.api_get_all(_config(), "campaigns", max_items=300)

    def test_download_never_repeats_the_signed_url(self):
        import requests

        url = "https://reports.roku.example/r1.csv?X-Amz-Signature=SECRET"
        with patch("requests.get", side_effect=requests.ConnectionError(f"failed {url}")):
            with pytest.raises(RokuAdsApiError) as err:
                client.download(url)
        assert "SECRET" not in str(err.value) and err.value.__suppress_context__

        class _Gone:
            status_code = 403
            content = b""

        with patch("requests.get", return_value=_Gone()), pytest.raises(RokuAdsApiError) as err:
            client.download(url)
        assert "SECRET" not in str(err.value)

    @pytest.mark.parametrize("wrap", ["plain", "gzip", "zip"])
    def test_csv_in_any_wrapping(self, wrap):
        data = "﻿Date,Impressions,Spend\n10/01/2026,100,5.5\n\n".encode()
        if wrap == "gzip":
            data = gzip.compress(data)
        elif wrap == "zip":
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as archive:
                archive.writestr("r.csv", data)
            data = out.getvalue()
        assert client.parse_csv(data) == [{"Date": "10/01/2026", "Impressions": "100", "Spend": "5.5"}]


# ---------------------------------------------------------------------------
# Accounts and campaigns
# ---------------------------------------------------------------------------


class TestAccountsAndCampaigns:
    def test_account_meta_currency_code(self):
        _, ctx = _fake_api()
        with ctx:
            meta = read.account_meta(_config())
        assert meta["currency_code"] == "USD" and meta["time_zone"] == TZ

    def test_list_accounts_joins_roles(self):
        _, ctx = _fake_api({
            ("GET", "organizations"): lambda p, b: _list_page([{"uid": "Porg1", "attributes": {"name": "Example Agency"}}], p),
            ("GET", "accounts"): lambda p, b: _list_page([_ACCOUNT["data"]], p),
            ("GET", "permissions"): lambda p, b: _list_page([{"uid": "perm", "attributes": {
                "organization_uid": "Porg1", "organization_name": "Example Agency",
                "accounts": [{"account_uid": ACCOUNT, "account_name": "Acme", "roles": ["Viewer"]}]}}], p),
        })
        with ctx:
            result = read.list_roku_accounts(_config())
        account = result["accounts"][0]
        assert account["roles"] == ["Viewer"] and account["organization_name"] == "Example Agency"
        assert account["currency_code"] == "USD"

    def test_campaigns_summaries_and_insights(self):
        calls, ctx = _fake_api()
        with ctx:
            result = read.get_roku_campaigns(_config())
        assert [c["name"] for c in result["campaigns"]] == ["Acme TV - Springfield", "Acme TV - Brand"]
        group = result["campaigns"][0]["ad_groups"][0]
        assert group["schedule"] == "Mon 09–11 (account time)"
        assert group["locations_included"] == ["Springfield-Decatur IL (metro_market)"]
        assert group["frequency_cap"] == "3 per 1 days"
        assert group["audiences"]["custom_included"] == ["Site visitors"]
        joined = " ".join(result["insights"])
        assert "no approved creative" in joined
        campaign_call = next(c for c in calls if c[1] == "campaigns")
        assert campaign_call[2]["account_uid"] == [ACCOUNT]

    def test_deactivated_campaign_and_goal_without_event_group(self):
        broken = [
            {"type": "campaign", "uid": "PcmpX", "attributes": {
                "name": "Billing issue", "objective": "Conversion", "status": "deactivated", "is_enabled": True,
                "ad_groups": [dict(_LIVE_GROUP, uid="PadGrpX", status="active")]}},
            {"type": "campaign", "uid": "PcmpY", "attributes": {
                "name": "No pixel", "objective": "Conversion", "status": "active", "is_enabled": True,
                "ad_groups": [dict(_LIVE_GROUP, uid="PadGrpY", event_group_uid=None, status="active")]}},
        ]
        _, ctx = _fake_api({("GET", "campaigns"): lambda p, b: _list_page(broken, p)})
        with ctx:
            result = read.get_roku_campaigns(_config())
        joined = " ".join(result["insights"])
        assert "'Billing issue' is deactivated by Roku" in joined
        assert "'No pixel' optimizes for Leads without an event group" in joined
        # A deactivated parent overrides the active ad group.
        group = result["campaigns"][0]["ad_groups"][0]
        assert group["status"] == "active" and group["serving"] is False and group["serving_note"] == "campaign deactivated"


# ---------------------------------------------------------------------------
# Synchronous metrics
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_campaign_totals_computed_from_counts(self):
        calls, ctx = _fake_api()
        with ctx:
            result = read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-07")
        metric_calls = [c for c in calls if c[1] == "metrics"]
        assert len(metric_calls) == 1
        params = metric_calls[0][2]
        assert params["entity"] == "campaign" and params["granularity"] == "total"
        assert params["entity_uids"] == ["Pcmp1", "Pcmp2", "Pcmp3"]
        assert "action_lead" in params["fields"] and params["attributed_events_by"] == "time_of_impression"
        row = next(r for r in result["rows"] if r["campaign_uid"] == "Pcmp1")
        assert row["spend"] == 50.0 and row["impressions"] == 2500 and row["cpm"] == 20.0
        assert row["vcr"] == 0.9 and row["actions"] == {"lead": 1} and row["cpa"] == {"lead": 50.0}
        assert row["name"] == "Acme TV - Springfield" and row["optimization_goals"] == ["Leads"]
        assert result["actions_requested"] == ["lead"]
        assert result["attribution"]["default_optimizations"][0]["attribution_window"] == ["click_14d", "view_14d"]

    def test_long_windows_are_read_in_seven_day_pieces_and_added_up(self):
        calls, ctx = _fake_api()
        with ctx:
            result = read.get_roku_performance(_config(), level="ad_group", date_range_start="2026-10-01",
                                               date_range_end="2026-10-10")
        pieces = [(c[2]["start_date"], c[2]["end_date"]) for c in calls if c[1] == "metrics"]
        assert pieces == [("2026-10-01", "2026-10-07"), ("2026-10-08", "2026-10-10")]
        row = next(r for r in result["rows"] if r["ad_group_uid"] == "PadGrp1")
        assert row["impressions"] == 5000 and row["spend"] == 100.0
        assert row["campaign_name"] == "Acme TV - Springfield" and row["optimization_goal"] == "Leads"

    def test_daily_rows_follow_the_dates(self):
        def daily(params, body):
            return {"data": [{"dimensions": {"entity_uid": "Pcmp1", "date": ["2026-10-01", "2026-10-02"]},
                              "fields": {"impressions": [100, 200], "billed_local_micro_spend": [1_000_000, 2_000_000]}}],
                    "included": {}, "meta": {"pagination": {"has_more": False}, "warnings": ["partial data"]}}

        _, ctx = _fake_api({("GET", "metrics"): daily})
        with ctx:
            result = read.get_roku_performance(_config(), breakdown="date", campaign_uid="Pcmp1",
                                               date_range_start="2026-10-01", date_range_end="2026-10-02",
                                               actions=["page_view"], include_raw=True)
        assert [(r["date"], r["impressions"], r["spend"]) for r in result["rows"]] == [
            ("2026-10-01", 100, 1.0), ("2026-10-02", 200, 2.0)]
        assert "Roku: partial data" in result["insights"]
        assert result["raw_first_page"]["data"][0]["dimensions"]["entity_uid"] == "Pcmp1"

    def test_cursor_pages_are_followed(self):
        pages = []

        def paged(params, body):
            pages.append(dict(params))
            if "cursor" not in params:
                return {"data": [{"dimensions": {"campaign_uid": "Pcmp1"}, "fields": {"impressions": [10]}}],
                        "meta": {"pagination": {"has_more": True, "next_cursor": "c2"}}}
            return {"data": [{"dimensions": {"campaign_uid": "Pcmp2"}, "fields": {"impressions": [5]}}],
                    "meta": {"pagination": {"has_more": False, "next_cursor": None}}}

        _, ctx = _fake_api({("GET", "metrics"): paged})
        with ctx:
            result = read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-03")
        assert pages[1] == {"account_uid": ACCOUNT, "cursor": "c2", "limit": "1000"}
        assert result["totals"]["impressions"] == 15

    def test_rows_without_an_entity_key_are_asked_one_uid_at_a_time(self):
        seen = []

        def anonymous(params, body):
            uids = params["entity_uids"].split(",")
            seen.append(uids)
            impressions = {"Pcmp1": 100, "Pcmp2": 7, "Pcmp3": 0}
            return {"data": [{"dimensions": {}, "fields": {"impressions": [impressions[u]]}} for u in uids],
                    "meta": {"pagination": {"has_more": False}}}

        _, ctx = _fake_api({("GET", "metrics"): anonymous})
        with ctx:
            result = read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-03")
        assert seen == [["Pcmp1", "Pcmp2", "Pcmp3"], ["Pcmp1"], ["Pcmp2"], ["Pcmp3"]]
        by_uid = {r["campaign_uid"]: r["impressions"] for r in result["rows"]}
        assert by_uid == {"Pcmp1": 100, "Pcmp2": 7, "Pcmp3": 0}

    def test_refused_action_fields_fall_back_to_delivery(self):
        def picky(params, body):
            if "action_lead" in str(params.get("fields")):
                raise RokuAdsApiError("Roku Ads API returned 422: unknown field", status=422)
            return _default_metrics(params, body)

        _, ctx = _fake_api({("GET", "metrics"): picky})
        with ctx:
            result = read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-07")
        assert result["actions_requested"] == ["lead"]
        assert result["totals"]["actions"] is None and all(r["actions"] is None for r in result["rows"])
        joined = " ".join(result["insights"])
        assert "refused the action fields" in joined and "could not be read" in joined
        assert result["totals"]["impressions"] == 7500

    def test_spend_without_goal_actions_is_flagged(self):
        def quiet(params, body):
            payload = _default_metrics(params, body)
            for row in payload["data"]:
                if "action_lead" in row["fields"]:
                    row["fields"]["action_lead"] = [0]
            return payload

        _, ctx = _fake_api({("GET", "metrics"): quiet})
        with ctx:
            result = read.get_roku_performance(_config(), level="ad_group", date_range_start="2026-10-01",
                                               date_range_end="2026-10-07")
        assert any("no attributed lead" in i for i in result["insights"])

    def test_guards(self):
        with pytest.raises(ValueError, match="whole account"):
            read.get_roku_performance(_config(), level="account", campaign_uid="Pcmp1")
        with pytest.raises(ValueError, match="Unknown action"):
            read.get_roku_performance(_config(), actions=["phone_call"])
        _, ctx = _fake_api()
        with ctx, pytest.raises(ValueError, match="entity_uids"):
            read.get_roku_performance(_config(), level="creative")
        with ctx, pytest.raises(ValueError, match="92 days"):
            read.get_roku_performance(_config(), date_range_start="2026-01-01", date_range_end="2026-10-01")


# ---------------------------------------------------------------------------
# Asynchronous reports
# ---------------------------------------------------------------------------


_CSV = (
    "Campaign ID,Campaign Name,Activity Name,Impressions,Spend,Actions,Household Reach\n"
    "Pcmp1,Acme TV - Springfield,lead,2500,50.00,1,1900\n"
    "Pcmp2,Acme TV - Brand,lead,1000,20.00,0,800\n"
).encode()


class TestReports:
    def _routes(self, statuses):
        state = {"polls": 0}

        def status(params, body):
            state["polls"] += 1
            value = statuses[min(state["polls"] - 1, len(statuses) - 1)]
            attrs = {"status": value}
            if value == "done":
                attrs["download_url"] = "https://files.roku.example/r1.csv?sig=SECRET"
            return {"data": {"uid": "Prep1", "type": "report_status", "attributes": attrs}}

        return {
            ("POST", "reports"): {"data": [{"type": "report", "uid": "Prep1", "attributes": {"status": "pending"}}]},
            ("GET", "reports/Prep1/status"): status,
        }

    def test_report_ready_within_the_wait(self):
        calls, ctx = _fake_api(self._routes(["running", "done"]))

        class _File:
            status_code = 200
            content = _CSV

        with ctx, patch("requests.get", return_value=_File()):
            result = read.run_roku_report(_config(), metrics=["impressions", "spend", "actions", "household_reach"],
                                          dimensions=["campaign_id"], campaign_uid="Pcmp1",
                                          date_range_start="2026-10-01", date_range_end="2026-10-31",
                                          sleep=lambda _s: None)
        body = next(c[3] for c in calls if c[1] == "reports")
        attrs = body["data"][0]["attributes"]
        assert attrs["dimensions"] == ["campaign_id", "activity_name"]
        assert attrs["filters"] == [{"metric": "account_uid", "value": ACCOUNT},
                                    {"metric": "campaign_uid", "value": "Pcmp1"}]
        assert attrs["start_at"] == "2026-10-01T00:00:00-07:00" and attrs["end_at"] == "2026-10-31T23:59:59-07:00"
        assert result["status"] == "done" and result["row_count"] == 1
        assert result["totals"] == {"Impressions": 2500.0, "Spend": 50.0, "Actions": 1.0}
        assert "Household Reach" not in result["totals"]
        assert "SECRET" not in json.dumps(result)
        assert any("activity_name" in n for n in result["notes"])

    def test_unfinished_report_returns_its_uid(self):
        _, ctx = _fake_api(self._routes(["running"]))
        with ctx:
            result = read.run_roku_report(_config(), wait_seconds=5, sleep=lambda _s: None)
        assert result["status"] == "running" and result["report_uid"] == "Prep1"
        assert "get_roku_report" in result["note"]

    def test_get_report_later(self):
        _, ctx = _fake_api(self._routes(["done"]))

        class _File:
            status_code = 200
            content = _CSV

        with ctx, patch("requests.get", return_value=_File()):
            result = read.get_roku_report(_config(), report_uid="Prep1", max_rows=1)
        assert result["row_count"] == 2 and len(result["rows"]) == 1 and result["truncated"] is True

    def test_unknown_metric_or_dimension(self):
        with pytest.raises(ValueError, match="clicks"):
            read.run_roku_report(_config(), metrics=["clicks"])
        with pytest.raises(ValueError, match="dma"):
            read.run_roku_report(_config(), dimensions=["dma"])


# ---------------------------------------------------------------------------
# Conversion setup
# ---------------------------------------------------------------------------


class TestConversionSetup:
    def test_healthy_account_passes(self):
        _, ctx = _fake_api()
        with ctx:
            result = read.get_roku_conversion_setup(_config(), date_range_start="2026-10-01",
                                                    date_range_end="2026-10-14")
        by_check = {c["check"]: c for c in result["checks"]}
        assert by_check["event group 'Website' (Pevt1)"]["status"] == "pass"
        assert by_check["campaign 'Acme TV - Springfield': goal"]["status"] == "pass"
        assert by_check["campaign 'Acme TV - Springfield': lead from impressions"]["status"] == "pass"
        # Fourteen days are two 7-day requests, one lead each.
        assert result["ad_group_activity"][0]["goal_actions"] == 2
        assert result["verdict"] == "ok"

    def test_no_actions_is_a_warning_not_a_failure(self):
        def quiet(params, body):
            payload = _default_metrics(params, body)
            for row in payload["data"]:
                row["fields"]["action_lead"] = [0]
            return payload

        _, ctx = _fake_api({("GET", "metrics"): quiet})
        with ctx:
            result = read.get_roku_conversion_setup(_config(), date_range_start="2026-10-01",
                                                    date_range_end="2026-10-14", min_impressions=1000)
        check = next(c for c in result["checks"] if c["check"].endswith("lead from impressions"))
        assert check["status"] == "warn" and "never reach Roku" in check["detail"]
        assert result["verdict"] == "needs attention"

    def test_missing_and_stale_event_groups(self):
        stale = [{"type": "event_group", "uid": "Pevt1", "attributes": {"name": "Website", "status": "stale"}}]
        _, ctx = _fake_api({("GET", "event_groups"): lambda p, b: _list_page(stale, p)})
        with ctx:
            result = read.get_roku_conversion_setup(_config(), date_range_start="2026-10-01", date_range_end="2026-10-14")
        assert next(c for c in result["checks"] if c["check"].startswith("event group"))["status"] == "warn"

        _, ctx = _fake_api({("GET", "event_groups"): lambda p, b: _list_page([], p)})
        with ctx:
            result = read.get_roku_conversion_setup(_config(), date_range_start="2026-10-01", date_range_end="2026-10-14")
        by_check = {c["check"]: c["status"] for c in result["checks"]}
        assert by_check["event groups"] == "fail"
        assert by_check["campaign 'Acme TV - Springfield': goal"] == "fail"
        assert result["verdict"] == "broken"


# ---------------------------------------------------------------------------
# Audiences
# ---------------------------------------------------------------------------


class TestAudiences:
    def test_sources_usage_and_problems(self):
        custom = [
            # Roku's documented shape: attributes are {key, value} pairs.
            {"type": "audience", "uid": "Paud1", "attributes": {"name": "Site visitors", "audiences_config": [
                {"type": "event_audiences", "attributes": [
                    {"key": "event_group_uid", "value": "Pevt9"},
                    {"key": "qualified_event_timeframe_value", "value": 30},
                    {"key": "qualified_event_timeframe_unit", "value": "day"},
                    {"key": "event_name", "value": ["page_view"]}]}]}},
            {"type": "audience", "uid": "Paud2", "attributes": {"name": "Past customers", "audiences_config": [
                {"type": "file_audiences", "attributes": [
                    {"key": "crm_file_uid", "value": "Pcrm1"},
                    {"key": "expiration_unit", "value": "day"},
                    {"key": "expiration_value", "value": 90}]}]}},
        ]
        included = {"audiences_config_items": [{"type": "crm_file", "uid": "Pcrm1", "attributes": {
            "filename": "customers.csv", "status": "match_invalid",
            "match_data": {"household_matched_counts": 420}}}]}
        _, ctx = _fake_api({
            ("GET", "audiences/custom"): lambda p, b: _list_page(custom, p, included),
            ("GET", "audiences/standard"): lambda p, b: _list_page([], p),
        })
        with ctx:
            result = read.get_roku_audiences(_config())
        rows = {r["audience_uid"]: r for r in result["custom_audiences"]}
        assert rows["Paud1"]["used_by"][0]["campaign_name"] == "Acme TV - Springfield"
        assert rows["Paud1"]["sources"][0]["event_group_status"] == "missing"
        assert rows["Paud2"]["sources"][0]["households_matched"] == 420
        joined = " ".join(result["insights"])
        assert "event group that is missing" in joined and "match_invalid" in joined
        assert "1 custom audience(s) are on no ad group" in joined


# ---------------------------------------------------------------------------
# Event audiences (the one write Roku's API offers)
# ---------------------------------------------------------------------------


class TestEventAudience:
    @pytest.fixture(autouse=True)
    def _store(self, tmp_path):
        from adloop.safety import preview as preview_store
        from adloop.safety.preview import InMemoryPlanStore

        preview_store.set_plan_store(InMemoryPlanStore())
        self.log = str(tmp_path / "audit.log")
        yield
        preview_store.set_plan_store(InMemoryPlanStore())

    def _cfg(self, **safety):
        from adloop.config import SafetyConfig

        cfg = _config()
        cfg.safety = SafetyConfig(require_dry_run=False, log_file=self.log, **safety)
        return cfg

    def _routes(self, groups=None, existing=()):
        return {
            ("GET", "event_groups"): lambda p, b: _list_page(groups if groups is not None else _EVENT_GROUPS, p),
            ("GET", "audiences/custom"): lambda p, b: _list_page(
                [{"uid": f"Paud{i}", "attributes": {"name": n}} for i, n in enumerate(existing)], p),
            ("POST", "audiences"): lambda p, b: {"data": [{"type": "audience", "uid": "PaudNew",
                                                           "attributes": {"name": b["data"][0]["attributes"]["name"]}}]},
        }

    def test_end_to_end_through_the_gate(self):
        from adloop.ads import write as ads_write
        from adloop.roku_ads import write as roku_write

        calls, ctx = _fake_api(self._routes())
        cfg = self._cfg()
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Site visitors 30d", event_names=["page_view"])
            assert preview["platform"] == "roku" and preview["operation"] == "roku_create_audience"
            assert preview["changes"]["event_group"] == "Website" and preview["changes"]["lookback_days"] == 30
            assert any("72 hours" in w for w in preview["warnings"])
            dry = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=True)
            assert dry["status"] == "DRY_RUN_SUCCESS" and "Roku Ads has no validate-only mode" in dry["note"]
            assert not [c for c in calls if c[0] == "POST"]
            result = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "APPLIED" and result["result"]["audience_uid"] == "PaudNew"
        body = next(c[3] for c in calls if c[0] == "POST")
        attrs = body["data"][0]["attributes"]
        assert attrs["account_uid"] == ACCOUNT and attrs["name"] == "Site visitors 30d"
        assert attrs["audiences_config"] == [{"type": "event_audiences", "attributes": [
            {"key": "event_group_uid", "value": "Pevt1"},
            {"key": "qualified_event_timeframe_value", "value": 30},
            {"key": "qualified_event_timeframe_unit", "value": "day"},
            {"key": "event_name", "value": ["page_view"]},
        ]}]

    def test_guards(self):
        from adloop.roku_ads import write as roku_write

        cfg = self._cfg()
        assert "phone_call" in json.dumps(roku_write.draft_roku_event_audience(cfg, name="Calls", event_names=["phone_call"]))
        assert "1–90" in json.dumps(roku_write.draft_roku_event_audience(cfg, name="Long", event_names=["lead"], lookback_days=120), ensure_ascii=False)
        assert "at least one event" in json.dumps(roku_write.draft_roku_event_audience(cfg, name="None", event_names=[]))
        _, ctx = _fake_api(self._routes(groups=[]))
        with ctx:
            assert "no event group" in roku_write.draft_roku_event_audience(cfg, name="X visitors", event_names=["lead"])["error"]
        stale = [{"uid": "Pevt1", "attributes": {"name": "Website", "status": "stale"}}]
        _, ctx = _fake_api(self._routes(groups=stale, existing=["Leads 30d"]))
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Leads 30d", event_names=["lead"])
        joined = " ".join(preview["warnings"])
        assert "is stale" in joined and "already exists" in joined

    def test_event_group_archived_after_the_draft_fails_the_dry_run(self):
        from adloop.ads import write as ads_write
        from adloop.roku_ads import write as roku_write

        cfg = self._cfg()
        _, ctx = _fake_api(self._routes())
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Visitors", event_names=["page_view"])
        archived = [{"uid": "Pevt1", "attributes": {"name": "Website", "is_archived": True}}]
        calls, ctx = _fake_api(self._routes(groups=archived))
        with ctx:
            dry = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=True)
            applied = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
        assert dry["status"] == "DRY_RUN_FAILED" and "archived" in dry["error"]
        assert "archived" in applied["error"] and not [c for c in calls if c[0] == "POST"]

    def test_key_value_config_is_read_back(self):
        custom = [{"uid": "Paud1", "attributes": {"name": "Visitors", "audiences_config": [
            {"type": "event_audiences", "attributes": [
                {"key": "event_group_uid", "value": "Pevt1"},
                {"key": "qualified_event_timeframe_value", "value": 30},
                {"key": "qualified_event_timeframe_unit", "value": "day"},
                {"key": "event_name", "value": ["page_view"]}]}]}}]
        _, ctx = _fake_api({
            ("GET", "audiences/custom"): lambda p, b: _list_page(custom, p),
            ("GET", "audiences/standard"): lambda p, b: _list_page([], p),
        })
        with ctx:
            row = read.get_roku_audiences(_config())["custom_audiences"][0]
        assert row["sources"][0]["events"] == ["page_view"] and row["sources"][0]["event_group"] == "Website"

    def test_dialog_names_roku(self):
        from adloop.roku_ads import write as roku_write
        from adloop.safety import preview as preview_store
        from adloop.safety.approval import _platform_label

        _, ctx = _fake_api(self._routes())
        with ctx:
            preview = roku_write.draft_roku_event_audience(self._cfg(), name="Visitors", event_names=["page_view"])
        assert _platform_label(preview_store.get_plan(preview["plan_id"])) == "Roku Ads"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class TestServer:
    def test_missing_app_gets_a_hint(self):
        from adloop import runtime, server

        runtime.set_default_config(AdLoopConfig(roku_ads=RokuAdsConfig(account_uid=ACCOUNT)))
        try:
            result = server.get_roku_campaigns()
        finally:
            runtime.set_default_config(None)
        assert result["auth_error"] == "ROKU_ADS_MISSING_CLIENT"
        assert "Ads API" in result["hint"]

    def test_health_check(self, monkeypatch):
        import adloop.ads.gaql as gaql
        import adloop.ga4.reports as reports
        from adloop import runtime, server

        monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [])
        monkeypatch.setattr(reports, "get_account_summaries", lambda _c: {"accounts": [], "total_properties": 0})
        runtime.set_default_config(AdLoopConfig())
        try:
            assert server.health_check()["roku_ads"] == "not_configured"
        finally:
            runtime.set_default_config(None)

        runtime.set_default_config(_config())
        _, ctx = _fake_api()
        try:
            with ctx:
                status = server.health_check()
        finally:
            runtime.set_default_config(None)
        assert status["roku_ads"] == "ok" and status["roku_ads_account"] == ACCOUNT
        assert "get_roku_conversion_setup" in status["tools_offered"]


# ---------------------------------------------------------------------------
# Review findings (Codex, second round): one reproduction each
# ---------------------------------------------------------------------------


def _metrics_reply(rows):
    return lambda params, body: {"data": rows, "meta": {"pagination": {"has_more": False}}}


class TestReviewFindings:
    def test_documented_audience_targeting_arrays_are_read(self):
        # Finding 3: included / excluded are arrays of objects in Roku's schema.
        group = dict(_LIVE_GROUP, audience_targeting={
            "included": [{"age": ["25-34", "35-44"], "gender": ["female"]},
                         {"custom_audiences": [{"uid": "Paud1", "name": "Site visitors", "type": "event_audiences"}]}],
            "excluded": [{"custom_audiences": [{"uid": "Paud2", "name": "Customers", "type": "file_audiences"}]}],
        })
        campaigns = [{"type": "campaign", "uid": "Pcmp1", "attributes": dict(_CAMPAIGNS[0]["attributes"], ad_groups=[group])}]
        _, ctx = _fake_api({("GET", "campaigns"): lambda p, b: _list_page(campaigns, p)})
        with ctx:
            audiences = read.get_roku_campaigns(_config())["campaigns"][0]["ad_groups"][0]["audiences"]
        assert audiences["age"] == ["25-34", "35-44"] and audiences["gender"] == ["female"]
        assert audiences["custom_included"] == ["Site visitors"] and audiences["custom_excluded"] == ["Customers"]

    def test_incomplete_audience_config_is_flagged_not_misread(self):
        # Finding 4: every documented key is required; a missing one is reported.
        custom = [{"uid": "Paud1", "attributes": {"name": "Half", "audiences_config": [
            {"type": "event_audiences", "attributes": [{"key": "event_group_uid", "value": "Pevt1"}]}]}}]
        _, ctx = _fake_api({("GET", "audiences/custom"): lambda p, b: _list_page(custom, p),
                            ("GET", "audiences/standard"): lambda p, b: _list_page([], p)})
        with ctx:
            result = read.get_roku_audiences(_config())
        source = result["custom_audiences"][0]["sources"][0]
        assert source["incomplete"] is True and "event_name" in source["missing_keys"]

    def test_undated_daily_values_that_do_not_match_the_days_are_refused(self):
        # Finding 5a: two undated values for a seven-day daily request.
        rows = [{"dimensions": {"campaign_uid": "Pcmp1"}, "fields": {"impressions": [10, 20]}}]
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx, pytest.raises(read.MetricsLayoutError, match="2 daily value"):
            read.get_roku_performance(_config(), breakdown="date", campaign_uid="Pcmp1",
                                      date_range_start="2026-10-01", date_range_end="2026-10-07")

    def test_a_row_naming_two_entities_is_not_credited_to_the_first(self):
        # Finding 5b: campaign_uid ["A", "B"] with [100, 200] must not become 300 for A.
        seen = []

        def reply(params, body):
            uids = params["entity_uids"].split(",")
            seen.append(uids)
            if len(uids) > 1:
                return {"data": [{"dimensions": {"campaign_uid": ["Pcmp1", "Pcmp2"]},
                                  "fields": {"impressions": [100, 200]}}], "meta": {"pagination": {}}}
            return {"data": [{"dimensions": {"campaign_uid": uids[0]},
                              "fields": {"impressions": [{"Pcmp1": 100, "Pcmp2": 200}.get(uids[0], 0)]}}],
                    "meta": {"pagination": {}}}

        _, ctx = _fake_api({("GET", "metrics"): reply})
        with ctx:
            result = read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-03")
        assert {r["campaign_uid"]: r["impressions"] for r in result["rows"]} == {"Pcmp1": 100, "Pcmp2": 200, "Pcmp3": 0}
        assert seen[1:] == [["Pcmp1"], ["Pcmp2"], ["Pcmp3"]]

    def test_a_row_about_an_unrequested_entity_is_refused(self):
        rows = [{"dimensions": {"campaign_uid": "PcmpOther"}, "fields": {"impressions": [5]}}]
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx, pytest.raises(read.MetricsLayoutError, match="not requested|for a request about"):
            read.get_roku_performance(_config(), campaign_uid="Pcmp1", date_range_start="2026-10-01",
                                      date_range_end="2026-10-03")

    def test_missing_values_stay_unknown(self):
        # Finding 5: a requested field Roku did not return is null, not zero.
        rows = [{"dimensions": {"campaign_uid": "Pcmp1"},
                 "fields": {"impressions": [1000], "billed_local_micro_spend": [5_000_000], "action_lead": [None]}}]
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx:
            result = read.get_roku_performance(_config(), campaign_uid="Pcmp1", date_range_start="2026-10-01",
                                               date_range_end="2026-10-03")
        row = result["rows"][0]
        assert row["actions"] == {"lead": None} and row["video_completes"] is None and row["vcr"] is None
        assert row["spend"] == 5.0 and row["cpm"] == 5.0
        assert any("unknown (null), not zero" in i for i in result["insights"])
        assert not any("no attributed lead" in i for i in result["insights"])

    def test_actions_refused_mid_window_drop_the_partial_totals(self):
        # Finding 6a: week one's actions and order value must not stand as the window's total.
        calls = []

        def reply(params, body):
            calls.append(params.get("start_date"))
            if "action_purchase" in params.get("fields", "") and params.get("start_date") == "2026-10-08":
                raise RokuAdsApiError("Roku Ads API returned 422: action fields unavailable", status=422)
            fields = params["fields"].split(",")
            values = {"impressions": [100], "billed_local_micro_spend": [1_000_000], "action_purchase": [2],
                      "order_value_purchase": [300]}
            return {"data": [{"dimensions": {"campaign_uid": params["entity_uids"]},
                              "fields": {f: values.get(f, [0]) for f in fields}}], "meta": {"pagination": {}}}

        _, ctx = _fake_api({("GET", "metrics"): reply})
        with ctx:
            result = read.get_roku_performance(_config(), campaign_uid="Pcmp1", actions=["purchase"],
                                               date_range_start="2026-10-01", date_range_end="2026-10-10")
        row = result["rows"][0]
        assert row["impressions"] == 200 and row["actions"] is None and "order_value_purchase" not in row
        assert result["totals"]["actions"] is None

    def test_conversion_audit_with_unavailable_actions_is_not_ok(self):
        # Finding 6b: unavailable counts are not zeros, and the verdict cannot be "ok".
        def reply(params, body):
            if "action_" in params.get("fields", ""):
                raise RokuAdsApiError("Roku Ads API returned 422: no", status=422)
            return _default_metrics(params, body)

        _, ctx = _fake_api({("GET", "metrics"): reply})
        with ctx:
            result = read.get_roku_conversion_setup(_config(), date_range_start="2026-10-01", date_range_end="2026-10-14")
        assert result["complete"] is False and result["verdict"] != "ok"
        assert result["ad_group_activity"][0]["goal_actions"] is None
        check = next(c for c in result["checks"] if c["check"].endswith("lead from impressions"))
        assert check["status"] == "warn" and "incomplete" in check["detail"]

    def test_requested_action_drives_the_insight(self):
        # Finding 7: a Leads ad group with actions=["page_view"] and 12 page views.
        _, ctx = _fake_api()
        with ctx:
            result = read.get_roku_performance(_config(), level="ad_group", actions=["page_view"],
                                               date_range_start="2026-10-01", date_range_end="2026-10-07")
        joined = " ".join(result["insights"])
        assert "no attributed page_view" not in joined
        assert "optimize for ['lead'], which was not requested" in joined

    def test_explicit_ids_stay_inside_the_campaign(self):
        # Finding 8.
        _, ctx = _fake_api()
        with ctx, pytest.raises(ValueError, match="not in campaign 'Pcmp1'"):
            read.get_roku_performance(_config(), campaign_uid="Pcmp1", entity_uids=["Pcmp2"])
        with ctx, pytest.raises(ValueError, match="cannot narrow ads"):
            read.get_roku_performance(_config(), level="ad", campaign_uid="Pcmp1", entity_uids=["Pad1"])

    def test_disabled_parent_is_visible_on_children_everywhere(self):
        # Finding 9: is_enabled false on an active campaign with an active ad group.
        off = [{"type": "campaign", "uid": "Pcmp1", "attributes": dict(_CAMPAIGNS[0]["attributes"], is_enabled=False)}]
        _, ctx = _fake_api({("GET", "campaigns"): lambda p, b: _list_page(off, p)})
        with ctx:
            perf = read.get_roku_performance(_config(), level="ad_group", date_range_start="2026-10-01",
                                             date_range_end="2026-10-07")
            audit = read.get_roku_conversion_setup(_config(), date_range_start="2026-10-01", date_range_end="2026-10-14")
        row = perf["rows"][0]
        assert row["status"] == "active" and row["campaign_status"] == "active"
        assert row["campaign_is_enabled"] is False and row["serving"] is False and row["serving_note"] == "campaign switched off"
        assert any("cannot serve now" in i for i in perf["insights"])
        assert audit["ad_group_activity"] == []  # not treated as live
        custom = [{"uid": "Paud1", "attributes": {"name": "Visitors", "audiences_config": []}}]
        _, ctx = _fake_api({("GET", "campaigns"): lambda p, b: _list_page(off, p),
                            ("GET", "audiences/custom"): lambda p, b: _list_page(custom, p),
                            ("GET", "audiences/standard"): lambda p, b: _list_page([], p)})
        with ctx:
            usage = read.get_roku_audiences(_config())["custom_audiences"][0]["used_by"][0]
        assert usage["serving"] is False and usage["serving_note"] == "campaign switched off"

    def test_rates_come_from_unrounded_spend(self):
        # Finding 11: 8,000 micros and one impression is a CPM of 8, not 10.
        row = read.metric_row({"billed_local_micro_spend": 8_000.0, "impressions": 1.0}, [])
        assert row["spend"] == 0.01 and row["cpm"] == 8.0

    def test_unique_household_columns_are_not_summed(self):
        # Finding 12.
        rows = [{"Date": "10/01/2026", "Impressions": "100", "Total Unique Actions": "3", "Household Reach": "80"},
                {"Date": "10/02/2026", "Impressions": "50", "Total Unique Actions": "2", "Household Reach": "40"}]
        summary = read.summarize_report(rows)
        assert summary["totals"] == {"Impressions": 150.0}
        assert any("not added up" in n for n in summary["notes"])

    def test_token_errors_never_echo_credentials(self, tmp_path):
        # Finding 13.
        creds = auth.FileRokuCredentials(token_path=tmp_path / "t.json", client_id="app", client_secret="s3cr3t-value",
                                         refresh_token="rt-secret-1")
        echo = _TokenResponse(400, {"errors": [{"status": "invalidRequest",
                                                "title": "bad client_secret s3cr3t-value for rt-secret-1"}]})
        with patch("requests.post", return_value=echo), pytest.raises(RokuAdsAuthError) as err:
            creds.token()
        assert "s3cr3t-value" not in str(err.value) and "rt-secret-1" not in str(err.value)
        assert "[redacted]" in str(err.value)

    def test_api_errors_never_echo_the_access_token(self):
        creds = _Creds()
        body = {"errors": [{"status": "invalidRequest", "title": "token at-1 not valid for this scope"}]}
        with patch("adloop.roku_ads.client._credentials", return_value=creds), \
                patch("requests.request", return_value=_Resp(422, body)), pytest.raises(RokuAdsApiError) as err:
            client.api_get(_config(), "campaigns")
        assert "at-1" not in str(err.value)

    def test_idle_connection_cannot_hold_the_sign_in_past_its_deadline(self):
        # Finding 14: an accepted connection that sends nothing.
        import socket
        import threading
        import time as _time

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        uri = f"http://127.0.0.1:{port}/cb"
        outcome: dict = {}

        def run():
            started = _time.monotonic()
            try:
                auth.listen_once(uri, timeout=0.5)
            except RokuAdsAuthError as exc:
                outcome["error"] = exc.error_code
            outcome["elapsed"] = _time.monotonic() - started

        worker = threading.Thread(target=run)
        worker.start()
        idle = None
        for _ in range(50):
            try:
                idle = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                _time.sleep(0.02)
        worker.join(timeout=5)
        if idle:
            idle.close()
        assert not worker.is_alive()
        assert outcome["error"] == "timeout" and outcome["elapsed"] < 2.0


class TestRoundThreeFindings:
    """Codex review round three (adloop-audit-2026-11-04.md), Roku findings 8 and 10."""

    @pytest.fixture(autouse=True)
    def _store(self, tmp_path):
        from adloop.safety import preview as preview_store
        from adloop.safety.preview import InMemoryPlanStore

        preview_store.set_plan_store(InMemoryPlanStore())
        self.log = str(tmp_path / "audit.log")
        yield
        preview_store.set_plan_store(InMemoryPlanStore())

    def _cfg(self):
        from adloop.config import SafetyConfig

        cfg = _config()
        cfg.safety = SafetyConfig(require_dry_run=False, log_file=self.log)
        return cfg

    def _audience_routes(self, audiences: list, *, lose_response: bool):
        from adloop.roku_ads.auth import RokuAdsApiError

        def create(params, body):
            uid = f"Paud{len(audiences) + 1}"
            audiences.append({"uid": uid, "attributes": {"name": body["data"][0]["attributes"]["name"]}})
            if lose_response:
                raise RokuAdsApiError("Could not reach the Roku Ads API (ReadTimeout)", status=0)
            return {"data": [{"type": "audience", "uid": uid, "attributes": audiences[-1]["attributes"]}]}

        return {
            ("GET", "event_groups"): lambda p, b: _list_page(_EVENT_GROUPS, p),
            ("GET", "audiences/custom"): lambda p, b: _list_page(list(audiences), p),
            ("POST", "audiences"): create,
        }

    # -- finding 8: an audience created behind a lost response is never created twice
    def test_lost_create_response_is_an_unknown_outcome_and_never_replayed(self):
        from adloop.ads import write as ads_write
        from adloop.roku_ads import write as roku_write

        audiences: list = []
        cfg = self._cfg()
        calls, ctx = _fake_api(self._audience_routes(audiences, lose_response=True))
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Site visitors 30d", event_names=["page_view"])
            first = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
            again = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
        assert first["status"] == "UNKNOWN_OUTCOME", first
        assert "get_roku_audiences" in first["error"]
        assert "No pending plan" in json.dumps(again)
        assert len([c for c in calls if c[0] == "POST"]) == 1 and len(audiences) == 1

    def test_same_name_audience_created_after_the_preview_blocks_the_apply(self):
        from adloop.ads import write as ads_write
        from adloop.roku_ads import write as roku_write

        audiences: list = []
        cfg = self._cfg()
        calls, ctx = _fake_api(self._audience_routes(audiences, lose_response=False))
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Site visitors 30d", event_names=["page_view"])
            # An earlier attempt (another plan, another session) went through meanwhile.
            audiences.append({"uid": "PaudEarlier", "attributes": {"name": "Site Visitors 30d"}})
            result = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "created after the preview (PaudEarlier)" in result["error"]
        assert not [c for c in calls if c[0] == "POST"]

    def test_a_name_that_already_existed_at_the_draft_still_applies(self):
        from adloop.ads import write as ads_write
        from adloop.roku_ads import write as roku_write

        audiences = [{"uid": "PaudOld", "attributes": {"name": "Leads 30d"}}]
        cfg = self._cfg()
        _, ctx = _fake_api(self._audience_routes(audiences, lose_response=False))
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Leads 30d", event_names=["lead"])
            assert any("already exists (PaudOld)" in w for w in preview["warnings"])
            result = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "APPLIED", result

    def test_a_refused_create_keeps_the_plan(self):
        from adloop.ads import write as ads_write
        from adloop.roku_ads import write as roku_write
        from adloop.roku_ads.auth import RokuAdsApiError
        from adloop.safety import preview as preview_store

        cfg = self._cfg()
        routes = self._audience_routes([], lose_response=False)
        routes[("POST", "audiences")] = RokuAdsApiError("Roku Ads API returned 422: name taken", status=422)
        _, ctx = _fake_api(routes)
        with ctx:
            preview = roku_write.draft_roku_event_audience(cfg, name="Visitors", event_names=["page_view"])
            result = ads_write.confirm_and_apply(cfg, plan_id=preview["plan_id"], dry_run=False)
        assert "422" in result["error"] and preview_store.get_plan(preview["plan_id"]) is not None

    # -- finding 10: a re-read that names another entity, or several, fails closed
    def test_per_uid_reread_naming_a_foreign_entity_is_refused(self):
        seen = []

        def reply(params, body):
            uids = params["entity_uids"].split(",")
            seen.append(uids)
            # Never repaired: every answer is about an entity nobody asked for.
            return {"data": [{"dimensions": {"campaign_uid": "never_requested"}, "fields": {"impressions": [99]}}],
                    "meta": {"pagination": {}}}

        _, ctx = _fake_api({("GET", "metrics"): reply})
        with ctx, pytest.raises(read.MetricsLayoutError, match="never_requested"):
            read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-03")
        assert len(seen) >= 2 and seen[1] == ["Pcmp1"]  # it did fall back to one uid at a time

    def test_per_uid_reread_naming_several_entities_is_refused(self):
        def reply(params, body):
            return {"data": [{"dimensions": {"campaign_uid": ["Pcmp1", "Pcmp2"]}, "fields": {"impressions": [100, 200]}}],
                    "meta": {"pagination": {}}}

        _, ctx = _fake_api({("GET", "metrics"): reply})
        with ctx, pytest.raises(read.MetricsLayoutError, match="several entities"):
            read.get_roku_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-03")

    def test_single_uid_request_answered_for_several_entities_is_not_credited(self):
        rows = [{"dimensions": {"campaign_uid": ["Pcmp1", "Pcmp2"]}, "fields": {"impressions": [300]}}]
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx, pytest.raises(read.MetricsLayoutError, match="several entities"):
            read.get_roku_performance(_config(), campaign_uid="Pcmp1", date_range_start="2026-10-01",
                                      date_range_end="2026-10-03")

    def test_rows_without_identity_on_a_single_uid_request_still_count(self):
        rows = [{"dimensions": {}, "fields": {"impressions": [40]}}]
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx:
            result = read.get_roku_performance(_config(), campaign_uid="Pcmp1", date_range_start="2026-10-01",
                                               date_range_end="2026-10-03")
        assert [(r["campaign_uid"], r["impressions"]) for r in result["rows"]] == [("Pcmp1", 40)]


class TestUpstreamAuditFindings:
    """Codex audit of the upstream ports (adloop-audit-2026-11-04-upstream.md), Roku findings."""

    @staticmethod
    def _daily(rows, start="2026-10-01", end="2026-10-02"):
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx:
            return read.get_roku_performance(_config(), breakdown="date", campaign_uid="Pcmp1",
                                             date_range_start=start, date_range_end=end)

    # -- finding 7: daily values land on their own day or the call fails
    def test_date_keyed_fields_are_matched_by_date_not_by_order(self):
        rows = [{"dimensions": {"campaign_uid": "Pcmp1"}, "fields": {
            "impressions": {"2026-10-01": 10, "2026-10-02": 90},
            "billed_local_micro_spend": {"2026-10-02": 9_000_000, "2026-10-01": 1_000_000}}}]
        result = self._daily(rows)
        assert [(r["date"], r["impressions"], r["spend"]) for r in result["rows"]] == [
            ("2026-10-01", 10, 1.0), ("2026-10-02", 90, 9.0)]

    @pytest.mark.parametrize("dims,fields,message", [
        ({"date": ["2026-09-01", "2026-09-02"]}, {"impressions": [10, 20]}, "outside the requested"),
        ({"date": ["2026-10-01"]}, {"impressions": [10, 20]}, "1 date(s) for 2 daily value(s)"),
        ({"date": ["2026-10-01", "2026-10-01"]}, {"impressions": [10, 20]}, "same date twice"),
        ({"date": ["1793606400", "1793692800"]}, {"impressions": [10, 20]}, "not an ISO date"),
        ({}, {"impressions": {"2026-10-01": 1}, "billed_local_micro_spend": {"2026-10-02": 5}}, "different days"),
        ({"date": ["2026-10-01"]}, {"impressions": {"2026-10-01": 1, "2026-10-02": 2}}, "contradict"),
    ])
    def test_contradictory_dates_are_refused(self, dims, fields, message):
        rows = [{"dimensions": {"campaign_uid": "Pcmp1", **dims}, "fields": fields}]
        with pytest.raises(read.MetricsLayoutError, match=re.escape(message)):
            self._daily(rows)

    def test_rows_without_dates_still_use_day_order_when_counts_match(self):
        rows = [{"dimensions": {"campaign_uid": "Pcmp1"}, "fields": {"impressions": [10, 20]}}]
        assert [(r["date"], r["impressions"]) for r in self._daily(rows)["rows"]] == [
            ("2026-10-01", 10), ("2026-10-02", 20)]

    # -- finding 8: only an active ad group in an active campaign serves
    @pytest.mark.parametrize("campaign_status,group_status,serving,note", [
        ("active", "active", True, "active"),
        ("active", "scheduled", False, "ad group scheduled, not started yet"),
        ("active", "reviewing_creative", False, "ad group creative in review"),
        ("active", "creative_needed", False, "ad group no approved creative"),
        ("scheduled", "active", False, "campaign scheduled"),
        ("active", "inactive", False, "ad group inactive"),
        ("active", "ended", False, "ad group ended"),
        ("active", "deactivated", False, "ad group deactivated"),
        ("draft", "active", False, "campaign draft"),
    ])
    def test_serving_means_active_at_both_levels(self, campaign_status, group_status, serving, note):
        campaign = {"attributes": {"status": campaign_status, "is_enabled": True}}
        got = read.effective_state(campaign, {"status": group_status})
        assert got[0] is serving and got[1].startswith(note)

    def test_planned_ad_groups_still_get_the_readiness_audit(self):
        campaign = {"attributes": {"status": "active", "is_enabled": True}}
        assert read.in_play(campaign, {"status": "creative_needed"}) and read.in_play(campaign, {"status": "scheduled"})
        assert not read.in_play(campaign, {"status": "ended"})
        assert not read.in_play({"attributes": {"status": "active", "is_archived": True}}, {"status": "active"})

    def test_inventory_and_insights_agree_on_a_group_without_creative(self):
        group = dict(_LIVE_GROUP, status="creative_needed")
        campaigns = [{"type": "campaign", "uid": "Pcmp1", "attributes": dict(_CAMPAIGNS[0]["attributes"], ad_groups=[group])}]
        _, ctx = _fake_api({("GET", "campaigns"): lambda p, b: _list_page(campaigns, p)})
        with ctx:
            result = read.get_roku_campaigns(_config())
        row = result["campaigns"][0]["ad_groups"][0]
        assert row["serving"] is False and "no approved creative" in row["serving_note"]
        assert any("no approved creative" in i for i in result["insights"])

    # -- finding 11: a blank report cell is unknown, not zero
    def test_report_totals_with_blank_cells_are_unknown(self):
        rows = [{"impressions": "100", "spend": "5.00", "actions": "2"},
                {"impressions": "", "spend": "n/a", "actions": "3"}]
        result = read.summarize_report(rows)
        assert result["totals"]["impressions"] is None and result["totals"]["spend"] is None
        assert result["totals"]["actions"] == 5
        assert any("'impressions' is blank or unreadable in 1 of 2" in n for n in result["notes"])

    def test_report_totals_count_real_zeros(self):
        rows = [{"impressions": "100", "spend": "5.00"}, {"impressions": "0", "spend": "0"}]
        assert read.summarize_report(rows)["totals"] == {"impressions": 100, "spend": 5.0}

    # -- delta audit finding 3: no branch loses or misplaces a value silently
    def test_duplicate_row_dates_with_date_keyed_fields_are_refused(self):
        rows = [{"dimensions": {"campaign_uid": "Pcmp1", "date": ["2026-10-01", "2026-10-01"]},
                 "fields": {"impressions": {"2026-10-01": 10}, "billed_local_micro_spend": [1_000_000, 9_000_000]}}]
        with pytest.raises(read.MetricsLayoutError, match="same date twice"):
            self._daily(rows)

    def test_a_total_row_dated_outside_the_window_is_refused(self):
        rows = [{"dimensions": {"campaign_uid": "Pcmp1", "date": ["2026-09-01", "2026-09-02"]},
                 "fields": {"impressions": [40, 60]}}]
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx, pytest.raises(read.MetricsLayoutError, match="outside the requested"):
            read.get_roku_performance(_config(), campaign_uid="Pcmp1", date_range_start="2026-10-01",
                                      date_range_end="2026-10-02")

    def test_an_empty_date_map_is_not_zero_delivery(self):
        rows = [{"dimensions": {"campaign_uid": "Pcmp1"}, "fields": {"impressions": {}}}]
        with pytest.raises(read.MetricsLayoutError, match="empty date map"):
            self._daily(rows)
        _, ctx = _fake_api({("GET", "metrics"): _metrics_reply(rows)})
        with ctx:
            total = read.get_roku_performance(_config(), campaign_uid="Pcmp1", date_range_start="2026-10-01",
                                              date_range_end="2026-10-02")
        assert total["rows"][0]["impressions"] is None
        assert not any("No delivery" in i for i in total["insights"])

    # -- delta audit finding 5: a scheduled ad group still gets its setup warnings
    def test_scheduled_ad_group_keeps_its_readiness_warnings(self):
        group = dict(_LIVE_GROUP, status="scheduled", location_targeting={}, frequency_caps=[],
                     frequency_cap=None, optimization_goal="Leads", event_group_uid=None)
        campaigns = [{"type": "campaign", "uid": "Pcmp1", "attributes": dict(_CAMPAIGNS[0]["attributes"], ad_groups=[group])}]
        _, ctx = _fake_api({("GET", "campaigns"): lambda p, b: _list_page(campaigns, p)})
        with ctx:
            result = read.get_roku_campaigns(_config())
        assert result["campaigns"][0]["ad_groups"][0]["serving"] is False
        joined = " ".join(result["insights"])
        assert "targets no location, so it will serve wherever Roku sells" in joined
        assert "without an event group" in joined
