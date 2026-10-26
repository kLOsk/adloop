"""Dismiss Google Ads recommendations.

``get_recommendations`` (in ``ads/read.py``) lists the active ones; this module
turns a chosen subset down. The v25 call (verified against the SDK):

    RecommendationService.DismissRecommendation
        DismissRecommendationRequest{customer_id, operations[], partial_failure}
        DismissRecommendationOperation{resource_name}

The request has **no** ``validate_only`` field, so a dry run cannot hand the
exact call to Google the way other Google Ads writes do. Instead the dry run
re-reads the account and checks that every planned recommendation is still
active (``preflight``), the same approach the Reddit, Tag Manager and GA4
writes use. The validate-only client still refuses the call outright, so this
path can never dismiss anything during a dry run.

Dismissing is mild: nothing in the account changes, and Google can show the
recommendation again later. A single confirmation is enough.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

OPERATION = "dismiss_recommendations"

# A recommendation ID is the last segment of its resource name. Google does not
# document the alphabet, so accept what resource names can carry and nothing
# that could break out of one.
_ID_RE = re.compile(r"^[A-Za-z0-9_~=+-]+$")
_RESOURCE_RE = re.compile(r"^customers/(\d+)/recommendations/([A-Za-z0-9_~=+-]+)$")


def recommendation_id(resource_name: str) -> str:
    """``customers/1/recommendations/abc`` -> ``abc``."""
    return str(resource_name or "").rsplit("/", 1)[-1]


def _clean_ids(values: list[str] | None, cid: str) -> tuple[list[str], list[str]]:
    """Accept bare IDs or full resource names. Returns (errors, deduped IDs)."""
    errors: list[str] = []
    ids: list[str] = []
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        match = _RESOURCE_RE.match(value)
        if match:
            if match.group(1) != cid:
                errors.append(
                    f"{value} belongs to account {match.group(1)}, not {cid}"
                )
                continue
            value = match.group(2)
        elif not _ID_RE.match(value):
            errors.append(f"'{value}' is not a recommendation ID or resource name")
            continue
        if value not in ids:
            ids.append(value)
    return errors, ids


def _active_by_id(config: AdLoopConfig, customer_id: str) -> dict[str, dict]:
    """Every active (not dismissed) recommendation, keyed by ID."""
    from adloop.ads.read import get_recommendations

    rows = get_recommendations(config, customer_id=customer_id).get(
        "recommendations", []
    )
    return {
        recommendation_id(row.get("recommendation.resource_name", "")): row
        for row in rows
        if row.get("recommendation.resource_name")
    }


def draft_dismiss_recommendations(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    recommendation_ids: list[str] | None = None,
) -> dict:
    """Draft dismissing recommendations — returns PREVIEW.

    Reads the active recommendations first: an ID that is unknown or already
    dismissed is refused by name, so the preview only ever lists what the
    apply will turn down. Each entry carries its type, campaign and Google's
    estimated improvement, the same numbers ``get_recommendations`` shows.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(OPERATION, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    errors, ids = _clean_ids(recommendation_ids, cid)
    if not ids and not errors:
        errors.append("At least one recommendation_id is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    active = _active_by_id(config, customer_id)
    missing = [rec_id for rec_id in ids if rec_id not in active]
    if missing:
        return {
            "error": "Validation failed",
            "details": [
                "Not an active recommendation in this account (unknown, or "
                "already dismissed): " + ", ".join(missing)
                + ". get_recommendations lists the active ones with their IDs."
            ],
        }

    recommendations = [
        {
            "recommendation_id": rec_id,
            "resource_name": active[rec_id]["recommendation.resource_name"],
            "type": active[rec_id].get("recommendation.type", ""),
            "campaign": active[rec_id].get("recommendation.campaign", ""),
            "ad_group": active[rec_id].get("recommendation.ad_group", ""),
            "estimated_improvement": active[rec_id].get("estimated_improvement", {}),
        }
        for rec_id in ids
    ]
    plan = ChangePlan(
        operation=OPERATION,
        entity_type="recommendation",
        entity_id=",".join(ids),
        customer_id=customer_id,
        changes={
            "recommendations": recommendations,
            "count": len(recommendations),
            "note": (
                "Dismissing changes nothing in the account. Google can show a "
                "dismissed recommendation again later."
            ),
        },
    )
    store_plan(plan)
    return plan.to_preview()


def preflight(config: AdLoopConfig, plan: object) -> dict:
    """Dry run: every planned recommendation must still be active.

    DismissRecommendation has no validate-only mode, so this re-read is the
    whole check. Raises when any recommendation has gone (dismissed in the UI,
    applied, or expired), so the dry run fails instead of reporting success.
    """
    planned = [r["recommendation_id"] for r in plan.changes["recommendations"]]
    active = _active_by_id(config, plan.customer_id)
    gone = [rec_id for rec_id in planned if rec_id not in active]
    if gone:
        raise ValueError(
            "No longer active (dismissed, applied or expired since the "
            "preview): " + ", ".join(gone) + ". Draft again."
        )
    return {"recommendations_checked": len(planned), "still_active": len(planned)}


def _apply_dismiss_recommendations(client: object, cid: str, changes: dict) -> dict:
    """Dismiss the planned recommendations; per-recommendation errors are reported."""
    from adloop.ads.write import _parse_partial_failure_per_op

    names = [r["resource_name"] for r in changes["recommendations"]]
    service = client.get_service("RecommendationService")
    request = client.get_type("DismissRecommendationRequest")
    request.customer_id = cid
    request.partial_failure = True
    operation_type = type(request).DismissRecommendationOperation
    for name in names:
        request.operations.append(operation_type(resource_name=name))

    response = service.dismiss_recommendation(request=request)

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)
    dismissed: list[str] = []
    failed: list[dict] = []
    for index, result in enumerate(response.results):
        if result.resource_name:
            dismissed.append(result.resource_name)
        else:
            failed.append(
                {
                    "recommendation_id": recommendation_id(names[index]),
                    "error": per_op_errors.get(
                        index, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out: dict = {"dismissed": dismissed, "dismissed_count": len(dismissed)}
    if failed:
        out["partial_failure"] = True
        out["failed"] = failed
        message = getattr(pf_error, "message", "") if pf_error is not None else ""
        if message:
            out["partial_failure_message"] = message
    return out
