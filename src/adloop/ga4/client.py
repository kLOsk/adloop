"""GA4 API client wrapper — thin layer over google-analytics-data and google-analytics-admin."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    from adloop.config import AdLoopConfig


def get_data_client(config: AdLoopConfig) -> BetaAnalyticsDataClient:
    """Return an authenticated GA4 Data API client."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    from adloop.auth import get_ga4_credentials

    credentials = get_ga4_credentials(config)
    return BetaAnalyticsDataClient(credentials=credentials)


def get_admin_client(config: AdLoopConfig) -> AnalyticsAdminServiceClient:
    """Return an authenticated GA4 Admin API client."""
    from google.analytics.admin_v1beta import AnalyticsAdminServiceClient

    from adloop.auth import get_ga4_credentials

    credentials = get_ga4_credentials(config)
    return AnalyticsAdminServiceClient(credentials=credentials)


def get_admin_alpha_client(config: AdLoopConfig):
    """GA4 Admin API v1alpha client: annotations, Google signals, audiences.

    Google can change v1alpha without notice; callers turn its errors into
    a plain message instead of a traceback.
    """
    from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient

    from adloop.auth import get_ga4_credentials

    return AnalyticsAdminServiceClient(credentials=get_ga4_credentials(config))


def get_data_alpha_client(config: AdLoopConfig):
    """GA4 Data API v1alpha client: funnel reports and conversion attribution."""
    from google.analytics.data_v1alpha import AlphaAnalyticsDataClient

    from adloop.auth import get_ga4_credentials

    return AlphaAnalyticsDataClient(credentials=get_ga4_credentials(config))
