"""Asset extensions — promotion and price assets.

Both are created through one ``GoogleAdsService.Mutate`` call: the asset with a
temporary resource name (``-1``) plus the link that attaches it to an ad
group (``AdGroupAsset``), a campaign (``CampaignAsset``) or the whole account
(``CustomerAsset``). The account level is never inferred from a missing
campaign id: it applies to every eligible campaign, so it is an explicit
``scope="account"`` opt-in.

Units on the v25 protos (verified against the SDK):

    PromotionAsset.percent_off        1,000,000 = 100%  (10% -> 100_000)
    PromotionAsset.money_amount_off   Money.amount_micros (1.00 -> 1_000_000)
    PromotionAsset.orders_over_amount Money.amount_micros
    PriceOffering.price               Money.amount_micros

``update_promotion`` is a swap: create the new asset, link it at the old
link's scope and remove the old link, all in one batched Mutate so the
promotion is never shown twice or missing. The old link is looked up before
anything is sent; when it is gone the apply refuses instead of only adding.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from adloop.ads.enums import enum_names

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


# ---------------------------------------------------------------------------
# Link scope (ad group / campaign / account)
# ---------------------------------------------------------------------------

_LINK_SCOPES = ("ad_group", "campaign", "account")
# Link resource per scope: also the ChangePlan entity_type, the GAQL resource,
# the ``*_operation`` field on MutateOperation and the ``*_result`` field on
# its response.
_LINK_RESOURCE = {
    "ad_group": "ad_group_asset",
    "campaign": "campaign_asset",
    "account": "customer_asset",
}


def _resolve_asset_link_scope(
    scope: str, campaign_id: str, ad_group_id: str, customer_id: str
) -> tuple[dict, list[str]]:
    """Decide where an asset is linked. Returns (scope_info, errors).

    Exactly one of ``ad_group_id`` / ``campaign_id`` selects an ad-group or
    campaign link. Account level needs ``scope="account"`` and no ids; empty
    ids alone are an error, never a silent account-wide link.
    """
    errors: list[str] = []
    scope = (scope or "").strip().lower()
    campaign_id = str(campaign_id or "").strip()
    ad_group_id = str(ad_group_id or "").strip()

    if scope and scope not in _LINK_SCOPES:
        errors.append(
            f"scope '{scope}' invalid; valid values: {list(_LINK_SCOPES)} "
            "(or empty to infer it from campaign_id / ad_group_id)"
        )
        return {}, errors
    if campaign_id and not campaign_id.isdigit():
        errors.append("campaign_id must be a numeric ID")
    if ad_group_id and not ad_group_id.isdigit():
        errors.append("ad_group_id must be a numeric ID")
    if errors:
        return {}, errors

    if scope == "account":
        if campaign_id or ad_group_id:
            errors.append(
                "scope='account' links the asset to the whole account; "
                "leave campaign_id and ad_group_id empty"
            )
            return {}, errors
        return _scope_info("account", "", "", customer_id), []

    if campaign_id and ad_group_id:
        errors.append(
            "Pass either campaign_id or ad_group_id, not both — the asset is "
            "linked at exactly one level"
        )
        return {}, errors
    if not campaign_id and not ad_group_id:
        errors.append(
            "campaign_id or ad_group_id is required. Account-level linking "
            "(every eligible campaign) needs scope='account'"
        )
        return {}, errors

    inferred = "ad_group" if ad_group_id else "campaign"
    if scope and scope != inferred:
        errors.append(
            f"scope='{scope}' does not match the id given "
            f"({'ad_group_id' if ad_group_id else 'campaign_id'})"
        )
        return {}, errors
    return _scope_info(inferred, campaign_id, ad_group_id, customer_id), []


def _scope_info(
    scope: str, campaign_id: str, ad_group_id: str, customer_id: str
) -> dict:
    entity_id = {"ad_group": ad_group_id, "campaign": campaign_id}.get(
        scope, customer_id
    )
    return {
        "scope": scope,
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
        "entity_type": _LINK_RESOURCE[scope],
        "entity_id": entity_id,
    }


def _link_create_operation(
    client: object,
    cid: str,
    changes: dict,
    asset_resource: str,
    field_type: object,
) -> object:
    """A MutateOperation linking ``asset_resource`` at the plan's scope."""
    googleads_service = client.get_service("GoogleAdsService")
    scope = changes.get("scope")
    op = client.get_type("MutateOperation")
    if scope == "ad_group":
        link = op.ad_group_asset_operation.create
        link.ad_group = googleads_service.ad_group_path(cid, changes["ad_group_id"])
    elif scope == "campaign":
        link = op.campaign_asset_operation.create
        link.campaign = googleads_service.campaign_path(cid, changes["campaign_id"])
    elif scope == "account":
        link = op.customer_asset_operation.create
    else:
        raise ValueError(f"Unknown asset link scope: {scope!r}")
    link.asset = asset_resource
    link.field_type = field_type
    return op


def _link_result(resp: object, scope: str) -> str:
    return getattr(resp, f"{_LINK_RESOURCE[scope]}_result").resource_name


def _apply_create_linked_asset(
    client: object,
    cid: str,
    changes: dict,
    field_type: object,
    populate: object,
) -> dict:
    """Create one asset and link it at the plan's scope in a single Mutate."""
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    temp_asset = asset_service.asset_path(cid, "-1")

    create_op = client.get_type("MutateOperation")
    asset = create_op.asset_operation.create
    asset.resource_name = temp_asset
    populate(asset)

    link_op = _link_create_operation(client, cid, changes, temp_asset, field_type)
    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=[create_op, link_op]
    )
    responses = list(response.mutate_operation_responses)
    return {
        "scope": changes["scope"],
        "asset": responses[0].asset_result.resource_name,
        "link": _link_result(responses[1], changes["scope"]),
    }


# ---------------------------------------------------------------------------
# Shared field checks
# ---------------------------------------------------------------------------

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_iso_date(value: str) -> bool:
    """True for a real calendar date written YYYY-MM-DD."""
    from datetime import date

    if not _ISO_DATE_RE.match(value or ""):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _as_amount(value: object, label: str, errors: list[str]) -> float:
    """Parse a non-negative amount; record an error and return 0 otherwise."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        errors.append(f"{label} must be a number")
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number")
        return 0.0
    if amount < 0:
        errors.append(f"{label} must not be negative; got {value}")
        return 0.0
    return amount


def _micros(amount: float) -> int:
    """Account currency -> micros, rounded (19.99 is 19_990_000, not ..._999)."""
    return round(float(amount) * 1_000_000)


def _url_warnings_or_errors(urls: list[str]) -> tuple[list[str], list[str]]:
    """Check landing pages are reachable. Returns (errors, warnings)."""
    from adloop.ads.write import _validate_urls

    checks, inconclusive = _validate_urls(list(dict.fromkeys(u for u in urls if u)))
    errors = [f"final_url '{u}' is not reachable: {err}" for u, err in checks.items() if err]
    warnings = [f"'{u}': {msg}" for u, msg in inconclusive.items()]
    return errors, warnings


# ---------------------------------------------------------------------------
# Promotion assets
# ---------------------------------------------------------------------------

_VALID_PROMOTION_OCCASIONS = enum_names("PromotionExtensionOccasionEnum")
_VALID_DISCOUNT_MODIFIERS = enum_names("PromotionExtensionDiscountModifierEnum")

_PROMOTION_TARGET_MAX = 20
_PROMOTION_CODE_MAX = 15


def _validate_promotion_inputs(
    *,
    promotion_target: str,
    final_url: str,
    money_off: float,
    percent_off: float,
    currency_code: str,
    promotion_code: str,
    orders_over_amount: float,
    occasion: str,
    discount_modifier: str,
    language_code: str,
    start_date: str,
    end_date: str,
    redemption_start_date: str,
    redemption_end_date: str,
) -> tuple[dict, list[str], list[str]]:
    """Validate every PromotionAsset field. Returns (normalized, errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    target = (promotion_target or "").strip()
    url = (final_url or "").strip()

    if not target:
        errors.append("promotion_target is required")
    elif len(target) > _PROMOTION_TARGET_MAX:
        errors.append(
            f"promotion_target '{target}' is {len(target)} chars "
            f"(max {_PROMOTION_TARGET_MAX})"
        )
    if not url:
        errors.append("final_url is required")

    money = _as_amount(money_off, "money_off", errors)
    percent = _as_amount(percent_off, "percent_off", errors)
    if money > 0 and percent > 0:
        errors.append("Specify exactly one of money_off or percent_off, not both")
    elif money <= 0 and percent <= 0:
        errors.append("One of money_off or percent_off is required (must be > 0)")
    if percent > 100:
        errors.append(f"percent_off must be in (0, 100]; got {percent_off}")

    currency = (currency_code or "USD").strip().upper()
    if not _CURRENCY_RE.match(currency):
        errors.append(f"currency_code '{currency_code}' must be a 3-letter ISO 4217 code")

    code = (promotion_code or "").strip()
    if len(code) > _PROMOTION_CODE_MAX:
        errors.append(
            f"promotion_code '{code}' is {len(code)} chars (max {_PROMOTION_CODE_MAX})"
        )
    orders_over = _as_amount(orders_over_amount, "orders_over_amount", errors)
    if code and orders_over > 0:
        errors.append(
            "promotion_code and orders_over_amount are mutually exclusive "
            "(PromotionAsset.promotion_trigger is a oneof)"
        )

    occ = (occasion or "").strip().upper()
    if occ and occ not in _VALID_PROMOTION_OCCASIONS:
        errors.append(
            f"occasion '{occ}' invalid; valid values: {sorted(_VALID_PROMOTION_OCCASIONS)}"
        )
    modifier = (discount_modifier or "").strip().upper()
    if modifier and modifier not in _VALID_DISCOUNT_MODIFIERS:
        errors.append(
            f"discount_modifier '{modifier}' invalid; valid: "
            f"{sorted(_VALID_DISCOUNT_MODIFIERS)} (or empty for none)"
        )

    dates = {
        "start_date": (start_date or "").strip(),
        "end_date": (end_date or "").strip(),
        "redemption_start_date": (redemption_start_date or "").strip(),
        "redemption_end_date": (redemption_end_date or "").strip(),
    }
    for label, value in dates.items():
        if value and not _is_iso_date(value):
            errors.append(f"{label} '{value}' must be YYYY-MM-DD")
    for start, end in (
        ("start_date", "end_date"),
        ("redemption_start_date", "redemption_end_date"),
    ):
        if (
            dates[start] and dates[end]
            and _is_iso_date(dates[start]) and _is_iso_date(dates[end])
            and dates[end] < dates[start]
        ):
            errors.append(f"{end} ({dates[end]}) is before {start} ({dates[start]})")

    if errors:
        return {}, errors, warnings

    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    if dates["end_date"] and dates["end_date"] < today:
        warnings.append(
            f"end_date {dates['end_date']} is in the past — the promotion would never show"
        )

    url_errors, url_warnings = _url_warnings_or_errors([url])
    if url_errors:
        return {}, url_errors, warnings
    warnings.extend(url_warnings)

    normalized = {
        "promotion_target": target,
        "final_url": url,
        "money_off": money if money > 0 else 0.0,
        "percent_off": percent if percent > 0 else 0.0,
        "currency_code": currency,
        "promotion_code": code,
        "orders_over_amount": orders_over,
        "occasion": occ,
        "discount_modifier": modifier,
        "language_code": (language_code or "en").strip().lower(),
        **dates,
    }
    return normalized, [], warnings


def _promotion_draft(
    config: AdLoopConfig,
    operation: str,
    *,
    customer_id: str,
    scope: str,
    campaign_id: str,
    ad_group_id: str,
    promo_fields: dict,
) -> tuple[dict | None, dict, list[str]]:
    """Shared front half of the promotion drafts.

    Returns ``(error, info, warnings)``: ``error`` is a ready error response or
    None; ``info`` is the resolved scope plus the normalized ``promotion``.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}, {}, []

    scope_info, scope_errors = _resolve_asset_link_scope(
        scope, campaign_id, ad_group_id, customer_id
    )
    normalized, errors, warnings = _validate_promotion_inputs(**promo_fields)
    errors = scope_errors + errors
    if errors:
        return {"error": "Validation failed", "details": errors}, {}, []
    scope_info["promotion"] = normalized
    return None, scope_info, warnings


def draft_promotion(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    scope: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    promotion_target: str = "",
    final_url: str = "",
    money_off: float = 0,
    percent_off: float = 0,
    currency_code: str = "USD",
    promotion_code: str = "",
    orders_over_amount: float = 0,
    occasion: str = "",
    discount_modifier: str = "",
    language_code: str = "en",
    start_date: str = "",
    end_date: str = "",
    redemption_start_date: str = "",
    redemption_end_date: str = "",
) -> dict:
    """Draft a PromotionAsset linked at ad-group, campaign or account level."""
    from adloop.safety.preview import ChangePlan, store_plan

    error, info, warnings = _promotion_draft(
        config,
        "create_promotion",
        customer_id=customer_id,
        scope=scope,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        promo_fields=_promo_fields(locals()),
    )
    if error:
        return error

    plan = ChangePlan(
        operation="create_promotion",
        entity_type=info.pop("entity_type"),
        entity_id=info.pop("entity_id"),
        customer_id=customer_id,
        changes=info,
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def _promo_fields(scope_vars: dict) -> dict:
    """Pick the PromotionAsset fields out of a draft function's locals()."""
    return {
        key: scope_vars[key]
        for key in (
            "promotion_target", "final_url", "money_off", "percent_off",
            "currency_code", "promotion_code", "orders_over_amount", "occasion",
            "discount_modifier", "language_code", "start_date", "end_date",
            "redemption_start_date", "redemption_end_date",
        )
    }


def _promotion_link_query(info: dict, asset_id: str) -> str:
    """GAQL for the live (non-removed) PROMOTION link of ``asset_id`` at a scope.

    Every id interpolated here was checked numeric by the draft.
    """
    scope = info["scope"]
    if scope == "ad_group":
        return (
            "SELECT ad_group_asset.resource_name, asset.promotion_asset.promotion_target "
            "FROM ad_group_asset "
            f"WHERE asset.id = {asset_id} AND ad_group.id = {info['ad_group_id']} "
            "AND ad_group_asset.field_type = 'PROMOTION' "
            "AND ad_group_asset.status != 'REMOVED'"
        )
    if scope == "campaign":
        return (
            "SELECT campaign_asset.resource_name, asset.promotion_asset.promotion_target "
            "FROM campaign_asset "
            f"WHERE asset.id = {asset_id} AND campaign.id = {info['campaign_id']} "
            "AND campaign_asset.field_type = 'PROMOTION' "
            "AND campaign_asset.status != 'REMOVED'"
        )
    return (
        "SELECT customer_asset.resource_name, asset.promotion_asset.promotion_target "
        "FROM customer_asset "
        f"WHERE asset.id = {asset_id} "
        "AND customer_asset.field_type = 'PROMOTION' "
        "AND customer_asset.status != 'REMOVED'"
    )


def update_promotion(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    scope: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    promotion_target: str = "",
    final_url: str = "",
    money_off: float = 0,
    percent_off: float = 0,
    currency_code: str = "USD",
    promotion_code: str = "",
    orders_over_amount: float = 0,
    occasion: str = "",
    discount_modifier: str = "",
    language_code: str = "en",
    start_date: str = "",
    end_date: str = "",
    redemption_start_date: str = "",
    redemption_end_date: str = "",
) -> dict:
    """Draft replacing a linked PromotionAsset with a new one (a swap)."""
    from adloop.ads.gaql import execute_query
    from adloop.safety.preview import ChangePlan, store_plan

    asset_id = str(asset_id or "").strip()
    if not asset_id:
        return {"error": "asset_id is required (the PromotionAsset being replaced)"}
    if not asset_id.isdigit():
        return {"error": "asset_id must be a numeric ID"}

    error, info, warnings = _promotion_draft(
        config,
        "update_promotion",
        customer_id=customer_id,
        scope=scope,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        promo_fields=_promo_fields(locals()),
    )
    if error:
        return error

    rows = execute_query(config, customer_id, _promotion_link_query(info, asset_id))
    if not rows:
        where = {
            "ad_group": f"ad group {info['ad_group_id']}",
            "campaign": f"campaign {info['campaign_id']}",
            "account": "the account (CustomerAsset)",
        }[info["scope"]]
        return {
            "error": (
                f"Asset {asset_id} has no active PROMOTION link on {where}; "
                "nothing to replace."
            ),
            "details": [
                (
                    "Check the asset id and the level it is linked at "
                    "(ad_group_asset / campaign_asset / customer_asset)."
                ),
            ],
        }
    link_field = _LINK_RESOURCE[info["scope"]]
    info["old_asset_id"] = asset_id
    info["old_link"] = rows[0].get(f"{link_field}.resource_name", "")
    info["old_promotion_target"] = rows[0].get("asset.promotion_asset.promotion_target", "")

    warnings = [
        (
            "Swap: one request creates the new promotion, links it and removes "
            "the old link. The old asset itself stays in the account, unlinked "
            "(Google Ads assets cannot be deleted)."
        ),
        *warnings,
    ]
    plan = ChangePlan(
        operation="update_promotion",
        entity_type=info.pop("entity_type"),
        entity_id=info.pop("entity_id"),
        customer_id=customer_id,
        changes=info,
        # The old promotion link is removed; that part cannot be undone.
        requires_double_confirm=True,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def _populate_promotion_asset(client: object, asset: object, promo: dict) -> None:
    """Fill an Asset proto with PromotionAsset fields from a normalized dict."""
    p = asset.promotion_asset
    p.promotion_target = promo["promotion_target"]
    currency = promo.get("currency_code") or "USD"
    if promo.get("money_off"):
        p.money_amount_off.amount_micros = _micros(promo["money_off"])
        p.money_amount_off.currency_code = currency
    elif promo.get("percent_off"):
        # PromotionAsset.percent_off: 1,000,000 = 100%, so 10% -> 100,000.
        p.percent_off = round(float(promo["percent_off"]) * 10_000)

    if promo.get("promotion_code"):
        p.promotion_code = promo["promotion_code"]
    if promo.get("orders_over_amount"):
        p.orders_over_amount.amount_micros = _micros(promo["orders_over_amount"])
        p.orders_over_amount.currency_code = currency
    if promo.get("occasion"):
        p.occasion = getattr(client.enums.PromotionExtensionOccasionEnum, promo["occasion"])
    if promo.get("discount_modifier"):
        p.discount_modifier = getattr(
            client.enums.PromotionExtensionDiscountModifierEnum, promo["discount_modifier"]
        )

    p.language_code = promo.get("language_code") or "en"
    for field in ("start_date", "end_date", "redemption_start_date", "redemption_end_date"):
        if promo.get(field):
            setattr(p, field, promo[field])
    asset.final_urls.append(promo["final_url"])


def _apply_create_promotion(client: object, cid: str, changes: dict) -> dict:
    """Create a PromotionAsset and link it at the plan's scope."""
    return _apply_create_linked_asset(
        client,
        cid,
        changes,
        client.enums.AssetFieldTypeEnum.PROMOTION,
        lambda asset: _populate_promotion_asset(client, asset, changes["promotion"]),
    )


def _apply_update_promotion(client: object, cid: str, changes: dict) -> dict:
    """Swap a PromotionAsset in one Mutate: create new, link new, unlink old.

    The old link is re-read first. If it is gone (removed since the draft),
    nothing is sent: adding the new promotion without removing the old one
    would leave both serving.
    """
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    scope = changes["scope"]
    old_asset_id = str(changes["old_asset_id"])
    if not old_asset_id.isdigit():
        raise ValueError("old_asset_id must be a numeric ID")

    link_field = _LINK_RESOURCE[scope]
    old_link = ""
    for row in googleads_service.search(
        customer_id=cid, query=_promotion_link_query(changes, old_asset_id)
    ):
        old_link = getattr(row, link_field).resource_name
        break
    if not old_link:
        raise ValueError(
            f"Asset {old_asset_id} no longer has an active PROMOTION link at "
            f"{scope} level; nothing was sent. Draft the update again."
        )

    temp_asset = asset_service.asset_path(cid, "-1")
    create_op = client.get_type("MutateOperation")
    asset = create_op.asset_operation.create
    asset.resource_name = temp_asset
    _populate_promotion_asset(client, asset, changes["promotion"])

    field_type = client.enums.AssetFieldTypeEnum.PROMOTION
    link_op = _link_create_operation(client, cid, changes, temp_asset, field_type)

    unlink_op = client.get_type("MutateOperation")
    getattr(unlink_op, f"{link_field}_operation").remove = old_link

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=[create_op, link_op, unlink_op]
    )
    responses = list(response.mutate_operation_responses)
    return {
        "scope": scope,
        "new_asset": responses[0].asset_result.resource_name,
        "new_link": _link_result(responses[1], scope),
        "old_link_removed": old_link,
    }


# ---------------------------------------------------------------------------
# Price assets
# ---------------------------------------------------------------------------

_VALID_PRICE_TYPES = enum_names("PriceExtensionTypeEnum")
_VALID_PRICE_QUALIFIERS = enum_names("PriceExtensionPriceQualifierEnum")
_VALID_PRICE_UNITS = enum_names("PriceExtensionPriceUnitEnum")

# Google Ads takes 3-8 offerings per price asset; header and description are
# each capped at 25 characters.
_PRICE_OFFERING_MIN = 3
_PRICE_OFFERING_MAX = 8
_PRICE_TEXT_MAX = 25


def _validate_price_inputs(
    *,
    price_type: str,
    price_qualifier: str,
    language_code: str,
    currency_code: str,
    offerings: list[dict] | None,
) -> tuple[dict, list[str], list[str]]:
    """Validate every PriceAsset field. Returns (normalized, errors, warnings)."""
    errors: list[str] = []

    ptype = (price_type or "").strip().upper()
    if not ptype:
        errors.append("price_type is required (e.g. SERVICES, BRANDS, EVENTS)")
    elif ptype not in _VALID_PRICE_TYPES:
        errors.append(
            f"price_type '{ptype}' invalid; valid values: {sorted(_VALID_PRICE_TYPES)}"
        )

    qualifier = (price_qualifier or "").strip().upper()
    if qualifier and qualifier not in _VALID_PRICE_QUALIFIERS:
        errors.append(
            f"price_qualifier '{qualifier}' invalid; valid: "
            f"{sorted(_VALID_PRICE_QUALIFIERS)} (or empty for none)"
        )

    currency = (currency_code or "USD").strip().upper()
    if not _CURRENCY_RE.match(currency):
        errors.append(f"currency_code '{currency_code}' must be a 3-letter ISO 4217 code")

    rows = offerings or []
    if not (_PRICE_OFFERING_MIN <= len(rows) <= _PRICE_OFFERING_MAX):
        errors.append(
            f"a price asset needs {_PRICE_OFFERING_MIN}-{_PRICE_OFFERING_MAX} "
            f"offerings; got {len(rows)}"
        )

    normalized_offerings: list[dict] = []
    seen_headers: set[str] = set()
    urls: list[str] = []
    for i, row in enumerate(rows):
        label = f"offering[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{label}: must be an object with header, description, price, final_url")
            continue
        header = str(row.get("header") or "").strip()
        description = str(row.get("description") or "").strip()
        url = str(row.get("final_url") or "").strip()
        mobile_url = str(row.get("final_mobile_url") or "").strip()
        unit = str(row.get("unit") or "").strip().upper()

        for name, value in (("header", header), ("description", description)):
            if not value:
                errors.append(f"{label}: {name} is required")
            elif len(value) > _PRICE_TEXT_MAX:
                errors.append(
                    f"{label}: {name} '{value}' is {len(value)} chars (max {_PRICE_TEXT_MAX})"
                )
        # Google rejects a price asset whose offerings share a header.
        if header and header.lower() in seen_headers:
            errors.append(f"{label}: duplicate header '{header}'")
        seen_headers.add(header.lower())

        price = _as_amount(row.get("price"), f"{label}: price", errors)
        if price <= 0:
            errors.append(f"{label}: price must be > 0")

        if not url:
            errors.append(f"{label}: final_url is required")
        urls.extend(u for u in (url, mobile_url) if u)

        if unit and unit not in _VALID_PRICE_UNITS:
            errors.append(
                f"{label}: unit '{unit}' invalid; valid: "
                f"{sorted(_VALID_PRICE_UNITS)} (or empty for none)"
            )

        normalized_offerings.append(
            {
                "header": header,
                "description": description,
                "price": price,
                "final_url": url,
                "final_mobile_url": mobile_url,
                "unit": unit,
            }
        )

    if errors:
        return {}, errors, []

    url_errors, warnings = _url_warnings_or_errors(urls)
    if url_errors:
        return {}, url_errors, []

    normalized = {
        "price_type": ptype,
        "price_qualifier": qualifier,
        "language_code": (language_code or "en").strip().lower(),
        "currency_code": currency,
        "offerings": normalized_offerings,
    }
    return normalized, [], warnings


def draft_price_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    scope: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    price_type: str = "SERVICES",
    price_qualifier: str = "FROM",
    language_code: str = "en",
    currency_code: str = "USD",
    offerings: list[dict] | None = None,
) -> dict:
    """Draft a PriceAsset (3-8 offerings) linked at ad-group, campaign or account level."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_price_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    info, scope_errors = _resolve_asset_link_scope(
        scope, campaign_id, ad_group_id, customer_id
    )
    normalized, errors, warnings = _validate_price_inputs(
        price_type=price_type,
        price_qualifier=price_qualifier,
        language_code=language_code,
        currency_code=currency_code,
        offerings=offerings,
    )
    errors = scope_errors + errors
    if errors:
        return {"error": "Validation failed", "details": errors}

    info["price"] = normalized
    plan = ChangePlan(
        operation="create_price_asset",
        entity_type=info.pop("entity_type"),
        entity_id=info.pop("entity_id"),
        customer_id=customer_id,
        changes=info,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        (
            "Google disapproves price assets whose prices differ from the "
            "landing page; Google's review checks each offering's price "
            "against its final_url, this draft does not."
        ),
        *warnings,
    ]
    return preview


def _populate_price_asset(client: object, asset: object, price: dict) -> None:
    """Fill an Asset proto with PriceAsset fields from a normalized dict."""
    p = asset.price_asset
    p.type_ = getattr(client.enums.PriceExtensionTypeEnum, price["price_type"])
    if price.get("price_qualifier"):
        p.price_qualifier = getattr(
            client.enums.PriceExtensionPriceQualifierEnum, price["price_qualifier"]
        )
    p.language_code = price.get("language_code") or "en"

    currency = price.get("currency_code") or "USD"
    for row in price["offerings"]:
        offering = client.get_type("PriceOffering")
        offering.header = row["header"]
        offering.description = row["description"]
        offering.price.amount_micros = _micros(row["price"])
        offering.price.currency_code = currency
        offering.final_url = row["final_url"]
        if row.get("final_mobile_url"):
            offering.final_mobile_url = row["final_mobile_url"]
        if row.get("unit"):
            offering.unit = getattr(client.enums.PriceExtensionPriceUnitEnum, row["unit"])
        p.price_offerings.append(offering)


def _apply_create_price_asset(client: object, cid: str, changes: dict) -> dict:
    """Create a PriceAsset and link it at the plan's scope."""
    return _apply_create_linked_asset(
        client,
        cid,
        changes,
        client.enums.AssetFieldTypeEnum.PRICE,
        lambda asset: _populate_price_asset(client, asset, changes["price"]),
    )
