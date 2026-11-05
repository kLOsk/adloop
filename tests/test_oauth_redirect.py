"""The one-shot localhost listener for sign-in redirects (oauth_redirect).

A connection that trickles its request must not hold the sign-in past its
deadline, nor keep a real redirect waiting behind it until then.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from adloop import oauth_redirect
from adloop.oauth_redirect import RedirectTimeout, listen_once


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connect(port: int) -> socket.socket:
    for _ in range(100):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.02)
    raise AssertionError("listener did not come up")


def _trickle(port: int, data: bytes, every: float, stop: threading.Event) -> None:
    try:
        sock = _connect(port)
    except AssertionError:
        return
    try:
        for byte in data:
            if stop.is_set():
                break
            sock.sendall(bytes([byte]))
            time.sleep(every)
    except OSError:
        pass  # the listener cut the connection off
    finally:
        sock.close()


def _run(uri: str, timeout: float) -> tuple[dict | Exception, float]:
    started = time.monotonic()
    try:
        result: dict | Exception = listen_once(uri, timeout, provider="Test")
    except RedirectTimeout as exc:
        result = exc
    return result, time.monotonic() - started


@pytest.mark.parametrize("data", [
    b"GET /callback?code=c1&state=s1 HTTP/1.1\r\n",            # a slow request line
    b"GET /callback HTTP/1.1\r\nHost: x\r\nX-Slow: " + b"a" * 40,  # slow headers
])
def test_a_trickled_request_cannot_outlast_the_deadline(data):
    port = _free_port()
    stop = threading.Event()
    worker = threading.Thread(target=_trickle, args=(port, data, 0.05, stop), daemon=True)
    worker.start()
    result, elapsed = _run(f"http://127.0.0.1:{port}/callback", 0.4)
    stop.set()
    assert isinstance(result, RedirectTimeout)
    assert elapsed < 1.2, f"the listener stayed {elapsed:.2f}s past a 0.4s deadline"


def test_a_real_redirect_behind_a_trickling_connection_is_served(monkeypatch):
    monkeypatch.setattr(oauth_redirect, "_PER_CONNECTION_SECONDS", 0.3)
    port = _free_port()
    stop = threading.Event()
    slow = threading.Thread(target=_trickle, args=(port, b"GET /callback HTTP/1.1\r\n" * 10, 0.05, stop),
                            daemon=True)
    slow.start()

    def real():
        time.sleep(0.1)
        sock = _connect(port)
        sock.sendall(b"GET /callback?code=c1&state=s1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        sock.recv(1024)
        sock.close()

    threading.Thread(target=real, daemon=True).start()
    result, elapsed = _run(f"http://127.0.0.1:{port}/callback", 5.0)
    stop.set()
    assert result == {"code": "c1", "state": "s1"}
    assert elapsed < 2.0
