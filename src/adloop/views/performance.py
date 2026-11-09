"""The campaign performance view: get_campaign_performance as a dashboard.

Built only from the tool's own result, so the view can never show a number
the AI did not also get. Works on the full result (every campaign) and on
the compact one (totals plus the top spenders).
"""

from __future__ import annotations

from datetime import date
from typing import Any

_STATUS_LABELS = {"ENABLED": "Active", "PAUSED": "Paused", "REMOVED": "Removed"}
_TYPE_LABELS = {
    "SEARCH": "Search",
    "PERFORMANCE_MAX": "Performance Max",
    "SHOPPING": "Shopping",
    "DISPLAY": "Display",
    "VIDEO": "Video",
    "DEMAND_GEN": "Demand Gen",
}
# Campaigns shown as bars; the table lists them all.
_BAR_ROWS = 8
# A converting campaign losing this much Search impression share to budget
# is flagged (the tool uses the same threshold).
_BUDGET_LOST_FLAG = 0.2
# AdLoop's accent (getadloop.com: blue-600, blue-500 in dark mode) and the
# warning amber the bars use for campaigns without a conversion.
_LIGHT = "--primary: #2563eb; --ring: #2563eb;"
_DARK = "--primary: #3b82f6; --ring: #3b82f6;"

# The renderer ships a fixed stylesheet without responsive type sizes, so
# the few styles the view needs live here.
_CSS = """
.al-kpi { padding: 12px 14px; min-width: 0; }
.al-kpi-label { font-size: 12px; line-height: 16px; color: var(--muted-foreground); }
.al-kpi-value { font-size: 20px; line-height: 28px; font-weight: 700; margin-top: 2px; letter-spacing: -0.01em;
  font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.al-kpi-caption { font-size: 12px; line-height: 16px; color: var(--muted-foreground);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.al-bar-ok { background: var(--primary); }
.al-bar-warn { background: #f59e0b; }
.al-amount { font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
@media (min-width: 640px) { .al-kpi-value { font-size: 24px; line-height: 32px; } }
@media (max-width: 480px) { .pf-app-root { padding: 8px; } }
"""


def _row(r: dict) -> dict:
    cost = r.get("metrics.cost") or 0
    conversions = round(r.get("metrics.conversions") or 0, 1)
    channel = str(r.get("campaign.advertising_channel_type") or "")
    return {
        "id": str(r.get("campaign.id") or ""),
        "name": str(r.get("campaign.name") or ""),
        "status": _STATUS_LABELS.get(str(r.get("campaign.status")), str(r.get("campaign.status") or "")),
        "type": _TYPE_LABELS.get(channel, channel.replace("_", " ").title()),
        "cost": cost,
        "clicks": r.get("metrics.clicks") or 0,
        "impressions": r.get("metrics.impressions") or 0,
        "conversions": conversions,
        "cpa": r.get("metrics.cpa"),
        "ctr": r.get("metrics.ctr"),
        "lost_to_budget": r.get("metrics.search_budget_lost_impression_share"),
    }


def _totals(rows: list[dict], result: dict) -> dict:
    if result.get("compact") and isinstance(result.get("totals"), dict):
        t = result["totals"]
        cost, clicks = t.get("cost") or 0, t.get("clicks") or 0
        impressions, conversions = t.get("impressions") or 0, t.get("conversions") or 0
    else:
        cost = round(sum(r["cost"] for r in rows), 2)
        clicks = sum(r["clicks"] for r in rows)
        impressions = sum(r["impressions"] for r in rows)
        conversions = round(sum(r["conversions"] for r in rows), 1)
    return {
        "cost": cost,
        "clicks": clicks,
        "conversions": conversions,
        "cpa": round(cost / conversions, 2) if conversions else None,
        "ctr": clicks / impressions if impressions else None,
        "conv_rate": conversions / clicks if clicks else None,
        "cpc": round(cost / clicks, 2) if clicks else None,
    }


def _period(start: str, end: str) -> str:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return "Last 30 days"
    if first.year == last.year:
        return f"{first.day} {first:%b} to {last.day} {last:%b %Y}"
    return f"{first.day} {first:%b %Y} to {last.day} {last:%b %Y}"


def _campaigns(n: int) -> str:
    return f"{n} campaign" if n == 1 else f"{n} campaigns"


def _names(items: list[dict], limit: int = 3) -> str:
    names = [str(i.get("name")) for i in items[:limit]]
    more = len(items) - limit
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def _alerts(rows: list[dict], result: dict) -> dict:
    """What needs attention, from the same numbers as the rest of the view."""
    if result.get("compact"):
        idle = [i for i in result.get("zero_conversion_spenders") or [] if isinstance(i, dict)]
    else:
        idle = [{"name": r["name"], "cost": r["cost"]} for r in rows if r["cost"] > 0 and not r["conversions"]]
    limited = [i for i in result.get("budget_limited_converters") or [] if isinstance(i, dict)]
    if not limited:
        limited = [
            {"name": r["name"], "search_budget_lost_impression_share_pct": round(r["lost_to_budget"] * 100)}
            for r in rows
            if r["conversions"] and (r["lost_to_budget"] or 0) >= _BUDGET_LOST_FLAG
        ]
    alerts = {}
    if idle:
        alerts["idle"] = {
            "cost": round(sum(i.get("cost") or 0 for i in idle), 2),
            "title": f"{_campaigns(len(idle))} spent without a conversion",
            "names": _names(idle),
        }
    if limited:
        top = limited[0]
        lost = top.get("search_budget_lost_impression_share_pct")
        detail = f"{top.get('name')} loses {lost:.0f}% of its Search impressions to budget" if lost is not None else ""
        if len(limited) > 1:
            detail += f", and {_campaigns(len(limited) - 1)} more"
        alerts["limited"] = {
            "title": f"{_campaigns(len(limited))} {'is' if len(limited) == 1 else 'are'} held back by budget while converting",
            "detail": detail + ".",
        }
    return alerts


def campaign_performance_view(
    result: dict, *, date_range_start: str = "", date_range_end: str = ""
) -> Any:
    """Return the PrefabApp for a get_campaign_performance result."""
    from prefab_ui.actions.mcp import SendMessage
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        Alert,
        AlertDescription,
        AlertTitle,
        Badge,
        Card,
        CardContent,
        CardHeader,
        CardTitle,
        Column,
        DataTable,
        DataTableColumn,
        Div,
        Grid,
        Heading,
        Muted,
        Progress,
        Row,
        Text,
    )
    from prefab_ui.components.control_flow import ForEach
    from prefab_ui.themes import Theme

    source = result.get("campaigns_top_spend") if result.get("compact") else result.get("campaigns")
    raw_rows = [r for r in (source or []) if isinstance(r, dict)]
    rows = [_row(r) for r in raw_rows]
    currency = next((str(r.get("metrics.currency")) for r in raw_rows if r.get("metrics.currency")), "")
    if not currency and isinstance(result.get("totals"), dict):
        currency = str(result["totals"].get("currency") or "")
    totals = _totals(rows, result)
    total_campaigns = result.get("total_campaigns", len(rows))
    period = _period(date_range_start, date_range_end) if date_range_start and date_range_end else "Last 30 days"

    bar_rows = [r for r in rows if r["cost"] > 0][:_BAR_ROWS]
    top_cost = max((r["cost"] for r in bar_rows), default=0) or 1
    # Every number lives once, in state: ChatGPT shows the model the whole
    # view next to the text answer, so a second copy would cost it twice.
    state = {
        "totals": totals,
        "rows": rows,
        "bars": [
            {
                "name": r["name"],
                "cost": r["cost"],
                "share": round(r["cost"] / top_cost * 100, 1),
                "bar": "al-bar-ok" if r["conversions"] else "al-bar-warn",
            }
            for r in bar_rows
        ],
        "alerts": _alerts(rows, result),
    }
    money = (lambda path: f"{{{{ {path} | currency:{currency} }}}}") if currency else (
        lambda path: f"{{{{ {path} | number:2 }}}}"
    )

    def kpi(label: str, value: str, caption: str | None) -> None:
        with Card(css_class="al-kpi"):
            with Column(gap=0):
                Text(label, css_class="al-kpi-label")
                Text(value, css_class="al-kpi-value")
                if caption:
                    Text(caption, css_class="al-kpi-caption")

    theme = Theme(light_css=_LIGHT, dark_css=_DARK, gradient=False)
    with PrefabApp(title="Campaign performance", state=state, css=[_CSS], theme=theme) as app:
        with Column(gap=4):
            with Column(gap=1):
                Heading("Campaign performance", level=4)
                with Row(gap=2, align="center", css_class="flex-wrap"):
                    Badge("Google Ads", variant="secondary")
                    Muted(f"{period} · {_campaigns(total_campaigns)}")

            if not rows:
                Text("No campaign had impressions or spend in this period.")
                return app

            with Grid(columns={"default": 2, "md": 4}, gap=3):
                kpi("Spend", money("totals.cost"), _campaigns(total_campaigns))
                kpi(
                    "Conversions",
                    "{{ totals.conversions | number:1 }}",
                    "{{ totals.conv_rate | percent:2 }} of clicks" if totals["conv_rate"] is not None else None,
                )
                kpi(
                    "Cost per conversion",
                    money("totals.cpa") if totals["cpa"] is not None else "–",
                    f"{money('totals.cpc')} per click" if totals["cpc"] is not None else None,
                )
                kpi(
                    "Clicks",
                    "{{ totals.clicks | number }}",
                    "{{ totals.ctr | percent:2 }} CTR" if totals["ctr"] is not None else None,
                )

            alerts = state["alerts"]
            if "idle" in alerts:
                with Alert(variant="warning", icon="circle-alert"):
                    AlertTitle(f"{alerts['idle']['title']}: {money('alerts.idle.cost')}")
                    AlertDescription("{{ alerts.idle.names }}")
            if "limited" in alerts:
                with Alert(variant="info", icon="trending-up"):
                    AlertTitle("{{ alerts.limited.title }}")
                    AlertDescription("{{ alerts.limited.detail }}")

            if len(bar_rows) > 1:
                with Card():
                    with CardHeader():
                        CardTitle("Spend by campaign")
                    with CardContent():
                        with Column(gap=3):
                            with ForEach("bars"):
                                # Name above its bar: campaign names are long,
                                # and a phone has no room beside them.
                                with Grid(column_template="minmax(0, 1fr) auto", gap=3, align="end"):
                                    with Column(gap=1):
                                        Text("{{ $item.name }}", css_class="truncate text-sm")
                                        Progress(value="{{ $item.share }}", max=100, size="sm", indicator_class="{{ $item.bar }}")
                                    Text(money("$item.cost"), css_class="al-amount")
                            if any(not r["conversions"] for r in bar_rows):
                                Muted("Amber: no conversion in this period.")

            money_format = f"currency:{currency}" if currency else "number:2"
            columns = [
                DataTableColumn(key="name", header="Campaign", sortable=True, min_width="160px"),
                DataTableColumn(key="status", header="Status", sortable=True),
                DataTableColumn(key="type", header="Type", sortable=True),
                DataTableColumn(key="cost", header="Spend", sortable=True, align="right", format=money_format),
                DataTableColumn(key="clicks", header="Clicks", sortable=True, align="right", format="number"),
                DataTableColumn(key="conversions", header="Conv.", sortable=True, align="right", format="number:1"),
                DataTableColumn(key="cpa", header="CPA", sortable=True, align="right", format=money_format),
                DataTableColumn(key="ctr", header="CTR", sortable=True, align="right", format="percent:2"),
                DataTableColumn(
                    key="lost_to_budget", header="Lost to budget", sortable=True, align="right", format="percent:0"
                ),
            ]
            ask = SendMessage(
                "Look into the Google Ads campaign \"{{ $event.name }}\" "
                "(ID {{ $event.id }}) for this period: what drives its cost "
                "per conversion, and what should change?"
            )
            with Card():
                with CardHeader():
                    CardTitle("All campaigns" if not result.get("compact") else "Top campaigns by spend")
                with CardContent():
                    # Nine columns are wider than a chat pane: the table
                    # scrolls, not the page.
                    with Div(css_class="overflow-x-auto max-w-full"):
                        DataTable(
                            columns=columns,
                            rows="{{ rows }}",
                            search=len(rows) > 10,
                            search_placeholder="Find a campaign",
                            paginated=len(rows) > 15,
                            page_size=15,
                            on_row_click=ask,
                        )
            footnote = "Click a campaign to ask about it. Lost to budget is the Search impression share lost to budget."
            if result.get("compact") and total_campaigns > len(rows):
                footnote = f"Showing the {len(rows)} campaigns with the most spend of {total_campaigns}. " + footnote
            Muted(footnote)
    return app
