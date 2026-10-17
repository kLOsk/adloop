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


def get_merchant_feed_health(
    config: AdLoopConfig,
    *,
    account_id: str = "",
) -> dict:
    """Feed health for a Merchant Center account — disapprovals + issues.

    Combines aggregateProductStatuses (per reporting-context counts of
    approved/pending/disapproved products plus the product issues behind
    them) with account-level issues that can suspend the whole account.
    """
    from adloop.merchant.client import (
        API_ISSUE_RESOLUTION,
        MerchantNotRegistered,
        merchant_get,
        register_gcp,
    )

    account_id, error = _account_or_error(config, account_id)
    if error:
        return error

    def _fetch() -> tuple[dict, dict]:
        return (
            # Product-status aggregation is served by the issueresolution
            # sub-API, not accounts — the wrong prefix 404s.
            merchant_get(
                config,
                f"accounts/{account_id}/aggregateProductStatuses",
                api=API_ISSUE_RESOLUTION,
            ),
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

    contexts = []
    top_item_issues: list[dict] = []
    totals = {"approved": 0, "pending": 0, "disapproved": 0}
    for status in statuses.get("aggregateProductStatuses", []):
        stats = status.get("statistics", {}) or {}
        entry = {
            "reporting_context": status.get("reportingContext", ""),
            "country": status.get("countryCode", ""),
            "approved": int(stats.get("approvedCount", 0) or 0),
            "pending": int(stats.get("pendingCount", 0) or 0),
            "disapproved": int(stats.get("disapprovedCount", 0) or 0),
        }
        contexts.append(entry)
        totals["approved"] += entry["approved"]
        totals["pending"] += entry["pending"]
        totals["disapproved"] += entry["disapproved"]
        for issue in status.get("issues", []):
            top_item_issues.append({
                "reporting_context": status.get("reportingContext", ""),
                "issue": issue.get("issueType", "")
                or issue.get("title", ""),
                "severity": issue.get("severity", ""),
                "affected_products": int(issue.get("numProducts", 0) or 0),
                "documentation": issue.get("documentationUri", ""),
            })
    top_item_issues.sort(key=lambda i: i["affected_products"], reverse=True)

    insights = []
    serving_total = sum(totals.values())
    if serving_total and totals["disapproved"] > 0:
        pct = round(totals["disapproved"] / serving_total * 100, 1)
        insights.append(
            f"{totals['disapproved']:,} product(s) ({pct}%) are DISAPPROVED "
            f"and not serving — this directly starves Shopping and "
            f"Performance Max campaigns. Fix the top issues below before "
            f"touching bids or budgets."
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
    if not serving_total:
        # Zero products is not a healthy feed: Shopping and Performance Max
        # have nothing to advertise.
        insights.append(
            "The account has no products at all (0 approved, pending or "
            "disapproved), so Shopping and Performance Max campaigns have "
            "nothing to show. Either no feed or data source is set up yet, "
            "products were added in the last ~30 minutes (status lags), or "
            "the products live in another Merchant Center account."
        )
    elif not account_issues and not totals["disapproved"]:
        insights.append("No disapprovals or account issues — feed is healthy.")

    return {
        "account_id": account_id,
        "totals": totals,
        "by_reporting_context": contexts,
        "account_issues": account_issues,
        "top_item_issues": top_item_issues[:15],
        "insights": insights,
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

