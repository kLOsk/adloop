"""Hosted HTTP/ASGI entry point for AdLoop (multi-tenant *server* mode).

Importing this module is deliberately free of process-global side effects,
mirroring ``adloop.server``: the deployment-mode switch, runtime patches, and
the HTTP server are only touched inside :func:`create_app` / :func:`main`. That
keeps the module safe to import from tests and tooling without flipping the
whole process into server mode.

Two ways to serve it:

  * ``python -m adloop.asgi``                     — runs the server directly
    (FastMCP drives uvicorn); binds ``0.0.0.0:$PORT``. This is the Cloud Run
    entry point.
  * ``uvicorn adloop.asgi:create_app --factory``  — bring your own ASGI server.

Calling either flips the process into **server mode** (``set_deployment_mode
("server")``), which makes the merged upstream refuse local-file credentials
and filesystem tools. A hosted deployment must still install a credentials
provider, a plan store, and an audit sink (later phases) before any tool can
execute for a tenant — this module only stands up the transport.
"""

from __future__ import annotations

import os

# Default listen settings. Cloud Run injects PORT; 8080 is its conventional
# default when running the container locally.
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8080


def _configure_server_runtime() -> None:
    """Arm runtime patches and flip the process into server mode.

    Idempotent: ``install_runtime_patches`` is idempotent and
    ``set_deployment_mode`` just (re)sets a process-global. Kept out of module
    import so importing this file never changes global state.
    """
    from adloop import install_runtime_patches
    from adloop.runtime import set_deployment_mode

    install_runtime_patches()
    set_deployment_mode("server")


def _env_list(name: str) -> list[str] | None:
    """Parse a comma-separated env var into a list, or None if unset/empty."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _transport_kwargs() -> dict:
    """Host/origin allow-lists for the HTTP transport, driven by env.

    A hosted deployment behind Cloud Run must allow-list its own domain so
    FastMCP's DNS-rebinding protection doesn't reject legitimate traffic:

      ADLOOP_ALLOWED_HOSTS    comma-separated Host header values
      ADLOOP_ALLOWED_ORIGINS  comma-separated Origin values

    Unset → FastMCP defaults (localhost-friendly, for local dev).
    """
    kwargs: dict = {}
    hosts = _env_list("ADLOOP_ALLOWED_HOSTS")
    origins = _env_list("ADLOOP_ALLOWED_ORIGINS")
    if hosts is not None:
        kwargs["allowed_hosts"] = hosts
    if origins is not None:
        kwargs["allowed_origins"] = origins
    return kwargs


def _prepare_server():
    """Configure server mode and install the hosting shell, return the mcp server.

    Order matters: ``install_auth`` must run before ``http_app()`` (the OAuth
    routes are built from ``mcp.auth``), and ``install_credentials_provider``
    must run before any tool executes (server mode refuses the default
    local-file provider).
    """
    _configure_server_runtime()
    from adloop.hosting.auth import install_auth
    from adloop.hosting.datastore import install_datastore
    from adloop.hosting.shared_credentials import install_shared_credentials_provider
    from adloop.hosting.shared_token_lookup import build_shared_token_lookup
    from adloop.hosting.token_lookup import build_supabase_token_lookup
    from adloop.server import mcp

    install_auth(mcp)  # Supabase auth + tenant middleware (if configured)
    # GA4/GTM/GSC run off the shared reporting@ token (one per service); Ads
    # stays per-user. With a DB configured, the real Supabase lookups are used;
    # otherwise the env-var dev fallbacks apply (local dev only).
    install_shared_credentials_provider(
        shared_lookup=build_shared_token_lookup(),
        ads_lookup=build_supabase_token_lookup(),
    )
    install_datastore()  # Supabase-backed plan store + audit sink (if configured)
    return mcp


def create_app():
    """Build the streamable-HTTP ASGI app in server mode.

    Use with an external ASGI server, e.g.
    ``uvicorn adloop.asgi:create_app --factory``.
    """
    mcp = _prepare_server()
    return mcp.http_app(**_transport_kwargs())


def main() -> None:
    """Run the hosted HTTP server (Cloud Run entry point).

    Binds ``$HOST`` (default 0.0.0.0) : ``$PORT`` (default 8080) over
    streamable HTTP.
    """
    mcp = _prepare_server()

    host = os.environ.get("HOST", _DEFAULT_HOST)
    port = int(os.environ.get("PORT", str(_DEFAULT_PORT)))

    mcp.run(transport="http", host=host, port=port, **_transport_kwargs())


if __name__ == "__main__":
    main()
