"""Microsoft Advertising REST client: headers, faults, retries, reports.

Every call goes through :func:`ms_request`:

- **Headers**: ``Authorization: Bearer``, ``DeveloperToken``, ``CustomerId``
  (manager account) and ``CustomerAccountId`` (ad account). Bodies and
  responses are JSON with PascalCase names; flag enums such as
  ``CampaignType`` travel as comma-separated strings ("Search, Shopping").
- **Faults**: Microsoft returns ``AdApiFaultDetail`` (``Errors``) or
  ``ApiFaultDetail`` (``OperationErrors`` / ``BatchErrors``) bodies; the
  codes and messages are folded into one :class:`MicrosoftAdsApiError`.
  An expired token (codes 105/109 or HTTP 401) is refreshed once.
- **Throttling**: HTTP 429 or code 117 (CallRateExceeded) waits briefly and
  retries; a long wait surfaces as an error instead of blocking the call.

Reports are asynchronous: :func:`run_report` submits, polls, downloads the
zipped CSV and returns its rows as dicts keyed by column name.
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from typing import TYPE_CHECKING, Any

from adloop.microsoft_ads.auth import MicrosoftAdsApiError, MicrosoftAdsAuthError

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

HOSTS = {
    "campaign": "https://campaign.api.bingads.microsoft.com/CampaignManagement/v13",
    "customer": "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13",
    "reporting": "https://reporting.api.bingads.microsoft.com/Reporting/v13",
}
DEVELOPER_SETTINGS_URL = "https://developers.ads.microsoft.com/Account"

_TIMEOUT_SECONDS = 60
_MAX_THROTTLE_RETRIES = 3
_MAX_THROTTLE_WAIT_SECONDS = 10
# Codes that mean the access token is not (or no longer) accepted.
_AUTH_CODES = {"105", "109", "AuthenticationTokenExpired", "InvalidCredentials"}
_THROTTLE_CODES = {"117", "CallRateExceeded"}
_MAX_MESSAGE = 500


def _credentials(config: AdLoopConfig):
    from adloop.auth import get_microsoft_ads_credentials

    return get_microsoft_ads_credentials(config)


def _user_agent() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"adloop/{version('adloop')}"
    except PackageNotFoundError:
        return "adloop/dev"


def fault_details(payload: Any) -> tuple[list[str], str, str]:
    """(codes, message, tracking id) from any of Microsoft's fault shapes."""
    if not isinstance(payload, dict):
        return [], "", ""
    entries: list[dict] = []
    for key in ("Errors", "OperationErrors", "BatchErrors", "PartialErrors"):
        value = payload.get(key)
        if isinstance(value, list):
            entries.extend(e for e in value if isinstance(e, dict))
    # Some faults nest the detail one level down.
    for key in ("Detail", "detail", "FaultDetail"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            codes, message, tracking = fault_details(inner)
            if codes or message:
                return codes, message, tracking or str(payload.get("TrackingId") or "")
    codes = [str(e.get("ErrorCode") or e.get("Code") or "") for e in entries]
    parts = []
    for e in entries[:5]:
        text = str(e.get("Message") or e.get("Details") or e.get("ErrorCode") or "")
        label = e.get("ErrorCode") or e.get("Code")
        field = e.get("FieldPath")
        parts.append(f"{label}: {text}" + (f" ({field})" if field else "") if label else text)
    message = "; ".join(p for p in parts if p) or str(
        payload.get("Message") or payload.get("message") or payload.get("error") or ""
    )
    numeric = [str(e.get("Code")) for e in entries if e.get("Code") is not None]
    return [c for c in codes + numeric if c], message[:_MAX_MESSAGE], str(payload.get("TrackingId") or "")


def _retry_after(headers: Any) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return max(int(float(raw)), 0) if raw is not None else None
    except (TypeError, ValueError):
        return None


def ms_request(
    config: AdLoopConfig,
    service: str,
    path: str,
    body: dict | None = None,
    *,
    method: str = "POST",
    account_id: str = "",
    customer_id: str = "",
    mutation: bool = False,
) -> dict:
    """One authenticated call to a Microsoft Advertising REST service.

    ``mutation`` marks a call that changes the account (an apply); it only
    changes what a lost connection is said to mean.
    """
    import requests

    cfg = config.microsoft_ads
    if not cfg.developer_token:
        raise MicrosoftAdsAuthError(
            "microsoft_ads.developer_token is not set. Request one in Microsoft Advertising "
            f"(Settings → Developer settings, or {DEVELOPER_SETTINGS_URL}) and add it to the config.",
            error_code="missing_developer_token",
        )
    creds = _credentials(config)
    url = f"{HOSTS[service]}/{path.lstrip('/')}"
    headers = {
        "DeveloperToken": cfg.developer_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _user_agent(),
    }
    customer = customer_id or cfg.customer_id
    if customer:
        headers["CustomerId"] = str(customer)
    if account_id:
        headers["CustomerAccountId"] = str(account_id)

    refreshed = False
    throttled = 0
    while True:
        headers["Authorization"] = f"Bearer {creds.token()}"
        try:
            response = requests.request(
                method.upper(), url, json=body if body is not None else {}, headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # A read can simply be repeated; a change may already have been
            # made before the connection broke.
            after = "the change may or may not have been made" if mutation else "try again"
            raise MicrosoftAdsApiError(
                f"Could not reach Microsoft Advertising for {path} ({type(exc).__name__}); {after}."
            ) from None
        status = response.status_code
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        # A fault may echo the request; keep every credential out of errors,
        # tool results and the audit log. A successful batch can carry item
        # errors too (PartialErrors, NestedPartialErrors), so those are
        # scrubbed as well; the rest of a successful answer is left alone.
        secrets = [headers["Authorization"][len("Bearer "):], cfg.developer_token, cfg.client_secret,
                   getattr(creds, "refresh_token", ""), getattr(creds, "access_token", "")]
        if 200 <= status < 300:
            if not isinstance(payload, dict):
                return {"data": payload}
            for key in _ERROR_KEYS:
                if payload.get(key):
                    payload[key] = _scrub(payload[key], secrets)
            return payload

        payload = _scrub(payload, secrets)
        codes, message, tracking = fault_details(payload)
        if (status == 401 or set(codes) & _AUTH_CODES) and not refreshed:
            refreshed = True
            creds.invalidate()
            continue
        if status == 429 or set(codes) & _THROTTLE_CODES:
            throttled += 1
            wait = _retry_after(response.headers)
            wait = 5 if wait is None else wait
            if throttled <= _MAX_THROTTLE_RETRIES and wait <= _MAX_THROTTLE_WAIT_SECONDS:
                time.sleep(max(wait, 1))
                continue
            raise MicrosoftAdsApiError(
                "Microsoft Advertising rate limit reached"
                + (f"; retry after {wait}s" if wait else "")
                + ". Wait before calling again; do not retry in a loop.",
                status=429, url=url, codes=codes, tracking_id=tracking, retry_after=wait,
            )
        if status == 401 or set(codes) & _AUTH_CODES:
            raise MicrosoftAdsAuthError(
                "Microsoft Advertising rejected the sign-in even after a refresh"
                + (f": {message}" if message else "")
                + ". Run `adloop microsoft-login` again.",
                error_code="invalid_credentials",
            )
        raise MicrosoftAdsApiError(
            f"Microsoft Advertising API returned {status} for {path}"
            + (f": {message}" if message else "")
            + (f" (tracking id {tracking})" if tracking else ""),
            status=status, url=url, codes=codes, tracking_id=tracking,
        )


# Response fields that carry errors, also inside a successful (2xx) answer.
_ERROR_KEYS = ("PartialErrors", "NestedPartialErrors", "Errors", "OperationErrors", "BatchErrors")


def _scrub(value: Any, secrets: list[str]) -> Any:
    """A parsed response with every secret (8+ characters) redacted from its strings."""
    if isinstance(value, str):
        for secret in secrets:
            if secret and len(secret) >= 8:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value


def campaign_request(config: AdLoopConfig, path: str, body: dict, *, account_id: str,
                     customer_id: str = "", method: str = "POST", mutation: bool = False) -> dict:
    return ms_request(config, "campaign", path, body, method=method, account_id=account_id,
                      customer_id=customer_id, mutation=mutation)


def customer_request(config: AdLoopConfig, path: str, body: dict, *, account_id: str = "") -> dict:
    return ms_request(config, "customer", path, body, account_id=account_id)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _date(value) -> dict:
    return {"Day": value.day, "Month": value.month, "Year": value.year}


def report_time(start, end, time_zone: str = "") -> dict:
    time_block: dict[str, Any] = {
        "CustomDateRangeStart": _date(start),
        "CustomDateRangeEnd": _date(end),
    }
    if time_zone:
        time_block["ReportTimeZone"] = time_zone
    return time_block


def parse_csv(data: bytes) -> list[dict]:
    """Rows of a report CSV (optionally zipped), keyed by column name."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                return []
            data = archive.read(names[0])
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        if not row or all(v in (None, "") for v in row.values()):
            continue
        # A report footer ("©2026 Microsoft Corporation") would land in the first column.
        first = next(iter(row.values()), "") or ""
        if first.startswith("©") or "Microsoft Corporation" in first:
            continue
        rows.append({k: (v if v is not None else "") for k, v in row.items() if k is not None})
    return rows


def download_file(url: str, *, what: str = "file") -> bytes:
    """Fetch a signed download URL; an error never repeats the URL."""
    import requests

    try:
        response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise MicrosoftAdsApiError(f"Downloading the {what} failed ({type(exc).__name__}); run the tool again.") from None
    if response.status_code >= 400:
        raise MicrosoftAdsApiError(
            f"Downloading the {what} failed with HTTP {response.status_code}; the link may have "
            "expired, run the tool again.",
            status=response.status_code,
        )
    return response.content


def run_report(
    config: AdLoopConfig,
    report_request: dict,
    *,
    account_id: str,
    customer_id: str = "",
    timeout_seconds: float = 90,
    sleep=time.sleep,
) -> list[dict]:
    """Submit a report, poll until it is ready, download and parse it."""
    request = {
        "Format": "Csv",
        "ExcludeReportHeader": True,
        "ExcludeReportFooter": True,
        "ExcludeColumnHeaders": False,
        "ReturnOnlyCompleteData": False,
        **report_request,
    }
    submitted = ms_request(
        config, "reporting", "GenerateReport/Submit", {"ReportRequest": request},
        account_id=account_id, customer_id=customer_id,
    )
    request_id = submitted.get("ReportRequestId")
    if not request_id:
        raise MicrosoftAdsApiError("Microsoft accepted the report request but returned no id.")

    waited = 0.0
    delay = 1.0
    while True:
        polled = ms_request(
            config, "reporting", "GenerateReport/Poll", {"ReportRequestId": request_id},
            account_id=account_id, customer_id=customer_id,
        )
        status_block = polled.get("ReportRequestStatus") or {}
        status = str(status_block.get("Status") or "")
        if status == "Success":
            url = status_block.get("ReportDownloadUrl")
            if not url:
                return []  # Microsoft returns no file when the report has no rows.
            return parse_csv(download_file(url, what=f"{report_request.get('Type') or 'report'} file"))
        if status == "Error":
            raise MicrosoftAdsApiError(
                f"Microsoft could not build the {report_request.get('Type')} report."
            )
        if waited >= timeout_seconds:
            raise MicrosoftAdsApiError(
                f"The {report_request.get('Type')} report was not ready after "
                f"{int(timeout_seconds)}s; try a shorter date range."
            )
        sleep(delay)
        waited += delay
        delay = min(delay * 2, 8.0)
