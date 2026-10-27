"""Read and pause Google Ads auto-apply recommendation subscriptions.

A subscription makes Google apply every new recommendation of one type
without asking, so a change reaches the account with no preview. The v25 API
(verified against the SDK):

    recommendation_subscription  resource_name, type, status (ENABLED | PAUSED),
                                 create_date_time, modify_date_time
    RecommendationSubscriptionService.MutateRecommendationSubscription
        MutateRecommendationSubscriptionRequest{customer_id, operations[],
            partial_failure, validate_only, response_content_type}

A type with no subscription row is not auto-applied. The write side can only
set a subscription to PAUSED: re-enabling auto-apply is a decision for the
Google Ads UI, because it lets Google change the account without a preview,
which is what AdLoop's flow exists to prevent. Pausing reduces automation, so
a single confirmation is enough. The request has ``validate_only``, so the dry
run goes through the validate-only client like other Google Ads writes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

OPERATION = "pause_recommendation_subscriptions"

# A subscription modified this recently is reported as a change, not only as a
# standing setting: someone (or something) turned it on or touched it lately.
RECENT_DAYS = 30

_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_QUERY = """
    SELECT recommendation_subscription.resource_name,
           recommendation_subscription.type,
           recommendation_subscription.status,
           recommendation_subscription.create_date_time,
           recommendation_subscription.modify_date_time
    FROM recommendation_subscription
"""


def _read(config: AdLoopConfig, customer_id: str) -> list[dict]:
    from adloop.ads.gaql import execute_query

    return [
        {
            "type": row.get("recommendation_subscription.type", ""),
            "status": row.get("recommendation_subscription.status", ""),
            "resource_name": row.get("recommendation_subscription.resource_name", ""),
            "create_date_time": row.get("recommendation_subscription.create_date_time", ""),
            "modify_date_time": row.get("recommendation_subscription.modify_date_time", ""),
        }
        for row in execute_query(config, customer_id, _QUERY)
    ]


def _day(value: str) -> date | None:
    """``2026-10-27 09:14:03`` (account time zone) -> the date, or None."""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def get_recommendation_subscriptions(
    config: AdLoopConfig, *, customer_id: str = "", today: date | None = None
) -> dict:
    """Read-only: every auto-apply subscription with its status and dates."""
    subscriptions = sorted(_read(config, customer_id), key=lambda s: s["type"])
    enabled = [s for s in subscriptions if s["status"] == "ENABLED"]
    today = today or datetime.now(timezone.utc).date()

    insights: list[str] = []
    if enabled:
        insights.append(
            f"{len(enabled)} recommendation type(s) auto-apply without a preview: "
            + ", ".join(s["type"] for s in enabled)
            + ". Google changes the account for these on its own. "
            "draft_recommendation_subscriptions pauses them."
        )
    for sub in enabled:
        modified = _day(sub["modify_date_time"])
        if modified and (today - modified).days <= RECENT_DAYS:
            insights.append(
                f"{sub['type']} auto-apply was turned on or changed on "
                f"{sub['modify_date_time']}, within the last {RECENT_DAYS} days. "
                "Confirm that was intended."
            )
    if not enabled:
        insights.append("No recommendation type is set to auto-apply.")

    return {
        "subscriptions": subscriptions,
        "total_subscriptions": len(subscriptions),
        "enabled_count": len(enabled),
        "insights": insights,
    }


def draft_recommendation_subscriptions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    recommendation_types: list[str] | None = None,
) -> dict:
    """Draft pausing auto-apply for recommendation types — returns PREVIEW.

    Pausing is the only change this can make. Each type must have an ENABLED
    subscription; a type with none, or one already paused, is refused by name
    so the preview only lists what the apply will change.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(OPERATION, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    types: list[str] = []
    errors: list[str] = []
    for raw in recommendation_types or []:
        value = str(raw or "").strip().upper()
        if not value:
            continue
        if not _TYPE_RE.match(value):
            errors.append(f"'{raw}' is not a RecommendationType name")
        elif value not in types:
            types.append(value)
    if not types and not errors:
        errors.append("At least one recommendation type is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    current = {s["type"]: s for s in _read(config, customer_id)}
    not_enabled = [t for t in types if current.get(t, {}).get("status") != "ENABLED"]
    if not_enabled:
        return {
            "error": "Validation failed",
            "details": [
                "Not auto-applied in this account (no subscription, or already "
                "paused): " + ", ".join(not_enabled)
                + ". get_recommendation_subscriptions lists the subscriptions."
            ],
        }

    subscriptions = [
        {
            "type": t,
            "resource_name": current[t]["resource_name"],
            "current_status": "ENABLED",
            "status": "PAUSED",
            "modify_date_time": current[t]["modify_date_time"],
        }
        for t in types
    ]
    plan = ChangePlan(
        operation=OPERATION,
        entity_type="recommendation_subscription",
        entity_id=",".join(types),
        customer_id=customer_id,
        changes={
            "subscriptions": subscriptions,
            "count": len(subscriptions),
            "note": (
                "Pausing stops Google applying these recommendation types on its "
                "own. Recommendations still appear in get_recommendations. "
                "Re-enabling auto-apply is done in the Google Ads UI."
            ),
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _apply_pause_recommendation_subscriptions(
    client: object, cid: str, changes: dict
) -> dict:
    """Set each planned subscription to PAUSED in one request."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("RecommendationSubscriptionService")
    request = client.get_type("MutateRecommendationSubscriptionRequest")
    request.customer_id = cid
    paused_status = client.enums.RecommendationSubscriptionStatusEnum.PAUSED
    for sub in changes["subscriptions"]:
        # Hard-coded: whatever a stored plan says, this path only pauses.
        operation = client.get_type("RecommendationSubscriptionOperation")
        operation.update.resource_name = sub["resource_name"]
        operation.update.status = paused_status
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        request.operations.append(operation)

    response = service.mutate_recommendation_subscription(request=request)
    paused = [result.resource_name for result in response.results]
    return {"paused": paused, "paused_count": len(paused)}
