"""Conversion tracking setup across GA4 and Google Ads in one approved plan.

Getting a GA4 key event into Google Ads bidding takes six settings in two
products: the GA4 property linked to the Ads account, the event marked as a
key event, auto-tagging on, the conversion action Google Ads creates for the
key event imported (it starts hidden), the action set to primary, and its goal
used for bidding. The draft reads all of them and plans only what is missing;
one approval applies the rest in order.

Every step re-reads its target before it acts and skips itself when the
target already is in the wanted state, so confirming the plan again after a
failed step continues where it stopped. Google Ads steps are validated with
validate_only in the dry run; the GA4 Admin API has no such mode, so its
steps are re-read instead.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

OPERATION = "conversion_tracking_setup"

_EVENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_COUNTING_METHODS = ("ONCE_PER_EVENT", "ONCE_PER_SESSION")
_GA4_ACTION_TYPES = ("GOOGLE_ANALYTICS_4_CUSTOM", "GOOGLE_ANALYTICS_4_PURCHASE")
_GOAL_NAMES = {
    "DEFAULT": "Other",
    "SIGNUP": "Sign-up",
    "SUBMIT_LEAD_FORM": "Submit lead form",
    "BOOK_APPOINTMENT": "Book appointment",
    "REQUEST_QUOTE": "Request quote",
    "GET_DIRECTIONS": "Get directions",
    "OUTBOUND_CLICK": "Outbound click",
    "PAGE_VIEW": "Page view",
    "ADD_TO_CART": "Add to cart",
    "BEGIN_CHECKOUT": "Begin checkout",
    "SUBSCRIBE_PAID": "Subscribe",
    "QUALIFIED_LEAD": "Qualified lead",
    "CONVERTED_LEAD": "Converted lead",
}


class SetupStepError(RuntimeError):
    """A step failed; the message says which, and what ran before it."""


def _goal_name(category: str) -> str:
    return _GOAL_NAMES.get(category) or category.replace("_", " ").capitalize()


def _parse_events(events: list | None) -> tuple[list[dict], list[str]]:
    parsed, errors, seen = [], [], set()
    for item in events or []:
        if isinstance(item, dict):
            name = str(item.get("event") or item.get("event_name") or "").strip()
            counting = str(item.get("counting_method") or "ONCE_PER_EVENT").strip().upper()
        else:
            name, counting = str(item or "").strip(), "ONCE_PER_EVENT"
        if not _EVENT_NAME.match(name):
            errors.append(f"'{name}' is not a GA4 event name (letters, digits and underscores, up to 40)")
            continue
        if counting not in _COUNTING_METHODS:
            errors.append(f"counting_method for '{name}' must be ONCE_PER_EVENT or ONCE_PER_SESSION")
            continue
        if name not in seen:
            seen.add(name)
            parsed.append({"event": name, "counting_method": counting})
    return parsed, errors


def _read_state(config: AdLoopConfig, cid: str, pid: str) -> dict:
    """Everything the plan depends on, from both products. Raises on a failed read."""
    from adloop.ads.client import normalize_customer_id
    from adloop.ads.gaql import execute_query
    from adloop.ga4.client import get_admin_client

    admin = get_admin_client(config)
    prop = admin.get_property(name=f"properties/{pid}")
    state = {
        "property_name": prop.display_name or f"properties/{pid}",
        "linked": any(
            normalize_customer_id(link.customer_id) == cid
            for link in admin.list_google_ads_links(parent=f"properties/{pid}")
        ),
        "key_events": {ke.event_name for ke in admin.list_key_events(parent=f"properties/{pid}")},
    }
    customer = execute_query(config, cid, (
        "SELECT customer.descriptive_name, customer.auto_tagging_enabled FROM customer"
    ))
    first = customer[0] if customer else {}
    state["account_name"] = first.get("customer.descriptive_name") or ""
    state["auto_tagging"] = bool(first.get("customer.auto_tagging_enabled"))
    types = ", ".join(f"'{t}'" for t in _GA4_ACTION_TYPES)
    state["actions"] = [
        {
            "id": str(row.get("conversion_action.id")),
            "name": row.get("conversion_action.name") or "",
            "status": str(row.get("conversion_action.status") or ""),
            "primary": bool(row.get("conversion_action.primary_for_goal")),
            "category": str(row.get("conversion_action.category") or ""),
            "origin": str(row.get("conversion_action.origin") or ""),
            "event": row.get("conversion_action.google_analytics_4_settings.event_name") or "",
            "property_id": str(row.get("conversion_action.google_analytics_4_settings.property_id") or ""),
        }
        for row in execute_query(config, cid, (
            "SELECT conversion_action.id, conversion_action.name, conversion_action.status, "
            "conversion_action.primary_for_goal, conversion_action.category, conversion_action.origin, "
            "conversion_action.google_analytics_4_settings.event_name, "
            "conversion_action.google_analytics_4_settings.property_id "
            f"FROM conversion_action WHERE conversion_action.type IN ({types}) "
            "AND conversion_action.status != 'REMOVED'"
        ))
    ]
    state["goals"] = {
        (str(row.get("customer_conversion_goal.category")), str(row.get("customer_conversion_goal.origin"))):
            bool(row.get("customer_conversion_goal.biddable"))
        for row in execute_query(config, cid, (
            "SELECT customer_conversion_goal.category, customer_conversion_goal.origin, "
            "customer_conversion_goal.biddable FROM customer_conversion_goal"
        ))
    }
    return state


def _event_counts(config: AdLoopConfig, pid: str) -> dict[str, int] | None:
    from adloop.ga4.reports import run_ga4_report

    report = run_ga4_report(
        config, property_id=f"properties/{pid}", dimensions=["eventName"], metrics=["eventCount"],
        date_range_start="28daysAgo", date_range_end="today", limit=500,
    )
    if "error" in report:
        return None
    return {r.get("eventName"): int(float(r.get("eventCount") or 0)) for r in report.get("rows", [])}


def draft_conversion_tracking_setup(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    property_id: str = "",
    events: list | None = None,
    primary: bool = True,
    data_retention: str = "",
    ads_personalization: bool = False,
) -> dict:
    """Plan the missing steps between a GA4 event and Google Ads bidding — returns PREVIEW."""
    from adloop.ads.client import normalize_customer_id
    from adloop.ga4 import retention
    from adloop.ga4.admin import check_property, fingerprint, format_customer_id, service_level
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(OPERATION, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    cid = normalize_customer_id(customer_id)
    pid, error = check_property(property_id)
    errors = [error] if error else []
    if not cid:
        errors.append("customer_id is required (falls back to the configured Google Ads account)")
    wanted_events, event_errors = _parse_events(events)
    errors += event_errors
    wanted_retention, retention_error = retention.parse_retention(data_retention)
    if retention_error:
        errors.append(retention_error)
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        state = _read_state(config, cid, pid)
    except Exception as e:  # noqa: BLE001 - the plan cannot be built without the current state
        return {"error": f"Could not read the current setup: {e}"}
    counts = _event_counts(config, pid)

    warnings: list[str] = []
    if not wanted_events:
        fired = sorted(
            (name for name in state["key_events"] if counts is None or counts.get(name, 0) > 0),
        )
        if not fired:
            top = sorted((counts or {}).items(), key=lambda kv: -kv[1])[:15]
            return {
                "error": (
                    "None of the property's key events fired in the last 28 days, so there is nothing "
                    "to set up by default. Name the events to use as conversions with `events`."
                ),
                "key_events": sorted(state["key_events"]),
                "events_last_28_days": dict(top),
            }
        wanted_events = [{"event": name, "counting_method": "ONCE_PER_EVENT"} for name in fired]
        skipped = sorted(state["key_events"] - set(fired))
        if skipped:
            warnings.append(
                f"Left out because they did not fire in the last 28 days: {', '.join(skipped)}. "
                "Pass them in `events` to include them."
            )
    if counts is None:
        warnings.append("Could not read the event counts from GA4, so it is unchecked whether the events fire.")

    account = f"{state['account_name']} ({format_customer_id(cid)})" if state["account_name"] else format_customer_id(cid)
    prop_label = f"{state['property_name']} (properties/{pid})"
    actions: list[dict] = []
    done: list[str] = []
    pending: list[str] = []

    if state["linked"]:
        done.append(f"GA4 property linked to {format_customer_id(cid)}")
    else:
        actions.append({
            "kind": "link", "ads_personalization": bool(ads_personalization),
            "title": f"Google Analytics: link {state['property_name']} to Google Ads {account}",
        })
        warnings.append(
            "Linking needs Editor access to the GA4 property and admin access to the Google Ads account. "
            + (
                "Personalized advertising is on for the link: GA4 shares its audiences and remarketing data "
                "with Google Ads."
                if ads_personalization else
                "Personalized advertising stays off for the link, so GA4 does not share audiences or "
                "remarketing data with Google Ads; pass ads_personalization=true to turn it on."
            )
        )

    if state["auto_tagging"]:
        done.append("auto-tagging on")
    else:
        actions.append({
            "kind": "auto_tagging",
            "title": "Google Ads: turn on auto-tagging, so GA4 can tell which conversions came from ads",
        })

    goals_planned: set[tuple[str, str]] = set()
    for item in wanted_events:
        event = item["event"]
        if counts is not None and counts.get(event, 0) == 0:
            warnings.append(
                f"'{event}' did not fire in the last 28 days: check that the website sends it, or it will "
                "never record a conversion."
            )
        if event in state["key_events"]:
            done.append(f"'{event}' is a key event")
        else:
            actions.append({
                "kind": "key_event", "event": event, "counting_method": item["counting_method"],
                "title": (
                    f"Google Analytics: mark '{event}' as a key event "
                    f"({'once per session' if item['counting_method'] == 'ONCE_PER_SESSION' else 'every event'})"
                ),
            })

        matches = [a for a in state["actions"] if a["event"] == event and a["property_id"] == pid]
        if not matches:
            pending.append(
                f"'{event}': Google Ads has no conversion action for it yet. Google Ads creates one by itself "
                "once the property is linked to this account and the event is a key event; that is not "
                "instant. Run this setup again later to import it."
            )
            continue
        enabled = [a for a in matches if a["status"] == "ENABLED"]
        action = enabled[0] if enabled else matches[0]
        role = "primary" if primary else "secondary"
        if not enabled:
            actions.append({
                "kind": "import", "event": event, "conversion_action_id": action["id"], "primary": primary,
                "title": f"Google Ads: import '{event}' (“{action['name']}”) as a {role} conversion",
            })
        elif action["primary"] != primary:
            actions.append({
                "kind": "primary", "event": event, "conversion_action_id": action["id"], "primary": primary,
                "title": f"Google Ads: make “{action['name']}” a {role} conversion",
            })
        else:
            done.append(f"'{event}' imported as a {role} conversion")
        goal = (action["category"], action["origin"])
        if primary and state["goals"].get(goal) is False and goal not in goals_planned:
            goals_planned.add(goal)
            actions.append({
                "kind": "goal", "category": goal[0], "origin": goal[1],
                "title": (
                    f"Google Ads: bid on the “{_goal_name(goal[0])}” goal in campaigns that use "
                    "the account's goals"
                ),
            })

    if wanted_retention:
        try:
            current = retention.read_settings(config, pid)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Could not read the data retention of properties/{pid}: {e}"}
        after, changed, shortens, refusal = retention.plan_retention_change(
            current, {"event_data_retention": wanted_retention}, service_level(config, pid),
        )
        if refusal:
            return {"error": refusal}
        if shortens:
            return {
                "error": (
                    "This setup only lengthens data retention; shortening deletes data, so it goes through "
                    "draft_ga4_data_retention on its own."
                ),
            }
        if changed:
            actions.append({
                "kind": "retention", "after": after, "fields": changed, "fingerprint": fingerprint(current),
                "title": (
                    f"Google Analytics: keep event data for {retention.MONTHS[after['event_data_retention']]} "
                    f"months instead of {retention.MONTHS[current['event_data_retention']]}"
                ),
            })
            warnings += retention.retention_warnings(changed, False)
        else:
            done.append(f"event data kept {retention.MONTHS[current['event_data_retention']]} months")
    else:
        try:
            if retention.read_settings(config, pid)["event_data_retention"] == "TWO_MONTHS":
                warnings.append(
                    "GA4 keeps event data for 2 months, its default, which cuts explorations and funnels "
                    "short. Pass data_retention=14 to include the change (update your privacy policy if it "
                    "names a period)."
                )
        except Exception:  # noqa: BLE001 - a hint, not part of the plan
            pass

    blocked = {
        "key_event": "create_key_event", "import": "update_conversion_action",
        "primary": "update_conversion_action", "retention": retention.OPERATION,
    }
    for kind in {a["kind"] for a in actions} & set(blocked):
        try:
            check_blocked_operation(blocked[kind], config.safety)
        except SafetyViolation as e:
            return {"error": str(e)}

    if not actions:
        return {
            "status": "NO_CHANGE",
            "message": (
                "Nothing to change: " + "; ".join(done) + "."
                if not pending else
                "Nothing to change right now; something is still pending on Google's side."
            ),
            "account": account,
            "property": prop_label,
            "already_done": done,
            **({"pending": pending} if pending else {}),
            **({"warnings": warnings} if warnings else {}),
        }

    plan = ChangePlan(
        operation=OPERATION,
        entity_type="conversion_tracking",
        entity_id=f"properties/{pid}",
        customer_id=cid,
        changes={
            "account": account,
            "property": prop_label,
            "property_id": pid,
            "steps": [a["title"] for a in actions],
            **({"already_done": done} if done else {}),
        },
        apply_only_payload={"actions": actions},
    )
    store_plan(plan)
    preview = plan.to_preview()
    if any(a["kind"] in ("import", "primary", "goal") and primary for a in actions):
        warnings.append(
            "Campaigns with Smart Bidding optimize toward primary conversions, so bidding starts using "
            "these as soon as they record conversions."
        )
    preview["warnings"] = [
        *warnings,
        *(f"After this: {p}" for p in pending),
    ]
    return preview


# ---------------------------------------------------------------------------
# Running the steps (dry run and apply share this code)
# ---------------------------------------------------------------------------


class _Run:
    def __init__(self, config: AdLoopConfig, cid: str, pid: str, validate_only: bool) -> None:
        self.config, self.cid, self.pid, self.validate_only = config, cid, pid, validate_only
        self._ads = None
        self.validated = 0

    @property
    def ads(self):
        if self._ads is None:
            from adloop.ads.client import get_ads_client

            client = get_ads_client(self.config)
            if self.validate_only:
                from adloop.ads.validate_only import ValidateOnlyClient

                client = ValidateOnlyClient(client)
            self._ads = client
        return self._ads

    def query(self, gaql: str) -> list[dict]:
        from adloop.ads.gaql import execute_query

        return execute_query(self.config, self.cid, gaql)

    def mutate(self, service: str, method: str, request) -> None:
        getattr(self.ads.get_service(service), method)(request=request)
        if self.validate_only:
            self.validated += 1


def _mask(*paths: str):
    from google.protobuf import field_mask_pb2

    return field_mask_pb2.FieldMask(paths=list(paths))


def _run_link(run: _Run, action: dict) -> str:
    from google.analytics.admin_v1beta import types

    from adloop.ads.client import normalize_customer_id
    from adloop.ga4.client import get_admin_client

    admin = get_admin_client(run.config)
    parent = f"properties/{run.pid}"
    if any(normalize_customer_id(link.customer_id) == run.cid for link in admin.list_google_ads_links(parent=parent)):
        return "already done"
    if run.validate_only:
        return "checked"
    admin.create_google_ads_link(
        parent=parent,
        google_ads_link=types.GoogleAdsLink(
            customer_id=run.cid, ads_personalization_enabled=action["ads_personalization"],
        ),
    )
    return "done"


def _run_key_event(run: _Run, action: dict) -> str:
    from adloop.ga4.client import get_admin_client
    from adloop.ga4.write import _key_event_proto

    admin = get_admin_client(run.config)
    parent = f"properties/{run.pid}"
    if any(ke.event_name == action["event"] for ke in admin.list_key_events(parent=parent)):
        return "already done"
    if run.validate_only:
        return "checked"
    admin.create_key_event(parent=parent, key_event=_key_event_proto(action["event"], action["counting_method"], None))
    return "done"


def _run_auto_tagging(run: _Run, action: dict) -> str:
    rows = run.query("SELECT customer.auto_tagging_enabled FROM customer")
    if rows and rows[0].get("customer.auto_tagging_enabled"):
        return "already done"
    request = run.ads.get_type("MutateCustomerRequest")
    request.customer_id = run.cid
    request.operation.update.resource_name = f"customers/{run.cid}"
    request.operation.update.auto_tagging_enabled = True
    request.operation.update_mask.CopyFrom(_mask("auto_tagging_enabled"))
    run.mutate("CustomerService", "mutate_customer", request)
    return "validated" if run.validate_only else "done"


def _run_conversion_action(run: _Run, action: dict) -> str:
    action_id = str(action["conversion_action_id"])
    if not action_id.isdigit():
        raise ValueError(f"conversion action id '{action_id}' is not numeric")
    rows = run.query(
        "SELECT conversion_action.status, conversion_action.primary_for_goal FROM conversion_action "
        f"WHERE conversion_action.id = {action_id}"
    )
    if not rows:
        raise ValueError(f"The conversion action {action_id} no longer exists in Google Ads.")
    status = str(rows[0].get("conversion_action.status") or "")
    if status == "REMOVED":
        raise ValueError(f"The conversion action {action_id} was removed in Google Ads.")
    is_primary = bool(rows[0].get("conversion_action.primary_for_goal"))
    if status == "ENABLED" and is_primary == action["primary"]:
        return "already done"
    op = run.ads.get_type("ConversionActionOperation")
    op.update.resource_name = f"customers/{run.cid}/conversionActions/{action_id}"
    paths = []
    if status != "ENABLED":
        op.update.status = run.ads.enums.ConversionActionStatusEnum.ENABLED
        paths.append("status")
    if is_primary != action["primary"]:
        op.update.primary_for_goal = action["primary"]
        paths.append("primary_for_goal")
    op.update_mask.CopyFrom(_mask(*paths))
    request = run.ads.get_type("MutateConversionActionsRequest")
    request.customer_id = run.cid
    request.operations.append(op)
    run.mutate("ConversionActionService", "mutate_conversion_actions", request)
    return "validated" if run.validate_only else "done"


def _run_goal(run: _Run, action: dict) -> str:
    category, origin = action["category"], action["origin"]
    if not (category.replace("_", "").isalnum() and origin.replace("_", "").isalnum()):
        raise ValueError("unexpected conversion goal")
    rows = run.query(
        "SELECT customer_conversion_goal.biddable FROM customer_conversion_goal "
        f"WHERE customer_conversion_goal.category = '{category}' "
        f"AND customer_conversion_goal.origin = '{origin}'"
    )
    if rows and rows[0].get("customer_conversion_goal.biddable"):
        return "already done"
    op = run.ads.get_type("CustomerConversionGoalOperation")
    op.update.resource_name = f"customers/{run.cid}/customerConversionGoals/{category}~{origin}"
    op.update.biddable = True
    op.update_mask.CopyFrom(_mask("biddable"))
    request = run.ads.get_type("MutateCustomerConversionGoalsRequest")
    request.customer_id = run.cid
    request.operations.append(op)
    run.mutate("CustomerConversionGoalService", "mutate_customer_conversion_goals", request)
    return "validated" if run.validate_only else "done"


def _run_retention(run: _Run, action: dict) -> str:
    from adloop.ga4 import retention
    from adloop.ga4.admin import fingerprint

    current = retention.read_settings(run.config, run.pid)
    if all(current[f] == action["after"][f] for f in action["fields"]):
        return "already done"
    if fingerprint(current) != action["fingerprint"]:
        raise ValueError(
            f"The data retention of properties/{run.pid} was changed in GA4 after the preview; draft again."
        )
    if run.validate_only:
        return "checked"
    retention.write_settings(run.config, run.pid, action["after"], action["fields"])
    return "done"


_RUNNERS = {
    "link": _run_link,
    "key_event": _run_key_event,
    "auto_tagging": _run_auto_tagging,
    "import": _run_conversion_action,
    "primary": _run_conversion_action,
    "goal": _run_goal,
    "retention": _run_retention,
}


def _run_steps(config: AdLoopConfig, plan, *, validate_only: bool) -> dict:
    payload = plan.apply_payload()
    actions = payload.get("actions")
    if not actions:
        raise SetupStepError(
            "This plan's steps are missing (the plan store did not keep them); draft the setup again."
        )
    run = _Run(config, plan.customer_id, payload["property_id"], validate_only)
    results: list[dict] = []
    for index, action in enumerate(actions, 1):
        try:
            outcome = _RUNNERS[action["kind"]](run, action)
        except Exception as e:  # noqa: BLE001 - reported with the steps that ran before it
            from adloop.ads.write import _extract_error_message

            finished = [r["step"] for r in results if r["result"] in ("done", "already done")]
            message = f"Step {index} of {len(actions)} failed ({action['title']}): {_extract_error_message(e)}"
            if not validate_only and finished:
                message += (
                    " Already in place: " + "; ".join(finished) + ". Confirming the plan again continues "
                    "with the failed step; finished steps are skipped."
                )
            raise SetupStepError(message) from e
        results.append({"step": action["title"], "result": outcome})
    return {"steps": results, "validated_calls": run.validated} if validate_only else {"steps": results}


def preflight(config: AdLoopConfig, plan) -> dict:
    """Dry run: Google Ads steps go to Google with validate_only, GA4 steps are re-read."""
    return _run_steps(config, plan, validate_only=True)


def apply_plan(config: AdLoopConfig, plan) -> dict:
    result = _run_steps(config, plan, validate_only=False)
    if any(r["result"] == "done" and r["step"].startswith("Google Ads: import") for r in result["steps"]):
        result["next_step"] = (
            "Run check_key_event_imports in a day or two: the imported conversions should show up "
            "as active and primary, and start counting once GA4 records them."
        )
    return result
