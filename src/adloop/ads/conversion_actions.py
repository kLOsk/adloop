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

import hashlib
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
    default_value: monetary value attributed to each conversion.
    always_use_default_value: when True, transaction values from the
        snippet/import are ignored and default_value is used instead. When
        False with a positive default_value, Google treats default_value as
        a fallback ("tag value with fallback"). Passing a positive
        default_value with this flag False is a legal config — the draft
        surfaces a warning (see below) but does NOT flip the flag for you.
    counting_type: ONE_PER_CLICK (recommended for lead gen — one click,
        one conversion no matter how many events fire) or MANY_PER_CLICK
        (better for ecommerce where multiple purchases per click are real).
    phone_call_duration_seconds: ONLY meaningful for PHONE_CALL_LEAD
        category. The call must last at least this many seconds to count.
    primary_for_goal: True = drives Smart Bidding optimization;
        False = Secondary (records but doesn't affect bidding).
    include_in_conversions_metric: True (default) = appears in the
        "Conversions" column; False = "All conversions" only. NOTE: this is
        IMMUTABLE on create — Google derives it from the category and rejects
        any value set in the create mutate. To change it, use
        draft_update_conversion_action after the create succeeds.
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

    warnings: list[str] = []

    # A positive default_value paired with always_use_default_value=False is a
    # LEGAL config: Google treats default_value as a fallback when the
    # snippet/import supplies no value ("tag value with fallback"). We used to
    # silently force the flag to True, which turned that fallback config into
    # an unconditional override — a real change in accounting the caller never
    # asked for. Surface it as a preview warning instead and leave the flag
    # exactly as the caller set it.
    if default_value > 0 and not always_use_default_value:
        warnings.append(
            "default_value is set but always_use_default_value is False: "
            "Google will treat default_value as a FALLBACK, used only when "
            "the tag/import provides no value. If you want default_value to "
            "override every conversion's value, set "
            "always_use_default_value=True explicitly."
        )

    if type_ in _AUTO_MANAGED_TYPES:
        warnings.append(
            f"type '{type_}' is auto-managed by Google (Smart Campaigns / "
            "Business Profile). Mutations are rejected with MUTATE_NOT_ALLOWED."
        )

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
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


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
    include_in_conversions_metric IS mutable here (unlike on create).

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

    warnings: list[str] = []
    # Same fallback-vs-override nuance as create: on update, a positive
    # default_value with always_use_default_value explicitly set to False is
    # legal (fallback). Warn rather than silently overriding intent.
    if changes.get("default_value", 0) > 0 and (
        changes.get("always_use_default_value") is False
    ):
        warnings.append(
            "default_value is set but always_use_default_value is False: "
            "Google will treat default_value as a FALLBACK, used only when "
            "the tag/import provides no value. Set "
            "always_use_default_value=True explicitly to override every "
            "conversion's value."
        )

    plan = ChangePlan(
        operation="update_conversion_action",
        entity_type="conversion_action",
        entity_id=str(conversion_action_id),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


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
    # NOTE: include_in_conversions_metric is IMMUTABLE on create — Google
    # derives it from the conversion category and rejects any value set in
    # the create mutate (IMMUTABLE_FIELD). To change it, use
    # draft_update_conversion_action after the create succeeds.
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


# ===========================================================================
# Offline conversion uploads — ConversionUploadService
# ===========================================================================
#
# Two upload paths live here:
#
#   1. Call conversions (UploadCallConversions) — matches phone calls back to
#      ad clicks by caller_id (E.164 phone). The caller_id is REQUIRED raw by
#      Google for matching and CANNOT be hashed. It is stored in the plan so
#      apply can rebuild the upload without re-reading the CSV, but it is
#      REDACTED in the audit log and preview display (see _redact_caller_id
#      and _redact_changes_for_audit in write.py).
#
#   2. Enhanced Conversions for Leads (UploadClickConversions with
#      user_identifiers) — matches hashed PII (email / phone / name) back to
#      logged-in Google users who clicked our ads. PII is normalized and
#      SHA-256-hashed AT PREVIEW TIME; only the hashes are stored in the plan.
#      Raw PII never lands in plan.changes and never reaches the audit log.
#
# Security invariant shared by both: apply builds the upload protos from
# ``plan.changes["rows"]`` (frozen at preview time), NOT by re-reading the
# CSV. What you previewed is exactly what gets uploaded, and no raw PII is
# re-read at apply time.
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    """SHA-256 a UTF-8 string, return lowercase hex. Empty in → empty out."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    """Normalize an email for Enhanced Conversions: trim + lowercase.

    Google's canonicalization for EC is trim + lowercase. (Gmail dot/plus
    stripping is NOT applied by Google's EC matcher — it matches on the
    literal normalized address — so we deliberately do NOT strip dots or
    +tags. Doing so would REDUCE the match rate.)
    """
    return (email or "").strip().lower()


def _normalize_name(name: str) -> str:
    """Normalize a first/last name for EC: trim + lowercase."""
    return (name or "").strip().lower()


def _normalize_phone_e164(phone: str) -> str:
    """Best-effort E.164 normalization for a phone number.

    Rules (deliberately conservative — Google requires E.164 for EC phone
    hashing and CallConversion.caller_id):
      * Strip spaces, hyphens, parens, dots.
      * A leading "00" is the international-access prefix → replace with "+".
      * A single leading domestic trunk "0" (common in EU national format,
        e.g. UK "020 7946 0018") is dropped — but ONLY one zero, and ONLY
        when there's no "+" already. We do NOT strip every leading zero.
      * Italy is the notable exception: Italian fixed-line numbers KEEP their
        leading 0 in E.164 (e.g. Rome "+39 06 …"). We can't reliably detect
        country from a bare national number, so the safe, documented rule is:
        if the number already carries a country code (starts with "+"), we
        never touch interior digits. A bare Italian number passed without a
        "+" can't be disambiguated here — callers should pass Italian numbers
        in full "+39…" form. This keeps the common EU trunk-zero case correct
        without corrupting Italy's retained-zero numbers that arrive as "+39…".

    Returns the number with a leading "+" when we could infer one; otherwise
    returns the cleaned digits unchanged (Google will reject a non-E.164
    number, surfaced as a per-row failure rather than silently mangled).
    """
    s = (phone or "").strip()
    if not s:
        return ""
    has_plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if has_plus:
        # Already carries a country code — trust it verbatim (this is the
        # path that preserves Italy's retained leading zero, e.g. +3906…).
        return "+" + digits
    # "00" international access prefix → "+"
    if digits.startswith("00"):
        return "+" + digits[2:]
    # Strip EXACTLY ONE domestic trunk zero (national dialing format).
    if digits.startswith("0"):
        return digits[1:]
    return digits


def _gaql_escape(s: str) -> str:
    """Escape a string literal for interpolation into a GAQL WHERE clause.

    GAQL uses BACKSLASH escaping (NOT SQL-style doubled quotes). Escape the
    backslash first, then the single quote. Handles names like ``O'Brien``.
    """
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _consent_from_param(consent: dict | None) -> dict | None:
    """Validate + normalize the ``consent`` tool parameter.

    Accepts ``{"ad_user_data": "GRANTED"|"DENIED"|"UNSPECIFIED",
    "ad_personalization": ...}``. Missing keys default to UNSPECIFIED.
    Returns a plain dict stored in the plan (JSON-safe, non-PII), or None
    if no consent was supplied at all.
    """
    if not consent:
        return None
    valid = {"UNSPECIFIED", "UNKNOWN", "GRANTED", "DENIED"}
    out: dict[str, str] = {}
    for key in ("ad_user_data", "ad_personalization"):
        raw = str(consent.get(key, "UNSPECIFIED") or "UNSPECIFIED").upper()
        if raw not in valid:
            raise ValueError(
                f"consent.{key}='{raw}' is invalid. Use one of: "
                "GRANTED, DENIED, UNSPECIFIED."
            )
        out[key] = raw
    return out


def _apply_consent(client: object, conversion: object, consent: dict | None) -> None:
    """Set conversion.consent.{ad_user_data,ad_personalization} from a plan dict.

    Maps the stored string values to the ConsentStatus enum. A None/empty
    consent leaves the proto default (UNSPECIFIED) — which is the correct
    "not provided" signal for Google.
    """
    if not consent:
        return
    status_enum = client.enums.ConsentStatusEnum
    aud = consent.get("ad_user_data", "UNSPECIFIED")
    ap = consent.get("ad_personalization", "UNSPECIFIED")
    conversion.consent.ad_user_data = getattr(status_enum, aud)
    conversion.consent.ad_personalization = getattr(status_enum, ap)


# ---------------------------------------------------------------------------
# Timestamp + CSV parsing shared with the call-conversion path
# ---------------------------------------------------------------------------

_EXPECTED_CALL_HEADERS = [
    "Caller's Phone Number",
    "Call Start Time",
    "Conversion Name",
    "Conversion Time",
    "Conversion Value",
    "Conversion Currency",
]


def _normalize_call_timestamp(ts: str) -> str:
    """Google Ads API wants 'yyyy-mm-dd HH:MM:SS+|-HH:MM'.

    Our CSV writes ISO 8601 with 'T' separator and trailing 'Z'
    (e.g. '2026-02-26T16:49:44.567Z'). Convert: strip fractional
    seconds, replace 'T' with space, replace 'Z' with '+00:00'.
    """
    s = (ts or "").strip()
    if not s:
        return s
    if "." in s:
        head, tail = s.split(".", 1)
        tz = ""
        for marker in ("+", "-", "Z"):
            idx = tail.find(marker)
            if idx >= 0:
                tz = tail[idx:]
                break
        s = head + (tz or "")
    s = s.replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s


def _parse_call_conversion_csv(csv_path: str) -> tuple[list[dict], list[str]]:
    """Read the AdLoop-generated phone-conversions CSV.

    Returns (rows, errors). Rows are dicts keyed by canonical column name.
    Skips the optional `Parameters:TimeZone=...` row at the top.

    The ``caller_id`` (E.164 phone) is retained RAW — Google requires it for
    call-to-click matching and it cannot be hashed. Callers that persist
    these rows (the draft path does, into the plan) are responsible for
    redacting caller_id from any audit log or preview surface.
    """
    import csv
    from pathlib import Path

    errors: list[str] = []
    path = Path(csv_path).expanduser()
    if not path.exists():
        return [], [f"CSV not found at {path}"]

    with path.open("r", newline="") as f:
        reader = csv.reader(f)
        rows_iter = iter(reader)
        header: list[str] | None = None
        for raw in rows_iter:
            if not raw:
                continue
            first = (raw[0] or "").strip()
            if first.startswith(("Parameters:", "#", "###")):
                continue
            header = [c.strip() for c in raw]
            break
        if header is None:
            return [], ["CSV is empty (no header row found)"]

        missing = [c for c in _EXPECTED_CALL_HEADERS if c not in header]
        if missing:
            errors.append(
                f"CSV missing required columns: {missing}. Got: {header}"
            )
            return [], errors

        col = {name: header.index(name) for name in _EXPECTED_CALL_HEADERS}
        out: list[dict] = []
        for line_num, raw in enumerate(rows_iter, start=2):
            if not raw or all((c or "").strip() == "" for c in raw):
                continue
            if (raw[0] or "").strip().startswith(("#", "Parameters:")):
                continue
            try:
                value_str = raw[col["Conversion Value"]].strip()
                value = float(value_str) if value_str else 0.0
            except (ValueError, IndexError):
                errors.append(f"Row {line_num}: invalid Conversion Value")
                continue
            out.append({
                "caller_id": _normalize_phone_e164(
                    raw[col["Caller's Phone Number"]]
                ),
                "call_start_time": _normalize_call_timestamp(
                    raw[col["Call Start Time"]]
                ),
                "conversion_name": raw[col["Conversion Name"]].strip(),
                "conversion_time": _normalize_call_timestamp(
                    raw[col["Conversion Time"]]
                ),
                "conversion_value": value,
                "currency_code": (
                    raw[col["Conversion Currency"]].strip().upper() or "USD"
                ),
            })
        return out, errors


def _redact_caller_id(caller_id: str) -> str:
    """Mask an E.164 phone for display/logging: keep the leading digits and
    the last 4, star the middle. e.g. '+15555550142' -> '+155***0142'.
    """
    s = (caller_id or "").strip()
    if not s:
        return ""
    if len(s) <= 6:
        return "***"
    head = s[:4]
    tail = s[-4:]
    return f"{head}***{tail}"


def draft_upload_call_conversions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    partial_failure: bool = True,
    consent: dict | None = None,
) -> dict:
    """Draft an upload of call conversions from CSV — returns a PREVIEW.

    Reads any CSV matching Google Ads' call-upload schema and previews what
    would be sent to ConversionUploadService.UploadCallConversions.

    Required CSV columns: Caller's Phone Number, Call Start Time, Conversion
    Name, Conversion Time, Conversion Value, Conversion Currency. An optional
    ``Parameters:TimeZone=...`` row at the top is ignored.

    The ``Conversion Name`` value MUST exactly match an existing conversion
    action whose type is UPLOAD_CALLS.

    ``partial_failure`` (default True) lets Google accept the rows that parse
    successfully and report only the bad ones — recommended.

    ``consent`` (GDPR/EEA): a dict like
    ``{"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}``. Values:
    GRANTED / DENIED / UNSPECIFIED. Required for EEA traffic. Defaults to
    UNSPECIFIED when omitted.

    PII note: the caller phone number is required raw by Google for matching,
    so it is stored in the plan (needed by apply) but REDACTED in the preview
    and the audit log. Call confirm_and_apply with the returned plan_id.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("upload_call_conversions", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    try:
        consent_norm = _consent_from_param(consent)
    except ValueError as e:
        return {"error": str(e)}

    rows, parse_errors = _parse_call_conversion_csv(csv_path)
    if parse_errors and not rows:
        return {"error": "CSV parse failed", "details": parse_errors}
    if not rows:
        return {"error": "CSV contained zero conversion rows"}

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value = sum(r["conversion_value"] for r in rows)

    # Freeze the exact rows apply will upload (caller_id RAW — required by
    # Google). These live in the plan; the audit path redacts caller_id.
    frozen_rows = [
        {
            "caller_id": r["caller_id"],
            "call_start_time": r["call_start_time"],
            "conversion_name": r["conversion_name"],
            "conversion_time": r["conversion_time"],
            "conversion_value": r["conversion_value"],
            "currency_code": r["currency_code"],
        }
        for r in rows
    ]

    plan = ChangePlan(
        operation="upload_call_conversions",
        entity_type="call_conversion_batch",
        entity_id=str(len(rows)),
        customer_id=customer_id,
        changes={
            "row_count": len(rows),
            "total_value": round(total_value, 2),
            "currency_hint": rows[0]["currency_code"] if rows else "USD",
            "distinct_conversion_actions": distinct_actions,
            "partial_failure": bool(partial_failure),
            "consent": consent_norm,
            "parse_warnings": parse_errors,
            # RAW caller_id lives here (apply needs it). REDACTED for audit.
            "rows": frozen_rows,
            # Display sample uses REDACTED caller ids only.
            "sample_rows": [
                {
                    "caller_id": _redact_caller_id(r["caller_id"]),
                    "call_start_time": r["call_start_time"],
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                }
                for r in rows[:3]
            ],
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _resolve_conversion_action_ids(
    client: object, cid: str, names: list[str]
) -> dict[str, str]:
    """Look up conversion_action.resource_name for a list of names.

    Raises ValueError if any name isn't found OR isn't of type UPLOAD_CALLS.
    """
    if not names:
        return {}

    ga_service = client.get_service("GoogleAdsService")
    quoted = ", ".join(f"'{_gaql_escape(n)}'" for n in names)
    query = (
        "SELECT conversion_action.id, conversion_action.name, "
        "conversion_action.resource_name, conversion_action.type, "
        "conversion_action.status "
        "FROM conversion_action "
        f"WHERE conversion_action.name IN ({quoted}) "
        "AND conversion_action.status != 'REMOVED'"
    )
    response = ga_service.search(customer_id=cid, query=query)

    mapping: dict[str, str] = {}
    bad_types: list[str] = []
    for row in response:
        ca = row.conversion_action
        ca_type = ca.type_.name if hasattr(ca.type_, "name") else str(ca.type_)
        if ca_type != "UPLOAD_CALLS":
            bad_types.append(f"{ca.name} (type={ca_type})")
            continue
        mapping[ca.name] = ca.resource_name

    missing = [n for n in names if n not in mapping]
    if bad_types:
        raise ValueError(
            "Some conversion actions exist but are not of type UPLOAD_CALLS "
            f"(needed for call uploads): {bad_types}. "
            "Create a new conversion action via "
            "draft_create_conversion_action(type_='UPLOAD_CALLS', ...) "
            "or via Google Ads UI: Tools → Conversions → New → "
            "Import → Other data sources → Track conversions from calls."
        )
    if missing:
        raise ValueError(
            f"Conversion action(s) not found: {missing}. "
            "Verify the 'Conversion Name' column in the CSV matches exactly."
        )
    return mapping


def _apply_upload_call_conversions(
    client: object, cid: str, changes: dict
) -> dict:
    """Execute the call-conversion upload via ConversionUploadService.

    Builds the upload protos from ``changes["rows"]`` (frozen at preview
    time) — the CSV is NOT re-read. Returns counts of successes / failures
    plus per-row error details.
    """
    rows = changes.get("rows") or []
    if not rows:
        return {"error": "Plan contained zero call-conversion rows"}

    distinct = sorted({r["conversion_name"] for r in rows})
    action_resources = _resolve_conversion_action_ids(client, cid, distinct)
    consent = changes.get("consent")

    payload: list = []
    for r in rows:
        cc = client.get_type("CallConversion")
        cc.caller_id = r["caller_id"]
        cc.call_start_date_time = r["call_start_time"]
        cc.conversion_action = action_resources[r["conversion_name"]]
        cc.conversion_date_time = r["conversion_time"]
        cc.conversion_value = float(r["conversion_value"])
        cc.currency_code = r["currency_code"]
        _apply_consent(client, cc, consent)
        payload.append(cc)

    upload_service = client.get_service("ConversionUploadService")
    response = upload_service.upload_call_conversions(
        customer_id=cid,
        conversions=payload,
        partial_failure=bool(changes.get("partial_failure", True)),
    )

    results = list(response.results)
    # Google only populates the result row's ``conversion_action`` for rows
    # that were actually accepted. Echoed identifiers (caller_id) come back
    # even for FAILED rows, so counting those overreports success — key off
    # conversion_action being populated instead.
    success_count = sum(
        1 for r in results if getattr(r, "conversion_action", "")
    )
    failure_count = len(results) - success_count

    row_errors: list[dict] = []
    partial = getattr(response, "partial_failure_error", None)
    if partial and partial.message:
        row_errors.append({
            "type": "partial_failure",
            "message": partial.message,
            "code": getattr(partial, "code", None),
        })

    return {
        "uploaded_total": len(payload),
        "success_count": success_count,
        "failure_count": failure_count,
        "conversion_actions_used": action_resources,
        "row_errors": row_errors,
    }


# ---------------------------------------------------------------------------
# Enhanced Conversions for Leads — UploadClickConversions w/ user_identifiers
#
# The CSV here carries RAW PII (email / phone / name). We normalize and
# SHA-256-hash it AT PREVIEW TIME and store only the hashes in the plan.
# Raw PII is never persisted and never reaches the audit log.
# ---------------------------------------------------------------------------

_EXPECTED_EC_HEADERS = [
    "Email",
    "Phone Number",
    "First Name",
    "Last Name",
    "Conversion Name",
    "Conversion Time",
    "Conversion Value",
    "Conversion Currency",
]

# Optional CSV columns — parsed when present, omitted otherwise. Order ID is
# Google's dedup key for ClickConversion uploads: if set, re-uploading the
# same (conversion_action, order_id) pair is idempotent; if absent, re-uploads
# double-count.
_OPTIONAL_EC_HEADERS = [
    "Order ID",
]


def _parse_ec_for_leads_csv(csv_path: str) -> tuple[list[dict], list[str]]:
    """Parse the EC-for-Leads CSV and hash PII at parse time.

    Required columns: Email, Phone Number, First Name, Last Name,
    Conversion Name, Conversion Time, Conversion Value, Conversion Currency.
    Optional: Order ID (Google's dedup key — strongly recommended so
    re-uploads of the same source row don't double-count).

    The Email / Phone Number / First Name / Last Name columns hold RAW PII.
    Each is normalized (email→trim+lowercase, phone→E.164, names→trim+
    lowercase) and then SHA-256-hashed here. Returned rows contain ONLY the
    hashes (``*_sha256`` keys) plus non-PII fields — the raw values never
    leave this function.
    """
    import csv
    from pathlib import Path

    errors: list[str] = []
    path = Path(csv_path).expanduser()
    if not path.exists():
        return [], [f"CSV not found at {path}"]

    with path.open("r", newline="") as f:
        reader = csv.reader(f)
        rows_iter = iter(reader)
        header: list[str] | None = None
        for raw in rows_iter:
            if not raw:
                continue
            first = (raw[0] or "").strip()
            if first.startswith(("Parameters:", "#")):
                continue
            header = [c.strip() for c in raw]
            break
        if header is None:
            return [], ["CSV is empty"]

        missing = [c for c in _EXPECTED_EC_HEADERS if c not in header]
        if missing:
            return [], [
                f"CSV missing required columns: {missing}. Got: {header}"
            ]
        col = {n: header.index(n) for n in _EXPECTED_EC_HEADERS}
        optional_col = {
            n: header.index(n) for n in _OPTIONAL_EC_HEADERS if n in header
        }
        out: list[dict] = []
        for line_num, raw in enumerate(rows_iter, start=2):
            if not raw or all((c or "").strip() == "" for c in raw):
                continue
            try:
                value_str = raw[col["Conversion Value"]].strip()
                value = float(value_str) if value_str else 0.0
            except (ValueError, IndexError):
                errors.append(f"Row {line_num}: invalid Conversion Value")
                continue
            order_id = ""
            if "Order ID" in optional_col:
                try:
                    order_id = raw[optional_col["Order ID"]].strip()
                except IndexError:
                    pass
            # Normalize THEN hash. Raw values are discarded immediately.
            email_norm = _normalize_email(raw[col["Email"]])
            phone_norm = _normalize_phone_e164(raw[col["Phone Number"]])
            first_norm = _normalize_name(raw[col["First Name"]])
            last_norm = _normalize_name(raw[col["Last Name"]])
            out.append({
                "email_sha256": _sha256_hex(email_norm),
                "phone_sha256": _sha256_hex(phone_norm),
                "first_name_sha256": _sha256_hex(first_norm),
                "last_name_sha256": _sha256_hex(last_norm),
                "conversion_name": raw[col["Conversion Name"]].strip(),
                "conversion_time": _normalize_call_timestamp(
                    raw[col["Conversion Time"]]
                ),
                "conversion_value": value,
                "currency_code": (
                    raw[col["Conversion Currency"]].strip().upper() or "USD"
                ),
                "order_id": order_id,
            })
        return out, errors


def draft_upload_enhanced_conversions_for_leads(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    partial_failure: bool = True,
    consent: dict | None = None,
) -> dict:
    """Draft an Enhanced Conversions for Leads upload — returns PREVIEW.

    Reads a CSV of RAW lead PII, normalizes + SHA-256-hashes the
    Email / Phone / First Name / Last Name columns, and previews what will
    be pushed via ConversionUploadService.UploadClickConversions with
    user_identifiers populated. Only the hashes are stored in the plan —
    raw PII never lands in plan.changes or the audit log.

    The target conversion action must be of type UPLOAD_CLICKS (EC for Leads
    layers user-identifier matching on top of click conversions). Works
    retroactively — no "action must exist before the call" constraint like
    UPLOAD_CALLS has.

    Optional ``Order ID`` CSV column → ClickConversion.order_id, Google's
    dedup key; without it, re-uploads double-count matched conversions.

    ``consent`` (GDPR/EEA): a dict like
    ``{"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}``. Values:
    GRANTED / DENIED / UNSPECIFIED. Required for EEA traffic. Defaults to
    UNSPECIFIED when omitted.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(
            "upload_enhanced_conversions_for_leads", config.safety
        )
    except SafetyViolation as e:
        return {"error": str(e)}

    try:
        consent_norm = _consent_from_param(consent)
    except ValueError as e:
        return {"error": str(e)}

    rows, parse_errors = _parse_ec_for_leads_csv(csv_path)
    if parse_errors and not rows:
        return {"error": "CSV parse failed", "details": parse_errors}
    if not rows:
        return {"error": "CSV contained zero conversion rows"}

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value = sum(r["conversion_value"] for r in rows)
    with_email = sum(1 for r in rows if r["email_sha256"])
    with_phone = sum(1 for r in rows if r["phone_sha256"])
    with_order_id = sum(1 for r in rows if r.get("order_id"))

    dedup_warnings: list[str] = []
    if with_order_id == 0:
        dedup_warnings.append(
            "No Order ID column present. Re-uploading this CSV will "
            "double-count any matched conversions because Google has no "
            "dedup key. Add an Order ID column (e.g. a stable source row "
            "identifier) so re-uploads are idempotent."
        )
    elif with_order_id < len(rows):
        dedup_warnings.append(
            f"Only {with_order_id} of {len(rows)} rows have an Order ID. "
            "Rows without one will double-count on re-upload."
        )

    # Freeze the hashed rows apply will upload. These are already SHA-256
    # hashes — NO raw PII. Safe to persist in the plan and (order_id/value/
    # currency/time/action only) surface in the audit log.
    frozen_rows = [
        {
            "email_sha256": r["email_sha256"],
            "phone_sha256": r["phone_sha256"],
            "first_name_sha256": r["first_name_sha256"],
            "last_name_sha256": r["last_name_sha256"],
            "conversion_name": r["conversion_name"],
            "conversion_time": r["conversion_time"],
            "conversion_value": r["conversion_value"],
            "currency_code": r["currency_code"],
            "order_id": r.get("order_id", ""),
        }
        for r in rows
    ]

    plan = ChangePlan(
        operation="upload_enhanced_conversions_for_leads",
        entity_type="ec_for_leads_batch",
        entity_id=str(len(rows)),
        customer_id=customer_id,
        changes={
            "row_count": len(rows),
            "total_value": round(total_value, 2),
            "currency_hint": rows[0]["currency_code"] if rows else "USD",
            "rows_with_email": with_email,
            "rows_with_phone": with_phone,
            "rows_with_order_id": with_order_id,
            "distinct_conversion_actions": distinct_actions,
            "partial_failure": bool(partial_failure),
            "consent": consent_norm,
            "parse_warnings": parse_errors,
            "dedup_warnings": dedup_warnings,
            # Hashed-only rows — no raw PII. Apply builds protos from here.
            "rows": frozen_rows,
            "sample_rows": [
                {
                    "email_sha256": (r["email_sha256"][:16] + "...")
                    if r["email_sha256"] else "",
                    "phone_sha256": (r["phone_sha256"][:16] + "...")
                    if r["phone_sha256"] else "",
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                    "conversion_time": r["conversion_time"],
                    "order_id": r.get("order_id", ""),
                }
                for r in rows[:3]
            ],
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _resolve_upload_clicks_action(
    client: object, cid: str, names: list[str]
) -> dict[str, str]:
    """Look up conversion_action.resource_name for UPLOAD_CLICKS actions."""
    if not names:
        return {}

    ga_service = client.get_service("GoogleAdsService")
    quoted = ", ".join(f"'{_gaql_escape(n)}'" for n in names)
    query = (
        "SELECT conversion_action.id, conversion_action.name, "
        "conversion_action.resource_name, conversion_action.type, "
        "conversion_action.status "
        "FROM conversion_action "
        f"WHERE conversion_action.name IN ({quoted}) "
        "AND conversion_action.status != 'REMOVED'"
    )
    response = ga_service.search(customer_id=cid, query=query)

    mapping: dict[str, str] = {}
    wrong_type: list[str] = []
    for row in response:
        ca = row.conversion_action
        ca_type = ca.type_.name if hasattr(ca.type_, "name") else str(ca.type_)
        if ca_type != "UPLOAD_CLICKS":
            wrong_type.append(f"{ca.name} (type={ca_type})")
            continue
        mapping[ca.name] = ca.resource_name

    missing = [n for n in names if n not in mapping]
    if wrong_type:
        raise ValueError(
            "Some conversion actions are not of type UPLOAD_CLICKS "
            "(required for Enhanced Conversions for Leads uploads): "
            f"{wrong_type}. Use an UPLOAD_CLICKS-type action — create one "
            "via draft_create_conversion_action(type_='UPLOAD_CLICKS', ...)."
        )
    if missing:
        raise ValueError(
            f"Conversion action(s) not found: {missing}. "
            "Verify the 'Conversion Name' column in the CSV."
        )
    return mapping


def _apply_upload_enhanced_conversions_for_leads(
    client: object, cid: str, changes: dict
) -> dict:
    """Execute EC-for-Leads upload via ConversionUploadService.

    Builds the upload protos from ``changes["rows"]`` (SHA-256 hashes frozen
    at preview time) — the CSV is NOT re-read, so no raw PII is touched here.
    """
    rows = changes.get("rows") or []
    if not rows:
        return {"error": "Plan contained zero EC-for-leads rows"}

    distinct = sorted({r["conversion_name"] for r in rows})
    action_resources = _resolve_upload_clicks_action(client, cid, distinct)
    consent = changes.get("consent")

    payload: list = []
    for r in rows:
        cc = client.get_type("ClickConversion")
        cc.conversion_action = action_resources[r["conversion_name"]]
        cc.conversion_date_time = r["conversion_time"]
        cc.conversion_value = float(r["conversion_value"])
        cc.currency_code = r["currency_code"]

        # Order ID is Google's dedup key for ClickConversion. When set,
        # re-uploading the same (conversion_action, order_id) is a no-op;
        # without it, every upload counts as a fresh conversion.
        if r.get("order_id"):
            cc.order_id = r["order_id"]

        _apply_consent(client, cc, consent)

        # Build user_identifiers from the hashed PII. Google matches the
        # hashed email/phone/name to logged-in users who clicked our ads.
        if r["email_sha256"]:
            uid = client.get_type("UserIdentifier")
            uid.hashed_email = r["email_sha256"]
            cc.user_identifiers.append(uid)
        if r["phone_sha256"]:
            uid = client.get_type("UserIdentifier")
            uid.hashed_phone_number = r["phone_sha256"]
            cc.user_identifiers.append(uid)
        if r["first_name_sha256"] and r["last_name_sha256"]:
            uid = client.get_type("UserIdentifier")
            uid.address_info.hashed_first_name = r["first_name_sha256"]
            uid.address_info.hashed_last_name = r["last_name_sha256"]
            cc.user_identifiers.append(uid)

        if not cc.user_identifiers:
            continue
        payload.append(cc)

    upload_service = client.get_service("ConversionUploadService")
    response = upload_service.upload_click_conversions(
        customer_id=cid,
        conversions=payload,
        partial_failure=bool(changes.get("partial_failure", True)),
    )

    results = list(response.results)
    # The Google Ads API only populates the result row's ``conversion_action``
    # for rows that actually matched and were accepted. Failed rows come back
    # with empty conversion_action (and a corresponding partial_failure_error
    # entry). Echoed user_identifiers are NOT a success signal — the API
    # echoes them back for failed rows too — so key success off
    # conversion_action being populated.
    success_count = sum(
        1 for r in results if getattr(r, "conversion_action", "")
    )
    failure_count = len(results) - success_count

    row_errors: list[dict] = []
    partial = getattr(response, "partial_failure_error", None)
    if partial and partial.message:
        row_errors.append({
            "type": "partial_failure",
            "message": partial.message,
            "code": getattr(partial, "code", None),
        })

    return {
        "uploaded_total": len(payload),
        "success_count": success_count,
        "failure_count": failure_count,
        "conversion_actions_used": action_resources,
        "row_errors": row_errors,
    }
