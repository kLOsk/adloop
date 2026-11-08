"""Tests for the GA4 property overview (get_ga4_property)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, AdsConfig, GA4Config
from adloop.ga4 import admin

_CREATED = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)


def _enum(name):
    return SimpleNamespace(name=name)


def _property(**overrides):
    values = dict(
        name="properties/123456", display_name="Shop", time_zone="Europe/Berlin", currency_code="EUR",
        industry_category=_enum("SHOPPING"), service_level=_enum("GOOGLE_ANALYTICS_STANDARD"),
        property_type=_enum("PROPERTY_TYPE_ORDINARY"), account="accounts/42", parent="accounts/42",
        create_time=_CREATED, delete_time=None, expire_time=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _link(customer_id="1234567890"):
    return SimpleNamespace(
        name=f"properties/123456/googleAdsLinks/{customer_id}", customer_id=customer_id,
        can_manage_clients=False, ads_personalization_enabled=True, create_time=_CREATED,
        creator_email_address="owner@example.com",
    )


def _web_stream():
    return SimpleNamespace(
        name="properties/123456/dataStreams/7", type_=_enum("WEB_DATA_STREAM"), display_name="Website",
        create_time=_CREATED,
        web_stream_data=SimpleNamespace(measurement_id="G-ABC123", default_uri="https://shop.example"),
        android_app_stream_data=None, ios_app_stream_data=None,
    )


def _client(*, links=None, streams=None, retention="FOURTEEN_MONTHS", key_events=2, fail=None):
    client = MagicMock()
    client.get_property.return_value = _property()
    client.list_google_ads_links.return_value = [_link()] if links is None else links
    client.list_data_streams.return_value = [_web_stream()] if streams is None else streams
    client.get_data_retention_settings.return_value = SimpleNamespace(
        event_data_retention=_enum(retention), user_data_retention=_enum("FOURTEEN_MONTHS"),
        reset_user_data_on_new_activity=True,
    )
    client.list_key_events.return_value = [object()] * key_events
    for method in fail or []:
        getattr(client, method).side_effect = PermissionError("403 The caller does not have permission")
    return client


def _overview(client, config=None, property_id="123456"):
    config = config or AdLoopConfig(ga4=GA4Config(property_id="properties/123456"))
    with patch("adloop.ga4.client.get_admin_client", return_value=client):
        return admin.get_ga4_property(config, property_id=property_id)


def test_one_call_returns_details_links_streams_and_retention():
    result = _overview(_client())

    assert result["details"] == {
        "property": "properties/123456", "display_name": "Shop", "time_zone": "Europe/Berlin",
        "currency_code": "EUR", "industry_category": "SHOPPING", "service_level": "standard",
        "property_type": "ordinary", "account": "accounts/42", "create_time": _CREATED.isoformat(),
    }
    assert result["google_ads_links"] == [{
        "customer_id": "123-456-7890", "can_manage_clients": False, "create_time": _CREATED.isoformat(),
        "resource_name": "properties/123456/googleAdsLinks/1234567890",
        "ads_personalization_enabled": True, "created_by": "owner@example.com",
    }]
    assert result["data_streams"][0]["measurement_id"] == "G-ABC123"
    assert result["data_streams"][0]["type"] == "web"
    assert result["data_retention"] == {
        "event_data_months": 14, "user_data_months": 14, "reset_user_data_on_new_activity": True,
    }
    assert result["key_event_count"] == 2
    assert result["insights"] == []
    assert "unavailable" not in result


def test_two_month_retention_is_flagged():
    result = _overview(_client(retention="TWO_MONTHS"))
    assert any("kept for 2 months" in i for i in result["insights"])


def test_missing_ads_link_is_flagged_with_the_key_event_count():
    result = _overview(_client(links=[], key_events=3))
    assert result["insights"] == [
        "No Google Ads account is linked to this property, so Google Ads cannot import its "
        "3 key event(s) as conversions or use its audiences."
    ]


def test_configured_ads_account_that_is_not_linked_is_flagged():
    config = AdLoopConfig(ga4=GA4Config(property_id="properties/123456"), ads=AdsConfig(customer_id="9998887777"))
    result = _overview(_client(), config=config)
    assert any("(999-888-7777) is not linked" in i and "123-456-7890" in i for i in result["insights"])


def test_an_unreadable_part_is_listed_instead_of_failing():
    result = _overview(_client(fail=["get_data_retention_settings"]))
    assert result["unavailable"] == {"data_retention": "403 The caller does not have permission"}
    assert result["google_ads_links"]


def test_unreadable_property_is_an_error():
    client = _client()
    client.get_property.side_effect = PermissionError("403 denied")
    assert _overview(client)["error"] == "Could not read properties/123456: 403 denied"


@pytest.mark.parametrize("property_id, message", [("", "required"), ("abc", "numeric")])
def test_property_id_is_checked(property_id, message):
    config = AdLoopConfig(ga4=GA4Config(property_id=""))
    assert message in _overview(_client(), config=config, property_id=property_id)["error"]
