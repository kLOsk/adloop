"""Roku Ads write tools — the only object Roku's API (beta) lets anyone create.

Roku's Ads API documents no campaign, ad group or creative writes yet (its
introduction lists "Create and edit campaigns and ad groups" and "View,
create, and edit creatives" as in development). What it does accept is
``POST /developer/audiences``: custom audiences built from pixel events or
from an uploaded customer file. AdLoop drafts **event-based** audiences
(people whose households fired chosen events within a lookback window);
customer files are personal data, which AdLoop does not upload (the same
rule as Reddit).

Same gate as every other platform: draft → preview (platform ``roku``,
operation ``roku_create_audience``) → ``confirm_and_apply``. There is no
validate-only mode, so the dry run re-reads the event group and the
account's audiences; the apply runs that preflight again first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adloop.roku_ads import client as _client
from adloop.roku_ads.auth import RokuAdsApiError
from adloop.roku_ads.read import _attrs, account_meta, load_event_groups

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

PLATFORM = "roku"
OPERATION_PREFIX = "roku_"
# Event names an event audience can qualify on (Roku's snake_case names).
AUDIENCE_EVENTS = (
    "achieve_level", "add_payment_info", "add_to_cart", "add_to_wishlist", "app_install",
    "complete_registration", "contact", "customize_product", "donate", "download",
    "find_location", "first_view_video", "initiate_checkout", "lead", "page_view", "purchase",
    "qss", "schedule", "search", "session_start", "sign_up", "start_trial", "subscribe",
    "subscription_cancellation", "subscription_renewal", "unlock_achievement", "view_content",
)
_MAX_LOOKBACK_DAYS = 90


def _validation_error(details: list[str]) -> dict:
    return {"error": "Validation failed", "details": details}


def _guard(operation: str, config: AdLoopConfig) -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def _custom_audience_names(config: AdLoopConfig, account: str) -> dict[str, list[str]]:
    """Audience uids per lower-cased name."""
    items, _ = _client.api_get_all(config, "audiences/custom", {"owner_uid": [account]})
    names: dict[str, list[str]] = {}
    for a in items:
        names.setdefault(str(_attrs(a).get("name") or "").strip().lower(), []).append(str(a.get("uid")))
    return names


def draft_roku_event_audience(
    config: AdLoopConfig,
    *,
    account_uid: str = "",
    name: str = "",
    event_group_uid: str = "",
    event_names: list[str] | None = None,
    lookback_days: int = 30,
) -> dict:
    """Draft a custom audience of households that fired the events within the lookback."""
    operation = "roku_create_audience"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    name = (name or "").strip()
    if len(name) < 3:
        errors.append("name must be at least 3 characters.")
    events = [str(e).strip().lower() for e in (event_names or []) if str(e).strip()]
    if not events:
        errors.append("event_names needs at least one event, e.g. ['page_view'] or ['lead'].")
    unknown = [e for e in events if e not in AUDIENCE_EVENTS]
    if unknown:
        errors.append(f"Unknown event name(s) {unknown}; Roku's events are {list(AUDIENCE_EVENTS)}.")
    try:
        lookback = int(lookback_days)
    except (TypeError, ValueError):
        lookback = 0
    if not 1 <= lookback <= _MAX_LOOKBACK_DAYS:
        errors.append(f"lookback_days must be 1–{_MAX_LOOKBACK_DAYS}.")
    if errors:
        return _validation_error(errors)

    meta = account_meta(config, account_uid)
    account = meta["account_uid"]
    groups = {str(e.get("uid")): e for e in load_event_groups(config, account)}
    if not groups:
        return {"error": "The account has no event group (Roku pixel), so there are no events to build an audience from."}
    event_group_uid = (event_group_uid or "").strip()
    if not event_group_uid:
        defaults = [uid for uid, e in groups.items() if _attrs(e).get("is_default")]
        if len(defaults) != 1 and len(groups) != 1:
            return _validation_error([f"event_group_uid is required: one of {sorted(groups)}."])
        event_group_uid = defaults[0] if len(defaults) == 1 else next(iter(groups))
    group = groups.get(event_group_uid)
    if group is None:
        return {"error": f"Event group '{event_group_uid}' is not in account {account} (see get_roku_conversion_setup)."}
    attrs = _attrs(group)
    warnings: list[str] = []
    if attrs.get("is_archived"):
        return {"error": f"Event group '{attrs.get('name')}' is archived."}
    if attrs.get("status") in ("stale", "inactive"):
        warnings.append(f"Event group '{attrs.get('name')}' is {attrs.get('status')}: the audience will fill slowly or not at all.")
    existing = _custom_audience_names(config, account)
    same_name = existing.get(name.lower(), [])
    if same_name:
        warnings.append(f"An audience named '{name}' already exists ({', '.join(same_name)}); this creates another one.")
    warnings.append(
        "Roku shows a new event audience as pending for about 72 hours. Attaching it to a campaign "
        "happens in Ads Manager: Roku's API cannot edit campaigns yet."
    )
    body = {"data": [{"type": "audience", "attributes": {
        "name": name,
        "account_uid": account,
        "audiences_config": [{"type": "event_audiences", "attributes": [
            {"key": "event_group_uid", "value": event_group_uid},
            {"key": "qualified_event_timeframe_value", "value": lookback},
            {"key": "qualified_event_timeframe_unit", "value": "day"},
            {"key": "event_name", "value": events},
        ]}],
    }}]}

    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(
        platform=PLATFORM,
        operation=operation,
        entity_type="audience",
        entity_id="",
        customer_id=account,
        changes={
            "account_uid": account,
            "entity_name": name,
            "event_group": attrs.get("name"),
            "event_group_uid": event_group_uid,
            "event_group_status": attrs.get("status"),
            "events": events,
            "lookback_days": lookback,
            "body": body,
            # Audiences with this name when drafted: one appearing later was
            # created after the preview (an earlier attempt whose response was lost).
            "same_name_at_draft": same_name,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def is_roku_plan(plan: ChangePlan) -> bool:
    return str(plan.operation or "").startswith(OPERATION_PREFIX)


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read the event group; sends nothing."""
    changes = plan.changes or {}
    if plan.operation != "roku_create_audience":
        raise ValueError(f"Unknown Roku operation: {plan.operation}")
    account = changes.get("account_uid") or plan.customer_id
    groups = {str(e.get("uid")): e for e in load_event_groups(config, account)}
    group = groups.get(str(changes.get("event_group_uid")))
    if group is None or _attrs(group).get("is_archived"):
        raise ValueError(f"Event group '{changes.get('event_group_uid')}' is gone or archived.")
    now = _custom_audience_names(config, account).get(str(changes.get("entity_name") or "").lower(), [])
    new = [uid for uid in now if uid not in (changes.get("same_name_at_draft") or [])]
    if new:
        raise ValueError(
            f"An audience named '{changes.get('entity_name')}' was created after the preview ({', '.join(new)}); "
            "an earlier attempt probably went through. Check get_roku_audiences before drafting again."
        )
    return {"account_uid": account, "event_group": _attrs(group).get("name"),
            "event_group_status": _attrs(group).get("status"), "audience_name": changes.get("entity_name")}


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Create the audience. Called by ``confirm_and_apply`` after the gate."""
    from adloop.safety.outcomes import PartialApplyError

    preflight(config, plan)
    changes = plan.changes or {}
    try:
        created = _client.api_post(config, "audiences", changes["body"])
    except RokuAdsApiError as exc:
        if exc.status == 0 or exc.status >= 500:
            # The request may have reached Roku: whether the audience exists is
            # unknown, and Roku offers no idempotency, so never replay it.
            raise PartialApplyError(
                f"Roku did not confirm the audience ({exc}); it may or may not exist. Check "
                "get_roku_audiences before drafting it again.",
                unknown=True,
            ) from None
        raise
    data: Any = created.get("data")
    audience = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
    return {
        "entity_type": "audience",
        "audience_uid": audience.get("uid"),
        "name": _attrs(audience).get("name") or changes.get("entity_name"),
        "note": "Pending for about 72 hours; attach it to a campaign in Roku Ads Manager.",
    }
