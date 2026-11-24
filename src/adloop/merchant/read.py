"""Merchant Center read tools — account discovery + feed health.

Read-only use of the Merchant API (Google offers no read-only scope for
it). This is the minimal slice of the Merchant Center integration: which
accounts exist, and whether products are actually serving — disapproved
feed items are the most common silent killer of Shopping/PMax campaigns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_REGISTRATION_GUIDE = (
    "https://developers.google.com/merchant/api/guides/quickstart/registration"
)


def _registration_failed(account_id: str, error: Exception) -> dict:
    """User-facing error when automatic registerGcp did not succeed."""
    return {
        "error": (
            f"The Merchant API blocks this Google Cloud project until it is "
            f"registered as a developer with merchant account {account_id}, "
            f"and automatic registration failed: {error}"
        ),
        "hint": (
            "Registration requires the connected Google user to have Admin "
            "access on the Merchant Center account (Merchant Center → "
            "Settings → People and access). Grant Admin access and retry, "
            f"or register manually: {_REGISTRATION_GUIDE}"
        ),
        "registration_required": True,
    }


def _registered_retry_later(account_id: str) -> dict:
    """Registration just succeeded but Google has not propagated it yet."""
    return {
        "status": "DEVELOPER_REGISTRATION_COMPLETED",
        "account_id": account_id,
        "retry_after_seconds": 300,
        "message": (
            f"Done: this Google Cloud project was just registered as a "
            f"developer with merchant account {account_id} — a one-time "
            f"Merchant API requirement. Google needs about 5 minutes to "
            f"propagate the registration; retry this tool shortly. No feed "
            f"data was fetched yet."
        ),
    }


def list_merchant_accounts(config: AdLoopConfig) -> dict:
    """List Merchant Center accounts the connected Google user can access."""
    from adloop.merchant.client import MerchantNotRegistered, merchant_get

    accounts: list[dict] = []
    page_token = ""
    while True:
        params = {"pageToken": page_token} if page_token else {}
        try:
            payload = merchant_get(config, "accounts", params)
        except MerchantNotRegistered as e:
            # Chicken-and-egg: every call except registerGcp is blocked
            # while unregistered, and registering needs an account ID we
            # cannot list. The user must supply the ID once.
            return {
                "error": (
                    "The Merchant API rejected the call: this Google Cloud "
                    "project is not yet registered as a developer with your "
                    "merchant account — a one-time Merchant API requirement. "
                    "Accounts cannot be listed until then (registration is "
                    "the only call Google allows beforehand)."
                ),
                "hint": (
                    "Find the numeric Merchant Center ID in the Merchant "
                    "Center UI (top-right corner / in the URL), then call "
                    "get_merchant_feed_health with that account_id — it "
                    "registers this project automatically. Manual steps: "
                    f"{_REGISTRATION_GUIDE}"
                ),
                "registration_required": True,
                "details": str(e),
            }
        for account in payload.get("accounts", []):
            name = account.get("name", "")
            accounts.append({
                "account_id": account.get("accountId")
                or name.removeprefix("accounts/"),
                "name": account.get("accountName", ""),
                "test_account": bool(account.get("testAccount", False)),
            })
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            break

    insights = []
    if not accounts:
        insights.append(
            "No Merchant Center accounts are accessible with this Google "
            "account. If you expected one, check user access in Merchant "
            "Center under Settings → People and access."
        )
    return {"accounts": accounts, "total": len(accounts), "insights": insights}


# ---------------------------------------------------------------------------
# Feed health (issueresolution/v1 aggregateProductStatuses)
# ---------------------------------------------------------------------------

# Stats counters of an AggregateProductStatus row, as output key -> API
# field. The API calls serving products "active"; the output keeps
# "approved". An expiring product is still active, so "expiring" is
# reported but never added into a total or a percentage.
_STAT_FIELDS = (
    ("approved", "activeCount"),
    ("pending", "pendingCount"),
    ("disapproved", "disapprovedCount"),
    ("expiring", "expiringCount"),
)
_LISTED_FIELDS = ("approved", "pending", "disapproved")

# Reporting contexts in which products are advertised for money. A
# disapproval there costs Shopping / Performance Max reach; one in a free
# context (FREE_LISTINGS, FREE_LOCAL_LISTINGS, ...) costs organic exposure
# only. Everything else (YouTube, reviews, cloud retail) is "other".
_PAID_CONTEXTS = frozenset({
    "SHOPPING_ADS",
    "DISPLAY_ADS",
    "VIDEO_ADS",
    "DEMAND_GEN_ADS",
    "DEMAND_GEN_ADS_DISCOVER_SURFACE",
    "DISCOVERY_ADS",
    "LOCAL_INVENTORY_ADS",
    "VEHICLE_INVENTORY_ADS",
    "RENTAL_ADS",
})
_UNSPECIFIED_CONTEXT = "REPORTING_CONTEXT_ENUM_UNSPECIFIED"
_SURFACE_ORDER = {"paid": 0, "free": 1, "other": 2, "unspecified": 3}
_SEVERITY_ORDER = {"DISAPPROVED": 0, "DEMOTED": 1, "NOT_IMPACTED": 2}
_SERVING_IMPACT = ("DISAPPROVED", "DEMOTED")

# The API returns 25 rows per page by default and at most 250. An account
# with a few target countries already spans several reporting contexts per
# country, so the first page alone undercounts.
_AGGREGATE_PAGE_SIZE = 250

_FEED_HEALTH_NOTE = (
    "A product is counted once per reporting context and target country, "
    "so never add reporting contexts together: read the counts per "
    "context. affected_products on an item issue is its largest single "
    "context, a floor on the products it hits. Product-status data lags "
    "feed changes by about 30 minutes."
)


def _surface(context: str) -> str:
    """paid / free / other / unspecified for a reporting context."""
    if context in _PAID_CONTEXTS:
        return "paid"
    if context.startswith("FREE_"):
        return "free"
    if context == _UNSPECIFIED_CONTEXT:
        return "unspecified"
    return "other"


def _row_context(row: dict) -> str:
    """Reporting context of an aggregate row.

    Google leaves reportingContext out on some rows. The resource name of
    such a row still ends in ``{context}~{country}``, so fall back to it;
    a row that names no context either is unspecified.
    """
    context = row.get("reportingContext") or ""
    if context:
        return context
    tail = (row.get("name") or "").rsplit("/", 1)[-1]
    candidate = tail.split("~", 1)[0] if "~" in tail else ""
    if candidate and candidate.replace("_", "").isalpha() and candidate.isupper():
        return candidate
    return _UNSPECIFIED_CONTEXT


def _fetch_aggregate_statuses(config: AdLoopConfig, account_id: str) -> list[dict]:
    """Every aggregateProductStatuses row, across all pages.

    Google can return the same row on more than one page; a row is one
    (reporting context, country) pair with a unique resource name, so rows
    whose name was already seen are dropped instead of being counted twice.
    """
    from adloop.merchant.client import API_ISSUE_RESOLUTION, merchant_get

    rows: list[dict] = []
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()
    page_token = ""
    while True:
        params: dict = {"pageSize": _AGGREGATE_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        # Product-status aggregation is served by the issueresolution
        # sub-API, not accounts — the wrong prefix 404s.
        payload = merchant_get(
            config,
            f"accounts/{account_id}/aggregateProductStatuses",
            params,
            api=API_ISSUE_RESOLUTION,
        )
        for row in payload.get("aggregateProductStatuses") or []:
            name = row.get("name") or ""
            if name:
                if name in seen_names:
                    continue
                seen_names.add(name)
            rows.append(row)
        page_token = payload.get("nextPageToken") or ""
        # A token handed out twice would loop forever.
        if not page_token or page_token in seen_tokens:
            break
        seen_tokens.add(page_token)
    return rows


def _disapproval_share(context: str, counts: dict) -> str:
    listed = sum(counts[key] for key in _LISTED_FIELDS)
    pct = round(counts["disapproved"] / listed * 100, 1) if listed else 0.0
    return f"{context} {counts['disapproved']:,} of {listed:,} ({pct}%)"


def get_merchant_feed_health(
    config: AdLoopConfig,
    *,
    account_id: str = "",
) -> dict:
    """Feed health for a Merchant Center account — disapprovals + issues.

    Combines aggregateProductStatuses (per reporting context and country:
    approved/pending/disapproved/expiring products plus the item issues
    behind them) with account-level issues that can suspend the whole
    account. Paid reporting contexts (Shopping, display, video, ...) are
    reported apart from free listings: only the former starve Shopping and
    Performance Max campaigns.
    """
    from adloop.merchant.client import (
        MerchantNotRegistered,
        merchant_get,
        register_gcp,
    )

    account_id, error = _account_or_error(config, account_id)
    if error:
        return error

    def _fetch() -> tuple[list[dict], dict]:
        return (
            _fetch_aggregate_statuses(config, account_id),
            merchant_get(config, f"accounts/{account_id}/issues"),
        )

    try:
        statuses, issues_payload = _fetch()
    except MerchantNotRegistered:
        # One-time Merchant API setup: register this GCP project as a
        # developer with the merchant account, then retry once in case
        # the registration propagates instantly.
        try:
            register_gcp(config, account_id)
        except Exception as e:
            return _registration_failed(account_id, e)
        try:
            statuses, issues_payload = _fetch()
        except MerchantNotRegistered:
            return _registered_retry_later(account_id)

    account_issues = [
        {
            "title": issue.get("title", ""),
            "severity": issue.get("severity", ""),
            "detail": issue.get("detail", ""),
            "reporting_contexts": sorted({
                dest.get("reportingContext", "")
                for dest in issue.get("impactedDestinations", [])
                if dest.get("reportingContext")
            }),
            "documentation": issue.get("documentationUri", ""),
        }
        for issue in issues_payload.get("accountIssues", [])
    ]

    contexts: list[dict] = []
    totals: dict[str, dict] = {}
    by_code: dict[str, dict] = {}
    for status in statuses:
        context = _row_context(status)
        surface = _surface(context)
        stats = status.get("stats") or {}
        entry = {
            "reporting_context": context,
            "surface": surface,
            "country": status.get("country", ""),
        }
        for key, field in _STAT_FIELDS:
            entry[key] = int(stats.get(field, 0) or 0)
        contexts.append(entry)

        total = totals.setdefault(
            context, {"surface": surface, **{key: 0 for key, _ in _STAT_FIELDS}}
        )
        for key, _ in _STAT_FIELDS:
            total[key] += entry[key]

        # The same issue code repeats for every context and country it
        # touches; one entry per code, sized by its largest single row,
        # because summing rows would count a product once per context.
        for issue in status.get("itemLevelIssues") or []:
            code = issue.get("code", "")
            severity = issue.get("severity", "")
            record = by_code.setdefault(code, {
                "issue": code,
                "description": issue.get("description", ""),
                "severity": severity,
                "resolution": issue.get("resolution", ""),
                "attribute": issue.get("attribute", ""),
                "affected_products": 0,
                "hits_paid_surface": False,
                "reporting_contexts": set(),
                "documentation": issue.get("documentationUri", ""),
            })
            record["affected_products"] = max(
                record["affected_products"], int(issue.get("productCount", 0) or 0)
            )
            if _SEVERITY_ORDER.get(severity, 9) < _SEVERITY_ORDER.get(record["severity"], 9):
                record["severity"] = severity
            record["reporting_contexts"].add(context)
            if surface == "paid" and severity in _SERVING_IMPACT:
                record["hits_paid_surface"] = True

    contexts.sort(key=lambda c: (
        _SURFACE_ORDER[c["surface"]], c["reporting_context"], c["country"],
    ))
    totals = dict(sorted(
        totals.items(), key=lambda kv: (_SURFACE_ORDER[kv[1]["surface"]], kv[0])
    ))
    top_item_issues = sorted(
        by_code.values(),
        key=lambda i: (
            _SEVERITY_ORDER.get(i["severity"], 9),
            not i["hits_paid_surface"],
            -i["affected_products"],
        ),
    )
    for issue in top_item_issues:
        issue["reporting_contexts"] = sorted(issue["reporting_contexts"])

    insights = []
    paid = {c: t for c, t in totals.items() if t["surface"] == "paid"}
    free = {c: t for c, t in totals.items() if t["surface"] == "free"}
    paid_hit = [c for c, t in paid.items() if t["disapproved"]]
    free_hit = [c for c, t in free.items() if t["disapproved"]]
    if paid_hit:
        insights.append(
            "Paid surfaces: "
            + "; ".join(_disapproval_share(c, paid[c]) for c in paid_hit)
            + " products DISAPPROVED and not serving — this directly starves "
            "Shopping and Performance Max campaigns. Fix the top issues "
            "below before touching bids or budgets."
        )
    elif paid:
        insights.append(
            "Paid surfaces (" + ", ".join(paid) + ") have no disapproved "
            "products — disapprovals are not what holds Shopping or "
            "Performance Max back."
        )
    if free_hit:
        insights.append(
            "Free listings: "
            + "; ".join(_disapproval_share(c, free[c]) for c in free_hit)
            + " products disapproved — this costs organic exposure only, "
            "not Shopping or Performance Max reach."
        )
    unspecified = [c for c in contexts if c["surface"] == "unspecified"]
    if unspecified:
        insights.append(
            f"{len(unspecified)} row(s) came back without a reporting "
            "context; they are listed in by_reporting_context but left out "
            "of the paid / free split."
        )
    critical = [
        i for i in account_issues if i["severity"] in ("CRITICAL", "ERROR")
    ]
    if critical:
        insights.append(
            f"{len(critical)} account-level issue(s) at ERROR/CRITICAL "
            f"severity — CRITICAL means offers stop serving entirely: "
            + "; ".join(i["title"] for i in critical[:3])
        )
    any_products = any(c[key] for c in contexts for key in _LISTED_FIELDS)
    any_disapproved = any(c["disapproved"] for c in contexts)
    if not any_products:
        # Zero products is not a healthy feed: Shopping and Performance Max
        # have nothing to advertise.
        insights.append(
            "The account has no products at all (0 approved, pending or "
            "disapproved), so Shopping and Performance Max campaigns have "
            "nothing to show. Either no feed or data source is set up yet, "
            "products were added in the last ~30 minutes (status lags), or "
            "the products live in another Merchant Center account."
        )
    elif not account_issues and not any_disapproved:
        insights.append("No disapprovals or account issues — feed is healthy.")

    return {
        "account_id": account_id,
        "totals": totals,
        "by_reporting_context": contexts,
        "account_issues": account_issues,
        "top_item_issues": top_item_issues[:15],
        "insights": insights,
        "note": _FEED_HEALTH_NOTE,
    }


# ---------------------------------------------------------------------------
# Product-level issues (Reports API, product_view)
# ---------------------------------------------------------------------------

_STATUS_FILTERS = {
    "disapproved": "NOT_ELIGIBLE_OR_DISAPPROVED",
    "limited": "ELIGIBLE_LIMITED",
    "pending": "PENDING",
}
_CLICK_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_MAX_PRODUCTS = 200


def _account_or_error(config: AdLoopConfig, account_id: str) -> tuple[str, dict | None]:
    account_id = str(account_id or "").strip() or config.merchant.account_id.strip()
    if not account_id:
        from adloop.runtime import default_setting_hint

        return "", {
            "error": "account_id is required: no Merchant Center account was passed and no default is configured.",
            "hint": (
                "Call list_merchant_accounts to find the numeric Merchant Center ID and pass it as account_id, or "
                + default_setting_hint("merchant.account_id", "Settings → Google & accounts")
                + "."
            ),
        }
    if not account_id.isdigit():
        return "", {"error": "account_id must be a numeric Merchant Center ID — call list_merchant_accounts first."}
    return account_id, None


def _issue_entry(issue: dict) -> dict:
    severity = issue.get("severity") or {}
    contexts = [
        {
            "reporting_context": ctx.get("reportingContext", ""),
            "disapproved_countries": ctx.get("disapprovedCountries", []) or [],
            "demoted_countries": ctx.get("demotedCountries", []) or [],
        }
        for ctx in severity.get("severityPerReportingContext", []) or []
    ]
    return {
        "code": (issue.get("type") or {}).get("code", ""),
        "attribute": (issue.get("type") or {}).get("canonicalAttribute", ""),
        "severity": severity.get("aggregatedSeverity", ""),
        "resolution": issue.get("resolution", ""),
        "where": contexts,
    }


def get_merchant_product_issues(
    config: AdLoopConfig,
    *,
    account_id: str = "",
    status: str = "disapproved",
    issue_code: str = "",
    limit: int = 50,
) -> dict:
    """The products behind the feed-health counts, with each product's issues.

    Queries product_view through the Reports API: offer id, title, brand,
    status, click potential and item issues (code, attribute, severity per
    reporting context with the affected countries, and whether the merchant
    has to act). Products with high click potential come first, because
    fixing them recovers the most traffic.
    """
    from adloop.merchant.client import API_REPORTS, MerchantNotRegistered, merchant_post, register_gcp

    account_id, error = _account_or_error(config, account_id)
    if error:
        return error
    if status not in (*_STATUS_FILTERS, "any"):
        return {"error": "status must be disapproved (default), limited, pending or any"}
    limit = max(1, min(int(limit or 50), _MAX_PRODUCTS))
    issue_code = str(issue_code or "").strip()

    where = f" WHERE aggregated_reporting_context_status = '{_STATUS_FILTERS[status]}'" if status in _STATUS_FILTERS else ""
    query = (
        "SELECT id, offer_id, title, brand, feed_label, availability, click_potential, "
        f"aggregated_reporting_context_status, item_issues FROM product_view{where}"
    )

    def _fetch() -> dict:
        return merchant_post(
            config,
            f"accounts/{account_id}/reports:search",
            {"query": query, "pageSize": 1000},
            api=API_REPORTS,
        )

    try:
        payload = _fetch()
    except MerchantNotRegistered:
        try:
            register_gcp(config, account_id)
        except Exception as e:
            return _registration_failed(account_id, e)
        try:
            payload = _fetch()
        except MerchantNotRegistered:
            return _registered_retry_later(account_id)

    products = []
    by_code: dict[str, dict] = {}
    for row in payload.get("results", []) or []:
        view = row.get("productView") or {}
        issues = [_issue_entry(i) for i in view.get("itemIssues", []) or []]
        if status == "any" and not issues:
            continue
        if issue_code and not any(i["code"] == issue_code for i in issues):
            continue
        product = {
            "offer_id": view.get("offerId", ""),
            "product_id": view.get("id", ""),
            "title": view.get("title", ""),
            "brand": view.get("brand", ""),
            "feed_label": view.get("feedLabel", ""),
            "availability": view.get("availability", ""),
            "status": view.get("aggregatedReportingContextStatus", ""),
            "click_potential": view.get("clickPotential", ""),
            "issues": issues,
        }
        products.append(product)
        for issue in issues:
            entry = by_code.setdefault(issue["code"], {
                "code": issue["code"],
                "attribute": issue["attribute"],
                "severity": issue["severity"],
                "merchant_action": issue["resolution"] == "MERCHANT_ACTION",
                "products": 0,
            })
            entry["products"] += 1
    products.sort(key=lambda p: (_CLICK_ORDER.get(p["click_potential"], 3), p["title"]))
    summary = sorted(by_code.values(), key=lambda e: e["products"], reverse=True)

    insights = []
    if not products:
        insights.append(
            f"No products with status {status}." if status != "any" else "No product has an item issue."
        )
    high = [p for p in products if p["click_potential"] == "HIGH"]
    if high and status == "disapproved":
        insights.append(
            f"{len(high)} disapproved product(s) have HIGH click potential: fixing them recovers the most traffic first."
        )
    if summary:
        top = summary[0]
        insights.append(
            f"Most common issue: {top['code']}"
            + (f" on attribute {top['attribute']}" if top["attribute"] else "")
            + f" ({top['products']} product(s))."
        )
    waiting = [e["code"] for e in summary if not e["merchant_action"]]
    if waiting:
        insights.append(
            "Pending Google's processing, nothing to do yet: " + ", ".join(waiting[:5]) + "."
        )
    if payload.get("nextPageToken"):
        insights.append("More than 1,000 products matched; narrow with issue_code to see the rest.")

    return {
        "account_id": account_id,
        "status": status,
        "products": products[:limit],
        "shown": min(limit, len(products)),
        "matched": len(products),
        "issues_by_code": summary[:20],
        "insights": insights,
        "note": "Product data lags about 30 minutes behind feed changes.",
    }

