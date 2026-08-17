"""Resolve a ClientBrain client to its Google reporting targets (server side).

The shared reporting tools query Google *as* reporting@, but each call still
needs to know WHICH property/container/site to hit for a given ClientBrain
client. That mapping lives in ClientBrain, not here, so resolution happens on
the server via a security-definer RPC — the client never passes a raw property
id it made up.

--------------------------------------------------------------------------------
CONTRACT with the ClientBrain "client Google properties" half
--------------------------------------------------------------------------------
ClientBrain stores a per-client map (GA4 property id, GTM account + container id,
GSC site url) and exposes it through:

    public.resolve_client_google_targets(p_client_id uuid)
      RETURNS TABLE (ga4_property_id text, gtm_account_id text,
                     gtm_container_id text, gsc_site_url text)

  * argument: the ClientBrain client id (uuid)
  * returns: one row of targets, or NO row if the client has no mapping yet
  * grants: EXECUTE to the role this server connects as (the pooler role) and to
    ``postgres``

Reuses the Phase D connection pool (``ADLOOP_DATABASE_URL``); importing this
module is side-effect-free (the pool is built lazily on first call).
"""

from __future__ import annotations

import logging
from typing import Any

from adloop.hosting.datastore import ConnectionProvider, build_connection_provider

log = logging.getLogger("adloop.hosting.client_resolution")

_FIELDS = ("ga4_property_id", "gtm_account_id", "gtm_container_id", "gsc_site_url")

_RPC_SQL = (
    "select ga4_property_id, gtm_account_id, gtm_container_id, gsc_site_url "
    "from public.resolve_client_google_targets(%s)"
)


def _row_to_targets(row: Any) -> dict[str, str | None] | None:
    """Normalize a psycopg row (dict_row or tuple) into a targets dict.

    Empty strings become ``None`` so an unconfigured field reads the same as a
    missing one. Returns ``None`` when the client has no mapping row at all.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return {field: (row.get(field) or None) for field in _FIELDS}
    return {field: (row[index] or None) for index, field in enumerate(_FIELDS)}


class ClientTargetResolver:
    """Resolve a ClientBrain client id to its GA4/GTM/GSC targets."""

    def __init__(self, connect: ConnectionProvider) -> None:
        self._connect = connect

    def __call__(self, client_id: str) -> dict[str, str | None] | None:
        with self._connect() as conn:
            row = conn.execute(_RPC_SQL, (client_id,)).fetchone()
        return _row_to_targets(row)


def build_client_target_resolver() -> ClientTargetResolver | None:
    """Return a Supabase-backed resolver, or ``None`` if no DB is configured."""
    connect = build_connection_provider()
    if connect is None:
        log.warning(
            "ADLOOP_DATABASE_URL not set -- client target resolution unavailable."
        )
        return None
    return ClientTargetResolver(connect)
