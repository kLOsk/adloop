"""Localhost listener for OAuth redirects (sign-in commands).

``adloop microsoft-login`` opens the provider's sign-in page in the
browser; the provider then redirects to
``http://localhost:<port>/<path>?code=...&state=...``. The listener binds a
free port first (Entra, like RFC 8252 asks, ignores the port of a localhost
redirect URI), so the redirect URI is only known once it is listening:
:class:`RedirectListener` binds, then names its ``redirect_uri``, then waits.

It serves requests until one carries the sign-in's ``state`` (a stray or
forged request is answered and ignored), and is hardened against a stuck
sign-in: the overall deadline uses a monotonic clock, and every socket read
is bounded, so an accepted connection that never sends a request cannot hold
the command open.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qsl, urlsplit


class RedirectTimeout(Exception):
    """No redirect arrived before the deadline."""


# How long one connection may take to send its request. A client that
# trickles bytes is cut off here, so a real redirect waiting behind it is
# still served before the sign-in deadline.
_PER_CONNECTION_SECONDS = 5.0


class _DeadlineReader:
    """The request side of a connection, with an absolute deadline.

    A per-read socket timeout alone can be outlasted by a peer that sends a
    byte just before each read times out; this bounds the whole request.
    """

    def __init__(self, sock, deadline: float):
        self._sock = sock
        self._deadline = deadline
        self._buffer = b""

    def _fill(self) -> bool:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the request took too long")
        self._sock.settimeout(remaining)
        chunk = self._sock.recv(4096)
        if not chunk:
            return False
        self._buffer += chunk
        return True

    def readline(self, limit: int = -1) -> bytes:
        while b"\n" not in self._buffer and (limit < 0 or len(self._buffer) < limit):
            if not self._fill():
                break
        end = self._buffer.find(b"\n")
        size = end + 1 if end >= 0 else len(self._buffer)
        if limit >= 0:
            size = min(size, limit)
        line, self._buffer = self._buffer[:size], self._buffer[size:]
        return line

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self._buffer) < size) and self._fill():
            pass
        size = len(self._buffer) if size < 0 else size
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def close(self) -> None:
        return None


class RedirectListener:
    """One sign-in's redirect listener on localhost; ``port=0`` picks a free port."""

    def __init__(self, *, host: str = "localhost", path: str = "/callback", port: int = 0, provider: str = "the"):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self.path = path or "/"
        self._deadline = 0.0
        self._state: str | None = None
        self.captured: dict = {}
        listener = self
        page = (
            "<html><body style='font-family:sans-serif'><h3>AdLoop received the "
            f"{provider} sign-in.</h3><p>You can close this tab and return to the terminal.</p></body></html>"
        ).encode()
        stray = (
            b"<html><body style='font-family:sans-serif'><h3>This is not the sign-in AdLoop is waiting "
            b"for.</h3><p>Finish the sign-in in the tab AdLoop opened.</p></body></html>"
        )

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                # The whole request must arrive within the connection's budget
                # and the sign-in deadline, however slowly it trickles in.
                self.rfile = _DeadlineReader(
                    self.connection, min(listener._deadline, time.monotonic() + _PER_CONNECTION_SECONDS))

            def do_GET(self):  # noqa: N802 (http.server naming)
                here = urlsplit(self.path)
                if (here.path or "/") != listener.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = dict(parse_qsl(here.query))
                ours = listener._state is None or query.get("state") == listener._state
                self.send_response(200 if ours else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page if ours else stray)
                if ours:
                    listener.captured.update(query)

            def log_message(self, *_args):
                return

        class Server(HTTPServer):
            def handle_error(self, request, client_address):  # noqa: D401 — silence timeouts
                return

        self._handler = Handler
        self._server = Server((host or "127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self.redirect_uri = f"http://{host}:{self.port}{self.path}"

    def wait(self, timeout: float, *, state: str | None = None) -> dict:
        """Serve until a request with ``state`` (any, when None) arrives; return its query."""
        self._state = state
        self._deadline = time.monotonic() + timeout
        # An accepted connection that never sends a request must not hold the
        # sign-in past its deadline: every socket read is bounded too.
        self._handler.timeout = max(0.1, min(5.0, timeout))
        while not self.captured:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                break
            self._server.timeout = min(1.0, remaining)
            self._server.handle_request()
        if not self.captured:
            raise RedirectTimeout("No sign-in arrived before the timeout.")
        return self.captured

    def close(self) -> None:
        self._server.server_close()

    def __enter__(self) -> RedirectListener:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def listen_once(redirect_uri: str, timeout: float, *, provider: str = "the", state: str | None = None) -> dict:
    """Serve a fixed redirect URI until the browser arrives; return its query."""
    parts = urlsplit(redirect_uri)
    with RedirectListener(host=parts.hostname or "127.0.0.1", path=parts.path or "/",
                          port=parts.port or 80, provider=provider) as listener:
        return listener.wait(timeout, state=state)
