# AdLoop

MCP server connecting Google Ads + GA4 into one AI-driven feedback loop inside your IDE.

## Quick Reference

```bash
uv sync                  # Install dependencies
uv run adloop init       # Interactive setup wizard
uv run adloop            # Start MCP server
pytest                   # Run tests
python scripts/sync-rules.py  # Sync rules: .cursor/rules/ -> .claude/rules/
```

## Architecture

```
src/adloop/
├── __init__.py        # Entry point — routes 'adloop init' vs MCP server
├── server.py          # FastMCP server — 43 tool registrations
├── config.py          # Config loader (~/.adloop/config.yaml)
├── auth.py            # OAuth 2.0 (bundled + custom creds, headless fallback) + service accounts
├── cli.py             # Interactive setup wizard (auto-discovery, bundled/custom mode fork)
├── bundled_credentials.json  # Built-in OAuth client for zero-GCP setup
├── crossref.py        # Cross-reference tools (GA4 + Ads combined)
├── tracking.py        # Tracking validation + code generation
├── ga4/               # GA4 Data + Admin API (reports, realtime, events)
├── ads/               # Google Ads API (read, write, GAQL, forecasting, PMax, recommendations)
└── safety/            # Guards, previews, audit logging
```

## Orchestration Rules

All tool usage rules, safety protocols, orchestration patterns, GAQL reference, GDPR awareness, and marketing best practices live in a single canonical file:

**Read and follow `.claude/rules/adloop.md` for all AdLoop MCP tool orchestration.**

That file is the complete guide for combining AdLoop's 43 tools. It covers:
- Tool inventory with parameters and when to use each
- 9 safety rules (budget caps, dry-run defaults, Broad Match prevention, pre-write validation)
- 16 orchestration patterns (performance review, PMax analysis, recommendations, ad creation, tracking diagnosis, etc.)
- GAQL quick reference with syntax, common queries, and gotchas
- GDPR consent awareness for EU markets
- Ad copy character limits and marketing best practices

## Safety Model (Summary)

- Two-step writes: draft -> preview -> confirm_and_apply
- dry_run=true by default; require_dry_run in config overrides
- Budget caps enforced; new campaigns/ads created as PAUSED
- Broad Match + Manual CPC automatically blocked
- All mutations logged to ~/.adloop/audit.log

## Key Files

| File | Purpose |
|------|---------|
| `.claude/rules/adloop.md` | Orchestration rules (synced from .cursor/rules/adloop.mdc) |
| `.claude/commands/` | Slash commands for common workflows |
| `.mcp.json` | MCP auto-discovery for this project |
| `.cursor/rules/adloop.mdc` | Canonical rules source (Cursor format) |
| `config.yaml.example` | Documented config template |
| `scripts/sync-rules.py` | Keeps .claude/rules/ in sync with .cursor/rules/ |
