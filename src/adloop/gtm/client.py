"""GTM API client wrapper — Google Tag Manager API v2."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def get_gtm_client(config: AdLoopConfig):
    """Return an authenticated Google Tag Manager API v2 client."""
    from googleapiclient.discovery import build

    from adloop.auth import get_gtm_credentials

    credentials = get_gtm_credentials(config)
    return build(
        "tagmanager", "v2", credentials=credentials, cache_discovery=False
    )
