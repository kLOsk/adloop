"""Load and validate AdLoop configuration from ~/.adloop/config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class GoogleConfig:
    project_id: str = ""
    credentials_path: str = ""  # empty = ~/.adloop/credentials.json, else Application Default Credentials
    token_path: str = "~/.adloop/token.json"


@dataclass
class GA4Config:
    property_id: str = ""

    def __post_init__(self) -> None:
        if self.property_id and not self.property_id.startswith("properties/"):
            self.property_id = f"properties/{self.property_id}"


@dataclass
class AdsConfig:
    developer_token: str = ""
    customer_id: str = ""
    login_customer_id: str = ""


@dataclass
class GscConfig:
    site_url: str = ""  # e.g. "https://example.com/" or "sc-domain:example.com"


@dataclass
class GtmConfig:
    account_id: str = ""
    container_id: str = ""


@dataclass
class PageSpeedConfig:
    api_key: str = ""  # optional — keyless PSI calls work but are rate-limited


@dataclass
class RedditConfig:
    """Reddit Ads: own OAuth app (Reddit Business Manager → Developer
    Application), own token file. ``ad_account_id`` is the default account
    for every Reddit tool; ``username`` only feeds the User-Agent Reddit
    asks for (``platform:app:version (by /u/name)``)."""

    client_id: str = ""
    client_secret: str = ""
    ad_account_id: str = ""
    business_id: str = ""
    username: str = ""
    user_agent: str = ""  # empty = built from client_id + username
    token_path: str = "~/.adloop/reddit_token.json"


@dataclass
class SafetyConfig:
    max_daily_budget: float = 50.0
    max_bid_increase_pct: int = 100
    require_dry_run: bool = True
    # Two-phase apply: confirm_and_apply refuses dry_run=false for a plan
    # that has not completed a dry-run pass yet. Unlike require_dry_run
    # (which blocks real writes entirely), this only enforces the order:
    # preview first, apply second.
    two_phase_apply: bool = False
    log_file: str = "~/.adloop/audit.log"
    blocked_operations: list[str] = field(default_factory=list)


@dataclass
class AdLoopConfig:
    google: GoogleConfig = field(default_factory=GoogleConfig)
    ga4: GA4Config = field(default_factory=GA4Config)
    ads: AdsConfig = field(default_factory=AdsConfig)
    gsc: GscConfig = field(default_factory=GscConfig)
    gtm: GtmConfig = field(default_factory=GtmConfig)
    pagespeed: PageSpeedConfig = field(default_factory=PageSpeedConfig)
    reddit: RedditConfig = field(default_factory=RedditConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    # Absolute path the config was resolved from (even if it did not exist
    # on disk when loaded). Used by the runtime to tell callers exactly
    # which file to edit when a safety flag overrides their request.
    source_path: str = ""


def _resolve_path(path_str: str) -> Path:
    """Expand ~ and env vars in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(path_str)))


def _text(raw: dict, key: str, default: str = "") -> str:
    """Read a string setting, treating blank as absent.

    ``raw.get(key, default)`` only falls back when the key is *missing*, so an
    explicit ``token_path: ""`` — easy to produce from a template with blank
    placeholders — passed straight through. ``Path("")`` is ``Path(".")``, the
    working directory always exists, and adloop then tried to read the cwd as
    a token file and died with ``[Errno 1] Operation not permitted: '.'``,
    which points nowhere near the config that caused it.

    Also coerces non-strings, so ``customer_id: 1234567890`` in YAML arrives
    as text rather than an int.
    """
    value = raw.get(key, default)

    if value is None:
        return default

    return str(value).strip() or default

def load_config(config_path: str | None = None) -> AdLoopConfig:
    """Load configuration from YAML file.

    Resolution order:
    1. Explicit ``config_path`` argument
    2. ``ADLOOP_CONFIG`` environment variable
    3. ``~/.adloop/config.yaml`` default
    """
    if config_path is None:
        config_path = os.environ.get("ADLOOP_CONFIG", "~/.adloop/config.yaml")

    path = _resolve_path(config_path)
    resolved = str(path)

    if not path.exists():
        return AdLoopConfig(source_path=resolved)

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    google_raw = raw.get("google", {})
    ga4_raw = raw.get("ga4", {})
    ads_raw = raw.get("ads", {})
    gsc_raw = raw.get("gsc", {})
    gtm_raw = raw.get("gtm", {})
    pagespeed_raw = raw.get("pagespeed", {})
    reddit_raw = raw.get("reddit", {}) or {}
    safety_raw = raw.get("safety", {})

    return AdLoopConfig(
        google=GoogleConfig(
            project_id=_text(google_raw, "project_id"),
            credentials_path=_text(google_raw, "credentials_path"),
            token_path=_text(google_raw, "token_path", "~/.adloop/token.json"),
        ),
        ga4=GA4Config(
            property_id=_text(ga4_raw, "property_id"),
        ),
        ads=AdsConfig(
            developer_token=_text(ads_raw, "developer_token"),
            customer_id=_text(ads_raw, "customer_id"),
            login_customer_id=_text(ads_raw, "login_customer_id"),
        ),
        gsc=GscConfig(
            site_url=_text(gsc_raw, "site_url"),
        ),
        gtm=GtmConfig(
            account_id=_text(gtm_raw, "account_id"),
            container_id=_text(gtm_raw, "container_id"),
        ),
        pagespeed=PageSpeedConfig(
            api_key=_text(pagespeed_raw, "api_key"),
        ),
        reddit=RedditConfig(
            client_id=_text(reddit_raw, "client_id"),
            client_secret=_text(reddit_raw, "client_secret"),
            ad_account_id=_text(reddit_raw, "ad_account_id"),
            business_id=_text(reddit_raw, "business_id"),
            username=_text(reddit_raw, "username"),
            user_agent=_text(reddit_raw, "user_agent"),
            token_path=_text(reddit_raw, "token_path", "~/.adloop/reddit_token.json"),
        ),
        safety=SafetyConfig(
            max_daily_budget=safety_raw.get("max_daily_budget", 50.0),
            max_bid_increase_pct=safety_raw.get("max_bid_increase_pct", 100),
            require_dry_run=safety_raw.get("require_dry_run", True),
            two_phase_apply=safety_raw.get("two_phase_apply", False),
            log_file=_text(safety_raw, "log_file", "~/.adloop/audit.log"),
            blocked_operations=safety_raw.get("blocked_operations", []),
        ),
        source_path=resolved,
    )
