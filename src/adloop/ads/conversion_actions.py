"""Conversion-action write tools — Google Ads ConversionActionService.

All operations follow the AdLoop safety pattern:
    1. draft_*  → creates a ChangePlan, stores it, returns plan_id
    2. confirm_and_apply(plan_id) → executes via the Google Ads API

Supported types (conversion_action.type):
    AD_CALL              — calls from Call assets in ads
    WEBSITE_CALL         — Google Forwarding Number calls (uses
                           phone_call_duration_seconds threshold)
    WEBPAGE              — page-load conversions with code-based tracking
    WEBPAGE_CODELESS     — page-load conversions detected by Ads (no snippet)
    GOOGLE_ANALYTICS_4_CUSTOM   — imported from GA4 (custom event)
    GOOGLE_ANALYTICS_4_PURCHASE — imported from GA4 (purchase event)
    UPLOAD_CALLS, UPLOAD_CLICKS — offline imports

NOT supported here (Google manages them — mutations are rejected with
MUTATE_NOT_ALLOWED):
    SMART_CAMPAIGN_*  — auto-created by Smart Campaigns
    GOOGLE_HOSTED     — auto-created by Google Business Profile / LSA links
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.ads.enums import enum_names

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


# Pulled dynamically from the google-ads SDK at the API version we're
# pinned to (see adloop.ads.client.GOOGLE_ADS_API_VERSION). Keeps the
# validators in sync with whatever the SDK supports — no hand-maintained
# parallel lists to drift.
_VALID_TYPES = enum_names("ConversionActionTypeEnum")
_VALID_CATEGORIES = enum_names("ConversionActionCategoryEnum")
_VALID_COUNTING_TYPES = enum_names("ConversionActionCountingTypeEnum")
_VALID_ATTRIBUTION_MODELS = enum_names("AttributionModelEnum")

# These types ARE in ConversionActionTypeEnum but Google rejects mutations
# on them with MUTATE_NOT_ALLOWED (they're auto-created by Smart Campaigns,
# Local Services, and Business Profile links). We don't filter them from
# `_VALID_TYPES` — the SDK accepts them syntactically — but warn callers.
_AUTO_MANAGED_TYPES = frozenset({
    "SMART_CAMPAIGN_TRACKED_CALLS",
    "SMART_CAMPAIGN_MAP_DIRECTIONS",
    "SMART_CAMPAIGN_MAP_CLICKS_TO_CALL",
    "SMART_CAMPAIGN_AD_CLICKS_TO_CALL",
    "GOOGLE_HOSTED",
})


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_create_inputs(
    *,
    name: str,
    type_: str,
    category: str,
    counting_type: str,
    default_value: float,
    currency_code: str,
    phone_call_duration_seconds: int,
    click_through_window_days: int,
    view_through_window_days: int,
    attribution_model: str,
) -> list[str]:
    errors: list[str] = []
    if not name or not name.strip():
        errors.append("name is required")
    if type_ not in _VALID_TYPES:
        errors.append(
            f"type '{type_}' invalid; valid: {sorted(_VALID_TYPES)}"
        )
    if category and category not in _VALID_CATEGORIES:
        errors.append(
            f"category '{category}' invalid; valid: {sorted(_VALID_CATEGORIES)}"
        )
    if counting_type and counting_type not in _VALID_COUNTING_TYPES:
        errors.append(
            f"counting_type '{counting_type}' invalid; valid: "
            f"{sorted(_VALID_COUNTING_TYPES)}"
        )
    if default_value < 0:
        errors.append("default_value must be >= 0")
    if currency_code and len(currency_code) != 3:
        errors.append(
            f"currency_code '{currency_code}' must be a 3-letter ISO code"
        )
    if phone_call_duration_seconds and phone_call_duration_seconds < 0:
        errors.append("phone_call_duration_seconds must be >= 0")
    if (click_through_window_days
            and not (1 <= click_through_window_days <= 90)):
        errors.append(
            "click_through_window_days must be between 1 and 90"
        )
    if (view_through_window_days
            and not (1 <= view_through_window_days <= 30)):
        errors.append(
            "view_through_window_days must be between 1 and 30"
        )
    if attribution_model and attribution_model not in _VALID_ATTRIBUTION_MODELS:
        errors.append(
            f"attribution_model '{attribution_model}' invalid; valid: "
            f"{sorted(_VALID_ATTRIBUTION_MODELS)}"
        )
    return errors


def _validate_update_inputs(
    *,
    counting_type: str,
    default_value: float,
    currency_code: str,
    phone_call_duration_seconds: int,
    click_through_window_days: int,
    view_through_window_days: int,
    attribution_model: str,
) -> list[str]:
    errors: list[str] = []
    if counting_type and counting_type not in _VALID_COUNTING_TYPES:
        errors.append(
            f"counting_type '{counting_type}' invalid; valid: "
            f"{sorted(_VALID_COUNTING_TYPES)}"
        )
    if default_value < 0:
        errors.append("default_value must be >= 0")
    if currency_code and len(currency_code) != 3:
        errors.append(
            f"currency_code '{currency_code}' must be a 3-letter ISO code"
        )
    if phone_call_duration_seconds and phone_call_duration_seconds < 0:
        errors.append("phone_call_duration_seconds must be >= 0")
    if (click_through_window_days
            and not (1 <= click_through_window_days <= 90)):
        errors.append(
            "click_through_window_days must be between 1 and 90"
        )
    if (view_through_window_days
            and not (1 <= view_through_window_days <= 30)):
        errors.append(
            "view_through_window_days must be between 1 and 30"
        )
    if attribution_model and attribution_model not in _VALID_ATTRIBUTION_MODELS:
        errors.append(
            f"attribution_model '{attribution_model}' invalid; valid: "
            f"{sorted(_VALID_ATTRIBUTION_MODELS)}"
        )
    return errors


# ---------------------------------------------------------------------------
# Draft tools (return PREVIEW + plan_id)
# ---------------------------------------------------------------------------


def draft_create_conversion_action(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    name: str,
    type_: str,
    category: str = "DEFAULT",
    default_value: float = 0,
    currency_code: str = "USD",
    always_use_default_value: bool = False,
    counting_type: str = "ONE_PER_CLICK",
    phone_call_duration_seconds: int = 0,
    primary_for_goal: bool = True,
    include_in_conversions_metric: bool = True,
    click_through_window_days: int = 0,
    view_through_window_days: int = 0,
    attribution_model: str = "",
) -> dict:
    """Draft a new ConversionAction — returns a PREVIEW.

    type_: the ConversionAction.type enum value (AD_CALL, WEBSITE_CALL,
        WEBPAGE, WEBPAGE_CODELESS, GOOGLE_ANALYTICS_4_CUSTOM, etc.).
    category: the conversion category (PHONE_CALL_LEAD, SUBMIT_LEAD_FORM,
        PURCHASE, etc.). Defaults to DEFAULT.
    default_value: monetary value attributed to each conversion. Set 250
        for high-intent lead actions (per BGI Lead Conversion playbook).
    always_use_default_value: when True, transaction values from the
        snippet/import are ignored and default_value is used instead.
    counting_type: ONE_PER_CLICK (recommended for lead gen — one click,
        one conversion no matter how many events fire) or MANY_PER_CLICK
        (better for ecommerce where multiple purchases per click are real).
    phone_call_duration_seconds: ONLY meaningful for PHONE_CALL_LEAD
        category. The call must last at least this many seconds to count.
    primary_for_goal: True = drives Smart Bidding optimization;
        False = Secondary (records but doesn't affect bidding).
    include_in_conversions_metric: True (default) = appears in the
        "Conversions" column; False = "All conversions" only.
    click_through_window_days / view_through_window_days: attribution
        windows. 30/1 is the typical lead-gen pair.
    attribution_model: leave empty for the default. For data-driven,
        pass GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_conversion_action", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = _validate_create_inputs(
        name=name,
        type_=type_,
        category=category,
        counting_type=counting_type,
        default_value=default_value,
        currency_code=currency_code,
        phone_call_duration_seconds=phone_call_duration_seconds,
        click_through_window_days=click_through_window_days,
        view_through_window_days=view_through_window_days,
        attribution_model=attribution_model,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_conversion_action",
        entity_type="conversion_action",
        entity_id="",
        customer_id=customer_id,
        changes={
            "name": name.strip(),
            "type": type_,
            "category": category,
            "default_value": float(default_value),
            "currency_code": currency_code.upper(),
            "always_use_default_value": bool(always_use_default_value),
            "counting_type": counting_type,
            "phone_call_duration_seconds": int(phone_call_duration_seconds or 0),
            "primary_for_goal": bool(primary_for_goal),
            "include_in_conversions_metric": bool(include_in_conversions_metric),
            "click_through_window_days": int(click_through_window_days or 0),
            "view_through_window_days": int(view_through_window_days or 0),
            "attribution_model": attribution_model,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_update_conversion_action(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    conversion_action_id: str,
    name: str = "",
    primary_for_goal: bool | None = None,
    default_value: float = 0,
    currency_code: str = "",
    always_use_default_value: bool | None = None,
    counting_type: str = "",
    phone_call_duration_seconds: int = 0,
    include_in_conversions_metric: bool | None = None,
    click_through_window_days: int = 0,
    view_through_window_days: int = 0,
    attribution_model: str = "",
) -> dict:
    """Draft a partial UPDATE of an existing ConversionAction — returns PREVIEW.

    Only the parameters you pass non-empty/non-default will be sent to the
    API. Use this to rename, demote a Primary to Secondary, change value,
    adjust the call-duration threshold, or change attribution settings.

    conversion_action_id: numeric ID. Find via:
        SELECT conversion_action.id, conversion_action.name FROM conversion_action

    Note: Google rejects mutations on SMART_CAMPAIGN_* and GOOGLE_HOSTED
    types with MUTATE_NOT_ALLOWED. Catch and report this at apply time.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_conversion_action", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not conversion_action_id:
        return {"error": "conversion_action_id is required"}

    errors = _validate_update_inputs(
        counting_type=counting_type,
        default_value=default_value,
        currency_code=currency_code,
        phone_call_duration_seconds=phone_call_duration_seconds,
        click_through_window_days=click_through_window_days,
        view_through_window_days=view_through_window_days,
        attribution_model=attribution_model,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    # Track which fields the caller actually wants to update so we build
    # the right field_mask at apply time.
    changes: dict = {"conversion_action_id": str(conversion_action_id)}
    if name:
        changes["name"] = name.strip()
    if primary_for_goal is not None:
        changes["primary_for_goal"] = bool(primary_for_goal)
    if default_value:
        changes["default_value"] = float(default_value)
    if currency_code:
        changes["currency_code"] = currency_code.upper()
    if always_use_default_value is not None:
        changes["always_use_default_value"] = bool(always_use_default_value)
    if counting_type:
        changes["counting_type"] = counting_type
    if phone_call_duration_seconds:
        changes["phone_call_duration_seconds"] = int(phone_call_duration_seconds)
    if include_in_conversions_metric is not None:
        changes["include_in_conversions_metric"] = bool(
            include_in_conversions_metric
        )
    if click_through_window_days:
        changes["click_through_window_days"] = int(click_through_window_days)
    if view_through_window_days:
        changes["view_through_window_days"] = int(view_through_window_days)
    if attribution_model:
        changes["attribution_model"] = attribution_model

    if len(changes) == 1:  # only conversion_action_id
        return {"error": "No fields to update"}

    plan = ChangePlan(
        operation="update_conversion_action",
        entity_type="conversion_action",
        entity_id=str(conversion_action_id),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def draft_remove_conversion_action(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    conversion_action_id: str,
) -> dict:
    """Draft a REMOVAL of a ConversionAction — returns PREVIEW.

    Removed conversion actions stop counting and disappear from goal lists.
    Historical data is preserved. SMART_CAMPAIGN_* and GOOGLE_HOSTED types
    cannot be removed via API (Google manages them); the apply will fail
    with MUTATE_NOT_ALLOWED for those.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("remove_conversion_action", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not conversion_action_id:
        return {"error": "conversion_action_id is required"}

    plan = ChangePlan(
        operation="remove_conversion_action",
        entity_type="conversion_action",
        entity_id=str(conversion_action_id),
        customer_id=customer_id,
        changes={"conversion_action_id": str(conversion_action_id)},
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Removing a ConversionAction is irreversible. Smart Campaign / GBP-"
        "managed types reject mutation with MUTATE_NOT_ALLOWED."
    ]
    return preview


# ---------------------------------------------------------------------------
# Apply handlers
# ---------------------------------------------------------------------------


def _apply_create_conversion_action(client: object, cid: str, changes: dict) -> dict:
    """Create a new ConversionAction."""
    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    ca = op.create
    ca.name = changes["name"]
    ca.type_ = getattr(client.enums.ConversionActionTypeEnum, changes["type"])
    ca.category = getattr(
        client.enums.ConversionActionCategoryEnum, changes["category"]
    )
    ca.status = client.enums.ConversionActionStatusEnum.ENABLED
    ca.counting_type = getattr(
        client.enums.ConversionActionCountingTypeEnum, changes["counting_type"]
    )
    ca.value_settings.default_value = changes["default_value"]
    ca.value_settings.default_currency_code = changes["currency_code"]
    ca.value_settings.always_use_default_value = changes["always_use_default_value"]
    ca.primary_for_goal = changes["primary_for_goal"]
    ca.include_in_conversions_metric = changes["include_in_conversions_metric"]
    if changes.get("phone_call_duration_seconds"):
        ca.phone_call_duration_seconds = changes["phone_call_duration_seconds"]
    if changes.get("click_through_window_days"):
        ca.click_through_lookback_window_days = changes["click_through_window_days"]
    if changes.get("view_through_window_days"):
        ca.view_through_lookback_window_days = changes["view_through_window_days"]
    if changes.get("attribution_model"):
        ca.attribution_model_settings.attribution_model = getattr(
            client.enums.AttributionModelEnum, changes["attribution_model"]
        )

    response = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_update_conversion_action(client: object, cid: str, changes: dict) -> dict:
    """Partial update of an existing ConversionAction.

    Builds a FieldMask listing only the fields the caller wanted to update.
    """
    from google.protobuf import field_mask_pb2

    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    ca = op.update
    ca.resource_name = svc.conversion_action_path(
        cid, changes["conversion_action_id"]
    )

    paths: list[str] = []

    if "name" in changes:
        ca.name = changes["name"]
        paths.append("name")
    if "primary_for_goal" in changes:
        ca.primary_for_goal = changes["primary_for_goal"]
        paths.append("primary_for_goal")
    if "default_value" in changes:
        ca.value_settings.default_value = changes["default_value"]
        paths.append("value_settings.default_value")
    if "currency_code" in changes:
        ca.value_settings.default_currency_code = changes["currency_code"]
        paths.append("value_settings.default_currency_code")
    if "always_use_default_value" in changes:
        ca.value_settings.always_use_default_value = changes["always_use_default_value"]
        paths.append("value_settings.always_use_default_value")
    if "counting_type" in changes:
        ca.counting_type = getattr(
            client.enums.ConversionActionCountingTypeEnum, changes["counting_type"]
        )
        paths.append("counting_type")
    if "phone_call_duration_seconds" in changes:
        ca.phone_call_duration_seconds = changes["phone_call_duration_seconds"]
        paths.append("phone_call_duration_seconds")
    if "include_in_conversions_metric" in changes:
        ca.include_in_conversions_metric = changes["include_in_conversions_metric"]
        paths.append("include_in_conversions_metric")
    if "click_through_window_days" in changes:
        ca.click_through_lookback_window_days = changes["click_through_window_days"]
        paths.append("click_through_lookback_window_days")
    if "view_through_window_days" in changes:
        ca.view_through_lookback_window_days = changes["view_through_window_days"]
        paths.append("view_through_lookback_window_days")
    if "attribution_model" in changes:
        ca.attribution_model_settings.attribution_model = getattr(
            client.enums.AttributionModelEnum, changes["attribution_model"]
        )
        paths.append("attribution_model_settings.attribution_model")

    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
    response = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_remove_conversion_action(client: object, cid: str, changes: dict) -> dict:
    """Remove a ConversionAction (sets status=REMOVED)."""
    svc = client.get_service("ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    op.remove = svc.conversion_action_path(
        cid, changes["conversion_action_id"]
    )
    response = svc.mutate_conversion_actions(
        customer_id=cid, operations=[op]
    )
    return {"resource_name": response.results[0].resource_name}
