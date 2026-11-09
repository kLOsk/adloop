"""Responsive search ads."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.manage._common import (
    _PINS,
    Handler,
    Step,
    _call,
    _check_url,
    _ctx,
    _plan,
    _Progress,
    _require_unchanged,
)
from adloop.microsoft_ads.read import (
    _ads,
)
from adloop.microsoft_ads.write import _blocked as _guard
from adloop.microsoft_ads.write import (
    _validation_error,
    find_entity,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def _assets(items: list, limit: int, slots: tuple, field: str, errors: list[str]) -> list[dict]:
    links = []
    for i, item in enumerate(items or []):
        text, pin = (item.get("text"), item.get("pinned_field")) if isinstance(item, dict) else (item, None)
        text = str(text or "").strip()
        if not text or len(text) > limit:
            errors.append(f"{field}[{i}] is 1–{limit} characters (got {len(text)}): '{text}'.")
        link: dict[str, Any] = {"Asset": {"Type": "TextAsset", "Text": text}}
        if pin:
            normalized = _PINS.get(str(pin).strip().lower().replace(" ", "_"))
            if normalized not in slots:
                errors.append(f"{field}[{i}].pinned_field must be one of {list(slots)}.")
            link["PinnedField"] = normalized
        links.append(link)
    texts = [l["Asset"]["Text"].lower() for l in links]
    if len(set(texts)) != len(texts):
        errors.append(f"{field} repeat the same text.")
    for slot in slots:
        pinned = [l for l in links if l.get("PinnedField") == slot]
        unpinned = [l for l in links if not l.get("PinnedField")]
        if not pinned and not unpinned:
            errors.append(f"Nothing can fill {slot}: pin an asset there or leave some unpinned.")
    return links


def _rsa_fields(headlines, descriptions, final_url, path1, path2, errors, warnings, *, partial: bool) -> dict:
    fields: dict[str, Any] = {}
    if headlines is not None or not partial:
        h = _assets(headlines or [], 30, ("Headline1", "Headline2", "Headline3"), "headlines", errors)
        if not 3 <= len(h) <= 15:
            errors.append(f"An RSA takes 3–15 headlines (got {len(h)}).")
        fields["Headlines"] = h
    if descriptions is not None or not partial:
        d = _assets(descriptions or [], 90, ("Description1", "Description2"), "descriptions", errors)
        if not 2 <= len(d) <= 4:
            errors.append(f"An RSA takes 2–4 descriptions (got {len(d)}).")
        fields["Descriptions"] = d
    if final_url or not partial:
        _check_url((final_url or "").strip(), errors, warnings)
        fields["FinalUrls"] = [(final_url or "").strip()]
    for name, value in (("Path1", path1), ("Path2", path2)):
        if value is None:
            continue
        value = value.strip()
        if len(value) > 15 or "/" in value:
            errors.append(f"{name.lower()} is at most 15 characters, without '/'.")
        fields[name] = value
    if fields.get("Path2") and not (fields.get("Path1") or (partial and path1 is None)):
        errors.append("path2 needs path1.")
    if not partial and fields.get("Headlines") and len(fields["Headlines"]) < 8:
        warnings.append("8–15 distinct headlines give Microsoft more combinations to test.")
    pins = [l for l in fields.get("Headlines", []) + fields.get("Descriptions", []) if l.get("PinnedField")]
    if pins:
        warnings.append(f"{len(pins)} asset(s) are pinned; pinning limits the combinations Microsoft can show.")
    return fields


def draft_microsoft_responsive_search_ad(
    config: AdLoopConfig, *, ad_account_id: str = "", ad_group_id: str = "", headlines: list | None = None,
    descriptions: list | None = None, final_url: str = "", path1: str = "", path2: str = "",
) -> dict:
    """Draft a responsive search ad, created PAUSED."""
    operation = "microsoft_create_rsa"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    fields = _rsa_fields(headlines, descriptions, final_url, path1, path2, errors, warnings, partial=False)
    if errors:
        return _validation_error(errors)
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad_group", str(ad_group_id))
    if not found:
        return {"error": f"Microsoft ad group '{ad_group_id}' was not found in account {account}."}
    group, campaign = found["entity"], found["campaign"]
    ad = {"Type": "ResponsiveSearch", "Status": "Paused", **fields}
    for key in ("Path1", "Path2"):
        if not ad.get(key):
            ad.pop(key, None)
    if "Path1" not in ad:
        ad.pop("Path2", None)
        warnings.append("No display paths: path1/path2 make the display URL more specific.")
    return _plan(
        operation, "ad_group", str(ad_group_id), account, customer,
        {"entity_name": group.get("Name"), "campaign_name": campaign.get("Name"), "status_on_create": "Paused",
         "headlines": [l["Asset"]["Text"] + (f" [pinned {l['PinnedField']}]" if l.get("PinnedField") else "")
                       for l in fields["Headlines"]],
         "descriptions": [l["Asset"]["Text"] for l in fields["Descriptions"]],
         "final_url": fields["FinalUrls"][0], "path1": ad.get("Path1"), "path2": ad.get("Path2")},
        {"campaign_id": str(campaign.get("Id")), "ad": ad},
        warnings=warnings,
    )


def update_microsoft_responsive_search_ad(
    config: AdLoopConfig, *, ad_account_id: str = "", ad_id: str = "", headlines: list | None = None,
    descriptions: list | None = None, final_url: str = "", path1: str | None = None, path2: str | None = None,
) -> dict:
    """Draft RSA changes: headlines or descriptions (each list replaced whole), landing page, paths."""
    operation = "microsoft_update_rsa"
    blocked = _guard(operation, config)
    if blocked:
        return blocked
    errors: list[str] = []
    warnings: list[str] = []
    fields = _rsa_fields(headlines, descriptions, final_url, path1, path2, errors, warnings, partial=True)
    if errors:
        return _validation_error(errors)
    if not fields:
        return {"error": "Nothing to change: pass headlines, descriptions, final_url, path1 or path2."}
    meta, account, customer = _ctx(config, ad_account_id)
    found = find_entity(config, account, customer, "ad", str(ad_id))
    if not found:
        return {"error": f"Microsoft ad '{ad_id}' was not found in account {account}."}
    ad, group, campaign = found["entity"], found["ad_group"], found["campaign"]
    if ad.get("Type") != "ResponsiveSearch":
        return _validation_error([f"Ad {ad_id} is a {ad.get('Type')} ad; only responsive search ads are edited here."])
    shown = {}
    for key, value in fields.items():
        if key in ("Headlines", "Descriptions"):
            shown[key.lower()] = {"from": [l.get("Asset", {}).get("Text") for l in ad.get(key) or []],
                                  "to": [l["Asset"]["Text"] for l in value]}
        else:
            shown[key.lower()] = {"from": ad.get(key), "to": value}
    warnings.append("Edited ads go back to Microsoft's editorial review.")
    return _plan(
        operation, "ad", str(ad_id), account, customer,
        {"entity_name": f"RSA {ad_id}", "ad_group_name": group.get("Name"), "campaign_name": campaign.get("Name"),
         "changes": shown},
        {"campaign_id": str(campaign.get("Id")), "ad_group_id": str(group.get("Id")),
         "update": {"Type": "ResponsiveSearch", "Id": str(ad_id), **fields},
         "before": {k: copy.deepcopy(ad.get(k)) for k in fields}},
        warnings=warnings,
    )


def _check_create_rsa(s: Step) -> None:
    config, plan, c, account, customer = s.config, s.plan, s.c, s.account, s.customer
    found = s.entity("ad_group", plan.entity_id)
    errors: list[str] = []
    _check_url(c["final_url"], errors, [])
    if errors:
        raise ValueError("; ".join(errors))
    wanted = sorted(l["Asset"]["Text"] for l in c["ad"]["Headlines"])
    for ad in _ads(config, account, customer, plan.entity_id):
        if sorted(str(l.get("Asset", {}).get("Text")) for l in ad.get("Headlines") or []) == wanted:
            raise ValueError(f"Ad group '{found['entity'].get('Name')}' already has an RSA with these headlines (ad {ad.get('Id')}).")


def _apply_create_rsa(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    payload = _call(config, c, "POST", "Ads", {"AdGroupId": plan.entity_id, "Ads": [c["ad"]]}, "responsive search ad")
    return {"ad_id": str((payload.get("AdIds") or [None])[0]), "status": "Paused",
            "next": "Enable it with enable_microsoft_entity once reviewed."}


def _check_update_rsa(s: Step) -> None:
    plan, c = s.plan, s.c
    found = s.entity("ad", plan.entity_id)
    _require_unchanged(found["entity"], c["before"], "ad")
    if c["update"].get("FinalUrls"):
        errors = []
        _check_url(c["update"]["FinalUrls"][0], errors, [])
        if errors:
            raise ValueError("; ".join(errors))


def _apply_update_rsa(s: Step, progress: _Progress) -> dict:
    config, plan, c = s.config, s.plan, s.c
    _call(config, c, "PUT", "Ads", {"AdGroupId": c["ad_group_id"], "Ads": [c["update"]]}, "responsive search ad update")
    return {"ad_id": plan.entity_id, "updated": sorted(k for k in c["update"] if k not in ("Id", "Type"))}


HANDLERS = {
    "microsoft_create_rsa": Handler(_check_create_rsa, _apply_create_rsa),
    "microsoft_update_rsa": Handler(_check_update_rsa, _apply_update_rsa),
}
