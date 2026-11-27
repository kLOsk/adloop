"""Tests for currency detection, caching, and formatting."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from adloop.ads.currency import format_currency, get_currency_code, _cache
from adloop.ads.read import _enrich_cost_fields


# ---------------------------------------------------------------------------
# format_currency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount, code, expected",
    [
        (42.50, "EUR", "42.50 EUR"),
        (42.50, "PLN", "42.50 PLN"),
        (42.50, "USD", "42.50 USD"),
        (0.00, "GBP", "0.00 GBP"),
        (1234.56, "XYZ", "1234.56 XYZ"),
    ],
)
def test_format_currency(amount: float, code: str, expected: str) -> None:
    assert format_currency(amount, code) == expected


# ---------------------------------------------------------------------------
# get_currency_code — caching
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the module-level currency cache before each test."""
    _cache.clear()
    yield
    _cache.clear()


def _make_config() -> MagicMock:
    config = MagicMock()
    config.ads.customer_id = "123-456-7890"
    config.ads.login_customer_id = "111-222-3333"
    config.ads.developer_token = "test-token"
    return config


@patch("adloop.ads.gaql.execute_query")
def test_get_currency_code_queries_api(mock_eq: MagicMock) -> None:
    mock_eq.return_value = [{"customer.currency_code": "PLN"}]
    config = _make_config()

    result = get_currency_code(config, "123-456-7890")

    assert result == "PLN"
    mock_eq.assert_called_once()


@patch("adloop.ads.gaql.execute_query")
def test_get_currency_code_caches_result(mock_eq: MagicMock) -> None:
    mock_eq.return_value = [{"customer.currency_code": "PLN"}]
    config = _make_config()

    get_currency_code(config, "123-456-7890")
    get_currency_code(config, "123-456-7890")

    # Only one API call despite two invocations
    mock_eq.assert_called_once()


@patch("adloop.ads.gaql.execute_query")
def test_get_currency_code_fallback_on_error(mock_eq: MagicMock) -> None:
    mock_eq.side_effect = Exception("API failure")
    config = _make_config()

    result = get_currency_code(config, "123-456-7890")

    assert result == "EUR"


@patch("adloop.ads.gaql.execute_query")
def test_get_currency_code_fallback_on_empty_rows(mock_eq: MagicMock) -> None:
    mock_eq.return_value = []
    config = _make_config()

    result = get_currency_code(config, "123-456-7890")

    assert result == "EUR"


@patch("adloop.ads.gaql.execute_query")
def test_a_failed_lookup_is_not_remembered(mock_eq: MagicMock) -> None:
    """A passing error used to pin the account to EUR until a restart."""
    config = _make_config()
    mock_eq.side_effect = Exception("API failure")
    assert get_currency_code(config, "123-456-7890") == "EUR"

    mock_eq.side_effect = None
    mock_eq.return_value = [{"customer.currency_code": "PLN"}]
    assert get_currency_code(config, "123-456-7890") == "PLN"


@patch("adloop.ads.gaql.execute_query")
def test_currency_is_cached_per_tenant(mock_eq: MagicMock) -> None:
    """On a hosted server one tenant's lookup of an account id, failed or
    not, must not decide what another tenant sees for that id."""
    from adloop import runtime

    config = _make_config()
    runtime.set_deployment_mode("server")
    try:
        mock_eq.return_value = [{"customer.currency_code": "PLN"}]
        with runtime.use_runtime(config, tenant="1"):
            assert get_currency_code(config, "123-456-7890") == "PLN"
        mock_eq.return_value = [{"customer.currency_code": "USD"}]
        with runtime.use_runtime(config, tenant="2"):
            assert get_currency_code(config, "123-456-7890") == "USD"
        with runtime.use_runtime(config, tenant="1"):
            assert get_currency_code(config, "123-456-7890") == "PLN"
    finally:
        runtime.set_deployment_mode("local")
    assert mock_eq.call_count == 2


# ---------------------------------------------------------------------------
# _enrich_cost_fields with currency_code
# ---------------------------------------------------------------------------


def test_enrich_cost_fields_with_currency() -> None:
    rows = [
        {
            "metrics.cost_micros": 5_000_000,
            "metrics.conversions": 2,
            "metrics.average_cpc": 1_500_000,
        }
    ]

    _enrich_cost_fields(rows, currency_code="PLN")

    row = rows[0]
    assert row["metrics.cost"] == 5.0
    assert row["metrics.cpa"] == 2.5
    assert row["metrics.average_cpc_amount"] == 1.5
    assert row["metrics.currency"] == "PLN"


def test_enrich_cost_fields_default_eur() -> None:
    rows = [{"metrics.cost_micros": 1_000_000}]

    _enrich_cost_fields(rows)

    assert rows[0]["metrics.currency"] == "EUR"


def test_enrich_cost_fields_no_old_eur_field() -> None:
    """The old ``metrics.average_cpc_eur`` field should no longer appear."""
    rows = [
        {
            "metrics.cost_micros": 2_000_000,
            "metrics.average_cpc": 500_000,
        }
    ]

    _enrich_cost_fields(rows, currency_code="USD")

    assert "metrics.average_cpc_eur" not in rows[0]
    assert rows[0]["metrics.average_cpc_amount"] == 0.5
