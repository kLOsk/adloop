---
name: adloop-tracking
description: AdLoop tracking diagnosis and code generation — diagnosing GA4 event tracking issues, verifying conversion tracking, generating gtag event snippets, and checking GDPR consent mode. Use this skill whenever the user asks about tracking problems, conversion tracking, GA4 events not firing, tracking verification, consent mode, or wants to generate tracking code. Also use when the user says "is my tracking working", "tracking issues", "event not firing", "add event tracking", "GDPR tracking", "consent mode", "attribution gap", or any question about whether their analytics/tracking setup is correct.
---

# AdLoop — Tracking Diagnosis & Code Generation

You have access to AdLoop MCP tools for diagnosing GA4 tracking issues, verifying conversion tracking, and generating gtag event snippets. This skill covers the complete tracking workflow: from detecting GDPR consent gaps to generating ready-to-paste tracking code.

## Tool Inventory

| Tool | Key Params | When to Use |
|------|-----------|------------|
| `validate_tracking` | `expected_events` (list of event names), `property_id`, `date_range_start`, `date_range_end` | Compare codebase events against actual GA4 data — find missing/broken tracking |
| `generate_tracking_code` | `event_name`, `event_params` (optional dict), `trigger` (optional: form_submit/button_click/page_load), `property_id`, `check_existing` (default true) | Generate a ready-to-paste GA4 gtag JavaScript snippet |
| `attribution_check` | `customer_id`, `property_id`, `date_range_start`, `date_range_end`, `conversion_events` (optional list) | Compare Ads-reported conversions vs GA4 — find tracking discrepancies |
| `get_tracking_events` | `property_id`, `date_range_start`, `date_range_end` | List all GA4 events and their volume (sorted descending by count) |

## Orchestration Patterns

### Pattern 1: "Is my tracking working?" (Tracking Verification)

This is the most common tracking question. Follow this exact sequence:

1. **Consider GDPR consent first** — If Ads clicks are significantly higher than GA4 sessions, this is likely consent rejection, not broken tracking. A 2:1 to 5:1 ratio (clicks:sessions) is normal with EU consent banners. State this before investigating further.

2. **Codebase search** — If the codebase is accessible, search for:
   - `gtag('event'` — find all GA4 event calls
   - `dataLayer.push({event:` — find tag manager events
   - `gtag('consent'` — find consent mode implementation
   Extract the event names from these calls.

3. **Call `attribution_check`** with relevant date range and `conversion_events` if the user mentions specific events (e.g., `["sign_up", "purchase"]`). This does Ads vs GA4 comparison in one call and auto-generates insights.

4. **Call `validate_tracking`** with the event names found in the codebase. It returns:
   - `matched`: Events found in both code and GA4 (with their count)
   - `missing_from_ga4`: Events in code but NOT firing in GA4
   - `unexpected_in_ga4`: Events in GA4 but NOT in the expected list
   - `auto_collected`: GA4 automatic events (not from your code)
   - `insights[]`: Actionable diagnosis

5. **If tracking code needs to be added**, use `generate_tracking_code` to produce the gtag snippet with recommended parameters.

6. **Present a unified diagnosis** combining the attribution check insights with the tracking validation results.

### Pattern 2: Tracking/Event Issues (Diagnosing Specific Problems)

1. **If the user reports specific events not firing:**
   - Call `get_tracking_events` to see all events and their counts
   - Check if the event name exists in GA4 at all
   - If it exists but has 0 count: the event implementation may be conditional or broken
   - If it doesn't exist: the tracking code may not be deployed or there's a typo

2. **If the user reports conversion discrepancies:**
   - Call `attribution_check` with `conversion_events` set to the relevant event names
   - Review `insights[]` for specific diagnosis (GDPR gaps, attribution model mismatches, missing events)

3. **If the user needs to add new tracking:**
   - Call `generate_tracking_code` with the desired event name
   - If it's a well-known event (purchase, sign_up, etc.), the tool auto-fills recommended parameters
   - Choose the appropriate trigger: `form_submit`, `button_click`, or `page_load`
   - The tool checks if the event already exists in GA4 and warns about duplicates

## GA4 Auto-Collected Events

These events are automatically collected by GA4 and do NOT need manual implementation. If you see them in the codebase or in `unexpected_in_ga4`, that's normal:

`page_view`, `session_start`, `first_visit`, `user_engagement`, `scroll`, `click`, `file_download`, `video_start`, `video_progress`, `video_complete`, `view_search_results`, `form_start`, `form_submit`

## GA4 Recommended Event Parameters

For well-known events, `generate_tracking_code` auto-fills these parameters. When the user asks about a specific event, these are the recommended parameters:

| Event | Recommended Parameters |
|-------|----------------------|
| `sign_up` | `method: string` (e.g., "Google", "Email") |
| `login` | `method: string` |
| `purchase` | `transaction_id: string, value: number, currency: string, items: array` |
| `begin_checkout` | `value: number, currency: string, items: array` |
| `add_to_cart` | `value: number, currency: string, items: array` |
| `generate_lead` | `value: number, currency: string` |
| `select_content` | `content_type: string, content_id: string` |
| `share` | `method: string, content_type: string, item_id: string` |
| `search` | `search_term: string` |
| `view_item` | `items: array` |

## Trigger Templates

`generate_tracking_code` supports three triggers. Each wraps the gtag call in a DOM event listener:

**`form_submit`** — Fires when a specific form is submitted:
```javascript
document.querySelector('{selector}').addEventListener('submit', function() {
  gtag('event', 'event_name', {...params});
});
```

**`button_click`** — Fires when a specific button is clicked:
```javascript
document.querySelector('{selector}').addEventListener('click', function() {
  gtag('event', 'event_name', {...params});
});
```

**`page_load`** — Fires when the page loads:
```javascript
document.addEventListener('DOMContentLoaded', function() {
  gtag('event', 'event_name', {...params});
});
```

When using a trigger, replace `{selector}` with the actual CSS selector for the form or button element.

## GDPR Consent & Data Discrepancies

This is critical context for tracking diagnosis. Most EU websites use a GDPR cookie consent banner:

- **Google Ads counts all clicks** regardless of consent. A click is a click.
- **GA4 only records sessions for users who accept analytics cookies.** Users who reject are invisible.
- **A clicks-to-sessions ratio of 2:1 to 5:1 is normal** with consent banners. This is NOT a tracking bug.
- **Conversion events in GA4 are also affected** — true conversion rates are likely higher than GA4 reports.

**Before diagnosing broken tracking:**
1. If Ads shows 10 clicks but GA4 shows 3 sessions → likely consent rejection, not broken tracking.
2. If GA4 shows 0 sessions from paid traffic → consent could explain it, but also check UTM parameters and GA4 filters.
3. Only flag tracking as broken when the discrepancy cannot be explained by consent (e.g., GA4 shows zero for ALL traffic sources, or organic traffic also shows anomalies).

**Google Consent Mode v2:** Some sites send cookieless pings to GA4 even without consent. This reduces (but doesn't eliminate) the gap. If you see `gtag('consent', ...)` in the codebase, Consent Mode is active — GA4 data will be closer to Ads data.

**Common tracking problems and their solutions:**

| Symptom | Likely Cause | Solution |
|----------|-------------|---------|
| Events in code but missing from GA4 | Not deployed, conditional trigger, or GDPR blocking | Check deployment; verify event triggers only for consenting users |
| Ads reports conversions but GA4 shows 0 from paid | Attribution model mismatch or GDPR | Check if GA4 uses data-driven attribution; verify consent mode |
| GA4 has conversions but Ads shows 0 | Conversion actions not imported into Ads | Import GA4 conversion events as Google Ads conversion actions |
| Click-to-session ratio > 5:1 | Severe consent rejection or tracking misconfiguration | Verify gtag.js is loaded; check for ad blockers; verify property ID |
| Event fires in GA4 but conversion not counted | Event not marked as conversion in GA4 Admin | Mark the event as a conversion in GA4 Admin → Events |
| Events have count but are all from paid traffic | Landing page loads gtag but consent is only given via ad click | This is actually normal for ad traffic; check organic traffic separately |

## How to Mark Events as Conversions

After generating tracking code or validating that events fire correctly, the user needs to mark key events as conversions in GA4 Admin:

1. Go to GA4 → Admin → Events
2. Find the event (e.g., `purchase`, `sign_up`)
3. Toggle "Mark as conversion" to ON
4. The event will appear in Conversions within 24 hours
5. For Google Ads conversion tracking: import the conversion event in Google Ads → Tools → Conversions → Add → Import from GA4

## Default Parameters

When the user doesn't specify:
- **Date range**: Default to last 28 days for tracking analysis (wider window catches more events)
- **Property ID**: Use the default from config
- **Customer ID**: Use the default from config
- **check_existing** in `generate_tracking_code`: Default is `true` — the tool will check if the event already fires in GA4