"""Image assets from URLs: the safe fetcher and the draft/apply flow around it.

Everything here is offline. ``FakeNet`` replaces ``socket.getaddrinfo`` and
``socket.create_connection`` so each test decides what a hostname resolves
to and what the "server" at an address answers — and can assert exactly
which address a connection went to.
"""

from __future__ import annotations

import base64
import hashlib
import io
import socket
import ssl
import struct

import pytest

from adloop import runtime
from adloop.ads import image_fetch, write
from adloop.ads.image_fetch import MAX_IMAGE_BYTES, ImageFetchError, fetch_image_bytes
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import get_plan
from tests.test_ads_write import (
    _FakeClient,
    _FakeGoogleAdsService,
    _FakeMutateOperationResponse,
    _FakePathService,
)
from tests.test_validate_only import FakeAdsClient

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2ZfZ0AAAAASUVORK5CYII="
)

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "151.101.1.69"


def png(width: int, height: int, tail: bytes = b"") -> bytes:
    """A PNG signature + IHDR header — all the sniffer needs."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + tail
    )


def http_response(
    status: int = 200,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    reason: str = "OK",
) -> bytes:
    lines = [f"HTTP/1.1 {status} {reason}"]
    all_headers = {"Content-Length": str(len(body)), "Connection": "close"}
    all_headers.update(headers or {})
    lines += [f"{k}: {v}" for k, v in all_headers.items() if v is not None]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class StreamingBody(io.RawIOBase):
    """Serves a header block then ``total`` body bytes, counting what's read."""

    def __init__(self, head: bytes, first: bytes, total: int):
        self._head = head + first
        self._remaining_body = total - len(first)
        self.served = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._head:
            n = min(len(buffer), len(self._head))
            buffer[:n] = self._head[:n]
            self._head = self._head[n:]
        elif self._remaining_body > 0:
            n = min(len(buffer), self._remaining_body)
            buffer[:n] = b"\0" * n
            self._remaining_body -= n
        else:
            return 0
        self.served += n
        return n


class FakeSocket:
    def __init__(self, net: FakeNet, address: tuple[str, int]):
        self.net = net
        self.address = address
        self.sent = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str = "rb", *args, **kwargs):
        self.net.requests.append((self.address, self.sent))
        answer = self.net.servers[self.address](self.sent)
        if isinstance(answer, bytes):
            return io.BytesIO(answer)
        return io.BufferedReader(answer)

    def settimeout(self, _timeout) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeNet:
    """Fake DNS + TCP. ``dns[host]`` is a list of successive answers."""

    def __init__(self, monkeypatch):
        self.dns: dict[str, list[list[str]]] = {}
        self.servers: dict[tuple[str, int], object] = {}
        self.lookups: list[str] = []
        self.connections: list[tuple[str, int]] = []
        self.requests: list[tuple[tuple[str, int], bytes]] = []
        monkeypatch.setattr(socket, "getaddrinfo", self.getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", self.create_connection)

    def resolve(self, host: str, *answers: list[str]) -> None:
        self.dns[host] = [list(answer) for answer in answers]

    def serve(self, ip: str, port: int, handler) -> None:
        if isinstance(handler, bytes):
            payload = handler
            handler = lambda _request: payload  # noqa: E731
        self.servers[(ip, port)] = handler

    def getaddrinfo(self, host, port, *args, **kwargs):
        self.lookups.append(host)
        answers = self.dns.get(host)
        if not answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        result = []
        for ip in answer:
            if ":" in ip:
                result.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)))
            else:
                result.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
        return result

    def create_connection(self, address, timeout=None, *args, **kwargs):
        address = (address[0], address[1])
        self.connections.append(address)
        if address not in self.servers:
            raise ConnectionRefusedError(f"fake: nothing listens on {address}")
        return FakeSocket(self, address)


@pytest.fixture
def net(monkeypatch) -> FakeNet:
    return FakeNet(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_state():
    runtime.set_deployment_mode("local")
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    runtime.set_deployment_mode("local")
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")),
    )


# ---------------------------------------------------------------------------
# The fetcher
# ---------------------------------------------------------------------------


class TestFetcher:
    def test_fetches_from_the_resolved_address_with_the_original_host(self, net):
        net.resolve("img.example.com", [PUBLIC_IP])
        net.serve(PUBLIC_IP, 80, http_response(body=PNG_1X1, headers={"Content-Type": "image/png"}))

        fetched = fetch_image_bytes("http://img.example.com/banners/hero.png?v=2")

        assert fetched.data == PNG_1X1
        assert fetched.sha256 == hashlib.sha256(PNG_1X1).hexdigest()
        assert net.connections == [(PUBLIC_IP, 80)]
        request = net.requests[0][1].decode()
        assert request.startswith("GET /banners/hero.png?v=2 HTTP/1.1\r\n")
        assert "\r\nHost: img.example.com\r\n" in request
        assert "\r\nUser-Agent: AdLoop/" in request
        assert "cookie" not in request.lower()
        assert "authorization" not in request.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/a.png",
            "http://10.1.2.3/a.png",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/a.png",
            "http://[::ffff:127.0.0.1]/a.png",
            "http://[::ffff:169.254.169.254]/a.png",
            "http://100.64.0.1/a.png",
            "http://0.0.0.0/a.png",
        ],
    )
    def test_refuses_non_public_ip_literals(self, net, url):
        with pytest.raises(ImageFetchError, match="non-public"):
            fetch_image_bytes(url)
        assert net.connections == []

    @pytest.mark.parametrize("private_ip", ["169.254.169.254", "10.0.0.5", "127.0.0.1", "fd00::1"])
    def test_refuses_hostnames_resolving_to_non_public_addresses(self, net, private_ip):
        net.resolve("evil.example.com", [private_ip])
        net.serve(private_ip, 80, http_response(body=PNG_1X1))

        with pytest.raises(ImageFetchError, match="non-public"):
            fetch_image_bytes("http://evil.example.com/a.png")
        assert net.connections == []

    def test_refuses_a_hostname_if_any_address_is_private(self, net):
        net.resolve("mixed.example.com", [PUBLIC_IP, "10.0.0.5"])
        net.serve(PUBLIC_IP, 80, http_response(body=PNG_1X1))

        with pytest.raises(ImageFetchError, match="10.0.0.5"):
            fetch_image_bytes("http://mixed.example.com/a.png")
        assert net.connections == []

    def test_dns_rebinding_cannot_move_the_connection(self, net):
        """First answer public, every later answer private: the connection
        must go to the first (pinned) address and nothing re-resolves."""
        net.resolve("rebind.example.com", [PUBLIC_IP], ["169.254.169.254"])
        net.serve(PUBLIC_IP, 80, http_response(body=PNG_1X1))
        net.serve("169.254.169.254", 80, http_response(body=b"SECRET-CREDENTIALS"))

        fetched = fetch_image_bytes("http://rebind.example.com/a.png")

        assert fetched.data == PNG_1X1
        assert net.lookups == ["rebind.example.com"]
        assert net.connections == [(PUBLIC_IP, 80)]

    def test_https_pins_the_ip_but_verifies_tls_against_the_hostname(self, net, monkeypatch):
        wrapped = []

        class FakeContext:
            # Python 3.11's HTTPSConnection reads these in __init__.
            verify_mode = ssl.CERT_REQUIRED
            check_hostname = True

            def wrap_socket(self, sock, server_hostname=None):
                wrapped.append((sock.address, server_hostname))
                return sock

        monkeypatch.setattr(image_fetch, "_tls_context", FakeContext)
        net.resolve("cdn.example.com", [PUBLIC_IP], ["10.0.0.9"])
        net.serve(PUBLIC_IP, 443, http_response(body=PNG_1X1))

        fetched = fetch_image_bytes("https://cdn.example.com/a.png")

        assert fetched.data == PNG_1X1
        assert net.connections == [(PUBLIC_IP, 443)]
        assert wrapped == [((PUBLIC_IP, 443), "cdn.example.com")]
        assert b"\r\nHost: cdn.example.com\r\n" in net.requests[0][1]

    def test_the_real_tls_context_verifies_certificates_and_hostnames(self):
        context = image_fetch._tls_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_redirects_are_followed_and_each_hop_is_re_pinned(self, net):
        net.resolve("short.example.com", [PUBLIC_IP])
        net.resolve("cdn.example.net", [OTHER_PUBLIC_IP])
        net.serve(
            PUBLIC_IP,
            80,
            http_response(302, headers={"Location": "http://cdn.example.net/real.png"}, reason="Found"),
        )
        net.serve(OTHER_PUBLIC_IP, 80, http_response(body=PNG_1X1))

        fetched = fetch_image_bytes("http://short.example.com/x")

        assert fetched.final_url == "http://cdn.example.net/real.png"
        assert net.connections == [(PUBLIC_IP, 80), (OTHER_PUBLIC_IP, 80)]
        assert b"\r\nHost: cdn.example.net\r\n" in net.requests[1][1]

    @pytest.mark.parametrize(
        "location, internal_ip",
        [
            ("http://internal.example.com/admin.png", "10.0.0.7"),
            ("http://169.254.169.254/latest/meta-data/iam/", "169.254.169.254"),
        ],
    )
    def test_a_redirect_to_a_private_address_is_refused(self, net, location, internal_ip):
        net.resolve("img.example.com", [PUBLIC_IP])
        net.resolve("internal.example.com", ["10.0.0.7"])
        net.serve(PUBLIC_IP, 80, http_response(301, headers={"Location": location}, reason="Moved"))
        net.serve(internal_ip, 80, http_response(body=PNG_1X1))

        with pytest.raises(ImageFetchError, match="non-public"):
            fetch_image_bytes("http://img.example.com/a.png")
        assert net.connections == [(PUBLIC_IP, 80)]

    def test_more_than_three_redirects_are_refused(self, net):
        net.resolve("loop.example.com", [PUBLIC_IP])
        net.serve(PUBLIC_IP, 80, http_response(302, headers={"Location": "/again"}, reason="Found"))

        with pytest.raises(ImageFetchError, match="redirected more than 3 times"):
            fetch_image_bytes("http://loop.example.com/start")
        assert len(net.connections) == 4

    def test_an_oversized_body_is_aborted_mid_stream(self, net):
        total = 50 * 1024 * 1024
        head = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nConnection: close\r\n\r\n"
        streams = []

        def handler(_request):
            stream = StreamingBody(head, png(1200, 628), total)
            streams.append(stream)
            return stream

        net.resolve("big.example.com", [PUBLIC_IP])
        net.serve(PUBLIC_IP, 80, handler)

        with pytest.raises(ImageFetchError, match="5120 KB"):
            fetch_image_bytes("http://big.example.com/huge.png")
        # Stopped right after crossing the cap, not after downloading 50 MB.
        assert streams[0].served < MAX_IMAGE_BYTES + 256 * 1024

    def test_a_trickling_server_cannot_outlast_the_deadline(self, net, monkeypatch):
        """One byte per recv, each just under the socket timeout: only the
        overall deadline stops it, so it must be checked per packet."""
        clock = {"now": 1000.0}
        monkeypatch.setattr(image_fetch.time, "monotonic", lambda: clock["now"])

        class Trickle(io.RawIOBase):
            def __init__(self, head: bytes, body: bytes):
                self._head, self._body = head, body

            def readable(self) -> bool:
                return True

            def readinto(self, buffer) -> int:
                if self._head:
                    n = min(len(buffer), len(self._head))
                    buffer[:n] = self._head[:n]
                    self._head = self._head[n:]
                    return n
                if not self._body:
                    return 0
                clock["now"] += 3.0  # each byte arrives three seconds later
                buffer[:1] = self._body[:1]
                self._body = self._body[1:]
                return 1

        body = png(600, 314, b"\0" * 2000)
        head = http_response(body=body)[: -len(body)]
        net.resolve("slow.example", [PUBLIC_IP])
        net.serve(PUBLIC_IP, 80, lambda _request: Trickle(head, body))

        with pytest.raises(ImageFetchError, match="took longer than"):
            fetch_image_bytes("http://slow.example/banner.png")

        # Stopped within one packet of the deadline, not after the whole body.
        assert clock["now"] <= 1000.0 + image_fetch.TIMEOUT_SECONDS + 3.0

    def test_a_declared_oversized_body_is_refused_before_reading(self, net):
        net.resolve("big.example.com", [PUBLIC_IP])
        net.serve(
            PUBLIC_IP,
            80,
            http_response(body=PNG_1X1, headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)}),
        )

        with pytest.raises(ImageFetchError, match="at most 5120 KB"):
            fetch_image_bytes("http://big.example.com/huge.png")

    def test_http_errors_are_reported(self, net):
        net.resolve("img.example.com", [PUBLIC_IP])
        net.serve(PUBLIC_IP, 80, http_response(404, reason="Not Found"))

        with pytest.raises(ImageFetchError, match="HTTP 404"):
            fetch_image_bytes("http://img.example.com/missing.png")

    @pytest.mark.parametrize(
        "url, message",
        [
            ("file:///etc/passwd", "unsupported URL scheme"),
            ("ftp://img.example.com/a.png", "unsupported URL scheme"),
            ("gopher://img.example.com/", "unsupported URL scheme"),
            ("http://user:pass@img.example.com/a.png", "credentials"),
            ("http:///a.png", "no hostname"),
        ],
    )
    def test_refuses_unsupported_urls_without_touching_the_network(self, net, url, message):
        with pytest.raises(ImageFetchError, match=message):
            fetch_image_bytes(url)
        assert net.lookups == []
        assert net.connections == []

    def test_unresolvable_hostnames_are_reported(self, net):
        with pytest.raises(ImageFetchError, match="does not resolve"):
            fetch_image_bytes("http://nowhere.example.com/a.png")


# ---------------------------------------------------------------------------
# draft_image_assets with image_urls
# ---------------------------------------------------------------------------


def _serve_image(net: FakeNet, host: str, data: bytes, ip: str = PUBLIC_IP) -> None:
    net.resolve(host, [ip])
    net.serve(ip, 80, http_response(body=data, headers={"Content-Type": "image/png"}))


class TestDraftFromUrls:
    def test_url_image_is_fetched_and_described_without_storing_bytes(self, net, config):
        image = png(1200, 628, b"\0" * 2048)
        _serve_image(net, "img.example.com", image)

        result = write.draft_image_assets(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            image_urls=["http://img.example.com/banners/Summer%20Sale.png"],
        )

        assert result["operation"] == "create_image_assets", result
        [entry] = result["changes"]["images"]
        assert entry["url"] == "http://img.example.com/banners/Summer%20Sale.png"
        assert entry["sha256"] == hashlib.sha256(image).hexdigest()
        assert entry["mime_type"] == "image/png"
        assert (entry["width"], entry["height"]) == (1200, 628)
        assert entry["name"].startswith("AdLoop image Summer Sale ")
        assert entry["size_kb"] == round(len(image) / 1024, 1)
        assert "path" not in entry
        # Plans are persisted: the image bytes must not be in them.
        assert all(not isinstance(v, bytes) for v in entry.values())
        assert result["changes"]["summary"] == [
            f"http://img.example.com/banners/Summer%20Sale.png — PNG 1200x628, {entry['size_kb']} KB"
        ]

    def test_non_image_content_is_refused_even_when_labelled_png(self, net, config):
        _serve_image(net, "img.example.com", b"<html>definitely an image</html>")

        result = write.draft_image_assets(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            image_urls=["http://img.example.com/a.png"],
        )

        assert result["error"] == "Validation failed"
        assert "Unsupported image type" in result["details"][0]
        assert "http://img.example.com/a.png" in result["details"][0]

    def test_private_url_is_a_validation_error(self, net, config):
        net.resolve("metadata.example.com", ["169.254.169.254"])

        result = write.draft_image_assets(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            image_urls=["http://metadata.example.com/latest/meta-data/"],
        )

        assert result["error"] == "Validation failed"
        assert "non-public" in result["details"][0]
        assert net.connections == []

    def test_at_least_one_image_is_required(self, config):
        result = write.draft_image_assets(config, customer_id="123-456-7890", campaign_id="1001")
        assert result["error"] == "Validation failed"
        assert "At least one image" in result["details"][0]

    def test_local_mode_accepts_paths_and_urls_together(self, net, config, tmp_path):
        local = tmp_path / "logo.png"
        local.write_bytes(PNG_1X1)
        _serve_image(net, "img.example.com", png(600, 600))

        result = write.draft_image_assets(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            image_paths=[str(local)],
            image_urls=["http://img.example.com/square.png"],
        )

        images = result["changes"]["images"]
        assert [("path" in i, "url" in i) for i in images] == [(True, False), (False, True)]
        assert result["changes"]["summary"][0].startswith(f"{local} — PNG 1x1, ")

    def test_server_mode_refuses_paths_but_accepts_urls(self, net, config, tmp_path):
        runtime.set_deployment_mode("server")
        local = tmp_path / "logo.png"
        local.write_bytes(PNG_1X1)
        _serve_image(net, "img.example.com", PNG_1X1)

        refused = write.draft_image_assets(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            image_paths=[str(local)],
            image_urls=["http://img.example.com/a.png"],
        )
        assert "local file paths aren't available on a hosted server" in refused["error"]
        assert "pass image_urls" in refused["error"]

        accepted = write.draft_image_assets(
            config,
            customer_id="123-456-7890",
            campaign_id="1001",
            image_urls=["http://img.example.com/a.png"],
        )
        assert accepted["status"] == "PENDING_CONFIRMATION", accepted
        assert accepted["changes"]["images"][0]["url"] == "http://img.example.com/a.png"


# ---------------------------------------------------------------------------
# Apply: re-fetch, verify the approved digest, upload those bytes
# ---------------------------------------------------------------------------


def _real_client():
    google_ads_service = _FakeGoogleAdsService(
        [
            _FakeMutateOperationResponse("asset_result", "customers/1234567890/assets/1"),
            _FakeMutateOperationResponse(
                "campaign_asset_result", "customers/1234567890/campaignAssets/1001~1~AD_IMAGE"
            ),
        ]
    )
    client = _FakeClient(
        {"GoogleAdsService": google_ads_service, "AssetService": _FakePathService("assets")}
    )
    return client, google_ads_service


def _draft(config, url: str) -> dict:
    result = write.draft_image_assets(
        config, customer_id="123-456-7890", campaign_id="1001", image_urls=[url]
    )
    assert result["status"] == "PENDING_CONFIRMATION", result
    return result


class TestApplyFromUrls:
    def test_apply_uploads_the_fetched_bytes(self, net, config):
        image = png(1200, 1200, b"\x01" * 512)
        _serve_image(net, "img.example.com", image)
        preview = _draft(config, "http://img.example.com/square.png")

        client, google_ads_service = _real_client()
        write._apply_create_image_assets(client, "1234567890", get_plan(preview["plan_id"]).changes)

        created = google_ads_service.operations[0].asset_operation.create
        assert created.image_asset.data == image
        assert created.image_asset.mime_type == client.enums.MimeTypeEnum.IMAGE_PNG
        assert created.image_asset.full_size.width_pixels == 1200
        assert created.name.startswith("AdLoop image square ")
        link = google_ads_service.operations[1].campaign_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.AD_IMAGE
        # Draft and apply each fetched once.
        assert net.connections == [(PUBLIC_IP, 80), (PUBLIC_IP, 80)]

    def test_apply_refuses_an_image_that_changed_since_the_preview(self, net, config):
        _serve_image(net, "img.example.com", png(1200, 1200))
        preview = _draft(config, "http://img.example.com/square.png")
        net.serve(PUBLIC_IP, 80, http_response(body=png(1200, 1200, b"swapped")))

        client, google_ads_service = _real_client()
        with pytest.raises(ValueError, match="changed since the preview; draft again"):
            write._apply_create_image_assets(
                client, "1234567890", get_plan(preview["plan_id"]).changes
            )
        assert google_ads_service.operations is None

    def test_apply_refuses_when_the_host_now_resolves_privately(self, net, config):
        net.resolve("img.example.com", [PUBLIC_IP], ["10.0.0.5"])
        net.serve(PUBLIC_IP, 80, http_response(body=PNG_1X1))
        net.serve("10.0.0.5", 80, http_response(body=PNG_1X1))
        preview = _draft(config, "http://img.example.com/a.png")

        client, _ = _real_client()
        with pytest.raises(ImageFetchError, match="non-public"):
            write._apply_create_image_assets(
                client, "1234567890", get_plan(preview["plan_id"]).changes
            )
        assert ("10.0.0.5", 80) not in net.connections

    def test_dry_run_through_validate_only_sends_the_fetched_bytes(
        self, net, config, monkeypatch
    ):
        image = png(800, 800)
        _serve_image(net, "img.example.com", image)
        preview = _draft(config, "http://img.example.com/a.png")

        fake = FakeAdsClient()
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: fake)

        result = write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS", result
        [(service, method, request)] = fake.calls
        assert (service, method) == ("GoogleAdsService", "mutate")
        assert request.validate_only is True
        assert request.mutate_operations[0].asset_operation.create.image_asset.data == image

    def test_dry_run_fails_cleanly_when_the_image_changed(self, net, config, monkeypatch):
        _serve_image(net, "img.example.com", png(800, 800))
        preview = _draft(config, "http://img.example.com/a.png")
        net.serve(PUBLIC_IP, 80, http_response(body=png(800, 801)))

        fake = FakeAdsClient()
        monkeypatch.setattr(
            write,
            "_validate_with_google",
            lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: fake)

        result = write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)

        assert result["status"] == "DRY_RUN_FAILED"
        assert "changed since the preview" in result["error"]
        assert fake.calls == []


@pytest.mark.asyncio
async def test_tool_schema_describes_both_image_sources():
    from adloop.server import mcp

    tool = {t.name: t for t in await mcp.list_tools()}["draft_image_assets"]
    properties = tool.parameters["properties"]
    for param in ("campaign_id", "image_paths", "image_urls", "customer_id"):
        assert properties[param].get("description"), param
    assert "campaign_id" not in tool.parameters.get("required", [])  # scope="account" takes none
    assert "image_paths" not in tool.parameters.get("required", [])


def test_apply_never_reads_local_paths_in_server_mode(tmp_path):
    local = tmp_path / "secret.png"
    local.write_bytes(PNG_1X1)
    runtime.set_deployment_mode("server")
    client, google_ads_service = _real_client()

    with pytest.raises(ValueError, match="local file paths aren't available"):
        write._apply_create_image_assets(
            client,
            "1234567890",
            {
                "campaign_id": "1001",
                "images": [{"path": str(local), "mime_type": "image/png", "width": 1, "height": 1}],
            },
        )
    assert google_ads_service.operations is None
