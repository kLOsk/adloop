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

# AdLoop's mark (the loop from getadloop.com's icon), drawn in white.
_LOOP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">'
    '<path d="M16 16 C 13.5 11.5, 6.5 11.5, 6.5 16 C 6.5 20.5, 13.5 20.5, 16 16 '
    'C 18.5 11.5, 25.5 11.5, 25.5 16 C 25.5 20.5, 18.5 20.5, 16 16 Z" '
    'stroke="currentColor" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/></svg>'
)

# The renderer ships a fixed stylesheet without responsive type sizes or
# gradients, so the styles the view needs live here.
_CSS = """
.al-hero { position: relative; overflow: hidden; border-radius: 16px; padding: 18px;
  color: #fff; background: linear-gradient(135deg, #2563eb 0%, #4f46e5 55%, #7c3aed 100%);
  box-shadow: 0 10px 30px -12px rgba(37, 99, 235, .55); }
.dark .al-hero { background: linear-gradient(135deg, #1d4ed8 0%, #4338ca 55%, #6d28d9 100%);
  box-shadow: none; }
.al-hero-glow { position: absolute; right: -80px; top: -90px; width: 260px; height: 260px;
  border-radius: 999px; background: radial-gradient(circle, rgba(255,255,255,.28), transparent 68%);
  pointer-events: none; }
.al-hero-art { position: absolute; right: -48px; top: -64px; width: 240px; color: #fff;
  opacity: .09; pointer-events: none; }
.al-hero-body { position: relative; }
.al-logo { width: 26px; height: 26px; border-radius: 8px; padding: 3px; color: #fff;
  background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.25); }
.al-eyebrow { font-size: 12px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: rgba(255,255,255,.85); }
.al-pill { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 500; color: #fff;
  background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.22); white-space: nowrap; }
.al-dot { width: 7px; height: 7px; border-radius: 999px; background: #fbbf24; }
.al-dot-ok { background: #4ade80; }
.al-title, .al-sub, .al-kpi-label, .al-kpi-value, .al-kpi-caption { display: block; }
.al-title { font-size: 22px; line-height: 28px; font-weight: 700; letter-spacing: -0.02em; color: #fff;
  margin-top: 14px; }
.al-sub { font-size: 13px; color: rgba(255,255,255,.78); margin-top: 2px; }
.al-kpis { margin-top: 16px; }
.al-kpi { min-width: 0; padding: 10px 12px; border-radius: 12px;
  background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.18); }
.al-kpi-label { font-size: 12px; line-height: 16px; color: rgba(255,255,255,.8); }
.al-kpi-value { font-size: 20px; line-height: 28px; font-weight: 700; margin-top: 2px; letter-spacing: -0.01em;
  color: #fff; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.al-kpi-caption { font-size: 12px; line-height: 16px; color: rgba(255,255,255,.72);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.al-bar-ok { background: var(--primary); }
.al-bar-warn { background: #f59e0b; }
.al-amount { font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.al-name { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* The table shows the columns that fit its own width (not the window's:
   hosts pad and scale the frame, and bring wider fonts: claude.ai lays it
   out at ~730px with a ~650px table box), so a chat pane never scrolls
   sideways. The tiers leave room for a host font. */
.al-table { container-type: inline-size; }
@container (max-width: 919px) { .al-col-xl { display: none; } }
@container (max-width: 759px) { .al-col-lg { display: none; } .al-name { max-width: 200px; } }
@container (max-width: 419px) {
  .al-col-md { display: none; }
  .al-name { max-width: 130px; }
  .al-table th, .al-table td { padding-left: 6px; padding-right: 6px; }
}
@media (min-width: 640px) {
  .al-hero { padding: 22px; }
  .al-title { font-size: 26px; line-height: 32px; }
  .al-kpi-value { font-size: 24px; line-height: 32px; }
}
@media (max-width: 480px) {
  .pf-app-root { padding: 8px; }
  .al-tight { padding-left: 12px; padding-right: 12px; }
}
"""


def _row(r: dict) -> dict:
    cost = r.get("metrics.cost") or 0
    conversions = round(r.get("metrics.conversions") or 0, 1)
    channel = str(r.get("campaign.advertising_channel_type") or "")
    name = str(r.get("campaign.name") or "")
    status = _STATUS_LABELS.get(str(r.get("campaign.status")), str(r.get("campaign.status") or ""))
    return {
        "id": str(r.get("campaign.id") or ""),
        "name": name,
        # Narrow views hide the Status column; a paused campaign still says so.
        "label": name if status == "Active" else f"{name} · {status}",
        "status": status,
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


def _column(key: str, header: str, fmt: str | None = None, *, tier: str = "", cell_class: str = "") -> Any:
    """A sortable table column; ``tier`` hides it below that width (md, lg, xl)."""
    from prefab_ui.components import DataTableColumn

    hide = f"al-col-{tier}" if tier else ""
    numeric = fmt is not None
    return DataTableColumn(
        key=key,
        header=header,
        sortable=True,
        format=fmt,
        align="right" if numeric else "left",
        header_class=hide or None,
        cell_class=" ".join(c for c in (hide, cell_class) if c) or None,
    )


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
        Card,
        CardContent,
        CardHeader,
        CardTitle,
        Column,
        DataTable,
        Div,
        Grid,
        Muted,
        Progress,
        Row,
        Svg,
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
        with Column(gap=0, css_class="al-kpi"):
            Text(label, css_class="al-kpi-label")
            Text(value, css_class="al-kpi-value")
            if caption:
                Text(caption, css_class="al-kpi-caption")

    flagged = len(state["alerts"])
    theme = Theme(light_css=_LIGHT, dark_css=_DARK, gradient=False)
    with PrefabApp(title="Campaign performance", state=state, css=[_CSS], theme=theme) as app:
        with Column(gap=4):
            with Div(css_class="al-hero"):
                Div(css_class="al-hero-glow")
                with Div(css_class="al-hero-art"):
                    Svg(_LOOP, width="100%")
                with Column(gap=0, css_class="al-hero-body"):
                    with Row(gap=2, align="center", justify="between"):
                        with Row(gap=2, align="center"):
                            with Div(css_class="al-logo"):
                                Svg(_LOOP, width="100%", height="100%")
                            Text("Google Ads", css_class="al-eyebrow")
                        if rows:
                            with Row(gap=2, align="center", css_class="al-pill"):
                                Div(css_class="al-dot" if flagged else "al-dot al-dot-ok")
                                Text(f"{flagged} to review" if flagged else "Nothing flagged")
                    Text("Campaign performance", css_class="al-title")
                    Text(f"{period} · {_campaigns(total_campaigns)}", css_class="al-sub")
                    if rows:
                        with Grid(columns={"default": 2, "md": 4}, gap=2, css_class="al-kpis"):
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

            if not rows:
                Text("No campaign had impressions or spend in this period.")
                return app

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
                _column("label", "Campaign", cell_class="al-name"),
                _column("status", "Status", tier="xl"),
                _column("type", "Type", tier="xl"),
                _column("cost", "Spend", money_format),
                _column("clicks", "Clicks", "number", tier="lg"),
                _column("conversions", "Conv.", "number:1"),
                _column("cpa", "CPA", money_format, tier="md"),
                _column("ctr", "CTR", "percent:2", tier="lg"),
                _column("lost_to_budget", "Lost to budget", "percent:0", tier="lg"),
            ]
            ask = SendMessage(
                "Look into the Google Ads campaign \"{{ $event.name }}\" "
                "(ID {{ $event.id }}) for this period: what drives its cost "
                "per conversion, and what should change?"
            )
            with Card():
                with CardHeader():
                    CardTitle("All campaigns" if not result.get("compact") else "Top campaigns by spend")
                with CardContent(css_class="al-tight"):
                    # Columns drop out as the view narrows (see _CSS); the
                    # scroll container is only a safety net.
                    with Div(css_class="al-table overflow-x-auto max-w-full"):
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
