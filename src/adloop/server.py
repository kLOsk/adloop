"""AdLoop MCP server — FastMCP instance with all tool registrations."""

from __future__ import annotations

import functools
from typing import Callable

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from adloop import _mcp_patches, diagnostics
from adloop.config import load_config

diagnostics.install()
_mcp_patches.install()

_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

mcp = FastMCP(
    "AdLoop",
    instructions=(
        "AdLoop connects Google Ads and Google Analytics (GA4) data to your "
        "codebase. Use the read tools to analyze performance, and the write "
        "tools (with safety confirmation) to manage campaigns."
    ),
)

_config = load_config()


def _structured_error(fn_name: str, exc: Exception) -> dict:
    """Translate common auth failures into actionable structured errors."""
    err = str(exc)
    err_lower = err.lower()

    if "developer_token_not_approved" in err_lower or "only approved for use with test accounts" in err_lower:
        return {
            "error": (
                "Google Ads authorization failed — developer token is not "
                "approved for production accounts."
            ),
            "hint": (
                "This developer token can only access Google Ads test accounts. "
                "Apply for Basic or Standard access in the Google Ads API Center, "
                "or switch AdLoop to a test account."
            ),
            "auth_error": "DEVELOPER_TOKEN_NOT_APPROVED",
        }

    if "developer_token_invalid" in err_lower or "developer token is not valid" in err_lower:
        return {
            "error": "Google Ads authentication failed — developer token is invalid.",
            "hint": (
                "Update `ads.developer_token` in `~/.adloop/config.yaml` with "
                "the token from your Google Ads manager account API Center. "
                "OAuth is working if GA4 tools succeed."
            ),
            "auth_error": "DEVELOPER_TOKEN_INVALID",
        }

    if "invalid_grant" in err_lower or "revoked" in err_lower:
        return {
            "error": "Authentication failed — OAuth token expired or revoked.",
            "hint": (
                "Delete ~/.adloop/token.json and re-run any tool to "
                "trigger re-authorization. If this keeps happening, "
                "publish the GCP consent screen to 'In production'."
            ),
            "auth_error": "INVALID_GRANT",
        }

    if "statuscode.unauthenticated" in err_lower:
        return {
            "error": "Authentication failed — Google rejected the request as unauthenticated.",
            "hint": (
                "If GA4 tools work but Ads tools fail, check `ads.developer_token`. "
                "Otherwise delete ~/.adloop/token.json and re-run any tool to "
                "trigger re-authorization."
            ),
            "details": err,
        }

    return {"error": err, "tool": fn_name}


def _safe(fn: Callable) -> Callable:
    """Wrap a tool function so exceptions return structured error dicts.

    When ``ADLOOP_DEBUG`` is set, the resulting callable is additionally
    instrumented via :mod:`adloop.diagnostics` to emit tool_start/tool_end
    events and update the last-activity timestamp.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as e:
            return {"error": str(e)}
        except Exception as e:
            return _structured_error(fn.__name__, e)

    return diagnostics.wrap_tool(wrapper)

# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def health_check() -> dict:
    """Test AdLoop connectivity — checks OAuth token, GA4 API, and Google Ads API.

    Run this first if other tools are failing. Returns status for each service
    and actionable guidance if something is broken.
    """
    from adloop.ads.client import GOOGLE_ADS_API_VERSION

    status = {
        "ga4": "unknown",
        "ads": "unknown",
        "config": "ok",
        "google_ads_api_version": GOOGLE_ADS_API_VERSION,
    }

    try:
        from google.ads.googleads.client import _DEFAULT_VERSION
        if _DEFAULT_VERSION != GOOGLE_ADS_API_VERSION:
            status["ads_version_note"] = (
                f"AdLoop is pinned to {GOOGLE_ADS_API_VERSION} but the "
                f"google-ads library defaults to {_DEFAULT_VERSION}. "
                f"A newer API version is available — update "
                f"GOOGLE_ADS_API_VERSION in ads/client.py when ready to migrate."
            )
    except ImportError:
        pass

    try:
        from adloop.ga4.reports import get_account_summaries as _ga4_test

        result = _ga4_test(_config)
        status["ga4"] = "ok"
        status["ga4_properties"] = result.get("total_properties", 0)
    except Exception as e:
        parsed = _structured_error("health_check", e)
        status["ga4"] = "error"
        status["ga4_error"] = parsed["error"]
        if "hint" in parsed:
            status["ga4_hint"] = parsed["hint"]
        if "auth_error" in parsed:
            status["ga4_auth_error"] = parsed["auth_error"]
        if "details" in parsed:
            status["ga4_error_details"] = parsed["details"]

    try:
        from adloop.ads.gaql import execute_query

        # Minimal probe — one row is enough to confirm OAuth, developer token,
        # and API reachability. We deliberately avoid enumerating customer_client
        # here: on large MCCs (100+ accounts) that call can take multiple seconds
        # and its size/latency is the likely culprit when the MCP host kills the
        # connection shortly after health_check. Call list_accounts explicitly
        # if a count or listing is actually needed.
        mcc_id = _config.ads.login_customer_id or _config.ads.customer_id
        execute_query(
            _config,
            mcc_id,
            "SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1",
        )
        status["ads"] = "ok"
    except Exception as e:
        parsed = _structured_error("health_check", e)
        status["ads"] = "error"
        status["ads_error"] = parsed["error"]
        if "hint" in parsed:
            status["ads_hint"] = parsed["hint"]
        if "auth_error" in parsed:
            status["ads_auth_error"] = parsed["auth_error"]
        if "details" in parsed:
            status["ads_error_details"] = parsed["details"]

    if status["ga4"] == "error" or status["ads"] == "error":
        if status.get("ads_hint"):
            status["hint"] = status["ads_hint"]
        elif status.get("ga4_hint"):
            status["hint"] = status["ga4_hint"]

    return status


# ---------------------------------------------------------------------------
# GA4 Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def get_account_summaries() -> dict:
    """List all GA4 accounts and properties accessible by the authenticated user.

    Use this as the first step to discover which GA4 properties are available.
    Returns account names, property names, and property IDs.
    """
    from adloop.ga4.reports import get_account_summaries as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def run_ga4_report(
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    property_id: str = "",
    limit: int = 100,
) -> dict:
    """Run a custom GA4 report with specified dimensions, metrics, and date range.

    Common dimensions: date, pagePath, sessionSource, sessionMedium, country, deviceCategory, eventName
    Common metrics: sessions, totalUsers, newUsers, screenPageViews, conversions, eventCount, bounceRate

    Date formats: "today", "yesterday", "7daysAgo", "28daysAgo", "90daysAgo", or "YYYY-MM-DD".
    If property_id is empty, uses the default from config.
    """
    from adloop.ga4.reports import run_ga4_report as _impl

    return _impl(
        _config,
        property_id=property_id or _config.ga4.property_id,
        dimensions=dimensions,
        metrics=metrics,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        limit=limit,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def run_realtime_report(
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    property_id: str = "",
) -> dict:
    """Run a GA4 realtime report showing current active users and events.

    Useful for checking if tracking is firing correctly after code changes.
    Common dimensions: unifiedScreenName, eventName, country, deviceCategory
    Common metrics: activeUsers, eventCount
    """
    from adloop.ga4.reports import run_realtime_report as _impl

    return _impl(
        _config,
        property_id=property_id or _config.ga4.property_id,
        dimensions=dimensions,
        metrics=metrics,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_tracking_events(
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
    property_id: str = "",
) -> dict:
    """List all GA4 events and their volume for the given date range.

    Returns every distinct event name with its total event count.
    Use this to understand what tracking is configured and active.
    """
    from adloop.ga4.tracking import get_tracking_events as _impl

    return _impl(
        _config,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


# ---------------------------------------------------------------------------
# Google Ads Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def list_accounts(limit: int = 200) -> dict:
    """List accessible Google Ads accounts.

    Returns account names, IDs, and status. The default cap of 200 covers
    the vast majority of agency MCCs in one call. If the user explicitly
    asked to see ALL of their accounts and the response comes back with
    'truncated: true', call this tool again with a much higher limit (e.g.
    list_accounts(limit=1000)) — do not stop at the truncated list. For
    workflows that target a specific account you don't need to enumerate
    at all: pass customer_id directly to get_campaign_performance,
    run_gaql, etc.
    """
    from adloop.ads.read import list_accounts as _impl

    return _impl(_config, limit=limit)


@mcp.tool(annotations=_READONLY)
@_safe
def get_campaign_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get campaign-level performance metrics for a date range.

    Returns: campaign name, status, type, impressions, clicks, cost,
    conversions, CPA, ROAS, CTR for each campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_campaign_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_ad_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad-level performance data including headlines, descriptions, and metrics.

    Returns: ad type, headlines, descriptions, final URL, impressions,
    clicks, CTR, conversions, cost for each ad.
    """
    from adloop.ads.read import get_ad_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_keyword_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get keyword metrics including quality scores and competitive data.

    Returns: keyword text, match type, quality score, impressions,
    clicks, CTR, CPC, conversions for each keyword.
    """
    from adloop.ads.read import get_keyword_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_search_terms(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get search terms report — what users actually typed before clicking your ads.

    Critical for finding negative keyword opportunities and understanding user intent.
    Returns: search term, campaign, ad group, impressions, clicks, conversions.
    """
    from adloop.ads.read import get_search_terms as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_negative_keywords(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List existing negative keywords for a campaign or all campaigns.

    Use this before adding negative keywords to check for duplicates.
    If campaign_id is empty, returns negatives across all campaigns.
    """
    from adloop.ads.read import get_negative_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_negative_keyword_lists(
    customer_id: str = "",
) -> dict:
    """List all shared negative keyword lists (SharedSets) in the account.

    Returns each list's ID, name, status, and keyword count. Always call
    this before propose_negative_keyword_list to avoid creating duplicates —
    a suitable list may already exist and just need attaching to a campaign.
    """
    from adloop.ads.read import get_negative_keyword_lists as _impl

    return _impl(_config, customer_id=customer_id or _config.ads.customer_id)


@mcp.tool(annotations=_READONLY)
@_safe
def get_negative_keyword_list_keywords(
    shared_set_id: str,
    customer_id: str = "",
) -> dict:
    """List the keywords inside a shared negative keyword list.

    shared_set_id: numeric ID from get_negative_keyword_lists (shared_set.id).
    """
    from adloop.ads.read import get_negative_keyword_list_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        shared_set_id=shared_set_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_negative_keyword_list_campaigns(
    shared_set_id: str = "",
    customer_id: str = "",
) -> dict:
    """List which campaigns a shared negative keyword list is attached to.

    shared_set_id: numeric ID from get_negative_keyword_lists. Omit to see
    all list-to-campaign attachments across the account.
    """
    from adloop.ads.read import get_negative_keyword_list_campaigns as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        shared_set_id=shared_set_id,
    )


# ---------------------------------------------------------------------------
# Google Ads — Recommendations, Performance Max & Audience Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def get_recommendations(
    customer_id: str = "",
    recommendation_types: list[str] | None = None,
    campaign_id: str = "",
) -> dict:
    """Retrieve Google's auto-generated recommendations with estimated impact.

    Returns each recommendation's type, associated campaign/ad group, current
    (base) and projected (potential) metrics, and the estimated improvement.

    recommendation_types: optional filter — e.g. ["KEYWORD", "TARGET_CPA_OPT_IN",
        "MAXIMIZE_CONVERSIONS_OPT_IN", "RESPONSIVE_SEARCH_AD"]. Empty = all types.
    campaign_id: optional — scope to a single campaign.

    Includes insights that flag budget-increase recommendations (often self-serving)
    and highlight high-impact suggestions worth investigating.
    """
    from adloop.ads.read import get_recommendations as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        recommendation_types=recommendation_types,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_pmax_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get Performance Max campaign and asset group performance.

    Returns two result sets:
    - campaigns: PMax campaign metrics broken down by ad_network_type (SEARCH,
      CONTENT, YOUTUBE_SEARCH, YOUTUBE_WATCH, MIXED). Note: MIXED is a catch-all
      that Google uses for most PMax traffic — full channel splits are not
      available via the API.
    - asset_groups: per-asset-group metrics including ad_strength (EXCELLENT,
      GOOD, AVERAGE, POOR).

    Includes insights flagging weak ad strength, zero-conversion asset groups,
    and network type distribution.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.pmax import get_pmax_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_asset_performance(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Get per-asset details for Performance Max campaigns.

    Returns each asset's field_type (HEADLINE, DESCRIPTION, MARKETING_IMAGE,
    YOUTUBE_VIDEO, etc.), primary_status (ELIGIBLE, NOT_ELIGIBLE, PAUSED,
    PENDING), and content (text or image URL).

    Note: per-asset performance labels (BEST/GOOD/LOW) are not available for
    PMax assets in Google Ads API v23. Use get_detailed_asset_performance to
    see which asset combinations Google selects most — the closest proxy for
    individual asset quality.

    campaign_id: optional filter to a single PMax campaign.
    Includes by_status and by_field_type summaries.
    """
    from adloop.ads.pmax import get_asset_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_detailed_asset_performance(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Get top-performing asset combinations for Performance Max campaigns.

    Shows which headline + description + image combinations Google selects
    most often. Each combination lists the assets used and their field types.
    This data helps identify which creative elements work well together.

    campaign_id: optional filter to a single PMax campaign.
    """
    from adloop.ads.pmax import get_detailed_asset_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_audience_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
) -> dict:
    """Get audience segment performance metrics.

    Returns performance by audience type — remarketing lists (USER_LIST),
    in-market segments (USER_INTEREST), affinity, demographics (AGE_RANGE,
    GENDER), etc. Shows display_name, impressions, clicks, cost, conversions,
    CTR, and CPC for each audience.

    Works for campaigns with explicit audience targeting (Search, Display).
    PMax audience targeting is automatic and may not appear in this report.
    campaign_id: optional filter to a single campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_audience_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


# ---------------------------------------------------------------------------
# Cross-Reference Tools (GA4 + Ads Combined)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def analyze_campaign_conversions(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
    campaign_name: str = "",
) -> dict:
    """Campaign clicks → GA4 conversions mapping — the real cost-per-conversion.

    Combines Google Ads campaign metrics with GA4 session/conversion data to
    reveal click-to-session ratios (GDPR indicator), compare Ads-reported vs
    GA4-reported conversions, and compute cost-per-GA4-conversion.

    Also returns non-paid channel conversion rates for comparison context.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.crossref import analyze_campaign_conversions as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_name=campaign_name,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def landing_page_analysis(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
) -> dict:
    """Analyze which landing pages convert and which don't.

    Combines ad final URLs with GA4 page-level data to show paid traffic
    sessions, conversion rates, bounce rates, and engagement per landing page.
    Identifies pages that get ad clicks but zero conversions and orphaned URLs.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.crossref import landing_page_analysis as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def attribution_check(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
    conversion_events: list[str] | None = None,
) -> dict:
    """Compare Ads-reported conversions vs GA4 — find tracking discrepancies.

    Checks whether conversions reported by Google Ads match what GA4 records,
    diagnoses GDPR consent gaps, attribution model differences, and missing
    conversion event configuration.

    conversion_events: optional list of GA4 event names to specifically check
    (e.g. ["sign_up", "purchase"]). If omitted, compares aggregate totals only.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.crossref import attribution_check as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        conversion_events=conversion_events,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def audit_event_coverage(
    expected_events: list[str],
    gtm_account_id: str,
    gtm_container_id: str,
    property_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Three-way audit: codebase events ↔ GTM tags ↔ GA4 actual fires.

    First, search the user's codebase for gtag('event', ...) and
    dataLayer.push({event: ...}) calls and extract every distinct event name.
    Pass that list as `expected_events`. The tool fetches the LIVE GTM
    container, joins it against GA4 event counts for the date range, and
    returns a per-event matrix with one of these statuses:
      ok                          — tag active and event firing
      ok_auto_collected           — GA4 Enhanced Measurement event, no tag needed
      no_tag_no_fire              — codebase event, no GTM tag, never fires
      tag_paused                  — GTM tag exists but is paused
      tag_active_but_not_firing   — tag is active but no GA4 hits
      gtm_only_firing             — GA4 event from a tag, not in codebase
      gtm_only_not_firing         — tag exists, not in codebase, no fires
      ga4_only                    — fires in GA4, no tag, no codebase ref
      ga4_fires_no_tag            — codebase event firing without a GTM tag
      auto_event_only             — Enhanced Measurement event with no codebase ref

    Also surfaces dynamic-event tags ({{Event}} variables) and Custom HTML
    tags that the audit cannot interpret automatically.

    GTM IDs come from Tag Manager UI → Admin → Container Settings.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.crossref import audit_event_coverage as _impl

    return _impl(
        _config,
        expected_events=expected_events,
        gtm_account_id=gtm_account_id,
        gtm_container_id=gtm_container_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_accounts() -> dict:
    """List all GTM accounts the AdLoop service account / OAuth user can read.

    Use this for first-time discovery before calling audit_event_coverage —
    you need the account_id from here. If this returns an empty list, the
    service account hasn't been added to any GTM container with at least
    Read permission.
    """
    from adloop.gtm.read import list_accounts as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_containers(gtm_account_id: str) -> dict:
    """List all containers under a GTM account.

    Returns container_id (the numeric ID needed by audit_event_coverage),
    public_id (the GTM-XXXXXXX string shown in the UI), name, and usage
    context (web / iOS / Android / amp / server).
    """
    from adloop.gtm.read import list_containers as _impl

    return _impl(_config, account_id=gtm_account_id)


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_tags(gtm_account_id: str, gtm_container_id: str) -> dict:
    """List every tag in the LIVE GTM container.

    Each tag includes type, status, parsed parameters, the GA4 event name
    (for GA4 event tags), and resolved firing/blocking trigger names.
    Use after audit_event_coverage to inspect specific tags.
    """
    from adloop.gtm.read import list_tags as _impl

    return _impl(
        _config, account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_gtm_tag(
    gtm_account_id: str, gtm_container_id: str, tag_id: str
) -> dict:
    """Get the full RAW configuration for a single GTM tag.

    Includes every parameter, firing/blocking triggers (with their filter
    conditions resolved to text), priority, pause status, sampling, and
    monitoring metadata. Use to inspect a tag flagged by audit_event_coverage.
    """
    from adloop.gtm.read import get_tag as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        tag_id=tag_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_triggers(gtm_account_id: str, gtm_container_id: str) -> dict:
    """List every trigger in the LIVE GTM container.

    Each trigger has its filter conditions parsed to readable text
    (e.g. "{{Page Path}} matches RegExp ^/service-promotions/"). Use to
    diagnose why a tag fires or doesn't fire on specific pages.
    """
    from adloop.gtm.read import list_triggers as _impl

    return _impl(
        _config, account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_gtm_trigger(
    gtm_account_id: str, gtm_container_id: str, trigger_id: str
) -> dict:
    """Get the full RAW configuration for a single GTM trigger.

    Includes filters, auto-event filters, custom-event filters, validation
    settings, and a list of every tag that uses this trigger. Use to
    diagnose why a tag with a specific trigger ID does or doesn't fire.
    """
    from adloop.gtm.read import get_trigger as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        trigger_id=trigger_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_variables(gtm_account_id: str, gtm_container_id: str) -> dict:
    """List GTM variables — both custom and enabled built-in.

    Custom variables come from the live container. Built-in variables
    (Page URL, Click Element, Form ID, etc.) come from the workspace's
    enabled-built-ins list. Variables matter because triggers reference
    them — if a trigger uses {{Form ID}} but Form ID isn't enabled, the
    trigger never matches.
    """
    from adloop.gtm.read import list_variables as _impl

    return _impl(
        _config, account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_workspaces(gtm_account_id: str, gtm_container_id: str) -> dict:
    """List workspaces (drafts) under a GTM container.

    Workspace IDs are needed for `get_gtm_workspace_diff`. Most containers
    have a single Default Workspace; multiple workspaces appear when the
    team uses parallel drafts.
    """
    from adloop.gtm.read import list_workspaces as _impl

    return _impl(
        _config, account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_gtm_workspace_diff(
    gtm_account_id: str, gtm_container_id: str, workspace_id: str
) -> dict:
    """Show drafted-but-not-published changes in a GTM workspace.

    Returns the list of entities (tags, triggers, variables) added,
    modified, or deleted relative to the live published version, plus
    any merge conflicts. Common cause of "I edited a tag but nothing
    happened" — the workspace was never published. is_clean=true means
    no pending changes and no conflicts.
    """
    from adloop.gtm.read import get_workspace_diff as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def list_gtm_versions(
    gtm_account_id: str, gtm_container_id: str, page_size: int = 50
) -> dict:
    """List published GTM version history (newest first).

    Version headers include version_id, name, and entity counts. Use to
    correlate a metric drop with a recent publish: fetch versions, find
    one with timestamps near the drop date, then call get_gtm_version
    for full content + author info.
    """
    from adloop.gtm.read import list_versions as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        page_size=page_size,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_gtm_version(
    gtm_account_id: str, gtm_container_id: str, container_version_id: str
) -> dict:
    """Get full metadata + entity counts for a single GTM container version.

    Returns name, description, fingerprint, and lists of tag/trigger/
    variable names at that point in time. Use after list_gtm_versions
    when correlating a metric drop with a specific publish.
    """
    from adloop.gtm.read import get_version as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        container_version_id=container_version_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def run_gaql(
    query: str,
    customer_id: str = "",
    format: str = "table",
) -> dict:
    """Execute an arbitrary GAQL (Google Ads Query Language) query.

    Use this for advanced queries not covered by the other tools.
    See the GAQL reference in the AdLoop cursor rules for syntax help.

    format: "table" (default, readable), "json" (structured), "csv" (exportable)
    """
    from adloop.ads.gaql import run_gaql as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        query=query,
        format=format,
    )


# ---------------------------------------------------------------------------
# Google Ads Write Tools (Safety Layer)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
@_safe
def draft_campaign(
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    geo_target_ids: list[str],
    language_ids: list[str],
    customer_id: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    channel_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
    search_partners_enabled: bool = False,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft a full campaign structure — returns a PREVIEW, does NOT create anything.

    Creates: CampaignBudget + Campaign (PAUSED) + AdGroup + optional Keywords
    + geo targeting + language targeting.
    Ads are NOT included — use draft_responsive_search_ad after the campaign exists.

    bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
                      MAXIMIZE_CONVERSION_VALUE | TARGET_SPEND | MANUAL_CPC
    target_cpa: required if bidding_strategy is TARGET_CPA (in account currency)
    target_roas: required if bidding_strategy is TARGET_ROAS
    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD"}
    search_partners_enabled: include ads on Search partners
    display_network_enabled: enable Search campaign display expansion
    display_expansion_enabled: alias for display_network_enabled
    max_cpc: manual CPC bid for the initial ad group when bidding_strategy is
        MANUAL_CPC, or the Maximize Clicks CPC cap when bidding_strategy is
        TARGET_SPEND
    geo_target_ids: REQUIRED list of geo target constant IDs
        Common: "2276" Germany, "2040" Austria, "2756" Switzerland, "2840" USA,
        "2826" UK, "2250" France. Full list: Google Ads API geo target constants.
    language_ids: REQUIRED list of language constant IDs
        Common: "1001" German, "1000" English, "1002" French, "1004" Spanish,
        "1014" Portuguese. Full list: Google Ads API language constants.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_campaign as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        channel_type=channel_type,
        ad_group_name=ad_group_name,
        keywords=keywords,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_ad_group(
    campaign_id: str,
    ad_group_name: str,
    keywords: list[dict] | None = None,
    customer_id: str = "",
    cpc_bid_micros: int = 0,
) -> dict:
    """Draft a new ad group within an existing campaign — returns a PREVIEW, does NOT create.

    Creates an ad group (ENABLED, type SEARCH_STANDARD) in the specified campaign.
    Optionally includes keywords in the same atomic operation.

    campaign_id: The campaign to add the ad group to (get from get_campaign_performance).
    ad_group_name: Name for the new ad group.
    keywords: Optional list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD"}.
    cpc_bid_micros: Optional ad group CPC bid in micros (only for MANUAL_CPC campaigns).

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_ad_group as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        ad_group_name=ad_group_name,
        keywords=keywords,
        cpc_bid_micros=cpc_bid_micros,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def update_campaign(
    campaign_id: str,
    customer_id: str = "",
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    daily_budget: float = 0,
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool | None = None,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft an update to an existing campaign — returns a PREVIEW, does NOT apply.

    Only include the parameters you want to change. Omit the rest.

    campaign_id: the numeric ID of the campaign to update (required)
    bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
                      MAXIMIZE_CONVERSION_VALUE | TARGET_SPEND | MANUAL_CPC
    target_cpa: required if bidding_strategy is TARGET_CPA (in account currency)
    target_roas: required if bidding_strategy is TARGET_ROAS
    daily_budget: new daily budget in account currency
    geo_target_ids: REPLACES all geo targets. Common IDs: "2276" Germany,
        "2040" Austria, "2756" Switzerland, "2840" USA, "2826" UK
    language_ids: REPLACES all language targets. Common IDs: "1001" German,
        "1000" English, "1002" French, "1004" Spanish
    search_partners_enabled: include ads on Search partners
    display_network_enabled: enable Search campaign display expansion
    display_expansion_enabled: alias for display_network_enabled
    max_cpc: Maximize Clicks CPC cap when bidding_strategy is TARGET_SPEND, or
        when the existing campaign already uses TARGET_SPEND

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import update_campaign as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        daily_budget=daily_budget,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_responsive_search_ad(
    ad_group_id: str,
    headlines: list[str | dict],
    descriptions: list[str | dict],
    final_url: str,
    customer_id: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns a PREVIEW, does NOT create the ad.

    Provide 3-15 headlines (max 30 chars each) and 2-4 descriptions (max 90 chars each).
    The preview shows exactly what will be created. Call confirm_and_apply to execute.

    Each headline/description entry may be either:

    - a plain string (unpinned), or
    - a dict ``{"text": "...", "pinned_field": "HEADLINE_1"}`` (pinned).

    Valid pin values:
        headlines:    HEADLINE_1, HEADLINE_2, HEADLINE_3
        descriptions: DESCRIPTION_1, DESCRIPTION_2

    Google caps: at most 2 headlines per pin slot, at most 1 description per pin
    slot. Mixed plain-string and dict entries are allowed within a single call
    (e.g. brand pinned to HEADLINE_1, the rest unpinned).
    """
    from adloop.ads.write import draft_responsive_search_ad as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        ad_group_id=ad_group_id,
        headlines=headlines,
        descriptions=descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_keywords(
    ad_group_id: str,
    keywords: list[dict],
    customer_id: str = "",
) -> dict:
    """Draft keyword additions — returns a PREVIEW, does NOT add keywords.

    keywords: list of {"text": "keyword phrase", "match_type": "EXACT|PHRASE|BROAD"}
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        ad_group_id=ad_group_id,
        keywords=keywords,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def add_negative_keywords(
    campaign_id: str,
    keywords: list[str],
    customer_id: str = "",
    match_type: str = "EXACT",
) -> dict:
    """Draft negative keyword additions — returns a PREVIEW.

    Negative keywords prevent your ads from showing for irrelevant searches.
    match_type: "EXACT", "PHRASE", or "BROAD"
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import add_negative_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def propose_negative_keyword_list(
    campaign_id: str,
    list_name: str,
    keywords: list[str],
    customer_id: str = "",
    match_type: str = "EXACT",
) -> dict:
    """Draft a shared negative keyword list and attach it to a campaign — returns a PREVIEW.

    Creates a reusable negative keyword list that can later be applied to multiple
    campaigns, unlike add_negative_keywords which adds directly to one campaign.
    match_type: "EXACT", "PHRASE", or "BROAD"
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import propose_negative_keyword_list as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        list_name=list_name,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def add_to_negative_keyword_list(
    shared_set_id: str,
    keywords: list[str],
    customer_id: str = "",
    match_type: str = "EXACT",
) -> dict:
    """Append keywords to an EXISTING shared negative keyword list — returns a PREVIEW.

    Use this when a suitable list already exists and only needs more keywords
    (instead of propose_negative_keyword_list, which creates a new list).
    Always call get_negative_keyword_lists first to find the right shared_set_id
    and get_negative_keyword_list_keywords to avoid duplicating existing terms.

    shared_set_id: numeric ID from get_negative_keyword_lists (shared_set.id).
    keywords: list of keyword strings to append (duplicates in the input list
        are collapsed).
    match_type: "EXACT", "PHRASE", or "BROAD"

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import add_to_negative_keyword_list as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        shared_set_id=shared_set_id,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def update_ad_group(
    ad_group_id: str,
    customer_id: str = "",
    ad_group_name: str = "",
    max_cpc: float = 0,
) -> dict:
    """Draft an ad group update for name and/or manual CPC bid."""
    from adloop.ads.write import update_ad_group as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        ad_group_id=ad_group_id,
        ad_group_name=ad_group_name,
        max_cpc=max_cpc,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_callouts(
    campaign_id: str,
    callouts: list[str],
    customer_id: str = "",
) -> dict:
    """Draft campaign callout assets — returns a PREVIEW."""
    from adloop.ads.write import draft_callouts as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        callouts=callouts,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_structured_snippets(
    campaign_id: str,
    snippets: list[dict],
    customer_id: str = "",
) -> dict:
    """Draft campaign structured snippet assets — returns a PREVIEW."""
    from adloop.ads.write import draft_structured_snippets as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        snippets=snippets,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_image_assets(
    campaign_id: str,
    image_paths: list[str],
    customer_id: str = "",
) -> dict:
    """Draft campaign image assets from local PNG, JPEG, or GIF files."""
    from adloop.ads.write import draft_image_assets as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        image_paths=image_paths,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def pause_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft pausing a campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", or "keyword"
    entity_id format by type:
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: ad group ID (e.g. "12345678")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~criterionId" (e.g. "12345678~987654")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import pause_entity as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def enable_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft enabling a paused campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", or "keyword"
    entity_id format by type:
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: ad group ID (e.g. "12345678")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~criterionId" (e.g. "12345678~987654")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import enable_entity as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def remove_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft REMOVING an entity — returns a PREVIEW. This is IRREVERSIBLE.

    entity_type: "campaign", "ad_group", "ad", "keyword", "negative_keyword",
                 "shared_criterion", "campaign_asset", "asset", or "customer_asset"
    entity_id: The resource ID.
               For keywords: "adGroupId~criterionId"
               For negative_keywords: "campaignId~criterionId"
                   (use the resource_id field from get_negative_keywords)
               For shared_criterion: "sharedSetId~criterionId"
                   (use the resource_id field from get_negative_keyword_list_keywords)
               For campaign_asset: "campaignId~assetId~fieldType"
               For asset: simple asset ID
               For customer_asset: "assetId~fieldType"

    WARNING: Removed entities cannot be re-enabled. Use pause_entity instead
    if you just want to temporarily disable something.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import remove_entity as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_sitelinks(
    campaign_id: str,
    sitelinks: list[dict],
    customer_id: str = "",
) -> dict:
    """Draft sitelink extensions for a campaign — returns a PREVIEW.

    Sitelinks appear as additional links below your ad, increasing click area
    and directing users to specific pages.

    campaign_id: the campaign to attach sitelinks to
    sitelinks: list of dicts, each with:
        - link_text (str, required, max 25 chars) — the clickable text shown
        - final_url (str, required) — destination URL for this sitelink
        - description1 (str, optional, max 35 chars) — first description line
        - description2 (str, optional, max 35 chars) — second description line

    Google recommends at least 4 sitelinks per campaign. Fewer than 2 may not show.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_sitelinks as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        sitelinks=sitelinks,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def confirm_and_apply(
    plan_id: str,
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    IMPORTANT: Defaults to dry_run=True. You MUST explicitly pass dry_run=false
    to make real changes to the Google Ads account.

    Config override: if 'safety.require_dry_run: true' is set in the user's
    config file (default ~/.adloop/config.yaml), dry_run=false is IGNORED
    and this tool will keep returning DRY_RUN_SUCCESS. When that happens the
    response includes 'dry_run_forced_by', 'config_path', and 'remediation'
    fields — surface those to the user verbatim and STOP retrying. Calling
    this tool again with dry_run=false will not change anything until the
    user edits the config file, sets 'require_dry_run: false', and restarts
    the AdLoop MCP server.

    The plan_id comes from a prior draft_* or pause/enable tool call.
    """
    from adloop.ads.write import confirm_and_apply as _impl

    return _impl(_config, plan_id=plan_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Tracking Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def validate_tracking(
    expected_events: list[str],
    property_id: str = "",
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
) -> dict:
    """Compare tracking events found in the codebase against actual GA4 data.

    First, search the user's codebase for gtag('event', ...) or dataLayer.push
    calls and extract event names. Then pass those names here to check which
    ones actually fire in GA4.

    Returns: matched events, events missing from GA4, unexpected GA4 events,
    and auto-collected events (page_view, session_start, etc.).
    """
    from adloop.tracking import validate_tracking as _impl

    return _impl(
        _config,
        expected_events=expected_events,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def generate_tracking_code(
    event_name: str,
    event_params: dict | None = None,
    trigger: str = "",
    property_id: str = "",
    check_existing: bool = True,
) -> dict:
    """Generate a GA4 event tracking JavaScript snippet.

    Produces ready-to-paste gtag code for the specified event. Includes
    recommended parameters for well-known GA4 events (sign_up, purchase, etc.).
    Optionally checks GA4 to warn if the event already fires.

    trigger: "form_submit", "button_click", or "page_load" — wraps the gtag
    call in an appropriate event listener. Empty = bare gtag call.
    """
    from adloop.tracking import generate_tracking_code as _impl

    return _impl(
        _config,
        event_name=event_name,
        event_params=event_params,
        trigger=trigger,
        property_id=property_id or _config.ga4.property_id,
        check_existing=check_existing,
    )


# ---------------------------------------------------------------------------
# Planning Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def estimate_budget(
    keywords: list[dict],
    daily_budget: float = 0,
    geo_target_id: str = "2276",
    language_id: str = "1000",
    forecast_days: int = 30,
    customer_id: str = "",
) -> dict:
    """Forecast clicks, impressions, and cost for a set of keywords.

    Uses Google Ads Keyword Planner to estimate campaign performance without
    creating anything. Essential for budget planning before launching campaigns.

    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD", "max_cpc": 1.50}
        max_cpc is optional (defaults to 1.00 in account currency)
    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK, 2250=France)
    language_id: language constant (1000=English, 1001=German, 1002=French, 1003=Spanish)
    daily_budget: if provided, insights will show what % of traffic the budget captures
    forecast_days: forecast horizon in days (default 30)
    """
    from adloop.ads.forecast import estimate_budget as _impl

    return _impl(
        _config,
        keywords=keywords,
        daily_budget=daily_budget,
        geo_target_id=geo_target_id,
        language_id=language_id,
        forecast_days=forecast_days,
        customer_id=customer_id or _config.ads.customer_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def discover_keywords(
    seed_keywords: list[str] = [],  # noqa: B006 — mutable default required for MCP JSON schema
    url: str = "",
    geo_target_id: str = "2276",
    language_id: str = "1000",
    page_size: int = 50,
    customer_id: str = "",
) -> dict:
    """Discover new keyword ideas using Google Ads Keyword Planner.

    Mirrors the "Discover new keywords" UI in Keyword Planner:
    - Start with keywords: pass seed_keywords (e.g. ["running shoes"])
    - Start with a website: pass url (e.g. "https://example.com/products")
    - Both together: keywords + url for more targeted ideas

    Returns keyword ideas sorted by avg monthly search volume, with
    competition level (LOW/MEDIUM/HIGH) and top-of-page bid range.

    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK)
    language_id: language constant (1000=English, 1001=German, 1002=French)
    page_size: max keyword ideas to return (default 50, max 1000)
    """
    from adloop.ads.forecast import discover_keywords as _impl

    return _impl(
        _config,
        seed_keywords=seed_keywords,
        url=url,
        geo_target_id=geo_target_id,
        language_id=language_id,
        page_size=page_size,
        customer_id=customer_id or _config.ads.customer_id,
    )


# ---------------------------------------------------------------------------
# Optional local-only debug tools (not shipped in git).
# ---------------------------------------------------------------------------
# Activated by ``ADLOOP_DEBUG_TOOLS=1``. The module file is .gitignored and
# only present on developer machines doing MCP-host stress testing.

import os as _os  # noqa: E402

if _os.getenv("ADLOOP_DEBUG_TOOLS", "").lower() in ("1", "true", "yes", "on"):
    try:
        from adloop import _debug_tools  # noqa: F401
    except ImportError:
        # _debug_tools.py is intentionally absent in released builds.
        pass
# ---------------------------------------------------------------------------# GTM Write Tools# ---------------------------------------------------------------------------

@mcp.tool(annotations=_WRITE)
@_safe
def draft_gtm_tag(
    gtm_account_id: str,
    gtm_container_id: str,
    name: str,
    tag_type: str,
    parameters: list[dict] | None = None,
    firing_trigger_ids: list[str] | None = None,
    blocking_trigger_ids: list[str] | None = None,
    paused: bool = False,
    notes: str = "",
    workspace_id: str = "",
) -> dict:
    """Draft a new GTM tag in the Default Workspace — returns a PREVIEW.

    Common tag_type values:
        googtag — Google Tag (gtag.js config). Required for both AW-... and G-...
        awct    — Google Ads Conversion Tracking
        gclidw  — Conversion Linker (no parameters required)
        html    — Custom HTML
        gaawe   — GA4 Event tag

    parameters: list of GTM parameter dicts. Common shapes:

        Google Tag config (Google Ads):
            [{"type": "TEMPLATE", "key": "tagId", "value": "AW-11437481610"}]

        Google Ads Conversion (with phone_conversion_number for GFN):
            [{"type": "TEMPLATE", "key": "conversionId", "value": "11437481610"},
             {"type": "TEMPLATE", "key": "conversionLabel", "value": "_qxp..."},
             {"type": "TEMPLATE", "key": "conversionValue", "value": "250"},
             {"type": "TEMPLATE", "key": "conversionCurrency", "value": "USD"},
             {"type": "TEMPLATE", "key": "phone_conversion_number",
              "value": "(916) 460-9257"}]

        Custom HTML (e.g. GFN snippet):
            [{"type": "TEMPLATE", "key": "html",
              "value": "<script>...gtag config...</script>"},
             {"type": "BOOLEAN", "key": "supportDocumentWrite", "value": "false"}]

    firing_trigger_ids: list of trigger ID strings. Use ["2147479573"] for
        the built-in "All Pages — Initialization" trigger.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.gtm.write import draft_gtm_tag as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        name=name,
        tag_type=tag_type,
        parameters=parameters,
        firing_trigger_ids=firing_trigger_ids,
        blocking_trigger_ids=blocking_trigger_ids,
        paused=paused,
        notes=notes,
    )

@mcp.tool(annotations=_WRITE)
@_safe
def draft_gtm_trigger(
    gtm_account_id: str,
    gtm_container_id: str,
    name: str,
    trigger_type: str,
    filters: list[dict] | None = None,
    custom_event_filters: list[dict] | None = None,
    auto_event_filters: list[dict] | None = None,
    custom_event_name: str = "",
    parameters: list[dict] | None = None,
    notes: str = "",
    workspace_id: str = "",
) -> dict:
    """Draft a new GTM trigger in the Default Workspace — returns a PREVIEW.

    Common trigger_type values:
        pageview, dom_ready, window_loaded
        click, linkClick
        formSubmission
        customEvent (requires custom_event_name)

    filters: GTM filter shape — list of dicts:
        [{"type": "EQUALS"|"CONTAINS"|"STARTS_WITH"|"REGEX"|"GREATER"|...,
          "parameter": [
            {"type": "TEMPLATE", "key": "arg0", "value": "{{Variable}}"},
            {"type": "TEMPLATE", "key": "arg1", "value": "expected"}
          ]}]

    Common patterns:

        Click on tel: links anywhere on site:
            trigger_type="linkClick", filters=[{
                "type": "STARTS_WITH",
                "parameter": [
                    {"type": "TEMPLATE", "key": "arg0", "value": "{{Click URL}}"},
                    {"type": "TEMPLATE", "key": "arg1", "value": "tel:"}
                ]
            }]

        Form submit on /contacts:
            trigger_type="formSubmission", filters=[{
                "type": "CONTAINS",
                "parameter": [
                    {"type": "TEMPLATE", "key": "arg0", "value": "{{Page Path}}"},
                    {"type": "TEMPLATE", "key": "arg1", "value": "/contacts"}
                ]
            }]

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.gtm.write import draft_gtm_trigger as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        name=name,
        trigger_type=trigger_type,
        filters=filters,
        custom_event_filters=custom_event_filters,
        auto_event_filters=auto_event_filters,
        custom_event_name=custom_event_name,
        parameters=parameters,
        notes=notes,
    )

@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def publish_gtm_workspace(
    gtm_account_id: str,
    gtm_container_id: str,
    workspace_id: str = "",
    version_name: str = "",
    version_notes: str = "",
) -> dict:
    """Publish the workspace's drafted changes — returns a PREVIEW (irreversible apply).

    Publishing creates a new container version from the workspace and sets it
    LIVE. Until you call confirm_and_apply with dry_run=false, no live change
    happens.

    Once published, all visitors with the GTM snippet on their pages will
    start firing the new tags within minutes. There's no rollback to the
    workspace state — to revert, you'd publish a previous version (use
    list_gtm_versions to find one).

    workspace_id: leave empty to use the Default Workspace.
    version_name: optional friendly name for the version.
    version_notes: optional release notes.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.gtm.write import publish_gtm_workspace as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        version_name=version_name,
        version_notes=version_notes,
    )

@mcp.tool(annotations=_WRITE)
@_safe
def draft_update_gtm_tag(
    gtm_account_id: str,
    gtm_container_id: str,
    tag_id: str,
    name: str = "",
    parameters: list[dict] | None = None,
    firing_trigger_ids: list[str] | None = None,
    blocking_trigger_ids: list[str] | None = None,
    paused: bool | None = None,
    notes: str = "",
    tag_type: str = "",
    workspace_id: str = "",
) -> dict:
    """Draft a partial UPDATE to an existing GTM tag — returns a PREVIEW.

    Pass only the fields you want to change. Empty/None means "keep current".

    Common use cases:
      - Rename a tag: pass `name`
      - Pause/unpause: pass `paused=True/False`
      - Replace parameters: pass full `parameters` list (no partial merge)
      - Re-route firing triggers: pass `firing_trigger_ids`

    Note: tag type cannot usually be changed; if you need to switch (e.g.
    `html` → `awcc`), delete the old and create a new one.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.gtm.write import draft_update_gtm_tag as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        tag_id=tag_id,
        name=name,
        parameters=parameters,
        firing_trigger_ids=firing_trigger_ids,
        blocking_trigger_ids=blocking_trigger_ids,
        paused=paused,
        notes=notes,
        tag_type=tag_type,
    )

@mcp.tool(annotations=_WRITE)
@_safe
def draft_update_gtm_trigger(
    gtm_account_id: str,
    gtm_container_id: str,
    trigger_id: str,
    name: str = "",
    filters: list[dict] | None = None,
    custom_event_filters: list[dict] | None = None,
    auto_event_filters: list[dict] | None = None,
    parameters: list[dict] | None = None,
    notes: str = "",
    workspace_id: str = "",
) -> dict:
    """Draft a partial UPDATE to an existing GTM trigger — returns a PREVIEW.

    Trigger type cannot be changed (delete + create instead).

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.gtm.write import draft_update_gtm_trigger as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        trigger_id=trigger_id,
        name=name,
        filters=filters,
        custom_event_filters=custom_event_filters,
        auto_event_filters=auto_event_filters,
        parameters=parameters,
        notes=notes,
    )

@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def draft_delete_gtm_tag(
    gtm_account_id: str,
    gtm_container_id: str,
    tag_id: str,
    workspace_id: str = "",
) -> dict:
    """Draft a deletion of a GTM tag — returns a PREVIEW (irreversible).

    Publish the workspace to make the deletion live.
    """
    from adloop.gtm.write import draft_delete_gtm_tag as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        tag_id=tag_id,
    )

@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def draft_delete_gtm_trigger(
    gtm_account_id: str,
    gtm_container_id: str,
    trigger_id: str,
    workspace_id: str = "",
) -> dict:
    """Draft a deletion of a GTM trigger — returns a PREVIEW (irreversible).

    GTM blocks the deletion if any tag references this trigger.
    """
    from adloop.gtm.write import draft_delete_gtm_trigger as _impl

    return _impl(
        _config,
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        trigger_id=trigger_id,
    )
