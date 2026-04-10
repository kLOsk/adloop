"""Tests for Keyword Planner forecast and discovery functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

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


def _make_idea(text, avg_monthly, competition_value, competition_index, low_bid, high_bid):
    """Build a fake keyword idea proto-like object."""
    metrics = SimpleNamespace(
        avg_monthly_searches=avg_monthly,
        competition=competition_value,
        competition_index=competition_index,
        low_top_of_page_bid_micros=low_bid,
        high_top_of_page_bid_micros=high_bid,
    )
    return SimpleNamespace(text=text, keyword_idea_metrics=metrics)


class TestDiscoverKeywords:
    def test_requires_seed_keywords_or_url(self, config):
        result = forecast.discover_keywords(config)
        assert result["error"] == "Provide at least one of: seed_keywords or url"

    def test_seed_keywords_only_uses_keyword_seed(self, config):
        fake_idea = _make_idea("trail running shoes", 5000, 2, 60, 800_000, 2_000_000)

        with patch("adloop.ads.client.get_ads_client") as mock_client_fn:
            client = MagicMock()
            mock_client_fn.return_value = client

            # configure resource-path helpers
            gs = MagicMock()
            gs.language_constant_path.return_value = "languageConstants/1000"
            gs.geo_target_constant_path.return_value = "geoTargetConstants/2276"
            client.get_service.side_effect = lambda name: gs if name == "GoogleAdsService" else MagicMock(generate_keyword_ideas=MagicMock(return_value=[fake_idea]))

            request_obj = MagicMock()
            client.get_type.return_value = request_obj

            result = forecast.discover_keywords(
                config,
                seed_keywords=["trail running"],
                geo_target_id="2276",
                language_id="1000",
            )

        assert result["total_ideas"] == 1
        assert result["keyword_ideas"][0]["keyword"] == "trail running shoes"
        assert result["keyword_ideas"][0]["competition"] == "MEDIUM"
        assert result["keyword_ideas"][0]["avg_monthly_searches"] == 5000
        assert result["keyword_ideas"][0]["low_top_of_page_bid"] == 0.80
        assert result["keyword_ideas"][0]["high_top_of_page_bid"] == 2.00
        assert result["seed_keywords"] == ["trail running"]
        assert result["seed_url"] == ""

    def test_url_only_uses_url_seed(self, config):
        fake_idea = _make_idea("running gear", 1200, 1, 20, 400_000, 900_000)

        with patch("adloop.ads.client.get_ads_client") as mock_client_fn:
            client = MagicMock()
            mock_client_fn.return_value = client

            gs = MagicMock()
            gs.language_constant_path.return_value = "languageConstants/1000"
            gs.geo_target_constant_path.return_value = "geoTargetConstants/2840"
            client.get_service.side_effect = lambda name: gs if name == "GoogleAdsService" else MagicMock(generate_keyword_ideas=MagicMock(return_value=[fake_idea]))

            request_obj = MagicMock()
            client.get_type.return_value = request_obj

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

    def test_ideas_sorted_by_avg_monthly_searches_descending(self, config):
        ideas = [
            _make_idea("low volume", 100, 1, 10, None, None),
            _make_idea("high volume", 50000, 3, 90, 1_000_000, 3_000_000),
            _make_idea("mid volume", 5000, 2, 50, 500_000, 1_500_000),
        ]

        with patch("adloop.ads.client.get_ads_client") as mock_client_fn:
            client = MagicMock()
            mock_client_fn.return_value = client

            gs = MagicMock()
            gs.language_constant_path.return_value = "languageConstants/1000"
            gs.geo_target_constant_path.return_value = "geoTargetConstants/2276"
            client.get_service.side_effect = lambda name: gs if name == "GoogleAdsService" else MagicMock(generate_keyword_ideas=MagicMock(return_value=ideas))
            client.get_type.return_value = MagicMock()

            result = forecast.discover_keywords(config, seed_keywords=["running"])

        volumes = [i["avg_monthly_searches"] for i in result["keyword_ideas"]]
        assert volumes == sorted(volumes, reverse=True)

    def test_insights_surface_competition_breakdown(self, config):
        ideas = [
            _make_idea("cheap option", 200, 1, 15, None, None),
            _make_idea("popular term", 8000, 3, 85, 2_000_000, 5_000_000),
        ]

        with patch("adloop.ads.client.get_ads_client") as mock_client_fn:
            client = MagicMock()
            mock_client_fn.return_value = client

            gs = MagicMock()
            gs.language_constant_path.return_value = "languageConstants/1000"
            gs.geo_target_constant_path.return_value = "geoTargetConstants/2276"
            client.get_service.side_effect = lambda name: gs if name == "GoogleAdsService" else MagicMock(generate_keyword_ideas=MagicMock(return_value=ideas))
            client.get_type.return_value = MagicMock()

            result = forecast.discover_keywords(config, seed_keywords=["option"])

        assert any("high-competition" in i for i in result["insights"])
        assert any("low-competition" in i for i in result["insights"])

    def test_page_size_capped_at_1000(self, config):
        with patch("adloop.ads.client.get_ads_client") as mock_client_fn:
            client = MagicMock()
            mock_client_fn.return_value = client

            gs = MagicMock()
            gs.language_constant_path.return_value = "languageConstants/1000"
            gs.geo_target_constant_path.return_value = "geoTargetConstants/2276"
            kp = MagicMock(generate_keyword_ideas=MagicMock(return_value=[]))
            client.get_service.side_effect = lambda name: gs if name == "GoogleAdsService" else kp

            request_obj = MagicMock()
            client.get_type.return_value = request_obj

            forecast.discover_keywords(config, seed_keywords=["test"], page_size=9999)

        assert request_obj.page_size == 1000

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
        rate_limit = Exception("429 Too Many Requests")
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
        "status 429 Too Many Requests",
        "RATE_LIMIT_EXCEEDED",
        "QUOTA_EXCEEDED for the day",
    ])
    def test_detects_rate_limit_messages(self, msg):
        assert _is_rate_limit_error(Exception(msg))

    def test_ignores_unrelated_errors(self):
        assert not _is_rate_limit_error(ValueError("some other error"))
        assert not _is_rate_limit_error(Exception("INTERNAL: server error"))
