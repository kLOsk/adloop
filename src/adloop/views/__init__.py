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
# Added to the AI's copy of a result that is shown as a view, so the reply
# comments on the dashboard instead of rebuilding it as a table. "Usually":
# claude.ai draws no view for a call folded into a run of several tool calls.
VIEW_NOTE = (
    "The app usually shows the user this result as an interactive dashboard: "
    "headline numbers, alerts, spend per campaign and a sortable table of every "
    "campaign. Say what stands out and what to do next instead of repeating "
    "those numbers as a table."
)
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

        # content is converted exactly like a plain dict return: the AI reads
        # the same result, plus a note that the user is looking at the view.
        return ToolResult(content={**result, "view_note": VIEW_NOTE}, structured_content=app)

    return wrapper


# Where a hosted runtime serves the renderer files (see set_renderer_base).
_renderer_base = ""

_STUB = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AdLoop view</title>
  <link rel="stylesheet" crossorigin href="{base}/{digest}/renderer.css">
  <script type="module" crossorigin src="{base}/{digest}/renderer.js"></script>
</head>
<body>
  <div id="root"></div>
</body>
</html>
"""


def set_renderer_base(url: str) -> None:
    """Serve the renderer from a host instead of inlining it in every view.

    Self-hosted, the view resource carries Prefab's whole renderer (about
    7 MB) and the app loads nothing from anywhere else. A hosted runtime can
    serve the same renderer as two files (``renderer_assets``) under ``url``
    at ``<url>/<digest>/renderer.js`` and ``renderer.css``. The resource is
    then a small page loading them, and the app's browser caches them across
    conversations. Empty switches back to the inline renderer.
    """
    global _renderer_base
    _renderer_base = str(url or "").rstrip("/")


@functools.lru_cache(maxsize=1)
def renderer_assets() -> tuple[str, dict[str, tuple[bytes, str]]]:
    """Prefab's bundled renderer split into the files a host serves.

    Returns ``(digest, {"renderer.js": (bytes, type), "renderer.css": ...})``.
    The digest names this renderer build, so a host can cache the files
    forever: a new prefab-ui release changes the digest, and with it the
    URLs the view resource points at.
    """
    import hashlib

    from prefab_ui.renderer import get_renderer_html

    html = get_renderer_html("bundled")
    # Vite inlines one module script and one stylesheet. The script escapes
    # "</script>" in its own strings, so its first literal closing tag is the
    # real one (the same rule prefab_ui.renderer relies on).
    opening = '<script type="module" crossorigin>'
    start = html.index(opening) + len(opening)
    end = html.index("</script>", start)
    script = html[start:end]
    style_open = html.index("<style", end)
    style_start = html.index(">", style_open) + 1
    style = html[style_start : html.index("</style>", style_start)]

    js, css = script.encode(), style.encode()
    digest = hashlib.sha256(js + b"\0" + css).hexdigest()[:16]
    return digest, {
        "renderer.js": (js, "text/javascript; charset=utf-8"),
        "renderer.css": (css, "text/css; charset=utf-8"),
    }


def renderer_html() -> str:
    """The page the view resource serves: hosted files, or all inline."""
    if _renderer_base:
        digest, _files = renderer_assets()
        return _STUB.format(base=_renderer_base, digest=digest)
    from prefab_ui.renderer import get_renderer_html

    return get_renderer_html("bundled")


def renderer_meta() -> dict:
    """The resource's MCP Apps metadata, including what the page may load."""
    ui: dict = {"prefersBorder": False}
    if _renderer_base:
        from urllib.parse import urlsplit

        parts = urlsplit(_renderer_base)
        ui["csp"] = {"resourceDomains": [f"{parts.scheme}://{parts.netloc}"]}
    return {"ui": ui}


def view_resource() -> Any:
    """The ``ui://adloop/view.html`` read, with its metadata decided per read."""
    from fastmcp.resources import ResourceContent, ResourceResult

    return ResourceResult([ResourceContent(renderer_html(), mime_type=VIEW_MIME_TYPE, meta=renderer_meta())])


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
