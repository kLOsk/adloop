"""Tests for the server-side ClientBrain client -> Google target resolver."""

from contextlib import contextmanager

from adloop.hosting.client_resolution import (
    ClientTargetResolver,
    build_client_target_resolver,
)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row
        self.calls = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _FakeCursor(self._row)


def test_resolver_returns_targets():
    db = _FakeDB({
        "ga4_property_id": "123456789",
        "gtm_account_id": "6000000",
        "gtm_container_id": "GTM-ABC123",
        "gsc_site_url": "https://example.com/",
    })
    resolver = ClientTargetResolver(db.connect)
    assert resolver("client-1") == {
        "ga4_property_id": "123456789",
        "gtm_account_id": "6000000",
        "gtm_container_id": "GTM-ABC123",
        "gsc_site_url": "https://example.com/",
    }
    sql, params = db.calls[0]
    assert "resolve_client_google_targets" in sql
    assert params == ("client-1",)


def test_resolver_none_when_unmapped():
    # RPC returns no row -> the client has no mapping yet.
    assert ClientTargetResolver(_FakeDB(None).connect)("client-x") is None


def test_resolver_empty_fields_normalize_to_none():
    resolver = ClientTargetResolver(_FakeDB({
        "ga4_property_id": "123456789",
        "gtm_account_id": "",
        "gtm_container_id": None,
        "gsc_site_url": "",
    }).connect)
    assert resolver("c") == {
        "ga4_property_id": "123456789",
        "gtm_account_id": None,
        "gtm_container_id": None,
        "gsc_site_url": None,
    }


def test_resolver_tolerates_tuple_rows():
    resolver = ClientTargetResolver(
        _FakeDB(("123456789", "6000000", "GTM-ABC123", "https://example.com/")).connect
    )
    assert resolver("c")["ga4_property_id"] == "123456789"
    assert resolver("c")["gsc_site_url"] == "https://example.com/"


def test_build_returns_none_without_db(monkeypatch):
    monkeypatch.delenv("ADLOOP_DATABASE_URL", raising=False)
    assert build_client_target_resolver() is None


def test_build_returns_resolver_with_db(monkeypatch):
    monkeypatch.setenv("ADLOOP_DATABASE_URL", "postgresql://u:p@host:6543/db")
    assert isinstance(build_client_target_resolver(), ClientTargetResolver)
