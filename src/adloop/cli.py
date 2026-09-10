"""Interactive setup wizard for first-time AdLoop configuration."""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

_ADLOOP_DIR = Path.home() / ".adloop"
_CONFIG_PATH = _ADLOOP_DIR / "config.yaml"

_GOOGLE_CLOUD_INSTRUCTIONS = """\
┌─────────────────────────────────────────────────────────────────┐
│  Google Cloud Setup Checklist                                   │
│                                                                 │
│  Complete these steps in your browser before continuing:        │
│                                                                 │
│  1. Create (or select) a Google Cloud project                   │
│     → https://console.cloud.google.com/projectcreate            │
│                                                                 │
│  2. Enable these APIs:                                          │
│     • Google Analytics Data API                                 │
│     • Google Analytics Admin API                                │
│     • Google Ads API                                            │
│     → https://console.cloud.google.com/apis/library             │
│                                                                 │
│  3. Create an OAuth consent screen (External, Testing mode OK)  │
│     → https://console.cloud.google.com/apis/credentials/consent │
│     Add your email as a test user.                              │
│                                                                 │
│  4. Create OAuth 2.0 Client ID (Desktop application)            │
│     → https://console.cloud.google.com/apis/credentials         │
│     Download the JSON file.                                     │
│                                                                 │
│  5. Get your Google Ads Developer Token from your MCC account   │
│     → https://ads.google.com/aw/apicenter                      │
│     (You need a Manager Account / MCC)                          │
└─────────────────────────────────────────────────────────────────┘
"""


def _print(msg: str = "") -> None:
    print(msg)


def _prompt(label: str, default: str = "", required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"  {label}{suffix}: ").strip()
        value = raw or default
        if value or not required:
            return value
        _print("    ⚠  This field is required.")


def _prompt_bool(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {label} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "true", "1")


def _format_customer_id(raw: str) -> str:
    """Normalize a customer ID to XXX-XXX-XXXX format."""
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return raw


def _validate_customer_id(raw: str) -> str | None:
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) != 10:
        return "Customer ID must be 10 digits (e.g. 123-456-7890)"
    return None


def _prompt_customer_id(label: str, default: str = "") -> str:
    while True:
        value = _prompt(label, default=default)
        formatted = _format_customer_id(value)
        err = _validate_customer_id(formatted)
        if err:
            _print(f"    ⚠  {err}")
            continue
        return formatted


def _validate_credentials_path(path_str: str) -> str | None:
    p = Path(path_str).expanduser()
    if not p.exists():
        return f"File not found: {p}"
    if not p.suffix == ".json":
        return "Expected a .json file"
    return None


def _prompt_credentials_path(default: str = "~/.adloop/credentials.json") -> str:
    while True:
        value = _prompt("Path to OAuth credentials JSON", default=default)
        err = _validate_credentials_path(value)
        if err:
            _print(f"    ⚠  {err}")
            retry = _prompt_bool("Try again?", default=True)
            if not retry:
                return value
            continue
        return value


def _prompt_property_id(default: str = "") -> str:
    while True:
        value = _prompt("GA4 Property ID (numeric)", default=default)
        if value and not value.isdigit():
            _print("    ⚠  Property ID should be numeric (e.g. 519379787)")
            continue
        return value


def _prompt_choice(label: str, choices: list[tuple[str, str]]) -> str:
    """Present a numbered list of choices and return the selected value."""
    _print(f"  {label}")
    _print()
    for i, (value, display) in enumerate(choices, 1):
        _print(f"    {i}. {display}")
    _print()
    while True:
        raw = input(f"  Enter number [1-{len(choices)}]: ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx][0]
        except ValueError:
            pass
        _print(f"    ⚠  Please enter a number between 1 and {len(choices)}")


def _generate_config_yaml(
    *,
    project_id: str = "",
    credentials_path: str = "",
    property_id: str,
    developer_token: str,
    customer_id: str,
    login_customer_id: str,
    max_daily_budget: float,
    require_dry_run: bool,
    gsc_site_url: str = "",
    gtm_account_id: str = "",
    gtm_container_id: str = "",
    pagespeed_api_key: str = "",
    reddit_client_id: str = "",
    reddit_client_secret: str = "",
    reddit_ad_account_id: str = "",
    reddit_username: str = "",
) -> str:
    dry_run_str = "true" if require_dry_run else "false"

    lines = [
        "# AdLoop configuration",
        "# Generated by: adloop init",
        "# Docs: https://github.com/kLOsk/adloop",
        "",
        "google:",
    ]
    if project_id:
        lines.append(f'  project_id: "{project_id}"')
    if credentials_path:
        # YAML treats `\` inside double-quoted strings as escape sequences (e.g.
        # `\U` begins a Unicode escape, breaking Windows paths like `c:\Users\...`).
        # Single-quoted strings are literal — backslashes are safe. Embedded
        # single quotes (rare in paths) are escaped per YAML spec by doubling them.
        escaped = credentials_path.replace("'", "''")
        lines.append(f"  credentials_path: '{escaped}'")
    else:
        lines.append("  # credentials_path resolved from ~/.adloop/credentials.json or ADC")
    lines.append('  token_path: "~/.adloop/token.json"')
    lines += [
        "",
        "ga4:",
        f'  property_id: "{property_id}"',
        "",
        "ads:",
        f'  developer_token: "{developer_token}"',
        f'  customer_id: "{customer_id}"',
        "  # MCC / Manager Account ID (required if using a manager account)",
        f'  login_customer_id: "{login_customer_id}"',
        "",
        "gsc:",
        '  # "https://example.com/" (URL-prefix) or "sc-domain:example.com".',
        "  # Empty = pass site_url as a parameter in each tool call instead.",
        f'  site_url: "{gsc_site_url}"',
        "",
        "gtm:",
        "  # Default GTM account/container for the GTM tools.",
        "  # Empty = pass gtm_account_id / gtm_container_id per call instead.",
        f'  account_id: "{gtm_account_id}"',
        f'  container_id: "{gtm_container_id}"',
        "",
        "pagespeed:",
        "  # Optional API key for analyze_page_speed (keyless works, low quota).",
        f'  api_key: "{pagespeed_api_key}"',
        "",
        "reddit:",
        "  # Reddit Ads: your own developer app (Reddit Business Manager →",
        "  # Developer Application). Token file is written by `adloop init`.",
        f'  client_id: "{reddit_client_id}"',
        f'  client_secret: "{reddit_client_secret}"',
        "  # Default ad account for every Reddit tool (see list_reddit_accounts).",
        f'  ad_account_id: "{reddit_ad_account_id}"',
        "  # Your Reddit username, only used in the User-Agent Reddit requires.",
        f'  username: "{reddit_username}"',
        '  token_path: "~/.adloop/reddit_token.json"',
        "",
        "safety:",
        "  # Maximum daily budget AdLoop can set (safety cap)",
        f"  max_daily_budget: {max_daily_budget}",
        "  max_bid_increase_pct: 100",
        "  # When true, confirm_and_apply always runs as dry_run regardless of the parameter",
        f"  require_dry_run: {dry_run_str}",
        '  log_file: "~/.adloop/audit.log"',
        "  blocked_operations: []",
        "",
    ]
    return "\n".join(lines)


def _mcp_json_snippet(toolsets: str = "") -> str:
    """mcpServers JSON block (Cursor and Claude Code use the same shape)."""
    python_path = sys.executable
    env_line = (
        f'\n      "env": {{ "ADLOOP_TOOLSETS": "{toolsets}" }},' if toolsets else ""
    )
    return textwrap.dedent(f"""\
        {{
          "mcpServers": {{
            "adloop": {{{env_line}
              "command": "{python_path}",
              "args": ["-m", "adloop"]
            }}
          }}
        }}
    """).strip()


def _generate_cursor_snippet(toolsets: str = "") -> str:
    return _mcp_json_snippet(toolsets)


def _generate_claude_code_snippet(toolsets: str = "") -> str:
    """Generate the Claude Code CLI command to add AdLoop as an MCP server."""
    python_path = sys.executable
    quoted = f'"{python_path}"' if " " in python_path else python_path
    env = f" --env ADLOOP_TOOLSETS={toolsets}" if toolsets else ""
    return f"claude mcp add --transport stdio adloop{env} -- {quoted} -m adloop"


def _generate_claude_json_snippet(toolsets: str = "") -> str:
    """Generate .mcp.json configuration for Claude Code."""
    return _mcp_json_snippet(toolsets)


def _prompt_toolsets() -> str:
    """Optional toolset subset for the MCP client's env ("" = everything).

    Client-side (env), not config.yaml: the same machine can run one
    full-catalog client and one trimmed client off one AdLoop config.
    """
    from adloop.server import TOOLSETS

    _print("  AdLoop can expose only some toolsets to your AI client —")
    _print("  a smaller tool list costs less context per conversation.")
    _print()
    for slug, description in TOOLSETS.items():
        _print(f"    {slug:<9} {description}")
    _print()
    _print("  (health_check and confirm_and_apply are always included.)")
    if _prompt_bool("Expose the full toolset?", default=True):
        return ""
    while True:
        raw = _prompt("Toolsets (comma-separated, e.g. ads,ga4)")
        chosen = [part.strip().lower() for part in raw.split(",") if part.strip()]
        unknown = [slug for slug in chosen if slug not in TOOLSETS]
        if chosen and not unknown:
            return ",".join(dict.fromkeys(chosen))
        _print(
            f"    ⚠  Unknown toolset(s): {', '.join(unknown) or '(none given)'}."
            f" Valid: {', '.join(TOOLSETS)}"
        )


def _wizard_gtm_step(oauth_ok: bool, _existing) -> tuple[str, str]:
    """Optionally pin a GTM account + container (discovery when possible)."""
    if not _prompt_bool("Pin a Google Tag Manager container? (optional)", default=False):
        return (_existing("gtm", "account_id"), _existing("gtm", "container_id"))

    if oauth_ok:
        try:
            from adloop.config import load_config
            from adloop.gtm.read import list_accounts, list_containers

            cfg = load_config(str(_CONFIG_PATH))
            accounts = list_accounts(cfg).get("accounts", [])
            if not accounts:
                _print("  No GTM accounts on this Google account — skipping.")
                _print("  (Sites can track fine without GTM — gtag.js directly.)")
                return ("", "")
            if len(accounts) == 1:
                account_id = str(accounts[0]["account_id"])
                _print(f"  ✓ Found GTM account: {accounts[0]['name']}")
            else:
                account_id = _prompt_choice(
                    "Select your GTM account:",
                    [
                        (str(a["account_id"]), f"{a['name']} ({a['account_id']})")
                        for a in accounts
                    ],
                )
            containers = list_containers(cfg, account_id=account_id).get(
                "containers", []
            )
            web = [
                c
                for c in containers
                if "web" in [str(u).lower() for u in (c.get("usage_context") or [])]
            ] or containers
            if not web:
                _print("  No containers in that account — skipping container pin.")
                return (account_id, "")
            if len(web) == 1:
                _print(f"  ✓ Found container: {web[0]['name']} ({web[0]['public_id']})")
                return (account_id, str(web[0]["container_id"]))
            container_id = _prompt_choice(
                "Select your GTM container:",
                [
                    (str(c["container_id"]), f"{c['name']} ({c['public_id']})")
                    for c in web
                ],
            )
            return (account_id, container_id)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _print(f"  Could not discover GTM ({exc}) — enter manually or skip.")
            _print("  (The Tag Manager API must be enabled in your GCP project.)")

    account_id = _prompt(
        "GTM Account ID (Enter to skip)",
        default=_existing("gtm", "account_id"),
        required=False,
    )
    container_id = ""
    if account_id:
        container_id = _prompt(
            "GTM Container ID (numeric, Enter to skip)",
            default=_existing("gtm", "container_id"),
            required=False,
        )
    return (account_id, container_id)


def _wizard_gsc_step(oauth_ok: bool, _existing) -> str:
    """Optionally pin a Search Console property (discovery when possible)."""
    if not _prompt_bool("Pin a Search Console property? (optional)", default=False):
        return _existing("gsc", "site_url")

    if oauth_ok:
        try:
            from adloop.config import load_config
            from adloop.gsc.reports import list_gsc_sites

            cfg = load_config(str(_CONFIG_PATH))
            sites = list_gsc_sites(cfg).get("sites", [])
            if not sites:
                _print("  No Search Console properties on this account — skipping.")
                return ""
            if len(sites) == 1:
                _print(f"  ✓ Found GSC property: {sites[0]['site_url']}")
                if _prompt_bool("Use this property?", default=True):
                    return sites[0]["site_url"]
            else:
                return _prompt_choice(
                    "Select your Search Console property:",
                    [
                        (s["site_url"], f"{s['site_url']} ({s['permission_level']})")
                        for s in sites
                    ],
                )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _print(f"  Could not discover GSC properties ({exc}).")
            _print("  (The Search Console API must be enabled in your GCP project.)")

    return _prompt(
        'GSC site URL ("https://example.com/" or "sc-domain:example.com", Enter to skip)',
        default=_existing("gsc", "site_url"),
        required=False,
    )


def _wizard_reddit_step(_existing) -> dict[str, str]:
    """Optionally connect Reddit Ads: own developer app, own OAuth, own token file.

    Returns the ``reddit:`` values for the config. Reddit's redirect URI
    must match the app exactly, so the loopback listener uses a fixed port
    and the user registers precisely that URL on the app.
    """
    from adloop.reddit.auth import LOCAL_REDIRECT_URI

    existing_id = _existing("reddit", "client_id")
    values = {
        "client_id": existing_id,
        "client_secret": _existing("reddit", "client_secret"),
        "ad_account_id": _existing("reddit", "ad_account_id"),
        "username": _existing("reddit", "username"),
    }
    if not _prompt_bool("Connect Reddit Ads? (optional)", default=bool(existing_id)):
        return values

    _print()
    _print("  Reddit Ads uses its own developer app (no approval, no developer token):")
    _print("    1. Reddit Ads Manager → Business Manager → Developer Application → Create app")
    _print(f"    2. Redirect URL: exactly {LOCAL_REDIRECT_URI}")
    _print("    3. Copy the app ID and secret below")
    _print()
    values["client_id"] = _prompt("Reddit app ID", default=existing_id)
    values["client_secret"] = _prompt(
        "Reddit app secret", default=values["client_secret"]
    )
    values["username"] = _prompt(
        "Your Reddit username (for the User-Agent Reddit requires)",
        default=values["username"],
        required=False,
    )

    from adloop.config import AdLoopConfig, RedditConfig

    cfg = AdLoopConfig(
        reddit=RedditConfig(
            client_id=values["client_id"],
            client_secret=values["client_secret"],
            username=values["username"],
        )
    )
    try:
        from adloop.reddit.auth import run_local_authorization

        run_local_authorization(cfg)
        _print("  ✓ Reddit authorized; token saved to ~/.adloop/reddit_token.json")
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _print(f"  ✗ Reddit authorization failed: {exc}")
        _print("    You can re-run `adloop init` later; the app credentials are kept.")
        return values

    try:
        from adloop.reddit.read import list_reddit_accounts

        accounts = list_reddit_accounts(cfg).get("accounts", [])
        if not accounts:
            _print("  No Reddit ad accounts visible to this user — set ad_account_id later.")
        elif len(accounts) == 1:
            acct = accounts[0]
            _print(f"  ✓ Found Reddit ad account: {acct['name']} ({acct['ad_account_id']})")
            if _prompt_bool("Use this ad account?", default=True):
                values["ad_account_id"] = str(acct["ad_account_id"])
        else:
            values["ad_account_id"] = _prompt_choice(
                "Select your Reddit ad account:",
                [
                    (
                        str(a["ad_account_id"]),
                        f"{a['name']} ({a['ad_account_id']}, {a.get('currency') or '?'}, "
                        f"{a.get('business_name') or ''})",
                    )
                    for a in accounts
                ],
            )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _print(f"  Could not list Reddit ad accounts ({exc}); set reddit.ad_account_id later.")
    return values


def _step_header(num: int, title: str) -> None:
    _print()
    _print(f"  ── Step {num}: {title} ──")
    _print()


def _discover_ga4_properties(config: object) -> list[tuple[str, str]]:
    """Call GA4 Admin API to discover accessible properties.

    Returns list of (property_id, display_string) tuples.
    """
    from adloop.ga4.reports import get_account_summaries

    result = get_account_summaries(config)  # type: ignore[arg-type]
    properties: list[tuple[str, str]] = []
    for account in result.get("accounts", []):
        acct_name = account.get("display_name", "Unknown")
        for prop in account.get("properties", []):
            prop_path = prop.get("property", "")
            prop_name = prop.get("display_name", "Unknown")
            prop_id = prop_path.replace("properties/", "")
            properties.append((prop_id, f"{prop_name} ({prop_id}) — {acct_name}"))
    return properties


def _discover_ads_accounts(config: object) -> list[tuple[str, str]]:
    """Call Google Ads API to discover accessible accounts.

    Returns list of (customer_id_formatted, display_string) tuples.
    Non-manager accounts only.
    """
    from adloop.ads.read import list_accounts

    result = list_accounts(config)  # type: ignore[arg-type]
    accounts: list[tuple[str, str]] = []
    for acct in result.get("accounts", []):
        is_manager = False
        for key in ("customer_client.manager", "customer.manager"):
            if acct.get(key) is True:
                is_manager = True
                break
        if is_manager:
            continue

        raw_id = ""
        name = "Unknown"
        for id_key in ("customer_client.id", "customer.id"):
            if id_key in acct:
                raw_id = str(acct[id_key])
                break
        for name_key in ("customer_client.descriptive_name", "customer.descriptive_name"):
            if name_key in acct:
                name = str(acct[name_key])
                break

        if raw_id:
            formatted = _format_customer_id(raw_id)
            accounts.append((formatted, f"{name} ({formatted})"))
    return accounts


def run_init_wizard() -> None:
    """Interactive setup wizard for AdLoop."""
    _print()
    _print("  ╔═══════════════════════════════════╗")
    _print("  ║     AdLoop Setup Wizard           ║")
    _print("  ╚═══════════════════════════════════╝")
    _print()

    existing_config = None
    _original_config_backup: str | None = None
    if _CONFIG_PATH.exists():
        _print(f"  Found existing config at {_CONFIG_PATH}")
        if not _prompt_bool("Overwrite existing configuration?", default=False):
            _print("  Keeping existing config. Exiting.")
            return
        _original_config_backup = _CONFIG_PATH.read_text()
        try:
            import yaml

            existing_config = yaml.safe_load(_original_config_backup) or {}
        except Exception:
            existing_config = {}

    def _existing(section: str, key: str, fallback: str = "") -> str:
        if existing_config and section in existing_config:
            return str(existing_config[section].get(key, fallback))
        return fallback

    # Step 1: Google Cloud project
    #
    # AdLoop no longer ships OAuth credentials. Every install brings its
    # own Google Cloud project: no shared user caps, no dependency on a
    # central verification review, and the consent screen shows YOUR
    # project to YOUR account. The hosted zero-setup alternative is
    # AdLoop Cloud (https://getadloop.com).
    _step_header(1, "Google Cloud Project")
    _print("  AdLoop uses your own (free) Google Cloud project for OAuth —")
    _print("  a one-time setup of about 5 minutes.")
    _print("  Prefer zero setup? AdLoop Cloud handles Google credentials")
    _print("  for you: https://getadloop.com")
    _print()
    _print(_GOOGLE_CLOUD_INSTRUCTIONS)
    input("  Press Enter when you've completed the steps above...")

    _step_header(2, "OAuth Credentials")
    credentials_path = _prompt_credentials_path(
        default=_existing("google", "credentials_path", "~/.adloop/credentials.json")
    )

    _step_header(3, "Google Cloud Project ID")
    project_id = _prompt(
        "Google Cloud Project ID",
        default=_existing("google", "project_id"),
    )

    step_num = 4
    _step_header(step_num, "Google Ads Developer Token")
    _print("  Find your developer token in your MCC account:")
    _print("  → https://ads.google.com/aw/apicenter")
    _print()
    developer_token = _prompt(
        "Developer Token",
        default=_existing("ads", "developer_token"),
    )

    # MCC Account ID (needed before auto-discovery for Ads API calls)
    step_num += 1
    _step_header(step_num, "MCC / Manager Account")
    _print("  Your MCC (Manager) account ID is in the top bar of your MCC.")
    _print()
    login_customer_id = _prompt_customer_id(
        "MCC Account ID (XXX-XXX-XXXX)",
        default=_existing("ads", "login_customer_id"),
    )

    # OAuth + auto-discovery
    step_num += 1
    _step_header(step_num, "Authorization & Account Discovery")

    # Write a temporary config for OAuth + discovery.  If the wizard is
    # interrupted after this point, _cleanup_on_cancel restores the original.
    _ADLOOP_DIR.mkdir(parents=True, exist_ok=True)
    temp_config_yaml = _generate_config_yaml(
        project_id=project_id,
        credentials_path=credentials_path,
        property_id="",
        developer_token=developer_token,
        customer_id="",
        login_customer_id=login_customer_id,
        max_daily_budget=50.0,
        require_dry_run=True,
    )
    _CONFIG_PATH.write_text(temp_config_yaml)

    # Everything below uses the temp config for OAuth and discovery.
    # If the wizard is interrupted, restore the original config (or remove
    # the temp) so we never leave a half-baked config on disk.
    try:
        _run_wizard_post_config(
            credentials_path=credentials_path,
            project_id=project_id,
            developer_token=developer_token,
            login_customer_id=login_customer_id,
            step_num=step_num,
            _existing=_existing,
        )
    except KeyboardInterrupt:
        if _original_config_backup is not None:
            _CONFIG_PATH.write_text(_original_config_backup)
        elif _CONFIG_PATH.exists():
            _CONFIG_PATH.unlink()
        raise


def _run_wizard_post_config(
    *,
    credentials_path: str,
    project_id: str,
    developer_token: str,
    login_customer_id: str,
    step_num: int,
    _existing: object,
) -> None:
    """Run the wizard steps after the temp config has been written."""
    from adloop.config import load_config

    # Optional: copy custom credentials to ~/.adloop/
    if credentials_path:
        creds_expanded = Path(credentials_path).expanduser()
        adloop_creds = _ADLOOP_DIR / "credentials.json"
        if creds_expanded != adloop_creds and creds_expanded.exists():
            if _prompt_bool(
                f"Copy {creds_expanded.name} to {_ADLOOP_DIR}?", default=True
            ):
                import shutil

                shutil.copy2(creds_expanded, adloop_creds)
                _print(f"  ✓ Credentials copied to {adloop_creds}")

    _print("  Signing in with Google (this may open a browser)...")
    _print()
    oauth_ok = False
    try:
        from adloop.auth import _oauth_flow

        cfg = load_config(str(_CONFIG_PATH))
        _oauth_flow(cfg)
        _print("  ✓ OAuth token saved")
        oauth_ok = True
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _print(f"  ✗ OAuth failed: {exc}")
        _print("    You can retry later — any AdLoop tool call will trigger auth.")

    # Auto-discover GA4 properties
    property_id = ""
    if oauth_ok:
        _print()
        _print("  Discovering GA4 properties...")
        try:
            cfg = load_config(str(_CONFIG_PATH))
            ga4_props = _discover_ga4_properties(cfg)
            if len(ga4_props) == 1:
                property_id = ga4_props[0][0]
                _print(f"  ✓ Found GA4 property: {ga4_props[0][1]}")
                if not _prompt_bool("Use this property?", default=True):
                    property_id = _prompt_property_id()
            elif len(ga4_props) > 1:
                _print(f"  Found {len(ga4_props)} GA4 properties:")
                property_id = _prompt_choice(
                    "Select your GA4 property:", ga4_props
                )
            else:
                _print("  No GA4 properties found. Enter manually:")
                property_id = _prompt_property_id()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _print(f"  Could not auto-discover GA4 properties: {exc}")
            property_id = _prompt_property_id(
                default=_existing("ga4", "property_id"),  # type: ignore[operator]
            )
    else:
        step_num += 1
        _step_header(step_num, "Google Analytics (GA4)")
        _print("  Find your GA4 Property ID at:")
        _print("  → https://analytics.google.com → Admin → Property Settings")
        _print()
        property_id = _prompt_property_id(
            default=_existing("ga4", "property_id"),  # type: ignore[operator]
        )

    # Auto-discover Ads accounts
    customer_id = ""
    if oauth_ok:
        _print()
        _print("  Discovering Google Ads accounts...")
        try:
            cfg = load_config(str(_CONFIG_PATH))
            ads_accounts = _discover_ads_accounts(cfg)
            if len(ads_accounts) == 1:
                customer_id = ads_accounts[0][0]
                _print(f"  ✓ Found Ads account: {ads_accounts[0][1]}")
                if not _prompt_bool("Use this account?", default=True):
                    customer_id = _prompt_customer_id("Ads Customer ID (XXX-XXX-XXXX)")
            elif len(ads_accounts) > 1:
                _print(f"  Found {len(ads_accounts)} Ads accounts:")
                customer_id = _prompt_choice(
                    "Select your default Ads account:", ads_accounts
                )
            else:
                _print("  No Ads accounts found. Enter manually:")
                customer_id = _prompt_customer_id("Ads Customer ID (XXX-XXX-XXXX)")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _print(f"  Could not auto-discover Ads accounts: {exc}")
            customer_id = _prompt_customer_id(
                "Ads Customer ID (XXX-XXX-XXXX)",
                default=_existing("ads", "customer_id"),  # type: ignore[operator]
            )
    else:
        step_num += 1
        _step_header(step_num, "Google Ads Account")
        customer_id = _prompt_customer_id(
            "Ads Customer ID (XXX-XXX-XXXX)",
            default=_existing("ads", "customer_id"),  # type: ignore[operator]
        )

    # Optional service pins: GTM / Search Console / PageSpeed. All three
    # work without config (explicit args or discovery per call) — a pin
    # just saves the model a lookup and locks tools to the right property.
    step_num += 1
    _step_header(step_num, "Optional Services (GTM, Search Console, PageSpeed)")
    gtm_account_id, gtm_container_id = _wizard_gtm_step(oauth_ok, _existing)
    gsc_site_url = _wizard_gsc_step(oauth_ok, _existing)
    pagespeed_api_key = _prompt(
        "PageSpeed Insights API key (optional, Enter to skip)",
        default=_existing("pagespeed", "api_key"),  # type: ignore[operator]
        required=False,
    )

    # Reddit Ads: a second ad platform with its own OAuth app and token file.
    step_num += 1
    _step_header(step_num, "Reddit Ads (optional)")
    reddit_values = _wizard_reddit_step(_existing)

    # Safety defaults
    step_num += 1
    _step_header(step_num, "Safety Defaults")
    budget_str = _prompt(
        "Max daily budget cap (safety limit)",
        default=str(_existing("safety", "max_daily_budget", "50")),  # type: ignore[operator]
        required=False,
    )
    try:
        max_daily_budget = float(budget_str) if budget_str else 50.0
    except ValueError:
        max_daily_budget = 50.0
        _print("    ⚠  Invalid number, using default 50.0")

    require_dry_run = _prompt_bool(
        "Require dry_run for all mutations? (recommended for setup)",
        default=True,
    )

    # Write final config
    _print()
    _print("  ── Writing Configuration ──")

    config_yaml = _generate_config_yaml(
        project_id=project_id,
        credentials_path=credentials_path,
        property_id=property_id,
        developer_token=developer_token,
        customer_id=customer_id,
        login_customer_id=login_customer_id,
        max_daily_budget=max_daily_budget,
        require_dry_run=require_dry_run,
        gsc_site_url=gsc_site_url,
        gtm_account_id=gtm_account_id,
        gtm_container_id=gtm_container_id,
        pagespeed_api_key=pagespeed_api_key,
        reddit_client_id=reddit_values["client_id"],
        reddit_client_secret=reddit_values["client_secret"],
        reddit_ad_account_id=reddit_values["ad_account_id"],
        reddit_username=reddit_values["username"],
    )
    _CONFIG_PATH.write_text(config_yaml)
    _print(f"  ✓ Config written to {_CONFIG_PATH}")

    # Toolset subset (optional, client-side env)
    step_num += 1
    _step_header(step_num, "Toolsets (optional)")
    toolsets = _prompt_toolsets()

    # MCP configuration snippets
    _print()
    _print("  ── MCP Configuration ──")

    # Cursor
    _print()
    _print("  For Cursor, add to .cursor/mcp.json:")
    _print()
    cursor_snippet = _generate_cursor_snippet(toolsets)
    for line in cursor_snippet.splitlines():
        _print(f"    {line}")
    _print()
    _print("  Then copy .cursor/rules/adloop.mdc into your project.")

    # Claude Code
    _print()
    _print("  For Claude Code, run:")
    _print()
    _print(f"    {_generate_claude_code_snippet(toolsets)}")
    _print()
    _print("  Or add to your project's .mcp.json:")
    _print()
    claude_snippet = _generate_claude_json_snippet(toolsets)
    for line in claude_snippet.splitlines():
        _print(f"    {line}")
    if toolsets:
        _print()
        _print("  (Toolsets are set per client — edit ADLOOP_TOOLSETS in the")
        _print("   snippet above anytime to widen or narrow the selection.)")
    _print()
    _print("  (Or run `adloop install-rules` after this wizard to install")
    _print("   rules + slash commands automatically into ~/.claude/.)")

    # Offer to install Claude rules globally now if a Claude installation
    # is detected. Cursor is intentionally skipped — Cursor handles workspace
    # rules natively via .cursor/rules/.
    _maybe_offer_global_rules_install()

    _print()
    _print("  Restart your editor to pick up the MCP server.")
    _print()
    _print("  ✓ Setup complete!")
    _print()


def _maybe_offer_global_rules_install() -> None:
    """If Claude is detected, offer to install rules globally during init."""
    from adloop.rules_install import detect_clients, install_rules

    clients = detect_clients()
    if not clients:
        return

    _print()
    _print("  ── Claude Orchestration Rules ──")
    _print()
    _print("  Detected Claude installations:")
    for c in clients:
        _print(f"    • {c.display_name}")
    _print()
    _print("  AdLoop ships orchestration rules that teach Claude how to use")
    _print("  these tools safely (tool inventory, safety patterns, GAQL reference).")
    _print("  Without them, you get raw tool access but no orchestration.")
    _print()

    if not _prompt_bool("Install rules globally now?", default=True):
        _print("  Skipping. Run `adloop install-rules` later if you change your mind.")
        return

    _print()
    _print("  Install mode:")
    _print("    1. inline (default) — full rules in ~/.claude/CLAUDE.md")
    _print("       Reliable; loaded every Claude Code session (~10K tokens).")
    _print("    2. lazy — small directive in CLAUDE.md, full rules in")
    _print("       ~/.claude/rules/adloop.md, loaded only when AdLoop is active.")
    _print("       Cheaper baseline cost; slightly less reliable.")
    _print()
    raw = input("  Choose [1/2, default 1]: ").strip()
    mode = "lazy" if raw == "2" else "inline"

    _print()
    results = install_rules(mode=mode)
    _print_install_results(results)


def _print_install_results(results: list) -> None:
    """Pretty-print install/update/uninstall results."""
    if not results:
        _print("  No Claude installations detected — nothing to do.")
        return
    for r in results:
        if r.action == "manual":
            _print(f"  ⚠  {r.client}: manual step required")
            _print()
            for line in r.instructions.splitlines():
                _print(f"    {line}")
            _print()
            continue
        target = r.rules_target if r.rules_target else "(none)"
        _print(f"  ✓ {r.client}: {r.action} → {target}")
        if r.commands_installed:
            _print(
                f"    {len(r.commands_installed)} slash command(s) installed"
                f" (prefixed adloop-*)"
            )
        if r.commands_removed:
            _print(
                f"    {len(r.commands_removed)} slash command(s) removed"
            )


def run_rules_command(subcommand: str, argv: list[str]) -> int:
    """Entry point for `adloop install-rules / update-rules / uninstall-rules`.

    Returns the process exit code (0 = success).
    """
    from adloop.rules_install import (
        install_rules,
        uninstall_rules,
        update_rules,
    )

    mode = "inline"
    install_commands = True
    if "--lazy" in argv:
        mode = "lazy"
    if "--no-commands" in argv:
        install_commands = False

    _print()
    if subcommand == "install-rules":
        _print("  Installing AdLoop rules...")
        results = install_rules(mode=mode, install_commands=install_commands)
    elif subcommand == "update-rules":
        _print("  Updating AdLoop rules...")
        # update-rules preserves the existing mode when no flag is passed.
        explicit_mode = mode if "--lazy" in argv or "--inline" in argv else None
        results = update_rules(
            mode=explicit_mode, install_commands=install_commands  # type: ignore[arg-type]
        )
    elif subcommand == "uninstall-rules":
        _print("  Uninstalling AdLoop rules...")
        results = uninstall_rules(remove_commands=install_commands)
    else:
        _print(f"  Unknown subcommand: {subcommand}")
        return 1

    _print_install_results(results)
    _print()
    return 0
