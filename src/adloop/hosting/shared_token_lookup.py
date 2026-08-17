"""Supabase-backed shared Google refresh-token lookup (reporting@, per service).

Mirrors :mod:`adloop.hosting.token_lookup`, but keyed by SERVICE
(``ga4``/``gtm``/``gsc``) rather than by user. Unlike the per-user Ads flow, the
GA4/GTM/GSC connections use a single shared Google identity
(reporting@motiventmarketing.com); every signed-in ClientBrain employee borrows
that access through their session, and nobody else signs in to Google.

--------------------------------------------------------------------------------
CONTRACT with the ClientBrain "shared Google reporting" half
--------------------------------------------------------------------------------
ClientBrain captures one refresh token PER SERVICE (each from its own Web OAuth
client + a single read-only scope) and stores them encrypted in Supabase Vault,
exposed through a ``security definer`` RPC so the token never lives in a readable
column. This module calls exactly that RPC:

    public.get_shared_google_refresh_credential(p_service text) RETURNS text

  * argument: the service key -- ``ga4`` | ``gtm`` | ``gsc``
  * returns: the decrypted refresh token, or NULL if that service is not
    connected (or was disconnected)
  * grants: EXECUTE to the role this server connects as (the pooler role) and to
    ``postgres``

If the RPC returns NULL / no row, this lookup returns ``None`` and the shared
credentials provider raises ``MissingGoogleConnection``. Reuses the Phase D
connection pool (``ADLOOP_DATABASE_URL``); importing this module is
side-effect-free (the pool is built lazily on first call).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from adloop.hosting.datastore import ConnectionProvider, build_connection_provider

log = logging.getLogger("adloop.hosting.shared_token_lookup")

# service key ('ga4'|'gtm'|'gsc') -> refresh token (or None if not connected)
SharedTokenLookupFn = Callable[[str], "str | None"]

# Single-column RPC call. psycopg passes the service key as text.
_RPC_SQL = "select public.get_shared_google_refresh_credential(%s) as refresh_token"


def _first_value(row: Any) -> Any:
    """Read the single selected column from a psycopg row (dict_row) or a tuple."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("refresh_token")
    return row[0]


class SharedTokenLookup:
    """Shared refresh-token lookup via ``get_shared_google_refresh_credential``."""

    def __init__(self, connect: ConnectionProvider) -> None:
        self._connect = connect

    def __call__(self, service: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(_RPC_SQL, (service,)).fetchone()
        token = _first_value(row)
        return token or None


def build_shared_token_lookup() -> SharedTokenLookup | None:
    """Return a Supabase-backed shared lookup, or ``None`` if no DB is configured.

    ``None`` means the shared reporting connections are unavailable (the provider
    then raises ``MissingGoogleConnection`` for GA4/GTM/GSC). In a hosted
    deployment ``ADLOOP_DATABASE_URL`` is set, so the real lookup is used.
    """
    connect = build_connection_provider()
    if connect is None:
        log.warning(
            "ADLOOP_DATABASE_URL not set -- shared Google reporting lookup "
            "unavailable; GA4/GTM/GSC tools will report no connection."
        )
        return None
    return SharedTokenLookup(connect)
