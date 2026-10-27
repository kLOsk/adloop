"""One read for the settings that decide how far a Search campaign reaches.

A recurring audit ("has Google switched anything back on?") otherwise needs
``get_ai_max_settings`` plus hand-written GAQL for networks, location option,
campaign-level broad match and URL options. Every field below was checked
against the v25 ``campaign`` resource:

    campaign.network_settings.target_google_search / target_search_network /
        target_content_network / target_partner_search_network
    campaign.geo_target_type_setting.positive_geo_target_type   PRESENCE_OR_INTEREST,
                                                                SEARCH_INTEREST, PRESENCE
    campaign.geo_target_type_setting.negative_geo_target_type   PRESENCE_OR_INTEREST, PRESENCE
    campaign.keyword_match_type                                 BROAD, or unset
    campaign.tracking_url_template, final_url_suffix, url_custom_parameters
    campaign.bidding_strategy_type and the per-strategy targets
    campaign_budget.amount_micros, explicitly_shared

The AI Max half comes from ``ai_max.read_ai_max_state`` unchanged, so both tools
always agree. Read-only: nothing here mutates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_SETTINGS_SELECT = """
    SELECT campaign.id, campaign.name, campaign.status,
           campaign.advertising_channel_type,
           campaign.bidding_strategy_type, campaign.bidding_strategy,
           campaign.target_spend.cpc_bid_ceiling_micros,
           campaign.maximize_conversions.target_cpa_micros,
           campaign.target_cpa.target_cpa_micros,
           campaign.target_roas.target_roas,
           campaign.maximize_conversion_value.target_roas,
           campaign.network_settings.target_google_search,
           campaign.network_settings.target_search_network,
           campaign.network_settings.target_content_network,
           campaign.geo_target_type_setting.positive_geo_target_type,
           campaign.geo_target_type_setting.negative_geo_target_type,
           campaign.keyword_match_type,
           campaign.tracking_url_template, campaign.final_url_suffix,
           campaign.url_custom_parameters,
           campaign_budget.amount_micros, campaign_budget.explicitly_shared
    FROM campaign
    WHERE {where}
    ORDER BY campaign.name
"""

_SEARCH_ONLY = "campaign.advertising_channel_type = 'SEARCH'"
_WHERE_ALL = f"campaign.status != 'REMOVED' AND {_SEARCH_ONLY}"

# Google's default; it also reaches people elsewhere who search *about* the
# targeted locations.
_WIDE_LOCATION_OPTIONS = {"PRESENCE_OR_INTEREST", "SEARCH_INTEREST"}


def _money(micros: object) -> float | None:
    """Micros to account currency; 0 or missing means "not set"."""
    if not micros:
        return None
    return round(int(micros) / 1_000_000, 2)


def _target_cpa(row: dict) -> float | None:
    return _money(
        row.get("campaign.target_cpa.target_cpa_micros")
        or row.get("campaign.maximize_conversions.target_cpa_micros")
    )


def _target_roas(row: dict) -> float | None:
    value = row.get("campaign.target_roas.target_roas") or row.get(
        "campaign.maximize_conversion_value.target_roas"
    )
    return value or None


def _custom_parameters(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [
        {"key": item.get("key", ""), "value": item.get("value", "")}
        for item in value
        if isinstance(item, dict)
    ]


def _widening(campaign: dict) -> list[str]:
    """The settings on this campaign that let ads reach beyond what was chosen."""
    found: list[str] = []
    if campaign["search_partners"]:
        found.append("search partners on")
    if campaign["display_network"]:
        found.append("Display network on")
    if campaign["location_option"] in _WIDE_LOCATION_OPTIONS:
        found.append(f"location option {campaign['location_option']}")
    if campaign["keyword_match_type"] == "BROAD":
        found.append("campaign-level broad match on")
    if campaign["enable_ai_max"]:
        found.append("AI Max on")
    for automation, status in campaign["asset_automation"].items():
        if status == "OPTED_IN":
            found.append(f"{automation} opted in")
    return found


def get_campaign_settings(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Read-only, one flat record per Search campaign. See the module docstring."""
    from adloop.ads.client import get_ads_client, normalize_customer_id
    from adloop.ads.currency import get_currency_code

    campaign_id = str(campaign_id or "").strip()
    if campaign_id and not campaign_id.isdigit():
        return {"error": "campaign_id must be a numeric ID"}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    client = get_ads_client(config)
    result = read_campaign_settings(client, cid, campaign_id=campaign_id)
    result["currency"] = get_currency_code(config, cid)
    return result


def read_campaign_settings(client: object, cid: str, *, campaign_id: str = "") -> dict:
    """Run both reads and merge them per campaign."""
    from adloop.ads.ai_max import _search, read_ai_max_state

    if campaign_id and not str(campaign_id).isdigit():
        # The id goes into the query text; anything else must not reach GAQL.
        raise ValueError("campaign_id must be a numeric ID")
    # An explicit ID stays Search-only too: the AI Max and location settings
    # read here don't apply to Performance Max or Display campaigns.
    where = f"campaign.id = {campaign_id} AND {_SEARCH_ONLY}" if campaign_id else _WHERE_ALL
    rows = _search(
        client.get_service("GoogleAdsService"), cid, _SETTINGS_SELECT.format(where=where)
    )
    ai_max = {
        c["campaign_id"]: c
        for c in read_ai_max_state(client, cid, campaign_id=campaign_id)["campaigns"]
    }

    campaigns: list[dict] = []
    for row in rows:
        key = str(row.get("campaign.id", ""))
        if not key or (campaign_id and key != str(campaign_id)):
            continue
        ai = ai_max.get(key, {})
        campaign = {
            "campaign_id": key,
            "campaign_name": row.get("campaign.name"),
            "status": row.get("campaign.status"),
            "advertising_channel_type": row.get("campaign.advertising_channel_type"),
            "bidding_strategy_type": row.get("campaign.bidding_strategy_type"),
            "portfolio_bidding_strategy": row.get("campaign.bidding_strategy") or None,
            "cpc_bid_ceiling": _money(
                row.get("campaign.target_spend.cpc_bid_ceiling_micros")
            ),
            "target_cpa": _target_cpa(row),
            "target_roas": _target_roas(row),
            "daily_budget": _money(row.get("campaign_budget.amount_micros")),
            "budget_shared": bool(row.get("campaign_budget.explicitly_shared")),
            "google_search": bool(row.get("campaign.network_settings.target_google_search")),
            "search_partners": bool(
                row.get("campaign.network_settings.target_search_network")
            ),
            "display_network": bool(
                row.get("campaign.network_settings.target_content_network")
            ),
            "location_option": row.get(
                "campaign.geo_target_type_setting.positive_geo_target_type"
            ),
            "negative_location_option": row.get(
                "campaign.geo_target_type_setting.negative_geo_target_type"
            ),
            "keyword_match_type": row.get("campaign.keyword_match_type"),
            "tracking_url_template": row.get("campaign.tracking_url_template") or "",
            "final_url_suffix": row.get("campaign.final_url_suffix") or "",
            "url_custom_parameters": _custom_parameters(
                row.get("campaign.url_custom_parameters")
            ),
            "enable_ai_max": bool(ai.get("enable_ai_max")),
            "asset_automation": {
                item["asset_automation_type"]: item.get("asset_automation_status", "")
                for item in ai.get("asset_automation_settings", [])
            },
            # Search term matching only acts under AI Max; listing every ad
            # group's default False otherwise would be noise.
            "ad_groups_with_search_term_matching": [
                g["ad_group_name"]
                for g in ai.get("ad_groups", [])
                if g.get("disable_search_term_matching") is False
            ] if ai.get("enable_ai_max") else [],
        }
        campaign["widening_settings"] = _widening(campaign)
        campaigns.append(campaign)

    insights: list[str] = []
    for campaign in campaigns:
        if campaign["widening_settings"]:
            insights.append(
                f"{campaign['campaign_name']} ({campaign['campaign_id']}): "
                + ", ".join(campaign["widening_settings"])
                + ". These let the ads reach beyond the keywords and locations "
                "chosen; confirm they are intended."
            )
    if campaigns and not insights:
        insights.append("No setting that widens reach is on in any campaign returned.")
    if not campaigns:
        insights.append("No non-removed Search campaign matched.")

    return {
        "campaigns": campaigns,
        "total_campaigns": len(campaigns),
        "insights": insights,
    }
