"""Microsoft Advertising: config, OAuth, REST client, reports, reads, writes and the apply gate."""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig, MicrosoftAdsConfig, SafetyConfig, load_config
from adloop.microsoft_ads import auth, client, read, write
from adloop.microsoft_ads.auth import MicrosoftAdsApiError, MicrosoftAdsAuthError, MicrosoftCredentials
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore

ACCOUNT = "100200300"
CUSTOMER = "900800700"
_LOG_FILE = ""


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path):
    global _LOG_FILE
    _LOG_FILE = str(tmp_path / "audit.log")
    preview_store.set_plan_store(InMemoryPlanStore())
    read.reset_account_cache()
    auth.reset_credentials_cache()
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(**safety) -> AdLoopConfig:
    kwargs = dict(max_daily_budget=50.0, require_dry_run=False, log_file=_LOG_FILE)
    kwargs.update(safety)
    return AdLoopConfig(
        microsoft_ads=MicrosoftAdsConfig(
            developer_token="dev-token", client_id="app-id", customer_id=CUSTOMER, ad_account_id=ACCOUNT,
        ),
        safety=SafetyConfig(**kwargs),
    )


class _Creds(MicrosoftCredentials):
    def __init__(self):
        super().__init__(client_id="app-id", refresh_token="rt", access_token="at-1", expires_at=10**12)
        self.refreshes = 0

    def _refresh_locked(self):
        self.refreshes += 1
        self.access_token = f"at-{self.refreshes + 1}"
        self.expires_at = 10**12


@pytest.fixture
def creds():
    c = _Creds()
    with patch("adloop.microsoft_ads.client._credentials", return_value=c):
        yield c


# ---------------------------------------------------------------------------
# Fake API
# ---------------------------------------------------------------------------

_ACCOUNT = {"Account": {"Id": ACCOUNT, "Name": "Acme Plumbing", "Number": "X0000AAA", "CurrencyCode": "USD",
                        "TimeZone": "PacificTimeUSCanadaTijuana", "AccountLifeCycleStatus": "Active",
                        "ParentCustomerId": CUSTOMER}}
_CAMPAIGNS = {"Campaigns": [
    {"Id": "500100200", "Name": "Acme Plumbing - Search", "Status": "Active", "CampaignType": "Search",
     "DailyBudget": 35.0, "BudgetType": "DailyBudgetStandard", "BudgetId": None,
     "BiddingScheme": {"Type": "MaxClicksBiddingScheme", "MaxCpc": {"Amount": 9.0}},
     "Languages": ["English"], "GoalIds": None},
    {"Id": "600", "Name": "Shared", "Status": "Paused", "CampaignType": "Search", "DailyBudget": 50.0,
     "BudgetId": "777", "BiddingScheme": {"Type": "ManualCpcBiddingScheme"}},
]}
_CRITERIONS = {
    ("500100200", "Location"): [
        {"Type": "BiddableCampaignCriterion", "Criterion": {"Type": "LocationCriterion", "DisplayName": "62704",
                                                            "LocationId": "1", "LocationType": "PostalCode"}},
        {"Type": "NegativeCampaignCriterion", "Criterion": {"Type": "LocationCriterion", "DisplayName": "Chatham",
                                                            "LocationId": "2", "LocationType": "City"}},
    ],
    ("500100200", "LocationIntent"): [
        {"Type": "BiddableCampaignCriterion",
         "Criterion": {"Type": "LocationIntentCriterion", "IntentOption": "PeopleInOrSearchingForOrViewingPages"}},
    ],
}
_AD_GROUPS = {
    "500100200": [
        {"Id": "11", "Name": "Drains", "Status": "Active", "CpcBid": {"Amount": 4.5},
         "Network": "OwnedAndOperatedAndSyndicatedSearch", "Language": "English", "AudienceAdsBidAdjustment": 0},
        {"Id": "12", "Name": "Water heaters", "Status": "Paused", "CpcBid": {"Amount": 5.0},
         "Network": "OwnedAndOperatedOnly", "AudienceAdsBidAdjustment": -100},
    ],
    "600": [],
}
_KEYWORDS = {
    "11": [{"Id": "101", "Text": "drain cleaning", "MatchType": "Phrase", "Bid": {"Amount": None},
            "Status": "Active", "EditorialStatus": "Active"},
           {"Id": "102", "Text": "plumber", "MatchType": "Broad", "Status": "Active", "EditorialStatus": "Disapproved"}],
    "12": [{"Id": "103", "Text": "water heater", "MatchType": "Exact", "Status": "Active"}],
}
_ADS = {
    "11": [{"Id": "201", "Type": "ResponsiveSearch", "Status": "Active", "EditorialStatus": "Active",
            "Headlines": [{"Asset": {"Text": "Drain Cleaning $69", "Type": "TextAsset"}, "PinnedField": "Headline1"},
                          {"Asset": {"Text": "Springfield Plumber"}}],
            "Descriptions": [{"Asset": {"Text": "Licensed local plumbers."}}],
            "FinalUrls": ["https://example.com/drain"], "Path1": "drain"}],
    "12": [{"Id": "202", "Type": "ResponsiveSearch", "Status": "Paused", "EditorialStatus": "Active",
            "Headlines": [], "Descriptions": [], "FinalUrls": []}],
}
_TAGS = {"UetTags": [{"Id": "400500600", "Name": "Main site", "TrackingStatus": "Unverified"}]}
_GOALS = {"ConversionGoals": [
    {"Id": "9", "Name": "Web Form Lead", "Type": "Event", "Status": "Active", "TrackingStatus": "NoRecentConversions",
     "ExcludeFromBidding": False, "ConversionWindowInMinutes": 43200, "TagId": "400500600", "Scope": "Account"},
    {"Id": "10", "Name": "Thank you page", "Type": "Url", "Status": "Active", "TrackingStatus": "TagUnverified",
     "ExcludeFromBidding": True},
]}


def _csv(rows: list[dict], zipped: bool = True) -> bytes:
    if not rows:
        text = ""
    else:
        buf = io.StringIO()
        import csv as _csvmod

        writer = _csvmod.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        text = "﻿" + buf.getvalue()
    data = text.encode("utf-8")
    if not zipped:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("report.csv", data)
    return out.getvalue()


_REPORTS = {
    "CampaignPerformanceReportRequest": [
        {"CampaignId": "500100200", "CampaignName": "Acme Plumbing - Search", "CampaignStatus": "Active",
         "CampaignType": "Search", "Impressions": "1,200", "Clicks": "45", "Spend": "382.10", "Ctr": "3.75%",
         "AverageCpc": "8.49", "ConversionsQualified": "0",
         "Revenue": "0.00", "ImpressionSharePercent": "12.5", "ImpressionLostToBudgetPercent": "41.2",
         "ImpressionLostToRankAggPercent": "46.3", "AllConversionsQualified": "0"},
    ],
    "SearchQueryPerformanceReportRequest": [
        {"CampaignName": "Acme Plumbing - Search", "CampaignStatus": "Active", "AdGroupName": "Drains",
         "AdGroupStatus": "Active", "SearchQuery": "plumber riverside", "Keyword": "plumber", "BidMatchType": "Broad",
         "DeliveredMatchType": "Broad", "Impressions": "40", "Clicks": "6", "Spend": "51.00", "Ctr": "15.00%",
         "AverageCpc": "8.50", "ConversionsQualified": "0", "Revenue": "0"},
        {"CampaignName": "Acme Plumbing - Search", "CampaignStatus": "Active", "AdGroupName": "Drains",
         "AdGroupStatus": "Active", "SearchQuery": "drain cleaning springfield", "Keyword": "drain cleaning",
         "BidMatchType": "Phrase", "DeliveredMatchType": "Broad", "Impressions": "30", "Clicks": "3", "Spend": "24.00",
         "Ctr": "10.00%", "AverageCpc": "8.00", "ConversionsQualified": "1",
         "Revenue": "0"},
    ],
    "GeographicPerformanceReportRequest": [
        {"CampaignName": "Acme Plumbing - Search", "LocationType": "Physical location", "City": "Springfield",
         "State": "California", "Country": "United States", "Impressions": "500", "Clicks": "20", "Spend": "150.00",
         "ConversionsQualified": "0"},
        {"CampaignName": "Acme Plumbing - Search", "LocationType": "Location of interest", "City": "Los Angeles",
         "State": "California", "Country": "United States", "Impressions": "100", "Clicks": "10", "Spend": "90.00",
         "ConversionsQualified": "0"},
    ],
}


_DEPRECATED_COLUMNS = {
    "Conversions", "ConversionRate", "CostPerConversion",
    "AllConversions", "AllConversionRate", "AllCostPerConversion",
}


def _fake_api(extra: dict | None = None, reports: dict | None = None):
    calls: list[tuple] = []
    submitted: dict[str, dict] = {}
    reports = {**_REPORTS, **(reports or {})}

    def handle(config, service, path, body=None, *, method="POST", account_id="", customer_id=""):
        calls.append((service, path, body, method, account_id, customer_id))
        key = (service, path)
        if extra and key in extra:
            handler = extra[key]
            if isinstance(handler, Exception):
                raise handler
            return handler(body) if callable(handler) else handler
        if key == ("customer", "Account/Query"):
            return _ACCOUNT
        if key == ("campaign", "Campaigns/QueryByAccountId"):
            return _CAMPAIGNS
        if key == ("campaign", "CampaignCriterions/QueryByIds"):
            return {"CampaignCriterions": _CRITERIONS.get((body["CampaignId"], body["CriterionType"]), [])}
        if key == ("campaign", "AdGroups/QueryByCampaignId"):
            return {"AdGroups": _AD_GROUPS.get(body["CampaignId"], [])}
        if key == ("campaign", "Keywords/QueryByAdGroupId"):
            return {"Keywords": _KEYWORDS.get(body["AdGroupId"], [])}
        if key == ("campaign", "Ads/QueryByAdGroupId"):
            return {"Ads": _ADS.get(body["AdGroupId"], [])}
        if key == ("campaign", "UetTags/QueryByIds"):
            return _TAGS
        if key == ("campaign", "ConversionGoals/QueryByIds"):
            return _GOALS
        if key == ("reporting", "GenerateReport/Submit"):
            rid = f"r{len(submitted) + 1}"
            submitted[rid] = body["ReportRequest"]
            return {"ReportRequestId": rid}
        if key == ("reporting", "GenerateReport/Poll"):
            rid = body["ReportRequestId"]
            return {"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": f"https://dl.example/{rid}"}}
        if method == "PUT":
            return {"PartialErrors": []}
        raise AssertionError(f"unexpected Microsoft call {service} {path}")

    class _Download:
        status_code = 200

        def __init__(self, content):
            self.content = content

    def download(url, timeout=None):
        rid = url.rsplit("/", 1)[-1]
        request = submitted[rid]
        columns = request.get("Columns") or []
        # Like Microsoft: only the requested columns come back, and the
        # deprecated conversion columns read 0.
        rows = [
            {c: ("0" if c in _DEPRECATED_COLUMNS else r.get(c, "")) for c in columns} if columns else r
            for r in reports.get(request["Type"], [])
        ]
        return _Download(_csv(rows))

    ctx = _Patches(
        patch("adloop.microsoft_ads.client.ms_request", side_effect=handle),
        patch("requests.get", side_effect=download),
    )
    return calls, submitted, ctx


class _Patches:
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)


# ---------------------------------------------------------------------------
# Config and OAuth
# ---------------------------------------------------------------------------


class TestConfig:
    def test_block_parses(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "microsoft_ads:\n  developer_token: dt\n  client_id: app\n  customer_id: 900800700\n"
            "  ad_account_id: 100200300\n"
        )
        cfg = load_config(str(path)).microsoft_ads
        assert (cfg.developer_token, cfg.client_id, cfg.customer_id, cfg.ad_account_id) == (
            "dt", "app", "900800700", "100200300")
        assert cfg.tenant == "common" and cfg.token_path.endswith("microsoft_token.json")


class _Resp:
    def __init__(self, status: int, payload=None, headers=None, content=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = content if content is not None else (b"x" if payload is not None else b"")

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class TestAuth:
    def test_missing_client_and_refresh_token(self):
        with pytest.raises(MicrosoftAdsAuthError) as err:
            MicrosoftCredentials(client_id="", refresh_token="x")
        assert err.value.error_code == "missing_client"
        with pytest.raises(MicrosoftAdsAuthError) as err:
            MicrosoftCredentials(client_id="app", refresh_token="")
        assert "adloop microsoft-login" in str(err.value)

    def test_refresh_rotates_and_persists_the_token(self, tmp_path):
        path = tmp_path / "ms.json"
        creds = auth.FileMicrosoftCredentials(token_path=path, client_id="app", refresh_token="old")
        reply = {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600}
        with patch("requests.post", return_value=_Resp(200, reply)) as post:
            assert creds.token() == "new-at"
        sent = post.call_args.kwargs["data"]
        assert sent["grant_type"] == "refresh_token" and sent["scope"] == auth.SCOPE
        stored = json.loads(path.read_text())
        assert stored["refresh_token"] == "new-rt"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_invalid_grant_says_sign_in_again(self, tmp_path):
        creds = auth.FileMicrosoftCredentials(token_path=tmp_path / "t.json", client_id="app", refresh_token="old")
        with patch("requests.post", return_value=_Resp(400, {"error": "invalid_grant"})):
            with pytest.raises(MicrosoftAdsAuthError) as err:
                creds.token()
        assert err.value.error_code == "invalid_grant" and "microsoft-login" in str(err.value)

    def test_device_code_login_waits_then_stores(self, tmp_path):
        cfg = AdLoopConfig(microsoft_ads=MicrosoftAdsConfig(client_id="app", token_path=str(tmp_path / "ms.json")))
        replies = [
            _Resp(200, {"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://microsoft.com/devicelogin",
                        "interval": 1, "expires_in": 60, "message": "Go to the page and enter ABCD."}),
            _Resp(400, {"error": "authorization_pending"}),
            _Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
        ]
        said: list[str] = []
        with patch("requests.post", side_effect=replies):
            path = auth.device_code_login(cfg, say=said.append, sleep=lambda s: None)
        assert json.loads(path.read_text())["refresh_token"] == "rt"
        assert "ABCD" in said[0]

    def test_local_credentials_read_the_token_file(self, tmp_path):
        path = tmp_path / "ms.json"
        path.write_text(json.dumps({"refresh_token": "rt", "access_token": "at", "expires_at": 10**12}))
        cfg = AdLoopConfig(microsoft_ads=MicrosoftAdsConfig(client_id="app", token_path=str(path)))
        creds = auth.local_credentials(cfg)
        assert creds.token() == "at"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TestClient:
    def test_headers_and_body(self, creds):
        with patch("requests.request", return_value=_Resp(200, {"Campaigns": []})) as req:
            client.campaign_request(_config(), "Campaigns/QueryByAccountId", {"AccountId": ACCOUNT},
                                    account_id=ACCOUNT, customer_id=CUSTOMER)
        args, kwargs = req.call_args
        assert args == ("POST", client.HOSTS["campaign"] + "/Campaigns/QueryByAccountId")
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer at-1"
        assert headers["DeveloperToken"] == "dev-token"
        assert headers["CustomerId"] == CUSTOMER and headers["CustomerAccountId"] == ACCOUNT
        assert kwargs["json"] == {"AccountId": ACCOUNT}

    def test_missing_developer_token(self, creds):
        cfg = AdLoopConfig(microsoft_ads=MicrosoftAdsConfig(client_id="app"))
        with pytest.raises(MicrosoftAdsAuthError) as err:
            client.ms_request(cfg, "customer", "User/Query", {})
        assert err.value.error_code == "missing_developer_token"

    def test_expired_token_is_refreshed_once(self, creds):
        expired = _Resp(400, {"Type": "AdApiFaultDetail", "Errors": [{"Code": 109, "ErrorCode": "AuthenticationTokenExpired",
                                                                       "Message": "expired"}]})
        with patch("requests.request", side_effect=[expired, _Resp(200, {"ok": True})]) as req:
            assert client.ms_request(_config(), "customer", "User/Query", {}) == {"ok": True}
        assert creds.refreshes == 1
        assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer at-2"

    def test_operation_errors_are_reported(self, creds):
        fault = {"Type": "ApiFaultDetail", "TrackingId": "trk-1",
                 "OperationErrors": [{"Code": 1100, "ErrorCode": "CampaignServiceInvalidAccountId",
                                      "Message": "The account ID is invalid."}]}
        with patch("requests.request", return_value=_Resp(400, fault)):
            with pytest.raises(MicrosoftAdsApiError) as err:
                client.campaign_request(_config(), "Campaigns/QueryByAccountId", {}, account_id="1")
        assert "CampaignServiceInvalidAccountId: The account ID is invalid." in str(err.value)
        assert "trk-1" in str(err.value) and "1100" in err.value.codes

    def test_throttling_waits_briefly(self, creds):
        throttled = _Resp(400, {"Errors": [{"Code": 117, "ErrorCode": "CallRateExceeded", "Message": "slow down"}]},
                          headers={"Retry-After": "2"})
        with patch("requests.request", side_effect=[throttled, _Resp(200, {"ok": 1})]), patch("time.sleep") as sleep:
            assert client.ms_request(_config(), "customer", "User/Query", {}) == {"ok": 1}
        sleep.assert_called_once_with(2)

    def test_report_runner_polls_and_parses(self, creds):
        responses = iter([
            {"ReportRequestId": "r1"},
            {"ReportRequestStatus": {"Status": "Pending"}},
            {"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": "https://dl/r1"}},
        ])

        class _Dl:
            status_code = 200
            content = _csv([{"CampaignName": "A", "Clicks": "3"}])

        with patch("adloop.microsoft_ads.client.ms_request", side_effect=lambda *a, **k: next(responses)) as req, \
                patch("requests.get", return_value=_Dl()):
            rows = client.run_report(_config(), {"Type": "CampaignPerformanceReportRequest"},
                                     account_id=ACCOUNT, sleep=lambda s: None)
        assert rows == [{"CampaignName": "A", "Clicks": "3"}]
        submitted = req.call_args_list[0].args[3]["ReportRequest"]
        assert submitted["Format"] == "Csv" and submitted["ExcludeReportHeader"] is True

    def test_report_without_rows_has_no_file(self, creds):
        responses = iter([{"ReportRequestId": "r1"}, {"ReportRequestStatus": {"Status": "Success"}}])
        with patch("adloop.microsoft_ads.client.ms_request", side_effect=lambda *a, **k: next(responses)):
            assert client.run_report(_config(), {"Type": "X"}, account_id=ACCOUNT, sleep=lambda s: None) == []

    def test_parse_csv_skips_footer_and_bom(self):
        data = ("﻿CampaignName,Clicks\nA,3\n\"©2026 Microsoft Corporation. All rights reserved. \",\n").encode()
        assert client.parse_csv(data) == [{"CampaignName": "A", "Clicks": "3"}]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:
    def test_accounts(self):
        _, _, ctx = _fake_api({
            ("customer", "User/Query"): {"User": {"Id": 5, "UserName": "ads@x", "ContactInfo": {"Email": "a@x"}},
                                         "CustomerRoles": [{"CustomerId": CUSTOMER, "RoleId": 41, "AccountIds": None}]},
            ("customer", "Accounts/Search"): {"Accounts": [_ACCOUNT["Account"]]},
        })
        with ctx:
            result = read.list_microsoft_ads_accounts(_config())
        assert result["total"] == 1
        assert result["accounts"][0] == {
            "ad_account_id": ACCOUNT, "name": "Acme Plumbing", "number": "X0000AAA", "customer_id": CUSTOMER,
            "currency": "USD", "time_zone": "PacificTimeUSCanadaTijuana", "status": "Active", "pause_reason": None,
        }

    def test_campaigns_with_location_targeting(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_campaigns(_config())
        core = result["campaigns"][0]
        assert core["daily_budget"] == 35.0 and core["bidding"] == {"strategy": "MaxClicks", "max_cpc": 9.0}
        assert core["locations"]["included"] == ["62704 (PostalCode)"]
        assert core["locations"]["excluded"] == ["Chatham (City)"]
        assert core["locations"]["intent"] == "PeopleInOrSearchingForOrViewingPages"
        shared = result["campaigns"][1]
        assert shared["shared_budget_id"] == "777"
        joined = " ".join(result["insights"])
        assert "'PeopleIn' keeps clicks local" in joined
        assert "shared budget 777" in joined

    def test_paused_account_is_flagged(self):
        paused = {"Account": dict(_ACCOUNT["Account"], AccountLifeCycleStatus="Pause", PauseReason=4)}
        _, _, ctx = _fake_api({("customer", "Account/Query"): paused})
        with ctx:
            result = read.get_microsoft_campaigns(_config(), include_targeting=False)
        assert any("account is Pause" in i for i in result["insights"])

    def test_ad_groups_flag_partners_and_audience_network(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_ad_groups(_config())
        drains = result["ad_groups"][0]
        assert drains["cpc_bid"] == 4.5 and drains["campaign_status"] == "Active"
        joined = " ".join(result["insights"])
        assert "syndicated search partners" in joined and "Audience Network" in joined

    def test_keywords_flag_disapproved_and_broad_manual(self):
        manual = {"Campaigns": [dict(_CAMPAIGNS["Campaigns"][0], BiddingScheme={"Type": "ManualCpcBiddingScheme"})]}
        _, _, ctx = _fake_api({("campaign", "Campaigns/QueryByAccountId"): manual})
        with ctx:
            result = read.get_microsoft_keywords(_config())
        assert result["total_keywords"] == 3
        joined = " ".join(result["insights"])
        assert "'plumber'" in joined and "broad-match" in joined

    def test_ads_texts_and_groups_without_active_ads(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_ads(_config())
        ad = result["ads"][0]
        assert ad["headlines"] == ["Drain Cleaning $69 [pinned Headline1]", "Springfield Plumber"]
        assert ad["final_urls"] == ["https://example.com/drain"]
        assert any("no active ad" in i for i in result["insights"])

    def test_campaign_performance_report(self):
        _, submitted, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_performance(_config(), date_range_start="2026-10-01", date_range_end="2026-10-31")
        request = submitted["r1"]
        assert request["Type"] == "CampaignPerformanceReportRequest"
        assert request["Aggregation"] == "Summary"
        assert "ImpressionLostToBudgetPercent" in request["Columns"]
        assert request["Time"] == {
            "CustomDateRangeStart": {"Day": 1, "Month": 10, "Year": 2026},
            "CustomDateRangeEnd": {"Day": 31, "Month": 10, "Year": 2026},
            "ReportTimeZone": "PacificTimeUSCanadaTijuana",
        }
        assert request["Scope"] == {"AccountIds": [ACCOUNT]}
        row = result["rows"][0]
        assert row["clicks"] == 45 and row["impressions"] == 1200 and row["spend"] == 382.1
        assert row["ctr"] == 0.0375 and row["lost_to_budget_pct"] == 41.2
        assert row["campaign_status"] == "Active"
        joined = " ".join(result["insights"])
        assert "zero conversions" in joined

    def test_daily_breakdown_and_bad_level(self):
        _, submitted, ctx = _fake_api(reports={"AdGroupPerformanceReportRequest": []})
        with ctx:
            result = read.get_microsoft_performance(_config(), level="ad_group", breakdown="date", campaign_id="500100200")
        request = submitted["r1"]
        assert request["Aggregation"] == "Daily" and request["Columns"][0] == "TimePeriod"
        assert request["Scope"] == {"Campaigns": [{"AccountId": ACCOUNT, "CampaignId": "500100200"}]}
        assert result["insights"] == ["No delivery in this window."]
        with pytest.raises(ValueError):
            read.get_microsoft_performance(_config(), level="search_term")

    def test_search_terms_compact(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_search_terms(_config(), compact=True)
        assert result["waste_candidates"][0]["search_term"] == "plumber riverside"
        assert result["top_converters"][0]["search_term"] == "drain cleaning springfield"
        joined = " ".join(result["insights"])
        assert "negative keyword candidates" in joined and "close variants" in joined

    def test_location_report_flags_interest_traffic(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_location_performance(_config())
        assert result["totals"]["spend_from_location_of_interest"] == 90.0
        assert any("location of interest" in i for i in result["insights"])

    def test_location_report_never_repeats_a_column(self):
        _, submitted, ctx = _fake_api()
        with ctx:
            read.get_microsoft_location_performance(_config(), group_by="state")
        columns = submitted["r1"]["Columns"]
        assert len(columns) == len(set(columns))

    def test_conversion_setup_verdict(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_conversion_setup(_config(), min_clicks=30)
        assert result["verdict"] == "broken"
        by_check = {c["check"]: c for c in result["checks"]}
        assert by_check["uet tag 'Main site' (400500600)"]["status"] == "fail"
        assert by_check["goal 'Thank you page'"]["status"] == "fail"
        assert "excluded from bidding" in by_check["goal 'Thank you page'"]["detail"]
        assert by_check["goal 'Web Form Lead'"]["status"] == "warn"
        assert by_check["campaign 'Acme Plumbing - Search' conversions"]["status"] == "warn"

    def test_window_rejects_future_and_inverted_dates(self):
        future = (date.today() + timedelta(days=3)).isoformat()
        with pytest.raises(ValueError):
            read.report_window("", future, "PacificTimeUSCanadaTijuana")
        with pytest.raises(ValueError):
            read.report_window("2026-10-10", "2026-10-01", "PacificTimeUSCanadaTijuana")


# ---------------------------------------------------------------------------
# Writes and the apply gate
# ---------------------------------------------------------------------------


class TestWrites:
    def test_pause_keyword_finds_its_parents(self):
        _, _, ctx = _fake_api()
        with ctx:
            preview = write.draft_microsoft_status(_config(), action="pause", entity_type="keyword", entity_id="101")
        assert preview["platform"] == "microsoft"
        changes = preview["changes"]
        assert changes["name"] == "drain cleaning [Phrase]"
        assert changes["ad_group_id"] == "11" and changes["campaign_id"] == "500100200"
        assert changes["current_status"] == "Active" and changes["target_status"] == "Paused"

    def test_enable_warns_about_paused_parent(self):
        _, _, ctx = _fake_api()
        with ctx:
            preview = write.draft_microsoft_status(_config(), action="enable", entity_type="keyword", entity_id="103")
        assert any("'Water heaters' is Paused" in w for w in preview["warnings"])

    def test_unknown_entity(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = write.draft_microsoft_status(_config(), action="pause", entity_type="ad", entity_id="999")
        assert "was not found" in result["error"]

    def test_budget_draft_guards(self):
        _, _, ctx = _fake_api()
        with ctx:
            shared = write.draft_microsoft_campaign_budget(_config(), campaign_id="600", daily_budget=40)
            capped = write.draft_microsoft_campaign_budget(_config(max_daily_budget=30), campaign_id="500100200",
                                                           daily_budget=40)
            ok = write.draft_microsoft_campaign_budget(_config(), campaign_id="500100200", daily_budget=40)
        assert "shared budget 777" in shared["error"]
        assert "error" in capped
        assert ok["changes"]["current_daily_budget"] == 35.0 and ok["changes"]["new_daily_budget"] == 40.0
        assert any("1216.0" in w for w in ok["warnings"])

    def test_confirm_and_apply_dry_run_then_apply(self):
        from adloop.ads import write as ads_write

        calls, _, ctx = _fake_api()
        with ctx:
            plan_id = write.draft_microsoft_campaign_budget(_config(), campaign_id="500100200", daily_budget=40)["plan_id"]
            dry = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
            assert dry["status"] == "DRY_RUN_SUCCESS" and dry["checks"]["budget_cap_ok"] is True
            assert "Microsoft Advertising has no validate-only mode" in dry["note"]
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert result["status"] == "APPLIED"
        put = [c for c in calls if c[3] == "PUT"]
        assert put == [("campaign", "Campaigns",
                        {"AccountId": ACCOUNT, "Campaigns": [{"Id": "500100200", "DailyBudget": 40.0}]},
                        "PUT", ACCOUNT, CUSTOMER)]

    def test_dry_run_refuses_a_budget_changed_since_the_draft(self):
        from adloop.ads import write as ads_write

        _, _, ctx = _fake_api()
        with ctx:
            plan_id = write.draft_microsoft_campaign_budget(_config(), campaign_id="500100200", daily_budget=40)["plan_id"]
        moved = {"Campaigns": [dict(_CAMPAIGNS["Campaigns"][0], DailyBudget=45.0)]}
        _, _, ctx = _fake_api({("campaign", "Campaigns/QueryByAccountId"): moved})
        with ctx:
            dry = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
        assert dry["status"] == "DRY_RUN_FAILED" and "changed since the draft" in dry["error"]

    def test_status_apply_sends_the_ad_type_and_reports_partial_errors(self):
        _, _, ctx = _fake_api()
        with ctx:
            preview = write.draft_microsoft_status(_config(), action="pause", entity_type="ad", entity_id="201")
        plan = preview_store.get_plan(preview["plan_id"])
        failing = {"PartialErrors": [{"Index": 0, "Code": 1, "ErrorCode": "InvalidStatus", "Message": "nope"}]}
        calls, _, ctx = _fake_api({("campaign", "Ads"): failing})
        with ctx, pytest.raises(MicrosoftAdsApiError) as err:
            write.apply_plan(_config(), plan)
        assert "InvalidStatus: nope" in str(err.value)
        body = [c for c in calls if c[1] == "Ads"][0][2]
        assert body == {"AdGroupId": "11", "Ads": [{"Id": "201", "Status": "Paused", "Type": "ResponsiveSearch"}]}


class TestReviewFixes:
    """Findings from the Codex review: each test pins one of them."""

    # -- #1: the real apply re-checks what the dry run checked -------------

    def _drafted_budget(self):
        _, _, ctx = _fake_api()
        with ctx:
            return write.draft_microsoft_campaign_budget(_config(), campaign_id="500100200", daily_budget=40)["plan_id"]

    def test_apply_refuses_a_budget_changed_after_the_dry_run(self):
        from adloop.ads import write as ads_write

        plan_id = self._drafted_budget()
        _, _, ctx = _fake_api()
        with ctx:
            assert ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        moved = {"Campaigns": [dict(_CAMPAIGNS["Campaigns"][0], DailyBudget=45.0)]}
        calls, _, ctx = _fake_api({("campaign", "Campaigns/QueryByAccountId"): moved})
        with ctx:
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert "changed since the draft" in result["error"]
        assert not [c for c in calls if c[3] == "PUT"]
        # Nothing was sent, so the plan stays available.
        assert preview_store.get_plan(plan_id) is not None

    def test_apply_refuses_a_campaign_moved_to_a_shared_budget(self):
        plan = preview_store.get_plan(self._drafted_budget())
        shared = {"Campaigns": [dict(_CAMPAIGNS["Campaigns"][0], BudgetId=777)]}
        calls, _, ctx = _fake_api({("campaign", "Campaigns/QueryByAccountId"): shared})
        with ctx, pytest.raises(ValueError, match="shared budget"):
            write.apply_plan(_config(), plan)
        assert not [c for c in calls if c[3] == "PUT"]

    def test_apply_rechecks_the_budget_cap(self):
        from adloop.safety.guards import SafetyViolation

        plan = preview_store.get_plan(self._drafted_budget())
        calls, _, ctx = _fake_api()
        with ctx, pytest.raises(SafetyViolation):
            write.apply_plan(_config(max_daily_budget=30), plan)
        assert not [c for c in calls if c[3] == "PUT"]

    def test_apply_refuses_a_status_changed_since_the_draft(self):
        _, _, ctx = _fake_api()
        with ctx:
            plan = preview_store.get_plan(
                write.draft_microsoft_status(_config(), action="pause", entity_type="campaign", entity_id="500100200")["plan_id"]
            )
        paused = {"Campaigns": [dict(_CAMPAIGNS["Campaigns"][0], Status="Paused")]}
        calls, _, ctx = _fake_api({("campaign", "Campaigns/QueryByAccountId"): paused})
        with ctx, pytest.raises(ValueError, match="status changed since the draft"):
            write.apply_plan(_config(), plan)
        assert not [c for c in calls if c[3] == "PUT"]

    def test_preview_names_the_entity_for_the_approval_card(self):
        _, _, ctx = _fake_api()
        with ctx:
            preview = write.draft_microsoft_status(_config(), action="pause", entity_type="keyword", entity_id="101")
        assert preview["changes"]["entity_name"] == "drain cleaning [Phrase]"

    # -- #2: qualified conversion columns ------------------------------------

    def test_reports_ask_for_qualified_conversions_only(self):
        keyword_rows = [{
            "CampaignName": "Acme Plumbing - Search", "CampaignStatus": "Active", "AdGroupId": "11",
            "AdGroupName": "Drains", "AdGroupStatus": "Active", "KeywordId": "101", "Keyword": "drain cleaning",
            "KeywordStatus": "Active", "BidMatchType": "Phrase", "QualityScore": "7", "Impressions": "100",
            "Clicks": "10", "Spend": "80.00", "Ctr": "10.00%", "AverageCpc": "8.00",
            "ConversionsQualified": "2", "Revenue": "0",
        }]
        _, submitted, ctx = _fake_api(reports={"KeywordPerformanceReportRequest": keyword_rows})
        with ctx:
            result = read.get_microsoft_performance(_config(), level="keyword")
            read.get_microsoft_search_terms(_config())
            read.get_microsoft_location_performance(_config())
            read.get_microsoft_conversion_setup(_config())
        for request in submitted.values():
            assert not set(request["Columns"]) & _DEPRECATED_COLUMNS, request["Type"]
        row = result["rows"][0]
        assert row["conversions"] == 2 and row["conversion_rate"] == 0.2 and row["cpa"] == 40.0

    # -- #3: report scope ------------------------------------------------------

    def test_campaign_scope_leaves_the_account_out(self):
        _, submitted, ctx = _fake_api()
        with ctx:
            read.get_microsoft_search_terms(_config(), campaign_id="500100200")
        assert submitted["r1"]["Scope"] == {"Campaigns": [{"AccountId": ACCOUNT, "CampaignId": "500100200"}]}

    def test_account_level_refuses_a_campaign_id(self):
        with pytest.raises(ValueError, match="whole account"):
            read.get_microsoft_performance(_config(), level="account", campaign_id="500100200")

    # -- #5: no signed URL in errors -----------------------------------------

    def _report_until_download(self, download):
        responses = iter([
            {"ReportRequestId": "r1"},
            {"ReportRequestStatus": {"Status": "Success",
                                     "ReportDownloadUrl": "https://dl.example/r1?sig=SECRET-SIGNATURE"}},
        ])
        with patch("adloop.microsoft_ads.client.ms_request", side_effect=lambda *a, **k: next(responses)), \
                patch("requests.get", side_effect=download), pytest.raises(MicrosoftAdsApiError) as err:
            client.run_report(_config(), {"Type": "CampaignPerformanceReportRequest"},
                              account_id=ACCOUNT, sleep=lambda _s: None)
        return err.value

    def test_download_transport_error_hides_the_signed_url(self):
        import requests

        def boom(url, timeout=None):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

        err = self._report_until_download(boom)
        assert "SECRET-SIGNATURE" not in str(err) and "dl.example" not in str(err)
        assert err.__cause__ is None and err.__suppress_context__

    def test_download_http_error_hides_the_signed_url(self):
        class _Forbidden:
            status_code = 403
            content = b""

        err = self._report_until_download(lambda url, timeout=None: _Forbidden())
        assert err.status == 403 and "SECRET-SIGNATURE" not in str(err) and "dl.example" not in str(err)

    def test_api_transport_error_is_wrapped(self, creds):
        import requests

        with patch("adloop.microsoft_ads.client._credentials", return_value=creds), \
                patch("requests.request", side_effect=requests.Timeout("read timed out")), \
                pytest.raises(MicrosoftAdsApiError, match="Could not reach Microsoft Advertising"):
            client.campaign_request(_config(), "Campaigns/QueryByAccountId", {}, account_id=ACCOUNT)

    # -- #9: UET only where a goal needs it ----------------------------------

    def test_offline_only_goals_need_no_tag(self):
        offline = {"ConversionGoals": [
            {"Id": "20", "Name": "Booked jobs", "Type": "OfflineConversion", "Status": "Active",
             "TrackingStatus": "RecordingConversions", "ExcludeFromBidding": False},
        ]}
        _, _, ctx = _fake_api({
            ("campaign", "UetTags/QueryByIds"): {"UetTags": []},
            ("campaign", "ConversionGoals/QueryByIds"): offline,
        })
        with ctx:
            result = read.get_microsoft_conversion_setup(_config())
        tag = next(c for c in result["checks"] if c["check"] == "uet tag")
        assert tag["status"] == "info"
        assert result["verdict"] != "broken"

    def test_unused_unverified_tag_only_warns(self):
        tags = {"UetTags": [
            {"Id": "1", "Name": "Main", "TrackingStatus": "Active"},
            {"Id": "2", "Name": "Old agency tag", "TrackingStatus": "Unverified"},
        ]}
        goals = {"ConversionGoals": [
            {"Id": "9", "Name": "Web Form Lead", "Type": "Event", "Status": "Active", "TagId": "1",
             "TrackingStatus": "RecordingConversions", "ExcludeFromBidding": False},
        ]}
        _, _, ctx = _fake_api({
            ("campaign", "UetTags/QueryByIds"): tags,
            ("campaign", "ConversionGoals/QueryByIds"): goals,
        })
        with ctx:
            result = read.get_microsoft_conversion_setup(_config())
        by_check = {c["check"]: c for c in result["checks"]}
        assert by_check["uet tag 'Main' (1)"]["status"] == "pass"
        assert "Used by 'Web Form Lead'" in by_check["uet tag 'Main' (1)"]["detail"]
        old = by_check["uet tag 'Old agency tag' (2)"]
        assert old["status"] == "warn" and "No active goal uses this tag" in old["detail"]

    def test_tag_an_event_goal_depends_on_fails(self):
        _, _, ctx = _fake_api()
        with ctx:
            result = read.get_microsoft_conversion_setup(_config())
        tag = next(c for c in result["checks"] if c["check"] == "uet tag 'Main site' (400500600)")
        assert tag["status"] == "fail" and "Web Form Lead" in tag["detail"]

    def test_setup_and_campaigns_ask_for_campaign_goals(self):
        calls, _, ctx = _fake_api()
        with ctx:
            read.get_microsoft_conversion_setup(_config())
            read.get_microsoft_campaigns(_config(), include_targeting=False)
        bodies = [c[2] for c in calls if c[1] == "Campaigns/QueryByAccountId"]
        assert bodies and all("CampaignConversionGoal" in b.get("ReturnAdditionalFields", "").split(", ") for b in bodies)

    # -- #4: approval card ---------------------------------------------------

    def test_approval_card_labels_microsoft_plans(self):
        from pathlib import Path

        import adloop.safety as safety_pkg

        html = (Path(safety_pkg.__file__).parent / "approval_card.html").read_text()
        assert 'microsoft: "Microsoft Advertising"' in html
        assert 'op.startsWith("microsoft_")' in html


class TestServer:
    def test_missing_developer_token_gets_a_hint(self):
        from adloop import runtime, server

        runtime.set_default_config(AdLoopConfig(microsoft_ads=MicrosoftAdsConfig(client_id="app", ad_account_id="1")))
        try:
            with patch("adloop.microsoft_ads.client._credentials", return_value=_Creds()):
                result = server.get_microsoft_campaigns()
        finally:
            runtime.set_default_config(None)
        assert result["auth_error"] == "MICROSOFT_ADS_MISSING_DEVELOPER_TOKEN"
        assert "developers.ads.microsoft.com" in result["hint"]

    def test_health_check_without_microsoft(self, monkeypatch):
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
        assert status["microsoft_ads"] == "not_configured"

    def test_health_check_reports_the_account(self, monkeypatch):
        import adloop.ads.gaql as gaql
        import adloop.ga4.reports as reports
        from adloop import runtime, server

        monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [])
        monkeypatch.setattr(reports, "get_account_summaries", lambda _c: {"accounts": [], "total_properties": 0})
        runtime.set_default_config(_config())
        _, _, ctx = _fake_api()
        try:
            with ctx:
                status = server.health_check()
        finally:
            runtime.set_default_config(None)
        assert status["microsoft_ads"] == "ok" and status["microsoft_ads_account"] == ACCOUNT


class TestReviewFindingsRoundTwo:
    def test_budget_blocked_after_the_dry_run_cannot_apply(self):
        # Finding 1: draft, dry-run, then block the operation: the apply refuses.
        from adloop.ads import write as ads_write

        calls, _, ctx = _fake_api()
        cfg = _config()
        with ctx:
            plan_id = write.draft_microsoft_campaign_budget(cfg, campaign_id="500100200", daily_budget=40)["plan_id"]
            assert ads_write.confirm_and_apply(cfg, plan_id=plan_id, dry_run=True)["status"] == "DRY_RUN_SUCCESS"
            cfg.safety.blocked_operations = ["microsoft_update_campaign_budget"]
            result = ads_write.confirm_and_apply(cfg, plan_id=plan_id, dry_run=False)
        assert result["status"] == "BLOCKED" and not [c for c in calls if c[3] == "PUT"]

    def test_status_dialog_shows_the_target_status(self):
        # Finding 2: the requested status must not hide behind "more fields".
        from adloop.safety.approval import confirmation_message

        _, _, ctx = _fake_api()
        with ctx:
            preview = write.draft_microsoft_status(_config(), action="pause", entity_type="keyword", entity_id="101")
        message = confirmation_message(preview_store.get_plan(preview["plan_id"]))
        lines = message.splitlines()
        assert "- target status: Paused" in lines and "- current status: Active" in lines
        assert "more fields" not in message
        assert sum(1 for l in lines if "drain cleaning [Phrase]" in l) == 1  # no duplicate name line

    def test_token_errors_never_echo_credentials(self):
        creds = auth.MicrosoftCredentials(client_id="app", client_secret="ms-secret-xyz", refresh_token="rt-abcdef")
        echo = {"error": "invalid_grant", "error_description": "refresh token rt-abcdef rejected for ms-secret-xyz"}
        with patch("adloop.microsoft_ads.auth._post_form", return_value=(400, echo)), \
                pytest.raises(MicrosoftAdsAuthError) as err:
            creds.token()
        assert "rt-abcdef" not in str(err.value) and "ms-secret-xyz" not in str(err.value)


class TestBrowserLogin:
    """`adloop microsoft-login` signs in in the browser: security defaults block device code (530035)."""

    def _cfg(self, tmp_path):
        return AdLoopConfig(microsoft_ads=MicrosoftAdsConfig(
            developer_token="dt", client_id="app-client-id", token_path=str(tmp_path / "ms_token.json")))

    def test_authorization_code_with_pkce_end_to_end(self, tmp_path):
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlsplit

        opened: list[str] = []

        def listen(uri):
            assert uri == "http://localhost:8080/callback"
            query = parse_qs(urlsplit(opened[0]).query)
            return {"code": "auth-code-1", "state": query["state"][0]}

        with patch("adloop.microsoft_ads.auth._post_form", return_value=(200, {
            "access_token": "at", "refresh_token": "rt-new", "expires_in": 3600, "scope": auth.SCOPE,
        })) as post:
            path = auth.browser_login(self._cfg(tmp_path), login_hint="ads@contoso.onmicrosoft.com",
                                      say=lambda _m: None, open_browser=opened.append, listen=listen)
        url = urlsplit(opened[0])
        query = parse_qs(url.query)
        assert url.netloc == "login.microsoftonline.com" and url.path == "/common/oauth2/v2.0/authorize"
        assert query["client_id"] == ["app-client-id"] and query["response_type"] == ["code"]
        assert query["redirect_uri"] == ["http://localhost:8080/callback"]
        assert query["scope"] == [auth.SCOPE] and query["code_challenge_method"] == ["S256"]
        assert query["login_hint"] == ["ads@contoso.onmicrosoft.com"] and query["prompt"] == ["select_account"]
        form = post.call_args.args[1]
        assert post.call_args.args[0].endswith("/common/oauth2/v2.0/token")
        assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code-1"
        assert form["redirect_uri"] == "http://localhost:8080/callback"
        # The verifier sent at the token step matches the challenge sent at authorize.
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
        assert challenge == query["code_challenge"][0]
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "rt-new" and stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_state_mismatch_and_provider_errors(self, tmp_path):
        with patch("adloop.microsoft_ads.auth._post_form") as post, pytest.raises(MicrosoftAdsAuthError) as err:
            auth.browser_login(self._cfg(tmp_path), say=lambda _m: None, open_browser=lambda _u: None,
                               listen=lambda _u: {"code": "c", "state": "forged"})
        assert err.value.error_code == "state_mismatch" and not post.called
        with pytest.raises(MicrosoftAdsAuthError) as err:
            auth.browser_login(self._cfg(tmp_path), say=lambda _m: None, open_browser=lambda _u: None,
                               listen=lambda _u: {"error": "access_denied",
                                                  "error_description": "AADSTS530035: blocked by security defaults"})
        assert err.value.error_code == "access_denied" and "530035" in str(err.value)

    def test_refused_code_redacts_it(self, tmp_path):
        from urllib.parse import parse_qs, urlsplit

        opened: list[str] = []
        listen = lambda _u: {"code": "secret-code-123", "state": parse_qs(urlsplit(opened[0]).query)["state"][0]}  # noqa: E731
        echo = {"error": "invalid_grant", "error_description": "code secret-code-123 was already redeemed"}
        with patch("adloop.microsoft_ads.auth._post_form", return_value=(400, echo)), \
                pytest.raises(MicrosoftAdsAuthError) as err:
            auth.browser_login(self._cfg(tmp_path), say=lambda _m: None, open_browser=opened.append, listen=listen)
        assert "secret-code-123" not in str(err.value)

    def test_real_listener_receives_the_redirect(self, tmp_path):
        import socket
        import threading

        import requests

        from adloop.oauth_redirect import listen_once

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        uri = f"http://127.0.0.1:{port}/callback"
        got: dict = {}
        worker = threading.Thread(target=lambda: got.update(listen_once(uri, 10, provider="Microsoft Advertising")))
        worker.start()
        for _ in range(50):
            try:
                response = requests.get(f"{uri}?code=abc&state=xyz", timeout=2)
                break
            except requests.ConnectionError:
                import time as _t

                _t.sleep(0.05)
        worker.join(timeout=10)
        assert got == {"code": "abc", "state": "xyz"} and "Microsoft Advertising sign-in" in response.text

    def test_cli_defaults_to_the_browser_and_keeps_device_code(self, tmp_path, monkeypatch, capsys):
        import adloop

        calls = []
        monkeypatch.setattr("adloop.microsoft_ads.auth.browser_login",
                            lambda config, login_hint="": calls.append(("browser", login_hint)) or tmp_path / "t.json")
        monkeypatch.setattr("adloop.microsoft_ads.auth.device_code_login",
                            lambda config: calls.append(("device", "")) or tmp_path / "t.json")
        monkeypatch.setattr("adloop.config.load_config", lambda *a, **k: AdLoopConfig())
        monkeypatch.setattr("sys.argv", ["adloop", "microsoft-login", "--user", "ads@x.com"])
        adloop.main()
        monkeypatch.setattr("sys.argv", ["adloop", "microsoft-login", "--device-code"])
        adloop.main()
        assert calls == [("browser", "ads@x.com"), ("device", "")]


class TestUpstreamAuditFindings:
    """Codex audit of the upstream ports (adloop-audit-2026-11-04-upstream.md), Microsoft read side."""

    def test_api_errors_never_carry_credentials(self):
        # Finding 10: a fault that echoes the request must not leak a token.
        cfg = AdLoopConfig(microsoft_ads=MicrosoftAdsConfig(
            developer_token="developer-token-123456", client_id="app-id", client_secret="client-secret-abcdef",
            customer_id=CUSTOMER, ad_account_id=ACCOUNT))
        creds = _Creds()
        creds.refresh_token, creds.access_token = "refresh-token-987654", "access-token-555555"
        echo = {"Errors": [{"Code": 1001, "ErrorCode": "CampaignServiceInvalidRequest",
                            "Message": "Bearer access-token-555555 / developer-token-123456 / "
                                       "client-secret-abcdef / refresh-token-987654"}]}
        with patch("adloop.microsoft_ads.client._credentials", return_value=creds), \
                patch("requests.request", return_value=_Resp(400, echo)), \
                pytest.raises(MicrosoftAdsApiError) as err:
            client.campaign_request(cfg, "Campaigns/QueryByAccountId", {}, account_id=ACCOUNT)
        text = str(err.value)
        for secret in ("access-token-555555", "developer-token-123456", "client-secret-abcdef", "refresh-token-987654"):
            assert secret not in text
        assert "CampaignServiceInvalidRequest" in text and "[redacted]" in text and creds.refreshes == 0

    def test_a_lost_write_is_not_called_safe_to_retry(self, creds):
        import requests

        with patch("requests.request", side_effect=requests.Timeout("read timed out")), \
                pytest.raises(MicrosoftAdsApiError) as err:
            client.campaign_request(_config(), "AdExtensions", {}, account_id=ACCOUNT)
        assert "may or may not have been made" in str(err.value) and "try again" not in str(err.value)

    # -- finding 6: every Microsoft time zone has its own "today"
    @staticmethod
    def _at(monkeypatch, utc_text):
        from datetime import datetime as real, timezone as tz

        instant = real.fromisoformat(utc_text).replace(tzinfo=tz.utc)

        class Frozen(real):
            @classmethod
            def now(cls, tzinfo=None):
                return instant.astimezone(tzinfo) if tzinfo else instant.replace(tzinfo=None)

        monkeypatch.setattr(read, "datetime", Frozen)

    @pytest.mark.parametrize("zone,utc,yesterday", [
        ("OsakaSapporoTokyo", "2026-11-03T16:30:00", "2026-11-03"),      # Tokyo is already Nov 4
        ("CanberraMelbourneSydney", "2026-11-04T13:30:00", "2026-11-04"),  # Sydney (UTC+11 in Nov) is Nov 5 00:30
        ("Newfoundland", "2026-11-04T03:00:00", "2026-11-02"),           # UTC-3:30 is still Nov 3
        ("MountainTimeUSCanada", "2026-11-04T06:30:00", "2026-11-02"),   # guide spelling
        ("MountainTime_US_Canada", "2026-11-04T06:30:00", "2026-11-02"),  # table spelling
        ("InternationalDateLineWest", "2026-11-04T11:00:00", "2026-11-02"),
    ])
    def test_default_window_ends_on_the_accounts_yesterday(self, monkeypatch, zone, utc, yesterday):
        self._at(monkeypatch, utc)
        start, end = read.report_window("", "", zone)
        assert end.isoformat() == yesterday and (end - start).days == 29

    def test_every_mapped_zone_exists(self):
        from zoneinfo import ZoneInfo

        for name, iana in read._TIME_ZONES.items():
            ZoneInfo(iana)
        assert {"OsakaSapporoTokyo", "CanberraMelbourneSydney", "PacificTimeUSCanadaTijuana"} <= set(read._TIME_ZONES)

    def test_an_unknown_zone_is_said_out_loud(self):
        assert read.zone_note("PacificTimeUSCanadaTijuana") is None
        assert "not in AdLoop's Microsoft time zone table" in read.zone_note("MarsStandardTime")
