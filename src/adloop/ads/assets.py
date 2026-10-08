"""Google Ads text assets: where they link, and edits to existing ones.

Callouts and structured snippets link at one of three levels: an ad group
(``AdGroupAsset``), a campaign (``CampaignAsset``) or the whole account
(``CustomerAsset``). The level is the ``scope`` argument. It defaults to the
campaign, and account-wide linking happens only when ``scope="account"`` is
passed: an empty ``campaign_id`` is a validation error, never a silent
account-wide link.

Existing assets are edited in two ways:

- ``update_callout`` / ``update_sitelink`` change the asset in place
  (``AssetService`` update). The asset keeps its ID and its performance
  history, and the edit shows wherever the asset is linked.
- ``update_structured_snippet`` swaps one link: it creates a new snippet
  asset, links it at the same scope and removes the old link, all in one
  ``GoogleAdsService.Mutate`` request, so either everything happens or
  nothing does. Other places the old asset is linked are left alone.

``draft_business_name_asset`` creates a BUSINESS_NAME text asset, and
``link_asset_to_customer`` links assets that already exist at account level.

Every id that ends up in GAQL is checked to be numeric first.
"""

from __future__ import annotations

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

_SCOPES = ("campaign", "ad_group", "account")

# scope -> the link resource (GAQL name) and the plan's entity_type.
_LINK_RESOURCE = {
    "ad_group": "ad_group_asset",
    "campaign": "campaign_asset",
    "account": "customer_asset",
}

# Field types CustomerAsset accepts, mapped to the asset type each one
# links. Source: "Asset types linked to customers, campaigns, and ad
# groups" in the Google Ads API assets overview
# (https://developers.google.com/google-ads/api/docs/assets/overview).
# BUSINESS_MESSAGE is left out on purpose: only one per message provider
# may be active, which this tool does not check.
_CUSTOMER_ASSET_FIELD_TYPES = {
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

_SWAP_WARNING = (
    "This is a swap, not an in-place edit: a new structured snippet asset is "
    "created and linked at the same scope, and the old link is removed in the "
    "same request. The old asset keeps its performance history and stays "
    "linked anywhere else it is used; the new asset starts without history."
)

_IN_PLACE_WARNING = (
    "In-place edit: the asset keeps its ID and performance history, and the "
    "new text shows everywhere this asset is linked (other campaigns, ad "
    "groups or the account)."
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _is_numeric_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit()


def _resolve_scope(
    scope: str,
    campaign_id: str,
    ad_group_id: str,
    *,
    allowed: tuple[str, ...] = _SCOPES,
) -> tuple[str, list[str]]:
    """Normalise ``scope`` and check it against the ids that came with it.

    Returns ``(scope, errors)``. Each scope takes exactly the id it links
    to; account scope takes none and must be asked for by name.
    """
    scope = (scope or "campaign").strip().lower()
    campaign_id = (campaign_id or "").strip()
    ad_group_id = (ad_group_id or "").strip()
    errors: list[str] = []

    if scope not in allowed:
        return scope, [f"scope must be one of {list(allowed)}, got '{scope}'"]

    if scope == "campaign":
        if not campaign_id:
            hint = (
                "for an ad-group link pass scope='ad_group'"
                if ad_group_id
                else "account-wide linking needs scope='account'"
            )
            errors.append(f"campaign_id is required for scope='campaign' ({hint})")
        elif not _is_numeric_id(campaign_id):
            errors.append(f"campaign_id must be numeric, got '{campaign_id}'")
        if ad_group_id:
            errors.append("ad_group_id is only used with scope='ad_group'")
    elif scope == "ad_group":
        if not ad_group_id:
            errors.append("ad_group_id is required for scope='ad_group'")
        elif not _is_numeric_id(ad_group_id):
            errors.append(f"ad_group_id must be numeric, got '{ad_group_id}'")
        if campaign_id:
            errors.append("campaign_id is not used with scope='ad_group'")
    elif campaign_id or ad_group_id:
        errors.append(
            "scope='account' links account-wide; leave campaign_id and "
            "ad_group_id empty"
        )
    return scope, errors


def _scope_entity(scope: str, customer_id: str, campaign_id: str, ad_group_id: str) -> tuple[str, str]:
    """The ChangePlan ``(entity_type, entity_id)`` for a scope."""
    entity_id = {"ad_group": ad_group_id, "campaign": campaign_id}.get(scope, customer_id)
    return _LINK_RESOURCE[scope], entity_id


def _validate_callouts(callouts: list[str]) -> tuple[list[str], list[str]]:
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


def _account_scope_warning(kind: str) -> str:
    return (
        f"Account-level {kind} serve on every eligible campaign in the account "
        f"that has no {kind} of its own at campaign or ad-group level."
    )


# ---------------------------------------------------------------------------
# GAQL lookups (ids are validated before they are interpolated)
# ---------------------------------------------------------------------------


def _asset_link_query(
    asset_id: str,
    field_type: str,
    scope: str,
    campaign_id: str = "",
    ad_group_id: str = "",
    extra_fields: tuple[str, ...] = (),
) -> str:
    """GAQL for the live link of ``asset_id`` at ``scope``.

    Raises ValueError before any id reaches the query string unless every
    id is numeric and the field type is a plain enum name.
    """
    if not _is_numeric_id(asset_id):
        raise ValueError(f"asset_id must be numeric, got '{asset_id}'")
    if not (field_type.isupper() and field_type.replace("_", "").isalpha()):
        raise ValueError(f"field_type must be an AssetFieldType name, got '{field_type}'")
    if scope not in _LINK_RESOURCE:
        raise ValueError(f"Unknown asset scope: {scope}")

    link = _LINK_RESOURCE[scope]
    conditions = [
        f"asset.id = {asset_id}",
        f"{link}.field_type = '{field_type}'",
        f"{link}.status != 'REMOVED'",
    ]
    if scope == "ad_group":
        if not _is_numeric_id(ad_group_id):
            raise ValueError(f"ad_group_id must be numeric, got '{ad_group_id}'")
        conditions.append(f"ad_group.id = {ad_group_id}")
    elif scope == "campaign":
        if not _is_numeric_id(campaign_id):
            raise ValueError(f"campaign_id must be numeric, got '{campaign_id}'")
        conditions.append(f"campaign.id = {campaign_id}")

    fields = ", ".join((f"{link}.resource_name", *extra_fields))
    return f"SELECT {fields} FROM {link} WHERE " + " AND ".join(conditions)


def _find_asset_link(
    client: object,
    cid: str,
    asset_id: str,
    field_type: str,
    scope: str,
    campaign_id: str = "",
    ad_group_id: str = "",
) -> str:
    """Resource name of the live link of ``asset_id`` at ``scope``, or ""."""
    query = _asset_link_query(asset_id, field_type, scope, campaign_id, ad_group_id)
    link = _LINK_RESOURCE[scope]
    googleads_service = client.get_service("GoogleAdsService")
    for row in googleads_service.search(customer_id=cid, query=query):
        return getattr(row, link).resource_name
    return ""


def _read_asset(
    config: AdLoopConfig, customer_id: str, asset_id: str, fields: tuple[str, ...]
) -> dict | None:
    """One asset row (``asset.type`` plus ``fields``), or None if it doesn't exist."""
    from adloop.ads.gaql import execute_query

    if not _is_numeric_id(asset_id):
        raise ValueError(f"asset_id must be numeric, got '{asset_id}'")
    selected = ", ".join(("asset.id", "asset.type", *fields))
    rows = execute_query(
        config, customer_id, f"SELECT {selected} FROM asset WHERE asset.id = {asset_id}"
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Drafts: new callouts / snippets / business name, scoped
# ---------------------------------------------------------------------------


def draft_callouts(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_id: str = "",
    scope: str = "campaign",
    callouts: list[str] | None = None,
) -> dict:
    """Draft callout assets linked at ad group, campaign or account scope."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_callouts", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    scope, errors = _resolve_scope(scope, campaign_id, ad_group_id)
    validated_callouts, callout_errors = _validate_callouts(callouts or [])
    errors.extend(callout_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    entity_type, entity_id = _scope_entity(scope, customer_id, campaign_id, ad_group_id)
    plan = ChangePlan(
        operation="create_callouts",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "callouts": validated_callouts,
        },
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
    scope: str = "campaign",
    snippets: list[dict] | None = None,
) -> dict:
    """Draft structured snippet assets linked at ad group, campaign or account scope."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_structured_snippets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    scope, errors = _resolve_scope(scope, campaign_id, ad_group_id)
    validated_snippets, snippet_errors = _validate_structured_snippets(snippets or [])
    errors.extend(snippet_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    entity_type, entity_id = _scope_entity(scope, customer_id, campaign_id, ad_group_id)
    plan = ChangePlan(
        operation="create_structured_snippets",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "snippets": validated_snippets,
        },
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
    scope: str = "campaign",
    campaign_id: str = "",
) -> dict:
    """Draft a BUSINESS_NAME text asset linked to a campaign or the account."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_business_name_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    # Google links BUSINESS_NAME at campaign or account level, not ad group.
    scope, errors = _resolve_scope(scope, campaign_id, "", allowed=("campaign", "account"))
    text = (business_name or "").strip()
    if not text:
        errors.append("business_name is required")
    elif len(text) > 25:
        errors.append(f"business_name '{text}' is {len(text)} chars (max 25)")
    if errors:
        return {"error": "Validation failed", "details": errors}

    entity_type, entity_id = _scope_entity(scope, customer_id, campaign_id, "")
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
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(links):
        if not isinstance(item, dict):
            errors.append(f"Link {i + 1}: must be an object with asset_id and field_type")
            continue
        asset_id = str(item.get("asset_id", "")).strip()
        field_type = str(item.get("field_type", "")).strip().upper()
        if not _is_numeric_id(asset_id):
            errors.append(f"Link {i + 1}: asset_id must be numeric, got '{asset_id}'")
            continue
        if field_type not in _CUSTOMER_ASSET_FIELD_TYPES:
            errors.append(
                f"Link {i + 1}: field_type '{field_type}' cannot be linked at "
                f"account level; allowed: {sorted(_CUSTOMER_ASSET_FIELD_TYPES)}"
            )
            continue
        if (asset_id, field_type) in seen:
            continue
        seen.add((asset_id, field_type))
        validated.append({"asset_id": asset_id, "field_type": field_type})
    if errors:
        return {"error": "Validation failed", "details": errors}

    ids = ", ".join(sorted({link["asset_id"] for link in validated}))
    rows = execute_query(
        config,
        customer_id,
        f"SELECT asset.id, asset.type, asset.name FROM asset WHERE asset.id IN ({ids})",
    )
    found = {str(row.get("asset.id")): row for row in rows}
    for link in validated:
        row = found.get(link["asset_id"])
        expected = _CUSTOMER_ASSET_FIELD_TYPES[link["field_type"]]
        if row is None:
            errors.append(f"Asset {link['asset_id']} was not found in this account")
        elif row.get("asset.type") != expected:
            errors.append(
                f"Asset {link['asset_id']} is a {row.get('asset.type')} asset; "
                f"{link['field_type']} needs a {expected} asset"
            )
        else:
            link["asset_type"] = expected
            if row.get("asset.name"):
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
        (
            "Account-level assets serve on every eligible campaign that has no "
            "asset of the same type linked at campaign or ad-group level."
        )
    ]
    return preview


# ---------------------------------------------------------------------------
# Drafts: edits of existing assets
# ---------------------------------------------------------------------------


def update_callout(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    callout_text: str = "",
) -> dict:
    """Draft an in-place edit of an existing callout asset's text."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_callout", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    asset_id = (asset_id or "").strip()
    errors: list[str] = []
    if not _is_numeric_id(asset_id):
        errors.append(f"asset_id must be numeric, got '{asset_id}'")
    validated, text_errors = _validate_callouts([callout_text or ""])
    errors.extend(text_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}
    text = validated[0]

    current = _read_asset(config, customer_id, asset_id, ("asset.callout_asset.callout_text",))
    if current is None:
        return {"error": f"Asset {asset_id} was not found in this account"}
    if current.get("asset.type") != "CALLOUT":
        return {"error": f"Asset {asset_id} is a {current.get('asset.type')} asset, not a CALLOUT"}
    old_text = current.get("asset.callout_asset.callout_text") or ""
    if old_text == text:
        return {"error": f"Callout {asset_id} already reads '{text}'; nothing to change"}

    plan = ChangePlan(
        operation="update_callout",
        entity_type="asset",
        entity_id=asset_id,
        customer_id=customer_id,
        changes={
            "asset_id": asset_id,
            "callout_text": text,
            "previous": {"callout_text": old_text},
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [_IN_PLACE_WARNING]
    return preview


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
    """Draft an in-place edit of an existing sitelink asset. Empty fields stay as they are."""
    from adloop.ads.write import _validate_urls
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_sitelink", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    asset_id = (asset_id or "").strip()
    new = {
        "link_text": (link_text or "").strip(),
        "final_url": (final_url or "").strip(),
        "description1": (description1 or "").strip(),
        "description2": (description2 or "").strip(),
    }
    errors: list[str] = []
    if not _is_numeric_id(asset_id):
        errors.append(f"asset_id must be numeric, got '{asset_id}'")
    if len(new["link_text"]) > 25:
        errors.append(f"link_text '{new['link_text']}' is {len(new['link_text'])} chars (max 25)")
    for key in ("description1", "description2"):
        if len(new[key]) > 35:
            errors.append(f"{key} is {len(new[key])} chars (max 35)")
    if not any(new.values()):
        errors.append(
            "No changes specified — provide link_text, final_url, description1 "
            "and/or description2"
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    current = _read_asset(
        config,
        customer_id,
        asset_id,
        (
            "asset.sitelink_asset.link_text",
            "asset.sitelink_asset.description1",
            "asset.sitelink_asset.description2",
            "asset.final_urls",
        ),
    )
    if current is None:
        return {"error": f"Asset {asset_id} was not found in this account"}
    if current.get("asset.type") != "SITELINK":
        return {"error": f"Asset {asset_id} is a {current.get('asset.type')} asset, not a SITELINK"}
    previous = {
        "link_text": current.get("asset.sitelink_asset.link_text") or "",
        "final_url": (current.get("asset.final_urls") or [""])[0],
        "description1": current.get("asset.sitelink_asset.description1") or "",
        "description2": current.get("asset.sitelink_asset.description2") or "",
    }

    changed = {k: v for k, v in new.items() if v and v != previous[k]}
    if not changed:
        return {"error": f"Sitelink {asset_id} already has these values; nothing to change"}

    # Google requires the two description lines together or not at all.
    after = {**previous, **changed}
    if bool(after["description1"]) != bool(after["description2"]):
        return {
            "error": "Validation failed",
            "details": [
                (
                    "description1 and description2 must both be set (Google "
                    "rejects a sitelink with only one description line)"
                )
            ],
        }

    if "final_url" in changed:
        url_checks, _ = _validate_urls([changed["final_url"]])
        url_error = url_checks.get(changed["final_url"])
        if url_error:
            return {
                "error": "URL validation failed — sitelinks MUST point to working URLs",
                "details": [f"'{changed['final_url']}' is not reachable: {url_error}"],
            }

    plan = ChangePlan(
        operation="update_sitelink",
        entity_type="asset",
        entity_id=asset_id,
        customer_id=customer_id,
        changes={
            "asset_id": asset_id,
            **changed,
            "previous": {k: previous[k] for k in changed},
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [_IN_PLACE_WARNING]
    return preview


def update_structured_snippet(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    asset_id: str = "",
    header: str = "",
    values: list[str] | None = None,
    scope: str = "campaign",
    campaign_id: str = "",
    ad_group_id: str = "",
) -> dict:
    """Draft swapping one structured snippet link for a new snippet asset."""
    from adloop.ads.gaql import execute_query
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_structured_snippet", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    asset_id = (asset_id or "").strip()
    scope, errors = _resolve_scope(scope, campaign_id, ad_group_id)
    if not _is_numeric_id(asset_id):
        errors.append(f"asset_id must be numeric, got '{asset_id}'")
    validated, snippet_errors = _validate_structured_snippets(
        [{"header": header or "", "values": values or []}]
    )
    errors.extend(snippet_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}
    snippet = validated[0]

    link = _LINK_RESOURCE[scope]
    rows = execute_query(
        config,
        customer_id,
        _asset_link_query(
            asset_id,
            "STRUCTURED_SNIPPET",
            scope,
            campaign_id,
            ad_group_id,
            extra_fields=(
                "asset.structured_snippet_asset.header",
                "asset.structured_snippet_asset.values",
            ),
        ),
    )
    if not rows:
        where = {"ad_group": f"ad group {ad_group_id}", "campaign": f"campaign {campaign_id}"}
        return {
            "error": (
                f"Asset {asset_id} is not linked as a structured snippet at "
                f"{where.get(scope, 'account level')}; there is no link to replace"
            )
        }
    old_link = rows[0].get(f"{link}.resource_name") or ""
    previous = {
        "header": rows[0].get("asset.structured_snippet_asset.header") or "",
        "values": list(rows[0].get("asset.structured_snippet_asset.values") or []),
    }
    if previous == snippet:
        return {"error": f"Structured snippet {asset_id} already has this header and these values"}

    entity_type, entity_id = _scope_entity(scope, customer_id, campaign_id, ad_group_id)
    plan = ChangePlan(
        operation="update_structured_snippet",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={
            "scope": scope,
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "old_asset_id": asset_id,
            "old_link": old_link,
            "previous": previous,
            "snippet": snippet,
        },
        # The swap removes an existing link.
        requires_double_confirm=True,
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [_SWAP_WARNING]
    return preview


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _link_operation(
    client: object,
    cid: str,
    asset_resource: str,
    field_type: object,
    scope: str,
    campaign_id: str,
    ad_group_id: str,
) -> object:
    """A MutateOperation linking ``asset_resource`` at ``scope``."""
    googleads_service = client.get_service("GoogleAdsService")
    op = client.get_type("MutateOperation")
    if scope == "ad_group":
        if not ad_group_id:
            raise ValueError("ad_group_id is required for ad_group-scope assets")
        link = op.ad_group_asset_operation.create
        link.ad_group = googleads_service.ad_group_path(cid, ad_group_id)
    elif scope == "campaign":
        if not campaign_id:
            raise ValueError("campaign_id is required for campaign-scope assets")
        link = op.campaign_asset_operation.create
        link.campaign = googleads_service.campaign_path(cid, campaign_id)
    elif scope == "account":
        link = op.customer_asset_operation.create
    else:
        raise ValueError(f"Unknown asset scope: {scope}")
    link.asset = asset_resource
    link.field_type = field_type
    return op


def _create_and_link_assets(
    client: object,
    cid: str,
    assets: list[dict],
    field_type: object,
    populate_asset: object,
    *,
    scope: str = "campaign",
    campaign_id: str = "",
    ad_group_id: str = "",
) -> dict:
    """Create assets and link them at ``scope`` in one GoogleAdsService.Mutate."""
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
        operations.append(
            _link_operation(
                client,
                cid,
                asset_service.asset_path(cid, str(-(i + 1))),
                field_type,
                scope,
                campaign_id,
                ad_group_id,
            )
        )

    response = googleads_service.mutate(customer_id=cid, mutate_operations=operations)

    link = _LINK_RESOURCE[scope]
    link_key = f"{link}s"  # "ad_group_assets" / "campaign_assets" / "customer_assets"
    results: dict[str, list[str]] = {"assets": [], link_key: []}
    num_assets = len(assets)
    for i, resp in enumerate(response.mutate_operation_responses):
        if i < num_assets:
            resource = resp.asset_result.resource_name
            key = "assets"
        else:
            resource = getattr(resp, f"{link}_result").resource_name
            key = link_key
        if resource:
            results[key].append(resource)
    return results


def _apply_create_callouts(client: object, cid: str, changes: dict) -> dict:
    """Create callout assets and link them at the plan's scope."""

    def populate(asset: object, payload: dict) -> None:
        asset.callout_asset.callout_text = payload["callout_text"]

    return _create_and_link_assets(
        client,
        cid,
        [{"callout_text": text} for text in changes["callouts"]],
        client.enums.AssetFieldTypeEnum.CALLOUT,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
        ad_group_id=changes.get("ad_group_id", ""),
    )


def _apply_create_structured_snippets(client: object, cid: str, changes: dict) -> dict:
    """Create structured snippet assets and link them at the plan's scope."""

    def populate(asset: object, payload: dict) -> None:
        asset.structured_snippet_asset.header = payload["header"]
        asset.structured_snippet_asset.values.extend(payload["values"])

    return _create_and_link_assets(
        client,
        cid,
        changes["snippets"],
        client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
        ad_group_id=changes.get("ad_group_id", ""),
    )


def _apply_create_business_name_asset(client: object, cid: str, changes: dict) -> dict:
    """Create a TEXT asset and link it as BUSINESS_NAME at the plan's scope."""

    def populate(asset: object, payload: dict) -> None:
        asset.type_ = client.enums.AssetTypeEnum.TEXT
        asset.text_asset.text = payload["business_name"]

    return _create_and_link_assets(
        client,
        cid,
        [{"business_name": changes["business_name"]}],
        client.enums.AssetFieldTypeEnum.BUSINESS_NAME,
        populate,
        scope=changes.get("scope", "campaign"),
        campaign_id=changes.get("campaign_id", ""),
    )


def _apply_link_asset_to_customer(client: object, cid: str, changes: dict) -> dict:
    """Link existing assets at account level; no asset is created."""
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []
    for link in changes["links"]:
        op = client.get_type("MutateOperation")
        customer_asset = op.customer_asset_operation.create
        customer_asset.asset = asset_service.asset_path(cid, link["asset_id"])
        customer_asset.field_type = getattr(
            client.enums.AssetFieldTypeEnum, link["field_type"]
        )
        operations.append(op)

    response = googleads_service.mutate(customer_id=cid, mutate_operations=operations)
    return {
        "customer_assets": [
            resp.customer_asset_result.resource_name
            for resp in response.mutate_operation_responses
        ],
    }


def _apply_asset_update(client: object, cid: str, asset_id: str, set_fields: object) -> dict:
    """Update one asset in place; ``set_fields`` fills it and returns the mask paths."""
    from google.protobuf import field_mask_pb2

    asset_service = client.get_service("AssetService")
    operation = client.get_type("AssetOperation")
    asset = operation.update
    asset.resource_name = asset_service.asset_path(cid, asset_id)
    paths = set_fields(asset)
    operation.update_mask = field_mask_pb2.FieldMask(paths=paths)
    response = asset_service.mutate_assets(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_update_callout(client: object, cid: str, changes: dict) -> dict:
    """Change a callout asset's text in place."""

    def set_fields(asset: object) -> list[str]:
        asset.callout_asset.callout_text = changes["callout_text"]
        return ["callout_asset.callout_text"]

    return _apply_asset_update(client, cid, changes["asset_id"], set_fields)


def _apply_update_sitelink(client: object, cid: str, changes: dict) -> dict:
    """Change a sitelink asset's text, descriptions and/or URL in place."""

    def set_fields(asset: object) -> list[str]:
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


def _apply_update_structured_snippet(client: object, cid: str, changes: dict) -> dict:
    """Swap a structured snippet link in one atomic GoogleAdsService.Mutate.

    The old link is looked up first; if it is gone, nothing is sent. The
    request then creates the new asset, links it at the same scope and
    removes the old link, so Google applies all three or none.
    """
    scope = changes.get("scope", "campaign")
    campaign_id = changes.get("campaign_id", "")
    ad_group_id = changes.get("ad_group_id", "")
    old_asset_id = str(changes["old_asset_id"])
    snippet = changes["snippet"]

    old_link = _find_asset_link(
        client, cid, old_asset_id, "STRUCTURED_SNIPPET", scope, campaign_id, ad_group_id
    )
    if not old_link:
        raise ValueError(
            f"Asset {old_asset_id} is no longer linked as a structured snippet at "
            f"this scope; nothing was sent. Draft the update again."
        )

    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    new_asset_resource = asset_service.asset_path(cid, "-1")

    create_op = client.get_type("MutateOperation")
    new_asset = create_op.asset_operation.create
    new_asset.resource_name = new_asset_resource
    new_asset.structured_snippet_asset.header = snippet["header"]
    new_asset.structured_snippet_asset.values.extend(snippet["values"])

    link_op = _link_operation(
        client,
        cid,
        new_asset_resource,
        client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET,
        scope,
        campaign_id,
        ad_group_id,
    )

    remove_op = client.get_type("MutateOperation")
    link = _LINK_RESOURCE[scope]
    getattr(remove_op, f"{link}_operation").remove = old_link

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=[create_op, link_op, remove_op]
    )
    created, linked, removed = response.mutate_operation_responses
    return {
        "new_asset": created.asset_result.resource_name,
        "new_link": getattr(linked, f"{link}_result").resource_name,
        "old_link_removed": getattr(removed, f"{link}_result").resource_name or old_link,
    }
