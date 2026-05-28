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


# ---------------------------------------------------------------------------
# Call-conversion CSV upload — ConversionUploadService.UploadCallConversions
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
    # Drop fractional seconds if present
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
        # First non-Parameters row is the header
        header: list[str] | None = None
        for raw in rows_iter:
            if not raw:
                continue
            first = (raw[0] or "").strip()
            if first.startswith("Parameters:") or first.startswith("#") or first.startswith("###"):
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
                "caller_id": raw[col["Caller's Phone Number"]].strip(),
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


def draft_upload_call_conversions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    partial_failure: bool = True,
) -> dict:
    """Draft an upload of call conversions from CSV — returns a PREVIEW.

    Reads any CSV matching Google Ads' call-upload schema and previews
    what would be sent to ConversionUploadService.UploadCallConversions.

    The CSV must have columns: Caller's Phone Number, Call Start Time,
    Conversion Name, Conversion Time, Conversion Value, Conversion Currency.
    An optional `Parameters:TimeZone=...` row at the top is ignored.

    The `Conversion Name` value MUST exactly match an existing
    conversion action whose type is UPLOAD_CALLS.

    partial_failure (default True) lets Google accept the rows that
    parse successfully and report only the bad ones — recommended.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("upload_call_conversions", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    rows, parse_errors = _parse_call_conversion_csv(csv_path)
    if parse_errors and not rows:
        return {"error": "CSV parse failed", "details": parse_errors}

    if not rows:
        return {"error": "CSV contained zero conversion rows"}

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value = sum(r["conversion_value"] for r in rows)
    sample = rows[:3]

    plan = ChangePlan(
        operation="upload_call_conversions",
        entity_type="call_conversion_batch",
        entity_id=str(len(rows)),
        customer_id=customer_id,
        changes={
            "csv_path": str(csv_path),
            "row_count": len(rows),
            "total_value": round(total_value, 2),
            "currency_hint": rows[0]["currency_code"] if rows else "USD",
            "distinct_conversion_actions": distinct_actions,
            "partial_failure": bool(partial_failure),
            "parse_warnings": parse_errors,
            "sample_rows": [
                {
                    "caller_id": r["caller_id"],
                    "call_start_time": r["call_start_time"],
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                }
                for r in sample
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
    quoted = ", ".join(f"'{n.replace(chr(39), chr(39) + chr(39))}'" for n in names)
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

    Returns counts of successes / failures plus per-row error details.
    """
    rows, parse_errors = _parse_call_conversion_csv(changes["csv_path"])
    if not rows:
        return {
            "error": "CSV produced zero parseable rows at apply time",
            "parse_errors": parse_errors,
        }

    distinct = sorted({r["conversion_name"] for r in rows})
    action_resources = _resolve_conversion_action_ids(client, cid, distinct)

    conversion_type = client.get_type("CallConversion")
    payload: list = []
    for r in rows:
        cc = conversion_type.__class__()
        cc.caller_id = r["caller_id"]
        cc.call_start_date_time = r["call_start_time"]
        cc.conversion_action = action_resources[r["conversion_name"]]
        cc.conversion_date_time = r["conversion_time"]
        cc.conversion_value = float(r["conversion_value"])
        cc.currency_code = r["currency_code"]
        payload.append(cc)

    upload_service = client.get_service("ConversionUploadService")
    response = upload_service.upload_call_conversions(
        customer_id=cid,
        conversions=payload,
        partial_failure=bool(changes.get("partial_failure", True)),
    )

    results = list(response.results)
    success_count = sum(1 for r in results if r.caller_id)
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
        "parse_warnings": parse_errors,
    }


# ---------------------------------------------------------------------------
# Enhanced Conversions for Leads — UploadClickConversions w/ user_identifiers
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


def _parse_ec_for_leads_csv(csv_path: str) -> tuple[list[dict], list[str]]:
    """Parse the AdLoop EC-for-Leads CSV (already SHA-256 hashed PII)."""
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
            if first.startswith("Parameters:") or first.startswith("#"):
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
            out.append({
                "email_sha256": raw[col["Email"]].strip(),
                "phone_sha256": raw[col["Phone Number"]].strip(),
                "first_name_sha256": raw[col["First Name"]].strip(),
                "last_name_sha256": raw[col["Last Name"]].strip(),
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


def draft_upload_enhanced_conversions_for_leads(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    csv_path: str,
    partial_failure: bool = True,
) -> dict:
    """Draft an Enhanced Conversions for Leads upload — returns PREVIEW.

    Reads any SHA-256-hashed PII CSV matching Google Ads' EC for Leads
    schema and previews what will be pushed via
    ConversionUploadService.UploadClickConversions with user_identifiers
    populated.

    The target conversion action must be of type UPLOAD_CLICKS (EC for
    Leads layers user-identifier matching on top of click conversions).
    Works retroactively — no "action must be created before call" constraint
    like UPLOAD_CALLS has.

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

    rows, parse_errors = _parse_ec_for_leads_csv(csv_path)
    if parse_errors and not rows:
        return {"error": "CSV parse failed", "details": parse_errors}
    if not rows:
        return {"error": "CSV contained zero conversion rows"}

    distinct_actions = sorted({r["conversion_name"] for r in rows})
    total_value = sum(r["conversion_value"] for r in rows)
    with_email = sum(1 for r in rows if r["email_sha256"])
    with_phone = sum(1 for r in rows if r["phone_sha256"])
    sample = rows[:3]

    plan = ChangePlan(
        operation="upload_enhanced_conversions_for_leads",
        entity_type="ec_for_leads_batch",
        entity_id=str(len(rows)),
        customer_id=customer_id,
        changes={
            "csv_path": str(csv_path),
            "row_count": len(rows),
            "total_value": round(total_value, 2),
            "currency_hint": rows[0]["currency_code"] if rows else "USD",
            "rows_with_email": with_email,
            "rows_with_phone": with_phone,
            "distinct_conversion_actions": distinct_actions,
            "partial_failure": bool(partial_failure),
            "parse_warnings": parse_errors,
            "sample_rows": [
                {
                    "email_sha256": r["email_sha256"][:16] + "..."
                    if r["email_sha256"] else "",
                    "phone_sha256": r["phone_sha256"][:16] + "..."
                    if r["phone_sha256"] else "",
                    "conversion_name": r["conversion_name"],
                    "conversion_value": r["conversion_value"],
                    "conversion_time": r["conversion_time"],
                }
                for r in sample
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
    quoted = ", ".join(
        f"'{n.replace(chr(39), chr(39) + chr(39))}'" for n in names
    )
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
    """Execute EC-for-Leads upload via ConversionUploadService."""
    rows, parse_errors = _parse_ec_for_leads_csv(changes["csv_path"])
    if not rows:
        return {
            "error": "CSV produced zero parseable rows at apply time",
            "parse_errors": parse_errors,
        }

    distinct = sorted({r["conversion_name"] for r in rows})
    action_resources = _resolve_upload_clicks_action(client, cid, distinct)

    click_conv_type = client.get_type("ClickConversion")
    user_id_type = client.get_type("UserIdentifier")
    payload: list = []

    for r in rows:
        cc = click_conv_type.__class__()
        cc.conversion_action = action_resources[r["conversion_name"]]
        cc.conversion_date_time = r["conversion_time"]
        cc.conversion_value = float(r["conversion_value"])
        cc.currency_code = r["currency_code"]

        # Build user_identifiers — Google matches the hashed email/phone
        # to logged-in Google users who clicked our ads.
        if r["email_sha256"]:
            uid = user_id_type.__class__()
            uid.hashed_email = r["email_sha256"]
            cc.user_identifiers.append(uid)
        if r["phone_sha256"]:
            uid = user_id_type.__class__()
            uid.hashed_phone_number = r["phone_sha256"]
            cc.user_identifiers.append(uid)
        # First+Last together as address_info (improves match rate)
        if r["first_name_sha256"] and r["last_name_sha256"]:
            uid = user_id_type.__class__()
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
    success_count = sum(
        1 for r in results
        if getattr(r, "conversion_action", "") or getattr(r, "gclid", "")
        or getattr(r, "user_identifiers", None)
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
        "parse_warnings": parse_errors,
    }
