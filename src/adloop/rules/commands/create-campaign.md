---
description: Create a new Google Ads search campaign with safety checks
allowed-tools: ["mcp"]
---

Create a new Google Ads campaign for: $ARGUMENTS

## 1. Research existing structure

- `get_campaign_performance` — understand existing campaigns, avoid duplicate names
- Check what's already running and what bidding strategies are in use

## 2. Budget estimation

- If user hasn't specified a budget, call `estimate_budget` with proposed keywords
- Ask for target geography and language if not clear (common: 2276=Germany, 2840=USA, 2826=UK)
- Present the forecast: estimated clicks, impressions, cost, avg CPC

## 3. Pre-write checks (CRITICAL)

- Bidding strategy: recommend MAXIMIZE_CONVERSIONS or TARGET_CPA over MANUAL_CPC
- Conversion tracking: run `attribution_check` — if zero conversions across the board, WARN that a new campaign won't help until tracking is fixed
- Budget: must be <= max_daily_budget in config, ideally >= 5x target CPA
- Keywords: if using BROAD match, campaign MUST use Smart Bidding — otherwise use PHRASE or EXACT

## 4. Draft campaign

- Call `draft_campaign` with:
  - campaign_name, daily_budget, bidding_strategy
  - ad_group_name
  - Optional keywords with appropriate match types
- Review the preview and any warnings (budget sufficiency, bidding, match type safety)

## 5. Present and confirm

- Show the complete preview to the user
- Emphasize: campaign will be created as PAUSED
- Wait for explicit approval
- `confirm_and_apply(plan_id=..., dry_run=true)` first
- Only `dry_run=false` after user confirms

## 6. Next steps

After campaign creation, remind the user to:
1. Add ads via `draft_responsive_search_ad` (use /create-ad)
2. Enable the campaign via `enable_entity` when ready
