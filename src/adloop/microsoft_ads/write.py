"""Microsoft Advertising write tools — draft → preview → (dry run) → apply.

Same gate as Google Ads and Reddit writes: a draft function validates the
request, runs the safety guards, stores a :class:`ChangePlan` and returns
its preview; ``confirm_and_apply`` recognises the ``microsoft_`` prefix and
hands the plan to :func:`preflight` (dry run) or :func:`apply_plan`.

Microsoft's Campaign Management API has no validate-only mode, so the dry
run is a read-side preflight: it re-reads the target, confirms it still
exists in the state the preview showed, and re-runs the budget cap. Nothing
is sent. The real apply runs the same preflight first, so a change made in
Microsoft Advertising between the preview (or the dry run) and the apply is
refused instead of overwritten. Updates send only the fields that change;
Microsoft leaves every field it does not receive as it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.auth import MicrosoftAdsApiError
from adloop.microsoft_ads.client import campaign_request, fault_details
from adloop.microsoft_ads.read import (
    _ad_groups,
    _ads,
    _campaigns,
    _customer,
    _keywords,
    _money,
    account_meta,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

PLATFORM = "microsoft"
OPERATION_PREFIX = "microsoft_"
ENTITY_TYPES = ("campaign", "ad_group", "keyword", "ad")
_STATUS = {"pause": "Paused", "enable": "Active"}


def _validation_error(details: list[str]) -> dict:
    return {"error": "Validation failed", "details": details}


def _guard(operation: str, config: AdLoopConfig) -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


def _store(plan_kwargs: dict, *, warnings: list[str] | None = None) -> dict:
    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(platform=PLATFORM, **plan_kwargs)
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def find_entity(
    config: AdLoopConfig, account: str, customer: str, entity_type: str, entity_id: str,
    *, parent_id: str = "",
) -> dict | None:
    """Locate an entity and its parents; ad groups, keywords and ads carry no parent id."""
    entity_id = str(entity_id)
    for c in _campaigns(config, account, customer):
        cid = str(c.get("Id"))
        if entity_type == "campaign":
            if cid == entity_id:
                return {"entity": c, "campaign": c}
            continue
        if entity_type == "ad_group" and parent_id and cid != str(parent_id):
            continue
        for g in _ad_groups(config, account, customer, cid):
            gid = str(g.get("Id"))
            if entity_type == "ad_group":
                if gid == entity_id:
                    return {"entity": g, "campaign": c, "ad_group": g}
                continue
            if parent_id and gid != str(parent_id):
                continue
            children = (_keywords if entity_type == "keyword" else _ads)(config, account, customer, gid)
            for child in children:
                if str(child.get("Id")) == entity_id:
                    return {"entity": child, "campaign": c, "ad_group": g}
    return None


def _label(found: dict, entity_type: str) -> str:
    entity = found["entity"]
    if entity_type == "keyword":
        return f"{entity.get('Text')} [{entity.get('MatchType')}]"
    if entity_type == "ad":
        return f"{entity.get('Type')} ad {entity.get('Id')}"
    return str(entity.get("Name"))


def draft_microsoft_status(
    config: AdLoopConfig,
    *,
    action: str,
    ad_account_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
    parent_id: str = "",
) -> dict:
    """Draft pausing or enabling a campaign, ad group, keyword or ad."""
    operation = "microsoft_set_status"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    entity_type = (entity_type or "").strip().lower()
    entity_id = str(entity_id or "").strip()
    errors = []
    if entity_type not in ENTITY_TYPES:
        errors.append(f"entity_type must be one of {list(ENTITY_TYPES)}, got '{entity_type}'")
    if not entity_id:
        errors.append("entity_id is required")
    if errors:
        return _validation_error(errors)
    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    found = find_entity(config, account, customer, entity_type, entity_id, parent_id=parent_id)
    if not found:
        return {"error": f"Microsoft {entity_type.replace('_', ' ')} '{entity_id}' was not found in account {account}."}

    target = _STATUS[action]
    entity = found["entity"]
    current = entity.get("Status")
    warnings = []
    if current == target:
        warnings.append(f"It is already {target}; applying changes nothing.")
    if target == "Active":
        for level in ("campaign", "ad_group"):
            parent = found.get(level)
            if parent is not None and parent is not entity and parent.get("Status") != "Active":
                warnings.append(
                    f"Its {level.replace('_', ' ')} '{parent.get('Name')}' is {parent.get('Status')}, "
                    "so it still will not serve."
                )
        if entity.get("EditorialStatus") in ("Disapproved", "Inactive"):
            warnings.append(f"Editorial status is {entity.get('EditorialStatus')}: it will not serve until fixed.")
    if target == "Paused" and entity_type == "campaign":
        warnings.append("Pausing the campaign stops every ad in it.")

    changes = {
        "ad_account_id": account,
        "customer_id": customer,
        "name": _label(found, entity_type),
        "entity_name": _label(found, entity_type),
        "campaign_id": str(found["campaign"].get("Id")),
        "campaign_name": found["campaign"].get("Name"),
        "ad_group_id": str(found["ad_group"].get("Id")) if found.get("ad_group") else None,
        "ad_group_name": found["ad_group"].get("Name") if found.get("ad_group") else None,
        "ad_type": entity.get("Type") if entity_type == "ad" else None,
        "current_status": current,
        "target_status": target,
    }
    return _store(
        {"operation": operation, "entity_type": entity_type, "entity_id": entity_id,
         "customer_id": account, "changes": changes},
        warnings=warnings,
    )


def draft_microsoft_campaign_budget(
    config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "", daily_budget: float = 0.0,
) -> dict:
    """Draft a new daily budget for one campaign (its own budget, not a shared one)."""
    from adloop.safety.guards import SafetyViolation, check_budget_cap

    operation = "microsoft_update_campaign_budget"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors = []
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    try:
        amount = round(float(daily_budget), 2)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        errors.append("daily_budget must be a positive amount in the account currency")
    if errors:
        return _validation_error(errors)
    try:
        check_budget_cap(amount, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    found = find_entity(config, account, customer, "campaign", campaign_id)
    if not found:
        return {"error": f"Microsoft campaign '{campaign_id}' was not found in account {account}."}
    campaign = found["entity"]
    budget_id = campaign.get("BudgetId")
    if budget_id and str(budget_id) not in ("0", "None"):
        return {
            "error": (
                f"Campaign '{campaign.get('Name')}' uses shared budget {budget_id}; its daily amount "
                "is set on that budget and changing it changes every campaign sharing it. Change "
                "it in Microsoft Advertising (Shared library → Budgets)."
            )
        }
    current = _money(campaign.get("DailyBudget"))
    warnings = []
    if current is not None and current == amount:
        warnings.append("The budget is already this amount; applying changes nothing.")
    if current and amount > current * 2:
        warnings.append(f"This more than doubles the budget ({current} → {amount} {meta['currency']}).")
    warnings.append(
        f"Microsoft can spend up to 30.4 × the daily budget in a month: about "
        f"{round(amount * 30.4, 2)} {meta['currency']}."
    )
    return _store(
        {
            "operation": operation,
            "entity_type": "campaign",
            "entity_id": campaign_id,
            "customer_id": account,
            "changes": {
                "ad_account_id": account,
                "customer_id": customer,
                "entity_name": campaign.get("Name"),
                "campaign_name": campaign.get("Name"),
                "campaign_status": campaign.get("Status"),
                "current_daily_budget": current,
                "new_daily_budget": amount,
                "currency": meta["currency"],
            },
        },
        warnings=warnings,
    )


def is_microsoft_plan(plan: ChangePlan) -> bool:
    return str(plan.operation or "").startswith(OPERATION_PREFIX)


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read the target and re-check the gates; sends nothing."""
    from adloop.microsoft_ads import manage
    from adloop.safety.guards import check_budget_cap

    if plan.operation in manage.OPERATIONS:
        return manage.preflight(config, plan)
    changes = plan.changes or {}
    account = changes.get("ad_account_id") or plan.customer_id
    customer = changes.get("customer_id") or ""
    parent = changes.get("ad_group_id") if plan.entity_type in ("keyword", "ad") else ""
    found = find_entity(config, account, customer, plan.entity_type, plan.entity_id, parent_id=parent or "")
    if not found:
        raise ValueError(f"{plan.entity_type} '{plan.entity_id}' no longer exists.")
    entity = found["entity"]
    checks: dict[str, Any] = {"ad_account_id": account, "entity": changes.get("name") or entity.get("Name")}
    if plan.operation == "microsoft_set_status":
        now = entity.get("Status")
        if now != changes.get("current_status"):
            raise ValueError(
                f"The status changed since the draft ({changes.get('current_status')} → {now}); redraft."
            )
        checks["status_now"] = now
        checks["already_in_target_status"] = now == changes.get("target_status")
    elif plan.operation == "microsoft_update_campaign_budget":
        budget_id = entity.get("BudgetId")
        if budget_id and str(budget_id) not in ("0", "None"):
            raise ValueError("The campaign moved to a shared budget since the draft; redraft.")
        now = _money(entity.get("DailyBudget"))
        if now != changes.get("current_daily_budget"):
            raise ValueError(
                f"The budget changed since the draft ({changes.get('current_daily_budget')} → {now}); redraft."
            )
        check_budget_cap(float(changes["new_daily_budget"]), config.safety)
        checks["budget_now"] = now
        checks["budget_cap_ok"] = True
    else:
        raise ValueError(f"Unknown Microsoft operation: {plan.operation}")
    return checks


def _raise_partial(payload: dict, what: str) -> None:
    partial = [e for e in payload.get("PartialErrors") or [] if e]
    if partial:
        _codes, message, _tracking = fault_details({"PartialErrors": partial})
        raise MicrosoftAdsApiError(f"Microsoft refused the {what}: {message or partial}")


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a Microsoft plan. Called by ``confirm_and_apply`` after the gate.

    Re-runs :func:`preflight` first: a dry run can be minutes or hours old,
    and the plan must still describe the live entity when it is applied.
    """
    from adloop.microsoft_ads import manage

    preflight(config, plan)
    if plan.operation in manage.OPERATIONS:
        return manage.apply_plan(config, plan)
    changes = plan.changes or {}
    account = changes.get("ad_account_id") or plan.customer_id
    customer = changes.get("customer_id") or ""
    kwargs = {"account_id": account, "customer_id": customer, "method": "PUT", "mutation": True}

    if plan.operation == "microsoft_update_campaign_budget":
        payload = campaign_request(
            config, "Campaigns",
            {"AccountId": account,
             "Campaigns": [{"Id": plan.entity_id, "DailyBudget": changes["new_daily_budget"]}]},
            **kwargs,
        )
        _raise_partial(payload, "budget change")
        return {"entity_type": "campaign", "entity_id": plan.entity_id,
                "campaign_name": changes.get("campaign_name"),
                "daily_budget": changes["new_daily_budget"], "currency": changes.get("currency")}

    if plan.operation == "microsoft_set_status":
        status = changes["target_status"]
        etype, eid = plan.entity_type, plan.entity_id
        if etype == "campaign":
            body = {"AccountId": account, "Campaigns": [{"Id": eid, "Status": status}]}
            path = "Campaigns"
        elif etype == "ad_group":
            body = {"CampaignId": changes["campaign_id"], "AdGroups": [{"Id": eid, "Status": status}]}
            path = "AdGroups"
        elif etype == "keyword":
            body = {"AdGroupId": changes["ad_group_id"], "Keywords": [{"Id": eid, "Status": status}]}
            path = "Keywords"
        else:
            body = {"AdGroupId": changes["ad_group_id"],
                    "Ads": [{"Id": eid, "Status": status, "Type": changes.get("ad_type")}]}
            path = "Ads"
        payload = campaign_request(config, path, body, **kwargs)
        _raise_partial(payload, "status change")
        return {"entity_type": etype, "entity_id": eid, "name": changes.get("name"), "status": status}

    raise ValueError(f"Unknown Microsoft operation: {plan.operation}")
