"""Performance Max asset groups: add and remove assets — draft → preview → apply.

``draft_asset_group_assets`` changes the assets of one existing asset group
in one atomic GoogleAdsService.Mutate: new assets are created with temp ids
and linked in the same request, removals unlink (the asset itself stays in
the account library). Adding and removing in one draft is how an asset is
replaced without the group dipping below Google's minimums in between.

Images come from public URLs, uploads (``up_...``) or local files, are
pinned by sha256 like campaign image assets, and are sorted into landscape
(1.91:1), square (1:1) or portrait (4:5) marketing images by their aspect
ratio. Logos are square, landscape logos 4:1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# Field type → (minimum, maximum) per asset group, and the text length cap.
# Minimums are what an asset group needs to stay valid; Google rejects a
# change that goes below them, the draft refuses it first.
_LIMITS: dict[str, tuple[int, int]] = {
    "HEADLINE": (3, 15),
    "LONG_HEADLINE": (1, 5),
    "DESCRIPTION": (2, 5),
    "BUSINESS_NAME": (0, 1),
    "MARKETING_IMAGE": (0, 20),
    "SQUARE_MARKETING_IMAGE": (0, 20),
    "PORTRAIT_MARKETING_IMAGE": (0, 20),
    "LOGO": (0, 5),
    "LANDSCAPE_LOGO": (0, 5),
    "YOUTUBE_VIDEO": (0, 5),
}
_TEXT_CAPS = {"HEADLINE": 30, "LONG_HEADLINE": 90, "DESCRIPTION": 90, "BUSINESS_NAME": 25}
_IMAGE_TYPES = {"MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "PORTRAIT_MARKETING_IMAGE"}
_MAX_IMAGES = 20
# (field type, ratio width/height, minimum width, minimum height)
_RATIOS = [
    ("MARKETING_IMAGE", 1.91, 600, 314),
    ("SQUARE_MARKETING_IMAGE", 1.0, 300, 300),
    ("PORTRAIT_MARKETING_IMAGE", 0.8, 480, 600),
]
_LOGO_RULES = {"logo": ("LOGO", 1.0, 128, 128), "landscape_logo": ("LANDSCAPE_LOGO", 4.0, 512, 128)}
_RATIO_TOLERANCE = 0.02


def asset_group_state(config: AdLoopConfig, customer_id: str, asset_group_id: str) -> dict:
    """The asset group, its campaign, and its current asset links by field type."""
    from adloop.ads.gaql import execute_query

    groups = execute_query(
        config,
        customer_id,
        "SELECT asset_group.id, asset_group.name, asset_group.status, campaign.id, campaign.name, "
        "campaign.advertising_channel_type FROM asset_group "
        f"WHERE asset_group.id = {int(asset_group_id)}",
    )
    if not groups:
        raise ValueError(f"asset group {asset_group_id} was not found")
    links = execute_query(
        config,
        customer_id,
        "SELECT asset_group_asset.resource_name, asset_group_asset.field_type, asset_group_asset.status, "
        "asset.id, asset.type, asset.text_asset.text, asset.youtube_video_asset.youtube_video_id "
        "FROM asset_group_asset "
        f"WHERE asset_group.id = {int(asset_group_id)} AND asset_group_asset.status != 'REMOVED'",
    )
    group = groups[0]
    return {
        "asset_group_name": group.get("asset_group.name"),
        "asset_group_status": group.get("asset_group.status"),
        "campaign_id": str(group.get("campaign.id") or ""),
        "campaign_name": group.get("campaign.name"),
        "channel": group.get("campaign.advertising_channel_type"),
        "links": [
            {
                "resource_name": row.get("asset_group_asset.resource_name"),
                "field_type": str(row.get("asset_group_asset.field_type") or ""),
                "asset_id": str(row.get("asset.id") or ""),
                "text": row.get("asset.text_asset.text") or row.get("asset.youtube_video_asset.youtube_video_id"),
            }
            for row in links
        ],
    }


def _classify_image(payload: dict, role: str) -> tuple[str | None, str | None]:
    """(field type, error) for an image by its role and aspect ratio."""
    width, height = int(payload["width"]), int(payload["height"])
    ratio = width / height if height else 0
    candidates = [_LOGO_RULES[role]] if role in _LOGO_RULES else _RATIOS
    for field_type, target, min_w, min_h in candidates:
        if abs(ratio - target) / target <= _RATIO_TOLERANCE:
            if width < min_w or height < min_h:
                return None, f"{width}x{height} is too small for {field_type} (at least {min_w}x{min_h})"
            return field_type, None
    wanted = "1:1" if role == "logo" else "4:1" if role == "landscape_logo" else "1.91:1, 1:1 or 4:5"
    return None, f"{width}x{height} is not {wanted}; crop it to that ratio first"


def draft_asset_group_assets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_group_id: str = "",
    headlines: list[str] | None = None,
    long_headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    business_name: str = "",
    images: list[dict] | None = None,
    youtube_video_ids: list[str] | None = None,
    remove_asset_ids: list[str] | None = None,
) -> dict:
    """Draft adding and/or removing assets of one PMax asset group — returns a preview."""
    from adloop.ads.write import image_payloads
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_asset_group_assets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    warnings: list[str] = []
    asset_group_id = str(asset_group_id or "").strip()
    if not asset_group_id.isdigit():
        return {"error": "Validation failed", "details": ["asset_group_id is required (see get_pmax_performance)"]}

    def clean(items: list | None) -> list[str]:
        return [str(x).strip() for x in (items or []) if str(x).strip()]

    texts: dict[str, list[str]] = {
        "HEADLINE": clean(headlines),
        "LONG_HEADLINE": clean(long_headlines),
        "DESCRIPTION": clean(descriptions),
        "BUSINESS_NAME": clean([business_name]),
    }
    for field_type, values in texts.items():
        cap = _TEXT_CAPS[field_type]
        for value in values:
            if len(value) > cap:
                errors.append(f"{field_type.lower()} '{value}' is {len(value)} characters; the limit is {cap}")
    videos = clean(youtube_video_ids)
    for video in videos:
        if not (len(video) == 11 and all(c.isalnum() or c in "-_" for c in video)):
            errors.append(f"'{video}' is not a YouTube video id (11 characters, from the video URL)")
    removals = clean(remove_asset_ids)

    image_adds: list[dict] = []
    for index, image in enumerate(images or []):
        if not isinstance(image, dict):
            errors.append(f'images[{index}] must be an object like {{"url": "https://...", "role": "marketing"}}')
            continue
        role = str(image.get("role") or "marketing").strip().lower()
        if role not in ("marketing", "logo", "landscape_logo"):
            errors.append(f"images[{index}].role must be marketing, logo or landscape_logo")
            continue
        url, upload_id = str(image.get("url") or "").strip(), str(image.get("upload_id") or "").strip()
        if bool(url) == bool(upload_id):
            errors.append(f"images[{index}] needs exactly one of url or upload_id")
            continue
        payloads, problems = image_payloads([], [url] if url else [], [upload_id] if upload_id else [])
        errors.extend(f"images[{index}]: {p}" for p in problems)
        for payload in payloads:
            field_type, problem = _classify_image(payload, role)
            if problem:
                errors.append(f"images[{index}]: {problem}")
            else:
                image_adds.append({**payload, "field_type": field_type})

    if not any(texts.values()) and not image_adds and not videos and not removals and not errors:
        errors.append("Nothing to change: pass assets to add and/or remove_asset_ids")
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        state = asset_group_state(config, customer_id, asset_group_id)
    except ValueError as e:
        return {"error": str(e)}
    if state["channel"] not in (None, "PERFORMANCE_MAX"):
        return {"error": f"asset group {asset_group_id} belongs to a {state['channel']} campaign, not Performance Max"}

    links = state["links"]
    by_asset = {link["asset_id"]: link for link in links}
    unknown = [a for a in removals if a not in by_asset]
    if unknown:
        return {
            "error": "Validation failed",
            "details": [f"assets not linked to asset group {asset_group_id}: {', '.join(unknown)} (see get_pmax_assets)"],
        }
    existing_texts = {(link["field_type"], (link["text"] or "").strip().lower()) for link in links}
    for field_type in list(texts):
        kept = []
        for value in texts[field_type]:
            if (field_type, value.lower()) in existing_texts:
                warnings.append(f"{field_type.lower()} '{value}' is already in the asset group; skipped")
            else:
                kept.append(value)
        texts[field_type] = kept
    existing_videos = {link["text"] for link in links if link["field_type"] == "YOUTUBE_VIDEO"}
    for video in [v for v in videos if v in existing_videos]:
        warnings.append(f"YouTube video {video} is already in the asset group; skipped")
    videos = [v for v in videos if v not in existing_videos]

    counts: dict[str, int] = {}
    for link in links:
        if link["asset_id"] not in removals:
            counts[link["field_type"]] = counts.get(link["field_type"], 0) + 1
    for field_type, values in texts.items():
        counts[field_type] = counts.get(field_type, 0) + len(values)
    for image in image_adds:
        counts[image["field_type"]] = counts.get(image["field_type"], 0) + 1
    counts["YOUTUBE_VIDEO"] = counts.get("YOUTUBE_VIDEO", 0) + len(videos)
    for field_type, (minimum, maximum) in _LIMITS.items():
        count = counts.get(field_type, 0)
        if count > maximum:
            errors.append(f"{field_type} would have {count} assets; an asset group takes at most {maximum}")
        touched = field_type in {by_asset[a]["field_type"] for a in removals}
        if touched and count < minimum:
            errors.append(
                f"{field_type} would drop to {count}; an asset group needs at least {minimum}. "
                "Add a replacement in the same draft."
            )
    if sum(counts.get(t, 0) for t in _IMAGE_TYPES) > _MAX_IMAGES:
        errors.append(f"an asset group takes at most {_MAX_IMAGES} marketing images in total")
    if texts["DESCRIPTION"] or any(by_asset[a]["field_type"] == "DESCRIPTION" for a in removals):
        remaining = [link["text"] or "" for link in links
                     if link["field_type"] == "DESCRIPTION" and link["asset_id"] not in removals]
        if not any(len(d) <= 60 for d in remaining + texts["DESCRIPTION"]):
            errors.append("Google needs at least one description of 60 characters or fewer")
    if errors:
        return {"error": "Validation failed", "details": errors}

    if any(i["field_type"] in ("LOGO", "LANDSCAPE_LOGO") for i in image_adds) or texts["BUSINESS_NAME"]:
        warnings.append(
            "Campaigns that use brand guidelines keep the business name and logos at campaign level; "
            "the dry run tells if Google refuses them on the asset group."
        )
    warnings.append("New assets go through Google's policy review before they serve.")

    plan = ChangePlan(
        operation="update_asset_group_assets",
        entity_type="asset_group",
        entity_id=asset_group_id,
        customer_id=customer_id,
        changes={
            "asset_group_id": asset_group_id,
            "asset_group_name": state["asset_group_name"],
            "campaign_name": state["campaign_name"],
            "add_texts": {k.lower(): v for k, v in texts.items() if v},
            "add_images": image_adds,
            "add_youtube_video_ids": videos,
            "remove": [
                {"asset_id": a, "field_type": by_asset[a]["field_type"], "text": by_asset[a]["text"],
                 "resource_name": by_asset[a]["resource_name"]}
                for a in removals
            ],
            "counts_after": {k: v for k, v in sorted(counts.items()) if v},
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def build_asset_group_operations(client: object, cid: str, changes: dict) -> list:
    from adloop.ads.write import populate_image_asset

    service = client.get_service("GoogleAdsService")
    group_path = service.asset_group_path(cid, changes["asset_group_id"])
    operations: list[Any] = []
    temp = 0

    def link(asset_path: str, field_type: str) -> None:
        op = client.get_type("MutateOperation")
        aga = op.asset_group_asset_operation.create
        aga.asset_group = group_path
        aga.asset = asset_path
        aga.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)
        operations.append(op)

    def new_asset() -> tuple[Any, str]:
        nonlocal temp
        temp -= 1
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = service.asset_path(cid, str(temp))
        operations.append(op)
        return asset, asset.resource_name

    for field_type, values in (changes.get("add_texts") or {}).items():
        for value in values:
            asset, path = new_asset()
            asset.text_asset.text = value
            link(path, field_type.upper())
    for image in changes.get("add_images") or []:
        asset, path = new_asset()
        populate_image_asset(client, asset, image)
        link(path, image["field_type"])
    for video in changes.get("add_youtube_video_ids") or []:
        asset, path = new_asset()
        asset.youtube_video_asset.youtube_video_id = video
        link(path, "YOUTUBE_VIDEO")
    for removal in changes.get("remove") or []:
        op = client.get_type("MutateOperation")
        op.asset_group_asset_operation.remove = removal["resource_name"]
        operations.append(op)
    return operations


def apply_asset_group_assets(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("GoogleAdsService")
    operations = build_asset_group_operations(client, cid, changes)
    service.mutate(customer_id=cid, mutate_operations=operations)
    added = sum(len(v) for v in (changes.get("add_texts") or {}).values())
    added += len(changes.get("add_images") or []) + len(changes.get("add_youtube_video_ids") or [])
    return {
        "asset_group_id": changes["asset_group_id"],
        "assets_added": added,
        "assets_removed": len(changes.get("remove") or []),
        "note": "New assets are reviewed by Google before they serve; get_pmax_assets shows their status.",
    }


# ---------------------------------------------------------------------------
# Signals: search themes and the audience signal of an asset group
# ---------------------------------------------------------------------------

_MAX_SEARCH_THEMES = 50
_MAX_THEME_CHARS = 80


def get_asset_group_signals(config: AdLoopConfig, *, customer_id: str = "", asset_group_id: str = "") -> dict:
    """An asset group's search themes and audience signal, plus the account's audiences."""
    from adloop.ads.gaql import execute_query

    asset_group_id = str(asset_group_id or "").strip()
    if not asset_group_id.isdigit():
        return {"error": "asset_group_id is required (see get_pmax_performance)"}
    rows = execute_query(
        config,
        customer_id,
        "SELECT asset_group_signal.resource_name, asset_group_signal.search_theme.text, "
        "asset_group_signal.audience.audience, asset_group_signal.approval_status, "
        "asset_group_signal.disapproval_reasons FROM asset_group_signal "
        f"WHERE asset_group.id = {int(asset_group_id)}",
    )
    audiences = execute_query(
        config,
        customer_id,
        "SELECT audience.id, audience.name, audience.description, audience.status FROM audience "
        "WHERE audience.status = 'ENABLED'",
    )
    from adloop.ads.client import normalize_customer_id

    cid = normalize_customer_id(customer_id)
    names = {f"customers/{cid}/audiences/{a.get('audience.id')}": a.get("audience.name") for a in audiences}
    themes, audience_signals = [], []
    for row in rows:
        signal_id = str(row.get("asset_group_signal.resource_name") or "").rsplit("/", 1)[-1]
        entry = {
            "signal_id": signal_id,
            "approval_status": row.get("asset_group_signal.approval_status"),
            "disapproval_reasons": row.get("asset_group_signal.disapproval_reasons") or [],
        }
        if row.get("asset_group_signal.search_theme.text"):
            themes.append({**entry, "search_theme": row["asset_group_signal.search_theme.text"]})
        elif row.get("asset_group_signal.audience.audience"):
            resource = row["asset_group_signal.audience.audience"]
            audience_signals.append({
                **entry,
                "audience_id": resource.rsplit("/", 1)[-1],
                "audience_name": names.get(resource),
            })
    insights = []
    if not themes:
        insights.append("No search themes: adding a few tells PMax which searches the products fit.")
    if not audience_signals:
        insights.append("No audience signal: an audience (customer list, site visitors) speeds up PMax learning.")
    disapproved = [t["search_theme"] for t in themes if t["approval_status"] == "DISAPPROVED"]
    if disapproved:
        insights.append(f"Disapproved search themes: {', '.join(disapproved)}")
    return {
        "asset_group_id": asset_group_id,
        "search_themes": themes,
        "audience_signals": audience_signals,
        "available_audiences": [
            {"audience_id": str(a.get("audience.id")), "name": a.get("audience.name"), "description": a.get("audience.description")}
            for a in audiences
        ],
        "insights": insights,
    }


def draft_asset_group_signals(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_group_id: str = "",
    add_search_themes: list[str] | None = None,
    audience_id: str = "",
    remove_signal_ids: list[str] | None = None,
) -> dict:
    """Draft adding search themes or an audience signal, and removing signals — returns a preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_asset_group_signals", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    errors: list[str] = []
    themes = list(dict.fromkeys(str(t).strip() for t in (add_search_themes or []) if str(t).strip()))
    audience_id = str(audience_id or "").strip()
    removals = [str(r).strip() for r in (remove_signal_ids or []) if str(r).strip()]
    for theme in themes:
        if len(theme) > _MAX_THEME_CHARS:
            errors.append(f"search theme '{theme[:40]}...' is {len(theme)} characters; keep it under {_MAX_THEME_CHARS}")
    if audience_id and not audience_id.isdigit():
        errors.append("audience_id must be numeric (see get_asset_group_signals available_audiences)")
    if not themes and not audience_id and not removals:
        errors.append("Nothing to change: pass add_search_themes, audience_id and/or remove_signal_ids")
    if errors:
        return {"error": "Validation failed", "details": errors}

    current = get_asset_group_signals(config, customer_id=customer_id, asset_group_id=asset_group_id)
    if "error" in current:
        return current
    existing = {t["signal_id"]: t for t in current["search_themes"]} | {a["signal_id"]: a for a in current["audience_signals"]}
    unknown = [r for r in removals if r not in existing]
    if unknown:
        return {"error": "Validation failed", "details": [f"signals not on this asset group: {', '.join(unknown)}"]}
    known_themes = {t["search_theme"].lower() for t in current["search_themes"] if t["signal_id"] not in removals}
    warnings = [f"search theme '{t}' is already on the asset group; skipped" for t in themes if t.lower() in known_themes]
    themes = [t for t in themes if t.lower() not in known_themes]
    remaining_themes = len(known_themes) + len(themes)
    if remaining_themes > _MAX_SEARCH_THEMES:
        errors.append(f"an asset group takes at most {_MAX_SEARCH_THEMES} search themes ({remaining_themes} after this change)")
    kept_audiences = [a for a in current["audience_signals"] if a["signal_id"] not in removals]
    if audience_id:
        if audience_id not in {a["audience_id"] for a in current["available_audiences"]}:
            errors.append(f"audience {audience_id} is not an enabled audience of this account")
        if kept_audiences:
            errors.append(
                f"the asset group already has an audience signal ({kept_audiences[0]['audience_name'] or kept_audiences[0]['audience_id']}); "
                f"remove it in the same draft (remove_signal_ids=['{kept_audiences[0]['signal_id']}'])"
            )
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings.append("Signals are hints, not targeting: PMax still serves beyond them.")
    plan = ChangePlan(
        operation="update_asset_group_signals",
        entity_type="asset_group",
        entity_id=str(asset_group_id),
        customer_id=customer_id,
        changes={
            "asset_group_id": str(asset_group_id),
            "add_search_themes": themes,
            "audience_id": audience_id or None,
            "remove": [
                {"signal_id": r, "label": existing[r].get("search_theme") or existing[r].get("audience_name") or existing[r].get("audience_id")}
                for r in removals
            ],
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def apply_asset_group_signals(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("GoogleAdsService")
    group_path = service.asset_group_path(cid, changes["asset_group_id"])
    operations = []
    for removal in changes.get("remove") or []:
        op = client.get_type("MutateOperation")
        op.asset_group_signal_operation.remove = f"customers/{cid}/assetGroupSignals/{removal['signal_id']}"
        operations.append(op)
    for theme in changes.get("add_search_themes") or []:
        op = client.get_type("MutateOperation")
        signal = op.asset_group_signal_operation.create
        signal.asset_group = group_path
        signal.search_theme.text = theme
        operations.append(op)
    if changes.get("audience_id"):
        op = client.get_type("MutateOperation")
        signal = op.asset_group_signal_operation.create
        signal.asset_group = group_path
        signal.audience.audience = f"customers/{cid}/audiences/{changes['audience_id']}"
        operations.append(op)
    service.mutate(customer_id=cid, mutate_operations=operations)
    return {
        "asset_group_id": changes["asset_group_id"],
        "search_themes_added": len(changes.get("add_search_themes") or []),
        "audience_added": bool(changes.get("audience_id")),
        "signals_removed": len(changes.get("remove") or []),
    }
