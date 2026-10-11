"""Per-request runtime context — config, tenant, and deployment mode.

AdLoop runs in two shapes:

- **local** (the OSS default): one user, one process, config loaded from
  ``~/.adloop/config.yaml``. Nothing here changes for that case — the
  config is loaded lazily on first use and shared process-wide.
- **server** (hosted/multi-tenant): many users share one process. An
  ASGI layer resolves the authenticated tenant per request and binds
  that tenant's config via :func:`use_runtime` before any tool runs.

Tool code never touches this distinction — it calls
:func:`current_config` and :func:`current_tenant` and gets the right
answer in both shapes. FastMCP executes sync tools through anyio's
thread offloading, which copies the caller's contextvars into the
worker thread, so values bound here are visible inside tool bodies.

In server mode there is deliberately NO fallback to the local config
file: a request that reaches a tool without a bound tenant config is a
bug, and silently using the operator's own ``~/.adloop`` credentials
would be a cross-tenant leak.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Literal

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

LOCAL_TENANT = "local"

DeploymentMode = Literal["local", "server"]

_config_var: ContextVar["AdLoopConfig | None"] = ContextVar(
    "adloop_config", default=None
)
_tenant_var: ContextVar[str | None] = ContextVar("adloop_tenant", default=None)

_default_config: AdLoopConfig | None = None
_default_config_lock = threading.Lock()

_deployment_mode: DeploymentMode = "local"


# Which registered tools the current caller is actually offered, given each
# tool's tags. Self-hosted, every enabled tool is; a hosted runtime narrows it
# per request (a key's toolsets, tools it cannot serve) so health_check can
# report the same list the caller's tools/list shows.
ToolVisibility = Callable[[dict[str, frozenset[str]]], Iterable[str]]
_tool_visibility: ToolVisibility | None = None


def set_tool_visibility(fn: ToolVisibility | None) -> None:
    """Install the per-request tool filter. Called once at startup."""
    global _tool_visibility
    _tool_visibility = fn


def tool_visibility() -> ToolVisibility | None:
    return _tool_visibility


def default_setting_hint(config_key: str, dashboard_page: str) -> str:
    """How to set a missing default, phrased for where AdLoop runs.

    Self-hosted it is a key in the config file; a hosted runtime has no
    config file the user can edit, so it names the dashboard page instead.
    """
    if _deployment_mode == "server":
        return (
            f"choose a default under {dashboard_page} in the AdLoop Cloud "
            "dashboard (agencies: per client under Settings → Clients)"
        )
    return f"set {config_key} in the config (~/.adloop/config.yaml)"


def deployment_mode() -> DeploymentMode:
    """Return the process-wide deployment mode ("local" or "server")."""
    return _deployment_mode


def set_deployment_mode(mode: DeploymentMode) -> None:
    """Set the deployment mode. Called once at process startup."""
    global _deployment_mode
    if mode not in ("local", "server"):
        raise ValueError(f"Unknown deployment mode: {mode!r}")
    _deployment_mode = mode


def current_config() -> AdLoopConfig:
    """Return the config for the current execution context.

    Resolution order:
    1. A config bound to the current context via :func:`use_runtime`
       (or :func:`set_default_config` in tests).
    2. In local mode: the process-wide default, loaded lazily from
       ``~/.adloop/config.yaml`` on first use.

    In server mode, an unbound context raises instead of falling back.
    """
    cfg = _config_var.get()
    if cfg is not None:
        return cfg

    global _default_config
    if _default_config is None:
        if _deployment_mode == "server":
            raise RuntimeError(
                "No tenant config is bound to this request. In server mode "
                "every request must bind its tenant's config via "
                "adloop.runtime.use_runtime() before tools execute."
            )
        with _default_config_lock:
            if _default_config is None:
                from adloop.config import load_config

                _default_config = load_config()
    return _default_config


def set_default_config(config: AdLoopConfig | None) -> None:
    """Set (or clear with ``None``) the process-wide fallback config.

    The stdio entry point may call this once at startup; tests use it to
    inject fixtures without touching ``~/.adloop``.
    """
    global _default_config
    _default_config = config


def current_tenant() -> str:
    """Return the tenant id for the current execution context.

    Always ``"local"`` in local mode. In server mode the value comes
    from :func:`use_runtime`.
    """
    return _tenant_var.get() or LOCAL_TENANT


@contextmanager
def use_runtime(
    config: AdLoopConfig, *, tenant: str = LOCAL_TENANT
) -> Iterator[None]:
    """Bind a config (and tenant id) to the current execution context.

    The hosted server wraps every MCP request in this; tests use it to
    run tool code against a fixture config.
    """
    config_token = _config_var.set(config)
    tenant_token = _tenant_var.set(tenant)
    try:
        yield
    finally:
        _config_var.reset(config_token)
        _tenant_var.reset(tenant_token)
