"""Tests for the Merchant Center read tools (Merchant API v1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig
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


class TestFeedHealth:
    def _responses(self):
        return {
            "aggregateProductStatuses": {"aggregateProductStatuses": [
                {
                    "reportingContext": "SHOPPING_ADS",
                    "countryCode": "DE",
                    "statistics": {"approvedCount": "900", "pendingCount": "20",
                                   "disapprovedCount": "80"},
                    "issues": [
                        {"issueType": "image_link_broken", "severity": "ERROR",
                         "numProducts": "60",
                         "documentationUri": "https://support.google.com/y"},
                        {"issueType": "price_mismatch", "severity": "ERROR",
                         "numProducts": "20",
                         "documentationUri": "https://support.google.com/z"},
                    ],
                },
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

    def test_summarizes_disapprovals_and_ranks_issues(self, config):
        calls, patcher = _patch_get(self._responses())
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert [(c[0], c[1]) for c in calls] == [
            ("accounts/111/aggregateProductStatuses", "issueresolution/v1"),
            ("accounts/111/issues", "accounts/v1"),
        ]
        assert result["totals"] == {"approved": 900, "pending": 20,
                                    "disapproved": 80}
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
                {"reportingContext": "SHOPPING_ADS",
                 "statistics": {"approvedCount": "500", "pendingCount": "0",
                                "disapprovedCount": "0"}},
            ]},
            "issues": {},
        })
        with patcher:
            result = read.get_merchant_feed_health(config, account_id="111")

        assert any("healthy" in i for i in result["insights"])


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
