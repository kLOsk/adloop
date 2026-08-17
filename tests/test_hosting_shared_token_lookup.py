"""Tests for the shared reporting per-service refresh-token lookup."""

from contextlib import contextmanager

from adloop.hosting.shared_token_lookup import (
    SharedTokenLookup,
    build_shared_token_lookup,
)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDB:
    """Records the RPC call and returns a canned row (dict_row shape)."""

    def __init__(self, row):
        self._row = row
        self.calls = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _FakeCursor(self._row)


def test_lookup_returns_token_for_service():
    db = _FakeDB({"refresh_token": "rt-ga4"})
    lookup = SharedTokenLookup(db.connect)
    assert lookup("ga4") == "rt-ga4"
    # Calls the contract RPC keyed by the service, not a user id.
    sql, params = db.calls[0]
    assert "get_shared_google_refresh_credential" in sql
    assert params == ("ga4",)


def test_lookup_none_when_unconnected():
    assert SharedTokenLookup(_FakeDB({"refresh_token": None}).connect)("gtm") is None
    assert SharedTokenLookup(_FakeDB(None).connect)("gsc") is None
    assert SharedTokenLookup(_FakeDB({"refresh_token": ""}).connect)("ga4") is None


def test_lookup_tolerates_tuple_rows():
    assert SharedTokenLookup(_FakeDB(("rt-tuple",)).connect)("ga4") == "rt-tuple"


def test_build_returns_none_without_db(monkeypatch):
    monkeypatch.delenv("ADLOOP_DATABASE_URL", raising=False)
    assert build_shared_token_lookup() is None


def test_build_returns_lookup_with_db(monkeypatch):
    monkeypatch.setenv("ADLOOP_DATABASE_URL", "postgresql://u:p@host:6543/db")
    assert isinstance(build_shared_token_lookup(), SharedTokenLookup)
