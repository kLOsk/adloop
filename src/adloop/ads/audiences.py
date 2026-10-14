"""Google Ads audiences: list them, see what is attached, attach them (#48).

Reads cover the account's own audiences (remarketing/customer lists, custom
segments, combined segments), Google's segment catalog (in-market and
affinity), and the audiences attached to one campaign or ad group together
with its audience mode.

``draft_audience_attachment`` attaches audiences in one atomic
GoogleAdsService.Mutate. Observation is the default: the audiences only
adjust bids and report, reach stays the same. Targeting narrows serving to
the audiences, so it has to be asked for explicitly and needs a double
confirmation. The mode is the entity's AUDIENCE target restriction
(``bid_only``); it is always written explicitly with the attachment, so the
result never depends on Google's default for the campaign type. Exclusions
(``exclude=True``) are negative criteria and leave the mode alone.

Performance Max takes audiences only as asset group signals
(draft_asset_group_signals) and is refused here with that pointer. Creating
audiences is out of scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Audience kinds a criterion can point at: (criterion type in GAQL,
# criterion field, resource collection).
_KINDS = {
    "user_list": ("USER_LIST", "user_list.user_list", "userLists"),
    "segment": ("USER_INTEREST", "user_interest.user_interest_category", "userInterests"),
    "custom_audience": ("CUSTOM_AUDIENCE", "custom_audience.custom_audience", "customAudiences"),
    "combined_audience": ("COMBINED_AUDIENCE", "combined_audience.combined_audience", "combinedAudiences"),
}
_TYPE_TO_KIND = {v[0]: k for k, v in _KINDS.items()}

# Which kinds each campaign type accepts. Custom segments cannot be attached
# to Search; combined segments are Search-only. Google's dry run checks the
# rest (e.g. level rules) against the live account.
_COMPATIBLE = {
    "SEARCH": {"user_list", "segment", "combined_audience"},
    "DISPLAY": {"user_list", "segment", "custom_audience"},
    "VIDEO": {"user_list", "segment", "custom_audience"},
    "SHOPPING": {"user_list", "segment"},
}
_KIND_LABELS = {
    "user_list": "remarketing or customer lists",
    "segment": "in-market and affinity segments",
    "custom_audience": "custom segments",
    "combined_audience": "combined segments",
}
# Search shows ads to a remarketing list once it has this many active users.
_SEARCH_MIN_SIZE = 1000
_MAX_PER_DRAFT = 50


def _id(value: object) -> str:
    return str(value or "").strip()


def _last(resource: str | None) -> str:
    return str(resource or "").rsplit("/", 1)[-1]


def get_audience_lists(config: AdLoopConfig, *, customer_id: str = "") -> dict:
    """The account's own audiences: remarketing/customer lists, custom and combined segments."""
    from adloop.ads.gaql import execute_query

    lists = execute_query(
        config,
        customer_id,
        "SELECT user_list.id, user_list.name, user_list.type, user_list.membership_status, "
        "user_list.size_for_search, user_list.size_for_display, user_list.eligible_for_search, "
        "user_list.eligible_for_display, user_list.membership_life_span, "
        "user_list.account_user_list_status FROM user_list",
    )
    custom = execute_query(
        config,
        customer_id,
        "SELECT custom_audience.id, custom_audience.name, custom_audience.type, custom_audience.description "
        "FROM custom_audience WHERE custom_audience.status = 'ENABLED'",
    )
    combined = execute_query(
        config,
        customer_id,
        "SELECT combined_audience.id, combined_audience.name, combined_audience.description "
        "FROM combined_audience WHERE combined_audience.status = 'ENABLED'",
    )
    user_lists = []
    for row in lists:
        if row.get("user_list.account_user_list_status") == "CLOSED":
            continue
        user_lists.append({
            "user_list_id": str(row.get("user_list.id")),
            "name": row.get("user_list.name"),
            "type": row.get("user_list.type"),
            "membership_status": row.get("user_list.membership_status"),
            "size_for_search": row.get("user_list.size_for_search"),
            "size_for_display": row.get("user_list.size_for_display"),
            "eligible_for_search": row.get("user_list.eligible_for_search"),
            "eligible_for_display": row.get("user_list.eligible_for_display"),
            "membership_life_span_days": row.get("user_list.membership_life_span"),
        })
    insights = []
    small = [u["name"] for u in user_lists if (u["size_for_search"] or 0) < _SEARCH_MIN_SIZE and u["eligible_for_search"]]
    if small:
        insights.append(
            f"Below {_SEARCH_MIN_SIZE:,} users for Search (attachable, but they adjust nothing there until they grow): "
            + ", ".join(small[:10])
        )
    closed = [u["name"] for u in user_lists if u["membership_status"] == "CLOSED"]
    if closed:
        insights.append("Closed lists no longer add members and shrink over time: " + ", ".join(closed[:10]))
    if not user_lists and not custom and not combined:
        insights.append(
            "No audiences of your own yet. In-market and affinity segments (search_audience_segments) work without any."
        )
    return {
        "user_lists": user_lists,
        "custom_segments": [
            {"custom_audience_id": str(r.get("custom_audience.id")), "name": r.get("custom_audience.name"),
             "type": r.get("custom_audience.type"), "description": r.get("custom_audience.description")}
            for r in custom
        ],
        "combined_segments": [
            {"combined_audience_id": str(r.get("combined_audience.id")), "name": r.get("combined_audience.name"),
             "description": r.get("combined_audience.description")}
            for r in combined
        ],
        "insights": insights,
    }


def search_audience_segments(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    query: str = "",
    kind: str = "all",
    limit: int = 25,
) -> dict:
    """Google's in-market and affinity segments whose name matches ``query``."""
    from adloop.ads.gaql import execute_query

    query = str(query or "").strip()
    if len(query) < 2:
        return {"error": "query needs at least 2 characters, e.g. 'running' or 'insurance'"}
    taxonomies = {"in_market": ["IN_MARKET"], "affinity": ["AFFINITY"], "all": ["IN_MARKET", "AFFINITY"]}
    if kind not in taxonomies:
        return {"error": "kind must be in_market, affinity or all"}
    limit = max(1, min(int(limit or 25), 100))
    # GAQL's LIKE is case-sensitive and the catalog is small (about 1,500
    # segments), so match here instead. launched_to_all is False for most
    # segments ("not in every channel and locale"), so it is no filter.
    rows = execute_query(
        config,
        customer_id,
        "SELECT user_interest.user_interest_id, user_interest.name, user_interest.taxonomy_type FROM user_interest "
        f"WHERE user_interest.taxonomy_type IN ({', '.join(repr(t) for t in taxonomies[kind])})",
    )
    needle = query.lower()
    matches = [r for r in rows if needle in str(r.get("user_interest.name") or "").lower()]
    # Names that start with the query first, then the rest alphabetically.
    matches.sort(key=lambda r: (not str(r.get("user_interest.name")).lower().startswith(needle), str(r.get("user_interest.name"))))
    segments = [
        {
            "segment_id": str(r.get("user_interest.user_interest_id")),
            "name": r.get("user_interest.name"),
            "kind": "in_market" if r.get("user_interest.taxonomy_type") == "IN_MARKET" else "affinity",
        }
        for r in matches[:limit]
    ]
    return {"query": query, "segments": segments, "count": len(segments)}


def _entity_context(config: AdLoopConfig, customer_id: str, campaign_id: str, ad_group_id: str) -> dict:
    """Channel, names, the AUDIENCE mode and the audience criteria at both levels."""
    from adloop.ads.gaql import execute_query

    if ad_group_id:
        rows = execute_query(
            config,
            customer_id,
            "SELECT ad_group.id, ad_group.name, ad_group.targeting_setting.target_restrictions, campaign.id, "
            "campaign.name, campaign.advertising_channel_type FROM ad_group "
            f"WHERE ad_group.id = {int(ad_group_id)}",
        )
        if not rows:
            raise ValueError(f"ad group {ad_group_id} was not found")
        row = rows[0]
        restrictions = row.get("ad_group.targeting_setting.target_restrictions") or []
        campaign_id = str(row.get("campaign.id"))
        entity_name = row.get("ad_group.name")
    else:
        rows = execute_query(
            config,
            customer_id,
            "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
            "campaign.targeting_setting.target_restrictions FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}",
        )
        if not rows:
            raise ValueError(f"campaign {campaign_id} was not found")
        row = rows[0]
        restrictions = row.get("campaign.targeting_setting.target_restrictions") or []
        entity_name = row.get("campaign.name")

    types = ", ".join(f"'{t}'" for t, _f, _c in _KINDS.values())
    campaign_criteria = execute_query(
        config,
        customer_id,
        "SELECT campaign_criterion.criterion_id, campaign_criterion.type, campaign_criterion.negative, "
        "campaign_criterion.user_list.user_list, campaign_criterion.user_interest.user_interest_category, "
        "campaign_criterion.custom_audience.custom_audience, campaign_criterion.combined_audience.combined_audience "
        f"FROM campaign_criterion WHERE campaign.id = {int(campaign_id)} AND campaign_criterion.type IN ({types}) "
        "AND campaign_criterion.status != 'REMOVED'",
    )
    ad_group_criteria = execute_query(
        config,
        customer_id,
        "SELECT ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.type, ad_group_criterion.negative, "
        "ad_group_criterion.user_list.user_list, ad_group_criterion.user_interest.user_interest_category, "
        "ad_group_criterion.custom_audience.custom_audience, ad_group_criterion.combined_audience.combined_audience "
        f"FROM ad_group_criterion WHERE campaign.id = {int(campaign_id)} AND ad_group_criterion.type IN ({types}) "
        "AND ad_group_criterion.status != 'REMOVED'",
    )

    def criteria(rows: list[dict], prefix: str, owner: str) -> list[dict]:
        out = []
        for r in rows:
            kind = _TYPE_TO_KIND.get(str(r.get(f"{prefix}.type") or ""))
            if not kind:
                continue
            out.append({
                "kind": kind,
                "audience_id": _last(r.get(f"{prefix}.{_KINDS[kind][1]}")),
                "negative": bool(r.get(f"{prefix}.negative")),
                "remove_id": f"{owner if owner else r.get('ad_group.id')}~{r.get(f'{prefix}.criterion_id')}",
                "ad_group_id": str(r.get("ad_group.id")) if not owner else None,
            })
        return out

    mode = None
    for restriction in restrictions:
        if isinstance(restriction, dict) and restriction.get("targeting_dimension") == "AUDIENCE":
            mode = "observation" if restriction.get("bid_only") else "targeting"
    return {
        "campaign_id": campaign_id,
        "channel": str(row.get("campaign.advertising_channel_type") or ""),
        "campaign_name": row.get("campaign.name"),
        "entity_name": entity_name,
        "mode": mode,
        "restrictions": [r for r in restrictions if isinstance(r, dict)],
        "campaign_criteria": criteria(campaign_criteria, "campaign_criterion", campaign_id),
        "ad_group_criteria": criteria(ad_group_criteria, "ad_group_criterion", ""),
    }


def _names(config: AdLoopConfig, customer_id: str, entries: list[dict], lists: dict | None = None) -> None:
    """Fill in ``name`` for criteria entries (lists from get_audience_lists, segments by id)."""
    from adloop.ads.gaql import execute_query

    lists = lists or get_audience_lists(config, customer_id=customer_id)
    names = {("user_list", u["user_list_id"]): u["name"] for u in lists["user_lists"]}
    names |= {("custom_audience", c["custom_audience_id"]): c["name"] for c in lists["custom_segments"]}
    names |= {("combined_audience", c["combined_audience_id"]): c["name"] for c in lists["combined_segments"]}
    segment_ids = sorted({e["audience_id"] for e in entries if e["kind"] == "segment" and e["audience_id"].isdigit()})
    if segment_ids:
        rows = execute_query(
            config,
            customer_id,
            "SELECT user_interest.user_interest_id, user_interest.name, user_interest.taxonomy_type FROM user_interest "
            f"WHERE user_interest.user_interest_id IN ({', '.join(segment_ids)})",
        )
        names |= {("segment", str(r.get("user_interest.user_interest_id"))): r.get("user_interest.name") for r in rows}
    for entry in entries:
        entry["name"] = names.get((entry["kind"], entry["audience_id"]))


def get_audience_targeting(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
) -> dict:
    """Audiences attached to one campaign or ad group, with the audience mode."""
    campaign_id, ad_group_id = _id(campaign_id), _id(ad_group_id)
    if bool(campaign_id) == bool(ad_group_id):
        return {"error": "Provide exactly one of campaign_id or ad_group_id"}
    if not (campaign_id or ad_group_id).isdigit():
        return {"error": "campaign_id / ad_group_id must be a numeric Google Ads id"}
    try:
        ctx = _entity_context(config, customer_id, campaign_id, ad_group_id)
    except ValueError as e:
        return {"error": str(e)}
    if ad_group_id:
        attached = [c for c in ctx["ad_group_criteria"] if c["ad_group_id"] == ad_group_id]
    else:
        attached = list(ctx["campaign_criteria"])
    _names(config, customer_id, attached)
    for entry in attached:
        entry.pop("ad_group_id", None)
    insights = []
    if ctx["mode"] == "targeting" and any(not c["negative"] for c in attached):
        insights.append("Targeting mode: ads only reach people in the attached audiences.")
    if ad_group_id and any(not c["negative"] for c in ctx["campaign_criteria"]):
        insights.append("The campaign has audiences of its own; Google does not allow audiences at both levels.")
    return {
        "level": "ad_group" if ad_group_id else "campaign",
        "campaign_id": ctx["campaign_id"],
        "campaign_name": ctx["campaign_name"],
        "channel": ctx["channel"],
        "audience_mode": ctx["mode"] or "not set",
        "audiences": [c for c in attached if not c["negative"]],
        "exclusions": [c for c in attached if c["negative"]],
        "insights": insights,
    }


def draft_audience_attachment(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    user_list_ids: list[str] | None = None,
    segment_ids: list[str] | None = None,
    custom_audience_ids: list[str] | None = None,
    combined_audience_ids: list[str] | None = None,
    mode: str = "observation",
    exclude: bool = False,
) -> dict:
    """Draft attaching audiences to a campaign or ad group: returns a preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_audience_criteria", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    campaign_id, ad_group_id = _id(campaign_id), _id(ad_group_id)
    requested = {
        "user_list": user_list_ids,
        "segment": segment_ids,
        "custom_audience": custom_audience_ids,
        "combined_audience": combined_audience_ids,
    }
    wanted = [
        {"kind": kind, "audience_id": _id(value)}
        for kind, values in requested.items()
        for value in dict.fromkeys(values or [])
        if _id(value)
    ]
    errors = []
    if bool(campaign_id) == bool(ad_group_id):
        errors.append("Provide exactly one of campaign_id or ad_group_id")
    elif not (campaign_id or ad_group_id).isdigit():
        errors.append("campaign_id / ad_group_id must be a numeric Google Ads id")
    if not wanted:
        errors.append("Pass at least one of user_list_ids, segment_ids, custom_audience_ids, combined_audience_ids")
    bad = [w["audience_id"] for w in wanted if not w["audience_id"].isdigit()]
    if bad:
        errors.append(f"audience ids must be numeric: {', '.join(bad)}")
    if len(wanted) > _MAX_PER_DRAFT:
        errors.append(f"at most {_MAX_PER_DRAFT} audiences per draft")
    if mode not in ("observation", "targeting"):
        errors.append("mode must be 'observation' (default, bids and reporting only) or 'targeting'")
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        ctx = _entity_context(config, customer_id, campaign_id, ad_group_id)
    except ValueError as e:
        return {"error": str(e)}
    channel = ctx["channel"]
    if channel == "PERFORMANCE_MAX":
        return {"error": (
            "Performance Max takes audiences only as asset group signals, not as targeting. "
            "Use get_asset_group_signals and draft_asset_group_signals instead."
        )}
    allowed = _COMPATIBLE.get(channel)
    if allowed is None:
        return {"error": f"Attaching audiences to {channel or 'this'} campaigns is not supported yet; use the Google Ads interface."}
    for kind in {w["kind"] for w in wanted} - allowed:
        errors.append(
            f"{_KIND_LABELS[kind].capitalize()} cannot be attached to {channel} campaigns"
            + (" (custom segments work on Display and Video)" if kind == "custom_audience" else "")
            + (" (combined segments are Search-only)" if kind == "combined_audience" else "")
        )

    lists = get_audience_lists(config, customer_id=customer_id)
    known = {
        "user_list": {u["user_list_id"]: u for u in lists["user_lists"]},
        "custom_audience": {c["custom_audience_id"]: c for c in lists["custom_segments"]},
        "combined_audience": {c["combined_audience_id"]: c for c in lists["combined_segments"]},
    }
    _names(config, customer_id, wanted, lists)
    warnings = []
    for w in wanted:
        if w["kind"] == "segment":
            if not w["name"]:
                errors.append(f"segment {w['audience_id']} was not found (see search_audience_segments)")
            continue
        info = known[w["kind"]].get(w["audience_id"])
        if not info:
            errors.append(f"{w['kind'].replace('_', ' ')} {w['audience_id']} is not an audience of this account (see get_audience_lists)")
            continue
        if w["kind"] == "user_list":
            if channel == "SEARCH" and info.get("eligible_for_search") is False:
                errors.append(f"list '{info['name']}' is not eligible for Search")
            elif channel == "SEARCH" and (info.get("size_for_search") or 0) < _SEARCH_MIN_SIZE:
                size = info.get("size_for_search") or 0
                if mode == "targeting" and not exclude:
                    errors.append(
                        f"list '{info['name']}' has {size:,} Search users; with targeting the ads would not serve "
                        f"until it reaches {_SEARCH_MIN_SIZE:,}. Use observation, or wait for the list to grow."
                    )
                else:
                    warnings.append(f"list '{info['name']}' has {size:,} Search users; it takes effect from {_SEARCH_MIN_SIZE:,}")
            if info.get("membership_status") == "CLOSED":
                warnings.append(f"list '{info['name']}' is closed: it no longer adds members")

    # Google keeps audiences at one level per campaign.
    if not exclude:
        if ad_group_id and any(not c["negative"] for c in ctx["campaign_criteria"]):
            errors.append("The campaign already has audiences; Google does not allow audiences at both campaign and ad group level. Attach at the campaign instead.")
        if campaign_id and any(not c["negative"] for c in ctx["ad_group_criteria"]):
            errors.append("Ad groups of this campaign already have audiences; Google does not allow both levels. Attach at those ad groups instead.")
    if errors:
        return {"error": "Validation failed", "details": errors}

    existing = ctx["ad_group_criteria"] if ad_group_id else ctx["campaign_criteria"]
    present = {
        (c["kind"], c["audience_id"], c["negative"])
        for c in existing
        if not ad_group_id or c["ad_group_id"] == ad_group_id
    }
    duplicates = [w for w in wanted if (w["kind"], w["audience_id"], exclude) in present]
    for w in duplicates:
        warnings.append(f"'{w['name'] or w['audience_id']}' is already attached; skipped")
    wanted = [w for w in wanted if w not in duplicates]
    if not wanted:
        return {"error": "Nothing to attach: every audience is already there", "warnings": warnings}

    set_mode = None
    double_confirm = False
    if not exclude and ctx["mode"] != mode:
        set_mode = mode
        others = [c for c in existing if not c["negative"] and (not ad_group_id or c["ad_group_id"] == ad_group_id)]
        if mode == "targeting":
            double_confirm = True
            warnings.insert(0, (
                "TARGETING narrows delivery: from now on the ads only reach people in the attached audiences"
                + (" (including the ones already attached)" if others else "")
                + ". Impressions and clicks usually drop sharply. Observation keeps the reach and only adjusts bids."
            ))
        elif ctx["mode"] == "targeting" and others:
            warnings.insert(0, "Switches from targeting to observation: the audiences already attached stop restricting delivery, so reach grows.")
    elif mode == "targeting" and not exclude:
        warnings.insert(0, "The audience mode is already targeting: the ads only reach people in the attached audiences.")
    if exclude:
        warnings.append("Exclusions keep these people from seeing the ads; the audience mode is unchanged.")

    plan = ChangePlan(
        operation="add_audience_criteria",
        entity_type="ad_group" if ad_group_id else "campaign",
        entity_id=ad_group_id or campaign_id,
        customer_id=customer_id,
        requires_double_confirm=double_confirm,
        changes={
            "level": "ad_group" if ad_group_id else "campaign",
            "campaign_id": ctx["campaign_id"],
            "ad_group_id": ad_group_id or None,
            "name": ctx["entity_name"],
            "channel": channel,
            "exclude": bool(exclude),
            "audiences": [{"kind": w["kind"], "audience_id": w["audience_id"], "name": w["name"]} for w in wanted],
            "audience_mode_before": ctx["mode"] or "not set",
            "set_audience_mode": set_mode,
            "target_restrictions": ctx["restrictions"],
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_audience_criteria(client: object, cid: str, changes: dict) -> dict:
    """One mutate: the audience mode (if it changes), then the criteria."""
    service = client.get_service("GoogleAdsService")
    at_ad_group = changes["level"] == "ad_group"
    operations = []

    if changes.get("set_audience_mode"):
        op = client.get_type("MutateOperation")
        if at_ad_group:
            entity = op.ad_group_operation.update
            entity.resource_name = service.ad_group_path(cid, changes["ad_group_id"])
            mask = op.ad_group_operation.update_mask
        else:
            entity = op.campaign_operation.update
            entity.resource_name = service.campaign_path(cid, changes["campaign_id"])
            mask = op.campaign_operation.update_mask
        dimension = client.enums.TargetingDimensionEnum
        # The mask replaces the whole list: keep the other dimensions as they are.
        for existing in changes.get("target_restrictions") or []:
            if existing.get("targeting_dimension") == "AUDIENCE":
                continue
            kept = client.get_type("TargetRestriction")
            kept.targeting_dimension = getattr(dimension, existing["targeting_dimension"])
            kept.bid_only = bool(existing.get("bid_only"))
            entity.targeting_setting.target_restrictions.append(kept)
        audience = client.get_type("TargetRestriction")
        audience.targeting_dimension = dimension.AUDIENCE
        audience.bid_only = changes["set_audience_mode"] == "observation"
        entity.targeting_setting.target_restrictions.append(audience)
        mask.paths.append("targeting_setting.target_restrictions")
        operations.append(op)

    for entry in changes["audiences"]:
        _type, field, collection = _KINDS[entry["kind"]]
        op = client.get_type("MutateOperation")
        if at_ad_group:
            criterion = op.ad_group_criterion_operation.create
            criterion.ad_group = service.ad_group_path(cid, changes["ad_group_id"])
        else:
            criterion = op.campaign_criterion_operation.create
            criterion.campaign = service.campaign_path(cid, changes["campaign_id"])
        message, attribute = field.split(".")
        setattr(getattr(criterion, message), attribute, f"customers/{cid}/{collection}/{entry['audience_id']}")
        if changes.get("exclude"):
            criterion.negative = True
        operations.append(op)

    service.mutate(customer_id=cid, mutate_operations=operations)
    return {
        "level": changes["level"],
        "entity_id": changes.get("ad_group_id") or changes["campaign_id"],
        "audiences_attached": len(changes["audiences"]),
        "excluded": bool(changes.get("exclude")),
        "audience_mode": changes.get("set_audience_mode") or changes.get("audience_mode_before"),
    }
