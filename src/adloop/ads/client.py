"""Google Ads API client wrapper — thin layer over the google-ads library."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
