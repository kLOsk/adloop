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
    # When a confirmation dialog for this plan was last sent and not yet
    # answered (ISO timestamp, "" when none is open). An app that receives
    # the dialog but never answers it would otherwise leave every retry
    # waiting until its own timeout; see approval.confirm_in_dialog.
    dialog_requested_at: str = ""

    # Payload the applier needs but a preview must never show: an upload's rows
    # (the caller id cannot be hashed, so call uploads carry raw phone numbers),
    # or a row blob too bulky to be useful in a model's context. It is a
    # separate field rather than a key inside ``changes`` so that "what may be
    # shown" is decided in one place by construction, not by filtering each
    # surface — the previous attempt at that filtering is exactly what let raw
    # numbers reach a preview.
    #
    # A plan store must round-trip this field; a store that drops it makes the
    # applier refuse loudly instead of uploading nothing quietly.
    # `repr=False`: the payload holds raw caller ids, so a plan that ends up in
    # a log line or a traceback must not print it.
    apply_only_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def apply_payload(self) -> dict[str, Any]:
        """What the applier gets: ``changes`` with the apply-only payload merged in.

        Keys of ``changes`` win on collision — the apply-only payload is the
        data the summary in ``changes`` describes, never the other way round.
        """
        payload = dict(self.apply_only_payload)
        payload.update(self.changes)
        return payload

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

    def claim(self, tenant: str, plan_id: str) -> ChangePlan | None:
        """Take the plan out of the store in one atomic step.

        An apply that is already running must not be started a second time: the
        client times out, the model confirms again, and the upload goes out
        twice. Claiming before the first request makes the second call find
        nothing; a failure that provably happened before anything was sent can
        put the plan back.
        """
        ...


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

    def claim(self, tenant: str, plan_id: str) -> ChangePlan | None:
        with self._lock:
            return self._plans.pop((tenant, plan_id), None)


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


_FALLBACK_WARNED = False


def claim_plan(plan_id: str) -> ChangePlan | None:
    """Atomically take a plan out of the store (see ``PlanStore.claim``).

    ``claim`` is what a store should implement; a store that predates it gets
    the non-atomic get-then-remove fallback rather than an AttributeError.

    **The fallback is not safe against overlapping confirmations**: two calls
    that interleave between ``get`` and ``remove`` both receive the plan and
    both upload. It exists so an older custom store keeps working at all; the
    hosted store implements ``claim``. The first use of the fallback logs a
    warning once, so a deployment can see that it is running without the real
    guarantee.
    """
    global _FALLBACK_WARNED

    from adloop.runtime import current_tenant

    tenant = current_tenant()
    store = _active_store
    claim = getattr(store, "claim", None)
    if claim is not None:
        return claim(tenant, plan_id)
    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        import logging

        logging.getLogger(__name__).warning(
            "Plan store %s has no claim(); falling back to get-then-remove, "
            "which cannot stop two overlapping confirmations from both "
            "uploading. Implement claim() on the store.",
            type(store).__name__,
        )
    plan = store.get(tenant, plan_id)
    if plan is not None:
        store.remove(tenant, plan_id)
    return plan
