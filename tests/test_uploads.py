"""Image uploads: the store, the upload card and link flows, and the tools that use uploads."""

import base64
import struct
import time
from unittest.mock import patch

import pytest
from fastmcp import Client
from mcp.client import advertise

from adloop import runtime, server, uploads
from adloop.config import AdLoopConfig, RedditConfig, SafetyConfig
from adloop.safety.approval import UI_EXTENSION_ID
from adloop.uploads import LocalUploadStore, UploadError

CARDS = [advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})]


def _png(width=1200, height=628, salt=b"") -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + salt


@pytest.fixture
def store(tmp_path):
    s = LocalUploadStore(tmp_path / "uploads")
    uploads.set_upload_store(s)
    yield s
    uploads.set_upload_store(None)
    uploads.set_upload_links(None)


@pytest.fixture(autouse=True)
def config(tmp_path):
    runtime.set_default_config(AdLoopConfig(
        reddit=RedditConfig(client_id="app", client_secret="s", ad_account_id="a2_acct"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False),
    ))
    yield
    runtime.set_default_config(None)


class _Links:
    def upload_page(self, tenant):
        return f"https://mcp.example/u/tok-{tenant}", 1800

    def public_file(self, tenant, upload_id):
        return f"https://mcp.example/f/{tenant}/{upload_id}"


class TestStore:
    def test_put_get_list_and_dedupe(self, store):
        first = store.put("t1", "../../etc/logo.png", _png())
        assert first.upload_id.startswith("up_")
        assert (first.name, first.mime_type, first.width, first.height) == ("logo.png", "image/png", 1200, 628)
        again = store.put("t1", "copy.png", _png())
        assert again.upload_id == first.upload_id
        upload, data = store.get("t1", first.upload_id)
        assert data == _png() and upload.sha256 == first.sha256
        assert [u.upload_id for u in store.list("t1")] == [first.upload_id]

    def test_tenants_are_separate(self, store):
        mine = store.put("t1", "a.png", _png())
        assert store.list("t2") == []
        with pytest.raises(UploadError, match="was not found"):
            store.get("t2", mine.upload_id)

    def test_refuses_non_images_oversize_and_bad_ids(self, store):
        with pytest.raises(UploadError, match="not a usable image"):
            store.put("t1", "notes.txt", b"hello world, not an image at all")
        with pytest.raises(UploadError, match="at most 5120 KB"):
            store.put("t1", "big.png", _png(salt=b"\x00" * (5121 * 1024)))
        with pytest.raises(UploadError, match="not an upload id"):
            store.get("t1", "../secret")

    def test_expired_uploads_disappear(self, tmp_path):
        s = LocalUploadStore(tmp_path / "u", ttl_seconds=1)
        upload = s.put("t1", "a.png", _png())
        with patch("adloop.uploads.time.time", return_value=time.time() + 5):
            assert s.list("t1") == []
            with pytest.raises(UploadError):
                s.get("t1", upload.upload_id)

    def test_workspace_limits(self, tmp_path):
        s = LocalUploadStore(tmp_path / "u", max_files=2)
        s.put("t1", "a.png", _png(salt=b"a"))
        s.put("t1", "b.png", _png(salt=b"b"))
        with pytest.raises(UploadError, match="already holds 2 uploads"):
            s.put("t1", "c.png", _png(salt=b"c"))



class TestTools:
    @pytest.mark.asyncio
    async def test_card_apps_get_the_card_and_can_store(self, store):
        async with Client(server.mcp, extensions=CARDS) as client:
            tools = {t.name: t for t in await client.list_tools()}
            shown = await client.call_tool("upload_images", {})
            stored = await client.call_tool(
                "store_image_upload",
                {"name": "hero.png", "data_base64": base64.b64encode(_png()).decode()},
            )
            listed = await client.call_tool("list_uploads", {})
        assert tools["upload_images"].meta["ui"]["resourceUri"] == uploads.UPLOAD_CARD_URI
        assert tools["store_image_upload"].meta["ui"]["visibility"] == ["app"]
        assert shown.structured_content["upload"] == "card"
        assert stored.structured_content["status"] == "UPLOADED"
        assert listed.structured_content["uploads"][0]["upload_id"] == stored.structured_content["upload_id"]

    @pytest.mark.asyncio
    async def test_apps_without_cards_get_a_link_or_local_instructions(self, store):
        async with Client(server.mcp) as client:
            names = [t.name for t in await client.list_tools()]
            local = (await client.call_tool("upload_images", {})).structured_content
            refused = await client.call_tool(
                "store_image_upload", {"name": "x.png", "data_base64": ""}, raise_on_error=False
            )
            uploads.set_upload_links(_Links())
            hosted = (await client.call_tool("upload_images", {})).structured_content
        assert "store_image_upload" not in names
        assert refused.is_error and "upload card" in refused.content[0].text
        assert "image_paths" in local["instructions"] and "upload_url" not in local
        assert hosted["upload_url"] == "https://mcp.example/u/tok-local"
        assert hosted["expires_in_minutes"] == 30
        assert hosted["curl_example"].startswith('curl -T "logo.png" "https://mcp.example/u/tok-local/"')

    @pytest.mark.asyncio
    async def test_the_card_code_lets_a_card_upload_without_the_extension_declaration(self, store):
        """The Claude iOS app sends the card's calls without declaring MCP Apps;
        the code from upload_images authorises them instead."""
        from adloop.safety.approval import UPLOAD_META_KEY, upload_code

        async with Client(server.mcp, extensions=CARDS) as card_app:
            shown = await card_app.call_tool("upload_images", {})
        code = shown.meta[UPLOAD_META_KEY]["code"]
        assert code not in str(shown.structured_content) and code not in shown.content[0].text

        image = base64.b64encode(_png()).decode()
        async with Client(server.mcp) as same_app_without_declaration:
            stored = await same_app_without_declaration.call_tool(
                "store_image_upload", {"name": "hero.png", "data_base64": image, "code": code}, raise_on_error=False
            )
            forged = await same_app_without_declaration.call_tool(
                "store_image_upload", {"name": "hero.png", "data_base64": image, "code": code[:-1] + "0"}, raise_on_error=False
            )
            expired = await same_app_without_declaration.call_tool(
                "store_image_upload",
                {"name": "hero.png", "data_base64": image, "code": upload_code(now=time.time() - 3 * 3600)},
                raise_on_error=False,
            )
        assert stored.structured_content["status"] == "UPLOADED"
        assert forged.is_error and "upload card" in forged.content[0].text
        assert expired.is_error and "upload card" in expired.content[0].text

    @pytest.mark.asyncio
    async def test_store_refuses_garbage(self, store):
        async with Client(server.mcp, extensions=CARDS) as client:
            broken = await client.call_tool("store_image_upload", {"name": "x.png", "data_base64": "%%%"})
            text = await client.call_tool(
                "store_image_upload", {"name": "x.txt", "data_base64": base64.b64encode(b"plain text here").decode()}
            )
        assert "did not arrive intact" in broken.structured_content["error"]
        assert "not a usable image" in text.structured_content["error"]


class TestConsumers:
    def test_google_image_assets_take_upload_ids(self, store):
        from adloop.ads.write import _validate_image_assets

        upload = store.put("local", "Hero Banner.png", _png())
        images, errors = _validate_image_assets("123", [], [], [upload.upload_id, "up_missing0000"])
        assert len(images) == 1 and len(errors) == 1
        assert images[0]["upload_id"] == upload.upload_id
        assert images[0]["sha256"] == upload.sha256
        assert (images[0]["width"], images[0]["height"]) == (1200, 628)
        assert "Hero Banner" in images[0]["name"]
        assert "was not found" in errors[0]

    def test_reddit_image_from_upload_needs_cloud_links(self, store):
        from adloop.reddit import write

        upload = store.put("local", "ad.png", _png())
        ref = f"upload:{upload.upload_id}"
        assert "only AdLoop Cloud" in write._media_problem(ref, "image")[0]
        assert "videos cannot be uploaded" in write._media_problem(ref, "video")[0]
        uploads.set_upload_links(_Links())
        assert write._media_problem(ref, "image") == (None, None)
        resolved = write._resolve_uploads({
            "type": "CAROUSEL",
            "content": [{"media_url": ref, "destination_url": "https://example.com"},
                        {"media_url": "https://cdn.example.com/2.png"}],
        })
        assert resolved["content"][0]["media_url"] == f"https://mcp.example/f/local/{upload.upload_id}"
        assert resolved["content"][1]["media_url"] == "https://cdn.example.com/2.png"
