---
description: Analyze Google Ads + GA4 performance with cross-channel insights
allowed-tools: ["mcp"]
---

Analyze Google Ads and GA4 performance: $ARGUMENTS

## 1. Pull data (AdLoop MCP)

- `get_campaign_performance` — relevant date range (default: last 30 days)
- `analyze_campaign_conversions` — cross-referenced Ads + GA4 data with GDPR gap detection
- If specific campaigns mentioned, filter by name
- If keywords are relevant, also pull `get_keyword_performance` and `get_search_terms`

## 2. Analyze

- Spend, Clicks, Conversions, CPA, CTR per campaign
- Paid vs organic comparison (from non_paid_channels)
- GDPR gap (clicks vs sessions ratio — 2:1 to 5:1 is normal in EU)
- Flag: zero conversions with significant spend, CPA > 3x target, QS < 5, wasteful search terms

If conversion issues found: run `attribution_check`
If landing page problems suspected: run `landing_page_analysis`

## 3. Present results

- Summary table of all campaigns with key metrics
- Highlight what's working and what's not
- Ranked list of recommended actions with priority and estimated impact
- If search terms show waste, quantify the amount and suggest negatives

Keep the GDPR consent gap in mind — never diagnose clicks > sessions as broken tracking without considering consent rejection first.
