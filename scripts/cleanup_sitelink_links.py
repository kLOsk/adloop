"""Bulk removal of legacy campaign-level sitelink/promotion links AND empty
customer-level sitelink placeholders, after promoting clean copies to customer
scope.

Phases 1-3 of the cleanup ran via AdLoop tools (link_asset_to_customer and
draft_sitelinks). This handles phases 4-5 (the bulk removes) in two API calls
instead of 31 separate AdLoop plans.

Customer assets we just added (DO NOT remove):
    13 sitelinks: 307349941499, 307350369410, 307350415811, 307354298834,
                  307443066358, 307447164526, 308700028867, 310581372095,
                  310691883138, 314519875551, 314519875554, 314519875557,
                  314519875560
    7 new sitelinks: 357980743926, 357980743929, 357980743932, 357980743935,
                     357980743938, 357980743941, 357980743944
    1 promotion: 310667906146
"""
from __future__ import annotations

from adloop.ads.client import get_ads_client
from adloop.config import load_config

CUSTOMER_ID = "8860868272"

# (campaign_id, asset_id, field_type)
CAMPAIGN_ASSET_REMOVALS = [
    # NE — 7 sitelinks
    ("23504229772", "307350369410", "SITELINK"),
    ("23504229772", "325613094542", "SITELINK"),
    ("23504229772", "325613094545", "SITELINK"),
    ("23504229772", "325613094548", "SITELINK"),
    ("23504229772", "325613094551", "SITELINK"),
    ("23504229772", "325613094554", "SITELINK"),
    ("23504229772", "325613094557", "SITELINK"),
    # SE — 10 sitelinks
    ("23504046862", "307350369410", "SITELINK"),
    ("23504046862", "325601697149", "SITELINK"),
    ("23504046862", "325601697152", "SITELINK"),
    ("23504046862", "325601697155", "SITELINK"),
    ("23504046862", "325613094542", "SITELINK"),
    ("23504046862", "325613094545", "SITELINK"),
    ("23504046862", "325613094548", "SITELINK"),
    ("23504046862", "325613094551", "SITELINK"),
    ("23504046862", "325613094554", "SITELINK"),
    ("23504046862", "325613094557", "SITELINK"),
    # Shingle Springs — 6 sitelinks
    ("23246712952", "307354298834", "SITELINK"),
    ("23246712952", "307447164526", "SITELINK"),
    ("23246712952", "314519875551", "SITELINK"),
    ("23246712952", "314519875554", "SITELINK"),
    ("23246712952", "314519875557", "SITELINK"),
    ("23246712952", "314519875560", "SITELINK"),
    # PMax — 1 sitelink (empty placeholder)
    ("23803368702", "355786191833", "SITELINK"),
    # Promotion — 3 campaign links (NE/SE/SS) for asset 310667906146
    ("23504229772", "310667906146", "PROMOTION"),
    ("23504046862", "310667906146", "PROMOTION"),
    ("23246712952", "310667906146", "PROMOTION"),
]

# (asset_id, field_type) — the 4 empty customer-level sitelink placeholders
CUSTOMER_ASSET_REMOVALS = [
    ("313519233179", "SITELINK"),  # Paint Scratch Repair (empty)
    ("313519233182", "SITELINK"),  # Auto Body Repair (empty)
    ("313519233185", "SITELINK"),  # Bumper Repair (empty)
    ("313519233188", "SITELINK"),  # Our Services (empty)
]


def main() -> None:
    config = load_config()
    client = get_ads_client(config)

    ca_service = client.get_service("CampaignAssetService")
    cust_asset_service = client.get_service("CustomerAssetService")

    # ----- Phase 4: campaign_asset removals -----
    operations = []
    for campaign_id, asset_id, field_type in CAMPAIGN_ASSET_REMOVALS:
        op = client.get_type("CampaignAssetOperation")
        op.remove = (
            f"customers/{CUSTOMER_ID}/campaignAssets/"
            f"{campaign_id}~{asset_id}~{field_type}"
        )
        operations.append(op)

    print(f"Removing {len(operations)} campaign_asset links ...")
    request = client.get_type("MutateCampaignAssetsRequest")
    request.customer_id = CUSTOMER_ID
    request.operations.extend(operations)
    request.partial_failure = True
    response = ca_service.mutate_campaign_assets(request=request)
    success = sum(1 for r in response.results if r.resource_name)
    print(f"  campaign_asset: {success} removed")
    if response.partial_failure_error.message:
        print(f"  partial failure: {response.partial_failure_error.message}")

    # ----- Phase 5: customer_asset placeholder removals -----
    operations = []
    for asset_id, field_type in CUSTOMER_ASSET_REMOVALS:
        op = client.get_type("CustomerAssetOperation")
        op.remove = (
            f"customers/{CUSTOMER_ID}/customerAssets/{asset_id}~{field_type}"
        )
        operations.append(op)

    print(f"\nRemoving {len(operations)} empty customer_asset placeholders ...")
    request = client.get_type("MutateCustomerAssetsRequest")
    request.customer_id = CUSTOMER_ID
    request.operations.extend(operations)
    request.partial_failure = True
    response = cust_asset_service.mutate_customer_assets(request=request)
    success = sum(1 for r in response.results if r.resource_name)
    print(f"  customer_asset: {success} removed")
    if response.partial_failure_error.message:
        print(f"  partial failure: {response.partial_failure_error.message}")


if __name__ == "__main__":
    main()
