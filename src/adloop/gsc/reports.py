"""Google Search Console report tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def list_gsc_sites(config: AdLoopConfig) -> dict:
    """List all Google Search Console properties the authenticated user can access."""
    from adloop.gsc.client import get_gsc_client

    client = get_gsc_client(config)
    result = client.sites().list().execute()

    sites = result.get("siteEntry", [])
    payload = {
        "sites": [
            {
                "site_url": s["siteUrl"],
                "permission_level": s.get("permissionLevel", "unknown"),
            }
            for s in sites
        ],
        "total": len(sites),
    }
    if not sites:
        # Same guard as list_gtm_accounts: an empty list must not be
        # misread as "audited and healthy".
        payload["insights"] = [
            "This Google account has NO Search Console properties. Do "
            "not report Search Console data as checked — report that "
            "GSC is not set up for this account."
        ]
    return payload


MAX_ROWS_PER_REQUEST = 25_000

# Documented request values (case-insensitive on the API side). "hourly_all"
# is the data state the "hour" dimension requires.
DATA_STATES = ("final", "all", "hourly_all")
AGGREGATION_TYPES = ("auto", "byPage", "byProperty")

# Accepted spellings -> the dimension name sent to the API and used as the
# row key. Anything else passes through unchanged for the API to judge.
_DIMENSION_ALIASES = {
    "search_appearance": "searchAppearance",
    "searchappearance": "searchAppearance",
}


def _normalize_dimension(dim: str) -> str:
    key = str(dim).strip()
    return _DIMENSION_ALIASES.get(key.lower(), key.lower() if key.isupper() else key)


def run_gsc_report(
    config: AdLoopConfig,
    *,
    site_url: str = "",
    dimensions: list[str] | None = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    limit: int = 100,
    search_type: Literal["web", "image", "video", "news", "discover", "googleNews"] = "web",
    dimension_filter_groups: list[dict] | None = None,
    data_state: str = "final",
    aggregation_type: str = "auto",
    start_row: int = 0,
) -> dict:
    """Run a Google Search Console search analytics report.

    Returns clicks, impressions, CTR, and average position for the
    requested dimensions and date range.

    dimensions: one or more of ["query", "page", "country", "device", "date",
        "searchAppearance", "hour"]. "hour" needs data_state "hourly_all",
        which is selected automatically when "hour" is requested.
    date_range_start / date_range_end: ISO dates (YYYY-MM-DD) or relative
        values like "7daysAgo", "30daysAgo", "today"
    search_type: "web" (default), "image", "video", "news", "discover",
        "googleNews"
    dimension_filter_groups: optional list of GSC DimensionFilterGroup dicts
        to filter by query, page, country, device or searchAppearance.
        Example:
        [{"filters": [{"dimension": "query", "operator": "contains",
                       "expression": "analytics"}]}]
    limit: maximum number of rows to return (default 100, max 25000 per
        request; page with start_row)
    data_state: "final" (default, finalized data only), "all" (adds fresh,
        still-changing data) or "hourly_all"
    aggregation_type: "auto" (default), "byPage" or "byProperty"
    start_row: zero-based offset of the first row, for paging past 25000
    """
    from adloop.gsc.client import get_gsc_client

    if not site_url:
        site_url = config.gsc.site_url

    if not site_url:
        from adloop.runtime import default_setting_hint

        return {
            "error": "site_url is required. Pass it as an argument (see "
                     "list_gsc_sites) or "
                     + default_setting_hint("gsc.site_url", "Settings → Google & accounts")
                     + "."
        }

    dimensions = [_normalize_dimension(d) for d in (dimensions or ["query"])]

    data_state = (data_state or "final").strip().lower()
    if data_state not in DATA_STATES:
        return {"error": f"data_state must be one of {', '.join(DATA_STATES)}"}
    aggregation_by_lower = {a.lower(): a for a in AGGREGATION_TYPES}
    aggregation_type = aggregation_by_lower.get(
        (aggregation_type or "auto").strip().lower()
    )
    if aggregation_type is None:
        return {
            "error": "aggregation_type must be one of "
                     f"{', '.join(AGGREGATION_TYPES)}"
        }
    if start_row < 0:
        return {"error": "start_row must be 0 or greater"}

    notes: list[str] = []
    if "hour" in dimensions and data_state != "hourly_all":
        notes.append(
            f"data_state switched from '{data_state}' to 'hourly_all': the "
            "hour dimension requires it. Hourly data covers roughly the last "
            "10 days and includes partial data."
        )
        data_state = "hourly_all"
    filters_page = any(
        f.get("dimension") == "page"
        for group in dimension_filter_groups or []
        for f in group.get("filters", []) or []
        if isinstance(f, dict)
    )
    if aggregation_type == "byProperty" and ("page" in dimensions or filters_page):
        return {
            "error": "aggregation_type 'byProperty' cannot be combined with "
                     "grouping or filtering by page (Search Console rejects it). "
                     "Use 'auto' or 'byPage'."
        }

    # Resolve relative date shorthands to ISO dates
    start_date = _resolve_date(date_range_start)
    end_date = _resolve_date(date_range_end)

    row_limit = max(1, min(limit, MAX_ROWS_PER_REQUEST))
    body: dict = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "type": search_type,
        "rowLimit": row_limit,
        "startRow": start_row,
        "dataState": data_state,
        "aggregationType": aggregation_type,
    }

    if dimension_filter_groups:
        body["dimensionFilterGroups"] = dimension_filter_groups

    client = get_gsc_client(config)
    result = (
        client.searchanalytics()
        .query(siteUrl=site_url, body=body)
        .execute()
    )

    rows = result.get("rows", [])
    formatted = []
    for row in rows:
        entry: dict = {}
        for i, dim in enumerate(dimensions):
            entry[dim] = row["keys"][i]
        entry["clicks"] = row.get("clicks", 0)
        entry["impressions"] = row.get("impressions", 0)
        entry["ctr"] = round(row.get("ctr", 0.0) * 100, 2)  # as %
        entry["position"] = round(row.get("position", 0.0), 1)
        formatted.append(entry)

    payload = {
        "site_url": site_url,
        "date_range": {"start": start_date, "end": end_date},
        "search_type": search_type,
        "dimensions": dimensions,
        "data_state": data_state,
        "aggregation_type": aggregation_type,
        "response_aggregation_type": result.get("responseAggregationType"),
        "start_row": start_row,
        "rows": formatted,
        "total_rows": len(formatted),
    }
    if len(formatted) >= row_limit:
        # A full page: there may be more rows behind it.
        payload["next_start_row"] = start_row + len(formatted)
    if result.get("metadata"):
        payload["metadata"] = result["metadata"]
    if notes:
        payload["notes"] = notes
    return payload


def _resolve_date(value: str) -> str:
    """Resolve a relative date string to ISO format (YYYY-MM-DD)."""
    import re
    from datetime import date, timedelta

    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value

    today = date.today()
    if value == "today":
        return today.isoformat()
    if value == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    m = re.match(r"^(\d+)daysAgo$", value)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()

    # Unknown format — pass through and let the API surface the error
    return value
