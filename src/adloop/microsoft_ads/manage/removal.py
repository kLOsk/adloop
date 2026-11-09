"""Deleting entities, negatives, criteria and extension links."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.manage._common import (
    _LIST_TYPE,
    Handler,
    Step,
    _call,
    _ctx,
    _plan,
    _Progress,
)
from adloop.microsoft_ads.manage.extensions import _ASSOCIATION_ENTITY
from adloop.microsoft_ads.manage.targeting import _block_label
from adloop.microsoft_ads.read import (
    is_negative,
    target_criterions,
)
from adloop.microsoft_ads.write import _blocked as _guard
from adloop.microsoft_ads.write import (
    _validation_error,
    find_entity,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

REMOVABLE = ("campaign", "ad_group", "ad", "keyword", "negative_keyword", "negative_list_item",
             "campaign_criterion", "extension_association")


# Criterion types a campaign_criterion remove_id can point at, looked up in
# this order (one query each, until the criterion is found).
_CRITERION_KINDS = ("Location", "Radius", "DayTime", "LocationIntent", "Age", "Gender", "Device")


def _find_criterion(by_kind: dict[str, list[dict]], criterion_id: str) -> dict | None:
    """The criterion with this id among a campaign's target criteria, with the others of its kind."""
    for kind, rows in by_kind.items():
        for row in rows:
            if str(row.get("Id")) == str(criterion_id):
                return {"criterion": row, "kind": kind, "siblings": rows}
    return None


def _criterion_label(row: dict, kind: str) -> str:
    crit = row.get("Criterion") or {}
    side = "excluded" if is_negative(row) else "targeted"
    if kind == "Location":
        return f"location {crit.get('DisplayName') or crit.get('LocationId')} ({side})"
    if kind == "Radius":
        return f"radius {crit.get('Name') or ''} {crit.get('Radius')} {crit.get('RadiusUnit') or ''} ({side})".replace("  ", " ")
    if kind == "DayTime":
        return f"ad schedule window {_block_label(crit)}"
    if kind == "LocationIntent":
        return f"location intent {crit.get('IntentOption')}"
    return f"{kind.lower()} criterion {row.get('Id')}"


def _positive_locations(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not is_negative(r)]


def _criterion_refusal(campaign_name: str, found: dict, by_kind: dict[str, list[dict]]) -> str | None:
    """Why removing this criterion must go through update_microsoft_campaign_targeting, or None."""
    if found["kind"] not in ("Location", "Radius") or is_negative(found["criterion"]):
        return None
    positives = _positive_locations(by_kind.get("Location", []) + by_kind.get("Radius", []))
    others = [r for r in positives if str(r.get("Id")) != str(found["criterion"].get("Id"))]
    if others:
        return None
    return (f"This is the last targeted location of campaign '{campaign_name}': without it the campaign "
            "serves everywhere. Use update_microsoft_campaign_targeting with the full location list instead.")


def remove_microsoft_entity(config: AdLoopConfig, *, ad_account_id: str = "", entity_type: str = "", entity_id: str = "") -> dict:
    """Draft deleting an entity (irreversible; double confirmation)."""
    operation = "microsoft_remove_entity"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    entity_type = (entity_type or "").strip().lower()
    entity_id = str(entity_id or "").strip()
    if entity_type not in REMOVABLE:
        return _validation_error([f"entity_type must be one of {list(REMOVABLE)}."])
    if not entity_id:
        return _validation_error(["entity_id is required."])
    meta, account, customer = _ctx(config, ad_account_id)
    shown: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    request: dict[str, Any]
    warnings: list[str] = []
    if entity_type in ("campaign", "ad_group", "ad", "keyword"):
        found = find_entity(config, account, customer, entity_type, entity_id)
        if not found:
            return {"error": f"Microsoft {entity_type.replace('_', ' ')} '{entity_id}' was not found."}
        entity = found["entity"]
        label = entity.get("Name") or entity.get("Text") or f"{entity.get('Type')} ad {entity_id}"
        shown = {"name": label, "campaign_name": found["campaign"].get("Name") if entity_type != "campaign" else None,
                 "ad_group_name": (found.get("ad_group") or {}).get("Name") if entity_type in ("ad", "keyword") else None,
                 "current_status": entity.get("Status")}
        payload = {"campaign_id": str(found["campaign"].get("Id")),
                   "ad_group_id": str((found.get("ad_group") or {}).get("Id") or "")}
        if entity_type == "campaign":
            request = {"path": "Campaigns", "body": {"AccountId": account, "CampaignIds": [entity_id]}}
        elif entity_type == "ad_group":
            request = {"path": "AdGroups", "body": {"CampaignId": str(found["campaign"].get("Id")), "AdGroupIds": [entity_id]}}
        elif entity_type == "ad":
            request = {"path": "Ads", "body": {"AdGroupId": str(found["ad_group"].get("Id")), "AdIds": [entity_id]}}
        else:
            request = {"path": "Keywords", "body": {"AdGroupId": str(found["ad_group"].get("Id")), "KeywordIds": [entity_id]}}
        warnings.append("Deleting cannot be undone; pausing keeps the history and lets it come back.")
    else:
        parts = entity_id.split("~")
        if entity_type == "negative_keyword" and len(parts) == 3 and parts[0] in ("Campaign", "AdGroup"):
            kind, owner, neg = parts
            request = {"path": "EntityNegativeKeywords", "body": {"EntityNegativeKeywords": [
                {"EntityId": int(owner), "EntityType": kind, "NegativeKeywords": [{"Id": int(neg)}]}]}}
            shown = {"negative_keyword_id": neg, "on": f"{kind} {owner}"}
        elif entity_type == "negative_list_item" and len(parts) == 2:
            list_id, item = parts
            request = {"path": "ListItems", "body": {"ListItemIds": [int(item)], "SharedList": {"Type": _LIST_TYPE, "Id": int(list_id)},
                                                     "SharedEntityScope": "Account"}}
            shown = {"list_id": list_id, "item_id": item}
        elif entity_type == "campaign_criterion" and len(parts) == 2 and all(x.isdigit() for x in parts):
            cid, crit = parts
            campaign = find_entity(config, account, customer, "campaign", cid)
            if not campaign:
                return {"error": f"Microsoft campaign '{cid}' was not found in account {account}."}
            name = campaign["entity"].get("Name")
            by_kind = target_criterions(config, account, customer, cid, _CRITERION_KINDS)
            found = _find_criterion(by_kind, crit)
            if not found:
                return {"error": f"Campaign '{name}' has no target criterion {crit} (get_microsoft_campaigns lists them)."}
            refusal = _criterion_refusal(name, found, by_kind)
            if refusal:
                return _validation_error([refusal])
            label = _criterion_label(found["criterion"], found["kind"])
            shown = {"name": label, "campaign_name": name, "criterion_type": found["kind"]}
            payload = {"campaign_id": cid, "criterion_kind": found["kind"]}
            request = {"path": "CampaignCriterions", "body": {"CampaignCriterionIds": [int(crit)], "CampaignId": int(cid),
                                                              "CriterionType": "Targets"}}
            if found["kind"] == "LocationIntent":
                warnings.append("Without its location intent the campaign falls back to Microsoft's default, "
                                "which also reaches people outside the locations who search for them.")
            if found["kind"] == "DayTime" and len(found["siblings"]) == 1:
                warnings.append("This is the campaign's last ad schedule window: without it the campaign serves at all hours.")
        elif entity_type == "extension_association" and len(parts) == 3 and parts[2] in _ASSOCIATION_ENTITY:
            ext, owner, association = parts
            request = {"path": "AdExtensionsAssociations", "body": {"AccountId": account, "AssociationType": association,
                                                                     "AdExtensionIdToEntityIdAssociations": [
                                                                         {"AdExtensionId": int(ext), "EntityId": int(owner)}]}}
            shown = {"extension_id": ext, "removed_from": f"{association} {owner}"}
            warnings.append("This unlinks the extension only; the extension (and an image extension's images) "
                            "stays in the account's library.")
        else:
            return _validation_error([
                "entity_id format: negative_keyword 'Campaign~<id>~<negativeId>' or 'AdGroup~<id>~<negativeId>', "
                "negative_list_item '<listId>~<itemId>', campaign_criterion '<campaignId>~<criterionId>', "
                "extension_association '<extensionId>~<entityId>~<Account|Campaign|AdGroup>' (the remove_id values the read "
                "tools return)."
            ])
    return _plan(operation, entity_type, entity_id, account, customer,
                 {"entity_name": shown.pop("name", None) or entity_id, **shown},
                 {**payload, "request": request}, warnings=warnings, double=True)


def _check_remove_entity(s: Step) -> None:
    config, plan, account, customer = s.config, s.plan, s.account, s.customer
    if plan.entity_type in ("campaign", "ad_group", "ad", "keyword"):
        s.entity(plan.entity_type, plan.entity_id)
    elif plan.entity_type == "campaign_criterion":
        cid, crit = plan.entity_id.split("~")
        name = s.entity("campaign", cid)["entity"].get("Name")
        by_kind = target_criterions(config, account, customer, cid, _CRITERION_KINDS)
        found = _find_criterion(by_kind, crit)
        if not found:
            raise ValueError(f"Criterion {crit} is no longer on campaign '{name}'.")
        refusal = _criterion_refusal(name, found, by_kind)
        if refusal:
            raise ValueError(refusal)


def _apply_remove_entity(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    request = c["request"]
    _call(config, c, "DELETE", request["path"], request["body"], f"{plan.entity_type.replace('_', ' ')} removal")
    return {"removed": plan.entity_type, "entity_id": plan.entity_id}


HANDLERS = {
    "microsoft_remove_entity": Handler(_check_remove_entity, _apply_remove_entity),
}
