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


def _validate_snippet_items(snippets: list[dict]) -> tuple[list[dict], list[str]]:
    from adloop.ads.write import _STRUCTURED_SNIPPET_HEADERS

    errors: list[str] = []
    validated: list[dict] = []
    if not snippets:
        errors.append("At least one structured snippet is required")
    for index, snippet in enumerate(snippets):
        header = _clean(snippet.get("header", ""))
        values = [_clean(value) for value in snippet.get("values", []) or []]
        if header not in _STRUCTURED_SNIPPET_HEADERS:
            errors.append(
                f"Structured snippet {index + 1}: header must be one of "
                f"{sorted(_STRUCTURED_SNIPPET_HEADERS)}"
            )
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
    preview["warnings"] = [
        "Account-level assets serve on every eligible campaign that has no asset of "
        "the same type linked at campaign or ad-group level."
    ]
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
    return {k: (v if v is not None else "") for k, v in out.items()}


def _asset_for_edit(config, customer_id, asset_id, fields, expected_type) -> tuple[dict | None, dict | None]:
    """``(current, error)``: the asset's pinned values, or why it cannot be edited."""
    current = _read_asset(config, customer_id, asset_id, fields)
    if current is None:
        return None, {"error": f"Asset {asset_id} was not found in this account"}
    if current["type"] != expected_type:
        return None, {"error": f"Asset {asset_id} is a {current['type']} asset, not a {expected_type}"}
    return current, None


def _edit_plan(operation: str, customer_id: str, asset_id: str, changed: dict, previous: dict) -> dict:
    from adloop.safety.preview import ChangePlan, store_plan

    plan = ChangePlan(
        operation=operation,
        entity_type="asset",
        entity_id=asset_id,
        customer_id=customer_id,
        changes={"asset_id": asset_id, **changed, "previous": previous},
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
        return {"error": "Validation failed", "details": [
            "description1 and description2 must both be set (Google rejects a sitelink "
            "with only one description line)"
        ]}
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
    # An empty header keeps the current one; values are always the full list.
    snippet = {"header": _clean(header) or (current or {}).get("header", ""), "values": values or []}
    validated, snippet_errors = _validate_snippet_items([snippet])
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
    """The asset's pinned fields as Google has them now (a read, so it also runs in a dry run)."""
    service = client.get_service("GoogleAdsService")
    query = f"SELECT asset.id, {', '.join(fields.values())} FROM asset WHERE asset.id = {asset_id}"
    for row in service.search(customer_id=cid, query=query):
        values = {}
        for key, field in fields.items():
            value = row
            for part in field.split("."):
                value = getattr(value, part)
            values[key] = value
        return _normalize(values)
    raise RuntimeError(f"Asset {asset_id} no longer exists; nothing was sent.")


def _check_unchanged(client, cid: str, changes: dict, fields: dict) -> None:
    """Refuse when the asset changed in Google Ads after the preview."""
    pinned = changes.get("previous") or {}
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
