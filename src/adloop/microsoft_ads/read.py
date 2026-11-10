"""Microsoft Advertising read and diagnostic tools.

Every function takes ``config`` first and an ``ad_account_id`` that falls
back to ``microsoft_ads.ad_account_id``. Output follows the other ad
platforms: money in account currency, the status of every level of the
hierarchy, ``insights[]`` with computed findings, ``compact`` mode for
audits.

Structure comes from Campaign Management (campaigns, location criteria, ad
groups, keywords, ads, UET tags, conversion goals); numbers come from the
Reporting service, which builds a CSV asynchronously (see
:func:`adloop.microsoft_ads.client.run_report`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads import client as _client
from adloop.microsoft_ads.auth import MicrosoftAdsApiError
from adloop.microsoft_ads.client import campaign_request, customer_request, report_time

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

CAMPAIGN_TYPES = "Search, Shopping, DynamicSearchAds, Audience, PerformanceMax"
AD_TYPES = ["ResponsiveSearch", "ExpandedText", "DynamicSearch", "ResponsiveAd"]
_GOAL_TYPES_WITHOUT_IN_STORE = "Url, Duration, PagesViewedPerVisit, Event, AppInstall, OfflineConversion"
CONVERSION_GOAL_TYPES = _GOAL_TYPES_WITHOUT_IN_STORE + ", InStoreTransaction"
# Asking for InStoreTransaction goals fails the whole query for accounts
# outside Microsoft's in-store pilot, which is almost all of them.
_IN_STORE_PILOT_CODES = {"InStoreTransactionPilotNotEnabledForCustomer", "5359"}

# Bid strategies that optimise for conversions (BiddingScheme Type without its
# "BiddingScheme" suffix). Broad match is only safe under these: Maximize
# Clicks and manual bids buy whatever traffic a loose match brings. TargetCpa
# and TargetRoas are the older stand-alone schemes Microsoft folded into
# MaxConversions and MaxConversionValue; campaigns can still carry them.
CONVERSION_BASED_STRATEGIES = frozenset({"MaxConversions", "MaxConversionValue", "TargetCpa", "TargetRoas", "MaxRoas"})

# Microsoft time zone names → IANA, for "today" in the account's zone. Reports
# take Microsoft's own name (ReportTimeZone) directly. From Microsoft's time
# zone mapping table (advertising/scripts/concepts/timezone-mapping), plus the
# spellings its time zones guide uses for the same zones. Where the two
# disagree on the zone itself, the offset the guide states wins: Midway
# Island and Samoa is UTC-11 (the table's Pacific/Apia is UTC+13), the
# guide's Athens zone UTC+2, International Date Line West UTC-12, and
# Astana/Dhaka and Almaty/Novosibirsk UTC+6. Every value of the API's
# TimeZoneType enum (customer-management-service/timezonetype) is covered.
_TIME_ZONES = {
    "AbuDhabiMuscat": "Asia/Dubai",
    "Adelaide": "Australia/Adelaide",
    "Alaska": "America/Anchorage",
    "AlmatyNovosibirsk": "Asia/Omsk",
    "Almaty_Novosibirsk": "Asia/Omsk",
    "AmsterdamBerlinBernRomeStockholmVienna": "Europe/Berlin",
    "Arizona": "America/Phoenix",
    "AstanaDhaka": "Asia/Dhaka",
    "AthensBuchartestIstanbul": "Europe/Athens",
    "AthensBuckarestIstanbul": "Europe/Athens",
    "AthensIslandanbulMinsk": "Europe/Minsk",
    "AtlanticTimeCanada": "America/Halifax",
    "AucklandWellington": "Pacific/Auckland",
    "Azores": "Atlantic/Azores",
    "Baghdad": "Asia/Baghdad",
    "BakuTbilisiYerevan": "Asia/Baku",
    "BangkokHanoiJakarta": "Asia/Bangkok",
    "BeijingChongqingHongKongUrumqi": "Asia/Shanghai",
    "BelgradeBratislavaBudapestLjubljanaPrague": "Europe/Budapest",
    "BogotaLimaQuito": "America/Bogota",
    "Brasilia": "America/Sao_Paulo",
    "Brisbane": "Australia/Brisbane",
    "BrusselsCopenhagenMadridParis": "Europe/Paris",
    "Bucharest": "Europe/Bucharest",
    "BuenosAiresGeorgetown": "America/Argentina/Buenos_Aires",
    "Cairo": "Africa/Cairo",
    "CanberraMelbourneSydney": "Australia/Sydney",
    "CapeVerdeIsland": "Atlantic/Cape_Verde",
    "CaracasLaPaz": "America/Caracas",
    "CasablancaMonrovia": "Africa/Casablanca",
    "CentralAmerica": "America/Guatemala",
    "CentralTimeUSCanada": "America/Chicago",
    "ChennaiKolkataMumbaiNewDelhi": "Asia/Calcutta",
    "ChihuahuaLaPazMazatlan": "America/Chihuahua",
    "Darwin": "Australia/Darwin",
    "EasternTimeUSCanada": "America/New_York",
    "Ekaterinburg": "Asia/Yekaterinburg",
    "FijiKamchatkaMarshallIsland": "Pacific/Fiji",
    "Greenland": "America/Godthab",
    "GreenwichMeanTimeDublinEdinburghLisbonLondon": "Europe/London",
    "GuadalajaraMexicoCityMonterrey": "America/Mexico_City",
    "GuamPortMoresby": "Pacific/Port_Moresby",
    "HararePretoria": "Africa/Harare",
    "Hawaii": "Pacific/Honolulu",
    "HelsinkiKyivRigaSofiaTallinnVilnius": "Europe/Kyiv",
    "Hobart": "Australia/Hobart",
    "IndianaEast": "America/Indianapolis",
    "InternationalDateLineWest": "Etc/GMT+12",
    "IrkutskUlaanBataar": "Asia/Irkutsk",
    "IslamabadKarachiTashkent": "Asia/Karachi",
    "IslandamabadKarachiTashkent": "Asia/Karachi",
    "Jerusalem": "Asia/Jerusalem",
    "Kabul": "Asia/Kabul",
    "Kathmandu": "Asia/Kathmandu",
    "Krasnoyarsk": "Asia/Krasnoyarsk",
    "KualaLumpurSingapore": "Asia/Singapore",
    "KuwaitRiyadh": "Asia/Riyadh",
    "MagadanSolomonIslandNewCaledonia": "Asia/Magadan",
    "MidAtlantic": "America/Noronha",
    "MidwayIslandAndSamoa": "Pacific/Pago_Pago",
    "MidwayIslandand_Samoa": "Pacific/Pago_Pago",
    "MoscowStPetersburgVolgograd": "Europe/Moscow",
    "MountainTimeUSCanada": "America/Denver",
    "MountainTime_US_Canada": "America/Denver",
    "Nairobi": "Africa/Nairobi",
    "Newfoundland": "America/St_Johns",
    "Nukualofa": "Pacific/Tongatapu",
    "OsakaSapporoTokyo": "Asia/Tokyo",
    "PacificTimeUSCanadaTijuana": "America/Los_Angeles",
    "Perth": "Australia/Perth",
    "Rangoon": "Asia/Yangon",
    "Santiago": "America/Santiago",
    "SarajevoSkopjeWarsawZagreb": "Europe/Warsaw",
    "Saskatchewan": "America/Regina",
    "Seoul": "Asia/Seoul",
    "SolomonIslandNewCaledonia": "Asia/Magadan",
    "SriJayawardenepura": "Asia/Colombo",
    "Taipei": "Asia/Taipei",
    "Tehran": "Asia/Tehran",
    "Vladivostok": "Asia/Vladivostok",
    "WestCentralAfrica": "Africa/Lagos",
    "Yakutsk": "Asia/Yakutsk",
    "Yangon": "Asia/Yangon",
}

_PERFORMANCE_LEVELS = {
    "account": "AccountPerformanceReportRequest",
    "campaign": "CampaignPerformanceReportRequest",
    "ad_group": "AdGroupPerformanceReportRequest",
    "keyword": "KeywordPerformanceReportRequest",
    "ad": "AdPerformanceReportRequest",
}
_LEVEL_COLUMNS = {
    "account": ["AccountId", "AccountName"],
    "campaign": ["CampaignId", "CampaignName", "CampaignStatus", "CampaignType"],
    "ad_group": ["CampaignId", "CampaignName", "CampaignStatus", "AdGroupId", "AdGroupName", "Status"],
    "keyword": [
        "CampaignName", "CampaignStatus", "AdGroupId", "AdGroupName", "AdGroupStatus",
        "KeywordId", "Keyword", "KeywordStatus", "BidMatchType", "QualityScore",
    ],
    "ad": [
        "CampaignName", "CampaignStatus", "AdGroupId", "AdGroupName", "AdGroupStatus",
        "AdId", "AdType", "AdStatus", "FinalUrl",
    ],
}
# Conversions, ConversionRate, CostPerConversion and AllConversions are
# deprecated: keyword and search-query reports return 0 for them. The
# "Qualified" columns are their replacements; rate and CPA are computed here.
_METRIC_COLUMNS = [
    "Impressions", "Clicks", "Spend", "Ctr", "AverageCpc", "ConversionsQualified", "Revenue",
]
# Goals that a UET tag has to report; offline, app-install and in-store
# goals arrive by import or another channel and need no tag.
_TAG_GOAL_TYPES = {"Url", "Duration", "PagesViewedPerVisit", "Event"}
# Impression share, the share lost to budget and to rank (summary rows only).
_SHARE_NAMES = {
    "ImpressionSharePercent": "impression_share_pct",
    "ImpressionLostToBudgetPercent": "lost_to_budget_pct",
    "ImpressionLostToRankAggPercent": "lost_to_rank_pct",
}
_SHARE_COLUMNS = list(_SHARE_NAMES)
_BREAKDOWNS = ("date", "device")

_INTENT_OPTIONS = {
    "PeopleIn": "people in the targeted locations",
    "PeopleInOrSearchingForOrViewingPages": "people in, searching for or viewing pages about the locations",
    "PeopleSearchingForOrViewingPages": "people searching for or viewing pages about the locations",
}

_account_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_account(config: AdLoopConfig, ad_account_id: str = "") -> str:
    account = str(ad_account_id or config.microsoft_ads.ad_account_id or "").strip()
    if not account:
        from adloop.runtime import default_setting_hint

        raise ValueError(
            "ad_account_id is required: pass it (see list_microsoft_ads_accounts) or "
            + default_setting_hint("microsoft_ads.ad_account_id", "Settings → Microsoft Ads")
            + "."
        )
    return account


def account_meta(config: AdLoopConfig, ad_account_id: str = "", *, fresh: bool = False) -> dict:
    """Name, number, currency, time zone, status and manager of an ad account.

    Cached per process for the currency and time zone the reports need; a
    hosted server never caches, so one tenant cannot read another's entry.
    """
    from adloop.runtime import deployment_mode

    account = resolve_account(config, ad_account_id)
    cacheable = deployment_mode() != "server"
    if cacheable and not fresh and account in _account_cache:
        return _account_cache[account]
    data = customer_request(config, "Account/Query", {"AccountId": account}, account_id=account)
    acct = data.get("Account") or {}
    meta = {
        "ad_account_id": str(acct.get("Id") or account),
        "name": acct.get("Name") or "",
        "number": acct.get("Number") or "",
        "currency": acct.get("CurrencyCode") or "",
        "time_zone": acct.get("TimeZone") or "",
        "status": acct.get("AccountLifeCycleStatus") or "",
        "pause_reason": acct.get("PauseReason"),
        "customer_id": str(acct.get("ParentCustomerId") or config.microsoft_ads.customer_id or ""),
    }
    if cacheable:
        _account_cache[account] = meta
    return meta


def _customer(config: AdLoopConfig, meta: dict) -> str:
    return str(config.microsoft_ads.customer_id or meta.get("customer_id") or "")


def _zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(_TIME_ZONES.get(name, "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def zone_note(name: str) -> str | None:
    """A visible note when the account's time zone is not one AdLoop knows."""
    if not name or name in _TIME_ZONES:
        return None
    return (f"The account's time zone '{name}' is not in AdLoop's Microsoft time zone table, so the default "
            "window and 'today' were worked out in UTC; the last day may be off by one. Pass explicit dates.")


def _parse_date(value: str, *, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}' — use YYYY-MM-DD (e.g. 2026-10-01).") from exc


def report_window(date_range_start: str, date_range_end: str, time_zone: str) -> tuple[date, date]:
    """Account-local days; default the 30 days ending yesterday."""
    today = datetime.now(_zone(time_zone)).date()
    end = _parse_date(date_range_end, default=today - timedelta(days=1))
    start = _parse_date(date_range_start, default=end - timedelta(days=29))
    if start > end:
        raise ValueError("date_range_start must not be after date_range_end.")
    if end > today:
        raise ValueError("date_range_end cannot be in the future.")
    return start, end


def _num(value: Any) -> float | None:
    """'1,234.56', '3.70%', '--' → float; percentages stay in percent units."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").rstrip("%")
    if text in ("", "--", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _money(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("Amount")
    number = _num(value)
    return None if number is None else round(number, 2)


def _ms_date(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("Year"):
        return f"{int(value['Year']):04d}-{int(value['Month']):02d}-{int(value['Day']):02d}"
    return None


def _check_choice(name: str, value: Any, allowed) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def list_microsoft_ads_accounts(config: AdLoopConfig) -> dict:
    """The signed-in user and every ad account they can reach."""
    user_payload = customer_request(config, "User/Query", {"UserId": None})
    user = user_payload.get("User") or {}
    roles = user_payload.get("CustomerRoles") or []
    accounts: list[dict] = []
    page = 0
    while page < 20:
        payload = customer_request(
            config,
            "Accounts/Search",
            {
                "Predicates": [{"Field": "UserId", "Operator": "Equals", "Value": str(user.get("Id") or "")}],
                "Ordering": None,
                "PageInfo": {"Index": page, "Size": 100},
            },
        )
        batch = payload.get("Accounts") or []
        accounts.extend(a for a in batch if isinstance(a, dict))
        if len(batch) < 100:
            break
        page += 1
    return {
        "user": {
            "user_id": user.get("Id"),
            "user_name": user.get("UserName"),
            "email": (user.get("ContactInfo") or {}).get("Email"),
        },
        "customers": [
            {"customer_id": r.get("CustomerId"), "role_id": r.get("RoleId"),
             "account_ids": r.get("AccountIds")}
            for r in roles if isinstance(r, dict)
        ],
        "accounts": [
            {
                "ad_account_id": str(a.get("Id")),
                "name": a.get("Name"),
                "number": a.get("Number"),
                "customer_id": str(a.get("ParentCustomerId") or ""),
                "currency": a.get("CurrencyCode"),
                "time_zone": a.get("TimeZone"),
                "status": a.get("AccountLifeCycleStatus"),
                "pause_reason": a.get("PauseReason"),
            }
            for a in accounts
        ],
        "total": len(accounts),
        "note": (
            "Pass ad_account_id to the other Microsoft tools, or set microsoft_ads.ad_account_id "
            "and microsoft_ads.customer_id in the config."
        ),
    }


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


# Campaign fields Microsoft returns only when asked for. Previews and the
# changed-since-the-draft checks depend on the first two: a MaxConversions
# campaign's target CPA, and whether its ad schedule runs in the account's
# or each searcher's time zone.
_CAMPAIGN_FIELDS = "AdScheduleUseSearcherTimeZone, TargetCpaInMaxConversion"


def _campaigns(config: AdLoopConfig, account: str, customer: str, *, with_goals: bool = False) -> list[dict]:
    body: dict[str, Any] = {"AccountId": account, "CampaignType": CAMPAIGN_TYPES,
                            "ReturnAdditionalFields": _CAMPAIGN_FIELDS}
    if with_goals:
        # GoalIds only come back when asked for.
        body["ReturnAdditionalFields"] = f"{_CAMPAIGN_FIELDS}, CampaignConversionGoal"
    payload = campaign_request(
        config, "Campaigns/QueryByAccountId", body, account_id=account, customer_id=customer,
    )
    return [c for c in payload.get("Campaigns") or [] if isinstance(c, dict)]


def _criterions(config: AdLoopConfig, account: str, customer: str, campaign_id: str, kind: str) -> list[dict]:
    payload = campaign_request(
        config, "CampaignCriterions/QueryByIds",
        {"CampaignCriterionIds": None, "CampaignId": campaign_id, "CriterionType": kind},
        account_id=account, customer_id=customer,
    )
    return [c for c in payload.get("CampaignCriterions") or [] if isinstance(c, dict)]


def criterion_kind(c: dict) -> str:
    """'Location', 'Radius', 'DayTime', ...: reads name criteria without the 'Criterion' suffix."""
    kind = str((c.get("Criterion") or {}).get("Type") or "")
    return kind.removesuffix("Criterion")


def is_negative(c: dict) -> bool:
    return str(c.get("Type") or "").startswith("Negative")


TARGET_KINDS = ("Location", "LocationIntent", "Radius", "DayTime")
# One query with the types joined by commas ("Location,LocationIntent,...")
# returns every target criterion of a campaign instead of one call per type.
# QueryByIds refuses the "Targets" shorthand that adds, updates and deletes
# accept (seen live: 4503 CampaignCriterionTypeInvalid, "Invalid API Campaign
# Criterion Type : Targets on API tier"). If Microsoft ever refuses the
# joined form, the per-type queries take over for the rest of the process.
_targets_query = {"refused": False}


def target_criterions(config: AdLoopConfig, account: str, customer: str, campaign_id: str,
                      kinds: tuple[str, ...] = TARGET_KINDS) -> dict[str, list[dict]]:
    """A campaign's target criteria by kind, in one call where Microsoft allows it."""
    if not _targets_query["refused"]:
        try:
            rows = _criterions(config, account, customer, campaign_id, ",".join(kinds))
        except MicrosoftAdsApiError as exc:
            if exc.status != 400:
                raise
            _targets_query["refused"] = True
        else:
            out: dict[str, list[dict]] = {k: [] for k in kinds}
            for row in rows:
                if criterion_kind(row) in out:
                    out[criterion_kind(row)].append(row)
            return out
    return {k: _criterions(config, account, customer, campaign_id, k) for k in kinds}


def _ad_groups(config: AdLoopConfig, account: str, customer: str, campaign_id: str) -> list[dict]:
    payload = campaign_request(
        config, "AdGroups/QueryByCampaignId", {"CampaignId": campaign_id},
        account_id=account, customer_id=customer,
    )
    return [g for g in payload.get("AdGroups") or [] if isinstance(g, dict)]


def _keywords(config: AdLoopConfig, account: str, customer: str, ad_group_id: str) -> list[dict]:
    payload = campaign_request(
        config, "Keywords/QueryByAdGroupId", {"AdGroupId": ad_group_id},
        account_id=account, customer_id=customer,
    )
    return [k for k in payload.get("Keywords") or [] if isinstance(k, dict)]


def _ads(config: AdLoopConfig, account: str, customer: str, ad_group_id: str) -> list[dict]:
    payload = campaign_request(
        config, "Ads/QueryByAdGroupId", {"AdGroupId": ad_group_id, "AdTypes": AD_TYPES},
        account_id=account, customer_id=customer,
    )
    return [a for a in payload.get("Ads") or [] if isinstance(a, dict)]


def _conversion_goals(config: AdLoopConfig, account: str, customer: str) -> list[dict]:
    """Every conversion goal; in-store ones only where the account is in Microsoft's pilot."""
    def query(types: str) -> dict:
        return campaign_request(
            config, "ConversionGoals/QueryByIds", {"ConversionGoalIds": None, "ConversionGoalTypes": types},
            account_id=account, customer_id=customer,
        )

    try:
        payload = query(CONVERSION_GOAL_TYPES)
    except MicrosoftAdsApiError as exc:
        if not set(exc.codes) & _IN_STORE_PILOT_CODES:
            raise
        payload = query(_GOAL_TYPES_WITHOUT_IN_STORE)
    return [g for g in payload.get("ConversionGoals") or [] if isinstance(g, dict)]


def _bidding(scheme: dict | None) -> dict:
    scheme = scheme or {}
    kind = str(scheme.get("Type") or "")
    out: dict[str, Any] = {"strategy": kind.replace("BiddingScheme", "") or None}
    for key, label in (("MaxCpc", "max_cpc"), ("TargetCpa", "target_cpa"), ("TargetRoas", "target_roas")):
        if scheme.get(key) is not None:
            out[label] = _money(scheme.get(key))
    return out


def _location_summary(locations: list[dict], intent: list[dict], radius: list[dict]) -> dict:
    included, excluded = [], []
    for c in locations + radius:
        crit = c.get("Criterion") or {}
        if criterion_kind(c) == "Radius":
            label = f"{crit.get('Name') or 'radius'} ({crit.get('Radius')} {crit.get('RadiusUnit') or ''})".strip()
        else:
            label = crit.get("DisplayName") or str(crit.get("LocationId") or "")
            if crit.get("LocationType"):
                label += f" ({crit.get('LocationType')})"
        (excluded if is_negative(c) else included).append(label)
    option = ""
    for c in intent:
        option = str((c.get("Criterion") or {}).get("IntentOption") or "")
    return {
        "included": included,
        "excluded": excluded,
        "intent": option or None,
        "intent_meaning": _INTENT_OPTIONS.get(option),
    }


def campaign_summary(campaign: dict, targeting: dict | None = None) -> dict:
    budget_id = campaign.get("BudgetId")
    shared = bool(budget_id) and str(budget_id) not in ("0", "None")
    return {
        "campaign_id": str(campaign.get("Id")),
        "name": campaign.get("Name"),
        "status": campaign.get("Status"),
        "campaign_type": campaign.get("CampaignType"),
        "daily_budget": _money(campaign.get("DailyBudget")),
        "budget_type": campaign.get("BudgetType"),
        "shared_budget_id": str(budget_id) if shared else None,
        "bidding": _bidding(campaign.get("BiddingScheme")),
        "languages": campaign.get("Languages") or [],
        "time_zone": campaign.get("TimeZone"),
        "goal_ids": [str(g) for g in campaign.get("GoalIds") or []],
        "tracking_template": campaign.get("TrackingUrlTemplate") or "",
        "final_url_suffix": campaign.get("FinalUrlSuffix") or "",
        "locations": targeting,
    }


def get_microsoft_campaigns(
    config: AdLoopConfig, *, ad_account_id: str = "", include_targeting: bool = True,
) -> dict:
    """Campaigns with budget, bidding, languages and location targeting."""
    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    rows = []
    for c in _campaigns(config, account, customer, with_goals=True):
        targeting = None
        if include_targeting:
            found = target_criterions(config, account, customer, str(c.get("Id")),
                                      ("Location", "LocationIntent", "Radius"))
            targeting = _location_summary(found["Location"], found["LocationIntent"], found["Radius"])
        rows.append(campaign_summary(c, targeting))

    insights: list[str] = []
    if meta["status"] and meta["status"] != "Active":
        insights.append(
            f"The ad account is {meta['status']}"
            + (f" (pause reason {meta['pause_reason']})" if meta.get("pause_reason") else "")
            + ": nothing serves until it is Active."
        )
    for c in rows:
        live = c["status"] == "Active"
        if c["status"] in ("BudgetPaused", "BudgetAndManualPaused"):
            insights.append(f"Campaign '{c['name']}' is paused by its budget ({c['status']}).")
        if c["status"] == "Suspended":
            insights.append(f"Campaign '{c['name']}' is Suspended by Microsoft.")
        loc = c["locations"]
        if loc is not None and live and not loc["included"]:
            insights.append(f"Campaign '{c['name']}' targets no location, so it can serve everywhere.")
        if loc is not None and live and loc["intent"] and loc["intent"] != "PeopleIn":
            insights.append(
                f"Campaign '{c['name']}' reaches {loc['intent_meaning']}, not only people in them; "
                "for a local service business 'PeopleIn' keeps clicks local."
            )
        if c["shared_budget_id"]:
            insights.append(
                f"Campaign '{c['name']}' uses shared budget {c['shared_budget_id']}: its daily "
                "amount is set on that budget, not on the campaign."
            )
    return {
        "ad_account_id": account,
        "account_name": meta["name"],
        "currency": meta["currency"],
        "time_zone": meta["time_zone"],
        "campaigns": rows,
        "total_campaigns": len(rows),
        "insights": insights,
    }


def load_ad_groups(config: AdLoopConfig, account: str, customer: str, campaign_id: str = "") -> list[tuple[dict, dict]]:
    pairs = []
    for c in _campaigns(config, account, customer):
        if campaign_id and str(c.get("Id")) != str(campaign_id):
            continue
        for g in _ad_groups(config, account, customer, str(c.get("Id"))):
            pairs.append((g, c))
    return pairs


def get_microsoft_ad_groups(config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "") -> dict:
    """Ad groups with bid, network, language, dates and their campaign's status."""
    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    rows = []
    for g, c in load_ad_groups(config, account, customer, campaign_id.strip()):
        audience = audience_network(g, c)
        rows.append({
            "ad_group_id": str(g.get("Id")),
            "name": g.get("Name"),
            "status": g.get("Status"),
            "campaign_id": str(c.get("Id")),
            "campaign_name": c.get("Name"),
            "campaign_status": c.get("Status"),
            "cpc_bid": _money(g.get("CpcBid")),
            "network": g.get("Network"),
            "language": g.get("Language"),
            "audience_ads_bid_adjustment": g.get("AudienceAdsBidAdjustment"),
            "audience_network": audience,
            "start_date": _ms_date(g.get("StartDate")),
            "end_date": _ms_date(g.get("EndDate")),
        })
    insights: list[str] = []
    syndicated = [r for r in rows if r["status"] == "Active" and r["network"] == "OwnedAndOperatedAndSyndicatedSearch"]
    if syndicated:
        insights.append(
            f"{len(syndicated)} active ad group(s) also run on syndicated search partners "
            "(OwnedAndOperatedAndSyndicatedSearch); set OwnedAndOperatedOnly to keep ads on Bing, "
            "Yahoo and AOL only."
        )
    audience = [r for r in rows if r["status"] == "Active" and r["audience_network"]["on"]]
    if audience:
        insights.append(
            f"{len(audience)} active ad group(s) can show on the Microsoft Audience Network "
            "(display placements); a bid adjustment of -100 turns that off."
        )
    expired = [r for r in rows if r["status"] == "Expired"]
    if expired:
        insights.append(f"{len(expired)} ad group(s) have passed their end date (Expired).")
    return {"ad_account_id": account, "currency": meta["currency"], "ad_groups": rows,
            "total_ad_groups": len(rows), "insights": insights}


def audience_network(group: dict, campaign: dict) -> dict:
    """Whether an ad group can show on the Microsoft Audience Network, and where that is set.

    An ad group without its own AudienceAdsBidAdjustment inherits the
    campaign's; -100 switches the Audience Network off, any other value
    (or none at either level, Microsoft's default) leaves it on.
    """
    own, inherited = group.get("AudienceAdsBidAdjustment"), campaign.get("AudienceAdsBidAdjustment")
    effective = own if own is not None else inherited
    source = "ad group" if own is not None else "campaign" if inherited is not None else "Microsoft default"
    return {"on": effective != -100, "bid_adjustment": effective, "set_on": source}


def get_microsoft_keywords(
    config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "", ad_group_id: str = "",
) -> dict:
    """Keywords with match type, bid, status and editorial review, with parent statuses."""
    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    rows = []
    for g, c in load_ad_groups(config, account, customer, campaign_id.strip()):
        if ad_group_id and str(g.get("Id")) != str(ad_group_id):
            continue
        for k in _keywords(config, account, customer, str(g.get("Id"))):
            rows.append({
                "keyword_id": str(k.get("Id")),
                "text": k.get("Text"),
                "match_type": k.get("MatchType"),
                "bid": _money(k.get("Bid")),
                "status": k.get("Status"),
                "editorial_status": k.get("EditorialStatus"),
                "ad_group_id": str(g.get("Id")),
                "ad_group_name": g.get("Name"),
                "ad_group_status": g.get("Status"),
                "campaign_name": c.get("Name"),
                "campaign_status": c.get("Status"),
                "campaign_bidding": _bidding(c.get("BiddingScheme")).get("strategy"),
            })
    insights: list[str] = []
    disapproved = [r for r in rows if r["editorial_status"] == "Disapproved"]
    if disapproved:
        insights.append(
            f"{len(disapproved)} keyword(s) were disapproved in editorial review: "
            + ", ".join(f"'{r['text']}'" for r in disapproved[:5]) + "."
        )
    broad_manual = [
        r for r in rows
        if r["match_type"] == "Broad" and r["status"] == "Active"
        and r["campaign_bidding"] not in CONVERSION_BASED_STRATEGIES
    ]
    if broad_manual:
        insights.append(
            f"{len(broad_manual)} active broad-match keyword(s) sit in campaigns without conversion-based "
            "bidding (manual bids or Maximize Clicks), the combination that wastes the most spend; use "
            "phrase/exact, or move the campaign to MaxConversions or MaxConversionValue."
        )
    return {"ad_account_id": account, "currency": meta["currency"], "keywords": rows,
            "total_keywords": len(rows), "insights": insights}


def _texts(items: Any) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            asset = item.get("Asset") or {}
            text = asset.get("Text") or item.get("Text")
            pin = item.get("PinnedField")
            if text:
                out.append(f"{text} [pinned {pin}]" if pin else text)
    return out


def get_microsoft_ads(
    config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "", ad_group_id: str = "",
) -> dict:
    """Ads with headlines, descriptions, final URLs, status and editorial review."""
    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    rows = []
    for g, c in load_ad_groups(config, account, customer, campaign_id.strip()):
        if ad_group_id and str(g.get("Id")) != str(ad_group_id):
            continue
        for a in _ads(config, account, customer, str(g.get("Id"))):
            rows.append({
                "ad_id": str(a.get("Id")),
                "type": a.get("Type"),
                "status": a.get("Status"),
                "editorial_status": a.get("EditorialStatus"),
                "headlines": _texts(a.get("Headlines")) or [t for t in (a.get("TitlePart1"), a.get("TitlePart2"), a.get("TitlePart3")) if t],
                "descriptions": _texts(a.get("Descriptions")) or [t for t in (a.get("Text"), a.get("TextPart2")) if t],
                "path1": a.get("Path1"),
                "path2": a.get("Path2"),
                "final_urls": a.get("FinalUrls") or [],
                "ad_group_id": str(g.get("Id")),
                "ad_group_name": g.get("Name"),
                "ad_group_status": g.get("Status"),
                "campaign_name": c.get("Name"),
                "campaign_status": c.get("Status"),
            })
    insights: list[str] = []
    disapproved = [r for r in rows if r["editorial_status"] in ("Disapproved", "Inactive")]
    if disapproved:
        insights.append(f"{len(disapproved)} ad(s) are Disapproved or Inactive in editorial review.")
    live_groups = {r["ad_group_id"] for r in rows if r["status"] == "Active"}
    all_groups = {r["ad_group_id"] for r in rows}
    no_live = all_groups - live_groups
    if no_live:
        insights.append(f"{len(no_live)} ad group(s) have no active ad, so they cannot serve.")
    return {"ad_account_id": account, "ads": rows, "total_ads": len(rows), "insights": insights}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _report(
    config: AdLoopConfig, meta: dict, report_type: str, columns: list[str], start: date, end: date,
    *, aggregation: str = "Summary", scope_campaign_id: str = "",
) -> list[dict]:
    account = meta["ad_account_id"]
    # AccountIds and Campaigns are a union, not a filter: a campaign scope
    # must leave AccountIds out or the report covers the whole account.
    scope: dict[str, Any] = (
        {"Campaigns": [{"AccountId": account, "CampaignId": scope_campaign_id}]}
        if scope_campaign_id else {"AccountIds": [account]}
    )
    request = {
        "Type": report_type,
        "ReportName": f"AdLoop {report_type}",
        "Aggregation": aggregation,
        "Columns": columns,
        "Scope": scope,
        "Time": report_time(start, end, meta["time_zone"]),
    }
    return _client.run_report(config, request, account_id=account, customer_id=_customer(config, meta))


def _metrics(row: dict) -> dict:
    clicks = int(_num(row.get("Clicks")) or 0)
    impressions = int(_num(row.get("Impressions")) or 0)
    spend = round(_num(row.get("Spend")) or 0.0, 2)
    conversions = _num(row.get("ConversionsQualified")) or 0.0
    ctr = _num(row.get("Ctr"))
    out = {
        "impressions": impressions,
        "clicks": clicks,
        "spend": spend,
        "ctr": round(ctr / 100, 4) if ctr is not None else (round(clicks / impressions, 4) if impressions else None),
        "cpc": _money(row.get("AverageCpc")) if row.get("AverageCpc") not in (None, "") else (round(spend / clicks, 2) if clicks else None),
        "conversions": round(conversions, 2),
        "conversion_rate": round(conversions / clicks, 4) if clicks else None,
        "cpa": round(spend / conversions, 2) if conversions else None,
        "revenue": _money(row.get("Revenue")),
    }
    for column, name in _SHARE_NAMES.items():
        if row.get(column) not in (None, ""):
            out[name] = _num(row.get(column))
    return out


_ID_RENAMES = {
    "AccountId": "ad_account_id", "AccountName": "account_name",
    "CampaignId": "campaign_id", "CampaignName": "campaign_name", "CampaignStatus": "campaign_status",
    "CampaignType": "campaign_type", "AdGroupId": "ad_group_id", "AdGroupName": "ad_group_name",
    "AdGroupStatus": "ad_group_status", "Status": "ad_group_status", "KeywordId": "keyword_id",
    "Keyword": "keyword", "KeywordStatus": "keyword_status", "BidMatchType": "match_type",
    "QualityScore": "quality_score", "AdId": "ad_id", "AdType": "ad_type", "AdStatus": "ad_status",
    "FinalUrl": "final_url", "TimePeriod": "date", "DeviceType": "device",
}


def _totals(rows: list[dict], currency: str) -> dict:
    spend = round(sum(r["spend"] for r in rows), 2)
    clicks = sum(r["clicks"] for r in rows)
    impressions = sum(r["impressions"] for r in rows)
    conversions = round(sum(r["conversions"] for r in rows), 2)
    return {
        "spend": spend, "impressions": impressions, "clicks": clicks, "conversions": conversions,
        "ctr": round(clicks / impressions, 4) if impressions else None,
        "cpc": round(spend / clicks, 2) if clicks else None,
        "cpa": round(spend / conversions, 2) if conversions else None,
        "currency": currency,
    }


def get_microsoft_performance(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    level: str = "campaign",
    date_range_start: str = "",
    date_range_end: str = "",
    breakdown: str = "",
    campaign_id: str = "",
    compact: bool = False,
) -> dict:
    """Spend, clicks, conversions, CPA and impression share per level, statuses joined."""
    level = (level or "campaign").strip().lower()
    breakdown = (breakdown or "").strip().lower()
    _check_choice("level", level, _PERFORMANCE_LEVELS)
    if breakdown:
        _check_choice("breakdown", breakdown, _BREAKDOWNS)
    if level == "account" and campaign_id.strip():
        raise ValueError("campaign_id narrows campaign, ad_group, keyword and ad reports; "
                         "the account level covers the whole account.")
    meta = account_meta(config, ad_account_id)
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"])
    columns = list(_LEVEL_COLUMNS[level]) + _METRIC_COLUMNS
    aggregation = "Summary"
    if breakdown == "date":
        columns.insert(0, "TimePeriod")
        aggregation = "Daily"
    elif breakdown == "device":
        columns.insert(0, "DeviceType")
    elif level in ("campaign", "ad_group"):
        columns += _SHARE_COLUMNS
    raw = _report(config, meta, _PERFORMANCE_LEVELS[level], columns, start, end,
                  aggregation=aggregation, scope_campaign_id=campaign_id.strip())

    rows = []
    for r in raw:
        row = {_ID_RENAMES[k]: v for k, v in r.items() if k in _ID_RENAMES}
        row.update(_metrics(r))
        row["currency"] = meta["currency"]
        rows.append(row)
    rows.sort(key=lambda x: x["spend"], reverse=True)

    insights: list[str] = []
    name_key = {"campaign": "campaign_name", "ad_group": "ad_group_name", "keyword": "keyword", "ad": "ad_id"}.get(level)
    zero = [r for r in rows if r["spend"] > 0 and not r["conversions"]] if name_key and not breakdown else []
    if zero:
        insights.append(
            f"{len(zero)} {level.replace('_', ' ')}(s) spent {round(sum(r['spend'] for r in zero), 2)} "
            f"{meta['currency']} with zero conversions: "
            + ", ".join(str(r.get(name_key)) for r in zero[:5])
            + ". Check get_microsoft_conversion_setup before cutting them: calls may convert "
            "outside Microsoft's view."
        )
    budget_lost = [r for r in rows if (r.get("lost_to_budget_pct") or 0) >= 20 and r["conversions"]]
    if budget_lost:
        insights.append(
            f"{len(budget_lost)} converting {level.replace('_', ' ')}(s) lose 20%+ of impressions to "
            "budget: " + ", ".join(f"{r.get(name_key)} ({r['lost_to_budget_pct']:g}%)" for r in budget_lost[:5]) + "."
        )
    paused = [
        r for r in rows if r["spend"] > 0
        and {r.get("campaign_status"), r.get("ad_group_status"), r.get("keyword_status"), r.get("ad_status")}
        & {"Paused", "Deleted", "BudgetPaused", "BudgetAndManualPaused", "Expired"}
    ]
    if paused and not breakdown:
        insights.append(
            f"{len(paused)} row(s) spent in this window but sit under a paused, deleted or "
            "expired level now."
        )
    if not rows:
        insights.append("No delivery in this window.")

    result: dict[str, Any] = {
        "ad_account_id": meta["ad_account_id"],
        "level": level,
        "breakdown": breakdown or None,
        "date_range": {"start": start.isoformat(), "end": end.isoformat(), "time_zone": meta["time_zone"]},
        "currency": meta["currency"],
        "totals": _totals(rows, meta["currency"]),
        "insights": ([zone_note(meta["time_zone"])] if zone_note(meta["time_zone"]) else []) + insights,
        "note": (
            "Conversions are the account's conversion goals that count toward 'Conversions' "
            "(goals excluded from bidding appear only in All conversions). Ctr and conversion "
            "rate are fractions; impression share columns are percentages."
        ),
    }
    if compact:
        result.update(compact=True, total_rows=len(rows), rows_top_spend=rows[:10],
                      zero_conversion_spenders=zero[:10])
    else:
        result.update(rows=rows, total_rows=len(rows))
    return result


def get_microsoft_search_terms(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
    min_clicks: int = 2,
    compact: bool = False,
) -> dict:
    """What people searched before clicking, with the keyword and match type that matched."""
    meta = account_meta(config, ad_account_id)
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"])
    columns = [
        "CampaignName", "CampaignStatus", "AdGroupName", "AdGroupStatus", "SearchQuery", "Keyword",
        "BidMatchType", "DeliveredMatchType", "Impressions", "Clicks", "Spend", "Ctr", "AverageCpc",
        "ConversionsQualified", "Revenue",
    ]
    raw = _report(config, meta, "SearchQueryPerformanceReportRequest", columns, start, end,
                  scope_campaign_id=campaign_id.strip())
    rows = []
    for r in raw:
        row = {
            "search_term": r.get("SearchQuery"),
            "keyword": r.get("Keyword"),
            "keyword_match_type": r.get("BidMatchType"),
            "delivered_match_type": r.get("DeliveredMatchType"),
            "campaign_name": r.get("CampaignName"),
            "campaign_status": r.get("CampaignStatus"),
            "ad_group_name": r.get("AdGroupName"),
            "ad_group_status": r.get("AdGroupStatus"),
        }
        row.update(_metrics(r))
        rows.append(row)
    rows.sort(key=lambda x: x["spend"], reverse=True)
    waste = [r for r in rows if r["clicks"] >= max(int(min_clicks or 1), 1) and not r["conversions"]]
    converters = [r for r in rows if r["conversions"]]

    insights: list[str] = []
    if waste:
        insights.append(
            f"{len(waste)} search term(s) with {min_clicks}+ clicks and no conversion cost "
            f"{round(sum(r['spend'] for r in waste), 2)} {meta['currency']}; review them as "
            "negative keyword candidates (calls may convert outside Microsoft's view)."
        )
    loose = [r for r in rows if r["keyword_match_type"] in ("Exact", "Phrase") and r["delivered_match_type"] == "Broad"]
    if loose:
        insights.append(
            f"{len(loose)} term(s) matched exact/phrase keywords as broad (close variants); "
            "check them for intent drift."
        )
    if not rows:
        insights.append("No search terms with clicks or impressions in this window.")

    result: dict[str, Any] = {
        "ad_account_id": meta["ad_account_id"],
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "currency": meta["currency"],
        "totals": _totals(rows, meta["currency"]),
        "insights": ([zone_note(meta["time_zone"])] if zone_note(meta["time_zone"]) else []) + insights,
    }
    if compact:
        result.update(compact=True, total_terms=len(rows), top_by_spend=rows[:25],
                      waste_candidates=waste[:25], top_converters=converters[:10])
    else:
        result.update(search_terms=rows, total_terms=len(rows))
    return result


def get_microsoft_location_performance(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_id: str = "",
    group_by: str = "city",
) -> dict:
    """Where clicks came from, split into people physically there and people interested in it."""
    group_by = (group_by or "city").strip().lower()
    fields = {"city": "City", "metro": "MetroArea", "state": "State", "postal_code": "PostalCode",
              "most_specific": "MostSpecificLocation"}
    _check_choice("group_by", group_by, fields)
    meta = account_meta(config, ad_account_id)
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"])
    columns = list(dict.fromkeys([
        "CampaignName", "LocationType", fields[group_by], "State", "Country",
        "Impressions", "Clicks", "Spend", "ConversionsQualified",
    ]))
    raw = _report(config, meta, "GeographicPerformanceReportRequest", columns, start, end,
                  scope_campaign_id=campaign_id.strip())
    merged: dict[tuple, dict] = {}
    for r in raw:
        key = (r.get(fields[group_by]) or "(unknown)", r.get("State") or "", r.get("LocationType") or "")
        entry = merged.setdefault(key, {
            "location": key[0], "state": key[1] or None, "country": r.get("Country"),
            "location_type": key[2] or None, "impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0.0,
        })
        m = _metrics(r)
        for k in ("impressions", "clicks", "spend", "conversions"):
            entry[k] += m[k]
    rows = sorted(merged.values(), key=lambda x: x["spend"], reverse=True)
    for r in rows:
        r["spend"] = round(r["spend"], 2)
        r["conversions"] = round(r["conversions"], 2)

    total_spend = round(sum(r["spend"] for r in rows), 2)
    interest = [r for r in rows if r["location_type"] and "interest" in r["location_type"].lower()]
    interest_spend = round(sum(r["spend"] for r in interest), 2)
    insights: list[str] = []
    if total_spend and interest_spend / total_spend >= 0.1:
        insights.append(
            f"{round(100 * interest_spend / total_spend)}% of spend ({interest_spend} {meta['currency']}) "
            "came from people searching about the area from elsewhere (location of interest). "
            "Set the campaign's location intent to 'PeopleIn' to stop that."
        )
    return {
        "ad_account_id": meta["ad_account_id"],
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "group_by": group_by,
        "currency": meta["currency"],
        "totals": {"spend": total_spend, "clicks": sum(r["clicks"] for r in rows),
                   "conversions": round(sum(r["conversions"] for r in rows), 2),
                   "spend_from_location_of_interest": interest_spend},
        "locations": rows,
        "insights": ([zone_note(meta["time_zone"])] if zone_note(meta["time_zone"]) else []) + insights,
        "note": "Location type 'Physical location' is where the person was; 'Location of interest' is what they searched about.",
    }


# ---------------------------------------------------------------------------
# Conversion setup audit
# ---------------------------------------------------------------------------


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def get_microsoft_conversion_setup(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    min_clicks: int = 30,
) -> dict:
    """Audit UET tags, conversion goals, campaign goals and recent conversions; returns a verdict."""
    meta = account_meta(config, ad_account_id)
    account, customer = meta["ad_account_id"], _customer(config, meta)
    start, end = report_window(date_range_start, date_range_end, meta["time_zone"])
    checks: list[dict] = []
    insights: list[str] = []

    tags = campaign_request(config, "UetTags/QueryByIds", {"TagIds": None},
                            account_id=account, customer_id=customer).get("UetTags") or []
    goals = _conversion_goals(config, account, customer)
    campaigns = _campaigns(config, account, customer, with_goals=True)

    # 1. UET tag: required only by goals the tag reports (URL, duration,
    # pages per visit, event). A tag no active goal uses is a loose end, not
    # a broken setup.
    tags = [t for t in tags if isinstance(t, dict)]
    tag_goals = [g for g in goals if g.get("Status") == "Active" and g.get("Type") in _TAG_GOAL_TYPES]
    if not tags:
        if tag_goals:
            checks.append(_check(
                "uet tag", "fail",
                "No UET tag exists, yet " + ", ".join(f"'{g.get('Name')}'" for g in tag_goals[:5])
                + " need one: Microsoft cannot see those site conversions.",
            ))
        elif goals:
            checks.append(_check(
                "uet tag", "info",
                "No UET tag; the active goals arrive without one (offline import, app or store).",
            ))
        else:
            checks.append(_check("uet tag", "fail", "No UET tag exists: Microsoft cannot see any site conversion."))
    for t in tags:
        status = t.get("TrackingStatus")
        users = [g for g in tag_goals if str(g.get("TagId") or "") == str(t.get("Id"))]
        detail = {
            "Active": "Receiving events.",
            "Unverified": "Never received an event: the tag is not on the site, or consent blocks it.",
            "Inactive": "No events in the last 24 hours.",
        }.get(status, f"Tracking status {status}.")
        if status == "Active":
            level = "pass"
        elif status in ("Unverified", "Inactive"):
            level = "fail" if users or (len(tags) == 1 and tag_goals) else "warn"
            if level == "warn":
                detail += " No active goal uses this tag, so nothing depends on it."
        else:
            level = "warn"
        if users:
            detail += " Used by " + ", ".join(f"'{g.get('Name')}'" for g in users[:5]) + "."
        checks.append(_check(f"uet tag '{t.get('Name')}' ({t.get('Id')})", level, detail))

    # 2. Goals
    active_goals = [g for g in goals if g.get("Status") == "Active"]
    if not active_goals:
        checks.append(_check("conversion goals", "fail",
                             "No active conversion goal: Conversions stay 0 and conversion bidding has nothing to use."))
    for g in goals:
        tracking = g.get("TrackingStatus")
        level = {
            "RecordingConversions": "pass", "NoRecentConversions": "warn", "TagUnverified": "fail",
            "TagInactive": "fail", "InactiveDueToTagUnavailable": "fail",
        }.get(tracking, "info")
        if g.get("Status") != "Active":
            level = "info"
        detail = f"{g.get('Type')} goal, status {g.get('Status')}, tracking {tracking}"
        if g.get("ExcludeFromBidding"):
            detail += "; excluded from bidding (counts only in All conversions)"
        if g.get("ConversionWindowInMinutes"):
            detail += f"; {int(g['ConversionWindowInMinutes']) // 1440}-day window"
        checks.append(_check(f"goal '{g.get('Name')}'", level, detail + "."))
    counted = [g for g in active_goals if not g.get("ExcludeFromBidding")]
    if active_goals and not counted:
        checks.append(_check("goals counted in Conversions", "fail",
                             "Every active goal is excluded from bidding, so the Conversions column stays 0."))

    # 3. Campaign-level goals
    goal_names = {str(g.get("Id")): g.get("Name") for g in goals}
    for c in campaigns:
        ids = [str(i) for i in c.get("GoalIds") or []]
        if ids:
            checks.append(_check(
                f"campaign '{c.get('Name')}' goals", "info",
                "Optimizes for its own goals: " + ", ".join(goal_names.get(i, i) for i in ids) + ".",
            ))

    # 4. Clicks against conversions in the window
    report = _report(config, meta, "CampaignPerformanceReportRequest",
                     ["CampaignName", "CampaignStatus", "Clicks", "Spend", "ConversionsQualified",
                      "AllConversionsQualified"],
                     start, end)
    activity = []
    for r in report:
        clicks = int(_num(r.get("Clicks")) or 0)
        conversions = _num(r.get("ConversionsQualified")) or 0.0
        all_conversions = _num(r.get("AllConversionsQualified")) or 0.0
        activity.append({"campaign_name": r.get("CampaignName"), "campaign_status": r.get("CampaignStatus"),
                         "clicks": clicks, "spend": round(_num(r.get("Spend")) or 0.0, 2),
                         "conversions": conversions, "all_conversions": all_conversions})
        if clicks >= min_clicks and not all_conversions:
            checks.append(_check(
                f"campaign '{r.get('CampaignName')}' conversions", "warn",
                f"{clicks} clicks and no conversion of any goal. Check the UET tag on the landing "
                "pages and the goal's URL or event rule; phone calls are not counted unless call "
                "tracking feeds Microsoft.",
            ))
        elif clicks and all_conversions and not conversions:
            checks.append(_check(
                f"campaign '{r.get('CampaignName')}' conversions", "warn",
                f"{all_conversions:g} conversion(s) only in All conversions: the goals that fire are "
                "excluded from bidding.",
            ))

    order = {"fail": 0, "warn": 1, "info": 2, "pass": 3}
    checks.sort(key=lambda c: order.get(c["status"], 4))
    verdict = ("broken" if any(c["status"] == "fail" for c in checks)
               else "needs attention" if any(c["status"] == "warn" for c in checks) else "ok")
    insights.append(
        "A consent banner that blocks the UET tag hides conversions from people who decline; "
        "Microsoft's numbers then run below real leads."
    )
    return {
        "ad_account_id": account,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "verdict": verdict,
        "checks": checks,
        "uet_tags": [{"tag_id": str(t.get("Id")), "name": t.get("Name"), "tracking_status": t.get("TrackingStatus")}
                     for t in tags if isinstance(t, dict)],
        "goals": [
            {"goal_id": str(g.get("Id")), "name": g.get("Name"), "type": g.get("Type"),
             "status": g.get("Status"), "tracking_status": g.get("TrackingStatus"),
             "exclude_from_bidding": g.get("ExcludeFromBidding"), "count_type": g.get("CountType"),
             "scope": g.get("Scope"), "tag_id": str(g.get("TagId") or "") or None}
            for g in goals
        ],
        "campaign_activity": activity,
        "insights": ([zone_note(meta["time_zone"])] if zone_note(meta["time_zone"]) else []) + insights,
    }


# ---------------------------------------------------------------------------
# Location lookup (Microsoft's geographic location codes file)
# ---------------------------------------------------------------------------

_GEO_CACHE: dict[str, Any] = {}


def _geo_cache_path():
    from pathlib import Path

    return Path("~/.adloop/cache/microsoft_geo_locations.csv").expanduser()


def _geo_column(headers: list[str], *needles: str) -> str | None:
    lowered = {h: h.lower() for h in headers}
    for needle in needles:
        for header, low in lowered.items():
            if needle in low:
                return header
    return None


def load_geo_locations(config: AdLoopConfig, *, account: str = "", customer: str = "") -> list[dict]:
    """Every Microsoft location (id, name, type, status), cached per file version.

    GeoLocationsFileUrl/Query returns a short-lived download link and the
    file's last-modified time; the file is downloaded again only when it is
    newer than the cached copy.
    """
    import gzip
    import json as _json

    info = campaign_request(
        config, "GeoLocationsFileUrl/Query",
        {"Version": "2.0", "LanguageLocale": "en", "CompressionType": "GZip"},
        account_id=account, customer_id=customer,
    )
    modified = str(info.get("LastModifiedTimeUtc") or "")
    if _GEO_CACHE.get("modified") == modified and _GEO_CACHE.get("rows"):
        return _GEO_CACHE["rows"]
    path = _geo_cache_path()
    meta_path = path.with_suffix(".json")
    data: bytes | None = None
    try:
        if path.exists() and meta_path.exists() and _json.loads(meta_path.read_text()).get("modified") == modified:
            data = path.read_bytes()
    except (OSError, ValueError):
        data = None
    if data is None:
        url = info.get("FileUrl")
        if not url:
            raise _client_error("Microsoft returned no location file link.")
        data = _client.download_file(url, what="location file")
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            meta_path.write_text(_json.dumps({"modified": modified}))
        except OSError:
            pass
    rows = _client.parse_csv(data)
    headers = list(rows[0]) if rows else []
    id_col = _geo_column(headers, "location id", "id")
    name_col = _geo_column(headers, "display name", "name")
    type_col = _geo_column(headers, "type")
    status_col = _geo_column(headers, "status")
    out = [
        {
            "location_id": str(r.get(id_col) or "").strip(),
            "name": r.get(name_col) or "",
            "type": r.get(type_col) if type_col else None,
            "status": r.get(status_col) if status_col else None,
        }
        for r in rows if id_col and str(r.get(id_col) or "").strip()
    ]
    _GEO_CACHE.update(modified=modified, rows=out)
    return out


def _client_error(message: str):
    from adloop.microsoft_ads.auth import MicrosoftAdsApiError

    return MicrosoftAdsApiError(message)


def location_names(rows: list[dict], ids: list[str]) -> dict[str, str]:
    wanted = {str(i) for i in ids}
    return {r["location_id"]: r["name"] for r in rows if r["location_id"] in wanted}


def search_microsoft_locations(
    config: AdLoopConfig, *, ad_account_id: str = "", query: str = "", location_type: str = "", limit: int = 20,
) -> dict:
    """Location ids (countries, states, metros, cities, postal codes) matching a name."""
    query = (query or "").strip().lower()
    if not query:
        raise ValueError("query is required, e.g. 'Chicago' or '60601'.")
    meta = account_meta(config, ad_account_id)
    rows = load_geo_locations(config, account=meta["ad_account_id"], customer=_customer(config, meta))
    kind = (location_type or "").strip().lower()
    hits = [
        r for r in rows
        if query in r["name"].lower() and (not kind or kind in str(r.get("type") or "").lower())
    ]
    hits.sort(key=lambda r: (not r["name"].lower().startswith(query), "deprecat" in str(r.get("status") or "").lower(), len(r["name"])))
    limit = max(1, min(int(limit or 20), 100))
    return {
        "query": query,
        "locations": hits[:limit],
        "total_matches": len(hits),
        "note": (
            "Pass location_id values to update_microsoft_campaign_targeting or "
            "draft_microsoft_campaign. Locations whose status is PendingDeprecation cannot be added."
        ),
    }
