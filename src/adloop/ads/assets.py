"""Asset extensions: callouts, structured snippets, sitelinks, business name.

Where an asset is linked is its *scope*: an ad group (AdGroupAsset), a
campaign (CampaignAsset) or the whole account (CustomerAsset). Every tool
here resolves it the same way (:func:`resolve_scope`): the scope follows the
id that was passed (``ad_group_id`` or ``campaign_id``), and account-wide
linking has to be asked for by name with ``scope="account"``, because it
reaches every campaign without assets of its own.

Edits change an asset in place (``update_callout``, ``update_sitelink``,
``update_structured_snippet``): the asset keeps its id and performance
history everywhere it is linked. The preview pins the values it read, and
apply re-reads the asset first: an edit made in Google Ads in between stops
the apply before anything is sent.

Based on work by Illia Sapryga (PR #95).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

SCOPES = ("campaign", "ad_group", "account")

# scope -> the link resource (GAQL name and the plan's entity_type).
LINK_RESOURCE = {
    "ad_group": "ad_group_asset",
    "campaign": "campaign_asset",
    "account": "customer_asset",
}

# Field types CustomerAsset accepts, mapped to the asset type each one links
# ("Asset types linked to customers, campaigns, and ad groups" in the Google
# Ads API assets overview). BUSINESS_MESSAGE is left out on purpose: only one
# per message provider may be active, which this tool does not check.
CUSTOMER_ASSET_FIELD_TYPES = {
    "BUSINESS_NAME": "TEXT",
    "BUSINESS_LOGO": "IMAGE",
    "CALL": "CALL",
    "CALLOUT": "CALLOUT",
    "HOTEL_CALLOUT": "HOTEL_CALLOUT",
    "MOBILE_APP": "MOBILE_APP",
    "PRICE": "PRICE",
    "PROMOTION": "PROMOTION",
    "SITELINK": "SITELINK",
    "STRUCTURED_SNIPPET": "STRUCTURED_SNIPPET",
}

IN_PLACE_WARNING = (
    "In-place edit: the asset keeps its ID and performance history, and the new "
    "content shows everywhere this asset is linked (other campaigns, ad groups or "
    "the account)."
)


def _is_numeric_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit()


def _clean(value: object) -> str:
    return str(value or "").strip()


def resolve_scope(
    scope: str,
    campaign_id: str,
    ad_group_id: str,
    *,
    allowed: tuple[str, ...] = SCOPES,
) -> tuple[str, str, str, list[str]]:
    """``(scope, campaign_id, ad_group_id, errors)``, the ids stripped.

    Without a scope it follows the id: ``ad_group_id`` links to the ad group,
    ``campaign_id`` to the campaign. Account scope is never inferred. An
    explicit scope must come with exactly the id it links to.
    """
    scope = _clean(scope).lower()
    campaign_id, ad_group_id = _clean(campaign_id), _clean(ad_group_id)
    errors: list[str] = []

    if scope and scope not in allowed:
        return scope, campaign_id, ad_group_id, [
            f"scope must be one of {list(allowed)}, or empty to follow the id; got '{scope}'"
        ]
    if not scope:
        if campaign_id and ad_group_id:
            return "", campaign_id, ad_group_id, [
                "Pass campaign_id or ad_group_id, not both: the asset is linked to one of them"
            ]
        if ad_group_id:
            if "ad_group" not in allowed:
                return "", campaign_id, ad_group_id, [
                    f"This asset cannot be linked to an ad group; use {' or '.join(allowed)} scope"
                ]
            scope = "ad_group"
        elif campaign_id:
            scope = "campaign"
        else:
            return "", campaign_id, ad_group_id, [
                "campaign_id or ad_group_id is required; account-wide linking needs scope='account'"
            ]

    if scope == "campaign":
        if not campaign_id:
            errors.append("campaign_id is required for scope='campaign'")
        elif not _is_numeric_id(campaign_id):
            errors.append(f"campaign_id must be numeric, got '{campaign_id}'")
        if ad_group_id:
            errors.append("ad_group_id is not used with scope='campaign'")
    elif scope == "ad_group":
        if not ad_group_id:
            errors.append("ad_group_id is required for scope='ad_group'")
        elif not _is_numeric_id(ad_group_id):
            errors.append(f"ad_group_id must be numeric, got '{ad_group_id}'")
        if campaign_id:
            errors.append("campaign_id is not used with scope='ad_group'")
    elif campaign_id or ad_group_id:
        errors.append("scope='account' links account-wide; leave campaign_id and ad_group_id empty")
    return scope, campaign_id, ad_group_id, errors


def scope_entity(scope: str, customer_id: str, campaign_id: str, ad_group_id: str) -> tuple[str, str]:
    """The ChangePlan ``(entity_type, entity_id)`` for a scope."""
    entity_id = {"ad_group": ad_group_id, "campaign": campaign_id}.get(scope, customer_id)
    return LINK_RESOURCE[scope], entity_id


def _account_scope_warning(kind: str) -> str:
    return (
        f"Account-level {kind} serve on every eligible campaign in the account that "
        f"has no {kind} of its own at campaign or ad-group level."
    )


def _validate_callout_texts(callouts: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    validated: list[str] = []
    if not callouts:
        errors.append("At least one callout is required")
    for index, callout in enumerate(callouts):
        text = _clean(callout)
        if not text:
            errors.append(f"Callout {index + 1}: text is required")
        elif len(text) > 25:
            errors.append(f"Callout {index + 1}: '{text}' is {len(text)} chars (max 25)")
        else:
            validated.append(text)
    return validated, errors


def _header_error(index: int, header: str) -> str:
    from adloop.ads.snippet_headers import ENGLISH_HEADERS

    return (
        f"Structured snippet {index + 1}: header '{header}' is not one of Google's snippet "
        f"headers. English: {', '.join(ENGLISH_HEADERS)}. Google's translations are "
        "accepted too, in the language of the values (German 'Typen', 'Marken', "
        "'Dienstleistungen'): https://developers.google.com/google-ads/api/data/structured-snippet-headers"
    )


def _validate_snippet_items(
    snippets: list[dict], *, keep_header: str = ""
) -> tuple[list[dict], list[str]]:
    """Check snippets; ``keep_header`` is an existing asset's header, accepted as is."""
    from adloop.ads.snippet_headers import canonical_header

    errors: list[str] = []
    validated: list[dict] = []
    if not snippets:
        errors.append("At least one structured snippet is required")
    for index, snippet in enumerate(snippets):
        header = _clean(snippet.get("header", ""))
        values = [_clean(value) for value in snippet.get("values", []) or []]
        if keep_header and header == keep_header:
            pass  # Google already holds this header; it is not ours to reject.
        elif canonical := canonical_header(header):
            header = canonical
        else:
            errors.append(_header_error(index, header))
        if len(values) < 3 or len(values) > 10:
            errors.append(f"Structured snippet {index + 1}: values must contain 3-10 items")
        for value_index, value in enumerate(values):
            if not value:
                errors.append(f"Structured snippet {index + 1}: value {value_index + 1} is required")
            elif len(value) > 25:
                errors.append(
                    f"Structured snippet {index + 1}: value '{value}' is {len(value)} chars (max 25)"
                )
        validated.append({"header": header, "values": values})
    return validated, errors


def _blocked(operation: str, config: AdLoopConfig) -> dict | None:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}
    return None


# ---------------------------------------------------------------------------
# Drafts: new assets, scoped
# ---------------------------------------------------------------------------


def draft_callouts(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "",
    callouts: list[str] | None = None,
) -> dict:
    """Draft callout assets linked to an ad group, a campaign or the account."""
    from adloop.safety.preview import ChangePlan, store_plan

    if blocked := _blocked("create_callouts", config):
        return blocked
    scope, campaign_id, ad_group_id, errors = resolve_scope(scope, campaign_id, ad_group_id)
    validated, text_errors = _validate_callout_texts(callouts or [])
    errors.extend(text_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    entity_type, entity_id = scope_entity(scope, customer_id, campaign_id, ad_group_id)
    plan = ChangePlan(
        operation="create_callouts",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"scope": scope, "campaign_id": campaign_id, "ad_group_id": ad_group_id,
                 "callouts": validated},
    )
    store_plan(plan)
    preview = plan.to_preview()
    if scope == "account":
        preview["warnings"] = [_account_scope_warning("callouts")]
    return preview


def draft_structured_snippets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "",
    snippets: list[dict] | None = None,
) -> dict:
    """Draft structured snippet assets linked to an ad group, a campaign or the account."""
    from adloop.safety.preview import ChangePlan, store_plan

    if blocked := _blocked("create_structured_snippets", config):
        return blocked
    scope, campaign_id, ad_group_id, errors = resolve_scope(scope, campaign_id, ad_group_id)
    validated, snippet_errors = _validate_snippet_items(snippets or [])
    errors.extend(snippet_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    entity_type, entity_id = scope_entity(scope, customer_id, campaign_id, ad_group_id)
    plan = ChangePlan(
        operation="create_structured_snippets",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"scope": scope, "campaign_id": campaign_id, "ad_group_id": ad_group_id,
                 "snippets": validated},
    )
    store_plan(plan)
    preview = plan.to_preview()
    if scope == "account":
        preview["warnings"] = [_account_scope_warning("structured snippets")]
    return preview


def draft_business_name_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    business_name: str = "",
    campaign_id: str = "",
    scope: str = "",
) -> dict:
    """Draft a business name (TEXT asset linked as BUSINESS_NAME), campaign or account."""
    from adloop.safety.preview import ChangePlan, store_plan

    if blocked := _blocked("create_business_name_asset", config):
        return blocked
    # Google links BUSINESS_NAME at campaign or account level, not ad group.
    scope, campaign_id, _, errors = resolve_scope(scope, campaign_id, "", allowed=("campaign", "account"))
    text = _clean(business_name)
    if not text:
        errors.append("business_name is required")
    elif len(text) > 25:
        errors.append(f"business_name '{text}' is {len(text)} chars (max 25)")
    if errors:
        return {"error": "Validation failed", "details": errors}

    entity_type, entity_id = scope_entity(scope, customer_id, campaign_id, "")
    plan = ChangePlan(
        operation="create_business_name_asset",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"scope": scope, "campaign_id": campaign_id, "business_name": text},
    )
    store_plan(plan)
    preview = plan.to_preview()
    if scope == "account":
        preview["warnings"] = [_account_scope_warning("business names")]
    return preview


def link_asset_to_customer(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    links: list[dict] | None = None,
) -> dict:
    """Draft account-level (CustomerAsset) links for assets that already exist."""
    from adloop.ads.gaql import execute_query
    from adloop.safety.preview import ChangePlan, store_plan

    if blocked := _blocked("link_asset_to_customer", config):
        return blocked
    if not links:
        return {"error": "At least one link is required"}

    errors: list[str] = []
    validated: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(links):
        if not isinstance(item, dict):
            errors.append(f"Link {i + 1}: must be an object with asset_id and field_type")
            continue
        asset_id = _clean(item.get("asset_id"))
        field_type = _clean(item.get("field_type")).upper()
        if not _is_numeric_id(asset_id):
            errors.append(f"Link {i + 1}: asset_id must be numeric, got '{asset_id}'")
            continue
        if field_type not in CUSTOMER_ASSET_FIELD_TYPES:
            errors.append(
                f"Link {i + 1}: field_type '{field_type}' cannot be linked at account level; "
                f"allowed: {sorted(CUSTOMER_ASSET_FIELD_TYPES)}"
            )
            continue
        if (asset_id, field_type) not in seen:
            seen.add((asset_id, field_type))
            validated.append({"asset_id": asset_id, "field_type": field_type})
    if errors:
        return {"error": "Validation failed", "details": errors}

    ids = ", ".join(sorted({link["asset_id"] for link in validated}))
    rows = execute_query(
        config, customer_id, f"SELECT asset.id, asset.type, asset.name FROM asset WHERE asset.id IN ({ids})"
    )
    found = {str(row.get("asset.id")): row for row in rows}
    for link in validated:
        row = found.get(link["asset_id"])
        expected = CUSTOMER_ASSET_FIELD_TYPES[link["field_type"]]
        if row is None:
            errors.append(f"Asset {link['asset_id']} was not found in this account")
        elif row.get("asset.type") != expected:
            errors.append(
                f"Asset {link['asset_id']} is a {row.get('asset.type')} asset; "
                f"{link['field_type']} needs a {expected} asset"
            )
        elif row.get("asset.name"):
            link["asset_name"] = row["asset.name"]
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
    preview = plan.to_preview()
    preview["warnings"] = [(
        "Account-level assets serve on every eligible campaign that has no asset of "
        "the same type linked at campaign or ad-group level."
    )]
    return preview


# ---------------------------------------------------------------------------
# Drafts: in-place edits
# ---------------------------------------------------------------------------

# What each edit reads and pins: plan key -> GAQL field.
_CALLOUT_FIELDS = {"callout_text": "asset.callout_asset.callout_text"}
_SITELINK_FIELDS = {
    "link_text": "asset.sitelink_asset.link_text",
    "description1": "asset.sitelink_asset.description1",
    "description2": "asset.sitelink_asset.description2",
    "final_url": "asset.final_urls",
}
_SNIPPET_FIELDS = {
    "header": "asset.structured_snippet_asset.header",
    "values": "asset.structured_snippet_asset.values",
}


def _read_asset(config: AdLoopConfig, customer_id: str, asset_id: str, fields: dict) -> dict | None:
    """``{"type": ..., <plan key>: value}`` for one asset, or None."""
    from adloop.ads.gaql import execute_query

    selected = ", ".join(("asset.id", "asset.type", *fields.values()))
    rows = execute_query(config, customer_id, f"SELECT {selected} FROM asset WHERE asset.id = {asset_id}")
    if not rows:
        return None
    return {"type": rows[0].get("asset.type"), **_normalize(
        {key: rows[0].get(field) for key, field in fields.items()}
    )}


def _normalize(values: dict) -> dict:
    """Comparable values: final_urls -> first URL, snippet values -> list."""
    out = dict(values)
    if "final_url" in out:
        urls = out["final_url"] or []
        out["final_url"] = (list(urls) or [""])[0] if not isinstance(urls, str) else urls
    if "values" in out:
        out["values"] = list(out["values"] or [])
    # An unset enum reads as UNSPECIFIED; treat it like an empty field.
    return {k: ("" if v is None or v in ("UNSPECIFIED", "UNKNOWN") else v) for k, v in out.items()}


def _asset_for_edit(config, customer_id, asset_id, fields, expected_type) -> tuple[dict | None, dict | None]:
    """``(current, error)``: the asset's pinned values, or why it cannot be edited."""
    current = _read_asset(config, customer_id, asset_id, fields)
    if current is None:
        return None, {"error": f"Asset {asset_id} was not found in this account"}
    if current["type"] != expected_type:
        return None, {"error": f"Asset {asset_id} is a {current['type']} asset, not a {expected_type}"}
    return current, None


def _edit_plan(operation: str, customer_id: str, asset_id: str, changed: dict, previous: dict,
               *, pinned: dict | None = None) -> dict:
    """Store an in-place edit. ``previous`` is shown; ``pinned`` (raw values) is re-checked at apply."""
    from adloop.safety.preview import ChangePlan, store_plan

    changes = {"asset_id": asset_id, **changed, "previous": previous}
    if pinned is not None:
        changes["pinned"] = pinned
    plan = ChangePlan(
        operation=operation,
        entity_type="asset",
        entity_id=asset_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [IN_PLACE_WARNING]
    return preview


def update_callout(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    callout_text: str = "",
) -> dict:
    """Draft an in-place edit of a callout asset's text."""
    if blocked := _blocked("update_callout", config):
        return blocked
    asset_id = _clean(asset_id)
    errors: list[str] = [] if _is_numeric_id(asset_id) else [f"asset_id must be numeric, got '{asset_id}'"]
    validated, text_errors = _validate_callout_texts([callout_text or ""])
    errors.extend(text_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}
    text = validated[0]

    current, error = _asset_for_edit(config, customer_id, asset_id, _CALLOUT_FIELDS, "CALLOUT")
    if error:
        return error
    if current["callout_text"] == text:
        return {"error": f"Callout {asset_id} already reads '{text}'; nothing to change"}
    return _edit_plan("update_callout", customer_id, asset_id, {"callout_text": text},
                      {"callout_text": current["callout_text"]})


def update_sitelink(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    link_text: str = "",
    final_url: str = "",
    description1: str = "",
    description2: str = "",
) -> dict:
    """Draft an in-place edit of a sitelink asset; empty fields stay as they are."""
    from adloop.ads.write import _validate_urls

    if blocked := _blocked("update_sitelink", config):
        return blocked
    asset_id = _clean(asset_id)
    new = {"link_text": _clean(link_text), "final_url": _clean(final_url),
           "description1": _clean(description1), "description2": _clean(description2)}
    errors: list[str] = [] if _is_numeric_id(asset_id) else [f"asset_id must be numeric, got '{asset_id}'"]
    if len(new["link_text"]) > 25:
        errors.append(f"link_text '{new['link_text']}' is {len(new['link_text'])} chars (max 25)")
    for key in ("description1", "description2"):
        if len(new[key]) > 35:
            errors.append(f"{key} is {len(new[key])} chars (max 35)")
    if not any(new.values()):
        errors.append("No changes specified: pass link_text, final_url, description1 and/or description2")
    if errors:
        return {"error": "Validation failed", "details": errors}

    current, error = _asset_for_edit(config, customer_id, asset_id, _SITELINK_FIELDS, "SITELINK")
    if error:
        return error
    previous = {key: current[key] for key in _SITELINK_FIELDS}
    changed = {k: v for k, v in new.items() if v and v != previous[k]}
    if not changed:
        return {"error": f"Sitelink {asset_id} already has these values; nothing to change"}
    after = {**previous, **changed}
    if bool(after["description1"]) != bool(after["description2"]):
        return {"error": "Validation failed", "details": [(
            "description1 and description2 must both be set (Google rejects a sitelink "
            "with only one description line)"
        )]}
    if "final_url" in changed:
        url_errors, _ = _validate_urls([changed["final_url"]])
        if url_errors.get(changed["final_url"]):
            return {"error": "URL validation failed: sitelinks must point to working URLs",
                    "details": [f"'{changed['final_url']}' is not reachable: {url_errors[changed['final_url']]}"]}
    return _edit_plan("update_sitelink", customer_id, asset_id, changed,
                      {k: previous[k] for k in changed})


def update_structured_snippet(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    header: str = "",
    values: list[str] | None = None,
) -> dict:
    """Draft an in-place edit of a structured snippet asset (header and values)."""
    if blocked := _blocked("update_structured_snippet", config):
        return blocked
    asset_id = _clean(asset_id)
    errors: list[str] = [] if _is_numeric_id(asset_id) else [f"asset_id must be numeric, got '{asset_id}'"]
    current = None
    if not errors:
        current, error = _asset_for_edit(config, customer_id, asset_id, _SNIPPET_FIELDS, "STRUCTURED_SNIPPET")
        if error:
            return error
    # An empty header keeps the current one, accepted as Google stores it;
    # values are always the full list.
    kept = (current or {}).get("header", "")
    snippet = {"header": _clean(header) or kept, "values": values or []}
    validated, snippet_errors = _validate_snippet_items([snippet], keep_header=kept)
    errors.extend(snippet_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}
    snippet = validated[0]
    previous = {"header": current["header"], "values": current["values"]}
    if previous == snippet:
        return {"error": f"Structured snippet {asset_id} already has this header and these values"}
    return _edit_plan("update_structured_snippet", customer_id, asset_id, snippet, previous)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _link_operation(client, cid: str, asset_resource: str, field_type, scope: str,
                    campaign_id: str, ad_group_id: str):
    """A MutateOperation linking ``asset_resource`` at ``scope``."""
    googleads_service = client.get_service("GoogleAdsService")
    op = client.get_type("MutateOperation")
    if scope == "ad_group":
        link = op.ad_group_asset_operation.create
        link.ad_group = googleads_service.ad_group_path(cid, ad_group_id)
    elif scope == "campaign":
        link = op.campaign_asset_operation.create
        link.campaign = googleads_service.campaign_path(cid, campaign_id)
    elif scope == "account":
        link = op.customer_asset_operation.create
    else:
        raise ValueError(f"Unknown asset scope: {scope}")
    link.asset = asset_resource
    link.field_type = field_type
    return op


def _create_and_link(client, cid: str, assets: list[dict], field_type, populate, changes: dict) -> dict:
    """Create assets and link them at the plan's scope in one Mutate.

    Plans from before scopes existed have no ``scope`` and link to their
    campaign, as they always did.
    """
    scope = changes.get("scope") or "campaign"
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []
    for i, payload in enumerate(assets):
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = asset_service.asset_path(cid, str(-(i + 1)))
        populate(asset, payload)
        operations.append(op)
    for i in range(len(assets)):
        operations.append(_link_operation(
            client, cid, asset_service.asset_path(cid, str(-(i + 1))), field_type, scope,
            changes.get("campaign_id", ""), changes.get("ad_group_id", ""),
        ))
    response = googleads_service.mutate(customer_id=cid, mutate_operations=operations)

    link = LINK_RESOURCE[scope]
    link_key = f"{link}s"
    results: dict[str, list[str]] = {"assets": [], link_key: []}
    for i, resp in enumerate(response.mutate_operation_responses):
        resource = (resp.asset_result.resource_name if i < len(assets)
                    else getattr(resp, f"{link}_result").resource_name)
        if resource:
            results["assets" if i < len(assets) else link_key].append(resource)
    return results


def _apply_create_callouts(client, cid: str, changes: dict) -> dict:
    def populate(asset, payload):
        asset.callout_asset.callout_text = payload["callout_text"]

    return _create_and_link(client, cid, [{"callout_text": t} for t in changes["callouts"]],
                            client.enums.AssetFieldTypeEnum.CALLOUT, populate, changes)


def _apply_create_structured_snippets(client, cid: str, changes: dict) -> dict:
    def populate(asset, payload):
        asset.structured_snippet_asset.header = payload["header"]
        asset.structured_snippet_asset.values.extend(payload["values"])

    return _create_and_link(client, cid, changes["snippets"],
                            client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET, populate, changes)


def _apply_create_business_name_asset(client, cid: str, changes: dict) -> dict:
    def populate(asset, payload):
        asset.type_ = client.enums.AssetTypeEnum.TEXT
        asset.text_asset.text = payload["business_name"]

    return _create_and_link(client, cid, [{"business_name": changes["business_name"]}],
                            client.enums.AssetFieldTypeEnum.BUSINESS_NAME, populate, changes)


def _apply_link_asset_to_customer(client, cid: str, changes: dict) -> dict:
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []
    for link in changes["links"]:
        op = client.get_type("MutateOperation")
        customer_asset = op.customer_asset_operation.create
        customer_asset.asset = asset_service.asset_path(cid, link["asset_id"])
        customer_asset.field_type = getattr(client.enums.AssetFieldTypeEnum, link["field_type"])
        operations.append(op)
    response = googleads_service.mutate(customer_id=cid, mutate_operations=operations)
    return {"customer_assets": [r.customer_asset_result.resource_name
                                for r in response.mutate_operation_responses]}


def _current_values(client, cid: str, asset_id: str, fields: dict) -> dict:
    """The asset's pinned fields as Google has them now (a read, so it also runs in a dry run).

    Converted exactly as the draft's GAQL read converts them (enum names,
    lists), so pinned and current values compare like for like.
    """
    from adloop.ads.gaql import _extract_field

    service = client.get_service("GoogleAdsService")
    query = f"SELECT asset.id, {', '.join(fields.values())} FROM asset WHERE asset.id = {asset_id}"
    for row in service.search(customer_id=cid, query=query):
        return _normalize({key: _extract_field(row, field) for key, field in fields.items()})
    raise RuntimeError(f"Asset {asset_id} no longer exists; nothing was sent.")


def _check_unchanged(client, cid: str, changes: dict, fields: dict) -> None:
    """Refuse when the asset changed in Google Ads after the preview.

    ``pinned`` holds the raw values read at draft; plans whose preview values
    are already raw pin them as ``previous``.
    """
    pinned = changes.get("pinned") or changes.get("previous") or {}
    now = _current_values(client, cid, changes["asset_id"], {k: fields[k] for k in pinned})
    moved = sorted(k for k, v in pinned.items() if now.get(k) != v)
    if moved:
        raise RuntimeError(
            f"Asset {changes['asset_id']} was changed in Google Ads after this preview "
            f"({', '.join(moved)}); nothing was sent. Draft the edit again so the preview "
            "shows the current values."
        )


def _apply_asset_update(client, cid: str, asset_id: str, set_fields) -> dict:
    from google.protobuf import field_mask_pb2

    asset_service = client.get_service("AssetService")
    operation = client.get_type("AssetOperation")
    asset = operation.update
    asset.resource_name = asset_service.asset_path(cid, asset_id)
    operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=set_fields(asset)))
    response = asset_service.mutate_assets(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_update_callout(client, cid: str, changes: dict) -> dict:
    _check_unchanged(client, cid, changes, _CALLOUT_FIELDS)

    def set_fields(asset):
        asset.callout_asset.callout_text = changes["callout_text"]
        return ["callout_asset.callout_text"]

    return _apply_asset_update(client, cid, changes["asset_id"], set_fields)


def _apply_update_sitelink(client, cid: str, changes: dict) -> dict:
    _check_unchanged(client, cid, changes, _SITELINK_FIELDS)

    def set_fields(asset):
        paths = []
        for key in ("link_text", "description1", "description2"):
            if changes.get(key):
                setattr(asset.sitelink_asset, key, changes[key])
                paths.append(f"sitelink_asset.{key}")
        if changes.get("final_url"):
            asset.final_urls.append(changes["final_url"])
            paths.append("final_urls")
        return paths

    return _apply_asset_update(client, cid, changes["asset_id"], set_fields)


def _apply_update_structured_snippet(client, cid: str, changes: dict) -> dict:
    _check_unchanged(client, cid, changes, _SNIPPET_FIELDS)

    def set_fields(asset):
        asset.structured_snippet_asset.header = changes["header"]
        asset.structured_snippet_asset.values.extend(changes["values"])
        return ["structured_snippet_asset.header", "structured_snippet_asset.values"]

    return _apply_asset_update(client, cid, changes["asset_id"], set_fields)


# ---------------------------------------------------------------------------
# Call assets and campaign ad schedules (after PR #96 by Illia Sapryga)
# ---------------------------------------------------------------------------

_DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
_MINUTE_TO_ENUM = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}
_ENUM_TO_MINUTE = {name: minute for minute, name in _MINUTE_TO_ENUM.items()}
# CriterionError.AD_SCHEDULE_EXCEEDED_INTERVALS_PER_DAY_LIMIT
_MAX_INTERVALS_PER_DAY = 6
_REPORTING_STATES = (
    "DISABLED",
    "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION",
    "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
)
_RESOURCE_LEVEL = "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"


def _phone_e164(phone: str, country_code: str) -> tuple[str, str | None]:
    """``(e164, error)``. Parsed by ``phonenumbers`` (Google's libphonenumber port).

    It reads what a hand-written map got wrong: the "(0)" trunk marker in
    "+44 (0)20 7946 0018", the "00" international prefix, Italy's kept 0.
    ``country_code`` is required and must dial the number's country code.
    """
    import phonenumbers

    cc = _clean(country_code).upper()
    if not cc:
        return "", (
            "country_code is required with phone_number (ISO 3166-1 alpha-2 such as "
            "'US', 'GB', 'DE'): it is stored on the call asset and decides how a "
            "national number is read"
        )
    expected = phonenumbers.country_code_for_region(cc) if len(cc) == 2 and cc.isalpha() else 0
    if not expected:
        return "", f"country_code '{country_code}' is not a 2-letter ISO country code"
    raw = _clean(phone)
    if not raw:
        return "", "phone_number is required"
    try:
        number = phonenumbers.parse(raw, cc)
    except phonenumbers.NumberParseException:
        return "", f"phone_number '{raw}' cannot be read as a phone number"
    if number.country_code != expected:
        return "", (
            f"phone_number '{raw}' has country code +{number.country_code}, but "
            f"country_code {cc} dials +{expected}"
        )
    if not phonenumbers.is_valid_number(number):
        return "", f"phone_number '{raw}' is not a valid {cc} number"
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164), None


def _whole_number(value: object, default: int | None = None) -> int:
    """An integer, or ValueError / TypeError: 9.7 is refused instead of becoming 9."""
    if value is None or value == "":
        if default is None:
            raise ValueError("missing")
        return default
    if isinstance(value, bool):
        raise TypeError("bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(str(value))


def _validate_schedule(schedule: list[dict]) -> tuple[list[dict], list[str]]:
    """``(validated, errors)`` for ad schedule windows.

    Each window: day_of_week, start_hour 0-23, end_hour 0-24 (24 only with
    minute 0), start_minute/end_minute 0, 15, 30 or 45; end after start; no
    overlaps and at most six windows per day.
    """
    errors: list[str] = []
    validated: list[dict] = []
    for i, entry in enumerate(schedule or []):
        where = f"schedule[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object with day_of_week, start_hour, end_hour")
            continue
        day = _clean(entry.get("day_of_week")).upper()
        if day not in _DAYS:
            errors.append(f"{where}: day_of_week must be one of {list(_DAYS)}")
            continue
        try:
            start_hour = _whole_number(entry.get("start_hour"))
            end_hour = _whole_number(entry.get("end_hour"))
            start_minute = _whole_number(entry.get("start_minute"), 0)
            end_minute = _whole_number(entry.get("end_minute"), 0)
        except (TypeError, ValueError):
            errors.append(
                f"{where}: start_hour and end_hour are required whole numbers, minutes "
                "0, 15, 30 or 45 (9:30 is start_hour 9, start_minute 30)"
            )
            continue
        bad = []
        if not 0 <= start_hour <= 23:
            bad.append(f"{where}: start_hour must be 0-23")
        if not 0 <= end_hour <= 24:
            bad.append(f"{where}: end_hour must be 0-24")
        if start_minute not in _MINUTE_TO_ENUM or end_minute not in _MINUTE_TO_ENUM:
            bad.append(f"{where}: minutes must be 0, 15, 30 or 45")
        if end_hour == 24 and end_minute:
            bad.append(f"{where}: end_hour 24 takes end_minute 0")
        if not bad and (end_hour, end_minute) <= (start_hour, start_minute):
            bad.append(
                f"{where}: end {end_hour}:{end_minute:02d} must be after start "
                f"{start_hour}:{start_minute:02d}"
            )
        if bad:
            errors.extend(bad)
            continue
        validated.append({"day_of_week": day, "start_hour": start_hour, "start_minute": start_minute,
                          "end_hour": end_hour, "end_minute": end_minute})
    if not errors:
        errors.extend(_schedule_conflicts(validated))
    return validated, errors


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _schedule_conflicts(new: list[dict], existing: list[dict] | None = None) -> list[str]:
    """Overlaps and the six-per-day limit across new (and existing) windows."""
    from itertools import pairwise

    by_day: dict[str, list[tuple[int, int, str]]] = {}
    for label, items in (("existing", existing or []), ("new", new)):
        for w in items:
            by_day.setdefault(w["day_of_week"], []).append(
                (w["start_hour"] * 60 + w["start_minute"], w["end_hour"] * 60 + w["end_minute"], label))
    errors: list[str] = []
    for day in _DAYS:
        windows = sorted(by_day.get(day, []))
        if len(windows) > _MAX_INTERVALS_PER_DAY:
            errors.append(f"{day}: {len(windows)} windows; Google allows at most {_MAX_INTERVALS_PER_DAY} a day")
        for (s1, e1, l1), (s2, e2, l2) in pairwise(windows):
            if s2 < e1:
                errors.append(f"{day}: {l1} window {_hhmm(s1)}-{_hhmm(e1)} overlaps {l2} window {_hhmm(s2)}-{_hhmm(e2)}")
    return errors


def _schedule_from_google(items: list) -> list[dict]:
    """Windows as read from GAQL (enum names) in the shape the drafts take."""
    out = []
    for item in items or []:
        item = item or {}
        out.append({
            "day_of_week": str(item.get("day_of_week") or ""),
            "start_hour": int(item.get("start_hour") or 0),
            "start_minute": _ENUM_TO_MINUTE.get(str(item.get("start_minute") or "ZERO"), 0),
            "end_hour": int(item.get("end_hour") or 0),
            "end_minute": _ENUM_TO_MINUTE.get(str(item.get("end_minute") or "ZERO"), 0),
        })
    return out


def _populate_schedule(client, info, window: dict) -> None:
    info.day_of_week = getattr(client.enums.DayOfWeekEnum, window["day_of_week"])
    info.start_hour = int(window["start_hour"])
    info.end_hour = int(window["end_hour"])
    info.start_minute = getattr(client.enums.MinuteOfHourEnum, _MINUTE_TO_ENUM[int(window["start_minute"])])
    info.end_minute = getattr(client.enums.MinuteOfHourEnum, _MINUTE_TO_ENUM[int(window["end_minute"])])


def _account_settings(config: AdLoopConfig, customer_id: str) -> dict:
    """The account's currency and time zone (both shown in previews)."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(config, customer_id, "SELECT customer.currency_code, customer.time_zone FROM customer LIMIT 1")
    row = rows[0] if rows else {}
    return {"currency_code": str(row.get("customer.currency_code") or ""),
            "time_zone": str(row.get("customer.time_zone") or "")}


def _time_zone_note(time_zone: str) -> str:
    return (
        f"Schedule hours are in the account time zone ({time_zone or 'not readable'}), "
        "not the searcher's local time."
    )


def draft_call_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    phone_number: str = "",
    country_code: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "",
    call_conversion_action_id: str = "",
    ad_schedule: list[dict] | None = None,
) -> dict:
    """Draft a call asset linked to an ad group, a campaign or the account."""
    from adloop.safety.preview import ChangePlan, store_plan

    if blocked := _blocked("create_call_asset", config):
        return blocked
    scope, campaign_id, ad_group_id, errors = resolve_scope(scope, campaign_id, ad_group_id)
    e164, phone_error = _phone_e164(phone_number, country_code)
    if phone_error:
        errors.append(phone_error)
    conversion_id = _clean(call_conversion_action_id)
    if conversion_id and not _is_numeric_id(conversion_id):
        errors.append(f"call_conversion_action_id must be numeric, got '{conversion_id}'")
    schedule, schedule_errors = _validate_schedule(ad_schedule or [])
    errors.extend(schedule_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = [(
        "Google reviews the phone number before the call asset serves; it can stay under "
        "review, or need verification in Google Ads (Assets, Calls)."
    )]
    if scope == "account":
        warnings.append(_account_scope_warning("call assets"))
    if schedule:
        warnings.append(_time_zone_note(_account_settings(config, customer_id)["time_zone"]))
    entity_type, entity_id = scope_entity(scope, customer_id, campaign_id, ad_group_id)
    plan = ChangePlan(
        operation="create_call_asset",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"scope": scope, "campaign_id": campaign_id, "ad_group_id": ad_group_id,
                 "phone_number": e164, "country_code": _clean(country_code).upper(),
                 "call_conversion_action_id": conversion_id, "ad_schedule": schedule},
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


_CALL_FIELDS = {
    "phone_number": "asset.call_asset.phone_number",
    "country_code": "asset.call_asset.country_code",
    "call_conversion_action": "asset.call_asset.call_conversion_action",
    "call_conversion_reporting_state": "asset.call_asset.call_conversion_reporting_state",
    "ad_schedule": "asset.call_asset.ad_schedule_targets",
}


def update_call_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    phone_number: str = "",
    country_code: str = "",
    call_conversion_action_id: str = "",
    call_conversion_reporting_state: str = "",
    ad_schedule: list[dict] | None = None,
    clear_ad_schedule: bool = False,
) -> dict:
    """Draft an in-place edit of a call asset; fields not passed stay as they are."""
    if blocked := _blocked("update_call_asset", config):
        return blocked
    asset_id = _clean(asset_id)
    errors: list[str] = [] if _is_numeric_id(asset_id) else [f"asset_id must be numeric, got '{asset_id}'"]
    e164 = ""
    if _clean(phone_number):
        e164, phone_error = _phone_e164(phone_number, country_code)
        if phone_error:
            errors.append(phone_error)
    elif _clean(country_code):
        errors.append("country_code only changes together with phone_number; pass both")
    conversion_id = _clean(call_conversion_action_id)
    if conversion_id and not _is_numeric_id(conversion_id):
        errors.append(f"call_conversion_action_id must be numeric, got '{conversion_id}'")
    state = _clean(call_conversion_reporting_state).upper()
    if state and state not in _REPORTING_STATES:
        errors.append(f"call_conversion_reporting_state must be one of {list(_REPORTING_STATES)}")
    if conversion_id:
        if state and state != _RESOURCE_LEVEL:
            errors.append(f"call_conversion_action_id only counts with call_conversion_reporting_state={_RESOURCE_LEVEL}")
        state = _RESOURCE_LEVEL
    schedule, schedule_errors = _validate_schedule(ad_schedule or [])
    errors.extend(schedule_errors)
    if clear_ad_schedule and schedule:
        errors.append("Pass ad_schedule or clear_ad_schedule, not both")
    if not errors and not (e164 or conversion_id or state or schedule or clear_ad_schedule):
        errors.append("No changes specified: pass phone_number (with country_code), "
                      "call_conversion_action_id, call_conversion_reporting_state, "
                      "ad_schedule or clear_ad_schedule")
    if errors:
        return {"error": "Validation failed", "details": errors}

    current, error = _asset_for_edit(config, customer_id, asset_id, _CALL_FIELDS, "CALL")
    if error:
        return error
    if state == _RESOURCE_LEVEL and not conversion_id and not current["call_conversion_action"]:
        return {"error": "Validation failed", "details": [
            f"{_RESOURCE_LEVEL} needs a conversion action and the asset has none; pass call_conversion_action_id"]}

    changed: dict = {}
    if e164 and (e164, _clean(country_code).upper()) != (current["phone_number"], current["country_code"]):
        changed.update(phone_number=e164, country_code=_clean(country_code).upper())
    if conversion_id and not str(current["call_conversion_action"]).endswith(f"/{conversion_id}"):
        changed["call_conversion_action_id"] = conversion_id
    if state and state != current["call_conversion_reporting_state"]:
        changed["call_conversion_reporting_state"] = state
    current_schedule = _schedule_from_google(current["ad_schedule"])
    if (schedule or clear_ad_schedule) and schedule != current_schedule:
        changed["ad_schedule"] = schedule
    if not changed:
        return {"error": f"Call asset {asset_id} already has these values; nothing to change"}

    pinned_keys = {"phone_number": ("phone_number", "country_code"), "country_code": (),
                   "call_conversion_action_id": ("call_conversion_action",),
                   "call_conversion_reporting_state": ("call_conversion_reporting_state",),
                   "ad_schedule": ("ad_schedule",)}
    pinned = {k: current[k] for key in changed for k in pinned_keys[key]}
    previous = {"phone_number": current["phone_number"], "country_code": current["country_code"],
                "call_conversion_action": current["call_conversion_action"],
                "call_conversion_reporting_state": current["call_conversion_reporting_state"],
                "ad_schedule": current_schedule}
    preview = _edit_plan("update_call_asset", customer_id, asset_id, changed, previous, pinned=pinned)
    if "phone_number" in changed:
        preview["warnings"].append("A new number goes through Google's review again before the asset serves.")
    if schedule:
        preview["warnings"].append(_time_zone_note(_account_settings(config, customer_id)["time_zone"]))
    if "ad_schedule" in changed and not schedule:
        preview["warnings"].append("Without a schedule the number shows at all hours.")
    return preview


def _campaign_schedule(config: AdLoopConfig, customer_id: str, campaign_id: str) -> tuple[dict | None, list[dict]]:
    """The campaign (name, status, account time zone) and its current windows."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(config, customer_id, (
        "SELECT campaign.id, campaign.name, campaign.status, customer.time_zone "
        f"FROM campaign WHERE campaign.id = {campaign_id} LIMIT 1"))
    if not rows:
        return None, []
    campaign = {"name": rows[0].get("campaign.name") or "", "status": rows[0].get("campaign.status") or "",
                "time_zone": rows[0].get("customer.time_zone") or ""}
    windows = execute_query(config, customer_id, (
        "SELECT campaign_criterion.criterion_id, campaign_criterion.ad_schedule.day_of_week, "
        "campaign_criterion.ad_schedule.start_hour, campaign_criterion.ad_schedule.start_minute, "
        "campaign_criterion.ad_schedule.end_hour, campaign_criterion.ad_schedule.end_minute "
        f"FROM campaign_criterion WHERE campaign.id = {campaign_id} "
        "AND campaign_criterion.type = 'AD_SCHEDULE' AND campaign_criterion.status != 'REMOVED'"))
    existing = []
    for row in windows:
        window = _schedule_from_google([{
            key: row.get(f"campaign_criterion.ad_schedule.{key}")
            for key in ("day_of_week", "start_hour", "start_minute", "end_hour", "end_minute")
        }])[0]
        window["remove_id"] = f"{campaign_id}~{row.get('campaign_criterion.criterion_id')}"
        existing.append(window)
    return campaign, existing


def add_ad_schedule(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    schedule: list[dict] | None = None,
) -> dict:
    """Draft ad schedule windows for a campaign (added; existing windows stay)."""
    from adloop.safety.preview import ChangePlan, store_plan

    if blocked := _blocked("add_ad_schedule", config):
        return blocked
    campaign_id = _clean(campaign_id)
    errors: list[str] = [] if _is_numeric_id(campaign_id) else [f"campaign_id must be numeric, got '{campaign_id}'"]
    windows, schedule_errors = _validate_schedule(schedule or [])
    errors.extend(schedule_errors)
    if not schedule_errors and not windows:
        errors.append("schedule needs at least one window")
    if errors:
        return {"error": "Validation failed", "details": errors}

    campaign, existing = _campaign_schedule(config, customer_id, campaign_id)
    if campaign is None:
        return {"error": f"Campaign {campaign_id} was not found in this account"}
    if campaign["status"] == "REMOVED":
        return {"error": f"Campaign {campaign_id} is removed"}
    conflicts = _schedule_conflicts(windows, existing)
    if conflicts:
        return {"error": "Validation failed", "details": conflicts, "existing_schedule": existing}

    warnings = [_time_zone_note(campaign["time_zone"])]
    first = not existing
    if first:
        warnings.append(
            f"Campaign '{campaign['name']}' has no ad schedule, so it serves at all hours. "
            "With these windows it serves only inside them: every other hour, and every "
            "day without a window, stops serving."
        )
    if campaign["status"] != "ENABLED":
        warnings.append(f"The campaign is {campaign['status']}; the schedule applies once it is enabled.")
    plan = ChangePlan(
        operation="add_ad_schedule",
        entity_type="campaign_criterion",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={"campaign_id": campaign_id, "campaign_name": campaign["name"],
                 "account_time_zone": campaign["time_zone"], "schedule": windows,
                 "existing_schedule": existing},
        # A campaign's first windows switch off every hour outside them.
        requires_double_confirm=first,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def _apply_create_call_asset(client, cid: str, changes: dict) -> dict:
    def populate(asset, payload):
        call = asset.call_asset
        call.country_code = payload["country_code"]
        call.phone_number = payload["phone_number"]
        if payload.get("call_conversion_action_id"):
            call.call_conversion_action = client.get_service("ConversionActionService").conversion_action_path(
                cid, payload["call_conversion_action_id"])
            call.call_conversion_reporting_state = (
                client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION)
        for window in payload.get("ad_schedule") or []:
            info = client.get_type("AdScheduleInfo")
            _populate_schedule(client, info, window)
            call.ad_schedule_targets.append(info)

    return _create_and_link(client, cid, [changes], client.enums.AssetFieldTypeEnum.CALL, populate, changes)


def _apply_update_call_asset(client, cid: str, changes: dict) -> dict:
    _check_unchanged(client, cid, changes, _CALL_FIELDS)

    def set_fields(asset):
        call, paths = asset.call_asset, []
        if "phone_number" in changes:
            call.phone_number = changes["phone_number"]
            call.country_code = changes["country_code"]
            paths += ["call_asset.phone_number", "call_asset.country_code"]
        if changes.get("call_conversion_action_id"):
            call.call_conversion_action = client.get_service("ConversionActionService").conversion_action_path(
                cid, changes["call_conversion_action_id"])
            paths.append("call_asset.call_conversion_action")
        if changes.get("call_conversion_reporting_state"):
            call.call_conversion_reporting_state = getattr(
                client.enums.CallConversionReportingStateEnum, changes["call_conversion_reporting_state"])
            paths.append("call_asset.call_conversion_reporting_state")
        if "ad_schedule" in changes:  # replaces the whole list; empty clears it
            for window in changes["ad_schedule"]:
                info = client.get_type("AdScheduleInfo")
                _populate_schedule(client, info, window)
                call.ad_schedule_targets.append(info)
            paths.append("call_asset.ad_schedule_targets")
        return paths

    return _apply_asset_update(client, cid, changes["asset_id"], set_fields)


def _apply_add_ad_schedule(client, cid: str, changes: dict) -> dict:
    campaign_id = changes["campaign_id"]
    if changes.get("existing_schedule"):
        # Drafted as an addition to existing windows. If those are gone now,
        # these windows would switch off every other hour without the double
        # confirmation a first schedule gets.
        query = ("SELECT campaign_criterion.criterion_id FROM campaign_criterion "
                 f"WHERE campaign.id = {campaign_id} AND campaign_criterion.type = 'AD_SCHEDULE' "
                 "AND campaign_criterion.status != 'REMOVED'")
        if not list(client.get_service("GoogleAdsService").search(customer_id=cid, query=query)):
            raise RuntimeError(
                f"Campaign {campaign_id} no longer has the ad schedule this preview added to, so "
                "these windows would now switch off every other hour. Nothing was sent; draft again.")
    campaign = client.get_service("CampaignService").campaign_path(cid, campaign_id)
    operations = []
    for window in changes["schedule"]:
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = campaign
        _populate_schedule(client, op.create.ad_schedule, window)
        operations.append(op)
    response = client.get_service("CampaignCriterionService").mutate_campaign_criteria(
        customer_id=cid, operations=operations)
    return {"campaign_criteria": [r.resource_name for r in response.results]}
