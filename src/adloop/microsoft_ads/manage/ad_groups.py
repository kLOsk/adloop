"""Ad groups, keywords and keyword bids."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.manage._common import (
    _CONVERSION_BASED,
    _MANUAL,
    MATCH_TYPES,
    NETWORKS,
    Handler,
    Step,
    _call,
    _check_bid,
    _chunks,
    _ctx,
    _plan,
    _Progress,
    _require_unchanged,
    _same,
    _strategy_name,
)
from adloop.microsoft_ads.read import (
    _ad_groups,
    _keywords,
    _money,
)
from adloop.microsoft_ads.write import _blocked as _guard
from adloop.microsoft_ads.write import (
    _validation_error,
    find_entity,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def _keyword_rows(keywords: list, errors: list[str], *, default_match: str = "Phrase") -> list[dict]:
    rows = []
    for i, k in enumerate(keywords or []):
        if isinstance(k, str):
            k = {"text": k}
        if not isinstance(k, dict):
            errors.append(f"keywords[{i}] must be text or {{text, match_type, bid}}.")
            continue
        text = str(k.get("text") or "").strip()
        match = str(k.get("match_type") or default_match).strip().title()
        if not text or len(text) > 100:
            errors.append(f"keywords[{i}] text is 1–100 characters.")
        if match not in MATCH_TYPES:
            errors.append(f"keywords[{i}].match_type must be one of {list(MATCH_TYPES)}.")
        row: dict[str, Any] = {"Text": text, "MatchType": match, "Status": "Active"}
        if k.get("bid") is not None:
            row["Bid"] = {"Amount": round(float(k["bid"]), 2)}
        rows.append(row)
    return rows


def _refuse_broad_without_conversion_bidding(rows: list[dict], campaign: dict, errors: list[str]) -> None:
    strategy = _strategy_name(campaign.get("BiddingScheme"))
    broad = [r["Text"] for r in rows if r["MatchType"] == "Broad"]
    if broad and strategy not in _CONVERSION_BASED:
        errors.append(
            f"Broad match needs conversion-based bidding (MaxConversions, MaxConversionValue or a target "
            f"CPA/ROAS); on a {strategy or 'manual'} campaign it buys whatever a loose match brings. Use Phrase "
            f"or Exact for {broad[:5]}, or move the campaign to MaxConversions first."
        )


def draft_microsoft_ad_group(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    name: str = "",
    cpc_bid: float | None = None,
    network: str = "OwnedAndOperatedOnly",
    keywords: list | None = None,
) -> dict:
    """Draft a new ad group (with optional keywords) in a campaign."""
    operation = "microsoft_create_ad_group"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    name = (name or "").strip()
    if not name or len(name) > 256:
        errors.append("name is required (at most 256 characters).")
    if network not in NETWORKS:
        errors.append(f"network must be one of {list(NETWORKS)}.")
    rows = _keyword_rows(keywords or [], errors)
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "campaign", str(campaign_id))
    if not found:
        return {"error": f"Microsoft campaign '{campaign_id}' was not found in account {account}."}
    campaign = found["entity"]
    strategy = _strategy_name(campaign.get("BiddingScheme"))
    if strategy in _MANUAL and cpc_bid is None:
        errors.append(f"A {strategy} campaign needs cpc_bid for the ad group.")
    _refuse_broad_without_conversion_bidding(rows, campaign, errors)
    names = {str(g.get("Name")).lower() for g in _ad_groups(config, account, customer, str(campaign_id))}
    if name.lower() in names:
        errors.append(f"The campaign already has an ad group named '{name}'.")
    if errors:
        return _validation_error(errors)
    group: dict[str, Any] = {"Name": name, "Network": network, "Status": "Active"}
    if cpc_bid is not None:
        group["CpcBid"] = {"Amount": round(float(cpc_bid), 2)}
    warnings = ["The ad group is active but serves only once an ad in it is enabled (new ads are created paused)."]
    if network == "OwnedAndOperatedAndSyndicatedSearch":
        warnings.append("Syndicated search partners are included; their traffic is usually weaker.")
    return _plan(
        operation, "ad_group", "", account, customer,
        {"entity_name": name, "campaign_name": campaign.get("Name"), "campaign_status": campaign.get("Status"),
         "cpc_bid": _money(group.get("CpcBid")), "network": network,
         "status_on_create": "Active (its keywords too); nothing serves until an ad in it is enabled",
         "currency": meta["currency"],
         "keywords": [f"{r['Text']} [{r['MatchType']}]" + (f" bid {r['Bid']['Amount']}" if r.get("Bid") else "")
                      for r in rows] or None},
        {"campaign_id": str(campaign_id), "ad_group": group, "keyword_rows": rows},
        warnings=warnings,
    )


def update_microsoft_ad_group(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    ad_group_id: str = "",
    name: str = "",
    cpc_bid: float | None = None,
    network: str = "",
    audience_ads_bid_adjustment: int | None = None,
) -> dict:
    """Draft ad group changes: name, default bid, search network, Audience Network bid adjustment."""
    from adloop.safety.guards import SafetyViolation, check_bid_increase

    operation = "microsoft_update_ad_group"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    if network and network not in NETWORKS:
        errors.append(f"network must be one of {list(NETWORKS)}.")
    if audience_ads_bid_adjustment is not None and not -100 <= int(audience_ads_bid_adjustment) <= 900:
        errors.append("audience_ads_bid_adjustment is -100 (off) to 900.")
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad_group", str(ad_group_id))
    if not found:
        return {"error": f"Microsoft ad group '{ad_group_id}' was not found in account {account}."}
    group, campaign = found["entity"], found["campaign"]
    update: dict[str, Any] = {"Id": str(ad_group_id)}
    shown: dict[str, Any] = {}
    touched: list[str] = []
    warnings: list[str] = []
    if name and name.strip() != group.get("Name"):
        update["Name"] = name.strip()
        shown["name"] = {"from": group.get("Name"), "to": name.strip()}
        touched.append("Name")
    if cpc_bid is not None:
        old = _money(group.get("CpcBid"))
        try:
            if old:
                check_bid_increase(old, float(cpc_bid), config.safety)
        except SafetyViolation as e:
            return {"error": str(e)}
        update["CpcBid"] = {"Amount": round(float(cpc_bid), 2)}
        shown["cpc_bid"] = {"from": old, "to": round(float(cpc_bid), 2), "currency": meta["currency"]}
        touched.append("CpcBid")
        if _strategy_name(campaign.get("BiddingScheme")) not in _MANUAL:
            warnings.append("The campaign uses automated bidding, which ignores the ad group's default bid.")
    if network and network != group.get("Network"):
        update["Network"] = network
        shown["network"] = {"from": group.get("Network"), "to": network}
        touched.append("Network")
    adjust = None
    if audience_ads_bid_adjustment is not None and int(audience_ads_bid_adjustment) != group.get("AudienceAdsBidAdjustment"):
        from adloop.microsoft_ads.read import audience_network

        adjust = int(audience_ads_bid_adjustment)
        update["AudienceAdsBidAdjustment"] = adjust
        now = audience_network(group, campaign)
        was = ("off" if not now["on"] else f"{now['bid_adjustment'] or 0:+d}%") + f" (set on the {now['set_on']})"
        shown["audience_network"] = {"from": was, "to": adjust,
                                     "meaning": "off" if adjust == -100 else f"{adjust:+d}% bids on the Audience Network"}
        touched.append("AudienceAdsBidAdjustment")
    if len(update) == 1:
        return {"error": "Nothing to change: pass at least one setting that differs from the ad group."}
    return _plan(
        operation, "ad_group", str(ad_group_id), account, customer,
        {"entity_name": group.get("Name"), "campaign_name": campaign.get("Name"), "changes": shown},
        {"campaign_id": str(campaign.get("Id")), "update": update, "update_audience_adjustment": adjust is not None,
         "before": {k: copy.deepcopy(group.get(k)) for k in sorted(set(touched))}},
        warnings=warnings,
    )


def draft_microsoft_keywords(
    config: AdLoopConfig, *, ad_account_id: str = "", ad_group_id: str = "", keywords: list | None = None,
) -> dict:
    """Draft keyword additions to an ad group (duplicates skipped, broad refused on manual bids)."""
    operation = "microsoft_add_keywords"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    rows = _keyword_rows(keywords or [], errors)
    if not rows and not errors:
        errors.append("keywords needs at least one keyword.")
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad_group", str(ad_group_id))
    if not found:
        return {"error": f"Microsoft ad group '{ad_group_id}' was not found in account {account}."}
    group, campaign = found["entity"], found["campaign"]
    _refuse_broad_without_conversion_bidding(rows, campaign, errors)
    if errors:
        return _validation_error(errors)
    existing = {(str(k.get("Text")).lower(), k.get("MatchType")) for k in _keywords(config, account, customer, str(ad_group_id))
                if k.get("Status") != "Deleted"}
    seen: set = set()
    new_rows, skipped = [], []
    for r in rows:
        key = (r["Text"].lower(), r["MatchType"])
        if key in existing or key in seen:
            skipped.append(f"{r['Text']} [{r['MatchType']}]")
            continue
        seen.add(key)
        new_rows.append(r)
    if not new_rows:
        return {"error": "Every keyword is already in the ad group.", "skipped": skipped}
    warnings = [f"Skipped, already in the ad group: {skipped}."] if skipped else []
    if group.get("Status") != "Active" or campaign.get("Status") != "Active":
        warnings.append(f"The ad group is {group.get('Status')} and the campaign {campaign.get('Status')}: the keywords serve once both are active.")
    else:
        # Keywords are created Active, as Google Ads keywords are: in a live
        # ad group they can serve the moment this is applied.
        warnings.append("The keywords are added Active and the ad group and campaign are active, so they can serve "
                        "as soon as this is applied (once the ad group has an enabled ad).")
    return _plan(
        operation, "ad_group", str(ad_group_id), account, customer,
        {"entity_name": group.get("Name"), "campaign_name": campaign.get("Name"), "status_on_create": "Active",
         "ad_group_status": group.get("Status"), "campaign_status": campaign.get("Status"),
         "keywords": [f"{r['Text']} [{r['MatchType']}]" + (f" bid {r['Bid']['Amount']}" if r.get("Bid") else "")
                      for r in new_rows]},
        {"campaign_id": str(campaign.get("Id")), "keyword_rows": new_rows},
        warnings=warnings,
    )


def update_microsoft_keyword_bids(
    config: AdLoopConfig, *, ad_account_id: str = "", ad_group_id: str = "", bids: list | None = None,
) -> dict:
    """Draft keyword bid changes in one ad group (capped by max_bid_increase_pct)."""
    from adloop.safety.guards import SafetyViolation

    operation = "microsoft_update_keyword_bids"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    wanted: dict[str, float | None] = {}
    for i, b in enumerate(bids or []):
        if not isinstance(b, dict) or not b.get("keyword_id"):
            errors.append(f"bids[{i}] needs keyword_id and bid (null removes the keyword's own bid).")
            continue
        wanted[str(b["keyword_id"])] = None if b.get("bid") is None else round(float(b["bid"]), 2)
    if not wanted and not errors:
        errors.append("bids needs at least one {keyword_id, bid}.")
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad_group", str(ad_group_id))
    if not found:
        return {"error": f"Microsoft ad group '{ad_group_id}' was not found in account {account}."}
    group, campaign = found["entity"], found["campaign"]
    keywords = {str(k.get("Id")): k for k in _keywords(config, account, customer, str(ad_group_id))}
    missing = [k for k in wanted if k not in keywords]
    if missing:
        return _validation_error([f"Keyword(s) {missing} are not in ad group '{group.get('Name')}'."])
    default_bid = _money(group.get("CpcBid"))
    rows, shown, before = [], [], {}
    for kid, bid in wanted.items():
        current = _money(keywords[kid].get("Bid")) or default_bid
        # Removing a keyword's own bid makes it inherit the ad group default:
        # that is a bid change too, and it is capped the same way.
        effective = bid if bid is not None else default_bid
        try:
            _check_bid(current, effective, config)
        except SafetyViolation as e:
            return {"error": f"{keywords[kid].get('Text')}: {e}"}
        rows.append({"Id": kid, "Bid": {"Amount": bid}})
        shown.append({"keyword": f"{keywords[kid].get('Text')} [{keywords[kid].get('MatchType')}]",
                      "from": current, "to": bid if bid is not None else f"ad group default {default_bid}"})
        before[kid] = copy.deepcopy(keywords[kid].get("Bid"))
    return _plan(
        operation, "ad_group", str(ad_group_id), account, customer,
        {"entity_name": group.get("Name"), "currency": meta["currency"], "bids": shown},
        {"campaign_id": str(campaign.get("Id")), "keyword_rows": rows, "before": before,
         "default_bid_before": copy.deepcopy(group.get("CpcBid"))},
    )


def _check_create_ad_group(s: Step) -> None:
    config, c, account, customer = s.config, s.c, s.account, s.customer
    campaign = s.entity("campaign", c["campaign_id"])["entity"]
    if c["entity_name"].lower() in {str(g.get("Name")).lower() for g in _ad_groups(config, account, customer, c["campaign_id"])}:
        raise ValueError(f"An ad group named '{c['entity_name']}' now exists in the campaign.")
    errors = []
    _refuse_broad_without_conversion_bidding(c["keyword_rows"], campaign, errors)
    if _strategy_name(campaign.get("BiddingScheme")) in _MANUAL and not c["ad_group"].get("CpcBid"):
        errors.append("The campaign now uses manual bidding, which needs a default bid for the ad group.")
    if errors:
        raise ValueError("; ".join(errors))


def _apply_create_ad_group(s: Step, progress: _Progress) -> dict:
    config, c = s.config, s.c
    payload = _call(config, c, "POST", "AdGroups", {"CampaignId": c["campaign_id"], "AdGroups": [c["ad_group"]]}, "new ad group")
    ad_group_id = str((payload.get("AdGroupIds") or [None])[0])
    progress.done.append(f"created ad group {ad_group_id}")
    for chunk in _chunks(c["keyword_rows"], 1000):
        progress.step("Adding its keywords", lambda chunk=chunk: _call(
            config, c, "POST", "Keywords", {"AdGroupId": ad_group_id, "Keywords": chunk}, "keywords"))
    return {"ad_group_id": ad_group_id, "name": c["entity_name"], "keywords_added": len(c["keyword_rows"])}


def _check_update_ad_group(s: Step) -> None:
    config, plan, c = s.config, s.plan, s.c
    group = s.entity("ad_group", plan.entity_id)["entity"]
    _require_unchanged(group, c["before"], "ad group")
    if "CpcBid" in c["update"]:
        _check_bid(_money(group.get("CpcBid")), c["update"]["CpcBid"]["Amount"], config)


def _apply_update_ad_group(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    _call(config, c, "PUT", "AdGroups", {"CampaignId": c["campaign_id"], "AdGroups": [c["update"]],
                                         "UpdateAudienceAdsBidAdjustment": bool(c.get("update_audience_adjustment"))},
          "ad group update")
    return {"ad_group_id": plan.entity_id, "updated": sorted(k for k in c["update"] if k != "Id")}


def _check_add_keywords(s: Step) -> None:
    config, plan, c, account, customer = s.config, s.plan, s.c, s.account, s.customer
    found = s.entity("ad_group", plan.entity_id)
    errors = []
    _refuse_broad_without_conversion_bidding(c["keyword_rows"], found["campaign"], errors)
    if errors:
        raise ValueError("; ".join(errors))
    have = {(str(k.get("Text")).lower(), k.get("MatchType")) for k in _keywords(config, account, customer, plan.entity_id)
            if k.get("Status") != "Deleted"}
    again = [r["Text"] for r in c["keyword_rows"] if (r["Text"].lower(), r["MatchType"]) in have]
    if again:
        raise ValueError(f"Keyword(s) {again} are in the ad group now (an earlier attempt may have added them).")


def _apply_add_keywords(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    payload = _call(config, c, "POST", "Keywords", {"AdGroupId": plan.entity_id, "Keywords": c["keyword_rows"]}, "keywords")
    return {"ad_group_id": plan.entity_id, "keyword_ids": [str(i) for i in payload.get("KeywordIds") or []]}


def _check_update_keyword_bids(s: Step) -> None:
    config, plan, c, account, customer = s.config, s.plan, s.c, s.account, s.customer
    group = s.entity("ad_group", plan.entity_id)["entity"]
    if not _same(group.get("CpcBid"), c.get("default_bid_before")):
        raise ValueError("The ad group's default bid changed since the draft; draft again.")
    now = {str(k.get("Id")): k.get("Bid") for k in _keywords(config, account, customer, plan.entity_id)}
    for kid, bid in (c["before"] or {}).items():
        if kid not in now:
            raise ValueError(f"Keyword {kid} no longer exists.")
        if not _same(now[kid], bid):
            raise ValueError(f"Keyword {kid}'s bid changed since the draft; draft again.")
    default = _money(group.get("CpcBid"))
    for row in c["keyword_rows"]:
        current = _money(now[str(row["Id"])]) or default
        proposed = row["Bid"]["Amount"] if row["Bid"]["Amount"] is not None else default
        _check_bid(current, proposed, config)


def _apply_update_keyword_bids(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    _call(config, c, "PUT", "Keywords", {"AdGroupId": plan.entity_id, "Keywords": c["keyword_rows"]}, "keyword bids")
    return {"ad_group_id": plan.entity_id, "keywords_updated": len(c["keyword_rows"])}


HANDLERS = {
    "microsoft_create_ad_group": Handler(_check_create_ad_group, _apply_create_ad_group),
    "microsoft_update_ad_group": Handler(_check_update_ad_group, _apply_update_ad_group),
    "microsoft_add_keywords": Handler(_check_add_keywords, _apply_add_keywords),
    "microsoft_update_keyword_bids": Handler(_check_update_keyword_bids, _apply_update_keyword_bids),
}
