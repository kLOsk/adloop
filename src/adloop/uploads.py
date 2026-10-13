"""Image uploads: files a user hands to AdLoop without a public URL.

AI apps cannot pass a file from the chat to a tool, so an upload reaches
AdLoop on its own path: the upload card (MCP Apps) calls the app-only
``store_image_upload`` tool with the file, or, on a hosted server, the user
opens a one-time upload link. Either way the image lands in an
:class:`UploadStore` under an ``up_...`` id, and the tools take that id:
``draft_image_assets(upload_ids=[...])`` for Google Ads, ``upload:<id>`` in
the media fields of ``draft_reddit_ad``.

Uploads are short-lived: they expire after a day and exist to be turned
into ad assets, not to be kept. Self-hosted, the store is a folder under
``~/.adloop/uploads``. A hosted runtime sets its own store and the links
(:func:`set_upload_links`): the upload page and the short-lived public file
link Reddit downloads from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

UPLOAD_PREFIX = "upload:"
UPLOAD_CARD_URI = "ui://adloop/upload-card.html"
STORE_UPLOAD_TOOL = "store_image_upload"
MAX_UPLOAD_BYTES = 5120 * 1024  # Google's image asset limit; Reddit takes more
DEFAULT_TTL_SECONDS = 24 * 3600
MAX_FILES_PER_TENANT = 50
MAX_BYTES_PER_TENANT = 100 * 1024 * 1024
_ID_PATTERN = re.compile(r"^up_[A-Za-z0-9_-]{8,40}$")


@dataclass(frozen=True)
class Upload:
    upload_id: str
    name: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    created_at: float
    expires_at: float

    def summary(self) -> dict:
        return {
            "upload_id": self.upload_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "size_kb": round(self.size_bytes / 1024, 1),
            "expires_in_hours": max(0, round((self.expires_at - time.time()) / 3600, 1)),
        }


class UploadError(ValueError):
    """An upload was refused or cannot be found; the message says why."""


class UploadLinks(Protocol):
    def upload_page(self, tenant: str) -> tuple[str, int]:
        """A URL where the user uploads files for ``tenant``, and its lifetime in seconds."""

    def public_file(self, tenant: str, upload_id: str) -> str:
        """A short-lived public URL serving the upload (for Reddit to download)."""


def _clean_name(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f]", "", base)
    return base[:120] or "image"


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """(mime_type, width, height) of a PNG, JPEG or GIF; raises UploadError otherwise."""
    from adloop.ads.write import _detect_image_type_and_size

    if not data:
        raise UploadError("the file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"the file is {len(data) / 1024:.0f} KB; images can be at most {MAX_UPLOAD_BYTES // 1024} KB"
        )
    try:
        return _detect_image_type_and_size(data)
    except ValueError as exc:
        raise UploadError(f"not a usable image: {exc} (PNG, JPEG or GIF)") from exc


class LocalUploadStore:
    """Uploads as files in a folder, one subfolder per tenant.

    Expired files are removed whenever the store is used; there is no timer.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_files: int = MAX_FILES_PER_TENANT,
        max_bytes: int = MAX_BYTES_PER_TENANT,
    ) -> None:
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.max_files = max_files
        self.max_bytes = max_bytes

    def _dir(self, tenant: str) -> Path:
        key = hashlib.sha256(str(tenant or "local").encode()).hexdigest()[:24]
        return self.root / key

    def _entries(self, tenant: str) -> list[Upload]:
        folder = self._dir(tenant)
        if not folder.is_dir():
            return []
        now = time.time()
        uploads: list[Upload] = []
        for meta_path in folder.glob("up_*.json"):
            try:
                upload = Upload(**json.loads(meta_path.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if upload.expires_at <= now:
                self._remove(folder, upload.upload_id)
                continue
            uploads.append(upload)
        return sorted(uploads, key=lambda u: u.created_at)

    @staticmethod
    def _remove(folder: Path, upload_id: str) -> None:
        for suffix in (".bin", ".json"):
            try:
                (folder / f"{upload_id}{suffix}").unlink()
            except FileNotFoundError:
                pass

    def put(self, tenant: str, name: str, data: bytes) -> Upload:
        mime_type, width, height = inspect_image(data)
        existing = self._entries(tenant)
        digest = hashlib.sha256(data).hexdigest()
        for upload in existing:
            if upload.sha256 == digest:
                return upload  # the same image twice is one upload
        if len(existing) >= self.max_files:
            raise UploadError(
                f"this workspace already holds {len(existing)} uploads; they expire after a day"
            )
        if sum(u.size_bytes for u in existing) + len(data) > self.max_bytes:
            raise UploadError(
                f"uploads are limited to {self.max_bytes // (1024 * 1024)} MB per workspace; "
                "they expire after a day"
            )
        now = time.time()
        upload = Upload(
            upload_id="up_" + secrets.token_urlsafe(12),
            name=_clean_name(name),
            mime_type=mime_type,
            width=width,
            height=height,
            size_bytes=len(data),
            sha256=digest,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        folder = self._dir(tenant)
        folder.mkdir(parents=True, exist_ok=True)
        for suffix, payload in ((".bin", data), (".json", json.dumps(asdict(upload)).encode())):
            tmp = folder / f".{upload.upload_id}{suffix}.tmp"
            tmp.write_bytes(payload)
            os.replace(tmp, folder / f"{upload.upload_id}{suffix}")
        return upload

    def get(self, tenant: str, upload_id: str) -> tuple[Upload, bytes]:
        upload_id = str(upload_id or "").strip()
        if not _ID_PATTERN.match(upload_id):
            raise UploadError(f"'{upload_id}' is not an upload id (they look like up_...)")
        for upload in self._entries(tenant):
            if upload.upload_id == upload_id:
                data = (self._dir(tenant) / f"{upload_id}.bin").read_bytes()
                return upload, data
        raise UploadError(
            f"upload {upload_id} was not found; uploads expire after a day (see list_uploads)"
        )

    def list(self, tenant: str) -> list[Upload]:
        return self._entries(tenant)


_store: LocalUploadStore | None = None
_links: UploadLinks | None = None


def set_upload_store(store: LocalUploadStore | None) -> None:
    global _store
    _store = store


def get_upload_store() -> LocalUploadStore:
    global _store
    if _store is None:
        _store = LocalUploadStore(Path.home() / ".adloop" / "uploads")
    return _store


def set_upload_links(links: UploadLinks | None) -> None:
    """Hosted runtimes: where users upload, and how Reddit downloads uploads."""
    global _links
    _links = links


def get_upload_links() -> UploadLinks | None:
    return _links


def current_tenant() -> str:
    from adloop.runtime import current_tenant as _tenant

    return _tenant() or ""


def upload_ref(value: str) -> str | None:
    """The upload id in an ``upload:<id>`` reference, else None."""
    text = str(value or "").strip()
    if text.lower().startswith(UPLOAD_PREFIX):
        return text[len(UPLOAD_PREFIX):].strip()
    return None


def public_url_for(upload_id: str) -> str:
    """A URL Reddit can download the upload from; raises UploadError without one."""
    links = get_upload_links()
    if links is None:
        raise UploadError(
            "upload: references need a public link Reddit can download, which only AdLoop "
            "Cloud provides. Self-hosted, pass a public image URL instead."
        )
    get_upload_store().get(current_tenant(), upload_id)  # exists and not expired
    return links.public_file(current_tenant(), upload_id)


def upload_instructions(shows_cards: bool) -> dict:
    """What ``upload_images`` returns: current uploads plus the way to add more."""
    tenant = current_tenant()
    result: dict = {"uploads": [u.summary() for u in get_upload_store().list(tenant)]}
    use = (
        "Use the ids as upload_ids in draft_image_assets, or as \"upload:<upload_id>\" in "
        "the image fields of draft_reddit_ad."
    )
    if shows_cards:
        result["upload"] = "card"
        result["instructions"] = (
            "The user sees an AdLoop upload card and drops the images there. The card posts "
            "the upload ids into the chat when it is done; wait for that. " + use
        )
        return result
    links = get_upload_links()
    if links is None:
        result["instructions"] = (
            "This app shows no upload card. Self-hosted, pass the files directly: "
            "draft_image_assets(image_paths=[...]) takes local files; Reddit ads need a "
            "public image URL."
        )
        return result
    url, lifetime = links.upload_page(tenant)
    minutes = max(1, lifetime // 60)
    result.update({
        "upload_url": url,
        "expires_in_minutes": minutes,
        "curl_example": f'curl -T "logo.png" "{url}/"',
        "instructions": (
            f"Give the user this upload link (valid {minutes} minutes): they open it in the "
            "browser and drop the images, then tell you they are done; call list_uploads for "
            "the ids. A coding agent with the files on disk can upload them itself with curl "
            "(curl_example; one call per file). " + use
        ),
    })
    return result
