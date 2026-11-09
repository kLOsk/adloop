"""Negative keywords: direct ones and shared negative keyword lists."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.microsoft_ads.client import campaign_request
from adloop.microsoft_ads.manage._common import (
    _LIST_TYPE,
    _MAX_LISTS_PER_CAMPAIGN,
    NEGATIVE_MATCH_TYPES,
    Handler,
    Step,
    _call,
    _chunks,
    _clean,
    _ctx,
    _plan,
    _Progress,
)
from adloop.microsoft_ads.read import (
    _ad_groups,
    _campaigns,
    _keywords,
)
from adloop.microsoft_ads.write import _blocked as _guard
from adloop.microsoft_ads.write import (
    _validation_error,
    find_entity,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def _negative_rows(keywords: list, match_type: str, errors: list[str]) -> list[dict]:
    match = (match_type or "Phrase").strip().title()
    if match not in NEGATIVE_MATCH_TYPES:
        errors.append(f"match_type for negatives must be one of {list(NEGATIVE_MATCH_TYPES)}.")
    rows, seen = [], set()
    for text in _clean(keywords):
        if len(text) > 100:
            errors.append(f"Negative '{text[:20]}…' is longer than 100 characters.")
        if text.lower() not in seen:
            seen.add(text.lower())
            rows.append({"Text": text, "MatchType": match})
    if not rows and not errors:
        errors.append("keywords needs at least one negative keyword.")
    return rows


def _entity_negatives(config: AdLoopConfig, account: str, customer: str, entity_type: str, entity_id: str,
                      parent_id: str = "") -> list[dict]:
    payload = campaign_request(
        config, "NegativeKeywords/QueryByEntityIds",
        {"EntityIds": [int(entity_id)], "EntityType": entity_type, "ParentEntityId": int(parent_id) if parent_id else None},
        account_id=account, customer_id=customer,
    )
    out = []
    for entry in payload.get("EntityNegativeKeywords") or []:
        out.extend(n for n in (entry or {}).get("NegativeKeywords") or [] if isinstance(n, dict))
    return out


def _shared_lists(config: AdLoopConfig, account: str, customer: str) -> list[dict]:
    payload = campaign_request(
        config, "SharedEntities/Query", {"SharedEntityType": _LIST_TYPE, "SharedEntityScope": "Account"},
        account_id=account, customer_id=customer,
    )
    return [e for e in payload.get("SharedEntities") or [] if isinstance(e, dict)]


def _list_items(config: AdLoopConfig, account: str, customer: str, list_id: str) -> list[dict]:
    payload = campaign_request(
        config, "ListItems/QueryBySharedList",
        {"SharedList": {"Type": _LIST_TYPE, "Id": int(list_id)}, "SharedEntityScope": "Account"},
        account_id=account, customer_id=customer,
    )
    return [i for i in payload.get("ListItems") or [] if isinstance(i, dict)]


def _list_associations(config: AdLoopConfig, account: str, customer: str, campaign_ids: list[str]) -> list[dict]:
    out = []
    for chunk in _chunks([int(c) for c in campaign_ids]):
        payload = campaign_request(
            config, "SharedEntityAssociations/QueryByEntityIds",
            {"EntityIds": chunk, "EntityType": "Campaign", "SharedEntityType": _LIST_TYPE, "SharedEntityScope": "Account"},
            account_id=account, customer_id=customer,
        )
        out.extend(a for a in payload.get("Associations") or [] if isinstance(a, dict))
    return out


def _positive_conflicts(config: AdLoopConfig, account: str, customer: str, campaign_ids: list[str], rows: list[dict]) -> list[str]:
    texts = {r["Text"].lower(): r for r in rows}
    hits = []
    for cid in campaign_ids:
        for g in _ad_groups(config, account, customer, cid):
            for k in _keywords(config, account, customer, str(g.get("Id"))):
                text = str(k.get("Text") or "").lower()
                if k.get("Status") == "Active" and text in texts:
                    hits.append(f"'{k.get('Text')}' ({g.get('Name')})")
    return hits


def get_microsoft_negative_keywords(config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "") -> dict:
    """Negative keywords of the account's campaigns: direct ones and shared lists with their items."""
    meta, account, customer = _ctx(config, ad_account_id)
    campaigns = [c for c in _campaigns(config, account, customer) if not campaign_id or str(c.get("Id")) == str(campaign_id)]
    direct = []
    for c in campaigns:
        for n in _entity_negatives(config, account, customer, "Campaign", str(c.get("Id"))):
            direct.append({"campaign_id": str(c.get("Id")), "campaign_name": c.get("Name"), "text": n.get("Text"),
                           "match_type": n.get("MatchType"),
                           "remove_id": f"Campaign~{c.get('Id')}~{n.get('Id')}"})
    lists = _shared_lists(config, account, customer)
    associations = _list_associations(config, account, customer, [str(c.get("Id")) for c in campaigns]) if campaigns else []
    names = {str(c.get("Id")): c.get("Name") for c in campaigns}
    attached: dict[str, list[str]] = {}
    for a in associations:
        attached.setdefault(str(a.get("SharedEntityId")), []).append(names.get(str(a.get("EntityId")), str(a.get("EntityId"))))
    shared = []
    for lst in lists:
        lid = str(lst.get("Id"))
        if campaign_id and lid not in attached:
            continue
        items = _list_items(config, account, customer, lid)
        shared.append({
            "list_id": lid, "name": lst.get("Name"), "item_count": lst.get("ItemCount", len(items)),
            "attached_to": attached.get(lid, []),
            "keywords": [{"text": i.get("Text"), "match_type": i.get("MatchType"), "remove_id": f"{lid}~{i.get('Id')}"}
                         for i in items],
        })
    insights = []
    if direct:
        insights.append(f"{len(direct)} negative(s) sit directly on campaigns; a shared list keeps them reusable across campaigns.")
    unattached = [s["name"] for s in shared if not s["attached_to"]]
    if unattached and not campaign_id:
        insights.append(f"Lists attached to no campaign: {unattached}.")
    return {"ad_account_id": account, "direct_negatives": direct, "negative_keyword_lists": shared, "insights": insights}


def add_microsoft_negative_keywords(
    config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "", ad_group_id: str = "",
    keywords: list | None = None, match_type: str = "Phrase",
) -> dict:
    """Draft negatives directly on one campaign or ad group."""
    operation = "microsoft_add_negative_keywords"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    if bool(campaign_id) == bool(ad_group_id):
        errors.append("Pass exactly one of campaign_id or ad_group_id.")
    rows = _negative_rows(keywords or [], match_type, errors)
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    kind = "campaign" if campaign_id else "ad_group"
    found = find_entity(config, account, customer, kind, str(campaign_id or ad_group_id))
    if not found:
        return {"error": f"Microsoft {kind.replace('_', ' ')} '{campaign_id or ad_group_id}' was not found."}
    entity, campaign = found["entity"], found["campaign"]
    entity_type = "Campaign" if kind == "campaign" else "AdGroup"
    existing = {(str(n.get("Text")).lower(), n.get("MatchType")) for n in _entity_negatives(
        config, account, customer, entity_type, str(entity.get("Id")), "" if kind == "campaign" else str(campaign.get("Id")))}
    new_rows = [r for r in rows if (r["Text"].lower(), r["MatchType"]) not in existing]
    if not new_rows:
        return {"error": "Every negative is already there."}
    warnings = ["A shared negative keyword list (propose_microsoft_negative_keyword_list) keeps negatives reusable across campaigns."]
    conflicts = _positive_conflicts(config, account, customer, [str(campaign.get("Id"))], new_rows)
    if conflicts:
        warnings.append(f"These negatives block active keywords: {conflicts[:10]}.")
    return _plan(
        operation, kind, str(entity.get("Id")), account, customer,
        {"entity_name": entity.get("Name"),
         "campaign_name": campaign.get("Name") if kind == "ad_group" else None,
         "negatives": [f"{r['Text']} [{r['MatchType']}]" for r in new_rows]},
        {"entity_kind": entity_type, "campaign_id": str(campaign.get("Id")), "negative_rows": new_rows},
        warnings=warnings,
    )


def propose_microsoft_negative_keyword_list(
    config: AdLoopConfig, *, ad_account_id: str = "", list_name: str = "", keywords: list | None = None,
    match_type: str = "Phrase", campaign_ids: list[str] | None = None,
) -> dict:
    """Draft a new shared negative keyword list and attach it to campaigns."""
    operation = "microsoft_create_negative_list"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    list_name = (list_name or "").strip()
    if not list_name or len(list_name) > 255:
        errors.append("list_name is required (at most 255 characters).")
    rows = _negative_rows(keywords or [], match_type, errors)
    if len(rows) > 5000:
        errors.append("At most 5,000 negatives in one draft.")
    cids = _clean(campaign_ids)
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    if list_name.lower() in {str(lst.get("Name")).lower() for lst in _shared_lists(config, account, customer)}:
        return _validation_error([f"A negative keyword list named '{list_name}' already exists; add_to_microsoft_negative_keyword_list appends to it."])
    campaigns = {str(c.get("Id")): c for c in _campaigns(config, account, customer)}
    unknown = [c for c in cids if c not in campaigns]
    if unknown:
        return _validation_error([f"Campaign(s) {unknown} are not in account {account}."])
    warnings = []
    if cids:
        counts: dict[str, int] = {}
        for a in _list_associations(config, account, customer, cids):
            counts[str(a.get("EntityId"))] = counts.get(str(a.get("EntityId")), 0) + 1
        full = [campaigns[c].get("Name") for c in cids if counts.get(c, 0) >= _MAX_LISTS_PER_CAMPAIGN]
        if full:
            return _validation_error([f"Campaign(s) {full} already have {_MAX_LISTS_PER_CAMPAIGN} lists, Microsoft's limit."])
        conflicts = _positive_conflicts(config, account, customer, cids, rows)
        if conflicts:
            warnings.append(f"These negatives block active keywords: {conflicts[:10]}.")
    else:
        warnings.append("The list is attached to no campaign, so it blocks nothing until attached.")
    return _plan(
        operation, "negative_keyword_list", "", account, customer,
        {"entity_name": list_name, "negatives": [f"{r['Text']} [{r['MatchType']}]" for r in rows],
         "attach_to": [campaigns[c].get("Name") for c in cids]},
        {"campaign_ids": cids, "negative_rows": rows},
        warnings=warnings,
    )


def add_to_microsoft_negative_keyword_list(
    config: AdLoopConfig, *, ad_account_id: str = "", list_id: str = "", keywords: list | None = None,
    match_type: str = "Phrase",
) -> dict:
    """Draft appending negatives to an existing shared list (items already there are skipped)."""
    operation = "microsoft_add_to_negative_list"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    rows = _negative_rows(keywords or [], match_type, errors)
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    lists = {str(lst.get("Id")): lst for lst in _shared_lists(config, account, customer)}
    if str(list_id) not in lists:
        return {"error": f"Negative keyword list '{list_id}' was not found (get_microsoft_negative_keywords lists them)."}
    items = _list_items(config, account, customer, str(list_id))
    have = {(str(i.get("Text")).lower(), i.get("MatchType")) for i in items}
    new_rows = [r for r in rows if (r["Text"].lower(), r["MatchType"]) not in have]
    if not new_rows:
        return {"error": "Every negative is already in the list."}
    attached = [str(a.get("EntityId")) for a in _list_associations(
        config, account, customer, [str(c.get("Id")) for c in _campaigns(config, account, customer)])
        if str(a.get("SharedEntityId")) == str(list_id)]
    warnings = []
    conflicts = _positive_conflicts(config, account, customer, attached, new_rows)
    if conflicts:
        warnings.append(f"These negatives block active keywords: {conflicts[:10]}.")
    return _plan(
        operation, "negative_keyword_list", str(list_id), account, customer,
        {"entity_name": lists[str(list_id)].get("Name"), "attached_campaigns": len(attached),
         "negatives": [f"{r['Text']} [{r['MatchType']}]" for r in new_rows]},
        {"negative_rows": new_rows},
        warnings=warnings,
    )


def _draft_list_link(config, *, attach: bool, ad_account_id: str, list_id: str, campaign_ids: list | None) -> dict:
    operation = "microsoft_attach_negative_list" if attach else "microsoft_detach_negative_list"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    cids = _clean(campaign_ids)
    if not cids:
        return _validation_error(["campaign_ids needs at least one campaign id."])
    meta, account, customer = _ctx(config, ad_account_id)
    lists = {str(lst.get("Id")): lst for lst in _shared_lists(config, account, customer)}
    if str(list_id) not in lists:
        return {"error": f"Negative keyword list '{list_id}' was not found."}
    campaigns = {str(c.get("Id")): c for c in _campaigns(config, account, customer)}
    unknown = [c for c in cids if c not in campaigns]
    if unknown:
        return _validation_error([f"Campaign(s) {unknown} are not in account {account}."])
    linked = {str(a.get("EntityId")) for a in _list_associations(config, account, customer, cids)
              if str(a.get("SharedEntityId")) == str(list_id)}
    targets = [c for c in cids if (c not in linked) == attach]
    if not targets:
        return {"error": "Nothing to change: every campaign is already " + ("attached." if attach else "detached.")}
    warnings = []
    if not attach:
        warnings.append("Detached campaigns stop blocking these negatives; the list itself stays.")
    return _plan(
        operation, "negative_keyword_list", str(list_id), account, customer,
        {"entity_name": lists[str(list_id)].get("Name"), "campaigns": [campaigns[c].get("Name") for c in targets]},
        {"campaign_ids": targets},
        warnings=warnings,
    )


def attach_microsoft_negative_keyword_list(config, *, ad_account_id="", list_id="", campaign_ids=None) -> dict:
    return _draft_list_link(config, attach=True, ad_account_id=ad_account_id, list_id=list_id, campaign_ids=campaign_ids)


def detach_microsoft_negative_keyword_list(config, *, ad_account_id="", list_id="", campaign_ids=None) -> dict:
    return _draft_list_link(config, attach=False, ad_account_id=ad_account_id, list_id=list_id, campaign_ids=campaign_ids)


def _check_add_negative_keywords(s: Step) -> None:
    config, plan, c, account, customer = s.config, s.plan, s.c, s.account, s.customer
    found = s.entity(plan.entity_type, plan.entity_id)
    have = {(str(n.get("Text")).lower(), n.get("MatchType")) for n in _entity_negatives(
        config, account, customer, c["entity_kind"], plan.entity_id,
        "" if plan.entity_type == "campaign" else str(found["campaign"].get("Id")))}
    again = [r["Text"] for r in c["negative_rows"] if (r["Text"].lower(), r["MatchType"]) in have]
    if again:
        raise ValueError(f"Negative(s) {again} are there now (an earlier attempt may have added them).")


def _apply_add_negative_keywords(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    _call(config, c, "POST", "EntityNegativeKeywords", {"EntityNegativeKeywords": [
        {"EntityId": int(plan.entity_id), "EntityType": c["entity_kind"], "NegativeKeywords": c["negative_rows"]}]},
        "negative keywords")
    return {"entity": c["entity_name"], "negatives_added": len(c["negative_rows"])}


def _check_create_negative_list(s: Step) -> None:
    config, c, account, customer = s.config, s.c, s.account, s.customer
    if c["entity_name"].lower() in {str(x.get("Name")).lower() for x in _shared_lists(config, account, customer)}:
        raise ValueError(f"A list named '{c['entity_name']}' now exists (an earlier attempt may have created it).")


def _apply_create_negative_list(s: Step, progress: _Progress) -> dict:
    config, c = s.config, s.c
    items = [{"Type": "NegativeKeyword", **r} for r in c["negative_rows"]]
    payload = _call(config, c, "POST", "SharedEntity", {"SharedEntity": {"Type": _LIST_TYPE, "Name": c["entity_name"]},
                                                        "ListItems": items[:5000], "SharedEntityScope": "Account"},
                    "negative keyword list")
    list_id = str(payload.get("SharedEntityId"))
    progress.done.append(f"created list {list_id} with {len(items)} negatives")
    if c["campaign_ids"]:
        progress.step("Attaching the list", lambda: _call(config, c, "POST", "SharedEntityAssociations/Set", {
            "Associations": [{"EntityId": int(cid), "EntityType": "Campaign", "SharedEntityId": int(list_id),
                              "SharedEntityType": _LIST_TYPE} for cid in c["campaign_ids"]],
            "SharedEntityScope": "Account"}, "list attachment"))
    return {"list_id": list_id, "name": c["entity_name"], "negatives": len(items), "attached_to": c["attach_to"]}


def _check_negative_list(s: Step) -> None:
    config, plan, c, account, customer, op = s.config, s.plan, s.c, s.account, s.customer, s.plan.operation
    if plan.entity_id not in {str(x.get("Id")) for x in _shared_lists(config, account, customer)}:
        raise ValueError(f"Negative keyword list {plan.entity_id} no longer exists.")
    if op == "microsoft_add_to_negative_list":
        have = {(str(i.get("Text")).lower(), i.get("MatchType")) for i in _list_items(config, account, customer, plan.entity_id)}
        again = [r["Text"] for r in c["negative_rows"] if (r["Text"].lower(), r["MatchType"]) in have]
        if again:
            raise ValueError(f"Negative(s) {again} are in the list now (an earlier attempt may have added them).")


def _apply_add_to_negative_list(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    items = [{"Type": "NegativeKeyword", **r} for r in c["negative_rows"]]
    for chunk in _chunks(items, 5000):
        progress.step("Adding to the list", lambda chunk=chunk: _call(config, c, "POST", "ListItems", {
            "ListItems": chunk, "SharedList": {"Type": _LIST_TYPE, "Id": int(plan.entity_id)},
            "SharedEntityScope": "Account"}, "list items"))
    return {"list_id": plan.entity_id, "negatives_added": len(items)}


def _apply_list_link(s: Step, progress: _Progress) -> dict:
    config, plan, c, op = s.config, s.plan, s.c, s.plan.operation
    body = {"Associations": [{"EntityId": int(cid), "EntityType": "Campaign", "SharedEntityId": int(plan.entity_id),
                              "SharedEntityType": _LIST_TYPE} for cid in c["campaign_ids"]],
            "SharedEntityScope": "Account"}
    if op == "microsoft_attach_negative_list":
        _call(config, c, "POST", "SharedEntityAssociations/Set", body, "list attachment")
    else:
        _call(config, c, "DELETE", "SharedEntityAssociations", body, "list detachment")
    return {"list_id": plan.entity_id, "campaigns": c["campaigns"]}


HANDLERS = {
    "microsoft_add_negative_keywords": Handler(_check_add_negative_keywords, _apply_add_negative_keywords),
    "microsoft_create_negative_list": Handler(_check_create_negative_list, _apply_create_negative_list),
    "microsoft_add_to_negative_list": Handler(_check_negative_list, _apply_add_to_negative_list),
    "microsoft_attach_negative_list": Handler(_check_negative_list, _apply_list_link),
    "microsoft_detach_negative_list": Handler(_check_negative_list, _apply_list_link),
}
