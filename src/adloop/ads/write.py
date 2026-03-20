"""Google Ads write tools — all behind the safety layer.

Every write tool returns a preview/plan. Nothing executes until
``confirm_and_apply`` is called with the plan ID.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


_STRUCTURED_SNIPPET_HEADERS = {
    "Amenities",
    "Brands",
    "Courses",
    "Degree programs",
    "Destinations",
    "Featured Hotels",
    "Insurance coverage",
    "Models",
    "Neighborhoods",
    "Services",
    "Shows",
    "Styles",
    "Types",
}

_VALID_IMAGE_MIME_TYPES = {
    "image/gif": "IMAGE_GIF",
    "image/jpeg": "IMAGE_JPEG",
    "image/png": "IMAGE_PNG",
}


# ---------------------------------------------------------------------------
# URL validation — verify URLs exist before creating ads/sitelinks
# ---------------------------------------------------------------------------


def _validate_urls(urls: list[str], timeout: int = 10) -> dict[str, str | None]:
    """Check that each URL returns a 2xx/3xx status.

    Returns a dict of {url: error_message_or_None}. None means the URL is fine.
    """
    import urllib.request
    import urllib.error

    results = {}
    for url in urls:
        if not url:
            continue
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "AdLoop-URLCheck/1.0")
            resp = urllib.request.urlopen(req, timeout=timeout)
            if resp.status >= 400:
                results[url] = f"HTTP {resp.status}"
            else:
                results[url] = None
        except urllib.error.HTTPError as e:
            if e.code == 405:
                # HEAD not allowed, try GET
                try:
                    req = urllib.request.Request(url, method="GET")
                    req.add_header("User-Agent", "AdLoop-URLCheck/1.0")
                    resp = urllib.request.urlopen(req, timeout=timeout)
                    if resp.status >= 400:
                        results[url] = f"HTTP {resp.status}"
                    else:
                        results[url] = None
                except Exception as e2:
                    results[url] = str(e2)
            else:
                results[url] = f"HTTP {e.code}"
        except Exception as e:
            results[url] = str(e)

    return results


def _normalize_display_network_setting(
    display_network_enabled: bool | None,
    display_expansion_enabled: bool | None,
) -> tuple[bool | None, list[str]]:
    """Normalize the deprecated alias to one canonical display network flag."""
    errors = []
    if (
        display_network_enabled is not None
        and display_expansion_enabled is not None
        and display_network_enabled != display_expansion_enabled
    ):
        errors.append(
            "display_network_enabled and display_expansion_enabled must match "
            "when both are provided"
        )
    if errors:
        return None, errors
    if display_network_enabled is not None:
        return display_network_enabled, []
    return display_expansion_enabled, []


def _parse_image_metadata(path_str: str) -> dict[str, object]:
    """Validate a local image file and return metadata used for asset creation."""
    path = Path(path_str).expanduser()
    if not path.exists():
        raise ValueError(f"Image file does not exist: {path_str}")
    if not path.is_file():
        raise ValueError(f"Image path is not a file: {path_str}")

    data = path.read_bytes()
    mime_type, width, height = _detect_image_type_and_size(data)
    return {
        "path": str(path),
        "name": _build_image_asset_name(path, data),
        "mime_type": mime_type,
        "width": width,
        "height": height,
    }


def _build_image_asset_name(path: Path, data: bytes) -> str:
    """Build a deterministic asset name required by Google Ads image assets."""
    digest = hashlib.sha1(data).hexdigest()[:12]
    stem = path.stem.strip() or "image"
    return f"AdLoop image {stem[:80]} {digest}"


def _detect_image_type_and_size(data: bytes) -> tuple[str, int, int]:
    """Return MIME type plus width/height for supported local image files."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height

    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 1 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break

            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 1 >= len(data):
                break

            segment_length = struct.unpack(">H", data[index:index + 2])[0]
            if segment_length < 2 or index + segment_length > len(data):
                break

            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3,
                0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            }:
                if index + 7 > len(data):
                    break
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return "image/jpeg", width, height

            index += segment_length

    raise ValueError(
        "Unsupported image type. Use a local PNG, JPEG, or GIF file."
    )


# ---------------------------------------------------------------------------
# Draft tools — validate inputs, create a ChangePlan, return preview
# ---------------------------------------------------------------------------


def draft_responsive_search_ad(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    final_url: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns preview, does NOT execute."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    headlines = headlines or []
    descriptions = descriptions or []

    errors = _validate_rsa(ad_group_id, headlines, descriptions, final_url)
    if errors:
        return {"error": "Validation failed", "details": errors}

    url_check = _validate_urls([final_url])
    if url_check.get(final_url):
        return {
            "error": "URL validation failed",
            "details": [
                f"final_url '{final_url}' is not reachable: {url_check[final_url]}. "
                f"Ads MUST point to working URLs."
            ],
        }

    warnings = []
    if len(headlines) < 8:
        warnings.append(
            f"Only {len(headlines)} headlines provided. Google recommends 8-15 "
            "diverse headlines for optimal RSA performance."
        )
    if len(descriptions) < 3:
        warnings.append(
            f"Only {len(descriptions)} descriptions provided. Google recommends "
            "3-4 descriptions for optimal RSA performance."
        )

    plan = ChangePlan(
        operation="create_responsive_search_ad",
        entity_type="ad",
        customer_id=customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "headlines": headlines,
            "descriptions": descriptions,
            "final_url": final_url,
            "path1": path1,
            "path2": path2,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    keywords: list[dict] | None = None,
) -> dict:
    """Draft keyword additions with match types — returns preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_keywords", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []

    errors = _validate_keywords(ad_group_id, keywords)
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = _check_broad_match_safety(config, customer_id, ad_group_id, keywords)

    plan = ChangePlan(
        operation="add_keywords",
        entity_type="keyword",
        customer_id=customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "keywords": keywords,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def add_negative_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft negative keyword additions — returns preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_negative_keywords", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _VALID_MATCH_TYPES:
        errors.append(f"Invalid match_type '{match_type}' — use EXACT, PHRASE, or BROAD")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="add_negative_keywords",
        entity_type="negative_keyword",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "keywords": keywords,
            "match_type": match_type,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def update_ad_group(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    ad_group_name: str = "",
    max_cpc: float = 0,
) -> dict:
    """Draft an ad group update for name and manual CPC bid."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_ad_group", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if max_cpc < 0:
        errors.append("max_cpc cannot be negative")
    if max_cpc:
        uses_manual_cpc = _ad_group_uses_manual_cpc(config, customer_id, ad_group_id)
        if uses_manual_cpc is False:
            errors.append("max_cpc requires an ad group in a MANUAL_CPC campaign")
        elif uses_manual_cpc is None:
            errors.append(
                f"Unable to verify bidding strategy for ad_group_id '{ad_group_id}'"
            )

    has_any_change = bool(ad_group_name.strip() or max_cpc)
    if not has_any_change:
        errors.append("No changes specified — provide ad_group_name and/or max_cpc")

    if errors:
        return {"error": "Validation failed", "details": errors}

    changes: dict = {"ad_group_id": ad_group_id}
    if ad_group_name.strip():
        changes["ad_group_name"] = ad_group_name.strip()
    if max_cpc:
        changes["max_cpc"] = max_cpc

    plan = ChangePlan(
        operation="update_ad_group",
        entity_type="ad_group",
        entity_id=ad_group_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def pause_entity(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft pausing a campaign/ad group/ad/keyword — returns preview."""
    return _draft_status_change(
        config, "pause_entity", customer_id, entity_type, entity_id, "PAUSED"
    )


def enable_entity(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft enabling a paused entity — returns preview."""
    return _draft_status_change(
        config, "enable_entity", customer_id, entity_type, entity_id, "ENABLED"
    )


def remove_entity(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft removing an entity (keyword, negative_keyword, ad, ad_group, campaign).

    This is a DESTRUCTIVE operation — removed entities cannot be re-enabled.
    For keywords and negative keywords, this fully deletes the criterion.
    Returns a preview; call confirm_and_apply to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("remove_entity", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if entity_type not in _REMOVABLE_ENTITY_TYPES:
        errors.append(
            f"entity_type must be one of {_REMOVABLE_ENTITY_TYPES}, "
            f"got '{entity_type}'"
        )
    if not entity_id:
        errors.append("entity_id is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    # Normalize composite IDs: commas → tildes
    if entity_type in ("campaign_asset", "customer_asset"):
        entity_id = entity_id.replace(",", "~")

    plan = ChangePlan(
        operation="remove_entity",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"action": "REMOVE"},
    )
    store_plan(plan)
    return plan.to_preview()


def draft_campaign(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_name: str = "",
    daily_budget: float = 0,
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    channel_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool = False,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft a full campaign structure — returns preview, does NOT execute.

    Creates: CampaignBudget + Campaign (PAUSED) + AdGroup + optional Keywords
    + geo targeting + language targeting.
    Ads are NOT included — use draft_responsive_search_ad separately.

    geo_target_ids: list of geo target constant IDs (e.g. ["2276"] for Germany,
        ["2840"] for USA). REQUIRED — campaigns must target specific countries.
    language_ids: list of language constant IDs (e.g. ["1001"] for German,
        ["1000"] for English). REQUIRED — campaigns must target specific languages.
    """
    from adloop.safety.guards import (
        SafetyViolation,
        check_blocked_operation,
        check_budget_cap,
    )
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    normalized_display_network_enabled, alias_errors = _normalize_display_network_setting(
        display_network_enabled,
        display_expansion_enabled,
    )
    if alias_errors:
        return {"error": "Validation failed", "details": alias_errors}
    if normalized_display_network_enabled is None:
        normalized_display_network_enabled = False

    errors, warnings = _validate_campaign(
        config,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        channel_type=channel_type,
        keywords=keywords,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        customer_id=customer_id,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=normalized_display_network_enabled,
        max_cpc=max_cpc,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        check_budget_cap(daily_budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    plan = ChangePlan(
        operation="create_campaign",
        entity_type="campaign",
        customer_id=customer_id,
        changes={
            "campaign_name": campaign_name,
            "daily_budget": daily_budget,
            "bidding_strategy": bidding_strategy.upper(),
            "target_cpa": target_cpa if target_cpa else None,
            "target_roas": target_roas if target_roas else None,
            "channel_type": channel_type.upper(),
            "ad_group_name": ad_group_name or campaign_name,
            "keywords": keywords,
            "geo_target_ids": geo_target_ids or [],
            "language_ids": language_ids or [],
            "search_partners_enabled": search_partners_enabled,
            "display_network_enabled": normalized_display_network_enabled,
            "max_cpc": max_cpc if max_cpc else None,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_ad_group(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
    cpc_bid_micros: int = 0,
) -> dict:
    """Draft a new ad group within an existing campaign — returns preview.

    Creates: AdGroup (ENABLED, SEARCH_STANDARD) + optional Keywords.
    Ads are NOT included — use draft_responsive_search_ad separately
    after the ad group is created.

    cpc_bid_micros: Optional ad-group-level CPC bid in micros. Only relevant
        for campaigns using MANUAL_CPC bidding.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_ad_group", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = _validate_ad_group(
        campaign_id=campaign_id,
        ad_group_name=ad_group_name,
        keywords=keywords,
        cpc_bid_micros=cpc_bid_micros,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    keywords = keywords or []
    preflight_errors, warnings = _preflight_ad_group_checks(
        config, customer_id, campaign_id, ad_group_name, keywords, cpc_bid_micros
    )
    if preflight_errors:
        return {"error": "Pre-flight check failed", "details": preflight_errors}

    plan = ChangePlan(
        operation="create_ad_group",
        entity_type="ad_group",
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "ad_group_name": ad_group_name,
            "keywords": keywords,
            "cpc_bid_micros": cpc_bid_micros,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def update_campaign(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    daily_budget: float = 0,
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool | None = None,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft an update to an existing campaign — returns preview, does NOT execute.

    All parameters except campaign_id are optional — only include what you want
    to change. Geo/language targets are REPLACED entirely (not appended).
    """
    from adloop.safety.guards import (
        SafetyViolation,
        check_blocked_operation,
        check_budget_cap,
    )
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    warnings = []

    normalized_display_network_enabled, alias_errors = _normalize_display_network_setting(
        display_network_enabled,
        display_expansion_enabled,
    )
    errors.extend(alias_errors)

    if not campaign_id:
        errors.append("campaign_id is required")

    bs = bidding_strategy.upper() if bidding_strategy else ""
    if bs and bs not in _VALID_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(_VALID_BIDDING_STRATEGIES)}, "
            f"got '{bidding_strategy}'"
        )
    if bs == "TARGET_CPA" and not target_cpa:
        errors.append("target_cpa is required when bidding_strategy is TARGET_CPA")
    if bs == "TARGET_ROAS" and not target_roas:
        errors.append("target_roas is required when bidding_strategy is TARGET_ROAS")
    if max_cpc < 0:
        errors.append("max_cpc cannot be negative")

    if daily_budget and daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")

    if daily_budget:
        try:
            check_budget_cap(daily_budget, config.safety)
        except SafetyViolation as e:
            errors.append(str(e))

    if geo_target_ids is not None and len(geo_target_ids) == 0:
        errors.append("geo_target_ids cannot be empty — provide at least one geo target")
    if language_ids is not None and len(language_ids) == 0:
        errors.append("language_ids cannot be empty — provide at least one language")
    if max_cpc:
        strategy_for_cap = bs or _campaign_bidding_strategy(config, customer_id, campaign_id)
        if strategy_for_cap is None:
            errors.append("campaign_id was not found")
        elif strategy_for_cap != "TARGET_SPEND":
            errors.append("max_cpc requires TARGET_SPEND bidding_strategy")

    has_any_change = any([
        bs,
        daily_budget,
        geo_target_ids is not None,
        language_ids is not None,
        search_partners_enabled is not None,
        normalized_display_network_enabled is not None,
        max_cpc,
    ])
    if not has_any_change:
        errors.append("No changes specified — provide at least one parameter to update")

    if errors:
        return {"error": "Validation failed", "details": errors}

    if bs == "MANUAL_CPC":
        warnings.append(
            "MANUAL_CPC bidding requires constant monitoring. Consider using "
            "MAXIMIZE_CONVERSIONS or TARGET_CPA for automated optimization."
        )

    if daily_budget and target_cpa > 0 and daily_budget < 5 * target_cpa:
        from adloop.ads.currency import format_currency, get_currency_code
        currency_code = get_currency_code(config, customer_id)
        warnings.append(
            f"Daily budget {format_currency(daily_budget, currency_code)} is less than 5x target CPA "
            f"{format_currency(target_cpa, currency_code)}. Google recommends at least 5x."
        )

    changes: dict = {"campaign_id": campaign_id}
    if bs:
        changes["bidding_strategy"] = bs
    if target_cpa:
        changes["target_cpa"] = target_cpa
    if target_roas:
        changes["target_roas"] = target_roas
    if daily_budget:
        changes["daily_budget"] = daily_budget
    if geo_target_ids is not None:
        changes["geo_target_ids"] = geo_target_ids
    if language_ids is not None:
        changes["language_ids"] = language_ids
    if search_partners_enabled is not None:
        changes["search_partners_enabled"] = search_partners_enabled
    if normalized_display_network_enabled is not None:
        changes["display_network_enabled"] = normalized_display_network_enabled
    if max_cpc:
        changes["max_cpc"] = max_cpc

    plan = ChangePlan(
        operation="update_campaign",
        entity_type="campaign",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_callouts(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    callouts: list[str] | None = None,
) -> dict:
    """Draft campaign callout assets."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_callouts", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_callouts, errors = _validate_callouts(campaign_id, callouts or [])
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_callouts",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "callouts": validated_callouts,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_structured_snippets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    snippets: list[dict] | None = None,
) -> dict:
    """Draft campaign structured snippet assets."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_structured_snippets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_snippets, errors = _validate_structured_snippets(
        campaign_id, snippets or []
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_structured_snippets",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "snippets": validated_snippets,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_image_assets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    image_paths: list[str] | None = None,
) -> dict:
    """Draft campaign image assets from local files."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_image_assets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_images, errors = _validate_image_assets(campaign_id, image_paths or [])
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_image_assets",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "images": validated_images,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_sitelinks(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    sitelinks: list[dict] | None = None,
) -> dict:
    """Draft sitelink extensions for a campaign — returns preview, does NOT execute.

    sitelinks: list of dicts, each with:
        - link_text (str, required, max 25 chars) — the clickable text
        - final_url (str, required) — where the sitelink points
        - description1 (str, optional, max 35 chars) — first description line
        - description2 (str, optional, max 35 chars) — second description line
    campaign_id: the campaign to attach sitelinks to
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_sitelinks", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not campaign_id:
        return {"error": "campaign_id is required"}
    if not sitelinks:
        return {"error": "At least one sitelink is required"}

    errors = []
    warnings = []
    validated = []

    for i, sl in enumerate(sitelinks):
        link_text = sl.get("link_text", "").strip()
        final_url = sl.get("final_url", "").strip()
        desc1 = sl.get("description1", "").strip()
        desc2 = sl.get("description2", "").strip()

        if not link_text:
            errors.append(f"Sitelink {i + 1}: link_text is required")
        elif len(link_text) > 25:
            errors.append(
                f"Sitelink {i + 1}: link_text '{link_text}' is {len(link_text)} chars (max 25)"
            )
        if not final_url:
            errors.append(f"Sitelink {i + 1}: final_url is required")
        if desc1 and len(desc1) > 35:
            errors.append(
                f"Sitelink {i + 1}: description1 is {len(desc1)} chars (max 35)"
            )
        if desc2 and len(desc2) > 35:
            errors.append(
                f"Sitelink {i + 1}: description2 is {len(desc2)} chars (max 35)"
            )
        if desc2 and not desc1:
            warnings.append(
                f"Sitelink {i + 1}: description2 without description1 — Google may ignore it"
            )

        validated.append({
            "link_text": link_text,
            "final_url": final_url,
            "description1": desc1,
            "description2": desc2,
        })

    if errors:
        return {"error": "Validation failed", "details": errors}

    sitelink_urls = [sl["final_url"] for sl in validated]
    url_checks = _validate_urls(sitelink_urls)
    bad_urls = {u: err for u, err in url_checks.items() if err}
    if bad_urls:
        return {
            "error": "URL validation failed — sitelinks MUST point to working URLs",
            "details": [
                f"'{url}' is not reachable: {err}" for url, err in bad_urls.items()
            ],
        }

    if len(validated) < 2:
        warnings.append(
            "Google recommends at least 4 sitelinks per campaign. "
            "Fewer than 2 may not show at all."
        )
    elif len(validated) < 4:
        warnings.append(
            f"Only {len(validated)} sitelinks — Google recommends at least 4 for "
            f"maximum ad real estate."
        )

    plan = ChangePlan(
        operation="create_sitelinks",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={"campaign_id": campaign_id, "sitelinks": validated},
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


# ---------------------------------------------------------------------------
# confirm_and_apply — the only function that actually mutates Google Ads
# ---------------------------------------------------------------------------


def confirm_and_apply(
    config: AdLoopConfig,
    *,
    plan_id: str = "",
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    Defaults to dry_run=True. The caller must explicitly pass dry_run=False
    to make real changes.
    """
    from adloop.safety.audit import log_mutation
    from adloop.safety.preview import get_plan, remove_plan

    plan = get_plan(plan_id)
    if plan is None:
        return {
            "error": f"No pending plan found with id '{plan_id}'. "
            "Plans expire when the MCP server restarts.",
        }

    if config.safety.require_dry_run:
        dry_run = True

    if dry_run:
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=True,
            result="dry_run_success",
        )
        return {
            "status": "DRY_RUN_SUCCESS",
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "changes": plan.changes,
            "message": (
                "Dry run completed — no changes were made to your Google Ads account. "
                "To apply for real, call confirm_and_apply again with dry_run=false."
            ),
        }

    try:
        result = _execute_plan(config, plan)
    except Exception as e:
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=False,
            result="error",
            error=str(e),
        )
        return {"error": str(e), "plan_id": plan.plan_id}

    log_mutation(
        config.safety.log_file,
        operation=plan.operation,
        customer_id=plan.customer_id,
        entity_type=plan.entity_type,
        entity_id=plan.entity_id,
        changes=plan.changes,
        dry_run=False,
        result="success",
    )
    remove_plan(plan.plan_id)

    return {
        "status": "APPLIED",
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

_VALID_MATCH_TYPES = {"EXACT", "PHRASE", "BROAD"}
_VALID_ENTITY_TYPES = {"campaign", "ad_group", "ad", "keyword"}
_REMOVABLE_ENTITY_TYPES = _VALID_ENTITY_TYPES | {
    "negative_keyword", "campaign_asset", "asset", "customer_asset",
}

_SMART_BIDDING_STRATEGIES = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
}


def _campaign_uses_manual_cpc(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> bool | None:
    """Return True when the campaign exists and uses MANUAL_CPC."""
    bidding_strategy = _campaign_bidding_strategy(config, customer_id, campaign_id)
    if bidding_strategy is None:
        return None
    return bidding_strategy == "MANUAL_CPC"


def _campaign_bidding_strategy(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> str | None:
    """Return the bidding strategy type for the campaign, if it exists."""
    from adloop.ads.gaql import execute_query

    query = f"""
        SELECT campaign.bidding_strategy_type
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """
    rows = execute_query(config, customer_id, query)
    if not rows:
        return None
    return rows[0].get("campaign.bidding_strategy_type")


def _ad_group_uses_manual_cpc(
    config: AdLoopConfig, customer_id: str, ad_group_id: str
) -> bool | None:
    """Return True when the ad group exists in a MANUAL_CPC campaign."""
    from adloop.ads.gaql import execute_query

    query = f"""
        SELECT campaign.bidding_strategy_type
        FROM ad_group
        WHERE ad_group.id = {ad_group_id}
        LIMIT 1
    """
    rows = execute_query(config, customer_id, query)
    if not rows:
        return None
    return rows[0].get("campaign.bidding_strategy_type") == "MANUAL_CPC"


def _validate_callouts(
    campaign_id: str, callouts: list[str]
) -> tuple[list[str], list[str]]:
    errors = []
    validated = []

    if not campaign_id:
        errors.append("campaign_id is required")
    if not callouts:
        errors.append("At least one callout is required")

    for index, callout in enumerate(callouts):
        text = callout.strip()
        if not text:
            errors.append(f"Callout {index + 1}: text is required")
        elif len(text) > 25:
            errors.append(
                f"Callout {index + 1}: '{text}' is {len(text)} chars (max 25)"
            )
        else:
            validated.append(text)

    return validated, errors


def _validate_structured_snippets(
    campaign_id: str, snippets: list[dict]
) -> tuple[list[dict], list[str]]:
    errors = []
    validated = []

    if not campaign_id:
        errors.append("campaign_id is required")
    if not snippets:
        errors.append("At least one structured snippet is required")

    for index, snippet in enumerate(snippets):
        header = snippet.get("header", "").strip()
        values = [value.strip() for value in snippet.get("values", [])]

        if header not in _STRUCTURED_SNIPPET_HEADERS:
            errors.append(
                f"Structured snippet {index + 1}: header must be one of "
                f"{sorted(_STRUCTURED_SNIPPET_HEADERS)}"
            )
        if len(values) < 3 or len(values) > 10:
            errors.append(
                f"Structured snippet {index + 1}: values must contain 3-10 items"
            )
        for value_index, value in enumerate(values):
            if not value:
                errors.append(
                    f"Structured snippet {index + 1}: value {value_index + 1} is required"
                )
            elif len(value) > 25:
                errors.append(
                    f"Structured snippet {index + 1}: value '{value}' is "
                    f"{len(value)} chars (max 25)"
                )

        validated.append({"header": header, "values": values})

    return validated, errors


def _validate_image_assets(
    campaign_id: str, image_paths: list[str]
) -> tuple[list[dict[str, object]], list[str]]:
    errors = []
    validated = []

    if not campaign_id:
        errors.append("campaign_id is required")
    if not image_paths:
        errors.append("At least one image path is required")

    for index, image_path in enumerate(image_paths):
        try:
            validated.append(_parse_image_metadata(image_path))
        except ValueError as exc:
            errors.append(f"Image {index + 1}: {exc}")

    return validated, errors


def _check_broad_match_safety(
    config: AdLoopConfig,
    customer_id: str,
    ad_group_id: str,
    keywords: list[dict],
) -> list[str]:
    """Warn if BROAD match keywords are being added to a non-Smart Bidding campaign."""
    has_broad = any(
        (kw.get("match_type") or "").upper() == "BROAD" for kw in keywords
    )
    if not has_broad:
        return []

    try:
        from adloop.ads.gaql import execute_query

        query = f"""
            SELECT campaign.bidding_strategy_type, campaign.name
            FROM ad_group
            WHERE ad_group.id = {ad_group_id}
        """
        rows = execute_query(config, customer_id, query)
        if not rows:
            return []

        bidding = rows[0].get("campaign.bidding_strategy_type", "")
        campaign_name = rows[0].get("campaign.name", "")

        if bidding not in _SMART_BIDDING_STRATEGIES:
            return [
                f"DANGEROUS: Adding BROAD match keywords to campaign "
                f"'{campaign_name}' which uses {bidding} bidding. "
                f"Broad Match without Smart Bidding (tCPA/tROAS/Maximize Conversions) "
                f"leads to irrelevant matches and wasted budget. "
                f"Use PHRASE or EXACT match instead, or switch the campaign "
                f"to Smart Bidding first."
            ]
    except Exception:
        pass

    return []


def _validate_rsa(
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
) -> list[str]:
    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if not final_url:
        errors.append("final_url is required")
    if len(headlines) < 3:
        errors.append(f"Need at least 3 headlines, got {len(headlines)}")
    if len(headlines) > 15:
        errors.append(f"Maximum 15 headlines, got {len(headlines)}")
    if len(descriptions) < 2:
        errors.append(f"Need at least 2 descriptions, got {len(descriptions)}")
    if len(descriptions) > 4:
        errors.append(f"Maximum 4 descriptions, got {len(descriptions)}")
    for i, h in enumerate(headlines):
        if len(h) > 30:
            errors.append(f"Headline {i + 1} exceeds 30 chars ({len(h)}): '{h}'")
    for i, d in enumerate(descriptions):
        if len(d) > 90:
            errors.append(f"Description {i + 1} exceeds 90 chars ({len(d)}): '{d}'")
    return errors


_VALID_BIDDING_STRATEGIES = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
    "TARGET_SPEND",
    "MANUAL_CPC",
}

_VALID_CHANNEL_TYPES = {"SEARCH", "DISPLAY", "SHOPPING", "VIDEO", "PERFORMANCE_MAX"}


def _validate_campaign(
    config: AdLoopConfig,
    *,
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    target_cpa: float,
    target_roas: float,
    channel_type: str,
    keywords: list[dict] | None,
    geo_target_ids: list[str] | None,
    language_ids: list[str] | None,
    customer_id: str = "",
    search_partners_enabled: bool = False,
    display_network_enabled: bool = False,
    max_cpc: float = 0,
) -> tuple[list[str], list[str]]:
    """Validate campaign draft inputs. Returns (errors, warnings)."""
    errors = []
    warnings = []

    if not campaign_name or not campaign_name.strip():
        errors.append("campaign_name is required")
    if daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")
    if not geo_target_ids:
        errors.append(
            "geo_target_ids is required — campaigns must target at least one "
            "country/region (e.g. ['2276'] for Germany, ['2840'] for USA)"
        )
    if not language_ids:
        errors.append(
            "language_ids is required — campaigns must target at least one "
            "language (e.g. ['1001'] for German, ['1000'] for English)"
        )

    bs = bidding_strategy.upper()
    if bs not in _VALID_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(_VALID_BIDDING_STRATEGIES)}, "
            f"got '{bidding_strategy}'"
        )
    if bs == "TARGET_CPA" and not target_cpa:
        errors.append("target_cpa is required when bidding_strategy is TARGET_CPA")
    if bs == "TARGET_ROAS" and not target_roas:
        errors.append("target_roas is required when bidding_strategy is TARGET_ROAS")

    ct = channel_type.upper()
    if ct not in _VALID_CHANNEL_TYPES:
        errors.append(
            f"channel_type must be one of {sorted(_VALID_CHANNEL_TYPES)}, "
            f"got '{channel_type}'"
        )
    if ct != "SEARCH" and search_partners_enabled:
        errors.append("search_partners_enabled is only supported for SEARCH campaigns")
    if ct != "SEARCH" and display_network_enabled:
        errors.append("display_network_enabled is only supported for SEARCH campaigns")
    if max_cpc < 0:
        errors.append("max_cpc cannot be negative")
    if max_cpc and bs not in {"MANUAL_CPC", "TARGET_SPEND"}:
        errors.append("max_cpc requires MANUAL_CPC or TARGET_SPEND bidding_strategy")

    if keywords:
        has_broad = any(
            (kw.get("match_type") or "").upper() == "BROAD" for kw in keywords
        )
        if has_broad and bs not in _SMART_BIDDING_STRATEGIES:
            errors.append(
                f"BROAD match keywords require Smart Bidding "
                f"(tCPA/tROAS/Maximize Conversions). "
                f"'{bidding_strategy}' is not a Smart Bidding strategy. "
                f"Use PHRASE or EXACT match instead."
            )
        for i, kw in enumerate(keywords):
            if not kw.get("text"):
                errors.append(f"Keyword {i + 1} has no text")
            mt = (kw.get("match_type") or "").upper()
            if mt not in _VALID_MATCH_TYPES:
                errors.append(
                    f"Keyword {i + 1} has invalid match_type '{mt}' "
                    "(must be EXACT, PHRASE, or BROAD)"
                )

    if target_cpa > 0 and daily_budget < 5 * target_cpa:
        from adloop.ads.currency import format_currency, get_currency_code
        currency_code = get_currency_code(config, customer_id)
        warnings.append(
            f"Daily budget {format_currency(daily_budget, currency_code)} is less than 5x target CPA "
            f"{format_currency(target_cpa, currency_code)}. Google recommends at least 5x target CPA "
            f"({format_currency(5 * target_cpa, currency_code)}/day) for sufficient learning data."
        )

    if bs == "MANUAL_CPC":
        warnings.append(
            "MANUAL_CPC bidding requires constant monitoring. Consider using "
            "MAXIMIZE_CONVERSIONS or TARGET_CPA for automated optimization."
        )

    return errors, warnings


def _validate_keywords(ad_group_id: str, keywords: list[dict]) -> list[str]:
    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if not keywords:
        errors.append("At least one keyword is required")
    for i, kw in enumerate(keywords):
        if not kw.get("text"):
            errors.append(f"Keyword {i + 1} has no text")
        mt = (kw.get("match_type") or "").upper()
        if mt not in _VALID_MATCH_TYPES:
            errors.append(
                f"Keyword {i + 1} has invalid match_type '{mt}' "
                "(must be EXACT, PHRASE, or BROAD)"
            )
    return errors


def _validate_ad_group(
    *,
    campaign_id: str,
    ad_group_name: str,
    keywords: list[dict] | None,
    cpc_bid_micros: int,
) -> list[str]:
    """Validate inputs for draft_ad_group."""
    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not ad_group_name or not ad_group_name.strip():
        errors.append("ad_group_name is required")
    if cpc_bid_micros < 0:
        errors.append("cpc_bid_micros must be >= 0")
    if keywords:
        for i, kw in enumerate(keywords):
            if not kw.get("text"):
                errors.append(f"Keyword {i + 1} has no text")
            mt = (kw.get("match_type") or "").upper()
            if mt not in _VALID_MATCH_TYPES:
                errors.append(
                    f"Keyword {i + 1} has invalid match_type '{mt}' "
                    "(must be EXACT, PHRASE, or BROAD)"
                )
    return errors


def _preflight_ad_group_checks(
    config: AdLoopConfig,
    customer_id: str,
    campaign_id: str,
    ad_group_name: str,
    keywords: list[dict],
    cpc_bid_micros: int,
) -> tuple[list[str], list[str]]:
    """Run pre-flight checks before creating an ad group.

    Returns (errors, warnings). Errors block the draft; warnings are informational.

    Checks performed:
    1. Campaign must be a SEARCH campaign (error if not).
    2. Warn if cpc_bid_micros is set but campaign uses Smart Bidding (ignored).
    3. Warn if BROAD match keywords + non-Smart Bidding campaign.
    4. Warn if an ad group with the same name already exists in the campaign.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        from adloop.ads.gaql import execute_query

        # Query 1: campaign info (type, bidding, name)
        campaign_query = f"""
            SELECT campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   campaign.name
            FROM campaign
            WHERE campaign.id = {campaign_id}
        """
        rows = execute_query(config, customer_id, campaign_query)
        if not rows:
            errors.append(
                f"Campaign {campaign_id} not found. Verify the campaign ID "
                "using get_campaign_performance."
            )
            return errors, warnings

        row = rows[0]
        channel_type = row.get("campaign.advertising_channel_type", "")
        bidding = row.get("campaign.bidding_strategy_type", "")
        campaign_name = row.get("campaign.name", "")

        # Check 1: campaign type must be SEARCH
        if channel_type and channel_type != "SEARCH":
            errors.append(
                f"Campaign '{campaign_name}' is a {channel_type} campaign. "
                "draft_ad_group only supports SEARCH campaigns."
            )

        # Check 2: cpc_bid_micros on Smart Bidding is ignored
        if cpc_bid_micros and bidding in _SMART_BIDDING_STRATEGIES:
            warnings.append(
                f"Campaign '{campaign_name}' uses {bidding} (Smart Bidding). "
                "The cpc_bid_micros value will be ignored — Smart Bidding "
                "sets bids automatically."
            )

        # Check 3: BROAD match + non-Smart Bidding
        has_broad = any(
            (kw.get("match_type") or "").upper() == "BROAD" for kw in keywords
        )
        if has_broad and bidding not in _SMART_BIDDING_STRATEGIES:
            warnings.append(
                f"DANGEROUS: Adding BROAD match keywords to campaign "
                f"'{campaign_name}' which uses {bidding} bidding. "
                f"Broad Match without Smart Bidding (tCPA/tROAS/Maximize "
                f"Conversions) leads to irrelevant matches and wasted budget. "
                f"Use PHRASE or EXACT match instead, or switch the campaign "
                f"to Smart Bidding first."
            )

        # Check 4: existing ad groups (duplicate name check)
        ag_query = f"""
            SELECT ad_group.name
            FROM ad_group
            WHERE campaign.id = {campaign_id}
        """
        ag_rows = execute_query(config, customer_id, ag_query)
        existing_names = {r.get("ad_group.name", "") for r in ag_rows}
        if ad_group_name in existing_names:
            warnings.append(
                f"An ad group named '{ad_group_name}' already exists in "
                f"campaign '{campaign_name}'. This will create a duplicate. "
                f"Consider using a different name to avoid confusion."
            )

    except Exception as exc:
        # Surface preflight failures as warnings so users know checks
        # were skipped, rather than silently producing a clean preview.
        warnings.append(
            f"Preflight checks could not complete ({exc}). "
            "The draft will proceed, but some validations were skipped. "
            "Full validation happens at confirm_and_apply time."
        )

    return errors, warnings


def _draft_status_change(
    config: AdLoopConfig,
    operation: str,
    customer_id: str,
    entity_type: str,
    entity_id: str,
    target_status: str,
) -> dict:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if entity_type not in _VALID_ENTITY_TYPES:
        errors.append(
            f"entity_type must be one of {_VALID_ENTITY_TYPES}, got '{entity_type}'"
        )
    if not entity_id:
        errors.append("entity_id is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"target_status": target_status},
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# Execution — actual Google Ads API mutate calls
# ---------------------------------------------------------------------------


def _execute_plan(config: AdLoopConfig, plan: object) -> dict:
    """Dispatch to the right Google Ads mutate call based on plan.operation."""
    from adloop.ads.client import get_ads_client, normalize_customer_id

    client = get_ads_client(config)
    cid = normalize_customer_id(plan.customer_id)

    dispatch = {
        "create_campaign": _apply_create_campaign,
        "create_ad_group": _apply_create_ad_group,
        "update_campaign": _apply_update_campaign,
        "update_ad_group": _apply_update_ad_group,
        "create_responsive_search_ad": _apply_create_rsa,
        "add_keywords": _apply_add_keywords,
        "add_negative_keywords": _apply_add_negative_keywords,
        "pause_entity": _apply_status_change,
        "enable_entity": _apply_status_change,
        "remove_entity": _apply_remove,
        "create_callouts": _apply_create_callouts,
        "create_structured_snippets": _apply_create_structured_snippets,
        "create_image_assets": _apply_create_image_assets,
        "create_sitelinks": _apply_create_sitelinks,
    }

    handler = dispatch.get(plan.operation)
    if handler is None:
        raise ValueError(f"Unknown operation: {plan.operation}")

    if plan.operation in ("pause_entity", "enable_entity"):
        return handler(
            client,
            cid,
            plan.entity_type,
            plan.entity_id,
            plan.changes["target_status"],
        )

    if plan.operation == "remove_entity":
        return handler(client, cid, plan.entity_type, plan.entity_id)

    return handler(client, cid, plan.changes)


def _apply_update_ad_group(client: object, cid: str, changes: dict) -> dict:
    """Update an ad group's name and/or manual CPC bid."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = service.ad_group_path(cid, changes["ad_group_id"])

    field_paths = []
    if changes.get("ad_group_name"):
        ad_group.name = changes["ad_group_name"]
        field_paths.append("name")
    if changes.get("max_cpc"):
        ad_group.cpc_bid_micros = int(changes["max_cpc"] * 1_000_000)
        field_paths.append("cpc_bid_micros")

    operation.update_mask = field_mask_pb2.FieldMask(paths=field_paths)
    response = service.mutate_ad_groups(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_create_campaign(client: object, cid: str, changes: dict) -> dict:
    """Create campaign + budget + ad group + optional keywords atomically."""
    service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")
    budget_service = client.get_service("CampaignBudgetService")
    ad_group_service = client.get_service("AdGroupService")

    operations = []

    # 1. CampaignBudget (temp ID: -1)
    budget_op = client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_service.campaign_budget_path(cid, "-1")
    budget.name = f"Budget - {changes['campaign_name']}"
    budget.amount_micros = int(changes["daily_budget"] * 1_000_000)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(budget_op)

    # 2. Campaign (temp ID: -2, references budget -1)
    campaign_op = client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_service.campaign_path(cid, "-2")
    campaign.name = changes["campaign_name"]
    campaign.campaign_budget = budget_service.campaign_budget_path(cid, "-1")
    campaign.status = client.enums.CampaignStatusEnum.PAUSED

    channel = changes.get("channel_type", "SEARCH")
    campaign.advertising_channel_type = getattr(
        client.enums.AdvertisingChannelTypeEnum, channel
    )

    bs = changes["bidding_strategy"]
    if bs == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions.target_cpa_micros = 0
        if changes.get("target_cpa"):
            campaign.maximize_conversions.target_cpa_micros = int(
                changes["target_cpa"] * 1_000_000
            )
    elif bs == "TARGET_CPA":
        campaign.maximize_conversions.target_cpa_micros = int(
            changes["target_cpa"] * 1_000_000
        )
    elif bs == "MAXIMIZE_CONVERSION_VALUE":
        campaign.maximize_conversion_value.target_roas = 0
        if changes.get("target_roas"):
            campaign.maximize_conversion_value.target_roas = changes["target_roas"]
    elif bs == "TARGET_ROAS":
        campaign.maximize_conversion_value.target_roas = changes["target_roas"]
    elif bs == "TARGET_SPEND":
        campaign.target_spend.target_spend_micros = 0
        if changes.get("max_cpc"):
            campaign.target_spend.cpc_bid_ceiling_micros = int(
                changes["max_cpc"] * 1_000_000
            )
    elif bs == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False

    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = changes.get(
        "search_partners_enabled", False
    )
    campaign.network_settings.target_content_network = changes.get(
        "display_network_enabled", False
    )

    # EU political advertising declaration — required for campaigns that may
    # serve in EU countries. This is an ENUM, not a bool. Value 3 means
    # "does not contain EU political advertising" (the default for most users).
    # Setting False/0 maps to UNSPECIFIED which proto3 strips from the wire.
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )

    operations.append(campaign_op)

    # 3. AdGroup (temp ID: -3, references campaign -2)
    ag_op = client.get_type("MutateOperation")
    ad_group = ag_op.ad_group_operation.create
    ad_group.resource_name = ad_group_service.ad_group_path(cid, "-3")
    ad_group.name = changes.get("ad_group_name", changes["campaign_name"])
    ad_group.campaign = campaign_service.campaign_path(cid, "-2")
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if bs == "MANUAL_CPC" and changes.get("max_cpc"):
        ad_group.cpc_bid_micros = int(changes["max_cpc"] * 1_000_000)
    operations.append(ag_op)

    # 4. Keywords (reference ad_group -3)
    kw_list = changes.get("keywords") or []
    for kw in kw_list:
        kw_op = client.get_type("MutateOperation")
        criterion = kw_op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_service.ad_group_path(cid, "-3")
        criterion.keyword.text = kw["text"]
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"].upper()
        )
        operations.append(kw_op)

    # 5. Geo targeting (CampaignCriterion referencing campaign -2)
    for geo_id in changes.get("geo_target_ids") or []:
        geo_op = client.get_type("MutateOperation")
        geo_criterion = geo_op.campaign_criterion_operation.create
        geo_criterion.campaign = campaign_service.campaign_path(cid, "-2")
        geo_criterion.location.geo_target_constant = (
            f"geoTargetConstants/{geo_id}"
        )
        operations.append(geo_op)

    # 6. Language targeting (CampaignCriterion referencing campaign -2)
    for lang_id in changes.get("language_ids") or []:
        lang_op = client.get_type("MutateOperation")
        lang_criterion = lang_op.campaign_criterion_operation.create
        lang_criterion.campaign = campaign_service.campaign_path(cid, "-2")
        lang_criterion.language.language_constant = (
            f"languageConstants/{lang_id}"
        )
        operations.append(lang_op)

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results = {}
    num_keywords = len(kw_list)
    num_geo = len(changes.get("geo_target_ids") or [])
    num_lang = len(changes.get("language_ids") or [])
    for i, resp in enumerate(response.mutate_operation_responses):
        resp_type = resp.WhichOneof("response")
        if resp_type:
            inner = getattr(resp, resp_type)
            resource = getattr(inner, "resource_name", str(inner))
            if i == 0:
                results["campaign_budget"] = resource
            elif i == 1:
                results["campaign"] = resource
            elif i == 2:
                results["ad_group"] = resource
            elif i < 3 + num_keywords:
                results.setdefault("keywords", []).append(resource)
            elif i < 3 + num_keywords + num_geo:
                results.setdefault("geo_targets", []).append(resource)
            else:
                results.setdefault("language_targets", []).append(resource)

    return results


def _apply_create_ad_group(client: object, cid: str, changes: dict) -> dict:
    """Create ad group + optional keywords in an existing campaign atomically."""
    service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")
    ad_group_service = client.get_service("AdGroupService")

    operations: list = []

    # 1. AdGroup (temp ID: -1, references existing campaign)
    ag_op = client.get_type("MutateOperation")
    ad_group = ag_op.ad_group_operation.create
    ad_group.resource_name = ad_group_service.ad_group_path(cid, "-1")
    ad_group.name = changes["ad_group_name"]
    ad_group.campaign = campaign_service.campaign_path(cid, changes["campaign_id"])
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if changes.get("cpc_bid_micros"):
        ad_group.cpc_bid_micros = changes["cpc_bid_micros"]
    operations.append(ag_op)

    # 2. Keywords (reference ad_group -1)
    kw_list = changes.get("keywords") or []
    for kw in kw_list:
        kw_op = client.get_type("MutateOperation")
        criterion = kw_op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_service.ad_group_path(cid, "-1")
        criterion.keyword.text = kw["text"]
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"].upper()
        )
        operations.append(kw_op)

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results: dict = {}
    for i, resp in enumerate(response.mutate_operation_responses):
        resp_type = resp.WhichOneof("response")
        if resp_type:
            inner = getattr(resp, resp_type)
            resource = getattr(inner, "resource_name", str(inner))
            if i == 0:
                results["ad_group"] = resource
            else:
                results.setdefault("keywords", []).append(resource)

    return results


def _apply_update_campaign(client: object, cid: str, changes: dict) -> dict:
    """Update an existing campaign's settings."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")
    operations = []
    field_paths = []

    campaign_id = changes["campaign_id"]
    resource_name = campaign_service.campaign_path(cid, campaign_id)

    # Bid strategy and campaign-level setting changes
    bs = changes.get("bidding_strategy")
    search_partners_enabled = changes.get("search_partners_enabled")
    display_network_enabled = changes.get("display_network_enabled")
    if (
        bs
        or search_partners_enabled is not None
        or display_network_enabled is not None
        or changes.get("max_cpc")
    ):
        campaign_op = client.get_type("MutateOperation")
        campaign = campaign_op.campaign_operation.update
        campaign.resource_name = resource_name

        if bs == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions.target_cpa_micros = 0
            if changes.get("target_cpa"):
                campaign.maximize_conversions.target_cpa_micros = int(
                    changes["target_cpa"] * 1_000_000
                )
            field_paths.append("maximize_conversions.target_cpa_micros")
        elif bs == "TARGET_CPA":
            campaign.maximize_conversions.target_cpa_micros = int(
                changes["target_cpa"] * 1_000_000
            )
            field_paths.append("maximize_conversions.target_cpa_micros")
        elif bs == "MAXIMIZE_CONVERSION_VALUE":
            campaign.maximize_conversion_value.target_roas = 0
            if changes.get("target_roas"):
                campaign.maximize_conversion_value.target_roas = changes[
                    "target_roas"
                ]
            field_paths.append("maximize_conversion_value.target_roas")
        elif bs == "TARGET_ROAS":
            campaign.maximize_conversion_value.target_roas = changes["target_roas"]
            field_paths.append("maximize_conversion_value.target_roas")
        elif bs == "TARGET_SPEND":
            campaign.target_spend.target_spend_micros = 0
            field_paths.append("target_spend.target_spend_micros")
        elif bs == "MANUAL_CPC":
            campaign.manual_cpc.enhanced_cpc_enabled = False
            field_paths.append("manual_cpc.enhanced_cpc_enabled")

        if changes.get("max_cpc"):
            campaign.target_spend.cpc_bid_ceiling_micros = int(
                changes["max_cpc"] * 1_000_000
            )
            field_paths.append("target_spend.cpc_bid_ceiling_micros")

        if search_partners_enabled is not None:
            campaign.network_settings.target_search_network = search_partners_enabled
            field_paths.append("network_settings.target_search_network")
        if display_network_enabled is not None:
            campaign.network_settings.target_content_network = display_network_enabled
            field_paths.append("network_settings.target_content_network")

        if field_paths:
            campaign_op.campaign_operation.update_mask.CopyFrom(
                field_mask_pb2.FieldMask(paths=field_paths)
            )
            operations.append(campaign_op)

    # Budget change — requires finding the budget resource name first
    new_budget = changes.get("daily_budget")
    if new_budget:
        budget_query = f"""
            SELECT campaign.campaign_budget
            FROM campaign
            WHERE campaign.id = {campaign_id}
        """
        rows = list(service.search(customer_id=cid, query=budget_query))
        if not rows:
            raise ValueError(f"Campaign {campaign_id} not found")
        budget_rn = rows[0].campaign.campaign_budget

        budget_op = client.get_type("MutateOperation")
        budget = budget_op.campaign_budget_operation.update
        budget.resource_name = budget_rn
        budget.amount_micros = int(new_budget * 1_000_000)
        budget_op.campaign_budget_operation.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=["amount_micros"])
        )
        operations.append(budget_op)

    # Geo targeting — remove existing, add new
    geo_ids = changes.get("geo_target_ids")
    if geo_ids is not None:
        existing_geo = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = 'LOCATION'
        """
        for row in service.search(customer_id=cid, query=existing_geo):
            rm_op = client.get_type("MutateOperation")
            rm_op.campaign_criterion_operation.remove = (
                row.campaign_criterion.resource_name
            )
            operations.append(rm_op)

        for geo_id in geo_ids:
            add_op = client.get_type("MutateOperation")
            criterion = add_op.campaign_criterion_operation.create
            criterion.campaign = resource_name
            criterion.location.geo_target_constant = (
                f"geoTargetConstants/{geo_id}"
            )
            operations.append(add_op)

    # Language targeting — remove existing, add new
    lang_ids = changes.get("language_ids")
    if lang_ids is not None:
        existing_lang = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = 'LANGUAGE'
        """
        for row in service.search(customer_id=cid, query=existing_lang):
            rm_op = client.get_type("MutateOperation")
            rm_op.campaign_criterion_operation.remove = (
                row.campaign_criterion.resource_name
            )
            operations.append(rm_op)

        for lang_id in lang_ids:
            add_op = client.get_type("MutateOperation")
            criterion = add_op.campaign_criterion_operation.create
            criterion.campaign = resource_name
            criterion.language.language_constant = (
                f"languageConstants/{lang_id}"
            )
            operations.append(add_op)

    if not operations:
        return {"message": "No changes to apply"}

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results = {"updated": []}
    for resp in response.mutate_operation_responses:
        rn = (
            resp.campaign_result.resource_name
            or resp.campaign_budget_result.resource_name
            or resp.campaign_criterion_result.resource_name
        )
        if rn:
            results["updated"].append(rn)
    return results


def _apply_create_rsa(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("AdGroupAdService")
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create

    ad_group_ad.ad_group = client.get_service("AdGroupService").ad_group_path(
        cid, changes["ad_group_id"]
    )
    # Create as PAUSED for safety — user can enable separately
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED

    ad = ad_group_ad.ad
    ad.final_urls.append(changes["final_url"])

    for text in changes["headlines"]:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.responsive_search_ad.headlines.append(asset)

    for text in changes["descriptions"]:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.responsive_search_ad.descriptions.append(asset)

    if changes.get("path1"):
        ad.responsive_search_ad.path1 = changes["path1"]
    if changes.get("path2"):
        ad.responsive_search_ad.path2 = changes["path2"]

    response = service.mutate_ad_group_ads(
        customer_id=cid, operations=[operation]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_add_keywords(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("AdGroupCriterionService")
    ad_group_path = client.get_service("AdGroupService").ad_group_path(
        cid, changes["ad_group_id"]
    )

    operations = []
    for kw in changes["keywords"]:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = ad_group_path
        criterion.keyword.text = kw["text"]
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"].upper()
        )
        operations.append(operation)

    response = service.mutate_ad_group_criteria(
        customer_id=cid, operations=operations
    )
    return {"resource_names": [r.resource_name for r in response.results]}


def _apply_add_negative_keywords(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("CampaignCriterionService")
    campaign_path = client.get_service("CampaignService").campaign_path(
        cid, changes["campaign_id"]
    )

    operations = []
    for kw_text in changes["keywords"]:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.negative = True
        criterion.keyword.text = kw_text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, changes["match_type"]
        )
        operations.append(operation)

    response = service.mutate_campaign_criteria(
        customer_id=cid, operations=operations
    )
    return {"resource_names": [r.resource_name for r in response.results]}


def _resolve_ad_entity_id(client: object, cid: str, entity_id: str) -> str:
    """Ensure ad entity_id is in 'adGroupId~adId' composite format.

    If only a bare ad ID is given, queries the API to find the ad group.
    """
    if "~" in entity_id:
        return entity_id

    ga_service = client.get_service("GoogleAdsService")
    query = (
        f"SELECT ad_group.id, ad_group_ad.ad.id "
        f"FROM ad_group_ad "
        f"WHERE ad_group_ad.ad.id = {entity_id} "
        f"LIMIT 1"
    )
    response = ga_service.search(customer_id=cid, query=query)
    for row in response:
        ag_id = row.ad_group.id
        return f"{ag_id}~{entity_id}"

    raise ValueError(
        f"Ad ID {entity_id} not found. Pass the composite ID as "
        f"'adGroupId~adId' (e.g. '12345678~{entity_id}')."
    )


def _apply_remove(
    client: object,
    cid: str,
    entity_type: str,
    entity_id: str,
) -> dict:
    """Remove an entity via the REMOVE mutate operation (irreversible)."""
    if entity_type == "campaign":
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        operation.remove = service.campaign_path(cid, entity_id)
        response = service.mutate_campaigns(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "ad_group":
        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        operation.remove = service.ad_group_path(cid, entity_id)
        response = service.mutate_ad_groups(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "ad":
        resolved_id = _resolve_ad_entity_id(client, cid, entity_id)
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        operation.remove = f"customers/{cid}/adGroupAds/{resolved_id}"
        response = service.mutate_ad_group_ads(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "keyword":
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        operation.remove = f"customers/{cid}/adGroupCriteria/{entity_id}"
        response = service.mutate_ad_group_criteria(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "negative_keyword":
        service = client.get_service("CampaignCriterionService")
        operation = client.get_type("CampaignCriterionOperation")
        operation.remove = f"customers/{cid}/campaignCriteria/{entity_id}"
        response = service.mutate_campaign_criteria(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "campaign_asset":
        # Campaign asset composite ID: {campaign_id}~{asset_id}~{field_type}
        parts = entity_id.replace(",", "~").split("~")
        if len(parts) != 3:
            raise ValueError(
                f"campaign_asset entity_id must be "
                f"'campaignId~assetId~fieldType', got '{entity_id}'"
            )
        ca_service = client.get_service("CampaignAssetService")
        resource_name = ca_service.campaign_asset_path(
            cid, parts[0], parts[1], parts[2]
        )
        ga_service = client.get_service("GoogleAdsService")
        op = client.get_type("MutateOperation")
        op.campaign_asset_operation.remove = resource_name
        response = ga_service.mutate(
            customer_id=cid, mutate_operations=[op]
        )
        resp_inner = response.mutate_operation_responses[0]
        if resp_inner.campaign_asset_result.resource_name:
            return {"resource_name": resp_inner.campaign_asset_result.resource_name}
        return {"resource_name": resource_name, "status": "removed"}

    elif entity_type == "asset":
        service = client.get_service("AssetService")
        operation = client.get_type("AssetOperation")
        operation.remove = service.asset_path(cid, entity_id)
        response = service.mutate_assets(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "customer_asset":
        parts = entity_id.replace(",", "~").split("~")
        if len(parts) != 2:
            raise ValueError(
                f"customer_asset entity_id must be "
                f"'assetId~fieldType', got '{entity_id}'"
            )
        ca_service = client.get_service("CustomerAssetService")
        resource_name = ca_service.customer_asset_path(cid, parts[0], parts[1])
        ga_service = client.get_service("GoogleAdsService")
        op = client.get_type("MutateOperation")
        op.customer_asset_operation.remove = resource_name
        response = ga_service.mutate(
            customer_id=cid, mutate_operations=[op]
        )
        resp_inner = response.mutate_operation_responses[0]
        if resp_inner.customer_asset_result.resource_name:
            return {"resource_name": resp_inner.customer_asset_result.resource_name}
        return {"resource_name": resource_name, "status": "removed"}

    else:
        raise ValueError(f"Cannot remove entity_type: {entity_type}")

    return {"resource_name": response.results[0].resource_name}


def _apply_status_change(
    client: object,
    cid: str,
    entity_type: str,
    entity_id: str,
    status: str,
) -> dict:
    """Update the status of a campaign, ad group, ad, or keyword."""
    if entity_type == "campaign":
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        entity = operation.update
        entity.resource_name = service.campaign_path(cid, entity_id)
        entity.status = getattr(client.enums.CampaignStatusEnum, status)
        mutate = service.mutate_campaigns

    elif entity_type == "ad_group":
        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        entity = operation.update
        entity.resource_name = service.ad_group_path(cid, entity_id)
        entity.status = getattr(client.enums.AdGroupStatusEnum, status)
        mutate = service.mutate_ad_groups

    elif entity_type == "ad":
        resolved_id = _resolve_ad_entity_id(client, cid, entity_id)
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        entity = operation.update
        entity.resource_name = f"customers/{cid}/adGroupAds/{resolved_id}"
        entity.status = getattr(client.enums.AdGroupAdStatusEnum, status)
        mutate = service.mutate_ad_group_ads

    elif entity_type == "keyword":
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        entity = operation.update
        entity.resource_name = f"customers/{cid}/adGroupCriteria/{entity_id}"
        entity.status = getattr(
            client.enums.AdGroupCriterionStatusEnum, status
        )
        mutate = service.mutate_ad_group_criteria

    else:
        raise ValueError(f"Unknown entity_type: {entity_type}")

    # Build field mask for the status field only
    from google.protobuf import field_mask_pb2

    operation.update_mask = field_mask_pb2.FieldMask(paths=["status"])

    response = mutate(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_campaign_assets(
    client: object,
    cid: str,
    campaign_id: str,
    assets: list[dict],
    field_type: object,
    populate_asset: object,
) -> dict:
    """Create assets and link them to a campaign via CampaignAsset."""
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []

    for i, payload in enumerate(assets):
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = asset_service.asset_path(cid, str(-(i + 1)))
        populate_asset(asset, payload)
        operations.append(op)

    for i in range(len(assets)):
        op = client.get_type("MutateOperation")
        ca = op.campaign_asset_operation.create
        ca.asset = asset_service.asset_path(cid, str(-(i + 1)))
        ca.campaign = googleads_service.campaign_path(cid, campaign_id)
        ca.field_type = field_type
        operations.append(op)

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    results = {"assets": [], "campaign_assets": []}
    num_assets = len(assets)
    for i, resp in enumerate(response.mutate_operation_responses):
        resource = None
        if resp.asset_result.resource_name:
            resource = resp.asset_result.resource_name
        elif resp.campaign_asset_result.resource_name:
            resource = resp.campaign_asset_result.resource_name

        if resource:
            if i < num_assets:
                results["assets"].append(resource)
            else:
                results["campaign_assets"].append(resource)

    return results


def _apply_create_callouts(client: object, cid: str, changes: dict) -> dict:
    """Create callout assets and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        asset.callout_asset.callout_text = payload["callout_text"]

    assets = [{"callout_text": text} for text in changes["callouts"]]
    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        assets,
        client.enums.AssetFieldTypeEnum.CALLOUT,
        populate,
    )


def _apply_create_structured_snippets(
    client: object, cid: str, changes: dict
) -> dict:
    """Create structured snippet assets and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        asset.structured_snippet_asset.header = payload["header"]
        asset.structured_snippet_asset.values.extend(payload["values"])

    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        changes["snippets"],
        client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET,
        populate,
    )


def _apply_create_image_assets(client: object, cid: str, changes: dict) -> dict:
    """Create image assets from local files and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        image_path = Path(str(payload["path"]))
        image_bytes = image_path.read_bytes()
        mime_type_name = _VALID_IMAGE_MIME_TYPES[str(payload["mime_type"])]
        asset.name = str(payload.get("name") or _build_image_asset_name(image_path, image_bytes))
        asset.type_ = client.enums.AssetTypeEnum.IMAGE
        asset.image_asset.data = image_bytes
        asset.image_asset.mime_type = getattr(client.enums.MimeTypeEnum, mime_type_name)
        asset.image_asset.full_size.width_pixels = int(payload["width"])
        asset.image_asset.full_size.height_pixels = int(payload["height"])

    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        changes["images"],
        client.enums.AssetFieldTypeEnum.AD_IMAGE,
        populate,
    )


def _apply_create_sitelinks(client: object, cid: str, changes: dict) -> dict:
    """Create sitelink assets and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        asset.sitelink_asset.link_text = payload["link_text"]
        asset.final_urls.append(payload["final_url"])
        if payload.get("description1"):
            asset.sitelink_asset.description1 = payload["description1"]
        if payload.get("description2"):
            asset.sitelink_asset.description2 = payload["description2"]

    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        changes["sitelinks"],
        client.enums.AssetFieldTypeEnum.SITELINK,
        populate,
    )
