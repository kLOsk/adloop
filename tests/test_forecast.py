"""Tests for Keyword Planner forecast and discovery functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.ads import forecast
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
