"""Runtime patches for upstream ``mcp`` / ``fastmcp`` bugs.

This module exists to work around specific upstream defects that affect AdLoop
users in the wild. Every patch in here is a **temporary workaround** — the long-
term fix belongs upstream. Each entry below records exactly which upstream issue
it tracks and the condition under which this file should stop patching.

Currently tracked:

- **modelcontextprotocol/python-sdk#2416** — ``AssertionError: Request already
  responded to`` cancellation race in ``mcp`` 1.27.0. Reliably crashes the MCP
  server whenever the host (Claude Cowork, Claude Code post-update, etc.) sends
  ``notifications/cancelled`` while a synchronous tool handler is still running.
  We replace the two racing methods (``respond`` / ``cancel``) with guard-based
  versions that cannot double-send a JSON-RPC response.

  **Remove this patch** once ``mcp`` upstream ships a fix (watch the issue,
  check the installed source for the ``assert not self._completed`` line — if
  it's gone, we detect that automatically and skip patching).

Design goals:

1. **Self-removing.** Each patch inspects the upstream source first and bails
   out silently when the bug appears fixed. Upgrading the dependency is then
   enough to deactivate the workaround — no code changes needed in AdLoop.
2. **Idempotent.** ``install()`` is safe to call multiple times.
3. **Never fatal.** If patching itself fails (e.g. upstream restructured the
   module so inspection breaks), we log and continue — the server must start
   even if the patch can't be applied.
"""

from __future__ import annotations

import inspect
import sys
from typing import Any

from adloop import diagnostics


def _log(message: str) -> None:
    """Write a single patch-status line via the diagnostics channel.

    Falls back to a plain ``stderr`` write when diagnostics are disabled so
    maintainers can still see patch activity if they go looking.
    """
    line = f"[adloop-patches] {message}"
    if diagnostics.enabled():
        diagnostics._emit("patch", message=message)  # type: ignore[attr-defined]
        return
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _patch_request_responder_cancel_race() -> None:
    """Apply the fix for python-sdk issue #2416.

    Replaces ``RequestResponder.respond`` and ``RequestResponder.cancel`` with
    versions that guard against double-responding via a synchronous
    ``self._completed`` check before any ``await``. Exactly one of the two
    methods wins the race; the loser returns without sending a second JSON-RPC
    response, which in turn prevents the anyio TaskGroup from tearing the
    entire stdio transport down.

    Autodetects the upstream fix by inspecting the installed ``respond``
    source: if the signature ``assert not self._completed`` is no longer
    present, we assume upstream landed a fix and skip patching.
    """
    try:
        from mcp.shared import session as _session_module
    except Exception as exc:  # pragma: no cover — mcp is a hard dep
        _log(f"skip pysdk-2416: cannot import mcp.shared.session ({exc})")
        return

    responder_cls = getattr(_session_module, "RequestResponder", None)
    if responder_cls is None:
        _log("skip pysdk-2416: RequestResponder class not found")
        return

    try:
        original_respond_src = inspect.getsource(responder_cls.respond)
    except (OSError, TypeError) as exc:
        _log(f"skip pysdk-2416: cannot inspect respond() source ({exc})")
        return

    if "assert not self._completed" not in original_respond_src:
        # Upstream fixed the race (or moved it). No need to patch further.
        _log("skip pysdk-2416: upstream source no longer contains the racey assert")
        return

    if getattr(responder_cls, "_adloop_2416_patched", False):
        return

    async def respond(self: Any, response: Any) -> None:
        """Patched RequestResponder.respond — see python-sdk#2416."""
        if not self._entered:
            raise RuntimeError("RequestResponder must be used as a context manager")
        # Upstream asserts here; we return early instead. If a concurrent
        # cancel() already marked the request complete, silently drop this
        # response — the cancel path has already sent one error response and a
        # second JSON-RPC reply for the same request ID would be a protocol
        # violation as well as crashing the TaskGroup on the assert.
        if self._completed:
            return
        if not self.cancelled:
            self._completed = True
            await self._session._send_response(
                request_id=self.request_id, response=response
            )

    async def cancel(self: Any) -> None:
        """Patched RequestResponder.cancel — see python-sdk#2416."""
        if not self._entered:
            raise RuntimeError("RequestResponder must be used as a context manager")
        if not self._cancel_scope:
            raise RuntimeError("No active cancel scope")

        self._cancel_scope.cancel()
        # Symmetrical guard: if the handler's respond() already ran before the
        # cancel notification arrived, don't send a second response.
        if self._completed:
            return
        self._completed = True

        from mcp.types import ErrorData  # imported lazily to avoid startup cost

        await self._session._send_response(
            request_id=self.request_id,
            response=ErrorData(code=0, message="Request cancelled", data=None),
        )

    responder_cls.respond = respond
    responder_cls.cancel = cancel
    responder_cls._adloop_2416_patched = True  # type: ignore[attr-defined]

    _log(
        "applied pysdk-2416: RequestResponder.respond/cancel guarded against "
        "cancellation double-respond race"
    )


_INSTALLED = False


def install() -> None:
    """Apply all AdLoop runtime patches. Idempotent.

    Call this exactly once at server startup, before ``FastMCP`` begins
    accepting messages. Calling it multiple times is safe — we gate on a
    module-level flag and each individual patch also has its own reentry
    guard.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _patch_request_responder_cancel_race()
