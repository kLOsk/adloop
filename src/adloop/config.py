"""Load and validate AdLoop configuration from ~/.adloop/config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class GoogleConfig:
    project_id: str = ""
    credentials_path: str = ""  # empty = use bundled credentials shipped with the package
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
class SafetyConfig:
    max_daily_budget: float = 50.0
    max_bid_increase_pct: int = 100
    require_dry_run: bool = True
    log_file: str = "~/.adloop/audit.log"
    blocked_operations: list[str] = field(default_factory=list)


@dataclass
class AdLoopConfig:
    google: GoogleConfig = field(default_factory=GoogleConfig)
    ga4: GA4Config = field(default_factory=GA4Config)
    ads: AdsConfig = field(default_factory=AdsConfig)
    gsc: GscConfig = field(default_factory=GscConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    # Absolute path the config was resolved from (even if it did not exist
    # on disk when loaded). Used by the runtime to tell callers exactly
    # which file to edit when a safety flag overrides their request.
    source_path: str = ""


def _resolve_path(path_str: str) -> Path:
    """Expand ~ and env vars in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(path_str)))


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
    safety_raw = raw.get("safety", {})

    return AdLoopConfig(
        google=GoogleConfig(
            project_id=google_raw.get("project_id", ""),
            credentials_path=google_raw.get("credentials_path", ""),
            token_path=google_raw.get("token_path", "~/.adloop/token.json"),
        ),
        ga4=GA4Config(
            property_id=ga4_raw.get("property_id", ""),
        ),
        ads=AdsConfig(
            developer_token=ads_raw.get("developer_token", ""),
            customer_id=ads_raw.get("customer_id", ""),
            login_customer_id=ads_raw.get("login_customer_id", ""),
        ),
        gsc=GscConfig(
            site_url=gsc_raw.get("site_url", ""),
        ),
        safety=SafetyConfig(
            max_daily_budget=safety_raw.get("max_daily_budget", 50.0),
            max_bid_increase_pct=safety_raw.get("max_bid_increase_pct", 100),
            require_dry_run=safety_raw.get("require_dry_run", True),
            log_file=safety_raw.get("log_file", "~/.adloop/audit.log"),
            blocked_operations=safety_raw.get("blocked_operations", []),
        ),
        source_path=resolved,
    )
