---
description: Estimate budget for keywords using Google Ads Keyword Planner
allowed-tools: ["mcp"]
---

Plan budget for Google Ads keywords: $ARGUMENTS

## 1. Gather inputs

- Target keywords: from user input or suggest based on business context
- Match types: EXACT or PHRASE preferred (BROAD only with Smart Bidding)
- Max CPC bids: optional, helps refine forecasts
- Geography: ask user or infer from existing account (common: 2276=Germany, 2840=USA, 2826=UK)
- Language: ask user or infer (common: 1000=English, 1001=German, 1002=French)
- Daily budget: optional — if provided, shows whether it's sufficient

## 2. Run forecast

- Call `estimate_budget` with keywords, match types, optional daily budget, geo target, language
- The tool uses Google Ads Keyword Planner API (read-only, creates nothing)

## 3. Present results

- Estimated daily clicks, impressions, cost, and average CPC
- If daily budget was provided: is it sufficient to capture most available traffic?
- Compare estimated CPC across keywords — some may be too expensive
- Highlight keywords with low forecast volume (may not be worth targeting)

## 4. Recommendations

- Suggest a daily budget based on the forecast data
- If budget is tight, recommend focusing on highest-intent keywords with EXACT match
- If budget is ample, PHRASE match captures more volume
- Link to campaign creation: use the forecast to set the budget in `draft_campaign`
