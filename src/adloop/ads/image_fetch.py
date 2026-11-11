"""Fetch ad images from public URLs without letting the fetch reach private hosts.

The fetched bytes are uploaded into the user's Google Ads account, where
they become visible. A fetch that could be steered at an internal address
(cloud metadata, a database admin page, a loopback service) would therefore
be a way to exfiltrate whatever lives there. So this fetcher closes the
DNS-rebinding gap (the reachability check for landing pages reuses its
pinned connections):

* the hostname is resolved exactly once per request, and every resolved
  address must be public;
* the TCP connection goes to that resolved address (pinning) — the socket
  never resolves the name again — while the request still carries the
  original ``Host`` header and, for https, TLS uses the original hostname
  for SNI and certificate verification;
* redirects are followed by hand (at most ``MAX_REDIRECTS``), each hop
  re-validated and re-pinned;
* the body is streamed under a hard size cap and a wall-clock deadline;
* the content is trusted only after sniffing the bytes, never by
  ``Content-Type``.

No cookies, credentials or auth headers are ever sent.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

# Google Ads rejects image assets above 5120 KB.
MAX_IMAGE_BYTES = 5120 * 1024
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10.0

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE = 64 * 1024
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class ImageFetchError(ValueError):
    """The image URL could not be fetched safely or is not a usable image."""


@dataclass(frozen=True)
class FetchedImage:
    """Bytes fetched from ``url`` (after following ``final_url`` redirects)."""

    url: str
    final_url: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def non_public_reason(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str | None:
    """Return why ``addr`` must not be fetched from, or None if it is public.

    IPv6 forms that embed an IPv4 address (IPv4-mapped, 6to4, Teredo,
    NAT64) are judged by the embedded address too, so ``::ffff:127.0.0.1``
    cannot slip past as "an IPv6 address that isn't loopback".
    """
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            # The socket really talks IPv4 to the mapped address, so that
            # address alone decides.
            inner_reason = non_public_reason(addr.ipv4_mapped)
            if inner_reason is not None:
                return f"maps to {addr.ipv4_mapped}, {inner_reason}"
            return None
        embedded = [addr.sixtofour, *(addr.teredo or ())]
        if addr in _NAT64_PREFIX:
            embedded.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        for inner in embedded:
            if inner is not None and non_public_reason(inner) is not None:
                return f"embeds non-public address {inner}"

    for label in (
        "unspecified",
        "loopback",
        "link_local",
        "multicast",
        "private",
        "reserved",
    ):
        if getattr(addr, f"is_{label}"):
            return label.replace("_", "-")
    if not addr.is_global:
        return "not globally routable"
    return None


def resolve_public_address(host: str, port: int) -> str:
    """Resolve ``host`` once and return the address to connect to.

    Raises ImageFetchError when the name does not resolve or when ANY of
    its addresses is non-public — a hostname that mixes public and private
    records is refused outright rather than having one picked.
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        except (OSError, UnicodeError) as exc:
            raise ImageFetchError(f"hostname {host!r} does not resolve: {exc}") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError as exc:
                raise ImageFetchError(
                    f"hostname {host!r} resolved to an unusable address {info[4][0]!r}"
                ) from exc
        if not addresses:
            raise ImageFetchError(f"hostname {host!r} has no addresses")

    for addr in addresses:
        reason = non_public_reason(addr)
        if reason is not None:
            raise ImageFetchError(
                f"{host} resolves to a non-public address ({addr}, {reason}) — "
                "refusing to fetch"
            )
    return str(addresses[0])


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a pre-resolved IP instead of ``host``.

    ``self.host`` stays the original hostname, so http.client still sends
    the right ``Host`` header.
    """

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a pre-resolved IP.

    TLS still uses the original hostname for SNI and certificate
    verification, so pinning cannot be abused to accept a certificate for
    a different name.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self.pinned_ip = pinned_ip
        self.tls_context = context

    def connect(self) -> None:
        raw = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _tls_context() -> ssl.SSLContext:
    """Default verifying context: CERT_REQUIRED + hostname checking."""
    return ssl.create_default_context()


def _user_agent() -> str:
    from adloop import __version__

    return f"AdLoop/{__version__} (image asset fetch)"


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str


def _parse_target(url: str) -> _Target:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ImageFetchError(f"unparseable URL {url!r}: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ImageFetchError(
            f"unsupported URL scheme {parts.scheme!r} in {url!r} (only http/https)"
        )
    if parts.username is not None or parts.password is not None:
        raise ImageFetchError(f"URL {url!r} contains credentials — refusing to fetch")
    host = parts.hostname
    if not host:
        raise ImageFetchError(f"URL {url!r} has no hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ImageFetchError(f"invalid hostname in {url!r}: {exc}") from exc

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _Target(
        scheme=scheme,
        host=host,
        port=port or (443 if scheme == "https" else 80),
        path=path,
    )


def _open_connection(target: _Target, pinned_ip: str) -> http.client.HTTPConnection:
    if target.scheme == "https":
        return _PinnedHTTPSConnection(
            target.host,
            target.port,
            pinned_ip=pinned_ip,
            timeout=TIMEOUT_SECONDS,
            context=_tls_context(),
        )
    return _PinnedHTTPConnection(
        target.host, target.port, pinned_ip=pinned_ip, timeout=TIMEOUT_SECONDS
    )


def _read_capped(response: http.client.HTTPResponse, url: str, deadline: float) -> bytes:
    """Stream the body, aborting the moment it exceeds the size cap."""
    declared = response.getheader("Content-Length")
    if declared is not None and declared.strip().isdigit():
        if int(declared) > MAX_IMAGE_BYTES:
            raise ImageFetchError(
                f"image at {url} is {int(declared) // 1024} KB; Google Ads "
                f"accepts at most {MAX_IMAGE_BYTES // 1024} KB"
            )

    read = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise ImageFetchError(
                f"fetching {url} took longer than {TIMEOUT_SECONDS:.0f}s"
            )
        # read1 returns whatever has arrived instead of waiting for a full
        # chunk, so a server trickling a byte at a time cannot outlast the
        # deadline: the per-recv socket timeout alone would never fire.
        chunk = read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ImageFetchError(
                f"image at {url} exceeds {MAX_IMAGE_BYTES // 1024} KB, the "
                "Google Ads image asset limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_image_bytes(url: str) -> FetchedImage:
    """Fetch ``url`` safely and return the raw bytes (not yet sniffed)."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        target = _parse_target(current)
        pinned_ip = resolve_public_address(target.host, target.port)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        connection = _open_connection(target, pinned_ip)
        try:
            connection.request(
                "GET",
                target.path,
                headers={
                    "User-Agent": _user_agent(),
                    "Accept": "image/png, image/jpeg, image/gif",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise ImageFetchError(
                        f"{current} answered HTTP {response.status} without a Location"
                    )
                current = urljoin(current, location.strip())
                continue
            if response.status != 200:
                raise ImageFetchError(f"{current} answered HTTP {response.status}")
            data = _read_capped(response, current, deadline)
        except ImageFetchError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise ImageFetchError(f"could not fetch {current}: {exc}") from exc
        finally:
            connection.close()
        return FetchedImage(url=url, final_url=current, data=data)

    raise ImageFetchError(f"{url} redirected more than {MAX_REDIRECTS} times")
