"""Google Search Console API client wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from adloop.config import AdLoopConfig


def get_gsc_client(config: AdLoopConfig) -> Resource:
    """Return an authenticated Google Search Console API client."""
    from googleapiclient.discovery import build

    from adloop.auth import get_ga4_credentials

    credentials = get_ga4_credentials(config)
    return build("searchconsole", "v1", credentials=credentials)
