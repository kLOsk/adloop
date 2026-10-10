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
    "gtm": "Google Tag Manager reads and (opt-in) writes",
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
        "validate_only (nothing executes); DRY_RUN_FAILED means Google "
        "rejected the request, so nothing was changed and the apply would be "
        "rejected too. Per-row problems in an upload are not a rejection: they "
        "come back as `row_errors` with DRY_RUN_SUCCESS, and the apply would "
        "carry out the other rows.\n"
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

    Returns the property, the date range, the dimension and metric headers,
    one row per dimension combination (values as strings), row_count and
    total_row_count. At least one dimension or metric is required.

    Queries the GA4 Data API. Dimensions and metrics:
    https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema

    Args:
        dimensions: GA4 dimension API names, e.g. date, pagePath,
            sessionSource, sessionMedium, country, deviceCategory, eventName.
        metrics: GA4 metric API names, e.g. sessions, totalUsers, newUsers,
            screenPageViews, conversions, eventCount, bounceRate.
        date_range_start: Start date: "today", "yesterday", "NdaysAgo" (e.g.
            "7daysAgo", "28daysAgo", "90daysAgo"), or "YYYY-MM-DD".
        date_range_end: End date, same formats as date_range_start.
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
        limit: Maximum number of rows returned.
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
    Returns the dimension and metric headers and one row per dimension
    combination over the realtime window (the last 30 minutes).

    Args:
        dimensions: GA4 realtime dimension API names, e.g. unifiedScreenName,
            eventName, country, deviceCategory.
        metrics: GA4 realtime metric API names, e.g. activeUsers, eventCount.
            Defaults to ["activeUsers"].
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
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

    Returns every distinct event name with its total event count, sorted by
    count (highest first). Shows what tracking is configured and active.

    Args:
        date_range_start: Start date: "today", "yesterday", "NdaysAgo" (e.g.
            "28daysAgo"), or "YYYY-MM-DD".
        date_range_end: End date, same formats as date_range_start.
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
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

    Queries the Search Console API:
    https://developers.google.com/webmaster-tools/v1/searchanalytics/query

    Args:
        site_url: The GSC property URL (e.g. "https://example.com/" or
            "sc-domain:example.com"), as listed by list_gsc_sites. Defaults to
            the configured Search Console site (gsc.site_url).
        dimensions: One or more of "query", "page", "country", "device",
            "date". Defaults to ["query"].
        date_range_start: Start date as ISO "YYYY-MM-DD" or a relative value
            like "7daysAgo", "30daysAgo", "today".
        date_range_end: End date, same formats as date_range_start.
        limit: Maximum rows to return (default 100, max 25000; higher values
            are capped at 25000).
        search_type: "web" (default), "image", "video", "news", "discover",
            or "googleNews".
        dimension_filter_groups: Optional list of GSC DimensionFilterGroup
            objects to filter by query, page, country, or device, e.g.
            [{"filters": [{"dimension": "query", "operator": "contains",
            "expression": "analytics"}]}].
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
    Score and waste paid clicks. Takes 10-30s; that is normal for a
    Lighthouse run.

    Args:
        url: Full URL of the page to analyze, e.g. an ad's final_url
            ("https://example.com/landing").
        strategy: "mobile" (default — most paid traffic) or "desktop".
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
    campaigns; this reports approved/pending/disapproved counts per
    reporting context (Shopping ads, free listings, ...), the top product
    issues by affected products (with documentation links), and
    account-level issues — CRITICAL ones stop offers serving entirely.
    Product-status data lags reality by ~30 minutes.

    Args:
        account_id: Numeric Merchant Center ID from list_merchant_accounts.
            Defaults to merchant.account_id in the config.
    """
    from adloop.merchant.read import get_merchant_feed_health as _impl

    return _impl(current_config(), account_id=account_id)


@_tool(title="List Google Ads accounts", annotations=_READONLY, tags={"ads"})
@_safe
def list_accounts(limit: int = 200) -> dict:
    """List accessible Google Ads accounts.

    Returns account names, IDs, status, and whether each is a manager account.
    When more accounts exist than `limit`, the response has
    'truncated: true'; a much higher limit (e.g. list_accounts(limit=1000))
    returns the full list. Workflows that target a specific account need no
    enumeration at all: get_campaign_performance, run_gaql, etc. take
    customer_id directly.

    Args:
        limit: Maximum number of accounts listed under the manager (MCC)
            account. The default cap of 200 covers the vast majority of agency
            MCCs in one call.
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

    Returns campaign name, status, type, impressions, clicks, cost,
    conversions, CPA, ROAS, CTR for each campaign, plus Search impression
    share and the share lost to budget and to ad rank (fractions with
    matching *_pct percentages; null for campaigns that do not serve on
    Search or had no impressions). Insights flag converting campaigns that
    lose a significant share of impressions to budget.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        compact: When true (for audits/overviews on large accounts), returns
            account totals, status/type breakdowns, the top-10 spenders,
            zero-conversion offenders and budget-limited converters instead
            of every row (~90% smaller).
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

    Returns ad type, headlines, descriptions, final URL, impressions,
    clicks, CTR, conversions, cost for each ad, plus its policy approval
    status, review status and policy topic names. Disapproved and
    limited-by-policy ads are listed under policy_issues with their spend.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        compact: When true (for audits/overviews), returns totals, the top-10
            ads with headline/description COUNTS instead of full asset lists,
            plus incomplete-RSA, single-ad ad-group and policy findings with
            approval-status counts (~90% smaller).
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

    Returns keyword text, match type, quality score, impressions,
    clicks, CTR, CPC, conversions for each keyword.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        compact: When true (for audits/overviews), returns totals, match-type
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

    Critical for finding negative keyword opportunities and understanding user
    intent. Returns search term, campaign, ad group, impressions, clicks,
    conversions.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        compact: When true (for audits/overviews), returns totals, the top-10
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
    negative keywords are added. Returns each negative's campaign, keyword
    text, match type, and a resource_id ("campaignId~criterionId") as taken
    by remove_entity, plus the total count.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        campaign_id: Numeric campaign ID to filter by. If empty, returns
            negatives across all campaigns.
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

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
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

    Returns each negative keyword's text, match type, and a resource_id
    ("sharedSetId~criterionId"), with the total keyword count.

    Args:
        shared_set_id: Numeric ID from get_negative_keyword_lists (shared_set.id).
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
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

    Returns each list-to-campaign attachment with the list and campaign IDs
    and names.

    Args:
        shared_set_id: Numeric ID from get_negative_keyword_lists. When
            omitted, returns all list-to-campaign attachments across the account.
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
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
    Includes insights that flag budget-increase recommendations (their projected
    gain comes from spending more) and highlight high-impact suggestions.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        recommendation_types: Optional filter of Google Ads RecommendationType
            names, e.g. ["KEYWORD", "TARGET_CPA_OPT_IN",
            "MAXIMIZE_CONVERSIONS_OPT_IN", "RESPONSIVE_SEARCH_AD"].
            Empty = all types.
        campaign_id: Optional numeric campaign ID to scope to a single campaign.
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

    Returns two result sets. campaigns: PMax campaign metrics broken down by
    ad_network_type (SEARCH, CONTENT, YOUTUBE_SEARCH, YOUTUBE_WATCH, MIXED).
    MIXED is a catch-all that Google uses for most PMax traffic — full channel
    splits are not available via the API. asset_groups: per-asset-group
    metrics including ad_strength (EXCELLENT, GOOD, AVERAGE, POOR).

    Includes insights flagging weak ad strength, zero-conversion asset groups,
    and network type distribution.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
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
    PENDING), and content (text or image URL), plus by_status and
    by_field_type summaries.

    Per-asset performance labels (BEST/GOOD/LOW) are not available for
    PMax assets in the Google Ads API. get_detailed_asset_performance reports
    which asset combinations Google selects most — the closest proxy for
    individual asset quality.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        campaign_id: Optional numeric ID to filter to a single PMax campaign.
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
    most often. Returns each combination with the assets used and their field
    types. This data helps identify which creative elements work well together.

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        campaign_id: Optional numeric ID to filter to a single PMax campaign.
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

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        campaign_id: Optional numeric ID to filter to a single campaign.
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

    Takes exactly one of ad_group_id or campaign_id. Returns each
    criterion's value, whether it's negative (excluded) or positive
    (narrowing), status, and a remove_id (composite resource ID) that
    can be passed directly to remove_entity with
    entity_type='ad_group_criterion' or 'campaign_criterion'.

    By default, Google Ads serves ads to ALL demographic segments — a
    criterion only appears here once a segment has been actively excluded
    or narrowed.

    Args:
        ad_group_id: Numeric ad group ID. Mutually exclusive with campaign_id.
        campaign_id: Numeric campaign ID. Mutually exclusive with ad_group_id.
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
    """
    from adloop.ads.read import get_demographic_targeting as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        ad_group_id=ad_group_id,
        campaign_id=campaign_id,
    )


@_tool(title="Change history", annotations=_READONLY, tags={"ads"})
@_safe
def get_change_history(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
    resource_types: _StrListOpt = None,
    limit: int = 1000,
) -> dict:
    """List recent account changes: who changed what, when, and through which tool.

    Answers "what changed before the drop?". Returns each change newest
    first with its time, the user's email, the client it came through
    (Google Ads UI, API, Google Ads scripts, Editor, automated rules,
    auto-applied recommendations, ...), the changed resource type, the
    operation (CREATE, UPDATE, REMOVE), the changed field paths, and the
    campaign and ad group names. Also returns counts by resource type,
    client, user and day, plus insights on auto-applied recommendations
    and budget or bidding changes. Google Ads keeps change history for 30
    days, so older start dates are moved to the earliest available day.

    Reads the change_event resource:
    https://developers.google.com/google-ads/api/docs/change-event

    Args:
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        date_range_start: Start date as "YYYY-MM-DD", at most 30 days back.
            Empty starts at the oldest day Google Ads still keeps.
        date_range_end: End date as "YYYY-MM-DD" (inclusive). Empty means today.
        campaign_id: Optional numeric campaign ID; only changes attributed to
            that campaign are returned.
        resource_types: Optional filter of ChangeEventResourceType names,
            e.g. ["CAMPAIGN", "CAMPAIGN_BUDGET", "AD_GROUP_AD",
            "AD_GROUP_CRITERION"]. Empty = all types.
        limit: Maximum number of changes returned, newest first (1 to
            10000; default 1000). The response is marked truncated when
            the limit is reached.
    """
    from adloop.ads.read import get_change_history as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_id=campaign_id,
        resource_types=resource_types,
        limit=limit,
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

    Args:
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
        campaign_name: Optional case-insensitive substring; only campaigns
            whose name contains it are included. Empty = all campaigns.
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

    Combines ad final URLs with GA4 page-level data. Returns paid traffic
    sessions, conversion rates, bounce rates, and engagement per landing page,
    and identifies pages that get ad clicks but zero conversions and orphaned
    URLs.

    Args:
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
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
    conversion event configuration. Returns both sides' totals with the
    discrepancy and diagnostic insights.

    Args:
        date_range_start: Start date as "YYYY-MM-DD". Both dates must be set
            to apply a range; if either is empty, the last 30 days are used.
        date_range_end: End date as "YYYY-MM-DD" (inclusive).
        customer_id: Google Ads customer ID, digits with or without dashes
            (e.g. "123-456-7890"). Defaults to the configured ads.customer_id.
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
        conversion_events: Optional list of GA4 event names to specifically
            check (e.g. ["sign_up", "purchase"]). If omitted, compares
            aggregate totals only.
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

    Fetches the LIVE GTM container, joins it against GA4 event counts for the
    date range, and returns a per-event matrix with one of these statuses:
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

    The result also lists dynamic-event tags ({{Event}} variables) and Custom
    HTML tags that the audit cannot interpret automatically.

    Args:
        expected_events: Distinct event names found in the codebase's
            gtag('event', ...) and dataLayer.push({event: ...}) calls.
        gtm_account_id: Numeric GTM account ID (Tag Manager UI → Admin →
            Container Settings, or list_gtm_accounts). Empty uses
            gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (not the GTM-XXXXXXX public
            ID; see list_gtm_containers). Empty uses gtm.container_id from the
            config.
        property_id: GA4 property as "properties/123456789". Empty uses the
            default from config.
        date_range_start: Start date "YYYY-MM-DD". Empty (or an empty
            date_range_end) means the last 30 days.
        date_range_end: End date "YYYY-MM-DD". Empty (or an empty
            date_range_start) means the last 30 days.
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
    audit_event_coverage takes. Returns each account's ID and name. An empty
    list means the service account hasn't been added to any GTM container
    with at least Read permission.
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

    Args:
        gtm_account_id: Numeric GTM account ID from list_gtm_accounts. Empty
            uses gtm.account_id from the config; the tool errors when neither
            is set.
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

    Args:
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers),
            not the GTM-XXXXXXX public ID. Empty uses gtm.container_id from
            the config.
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
    audit_event_coverage. An unknown tag_id returns an error with the
    available tag IDs.

    Args:
        tag_id: Numeric GTM tag ID, as listed by list_gtm_tags.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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

    Args:
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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
    diagnose why a tag with a specific trigger ID does or doesn't fire. An
    unknown trigger_id returns an error with the available trigger IDs.

    Args:
        trigger_id: Numeric GTM trigger ID, as listed by list_gtm_triggers or
            a tag's firing/blocking triggers.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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

    Args:
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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

    Returns each workspace's ID, name and description; the IDs are what
    `get_gtm_workspace_diff` takes. Most containers have a single Default
    Workspace; multiple workspaces appear when the team uses parallel drafts.

    Args:
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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

    Args:
        workspace_id: Numeric GTM workspace ID from list_gtm_workspaces.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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

    Args:
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
        page_size: Maximum number of version headers returned (newest first).
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

    Args:
        container_version_id: Numeric container version ID from
            list_gtm_versions.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
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
# Google Tag Manager — writes (opt-in: gtm.write_enabled)
# ---------------------------------------------------------------------------


@_tool(title="Draft a Tag Manager tag", annotations=_WRITE, tags={"gtm"})
@_safe
def draft_gtm_tag(
    name: str = "",
    tag_type: str = "",
    parameters: _DictListOpt = None,
    firing_trigger_ids: _StrListOpt = None,
    blocking_trigger_ids: _StrListOpt = None,
    paused: bool | None = None,
    notes: str | None = None,
    tag_id: str = "",
    workspace_id: str = "",
    gtm_account_id: str = "",
    gtm_container_id: str = "",
) -> dict:
    """Draft creating or updating a Tag Manager tag — returns a PREVIEW.

    Without tag_id this creates a tag in a workspace; with tag_id it updates
    the existing tag. The edit lands in the workspace and changes nothing on
    the site until the workspace is published. Refused unless
    gtm.write_enabled is set, and Custom HTML (html) is refused as well
    unless gtm.allow_custom_html is set. Returns the resolved workspace, the
    tag type, the fields that would change and a plan_id.

    Args:
        name: Tag name, 1-200 characters. Required when creating a tag.
        tag_type: Tag type, required when creating a tag because GTM cannot
            change a tag's type later: googtag, gaawe (GA4 event), awct (Ads
            conversion), awcc, awud, sp (Ads remarketing), gclidw, flc, fls,
            img, html (Custom HTML) or cvt_<id> for a Community Gallery
            template.
        parameters: GTM parameter dicts with at least "type" and "key", for
            example gaawe [{"type": "TEMPLATE", "key": "eventName", "value":
            "form_submit"}] or awct [{"type": "TEMPLATE", "key":
            "conversionId", "value": "123456789"}, {"type": "TEMPLATE",
            "key": "conversionLabel", "value": "AbC-dEf"}]. On update they
            merge by key: passed keys replace, other keys stay.
        firing_trigger_ids: Trigger IDs that fire the tag (see
            list_gtm_triggers). The built-in All Pages trigger is
            "2147479553".
        blocking_trigger_ids: Trigger IDs that must not fire the tag.
        paused: True stores the tag paused instead of deleting it. None keeps
            the current state.
        notes: Free-form note stored on the tag. None keeps the current note.
        tag_id: Existing tag ID to update (see list_gtm_tags). Empty creates
            a new tag.
        workspace_id: Workspace to edit (see list_gtm_workspaces). Empty uses
            the Default Workspace, or the only workspace there is.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
    """
    from adloop.gtm.write import draft_gtm_tag as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(gtm_account_id, gtm_container_id)
    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        tag_id=tag_id,
        workspace_id=workspace_id,
        name=name,
        tag_type=tag_type,
        parameters=parameters,
        firing_trigger_ids=firing_trigger_ids,
        blocking_trigger_ids=blocking_trigger_ids,
        paused=paused,
        notes=notes,
    )


@_tool(title="Draft a Tag Manager trigger", annotations=_WRITE, tags={"gtm"})
@_safe
def draft_gtm_trigger(
    name: str = "",
    trigger_type: str = "",
    custom_event_name: str = "",
    filters: _DictListOpt = None,
    custom_event_filters: _DictListOpt = None,
    auto_event_filters: _DictListOpt = None,
    parameters: _DictListOpt = None,
    notes: str | None = None,
    trigger_id: str = "",
    workspace_id: str = "",
    gtm_account_id: str = "",
    gtm_container_id: str = "",
) -> dict:
    """Draft creating or updating a Tag Manager trigger — returns a PREVIEW.

    Without trigger_id this creates a trigger in a workspace; with trigger_id
    it updates the existing one. The edit lands in the workspace and changes
    nothing on the site until the workspace is published. Refused unless
    gtm.write_enabled is set. Returns the resolved workspace, the trigger
    fields that would change and a plan_id.

    Args:
        name: Trigger name, 1-200 characters. Required when creating a
            trigger.
        trigger_type: Trigger type, required when creating a trigger and
            rejected on an existing one, because GTM cannot change it later:
            pageview, domReady, windowLoaded, click, linkClick,
            formSubmission, customEvent, elementVisibility, scrollDepth,
            youTubeVideo, historyChange, timer, jsError or triggerGroup.
        custom_event_name: The dataLayer event name. Required for trigger type
            customEvent, rejected for every other type, and rejected on an
            existing trigger, where the event name sits in its custom event
            filter.
        filters: GTM conditions, each a dict with "type" (EQUALS, CONTAINS,
            MATCH_REGEX, ...) and "parameter" (arg0 = variable, arg1 =
            value), for example clicks on tel: links [{"type": "CONTAINS",
            "parameter": [{"type": "TEMPLATE", "key": "arg0", "value":
            "{{Click URL}}"}, {"type": "TEMPLATE", "key": "arg1", "value":
            "tel:"}]}]. On update the list replaces the existing one.
        custom_event_filters: Conditions on custom event parameters, same
            shape as filters. On update the list replaces the existing one.
        auto_event_filters: Conditions on automatic event parameters, same
            shape as filters. On update the list replaces the existing one.
        parameters: GTM parameter dicts with at least "type" and "key". On
            update they merge by key: passed keys replace, other keys stay.
        notes: Free-form note stored on the trigger. None keeps the current
            note.
        trigger_id: Existing trigger ID to update (see list_gtm_triggers).
            Empty creates a new trigger.
        workspace_id: Workspace to edit (see list_gtm_workspaces). Empty uses
            the Default Workspace, or the only workspace there is.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
    """
    from adloop.gtm.write import draft_gtm_trigger as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(gtm_account_id, gtm_container_id)
    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        trigger_id=trigger_id,
        workspace_id=workspace_id,
        name=name,
        trigger_type=trigger_type,
        custom_event_name=custom_event_name,
        filters=filters,
        custom_event_filters=custom_event_filters,
        auto_event_filters=auto_event_filters,
        parameters=parameters,
        notes=notes,
    )


@_tool(title="Draft deleting a Tag Manager tag or trigger", annotations=_DESTRUCTIVE, tags={"gtm"})
@_safe
def draft_delete_gtm_entity(
    entity_type: str,
    entity_id: str,
    workspace_id: str = "",
    gtm_account_id: str = "",
    gtm_container_id: str = "",
) -> dict:
    """Draft deleting a Tag Manager tag or trigger — returns a PREVIEW.

    The entity is removed from a workspace and stays on the site until the
    workspace is published. A trigger that any tag still references is
    refused up front, with the referencing tags named. Refused unless
    gtm.write_enabled is set. Returns the entity that would be deleted, the
    resolved workspace and a plan_id.

    Args:
        entity_type: "tag" or "trigger".
        entity_id: Numeric ID of the entity (see list_gtm_tags and
            list_gtm_triggers).
        workspace_id: Workspace to edit (see list_gtm_workspaces). Empty uses
            the Default Workspace, or the only workspace there is.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
    """
    from adloop.gtm.write import draft_delete_gtm_entity as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(gtm_account_id, gtm_container_id)
    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        entity_type=entity_type,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )


@_tool(title="Draft publishing a Tag Manager workspace", annotations=_DESTRUCTIVE, tags={"gtm"})
@_safe
def draft_publish_gtm_workspace(
    version_name: str = "",
    version_notes: str = "",
    workspace_id: str = "",
    gtm_account_id: str = "",
    gtm_container_id: str = "",
) -> dict:
    """Draft publishing a Tag Manager workspace LIVE — returns a PREVIEW.

    Every pending change in the workspace goes live, including changes other
    people made in the GTM UI; the preview lists them all. Apply refuses when
    the workspace changed after the preview, has merge conflicts, or reports
    compiler errors in a quick preview, and it refuses a Custom HTML tag
    being added or changed while gtm.allow_custom_html is off. Refused unless
    gtm.write_enabled is set. Returns the pending changes, the version that
    is live now (for a one-step rollback), the version name that would be
    created and a plan_id. That gate matches the tag type html; a Custom
    JavaScript variable or a custom template tag (cvt_…), which can inject a
    script, is not covered and needs review in the GTM UI.

    Args:
        version_name: Name of the container version created by the publish.
            Empty uses "AdLoop publish <UTC timestamp>".
        version_notes: Notes stored on the created version. Empty leaves them
            out.
        workspace_id: Workspace to publish (see list_gtm_workspaces). Empty
            uses the Default Workspace, or the only workspace there is.
        gtm_account_id: Numeric GTM account ID (see list_gtm_accounts). Empty
            uses gtm.account_id from the config.
        gtm_container_id: Numeric GTM container ID (see list_gtm_containers).
            Empty uses gtm.container_id from the config.
    """
    from adloop.gtm.write import draft_publish_gtm_workspace as _impl

    gtm_account_id, gtm_container_id = _gtm_defaults(gtm_account_id, gtm_container_id)
    return _impl(
        current_config(),
        account_id=gtm_account_id,
        container_id=gtm_container_id,
        workspace_id=workspace_id,
        version_name=version_name,
        version_notes=version_notes,
    )


@_tool(title="Custom Google Ads query", annotations=_READONLY, tags={"ads"})
@_safe
def run_gaql(
    query: str,
    customer_id: str = "",
    format: str = "table",
) -> dict:
    """Execute an arbitrary GAQL (Google Ads Query Language) query.

    For queries beyond the dedicated report tools. Returns the result rows
    in the requested format with the query echoed back; cost fields stay in
    raw micros (cost_micros / 1,000,000 = account currency). A failing query
    returns an error with a hint.

    Queries the Google Ads API. GAQL syntax and fields:
    https://developers.google.com/google-ads/api/docs/query/overview

    Args:
        query: The GAQL query, e.g. "SELECT campaign.name, metrics.clicks
            FROM campaign WHERE segments.date DURING LAST_7_DAYS".
        customer_id: Google Ads customer ID, digits with or without dashes
            (123-456-7890). Empty uses the configured default account.
        format: Output format: "table" (default, readable), "json"
            (structured), or "csv" (exportable).
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
    campaign exists. Only SEARCH campaigns can be created: Performance Max
    needs an asset group with all its assets in one request, and DISPLAY,
    SHOPPING and VIDEO need channel-specific settings and ad group types, so
    the draft refuses them.

    Returns a preview with a plan_id plus any warnings (e.g. a daily budget
    below 5x target CPA, MANUAL_CPC); nothing changes until confirm_and_apply
    applies the returned plan_id.

    Args:
        campaign_name: Name of the new campaign.
        daily_budget: Daily budget in the account's currency (not micros);
            must be greater than 0 and at most the config's max_daily_budget.
        bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
            MAXIMIZE_CONVERSION_VALUE | TARGET_SPEND | MANUAL_CPC. BROAD
            keywords require a Smart Bidding strategy (MAXIMIZE_CONVERSIONS,
            MAXIMIZE_CONVERSION_VALUE, TARGET_CPA, TARGET_ROAS).
        geo_target_ids: REQUIRED list of geo target constant IDs.
            Common: "2276" Germany, "2040" Austria, "2756" Switzerland, "2840" USA,
            "2826" UK, "2250" France. Full list: Google Ads API geo target constants.
        language_ids: REQUIRED list of language constant IDs.
            Common: "1001" German, "1000" English, "1002" French, "1003" Spanish,
            "1004" Italian, "1014" Portuguese. Full list: Google Ads API
            language constants.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
        target_cpa: Target cost per acquisition in the account's currency;
            required if bidding_strategy is TARGET_CPA, optional target for
            MAXIMIZE_CONVERSIONS. 0 means none.
        target_roas: Target return on ad spend as a ratio (e.g. 4.0 = 400%);
            required if bidding_strategy is TARGET_ROAS, optional target for
            MAXIMIZE_CONVERSION_VALUE. 0 means none.
        channel_type: SEARCH (default). DISPLAY, SHOPPING, VIDEO and
            PERFORMANCE_MAX are refused with the reason (see above).
        ad_group_name: Name of the initial ad group. Empty uses campaign_name.
        keywords: Optional list of {"text": "keyword", "match_type":
            "EXACT|PHRASE|BROAD"} added to the initial ad group.
        search_partners_enabled: Include ads on Search partners (SEARCH
            campaigns only).
        display_network_enabled: Enable Search campaign display expansion
            (SEARCH campaigns only). Defaults to off.
        display_expansion_enabled: Alias for display_network_enabled; both must
            match when both are given.
        max_cpc: In the account's currency: the manual CPC bid for the initial
            ad group when bidding_strategy is MANUAL_CPC, or the Maximize Clicks
            CPC cap when bidding_strategy is TARGET_SPEND. Not allowed with
            other strategies; 0 means none.
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

    Creates an ad group (ENABLED, type SEARCH_STANDARD) in the specified campaign,
    optionally with keywords in the same atomic operation. Only SEARCH campaigns
    are supported. Ads are not included, so the ad group cannot serve until one
    of its ads is created and enabled.

    Returns a preview with a plan_id plus warnings (BROAD keywords on a
    non-Smart-Bidding campaign, a duplicate ad group name, a CPC bid that Smart
    Bidding ignores); nothing changes until confirm_and_apply applies it.

    Args:
        campaign_id: The campaign to add the ad group to (numeric ID as returned
            by get_campaign_performance).
        ad_group_name: Name for the new ad group.
        keywords: Optional list of {"text": "keyword", "match_type":
            "EXACT|PHRASE|BROAD"}.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
        cpc_bid_micros: Optional ad group CPC bid in micros (1,000,000 = one
            unit of the account's currency); only used by MANUAL_CPC campaigns.
            0 means no ad-group bid; must not be negative.
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

    Only the parameters passed are changed; omitted ones stay as they are, and at
    least one change is required. Replacing geo targets keeps the campaign's
    negative geo exclusions (the preview lists them).

    Returns a preview with a plan_id plus any warnings; nothing changes until
    confirm_and_apply applies it.

    Args:
        campaign_id: The numeric ID of the campaign to update (required).
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
        bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
            MAXIMIZE_CONVERSION_VALUE | TARGET_SPEND | MANUAL_CPC. Empty leaves
            the strategy unchanged.
        target_cpa: Target CPA in the account's currency; required if
            bidding_strategy is TARGET_CPA. 0 leaves it unchanged.
        target_roas: Target ROAS as a ratio (e.g. 4.0 = 400%); required if
            bidding_strategy is TARGET_ROAS. 0 leaves it unchanged.
        daily_budget: New daily budget in the account's currency (not micros),
            at most the config's max_daily_budget. 0 leaves it unchanged.
        geo_target_ids: REPLACES all positive geo targets; must not be empty
            when given. Common IDs: "2276" Germany, "2040" Austria, "2756"
            Switzerland, "2840" USA, "2826" UK.
        language_ids: REPLACES all language targets; must not be empty when
            given. Common IDs: "1001" German, "1000" English, "1002" French,
            "1003" Spanish, "1004" Italian.
        search_partners_enabled: Include ads on Search partners. Omitted leaves
            the setting unchanged.
        display_network_enabled: Enable Search campaign display expansion.
            Omitted leaves the setting unchanged.
        display_expansion_enabled: Alias for display_network_enabled; both must
            match when both are given.
        max_cpc: Maximize Clicks CPC cap in the account's currency when
            bidding_strategy is TARGET_SPEND, or when the existing campaign
            already uses TARGET_SPEND. Increases are limited by the config's
            max_bid_increase_pct. 0 leaves it unchanged.
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

    The preview shows exactly what will be created; confirm_and_apply executes
    the returned plan_id. The ad is created PAUSED. final_url is checked for
    reachability first and an unreachable URL is refused. Google caps: at most
    2 headlines per pin slot, at most 1 description per pin slot. Mixed
    plain-string and dict entries are allowed within a single call (e.g. brand
    pinned to HEADLINE_1, the rest unpinned). Warnings note fewer than 8
    headlines or 3 descriptions.

    Args:
        ad_group_id: Numeric ID of the ad group that gets the ad.
        headlines: 3-15 headlines, max 30 chars each. Each entry is a plain
            string (unpinned) or {"text": "...", "pinned_field": "HEADLINE_1"};
            valid pins are HEADLINE_1, HEADLINE_2, HEADLINE_3.
        descriptions: 2-4 descriptions, max 90 chars each. Each entry is a plain
            string (unpinned) or {"text": "...", "pinned_field": "DESCRIPTION_1"};
            valid pins are DESCRIPTION_1, DESCRIPTION_2.
        final_url: Landing page URL the ad links to; must be reachable.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
        path1: Optional first display-URL path segment (Google allows up to 15
            chars).
        path2: Optional second display-URL path segment (Google allows up to 15
            chars).
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
    At least one mutation must be requested.

    IMPORTANT: replacing headlines or descriptions is NOT a free in-place
    edit. Even though the ad ID is preserved, swapping the creative text
    RESETS the ad's asset-combination learning and performance history and
    sends the ad BACK THROUGH Google policy review — Google treats the
    creative as new for optimization. URL-only and path-only edits do not
    incur this. When headlines/descriptions change, the returned preview
    includes a ``warnings`` entry stating this trade-off before anything
    is applied.

    Returns a preview with a plan_id; nothing changes until confirm_and_apply
    applies it.

    Args:
        ad_id: Numeric ID of the existing responsive search ad.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
        headlines: LIST-REPLACE: None or [] means no change; a non-empty list
            fully swaps in for the existing headlines. Google's RSA constraints
            apply: 3-15 headlines, max 30 chars each, at most 2 per pin slot.
            Each entry is a plain string (unpinned) or {"text": "...",
            "pinned_field": "HEADLINE_1"} (HEADLINE_1-3).
        descriptions: LIST-REPLACE: None or [] means no change; a non-empty list
            fully swaps in. 2-4 descriptions, max 90 chars each, at most 1 per
            pin slot (DESCRIPTION_1, DESCRIPTION_2). Same entry format as
            headlines.
        final_url: Empty means no change; non-empty replaces the final URL and
            is checked for reachability.
        path1: Empty means no change; non-empty sets the first display path
            (max 15 chars).
        path2: Empty means no change; non-empty sets the second display path
            (max 15 chars).
        clear_path1: True sets path1 to an empty string (overrides path1).
        clear_path2: True sets path2 to an empty string (overrides path2).
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

    Returns a preview with a plan_id, plus a warning when BROAD keywords go into
    a campaign without Smart Bidding; nothing changes until confirm_and_apply
    applies the returned plan_id.

    Args:
        ad_group_id: Numeric ID of the ad group that gets the keywords.
        keywords: List of {"text": "keyword phrase", "match_type":
            "EXACT|PHRASE|BROAD"}; at least one is required.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
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
    They are added at campaign level. Returns a preview with a plan_id; nothing
    changes until confirm_and_apply applies it.

    Args:
        campaign_id: Numeric ID of the campaign that gets the negatives.
        keywords: Negative keyword texts; at least one is required. All share
            the same match_type.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
        match_type: "EXACT" (default), "PHRASE", or "BROAD".
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
    targets such as State of Sao Paulo. Returns a preview with a plan_id;
    nothing changes until confirm_and_apply applies it.

    Args:
        campaign_id: Numeric ID of the campaign to add the location exclusions to.
        geo_target_ids: Numeric Google geo target constant IDs to exclude (e.g.
            "1001773"). Duplicates are collapsed; non-numeric IDs are rejected.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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

    Creates a reusable negative keyword list (shared set) that can later be
    applied to multiple campaigns, whereas add_negative_keywords adds negatives
    directly to one campaign. Returns a preview with a plan_id; nothing changes
    until confirm_and_apply applies it.

    Args:
        campaign_id: Numeric ID of the campaign the new list is attached to.
        list_name: Name of the new shared negative keyword list.
        keywords: Negative keyword texts to put in the list (at least one).
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
        match_type: Match type applied to every keyword: "EXACT" (default),
            "PHRASE", or "BROAD" (case-insensitive).
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

    Adds terms to a list that already exists (propose_negative_keyword_list
    creates a new list instead); get_negative_keyword_list_keywords shows the
    terms already in a list. Returns a preview with a plan_id; nothing changes
    until confirm_and_apply applies it.

    Args:
        shared_set_id: Numeric ID of the list (shared_set.id from
            get_negative_keyword_lists).
        keywords: Keyword strings to append. Blank entries are dropped and
            duplicates in the input list are collapsed (case-insensitive).
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
        match_type: Match type applied to every keyword: "EXACT" (default),
            "PHRASE", or "BROAD" (case-insensitive).
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
    shared negative keyword list to newly-built campaigns;
    get_negative_keyword_list_campaigns lists existing attachments. Returns a
    preview with a plan_id; nothing changes until confirm_and_apply applies it.

    Args:
        shared_set_id: Numeric ID of the shared set (from
            get_negative_keyword_lists).
        campaign_ids: Numeric campaign IDs to attach the set to (at least one).
            Blank entries are dropped and duplicates collapsed.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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
    per-campaign attachment is removed. get_negative_keyword_list_campaigns
    lists the existing attachments. Returns a preview with a plan_id; nothing
    changes until confirm_and_apply applies it.

    Args:
        shared_set_id: Numeric ID of the shared set (from
            get_negative_keyword_lists).
        campaign_ids: Numeric campaign IDs to detach the set from (at least one).
            Blank entries are dropped and duplicates collapsed.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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
    ad groups and shows current values per knob. Returns a preview with a
    plan_id; nothing is written until confirm_and_apply runs.

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
        text_asset_automation: OPTED_IN, OPTED_OUT or UNCHANGED (default) for
            TEXT_ASSET_AUTOMATION.
        final_url_expansion: OPTED_IN, OPTED_OUT or UNCHANGED (default) for
            FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION (the v25 field name for
            final URL expansion).
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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

    Exactly one combination, nothing else: enable_ai_max = true,
    disable_search_term_matching = true (all non-removed ad groups),
    TEXT_ASSET_AUTOMATION = OPTED_OUT and
    FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION = OPTED_OUT.

    No bidding, keyword, match type, ad, URL or budget change, and no brand list
    is attached — attaching stays a separate step with propose_brand_list /
    attach_brand_list_to_campaigns once this state is verified in the account.

    Returns a preview with a plan_id (purpose brand_exclusions); nothing changes
    until confirm_and_apply applies it.

    Args:
        campaign_id: Numeric ID of the Search campaign to prepare.
        include_paused_ad_groups: Default true — paused ad groups are set too,
            so re-enabling one later cannot silently restore search term
            matching.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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
    targeting to it (negative=False — uncommon). Takes exactly one of
    ad_group_id or campaign_id, and at least one of the four demographic lists
    must contain a value.

    Returns a preview with a plan_id, plus warnings when the change narrows
    targeting, excludes 'undetermined' users, or excludes nearly a whole
    dimension; nothing changes until confirm_and_apply applies it.

    Args:
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
        ad_group_id: Numeric ad group ID to add the criteria to. Mutually
            exclusive with campaign_id.
        campaign_id: Numeric campaign ID to add the criteria to. Mutually
            exclusive with ad_group_id; campaign-level criteria can only be
            exclusions (negative=True).
        age_ranges: Age buckets '18-24', '25-34', '35-44', '45-54', '55-64',
            '65+', 'undetermined' (or canonical names such as AGE_RANGE_18_24).
            Google's buckets are FIXED — 'Exclude 23-35' has no exact mapping
            and needs a choice of buckets.
        genders: 'female', 'male', 'undetermined'.
        parental_statuses: 'parent', 'not_a_parent', 'undetermined'.
        income_ranges: PERCENTILES (not currency): 'top-10', '11-20', '21-30',
            '31-40', '41-50', 'lower-50', 'undetermined'. Available in select
            countries only (US, AU, JP, etc.).
        negative: True (default) excludes the listed segments; False adds
            positive criteria that narrow an ad group's targeting to them.
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
    """Draft an ad group update for name and/or manual CPC bid — returns a PREVIEW.

    A bid change applies only when the campaign uses MANUAL_CPC: automated
    bidding strategies ignore ad-group CPC bids, so such a change is refused
    (for Maximize Clicks the campaign cpc_bid_ceiling, set through
    update_campaign, is the active constraint). Bid increases above the
    configured safety.max_bid_increase_pct are refused. Returns a preview with
    a plan_id; nothing changes until confirm_and_apply applies it.

    Args:
        ad_group_id: Numeric ID of the ad group to update.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
        ad_group_name: New ad group name. Empty leaves the name unchanged.
        max_cpc: New maximum CPC bid in the account's currency (an amount, not
            micros). 0 leaves the bid unchanged; negative values are rejected.
            At least one of ad_group_name or max_cpc is required.
    """
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
    campaign_id: str,
    callouts: _StrList,
    customer_id: str = "",
) -> dict:
    """Draft campaign callout assets — returns a PREVIEW.

    Creates callout assets and links them to the campaign. Returns a preview
    with a plan_id; nothing changes until confirm_and_apply applies it.

    Args:
        campaign_id: Numeric ID of the campaign to attach the callouts to.
        callouts: Callout texts (at least one), each non-empty and at most 25
            characters after trimming.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
    """
    from adloop.ads.write import draft_callouts as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        callouts=callouts,
    )


@_tool(title="Draft structured snippets", annotations=_WRITE, tags={"ads"})
@_safe
def draft_structured_snippets(
    campaign_id: str,
    snippets: _DictList,
    customer_id: str = "",
) -> dict:
    """Draft campaign structured snippet assets — returns a PREVIEW.

    Creates structured snippet assets and links them to the campaign. Returns a
    preview with a plan_id; nothing changes until confirm_and_apply applies it.

    Args:
        campaign_id: Numeric ID of the campaign to attach the snippets to.
        snippets: At least one dict with "header" and "values". header is one of
            Amenities, Brands, Courses, Degree programs, Destinations, Featured
            Hotels, Insurance coverage, Models, Neighborhoods, Services, Shows,
            Styles, Types (exact spelling). values is a list of 3-10 non-empty
            strings, each at most 25 characters.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
    """
    from adloop.ads.write import draft_structured_snippets as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
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
    and refuses if the image changed since the preview. Returns a preview with
    a plan_id and a per-image summary; nothing changes until confirm_and_apply
    applies it.

    Args:
        campaign_id: Numeric ID of the campaign to attach the images to.
        image_paths: Local PNG/JPEG/GIF file paths on the machine running
            AdLoop. Not available on a hosted server, where image_urls
            applies instead.
        image_urls: Public http(s) URLs of PNG/JPEG/GIF images. Links to
            private, local or internal addresses are refused.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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

    Pausing is reversible with enable_entity. Returns a preview with a plan_id
    (target status PAUSED); nothing changes until confirm_and_apply applies it.

    Args:
        entity_type: "campaign", "ad_group", "ad", or "keyword".
        entity_id: ID in the format for its type. campaign: campaign ID (e.g.
            "12345678"); ad_group: ad group ID (e.g. "12345678"); ad:
            "adGroupId~adId" (e.g. "12345678~987654"; a bare ad ID is resolved
            to its ad group at apply time); keyword: "adGroupId~criterionId"
            (e.g. "12345678~987654").
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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

    Returns a preview with a plan_id (target status ENABLED); nothing changes
    until confirm_and_apply applies it.

    Args:
        entity_type: "campaign", "ad_group", "ad", or "keyword".
        entity_id: ID in the format for its type. campaign: campaign ID (e.g.
            "12345678"); ad_group: ad group ID (e.g. "12345678"); ad:
            "adGroupId~adId" (e.g. "12345678~987654"; a bare ad ID is resolved
            to its ad group at apply time); keyword: "adGroupId~criterionId"
            (e.g. "12345678~987654").
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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

    Removed entities cannot be re-enabled; pause_entity is the reversible way
    to temporarily disable something. Returns a preview with a plan_id and
    requires_double_confirm=true; nothing changes until confirm_and_apply
    applies it.

    Args:
        entity_type: "campaign", "ad_group", "ad", "keyword", "negative_keyword",
            "shared_criterion", "campaign_asset", "asset", "customer_asset",
            "ad_group_criterion", or "campaign_criterion".
        entity_id: The resource ID. campaign / ad_group / asset: simple numeric
            ID. ad: "adGroupId~adId" (a bare ad ID is resolved to its ad group).
            keyword and ad_group_criterion: "adGroupId~criterionId".
            negative_keyword and campaign_criterion: "campaignId~criterionId"
            (the resource_id field from get_negative_keywords).
            shared_criterion: "sharedSetId~criterionId" (the resource_id field
            from get_negative_keyword_list_keywords). campaign_asset:
            "campaignId~assetId~fieldType". customer_asset: "assetId~fieldType".
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
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
    and directing users to specific pages. Every final_url is fetched and an
    unreachable URL (HTTP 4xx/5xx) blocks the draft. Google recommends at least
    4 sitelinks per campaign; fewer than 2 may not show. Returns a preview with
    a plan_id and any warnings; nothing changes until confirm_and_apply applies
    it.

    Args:
        campaign_id: Numeric ID of the campaign to attach sitelinks to.
        sitelinks: List of dicts (at least one), each with link_text (str,
            required, max 25 chars) — the clickable text shown; final_url (str,
            required) — destination URL for this sitelink; description1 (str,
            optional, max 35 chars) — first description line; description2
            (str, optional, max 35 chars) — second description line.
        customer_id: Google Ads customer ID (digits, dashes optional). Defaults to
            the configured account.
    """
    from adloop.ads.write import draft_sitelinks as _impl

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        campaign_id=campaign_id,
        sitelinks=sitelinks,
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

    A DRY_RUN_FAILED result means the change does not go through as previewed.
    The real apply fails the same way, unless Google rejected only part of the
    request: then it carries out the rest and reports those operations again.

    Returns a status with the plan_id and operation: DRY_RUN_SUCCESS,
    DRY_RUN_FAILED, DRY_RUN_REQUIRED, APPLIED, PARTIAL_UPLOAD or
    APPLY_IN_PROGRESS. APPLIED results carry the created or changed resource
    names under 'result'. PARTIAL_UPLOAD belongs to the upload tools: the
    batches it lists are already in the account, so the plan is retired.
    'sent_total', 'accepted_total', 'rejected_total' and 'resume_from_line'
    say how far the upload got and at which CSV line a new draft continues;
    when a batch's fate is unknown, 'unknown_status', 'uncertain_lines' and
    'row_errors' name the rows whose outcome has to be checked first.
    APPLY_IN_PROGRESS means an earlier call claimed this plan first, so this
    call executed nothing.

    Args:
        plan_id: The plan_id returned by a prior draft_*, update_*, add_*,
            pause/enable/remove or other preview tool call. An unknown or
            expired plan_id returns an error.
        dry_run: True (default) validates without changing anything; false
            applies the change for real.
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

    Returns the Reddit username, the businesses, and per ad account the
    ad_account_id values (plus currency, time zone and approval state) that
    every other Reddit tool takes. Requires the Reddit Ads connection
    (adloop init → Reddit step, or Settings → Reddit Ads in Cloud).
    """
    from adloop.reddit.read import list_reddit_accounts as _impl

    return _impl(current_config())


@_tool(
    title="Reddit billing + posting profiles", annotations=_READONLY, tags={"reddit"}
)
@_safe
def list_reddit_funding_instruments(ad_account_id: str = "") -> dict:
    """Funding instruments (billing) and posting profiles of a Reddit ad account.

    Returns funding_instruments (funding_instrument_id, currency, is_servable,
    reasons_not_servable, credit limit and billable amount in account
    currency) and profiles (profile_id, username). draft_reddit_campaign needs
    a servable funding_instrument_id; draft_reddit_ad needs the profile_id
    that authors the post. Both come from here.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
    """
    from adloop.reddit.read import list_reddit_funding_instruments as _impl

    return _impl(current_config(), ad_account_id=_reddit_account(ad_account_id))


@_tool(title="Reddit campaigns", annotations=_READONLY, tags={"reddit"})
@_safe
def get_reddit_campaigns(ad_account_id: str = "", include_archived: bool = False) -> dict:
    """List Reddit campaigns with status, objective, budget mode and bids.

    Returns the account currency, the campaigns, and insights for campaigns
    blocked by account state. Each campaign carries configured_status (what
    was set) and effective_status (what Reddit computes: PENDING_APPROVAL,
    REJECTED, PENDING_BILLING_INFO, ...). Money is in account currency.
    Budgets live on ad groups unless is_campaign_budget_optimization is true.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        include_archived: Include ARCHIVED and DELETED campaigns, which are
            left out by default.
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

    Returns the account currency, the ad groups, and insights. Reddit
    requires conversion_pixel_id on every ad group; insights flag ad groups
    without one and name ad groups that run on a weekly schedule.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        campaign_id: Reddit campaign id (from get_reddit_campaigns) to limit
            the list to; empty lists every ad group of the account.
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

    Returns the ads and insights: insights flag REJECTED ads (policy review)
    and ads still PENDING_APPROVAL.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        ad_group_id: Reddit ad group id (from get_reddit_ad_groups) to limit
            the list to; empty means no ad group filter.
        campaign_id: Reddit campaign id (from get_reddit_campaigns) to limit
            the list to; empty means no campaign filter.
        include_copy: When true, adds each ad's post: type (TEXT, IMAGE,
            VIDEO, CAROUSEL), headline, body, destination and media, one
            request per distinct post (at most 50 posts per call), so creative
            can be reviewed without opening Reddit.
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

    Returns rows per entity for the chosen level with names joined, plus
    totals and insights[] (zero-conversion spenders, rejected ads, empty
    windows). 'conversions' is the account's key conversion event. Money is
    in account currency. Data lags up to 6 hours.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        level: Row granularity: "account", "campaign" (default), "ad_group"
            or "ad".
        date_range_start: First day, YYYY-MM-DD, in the account's time zone.
            Empty means 29 days before date_range_end (a 30-day window).
        date_range_end: Last day (inclusive), YYYY-MM-DD, in the account's
            time zone. Empty means today.
        breakdown: Optional extra dimension: "date", "hour", "country",
            "region", "community", "keyword", "interest", "placement",
            "gender" or "os_type". Empty means none.
        compact: When true, returns totals + top-10 rows by spend + offender
            lists instead of every row.
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

    Returns the requested fields per breakdown row, the date range and the
    account currency. Microcurrency and cent fields are converted to
    currency amounts.

    Queries the Reddit Ads API: https://ads-api.reddit.com/docs/v3/

    Args:
        fields: Reddit report field names (required), e.g. ["SPEND",
            "CLICKS", "REACH", "VIDEO_STARTED",
            "CONVERSION_PURCHASE_TOTAL_VALUE", "KEY_CONVERSION_TOTAL_COUNT"].
        breakdowns: Up to 3 of DATE, HOUR, CAMPAIGN_ID, AD_GROUP_ID, AD_ID,
            COUNTRY, REGION, COMMUNITY, KEYWORD, INTEREST, PLACEMENT, GENDER,
            OS_TYPE (4 when COUNTRY and REGION are both included; HOUR and
            DATE cannot be combined).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        date_range_start: First day, YYYY-MM-DD (account-local day). Empty
            means 29 days before date_range_end (a 30-day window).
        date_range_end: Last day (inclusive), YYYY-MM-DD (account-local day).
            Empty means today.
        filter: Reddit filter expression (e.g. "campaign_id==abc123"). Empty
            means no filter.
        time_zone_id: IANA time zone for the day boundaries and DATE/HOUR
            rows, e.g. "Europe/Berlin". Empty uses the ad account's time zone.
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

    Returns each pixel's pixel_id, name, last_fired_at per standard event,
    custom events and a never_fired flag, plus insights. Relevant before
    conversion-optimized campaigns: insights flag pixels that never fired and
    ad groups optimizing for an event their pixel has never sent. Every new
    ad group needs a conversion_pixel_id from here.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
    """
    from adloop.reddit.read import get_reddit_pixels as _impl

    return _impl(current_config(), ad_account_id=_reddit_account(ad_account_id))


@_tool(title="Search Reddit targeting", annotations=_READONLY, tags={"reddit"})
@_safe
def search_reddit_targeting(
    kind: str, query: str = "", country: str = "", website_url: str = "", limit: int = 25
) -> dict:
    """Look up targeting options for draft_reddit_ad_group.

    Returns ids/names to pass into the targeting lists, with the kind, the
    query and the total number of matches. Communities target by name,
    interests by id, geolocations by id (e.g. "DE" or "DE:2874225"),
    languages by code (e.g. "DE"), carriers by id (e.g. "O2_DEUTSCHLAND").
    Devices come back as make/model pairs for the label_map of a device
    target. Keyword suggestions carry Reddit-wide monthly views, not search
    volume.

    Args:
        kind: What to look up: "communities" (subreddits; query required),
            "interests" (query filters by name), "geolocations" (country ISO
            code and/or city query), "languages" (upper-case ISO 639-1
            codes), "keywords" (comma-separated seed terms → suggestions with
            Reddit-wide monthly views), "community_suggestions" (Reddit's
            related-community picks for seed communities in query and/or a
            website_url), "devices" (device makes and models; query filters
            by make or model) or "carriers" (mobile carrier ids; query
            filters by name, country by country code).
        query: Search text for the chosen kind: a community search term, an
            interest or language name filter, a city name, comma-separated
            seed keywords, comma-separated seed communities for
            community_suggestions (e.g. "PPC,googleads"), a device make or
            model filter, or a carrier name filter.
        country: Country ISO code (e.g. "DE") for kinds "geolocations" and
            "carriers"; ignored for other kinds.
        website_url: Website for kind "community_suggestions"; ignored for
            other kinds.
        limit: Maximum number of results, clamped to 1-100.
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

    Returns the changes, most recent first, with changed_at, by, entity type,
    id and name, field, and before/after values. The first thing to check
    when performance moves. Child entities are included when filtering by
    entity. Changes made through AdLoop show under the connected Reddit user,
    like changes made in Ads Manager. Money fields are in account currency.

    Args:
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        date_range_start: First day, YYYY-MM-DD (account-local day). Empty
            means 29 days before date_range_end (default window is the last
            30 days).
        date_range_end: Last day (inclusive), YYYY-MM-DD (account-local day).
            Empty means today.
        entity_type: Optional filter to one entity type: "campaign",
            "ad_group" or "ad". Requires entity_ids.
        entity_ids: Ids of the entities of entity_type to filter to; required
            when entity_type is set.
        limit: Maximum number of changes returned, clamped to 1-500.
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
    devices: _DictListOpt = None,
    carriers: _StrListOpt = None,
    ad_account_id: str = "",
) -> dict:
    """Audience size, delivery estimate and Reddit's suggested bid for a planned ad group — read-only.

    Reddit's counterpart of estimate_budget: takes the same targeting and
    budget as draft_reddit_ad_group, ahead of drafting. Returns the reachable
    and targetable audience (fixed 30-day basis), estimated
    impressions/clicks/reach for the schedule, the minimum/suggested bid
    range for bid_type in account currency, and insights (audience under
    10,000 people, bid_value below the suggested minimum). Nothing is
    created. At least one of geolocations, communities, interests or keywords
    is required.

    Args:
        daily_budget: Daily budget in account currency (positive). One of
            daily_budget or lifetime_budget is required; daily_budget wins
            when both are given.
        lifetime_budget: Lifetime budget in account currency (positive),
            used when daily_budget is not given.
        objective: Campaign objective the estimate assumes, e.g. CLICKS
            (default), CONVERSIONS, IMPRESSIONS, LEAD_GENERATION,
            APP_INSTALLS, CATALOG_SALES or VIDEO_VIEWABLE_IMPRESSIONS.
        bid_type: CPC (default), CPM, CPV, CPV6 or CPV15; the bid suggestion
            is for this bid type.
        bid_strategy: BIDLESS, MANUAL_BIDDING, MAXIMIZE_VOLUME or TARGET_CPX.
            Empty means BIDLESS.
        bid_value: Planned bid in account currency; compared with Reddit's
            suggested minimum.
        optimization_goal: Pixel event to optimize for (PURCHASE, SIGN_UP,
            LEAD, PAGE_VISIT, ...). Empty means none.
        start_time: Schedule start, ISO 8601 (e.g. 2026-10-01T00:00:00Z).
            Empty means tomorrow 00:00 UTC.
        end_time: Schedule end, ISO 8601. Empty means 30 days after the
            default start.
        geolocations: Geolocation ids to target (e.g. "DE" or "DE:2874225",
            from search_reddit_targeting kind "geolocations").
        excluded_geolocations: Geolocation ids to exclude.
        communities: Community (subreddit) names to target.
        excluded_communities: Community names to exclude.
        interests: Interest ids to target (from search_reddit_targeting kind
            "interests").
        keywords: Keywords to target.
        languages: Language codes (ISO 639-1, upper-cased automatically,
            e.g. "DE"). Empty means all languages.
        gender: FEMALE or MALE; empty means all genders.
        platforms: Platforms to target, e.g. ALL, DESKTOP, MOBILE_NATIVE,
            MOBILE_WEB. Empty means no platform restriction.
        devices: Device targets, objects like {"type": "MOBILE", "os":
            "IOS", "min_version": "16"} (type DESKTOP or MOBILE; optional os
            ANDROID or IOS, major OS versions, and label_map {make: [models]}
            with makes/models from search_reddit_targeting kind "devices").
            Empty means every device.
        carriers: Mobile carrier ids (from search_reddit_targeting kind
            "carriers", e.g. "O2_DEUTSCHLAND"). Empty means every carrier.
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
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
        "devices": devices,
        "carriers": carriers,
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

    Returns a preview with a plan_id, the current and target status (PAUSED)
    and warnings (e.g. already paused); nothing changes until
    confirm_and_apply applies the plan_id.

    Args:
        entity_type: "campaign", "ad_group" or "ad".
        entity_id: Reddit id of the entity, from the read tools
            (get_reddit_campaigns, get_reddit_ad_groups, get_reddit_ads).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
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
    Returns a preview with a plan_id, the current and target status and
    warnings (e.g. an effective_status of REJECTED or PENDING_BILLING_INFO
    that keeps it from serving); nothing changes until confirm_and_apply
    applies the plan_id.

    Args:
        entity_type: "campaign", "ad_group" or "ad".
        entity_id: Reddit id of the entity, from the read tools
            (get_reddit_campaigns, get_reddit_ad_groups, get_reddit_ads).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
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
    not need to be gone. Returns a preview with a plan_id that requires
    double confirmation; confirm_and_apply executes it.

    Args:
        entity_type: "campaign", "ad_group" or "ad".
        entity_id: Reddit id of the entity, from the read tools
            (get_reddit_campaigns, get_reddit_ad_groups, get_reddit_ads).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
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

    daily_budget / lifetime_budget / bid_* / schedule apply only when the
    campaign uses campaign budget optimization; otherwise the budget lives on
    its ad groups (changed via update_reddit_ad_group). Budgets are in
    account currency and checked against max_daily_budget; bid_value against
    max_bid_increase_pct. Returns a preview with a plan_id showing old → new
    per field; nothing changes until confirm_and_apply applies it.

    Args:
        campaign_id: Reddit campaign id (from get_reddit_campaigns).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        name: New campaign name; empty keeps the current one.
        daily_budget: New daily budget in account currency (positive; CBO
            only). Mutually exclusive with lifetime_budget; the budget type
            (daily vs lifetime) cannot change after publishing.
        lifetime_budget: New lifetime budget in account currency (positive;
            CBO only). Needs an end_time (passed or already set); compared to
            max_daily_budget by its per-day share.
        spend_cap: Campaign spend cap in account currency; 0 removes it.
        bid_strategy: BIDLESS, MAXIMIZE_VOLUME or TARGET_CPX (CBO only).
        bid_type: CPC, CPM, CPV, CPV6 or CPV15 (CBO only).
        bid_value: New bid in account currency (CBO only).
        start_time: New start, ISO 8601 (e.g. 2026-10-01T00:00:00Z); a bare
            YYYY-MM-DD means 00:00 UTC that day.
        end_time: New end, same formats as start_time.
        schedule: Weekly delivery windows ("time of day" in Ads Manager; CBO
            only), a list of blocks like {"days": "MON-FRI", "start_hour": 13,
            "end_hour": 23} (day names, hours 0-23, end_hour inclusive) or the
            native {"start_day": "FRI", "start_hour": 22, "end_day": "SAT",
            "end_hour": 3}. [] clears it (deliver at any time). Hours apply in
            each viewer's local time, not the account time zone. A campaign
            schedule replaces every ad group's.
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
    excluded_interests: _StrListOpt = None,
    devices: _DictListOpt = None,
    carriers: _StrListOpt = None,
    view_modes: _StrListOpt = None,
) -> dict:
    """Draft changes to a Reddit ad group — budget, bid, run dates, weekly schedule, targeting.

    Budget (daily_budget or lifetime_budget + end_time) is checked against
    max_daily_budget; bid_value against max_bid_increase_pct. Targeting lists
    REPLACE the current value of each key passed (each takes the full list);
    omitted keys are preserved. Ids/names come from search_reddit_targeting.
    Budget and schedule of an ad group in a campaign-budget-optimization
    campaign are set on the campaign (update_reddit_campaign). Returns a
    preview with a plan_id showing old → new per field; nothing changes until
    confirm_and_apply applies it.

    Args:
        ad_group_id: Reddit ad group id (from get_reddit_ad_groups).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        name: New ad group name; empty keeps the current one.
        daily_budget: New daily budget in account currency (positive).
            Mutually exclusive with lifetime_budget; the budget type (daily vs
            lifetime) cannot change after publishing.
        lifetime_budget: New lifetime budget in account currency (positive).
            Needs an end_time (passed or already set); compared to
            max_daily_budget by its per-day share.
        bid_value: New bid in account currency.
        bid_strategy: BIDLESS, MANUAL_BIDDING, MAXIMIZE_VOLUME or TARGET_CPX.
        bid_type: CPC, CPM, CPV, CPV6 or CPV15.
        start_time: New start, ISO 8601 (e.g. 2026-10-01T00:00:00Z); a bare
            YYYY-MM-DD means 00:00 UTC that day.
        end_time: New end, same formats as start_time.
        geolocations: Full list of geolocation ids to target (e.g. "DE" or
            "DE:2874225").
        excluded_geolocations: Full list of geolocation ids to exclude.
        communities: Full list of community (subreddit) names to target.
        excluded_communities: Full list of community names to exclude.
        interests: Full list of interest ids to target.
        keywords: Full list of keywords to target.
        excluded_keywords: Full list of keywords to exclude.
        languages: Full list of language codes (ISO 639-1, upper-cased
            automatically, e.g. "DE").
        gender: FEMALE or MALE; empty leaves gender targeting unchanged.
        platforms: Full list of platforms: ALL, DESKTOP, DESKTOP_LEGACY,
            MOBILE_NATIVE, MOBILE_WEB, MOBILE_WEB_3X or SHREDTOP.
        expand_targeting: Whether Reddit may expand delivery beyond the
            targeting; omitted leaves it unchanged.
        schedule: Weekly delivery windows ("time of day" in Ads Manager), a
            list of blocks like {"days": "MON-FRI", "start_hour": 13,
            "end_hour": 23} (day names, hours 0-23, end_hour inclusive) or the
            native {"start_day": "FRI", "start_hour": 22, "end_day": "SAT",
            "end_hour": 3}. [] clears it (deliver at any time). Hours apply in
            each viewer's local time, not the account time zone.
        locations: Placements, FEED and/or COMMENTS_PAGE (conversation
            pages); cannot be an empty list.
        excluded_interests: Full list of interest ids to exclude (Reddit
            marks this field deprecated). [] clears it.
        devices: Full list of device targets, objects like {"type":
            "MOBILE", "os": "IOS", "min_version": "16"} (type DESKTOP or
            MOBILE; optional os ANDROID or IOS, major OS versions with iOS at
            least 14, and label_map {make: [models]} with makes/models from
            search_reddit_targeting kind "devices"). [] clears it (every
            device).
        carriers: Full list of mobile carrier ids (from
            search_reddit_targeting kind "carriers", e.g. "O2_DEUTSCHLAND"),
            checked against Reddit's carrier list. [] clears it.
        view_modes: Full list of feed layouts: ALL, CARD, CLASSIC, COMPACT
            or IMMERSIVE. [] clears it.
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
        excluded_interests=excluded_interests,
        devices=devices,
        carriers=carriers,
        view_modes=view_modes,
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

    The headline and body of a live Reddit post cannot be edited; new copy
    means a new ad via draft_reddit_ad, with the old ad paused. Returns a
    preview with a plan_id showing old → new; nothing changes until
    confirm_and_apply executes it.

    Args:
        ad_id: Reddit ad id (from get_reddit_ads).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        name: New ad name; empty keeps the current one.
        click_url: New landing URL (http:// or https://), verified to be
            reachable. TEXT (free-form) ads open the post itself and take no
            click_url.
        allow_comments: true or false turns comments on the ad's post on or
            off; omitted leaves them unchanged.
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

    By default the budget lives on the ad groups; campaign_budget_optimization
    holds it on the campaign. Budgets are checked against max_daily_budget.
    Ad groups are added with draft_reddit_ad_group. Returns a preview with a
    plan_id, the payload and warnings; nothing changes until
    confirm_and_apply applies it.

    Args:
        campaign_name: Name of the new campaign.
        objective: CLICKS, CONVERSIONS, IMPRESSIONS, LEAD_GENERATION,
            APP_INSTALLS, CATALOG_SALES or VIDEO_VIEWABLE_IMPRESSIONS. Other
            values only produce a warning, since Reddit is rolling out new
            objective enums.
        funding_instrument_id: Servable funding instrument id from
            list_reddit_funding_instruments.
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        campaign_budget_optimization: true holds the budget on the campaign
            (then daily_budget or lifetime_budget, bid_strategy, bid_type and
            conversion_pixel_id are required). false (default) leaves budget
            and bids to the ad groups, and none of the budget or bid
            parameters may be passed.
        daily_budget: Daily budget in account currency (positive; CBO only).
            Mutually exclusive with lifetime_budget.
        lifetime_budget: Lifetime budget in account currency (positive; CBO
            only); requires end_time and is compared to max_daily_budget by
            its per-day share.
        bid_strategy: BIDLESS, MAXIMIZE_VOLUME or TARGET_CPX (CBO only).
        bid_type: CPC, CPM, CPV, CPV6 or CPV15 (CBO only).
        bid_value: Bid in account currency (CBO only).
        optimization_goal: Pixel event to optimize for (PURCHASE, SIGN_UP,
            LEAD, PAGE_VISIT, ...); used with CBO only.
        conversion_pixel_id: Pixel id from get_reddit_pixels; required with
            CBO, optional otherwise.
        spend_cap: Campaign spend cap in account currency; 0 or empty means
            none.
        start_time: Start, ISO 8601 (e.g. 2026-10-01T00:00:00Z); a bare
            YYYY-MM-DD means 00:00 UTC that day.
        end_time: End, same formats as start_time.
        schedule: Weekly delivery windows ("time of day" in Ads Manager; CBO
            only), a list of blocks like {"days": "MON-FRI", "start_hour": 13,
            "end_hour": 23} (day names, hours 0-23, end_hour inclusive) or the
            native {"start_day": "FRI", "start_hour": 22, "end_day": "SAT",
            "end_hour": 3}. [] clears it (deliver at any time). Hours apply in
            each viewer's local time, not the account time zone. A campaign
            schedule replaces every ad group's.
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
    excluded_interests: _StrListOpt = None,
    devices: _DictListOpt = None,
    carriers: _StrListOpt = None,
    view_modes: _StrListOpt = None,
) -> dict:
    """Draft a new Reddit ad group (created PAUSED) — returns a PREVIEW.

    Required: campaign_id, ad_group_name, conversion_pixel_id and at least
    one targeting list (geolocations, communities, interests or keywords —
    ids/names from search_reddit_targeting). For non-CBO campaigns also
    daily_budget (or lifetime_budget + end_time), bid_strategy and bid_type.
    Budgets are checked against max_daily_budget; keywords and geolocations
    are pre-validated with Reddit. Returns a preview with a plan_id, the
    payload and warnings; nothing changes until confirm_and_apply applies it.

    Args:
        campaign_id: Reddit campaign id (from get_reddit_campaigns).
        ad_group_name: Name of the new ad group.
        conversion_pixel_id: Pixel id (Reddit rule; from get_reddit_pixels).
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        daily_budget: Daily budget in account currency (positive). Required
            for non-CBO campaigns unless lifetime_budget is given; not allowed
            in CBO campaigns. Mutually exclusive with lifetime_budget.
        lifetime_budget: Lifetime budget in account currency (positive);
            requires end_time and is compared to max_daily_budget by its
            per-day share.
        bid_strategy: BIDLESS, MANUAL_BIDDING, MAXIMIZE_VOLUME or TARGET_CPX.
            Required for non-CBO campaigns; in CBO campaigns it must match the
            campaign's.
        bid_type: CPC, CPM, CPV, CPV6 or CPV15. Required for non-CBO
            campaigns.
        bid_value: Bid in account currency; MANUAL_BIDDING and TARGET_CPX
            (target cost per result) need it.
        optimization_goal: The pixel event to optimize for (PURCHASE,
            SIGN_UP, LEAD, PAGE_VISIT, ...). Empty in a CBO campaign inherits
            the campaign's.
        geolocations: Geolocation ids to target (e.g. "DE" or "DE:2874225").
        excluded_geolocations: Geolocation ids to exclude.
        communities: Community (subreddit) names to target.
        excluded_communities: Community names to exclude.
        interests: Interest ids to target.
        keywords: Keywords to target; Reddit refuses keywords that are not
            brand-safe.
        excluded_keywords: Keywords to exclude.
        languages: Language codes (ISO 639-1, upper-cased automatically,
            e.g. "DE"). Empty means every language.
        gender: FEMALE or MALE; empty means all genders.
        platforms: ALL, DESKTOP, DESKTOP_LEGACY, MOBILE_NATIVE, MOBILE_WEB,
            MOBILE_WEB_3X or SHREDTOP.
        expand_targeting: Whether Reddit may expand delivery beyond the
            targeting; omitted uses Reddit's default.
        start_time: Start, ISO 8601 (e.g. 2026-10-01T00:00:00Z); a bare
            YYYY-MM-DD means 00:00 UTC that day. Empty means now.
        end_time: End, same formats as start_time.
        schedule: Weekly delivery windows ("time of day" in Ads Manager), a
            list of blocks like {"days": "MON-FRI", "start_hour": 13,
            "end_hour": 23} (day names, hours 0-23, end_hour inclusive) or the
            native {"start_day": "FRI", "start_hour": 22, "end_day": "SAT",
            "end_hour": 3}. Omitted means delivery at any time. Hours apply in
            each viewer's local time, not the account time zone. Not allowed
            in CBO campaigns, whose schedule overrides every ad group.
        locations: Placements, FEED and/or COMMENTS_PAGE (conversation
            pages); cannot be an empty list.
        excluded_interests: Interest ids to exclude (Reddit marks this field
            deprecated).
        devices: Device targets, objects like {"type": "MOBILE", "os":
            "IOS", "min_version": "16"} (type DESKTOP or MOBILE; optional os
            ANDROID or IOS, major OS versions with iOS at least 14, and
            label_map {make: [models]} with makes/models from
            search_reddit_targeting kind "devices"). Omitted means every
            device; APP_INSTALLS campaigns take exactly one.
        carriers: Mobile carrier ids (from search_reddit_targeting kind
            "carriers", e.g. "O2_DEUTSCHLAND"), checked against Reddit's
            carrier list. Omitted means every carrier.
        view_modes: Feed layouts: ALL, CARD, CLASSIC, COMPACT or IMMERSIVE.
            Omitted uses Reddit's default.
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
        excluded_interests=excluded_interests,
        devices=devices,
        carriers=carriers,
        view_modes=view_modes,
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

    click_url (and image_url) are verified to be reachable before drafting,
    so ads do not point at unverified pages. Comments are public on Reddit
    ads. With post_id an EXISTING post is promoted instead, keeping its
    upvotes and comments: no post is created and the copy arguments are
    ignored. Returns a preview with a plan_id; nothing changes until
    confirm_and_apply applies it. Once enabled, the ad goes through Reddit
    policy review (PENDING_APPROVAL).

    Args:
        ad_group_id: Reddit ad group id (from get_reddit_ad_groups).
        profile_id: Reddit profile that authors the post, from
            list_reddit_funding_instruments (profiles). Required for a new
            post; with post_id it defaults to the post's.
        headline: Post headline, required for a new post, at most 300
            characters.
        click_url: Landing page (http:// or https://). Required for a new
            post. TEXT posts open themselves, so the full click_url must also
            appear in body. With post_id, TEXT posts take no click_url and
            media posts default it to the post's destination.
        ad_account_id: Reddit ad account id (from list_reddit_accounts).
            Empty uses reddit.ad_account_id from the config.
        ad_name: Ad name; empty uses the headline. Truncated to 200
            characters.
        post_type: TEXT (headline + optional body; default) or IMAGE
            (headline + public image_url).
        body: Post body text for TEXT posts, up to 40,000 characters.
        image_url: Publicly reachable image URL, required for IMAGE posts.
        call_to_action: One of Reddit's fixed labels for IMAGE posts: Apply
            Now, Book Now, Contact Us, Download, Get a Quote, Get Showtimes,
            Install, Learn More, Order Now, Play Now, Pre-order Now, See Menu,
            Shop Now, Sign Up, View More, Watch Now.
        display_url: Optional display URL shown with IMAGE posts.
        allow_comments: Whether Redditors can comment publicly on the ad
            (default true); false disables them.
        post_id: Existing post to promote (t3_...); headline, body,
            image_url, call_to_action and display_url are then ignored. Empty
            creates a new post.
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
    attribution_check / validate_tracking diagnose it, this closes the loop.
    Applies to future data only. Returns a preview with a plan_id and warnings;
    nothing changes until confirm_and_apply applies it.

    Args:
        event_name: Name of the GA4 event to mark as a key event, exactly as it
            fires (e.g. "sign_up", "purchase"). Required.
        counting_method: ONCE_PER_EVENT (counts every occurrence, e.g.
            purchases) or ONCE_PER_SESSION (counts once per session, e.g.
            sign-ups). Defaults to ONCE_PER_EVENT.
        property_id: Numeric GA4 property ID, with or without the "properties/"
            prefix. If empty, uses the default from config.
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

    Returns a preview with a plan_id (plus warnings where relevant); nothing
    changes until confirm_and_apply applies it. SMART_CAMPAIGN_* and
    GOOGLE_HOSTED types are auto-managed by Google and rejected with
    MUTATE_NOT_ALLOWED.

    Args:
        name: Conversion action name as it appears in Google Ads. Required.
        type_: ConversionActionType enum value, e.g. AD_CALL, WEBSITE_CALL,
            WEBPAGE, WEBPAGE_CODELESS, GOOGLE_ANALYTICS_4_CUSTOM,
            GOOGLE_ANALYTICS_4_PURCHASE, UPLOAD_CALLS, UPLOAD_CLICKS.
        category: ConversionActionCategory enum value, e.g. PHONE_CALL_LEAD,
            SUBMIT_LEAD_FORM, PURCHASE, ... (default DEFAULT).
        default_value: Value attributed to each conversion, in currency_code
            units (not micros). Must be >= 0. A positive default_value with
            always_use_default_value=False is a legal "fallback" config: the
            preview warns but does NOT flip the flag.
        currency_code: 3-letter ISO 4217 currency code for default_value
            (default USD).
        always_use_default_value: True ignores transaction values from the
            tag/import and always uses default_value; False uses default_value
            only as a fallback when no value is supplied.
        counting_type: ONE_PER_CLICK (default; one conversion per click, typical
            for lead gen) or MANY_PER_CLICK (every conversion counts, typical
            for ecommerce).
        phone_call_duration_seconds: Minimum call length in seconds for a call
            to count; only meaningful for call conversions (PHONE_CALL_LEAD
            category). 0 leaves Google's default.
        primary_for_goal: True (default) makes it a Primary action that Smart
            Bidding optimizes toward; False makes it Secondary (recorded only).
        include_in_conversions_metric: IMMUTABLE on create — Google derives it
            from the category, so this value is not sent; change it later via
            draft_update_conversion_action.
        click_through_window_days: Click-through attribution window in days,
            1-90. 0 leaves Google's default.
        view_through_window_days: View-through attribution window in days,
            1-30. 0 leaves Google's default.
        attribution_model: AttributionModel enum value, e.g.
            GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN. Empty leaves Google's default.
        customer_id: Google Ads customer ID (digits, dashes allowed). Defaults
            to the configured account.
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
    adjusting the call-duration threshold, or changing attribution.
    SMART_CAMPAIGN_* and GOOGLE_HOSTED types reject mutations with
    MUTATE_NOT_ALLOWED at apply time. Returns a preview with a plan_id (plus
    warnings where relevant); nothing changes until confirm_and_apply applies
    it.

    Args:
        conversion_action_id: Numeric ConversionAction ID. It comes from:
            SELECT conversion_action.id, conversion_action.name FROM
            conversion_action.
        name: New name. Empty keeps the current name.
        primary_for_goal: True makes it Primary (Smart Bidding optimizes toward
            it); False demotes it to Secondary. Omit to leave unchanged.
        default_value: New default value in the action's currency (not micros),
            >= 0. 0 leaves it unchanged.
        currency_code: New 3-letter ISO 4217 currency code. Empty leaves it
            unchanged.
        always_use_default_value: True always uses default_value; False uses
            it only as a fallback when the tag/import supplies no value (the
            preview warns when combined with a positive default_value). Omit to
            leave unchanged.
        counting_type: ONE_PER_CLICK or MANY_PER_CLICK. Empty leaves it
            unchanged.
        phone_call_duration_seconds: Minimum call length in seconds for a call
            to count. 0 leaves it unchanged.
        include_in_conversions_metric: True shows the action in the
            "Conversions" column; False in "All conversions" only. Mutable here
            (unlike on create). Omit to leave unchanged.
        click_through_window_days: Click-through attribution window in days,
            1-90. 0 leaves it unchanged.
        view_through_window_days: View-through attribution window in days,
            1-30. 0 leaves it unchanged.
        attribution_model: AttributionModel enum value, e.g.
            GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN. Empty leaves it unchanged.
        customer_id: Google Ads customer ID (digits, dashes allowed). Defaults
            to the configured account.
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
    GOOGLE_HOSTED types reject removal with MUTATE_NOT_ALLOWED. Returns a
    preview with a plan_id; nothing changes until confirm_and_apply applies it.

    Args:
        conversion_action_id: Numeric ConversionAction ID, e.g. from SELECT
            conversion_action.id, conversion_action.name FROM conversion_action.
        customer_id: Google Ads customer ID (digits, dashes allowed). Defaults
            to the configured account.
    """
    from adloop.ads.conversion_actions import (
        draft_remove_conversion_action as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        conversion_action_id=conversion_action_id,
    )


@_tool(title="Draft call conversion upload", annotations=_WRITE, tags={"ads"})
@_safe
def draft_upload_call_conversions(
    csv_path: str,
    default_region: str = "",
    consent: dict | None = None,
    customer_id: str = "",
) -> dict:
    """Draft an offline CALL-conversion upload from a CSV — returns a PREVIEW.

    Uploads call conversions via ConversionUploadService.UploadCallConversions,
    matching the call against the ad click by the caller's phone number.
    The CSV must have columns: Caller's Phone Number,
    Call Start Time, Conversion Name, Conversion Time, Conversion Value,
    Conversion Currency. The Conversion Name must match an existing
    UPLOAD_CALLS-type conversion action — verified against the account while
    drafting, so a typo fails here rather than after the upload.

    Local file only: on the hosted server this tool refuses, because it would
    read a path on the server rather than the caller's machine.

    PII: the caller phone number is required RAW by Google (it cannot be
    hashed), so it lives in the plan's apply-only payload — the preview shows
    redacted ids and counts, the audit log and `plan.changes` show neither.
    Apply uploads exactly what you previewed (no CSV re-read).

    Phone numbers are normalized with libphonenumber semantics: a number
    carries no country code needs ``default_region``, an extension is dropped,
    and the German trunk marker in "+49 (0)89 …" is handled. Rows whose number
    stays unusable are skipped and reported in `skipped_rows` instead of being
    uploaded to no effect.

    Returns a preview with a plan_id, the row counts and the skipped rows;
    nothing is uploaded until that preview is confirmed.

    Args:
        csv_path: Path of the CSV file holding the upload rows, read on the
            machine that runs AdLoop. The hosted server refuses this tool
            rather than reading a path of its own.
        default_region: ISO 3166-1 alpha-2 country assumed for phone numbers
            that carry no country code, e.g. "DE" for "0151 12345678". Empty
            leaves such numbers unusable and the row is skipped.
        consent: Consent signals for GDPR/EEA, as an object with "ad_user_data"
            and/or "ad_personalization", each "GRANTED", "DENIED" or
            "UNSPECIFIED". None sends UNSPECIFIED.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
    """
    from adloop.ads.conversion_actions import (
        draft_upload_call_conversions as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        csv_path=csv_path,
        default_region=default_region,
        consent=consent,
    )


@_tool(title="Draft enhanced conversions upload", annotations=_WRITE, tags={"ads"})
@_safe
def draft_upload_enhanced_conversions_for_leads(
    csv_path: str,
    default_region: str = "",
    consent: dict | None = None,
    customer_id: str = "",
) -> dict:
    """Draft an Enhanced Conversions for LEADS upload from a CSV — PREVIEW.

    Uploads lead conversions via
    ConversionUploadService.UploadClickConversions with user_identifiers,
    matching hashed customer PII back to the Google users who clicked the ads.
    The CSV holds RAW PII in columns: Email, Phone
    Number, First Name, Last Name (plus Conversion Name, Conversion Time,
    Conversion Value, Conversion Currency; optional Order ID dedup key).

    PII is normalized and SHA-256-hashed AT PREVIEW TIME — only the hashes are
    stored in the plan. Raw email/phone/name never land in the plan or the
    audit log. The target conversion action must be UPLOAD_CLICKS-type.

    An Order ID column makes re-uploads dedup instead of double-counting.

    Local file only: on the hosted server this tool refuses, because it would
    read a path on the server rather than the caller's machine.

    Returns a preview with a plan_id, the row counts and the skipped rows;
    nothing is uploaded until that preview is confirmed.

    Args:
        csv_path: Path of the CSV file holding the upload rows, read on the
            machine that runs AdLoop. The hosted server refuses this tool
            rather than reading a path of its own.
        default_region: ISO 3166-1 alpha-2 country assumed for phone numbers
            that carry no country code, e.g. "DE" for "0151 12345678". Empty
            leaves such numbers unusable; the row still uploads when its email
            or its complete address identifies the lead.
        consent: Consent signals for GDPR/EEA, as an object with "ad_user_data"
            and/or "ad_personalization", each "GRANTED", "DENIED" or
            "UNSPECIFIED". None sends UNSPECIFIED.
        customer_id: Google Ads customer ID (digits, e.g. "1234567890"). Empty
            uses the configured default account.
    """
    from adloop.ads.conversion_actions import (
        draft_upload_enhanced_conversions_for_leads as _impl,
    )

    return _impl(
        current_config(),
        customer_id=customer_id or current_config().ads.customer_id,
        csv_path=csv_path,
        default_region=default_region,
        consent=consent,
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

    Returns matched events with their GA4 counts, events missing from GA4,
    unexpected GA4 events, auto-collected events (page_view, session_start,
    etc.), insights on likely causes, and the date range.

    Args:
        expected_events: Event names found in the codebase, e.g. ["sign_up",
            "purchase"].
        property_id: GA4 property as "properties/123456789" (see
            get_account_summaries). If empty, uses the default from config.
        date_range_start: Start date: "today", "yesterday", "NdaysAgo" (e.g.
            "28daysAgo"), or "YYYY-MM-DD".
        date_range_end: End date, same formats as date_range_start.
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
    Optionally checks GA4 to warn if the event already fires. Returns the
    javascript snippet, already_exists / existing_count from GA4, and notes
    (duplicates, marking it as a key event, GDPR consent).

    Args:
        event_name: GA4 event name for the snippet, e.g. "sign_up" or
            "generate_lead".
        event_params: Event parameters as a name-to-value object, e.g.
            {"method": "email"}. Missing recommended parameters for well-known
            events are added as typed placeholders.
        trigger: "form_submit", "button_click", or "page_load" — wraps the gtag
            call in an appropriate event listener (with a YOUR_SELECTOR
            placeholder for forms and buttons). Empty = bare gtag call.
        property_id: GA4 property as "properties/123456789" used for the
            existing-event check. If empty, uses the default from config.
        check_existing: When true (default), looks up the last 28 days of GA4
            events to report whether this event already fires.
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
    Returns estimated clicks, cost, avg CPC, conversions and avg CPA for the
    forecast period, daily estimates, and insights.

    Args:
        keywords: List of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD",
            "max_cpc": 1.50}. match_type defaults to BROAD; max_cpc is optional
            (defaults to 1.00 in account currency, not micros). At least one
            keyword is required.
        daily_budget: Daily budget in account currency. If provided, insights
            will show what % of traffic the budget captures. 0 = no budget
            comparison.
        geo_target_id: Geo target constant ID (2276=Germany, 2840=USA, 2826=UK,
            2250=France). Defaults to 2276.
        language_id: Language constant ID (1000=English, 1001=German,
            1002=French, 1003=Spanish). Defaults to 1000.
        forecast_days: Forecast horizon in days, starting tomorrow (default 30).
        customer_id: Google Ads customer ID (digits, dashes allowed). Defaults
            to the configured account.
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

    Mirrors the "Discover new keywords" UI in Keyword Planner: start with
    keywords (seed_keywords), start with a website (url), or both together
    for more targeted ideas. At least one of seed_keywords or url is required.

    Returns keyword ideas sorted by avg monthly search volume, with
    competition level (LOW/MEDIUM/HIGH) and top-of-page bid range, plus the
    seeds used and insights.

    Args:
        seed_keywords: Seed terms, e.g. ["running shoes", "trail running"].
        url: A landing page or site URL to extract ideas from, e.g.
            "https://example.com/products".
        geo_target_id: Geo target constant ID (2276=Germany, 2840=USA, 2826=UK).
            Defaults to 2276.
        language_id: Language constant ID (1000=English, 1001=German,
            1002=French). Defaults to 1000.
        page_size: Max keyword ideas to return (default 50, clamped to 1-1000).
        customer_id: Google Ads customer ID (digits, dashes allowed). Defaults
            to the configured account.
        include_monthly_volumes: true adds per-month search history (last 24
            months, top-20 ideas) plus a seasonality insight — relevant to
            questions about demand trends, seasonality, or "when should I ramp
            budget".
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

    Args:
        brand_prefix: The brand name to look up, free text (e.g. "NoWayOut").
        selected_brand_ids: Commercial Knowledge Graph MIDs already picked,
            handed back so Google keeps them in the suggestion set while the
            prefix narrows. Optional.
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

    Returns checked / matched / no_match counts and one result per name with
    status "matched" (with a best-match brand plus all candidates) or
    "no_match" (empty candidate list — a normal answer, not an error).
    exact_match marks a candidate whose name matches the query apart from case
    and punctuation; anything else is a Google suggestion, not a guarantee.

    Args:
        brand_names: The names to triage, at most 25 per call — the API
            resolves one prefix per request, so longer lists take several calls.
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

    Returns each list's ID, name, status, and member count, plus the total —
    the basis for reusing an existing list with add_to_brand_list instead of
    duplicating it with propose_brand_list.

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

    Returns the brands and their total. Each entry carries brand.entity_id
    (the Commercial KG MID that suggest_brands returns as id), the display
    name, primary URL, status and a criterion_id — the latter is what
    remove_from_brand_list needs.

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
    CampaignSharedSet like negative keyword lists). Returns each attachment's
    campaign ID, name and status, the criterion, and `role`: "excluded" when
    the criterion is negative, "targeted" when it restricts targeting to the
    list.

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

    Returns a preview with a plan_id; nothing changes until the returned
    plan_id is applied with confirm_and_apply.

    Args:
        list_name: Name for the new list, as it should appear in Google Ads.
        brand_ids: Commercial Knowledge Graph MIDs — the `id` field from
            suggest_brands / check_brand_names, which resolve names; a
            display name alone cannot be written. Duplicates are dropped.
        campaign_ids: Optional numeric campaign IDs. When omitted, the list is
            created without being used yet; attach_brand_list_to_campaigns
            attaches it later.
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

    Returns a preview with a plan_id; nothing changes until the returned
    plan_id is applied with confirm_and_apply.

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

    Returns a preview with a plan_id; nothing changes until the returned
    plan_id is applied with confirm_and_apply.

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
    criteria, and `negative` decides the role. The shared_set_id comes from
    get_brand_lists; get_brand_list_campaigns lists existing attachments.

    Returns a preview with a plan_id; nothing changes until the returned
    plan_id is applied with confirm_and_apply.

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
    list itself and its brands are unchanged. get_brand_list_campaigns lists
    the existing attachments.

    Returns a preview with a plan_id; nothing changes until the returned
    plan_id is applied with confirm_and_apply.

    Args:
        shared_set_id: Numeric list ID from get_brand_lists.
        campaign_ids: Numeric campaign IDs to detach the list from. Campaigns
            that do not carry the list are reported as `not_attached` in the
            apply result instead of failing the batch.
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
