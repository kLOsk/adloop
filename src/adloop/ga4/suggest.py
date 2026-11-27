"""A GA4 annotation suggested after an ad change that moves traffic.

When AdLoop applies a budget, bidding, targeting or status change, the GA4
charts later show a jump with no explanation. The apply result therefore
carries a ready annotation (title, date, description) for the AI to offer.
Nothing is written to GA4 by this: the user approves the annotation like
any other change (draft_ga4_annotation), because the GA4 property may be a
client's.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

_STATUS_VERBS = {
    "pause_entity": "paused",
    "enable_entity": "enabled",
    "remove_entity": "removed",
}
_REDDIT_STATUS = {"PAUSED": "paused", "ACTIVE": "enabled", "ARCHIVED": "archived"}
# update_campaign keys that change who sees the ads or how much they cost;
# URL options and tracking templates do not move traffic.
_TARGETING_KEYS = (
    "geo_target_ids", "geo_exclude_ids", "language_ids", "location_option", "negative_location_option",
)
_NETWORK_KEYS = ("search_partners_enabled", "display_network_enabled")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _amount(value) -> str:
    if isinstance(value, (int, float)):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _google_parts(plan: ChangePlan) -> list[str]:
    changes = plan.changes
    parts = []
    if "daily_budget" in changes:
        before = changes.get("current_daily_budget")
        new = _amount(changes["daily_budget"])
        parts.append(f"budget {_amount(before)} → {new}" if before is not None else f"budget {new}")
    if "bidding_strategy" in changes:
        parts.append(f"bidding {str(changes['bidding_strategy']).lower().replace('_', ' ')}")
    elif "max_cpc" in changes:
        parts.append(f"max CPC {_amount(changes['max_cpc'])}")
    if any(k in changes for k in _TARGETING_KEYS):
        parts.append("targeting")
    if "ad_schedule" in changes:
        parts.append("ad schedule")
    if any(k in changes for k in _NETWORK_KEYS):
        parts.append("networks")
    if "keyword_match_type" in changes:
        parts.append(f"match type {str(changes['keyword_match_type']).lower()}")
    return parts


def _reddit_parts(changes: dict) -> list[str]:
    display = changes.get("display") or {}
    parts = []
    budget = display.get("budget")
    if isinstance(budget, dict) and "to" in budget:
        parts.append(f"budget {_amount(budget.get('from'))} → {_amount(budget['to'])}")
    bid = display.get("bid_value")
    if isinstance(bid, dict) and "to" in bid:
        parts.append(f"bid {_amount(bid.get('from'))} → {_amount(bid['to'])}")
    if "targeting" in display:
        parts.append("targeting")
    if "schedule" in display:
        parts.append("schedule")
    return parts


def _headline(plan: ChangePlan) -> tuple[str, str] | None:
    """(platform, what happened) for changes worth a note, else None."""
    op, changes = plan.operation, plan.changes
    name = (
        changes.get("campaign_name") or changes.get("entity_name") or changes.get("ad_group_name")
        or changes.get("name") or plan.entity_id
    )
    if op in _STATUS_VERBS and plan.entity_type in ("campaign", "asset_group"):
        return "Google Ads", f"{plan.entity_type.replace('_', ' ')} {name} {_STATUS_VERBS[op]}"
    if op == "update_campaign":
        parts = _google_parts(plan)
        return ("Google Ads", f"{name}: {', '.join(parts)}") if parts else None
    if op == "update_ai_max_settings":
        return "Google Ads", f"{name}: AI Max settings changed"
    if op == "add_ad_schedule":
        return "Google Ads", f"{name}: ad schedule added"
    if op == "reddit_set_status" and plan.entity_type in ("campaign", "ad_group"):
        verb = _REDDIT_STATUS.get(str(changes.get("target_status") or ""), "status changed")
        return "Reddit Ads", f"{plan.entity_type.replace('_', ' ')} {name} {verb}"
    if op in ("reddit_update_campaign", "reddit_update_ad_group"):
        parts = _reddit_parts(changes)
        return ("Reddit Ads", f"{name}: {', '.join(parts)}") if parts else None
    return None


def suggested_annotation(config: AdLoopConfig, plan: ChangePlan) -> dict | None:
    """A ready GA4 annotation for an applied ad change, or None.

    Only when a GA4 property is set up and the change can move traffic.
    """
    property_id = str(getattr(getattr(config, "ga4", None), "property_id", "") or "").strip()
    if not property_id:
        return None
    headline = _headline(plan)
    if headline is None:
        return None
    platform, what = headline
    short = "Ads" if platform == "Google Ads" else "Reddit"
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "title": _clip(f"{short}: {what}", 60),
        "date": today,
        "description": _clip(f"{platform} change applied with AdLoop: {what} (plan {plan.plan_id}).", 150),
        "property_id": property_id,
        "next_step": (
            "Offer the user to note this change in GA4 so a later jump in the data has its explanation: "
            "draft_ga4_annotation(action='create', title=..., date=..., description=...) with these values. "
            "It is a change like any other and needs the user's approval."
        ),
    }
