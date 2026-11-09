"""The campaign performance view: get_campaign_performance as a dashboard.

Built only from the tool's own result, so the view can never show a number
the AI did not also get. Works on the full result (every campaign) and on
the compact one (totals plus the top spenders).
"""

from __future__ import annotations

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
# Campaigns shown in the spend chart; the table lists them all.
_CHART_ROWS = 8
# Chat apps already frame the view; on a phone, Prefab's own padding would
# leave the numbers too little room.
_CSS = "@media (max-width: 480px) { .pf-app-root { padding: 8px; } }"


def _row(r: dict) -> dict:
    cost = r.get("metrics.cost") or 0
    return {
        "id": str(r.get("campaign.id") or ""),
        "name": str(r.get("campaign.name") or ""),
        "status": _STATUS_LABELS.get(str(r.get("campaign.status")), str(r.get("campaign.status") or "")),
        "type": _TYPE_LABELS.get(
            str(r.get("campaign.advertising_channel_type")),
            str(r.get("campaign.advertising_channel_type") or "").replace("_", " ").title(),
        ),
        "cost": cost,
        "clicks": r.get("metrics.clicks") or 0,
        "impressions": r.get("metrics.impressions") or 0,
        "conversions": round(r.get("metrics.conversions") or 0, 1),
        "cpa": r.get("metrics.cpa"),
        "ctr": r.get("metrics.ctr"),
        "lost_to_budget": r.get("search_budget_lost_impression_share"),
    }


def _totals(rows: list[dict], result: dict) -> dict:
    if result.get("compact") and isinstance(result.get("totals"), dict):
        t = result["totals"]
        ctr = t.get("ctr_pct")
        return {
            "cost": t.get("cost") or 0,
            "clicks": t.get("clicks") or 0,
            "conversions": t.get("conversions") or 0,
            "cpa": t.get("cpa"),
            "ctr": ctr / 100 if ctr is not None else None,
            "currency": t.get("currency") or "",
        }
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
        "currency": "",
    }


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "–"
    return f"{value:,.2f} {currency}".strip()


def _period(date_range_start: str, date_range_end: str) -> str:
    if date_range_start and date_range_end:
        return f"{date_range_start} to {date_range_end}"
    return "Last 30 days"


def campaign_performance_view(
    result: dict, *, date_range_start: str = "", date_range_end: str = ""
) -> Any:
    """Return the PrefabApp for a get_campaign_performance result."""
    from prefab_ui.actions.mcp import SendMessage
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        Alert,
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
        Metric,
        Muted,
        Row,
        Progress,
        Text,
    )
    from prefab_ui.components.control_flow import ForEach
    from prefab_ui.rx import Rx

    source = result.get("campaigns_top_spend") if result.get("compact") else result.get("campaigns")
    raw_rows = [r for r in (source or []) if isinstance(r, dict)]
    rows = [_row(r) for r in raw_rows]
    currency = next((str(r.get("metrics.currency")) for r in raw_rows if r.get("metrics.currency")), "")
    totals = _totals(rows, result)
    currency = currency or totals["currency"]
    total_campaigns = result.get("total_campaigns", len(rows))
    period = _period(date_range_start, date_range_end)

    zero_conversion = [r for r in rows if r["cost"] > 0 and not r["conversions"]]
    notes = [str(i) for i in (result.get("insights") or []) if i]
    if zero_conversion and not result.get("compact"):
        wasted = round(sum(r["cost"] for r in zero_conversion), 2)
        notes.insert(
            0,
            f"{len(zero_conversion)} campaign(s) spent {_money(wasted, currency)} "
            "without a conversion.",
        )

    chart_rows = [r for r in rows if r["cost"] > 0][:_CHART_ROWS]
    top_cost = max((r["cost"] for r in chart_rows), default=0) or 1
    state = {
        "totals": totals,
        "bars": [
            {"name": r["name"], "cost": r["cost"], "share": round(r["cost"] / top_cost * 100, 1)}
            for r in chart_rows
        ],
    }

    def money(path: str) -> Rx:
        return Rx(path).currency(currency) if currency else Rx(path).number(2)

    with PrefabApp(title="Campaign performance", state=state, css=[_CSS]) as app:
        with Column(gap=4):
            with Column(gap=0):
                Heading("Campaign performance", level=3)
                Muted(f"{period} · {total_campaigns} campaign(s)")

            if not rows:
                Text("No campaign had impressions or spend in this period.")
            else:
                with Grid(columns={"default": 2, "sm": 3, "lg": 5}, gap=3):
                    for label, value in (
                        ("Spend", money("totals.cost")),
                        ("Clicks", Rx("totals.clicks").number()),
                        ("Conversions", Rx("totals.conversions").number(1)),
                        ("Cost per conversion", money("totals.cpa") if totals["cpa"] is not None else "–"),
                        ("Click-through rate", Rx("totals.ctr").percent(2) if totals["ctr"] is not None else "–"),
                    ):
                        with Card(css_class="p-3"):
                            Metric(label=label, value=value)

                for note in notes[:4]:
                    with Alert(variant="warning", icon="triangle-alert"):
                        Text(note)

                if len(chart_rows) > 1:
                    with Card():
                        with CardHeader():
                            CardTitle("Spend by campaign")
                        with CardContent():
                            with Column(gap=3):
                                with ForEach("bars"):
                                    # Name above its bar: campaign names are
                                    # long, and a phone has no room beside it.
                                    with Grid(column_template="minmax(0, 1fr) auto", gap=3, align="end"):
                                        with Column(gap=1):
                                            Text("{{ $item.name }}", css_class="truncate text-sm")
                                            Progress(value="{{ $item.share }}", max=100, size="sm")
                                        Text(money("$item.cost"), align="right", css_class="text-sm tabular-nums")

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
                # Nine columns are wider than a chat pane: the table scrolls,
                # not the page.
                with Div(css_class="overflow-x-auto max-w-full"):
                    DataTable(
                        columns=columns,
                        rows=rows,
                        search=len(rows) > 10,
                        search_placeholder="Find a campaign",
                        paginated=len(rows) > 15,
                        page_size=15,
                        on_row_click=ask,
                    )
                Muted("Click a campaign to ask about it. Lost to budget is Search impression share.")
                if result.get("compact") and total_campaigns > len(rows):
                    Muted(f"Showing the {len(rows)} campaigns with the most spend of {total_campaigns}.")
    return app
