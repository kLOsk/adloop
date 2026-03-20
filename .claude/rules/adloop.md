---
description: AdLoop MCP orchestration — Google Ads + GA4 + codebase intelligence
---

# AdLoop — AI Orchestration Rules

You have access to AdLoop MCP tools that connect Google Ads and Google Analytics (GA4) data. These rules teach you how to use them intelligently.

## Tool Inventory

### Diagnostics

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `health_check` | First thing to run when tools are failing — tests OAuth token, GA4, and Ads connectivity | (none) |

**If health_check reports auth errors:** Tell the user to delete `~/.adloop/token.json` and re-run any tool to trigger re-authorization. If tokens keep expiring weekly, the GCP consent screen needs to be published from "Testing" to "In production".

### GA4 Read Tools

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `get_account_summaries` | First-time discovery — find which GA4 properties exist | (none — uses config) |
| `run_ga4_report` | Any analytics question — sessions, users, conversions, page performance | `dimensions`, `metrics`, `date_range_start`, `date_range_end`, `limit` |
| `run_realtime_report` | After code deploys — verify tracking fires correctly | `dimensions`, `metrics` |
| `get_tracking_events` | Understanding what events are configured and their volume | `date_range_start`, `date_range_end` |

### Google Ads Read Tools

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `list_accounts` | First-time discovery — find which Ads accounts exist | (none — uses MCC from config) |
| `get_campaign_performance` | Campaign-level metrics — impressions, clicks, cost, conversions | `date_range_start`, `date_range_end` |
| `get_ad_performance` | Ad copy analysis — which headlines/descriptions work | `date_range_start`, `date_range_end` |
| `get_keyword_performance` | Keyword analysis — quality scores, competitive metrics | `date_range_start`, `date_range_end` |
| `get_search_terms` | Find negative keyword opportunities and understand user intent | `date_range_start`, `date_range_end` |
| `get_negative_keywords` | List existing negative keywords for a campaign or all campaigns | `campaign_id` (optional) |
| `run_gaql` | Custom queries not covered by other tools | `query`, `format` (table/json/csv) |

**Return format notes:**
- Ads read tools automatically compute `metrics.cost` and `metrics.cpa` from `metrics.cost_micros` — no manual division needed. `metrics.currency` contains the account's currency code (auto-detected).
- `metrics.average_cpc_amount` is also pre-computed where available.
- `get_ad_performance` returns full `headlines` and `descriptions` lists for RSAs.

### Cross-Reference Tools (GA4 + Ads combined)

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `analyze_campaign_conversions` | "What's my real CPA?", paid vs organic comparison, GDPR gap analysis | `date_range_start`, `date_range_end`, `campaign_name` (optional filter) |
| `landing_page_analysis` | "Which landing pages convert?", identify pages with traffic but no conversions | `date_range_start`, `date_range_end` |
| `attribution_check` | "Are my conversions tracked correctly?", Ads vs GA4 conversion discrepancies | `date_range_start`, `date_range_end`, `conversion_events` (optional GA4 event names) |

These tools call both APIs internally and return unified results with computed `insights[]`. They are read-only — no mutations. Each returns a `date_range` and auto-generates conditional warnings (GDPR gaps, zero conversions, attribution mismatches, orphaned URLs).

### Tracking Tools

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `validate_tracking` | Compare codebase event code against actual GA4 events — find missing/broken tracking | `expected_events` (list of event names found in code), `date_range_start`, `date_range_end` |
| `generate_tracking_code` | Generate ready-to-paste GA4 gtag JavaScript for an event | `event_name`, `event_params` (optional), `trigger` (form_submit/button_click/page_load) |

`validate_tracking` requires the AI to first search the codebase for `gtag('event', ...)` or `dataLayer.push({event: ...})` calls, extract event names, then pass them to the tool. The tool queries GA4 and returns a structured comparison (matched, missing, unexpected, auto-collected).

`generate_tracking_code` includes recommended parameters for well-known GA4 events (sign_up, purchase, etc.) and optionally checks if the event already fires in GA4.

### Planning Tools

| Tool | When to Use | Key Parameters |
|------|-------------|----------------|
| `estimate_budget` | Budget planning before launching a campaign — forecasts clicks, impressions, cost for a set of keywords | `keywords` (list of {text, match_type, max_cpc}), `daily_budget` (optional), `geo_target_id`, `language_id`, `forecast_days` |

`estimate_budget` calls the Google Ads Keyword Planner API (read-only — creates nothing). Returns forecast metrics for the specified keywords and optional budget, including daily estimates and insights about budget sufficiency. Common geo targets: 2276=Germany, 2840=USA, 2826=UK. Common languages: 1000=English, 1001=German, 1002=French.

### Google Ads Write Tools (ALL require safety confirmation)

| Tool | What It Does | Validation |
|------|-------------|------------|
| `draft_campaign` | Create full campaign structure (budget + campaign + ad group + keywords + geo/language targeting) | `campaign_name`, `daily_budget`, `bidding_strategy`, `geo_target_ids` (REQUIRED), `language_ids` (REQUIRED), optional `search_partners_enabled`, `display_network_enabled`, `display_expansion_enabled`, optional `max_cpc` for MANUAL_CPC ad-group bids or TARGET_SPEND CPC caps |
| `draft_ad_group` | Create a new ad group within an existing campaign (does NOT publish) | `campaign_id` (REQUIRED), `ad_group_name` (REQUIRED), `keywords` (optional list of {text, match_type}), `cpc_bid_micros` (optional) |
| `update_campaign` | Modify existing campaign settings — bid strategy, budget, geo targets, language targets, Search partners, display expansion | `campaign_id` (REQUIRED), plus any of: `bidding_strategy`, `daily_budget`, `geo_target_ids`, `language_ids`, `search_partners_enabled`, `display_network_enabled`, TARGET_SPEND `max_cpc` |
| `update_ad_group` | Update ad group name and/or MANUAL_CPC `max_cpc` | `ad_group_id`, optional `ad_group_name`, optional `max_cpc` |
| `draft_responsive_search_ad` | Create RSA preview (does NOT publish) | 3-15 headlines (≤30 chars), 2-4 descriptions (≤90 chars), final_url required, path1/path2 (≤15 chars each) |
| `draft_callouts` | Create callout assets for a campaign (does NOT publish) | `campaign_id`, `callouts` list with 1-25 chars each |
| `draft_structured_snippets` | Create structured snippet assets for a campaign (does NOT publish) | `campaign_id`, `snippets` list of `{header, values}` with official header values and 3-10 values |
| `draft_image_assets` | Create image assets for a campaign from local files (does NOT publish) | `campaign_id`, `image_paths` list of local PNG/JPEG/GIF files |
| `draft_sitelinks` | Create sitelink extensions for a campaign (does NOT publish) | `campaign_id`, `sitelinks` list of {link_text ≤25 chars, final_url, description1 ≤35 chars, description2 ≤35 chars} |
| `draft_keywords` | Propose keyword additions (does NOT add) | Each keyword needs `text` and `match_type` (EXACT/PHRASE/BROAD) |
| `add_negative_keywords` | Propose negative keywords (does NOT add) | `campaign_id`, keyword list, `match_type` |
| `pause_entity` | Propose pausing campaign/ad group/ad/keyword | `entity_type`, `entity_id` |
| `enable_entity` | Propose enabling paused entity | `entity_type`, `entity_id` |
| `remove_entity` | Propose REMOVING an entity (irreversible) | `entity_type` (incl. "negative_keyword", "campaign_asset", "asset", "customer_asset"), `entity_id` |
| `confirm_and_apply` | Execute a previously previewed change | `plan_id` from a draft tool, `dry_run` (default true) |

**Write tool workflow:**
1. Call a `draft_*` tool → returns a preview with a `plan_id`
2. Show the full preview to the user and wait for approval
3. Call `confirm_and_apply(plan_id=..., dry_run=true)` first to test
4. Only call with `dry_run=false` after explicit user confirmation

**Safety behaviors:**
- New campaigns and RSAs are created as PAUSED — user must explicitly enable them after review.
- `draft_campaign` REQUIRES `geo_target_ids` and `language_ids` — campaigns without targeting waste budget. The tool rejects drafts with missing targeting.
- `draft_campaign` enforces the `max_daily_budget` safety cap, rejects BROAD match + non-Smart Bidding, warns if budget is below 5x target CPA, and interprets `max_cpc` by bidding strategy: MANUAL_CPC seeds the initial ad-group bid, TARGET_SPEND sets the Maximize Clicks CPC ceiling.
- `display_network_enabled` is the canonical Search display-expansion flag. `display_expansion_enabled` is only a compatibility alias and should be normalized away before presenting the plan to the user.
- `update_ad_group` is the right tool for later MANUAL_CPC bid changes. Use `update_campaign` for TARGET_SPEND (Maximize Clicks) `max_cpc` changes.
- Ad-group pause/enable is already handled by `pause_entity` / `enable_entity` with `entity_type="ad_group"`; do not invent a separate pause tool.
- `update_campaign` replaces geo/language targets entirely (not append). Pass the full desired list.
- `remove_entity` is IRREVERSIBLE — always prefer `pause_entity` unless the user explicitly wants permanent removal. Removal triggers double confirmation in the safety layer.
- `remove_entity` supports `entity_type` values: "campaign", "ad_group", "ad", "keyword", "negative_keyword", "campaign_asset", "asset", "customer_asset". Use "negative_keyword" to remove campaign-level negative keywords. Use "campaign_asset" to remove sitelinks and other asset links from a campaign. Use "asset" to remove a standalone asset. Use "customer_asset" to remove a customer-level asset link.
- `require_dry_run: true` in config overrides `dry_run=false` — the user must change the config to allow real mutations.
- All operations (including dry runs) are logged to `~/.adloop/audit.log`.

## Safety Rules (CRITICAL — always follow)

1. **NEVER call confirm_and_apply without showing the preview to the user first.** Always present the full preview from a draft_* tool and wait for explicit user approval.

2. **Default to dry_run=true.** When calling confirm_and_apply, always use dry_run=true unless the user explicitly says to apply for real. Even then, `require_dry_run` in config may override this.

3. **Respect budget caps.** The config has max_daily_budget set. Never propose a campaign budget above this.

4. **Double-check destructive operations.** For any pause, enable, remove, or budget change, explicitly warn the user about the impact before proceeding. `remove_entity` is irreversible — prefer `pause_entity` and only use removal when the user explicitly requests it.

5. **One change at a time.** Don't batch multiple write operations. Draft one change, get approval, apply it, then move to the next.

6. **Never guess entity IDs.** Always retrieve IDs from a read tool first (`get_campaign_performance` for campaign IDs, `get_ad_performance` for ad IDs, etc.) before passing them to write tools.

7. **NEVER add BROAD match keywords without verifying Smart Bidding.** Before calling `draft_keywords` with BROAD match, ALWAYS check the campaign's bidding strategy via `get_campaign_performance` or `run_gaql`. If the campaign uses MANUAL_CPC, MANUAL_CPM, or any non-Smart Bidding strategy, REFUSE to add BROAD match keywords. Use PHRASE or EXACT instead. The `draft_keywords` tool will also return a warning, but you must catch this BEFORE drafting. Broad Match + Manual CPC is the single most common cause of wasted ad spend.

8. **NEVER create ads or sitelinks with URLs you haven't verified.** Every `final_url` in an RSA and every sitelink URL MUST point to a real, working page. The draft tools now validate URLs automatically and reject non-reachable ones. But before you even call a draft tool, verify the URLs exist — check the codebase for route definitions, or confirm the pages are live. Ads pointing to 404 pages waste budget, destroy quality score, and create a terrible user experience. This applies to display paths too — don't invent URL paths that don't exist on the site.

9. **Pre-write validation: check before you change.** Before ANY write operation, verify the campaign/ad group context is sound:
   - Check bidding strategy (rule 7)
   - Check if conversion tracking is active (zero conversions + high spend = problem to fix first, not more ads to create)
   - Check quality scores (if all keywords have QS < 5, the problem is landing page/relevance, not keyword count)
   - If the account has systemic issues, WARN the user before making changes that won't help. Adding more keywords to a campaign with zero conversions and quality score 0 makes things worse, not better.

## GDPR Consent & Data Discrepancies

Most websites (especially in the EU) use a GDPR cookie consent banner. This has a critical impact on data interpretation:

- **Google Ads counts all clicks** regardless of consent. A click is a click — no consent needed.
- **GA4 only records sessions for users who accept analytics cookies.** Users who reject or ignore the consent banner are invisible to GA4.
- **This means Ads clicks will almost always be higher than GA4 sessions.** A ratio of 2:1 to 5:1 (clicks:sessions) is normal with consent banners, not a tracking bug.
- **Conversion events in GA4 are also affected** — only consenting users trigger events. True conversion rates are likely higher than what GA4 reports.

**Before diagnosing a tracking issue, always consider consent:**
1. If Ads shows 10 clicks but GA4 shows 3 sessions → likely consent rejection, not broken tracking.
2. If GA4 shows 0 sessions from paid traffic → consent could explain it, but also check UTM parameters and GA4 filters.
3. Only flag tracking as broken when the discrepancy cannot be explained by consent (e.g., GA4 shows zero sessions for ALL traffic sources, or organic traffic also shows anomalies).

**Google Consent Mode v2:** Some sites implement Consent Mode, which sends cookieless pings to GA4 even without consent. This reduces (but doesn't eliminate) the gap. If you see GA4 data is closer to Ads data, Consent Mode may be active. Check for `gtag('consent', ...)` calls in the codebase.

## Orchestration Patterns

### When user asks about performance or "how are my ads doing"

1. Call `get_campaign_performance` for the relevant date range
2. If they mention conversions, CPA, or "is it worth it", call `analyze_campaign_conversions` instead — it gives Ads + GA4 data in one call with GDPR-aware cost-per-conversion
3. If they mention specific keywords or search terms, also call `get_keyword_performance` or `get_search_terms`
4. Present a summary with the key metrics: spend (`metrics.cost`), clicks, conversions, CPA (`metrics.cpa`), CTR
5. Highlight anything concerning: zero conversions, high CPA, low quality scores, wasteful search terms
6. Compare against best practices (see Marketing Best Practices section)

### When user asks about conversions or conversion drops

1. Call `attribution_check` with relevant date range and `conversion_events` if the user mentions specific events (e.g. sign_up, purchase) — this does the Ads vs GA4 comparison in one call and auto-generates insights
2. If the discrepancy needs page-level drill-down, call `landing_page_analysis` to see which pages get paid traffic but don't convert
3. Call `get_search_terms` to see if search intent shifted
4. **Before concluding tracking is broken:** Check the `insights` from `attribution_check` — it already factors in GDPR consent gaps. Only diagnose a tracking issue if the tool's insights suggest it.
5. If the user's codebase is accessible (Cursor native), search for recent changes to the affected pages
6. Present a unified diagnosis combining the cross-reference tool insights with code analysis

### When user wants to create an ad

1. Call `get_campaign_performance` to understand existing campaign structure and find the right `campaign.id`
2. **Pre-write checks (CRITICAL):**
   - Is the target campaign's bidding strategy appropriate? MANUAL_CPC campaigns should NOT get more ads before fixing bidding.
   - Does the campaign have any conversions? If it has significant spend and zero conversions, WARN the user that adding ads won't help — conversion tracking and campaign setup need fixing first.
   - What are the quality scores? If all keywords are below 5, improving ad relevance and landing pages matters more than new ads.
3. Use `run_gaql` to find ad group IDs: `SELECT ad_group.id, ad_group.name FROM ad_group WHERE campaign.id = {campaign_id}`
4. Call `get_tracking_events` to verify conversion tracking exists
5. If codebase is accessible, read the landing page code to extract value propositions and determine the language. **Verify the final_url page actually exists** — check route definitions or confirm the URL is live. NEVER use a URL you haven't verified.
6. Call `draft_responsive_search_ad` with at least 8-10 diverse headlines and 3-4 descriptions. Write copy in the correct language — if the landing page is multilingual or the language is unclear, ask the user before writing. Follow the "Ad Copy Character Limits" section — count characters for every headline before generating
7. **Always set display paths** (`path1`, `path2`, max 15 chars each). These appear in the display URL (e.g. `example.com/Products/Pricing`) and significantly improve ad relevance. Derive them from the landing page URL structure or the ad's value proposition.
8. Present the complete preview to the user — include any warnings from the pre-write checks
9. After the ad is created, **suggest sitelinks** if the campaign doesn't have any. Use `draft_sitelinks` with at least 4 relevant links (key pages like pricing, features, signup, etc.). Sitelinks increase ad real estate and CTR.
10. Wait for explicit user approval before calling `confirm_and_apply`

### When user wants to add keywords

1. Call `get_campaign_performance` to identify the target campaign and its **bidding strategy**
2. **Pre-write checks (CRITICAL):**
   - If the campaign uses MANUAL_CPC/MANUAL_CPM: ONLY use EXACT or PHRASE match. NEVER propose BROAD match. Explain why.
   - If the campaign has zero conversions: WARN that adding keywords won't help until conversion tracking is working.
   - If all existing keywords have quality score < 5: WARN that the problem is ad relevance and landing pages, not keyword coverage.
3. Call `get_keyword_performance` to see what keywords already exist — avoid duplicates
4. Call `get_search_terms` to understand what's already triggering ads
5. Call `draft_keywords` with appropriate match types (the tool will also warn about BROAD + non-Smart Bidding)
6. Present the preview with any warnings
7. Wait for explicit user approval

### When user wants to create a new campaign

1. Call `get_campaign_performance` to understand the existing campaign structure — avoid duplicate campaign names
2. If the user hasn't specified a budget, call `estimate_budget` with proposed keywords to get a data-driven budget recommendation
3. **Pre-write checks (CRITICAL):**
   - **Bidding strategy**: Default to MAXIMIZE_CONVERSIONS. It tells Google the goal is conversions (not just clicks), doesn't waste budget on non-converting clicks, and starts building the conversion model from day one — even with zero history, Google uses broad signals (search intent, device, time of day). Only use TARGET_SPEND/MANUAL_CPC if the user explicitly requests it and understands the trade-offs.
   - **Geo targeting**: ALWAYS ask the user which countries/regions to target if not specified. Never create a campaign without geo targets — untargeted campaigns waste budget on irrelevant geographies. Common IDs: 2276=Germany, 2040=Austria, 2756=Switzerland, 2840=USA, 2826=UK.
   - **Language targeting**: ALWAYS ask the user which languages to target if not specified. Language targeting restricts ads to users whose browser/Google language matches — without it, ads show to anyone in the geo region regardless of language. Common IDs: 1001=German, 1000=English, 1002=French.
   - Does the account have conversion tracking working? Call `attribution_check` — if zero conversions across the board, WARN that new campaigns won't help until tracking is fixed.
   - Is the proposed budget reasonable? Must be ≤ `max_daily_budget` in config, and ideally ≥ 5x target CPA.
4. Call `draft_campaign` with campaign name, daily budget, bidding strategy, `geo_target_ids`, `language_ids`, ad group name, and optional keywords
5. Review the preview and any `warnings` (budget sufficiency, MANUAL_CPC warning, BROAD match rejection)
6. Present the complete preview to the user — emphasize the campaign will be created as PAUSED, and confirm the geo/language targets are correct
7. After campaign creation, remind the user to:
   - Add ads via `draft_responsive_search_ad` (with display paths set)
   - Add sitelinks via `draft_sitelinks` (at least 4 recommended)
   - If the user needs multiple ad groups (e.g., different keyword themes), use `draft_ad_group` to add additional ad groups after the initial campaign is created and confirmed
   - Enable the campaign via `enable_entity` only after ads and sitelinks are in place
8. Wait for explicit user approval before calling `confirm_and_apply`

### When user wants to add an ad group to an existing campaign

1. Call `get_campaign_performance` to identify the target campaign and verify it exists
2. **Pre-write checks (CRITICAL):**
   - Check the campaign's bidding strategy — if MANUAL_CPC, only use EXACT or PHRASE match keywords
   - Check if conversion tracking is active (zero conversions + high spend = problem to fix first)
   - Check existing ad groups via `run_gaql`: `SELECT ad_group.id, ad_group.name FROM ad_group WHERE campaign.id = {campaign_id}` — avoid duplicate ad group names
3. Call `draft_ad_group` with `campaign_id`, `ad_group_name`, and optional `keywords`
4. Present the complete preview to the user
5. Wait for explicit user approval before calling `confirm_and_apply`
6. After the ad group is created, remind the user to add RSAs via `draft_responsive_search_ad` using the new `ad_group_id` from the result — an ad group without ads won't serve

### When user wants to change campaign settings (bid strategy, targeting, budget)

1. Call `get_campaign_performance` to identify the campaign and its current settings
2. Use `run_gaql` to check current targeting:
   - Geo: `SELECT campaign_criterion.location.geo_target_constant FROM campaign_criterion WHERE campaign.id = {id} AND campaign_criterion.type = 'LOCATION'`
   - Language: `SELECT campaign_criterion.language.language_constant FROM campaign_criterion WHERE campaign.id = {id} AND campaign_criterion.type = 'LANGUAGE'`
3. **Pre-write checks:**
   - If changing to MAXIMIZE_CONVERSIONS: good default choice — confirm with the user
   - If changing to MANUAL_CPC: warn about the trade-offs (no automation, requires constant monitoring)
   - If removing geo or language targets: warn that this broadens targeting and may waste budget
   - If the campaign is in a learning phase: warn that changes will restart the learning phase
4. Call `update_campaign` with only the parameters that need to change
5. Present the preview — clearly show what's changing (old → new)
6. Wait for explicit user approval before calling `confirm_and_apply`

### When user asks "how much should I spend" or "what budget do I need"

1. Ask the user for their target keywords (or suggest some based on the business context)
2. Ask for the target geography and language (or infer from the existing account)
3. Call `estimate_budget` with the keywords, match types, and optional daily budget
4. Present the forecast: estimated clicks, impressions, cost, and avg CPC
5. If the user provided a daily budget, highlight whether it's sufficient to capture most available traffic
6. Use the forecast to inform `draft_campaign` decisions — the estimated daily cost guides the budget parameter

### When user asks about tracking or event issues

1. **First, consider GDPR consent** — if Ads clicks > GA4 sessions, this is likely consent rejection, not broken tracking. State this before investigating further.
2. If the codebase is accessible, search for `gtag('event'` and `dataLayer.push` calls to extract event names. Also look for consent mode implementation (`gtag('consent', ...)`)
3. Call `validate_tracking` with the extracted event names — it compares codebase events against actual GA4 data and returns matched, missing, and unexpected events
4. Review the `insights[]` from `validate_tracking` — missing events indicate code not deployed or behind untriggered conditions; unexpected events may come from tag managers
5. If the user needs to add new tracking, use `generate_tracking_code` to produce the gtag snippet with recommended parameters

### When user asks to add negative keywords

1. Call `get_search_terms` to see current search term data
2. Call `get_negative_keywords` to see what's already blocked — avoid duplicates
3. Identify irrelevant terms that waste budget — group them by theme
4. Call `get_campaign_performance` to get the right `campaign.id`
5. Call `add_negative_keywords` with the proposed list and the campaign ID
6. Present preview and wait for confirmation

### When user asks to pause or enable something

1. Call the appropriate read tool to confirm the entity exists and get its current status
2. Call `pause_entity` or `enable_entity` with the entity type and ID
3. Present the preview with a clear warning about impact (e.g. "This will stop all ads in this campaign")
4. Wait for confirmation

### When user asks about landing page performance

1. Call `landing_page_analysis` — it combines ad final URLs with GA4 page data in one call
2. Review the `insights[]` for pages with traffic but zero conversions, high bounce rates, or orphaned URLs
3. If the codebase is accessible, read the flagged landing pages to identify UX or content issues
4. Present the results sorted by paid sessions, highlighting problem pages

### When user asks "is my tracking working" or "are conversions set up correctly"

1. Call `attribution_check` with `conversion_events` set to the expected events (e.g. `["sign_up", "purchase"]`)
2. The tool checks: do these events exist in GA4? Do they fire from paid traffic? Does Ads agree?
3. If the `insights[]` mention missing events or zero counts, search the codebase for tracking code and then call `validate_tracking` with the extracted event names for a structured comparison
4. If the `insights[]` mention GDPR consent gaps, explain that this is normal EU behavior, not broken tracking
5. If tracking code needs to be added, use `generate_tracking_code` to produce ready-to-paste gtag snippets with the right parameters

### When user asks "paid vs organic" or "which channel converts better"

1. Call `analyze_campaign_conversions` — it returns both paid campaign metrics and non-paid channel conversion rates
2. Compare `campaigns[].ga4_conversion_rate` (paid) vs `non_paid_channels[].conversion_rate` (organic/direct/referral)
3. If paid conversion rate is significantly lower, investigate landing page relevance and ad targeting before increasing spend

## Default Parameters

When the user doesn't specify:
- **Date range**: Default to last 30 days for Ads, last 7 days for GA4
- **Customer ID**: Use the default from config (no need to ask)
- **Property ID**: Use the default from config (no need to ask)
- **Format**: Use "table" for run_gaql results

## GAQL Quick Reference

GAQL (Google Ads Query Language) is SQL-like but with specific resource names and field paths.

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
| `customer_client` | List accounts under an MCC (uses login_customer_id) |

### Common Fields

**Campaign fields:**
- `campaign.id`, `campaign.name`, `campaign.status`
- `campaign.advertising_channel_type` (SEARCH, DISPLAY, SHOPPING, VIDEO)
- `campaign.bidding_strategy_type`

**Ad Group fields:**
- `ad_group.id`, `ad_group.name`, `ad_group.status`
- `ad_group.cpc_bid_micros` (bid in micros — divide by 1,000,000 for actual value)

**Ad fields:**
- `ad_group_ad.ad.responsive_search_ad.headlines`
- `ad_group_ad.ad.responsive_search_ad.descriptions`
- `ad_group_ad.ad.final_urls`
- `ad_group_ad.status`

**Keyword fields:**
- `ad_group_criterion.keyword.text`
- `ad_group_criterion.keyword.match_type` (EXACT, PHRASE, BROAD)
- `ad_group_criterion.quality_info.quality_score`

**Metrics (available on most resources):**
- `metrics.impressions`, `metrics.clicks`, `metrics.cost_micros`
- `metrics.conversions`, `metrics.conversions_value`
- `metrics.ctr`, `metrics.average_cpc`
- `metrics.search_impression_share`, `metrics.search_rank_lost_impression_share`

**Segments (for time-based breakdowns):**
- `segments.date` — daily breakdown
- `segments.device` — MOBILE, DESKTOP, TABLET
- `segments.ad_network_type` — SEARCH, CONTENT, YOUTUBE

### Date Ranges

```sql
WHERE segments.date DURING LAST_7_DAYS
WHERE segments.date DURING LAST_30_DAYS
WHERE segments.date DURING THIS_MONTH
WHERE segments.date DURING LAST_MONTH
WHERE segments.date BETWEEN '2026-01-01' AND '2026-01-31'
```

### Important GAQL Rules

- You CANNOT use `SELECT *` — every field must be named explicitly
- **Fields used in ORDER BY must appear in SELECT.** `ORDER BY metrics.cost_micros` will fail unless `metrics.cost_micros` is in your SELECT clause. This is the most common GAQL error.
- Metrics fields cannot appear in WHERE clauses with resource fields in the same query (use HAVING for post-filtering or filter in application logic)
- `cost_micros` values are in micros — divide by 1,000,000 for the actual currency amount. The dedicated read tools (get_campaign_performance, etc.) already compute `metrics.cost` and `metrics.cpa` for you. Only `run_gaql` returns raw micros.
- When selecting `segments.date`, results are broken down by day
- Status values are strings: `'ENABLED'`, `'PAUSED'`, `'REMOVED'`
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

## Ad Copy Character Limits

Google Ads enforces hard character limits. The `draft_responsive_search_ad` tool will reject copy that exceeds them, but you must write copy that fits on the FIRST attempt — do not generate copy and hope it fits.

**Hard limits:**
- Headlines: **30 characters** max (including spaces)
- Descriptions: **90 characters** max (including spaces)
- Display path fields: **15 characters** each (path1, path2)
- Sitelink link text: **25 characters** max
- Sitelink descriptions: **35 characters** max (description1, description2)

**30 characters is very short.** This is the most common source of rejected ad drafts. Many languages produce words and phrases that easily exceed 30 characters — German compound words, French/Spanish phrases with articles, etc. English is among the most compact languages for ad copy.

**Rules for writing ad copy that fits:**
1. **Count characters for every headline BEFORE calling the draft tool.** Every space, hyphen, accent, and punctuation mark counts as 1 character. If a headline is over 30, rewrite it shorter — don't submit it and hope.
2. **Abbreviate long words.** If a domain term exceeds ~15 characters, abbreviate it or use a shorter synonym. Save long terminology for descriptions (90 char limit).
3. **Use short action verbs and numbers.** Numbers, symbols, and abbreviations save space.
4. **Put long phrases in descriptions, not headlines.** Headlines are for punchy hooks. Descriptions have 3x the space for detail.
5. **Write in the correct language.** Check the landing page to determine the language. If the landing page is multilingual or the language is ambiguous, ASK the user which language the ad should be in before writing any copy.
6. **Test each headline mentally:** if it's close to 30, it's probably over. Aim for 25 or fewer to leave margin.

## Geo & Language Targeting Reference

### Common Geo Target IDs (geoTargetConstants)

| ID | Country |
|----|---------|
| 2276 | Germany |
| 2040 | Austria |
| 2756 | Switzerland |
| 2840 | United States |
| 2826 | United Kingdom |
| 2250 | France |
| 2380 | Italy |
| 2724 | Spain |
| 2528 | Netherlands |
| 2056 | Belgium |

For cities/regions, use `run_gaql` with `geo_target_constant` resource or look up IDs in the Google Ads API Geo Target documentation.

### Common Language IDs (languageConstants)

| ID | Language |
|----|----------|
| 1000 | English |
| 1001 | German |
| 1002 | French |
| 1003 | Italian |
| 1004 | Spanish |
| 1005 | Dutch |
| 1009 | Portuguese |
| 1014 | Polish |

## Marketing Best Practices

When advising on Google Ads:

- **Bid strategy default**: Prefer MAXIMIZE_CONVERSIONS for new campaigns. It leverages Google's signals from day one, even without conversion history. TARGET_SPEND (Maximize Clicks) only makes sense as a deliberate data-collection phase — and even then, MAXIMIZE_CONVERSIONS is usually better because it starts optimizing for conversions immediately instead of just accumulating clicks.
- **Geo targeting is mandatory**: Every campaign must target specific countries/regions. Untargeted campaigns serve globally and waste budget. ALWAYS set geo_target_ids when creating campaigns.
- **Language targeting is mandatory**: Every campaign must target specific languages. Without it, ads show to all users in the geo region regardless of browser language. A German-language ad shown to an English speaker in Germany wastes budget. ALWAYS set language_ids when creating campaigns.
- **Match types**: Never recommend Broad Match without Smart Bidding (tCPA or tROAS) active on the campaign. Broad Match without Smart Bidding leads to budget waste.
- **CPA monitoring**: Flag any campaign where CPA exceeds 3x the target for review.
- **Budget sufficiency**: A campaign's daily budget should be at least 5x its target CPA to generate enough data for the algorithm.
- **Learning phase**: Don't edit campaigns that are in an active learning phase (Google Ads shows "Learning" or "Learning (limited)" status). Wait until the learning phase completes before making changes.
- **Negative keyword hygiene**: After reviewing search terms, always suggest adding irrelevant terms as negatives. Group them by theme.
- **RSA best practices**: Provide at least 8-10 unique headlines (out of max 15) and 3-4 descriptions (out of max 4). Make headlines diverse — don't repeat the same message. Pin only when necessary. See "Ad Copy Character Limits" section below for language-specific guidance.
- **Quality Score**: If keywords have quality score < 5, prioritize improving ad relevance and landing page experience over bid increases.
- **Zero conversions**: When a campaign has spent significant budget with zero conversions, investigate (1) is GDPR consent reducing visible conversions? (2) is conversion tracking set up correctly in GA4? (3) is the landing page converting organic traffic? (4) are search terms relevant? Don't just increase budget.
- **Manual CPC + Broad Match**: This combination is the #1 cause of wasted budget. Broad Match without Smart Bidding matches any vaguely related query — a niche industry keyword on BROAD will match generic, irrelevant, and competitor terms. NEVER create this combination. If it already exists, recommend switching to PHRASE/EXACT match or moving the campaign to Smart Bidding BEFORE any other changes.
- **Display paths**: Always set `path1` and `path2` on RSAs. They cost nothing, improve ad relevance, and make the display URL informative (e.g. `example.com/Features/Pricing` instead of bare `example.com`). Derive them from the landing page path or the ad's core message. Max 15 chars each.
- **Sitelinks**: Every campaign should have at least 4 sitelinks. They increase ad real estate (more screen space = higher CTR), direct users to key pages, and are free. Good candidates: pricing, features, signup/trial, about, key product pages. Use `draft_sitelinks` to create them. Link text max 25 chars, descriptions max 35 chars each.
- **Clicks vs sessions gap**: Never report a clicks > sessions discrepancy as a tracking bug without first accounting for GDPR consent. In the EU, 30-70% of users may reject analytics cookies. This is normal, not broken.
