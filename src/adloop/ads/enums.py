"""Google Ads enum introspection — pulls valid enum names from the SDK.

Avoids hardcoded enum sets in validators. The google-ads SDK ships the
canonical list for every enum at the API version we're pinned to; this
module surfaces those lists directly so AdLoop validation stays in sync
with whatever the SDK supports without us maintaining a parallel copy.

Usage:
    from adloop.ads.enums import enum_names
    _VALID_TYPES = enum_names("ConversionActionTypeEnum")

The result is a frozenset of member name strings (e.g. {"AD_CALL", ...}),
with sentinel values UNSPECIFIED + UNKNOWN dropped by default.
"""
from __future__ import annotations

import functools


@functools.lru_cache(maxsize=None)
def _enum_introspection_client():
    """Memoized no-auth GoogleAdsClient used purely for enum introspection.

    The client constructor doesn't make any network calls and doesn't
    validate credentials beyond requiring SOMETHING in the developer-token
    field — perfect for reading the bundled enum protos. We cache it so
    every enum_names() call after the first is essentially free.
    """
    from google.ads.googleads.client import GoogleAdsClient

    from adloop.ads.client import GOOGLE_ADS_API_VERSION

    return GoogleAdsClient(
        credentials=None,
        developer_token="adloop-enum-introspection-not-used",
        use_proto_plus=True,
        version=GOOGLE_ADS_API_VERSION,
    )


@functools.lru_cache(maxsize=None)
def enum_names(
    enum_attr: str, *, exclude_unspecified: bool = True
) -> frozenset[str]:
    """Return all member names of a Google Ads enum, as a frozenset.

    Args:
        enum_attr: the attribute on ``client.enums``, e.g.
            ``"ConversionActionTypeEnum"`` or ``"AssetFieldTypeEnum"``.
        exclude_unspecified: when True (default) drops the sentinel values
            ``UNSPECIFIED`` and ``UNKNOWN`` — those are protobuf defaults,
            never valid for user input.

    Raises:
        AttributeError: if ``enum_attr`` doesn't exist on ``client.enums``.

    Caching: the result is memoized for the lifetime of the process, so
    repeated calls return the same frozenset instance.
    """
    enum_cls = getattr(_enum_introspection_client().enums, enum_attr)
    return frozenset(
        m.name for m in enum_cls
        if not exclude_unspecified or m.name not in ("UNSPECIFIED", "UNKNOWN")
    )
