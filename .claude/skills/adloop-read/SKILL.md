---
name: adloop-read
description: AdLoop performance analysis and reporting. Use this skill whenever the user asks about ad performance, campaign metrics, conversions, search terms, audience data, GA4 reports, PMax analysis, keyword quality, recommendations, or any read-only analysis of Google Ads or GA4 data. Also use when the user says "how are my ads doing", "show me my campaigns", "analyze performance", "check conversions", "what keywords are performing", "GA4 report", "search terms report", "attribution", "landing page performance", "PMax analysis", "audience targeting", or any question that requires reading (not modifying) advertising data. This skill covers all 22 read-only AdLoop tools plus cross-reference, GAQL, and 7 orchestration patterns.
---

# AdLoop — Performance Analysis & Reporting

You have access to AdLoop MCP tools connecting Google Ads and GA4. This skill teaches you how to use the read-only tools intelligently for analysis, reporting, and diagnosis.

## Tool Inventory

### Diagnostics

| Tool | When to Use |
|------|------------|
| `health_check` | First thing to run when tools are failing. Tests OAuth, GA4, and Ads connectivity. If auth errors appear, tell user to delete `~/.adloop/token.json` and retry. Weekly expiry means GCP consent screen is in "Testing" mode — publish it. |

### GA4 Read Tools

| Tool | Key Params | When to Use |
|------|-----------|------------|
| `get_account_summaries` | *(none)* | First-time discovery — find which GA4 properties exist |
| `run_ga4_report` | `dimensions`, `metrics`, `date_range_start` (default "7daysAgo"), `date_range_end` (default "today"), `property_id`, `limit` (default 100) | Any analytics question — sessions, users, conversions, page performance |
| `run_realtime_report` | `dimensions`, `metrics`, `property_id` | After code deploys — verify tracking fires correctly. Default metric: `activeUsers` |
| `get_tracking_events` | `date_range_start` (default "28daysAgo"), `date_range_end` (default "today"), `property_id` | Understanding what events are configured and their volume |

### Google Ads Read Tools

| Tool | Key Params | When to Use |
|------|-----------|------------|
| `list_accounts` | *(none)* | First-time discovery — find which Ads accounts exist |
| `get_campaign_performance` | `customer_id`, `date_range_start`, `date_range_end` | Campaign-level metrics: impressions, clicks, cost, conversions, CPA, ROAS, CTR |
| `get_ad_performance` | `customer_id`, `date_range_start`, `date_range_end` | Ad copy analysis — which headlines/descriptions work |
| `get_keyword_performance` | `customer_id`, `date_range_start`, `date_range_end` | Keyword analysis — quality scores, competitive metrics |
| `get_search_terms` | `customer_id`, `date_range_start`, `date_range_end` | Find negative keyword opportunities and understand user intent |
| `get_negative_keywords` | `customer_id`, `campaign_id` | List campaign-level negative keywords |
| `get_negative_keyword_lists` | `customer_id` | List all shared negative keyword lists (SharedSets) |
| `get_negative_keyword_list_keywords` | `shared_set_id` (required), `customer_id` | List keywords inside a specific shared list |
| `get_negative_keyword_list_campaigns` | `shared_set_id`, `customer_id` | List which campaigns a shared list is attached to |
| `get_recommendations` | `customer_id`, `recommendation_types`, `campaign_id` | Google's auto-generated recommendations with estimated impact |
| `get_pmax_performance` | `customer_id`, `date_range_start`, `date_range_end` | PMax campaign metrics with network breakdown + asset group ad strength |
| `get_asset_performance` | `customer_id`, `campaign_id` | Per-asset details for PMax — field type, serving status, content |
| `get_detailed_asset_performance` | `customer_id`, `campaign_id` | Top-performing asset combinations for PMax |
| `get_audience_performance` | `customer_id`, `date_range_start`, `date_range_end`, `campaign_id` | Audience segment metrics — remarketing, in-market, demographics |

### Cross-Reference Tools (GA4 + Ads combined)

| Tool | Key Params | When to Use |
|------|-----------|------------|
| `analyze_campaign_conversions` | `customer_id`, `property_id`, `date_range_start`, `date_range_end`, `campaign_name` | "What's my real CPA?", paid vs organic comparison, GDPR gap analysis |
| `landing_page_analysis` | `customer_id`, `property_id`, `date_range_start`, `date_range_end` | "Which landing pages convert?", pages with traffic but no conversions |
| `attribution_check` | `customer_id`, `property_id`, `date_range_start`, `date_range_end`, `conversion_events` | "Are my conversions tracked correctly?", Ads vs GA4 discrepancies |

### GAQL (custom queries)

| Tool | Key Params | When to Use |
|------|-----------|------------|
| `run_gaql` | `query` (required), `customer_id`, `format` (table/json/csv) | Custom queries not covered by other tools |

### Return Format Notes

- Ads read tools auto-compute `metrics.cost` and `metrics.cpa` from `metrics.cost_micros`. No manual division needed. `metrics.currency` has the account's currency code.
- `get_ad_performance` returns full `headlines` and `descriptions` lists for RSAs.
- `get_recommendations` returns `estimated_improvement` per recommendation and `insights[]` flagging self-serving budget recommendations.
- PMax: `segments.ad_network_type` includes MIXED — a catch-all for most PMax traffic. Full channel splits are not available via the API.
- `get_audience_performance` works for campaigns with explicit audience targeting. PMax audience targeting is automatic and may not appear.

## Default Parameters

When the user doesn't specify:
- **Date range**: Default to last 30 days for Ads, last 7 days for GA4
- **Customer ID**: Use the default from config (no need to ask)
- **Property ID**: Use the default from config (no need to ask)
- **Format**: Use "table" for run_gaql results

## Orchestration Patterns

### Pattern 1: Performance Review ("how are my ads doing")

This is the most common pattern. Follow this when the user asks about campaign performance, spending, or wants a general overview.

1. Call `get_campaign_performance` for the relevant date range
2. If results include PMax campaigns (`campaign.advertising_channel_type = PERFORMANCE_MAX`), also call `get_pmax_performance` for asset group ad strength and network breakdown
3. If they mention conversions, CPA, or "is it worth it", call `analyze_campaign_conversions` — it gives Ads + GA4 data in one call with GDPR-aware cost-per-conversion
4. If they mention specific keywords or search terms, also call `get_keyword_performance` or `get_search_terms`
5. Present a summary with key metrics: spend (`metrics.cost`), clicks, conversions, CPA (`metrics.cpa`), CTR
6. Highlight anything concerning: zero conversions, high CPA, low quality scores, wasteful search terms
7. Compare against best practices (see Marketing Best Practices below)
8. If the account has active recommendations, mention that `get_recommendations` can surface suggestions

### Pattern 2: Conversion Analysis (conversion drops, zero conversions)

1. Call `attribution_check` with relevant date range and `conversion_events` if the user mentions specific events (e.g. sign_up, purchase) — this does Ads vs GA4 comparison in one call and auto-generates insights
2. If the discrepancy needs page-level drill-down, call `landing_page_analysis` to see which pages get paid traffic but don't convert
3. Call `get_search_terms` to see if search intent shifted
4. **Before concluding tracking is broken:** Check the `insights` from `attribution_check` — it already factors in GDPR consent gaps. Only diagnose a tracking issue if the tool's insights suggest it.

### Pattern 3: Landing Page Performance

1. Call `landing_page_analysis` — it combines ad final URLs with GA4 page data in one call
2. Review `insights[]` for pages with traffic but zero conversions, high bounce rates, or orphaned URLs
3. If the codebase is accessible, read the flagged landing pages to identify UX or content issues
4. Present results sorted by paid sessions, highlighting problem pages

### Pattern 4: Paid vs Organic Comparison

1. Call `analyze_campaign_conversions` — it returns both paid campaign metrics and non-paid channel conversion rates
2. Compare `campaigns[].ga4_conversion_rate` (paid) vs `non_paid_channels[].conversion_rate` (organic/direct/referral)
3. If paid conversion rate is significantly lower, investigate landing page relevance and ad targeting before increasing spend

### Pattern 5: PMax (Performance Max) Analysis

1. Call `get_pmax_performance` for campaign-level metrics with network breakdown and asset group ad strength
2. Review `insights[]` — weak ad strength and zero-conversion asset groups are the most actionable findings
3. If ad strength is POOR or AVERAGE, call `get_asset_performance` to see which specific assets are underperforming and which asset types are missing
4. Call `get_detailed_asset_performance` to see which headline+description+image combinations Google selects most
5. To improve PMax performance:
   - Replace LOW-performing assets with new ones
   - Ensure minimum asset diversity: 5+ headlines, 5+ descriptions, 5+ marketing images, 1+ landscape image, 1+ logo, 1+ YouTube video
   - Check that final URLs point to relevant, working landing pages
6. Note: PMax campaigns are partially opaque. MIXED is a catch-all for most traffic. Full per-channel transparency is not available via the API.

### Pattern 6: Google Recommendations Evaluation

1. Call `get_recommendations` to retrieve all active (non-dismissed) recommendations
2. Review the `by_type` summary and `insights[]` — these flag self-serving budget recommendations
3. **NEVER blindly endorse Google's recommendations.** Cross-reference each against actual account data:
   - "Raise budget" + zero conversions → bad advice. Fix tracking/landing pages first.
   - "Add keywords" + quality score < 5 → bad advice. Fix relevance first.
   - "Switch to Maximize Conversions" from Manual CPC → likely good advice, but verify conversion tracking works.
   - "Use Broad Match" without Smart Bidding → reject this.
4. Use `estimated_improvement` to prioritize: recommendations with >1 estimated additional conversion are worth investigating
5. For keyword recommendations, call `get_keyword_performance` to check overlap

### Pattern 7: Audience Performance / Targeting

1. Call `get_audience_performance` for the relevant date range
2. If filtered to a specific campaign, also call `get_campaign_performance` for context
3. Compare audience segment metrics: which segments convert best? Which have high spend but zero conversions?
4. For PMax campaigns, note that audience targeting is automatic — audience data may not appear in the report

## GDPR Consent & Data Discrepancies

Most websites (especially in the EU) use a GDPR cookie consent banner. This critically impacts data interpretation:

- **Google Ads counts all clicks** regardless of consent.
- **GA4 only records sessions for users who accept analytics cookies.** Users who reject are invisible to GA4.
- **Ads clicks will almost always be higher than GA4 sessions.** A ratio of 2:1 to 5:1 (clicks:sessions) is normal with consent banners, not a tracking bug.
- **GA4 conversion events are also affected** — only consenting users trigger events. True conversion rates are likely higher than GA4 reports.

**Before diagnosing a tracking issue, always consider consent:**
1. If Ads shows 10 clicks but GA4 shows 3 sessions → likely consent rejection, not broken tracking.
2. If GA4 shows 0 sessions from paid traffic → consent could explain it, but also check UTM parameters and GA4 filters.
3. Only flag tracking as broken when the discrepancy cannot be explained by consent (e.g., GA4 shows zero for ALL traffic sources, or organic traffic also shows anomalies).

**Google Consent Mode v2:** Some sites send cookieless pings to GA4 even without consent. This reduces (but doesn't eliminate) the gap. If GA4 data is closer to Ads data, Consent Mode may be active. Check for `gtag('consent', ...)` calls in the codebase.

## GAQL Quick Reference

GAQL is SQL-like but with specific resource names and field paths.

### Basic Syntax

```sql
SELECT field1, field2, ...
FROM resource
WHERE condition
ORDER BY field [ASC|DESC]
LIMIT n
```

### Common Resources

| Resource | Use For |
|----------|---------|
| `campaign` | Campaign-level data |
| `ad_group` | Ad group-level data |
| `ad_group_ad` | Ad-level data (includes ad copy) |
| `keyword_view` | Keyword performance |
| `search_term_view` | Search terms report |
| `ad_group_criterion` | Keywords and targeting criteria |
| `campaign_budget` | Budget information |
| `bidding_strategy` | Bidding strategy details |
| `customer_client` | List accounts under an MCC |
| `asset_group` | PMax asset group data |
| `asset_group_asset` | PMax per-asset performance |
| `asset_group_top_combination_view` | PMax top asset combinations |
| `recommendation` | Google's auto-generated recommendations |
| `ad_group_audience_view` | Audience segment performance |

### Common Fields

**Campaign:** `campaign.id`, `campaign.name`, `campaign.status`, `campaign.advertising_channel_type` (SEARCH, DISPLAY, SHOPPING, VIDEO), `campaign.bidding_strategy_type`

**Ad Group:** `ad_group.id`, `ad_group.name`, `ad_group.status`, `ad_group.cpc_bid_micros`

**Ad:** `ad_group_ad.ad.responsive_search_ad.headlines`, `ad_group_ad.ad.responsive_search_ad.descriptions`, `ad_group_ad.ad.final_urls`, `ad_group_ad.status`

**Keyword:** `ad_group_criterion.keyword.text`, `ad_group_criterion.keyword.match_type` (EXACT, PHRASE, BROAD), `ad_group_criterion.quality_info.quality_score`

**Metrics:** `metrics.impressions`, `metrics.clicks`, `metrics.cost_micros`, `metrics.conversions`, `metrics.conversions_value`, `metrics.ctr`, `metrics.average_cpc`, `metrics.search_impression_share`, `metrics.search_rank_lost_impression_share`

**Segments:** `segments.date`, `segments.device` (MOBILE, DESKTOP, TABLET), `segments.ad_network_type` (SEARCH, CONTENT, YOUTUBE)

### Date Ranges

```sql
WHERE segments.date DURING LAST_7_DAYS
WHERE segments.date DURING LAST_30_DAYS
WHERE segments.date DURING THIS_MONTH
WHERE segments.date DURING LAST_MONTH
WHERE segments.date BETWEEN '2026-01-01' AND '2026-01-31'
```

### Important GAQL Rules

- NO `SELECT *` — every field must be named explicitly
- **Fields in ORDER BY must appear in SELECT.** This is the most common GAQL error.
- Metrics cannot appear in WHERE with resource fields in the same query
- `cost_micros` values are in micros — divide by 1,000,000. The dedicated read tools already compute `metrics.cost` and `metrics.cpa`. Only `run_gaql` returns raw micros.
- When selecting `segments.date`, results are broken down by day
- Status values: `'ENABLED'`, `'PAUSED'`, `'REMOVED'`
- `search_term_view` always requires a date segment in WHERE

### Example Queries

**Top campaigns by spend:**
```sql
SELECT campaign.name, campaign.status, metrics.cost_micros, metrics.clicks, metrics.conversions
FROM campaign
WHERE segments.date DURING LAST_30_DAYS AND campaign.status = 'ENABLED'
ORDER BY metrics.cost_micros DESC
LIMIT 10
```

**Keywords with low quality score:**
```sql
SELECT ad_group_criterion.keyword.text, ad_group_criterion.quality_info.quality_score,
       metrics.impressions, metrics.clicks, metrics.cost_micros
FROM keyword_view
WHERE segments.date DURING LAST_30_DAYS
  AND ad_group_criterion.quality_info.quality_score < 5
ORDER BY metrics.cost_micros DESC
```

**Search terms that cost money but don't convert:**
```sql
SELECT search_term_view.search_term, metrics.clicks, metrics.cost_micros, metrics.conversions
FROM search_term_view
WHERE segments.date DURING LAST_30_DAYS AND metrics.clicks > 5 AND metrics.conversions = 0
ORDER BY metrics.cost_micros DESC
LIMIT 20
```

**Ad groups in a specific campaign:**
```sql
SELECT ad_group.id, ad_group.name, ad_group.status
FROM ad_group
WHERE campaign.id = 12345678
```

## Marketing Best Practices

When analyzing Google Ads performance and advising users:

- **Bid strategy**: MAXIMIZE_CONVERSIONS is the best default for most campaigns. TARGET_SPEND (Maximize Clicks) only makes sense as a deliberate data-collection phase.
- **CPA monitoring**: Flag any campaign where CPA exceeds 3x the target.
- **Budget sufficiency**: A campaign's daily budget should be at least 5x its target CPA.
- **Quality Score**: If keywords have QS < 5, the problem is ad relevance/landing page, not keyword count.
- **Zero conversions**: When a campaign has spent significantly with zero conversions, investigate: (1) GDPR consent (2) conversion tracking (3) landing page (4) search term relevance. Don't just increase budget.
- **Clicks vs sessions gap**: Never report a clicks > sessions discrepancy as a tracking bug without accounting for GDPR consent first.
- **PMax transparency**: The API does not provide full channel breakdowns for PMax. MIXED is a catch-all. Be honest about data limitations.
- **Google recommendations**: Not neutral — they optimize for Google's revenue. Budget increase and Broad Match recommendations should always be cross-referenced against actual conversion data.