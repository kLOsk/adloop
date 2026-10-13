"""Standard Shopping campaigns: draft → preview → apply.

The create path follows the flow validated end to end against a live
account in kLOsk/adloop#68 (thanks to @felipebarbosavasconcelos13-coder),
sent as one atomic GoogleAdsService.Mutate:

1. CampaignBudget
2. Campaign: SHOPPING channel, ``shopping_setting.merchant_id`` and the
   required ``campaign_priority``, created PAUSED
3. AdGroup of type SHOPPING_PRODUCT_ADS
4. AdGroupAd with an empty ``ShoppingProductAdInfo`` (without it the
   campaign reports SERVING and gets no impressions)
5. Listing groups: a root SUBDIVISION plus its "others" UNIT, which needs an
   explicit empty case value and a bid
6. Location criteria only: Shopping refuses language criteria, the feed's
   language decides

Temp ids are unique across the whole request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Bid strategies Standard Shopping accepts. Maximize clicks is TARGET_SPEND in
# the API, as in draft_campaign.
SHOPPING_BIDDING_STRATEGIES = {"MANUAL_CPC", "TARGET_SPEND", "TARGET_ROAS"}
CAMPAIGN_PRIORITIES = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

_BUDGET, _CAMPAIGN, _AD_GROUP, _ROOT, _OTHERS = "-1", "-2", "-3", "-4", "-5"


def linked_merchant_ids(config: AdLoopConfig, customer_id: str) -> set[str]:
    """Merchant Center accounts linked to the Ads account."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(
        config,
        customer_id,
        "SELECT product_link.merchant_center.merchant_center_id, product_link.type "
        "FROM product_link WHERE product_link.type = 'MERCHANT_CENTER'",
    )
    return {
        str(row.get("product_link.merchant_center.merchant_center_id"))
        for row in rows
        if row.get("product_link.merchant_center.merchant_center_id")
    }


def draft_shopping_campaign(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_name: str = "",
    daily_budget: float = 0,
    merchant_id: str = "",
    bidding_strategy: str = "MANUAL_CPC",
    product_bid: float = 0,
    target_roas: float = 0,
    max_cpc: float = 0,
    geo_target_ids: list[str] | None = None,
    campaign_priority: str = "LOW",
    feed_label: str = "",
    ad_group_name: str = "",
    search_partners_enabled: bool = False,
) -> dict:
    """Draft a Standard Shopping campaign (created PAUSED) — returns a preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation, check_budget_cap
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_shopping_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    campaign_name = (campaign_name or "").strip()
    if not campaign_name:
        errors.append("campaign_name is required")
    if not daily_budget or daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")
    merchant_id = str(merchant_id or "").strip()
    if not merchant_id.isdigit():
        errors.append("merchant_id is required: the numeric Merchant Center id (see list_merchant_accounts)")
    bs = (bidding_strategy or "").strip().upper()
    if bs not in SHOPPING_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(SHOPPING_BIDDING_STRATEGIES)} for Standard Shopping "
            "(TARGET_SPEND is Maximize clicks)"
        )
    if bs == "TARGET_ROAS" and not target_roas:
        errors.append("target_roas is required for TARGET_ROAS, e.g. 4.0 for 400%")
    if not product_bid or product_bid <= 0:
        errors.append(
            "product_bid is required: the CPC bid of the 'All products' group in account currency. "
            "Google needs it on the product group even when the strategy sets bids itself"
        )
    geo_target_ids = [str(g).strip() for g in (geo_target_ids or []) if str(g).strip()]
    if not geo_target_ids:
        errors.append("geo_target_ids is required: Shopping campaigns must target at least one country")
    bad_geo = [g for g in geo_target_ids if not g.isdigit()]
    if bad_geo:
        errors.append(f"geo_target_ids must be numeric geo target constant ids, got {bad_geo}")
    priority = (campaign_priority or "LOW").strip().upper()
    if priority not in CAMPAIGN_PRIORITIES:
        errors.append(f"campaign_priority must be one of {sorted(CAMPAIGN_PRIORITIES)}")
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        check_budget_cap(daily_budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    try:
        linked = linked_merchant_ids(config, customer_id)
    except Exception as exc:  # noqa: BLE001 — the dry run asks Google again
        linked = None
        warnings.append(f"Could not check the Merchant Center link ({exc}); the dry run will.")
    if linked is not None and merchant_id not in linked:
        return {
            "error": "Validation failed",
            "details": [
                f"Merchant Center {merchant_id} is not linked to this Google Ads account"
                + (f" (linked: {', '.join(sorted(linked))})" if linked else " (no account is linked)")
                + ". Link it in Merchant Center under Settings → Apps and services, then draft again."
            ],
        }

    warnings.append(
        "Products only serve once Merchant Center has approved them; check "
        "get_merchant_feed_health before enabling the campaign."
    )
    warnings.append(
        "No language targeting: Shopping campaigns serve in the feed's language, Google refuses "
        "language criteria here."
    )
    if bs == "MANUAL_CPC":
        warnings.append(
            f"Every product starts at the same bid ({product_bid}). Split product groups by brand or "
            "custom label later to bid on what sells."
        )

    plan = ChangePlan(
        operation="create_shopping_campaign",
        entity_type="campaign",
        customer_id=customer_id,
        changes={
            "campaign_name": campaign_name,
            "daily_budget": daily_budget,
            "merchant_id": merchant_id,
            "feed_label": (feed_label or "").strip() or None,
            "campaign_priority": priority,
            "bidding_strategy": bs,
            "target_roas": target_roas or None,
            "max_cpc": max_cpc or None,
            "product_bid": product_bid,
            "ad_group_name": (ad_group_name or "").strip() or f"{campaign_name} - All products",
            "geo_target_ids": geo_target_ids,
            "search_partners_enabled": bool(search_partners_enabled),
            "status_on_create": "PAUSED",
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def build_shopping_operations(client: object, cid: str, changes: dict) -> list:
    """The atomic mutate for a Standard Shopping campaign (see module doc)."""
    service = client.get_service("GoogleAdsService")
    budget_path = service.campaign_budget_path(cid, _BUDGET)
    campaign_path = service.campaign_path(cid, _CAMPAIGN)
    ad_group_path = service.ad_group_path(cid, _AD_GROUP)
    root_path = service.ad_group_criterion_path(cid, _AD_GROUP, _ROOT)
    operations = []

    op = client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.create
    budget.resource_name = budget_path
    budget.name = f"Budget - {changes['campaign_name']}"
    budget.amount_micros = int(changes["daily_budget"] * 1_000_000)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(op)

    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.create
    campaign.resource_name = campaign_path
    campaign.name = changes["campaign_name"]
    campaign.campaign_budget = budget_path
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SHOPPING
    campaign.shopping_setting.merchant_id = int(changes["merchant_id"])
    campaign.shopping_setting.campaign_priority = CAMPAIGN_PRIORITIES[changes["campaign_priority"]]
    if changes.get("feed_label"):
        campaign.shopping_setting.feed_label = changes["feed_label"]
    bs = changes["bidding_strategy"]
    if bs == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False
    elif bs == "TARGET_SPEND":
        campaign.target_spend.target_spend_micros = 0
        if changes.get("max_cpc"):
            campaign.target_spend.cpc_bid_ceiling_micros = int(changes["max_cpc"] * 1_000_000)
    elif bs == "TARGET_ROAS":
        campaign.target_roas.target_roas = float(changes["target_roas"])
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = bool(changes.get("search_partners_enabled"))
    campaign.network_settings.target_content_network = False
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    operations.append(op)

    op = client.get_type("MutateOperation")
    ad_group = op.ad_group_operation.create
    ad_group.resource_name = ad_group_path
    ad_group.name = changes["ad_group_name"]
    ad_group.campaign = campaign_path
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS
    operations.append(op)

    op = client.get_type("MutateOperation")
    ad_group_ad = op.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_path
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    client.copy_from(ad_group_ad.ad.shopping_product_ad, client.get_type("ShoppingProductAdInfo"))
    operations.append(op)

    op = client.get_type("MutateOperation")
    root = op.ad_group_criterion_operation.create
    root.resource_name = root_path
    root.ad_group = ad_group_path
    root.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    root.listing_group.type_ = client.enums.ListingGroupTypeEnum.SUBDIVISION
    operations.append(op)

    op = client.get_type("MutateOperation")
    others = op.ad_group_criterion_operation.create
    others.resource_name = service.ad_group_criterion_path(cid, _AD_GROUP, _OTHERS)
    others.ad_group = ad_group_path
    others.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    others.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    others.listing_group.parent_ad_group_criterion = root_path
    # An empty condition dimension is the "everything else" case.
    client.copy_from(others.listing_group.case_value.product_condition, client.get_type("ProductConditionInfo"))
    others.cpc_bid_micros = int(changes["product_bid"] * 1_000_000)
    operations.append(op)

    for geo_id in changes["geo_target_ids"]:
        op = client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = campaign_path
        criterion.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        operations.append(op)

    return operations


def apply_create_shopping_campaign(client: object, cid: str, changes: dict) -> dict:
    from adloop.ads.write import _extract_resource_name

    service = client.get_service("GoogleAdsService")
    response = service.mutate(customer_id=cid, mutate_operations=build_shopping_operations(client, cid, changes))
    names = [_extract_resource_name(r) for r in response.mutate_operation_responses]
    names += [None] * (6 - len(names))
    return {
        "campaign_budget": names[0],
        "campaign": names[1],
        "ad_group": names[2],
        "ad": names[3],
        "listing_groups": [n for n in names[4:6] if n],
        "geo_targets": [n for n in names[6:] if n],
        "status": "PAUSED",
        "next_steps": (
            "Check get_merchant_feed_health for disapproved products, then enable the campaign "
            "with enable_entity(entity_type='campaign')."
        ),
    }
