"""Views: read results drawn as dashboards in apps that show MCP Apps.

A view is an extra layer on a read tool, never a replacement:

- The answer the AI reads (the tool's text content) is exactly what the tool
  returns without views. The view goes in ``structuredContent``, which apps
  with MCP Apps hand to the view.
- Views are off unless ``views.enabled`` is set. Off, the tools carry no UI
  metadata at all, so nothing changes for any app.
- Apps that do not declare MCP Apps support, results that are errors, and a
  server without the optional ``prefab-ui`` package get the plain result.

Views are built with Prefab (``prefab-ui``), FastMCP's component library,
and rendered by its renderer, which this server serves itself (``ui://``
resource) so nothing loads from a third-party CDN.
"""

from __future__ import annotations

import functools
import importlib.util
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp.tools.base import Tool

VIEW_URI = "ui://adloop/view.html"
VIEW_MIME_TYPE = "text/html;profile=mcp-app"


def _performance(result: dict, kwargs: dict) -> Any:
    from adloop.views.performance import campaign_performance_view

    return campaign_performance_view(
        result,
        date_range_start=str(kwargs.get("date_range_start") or ""),
        date_range_end=str(kwargs.get("date_range_end") or ""),
    )


# Tool name -> builder(result, tool kwargs) returning a PrefabApp.
VIEWS: dict[str, Callable[[dict, dict], Any]] = {
    "get_campaign_performance": _performance,
}


def prefab_installed() -> bool:
    return importlib.util.find_spec("prefab_ui") is not None


def views_enabled() -> bool:
    """Whether this server (or this workspace, when hosted) shows views."""
    try:
        from adloop.runtime import current_config

        return bool(current_config().views.enabled) and prefab_installed()
    except Exception:
        return False


def with_view(name: str, fn: Callable) -> Callable:
    """Wrap a read tool so card-capable apps also get its view."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if not isinstance(result, dict) or "error" in result or not views_enabled():
            return result
        from adloop.safety.approval import client_shows_cards

        if not client_shows_cards():
            return result
        try:
            app = VIEWS[name](result, kwargs)
        except Exception:  # noqa: BLE001 - a broken view must never cost the answer
            return result
        from fastmcp.tools import ToolResult

        # content=result is converted exactly like a plain dict return, so
        # the AI reads the same text with or without the view.
        return ToolResult(content=result, structured_content=app)

    return wrapper


def renderer_html() -> str:
    from prefab_ui.renderer import get_renderer_html

    return get_renderer_html("bundled")


try:
    from fastmcp.server.transforms import Transform
except ImportError:  # pragma: no cover - FastMCP without transforms
    Transform = object  # type: ignore[assignment,misc]


class ViewMeta(Transform):  # type: ignore[misc,valid-type]
    """Point the view tools at the view resource while views are on.

    Read per request, so a hosted server can decide per workspace. Apps
    cache the tool list, so switching views on or off needs a reconnect.
    """

    def _apply(self, tool: Tool) -> Tool:
        if tool.name not in VIEWS or not views_enabled():
            return tool
        meta = dict(tool.meta or {})
        meta["ui"] = {**(meta.get("ui") or {}), "resourceUri": VIEW_URI}
        return tool.model_copy(update={"meta": meta})

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._apply(tool) for tool in tools]

    async def get_tool(self, name, call_next, *, version=None):
        tool = await call_next(name, version=version)
        return self._apply(tool) if tool is not None else None
