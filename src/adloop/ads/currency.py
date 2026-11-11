"""Currency detection and formatting — auto-detect from Google Ads account."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "\u20ac",
    "USD": "$",
    "GBP": "\u00a3",
    "PLN": "z\u0142",
    "CHF": "CHF",
    "CZK": "K\u010d",
    "SEK": "kr",
    "NOK": "kr",
    "DKK": "kr",
    "HUF": "Ft",
    "RON": "lei",
    "BGN": "лв",
    "HRK": "kn",
    "TRY": "\u20ba",
    "JPY": "\u00a5",
    "CNY": "\u00a5",
    "KRW": "\u20a9",
    "INR": "\u20b9",
    "BRL": "R$",
    "AUD": "A$",
    "CAD": "C$",
    "NZD": "NZ$",
    "MXN": "MX$",
    "ARS": "ARS",
    "ZAR": "R",
    "RUB": "\u20bd",
    "UAH": "\u20b4",
}

# Module-level cache: (tenant, customer_id) -> currency_code (one API call
# per session). The tenant is part of the key because a hosted server runs
# many tenants in one process: what one of them reads, or fails to read,
# about an account must never be served to another.
_cache: dict[tuple[str, str], str] = {}


def get_currency_code(config: AdLoopConfig, customer_id: str) -> str:
    """Detect the account's currency via ``customer.currency_code``.

    A successful lookup is cached per tenant and *customer_id* for the
    lifetime of the server process. Falls back to ``"EUR"`` on any error;
    the fallback is not cached, so a passing failure does not stick.
    """
    from adloop.ads.client import normalize_customer_id
    from adloop.runtime import current_tenant

    key = (current_tenant(), normalize_customer_id(customer_id))
    if key in _cache:
        return _cache[key]

    try:
        from adloop.ads.gaql import execute_query

        rows = execute_query(
            config, customer_id, "SELECT customer.currency_code FROM customer LIMIT 1"
        )
        code = rows[0].get("customer.currency_code") if rows else None
    except Exception:
        code = None
    if not code:
        return "EUR"

    _cache[key] = code
    return code


def format_currency(amount: float, currency_code: str) -> str:
    """Format *amount* with the correct currency symbol/code.

    Examples::

        format_currency(42.50, "PLN")  -> "42.50 PLN"
        format_currency(42.50, "EUR")  -> "42.50 EUR"
        format_currency(42.50, "USD")  -> "42.50 USD"
    """
    return f"{amount:.2f} {currency_code}"
