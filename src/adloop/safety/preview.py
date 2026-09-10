"""Change preview formatting — structured output for proposed mutations."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass
class ChangePlan:
    """A proposed change that must be confirmed before execution."""

    plan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    operation: str = ""
    entity_type: str = ""
    entity_id: str = ""
    customer_id: str = ""
    changes: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    requires_double_confirm: bool = False
    dry_run_result: dict[str, Any] | None = None
    # Ad platform the plan targets: "" (Google, the default so stored plans
    # from before the field existed still load) or "reddit".
    platform: str = ""

    def to_preview(self) -> dict[str, Any]:
        """Format as a human-readable preview dict for the AI to present."""
        return {
            "plan_id": self.plan_id,
            "platform": self.platform or "google",
            "operation": self.operation,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "customer_id": self.customer_id,
            "changes": self.changes,
            "requires_double_confirm": self.requires_double_confirm,
            "status": "PENDING_CONFIRMATION",
            "instructions": (
                "Review the changes above. To apply, call confirm_and_apply "
                f"with plan_id='{self.plan_id}' and dry_run=false."
            ),
        }


class PlanStore(Protocol):
    """Storage for pending plans between draft and confirm_and_apply.

    Every operation is scoped by tenant so that in a multi-tenant server
    one tenant can never look up — let alone apply — another tenant's
    pending mutation, even with a known plan_id.

    ``store`` MUST overwrite an existing plan with the same
    ``(tenant, plan_id)``: confirm_and_apply re-stores a plan after a
    dry-run pass to persist ``dry_run_result``, which two-phase apply
    checks before allowing a real write.
    """

    def store(self, tenant: str, plan: ChangePlan) -> None: ...

    def get(self, tenant: str, plan_id: str) -> ChangePlan | None: ...

    def remove(self, tenant: str, plan_id: str) -> None: ...


class InMemoryPlanStore:
    """Default store: plans live in process memory and expire on restart."""

    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], ChangePlan] = {}
        self._lock = threading.Lock()

    def store(self, tenant: str, plan: ChangePlan) -> None:
        with self._lock:
            self._plans[(tenant, plan.plan_id)] = plan

    def get(self, tenant: str, plan_id: str) -> ChangePlan | None:
        with self._lock:
            return self._plans.get((tenant, plan_id))

    def remove(self, tenant: str, plan_id: str) -> None:
        with self._lock:
            self._plans.pop((tenant, plan_id), None)


_active_store: PlanStore = InMemoryPlanStore()


def set_plan_store(store: PlanStore) -> None:
    """Swap the plan store (the hosted server plugs in a persistent one)."""
    global _active_store
    _active_store = store


def get_plan_store() -> PlanStore:
    return _active_store


def store_plan(plan: ChangePlan) -> None:
    """Store a plan for later retrieval by confirm_and_apply."""
    from adloop.runtime import current_tenant

    _active_store.store(current_tenant(), plan)


def get_plan(plan_id: str) -> ChangePlan | None:
    """Retrieve a stored plan by ID (scoped to the current tenant)."""
    from adloop.runtime import current_tenant

    return _active_store.get(current_tenant(), plan_id)


def remove_plan(plan_id: str) -> None:
    """Remove a plan after execution."""
    from adloop.runtime import current_tenant

    _active_store.remove(current_tenant(), plan_id)
