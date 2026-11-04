"""One-shot localhost listener for OAuth redirects (sign-in commands).

``adloop roku-login`` and ``adloop microsoft-login`` open the provider's
sign-in page in the browser; the provider then redirects to
``http://localhost:<port>/<path>?code=...&state=...``. This serves that one
request and returns its query parameters.

Hardened against a stuck sign-in: the overall deadline uses a monotonic
clock, and every socket read is bounded, so an accepted connection that
never sends a request cannot hold the command open.
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


def is_local_redirect(redirect_uri: str) -> bool:
    parts = urlsplit(redirect_uri)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def listen_once(redirect_uri: str, timeout: float, *, provider: str = "the") -> dict:
    """Serve the redirect URI on localhost until the browser arrives; return its query."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    parts = urlsplit(redirect_uri)
    expected_path = parts.path or "/"
    captured: dict = {}
    page = (
        "<html><body style='font-family:sans-serif'><h3>AdLoop received the "
        f"{provider} sign-in.</h3><p>You can close this tab and return to the terminal.</p></body></html>"
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            # The whole request must arrive within the connection's budget
            # and the sign-in deadline, however slowly it trickles in.
            self.rfile = _DeadlineReader(
                self.connection, min(deadline, time.monotonic() + _PER_CONNECTION_SECONDS))

        def do_GET(self):  # noqa: N802 (http.server naming)
            here = urlsplit(self.path)
            if (here.path or "/") != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            captured.update(dict(parse_qsl(here.query)))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            return

    # An accepted connection that never sends a request must not hold the
    # sign-in past its deadline: every socket read is bounded too.
    Handler.timeout = max(0.1, min(5.0, timeout))

    class Server(HTTPServer):
        def handle_error(self, request, client_address):  # noqa: D401 — silence timeouts
            return

    server = Server((parts.hostname or "127.0.0.1", parts.port or 80), Handler)
    deadline = time.monotonic() + timeout
    try:
        while not captured:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = min(1.0, remaining)
            server.handle_request()
    finally:
        server.server_close()
    if not captured:
        raise RedirectTimeout("No sign-in arrived before the timeout.")
    return captured
