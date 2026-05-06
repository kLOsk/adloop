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


# ---------------------------------------------------------------------------
# GTM Write Tools
# ---------------------------------------------------------------------------

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
# ServiceTitan Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def health_check_servicetitan() -> dict:
    """Verify ServiceTitan auth + tenant access.

    Tests OAuth client_credentials flow against auth.servicetitan.io and a
    tenant-scoped read against the configured tenant_id. Returns auth_ok,
    tenant_ok, and a count of business units the app can see.

    Run this first if any ServiceTitan tool is failing.
    """
    from adloop.servicetitan.read import health_check as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def list_st_business_units() -> dict:
    """List business units in the ServiceTitan tenant.

    Most ST endpoints accept a businessUnitId filter — use this to discover
    the IDs you'll need (e.g. separate residential vs commercial BUs).
    """
    from adloop.servicetitan.read import list_business_units as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def list_st_campaigns(active_only: bool = False) -> dict:
    """List ServiceTitan marketing campaigns (channel-level).

    These are the channels ST uses to attribute leads/jobs (e.g. "Google PPC",
    "Direct Mail", "Yelp"). Campaign IDs returned here are the values to pass
    to get_st_calls / get_st_leads / get_st_jobs as `campaign_id`.

    Set active_only=True to filter to currently-active campaigns.
    """
    from adloop.servicetitan.read import list_campaigns as _impl

    return _impl(_config, active_only=active_only)


@mcp.tool(annotations=_READONLY)
@_safe
def list_st_campaign_categories() -> dict:
    """List ServiceTitan marketing campaign categories (parent groupings)."""
    from adloop.servicetitan.read import list_campaign_categories as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def get_st_calls(
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: int | None = None,
    business_unit_id: int | None = None,
    direction: str = "",
    has_recording: bool | None = None,
    max_results: int = 500,
) -> dict:
    """Pull ServiceTitan calls — duration, customer, campaign, recording flag.

    date_range_start / date_range_end accept ISO-8601 (e.g. "2026-04-01T00:00:00Z").
    Defaults to last 30 days when omitted.

    Filters: campaign_id (from list_st_campaigns), business_unit_id, direction
    ("Inbound" or "Outbound"), has_recording (True/False/None).

    Returns calls with `recording_id` populated when audio is available — feed
    that ID to get_st_call_recording_url to fetch the audio for transcription.
    """
    from adloop.servicetitan.read import get_calls as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        campaign_id=campaign_id,
        business_unit_id=business_unit_id,
        direction=direction or None,
        has_recording=has_recording,
        max_results=max_results,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_st_call_recording_url(call_id: int) -> dict:
    """Get a downloadable URL for a ServiceTitan call recording.

    Returns the recording payload (URL or signed redirect) for the given call.
    Pass call_id from get_st_calls (where has_recording=true).

    Note: requires the Call Recording API add-on on your ST tenant.
    """
    from adloop.servicetitan.read import get_call_recording_url as _impl

    return _impl(_config, call_id=call_id)


@mcp.tool(annotations=_READONLY)
@_safe
def get_st_leads(
    date_range_start: str = "",
    date_range_end: str = "",
    status: str = "",
    campaign_id: int | None = None,
    max_results: int = 500,
) -> dict:
    """Pull ServiceTitan leads — status, campaign, and GCLID extraction from notes.

    Each lead is scanned for GCLID-shaped strings in the `summary` field
    (since ST has no native GCLID field, web form integrations sometimes
    push it into Notes). The response includes `leads_with_gclid_in_notes`
    and per-lead `gclids_in_notes` arrays.

    Defaults to last 30 days. Filter by status or campaign_id as needed.
    """
    from adloop.servicetitan.read import get_leads as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        status=status or None,
        campaign_id=campaign_id,
        max_results=max_results,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_st_jobs(
    date_range_start: str = "",
    date_range_end: str = "",
    job_status: str = "",
    campaign_id: int | None = None,
    business_unit_id: int | None = None,
    max_results: int = 500,
) -> dict:
    """Pull ServiceTitan jobs — status, campaign, BU, originating lead, total revenue.

    Defaults to last 30 days. Use `total` to compute average job value for
    static conversion-value calibration in Google Ads.
    """
    from adloop.servicetitan.read import get_jobs as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        job_status=job_status or None,
        campaign_id=campaign_id,
        business_unit_id=business_unit_id,
        max_results=max_results,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def find_gclid_in_st(
    date_range_start: str = "",
    date_range_end: str = "",
    max_results: int = 500,
) -> dict:
    """Scan recent ServiceTitan leads + jobs for GCLID-shaped strings in notes.

    ServiceTitan has no native GCLID field. This tool checks if your form
    integration pushes the GCLID into Notes/Summary so it can be uploaded
    to Google Ads as an offline conversion.

    Returns counts and per-entity matches plus an actionable insight if
    nothing was found (i.e. the form integration needs to be updated).
    """
    from adloop.servicetitan.read import find_gclid_in_st as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        max_results=max_results,
    )


# ---------------------------------------------------------------------------
# ServiceTitan Analytics — value calibration + funnel + cross-system
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def st_compute_avg_job_value_by_campaign(
    date_range_start: str = "",
    date_range_end: str = "",
    business_unit_id: int | None = None,
    min_jobs: int = 5,
) -> dict:
    """Average job revenue per ST campaign over a window (default last 90d).

    Returns per-campaign avg + overall avg. Use the per-campaign averages to
    set differentiated Google Ads conversion values — PPC may have a different
    avg ticket than Direct Mail or Yelp. Combine with st_compute_close_rate
    to compute: conversion_value = avg_job_value × close_rate.

    Insights flag campaigns whose avg deviates ≥40% from overall — those
    deserve their own conversion value rather than the global default.
    """
    from adloop.servicetitan.analytics import compute_avg_job_value_by_campaign as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        business_unit_id=business_unit_id,
        min_jobs=min_jobs,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_compute_close_rate_by_campaign(
    date_range_start: str = "",
    date_range_end: str = "",
    min_leads: int = 10,
) -> dict:
    """Lead → paying-job close rate per ST campaign (default last 90d).

    Highlights best/worst close-rate campaigns. Low close rate at high lead
    volume usually indicates a dispatch/sales process problem rather than a
    Google Ads keyword problem — investigate before scaling spend.
    """
    from adloop.servicetitan.analytics import compute_close_rate_by_campaign as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        min_leads=min_leads,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_compute_lead_to_revenue_funnel(
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: int | None = None,
) -> dict:
    """Funnel: leads → bookings → jobs → completed → paying, per campaign.

    Reveals at which stage leads die. If a Google Ads campaign produces leads
    that never book, the campaign is fine — the dispatch process is broken.
    """
    from adloop.servicetitan.analytics import compute_lead_to_revenue_funnel as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_correlate_ads_to_revenue(
    date_range_start: str = "",
    date_range_end: str = "",
    name_overrides: dict | None = None,
) -> dict:
    """Join Google Ads campaigns to ServiceTitan revenue by name match.

    Computes true_cpa (ads_cost / st_jobs_paid) and true_roas (st_revenue /
    ads_cost) — the actual numbers that should drive bidding decisions. Pass
    `name_overrides={ads_name: st_name}` for campaigns whose names don't
    fuzzy-match.
    """
    from adloop.servicetitan.analytics import correlate_ads_to_st_revenue as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        name_overrides=name_overrides,
    )


# ---------------------------------------------------------------------------
# ServiceTitan Exports — Google Ads-ready CSVs
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def st_export_offline_conversions(
    conversion_action_name: str,
    date_range_start: str = "",
    date_range_end: str = "",
    use_avg_value: float | None = None,
    currency: str = "USD",
) -> dict:
    """GCLID-based offline conversion CSV for upload to Google Ads.

    Scans completed jobs whose originating lead has a GCLID in its notes.
    Writes a Google Ads-ready CSV under ~/.adloop/exports/. Returns the
    rows-written count and absolute path.

    `conversion_action_name` MUST exactly match an existing conversion
    action in Google Ads. Pass `use_avg_value` to override per-job revenue
    with a static value (e.g. for early-stage value calibration).
    """
    from adloop.servicetitan.exports import export_offline_conversions_for_ads_upload as _impl

    return _impl(
        _config,
        conversion_action_name=conversion_action_name,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        use_avg_value=use_avg_value,
        currency=currency,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_export_phone_conversions(
    conversion_action_name: str,
    date_range_start: str = "",
    date_range_end: str = "",
    use_avg_value: float | None = None,
    currency: str = "USD",
    business_unit_id: int | None = None,
) -> dict:
    """Phone-based call-conversion CSV for upload to Google Ads.

    Works WITHOUT GCLID — this is the right tool for accounts that don't
    yet capture GCLID through the web form. Matches inbound calls (lead-call)
    to Google Ads call extensions via Caller ID + Call Start Time.

    `conversion_action_name` MUST exactly match a call-conversion action
    in Google Ads.
    """
    from adloop.servicetitan.exports import export_phone_conversions_for_ads_upload as _impl

    return _impl(
        _config,
        conversion_action_name=conversion_action_name,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        use_avg_value=use_avg_value,
        currency=currency,
        business_unit_id=business_unit_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_export_enhanced_conversions_for_leads(
    conversion_action_name: str,
    date_range_start: str = "",
    date_range_end: str = "",
    use_avg_value: float | None = None,
    currency: str = "USD",
) -> dict:
    """Enhanced Conversions for Leads — hashed PII upload CSV.

    Recovers attribution for users who blocked GCLID (consent rejection,
    iOS, etc) by matching SHA-256 hashed email/phone/name to logged-in
    Google users. Pairs with the EC for Leads tag (awud) you set up in GTM.
    """
    from adloop.servicetitan.exports import export_enhanced_conversions_for_leads as _impl

    return _impl(
        _config,
        conversion_action_name=conversion_action_name,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        use_avg_value=use_avg_value,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# ServiceTitan Transcription + Classification
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def st_transcribe_call(call_id: int, force_refresh: bool = False) -> dict:
    """Transcribe a ServiceTitan call recording with speaker diarization.

    Uses Google Cloud Speech-to-Text (reuses the GA4/Ads service account).
    Cached at ~/.adloop/st_transcripts/{call_id}.json — pass force_refresh=True
    to re-transcribe.

    Requires Speech-to-Text API enabled on the GCP project and the service
    account to have `roles/speech.editor` (or "Cloud Speech-to-Text User").
    """
    from adloop.servicetitan.transcribe import transcribe_st_call as _impl

    return _impl(_config, call_id=call_id, force_refresh=force_refresh)


@mcp.tool(annotations=_READONLY)
@_safe
def st_classify_call_outcome(call_id: int) -> dict:
    """Classify a call as booked/quoted/no_show/wrong_number/sales_call/spam/info_only.

    Auto-transcribes if needed. Rule-based — predictable + free. Returns the
    matched rule + a snippet for verification.
    """
    from adloop.servicetitan.classify import classify_call_outcome as _impl

    return _impl(_config, call_id=call_id)


@mcp.tool(annotations=_READONLY)
@_safe
def st_extract_call_intent(call_id: int) -> dict:
    """Extract service intents from a call (drain, water heater, leak, emergency...).

    Multiple intents per call are possible. Use to map call patterns to ad
    groups: if 70% of "Plumbing PPC" calls ask for water heaters but you bid
    on drain cleaning, the budget allocation is wrong.
    """
    from adloop.servicetitan.classify import extract_call_intent as _impl

    return _impl(_config, call_id=call_id)


@mcp.tool(annotations=_READONLY)
@_safe
def st_extract_negative_keywords_from_calls(
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: int | None = None,
    min_calls_for_review: int = 5,
    max_calls: int = 100,
    only_with_recording: bool = True,
) -> dict:
    """Mine call transcripts for negative-keyword candidates.

    Walks recent calls, transcribes each (cached), and aggregates "do you
    also do X" / "I'm looking for X" phrases. Returns ranked candidates with
    occurrence counts and example snippets.

    NOTE: transcribing 100 calls can take several minutes and cost ~$1 in
    Google STT charges. Lower max_calls for a faster sample.
    """
    from adloop.servicetitan.classify import extract_negative_keywords_from_calls as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        campaign_id=campaign_id,
        min_calls_for_review=min_calls_for_review,
        max_calls=max_calls,
        only_with_recording=only_with_recording,
    )


# ---------------------------------------------------------------------------
# ServiceTitan Customer Match exports
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def st_export_customer_match_list(max_results: int = 5000) -> dict:
    """Full ST customer base as a hashed Google Ads Customer Match CSV.

    Output written to ~/.adloop/exports/. Email/Phone/First/Last hashed
    SHA-256 lowercase per Google spec. Upload in Google Ads → Audience
    manager → Your data segments.
    """
    from adloop.servicetitan.audiences import export_customer_match_list as _impl

    return _impl(_config, max_results=max_results)


@mcp.tool(annotations=_READONLY)
@_safe
def st_export_lapsed_customer_audience(
    months_inactive: int = 12, max_results: int = 5000
) -> dict:
    """Customers with no completed job in the last N months — reactivation audience.

    Use for low-CPM Display reactivation campaigns. Repeat-customer revenue
    is much cheaper than net-new acquisition.
    """
    from adloop.servicetitan.audiences import export_lapsed_customer_audience as _impl

    return _impl(_config, months_inactive=months_inactive, max_results=max_results)


@mcp.tool(annotations=_READONLY)
@_safe
def st_export_high_value_seed_audience(
    top_pct: float = 0.05, lookback_months: int = 24, max_results: int = 5000
) -> dict:
    """Top X% of customers by lifetime revenue — Customer Match + PMax seed.

    Exports a hashed Customer Match CSV. Use as the seed for Similar-Audience
    targeting and as a PMax audience signal (PMax signals are hints — these
    are your strongest hints).
    """
    from adloop.servicetitan.audiences import export_high_value_seed_audience as _impl

    return _impl(
        _config,
        top_pct=top_pct,
        lookback_months=lookback_months,
        max_results=max_results,
    )


# ---------------------------------------------------------------------------
# ServiceTitan Demand + Attribution Decay
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def st_geo_demand_analysis(
    date_range_start: str = "",
    date_range_end: str = "",
    business_unit_id: int | None = None,
    group_by: str = "zip",
    min_jobs: int = 5,
) -> dict:
    """Job revenue + count grouped by ZIP / city / state (default last 365d).

    Drives geo bid adjustments. Areas with above-average ticket size deserve
    positive bid adjustments; areas below average deserve negative or
    exclusion. Insights flag both ends with concrete adjustment recommendations.
    """
    from adloop.servicetitan.demand import geo_demand_analysis as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        business_unit_id=business_unit_id,
        group_by=group_by,
        min_jobs=min_jobs,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_seasonal_demand_curve(
    date_range_start: str = "",
    date_range_end: str = "",
    business_unit_id: int | None = None,
    group_by: str = "week",
) -> dict:
    """Jobs by week-of-year (or month) — drives ad scheduling + budget pacing.

    Identifies peak and trough periods. Ramp Google Ads budget the period
    BEFORE peak demand, not when leads are already pouring in.
    """
    from adloop.servicetitan.demand import seasonal_demand_curve as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        business_unit_id=business_unit_id,
        group_by=group_by,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def st_attribution_decay_report(
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: int | None = None,
) -> dict:
    """P50/P90/P95 days from job creation to completion + histogram.

    Use to set the conversion window in Google Ads. If P90 is 45 days, the
    default 30-day window will MISS 10%+ of true conversions and break Smart
    Bidding's optimization signal.
    """
    from adloop.servicetitan.demand import attribution_decay_report as _impl

    return _impl(
        _config,
        date_range_start=date_range_start or None,
        date_range_end=date_range_end or None,
        campaign_id=campaign_id,
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
    geo_exclude_ids: list[str] | None = None,
    ad_schedule: list[dict] | None = None,
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
    geo_exclude_ids: optional list of geo target constant IDs to EXCLUDE
        (negative location criteria). Useful when targeting a broad
        region while suppressing specific sub-geos.
    language_ids: REQUIRED list of language constant IDs
        Common: "1001" German, "1000" English, "1002" French, "1004" Spanish,
        "1014" Portuguese. Full list: Google Ads API language constants.
    ad_schedule: optional list of {day_of_week, start_hour, end_hour,
        start_minute, end_minute} entries restricting when the campaign
        serves. day_of_week: MONDAY..SUNDAY. minutes: 0/15/30/45.

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
        geo_exclude_ids=geo_exclude_ids,
        language_ids=language_ids,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
        ad_schedule=ad_schedule,
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
    geo_exclude_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool | None = None,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft an update to an existing campaign — returns a PREVIEW, does NOT apply.

    Only include the parameters you want to change. Omit the rest. List-typed
    fields (geo_target_ids, geo_exclude_ids, language_ids, ad_schedule) follow
    REPLACE semantics: when provided, all existing entries of that type are
    removed and the new list is added. Pass an empty list (e.g.
    ``geo_exclude_ids=[]``) to clear that field.

    campaign_id: the numeric ID of the campaign to update (required)
    bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
                      MAXIMIZE_CONVERSION_VALUE | TARGET_SPEND | MANUAL_CPC
    target_cpa: required if bidding_strategy is TARGET_CPA (in account currency)
    target_roas: required if bidding_strategy is TARGET_ROAS
    daily_budget: new daily budget in account currency
    geo_target_ids: REPLACES all geo targets.
    geo_exclude_ids: REPLACES all negative-location geo criteria.
    language_ids: REPLACES all language targets.
    search_partners_enabled: include ads on Search partners
    display_network_enabled: enable Search campaign display expansion
    display_expansion_enabled: alias for display_network_enabled
    max_cpc: Maximize Clicks CPC cap when bidding_strategy is TARGET_SPEND
    ad_schedule: REPLACES all schedule criteria. Each entry: {day_of_week,
        start_hour, end_hour, start_minute, end_minute}.

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
        geo_exclude_ids=geo_exclude_ids,
        language_ids=language_ids,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
        ad_schedule=ad_schedule,
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
def update_responsive_search_ad(
    ad_id: str,
    customer_id: str = "",
    final_url: str = "",
    path1: str = "",
    path2: str = "",
    clear_path1: bool = False,
    clear_path2: bool = False,
) -> dict:
    """Update mutable fields on an existing RSA — returns a PREVIEW.

    In-place edit of an RSA without creating a new ad (preserves serving
    history and avoids the learning-period reset that pause-old + create-new
    triggers). Google Ads API v23 lets you mutate ``final_urls``, ``path1``,
    and ``path2`` on existing RSAs; ``headlines`` and ``descriptions`` remain
    immutable — for those you still need draft_responsive_search_ad +
    pause_entity on the old ad.

    Argument semantics:
        - ``final_url``: empty -> no change; non-empty -> replaces final URL
        - ``path1`` / ``path2``: empty -> no change; non-empty -> sets value
        - ``clear_path1`` / ``clear_path2``: True -> set to empty string

    At least one mutation must be requested. Call confirm_and_apply with the
    returned plan_id to execute.
    """
    from adloop.ads.write import update_responsive_search_ad as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        ad_id=ad_id,
        final_url=final_url,
        path1=path1,
        path2=path2,
        clear_path1=clear_path1,
        clear_path2=clear_path2,
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
    callouts: list[str],
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """Draft callout assets — returns a PREVIEW.

    If ``campaign_id`` is empty, callouts attach at the customer/account
    level (CustomerAsset) and apply to all eligible campaigns. Pass a
    campaign_id to scope them to one campaign instead.
    """
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
    snippets: list[dict],
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """Draft structured snippet assets — returns a PREVIEW.

    If ``campaign_id`` is empty, snippets attach at the customer/account
    level. Pass a campaign_id to scope to one campaign.
    """
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
    image_paths: list[str],
    campaign_id: str = "",
    customer_id: str = "",
    field_types: list[str] | None = None,
) -> dict:
    """Draft image assets from local PNG, JPEG, or GIF files — returns a PREVIEW.

    Scope:
        - If ``campaign_id`` is empty (default), images attach at the
          customer/account level (CustomerAsset).
        - If ``campaign_id`` is provided, images attach at that campaign
          (CampaignAsset).

    Field type:
        Each image's AssetFieldType is auto-detected from its aspect
        ratio (with a 'logo' filename hint):
          1:1 → SQUARE_MARKETING_IMAGE (or BUSINESS_LOGO if 'logo' in name)
          1.91:1 → MARKETING_IMAGE
          4:1 → LANDSCAPE_LOGO (logo hint required)
          4:5 → PORTRAIT_MARKETING_IMAGE
        Pass ``field_types`` (one entry per image, same order as
        image_paths) to override. Valid override values:
        MARKETING_IMAGE, SQUARE_MARKETING_IMAGE,
        PORTRAIT_MARKETING_IMAGE, TALL_PORTRAIT_MARKETING_IMAGE,
        LOGO, LANDSCAPE_LOGO, BUSINESS_LOGO.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_image_assets as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        image_paths=image_paths,
        field_types=field_types,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_business_name_asset(
    business_name: str,
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """Draft a BUSINESS_NAME text asset — returns a PREVIEW.

    Creates a TEXT asset with the business name and links it as
    ``BUSINESS_NAME`` so Google can show the brand name alongside ads.

    Scope:
        - If ``campaign_id`` is empty (default), the asset attaches at the
          customer/account level (CustomerAsset) and applies to all
          eligible campaigns.
        - If ``campaign_id`` is provided, it scopes to that one campaign.

    business_name: max 25 characters per Google Ads policy.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_business_name_asset as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        business_name=business_name,
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
    sitelinks: list[dict],
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """Draft sitelink extensions — returns a PREVIEW.

    Sitelinks appear as additional links below your ad, increasing click area
    and directing users to specific pages.

    Scope:
        - If ``campaign_id`` is empty, sitelinks attach at the customer/account
          level (CustomerAsset) and apply to all eligible campaigns.
        - If ``campaign_id`` is provided, sitelinks attach at the campaign level
          (CampaignAsset).

    sitelinks: list of dicts, each with:
        - link_text (str, required, max 25 chars) — the clickable text shown
        - final_url (str, required) — destination URL for this sitelink
        - description1 (str, optional, max 35 chars) — first description line
        - description2 (str, optional, max 35 chars) — second description line

    Google recommends at least 4 sitelinks. Fewer than 2 may not show.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_sitelinks as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        sitelinks=sitelinks,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_call_asset(
    phone_number: str,
    country_code: str = "US",
    campaign_id: str = "",
    customer_id: str = "",
    call_conversion_action_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft a call asset (phone extension) — returns a PREVIEW.

    Scope:
        - If ``campaign_id`` is empty, the call asset is added at the
          customer/account level via CustomerAsset.
        - If ``campaign_id`` is provided, the call asset is scoped to that
          single campaign via CampaignAsset.

    phone_number: human-formatted or E.164 (e.g. "(916) 339-3676" or
        "+19163393676"). Auto-normalized to E.164 using country_code when
        no leading '+' is present.
    country_code: ISO-3166 alpha-2 (default "US"). Used only for E.164
        normalization when phone_number lacks a leading '+'.
    call_conversion_action_id: optional Google Ads conversion action ID to
        use for call-conversion counting (e.g. count calls ≥60 sec).
    ad_schedule: optional list limiting hours when the call asset shows.
        Each entry: {day_of_week: MONDAY..SUNDAY, start_hour: 0-23,
        end_hour: 0-24, start_minute: 0/15/30/45, end_minute: 0/15/30/45}.

    NOTE: Google Ads requires manual phone-number verification before the
    call asset can serve. The asset is created via API but won't show in
    ads until verification is completed in Tools → Assets → Calls.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_call_asset as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        phone_number=phone_number,
        country_code=country_code,
        campaign_id=campaign_id,
        call_conversion_action_id=call_conversion_action_id,
        ad_schedule=ad_schedule,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_location_asset(
    business_profile_account_id: str,
    asset_set_name: str = "",
    campaign_id: str = "",
    customer_id: str = "",
    label_filters: list[str] | None = None,
    listing_id_filters: list[str] | None = None,
) -> dict:
    """Draft a Google Business Profile-backed location AssetSet — PREVIEW.

    Creates a LOCATION_SYNC AssetSet that pulls business locations from a
    linked Google Business Profile (GBP) and exposes them as location assets
    in ads (used by location extensions and the local map pin).

    business_profile_account_id: numeric GBP/LBC account ID. Find it in
        the Google Business Profile admin URL or settings.
    asset_set_name: optional human-readable name. Defaults to
        "GBP Locations - <id>".
    campaign_id: empty (default) for customer/account-level scope; pass a
        campaign ID to limit the location assets to one campaign.
    label_filters: optional list of GBP location labels to limit sync.
    listing_id_filters: optional list of GBP listing IDs to limit sync.

    REQUIRED PRECONDITION: the Google Business Profile must already be
    linked at Tools → Linked accounts → Business Profile in Google Ads.
    If it isn't, this tool will fail at apply time with a clear error.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_location_asset as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        business_profile_account_id=business_profile_account_id,
        asset_set_name=asset_set_name,
        campaign_id=campaign_id,
        label_filters=label_filters,
        listing_id_filters=listing_id_filters,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_promotion(
    promotion_target: str,
    final_url: str,
    money_off: float = 0,
    percent_off: float = 0,
    currency_code: str = "USD",
    promotion_code: str = "",
    orders_over_amount: float = 0,
    occasion: str = "",
    discount_modifier: str = "",
    language_code: str = "en",
    start_date: str = "",
    end_date: str = "",
    redemption_start_date: str = "",
    redemption_end_date: str = "",
    campaign_id: str = "",
    customer_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft a promotion extension asset — returns a PREVIEW.

    Creates a PromotionAsset and links it at campaign or customer scope.
    Exactly one of money_off / percent_off must be provided.

    Scope:
        - campaign_id provided  → CampaignAsset link.
        - campaign_id empty     → CustomerAsset link (account-level, applies
          to every eligible campaign automatically).

    Required:
        promotion_target: what the promotion is for, e.g. "Window Tint"
            (max 20 chars; this is the label Google shows in the ad).
        final_url: landing page (must return 2xx/3xx — validated).
        money_off OR percent_off: the discount amount.

    Optional:
        currency_code: ISO 4217 (default USD).
        promotion_code: optional coupon code (max 15 chars).
        orders_over_amount: minimum order amount that unlocks the promo.
        occasion: optional event tag — BLACK_FRIDAY, CYBER_MONDAY,
            CHRISTMAS, NEW_YEARS, MOTHERS_DAY, FATHERS_DAY, BACK_TO_SCHOOL,
            HALLOWEEN, SUMMER_SALE, WINTER_SALE, etc. Leave blank for
            always-on.
        discount_modifier: "UP_TO" surfaces "Up to $X off" instead of
            "$X off". Leave blank for plain.
        language_code: BCP-47 (default "en").
        start_date / end_date: YYYY-MM-DD.
        redemption_start_date / redemption_end_date: YYYY-MM-DD.
        ad_schedule: optional list — see add_ad_schedule for shape.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_promotion as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        promotion_target=promotion_target,
        final_url=final_url,
        money_off=money_off,
        percent_off=percent_off,
        currency_code=currency_code,
        promotion_code=promotion_code,
        orders_over_amount=orders_over_amount,
        occasion=occasion,
        discount_modifier=discount_modifier,
        language_code=language_code,
        start_date=start_date,
        end_date=end_date,
        redemption_start_date=redemption_start_date,
        redemption_end_date=redemption_end_date,
        campaign_id=campaign_id,
        ad_schedule=ad_schedule,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def update_promotion(
    asset_id: str,
    promotion_target: str,
    final_url: str,
    money_off: float = 0,
    percent_off: float = 0,
    currency_code: str = "USD",
    promotion_code: str = "",
    orders_over_amount: float = 0,
    occasion: str = "",
    discount_modifier: str = "",
    language_code: str = "en",
    start_date: str = "",
    end_date: str = "",
    redemption_start_date: str = "",
    redemption_end_date: str = "",
    campaign_id: str = "",
    customer_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Update a promotion via swap — returns a PREVIEW.

    PromotionAsset fields are immutable once created, so "update" is a SWAP:
        1. Create a new PromotionAsset with the new values.
        2. Link it at the same scope.
        3. Unlink the old asset.

    The old Asset row stays in the account (orphaned) — Google Ads API
    does not support hard-deleting Asset rows.

    asset_id: numeric ID of the existing PromotionAsset (find via
        SELECT asset.id, asset.promotion_asset.promotion_target FROM asset
        WHERE asset.type = 'PROMOTION').
    campaign_id: pass to scope BOTH the new and old links to that
        campaign. Empty for customer/account-level scope.

    All other parameters: see draft_promotion.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import update_promotion as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        asset_id=asset_id,
        campaign_id=campaign_id,
        promotion_target=promotion_target,
        final_url=final_url,
        money_off=money_off,
        percent_off=percent_off,
        currency_code=currency_code,
        promotion_code=promotion_code,
        orders_over_amount=orders_over_amount,
        occasion=occasion,
        discount_modifier=discount_modifier,
        language_code=language_code,
        start_date=start_date,
        end_date=end_date,
        redemption_start_date=redemption_start_date,
        redemption_end_date=redemption_end_date,
        ad_schedule=ad_schedule,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def link_asset_to_customer(
    links: list[dict],
    customer_id: str = "",
) -> dict:
    """Link EXISTING assets to the customer (account) — returns a PREVIEW.

    Use this to "promote" assets that already exist in the account
    (typically attached to legacy campaigns) so they apply at the account
    level and inherit to every eligible campaign automatically.

    Unlike draft_image_assets / draft_callouts / etc., this does NOT
    create new Asset rows — it only adds CustomerAsset link rows
    pointing to assets you already have.

    Find candidate asset_ids via run_gaql:
        SELECT asset.id, asset.type, asset.name FROM asset

    links: list of dicts, each with:
        - asset_id (str, required) — numeric asset ID
        - field_type (str, required) — AssetFieldType enum value, e.g.
          BUSINESS_LOGO, AD_IMAGE, MARKETING_IMAGE, SQUARE_MARKETING_IMAGE,
          BUSINESS_NAME, SITELINK, CALLOUT, CALL, PROMOTION

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import link_asset_to_customer as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        links=links,
    )


# ---------------------------------------------------------------------------
# Conversion Actions (create / update / remove)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
@_safe
def draft_create_conversion_action(
    name: str,
    type_: str,
    category: str = "DEFAULT",
    default_value: float = 0,
    currency_code: str = "USD",
    always_use_default_value: bool = False,
    counting_type: str = "ONE_PER_CLICK",
    phone_call_duration_seconds: int = 0,
    primary_for_goal: bool = True,
    include_in_conversions_metric: bool = True,
    click_through_window_days: int = 0,
    view_through_window_days: int = 0,
    attribution_model: str = "",
    customer_id: str = "",
) -> dict:
    """Draft a new ConversionAction — returns a PREVIEW.

    type_ values: AD_CALL, WEBSITE_CALL, WEBPAGE, WEBPAGE_CODELESS,
        GOOGLE_ANALYTICS_4_CUSTOM, GOOGLE_ANALYTICS_4_PURCHASE,
        UPLOAD_CALLS, UPLOAD_CLICKS, FLOODLIGHT_ACTION, STORE_VISITS,
        STORE_SALES_DIRECT_UPLOAD.

    category: DEFAULT, PHONE_CALL_LEAD, SUBMIT_LEAD_FORM, PURCHASE,
        SIGNUP, LEAD, CONTACT, GET_DIRECTIONS, ENGAGEMENT, etc.

    For WEBSITE_CALL with GFN, set:
        type_="WEBSITE_CALL", category="PHONE_CALL_LEAD",
        phone_call_duration_seconds=90, default_value=250

    For AD_CALL (calls from Call assets in ads), set:
        type_="AD_CALL", category="PHONE_CALL_LEAD",
        default_value=250, counting_type="ONE_PER_CLICK"
        (the duration threshold for AD_CALL lives on the Call ASSET,
         not on the conversion action — see update_call_asset)

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.conversion_actions import draft_create_conversion_action as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        name=name,
        type_=type_,
        category=category,
        default_value=default_value,
        currency_code=currency_code,
        always_use_default_value=always_use_default_value,
        counting_type=counting_type,
        phone_call_duration_seconds=phone_call_duration_seconds,
        primary_for_goal=primary_for_goal,
        include_in_conversions_metric=include_in_conversions_metric,
        click_through_window_days=click_through_window_days,
        view_through_window_days=view_through_window_days,
        attribution_model=attribution_model,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_update_conversion_action(
    conversion_action_id: str,
    name: str = "",
    primary_for_goal: bool | None = None,
    default_value: float = 0,
    currency_code: str = "",
    always_use_default_value: bool | None = None,
    counting_type: str = "",
    phone_call_duration_seconds: int = 0,
    include_in_conversions_metric: bool | None = None,
    click_through_window_days: int = 0,
    view_through_window_days: int = 0,
    attribution_model: str = "",
    customer_id: str = "",
) -> dict:
    """Draft a partial UPDATE of an existing ConversionAction — PREVIEW.

    Pass only the fields you want to change. Empty strings/0/None mean
    "do not change this field".

    Common workflows:
      - Rename: name="Calls from Ads (>=90s)"
      - Demote to Secondary: primary_for_goal=False
      - Set value: default_value=250, currency_code="USD",
        always_use_default_value=True
      - Set call duration threshold: phone_call_duration_seconds=90
      - Switch counting: counting_type="ONE_PER_CLICK"

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.conversion_actions import draft_update_conversion_action as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        conversion_action_id=conversion_action_id,
        name=name,
        primary_for_goal=primary_for_goal,
        default_value=default_value,
        currency_code=currency_code,
        always_use_default_value=always_use_default_value,
        counting_type=counting_type,
        phone_call_duration_seconds=phone_call_duration_seconds,
        include_in_conversions_metric=include_in_conversions_metric,
        click_through_window_days=click_through_window_days,
        view_through_window_days=view_through_window_days,
        attribution_model=attribution_model,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def draft_remove_conversion_action(
    conversion_action_id: str,
    customer_id: str = "",
) -> dict:
    """Draft a REMOVAL of a ConversionAction — returns PREVIEW (irreversible).

    Removed conversion actions stop counting. Historical data is preserved.

    Note: SMART_CAMPAIGN_* and GOOGLE_HOSTED types reject removal with
    MUTATE_NOT_ALLOWED — those are auto-managed by Google.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.conversion_actions import draft_remove_conversion_action as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        conversion_action_id=conversion_action_id,
    )


# ---------------------------------------------------------------------------
# Asset in-place updates (call asset, sitelink, callout)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
@_safe
def update_call_asset(
    asset_id: str,
    phone_number: str = "",
    country_code: str = "",
    call_conversion_action_id: str = "",
    call_conversion_reporting_state: str = "",
    ad_schedule: list[dict] | None = None,
    customer_id: str = "",
) -> dict:
    """Update an existing CallAsset in place — returns a PREVIEW.

    Common use case: re-point a Call asset at a specific conversion action
    (e.g. 'Calls from Ads (>=90s)') with USE_RESOURCE_LEVEL.

    Fields:
        phone_number: human or E.164 (auto-normalized)
        country_code: ISO-3166 alpha-2 (default US when normalizing)
        call_conversion_action_id: numeric conversion action ID
        call_conversion_reporting_state: DISABLED |
            USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION |
            USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        ad_schedule: optional schedule list (replaces existing)

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import update_call_asset as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        asset_id=asset_id,
        phone_number=phone_number,
        country_code=country_code,
        call_conversion_action_id=call_conversion_action_id,
        call_conversion_reporting_state=call_conversion_reporting_state,
        ad_schedule=ad_schedule,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def update_sitelink(
    asset_id: str,
    link_text: str = "",
    final_url: str = "",
    description1: str = "",
    description2: str = "",
    customer_id: str = "",
) -> dict:
    """Update an existing SitelinkAsset in place — returns a PREVIEW.

    Pass only the fields you want to change. Empty string = "do not change".
    URL is validated for reachability when provided.
    """
    from adloop.ads.write import update_sitelink as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        asset_id=asset_id,
        link_text=link_text,
        final_url=final_url,
        description1=description1,
        description2=description2,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def update_callout(
    asset_id: str,
    callout_text: str,
    customer_id: str = "",
) -> dict:
    """Update an existing CalloutAsset's text in place — returns a PREVIEW.

    callout_text: new callout text (max 25 chars).
    """
    from adloop.ads.write import update_callout as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        asset_id=asset_id,
        callout_text=callout_text,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def add_ad_schedule(
    campaign_id: str,
    schedule: list[dict],
    customer_id: str = "",
) -> dict:
    """Draft ad schedule criteria for a campaign — returns a PREVIEW.

    Adds AdScheduleInfo CampaignCriterion records so the campaign only
    serves during the specified hours/days. Hours follow the account's
    configured time zone.

    schedule: list of dicts:
        - day_of_week: MONDAY..SUNDAY
        - start_hour: 0..23
        - end_hour: 0..24 (must be > start)
        - start_minute / end_minute: 0, 15, 30, or 45 (default 0)

    Note: this tool is additive. Existing schedule criteria are not
    removed; if you need a clean slate, use remove_entity on the existing
    schedule criteria first.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import add_ad_schedule as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        schedule=schedule,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def add_geo_exclusions(
    campaign_id: str,
    geo_target_ids: list[str],
    customer_id: str = "",
) -> dict:
    """Draft negative geo CampaignCriterion records — returns a PREVIEW.

    Adds excluded locations so the campaign does not serve to users in
    those geos. Use this when you have a broad include list but specific
    sub-geos to suppress (e.g. include "California" but exclude "Los
    Angeles").

    geo_target_ids: list of geoTargetConstant IDs. Look them up via:
        SELECT geo_target_constant.id, geo_target_constant.name
        FROM geo_target_constant
        WHERE geo_target_constant.country_code = 'US'
          AND geo_target_constant.name = 'Los Angeles'

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import add_geo_exclusions as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        geo_target_ids=geo_target_ids,
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
