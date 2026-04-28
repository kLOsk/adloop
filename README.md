<div align="center">

# AdLoop

**Stop switching between Google Ads, GA4, and your code editor to figure out why conversions dropped.**

[![PyPI](https://img.shields.io/pypi/v/adloop.svg)](https://pypi.org/project/adloop/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-8A2BE2.svg)](https://modelcontextprotocol.io)
[![Google Ads API](https://img.shields.io/badge/Google%20Ads-API%20v23-4285F4.svg?logo=google-ads&logoColor=white)](https://developers.google.com/google-ads/api/docs/start)
[![GA4 Data API](https://img.shields.io/badge/GA4-Data%20API-E37400.svg?logo=google-analytics&logoColor=white)](https://developers.google.com/analytics/devguides/reporting/data/v1)
[![GitHub stars](https://img.shields.io/github/stars/kLOsk/adloop?style=social)](https://github.com/kLOsk/adloop)

An MCP server that gives your AI assistant read + write access to Google Ads and GA4 — with safety guardrails that prevent accidental spend.

`pip install adloop`

</div>

---

## What It Solves

AdLoop exists because managing Google Ads alongside your code is a mess. These are the specific problems it handles:

- **"My conversions dropped and I don't know why."** AdLoop cross-references Ads clicks, GA4 sessions, and conversion events in one query. It detects whether the gap is from GDPR consent rejection, broken tracking, or an actual landing page problem — before you waste hours checking each dashboard separately.

- **"I'm wasting ad spend on irrelevant searches."** Pull your search terms report, identify the junk, and add negative keywords — all from a single conversation in your IDE. No context-switching to the Ads UI.

- **"Is my tracking even working?"** Compare the event names in your actual codebase against what GA4 is receiving. Find the mismatches: events you fire that GA4 never sees, events GA4 records that you didn't know about.

- **"I need to create ads but the Google Ads UI is hostile."** Draft responsive search ads, create campaigns, add keywords — all through natural language. Every change shows a preview first. Nothing goes live without your explicit confirmation. New ads and campaigns start paused.

- **"My landing page gets paid traffic but nobody converts."** AdLoop joins your ad final URLs with GA4 page-level data. See which pages get clicks but no conversions, which have high bounce rates, and which ones are orphaned from any ad campaign.

- **"I don't know if my EU consent setup is causing data gaps."** In Europe, 30-70% of users reject analytics cookies. AdLoop accounts for this automatically — it won't diagnose a normal GDPR consent gap as broken tracking.

## Built From Real Usage

Every tool exists because of an actual problem hit while running real Google Ads campaigns. The cross-reference tools exist because we kept manually asking the AI to "get Ads data, then get GA4 data, then compare them" — so we automated the join. The Broad Match + Manual CPC safety rule exists because the AI once created that exact combination and wasted budget. The GDPR consent awareness exists because the AI kept diagnosing normal EU cookie rejection as broken tracking.

The best features come from real workflows. If you're using AdLoop and find yourself wishing it could do something it can't, **open an issue describing your situation** — not just "add feature X" but "I was trying to do Y and couldn't because Z." The context matters more than the request.

## All 43 Tools

> **Quick start:** `pip install adloop` or `git clone https://github.com/kLOsk/adloop.git && cd adloop && uv sync && uv run adloop init`

### Diagnostics

| Tool | What It Does |
|------|-------------|
| `health_check` | Test OAuth, GA4, and Ads connectivity in one call — actionable error messages if anything is broken. Also reports the pinned Google Ads API version and warns if a newer version is available. |

### GA4 Read Tools

| Tool | What It Does |
|------|-------------|
| `get_account_summaries` | List GA4 accounts and properties |
| `run_ga4_report` | Custom reports — sessions, users, conversions, page performance |
| `run_realtime_report` | Live data — verify tracking fires after deploys |
| `get_tracking_events` | All configured events and their volume |

### Google Ads Read Tools

| Tool | What It Does |
|------|-------------|
| `list_accounts` | Discover accessible Ads accounts |
| `get_campaign_performance` | Campaign metrics — impressions, clicks, cost, conversions, CPA |
| `get_ad_performance` | Ad copy analysis — headlines, descriptions, CTR |
| `get_keyword_performance` | Keywords — quality scores, competitive metrics |
| `get_search_terms` | What users actually searched before clicking |
| `get_negative_keywords` | List direct campaign-level negative keywords |
| `get_negative_keyword_lists` | List all shared negative keyword lists (SharedSets) — names, IDs, status, keyword count |
| `get_negative_keyword_list_keywords` | List the keywords inside a specific shared negative keyword list |
| `get_negative_keyword_list_campaigns` | List which campaigns a shared negative keyword list is attached to |
| `get_recommendations` | Google's auto-generated recommendations with type, estimated impact, and campaign context |
| `get_pmax_performance` | Performance Max campaign metrics with network breakdown + asset group ad strength |
| `get_asset_performance` | Per-asset details for PMax — field type, serving status, content |
| `get_detailed_asset_performance` | Top-performing asset combinations — which headline+description+image combos Google selects most |
| `get_audience_performance` | Audience segment performance — remarketing, in-market, affinity, demographics |
| `run_gaql` | Arbitrary GAQL queries for anything else |

### Cross-Reference Tools (GA4 + Ads Combined)

These tools call both APIs internally and return unified results with auto-generated insights. They're the core of what makes AdLoop different from having separate GA4 and Ads tools.

| Tool | What It Does |
|------|-------------|
| `analyze_campaign_conversions` | Maps Ads clicks → GA4 sessions → conversions per campaign. Detects GDPR consent gaps, computes real CPA, compares paid vs organic channels. |
| `landing_page_analysis` | Joins ad final URLs with GA4 page data. Shows conversion rate, bounce rate, and engagement per landing page. Flags pages with paid traffic but zero conversions. |
| `attribution_check` | Compares Ads-reported conversions vs GA4 events. Diagnoses whether discrepancies are from GDPR consent, attribution windows, or broken tracking. |

### Tracking Tools

| Tool | What It Does |
|------|-------------|
| `validate_tracking` | Compare event names found in your codebase against what GA4 actually records. Returns matched, missing, and unexpected events with diagnostics. |
| `generate_tracking_code` | Generate ready-to-paste GA4 gtag JavaScript for any event, with recommended parameters for well-known events (sign_up, purchase, etc.) and optional trigger wrappers. |

### Planning Tools

| Tool | What It Does |
|------|-------------|
| `discover_keywords` | Discover new keyword ideas from seed keywords and/or a URL using Google Ads Keyword Planner. Returns avg monthly searches, competition level, and top-of-page bid range. |
| `estimate_budget` | Forecast clicks, impressions, and cost for a set of keywords using Google Ads Keyword Planner. Supports geo/language targeting. Essential for budget planning before launching campaigns. |

### Google Ads Write Tools

All write operations follow a **draft → preview → confirm** workflow. Nothing executes without explicit approval.

| Tool | What It Does |
|------|-------------|
| `draft_campaign` | Create a full campaign structure — budget + campaign (PAUSED) + ad group + optional keywords. Supports Search partners, display expansion, and `max_cpc` for either MANUAL_CPC initial ad-group bids or TARGET_SPEND (Maximize Clicks) CPC caps. |
| `update_campaign` | Modify existing campaign settings — bidding, budget, geo/language targeting, Search partners, display expansion, and TARGET_SPEND (Maximize Clicks) `max_cpc` caps. |
| `draft_ad_group` | Create a paused SEARCH_STANDARD ad group inside an existing campaign, with optional MANUAL_CPC `max_cpc`. |
| `update_ad_group` | Update an ad group name and/or MANUAL_CPC `max_cpc`. Use `pause_entity` / `enable_entity` for ad-group status changes. |
| `draft_responsive_search_ad` | Create RSA preview (3-15 headlines ≤30 chars, 2-4 descriptions ≤90 chars). Warns if headline/description count is below best practice. |
| `draft_callouts` | Create campaign callout assets from 1-25 character text snippets. |
| `draft_structured_snippets` | Create campaign structured snippet assets using official header values and 3-10 snippet values. |
| `draft_image_assets` | Create campaign image assets from local PNG, JPEG, or GIF files. |
| `draft_keywords` | Propose keyword additions with match types. Proactively checks bidding strategy — blocks BROAD match on Manual CPC campaigns. |
| `add_negative_keywords` | Propose negative keywords directly on a campaign |
| `propose_negative_keyword_list` | Draft a shared negative keyword list (SharedSet) and attach it to a campaign — reusable across multiple campaigns |
| `pause_entity` | Pause a campaign, ad group, ad, or keyword |
| `enable_entity` | Re-enable a paused entity |
| `remove_entity` | Permanently remove an entity (irreversible — prefers pause). Supports keywords, negative keywords, ads, ad groups, campaigns. |
| `confirm_and_apply` | Execute a previously previewed change |

### Orchestration Rules

AdLoop ships with orchestration rules that teach the AI *how* to combine these tools — marketing workflows, GAQL syntax, safety protocols, GDPR awareness, and best practices. Without rules, the AI has tools but doesn't know the playbook.

- **Cursor**: `.cursor/rules/adloop.mdc` (canonical source)
- **Claude Code**: `.claude/rules/adloop.md` (synced from Cursor rules via `scripts/sync-rules.py`)

The rules include:
- **Orchestration patterns** for common workflows (performance review, conversion diagnosis, campaign creation, negative keyword hygiene, keyword discovery, tracking validation, budget planning, landing page analysis)
- **GAQL quick reference** with syntax, common queries, and gotchas
- **Safety rules** including Broad Match + Manual CPC prevention and pre-write validation
- **Ad copy character limit guidance** (30-char headlines are shorter than you think)
- **GDPR consent awareness** to prevent false tracking diagnoses in EU markets

### Slash Commands (Claude Code)

AdLoop includes pre-built slash commands in `.claude/commands/` for common workflows:

| Command | What It Does |
|---------|-------------|
| `/analyze-performance` | Full performance review across Google Ads + GA4 |
| `/create-ad` | Create a responsive search ad with safety checks |
| `/diagnose-tracking` | Diagnose tracking and conversion issues |
| `/optimize-campaign` | Full optimization checklist for a campaign |
| `/create-campaign` | Create a new search campaign with budget estimation |
| `/budget-plan` | Estimate budget for keywords via Keyword Planner |

## Safety Model

AdLoop manages real ad spend, so safety is not optional.

- **Two-step writes.** Every mutation returns a preview first. A separate `confirm_and_apply` call is required to execute.
- **Dry-run by default.** Even `confirm_and_apply` defaults to `dry_run=true`. Real changes require explicit `dry_run=false`.
- **Budget caps.** Configurable maximum daily budget — the server rejects anything above the cap.
- **Audit log.** Every operation (including dry runs) is logged to `~/.adloop/audit.log`.
- **New campaigns and ads are PAUSED.** Nothing goes live without manual enablement.
- **Destructive ops require double confirmation.** Removing entities or large budget increases trigger extra warnings.
- **Broad Match + Manual CPC blocked.** The #1 cause of wasted ad spend is automatically prevented — `draft_keywords` refuses to add BROAD match keywords to campaigns without Smart Bidding.
- **Pre-write validation.** Before any write, the AI checks bidding strategy, conversion tracking status, and quality scores. If the campaign is fundamentally broken, AdLoop warns you instead of making things worse.
- **Structured error handling.** All tools return actionable error messages with hints instead of raw exceptions. Auth errors include specific re-authorization steps.
- **API version pinning.** The Google Ads API version is pinned to prevent silent breaking changes from library updates. `health_check` warns when a newer version is available.
- **Ask mode compatibility.** Read tools declare `readOnlyHint` so they work in Cursor's Ask mode without switching to Agent mode.

## Setup

### Quick Start (Recommended)

**Option A — Install from PyPI:**

```bash
pip install adloop
adloop init
```

**Option B — Install from source:**

```bash
git clone https://github.com/kLOsk/adloop.git
cd adloop
uv sync
uv run adloop init
```

The `adloop init` wizard walks you through everything. AdLoop ships with built-in Google OAuth credentials, so you don't need to create a Google Cloud project.

> **⚠️ Built-in credentials temporarily unavailable — Google verification pending.**
> Google limits unverified OAuth apps to 100 users. AdLoop has reached that cap while awaiting Google's app verification. Until verification is complete, the built-in credentials will show a **"This app is blocked"** error for new users.
>
> **Workaround:** set up your own Google Cloud project using the [Advanced Setup](#advanced-setup-custom-google-cloud-project) instructions below (takes ~5 minutes). Your own project has no user cap and is the recommended setup path in the meantime.

The wizard:

1. **Developer token** — from your Google Ads MCC ([API Center](https://ads.google.com/aw/apicenter))
2. **MCC Account ID** — your Manager Account ID (top bar in the MCC UI)
3. **OAuth sign-in** — opens a browser to sign in with Google (or prints a URL for headless servers)
4. **Auto-discovers your accounts** — finds your GA4 properties and Ads accounts automatically
5. **Safety defaults** — budget cap and dry-run preference
6. **Editor config snippets** — prints MCP configuration for both Cursor and Claude Code

### Requirements

- Python 3.11+
- A Google Ads account with an MCC (Manager Account)
- A Google Ads Developer Token (see below)

### Google Ads Developer Token

A developer token is **always required** — even when using AdLoop's built-in OAuth credentials. The built-in credentials handle Google sign-in; the developer token is a separate key that grants API access to your Google Ads data.

1. **Create an MCC** (free) at [ads.google.com/home/tools/manager-accounts](https://ads.google.com/home/tools/manager-accounts/) if you don't have one. Link your regular Google Ads account to it.
2. In the MCC, go to **Tools & Settings → API Center**
3. Your **developer token** is shown there. Copy it — the wizard will ask for it.

**Access levels** — your token's access level determines what it can do:

| Level | How to Get | What It Allows |
|-------|-----------|----------------|
| **Test Account** | Default for new tokens | Can only access test accounts — **not production accounts**. If you see `DEVELOPER_TOKEN_NOT_APPROVED`, this is why. |
| **Explorer** | Automatic after first API call with a production account | 2,880 operations/day on production accounts. Enough to get started. |
| **Basic** | [Apply through API Center](https://ads.google.com/aw/apicenter) | 15,000 operations/day. Apply if you need more. |

> **Getting `DEVELOPER_TOKEN_NOT_APPROVED`?** Your token is at "Test Account" level. Go to [API Center](https://ads.google.com/aw/apicenter) in your MCC and check your access level. If it shows "Test Account", you need to apply for Basic access or wait for Explorer access to be granted after your first production API call.

### Headless Servers

Running on a server without a browser (VMs, Docker, SSH)? The wizard automatically detects this and falls back to a manual flow: it prints an authorization URL you can open on any device, then you paste the redirect URL back into the terminal.

### Advanced Setup (Custom Google Cloud Project)

<details>
<summary>Click to expand — only needed if you want to use your own GCP project instead of AdLoop's built-in credentials</summary>

When you run `adloop init`, choose "No" when asked about built-in credentials. The wizard will guide you through:

#### Step 1 — Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a new project
2. Enable these three APIs (search for each in the API Library):
   - **Google Analytics Data API** — for GA4 reports and events
   - **Google Analytics Admin API** — for listing GA4 properties
   - **Google Ads API** — for all ads operations

#### Step 2 — OAuth Credentials

1. In your Google Cloud project, go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Select **Desktop app** as the application type, give it any name
4. Download the JSON file and save it as `~/.adloop/credentials.json`

> Service accounts are also supported — just place the service account key JSON at the same `credentials_path`. AdLoop detects the file type automatically.

#### Step 3 — Connect to Your Editor

**Cursor** — Add to your project's `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "adloop": {
      "command": "/absolute/path/to/adloop/.venv/bin/python",
      "args": ["-m", "adloop"]
    }
  }
}
```

Then copy `.cursor/rules/adloop.mdc` from this repo into your project's `.cursor/rules/` directory.

**Claude Code** — Run:

```bash
claude mcp add --transport stdio adloop -- /absolute/path/to/adloop/.venv/bin/python -m adloop
```

Or add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "adloop": {
      "command": "/absolute/path/to/adloop/.venv/bin/python",
      "args": ["-m", "adloop"]
    }
  }
}
```

Then install the orchestration rules + slash commands globally so every Claude Code session inherits them:

```bash
adloop install-rules
```

This writes a managed block to `~/.claude/CLAUDE.md` and copies the slash commands (prefixed `adloop-*`) into `~/.claude/commands/`. The block is delimited by sentinel comments so it's safe to run multiple times — re-running just refreshes the content. Two install modes:

- **inline** (default) — full rules embedded in `~/.claude/CLAUDE.md`. Reliable but adds ~10K tokens to every Claude Code session.
- **lazy** (`adloop install-rules --lazy`) — small directive in `CLAUDE.md` pointing at `~/.claude/rules/adloop.md`. Cheaper baseline cost; the LLM reads the rules file only when AdLoop tools are in scope.

To refresh after upgrading AdLoop: `adloop update-rules`. To remove cleanly: `adloop uninstall-rules` — only the managed block and `adloop-*` commands are touched, never your own content.

If you'd rather manage things by hand instead, copy `.claude/rules/adloop.md` and `.claude/commands/` from this repo into your project's `.claude/` directory.

**Claude Desktop / claude.ai** has no programmatic rules location. Run `adloop install-rules` and it will print the rules content for you to paste into Project settings → Custom instructions on claude.ai.

</details>

### Use It

Ask your AI assistant things like:

- *"How are my Google Ads campaigns performing this month?"*
- *"Which search terms are wasting budget? Add them as negative keywords."*
- *"My sign-up conversions dropped — check GA4 and Ads to find out why."*
- *"Draft a new responsive search ad for my main campaign."*
- *"Which landing pages get paid traffic but don't convert?"*
- *"Is my tracking set up correctly? Compare my codebase events against GA4."*
- *"What keywords should I target for [product]? Find ideas and estimate the budget."*
- *"How much budget would I need for these keywords in Germany?"*
- *"Create a new search campaign for [product feature] with a €20/day budget."*

## Configuration Reference

All configuration lives in `~/.adloop/config.yaml`. See [`config.yaml.example`](config.yaml.example) for a documented template.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `google` | `project_id` | *(empty)* | Google Cloud project ID (only needed with custom credentials) |
| `google` | `credentials_path` | *(empty — uses built-in)* | Path to OAuth client JSON or service account key. Leave empty to use AdLoop's built-in credentials. |
| `google` | `token_path` | `~/.adloop/token.json` | Where to store the OAuth token (auto-created) |
| `ga4` | `property_id` | — | Your GA4 property ID (auto-discovered by `adloop init`) |
| `ads` | `developer_token` | — | Your Google Ads API developer token |
| `ads` | `customer_id` | — | Default Google Ads customer ID (auto-discovered by `adloop init`) |
| `ads` | `login_customer_id` | — | Your MCC account ID |
| `safety` | `max_daily_budget` | `50.00` | Maximum allowed daily budget per campaign |
| `safety` | `require_dry_run` | `true` | Force all writes to dry-run mode |
| `safety` | `blocked_operations` | `[]` | Operations to block entirely |

## Project Structure

```
src/adloop/
├── __init__.py        # Entry point — routes 'adloop init' to wizard, otherwise starts MCP server
├── server.py          # FastMCP server — 43 tool registrations with safety annotations
├── config.py          # Config loader (~/.adloop/config.yaml)
├── auth.py            # OAuth 2.0 flow (bundled + custom credentials, headless fallback) + service accounts
├── cli.py             # Interactive 'adloop init' setup wizard
├── crossref.py        # Cross-reference tools (GA4 + Ads combined analysis)
├── tracking.py        # Tracking validation + code generation tools
├── ga4/
│   ├── client.py      # GA4 Data + Admin API clients
│   ├── reports.py     # Account summaries, reports, realtime
│   └── tracking.py    # Event discovery
├── ads/
│   ├── client.py      # Google Ads API client (version-pinned) + retry/backoff for rate limits
│   ├── gaql.py        # GAQL query execution with human-readable error parsing
│   ├── read.py        # Campaign, ad, keyword, search term, negative keyword, shared sets, recommendations, audience reads
│   ├── pmax.py        # Performance Max tools — campaign/asset group performance, asset labels, top combinations
│   ├── write.py       # Draft campaign, RSA, keywords; pause, enable, remove, confirm
│   └── forecast.py    # Budget estimation + keyword discovery via Keyword Planner API
└── safety/
    ├── guards.py      # Budget caps, bid limits, blocked operations, Broad Match safety
    ├── preview.py     # Change plans and previews
    └── audit.py       # Mutation audit logging
```

## Roadmap

What's been shipped and what's next:

- ~~GA4 read tools~~ ✓
- ~~Google Ads read + write tools with safety layer~~ ✓
- ~~Cross-reference intelligence (campaign→conversion mapping, landing page analysis, attribution comparison)~~ ✓
- ~~Tracking utilities (validate events against GA4, generate gtag code)~~ ✓
- ~~Budget estimation + keyword discovery via Keyword Planner~~ ✓
- ~~Shared negative keyword lists (SharedSet API)~~ ✓
- ~~Retry/backoff for API rate limits~~ ✓
- ~~Setup wizard (`adloop init`)~~ ✓
- ~~Claude Code support~~ ✓ — `CLAUDE.md`, `.mcp.json`, `.claude/rules/`, `.claude/commands/`, CLI wizard snippets
- **Claude Desktop one-click install** — `adloop install claude-desktop` (and/or a `.dxt` extension bundle) that writes the AdLoop MCP entry into `claude_desktop_config.json` automatically, so Claude Desktop + Cowork users don't have to hand-edit JSON
- ~~PyPI package~~ ✓ — `pip install adloop`
- ~~Bundled OAuth credentials~~ ✓ — no Google Cloud project required, auto-discovery of GA4/Ads accounts (currently capped at 100 users pending Google verification — use [Advanced Setup](#advanced-setup-custom-google-cloud-project) in the meantime)
- ~~Headless server support~~ ✓ — manual URL copy-paste flow for servers without a browser
- ~~Behavioral eval suites~~ ✓ — 28 prompt-and-expectation tests covering read, write, tracking, and planning workflows
- **Community launch** — HN, Indie Hackers, r/cursor, Twitter
- **Video walkthrough**

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. The short version: open an issue describing your situation first, then submit a PR if you want to build it.

## License

MIT — see [LICENSE](LICENSE).

## Privacy

AdLoop runs entirely on your machine. No data is collected, stored, or transmitted to any server. See [PRIVACY.md](PRIVACY.md) for the full privacy policy.

---

<div align="center">

**If AdLoop saves you from switching between Google Ads, GA4, and your code editor — [give it a star](https://github.com/kLOsk/adloop).**

Made by [@kLOsk](https://github.com/kLOsk) | [Privacy Policy](PRIVACY.md)

</div>
