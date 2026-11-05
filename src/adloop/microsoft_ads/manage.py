"""Microsoft Advertising management: campaigns, targeting, ad groups, keywords,
negatives, responsive search ads, ad extensions and removals.

Every function here drafts a :class:`ChangePlan` (platform ``microsoft``,
operation ``microsoft_*``) and returns its preview; nothing changes until
``confirm_and_apply``. :mod:`adloop.microsoft_ads.write` routes these
operations to :func:`preflight` and :func:`apply_plan` below.

Microsoft's Campaign Management API has no validate-only mode, so:

- **Updates** store a snapshot of every field they change; the dry run and
  the apply re-read the entity and refuse when one of those fields was
  edited in Microsoft Advertising since the preview.
- **Creates** re-check their parents, the safety caps and (for named
  objects) that the name is still free, so a retried apply after a partial
  failure refuses instead of creating a second copy.
- **Multi-step applies** (a campaign and then its targeting, a list and then
  its attachments) report exactly what was created when a later step fails.

REST conventions (learn.microsoft.com/advertising/campaign-management-service):
add is POST, update PUT, delete DELETE on the collection path; polymorphic
objects carry a ``Type`` discriminator; on update an omitted field stays as
it is and a list that is sent replaces the old one.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.auth import MicrosoftAdsApiError
from adloop.microsoft_ads.client import campaign_request, fault_details
from adloop.microsoft_ads.read import (
    _ad_groups,
    _ads,
    _bidding,
    _campaigns,
    _criterions,
    _customer,
    _keywords,
    _money,
    account_meta,
)
from adloop.microsoft_ads.write import _guard, _store, _validation_error, find_entity

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

STRATEGIES = ("ManualCpc", "EnhancedCpc", "MaxClicks", "MaxConversions")
_SMART = {"MaxClicks", "MaxConversions"}
_MANUAL = {"ManualCpc", "EnhancedCpc"}
MATCH_TYPES = ("Exact", "Phrase", "Broad")
NEGATIVE_MATCH_TYPES = ("Exact", "Phrase")
NETWORKS = ("OwnedAndOperatedOnly", "OwnedAndOperatedAndSyndicatedSearch")
INTENTS = ("PeopleIn", "PeopleInOrSearchingForOrViewingPages")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MINUTES = {0: "Zero", 15: "Fifteen", 30: "Thirty", 45: "FortyFive"}
_PINS = {
    "headline_1": "Headline1", "headline_2": "Headline2", "headline_3": "Headline3",
    "headline1": "Headline1", "headline2": "Headline2", "headline3": "Headline3",
    "description_1": "Description1", "description_2": "Description2",
    "description1": "Description1", "description2": "Description2",
}
_EXTENSION_TYPES = "SitelinkAdExtension, CalloutAdExtension, StructuredSnippetAdExtension, CallAdExtension"
_LIST_TYPE = "NegativeKeywordList"
_MAX_LISTS_PER_CAMPAIGN = 20
_CHUNK = 100


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ctx(config: AdLoopConfig, ad_account_id: str) -> tuple[dict, str, str]:
    meta = account_meta(config, ad_account_id)
    return meta, meta["ad_account_id"], _customer(config, meta)


def _clean(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, int)):
        values = [values]
    return [str(v).strip() for v in values if str(v).strip()]


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _check_url(url: str, errors: list[str], warnings: list[str], field: str = "final_url") -> None:
    if not url.startswith(("http://", "https://")):
        errors.append(f"{field} must start with http:// or https://")
        return
    from adloop.ads.write import _validate_urls

    bad, inconclusive = _validate_urls([url])
    if bad.get(url):
        errors.append(f"{field} '{url}' is not reachable: {bad[url]}")
    warnings.extend(inconclusive.values())


_ID_FIELDS = ("CampaignIds", "AdGroupIds", "KeywordIds", "AdIds", "CampaignCriterionIds", "ListItemIds")


def _created_ids(payload: dict) -> list[str]:
    """IDs a create reported as done (failed items come back as null)."""
    ids: list[str] = []
    for field in _ID_FIELDS:
        ids += [str(i) for i in payload.get(field) or [] if i is not None]
    ids += [str((i or {}).get("Id")) for i in payload.get("AdExtensionIdentities") or [] if i and i.get("Id")]
    if payload.get("SharedEntityId"):
        ids.append(str(payload["SharedEntityId"]))
    for group in payload.get("NegativeKeywordIds") or []:
        ids += [str(i) for i in (group or {}).get("Ids") or [] if i is not None]
    return ids


def _partial_errors(payload: dict) -> list[dict]:
    entries = [e for e in payload.get("PartialErrors") or [] if e]
    for nested in payload.get("NestedPartialErrors") or []:
        if not isinstance(nested, dict):
            continue
        entries.extend(e for e in nested.get("BatchErrors") or [] if e)
        if nested.get("ErrorCode") or nested.get("Message"):
            entries.append(nested)
    return entries


def _raise_partial(payload: dict, what: str) -> None:
    entries = _partial_errors(payload)
    if not entries:
        return
    _codes, message, _tracking = fault_details({"PartialErrors": entries})
    created = _created_ids(payload)
    if created:
        # Some items were created and some refused: replaying the plan would
        # create the successful ones again.
        from adloop.safety.outcomes import PartialApplyError

        raise PartialApplyError(
            f"Microsoft created part of the {what} (ids {created}) and refused the rest: {message or entries}",
            created={what: created},
        )
    raise MicrosoftAdsApiError(f"Microsoft refused the {what}: {message or entries}")


def _call(config: AdLoopConfig, changes: dict, method: str, path: str, body: dict, what: str) -> dict:
    try:
        payload = campaign_request(
            config, path, body, account_id=changes["ad_account_id"], customer_id=changes.get("customer_id", ""),
            method=method, mutation=True,
        )
    except MicrosoftAdsApiError as exc:
        if method == "POST" and (exc.status == 0 or exc.status >= 500):
            # The request may have reached Microsoft and created something:
            # these creates have no idempotency, so replaying the plan could
            # create it twice. Retire the plan and say so.
            from adloop.safety.outcomes import PartialApplyError

            raise PartialApplyError(
                f"Microsoft did not confirm the {what} ({exc}); it may or may not have been created. "
                "Check the account before drafting it again.",
                unknown=True,
            ) from None
        raise
    _raise_partial(payload, what)
    return payload


class _Progress:
    """What a multi-step apply already changed, for an honest error.

    Once one step has changed the account, a failure in a later step retires
    the plan (PartialApplyError) instead of leaving it to be replayed.
    """

    def __init__(self) -> None:
        self.done: list[str] = []

    def step(self, what: str, fn):
        from adloop.safety.outcomes import PartialApplyError

        try:
            return fn()
        except PartialApplyError as exc:
            if self.done:
                raise PartialApplyError(
                    f"{exc} (after: {'; '.join(self.done)})",
                    created={**exc.created, "done": list(self.done)}, unknown=exc.unknown,
                ) from None
            raise
        except Exception as exc:
            if self.done:
                raise PartialApplyError(
                    f"{what} failed after these steps succeeded: {'; '.join(self.done)}. "
                    f"Microsoft said: {exc}. Check the account before drafting again.",
                    created={"done": list(self.done)},
                ) from None
            raise


def _base_changes(account: str, customer: str, **extra: Any) -> dict:
    return {"ad_account_id": account, "customer_id": customer, **extra}


def _plan(operation: str, entity_type: str, entity_id: str, account: str, changes: dict,
          *, warnings: list[str] | None = None, double: bool = False) -> dict:
    return _store(
        {"operation": operation, "entity_type": entity_type, "entity_id": str(entity_id or ""),
         "customer_id": account, "requires_double_confirm": double, "changes": changes},
        warnings=warnings,
    )


def _chunks(items: list, size: int = _CHUNK) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Bidding
# ---------------------------------------------------------------------------


def bidding_scheme(strategy: str, *, target_cpa: float | None, max_cpc: float | None) -> dict:
    scheme: dict[str, Any] = {"Type": strategy}
    if strategy == "MaxConversions" and target_cpa is not None:
        scheme["TargetCpa"] = round(float(target_cpa), 2)
    if strategy in _SMART and max_cpc is not None:
        scheme["MaxCpc"] = {"Amount": round(float(max_cpc), 2)}
    return scheme


def _strategy_name(scheme: dict | None) -> str:
    return str((scheme or {}).get("Type") or "").replace("BiddingScheme", "")


def _counted_goals(config: AdLoopConfig, account: str, customer: str) -> int:
    from adloop.microsoft_ads.read import CONVERSION_GOAL_TYPES

    try:
        goals = campaign_request(
            config, "ConversionGoals/QueryByIds",
            {"ConversionGoalIds": None, "ConversionGoalTypes": CONVERSION_GOAL_TYPES},
            account_id=account, customer_id=customer,
        ).get("ConversionGoals") or []
    except MicrosoftAdsApiError:
        return -1
    return sum(1 for g in goals if isinstance(g, dict) and g.get("Status") == "Active" and not g.get("ExcludeFromBidding"))


# ---------------------------------------------------------------------------
# Campaign settings
# ---------------------------------------------------------------------------


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
    if target_cpa is not None and (strategy or "") not in ("", "MaxConversions"):
        errors.append("target_cpa goes with MaxConversions.")
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
        has_cap = (current_scheme.get("MaxCpc") or {}).get("Amount") is not None
        if max_cpc is not None and not has_cap and chosen in _SMART:
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
        if chosen != _strategy_name(current_scheme):
            warnings.append("Changing the bid strategy restarts Microsoft's learning period; expect a week or two of unsettled results.")
        if chosen == "MaxConversions" and _counted_goals(config, account, customer) == 0:
            warnings.append("No active conversion goal counts toward Conversions: MaxConversions has nothing to optimize for.")
        old_cap = (current_scheme.get("MaxCpc") or {}).get("Amount")
        new_cap = (scheme.get("MaxCpc") or {}).get("Amount")
        try:
            _check_bid(old_cap, new_cap, config)
        except Exception as e:  # SafetyViolation
            return {"error": f"Maximum CPC: {e}"}
        if chosen in _MANUAL:
            broad = _active_broad_keywords(config, account, customer, campaign_id)
            if broad:
                return _validation_error([
                    f"The campaign has {len(broad)} active broad-match keyword(s); manual bidding with broad "
                    "match wastes spend. Change them to phrase or exact first."
                ])
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
        operation, "campaign", campaign_id, account,
        _base_changes(account, customer, entity_name=campaign.get("Name"), campaign_status=campaign.get("Status"),
                      changes=shown, update=update,
                      before={k: copy.deepcopy(campaign.get(k)) for k in sorted(set(touched))}),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Targeting: locations, location intent, ad schedule
# ---------------------------------------------------------------------------


def _current_targeting(config: AdLoopConfig, account: str, customer: str, campaign_id: str) -> dict:
    locations = _criterions(config, account, customer, campaign_id, "Location")
    included = {}
    excluded = {}
    for c in locations:
        crit = c.get("Criterion") or {}
        entry = {"criterion_id": str(c.get("Id")), "name": crit.get("DisplayName") or str(crit.get("LocationId"))}
        target = excluded if c.get("Type") == "NegativeCampaignCriterion" else included
        target[str(crit.get("LocationId"))] = entry
    intent = _criterions(config, account, customer, campaign_id, "LocationIntent")
    schedule = _criterions(config, account, customer, campaign_id, "DayTime")
    return {
        "included": included,
        "excluded": excluded,
        "intent": {"criterion_id": str(intent[0].get("Id")),
                   "option": (intent[0].get("Criterion") or {}).get("IntentOption")} if intent else None,
        "schedule": [
            {"criterion_id": str(c.get("Id")), **{k: (c.get("Criterion") or {}).get(k)
                                                   for k in ("Day", "FromHour", "FromMinute", "ToHour", "ToMinute")}}
            for c in schedule
        ],
    }


def _schedule_blocks(schedule: list, errors: list[str]) -> list[dict]:
    blocks = []
    for i, raw in enumerate(schedule or []):
        if not isinstance(raw, dict):
            errors.append(f"ad_schedule[{i}] must be an object with day_of_week, start_hour, end_hour.")
            continue
        day = str(raw.get("day_of_week") or raw.get("day") or "").strip().title()
        try:
            start_h, end_h = int(raw.get("start_hour")), int(raw.get("end_hour"))
            start_m, end_m = int(raw.get("start_minute") or 0), int(raw.get("end_minute") or 0)
        except (TypeError, ValueError):
            errors.append(f"ad_schedule[{i}] hours and minutes must be whole numbers.")
            continue
        if day not in DAYS:
            errors.append(f"ad_schedule[{i}].day_of_week must be one of {list(DAYS)}, got '{raw.get('day_of_week') or raw.get('day')}'.")
        if not (0 <= start_h <= 23 and 0 <= end_h <= 24) or (end_h == 24 and end_m):
            errors.append(f"ad_schedule[{i}] hours are 0–23 to start and 0–24 to end (24 only with minute 0).")
        if start_m not in _MINUTES or end_m not in _MINUTES:
            errors.append(f"ad_schedule[{i}] minutes are 0, 15, 30 or 45.")
        if (end_h, end_m) <= (start_h, start_m):
            errors.append(f"ad_schedule[{i}] must end after it starts.")
        blocks.append({"Day": day, "FromHour": start_h, "FromMinute": _MINUTES.get(start_m, "Zero"),
                       "ToHour": end_h, "ToMinute": _MINUTES.get(end_m, "Zero")})
    by_day: dict[str, list] = {}
    for b in blocks:
        by_day.setdefault(b["Day"], []).append(b)
    for day, items in by_day.items():
        minutes = sorted((b["FromHour"] * 60 + list(_MINUTES.values()).index(b["FromMinute"]) * 15,
                          b["ToHour"] * 60 + list(_MINUTES.values()).index(b["ToMinute"]) * 15) for b in items)
        if any(a[1] > b[0] for a, b in zip(minutes, minutes[1:])):
            errors.append(f"ad_schedule windows on {day} overlap.")
        if len(items) > 6:
            errors.append(f"At most 6 ad_schedule windows on {day}.")
    return blocks


# Microsoft: "the maximum number of day and time criteria per day that you
# can set is seven" (per campaign or ad group).
_MAX_WINDOWS_PER_DAY = 7


def _block_span(b: dict) -> tuple[str, int, int]:
    """(day, start minute, end minute) of a DayTime window."""
    rev = {v: k for k, v in _MINUTES.items()}
    return (str(b.get("Day")), int(b.get("FromHour") or 0) * 60 + rev.get(b.get("FromMinute"), 0),
            int(b.get("ToHour") or 0) * 60 + rev.get(b.get("ToMinute"), 0))


def _schedule_steps(current: list[dict], blocks: list[dict], campaign_id: str) -> dict:
    """Order a schedule replacement the way Microsoft allows it.

    A DayTime window cannot be edited, only deleted and added, and windows
    in a campaign must not overlap. Unchanged windows stay; new windows that
    overlap none of today's go in first; then the old ones are deleted; then
    the remaining new ones are added. ``gap`` is True when, between the last
    two steps, the campaign has no window at all (it then serves at all hours).
    """
    old = {_block_span(c): c for c in current}
    new = {_block_span(b): b for b in blocks}
    keep = set(old) & set(new)
    remove = [old[s]["criterion_id"] for s in old if s not in keep]
    to_add = [new[s] for s in new if s not in keep]

    def overlaps(span: tuple[str, int, int]) -> bool:
        return any(o[0] == span[0] and span[1] < o[2] and o[1] < span[2] for o in old if o not in keep)

    def item(b: dict) -> dict:
        return {"Type": "BiddableCampaignCriterion", "CampaignId": campaign_id,
                "Criterion": {"Type": "DayTimeCriterion", **b},
                "CriterionBid": {"Type": "BidMultiplier", "Multiplier": 0}}

    # Before the deletes, a day still holds all of today's windows, and
    # Microsoft allows at most seven per day: add early only what fits.
    room = {}
    for span in old:
        room[span[0]] = room.get(span[0], _MAX_WINDOWS_PER_DAY) - 1
    first, after = [], []
    for b in to_add:
        span = _block_span(b)
        if not overlaps(span) and room.get(span[0], _MAX_WINDOWS_PER_DAY) > 0:
            room[span[0]] = room.get(span[0], _MAX_WINDOWS_PER_DAY) - 1
            first.append(item(b))
        else:
            after.append(item(b))
    return {"schedule_add_first": first, "schedule_delete": remove, "schedule_add_after": after,
            "schedule_kept": [_block_label(old[s]) for s in old if s in keep],
            "gap": bool(after) and not keep and not first}


def _block_label(b: dict) -> str:
    rev = {v: k for k, v in _MINUTES.items()}
    return f"{b['Day'][:3]} {b['FromHour']:02d}:{rev.get(b['FromMinute'], 0):02d}–{b['ToHour']:02d}:{rev.get(b['ToMinute'], 0):02d}"


def update_microsoft_campaign_targeting(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    location_ids: list[str] | None = None,
    excluded_location_ids: list[str] | None = None,
    location_intent: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft targeting changes: included and excluded locations, location intent, ad schedule."""
    operation = "microsoft_update_targeting"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    include = _clean(location_ids) if location_ids is not None else None
    exclude = _clean(excluded_location_ids) if excluded_location_ids is not None else None
    if include is not None and not include:
        errors.append("An empty location list would let the campaign serve everywhere; pass at least one location id.")
    if include and exclude and set(include) & set(exclude):
        errors.append(f"Location(s) {sorted(set(include) & set(exclude))} cannot be both included and excluded.")
    bad_ids = [i for i in (include or []) + (exclude or []) if not i.isdigit()]
    if bad_ids:
        errors.append(f"Location ids are numbers (search_microsoft_locations); got {bad_ids}.")
    intent = (location_intent or "").strip()
    if intent and intent not in INTENTS:
        errors.append(f"location_intent must be one of {list(INTENTS)}.")
    blocks = _schedule_blocks(ad_schedule, errors) if ad_schedule is not None else None
    if errors:
        return _validation_error(errors)
    if include is None and exclude is None and not intent and blocks is None:
        return {"error": "Nothing to change: pass location_ids, excluded_location_ids, location_intent or ad_schedule."}
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "campaign", campaign_id)
    if not found:
        return {"error": f"Microsoft campaign '{campaign_id}' was not found in account {account}."}
    campaign = found["entity"]
    current = _current_targeting(config, account, customer, campaign_id)
    names: dict[str, str] = {}
    try:
        from adloop.microsoft_ads.read import _GEO_CACHE, location_names

        if _GEO_CACHE.get("rows"):
            names = location_names(_GEO_CACHE["rows"], (include or []) + (exclude or []))
    except Exception:  # noqa: BLE001 — names are a convenience
        names = {}
    label = lambda i: names.get(i) or current["included"].get(i, {}).get("name") or current["excluded"].get(i, {}).get("name") or f"id {i}"  # noqa: E731
    plan_ops: dict[str, Any] = {"add": [], "delete": [], "update": [],
                                "schedule_add_first": [], "schedule_delete": [], "schedule_add_after": []}
    shown: dict[str, Any] = {}
    warnings: list[str] = []
    if include is not None:
        add = [i for i in include if i not in current["included"]]
        drop = [current["included"][i]["criterion_id"] for i in current["included"] if i not in include]
        plan_ops["add"] += [{"Type": "BiddableCampaignCriterion", "CampaignId": campaign_id,
                             "Criterion": {"Type": "LocationCriterion", "LocationId": int(i)},
                             "CriterionBid": {"Type": "BidMultiplier", "Multiplier": 0}} for i in add]
        plan_ops["delete"] += drop
        shown["locations"] = {"from": [v["name"] for v in current["included"].values()],
                              "to": [label(i) for i in include], "added": [label(i) for i in add],
                              "removed": [current["included"][i]["name"] for i in current["included"] if i not in include]}
    if exclude is not None:
        add = [i for i in exclude if i not in current["excluded"]]
        drop = [current["excluded"][i]["criterion_id"] for i in current["excluded"] if i not in exclude]
        plan_ops["add"] += [{"Type": "NegativeCampaignCriterion", "CampaignId": campaign_id,
                             "Criterion": {"Type": "LocationCriterion", "LocationId": int(i)}} for i in add]
        plan_ops["delete"] += drop
        shown["excluded_locations"] = {"from": [v["name"] for v in current["excluded"].values()],
                                       "to": [label(i) for i in exclude]}
    if intent and intent != (current["intent"] or {}).get("option"):
        item = {"Type": "BiddableCampaignCriterion", "CampaignId": campaign_id,
                "Criterion": {"Type": "LocationIntentCriterion", "IntentOption": intent}}
        if current["intent"]:
            plan_ops["update"].append({**item, "Id": current["intent"]["criterion_id"]})
        else:
            plan_ops["add"].append(item)
        shown["location_intent"] = {"from": (current["intent"] or {}).get("option") or "PeopleInOrSearchingForOrViewingPages (default)",
                                    "to": intent}
        if intent != "PeopleIn":
            warnings.append("This intent also reaches people outside the locations who search for or about them.")
    if blocks is not None:
        steps = _schedule_steps(current["schedule"], blocks, campaign_id)
        gap = steps.pop("gap")
        plan_ops.update(steps)
        # The campaign decides whose clock the hours follow.
        searcher_time = bool(campaign.get("AdScheduleUseSearcherTimeZone"))
        current["searcher_time_zone"] = searcher_time
        zone = "each searcher's own time zone" if searcher_time else f"the account time zone ({meta['time_zone']})"
        shown["ad_schedule"] = {"from": [_block_label(b) for b in current["schedule"]] or "all hours",
                                "to": [_block_label(b) for b in blocks] or "all hours",
                                "hours_in": zone}
        if searcher_time:
            warnings.append("This campaign runs its ad schedule in each searcher's own time zone "
                            "(AdScheduleUseSearcherTimeZone), not the account's: 09:00 means 09:00 where the searcher is.")
        else:
            warnings.append(f"Ad schedule hours are in the account time zone ({meta['time_zone']}), not the searcher's.")
        if blocks and not current["schedule"]:
            warnings.append("The campaign serves at all hours today; this schedule switches every other hour off.")
        if gap:
            warnings.append("Microsoft cannot edit a schedule window, only delete and add one, and windows may not "
                            "overlap: the old windows are removed before the new ones are added, so for a moment "
                            "the campaign has no schedule and serves at all hours. If adding the new windows then "
                            "fails, the apply reports it (PARTIAL_APPLY) and the campaign stays unscheduled until "
                            "the schedule is added again.")
    if not any(v for k, v in plan_ops.items() if k != "schedule_kept"):
        return {"error": "Nothing to change: the targeting already matches."}
    return _plan(
        operation, "campaign", campaign_id, account,
        _base_changes(account, customer, entity_name=campaign.get("Name"), changes=shown, operations=plan_ops,
                      before=current),
        warnings=warnings,
        double=bool(blocks) and not current["schedule"],
    )


# ---------------------------------------------------------------------------
# New campaign
# ---------------------------------------------------------------------------


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
    langs = _clean(languages)
    if not langs:
        errors.append("languages is required, e.g. ['English']: without it ads reach every language.")
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
    warnings: list[str] = []
    if strategy == "MaxConversions" and _counted_goals(config, account, customer) == 0:
        warnings.append("No active conversion goal counts toward Conversions: MaxConversions would have nothing to optimize for.")
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
    return _plan(
        operation, "campaign", "", account,
        _base_changes(
            account, customer, entity_name=name, status_on_create="Paused", daily_budget=budget,
            currency=meta["currency"], monthly_maximum=round(budget * 30.4, 2), bidding=_bidding(campaign["BiddingScheme"]),
            languages=langs, location_ids=include, excluded_location_ids=exclude, location_intent=location_intent,
            time_zone=meta["time_zone"], tracking_url_template=campaign.get("TrackingUrlTemplate"),
            final_url_suffix=campaign.get("FinalUrlSuffix"), campaign=campaign, criteria=criteria,
        ),
        warnings=warnings + [
            "Created paused. Add ad groups, keywords, ads and extensions, then enable it.",
        ],
    )


# ---------------------------------------------------------------------------
# Ad groups and keywords
# ---------------------------------------------------------------------------


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


def _active_broad_keywords(config, account: str, customer: str, campaign_id: str) -> list[dict]:
    return [
        k for g in _ad_groups(config, account, customer, campaign_id)
        for k in _keywords(config, account, customer, str(g.get("Id")))
        if k.get("MatchType") == "Broad" and k.get("Status") == "Active"
    ]


def _check_bid(current: float | None, proposed: float | None, config) -> None:
    """Bid-increase cap against the current effective bid (raises SafetyViolation)."""
    from adloop.safety.guards import check_bid_increase

    if current and proposed is not None:
        check_bid_increase(float(current), float(proposed), config.safety)


def _refuse_broad_on_manual(rows: list[dict], campaign: dict, errors: list[str]) -> None:
    strategy = _strategy_name(campaign.get("BiddingScheme"))
    broad = [r["Text"] for r in rows if r["MatchType"] == "Broad"]
    if broad and strategy not in _SMART:
        errors.append(
            f"Broad match on a {strategy or 'manual'}-bid campaign wastes spend; use Phrase or Exact for "
            f"{broad[:5]}, or move the campaign to MaxConversions first."
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
    _refuse_broad_on_manual(rows, campaign, errors)
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
        operation, "ad_group", "", account,
        _base_changes(account, customer, entity_name=name, campaign_id=str(campaign_id), campaign_name=campaign.get("Name"),
                      campaign_status=campaign.get("Status"), cpc_bid=_money(group.get("CpcBid")), network=network,
                      status_on_create="Active (its keywords too); nothing serves until an ad in it is enabled",
                      currency=meta["currency"],
                      keywords=[f"{r['Text']} [{r['MatchType']}]" + (f" bid {r['Bid']['Amount']}" if r.get("Bid") else "")
                                for r in rows],
                      ad_group=group, keyword_rows=rows),
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
        if _strategy_name(campaign.get("BiddingScheme")) in _SMART:
            warnings.append("The campaign uses automated bidding, which ignores the ad group's default bid.")
    if network and network != group.get("Network"):
        update["Network"] = network
        shown["network"] = {"from": group.get("Network"), "to": network}
        touched.append("Network")
    adjust = None
    if audience_ads_bid_adjustment is not None and int(audience_ads_bid_adjustment) != group.get("AudienceAdsBidAdjustment"):
        adjust = int(audience_ads_bid_adjustment)
        update["AudienceAdsBidAdjustment"] = adjust
        shown["audience_network"] = {"from": group.get("AudienceAdsBidAdjustment"), "to": adjust,
                                     "meaning": "off" if adjust == -100 else f"{adjust:+d}% bids on the Audience Network"}
        touched.append("AudienceAdsBidAdjustment")
    if len(update) == 1:
        return {"error": "Nothing to change: pass at least one setting that differs from the ad group."}
    return _plan(
        operation, "ad_group", str(ad_group_id), account,
        _base_changes(account, customer, entity_name=group.get("Name"), campaign_id=str(campaign.get("Id")),
                      campaign_name=campaign.get("Name"), changes=shown, update=update,
                      update_audience_adjustment=adjust is not None,
                      before={k: copy.deepcopy(group.get(k)) for k in sorted(set(touched))}),
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
    _refuse_broad_on_manual(rows, campaign, errors)
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
        operation, "ad_group", str(ad_group_id), account,
        _base_changes(account, customer, entity_name=group.get("Name"), campaign_name=campaign.get("Name"),
                      status_on_create="Active", ad_group_status=group.get("Status"),
                      campaign_status=campaign.get("Status"),
                      keywords=[f"{r['Text']} [{r['MatchType']}]" + (f" bid {r['Bid']['Amount']}" if r.get("Bid") else "")
                                for r in new_rows],
                      keyword_rows=new_rows),
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
    group = found["entity"]
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
        operation, "ad_group", str(ad_group_id), account,
        _base_changes(account, customer, entity_name=group.get("Name"), currency=meta["currency"], bids=shown,
                      keyword_rows=rows, before=before, default_bid_before=copy.deepcopy(group.get("CpcBid"))),
    )


# ---------------------------------------------------------------------------
# Negative keywords: direct and shared lists
# ---------------------------------------------------------------------------


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
        operation, kind, str(entity.get("Id")), account,
        _base_changes(account, customer, entity_name=entity.get("Name"), entity_kind=entity_type,
                      parent_id=str(campaign.get("Id")),
                      negatives=[f"{r['Text']} [{r['MatchType']}]" for r in new_rows], negative_rows=new_rows),
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
        operation, "negative_keyword_list", "", account,
        _base_changes(account, customer, entity_name=list_name, negatives=[f"{r['Text']} [{r['MatchType']}]" for r in rows],
                      attach_to=[campaigns[c].get("Name") for c in cids], campaign_ids=cids, negative_rows=rows),
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
        operation, "negative_keyword_list", str(list_id), account,
        _base_changes(account, customer, entity_name=lists[str(list_id)].get("Name"), attached_campaigns=len(attached),
                      negatives=[f"{r['Text']} [{r['MatchType']}]" for r in new_rows], negative_rows=new_rows),
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
        operation, "negative_keyword_list", str(list_id), account,
        _base_changes(account, customer, entity_name=lists[str(list_id)].get("Name"),
                      campaigns=[campaigns[c].get("Name") for c in targets], campaign_ids=targets),
        warnings=warnings,
    )


def attach_microsoft_negative_keyword_list(config, *, ad_account_id="", list_id="", campaign_ids=None) -> dict:
    return _draft_list_link(config, attach=True, ad_account_id=ad_account_id, list_id=list_id, campaign_ids=campaign_ids)


def detach_microsoft_negative_keyword_list(config, *, ad_account_id="", list_id="", campaign_ids=None) -> dict:
    return _draft_list_link(config, attach=False, ad_account_id=ad_account_id, list_id=list_id, campaign_ids=campaign_ids)


# ---------------------------------------------------------------------------
# Responsive search ads
# ---------------------------------------------------------------------------


def _assets(items: list, limit: int, slots: tuple, field: str, errors: list[str]) -> list[dict]:
    links = []
    for i, item in enumerate(items or []):
        text, pin = (item.get("text"), item.get("pinned_field")) if isinstance(item, dict) else (item, None)
        text = str(text or "").strip()
        if not text or len(text) > limit:
            errors.append(f"{field}[{i}] is 1–{limit} characters (got {len(text)}): '{text}'.")
        link: dict[str, Any] = {"Asset": {"Type": "TextAsset", "Text": text}}
        if pin:
            normalized = _PINS.get(str(pin).strip().lower().replace(" ", "_"))
            if normalized not in slots:
                errors.append(f"{field}[{i}].pinned_field must be one of {list(slots)}.")
            link["PinnedField"] = normalized
        links.append(link)
    texts = [l["Asset"]["Text"].lower() for l in links]
    if len(set(texts)) != len(texts):
        errors.append(f"{field} repeat the same text.")
    for slot in slots:
        pinned = [l for l in links if l.get("PinnedField") == slot]
        unpinned = [l for l in links if not l.get("PinnedField")]
        if not pinned and not unpinned:
            errors.append(f"Nothing can fill {slot}: pin an asset there or leave some unpinned.")
    return links


def _rsa_fields(headlines, descriptions, final_url, path1, path2, errors, warnings, *, partial: bool) -> dict:
    fields: dict[str, Any] = {}
    if headlines is not None or not partial:
        h = _assets(headlines or [], 30, ("Headline1", "Headline2", "Headline3"), "headlines", errors)
        if not 3 <= len(h) <= 15:
            errors.append(f"An RSA takes 3–15 headlines (got {len(h)}).")
        fields["Headlines"] = h
    if descriptions is not None or not partial:
        d = _assets(descriptions or [], 90, ("Description1", "Description2"), "descriptions", errors)
        if not 2 <= len(d) <= 4:
            errors.append(f"An RSA takes 2–4 descriptions (got {len(d)}).")
        fields["Descriptions"] = d
    if final_url or not partial:
        _check_url((final_url or "").strip(), errors, warnings)
        fields["FinalUrls"] = [(final_url or "").strip()]
    for name, value in (("Path1", path1), ("Path2", path2)):
        if value is None:
            continue
        value = value.strip()
        if len(value) > 15 or "/" in value:
            errors.append(f"{name.lower()} is at most 15 characters, without '/'.")
        fields[name] = value
    if fields.get("Path2") and not (fields.get("Path1") or (partial and path1 is None)):
        errors.append("path2 needs path1.")
    if not partial and fields.get("Headlines") and len(fields["Headlines"]) < 8:
        warnings.append("8–15 distinct headlines give Microsoft more combinations to test.")
    pins = [l for l in fields.get("Headlines", []) + fields.get("Descriptions", []) if l.get("PinnedField")]
    if pins:
        warnings.append(f"{len(pins)} asset(s) are pinned; pinning limits the combinations Microsoft can show.")
    return fields


def draft_microsoft_responsive_search_ad(
    config: AdLoopConfig, *, ad_account_id: str = "", ad_group_id: str = "", headlines: list | None = None,
    descriptions: list | None = None, final_url: str = "", path1: str = "", path2: str = "",
) -> dict:
    """Draft a responsive search ad, created PAUSED."""
    operation = "microsoft_create_rsa"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    fields = _rsa_fields(headlines, descriptions, final_url, path1, path2, errors, warnings, partial=False)
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad_group", str(ad_group_id))
    if not found:
        return {"error": f"Microsoft ad group '{ad_group_id}' was not found in account {account}."}
    group, campaign = found["entity"], found["campaign"]
    ad = {"Type": "ResponsiveSearch", "Status": "Paused", **fields}
    for key in ("Path1", "Path2"):
        if not ad.get(key):
            ad.pop(key, None)
    if "Path1" not in ad:
        ad.pop("Path2", None)
        warnings.append("No display paths: path1/path2 make the display URL more specific.")
    return _plan(
        operation, "ad_group", str(ad_group_id), account,
        _base_changes(account, customer, entity_name=group.get("Name"), campaign_name=campaign.get("Name"),
                      status_on_create="Paused",
                      headlines=[l["Asset"]["Text"] + (f" [pinned {l['PinnedField']}]" if l.get("PinnedField") else "") for l in fields["Headlines"]],
                      descriptions=[l["Asset"]["Text"] for l in fields["Descriptions"]],
                      final_url=fields["FinalUrls"][0], path1=ad.get("Path1"), path2=ad.get("Path2"), ad=ad),
        warnings=warnings,
    )


def update_microsoft_responsive_search_ad(
    config: AdLoopConfig, *, ad_account_id: str = "", ad_id: str = "", headlines: list | None = None,
    descriptions: list | None = None, final_url: str = "", path1: str | None = None, path2: str | None = None,
) -> dict:
    """Draft RSA changes: headlines or descriptions (each list replaced whole), landing page, paths."""
    operation = "microsoft_update_rsa"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    fields = _rsa_fields(headlines, descriptions, final_url, path1, path2, errors, warnings, partial=True)
    if errors:
        return _validation_error(errors)
    if not fields:
        return {"error": "Nothing to change: pass headlines, descriptions, final_url, path1 or path2."}
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad", str(ad_id))
    if not found:
        return {"error": f"Microsoft ad '{ad_id}' was not found in account {account}."}
    ad, group = found["entity"], found["ad_group"]
    if ad.get("Type") != "ResponsiveSearch":
        return _validation_error([f"Ad {ad_id} is a {ad.get('Type')} ad; only responsive search ads are edited here."])
    shown = {}
    for key, value in fields.items():
        if key in ("Headlines", "Descriptions"):
            shown[key.lower()] = {"from": [l.get("Asset", {}).get("Text") for l in ad.get(key) or []],
                                  "to": [l["Asset"]["Text"] for l in value]}
        else:
            shown[key.lower()] = {"from": ad.get(key), "to": value}
    warnings.append("Edited ads go back to Microsoft's editorial review.")
    return _plan(
        operation, "ad", str(ad_id), account,
        _base_changes(account, customer, entity_name=f"RSA {ad_id}", ad_group_id=str(group.get("Id")),
                      ad_group_name=group.get("Name"), changes=shown,
                      update={"Type": "ResponsiveSearch", "Id": str(ad_id), **fields},
                      before={k: copy.deepcopy(ad.get(k)) for k in fields}),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Ad extensions: sitelinks, callouts, structured snippets, call
# ---------------------------------------------------------------------------


def _extension_target(config, account, customer, campaign_id: str, scope: str) -> tuple[str, str, str] | dict:
    if scope == "account":
        if campaign_id:
            return _validation_error(["Pass campaign_id or scope='account', not both."])
        return "Account", account, f"account {account}"
    if not campaign_id:
        return _validation_error(["campaign_id is required (or scope='account' for every campaign without its own)."])
    found = find_entity(config, account, customer, "campaign", str(campaign_id))
    if not found:
        return {"error": f"Microsoft campaign '{campaign_id}' was not found in account {account}."}
    return "Campaign", str(campaign_id), f"campaign '{found['entity'].get('Name')}'"


def _associated_extensions(config, account, customer, association_type: str, entity_id: str) -> list[dict]:
    body = {"AccountId": account, "AdExtensionType": _EXTENSION_TYPES, "AssociationType": association_type,
            "EntityIds": [int(entity_id)]}
    payload = campaign_request(config, "AdExtensionsAssociations/Query", body, account_id=account, customer_id=customer)
    out = []
    for collection in payload.get("AdExtensionAssociationCollection") or []:
        for assoc in (collection or {}).get("AdExtensionAssociations") or []:
            if isinstance(assoc, dict) and isinstance(assoc.get("AdExtension"), dict):
                out.append(assoc)
    return out


def _draft_extensions(config, *, ad_account_id, campaign_id, scope, operation, kind, extensions, shown, warnings) -> dict:
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    meta, account, customer = _ctx(config, ad_account_id)
    target = _extension_target(config, account, customer, str(campaign_id or ""), (scope or "").strip().lower())
    if isinstance(target, dict):
        return target
    association, entity_id, where = target
    existing = _associated_extensions(config, account, customer, association, entity_id)
    same = [a["AdExtension"] for a in existing if a["AdExtension"].get("Type") == kind]
    if kind == "CallAdExtension" and same:
        return _validation_error([f"{where} already has a call extension; Microsoft allows one per campaign."])
    def key(ext):  # noqa: E306
        return json.dumps({k: ext.get(k) for k in ("Text", "DisplayText", "Header", "Values", "PhoneNumber")}, sort_keys=True).lower()
    have = {key(e) for e in same}
    new = [e for e in extensions if key(e) not in have]
    if not new:
        return {"error": f"Every one of these is already on {where}."}
    if len(new) < len(extensions):
        warnings.append(f"Skipped {len(extensions) - len(new)} already on {where}.")
    return _plan(
        operation, association.lower(), entity_id, account,
        _base_changes(account, customer, entity_name=where, association_type=association, entity_id=entity_id,
                      **shown, extensions=new),
        warnings=warnings,
    )


def draft_microsoft_sitelinks(config, *, ad_account_id="", campaign_id="", scope="", sitelinks=None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    exts = []
    for i, s in enumerate(sitelinks or []):
        if not isinstance(s, dict):
            errors.append(f"sitelinks[{i}] must be {{link_text, final_url, description1, description2}}.")
            continue
        text = str(s.get("link_text") or "").strip()
        d1, d2 = str(s.get("description1") or "").strip(), str(s.get("description2") or "").strip()
        limit = 25 if (d1 or d2) else 35
        if not text or len(text) > limit:
            errors.append(f"sitelinks[{i}].link_text is 1–{limit} characters.")
        if bool(d1) != bool(d2):
            errors.append(f"sitelinks[{i}] needs both descriptions or neither.")
        if len(d1) > 35 or len(d2) > 35:
            errors.append(f"sitelinks[{i}] descriptions are at most 35 characters each.")
        _check_url(str(s.get("final_url") or "").strip(), errors, warnings, field=f"sitelinks[{i}].final_url")
        ext = {"Type": "SitelinkAdExtension", "DisplayText": text, "FinalUrls": [str(s.get("final_url") or "").strip()]}
        if d1:
            ext.update(Description1=d1, Description2=d2)
        exts.append(ext)
    if not exts and not errors:
        errors.append("sitelinks needs at least one sitelink.")
    if len(exts) > 20:
        errors.append("At most 20 sitelinks per campaign.")
    if errors:
        return _validation_error(errors)
    if len(exts) < 4:
        warnings.append("Four or more sitelinks let Microsoft show the larger sitelink formats.")
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope=scope,
                             operation="microsoft_add_sitelinks", kind="SitelinkAdExtension", extensions=exts,
                             shown={"sitelinks": [
                                 f"{e['DisplayText']} → {e['FinalUrls'][0]}"
                                 + (f" ({e['Description1']} / {e['Description2']})" if e.get("Description1") else "")
                                 for e in exts]},
                             warnings=warnings)


def draft_microsoft_callouts(config, *, ad_account_id="", campaign_id="", scope="", callouts=None) -> dict:
    errors: list[str] = []
    texts = _clean(callouts)
    for t in texts:
        if len(t) > 25:
            errors.append(f"Callout '{t}' is longer than 25 characters.")
    if not texts:
        errors.append("callouts needs at least one callout.")
    if errors:
        return _validation_error(errors)
    warnings = [] if len(texts) >= 2 else ["A campaign needs at least 2 callouts before Microsoft shows them."]
    exts = [{"Type": "CalloutAdExtension", "Text": t} for t in dict.fromkeys(texts)]
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope=scope,
                             operation="microsoft_add_callouts", kind="CalloutAdExtension", extensions=exts,
                             shown={"callouts": [e["Text"] for e in exts]}, warnings=warnings)


def draft_microsoft_structured_snippet(config, *, ad_account_id="", campaign_id="", scope="", header="", values=None) -> dict:
    errors: list[str] = []
    vals = list(dict.fromkeys(_clean(values)))
    if not (header or "").strip():
        errors.append("header is required (one of Microsoft's headers, e.g. Services, Brands, Types).")
    if not 3 <= len(vals) <= 10:
        errors.append(f"A structured snippet takes 3–10 values (got {len(vals)}).")
    for v in vals:
        if len(v) > 25:
            errors.append(f"Value '{v}' is longer than 25 characters.")
    if errors:
        return _validation_error(errors)
    ext = {"Type": "StructuredSnippetAdExtension", "Header": header.strip(), "Values": vals}
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope=scope,
                             operation="microsoft_add_structured_snippet", kind="StructuredSnippetAdExtension",
                             extensions=[ext], shown={"snippet": f"{ext['Header']}: {', '.join(vals)}"}, warnings=[])


def draft_microsoft_call_extension(config, *, ad_account_id="", campaign_id="", phone_number="", country_code="") -> dict:
    errors: list[str] = []
    number = (phone_number or "").strip()
    country = (country_code or "").strip().upper()
    if not number or len(number) > 35:
        errors.append("phone_number is required (at most 35 characters).")
    if len(country) != 2 or not country.isalpha():
        errors.append("country_code is the two-letter country of the number, e.g. US.")
    if errors:
        return _validation_error(errors)
    ext = {"Type": "CallAdExtension", "PhoneNumber": number, "CountryCode": country, "IsCallOnly": False}
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope="",
                             operation="microsoft_add_call_extension", kind="CallAdExtension", extensions=[ext],
                             shown={"phone_number": number, "country_code": country},
                             warnings=["Use the call-tracking number that should receive Microsoft calls."])


def get_microsoft_ad_extensions(config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "") -> dict:
    """Sitelinks, callouts, snippets and call extensions on campaigns and the account."""
    meta, account, customer = _ctx(config, ad_account_id)
    campaigns = [c for c in _campaigns(config, account, customer) if not campaign_id or str(c.get("Id")) == str(campaign_id)]
    rows = []
    targets = [("Campaign", str(c.get("Id")), c.get("Name")) for c in campaigns]
    if not campaign_id:
        targets.append(("Account", account, "account"))
    for association, entity_id, label in targets:
        for a in _associated_extensions(config, account, customer, association, entity_id):
            ext = a["AdExtension"]
            kind = str(ext.get("Type") or "")
            rows.append({
                "level": association.lower(), "entity": label, "type": kind.replace("AdExtension", ""),
                "extension_id": str(ext.get("Id")),
                "content": ext.get("Text") or ext.get("DisplayText") or ext.get("PhoneNumber")
                or (f"{ext.get('Header')}: {', '.join(ext.get('Values') or [])}" if ext.get("Header") else None),
                "final_urls": ext.get("FinalUrls"),
                "editorial_status": a.get("EditorialStatus"),
                "remove_id": f"{ext.get('Id')}~{entity_id}~{association}",
            })
    insights = []
    for c in campaigns:
        mine = [r for r in rows if r["entity"] == c.get("Name")]
        if not any(r["type"] == "Sitelink" for r in mine):
            insights.append(f"Campaign '{c.get('Name')}' has no sitelinks of its own.")
    rejected = [r for r in rows if r["editorial_status"] in ("Disapproved", "Inactive")]
    if rejected:
        insights.append(f"{len(rejected)} extension(s) are disapproved or inactive.")
    return {"ad_account_id": account, "extensions": rows, "total": len(rows), "insights": insights}


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

REMOVABLE = ("campaign", "ad_group", "ad", "keyword", "negative_keyword", "negative_list_item",
             "campaign_criterion", "extension_association")


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
    request: dict[str, Any]
    if entity_type in ("campaign", "ad_group", "ad", "keyword"):
        found = find_entity(config, account, customer, entity_type, entity_id)
        if not found:
            return {"error": f"Microsoft {entity_type.replace('_', ' ')} '{entity_id}' was not found."}
        entity = found["entity"]
        label = entity.get("Name") or entity.get("Text") or f"{entity.get('Type')} ad {entity_id}"
        shown = {"name": label, "campaign": found["campaign"].get("Name"),
                 "ad_group": (found.get("ad_group") or {}).get("Name"), "current_status": entity.get("Status")}
        if entity_type == "campaign":
            request = {"path": "Campaigns", "body": {"AccountId": account, "CampaignIds": [entity_id]}}
        elif entity_type == "ad_group":
            request = {"path": "AdGroups", "body": {"CampaignId": str(found["campaign"].get("Id")), "AdGroupIds": [entity_id]}}
        elif entity_type == "ad":
            request = {"path": "Ads", "body": {"AdGroupId": str(found["ad_group"].get("Id")), "AdIds": [entity_id]}}
        else:
            request = {"path": "Keywords", "body": {"AdGroupId": str(found["ad_group"].get("Id")), "KeywordIds": [entity_id]}}
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
        elif entity_type == "campaign_criterion" and len(parts) == 2:
            cid, crit = parts
            request = {"path": "CampaignCriterions", "body": {"CampaignCriterionIds": [int(crit)], "CampaignId": int(cid),
                                                              "CriterionType": "Targets"}}
            shown = {"campaign_id": cid, "criterion_id": crit}
        elif entity_type == "extension_association" and len(parts) == 3:
            ext, owner, association = parts
            request = {"path": "AdExtensionsAssociations", "body": {"AccountId": account, "AssociationType": association,
                                                                     "AdExtensionIdToEntityIdAssociations": [
                                                                         {"AdExtensionId": int(ext), "EntityId": int(owner)}]}}
            shown = {"extension_id": ext, "removed_from": f"{association} {owner}"}
        else:
            return _validation_error([
                "entity_id format: negative_keyword 'Campaign~<id>~<negativeId>' or 'AdGroup~<id>~<negativeId>', "
                "negative_list_item '<listId>~<itemId>', campaign_criterion '<campaignId>~<criterionId>', "
                "extension_association '<extensionId>~<entityId>~<Campaign|Account>' (the remove_id values the read tools return)."
            ])
    warnings = ["Deleting cannot be undone; pausing keeps the history and lets it come back."] if entity_type in (
        "campaign", "ad_group", "ad", "keyword") else []
    return _plan(operation, entity_type, entity_id, account,
                 _base_changes(account, customer, entity_name=shown.get("name") or entity_id, target=shown, request=request),
                 warnings=warnings, double=True)


# ---------------------------------------------------------------------------
# Dry run and apply
# ---------------------------------------------------------------------------

OPERATIONS = {
    "microsoft_update_campaign", "microsoft_update_targeting", "microsoft_create_campaign",
    "microsoft_create_ad_group", "microsoft_update_ad_group", "microsoft_add_keywords",
    "microsoft_update_keyword_bids", "microsoft_add_negative_keywords", "microsoft_create_negative_list",
    "microsoft_add_to_negative_list", "microsoft_attach_negative_list", "microsoft_detach_negative_list",
    "microsoft_create_rsa", "microsoft_update_rsa", "microsoft_add_sitelinks", "microsoft_add_callouts",
    "microsoft_add_structured_snippet", "microsoft_add_call_extension", "microsoft_remove_entity",
}


def _require_unchanged(entity: dict, before: dict, what: str) -> None:
    for key, value in (before or {}).items():
        if not _same(entity.get(key), value):
            raise ValueError(f"The {what}'s {key} changed since the draft; draft again.")


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read what the plan touches and re-check the gates; sends nothing."""
    from adloop.safety.guards import check_budget_cap

    c = plan.changes or {}
    account, customer = c["ad_account_id"], c.get("customer_id", "")
    op = plan.operation
    checks: dict[str, Any] = {"ad_account_id": account}

    def entity(kind: str, eid: str) -> dict:
        found = find_entity(config, account, customer, kind, eid)
        if not found:
            raise ValueError(f"{kind.replace('_', ' ')} '{eid}' no longer exists.")
        return found

    if op == "microsoft_update_campaign":
        campaign = entity("campaign", plan.entity_id)["entity"]
        _require_unchanged(campaign, c["before"], "campaign")
        scheme = c["update"].get("BiddingScheme")
        if scheme:
            _check_bid((((campaign.get("BiddingScheme") or {}).get("MaxCpc")) or {}).get("Amount"),
                       (scheme.get("MaxCpc") or {}).get("Amount"), config)
            if scheme.get("Type") in _MANUAL and _active_broad_keywords(config, account, customer, plan.entity_id):
                raise ValueError("The campaign now has active broad-match keywords; manual bidding with broad match "
                                 "wastes spend. Change them to phrase or exact first.")
    elif op == "microsoft_update_targeting":
        ops = c.get("operations") or {}
        scheduled = {str(s.get("criterion_id")) for s in (c["before"].get("schedule") or [])}
        if (any(((x.get("Criterion") or {}).get("Type")) == "DayTimeCriterion" for x in ops.get("add") or [])
                or any(str(i) in scheduled for i in ops.get("delete") or [])):
            raise ValueError("This plan changes the ad schedule and was drafted by an earlier AdLoop version, "
                             "which did not check the schedule's time zone mode or order the windows Microsoft "
                             "allows; draft it again.")
        campaign = entity("campaign", plan.entity_id)["entity"]
        now = _current_targeting(config, account, customer, plan.entity_id)
        if "searcher_time_zone" in c["before"]:
            now["searcher_time_zone"] = bool(campaign.get("AdScheduleUseSearcherTimeZone"))
            if now["searcher_time_zone"] != c["before"]["searcher_time_zone"]:
                raise ValueError("The campaign's ad schedule time zone setting (AdScheduleUseSearcherTimeZone) "
                                 "changed since the draft, so the approved hours would mean other times; draft again.")
        if not _same(now, c["before"]):
            raise ValueError("The campaign's targeting changed since the draft; draft again.")
    elif op == "microsoft_create_campaign":
        check_budget_cap(float(c["daily_budget"]), config.safety)
        if c["entity_name"].lower() in {str(x.get("Name")).lower() for x in _campaigns(config, account, customer)}:
            raise ValueError(f"A campaign named '{c['entity_name']}' now exists (an earlier attempt may have created it).")
        checks["budget_cap_ok"] = True
    elif op == "microsoft_create_ad_group":
        campaign = entity("campaign", c["campaign_id"])["entity"]
        if c["entity_name"].lower() in {str(g.get("Name")).lower() for g in _ad_groups(config, account, customer, c["campaign_id"])}:
            raise ValueError(f"An ad group named '{c['entity_name']}' now exists in the campaign.")
        errors = []
        _refuse_broad_on_manual(c["keyword_rows"], campaign, errors)
        if _strategy_name(campaign.get("BiddingScheme")) in _MANUAL and not c["ad_group"].get("CpcBid"):
            errors.append("The campaign now uses manual bidding, which needs a default bid for the ad group.")
        if errors:
            raise ValueError("; ".join(errors))
    elif op == "microsoft_update_ad_group":
        group = entity("ad_group", plan.entity_id)["entity"]
        _require_unchanged(group, c["before"], "ad group")
        if "CpcBid" in c["update"]:
            _check_bid(_money(group.get("CpcBid")), c["update"]["CpcBid"]["Amount"], config)
    elif op == "microsoft_add_keywords":
        found = entity("ad_group", plan.entity_id)
        errors = []
        _refuse_broad_on_manual(c["keyword_rows"], found["campaign"], errors)
        if errors:
            raise ValueError("; ".join(errors))
        have = {(str(k.get("Text")).lower(), k.get("MatchType")) for k in _keywords(config, account, customer, plan.entity_id)
                if k.get("Status") != "Deleted"}
        again = [r["Text"] for r in c["keyword_rows"] if (r["Text"].lower(), r["MatchType"]) in have]
        if again:
            raise ValueError(f"Keyword(s) {again} are in the ad group now (an earlier attempt may have added them).")
    elif op == "microsoft_update_keyword_bids":
        group = entity("ad_group", plan.entity_id)["entity"]
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
    elif op == "microsoft_add_negative_keywords":
        found = entity(plan.entity_type, plan.entity_id)
        have = {(str(n.get("Text")).lower(), n.get("MatchType")) for n in _entity_negatives(
            config, account, customer, c["entity_kind"], plan.entity_id,
            "" if plan.entity_type == "campaign" else str(found["campaign"].get("Id")))}
        again = [r["Text"] for r in c["negative_rows"] if (r["Text"].lower(), r["MatchType"]) in have]
        if again:
            raise ValueError(f"Negative(s) {again} are there now (an earlier attempt may have added them).")
    elif op == "microsoft_create_negative_list":
        if c["entity_name"].lower() in {str(x.get("Name")).lower() for x in _shared_lists(config, account, customer)}:
            raise ValueError(f"A list named '{c['entity_name']}' now exists (an earlier attempt may have created it).")
    elif op in ("microsoft_add_to_negative_list", "microsoft_attach_negative_list", "microsoft_detach_negative_list"):
        if plan.entity_id not in {str(x.get("Id")) for x in _shared_lists(config, account, customer)}:
            raise ValueError(f"Negative keyword list {plan.entity_id} no longer exists.")
        if op == "microsoft_add_to_negative_list":
            have = {(str(i.get("Text")).lower(), i.get("MatchType")) for i in _list_items(config, account, customer, plan.entity_id)}
            again = [r["Text"] for r in c["negative_rows"] if (r["Text"].lower(), r["MatchType"]) in have]
            if again:
                raise ValueError(f"Negative(s) {again} are in the list now (an earlier attempt may have added them).")
    elif op == "microsoft_create_rsa":
        found = entity("ad_group", plan.entity_id)
        errors: list[str] = []
        _check_url(c["final_url"], errors, [])
        if errors:
            raise ValueError("; ".join(errors))
        wanted = sorted(l["Asset"]["Text"] for l in c["ad"]["Headlines"])
        for ad in _ads(config, account, customer, plan.entity_id):
            if sorted(str(l.get("Asset", {}).get("Text")) for l in ad.get("Headlines") or []) == wanted:
                raise ValueError(f"Ad group '{found['entity'].get('Name')}' already has an RSA with these headlines (ad {ad.get('Id')}).")
    elif op == "microsoft_update_rsa":
        found = entity("ad", plan.entity_id)
        _require_unchanged(found["entity"], c["before"], "ad")
        if c["update"].get("FinalUrls"):
            errors = []
            _check_url(c["update"]["FinalUrls"][0], errors, [])
            if errors:
                raise ValueError("; ".join(errors))
    elif op in ("microsoft_add_sitelinks", "microsoft_add_callouts", "microsoft_add_structured_snippet",
                "microsoft_add_call_extension"):
        if c["association_type"] == "Campaign":
            entity("campaign", c["entity_id"])
        linked = _associated_extensions(config, account, customer, c["association_type"], c["entity_id"])
        if op == "microsoft_add_call_extension" and any(a["AdExtension"].get("Type") == "CallAdExtension" for a in linked):
            raise ValueError("The campaign got a call extension since the draft.")
        def key(ext):  # noqa: E306
            return json.dumps({k: ext.get(k) for k in ("Type", "Text", "DisplayText", "Header", "Values", "PhoneNumber")},
                              sort_keys=True).lower()
        now = {key(a["AdExtension"]) for a in linked}
        again = [e for e in c["extensions"] if key(e) in now]
        if again:
            raise ValueError(f"{len(again)} of these extensions are linked now (an earlier attempt may have added them).")
        if op == "microsoft_add_sitelinks":
            errors = []
            for ext in c["extensions"]:
                _check_url(ext["FinalUrls"][0], errors, [])
            if errors:
                raise ValueError("; ".join(errors))
    elif op == "microsoft_remove_entity":
        if plan.entity_type in ("campaign", "ad_group", "ad", "keyword"):
            entity(plan.entity_type, plan.entity_id)
    else:
        raise ValueError(f"Unknown Microsoft operation: {op}")
    checks["checked"] = op.replace("microsoft_", "")
    return checks


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a management plan (after :func:`preflight`, which the caller runs first)."""
    c = plan.changes or {}
    account = c["ad_account_id"]
    op = plan.operation
    progress = _Progress()

    if op == "microsoft_update_campaign":
        _call(config, c, "PUT", "Campaigns", {"AccountId": account, "Campaigns": [c["update"]]}, "campaign update")
        return {"campaign_id": plan.entity_id, "updated": sorted(k for k in c["update"] if k != "Id")}

    if op == "microsoft_update_targeting":
        ops = c["operations"]
        for chunk in _chunks(ops["add"]):
            progress.step("Adding targeting", lambda chunk=chunk: _call(
                config, c, "POST", "CampaignCriterions", {"CampaignCriterions": chunk, "CriterionType": "Targets"}, "targeting additions"))
            progress.done.append(f"added {len(chunk)} criterion(s)")
        for chunk in _chunks(ops["update"]):
            progress.step("Updating location intent", lambda chunk=chunk: _call(
                config, c, "PUT", "CampaignCriterions", {"CampaignCriterions": chunk, "CriterionType": "Targets"}, "location intent"))
            progress.done.append("updated location intent")
        for chunk in _chunks(ops["delete"]):
            progress.step("Removing old targeting", lambda chunk=chunk: _call(
                config, c, "DELETE", "CampaignCriterions",
                {"CampaignCriterionIds": [int(x) for x in chunk], "CampaignId": int(plan.entity_id), "CriterionType": "Targets"},
                "targeting removal"))
            progress.done.append(f"removed {len(chunk)} criterion(s)")
        # Schedule windows: non-overlapping new ones, then the old ones out,
        # then the rest (see _schedule_steps).
        for chunk in _chunks(ops.get("schedule_add_first") or []):
            progress.step("Adding new ad schedule windows", lambda chunk=chunk: _call(
                config, c, "POST", "CampaignCriterions", {"CampaignCriterions": chunk, "CriterionType": "Targets"}, "ad schedule"))
            progress.done.append(f"added {len(chunk)} schedule window(s)")
        for chunk in _chunks(ops.get("schedule_delete") or []):
            progress.step("Removing the old ad schedule windows", lambda chunk=chunk: _call(
                config, c, "DELETE", "CampaignCriterions",
                {"CampaignCriterionIds": [int(x) for x in chunk], "CampaignId": int(plan.entity_id), "CriterionType": "Targets"},
                "ad schedule removal"))
            progress.done.append(f"removed {len(chunk)} schedule window(s)")
        remaining = list(ops.get("schedule_kept") or [])
        if ops.get("schedule_add_first"):
            remaining.append(f"{len(ops['schedule_add_first'])} new window(s) added before the removal")
        for chunk in _chunks(ops.get("schedule_add_after") or []):
            progress.step(
                "Adding the new ad schedule windows (the windows they replace are already removed; until this "
                "succeeds the campaign keeps "
                + (", ".join(remaining) if remaining else "no ad schedule and serves at all hours") + ")",
                lambda chunk=chunk: _call(config, c, "POST", "CampaignCriterions",
                                          {"CampaignCriterions": chunk, "CriterionType": "Targets"}, "ad schedule"))
            progress.done.append(f"added {len(chunk)} schedule window(s)")
        schedule_added = len(ops.get("schedule_add_first") or []) + len(ops.get("schedule_add_after") or [])
        return {"campaign_id": plan.entity_id, "added": len(ops["add"]) + schedule_added, "updated": len(ops["update"]),
                "removed": len(ops["delete"]) + len(ops.get("schedule_delete") or [])}

    if op == "microsoft_create_campaign":
        payload = _call(config, c, "POST", "Campaigns", {"AccountId": account, "Campaigns": [c["campaign"]]}, "new campaign")
        campaign_id = str((payload.get("CampaignIds") or [None])[0])
        progress.done.append(f"created campaign {campaign_id} (paused)")
        criteria = [{**x, "CampaignId": campaign_id} for x in c["criteria"]]
        for chunk in _chunks(criteria):
            progress.step("Adding the campaign's locations", lambda chunk=chunk: _call(
                config, c, "POST", "CampaignCriterions", {"CampaignCriterions": chunk, "CriterionType": "Targets"}, "locations"))
        return {"campaign_id": campaign_id, "name": c["entity_name"], "status": "Paused",
                "next": "Add ad groups, keywords, ads and extensions, then enable it."}

    if op == "microsoft_create_ad_group":
        payload = _call(config, c, "POST", "AdGroups", {"CampaignId": c["campaign_id"], "AdGroups": [c["ad_group"]]}, "new ad group")
        ad_group_id = str((payload.get("AdGroupIds") or [None])[0])
        progress.done.append(f"created ad group {ad_group_id}")
        for chunk in _chunks(c["keyword_rows"], 1000):
            progress.step("Adding its keywords", lambda chunk=chunk: _call(
                config, c, "POST", "Keywords", {"AdGroupId": ad_group_id, "Keywords": chunk}, "keywords"))
        return {"ad_group_id": ad_group_id, "name": c["entity_name"], "keywords_added": len(c["keyword_rows"])}

    if op == "microsoft_update_ad_group":
        _call(config, c, "PUT", "AdGroups", {"CampaignId": c["campaign_id"], "AdGroups": [c["update"]],
                                             "UpdateAudienceAdsBidAdjustment": bool(c.get("update_audience_adjustment"))},
              "ad group update")
        return {"ad_group_id": plan.entity_id, "updated": sorted(k for k in c["update"] if k != "Id")}

    if op == "microsoft_add_keywords":
        payload = _call(config, c, "POST", "Keywords", {"AdGroupId": plan.entity_id, "Keywords": c["keyword_rows"]}, "keywords")
        return {"ad_group_id": plan.entity_id, "keyword_ids": [str(i) for i in payload.get("KeywordIds") or []]}

    if op == "microsoft_update_keyword_bids":
        _call(config, c, "PUT", "Keywords", {"AdGroupId": plan.entity_id, "Keywords": c["keyword_rows"]}, "keyword bids")
        return {"ad_group_id": plan.entity_id, "keywords_updated": len(c["keyword_rows"])}

    if op == "microsoft_add_negative_keywords":
        _call(config, c, "POST", "EntityNegativeKeywords", {"EntityNegativeKeywords": [
            {"EntityId": int(plan.entity_id), "EntityType": c["entity_kind"], "NegativeKeywords": c["negative_rows"]}]},
            "negative keywords")
        return {"entity": c["entity_name"], "negatives_added": len(c["negative_rows"])}

    if op == "microsoft_create_negative_list":
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

    if op == "microsoft_add_to_negative_list":
        items = [{"Type": "NegativeKeyword", **r} for r in c["negative_rows"]]
        for chunk in _chunks(items, 5000):
            progress.step("Adding to the list", lambda chunk=chunk: _call(config, c, "POST", "ListItems", {
                "ListItems": chunk, "SharedList": {"Type": _LIST_TYPE, "Id": int(plan.entity_id)},
                "SharedEntityScope": "Account"}, "list items"))
        return {"list_id": plan.entity_id, "negatives_added": len(items)}

    if op in ("microsoft_attach_negative_list", "microsoft_detach_negative_list"):
        body = {"Associations": [{"EntityId": int(cid), "EntityType": "Campaign", "SharedEntityId": int(plan.entity_id),
                                  "SharedEntityType": _LIST_TYPE} for cid in c["campaign_ids"]],
                "SharedEntityScope": "Account"}
        if op == "microsoft_attach_negative_list":
            _call(config, c, "POST", "SharedEntityAssociations/Set", body, "list attachment")
        else:
            _call(config, c, "DELETE", "SharedEntityAssociations", body, "list detachment")
        return {"list_id": plan.entity_id, "campaigns": c["campaigns"]}

    if op == "microsoft_create_rsa":
        payload = _call(config, c, "POST", "Ads", {"AdGroupId": plan.entity_id, "Ads": [c["ad"]]}, "responsive search ad")
        return {"ad_id": str((payload.get("AdIds") or [None])[0]), "status": "Paused",
                "next": "Enable it with enable_microsoft_entity once reviewed."}

    if op == "microsoft_update_rsa":
        _call(config, c, "PUT", "Ads", {"AdGroupId": c["ad_group_id"], "Ads": [c["update"]]}, "responsive search ad update")
        return {"ad_id": plan.entity_id, "updated": sorted(k for k in c["update"] if k not in ("Id", "Type"))}

    if op in ("microsoft_add_sitelinks", "microsoft_add_callouts", "microsoft_add_structured_snippet",
              "microsoft_add_call_extension"):
        payload = _call(config, c, "POST", "AdExtensions", {"AccountId": account, "AdExtensions": c["extensions"]}, "extensions")
        ids = [str((i or {}).get("Id")) for i in payload.get("AdExtensionIdentities") or [] if i]
        progress.done.append(f"created extension(s) {ids}")
        progress.step("Linking the extensions", lambda: _call(config, c, "POST", "AdExtensionsAssociations/Set", {
            "AccountId": account, "AssociationType": c["association_type"],
            "AdExtensionIdToEntityIdAssociations": [{"AdExtensionId": int(i), "EntityId": int(c["entity_id"])} for i in ids],
        }, "extension links"))
        return {"extension_ids": ids, "linked_to": c["entity_name"]}

    if op == "microsoft_remove_entity":
        request = c["request"]
        _call(config, c, "DELETE", request["path"], request["body"], f"{plan.entity_type.replace('_', ' ')} removal")
        return {"removed": plan.entity_type, "entity_id": plan.entity_id}

    raise ValueError(f"Unknown Microsoft operation: {op}")
