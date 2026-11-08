"""Tests for the GA4 setup audit and the key event import check."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, AdsConfig, GA4Config
from adloop.ga4 import audit

_T = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _enum(name):
    return SimpleNamespace(name=name)


def _admin(*, retention="FOURTEEN_MONTHS", links=True, key_events=("purchase", "sign_up"), dims=0):
    client = MagicMock()
    client.get_property.return_value = SimpleNamespace(
        name="properties/123456", display_name="Shop", time_zone="Europe/Berlin", currency_code="EUR",
        industry_category=_enum("SHOPPING"), service_level=_enum("GOOGLE_ANALYTICS_STANDARD"),
        property_type=_enum("PROPERTY_TYPE_ORDINARY"), account="accounts/1", parent="accounts/1",
        create_time=_T, delete_time=None, expire_time=None,
    )
    client.list_google_ads_links.return_value = [SimpleNamespace(
        name="properties/123456/googleAdsLinks/1", customer_id="1234567890", can_manage_clients=False,
        ads_personalization_enabled=True, create_time=_T, creator_email_address="",
    )] if links else []
    client.list_data_streams.return_value = [SimpleNamespace(
        name="properties/123456/dataStreams/77", type_=_enum("WEB_DATA_STREAM"), display_name="Website", create_time=_T,
        web_stream_data=SimpleNamespace(measurement_id="G-ABC", default_uri="https://shop.example"),
        android_app_stream_data=None, ios_app_stream_data=None,
    )]
    client.get_data_retention_settings.return_value = SimpleNamespace(
        event_data_retention=_enum(retention), user_data_retention=_enum("FOURTEEN_MONTHS"),
        reset_user_data_on_new_activity=True,
    )
    events = [MagicMock(event_name=n, deletable=True, custom=True, create_time=None, default_value=None) for n in key_events]
    for e in events:
        e.counting_method.name = "ONCE_PER_EVENT"
        e.name = f"properties/123456/keyEvents/{e.event_name}"
    client.list_key_events.return_value = events
    client.list_custom_dimensions.return_value = [
        SimpleNamespace(name=f"d/{i}", parameter_name=f"p{i}", display_name=f"p{i}", scope=_enum("EVENT"),
                        description="", disallow_ads_personalization=False) for i in range(dims)
    ]
    client.list_custom_metrics.return_value = []
    return client


def _alpha(signals="GOOGLE_SIGNALS_ENABLED", enhanced=True):
    client = MagicMock()
    client.get_google_signals_settings.return_value = SimpleNamespace(state=_enum(signals))
    client.get_enhanced_measurement_settings.return_value = SimpleNamespace(
        stream_enabled=enhanced, scrolls_enabled=True, outbound_clicks_enabled=True, site_search_enabled=False,
        video_engagement_enabled=True, file_downloads_enabled=True, page_changes_enabled=True,
        form_interactions_enabled=True,
    )
    return client


def _reports(stream_active=True, fired=("purchase", "sign_up")):
    def run(config, **kwargs):
        if kwargs["dimensions"] == ["streamId"]:
            return {"rows": [{"streamId": "77", "eventCount": "500"}] if stream_active else []}
        return {"rows": [{"eventName": n, "eventCount": "12"} for n in fired]}
    return run


def _audit(admin=None, alpha=None, reports=None, config=None):
    config = config or AdLoopConfig(ga4=GA4Config(property_id="properties/123456"))
    with (
        patch("adloop.ga4.client.get_admin_client", return_value=admin or _admin()),
        patch("adloop.ga4.client.get_admin_alpha_client", return_value=alpha or _alpha()),
        patch("adloop.ga4.reports.run_ga4_report", side_effect=reports or _reports()),
    ):
        return audit.audit_ga4_property(config, property_id="123456")


def _statuses(result):
    return {(c["check"], c["status"]) for c in result["checks"]}


def test_healthy_property_has_no_warnings():
    result = _audit()
    assert result["summary"]["warn"] == 0
    assert result["insights"] == []
    assert ("data_retention", "ok") in _statuses(result)
    assert ("stream_activity", "ok") in _statuses(result)
    assert ("key_events", "ok") in _statuses(result)
    assert ("google_signals", "info") in _statuses(result)
    assert any("site search" in c["detail"] for c in result["checks"] if c["check"] == "enhanced_measurement")
    assert "unavailable" not in result


def test_problems_come_first_as_warnings():
    result = _audit(
        admin=_admin(retention="TWO_MONTHS", links=False, dims=45),
        reports=_reports(stream_active=False, fired=("purchase",)),
    )
    warns = [c["check"] for c in result["checks"] if c["status"] == "warn"]
    assert set(warns) == {"data_retention", "google_ads_link", "stream_activity", "key_events", "custom_definitions"}
    assert result["checks"][0]["status"] == "warn"
    assert any("sign_up" in i for i in result["insights"])
    assert any("event dimensions 45 of 50" in i for i in result["insights"])


def test_no_key_events_is_a_warning():
    result = _audit(admin=_admin(key_events=()))
    assert any(c["check"] == "key_events" and c["status"] == "warn" and "No key events" in c["detail"] for c in result["checks"])


def test_alpha_failures_are_listed_not_fatal():
    alpha = _alpha()
    alpha.get_google_signals_settings.side_effect = RuntimeError("404 not found")
    result = _audit(alpha=alpha)
    assert result["unavailable"]["google_signals"] == "404 not found"
    assert result["summary"]["warn"] == 0


def test_configured_but_unlinked_ads_account_is_flagged():
    config = AdLoopConfig(ga4=GA4Config(property_id="properties/123456"), ads=AdsConfig(customer_id="9998887777"))
    result = _audit(config=config)
    assert ("configured_ads_account", "warn") in _statuses(result)


# ---------------------------------------------------------------------------
# check_key_event_imports
# ---------------------------------------------------------------------------


def _imports(actions, recorded, ga4_rows, key_events=("purchase", "sign_up", "generate_lead")):
    from adloop import crossref

    def execute(config, customer_id, query):
        return actions if "FROM conversion_action" in query else recorded

    listed = {"key_events": [{"event_name": n} for n in key_events]}
    config = AdLoopConfig(ga4=GA4Config(property_id="properties/123456"))
    with (
        patch("adloop.ads.gaql.execute_query", side_effect=execute),
        patch("adloop.ga4.tracking.list_key_events", return_value=listed),
        patch("adloop.ga4.reports.run_ga4_report", return_value={"rows": ga4_rows}) as report,
    ):
        result = crossref.check_key_event_imports(
            config, customer_id="123-456-7890", property_id="123456",
            date_range_start="2026-10-01", date_range_end="2026-10-31",
        )
    return result, report


def _action(action_id, event, primary=True, property_id="123456", name=None, status="ENABLED"):
    return {
        "conversion_action.id": action_id, "conversion_action.name": name or f"GA4 {event}",
        "conversion_action.status": status, "conversion_action.primary_for_goal": primary,
        "conversion_action.google_analytics_4_settings.event_name": event,
        "conversion_action.google_analytics_4_settings.property_id": property_id,
    }


def test_import_check_names_every_gap():
    actions = [
        _action(1, "purchase"),
        _action(2, "sign_up", primary=False),
        _action(3, "old_lead"),
        _action(4, "purchase", property_id="999", name="Other shop purchase"),
    ]
    recorded = [{"segments.conversion_action": "customers/1234567890/conversionActions/2", "metrics.all_conversions": 3.0}]
    ga4_rows = [{"eventName": "purchase", "keyEvents": "40"}, {"eventName": "sign_up", "keyEvents": "8"},
                {"eventName": "generate_lead", "keyEvents": "6"}]
    result, report = _imports(actions, recorded, ga4_rows)

    text = " ".join(result["insights"])
    assert "'generate_lead' is a GA4 key event (6 from Google Ads traffic) but no Google Ads conversion action imports it" in text
    assert "'sign_up' is imported only as a secondary conversion" in text
    assert "GA4 counted 40 'purchase' key events from Google Ads traffic, but Google Ads recorded none" in text
    assert "Google Ads imports 'old_lead'" in text
    assert result["imports_from_other_properties"][0]["ga4_property"] == "properties/999"
    purchase = next(r for r in result["key_events"] if r["event_name"] == "purchase")
    assert purchase["imports"][0]["conversion_action_id"] == "1"

    ga4_filter = report.call_args.kwargs["dimension_filter"]
    assert ga4_filter[0] == {"field": "sessionSourceMedium", "value": "google / cpc"}


def test_quiet_period_is_not_called_broken():
    result, _ = _imports([_action(1, "purchase")], [], [{"eventName": "purchase", "keyEvents": "2"}], key_events=("purchase",))
    assert result["insights"] == []


@pytest.mark.parametrize("kwargs, message", [
    ({"property_id": "", "customer_id": "1234567890"}, "property_id is required"),
    ({"property_id": "123456", "customer_id": ""}, "customer_id is required"),
])
def test_import_check_needs_both_accounts(kwargs, message):
    from adloop import crossref

    result = crossref.check_key_event_imports(AdLoopConfig(), **kwargs)
    assert message in result["error"]


def test_hidden_imports_from_linking_are_not_imported_yet():
    # Seen live: linking GA4 created both actions as HIDDEN and not primary.
    actions = [_action(1, "sign_up", primary=False, status="HIDDEN", name="getadloop.com (web) sign_up")]
    result, _ = _imports(actions, [], [{"eventName": "sign_up", "keyEvents": "0"}], key_events=("sign_up",))
    assert result["insights"] == [
        "'sign_up' is not imported yet: Google Ads created 'getadloop.com (web) sign_up' when the property "
        "was linked, but it stays hidden and records no conversions until it is imported "
        "(Google Ads → Goals → Conversions → New conversion action → Import → Google Analytics 4)."
    ]
