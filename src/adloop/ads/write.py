"""Google Ads write tools — all behind the safety layer.

Every write tool returns a preview/plan. Nothing executes until
``confirm_and_apply`` is called with the plan ID.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


_STRUCTURED_SNIPPET_HEADERS = {
    "Amenities",
    "Brands",
    "Courses",
    "Degree programs",
    "Destinations",
    "Featured Hotels",
    "Insurance coverage",
    "Models",
    "Neighborhoods",
    "Services",
    "Shows",
    "Styles",
    "Types",
}

_VALID_IMAGE_MIME_TYPES = {
    "image/gif": "IMAGE_GIF",
    "image/jpeg": "IMAGE_JPEG",
    "image/png": "IMAGE_PNG",
}

_VALID_HEADLINE_PINS = {"HEADLINE_1", "HEADLINE_2", "HEADLINE_3"}
_VALID_DESCRIPTION_PINS = {"DESCRIPTION_1", "DESCRIPTION_2"}


def _normalize_rsa_assets(items: list) -> list[dict]:
    """Accept str or {text, pinned_field?} dict entries; return list of dicts.

    Plain strings are treated as unpinned. Dict entries may include an optional
    ``pinned_field`` key whose value must be a valid pin slot for the asset
    role (validated by ``_validate_rsa``).
    """
    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            out.append({"text": item, "pinned_field": None})
        elif isinstance(item, dict):
            out.append(
                {
                    "text": item.get("text", ""),
                    "pinned_field": item.get("pinned_field"),
                }
            )
        else:
            raise ValueError(
                f"RSA asset entry must be str or dict, got {type(item).__name__}"
            )
    return out


# Demographic targeting — Google Ads exposes four demographic dimensions.
# By default, ads serve to all segments. Criteria added below either exclude
# (negative=True, the common case) or narrow targeting (negative=False).
_AGE_RANGE_TYPES = {
    "AGE_RANGE_18_24",
    "AGE_RANGE_25_34",
    "AGE_RANGE_35_44",
    "AGE_RANGE_45_54",
    "AGE_RANGE_55_64",
    "AGE_RANGE_65_UP",
    "AGE_RANGE_UNDETERMINED",
}

_GENDER_TYPES = {"FEMALE", "MALE", "UNDETERMINED"}

_PARENTAL_STATUS_TYPES = {"PARENT", "NOT_A_PARENT", "UNDETERMINED"}

# Income ranges are demographic PERCENTILES (top X% by income in supported
# countries), not currency buckets. Available primarily in US/AU/JP.
_INCOME_RANGE_TYPES = {
    "INCOME_RANGE_0_50",   # Lower 50%
    "INCOME_RANGE_50_60",  # 41-50%
    "INCOME_RANGE_60_70",  # 31-40%
    "INCOME_RANGE_70_80",  # 21-30%
    "INCOME_RANGE_80_90",  # 11-20%
    "INCOME_RANGE_90_UP",  # Top 10%
    "INCOME_RANGE_UNDETERMINED",
}

# Human-readable aliases → API enum. Lowercased keys; lookup uses .lower().
_AGE_RANGE_ALIASES = {
    "18-24": "AGE_RANGE_18_24",
    "25-34": "AGE_RANGE_25_34",
    "35-44": "AGE_RANGE_35_44",
    "45-54": "AGE_RANGE_45_54",
    "55-64": "AGE_RANGE_55_64",
    "65+": "AGE_RANGE_65_UP",
    "65-up": "AGE_RANGE_65_UP",
    "undetermined": "AGE_RANGE_UNDETERMINED",
    "unknown": "AGE_RANGE_UNDETERMINED",
}

_GENDER_ALIASES = {
    "female": "FEMALE",
    "f": "FEMALE",
    "male": "MALE",
    "m": "MALE",
    "undetermined": "UNDETERMINED",
    "unknown": "UNDETERMINED",
}

_PARENTAL_ALIASES = {
    "parent": "PARENT",
    "parents": "PARENT",
    "not_a_parent": "NOT_A_PARENT",
    "not-a-parent": "NOT_A_PARENT",
    "not a parent": "NOT_A_PARENT",
    "non-parent": "NOT_A_PARENT",
    "undetermined": "UNDETERMINED",
    "unknown": "UNDETERMINED",
}

_INCOME_ALIASES = {
    "lower-50": "INCOME_RANGE_0_50",
    "0-50": "INCOME_RANGE_0_50",
    "41-50": "INCOME_RANGE_50_60",
    "50-60": "INCOME_RANGE_50_60",
    "31-40": "INCOME_RANGE_60_70",
    "60-70": "INCOME_RANGE_60_70",
    "21-30": "INCOME_RANGE_70_80",
    "70-80": "INCOME_RANGE_70_80",
    "11-20": "INCOME_RANGE_80_90",
    "80-90": "INCOME_RANGE_80_90",
    "top-10": "INCOME_RANGE_90_UP",
    "top 10%": "INCOME_RANGE_90_UP",
    "90-up": "INCOME_RANGE_90_UP",
    "90+": "INCOME_RANGE_90_UP",
    "undetermined": "INCOME_RANGE_UNDETERMINED",
    "unknown": "INCOME_RANGE_UNDETERMINED",
}


def _normalize_demographic_values(
    values: list[str] | None,
    enum_set: set[str],
    alias_map: dict[str, str],
    dimension: str,
) -> tuple[list[str], list[str]]:
    """Map a user-supplied list to Google Ads enum strings.

    Returns (normalized_values, errors). Accepts either the canonical enum
    (case-insensitive) or a human-readable alias like '25-34' or 'female'.
    """
    if not values:
        return [], []

    normalized: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            errors.append(f"{dimension}: values must be strings, got {type(raw).__name__}")
            continue
        candidate = raw.strip()
        if not candidate:
            continue
        upper = candidate.upper().replace(" ", "_").replace("-", "_")
        lower = candidate.lower()
        if upper in enum_set:
            api_value = upper
        elif lower in alias_map:
            api_value = alias_map[lower]
        else:
            errors.append(
                f"{dimension}: '{raw}' is not a valid value. "
                f"Use one of {sorted(enum_set)} or a human-readable alias."
            )
            continue
        if api_value in seen:
            continue
        seen.add(api_value)
        normalized.append(api_value)

    return normalized, errors


# ---------------------------------------------------------------------------
# URL validation — verify URLs exist before creating ads/sitelinks
# ---------------------------------------------------------------------------


def _ssrf_error(url: str) -> str | None:
    """Reject URLs that would make the validation fetch reach non-public hosts.

    User-supplied URLs are fetched from the machine running AdLoop; on a
    hosted multi-tenant server that request originates inside our network,
    so private/loopback/link-local/metadata targets must be refused. Ads
    pointing at such addresses could never serve anyway. Returns an error
    string, or None if the URL looks safe to fetch.

    Note: the fetch re-resolves DNS after this check, so a hostile DNS
    server could still rebind between check and fetch — acceptable here
    because the fetch result is only an up/down signal, never returned
    to the caller. Fetches whose bytes ARE used (image assets) go through
    ``adloop.ads.image_fetch``, which pins the connection to the checked
    address.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError as e:
        return f"unparseable URL: {e}"

    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme '{parsed.scheme}' (only http/https)"

    host = parsed.hostname
    if not host:
        return "URL has no hostname"

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addr_info = socket.getaddrinfo(host, None)
        except OSError as e:
            return f"hostname does not resolve: {e}"
        addresses = [
            ipaddress.ip_address(info[4][0]) for info in addr_info
        ]

    from adloop.ads.image_fetch import non_public_reason

    for addr in addresses:
        if non_public_reason(addr) is not None:
            return (
                f"URL resolves to a non-public address ({addr}) — "
                "refusing to fetch"
            )

    return None


def _build_public_only_opener():
    """Return a urllib opener that re-checks every redirect hop for SSRF."""
    import urllib.error
    import urllib.request

    class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            err = _ssrf_error(newurl)
            if err is not None:
                raise urllib.error.URLError(f"redirect blocked: {err}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(_PublicOnlyRedirectHandler)


# Statuses that say nothing about whether the landing page is any good:
# the site is throttling us, or is briefly unwell. Refusing to draft an ad
# over one punishes the advertiser for their own rate limiting — and we
# are frequently the cause of it, since drafting several ads at once fires
# several HEAD requests at the same origin within a second or two.
#
# A genuinely dead URL still blocks: 404 and 410 are the cases this check
# exists for, and they do not resolve themselves on a retry.
_INCONCLUSIVE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _validate_urls(
    urls: list[str], timeout: int = 10
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Check that each URL returns a 2xx/3xx status.

    Returns (errors, warnings). ``errors`` maps url -> message for URLs
    that should block the operation, None when fine. ``warnings`` maps
    url -> message for checks that came back inconclusive; callers should
    surface those but proceed, because the alternative is refusing to work
    whenever the advertiser's own site is briefly throttling or flaky.
    """
    import urllib.request
    import urllib.error

    opener = _build_public_only_opener()

    results: dict[str, str | None] = {}
    warnings: dict[str, str] = {}

    def _record_status(url: str, status: int) -> None:
        if status in _INCONCLUSIVE_STATUSES:
            results[url] = None
            warnings[url] = (
                f"HTTP {status} — could not verify the URL right now "
                "(the site may be rate-limiting or temporarily down). "
                "Proceeding without the check; confirm the page is live."
            )
        elif status >= 400:
            results[url] = f"HTTP {status}"
        else:
            results[url] = None

    for url in urls:
        if not url:
            continue
        ssrf = _ssrf_error(url)
        if ssrf is not None:
            results[url] = ssrf
            continue
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "AdLoop-URLCheck/1.0")
            resp = opener.open(req, timeout=timeout)
            _record_status(url, resp.status)
        except urllib.error.HTTPError as e:
            if e.code == 405:
                # HEAD not allowed, try GET
                try:
                    req = urllib.request.Request(url, method="GET")
                    req.add_header("User-Agent", "AdLoop-URLCheck/1.0")
                    resp = opener.open(req, timeout=timeout)
                    _record_status(url, resp.status)
                except urllib.error.HTTPError as e2:
                    _record_status(url, e2.code)
                except Exception as e2:
                    results[url] = str(e2)
            else:
                _record_status(url, e.code)
        except Exception as e:
            results[url] = str(e)

    return results, warnings


def _normalize_display_network_setting(
    display_network_enabled: bool | None,
    display_expansion_enabled: bool | None,
) -> tuple[bool | None, list[str]]:
    """Normalize the deprecated alias to one canonical display network flag."""
    errors = []
    if (
        display_network_enabled is not None
        and display_expansion_enabled is not None
        and display_network_enabled != display_expansion_enabled
    ):
        errors.append(
            "display_network_enabled and display_expansion_enabled must match "
            "when both are provided"
        )
    if errors:
        return None, errors
    if display_network_enabled is not None:
        return display_network_enabled, []
    return display_expansion_enabled, []


def _parse_image_metadata(path_str: str) -> dict[str, object]:
    """Validate a local image file and return metadata used for asset creation."""
    path = Path(path_str).expanduser()
    if not path.exists():
        raise ValueError(f"Image file does not exist: {path_str}")
    if not path.is_file():
        raise ValueError(f"Image path is not a file: {path_str}")

    data = path.read_bytes()
    mime_type, width, height = _detect_image_type_and_size(data)
    return {
        "path": str(path),
        "name": _build_image_asset_name(path, data),
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "size_kb": _size_kb(data),
    }


def _parse_image_url_metadata(url: str) -> dict[str, object]:
    """Fetch an image URL safely and return metadata used for asset creation.

    The bytes themselves are NOT returned: plans are persisted, so only a
    sha256 of the approved bytes is kept. Apply re-fetches the URL and
    refuses if the digest no longer matches.
    """
    from adloop.ads.image_fetch import fetch_image_bytes

    fetched = fetch_image_bytes(url)
    mime_type, width, height = _detect_image_type_and_size(fetched.data)
    return {
        "url": url,
        "sha256": fetched.sha256,
        "name": _build_image_asset_name(_url_path(url), fetched.data),
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "size_kb": _size_kb(fetched.data),
    }


def _fetch_approved_image_url(payload: dict) -> bytes:
    """Re-fetch a URL image at apply time and check it is the previewed one."""
    from adloop.ads.image_fetch import fetch_image_bytes

    url = str(payload["url"])
    fetched = fetch_image_bytes(url)
    if fetched.sha256 != payload.get("sha256"):
        raise ValueError(
            f"The image at {url} changed since the preview; draft again."
        )
    return fetched.data


def _url_path(url: str) -> PurePosixPath:
    """The URL's path, so asset names use its file stem like local images do."""
    from urllib.parse import unquote, urlsplit

    return PurePosixPath(unquote(urlsplit(url).path))


def _size_kb(data: bytes) -> float:
    return round(len(data) / 1024, 1)


def _describe_image(image: dict[str, object]) -> str:
    """One preview line: source, type, dimensions and size."""
    source = image.get("url") or image.get("path")
    kind = str(image.get("mime_type", "")).removeprefix("image/").upper()
    return (
        f"{source} — {kind} {image.get('width')}x{image.get('height')}, "
        f"{image.get('size_kb')} KB"
    )


def _build_image_asset_name(path: PurePath, data: bytes) -> str:
    """Build a deterministic asset name required by Google Ads image assets."""
    digest = hashlib.sha1(data).hexdigest()[:12]
    stem = path.stem.strip() or "image"
    return f"AdLoop image {stem[:80]} {digest}"


def _detect_image_type_and_size(data: bytes) -> tuple[str, int, int]:
    """Return MIME type plus width/height, sniffed from the bytes alone."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height

    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 1 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break

            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 1 >= len(data):
                break

            segment_length = struct.unpack(">H", data[index:index + 2])[0]
            if segment_length < 2 or index + segment_length > len(data):
                break

            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3,
                0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            }:
                if index + 7 > len(data):
                    break
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return "image/jpeg", width, height

            index += segment_length

    raise ValueError(
        "Unsupported image type. Use a PNG, JPEG, or GIF image."
    )


# ---------------------------------------------------------------------------
# Draft tools — validate inputs, create a ChangePlan, return preview
# ---------------------------------------------------------------------------


def draft_responsive_search_ad(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    headlines: list[str | dict] | None = None,
    descriptions: list[str | dict] | None = None,
    final_url: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns preview, does NOT execute.

    Each headline/description entry may be either:

    - a plain string (unpinned), or
    - a dict ``{"text": "...", "pinned_field": "HEADLINE_1"}`` (pinned).

    Valid pin values:
        headlines:    HEADLINE_1, HEADLINE_2, HEADLINE_3
        descriptions: DESCRIPTION_1, DESCRIPTION_2

    Google caps: at most 2 headlines per pin slot, at most 1 description per
    pin slot. Mixed plain-string and dict entries are allowed within a single
    call (e.g. brand pinned to HEADLINE_1, the rest unpinned).
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    headlines = headlines or []
    descriptions = descriptions or []

    try:
        headlines = _normalize_rsa_assets(headlines)
        descriptions = _normalize_rsa_assets(descriptions)
    except ValueError as e:
        return {"error": "Validation failed", "details": [str(e)]}

    errors = _validate_rsa(ad_group_id, headlines, descriptions, final_url)
    if errors:
        return {"error": "Validation failed", "details": errors}

    url_check, url_warnings = _validate_urls([final_url])
    if url_check.get(final_url):
        return {
            "error": "URL validation failed",
            "details": [
                f"final_url '{final_url}' is not reachable: {url_check[final_url]}. "
                f"Ads MUST point to working URLs."
            ],
        }

    warnings = []
    if url_warnings.get(final_url):
        warnings.append(f"final_url '{final_url}': {url_warnings[final_url]}")
    if len(headlines) < 8:
        warnings.append(
            f"Only {len(headlines)} headlines provided. Google recommends 8-15 "
            "diverse headlines for optimal RSA performance."
        )
    if len(descriptions) < 3:
        warnings.append(
            f"Only {len(descriptions)} descriptions provided. Google recommends "
            "3-4 descriptions for optimal RSA performance."
        )

    plan = ChangePlan(
        operation="create_responsive_search_ad",
        entity_type="ad",
        customer_id=customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "headlines": headlines,
            "descriptions": descriptions,
            "final_url": final_url,
            "path1": path1,
            "path2": path2,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


# Fires when an in-place RSA update replaces headlines or descriptions. This is
# NOT a free edit: swapping the creative text resets the ad's optimization even
# though the ad keeps its ID. This is the single most important thing to relay
# to the user before applying such a change.
_RSA_TEXT_UPDATE_WARNING = (
    "Replacing headlines/descriptions is NOT a free in-place edit. Even though "
    "the ad keeps its ID, Google treats the creative as new: this resets the "
    "ad's asset-combination learning and wipes its performance history, and the "
    "ad is sent back through policy review before it can serve again. Only "
    "replace headlines/descriptions when the copy genuinely needs to change — "
    "for URL or display-path tweaks these effects do not apply."
)


def update_responsive_search_ad(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_id: str = "",
    headlines: list[str | dict] | None = None,
    descriptions: list[str | dict] | None = None,
    final_url: str = "",
    path1: str = "",
    path2: str = "",
    clear_path1: bool = False,
    clear_path2: bool = False,
) -> dict:
    """Draft an in-place update on an existing RSA — returns a PREVIEW.

    Mutates fields on an existing Responsive Search Ad in place via the
    Google Ads API v23 ``AdService.MutateAds``. The ad keeps its ID. The
    following fields are mutable in place: ``final_urls``,
    ``responsive_search_ad.path1``, ``responsive_search_ad.path2``,
    ``responsive_search_ad.headlines``, ``responsive_search_ad.descriptions``.

    IMPORTANT — replacing headlines or descriptions is NOT a cost-free edit.
    Even though the ad ID is preserved, swapping the creative text RESETS the
    ad's asset-combination learning and performance reporting and sends the ad
    BACK THROUGH policy review — Google treats the creative as new for
    optimization. URL-only and display-path-only edits do not incur this reset.
    The returned preview carries a prominent warning whenever headlines or
    descriptions are being replaced; relay it to the user before applying.

    Headlines/descriptions are list-replace: when provided, the entire list
    swaps in. Google's RSA constraints still apply — 3-15 headlines,
    2-4 descriptions, 30/90 char caps, pin-slot rules — and are validated
    here before the plan is stored. Each entry may be a plain string
    (unpinned) or ``{"text": "...", "pinned_field": "HEADLINE_1"}``.

    Argument semantics:
        - ``headlines`` / ``descriptions`` None or [] -> no change
        - ``final_url`` empty -> no change; non-empty -> replaces final_urls
        - ``path1`` / ``path2`` empty -> no change; non-empty -> sets value
        - ``clear_path1`` / ``clear_path2`` True -> set the path to empty
          (overrides the corresponding path string argument)

    At least one mutation must be requested. Call ``confirm_and_apply`` with
    the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []

    if not ad_id:
        errors.append("ad_id is required")
    elif not str(ad_id).isdigit():
        errors.append("ad_id must be a numeric ID")

    final_url = (final_url or "").strip()
    path1 = (path1 or "").strip()
    path2 = (path2 or "").strip()

    if path1 and len(path1) > 15:
        errors.append(f"path1 must be 15 chars or fewer (got {len(path1)})")
    if path2 and len(path2) > 15:
        errors.append(f"path2 must be 15 chars or fewer (got {len(path2)})")

    norm_headlines: list[dict] = []
    norm_descriptions: list[dict] = []
    if headlines:
        try:
            norm_headlines = _normalize_rsa_assets(headlines)
        except ValueError as e:
            errors.append(str(e))
    if descriptions:
        try:
            norm_descriptions = _normalize_rsa_assets(descriptions)
        except ValueError as e:
            errors.append(str(e))

    if norm_headlines or norm_descriptions:
        # Only enforce count on the list being replaced. The other list is
        # untouched on the live ad, so its size on the wire is whatever the
        # ad already has — not our problem to validate.
        errors.extend(
            _validate_rsa_assets(
                norm_headlines,
                norm_descriptions,
                enforce_headline_count=bool(norm_headlines),
                enforce_description_count=bool(norm_descriptions),
            )
        )

    has_url_change = bool(final_url)
    has_path1_change = bool(path1) or clear_path1
    has_path2_change = bool(path2) or clear_path2
    has_headlines_change = bool(norm_headlines)
    has_descriptions_change = bool(norm_descriptions)

    if not (
        has_url_change
        or has_path1_change
        or has_path2_change
        or has_headlines_change
        or has_descriptions_change
    ):
        errors.append(
            "No changes specified — provide final_url, path1, path2, "
            "clear_path1, clear_path2, headlines, or descriptions"
        )

    if errors:
        return {"error": "Validation failed", "details": errors}

    url_warnings_found: list[str] = []
    if has_url_change:
        # _validate_urls returns (errors, warnings) — the same contract the
        # create path and the sitelink path already unpack. Treating the tuple
        # as a dict made every URL-only update fail with
        # "'tuple' object has no attribute 'get'" before a plan existed.
        url_check, url_warnings = _validate_urls([final_url])
        if url_check.get(final_url):
            return {
                "error": "URL validation failed",
                "details": [
                    f"final_url '{final_url}' is not reachable: "
                    f"{url_check[final_url]}. Ads MUST point to working URLs."
                ],
            }
        if url_warnings.get(final_url):
            url_warnings_found.append(
                f"final_url '{final_url}': {url_warnings[final_url]}"
            )

    changes: dict = {"ad_id": str(ad_id)}
    if has_url_change:
        changes["final_url"] = final_url
    if has_path1_change:
        changes["path1"] = "" if clear_path1 else path1
    if has_path2_change:
        changes["path2"] = "" if clear_path2 else path2
    if has_headlines_change:
        changes["headlines"] = norm_headlines
    if has_descriptions_change:
        changes["descriptions"] = norm_descriptions

    plan = ChangePlan(
        operation="update_responsive_search_ad",
        entity_type="ad",
        entity_id=str(ad_id),
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    # Only warn when the creative text is actually being replaced — URL/path
    # edits do not trigger the learning reset or policy re-review.
    if url_warnings_found:
        preview.setdefault("warnings", []).extend(url_warnings_found)
    if has_headlines_change or has_descriptions_change:
        preview.setdefault("warnings", []).append(_RSA_TEXT_UPDATE_WARNING)
    return preview


def draft_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    keywords: list[dict] | None = None,
) -> dict:
    """Draft keyword additions with match types — returns preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_keywords", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []

    errors = _validate_keywords(ad_group_id, keywords)
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = _check_broad_match_safety(config, customer_id, ad_group_id, keywords)

    plan = ChangePlan(
        operation="add_keywords",
        entity_type="keyword",
        customer_id=customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "keywords": keywords,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def add_negative_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft negative keyword additions — returns preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_negative_keywords", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _VALID_MATCH_TYPES:
        errors.append(f"Invalid match_type '{match_type}' — use EXACT, PHRASE, or BROAD")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="add_negative_keywords",
        entity_type="negative_keyword",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "keywords": keywords,
            "match_type": match_type,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def add_negative_locations(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    geo_target_ids: list[str] | None = None,
) -> dict:
    """Draft negative geo location additions — returns preview."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_negative_locations", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    geo_target_ids = [str(g).strip() for g in (geo_target_ids or []) if str(g).strip()]

    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not geo_target_ids:
        errors.append("At least one geo_target_id is required")
    if any(not geo_id.isdigit() for geo_id in geo_target_ids):
        errors.append("geo_target_ids must be numeric Google geo target constant IDs")
    if errors:
        return {"error": "Validation failed", "details": errors}

    deduped_geo_ids = list(dict.fromkeys(geo_target_ids))
    plan = ChangePlan(
        operation="add_negative_locations",
        entity_type="negative_location",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "geo_target_ids": deduped_geo_ids,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def propose_negative_keyword_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    list_name: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft a shared negative keyword list and attach it to a campaign — returns PREVIEW.

    Creates a reusable negative keyword list (SharedSet) with the given keywords
    and links it to the campaign. Unlike add_negative_keywords, the list can later
    be reused across multiple campaigns.
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_negative_keyword_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not list_name:
        errors.append("list_name is required")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _VALID_MATCH_TYPES:
        errors.append(f"Invalid match_type '{match_type}' — use EXACT, PHRASE, or BROAD")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_negative_keyword_list",
        entity_type="negative_keyword_list",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "list_name": list_name,
            "keywords": keywords,
            "match_type": match_type,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def add_to_negative_keyword_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft adding keywords to an existing shared negative keyword list — returns PREVIEW.

    Unlike ``propose_negative_keyword_list`` (which creates a NEW list), this
    appends keywords to an existing SharedSet identified by ``shared_set_id``.
    Use ``get_negative_keyword_lists`` to find the list's ID. Call
    ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_to_negative_keyword_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not shared_set_id:
        errors.append("shared_set_id is required")
    elif not str(shared_set_id).isdigit():
        errors.append("shared_set_id must be a numeric ID (from get_negative_keyword_lists)")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _VALID_MATCH_TYPES:
        errors.append(f"Invalid match_type '{match_type}' — use EXACT, PHRASE, or BROAD")
    if errors:
        return {"error": "Validation failed", "details": errors}

    seen: set[str] = set()
    deduped: list[str] = []
    for kw in keywords:
        text = kw.strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)

    if not deduped:
        return {
            "error": "Validation failed",
            "details": ["At least one non-empty keyword is required"],
        }

    plan = ChangePlan(
        operation="add_to_negative_keyword_list",
        entity_type="negative_keyword_list",
        entity_id=str(shared_set_id),
        customer_id=customer_id,
        changes={
            "shared_set_id": str(shared_set_id),
            "keywords": deduped,
            "match_type": match_type,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _normalize_shared_set_attachment_args(
    shared_set_id: str,
    campaign_ids: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Validate inputs for attach/detach. Returns (errors, deduped_campaign_ids)."""
    errors: list[str] = []
    if not shared_set_id:
        errors.append("shared_set_id is required")
    elif not str(shared_set_id).isdigit():
        errors.append(
            "shared_set_id must be a numeric ID (from get_negative_keyword_lists)"
        )

    campaign_errors, deduped = _normalize_campaign_ids(campaign_ids)
    errors.extend(campaign_errors)
    # Only complain about an empty list when the ids themselves were fine —
    # "campaign_id 'x' must be numeric" is the more useful answer otherwise.
    if not deduped and not campaign_errors:
        errors.append("At least one campaign_id is required")
    return errors, deduped


def _normalize_campaign_ids(
    campaign_ids: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Trim, drop blanks, de-duplicate and require digits. Returns (errors, ids).

    No minimum: whether an empty list is an error depends on the caller
    (attaching needs at least one campaign, creating a list does not).
    """
    errors: list[str] = []
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in campaign_ids or []:
        value = str(raw).strip()
        if not value:
            continue
        if not value.isdigit():
            errors.append(f"campaign_id '{value}' must be numeric")
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return errors, cleaned


def attach_shared_set_to_campaigns(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    campaign_ids: list[str] | None = None,
) -> dict:
    """Draft attaching an existing shared set to one or more campaigns — returns PREVIEW.

    Creates ``CampaignSharedSet`` linkages so the campaigns inherit the shared
    set's criteria (e.g. negative keywords). Most commonly used to attach a
    shared negative keyword list to newly-built campaigns. Use
    ``get_negative_keyword_lists`` to find the ``shared_set_id`` and
    ``get_negative_keyword_list_campaigns`` to see existing attachments.

    shared_set_id: numeric ID of the shared set to attach.
    campaign_ids: list of numeric campaign IDs to attach the set to. Duplicates
        in the input list are collapsed.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("attach_shared_set_to_campaigns", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors, deduped = _normalize_shared_set_attachment_args(
        shared_set_id, campaign_ids
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="attach_shared_set_to_campaigns",
        entity_type="campaign_shared_set",
        entity_id=str(shared_set_id),
        customer_id=customer_id,
        changes={
            "shared_set_id": str(shared_set_id),
            "campaign_ids": deduped,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def detach_shared_set_from_campaigns(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    campaign_ids: list[str] | None = None,
) -> dict:
    """Draft detaching a shared set from one or more campaigns — returns PREVIEW.

    Removes ``CampaignSharedSet`` linkages so the campaigns no longer inherit
    the shared set's criteria. The shared set itself is unchanged; only the
    per-campaign attachment record is removed. Use
    ``get_negative_keyword_list_campaigns`` to inspect existing attachments
    before detaching.

    shared_set_id: numeric ID of the shared set.
    campaign_ids: list of numeric campaign IDs to detach the set from.
        Detaching a set that isn't currently attached to a campaign is a no-op
        at the API level (the request will fail for that specific operation
        but does not affect the others; surfaced in the apply response).

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("detach_shared_set_from_campaigns", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors, deduped = _normalize_shared_set_attachment_args(
        shared_set_id, campaign_ids
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="detach_shared_set_from_campaigns",
        entity_type="campaign_shared_set",
        entity_id=str(shared_set_id),
        customer_id=customer_id,
        changes={
            "shared_set_id": str(shared_set_id),
            "campaign_ids": deduped,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def _normalize_brand_ids(brand_ids: list[str] | None) -> tuple[list[str], list[str]]:
    """Validate brand entity IDs. Returns (errors, deduped_ids).

    Brand IDs are Commercial Knowledge Graph MIDs (e.g. "/m/01n5j"), not
    numbers — do not require digits here.
    """
    errors: list[str] = []
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in brand_ids or []:
        brand_id = str(raw).strip()
        if not brand_id:
            continue
        if brand_id in seen:
            continue
        seen.add(brand_id)
        deduped.append(brand_id)
    if not deduped:
        errors.append("At least one brand_id is required")
    return errors, deduped


def _brand_id_warnings(brand_ids: list[str]) -> list[str]:
    """Warn about values that do not look like a Commercial KG MID.

    The most common mistake is passing a brand *name* straight from a search
    result. The API rejects that at apply time with a message that does not
    mention names, so the preview says it up front instead of blocking: a
    customer-scoped brand ID (UNVERIFIED/APPROVED) may legitimately be
    something else.
    """
    suspicious = [b for b in brand_ids if not b.startswith(("/m/", "/g/"))]
    if not suspicious:
        return []
    shown = ", ".join(f"'{b}'" for b in suspicious[:5])
    more = "" if len(suspicious) <= 5 else f" (+{len(suspicious) - 5} more)"
    return [
        f"{len(suspicious)} brand_id(s) do not look like a Commercial KG MID "
        f"(/m/… or /g/…): {shown}{more}. If these are brand names, resolve them "
        "with suggest_brands or check_brand_names first — a display name cannot "
        "be written."
    ]


def _normalize_criterion_ids(criterion_ids: list[str] | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in criterion_ids or []:
        criterion_id = str(raw).strip()
        if not criterion_id:
            continue
        if not criterion_id.isdigit():
            errors.append(
                f"criterion_id '{criterion_id}' must be numeric "
                "(from get_brand_list_brands)"
            )
            continue
        if criterion_id in seen:
            continue
        seen.add(criterion_id)
        deduped.append(criterion_id)
    if not deduped and not errors:
        errors.append("At least one criterion_id is required")
    return errors, deduped

def propose_brand_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    list_name: str = "",
    brand_ids: list[str] | None = None,
    campaign_ids: list[str] | None = None,
    negative: bool = True,
) -> dict:
    """Draft a brand list and optionally attach it to campaigns — returns PREVIEW.

    Creates a ``SharedSet`` of type BRANDS, fills it with ``SharedCriterion``
    entries and — when ``campaign_ids`` is given — attaches it as a
    ``CampaignCriterion.brand_list``. That criterion is what decides the role:
    ``negative=True`` excludes the brands, ``negative=False`` restricts
    targeting to them.

    brand_ids: Commercial Knowledge Graph MIDs, i.e. the ``id`` field from
        ``suggest_brands`` / ``check_brand_names``. Resolve names there first —
        a display name alone cannot be written.
    campaign_ids: optional. Omit to create the list without using it yet, then
        attach later with ``attach_brand_list_to_campaigns``.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_brand_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    if not list_name:
        errors.append("list_name is required")
    brand_errors, deduped_brands = _normalize_brand_ids(brand_ids)
    errors.extend(brand_errors)
    warnings = _brand_id_warnings(deduped_brands)

    campaign_errors, deduped_campaigns = _normalize_campaign_ids(campaign_ids)
    errors.extend(campaign_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_brand_list",
        entity_type="brand_list",
        entity_id="",
        customer_id=customer_id,
        changes={
            "list_name": list_name,
            "brand_ids": deduped_brands,
            "campaign_ids": deduped_campaigns,
            "negative": bool(negative),
        },
    )
    if warnings:
        plan.changes["warnings"] = warnings
    store_plan(plan)
    return plan.to_preview()

def add_to_brand_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    brand_ids: list[str] | None = None,
) -> dict:
    """Draft adding brands to an existing brand list — returns PREVIEW.

    Appends brands to the SharedSet identified by ``shared_set_id``. Use
    ``get_brand_lists`` to find the list and ``get_brand_list_brands`` to
    avoid adding a brand twice.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_to_brand_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    shared_set_id = str(shared_set_id or "").strip()
    if not shared_set_id:
        errors.append("shared_set_id is required")
    elif not shared_set_id.isdigit():
        errors.append("shared_set_id must be a numeric ID (from get_brand_lists)")
    brand_errors, deduped_brands = _normalize_brand_ids(brand_ids)
    errors.extend(brand_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="add_to_brand_list",
        entity_type="brand_list",
        entity_id=shared_set_id,
        customer_id=customer_id,
        changes={"shared_set_id": shared_set_id, "brand_ids": deduped_brands},
    )
    warnings = _brand_id_warnings(deduped_brands)
    if warnings:
        plan.changes["warnings"] = warnings
    store_plan(plan)
    return plan.to_preview()

def remove_from_brand_list(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    criterion_ids: list[str] | None = None,
) -> dict:
    """Draft removing brands from a brand list — returns PREVIEW.

    SharedCriteria have no status field, so removal is the only way to take a
    brand out of a list. Identify the entries with the numeric
    ``criterion_id`` (next to ``resource_id``) from ``get_brand_list_brands``.

    Removing a brand from a list does not detach the list from any campaign —
    use ``detach_brand_list_from_campaigns`` for that.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("remove_from_brand_list", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    shared_set_id = str(shared_set_id or "").strip()
    if not shared_set_id:
        errors.append("shared_set_id is required")
    elif not shared_set_id.isdigit():
        errors.append("shared_set_id must be a numeric ID (from get_brand_lists)")
    criterion_errors, deduped_criteria = _normalize_criterion_ids(criterion_ids)
    errors.extend(criterion_errors)
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="remove_from_brand_list",
        entity_type="brand_list",
        entity_id=shared_set_id,
        customer_id=customer_id,
        changes={
            "shared_set_id": shared_set_id,
            "criterion_ids": deduped_criteria,
        },
        # SharedCriteria have no status field, so this is a real removal — the
        # preview asks for a second explicit confirmation.
        requires_double_confirm=True,
    )
    store_plan(plan)
    return plan.to_preview()

def attach_brand_list_to_campaigns(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    campaign_ids: list[str] | None = None,
    negative: bool = True,
) -> dict:
    """Draft attaching a brand list to campaigns — returns PREVIEW.

    Creates a ``CampaignCriterion.brand_list`` per campaign. Note this is NOT
    the ``CampaignSharedSet`` linkage used for negative keyword lists — brand
    lists are criteria, and the criterion's ``negative`` flag is what makes
    the list an exclusion (True, the default) or a targeting restriction
    (False).

    Use ``get_brand_lists`` for the shared_set_id and
    ``get_brand_list_campaigns`` to see existing attachments.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("attach_brand_list_to_campaigns", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors, deduped = _normalize_shared_set_attachment_args(
        shared_set_id, campaign_ids
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="attach_brand_list_to_campaigns",
        entity_type="brand_list_attachment",
        entity_id=str(shared_set_id),
        customer_id=customer_id,
        changes={
            "shared_set_id": str(shared_set_id),
            "campaign_ids": deduped,
            "negative": bool(negative),
        },
    )
    store_plan(plan)
    return plan.to_preview()

def detach_brand_list_from_campaigns(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    shared_set_id: str = "",
    campaign_ids: list[str] | None = None,
) -> dict:
    """Draft detaching a brand list from campaigns — returns PREVIEW.

    Looks up the matching ``CampaignCriterion`` rows at apply time and removes
    them. Campaigns in the request that do not carry this brand list are
    reported as ``not_attached`` instead of failing the batch.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("detach_brand_list_from_campaigns", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors, deduped = _normalize_shared_set_attachment_args(
        shared_set_id, campaign_ids
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="detach_brand_list_from_campaigns",
        entity_type="brand_list_attachment",
        entity_id=str(shared_set_id),
        customer_id=customer_id,
        changes={
            "shared_set_id": str(shared_set_id),
            "campaign_ids": deduped,
        },
    )
    store_plan(plan)
    return plan.to_preview()
def draft_ai_max_settings(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    enable_ai_max: bool | None = None,
    disable_search_term_matching: bool | None = None,
    ad_group_ids: list[str] | None = None,
    include_paused_ad_groups: bool = True,
    text_asset_automation: str = "UNCHANGED",
    final_url_expansion: str = "UNCHANGED",
) -> dict:
    """Draft AI Max controls for one Search campaign — returns PREVIEW.

    AI Max is what makes brand exclusions usable in Search: Google rejects a
    brand list on a plain Search campaign ("For search advertising channel,
    brand lists can only be applied to exclusive targeting, broad match
    campaigns for inclusive targeting or PMax generated campaigns"). Enabling
    it as a container while leaving its automations on is the trap — this tool
    switches both in one planned change.

    Unlike the other draft tools this one reads the campaign and its ad groups
    first (read-only): the preview then names concrete ad groups and shows the
    current settings per knob instead of just echoing the arguments.

    ad_group_ids: optional explicit selection. Omitted/empty means every
        non-removed ad group of the campaign.
    include_paused_ad_groups: default True — paused groups are set too, so
        re-enabling one later cannot silently bring search term matching back.
        REMOVED ad groups are never touched.
    text_asset_automation / final_url_expansion: OPTED_IN, OPTED_OUT or
        UNCHANGED. Final URL expansion is modelled as
        FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION in API v25.

    Switching AI Max on while an automation keeps running is refused when the
    caller did not mention that automation (unchanged campaign setting, ad group
    outside the selection) — that is the accident this tool exists to prevent.
    Naming it explicitly (disable_search_term_matching=false,
    text_asset_automation=OPTED_IN) is allowed: the plan then carries
    requires_double_confirm=true and a warning listing what stays automated.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.ads import ai_max as ai
    from adloop.ads.client import get_ads_client, normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_ai_max_settings", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        errors.append("campaign_id is required")
    elif not campaign_id.isdigit():
        errors.append("campaign_id must be a numeric ID")
    for label, value in (
        ("text_asset_automation", text_asset_automation),
        ("final_url_expansion", final_url_expansion),
    ):
        if value not in ai.AUTOMATION_CHOICES:
            errors.append(
                f"{label} must be one of {', '.join(ai.AUTOMATION_CHOICES)}"
            )
    if (
        enable_ai_max is None
        and disable_search_term_matching is None
        and text_asset_automation == "UNCHANGED"
        and final_url_expansion == "UNCHANGED"
    ):
        errors.append(
            "Nothing to change — set enable_ai_max, disable_search_term_matching, "
            "text_asset_automation or final_url_expansion"
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    state = ai.read_ai_max_state(
        get_ads_client(config), cid, campaign_id=campaign_id
    )
    campaigns = state.get("campaigns") or []
    if not campaigns:
        return {
            "error": (
                f"Campaign {campaign_id} was not found in this account "
                "(or it is REMOVED). Nothing was planned."
            )
        }
    campaign = campaigns[0]
    if campaign.get("status") == "REMOVED":
        return {"error": f"Campaign {campaign_id} is REMOVED — nothing was planned."}
    if campaign.get("advertising_channel_type") != "SEARCH":
        return {
            "error": (
                "AI Max controls here are for Search campaigns; "
                f"{campaign_id} is {campaign.get('advertising_channel_type')}."
            )
        }

    warnings: list[str] = []
    targets, target_warnings = ai.plan_targets(
        campaign,
        disable_search_term_matching=bool(disable_search_term_matching),
        ad_group_ids=ad_group_ids,
        include_paused_ad_groups=include_paused_ad_groups,
    )
    warnings.extend(target_warnings)
    if not campaign.get("ad_groups"):
        warnings.append("This campaign has no non-removed ad groups.")

    automation_updates = {
        ai.TEXT_ASSET_AUTOMATION: text_asset_automation,
        ai.FINAL_URL_EXPANSION: final_url_expansion,
    }

    # AI Max with running automations is a real Google Ads configuration, but it
    # must not happen by accident: an automation nobody mentioned is refused, one
    # the caller named is planned with a second confirmation.
    risks = ai.plan_risks(
        campaign,
        enable_ai_max=enable_ai_max,
        disable_search_term_matching=disable_search_term_matching,
        automation_updates=automation_updates,
        targeted_ad_group_ids={group["ad_group_id"] for group in targets},
    )
    if risks["implicit"]:
        return {
            "error": (
                "Refusing to switch AI Max on while its automations keep "
                "running unnoticed. Nothing was planned."
            ),
            "details": risks["implicit"],
            "hint": (
                "Safe state: disable_search_term_matching=true, "
                "text_asset_automation=OPTED_OUT, final_url_expansion=OPTED_OUT "
                "(draft_prepare_brand_exclusions plans all three at once). If the "
                "automation is actually wanted, say so explicitly — "
                "disable_search_term_matching=false and/or "
                "text_asset_automation=OPTED_IN — then the plan asks for a second "
                "confirmation instead of refusing."
            ),
        }
    double_confirm = bool(risks["explicit"])
    warnings.extend(risks["explicit"])

    current_settings = campaign.get("asset_automation_settings") or []
    merged_settings = ai.merge_asset_automation(current_settings, automation_updates)
    changed_types = [
        asset_type
        for asset_type, status in automation_updates.items()
        if status != "UNCHANGED"
    ]

    ad_group_changes = []
    if disable_search_term_matching is not None:
        ad_group_changes = [
            {
                "ad_group_id": group["ad_group_id"],
                "ad_group_name": group.get("ad_group_name"),
                "status": group.get("status"),
                "before": bool(group.get("disable_search_term_matching")),
                "after": bool(disable_search_term_matching),
            }
            for group in targets
        ]

    changes: dict = {
        "campaign_id": campaign_id,
        "campaign_name": campaign.get("campaign_name"),
    }
    if enable_ai_max is not None:
        changes["enable_ai_max"] = enable_ai_max
        changes["ai_max_before"] = campaign.get("enable_ai_max")
    if disable_search_term_matching is not None:
        changes["disable_search_term_matching"] = bool(disable_search_term_matching)
    if ad_group_changes:
        changes["ad_groups"] = ad_group_changes
    if changed_types:
        changes["asset_automation_settings"] = [
            item
            for item in merged_settings
            if item["asset_automation_type"] in {
                ai.TEXT_ASSET_AUTOMATION,
                ai.FINAL_URL_EXPANSION,
            }
        ]
        changes["asset_automation_changed"] = changed_types
        changes["asset_automation_before"] = current_settings
        changes["_asset_automation_settings_full"] = merged_settings
    if warnings:
        changes["warnings"] = warnings

    plan = ChangePlan(
        operation="update_ai_max_settings",
        entity_type="campaign",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes=changes,
        requires_double_confirm=double_confirm,
    )
    store_plan(plan)
    return plan.to_preview()

def draft_prepare_brand_exclusions(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    include_paused_ad_groups: bool = True,
) -> dict:
    """Draft the safe standard state for brand exclusions — returns PREVIEW.

    Exactly one combination, nothing else:

        enable_ai_max = true
        disable_search_term_matching = true   (every non-removed ad group)
        TEXT_ASSET_AUTOMATION = OPTED_OUT
        FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION = OPTED_OUT

    No bidding, keyword, match type, ad, URL or budget change, and no brand
    list is attached — attaching one stays a separate step once this state is
    verified in the account.

    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    result = draft_ai_max_settings(
        config,
        customer_id=customer_id,
        campaign_id=campaign_id,
        enable_ai_max=True,
        disable_search_term_matching=True,
        ad_group_ids=None,
        include_paused_ad_groups=include_paused_ad_groups,
        text_asset_automation="OPTED_OUT",
        final_url_expansion="OPTED_OUT",
    )
    if isinstance(result, dict) and result.get("status") == "PENDING_CONFIRMATION":
        result["purpose"] = "brand_exclusions"
        result["changes"]["purpose"] = "brand_exclusions"
    return result

def draft_demographic_targeting(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    campaign_id: str = "",
    age_ranges: list[str] | None = None,
    genders: list[str] | None = None,
    parental_statuses: list[str] | None = None,
    income_ranges: list[str] | None = None,
    negative: bool = True,
) -> dict:
    """Draft demographic targeting criteria (age, gender, parental status, income) — returns PREVIEW.

    By default, Google Ads serves to all demographic segments. This tool adds
    criteria that either EXCLUDE a segment (negative=True, default) or NARROW
    targeting to it (negative=False — uncommon).

    Provide either `ad_group_id` or `campaign_id`. At least one of the
    demographic lists (age_ranges/genders/parental_statuses/income_ranges)
    must be non-empty.

    Accepted values per dimension:
    - age_ranges: '18-24', '25-34', '35-44', '45-54', '55-64', '65+' (or the
      canonical AGE_RANGE_18_24 etc.). Note: Google's buckets are fixed —
      'Exclude 23-35' has no exact mapping; you must pick the closest buckets.
    - genders: 'female', 'male', 'undetermined'
    - parental_statuses: 'parent', 'not_a_parent', 'undetermined'
    - income_ranges: PERCENTILE buckets, not currency. 'top-10' (top 10%),
      '11-20', '21-30', '31-40', '41-50', 'lower-50', 'undetermined'.
      Only available in select countries (US, AU, JP, etc.).
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_demographic_criteria", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors: list[str] = []
    if bool(ad_group_id) == bool(campaign_id):
        errors.append(
            "Provide exactly one of ad_group_id or campaign_id"
        )

    age_values, age_errors = _normalize_demographic_values(
        age_ranges, _AGE_RANGE_TYPES, _AGE_RANGE_ALIASES, "age_ranges"
    )
    gender_values, gender_errors = _normalize_demographic_values(
        genders, _GENDER_TYPES, _GENDER_ALIASES, "genders"
    )
    parental_values, parental_errors = _normalize_demographic_values(
        parental_statuses,
        _PARENTAL_STATUS_TYPES,
        _PARENTAL_ALIASES,
        "parental_statuses",
    )
    income_values, income_errors = _normalize_demographic_values(
        income_ranges, _INCOME_RANGE_TYPES, _INCOME_ALIASES, "income_ranges"
    )
    errors.extend(age_errors + gender_errors + parental_errors + income_errors)

    if not (age_values or gender_values or parental_values or income_values):
        errors.append(
            "At least one of age_ranges, genders, parental_statuses, or "
            "income_ranges must contain a value"
        )

    if campaign_id and not negative:
        errors.append(
            "Campaign-level demographic criteria can only be EXCLUSIONS "
            "(negative=True). Positive demographic targeting lives on ad "
            "groups — pass ad_group_id instead."
        )

    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings: list[str] = []
    if not negative:
        warnings.append(
            "negative=False adds POSITIVE demographic criteria, which NARROWS "
            "targeting. Users not matching the criteria (plus UNDETERMINED) "
            "will no longer see ads. This is uncommon — exclusions are the "
            "typical pattern."
        )
    if negative and any(
        v.endswith("UNDETERMINED")
        for v in age_values + gender_values + parental_values + income_values
    ):
        warnings.append(
            "Excluding UNDETERMINED blocks every user Google cannot classify "
            "for that dimension — often 30%+ of impressions, more under EU "
            "consent restrictions. Reach drops far beyond the named segment. "
            "Verify this is intentional."
        )
    excludes_all_age = len(age_values) >= len(_AGE_RANGE_TYPES) - 1
    excludes_all_gender = len(gender_values) >= len(_GENDER_TYPES) - 1
    if negative and (excludes_all_age or excludes_all_gender):
        warnings.append(
            "Excluding nearly every value in a single demographic dimension "
            "will reduce ad delivery sharply. Verify this is intentional."
        )

    plan = ChangePlan(
        operation="add_demographic_criteria",
        entity_type="ad_group_criterion" if ad_group_id else "campaign_criterion",
        entity_id=ad_group_id or campaign_id,
        customer_id=customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "campaign_id": campaign_id,
            "age_ranges": age_values,
            "genders": gender_values,
            "parental_statuses": parental_values,
            "income_ranges": income_values,
            "negative": negative,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def update_ad_group(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_group_id: str = "",
    ad_group_name: str = "",
    max_cpc: float = 0,
) -> dict:
    """Draft an ad group update for name and manual CPC bid."""
    from adloop.safety.guards import (
        SafetyViolation,
        check_bid_increase,
        check_blocked_operation,
    )
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_ad_group", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if max_cpc < 0:
        errors.append("max_cpc cannot be negative")
    if max_cpc:
        strategy = _ad_group_campaign_bidding_strategy(
            config, customer_id, ad_group_id
        )
        if strategy is None:
            errors.append(
                f"Unable to verify bidding strategy for ad_group_id '{ad_group_id}'"
            )
        elif strategy != "MANUAL_CPC":
            # Per Google Ads docs, ad-group CPC bids are ignored under every
            # automated bidding strategy — effective_cpc_bid_micros is always 0
            # and the campaign-level constraint (cpc_bid_ceiling for
            # TARGET_SPEND, target CPA/ROAS otherwise) is the only thing that
            # affects spend. Telling the user "requires MANUAL_CPC" makes it
            # sound like a tool limitation; the real story is that the bid
            # would be a no-op. Point at the right next step for the strategy.
            # https://support.google.com/google-ads/answer/6336101
            if strategy == "TARGET_SPEND":
                errors.append(
                    "Maximize Clicks (TARGET_SPEND) ignores ad-group CPC bids. "
                    "The campaign cpc_bid_ceiling is the active constraint — "
                    "set it via update_campaign(max_cpc=...) instead. "
                    "No change made."
                )
            else:
                errors.append(
                    f"{strategy} ignores ad-group CPC bids "
                    f"(effective_cpc_bid_micros = 0). The campaign-level "
                    f"target governs spend under automated bidding. "
                    f"No change made."
                )
        else:
            current_bid = _current_ad_group_cpc_bid(config, customer_id, ad_group_id)
            try:
                check_bid_increase(current_bid or 0, max_cpc, config.safety)
            except SafetyViolation as e:
                errors.append(str(e))

    has_any_change = bool(ad_group_name.strip() or max_cpc)
    if not has_any_change:
        errors.append("No changes specified — provide ad_group_name and/or max_cpc")

    if errors:
        return {"error": "Validation failed", "details": errors}

    changes: dict = {"ad_group_id": ad_group_id}
    if ad_group_name.strip():
        changes["ad_group_name"] = ad_group_name.strip()
    if max_cpc:
        changes["max_cpc"] = max_cpc

    plan = ChangePlan(
        operation="update_ad_group",
        entity_type="ad_group",
        entity_id=ad_group_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    return plan.to_preview()


def pause_entity(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft pausing a campaign/ad group/ad/keyword — returns preview."""
    return _draft_status_change(
        config, "pause_entity", customer_id, entity_type, entity_id, "PAUSED"
    )


def enable_entity(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft enabling a paused entity — returns preview."""
    return _draft_status_change(
        config, "enable_entity", customer_id, entity_type, entity_id, "ENABLED"
    )


def remove_entity(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft removing an entity — returns preview.

    Supported ``entity_type`` values: ``campaign``, ``ad_group``, ``ad``,
    ``keyword``, ``negative_keyword``, ``shared_criterion``, ``campaign_asset``,
    ``asset``, ``customer_asset``.

    Composite ``entity_id`` formats:

    - ``keyword``: ``adGroupId~criterionId``
    - ``negative_keyword``: ``campaignId~criterionId`` (use the ``resource_id``
      field from ``get_negative_keywords``)
    - ``shared_criterion``: ``sharedSetId~criterionId`` (use the ``resource_id``
      field from ``get_negative_keyword_list_keywords``)
    - ``campaign_asset``: ``campaignId~assetId~fieldType``
    - ``customer_asset``: ``assetId~fieldType``
    - ``asset``: bare asset ID

    This is a DESTRUCTIVE operation — removed entities cannot be re-enabled.
    Prefer ``pause_entity`` unless the user explicitly wants permanent removal.
    Call ``confirm_and_apply`` with the returned plan_id to execute.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("remove_entity", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if entity_type not in _REMOVABLE_ENTITY_TYPES:
        errors.append(
            f"entity_type must be one of {_REMOVABLE_ENTITY_TYPES}, "
            f"got '{entity_type}'"
        )
    if not entity_id:
        errors.append("entity_id is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    # Normalize composite IDs: commas → tildes
    if entity_type in ("campaign_asset", "customer_asset"):
        entity_id = entity_id.replace(",", "~")

    plan = ChangePlan(
        operation="remove_entity",
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"action": "REMOVE"},
        # Removal is irreversible; the preview asks for a second explicit
        # confirmation, as Reddit removals already did.
        requires_double_confirm=True,
    )
    store_plan(plan)
    return plan.to_preview()


def draft_campaign(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_name: str = "",
    daily_budget: float = 0,
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    channel_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool = False,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft a full campaign structure — returns preview, does NOT execute.

    Creates: CampaignBudget + Campaign (PAUSED) + AdGroup + optional Keywords
    + geo targeting + language targeting.
    Ads are NOT included — use draft_responsive_search_ad separately.

    geo_target_ids: list of geo target constant IDs (e.g. ["2276"] for Germany,
        ["2840"] for USA). REQUIRED — campaigns must target specific countries.
    language_ids: list of language constant IDs (e.g. ["1001"] for German,
        ["1000"] for English). REQUIRED — campaigns must target specific languages.
    """
    from adloop.safety.guards import (
        SafetyViolation,
        check_blocked_operation,
        check_budget_cap,
    )
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    normalized_display_network_enabled, alias_errors = _normalize_display_network_setting(
        display_network_enabled,
        display_expansion_enabled,
    )
    if alias_errors:
        return {"error": "Validation failed", "details": alias_errors}
    if normalized_display_network_enabled is None:
        normalized_display_network_enabled = False

    errors, warnings = _validate_campaign(
        config,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        channel_type=channel_type,
        keywords=keywords,
        geo_target_ids=geo_target_ids,
        language_ids=language_ids,
        customer_id=customer_id,
        search_partners_enabled=search_partners_enabled,
        display_network_enabled=normalized_display_network_enabled,
        max_cpc=max_cpc,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        check_budget_cap(daily_budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    plan = ChangePlan(
        operation="create_campaign",
        entity_type="campaign",
        customer_id=customer_id,
        changes={
            "campaign_name": campaign_name,
            "daily_budget": daily_budget,
            "bidding_strategy": bidding_strategy.upper(),
            "target_cpa": target_cpa if target_cpa else None,
            "target_roas": target_roas if target_roas else None,
            "channel_type": channel_type.upper(),
            "ad_group_name": ad_group_name or campaign_name,
            "keywords": keywords,
            "geo_target_ids": geo_target_ids or [],
            "language_ids": language_ids or [],
            "search_partners_enabled": search_partners_enabled,
            "display_network_enabled": normalized_display_network_enabled,
            "max_cpc": max_cpc if max_cpc else None,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_ad_group(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
    cpc_bid_micros: int = 0,
) -> dict:
    """Draft a new ad group within an existing campaign — returns preview.

    Creates: AdGroup (ENABLED, SEARCH_STANDARD) + optional Keywords.
    Ads are NOT included — use draft_responsive_search_ad separately
    after the ad group is created.

    cpc_bid_micros: Optional ad-group-level CPC bid in micros. Only relevant
        for campaigns using MANUAL_CPC bidding.
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_ad_group", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = _validate_ad_group(
        campaign_id=campaign_id,
        ad_group_name=ad_group_name,
        keywords=keywords,
        cpc_bid_micros=cpc_bid_micros,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    keywords = keywords or []
    preflight_errors, warnings = _preflight_ad_group_checks(
        config, customer_id, campaign_id, ad_group_name, keywords, cpc_bid_micros
    )
    if preflight_errors:
        return {"error": "Pre-flight check failed", "details": preflight_errors}

    plan = ChangePlan(
        operation="create_ad_group",
        entity_type="ad_group",
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "ad_group_name": ad_group_name,
            "keywords": keywords,
            "cpc_bid_micros": cpc_bid_micros,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def update_campaign(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    daily_budget: float = 0,
    geo_target_ids: list[str] | None = None,
    language_ids: list[str] | None = None,
    search_partners_enabled: bool | None = None,
    display_network_enabled: bool | None = None,
    display_expansion_enabled: bool | None = None,
    max_cpc: float = 0,
) -> dict:
    """Draft an update to an existing campaign — returns preview, does NOT execute.

    All parameters except campaign_id are optional — only include what you want
    to change. Geo/language targets are REPLACED entirely (not appended).
    """
    from adloop.safety.guards import (
        SafetyViolation,
        check_bid_increase,
        check_blocked_operation,
        check_budget_cap,
    )
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    warnings = []

    normalized_display_network_enabled, alias_errors = _normalize_display_network_setting(
        display_network_enabled,
        display_expansion_enabled,
    )
    errors.extend(alias_errors)

    if not campaign_id:
        errors.append("campaign_id is required")

    bs = bidding_strategy.upper() if bidding_strategy else ""
    if bs and bs not in _VALID_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(_VALID_BIDDING_STRATEGIES)}, "
            f"got '{bidding_strategy}'"
        )
    if bs == "TARGET_CPA" and not target_cpa:
        errors.append("target_cpa is required when bidding_strategy is TARGET_CPA")
    if bs == "TARGET_ROAS" and not target_roas:
        errors.append("target_roas is required when bidding_strategy is TARGET_ROAS")
    if max_cpc < 0:
        errors.append("max_cpc cannot be negative")

    if daily_budget and daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")

    if daily_budget:
        try:
            check_budget_cap(daily_budget, config.safety)
        except SafetyViolation as e:
            errors.append(str(e))

    if geo_target_ids is not None and len(geo_target_ids) == 0:
        errors.append("geo_target_ids cannot be empty — provide at least one geo target")
    if language_ids is not None and len(language_ids) == 0:
        errors.append("language_ids cannot be empty — provide at least one language")
    if max_cpc:
        strategy_for_cap = bs or _campaign_bidding_strategy(config, customer_id, campaign_id)
        if strategy_for_cap is None:
            errors.append("campaign_id was not found")
        elif strategy_for_cap != "TARGET_SPEND":
            errors.append("max_cpc requires TARGET_SPEND bidding_strategy")
        else:
            # Only a ceiling that already exists has a baseline; a campaign
            # switching to Maximize Clicks has none to compare against.
            current_ceiling = _current_campaign_cpc_ceiling(config, customer_id, campaign_id)
            try:
                check_bid_increase(current_ceiling or 0, max_cpc, config.safety)
            except SafetyViolation as e:
                errors.append(str(e))

    has_any_change = any([
        bs,
        daily_budget,
        geo_target_ids is not None,
        language_ids is not None,
        search_partners_enabled is not None,
        normalized_display_network_enabled is not None,
        max_cpc,
    ])
    if not has_any_change:
        errors.append("No changes specified — provide at least one parameter to update")

    if errors:
        return {"error": "Validation failed", "details": errors}

    if bs == "MANUAL_CPC":
        warnings.append(
            "MANUAL_CPC bidding requires constant monitoring. Consider using "
            "MAXIMIZE_CONVERSIONS or TARGET_CPA for automated optimization."
        )

    if daily_budget and target_cpa > 0 and daily_budget < 5 * target_cpa:
        from adloop.ads.currency import format_currency, get_currency_code
        currency_code = get_currency_code(config, customer_id)
        warnings.append(
            f"Daily budget {format_currency(daily_budget, currency_code)} is less than 5x target CPA "
            f"{format_currency(target_cpa, currency_code)}. Google recommends at least 5x."
        )

    # Surface negative geo exclusions that will survive a positive-geo
    # replacement. Issue #32: the previous implementation silently dropped
    # negative location criteria along with positive ones during a geo
    # replace — users only noticed when excluded-region traffic appeared in
    # reports. Negatives are now preserved (see ``_apply_update_campaign``);
    # this preview note makes the preserved-set explicit so the change is
    # auditable rather than implicit.
    preserved_negative_geos: list[str] = []
    if geo_target_ids is not None and campaign_id:
        preserved_negative_geos = _existing_negative_geo_exclusions(
            config, customer_id, campaign_id
        )
        if preserved_negative_geos:
            warnings.append(
                "Negative geo exclusions on this campaign will be PRESERVED "
                f"(geo_target_constant IDs: {sorted(preserved_negative_geos)}). "
                "Only positive geo targets are being replaced. To remove a "
                "negative exclusion, use remove_entity with "
                'entity_type="campaign_criterion".'
            )

    changes: dict = {"campaign_id": campaign_id}
    if bs:
        changes["bidding_strategy"] = bs
    if target_cpa:
        changes["target_cpa"] = target_cpa
    if target_roas:
        changes["target_roas"] = target_roas
    if daily_budget:
        changes["daily_budget"] = daily_budget
    if geo_target_ids is not None:
        changes["geo_target_ids"] = geo_target_ids
    if language_ids is not None:
        changes["language_ids"] = language_ids
    if search_partners_enabled is not None:
        changes["search_partners_enabled"] = search_partners_enabled
    if normalized_display_network_enabled is not None:
        changes["display_network_enabled"] = normalized_display_network_enabled
    if max_cpc:
        changes["max_cpc"] = max_cpc

    plan = ChangePlan(
        operation="update_campaign",
        entity_type="campaign",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes=changes,
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    if preserved_negative_geos:
        preview["preserved_negative_geo_target_ids"] = sorted(
            preserved_negative_geos
        )
    return preview


def draft_callouts(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    callouts: list[str] | None = None,
) -> dict:
    """Draft campaign callout assets."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_callouts", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_callouts, errors = _validate_callouts(campaign_id, callouts or [])
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_callouts",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "callouts": validated_callouts,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_structured_snippets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    snippets: list[dict] | None = None,
) -> dict:
    """Draft campaign structured snippet assets."""
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_structured_snippets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_snippets, errors = _validate_structured_snippets(
        campaign_id, snippets or []
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_structured_snippets",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "snippets": validated_snippets,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_image_assets(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    image_paths: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> dict:
    """Draft campaign image assets from local files and/or public image URLs.

    URL images are fetched now (public addresses only, pinned against DNS
    rebinding, capped at Google's 5120 KB limit) and their sha256 is kept
    in the plan; apply re-fetches and refuses if the bytes changed.
    """
    from adloop.runtime import deployment_mode
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    image_paths = list(image_paths or [])
    image_urls = list(image_urls or [])

    if image_paths and deployment_mode() == "server":
        return {
            "error": (
                "image_paths can't be used here: local file paths aren't "
                "available on a hosted server; pass image_urls (public "
                "https/http links to PNG, JPEG or GIF images) instead."
            )
        }

    try:
        check_blocked_operation("create_image_assets", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    validated_images, errors = _validate_image_assets(
        campaign_id, image_paths, image_urls
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="create_image_assets",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={
            "campaign_id": campaign_id,
            "images": validated_images,
            "summary": [_describe_image(image) for image in validated_images],
        },
    )
    store_plan(plan)
    return plan.to_preview()


def draft_sitelinks(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
    sitelinks: list[dict] | None = None,
) -> dict:
    """Draft sitelink extensions for a campaign — returns preview, does NOT execute.

    sitelinks: list of dicts, each with:
        - link_text (str, required, max 25 chars) — the clickable text
        - final_url (str, required) — where the sitelink points
        - description1 (str, optional, max 35 chars) — first description line
        - description2 (str, optional, max 35 chars) — second description line
    campaign_id: the campaign to attach sitelinks to
    """
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_sitelinks", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not campaign_id:
        return {"error": "campaign_id is required"}
    if not sitelinks:
        return {"error": "At least one sitelink is required"}

    errors = []
    warnings = []
    validated = []

    for i, sl in enumerate(sitelinks):
        link_text = sl.get("link_text", "").strip()
        final_url = sl.get("final_url", "").strip()
        desc1 = sl.get("description1", "").strip()
        desc2 = sl.get("description2", "").strip()

        if not link_text:
            errors.append(f"Sitelink {i + 1}: link_text is required")
        elif len(link_text) > 25:
            errors.append(
                f"Sitelink {i + 1}: link_text '{link_text}' is {len(link_text)} chars (max 25)"
            )
        if not final_url:
            errors.append(f"Sitelink {i + 1}: final_url is required")
        if desc1 and len(desc1) > 35:
            errors.append(
                f"Sitelink {i + 1}: description1 is {len(desc1)} chars (max 35)"
            )
        if desc2 and len(desc2) > 35:
            errors.append(
                f"Sitelink {i + 1}: description2 is {len(desc2)} chars (max 35)"
            )
        if desc2 and not desc1:
            warnings.append(
                f"Sitelink {i + 1}: description2 without description1 — Google may ignore it"
            )

        validated.append({
            "link_text": link_text,
            "final_url": final_url,
            "description1": desc1,
            "description2": desc2,
        })

    if errors:
        return {"error": "Validation failed", "details": errors}

    sitelink_urls = [sl["final_url"] for sl in validated]
    url_checks, url_warnings = _validate_urls(sitelink_urls)
    bad_urls = {u: err for u, err in url_checks.items() if err}
    if bad_urls:
        return {
            "error": "URL validation failed — sitelinks MUST point to working URLs",
            "details": [
                f"'{url}' is not reachable: {err}" for url, err in bad_urls.items()
            ],
        }
    warnings.extend(f"'{url}': {msg}" for url, msg in url_warnings.items())

    if len(validated) < 2:
        warnings.append(
            "Google recommends at least 4 sitelinks per campaign. "
            "Fewer than 2 may not show at all."
        )
    elif len(validated) < 4:
        warnings.append(
            f"Only {len(validated)} sitelinks — Google recommends at least 4 for "
            f"maximum ad real estate."
        )

    plan = ChangePlan(
        operation="create_sitelinks",
        entity_type="campaign_asset",
        entity_id=campaign_id,
        customer_id=customer_id,
        changes={"campaign_id": campaign_id, "sitelinks": validated},
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


# ---------------------------------------------------------------------------
# confirm_and_apply — the only function that actually mutates an ad account
# (Google Ads here; Reddit plans are dispatched to adloop.reddit.write and
# Tag Manager plans to adloop.gtm.write)
# ---------------------------------------------------------------------------


def _extract_error_message(exc: Exception) -> str:
    """Extract a meaningful error message from Google Ads API exceptions.

    GoogleAdsException.__init__ doesn't call super().__init__(), so str(e)
    returns ''. This function digs into the failure proto to surface the
    actual error code, message, and trigger values.
    """
    try:
        from google.ads.googleads.errors import GoogleAdsException

        if isinstance(exc, GoogleAdsException) and exc.failure:
            parts = []
            for error in exc.failure.errors:
                # The client returns proto-plus messages, on which WhichOneof
                # does not exist; read the underlying protobuf instead.
                pb = type(error).pb(error) if hasattr(type(error), "pb") else error
                code_field = pb.error_code.WhichOneof("error_code")
                code_name = "UNKNOWN"
                if code_field:
                    number = getattr(pb.error_code, code_field)
                    enum = pb.error_code.DESCRIPTOR.fields_by_name[code_field].enum_type
                    value = enum.values_by_number.get(number)
                    code_name = value.name if value else str(number)
                line = f"[{code_field}={code_name}]"
                if pb.message:
                    line += f" {pb.message}"
                if pb.HasField("trigger"):
                    kind = pb.trigger.WhichOneof("value")
                    if kind:
                        line += f" (trigger: {getattr(pb.trigger, kind)})"
                parts.append(line)
            if parts:
                msg = "; ".join(parts)
                if exc.request_id:
                    msg += f" [request_id={exc.request_id}]"
                return msg
    except Exception:
        pass

    fallback = str(exc)
    return fallback if fallback else repr(exc)


def _partial_upload_message(e: object) -> str:
    """The human-readable half of a ``PARTIAL_UPLOAD`` answer.

    The raw ``error`` and this message both reach the caller, so they must not
    disagree about what is safe to do next. When a batch's fate is unknown the
    message names exactly those CSV lines and tells the caller to check the
    conversion action first. The resume hint names the first row of the *next*
    batch, never the uncertain batch's own first line (that invites a
    duplicate upload), and it is ``None`` when nothing follows.
    """
    if getattr(e, "unknown_status", False):
        first, last = e.uncertain_lines
        rest = (
            f"Draft the remaining rows from line {e.resume_from_line}."
            if e.resume_from_line is not None
            else "No rows remain after these."
        )
        return (
            f"{e.uncertain_rows} row(s) in lines {first}-{last} may or may not "
            "have been received: check the conversion action for them first. "
            f"{rest} The plan is no longer pending, so it cannot resend "
            "anything."
        )
    return (
        f"{e.sent_total} row(s) were sent; the plan is no longer pending, so "
        "confirming it again cannot resend them. Resume the CSV at line "
        f"{e.resume_from_line} and draft the rest as a new upload."
    )


def confirm_and_apply(
    config: AdLoopConfig,
    *,
    plan_id: str = "",
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    Defaults to dry_run=True. The caller must explicitly pass dry_run=False
    to make real changes.
    """
    from adloop.safety.audit import log_mutation
    from adloop.safety.preview import claim_plan, get_plan, remove_plan, store_plan

    plan = get_plan(plan_id)
    if plan is None:
        return {
            "error": f"No pending plan found with id '{plan_id}'. "
            "Plans expire when the MCP server restarts.",
        }

    forced_by_config = bool(config.safety.require_dry_run) and not dry_run
    if config.safety.require_dry_run:
        dry_run = True

    is_reddit = plan.operation.startswith("reddit_")
    is_gtm = plan.operation.startswith("gtm_")
    platform_label = (
        "Reddit Ads" if is_reddit
        else "Google Tag Manager" if is_gtm
        else "Google Ads"
    )
    # ``plan.changes`` is the summary a preview may show; upload rows carry raw
    # PII in ``plan.apply_only_payload``, which never reaches the audit log or
    # a dry-run response.

    if dry_run:
        preflight_checks: dict | None = None
        validation: dict | None = None
        # Either way a failed check leaves dry_run_result unset, so two-phase
        # apply keeps refusing the real write.
        try:
            if is_reddit or is_gtm:
                # Neither Reddit nor Tag Manager has a validate-only mode; the
                # dry run re-reads the target and re-runs the safety checks
                # against live values.
                from adloop.gtm.write import preflight as gtm_preflight
                from adloop.reddit.write import preflight as reddit_preflight

                preflight = reddit_preflight if is_reddit else gtm_preflight
                preflight_checks = preflight(config, plan)
            elif plan.operation != "create_key_event":
                # Google Ads checks the exact mutates with validate_only=True
                # and executes nothing.
                validation = _validate_with_google(config, plan)
        except Exception as e:
            error_message = _extract_error_message(e)
            # Google reports per-conversion errors by request index; the upload
            # appliers translate them into CSV lines before raising.
            from adloop.ads.conversion_actions import PartialUploadError
            from adloop.ads.validate_only import ValidateOnlyFailure

            row_errors = (
                e.row_errors if isinstance(e, PartialUploadError) else []
            )
            # A validate-only partial failure is not a broken request: Google
            # rejected some operations and the apply would carry out the rest.
            # Saying "the real apply would fail the same way" is only true when
            # the request as a whole was rejected.
            partial_rejection = (
                isinstance(e, ValidateOnlyFailure)
                and getattr(e, "failure", None) is not None
            )
            log_mutation(
                config.safety.log_file,
                operation=plan.operation,
                customer_id=plan.customer_id,
                entity_type=plan.entity_type,
                entity_id=plan.entity_id,
                changes=plan.changes,
                dry_run=True,
                result="dry_run_failed",
                error=error_message,
            )
            checked_against = (
                f"re-checked the target against {platform_label}"
                if is_reddit or is_gtm
                else "sent the change to Google Ads in validate-only mode"
            )
            return {
                "status": "DRY_RUN_FAILED",
                "plan_id": plan.plan_id,
                "operation": plan.operation,
                "error": error_message,
                **({"row_errors": row_errors} if row_errors else {}),
                "message": (
                    f"The dry run {checked_against} and found a problem; "
                    "nothing was changed. "
                    + (
                        "Google rejected part of the request — the apply would "
                        "carry out the rest and report these operations again. "
                        if partial_rejection
                        else "The real apply would fail the same way. "
                    )
                    + "Fix the cause and draft again."
                ),
            }
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=True,
            result="dry_run_success",
        )
        if plan.dry_run_result is None:
            # Persist the dry-run pass on the plan; two-phase apply checks
            # this marker before allowing a real write. Re-storing
            # overwrites the pending plan (PlanStore.store is an upsert).
            from datetime import datetime, timezone

            plan.dry_run_result = {
                "status": "DRY_RUN_SUCCESS",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            store_plan(plan)
        response = {
            "status": "DRY_RUN_SUCCESS",
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "changes": plan.changes,
        }
        if preflight_checks is not None:
            response["checks"] = preflight_checks
            response["note"] = (
                f"{platform_label} has no validate-only mode: the dry run "
                "re-read the target and re-checked the safety gates; nothing "
                "was changed live."
                + (
                    " The publish preflight ran a Tag Manager quick preview, "
                    "which stores a preview version and publishes nothing."
                    if plan.operation == "gtm_publish_workspace"
                    else ""
                )
            )
        if validation is not None:
            row_errors = validation.pop("row_errors", [])
            response["checks"] = validation
            response["note"] = (
                "Google Ads validated this exact change (validate_only) and "
                "executed nothing."
            )
            if validation["skipped_calls"]:
                response["note"] += (
                    f" {validation['skipped_calls']} later step(s) build on "
                    "objects an earlier step would create, so Google could "
                    "only validate the step(s) before them."
                )
            if row_errors:
                # A per-row problem is not a broken request: the apply sends
                # the file and reports those rows again. Say that here instead
                # of letting the caller discover it at apply time.
                response["row_errors"] = row_errors
                # Only entries that name a CSV line are rows; the batch summary
                # that Google's own message produces is not one.
                per_row = [entry for entry in row_errors if "line" in entry]
                response["note"] += (
                    " Google reported problems "
                    + (f"for {len(per_row)} row(s) " if per_row else "")
                    + "in validate-only mode; the apply would send the file "
                    "anyway and carry out every other row."
                )

        from adloop.runtime import deployment_mode as _deployment_mode

        if forced_by_config and _deployment_mode() == "server":
            # Hosted: the flag is the workspace's live-changes setting,
            # switched in the dashboard. There is no config file to edit
            # and no server for the user to restart.
            response["dry_run_forced_by"] = "config.safety.require_dry_run"
            response["remediation"] = (
                "Live changes are switched off for this workspace (or for "
                "this client). The workspace owner can switch them on in "
                "the AdLoop Cloud dashboard under Settings → Safety. Passing "
                "dry_run=false on this tool will keep being overridden until "
                "then."
            )
            response["message"] = (
                "dry_run=false was IGNORED because live changes are switched "
                "off for this workspace. No changes were made. Retrying this "
                "tool with dry_run=false will not succeed until live changes "
                "are switched on in the dashboard."
            )
        elif forced_by_config:
            # The caller passed dry_run=false but safety.require_dry_run
            # forced it back on. Tell them exactly why and how to unlock
            # real writes — without this, agents (e.g. Claude Code) retry
            # in an infinite loop because the old message said to "call
            # again with dry_run=false", which they already did.
            config_path = config.source_path or "~/.adloop/config.yaml"
            response["dry_run_forced_by"] = "config.safety.require_dry_run"
            response["config_path"] = config_path
            response["remediation"] = (
                f"Edit {config_path}, set 'require_dry_run: false' under "
                "'safety:', then restart the AdLoop MCP server. Passing "
                "dry_run=false on this tool will keep being overridden "
                "until that flag is flipped."
            )
            response["message"] = (
                f"dry_run=false was IGNORED because 'safety.require_dry_run: true' "
                f"is set in {config_path}. No changes were made. To apply real "
                f"changes, flip that flag to false and restart the AdLoop MCP "
                f"server — retrying this tool with dry_run=false alone will "
                f"never succeed while the flag is on."
            )
        else:
            response["message"] = (
                f"Dry run completed — no changes were made to your {platform_label} "
                "account. To apply for real, call confirm_and_apply again with "
                "dry_run=false."
            )
        return response

    if config.safety.two_phase_apply and plan.dry_run_result is None:
        # Server-enforced two-phase apply: the preview→confirm flow is a
        # protocol requirement here, not a convention the calling agent
        # can skip. Refuse, log the refusal, and keep the plan pending.
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=False,
            result="refused_two_phase",
        )
        return {
            "status": "DRY_RUN_REQUIRED",
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "message": (
                f"No changes were made: two-phase apply is enabled and plan "
                f"'{plan.plan_id}' has not completed a dry run yet. Call "
                f"confirm_and_apply with dry_run=true once, show the result "
                f"to the user and get their approval, then call again with "
                f"dry_run=false — that second call will succeed."
            ),
        }

    # Claim the plan in one step so a second confirm while this one is still
    # running finds nothing to execute — the client may have timed out while the
    # first apply is happily uploading.
    claimed = claim_plan(plan.plan_id)
    if claimed is None:
        return {
            "status": "APPLY_IN_PROGRESS",
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "error": (
                f"Plan '{plan.plan_id}' is no longer pending: it is being "
                "applied right now or has already been applied. That is not a "
                "failure — wait for the first call to return, and check the "
                "account for what arrived before drafting anything again. For "
                "an upload, that check means the conversion action; for other "
                "operations, read the affected entity back."
            ),
        }
    plan = claimed

    # From here on the upload appliers tell us whether a request actually went
    # out; nothing below has to guess that from an exception type.
    from adloop.ads.conversion_actions import reset_send_state, sent_anything

    reset_send_state()
    try:
        result = _execute_plan(config, plan)
    except Exception as e:
        error_message = _extract_error_message(e)

        # An upload that stopped mid-way has already changed the account for
        # the batches that went through. Retire the plan: confirming it again —
        # the obvious reflex after an error — would resend those rows, and call
        # conversions have no dedup key to absorb the duplicates.
        from adloop.ads.conversion_actions import PartialUploadError

        # The plan stays usable unless a request may have reached Google. Only
        # the applier knows that moment, so it marks it — and a request Google
        # rejected outright wrote nothing, which the upload error says
        # explicitly. Anything else may have arrived and must not be retried.
        nothing_written = not sent_anything() or (
            isinstance(e, PartialUploadError)
            and not e.sent_total
            and not e.unknown_status
        )
        if not plan.operation.startswith("upload_") or nothing_written:
            store_plan(plan)

        if isinstance(e, PartialUploadError) and (e.sent_total or e.unknown_status):
            log_mutation(
                config.safety.log_file,
                operation=plan.operation,
                customer_id=plan.customer_id,
                entity_type=plan.entity_type,
                entity_id=plan.entity_id,
                changes=plan.changes,
                dry_run=False,
                result=(
                    "unknown_status"
                    if e.unknown_status
                    else "partial_upload"
                ),
                error=error_message,
            )
            remove_plan(plan.plan_id)
            # The message repeats what the raw error says, in the terms the
            # caller has to act on. For an unknown outcome the two must agree:
            # the uncertain batch is checked first, and the resume line points
            # after it, never at its first row.
            message = _partial_upload_message(e)
            return {
                "status": "PARTIAL_UPLOAD",
                "plan_id": plan.plan_id,
                "operation": plan.operation,
                "error": error_message,
                "sent_total": e.sent_total,
                **(
                    {
                        "accepted_total": sum(b["accepted"] for b in e.batches),
                        "rejected_total": sum(b["rejected"] for b in e.batches),
                    }
                    if e.batches
                    and all(
                        b.get("accepted") is not None
                        and b.get("rejected") is not None
                        for b in e.batches
                    )
                    else {}
                ),
                "batches": e.batches,
                "resume_from_line": e.resume_from_line,
                "unknown_status": e.unknown_status,
                **(
                    {"uncertain_lines": e.uncertain_lines}
                    if e.uncertain_lines
                    else {}
                ),
                **({"row_errors": e.row_errors} if e.row_errors else {}),
                "message": message,
            }

        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=False,
            result="error",
            error=error_message,
        )
        return {"error": error_message, "plan_id": plan.plan_id}

    log_mutation(
        config.safety.log_file,
        operation=plan.operation,
        customer_id=plan.customer_id,
        entity_type=plan.entity_type,
        entity_id=plan.entity_id,
        changes=plan.changes,
        dry_run=False,
        result="success",
    )
    remove_plan(plan.plan_id)

    return {
        "status": "APPLIED",
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

_VALID_MATCH_TYPES = {"EXACT", "PHRASE", "BROAD"}
_VALID_ENTITY_TYPES = {"campaign", "ad_group", "ad", "keyword"}
_REMOVABLE_ENTITY_TYPES = _VALID_ENTITY_TYPES | {
    "negative_keyword",
    "shared_criterion",
    "ad_group_criterion",
    "campaign_criterion",
    "campaign_asset",
    "asset",
    "customer_asset",
}

_SMART_BIDDING_STRATEGIES = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
}


def _campaign_uses_manual_cpc(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> bool | None:
    """Return True when the campaign exists and uses MANUAL_CPC."""
    bidding_strategy = _campaign_bidding_strategy(config, customer_id, campaign_id)
    if bidding_strategy is None:
        return None
    return bidding_strategy == "MANUAL_CPC"


def _campaign_bidding_strategy(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> str | None:
    """Return the bidding strategy type for the campaign, if it exists."""
    from adloop.ads.gaql import execute_query

    query = f"""
        SELECT campaign.bidding_strategy_type
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """
    rows = execute_query(config, customer_id, query)
    if not rows:
        return None
    return rows[0].get("campaign.bidding_strategy_type")


def _existing_negative_geo_exclusions(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> list[str]:
    """Return geo_target_constant IDs that are currently negative-excluded.

    Used by ``update_campaign`` to surface preserved negative-location
    criteria in the preview when ``geo_target_ids`` is being changed.
    Negative location criteria survive a positive-geo replacement (issue
    #32) — this helper makes that explicit in the preview so users can
    see what's staying. Returns an empty list on any query failure;
    surfacing exclusions is informational, not safety-critical.
    """
    from adloop.ads.gaql import execute_query

    query = f"""
        SELECT campaign_criterion.location.geo_target_constant
        FROM campaign_criterion
        WHERE campaign.id = {campaign_id}
          AND campaign_criterion.type = 'LOCATION'
          AND campaign_criterion.negative = TRUE
    """
    try:
        rows = execute_query(config, customer_id, query)
    except Exception:
        return []

    ids: list[str] = []
    for row in rows:
        gtc = row.get("campaign_criterion.location.geo_target_constant") or ""
        # gtc looks like "geoTargetConstants/2840" — strip prefix to numeric ID.
        if "/" in gtc:
            ids.append(gtc.rsplit("/", 1)[-1])
        elif gtc:
            ids.append(gtc)
    return ids


def _current_ad_group_cpc_bid(
    config: AdLoopConfig, customer_id: str, ad_group_id: str
) -> float | None:
    """The ad group's current manual CPC bid in account currency, or None."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(config, customer_id, f"""
        SELECT ad_group.cpc_bid_micros
        FROM ad_group
        WHERE ad_group.id = {ad_group_id}
        LIMIT 1
    """)
    micros = rows[0].get("ad_group.cpc_bid_micros") if rows else None
    return int(micros) / 1_000_000 if micros else None


def _current_campaign_cpc_ceiling(
    config: AdLoopConfig, customer_id: str, campaign_id: str
) -> float | None:
    """The campaign's current Maximize Clicks CPC ceiling, or None if unset."""
    from adloop.ads.gaql import execute_query

    rows = execute_query(config, customer_id, f"""
        SELECT campaign.target_spend.cpc_bid_ceiling_micros
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """)
    micros = rows[0].get("campaign.target_spend.cpc_bid_ceiling_micros") if rows else None
    return int(micros) / 1_000_000 if micros else None


def _ad_group_campaign_bidding_strategy(
    config: AdLoopConfig, customer_id: str, ad_group_id: str
) -> str | None:
    """Return the bidding strategy type of the campaign owning this ad group.

    Returns the enum name (``MANUAL_CPC``, ``TARGET_SPEND``,
    ``MAXIMIZE_CONVERSIONS``, ``TARGET_CPA``, ``TARGET_ROAS``, etc.) or
    ``None`` when the ad group can't be resolved.
    """
    from adloop.ads.gaql import execute_query

    query = f"""
        SELECT campaign.bidding_strategy_type
        FROM ad_group
        WHERE ad_group.id = {ad_group_id}
        LIMIT 1
    """
    rows = execute_query(config, customer_id, query)
    if not rows:
        return None
    return rows[0].get("campaign.bidding_strategy_type")


def _validate_callouts(
    campaign_id: str, callouts: list[str]
) -> tuple[list[str], list[str]]:
    errors = []
    validated = []

    if not campaign_id:
        errors.append("campaign_id is required")
    if not callouts:
        errors.append("At least one callout is required")

    for index, callout in enumerate(callouts):
        text = callout.strip()
        if not text:
            errors.append(f"Callout {index + 1}: text is required")
        elif len(text) > 25:
            errors.append(
                f"Callout {index + 1}: '{text}' is {len(text)} chars (max 25)"
            )
        else:
            validated.append(text)

    return validated, errors


def _validate_structured_snippets(
    campaign_id: str, snippets: list[dict]
) -> tuple[list[dict], list[str]]:
    errors = []
    validated = []

    if not campaign_id:
        errors.append("campaign_id is required")
    if not snippets:
        errors.append("At least one structured snippet is required")

    for index, snippet in enumerate(snippets):
        header = snippet.get("header", "").strip()
        values = [value.strip() for value in snippet.get("values", [])]

        if header not in _STRUCTURED_SNIPPET_HEADERS:
            errors.append(
                f"Structured snippet {index + 1}: header must be one of "
                f"{sorted(_STRUCTURED_SNIPPET_HEADERS)}"
            )
        if len(values) < 3 or len(values) > 10:
            errors.append(
                f"Structured snippet {index + 1}: values must contain 3-10 items"
            )
        for value_index, value in enumerate(values):
            if not value:
                errors.append(
                    f"Structured snippet {index + 1}: value {value_index + 1} is required"
                )
            elif len(value) > 25:
                errors.append(
                    f"Structured snippet {index + 1}: value '{value}' is "
                    f"{len(value)} chars (max 25)"
                )

        validated.append({"header": header, "values": values})

    return validated, errors


def _validate_image_assets(
    campaign_id: str, image_paths: list[str], image_urls: list[str] | None = None
) -> tuple[list[dict[str, object]], list[str]]:
    errors = []
    validated = []
    image_urls = image_urls or []

    if not campaign_id:
        errors.append("campaign_id is required")
    if not image_paths and not image_urls:
        errors.append("At least one image (image_paths or image_urls) is required")

    for index, image_path in enumerate(image_paths):
        try:
            validated.append(_parse_image_metadata(image_path))
        except ValueError as exc:
            errors.append(f"Image {index + 1}: {exc}")

    for index, image_url in enumerate(image_urls):
        try:
            validated.append(_parse_image_url_metadata(image_url))
        except ValueError as exc:
            errors.append(f"Image URL {index + 1} ({image_url}): {exc}")

    return validated, errors


def _check_broad_match_safety(
    config: AdLoopConfig,
    customer_id: str,
    ad_group_id: str,
    keywords: list[dict],
) -> list[str]:
    """Warn if BROAD match keywords are being added to a non-Smart Bidding campaign."""
    has_broad = any(
        (kw.get("match_type") or "").upper() == "BROAD" for kw in keywords
    )
    if not has_broad:
        return []

    try:
        from adloop.ads.gaql import execute_query

        query = f"""
            SELECT campaign.bidding_strategy_type, campaign.name
            FROM ad_group
            WHERE ad_group.id = {ad_group_id}
        """
        rows = execute_query(config, customer_id, query)
        if not rows:
            return []

        bidding = rows[0].get("campaign.bidding_strategy_type", "")
        campaign_name = rows[0].get("campaign.name", "")

        if bidding not in _SMART_BIDDING_STRATEGIES:
            return [
                f"DANGEROUS: Adding BROAD match keywords to campaign "
                f"'{campaign_name}' which uses {bidding} bidding. "
                f"Broad Match without Smart Bidding (tCPA/tROAS/Maximize Conversions) "
                f"leads to irrelevant matches and wasted budget. "
                f"Use PHRASE or EXACT match instead, or switch the campaign "
                f"to Smart Bidding first."
            ]
    except Exception:
        pass

    return []


def _validate_rsa_assets(
    headlines: list[dict],
    descriptions: list[dict],
    enforce_headline_count: bool = True,
    enforce_description_count: bool = True,
) -> list[str]:
    """Validate RSA headline/description content: count, char limits, pin slots.

    Shared by ``_validate_rsa`` (full RSA create) and
    ``update_responsive_search_ad`` (in-place replace of headlines/descriptions).
    Google enforces 3-15 headlines / 2-4 descriptions whenever a list is sent.
    The update path passes ``enforce_*_count=False`` for a list it is NOT
    replacing, since an omitted list stays untouched on the live ad — only the
    supplied list is gated on count.
    """
    errors: list[str] = []
    if enforce_headline_count:
        if len(headlines) < 3:
            errors.append(f"Need at least 3 headlines, got {len(headlines)}")
        if len(headlines) > 15:
            errors.append(f"Maximum 15 headlines, got {len(headlines)}")
    if enforce_description_count:
        if len(descriptions) < 2:
            errors.append(f"Need at least 2 descriptions, got {len(descriptions)}")
        if len(descriptions) > 4:
            errors.append(f"Maximum 4 descriptions, got {len(descriptions)}")

    headline_pin_counts: dict[str, int] = {}
    for i, h in enumerate(headlines):
        text = h["text"]
        pin = h["pinned_field"]
        if len(text) > 30:
            errors.append(
                f"Headline {i + 1} exceeds 30 chars ({len(text)}): '{text}'"
            )
        if pin is not None:
            if pin not in _VALID_HEADLINE_PINS:
                errors.append(
                    f"Headline {i + 1} pinned_field '{pin}' invalid; "
                    f"must be one of {sorted(_VALID_HEADLINE_PINS)} or null"
                )
            else:
                headline_pin_counts[pin] = headline_pin_counts.get(pin, 0) + 1
    for pin, count in headline_pin_counts.items():
        if count > 2:
            errors.append(f"At most 2 headlines may pin to {pin}; got {count}")

    description_pin_counts: dict[str, int] = {}
    for i, d in enumerate(descriptions):
        text = d["text"]
        pin = d["pinned_field"]
        if len(text) > 90:
            errors.append(
                f"Description {i + 1} exceeds 90 chars ({len(text)}): '{text}'"
            )
        if pin is not None:
            if pin not in _VALID_DESCRIPTION_PINS:
                errors.append(
                    f"Description {i + 1} pinned_field '{pin}' invalid; "
                    f"must be one of {sorted(_VALID_DESCRIPTION_PINS)} or null"
                )
            else:
                description_pin_counts[pin] = description_pin_counts.get(pin, 0) + 1
    for pin, count in description_pin_counts.items():
        if count > 1:
            errors.append(f"At most 1 description may pin to {pin}; got {count}")

    return errors


def _validate_rsa(
    ad_group_id: str,
    headlines: list[dict],
    descriptions: list[dict],
    final_url: str,
) -> list[str]:
    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if not final_url:
        errors.append("final_url is required")
    errors.extend(_validate_rsa_assets(headlines, descriptions))

    return errors


_VALID_BIDDING_STRATEGIES = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
    "TARGET_SPEND",
    "MANUAL_CPC",
}

_VALID_CHANNEL_TYPES = {"SEARCH", "DISPLAY", "SHOPPING", "VIDEO", "PERFORMANCE_MAX"}


def _validate_campaign(
    config: AdLoopConfig,
    *,
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    target_cpa: float,
    target_roas: float,
    channel_type: str,
    keywords: list[dict] | None,
    geo_target_ids: list[str] | None,
    language_ids: list[str] | None,
    customer_id: str = "",
    search_partners_enabled: bool = False,
    display_network_enabled: bool = False,
    max_cpc: float = 0,
) -> tuple[list[str], list[str]]:
    """Validate campaign draft inputs. Returns (errors, warnings)."""
    errors = []
    warnings = []

    if not campaign_name or not campaign_name.strip():
        errors.append("campaign_name is required")
    if daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")
    if not geo_target_ids:
        errors.append(
            "geo_target_ids is required — campaigns must target at least one "
            "country/region (e.g. ['2276'] for Germany, ['2840'] for USA)"
        )
    if not language_ids:
        errors.append(
            "language_ids is required — campaigns must target at least one "
            "language (e.g. ['1001'] for German, ['1000'] for English)"
        )

    bs = bidding_strategy.upper()
    if bs not in _VALID_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(_VALID_BIDDING_STRATEGIES)}, "
            f"got '{bidding_strategy}'"
        )
    if bs == "TARGET_CPA" and not target_cpa:
        errors.append("target_cpa is required when bidding_strategy is TARGET_CPA")
    if bs == "TARGET_ROAS" and not target_roas:
        errors.append("target_roas is required when bidding_strategy is TARGET_ROAS")

    ct = channel_type.upper()
    if ct not in _VALID_CHANNEL_TYPES:
        errors.append(
            f"channel_type must be one of {sorted(_VALID_CHANNEL_TYPES)}, "
            f"got '{channel_type}'"
        )

    # Performance Max has no ad groups. Every campaign needs at least one
    # asset group, and the API requires the asset group plus all of its
    # required assets in a single atomic mutate. We cannot supply the
    # images, so creating the campaign alone would leave the user with
    # something that can never serve while reporting success.
    if ct == "PERFORMANCE_MAX":
        errors.append(
            "Performance Max campaigns cannot be created yet. PMax has no ad "
            "groups: a campaign needs at least one asset group, and Google "
            "requires the asset group and all its assets (headlines, "
            "descriptions, business name, logo, and images in several aspect "
            "ratios) in one atomic request. Creating the campaign on its own "
            "would produce a campaign that can never serve. Create it in the "
            "Google Ads interface, then use AdLoop to analyse it — "
            "get_pmax_performance reports per-asset-group Ad Strength."
        )
    elif ct != "SEARCH":
        # The create path builds a Search campaign: a SEARCH_STANDARD ad
        # group, keywords and language criteria. Shopping also needs a
        # shopping_setting with the Merchant Center ID, a SHOPPING_PRODUCT_ADS
        # ad group, a product ad and a listing-group tree, and refuses
        # language criteria; Display and Video need their own ad group types.
        # Google rejects every one of these plans, so refuse them at draft
        # instead of letting the dry run or the apply discover it.
        errors.append(
            f"{ct} campaigns cannot be created yet: AdLoop builds Search "
            f"campaigns only, and Google rejects a {ct} campaign without its "
            f"channel-specific settings and ad group type. Create it in the "
            f"Google Ads interface, then use AdLoop to analyse and manage it."
        )

    if ct != "SEARCH" and search_partners_enabled:
        errors.append("search_partners_enabled is only supported for SEARCH campaigns")
    if ct != "SEARCH" and display_network_enabled:
        errors.append("display_network_enabled is only supported for SEARCH campaigns")
    if max_cpc < 0:
        errors.append("max_cpc cannot be negative")
    if max_cpc and bs not in {"MANUAL_CPC", "TARGET_SPEND"}:
        errors.append("max_cpc requires MANUAL_CPC or TARGET_SPEND bidding_strategy")

    if keywords:
        has_broad = any(
            (kw.get("match_type") or "").upper() == "BROAD" for kw in keywords
        )
        if has_broad and bs not in _SMART_BIDDING_STRATEGIES:
            errors.append(
                f"BROAD match keywords require Smart Bidding "
                f"(tCPA/tROAS/Maximize Conversions). "
                f"'{bidding_strategy}' is not a Smart Bidding strategy. "
                f"Use PHRASE or EXACT match instead."
            )
        for i, kw in enumerate(keywords):
            if not kw.get("text"):
                errors.append(f"Keyword {i + 1} has no text")
            mt = (kw.get("match_type") or "").upper()
            if mt not in _VALID_MATCH_TYPES:
                errors.append(
                    f"Keyword {i + 1} has invalid match_type '{mt}' "
                    "(must be EXACT, PHRASE, or BROAD)"
                )

    if target_cpa > 0 and daily_budget < 5 * target_cpa:
        from adloop.ads.currency import format_currency, get_currency_code
        currency_code = get_currency_code(config, customer_id)
        warnings.append(
            f"Daily budget {format_currency(daily_budget, currency_code)} is less than 5x target CPA "
            f"{format_currency(target_cpa, currency_code)}. Google recommends at least 5x target CPA "
            f"({format_currency(5 * target_cpa, currency_code)}/day) for sufficient learning data."
        )

    if bs == "MANUAL_CPC":
        warnings.append(
            "MANUAL_CPC bidding requires constant monitoring. Consider using "
            "MAXIMIZE_CONVERSIONS or TARGET_CPA for automated optimization."
        )

    return errors, warnings


def _validate_keywords(ad_group_id: str, keywords: list[dict]) -> list[str]:
    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if not keywords:
        errors.append("At least one keyword is required")
    for i, kw in enumerate(keywords):
        if not kw.get("text"):
            errors.append(f"Keyword {i + 1} has no text")
        mt = (kw.get("match_type") or "").upper()
        if mt not in _VALID_MATCH_TYPES:
            errors.append(
                f"Keyword {i + 1} has invalid match_type '{mt}' "
                "(must be EXACT, PHRASE, or BROAD)"
            )
    return errors


def _validate_ad_group(
    *,
    campaign_id: str,
    ad_group_name: str,
    keywords: list[dict] | None,
    cpc_bid_micros: int,
) -> list[str]:
    """Validate inputs for draft_ad_group."""
    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not ad_group_name or not ad_group_name.strip():
        errors.append("ad_group_name is required")
    if cpc_bid_micros < 0:
        errors.append("cpc_bid_micros must be >= 0")
    if keywords:
        for i, kw in enumerate(keywords):
            if not kw.get("text"):
                errors.append(f"Keyword {i + 1} has no text")
            mt = (kw.get("match_type") or "").upper()
            if mt not in _VALID_MATCH_TYPES:
                errors.append(
                    f"Keyword {i + 1} has invalid match_type '{mt}' "
                    "(must be EXACT, PHRASE, or BROAD)"
                )
    return errors


def _preflight_ad_group_checks(
    config: AdLoopConfig,
    customer_id: str,
    campaign_id: str,
    ad_group_name: str,
    keywords: list[dict],
    cpc_bid_micros: int,
) -> tuple[list[str], list[str]]:
    """Run pre-flight checks before creating an ad group.

    Returns (errors, warnings). Errors block the draft; warnings are informational.

    Checks performed:
    1. Campaign must be a SEARCH campaign (error if not).
    2. Warn if cpc_bid_micros is set but campaign uses Smart Bidding (ignored).
    3. Warn if BROAD match keywords + non-Smart Bidding campaign.
    4. Warn if an ad group with the same name already exists in the campaign.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        from adloop.ads.gaql import execute_query

        # Query 1: campaign info (type, bidding, name)
        campaign_query = f"""
            SELECT campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   campaign.name
            FROM campaign
            WHERE campaign.id = {campaign_id}
        """
        rows = execute_query(config, customer_id, campaign_query)
        if not rows:
            errors.append(
                f"Campaign {campaign_id} not found. Verify the campaign ID "
                "using get_campaign_performance."
            )
            return errors, warnings

        row = rows[0]
        channel_type = row.get("campaign.advertising_channel_type", "")
        bidding = row.get("campaign.bidding_strategy_type", "")
        campaign_name = row.get("campaign.name", "")

        # Check 1: campaign type must be SEARCH
        if channel_type and channel_type != "SEARCH":
            errors.append(
                f"Campaign '{campaign_name}' is a {channel_type} campaign. "
                "draft_ad_group only supports SEARCH campaigns."
            )

        # Check 2: cpc_bid_micros on Smart Bidding is ignored
        if cpc_bid_micros and bidding in _SMART_BIDDING_STRATEGIES:
            warnings.append(
                f"Campaign '{campaign_name}' uses {bidding} (Smart Bidding). "
                "The cpc_bid_micros value will be ignored — Smart Bidding "
                "sets bids automatically."
            )

        # Check 3: BROAD match + non-Smart Bidding
        has_broad = any(
            (kw.get("match_type") or "").upper() == "BROAD" for kw in keywords
        )
        if has_broad and bidding not in _SMART_BIDDING_STRATEGIES:
            warnings.append(
                f"DANGEROUS: Adding BROAD match keywords to campaign "
                f"'{campaign_name}' which uses {bidding} bidding. "
                f"Broad Match without Smart Bidding (tCPA/tROAS/Maximize "
                f"Conversions) leads to irrelevant matches and wasted budget. "
                f"Use PHRASE or EXACT match instead, or switch the campaign "
                f"to Smart Bidding first."
            )

        # Check 4: existing ad groups (duplicate name check)
        ag_query = f"""
            SELECT ad_group.name
            FROM ad_group
            WHERE campaign.id = {campaign_id}
        """
        ag_rows = execute_query(config, customer_id, ag_query)
        existing_names = {r.get("ad_group.name", "") for r in ag_rows}
        if ad_group_name in existing_names:
            warnings.append(
                f"An ad group named '{ad_group_name}' already exists in "
                f"campaign '{campaign_name}'. This will create a duplicate. "
                f"Consider using a different name to avoid confusion."
            )

    except Exception as exc:
        # Surface preflight failures as warnings so users know checks
        # were skipped, rather than silently producing a clean preview.
        warnings.append(
            f"Preflight checks could not complete ({exc}). "
            "The draft will proceed, but some validations were skipped. "
            "Full validation happens at confirm_and_apply time."
        )

    return errors, warnings


def _draft_status_change(
    config: AdLoopConfig,
    operation: str,
    customer_id: str,
    entity_type: str,
    entity_id: str,
    target_status: str,
) -> dict:
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = []
    if entity_type not in _VALID_ENTITY_TYPES:
        errors.append(
            f"entity_type must be one of {_VALID_ENTITY_TYPES}, got '{entity_type}'"
        )
    if not entity_id:
        errors.append("entity_id is required")
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        customer_id=customer_id,
        changes={"target_status": target_status},
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# Execution — actual Google Ads API mutate calls
# ---------------------------------------------------------------------------


_MUTATE_RESPONSE_RESULT_FIELDS = [
    "campaign_budget_result",
    "campaign_result",
    "ad_group_result",
    "ad_group_ad_result",
    "ad_group_criterion_result",
    "campaign_criterion_result",
    "asset_result",
    "campaign_asset_result",
    "customer_asset_result",
]


def _extract_resource_name(resp: object) -> str:
    """Extract the resource_name from a MutateOperationResponse.

    Uses direct field access instead of WhichOneof, which doesn't work on
    proto-plus wrapped messages returned by the google-ads library.
    """
    for field in _MUTATE_RESPONSE_RESULT_FIELDS:
        try:
            result = getattr(resp, field, None)
            if result and result.resource_name:
                return result.resource_name
        except Exception:
            continue
    return ""


def _validate_with_google(config: AdLoopConfig, plan: object) -> dict:
    """Send a Google Ads plan with validate_only=True; raises if Google rejects it."""
    return _execute_plan(config, plan, validate_only=True)


def _execute_plan(
    config: AdLoopConfig, plan: object, *, validate_only: bool = False
) -> dict:
    """Dispatch to the right API call based on plan.operation.

    With ``validate_only`` the plan's Google Ads mutates are sent with
    ``validate_only=True`` (nothing executes) and the return value reports
    how many calls Google validated; any rejection raises.
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    # Reddit plans have their own executors and never touch Google: the
    # branch sits before the Ads client is built AND before the customer
    # id is normalised (Reddit ids are alphanumeric).
    if plan.operation.startswith("reddit_"):
        from adloop.reddit.write import apply_plan

        return apply_plan(config, plan)

    # Tag Manager plans likewise never touch the Ads client.
    if plan.operation.startswith("gtm_"):
        from adloop.gtm.write import apply_plan as apply_gtm_plan

        return apply_gtm_plan(config, plan)

    # GA4 plans dispatch before Ads client construction so they work for
    # GA4-only setups (no Ads credentials/developer token required).
    if plan.operation == "create_key_event":
        from adloop.ga4.write import _apply_create_key_event

        return _apply_create_key_event(config, plan.changes)

    try:
        client = get_ads_client(config)
        cid = normalize_customer_id(plan.customer_id)
    except Exception as exc:  # noqa: BLE001 — uploads stay retryable
        if plan.operation.startswith("upload_"):
            # Credentials or client construction failed before anything could
            # leave the process. Retiring the plan here would force a whole new
            # draft for a retry that cannot have duplicated anything.
            from adloop.ads.conversion_actions import UploadNotSentError

            raise UploadNotSentError(
                "Could not reach Google Ads for this upload, so nothing was "
                f"sent and the plan is still usable: {exc}"
            ) from exc
        raise

    if validate_only:
        from adloop.ads.validate_only import ValidateOnlyClient

        validator = ValidateOnlyClient(client)
        result = _dispatch_ads_plan(validator, cid, plan)
        # Multi-step applies catch their own errors and report them in the
        # result instead of raising; in a dry run that is still a failure.
        if isinstance(result, dict) and result.get("error"):
            raise ValueError(result["error"])
        validation = {
            "validated_calls": validator.validated_calls,
            "skipped_calls": validator.skipped_calls,
        }
        # Uploads report per-row problems instead of failing the request (see
        # ValidateOnlyClient), so their detail has to reach the caller.
        if isinstance(result, dict) and result.get("row_errors"):
            validation["row_errors"] = result["row_errors"]
        if validator.partial_failures:
            validation["partial_failures"] = validator.partial_failures
        return validation

    return _dispatch_ads_plan(client, cid, plan)


def _dispatch_ads_plan(client: object, cid: str, plan: object) -> dict:
    """Run a Google Ads plan against ``client`` (real or validate-only)."""
    # Conversion-action CRUD lives in its own module; import lazily so the
    # dispatch table (and this module) don't take the dependency at import time.
    from adloop.ads.conversion_actions import (
        _apply_create_conversion_action,
        _apply_remove_conversion_action,
        _apply_update_conversion_action,
        _apply_upload_call_conversions,
        _apply_upload_enhanced_conversions_for_leads,
    )
    # Custom conversion goals live in their own module for the same reason.
    from adloop.ads.custom_conversion_goals import (
        _apply_assign_custom_conversion_goal,
        _apply_clear_custom_conversion_goal,
        _apply_create_custom_conversion_goal,
        _apply_update_custom_conversion_goal,
    )

    dispatch = {
        "create_campaign": _apply_create_campaign,
        "create_ad_group": _apply_create_ad_group,
        "update_campaign": _apply_update_campaign,
        "update_ad_group": _apply_update_ad_group,
        "create_responsive_search_ad": _apply_create_rsa,
        "update_responsive_search_ad": _apply_update_rsa,
        "add_keywords": _apply_add_keywords,
        "add_negative_keywords": _apply_add_negative_keywords,
        "add_negative_locations": _apply_add_negative_locations,
        "create_negative_keyword_list": _apply_create_negative_keyword_list,
        "add_to_negative_keyword_list": _apply_add_to_negative_keyword_list,
        "attach_shared_set_to_campaigns": _apply_attach_shared_set_to_campaigns,
        "detach_shared_set_from_campaigns": _apply_detach_shared_set_from_campaigns,
        "add_demographic_criteria": _apply_add_demographic_criteria,
        "create_brand_list": _apply_create_brand_list,
        "add_to_brand_list": _apply_add_to_brand_list,
        "remove_from_brand_list": _apply_remove_from_brand_list,
        "attach_brand_list_to_campaigns": _apply_attach_brand_list_to_campaigns,
        "detach_brand_list_from_campaigns": _apply_detach_brand_list_from_campaigns,
        "update_ai_max_settings": _apply_ai_max_settings,
        "create_custom_conversion_goal": _apply_create_custom_conversion_goal,
        "update_custom_conversion_goal": _apply_update_custom_conversion_goal,
        "assign_custom_conversion_goal": _apply_assign_custom_conversion_goal,
        "clear_custom_conversion_goal": _apply_clear_custom_conversion_goal,
        "pause_entity": _apply_status_change,
        "enable_entity": _apply_status_change,
        "remove_entity": _apply_remove,
        "create_callouts": _apply_create_callouts,
        "create_structured_snippets": _apply_create_structured_snippets,
        "create_image_assets": _apply_create_image_assets,
        "create_sitelinks": _apply_create_sitelinks,
        "create_conversion_action": _apply_create_conversion_action,
        "update_conversion_action": _apply_update_conversion_action,
        "remove_conversion_action": _apply_remove_conversion_action,
        "upload_call_conversions": _apply_upload_call_conversions,
        "upload_enhanced_conversions_for_leads": (
            _apply_upload_enhanced_conversions_for_leads
        ),
    }

    handler = dispatch.get(plan.operation)
    if handler is None:
        raise ValueError(f"Unknown operation: {plan.operation}")

    if plan.operation in ("pause_entity", "enable_entity"):
        return handler(
            client,
            cid,
            plan.entity_type,
            plan.entity_id,
            plan.changes["target_status"],
        )

    if plan.operation == "remove_entity":
        return handler(client, cid, plan.entity_type, plan.entity_id)

    return handler(client, cid, plan.apply_payload())


def _apply_update_ad_group(client: object, cid: str, changes: dict) -> dict:
    """Update an ad group's name and/or manual CPC bid."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = service.ad_group_path(cid, changes["ad_group_id"])

    field_paths = []
    if changes.get("ad_group_name"):
        ad_group.name = changes["ad_group_name"]
        field_paths.append("name")
    if changes.get("max_cpc"):
        ad_group.cpc_bid_micros = int(changes["max_cpc"] * 1_000_000)
        field_paths.append("cpc_bid_micros")

    operation.update_mask = field_mask_pb2.FieldMask(paths=field_paths)
    response = service.mutate_ad_groups(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_create_campaign(client: object, cid: str, changes: dict) -> dict:
    """Create campaign + budget + ad group + optional keywords atomically."""
    service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")
    budget_service = client.get_service("CampaignBudgetService")
    ad_group_service = client.get_service("AdGroupService")

    operations = []

    # 1. CampaignBudget (temp ID: -1)
    budget_op = client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_service.campaign_budget_path(cid, "-1")
    budget.name = f"Budget - {changes['campaign_name']}"
    budget.amount_micros = int(changes["daily_budget"] * 1_000_000)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(budget_op)

    # 2. Campaign (temp ID: -2, references budget -1)
    campaign_op = client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_service.campaign_path(cid, "-2")
    campaign.name = changes["campaign_name"]
    campaign.campaign_budget = budget_service.campaign_budget_path(cid, "-1")
    campaign.status = client.enums.CampaignStatusEnum.PAUSED

    channel = changes.get("channel_type", "SEARCH")
    campaign.advertising_channel_type = getattr(
        client.enums.AdvertisingChannelTypeEnum, channel
    )

    bs = changes["bidding_strategy"]
    if bs == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions.target_cpa_micros = 0
        if changes.get("target_cpa"):
            campaign.maximize_conversions.target_cpa_micros = int(
                changes["target_cpa"] * 1_000_000
            )
    elif bs == "TARGET_CPA":
        campaign.maximize_conversions.target_cpa_micros = int(
            changes["target_cpa"] * 1_000_000
        )
    elif bs == "MAXIMIZE_CONVERSION_VALUE":
        campaign.maximize_conversion_value.target_roas = 0
        if changes.get("target_roas"):
            campaign.maximize_conversion_value.target_roas = changes["target_roas"]
    elif bs == "TARGET_ROAS":
        campaign.maximize_conversion_value.target_roas = changes["target_roas"]
    elif bs == "TARGET_SPEND":
        campaign.target_spend.target_spend_micros = 0
        if changes.get("max_cpc"):
            campaign.target_spend.cpc_bid_ceiling_micros = int(
                changes["max_cpc"] * 1_000_000
            )
    elif bs == "MANUAL_CPC":
        campaign.manual_cpc.enhanced_cpc_enabled = False

    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = changes.get(
        "search_partners_enabled", False
    )
    campaign.network_settings.target_content_network = changes.get(
        "display_network_enabled", False
    )

    # EU political advertising declaration — required for campaigns that may
    # serve in EU countries. This is an ENUM, not a bool. Value 3 means
    # "does not contain EU political advertising" (the default for most users).
    # Setting False/0 maps to UNSPECIFIED which proto3 strips from the wire.
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )

    operations.append(campaign_op)

    # 3. AdGroup (temp ID: -3, references campaign -2)
    ag_op = client.get_type("MutateOperation")
    ad_group = ag_op.ad_group_operation.create
    ad_group.resource_name = ad_group_service.ad_group_path(cid, "-3")
    ad_group.name = changes.get("ad_group_name", changes["campaign_name"])
    ad_group.campaign = campaign_service.campaign_path(cid, "-2")
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if bs == "MANUAL_CPC" and changes.get("max_cpc"):
        ad_group.cpc_bid_micros = int(changes["max_cpc"] * 1_000_000)
    operations.append(ag_op)

    # 4. Keywords (reference ad_group -3)
    kw_list = changes.get("keywords") or []
    for kw in kw_list:
        kw_op = client.get_type("MutateOperation")
        criterion = kw_op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_service.ad_group_path(cid, "-3")
        criterion.keyword.text = kw["text"]
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"].upper()
        )
        operations.append(kw_op)

    # 5. Geo targeting (CampaignCriterion referencing campaign -2)
    for geo_id in changes.get("geo_target_ids") or []:
        geo_op = client.get_type("MutateOperation")
        geo_criterion = geo_op.campaign_criterion_operation.create
        geo_criterion.campaign = campaign_service.campaign_path(cid, "-2")
        geo_criterion.location.geo_target_constant = (
            f"geoTargetConstants/{geo_id}"
        )
        operations.append(geo_op)

    # 6. Language targeting (CampaignCriterion referencing campaign -2)
    for lang_id in changes.get("language_ids") or []:
        lang_op = client.get_type("MutateOperation")
        lang_criterion = lang_op.campaign_criterion_operation.create
        lang_criterion.campaign = campaign_service.campaign_path(cid, "-2")
        lang_criterion.language.language_constant = (
            f"languageConstants/{lang_id}"
        )
        operations.append(lang_op)

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results = {}
    num_keywords = len(kw_list)
    num_geo = len(changes.get("geo_target_ids") or [])
    for i, resp in enumerate(response.mutate_operation_responses):
        rn = _extract_resource_name(resp)
        if rn:
            if i == 0:
                results["campaign_budget"] = rn
            elif i == 1:
                results["campaign"] = rn
            elif i == 2:
                results["ad_group"] = rn
            elif i < 3 + num_keywords:
                results.setdefault("keywords", []).append(rn)
            elif i < 3 + num_keywords + num_geo:
                results.setdefault("geo_targets", []).append(rn)
            else:
                results.setdefault("language_targets", []).append(rn)

    return results


def _apply_create_ad_group(client: object, cid: str, changes: dict) -> dict:
    """Create ad group + optional keywords in an existing campaign atomically."""
    service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")
    ad_group_service = client.get_service("AdGroupService")

    operations: list = []

    # 1. AdGroup (temp ID: -1, references existing campaign)
    ag_op = client.get_type("MutateOperation")
    ad_group = ag_op.ad_group_operation.create
    ad_group.resource_name = ad_group_service.ad_group_path(cid, "-1")
    ad_group.name = changes["ad_group_name"]
    ad_group.campaign = campaign_service.campaign_path(cid, changes["campaign_id"])
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if changes.get("cpc_bid_micros"):
        ad_group.cpc_bid_micros = changes["cpc_bid_micros"]
    operations.append(ag_op)

    # 2. Keywords (reference ad_group -1)
    kw_list = changes.get("keywords") or []
    for kw in kw_list:
        kw_op = client.get_type("MutateOperation")
        criterion = kw_op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_service.ad_group_path(cid, "-1")
        criterion.keyword.text = kw["text"]
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"].upper()
        )
        operations.append(kw_op)

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results: dict = {}
    for i, resp in enumerate(response.mutate_operation_responses):
        rn = _extract_resource_name(resp)
        if rn:
            if i == 0:
                results["ad_group"] = rn
            else:
                results.setdefault("keywords", []).append(rn)

    return results


def _apply_update_campaign(client: object, cid: str, changes: dict) -> dict:
    """Update an existing campaign's settings."""
    from google.protobuf import field_mask_pb2

    service = client.get_service("GoogleAdsService")
    campaign_service = client.get_service("CampaignService")
    operations = []
    field_paths = []

    campaign_id = changes["campaign_id"]
    resource_name = campaign_service.campaign_path(cid, campaign_id)

    # Bid strategy and campaign-level setting changes
    bs = changes.get("bidding_strategy")
    search_partners_enabled = changes.get("search_partners_enabled")
    display_network_enabled = changes.get("display_network_enabled")
    if (
        bs
        or search_partners_enabled is not None
        or display_network_enabled is not None
        or changes.get("max_cpc")
    ):
        campaign_op = client.get_type("MutateOperation")
        campaign = campaign_op.campaign_operation.update
        campaign.resource_name = resource_name

        if bs == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions.target_cpa_micros = 0
            if changes.get("target_cpa"):
                campaign.maximize_conversions.target_cpa_micros = int(
                    changes["target_cpa"] * 1_000_000
                )
            field_paths.append("maximize_conversions.target_cpa_micros")
        elif bs == "TARGET_CPA":
            campaign.maximize_conversions.target_cpa_micros = int(
                changes["target_cpa"] * 1_000_000
            )
            field_paths.append("maximize_conversions.target_cpa_micros")
        elif bs == "MAXIMIZE_CONVERSION_VALUE":
            campaign.maximize_conversion_value.target_roas = 0
            if changes.get("target_roas"):
                campaign.maximize_conversion_value.target_roas = changes[
                    "target_roas"
                ]
            field_paths.append("maximize_conversion_value.target_roas")
        elif bs == "TARGET_ROAS":
            campaign.maximize_conversion_value.target_roas = changes["target_roas"]
            field_paths.append("maximize_conversion_value.target_roas")
        elif bs == "TARGET_SPEND":
            campaign.target_spend.target_spend_micros = 0
            field_paths.append("target_spend.target_spend_micros")
        elif bs == "MANUAL_CPC":
            campaign.manual_cpc.enhanced_cpc_enabled = False
            field_paths.append("manual_cpc.enhanced_cpc_enabled")

        if changes.get("max_cpc"):
            campaign.target_spend.cpc_bid_ceiling_micros = int(
                changes["max_cpc"] * 1_000_000
            )
            field_paths.append("target_spend.cpc_bid_ceiling_micros")

        if search_partners_enabled is not None:
            campaign.network_settings.target_search_network = search_partners_enabled
            field_paths.append("network_settings.target_search_network")
        if display_network_enabled is not None:
            campaign.network_settings.target_content_network = display_network_enabled
            field_paths.append("network_settings.target_content_network")

        if field_paths:
            campaign_op.campaign_operation.update_mask.CopyFrom(
                field_mask_pb2.FieldMask(paths=field_paths)
            )
            operations.append(campaign_op)

    # Budget change — requires finding the budget resource name first
    new_budget = changes.get("daily_budget")
    if new_budget:
        budget_query = f"""
            SELECT campaign.campaign_budget
            FROM campaign
            WHERE campaign.id = {campaign_id}
        """
        rows = list(service.search(customer_id=cid, query=budget_query))
        if not rows:
            raise ValueError(f"Campaign {campaign_id} not found")
        budget_rn = rows[0].campaign.campaign_budget

        budget_op = client.get_type("MutateOperation")
        budget = budget_op.campaign_budget_operation.update
        budget.resource_name = budget_rn
        budget.amount_micros = int(new_budget * 1_000_000)
        budget_op.campaign_budget_operation.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=["amount_micros"])
        )
        operations.append(budget_op)

    # Geo targeting — replace POSITIVE location criteria, preserve NEGATIVE
    # location exclusions. The previous implementation filtered on
    # campaign_criterion.type = 'LOCATION' alone, which swept up negative
    # exclusions (e.g. excluding the USA from a worldwide campaign) and
    # silently removed them when the user added or swapped a positive geo.
    # That's a data-safety bug — users wouldn't notice the exclusion was
    # gone until traffic from the excluded region started showing up in
    # reports (issue #32). Restrict the removal scope with
    # ``campaign_criterion.negative = FALSE`` so negatives survive.
    geo_ids = changes.get("geo_target_ids")
    if geo_ids is not None:
        existing_positive_geo = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = 'LOCATION'
              AND campaign_criterion.negative = FALSE
        """
        for row in service.search(customer_id=cid, query=existing_positive_geo):
            rm_op = client.get_type("MutateOperation")
            rm_op.campaign_criterion_operation.remove = (
                row.campaign_criterion.resource_name
            )
            operations.append(rm_op)

        for geo_id in geo_ids:
            add_op = client.get_type("MutateOperation")
            criterion = add_op.campaign_criterion_operation.create
            criterion.campaign = resource_name
            criterion.location.geo_target_constant = (
                f"geoTargetConstants/{geo_id}"
            )
            operations.append(add_op)

    # Language targeting — remove existing, add new
    lang_ids = changes.get("language_ids")
    if lang_ids is not None:
        existing_lang = f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = 'LANGUAGE'
        """
        for row in service.search(customer_id=cid, query=existing_lang):
            rm_op = client.get_type("MutateOperation")
            rm_op.campaign_criterion_operation.remove = (
                row.campaign_criterion.resource_name
            )
            operations.append(rm_op)

        for lang_id in lang_ids:
            add_op = client.get_type("MutateOperation")
            criterion = add_op.campaign_criterion_operation.create
            criterion.campaign = resource_name
            criterion.language.language_constant = (
                f"languageConstants/{lang_id}"
            )
            operations.append(add_op)

    if not operations:
        return {"message": "No changes to apply"}

    response = service.mutate(customer_id=cid, mutate_operations=operations)

    results = {"updated": []}
    for resp in response.mutate_operation_responses:
        rn = _extract_resource_name(resp)
        if rn:
            results["updated"].append(rn)
    return results


def _apply_create_rsa(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("AdGroupAdService")
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create

    ad_group_ad.ad_group = client.get_service("AdGroupService").ad_group_path(
        cid, changes["ad_group_id"]
    )
    # Create as PAUSED for safety — user can enable separately
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED

    ad = ad_group_ad.ad
    ad.final_urls.append(changes["final_url"])

    for entry in changes["headlines"]:
        asset = client.get_type("AdTextAsset")
        asset.text = entry["text"]
        if entry.get("pinned_field"):
            asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[
                entry["pinned_field"]
            ]
        ad.responsive_search_ad.headlines.append(asset)

    for entry in changes["descriptions"]:
        asset = client.get_type("AdTextAsset")
        asset.text = entry["text"]
        if entry.get("pinned_field"):
            asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[
                entry["pinned_field"]
            ]
        ad.responsive_search_ad.descriptions.append(asset)

    if changes.get("path1"):
        ad.responsive_search_ad.path1 = changes["path1"]
    if changes.get("path2"):
        ad.responsive_search_ad.path2 = changes["path2"]

    response = service.mutate_ad_group_ads(
        customer_id=cid, operations=[operation]
    )
    return {"resource_name": response.results[0].resource_name}


def _apply_update_rsa(client: object, cid: str, changes: dict) -> dict:
    """Update mutable fields on an existing RSA in place.

    Builds a sparse ``AdOperation.update`` with only the fields the caller
    asked to change, attached to a FieldMask so Google Ads ignores everything
    else. Mutable in place on RSAs via ``AdService.MutateAds`` (API v23):
    ``final_urls``, ``responsive_search_ad.path1``,
    ``responsive_search_ad.path2``, ``responsive_search_ad.headlines``,
    ``responsive_search_ad.descriptions``. Headlines/descriptions are
    list-replace — the supplied list fully replaces the existing one. Note
    that replacing the creative text resets the ad's learning and re-triggers
    policy review even though the ad keeps its ID (see
    ``update_responsive_search_ad``).
    """
    from google.protobuf import field_mask_pb2

    service = client.get_service("AdService")
    operation = client.get_type("AdOperation")
    ad = operation.update
    ad.resource_name = service.ad_path(cid, changes["ad_id"])

    field_paths: list[str] = []

    if "final_url" in changes:
        ad.final_urls.append(changes["final_url"])
        field_paths.append("final_urls")

    if "path1" in changes:
        ad.responsive_search_ad.path1 = changes["path1"]
        field_paths.append("responsive_search_ad.path1")

    if "path2" in changes:
        ad.responsive_search_ad.path2 = changes["path2"]
        field_paths.append("responsive_search_ad.path2")

    if "headlines" in changes:
        for entry in changes["headlines"]:
            asset = client.get_type("AdTextAsset")
            asset.text = entry["text"]
            if entry.get("pinned_field"):
                asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[
                    entry["pinned_field"]
                ]
            ad.responsive_search_ad.headlines.append(asset)
        field_paths.append("responsive_search_ad.headlines")

    if "descriptions" in changes:
        for entry in changes["descriptions"]:
            asset = client.get_type("AdTextAsset")
            asset.text = entry["text"]
            if entry.get("pinned_field"):
                asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[
                    entry["pinned_field"]
                ]
            ad.responsive_search_ad.descriptions.append(asset)
        field_paths.append("responsive_search_ad.descriptions")

    operation.update_mask = field_mask_pb2.FieldMask(paths=field_paths)
    response = service.mutate_ads(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_add_keywords(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("AdGroupCriterionService")
    ad_group_path = client.get_service("AdGroupService").ad_group_path(
        cid, changes["ad_group_id"]
    )

    operations = []
    for kw in changes["keywords"]:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = ad_group_path
        criterion.keyword.text = kw["text"]
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, kw["match_type"].upper()
        )
        operations.append(operation)

    response = service.mutate_ad_group_criteria(
        customer_id=cid, operations=operations
    )
    return {"resource_names": [r.resource_name for r in response.results]}


def _apply_add_negative_keywords(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("CampaignCriterionService")
    campaign_path = client.get_service("CampaignService").campaign_path(
        cid, changes["campaign_id"]
    )

    operations = []
    for kw_text in changes["keywords"]:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.negative = True
        criterion.keyword.text = kw_text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, changes["match_type"]
        )
        operations.append(operation)

    response = service.mutate_campaign_criteria(
        customer_id=cid, operations=operations
    )
    return {"resource_names": [r.resource_name for r in response.results]}


def _apply_add_negative_locations(client: object, cid: str, changes: dict) -> dict:
    service = client.get_service("CampaignCriterionService")
    campaign_path = client.get_service("CampaignService").campaign_path(
        cid, changes["campaign_id"]
    )

    operations = []
    for geo_id in changes["geo_target_ids"]:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path
        criterion.negative = True
        criterion.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
        operations.append(operation)

    response = service.mutate_campaign_criteria(
        customer_id=cid, operations=operations
    )
    return {"resource_names": [r.resource_name for r in response.results]}

def _apply_add_demographic_criteria(
    client: object, cid: str, changes: dict
) -> dict:
    """Create AGE_RANGE/GENDER/PARENTAL_STATUS/INCOME_RANGE criteria.

    Creates ad_group_criterion entries when `ad_group_id` is set, otherwise
    campaign_criterion entries when `campaign_id` is set.
    """
    ad_group_id = changes.get("ad_group_id") or ""
    campaign_id = changes.get("campaign_id") or ""
    negative = bool(changes.get("negative", True))

    if ad_group_id:
        service = client.get_service("AdGroupCriterionService")
        ad_group_path = client.get_service("AdGroupService").ad_group_path(
            cid, ad_group_id
        )

        def make_criterion():
            op = client.get_type("AdGroupCriterionOperation")
            criterion = op.create
            criterion.ad_group = ad_group_path
            criterion.negative = negative
            return op, criterion

        operations = _build_demographic_criteria(client, changes, make_criterion)
        response = service.mutate_ad_group_criteria(
            customer_id=cid, operations=operations
        )
        return {"resource_names": [r.resource_name for r in response.results]}

    if campaign_id:
        service = client.get_service("CampaignCriterionService")
        campaign_path = client.get_service("CampaignService").campaign_path(
            cid, campaign_id
        )

        def make_criterion():
            op = client.get_type("CampaignCriterionOperation")
            criterion = op.create
            criterion.campaign = campaign_path
            criterion.negative = negative
            return op, criterion

        operations = _build_demographic_criteria(client, changes, make_criterion)
        response = service.mutate_campaign_criteria(
            customer_id=cid, operations=operations
        )
        return {"resource_names": [r.resource_name for r in response.results]}

    raise ValueError("Either ad_group_id or campaign_id must be set")


def _build_demographic_criteria(
    client: object,
    changes: dict,
    make_criterion: object,
) -> list:
    """Build the list of criterion operations from the four demographic dimensions.

    `make_criterion` is a zero-arg callable that returns a fresh
    (operation, criterion) pair pre-populated with the parent (ad_group or
    campaign) and negative flag.
    """
    operations = []

    for value in changes.get("age_ranges") or []:
        op, criterion = make_criterion()
        criterion.age_range.type_ = getattr(
            client.enums.AgeRangeTypeEnum, value
        )
        operations.append(op)

    for value in changes.get("genders") or []:
        op, criterion = make_criterion()
        criterion.gender.type_ = getattr(client.enums.GenderTypeEnum, value)
        operations.append(op)

    for value in changes.get("parental_statuses") or []:
        op, criterion = make_criterion()
        criterion.parental_status.type_ = getattr(
            client.enums.ParentalStatusTypeEnum, value
        )
        operations.append(op)

    for value in changes.get("income_ranges") or []:
        op, criterion = make_criterion()
        criterion.income_range.type_ = getattr(
            client.enums.IncomeRangeTypeEnum, value
        )
        operations.append(op)

    return operations


def _resolve_ad_entity_id(client: object, cid: str, entity_id: str) -> str:
    """Ensure ad entity_id is in 'adGroupId~adId' composite format.

    If only a bare ad ID is given, queries the API to find the ad group.
    """
    if "~" in entity_id:
        return entity_id

    ga_service = client.get_service("GoogleAdsService")
    query = (
        f"SELECT ad_group.id, ad_group_ad.ad.id "
        f"FROM ad_group_ad "
        f"WHERE ad_group_ad.ad.id = {entity_id} "
        f"LIMIT 1"
    )
    response = ga_service.search(customer_id=cid, query=query)
    for row in response:
        ag_id = row.ad_group.id
        return f"{ag_id}~{entity_id}"

    raise ValueError(
        f"Ad ID {entity_id} not found. Pass the composite ID as "
        f"'adGroupId~adId' (e.g. '12345678~{entity_id}')."
    )


def _apply_remove(
    client: object,
    cid: str,
    entity_type: str,
    entity_id: str,
) -> dict:
    """Remove an entity via the REMOVE mutate operation (irreversible)."""
    if entity_type == "campaign":
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        operation.remove = service.campaign_path(cid, entity_id)
        response = service.mutate_campaigns(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "ad_group":
        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        operation.remove = service.ad_group_path(cid, entity_id)
        response = service.mutate_ad_groups(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "ad":
        resolved_id = _resolve_ad_entity_id(client, cid, entity_id)
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        operation.remove = f"customers/{cid}/adGroupAds/{resolved_id}"
        response = service.mutate_ad_group_ads(
            customer_id=cid, operations=[operation]
        )

    elif entity_type in ("keyword", "ad_group_criterion"):
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        operation.remove = f"customers/{cid}/adGroupCriteria/{entity_id}"
        response = service.mutate_ad_group_criteria(
            customer_id=cid, operations=[operation]
        )

    elif entity_type in ("negative_keyword", "campaign_criterion"):
        service = client.get_service("CampaignCriterionService")
        operation = client.get_type("CampaignCriterionOperation")
        operation.remove = f"customers/{cid}/campaignCriteria/{entity_id}"
        response = service.mutate_campaign_criteria(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "shared_criterion":
        if "~" not in entity_id:
            raise ValueError(
                f"shared_criterion entity_id must be "
                f"'sharedSetId~criterionId', got '{entity_id}'"
            )
        service = client.get_service("SharedCriterionService")
        operation = client.get_type("SharedCriterionOperation")
        operation.remove = f"customers/{cid}/sharedCriteria/{entity_id}"
        response = service.mutate_shared_criteria(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "campaign_asset":
        parts = entity_id.split("~")
        if len(parts) != 3:
            raise ValueError(
                f"campaign_asset entity_id must be "
                f"'campaignId~assetId~fieldType', got '{entity_id}'"
            )
        resource_name = f"customers/{cid}/campaignAssets/{entity_id}"
        ga_service = client.get_service("GoogleAdsService")
        op = client.get_type("MutateOperation")
        op.campaign_asset_operation.remove = resource_name
        response = ga_service.mutate(
            customer_id=cid, mutate_operations=[op]
        )
        resp_inner = response.mutate_operation_responses[0]
        if resp_inner.campaign_asset_result.resource_name:
            return {"resource_name": resp_inner.campaign_asset_result.resource_name}
        return {"resource_name": resource_name, "status": "removed"}

    elif entity_type == "asset":
        service = client.get_service("AssetService")
        operation = client.get_type("AssetOperation")
        operation.remove = service.asset_path(cid, entity_id)
        response = service.mutate_assets(
            customer_id=cid, operations=[operation]
        )

    elif entity_type == "customer_asset":
        parts = entity_id.split("~")
        if len(parts) != 2:
            raise ValueError(
                f"customer_asset entity_id must be "
                f"'assetId~fieldType', got '{entity_id}'"
            )
        resource_name = f"customers/{cid}/customerAssets/{entity_id}"
        ga_service = client.get_service("GoogleAdsService")
        op = client.get_type("MutateOperation")
        op.customer_asset_operation.remove = resource_name
        response = ga_service.mutate(
            customer_id=cid, mutate_operations=[op]
        )
        resp_inner = response.mutate_operation_responses[0]
        if resp_inner.customer_asset_result.resource_name:
            return {"resource_name": resp_inner.customer_asset_result.resource_name}
        return {"resource_name": resource_name, "status": "removed"}

    else:
        raise ValueError(f"Cannot remove entity_type: {entity_type}")

    return {"resource_name": response.results[0].resource_name}


def _apply_status_change(
    client: object,
    cid: str,
    entity_type: str,
    entity_id: str,
    status: str,
) -> dict:
    """Update the status of a campaign, ad group, ad, or keyword."""
    if entity_type == "campaign":
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        entity = operation.update
        entity.resource_name = service.campaign_path(cid, entity_id)
        entity.status = getattr(client.enums.CampaignStatusEnum, status)
        mutate = service.mutate_campaigns

    elif entity_type == "ad_group":
        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        entity = operation.update
        entity.resource_name = service.ad_group_path(cid, entity_id)
        entity.status = getattr(client.enums.AdGroupStatusEnum, status)
        mutate = service.mutate_ad_groups

    elif entity_type == "ad":
        resolved_id = _resolve_ad_entity_id(client, cid, entity_id)
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        entity = operation.update
        entity.resource_name = f"customers/{cid}/adGroupAds/{resolved_id}"
        entity.status = getattr(client.enums.AdGroupAdStatusEnum, status)
        mutate = service.mutate_ad_group_ads

    elif entity_type == "keyword":
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        entity = operation.update
        entity.resource_name = f"customers/{cid}/adGroupCriteria/{entity_id}"
        entity.status = getattr(
            client.enums.AdGroupCriterionStatusEnum, status
        )
        mutate = service.mutate_ad_group_criteria

    else:
        raise ValueError(f"Unknown entity_type: {entity_type}")

    # Build field mask for the status field only
    from google.protobuf import field_mask_pb2

    operation.update_mask = field_mask_pb2.FieldMask(paths=["status"])

    response = mutate(customer_id=cid, operations=[operation])
    return {"resource_name": response.results[0].resource_name}


def _apply_campaign_assets(
    client: object,
    cid: str,
    campaign_id: str,
    assets: list[dict],
    field_type: object,
    populate_asset: object,
) -> dict:
    """Create assets and link them to a campaign via CampaignAsset."""
    asset_service = client.get_service("AssetService")
    googleads_service = client.get_service("GoogleAdsService")
    operations = []

    for i, payload in enumerate(assets):
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = asset_service.asset_path(cid, str(-(i + 1)))
        populate_asset(asset, payload)
        operations.append(op)

    for i in range(len(assets)):
        op = client.get_type("MutateOperation")
        ca = op.campaign_asset_operation.create
        ca.asset = asset_service.asset_path(cid, str(-(i + 1)))
        ca.campaign = googleads_service.campaign_path(cid, campaign_id)
        ca.field_type = field_type
        operations.append(op)

    response = googleads_service.mutate(
        customer_id=cid, mutate_operations=operations
    )

    results = {"assets": [], "campaign_assets": []}
    num_assets = len(assets)
    for i, resp in enumerate(response.mutate_operation_responses):
        resource = None
        if resp.asset_result.resource_name:
            resource = resp.asset_result.resource_name
        elif resp.campaign_asset_result.resource_name:
            resource = resp.campaign_asset_result.resource_name

        if resource:
            if i < num_assets:
                results["assets"].append(resource)
            else:
                results["campaign_assets"].append(resource)

    return results


def _apply_create_callouts(client: object, cid: str, changes: dict) -> dict:
    """Create callout assets and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        asset.callout_asset.callout_text = payload["callout_text"]

    assets = [{"callout_text": text} for text in changes["callouts"]]
    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        assets,
        client.enums.AssetFieldTypeEnum.CALLOUT,
        populate,
    )


def _apply_create_structured_snippets(
    client: object, cid: str, changes: dict
) -> dict:
    """Create structured snippet assets and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        asset.structured_snippet_asset.header = payload["header"]
        asset.structured_snippet_asset.values.extend(payload["values"])

    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        changes["snippets"],
        client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET,
        populate,
    )


def _apply_create_image_assets(client: object, cid: str, changes: dict) -> dict:
    """Create image assets from local files or URLs and link them to a campaign.

    URL images are re-fetched through the same safe fetcher and must match
    the sha256 approved in the preview.
    """

    from adloop.runtime import deployment_mode

    def populate(asset: object, payload: dict) -> None:
        if payload.get("url"):
            image_bytes = _fetch_approved_image_url(payload)
            name_source = _url_path(str(payload["url"]))
        elif deployment_mode() == "server":
            # Drafts refuse paths on a server; never read server files even
            # if a path-based plan reached apply some other way.
            raise ValueError(
                "local file paths aren't available on a hosted server; "
                "pass image_urls"
            )
        else:
            name_source = Path(str(payload["path"]))
            image_bytes = name_source.read_bytes()
        mime_type_name = _VALID_IMAGE_MIME_TYPES[str(payload["mime_type"])]
        asset.name = str(payload.get("name") or _build_image_asset_name(name_source, image_bytes))
        asset.type_ = client.enums.AssetTypeEnum.IMAGE
        asset.image_asset.data = image_bytes
        asset.image_asset.mime_type = getattr(client.enums.MimeTypeEnum, mime_type_name)
        asset.image_asset.full_size.width_pixels = int(payload["width"])
        asset.image_asset.full_size.height_pixels = int(payload["height"])

    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        changes["images"],
        client.enums.AssetFieldTypeEnum.AD_IMAGE,
        populate,
    )


def _apply_create_sitelinks(client: object, cid: str, changes: dict) -> dict:
    """Create sitelink assets and link them to a campaign."""

    def populate(asset: object, payload: dict) -> None:
        asset.sitelink_asset.link_text = payload["link_text"]
        asset.final_urls.append(payload["final_url"])
        if payload.get("description1"):
            asset.sitelink_asset.description1 = payload["description1"]
        if payload.get("description2"):
            asset.sitelink_asset.description2 = payload["description2"]

    return _apply_campaign_assets(
        client,
        cid,
        changes["campaign_id"],
        changes["sitelinks"],
        client.enums.AssetFieldTypeEnum.SITELINK,
        populate,
    )


def _apply_create_negative_keyword_list(
    client: object, cid: str, changes: dict
) -> dict:
    """Create a shared negative keyword list and attach it to a campaign.

    Executes three sequential API calls. If any step fails, the result
    includes partial_failure info with the SharedSet resource name (if
    created) so the caller can clean up or retry the remaining steps.
    """
    # 1. Create the SharedSet
    try:
        shared_set_service = client.get_service("SharedSetService")
        ss_op = client.get_type("SharedSetOperation")
        shared_set = ss_op.create
        shared_set.name = changes["list_name"]
        shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
        ss_response = shared_set_service.mutate_shared_sets(
            customer_id=cid, operations=[ss_op]
        )
        shared_set_resource = ss_response.results[0].resource_name
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": None,
            "completed_steps": [],
            "failed_step": "create_shared_set",
            "error": _extract_error_message(exc),
        }

    # 2. Add keywords to the list
    try:
        sc_service = client.get_service("SharedCriterionService")
        sc_ops = []
        for kw_text in changes["keywords"]:
            sc_op = client.get_type("SharedCriterionOperation")
            criterion = sc_op.create
            criterion.shared_set = shared_set_resource
            criterion.keyword.text = kw_text
            criterion.keyword.match_type = getattr(
                client.enums.KeywordMatchTypeEnum, changes["match_type"]
            )
            sc_ops.append(sc_op)
        sc_service.mutate_shared_criteria(customer_id=cid, operations=sc_ops)
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": shared_set_resource,
            "completed_steps": ["create_shared_set"],
            "failed_step": "add_keywords",
            "error": _extract_error_message(exc),
        }

    # 3. Attach the list to the campaign
    try:
        css_service = client.get_service("CampaignSharedSetService")
        css_op = client.get_type("CampaignSharedSetOperation")
        campaign_shared_set = css_op.create
        campaign_shared_set.campaign = client.get_service(
            "CampaignService"
        ).campaign_path(cid, changes["campaign_id"])
        campaign_shared_set.shared_set = shared_set_resource
        css_response = css_service.mutate_campaign_shared_sets(
            customer_id=cid, operations=[css_op]
        )
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": shared_set_resource,
            "keyword_count": len(changes["keywords"]),
            "completed_steps": ["create_shared_set", "add_keywords"],
            "failed_step": "attach_to_campaign",
            "error": _extract_error_message(exc),
        }

    return {
        "shared_set_resource": shared_set_resource,
        "campaign_shared_set_resource": css_response.results[0].resource_name,
        "keyword_count": len(changes["keywords"]),
    }


def _apply_add_to_negative_keyword_list(
    client: object, cid: str, changes: dict
) -> dict:
    """Append keywords to an existing shared negative keyword list."""
    shared_set_service = client.get_service("SharedSetService")
    shared_set_resource = shared_set_service.shared_set_path(
        cid, changes["shared_set_id"]
    )

    sc_service = client.get_service("SharedCriterionService")
    operations = []
    for kw_text in changes["keywords"]:
        op = client.get_type("SharedCriterionOperation")
        criterion = op.create
        criterion.shared_set = shared_set_resource
        criterion.keyword.text = kw_text
        criterion.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, changes["match_type"]
        )
        operations.append(op)

    response = sc_service.mutate_shared_criteria(
        customer_id=cid, operations=operations
    )
    return {
        "shared_set_resource": shared_set_resource,
        "resource_names": [r.resource_name for r in response.results],
        "keyword_count": len(response.results),
    }


def _parse_partial_failure_per_op(
    client: object, partial_failure_error: object
) -> dict:
    """Parse a Google Ads partial_failure_error proto into {op_index: message}.

    Returns an empty dict when there are no failures, when ``details`` is
    missing, or when proto deserialization fails for any reason — callers
    can still detect failed operations via empty ``result.resource_name``
    entries and surface ``partial_failure_error.message`` as a fallback.
    """
    if partial_failure_error is None:
        return {}
    if not getattr(partial_failure_error, "code", 0):
        return {}

    details = getattr(partial_failure_error, "details", None) or []
    out: dict = {}
    try:
        failure_msg = client.get_type("GoogleAdsFailure")
        failure_pb_cls = type(failure_msg).pb(failure_msg).__class__
        for detail in details:
            value = getattr(detail, "value", None)
            if value is None:
                continue
            failure = failure_pb_cls.FromString(value)
            for err in failure.errors:
                fpe = getattr(err.location, "field_path_elements", None)
                if fpe:
                    out[fpe[0].index] = err.message
    except Exception:
        pass
    return out


def _apply_attach_shared_set_to_campaigns(
    client: object, cid: str, changes: dict
) -> dict:
    """Create CampaignSharedSet linkages for one shared set across campaigns.

    Uses ``partial_failure=True`` so per-operation errors (e.g. attempting
    to attach a list to a campaign that already has it) don't fail the
    whole batch. Successful linkages are returned in ``resource_names``;
    failed linkages are surfaced under ``failed_campaigns`` with the
    originating campaign_id and per-op error message when parseable.
    """
    css_service = client.get_service("CampaignSharedSetService")
    campaign_service = client.get_service("CampaignService")
    shared_set_service = client.get_service("SharedSetService")

    shared_set_resource = shared_set_service.shared_set_path(
        cid, changes["shared_set_id"]
    )

    campaign_ids = list(changes["campaign_ids"])
    operations = []
    for campaign_id in campaign_ids:
        op = client.get_type("CampaignSharedSetOperation")
        css = op.create
        css.campaign = campaign_service.campaign_path(cid, campaign_id)
        css.shared_set = shared_set_resource
        operations.append(op)

    # CampaignSharedSetService.mutate_campaign_shared_sets does NOT accept a
    # flattened ``partial_failure`` kwarg (unlike GoogleAdsService.mutate);
    # it must be set on the request object.
    request = client.get_type("MutateCampaignSharedSetsRequest")
    request.customer_id = cid
    request.operations.extend(operations)
    request.partial_failure = True
    response = css_service.mutate_campaign_shared_sets(request=request)

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    succeeded: list = []
    failed: list = []
    for idx, result in enumerate(response.results):
        if result.resource_name:
            succeeded.append(result.resource_name)
        else:
            failed.append(
                {
                    "campaign_id": str(campaign_ids[idx]),
                    "operation_index": idx,
                    "error": per_op_errors.get(
                        idx, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out = {
        "shared_set_resource": shared_set_resource,
        "resource_names": succeeded,
        "campaign_count": len(succeeded),
    }
    if failed:
        out["partial_failure"] = True
        out["failed_campaigns"] = failed
        if pf_error is not None:
            msg = getattr(pf_error, "message", "")
            if msg:
                out["partial_failure_message"] = msg
    return out


def _apply_detach_shared_set_from_campaigns(
    client: object, cid: str, changes: dict
) -> dict:
    """Remove CampaignSharedSet linkages for one shared set across campaigns.

    The CampaignSharedSet resource name has a composite ID of the form
    ``{campaign_id}~{shared_set_id}`` so we can construct the resource path
    directly without needing to look up the linkage first.

    Uses ``partial_failure=True`` so per-operation errors (e.g. detaching
    a non-existent linkage) don't fail the whole batch. Successful removals
    are returned in ``removed_resource_names``; failed operations are
    surfaced under ``failed_campaigns`` with the originating campaign_id
    and per-op error message when parseable.
    """
    css_service = client.get_service("CampaignSharedSetService")

    shared_set_id = changes["shared_set_id"]
    campaign_ids = list(changes["campaign_ids"])
    operations = []
    for campaign_id in campaign_ids:
        op = client.get_type("CampaignSharedSetOperation")
        op.remove = (
            f"customers/{cid}/campaignSharedSets/{campaign_id}~{shared_set_id}"
        )
        operations.append(op)

    # partial_failure must be set on the request object — the flattened
    # kwarg is not supported by CampaignSharedSetService (see attach above).
    request = client.get_type("MutateCampaignSharedSetsRequest")
    request.customer_id = cid
    request.operations.extend(operations)
    request.partial_failure = True
    response = css_service.mutate_campaign_shared_sets(request=request)

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    succeeded: list = []
    failed: list = []
    for idx, result in enumerate(response.results):
        if result.resource_name:
            succeeded.append(result.resource_name)
        else:
            failed.append(
                {
                    "campaign_id": str(campaign_ids[idx]),
                    "operation_index": idx,
                    "error": per_op_errors.get(
                        idx, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out = {
        "shared_set_id": shared_set_id,
        "removed_resource_names": succeeded,
        "campaign_count": len(succeeded),
    }
    if failed:
        out["partial_failure"] = True
        out["failed_campaigns"] = failed
        if pf_error is not None:
            msg = getattr(pf_error, "message", "")
            if msg:
                out["partial_failure_message"] = msg
    return out


def _brand_list_criterion_operations(
    client: object, shared_set_resource: str, brand_ids: list[str]
) -> list:
    """Build SharedCriterionOperation creates from brand entity IDs."""
    operations = []
    for brand_id in brand_ids:
        op = client.get_type("SharedCriterionOperation")
        criterion = op.create
        criterion.shared_set = shared_set_resource
        # entity_id is the writable half of BrandInfo — display_name,
        # primary_url and status come back output-only from the API.
        criterion.brand.entity_id = str(brand_id)
        operations.append(op)
    return operations

def _attach_brand_list_resource(
    client: object,
    cid: str,
    shared_set_resource: str,
    campaign_ids: list[str],
    negative: bool,
) -> dict:
    """Create CampaignCriterion.brand_list attachments for one brand list.

    Brand lists are criteria, not CampaignSharedSet linkages — the criterion's
    ``negative`` flag decides whether the list excludes (True) or narrows
    targeting (False). ``partial_failure`` lets the remaining campaigns succeed
    when one of them does not support brand lists.
    """
    service = client.get_service("CampaignCriterionService")
    campaign_service = client.get_service("CampaignService")

    operations = []
    for campaign_id in campaign_ids:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_service.campaign_path(cid, campaign_id)
        criterion.brand_list.shared_set = shared_set_resource
        criterion.negative = bool(negative)
        operations.append(op)

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = cid
    request.operations.extend(operations)
    request.partial_failure = True
    response = service.mutate_campaign_criteria(request=request)

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    succeeded: list = []
    failed: list = []
    for idx, result in enumerate(response.results):
        if result.resource_name:
            succeeded.append(result.resource_name)
        else:
            failed.append(
                {
                    "campaign_id": str(campaign_ids[idx]),
                    "operation_index": idx,
                    "error": per_op_errors.get(
                        idx, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out = {
        "shared_set_resource": shared_set_resource,
        "negative": bool(negative),
        "resource_names": succeeded,
        "campaign_count": len(succeeded),
    }
    if failed:
        out["partial_failure"] = True
        out["failed_campaigns"] = failed
        if pf_error is not None:
            msg = getattr(pf_error, "message", "")
            if msg:
                out["partial_failure_message"] = msg
    return out

def _apply_create_brand_list(client: object, cid: str, changes: dict) -> dict:
    """Create a BRANDS shared set and fill it — one atomic mutate.

    The set is created under the temporary resource name
    ``customers/{cid}/sharedSets/-1`` and the brand criteria reference that same
    name; Google replaces the negative ID inside the single request, so either
    the complete list exists or nothing does. The two-call version this replaces
    could leave an empty list behind when one brand ID was rejected — and it
    could not be dry-run at all, because the second call referenced a shared set
    that did not exist yet.

    Attaching to campaigns is a different entity type (CampaignCriterion) and
    therefore stays a second call; when it fails the list itself is reported as
    created rather than pretending nothing happened.
    """
    google_ads_service = client.get_service("GoogleAdsService")
    shared_set_resource = google_ads_service.shared_set_path(cid, "-1")

    ss_op = client.get_type("SharedSetOperation")
    ss_op.create.name = changes["list_name"]
    ss_op.create.type_ = client.enums.SharedSetTypeEnum.BRANDS

    # Order matters: the criteria can only reference the temporary name after
    # the operation that defines it, and no partial_failure here — this request
    # is meant to be all-or-nothing.
    create_set = client.get_type("MutateOperation")
    create_set.shared_set_operation = ss_op
    operations = [create_set]
    for criterion_op in _brand_list_criterion_operations(
        client, shared_set_resource, changes["brand_ids"]
    ):
        wrapper = client.get_type("MutateOperation")
        wrapper.shared_criterion_operation = criterion_op
        operations.append(wrapper)

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend(operations)
    try:
        response = google_ads_service.mutate(request=request)
    except Exception as exc:
        return {
            "partial_failure": True,
            "shared_set_resource": None,
            "completed_steps": [],
            "failed_step": "create_brand_list",
            "error": _extract_error_message(exc),
        }

    responses = list(response.mutate_operation_responses)
    created_resource = responses[0].shared_set_result.resource_name if responses else ""
    criterion_resource_names = [
        entry.shared_criterion_result.resource_name
        for entry in responses[1:]
        if entry.shared_criterion_result.resource_name
    ]

    out = {
        "shared_set_resource": created_resource,
        "brand_count": len(criterion_resource_names),
        "criterion_resource_names": criterion_resource_names,
    }

    campaign_ids = list(changes.get("campaign_ids") or [])
    if campaign_ids:
        attachment = _attach_brand_list_resource(
            client,
            cid,
            created_resource,
            campaign_ids,
            bool(changes.get("negative", True)),
        )
        out["attachment"] = attachment
        if attachment.get("partial_failure"):
            out["partial_failure"] = True
            out["completed_steps"] = ["create_brand_list"]
            out["failed_step"] = "attach_to_campaigns"
    return out

def _apply_add_to_brand_list(client: object, cid: str, changes: dict) -> dict:
    """Append brands to an existing brand list."""
    shared_set_service = client.get_service("SharedSetService")
    shared_set_resource = shared_set_service.shared_set_path(
        cid, changes["shared_set_id"]
    )

    sc_service = client.get_service("SharedCriterionService")
    operations = _brand_list_criterion_operations(
        client, shared_set_resource, changes["brand_ids"]
    )
    response = sc_service.mutate_shared_criteria(
        customer_id=cid, operations=operations
    )
    return {
        "shared_set_resource": shared_set_resource,
        "resource_names": [r.resource_name for r in response.results],
        "brand_count": len(response.results),
    }

def _apply_remove_from_brand_list(client: object, cid: str, changes: dict) -> dict:
    """Remove brands (SharedCriteria) from a brand list.

    SharedCriterion has no status field, so this is a real removal — the
    composite resource name is ``{shared_set_id}~{criterion_id}``.
    """
    shared_set_id = changes["shared_set_id"]
    criterion_service = client.get_service("SharedCriterionService")

    operations = []
    for criterion_id in changes["criterion_ids"]:
        op = client.get_type("SharedCriterionOperation")
        op.remove = (
            f"customers/{cid}/sharedCriteria/{shared_set_id}~{criterion_id}"
        )
        operations.append(op)

    request = client.get_type("MutateSharedCriteriaRequest")
    request.customer_id = cid
    request.operations.extend(operations)
    request.partial_failure = True
    response = criterion_service.mutate_shared_criteria(request=request)

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    removed: list = []
    failed: list = []
    for idx, result in enumerate(response.results):
        if result.resource_name:
            removed.append(result.resource_name)
        else:
            failed.append(
                {
                    "criterion_id": str(changes["criterion_ids"][idx]),
                    "operation_index": idx,
                    "error": per_op_errors.get(
                        idx, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out = {
        "shared_set_id": shared_set_id,
        "removed_resource_names": removed,
        "removed_count": len(removed),
    }
    if failed:
        out["partial_failure"] = True
        out["failed_criteria"] = failed
        if pf_error is not None:
            msg = getattr(pf_error, "message", "")
            if msg:
                out["partial_failure_message"] = msg
    return out

def _apply_attach_brand_list_to_campaigns(
    client: object, cid: str, changes: dict
) -> dict:
    """Attach an existing brand list to campaigns as CampaignCriterion rows."""
    shared_set_service = client.get_service("SharedSetService")
    shared_set_resource = shared_set_service.shared_set_path(
        cid, changes["shared_set_id"]
    )
    return _attach_brand_list_resource(
        client,
        cid,
        shared_set_resource,
        list(changes["campaign_ids"]),
        bool(changes.get("negative", True)),
    )

def _apply_detach_brand_list_from_campaigns(
    client: object, cid: str, changes: dict
) -> dict:
    """Remove brand-list criteria from campaigns.

    Unlike CampaignSharedSet the criterion id cannot be derived from the
    shared set id, so the current attachments are read first and the matching
    rows removed. Campaigns without this list end up in ``not_attached``.
    """
    shared_set_id = changes["shared_set_id"]
    wanted = [str(c) for c in changes["campaign_ids"]]

    ads_service = client.get_service("GoogleAdsService")
    query = """
        SELECT campaign.id, campaign_criterion.criterion_id,
               campaign_criterion.brand_list.shared_set
        FROM campaign_criterion
        WHERE campaign_criterion.type = 'BRAND_LIST'
          AND campaign_criterion.status != 'REMOVED'
    """
    rows = list(ads_service.search(customer_id=cid, query=query))

    suffix = f"/sharedSets/{shared_set_id}"
    matches: list[tuple[str, str]] = []
    for row in rows:
        campaign_id = str(row.campaign.id)
        if campaign_id not in wanted:
            continue
        if not str(row.campaign_criterion.brand_list.shared_set).endswith(suffix):
            continue
        matches.append((campaign_id, str(row.campaign_criterion.criterion_id)))

    not_attached = sorted(set(wanted) - {campaign_id for campaign_id, _ in matches})
    if not matches:
        return {
            "shared_set_id": shared_set_id,
            "removed_resource_names": [],
            "removed_count": 0,
            "not_attached": not_attached,
        }

    criterion_service = client.get_service("CampaignCriterionService")
    operations = []
    for campaign_id, criterion_id in matches:
        op = client.get_type("CampaignCriterionOperation")
        op.remove = (
            f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        )
        operations.append(op)

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = cid
    request.operations.extend(operations)
    request.partial_failure = True
    response = criterion_service.mutate_campaign_criteria(request=request)

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    removed: list = []
    failed: list = []
    for idx, result in enumerate(response.results):
        if result.resource_name:
            removed.append(result.resource_name)
        else:
            failed.append(
                {
                    "campaign_id": matches[idx][0],
                    "criterion_id": matches[idx][1],
                    "operation_index": idx,
                    "error": per_op_errors.get(
                        idx, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out = {
        "shared_set_id": shared_set_id,
        "removed_resource_names": removed,
        "removed_count": len(removed),
        "not_attached": not_attached,
    }
    if failed:
        out["partial_failure"] = True
        out["failed_criteria"] = failed
        if pf_error is not None:
            msg = getattr(pf_error, "message", "")
            if msg:
                out["partial_failure_message"] = msg
    return out

def _rollback_ad_group_settings(
    client: object, cid: str, changes: dict, ad_group_ids: list[str]
) -> dict:
    """Restore ad groups to the values they had before this plan ran."""
    from adloop.ads import ai_max as ai

    by_id = {
        group["ad_group_id"]: group for group in changes.get("ad_groups") or []
    }
    targets = [by_id[gid] for gid in ad_group_ids if gid in by_id]
    if not targets:
        return {"attempted": False, "restored": [], "failed": []}

    restored: list[str] = []
    failed: list[dict] = []
    for previous in sorted({bool(t.get("before")) for t in targets}):
        subset = [t for t in targets if bool(t.get("before")) == previous]
        try:
            outcome = ai.mutate_ad_group_search_term_matching(
                client, cid, subset, previous
            )
        except Exception as exc:  # noqa: BLE001 — rollback must never mask the original error
            failed.extend(
                {"ad_group_id": t["ad_group_id"], "error": _extract_error_message(exc)}
                for t in subset
            )
            continue
        restored.extend(outcome.get("succeeded") or [])
        failed.extend(outcome.get("failed") or [])

    return {
        "attempted": True,
        "restored": restored,
        "restored_value": targets[0].get("before"),
        "failed": failed,
    }

def _apply_ai_max_settings(client: object, cid: str, changes: dict) -> dict:
    """Apply AI Max controls — ad groups first, campaign second, never reversed.

    The state this must not leave behind is "AI Max on while search term
    matching is still enabled". Disabling matching changes nothing while AI Max
    is off, so doing it first closes that window entirely: if an ad group
    fails, the campaign step is not attempted, and if the campaign step fails,
    the ad groups are put back to their previous values.
    """
    from adloop.ads import ai_max as ai

    campaign_id = changes["campaign_id"]
    result: dict = {
        "campaign_id": campaign_id,
        "completed_steps": [],
    }
    if changes.get("warnings"):
        result["warnings"] = list(changes["warnings"])

    target_matching = changes.get("disable_search_term_matching")
    ad_groups = list(changes.get("ad_groups") or [])

    if target_matching is not None and ad_groups:
        group_outcome = ai.mutate_ad_group_search_term_matching(
            client, cid, ad_groups, bool(target_matching)
        )
        result["ad_group_mutation"] = group_outcome
        if group_outcome.get("failed"):
            result["failed_step"] = "ad_groups"
            result["partial_failure"] = True
            result["message"] = (
                "Search term matching could not be disabled on every selected "
                "ad group, so the campaign-level change was NOT made. AI Max "
                "stays as it was."
            )
            result["rollback"] = _rollback_ad_group_settings(
                client, cid, changes, group_outcome.get("succeeded") or []
            )
            result["readback"] = ai.read_ai_max_state(
                client, cid, campaign_id=campaign_id
            )
            return result
        result["completed_steps"].append("ad_groups")
    elif target_matching is not None:
        result["ad_group_mutation"] = {
            "succeeded": [],
            "failed": [],
            "count": 0,
            "note": "no non-removed ad groups matched",
        }

    try:
        campaign_outcome = ai.mutate_campaign_ai_max(
            client,
            cid,
            {
                "campaign_id": campaign_id,
                "enable_ai_max": changes.get("enable_ai_max"),
                "asset_automation_settings": (
                    changes.get("_asset_automation_settings_full")
                    if changes.get("asset_automation_changed")
                    else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 — must roll back before surfacing
        result["failed_step"] = "campaign"
        result["partial_failure"] = True
        result["error"] = _extract_error_message(exc)
        if "ad_groups" in result["completed_steps"]:
            result["rollback"] = _rollback_ad_group_settings(
                client, cid, changes, [group["ad_group_id"] for group in ad_groups]
            )
            result["message"] = (
                "The campaign-level change failed, so the ad group changes were "
                "rolled back. Nothing about AI Max or search term matching "
                "should have changed — verify with the readback below."
            )
        result["readback"] = ai.read_ai_max_state(
            client, cid, campaign_id=campaign_id
        )
        return result
    result["campaign_mutation"] = campaign_outcome
    if campaign_outcome.get("attempted"):
        result["completed_steps"].append("campaign")

    result["readback"] = ai.read_ai_max_state(client, cid, campaign_id=campaign_id)
    return result
