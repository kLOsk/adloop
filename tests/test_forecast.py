"""Tests for Keyword Planner forecast and discovery functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.ads import forecast
from adloop.ads.client import _is_rate_limit_error, call_with_retry
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(require_dry_run=True),
    )


def _rest_idea(
    text: str,
    *,
    avg_monthly: int | None,
    competition: str,
    competition_index: int | None = None,
    low_bid_micros: int | None = None,
    high_bid_micros: int | None = None,
) -> dict:
    """Build a REST-shape keyword idea entry.

    Mirrors the v23 generateKeywordIdeas JSON response — int64 fields come
    back as strings, ``competition`` is the enum name (not a numeric code).
    """
    metrics: dict = {"competition": competition}
    if avg_monthly is not None:
        metrics["avgMonthlySearches"] = str(avg_monthly)
    if competition_index is not None:
        metrics["competitionIndex"] = str(competition_index)
    if low_bid_micros is not None:
        metrics["lowTopOfPageBidMicros"] = str(low_bid_micros)
    if high_bid_micros is not None:
        metrics["highTopOfPageBidMicros"] = str(high_bid_micros)
    return {"text": text, "keywordIdeaMetrics": metrics}


def _patch_rest(payload, monkeypatch):
    """Replace ``_post_keyword_ideas_rest_page`` with a recording fake.

    Returns (calls, fake). ``payload`` may be a single dict (returned every
    call) or a list of dicts (consumed one per page in order).
    """
    calls: list[dict] = []
    if isinstance(payload, list):
        responses = list(payload)

        def _fake(_config, _cid, body):
            calls.append(body)
            return responses.pop(0) if responses else {"results": []}
    else:

        def _fake(_config, _cid, body):
            calls.append(body)
            return payload

    monkeypatch.setattr(forecast, "_post_keyword_ideas_rest_page", _fake)
    return calls


class TestDiscoverKeywords:
    """REST-path tests for issue #37 — `discover_keywords` calls the v23 REST
    `generateKeywordIdeas` endpoint directly, bypassing the gRPC quota bucket
    that exhausts after a small number of sequential calls.
    """

    def test_requires_seed_keywords_or_url(self, config):
        result = forecast.discover_keywords(config)
        assert result["error"] == "Provide at least one of: seed_keywords or url"

    def test_seed_keywords_only_sends_keyword_seed(self, config, monkeypatch):
        calls = _patch_rest(
            {
                "results": [
                    _rest_idea(
                        "trail running shoes",
                        avg_monthly=5000,
                        competition="MEDIUM",
                        competition_index=60,
                        low_bid_micros=800_000,
                        high_bid_micros=2_000_000,
                    )
                ]
            },
            monkeypatch,
        )

        result = forecast.discover_keywords(
            config,
            seed_keywords=["trail running"],
            geo_target_id="2276",
            language_id="1000",
        )

        assert result["total_ideas"] == 1
        idea = result["keyword_ideas"][0]
        assert idea["keyword"] == "trail running shoes"
        assert idea["competition"] == "MEDIUM"
        assert idea["avg_monthly_searches"] == 5000
        assert idea["low_top_of_page_bid"] == 0.80
        assert idea["high_top_of_page_bid"] == 2.00

        body = calls[0]
        assert body["keywordSeed"] == {"keywords": ["trail running"]}
        assert "urlSeed" not in body and "keywordAndUrlSeed" not in body
        assert body["geoTargetConstants"] == ["geoTargetConstants/2276"]
        assert body["language"] == "languageConstants/1000"
        assert body["keywordPlanNetwork"] == "GOOGLE_SEARCH"

    def test_url_only_sends_url_seed(self, config, monkeypatch):
        calls = _patch_rest(
            {
                "results": [
                    _rest_idea(
                        "running gear",
                        avg_monthly=1200,
                        competition="LOW",
                        competition_index=20,
                        low_bid_micros=400_000,
                        high_bid_micros=900_000,
                    )
                ]
            },
            monkeypatch,
        )

        result = forecast.discover_keywords(
            config,
            url="https://example.com/running",
            geo_target_id="2840",
            language_id="1000",
        )

        assert result["total_ideas"] == 1
        assert result["keyword_ideas"][0]["competition"] == "LOW"
        assert result["seed_url"] == "https://example.com/running"
        assert result["seed_keywords"] == []

        body = calls[0]
        assert body["urlSeed"] == {"url": "https://example.com/running"}
        assert "keywordSeed" not in body and "keywordAndUrlSeed" not in body

    def test_keyword_and_url_sends_combined_seed(self, config, monkeypatch):
        calls = _patch_rest({"results": []}, monkeypatch)

        forecast.discover_keywords(
            config,
            seed_keywords=["a", "b"],
            url="https://example.com",
        )

        body = calls[0]
        assert body["keywordAndUrlSeed"] == {
            "url": "https://example.com",
            "keywords": ["a", "b"],
        }
        assert "keywordSeed" not in body and "urlSeed" not in body

    def test_ideas_sorted_by_avg_monthly_searches_descending(self, config, monkeypatch):
        _patch_rest(
            {
                "results": [
                    _rest_idea("low volume", avg_monthly=100, competition="LOW"),
                    _rest_idea(
                        "high volume",
                        avg_monthly=50000,
                        competition="HIGH",
                        low_bid_micros=1_000_000,
                        high_bid_micros=3_000_000,
                    ),
                    _rest_idea(
                        "mid volume",
                        avg_monthly=5000,
                        competition="MEDIUM",
                        low_bid_micros=500_000,
                        high_bid_micros=1_500_000,
                    ),
                ]
            },
            monkeypatch,
        )

        result = forecast.discover_keywords(config, seed_keywords=["running"])
        volumes = [i["avg_monthly_searches"] for i in result["keyword_ideas"]]
        assert volumes == sorted(volumes, reverse=True)

    def test_insights_surface_competition_breakdown(self, config, monkeypatch):
        _patch_rest(
            {
                "results": [
                    _rest_idea("cheap option", avg_monthly=200, competition="LOW"),
                    _rest_idea(
                        "popular term",
                        avg_monthly=8000,
                        competition="HIGH",
                        low_bid_micros=2_000_000,
                        high_bid_micros=5_000_000,
                    ),
                ]
            },
            monkeypatch,
        )

        result = forecast.discover_keywords(config, seed_keywords=["option"])
        assert any("high-competition" in i for i in result["insights"])
        assert any("low-competition" in i for i in result["insights"])

    def test_page_size_capped_at_1000(self, config, monkeypatch):
        calls = _patch_rest({"results": []}, monkeypatch)

        forecast.discover_keywords(config, seed_keywords=["test"], page_size=9999)

        assert calls[0]["pageSize"] == 1000

    def test_pagination_concatenates_all_pages(self, config, monkeypatch):
        """next_page_token in the response means we keep paging until empty."""
        calls = _patch_rest(
            [
                {
                    "results": [
                        _rest_idea("kw1", avg_monthly=100, competition="LOW")
                    ],
                    "nextPageToken": "page2",
                },
                {
                    "results": [
                        _rest_idea("kw2", avg_monthly=200, competition="LOW")
                    ],
                },
            ],
            monkeypatch,
        )

        result = forecast.discover_keywords(config, seed_keywords=["test"])

        assert result["total_ideas"] == 2
        assert len(calls) == 2
        assert "pageToken" not in calls[0]
        assert calls[1]["pageToken"] == "page2"

    def test_empty_seed_keywords_without_url_returns_error(self, config):
        result = forecast.discover_keywords(config, seed_keywords=[])
        assert "error" in result

    def test_default_seed_keywords_is_empty_list_not_none(self, config):
        """seed_keywords default must be [] so the MCP schema is array, not anyOf[array,null]."""
        import inspect
        sig = inspect.signature(forecast.discover_keywords)
        default = sig.parameters["seed_keywords"].default
        assert default == []
        assert default is not None


class TestZeroValuePreservation:
    """REST int64 fields can legitimately be 0 — bid bounds for keywords
    with no bid data, competition index for terms Google has no signal on,
    avg monthly searches for ultra-niche queries. The previous code used
    falsy checks (``int(v) if v else None``) which silently mapped a real
    ``0`` to ``None`` and lost the value. Cover each path explicitly so
    a future regression can't reintroduce the falsy-check pattern.
    """

    def test_zero_low_bid_micros_preserved_as_zero(self, config, monkeypatch):
        _patch_rest(
            {
                "results": [
                    _rest_idea(
                        "ultra niche term",
                        avg_monthly=10,
                        competition="LOW",
                        competition_index=1,
                        low_bid_micros=0,
                        high_bid_micros=500_000,
                    )
                ]
            },
            monkeypatch,
        )

        result = forecast.discover_keywords(config, seed_keywords=["niche"])
        idea = result["keyword_ideas"][0]
        assert idea["low_top_of_page_bid"] == 0.0
        assert idea["low_top_of_page_bid"] is not None
        assert idea["high_top_of_page_bid"] == 0.50

    def test_zero_high_bid_micros_preserved_as_zero(self, config, monkeypatch):
        _patch_rest(
            {
                "results": [
                    _rest_idea(
                        "no bid data term",
                        avg_monthly=50,
                        competition="LOW",
                        low_bid_micros=0,
                        high_bid_micros=0,
                    )
                ]
            },
            monkeypatch,
        )

        idea = forecast.discover_keywords(config, seed_keywords=["x"])[
            "keyword_ideas"
        ][0]
        assert idea["low_top_of_page_bid"] == 0.0
        assert idea["high_top_of_page_bid"] == 0.0
        # Sanity check the explicit-None case to make sure we didn't break it.
        assert all(
            idea[k] is not None for k in ("low_top_of_page_bid", "high_top_of_page_bid")
        )

    def test_zero_competition_index_preserved(self, config, monkeypatch):
        _patch_rest(
            {
                "results": [
                    _rest_idea(
                        "zero-competition keyword",
                        avg_monthly=5,
                        competition="LOW",
                        competition_index=0,
                    )
                ]
            },
            monkeypatch,
        )

        idea = forecast.discover_keywords(config, seed_keywords=["x"])[
            "keyword_ideas"
        ][0]
        assert idea["competition_index"] == 0
        assert idea["competition_index"] is not None

    def test_zero_avg_monthly_preserved(self, config, monkeypatch):
        _patch_rest(
            {
                "results": [
                    _rest_idea(
                        "ultra rare term",
                        avg_monthly=0,
                        competition="LOW",
                    )
                ]
            },
            monkeypatch,
        )

        idea = forecast.discover_keywords(config, seed_keywords=["x"])[
            "keyword_ideas"
        ][0]
        assert idea["avg_monthly_searches"] == 0
        assert idea["avg_monthly_searches"] is not None

    def test_missing_fields_still_return_none(self, config, monkeypatch):
        """Don't over-correct — an absent field is still None, not 0."""
        _patch_rest(
            {
                "results": [
                    # No metrics at all — every numeric field absent.
                    {"text": "no data idea", "keywordIdeaMetrics": {}}
                ]
            },
            monkeypatch,
        )

        idea = forecast.discover_keywords(config, seed_keywords=["x"])[
            "keyword_ideas"
        ][0]
        assert idea["avg_monthly_searches"] is None
        assert idea["competition_index"] is None
        assert idea["low_top_of_page_bid"] is None
        assert idea["high_top_of_page_bid"] is None
        # competition still defaults to UNSPECIFIED, not None.
        assert idea["competition"] == "UNSPECIFIED"


class TestMaybeIntHelper:
    """Unit-level coverage for ``_maybe_int`` — the proto3-JSON int64 parser
    underpinning the zero-value preservation fix above.
    """

    def test_none_returns_none(self):
        assert forecast._maybe_int(None) is None

    def test_empty_string_returns_none(self):
        # Invalid int64 wire format, defensively mapped to None.
        assert forecast._maybe_int("") is None

    def test_string_zero_returns_zero(self):
        # The actual bug regression — `"0"` is the wire format REST sends
        # for a legitimate int64=0 and must round-trip to int 0, not None.
        assert forecast._maybe_int("0") == 0

    def test_native_zero_returns_zero(self):
        # Defensive: if a caller ever passes a native int 0, preserve it.
        assert forecast._maybe_int(0) == 0

    def test_string_positive_int(self):
        assert forecast._maybe_int("12345") == 12345

    def test_native_positive_int(self):
        assert forecast._maybe_int(12345) == 12345

    def test_invalid_string_returns_none(self):
        # Don't crash the whole response on malformed data — just drop the
        # field. Surfacing the bad value would lock the caller out of every
        # other valid result on the page.
        assert forecast._maybe_int("not-a-number") is None


class TestMicrosToCurrency:
    def test_none_returns_none(self):
        assert forecast._micros_to_currency(None) is None

    def test_zero_returns_zero_not_none(self):
        # Regression: a bid bound of 0 micros must NOT become None — that's
        # the original Bug 1 in the released v0.8.0.
        assert forecast._micros_to_currency(0) == 0.0

    def test_normal_value_rounds_to_two_dp(self):
        assert forecast._micros_to_currency(1_234_567) == 1.23


class TestKeywordIdeasRestBody:
    """Direct coverage of the REST body builder — independent of the network."""

    def test_keyword_seed_shape(self):
        body = forecast._build_keyword_ideas_rest_body(
            language_id="1000",
            geo_target_id="2276",
            page_size=50,
            seed_keywords=["running shoes"],
            url="",
        )
        assert body["language"] == "languageConstants/1000"
        assert body["geoTargetConstants"] == ["geoTargetConstants/2276"]
        assert body["keywordPlanNetwork"] == "GOOGLE_SEARCH"
        assert body["pageSize"] == 50
        assert body["keywordSeed"] == {"keywords": ["running shoes"]}
        assert "urlSeed" not in body
        assert "keywordAndUrlSeed" not in body
        assert "pageToken" not in body

    def test_url_seed_shape(self):
        body = forecast._build_keyword_ideas_rest_body(
            language_id="1001",
            geo_target_id="2276",
            page_size=10,
            seed_keywords=[],
            url="https://example.com",
        )
        assert body["urlSeed"] == {"url": "https://example.com"}
        assert "keywordSeed" not in body

    def test_combined_seed_shape(self):
        body = forecast._build_keyword_ideas_rest_body(
            language_id="1000",
            geo_target_id="2840",
            page_size=25,
            seed_keywords=["a", "b"],
            url="https://example.com",
        )
        assert body["keywordAndUrlSeed"] == {
            "url": "https://example.com",
            "keywords": ["a", "b"],
        }

    def test_page_token_included_when_present(self):
        body = forecast._build_keyword_ideas_rest_body(
            language_id="1000",
            geo_target_id="2276",
            page_size=50,
            seed_keywords=["x"],
            url="",
            page_token="next-page-123",
        )
        assert body["pageToken"] == "next-page-123"


class TestEstimateBudgetZeroPreservation:
    """Regression suite for Bug 2 — ``estimate_budget`` used the same falsy
    pattern as ``discover_keywords`` (``int(v) if v else None`` and
    ``round(x) if x else None``), which silently mapped a legitimate 0-click,
    0-cost, or 0-CTR forecast onto ``None``. The caller couldn't tell "no
    data returned" apart from "Google says zero" — which matters because a
    real zero-cost / zero-click forecast is itself the diagnostic signal
    ("your keywords have impressions but no clicks", "the daily budget can
    cover 100% of estimated cost"). These tests pin the fix end-to-end and
    will fail if anyone re-introduces a falsy guard on these fields.
    """

    def _fake_client(self, metrics_obj):
        """Build a fake Google Ads client whose
        ``KeywordPlanIdeaService.generate_keyword_forecast_metrics`` returns
        the given ``metrics_obj`` as the ``campaign_forecast_metrics``.

        The wrapper apes the bits of the SDK that ``estimate_budget``
        actually touches: ``get_service``, ``get_type``, ``enums``, and the
        path builder methods. Nothing about the proto plumbing is
        exercised — that belongs in google-ads-python's own tests, not ours.
        """
        forecast_metrics_response = SimpleNamespace(
            campaign_forecast_metrics=metrics_obj
        )

        kp_service = SimpleNamespace(
            generate_keyword_forecast_metrics=lambda request=None: (
                forecast_metrics_response
            )
        )
        googleads_service = SimpleNamespace(
            geo_target_constant_path=lambda gid: f"geoTargetConstants/{gid}",
            language_constant_path=lambda lid: f"languageConstants/{lid}",
        )

        def _get_service(name):
            return {
                "KeywordPlanIdeaService": kp_service,
                "GoogleAdsService": googleads_service,
            }[name]

        # Each ``get_type`` call returns a fresh SimpleNamespace-ish object
        # so the function can assign attributes on it freely. We don't
        # validate what gets assigned — the proto wrapping is google-ads-
        # python's job — we just need attribute writes not to explode.
        def _get_type(_name):
            return SimpleNamespace(
                geo_modifiers=[],
                language_constants=[],
                ad_groups=[],
                biddable_keywords=[],
                keyword=SimpleNamespace(),
                bidding_strategy=SimpleNamespace(
                    manual_cpc_bidding_strategy=SimpleNamespace()
                ),
                forecast_period=SimpleNamespace(),
            )

        enums = SimpleNamespace(
            KeywordPlanNetworkEnum=SimpleNamespace(GOOGLE_SEARCH="GOOGLE_SEARCH"),
            KeywordMatchTypeEnum=SimpleNamespace(
                EXACT="EXACT", PHRASE="PHRASE", BROAD="BROAD"
            ),
        )

        return SimpleNamespace(
            get_service=_get_service,
            get_type=_get_type,
            enums=enums,
        )

    def _run(self, config, monkeypatch, metrics_obj):
        monkeypatch.setattr(
            forecast,
            "get_ads_client",
            lambda _cfg: self._fake_client(metrics_obj),
            raising=False,
        )
        # ``get_ads_client`` and ``normalize_customer_id`` are imported
        # inside the function body, so we have to patch them on the source
        # module (the same trick used for ``get_ads_credentials`` in
        # TestRestRateLimitRetry). Patching ``forecast.get_ads_client``
        # would be a no-op.
        import adloop.ads.client as _client_mod

        monkeypatch.setattr(
            _client_mod, "get_ads_client", lambda _cfg: self._fake_client(metrics_obj)
        )
        monkeypatch.setattr(
            _client_mod, "normalize_customer_id", lambda cid: cid.replace("-", "")
        )

        return forecast.estimate_budget(
            config,
            keywords=[{"text": "test kw", "match_type": "EXACT", "max_cpc": 1.0}],
            daily_budget=10.0,
            forecast_days=30,
        )

    def test_zero_clicks_preserved_not_nulled(self, config, monkeypatch):
        # The diagnostic insight "impressions but zero clicks" depends on
        # clicks being a literal 0, not None. If this regresses, the
        # insight silently stops firing.
        metrics = SimpleNamespace(
            clicks=0,
            impressions=1000,
            average_cpc_micros=None,
            cost_micros=0,
            click_through_rate=0.0,
        )
        result = self._run(config, monkeypatch, metrics)
        assert result["estimated_clicks"] == 0
        assert result["estimated_impressions"] == 1000
        assert result["estimated_cost"] == 0.0
        assert result["estimated_ctr"] == 0.0
        # The "zero clicks despite impressions" insight should fire.
        assert any("zero clicks" in i for i in result["insights"])

    def test_zero_cost_preserved_not_nulled(self, config, monkeypatch):
        metrics = SimpleNamespace(
            clicks=10,
            impressions=100,
            average_cpc_micros=0,
            cost_micros=0,
            click_through_rate=0.1,
        )
        result = self._run(config, monkeypatch, metrics)
        assert result["estimated_cost"] == 0.0
        assert result["estimated_avg_cpc"] == 0.0
        # ``daily_estimates.cost`` must round-trip 0.0, not None.
        assert result["daily_estimates"]["cost"] == 0.0

    def test_none_metrics_remain_none(self, config, monkeypatch):
        # Forecast service can legitimately return None for unset optional
        # fields. We must NOT coerce those to 0 (the opposite bug class).
        metrics = SimpleNamespace(
            clicks=None,
            impressions=None,
            average_cpc_micros=None,
            cost_micros=None,
            click_through_rate=None,
        )
        result = self._run(config, monkeypatch, metrics)
        assert result["estimated_clicks"] is None
        assert result["estimated_impressions"] is None
        assert result["estimated_cost"] is None
        assert result["estimated_avg_cpc"] is None
        assert result["estimated_ctr"] is None
        assert result["daily_estimates"]["clicks"] is None
        assert result["daily_estimates"]["impressions"] is None
        assert result["daily_estimates"]["cost"] is None

    def test_realistic_forecast_still_works(self, config, monkeypatch):
        # Sanity check that the fix didn't break the normal happy path —
        # non-zero numbers still flow through and the headline insight
        # still fires.
        metrics = SimpleNamespace(
            clicks=300,
            impressions=10_000,
            average_cpc_micros=500_000,  # 0.50
            cost_micros=150_000_000,     # 150.00
            click_through_rate=0.03,
        )
        result = self._run(config, monkeypatch, metrics)
        assert result["estimated_clicks"] == 300
        assert result["estimated_cost"] == 150.0
        assert result["estimated_avg_cpc"] == 0.5
        assert result["estimated_ctr"] == 0.03
        assert any("Estimated 300 clicks" in i for i in result["insights"])


class TestRestRateLimitRetry:
    """When the REST endpoint returns 429, surface a RESOURCE_EXHAUSTED error
    string so the existing ``call_with_retry`` helper backs off and re-tries.
    """

    def test_429_raises_resource_exhausted_string(self, config, monkeypatch):
        """HTTP 429 from the REST endpoint must be re-raised as a string
        containing 'RESOURCE_EXHAUSTED' so the shared ``call_with_retry``
        helper (which detects rate limits by substring match) recognises
        it and triggers exponential backoff. Without this surface, REST
        429s would propagate as plain ``HTTPError`` and skip the retry.
        """
        import requests as _requests

        fake_response = SimpleNamespace(
            status_code=429,
            text='{"error": {"status": "RESOURCE_EXHAUSTED"}}',
            raise_for_status=lambda: None,
            json=lambda: {},
        )
        fake_session = SimpleNamespace(post=lambda *_a, **_kw: fake_response)

        # Patch the *source* module — the function imports
        # ``get_ads_credentials`` from ``adloop.auth`` inside its body,
        # so patching ``forecast.get_ads_credentials`` (a name that
        # doesn't exist at module scope) would be a no-op and the real
        # OAuth flow would fire on CI where no cached token exists.
        import adloop.auth as _auth_mod
        monkeypatch.setattr(
            _auth_mod, "get_ads_credentials", lambda _config: SimpleNamespace(),
        )
        monkeypatch.setattr(
            "google.auth.transport.requests.AuthorizedSession",
            lambda _creds: fake_session,
        )

        with pytest.raises(_requests.HTTPError, match="RESOURCE_EXHAUSTED"):
            forecast._post_keyword_ideas_rest_page(
                config, "1234567890", {"keywordSeed": {"keywords": ["x"]}}
            )


class TestCallWithRetry:
    def test_returns_result_on_first_success(self):
        fn = MagicMock(return_value="ok")
        assert call_with_retry(fn, "arg", key="val") == "ok"
        fn.assert_called_once_with("arg", key="val")

    def test_non_rate_limit_error_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("unexpected"))
        with pytest.raises(ValueError, match="unexpected"):
            call_with_retry(fn, max_attempts=4)
        fn.assert_called_once()

    def test_retries_on_rate_limit_and_eventually_succeeds(self):
        rate_limit = Exception("RESOURCE_EXHAUSTED: quota exceeded")
        fn = MagicMock(side_effect=[rate_limit, rate_limit, "success"])
        with patch("adloop.ads.client.time.sleep") as mock_sleep:
            result = call_with_retry(fn, max_attempts=4, base_delay=1.0)
        assert result == "success"
        assert fn.call_count == 3
        assert mock_sleep.call_count == 2

    def test_raises_after_max_attempts_exhausted(self):
        rate_limit = Exception("RESOURCE_EXHAUSTED: quota exceeded")
        fn = MagicMock(side_effect=rate_limit)
        with patch("adloop.ads.client.time.sleep"):
            with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
                call_with_retry(fn, max_attempts=3, base_delay=0.01)
        assert fn.call_count == 3

    def test_backoff_delay_grows_exponentially(self):
        rate_limit = Exception("RATE_LIMIT: Too Many Requests")
        fn = MagicMock(side_effect=[rate_limit, rate_limit, "ok"])
        sleep_calls = []
        with patch("adloop.ads.client.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            with patch("adloop.ads.client.random.uniform", return_value=0.0):
                call_with_retry(fn, max_attempts=4, base_delay=1.0)
        assert sleep_calls[0] == pytest.approx(1.0)   # 1.0 * 2^0
        assert sleep_calls[1] == pytest.approx(2.0)   # 1.0 * 2^1


class TestIsRateLimitError:
    @pytest.mark.parametrize("msg", [
        "RESOURCE_EXHAUSTED: quota exceeded",
        "RATE_LIMIT_EXCEEDED",
        "QUOTA_EXCEEDED for the day",
    ])
    def test_detects_rate_limit_messages(self, msg):
        assert _is_rate_limit_error(Exception(msg))

    def test_ignores_unrelated_errors(self):
        assert not _is_rate_limit_error(ValueError("some other error"))
        assert not _is_rate_limit_error(Exception("INTERNAL: server error"))

    def test_bare_429_not_matched_to_avoid_false_positives(self):
        assert not _is_rate_limit_error(Exception("Error on entity 429"))
