"""GA4 report tools — account summaries and custom reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def get_account_summaries(config: AdLoopConfig) -> dict:
    """List GA4 accounts and properties accessible by the authenticated user."""
    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    summaries = client.list_account_summaries()

    accounts = []
    for summary in summaries:
        properties = []
        for prop in summary.property_summaries:
            properties.append({
                "property": prop.property,
                "display_name": prop.display_name,
            })
        accounts.append({
            "account": summary.account,
            "display_name": summary.display_name,
            "properties": properties,
        })

    return {
        "accounts": accounts,
        "total_accounts": len(accounts),
        "total_properties": sum(len(a["properties"]) for a in accounts),
    }


def first_property(summaries: dict) -> str:
    """The first property in a get_account_summaries() result, or ""."""
    for account in summaries.get("accounts", []):
        for prop in account.get("properties", []):
            if prop.get("property"):
                return prop["property"]
    return ""


def probe_data_api(config: AdLoopConfig, property_name: str) -> None:
    """Raise if the GA4 Data API cannot serve this property.

    A metadata lookup is the cheapest Data API call: it touches no report
    quota, yet fails with SERVICE_DISABLED exactly when every report would.
    """
    from adloop.ga4.client import get_data_client

    name = property_name if property_name.startswith("properties/") else f"properties/{property_name}"
    get_data_client(config).get_metadata(name=f"{name}/metadata")


# String match types accepted in dimension filters (GA4 StringFilter.MatchType).
_STRING_OPS = ("EXACT", "BEGINS_WITH", "ENDS_WITH", "CONTAINS", "FULL_REGEXP", "PARTIAL_REGEXP")
# Numeric comparisons accepted in metric filters (GA4 NumericFilter.Operation),
# plus the symbols a caller is likely to type.
_NUMERIC_OPS = {
    "EQUAL": "EQUAL",
    "=": "EQUAL",
    "==": "EQUAL",
    "LESS_THAN": "LESS_THAN",
    "<": "LESS_THAN",
    "LESS_THAN_OR_EQUAL": "LESS_THAN_OR_EQUAL",
    "<=": "LESS_THAN_OR_EQUAL",
    "GREATER_THAN": "GREATER_THAN",
    ">": "GREATER_THAN",
    "GREATER_THAN_OR_EQUAL": "GREATER_THAN_OR_EQUAL",
    ">=": "GREATER_THAN_OR_EQUAL",
}
# Names given to the two date ranges of a comparison report; GA4 echoes them
# back as the value of the automatic ``dateRange`` dimension.
CURRENT_RANGE = "current"
COMPARISON_RANGE = "comparison"


def _numeric_value(raw, *, where: str, errors: list[str]):
    """A GA4 NumericValue from an int, float or numeric string, or None."""
    from google.analytics.data_v1beta.types import NumericValue

    if isinstance(raw, bool):
        errors.append(f"{where}: value must be a number, got {raw!r}")
        return None
    if isinstance(raw, str):
        text = raw.strip()
        try:
            raw = int(text)
        except ValueError:
            try:
                raw = float(text)
            except ValueError:
                errors.append(f"{where}: value must be a number, got {raw!r}")
                return None
    if isinstance(raw, int):
        return NumericValue(int64_value=raw)
    if isinstance(raw, float):
        return NumericValue(double_value=raw)
    errors.append(f"{where}: value must be a number, got {raw!r}")
    return None


def _build_filter(condition, *, kind: str, index: int, errors: list[str]):
    """One condition dict -> GA4 FilterExpression, or None (errors appended).

    ``kind`` is "dimension" (string match or in-list) or "metric" (numeric
    comparison or BETWEEN).
    """
    from google.analytics.data_v1beta.types import Filter, FilterExpression

    where = f"{kind}_filter[{index}]"
    if not isinstance(condition, dict):
        errors.append(f"{where}: each condition must be an object like "
                      '{"field": ..., "op": ..., "value": ...}')
        return None
    unknown = set(condition) - {"field", "op", "value", "values", "case_sensitive", "not"}
    if unknown:
        errors.append(f"{where}: unknown key(s) {sorted(unknown)}")
        return None

    field = condition.get("field")
    if not isinstance(field, str) or not field.strip():
        errors.append(f"{where}: 'field' is required (a GA4 {kind} API name)")
        return None
    field = field.strip()
    op = str(condition.get("op") or ("EXACT" if kind == "dimension" else "")).strip().upper()
    value = condition.get("values", condition.get("value"))
    case_sensitive = bool(condition.get("case_sensitive", False))

    flt = None
    if kind == "dimension":
        if op == "IN_LIST":
            if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
                errors.append(f"{where}: IN_LIST needs 'values' as a non-empty list of strings")
                return None
            flt = Filter(
                field_name=field,
                in_list_filter=Filter.InListFilter(
                    values=[str(v) for v in value], case_sensitive=case_sensitive,
                ),
            )
        elif op in _STRING_OPS:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool) or value == "":
                errors.append(f"{where}: {op} needs 'value' as a non-empty string")
                return None
            flt = Filter(
                field_name=field,
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType[op],
                    value=str(value),
                    case_sensitive=case_sensitive,
                ),
            )
        else:
            errors.append(
                f"{where}: op {op!r} is not valid for dimensions; use one of "
                f"{', '.join(_STRING_OPS)} or IN_LIST"
            )
            return None
    else:
        if op == "BETWEEN":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                errors.append(f"{where}: BETWEEN needs 'value' as [from, to]")
                return None
            low = _numeric_value(value[0], where=where, errors=errors)
            high = _numeric_value(value[1], where=where, errors=errors)
            if low is None or high is None:
                return None
            flt = Filter(
                field_name=field,
                between_filter=Filter.BetweenFilter(from_value=low, to_value=high),
            )
        elif op in _NUMERIC_OPS:
            number = _numeric_value(value, where=where, errors=errors)
            if number is None:
                return None
            flt = Filter(
                field_name=field,
                numeric_filter=Filter.NumericFilter(
                    operation=Filter.NumericFilter.Operation[_NUMERIC_OPS[op]],
                    value=number,
                ),
            )
        else:
            errors.append(
                f"{where}: op {op!r} is not valid for metrics; use one of "
                "EQUAL, LESS_THAN, LESS_THAN_OR_EQUAL, GREATER_THAN, "
                "GREATER_THAN_OR_EQUAL (or =, <, <=, >, >=) or BETWEEN"
            )
            return None

    expression = FilterExpression(filter=flt)
    if condition.get("not"):
        expression = FilterExpression(not_expression=expression)
    return expression


def build_filter_expression(conditions, *, kind: str, errors: list[str]):
    """A list of conditions (AND semantics) -> one GA4 FilterExpression.

    Returns None for an empty list or when any condition is invalid (the
    reasons are appended to ``errors``).
    """
    from google.analytics.data_v1beta.types import FilterExpression, FilterExpressionList

    if not conditions:
        return None
    if isinstance(conditions, dict):
        conditions = [conditions]
    if not isinstance(conditions, (list, tuple)):
        errors.append(f"{kind}_filter must be a list of condition objects")
        return None
    before = len(errors)
    expressions = [
        _build_filter(c, kind=kind, index=i, errors=errors)
        for i, c in enumerate(conditions)
    ]
    if len(errors) > before:
        return None
    if len(expressions) == 1:
        return expressions[0]
    return FilterExpression(and_group=FilterExpressionList(expressions=expressions))


def build_order_bys(order_by, *, dimensions: list[str], metrics: list[str], errors: list[str]):
    """``[{"field": ..., "desc": bool}]`` -> GA4 OrderBy list.

    The field must be one of the requested dimensions or metrics; that is
    how the dimension/metric ordering type is chosen.
    """
    from google.analytics.data_v1beta.types import OrderBy

    if not order_by:
        return []
    if isinstance(order_by, dict):
        order_by = [order_by]
    if not isinstance(order_by, (list, tuple)):
        errors.append("order_by must be a list like [{\"field\": \"sessions\", \"desc\": true}]")
        return []
    result = []
    for i, item in enumerate(order_by):
        where = f"order_by[{i}]"
        if not isinstance(item, dict) or not isinstance(item.get("field"), str) or not item["field"].strip():
            errors.append(f"{where}: needs 'field' (a requested dimension or metric name)")
            continue
        unknown = set(item) - {"field", "desc"}
        if unknown:
            errors.append(f"{where}: unknown key(s) {sorted(unknown)}")
            continue
        field = item["field"].strip()
        desc = bool(item.get("desc", False))
        if field in metrics:
            result.append(OrderBy(metric=OrderBy.MetricOrderBy(metric_name=field), desc=desc))
        elif field in dimensions:
            result.append(OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name=field), desc=desc))
        else:
            errors.append(
                f"{where}: field {field!r} must be one of the requested "
                "dimensions or metrics"
            )
    return result


def run_ga4_report(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    limit: int = 100,
    dimension_filter: list[dict] | None = None,
    metric_filter: list[dict] | None = None,
    order_by: list[dict] | None = None,
    offset: int = 0,
    compare_start: str = "",
    compare_end: str = "",
) -> dict:
    """Run a GA4 report with specified dimensions, metrics, and date range.

    Optional filters (AND-combined conditions), ordering, paging offset and
    a comparison period (sent as a second named date range; GA4 then labels
    every row with a ``dateRange`` value of "current" or "comparison").
    """
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    from adloop.ga4.client import get_data_client

    if not dimensions and not metrics:
        return {"error": "At least one dimension or metric must be specified."}

    dimensions = list(dimensions or [])
    metrics = list(metrics or [])
    compare_start = (compare_start or "").strip()
    compare_end = (compare_end or "").strip()

    errors: list[str] = []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        errors.append("limit must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        errors.append("offset must be 0 or a positive integer")
    if bool(compare_start) != bool(compare_end):
        errors.append("compare_start and compare_end must be given together")
    dim_expr = build_filter_expression(dimension_filter, kind="dimension", errors=errors)
    met_expr = build_filter_expression(metric_filter, kind="metric", errors=errors)
    order_bys = build_order_bys(order_by, dimensions=dimensions, metrics=metrics, errors=errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    comparing = bool(compare_start)
    date_ranges = [
        DateRange(
            start_date=date_range_start,
            end_date=date_range_end,
            name=CURRENT_RANGE if comparing else "",
        )
    ]
    if comparing:
        date_ranges.append(
            DateRange(start_date=compare_start, end_date=compare_end, name=COMPARISON_RANGE)
        )

    request_kwargs = {
        "property": property_id,
        "dimensions": [Dimension(name=d) for d in dimensions],
        "metrics": [Metric(name=m) for m in metrics],
        "date_ranges": date_ranges,
        "limit": limit,
    }
    if offset:
        request_kwargs["offset"] = offset
    if dim_expr is not None:
        request_kwargs["dimension_filter"] = dim_expr
    if met_expr is not None:
        request_kwargs["metric_filter"] = met_expr
    if order_bys:
        request_kwargs["order_bys"] = order_bys

    client = get_data_client(config)
    response = client.run_report(RunReportRequest(**request_kwargs))

    dim_headers = [h.name for h in response.dimension_headers]
    met_headers = [h.name for h in response.metric_headers]

    rows = []
    for row in response.rows:
        r = {}
        for i, val in enumerate(row.dimension_values):
            r[dim_headers[i]] = val.value
        for i, val in enumerate(row.metric_values):
            r[met_headers[i]] = val.value
        rows.append(r)

    result = {
        "property": property_id,
        "date_range": {"start": date_range_start, "end": date_range_end},
        "dimensions": dim_headers,
        "metrics": met_headers,
        "rows": rows,
        "row_count": len(rows),
        "total_row_count": response.row_count,
    }
    if comparing:
        # Every row carries a ``dateRange`` value naming one of these.
        result["comparison_date_range"] = {"start": compare_start, "end": compare_end}
        result["date_ranges"] = {
            CURRENT_RANGE: result["date_range"],
            COMPARISON_RANGE: result["comparison_date_range"],
        }
    if offset:
        result["offset"] = offset
    if response.row_count and offset + len(rows) < response.row_count:
        result["next_offset"] = offset + len(rows)
    return result


def get_ga4_metadata(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    search: str = "",
    kind: str = "all",
    custom_only: bool = False,
    include_descriptions: bool | None = None,
) -> dict:
    """List the dimensions and metrics a GA4 property can report on.

    Includes the property's custom dimensions and metrics (customEvent:*,
    customUser:*, ...). ``search`` filters case-insensitively on API name,
    UI name, category, description and deprecated API names (so searching
    the legacy name "conversions" finds "keyEvents").
    """
    from adloop.ga4.client import get_data_client

    kind = (kind or "all").strip().lower()
    if kind not in ("all", "dimensions", "metrics"):
        return {"error": "kind must be one of: all, dimensions, metrics"}
    if not property_id:
        return {
            "error": "property_id is required (falls back to the configured "
            "GA4 property when set)"
        }
    name = property_id if property_id.startswith("properties/") else f"properties/{property_id}"
    needle = (search or "").strip().lower()
    if include_descriptions is None:
        include_descriptions = bool(needle)

    metadata = get_data_client(config).get_metadata(name=f"{name}/metadata")

    def matches(item) -> bool:
        if custom_only and not item.custom_definition:
            return False
        if not needle:
            return True
        haystack = [item.api_name, item.ui_name, item.category, item.description,
                    *list(item.deprecated_api_names)]
        return any(needle in (h or "").lower() for h in haystack)

    def common(item) -> dict:
        entry = {
            "api_name": item.api_name,
            "ui_name": item.ui_name,
            "category": item.category,
            "custom": bool(item.custom_definition),
        }
        deprecated = list(item.deprecated_api_names)
        if deprecated:
            entry["deprecated_api_names"] = deprecated
        if include_descriptions:
            entry["description"] = item.description
        return entry

    result: dict = {"property": name}
    if search:
        result["search"] = search
    if kind in ("all", "dimensions"):
        dims = [common(d) for d in metadata.dimensions if matches(d)]
        result["dimensions"] = dims
        result["dimension_count"] = len(dims)
    if kind in ("all", "metrics"):
        mets = []
        for m in metadata.metrics:
            if not matches(m):
                continue
            entry = common(m)
            entry["type"] = m.type_.name if hasattr(m.type_, "name") else str(m.type_)
            if m.expression:
                entry["expression"] = m.expression
            blocked = [b.name if hasattr(b, "name") else str(b) for b in m.blocked_reasons]
            if blocked:
                entry["blocked_reasons"] = blocked
            mets.append(entry)
        result["metrics"] = mets
        result["metric_count"] = len(mets)
    return result


def run_realtime_report(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
) -> dict:
    """Run a GA4 realtime report."""
    from google.analytics.data_v1beta.types import (
        Dimension,
        Metric,
        RunRealtimeReportRequest,
    )

    from adloop.ga4.client import get_data_client

    client = get_data_client(config)

    request = RunRealtimeReportRequest(
        property=property_id,
        dimensions=[Dimension(name=d) for d in (dimensions or [])],
        metrics=[Metric(name=m) for m in (metrics or ["activeUsers"])],
    )

    response = client.run_realtime_report(request)

    dim_headers = [h.name for h in response.dimension_headers]
    met_headers = [h.name for h in response.metric_headers]

    rows = []
    for row in response.rows:
        r = {}
        for i, val in enumerate(row.dimension_values):
            r[dim_headers[i]] = val.value
        for i, val in enumerate(row.metric_values):
            r[met_headers[i]] = val.value
        rows.append(r)

    return {
        "property": property_id,
        "dimensions": dim_headers,
        "metrics": met_headers,
        "rows": rows,
        "row_count": len(rows),
    }
