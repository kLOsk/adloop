"""GA4 property configuration reads: the property, its links, streams and retention.

Google's own Analytics MCP server spreads this over several tools that return
raw API objects. Here it is one call with plain values, and insights for the
settings that quietly cost data (two-month retention, a missing Ads link).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_SERVICE_LEVELS = {
    "GOOGLE_ANALYTICS_STANDARD": "standard",
    "GOOGLE_ANALYTICS_360": "360",
}
_RETENTION_MONTHS = {
    "TWO_MONTHS": 2,
    "FOURTEEN_MONTHS": 14,
    "TWENTY_SIX_MONTHS": 26,
    "THIRTY_EIGHT_MONTHS": 38,
    "FIFTY_MONTHS": 50,
}


def iso_time(value) -> str:
    """A protobuf timestamp (datetime-like) as ISO text, '' when unset."""
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def format_customer_id(customer_id: str) -> str:
    digits = str(customer_id or "").replace("-", "")
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits


def _enum_name(value) -> str:
    return getattr(value, "name", "") or (str(value) if value else "")


def _short_error(exc: Exception) -> str:
    message = getattr(exc, "message", "") or str(exc)
    return message.splitlines()[0][:300] if message else type(exc).__name__


def _property_entry(prop) -> dict:
    entry = {
        "property": prop.name,
        "display_name": prop.display_name,
        "time_zone": prop.time_zone,
        "currency_code": prop.currency_code,
        "industry_category": _enum_name(prop.industry_category),
        "service_level": _SERVICE_LEVELS.get(_enum_name(prop.service_level), _enum_name(prop.service_level)),
        "property_type": _enum_name(prop.property_type).removeprefix("PROPERTY_TYPE_").lower(),
        "account": prop.account,
        "create_time": iso_time(prop.create_time),
    }
    if prop.parent and prop.parent != prop.account:
        entry["parent"] = prop.parent
    for field in ("delete_time", "expire_time"):
        if stamp := iso_time(getattr(prop, field, None)):
            entry[field] = stamp
    return entry


def _ads_link_entry(link) -> dict:
    entry = {
        "customer_id": format_customer_id(link.customer_id),
        "can_manage_clients": bool(link.can_manage_clients),
        "create_time": iso_time(link.create_time),
        "resource_name": link.name,
    }
    personalization = link.ads_personalization_enabled
    if personalization is not None:
        entry["ads_personalization_enabled"] = bool(personalization)
    if link.creator_email_address:
        entry["created_by"] = link.creator_email_address
    return entry


def _stream_entry(stream) -> dict:
    entry = {
        "stream": stream.name,
        "type": _enum_name(stream.type_).removesuffix("_DATA_STREAM").lower(),
        "display_name": stream.display_name,
        "create_time": iso_time(stream.create_time),
    }
    if stream.web_stream_data and stream.web_stream_data.measurement_id:
        entry["measurement_id"] = stream.web_stream_data.measurement_id
        entry["default_uri"] = stream.web_stream_data.default_uri
    if stream.android_app_stream_data and stream.android_app_stream_data.package_name:
        entry["package_name"] = stream.android_app_stream_data.package_name
    if stream.ios_app_stream_data and stream.ios_app_stream_data.bundle_id:
        entry["bundle_id"] = stream.ios_app_stream_data.bundle_id
    return entry


def _retention_entry(settings) -> dict:
    event = _enum_name(settings.event_data_retention)
    user = _enum_name(settings.user_data_retention)
    return {
        "event_data_months": _RETENTION_MONTHS.get(event),
        "user_data_months": _RETENTION_MONTHS.get(user),
        "reset_user_data_on_new_activity": bool(settings.reset_user_data_on_new_activity),
    }


def get_ga4_property(config: AdLoopConfig, *, property_id: str = "") -> dict:
    """One GA4 property's configuration: details, Ads links, streams, retention.

    Each part is read separately; a part the user may not read (or that
    fails) is listed under ``unavailable`` instead of failing the call.
    """
    from adloop.ga4.client import get_admin_client
    from adloop.ga4.tracking import normalize_property

    name = normalize_property(property_id)
    if not name:
        return {"error": "property_id is required (falls back to the configured GA4 property when set)"}
    if not name.removeprefix("properties/").isdigit():
        return {"error": "property_id must be a numeric GA4 property ID"}

    client = get_admin_client(config)
    result: dict = {"property": name}
    unavailable: dict[str, str] = {}

    try:
        result["details"] = _property_entry(client.get_property(name=name))
    except Exception as e:  # noqa: BLE001 - the details call decides whether anything else is worth trying
        return {"error": f"Could not read {name}: {_short_error(e)}"}

    sections = {
        "google_ads_links": lambda: [_ads_link_entry(link) for link in client.list_google_ads_links(parent=name)],
        "data_streams": lambda: [_stream_entry(s) for s in client.list_data_streams(parent=name)],
        "data_retention": lambda: _retention_entry(
            client.get_data_retention_settings(name=f"{name}/dataRetentionSettings")
        ),
        "key_event_count": lambda: sum(1 for _ in client.list_key_events(parent=name)),
    }
    for key, read in sections.items():
        try:
            result[key] = read()
        except Exception as e:  # noqa: BLE001 - one unreadable part must not hide the rest
            unavailable[key] = _short_error(e)
    if unavailable:
        result["unavailable"] = unavailable

    result["insights"] = _property_insights(config, result)
    return result


def _property_insights(config: AdLoopConfig, result: dict) -> list[str]:
    insights: list[str] = []
    details = result["details"]

    if details.get("delete_time"):
        when = details.get("expire_time") or "soon"
        insights.append(f"This property is in the trash and will be deleted permanently ({when}).")

    retention = result.get("data_retention") or {}
    if retention.get("event_data_months") == 2:
        insights.append(
            "Event data is kept for 2 months, GA4's default. Explorations and funnel reports cannot look "
            "further back than that; standard properties allow 14 months (draft_ga4_data_retention, or Admin → "
            "Data collection and modification → Data retention). The change applies from now on, lost months "
            "stay lost."
        )

    links = result.get("google_ads_links")
    key_events = result.get("key_event_count")
    if links == []:
        if key_events:
            insights.append(
                f"No Google Ads account is linked to this property, so Google Ads cannot import its "
                f"{key_events} key event(s) as conversions or use its audiences."
            )
        else:
            insights.append("No Google Ads account is linked to this property.")
    elif links:
        configured = format_customer_id(getattr(getattr(config, "ads", None), "customer_id", "") or "")
        linked = {link["customer_id"] for link in links}
        if configured and configured not in linked:
            insights.append(
                f"The Google Ads account AdLoop is set up with ({configured}) is not linked to this "
                f"property; linked: {', '.join(sorted(linked))}. Google Ads can only import key events "
                "from a property it is linked to."
            )

    streams = result.get("data_streams")
    if streams == []:
        insights.append("The property has no data stream, so it receives no data.")
    return insights


def fingerprint(values: dict) -> str:
    """A short hash of an entity's current values, stored on update plans.

    The dry run and the apply re-read the entity and refuse when the hash
    differs: someone changed it in GA4 after the preview was shown.
    """
    import hashlib
    import json

    blob = json.dumps(values, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def check_property(property_id: str) -> tuple[str, str]:
    """('123', '') for a usable property ID, ('', error) otherwise."""
    pid = str(property_id or "").strip().removeprefix("properties/")
    if not pid:
        return "", "property_id is required (falls back to the configured GA4 property when set)"
    if not pid.isdigit():
        return "", "property_id must be a numeric GA4 property ID"
    return pid, ""


def service_level(config: AdLoopConfig, property_id: str) -> str:
    """'standard' or '360' (GA4 limits differ); 'standard' when unreadable."""
    from adloop.ga4.client import get_admin_client

    try:
        prop = get_admin_client(config).get_property(name=f"properties/{property_id}")
    except Exception:  # noqa: BLE001 - the lower limits are the safe assumption
        return "standard"
    return _SERVICE_LEVELS.get(_enum_name(prop.service_level), "standard")
