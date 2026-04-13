---
name: adloop-write
description: AdLoop campaign and ad management — creating, updating, and modifying Google Ads campaigns, ad groups, ads, keywords, extensions, and entities. Use this skill whenever the user wants to create a campaign, create an ad, add keywords, add negative keywords, pause or enable an entity, change budget or bidding, create sitelinks, callouts, structured snippets, or apply any mutation to Google Ads. Also use when the user says "draft ad", "apply changes", "remove campaign", "update bidding", "add extensions", or any write/create/update operation on Google Ads. This skill covers all 16 write tools plus confirm_and_apply, the two-step write pattern, complete safety rules, and 7 orchestration patterns.
---

# AdLoop — Campaign & Ad Management (Mutations)

You have access to AdLoop MCP tools that can create and modify Google Ads campaigns, ad groups, keywords, ads, and extensions. These are powerful tools that spend real money — safety is critical.

## Two-Step Write Pattern (CRITICAL)

Every write operation follows this two-step pattern:

1. **Draft**: Call a `draft_*`, `update_*`, `pause_entity`, `enable_entity`, or `remove_entity` tool → returns a preview with a `plan_id`
2. **Confirm & Apply**: Call `confirm_and_apply(plan_id=..., dry_run=true)` first to test, then only with `dry_run=false` after explicit user confirmation

**Never skip step 2.** Never call `confirm_and_apply` without showing the preview to the user first.

## Safety Rules (CRITICAL — always follow)

1. **NEVER call confirm_and_apply without showing the preview to the user first.** Always present the full preview and wait for explicit approval.

2. **Default to dry_run=true.** When calling confirm_and_apply, always use `dry_run=true` unless the user explicitly says to apply for real. Even then, `require_dry_run` in config may override this.

3. **Respect budget caps.** The config has `max_daily_budget` (default 50.0). Never propose a campaign budget above this.

4. **Double-check destructive operations.** For any pause, enable, remove, or budget change, explicitly warn about impact. `remove_entity` is IRREVERSIBLE — always prefer `pause_entity`.

5. **One change at a time.** Don't batch multiple write operations. Draft one change, get approval, apply, then move to the next.

6. **Never guess entity IDs.** Always retrieve IDs from a read tool first (`get_campaign_performance` for campaigns, `get_ad_performance` for ads, etc.) before passing them to write tools.

7. **NEVER add BROAD match keywords without verifying Smart Bidding.** Before calling `draft_keywords` with BROAD match, ALWAYS check the campaign's bidding strategy via `get_campaign_performance` or `run_gaql`. If the campaign uses MANUAL_CPC or any non-Smart Bidding strategy, REFUSE to add BROAD match. Use PHRASE or EXACT instead.

8. **NEVER create ads or sitelinks with unverified URLs.** Every `final_url` in an RSA and every sitelink URL MUST point to a real, working page. Check the codebase for route definitions or confirm the URLs are live before drafting.

9. **Pre-write validation: check before you change.** Before ANY write operation:
   - Check bidding strategy (rule 7)
   - Check if conversion tracking is active (zero conversions + high spend = fix tracking first, not add ads)
   - Check quality scores (if all keywords have QS < 5, the problem is relevance, not keyword count)
   - If the account has systemic issues, WARN the user before making changes

## Write Tool Inventory

### Campaign & Ad Group Creation

| Tool | Required Params | Key Optional Params |
|------|----------------|---------------------|
| `draft_campaign` | `campaign_name`, `daily_budget`, `bidding_strategy`, `geo_target_ids`, `language_ids` | `target_cpa`, `target_roas`, `channel_type` (default "SEARCH"), `ad_group_name`, `keywords` (list of {text, match_type}), `search_partners_enabled`, `display_network_enabled`, `display_expansion_enabled`, `max_cpc` |
| `draft_ad_group` | `campaign_id`, `ad_group_name` | `keywords` (list of {text, match_type}), `cpc_bid_micros` |
| `update_campaign` | `campaign_id` | `bidding_strategy`, `daily_budget`, `target_cpa`, `target_roas`, `geo_target_ids`, `language_ids`, `search_partners_enabled`, `display_network_enabled`, `display_expansion_enabled`, `max_cpc` |
| `update_ad_group` | `ad_group_id` | `ad_group_name`, `max_cpc` |

### Ad Creation

| Tool | Required Params | Validation Rules |
|------|----------------|-----------------|
| `draft_responsive_search_ad` | `ad_group_id`, `headlines` (3-15 items), `descriptions` (2-4 items), `final_url` | Headlines ≤30 chars, descriptions ≤90 chars, path1/path2 ≤15 chars |

### Extensions & Assets

| Tool | Required Params | Notes |
|------|----------------|-------|
| `draft_callouts` | `campaign_id`, `callouts` (list of strings) | Each callout ≤25 chars |
| `draft_structured_snippets` | `campaign_id`, `snippets` (list of {header, values}) | Header must be one of the 13 official values, 3-10 values per snippet, each value ≤25 chars |
| `draft_image_assets` | `campaign_id`, `image_paths` (list of local file paths) | PNG, JPEG, or GIF only |
| `draft_sitelinks` | `campaign_id`, `sitelinks` (list of {link_text, final_url, description1, description2}) | link_text ≤25 chars, descriptions ≤35 chars, at least 4 sitelinks recommended |

### Keywords & Negatives

| Tool | Required Params | Notes |
|------|----------------|-------|
| `draft_keywords` | `ad_group_id`, `keywords` (list of {text, match_type}) | match_type: EXACT, PHRASE, or BROAD. BROAD requires Smart Bidding. |
| `add_negative_keywords` | `campaign_id`, `keywords` (list of strings) | match_type default: EXACT |
| `propose_negative_keyword_list` | `campaign_id`, `list_name`, `keywords` (list of strings) | Creates a shared list + attaches to campaign. Check existing lists first with `get_negative_keyword_lists`. |

### Status Changes & Removal

| Tool | Required Params | Notes |
|------|----------------|-------|
| `pause_entity` | `entity_type`, `entity_id` | entity_type: campaign, ad_group, ad, keyword |
| `enable_entity` | `entity_type`, `entity_id` | entity_type: campaign, ad_group, ad, keyword |
| `remove_entity` | `entity_type`, `entity_id` | IRREVERSIBLE. entity_type: campaign, ad_group, ad, keyword, negative_keyword, campaign_asset, asset, customer_asset. Always prefer pause_entity. |

### Execution

| Tool | Required Params | Notes |
|------|----------------|-------|
| `confirm_and_apply` | `plan_id` | dry_run defaults to true. Set dry_run=false only after explicit user approval. |

## Key Validation Details

### Bidding Strategies

| Strategy | Smart? | Notes |
|----------|--------|-------|
| MAXIMIZE_CONVERSIONS | Yes | Can set optional `target_cpa`. Best default for new campaigns. |
| MAXIMIZE_CONVERSION_VALUE | Yes | Can set optional `target_roas` |
| TARGET_CPA | Yes | Alias for MAXIMIZE_CONVERSIONS with target |
| TARGET_ROAS | Yes | Alias for MAXIMIZE_CONVERSION_VALUE with target |
| TARGET_SPEND | No | Can set `max_cpc` as bid ceiling on the campaign |
| MANUAL_CPC | No | **Never combine with BROAD match.** `max_cpc` sets the ad-group initial bid. |

### Ad Copy Character Limits (count every character — spaces count!)

| Element | Max Length |
|---------|-----------|
| RSA Headline | 30 chars |
| RSA Description | 90 chars |
| Display Path (path1, path2) | 15 chars each |
| Sitelink Link Text | 25 chars |
| Sitelink Description | 35 chars each |
| Callout | 25 chars |
| Structured Snippet Value | 25 chars |

30 characters is very short. Many languages produce words/phrases that easily exceed this. **Count characters for every headline BEFORE calling the draft tool.** If a headline exceeds 30, rewrite it. Use short action verbs and numbers. Put long phrases in descriptions (90 chars).

### Structured Snippet Valid Headers

Only these are accepted: Amenities, Brands, Courses, Degree programs, Destinations, Featured Hotels, Insurance coverage, Models, Neighborhoods, Services, Shows, Styles, Types

### Entity Types for Pause/Enable/Remove

- **Pause/Enable:** campaign, ad_group, ad, keyword
- **Remove (irreversible):** campaign, ad_group, ad, keyword, negative_keyword, campaign_asset, asset, customer_asset

### Geo Target IDs (common)

| Country | ID | Country | ID |
|---------|------|---------|------|
| Germany | 2276 | United States | 2840 |
| Austria | 2040 | United Kingdom | 2826 |
| Switzerland | 2756 | France | 2250 |
| Spain | 2724 | Netherlands | 2528 |
| Italy | 2380 | Belgium | 2056 |

### Language IDs (common)

| Language | ID | Language | ID |
|----------|------|----------|------|
| English | 1000 | German | 1001 |
| French | 1002 | Italian | 1004 |
| Spanish | 1003 | Dutch | 1005 |
| Portuguese | 1009 | Polish | 1014 |

### Important Behaviors

- **New campaigns and RSAs are created as PAUSED** — the user must explicitly enable them after review.
- **`draft_campaign` REQUIRES `geo_target_ids` and `language_ids`** — campaigns without targeting waste budget.
- **`draft_campaign` enforces `max_daily_budget` safety cap** and warns if budget < 5x target CPA.
- **`display_network_enabled` is the canonical flag.** `display_expansion_enabled` is a compatibility alias.
- **`update_campaign` replaces geo/language targets entirely** (not append). Pass the full desired list.
- **`update_ad_group` is for MANUAL_CPC bid changes.** Use `update_campaign` for TARGET_SPEND `max_cpc` changes.
- **`require_dry_run: true` in config overrides `dry_run=false`** — the user must change config to allow real mutations.
- **All operations (including dry runs) are logged** to `~/.adloop/audit.log`.

## Orchestration Patterns

### Pattern 1: Create a New Campaign

1. Call `get_campaign_performance` to understand existing structure — avoid duplicate names
2. If the user hasn't specified a budget, suggest using `estimate_budget` (from adloop-planning skill) with proposed keywords
3. **Pre-write checks (CRITICAL):**
   - **Bidding strategy**: Default to MAXIMIZE_CONVERSIONS. It starts building the conversion model from day one. Only use TARGET_SPEND/MANUAL_CPC if the user explicitly requests it and understands the trade-offs.
   - **Geo targeting**: ALWAYS ask which countries/regions. Never create untargeted campaigns.
   - **Language targeting**: ALWAYS ask which languages. Language targeting prevents ads from showing to non-speakers.
   - Does conversion tracking work? Call `attribution_check` — if zero conversions, WARN that new campaigns won't help until tracking is fixed.
   - Is budget reasonable? Must be ≤ `max_daily_budget` in config, ideally ≥ 5x target CPA.
4. Call `draft_campaign` with campaign name, budget, bidding strategy, geo/language targets, optional keywords
5. Review the preview and any `warnings`
6. Present the complete preview. Emphasize the campaign is created as PAUSED.
7. After creation, remind user to:
   - Add ads via `draft_responsive_search_ad` (with display paths)
   - Add sitelinks via `draft_sitelinks` (at least 4)
   - Add additional ad groups via `draft_ad_group` if needed
   - Enable via `enable_entity` only after ads and sitelinks are in place
8. Wait for explicit approval before calling `confirm_and_apply`

### Pattern 2: Create a Responsive Search Ad

1. Call `get_campaign_performance` to find the target campaign
2. **Pre-write checks (CRITICAL):**
   - Is the campaign's bidding strategy appropriate? MANUAL_CPC should NOT get more ads before fixing bidding.
   - Does the campaign have conversions? Zero conversions + significant spend = fix tracking first.
   - What are the quality scores? All below 5 = fix relevance first, don't add more ads.
3. Use `run_gaql` to find ad group IDs: `SELECT ad_group.id, ad_group.name FROM ad_group WHERE campaign.id = {campaign_id}`
4. Read the landing page to extract value propositions and determine the language. **Verify the final_url page exists** — check route definitions or confirm the URL is live. NEVER use an unverified URL.
5. Call `draft_responsive_search_ad` with 8-10 diverse headlines and 3-4 descriptions. Write copy in the correct language. Follow the character limits above — **count every character before drafting**.
6. Always set display paths (`path1`, `path2`, max 15 chars each). Derive from landing page URL or ad value proposition.
7. Present the complete preview with any warnings from pre-write checks
8. After the ad is created, suggest sitelinks via `draft_sitelinks` (at least 4)
9. Wait for explicit approval

### Pattern 3: Add Keywords

1. Call `get_campaign_performance` to identify the target campaign and its **bidding strategy**
2. **Pre-write checks (CRITICAL):**
   - MANUAL_CPC/MANUAL_CPM: ONLY use EXACT or PHRASE match. NEVER propose BROAD.
   - Zero conversions: WARN that adding keywords won't help until tracking works.
   - All keywords QS < 5: WARN that the problem is relevance, not keyword count.
3. Call `get_keyword_performance` to check existing keywords — avoid duplicates
4. Call `get_search_terms` to understand current search intent
5. Call `draft_keywords` with appropriate match types
6. Present the preview with any warnings
7. Wait for explicit approval

### Pattern 4: Add an Ad Group to an Existing Campaign

1. Call `get_campaign_performance` to identify the target campaign
2. Check the campaign's bidding strategy — if MANUAL_CPC, only use EXACT or PHRASE keywords
3. Check for duplicate ad group names via `run_gaql`: `SELECT ad_group.id, ad_group.name FROM ad_group WHERE campaign.id = {campaign_id}`
4. Call `draft_ad_group` with `campaign_id`, `ad_group_name`, and optional `keywords`
5. Present the complete preview
6. After creation, remind user to add RSAs via `draft_responsive_search_ad` — an ad group without ads won't serve
7. Wait for explicit approval

### Pattern 5: Change Campaign Settings (bidding, budget, targeting)

1. Call `get_campaign_performance` to identify the campaign and its current settings
2. Use `run_gaql` to check current targeting:
   - Geo: `SELECT campaign_criterion.location.geo_target_constant FROM campaign_criterion WHERE campaign.id = {id} AND campaign_criterion.type = 'LOCATION'`
   - Language: `SELECT campaign_criterion.language.language_constant FROM campaign_criterion WHERE campaign.id = {id} AND campaign_criterion.type = 'LANGUAGE'`
3. **Pre-write checks:**
   - Changing to MAXIMIZE_CONVERSIONS: good default — confirm with user
   - Changing to MANUAL_CPC: warn about trade-offs (no automation, needs constant monitoring)
   - Removing geo/language targets: broadens targeting, may waste budget
   - Campaign in learning phase: warn that changes restart the learning phase
4. Call `update_campaign` with only the parameters that need to change
5. Present the preview — clearly show what's changing (old → new)
6. Wait for explicit approval

### Pattern 6: Add Negative Keywords

1. Call `get_search_terms` to see current search term data
2. Call `get_negative_keywords` to see what's already blocked — avoid duplicates
3. Identify irrelevant terms that waste budget — group them by theme
4. Call `get_campaign_performance` to get the right `campaign.id`
5. Choose the right write tool:
   - **Direct campaign negatives** (`add_negative_keywords`): faster, campaign-specific, no reuse
   - **Shared negative keyword list** (`propose_negative_keyword_list`): reusable across campaigns — prefer when the user wants a list or mentions reusability
6. **Before `propose_negative_keyword_list`**, always call `get_negative_keyword_lists` first to check existing lists. If a matching list exists, inspect via `get_negative_keyword_list_keywords` and `get_negative_keyword_list_campaigns` — it may just need attaching to a new campaign rather than recreating.
7. Present preview and wait for confirmation

### Pattern 7: Pause or Enable an Entity

1. Call the appropriate read tool to confirm the entity exists and get its current status
2. Call `pause_entity` or `enable_entity` with the entity type and ID
3. Present the preview with a clear warning about impact (e.g. "This will stop all ads in this campaign")
4. Wait for confirmation

## Pre-Write Validation Checklist

Before ANY write operation, always verify:

- [ ] **Bidding strategy**: If adding BROAD match, the campaign MUST use Smart Bidding (MAXIMIZE_CONVERSIONS, TARGET_CPA, TARGET_ROAS, MAXIMIZE_CONVERSION_VALUE). On MANUAL_CPC, only EXACT and PHRASE are allowed.
- [ ] **Conversion tracking**: If the campaign has zero conversions with significant spend, WARN the user. Adding ads/keywords to a campaign with broken tracking wastes money.
- [ ] **Quality scores**: If all keywords have QS < 5, the problem is ad relevance and landing pages. More keywords won't help.
- [ ] **Budget**: Must be ≤ `max_daily_budget` in config. Ideally ≥ 5x target CPA.
- [ ] **Entity IDs**: Never guess — always look up with a read tool first.
- [ ] **URLs**: Every final_url must point to a real, working page.

## Marketing Best Practices for Writes

- **Default to MAXIMIZE_CONVERSIONS** for new campaigns. It starts optimizing for conversions immediately, even without history.
- **Always set display paths** on RSAs (`path1`, `path2`). They're free, improve relevance, and make display URLs informative.
- **Provide 8-10 diverse headlines** for RSAs (out of max 15). Don't repeat the same message. Pin only when necessary.
- **Provide 3-4 descriptions** for RSAs (out of max 4).
- **Add at least 4 sitelinks** to every campaign. They increase ad real estate and CTR.
- **Negative keyword hygiene**: After reviewing search terms, always suggest adding irrelevant terms as negatives. Group by theme.
- **Learning phase**: Don't edit campaigns that are in an active learning phase. Wait until it completes before making changes.
- **Manual CPC + Broad Match**: The #1 cause of wasted budget. NEVER create this combination.