"""AdLoop MCP server — FastMCP instance with all tool registrations."""

from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Callable

from fastmcp import FastMCP
from fastmcp.utilities.docstring_parsing import parse_docstring
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator

from adloop import diagnostics
from adloop.runtime import current_config

# openWorldHint: every tool talks to an external service (Google, Reddit,
# a live web page), which directories such as ChatGPT's require to be said.
_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

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
    "reddit": "Reddit Ads reads, writes, and planning",
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
_StrOrDictListOpt = Annotated[
    list[str | dict] | None, BeforeValidator(_coerce_json_string_to_list)
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

    The text itself does not point at those rules files: connector
    directories reject server text that references outside instruction
    sources.
    """
    return (
        "AdLoop connects Google Ads + Google Analytics (GA4) + Reddit Ads + your codebase. "
        "These are the *minimum* orchestration rules. Read these before using "
        "any write tool.\n\n"
        "SAFETY (always):\n"
        "- Every write tool returns a PREVIEW with a `plan_id`. Show the "
        "preview to the user and wait for explicit approval before calling "
        "`confirm_and_apply`.\n"
        "- Default to `dry_run=true` on `confirm_and_apply`. Only set "
        "`dry_run=false` after the user explicitly approves the preview. "
        "`require_dry_run` in config can override this.\n"
        "- Respect the config's `max_daily_budget` cap.\n"
        "- New Google Ads campaigns and RSAs, and every new Reddit campaign, "
        "ad group and ad, are created PAUSED. A new Google Ads ad group is "
        "enabled but cannot serve until one of its (paused) ads is enabled. "
        "The user must enable them after review.\n"
        "- A Google Ads dry run sends the exact change to Google with "
        "validate_only (nothing executes); DRY_RUN_FAILED means the real "
        "apply would fail the same way.\n"
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
        "Untargeted campaigns waste budget."
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


# The API each toolset calls, linked at the end of every tool description:
# directory reviews ask descriptions to reference the API they target.
_API_DOCS: dict[str, str] = {
    "ads": "Google Ads API: https://developers.google.com/google-ads/api/docs/start",
    "ga4": (
        "Google Analytics Data API: "
        "https://developers.google.com/analytics/devguides/reporting/data/v1 "
        "and Admin API: https://developers.google.com/analytics/devguides/config/admin/v1"
    ),
    "tracking": (
        "Google Analytics Data API: "
        "https://developers.google.com/analytics/devguides/reporting/data/v1 "
        "and Google Ads API: https://developers.google.com/google-ads/api/docs/start"
    ),
    "gtm": "Tag Manager API: https://developers.google.com/tag-platform/tag-manager/api/v2",
    "gsc": "Search Console API: https://developers.google.com/webmaster-tools",
    "web": "PageSpeed Insights API: https://developers.google.com/speed/docs/insights/v5/about",
    "merchant": "Merchant API: https://developers.google.com/merchant/api/overview",
    "reddit": "Reddit Ads API: https://ads-api.reddit.com/docs/v3/",
    # health_check and confirm_and_apply act on whichever platform is set up.
    "core": (
        "Google Ads API: https://developers.google.com/google-ads/api/docs/start, "
        "Google Analytics Data API: "
        "https://developers.google.com/analytics/devguides/reporting/data/v1 "
        "and Reddit Ads API: https://ads-api.reddit.com/docs/v3/"
    ),
}


def _tool(*, title: str, annotations: ToolAnnotations, tags: set[str], **kwargs):
    """Register a tool with its title in both places MCP defines one, and
    with a link to the API it calls.

    The tool's own ``title`` is what clients display; ``annotations.title``
    is what directory reviews (Claude's connector portal) check. One title,
    copied into the shared annotation preset, keeps them from drifting.
    Descriptions that already link their API (the free-form query tools,
    which point at the query reference) keep their own link.
    """
    annotations = annotations.model_copy(update={"title": title})
    docs = " ".join(_API_DOCS[tag] for tag in sorted(tags) if tag in _API_DOCS)

    def register(fn):
        # FastMCP's own parser: it moves an Args: section into the
        # parameter schema, so the description must not repeat it.
        description = parse_docstring(fn).description or inspect.cleandoc(fn.__doc__ or "")
        if docs and "https://" not in description:
            description = f"{description}\n\n{docs}"
        return mcp.tool(
            title=title,
            annotations=annotations,
            tags=tags,
            description=description,
            **kwargs,
        )(fn)

    return register


def _reddit_structured_error(exc: Exception) -> dict | None:
    """Reddit-specific translations; keyed on exception type so Reddit's
    ``invalid_grant`` never gets the "Reconnect Google" hint below."""
    from adloop.reddit.auth import RedditApiError, RedditAuthError
    from adloop.runtime import deployment_mode

    hosted = deployment_mode() == "server"
    if isinstance(exc, RedditAuthError):
        if exc.error_code == "invalid_grant":
            return {
                "error": "Reddit authentication failed — the refresh token was revoked or expired.",
                "hint": (
                    "Reconnect Reddit Ads in your AdLoop Cloud dashboard "
                    "(Settings → Reddit Ads), then retry."
                    if hosted
                    else "Run `adloop init` and redo the Reddit Ads step (the old "
                    "token at ~/.adloop/reddit_token.json was discarded)."
                ),
                "auth_error": "REDDIT_INVALID_GRANT",
            }
        if exc.error_code in ("missing_refresh_token", "missing_client"):
            return {
                "error": str(exc),
                "hint": (
                    "Connect Reddit Ads in your AdLoop Cloud dashboard (Settings → Reddit Ads)."
                    if hosted
                    else "Run `adloop init` and complete the Reddit Ads step, or set "
                    "reddit.client_id / reddit.client_secret / reddit.token_path in the config."
                ),
                "auth_error": "REDDIT_NOT_CONNECTED",
            }
        if exc.error_code == "insufficient_scope":
            return {
                "error": str(exc),
                "hint": (
                    "Reconnect Reddit Ads in the dashboard and approve write access."
                    if hosted
                    else "Re-run the Reddit step of `adloop init`; it requests adsread and adsedit."
                ),
                "auth_error": "REDDIT_INSUFFICIENT_SCOPES",
            }
        return {
            "error": str(exc),
            "hint": (
                "Reconnect Reddit Ads (Settings → Reddit Ads)."
                if hosted
                else "Check reddit.client_id / reddit.client_secret and re-run `adloop init`."
            ),
            "auth_error": "REDDIT_AUTH_FAILED",
        }
    if isinstance(exc, RedditApiError):
        if exc.status == 429:
            return {
                "error": str(exc),
                "hint": (
                    "Reddit rate-limits per user and endpoint group (reporting: 60 "
                    "requests/min). Wait for the reset before calling again; do "
                    "not retry in a loop."
                ),
                "auth_error": "REDDIT_RATE_LIMITED",
                "reset_seconds": exc.reset_seconds,
            }
        if exc.status == 403:
            return {
                "error": str(exc),
                "hint": (
                    "The authorizing Reddit user needs a role on this ad account "
                    "(Business Manager → Ad accounts → Members) and the token needs "
                    "the adsedit scope for writes. Check ad_account_id against "
                    "list_reddit_accounts."
                ),
                "auth_error": "REDDIT_FORBIDDEN",
            }
        if exc.status == 404:
            return {
                "error": str(exc),
                "hint": "The entity or ad account id does not exist or is not visible to this user.",
            }
        return {"error": str(exc), "reddit_status": exc.status}
    return None


def _structured_error(fn_name: str, exc: Exception) -> dict:
    """Translate common auth failures into actionable structured errors."""
    reddit = _reddit_structured_error(exc)
    if reddit is not None:
        reddit.setdefault("tool", fn_name)
        return reddit

    err = str(exc)
    err_lower = err.lower()

    # Access levels belong to the Google Cloud project that owns the OAuth
    # client (developer tokens were sunset on 2026-09-09). v25+ names the
    # project; older API versions still answer with the token wording.
    if (
        "cloud_project_not_approved_for_production" in err_lower
        or "developer_token_not_approved" in err_lower
        or "only approved for use with test accounts" in err_lower
    ):
        return {
            "error": (
                "Google Ads authorization failed — your Google Cloud project's "
                "API access level (Test) cannot reach production accounts."
            ),
            "hint": (
                "Open the project's Google Ads API Overview page "
                "(https://console.cloud.google.com/google/ads-apis/overview) and "
                "apply for Explorer access (usually granted automatically) or "
                "Basic access (needs the OAuth consent screen brand-verified: "
                "External and In production). Or switch AdLoop to a test account."
            ),
            "auth_error": "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
        }

    if "developer_token_invalid" in err_lower or "developer token is not valid" in err_lower:
        return {
            "error": "Google Ads authentication failed — the configured developer token is invalid.",
            "hint": (
                "Since September 2026 no developer token is needed: remove "
                "`ads.developer_token` from `~/.adloop/config.yaml`. API access "
                "belongs to the Google Cloud project that owns your OAuth client. "
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
        from adloop.runtime import deployment_mode

        return {
            "error": (
                "Authentication failed — the OAuth client behind your stored "
                "credentials no longer exists or is invalid."
            ),
            # On a hosted server the user never sees an OAuth client: the
            # fix is reconnecting, and pointing at a product would be an ad.
            "hint": (
                "Reconnect Google in your AdLoop Cloud dashboard "
                "(Settings → Google), then retry."
                if deployment_mode() == "server"
                else "If you set up AdLoop before v0.10 with its bundled "
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


@_tool(title="Connection health check", annotations=_READONLY, tags={"core"})
@_safe
def health_check() -> dict:
    """Test AdLoop connectivity — checks OAuth token, GA4 API, Google Ads API,
    and (when configured) the Reddit Ads API.

    Returns the status of each service and actionable guidance when something
    is broken, which makes it the diagnostic for failures in other tools.
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

    def _ga4_failed(surface: str, e: Exception) -> None:
        parsed = _structured_error("health_check", e)
        status["ga4"] = "error"
        status[surface] = "error"
        status["ga4_error"] = parsed["error"]
        if "hint" in parsed:
            status["ga4_hint"] = parsed["hint"]
        if "auth_error" in parsed:
            status["ga4_auth_error"] = parsed["auth_error"]
        if "details" in parsed:
            status["ga4_error_details"] = parsed["details"]

    # Two surfaces: the Admin API lists properties, the Data API serves every
    # report. A project can have one enabled and not the other, so "ok" means
    # both answered.
    try:
        from adloop.ga4.reports import get_account_summaries as _ga4_test

        result = _ga4_test(current_config())
        status["ga4_admin"] = "ok"
        status["ga4_properties"] = result.get("total_properties", 0)
    except Exception as e:
        _ga4_failed("ga4_admin", e)
    else:
        from adloop.ga4.reports import first_property, probe_data_api

        prop = current_config().ga4.property_id or first_property(result)
        if not prop:
            status["ga4"] = "ok"
            status["ga4_data"] = "not_checked"
        else:
            try:
                probe_data_api(current_config(), prop)
                status["ga4"] = "ok"
                status["ga4_data"] = "ok"
            except Exception as e:
                _ga4_failed("ga4_data", e)

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

    reddit_cfg = current_config().reddit
    if reddit_cfg.ad_account_id or reddit_cfg.client_id:
        try:
            from adloop.reddit.client import data_of, reddit_get

            me = data_of(reddit_get(current_config(), "me"))
            status["reddit"] = "ok"
            status["reddit_username"] = me.get("reddit_username")
            status["reddit_ad_account_id"] = reddit_cfg.ad_account_id or None
        except Exception as e:
            parsed = _structured_error("health_check", e)
            status["reddit"] = "error"
            status["reddit_error"] = parsed["error"]
            if "hint" in parsed:
                status["reddit_hint"] = parsed["hint"]
            if "auth_error" in parsed:
                status["reddit_auth_error"] = parsed["auth_error"]
    else:
        status["reddit"] = "not_configured"

    if status["ga4"] == "error" or status["ads"] == "error":
        if status.get("ads_hint"):
            status["hint"] = status["ads_hint"]
        elif status.get("ga4_hint"):
            status["hint"] = status["ga4_hint"]
    elif status.get("reddit") == "error" and status.get("reddit_hint"):
        status["hint"] = status["reddit_hint"]

    return status


# ---------------------------------------------------------------------------
# GA4 Read Tools
# ---------------------------------------------------------------------------


@_tool(title="List Analytics properties", annotations=_READONLY, tags={"ga4"})
@_safe
def get_account_summaries() -> dict:
    """List all GA4 accounts and properties accessible by the authenticated user.

    Serves as the discovery step for which GA4 properties are available.
    Returns account names, property names, and property IDs.
    """
    from adloop.ga4.reports import get_account_summaries as _impl

    return _impl(current_config())


@_tool(title="Analytics report", annotations=_READONLY, tags={"ga4"})
@_safe
def run_ga4_report(
    dimensions: _StrListOpt = None,
    metrics: _StrListOpt = None,
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
    Queries the GA4 Data API. Dimensions and metrics:
    https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
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
    )


@_tool(title="Analytics realtime report", annotations=_READONLY, tags={"ga4"})
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


@_tool(title="List Analytics events", annotations=_READONLY, tags={"ga4"})
@_safe
def get_tracking_events(
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
    property_id: str = "",
) -> dict:
    """List all GA4 events and their volume for the given date range.

    Returns every distinct event name with its total event count.
    Shows what tracking is configured and active.
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


@_tool(title="List Search Console properties", annotations=_READONLY, tags={"gsc"})
@_safe
def list_gsc_sites() -> dict:
    """List all Google Search Console properties the authenticated user can access.

    Serves as the discovery step for the site URLs that search analytics
    reports (run_gsc_report) take. Returns the site URL and permission level
    for each property.
    """
    from adloop.gsc.reports import list_gsc_sites as _impl

    return _impl(current_config())


@_tool(title="Search Console report", annotations=_READONLY, tags={"gsc"})
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
        "sc-domain:example.com"). Defaults to the configured Search Console site.
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
    Queries the Search Console API:
    https://developers.google.com/webmaster-tools/v1/searchanalytics/query
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
# Google Ads Read Tools
# ---------------------------------------------------------------------------


@_tool(title="PageSpeed analysis", annotations=_READONLY, tags={"web"})
@_safe
def analyze_page_speed(url: str, strategy: str = "mobile") -> dict:
    """Run PageSpeed Insights for a landing page — Lighthouse + real-user data.

    Returns the performance score (0-100), lab Core Web Vitals (LCP, CLS,
    TBT, FCP), CrUX field data from real Chrome users where available
    (p75 LCP/INP/CLS + FAST/AVERAGE/SLOW ratings), and the top improvement
    opportunities with estimated savings.

    Typical input is an ad final_url: slow landing pages depress Quality
    Score and waste paid clicks. strategy: "mobile" (default — most paid traffic) or
    "desktop". Takes 10-30s; that is normal for a Lighthouse run.
    """
    from adloop.pagespeed import analyze_page_speed as _impl

    return _impl(current_config(), url=url, strategy=strategy)


@_tool(
    title="List Merchant Center accounts",
    annotations=_READONLY,
    tags={"merchant"},
)
@_safe
def list_merchant_accounts() -> dict:
    """List Google Merchant Center accounts the connected user can access.

    Returns each account's numeric ID, name and whether it is a test
    account. Provides the account_id used by get_merchant_feed_health;
    not needed when merchant.account_id is set in the config.
    """
    from adloop.merchant.read import list_merchant_accounts as _impl

    return _impl(current_config())


@_tool(title="Merchant Center feed health", annotations=_READONLY, tags={"merchant"})
@_safe
def get_merchant_feed_health(account_id: str = "") -> dict:
    """Merchant Center feed health — disapproved products + account issues.

    Disapproved feed items silently starve Shopping and Performance Max
    campaigns; this surfaces approved/pending/disapproved counts per
    reporting context (Shopping ads, free listings, ...), the top product
    issues by affected products (with documentation links), and
    account-level issues — CRITICAL ones stop offers serving entirely.

    account_id: numeric Merchant Center ID from list_merchant_accounts.
        Defaults to merchant.account_id in the config.
    Product-status data lags reality by ~30 minutes.
    """
    from adloop.merchant.read import get_merchant_feed_health as _impl

    return _impl(current_config(), account_id=account_id)


@_tool(title="List Google Ads accounts", annotations=_READONLY, tags={"ads"})
@_safe
def list_accounts(limit: int = 200) -> dict:
    """List accessible Google Ads accounts.

    Returns account names, IDs, and status. The default cap of 200 covers
    the vast majority of agency MCCs in one call. When more accounts exist
    than `limit`, the response has 'truncated: true'; a much higher limit
    (e.g. list_accounts(limit=1000)) returns the full list. Workflows that
    target a specific account need no enumeration at all:
    get_campaign_performance, run_gaql, etc. take customer_id directly.
    """
    from adloop.ads.read import list_accounts as _impl

    return _impl(current_config(), limit=limit)


@_tool(title="Campaign performance", annotations=_READONLY, tags={"ads"})
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

    compact=true (for audits/overviews on large accounts) returns
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


@_tool(title="Ad performance", annotations=_READONLY, tags={"ads"})
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

    compact=true (for audits/overviews) returns totals, the top-10
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


@_tool(title="Keyword performance", annotations=_READONLY, tags={"ads"})
@_safe
def get_keyword_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
) -> dict:
    """Get keyword metrics including quality scores and competitive data.

    Returns: keyword text, match type, quality score, impressions,
    clicks, CTR, CPC, conversions for each keyword.

    compact=true (for audits/overviews) returns totals, match-type
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


@_tool(title="Search terms report", annotations=_READONLY, tags={"ads"})
@_safe
def get_search_terms(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    compact: bool = False,
) -> dict:
    """Get search terms report — what users actually typed before clicking your ads.

    Critical for finding negative keyword opportunities and understanding user intent.
    Returns: search term, campaign, ad group, impressions, clicks, conversions.

    compact=true (for audits/overviews) returns totals, the top-10
    terms by clicks, ready-made negative-keyword waste candidates
    (5+ clicks, zero conversions), and top converters (~90% smaller).
    """
    from adloop.ads.read import get_search_terms as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        compact=compact,
    )


@_tool(title="List negative keywords", annotations=_READONLY, tags={"ads"})
@_safe
def get_negative_keywords(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List existing negative keywords for a campaign or all campaigns.

    Shows the existing negatives, which reveals duplicates before new
    negative keywords are added.
    If campaign_id is empty, returns negatives across all campaigns.
    """
    from adloop.ads.read import get_negative_keywords as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@_tool(
    title="List shared negative keyword lists",
    annotations=_READONLY,
    tags={"ads"},
)
@_safe
def get_negative_keyword_lists(
    customer_id: str = "",
) -> dict:
    """List all shared negative keyword lists (SharedSets) in the account.

    Returns each list's ID, name, status, and keyword count. Useful before
    propose_negative_keyword_list: a suitable list may already exist and
    just need attaching to a campaign.
    """
    from adloop.ads.read import get_negative_keyword_lists as _impl

    return _impl(current_config(), customer_id=customer_id or current_config().ads.customer_id)


@_tool(
    title="Keywords in a negative keyword list",
    annotations=_READONLY,
    tags={"ads"},
)
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


@_tool(
    title="Campaigns using a negative keyword list",
    annotations=_READONLY,
    tags={"ads"},
)
@_safe
def get_negative_keyword_list_campaigns(
    shared_set_id: str = "",
    customer_id: str = "",
) -> dict:
    """List which campaigns a shared negative keyword list is attached to.

    shared_set_id: numeric ID from get_negative_keyword_lists. When omitted,
    returns all list-to-campaign attachments across the account.
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


@_tool(title="Google Ads recommendations", annotations=_READONLY, tags={"ads"})
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

    Includes insights that flag budget-increase recommendations (their projected
    gain comes from spending more) and highlight high-impact suggestions.
    """
    from adloop.ads.read import get_recommendations as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        recommendation_types=recommendation_types,
        campaign_id=campaign_id,
    )


@_tool(title="Performance Max performance", annotations=_READONLY, tags={"ads"})
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


@_tool(
    title="Performance Max assets",
    annotations=_READONLY,
    tags={"ads"},
)
@_safe
def get_pmax_assets(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List the assets of Performance Max campaigns with type, status and content.

    Returns each asset's field_type (HEADLINE, DESCRIPTION, MARKETING_IMAGE,
    YOUTUBE_VIDEO, etc.), primary_status (ELIGIBLE, NOT_ELIGIBLE, PAUSED,
    PENDING), and content (text or image URL).

    Note: per-asset performance labels (BEST/GOOD/LOW) are not available for
    PMax assets in the Google Ads API. get_detailed_asset_performance reports
    which asset combinations Google selects most — the closest proxy for
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


@_tool(
    title="Performance Max asset combinations",
    annotations=_READONLY,
    tags={"ads"},
)
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


@_tool(title="Audience performance", annotations=_READONLY, tags={"ads"})
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


@_tool(title="Demographic targeting", annotations=_READONLY, tags={"ads"})
@_safe
def get_demographic_targeting(
    ad_group_id: str = "",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """List demographic targeting criteria (age, gender, parental status, income).

    Takes exactly one of `ad_group_id` or `campaign_id`. Returns each
    criterion's value, whether it's negative (excluded) or positive
    (narrowing), status, and a `remove_id` (composite resource ID) that
    can be passed directly to `remove_entity` with
    entity_type='ad_group_criterion' or 'campaign_criterion'.

    By default, Google Ads serves ads to ALL demographic segments — a
    criterion only appears here once a segment has been actively excluded
    or narrowed.
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


@_tool(
    title="Campaign conversions against Analytics",
    annotations=_READONLY,
    tags={"tracking"},
)
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
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_name=campaign_name,
    )


@_tool(title="Landing page analysis", annotations=_READONLY, tags={"tracking"})
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


@_tool(title="Attribution check", annotations=_READONLY, tags={"tracking"})
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


@_tool(title="Tracking coverage audit", annotations=_READONLY, tags={"gtm"})
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

    `expected_events` is the list of distinct event names found in the
    codebase's gtag('event', ...) and dataLayer.push({event: ...}) calls.
    The tool fetches the LIVE GTM
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


@_tool(title="List Tag Manager accounts", annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_accounts() -> dict:
    """List all GTM accounts the AdLoop service account / OAuth user can read.

    The first-time discovery step: provides the account_id that
    audit_event_coverage takes. An empty list means the service account
    hasn't been added to any GTM container with at least Read permission.
    """
    from adloop.gtm.read import list_accounts as _impl

    return _impl(current_config())


@_tool(title="List Tag Manager containers", annotations=_READONLY, tags={"gtm"})
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


@_tool(title="List Tag Manager tags", annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_tags(gtm_account_id: str = "", gtm_container_id: str = "") -> dict:
    """List every tag in the LIVE GTM container.

    Each tag includes type, status, parsed parameters, the GA4 event name
    (for GA4 event tags), and resolved firing/blocking trigger names.
    Complements audit_event_coverage for inspecting specific tags.
    """
    from adloop.gtm.read import list_tags as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(), account_id=gtm_account_id, container_id=gtm_container_id
    )


@_tool(title="Tag Manager tag details", annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_tag(
    tag_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Get the full RAW configuration for a single GTM tag.

    Includes every parameter, firing/blocking triggers (with their filter
    conditions resolved to text), priority, pause status, sampling, and
    monitoring metadata. Suited to inspecting a tag flagged by
    audit_event_coverage.
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


@_tool(title="List Tag Manager triggers", annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_triggers(gtm_account_id: str = "", gtm_container_id: str = "") -> dict:
    """List every trigger in the LIVE GTM container.

    Each trigger has its filter conditions parsed to readable text
    (e.g. "{{Page Path}} matches RegExp ^/service-promotions/"). Helps
    diagnose why a tag fires or doesn't fire on specific pages.
    """
    from adloop.gtm.read import list_triggers as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(
        gtm_account_id, gtm_container_id
    )

    return _impl(
        current_config(), account_id=gtm_account_id, container_id=gtm_container_id
    )


@_tool(title="Tag Manager trigger details", annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_trigger(
    trigger_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Get the full RAW configuration for a single GTM trigger.

    Includes filters, auto-event filters, custom-event filters, validation
    settings, and a list of every tag that uses this trigger. Helps
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


@_tool(title="List Tag Manager variables", annotations=_READONLY, tags={"gtm"})
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


@_tool(title="List Tag Manager workspaces", annotations=_READONLY, tags={"gtm"})
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


@_tool(title="Tag Manager workspace changes", annotations=_READONLY, tags={"gtm"})
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


@_tool(title="Tag Manager version history", annotations=_READONLY, tags={"gtm"})
@_safe
def list_gtm_versions(
    gtm_account_id: str = "", gtm_container_id: str = "", page_size: int = 50
) -> dict:
    """List published GTM version history (newest first).

    Version headers include version_id, name, and entity counts. Supports
    correlating a metric drop with a recent publish: a version with
    timestamps near the drop date has its full content + author info in
    get_gtm_version.
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


@_tool(title="Tag Manager version details", annotations=_READONLY, tags={"gtm"})
@_safe
def get_gtm_version(
    container_version_id: str, gtm_account_id: str = "", gtm_container_id: str = ""
) -> dict:
    """Get full metadata + entity counts for a single GTM container version.

    Returns name, description, fingerprint, and lists of tag/trigger/
    variable names at that point in time. Follows list_gtm_versions
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


@_tool(title="Custom Google Ads query", annotations=_READONLY, tags={"ads"})
@_safe
def run_gaql(
    query: str,
    customer_id: str = "",
    format: str = "table",
) -> dict:
    """Execute an arbitrary GAQL (Google Ads Query Language) query.

    For queries beyond the dedicated report tools.
    Queries the Google Ads API. GAQL syntax and fields:
    https://developers.google.com/google-ads/api/docs/query/overview

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


@_tool(title="Draft a campaign", annotations=_WRITE, tags={"ads"})
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
    search_partners_enabled: bool = False,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft a full campaign structure — returns a PREVIEW, does NOT create anything.

    Creates: CampaignBudget + Campaign (PAUSED) + AdGroup + optional Keywords
    + geo targeting + language targeting.
    Ads are NOT included — draft_responsive_search_ad adds them once the
    campaign exists.

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

    The returned plan_id is applied with confirm_and_apply.
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
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
    )


@_tool(title="Draft an ad group", annotations=_WRITE, tags={"ads"})
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

    campaign_id: The campaign to add the ad group to (ID as returned by
        get_campaign_performance).
    ad_group_name: Name for the new ad group.
    keywords: Optional list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD"}.
    cpc_bid_micros: Optional ad group CPC bid in micros (only for MANUAL_CPC campaigns).

    The returned plan_id is applied with confirm_and_apply.
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


@_tool(title="Draft campaign changes", annotations=_WRITE, tags={"ads"})
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
    search_partners_enabled: bool | None = None,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft an update to an existing campaign — returns a PREVIEW, does NOT apply.

    Only the parameters passed are changed; omitted ones stay as they are.

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

    The returned plan_id is applied with confirm_and_apply.
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
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=display_network_enabled,
        display_expansion_enabled=display_expansion_enabled,
        max_cpc=max_cpc,
    )


@_tool(title="Draft a responsive search ad", annotations=_WRITE, tags={"ads"})
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

    Takes 3-15 headlines (max 30 chars each) and 2-4 descriptions (max 90 chars each).
    The preview shows exactly what will be created; confirm_and_apply executes it.

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


@_tool(title="Update a responsive search ad", annotations=_WRITE, tags={"ads"})
@_safe
def update_responsive_search_ad(
    ad_id: str,
    customer_id: str = "",
    headlines: _StrOrDictListOpt = None,
    descriptions: _StrOrDictListOpt = None,
    final_url: str = "",
    path1: str = "",
    path2: str = "",
    clear_path1: bool = False,
    clear_path2: bool = False,
) -> dict:
    """Update mutable fields on an existing RSA in place — returns a PREVIEW.

    Edits an existing RSA without creating a new ad; the ad keeps its ID.
    Google Ads API v23 (``AdService.MutateAds``) permits in-place mutation of
    ``final_urls``, ``path1``, ``path2``, ``headlines``, and ``descriptions``.

    IMPORTANT: replacing headlines or descriptions is NOT a free in-place
    edit. Even though the ad ID is preserved, swapping the creative text
    RESETS the ad's asset-combination learning and performance history and
    sends the ad BACK THROUGH Google policy review — Google treats the
    creative as new for optimization. URL-only and path-only edits do not
    incur this. When headlines/descriptions change, the returned preview
    includes a ``warnings`` entry stating this trade-off before anything
    is applied.

    Headlines/descriptions are LIST-REPLACE — when provided, the supplied
    list fully swaps in for the existing one, and Google's RSA constraints
    apply (3-15 headlines, 2-4 descriptions, 30/90 char limits, pin-slot
    rules). Each entry may be a plain string (unpinned) or
    ``{"text": "...", "pinned_field": "HEADLINE_1"}``.

    Argument semantics:
        - ``headlines`` / ``descriptions``: None or [] -> no change;
          non-empty list -> replaces the existing list in full
        - ``final_url``: empty -> no change; non-empty -> replaces final URL
        - ``path1`` / ``path2``: empty -> no change; non-empty -> sets value
        - ``clear_path1`` / ``clear_path2``: True -> set to empty string

    At least one mutation must be requested. The returned plan_id is applied
    with confirm_and_apply.
    """
    from adloop.ads.write import update_responsive_search_ad as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_id=ad_id,
        headlines=headlines,
        descriptions=descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
        clear_path1=clear_path1,
        clear_path2=clear_path2,
    )


@_tool(title="Draft keywords", annotations=_WRITE, tags={"ads"})
@_safe
def draft_keywords(
    ad_group_id: str,
    keywords: _DictList,
    customer_id: str = "",
) -> dict:
    """Draft keyword additions — returns a PREVIEW, does NOT add keywords.

    keywords: list of {"text": "keyword phrase", "match_type": "EXACT|PHRASE|BROAD"}
    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import draft_keywords as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        keywords=keywords,
    )


@_tool(title="Custom conversion goals", annotations=_READONLY, tags={"ads"})
@_safe
def get_custom_conversion_goals(
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """Read the custom conversion goals and the campaign goal configuration.

    Returns every custom goal with its name, status and conversion actions,
    plus — optionally for one campaign — the goal config the campaign uses
    (`goal_config_level` CUSTOMER or CAMPAIGN and the assigned custom goal).

    A custom conversion goal bundles conversion actions into a named set that a
    campaign can be pointed at. Ahead of drafting, this shows which goals
    exist, which actions they contain and what a campaign currently uses.

    Args:
        campaign_id: Numeric campaign ID. When omitted, the campaign config
            is skipped.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.custom_conversion_goals import get_custom_conversion_goals as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@_tool(
    title="Draft a custom conversion goal",
    annotations=_WRITE,
    tags={"ads"},
)
@_safe
def draft_custom_conversion_goal(
    name: str,
    conversion_action_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Create a custom conversion goal — returns a PREVIEW.

    The goal bundles conversion actions into a named set that campaigns can
    then be pointed at with draft_assign_custom_conversion_goal. Only the goal
    is created: conversion actions are read for validation and never modified.
    It is always created ENABLED — a goal created as REMOVED would be invisible
    to every other tool, so there is nothing to plan.

    If a goal with the same name and exactly the same conversion actions
    already exists, nothing is planned and the result says `already_exists`
    with that goal's ID (REMOVED goals do not count). A name that exists with
    different actions is changed with draft_update_custom_conversion_goal.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        name: Name for the new goal, e.g. "OHL | Kauf + qualifizierte Anrufe".
        conversion_action_ids: Numeric conversion action IDs. Duplicates are
            collapsed; unknown or REMOVED actions are refused.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.custom_conversion_goals import (
        draft_custom_conversion_goal as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        name=name,
        conversion_action_ids=conversion_action_ids,
    )


@_tool(
    title="Draft changes to a custom conversion goal",
    annotations=_WRITE,
    tags={"ads"},
)
@_safe
def draft_update_custom_conversion_goal(
    custom_conversion_goal_id: str,
    name: str = "",
    conversion_action_ids: _StrList = [],  # noqa: B006 — mutable default required for MCP JSON schema
    customer_id: str = "",
) -> dict:
    """Rename and/or re-scope an existing custom conversion goal — PREVIEW.

    conversion_action_ids REPLACES the whole action list, it does not append.
    An empty parameter stays unchanged. The preview shows the action
    list before and after.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        custom_conversion_goal_id: Numeric goal ID from get_custom_conversion_goals.
        name: New name. Empty leaves the name unchanged.
        conversion_action_ids: The complete new action list. Empty leaves the
            actions unchanged.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.custom_conversion_goals import draft_update_custom_conversion_goal as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        custom_conversion_goal_id=custom_conversion_goal_id,
        name=name or None,
        conversion_action_ids=conversion_action_ids or None,
    )


@_tool(
    title="Draft assigning a custom conversion goal",
    annotations=_WRITE,
    tags={"ads"},
)
@_safe
def draft_assign_custom_conversion_goal(
    campaign_id: str,
    custom_conversion_goal_id: str,
    customer_id: str = "",
) -> dict:
    """Point a campaign at a custom conversion goal — returns a PREVIEW.

    Sets goal_config_level=CAMPAIGN and the goal on that campaign's conversion
    goal config. Nothing else changes: conversion actions, bidding, budgets and
    the account-level goals stay as they are.

    A campaign must have at least one goal configured when it switches to
    campaign level — Google answers EMPTY_CONVERSION_GOALS otherwise. That is
    why this tool assigns a goal instead of clearing goals;
    draft_clear_custom_conversion_goal goes back to the account level.

    If the campaign already uses exactly this goal, nothing is planned and the
    result says `already_configured`. The preview carries a note that changing
    what a campaign optimises for restarts its Smart Bidding learning period.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        campaign_id: Numeric campaign ID to configure.
        custom_conversion_goal_id: Numeric goal ID from get_custom_conversion_goals.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.custom_conversion_goals import draft_assign_custom_conversion_goal as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        custom_conversion_goal_id=custom_conversion_goal_id,
    )


@_tool(
    title="Draft clearing a custom conversion goal",
    annotations=_WRITE,
    tags={"ads"},
)
@_safe
def draft_clear_custom_conversion_goal(
    campaign_id: str,
    customer_id: str = "",
) -> dict:
    """Put a campaign back on the account-level goals — returns a PREVIEW.

    Sets goal_config_level=CUSTOMER and clears the custom goal: the rollback for
    draft_assign_custom_conversion_goal. The account-level goals themselves are
    not touched.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        campaign_id: Numeric campaign ID to reset.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.custom_conversion_goals import draft_clear_custom_conversion_goal as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )


@_tool(title="Draft negative keywords", annotations=_WRITE, tags={"ads"})
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
    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import add_negative_keywords as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        keywords=keywords,
        match_type=match_type,
    )


@_tool(title="Draft negative locations", annotations=_WRITE, tags={"ads"})
@_safe
def add_negative_locations(
    campaign_id: str,
    geo_target_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Draft negative geo location additions — returns a PREVIEW.

    Excludes cities/regions from a campaign while keeping broader positive
    targets such as State of Sao Paulo. geo_target_ids are numeric Google
    geo target constant IDs. The returned plan_id is applied with
    confirm_and_apply.
    """
    from adloop.ads.write import add_negative_locations as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        geo_target_ids=geo_target_ids,
    )


@_tool(
    title="Draft a shared negative keyword list",
    annotations=_WRITE,
    tags={"ads"},
)
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
    The returned plan_id is applied with confirm_and_apply.
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


@_tool(
    title="Draft additions to a negative keyword list",
    annotations=_WRITE,
    tags={"ads"},
)
@_safe
def add_to_negative_keyword_list(
    shared_set_id: str,
    keywords: _StrList,
    customer_id: str = "",
    match_type: str = "EXACT",
) -> dict:
    """Append keywords to an EXISTING shared negative keyword list — returns a PREVIEW.

    For a suitable list that already exists and only needs more keywords
    (propose_negative_keyword_list creates a new list instead).
    The shared_set_id comes from get_negative_keyword_lists;
    get_negative_keyword_list_keywords shows the terms already in a list.

    shared_set_id: numeric ID from get_negative_keyword_lists (shared_set.id).
    keywords: list of keyword strings to append (duplicates in the input list
        are collapsed).
    match_type: "EXACT", "PHRASE", or "BROAD"

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import add_to_negative_keyword_list as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        keywords=keywords,
        match_type=match_type,
    )


@_tool(title="Draft attaching a shared set", annotations=_WRITE, tags={"ads"})
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

    The shared_set_id comes from ``get_negative_keyword_lists``;
    ``get_negative_keyword_list_campaigns`` lists existing attachments.

    shared_set_id: numeric ID from get_negative_keyword_lists.
    campaign_ids: list of numeric campaign IDs to attach the set to.

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import attach_shared_set_to_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        campaign_ids=campaign_ids,
    )


@_tool(title="Draft detaching a shared set", annotations=_WRITE, tags={"ads"})
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

    ``get_negative_keyword_list_campaigns`` lists the existing attachments.

    shared_set_id: numeric ID from get_negative_keyword_lists.
    campaign_ids: list of numeric campaign IDs to detach the set from.

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import detach_shared_set_from_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        campaign_ids=campaign_ids,
    )


@_tool(title="AI Max settings", annotations=_READONLY, tags={"ads"})
@_safe
def get_ai_max_settings(
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """Read the AI Max controls of Search campaigns, per campaign and ad group.

    Returns for every campaign: id, name, status, advertising channel type,
    campaign.ai_max_setting.enable_ai_max, campaign.ai_max_setting.bundling_required
    (output only) and the full campaign.asset_automation_settings list. Each
    campaign also carries its non-REMOVED ad groups with
    ad_group.ai_max_ad_group_setting.disable_search_term_matching.

    Shows the current state ahead of draft_ai_max_settings, and what the
    account actually looks like after confirm_and_apply.

    Args:
        campaign_id: Numeric campaign ID. When omitted, every non-removed
            Search campaign in the account is listed.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.ai_max import get_ai_max_settings as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
    )

@_tool(title="Draft AI Max settings", annotations=_WRITE, tags={"ads"})
@_safe
def draft_ai_max_settings(
    campaign_id: str,
    enable_ai_max: bool | None = None,
    disable_search_term_matching: bool | None = None,
    ad_group_ids: _StrList = [],  # noqa: B006 — mutable default required for MCP JSON schema
    include_paused_ad_groups: bool = True,
    text_asset_automation: str = "UNCHANGED",
    final_url_expansion: str = "UNCHANGED",
    customer_id: str = "",
) -> dict:
    """Draft AI Max controls for a Search campaign — returns a PREVIEW.

    AI Max is the container that makes brand exclusions usable in Search:
    Google rejects a brand list on a plain Search campaign ("For search
    advertising channel, brand lists can only be applied to exclusive
    targeting, broad match campaigns for inclusive targeting or PMax generated
    campaigns"). Turning AI Max on while leaving its automations running is the
    trap, so both belong to the same plan. An automation the caller never mentioned is
    refused outright (the plan says which switch to pass); one named explicitly
    (disable_search_term_matching=False, text_asset_automation="OPTED_IN") is
    planned with requires_double_confirm=True, because switching to AI Max with
    its automations is a real configuration some accounts want.

    Reads the campaign and its ad groups first, so the preview names concrete
    ad groups and shows current values per knob. Nothing is written until
    confirm_and_apply runs.

    Args:
        campaign_id: Numeric ID of the Search campaign to prepare.
        enable_ai_max: Value for campaign.ai_max_setting.enable_ai_max. When
            omitted, it stays untouched.
        disable_search_term_matching: Value for
            ad_group.ai_max_ad_group_setting.disable_search_term_matching on the
            selected ad groups. When omitted, they stay untouched.
        ad_group_ids: Explicit ad group IDs. Empty means every non-removed ad
            group of the campaign.
        include_paused_ad_groups: Default true — paused groups are set as well,
            so re-enabling one later cannot silently restore search term
            matching. REMOVED ad groups are never touched.
        text_asset_automation: OPTED_IN, OPTED_OUT or UNCHANGED for
            TEXT_ASSET_AUTOMATION.
        final_url_expansion: OPTED_IN, OPTED_OUT or UNCHANGED for
            FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION (the v25 field name for
            final URL expansion).
        customer_id: Ads account ID. Defaults to the configured account.

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import draft_ai_max_settings as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        enable_ai_max=enable_ai_max,
        disable_search_term_matching=disable_search_term_matching,
        ad_group_ids=ad_group_ids or None,
        include_paused_ad_groups=include_paused_ad_groups,
        text_asset_automation=text_asset_automation,
        final_url_expansion=final_url_expansion,
    )


@_tool(
    title="Prepare a campaign for brand exclusions",
    annotations=_WRITE,
    tags={"ads"},
)

@_safe
def draft_prepare_brand_exclusions(
    campaign_id: str,
    include_paused_ad_groups: bool = True,
    customer_id: str = "",
) -> dict:
    """Draft the safe standard state for brand exclusions — returns a PREVIEW.

    Exactly one combination, nothing else:

        enable_ai_max = true
        disable_search_term_matching = true   (all non-removed ad groups)
        TEXT_ASSET_AUTOMATION = OPTED_OUT
        FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION = OPTED_OUT

    No bidding, keyword, match type, ad, URL or budget change, and no brand list
    is attached — attaching stays a separate step with propose_brand_list /
    attach_brand_list_to_campaigns once this state is verified in the account.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        campaign_id: Numeric ID of the Search campaign to prepare.
        include_paused_ad_groups: Default true — paused ad groups are set too,
            so re-enabling one later cannot silently restore search term
            matching.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import draft_prepare_brand_exclusions as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        include_paused_ad_groups=include_paused_ad_groups,
    )

@_tool(title="Draft demographic targeting", annotations=_WRITE, tags={"ads"})
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

    Takes exactly one of `ad_group_id` or `campaign_id`. At least one of
    the four demographic lists must contain a value.

    Accepted values:
    - age_ranges: '18-24', '25-34', '35-44', '45-54', '55-64', '65+'.
      Google's buckets are FIXED — 'Exclude 23-35' has no exact mapping
      and needs a choice of buckets.
    - genders: 'female', 'male', 'undetermined'
    - parental_statuses: 'parent', 'not_a_parent', 'undetermined'
    - income_ranges: PERCENTILES (not currency). 'top-10', '11-20', '21-30',
      '31-40', '41-50', 'lower-50', 'undetermined'. Available in select
      countries only (US, AU, JP, etc.).

    The returned plan_id is applied with confirm_and_apply.
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


@_tool(title="Draft ad group changes", annotations=_WRITE, tags={"ads"})
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


@_tool(title="Draft callouts", annotations=_WRITE, tags={"ads"})
@_safe
def draft_callouts(
    callouts: _StrList,
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "campaign",
    customer_id: str = "",
) -> dict:
    """Draft callout assets — returns a PREVIEW.

    Callouts are short, non-clickable phrases shown with a search ad. Where
    they link is set by scope: one campaign (the default), one ad group, or
    the whole account. Account-wide linking happens only with
    scope="account"; an empty campaign_id under the default scope is a
    validation error, not an account-wide link.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        callouts: Callout texts, 1-25 characters each.
        campaign_id: Numeric campaign ID; required for scope="campaign".
        ad_group_id: Numeric ad group ID; required for scope="ad_group".
        scope: "campaign" (CampaignAsset), "ad_group" (AdGroupAsset) or
            "account" (CustomerAsset: serves on every eligible campaign that
            has no callouts of its own; takes no campaign_id/ad_group_id).
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import draft_callouts as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        scope=scope,
        callouts=callouts,
    )


@_tool(title="Draft structured snippets", annotations=_WRITE, tags={"ads"})
@_safe
def draft_structured_snippets(
    snippets: _DictList,
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "campaign",
    customer_id: str = "",
) -> dict:
    """Draft structured snippet assets — returns a PREVIEW.

    A structured snippet is a fixed header (e.g. "Services") with 3-10 short
    values shown with a search ad. Where it links is set by scope: one
    campaign (the default), one ad group, or the whole account. Account-wide
    linking happens only with scope="account"; an empty campaign_id under
    the default scope is a validation error, not an account-wide link.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        snippets: List of {"header": str, "values": [str]}. header is one of
            Google's predefined headers (Amenities, Brands, Courses, Degree
            programs, Destinations, Featured Hotels, Insurance coverage,
            Models, Neighborhoods, Services, Shows, Styles, Types); 3-10
            values of 1-25 characters each.
        campaign_id: Numeric campaign ID; required for scope="campaign".
        ad_group_id: Numeric ad group ID; required for scope="ad_group".
        scope: "campaign" (CampaignAsset), "ad_group" (AdGroupAsset) or
            "account" (CustomerAsset: serves on every eligible campaign that
            has no snippets of its own; takes no campaign_id/ad_group_id).
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import draft_structured_snippets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        scope=scope,
        snippets=snippets,
    )


@_tool(title="Draft image assets", annotations=_WRITE, tags={"ads"})
@_safe
def draft_image_assets(
    campaign_id: str,
    image_paths: _StrList = [],  # noqa: B006 — mutable default required for MCP JSON schema
    image_urls: _StrList = [],  # noqa: B006
    customer_id: str = "",
) -> dict:
    """Draft campaign image assets from local files or public image URLs — returns a PREVIEW.

    At least one image in total is required across image_paths and image_urls.
    Images must be PNG, JPEG or GIF and at most 5120 KB (Google's limit).
    URL images are downloaded now and checked; applying re-downloads them
    and refuses if the image changed since the preview.

    Args:
        campaign_id: Numeric ID of the campaign to attach the images to.
        image_paths: Local PNG/JPEG/GIF file paths on the machine running
            AdLoop. Not available on a hosted server, where image_urls
            applies instead.
        image_urls: Public http(s) URLs of PNG/JPEG/GIF images. Links to
            private, local or internal addresses are refused.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import draft_image_assets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        image_paths=image_paths,
        image_urls=image_urls,
    )


@_tool(title="Draft pausing an entity", annotations=_WRITE, tags={"ads"})
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

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import pause_entity as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@_tool(title="Draft enabling an entity", annotations=_WRITE, tags={"ads"})
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

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import enable_entity as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@_tool(title="Draft removing an entity", annotations=_DESTRUCTIVE, tags={"ads"})
@_safe
def remove_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft REMOVING an entity — returns a PREVIEW. This is IRREVERSIBLE.

    entity_type: "campaign", "ad_group", "ad", "keyword", "negative_keyword",
                 "shared_criterion", "ad_group_asset", "campaign_asset", "asset",
                 or "customer_asset"
    entity_id: The resource ID.
               For keywords: "adGroupId~criterionId"
               For negative_keywords: "campaignId~criterionId"
                   (the resource_id field from get_negative_keywords)
               For shared_criterion: "sharedSetId~criterionId"
                   (the resource_id field from get_negative_keyword_list_keywords)
               For ad_group_asset: "adGroupId~assetId~fieldType"
               For campaign_asset: "campaignId~assetId~fieldType"
               For asset: simple asset ID
               For customer_asset: "assetId~fieldType"

    WARNING: Removed entities cannot be re-enabled. pause_entity is the
    reversible way to temporarily disable something.

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import remove_entity as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@_tool(title="Draft sitelinks", annotations=_WRITE, tags={"ads"})
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

    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.write import draft_sitelinks as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        sitelinks=sitelinks,
    )


@_tool(title="Draft a business name asset", annotations=_WRITE, tags={"ads"})
@_safe
def draft_business_name_asset(
    business_name: str,
    campaign_id: str = "",
    scope: str = "campaign",
    customer_id: str = "",
) -> dict:
    """Draft a business name asset — returns a PREVIEW.

    Creates a text asset and links it as BUSINESS_NAME, the advertiser name
    Google can show with ads. Google links business names to a campaign or
    to the whole account, not to an ad group. Account-wide linking happens
    only with scope="account".

    The returned plan_id is applied with confirm_and_apply.

    Args:
        business_name: The business name, 1-25 characters.
        campaign_id: Numeric campaign ID; required for scope="campaign".
        scope: "campaign" (default) or "account".
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import draft_business_name_asset as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        business_name=business_name,
        scope=scope,
        campaign_id=campaign_id,
    )


@_tool(title="Draft account-level asset links", annotations=_WRITE, tags={"ads"})
@_safe
def link_asset_to_customer(
    links: _DictList,
    customer_id: str = "",
) -> dict:
    """Draft linking existing assets at account level (CustomerAsset) — returns a PREVIEW.

    No asset is created: each entry links an asset that already exists in
    the account. Account-level assets serve on every eligible campaign that
    has no asset of the same type linked at campaign or ad-group level. The
    draft reads each asset and refuses ids that don't exist or whose asset
    type doesn't fit the field type.

    Field types CustomerAsset accepts, with the asset type each needs:
    BUSINESS_NAME (TEXT), BUSINESS_LOGO (IMAGE), CALL, CALLOUT,
    HOTEL_CALLOUT, MOBILE_APP, PRICE, PROMOTION, SITELINK,
    STRUCTURED_SNIPPET. Ad-only field types (HEADLINE, MARKETING_IMAGE, ...)
    are refused.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        links: List of {"asset_id": numeric asset ID, "field_type": one of
            the field types above}.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import link_asset_to_customer as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        links=links,
    )


@_tool(title="Draft a callout edit", annotations=_WRITE, tags={"ads"})
@_safe
def update_callout(
    asset_id: str,
    callout_text: str,
    customer_id: str = "",
) -> dict:
    """Draft an in-place edit of an existing callout asset — returns a PREVIEW.

    The asset keeps its ID and performance history, and the new text shows
    everywhere the asset is linked. The preview carries the current text
    next to the new one.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        asset_id: Numeric ID of the existing callout asset (asset.id).
        callout_text: New callout text, 1-25 characters.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import update_callout as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_id=asset_id,
        callout_text=callout_text,
    )


@_tool(title="Draft a sitelink edit", annotations=_WRITE, tags={"ads"})
@_safe
def update_sitelink(
    asset_id: str,
    link_text: str = "",
    final_url: str = "",
    description1: str = "",
    description2: str = "",
    customer_id: str = "",
) -> dict:
    """Draft an in-place edit of an existing sitelink asset — returns a PREVIEW.

    Only the fields passed change; empty fields keep their current value.
    The asset keeps its ID and performance history, and the edit shows
    everywhere the asset is linked. The preview carries the current value
    of every changed field. A new final_url is checked for reachability,
    and the two description lines must end up both set or both empty.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        asset_id: Numeric ID of the existing sitelink asset (asset.id).
        link_text: New link text, 1-25 characters.
        final_url: New landing page URL.
        description1: New first description line, 1-35 characters.
        description2: New second description line, 1-35 characters.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import update_sitelink as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_id=asset_id,
        link_text=link_text,
        final_url=final_url,
        description1=description1,
        description2=description2,
    )


@_tool(title="Draft a structured snippet swap", annotations=_DESTRUCTIVE, tags={"ads"})
@_safe
def update_structured_snippet(
    asset_id: str,
    header: str,
    values: _StrList,
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "campaign",
    customer_id: str = "",
) -> dict:
    """Draft replacing a structured snippet at one scope — returns a PREVIEW.

    This is a swap: one request creates a new snippet asset, links it where
    the old one is linked and removes the old link, so Google applies all of
    it or none of it. The old asset keeps its performance history and stays
    wherever else it is linked; the new asset starts without history. The
    draft refuses when the old asset is not linked as a structured snippet
    at the given scope. The plan needs double confirmation because it
    removes a link.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        asset_id: Numeric ID of the structured snippet asset to replace
            (asset.id).
        header: Google's predefined header for the new snippet (e.g.
            "Services", "Brands", "Types").
        values: 3-10 values of 1-25 characters each.
        campaign_id: Numeric campaign ID; required for scope="campaign".
        ad_group_id: Numeric ad group ID; required for scope="ad_group".
        scope: Where the old snippet is linked: "campaign" (default),
            "ad_group" or "account".
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.assets import update_structured_snippet as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        asset_id=asset_id,
        header=header,
        values=values,
        scope=scope,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
    )


@_tool(title="Apply a previewed change", annotations=_DESTRUCTIVE, tags={"core"})
@_safe
def confirm_and_apply(
    plan_id: str,
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    IMPORTANT: Defaults to dry_run=True (validate only). Only an explicit
    dry_run=false applies the change to the ad account (Google Ads or Reddit
    Ads — the plan knows which platform it targets).

    Forced dry runs: when real changes are switched off (live changes off
    for the workspace in AdLoop Cloud, or safety.require_dry_run in a
    self-hosted config), dry_run=false is IGNORED and the result is still
    DRY_RUN_SUCCESS, with 'dry_run_forced_by' and 'remediation' fields.
    'remediation' says how the user switches that setting; until it is
    changed, repeated calls with dry_run=false change nothing.

    Two-phase apply: if 'safety.two_phase_apply: true' is set (always on
    for AdLoop Cloud tenants), dry_run=false is REFUSED with status
    DRY_RUN_REQUIRED until this plan_id has completed one dry_run=true
    pass. The sequence is: dry run, review of its result, then the real
    apply.

    Google Ads plans: the dry run sends the exact mutates to Google with
    validate_only=True, so Google checks the change and executes nothing
    (`checks` counts the validated calls). Steps that build on an object an
    earlier step would create cannot be validated and are counted as skipped.

    Reddit plans: Reddit has no validate-only mode, so the dry run re-reads
    the target entity and re-checks the safety caps (returned as `checks`).

    Either way, a DRY_RUN_FAILED result means the real apply would also fail.

    The plan_id comes from a prior draft_* or pause/enable tool call.
    """
    from adloop.ads.write import confirm_and_apply as _impl

    return _impl(current_config(), plan_id=plan_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Reddit Ads Tools
# ---------------------------------------------------------------------------
# Second ad platform. Every tool takes ``ad_account_id`` (falls back to
# ``reddit.ad_account_id`` in the config); the name is deliberately not
# ``account_id`` so scope enforcement never confuses it with Merchant Center.


def _reddit_account(ad_account_id: str) -> str:
    return ad_account_id or current_config().reddit.ad_account_id


@_tool(title="List Reddit ad accounts", annotations=_READONLY, tags={"reddit"})
@_safe
def list_reddit_accounts() -> dict:
    """List Reddit businesses and ad accounts the connected Reddit user can access.

    Provides the ad_account_id values (plus currency, time zone and approval
    state) that every other Reddit tool takes. Requires the Reddit Ads
    connection (adloop init → Reddit step, or Settings → Reddit Ads in Cloud).
    """
    from adloop.reddit.read import list_reddit_accounts as _impl

    return _impl(current_config())


@_tool(
    title="Reddit billing + posting profiles", annotations=_READONLY, tags={"reddit"}
)
@_safe
def list_reddit_funding_instruments(ad_account_id: str = "") -> dict:
    """Funding instruments (billing) and posting profiles of a Reddit ad account.

    draft_reddit_campaign needs a servable funding_instrument_id; draft_reddit_ad
    needs the profile_id that authors the post. Both come from here.
    """
    from adloop.reddit.read import list_reddit_funding_instruments as _impl

    return _impl(current_config(), ad_account_id=_reddit_account(ad_account_id))


@_tool(title="Reddit campaigns", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_campaigns(ad_account_id: str = "", include_archived: bool = False) -> dict:
    """List Reddit campaigns with status, objective, budget mode and bids.

    Returns configured_status (what was set) and effective_status (what Reddit
    computes: PENDING_APPROVAL, REJECTED, PENDING_BILLING_INFO, ...). Money is
    in account currency. Budgets live on ad groups unless
    is_campaign_budget_optimization is true.
    """
    from adloop.reddit.read import get_reddit_campaigns as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        include_archived=include_archived,
    )


@_tool(title="Reddit ad groups", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_ad_groups(ad_account_id: str = "", campaign_id: str = "") -> dict:
    """List Reddit ad groups (budget, bid, pixel, weekly schedule, targeting summary), optionally per campaign.

    Reddit requires conversion_pixel_id on every ad group; insights flag ad
    groups without one.
    """
    from adloop.reddit.read import get_reddit_ad_groups as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        campaign_id=campaign_id,
    )


@_tool(title="Reddit ads", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_ads(
    ad_account_id: str = "",
    ad_group_id: str = "",
    campaign_id: str = "",
    include_copy: bool = False,
) -> dict:
    """List Reddit ads with status, rejection_reason, post and click URL.

    include_copy=true adds each ad's post: type (TEXT, IMAGE, VIDEO, CAROUSEL),
    headline, body, destination and media, one request per distinct post, so
    creative can be reviewed without opening Reddit. Insights flag REJECTED
    ads (policy review) and ads still PENDING_APPROVAL.
    """
    from adloop.reddit.read import get_reddit_ads as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        ad_group_id=ad_group_id,
        campaign_id=campaign_id,
        include_copy=include_copy,
    )


@_tool(title="Reddit performance", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_performance(
    ad_account_id: str = "",
    level: str = "campaign",
    date_range_start: str = "",
    date_range_end: str = "",
    breakdown: str = "",
    compact: bool = False,
) -> dict:
    """Reddit Ads performance: spend, impressions, clicks, CTR, CPC, conversions, CPA, ROAS.

    Returns: rows per entity for the chosen level with names joined, plus
    totals and insights[] (zero-conversion spenders, rejected ads, empty windows).

    level: "account", "campaign" (default), "ad_group" or "ad".
    breakdown: optional extra dimension — "date", "hour", "country", "region",
    "community", "keyword", "interest", "placement", "gender", "os_type".
    Dates are YYYY-MM-DD; default is the last 30 days in the account's time
    zone. 'conversions' is the account's key conversion event. Money is in
    account currency. Data lags up to 6 hours.
    compact=true: totals + top-10 rows + offender lists instead of every row.
    """
    from adloop.reddit.read import get_reddit_performance as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        level=level,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        breakdown=breakdown,
        compact=compact,
    )


@_tool(title="Raw Reddit report", annotations=_READONLY, tags={"reddit"})
@_safe
def run_reddit_report(
    fields: _StrList,
    breakdowns: _StrListOpt = None,
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    filter: str = "",
    time_zone_id: str = "",
) -> dict:
    """Run a custom Reddit Ads report for metrics get_reddit_performance omits.

    fields: Reddit report field names, e.g. ["SPEND", "CLICKS", "REACH",
    "VIDEO_STARTED", "CONVERSION_PURCHASE_TOTAL_VALUE", "KEY_CONVERSION_TOTAL_COUNT"].
    breakdowns: up to 3 of DATE, HOUR, CAMPAIGN_ID, AD_GROUP_ID, AD_ID, COUNTRY,
    REGION, COMMUNITY, KEYWORD, INTEREST, PLACEMENT, GENDER, OS_TYPE
    (HOUR and DATE cannot be combined). Dates are account-local days.
    filter: Reddit filter expression (e.g. "campaign_id==abc123").
    Microcurrency fields are converted to currency amounts.
    Queries the Reddit Ads API: https://ads-api.reddit.com/docs/v3/
    """
    from adloop.reddit.read import run_reddit_report as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        fields=fields,
        breakdowns=breakdowns,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        filter=filter,
        time_zone_id=time_zone_id,
    )


@_tool(title="Reddit pixels", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_pixels(ad_account_id: str = "") -> dict:
    """Reddit pixels of the account and when each event (purchase, sign_up, lead, ...) last fired.

    Relevant before conversion-optimized campaigns: insights flag pixels that never
    fired and ad groups optimizing for an event their pixel has never sent.
    Every new ad group needs a conversion_pixel_id from here.
    """
    from adloop.reddit.read import get_reddit_pixels as _impl

    return _impl(current_config(), ad_account_id=_reddit_account(ad_account_id))


@_tool(title="Search Reddit targeting", annotations=_READONLY, tags={"reddit"})
@_safe
def search_reddit_targeting(
    kind: str, query: str = "", country: str = "", website_url: str = "", limit: int = 25
) -> dict:
    """Look up targeting options for draft_reddit_ad_group.

    kind: "communities" (subreddits; query required), "interests" (query
    filters by name), "geolocations" (country ISO code and/or city query),
    "languages" (upper-case ISO 639-1 codes), "keywords" (comma-separated seed
    terms → suggestions with Reddit-wide monthly views), or
    "community_suggestions" (Reddit's related-community picks for seed
    communities in query, e.g. "PPC,googleads", and/or a website_url).
    Returns ids/names to pass into the targeting lists.
    """
    from adloop.reddit.read import search_reddit_targeting as _impl

    return _impl(
        current_config(), kind=kind, query=query, country=country, website_url=website_url, limit=limit
    )


@_tool(title="Reddit account change history", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_account_history(
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    entity_type: str = "",
    entity_ids: _StrListOpt = None,
    limit: int = 100,
) -> dict:
    """Who changed what in the Reddit ad account: field, before/after, member, time.

    The first thing to check when performance moves. Default window is the
    last 30 days (account-local days). Optionally filter to one entity_type
    ("campaign", "ad_group", "ad") with entity_ids; child entities are
    included. Changes made through AdLoop show under the connected Reddit
    user, like changes made in Ads Manager. Money fields are in account currency.
    """
    from adloop.reddit.read import get_reddit_account_history as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        entity_type=entity_type,
        entity_ids=entity_ids,
        limit=limit,
    )


@_tool(title="Estimate a Reddit ad group", annotations=_READONLY, tags={"reddit"})
@_safe
def estimate_reddit_ad_group(
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    objective: str = "CLICKS",
    bid_type: str = "CPC",
    bid_strategy: str = "",
    bid_value: float | None = None,
    optimization_goal: str = "",
    start_time: str = "",
    end_time: str = "",
    geolocations: _StrListOpt = None,
    excluded_geolocations: _StrListOpt = None,
    communities: _StrListOpt = None,
    excluded_communities: _StrListOpt = None,
    interests: _StrListOpt = None,
    keywords: _StrListOpt = None,
    languages: _StrListOpt = None,
    gender: str = "",
    platforms: _StrListOpt = None,
    ad_account_id: str = "",
) -> dict:
    """Audience size, delivery estimate and Reddit's suggested bid for a planned ad group — read-only.

    Reddit's counterpart of estimate_budget: takes the same targeting and
    budget as draft_reddit_ad_group, ahead of drafting. Returns the reachable and targetable
    audience (fixed 30-day basis), estimated impressions/clicks/reach for the
    schedule (default: tomorrow for 30 days), and the minimum/suggested
    bid range for bid_type in account currency. Nothing is created.
    """
    from adloop.reddit.read import estimate_reddit_ad_group as _impl

    targeting = {
        "geolocations": geolocations,
        "excluded_geolocations": excluded_geolocations,
        "communities": communities,
        "excluded_communities": excluded_communities,
        "interests": interests,
        "keywords": keywords,
        "languages": languages,
        "gender": gender.upper() if gender else None,
        "platforms": platforms,
    }
    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        objective=objective,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        bid_type=bid_type,
        bid_strategy=bid_strategy,
        bid_value=bid_value,
        optimization_goal=optimization_goal,
        start_time=start_time,
        end_time=end_time,
        targeting=targeting,
    )


@_tool(title="Draft pausing a Reddit entity", annotations=_WRITE, tags={"reddit"})
@_safe
def pause_reddit_entity(
    entity_type: str, entity_id: str, ad_account_id: str = ""
) -> dict:
    """Draft pausing a Reddit campaign, ad group or ad — returns a PREVIEW.

    entity_type: "campaign", "ad_group" or "ad"; entity_id from the read tools.
    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.reddit.write import pause_reddit_entity as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        entity_type=entity_type,
        entity_id=entity_id,
    )


@_tool(title="Draft enabling a Reddit entity", annotations=_WRITE, tags={"reddit"})
@_safe
def enable_reddit_entity(
    entity_type: str, entity_id: str, ad_account_id: str = ""
) -> dict:
    """Draft enabling (configured_status=ACTIVE) a Reddit campaign, ad group or ad — PREVIEW.

    Enabling a new ad sends it to Reddit policy review (PENDING_APPROVAL).
    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.reddit.write import enable_reddit_entity as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        entity_type=entity_type,
        entity_id=entity_id,
    )


@_tool(title="Draft archiving a Reddit entity", annotations=_DESTRUCTIVE, tags={"reddit"})
@_safe
def remove_reddit_entity(
    entity_type: str, entity_id: str, ad_account_id: str = ""
) -> dict:
    """Draft ARCHIVING a Reddit campaign, ad group or ad — irreversible, PREVIEW.

    Reddit has no hard delete for entities that ran; ARCHIVED is permanent.
    pause_reddit_entity is the reversible alternative when the entity does
    not need to be gone. Requires double confirmation; confirm_and_apply
    executes it.
    """
    from adloop.reddit.write import remove_reddit_entity as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        entity_type=entity_type,
        entity_id=entity_id,
    )


@_tool(title="Draft Reddit campaign changes", annotations=_WRITE, tags={"reddit"})
@_safe
def update_reddit_campaign(
    campaign_id: str,
    ad_account_id: str = "",
    name: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    spend_cap: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    bid_value: float | None = None,
    start_time: str = "",
    end_time: str = "",
    schedule: _DictListOpt = None,
) -> dict:
    """Draft changes to a Reddit campaign — name, run dates, spend cap, and (CBO only) budget/bid/schedule.

    daily_budget / lifetime_budget / bid_* apply only when the campaign uses
    campaign budget optimization; otherwise the budget lives on its ad groups
    (changed via update_reddit_ad_group). Budgets are in account currency and checked
    against max_daily_budget. The preview shows old → new per field.
    schedule: weekly delivery windows ("time of day" in Ads Manager), a list
    of blocks like {"days": "MON-FRI", "start_hour": 13, "end_hour": 23}
    (day names, hours 0-23, end_hour inclusive) or the native
    {"start_day": "FRI", "start_hour": 22, "end_day": "SAT", "end_hour": 3}.
    [] clears it (deliver at any time). Hours apply in each viewer's local
    time, not the account time zone.
    """
    from adloop.reddit.write import update_reddit_campaign as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        campaign_id=campaign_id,
        name=name,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        spend_cap=spend_cap,
        bid_strategy=bid_strategy,
        bid_type=bid_type,
        bid_value=bid_value,
        start_time=start_time,
        end_time=end_time,
        schedule=schedule,
    )


@_tool(title="Draft Reddit ad group changes", annotations=_WRITE, tags={"reddit"})
@_safe
def update_reddit_ad_group(
    ad_group_id: str,
    ad_account_id: str = "",
    name: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    bid_value: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    start_time: str = "",
    end_time: str = "",
    geolocations: _StrListOpt = None,
    excluded_geolocations: _StrListOpt = None,
    communities: _StrListOpt = None,
    excluded_communities: _StrListOpt = None,
    interests: _StrListOpt = None,
    keywords: _StrListOpt = None,
    excluded_keywords: _StrListOpt = None,
    languages: _StrListOpt = None,
    gender: str = "",
    platforms: _StrListOpt = None,
    expand_targeting: bool | None = None,
    schedule: _DictListOpt = None,
    locations: _StrListOpt = None,
) -> dict:
    """Draft changes to a Reddit ad group — budget, bid, run dates, weekly schedule, targeting.

    Budget (daily_budget or lifetime_budget + end_time) is checked against
    max_daily_budget; bid_value against max_bid_increase_pct. Targeting lists
    REPLACE the current value of each key passed (each takes the full list);
    omitted keys are preserved. Ids/names come from search_reddit_targeting.
    schedule: weekly delivery windows ("time of day" in Ads Manager), a list
    of blocks like {"days": "MON-FRI", "start_hour": 13, "end_hour": 23}
    (day names, hours 0-23, end_hour inclusive) or the native
    {"start_day": "FRI", "start_hour": 22, "end_day": "SAT", "end_hour": 3}.
    [] clears it (deliver at any time). Hours apply in each viewer's local
    time, not the account time zone.
    locations: placements, FEED and/or COMMENTS_PAGE (conversation pages).
    """
    from adloop.reddit.write import update_reddit_ad_group as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        ad_group_id=ad_group_id,
        name=name,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        bid_value=bid_value,
        bid_strategy=bid_strategy,
        bid_type=bid_type,
        start_time=start_time,
        end_time=end_time,
        geolocations=geolocations,
        excluded_geolocations=excluded_geolocations,
        communities=communities,
        excluded_communities=excluded_communities,
        interests=interests,
        keywords=keywords,
        excluded_keywords=excluded_keywords,
        languages=languages,
        gender=gender,
        platforms=platforms,
        expand_targeting=expand_targeting,
        schedule=schedule,
        locations=locations,
    )


@_tool(title="Draft Reddit ad changes", annotations=_WRITE, tags={"reddit"})
@_safe
def update_reddit_ad(
    ad_id: str,
    ad_account_id: str = "",
    name: str = "",
    click_url: str = "",
    allow_comments: bool | None = None,
) -> dict:
    """Draft changes to a Reddit ad — name, landing URL (click_url), comments on/off.

    click_url is verified to be reachable. The headline and body of a live
    Reddit post cannot be edited; new copy means a new ad via draft_reddit_ad,
    with the old ad paused. Preview shows old → new; confirm_and_apply executes.
    """
    from adloop.reddit.write import update_reddit_ad as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        ad_id=ad_id,
        name=name,
        click_url=click_url,
        allow_comments=allow_comments,
    )


@_tool(title="Draft a Reddit campaign", annotations=_WRITE, tags={"reddit"})
@_safe
def draft_reddit_campaign(
    campaign_name: str,
    objective: str,
    funding_instrument_id: str,
    ad_account_id: str = "",
    campaign_budget_optimization: bool = False,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    bid_value: float | None = None,
    optimization_goal: str = "",
    conversion_pixel_id: str = "",
    spend_cap: float | None = None,
    start_time: str = "",
    end_time: str = "",
    schedule: _DictListOpt = None,
) -> dict:
    """Draft a new Reddit campaign (created PAUSED) — returns a PREVIEW.

    objective: CLICKS, CONVERSIONS, IMPRESSIONS, LEAD_GENERATION, APP_INSTALLS,
    CATALOG_SALES or VIDEO_VIEWABLE_IMPRESSIONS. funding_instrument_id from
    list_reddit_funding_instruments. By default the budget lives on the ad
    groups; campaign_budget_optimization=true holds it on the campaign
    (then daily_budget or lifetime_budget, bid_strategy, bid_type and
    conversion_pixel_id are required). Budgets are checked against
    max_daily_budget. Times are ISO 8601. Ad groups are added with
    draft_reddit_ad_group.
    schedule: weekly delivery windows ("time of day" in Ads Manager), a list
    of blocks like {"days": "MON-FRI", "start_hour": 13, "end_hour": 23}
    (day names, hours 0-23, end_hour inclusive) or the native
    {"start_day": "FRI", "start_hour": 22, "end_day": "SAT", "end_hour": 3}.
    [] clears it (deliver at any time). Hours apply in each viewer's local
    time, not the account time zone.
    """
    from adloop.reddit.write import draft_reddit_campaign as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        campaign_name=campaign_name,
        objective=objective,
        funding_instrument_id=funding_instrument_id,
        campaign_budget_optimization=campaign_budget_optimization,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        bid_strategy=bid_strategy,
        bid_type=bid_type,
        bid_value=bid_value,
        optimization_goal=optimization_goal,
        conversion_pixel_id=conversion_pixel_id,
        spend_cap=spend_cap,
        start_time=start_time,
        end_time=end_time,
        schedule=schedule,
    )


@_tool(title="Draft a Reddit ad group", annotations=_WRITE, tags={"reddit"})
@_safe
def draft_reddit_ad_group(
    campaign_id: str,
    ad_group_name: str,
    conversion_pixel_id: str,
    ad_account_id: str = "",
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    bid_strategy: str = "",
    bid_type: str = "",
    bid_value: float | None = None,
    optimization_goal: str = "",
    geolocations: _StrListOpt = None,
    excluded_geolocations: _StrListOpt = None,
    communities: _StrListOpt = None,
    excluded_communities: _StrListOpt = None,
    interests: _StrListOpt = None,
    keywords: _StrListOpt = None,
    excluded_keywords: _StrListOpt = None,
    languages: _StrListOpt = None,
    gender: str = "",
    platforms: _StrListOpt = None,
    expand_targeting: bool | None = None,
    start_time: str = "",
    end_time: str = "",
    schedule: _DictListOpt = None,
    locations: _StrListOpt = None,
) -> dict:
    """Draft a new Reddit ad group (created PAUSED) — returns a PREVIEW.

    Required: campaign_id, ad_group_name, conversion_pixel_id (Reddit rule;
    from get_reddit_pixels) and at least one targeting list (geolocations,
    communities, interests or keywords — ids/names from
    search_reddit_targeting). For non-CBO campaigns also daily_budget (or
    lifetime_budget + end_time), bid_strategy (BIDLESS, MANUAL_BIDDING,
    MAXIMIZE_VOLUME, TARGET_CPX) and bid_type (CPC, CPM, CPV, CPV6, CPV15);
    MANUAL_BIDDING/TARGET_CPX need bid_value. optimization_goal is the pixel
    event to optimize for (PURCHASE, SIGN_UP, LEAD, PAGE_VISIT, ...).
    Budgets are checked against max_daily_budget.

    schedule: weekly delivery windows ("time of day" in Ads Manager), a list
    of blocks like {"days": "MON-FRI", "start_hour": 13, "end_hour": 23}
    (day names, hours 0-23, end_hour inclusive) or the native
    {"start_day": "FRI", "start_hour": 22, "end_day": "SAT", "end_hour": 3}.
    Omitted means delivery at any time. Hours apply in each viewer's local time,
    not the account time zone.
    locations: placements, FEED and/or COMMENTS_PAGE (conversation pages).
    """
    from adloop.reddit.write import draft_reddit_ad_group as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        campaign_id=campaign_id,
        ad_group_name=ad_group_name,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        bid_strategy=bid_strategy,
        bid_type=bid_type,
        bid_value=bid_value,
        optimization_goal=optimization_goal,
        conversion_pixel_id=conversion_pixel_id,
        geolocations=geolocations,
        excluded_geolocations=excluded_geolocations,
        communities=communities,
        excluded_communities=excluded_communities,
        interests=interests,
        keywords=keywords,
        excluded_keywords=excluded_keywords,
        languages=languages,
        gender=gender,
        platforms=platforms,
        expand_targeting=expand_targeting,
        start_time=start_time,
        end_time=end_time,
        schedule=schedule,
        locations=locations,
    )


@_tool(title="Draft a Reddit ad", annotations=_WRITE, tags={"reddit"})
@_safe
def draft_reddit_ad(
    ad_group_id: str,
    profile_id: str = "",
    headline: str = "",
    click_url: str = "",
    ad_account_id: str = "",
    ad_name: str = "",
    post_type: str = "TEXT",
    body: str = "",
    image_url: str = "",
    call_to_action: str = "",
    display_url: str = "",
    allow_comments: bool = True,
    post_id: str = "",
) -> dict:
    """Draft a Reddit ad: creates a post on the profile, then the ad (PAUSED) — PREVIEW.

    profile_id from list_reddit_funding_instruments (profiles). post_type TEXT
    (headline + optional body) or IMAGE (headline + public image_url).
    click_url is verified to be reachable before drafting, so ads do not
    point at unverified pages. call_to_action is one of Reddit's fixed labels
    (Learn More, Sign Up, Shop Now, Download, ...). Comments are public on
    Reddit ads; allow_comments=false disables them.

    post_id (t3_...) promotes an EXISTING post instead, keeping its upvotes
    and comments: no post is created, headline/body/image are ignored, and
    profile_id defaults to the post's. TEXT posts take no click_url (they
    open themselves); media posts default click_url to the post's destination.
    """
    from adloop.reddit.write import draft_reddit_ad as _impl

    return _impl(
        current_config(),
        ad_account_id=_reddit_account(ad_account_id),
        ad_group_id=ad_group_id,
        ad_name=ad_name,
        profile_id=profile_id,
        headline=headline,
        post_type=post_type,
        click_url=click_url,
        body=body,
        image_url=image_url,
        call_to_action=call_to_action,
        display_url=display_url,
        allow_comments=allow_comments,
        post_id=post_id,
    )


# ---------------------------------------------------------------------------
# Tracking Tools
# ---------------------------------------------------------------------------


@_tool(title="Draft an Analytics key event", annotations=_WRITE, tags={"ga4"})
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
    (sign-ups). The returned plan_id is applied with confirm_and_apply.
    Applies to future data only.
    """
    from adloop.ga4.write import draft_key_event as _impl

    return _impl(
        current_config(),
        property_id=property_id or current_config().ga4.property_id,
        event_name=event_name,
        counting_method=counting_method,
    )


@_tool(title="Draft a conversion action", annotations=_WRITE, tags={"ads"})
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
    """Draft a new Google Ads ConversionAction — returns a PREVIEW.

    type_: AD_CALL, WEBSITE_CALL, WEBPAGE, WEBPAGE_CODELESS,
      GOOGLE_ANALYTICS_4_CUSTOM, etc. category: PHONE_CALL_LEAD,
      SUBMIT_LEAD_FORM, PURCHASE, ... (default DEFAULT).

    A positive default_value with always_use_default_value=False is a legal
    "fallback" config — the preview warns but does NOT flip the flag.
    include_in_conversions_metric is IMMUTABLE on create (change it later via
    draft_update_conversion_action). The returned plan_id is applied with
    confirm_and_apply.
    """
    from adloop.ads.conversion_actions import (
        draft_create_conversion_action as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
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


@_tool(title="Draft conversion action changes", annotations=_WRITE, tags={"ads"})
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
    """Draft a partial UPDATE of an existing ConversionAction — returns PREVIEW.

    Only parameters passed non-empty/non-default are sent to the API.
    Covers renaming, demoting a Primary to Secondary, changing value,
    adjusting the call-duration threshold, or changing attribution. The ID
    comes from: SELECT conversion_action.id, conversion_action.name FROM
    conversion_action.
    The returned plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.conversion_actions import (
        draft_update_conversion_action as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
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


@_tool(title="Draft removing a conversion action", annotations=_WRITE, tags={"ads"})
@_safe
def draft_remove_conversion_action(
    conversion_action_id: str,
    customer_id: str = "",
) -> dict:
    """Draft a REMOVAL of a ConversionAction — returns a PREVIEW.

    Removal stops counting and drops the action from goal lists; historical
    data is preserved but the action is irreversible. SMART_CAMPAIGN_* and
    GOOGLE_HOSTED types reject removal with MUTATE_NOT_ALLOWED. The returned
    plan_id is applied with confirm_and_apply.
    """
    from adloop.ads.conversion_actions import (
        draft_remove_conversion_action as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        conversion_action_id=conversion_action_id,
    )


@_tool(title="Validate tracking", annotations=_READONLY, tags={"tracking"})
@_safe
def validate_tracking(
    expected_events: _StrList,
    property_id: str = "",
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
) -> dict:
    """Compare tracking events found in the codebase against actual GA4 data.

    Takes the event names found in the codebase's gtag('event', ...) or
    dataLayer.push calls and checks which ones actually fire in GA4.

    Returns: matched events, events missing from GA4, unexpected GA4 events,
    and auto-collected events (page_view, session_start, etc.).
    """
    from adloop.tracking import validate_tracking as _impl

    return _impl(
        current_config(),
        expected_events=expected_events,
        property_id=property_id or current_config().ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@_tool(title="Generate tracking code", annotations=_READONLY, tags={"tracking"})
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


@_tool(title="Budget forecast", annotations=_READONLY, tags={"ads"})
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


@_tool(title="Keyword ideas", annotations=_READONLY, tags={"ads"})
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
    - Start with keywords: seed_keywords (e.g. ["running shoes"])
    - Start with a website: url (e.g. "https://example.com/products")
    - Both together: keywords + url for more targeted ideas

    include_monthly_volumes=true adds per-month search history (last 24
    months, top-20 ideas) plus a seasonality insight — relevant to questions
    about demand trends, seasonality, or "when should I ramp budget".

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
# Google Ads — Brand Tools
# ---------------------------------------------------------------------------


@_tool(title="Suggest brands", annotations=_READONLY, tags={"ads"})
@_safe
def suggest_brands(
    brand_prefix: str,
    selected_brand_ids: _StrList = [],  # noqa: B006 — mutable default required for MCP JSON schema
    customer_id: str = "",
) -> dict:
    """Resolve a brand name to the brands Google recognizes for it.

    Mirrors the brand picker in the Google Ads UI
    (BrandSuggestionService.SuggestBrands): takes a free-text name such as
    "EscapeGame München" or "NoWayOut" and returns the matching brands with
    their ID, display name, state, and associated URLs.

    The returned brand ID is the Commercial Knowledge Graph ID. Brand
    criteria — brand lists, brand exclusions — target that ID, not a display
    name, so a name is resolved here before it can go into a brand list.

    selected_brand_ids: IDs already picked, handed back so Google keeps them
    in the suggestion set while the prefix narrows. Optional.

    Args:
        brand_prefix: The brand name to look up, free text (e.g. "NoWayOut").
        selected_brand_ids: Commercial Knowledge Graph MIDs already picked;
            optional, Google keeps them in the suggestion set.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.brands import suggest_brands as _impl

    return _impl(
        current_config(),
        brand_prefix=brand_prefix,
        selected_brand_ids=selected_brand_ids,
        customer_id=customer_id or current_config().ads.customer_id,
    )


@_tool(title="Check brand names", annotations=_READONLY, tags={"ads"})
@_safe
def check_brand_names(
    brand_names: _StrList,
    customer_id: str = "",
) -> dict:
    """Check a list of brand names against Google's brand knowledge graph.

    One SuggestBrands call per name, for the case where a shortlist has to be
    triaged instead of a single name resolved: which of these brands does
    Google know at all, and what is the ID of each?

    Every entry answers with status "matched" (with a best-match brand plus
    all candidates) or "no_match" (empty candidate list — a normal answer,
    not an error). exact_match marks a candidate whose name matches the query
    apart from case and punctuation; anything else is a Google suggestion,
    not a guarantee.

    At most 25 names per call — the API resolves one prefix per request, so
    longer lists take several calls.

    Args:
        brand_names: The names to triage, at most 25 per call.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.brands import check_brand_names as _impl

    return _impl(
        current_config(),
        brand_names=brand_names,
        customer_id=customer_id or current_config().ads.customer_id,
    )

@_tool(title="List brand lists", annotations=_READONLY, tags={"ads"})
@_safe
def get_brand_lists(
    customer_id: str = "",
) -> dict:
    """List all brand lists (SharedSets of type BRANDS) in the account.

    Returns each list's ID, name, status, and member count. Useful before
    propose_brand_list, so an existing list can be reused instead of duplicated.

    Args:
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.brands import get_brand_lists as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
    )

@_tool(title="Brands in a brand list", annotations=_READONLY, tags={"ads"})
@_safe
def get_brand_list_brands(
    shared_set_id: str,
    customer_id: str = "",
) -> dict:
    """List the brands inside a brand list.

    Each entry carries brand.entity_id (the Commercial KG MID that
    suggest_brands returns as id), the display name, primary URL, status and a
    criterion_id — the latter is what remove_from_brand_list needs.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists (shared_set.id).
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.brands import get_brand_list_brands as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
    )

@_tool(title="Campaigns using a brand list", annotations=_READONLY, tags={"ads"})
@_safe
def get_brand_list_campaigns(
    shared_set_id: str = "",
    customer_id: str = "",
) -> dict:
    """List which campaigns a brand list is attached to.

    Brand lists attach as CampaignCriterion rows of type BRAND_LIST (not as
    CampaignSharedSet like negative keyword lists), so each entry also reports
    `role`: "excluded" when the criterion is negative, "targeted" when it
    restricts targeting to the list.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists. When omitted,
            all brand-list attachments in the account are returned.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.brands import get_brand_list_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
    )

@_tool(title="Draft a brand list", annotations=_WRITE, tags={"ads"})
@_safe
def propose_brand_list(
    list_name: str,
    brand_ids: _StrList,
    campaign_ids: _StrList = [],  # noqa: B006 — mutable default required for MCP JSON schema
    negative: bool = True,
    customer_id: str = "",
) -> dict:
    """Draft a brand list and optionally attach it to campaigns — returns a PREVIEW.

    Creates a SharedSet of type BRANDS, fills it with brands, and — when
    campaign_ids is given — attaches it to those campaigns.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        list_name: Name for the new list, as it should appear in Google Ads.
        brand_ids: Commercial Knowledge Graph MIDs — the `id` field from
            suggest_brands / check_brand_names, which resolve names; a
            display name alone cannot be written.
        campaign_ids: Optional. When omitted, the list is created without
            being used yet; attach_brand_list_to_campaigns attaches it later.
        negative: True (default) excludes the brands from those campaigns;
            False restricts targeting to them instead.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import propose_brand_list as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        list_name=list_name,
        brand_ids=brand_ids,
        campaign_ids=campaign_ids,
        negative=negative,
    )

@_tool(title="Draft additions to a brand list", annotations=_WRITE, tags={"ads"})
@_safe
def add_to_brand_list(
    shared_set_id: str,
    brand_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Append brands to an EXISTING brand list — returns a PREVIEW.

    For a list that already exists and only needs more brands
    (propose_brand_list creates a new list instead). The shared_set_id comes
    from get_brand_lists; get_brand_list_brands shows the brands already in
    the list, which makes a brand that would be added twice visible.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists.
        brand_ids: Commercial Knowledge Graph MIDs from suggest_brands.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import add_to_brand_list as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        brand_ids=brand_ids,
    )

@_tool(title="Draft removing brands from a list", annotations=_DESTRUCTIVE, tags={"ads"})
@_safe
def remove_from_brand_list(
    shared_set_id: str,
    criterion_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Remove brands from a brand list — returns a PREVIEW.

    SharedCriteria have no status field, so removal is the only way to take a
    brand out of a list — there is nothing to pause. Removing a brand does not
    detach the list from any campaign.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists.
        criterion_ids: Numeric criterion_id values from get_brand_list_brands.
            This is irreversible — the brand leaves the list immediately.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import remove_from_brand_list as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        criterion_ids=criterion_ids,
    )

@_tool(title="Draft attaching a brand list", annotations=_WRITE, tags={"ads"})
@_safe
def attach_brand_list_to_campaigns(
    shared_set_id: str,
    campaign_ids: _StrList,
    negative: bool = True,
    customer_id: str = "",
) -> dict:
    """Attach an existing brand list to one or more campaigns — returns a PREVIEW.

    Creates a CampaignCriterion.brand_list per campaign. This is NOT the
    CampaignSharedSet linkage used for negative keyword lists — brand lists are
    criteria, and `negative` decides the role: True excludes the brands
    (default), False restricts targeting to the list.

    The shared_set_id comes from get_brand_lists; get_brand_list_campaigns
    lists existing attachments.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists.
        campaign_ids: Numeric campaign IDs to attach the list to.
        negative: True (default) excludes the brands from those campaigns;
            False restricts targeting to them instead.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import attach_brand_list_to_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        campaign_ids=campaign_ids,
        negative=negative,
    )

@_tool(title="Draft detaching a brand list", annotations=_DESTRUCTIVE, tags={"ads"})
@_safe
def detach_brand_list_from_campaigns(
    shared_set_id: str,
    campaign_ids: _StrList,
    customer_id: str = "",
) -> dict:
    """Detach a brand list from one or more campaigns — returns a PREVIEW.

    Removes the CampaignCriterion rows linking the list to those campaigns; the
    list itself and its brands are unchanged. Campaigns that do not carry the
    list are reported as `not_attached` in the apply result instead of
    failing the batch.

    get_brand_list_campaigns lists the existing attachments.

    The returned plan_id is applied with confirm_and_apply.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists.
        campaign_ids: Numeric campaign IDs to detach the list from. Campaigns
            that do not carry the list are reported, not treated as errors.
        customer_id: Ads account ID. Defaults to the configured account.
    """
    from adloop.ads.write import detach_brand_list_from_campaigns as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        shared_set_id=shared_set_id,
        campaign_ids=campaign_ids,
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
