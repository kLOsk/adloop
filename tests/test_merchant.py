"""Tests for the Merchant Center read tools (Merchant API v1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig, MerchantConfig
from adloop.merchant import client, read


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig()


def _patch_get(responses: dict):
    """Map path-prefix → payload for adloop.merchant.client.merchant_get.

    Records the sub-API each call selects: the Merchant API splits across
    accounts/v1, issueresolution/v1 and friends, and picking the wrong one
    404s, so the choice is asserted rather than mocked away.
    """
    calls: list[tuple[str, str, dict]] = []

    def fake(config, path, params=None, *, api=client.API_ACCOUNTS):
        calls.append((path, api, params or {}))
        for prefix, payload in responses.items():
            if path.endswith(prefix):
                return payload
        raise AssertionError(f"unexpected Merchant API path: {path}")

    return calls, patch("adloop.merchant.client.merchant_get", side_effect=fake)


class TestListAccounts:
    def test_lists_accounts_across_pages(self, config):
        page1 = {"accounts": [
            {"name": "accounts/111", "accountName": "Main Shop"},
        ], "nextPageToken": "t2"}
        page2 = {"accounts": [
            {"name": "accounts/222", "accountName": "Test", "testAccount": True},
        ]}
        pages = iter([page1, page2])
        with patch("adloop.merchant.client.merchant_get",
                   side_effect=lambda c, p, q=None: next(pages)):
            result = read.list_merchant_accounts(config)

        assert result["total"] == 2
        assert result["accounts"][0] == {
            "account_id": "111", "name": "Main Shop", "test_account": False,
        }
        assert result["accounts"][1]["test_account"] is True

    def test_no_accounts_yields_guidance(self, config):
        with patch("adloop.merchant.client.merchant_get", return_value={}):
            result = read.list_merchant_accounts(config)

        assert result["total"] == 0
        assert any("People and access" in i for i in result["insights"])


def _row(context, country, *, active=0, pending=0, disapproved=0,
         expiring=0, issues=(), name=True):
    """One aggregateProductStatuses row in the issueresolution/v1 shape.

    Counts are strings, as the API serializes int64; zero counters are
    omitted, as the API does.
    """
    stats = {
        field: str(value)
        for field, value in (
            ("activeCount", active),
            ("pendingCount", pending),
            ("disapprovedCount", disapproved),
            ("expiringCount", expiring),
        )
        if value
    }
    row = {"stats": stats, "itemLevelIssues": list(issues)}
    if context:
        row["reportingContext"] = context
    if country:
        row["country"] = country
    if name:
        row["name"] = (
            "accounts/111/aggregateProductStatuses/"
            f"{context or 'REPORTING_CONTEXT_ENUM_UNSPECIFIED'}~{country}"
        )
    return row


def _issue(code, severity, products, **extra):
    return {
        "code": code,
        "severity": severity,
        "resolution": "MERCHANT_ACTION",
        "productCount": str(products),
        "documentationUri": f"https://support.google.com/merchants/{code}",
        **extra,
    }


class TestFeedHealth:
    def _responses(self):
        return {
            "aggregateProductStatuses": {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "DE", active=900, pending=20,
                     disapproved=80, issues=[
                         _issue("image_link_broken", "DISAPPROVED", 60),
                         _issue("price_mismatch", "DISAPPROVED", 20),
                     ]),
            ]},
            "issues": {"accountIssues": [
                {"title": "Missing return policy", "severity": "CRITICAL",
                 "detail": "Add a return policy",
                 "impactedDestinations": [
                     {"reportingContext": "SHOPPING_ADS"},
                 ],
                 "documentationUri": "https://support.google.com/x"},
            ]},
        }

    def test_requires_numeric_account_id(self, config):
        result = read.get_merchant_feed_health(config, account_id="my-shop")
        assert "numeric" in result["error"]

    def test_configured_default_is_used_when_no_account_id_passed(self):
        config = AdLoopConfig(merchant=MerchantConfig(account_id="111"))
        calls, patcher = _patch_get(self._responses())
        with patcher:
            result = read.get_merchant_feed_health(config)

        assert result["account_id"] == "111"
        assert [c[0] for c in calls] == [
            "accounts/111/aggregateProductStatuses",
            "accounts/111/issues",
        ]

    def test_explicit_account_id_beats_the_configured_default(self):
        config = AdLoopConfig(merchant=MerchantConfig(account_id="111"))
        calls, patcher = _patch_get(self._responses())
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="222")

        assert result["account_id"] == "222"
        assert all(c[0].startswith("accounts/222/") for c in calls)

    def test_configured_default_is_still_validated_as_numeric(self):
        config = AdLoopConfig(merchant=MerchantConfig(account_id="my-shop"))
        result = read.get_merchant_feed_health(config)
        assert "numeric" in result["error"]

    def test_neither_argument_nor_default_returns_guided_error(self, config):
        with patch("adloop.merchant.client.merchant_get") as fake_get:
            result = read.get_merchant_feed_health(config)

        fake_get.assert_not_called()
        assert "account_id is required" in result["error"]
        assert "list_merchant_accounts" in result["hint"]
        assert "merchant.account_id" in result["hint"]

    def test_summarizes_disapprovals_and_ranks_issues(self, config):
        calls, patcher = _patch_get(self._responses())
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert [(c[0], c[1]) for c in calls] == [
            ("accounts/111/aggregateProductStatuses", "issueresolution/v1"),
            ("accounts/111/issues", "accounts/v1"),
        ]
        assert result["totals"] == {"SHOPPING_ADS": {
            "surface": "paid", "approved": 900, "pending": 20,
            "disapproved": 80, "expiring": 0,
        }}
        assert result["top_item_issues"][0]["issue"] == "image_link_broken"
        assert result["top_item_issues"][0]["affected_products"] == 60
        assert result["account_issues"][0]["severity"] == "CRITICAL"
        assert result["account_issues"][0]["reporting_contexts"] == ["SHOPPING_ADS"]
        joined = " ".join(result["insights"])
        assert "DISAPPROVED" in joined and "8.0%" in joined
        assert "CRITICAL" in joined

    def test_healthy_feed_says_so(self, config):
        _, patcher = _patch_get({
            "aggregateProductStatuses": {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "US", active=500),
            ]},
            "issues": {},
        })
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert any("healthy" in i for i in result["insights"])
        assert not any("no products at all" in i for i in result["insights"])

    def test_empty_account_is_not_called_healthy(self, config):
        _, patcher = _patch_get({"aggregateProductStatuses": {}, "issues": {}})
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert result["totals"] == {}
        assert not any("healthy" in i for i in result["insights"])
        assert "no products at all" in result["insights"][0]


class TestAggregateStatusFields:
    """Field names of AggregateProductStatus in issueresolution/v1 (#139).

    Verified against the live Discovery document (issueresolution_v1,
    rev 20261118): stats.{activeCount,pendingCount,disapprovedCount,
    expiringCount}, country, itemLevelIssues[].{code,severity,resolution,
    productCount,attribute,description,detail,documentationUri}. Reading
    the older statistics/approvedCount/countryCode/issues/numProducts names
    turned every count into 0 and reported a stocked account as empty.
    """

    def test_live_shaped_row_is_counted(self, config):
        _, patcher = _patch_get({
            "aggregateProductStatuses": {"aggregateProductStatuses": [
                {
                    "reportingContext": "DISPLAY_ADS",
                    "country": "US",
                    "stats": {"activeCount": "129", "disapprovedCount": "7"},
                    "itemLevelIssues": [{
                        "code": "identity_and_belief_policy_violation",
                        "severity": "DISAPPROVED",
                        "resolution": "MERCHANT_ACTION",
                        "documentationUri": "https://support.google.com/merchants/answer/143465",
                        "productCount": "7",
                    }],
                },
            ]},
            "issues": {},
        })
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert result["by_reporting_context"] == [{
            "reporting_context": "DISPLAY_ADS", "surface": "paid",
            "country": "US", "approved": 129, "pending": 0,
            "disapproved": 7, "expiring": 0,
        }]
        issue = result["top_item_issues"][0]
        assert issue["issue"] == "identity_and_belief_policy_violation"
        assert issue["affected_products"] == 7
        assert issue["resolution"] == "MERCHANT_ACTION"
        assert not any("no products at all" in i for i in result["insights"])
        assert any("DISPLAY_ADS 7 of 136 (5.1%)" in i for i in result["insights"])

    def test_expiring_is_reported_but_not_in_the_percentage(self, config):
        _, patcher = _patch_get({
            "aggregateProductStatuses": {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "US", active=90, disapproved=10,
                     expiring=40),
            ]},
            "issues": {},
        })
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert result["totals"]["SHOPPING_ADS"]["expiring"] == 40
        assert any("10 of 100 (10.0%)" in i for i in result["insights"])

    def test_issue_metadata_is_kept(self, config):
        _, patcher = _patch_get({
            "aggregateProductStatuses": {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "US", active=10, issues=[
                    _issue("missing_gtin", "NOT_IMPACTED", 4,
                           attribute="gtin", description="Missing GTIN",
                           detail="Add a GTIN"),
                ]),
            ]},
            "issues": {},
        })
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        issue = result["top_item_issues"][0]
        assert issue["attribute"] == "gtin"
        assert issue["description"] == "Missing GTIN"
        assert issue["severity"] == "NOT_IMPACTED"
        assert issue["reporting_contexts"] == ["SHOPPING_ADS"]


class TestAggregateStatusPagination:
    """aggregateProductStatuses returns 25 rows per page by default, and
    Google can repeat a row on a later page."""

    def _paged(self, pages):
        calls: list[dict] = []
        pages = iter(pages)

        def fake(config, path, params=None, *, api=client.API_ACCOUNTS):
            if path.endswith("aggregateProductStatuses"):
                calls.append(dict(params or {}))
                return next(pages)
            return {}

        return calls, patch("adloop.merchant.client.merchant_get", side_effect=fake)

    def test_follows_next_page_token(self, config):
        calls, patcher = self._paged([
            {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "DE", active=10)],
             "nextPageToken": "page-2"},
            {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "FR", active=5)]},
        ])
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert calls == [
            {"pageSize": 250},
            {"pageSize": 250, "pageToken": "page-2"},
        ]
        assert [r["country"] for r in result["by_reporting_context"]] == ["DE", "FR"]
        assert result["totals"]["SHOPPING_ADS"]["approved"] == 15

    def test_rows_repeated_across_pages_are_counted_once(self, config):
        repeated = _row("SHOPPING_ADS", "DE", active=10, disapproved=2,
                        issues=[_issue("image_link_broken", "DISAPPROVED", 2)])
        _, patcher = self._paged([
            {"aggregateProductStatuses": [repeated],
             "nextPageToken": "page-2"},
            {"aggregateProductStatuses": [
                repeated, _row("FREE_LISTINGS", "DE", active=12)]},
        ])
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert len(result["by_reporting_context"]) == 2
        assert result["totals"]["SHOPPING_ADS"]["approved"] == 10
        assert result["totals"]["SHOPPING_ADS"]["disapproved"] == 2
        assert result["top_item_issues"][0]["affected_products"] == 2

    def test_rows_without_a_name_are_never_dropped(self, config):
        _, patcher = self._paged([
            {"aggregateProductStatuses": [
                _row("SHOPPING_ADS", "DE", active=1, name=False),
                _row("SHOPPING_ADS", "FR", active=1, name=False),
            ]},
        ])
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert result["totals"]["SHOPPING_ADS"]["approved"] == 2

    def test_a_repeated_page_token_ends_the_loop(self, config):
        page = {"aggregateProductStatuses": [_row("SHOPPING_ADS", "DE", active=1)],
                "nextPageToken": "same"}
        calls, patcher = self._paged([page, page, page])
        with patcher:
            read.get_merchant_feed_health(config, account_id="111")

        assert len(calls) == 2


class TestPaidVersusFreeSurfaces:
    """A product is counted once per reporting context, so summing contexts
    double-counts (#139), and a disapproval in free listings does not
    starve Shopping or Performance Max."""

    def _health(self, config, rows):
        _, patcher = _patch_get({
            "aggregateProductStatuses": {"aggregateProductStatuses": rows},
            "issues": {},
        })
        with patcher:
            return read.get_merchant_feed_health(config, account_id="111")

    def test_contexts_are_never_summed(self, config):
        result = self._health(config, [
            _row("SHOPPING_ADS", "US", active=100),
            _row("DISPLAY_ADS", "US", active=100),
            _row("FREE_LISTINGS", "US", active=100),
        ])

        assert set(result["totals"]) == {"SHOPPING_ADS", "DISPLAY_ADS", "FREE_LISTINGS"}
        assert all(t["approved"] == 100 for t in result["totals"].values())
        assert "never add reporting contexts" in result["note"]

    def test_free_listing_disapprovals_are_not_a_paid_alarm(self, config):
        result = self._health(config, [
            _row("SHOPPING_ADS", "US", active=100),
            _row("DISPLAY_ADS", "US", active=100),
            _row("FREE_LISTINGS", "US", active=75, disapproved=25),
        ])
        joined = " ".join(result["insights"])

        assert "Paid surfaces (DISPLAY_ADS, SHOPPING_ADS) have no disapproved" in joined
        assert "FREE_LISTINGS 25 of 100 (25.0%)" in joined
        assert "organic exposure only" in joined
        assert "starves" not in joined
        assert not any("healthy" in i for i in result["insights"])

    def test_paid_disapprovals_are_measured_within_their_context(self, config):
        result = self._health(config, [
            _row("SHOPPING_ADS", "US", active=95, disapproved=5),
            _row("FREE_LISTINGS", "US", active=100),
            _row("FREE_LOCAL_LISTINGS", "US", active=100),
        ])
        joined = " ".join(result["insights"])

        assert "SHOPPING_ADS 5 of 100 (5.0%)" in joined
        assert "starves Shopping and Performance Max" in joined
        assert "Free listings:" not in joined

    def test_surface_is_labelled_per_row_and_paid_rows_come_first(self, config):
        result = self._health(config, [
            _row("FREE_LISTINGS", "US", active=1),
            _row("YOUTUBE_SHOPPING", "US", active=1),
            _row("SHOPPING_ADS", "US", active=1),
        ])

        assert [(r["reporting_context"], r["surface"])
                for r in result["by_reporting_context"]] == [
            ("SHOPPING_ADS", "paid"),
            ("FREE_LISTINGS", "free"),
            ("YOUTUBE_SHOPPING", "other"),
        ]

    def test_missing_context_is_recovered_from_the_resource_name(self, config):
        row = _row("FREE_LISTINGS", "US", active=3)
        del row["reportingContext"]
        result = self._health(config, [row])

        assert result["by_reporting_context"][0]["reporting_context"] == "FREE_LISTINGS"
        assert result["by_reporting_context"][0]["surface"] == "free"

    def test_rows_without_any_context_stay_out_of_the_split(self, config):
        result = self._health(config, [
            _row("SHOPPING_ADS", "US", active=100),
            _row("", "US", active=60, disapproved=40),
            _row("", "CA", active=60, disapproved=40, name=False),
        ])
        joined = " ".join(result["insights"])

        unspecified = result["totals"]["REPORTING_CONTEXT_ENUM_UNSPECIFIED"]
        assert unspecified["surface"] == "unspecified"
        assert unspecified["disapproved"] == 80
        assert "2 row(s) came back without a reporting context" in joined
        assert "Paid surfaces (SHOPPING_ADS) have no disapproved" in joined
        assert "Free listings:" not in joined

    def test_item_issues_merge_across_contexts_and_rank_paid_first(self, config):
        result = self._health(config, [
            _row("SHOPPING_ADS", "US", active=95, disapproved=5, issues=[
                _issue("price_mismatch", "DISAPPROVED", 5),
                _issue("missing_gtin", "NOT_IMPACTED", 50),
            ]),
            _row("FREE_LISTINGS", "US", active=60, disapproved=40, issues=[
                _issue("price_mismatch", "DISAPPROVED", 8),
                _issue("restricted_content", "DISAPPROVED", 30),
            ]),
            _row("FREE_LISTINGS", "CA", active=90, disapproved=10, issues=[
                _issue("restricted_content", "DISAPPROVED", 10),
            ]),
        ])
        issues = result["top_item_issues"]

        assert [i["issue"] for i in issues] == [
            "price_mismatch", "restricted_content", "missing_gtin",
        ]
        merged = issues[0]
        assert merged["affected_products"] == 8
        assert merged["reporting_contexts"] == ["FREE_LISTINGS", "SHOPPING_ADS"]
        assert merged["hits_paid_surface"] is True
        assert issues[1]["affected_products"] == 30
        assert issues[1]["hits_paid_surface"] is False
        # A warning on a paid context does not count as hitting it.
        assert issues[2]["hits_paid_surface"] is False


class TestSurfaceClassification:
    @pytest.mark.parametrize("context,surface", [
        ("SHOPPING_ADS", "paid"),
        ("DISPLAY_ADS", "paid"),
        ("VIDEO_ADS", "paid"),
        ("DEMAND_GEN_ADS", "paid"),
        ("LOCAL_INVENTORY_ADS", "paid"),
        ("FREE_LISTINGS", "free"),
        ("FREE_LOCAL_LISTINGS", "free"),
        ("FREE_LISTINGS_UCP_CHECKOUT", "free"),
        ("YOUTUBE_SHOPPING", "other"),
        ("PRODUCT_REVIEWS", "other"),
        ("REPORTING_CONTEXT_ENUM_UNSPECIFIED", "unspecified"),
    ])
    def test_surface(self, context, surface):
        assert read._surface(context) == surface


class TestDeveloperRegistration:
    """Merchant API developer registration (registerGcp) — the API blocks
    every call from an unregistered GCP project, so first use must either
    self-register (account ID known) or guide the user (listing blocked)."""

    _GOOGLE_MESSAGE = (
        "GCP project with id adloop-cloud and number 910720107180 is not "
        "registered with the merchant account. Follow these steps "
        "https://developers.google.com/merchant/api/guides/quickstart"
        "#register_as_a_developer to register the GCP project with the "
        "merchant account then try calling the API again in 5 minutes."
    )

    def test_client_raises_dedicated_exception_on_registration_401(self, config):
        from adloop.merchant import client

        class _FakeResponse:
            status_code = 401

            def json(self):
                return {"error": {"message":
                        TestDeveloperRegistration._GOOGLE_MESSAGE}}

        class _FakeSession:
            def __init__(self, *_): ...

            def request(self, *args, **kwargs):
                return _FakeResponse()

        with patch("google.auth.transport.requests.AuthorizedSession",
                   _FakeSession), \
             patch("adloop.auth.get_merchant_credentials",
                   return_value=object()):
            with pytest.raises(client.MerchantNotRegistered):
                client.merchant_get(config, "accounts")

    def test_register_gcp_posts_to_registration_endpoint(self, config):
        from adloop.merchant import client

        captured = {}

        class _FakeResponse:
            status_code = 200

            def json(self):
                return {}

        class _FakeSession:
            def __init__(self, *_): ...

            def request(self, method, url, **kwargs):
                captured["method"] = method
                captured["url"] = url
                return _FakeResponse()

        with patch("google.auth.transport.requests.AuthorizedSession",
                   _FakeSession), \
             patch("adloop.auth.get_merchant_credentials",
                   return_value=object()):
            client.register_gcp(config, "111")

        assert captured["method"] == "POST"
        assert captured["url"] == (
            "https://merchantapi.googleapis.com/accounts/v1/"
            "accounts/111/developerRegistration:registerGcp"
        )

    def test_list_accounts_unregistered_returns_guided_error(self, config):
        from adloop.merchant.client import MerchantNotRegistered

        with patch("adloop.merchant.client.merchant_get",
                   side_effect=MerchantNotRegistered(self._GOOGLE_MESSAGE)):
            result = read.list_merchant_accounts(config)

        assert result["registration_required"] is True
        assert "get_merchant_feed_health" in result["hint"]
        assert "Merchant Center" in result["hint"]
        assert "is not registered" in result["details"]

    def test_feed_health_auto_registers_then_succeeds(self, config):
        from adloop.merchant.client import MerchantNotRegistered

        state = {"calls": 0}

        def fake_get(c, path, params=None, *, api=client.API_ACCOUNTS):
            state["calls"] += 1
            if state["calls"] == 1:
                raise MerchantNotRegistered(self._GOOGLE_MESSAGE)
            if path.endswith("aggregateProductStatuses"):
                return {"aggregateProductStatuses": []}
            return {}

        registered: list[str] = []
        with patch("adloop.merchant.client.merchant_get",
                   side_effect=fake_get), \
             patch("adloop.merchant.client.register_gcp",
                   side_effect=lambda c, a: registered.append(a) or {}):
            result = read.get_merchant_feed_health(config, account_id="111")

        assert registered == ["111"]
        assert result["account_id"] == "111"
        assert "error" not in result

    def test_feed_health_registered_but_not_yet_propagated(self, config):
        from adloop.merchant.client import MerchantNotRegistered

        with patch("adloop.merchant.client.merchant_get",
                   side_effect=MerchantNotRegistered(self._GOOGLE_MESSAGE)), \
             patch("adloop.merchant.client.register_gcp",
                   return_value={}):
            result = read.get_merchant_feed_health(config, account_id="111")

        assert result["status"] == "DEVELOPER_REGISTRATION_COMPLETED"
        assert result["retry_after_seconds"] == 300
        assert "5 minutes" in result["message"]

    def test_feed_health_registration_failure_is_guided(self, config):
        from adloop.merchant.client import MerchantNotRegistered

        with patch("adloop.merchant.client.merchant_get",
                   side_effect=MerchantNotRegistered(self._GOOGLE_MESSAGE)), \
             patch("adloop.merchant.client.register_gcp",
                   side_effect=RuntimeError(
                       "Merchant API returned 403: permission denied")):
            result = read.get_merchant_feed_health(config, account_id="111")

        assert result["registration_required"] is True
        assert "403" in result["error"]
        assert "Admin access" in result["hint"]


class TestSubApiRouting:
    """The Merchant API is split into independently versioned sub-APIs and
    the sub-API is part of the URL. Every other test mocks merchant_get, so
    nothing exercised the URL the client actually builds — which is how
    aggregateProductStatuses shipped pointed at accounts/v1, where it
    answers 404 with an empty error message. Verified against the live
    Discovery documents (accounts_v1, issueresolution_v1; rev 20260903).
    """

    def _capture(self):
        captured = {}

        class _FakeResponse:
            status_code = 200

            def json(self):
                return {}

        class _FakeSession:
            def __init__(self, *_): ...

            def request(self, method, url, **kwargs):
                captured["url"] = url
                return _FakeResponse()

        return captured, patch(
            "google.auth.transport.requests.AuthorizedSession", _FakeSession
        ), patch("adloop.auth.get_merchant_credentials", return_value=object())

    def test_accounts_calls_go_to_the_accounts_sub_api(self, config):
        captured, session, creds = self._capture()
        with session, creds:
            client.merchant_get(config, "accounts")
            assert captured["url"] == (
                "https://merchantapi.googleapis.com/accounts/v1/accounts"
            )

            client.merchant_get(config, "accounts/111/issues")
            assert captured["url"] == (
                "https://merchantapi.googleapis.com/accounts/v1/"
                "accounts/111/issues"
            )

    def test_aggregate_product_statuses_goes_to_issue_resolution(self, config):
        captured, session, creds = self._capture()
        with session, creds:
            client.merchant_get(
                config,
                "accounts/111/aggregateProductStatuses",
                api=client.API_ISSUE_RESOLUTION,
            )

        assert captured["url"] == (
            "https://merchantapi.googleapis.com/issueresolution/v1/"
            "accounts/111/aggregateProductStatuses"
        )

    def test_feed_health_builds_both_real_urls(self, config):
        """End to end through the tool, with only the transport faked."""
        urls: list[str] = []

        class _FakeResponse:
            status_code = 200

            def json(self):
                return {}

        class _FakeSession:
            def __init__(self, *_): ...

            def request(self, method, url, **kwargs):
                urls.append(url)
                return _FakeResponse()

        with patch("google.auth.transport.requests.AuthorizedSession",
                   _FakeSession), \
             patch("adloop.auth.get_merchant_credentials",
                   return_value=object()):
            read.get_merchant_feed_health(config, account_id="111")

        assert urls == [
            "https://merchantapi.googleapis.com/issueresolution/v1/"
            "accounts/111/aggregateProductStatuses",
            "https://merchantapi.googleapis.com/accounts/v1/"
            "accounts/111/issues",
        ]

    def test_non_200_names_the_url_that_failed(self, config):
        """A bare 'returned 404:' with no message is what made this bug
        opaque in production; the URL has to be in the error."""

        class _FakeResponse:
            status_code = 404

            def json(self):
                return {}

        class _FakeSession:
            def __init__(self, *_): ...

            def request(self, *args, **kwargs):
                return _FakeResponse()

        with patch("google.auth.transport.requests.AuthorizedSession",
                   _FakeSession), \
             patch("adloop.auth.get_merchant_credentials",
                   return_value=object()):
            with pytest.raises(RuntimeError, match=r"404 for .*/accounts/v1/accounts/111/issues"):
                client.merchant_get(config, "accounts/111/issues")


class TestFeedHealthTool:
    @pytest.mark.asyncio
    async def test_tool_account_id_is_optional_and_uses_runtime_default(self):
        from adloop import runtime
        from adloop.server import mcp

        tool = await mcp.get_tool("get_merchant_feed_health")
        assert "account_id" not in (tool.parameters.get("required") or [])

        runtime.set_default_config(
            AdLoopConfig(merchant=MerchantConfig(account_id="333"))
        )
        try:
            with patch(
                "adloop.merchant.client.merchant_get", return_value={}
            ) as fake_get:
                result = tool.fn()
        finally:
            runtime.set_default_config(None)

        assert result["account_id"] == "333"
        assert fake_get.call_args_list[0].args[1].startswith("accounts/333/")
