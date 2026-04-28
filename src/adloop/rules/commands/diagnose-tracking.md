---
description: Diagnose tracking and conversion issues across Google Ads and GA4
allowed-tools: ["mcp"]
---

Diagnose tracking and conversion issues for: $ARGUMENTS

## 1. GDPR check first

Before investigating anything technical, consider:
- Are Ads clicks > GA4 sessions? This is likely GDPR consent rejection (normal in EU, 2:1 to 5:1 ratio)
- State this upfront before deeper investigation

## 2. Attribution check

- Run `attribution_check` with relevant `conversion_events` (e.g., sign_up, purchase, form_submit)
- Review the `insights[]` — the tool already factors in GDPR consent gaps
- Only proceed to deeper investigation if insights suggest a real tracking problem

## 3. Codebase analysis

- Search for `gtag('event'` and `dataLayer.push({event:` to find tracking code
- Search for `gtag('consent'` to check Consent Mode v2 implementation
- Extract all event names from code

## 4. Validate tracking

- Run `validate_tracking` with the extracted event names
- Compare results: matched (working), missing (in code but not firing), unexpected (in GA4 but not in code)
- Missing events = code not deployed or behind untriggered conditions
- Unexpected events = likely from tag managers

## 5. Landing page check

- Run `landing_page_analysis` to check pages with traffic but zero conversions
- If codebase is accessible, read flagged pages for UX issues

## 6. Diagnosis

- Only diagnose tracking as BROKEN when discrepancy can't be explained by consent
- Signs of real issues: zero sessions for ALL sources, organic also anomalous, events in code but never fire
- If tracking code needs to be added, use `generate_tracking_code` to produce the snippet

Present unified diagnosis: GDPR impact + tracking status + specific recommendations.
