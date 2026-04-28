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
    return {
        "sites": [
            {
                "site_url": s["siteUrl"],
                "permission_level": s.get("permissionLevel", "unknown"),
            }
            for s in sites
        ],
        "total": len(sites),
    }


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
) -> dict:
    """Run a Google Search Console search analytics report.

    Returns clicks, impressions, CTR, and average position for the
    requested dimensions and date range.

    dimensions: one or more of ["query", "page", "country", "device", "date"]
    date_range_start / date_range_end: ISO dates (YYYY-MM-DD) or relative
        values like "7daysAgo", "30daysAgo", "today"
    search_type: "web" (default), "image", "video", "news", "discover",
        "googleNews"
    dimension_filter_groups: optional list of GSC DimensionFilterGroup dicts
        to filter by query, page, country, or device. Example:
        [{"filters": [{"dimension": "query", "operator": "contains",
                       "expression": "analytics"}]}]
    limit: maximum number of rows to return (default 100, max 25000)
    """
    from adloop.gsc.client import get_gsc_client

    if not site_url:
        site_url = config.gsc.site_url

    if not site_url:
        return {
            "error": "site_url is required. Pass it as an argument or set "
                     "gsc.site_url in ~/.adloop/config.yaml."
        }

    if not dimensions:
        dimensions = ["query"]

    # Resolve relative date shorthands to ISO dates
    start_date = _resolve_date(date_range_start)
    end_date = _resolve_date(date_range_end)

    body: dict = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "type": search_type,
        "rowLimit": min(limit, 25_000),
        "startRow": 0,
    }

    if dimension_filter_groups:
        body["dimensionFilterGroups"] = dimension_filter_groups

    from adloop.gsc.client import get_gsc_client

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

    return {
        "site_url": site_url,
        "date_range": {"start": start_date, "end": end_date},
        "search_type": search_type,
        "dimensions": dimensions,
        "rows": formatted,
        "total_rows": len(formatted),
    }


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
