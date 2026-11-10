"""Campaign settings (name, bid strategy, tracking) and new Search campaigns."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.manage._common import (
    _CAPPED,
    _CONVERSION_BASED,
    INTENTS,
    STRATEGIES,
    Handler,
    Step,
    _active_broad_keywords,
    _broad_refusal,
    _call,
    _check_bid,
    _chunks,
    _clean,
    _counted_goals,
    _ctx,
    _goals_unknown_warning,
    _no_goal_refusal,
    _plan,
    _Progress,
    _require_unchanged,
    _strategy_name,
    bidding_scheme,
    search_strategy,
)
from adloop.microsoft_ads.read import (
    _bidding,
    _campaigns,
)
from adloop.microsoft_ads.write import _blocked as _guard
from adloop.microsoft_ads.write import (
    _validation_error,
    find_entity,
)
from adloop.safety.guards import check_budget_cap

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def update_microsoft_campaign(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    name: str = "",
    bidding_strategy: str = "",
    target_cpa: float | None = None,
    max_cpc: float | None = None,
    tracking_url_template: str | None = None,
    final_url_suffix: str | None = None,
) -> dict:
    """Draft campaign setting changes: name, bid strategy, tracking template, final URL suffix."""
    operation = "microsoft_update_campaign"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    if name and len(name.strip()) > 128:
        errors.append("name is at most 128 characters.")
    strategy = (bidding_strategy or "").strip()
    if strategy == "TargetCpa":
        strategy = "MaxConversions"
    if strategy and strategy not in STRATEGIES:
        errors.append(f"bidding_strategy must be one of {list(STRATEGIES)}.")
    strategy, strategy_note = search_strategy(strategy)
    if target_cpa is not None and (strategy or "") not in ("", "MaxConversions"):
        errors.append("target_cpa goes with MaxConversions.")
    if max_cpc is not None and strategy and strategy not in _CAPPED:
        errors.append(f"max_cpc caps {' or '.join(sorted(_CAPPED))} bids; a {strategy} campaign's bids are set "
                      "on its ad groups and keywords.")
    template = None if tracking_url_template is None else tracking_url_template.strip()
    if template and not template.startswith(("http://", "https://", "{lpurl}", "{unescapedlpurl}")):
        errors.append("tracking_url_template must start with {lpurl}, {unescapedlpurl}, http:// or https://.")
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "campaign", campaign_id)
    if not found:
        return {"error": f"Microsoft campaign '{campaign_id}' was not found in account {account}."}
    campaign = found["entity"]
    update: dict[str, Any] = {"Id": campaign_id}
    shown: dict[str, Any] = {}
    warnings: list[str] = []
    touched: list[str] = []
    if name and name.strip() != campaign.get("Name"):
        others = {str(c.get("Name")).lower() for c in _campaigns(config, account, customer) if str(c.get("Id")) != campaign_id}
        if name.strip().lower() in others:
            return _validation_error([f"Another campaign is already named '{name.strip()}'."])
        update["Name"] = name.strip()
        shown["name"] = {"from": campaign.get("Name"), "to": name.strip()}
        touched.append("Name")
    current_scheme = campaign.get("BiddingScheme") or {}
    if strategy or target_cpa is not None or max_cpc is not None:
        chosen = strategy or _strategy_name(current_scheme)
        if chosen not in STRATEGIES:
            return _validation_error([f"The campaign uses {chosen or 'an unknown strategy'}; pass bidding_strategy explicitly."])
        if target_cpa is not None and chosen != "MaxConversions":
            return _validation_error([f"target_cpa goes with MaxConversions; this campaign uses {chosen}. Pass "
                                      "bidding_strategy='MaxConversions' to switch."])
        if max_cpc is not None and chosen not in _CAPPED:
            return _validation_error([f"max_cpc caps {' or '.join(sorted(_CAPPED))} bids; this campaign uses "
                                      f"{chosen}, whose bids are set on its ad groups and keywords."])
        has_cap = (current_scheme.get("MaxCpc") or {}).get("Amount") is not None
        if max_cpc is not None and not has_cap and chosen in _CAPPED:
            return _validation_error([
                "Microsoft no longer lets a campaign add a maximum CPC to automated bidding; it can "
                "only change or remove an existing one."
            ])
        keep_cap = (current_scheme.get("MaxCpc") or {}).get("Amount") if chosen == _strategy_name(current_scheme) else None
        scheme = bidding_scheme(
            chosen,
            target_cpa=target_cpa if target_cpa is not None else (current_scheme.get("TargetCpa") if chosen == "MaxConversions" and _strategy_name(current_scheme) == "MaxConversions" else None),
            max_cpc=max_cpc if max_cpc is not None else keep_cap,
        )
        update["BiddingScheme"] = scheme
        touched.append("BiddingScheme")
        shown["bidding"] = {"from": _bidding(current_scheme), "to": _bidding(scheme), "currency": meta["currency"]}
        if strategy_note:
            warnings.append(strategy_note)
        switching = chosen != _strategy_name(current_scheme)
        if switching:
            warnings.append("Changing the bid strategy restarts Microsoft's learning period; expect a week or two of unsettled results.")
        if chosen in _CONVERSION_BASED:
            goals = _counted_goals(config, account, customer)
            if goals == 0 and switching:
                return _validation_error([_no_goal_refusal(chosen)])
            if goals == 0:
                warnings.append(f"No active conversion goal counts toward Conversions: {chosen} has nothing to optimize for.")
            elif goals < 0:
                warnings.append(_goals_unknown_warning(chosen))
        old_cap = (current_scheme.get("MaxCpc") or {}).get("Amount")
        new_cap = (scheme.get("MaxCpc") or {}).get("Amount")
        try:
            _check_bid(old_cap, new_cap, config)
        except Exception as e:  # SafetyViolation
            return {"error": f"Maximum CPC: {e}"}
        if chosen not in _CONVERSION_BASED:
            broad = _active_broad_keywords(config, account, customer, campaign_id)
            if broad:
                return _validation_error([_broad_refusal(len(broad), chosen)])
    for field, value, api_name in (("tracking_url_template", template, "TrackingUrlTemplate"),
                                   ("final_url_suffix", None if final_url_suffix is None else final_url_suffix.strip(), "FinalUrlSuffix")):
        if value is None or value == (campaign.get(api_name) or ""):
            continue
        update[api_name] = value
        touched.append(api_name)
        shown[field] = {"from": campaign.get(api_name) or None, "to": value or None}
        if value == "":
            warnings.append(f"{field} is removed.")
    if len(update) == 1:
        return {"error": "Nothing to change: pass at least one setting that differs from the campaign."}
    return _plan(
        operation, "campaign", campaign_id, account, customer,
        {"entity_name": campaign.get("Name"), "campaign_status": campaign.get("Status"), "changes": shown},
        {"update": update, "before": {k: copy.deepcopy(campaign.get(k)) for k in sorted(set(touched))}},
        warnings=warnings,
    )


def draft_microsoft_campaign(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    name: str = "",
    daily_budget: float = 0.0,
    bidding_strategy: str = "MaxConversions",
    target_cpa: float | None = None,
    languages: list[str] | None = None,
    location_ids: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    location_intent: str = "PeopleIn",
    tracking_url_template: str = "",
    final_url_suffix: str = "",
) -> dict:
    """Draft a new Search campaign, created PAUSED, with its locations and location intent."""
    from adloop.safety.guards import SafetyViolation, check_budget_cap

    operation = "microsoft_create_campaign"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    name = (name or "").strip()
    if not name or len(name) > 128:
        errors.append("name is required (at most 128 characters).")
    try:
        budget = round(float(daily_budget), 2)
    except (TypeError, ValueError):
        budget = 0.0
    if budget <= 0:
        errors.append("daily_budget must be a positive amount in the account currency.")
    strategy = "MaxConversions" if bidding_strategy == "TargetCpa" else (bidding_strategy or "MaxConversions")
    if strategy not in STRATEGIES:
        errors.append(f"bidding_strategy must be one of {list(STRATEGIES)}.")
    strategy, strategy_note = search_strategy(strategy)
    if target_cpa is not None and strategy != "MaxConversions":
        errors.append(f"target_cpa goes with MaxConversions, not {strategy}.")
    langs = _clean(languages)
    if not langs:
        errors.append("languages is required, e.g. ['English']: without it ads reach every language.")
    elif any(lang.lower() == "all" for lang in langs):
        errors.append("languages=['All'] reaches every language; name the languages the ads are written in, "
                      "e.g. ['English'].")
    include = _clean(location_ids)
    if not include:
        errors.append("location_ids is required (search_microsoft_locations): an untargeted campaign serves everywhere.")
    exclude = _clean(excluded_location_ids)
    if any(not i.isdigit() for i in include + exclude):
        errors.append("Location ids are numbers (search_microsoft_locations).")
    if location_intent not in INTENTS:
        errors.append(f"location_intent must be one of {list(INTENTS)}.")
    if errors:
        return _validation_error(errors)
    try:
        check_budget_cap(budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    meta, account, customer = _ctx(config, ad_account_id)
    existing = {str(c.get("Name")).lower() for c in _campaigns(config, account, customer)}
    if name.lower() in existing:
        return _validation_error([f"A campaign named '{name}' already exists."])
    warnings: list[str] = [strategy_note] if strategy_note else []
    if strategy in _CONVERSION_BASED:
        goals = _counted_goals(config, account, customer)
        if goals == 0:
            return _validation_error([_no_goal_refusal(strategy)])
        if goals < 0:
            warnings.append(_goals_unknown_warning(strategy))
    if target_cpa is not None and target_cpa > 0 and budget < 5 * target_cpa:
        warnings.append(f"A daily budget below 5× the target CPA ({target_cpa}) gives bidding little to learn from.")
    campaign: dict[str, Any] = {
        "Name": name,
        "BudgetType": "DailyBudgetStandard",
        "DailyBudget": budget,
        "CampaignType": "Search",
        "Languages": langs,
        "Status": "Paused",
        "TimeZone": meta["time_zone"],
        "BiddingScheme": bidding_scheme(strategy, target_cpa=target_cpa, max_cpc=None),
    }
    if tracking_url_template:
        campaign["TrackingUrlTemplate"] = tracking_url_template.strip()
    if final_url_suffix:
        campaign["FinalUrlSuffix"] = final_url_suffix.strip()
    criteria = (
        [{"Type": "BiddableCampaignCriterion", "Criterion": {"Type": "LocationCriterion", "LocationId": int(i)},
          "CriterionBid": {"Type": "BidMultiplier", "Multiplier": 0}} for i in include]
        + [{"Type": "NegativeCampaignCriterion", "Criterion": {"Type": "LocationCriterion", "LocationId": int(i)}}
           for i in exclude]
        + [{"Type": "BiddableCampaignCriterion",
            "Criterion": {"Type": "LocationIntentCriterion", "IntentOption": location_intent}}]
    )
    # [AUD]: search campaigns reach the Microsoft Audience Network (Outlook,
    # MSN) unless their AudienceAdsBidAdjustment is -100.
    warnings.append("New search campaigns can also show on the Microsoft Audience Network (Outlook, MSN, Edge); "
                    "set audience_ads_bid_adjustment=-100 on its ad groups (update_microsoft_ad_group) to keep "
                    "it on search only.")
    return _plan(
        operation, "campaign", "", account, customer,
        {
            "entity_name": name, "status_on_create": "Paused", "daily_budget": budget, "currency": meta["currency"],
            "monthly_maximum": round(budget * 30.4, 2), "bidding": _bidding(campaign["BiddingScheme"]),
            "languages": langs, "location_ids": include, "excluded_location_ids": exclude or None,
            "location_intent": location_intent, "time_zone": meta["time_zone"],
            "tracking_url_template": campaign.get("TrackingUrlTemplate"),
            "final_url_suffix": campaign.get("FinalUrlSuffix"),
        },
        {"campaign": campaign, "criteria": criteria},
        warnings=warnings + [
            "Created paused. Add ad groups, keywords, ads and extensions, then enable it.",
        ],
    )


def _check_update_campaign(s: Step) -> None:
    config, plan, c, account, customer = s.config, s.plan, s.c, s.account, s.customer
    campaign = s.entity("campaign", plan.entity_id)["entity"]
    _require_unchanged(campaign, c["before"], "campaign")
    scheme = c["update"].get("BiddingScheme")
    if scheme:
        _check_bid((((campaign.get("BiddingScheme") or {}).get("MaxCpc")) or {}).get("Amount"),
                   (scheme.get("MaxCpc") or {}).get("Amount"), config)
        strategy = _strategy_name(scheme)
        if strategy not in _CONVERSION_BASED:
            broad = _active_broad_keywords(config, account, customer, plan.entity_id)
            if broad:
                raise ValueError(_broad_refusal(len(broad), strategy).replace("The campaign has", "The campaign now has"))
        elif (strategy != _strategy_name(c["before"].get("BiddingScheme"))
              and _counted_goals(config, account, customer) == 0):
            raise ValueError(_no_goal_refusal(strategy))


def _apply_update_campaign(s: Step, progress: _Progress) -> dict:
    config, plan, c, account = s.config, s.plan, s.c, s.account
    _call(config, c, "PUT", "Campaigns", {"AccountId": account, "Campaigns": [c["update"]]}, "campaign update")
    return {"campaign_id": plan.entity_id, "updated": sorted(k for k in c["update"] if k != "Id")}


def _check_create_campaign(s: Step) -> None:
    config, c, account, customer = s.config, s.c, s.account, s.customer
    check_budget_cap(float(c["daily_budget"]), config.safety)
    if c["entity_name"].lower() in {str(x.get("Name")).lower() for x in _campaigns(config, account, customer)}:
        raise ValueError(f"A campaign named '{c['entity_name']}' now exists (an earlier attempt may have created it).")
    strategy = _strategy_name(c["campaign"].get("BiddingScheme"))
    if strategy in _CONVERSION_BASED:
        goals = _counted_goals(config, account, customer)
        if goals == 0:
            raise ValueError(_no_goal_refusal(strategy))
        s.checks["conversion_goals"] = goals if goals >= 0 else "unknown"
    s.checks["budget_cap_ok"] = True


def _apply_create_campaign(s: Step, progress: _Progress) -> dict:
    config, c, account = s.config, s.c, s.account
    payload = _call(config, c, "POST", "Campaigns", {"AccountId": account, "Campaigns": [c["campaign"]]}, "new campaign")
    campaign_id = str((payload.get("CampaignIds") or [None])[0])
    progress.done.append(f"created campaign {campaign_id} (paused)")
    criteria = [{**x, "CampaignId": campaign_id} for x in c["criteria"]]
    for chunk in _chunks(criteria):
        progress.step("Adding the campaign's locations", lambda chunk=chunk: _call(
            config, c, "POST", "CampaignCriterions", {"CampaignCriterions": chunk, "CriterionType": "Targets"}, "locations"))
    return {"campaign_id": campaign_id, "name": c["entity_name"], "status": "Paused",
            "next": "Add ad groups, keywords, ads and extensions, then enable it."}


HANDLERS = {
    "microsoft_update_campaign": Handler(_check_update_campaign, _apply_update_campaign),
    "microsoft_create_campaign": Handler(_check_create_campaign, _apply_create_campaign),
}
