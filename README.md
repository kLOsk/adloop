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

## All 33 Tools

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
| `get_negative_keywords` | List existing negative keywords for a campaign or all campaigns |
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
| `add_negative_keywords` | Propose negative keywords to reduce wasted spend |
| `pause_entity` | Pause a campaign, ad group, ad, or keyword |
| `enable_entity` | Re-enable a paused entity |
| `remove_entity` | Permanently remove an entity (irreversible — prefers pause). Supports keywords, negative keywords, ads, ad groups, campaigns. |
| `confirm_and_apply` | Execute a previously previewed change |

### Orchestration Rules

AdLoop ships with orchestration rules that teach the AI *how* to combine these tools — marketing workflows, GAQL syntax, safety protocols, GDPR awareness, and best practices. Without rules, the AI has tools but doesn't know the playbook.

- **Cursor**: `.cursor/rules/adloop.mdc` (canonical source)
- **Claude Code**: `.claude/rules/adloop.md` (synced from Cursor rules via `scripts/sync-rules.py`)

The rules include:
- **Orchestration patterns** for common workflows (performance review, conversion diagnosis, campaign creation, negative keyword hygiene, tracking validation, budget planning, landing page analysis)
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

The `adloop init` wizard walks you through everything:

1. **Google Cloud checklist** with clickable links to each setup page
2. **Credentials** — prompts for your OAuth JSON path, validates the file exists
3. **GA4 Property ID** — validates numeric format
4. **Developer Token** — from your Google Ads MCC API Center
5. **Customer IDs** — auto-formats `1234567890` → `123-456-7890`
6. **Safety defaults** — budget cap and dry-run preference
7. **OAuth authorization** — optionally opens your browser to complete auth immediately
8. **Editor config snippets** — prints MCP configuration for both Cursor and Claude Code

### Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for package management
- A Google Cloud project (free tier works)
- A Google Ads account with an MCC (Manager Account)

### Manual Setup (If Not Using the Wizard)

<details>
<summary>Click to expand manual setup steps</summary>

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

On first run, AdLoop opens a browser window where you sign in with your Google account and grant access. The resulting token is saved to `~/.adloop/token.json` and refreshed automatically.

> Service accounts are also supported — just place the service account key JSON at the same `credentials_path`. AdLoop detects the file type automatically.

#### Step 3 — Google Ads Developer Token

1. **Create an MCC** (free) at [ads.google.com/home/tools/manager-accounts](https://ads.google.com/home/tools/manager-accounts/) if you don't have one. Link your regular Google Ads account to it.
2. In the MCC, go to **Tools & Settings → API Center**
3. Your **developer token** is shown there. Copy it.

Access levels:
- **Explorer** (automatic) — 2,880 operations/day on production accounts. Enough to get started.
- **Basic** (requires application) — 15,000 operations/day. Apply through the same API Center page if you need more.

#### Step 4 — Find Your IDs

| ID | Where to Find It |
|----|-------------------|
| **GA4 Property ID** | GA4 → Admin → Property Settings (numeric, e.g. `123456789`) |
| **Google Ads Customer ID** | Google Ads UI → top bar (e.g. `123-456-7890`) |
| **MCC Account ID** | MCC UI → top bar (e.g. `123-456-7890`) |

#### Step 5 — Install and Configure

```bash
git clone https://github.com/kLOsk/adloop.git
cd adloop
uv sync

mkdir -p ~/.adloop
cp config.yaml.example ~/.adloop/config.yaml
```

Edit `~/.adloop/config.yaml` and fill in the values from the previous steps. See [`config.yaml.example`](config.yaml.example) for a fully documented template.

#### Step 6 — Connect to Your Editor

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

Then copy `.claude/rules/adloop.md` and `.claude/commands/` from this repo into your project for orchestration rules and slash commands.

</details>

### Use It

Ask your AI assistant things like:

- *"How are my Google Ads campaigns performing this month?"*
- *"Which search terms are wasting budget? Add them as negative keywords."*
- *"My sign-up conversions dropped — check GA4 and Ads to find out why."*
- *"Draft a new responsive search ad for my main campaign."*
- *"Which landing pages get paid traffic but don't convert?"*
- *"Is my tracking set up correctly? Compare my codebase events against GA4."*
- *"How much budget would I need for these keywords in Germany?"*
- *"Create a new search campaign for [product feature] with a €20/day budget."*

## Configuration Reference

All configuration lives in `~/.adloop/config.yaml`. See [`config.yaml.example`](config.yaml.example) for a documented template.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `google` | `project_id` | — | Your Google Cloud project ID |
| `google` | `credentials_path` | `~/.adloop/credentials.json` | Path to OAuth client JSON or service account key |
| `google` | `token_path` | `~/.adloop/token.json` | Where to store the OAuth token (auto-created) |
| `ga4` | `property_id` | — | Your GA4 property ID (found in GA4 Admin → Property Settings) |
| `ads` | `developer_token` | — | Your Google Ads API developer token |
| `ads` | `customer_id` | — | Default Google Ads customer ID |
| `ads` | `login_customer_id` | — | Your MCC account ID |
| `safety` | `max_daily_budget` | `50.00` | Maximum allowed daily budget per campaign |
| `safety` | `require_dry_run` | `true` | Force all writes to dry-run mode |
| `safety` | `blocked_operations` | `[]` | Operations to block entirely |

## Project Structure

```
src/adloop/
├── __init__.py        # Entry point — routes 'adloop init' to wizard, otherwise starts MCP server
├── server.py          # FastMCP server — 26 tool registrations with safety annotations
├── config.py          # Config loader (~/.adloop/config.yaml)
├── auth.py            # OAuth 2.0 Desktop flow + service account support + token refresh handling
├── cli.py             # Interactive 'adloop init' setup wizard
├── crossref.py        # Cross-reference tools (GA4 + Ads combined analysis)
├── tracking.py        # Tracking validation + code generation tools
├── ga4/
│   ├── client.py      # GA4 Data + Admin API clients
│   ├── reports.py     # Account summaries, reports, realtime
│   └── tracking.py    # Event discovery
├── ads/
│   ├── client.py      # Google Ads API client (version-pinned)
│   ├── gaql.py        # GAQL query execution with human-readable error parsing
│   ├── read.py        # Campaign, ad, keyword, search term, negative keyword reads
│   ├── write.py       # Draft campaign, RSA, keywords; pause, enable, remove, confirm
│   └── forecast.py    # Budget estimation via Keyword Planner API
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
- ~~Budget estimation via Keyword Planner~~ ✓
- ~~Setup wizard (`adloop init`)~~ ✓
- ~~Claude Code support~~ ✓ — `CLAUDE.md`, `.mcp.json`, `.claude/rules/`, `.claude/commands/`, CLI wizard snippets
- ~~PyPI package~~ ✓ — `pip install adloop`
- **Community launch** — HN, Indie Hackers, r/cursor, Twitter
- **Video walkthrough**

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. The short version: open an issue describing your situation first, then submit a PR if you want to build it.

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**If AdLoop saves you from switching between Google Ads, GA4, and your code editor — [give it a star](https://github.com/kLOsk/adloop).**

Made by [@kLOsk](https://github.com/kLOsk)

</div>
