"""Ad extensions: sitelinks, callouts, structured snippets, call and image extensions, and their read."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.auth import MicrosoftAdsApiError
from adloop.microsoft_ads.client import campaign_request
from adloop.microsoft_ads.manage._common import (
    _EXTENSION_TYPES,
    Handler,
    Step,
    _call,
    _check_url,
    _chunks,
    _clean,
    _ctx,
    _plan,
    _Progress,
)
from adloop.microsoft_ads.read import (
    _ad_groups,
    _campaigns,
)
from adloop.microsoft_ads.write import _blocked as _guard
from adloop.microsoft_ads.write import (
    _validation_error,
    find_entity,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

# The plan's entity_type for each Microsoft AssociationType.
_ASSOCIATION_ENTITY = {"Account": "account", "Campaign": "campaign", "AdGroup": "ad_group"}


def _extension_target(config, account, customer, campaign_id: str, scope: str,
                      ad_group_id: str = "") -> tuple[str, str, str] | dict:
    """(AssociationType, entity id, label) of where an extension links; ad groups only when the caller allows them."""
    if scope == "account":
        if campaign_id or ad_group_id:
            given = "ad_group_id" if ad_group_id else "campaign_id"
            return _validation_error([f"Pass {given} or scope='account', not both."])
        return "Account", account, f"account {account}"
    if ad_group_id:
        if campaign_id:
            return _validation_error(["Pass campaign_id or ad_group_id, not both."])
        found = find_entity(config, account, customer, "ad_group", str(ad_group_id))
        if not found:
            return {"error": f"Microsoft ad group '{ad_group_id}' was not found in account {account}."}
        return ("AdGroup", str(ad_group_id),
                f"ad group '{found['entity'].get('Name')}' in campaign '{found['campaign'].get('Name')}'")
    if not campaign_id:
        return _validation_error(["campaign_id is required (or scope='account' for every campaign without its own)."])
    found = find_entity(config, account, customer, "campaign", str(campaign_id))
    if not found:
        return {"error": f"Microsoft campaign '{campaign_id}' was not found in account {account}."}
    return "Campaign", str(campaign_id), f"campaign '{found['entity'].get('Name')}'"


def _associated_extensions(config, account, customer, association_type: str, entity_id: str | list[str]) -> list[dict]:
    """Extension links of one entity or of up to 100 (Microsoft's EntityIds limit); each carries its EntityId."""
    ids = [str(i) for i in (entity_id if isinstance(entity_id, list) else [entity_id])]
    body = {"AccountId": account, "AdExtensionType": _EXTENSION_TYPES, "AssociationType": association_type,
            "EntityIds": [int(i) for i in ids]}
    payload = campaign_request(config, "AdExtensionsAssociations/Query", body, account_id=account, customer_id=customer)
    out = []
    # One collection per requested entity, in the request's order.
    for owner, collection in zip(ids, payload.get("AdExtensionAssociationCollection") or [], strict=False):
        for assoc in (collection or {}).get("AdExtensionAssociations") or []:
            if isinstance(assoc, dict) and isinstance(assoc.get("AdExtension"), dict):
                out.append({**assoc, "EntityId": str(assoc.get("EntityId") or owner)})
    return out


def _draft_extensions(config, *, ad_account_id, campaign_id, scope, operation, kind, extensions, shown, warnings) -> dict:
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    meta, account, customer = _ctx(config, ad_account_id)
    target = _extension_target(config, account, customer, str(campaign_id or ""), (scope or "").strip().lower())
    if isinstance(target, dict):
        return target
    association, entity_id, where = target
    existing = _associated_extensions(config, account, customer, association, entity_id)
    same = [a["AdExtension"] for a in existing if a["AdExtension"].get("Type") == kind]
    if kind == "CallAdExtension" and same:
        return _validation_error([f"{where} already has a call extension; Microsoft allows one per campaign."])
    def key(ext):  # noqa: E306
        return json.dumps({k: ext.get(k) for k in ("Text", "DisplayText", "Header", "Values", "PhoneNumber")}, sort_keys=True).lower()
    have = {key(e) for e in same}
    new = [e for e in extensions if key(e) not in have]
    if not new:
        return {"error": f"Every one of these is already on {where}."}
    if len(new) < len(extensions):
        warnings.append(f"Skipped {len(extensions) - len(new)} already on {where}.")
    return _plan(
        operation, _ASSOCIATION_ENTITY[association], entity_id, account, customer,
        {"entity_name": where, **shown},
        {"association_type": association, "entity_id": entity_id, "extensions": new},
        warnings=warnings,
    )


def draft_microsoft_sitelinks(config, *, ad_account_id="", campaign_id="", scope="", sitelinks=None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    exts = []
    for i, s in enumerate(sitelinks or []):
        if not isinstance(s, dict):
            errors.append(f"sitelinks[{i}] must be {{link_text, final_url, description1, description2}}.")
            continue
        text = str(s.get("link_text") or "").strip()
        d1, d2 = str(s.get("description1") or "").strip(), str(s.get("description2") or "").strip()
        limit = 25 if (d1 or d2) else 35
        if not text or len(text) > limit:
            errors.append(f"sitelinks[{i}].link_text is 1–{limit} characters.")
        if bool(d1) != bool(d2):
            errors.append(f"sitelinks[{i}] needs both descriptions or neither.")
        if len(d1) > 35 or len(d2) > 35:
            errors.append(f"sitelinks[{i}] descriptions are at most 35 characters each.")
        _check_url(str(s.get("final_url") or "").strip(), errors, warnings, field=f"sitelinks[{i}].final_url")
        ext = {"Type": "SitelinkAdExtension", "DisplayText": text, "FinalUrls": [str(s.get("final_url") or "").strip()]}
        if d1:
            ext.update(Description1=d1, Description2=d2)
        exts.append(ext)
    if not exts and not errors:
        errors.append("sitelinks needs at least one sitelink.")
    if len(exts) > 20:
        errors.append("At most 20 sitelinks per campaign.")
    if errors:
        return _validation_error(errors)
    if len(exts) < 4:
        warnings.append("Four or more sitelinks let Microsoft show the larger sitelink formats.")
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope=scope,
                             operation="microsoft_add_sitelinks", kind="SitelinkAdExtension", extensions=exts,
                             shown={"sitelinks": [
                                 f"{e['DisplayText']} → {e['FinalUrls'][0]}"
                                 + (f" ({e['Description1']} / {e['Description2']})" if e.get("Description1") else "")
                                 for e in exts]},
                             warnings=warnings)


def draft_microsoft_callouts(config, *, ad_account_id="", campaign_id="", scope="", callouts=None) -> dict:
    errors: list[str] = []
    texts = _clean(callouts)
    for t in texts:
        if len(t) > 25:
            errors.append(f"Callout '{t}' is longer than 25 characters.")
    if not texts:
        errors.append("callouts needs at least one callout.")
    if errors:
        return _validation_error(errors)
    warnings = [] if len(texts) >= 2 else ["A campaign needs at least 2 callouts before Microsoft shows them."]
    exts = [{"Type": "CalloutAdExtension", "Text": t} for t in dict.fromkeys(texts)]
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope=scope,
                             operation="microsoft_add_callouts", kind="CalloutAdExtension", extensions=exts,
                             shown={"callouts": [e["Text"] for e in exts]}, warnings=warnings)


def draft_microsoft_structured_snippet(config, *, ad_account_id="", campaign_id="", scope="", header="", values=None) -> dict:
    errors: list[str] = []
    vals = list(dict.fromkeys(_clean(values)))
    if not (header or "").strip():
        errors.append("header is required (one of Microsoft's headers, e.g. Services, Brands, Types).")
    if not 3 <= len(vals) <= 10:
        errors.append(f"A structured snippet takes 3–10 values (got {len(vals)}).")
    for v in vals:
        if len(v) > 25:
            errors.append(f"Value '{v}' is longer than 25 characters.")
    if errors:
        return _validation_error(errors)
    ext = {"Type": "StructuredSnippetAdExtension", "Header": header.strip(), "Values": vals}
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope=scope,
                             operation="microsoft_add_structured_snippet", kind="StructuredSnippetAdExtension",
                             extensions=[ext], shown={"snippet": f"{ext['Header']}: {', '.join(vals)}"}, warnings=[])


def draft_microsoft_call_extension(config, *, ad_account_id="", campaign_id="", phone_number="", country_code="") -> dict:
    errors: list[str] = []
    number = (phone_number or "").strip()
    country = (country_code or "").strip().upper()
    if not number or len(number) > 35:
        errors.append("phone_number is required (at most 35 characters).")
    if len(country) != 2 or not country.isalpha():
        errors.append("country_code is the two-letter country of the number, e.g. US.")
    if errors:
        return _validation_error(errors)
    ext = {"Type": "CallAdExtension", "PhoneNumber": number, "CountryCode": country, "IsCallOnly": False}
    return _draft_extensions(config, ad_account_id=ad_account_id, campaign_id=campaign_id, scope="",
                             operation="microsoft_add_call_extension", kind="CallAdExtension", extensions=[ext],
                             shown={"phone_number": number, "country_code": country},
                             warnings=["Use the call-tracking number that should receive Microsoft calls."])


# ---------------------------------------------------------------------------
# Image extensions
# ---------------------------------------------------------------------------
# Microsoft's limits, from its documentation (checked 2026-11-05):
#   [EXT]   ImageAdExtension: learn.microsoft.com/advertising/campaign-management-service/imageadextension
#   [IMG]   Image (media):    learn.microsoft.com/advertising/campaign-management-service/image
#   [ADD]   AddMedia:         learn.microsoft.com/advertising/campaign-management-service/addmedia
#   [RSA]   ResponsiveAd Remarks (same media library):
#                             learn.microsoft.com/advertising/campaign-management-service/responsivead#remarks
#   [BULK]  Image Ad Extension record: learn.microsoft.com/advertising/bulk-service/image-ad-extension
#   [HELP]  Add images to your ad with image extensions:
#                             learn.microsoft.com/advertising/msa-help/hlp_ba_proc_addimageextension
#   [GUIDE] Image guidelines and policies: learn.microsoft.com/advertising/msa-help/hlp_ba_conc_imageguidelines
#   [AUD]   Audience ads guide: learn.microsoft.com/advertising/guides/audience-ads

# GIF, JPEG and PNG, no animation [IMG Remarks, GUIDE].
_IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF"}
# "1.91:1 aspect ratio is required for the extension to be eligible to serve"
# with a minimum of 703 x 368 pixels [GUIDE, HELP]; Microsoft crops the other
# placements' ratios from it. The extension links the image as its
# LandscapeImageMedia asset, which is 1.91:1 [BULK images.subType].
_IMAGE_RATIO = 1.91
_IMAGE_RATIO_TOLERANCE = 0.01  # 1200x628 is 1.911, 703x368 is 1.910
_IMAGE_MIN_SIZE = (703, 368)
# "The maximum width and height in pixels are 2592 and 2048 independently" [IMG, RSA].
_IMAGE_MAX_SIZE = (2592, 2048)
# "The maximum file size is 5 MB" [RSA Remarks]. [IMG] also names a cap of
# 102,400 base64 characters (about 75 KB) for Image.Data, but Microsoft does
# not enforce it on upload: JPEGs of 125-300 KB went through on a live
# account. The draft enforces the 5 MB limit only.
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
# "You can add a maximum of 10 media in a single call" [ADD].
_IMAGES_PER_DRAFT = 10
_ALT_TEXT_MAX = 35        # AlternativeText, required [EXT]
_DISPLAY_TEXT_MAX = 35    # DisplayText, required for the SearchMultiple layout [EXT]
_DESCRIPTION_MAX = 100    # Description, internal label, not shown [EXT]
# Image extensions linked to one account, campaign or ad group: 25 in [BULK]
# and [HELP], 6 in [EXT] and the "About ad extensions" association table.
_IMAGE_LINKS_MAX = 25
_IMAGE_LINKS_LOW = 6


def _gif_frames(data: bytes) -> int:
    """Image frames in a GIF (2 is enough to call it animated)."""
    def skip_blocks(i: int) -> int:
        while data[i]:
            i += data[i] + 1
        return i + 1

    frames = 0
    try:
        flags = data[10]
        i = 13 + (3 * 2 ** ((flags & 0x07) + 1) if flags & 0x80 else 0)
        while i < len(data) and frames < 2:
            block = data[i]
            if block == 0x2C:  # image descriptor
                frames += 1
                flags = data[i + 9]
                i += 10 + (3 * 2 ** ((flags & 0x07) + 1) if flags & 0x80 else 0)
                i = skip_blocks(i + 1)  # LZW minimum code size, then the data
            elif block == 0x21:  # extension
                i = skip_blocks(i + 2)
            else:
                break
    except IndexError:
        pass
    return frames


def _is_animated(data: bytes, mime_type: str) -> bool:
    if mime_type == "image/gif":
        return _gif_frames(data) > 1
    if mime_type == "image/png":
        # An APNG declares its animation (acTL) before the first image data.
        actl, idat = data.find(b"acTL"), data.find(b"IDAT")
        return actl != -1 and (idat == -1 or actl < idat)
    return False


def _crop_hint(width: int, height: int) -> str:
    if width / height > _IMAGE_RATIO:
        return f"{round(height * _IMAGE_RATIO)}x{height}"
    return f"{width}x{round(width / _IMAGE_RATIO)}"


def _image_problems(label: str, data: bytes, mime_type: str, width: int, height: int) -> list[str]:
    """Why Microsoft would refuse this image for an image extension (empty when it fits)."""
    problems = []
    if mime_type not in _IMAGE_FORMATS:
        problems.append(f"{label}: {mime_type} is not accepted; use JPEG, PNG or GIF.")
    elif _is_animated(data, mime_type):
        problems.append(f"{label}: animated images are not accepted; use a still image.")
    if len(data) > _IMAGE_MAX_BYTES:
        problems.append(f"{label}: {len(data) / 1024 / 1024:.1f} MB is over Microsoft's 5 MB limit.")
    if width < _IMAGE_MIN_SIZE[0] or height < _IMAGE_MIN_SIZE[1]:
        problems.append(f"{label}: {width}x{height} is smaller than Microsoft's minimum of 703x368 for image extensions.")
    if width > _IMAGE_MAX_SIZE[0] or height > _IMAGE_MAX_SIZE[1]:
        problems.append(f"{label}: {width}x{height} is larger than Microsoft's maximum (width 2592, height 2048); "
                        "scale it down, e.g. to 1200x628.")
    ratio = width / height if height else 0
    if abs(ratio - _IMAGE_RATIO) > _IMAGE_RATIO * _IMAGE_RATIO_TOLERANCE:
        problems.append(f"{label}: the aspect ratio is {ratio:.2f}:1; image extensions need 1.91:1 (e.g. 1200x628). "
                        f"Crop it to {_crop_hint(width, height)} first.")
    return problems


def _image_label(payload: dict) -> str:
    return str(payload.get("url") or payload.get("path") or f"upload {payload.get('upload_id')}")


def _draft_images(paths: list[str], urls: list[str], uploads: list[str]) -> tuple[list[dict], list[str]]:
    """Load, validate and pin (sha256) every image; the bytes stay out of the plan.

    Loading reuses the Google Ads image tools: image_payloads checks the files
    and fetches URLs through the private-address-proof fetcher, and
    load_image_bytes reads the approved bytes back.
    """
    from adloop.ads.write import image_payloads, load_image_bytes

    payloads, errors = image_payloads(paths, urls, uploads)
    images, seen = [], set()
    for payload in payloads:
        label = _image_label(payload)
        try:
            data, _name = load_image_bytes(payload)
        except Exception as exc:  # noqa: BLE001 - a fetch or upload problem is the user's to fix
            errors.append(f"{label}: {exc}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if payload.get("sha256") and payload["sha256"] != digest:
            errors.append(f"{label}: the image changed while it was being read; try again.")
            continue
        if digest in seen:
            errors.append(f"{label}: the same image is in this draft twice.")
            continue
        seen.add(digest)
        width, height = int(payload["width"]), int(payload["height"])
        errors.extend(_image_problems(label, data, str(payload["mime_type"]), width, height))
        images.append({**payload, "sha256": digest, "size_bytes": len(data),
                       "base64_chars": 4 * ((len(data) + 2) // 3)})
    return images, errors


def _approved_image(payload: dict) -> bytes:
    """The previewed bytes of an image, re-read now; refuses one that changed or no longer fits."""
    from adloop.ads.write import load_image_bytes

    label = _image_label(payload)
    data, _name = load_image_bytes(payload)
    if hashlib.sha256(data).hexdigest() != payload["sha256"]:
        raise ValueError(f"{label} changed since the preview; draft again.")
    problems = _image_problems(label, data, str(payload["mime_type"]), int(payload["width"]), int(payload["height"]))
    if problems:
        raise ValueError("; ".join(problems))
    return data


def _describe_ms_image(payload: dict) -> str:
    width, height = int(payload["width"]), int(payload["height"])
    return (f"{_image_label(payload)}: {_IMAGE_FORMATS.get(str(payload['mime_type']), payload['mime_type'])} "
            f"{width}x{height} ({width / height:.2f}:1), {payload['size_bytes'] / 1024:.1f} KB")


def _linked_images(config, account, customer, association: str, entity_id: str) -> list[dict]:
    return [a["AdExtension"] for a in _associated_extensions(config, account, customer, association, entity_id)
            if a["AdExtension"].get("Type") == "ImageAdExtension"]


def draft_microsoft_image_extension(
    config: AdLoopConfig,
    *,
    ad_account_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "",
    image_paths: list[str] | None = None,
    image_urls: list[str] | None = None,
    upload_ids: list[str] | None = None,
    alt_text: str = "",
    display_text: str = "",
    final_url: str = "",
) -> dict:
    """Draft image extensions (one per image) for a campaign, an ad group or the account.

    Every image is loaded and checked now against Microsoft's limits; the
    plan keeps its sha256, and the dry run and the apply read it again and
    refuse one that changed. The apply uploads the images to the account's
    media library (Media/Add), creates one ImageAdExtension per image and
    links them (AdExtensionsAssociations/Set).
    """
    from adloop.runtime import deployment_mode

    operation = "microsoft_add_image_extension"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    paths, urls, uploads = _clean(image_paths), _clean(image_urls), _clean(upload_ids)
    if paths and deployment_mode() == "server":
        return {"error": "image_paths can't be used here: local file paths aren't available on a hosted server; "
                         "pass image_urls or upload_ids instead."}
    alt, display, url = (alt_text or "").strip(), (display_text or "").strip(), (final_url or "").strip()
    errors: list[str] = []
    warnings: list[str] = []
    if not alt:
        errors.append("alt_text is required: it describes the image for screen readers and shows on hover "
                      f"(at most {_ALT_TEXT_MAX} characters).")
    elif len(alt) > _ALT_TEXT_MAX:
        errors.append(f"alt_text is {len(alt)} characters; Microsoft allows {_ALT_TEXT_MAX}.")
    if len(display) > _DISPLAY_TEXT_MAX:
        errors.append(f"display_text is {len(display)} characters; Microsoft allows {_DISPLAY_TEXT_MAX}.")
    count = len(paths) + len(urls) + len(uploads)
    if not count:
        errors.append("At least one image (image_paths, image_urls or upload_ids) is required.")
    elif count > _IMAGES_PER_DRAFT:
        errors.append(f"At most {_IMAGES_PER_DRAFT} images per draft (Microsoft uploads 10 per request); got {count}.")
    if url:
        _check_url(url, errors, warnings)
    if errors:
        return _validation_error(errors)
    images, image_errors = _draft_images(paths, urls, uploads)
    if image_errors:
        return _validation_error(image_errors)

    _meta, account, customer = _ctx(config, ad_account_id)
    target = _extension_target(config, account, customer, str(campaign_id or ""), (scope or "").strip().lower(),
                               ad_group_id=str(ad_group_id or "").strip())
    if isinstance(target, dict):
        return target
    association, entity_id, where = target
    existing = _linked_images(config, account, customer, association, entity_id)
    total = len(existing) + len(images)
    if total > _IMAGE_LINKS_MAX:
        return _validation_error([(
            f"{where} has {len(existing)} image extension(s); {len(images)} more would make {total}, over "
            f"Microsoft's {_IMAGE_LINKS_MAX} per account, campaign or ad group. Unlink some first "
            "(remove_microsoft_entity, entity_type extension_association).")])
    if total > _IMAGE_LINKS_LOW:
        warnings.append(f"{where} would have {total} image extensions. Microsoft's image extension reference "
                        f"lists {_IMAGE_LINKS_LOW} per entity while its help and bulk pages say "
                        f"{_IMAGE_LINKS_MAX}; if Microsoft refuses the link, the extensions stay in the "
                        "library unlinked.")
    layouts = ["SearchSingle"] + (["SearchMultiple"] if display else [])
    # [AUD]: "you can also extend search into native via image ad extensions".
    warnings.append("Image extensions can take these search ads beyond Bing results: Microsoft's audience ads "
                    "guide says search ads are extended to native placements on the Microsoft Audience Network "
                    "(Outlook, MSN) using their image extensions. Check the ad groups' Audience Network setting "
                    "(get_microsoft_ad_groups) if they should stay on search.")
    if display:
        # [HELP]: at least five images and four image extensions; not with
        # sitelink, enhanced sitelink, dynamic product, dynamic data or price cards.
        warnings.append("display_text makes them eligible for the multi-image layout too. Microsoft shows that "
                        "layout only with at least five images across at least four image extensions, and not "
                        "next to sitelink, price, dynamic product or dynamic data extension cards.")
    if any(p["mime_type"] == "image/png" for p in images):
        warnings.append("Microsoft converts PNG images to JPEG; upload a JPEG if the quality matters.")
    if existing:
        warnings.append(f"{where} already has {len(existing)} image extension(s); Microsoft rotates them.")
    shown: dict[str, Any] = {"images": [_describe_ms_image(p) for p in images], "alt_text": alt}
    if display:
        shown["display_text"] = display
    shown["final_url"] = url or "the ad's own final URL"
    shown["layouts"] = layouts
    return _plan(
        operation, _ASSOCIATION_ENTITY[association], entity_id, account, customer,
        {"entity_name": where, **shown},
        {"association_type": association, "entity_id": entity_id, "image_payloads": images},
        warnings=warnings,
    )


def _image_extension(c: dict, media_id: str, payload: dict) -> dict:
    """The ImageAdExtension for one uploaded image [EXT]; the image is its 1.91:1 LandscapeImageMedia asset."""
    ext: dict[str, Any] = {
        "Type": "ImageAdExtension",
        "AlternativeText": c["alt_text"],
        "Description": str(payload.get("name") or "")[:_DESCRIPTION_MAX] or None,
        "ImageMediaIds": [int(media_id)],
        "Images": [{"Asset": {"Type": "ImageAsset", "Id": int(media_id), "SubType": "LandscapeImageMedia"}}],
        "Layouts": list(c["layouts"]),
    }
    if c.get("display_text"):
        ext["DisplayText"] = c["display_text"]
    if c["final_url"].startswith(("http://", "https://")):
        ext["FinalUrls"] = [c["final_url"]]
    return {k: v for k, v in ext.items() if v is not None}


def _extension_row(assoc: dict, association: str, label: str, campaign_name: str | None) -> dict:
    ext = assoc["AdExtension"]
    kind = str(ext.get("Type") or "")
    entity_id = str(assoc["EntityId"])
    row: dict[str, Any] = {
        "level": _ASSOCIATION_ENTITY.get(association, association.lower()), "entity": label, "entity_id": entity_id,
        "type": kind.replace("AdExtension", ""), "extension_id": str(ext.get("Id")),
        "content": ext.get("Text") or ext.get("DisplayText") or ext.get("PhoneNumber")
        or (f"{ext.get('Header')}: {', '.join(ext.get('Values') or [])}" if ext.get("Header") else None),
        "final_urls": ext.get("FinalUrls"),
        "editorial_status": assoc.get("EditorialStatus"),
        "remove_id": f"{ext.get('Id')}~{entity_id}~{association}",
    }
    if association == "AdGroup":
        row["campaign"] = campaign_name
    if kind == "ImageAdExtension":
        row.update(content=ext.get("AlternativeText"), alt_text=ext.get("AlternativeText"),
                   display_text=ext.get("DisplayText"), media_ids=_image_media_ids(ext), layouts=ext.get("Layouts"),
                   label=ext.get("Description"), source=ext.get("SourceType"))
    return row


def _image_media_ids(ext: dict) -> list[str]:
    """The media an image extension shows: its ImageMediaIds, or the assets of its Images links."""
    media = [str(i) for i in ext.get("ImageMediaIds") or [] if i is not None] or [
        str(link["Asset"]["Id"]) for link in ext.get("Images") or []
        if isinstance(link, dict) and isinstance(link.get("Asset"), dict) and link["Asset"].get("Id") is not None]
    return sorted(set(media), key=lambda m: (len(m), m))


# The association query returns image extensions without their Images,
# DisplayText and Layouts; those come only from GetAdExtensionsByIds when
# ReturnAdditionalFields asks for them (an unknown field name fails the whole
# request). Media metadata then gives each image's size and a viewable URL.
_IMAGE_EXTRA_FIELDS = "Images,DisplayText,Layouts"


def _image_details(config: AdLoopConfig, account: str, customer: str, rows: list[dict]) -> list[str]:
    """Fill image rows with their media, display text, layouts and image links; insights on failure."""
    image_rows = [r for r in rows if r["type"] == "Image"]
    if not image_rows:
        return []
    try:
        extensions: dict[str, dict] = {}
        for chunk in _chunks(sorted({r["extension_id"] for r in image_rows})):
            payload = campaign_request(
                config, "AdExtensions/QueryByIds",
                {"AccountId": account, "AdExtensionIds": [int(i) for i in chunk],
                 "AdExtensionType": "ImageAdExtension", "ReturnAdditionalFields": _IMAGE_EXTRA_FIELDS},
                account_id=account, customer_id=customer,
            )
            extensions.update({str(e.get("Id")): e for e in payload.get("AdExtensions") or [] if isinstance(e, dict)})
        for row in image_rows:
            ext = extensions.get(row["extension_id"])
            if ext:
                row.update(media_ids=_image_media_ids(ext) or row["media_ids"],
                           display_text=ext.get("DisplayText") or row["display_text"],
                           layouts=ext.get("Layouts") or row["layouts"])
        media: dict[str, dict] = {}
        for chunk in _chunks(sorted({m for r in image_rows for m in r["media_ids"]})):
            payload = campaign_request(config, "MediaMetaData/QueryByIds",
                                       {"AccountId": account, "MediaIds": [int(m) for m in chunk]},
                                       account_id=account, customer_id=customer)
            for item in payload.get("MediaMetaData") or []:
                if not isinstance(item, dict):
                    continue
                reps = [r for r in item.get("Representations") or [] if isinstance(r, dict)]
                best = max(reps, key=lambda r: (r.get("Width") or 0) * (r.get("Height") or 0), default={})
                media[str(item.get("Id"))] = {"media_id": str(item.get("Id")), "media_type": item.get("MediaType"),
                                              "width": best.get("Width"), "height": best.get("Height"),
                                              "url": best.get("Url")}
    except MicrosoftAdsApiError as exc:
        return [f"Image details could not be read, so image rows lack their images and display text: {exc}"]
    for row in image_rows:
        row["images"] = [media.get(m) or {"media_id": m} for m in row["media_ids"]]
    return []


def get_microsoft_ad_extensions(config: AdLoopConfig, *, ad_account_id: str = "", campaign_id: str = "") -> dict:
    """Sitelinks, callouts, snippets, call and image extensions on ad groups, campaigns and the account."""
    meta, account, customer = _ctx(config, ad_account_id)
    campaigns = [c for c in _campaigns(config, account, customer) if not campaign_id or str(c.get("Id")) == str(campaign_id)]
    rows = []
    targets = [("Campaign", str(c.get("Id")), c.get("Name")) for c in campaigns]
    if not campaign_id:
        targets.append(("Account", account, "account"))
    for association, entity_id, label in targets:
        for a in _associated_extensions(config, account, customer, association, entity_id):
            rows.append(_extension_row(a, association, label, None))
    # Ad group links (image extensions often live there), up to 100 ad groups a request.
    groups = {str(g.get("Id")): (g, c) for c in campaigns for g in _ad_groups(config, account, customer, str(c.get("Id")))
              if g.get("Status") != "Deleted"}
    for chunk in _chunks(list(groups)):
        for a in _associated_extensions(config, account, customer, "AdGroup", chunk):
            group, campaign = groups.get(a["EntityId"], ({}, {}))
            rows.append(_extension_row(a, "AdGroup", group.get("Name") or a["EntityId"], campaign.get("Name")))
    insights = _image_details(config, account, customer, rows)
    for c in campaigns:
        mine = [r for r in rows if r["level"] == "campaign" and r["entity_id"] == str(c.get("Id"))]
        if not any(r["type"] == "Sitelink" for r in mine):
            insights.append(f"Campaign '{c.get('Name')}' has no sitelinks of its own.")
    rejected = [r for r in rows if r["editorial_status"] in ("Disapproved", "Inactive")]
    if rejected:
        insights.append(f"{len(rejected)} extension(s) are disapproved or inactive.")
    if not any(r["type"] == "Image" for r in rows):
        insights.append("No image extensions are linked. Microsoft may add autogenerated ones to ad groups "
                        "without any; draft_microsoft_image_extension adds your own.")
    return {"ad_account_id": account, "extensions": rows, "total": len(rows), "insights": insights}


def _apply_image_extensions(config: AdLoopConfig, c: dict, progress: _Progress) -> dict:
    """Media/Add, then one ImageAdExtension per image, then the links; a later failure names what exists."""
    from adloop.safety.outcomes import PartialApplyError

    account = c["ad_account_id"]
    payloads = c["image_payloads"]
    # Every image is read and checked before anything is sent.
    data = [_approved_image(p) for p in payloads]
    # REST v13 AddMedia is POST .../CampaignManagement/v13/Media [ADD]; GenericImage
    # is the MediaType Microsoft recommends for image extension media [IMG].
    media = [{"Type": "Image", "MediaType": "GenericImage", "Data": base64.b64encode(d).decode("ascii")} for d in data]
    payload = _call(config, c, "POST", "Media", {"AccountId": account, "Media": media}, "image upload")
    media_ids = [str(i) for i in payload.get("MediaIds") or [] if i is not None]
    if len(media_ids) != len(media):
        raise PartialApplyError(
            f"Microsoft stored {len(media_ids)} of {len(media)} images (media ids {media_ids}) without saying why; "
            "check the account's image library before drafting again.",
            created={"media_ids": media_ids},
        )
    progress.created["media_ids"] = media_ids
    progress.done.append(f"uploaded {len(media_ids)} image(s) to the media library (media ids {', '.join(media_ids)})")

    extensions = [_image_extension(c, mid, p) for mid, p in zip(media_ids, payloads, strict=True)]
    payload = progress.step("Creating the image extensions", lambda: _call(
        config, c, "POST", "AdExtensions", {"AccountId": account, "AdExtensions": extensions}, "image extensions"))
    ids = [str((i or {}).get("Id")) for i in payload.get("AdExtensionIdentities") or [] if i and i.get("Id")]
    progress.created["extension_ids"] = ids
    progress.done.append(f"created image extension(s) {', '.join(ids)}")
    progress.step("Linking the image extensions", lambda: _call(config, c, "POST", "AdExtensionsAssociations/Set", {
        "AccountId": account, "AssociationType": c["association_type"],
        "AdExtensionIdToEntityIdAssociations": [{"AdExtensionId": int(i), "EntityId": int(c["entity_id"])} for i in ids],
    }, "image extension links"))
    return {"media_ids": media_ids, "extension_ids": ids, "linked_to": c["entity_name"], "layouts": c["layouts"],
            "next": "The images go through Microsoft's editorial review; get_microsoft_ad_extensions shows each "
                    "extension's editorial_status."}


def _check_extensions(s: Step) -> None:
    config, c, account, customer, op = s.config, s.c, s.account, s.customer, s.plan.operation
    if c["association_type"] == "Campaign":
        s.entity("campaign", c["entity_id"])
    linked = _associated_extensions(config, account, customer, c["association_type"], c["entity_id"])
    if op == "microsoft_add_call_extension" and any(a["AdExtension"].get("Type") == "CallAdExtension" for a in linked):
        raise ValueError("The campaign got a call extension since the draft.")
    def key(ext):  # noqa: E306
        return json.dumps({k: ext.get(k) for k in ("Type", "Text", "DisplayText", "Header", "Values", "PhoneNumber")},
                          sort_keys=True).lower()
    now = {key(a["AdExtension"]) for a in linked}
    again = [e for e in c["extensions"] if key(e) in now]
    if again:
        raise ValueError(f"{len(again)} of these extensions are linked now (an earlier attempt may have added them).")
    if op == "microsoft_add_sitelinks":
        errors = []
        for ext in c["extensions"]:
            _check_url(ext["FinalUrls"][0], errors, [])
        if errors:
            raise ValueError("; ".join(errors))


def _apply_extensions(s: Step, progress: _Progress) -> dict:
    config, c, account = s.config, s.c, s.account
    payload = _call(config, c, "POST", "AdExtensions", {"AccountId": account, "AdExtensions": c["extensions"]}, "extensions")
    ids = [str((i or {}).get("Id")) for i in payload.get("AdExtensionIdentities") or [] if i]
    progress.done.append(f"created extension(s) {ids}")
    progress.step("Linking the extensions", lambda: _call(config, c, "POST", "AdExtensionsAssociations/Set", {
        "AccountId": account, "AssociationType": c["association_type"],
        "AdExtensionIdToEntityIdAssociations": [{"AdExtensionId": int(i), "EntityId": int(c["entity_id"])} for i in ids],
    }, "extension links"))
    return {"extension_ids": ids, "linked_to": c["entity_name"]}


def _check_add_image_extension(s: Step) -> None:
    config, c, account, customer = s.config, s.c, s.account, s.customer
    if c["association_type"] in ("Campaign", "AdGroup"):
        s.entity(_ASSOCIATION_ENTITY[c["association_type"]], c["entity_id"])
    images = c["image_payloads"]
    for payload in images:
        _approved_image(payload)  # re-reads (URLs re-fetched) and re-checks every image
    linked = _linked_images(config, account, customer, c["association_type"], c["entity_id"])
    if len(linked) + len(images) > _IMAGE_LINKS_MAX:
        raise ValueError(f"{c['entity_name']} now has {len(linked)} image extension(s); {len(images)} more would "
                         f"go over Microsoft's {_IMAGE_LINKS_MAX}.")
    if c["final_url"].startswith(("http://", "https://")):
        errors = []
        _check_url(c["final_url"], errors, [])
        if errors:
            raise ValueError("; ".join(errors))
    s.checks["images_verified"] = len(images)


def _apply_add_image_extension(s: Step, progress: _Progress) -> dict:
    config, c = s.config, s.c
    return _apply_image_extensions(config, c, progress)


HANDLERS = {
    "microsoft_add_sitelinks": Handler(_check_extensions, _apply_extensions),
    "microsoft_add_callouts": Handler(_check_extensions, _apply_extensions),
    "microsoft_add_structured_snippet": Handler(_check_extensions, _apply_extensions),
    "microsoft_add_call_extension": Handler(_check_extensions, _apply_extensions),
    "microsoft_add_image_extension": Handler(_check_add_image_extension, _apply_add_image_extension),
}
