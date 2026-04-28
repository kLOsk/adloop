---
description: Create a responsive search ad with pre-write validation and safety checks
allowed-tools: ["mcp"]
---

Create a Google Ads responsive search ad for: $ARGUMENTS

## 1. Research

- `get_campaign_performance` — find campaign structure and campaign.id
- `run_gaql` — get ad group IDs: `SELECT ad_group.id, ad_group.name FROM ad_group WHERE campaign.id = {id}`
- `get_tracking_events` — verify conversion tracking exists

## 2. Pre-write validation (CRITICAL)

Before drafting anything, check:
- Is the bidding strategy appropriate? MANUAL_CPC = warn user, adding ads won't help before fixing bidding
- Does the campaign have conversions? High spend + zero conversions = warn, adding ads won't help
- Quality scores? All below 5 = relevance problem, not ad problem
- If systemic issues found, warn user before proceeding

## 3. Landing page analysis

- Read the landing page code to extract value propositions
- Determine the correct language — if unclear, ASK the user before writing any copy

## 4. Write ad copy

- 8-10 diverse headlines (MAX 30 characters each — count every one!)
- 3-4 descriptions (MAX 90 characters each)
- Count characters BEFORE calling draft_responsive_search_ad
- Aim for 25 chars on headlines to leave margin
- Write in the landing page's language
- Make headlines diverse — don't repeat the same message

## 5. Draft and confirm

- Call `draft_responsive_search_ad` with the copy
- Show full preview to user including any warnings
- Wait for explicit approval
- `confirm_and_apply(plan_id=..., dry_run=true)` first
- Only `dry_run=false` after user explicitly confirms
