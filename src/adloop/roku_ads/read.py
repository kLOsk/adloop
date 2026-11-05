"""Roku Ads read and diagnostic tools.

Every function takes ``config`` first and an ``account_uid`` that falls back
to ``roku_ads.account_uid``. The Ads API (beta) exposes:

- **Structure**: organizations, accounts, permissions, campaigns with their
  ad groups nested (one ad group per campaign today). There is no ad or
  creative listing.
- **Synchronous metrics** (``GET /developer/metrics``): at most 7 days per
  request, ``day`` or ``total`` granularity, no segmentation. Longer windows
  are read in 7-day pieces and added up here. Spend arrives as micros; CPM,
  VCR, CPCV and CPA are computed here from counts, because Roku documents
  no unit for its own rate fields. There are **no clicks** on TV.
- **Asynchronous reports** (``POST /developer/reports``): reach, frequency,
  geography (postal code, metro, state), placement and actions by name;
  13 months of history, about 8 hours of data latency, and up to an hour
  to build, so a report can outlive a tool call.
- **Conversions** ("actions") are events from the Roku pixel or Conversions
  API matched to households that saw an ad: view-through, a 14-day window,
  credited by impression time in metrics and by conversion time in reports.

Response layouts the beta documentation leaves open (the keys inside a
metrics row's ``dimensions``, report CSV headers) are parsed defensively;
``include_raw`` returns the API's first page so a live run can confirm them.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from adloop.roku_ads import client as _client
from adloop.roku_ads.auth import RokuAdsApiError

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

LEVELS = ("account", "campaign", "ad_group", "ad", "creative")
STATUSES = (
    "draft", "scheduled", "creative_needed", "reviewing_creative", "active", "inactive", "ended",
    "deactivated",
)
# Ad groups that run now or will without anyone touching them.
# Delivery statuses. Only "active" serves now; the planned ones are set up
# but wait for a start date or an approved creative. Readiness audits (the
# conversion setup) cover both; "serving" means active only.
_SERVING = {"active"}
_PLANNED = {"scheduled": "scheduled, not started yet",
            "reviewing_creative": "creative in review, serves once one is approved",
            "creative_needed": "no approved creative, cannot serve"}
_IN_PLAY = _SERVING | set(_PLANNED)
# Optimization goal → the action (pixel event) it optimizes for.
GOAL_ACTIONS = {
    "Leads": "lead",
    "Page Views": "page_view",
    "Purchases": "purchase",
    "Sign-ups": "sign_up",
    "Subscriptions": "subscribe",
    "App installs": "app_install",
}
ACTION_NAMES = (
    "achieve_level", "ad_response", "add_payment_info", "add_to_cart", "add_to_wishlist",
    "app_install", "authentication", "complete_registration", "contact", "customize_product",
    "donate", "download", "find_location", "first_video_view", "initiate_checkout", "lead",
    "link_click", "page_view", "place_order", "purchase", "qss", "schedule", "search",
    "send_to_phone", "session_start", "sign_up", "start_trial", "store_view",
    "submit_application", "subscribe", "subscription_cancellation", "subscription_renewal",
    "unlock_achievement", "view_cart", "view_content", "fqss", "lapsed_30_streamer",
    "lapsed_60_streamer", "lapsed_90_streamer", "lapsed_180_streamer", "lapsed_365_streamer",
)
REPORT_METRICS = (
    "actions", "cpa", "cpcv", "cpm", "cpur", "roas", "household_frequency", "household_reach",
    "impressions", "order_value", "spend", "total_unique_actions", "vcr", "video_view_25pct",
    "video_view_50pct", "video_view_75pct", "video_view_100pct",
)
REPORT_DIMENSIONS = (
    "activity_name", "account_name", "account_id", "campaign_id", "campaign_name", "ad_id",
    "ad_group_id", "ad_group_budget", "ad_group_end_time", "ad_group_start_time", "country",
    "creative_id", "creative_name", "date", "metro_market", "month", "placement", "postal_code",
    "state_territory", "week", "year", "hour",
)
# Report metrics that are per activity: they need the activity_name dimension.
_ACTIVITY_METRICS = {"actions", "cpa", "order_value", "total_unique_actions"}
# Report columns that add up across rows (reach and frequency do not).
# Unique actions and reach count households, which overlap across rows, so
# they are never summed.
_ADDITIVE = {"impressions", "spend", "actions", "order_value"}
_NOT_SUMMED = {"total_unique_actions", "household_reach", "household_frequency"}
# Keys Roku documents as required in each audience configuration.
_REQUIRED_CONFIG = {
    "event_audiences": ("event_group_uid", "qualified_event_timeframe_value", "qualified_event_timeframe_unit", "event_name"),
    "file_audiences": ("crm_file_uid", "expiration_value", "expiration_unit"),
}

_BASE_FIELDS = [
    "billed_local_micro_spend", "impressions", "total_video_start", "total_video_one_quarter",
    "total_video_midpoint", "total_video_three_quarter", "total_video_complete",
]
_SYNC_DAYS = 7
_MAX_SYNC_WINDOW = 92
_MAX_REPORT_WINDOW = 396
_UIDS_PER_REQUEST = 100
_METRIC_PAGE = 1000
_ATTRIBUTION_DAYS = 14
_CURRENCY_CODES = {
    "united states dollar": "USD", "us dollar": "USD", "canadian dollar": "CAD", "euro": "EUR",
    "british pound": "GBP", "pound sterling": "GBP", "australian dollar": "AUD",
    "mexican peso": "MXN", "brazilian real": "BRL",
}

_account_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reset_account_cache() -> None:
    _account_cache.clear()


def _attrs(obj: Any) -> dict:
    return (obj.get("attributes") or {}) if isinstance(obj, dict) else {}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").rstrip("%")
    if text in ("", "-", "--", "null", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _check_choice(name: str, value: Any, allowed) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {list(allowed)}, got {value!r}.")


def _currency_code(name: str) -> str | None:
    text = (name or "").strip()
    if re.fullmatch(r"[A-Z]{3}", text):
        return text
    return _CURRENCY_CODES.get(text.lower())


def resolve_account(config: AdLoopConfig, account_uid: str = "") -> str:
    account = str(account_uid or config.roku_ads.account_uid or "").strip()
    if not account:
        from adloop.runtime import default_setting_hint

        raise ValueError(
            "account_uid is required: pass it (see list_roku_accounts) or "
            + default_setting_hint("roku_ads.account_uid", "Settings → Roku Ads")
            + "."
        )
    return account


def account_meta(config: AdLoopConfig, account_uid: str = "", *, fresh: bool = False) -> dict:
    """Name, organization, currency and time zone of an ad account.

    Cached per process for the time zone the date windows need; a hosted
    server never caches, so one tenant cannot read another's entry.
    """
    from adloop.runtime import deployment_mode

    account = resolve_account(config, account_uid)
    cacheable = deployment_mode() != "server"
    if cacheable and not fresh and account in _account_cache:
        return _account_cache[account]
    data = _client.api_get(config, f"accounts/{account}").get("data") or {}
    attrs = _attrs(data)
    meta = {
        "account_uid": str(data.get("uid") or account),
        "name": attrs.get("name") or "",
        "organization_uid": attrs.get("organization_uid") or "",
        "currency": attrs.get("currency") or "",
        "currency_code": _currency_code(attrs.get("currency") or ""),
        "time_zone": attrs.get("timezone") or "UTC",
        "country": attrs.get("country"),
        "website_url": attrs.get("website_url"),
    }
    if cacheable:
        _account_cache[account] = meta
    return meta


def _zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse_date(value: str, *, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}' — use YYYY-MM-DD (e.g. 2026-10-01).") from exc


def report_window(
    date_range_start: str, date_range_end: str, time_zone: str, *, default_days: int, max_days: int,
) -> tuple[date, date]:
    """Account-local days; by default ``default_days`` ending yesterday."""
    today = datetime.now(_zone(time_zone)).date()
    end = _parse_date(date_range_end, default=today - timedelta(days=1))
    start = _parse_date(date_range_start, default=end - timedelta(days=default_days - 1))
    if start > end:
        raise ValueError("date_range_start must not be after date_range_end.")
    if end > today:
        raise ValueError("date_range_end cannot be in the future.")
    if (end - start).days + 1 > max_days:
        raise ValueError(f"The window is limited to {max_days} days here; narrow it.")
    return start, end


def _chunks(start: date, end: date, size: int = _SYNC_DAYS) -> list[tuple[date, date]]:
    out = []
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=size - 1), end)
        out.append((cursor, last))
        cursor = last + timedelta(days=1)
    return out


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _filling_note(end: date, time_zone: str) -> str | None:
    today = datetime.now(_zone(time_zone)).date()
    if (today - end).days < _ATTRIBUTION_DAYS:
        return (
            f"Actions are credited up to {_ATTRIBUTION_DAYS} days after the impression, so counts "
            f"for days after {(today - timedelta(days=_ATTRIBUTION_DAYS)).isoformat()} are still filling in."
        )
    return None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def list_roku_accounts(config: AdLoopConfig) -> dict:
    """Organizations and ad accounts the signed-in app can read, with roles."""
    organizations, _ = _client.api_get_all(config, "organizations")
    accounts, _ = _client.api_get_all(config, "accounts")
    permissions, _ = _client.api_get_all(config, "permissions")
    org_names = {str(o.get("uid")): _attrs(o).get("name") for o in organizations}
    roles: dict[str, list[str]] = {}
    for p in permissions:
        attrs = _attrs(p)
        org_names.setdefault(str(attrs.get("organization_uid")), attrs.get("organization_name"))
        for a in attrs.get("accounts") or []:
            if isinstance(a, dict):
                roles[str(a.get("account_uid"))] = [str(r) for r in a.get("roles") or []]
    rows = []
    for a in accounts:
        attrs = _attrs(a)
        uid = str(a.get("uid"))
        rows.append({
            "account_uid": uid,
            "name": attrs.get("name"),
            "organization_uid": attrs.get("organization_uid"),
            "organization_name": org_names.get(str(attrs.get("organization_uid"))),
            "currency": attrs.get("currency"),
            "currency_code": _currency_code(attrs.get("currency") or ""),
            "time_zone": attrs.get("timezone"),
            "country": attrs.get("country"),
            "website_url": attrs.get("website_url"),
            "roles": roles.get(uid, []),
        })
    return {
        "organizations": [{"organization_uid": uid, "name": name} for uid, name in org_names.items() if uid != "None"],
        "accounts": rows,
        "total": len(rows),
        "note": (
            "Pass account_uid to the other Roku tools, or set roku_ads.account_uid in the config. "
            "Only self-serve Ads Manager accounts are in the API; insertion-order accounts are not."
        ),
    }


# ---------------------------------------------------------------------------
# Campaigns and ad groups
# ---------------------------------------------------------------------------


def load_campaigns(config: AdLoopConfig, account: str, *, include_archived: bool = True) -> list[dict]:
    items, _ = _client.api_get_all(config, "campaigns", {"account_uid": [account], "order": "-updated_at"})
    return [c for c in items if include_archived or not _attrs(c).get("is_archived")]


def ad_groups_of(campaign: dict) -> list[dict]:
    return [g for g in _attrs(campaign).get("ad_groups") or [] if isinstance(g, dict)]


def _location(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    name, kind = item.get("name"), item.get("type")
    return f"{name} ({kind})" if kind else str(name)


def _daypart_summary(dayparts: Any) -> str:
    if not isinstance(dayparts, dict) or not dayparts.get("schedule"):
        return "all hours"
    blocks = [
        f"{str(b.get('day_of_week', ''))[:3].title()} {int(b.get('start_hour') or 0):02d}–{int(b.get('end_hour') or 0):02d}"
        for b in dayparts["schedule"] if isinstance(b, dict)
    ]
    zone = "viewer's" if dayparts.get("timezone_source", "user") == "user" else "account"
    return ", ".join(blocks) + f" ({zone} time)"


def _frequency(caps: Any) -> str | None:
    items = caps if isinstance(caps, list) else [caps] if isinstance(caps, dict) else []
    parts = [
        f"{c.get('max_impressions')} per {c.get('frequency_value')} {c.get('frequency_unit_of_measure') or 'days'}"
        for c in items if isinstance(c, dict) and c.get("max_impressions")
    ]
    return "; ".join(parts) or None


_AUDIENCE_KEYS = ("age", "gender", "demographics", "household_income", "custom_audiences")


def audience_side(targeting: Any, side: str) -> dict[str, list]:
    """One side (included / excluded) of ``audience_targeting`` as one mapping.

    Roku's schema makes each side an array of objects (``[{"age": [...]},
    {"custom_audiences": [...]}]``); the values of every object are joined.
    A plain object is read the same way.
    """
    raw = targeting.get(side) if isinstance(targeting, dict) else None
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    out: dict[str, list] = {key: [] for key in _AUDIENCE_KEYS}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in _AUDIENCE_KEYS:
            values = item.get(key)
            if isinstance(values, list):
                out[key].extend(values)
    return out


def _audience_names(values: list) -> list[str]:
    return [str(a.get("name") or a.get("uid")) for a in values if isinstance(a, dict)]


def in_play(campaign: dict, group: dict | None = None) -> bool:
    """Serving or about to (scheduled, waiting for a creative): what readiness audits cover."""
    attrs = _attrs(campaign)
    return (not attrs.get("is_archived") and attrs.get("is_enabled") is not False
            and attrs.get("status") in _IN_PLAY and (group is None or group.get("status") in _IN_PLAY))


def effective_state(campaign: dict, group: dict | None = None) -> tuple[bool, str]:
    """Whether a campaign (and its ad group) serves now, and why not.

    A paused, ended or deactivated parent overrides an active child; so do
    the campaign's ``is_enabled`` false and ``is_archived`` flags. A
    scheduled one, or one waiting for an approved creative, does not serve yet.
    """
    attrs = _attrs(campaign)
    if attrs.get("is_archived"):
        return False, "campaign archived"
    if attrs.get("is_enabled") is False:
        return False, "campaign switched off"
    if attrs.get("status") not in _IN_PLAY:
        return False, f"campaign {attrs.get('status')}"
    if group is not None and group.get("status") not in _IN_PLAY:
        return False, f"ad group {group.get('status')}"
    if attrs.get("status") in _PLANNED:
        return False, f"campaign {_PLANNED[attrs['status']]}"
    if group is not None and group.get("status") in _PLANNED:
        return False, f"ad group {_PLANNED[group['status']]}"
    return True, "active"


def ad_group_summary(group: dict) -> dict:
    locations = group.get("location_targeting") or {}
    audiences = group.get("audience_targeting") or {}
    included = audience_side(audiences, "included")
    excluded = audience_side(audiences, "excluded")
    content = group.get("content") or {}
    bid = group.get("bid_strategy") or {}
    return {
        "ad_group_uid": group.get("uid"),
        "status": group.get("status"),
        "budget": group.get("budget"),
        "bid_strategy": bid.get("type"),
        "max_cpm": bid.get("value") if bid.get("type") == "max_cpm" else None,
        "start_at": group.get("start_at"),
        "end_at": group.get("end_at"),
        "schedule": _daypart_summary(group.get("dayparts")),
        "locations_included": [_location(x) for x in locations.get("included") or []],
        "locations_excluded": [_location(x) for x in locations.get("excluded") or []],
        "audiences": {
            "age": included["age"],
            "gender": included["gender"],
            "demographics": included["demographics"],
            "household_income": included["household_income"],
            "custom_included": _audience_names(included["custom_audiences"]),
            "custom_excluded": _audience_names(excluded["custom_audiences"]),
        },
        "placement": group.get("placement"),
        "frequency_cap": _frequency(group.get("impression_frequency_caps")),
        "exclude_mature_content": content.get("exclude_mature_content"),
        "genres": content.get("genres"),
        "optimization_goal": group.get("optimization_goal"),
        "event_group_uid": group.get("event_group_uid"),
    }


def campaign_summary(campaign: dict) -> dict:
    attrs = _attrs(campaign)
    return {
        "campaign_uid": campaign.get("uid"),
        "name": attrs.get("name"),
        "status": attrs.get("status"),
        "is_enabled": attrs.get("is_enabled"),
        "is_archived": bool(attrs.get("is_archived")),
        "objective": attrs.get("objective"),
        "special_advertising_categories": attrs.get("special_advertising_categories") or [],
        "updated_at": attrs.get("updated_at"),
        "ad_groups": [
            {**ad_group_summary(g), "serving": effective_state(campaign, g)[0],
             "serving_note": effective_state(campaign, g)[1]}
            for g in ad_groups_of(campaign)
        ],
    }


def get_roku_campaigns(config: AdLoopConfig, *, account_uid: str = "", include_archived: bool = False) -> dict:
    """Campaigns with their ad groups: status, budget, bid, flight, schedule, targeting, goal."""
    meta = account_meta(config, account_uid)
    campaigns = load_campaigns(config, meta["account_uid"], include_archived=include_archived)
    rows = [campaign_summary(c) for c in campaigns]
    now = datetime.now(timezone.utc)

    insights: list[str] = []
    for c in rows:
        name = c["name"]
        if c["status"] == "deactivated":
            insights.append(
                f"Campaign '{name}' is deactivated by Roku (an overdue invoice or a card problem, "
                "for example): nothing serves until billing is fixed in Ads Manager."
            )
        if c["is_enabled"] is False and c["status"] in _IN_PLAY:
            insights.append(f"Campaign '{name}' is switched off (is_enabled false) while its status reads {c['status']}.")
        for g in c["ad_groups"]:
            # Setup warnings matter before an ad group starts too: check every
            # one that serves or is about to (scheduled, waiting for or
            # reviewing a creative), not only those serving now.
            live = (not c["is_archived"] and c["is_enabled"] is not False
                    and c["status"] in _IN_PLAY and g["status"] in _IN_PLAY)
            serves = "serves" if g["serving"] else "will serve"
            if g["status"] == "creative_needed":
                insights.append(f"Campaign '{name}': its ad group has no approved creative, so it cannot serve.")
            elif g["status"] == "reviewing_creative":
                insights.append(f"Campaign '{name}': creatives are in review (it can serve once one is approved).")
            elif g["status"] == "deactivated" and c["status"] != "deactivated":
                insights.append(f"Campaign '{name}': its ad group is deactivated (billing or account issue).")
            if not live:
                continue
            end = _parse_time(g["end_at"])
            if end and end < now:
                insights.append(f"Campaign '{name}': the ad group's end date {g['end_at']} has passed.")
            if not g["locations_included"]:
                insights.append(f"Campaign '{name}' targets no location, so it {serves} wherever Roku sells.")
            goal = g["optimization_goal"]
            if goal in GOAL_ACTIONS and not g["event_group_uid"]:
                insights.append(
                    f"Campaign '{name}' optimizes for {goal} without an event group, so Roku has no "
                    "pixel events to optimize or report on."
                )
            if c["objective"] == "Conversion" and goal in (None, "Reach"):
                insights.append(f"Campaign '{name}' has the Conversion objective but its ad group optimizes for {goal or 'nothing'}.")
            if not g["frequency_cap"]:
                insights.append(f"Campaign '{name}' has no frequency cap: one household can see the spot without limit.")
    return {
        "account_uid": meta["account_uid"],
        "account_name": meta["name"],
        "currency": meta["currency_code"] or meta["currency"],
        "time_zone": meta["time_zone"],
        "campaigns": rows,
        "total_campaigns": len(rows),
        "insights": insights,
        "note": (
            "Budgets and max CPM are shown as Roku returns them, in the account currency (Roku does "
            "not document the unit). Each campaign has one ad group today; targeting, budget, bid and "
            "flight dates live on it. Status values: draft, scheduled, creative_needed, "
            "reviewing_creative, active, inactive, ended, deactivated."
        ),
    }


# ---------------------------------------------------------------------------
# Synchronous metrics
# ---------------------------------------------------------------------------


_UID_KEYS = ("{entity}_uid", "entity_uid", "uid", "{entity}_id", "entity_id", "id")
_DATE_KEYS = ("date", "dates", "day", "days", "time", "timestamp", "timestamps")


_AMBIGUOUS = object()


def _row_identity(row: dict, entity: str):
    """The uid a metrics row names: a uid, None when it names none, or
    ``_AMBIGUOUS`` when it names several entities (then it belongs to none)."""
    dims = row.get("dimensions") or {}
    for pattern in _UID_KEYS:
        value = dims.get(pattern.format(entity=entity))
        if isinstance(value, list):
            distinct = {str(v) for v in value if v not in (None, "")}
            if len(distinct) > 1:
                return _AMBIGUOUS
            value = next(iter(distinct), None)
        if value not in (None, ""):
            return str(value)
    return None


def _row_uid(row: dict, entity: str) -> str | None:
    """The single uid a metrics row names, if it names exactly one."""
    identity = _row_identity(row, entity)
    return None if identity is _AMBIGUOUS else identity


class MetricsLayoutError(RokuAdsApiError):
    """A metrics response whose layout cannot be read without guessing."""


def _known_sum(values: list) -> float | None:
    numbers = [_num(v) for v in values]
    return None if any(n is None for n in numbers) else float(sum(numbers))


def _day_key(value: Any) -> str:
    """An ISO day from a date or datetime value; anything else is not guessed at."""
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise MetricsLayoutError(
            f"Roku returned a metrics date '{text}' that is not an ISO date; include_raw=true shows the response."
        ) from None


def _check_days(days_: list[str], start: date, end: date) -> None:
    if len(set(days_)) != len(days_):
        raise MetricsLayoutError("Roku returned the same date twice in one metrics row; include_raw=true shows it.")
    outside = [d for d in days_ if not start.isoformat() <= d <= end.isoformat()]
    if outside:
        raise MetricsLayoutError(
            f"Roku returned metrics for {outside[:3]}, outside the requested {start}–{end}; "
            "include_raw=true shows the response."
        )


def _series(
    row: dict, entity: str, start: date, end: date, granularity: str, uid: str, requested: list[str],
) -> list[tuple]:
    """(entity uid, date or None, {field: number or None}) slices of one metrics row.

    A value Roku did not return stays None (unknown), never 0. Daily values
    keyed by date are matched by their keys; arrays are matched to the dates
    the row names, or, when it names none, to the requested days in order
    (only when there is exactly one value per day). Dates must be ISO days,
    unique and inside the window; anything contradictory raises instead of
    guessing.
    """
    dims = row.get("dimensions") or {}
    fields = row.get("fields") or {}
    row_dates: list[str] | None = None
    for key in _DATE_KEYS:
        value = dims.get(key)
        if isinstance(value, list):
            row_dates = [_day_key(v) for v in value]
            break
        if isinstance(value, str) and value:
            row_dates = [_day_key(value)]
            break
    keyed: dict[str, dict[str, Any]] = {}
    arrays: dict[str, list | None] = {}
    for name, value in fields.items():
        if isinstance(value, dict):
            by_day: dict[str, Any] = {}
            for k, v in value.items():
                day = _day_key(k)
                if day in by_day:
                    raise MetricsLayoutError(f"Roku returned two values for {day} in '{name}'; include_raw=true shows them.")
                by_day[day] = v
            keyed[name] = by_day
        elif isinstance(value, list):
            arrays[name] = value
        else:
            arrays[name] = [value]
    for name in requested:
        if name not in keyed:
            arrays.setdefault(name, None)
    days = (end - start).days + 1
    # The row's own dates, as sent (before any set or position map hides a
    # duplicate), and every date a keyed field names, must be real days in
    # the window, totals included.
    if row_dates is not None:
        _check_days(row_dates, start, end)
    for d in keyed.values():
        _check_days(list(d), start, end)
    if granularity == "total":
        lengths = {len(a) for a in arrays.values() if a is not None} | {len(d) for d in keyed.values() if d}
        if lengths and not (lengths <= {1} or lengths == {days}):
            raise MetricsLayoutError(
                f"Roku returned value arrays of lengths {sorted(lengths)} for a total over {days} day(s); "
                "the layout is ambiguous. include_raw=true shows the response."
            )
        totals = {n: None if a is None else _known_sum(a) for n, a in arrays.items()}
        # An empty date map measured nothing: unknown, not zero.
        totals.update({n: _known_sum(list(d.values())) if d else None for n, d in keyed.items()})
        return [(uid, None, totals)]

    present = {n: a for n, a in arrays.items() if a is not None}
    lengths = {len(a) for a in present.values()}
    if len(lengths) > 1:
        raise MetricsLayoutError(f"Roku's daily arrays disagree in length ({sorted(lengths)}); include_raw=true shows them.")
    if keyed:
        if any(not d for d in keyed.values()):
            raise MetricsLayoutError(
                "Roku returned an empty date map for a metric, so it is unknown which days it covers; "
                "include_raw=true shows the response."
            )
        key_sets = {frozenset(d) for d in keyed.values()}
        if len(key_sets) > 1:
            raise MetricsLayoutError("Roku's date-keyed metrics name different days per field; include_raw=true shows them.")
        keys = sorted(next(iter(key_sets)))
        if row_dates is not None and set(row_dates) != set(keys):
            raise MetricsLayoutError("Roku's metric dates contradict the row's dates; include_raw=true shows them.")
        if present and (row_dates is None or lengths != {len(row_dates)}):
            raise MetricsLayoutError(
                "Roku mixed date-keyed values with plain arrays that carry no dates of their own; "
                "include_raw=true shows the response."
            )
        position = {d: i for i, d in enumerate(row_dates or [])}
    else:
        length = next(iter(lengths), 0)
        if row_dates is not None:
            if len(row_dates) != length:
                raise MetricsLayoutError(
                    f"Roku returned {len(row_dates)} date(s) for {length} daily value(s); include_raw=true shows them."
                )
            keys = row_dates
        elif length == days:
            keys = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        else:
            raise MetricsLayoutError(
                f"Roku returned {length} daily value(s) for a {days}-day request without dates to match "
                "them to. Use breakdown='' for totals, or include_raw=true to inspect the response."
            )
        position = {d: i for i, d in enumerate(keys)}
    _check_days(list(keys), start, end)
    slices = []
    for day in keys:
        values: dict[str, float | None] = {n: _num(d.get(day)) for n, d in keyed.items()}
        values.update({n: None if a is None else _num(a[position[day]]) for n, a in arrays.items()})
        slices.append((uid, day, values))
    return slices


def _add_values(bucket: dict, values: dict) -> None:
    """Add a slice into a bucket; once a field is unknown it stays unknown."""
    for name, value in values.items():
        if name in bucket and bucket[name] is None:
            continue
        bucket[name] = None if value is None else (bucket.get(name) or 0.0) + value


def _metrics_pages(
    config: AdLoopConfig, account: str, *, entity: str, uids: list[str], start: date, end: date,
    granularity: str, fields: list[str], attributed_events_by: str, event_group_uids: list[str] | None = None,
) -> tuple[list[dict], list[dict], list[str], dict]:
    """All pages of one metrics request: (rows, default optimizations, warnings, first page)."""
    params: dict[str, Any] = {
        "account_uid": account,
        "entity": entity,
        "entity_uids": uids,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "granularity": granularity,
        "fields": fields,
        "limit": _METRIC_PAGE,
    }
    if any(f.startswith(("action_", "cpa_")) for f in fields):
        params["attributed_events_by"] = attributed_events_by
    if event_group_uids:
        params["event_group_uids"] = event_group_uids
    rows: list[dict] = []
    optimizations: list[dict] = []
    warnings: list[str] = []
    first: dict = {}
    for _ in range(50):
        payload = _client.api_get(config, "metrics", params)
        first = first or payload
        rows.extend(r for r in payload.get("data") or [] if isinstance(r, dict))
        optimizations.extend(
            o for o in (payload.get("included") or {}).get("default_optimizations") or [] if isinstance(o, dict)
        )
        meta = payload.get("meta") or {}
        warnings.extend(str(w) for w in meta.get("warnings") or [] if w)
        page = meta.get("pagination") or {}
        if not page.get("has_more") or not page.get("next_cursor"):
            return rows, optimizations, warnings, first
        params = {"account_uid": account, "cursor": page["next_cursor"], "limit": _METRIC_PAGE}
    raise RokuAdsApiError("The metrics response did not end after 50 pages; narrow the request.")


def collect_metrics(
    config: AdLoopConfig, account: str, *, entity: str, uids: list[str], start: date, end: date,
    granularity: str, actions: list[str], attributed_events_by: str = "time_of_impression",
) -> dict:
    """Metrics for any window: 7-day pieces, uid batches, added up per entity (and day).

    Every row must be attributable to one requested uid: a batch whose rows
    do not say which entity they are about is re-read one uid at a time, and
    a row about an entity that was not requested raises. If Roku refuses
    the action fields part-way, action and order values already read are
    dropped too, so no total covers only part of the window.
    """
    fields = list(_BASE_FIELDS) + [f"action_{a}" for a in actions]
    if "purchase" in actions:
        fields.append("order_value_purchase")
    actions_available = True
    totals: dict[tuple, dict[str, float | None]] = {}
    optimizations: dict[str, dict] = {}
    warnings: list[str] = []
    notes: list[str] = []
    first_page: dict = {}
    for piece_start, piece_end in _chunks(start, end):
        for i in range(0, len(uids), _UIDS_PER_REQUEST):
            batch = uids[i : i + _UIDS_PER_REQUEST]
            try:
                rows, opts, warns, first = _metrics_pages(
                    config, account, entity=entity, uids=batch, start=piece_start, end=piece_end,
                    granularity=granularity, fields=fields, attributed_events_by=attributed_events_by,
                )
            except RokuAdsApiError as exc:
                if exc.status not in (400, 422) or fields == _BASE_FIELDS:
                    raise
                # Action fields can be refused (no event group yet, an event the
                # account never sent): keep delivery numbers and say so.
                notes.append(
                    f"Roku refused the action fields ({exc}); actions and order values are not "
                    "available for this window, only delivery numbers."
                )
                fields = list(_BASE_FIELDS)
                actions_available = False
                for bucket in totals.values():
                    for key in [k for k in bucket if k.startswith(("action_", "cpa_", "order_value"))]:
                        del bucket[key]
                rows, opts, warns, first = _metrics_pages(
                    config, account, entity=entity, uids=batch, start=piece_start, end=piece_end,
                    granularity=granularity, fields=fields, attributed_events_by=attributed_events_by,
                )
            wanted = set(batch)
            if len(batch) > 1 and any(_row_identity(r, entity) not in wanted for r in rows):
                # The rows do not say which entity they are about: ask one
                # uid at a time rather than merge entities into one row.
                rows, opts, warns = [], list(opts), list(warns)
                for uid in batch:
                    one, one_opts, one_warns, _ = _metrics_pages(
                        config, account, entity=entity, uids=[uid], start=piece_start, end=piece_end,
                        granularity=granularity, fields=fields, attributed_events_by=attributed_events_by,
                    )
                    rows.extend({**r, "_uid": uid} for r in one)
                    opts.extend(one_opts)
                    warns.extend(one_warns)
            first_page = first_page or first
            warnings.extend(w for w in warns if w not in warnings)
            for o in opts:
                optimizations[str(o.get("ad_group_uid"))] = o
            for row in rows:
                # A single-uid request (or a re-read of one uid) is about that uid
                # only when the row names it or names nothing; a row naming
                # another entity, or several, contradicts the request.
                requested = row.get("_uid") or (batch[0] if len(batch) == 1 else None)
                reported = _row_identity(row, entity)
                if reported is _AMBIGUOUS:
                    raise MetricsLayoutError(
                        "Roku returned a metrics row naming several entities for one requested uid; "
                        "include_raw=true shows the response."
                    )
                if requested and reported not in (None, requested):
                    raise MetricsLayoutError(
                        f"Roku returned a metrics row about '{reported}' for a request about '{requested}'; "
                        "include_raw=true shows the response."
                    )
                uid = requested or reported
                if uid not in wanted:
                    raise MetricsLayoutError(
                        f"Roku returned a metrics row about '{uid}', which was not requested ({sorted(wanted)}); "
                        "include_raw=true shows the response."
                    )
                for slice_uid, day, values in _series(row, entity, piece_start, piece_end, granularity, uid, fields):
                    _add_values(totals.setdefault((slice_uid, day), {}), values)
    unknown = sorted({k for bucket in totals.values() for k, v in bucket.items() if v is None})
    if unknown:
        notes.append(f"Roku returned no value for {unknown} in part of the window; those totals are unknown (null), not zero.")
    return {
        "values": totals, "actions": actions, "actions_available": actions_available,
        "default_optimizations": optimizations, "warnings": warnings, "notes": notes, "first_page": first_page,
    }


def metric_row(values: dict[str, float | None], actions: list[str], *, actions_available: bool = True) -> dict:
    """Output metrics; rates come from unrounded spend, unknown inputs give null."""
    def get(name: str) -> float | None:
        if name in values:
            return values[name]
        return 0.0 if not values else None  # no slice at all means no delivery

    micros = get("billed_local_micro_spend")
    spend = None if micros is None else micros / 1_000_000
    impressions = get("impressions")
    complete = get("total_video_complete")

    def ratio(a: float | None, b: float | None, scale: float = 1.0, digits: int = 4) -> float | None:
        return round(a / b * scale, digits) if a is not None and b else None

    def whole(v: float | None) -> int | None:
        return None if v is None else int(round(v))

    out: dict[str, Any] = {
        "impressions": whole(impressions),
        "spend": None if spend is None else round(spend, 2),
        "cpm": ratio(spend, impressions, 1000, 2),
        "video_starts": whole(get("total_video_start")),
        "video_completes": whole(complete),
        "vcr": ratio(complete, impressions),
        "cpcv": ratio(spend, complete),
        "video_quartiles": {
            "25": whole(get("total_video_one_quarter")),
            "50": whole(get("total_video_midpoint")),
            "75": whole(get("total_video_three_quarter")),
            "100": whole(complete),
        },
    }
    if actions:
        if not actions_available:
            out["actions"] = None
            out["cpa"] = None
        else:
            counts = {a: (None if get(f"action_{a}") is None else round(get(f"action_{a}"), 2)) for a in actions}
            out["actions"] = counts
            out["cpa"] = {a: ratio(spend, n, 1, 2) for a, n in counts.items() if n}
            defaults = {a: values[f"action_{a}_default"] for a in actions if values.get(f"action_{a}_default") is not None}
            if defaults:
                out["actions_default_optimization"] = {a: round(v, 2) for a, v in defaults.items()}
        if "order_value_purchase" in values and actions_available:
            out["order_value_purchase"] = values["order_value_purchase"]
    return out


def _goal_actions(groups: list[dict]) -> list[str]:
    return sorted({GOAL_ACTIONS[g.get("optimization_goal")] for g in groups if g.get("optimization_goal") in GOAL_ACTIONS})


def _trim_raw(payload: dict) -> dict:
    if not payload:
        return {}
    out = dict(payload)
    data = out.get("data")
    if isinstance(data, list) and len(data) > 3:
        out["data"] = data[:3]
        out["data_rows_omitted"] = len(data) - 3
    return out


def get_roku_performance(
    config: AdLoopConfig,
    *,
    account_uid: str = "",
    level: str = "campaign",
    date_range_start: str = "",
    date_range_end: str = "",
    breakdown: str = "",
    campaign_uid: str = "",
    entity_uids: list[str] | str | None = None,
    actions: list[str] | str | None = None,
    attributed_events_by: str = "time_of_impression",
    include_raw: bool = False,
) -> dict:
    """Spend, impressions, CPM, video completion and actions per level (synchronous metrics)."""
    level = (level or "campaign").strip().lower()
    breakdown = (breakdown or "").strip().lower()
    _check_choice("level", level, LEVELS)
    _check_choice("breakdown", breakdown, ("", "date"))
    _check_choice("attributed_events_by", attributed_events_by, ("time_of_impression", "time_of_conversion"))
    campaign_uid = (campaign_uid or "").strip()
    if level == "account" and campaign_uid:
        raise ValueError("campaign_uid narrows campaign and ad_group levels; the account level is the whole account.")
    requested_actions = [a.removeprefix("action_") for a in _list(actions)]
    unknown = [a for a in requested_actions if a not in ACTION_NAMES]
    if unknown:
        raise ValueError(f"Unknown action(s) {unknown}; Roku's actions are {list(ACTION_NAMES)}.")

    meta = account_meta(config, account_uid)
    account = meta["account_uid"]
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"],
                               default_days=_SYNC_DAYS, max_days=_MAX_SYNC_WINDOW)
    campaigns = load_campaigns(config, account)
    in_scope = [c for c in campaigns if not campaign_uid or str(c.get("uid")) == campaign_uid]
    if campaign_uid and not in_scope:
        raise ValueError(f"Campaign '{campaign_uid}' is not in account {account} (see get_roku_campaigns).")
    group_index = {str(g.get("uid")): (g, c) for c in campaigns for g in ad_groups_of(c)}
    campaign_index = {str(c.get("uid")): c for c in campaigns}

    explicit = _list(entity_uids)
    if explicit and level in ("campaign", "ad_group"):
        allowed = ({str(c.get("uid")) for c in in_scope} if level == "campaign"
                   else {str(g.get("uid")) for c in in_scope for g in ad_groups_of(c)})
        outside = [u for u in explicit if u not in allowed]
        if outside:
            where = f"campaign '{campaign_uid}'" if campaign_uid else f"account {account}"
            raise ValueError(f"{level.replace('_', ' ')}(s) {outside} are not in {where}.")
    if explicit and level in ("ad", "creative") and campaign_uid:
        raise ValueError(
            "campaign_uid cannot narrow ads or creatives (Roku lists neither); pass only their entity_uids."
        )
    if explicit:
        uids = explicit
    elif level == "account":
        uids = [account]
    elif level == "campaign":
        uids = [str(c.get("uid")) for c in in_scope]
    elif level == "ad_group":
        uids = [str(g.get("uid")) for c in in_scope for g in ad_groups_of(c)]
    else:
        raise ValueError(
            "Roku's API lists no ads or creatives: pass entity_uids (ad or creative ids, e.g. from "
            "run_roku_report with the ad_id or creative_id dimension)."
        )
    scope_groups = [g for c in in_scope for g in ad_groups_of(c)]
    chosen_actions = requested_actions or _goal_actions(scope_groups)

    result: dict[str, Any] = {
        "account_uid": account,
        "level": level,
        "breakdown": breakdown or None,
        "date_range": {"start": start.isoformat(), "end": end.isoformat(), "time_zone": meta["time_zone"]},
        "currency": meta["currency_code"] or meta["currency"],
    }
    if not uids:
        result.update(rows=[], totals=metric_row({}, []), insights=["The account has no campaigns."])
        return result

    granularity = "day" if breakdown == "date" else "total"
    data = collect_metrics(config, account, entity=level, uids=uids, start=start, end=end,
                           granularity=granularity, actions=chosen_actions,
                           attributed_events_by=attributed_events_by)
    used_actions = data["actions"]

    rows = []
    grand: dict[str, float | None] = {}
    available = data["actions_available"]
    for (uid, day), values in sorted(data["values"].items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        _add_values(grand, values)
        row: dict[str, Any] = {f"{level}_uid": uid}
        if day:
            row["date"] = day
        if level == "account":
            row["name"] = meta["name"]
        elif level == "campaign" and uid in campaign_index:
            attrs = _attrs(campaign_index[uid])
            groups = ad_groups_of(campaign_index[uid])
            serving, why = effective_state(campaign_index[uid])
            row.update(name=attrs.get("name"), status=attrs.get("status"), is_enabled=attrs.get("is_enabled"),
                       is_archived=bool(attrs.get("is_archived")), serving=serving, serving_note=why,
                       objective=attrs.get("objective"),
                       optimization_goals=sorted({str(g.get("optimization_goal")) for g in groups if g.get("optimization_goal")}))
        elif level == "ad_group" and uid in group_index:
            group, campaign = group_index[uid]
            serving, why = effective_state(campaign, group)
            row.update(campaign_uid=campaign.get("uid"), campaign_name=_attrs(campaign).get("name"),
                       campaign_status=_attrs(campaign).get("status"),
                       campaign_is_enabled=_attrs(campaign).get("is_enabled"),
                       campaign_is_archived=bool(_attrs(campaign).get("is_archived")),
                       status=group.get("status"), serving=serving, serving_note=why,
                       optimization_goal=group.get("optimization_goal"),
                       event_group_uid=group.get("event_group_uid"))
        row.update(metric_row(values, used_actions, actions_available=available))
        rows.append(row)
    if not breakdown:
        rows.sort(key=lambda r: r["spend"] or 0, reverse=True)

    insights: list[str] = list(data["notes"])
    insights.extend(f"Roku: {w}" for w in data["warnings"])
    filling = _filling_note(end, meta["time_zone"])
    if filling and used_actions:
        insights.append(filling)
    paused_parent = [r for r in rows if r.get("serving") is False and (r.get("spend") or 0) > 0]
    if paused_parent and not breakdown:
        insights.append(
            f"{len(paused_parent)} row(s) spent in this window but cannot serve now: "
            + ", ".join(f"{r.get('name') or r.get('campaign_name')} ({r['serving_note']})" for r in paused_parent[:5]) + "."
        )
    if not used_actions:
        insights.append(
            "No action was requested: no ad group in scope optimizes for a pixel event. Pass "
            "actions=[...] (e.g. lead, page_view) to read pixel events."
        )
    elif not available:
        insights.append("Actions could not be read for this window, so no conclusion about conversions is possible.")
    elif not breakdown:
        def measured(r: dict) -> list[str]:
            """The requested actions a row is judged on: its goal when that was requested."""
            goal = GOAL_ACTIONS.get(r.get("optimization_goal") or "") if level == "ad_group" else None
            return [goal] if goal in used_actions else list(used_actions)

        unrequested = sorted({
            GOAL_ACTIONS[r["optimization_goal"]] for r in rows
            if level == "ad_group" and r.get("optimization_goal") in GOAL_ACTIONS
            and GOAL_ACTIONS[r["optimization_goal"]] not in used_actions
        })
        if unrequested:
            insights.append(f"The ad groups optimize for {unrequested}, which was not requested; add it to actions to judge them on their goal.")
        silent = [
            r for r in rows
            if (r["spend"] or 0) > 0 and r["actions"] is not None
            and all(r["actions"].get(a) == 0 for a in measured(r))
        ]
        if silent:
            judged = sorted({a for r in silent for a in measured(r)})
            insights.append(
                f"{len(silent)} row(s) spent {round(sum(r['spend'] for r in silent), 2)} with no "
                f"attributed {'/'.join(judged)}. TV actions are view-through: the household has "
                "to act later on a device the pixel can match; calls to a number on screen never reach "
                "Roku. Run get_roku_conversion_setup before judging the spend."
            )
    if not rows:
        insights.append("No delivery in this window.")

    result.update(
        totals=metric_row(grand, used_actions, actions_available=available),
        rows=rows,
        total_rows=len(rows),
        actions_requested=used_actions,
        attribution={
            "credited_by": attributed_events_by,
            "window_days": _ATTRIBUTION_DAYS,
            "default_optimizations": list(data["default_optimizations"].values()),
        },
        insights=insights,
        note=(
            "TV has no clicks: Roku reports impressions, video completion (VCR = completions / "
            "impressions) and actions, which are pixel or Conversions API events from households "
            "that saw the ad. Spend comes from billed_local_micro_spend; CPM, VCR, CPCV and CPA are "
            "computed from the counts."
        ),
    )
    if include_raw:
        result["raw_first_page"] = _trim_raw(data["first_page"])
    return result


# ---------------------------------------------------------------------------
# Asynchronous reports
# ---------------------------------------------------------------------------


def _norm(column: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(column).lower()).strip("_")


def _iso_local(day: date, time_zone: str, *, end_of_day: bool) -> str:
    moment = datetime(day.year, day.month, day.day, 23 if end_of_day else 0, 59 if end_of_day else 0,
                      59 if end_of_day else 0, tzinfo=_zone(time_zone))
    return moment.isoformat()


def summarize_report(rows: list[dict], *, campaign_uid: str = "", max_rows: int = 200) -> dict:
    notes: list[str] = []
    if campaign_uid:
        column = next((c for c in (rows[0] if rows else {}) if _norm(c) in ("campaign_id", "campaign_uid")), None)
        if column:
            rows = [r for r in rows if str(r.get(column)) == campaign_uid]
        elif rows:
            notes.append("The file has no campaign id column, so rows could not be narrowed to the campaign.")
    columns = list(rows[0]) if rows else []
    totals: dict[str, float | None] = {}
    for column in columns:
        name = _norm(column)
        if name in _ADDITIVE or name.startswith("video_view"):
            values = [_num(r.get(column)) for r in rows]
            unknown = sum(1 for v in values if v is None)
            if unknown:
                # A blank or unreadable cell is not a measured zero.
                totals[column] = None
                notes.append(f"'{column}' is blank or unreadable in {unknown} of {len(rows)} row(s), "
                             "so its total is unknown.")
            else:
                totals[column] = round(sum(values), 2)
    unique = [c for c in columns if _norm(c) in _NOT_SUMMED]
    if unique and len(rows) > 1:
        notes.append(
            f"{unique} count households, which overlap across rows, so they are not added up; for the "
            "total, run the report without dimensions."
        )
    return {
        "columns": columns,
        "row_count": len(rows),
        "rows": rows[: max(int(max_rows or 0), 0)],
        "truncated": len(rows) > max_rows,
        "totals": totals,
        "notes": notes,
    }


def _report_status(config: AdLoopConfig, report_uid: str) -> dict:
    data = _client.api_get(config, f"reports/{report_uid}/status").get("data") or {}
    return _attrs(data)


def _report_result(config: AdLoopConfig, report_uid: str, status: dict, *, campaign_uid: str, max_rows: int) -> dict:
    state = str(status.get("status") or "").lower()
    out: dict[str, Any] = {"report_uid": report_uid, "status": state or "unknown"}
    if state == "done":
        url = status.get("download_url")
        if not url:
            out.update(row_count=0, rows=[], note="Roku built the report but returned no file.")
            return out
        out.update(summarize_report(_client.parse_csv(_client.download(url)),
                                    campaign_uid=campaign_uid, max_rows=max_rows))
    elif state == "error":
        out["note"] = "Roku could not build this report; create it again, perhaps with fewer dimensions."
    else:
        out["note"] = (
            "Roku builds reports asynchronously (it promises about an hour). Call get_roku_report "
            "with this report_uid later to read it."
        )
    return out


def run_roku_report(
    config: AdLoopConfig,
    *,
    account_uid: str = "",
    metrics: list[str] | str | None = None,
    dimensions: list[str] | str | None = None,
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_uid: str = "",
    name: str = "",
    wait_seconds: int = 45,
    max_rows: int = 200,
    sleep=time.sleep,
) -> dict:
    """Create an asynchronous report, wait briefly, and return its rows when ready."""
    metric_list = _list(metrics) or ["impressions", "spend", "household_reach", "household_frequency", "vcr"]
    dimension_list = _list(dimensions)
    bad_metrics = [m for m in metric_list if m not in REPORT_METRICS]
    bad_dims = [d for d in dimension_list if d not in REPORT_DIMENSIONS]
    if bad_metrics:
        raise ValueError(f"Unknown metric(s) {bad_metrics}; Roku reports offer {list(REPORT_METRICS)}.")
    if bad_dims:
        raise ValueError(f"Unknown dimension(s) {bad_dims}; Roku reports offer {list(REPORT_DIMENSIONS)}.")
    notes: list[str] = []
    if set(metric_list) & _ACTIVITY_METRICS and "activity_name" not in dimension_list:
        dimension_list.append("activity_name")
        notes.append("Added the activity_name dimension: actions, cpa, order_value and unique actions are per activity.")
    campaign_uid = (campaign_uid or "").strip()
    if campaign_uid and "campaign_id" not in dimension_list:
        dimension_list.append("campaign_id")

    meta = account_meta(config, account_uid)
    account = meta["account_uid"]
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"],
                               default_days=30, max_days=_MAX_REPORT_WINDOW)
    filters = [{"metric": "account_uid", "value": account}]
    if campaign_uid:
        filters.append({"metric": "campaign_uid", "value": campaign_uid})
    attributes = {
        "name": name or f"AdLoop {start.isoformat()} to {end.isoformat()}",
        "metrics": metric_list,
        "dimensions": dimension_list,
        "filters": filters,
        "start_at": _iso_local(start, meta["time_zone"], end_of_day=False),
        "end_at": _iso_local(end, meta["time_zone"], end_of_day=True),
    }
    created = _client.api_post(config, "reports", {"data": [{"type": "report", "attributes": attributes}]})
    data = created.get("data")
    report = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
    report_uid = str(report.get("uid") or "")
    if not report_uid:
        raise RokuAdsApiError("Roku accepted the report request but returned no report id.")

    status = _attrs(report)
    waited, delay = 0.0, 3.0
    while str(status.get("status") or "").lower() not in ("done", "error") and waited < max(wait_seconds, 0):
        sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 10.0)
        status = _report_status(config, report_uid)
    out = _report_result(config, report_uid, status, campaign_uid=campaign_uid, max_rows=max_rows)
    out.update(
        account_uid=account,
        date_range={"start": start.isoformat(), "end": end.isoformat(), "time_zone": meta["time_zone"]},
        metrics=metric_list,
        dimensions=dimension_list,
    )
    out.setdefault("notes", [])
    out["notes"] = notes + out["notes"] + [
        "Reports credit actions by conversion time with a 14-day window and lag about 8 hours."
    ]
    return out


def get_roku_report(config: AdLoopConfig, *, report_uid: str = "", campaign_uid: str = "", max_rows: int = 200) -> dict:
    """Status of an asynchronous report; its rows once Roku has built it."""
    report_uid = (report_uid or "").strip()
    if not report_uid:
        raise ValueError("report_uid is required (from run_roku_report).")
    status = _report_status(config, report_uid)
    return _report_result(config, report_uid, status, campaign_uid=(campaign_uid or "").strip(), max_rows=max_rows)


# ---------------------------------------------------------------------------
# Conversion setup audit
# ---------------------------------------------------------------------------


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def load_event_groups(config: AdLoopConfig, account: str) -> list[dict]:
    items, _ = _client.api_get_all(config, "event_groups", {"account_uid": [account]})
    return items


def get_roku_conversion_setup(
    config: AdLoopConfig,
    *,
    account_uid: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    min_impressions: int = 5000,
) -> dict:
    """Audit event groups, ad group goals and whether delivery produces the goal actions."""
    meta = account_meta(config, account_uid)
    account = meta["account_uid"]
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"],
                               default_days=_SYNC_DAYS * 2, max_days=_MAX_SYNC_WINDOW)
    event_groups = load_event_groups(config, account)
    groups_by_uid = {str(e.get("uid")): e for e in event_groups}
    campaigns = load_campaigns(config, account, include_archived=False)
    live: list[tuple[dict, dict]] = [
        (g, c) for c in campaigns for g in ad_groups_of(c) if in_play(c, g)
    ]
    checks: list[dict] = []
    insights: list[str] = []
    wants_events = [
        (g, c) for g, c in live
        if g.get("optimization_goal") in GOAL_ACTIONS or _attrs(c).get("objective") == "Conversion"
    ]

    # 1. Event groups (the Roku pixel / Conversions API containers)
    if not event_groups:
        checks.append(_check(
            "event groups", "fail" if wants_events else "info",
            "No event group exists, so Roku receives no site or app events"
            + (" although active or upcoming ad groups optimize for conversions." if wants_events
               else "; awareness campaigns run without one, but no action can be reported."),
        ))
    used_by: dict[str, list[str]] = {}
    for g, c in live:
        if g.get("event_group_uid"):
            used_by.setdefault(str(g["event_group_uid"]), []).append(str(_attrs(c).get("name")))
    for e in event_groups:
        attrs = _attrs(e)
        uid = str(e.get("uid"))
        users = used_by.get(uid, [])
        status = str(attrs.get("status") or ("active" if attrs.get("is_active") else "") or "unknown")
        label = f"event group '{attrs.get('name')}' ({uid})"
        tail = (" Used by " + ", ".join(f"'{n}'" for n in users[:5]) + ".") if users else " No active or upcoming ad group uses it."
        if attrs.get("is_archived"):
            checks.append(_check(label, "fail" if users else "info", "Archived." + tail))
        elif status == "active":
            checks.append(_check(label, "pass", "Roku reports it active." + tail))
        elif status == "stale":
            checks.append(_check(label, "warn", "Roku marks it stale: check that the pixel still fires on the site." + tail))
        elif status == "inactive":
            checks.append(_check(label, "fail" if users else "warn", "Roku reports it inactive." + tail))
        else:
            checks.append(_check(label, "info", f"Status {status}." + tail))

    # 2. Each active or upcoming ad group's goal and event group
    for g, c in live:
        name = _attrs(c).get("name")
        goal = g.get("optimization_goal")
        event_uid = str(g.get("event_group_uid") or "")
        label = f"campaign '{name}': goal"
        if goal in GOAL_ACTIONS and not event_uid:
            checks.append(_check(label, "fail", f"Optimizes for {goal} with no event group: Roku cannot see or optimize toward it."))
        elif event_uid and event_uid not in groups_by_uid:
            checks.append(_check(label, "fail", f"Points at event group {event_uid}, which this account does not have."))
        elif _attrs(c).get("objective") == "Conversion" and goal in (None, "Reach"):
            checks.append(_check(label, "warn", f"Conversion objective, but the ad group optimizes for {goal or 'nothing'}."))
        elif goal in GOAL_ACTIONS:
            checks.append(_check(label, "pass", f"Optimizes for {goal} with event group {event_uid}."))
        else:
            checks.append(_check(label, "info", f"Optimizes for {goal or 'reach'}; actions are still reported when the pixel sends them."))

    # 3. Delivery against the goal action (observations: warn, never fail)
    activity: list[dict] = []
    complete = True
    measured = [(g, c) for g, c in live if g.get("optimization_goal") in GOAL_ACTIONS]
    if measured:
        actions = _goal_actions([g for g, _ in measured])
        data = collect_metrics(config, account, entity="ad_group", uids=[str(g.get("uid")) for g, _ in measured],
                               start=start, end=end, granularity="total", actions=actions)
        insights.extend(data["notes"])
        for g, c in measured:
            values = data["values"].get((str(g.get("uid")), None), {})
            row = metric_row(values, data["actions"], actions_available=data["actions_available"])
            goal_action = GOAL_ACTIONS[g["optimization_goal"]]
            # None when Roku did not return it: unknown, never a false zero.
            count = None if row.get("actions") is None else row["actions"].get(goal_action)
            name = _attrs(c).get("name")
            activity.append({"campaign_name": name, "ad_group_uid": g.get("uid"), "status": g.get("status"),
                             "impressions": row["impressions"], "spend": row["spend"],
                             "goal_action": goal_action, "goal_actions": count,
                             "cpa": (row.get("cpa") or {}).get(goal_action)})
            label = f"campaign '{name}': {goal_action} from impressions"
            if count is None:
                complete = False
                checks.append(_check(
                    label, "warn",
                    f"Roku did not return {goal_action} counts for this window, so whether impressions lead "
                    "to it cannot be checked. The audit is incomplete.",
                ))
                continue
            if count:
                checks.append(_check(label, "pass", f"{row['impressions']} impressions, {count:g} {goal_action} attributed."))
            elif (row["impressions"] or 0) >= min_impressions:
                checks.append(_check(
                    label, "warn",
                    f"{row['impressions']} impressions and no {goal_action} attributed. Either no household "
                    f"acted within {_ATTRIBUTION_DAYS} days, or the event does not reach Roku (pixel missing on "
                    "the form or thank-you page, another event name, consent blocking it). Calls to a number "
                    "shown on screen never reach Roku: count those in call tracking.",
                ))
            if goal_action == "purchase" and data["actions_available"] and not values.get("order_value_purchase"):
                insights.append(f"Campaign '{name}' optimizes for purchases but no order value arrives, so ROAS is unavailable.")
        filling = _filling_note(end, meta["time_zone"])
        if filling:
            insights.append(filling)

    order = {"fail": 0, "warn": 1, "info": 2, "pass": 3}
    checks.sort(key=lambda c: order.get(c["status"], 4))
    verdict = ("broken" if any(c["status"] == "fail" for c in checks)
               else "needs attention" if any(c["status"] == "warn" for c in checks) else "ok")
    insights.append(
        "Roku actions are view-through: a household that saw the spot acts later on a phone or "
        "laptop the pixel can match. GA4 sees those visits as direct, organic or branded search, not "
        "as Roku, so compare lifts in those channels and the call tracker rather than expecting clicks."
    )
    if not complete:
        insights.insert(0, "Part of the audit could not run (actions unavailable); the verdict covers only the checks listed.")
    return {
        "account_uid": account,
        "date_range": {"start": start.isoformat(), "end": end.isoformat(), "time_zone": meta["time_zone"]},
        "verdict": verdict,
        "complete": complete,
        "checks": checks,
        "event_groups": [
            {"event_group_uid": str(e.get("uid")), "name": _attrs(e).get("name"),
             "status": _attrs(e).get("status"), "is_default": _attrs(e).get("is_default"),
             "is_archived": _attrs(e).get("is_archived")}
            for e in event_groups
        ],
        "ad_group_activity": activity,
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Audiences
# ---------------------------------------------------------------------------


def _config_items(audience: dict) -> list[tuple[str, dict]]:
    """(type, attributes) of each audience config item.

    Roku's create schema writes attributes as ``[{"key": ..., "value": ...}]``;
    responses may use that shape or a plain object, so both are read.
    """
    items = []
    for item in _attrs(audience).get("audiences_config") or []:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes")
        if isinstance(attributes, list):
            attributes = {
                str(pair.get("key")): pair.get("value") for pair in attributes
                if isinstance(pair, dict) and "key" in pair
            }
        items.append((str(item.get("type") or ""), attributes if isinstance(attributes, dict) else item))
    return items


def get_roku_audiences(config: AdLoopConfig, *, account_uid: str = "") -> dict:
    """Custom and standard audiences, what they are built from, and which campaigns use them."""
    meta = account_meta(config, account_uid)
    account = meta["account_uid"]
    custom, included = _client.api_get_all(config, "audiences/custom", {"owner_uid": [account]})
    standard, _ = _client.api_get_all(config, "audiences/standard", {"account_uid": [account]})
    event_groups = {str(e.get("uid")): e for e in load_event_groups(config, account)}
    crm_files = {
        str(f.get("uid")): _attrs(f)
        for key, items in included.items() for f in items if f.get("type") == "crm_file" or key == "audiences_config_items"
    }

    usage: dict[str, list[dict]] = {}
    for c in load_campaigns(config, account, include_archived=False):
        for g in ad_groups_of(c):
            targeting = g.get("audience_targeting") or {}
            serving, why = effective_state(c, g)
            for mode in ("included", "excluded"):
                for a in audience_side(targeting, mode)["custom_audiences"]:
                    if isinstance(a, dict):
                        usage.setdefault(str(a.get("uid")), []).append({
                            "campaign_name": _attrs(c).get("name"), "ad_group_status": g.get("status"),
                            "serving": serving, "serving_note": why, "in_play": in_play(c, g), "mode": mode,
                        })

    insights: list[str] = []
    rows = []
    for audience in custom:
        attrs = _attrs(audience)
        uid = str(audience.get("uid"))
        sources = []
        for kind, item in _config_items(audience):
            missing = [k for k in _REQUIRED_CONFIG.get(kind, ()) if k not in item]
            if missing:
                sources.append({"type": kind.replace("_", " "), "incomplete": True, "missing_keys": missing})
                insights.append(f"Audience '{attrs.get('name')}': its {kind.replace('_', ' ')} configuration lacks {missing}.")
                continue
            if kind == "event_audiences":
                eg = event_groups.get(str(item.get("event_group_uid")))
                eg_status = _attrs(eg).get("status") if eg else "missing"
                sources.append({
                    "type": "website or app events", "events": item.get("event_name") or [],
                    "lookback": f"{item.get('qualified_event_timeframe_value')} {item.get('qualified_event_timeframe_unit') or 'day'}s",
                    "event_group": _attrs(eg).get("name") if eg else item.get("event_group_uid"),
                    "event_group_status": eg_status,
                })
                if eg_status in ("missing", "stale", "inactive"):
                    insights.append(f"Audience '{attrs.get('name')}' is built from an event group that is {eg_status}, so it stops filling.")
            elif kind == "file_audiences":
                crm = crm_files.get(str(item.get("crm_file_uid")), {})
                match = crm.get("match_data") or {}
                sources.append({
                    "type": "customer file", "file": crm.get("filename"), "file_status": crm.get("status"),
                    "households_matched": match.get("household_matched_counts"),
                    "expires_after": f"{item.get('expiration_value')} {item.get('expiration_unit') or 'day'}s",
                })
                if crm.get("status") in ("match_invalid", "user_error", "system_error"):
                    insights.append(f"Audience '{attrs.get('name')}': its customer file is {crm.get('status')}.")
            elif kind:
                sources.append({"type": kind.replace("_", " "), "partner": item.get("partner")})
        users = usage.get(uid, [])
        if attrs.get("is_archived") and any(u["in_play"] for u in users):
            insights.append(f"Archived audience '{attrs.get('name')}' is still on an active or upcoming ad group.")
        rows.append({
            "audience_uid": uid, "name": attrs.get("name"), "is_archived": bool(attrs.get("is_archived")),
            "sources": sources, "used_by": users, "updated_at": attrs.get("updated_at"),
        })
    unused = [r for r in rows if not r["used_by"] and not r["is_archived"]]
    if unused:
        insights.append(f"{len(unused)} custom audience(s) are on no ad group.")
    return {
        "account_uid": account,
        "custom_audiences": rows,
        "standard_audiences": [
            {"audience_uid": str(a.get("uid")), "name": _attrs(a).get("name"),
             "is_archived": bool(_attrs(a).get("is_archived"))}
            for a in standard
        ],
        "insights": insights,
        "note": (
            "Roku's API returns no audience size or status. New event audiences stay pending for about "
            "72 hours, and a customer file needs at least 1,000 matched households."
        ),
    }
