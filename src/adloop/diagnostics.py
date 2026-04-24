"""Optional diagnostic instrumentation for debugging MCP host disconnects.

Activated only when the environment variable ``ADLOOP_DEBUG`` is set to a
truthy value (``1``, ``true``, ``yes``). Otherwise all hooks are no-ops and
impose no runtime cost.

When enabled, this module emits structured ``[adloop-debug]`` lines to
**stderr** (never stdout — stdout is the MCP channel). The output is designed
to answer a specific question: when the MCP server process disappears without
a Python exception, did it exit via signal, EOF on stdin, or SIGKILL?

Emitted events:

- ``started`` — once, at server startup, including pid and python version.
- ``heartbeat`` — every ``ADLOOP_HEARTBEAT_SECONDS`` (default 30s) with uptime,
  time since last tool call, and peak RSS.
- ``tool_start`` / ``tool_end`` — bracketing every MCP tool invocation, with
  the tool name and duration.
- ``signal`` — on SIGTERM/SIGHUP/SIGINT/SIGPIPE, immediately before the signal
  propagates to Python's default handler (which then terminates the process).
- ``atexit`` — if the interpreter shuts down normally.

If stderr goes silent between two heartbeats and no ``signal`` or ``atexit``
line appears, the process was SIGKILL'd or the stdio pipe was torn down
without giving Python a chance to run handlers. That's the diagnostic signal
we care about.
"""

from __future__ import annotations

import atexit
import functools
import os
import signal
import sys
import threading
import time
from typing import Callable

_ENABLED = os.getenv("ADLOOP_DEBUG", "").lower() in ("1", "true", "yes", "on")
_HEARTBEAT_SECONDS = int(os.getenv("ADLOOP_HEARTBEAT_SECONDS", "30") or "30")

_start_time: float = time.monotonic()
_last_activity_time: float = time.monotonic()
_last_activity_label: str = "startup"
_activity_lock = threading.Lock()


def enabled() -> bool:
    """Return True if debug diagnostics are active."""
    return _ENABLED


def _uptime() -> float:
    return time.monotonic() - _start_time


def _time_since_activity() -> float:
    return time.monotonic() - _last_activity_time


def _rss_mb() -> float | None:
    """Return peak resident set size in MB, or None if unavailable."""
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None
    # macOS reports bytes, Linux reports kilobytes.
    if sys.platform == "darwin":
        return rss / (1024.0 * 1024.0)
    return rss / 1024.0


def _emit(event: str, **fields: object) -> None:
    """Write a single diagnostic line to stderr (never stdout)."""
    parts = [f"[adloop-debug] event={event}", f"uptime={_uptime():.2f}s"]
    for k, v in fields.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:.2f}")
        else:
            parts.append(f"{k}={v}")
    try:
        sys.stderr.write(" ".join(parts) + "\n")
        sys.stderr.flush()
    except Exception:
        # Stderr might be closed during shutdown — never raise from the logger.
        pass


def mark_activity(label: str) -> None:
    """Record that the server produced or started handling traffic."""
    if not _ENABLED:
        return
    global _last_activity_time, _last_activity_label
    with _activity_lock:
        _last_activity_time = time.monotonic()
        _last_activity_label = label


def wrap_tool(fn: Callable) -> Callable:
    """Wrap an MCP tool callable so its start/end events are logged.

    No-op when diagnostics are disabled, so there's zero overhead in production.
    """
    if not _ENABLED:
        return fn

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        name = getattr(fn, "__name__", "tool")
        t0 = time.monotonic()
        mark_activity(f"tool_start:{name}")
        _emit("tool_start", tool=name)
        try:
            result = fn(*args, **kwargs)
            return result
        finally:
            dt = time.monotonic() - t0
            mark_activity(f"tool_end:{name}")
            _emit("tool_end", tool=name, duration_s=dt)

    return wrapper


def _heartbeat_loop() -> None:
    """Daemon thread that emits a periodic liveness line."""
    while True:
        time.sleep(_HEARTBEAT_SECONDS)
        try:
            _emit(
                "heartbeat",
                idle_s=_time_since_activity(),
                last_activity=_last_activity_label,
                rss_mb=_rss_mb() if _rss_mb() is not None else "unknown",
            )
        except Exception:
            return


def _install_signal_handlers() -> None:
    """Log signal receipt, then let Python's default handler take over.

    We can't meaningfully prevent termination; the goal is only to prove that
    a signal was received (vs. silent SIGKILL or EOF).
    """
    def make_handler(signum: int):
        def handler(_signum, _frame):
            try:
                name = signal.Signals(signum).name
            except Exception:
                name = str(signum)
            _emit(
                "signal",
                name=name,
                idle_s=_time_since_activity(),
                last_activity=_last_activity_label,
                rss_mb=_rss_mb() if _rss_mb() is not None else "unknown",
            )
            # Restore default behavior and re-raise the signal against ourselves
            # so termination semantics (exit code, core dump, etc.) are preserved.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        return handler

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        try:
            signal.signal(sig, make_handler(sig))
        except (ValueError, OSError):
            # Some signals can't be installed from non-main threads or on
            # certain platforms — skip silently.
            pass


def _install_atexit() -> None:
    def _on_exit():
        _emit(
            "atexit",
            idle_s=_time_since_activity(),
            last_activity=_last_activity_label,
            rss_mb=_rss_mb() if _rss_mb() is not None else "unknown",
        )

    atexit.register(_on_exit)


def install() -> None:
    """Enable all diagnostic hooks. Safe to call multiple times (idempotent)."""
    if not _ENABLED:
        return

    if getattr(install, "_installed", False):
        return
    install._installed = True  # type: ignore[attr-defined]

    _emit(
        "started",
        pid=os.getpid(),
        python=sys.version.split()[0],
        heartbeat_s=_HEARTBEAT_SECONDS,
        platform=sys.platform,
    )
    _install_signal_handlers()
    _install_atexit()

    thread = threading.Thread(
        target=_heartbeat_loop,
        name="adloop-debug-heartbeat",
        daemon=True,
    )
    thread.start()
