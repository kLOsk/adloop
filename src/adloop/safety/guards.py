"""Safety guards — budget caps, bid limits, and blocked operation enforcement."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig, SafetyConfig
    from adloop.safety.preview import ChangePlan


class SafetyViolation(Exception):
    """Raised when a proposed change violates safety constraints."""


def check_budget_cap(daily_budget: float, config: SafetyConfig) -> None:
    """Reject if proposed daily budget exceeds configured maximum."""
    if daily_budget > config.max_daily_budget:
        raise SafetyViolation(
            f"Daily budget {daily_budget:.2f} exceeds maximum {config.max_daily_budget:.2f}"
        )


def check_bid_increase(current_bid: float, proposed_bid: float, config: SafetyConfig) -> None:
    """Reject if bid increase percentage exceeds configured maximum."""
    if current_bid <= 0:
        return
    increase_pct = ((proposed_bid - current_bid) / current_bid) * 100
    if increase_pct > config.max_bid_increase_pct:
        raise SafetyViolation(
            f"Bid increase {increase_pct:.0f}% exceeds maximum {config.max_bid_increase_pct}%"
        )


def check_blocked_operation(operation: str, config: SafetyConfig) -> None:
    """Reject if operation is in the blocked list."""
    if operation in config.blocked_operations:
        raise SafetyViolation(f"Operation '{operation}' is blocked by configuration")


def requires_double_confirmation(operation: str, **kwargs: object) -> bool:
    """Return True if this operation is destructive enough to need double confirmation.

    Triggers on:
    - Any delete or remove operation
    - Budget increases >50%
    """
    if "delete" in operation or "remove" in operation:
        return True

    current = kwargs.get("current_budget")
    proposed = kwargs.get("proposed_budget")
    if isinstance(current, (int, float)) and isinstance(proposed, (int, float)) and current > 0:
        if ((proposed - current) / current) > 0.5:
            return True

    return False


# ---------------------------------------------------------------------------
# Shared by the draft tools and confirm_and_apply of every platform
# ---------------------------------------------------------------------------


def blocked_operation_error(operation: str, config: SafetyConfig) -> dict | None:
    """A draft tool's refusal for a blocked operation, or None when it may proceed."""
    try:
        check_blocked_operation(operation, config)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def store_draft(plan: ChangePlan, *, warnings: list[str] | None = None) -> dict:
    """Store a drafted plan and return its preview, with the draft's warnings."""
    from adloop.safety.preview import store_plan

    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def blocked_plan_refusal(config: AdLoopConfig, plan: ChangePlan, *, dry_run: bool) -> dict | None:
    """confirm_and_apply's refusal for a plan whose operation is blocked now.

    The blocklist can grow after a plan was drafted, so it is checked again
    before a dry run or an apply. The refusal is logged like any other
    outcome; the plan stays pending, so it can still be discarded or applied
    once the operation is unblocked.
    """
    error = blocked_operation_error(plan.operation, config.safety)
    if error is None:
        return None
    from adloop.safety.audit import log_mutation

    log_mutation(
        config.safety.log_file,
        operation=plan.operation,
        customer_id=plan.customer_id,
        entity_type=plan.entity_type,
        entity_id=plan.entity_id,
        changes=plan.changes,
        dry_run=dry_run,
        result="blocked",
        error=error["error"],
    )
    return {
        "status": "BLOCKED",
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "error": error["error"],
        "message": (
            "Nothing was changed: this operation was added to safety.blocked_operations "
            "after the plan was drafted, so it can neither be tested nor applied."
        ),
    }
