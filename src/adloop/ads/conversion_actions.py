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
import math
import re
import threading
from datetime import datetime, timezone as _tz
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
        requires_double_confirm=True,
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
#      Google for matching and CANNOT be hashed. It lives in the plan's
#      apply-only payload so apply can rebuild the upload without re-reading
#      the CSV, and the preview/audit surfaces never see it — the summary they
#      show carries redacted ids only (see _redact_caller_id).
#
#   2. Enhanced Conversions for Leads (UploadClickConversions with
#      user_identifiers) — matches hashed PII (email / phone / name) back to
#      logged-in Google users who clicked our ads. PII is normalized and
#      SHA-256-hashed AT PREVIEW TIME; only the hashes are stored in the plan.
#      Raw PII never lands in plan.changes and never reaches the audit log.
#
# Security invariant shared by both: apply builds the upload protos from
# ``plan.apply_only_payload["rows"]`` (frozen at preview time), NOT by
# re-reading the CSV. What you previewed is exactly what gets uploaded, and no
# raw PII is re-read at apply time.
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    """SHA-256 a UTF-8 string, return lowercase hex. Empty in → empty out."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    """Normalize an email the way Enhanced Conversions expect it hashed.

    Google's rules, "Normalize and hash user-provided data" —
    https://developers.google.com/google-ads/api/docs/conversions/upload-identifiers#prepare-data
    (read 2026-10-07):

    * lowercase and remove whitespace everywhere;
    * for ``gmail.com`` / ``googlemail.com`` only, two steps the page lists
      under "Apply domain-specific normalization": "Remove periods ( . ): From
      the username portion … remove all period characters" and "Remove plus
      suffixes ( + ): … remove the first plus sign ( + ) and all subsequent
      characters up to the @ symbol". Its worked example is
      ``Jane.Doe+Shopping@googlemail.com`` → ``janedoe@googlemail.com``. The
      note above the code samples repeats it: for enhanced conversions the
      domain-specific normalization means "removing periods **and plus
      suffixes**", otherwise "different hash values than Google expects …
      leading to missed matches". Only some of the per-language samples on that
      page (C#, PHP, Ruby, Perl) stop at the periods, so this follows the
      stated rule, not those samples;
    * every other domain keeps dots and plus tags.

    A value that is not an address at all (no ``@``, or an empty side) hashes
    to nothing: a CRM export writes ``n/a`` or ``-`` into an empty column, and
    a hash of that would look like a usable identifier while matching nobody.
    """
    value = re.sub(r"\s+", "", (email or "").lower())
    if "@" not in value:
        return ""
    local, _, domain = value.rpartition("@")
    if not local or not domain:
        return ""
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def _normalize_name(name: str) -> str:
    """Normalize a first/last name for EC: trim, then lowercase.

    Google's own example (``upload_enhanced_conversions_for_leads.py``) does
    exactly ``s.strip().lower()`` for names — inner spaces stay, which matters
    for "Anna Lena" or "von der Berg". Removing them produces a hash Google
    does not expect, so the row would never match.
    """
    return (name or "").strip().lower()


def _normalize_phone_e164(phone: str, default_region: str = "") -> str:
    """E.164 for a phone number, or "" when the number is not usable.

    libphonenumber semantics, via ``phonenumbers`` (Google's own port): parse
    with an optional default region, then require ``is_valid_number``. That is
    what gets the common cases right — the German trunk marker in
    ``+49 (0)89 123456``, an extension in ``+1 415 555 0100 ext 12``, and a
    national ``0151 12345678`` that needs ``default_region="DE"`` to become
    ``+4915112345678``. A hand-rolled normalizer got all three wrong, silently.

    ``default_region`` is the ISO country code assumed for numbers without a
    country code. Without it such numbers cannot be resolved and return "".

    Returns "" for anything unparseable or invalid, so the caller can report
    the row instead of uploading a number Google cannot match.
    """
    import phonenumbers

    raw = (phone or "").strip()
    if not raw:
        return ""
    region = (default_region or "").strip().upper() or None
    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _gaql_escape(s: str) -> str:
    """Escape a string literal for interpolation into a GAQL WHERE clause.

    GAQL uses BACKSLASH escaping (NOT SQL-style doubled quotes). Escape the
    backslash first, then the single quote. Handles names like ``O'Brien``.
    """
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _consent_from_param(consent: dict | None) -> dict | None:
    """Validate + normalize the ``consent`` tool parameter.

    Accepts ``{"ad_user_data": "GRANTED"|"DENIED"|"UNSPECIFIED",
    "ad_personalization": ...}``. ``UNKNOWN`` is a legal enum value too.
    Missing keys default to UNSPECIFIED. Returns a plain dict stored in the
    plan (JSON-safe, non-PII), or None if no consent was supplied at all.

    An unknown key is an error rather than a silent no-op: ``adPersonalization``
    (camelCase) would otherwise leave the field at UNSPECIFIED, which for EEA
    traffic is exactly the wrong default to fall into unnoticed.
    """
    if not consent:
        return None
    if not isinstance(consent, dict):
        raise ValueError(
            "consent must be an object with 'ad_user_data' and/or "
            "'ad_personalization'"
        )
    known = ("ad_user_data", "ad_personalization")
    unknown = sorted(str(key) for key in set(consent) - set(known))
    if unknown:
        raise ValueError(
            f"consent has unknown key(s): {', '.join(unknown)}. Use only "
            "ad_user_data and ad_personalization."
        )
    valid = {"UNSPECIFIED", "UNKNOWN", "GRANTED", "DENIED"}
    out: dict[str, str] = {}
    for key in known:
        raw = str(consent.get(key, "UNSPECIFIED") or "UNSPECIFIED").upper()
        if raw not in valid:
            raise ValueError(
                f"consent.{key}='{raw}' is invalid. Use one of: "
                "GRANTED, DENIED, UNSPECIFIED, UNKNOWN."
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


# Fallbacks for what ``datetime.fromisoformat`` rejects. The two ISO entries are
# not dead code: ``fromisoformat`` insists on zero-padded components, so
# ``2026-3-1 12:00`` only parses here. The slash entries stay
# US-style (``mm/dd/yyyy``) — the dot separator is what marks German dates —
# and cover both the AM/PM form and Google's documented 24-hour one.
_TIMESTAMP_FORMATS = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def _has_time_component(value: str) -> bool:
    """Does this cell carry a clock time, or is it a bare date?"""
    return bool(re.search(r"\d{1,2}:\d{2}", value or ""))


def _parse_amount(value_cell: str, currency_cell: str) -> tuple[object, str, str]:
    """Validate a conversion value/currency pair; returns (value, currency, problem).

    An empty cell stays empty: sending 0.0 would override the conversion
    action's own default, and sending "USD" for a blank cell would override the
    account currency. ``nan``/``inf``/negative values are refused instead of
    being handed to the API.
    """
    raw_value = (value_cell or "").strip()
    if raw_value:
        try:
            value = float(raw_value)
        except ValueError:
            return None, "", "invalid Conversion Value"
        if not math.isfinite(value) or value < 0:
            return None, "", "Conversion Value must be a finite number ≥ 0"
    else:
        value = None

    currency = (currency_cell or "").strip().upper()
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        # The cell is never echoed: a shifted column can put an address or a
        # name there, and this text travels into changes, the preview and the
        # audit log. The caller names the row and the column instead.
        return None, "", "Conversion Currency must be a 3-letter ISO code"
    return value, currency, ""


def _valid_timezone(value: str) -> bool:
    """Is this a time zone Google's template allows — an IANA id or ±HHMM?"""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    text = (value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"[+-]\d{2}:?\d{2}", text):
        digits = text.replace(":", "")
        hours, minutes = int(digits[1:3]), int(digits[3:5])
        if hours > 14 or minutes > 59:
            return False
        try:
            _tz((1 if digits[0] == "+" else -1) * timedelta(
                hours=hours, minutes=minutes
            ))
        except ValueError:
            return False
        return True
    try:
        ZoneInfo(text)
    except Exception:  # noqa: BLE001 — unknown id from the CSV
        return False
    return True


def _tzinfo(value: str) -> object:
    """Build the tzinfo for a value ``_valid_timezone`` accepted."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    text = (value or "").strip()
    if re.fullmatch(r"[+-]\d{2}:?\d{2}", text):
        digits = text.replace(":", "")
        offset = timedelta(hours=int(digits[1:3]), minutes=int(digits[3:5]))
        return _tz((1 if digits[0] == "+" else -1) * offset)
    return ZoneInfo(text)


def _parse_timestamp(value: str, default_tz: str) -> tuple[str, str]:
    """Parse a CSV timestamp; returns ``(api_value, problem)``.

    Google wants ``yyyy-mm-dd hh:mm:ss±hh:mm``. Accepted input: ISO 8601 (with
    or without offset/``Z``), German ``dd.mm.yyyy hh:mm[:ss]`` and US
    ``mm/dd/yyyy hh:mm[:ss] [AM/PM]`` — the dot/slash separator is what tells
    the last two apart.

    A value without an offset needs a time zone: Google's own template ships a
    ``Parameters:TimeZone=…`` row for exactly that, and without one the row is
    refused instead of being uploaded against the server's idea of local time.
    """
    from datetime import datetime

    raw = (value or "").strip()
    if not raw:
        return "", "is empty"

    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    parsed = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return "", "is not a recognized timestamp"

    if parsed.tzinfo is None:
        if not default_tz:
            return "", (
                "has no time zone — add a 'Parameters:TimeZone=…' row to the "
                "CSV, or give the timestamp an offset"
            )
        if not _valid_timezone(default_tz):
            return "", (
                "uses a time zone that is not a valid IANA zone id or ±HHMM "
                "offset"
            )
        parsed = parsed.replace(tzinfo=_tzinfo(default_tz))

    return parsed.isoformat(sep=" ", timespec="seconds"), ""


# Google's own upload templates carry a "Parameters:TimeZone=…" row — read, not
# skipped, because it resolves timestamps without an offset — and use "#" for
# comment lines, which are skipped.
_CSV_PARAMETERS_PREFIX = "Parameters:"
_CSV_COMMENT_PREFIX = "#"

# A 2,000-row upload — the API's per-request cap — is well under 1 MB. The cap
# only stops a stray multi-gigabyte path from being read into memory.
_MAX_CSV_BYTES = 5 * 1024 * 1024


def _read_upload_csv(
    csv_path: str,
) -> tuple[list[tuple[int, list[str]]], list[str], str]:
    """Read an upload CSV into ``(source_line, cells)`` records (header first).

    ``source_line`` is the physical line in the file where the record starts —
    the only row number that means anything to the person editing the CSV, and
    therefore the only one used in errors, ``skipped_rows`` and the resume hint.
    Counting records instead would drift as soon as a comment or a bad row is
    dropped.

    Local-only by design: the path is read from the machine running AdLoop, so
    the tool refuses in server mode before it gets here. Errors name the file
    and the schema, never file *content* — a hosted runtime can read files the
    caller is not allowed to see.
    """
    import csv
    from pathlib import Path

    # Errors found while reading (an unreadable file, a bad time-zone row) ride
    # along with the records: the caller reports them, and a non-empty list
    # stops the parse.
    errors: list[str] = []
    # Google's template carries its time zone in a `Parameters:TimeZone=…` row,
    # which is what timestamps without an offset are resolved against.
    timezone = ""

    path = Path(csv_path).expanduser()
    if not path.is_file():
        return [], [f"CSV not found or not a regular file: {path}"], ""
    if path.suffix.lower() != ".csv":
        return [], [f"CSV must be a .csv file, got: {path.name}"], ""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], [f"CSV could not be read: {exc.strerror or exc}"], ""
    if size > _MAX_CSV_BYTES:
        return [], [
            f"CSV is {size / 1_048_576:.1f} MB; the limit is "
            f"{_MAX_CSV_BYTES // 1_048_576} MB. Split the file — Google accepts "
            "at most 2,000 rows per upload request anyway."
        ], ""

    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            position = {"line": 0}

            def _lines():
                for line in handle:
                    position["line"] += 1
                    yield line

            records: list[tuple[int, list[str]]] = []
            reader = csv.reader(_lines())
            while True:
                # The line where this record starts: a quoted field may span
                # several lines, and a resume hint has to point at the record,
                # not into the middle of it.
                start_line = position["line"] + 1
                try:
                    record = next(reader)
                except StopIteration:
                    break
                if not record:
                    continue
                # A blank record: every cell empty.
                if not any((cell or "").strip() for cell in record):
                    continue
                first = (record[0] or "").strip()
                if first.startswith(_CSV_PARAMETERS_PREFIX):
                    parameters = first[len(_CSV_PARAMETERS_PREFIX):]
                    for part in parameters.split(";"):
                        key, _, value = part.partition("=")
                        if key.strip().lower() != "timezone" or not value.strip():
                            continue
                        # Validated once, here: a bad row must not blow up per
                        # timestamp, and an unknown zone leaves the file without
                        # one, so offset-less values are refused individually.
                        if _valid_timezone(value):
                            timezone = timezone or value.strip()
                        else:
                            errors.append(
                                f"Line {start_line}: Parameters:TimeZone=… is "
                                "not a valid IANA zone id or ±HHMM offset"
                            )
                    continue
                if first.startswith(_CSV_COMMENT_PREFIX):
                    continue
                records.append((start_line, record))
    except OSError as exc:
        return [], [f"CSV could not be read: {exc.strerror or exc}"], ""
    except UnicodeDecodeError:
        return [], ["CSV is not valid UTF-8."], ""

    if not records:
        return [], ["CSV is empty (no header row found)"], ""
    return records, errors, timezone


def _column_map(
    header: list[str], expected: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Map required column names to indexes; report what is missing.

    The expected list is our own schema text, so it is safe in an error — the
    header the caller actually sent is not (it is file content).
    """
    columns = [cell.strip() for cell in header]
    missing = [name for name in expected if name not in columns]
    if missing:
        return {}, [
            f"CSV is missing required column(s): {', '.join(missing)}. "
            f"Expected columns: {', '.join(expected)}"
        ]
    return {name: columns.index(name) for name in expected}, []


def _short_row(raw: list[str], col: dict[str, int]) -> bool:
    """True when a record ends before the last required column.

    Google's exports occasionally break a line early (a stray newline, a hand
    edit). Indexing such a cell raises ``IndexError`` and aborts the whole
    draft with a message that names no row — the row is skipped with its
    ``source_line`` instead. A record that only lacks *optional* trailing
    columns is fine: it still carries everything the upload needs.
    """
    return len(raw) <= max(col.values())


def _pad_to_header(raw: list[str], columns: int) -> list[str]:
    """Fill missing trailing cells with "" so every header index exists."""
    if len(raw) >= columns:
        return raw
    return raw + [""] * (columns - len(raw))


def _parse_call_conversion_csv(
    csv_path: str, default_region: str = ""
) -> tuple[list[dict], list[str], list[str], list[dict]]:
    """Read the call-conversions CSV (local file) and normalize each row.

    Returns ``(rows, errors, advisories, skipped)``. Rows are dicts keyed by
    canonical column name; comment lines are skipped, and the optional
    ``Parameters:TimeZone=...`` row is read rather than skipped — it resolves
    timestamps that carry no offset.

    ``errors`` are file-level problems that stop the draft. ``advisories``
    describe rows that still upload (a bare date, a country code that dropped
    only the address fragment). ``skipped`` lists every row that will *not* be
    uploaded, with its ``source_line`` — that is what makes ``skipped_count``
    equal the number of CSV rows left out.

    The ``caller_id`` (E.164 phone) is retained RAW — Google requires it for
    call-to-click matching and it cannot be hashed. The draft stores it in
    ``ChangePlan.apply_only_payload``, which no preview or audit surface shows.
    """
    records, errors, timezone = _read_upload_csv(csv_path)
    if errors:
        return [], errors, [], []

    _, header = records[0]
    col, errors = _column_map(header, _EXPECTED_CALL_HEADERS)
    if errors:
        return [], errors, [], []

    now = datetime.now(_tz.utc)
    out: list[dict] = []
    skipped: list[dict] = []
    advisories: list[str] = []
    for source_line, raw in records[1:]:
        if _short_row(raw, col):
            skipped.append({
                "row": source_line,
                "reason": (
                    f"short row: {len(raw)} of {len(header)} columns — it ends "
                    "before the last required column"
                ),
            })
            continue
        raw = _pad_to_header(raw, len(header))
        value, currency, problem = _parse_amount(
            raw[col["Conversion Value"]], raw[col["Conversion Currency"]]
        )
        if problem:
            skipped.append({"row": source_line, "reason": problem})
            continue

        call_start, problem = _parse_timestamp(raw[col["Call Start Time"]], timezone)
        if problem:
            skipped.append({
                "row": source_line, "reason": f"Call Start Time {problem}"
            })
            continue
        converted, problem = _parse_timestamp(raw[col["Conversion Time"]], timezone)
        if problem:
            skipped.append({
                "row": source_line, "reason": f"Conversion Time {problem}"
            })
            continue

        start_dt = datetime.fromisoformat(call_start)
        converted_dt = datetime.fromisoformat(converted)
        if converted_dt < start_dt:
            skipped.append({
                "row": source_line,
                "reason": "Conversion Time is before Call Start Time",
            })
            continue
        if converted_dt > now:
            skipped.append({
                "row": source_line,
                "reason": "Conversion Time is in the future",
            })
            continue

        # A bare date parses to midnight, which is rarely what the export
        # meant: say so instead of quietly shifting the conversion by hours.
        for label in ("Call Start Time", "Conversion Time"):
            if not _has_time_component(raw[col[label]]):
                advisories.append(
                    f"Row {source_line}: {label} carries no time — midnight "
                    "was assumed"
                )

        raw_caller = raw[col["Caller's Phone Number"]]
        out.append({
            "source_line": source_line,
            "caller_was_given": bool((raw_caller or "").strip()),
            "caller_id": _normalize_phone_e164(raw_caller, default_region),
            "call_start_time": call_start,
            "conversion_name": raw[col["Conversion Name"]].strip(),
            "conversion_time": converted,
            "conversion_value": value,
            "currency_code": currency,
        })
    return out, errors, advisories, skipped


def _redact_caller_id(caller_id: str) -> str:
    """Mask an E.164 phone for display/logging: the country code plus the last
    two digits. e.g. '+14155550142' -> '+1***42'.

    Two trailing digits are enough to sanity-check that the right number was
    parsed. Everything between them is what would make the hint worth
    attacking, and this string reaches the preview, the model's context and the
    audit log.
    """
    s = (caller_id or "").strip()
    if not s:
        return ""
    if not s.startswith("+") or len(s) < 5:
        return "***"
    return f"+{_dialling_country_code(s)}***{s[-2:]}"


def _dialling_country_code(e164: str) -> str:
    """The dialling country code of an E.164 number, "" when it is unknown.

    libphonenumber knows where the country code ends (`+1` vs `+49` vs `+49`
    inside `+49151…`); a hand-rolled slice cannot. Failing to resolve it only
    costs the prefix of a display hint, so this never raises.
    """
    try:
        import phonenumbers

        return str(phonenumbers.parse(e164, None).country_code or "")
    except Exception:  # noqa: BLE001 — display helper, never fatal
        return ""


def draft_upload_call_conversions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    default_region: str = "",
    consent: dict | None = None,
) -> dict:
    """Draft an upload of call conversions from CSV — returns a PREVIEW.

    Reads any CSV matching Google Ads' call-upload schema and previews what
    would be sent to ConversionUploadService.UploadCallConversions.

    Required CSV columns: Caller's Phone Number, Call Start Time, Conversion
    Name, Conversion Time, Conversion Value, Conversion Currency. An optional
    ``Parameters:TimeZone=...`` row is read, not ignored: it resolves
    timestamps that carry no offset of their own.

    The ``Conversion Name`` value MUST exactly match an existing conversion
    action whose type is UPLOAD_CALLS — checked against the account here, so a
    typo fails the preview rather than the upload.

    Rows whose ``caller_id`` is empty or not E.164 are skipped and listed in
    ``skipped_rows``; they could never match. Batches of 2,000 rows are sent
    one request at a time, with partial failure always on (Google requires it),
    and the result carries a per-batch ledger.

    ``consent`` (GDPR/EEA): a dict like
    ``{"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}``. Values:
    GRANTED / DENIED / UNSPECIFIED. Required for EEA traffic. Defaults to
    UNSPECIFIED when omitted.

    PII note: the caller phone number is required raw by Google for matching,
    so the rows live in the plan's ``apply_only_payload`` — apply needs them,
    and neither the preview, the dry-run response nor the audit log ever see
    them. The preview shows counts and redacted sample rows. Call
    confirm_and_apply with the returned plan_id.
    """
    from adloop.safety.preview import ChangePlan, store_plan

    default_region, consent_norm, error = _upload_draft_preflight(
        config,
        operation="upload_call_conversions",
        default_region=default_region,
        consent=consent,
    )
    if error:
        return error

    rows, parse_advisories, skipped, error = _upload_parse_result(
        *_parse_call_conversion_csv(csv_path, default_region),
        empty_message="CSV contained zero conversion rows",
    )
    if error:
        return error

    # A call upload without a usable E.164 caller id cannot match anything —
    # Google fails such a row. Report it here instead of uploading a no-op.
    usable: list[dict] = []
    for row in rows:
        caller = (row.get("caller_id") or "").strip()
        if caller.startswith("+"):
            usable.append(row)
            continue
        skipped.append({
            "row": row.get("source_line"),
            "reason": (
                "caller_id is not a valid E.164 number — add a country code, "
                "or pass default_region for national formats"
                if row.get("caller_was_given")
                else "caller_id is empty"
            ),
        })
    if not usable:
        return {
            "error": (
                "No row carries a usable E.164 caller_id. Nothing was planned — "
                "check the numbers, or pass default_region for national "
                "formats."
            ),
            "skipped_rows": skipped,
        }
    rows = usable

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value, totals_by_currency, currency_hint, value_warnings = (
        _value_summary(rows)
    )
    rows_without_value = sum(
        1 for r in rows if r["conversion_value"] is None
    )
    rows_without_currency = sum(1 for r in rows if not r["currency_code"])

    # Validate the action names against the account now, not after the upload
    # ran: a typo in the CSV should not cost a confirmed plan. The resource
    # names travel in the plan, so apply does not query a second time.
    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    try:
        action_resources = _resolve_upload_action(
            get_ads_client(config), cid, distinct_actions,
            expected_type="UPLOAD_CALLS",
            lines=_action_first_lines(rows),
        )
    except ValueError as e:
        return {"error": str(e)}

    # Freeze the exact rows apply will upload (caller_id RAW — required by
    # Google). They go into the plan's apply-only payload, which no preview or
    # audit surface shows; the audit log gets the summary below.
    frozen_rows = [
        {
            "source_line": r.get("source_line"),
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
        # Signal only: on the Google path nothing enforces this flag — it tells
        # the model the upload cannot be undone. The enforced brake is
        # ``safety.two_phase_apply``.
        requires_double_confirm=True,
        changes={
            "row_count": len(rows),
            "total_value": total_value,
            # Only meaningful when every row shares one currency; the warning
            # below says so when they do not.
            "total_value_by_currency": totals_by_currency,
            "currency_hint": currency_hint,
            "value_warnings": value_warnings,
            # Blank stays blank: the field is left unset so Google falls back to
            # the conversion action's default instead of an invented currency.
            "rows_without_value": rows_without_value,
            "rows_without_currency": rows_without_currency,
            "skipped_count": len(skipped),
            "skipped_rows": skipped,
            # Say it in prose too: the count is easy to miss in a JSON blob.
            **({"skipped_note": (
                f"{len(skipped)} row(s) from the CSV are not uploaded — see "
                "skipped_rows for the line and the reason."
            )} if skipped else {}),
            "distinct_conversion_actions": distinct_actions,
            # Resolved at draft time; apply reads them instead of querying again.
            "conversion_actions": action_resources,
            "consent": consent_norm,
            "parse_warnings": parse_advisories,
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
        # RAW caller_id lives here (apply needs it, Google cannot hash it);
        # a preview never shows `apply_only_payload`.
        apply_only_payload={"rows": frozen_rows},
    )
    store_plan(plan)
    return plan.to_preview()


def _resolve_upload_action(
    client: object,
    cid: str,
    names: list[str],
    *,
    expected_type: str,
    lines: dict[str, object] | None = None,
) -> dict[str, str]:
    """Map conversion-action names to resource names, enforcing the type.

    Both uploads look actions up by the name in the CSV's ``Conversion Name``
    column, which is why this is validated at draft time: a typo would
    otherwise surface only after the upload ran. The resource names are stored
    in the plan, so apply does not query again.

    A name that is *not* found is reported by count and CSV line, never by
    value: a shifted column puts anything into that cell, and an error message
    travels into the preview and the audit log. Names that were found come from
    the account (``ca.name``) and may be echoed.

    ``UPLOAD_CALLS`` for call uploads, ``UPLOAD_CLICKS`` for Enhanced
    Conversions for Leads (which layers identifier matching on top of click
    conversions).
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
    wrong_type: list[str] = []
    for row in response:
        ca = row.conversion_action
        ca_type = ca.type_.name if hasattr(ca.type_, "name") else str(ca.type_)
        if ca_type != expected_type:
            wrong_type.append(f"{ca.name} (type={ca_type})")
            continue
        mapping[ca.name] = ca.resource_name

    if wrong_type:
        raise ValueError(
            f"Conversion action(s) are not of type {expected_type}, which this "
            f"upload requires: {wrong_type}. Use "
            f"draft_create_conversion_action(type_='{expected_type}', ...) or an "
            "existing action of that type."
        )
    missing = [n for n in names if n not in mapping]
    if missing:
        where = [lines.get(n) for n in missing] if lines else []
        raise ValueError(
            f"{len(missing)} conversion action name(s) from the 'Conversion "
            f"Name' column were not found (CSV lines: {where}). Verify the "
            "column matches existing action names exactly, or read the "
            "account's actions to compare."
        )
    return mapping


def _action_first_lines(rows: list[dict]) -> dict[str, object]:
    """The first CSV line each conversion-action name appears on."""
    lines: dict[str, object] = {}
    for index, row in enumerate(rows, start=1):
        lines.setdefault(row["conversion_name"], _source_line(row, index))
    return lines


def _value_summary(
    rows: list[dict],
) -> tuple[float, dict[str, float], str, list[str]]:
    """Total value, the same total per currency, a hint and a warning.

    ``total_value`` adds every row that carries a value. That number only means
    something when the rows share a currency, so the breakdown travels with it
    and a file that mixes EUR and USD is called out instead of being summed
    into a figure nobody can act on.
    """
    by_currency: dict[str, float] = {}
    codes: set[str] = set()
    for row in rows:
        if row.get("conversion_value") is None:
            continue
        # A blank currency cell means "the conversion action's currency".
        code = row.get("currency_code") or ""
        codes.add(code)
        key = code or "account default"
        by_currency[key] = round(
            by_currency.get(key, 0.0) + row["conversion_value"], 2
        )

    total = round(sum(by_currency.values()), 2)
    # The hint is only worth having when every row agrees on one real code.
    hint = next(iter(codes)) if len(codes) == 1 else ""
    warnings: list[str] = []
    if len(by_currency) > 1:
        warnings.append(
            f"The rows carry {len(by_currency)} different currencies "
            f"({', '.join(sorted(by_currency))}); total_value adds them up and "
            "is not a meaningful amount. Use total_value_by_currency."
        )
    return total, by_currency, hint, warnings


def _upload_draft_preflight(
    config: AdLoopConfig,
    *,
    operation: str,
    default_region: str,
    consent: dict | None,
) -> tuple[str, dict | None, dict | None]:
    """The guards both upload drafts run before they touch the CSV.

    Returns ``(default_region, consent, error_response)`` with the region
    normalised and the consent validated. Both drafts live on a local file, so
    the server-mode refusal sits here too.
    """
    from adloop.runtime import deployment_mode
    from adloop.safety.guards import SafetyViolation, check_blocked_operation

    if deployment_mode() == "server":
        return "", None, {
            "error": (
                "This tool uploads conversions from a CSV file on the machine "
                "running AdLoop and is not available on the hosted server. "
                "Use the self-hosted AdLoop MCP server for conversion uploads."
            )
        }

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return "", None, {"error": str(e)}

    try:
        consent_norm = _consent_from_param(consent)
    except ValueError as e:
        return "", None, {"error": str(e)}

    region = (default_region or "").strip().upper()
    if region and not re.fullmatch(r"[A-Z]{2}", region):
        return "", None, {
            "error": (
                "default_region must be a two-letter ISO country code "
                "(e.g. 'DE') or empty"
            )
        }
    return region, consent_norm, None


def _upload_parse_result(
    rows: list[dict],
    errors: list[str],
    advisories: list[str],
    dropped: list[dict],
    *,
    empty_message: str,
) -> tuple[list[dict], list[str], list[dict], dict | None]:
    """Turn a parser's return value into ``(rows, advisories, skipped, error)``."""
    if errors:
        return [], [], [], {"error": "CSV parse failed", "details": errors}
    if not rows:
        return [], [], [], {
            "error": empty_message,
            **({"skipped_rows": dropped} if dropped else {}),
        }
    return rows, advisories, list(dropped), None


# Google rejects a single upload request above 2,000 conversions with
# TOO_MANY_CONVERSIONS_IN_REQUEST, so a CSV larger than that is split here.
_MAX_ROWS_PER_REQUEST = 2000

# Did the current apply already put a request on the wire? `confirm_and_apply`
# asks this instead of guessing from the exception type, so a failure anywhere
# before the first request keeps the plan no matter what raised.
_SEND_STATE = threading.local()


def reset_send_state() -> None:
    """Mark the current thread as "nothing sent yet" (start of an apply)."""
    _SEND_STATE.sent = False


def sent_anything() -> bool:
    """Has this thread sent a conversion request since the last reset?"""
    return bool(getattr(_SEND_STATE, "sent", False))


def _mark_sent() -> None:
    _SEND_STATE.sent = True


class UploadNotSentError(RuntimeError):
    """A batch failed before anything left the process.

    Only this error means "nothing was sent": a missing payload, a proto that
    could not be built. Everything else that happens once the request is on its
    way leaves the rows' fate unknown, so the plan must not be retried blindly.
    """


class PartialUploadError(RuntimeError):
    """An upload stopped after some batches had already gone through.

    Carrying the ledger as data (not only inside the message) is what lets
    ``confirm_and_apply`` retire the plan: a second confirm after a partial
    failure would upload the finished batches again, and call conversions have
    no dedup key to absorb that.
    """

    def __init__(
        self,
        message: str,
        *,
        batches: list[dict],
        sent_total: int,
        resume_from_line: object,
        row_errors: list[dict] | None = None,
        unknown_status: bool = False,
        uncertain_lines: list[int] | None = None,
        uncertain_rows: int = 0,
    ) -> None:
        super().__init__(message)
        self.batches = batches
        self.sent_total = sent_total
        self.resume_from_line = resume_from_line
        self.row_errors = row_errors or []
        # A transport failure is not a rejection: the batch may have reached
        # Google, so the caller must not simply send it again.
        self.unknown_status = unknown_status
        # Inclusive [first, last] of the batch whose fate is unknown, so a
        # caller can check exactly those rows instead of a bare "resume from"
        # hint. Same shape as first_source_line/last_source_line in the ledger.
        self.uncertain_lines = uncertain_lines or []
        # How many rows went into that batch. Lines can carry comments or be
        # non-contiguous, so the count is carried instead of derived from the
        # line span.
        self.uncertain_rows = uncertain_rows


def _row_errors_from_failure(
    client: object, failure: object, chunk: list[dict], *, batch: int, offset: int
) -> list[dict]:
    """Turn Google's per-conversion errors into per-row messages.

    Google reports ``conversions[i]`` — an index into the request, not into the
    file. Mapping it back to the row's ``source_line`` is the difference between
    "batch 2 failed" and "line 1234 failed: invalid conversion action".
    """
    from adloop.ads.write import _parse_partial_failure_per_op

    per_op = _parse_partial_failure_per_op(client, failure)
    out: list[dict] = []
    for index, message in sorted(per_op.items()):
        if 0 <= index < len(chunk):
            line = _source_line(chunk[index], offset + index + 1)
        else:
            line = None
        out.append({"batch": batch, "line": line, "error": message})
    return out


def _source_line(row: dict, fallback: int) -> object:
    """The CSV line a row came from, or a positional fallback for hand-built plans."""
    return row.get("source_line") or fallback


def _next_batch_first_line(rows: list[dict], next_index: int) -> object:
    """The source line of the row at ``next_index``, or ``None`` if there is none.

    A resume hint must name a row that is really still to be sent. Adding one
    to the previous batch's last line would land inside that record when a
    quoted field spans several lines, on a comment line, or past the end of the
    file when the failing batch was the last one.
    """
    if next_index >= len(rows):
        return None
    return _source_line(rows[next_index], next_index + 1)


def _failure_from(
    exc: Exception,
    *,
    index: int,
    batch_total: int,
    first_line: object,
    last_line: object,
    resume_line: object,
    done: int,
    completed_batches: int,
    chunk: list[dict],
    start: int,
    client: object,
    ledger: list[dict],
    dry_run: bool,
    row_errors: list[dict],
) -> PartialUploadError:
    """Turn a failed request into the right ``PartialUploadError``.

    A rejection (Google answered) and a transport failure (it may not have) are
    different facts, and the caller needs to see which one it got.
    """
    from google.ads.googleads.errors import GoogleAdsException

    from adloop.ads.validate_only import ValidateOnlyFailure
    from adloop.ads.write import _extract_error_message

    rejected = isinstance(exc, (GoogleAdsException, ValidateOnlyFailure))
    # A rejection is an answer: only a transport-level failure leaves the
    # batch's fate open, and a dry run never sent anything.
    unknown = (not rejected) and not dry_run
    detail = _extract_error_message(exc)
    failure = getattr(exc, "failure", None)
    batch_row_errors = (
        _row_errors_from_failure(
            client, failure, chunk, batch=index + 1, offset=start
        )
        if failure is not None
        else []
    )

    if dry_run:
        message = (
            f"Validation failed in batch {index + 1} of {batch_total} "
            f"(CSV lines {first_line}-{last_line}): {detail} Nothing was "
            "uploaded — a dry run only validates."
        )
    elif rejected:
        if done:
            tail = (
                f"{done} row(s) from {completed_batches} batch(es) were already "
                f"sent and must not be sent again — resume the CSV at line "
                f"{first_line}."
            )
        else:
            tail = (
                "Nothing was uploaded — fix those rows and draft the file "
                "again."
            )
        message = (
            f"Upload failed in batch {index + 1} of {batch_total} (CSV lines "
            f"{first_line}-{last_line}): {detail} {tail}"
        )
    else:
        rest = (
            f"the remaining rows can be drafted from line {resume_line}."
            if resume_line is not None
            else "no rows remain after these."
        )
        message = (
            f"Batch {index + 1} of {batch_total} (CSV lines {first_line}-"
            f"{last_line}) failed with an unknown outcome: {detail} Those lines "
            "may or may not have been received — check the conversion action "
            "for them before anything else; do not resend them "
            f"unconditionally. {done} row(s) from earlier batches were sent and "
            f"answered — the batch ledger shows how many matched — and {rest}"
        )

    return PartialUploadError(
        message,
        batches=ledger,
        sent_total=done,
        # For an unknown outcome the line to resume from is the first line of
        # the *next* batch: the uncertain batch itself has to be checked first,
        # so pointing at its first line would invite a duplicate, and there is
        # nothing left to draft when no batch follows.
        resume_from_line=resume_line if unknown else first_line,
        row_errors=row_errors + batch_row_errors,
        unknown_status=unknown,
        # Inclusive, like ``first_source_line``/``last_source_line`` in the
        # ledger: a half-open interval in a field a model reads invites
        # off-by-one mistakes.
        uncertain_lines=[first_line, last_line] if unknown else [],
        uncertain_rows=len(chunk) if unknown else 0,
    )


def _upload_in_batches(
    rows: list[dict], build, send, *, dry_run: bool = False, client: object = None
) -> dict:
    """Send ``rows`` in API-sized batches and report progress per batch.

    ``build(chunk)`` turns rows into protos and ``send(payload)`` performs the
    request. Keeping them apart matters for the error contract: a build failure
    provably sent nothing, while anything after the request leaves the batch's
    fate unknown.

    Row numbers are the physical CSV lines the rows came from, so a message
    means the same thing to whoever edits the file. A failure in batch 3 leaves
    batches 1-2 already sent, so the error says which lines are already in: calls
    have no dedup key at all, and click uploads only dedupe on an order id, so
    a blind retry double-counts whatever went through. In a dry run nothing was
    sent and the error says so instead.
    """
    total = len(rows)
    batch_total = (total + _MAX_ROWS_PER_REQUEST - 1) // _MAX_ROWS_PER_REQUEST
    ledger: list[dict] = []
    row_errors: list[dict] = []

    for index in range(batch_total):
        start = index * _MAX_ROWS_PER_REQUEST
        chunk = rows[start:start + _MAX_ROWS_PER_REQUEST]
        # Positional fallbacks first, so an error message still has a line to
        # name even if the lookup itself is what fails.
        first_line: object = start + 1
        last_line: object = start + len(chunk)
        next_line: object = None
        # Phase 1: everything up to the request. For the FIRST batch nothing has
        # left the process, so a failure is an explicit "not sent" and the plan
        # stays retryable. Later batches are a different story: the earlier ones
        # are already in the account, so a retry would send those again — that is
        # a partial upload, and the plan has to be retired. The slicing and line
        # lookup sit inside the try for the same reason: they happen before any
        # request, and an exception there must not look like a sent batch.
        try:
            first_line = _source_line(chunk[0], start + 1)
            last_line = _source_line(chunk[-1], start + len(chunk))
            # Where the next batch begins, for a resume hint that names a row
            # that is really still to be sent.
            next_line = _next_batch_first_line(rows, start + len(chunk))
            payload = build(chunk)
        except Exception as exc:  # noqa: BLE001 — re-raised per the ledger
            done = sum(batch["sent"] for batch in ledger)
            if index == 0 or dry_run:
                raise UploadNotSentError(
                    f"Batch {index + 1} of {batch_total} could not be built "
                    f"(CSV lines {first_line}-{last_line}): {exc} Nothing was "
                    "uploaded."
                ) from exc
            # This batch provably did not go out, so the rows to resume at are
            # its own first line — unlike a transport failure, where the batch
            # itself is uncertain and has to be checked first.
            raise PartialUploadError(
                f"Batch {index + 1} of {batch_total} (CSV lines {first_line}-"
                f"{last_line}) could not be built: {exc} {done} row(s) from "
                f"{len(ledger)} earlier batch(es) were already sent and must not be "
                f"sent again — resume the CSV at line {first_line}.",
                batches=ledger,
                sent_total=done,
                resume_from_line=first_line,
                row_errors=row_errors,
                unknown_status=False,
            ) from exc

        # Phase 2: send it. From here on the batch's fate is Google's.
        # The marker is set first: from this line on, a failure may mean the
        # request arrived, and the caller must not offer a blind retry.
        _mark_sent()
        try:
            response = send(payload)
        except Exception as exc:  # noqa: BLE001 — re-raised with the ledger
            raise _failure_from(
                exc,
                index=index,
                batch_total=batch_total,
                first_line=first_line,
                last_line=last_line,
                resume_line=next_line,
                done=sum(batch["sent"] for batch in ledger),
                completed_batches=len(ledger),
                chunk=chunk,
                start=start,
                client=client,
                ledger=ledger,
                dry_run=dry_run,
                row_errors=row_errors,
            ) from exc

        # Phase 3: read the result. The rows are uploaded by now, so an error
        # here must not make the plan look retryable.
        try:
            results = list(response.results)
            # Google populates the result row's ``conversion_action`` only for
            # rows that actually matched; echoed identifiers come back for
            # failed rows too, so they are not a success signal.
            success = sum(
                1 for r in results if getattr(r, "conversion_action", "")
            )
            ledger.append({
                "batch": index + 1,
                "first_source_line": first_line,
                "last_source_line": last_line,
                # `sent` is what left the process; `accepted` is what Google
                # matched. A dry run answers with placeholder results and no
                # matching information, so its counts stay unknown instead of
                # pretending every row failed.
                "sent": len(payload),
                "accepted": None if dry_run else success,
                "rejected": None if dry_run else len(results) - success,
            })
            partial = getattr(response, "partial_failure_error", None)
            if partial and partial.message:
                row_errors.append({
                    "type": "partial_failure",
                    "batch": index + 1,
                    "message": partial.message,
                    "code": getattr(partial, "code", None),
                })
                row_errors.extend(
                    _row_errors_from_failure(
                        client, partial, chunk, batch=index + 1, offset=start
                    )
                )
        except Exception as exc:  # noqa: BLE001 — rows are already uploaded
            raise PartialUploadError(
                f"Batch {index + 1} of {batch_total} (CSV lines "
                f"{first_line}-{last_line}) was sent, but its result could not "
                f"be read: {exc} The rows may have been received — check the "
                "conversion action before resending them.",
                batches=ledger,
                sent_total=sum(batch["sent"] for batch in ledger),
                resume_from_line=next_line,
                row_errors=row_errors,
                unknown_status=True,
                uncertain_lines=[first_line, last_line],
                uncertain_rows=len(chunk),
            ) from exc

    sent_total = sum(batch["sent"] for batch in ledger)
    accepted_total = (
        None if dry_run else sum(batch["accepted"] for batch in ledger)
    )
    return {
        "sent_total": sent_total,
        "accepted_total": accepted_total,
        "rejected_total": None if dry_run else sent_total - accepted_total,
        "batch_count": len(ledger),
        "batches": ledger,
        "row_errors": row_errors,
    }


def _prepare_upload(
    client: object, changes: dict
) -> tuple[list[dict], dict, dict | None, object]:
    """Resolve everything an upload needs before its first request goes out.

    Client, service, payload and conversion-action lookup all happen here, and
    every failure is reported as ``UploadNotSentError``: at this point no
    request has left the process, so the caller must keep the plan and let the
    caller retry instead of retiring a plan that could still be applied.
    """
    try:
        rows = changes.get("rows") or []
        if not rows:
            expected = int(changes.get("row_count") or 0)
            if expected:
                # row_count > 0 without the payload means the plan store dropped
                # ``apply_only_payload``. Fail loudly; an empty upload that
                # reports success is the one outcome nobody would notice.
                raise UploadNotSentError(
                    f"This plan expects {expected} row(s) but carries none: the "
                    "plan store did not persist ChangePlan.apply_only_payload. "
                    "Nothing was uploaded — draft the upload again."
                )
            return [], {}, None, None
        return (
            rows,
            changes["conversion_actions"],
            changes.get("consent"),
            client.get_service("ConversionUploadService"),
        )
    except UploadNotSentError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised as "not sent"
        raise UploadNotSentError(
            "The upload could not be prepared, so nothing was sent and the "
            f"plan is still usable: {exc}"
        ) from exc


def _apply_upload_call_conversions(
    client: object, cid: str, changes: dict
) -> dict:
    """Execute the call-conversion upload via ConversionUploadService.

    Builds the upload protos from the frozen rows (``apply_only_payload``, put
    there at preview time) — the CSV is NOT re-read. Batches are sent one
    request at a time; a failure says which rows are already uploaded.
    """
    rows, action_resources, consent, upload_service = _prepare_upload(client, changes)
    if not rows:
        return {"error": "Plan contained zero call-conversion rows"}

    def _build(chunk: list[dict]):
        payload: list = []
        for r in chunk:
            cc = client.get_type("CallConversion")
            cc.caller_id = r["caller_id"]
            cc.call_start_date_time = r["call_start_time"]
            cc.conversion_action = action_resources[r["conversion_name"]]
            cc.conversion_date_time = r["conversion_time"]
            if r.get("conversion_value") is not None:
                cc.conversion_value = float(r["conversion_value"])
            if r.get("currency_code"):
                cc.currency_code = r["currency_code"]
            _apply_consent(client, cc, consent)
            payload.append(cc)
        return payload

    def _send(payload: list):
        return upload_service.upload_call_conversions(
            customer_id=cid,
            conversions=payload,
            # The API requires partial failure on uploads; leaving rows out of
            # the request because one is malformed would be worse.
            partial_failure=True,
        )

    ledger = _upload_in_batches(
        rows,
        _build,
        _send,
        dry_run=bool(getattr(client, "is_validate_only", False)),
        client=client,
    )
    ledger["conversion_actions_used"] = action_resources
    return ledger


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
    # Google's identifier list spells the address out: first name, last name,
    # country code and postal code belong together. Country and postal travel
    # as plain values (only the names are hashed).
    "Postal Code",
    "Country Code",
]


def _parse_ec_for_leads_csv(
    csv_path: str, default_region: str = ""
) -> tuple[list[dict], list[str], list[str], list[dict]]:
    """Parse the EC-for-Leads CSV (local file) and hash PII at parse time.

    Required columns: Email, Phone Number, First Name, Last Name,
    Conversion Name, Conversion Time, Conversion Value, Conversion Currency.
    Optional: Order ID (Google's dedup key — strongly recommended so
    re-uploads of the same source row don't double-count), Country Code,
    Postal Code. Returns ``(rows, errors, advisories, skipped)``: ``errors``
    are file-level problems, ``advisories`` describe rows that still upload, and
    ``skipped`` lists every row that will not be uploaded with its
    ``source_line``. A record that ends before the last required column is
    skipped instead of aborting the draft.

    The Email / Phone Number / First Name / Last Name columns hold RAW PII.
    Each is normalized (email→trim+lowercase, phone→E.164, names→trim+
    lowercase) and then SHA-256-hashed here. Returned rows contain ONLY the
    hashes (``*_sha256`` keys) plus non-PII fields — the raw values never
    leave this function.
    """
    records, errors, timezone = _read_upload_csv(csv_path)
    if errors:
        return [], errors, [], []

    _, raw_header = records[0]
    col, errors = _column_map(raw_header, _EXPECTED_EC_HEADERS)
    if errors:
        return [], errors, [], []
    header = [cell.strip() for cell in raw_header]
    optional_col = {n: header.index(n) for n in _OPTIONAL_EC_HEADERS if n in header}

    out: list[dict] = []
    skipped: list[dict] = []
    advisories: list[str] = []
    for source_line, raw in records[1:]:
        if _short_row(raw, col):
            skipped.append({
                "row": source_line,
                "reason": (
                    f"short row: {len(raw)} of {len(header)} columns — it ends "
                    "before the last required column"
                ),
            })
            continue
        # Optional columns may legitimately be missing: pad so that every index
        # the header promises exists (Country Code and Postal Code used to
        # raise IndexError here).
        raw = _pad_to_header(raw, len(header))
        value, currency, problem = _parse_amount(
            raw[col["Conversion Value"]], raw[col["Conversion Currency"]]
        )
        if problem:
            skipped.append({"row": source_line, "reason": problem})
            continue
        order_id = ""
        if "Order ID" in optional_col:
            order_id = raw[optional_col["Order ID"]].strip()
        # Normalize THEN hash. Raw values are discarded immediately.
        raw_email = raw[col["Email"]]
        email_norm = _normalize_email(raw_email)
        raw_phone = raw[col["Phone Number"]]
        phone_norm = _normalize_phone_e164(raw_phone, default_region)
        # A phone that is not E.164 hashes to a value Google can never match —
        # sending it would only pad the payload. Keep the row (email/address
        # may still match) but drop the identifier and say so.
        phone_usable = phone_norm.startswith("+")
        first_norm = _normalize_name(raw[col["First Name"]])
        last_norm = _normalize_name(raw[col["Last Name"]])

        def _optional(name: str) -> str:
            return raw[optional_col[name]].strip() if name in optional_col else ""

        converted_time, problem = _parse_timestamp(
            raw[col["Conversion Time"]], timezone
        )
        if problem:
            skipped.append({
                "row": source_line, "reason": f"Conversion Time {problem}"
            })
            continue
        if datetime.fromisoformat(converted_time) > datetime.now(_tz.utc):
            skipped.append({
                "row": source_line,
                "reason": "Conversion Time is in the future",
            })
            continue
        if not _has_time_component(raw[col["Conversion Time"]]):
            # A bare date parses to midnight; say so instead of shifting the
            # conversion by hours without a word.
            advisories.append(
                f"Row {source_line}: Conversion Time carries no time — "
                "midnight was assumed"
            )

        country = _optional("Country Code").upper()
        postal = _optional("Postal Code").strip()
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            # "Germany" instead of "DE" must not cost the whole row: drop the
            # address fragment, keep whatever else identifies the lead, and say
            # what was dropped. Only a row left without any identifier is
            # skipped, and that happens in the draft where it is reported as
            # skipped_rows rather than as a parse warning.
            advisories.append(
                f"Row {source_line}: Country Code must be a two-letter ISO "
                "code — the address was dropped for this row"
            )
            country = ""
            postal = ""
        out.append({
            "source_line": source_line,
            "email_sha256": _sha256_hex(email_norm),
            "email_was_given": bool((raw_email or "").strip()),
            "phone_sha256": _sha256_hex(phone_norm) if phone_usable else "",
            "phone_was_given": bool((raw_phone or "").strip()),
            "phone_usable": phone_usable,
            "first_name_sha256": _sha256_hex(first_norm),
            "last_name_sha256": _sha256_hex(last_norm),
            "postal_code": postal,
            "country_code": country,
            "conversion_name": raw[col["Conversion Name"]].strip(),
            "conversion_time": converted_time,
            "conversion_value": value,
            "currency_code": currency,
            "order_id": order_id,
        })
    return out, errors, advisories, skipped


def _has_complete_address(row: dict) -> bool:
    """The address identifier Google documents: names + country + postal code."""
    return bool(
        row["first_name_sha256"]
        and row["last_name_sha256"]
        and row["postal_code"]
        and row["country_code"]
    )


def _match_warnings(
    names_without_address: list[int],
    address_without_names: list[int],
    unusable_phones: list[int],
    unusable_emails: list[int],
) -> list[str]:
    """Warnings about identifiers that will not match, said up front.

    Rows listed here are still uploaded — on whatever identifier they do have —
    so the preview has to say what was left out instead of counting it as sent.
    Each case is phrased for what the caller actually supplied: a lead export
    with names and an email but no address columns has not sent a broken
    address, it simply has none.
    """
    warnings: list[str] = []
    if names_without_address:
        warnings.append(
            f"{len(names_without_address)} row(s) have first and last name but "
            "no country code or postal code. Names are sent only together with "
            "both — add a 'Country Code' and a 'Postal Code' column to use "
            f"them; first affected rows: {names_without_address[:5]}."
        )
    if address_without_names:
        warnings.append(
            f"{len(address_without_names)} row(s) have a country code and/or a "
            "postal code but no first and last name, so the address is not "
            "sent — the address identifier needs all four fields; first "
            f"affected rows: {address_without_names[:5]}."
        )
    if unusable_phones:
        warnings.append(
            f"{len(unusable_phones)} row(s) carry a phone number that is not a "
            "valid E.164 number, so the hashed value cannot match. Those rows "
            "are uploaded without the phone identifier — add a country code, "
            "or pass default_region for national formats; first affected rows: "
            f"{unusable_phones[:5]}."
        )
    if unusable_emails:
        warnings.append(
            f"{len(unusable_emails)} row(s) carry an email that is not an "
            "address (no '@', or an empty side), so the hashed value cannot "
            "match. Those rows are uploaded without the email identifier; "
            f"first affected rows: {unusable_emails[:5]}."
        )
    return warnings


def draft_upload_enhanced_conversions_for_leads(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    default_region: str = "",
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

    Optional columns: ``Order ID`` (Google's dedup key for ClickConversion —
    without it, re-uploads double-count matched conversions), ``Country Code``
    and ``Postal Code`` (sent plain in the address identifier — Enhanced
    Conversions for Leads matches name-based rows far better with them).

    ``consent`` (GDPR/EEA): a dict like
    ``{"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}``. Values:
    GRANTED / DENIED / UNSPECIFIED. Required for EEA traffic. Defaults to
    UNSPECIFIED when omitted.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.preview import ChangePlan, store_plan

    default_region, consent_norm, error = _upload_draft_preflight(
        config,
        operation="upload_enhanced_conversions_for_leads",
        default_region=default_region,
        consent=consent,
    )
    if error:
        return error

    rows, parse_advisories, skipped, error = _upload_parse_result(
        *_parse_ec_for_leads_csv(csv_path, default_region),
        empty_message="CSV contained zero conversion rows",
    )
    if error:
        return error

    # A row that cannot match would be uploaded to no effect and counted as a
    # success later. Google's identifier list is explicit about what an address
    # identifier is: first name, last name, country code and postal code, hashed
    # names and plain address — a postcode alone identifies nobody. So a row is
    # usable with an email, an E.164 phone, or that complete address.
    usable: list[dict] = []
    for row in rows:
        has_names = bool(row["first_name_sha256"] and row["last_name_sha256"])
        has_address = bool(
            has_names and row["postal_code"] and row["country_code"]
        )
        if row["email_sha256"] or row["phone_sha256"] or has_address:
            usable.append(row)
            continue

        if has_names and (row["postal_code"] or row["country_code"]):
            reason = (
                "address is incomplete — first name, last name, country code "
                "and postal code are needed together for an address identifier"
            )
        elif has_names:
            reason = (
                "only hashed names and no email/phone/address — an address "
                "identifier needs country code and postal code as well"
            )
        elif row["phone_was_given"] or row["email_was_given"]:
            # Both can be broken at once; name every reason, not just the first
            # one, or the caller fixes one and the row is skipped again.
            broken: list[str] = []
            if row["phone_was_given"]:
                broken.append("phone is not a valid E.164 number")
            if row["email_was_given"]:
                broken.append(
                    "email is not an address (no '@', or an empty side)"
                )
            reason = (
                " and ".join(broken) + " — the row has no other identifier"
            )
        elif row["postal_code"] or row["country_code"]:
            reason = (
                "only part of an address (no names) — that cannot match"
            )
        else:
            reason = "no usable identifier (no email, no E.164 phone, no address)"
        skipped.append({"row": row.get("source_line"), "reason": reason})
    if not usable:
        return {
            "error": (
                "No row carries a usable identifier (email, E.164 phone, or a "
                "complete address of names + country + postal code). Nothing "
                "was planned."
            ),
            "skipped_rows": skipped,
        }
    rows = usable

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value, totals_by_currency, currency_hint, value_warnings = (
        _value_summary(rows)
    )
    rows_without_value = sum(
        1 for r in rows if r["conversion_value"] is None
    )
    rows_without_currency = sum(1 for r in rows if not r["currency_code"])

    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    try:
        action_resources = _resolve_upload_action(
            get_ads_client(config), cid, distinct_actions,
            expected_type="UPLOAD_CLICKS",
            lines=_action_first_lines(rows),
        )
    except ValueError as e:
        return {"error": str(e)}

    with_email = sum(1 for r in rows if r["email_sha256"])
    with_phone = sum(1 for r in rows if r["phone_sha256"])
    with_order_id = sum(1 for r in rows if r.get("order_id"))
    # Counted with the same four-field condition the applier sends with: a
    # half address never leaves the process, so it must not show up as one.
    with_address = sum(
        1 for r in rows if _has_complete_address(r)
    )
    unusable_phones = [
        _source_line(r, index)
        for index, r in enumerate(rows, start=1)
        if r["phone_was_given"] and not r["phone_usable"]
    ]
    unusable_emails = [
        _source_line(r, index)
        for index, r in enumerate(rows, start=1)
        if r["email_was_given"] and not r["email_sha256"]
    ]
    names_without_address = [
        _source_line(r, index)
        for index, r in enumerate(rows, start=1)
        if (r["first_name_sha256"] or r["last_name_sha256"])
        and not _has_complete_address(r)
    ]
    address_without_names = [
        _source_line(r, index)
        for index, r in enumerate(rows, start=1)
        if (r["postal_code"] or r["country_code"])
        and not (r["first_name_sha256"] and r["last_name_sha256"])
    ]

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
            "source_line": r.get("source_line"),
            "email_sha256": r["email_sha256"],
            "phone_sha256": r["phone_sha256"],
            "first_name_sha256": r["first_name_sha256"],
            "last_name_sha256": r["last_name_sha256"],
            "postal_code": r["postal_code"],
            "country_code": r["country_code"],
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
        # Signal only, like the call upload — see the note there.
        requires_double_confirm=True,
        changes={
            "row_count": len(rows),
            "total_value": total_value,
            # Only meaningful when every row shares one currency; the warning
            # below says so when they do not.
            "total_value_by_currency": totals_by_currency,
            "currency_hint": currency_hint,
            "value_warnings": value_warnings,
            # Blank stays blank: the field is left unset so Google falls back to
            # the conversion action's default instead of an invented currency.
            "rows_without_value": rows_without_value,
            "rows_without_currency": rows_without_currency,
            "rows_with_email": with_email,
            "rows_with_phone": with_phone,
            "rows_with_order_id": with_order_id,
            "rows_with_address": with_address,
            "skipped_count": len(skipped),
            "skipped_rows": skipped,
            # Say it in prose too: the count is easy to miss in a JSON blob.
            **({"skipped_note": (
                f"{len(skipped)} row(s) from the CSV are not uploaded — see "
                "skipped_rows for the line and the reason."
            )} if skipped else {}),
            "distinct_conversion_actions": distinct_actions,
            # Resolved at draft time; apply reads them instead of querying again.
            "conversion_actions": action_resources,
            "consent": consent_norm,
            "parse_warnings": parse_advisories,
            "dedup_warnings": dedup_warnings,
            "match_warnings": _match_warnings(
                names_without_address,
                address_without_names,
                unusable_phones,
                unusable_emails,
            ),
            "sample_rows": [
                {
                    # A marker, not a prefix: the sample only has to show that
                    # an identifier was there. 16 hex characters are 64 bits,
                    # which is enough to confirm a guessed address from a word
                    # list — in a preview that lands in a model's context and
                    # in the audit log. The full hashes stay in
                    # apply_only_payload, where the upload needs them.
                    "email_sha256": "sha256:set" if r["email_sha256"] else "",
                    "phone_sha256": "sha256:set" if r["phone_sha256"] else "",
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                    "conversion_time": r["conversion_time"],
                    "order_id": r.get("order_id", ""),
                }
                for r in rows[:3]
            ],
        },
        # Hash-only rows, but still apply-only payload: the preview summarises
        # them, and the row set is noise in a model's context.
        apply_only_payload={"rows": frozen_rows},
    )
    store_plan(plan)
    return plan.to_preview()


def _apply_upload_enhanced_conversions_for_leads(
    client: object, cid: str, changes: dict
) -> dict:
    """Execute the EC-for-Leads upload via ConversionUploadService.

    Builds the upload protos from the frozen, already-hashed rows
    (``apply_only_payload``) — the CSV is NOT re-read, so no raw PII is touched
    here. Batched like the call upload.
    """
    rows, action_resources, consent, upload_service = _prepare_upload(client, changes)
    if not rows:
        return {"error": "Plan contained zero EC-for-leads rows"}

    def _build(chunk: list[dict]):
        payload: list = []
        for r in chunk:
            cc = client.get_type("ClickConversion")
            cc.conversion_action = action_resources[r["conversion_name"]]
            cc.conversion_date_time = r["conversion_time"]
            if r.get("conversion_value") is not None:
                cc.conversion_value = float(r["conversion_value"])
            if r.get("currency_code"):
                cc.currency_code = r["currency_code"]
            if r.get("order_id"):
                cc.order_id = r["order_id"]
            _apply_consent(client, cc, consent)

            # Hashed identifiers only; Google matches them to logged-in users
            # who clicked the ads. A row with no usable identifier never gets
            # here — the draft reports those instead (see `skipped_rows`).
            if r["email_sha256"]:
                uid = client.get_type("UserIdentifier")
                uid.hashed_email = r["email_sha256"]
                cc.user_identifiers.append(uid)
            if r["phone_sha256"]:
                uid = client.get_type("UserIdentifier")
                uid.hashed_phone_number = r["phone_sha256"]
                cc.user_identifiers.append(uid)
            # Address info only as the complete unit Google documents: first
            # name, last name, country code and postal code together. A row that
            # matched on its email must not carry a half address along — Google
            # would ignore it at best, and could fail the whole conversion at
            # worst.
            if _has_complete_address(r):
                uid = client.get_type("UserIdentifier")
                uid.address_info.hashed_first_name = r["first_name_sha256"]
                uid.address_info.hashed_last_name = r["last_name_sha256"]
                uid.address_info.postal_code = r["postal_code"]
                uid.address_info.country_code = r["country_code"]
                cc.user_identifiers.append(uid)
            payload.append(cc)
        return payload

    def _send(payload: list):
        return upload_service.upload_click_conversions(
            customer_id=cid,
            conversions=payload,
            partial_failure=True,
        )

    ledger = _upload_in_batches(
        rows,
        _build,
        _send,
        dry_run=bool(getattr(client, "is_validate_only", False)),
        client=client,
    )
    ledger["conversion_actions_used"] = action_resources
    return ledger
