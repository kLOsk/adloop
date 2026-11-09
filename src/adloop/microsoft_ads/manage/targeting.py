"""Campaign targeting: included and excluded locations, location intent, ad schedule."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.manage._common import (
    _MINUTES,
    DAYS,
    INTENTS,
    Handler,
    Step,
    _call,
    _chunks,
    _clean,
    _ctx,
    _plan,
    _Progress,
    _same,
)
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


def _current_targeting(config: AdLoopConfig, account: str, customer: str, campaign_id: str) -> dict:
    found = target_criterions(config, account, customer, campaign_id, ("Location", "LocationIntent", "DayTime"))
    included = {}
    excluded = {}
    for c in found["Location"]:
        crit = c.get("Criterion") or {}
        entry = {"criterion_id": str(c.get("Id")), "name": crit.get("DisplayName") or str(crit.get("LocationId"))}
        target = excluded if is_negative(c) else included
        target[str(crit.get("LocationId"))] = entry
    intent = found["LocationIntent"]
    schedule = found["DayTime"]
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
        if len(items) > _MAX_WINDOWS_PER_DAY:
            errors.append(f"At most {_MAX_WINDOWS_PER_DAY} ad_schedule windows on {day} (Microsoft's limit).")
    return blocks


# Microsoft: "the maximum number of day and time criteria per day that you
# can set is seven" (per campaign or ad group; DayTimeCriterion reference).
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
        operation, "campaign", campaign_id, account, customer,
        {"entity_name": campaign.get("Name"), "changes": shown},
        {"operations": plan_ops, "before": current},
        warnings=warnings,
        double=bool(blocks) and not current["schedule"],
    )


def _check_update_targeting(s: Step) -> None:
    config, plan, c, account, customer = s.config, s.plan, s.c, s.account, s.customer
    campaign = s.entity("campaign", plan.entity_id)["entity"]
    now = _current_targeting(config, account, customer, plan.entity_id)
    if "searcher_time_zone" in c["before"]:
        now["searcher_time_zone"] = bool(campaign.get("AdScheduleUseSearcherTimeZone"))
        if now["searcher_time_zone"] != c["before"]["searcher_time_zone"]:
            raise ValueError("The campaign's ad schedule time zone setting (AdScheduleUseSearcherTimeZone) "
                             "changed since the draft, so the approved hours would mean other times; draft again.")
    if not _same(now, c["before"]):
        raise ValueError("The campaign's targeting changed since the draft; draft again.")


def _apply_update_targeting(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
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


HANDLERS = {
    "microsoft_update_targeting": Handler(_check_update_targeting, _apply_update_targeting),
}
