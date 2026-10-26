"""Tests for get_campaign_settings (one read for every setting that widens reach)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adloop.ads import ai_max, campaign_settings
from adloop.config import AdLoopConfig, AdsConfig

CUSTOMER = "1234567890"
NARROW = 21001
WIDE = 21002


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id=CUSTOMER))


def _settings_row(campaign_id=NARROW, *, name="Search — narrow", **overrides):
    """A campaign the way an exact/phrase, presence-only build leaves it."""
    row = {
        "campaign.id": campaign_id,
        "campaign.name": name,
        "campaign.status": "PAUSED",
        "campaign.advertising_channel_type": "SEARCH",
        "campaign.bidding_strategy_type": "TARGET_SPEND",
        "campaign.bidding_strategy": "",
        "campaign.target_spend.cpc_bid_ceiling_micros": 15_000_000,
        "campaign.maximize_conversions.target_cpa_micros": 0,
        "campaign.target_cpa.target_cpa_micros": 0,
        "campaign.target_roas.target_roas": 0,
        "campaign.maximize_conversion_value.target_roas": 0,
        "campaign.network_settings.target_google_search": True,
        "campaign.network_settings.target_search_network": False,
        "campaign.network_settings.target_content_network": False,
        "campaign.network_settings.target_partner_search_network": False,
        "campaign.geo_target_type_setting.positive_geo_target_type": "PRESENCE",
        "campaign.geo_target_type_setting.negative_geo_target_type": "PRESENCE",
        "campaign.keyword_match_type": "UNSPECIFIED",
        "campaign.tracking_url_template": "{lpurl}?utm_source=google",
        "campaign.final_url_suffix": "",
        "campaign.url_custom_parameters": [{"key": "grp", "value": "b2c"}],
        "campaign_budget.amount_micros": 15_000_000,
        "campaign_budget.explicitly_shared": False,
    }
    row.update(overrides)
    return row


def _ai_campaign_row(campaign_id=NARROW, *, enable=False, automation=("OPTED_OUT", "OPTED_OUT")):
    text, url = automation
    return {
        "campaign.id": campaign_id,
        "campaign.name": "x",
        "campaign.status": "PAUSED",
        "campaign.advertising_channel_type": "SEARCH",
        "campaign.ai_max_setting.enable_ai_max": enable,
        "campaign.ai_max_setting.bundling_required": "UNSPECIFIED",
        "campaign.asset_automation_settings": [
            {"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": text},
            {
                "asset_automation_type": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
                "asset_automation_status": url,
            },
        ],
    }


def _ad_group_row(campaign_id, group_id, *, name, disable=False):
    return {
        "campaign.id": campaign_id,
        "ad_group.id": group_id,
        "ad_group.name": name,
        "ad_group.status": "ENABLED",
        "ad_group.ai_max_ad_group_setting.disable_search_term_matching": disable,
    }


def _patch(monkeypatch, settings, ai_campaigns, ad_groups=()):
    queries: list[str] = []

    def fake_search(_service, _cid, query):
        queries.append(query)
        if "geo_target_type_setting" in query:
            return list(settings)
        if "FROM campaign" in query:
            return list(ai_campaigns)
        return list(ad_groups)

    monkeypatch.setattr(ai_max, "_search", fake_search)
    return queries


def _client():
    return SimpleNamespace(get_service=lambda _name: SimpleNamespace(search=lambda **_kw: []))


class TestRead:
    def test_flat_record_per_campaign(self, monkeypatch):
        _patch(monkeypatch, [_settings_row()], [_ai_campaign_row()])

        result = campaign_settings.read_campaign_settings(_client(), CUSTOMER)

        (campaign,) = result["campaigns"]
        assert campaign["campaign_id"] == str(NARROW)
        assert campaign["cpc_bid_ceiling"] == 15.0
        assert campaign["daily_budget"] == 15.0
        assert campaign["target_cpa"] is None and campaign["target_roas"] is None
        assert campaign["portfolio_bidding_strategy"] is None
        assert campaign["google_search"] is True
        assert campaign["search_partners"] is False
        assert campaign["display_network"] is False
        assert campaign["location_option"] == "PRESENCE"
        assert campaign["keyword_match_type"] == "UNSPECIFIED"
        assert campaign["tracking_url_template"] == "{lpurl}?utm_source=google"
        assert campaign["url_custom_parameters"] == [{"key": "grp", "value": "b2c"}]
        assert campaign["enable_ai_max"] is False
        assert campaign["asset_automation"] == {
            "TEXT_ASSET_AUTOMATION": "OPTED_OUT",
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "OPTED_OUT",
        }
        assert campaign["widening_settings"] == []
        assert result["insights"] == [
            "No setting that widens reach is on in any campaign returned."
        ]

    def test_every_widening_setting_is_named(self, monkeypatch):
        _patch(
            monkeypatch,
            [
                _settings_row(
                    WIDE,
                    name="Search — wide",
                    **{
                        "campaign.network_settings.target_search_network": True,
                        "campaign.network_settings.target_content_network": True,
                        "campaign.geo_target_type_setting.positive_geo_target_type": (
                            "PRESENCE_OR_INTEREST"
                        ),
                        "campaign.keyword_match_type": "BROAD",
                    },
                )
            ],
            [_ai_campaign_row(WIDE, enable=True, automation=("OPTED_IN", "OPTED_OUT"))],
            [
                _ad_group_row(WIDE, 1, name="Matching"),
                _ad_group_row(WIDE, 2, name="Locked", disable=True),
            ],
        )

        result = campaign_settings.read_campaign_settings(_client(), CUSTOMER)

        (campaign,) = result["campaigns"]
        assert campaign["widening_settings"] == [
            "search partners on",
            "Display network on",
            "location option PRESENCE_OR_INTEREST",
            "campaign-level broad match on",
            "AI Max on",
            "TEXT_ASSET_AUTOMATION opted in",
        ]
        assert campaign["ad_groups_with_search_term_matching"] == ["Matching"]
        assert len(result["insights"]) == 1
        assert result["insights"][0].startswith(f"Search — wide ({WIDE}): search partners on")

    def test_search_interest_counts_as_widening_too(self, monkeypatch):
        _patch(
            monkeypatch,
            [_settings_row(**{
                "campaign.geo_target_type_setting.positive_geo_target_type": "SEARCH_INTEREST"
            })],
            [_ai_campaign_row()],
        )

        (campaign,) = campaign_settings.read_campaign_settings(_client(), CUSTOMER)["campaigns"]

        assert campaign["widening_settings"] == ["location option SEARCH_INTEREST"]

    def test_search_term_matching_is_not_listed_without_ai_max(self, monkeypatch):
        """Every ad group defaults to False; it only acts under AI Max."""
        _patch(
            monkeypatch,
            [_settings_row()],
            [_ai_campaign_row()],
            [_ad_group_row(NARROW, 1, name="Alternative")],
        )

        (campaign,) = campaign_settings.read_campaign_settings(_client(), CUSTOMER)["campaigns"]

        assert campaign["ad_groups_with_search_term_matching"] == []
        assert campaign["widening_settings"] == []

    def test_targets_and_portfolio_are_reported(self, monkeypatch):
        _patch(
            monkeypatch,
            [_settings_row(**{
                "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
                "campaign.bidding_strategy": f"customers/{CUSTOMER}/biddingStrategies/9",
                "campaign.target_spend.cpc_bid_ceiling_micros": 0,
                "campaign.maximize_conversions.target_cpa_micros": 85_500_000,
                "campaign_budget.explicitly_shared": True,
            })],
            [_ai_campaign_row()],
        )

        (campaign,) = campaign_settings.read_campaign_settings(_client(), CUSTOMER)["campaigns"]

        assert campaign["target_cpa"] == 85.5
        assert campaign["cpc_bid_ceiling"] is None
        assert campaign["portfolio_bidding_strategy"].endswith("/biddingStrategies/9")
        assert campaign["budget_shared"] is True

    def test_one_campaign_filters_both_queries_and_drops_strays(self, monkeypatch):
        queries = _patch(
            monkeypatch,
            [_settings_row(), _settings_row(WIDE, name="stray")],
            [_ai_campaign_row()],
        )

        result = campaign_settings.read_campaign_settings(
            _client(), CUSTOMER, campaign_id=str(NARROW)
        )

        assert [c["campaign_id"] for c in result["campaigns"]] == [str(NARROW)]
        assert f"campaign.id = {NARROW}" in queries[0]

    def test_no_campaigns(self, monkeypatch):
        _patch(monkeypatch, [], [])

        result = campaign_settings.read_campaign_settings(_client(), CUSTOMER)

        assert result["total_campaigns"] == 0
        assert result["insights"] == ["No non-removed Search campaign matched."]

    def test_the_query_only_selects_fields_that_exist_on_v25(self):
        """Every field must be a real campaign or campaign_budget field in v25,
        checked against the SDK's own proto rather than a hand-kept list."""
        from google.ads.googleads.v25.resources.types.campaign import Campaign
        from google.ads.googleads.v25.resources.types.campaign_budget import CampaignBudget

        from adloop.ads.gaql import _parse_select_fields

        roots = {"campaign": Campaign, "campaign_budget": CampaignBudget}
        for field in _parse_select_fields(campaign_settings._SETTINGS_SELECT):
            resource, *path = field.split(".")
            message = roots[resource]
            for part in path:
                assert part in message.meta.fields, field
                inner = message.meta.fields[part].message
                if inner is not None and part != path[-1]:
                    message = inner


class TestTool:
    def test_a_non_numeric_campaign_id_never_reaches_gaql(self, config, monkeypatch):
        def _no_client(*_args, **_kwargs):
            raise AssertionError("no client may be built for invalid input")

        monkeypatch.setattr("adloop.ads.client.get_ads_client", _no_client)

        result = campaign_settings.get_campaign_settings(
            config, campaign_id="1 OR campaign.id > 0"
        )

        assert result == {"error": "campaign_id must be a numeric ID"}

    def test_read_rejects_a_non_numeric_campaign_id(self):
        with pytest.raises(ValueError, match="numeric"):
            campaign_settings.read_campaign_settings(_client(), CUSTOMER, campaign_id="abc")

    def test_currency_is_attached(self, config, monkeypatch):
        _patch(monkeypatch, [_settings_row()], [_ai_campaign_row()])
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: _client())
        monkeypatch.setattr("adloop.ads.currency.get_currency_code", lambda _c, _cid: "USD")

        result = campaign_settings.get_campaign_settings(config)

        assert result["currency"] == "USD"
        assert result["total_campaigns"] == 1

    @pytest.mark.asyncio
    async def test_registered_read_only_with_documented_parameters(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        tool = tools["get_campaign_settings"]
        assert tool.annotations.read_only_hint is True
        for param in ("campaign_id", "customer_id"):
            assert tool.parameters["properties"][param].get("description"), param
