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

    account_id = str(account_id or "").strip()
    if not account_id.isdigit():
        return {
            "error": "account_id must be a numeric Merchant Center ID — "
                     "call list_merchant_accounts first."
        }

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
    if not account_issues and not totals["disapproved"]:
        insights.append("No disapprovals or account issues — feed is healthy.")

    return {
        "account_id": account_id,
        "totals": totals,
        "by_reporting_context": contexts,
        "account_issues": account_issues,
        "top_item_issues": top_item_issues[:15],
        "insights": insights,
    }
