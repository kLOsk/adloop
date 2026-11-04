"""Performance Max builders: campaign + asset group, behind draft -> confirm_and_apply.

Both drafts only create a ChangePlan. Nothing touches Google Ads until
``confirm_and_apply``. A PMax campaign is ALWAYS created PAUSED; no tool here
accepts a status argument, and the apply step refuses anything but PAUSED.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

PMAX_BIDDING = "MAXIMIZE_CONVERSIONS"

# Google's PMax asset group limits (text).
HEADLINE_MAX_LEN, HEADLINE_MIN, HEADLINE_MAX = 30, 3, 15
LONG_HEADLINE_MAX_LEN, LONG_HEADLINE_MIN, LONG_HEADLINE_MAX = 90, 1, 5
DESCRIPTION_MAX_LEN, DESCRIPTION_MIN, DESCRIPTION_MAX = 90, 2, 5
DESCRIPTION_SHORT_LEN = 60  # at least one description must be this short
BUSINESS_NAME_MAX_LEN = 25
MAX_FILE_BYTES = 5120 * 1024

# kind -> (ratio w/h, min_w, min_h, min_count, max_count, asset field type name)
IMAGE_SPECS = {
    "landscape": (1.91, 600, 314, 1, 20, "MARKETING_IMAGE"),
    "square": (1.0, 300, 300, 1, 20, "SQUARE_MARKETING_IMAGE"),
    "logo": (1.0, 128, 128, 1, 5, "LOGO"),
    "landscape_logo": (4.0, 512, 128, 0, 5, "LANDSCAPE_LOGO"),
}
ASPECT_TOLERANCE = 0.02  # 2 percent

# Auto-asset features we opt OUT of (final URL expansion included). The API rejects
# opt-out for the three video-generation types (ENUM_VALUE_NOT_PERMITTED); video
# auto-generation is not controllable here and is reported as a known gap.
OPT_OUT_AUTOMATIONS = (
    "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
    "TEXT_ASSET_AUTOMATION",
    "GENERATE_IMAGE_ENHANCEMENT",
    "GENERATE_IMAGE_EXTRACTION",
    "GENERATE_DESIGN_VERSIONS_FOR_IMAGES",
    "GENERATE_ENHANCED_YOUTUBE_VIDEOS",
)

VALID_CTAS = {
    "LEARN_MORE", "GET_QUOTE", "APPLY_NOW", "SIGN_UP", "CONTACT_US",
    "SUBSCRIBE", "DOWNLOAD", "BOOK_NOW", "SHOP_NOW", "ORDER_NOW",
    "SEE_MORE", "BUY_NOW", "DONATE_NOW", "REGISTER_NOW", "GET_STARTED",
    "VISIT_SITE", "INSTALL", "WATCH_NOW", "PLAY_NOW", "DOWNLOAD_NOW",
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_pmax_campaign(
    config: "AdLoopConfig",
    *,
    campaign_name: str,
    daily_budget: float,
    geo_target_ids: list[str] | None,
    language_ids: list[str] | None,
    brand_exclusions: list[str] | None,
) -> list[str]:
    errors: list[str] = []
    if not (campaign_name or "").strip():
        errors.append("campaign_name is required")
    if not daily_budget or daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")
    if not geo_target_ids:
        errors.append("geo_target_ids is required (geo target constant IDs)")
    else:
        for g in geo_target_ids:
            if not str(g).strip().isdigit():
                errors.append(f"geo target id '{g}' must be numeric")
    if not language_ids:
        errors.append("language_ids is required (e.g. ['1000'] for English)")
    else:
        for lg in language_ids:
            if not str(lg).strip().isdigit():
                errors.append(f"language id '{lg}' must be numeric")
    for b in brand_exclusions or []:
        if not str(b).strip():
            errors.append("brand_exclusions entries must be non-empty")
    return errors


def _text_list_errors(label, items, min_n, max_n, max_len) -> list[str]:
    errors = []
    items = items or []
    if len(items) < min_n:
        errors.append(f"{label}: need at least {min_n}, got {len(items)}")
    if len(items) > max_n:
        errors.append(f"{label}: at most {max_n} allowed, got {len(items)}")
    seen = set()
    for i, t in enumerate(items, 1):
        t = str(t)
        if not t.strip():
            errors.append(f"{label} #{i}: empty")
        elif len(t) > max_len:
            errors.append(f"{label} #{i}: {len(t)} chars, max {max_len} ({t!r})")
        if t.strip().lower() in seen:
            errors.append(f"{label} #{i}: duplicate ({t!r})")
        seen.add(t.strip().lower())
    return errors


def validate_image(kind: str, path_str: str) -> tuple[dict | None, list[str]]:
    """Validate one local image against the PMax spec for ``kind``. No upload."""
    from adloop.ads.write import _VALID_IMAGE_MIME_TYPES, _parse_image_metadata

    ratio, min_w, min_h, _mn, _mx, _field = IMAGE_SPECS[kind]
    try:
        meta = _parse_image_metadata(path_str)
    except ValueError as e:
        return None, [f"{kind} image {path_str}: {e}"]
    errors = []
    w, h = int(meta["width"]), int(meta["height"])
    label = f"{kind} image {Path(path_str).name}"
    if meta["mime_type"] not in _VALID_IMAGE_MIME_TYPES or meta["mime_type"] == "image/gif":
        errors.append(f"{label}: use PNG or JPEG, got {meta['mime_type']}")
    if w < min_w or h < min_h:
        errors.append(f"{label}: {w}x{h} is below the {min_w}x{min_h} minimum")
    if h and abs((w / h) - ratio) / ratio > ASPECT_TOLERANCE:
        errors.append(
            f"{label}: aspect {w}:{h} = {w / h:.3f}, needs {ratio} (+/-{int(ASPECT_TOLERANCE * 100)}%)"
        )
    size = Path(meta["path"]).stat().st_size
    if size > MAX_FILE_BYTES:
        errors.append(f"{label}: {size // 1024} KB exceeds the 5120 KB limit")
    meta["kind"] = kind
    return meta, errors


def validate_pmax_asset_group(
    *,
    campaign_id: str,
    asset_group_name: str,
    final_url: str,
    headlines,
    long_headlines,
    descriptions,
    business_name: str,
    images: dict | None,
    call_to_action: str,
    search_themes,
    audience_id: str,
) -> tuple[list[dict], list[str]]:
    """Return (validated image list, ALL errors). Runs before any upload."""
    errors: list[str] = []
    if not str(campaign_id or "").strip().isdigit():
        errors.append("campaign_id is required and must be numeric")
    if not (asset_group_name or "").strip():
        errors.append("asset_group_name is required")
    if not (final_url or "").startswith(("http://", "https://")):
        errors.append("final_url must start with http:// or https://")
    errors += _text_list_errors("headlines", headlines, HEADLINE_MIN, HEADLINE_MAX, HEADLINE_MAX_LEN)
    errors += _text_list_errors(
        "long_headlines", long_headlines, LONG_HEADLINE_MIN, LONG_HEADLINE_MAX, LONG_HEADLINE_MAX_LEN
    )
    errors += _text_list_errors(
        "descriptions", descriptions, DESCRIPTION_MIN, DESCRIPTION_MAX, DESCRIPTION_MAX_LEN
    )
    if descriptions and not any(len(str(d)) <= DESCRIPTION_SHORT_LEN for d in descriptions):
        errors.append(
            f"descriptions: at least one must be {DESCRIPTION_SHORT_LEN} chars or fewer"
        )
    bn = (business_name or "").strip()
    if not bn:
        errors.append("business_name is required")
    elif len(bn) > BUSINESS_NAME_MAX_LEN:
        errors.append(f"business_name: {len(bn)} chars, max {BUSINESS_NAME_MAX_LEN}")
    if call_to_action and call_to_action.upper() not in VALID_CTAS:
        errors.append(f"call_to_action '{call_to_action}' not in {sorted(VALID_CTAS)}")
    for t in search_themes or []:
        if not str(t).strip() or len(str(t)) > 80:
            errors.append(f"search theme invalid (empty or over 80 chars): {t!r}")
    if len(search_themes or []) > 25:
        errors.append("search_themes: at most 25 allowed")
    if audience_id and not str(audience_id).strip().isdigit():
        errors.append("audience_id must be a numeric audience ID")

    validated: list[dict] = []
    images = images or {}
    for kind in images:
        if kind not in IMAGE_SPECS:
            errors.append(f"unknown image kind '{kind}', use {sorted(IMAGE_SPECS)}")
    for kind, (_r, _w, _h, mn, mx, _f) in IMAGE_SPECS.items():
        paths = list(images.get(kind) or [])
        if len(paths) < mn:
            errors.append(f"images.{kind}: need at least {mn}, got {len(paths)}")
        if len(paths) > mx:
            errors.append(f"images.{kind}: at most {mx} allowed, got {len(paths)}")
        for p in paths:
            meta, errs = validate_image(kind, p)
            errors += errs
            if meta and not errs:
                validated.append(meta)
    return validated, errors


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


def draft_pmax_campaign(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    campaign_name: str = "",
    daily_budget: float = 0,
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    brand_exclusions: list[str] | None = None,
) -> dict:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation, check_budget_cap
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_pmax_campaign", config.safety)
        check_blocked_operation("create_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = validate_pmax_campaign(
        config,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        brand_exclusions=brand_exclusions,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}
    try:
        check_budget_cap(daily_budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    plan = ChangePlan(
        operation="create_pmax_campaign",
        entity_type="campaign",
        customer_id=customer_id,
        changes={
            "campaign_name": campaign_name.strip(),
            "daily_budget": daily_budget,
            "channel_type": "PERFORMANCE_MAX",
            "status": "PAUSED",
            "bidding_strategy": PMAX_BIDDING,
            "final_url_expansion": "OFF",
            "geo_target_type": "PRESENCE",
            "geo_target_ids": [str(g).strip() for g in geo_target_ids],
            "language_ids": [str(x).strip() for x in language_ids],
            "brand_exclusions": [b.strip() for b in (brand_exclusions or [])],
            "auto_asset_automation": "OPTED_OUT",
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_pmax_asset_group(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    campaign_id: str = "",
    asset_group_name: str = "",
    final_url: str = "",
    headlines: list[str] | None = None,
    long_headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    business_name: str = "",
    images: dict | None = None,
    call_to_action: str = "",
    search_themes: list[str] | None = None,
    audience_id: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_pmax_asset_group", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated, errors = validate_pmax_asset_group(
        campaign_id=campaign_id,
        asset_group_name=asset_group_name,
        final_url=final_url,
        headlines=headlines,
        long_headlines=long_headlines,
        descriptions=descriptions,
        business_name=business_name,
        images=images,
        call_to_action=call_to_action,
        search_themes=search_themes,
        audience_id=audience_id,
    )
    if path1 and len(path1) > 15:
        errors.append("path1 max 15 chars")
    if path2 and len(path2) > 15:
        errors.append("path2 max 15 chars")
    if errors:
        return {
            "error": "Validation failed: Google's PMax minimums are not met",
            "details": errors,
        }

    plan = ChangePlan(
        operation="create_pmax_asset_group",
        entity_type="asset_group",
        entity_id=str(campaign_id),
        customer_id=customer_id,
        changes={
            "campaign_id": str(campaign_id),
            "asset_group_name": asset_group_name.strip(),
            "final_url": final_url,
            "path1": path1,
            "path2": path2,
            "headlines": list(headlines),
            "long_headlines": list(long_headlines),
            "descriptions": list(descriptions),
            "business_name": business_name.strip(),
            "images": validated,
            "call_to_action": (call_to_action or "").upper(),
            "search_themes": list(search_themes or []),
            "audience_id": str(audience_id or ""),
            "status": "PAUSED",
        },
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# Apply: operation builders (shared by real apply and validate_only)
# ---------------------------------------------------------------------------


def _resolve_brand_ids(client, cid: str, names: list[str]) -> tuple[list[dict], list[str]]:
    """Resolve brand names via BrandSuggestionService (read-only)."""
    resolved, unresolved = [], []
    if not names:
        return resolved, unresolved
    svc = client.get_service("BrandSuggestionService")
    for name in names:
        try:
            resp = svc.suggest_brands(customer_id=cid, brand_prefix=name)
        except Exception:
            unresolved.append(name)
            continue
        match = None
        for b in resp.brands:
            if b.name.strip().lower() == name.strip().lower():
                match = b
                break
        if match is None:
            unresolved.append(name)
        else:
            resolved.append({"name": match.name, "id": match.id, "urls": list(match.urls)})
    return resolved, unresolved


def build_campaign_ops(client, cid: str, changes: dict, brands: list[dict] | None = None):
    """Return (operations, labels). Campaign temp id is -2."""
    if changes.get("status", "PAUSED") != "PAUSED":
        raise ValueError("PMax campaigns can only be created PAUSED")
    ops, labels = [], []
    budget_service = client.get_service("CampaignBudgetService")
    campaign_service = client.get_service("CampaignService")
    ga = client.get_service("GoogleAdsService")

    op = client.get_type("MutateOperation")
    b = op.campaign_budget_operation.create
    b.resource_name = budget_service.campaign_budget_path(cid, "-1")
    b.name = f"Budget - {changes['campaign_name']}"
    b.amount_micros = int(round(changes["daily_budget"] * 1_000_000))
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False
    ops.append(op); labels.append("campaign_budget")

    op = client.get_type("MutateOperation")
    c = op.campaign_operation.create
    c.resource_name = campaign_service.campaign_path(cid, "-2")
    c.name = changes["campaign_name"]
    c.campaign_budget = budget_service.campaign_budget_path(cid, "-1")
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    # Maximize Conversions with NO target CPA: touch the oneof without a target.
    c.maximize_conversions.target_cpa_micros = 0
    c.geo_target_type_setting.positive_geo_target_type = (
        client.enums.PositiveGeoTargetTypeEnum.PRESENCE
    )
    # Brand Guidelines (default ON in v23) forces campaign-level business name and
    # logo assets. We attach those at the asset group, so keep it OFF.
    c.brand_guidelines_enabled = False
    c.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    for name in OPT_OUT_AUTOMATIONS:
        s = client.get_type("Campaign").AssetAutomationSetting()
        s.asset_automation_type = getattr(client.enums.AssetAutomationTypeEnum, name)
        s.asset_automation_status = client.enums.AssetAutomationStatusEnum.OPTED_OUT
        c.asset_automation_settings.append(s)
    ops.append(op); labels.append("campaign")

    for geo_id in changes.get("geo_target_ids") or []:
        op = client.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_service.campaign_path(cid, "-2")
        cc.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        ops.append(op); labels.append("geo_target")

    for lang_id in changes.get("language_ids") or []:
        op = client.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_service.campaign_path(cid, "-2")
        cc.language.language_constant = f"languageConstants/{lang_id}"
        ops.append(op); labels.append("language_target")

    if brands:
        shared_service = client.get_service("SharedSetService")
        op = client.get_type("MutateOperation")
        ss = op.shared_set_operation.create
        ss.resource_name = shared_service.shared_set_path(cid, "-3")
        ss.name = f"Brand exclusions - {changes['campaign_name']}"
        ss.type_ = client.enums.SharedSetTypeEnum.BRANDS
        ops.append(op); labels.append("brand_shared_set")
        for br in brands:
            op = client.get_type("MutateOperation")
            sc = op.shared_criterion_operation.create
            sc.shared_set = shared_service.shared_set_path(cid, "-3")
            sc.brand.entity_id = br["id"]
            ops.append(op); labels.append("brand_criterion")
        # PMax brand exclusion attaches as a NEGATIVE brand_list campaign criterion.
        op = client.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_service.campaign_path(cid, "-2")
        cc.negative = True
        cc.brand_list.shared_set = shared_service.shared_set_path(cid, "-3")
        ops.append(op); labels.append("campaign_brand_list")
    return ops, labels


def build_asset_group_ops(client, cid: str, changes: dict, campaign_ref: str | None = None):
    """Return (operations, labels). Asset group temp id is -100."""
    if changes.get("status", "PAUSED") != "PAUSED":
        raise ValueError("PMax asset groups can only be created PAUSED")
    ops, labels = [], []
    asset_service = client.get_service("AssetService")
    campaign_service = client.get_service("CampaignService")
    ag_service = client.get_service("AssetGroupService")
    campaign_rn = campaign_ref or campaign_service.campaign_path(cid, changes["campaign_id"])
    ag_rn = ag_service.asset_group_path(cid, "-100")

    # (temp asset id, field type) links, created after the asset group.
    links: list[tuple[int, str]] = []
    counter = [0]

    def new_asset(populate, field_type: str) -> None:
        counter[0] += 1
        tid = -(1000 + counter[0])  # unique across the whole request
        op = client.get_type("MutateOperation")
        a = op.asset_operation.create
        a.resource_name = asset_service.asset_path(cid, str(tid))
        populate(a)
        ops.append(op); labels.append("asset")
        links.append((tid, field_type))

    def text(value):
        def _p(a):
            a.text_asset.text = value
        return _p

    for h in changes["headlines"]:
        new_asset(text(h), "HEADLINE")
    for h in changes["long_headlines"]:
        new_asset(text(h), "LONG_HEADLINE")
    for d in changes["descriptions"]:
        new_asset(text(d), "DESCRIPTION")
    new_asset(text(changes["business_name"]), "BUSINESS_NAME")

    cta = changes.get("call_to_action")
    if cta:
        def _cta(a):
            a.name = f"AdLoop CTA {cta}"
            a.call_to_action_asset.call_to_action = getattr(
                client.enums.CallToActionTypeEnum, cta
            )
        new_asset(_cta, "CALL_TO_ACTION_SELECTION")

    from adloop.ads.write import _VALID_IMAGE_MIME_TYPES

    for img in changes.get("images") or []:
        field = IMAGE_SPECS[img["kind"]][5]

        def _img(a, img=img):
            data = Path(img["path"]).read_bytes()
            a.name = img["name"]
            a.type_ = client.enums.AssetTypeEnum.IMAGE
            a.image_asset.data = data
            a.image_asset.mime_type = getattr(
                client.enums.MimeTypeEnum, _VALID_IMAGE_MIME_TYPES[img["mime_type"]]
            )
            a.image_asset.full_size.width_pixels = int(img["width"])
            a.image_asset.full_size.height_pixels = int(img["height"])
        new_asset(_img, field)

    op = client.get_type("MutateOperation")
    ag = op.asset_group_operation.create
    ag.resource_name = ag_rn
    ag.name = changes["asset_group_name"]
    ag.campaign = campaign_rn
    ag.final_urls.append(changes["final_url"])
    ag.status = client.enums.AssetGroupStatusEnum.PAUSED
    if changes.get("path1"):
        ag.path1 = changes["path1"]
    if changes.get("path2"):
        ag.path2 = changes["path2"]
    # Asset group must precede its links but follow nothing: insert before links.
    ops.append(op); labels.append("asset_group")

    for tid, field_type in links:
        op = client.get_type("MutateOperation")
        aga = op.asset_group_asset_operation.create
        aga.asset_group = ag_rn
        aga.asset = asset_service.asset_path(cid, str(tid))
        aga.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)
        ops.append(op); labels.append("asset_group_asset")

    for theme in changes.get("search_themes") or []:
        op = client.get_type("MutateOperation")
        sig = op.asset_group_signal_operation.create
        sig.asset_group = ag_rn
        sig.search_theme.text = theme
        ops.append(op); labels.append("search_theme")
    if changes.get("audience_id"):
        op = client.get_type("MutateOperation")
        sig = op.asset_group_signal_operation.create
        sig.asset_group = ag_rn
        sig.audience.audience = f"customers/{cid}/audiences/{changes['audience_id']}"
        ops.append(op); labels.append("audience_signal")
    return ops, labels


def _send(client, cid: str, ops: list, validate_only: bool):
    svc = client.get_service("GoogleAdsService")
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend(ops)
    request.validate_only = bool(validate_only)
    return svc.mutate(request=request)


def _collect(resp, labels: list[str]) -> dict:
    from adloop.ads.write import _extract_resource_name

    out: dict = {}
    for label, r in zip(labels, resp.mutate_operation_responses):
        rn = _extract_resource_name(r)
        if rn:
            out.setdefault(label, []).append(rn)
    return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


def apply_create_pmax_campaign(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    brands, unresolved = _resolve_brand_ids(client, cid, changes.get("brand_exclusions") or [])
    ops, labels = build_campaign_ops(client, cid, changes, brands)
    resp = _send(client, cid, ops, validate_only)
    result = {"validate_only": validate_only, "operations": len(ops)}
    if not validate_only:
        result.update(_collect(resp, labels))
    result["brand_exclusions_resolved"] = [b["name"] for b in brands]
    if unresolved:
        result["brand_exclusions_unresolved"] = unresolved
    return result


def apply_create_pmax_asset_group(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    # Re-validate images from disk at apply time (files may have changed).
    for img in changes.get("images") or []:
        _meta, errs = validate_image(img["kind"], img["path"])
        if errs:
            raise ValueError("; ".join(errs))
    ops, labels = build_asset_group_ops(client, cid, changes)
    resp = _send(client, cid, ops, validate_only)
    result = {"validate_only": validate_only, "operations": len(ops)}
    if not validate_only:
        result.update(_collect(resp, labels))
    return result


URL_EXPANSION_TYPE = "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"


def draft_pmax_url_expansion(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    campaign_id: str = "",
    opted_in: bool = False,
) -> dict:
    """Draft setting Final URL expansion on a PMax campaign (OPTED_IN / OPTED_OUT)."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_pmax_url_expansion", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    if not str(campaign_id or "").strip().isdigit():
        return {"error": "Validation failed", "details": ["campaign_id must be numeric"]}
    status = "OPTED_IN" if opted_in else "OPTED_OUT"
    plan = ChangePlan(
        operation="update_pmax_url_expansion",
        entity_type="campaign",
        entity_id=str(campaign_id).strip(),
        customer_id=customer_id,
        changes={"campaign_id": str(campaign_id).strip(), "status": status},
    )
    store_plan(plan)
    return plan.to_preview()


def apply_update_pmax_url_expansion(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    """Set campaign.asset_automation_settings for the final URL expansion type.

    The repeated field is replaced wholesale, so the other settings are re-read
    from the live campaign and re-sent to avoid dropping them.
    """
    ga = client.get_service("GoogleAdsService")
    rn = client.get_service("CampaignService").campaign_path(cid, changes["campaign_id"])
    existing: dict = {}
    q = (
        "SELECT campaign.asset_automation_settings FROM campaign "
        f"WHERE campaign.id = {int(changes['campaign_id'])}"
    )
    for batch in ga.search_stream(customer_id=cid, query=q):
        for row in batch.results:
            for s in row.campaign.asset_automation_settings:
                existing[s.asset_automation_type.name] = s.asset_automation_status.name
    existing[URL_EXPANSION_TYPE] = changes["status"]

    op = client.get_type("MutateOperation")
    c = op.campaign_operation.update
    c.resource_name = rn
    for t, st in existing.items():
        if t in ("UNSPECIFIED", "UNKNOWN"):
            continue
        s = client.get_type("Campaign").AssetAutomationSetting()
        s.asset_automation_type = getattr(client.enums.AssetAutomationTypeEnum, t)
        s.asset_automation_status = getattr(client.enums.AssetAutomationStatusEnum, st)
        c.asset_automation_settings.append(s)
    op.campaign_operation.update_mask.paths.append("asset_automation_settings")
    _send(client, cid, [op], validate_only)
    return {"validate_only": validate_only, "campaign": rn, "status": changes["status"]}


# ---------------------------------------------------------------------------
# Single-asset add / remove inside an existing asset group
# ---------------------------------------------------------------------------

# field type -> (min count Google requires, max count, max text length or None)
ASSET_FIELD_LIMITS = {
    "HEADLINE": (HEADLINE_MIN, HEADLINE_MAX, HEADLINE_MAX_LEN),
    "LONG_HEADLINE": (LONG_HEADLINE_MIN, LONG_HEADLINE_MAX, LONG_HEADLINE_MAX_LEN),
    "DESCRIPTION": (DESCRIPTION_MIN, DESCRIPTION_MAX, DESCRIPTION_MAX_LEN),
    "BUSINESS_NAME": (1, 1, BUSINESS_NAME_MAX_LEN),
    "MARKETING_IMAGE": (1, 20, None),
    "SQUARE_MARKETING_IMAGE": (1, 20, None),
    "LOGO": (1, 5, None),
    "LANDSCAPE_LOGO": (0, 5, None),
}
TEXT_FIELDS = ("HEADLINE", "LONG_HEADLINE", "DESCRIPTION")
IMAGE_FIELDS = ("MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "LOGO", "LANDSCAPE_LOGO")


def _group_field_counts(config, customer_id: str, asset_group_id: str) -> dict[str, list[str]]:
    """Return {field_type: [asset ids]} for non-removed links of one asset group."""
    from adloop.ads.gaql import execute_query

    query = f"""
        SELECT asset_group_asset.field_type, asset_group_asset.asset,
               asset_group_asset.status
        FROM asset_group_asset
        WHERE asset_group.id = {asset_group_id}
          AND asset_group_asset.status != 'REMOVED'
    """
    out: dict[str, list[str]] = {}
    for row in execute_query(config, customer_id, query):
        ft = str(row.get("asset_group_asset.field_type", ""))
        aid = str(row.get("asset_group_asset.asset", "")).rsplit("/", 1)[-1]
        out.setdefault(ft, []).append(aid)
    return out


def check_asset_group_asset_removal(config, customer_id: str, entity_id: str) -> list[str]:
    """Errors if removing ``assetGroupId~assetId~fieldType`` breaks a Google minimum.

    Fails closed: if the group cannot be read, the removal is refused.
    """
    parts = str(entity_id).split("~")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        return [f"asset_group_asset entity_id must be 'assetGroupId~assetId~fieldType', got '{entity_id}'"]
    ag_id, asset_id, field = (p.strip() for p in parts)
    if not ag_id.isdigit() or not asset_id.isdigit():
        return ["asset_group_id and asset_id must be numeric"]
    field = field.upper()
    if field not in ASSET_FIELD_LIMITS:
        return [f"field_type '{field}' must be one of {sorted(ASSET_FIELD_LIMITS)}"]
    try:
        counts = _group_field_counts(config, customer_id, ag_id)
    except Exception as e:  # fail closed
        return [f"could not read asset group {ag_id} to check minimums: {e}"]
    linked = counts.get(field, [])
    if asset_id not in linked:
        return [f"asset {asset_id} is not linked to asset group {ag_id} as {field}"]
    minimum = ASSET_FIELD_LIMITS[field][0]
    if len(linked) - 1 < minimum:
        return [
            f"removing this {field} would leave {len(linked) - 1} in asset group {ag_id}; "
            f"Google requires at least {minimum}. Add the replacement first, then remove."
        ]
    return []


def validate_asset_group_additions(add: list[dict] | None) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    cleaned: list[dict] = []
    if not add:
        return [], ["add must be a non-empty list of {field_type, text | image_asset_id}"]
    for i, item in enumerate(add, 1):
        label = f"add #{i}"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be an object")
            continue
        field = str(item.get("field_type", "")).strip().upper()
        if field in TEXT_FIELDS:
            text = str(item.get("text") or "").strip()
            if item.get("image_asset_id"):
                errors.append(f"{label}: {field} takes text, not image_asset_id")
            max_len = ASSET_FIELD_LIMITS[field][2]
            if not text:
                errors.append(f"{label}: text is required for {field}")
            elif len(text) > max_len:
                errors.append(f"{label}: {len(text)} chars, max {max_len} for {field} ({text!r})")
            else:
                cleaned.append({"field_type": field, "text": text})
        elif field in IMAGE_FIELDS:
            aid = str(item.get("image_asset_id") or "").strip()
            if item.get("text"):
                errors.append(f"{label}: {field} takes image_asset_id, not text")
            if not aid.isdigit():
                errors.append(f"{label}: image_asset_id must be a numeric asset ID")
            else:
                cleaned.append({"field_type": field, "image_asset_id": aid})
        else:
            errors.append(
                f"{label}: field_type '{field}' must be one of {list(TEXT_FIELDS + IMAGE_FIELDS)}"
            )
    return cleaned, errors


def draft_pmax_asset_group_assets(
    config: "AdLoopConfig",
    *,
    customer_id: str = "",
    asset_group_id: str = "",
    add: list[dict] | None = None,
) -> dict:
    """Draft adding single assets to an existing PMax asset group."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_pmax_asset_group_assets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    errors: list[str] = []
    if not str(asset_group_id or "").strip().isdigit():
        errors.append("asset_group_id must be numeric")
    cleaned, add_errors = validate_asset_group_additions(add)
    errors.extend(add_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}
    # Cap check (best effort: if the group cannot be read, Google enforces it).
    try:
        counts = _group_field_counts(config, customer_id, str(asset_group_id).strip())
    except Exception:
        counts = None
    if counts is not None:
        adding: dict[str, int] = {}
        for item in cleaned:
            adding[item["field_type"]] = adding.get(item["field_type"], 0) + 1
        for field, n in adding.items():
            cap = ASSET_FIELD_LIMITS[field][1]
            have = len(counts.get(field, []))
            if have + n > cap:
                errors.append(
                    f"{field}: group already has {have}, adding {n} exceeds the max of {cap}. "
                    "Remove one first (remove_entity asset_group_asset)."
                )
        if errors:
            return {"error": "Validation failed", "details": errors}
    plan = ChangePlan(
        operation="add_pmax_asset_group_assets",
        entity_type="asset_group_asset",
        entity_id=str(asset_group_id).strip(),
        customer_id=customer_id,
        changes={"asset_group_id": str(asset_group_id).strip(), "add": cleaned},
    )
    store_plan(plan)
    return plan.to_preview()


def build_asset_group_asset_add_ops(client, cid: str, changes: dict):
    """Return (operations, labels): create text assets as needed, then link."""
    ops, labels = [], []
    asset_service = client.get_service("AssetService")
    ag_rn = client.get_service("AssetGroupService").asset_group_path(
        cid, changes["asset_group_id"]
    )
    links: list[tuple[str, str]] = []
    counter = 0
    for item in changes["add"]:
        if item.get("text"):
            counter += 1
            tid = str(-(2000 + counter))
            op = client.get_type("MutateOperation")
            a = op.asset_operation.create
            a.resource_name = asset_service.asset_path(cid, tid)
            a.text_asset.text = item["text"]
            ops.append(op); labels.append("asset")
            links.append((asset_service.asset_path(cid, tid), item["field_type"]))
        else:
            links.append(
                (asset_service.asset_path(cid, item["image_asset_id"]), item["field_type"])
            )
    for asset_rn, field in links:
        op = client.get_type("MutateOperation")
        aga = op.asset_group_asset_operation.create
        aga.asset_group = ag_rn
        aga.asset = asset_rn
        aga.field_type = getattr(client.enums.AssetFieldTypeEnum, field)
        ops.append(op); labels.append("asset_group_asset")
    return ops, labels


def apply_add_pmax_asset_group_assets(
    client, cid: str, changes: dict, validate_only: bool = False
) -> dict:
    ops, labels = build_asset_group_asset_add_ops(client, cid, changes)
    resp = _send(client, cid, ops, validate_only)
    result = {"validate_only": validate_only, "operations": len(ops)}
    if not validate_only:
        result.update(_collect(resp, labels))
    return result
