---
name: adloop-planning
description: AdLoop keyword research and budget forecasting. Use this skill whenever the user wants to research keywords, estimate campaign budgets, plan keyword strategy, discover new keyword ideas, forecast ad performance, or asks "how much should I spend", "what keywords should I target", "budget planning", "keyword ideas", "search volume", "competition level", or any question about keyword research and budget estimation for Google Ads campaigns. This skill covers the Google Ads Keyword Planner tools (discover_keywords and estimate_budget) plus geo/language targeting references.
---

# AdLoop — Keyword Research & Budget Forecasting

You have access to AdLoop MCP tools connecting to the Google Ads Keyword Planner API. This skill teaches you how to discover keyword ideas and forecast budget needs for campaign planning.

## Tool Inventory

| Tool | Key Params | When to Use |
|------|-----------|------------|
| `discover_keywords` | `seed_keywords` (list, optional), `url` (optional), `geo_target_id` (default "2276"=Germany), `language_id` (default "1000"=English), `page_size` (default 50, max 1000) | Find new keyword ideas from seed keywords and/or a URL |
| `estimate_budget` | `keywords` (list of {text, match_type, max_cpc?}, required), `daily_budget` (optional), `geo_target_id` (default "2276"=Germany), `language_id` (default "1000"=English), `forecast_days` (default 30), `customer_id` (optional) | Forecast clicks, impressions, and cost for a set of keywords |

## Supporting Read Tools

These tools provide context for planning. Use them to understand the current account before making recommendations:

| Tool | When to Use in Planning |
|------|------------------------|
| `get_keyword_performance` | Check which keywords already exist and their performance before suggesting new ones |
| `get_search_terms` | Understand what search terms are already triggering ads — avoid duplicates and find gaps |
| `get_campaign_performance` | Understand existing campaign structure and budget |

## Orchestration Patterns

### Pattern 1: Budget Planning ("how much should I spend")

This pattern helps users estimate traffic and cost before committing budget to a campaign.

1. **Ask the user for their target keywords** (or suggest some based on the business context and landing page). If they have an existing account, call `get_search_terms` to see what's already working and `get_keyword_performance` to see current keyword performance.

2. **Ask for the target geography and language** (or infer from the existing account). See the Geo & Language Reference below for common IDs.

3. **Call `estimate_budget`** with the keywords, match types, and optional daily budget:
   ```
   keywords: [{"text": "running shoes", "match_type": "EXACT", "max_cpc": 1.50},
              {"text": "trail running shoes", "match_type": "PHRASE"}]
   geo_target_id: "2840"   # United States
   language_id: "1000"     # English
   daily_budget: 50.0      # optional — include if the user has a budget cap
   forecast_days: 30
   ```

4. **Present the forecast results:**
   - Estimated daily clicks, impressions, and cost
   - Estimated average CPC and CTR
   - Daily estimates breakdown (if available)
   - The `insights[]` field contains budget sufficiency analysis

5. **Interpret the results for the user:**
   - If daily budget is too low to capture all available traffic, say so. The forecast will show how many clicks the budget buys vs. total available.
   - If the user didn't provide a budget, the forecast shows maximum available traffic — suggest a budget that captures at least 70% of available impressions.
   - Compare estimated CPA against their business model (if they share conversion rate / target CPA).

6. **If the user wants to proceed**, suggest next steps using the adloop-write skill:
   - Use `draft_campaign` to create the campaign with the forecasted keywords and budget
   - Use `draft_keywords` to add keywords to an existing campaign

### Pattern 2: Discover New Keywords ("what keywords should I target")

This pattern helps users find keyword opportunities they may be missing.

1. **Ask whether to start with seed keywords, a URL, or both:**
   - **Seed keywords** — the user provides 1-5 terms related to their business. This maps to the "Discover new keywords" mode in Google Ads Keyword Planner, finding related terms.
   - **URL** — the user provides a landing page URL. The tool analyzes the page content and suggests relevant keywords. Useful when the user isn't sure which terms to start with.
   - **Both** — combines seed keywords with URL analysis for the broadest set of ideas.

2. **Call `discover_keywords`** with the appropriate parameters:
   ```
   # Seed keywords only
   seed_keywords: ["CRM software", "customer relationship management"]
   geo_target_id: "2276"   # Germany
   language_id: "1001"     # German
   
   # URL only
   url: "https://example.com/products/crm"
   geo_target_id: "2840"   # United States
   language_id: "1000"     # English
   
   # Both
   seed_keywords: ["project management"]
   url: "https://example.com/project-management"
   ```

3. **Review the results:**
   - `keyword_ideas[]` contains each idea with `avg_monthly_searches`, `competition` (UNSPECIFIED/LOW/MEDIUM/HIGH), `competition_index`, and bid range
   - `insights[]` highlights the highest-volume idea, high-competition terms, and low-competition opportunities
   - `total_ideas` shows how many results were returned

4. **Present results grouped by competition level:**

   **Easy wins (LOW competition):**
   - Low competition means less advertisers bidding, so CPC is typically lower
   - These are often long-tail or niche terms — great for starting campaigns
   - May have lower search volume but higher conversion intent

   **Moderate (MEDIUM competition):**
   - Balanced between volume and cost
   - Good candidates for PHRASE match to control when ads appear

   **Competitive (HIGH competition):**
   - High volume but expensive CPC
   - Only target these if the budget allows and conversion tracking is working
   - Consider EXACT match to control costs on these terms
   - Budget may be better spent on medium/low competition terms

5. **For keywords the user wants to act on**, suggest next steps:
   - Use `estimate_budget` with the selected keywords to forecast traffic and cost
   - Use `draft_keywords` to add them to an existing ad group
   - Use `draft_campaign` to build a new campaign around them

## Match Type Guidance

Choosing the right match type is critical for budget efficiency:

| Match Type | Behavior | When to Use |
|-----------|----------|------------|
| **EXACT** | Matches the exact term only (with close variants). Minimum reach, maximum control. | High-value keywords where you want precise control. Best for budget-constrained campaigns and for terms where broad matching would waste spend. |
| **PHRASE** | Matches the term with words before/after. Moderate reach, good control. | The sweet spot for most campaigns. Captures related searches while maintaining relevance. Start here when unsure. |
| **BROAD** | Matches any related term. Maximum reach, minimum control. **REQUIRES Smart Bidding** (MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS). NEVER use BROAD with MANUAL_CPC. | Only when using Smart Bidding and wanting maximum reach. Google's algorithm needs significant data to optimize Broad Match. |

**Common mistake:** Using BROAD match without Smart Bidding. This is the #1 cause of wasted ad spend. BROAD on MANUAL_CPC will match irrelevant queries and drain budget fast.

## Geo & Language Targeting Reference

### Common Geo Target IDs

| ID | Country | ID | Country |
|----|---------|-----|---------|
| 2840 | United States | 2276 | Germany |
| 2826 | United Kingdom | 2040 | Austria |
| 2250 | France | 2756 | Switzerland |
| 2380 | Italy | 2724 | Spain |
| 2528 | Netherlands | 2056 | Belgium |
| 2036 | Australia | 2124 | Canada |
| 2616 | Poland | 1014 | — |

For cities/regions, use `run_gaql` with `geo_target_constant` resource or look up IDs in Google Ads API Geo Target documentation.

### Common Language IDs

| ID | Language | ID | Language |
|----|----------|-----|----------|
| 1000 | English | 1001 | German |
| 1002 | French | 1003 | Spanish |
| 1004 | Italian | 1005 | Dutch |
| 1009 | Portuguese | 1014 | Polish |

## Budget Sufficiency Rules

When advising on budgets:

- **Daily budget should be at least 5x the target CPA.** A campaign budget below 5x CPA may not generate enough data for Smart Bidding to optimize. For example, if target CPA is $10, daily budget should be at least $50.
- **The forecast from `estimate_budget` shows how many clicks the budget buys.** If clicks drop significantly with a lower budget, the budget is insufficient.
- **Start with a higher budget and optimize down** rather than starting too low. Smart Bidding needs data to learn from.
- **New campaigns need time.** Don't judge a campaign's profitability until it has at least 30 conversion actions (or 100 if using micro-conversions).

## Forecast Interpretation

The `estimate_budget` result contains:

- `estimated_clicks`: Total clicks over the forecast period
- `estimated_impressions`: Total impressions
- `estimated_cost`: Total cost in the account's currency
- `estimated_avg_cpc`: Average cost per click
- `estimated_ctr`: Click-through rate as a decimal
- `daily_estimates[]`: Per-day breakdown
- `keywords_used[]`: The keywords entered (with match types and max CPC bids)
- `insights[]`: Budget sufficiency analysis

**What to highlight for the user:**
1. **Avg CPC vs. their budget**: Can they afford the traffic?
2. **Impression share**: If daily_budget is set, does it capture most available impressions or just a fraction?
3. **CTR**: Is it reasonable for their industry? (>2% for search is typical, >5% is strong)
4. **Cost trends**: Are daily costs stable or spiking?

## Competition Levels

The `discover_keywords` result includes `competition` and `competition_index`:

| Competition | Index Range | CPC Impact | Strategy |
|------------|-------------|-----------|----------|
| LOW | 0-33 | Lower CPC, less competition | Easy wins. Start here with modest budgets. |
| MEDIUM | 34-66 | Moderate CPC | Good balance of volume and cost. Use PHRASE match. |
| HIGH | 67-100 | Higher CPC, fierce competition | Only if budget allows and conversion tracking works. Use EXACT match for control. |

The `competition_index` is more granular than the label — use it for fine-grained prioritization.

## Default Parameters

When the user doesn't specify:
- **Geo target**: Use Germany (2276) as the default (the tool's default), but ASK the user which country they're targeting before running the forecast if not specified
- **Language**: Use English (1000) as the default, but ASK the user which language to target
- **Forecast days**: 30 (one month)
- **Page size**: 50 keyword ideas (max 1000)
- **Date range for supporting reads**: Last 30 days

## Transition to Campaign Creation

When the user is ready to act on the planning results, the natural next step is to create a campaign. This transitions to the adloop-write skill:

1. Use `estimate_budget` results to inform the `campaign_name`, `daily_budget`, `bidding_strategy`, and keyword list for `draft_campaign`
2. Use `discover_keywords` results to populate the `keywords` parameter with the right match types
3. Use `get_keyword_performance` and `get_search_terms` (from adloop-read) to avoid duplicating existing keywords
4. Then follow the campaign creation pattern in adloop-write