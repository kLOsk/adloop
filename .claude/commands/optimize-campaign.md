---
description: Full optimization checklist for a Google Ads campaign
allowed-tools: ["mcp"]
---

Optimize Google Ads campaign: $ARGUMENTS

Follow this checklist in order — earlier items have higher impact.

**Context tip:** run the diagnosis reads with `compact=true` (totals + top offenders instead of full row dumps); switch to full mode only when drafting a change against a specific entity.

## 1. Diagnose

- `get_campaign_performance` — current metrics
- `attribution_check` — is tracking working?
- If zero conversions + significant spend: STOP and resolve tracking first

## 2. Search term cleanup

- `get_search_terms` — identify irrelevant terms wasting budget
- `get_negative_keywords` — what's already blocked (avoid duplicates)
- Propose negatives with `add_negative_keywords` (show preview, wait for approval)
- Group negatives by theme for clarity

## 3. Quality Score

- `get_keyword_performance` — check QS for each keyword
- QS < 5 = relevance problem (landing page + ad copy mismatch)
- Suggest specific improvements to ad copy or landing page

## 4. Ad copy

- `get_ad_performance` — which ads perform best
- If CTR < 2% across all ads: headlines need rewriting
- If fewer than 3 active ads: create new ones with `draft_responsive_search_ad`
- Count characters before drafting (30 char headline limit)

## 5. Bidding strategy

- Check if campaign uses Smart Bidding or Manual CPC
- Manual CPC + Broad Match: MIGRATE to Phrase/Exact OR switch to Smart Bidding
- Recommend Maximize Conversions if sufficient conversion data exists

## 6. Budget

- Is daily budget >= 5x target CPA? (AdLoop's rule of thumb, not a Google requirement; Google's low-traffic guide suggests 2 to 3 times)
- If not: either increase budget or focus on reducing CPA first
- Use `estimate_budget` if considering budget changes

## Rules

- One change at a time — show preview, wait for approval
- Google's "Learning" status (up to 7 days) follows a new or reactivated bid strategy, a change in what the strategy covers, or a significant conversion-goal change. Budget, target, creative and targeting changes never trigger it. Avoid several target changes within one conversion cycle
- Priority order: tracking > negatives > QS > ad copy > bidding > budget
