"""Google Ads API client wrapper — thin layer over the google-ads library."""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any, Callable, TypeVar

_T = TypeVar("_T")

if TYPE_CHECKING:
    from google.ads.googleads.client import GoogleAdsClient

    from adloop.config import AdLoopConfig

# Pin the API version so library upgrades don't silently break field names,
# enum values, or mutate operation structures. Bump this deliberately when
# migrating to a new API version — never let it float to the library default.
GOOGLE_ADS_API_VERSION = "v23"


def get_ads_client(config: AdLoopConfig) -> GoogleAdsClient:
    """Return an authenticated Google Ads API client pinned to a specific API version."""
    from google.ads.googleads.client import GoogleAdsClient

    from adloop.auth import get_ads_credentials

    credentials = get_ads_credentials(config)

    client_config = {
        "developer_token": config.ads.developer_token,
        "use_proto_plus": True,
        "version": GOOGLE_ADS_API_VERSION,
    }

    if config.ads.login_customer_id:
        client_config["login_customer_id"] = config.ads.login_customer_id.replace("-", "")

    client = GoogleAdsClient(credentials=credentials, **client_config)
    return client


def normalize_customer_id(customer_id: str) -> str:
    """Strip dashes from customer ID for API calls (123-456-7890 -> 1234567890)."""
    return customer_id.replace("-", "")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if the exception is a 429 / RESOURCE_EXHAUSTED rate-limit error.

    Checks the gRPC status code on GoogleAdsException first (reliable),
    then falls back to string matching for non-gRPC exceptions.
    """
    try:
        from google.ads.googleads.errors import GoogleAdsException

        if isinstance(exc, GoogleAdsException):
            from grpc import StatusCode

            return exc.error.code() == StatusCode.RESOURCE_EXHAUSTED
    except ImportError:
        pass

    msg = str(exc).upper()
    return (
        "RESOURCE_EXHAUSTED" in msg
        or "RATE_LIMIT" in msg
        or "QUOTA_EXCEEDED" in msg
    )


def call_with_retry(
    fn: Callable[..., _T],
    /,
    *args: Any,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> _T:
    """Call fn(*args, **kwargs) with exponential backoff on 429 / RESOURCE_EXHAUSTED.

    Retries up to max_attempts times (default 4 — i.e. 3 retries after the
    initial attempt). Each wait is base_delay * 2^attempt seconds plus up to
    1 second of random jitter to avoid thundering-herd.

    All other exceptions are re-raised immediately without retrying.

    Note: uses time.sleep (blocking). This is safe because FastMCP runs sync
    tool functions in a thread executor — only the worker thread blocks.
    """
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt == max_attempts - 1:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, 1)
            time.sleep(delay)
