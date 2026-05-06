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

_VALID_HEADLINE_PINS = {"HEADLINE_1", "HEADLINE_2", "HEADLINE_3"}
_VALID_DESCRIPTION_PINS = {"DESCRIPTION_1", "DESCRIPTION_2"}


def _normalize_rsa_assets(items: list) -> list[dict]:
    """Accept str or {text, pinned_field?} dict entries; return list of dicts.

    Plain strings are treated as unpinned. Dict entries may include an optional
    ``pinned_field`` key whose value must be a valid pin slot for the asset
    role (validated by ``_validate_rsa``).
    """
    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            out.append({"text": item, "pinned_field": None})
        elif isinstance(item, dict):
            out.append(
                {
                    "text": item.get("text", ""),
                    "pinned_field": item.get("pinned_field"),
                }
            )
        else:
            raise ValueError(
                f"RSA asset entry must be str or dict, got {type(item).__name__}"
            )
    return out


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
    headlines: list[str | dict] | None = None,
    descriptions: list[str | dict] | None = None,
    final_url: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns preview, does NOT execute.

    Each headline/description entry may be either:

    - a plain string (unpinned), or
    - a dict ``{"text": "...", "pinned_field": "HEADLINE_1"}`` (pinned).

    Valid pin values:
        headlines:    HEADLINE_1, HEADLINE_2, HEADLINE_3
        descriptions: DESCRIPTION_1, DESCRIPTION_2

    Google caps: at most 2 headlines per pin slot, at most 1 description per
    pin slot. Mixed plain-string and dict entries are allowed within a single
    call (e.g. brand pinned to HEADLINE_1, the rest unpinned).
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    headlines = headlines or []
    descriptions = descriptions or []

    try:
        headlines = _normalize_rsa_assets(headlines)
        descriptions = _normalize_rsa_assets(descriptions)
    except ValueError as e:
        return {"error": "Validation failed", "details": [str(e)]}

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


def propose_negative_keyword_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    list_name: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft a shared negative keyword list and attach it to a campaign — returns PREVIEW.

    Creates a reusable negative keyword list (SharedSet) with the given keywords
    and links it to the campaign. Unlike add_negative_keywords, the list can later
    be reused across multiple campaigns.
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_negative_keyword_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not list_name:
        errors.append("list_name is required")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _VALID_MATCH_TYPES:
        errors.append(f"Invalid match_type '{match_type}' — use EXACT, PHRASE, or BROAD")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_negative_keyword_list",
        entity_type="negative_keyword_list",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "list_name": list_name,
            "keywords": keywords,
            "match_type": match_type,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def add_to_negative_keyword_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft adding keywords to an existing shared negative keyword list — returns PREVIEW.

    Unlike ``propose_negative_keyword_list`` (which creates a NEW list), this
    appends keywords to an existing SharedSet identified by ``shared_set_id``.
    Use ``get_negative_keyword_lists`` to find the list's ID. Call
    ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_to_negative_keyword_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not shared_set_id:
        errors.append("shared_set_id is required")
    elif not str(shared_set_id).isdigit():
        errors.append("shared_set_id must be a numeric ID (from get_negative_keyword_lists)")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _VALID_MATCH_TYPES:
        errors.append(f"Invalid match_type '{match_type}' — use EXACT, PHRASE, or BROAD")
    if errors:
        return {"error": "Validation failed", "details": errors}

    seen: set[str] = set()
    deduped: list[str] = []
    for kw in keywords:
        text = kw.strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)

    if not deduped:
        return {
            "error": "Validation failed",
            "details": ["At least one non-empty keyword is required"],
        }

    plan = ChangePlan(
        operation="add_to_negative_keyword_list",
        entity_type="negative_keyword_list",
        entity_id=str(shared_set_id),
        customer_id=customer_id,
        changes={
            "shared_set_id": str(shared_set_id),
            "keywords": deduped,
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


def update_responsive_search_ad(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_id: str = "",
    final_url: str = "",
    path1: str = "",
    path2: str = "",
    clear_path1: bool = False,
    clear_path2: bool = False,
) -> dict:
    """Draft an in-place update on an existing RSA — returns PREVIEW.

    Updates the mutable fields on an existing Responsive Search Ad without
    creating a new ad (no learning-period reset). The Google Ads API v23
    permits in-place mutation of ``final_urls``, ``path1`` and ``path2`` on
    an RSA via ``AdService.MutateAds``; nested ``headlines`` and
    ``descriptions`` remain immutable.

    Argument semantics:
        - ``final_url`` empty -> no change; non-empty -> replaces final_urls
        - ``path1`` / ``path2`` empty -> no change; non-empty -> sets value
        - ``clear_path1`` / ``clear_path2`` True -> set the path to empty
          (overrides the corresponding path string argument)

    At least one mutation must be requested. Call ``confirm_and_apply`` with
    the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []

    if not ad_id:
        errors.append("ad_id is required")
    elif not str(ad_id).isdigit():
        errors.append("ad_id must be a numeric ID")

    final_url = (final_url or "").strip()
    path1 = (path1 or "").strip()
    path2 = (path2 or "").strip()

    if path1 and len(path1) > 15:
        errors.append(f"path1 must be 15 chars or fewer (got {len(path1)})")
    if path2 and len(path2) > 15:
        errors.append(f"path2 must be 15 chars or fewer (got {len(path2)})")

    has_url_change = bool(final_url)
    has_path1_change = bool(path1) or clear_path1
    has_path2_change = bool(path2) or clear_path2

    if not (has_url_change or has_path1_change or has_path2_change):
        errors.append(
            "No changes specified — provide final_url, path1, path2, "
            "clear_path1, or clear_path2"
        )

    if errors:
        return {"error": "Validation failed", "details": errors}

    if has_url_change:
        url_check = _validate_urls([final_url])
        if url_check.get(final_url):
            return {
                "error": "URL validation failed",
                "details": [
                    f"final_url '{final_url}' is not reachable: "
                    f"{url_check[final_url]}. Ads MUST point to working URLs."
                ],
            }

    changes: dict = {"ad_id": str(ad_id)}
    if has_url_change:
        changes["final_url"] = final_url
    if has_path1_change:
        changes["path1"] = "" if clear_path1 else path1
    if has_path2_change:
        changes["path2"] = "" if clear_path2 else path2

    plan = ChangePlan(
        operation="update_responsive_search_ad",
        entity_type="ad",
        entity_id=str(ad_id),
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
    """Draft removing an entity — returns preview.

    Supported ``entity_type`` values: ``campaign``, ``ad_group``, ``ad``,
    ``keyword``, ``negative_keyword``, ``shared_criterion``, ``campaign_asset``,
    ``asset``, ``customer_asset``.

    Composite ``entity_id`` formats:

    - ``keyword``: ``adGroupId~criterionId``
    - ``negative_keyword``: ``campaignId~criterionId`` (use the ``resource_id``
      field from ``get_negative_keywords``)
    - ``shared_criterion``: ``sharedSetId~criterionId`` (use the ``resource_id``
      field from ``get_negative_keyword_list_keywords``)
    - ``campaign_asset``: ``campaignId~assetId~fieldType``
    - ``customer_asset``: ``assetId~fieldType``
    - ``asset``: bare asset ID

    This is a DESTRUCTIVE operation — removed entities cannot be re-enabled.
    Prefer ``pause_entity`` unless the user explicitly wants permanent removal.
    Call ``confirm_and_apply`` with the returned plan_id to execute.
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
    geo_exclude_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool = False,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
    ad_schedule: list[dict] | None = None,
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

    schedule_validated, schedule_errors = _validate_ad_schedule(ad_schedule or [])
    if schedule_errors:
        return {"error": "Ad schedule validation failed", "details": schedule_errors}

    geo_exclude_ids = [str(g) for g in (geo_exclude_ids or []) if str(g).strip()]
    overlap = set(geo_exclude_ids) & set(str(g) for g in (geo_target_ids or []))
    if overlap:
        return {
            "error": "geo_exclude_ids overlap with geo_target_ids",
            "details": [f"{g} appears in both include and exclude lists" for g in sorted(overlap)],
        }

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
            "geo_exclude_ids": geo_exclude_ids,
            "language_ids": language_ids or [],
            "search_partners_enabled": search_partners_enabled,
            "display_network_enabled": normalized_display_network_enabled,
            "max_cpc": max_cpc if max_cpc else None,
            "ad_schedule": schedule_validated,
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
    geo_exclude_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool | None = None,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft an update to an existing campaign — returns preview, does NOT execute.

    All parameters except campaign_id are optional — only include what you want
    to change. Geo/language targets, geo exclusions, and ad schedule are
    REPLACED entirely when provided (existing entries are removed first).

    Pass an empty list (e.g. ``geo_exclude_ids=[]``) to clear that field.
    Pass ``None`` (default) to leave it unchanged.
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

    cleaned_excl: list[str] | None = None
    if geo_exclude_ids is not None:
        cleaned_excl = [str(g).strip() for g in geo_exclude_ids if str(g).strip()]
        if geo_target_ids is not None:
            overlap = set(cleaned_excl) & set(str(g) for g in geo_target_ids)
            if overlap:
                errors.append(
                    "geo_exclude_ids overlap with geo_target_ids: "
                    + ", ".join(sorted(overlap))
                )

    schedule_validated: list[dict] | None = None
    if ad_schedule is not None:
        schedule_validated, schedule_errors = _validate_ad_schedule(ad_schedule)
        errors.extend(schedule_errors)
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
        geo_exclude_ids is not None,
        language_ids is not None,
        search_partners_enabled is not None,
        normalized_display_network_enabled is not None,
        max_cpc,
        ad_schedule is not None,
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
    if cleaned_excl is not None:
        changes["geo_exclude_ids"] = cleaned_excl
    if language_ids is not None:
        changes["language_ids"] = language_ids
    if search_partners_enabled is not None:
        changes["search_partners_enabled"] = search_partners_enabled
    if normalized_display_network_enabled is not None:
        changes["display_network_enabled"] = normalized_display_network_enabled
    if max_cpc:
        changes["max_cpc"] = max_cpc
    if schedule_validated is not None:
        changes["ad_schedule"] = schedule_validated

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
    """Draft callout assets — returns a PREVIEW.

    Scope:
        - If ``campaign_id`` is provided, the callouts are linked at the
          campaign level via ``CampaignAsset``.
        - If ``campaign_id`` is empty, the callouts are linked at the
          customer/account level via ``CustomerAsset`` and become available
          to all eligible campaigns automatically.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_callouts", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_callouts, errors = _validate_callouts(callouts or [])
    if errors:
        return {"error": "Validation failed", "details": errors}

    scope = "campaign" if campaign_id else "customer"
    plan = ChangePlan(
        operation="create_callouts",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
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
    """Draft structured snippet assets — returns a PREVIEW.

    Scope:
        - If ``campaign_id`` is provided, snippets attach at the campaign level.
        - If ``campaign_id`` is empty, snippets attach at the customer/account
          level and apply to all eligible campaigns by default.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_structured_snippets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_snippets, errors = _validate_structured_snippets(snippets or [])
    if errors:
        return {"error": "Validation failed", "details": errors}

    scope = "campaign" if campaign_id else "customer"
    plan = ChangePlan(
        operation="create_structured_snippets",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
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
    field_types: list[str] | None = None,
) -> dict:
    """Draft image assets from local PNG/JPEG/GIF files — returns a PREVIEW.

    Scope:
        - If ``campaign_id`` is empty, images attach at the customer/account
          level via CustomerAsset.
        - If ``campaign_id`` is provided, images attach at that campaign via
          CampaignAsset.

    Field type:
        Each image gets an AssetFieldType chosen from its aspect ratio
        (with a 'logo' filename hint):
            1:1 → SQUARE_MARKETING_IMAGE (or BUSINESS_LOGO if 'logo' in name)
            1.91:1 → MARKETING_IMAGE
            4:1 → LANDSCAPE_LOGO (logo hint required)
            4:5 → PORTRAIT_MARKETING_IMAGE
        Pass ``field_types`` (one entry per image_path) to override the
        auto-detection. Valid override values: MARKETING_IMAGE,
        SQUARE_MARKETING_IMAGE, PORTRAIT_MARKETING_IMAGE,
        TALL_PORTRAIT_MARKETING_IMAGE, LOGO, LANDSCAPE_LOGO, BUSINESS_LOGO.

    Note: AD_IMAGE is NOT a valid field type for direct asset linking —
    Google's API rejects it. The tool maps to the modern marketing-image
    types instead.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_image_assets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_images, errors = _validate_image_assets(image_paths or [])
    if errors:
        return {"error": "Validation failed", "details": errors}

    if field_types is not None:
        if len(field_types) != len(validated_images):
            return {
                "error": "Validation failed",
                "details": [
                    f"field_types has {len(field_types)} entries but "
                    f"image_paths has {len(validated_images)}"
                ],
            }
        for ft, img in zip(field_types, validated_images):
            if ft and str(ft).upper() not in _VALID_IMAGE_FIELD_TYPES:
                return {
                    "error": "Validation failed",
                    "details": [
                        f"field_type {ft!r} is not a supported image asset "
                        f"field type. Valid: {sorted(_VALID_IMAGE_FIELD_TYPES)}"
                    ],
                }
            if ft:
                img["field_type"] = str(ft).upper()

    # Compute the field type each image will resolve to and attach to
    # the preview so the user can see it before applying.
    for img in validated_images:
        img["resolved_field_type"] = _detect_image_field_type(img)

    scope = "campaign" if campaign_id else "customer"
    plan = ChangePlan(
        operation="create_image_assets",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
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
    """Draft sitelink extensions — returns a PREVIEW, does NOT execute.

    Scope:
        - If ``campaign_id`` is provided, sitelinks attach at the campaign level.
        - If ``campaign_id`` is empty, sitelinks attach at the customer/account
          level and apply to all eligible campaigns by default.

    sitelinks: list of dicts, each with:
        - link_text (str, required, max 25 chars) — the clickable text
        - final_url (str, required) — where the sitelink points
        - description1 (str, optional, max 35 chars) — first description line
        - description2 (str, optional, max 35 chars) — second description line
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_sitelinks", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

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

    scope = "campaign" if campaign_id else "customer"
    plan = ChangePlan(
        operation="create_sitelinks",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "sitelinks": validated,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


# Country dialing codes for E.164 phone normalization.
_COUNTRY_DIAL_CODES = {
    "US": "+1", "CA": "+1", "GB": "+44", "DE": "+49", "FR": "+33",
    "IT": "+39", "ES": "+34", "NL": "+31", "BE": "+32", "AT": "+43",
    "CH": "+41", "AU": "+61", "NZ": "+64", "IE": "+353", "PT": "+351",
}


def _normalize_phone_e164(phone: str, country_code: str) -> tuple[str, str | None]:
    """Return (normalized, error_or_None). Strips formatting, ensures + prefix.

    Handles two trunk-prefix patterns:
      - North America (US/CA): leading "1" before a 10-digit number is the
        country code; strip it before re-adding "+1".
      - European trunk "0": GB/DE/FR/IT/ES/NL/BE/AT/CH/IE/PT/AU/NZ all use a
        leading "0" for domestic dialing that must be removed when adding
        the international prefix.
    """
    raw = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if not raw:
        return "", "phone_number is empty after stripping formatting"
    if raw.startswith("+"):
        return raw, None
    dial = _COUNTRY_DIAL_CODES.get(country_code.upper())
    if not dial:
        return "", (
            f"country_code '{country_code}' is not in the dial-code map; "
            f"pass phone in E.164 form (with leading '+')"
        )
    cc_upper = country_code.upper()
    if cc_upper in ("US", "CA") and len(raw) == 11 and raw.startswith("1"):
        raw = raw[1:]
    elif cc_upper not in ("US", "CA") and raw.startswith("0"):
        raw = raw.lstrip("0")
    return f"{dial}{raw}", None


def draft_call_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    phone_number: str = "",
    country_code: str = "US",
    campaign_id: str = "",
    call_conversion_action_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft a call asset (phone extension) — returns a PREVIEW.

    Scope:
        - If ``campaign_id`` is provided, the call asset attaches to that
          campaign via ``CampaignAsset``.
        - If ``campaign_id`` is empty, the call asset attaches at the
          customer/account level via ``CustomerAsset``.

    phone_number: human or E.164 (e.g. "+19163393676" or "(916) 339-3676").
    country_code: 2-letter ISO country code used to canonicalize a national
        number to E.164. Ignored when phone_number already starts with '+'.
    call_conversion_action_id: optional Google Ads conversion action ID to
        count calls of qualifying duration (typically ≥60 sec). When omitted,
        the call asset uses the default account-level call-conversion settings.
    ad_schedule: optional schedule dict list — see add_ad_schedule for shape
        (day_of_week, start_hour/minute, end_hour/minute). Used to limit the
        hours when the call extension shows.

    Important: Google Ads requires manual phone-number verification before
    the call asset can serve. The asset is created in the account but won't
    show until verification completes in the Ads UI.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_call_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not phone_number:
        return {"error": "phone_number is required"}

    normalized_phone, phone_err = _normalize_phone_e164(phone_number, country_code)
    if phone_err:
        return {"error": phone_err}

    schedule_validated, schedule_errors = _validate_ad_schedule(ad_schedule or [])
    if schedule_errors:
        return {"error": "Ad schedule validation failed", "details": schedule_errors}

    scope = "campaign" if campaign_id else "customer"
    warnings = [
        "Google Ads requires phone-number verification before call assets serve. "
        "Complete verification in Ads UI → Tools → Assets → Calls."
    ]

    plan = ChangePlan(
        operation="create_call_asset",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "phone_number": normalized_phone,
            "country_code": country_code.upper(),
            "call_conversion_action_id": call_conversion_action_id,
            "ad_schedule": schedule_validated,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


# ---------------------------------------------------------------------------
# Promotion assets
# ---------------------------------------------------------------------------

# Pulled dynamically from the google-ads SDK so we don't drift when
# Google adds new occasions / modifiers in a future API version.
from adloop.ads.enums import enum_names as _enum_names

_VALID_PROMOTION_OCCASIONS = _enum_names("PromotionExtensionOccasionEnum")
_VALID_DISCOUNT_MODIFIERS = _enum_names("PromotionExtensionDiscountModifierEnum")


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
    ad_schedule: list[dict] | None,
) -> tuple[dict, list[str]]:
    """Validate every PromotionAsset field. Returns (normalized, errors)."""
    errors: list[str] = []
    target = (promotion_target or "").strip()
    url = (final_url or "").strip()

    if not target:
        errors.append("promotion_target is required")
    elif len(target) > 20:
        errors.append(
            f"promotion_target '{target}' is {len(target)} chars (max 20)"
        )

    if not url:
        errors.append("final_url is required")

    has_money = money_off and money_off > 0
    has_percent = percent_off and percent_off > 0
    if has_money and has_percent:
        errors.append(
            "Specify exactly one of money_off or percent_off, not both"
        )
    elif not has_money and not has_percent:
        errors.append(
            "One of money_off or percent_off is required (must be > 0)"
        )

    if has_percent and (percent_off <= 0 or percent_off > 100):
        errors.append(f"percent_off must be in (0, 100]; got {percent_off}")

    code = (promotion_code or "").strip()
    if code and len(code) > 15:
        errors.append(
            f"promotion_code '{code}' is {len(code)} chars (max 15)"
        )

    has_orders_over = bool(orders_over_amount and orders_over_amount > 0)
    if code and has_orders_over:
        errors.append(
            "promotion_code and orders_over_amount are mutually exclusive "
            "(Google Ads PromotionAsset.promotion_trigger is a oneof) — "
            "specify exactly one"
        )

    occ = (occasion or "").strip().upper()
    if occ and occ not in _VALID_PROMOTION_OCCASIONS:
        errors.append(
            f"occasion '{occ}' invalid; valid values: "
            f"{sorted(_VALID_PROMOTION_OCCASIONS)}"
        )

    modifier = (discount_modifier or "").strip().upper()
    if modifier and modifier not in _VALID_DISCOUNT_MODIFIERS:
        errors.append(
            f"discount_modifier '{modifier}' invalid; valid: "
            f"{sorted(_VALID_DISCOUNT_MODIFIERS)} (or empty for none)"
        )

    for label, value in (
        ("start_date", start_date),
        ("end_date", end_date),
        ("redemption_start_date", redemption_start_date),
        ("redemption_end_date", redemption_end_date),
    ):
        if value and not _is_valid_iso_date(value):
            errors.append(f"{label} '{value}' must be YYYY-MM-DD")

    schedule_validated, schedule_errors = _validate_ad_schedule(ad_schedule or [])
    errors.extend(schedule_errors)

    if errors:
        return {}, errors

    if url:
        url_checks = _validate_urls([url])
        url_err = url_checks.get(url)
        if url_err:
            errors.append(f"final_url '{url}' is not reachable: {url_err}")
            return {}, errors

    normalized: dict = {
        "promotion_target": target,
        "final_url": url,
        "currency_code": (currency_code or "USD").upper(),
        "promotion_code": code,
        "orders_over_amount": float(orders_over_amount or 0),
        "occasion": occ,
        "discount_modifier": modifier,
        "language_code": (language_code or "en").lower(),
        "start_date": start_date or "",
        "end_date": end_date or "",
        "redemption_start_date": redemption_start_date or "",
        "redemption_end_date": redemption_end_date or "",
        "ad_schedule": schedule_validated,
    }
    if has_money:
        normalized["money_off"] = float(money_off)
        normalized["percent_off"] = 0.0
    else:
        normalized["money_off"] = 0.0
        normalized["percent_off"] = float(percent_off)

    return normalized, []


def _is_valid_iso_date(value: str) -> bool:
    """True if value parses as a YYYY-MM-DD calendar date."""
    from datetime import datetime

    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def draft_promotion(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
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
    campaign_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft a promotion extension asset — returns a PREVIEW.

    Creates a PromotionAsset and links it at campaign or customer scope.
    Exactly one of money_off / percent_off must be provided.

    Scope:
        - campaign_id provided  → CampaignAsset link.
        - campaign_id empty     → CustomerAsset link (account-level).

    Required:
        promotion_target: what the promotion is for, e.g. "Window Tint"
            (max 20 chars; this is the label Google shows in the ad).
        final_url: landing page for the promotion (must return 2xx/3xx).
        money_off OR percent_off: the discount amount.

    Optional:
        currency_code: ISO 4217 (default USD). Used for money_off and
            orders_over_amount.
        promotion_code: optional coupon code (max 15 chars).
        orders_over_amount: minimum order amount that unlocks the promo.
        occasion: optional event tag — e.g. BLACK_FRIDAY, SUMMER_SALE.
            See PromotionExtensionOccasion enum for the full list.
        discount_modifier: optional modifier; "UP_TO" surfaces as
            "Up to $X off" instead of "$X off".
        language_code: BCP-47 (default "en").
        start_date / end_date: YYYY-MM-DD. Leave blank for always-on.
        redemption_start_date / redemption_end_date: YYYY-MM-DD.
        ad_schedule: optional list of {day_of_week, start_hour, end_hour,
            start_minute, end_minute} entries restricting when the promo
            shows.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_promotion", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    normalized, errors = _validate_promotion_inputs(
        promotion_target=promotion_target,
        final_url=final_url,
        money_off=money_off,
        percent_off=percent_off,
        currency_code=currency_code,
        promotion_code=promotion_code,
        orders_over_amount=orders_over_amount,
        occasion=occasion,
        discount_modifier=discount_modifier,
        language_code=language_code,
        start_date=start_date,
        end_date=end_date,
        redemption_start_date=redemption_start_date,
        redemption_end_date=redemption_end_date,
        ad_schedule=ad_schedule,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    scope = "campaign" if campaign_id else "customer"
    plan = ChangePlan(
        operation="create_promotion",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "promotion": normalized,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def update_promotion(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    campaign_id: str = "",
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
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Update a promotion via swap — returns a PREVIEW.

    PromotionAsset fields are immutable once created in the Google Ads
    API, so "update" is implemented as a swap:
        1. Create a new PromotionAsset with the updated values.
        2. Link the new asset at the same scope.
        3. Unlink the old asset.

    The old Asset row itself stays in the account (orphaned). The Ads
    API does not support hard-deleting Asset rows; Google reclaims
    orphaned assets in due course.

    asset_id: numeric ID of the existing PromotionAsset to replace.
        Find it via: SELECT asset.id, asset.name, asset.promotion_asset.promotion_target
                     FROM asset WHERE asset.type = 'PROMOTION'
    campaign_id: pass to scope BOTH the new and old links to that campaign.
        Leave empty for customer/account-level scope (matches CustomerAsset
        behavior of the original promotion).

    All other fields: see draft_promotion docstring.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_promotion", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not asset_id:
        return {"error": "asset_id is required (the existing PromotionAsset to replace)"}

    normalized, errors = _validate_promotion_inputs(
        promotion_target=promotion_target,
        final_url=final_url,
        money_off=money_off,
        percent_off=percent_off,
        currency_code=currency_code,
        promotion_code=promotion_code,
        orders_over_amount=orders_over_amount,
        occasion=occasion,
        discount_modifier=discount_modifier,
        language_code=language_code,
        start_date=start_date,
        end_date=end_date,
        redemption_start_date=redemption_start_date,
        redemption_end_date=redemption_end_date,
        ad_schedule=ad_schedule,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    scope = "campaign" if campaign_id else "customer"
    warnings = [
        "Update is a swap: a new PromotionAsset is created and linked, "
        "the old link is unlinked. The old Asset row stays in the account "
        "(orphaned) — Google Ads API does not support deleting Asset rows."
    ]

    plan = ChangePlan(
        operation="update_promotion",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "old_asset_id": asset_id,
            "promotion": normalized,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


# ---------------------------------------------------------------------------
# In-place asset updates (call asset, sitelink, callout)
# ---------------------------------------------------------------------------


_VALID_CALL_REPORTING_STATES = _enum_names("CallConversionReportingStateEnum")


def update_call_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str,
    phone_number: str = "",
    country_code: str = "",
    call_conversion_action_id: str = "",
    call_conversion_reporting_state: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Update an existing CallAsset in place — returns a PREVIEW.

    Use this to:
      - re-point a CallAsset at a specific conversion action (e.g. 'Calls
        from Ads (>=90s)') with USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
      - change the phone number / country code
      - replace the ad-schedule windows

    Pass only the fields you want to change. Empty strings/None are
    treated as "do not change".

    asset_id: numeric ID of the existing call asset.
    call_conversion_reporting_state: one of
        DISABLED | USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION |
        USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_call_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not asset_id:
        return {"error": "asset_id is required"}

    errors: list[str] = []
    normalized_phone = ""
    if phone_number:
        cc = (country_code or "US").upper()
        normalized_phone, phone_err = _normalize_phone_e164(phone_number, cc)
        if phone_err:
            errors.append(phone_err)

    if call_conversion_reporting_state and (
        call_conversion_reporting_state not in _VALID_CALL_REPORTING_STATES
    ):
        errors.append(
            f"call_conversion_reporting_state '{call_conversion_reporting_state}'"
            f" invalid; valid: {sorted(_VALID_CALL_REPORTING_STATES)}"
        )

    schedule_validated, schedule_errors = _validate_ad_schedule(ad_schedule or [])
    errors.extend(schedule_errors)

    if errors:
        return {"error": "Validation failed", "details": errors}

    changes: dict = {"asset_id": str(asset_id)}
    if normalized_phone:
        changes["phone_number"] = normalized_phone
    if country_code:
        changes["country_code"] = country_code.upper()
    if call_conversion_action_id:
        changes["call_conversion_action_id"] = str(call_conversion_action_id)
    if call_conversion_reporting_state:
        changes["call_conversion_reporting_state"] = call_conversion_reporting_state
    if ad_schedule is not None:
        changes["ad_schedule"] = schedule_validated

    if len(changes) == 1:
        return {"error": "No fields to update"}

    plan = ChangePlan(
        operation="update_call_asset",
        entity_type="asset",
        entity_id=str(asset_id),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def update_sitelink(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str,
    link_text: str = "",
    final_url: str = "",
    description1: str = "",
    description2: str = "",
) -> dict:
    """Update an existing SitelinkAsset in place — returns a PREVIEW.

    Pass only the fields you want to change. Empty string = "do not change".

    asset_id: numeric ID of the existing sitelink asset.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_sitelink", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not asset_id:
        return {"error": "asset_id is required"}

    errors: list[str] = []
    if link_text and len(link_text) > 25:
        errors.append(
            f"link_text '{link_text}' is {len(link_text)} chars (max 25)"
        )
    if description1 and len(description1) > 35:
        errors.append(
            f"description1 is {len(description1)} chars (max 35)"
        )
    if description2 and len(description2) > 35:
        errors.append(
            f"description2 is {len(description2)} chars (max 35)"
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    if final_url:
        url_checks = _validate_urls([final_url])
        url_err = url_checks.get(final_url)
        if url_err:
            return {
                "error": "URL validation failed",
                "details": [f"'{final_url}' is not reachable: {url_err}"],
            }

    changes: dict = {"asset_id": str(asset_id)}
    if link_text:
        changes["link_text"] = link_text
    if final_url:
        changes["final_url"] = final_url
    if description1:
        changes["description1"] = description1
    if description2:
        changes["description2"] = description2

    if len(changes) == 1:
        return {"error": "No fields to update"}

    plan = ChangePlan(
        operation="update_sitelink",
        entity_type="asset",
        entity_id=str(asset_id),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def update_callout(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str,
    callout_text: str,
) -> dict:
    """Update an existing CalloutAsset's text in place — returns a PREVIEW.

    asset_id: numeric ID of the existing callout asset.
    callout_text: new callout text (max 25 chars).
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_callout", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not asset_id:
        return {"error": "asset_id is required"}
    text = (callout_text or "").strip()
    if not text:
        return {"error": "callout_text is required"}
    if len(text) > 25:
        return {
            "error": "Validation failed",
            "details": [f"callout_text is {len(text)} chars (max 25)"],
        }

    plan = ChangePlan(
        operation="update_callout",
        entity_type="asset",
        entity_id=str(asset_id),
        customer_id=customer_id,
        changes={"asset_id": str(asset_id), "callout_text": text},
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# link_asset_to_customer — promote existing assets to customer/account scope
# ---------------------------------------------------------------------------

# AssetFieldType values that are valid for CustomerAsset (account-level).
# Asset types like SITELINK/CALLOUT/etc. are also valid here, but this tool
# is intended for "promote existing asset" use cases — typically images,
# logos, and business name assets that already exist in the account from
# legacy campaigns.
_VALID_CUSTOMER_ASSET_FIELD_TYPES = {
    "SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "PROMOTION", "PRICE",
    "CALL", "MOBILE_APP", "HOTEL_CALLOUT", "BUSINESS_LOGO", "BUSINESS_NAME",
    "AD_IMAGE", "MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE",
    "PORTRAIT_MARKETING_IMAGE", "LOGO", "LANDSCAPE_LOGO",
    "YOUTUBE_VIDEO", "MEDIA_BUNDLE", "BOOK_ON_GOOGLE", "LEAD_FORM",
    "HEADLINE", "DESCRIPTION", "LONG_HEADLINE",
}


def link_asset_to_customer(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    links: list[dict] | None = None,
) -> dict:
    """Link EXISTING assets to the customer (account) — returns a PREVIEW.

    Use this to "promote" assets that already exist in the account
    (typically attached to legacy campaigns) so they apply at the account
    level and inherit to every eligible campaign automatically.

    Unlike draft_image_assets / draft_callouts / etc., this tool does NOT
    create new Asset rows — it only adds CustomerAsset link rows pointing
    to assets you already have. Find candidate asset_ids via:
        SELECT asset.id, asset.type, asset.name FROM asset

    Args:
        links: list of dicts, each with:
            - asset_id (str, required) — numeric asset ID
            - field_type (str, required) — AssetFieldType, e.g.
              BUSINESS_LOGO, AD_IMAGE, MARKETING_IMAGE, BUSINESS_NAME,
              SITELINK, CALLOUT, CALL, PROMOTION, etc.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("link_asset_to_customer", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not links:
        return {"error": "At least one link is required"}

    errors: list[str] = []
    validated: list[dict] = []
    for i, item in enumerate(links):
        if not isinstance(item, dict):
            errors.append(f"Link {i + 1}: must be a dict, got {type(item).__name__}")
            continue
        asset_id = str(item.get("asset_id", "")).strip()
        field_type = str(item.get("field_type", "")).strip().upper()
        if not asset_id:
            errors.append(f"Link {i + 1}: asset_id is required")
            continue
        if not asset_id.isdigit():
            errors.append(
                f"Link {i + 1}: asset_id '{asset_id}' must be numeric"
            )
            continue
        if not field_type:
            errors.append(f"Link {i + 1}: field_type is required")
            continue
        if field_type not in _VALID_CUSTOMER_ASSET_FIELD_TYPES:
            errors.append(
                f"Link {i + 1}: field_type '{field_type}' is not valid for "
                f"CustomerAsset; valid: "
                f"{sorted(_VALID_CUSTOMER_ASSET_FIELD_TYPES)}"
            )
            continue
        validated.append({"asset_id": asset_id, "field_type": field_type})

    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="link_asset_to_customer",
        entity_type="customer_asset",
        entity_id=customer_id,
        customer_id=customer_id,
        changes={"links": validated},
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# confirm_and_apply — the only function that actually mutates Google Ads
# ---------------------------------------------------------------------------


def _extract_error_message(exc: Exception) -> str:
    """Extract a meaningful error message from Google Ads API exceptions.

    GoogleAdsException.__init__ doesn't call super().__init__(), so str(e)
    returns ''. This function digs into the failure proto to surface the
    actual error code, message, and trigger values.
    """
    try:
        from google.ads.googleads.errors import GoogleAdsException

        if isinstance(exc, GoogleAdsException) and exc.failure:
            parts = []
            for error in exc.failure.errors:
                error_code = error.error_code
                code_field = error_code.WhichOneof("error_code")
                code_value = getattr(error_code, code_field) if code_field else "UNKNOWN"
                line = f"[{code_field}={code_value.name if hasattr(code_value, 'name') else code_value}]"
                if error.message:
                    line += f" {error.message}"
                if error.trigger and error.trigger.string_value:
                    line += f" (trigger: {error.trigger.string_value})"
                parts.append(line)
            if parts:
                msg = "; ".join(parts)
                if exc.request_id:
                    msg += f" [request_id={exc.request_id}]"
                return msg
    except Exception:
        pass

    fallback = str(exc)
    return fallback if fallback else repr(exc)


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

    forced_by_config = bool(config.safety.require_dry_run) and not dry_run
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
        response = {
            "status": "DRY_RUN_SUCCESS",
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "changes": plan.changes,
        }
        if forced_by_config:
            # The caller passed dry_run=false but safety.require_dry_run
            # forced it back on. Tell them exactly why and how to unlock
            # real writes — without this, agents (e.g. Claude Code) retry
            # in an infinite loop because the old message said to "call
            # again with dry_run=false", which they already did.
            config_path = config.source_path or "~/.adloop/config.yaml"
            response["dry_run_forced_by"] = "config.safety.require_dry_run"
            response["config_path"] = config_path
            response["remediation"] = (
                f"Edit {config_path}, set 'require_dry_run: false' under "
                "'safety:', then restart the AdLoop MCP server. Passing "
                "dry_run=false on this tool will keep being overridden "
                "until that flag is flipped."
            )
            response["message"] = (
                f"dry_run=false was IGNORED because 'safety.require_dry_run: true' "
                f"is set in {config_path}. No changes were made. To apply real "
                f"changes, flip that flag to false and restart the AdLoop MCP "
                f"server — retrying this tool with dry_run=false alone will "
                f"never succeed while the flag is on."
            )
        else:
            response["message"] = (
                "Dry run completed — no changes were made to your Google Ads account. "
                "To apply for real, call confirm_and_apply again with dry_run=false."
            )
        return response

    try:
        result = _execute_plan(config, plan)
    except Exception as e:
        error_message = _extract_error_message(e)
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=False,
            result="error",
            error=error_message,
        )
        return {"error": error_message, "plan_id": plan.plan_id}

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
    "shared_criterion",
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


_VALID_DAYS_OF_WEEK = {
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
    "SATURDAY", "SUNDAY",
}
_VALID_MINUTES = {0, 15, 30, 45}


def _validate_ad_schedule(
    schedule: list[dict],
) -> tuple[list[dict], list[str]]:
    """Validate ad schedule entries. Returns (validated, errors).

    Each entry: {day_of_week, start_hour, end_hour, start_minute=0, end_minute=0}.
    Google Ads only accepts minutes in {0, 15, 30, 45}, hours in 0-24, and
    requires end > start. Day-of-week strings are normalized to upper-case.
    """
    errors = []
    validated = []
    for i, entry in enumerate(schedule or []):
        if not isinstance(entry, dict):
            errors.append(f"ad_schedule[{i}]: must be a dict")
            continue
        day = str(entry.get("day_of_week", "")).strip().upper()
        if day not in _VALID_DAYS_OF_WEEK:
            errors.append(
                f"ad_schedule[{i}]: day_of_week must be one of {sorted(_VALID_DAYS_OF_WEEK)}"
            )
            continue
        try:
            start_hour = int(entry.get("start_hour", -1))
            end_hour = int(entry.get("end_hour", -1))
            start_minute = int(entry.get("start_minute", 0))
            end_minute = int(entry.get("end_minute", 0))
        except (TypeError, ValueError):
            errors.append(f"ad_schedule[{i}]: hour/minute values must be integers")
            continue
        if not (0 <= start_hour <= 23):
            errors.append(f"ad_schedule[{i}]: start_hour must be in 0..23")
        if not (0 <= end_hour <= 24):
            errors.append(f"ad_schedule[{i}]: end_hour must be in 0..24")
        if start_minute not in _VALID_MINUTES:
            errors.append(
                f"ad_schedule[{i}]: start_minute must be one of {sorted(_VALID_MINUTES)}"
            )
        if end_minute not in _VALID_MINUTES:
            errors.append(
                f"ad_schedule[{i}]: end_minute must be one of {sorted(_VALID_MINUTES)}"
            )
        if (end_hour, end_minute) <= (start_hour, start_minute):
            errors.append(
                f"ad_schedule[{i}]: end ({end_hour}:{end_minute:02d}) must be after "
                f"start ({start_hour}:{start_minute:02d})"
            )
        validated.append({
            "day_of_week": day,
            "start_hour": start_hour,
            "start_minute": start_minute,
            "end_hour": end_hour,
            "end_minute": end_minute,
        })
    return validated, errors


def _validate_callouts(
    callouts: list[str],
) -> tuple[list[str], list[str]]:
    errors = []
    validated = []

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
    snippets: list[dict],
) -> tuple[list[dict], list[str]]:
    errors = []
    validated = []

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
    image_paths: list[str],
) -> tuple[list[dict[str, object]], list[str]]:
    errors = []
    validated = []

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
    headlines: list[dict],
    descriptions: list[dict],
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

    headline_pin_counts: dict[str, int] = {}
    for i, h in enumerate(headlines):
        text = h["text"]
        pin = h["pinned_field"]
        if len(text) > 30:
            errors.append(
                f"Headline {i + 1} exceeds 30 chars ({len(text)}): '{text}'"
            )
        if pin is not None:
            if pin not in _VALID_HEADLINE_PINS:
                errors.append(
                    f"Headline {i + 1} pinned_field '{pin}' invalid; "
                    f"must be one of {sorted(_VALID_HEADLINE_PINS)} or null"
                )
            else:
                headline_pin_counts[pin] = headline_pin_counts.get(pin, 0) + 1
    for pin, count in headline_pin_counts.items():
        if count > 2:
            errors.append(f"At most 2 headlines may pin to {pin}; got {count}")

    description_pin_counts: dict[str, int] = {}
    for i, d in enumerate(descriptions):
        text = d["text"]
        pin = d["pinned_field"]
        if len(text) > 90:
            errors.append(
                f"Description {i + 1} exceeds 90 chars ({len(text)}): '{text}'"
            )
        if pin is not None:
            if pin not in _VALID_DESCRIPTION_PINS:
                errors.append(
                    f"Description {i + 1} pinned_field '{pin}' invalid; "
                    f"must be one of {sorted(_VALID_DESCRIPTION_PINS)} or null"
                )
            else:
                description_pin_counts[pin] = description_pin_counts.get(pin, 0) + 1
    for pin, count in description_pin_counts.items():
        if count > 1:
            errors.append(f"At most 1 description may pin to {pin}; got {count}")

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


_MUTATE_RESPONSE_RESULT_FIELDS = [
    "campaign_budget_result",
    "campaign_result",
    "ad_group_result",
    "ad_group_ad_result",
    "ad_group_criterion_result",
    "campaign_criterion_result",
    "asset_result",
    "campaign_asset_result",
    "customer_asset_result",
]


def _extract_resource_name(resp: object) -> str:
    """Extract the resource_name from a MutateOperationResponse.

    Uses direct field access instead of WhichOneof, which doesn't work on
    proto-plus wrapped messages returned by the google-ads library.
    """
    for field in _MUTATE_RESPONSE_RESULT_FIELDS:
        try:
            result = getattr(resp, field, None)
            if result and result.resource_name:
                return result.resource_name
        except Exception:
            continue
    return ""


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
        "update_responsive_search_ad": _apply_update_rsa,
        "add_keywords": _apply_add_keywords,
        "add_negative_keywords": _apply_add_negative_keywords,
        "create_negative_keyword_list": _apply_create_negative_keyword_list,
        "add_to_negative_keyword_list": _apply_add_to_negative_keyword_list,
        "pause_entity": _apply_status_change,
        "enable_entity": _apply_status_change,
        "remove_entity": _apply_remove,
        "create_callouts": _apply_create_callouts,
        "create_structured_snippets": _apply_create_structured_snippets,
        "create_image_assets": _apply_create_image_assets,
        "create_sitelinks": _apply_create_sitelinks,
        "create_call_asset": _apply_create_call_asset,
        "create_location_asset": _apply_create_location_asset,
        "create_business_name_asset": _apply_create_business_name_asset,
        "create_promotion": _apply_create_promotion,
        "update_promotion": _apply_update_promotion,
        "link_asset_to_customer": _apply_link_asset_to_customer,
        "update_call_asset": _apply_update_call_asset,
        "update_sitelink": _apply_update_sitelink,
        "update_callout": _apply_update_callout,
        "create_conversion_action": _apply_create_conversion_action_route,
        "update_conversion_action": _apply_update_conversion_action_route,
        "remove_conversion_action": _apply_remove_conversion_action_route,
        "add_ad_schedule": _apply_add_ad_schedule,
        "add_geo_exclusions": _apply_add_geo_exclusions,
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

    # 7. Geo exclusions (negative CampaignCriterion location records)
    for geo_id in changes.get("geo_exclude_ids") or []:
        op = client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = campaign_service.campaign_path(cid, "-2")
        crit.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        crit.negative = True
        operations.append(op)

    # 8. Ad schedule (CampaignCriterion AdScheduleInfo records)
    for entry in changes.get("ad_schedule") or []:
        op = client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = campaign_service.campaign_path(cid, "-2")
        _populate_ad_schedule_info(client, crit.ad_schedule, entry)
        operations.append(op)

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results = {}
    num_keywords = len(kw_list)
    num_geo = len(changes.get("geo_target_ids") or [])
    num_lang = len(changes.get("language_ids") or [])
    num_excl = len(changes.get("geo_exclude_ids") or [])
    num_sched = len(changes.get("ad_schedule") or [])
    for i, resp in enumerate(response.mutate_operation_responses):
        rn = _extract_resource_name(resp)
        if not rn:
            continue
        if i == 0:
            results["campaign_budget"] = rn
        elif i == 1:
            results["campaign"] = rn
        elif i == 2:
            results["ad_group"] = rn
        elif i < 3 + num_keywords:
            results.setdefault("keywords", []).append(rn)
        elif i < 3 + num_keywords + num_geo:
            results.setdefault("geo_targets", []).append(rn)
        elif i < 3 + num_keywords + num_geo + num_lang:
            results.setdefault("language_targets", []).append(rn)
        elif i < 3 + num_keywords + num_geo + num_lang + num_excl:
            results.setdefault("geo_excludes", []).append(rn)
        elif i < 3 + num_keywords + num_geo + num_lang + num_excl + num_sched:
            results.setdefault("ad_schedule", []).append(rn)

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
        rn = _extract_resource_name(resp)
        if rn:
            if i == 0:
                results["ad_group"] = rn
            else:
                results.setdefault("keywords", []).append(rn)

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

    # Geo exclusions — remove existing negative-location criteria, add new
    excl_ids = changes.get("geo_exclude_ids")
    if excl_ids is not None:
        existing_excl = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = 'LOCATION'
              AND campaign_criterion.negative = TRUE
        """
        for row in service.search(customer_id=cid, query=existing_excl):
            rm_op = client.get_type("MutateOperation")
            rm_op.campaign_criterion_operation.remove = (
                row.campaign_criterion.resource_name
            )
            operations.append(rm_op)

        for geo_id in excl_ids:
            add_op = client.get_type("MutateOperation")
            criterion = add_op.campaign_criterion_operation.create
            criterion.campaign = resource_name
            criterion.location.geo_target_constant = (
                f"geoTargetConstants/{geo_id}"
            )
            criterion.negative = True
            operations.append(add_op)

    # Ad schedule — remove existing schedule criteria, add new
    schedule = changes.get("ad_schedule")
    if schedule is not None:
        existing_sched = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = 'AD_SCHEDULE'
        """
        for row in service.search(customer_id=cid, query=existing_sched):
            rm_op = client.get_type("MutateOperation")
            rm_op.campaign_criterion_operation.remove = (
                row.campaign_criterion.resource_name
            )
            operations.append(rm_op)

        for entry in schedule:
            add_op = client.get_type("MutateOperation")
            criterion = add_op.campaign_criterion_operation.create
            criterion.campaign = resource_name
            _populate_ad_schedule_info(client, criterion.ad_schedule, entry)
            operations.append(add_op)

    if not operations:
        return {"message": "No changes to apply"}

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results = {"updated": []}
    for resp in response.mutate_operation_responses:
        rn = _extract_resource_name(resp)
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

    for entry in changes["headlines"]:
        asset = client.get_type("AdTextAsset")
        asset.text = entry["text"]
        if entry.get("pinned_field"):
            asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[
                entry["pinned_field"]
            ]
        ad.responsive_search_ad.headlines.append(asset)

    for entry in changes["descriptions"]:
        asset = client.get_type("AdTextAsset")
        asset.text = entry["text"]
        if entry.get("pinned_field"):
            asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[
                entry["pinned_field"]
            ]
        ad.responsive_search_ad.descriptions.append(asset)

    if changes.get("path1"):
        ad.responsive_search_ad.path1 = changes["path1"]
    if changes.get("path2"):
        ad.responsive_search_ad.path2 = changes["path2"]

    response = service.mutate_ad_group_ads(
        customer_id=cid, operations=[operation]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_update_rsa(client: object, cid: str, changes: dict) -> dict:
    """Update mutable fields on an existing RSA in place.

    Builds a sparse AdOperation.update with only the fields the caller asked
    to change, attached to a FieldMask so Google Ads ignores everything else.
    Verified mutable on RSAs in API v23: ``final_urls``, ``responsive_search_ad.path1``,
    ``responsive_search_ad.path2``.
    """
    from google.protobuf import field_mask_pb2

    service = client.get_service("AdService")
    operation = client.get_type("AdOperation")
    ad = operation.update
    ad.resource_name = service.ad_path(cid, changes["ad_id"])

    field_paths: list[str] = []

    if "final_url" in changes:
        ad.final_urls.append(changes["final_url"])
        field_paths.append("final_urls")

    if "path1" in changes:
        ad.responsive_search_ad.path1 = changes["path1"]
        field_paths.append("responsive_search_ad.path1")

    if "path2" in changes:
        ad.responsive_search_ad.path2 = changes["path2"]
        field_paths.append("responsive_search_ad.path2")

    operation.update_mask = field_mask_pb2.FieldMask(paths=field_paths)
    response = service.mutate_ads(customer_id=cid, operations=[operation])
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

    elif entity_type == "shared_criterion":
        if "~" not in entity_id:
            raise ValueError(
                f"shared_criterion entity_id must be "
                f"'sharedSetId~criterionId', got '{entity_id}'"
            )
        service = client.get_service("SharedCriterionService")
        operation = client.get_type("SharedCriterionOperation")
        operation.remove = f"customers/{cid}/sharedCriteria/{entity_id}"
        response = service.mutate_shared_criteria(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "campaign_asset":
        parts = entity_id.split("~")
        if len(parts) != 3:
            raise ValueError(
                f"campaign_asset entity_id must be "
                f"'campaignId~assetId~fieldType', got '{entity_id}'"
            )
        resource_name = f"customers/{cid}/campaignAssets/{entity_id}"
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
        parts = entity_id.split("~")
        if len(parts) != 2:
            raise ValueError(
                f"customer_asset entity_id must be "
                f"'assetId~fieldType', got '{entity_id}'"
            )
        resource_name = f"customers/{cid}/customerAssets/{entity_id}"
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
    """Create assets and link them to a campaign via CampaignAsset (legacy alias)."""
    return _apply_assets(
        client,
        cid,
        assets,
        field_type,
        populate_asset,
        scope="campaign",
        campaign_id=campaign_id,
    )


def _apply_assets(
    client: object,
    cid: str,
    assets: list[dict],
    field_type: object,
    populate_asset: object,
    *,
    scope: str = "campaign",
    campaign_id: str = "",
) -> dict:
    """Create Asset rows + link them at campaign or customer scope.

    scope:
        - "campaign" → CampaignAsset (requires campaign_id)
        - "customer" → CustomerAsset (account-level, applies to all eligible
          campaigns by default)
    """
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []

    for i, payload in enumerate(assets):
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = asset_service.asset_path(cid, str(-(i + 1)))
        populate_asset(asset, payload)
        operations.append(op)

    if scope == "campaign":
        if not campaign_id:
            raise ValueError("campaign_id is required for campaign-scope assets")
        for i in range(len(assets)):
            op = client.get_type("MutateOperation")
            ca = op.campaign_asset_operation.create
            ca.asset = asset_service.asset_path(cid, str(-(i + 1)))
            ca.campaign = googleads_service.campaign_path(cid, campaign_id)
            ca.field_type = field_type
            operations.append(op)
    elif scope == "customer":
        for i in range(len(assets)):
            op = client.get_type("MutateOperation")
            cust_asset = op.customer_asset_operation.create
            cust_asset.asset = asset_service.asset_path(cid, str(-(i + 1)))
            cust_asset.field_type = field_type
            operations.append(op)
    else:
        raise ValueError(f"Unknown asset scope: {scope}")

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    if scope == "campaign":
        results = {"assets": [], "campaign_assets": []}
        link_key = "campaign_assets"
    else:
        results = {"assets": [], "customer_assets": []}
        link_key = "customer_assets"

    num_assets = len(assets)
    for i, resp in enumerate(response.mutate_operation_responses):
        resource = None
        if resp.asset_result.resource_name:
            resource = resp.asset_result.resource_name
        elif scope == "campaign" and resp.campaign_asset_result.resource_name:
            resource = resp.campaign_asset_result.resource_name
        elif scope == "customer" and resp.customer_asset_result.resource_name:
            resource = resp.customer_asset_result.resource_name

        if resource:
            if i < num_assets:
                results["assets"].append(resource)
            else:
                results[link_key].append(resource)

    return results


def _apply_create_callouts(client: object, cid: str, changes: dict) -> dict:
    """Create callout assets at customer or campaign scope."""

    def populate(asset: object, payload: dict) -> None:
        asset.callout_asset.callout_text = payload["callout_text"]

    assets = [{"callout_text": text} for text in changes["callouts"]]
    return _apply_assets(
        client,
        cid,
        assets,
        client.enums.AssetFieldTypeEnum.CALLOUT,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
    )


def _apply_create_structured_snippets(
    client: object, cid: str, changes: dict
) -> dict:
    """Create structured snippet assets at customer or campaign scope."""

    def populate(asset: object, payload: dict) -> None:
        asset.structured_snippet_asset.header = payload["header"]
        asset.structured_snippet_asset.values.extend(payload["values"])

    return _apply_assets(
        client,
        cid,
        changes["snippets"],
        client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
    )


_VALID_IMAGE_FIELD_TYPES = {
    "MARKETING_IMAGE",
    "SQUARE_MARKETING_IMAGE",
    "PORTRAIT_MARKETING_IMAGE",
    "TALL_PORTRAIT_MARKETING_IMAGE",
    "LOGO",
    "LANDSCAPE_LOGO",
    "BUSINESS_LOGO",
}


def _detect_image_field_type(payload: dict) -> str:
    """Pick the best AssetFieldType (string) for an image based on aspect
    ratio and a filename hint.

    Google rejects ``AD_IMAGE`` for direct campaign/customer asset links —
    image extensions need ``MARKETING_IMAGE``, ``SQUARE_MARKETING_IMAGE``,
    or one of the LOGO variants. This helper picks the field type the
    asset link service will actually accept.

    payload may include an explicit ``"field_type"`` to override detection.
    """
    explicit = payload.get("field_type")
    if explicit:
        upper = str(explicit).upper()
        if upper not in _VALID_IMAGE_FIELD_TYPES:
            raise ValueError(
                f"field_type '{explicit}' is not a supported image asset "
                f"field type. Valid: {sorted(_VALID_IMAGE_FIELD_TYPES)}"
            )
        return upper

    width = int(payload.get("width", 0))
    height = int(payload.get("height", 0))
    name_lower = str(payload.get("name", "")).lower()
    path_lower = str(payload.get("path", "")).lower()
    is_logo_hint = "logo" in name_lower or "logo" in path_lower

    if width <= 0 or height <= 0:
        return "MARKETING_IMAGE"

    ratio = width / height

    if 0.95 <= ratio <= 1.05:
        return "BUSINESS_LOGO" if is_logo_hint else "SQUARE_MARKETING_IMAGE"
    if 3.5 <= ratio <= 4.5 and is_logo_hint:
        return "LANDSCAPE_LOGO"
    if 1.65 <= ratio <= 2.15:
        return "MARKETING_IMAGE"
    if 0.7 <= ratio <= 0.85:
        return "PORTRAIT_MARKETING_IMAGE"
    if 0.4 <= ratio < 0.7:
        return "TALL_PORTRAIT_MARKETING_IMAGE"
    # Fallback: treat anything wider than tall as marketing image
    return "MARKETING_IMAGE" if ratio >= 1.0 else "PORTRAIT_MARKETING_IMAGE"


def _apply_create_image_assets(client: object, cid: str, changes: dict) -> dict:
    """Create image assets from local files and link them at customer or
    campaign scope.

    Field type is auto-detected per image from aspect ratio (with a 'logo'
    filename hint), or you can override per-image via ``payload['field_type']``.
    """
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    images = changes["images"]
    scope = changes.get("scope", "campaign")
    campaign_id = changes.get("campaign_id", "")

    if scope == "campaign" and not campaign_id:
        raise ValueError("campaign_id is required for campaign-scope image assets")

    operations: list = []

    # Phase 1 — create Asset rows
    for i, payload in enumerate(images):
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = asset_service.asset_path(cid, str(-(i + 1)))
        image_path = Path(str(payload["path"]))
        image_bytes = image_path.read_bytes()
        mime_type_name = _VALID_IMAGE_MIME_TYPES[str(payload["mime_type"])]
        asset.name = str(
            payload.get("name") or _build_image_asset_name(image_path, image_bytes)
        )
        asset.type_ = client.enums.AssetTypeEnum.IMAGE
        asset.image_asset.data = image_bytes
        asset.image_asset.mime_type = getattr(
            client.enums.MimeTypeEnum, mime_type_name
        )
        asset.image_asset.full_size.width_pixels = int(payload["width"])
        asset.image_asset.full_size.height_pixels = int(payload["height"])
        operations.append(op)

    # Phase 2 — link each asset with its detected/explicit field type
    for i, payload in enumerate(images):
        ft_name = _detect_image_field_type(payload)
        ft_enum = getattr(client.enums.AssetFieldTypeEnum, ft_name)
        op = client.get_type("MutateOperation")
        if scope == "campaign":
            link = op.campaign_asset_operation.create
            link.asset = asset_service.asset_path(cid, str(-(i + 1)))
            link.campaign = googleads_service.campaign_path(cid, campaign_id)
            link.field_type = ft_enum
        elif scope == "customer":
            link = op.customer_asset_operation.create
            link.asset = asset_service.asset_path(cid, str(-(i + 1)))
            link.field_type = ft_enum
        else:
            raise ValueError(f"Unknown asset scope: {scope}")
        operations.append(op)

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    results: dict = {
        "assets": [],
        "campaign_assets": [] if scope == "campaign" else None,
        "customer_assets": [] if scope == "customer" else None,
        "field_types": [_detect_image_field_type(p) for p in images],
    }
    # Drop the None side
    if scope == "campaign":
        results.pop("customer_assets", None)
    else:
        results.pop("campaign_assets", None)

    num_images = len(images)
    link_key = "campaign_assets" if scope == "campaign" else "customer_assets"
    for i, resp in enumerate(response.mutate_operation_responses):
        resource = None
        if resp.asset_result.resource_name:
            resource = resp.asset_result.resource_name
        elif scope == "campaign" and resp.campaign_asset_result.resource_name:
            resource = resp.campaign_asset_result.resource_name
        elif scope == "customer" and resp.customer_asset_result.resource_name:
            resource = resp.customer_asset_result.resource_name
        if resource:
            if i < num_images:
                results["assets"].append(resource)
            else:
                results[link_key].append(resource)
    return results


def draft_location_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    business_profile_account_id: str = "",
    asset_set_name: str = "",
    campaign_id: str = "",
    label_filters: list[str] | None = None,
    listing_id_filters: list[str] | None = None,
) -> dict:
    """Draft a Google Business Profile-backed location AssetSet — PREVIEW.

    Creates an ``AssetSet`` of type LOCATION_SYNC that pulls locations from a
    linked Google Business Profile and exposes them as location assets. The
    set is attached at the customer level (so all eligible campaigns get it
    by default). Optionally also creates a ``CampaignAssetSet`` link to a
    specific campaign.

    Required preflight: the Google Business Profile must already be linked
    in Google Ads → Tools → Linked accounts → Business Profile.

    business_profile_account_id: numeric Business Profile (LBC) account ID,
        e.g. "1234567890". Find via GBP admin.
    asset_set_name: optional name for the AssetSet. Defaults to
        "GBP Locations - <business_profile_account_id>".
    label_filters: optional list of GBP location labels to limit sync.
    listing_id_filters: optional list of GBP listing IDs to limit sync.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_location_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not business_profile_account_id:
        return {
            "error": (
                "business_profile_account_id is required (numeric GBP/LBC account ID). "
                "Find it in Google Business Profile admin."
            )
        }

    name = asset_set_name or f"GBP Locations - {business_profile_account_id}"
    warnings = [
        "The Google Business Profile must already be linked at Tools → Linked "
        "accounts → Business Profile in Google Ads. If it isn't, this tool "
        "will fail at apply time."
    ]

    plan = ChangePlan(
        operation="create_location_asset",
        entity_type="asset_set",
        entity_id=customer_id,
        customer_id=customer_id,
        changes={
            "scope": "campaign" if campaign_id else "customer",
            "campaign_id": campaign_id,
            "business_profile_account_id": str(business_profile_account_id),
            "asset_set_name": name,
            "label_filters": list(label_filters or []),
            "listing_id_filters": [str(x) for x in (listing_id_filters or [])],
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def _apply_create_location_asset(
    client: object, cid: str, changes: dict
) -> dict:
    """Create a LOCATION_SYNC AssetSet linked to a Google Business Profile.

    Steps:
      1. Create AssetSet (type=LOCATION_SYNC,
         location_set.business_profile_location_set.business_account_id=<id>).
      2. Create CustomerAssetSet linking the set to the customer (customer scope).
      3. Or create CampaignAssetSet linking it to one campaign (campaign scope).

    Field model: ``LOCATION_SYNC`` AssetSets carry a ``location_set`` oneof.
    For Google Business Profile, the ``business_profile_location_set`` variant
    holds the GBP/LBC account ID and optional listing/label filters.
    """
    asset_set_service = client.get_service("AssetSetService")

    # Step 1 — create AssetSet
    set_op = client.get_type("AssetSetOperation")
    asset_set = set_op.create
    asset_set.name = changes["asset_set_name"]
    asset_set.type_ = client.enums.AssetSetTypeEnum.LOCATION_SYNC
    bpls = asset_set.location_set.business_profile_location_set
    # business_account_id is exposed as STRING by proto-plus even though the
    # value is a numeric GBP/LBC account id.
    bpls.business_account_id = str(changes["business_profile_account_id"])
    for label in changes.get("label_filters") or []:
        bpls.label_filters.append(label)
    for listing_id in changes.get("listing_id_filters") or []:
        bpls.listing_id_filters.append(int(listing_id))

    set_response = asset_set_service.mutate_asset_sets(
        customer_id=cid, operations=[set_op]
    )
    asset_set_resource = set_response.results[0].resource_name

    result = {"asset_set": asset_set_resource}

    # Step 2/3 — link to customer or campaign
    scope = changes.get("scope", "customer")
    if scope == "customer":
        cas_service = client.get_service("CustomerAssetSetService")
        cas_op = client.get_type("CustomerAssetSetOperation")
        cas_op.create.asset_set = asset_set_resource
        cas_response = cas_service.mutate_customer_asset_sets(
            customer_id=cid, operations=[cas_op]
        )
        result["customer_asset_set"] = cas_response.results[0].resource_name
    elif scope == "campaign":
        if not changes.get("campaign_id"):
            raise ValueError("campaign_id required for campaign-scope location asset")
        campaign_service = client.get_service("CampaignService")
        cas_service = client.get_service("CampaignAssetSetService")
        cas_op = client.get_type("CampaignAssetSetOperation")
        cas_op.create.asset_set = asset_set_resource
        cas_op.create.campaign = campaign_service.campaign_path(
            cid, changes["campaign_id"]
        )
        cas_response = cas_service.mutate_campaign_asset_sets(
            customer_id=cid, operations=[cas_op]
        )
        result["campaign_asset_set"] = cas_response.results[0].resource_name
    else:
        raise ValueError(f"Unknown scope: {scope}")

    return result


def draft_business_name_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    business_name: str = "",
) -> dict:
    """Draft a business-name asset — returns a PREVIEW.

    Creates a TEXT asset and links it as ``BUSINESS_NAME`` at customer or
    campaign scope. Google shows the business name alongside ads (and on
    image-rich placements like the maps card / local pack) so users can
    recognize the brand at a glance.

    Scope:
        - If ``campaign_id`` is empty (default), the asset is linked at the
          customer/account level via CustomerAsset.
        - If ``campaign_id`` is provided, the asset is scoped to that
          single campaign via CampaignAsset.

    business_name: max 25 characters per Google Ads policy.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_business_name_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    text = (business_name or "").strip()
    if not text:
        return {"error": "business_name is required"}
    if len(text) > 25:
        return {
            "error": "Validation failed",
            "details": [
                f"business_name '{text}' is {len(text)} chars (max 25)"
            ],
        }

    scope = "campaign" if campaign_id else "customer"
    plan = ChangePlan(
        operation="create_business_name_asset",
        entity_type="campaign_asset" if scope == "campaign" else "customer_asset",
        entity_id=campaign_id or customer_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "business_name": text,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _apply_create_business_name_asset(
    client: object, cid: str, changes: dict
) -> dict:
    """Create a TEXT asset and link as BUSINESS_NAME at customer or campaign scope."""
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations: list = []

    # 1) Create Asset (TEXT)
    op = client.get_type("MutateOperation")
    asset = op.asset_operation.create
    asset.resource_name = asset_service.asset_path(cid, "-1")
    asset.type_ = client.enums.AssetTypeEnum.TEXT
    asset.text_asset.text = changes["business_name"]
    operations.append(op)

    # 2) Link as BUSINESS_NAME
    scope = changes.get("scope", "customer")
    link_op = client.get_type("MutateOperation")
    if scope == "campaign":
        if not changes.get("campaign_id"):
            raise ValueError("campaign_id required for campaign-scope business_name asset")
        link = link_op.campaign_asset_operation.create
        link.asset = asset_service.asset_path(cid, "-1")
        link.campaign = googleads_service.campaign_path(cid, changes["campaign_id"])
        link.field_type = client.enums.AssetFieldTypeEnum.BUSINESS_NAME
    elif scope == "customer":
        link = link_op.customer_asset_operation.create
        link.asset = asset_service.asset_path(cid, "-1")
        link.field_type = client.enums.AssetFieldTypeEnum.BUSINESS_NAME
    else:
        raise ValueError(f"Unknown scope: {scope}")
    operations.append(link_op)

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    result = {"asset": "", "link": ""}
    for resp in response.mutate_operation_responses:
        if resp.asset_result.resource_name and not result["asset"]:
            result["asset"] = resp.asset_result.resource_name
        elif scope == "campaign" and resp.campaign_asset_result.resource_name:
            result["link"] = resp.campaign_asset_result.resource_name
        elif scope == "customer" and resp.customer_asset_result.resource_name:
            result["link"] = resp.customer_asset_result.resource_name
    return result


def add_geo_exclusions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    geo_target_ids: list[str] | None = None,
) -> dict:
    """Draft negative geo CampaignCriterion records — returns a PREVIEW.

    Adds excluded locations so the campaign does not serve to users in those
    geos, even if they would otherwise match an included geo.

    geo_target_ids: list of geoTargetConstant IDs (e.g. ["1014962"] for
        Los Angeles). Look up IDs via geo_target_constant in run_gaql.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_geo_exclusions", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not campaign_id:
        return {"error": "campaign_id is required"}
    cleaned = [str(g).strip() for g in (geo_target_ids or []) if str(g).strip()]
    if not cleaned:
        return {"error": "At least one geo_target_id is required"}

    plan = ChangePlan(
        operation="add_geo_exclusions",
        entity_type="campaign_criterion",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "geo_target_ids": cleaned,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _apply_add_geo_exclusions(client: object, cid: str, changes: dict) -> dict:
    """Add negative location CampaignCriterion records to a campaign."""
    campaign_service = client.get_service("CampaignService")
    crit_service = client.get_service("CampaignCriterionService")
    operations = []
    for geo_id in changes["geo_target_ids"]:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_service.campaign_path(cid, changes["campaign_id"])
        crit.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        crit.negative = True
        operations.append(op)
    response = crit_service.mutate_campaign_criteria(
        customer_id=cid, operations=operations
    )
    return {
        "campaign_criteria": [r.resource_name for r in response.results],
    }


def add_ad_schedule(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    schedule: list[dict] | None = None,
) -> dict:
    """Draft ad schedule additions for a campaign — returns a PREVIEW.

    Creates ``CampaignCriterion`` records of type AD_SCHEDULE so the
    campaign only serves during the specified hours/days.

    schedule: list of dicts with keys:
        - day_of_week: MONDAY..SUNDAY
        - start_hour: 0..23
        - end_hour: 0..24 (must be > start)
        - start_minute / end_minute: 0, 15, 30, or 45 (default 0)

    Note: ad-schedule hours follow the account's configured time zone.
    Adding a schedule is additive — it does NOT replace existing schedule
    criteria. Pause/remove existing schedule entries first if you want a
    clean slate.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_ad_schedule", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not campaign_id:
        return {"error": "campaign_id is required"}
    validated, errors = _validate_ad_schedule(schedule or [])
    if errors:
        return {"error": "Validation failed", "details": errors}
    if not validated:
        return {"error": "At least one schedule entry is required"}

    plan = ChangePlan(
        operation="add_ad_schedule",
        entity_type="campaign_criterion",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "schedule": validated,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _apply_add_ad_schedule(client: object, cid: str, changes: dict) -> dict:
    """Add AdScheduleInfo CampaignCriterion records to a campaign."""
    campaign_service = client.get_service("CampaignService")
    crit_service = client.get_service("CampaignCriterionService")
    operations = []
    for entry in changes["schedule"]:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_service.campaign_path(
            cid, changes["campaign_id"]
        )
        _populate_ad_schedule_info(client, crit.ad_schedule, entry)
        operations.append(op)
    response = crit_service.mutate_campaign_criteria(
        customer_id=cid, operations=operations
    )
    return {
        "campaign_criteria": [r.resource_name for r in response.results],
    }


_AD_SCHEDULE_DAY_ENUM = {
    "MONDAY": "MONDAY",
    "TUESDAY": "TUESDAY",
    "WEDNESDAY": "WEDNESDAY",
    "THURSDAY": "THURSDAY",
    "FRIDAY": "FRIDAY",
    "SATURDAY": "SATURDAY",
    "SUNDAY": "SUNDAY",
}
_MINUTE_TO_ENUM = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}


def _populate_ad_schedule_info(client: object, info: object, entry: dict) -> None:
    """Set fields on an AdScheduleInfo proto from a validated entry."""
    info.day_of_week = getattr(
        client.enums.DayOfWeekEnum, _AD_SCHEDULE_DAY_ENUM[entry["day_of_week"]]
    )
    info.start_hour = int(entry["start_hour"])
    info.end_hour = int(entry["end_hour"])
    info.start_minute = getattr(
        client.enums.MinuteOfHourEnum, _MINUTE_TO_ENUM[int(entry["start_minute"])]
    )
    info.end_minute = getattr(
        client.enums.MinuteOfHourEnum, _MINUTE_TO_ENUM[int(entry["end_minute"])]
    )


def _apply_create_call_asset(client: object, cid: str, changes: dict) -> dict:
    """Create a CallAsset at customer or campaign scope."""
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []

    op = client.get_type("MutateOperation")
    asset = op.asset_operation.create
    asset.resource_name = asset_service.asset_path(cid, "-1")
    asset.call_asset.country_code = changes["country_code"]
    asset.call_asset.phone_number = changes["phone_number"]
    if changes.get("call_conversion_action_id"):
        ca_service = client.get_service("ConversionActionService")
        asset.call_asset.call_conversion_action = ca_service.conversion_action_path(
            cid, str(changes["call_conversion_action_id"])
        )
        asset.call_asset.call_conversion_reporting_state = (
            client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
    for entry in changes.get("ad_schedule") or []:
        info = client.get_type("AdScheduleInfo")
        _populate_ad_schedule_info(client, info, entry)
        asset.call_asset.ad_schedule_targets.append(info)
    operations.append(op)

    scope = changes.get("scope", "campaign")
    if scope == "campaign":
        if not changes.get("campaign_id"):
            raise ValueError("campaign_id required for campaign-scope call asset")
        link_op = client.get_type("MutateOperation")
        ca = link_op.campaign_asset_operation.create
        ca.asset = asset_service.asset_path(cid, "-1")
        ca.campaign = googleads_service.campaign_path(cid, changes["campaign_id"])
        ca.field_type = client.enums.AssetFieldTypeEnum.CALL
        operations.append(link_op)
    elif scope == "customer":
        link_op = client.get_type("MutateOperation")
        cust_asset = link_op.customer_asset_operation.create
        cust_asset.asset = asset_service.asset_path(cid, "-1")
        cust_asset.field_type = client.enums.AssetFieldTypeEnum.CALL
        operations.append(link_op)
    else:
        raise ValueError(f"Unknown scope: {scope}")

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    result = {"asset": "", "link": ""}
    for resp in response.mutate_operation_responses:
        if resp.asset_result.resource_name and not result["asset"]:
            result["asset"] = resp.asset_result.resource_name
        elif scope == "campaign" and resp.campaign_asset_result.resource_name:
            result["link"] = resp.campaign_asset_result.resource_name
        elif scope == "customer" and resp.customer_asset_result.resource_name:
            result["link"] = resp.customer_asset_result.resource_name
    return result


def _apply_create_conversion_action_route(client, cid, changes):
    from adloop.ads.conversion_actions import _apply_create_conversion_action
    return _apply_create_conversion_action(client, cid, changes)


def _apply_update_conversion_action_route(client, cid, changes):
    from adloop.ads.conversion_actions import _apply_update_conversion_action
    return _apply_update_conversion_action(client, cid, changes)


def _apply_remove_conversion_action_route(client, cid, changes):
    from adloop.ads.conversion_actions import _apply_remove_conversion_action
    return _apply_remove_conversion_action(client, cid, changes)


def _apply_update_call_asset(client: object, cid: str, changes: dict) -> dict:
    """In-place update of an existing CallAsset."""
    from google.protobuf import field_mask_pb2

    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.update
    asset.resource_name = asset_service.asset_path(cid, changes["asset_id"])

    paths: list[str] = []
    if "phone_number" in changes:
        asset.call_asset.phone_number = changes["phone_number"]
        paths.append("call_asset.phone_number")
    if "country_code" in changes:
        asset.call_asset.country_code = changes["country_code"]
        paths.append("call_asset.country_code")
    if "call_conversion_action_id" in changes:
        ca_service = client.get_service("ConversionActionService")
        asset.call_asset.call_conversion_action = ca_service.conversion_action_path(
            cid, changes["call_conversion_action_id"]
        )
        paths.append("call_asset.call_conversion_action")
    if "call_conversion_reporting_state" in changes:
        asset.call_asset.call_conversion_reporting_state = getattr(
            client.enums.CallConversionReportingStateEnum,
            changes["call_conversion_reporting_state"],
        )
        paths.append("call_asset.call_conversion_reporting_state")
    if "ad_schedule" in changes:
        # Replace the schedule list entirely
        for entry in changes["ad_schedule"]:
            info = client.get_type("AdScheduleInfo")
            _populate_ad_schedule_info(client, info, entry)
            asset.call_asset.ad_schedule_targets.append(info)
        paths.append("call_asset.ad_schedule_targets")

    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
    response = asset_service.mutate_assets(customer_id=cid, operations=[op])
    return {"resource_name": response.results[0].resource_name}


def _apply_update_sitelink(client: object, cid: str, changes: dict) -> dict:
    """In-place update of an existing SitelinkAsset."""
    from google.protobuf import field_mask_pb2

    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.update
    asset.resource_name = asset_service.asset_path(cid, changes["asset_id"])

    paths: list[str] = []
    if "link_text" in changes:
        asset.sitelink_asset.link_text = changes["link_text"]
        paths.append("sitelink_asset.link_text")
    if "description1" in changes:
        asset.sitelink_asset.description1 = changes["description1"]
        paths.append("sitelink_asset.description1")
    if "description2" in changes:
        asset.sitelink_asset.description2 = changes["description2"]
        paths.append("sitelink_asset.description2")
    if "final_url" in changes:
        asset.final_urls.append(changes["final_url"])
        paths.append("final_urls")

    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
    response = asset_service.mutate_assets(customer_id=cid, operations=[op])
    return {"resource_name": response.results[0].resource_name}


def _apply_update_callout(client: object, cid: str, changes: dict) -> dict:
    """In-place update of an existing CalloutAsset's text."""
    from google.protobuf import field_mask_pb2

    asset_service = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.update
    asset.resource_name = asset_service.asset_path(cid, changes["asset_id"])
    asset.callout_asset.callout_text = changes["callout_text"]
    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(
        paths=["callout_asset.callout_text"]
    ))
    response = asset_service.mutate_assets(customer_id=cid, operations=[op])
    return {"resource_name": response.results[0].resource_name}


def _populate_promotion_asset(client: object, asset: object, promo: dict) -> None:
    """Fill an Asset proto with PromotionAsset fields from a normalized dict."""
    p = asset.promotion_asset
    p.promotion_target = promo["promotion_target"]
    if promo.get("money_off"):
        p.money_amount_off.amount_micros = int(
            float(promo["money_off"]) * 1_000_000
        )
        p.money_amount_off.currency_code = promo["currency_code"]
    elif promo.get("percent_off"):
        p.percent_off = int(float(promo["percent_off"]) * 1_000_000)

    if promo.get("promotion_code"):
        p.promotion_code = promo["promotion_code"]

    if promo.get("orders_over_amount"):
        p.orders_over_amount.amount_micros = int(
            float(promo["orders_over_amount"]) * 1_000_000
        )
        p.orders_over_amount.currency_code = promo["currency_code"]

    if promo.get("occasion"):
        p.occasion = getattr(
            client.enums.PromotionExtensionOccasionEnum, promo["occasion"]
        )

    if promo.get("discount_modifier"):
        p.discount_modifier = getattr(
            client.enums.PromotionExtensionDiscountModifierEnum,
            promo["discount_modifier"],
        )

    p.language_code = promo.get("language_code") or "en"
    if promo.get("start_date"):
        p.start_date = promo["start_date"]
    if promo.get("end_date"):
        p.end_date = promo["end_date"]
    if promo.get("redemption_start_date"):
        p.redemption_start_date = promo["redemption_start_date"]
    if promo.get("redemption_end_date"):
        p.redemption_end_date = promo["redemption_end_date"]

    for entry in promo.get("ad_schedule") or []:
        info = client.get_type("AdScheduleInfo")
        _populate_ad_schedule_info(client, info, entry)
        p.ad_schedule_targets.append(info)

    asset.final_urls.append(promo["final_url"])


def _apply_create_promotion(client: object, cid: str, changes: dict) -> dict:
    """Create a PromotionAsset and link it at customer or campaign scope."""

    def populate(asset: object, payload: dict) -> None:
        _populate_promotion_asset(client, asset, payload)

    return _apply_assets(
        client,
        cid,
        [changes["promotion"]],
        client.enums.AssetFieldTypeEnum.PROMOTION,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
    )


def _apply_update_promotion(client: object, cid: str, changes: dict) -> dict:
    """Swap a PromotionAsset: create new + link, then unlink old.

    Steps (each is its own MutateOperation, batched into one mutate call):
      1. Create a new Asset with the new promotion fields.
      2. Link the new Asset (CampaignAsset or CustomerAsset).
      3. Remove the old link (CampaignAsset/CustomerAsset matching old asset_id).
      4. Optionally remove the old Asset row.
    """
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")

    scope = changes.get("scope", "campaign")
    campaign_id = changes.get("campaign_id", "")
    old_asset_id = str(changes["old_asset_id"])
    promo = changes["promotion"]

    operations = []

    # 1. Create new Asset
    create_op = client.get_type("MutateOperation")
    new_asset = create_op.asset_operation.create
    new_asset.resource_name = asset_service.asset_path(cid, "-1")
    _populate_promotion_asset(client, new_asset, promo)
    operations.append(create_op)

    # 2. Link the new asset
    if scope == "campaign":
        if not campaign_id:
            raise ValueError("campaign_id required for campaign-scope update")
        link_op = client.get_type("MutateOperation")
        ca = link_op.campaign_asset_operation.create
        ca.asset = asset_service.asset_path(cid, "-1")
        ca.campaign = campaign_service.campaign_path(cid, campaign_id)
        ca.field_type = client.enums.AssetFieldTypeEnum.PROMOTION
        operations.append(link_op)
    elif scope == "customer":
        link_op = client.get_type("MutateOperation")
        cust = link_op.customer_asset_operation.create
        cust.asset = asset_service.asset_path(cid, "-1")
        cust.field_type = client.enums.AssetFieldTypeEnum.PROMOTION
        operations.append(link_op)
    else:
        raise ValueError(f"Unknown scope: {scope}")

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    new_asset_resource = ""
    new_link_resource = ""
    for resp in response.mutate_operation_responses:
        if resp.asset_result.resource_name and not new_asset_resource:
            new_asset_resource = resp.asset_result.resource_name
        elif scope == "campaign" and resp.campaign_asset_result.resource_name:
            new_link_resource = resp.campaign_asset_result.resource_name
        elif scope == "customer" and resp.customer_asset_result.resource_name:
            new_link_resource = resp.customer_asset_result.resource_name

    # 3. Find the old link and remove it
    old_link_resource = _find_promotion_link(
        client, cid, old_asset_id, scope, campaign_id
    )
    old_link_removed = ""
    if old_link_resource:
        if scope == "campaign":
            ca_service = client.get_service("CampaignAssetService")
            rm_op = client.get_type("CampaignAssetOperation")
            rm_op.remove = old_link_resource
            ca_service.mutate_campaign_assets(customer_id=cid, operations=[rm_op])
        else:
            cust_service = client.get_service("CustomerAssetService")
            rm_op = client.get_type("CustomerAssetOperation")
            rm_op.remove = old_link_resource
            cust_service.mutate_customer_assets(customer_id=cid, operations=[rm_op])
        old_link_removed = old_link_resource

    return {
        "new_asset": new_asset_resource,
        "new_link": new_link_resource,
        "old_link_removed": old_link_removed,
    }


def _find_promotion_link(
    client: object,
    cid: str,
    asset_id: str,
    scope: str,
    campaign_id: str,
) -> str:
    """Look up the CampaignAsset or CustomerAsset link for a given asset_id."""
    googleads_service = client.get_service("GoogleAdsService")
    asset_service = client.get_service("AssetService")
    asset_resource = asset_service.asset_path(cid, asset_id)

    if scope == "campaign":
        if not campaign_id:
            return ""
        query = (
            "SELECT campaign_asset.resource_name "
            "FROM campaign_asset "
            f"WHERE campaign_asset.asset = '{asset_resource}' "
            f"  AND campaign_asset.campaign = "
            f"      '{client.get_service('CampaignService').campaign_path(cid, campaign_id)}' "
            "  AND campaign_asset.field_type = 'PROMOTION'"
        )
    else:
        query = (
            "SELECT customer_asset.resource_name "
            "FROM customer_asset "
            f"WHERE customer_asset.asset = '{asset_resource}' "
            "  AND customer_asset.field_type = 'PROMOTION'"
        )

    response = googleads_service.search(customer_id=cid, query=query)
    for row in response:
        if scope == "campaign":
            return row.campaign_asset.resource_name
        return row.customer_asset.resource_name
    return ""


def _apply_link_asset_to_customer(
    client: object, cid: str, changes: dict
) -> dict:
    """Create CustomerAsset link rows pointing to existing Asset rows.

    Does NOT create new Asset rows — only the link. Use this to promote
    existing assets (e.g. images, logos that were uploaded to a legacy
    campaign) so they apply at the account level.
    """
    asset_service = client.get_service("AssetService")
    cust_service = client.get_service("CustomerAssetService")

    operations = []
    for link in changes["links"]:
        op = client.get_type("CustomerAssetOperation")
        ca = op.create
        ca.asset = asset_service.asset_path(cid, link["asset_id"])
        ca.field_type = getattr(
            client.enums.AssetFieldTypeEnum, link["field_type"]
        )
        operations.append(op)

    response = cust_service.mutate_customer_assets(
        customer_id=cid, operations=operations
    )
    return {
        "customer_assets": [r.resource_name for r in response.results],
        "linked_count": len(response.results),
    }


def _apply_create_sitelinks(client: object, cid: str, changes: dict) -> dict:
    """Create sitelink assets at customer or campaign scope."""

    def populate(asset: object, payload: dict) -> None:
        asset.sitelink_asset.link_text = payload["link_text"]
        asset.final_urls.append(payload["final_url"])
        if payload.get("description1"):
            asset.sitelink_asset.description1 = payload["description1"]
        if payload.get("description2"):
            asset.sitelink_asset.description2 = payload["description2"]

    return _apply_assets(
        client,
        cid,
        changes["sitelinks"],
        client.enums.AssetFieldTypeEnum.SITELINK,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
    )


def _apply_create_negative_keyword_list(
    client: object, cid: str, changes: dict
) -> dict:
    """Create a shared negative keyword list and attach it to a campaign.

    Executes three sequential API calls. If any step fails, the result
    includes partial_failure info with the SharedSet resource name (if
    created) so the caller can clean up or retry the remaining steps.
    """
    # 1. Create the SharedSet
    try:
        shared_set_service = client.get_service("SharedSetService")
        ss_op = client.get_type("SharedSetOperation")
        shared_set = ss_op.create
        shared_set.name = changes["list_name"]
        shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
        ss_response = shared_set_service.mutate_shared_sets(
            customer_id=cid, operations=[ss_op]
        )
        shared_set_resource = ss_response.results[0].resource_name
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": None,
            "completed_steps": [],
            "failed_step": "create_shared_set",
            "error": _extract_error_message(exc),
        }

    # 2. Add keywords to the list
    try:
        sc_service = client.get_service("SharedCriterionService")
        sc_ops = []
        for kw_text in changes["keywords"]:
            sc_op = client.get_type("SharedCriterionOperation")
            criterion = sc_op.create
            criterion.shared_set = shared_set_resource
            criterion.keyword.text = kw_text
            criterion.keyword.match_type = getattr(
                client.enums.KeywordMatchTypeEnum, changes["match_type"]
            )
            sc_ops.append(sc_op)
        sc_service.mutate_shared_criteria(customer_id=cid, operations=sc_ops)
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": shared_set_resource,
            "completed_steps": ["create_shared_set"],
            "failed_step": "add_keywords",
            "error": _extract_error_message(exc),
        }

    # 3. Attach the list to the campaign
    try:
        css_service = client.get_service("CampaignSharedSetService")
        css_op = client.get_type("CampaignSharedSetOperation")
        campaign_shared_set = css_op.create
        campaign_shared_set.campaign = client.get_service(
            "CampaignService"
        ).campaign_path(cid, changes["campaign_id"])
        campaign_shared_set.shared_set = shared_set_resource
        css_response = css_service.mutate_campaign_shared_sets(
            customer_id=cid, operations=[css_op]
        )
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": shared_set_resource,
            "keyword_count": len(changes["keywords"]),
            "completed_steps": ["create_shared_set", "add_keywords"],
            "failed_step": "attach_to_campaign",
            "error": _extract_error_message(exc),
        }

    return {
        "shared_set_resource": shared_set_resource,
        "campaign_shared_set_resource": css_response.results[0].resource_name,
        "keyword_count": len(changes["keywords"]),
    }


def _apply_add_to_negative_keyword_list(
    client: object, cid: str, changes: dict
) -> dict:
    """Append keywords to an existing shared negative keyword list."""
    shared_set_service = client.get_service("SharedSetService")
    shared_set_resource = shared_set_service.shared_set_path(
        cid, changes["shared_set_id"]
    )

    sc_service = client.get_service("SharedCriterionService")
    operations = []
    for kw_text in changes["keywords"]:
        op = client.get_type("SharedCriterionOperation")
        criterion = op.create
        criterion.shared_set = shared_set_resource
        criterion.keyword.text = kw_text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, changes["match_type"]
        )
        operations.append(op)

    response = sc_service.mutate_shared_criteria(
        customer_id=cid, operations=operations
    )
    return {
        "shared_set_resource": shared_set_resource,
        "resource_names": [r.resource_name for r in response.results],
        "keyword_count": len(response.results),
    }
