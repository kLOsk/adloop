"""GA4 funnel reports: how many users get from one step to the next.

Data API v1alpha (runFunnelReport), the endpoint behind GA4's funnel
explorations. Steps are plain event names or pages instead of raw filter
objects, and the result is computed per step (users, completion, drop-off,
the biggest leak) instead of returned as a raw sub-report.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

MAX_STEPS = 10
MAX_BREAKDOWN_VALUES = 15
_NEXT_ACTION_LIMIT = 5  # GA4 maximum
_STEP_KEYS = {"name", "event", "page", "directly_followed", "within_minutes"}
_TOTAL = "RESERVED_TOTAL"


def to_alpha_filter(expression):
    """A v1beta FilterExpression (from build_filter_expression) as v1alpha.

    Both versions share field names, so JSON carries it over without
    depending on field numbers.
    """
    from google.analytics.data_v1alpha import types as alpha
    from google.analytics.data_v1beta import types as beta

    return alpha.FilterExpression.from_json(beta.FilterExpression.to_json(expression))


def _step(step, index: int, errors: list[str]):
    from google.analytics.data_v1alpha import types as T
    from google.protobuf import duration_pb2

    where = f"steps[{index}]"
    if isinstance(step, str):
        step = {"event": step}
    if not isinstance(step, dict):
        errors.append(f'{where}: a step is an event name or an object like {{"event": "add_to_cart"}}')
        return None
    unknown = set(step) - _STEP_KEYS
    if unknown:
        errors.append(f"{where}: unknown key(s) {sorted(unknown)}; use name, event, page, directly_followed, within_minutes")
        return None
    event = str(step.get("event") or "").strip()
    page = str(step.get("page") or "").strip()
    if bool(event) == bool(page):
        errors.append(f"{where}: give either 'event' (an event name) or 'page' (part of the page URL)")
        return None
    if page:
        # Every page view carries page_location, the full URL, so "contains"
        # matches a path whatever the domain or query string.
        event_filter = T.FunnelEventFilter(
            event_name="page_view",
            funnel_parameter_filter_expression=T.FunnelParameterFilterExpression(
                funnel_parameter_filter=T.FunnelParameterFilter(
                    event_parameter_name="page_location",
                    string_filter=T.StringFilter(match_type=T.StringFilter.MatchType.CONTAINS, value=page),
                )
            ),
        )
    else:
        event_filter = T.FunnelEventFilter(event_name=event)
    kwargs = {
        "name": str(step.get("name") or event or page).strip(),
        "filter_expression": T.FunnelFilterExpression(funnel_event_filter=event_filter),
    }
    if step.get("directly_followed"):
        if index == 0:
            errors.append(f"{where}: directly_followed applies from the second step on")
            return None
        kwargs["is_directly_followed_by"] = True
    within = step.get("within_minutes")
    if within is not None:
        if index == 0 or isinstance(within, bool) or not isinstance(within, int) or within < 1:
            errors.append(f"{where}: within_minutes is a whole number of minutes, from the second step on")
            return None
        kwargs["within_duration_from_prior_step"] = duration_pb2.Duration(seconds=within * 60)
    return T.FunnelStep(**kwargs)


def _number(value: str):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else round(number, 4)


def _sub_report_rows(sub_report) -> list[dict]:
    dims = [h.name for h in sub_report.dimension_headers]
    mets = [h.name for h in sub_report.metric_headers]
    rows = []
    for row in sub_report.rows:
        entry = {dims[i]: v.value for i, v in enumerate(row.dimension_values)}
        entry.update({mets[i]: _number(v.value) for i, v in enumerate(row.metric_values)})
        rows.append(entry)
    return rows


def _step_order(name: str) -> int:
    head = str(name).split(".", 1)[0]
    return int(head) if head.isdigit() else 999


def _steps(rows: list[dict]) -> list[dict]:
    steps = [
        {
            "step": r.get("funnelStepName", ""),
            "users": r.get("activeUsers", 0),
            "completion_rate": r.get("funnelStepCompletionRate"),
            "abandonments": r.get("funnelStepAbandonments"),
            "abandonment_rate": r.get("funnelStepAbandonmentRate"),
        }
        for r in rows
    ]
    steps.sort(key=lambda s: _step_order(s["step"]))
    return steps


def _summary(steps: list[dict]) -> dict:
    if not steps:
        return {}
    first, last = steps[0]["users"] or 0, steps[-1]["users"] or 0
    summary = {"started": first, "finished": last}
    if first:
        summary["overall_completion_rate"] = round(last / first, 4)
    drops = [
        (s, nxt) for s, nxt in zip(steps, steps[1:])
        if isinstance(s.get("abandonment_rate"), (int, float))
    ]
    if drops:
        worst, after = max(drops, key=lambda pair: pair[0]["abandonment_rate"])
        summary["biggest_drop"] = {
            "after_step": worst["step"],
            "before_step": after["step"],
            "abandonment_rate": worst["abandonment_rate"],
            "users_lost": worst.get("abandonments"),
        }
    return summary


def run_ga4_funnel(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    steps: list | None = None,
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
    open_funnel: bool = False,
    breakdown: str = "",
    breakdown_limit: int = 5,
    next_action: str = "",
    dimension_filter: list[dict] | None = None,
) -> dict:
    """Run a GA4 funnel: users per step, completion, drop-off, biggest leak."""
    from google.analytics.data_v1alpha import types as T

    from adloop.ga4.admin import check_property
    from adloop.ga4.client import get_data_alpha_client
    from adloop.ga4.reports import build_filter_expression, response_insights

    pid, error = check_property(property_id)
    errors = [error] if error else []
    steps = list(steps or [])
    if len(steps) < 2 or len(steps) > MAX_STEPS:
        errors.append(f"steps takes 2 to {MAX_STEPS} steps, e.g. [\"view_item\", \"add_to_cart\", \"purchase\"]")
    built = [_step(s, i, errors) for i, s in enumerate(steps)] if 2 <= len(steps) <= MAX_STEPS else []
    breakdown, next_action = (breakdown or "").strip(), (next_action or "").strip()
    if isinstance(breakdown_limit, bool) or not isinstance(breakdown_limit, int) or not 1 <= breakdown_limit <= MAX_BREAKDOWN_VALUES:
        errors.append(f"breakdown_limit must be 1 to {MAX_BREAKDOWN_VALUES}")
    beta_filter = build_filter_expression(dimension_filter, kind="dimension", errors=errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    request = {
        "property": f"properties/{pid}",
        "date_ranges": [T.DateRange(start_date=date_range_start, end_date=date_range_end)],
        "funnel": T.Funnel(is_open_funnel=bool(open_funnel), steps=built),
        "return_property_quota": True,
    }
    if breakdown:
        request["funnel_breakdown"] = T.FunnelBreakdown(
            breakdown_dimension=T.Dimension(name=breakdown), limit=breakdown_limit,
        )
    if next_action:
        request["funnel_next_action"] = T.FunnelNextAction(
            next_action_dimension=T.Dimension(name=next_action), limit=_NEXT_ACTION_LIMIT,
        )
    if beta_filter is not None:
        request["dimension_filter"] = to_alpha_filter(beta_filter)

    try:
        response = get_data_alpha_client(config).run_funnel_report(T.RunFunnelReportRequest(**request))
    except Exception as e:  # noqa: BLE001 - v1alpha: a plain message instead of a traceback
        message = (getattr(e, "message", "") or str(e)).splitlines()[0][:400]
        return {"error": f"GA4 funnel API (v1alpha) error: {message}"}

    table = _sub_report_rows(response.funnel_table)
    result: dict = {
        "property": f"properties/{pid}",
        "date_range": {"start": date_range_start, "end": date_range_end},
        "funnel": "open" if open_funnel else "closed",
    }
    if breakdown:
        groups: dict[str, list[dict]] = {}
        for row in table:
            value = row.get(breakdown, _TOTAL)
            groups.setdefault("total" if value == _TOTAL else value, []).append(row)
        total = _steps(groups.pop("total", []))
        result["steps"] = total
        result["breakdown"] = {
            "dimension": breakdown,
            "groups": {value: {"steps": _steps(rows), **_summary(_steps(rows))} for value, rows in groups.items()},
        }
    else:
        total = _steps(table)
        result["steps"] = total
    result.update(_summary(total))

    if next_action:
        actions: dict[str, list[dict]] = {}
        for row in _sub_report_rows(response.funnel_visualization):
            # GA4 names the column funnelStepNextAction, whatever dimension
            # was asked for.
            value = row.get("funnelStepNextAction", row.get(next_action))
            if value in (None, "", _TOTAL):
                continue
            actions.setdefault(row.get("funnelStepName", ""), []).append(
                {"value": value, "users": row.get("activeUsers", 0)}
            )
        result["next_actions"] = {
            "dimension": next_action,
            "by_step": {
                step: sorted(values, key=lambda v: v["users"], reverse=True)[:_NEXT_ACTION_LIMIT]
                for step, values in sorted(actions.items(), key=lambda kv: _step_order(kv[0]))
            },
        }

    # response_insights reads sampling and quota; a funnel keeps its
    # sampling on the table's metadata.
    quality = SimpleNamespace(
        metadata=SimpleNamespace(
            sampling_metadatas=list(getattr(getattr(response.funnel_table, "metadata", None), "sampling_metadatas", None) or []),
        ),
        property_quota=getattr(response, "property_quota", None),
    )
    insights = _insights(result, open_funnel) + response_insights(quality)
    if insights:
        result["insights"] = insights
    return result


def _insights(result: dict, open_funnel: bool) -> list[str]:
    insights = []
    started = result.get("started", 0)
    if not started:
        insights.append(
            "No user reached the first step in this date range. Check the event name against "
            "get_tracking_events (names are case-sensitive) or widen the dates."
        )
        return insights
    rate = result.get("overall_completion_rate")
    if rate is not None:
        insights.append(f"{result['finished']:,} of {started:,} users ({rate:.1%}) went through every step.")
    drop = result.get("biggest_drop")
    if drop and isinstance(drop.get("abandonment_rate"), (int, float)):
        insights.append(
            f"The biggest drop is between '{drop['after_step']}' and '{drop['before_step']}': "
            f"{drop['abandonment_rate']:.0%} of users leave there."
        )
    if not open_funnel:
        insights.append("Closed funnel: only users who began at step 1 are counted (open_funnel=true lets them join at any step).")
    return insights
