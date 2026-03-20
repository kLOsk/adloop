"""GAQL (Google Ads Query Language) tool — run arbitrary queries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def execute_query(
    config: AdLoopConfig, customer_id: str, query: str
) -> list[dict]:
    """Execute a GAQL query and return results as a list of flat dicts.

    Shared by run_gaql (the MCP tool) and the individual read tools
    in ads/read.py.
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    client = get_ads_client(config)
    service = client.get_service("GoogleAdsService")
    cid = normalize_customer_id(customer_id)

    fields = _parse_select_fields(query)

    rows = []
    for row in service.search(customer_id=cid, query=query):
        r = {}
        for field in fields:
            r[field] = _extract_field(row, field)
        rows.append(r)

    return rows


def run_gaql(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    query: str = "",
    format: str = "table",
) -> dict:
    """Execute an arbitrary GAQL query and return formatted results."""
    if not query:
        return {"error": "Query string is required."}

    try:
        rows = execute_query(config, customer_id, query)
    except Exception as e:
        return {"error": _parse_gaql_error(e), "query": query}

    if format == "table":
        return _format_table(rows, query)
    elif format == "csv":
        return _format_csv(rows, query)
    return {"rows": rows, "row_count": len(rows), "query": query}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GAQL_ERROR_HINTS = {
    "DEVELOPER_TOKEN_NOT_APPROVED": (
        "Your Google Ads developer token is only approved for test accounts. "
        "Apply for Basic or Standard access in the Google Ads API Center, "
        "or use a test account."
    ),
    "DEVELOPER_TOKEN_INVALID": (
        "Your Google Ads developer token is invalid. Update "
        "`ads.developer_token` in `~/.adloop/config.yaml` using the token "
        "from your manager account API Center."
    ),
    "EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE": (
        "Fields used in ORDER BY or HAVING must also appear in the SELECT clause. "
        "Add the missing field to your SELECT."
    ),
    "UNRECOGNIZED_FIELD": "Check the field name — it may be misspelled or not available on this resource.",
    "INVALID_RESOURCE_NAME": "The resource name in FROM is invalid. Check GAQL resource names.",
    "PROHIBITED_FIELD_COMBINATION": (
        "Some fields can't be selected together. Metrics and certain resource fields "
        "may conflict. Try splitting into separate queries."
    ),
    "QUERY_NOT_ALLOWED": "This query type is not supported for this resource or access level.",
    "INVALID_ARGUMENT": "Check your WHERE clause values — date formats, status strings, and IDs must be valid.",
}


def _parse_gaql_error(exc: Exception) -> str:
    """Extract a human-readable message from Google Ads gRPC errors."""
    raw = str(exc)
    for code, hint in _GAQL_ERROR_HINTS.items():
        if code in raw:
            return f"{code}: {hint}"
    if len(raw) > 500:
        return raw[:500] + "..."
    return raw


def _parse_select_fields(query: str) -> list[str]:
    """Extract field names from the SELECT clause of a GAQL query."""
    match = re.search(r"SELECT\s+(.*?)\s+FROM", query, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return [f.strip() for f in match.group(1).split(",") if f.strip()]


def _extract_field(row: object, field_path: str) -> object:
    """Walk a dotted field path on a proto-plus GoogleAdsRow."""
    obj = row
    for part in field_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            return None
    return _to_python(obj)


def _to_python(obj: object) -> object:
    """Convert proto-plus / protobuf values to plain Python types."""
    if obj is None:
        return None
    if isinstance(obj, (str, float, bool)):
        return obj
    if isinstance(obj, int):
        # Proto-plus enums are int subclasses with a .name attribute
        if type(obj) is not int and hasattr(obj, "name"):
            return obj.name
        return obj
    # Repeated fields (headlines, final_urls, etc.)
    try:
        return [_to_python(item) for item in obj]
    except TypeError:
        pass
    # AdTextAsset and similar message types
    if hasattr(obj, "text") and isinstance(getattr(obj, "text", None), str):
        return obj.text
    return str(obj)


def _format_table(rows: list[dict], query: str) -> dict:
    """Format query results as an aligned text table."""
    if not rows:
        return {"table": "(no results)", "row_count": 0, "query": query}

    headers = list(rows[0].keys())
    widths = {h: len(h) for h in headers}

    str_rows = []
    for row in rows:
        sr = {}
        for h in headers:
            val = row.get(h)
            if isinstance(val, list):
                s = ", ".join(str(v) for v in val)
            else:
                s = str(val) if val is not None else ""
            sr[h] = s
            widths[h] = max(widths[h], len(s))
        str_rows.append(sr)

    header_line = " | ".join(h.ljust(widths[h]) for h in headers)
    separator = "-+-".join("-" * widths[h] for h in headers)
    data_lines = [
        " | ".join(sr[h].ljust(widths[h]) for h in headers) for sr in str_rows
    ]

    table = "\n".join([header_line, separator, *data_lines])
    return {"table": table, "row_count": len(rows), "query": query}


def _format_csv(rows: list[dict], query: str) -> dict:
    """Format query results as CSV."""
    if not rows:
        return {"csv": "", "row_count": 0, "query": query}

    import csv
    import io

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    for row in rows:
        writer.writerow({k: v if not isinstance(v, list) else "; ".join(str(i) for i in v) for k, v in row.items()})

    return {"csv": output.getvalue(), "row_count": len(rows), "query": query}
