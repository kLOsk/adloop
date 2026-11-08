"""GA4 conversion attribution: who gets credit for conversions, by model.

Data API v1alpha runReport with a ConversionSpec, the data behind GA4's
Advertising workspace. Google is still rolling it out ("may not be
available to your Google Analytics property"); a property without it gets
a plain explanation instead of a raw error. Conversions can be named by
their key event name; the IDs GA4 needs are looked up in its metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# The only fields the conversions report accepts (Google's Analytics MCP
# server documents the same lists; the API rejects others).
DIMENSIONS = (
    "campaignName", "continent", "country", "defaultChannelGroup", "deviceCategory", "medium",
    "platform", "primaryChannelGroup", "source", "sourceMedium", "sourcePlatform", "subcontinent",
)
METRICS = (
    "advertiserAdClicks", "advertiserAdCost", "advertiserAdCostPerAllConversionsByConversionDate",
    "advertiserAdCostPerAllConversionsByInteractionDate", "advertiserAdCostPerClick",
    "advertiserAdImpressions", "allConversionsByConversionDate", "allConversionsByInteractionDate",
    "returnOnAdSpendByConversionDate", "returnOnAdSpendByInteractionDate",
    "totalRevenueByConversionDate", "totalRevenueByInteractionDate",
)
DEFAULT_METRICS = ("allConversionsByConversionDate", "totalRevenueByConversionDate", "advertiserAdCost",
                   "returnOnAdSpendByConversionDate")
MODELS = ("DATA_DRIVEN", "LAST_CLICK", "COMPARE")
_CONVERSION_METRICS = ("allConversionsByConversionDate", "allConversionsByInteractionDate")


def _api_error(exc: Exception) -> dict:
    message = (getattr(exc, "message", "") or str(exc)).splitlines()[0][:400]
    return {
        "error": f"GA4 refused the attribution report (Data API v1alpha): {message}",
        "hint": (
            "Google is still rolling the conversions report out and some properties do not have it "
            "yet. run_ga4_report with keyEvents by sessionDefaultChannelGroup gives the session-based "
            "view meanwhile."
        ),
    }


def conversion_actions(config: AdLoopConfig, pid: str) -> list[dict]:
    """The property's conversion actions: [{"conversion_action", "display_name"}]."""
    from adloop.ga4.client import get_data_alpha_client

    metadata = get_data_alpha_client(config).get_metadata(name=f"properties/{pid}/metadata")
    return [
        {"conversion_action": c.conversion_action, "display_name": c.display_name}
        for c in getattr(metadata, "conversions", [])
    ]


def _resolve(config: AdLoopConfig, pid: str, conversions: list[str]) -> tuple[list[str], dict | None]:
    wanted = [str(c).strip() for c in conversions if str(c).strip()]
    if not wanted or all(c.startswith("conversionActions/") for c in wanted):
        return wanted, None
    available = conversion_actions(config, pid)
    by_name = {a["display_name"]: a["conversion_action"] for a in available}
    resolved, unknown = [], []
    for name in wanted:
        if name.startswith("conversionActions/"):
            resolved.append(name)
        elif name in by_name:
            resolved.append(by_name[name])
        else:
            unknown.append(name)
    if unknown:
        return [], {
            "error": f"No conversion action called {', '.join(repr(u) for u in unknown)} on properties/{pid}.",
            "conversions": sorted(by_name),
        }
    return resolved, None


def _run(config: AdLoopConfig, pid: str, *, dimensions, metrics, start, end, actions, model, limit):
    from google.analytics.data_v1alpha import types as T

    from adloop.ga4.client import get_data_alpha_client
    from adloop.ga4.funnels import _number

    request = T.RunReportRequest(
        property=f"properties/{pid}",
        dimensions=[T.Dimension(name=d) for d in dimensions],
        metrics=[T.Metric(name=m) for m in metrics],
        date_ranges=[T.DateRange(start_date=start, end_date=end)],
        limit=limit,
        return_property_quota=True,
        conversion_spec=T.ConversionSpec(
            conversion_actions=actions, attribution_model=T.ConversionSpec.AttributionModel[model],
        ),
    )
    response = get_data_alpha_client(config).run_report(request)
    dims = [h.name for h in response.dimension_headers]
    mets = [h.name for h in response.metric_headers]
    rows = []
    for row in response.rows:
        entry = {dims[i]: v.value for i, v in enumerate(row.dimension_values)}
        entry.update({mets[i]: _number(v.value) for i, v in enumerate(row.metric_values)})
        rows.append(entry)
    rows.sort(key=lambda r: r.get(metrics[0], 0) if isinstance(r.get(metrics[0]), (int, float)) else 0, reverse=True)
    return rows, response


def run_ga4_attribution(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
    conversions: list[str] | None = None,
    attribution_model: str = "DATA_DRIVEN",
    limit: int = 50,
) -> dict:
    """Conversions by channel, campaign or source under one model, or both compared."""
    from adloop.ga4.admin import check_property
    from adloop.ga4.reports import response_insights

    pid, error = check_property(property_id)
    errors = [error] if error else []
    dimensions = [d.strip() for d in (dimensions or ["defaultChannelGroup"]) if d.strip()]
    metrics = [m.strip() for m in (metrics or list(DEFAULT_METRICS)) if m.strip()]
    model = (attribution_model or "DATA_DRIVEN").strip().upper()
    if bad := [d for d in dimensions if d not in DIMENSIONS]:
        errors.append(f"dimensions {bad} are not available in the attribution report; use {', '.join(DIMENSIONS)}")
    if bad := [m for m in metrics if m not in METRICS]:
        errors.append(f"metrics {bad} are not available in the attribution report; use {', '.join(METRICS)}")
    if model not in MODELS:
        errors.append("attribution_model must be DATA_DRIVEN, LAST_CLICK or COMPARE (both, side by side)")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        errors.append("limit must be a positive integer")
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        actions, problem = _resolve(config, pid, list(conversions or []))
        if problem:
            return problem
        kwargs = dict(dimensions=dimensions, metrics=metrics, start=date_range_start, end=date_range_end,
                      actions=actions, limit=limit)
        if model != "COMPARE":
            rows, response = _run(config, pid, model=model, **kwargs)
            result = _base(pid, dimensions, metrics, date_range_start, date_range_end, conversions)
            result.update({"attribution_model": model, "rows": rows, "row_count": len(rows)})
            insights = response_insights(response)
        else:
            data_driven, response = _run(config, pid, model="DATA_DRIVEN", **kwargs)
            last_click, _ = _run(config, pid, model="LAST_CLICK", **kwargs)
            result = _base(pid, dimensions, metrics, date_range_start, date_range_end, conversions)
            result["attribution_model"] = "COMPARE"
            result.update(_compare(dimensions, metrics, data_driven, last_click))
            insights = result.pop("insights", []) + response_insights(response)
    except Exception as e:  # noqa: BLE001 - v1alpha and a gradual rollout: explain instead of raising
        return _api_error(e)

    if not result.get("rows"):
        insights.append(
            "GA4 attributed no conversions to these dimensions in this period. Check that key events "
            "fired (get_tracking_events) and widen the dates; attribution can lag a day or two."
        )
    if insights:
        result["insights"] = insights
    return result


def _base(pid, dimensions, metrics, start, end, conversions) -> dict:
    return {
        "property": f"properties/{pid}",
        "date_range": {"start": start, "end": end},
        "dimensions": dimensions,
        "metrics": metrics,
        "conversions": list(conversions or []) or "all",
    }


def _compare(dimensions, metrics, data_driven: list[dict], last_click: list[dict]) -> dict:
    """Join both models per dimension value and show how credit moves."""
    key_metric = next((m for m in _CONVERSION_METRICS if m in metrics), metrics[0])
    key = lambda row: tuple(row.get(d, "") for d in dimensions)  # noqa: E731
    dd = {key(r): r for r in data_driven}
    lc = {key(r): r for r in last_click}
    total_dd = sum(r.get(key_metric, 0) or 0 for r in data_driven) or 0
    total_lc = sum(r.get(key_metric, 0) or 0 for r in last_click) or 0

    rows = []
    for k in list(dd) + [k for k in lc if k not in dd]:
        a, b = dd.get(k, {}), lc.get(k, {})
        row = {d: k[i] for i, d in enumerate(dimensions)}
        for m in metrics:
            row[f"{m}_data_driven"] = a.get(m, 0)
            row[f"{m}_last_click"] = b.get(m, 0)
        share_dd = (a.get(key_metric, 0) or 0) / total_dd if total_dd else 0
        share_lc = (b.get(key_metric, 0) or 0) / total_lc if total_lc else 0
        row["credit_share_data_driven"] = round(share_dd, 4)
        row["credit_share_last_click"] = round(share_lc, 4)
        row["credit_shift_points"] = round((share_dd - share_lc) * 100, 1)
        rows.append(row)
    rows.sort(key=lambda r: abs(r["credit_shift_points"]), reverse=True)

    insights = []
    label = lambda r: " / ".join(str(r[d]) for d in dimensions)  # noqa: E731
    for row in rows[:3]:
        shift = row["credit_shift_points"]
        if abs(shift) >= 1:
            direction = "more" if shift > 0 else "less"
            insights.append(
                f"{label(row)} gets {abs(shift):.1f} points {direction} of the credit under data-driven "
                f"attribution ({row['credit_share_data_driven']:.0%}) than under last click "
                f"({row['credit_share_last_click']:.0%})."
            )
    if not insights and rows:
        insights.append("Both models give every row about the same share of the credit (within one point).")
    return {"compared_on": key_metric, "rows": rows, "row_count": len(rows), "insights": insights}
