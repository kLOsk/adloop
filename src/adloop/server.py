"""AdLoop MCP server — FastMCP instance with all tool registrations."""

from __future__ import annotations

import functools
import json
from typing import Annotated, Callable

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator

from adloop import diagnostics
from adloop.runtime import current_config

_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

# Toolset taxonomy: every tool carries exactly one of these tags (or "core",
# which survives every ADLOOP_TOOLSETS selection — health_check and
# confirm_and_apply must always be callable). Shared contract with AdLoop
# Cloud: the Laravel dashboard (config/toolsets.php) and the runtime's
# per-key filtering pin this list in their test suites.
TOOLSETS: dict[str, str] = {
    "ads": "Google Ads reads, writes, and planning",
    "ga4": "Google Analytics reads and key events",
    "tracking": "Cross-channel attribution and tracking code",
    "gtm": "Google Tag Manager reads",
    "gsc": "Search Console reads",
    "web": "PageSpeed / web performance",
    "merchant": "Merchant Center reads",
}


def _coerce_json_string_to_list(value):
    """Decode a JSON-array-shaped string into a native list.

    Some MCP clients (Cowork at the time of writing — see issue #28)
    serialize list-typed tool arguments as JSON-encoded strings rather
    than native arrays. Pydantic v2 rejects those calls with
    ``Input should be a valid list`` because string→list isn't a default
    coercion. This validator detects the pattern (``"[...]"``) and decodes
    it to an actual list so the standard list validator can proceed.

    Anything that isn't a JSON-encoded list passes through untouched —
    so legitimate native arrays and ``None`` are unaffected. The fix is
    invisible to the JSON schema (``Annotated`` metadata isn't included
    in schema generation), so well-behaved clients keep sending arrays.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        if isinstance(decoded, list):
            return decoded
    return value


# JSON-string-tolerant list aliases. Applied to every tool parameter that
# accepts a list so the server works equally well against clients that
# send native arrays and clients that pre-serialize them as JSON strings.
_StrList = Annotated[list[str], BeforeValidator(_coerce_json_string_to_list)]
_StrListOpt = Annotated[
    list[str] | None, BeforeValidator(_coerce_json_string_to_list)
]
_DictList = Annotated[list[dict], BeforeValidator(_coerce_json_string_to_list)]
_DictListOpt = Annotated[
    list[dict] | None, BeforeValidator(_coerce_json_string_to_list)
]
_StrOrDictList = Annotated[
    list[str | dict], BeforeValidator(_coerce_json_string_to_list)
]

def _build_orchestration_instructions() -> str:
    """Compact orchestration hint sent via MCP ``InitializeResult.instructions``.

    Per the MCP spec, ``instructions`` is described as "a hint to the model" —
    not a place to dump a 50KB manual. So we send a curated, ~500-token subset
    covering the rules that matter most when used **without** the full
    orchestration rules loaded (e.g. in MCP clients that don't pick up project
    rules). The full ruleset stays canonical at:

      - ``.cursor/rules/adloop.mdc`` (Cursor — auto-loaded as a workspace rule)
      - ``.claude/rules/adloop.md`` (Claude Code in this repo)
      - ``~/.claude/CLAUDE.md`` (after the user runs ``adloop install-rules``)

    Clients that honor ``instructions`` (Claude Code, VSCode Copilot, Goose,
    Cursor v1.6+) will inject this hint into the LLM's system prompt
    automatically — so even users running AdLoop without any per-project
    rules file get the absolute must-knows around safety, dry-run defaults,
    and the most common cost-burning mistakes.
    """
    return (
        "AdLoop connects Google Ads + Google Analytics (GA4) + your codebase. "
        "These are the *minimum* orchestration rules — the full ruleset lives "
        "in `.cursor/rules/adloop.mdc` or `~/.claude/CLAUDE.md` (run "
        "`adloop install-rules` to install globally). Read these before using "
        "any write tool.\n\n"
        "SAFETY (always):\n"
        "- Every write tool returns a PREVIEW with a `plan_id`. Show the "
        "preview to the user and wait for explicit approval before calling "
        "`confirm_and_apply`.\n"
        "- Default to `dry_run=true` on `confirm_and_apply`. Only set "
        "`dry_run=false` after the user explicitly approves the preview. "
        "`require_dry_run` in config can override this.\n"
        "- Respect the config's `max_daily_budget` cap.\n"
        "- New campaigns and RSAs are created PAUSED. The user must enable "
        "them after review.\n"
        "- One change at a time — don't batch unrelated writes.\n\n"
        "PRE-WRITE CHECKS (before any `draft_*`):\n"
        "- BROAD match keywords require Smart Bidding (MAXIMIZE_CONVERSIONS, "
        "tCPA, tROAS). Refuse BROAD on MANUAL_CPC. This is the #1 cause of "
        "wasted ad spend.\n"
        "- Verify `final_url` exists before creating ads or sitelinks. URLs "
        "to 404 pages destroy quality score.\n"
        "- If a campaign has zero conversions and high spend, fix tracking "
        "before adding more ads/keywords. Don't just throw budget at it.\n"
        "- If keyword quality scores are <5, fix ad relevance and landing "
        "pages before adding more keywords.\n\n"
        "DATA LITERACY:\n"
        "- Ads clicks > GA4 sessions is normal in EU markets due to GDPR "
        "consent rejection (typically 30-70% of users opt out). It's not a "
        "tracking bug. Use `analyze_campaign_conversions` and "
        "`attribution_check` — they factor this in.\n"
        "- `cost_micros / 1,000,000` = actual currency. Read tools "
        "auto-compute `metrics.cost`; only `run_gaql` returns raw micros.\n"
        "- New campaigns MUST have `geo_target_ids` and `language_ids` set. "
        "Untargeted campaigns waste budget.\n\n"
        "For full orchestration patterns (when to call which tools, GAQL "
        "reference, language-specific copy guidance, PMax handling, "
        "shared-set lifecycle, RSA pinning trade-offs), see the canonical "
        "rules file."
    )


def _gtm_defaults(account_id: str, container_id: str) -> tuple[str, str]:
    """Fall back to configured GTM defaults when ids are omitted."""
    cfg = current_config().gtm
    account_id = account_id or cfg.account_id
    container_id = container_id or cfg.container_id
    if not account_id or not container_id:
        raise ValueError(
            "gtm_account_id and gtm_container_id are required — pass them "
            "explicitly (see list_gtm_accounts / list_gtm_containers) or set "
            "gtm.account_id / gtm.container_id in the config."
        )
    return account_id, container_id


mcp = FastMCP(
    "AdLoop",
    instructions=_build_orchestration_instructions(),
)


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
        from adloop.runtime import deployment_mode

        return {
            "error": "Authentication failed — OAuth token expired or revoked.",
            "hint": (
                "Reconnect Google in your AdLoop Cloud dashboard "
                "(Settings → Google), then retry."
                if deployment_mode() == "server"
                else "Delete ~/.adloop/token.json and re-run any tool to "
                "trigger re-authorization. If this keeps happening, "
                "publish the GCP consent screen to 'In production'."
            ),
            "auth_error": "INVALID_GRANT",
        }

    if "deleted_client" in err_lower or "invalid_client" in err_lower:
        return {
            "error": (
                "Authentication failed — the OAuth client behind your stored "
                "credentials no longer exists or is invalid."
            ),
            "hint": (
                "If you set up AdLoop before v0.10 with its bundled "
                "credentials: that shared Google Cloud project has been "
                "retired. Fastest fix: AdLoop Cloud (https://getadloop.com) — "
                "connect Google in two clicks, no Google Cloud project "
                "needed. To stay self-hosted, run `adloop init` and supply "
                "your own OAuth credentials. If you already use your own "
                "project, verify client id/secret in "
                "~/.adloop/credentials.json."
            ),
            "auth_error": "OAUTH_CLIENT_DELETED_OR_INVALID",
        }

    if (
        "insufficient authentication scopes" in err_lower
        or "insufficient_scope" in err_lower
        or "act_insufficient_permission" in err_lower
    ):
        from adloop.runtime import deployment_mode as _deployment_mode

        return {
            "error": (
                "Authorization failed — the stored OAuth token lacks a "
                "required scope."
            ),
            "hint": (
                "Your Google connection predates a newer permission (e.g. "
                "Tag Manager or Search Console). Reconnect Google in your "
                "AdLoop Cloud dashboard (Settings → Google) and leave all "
                "permission boxes ticked."
                if _deployment_mode() == "server"
                else "This token was granted before a newer API scope was "
                "added (e.g. Tag Manager or Search Console). Delete "
                "~/.adloop/token.json and re-run any tool to re-consent "
                "with the full scope set. Also ensure the corresponding "
                "API is enabled in your GCP project."
            ),
            "auth_error": "INSUFFICIENT_SCOPES",
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


@mcp.tool(annotations=_READONLY, tags={"core"})
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

        result = _ga4_test(current_config())
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
        mcc_id = current_config().ads.login_customer_id or current_config().ads.customer_id
        execute_query(
            current_config(),
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


@mcp.tool(annotations=_READONLY, tags={"ga4"})
@_safe
def get_account_summaries() -> dict:
    """List all GA4 accounts and properties accessible by the authenticated user.

    Use this as the first step to discover which GA4 properties are available.
    Returns account names, property names, and property IDs.
    """
    from adloop.ga4.reports import get_account_summaries as _impl

    return _impl(current_config())


@mcp.tool(annotations=_READONLY, tags={"ga4"})
@_safe
def run_ga4_report(
    dimensions: _StrListOpt = None,
    metrics: _StrListOpt = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    property_id: str = "",
    limit: int = 100,
    dimension_filter: dict[str, str] = {},
) -> dict:
    """Run a custom GA4 report with specified dimensions, metrics, and date range.

    Common dimensions: date, pagePath, sessionSource, sessionMedium, country, deviceCategory, eventName
    Common metrics: sessions, totalUsers, newUsers, screenPageViews, conversions, eventCount, bounceRate

    Date formats: "today", "yesterday", "7daysAgo", "28daysAgo", "90daysAgo", or "YYYY-MM-DD".
    If property_id is empty, uses the default from config.

    dimension_filter: optional dict of dimension_name -> exact match value to
    filter results server-side. Multiple entries are combined with AND logic.
    Example: {"sessionSource": "google", "sessionMedium": "cpc"} returns only
    paid search traffic.
    """
    from adloop.ga4.reports import run_ga4_report as _impl

    return _impl(
        current_config(),
        property_id=property_id or current_config().ga4.property_id,
        dimensions=dimensions,
        metrics=metrics,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        limit=limit,
        dimension_filter=dimension_filter,
    )


@mcp.tool(annotations=_READONLY, tags={"ga4"})
@_safe
def run_realtime_report(
    dimensions: _StrListOpt = None,
    metrics: _StrListOpt = None,
    property_id: str = "",
) -> dict:
    """Run a GA4 realtime report showing current active users and events.

    Useful for checking if tracking is firing correctly after code changes.
    Common dimensions: unifiedScreenName, eventName, country, deviceCategory
    Common metrics: activeUsers, eventCount
    """
    from adloop.ga4.reports import run_realtime_report as _impl

    return _impl(
        current_config(),
        property_id=property_id or current_config().ga4.property_id,
        dimensions=dimensions,
        metrics=metrics,
    )


@mcp.tool(annotations=_READONLY, tags={"ga4"})
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
        current_config(),
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


# ---------------------------------------------------------------------------
# Google Search Console Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"gsc"})
@_safe
def list_gsc_sites() -> dict:
    """List all Google Search Console properties the authenticated user can access.

    Use this first to discover which site URLs are available before running
    search analytics reports. Returns the site URL and permission level for
    each property.
    """
    from adloop.gsc.reports import list_gsc_sites as _impl

    return _impl(current_config())


@mcp.tool(annotations=_READONLY, tags={"gsc"})
@_safe
def run_gsc_report(
    site_url: str = "",
    dimensions: _StrListOpt = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    limit: int = 100,
    search_type: str = "web",
    dimension_filter_groups: _DictListOpt = None,
) -> dict:
    """Run a Google Search Console search analytics report.

    Returns clicks, impressions, CTR, and average position broken down by
    the requested dimensions. Useful for diagnosing organic traffic drops,
    finding keyword opportunities, and cross-referencing with GA4 and Ads data.

    site_url: the GSC property URL (e.g. "https://example.com/" or
        "sc-domain:example.com"). Defaults to gsc.site_url in config.yaml.
    dimensions: one or more of ["query", "page", "country", "device", "date"].
        Defaults to ["query"].
    date_range_start / date_range_end: ISO dates (YYYY-MM-DD) or relative
        values like "7daysAgo", "30daysAgo", "today".
    search_type: "web" (default), "image", "video", "news", "discover",
        or "googleNews".
    dimension_filter_groups: optional GSC DimensionFilterGroup list to filter
        by query, page, country, or device. Example:
        [{"filters": [{"dimension": "query", "operator": "contains",
                       "expression": "analytics"}]}]
    limit: maximum rows to return (default 100, max 25000).
    """
    from adloop.gsc.reports import run_gsc_report as _impl

    return _impl(
        current_config(),
        site_url=site_url,
        dimensions=dimensions,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        limit=limit,
        search_type=search_type,
        dimension_filter_groups=dimension_filter_groups,
    )


# ---------------------------------------------------------------------------
# Client Reporting-Target Resolution
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"core"})
@_safe
def resolve_client_google_targets(client_id: str) -> dict:
    """Resolve a ClientBrain client to its Google reporting targets.

    Given a ClientBrain client id (uuid), returns that client's GA4 property id,
    GTM account + container id, and GSC site url — the ids to pass to the GA4,
    GTM, and Search Console report tools. Look the client id up first via the
    ClientBrain connector (find_client). Any target not configured for the
    client comes back null.

    client_id: the ClientBrain client id (uuid), e.g. from find_client.
    """
    from adloop.hosting.client_resolution import build_client_target_resolver

    resolver = build_client_target_resolver()
    if resolver is None:
        raise RuntimeError(
            "Client target resolution needs a database connection "
            "(ADLOOP_DATABASE_URL); it is unavailable in local mode."
        )
    targets = resolver(client_id)
    if targets is None:
        return {
            "client_id": client_id,
            "ga4_property_id": None,
            "gtm_account_id": None,
            "gtm_container_id": None,
            "gsc_site_url": None,
            "note": "No Google reporting targets are mapped for this client yet.",
        }
    return {"client_id": client_id, **targets}


# ---------------------------------------------------------------------------
# Google Ads Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"web"})
@_safe
def analyze_page_speed(url: str, strategy: str = "mobile") -> dict:
    """Run PageSpeed Insights for a landing page — Lighthouse + real-user data.

    Returns the performance score (0-100), lab Core Web Vitals (LCP, CLS,
    TBT, FCP), CrUX field data from real Chrome users where available
    (p75 LCP/INP/CLS + FAST/AVERAGE/SLOW ratings), and the top improvement
    opportunities with estimated savings.

    Use on ad final_urls: slow landing pages depress Quality Score and
    waste paid clicks. strategy: "mobile" (default — most paid traffic) or
    "desktop". Takes 10-30s; that is normal for a Lighthouse run.
    """
    from adloop.pagespeed import analyze_page_speed as _impl

    return _impl(current_config(), url=url, strategy=strategy)


@mcp.tool(annotations=_READONLY, tags={"merchant"})
@_safe
def list_merchant_accounts() -> dict:
    """List Google Merchant Center accounts the connected user can access.

    Call first to discover merchant IDs for get_merchant_feed_health.
    Distinguishes standalone accounts from aggregator (MCA) accounts.
    """
    from adloop.merchant.read import list_merchant_accounts as _impl

    return _impl(current_config())


@mcp.tool(annotations=_READONLY, tags={"merchant"})
@_safe
def get_merchant_feed_health(account_id: str) -> dict:
    """Merchant Center feed health — disapproved products + account issues.

    Disapproved feed items silently starve Shopping and Performance Max
    campaigns; this surfaces approved/pending/disapproved counts per
    reporting context (Shopping ads, free listings, ...), the top product
    issues by affected products (with documentation links), and
    account-level issues — CRITICAL ones stop offers serving entirely.

    account_id: numeric Merchant Center ID from list_merchant_accounts.
    Product-status data lags reality by ~30 minutes.
    """
    from adloop.merchant.read import get_merchant_feed_health as _impl

    return _impl(current_config(), account_id=account_id)


@mcp.tool(annotations=_READONLY, tags={"ads"})
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

    return _impl(current_config(), limit=limit)


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_campaign_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
) -> dict:
    """Get campaign-level performance metrics for a date range.

    Returns: campaign name, status, type, impressions, clicks, cost,
    conversions, CPA, ROAS, CTR for each campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.

    Set compact=true for audits/overviews on large accounts: returns
    account totals, status/type breakdowns, the top-10 spenders, and
    zero-conversion offenders instead of every row (~90% smaller).
    """
    from adloop.ads.read import get_campaign_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        compact=compact,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_ad_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
) -> dict:
    """Get ad-level performance data including headlines, descriptions, and metrics.

    Returns: ad type, headlines, descriptions, final URL, impressions,
    clicks, CTR, conversions, cost for each ad.

    Set compact=true for audits/overviews: returns totals, the top-10
    ads with headline/description COUNTS instead of full asset lists,
    plus incomplete-RSA and single-ad ad-group findings (~90% smaller).
    """
    from adloop.ads.read import get_ad_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        compact=compact,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_keyword_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
) -> dict:
    """Get keyword metrics including quality scores and competitive data.

    Returns: keyword text, match type, quality score, ad_group.id, ad_group.name,
    ad_group_criterion.criterion_id, impressions, clicks, CTR, CPC, cost,
    conversions for each keyword. Use ad_group.id and criterion_id to
    construct entity_id strings (e.g. "adGroupId~criterionId") for pause_entity.

    Set compact=true for audits/overviews: returns totals, match-type
    distribution, the top-10 spenders, low-quality-score keywords, and
    zero-conversion spenders instead of every row (~90% smaller).
    """
    from adloop.ads.read import get_keyword_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        compact=compact,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_search_terms(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
    compact: bool = False,
) -> dict:
    """Get search terms report — what users actually typed before clicking your ads.

    Critical for finding negative keyword opportunities and understanding user intent.
    Returns: search term, campaign_id, campaign_name, ad group, impressions,
    clicks, cost, conversions. Each row includes campaign.id so you can pass
    it directly to add_negative_keywords.

    campaign_id: optional filter to a specific campaign. When omitted, returns
    search terms across all campaigns.

    Set compact=true for audits/overviews: returns totals, the top-10
    terms by clicks, ready-made negative-keyword waste candidates
    (5+ clicks, zero conversions), and top converters (~90% smaller).
    """
    from adloop.ads.read import get_search_terms as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
        compact=compact,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


# ---------------------------------------------------------------------------
# Google Ads Insights Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_impression_share(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    level: str = "campaign",
) -> dict:
    """Get impression share metrics — how much of available search traffic you're capturing.

    Shows search impression share, budget-lost share, rank-lost share,
    top impression share, and absolute top impression share.

    level: "campaign" (default), "ad_group", or "keyword"
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_impression_share as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        level=level,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_change_history(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    resource_type: str = "",
    operation_type: str = "",
    limit: int = 100,
) -> dict:
    """Get recent account change history — who changed what and when.

    Critical for correlating performance shifts with account changes.
    Goes back up to 30 days (API limit). Default shows last 14 days.

    resource_type: filter by type — "CAMPAIGN", "AD_GROUP", "AD",
        "AD_GROUP_CRITERION", "CAMPAIGN_BUDGET", "BIDDING_STRATEGY"
    operation_type: filter by action — "CREATE", "UPDATE", "REMOVE"
    Date format: "YYYY-MM-DD". Empty = last 14 days.
    """
    from adloop.ads.read import get_change_history as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        resource_type=resource_type,
        operation_type=operation_type,
        limit=limit,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_device_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    level: str = "campaign",
) -> dict:
    """Get performance segmented by device — MOBILE, DESKTOP, TABLET.

    Essential for businesses where mobile intent differs
    dramatically from desktop. Shows clicks, cost, conversions, and
    conversion rate per device.

    level: "campaign" (default) or "ad_group"
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_device_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        level=level,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_location_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get performance segmented by geographic location.

    Shows impressions, clicks, cost, and conversions per location.
    Useful for identifying underperforming service areas or wasted spend
    outside the target service radius.

    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_location_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_quality_score_details(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
) -> dict:
    """Get keyword Quality Score with component breakdowns.

    Returns quality_score (1-10), creative_quality_score (ad relevance),
    post_click_quality_score (landing page), and search_predicted_ctr
    (expected CTR). Sorted by spend so high-cost low-QS keywords surface first.

    campaign_id: optional filter to a specific campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_quality_score_details as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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

    return _impl(current_config(), customer_id=customer_id or current_config().ads.customer_id)


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
    )


# ---------------------------------------------------------------------------
# Google Ads — Recommendations, Performance Max & Audience Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_recommendations(
    customer_id: str = "",
    recommendation_types: _StrListOpt = None,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        recommendation_types=recommendation_types,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
    PMax assets in the Google Ads API. Use get_detailed_asset_performance to
    see which asset combinations Google selects most — the closest proxy for
    individual asset quality.

    campaign_id: optional filter to a single PMax campaign.
    Includes by_status and by_field_type summaries.
    """
    from adloop.ads.pmax import get_asset_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_bid_strategy_status(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Get bid strategy type, system status, and learning state per campaign.

    Shows bidding_strategy_type (MAXIMIZE_CONVERSIONS, TARGET_CPA, etc.),
    bidding_strategy_system_status (LEARNING, ELIGIBLE, LIMITED, etc.),
    daily budget, and last-30-day metrics.

    Use this before recommending changes — don't edit campaigns in a learning phase.
    campaign_id: optional filter to a specific campaign.
    """
    from adloop.ads.read import get_bid_strategy_status as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_budget_pacing(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Get monthly budget pacing — spend-to-date, projected spend, pace percentage.

    Shows daily budget, month-to-date spend, daily average spend,
    projected month-end spend, and whether each campaign is over or under pace.

    campaign_id: optional filter to a specific campaign.
    """
    from adloop.ads.read import get_budget_pacing as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_ad_schedule_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
) -> dict:
    """Get performance by hour of day and day of week.

    Identifies peak and off-peak patterns. Important for service
    businesses (e.g. emergency plumber at 2am vs 2pm).

    Returns: campaign, day_of_week, hour, impressions, clicks, CTR, cost,
    conversions, conversion_rate, CPA for each time slot.

    campaign_id: optional filter to a specific campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_ad_schedule_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_auction_insights(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
) -> dict:
    """Get auction insights — competitor overlap rate, outranking share, position data.

    Shows which competitors appear alongside your ads and how often you
    outrank them. Requires an allowlisted Google Ads account — returns a
    helpful error if access is not available.

    campaign_id: optional filter to a specific campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.read import get_auction_insights as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_demographic_targeting(
    ad_group_id: str = "",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """List demographic targeting criteria (age, gender, parental status, income).

    Provide exactly one of `ad_group_id` or `campaign_id`. Returns each
    criterion's value, whether it's negative (excluded) or positive
    (narrowing), status, and a `remove_id` (composite resource ID) that
    can be passed directly to `remove_entity` with
    entity_type='ad_group_criterion' or 'campaign_criterion'.

    By default, Google Ads serves ads to ALL demographic segments — a
    criterion only appears here once you've actively excluded or narrowed.
    """
    from adloop.ads.read import get_demographic_targeting as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        campaign_id=campaign_id,
    )


# ---------------------------------------------------------------------------
# Cross-Reference Tools (GA4 + Ads Combined)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"tracking"})
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

    Returns one row per campaign (with campaign_id) including
    conversion_discrepancy_pct between Ads and GA4. When campaign_name is
    provided, filters to matching campaigns.

    Also returns non-paid channel conversion rates for comparison context.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.crossref import analyze_campaign_conversions as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_name=campaign_name,
    )


@mcp.tool(annotations=_READONLY, tags={"tracking"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"tracking"})
@_safe
def attribution_check(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
    conversion_events: _StrListOpt = None,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        conversion_events=conversion_events,
    )


# ---------------------------------------------------------------------------
# Performance Max Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_pmax_campaigns(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get Performance Max campaigns with PMax-specific settings and metrics.

    Returns: campaign id/name/status, bidding strategy, brand guidelines flag,
    daily budget, impressions, clicks, cost, conversions, conversions_value,
    CPA, and ROAS for each PMax campaign.

    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.pmax_read import get_pmax_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_pmax_channel_breakdown(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
) -> dict:
    """Get PMax spend/clicks/conversions per serving surface (Search/Display/YouTube/etc.).

    Uses segments.ad_network_type to break down where PMax actually served.
    Channel-level data is only reliable from 2025-06-01 onwards — earlier
    rows return MIXED. The tool emits a warning in insights when the date
    range overlaps that period.

    campaign_id: optional filter to a single campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.pmax_read import get_pmax_channel_breakdown as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_asset_groups(
    customer_id: str = "",
    campaign_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """List PMax asset groups with their final URLs, paths, ad strength, and metrics.

    Asset groups are the PMax equivalent of ad groups — each contains a bundle
    of assets (headlines, descriptions, images, logos, videos) that Google
    assembles dynamically. Ad strength values: POOR | AVERAGE | GOOD | EXCELLENT.

    campaign_id: optional filter to a single campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.pmax_read import get_asset_groups as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_asset_group_assets(
    customer_id: str = "",
    asset_group_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List individual assets in PMax asset groups with field type and policy review.

    Returns asset id/type, field_type (HEADLINE, DESCRIPTION, MARKETING_IMAGE,
    LOGO, YOUTUBE_VIDEO, etc.), status, policy_summary.review_status, and the
    text content, image URL, or YouTube video id/title/url depending on type.

    Note: the LOW/GOOD/BEST/PENDING performance_label was removed from
    asset_group_asset in Google Ads API v24. To judge per-asset performance
    now, query metrics directly via asset_field_type_view, or use
    get_asset_group_top_combinations to see which combinations actually serve.

    Provide either asset_group_id (single group) or campaign_id (all groups in
    the campaign). With both empty, returns all assets across all PMax campaigns.
    """
    from adloop.ads.pmax_read import get_asset_group_assets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_group_id=asset_group_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_asset_group_signals(
    customer_id: str = "",
    asset_group_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List audience and search-theme signals attached to PMax asset groups.

    Signals are not hard targeting — they are hints to Google's algorithm about
    who and what kind of search intent the asset group should match. Each row
    has signal_type = SEARCH_THEME or AUDIENCE.

    Provide either asset_group_id or campaign_id. Both empty returns all signals
    across all PMax campaigns.
    """
    from adloop.ads.pmax_read import get_asset_group_signals as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_group_id=asset_group_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_asset_group_top_combinations(
    customer_id: str = "",
    asset_group_id: str = "",
    campaign_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get the asset combinations Google has assembled at serve time for PMax.

    Each row's asset_group_top_combinations field is a repeated message of
    the assets that served together (headlines, descriptions, images, optional
    video). The view does NOT expose metrics in v24 — the API rejects any
    metrics.* field on this resource. Combinations come pre-ordered by Google
    by serving frequency.

    Provide either asset_group_id or campaign_id. Returns up to 50 rows.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.pmax_read import get_asset_group_top_combinations as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_group_id=asset_group_id,
        campaign_id=campaign_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def get_pmax_search_terms(
    campaign_id: str,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get aggregated search-term category insights for a Performance Max campaign.

    Note: PMax does NOT expose individual search terms (Google's design choice).
    This returns category labels (e.g. "Buy women's running shoes") aggregated
    across many real queries, with impression and click counts. The Google Ads
    API does NOT expose cost, conversions, or conversions_value on
    campaign_search_term_insight (PROHIBITED_METRIC_IN_SELECT_OR_WHERE_CLAUSE),
    so per-category cost is not available.

    campaign_id is REQUIRED — these insights are queried per-campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.ads.pmax_read import get_pmax_search_terms as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def analyze_pmax_performance(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Comprehensive PMax diagnostic — campaign + asset groups + assets + channels + GA4.

    Pulls everything you can inspect about Performance Max in one call:
    campaign metrics + bidding/brand-guidelines settings, every asset group
    with its ad strength and asset counts, channel-mix breakdown, and (when
    property_id is configured) GA4 paid sessions/conversions per campaign.

    Returns auto-generated insights[] flagging:
    - Asset groups with POOR or AVERAGE ad strength
    - Asset groups below the documented PMax asset-type minimums
    - Channel skew (e.g. >90% of spend on a single surface)
    - Zero-conversion campaigns despite spend
    - GDPR consent gaps (click-to-session ratio > 2:1)
    - Pre-2025-06-01 channel breakdown caveats

    campaign_id: optional filter — when provided, returns only that PMax campaign.
    Date format: "YYYY-MM-DD". Empty = last 30 days.
    """
    from adloop.crossref import analyze_pmax_performance as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
    )


# ---------------------------------------------------------------------------
# Performance Max Write Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_pmax_campaign(
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    geo_target_ids: list[str],
    language_ids: list[str],
    asset_group: dict,
    customer_id: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    final_url_suffix: str | None = None,
    brand_guidelines_enabled: bool = True,
) -> dict:
    """Draft a Performance Max campaign with its first asset group — returns PREVIEW.

    Creates: CampaignBudget + Campaign (PAUSED, no network_settings) + geo +
    language + AssetGroup (PAUSED) + Assets + AssetGroupAsset links + Signals
    in one atomic mutate. PMax requires this all-in-one shape.

    bidding_strategy: PMax accepts only Smart Bidding —
        MAXIMIZE_CONVERSIONS | MAXIMIZE_CONVERSION_VALUE | TARGET_CPA | TARGET_ROAS
    target_cpa / target_roas: required when bidding_strategy is the matching name.
    geo_target_ids / language_ids: REQUIRED — same constants as draft_campaign.

    brand_guidelines_enabled: defaults to True (matches Google's new PMax
        default). When True, BUSINESS_NAME and the first LOGO are also linked
        at the campaign level via CampaignAsset — required for the mutate to
        succeed on Brand-Guidelines-defaulted accounts. Pass False to opt
        out (assets stay at the asset-group level only).

    asset_group dict: see draft_pmax_campaign in pmax_write.py. Keys:
        - name (str): asset group name
        - final_urls (list[str]): at least one
        - path1, path2 (str, optional, <=15 chars)
        - headlines (list[str], 3-5, <=30 chars)
        - long_headlines (list[str], 1-5, <=90 chars)
        - descriptions (list[str], 2-5, <=90 chars)
        - business_name (str, <=25 chars)
        - marketing_image_assets (list[str]): resource_names of pre-uploaded
          1.91:1 images. PMax requires at least one.
        - square_marketing_image_assets (list[str]): resource_names of pre-
          uploaded 1:1 images. At least one required.
        - logo_assets (list[str]): resource_names of pre-uploaded logos. At
          least one required.
        - youtube_video_ids (list[str], optional)
        - search_themes (list[str], optional): SearchTheme signal phrases
        - audience_resource_names (list[str], optional): Audience resource_names

    NOTE: image/logo assets cannot be created inline through this MCP — use
    draft_image_asset to upload local JPG/PNG/GIF files, or paste resource_names
    of assets already uploaded via the Google Ads UI.

    Call confirm_and_apply with the returned plan_id to execute. The new
    campaign is created as PAUSED — enable_entity it after review.
    """
    from adloop.ads.pmax_write import draft_pmax_campaign as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        final_url_suffix=final_url_suffix,
        brand_guidelines_enabled=brand_guidelines_enabled,
        asset_group=asset_group,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_asset_group(
    campaign_id: str,
    asset_group: dict,
    customer_id: str = "",
) -> dict:
    """Draft a new asset group inside an existing PMax campaign — returns PREVIEW.

    asset_group has the same shape as draft_pmax_campaign's asset_group field.
    See that tool's docstring for the full schema.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.pmax_write import draft_asset_group as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        asset_group=asset_group,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_asset_group_assets(
    asset_group_id: str,
    customer_id: str = "",
    headlines: list[str] = [],
    long_headlines: list[str] = [],
    descriptions: list[str] = [],
    business_name: str = "",
    marketing_image_assets: list[str] = [],
    square_marketing_image_assets: list[str] = [],
    logo_assets: list[str] = [],
    youtube_video_ids: list[str] = [],
) -> dict:
    """Draft attaching new assets to an existing asset group — returns PREVIEW.

    Use this to add more headlines, descriptions, images, etc. to an asset
    group that already exists. Each text/youtube asset is created inline (one
    Asset.create + one AssetGroupAsset.create per item). Image and logo assets
    must already exist in the account — pass their resource_names.

    Char limits: headlines <=30, long_headlines <=90, descriptions <=90,
    business_name <=25.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.pmax_write import draft_asset_group_assets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_group_id=asset_group_id,
        headlines=headlines,
        long_headlines=long_headlines,
        descriptions=descriptions,
        business_name=business_name,
        marketing_image_assets=marketing_image_assets,
        square_marketing_image_assets=square_marketing_image_assets,
        logo_assets=logo_assets,
        youtube_video_ids=youtube_video_ids,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_image_asset(
    images: list[dict],
    customer_id: str = "",
) -> dict:
    """Draft uploading one or more local image files as Google Ads Assets — returns PREVIEW.

    PMax campaigns require pre-uploaded MARKETING_IMAGE, SQUARE_MARKETING_IMAGE,
    and LOGO assets that are referenced by resource_name. This tool reads
    local JPG / PNG / GIF files, validates them, and produces a ChangePlan that
    uploads bytes via AssetService.MutateAssets when confirm_and_apply is
    called. On apply, returns the new Asset resource_names so they can be
    passed to draft_pmax_campaign / draft_asset_group / draft_asset_group_assets.

    The same uploaded Asset can be linked as MARKETING_IMAGE (1.91:1, min
    600x314), SQUARE_MARKETING_IMAGE (1:1, min 300x300), or LOGO (1:1, min
    128x128) — Google checks the pixel dimensions against the slot at link
    time.

    Accepted formats: JPG (.jpg/.jpeg), PNG (.png), static GIF (.gif).
    Max file size: 5 MB per image. File bytes are read at apply time; the file
    must still exist when confirm_and_apply runs.

    images: list of dicts, each with:
        - file_path (str, REQUIRED): absolute path to a local image file
        - name (str, REQUIRED): the Asset display name in Google Ads

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.pmax_write import draft_image_asset as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        images=images,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_asset_group_signal(
    asset_group_id: str,
    customer_id: str = "",
    search_theme: str = "",
    audience_resource_name: str = "",
) -> dict:
    """Draft a new signal (search theme OR audience) on an asset group — returns PREVIEW.

    Pass exactly one of search_theme (a phrase) or audience_resource_name
    (a 'customers/.../audiences/...' resource name). Search themes are
    immutable once created — to "edit", remove the old signal and add a new
    one.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.pmax_write import draft_asset_group_signal as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_group_id=asset_group_id,
        search_theme=search_theme,
        audience_resource_name=audience_resource_name,
    )


# ---------------------------------------------------------------------------
# Label Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def list_labels(customer_id: str = "") -> dict:
    """List all labels in the Google Ads account.

    Returns each label's id, name, status, description, and background_color.
    Use the IDs returned here with apply_label / unapply_label / remove_entity.
    """
    from adloop.ads.labels import list_labels as _impl

    return _impl(current_config(), customer_id=customer_id or current_config().ads.customer_id)


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_label(
    name: str,
    customer_id: str = "",
    description: str = "",
    background_color: str = "",
) -> dict:
    """Draft creating a new Label — returns PREVIEW.

    name: required. Must be unique in the account.
    description: optional human description.
    background_color: optional hex color string like '#FF5733'.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.labels import draft_label as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        name=name,
        description=description,
        background_color=background_color,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def apply_label(
    entity_type: str,
    entity_id: str,
    label_id: str,
    customer_id: str = "",
) -> dict:
    """Draft attaching a label to a campaign/ad_group/ad/keyword — returns PREVIEW.

    entity_type: 'campaign', 'ad_group', 'ad', or 'keyword'.
    entity_id: bare ID for campaign/ad_group, 'adGroupId~adId' for ad,
        'adGroupId~criterionId' for keyword.
    label_id: the ID of an existing Label (use list_labels to discover them).

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.labels import draft_apply_label as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
        label_id=label_id,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def unapply_label(
    entity_type: str,
    entity_id: str,
    label_id: str,
    customer_id: str = "",
) -> dict:
    """Draft detaching a label from an entity (does NOT delete the Label itself).

    To delete the Label resource itself, use remove_entity with
    entity_type='label'.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.labels import draft_unapply_label as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
        label_id=label_id,
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def audit_event_coverage(
    expected_events: list[str],
    gtm_account_id: str = "",
    gtm_container_id: str = "",
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
      gtm_paused_but_firing       — only paused tag(s), not in codebase, yet
                                    GA4 still fires (event comes from elsewhere)
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

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(),
        expected_events=expected_events,
        gtm_account_id=gtm_account_id,
        gtm_container_id=gtm_container_id,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_accounts() -> dict:
    """List all GTM accounts the AdLoop service account / OAuth user can read.

    Use this for first-time discovery before calling audit_event_coverage —
    you need the account_id from here. If this returns an empty list, the
    service account hasn't been added to any GTM container with at least
    Read permission.
    """
    from adloop.gtm.read import list_accounts as _impl

    return _impl(current_config())


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_containers(gtm_account_id: str = "") -> dict:
    """List all containers under a GTM account.

    Returns container_id (the numeric ID needed by audit_event_coverage),
    public_id (the GTM-XXXXXXX string shown in the UI), name, and usage
    context (web / iOS / Android / amp / server).
    """
    from adloop.gtm.read import list_containers as _impl

    gtm_account_id = gtm_account_id or current_config().gtm.account_id
    if not gtm_account_id:
        raise ValueError(
            "gtm_account_id is required — call list_gtm_accounts first or "
            "set gtm.account_id in the config."
        )
    return _impl(current_config(), account_id=gtm_account_id)


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_tags(gtm_account_id: str = "", gtm_container_id: str = "") -> dict:
    """List every tag in the LIVE GTM container.

    Each tag includes type, status, parsed parameters, the GA4 event name
    (for GA4 event tags), and resolved firing/blocking trigger names.
    Use after audit_event_coverage to inspect specific tags.
    """
    from adloop.gtm.read import list_tags as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(), account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_tag(
    tag_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Get the full RAW configuration for a single GTM tag.

    Includes every parameter, firing/blocking triggers (with their filter
    conditions resolved to text), priority, pause status, sampling, and
    monitoring metadata. Use to inspect a tag flagged by audit_event_coverage.
    """
    from adloop.gtm.read import get_tag as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        tag_id=tag_id,
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_triggers(gtm_account_id: str = "", gtm_container_id: str = "") -> dict:
    """List every trigger in the LIVE GTM container.

    Each trigger has its filter conditions parsed to readable text
    (e.g. "{{Page Path}} matches RegExp ^/service-promotions/"). Use to
    diagnose why a tag fires or doesn't fire on specific pages.
    """
    from adloop.gtm.read import list_triggers as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(), account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_trigger(
    trigger_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Get the full RAW configuration for a single GTM trigger.

    Includes filters, auto-event filters, custom-event filters, validation
    settings, and a list of every tag that uses this trigger. Use to
    diagnose why a tag with a specific trigger ID does or doesn't fire.
    """
    from adloop.gtm.read import get_trigger as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        trigger_id=trigger_id,
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_variables(gtm_account_id: str = "", gtm_container_id: str = "") -> dict:
    """List GTM variables — both custom and enabled built-in.

    Custom variables come from the live container. Built-in variables
    (Page URL, Click Element, Form ID, etc.) come from the workspace's
    enabled-built-ins list. Variables matter because triggers reference
    them — if a trigger uses {{Form ID}} but Form ID isn't enabled, the
    trigger never matches.
    """
    from adloop.gtm.read import list_variables as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(), account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_workspaces(gtm_account_id: str = "", gtm_container_id: str = "") -> dict:
    """List workspaces (drafts) under a GTM container.

    Workspace IDs are needed for `get_gtm_workspace_diff`. Most containers
    have a single Default Workspace; multiple workspaces appear when the
    team uses parallel drafts.
    """
    from adloop.gtm.read import list_workspaces as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(), account_id=gtm_account_id, container_id=gtm_container_id
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_workspace_diff(
    workspace_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Show drafted-but-not-published changes in a GTM workspace.

    Returns the list of entities (tags, triggers, variables) added,
    modified, or deleted relative to the live published version, plus
    any merge conflicts. Common cause of "I edited a tag but nothing
    happened" — the workspace was never published. is_clean=true means
    no pending changes and no conflicts.
    """
    from adloop.gtm.read import get_workspace_diff as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_versions(
    gtm_account_id: str = "", gtm_container_id: str = "", page_size: int = 50
) -> dict:
    """List published GTM version history (newest first).

    Version headers include version_id, name, and entity counts. Use to
    correlate a metric drop with a recent publish: fetch versions, find
    one with timestamps near the drop date, then call get_gtm_version
    for full content + author info.
    """
    from adloop.gtm.read import list_versions as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        page_size=page_size,
    )


@mcp.tool(annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_version(
    container_version_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Get full metadata + entity counts for a single GTM container version.

    Returns name, description, fingerprint, and lists of tag/trigger/
    variable names at that point in time. Use after list_gtm_versions
    when correlating a metric drop with a specific publish.
    """
    from adloop.gtm.read import get_version as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        container_version_id=container_version_id,
    )


# ---------------------------------------------------------------------------
# Custom GAQL
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        query=query,
        format=format,
    )


# ---------------------------------------------------------------------------
# Google Ads Write Tools (Safety Layer)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_campaign(
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    geo_target_ids: _StrList,
    language_ids: _StrList,
    customer_id: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    channel_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: _DictListOpt = None,
    final_url_suffix: str | None = None,
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
    final_url_suffix: UTM suffix auto-applied to SEARCH campaigns. Pass "" to
        disable. Defaults to standard UTM tracking with ValueTrack parameters:
        utm_source=google&utm_medium=cpc&utm_campaign={campaignid}&utm_content={adgroupid}&utm_term={keyword}

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_campaign as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
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
        final_url_suffix=final_url_suffix,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_ad_group(
    campaign_id: str,
    ad_group_name: str,
    keywords: _DictListOpt = None,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        ad_group_name=ad_group_name,
        keywords=keywords,
        cpc_bid_micros=cpc_bid_micros,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def update_campaign(
    campaign_id: str,
    customer_id: str = "",
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    daily_budget: float = 0,
    geo_target_ids: _StrListOpt = None,
    language_ids: _StrListOpt = None,
    final_url_suffix: str | None = None,
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
    final_url_suffix: set or change the campaign's Final URL suffix. Pass "" to clear.
    search_partners_enabled: include ads on Search partners
    display_network_enabled: enable Search campaign display expansion
    display_expansion_enabled: alias for display_network_enabled
    max_cpc: Maximize Clicks CPC cap when bidding_strategy is TARGET_SPEND, or
        when the existing campaign already uses TARGET_SPEND

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import update_campaign as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        daily_budget=daily_budget,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        final_url_suffix=final_url_suffix,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_responsive_search_ad(
    ad_group_id: str,
    headlines: _StrOrDictList,
    descriptions: _StrOrDictList,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        headlines=headlines,
        descriptions=descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_rsa_replacement(
    ad_id: str,
    headlines: list[str | dict],
    descriptions: list[str | dict],
    final_url: str = "",
    customer_id: str = "",
    path1: str = "",
    path2: str = "",
    remove_old: bool = True,
) -> dict:
    """Fix an existing RSA — creates a corrected replacement and removes the old ad.

    Use this to fix issues with an existing RSA: wrong copy, character errors,
    data inconsistencies, truncated names, etc. The old ad is REMOVED by default
    so it cannot be accidentally re-enabled.

    For A/B testing or adding ad variants, use draft_responsive_search_ad instead.

    Provide the ad_id of the RSA to fix, plus the complete corrected copy.
    The tool fetches the old ad's details and shows a side-by-side diff preview.
    The new ad inherits the ad group from the old one and is created as PAUSED.
    If final_url is omitted, the old ad's URL is reused.
    Call confirm_and_apply with the returned plan_id to execute.

    Each headline/description can be a plain string (unpinned) or a dict with
    optional pinning: {"text": "...", "pinned_field": "HEADLINE_1"}.
    Valid headline pins: HEADLINE_1, HEADLINE_2, HEADLINE_3.
    Valid description pins: DESCRIPTION_1, DESCRIPTION_2.
    """
    from adloop.ads.write import draft_rsa_replacement as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_id=ad_id,
        headlines=headlines,
        descriptions=descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
        remove_old=remove_old,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_keywords(
    ad_group_id: str,
    keywords: _DictList,
    customer_id: str = "",
) -> dict:
    """Draft keyword additions — returns a PREVIEW, does NOT add keywords.

    keywords: list of {"text": "keyword phrase", "match_type": "EXACT|PHRASE|BROAD"}
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_keywords as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        keywords=keywords,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def add_negative_keywords(
    campaign_id: str,
    keywords: _StrList,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def add_negative_locations(
    campaign_id: str,
    geo_target_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Draft negative geo location additions — returns a PREVIEW.

    Use this to exclude cities/regions from a campaign while keeping broader
    positive targets such as State of Sao Paulo. geo_target_ids are numeric
    Google geo target constant IDs. Call confirm_and_apply with the returned
    plan_id to execute.
    """
    from adloop.ads.write import add_negative_locations as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        geo_target_ids=geo_target_ids,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def propose_negative_keyword_list(
    campaign_id: str,
    list_name: str,
    keywords: _StrList,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        list_name=list_name,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def add_to_negative_keyword_list(
    shared_set_id: str,
    keywords: _StrList,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def attach_shared_set_to_campaigns(
    shared_set_id: str,
    campaign_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Attach an existing shared set to one or more campaigns — returns a PREVIEW.

    Creates CampaignSharedSet linkages so the campaigns inherit the shared
    set's criteria (e.g. negative keywords). Most commonly used to attach a
    shared negative keyword list to newly-built campaigns.

    Use ``get_negative_keyword_lists`` to find the shared_set_id, and
    ``get_negative_keyword_list_campaigns`` to inspect existing attachments.

    shared_set_id: numeric ID from get_negative_keyword_lists.
    campaign_ids: list of numeric campaign IDs to attach the set to.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import attach_shared_set_to_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        campaign_ids=campaign_ids,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def detach_shared_set_from_campaigns(
    shared_set_id: str,
    campaign_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Detach a shared set from one or more campaigns — returns a PREVIEW.

    Removes CampaignSharedSet linkages so the campaigns no longer inherit the
    shared set's criteria. The shared set itself is unchanged; only the
    per-campaign attachment is removed.

    Use ``get_negative_keyword_list_campaigns`` to inspect existing attachments
    before detaching.

    shared_set_id: numeric ID from get_negative_keyword_lists.
    campaign_ids: list of numeric campaign IDs to detach the set from.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import detach_shared_set_from_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        campaign_ids=campaign_ids,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_demographic_targeting(
    customer_id: str = "",
    ad_group_id: str = "",
    campaign_id: str = "",
    # noqa: B006 — mutable default required for MCP JSON schema. Using
    # `_StrList = []` produces a flat `{"type": "array", "default": []}`
    # schema that naive MCP clients handle; `_StrListOpt = None` would
    # produce an `anyOf: [array, null]` form that some clients ignore.
    age_ranges: _StrList = [],  # noqa: B006
    genders: _StrList = [],  # noqa: B006
    parental_statuses: _StrList = [],  # noqa: B006
    income_ranges: _StrList = [],  # noqa: B006
    negative: bool = True,
) -> dict:
    """Draft demographic targeting (age/gender/parental status/income) — returns a PREVIEW.

    By default, Google Ads serves to all demographic segments. This tool adds
    criteria that EXCLUDE a segment (negative=True, default) or NARROW
    targeting to it (negative=False — uncommon).

    Provide exactly one of `ad_group_id` or `campaign_id`. At least one of
    the four demographic lists must contain a value.

    Accepted values:
    - age_ranges: '18-24', '25-34', '35-44', '45-54', '55-64', '65+'.
      Google's buckets are FIXED — 'Exclude 23-35' has no exact mapping;
      ask the user which buckets to use.
    - genders: 'female', 'male', 'undetermined'
    - parental_statuses: 'parent', 'not_a_parent', 'undetermined'
    - income_ranges: PERCENTILES (not currency). 'top-10', '11-20', '21-30',
      '31-40', '41-50', 'lower-50', 'undetermined'. Available in select
      countries only (US, AU, JP, etc.).

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_demographic_targeting as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        campaign_id=campaign_id,
        age_ranges=age_ranges,
        genders=genders,
        parental_statuses=parental_statuses,
        income_ranges=income_ranges,
        negative=negative,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        ad_group_name=ad_group_name,
        max_cpc=max_cpc,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_callouts(
    campaign_id: str,
    callouts: _StrList,
    customer_id: str = "",
) -> dict:
    """Draft campaign callout assets — returns a PREVIEW."""
    from adloop.ads.write import draft_callouts as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        callouts=callouts,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_structured_snippets(
    campaign_id: str,
    snippets: _DictList,
    customer_id: str = "",
) -> dict:
    """Draft campaign structured snippet assets — returns a PREVIEW."""
    from adloop.ads.write import draft_structured_snippets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        snippets=snippets,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_image_assets(
    campaign_id: str,
    image_paths: _StrList,
    customer_id: str = "",
) -> dict:
    """Draft campaign image assets from local PNG, JPEG, or GIF files."""
    from adloop.ads.write import draft_image_assets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        image_paths=image_paths,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def pause_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft pausing a campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", "keyword", or "asset_group"
    entity_id format by type:
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: ad group ID (e.g. "12345678")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~criterionId" (e.g. "12345678~987654")
      - asset_group: asset group ID (e.g. "6572147947")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import pause_entity as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def enable_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft enabling a paused campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", "keyword", or "asset_group"
    entity_id format by type:
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: ad group ID (e.g. "12345678")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~criterionId" (e.g. "12345678~987654")
      - asset_group: asset group ID (e.g. "6572147947")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import enable_entity as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_DESTRUCTIVE, tags={"ads"})
@_safe
def remove_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft REMOVING an entity — returns a PREVIEW. This is IRREVERSIBLE.

    entity_type: "campaign", "ad_group", "ad", "keyword", "negative_keyword",
                 "shared_criterion", "ad_group_criterion", "campaign_criterion",
                 "campaign_asset", "asset", "customer_asset", "asset_group",
                 "asset_group_signal", or "label"
    entity_id: The resource ID.
               For keywords: "adGroupId~criterionId"
               For negative_keywords: "campaignId~criterionId"
                   (use the resource_id field from get_negative_keywords)
               For shared_criterion: "sharedSetId~criterionId"
                   (use the resource_id field from get_negative_keyword_list_keywords)
               For ad_group_criterion / campaign_criterion: the remove_id from
                   get_demographic_targeting
               For campaign_asset: "campaignId~assetId~fieldType"
               For asset: simple asset ID
               For customer_asset: "assetId~fieldType"
               For asset_groups: the asset group ID
               For asset_group_signals: "assetGroupId~criterionId" (the
                   format get_asset_group_signals returns)
               For labels: the label ID (cascades to all assignments)

    WARNING: Removed entities cannot be re-enabled. Use pause_entity instead
    if you just want to temporarily disable something. To detach a label from
    a single entity (without deleting the Label itself), use unapply_label.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import remove_entity as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_WRITE, tags={"ads"})
@_safe
def draft_sitelinks(
    campaign_id: str,
    sitelinks: _DictList,
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        sitelinks=sitelinks,
    )


@mcp.tool(annotations=_DESTRUCTIVE, tags={"core"})
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

    Two-phase apply: if 'safety.two_phase_apply: true' is set (always on
    for AdLoop Cloud tenants), dry_run=false is REFUSED with status
    DRY_RUN_REQUIRED until this plan_id has completed one dry_run=true
    pass. Run the dry run, show it to the user, then apply for real.

    The plan_id comes from a prior draft_* or pause/enable tool call.
    """
    from adloop.ads.write import confirm_and_apply as _impl

    return _impl(current_config(), plan_id=plan_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Tracking Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE, tags={"ga4"})
@_safe
def draft_key_event(
    event_name: str,
    counting_method: str = "ONCE_PER_EVENT",
    property_id: str = "",
) -> dict:
    """Draft marking a GA4 event as a key event (conversion) — returns a PREVIEW.

    The fix for "the event fires but isn't tracked as a conversion":
    attribution_check / validate_tracking diagnose it, this closes the
    loop. counting_method: ONCE_PER_EVENT (purchases) or ONCE_PER_SESSION
    (sign-ups). Call confirm_and_apply with the returned plan_id to
    execute. Applies to future data only.
    """
    from adloop.ga4.write import draft_key_event as _impl

    return _impl(
        current_config(),
        property_id=property_id or current_config().ga4.property_id,
        event_name=event_name,
        counting_method=counting_method,
    )


@mcp.tool(annotations=_READONLY, tags={"tracking"})
@_safe
def validate_tracking(
    expected_events: _StrList,
    property_id: str = "",
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
    customer_id: str = "",
) -> dict:
    """Compare tracking events found in the codebase against actual GA4 data.

    First, search the user's codebase for gtag('event', ...) or dataLayer.push
    calls and extract event names. Then pass those names here to check which
    ones actually fire in GA4.

    Returns: matched events, events missing from GA4, unexpected GA4 events,
    and auto-collected events (page_view, session_start, etc.).

    customer_id: optional — when provided, also pulls Google Ads conversion
    actions and checks which expected events have matching Ads conversion
    actions configured.
    """
    from adloop.tracking import validate_tracking as _impl

    return _impl(
        current_config(),
        expected_events=expected_events,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        customer_id=customer_id,
    )


@mcp.tool(annotations=_READONLY, tags={"tracking"})
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
        current_config(),
        event_name=event_name,
        event_params=event_params,
        trigger=trigger,
        property_id=property_id or current_config().ga4.property_id,
        check_existing=check_existing,
    )


# ---------------------------------------------------------------------------
# Planning Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def estimate_budget(
    keywords: _DictList,
    daily_budget: float = 0,
    geo_target_id: str = "2276",
    language_id: str = "1000",
    forecast_days: int = 30,
    customer_id: str = "",
) -> dict:
    """Forecast clicks, cost, and conversions for a set of keywords.

    Uses Google Ads Keyword Planner to estimate campaign performance without
    creating anything. Essential for budget planning before launching campaigns.
    Per-keyword max_cpc values are collapsed to a campaign-level manual CPC
    cap (the highest one) — the Ads API forecast takes no per-keyword bids.

    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD", "max_cpc": 1.50}
        max_cpc is optional (defaults to 1.00 in account currency)
    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK, 2250=France)
    language_id: language constant (1000=English, 1001=German, 1002=French, 1003=Spanish)
    daily_budget: if provided, insights will show what % of traffic the budget captures
    forecast_days: forecast horizon in days (default 30)
    """
    from adloop.ads.forecast import estimate_budget as _impl

    return _impl(
        current_config(),
        keywords=keywords,
        daily_budget=daily_budget,
        geo_target_id=geo_target_id,
        language_id=language_id,
        forecast_days=forecast_days,
        customer_id=customer_id or current_config().ads.customer_id,
    )


@mcp.tool(annotations=_READONLY, tags={"ads"})
@_safe
def discover_keywords(
    seed_keywords: _StrList = [],  # noqa: B006 — mutable default required for MCP JSON schema
    url: str = "",
    geo_target_id: str = "2276",
    language_id: str = "1000",
    page_size: int = 50,
    customer_id: str = "",
    include_monthly_volumes: bool = False,
) -> dict:
    """Discover new keyword ideas using Google Ads Keyword Planner.

    Mirrors the "Discover new keywords" UI in Keyword Planner:
    - Start with keywords: pass seed_keywords (e.g. ["running shoes"])
    - Start with a website: pass url (e.g. "https://example.com/products")

    Set include_monthly_volumes=true for per-month search history (last 24
    months, top-20 ideas) plus a seasonality insight — use when the user
    asks about demand trends, seasonality, or "when should I ramp budget".
    - Both together: keywords + url for more targeted ideas

    Returns keyword ideas sorted by avg monthly search volume, with
    competition level (LOW/MEDIUM/HIGH) and top-of-page bid range.

    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK)
    language_id: language constant (1000=English, 1001=German, 1002=French)
    page_size: max keyword ideas to return (default 50, max 1000)
    """
    from adloop.ads.forecast import discover_keywords as _impl

    return _impl(
        current_config(),
        seed_keywords=seed_keywords,
        url=url,
        geo_target_id=geo_target_id,
        language_id=language_id,
        page_size=page_size,
        customer_id=customer_id or current_config().ads.customer_id,
        include_monthly_volumes=include_monthly_volumes,
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


def _apply_toolsets_env() -> None:
    """Expose only the toolsets named in ``ADLOOP_TOOLSETS`` (comma-separated).

    Unset/empty = the full catalog. Core tools (health_check,
    confirm_and_apply) survive every selection. A smaller tools/list costs
    less context in MCP clients that load all tool schemas upfront.
    """
    raw = _os.getenv("ADLOOP_TOOLSETS", "").strip()
    if not raw:
        return
    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = sorted(requested - set(TOOLSETS))
    if unknown:
        raise ValueError(
            f"ADLOOP_TOOLSETS names unknown toolset(s): {', '.join(unknown)}. "
            f"Valid toolsets: {', '.join(TOOLSETS)}. Example: ADLOOP_TOOLSETS=ads,ga4"
        )
    mcp.enable(tags=requested | {"core"}, only=True)


_apply_toolsets_env()
