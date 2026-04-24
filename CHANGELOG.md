# Changelog

All notable changes to AdLoop will be documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and AdLoop adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.3] — 2026-04-21

### Fixed

- **Server no longer crashes when an MCP host cancels a long-running tool call.** Under Claude Cowork, Claude Code (post-September 2025 update), and any other MCP host that sends `notifications/cancelled`, a cancellation arriving between handler completion and the server's response call would trigger `AssertionError: Request already responded to` in `mcp.shared.session.RequestResponder.respond`, which escaped the anyio TaskGroup and tore down the entire stdio transport. The server process would exit and the host would respawn it, manifesting as "the MCP server just disconnected" mid-session — especially common on large agency MCCs where tools run long enough for the host's per-tool timeout to fire. This is [upstream bug modelcontextprotocol/python-sdk#2416](https://github.com/modelcontextprotocol/python-sdk/issues/2416) and is still open at the time of this release, so AdLoop ships a self-removing runtime patch in `adloop._mcp_patches` that guards both `respond()` and `cancel()` against the double-respond race. The patch detects upstream fixes automatically: as soon as the installed `mcp` version no longer contains the racey `assert not self._completed` in `RequestResponder.respond`, the patch becomes a no-op at startup. **Action for maintainers:** when `mcp` upstream lands the fix (tracked via issue #2416), delete `src/adloop/_mcp_patches.py` and its import in `src/adloop/server.py`.

### Added

- Diagnostic instrumentation (`adloop.diagnostics`) that can be activated with `ADLOOP_DEBUG=1`. Emits structured `[adloop-debug] event=...` lines to stderr covering process start, tool call start/end, periodic heartbeats, signal receipt (SIGTERM/SIGHUP/SIGINT/SIGPIPE), and `atexit`. Designed specifically to distinguish graceful exits from signal-driven kills and silent SIGKILLs when troubleshooting host-side disconnects.

### Changed

- `health_check` is now a single-row probe instead of a full `customer_client` enumeration. On large MCCs (100+ accounts) the old path took multiple seconds and produced a large payload for no diagnostic benefit; the new probe returns in under a second regardless of account count.
- `list_accounts` now accepts a `limit` parameter (default `50`) and returns a truncation note when there are more accounts than the limit. Callers that actually need the full list can raise the limit explicitly; most workflows can pass `customer_id` directly to other tools without enumerating accounts at all.

### Known issues

- `adloop._mcp_patches` is a temporary workaround for upstream python-sdk#2416. Keep watching that issue; once a fixed `mcp` release is out and pinned, remove the patch module and its call site. The patch already skips itself when it detects the fix in the installed source, so leaving it in place after an upstream fix is safe but unnecessary.

## [0.6.2] — 2026-04

### Changed

- Internal refactors and minor documentation updates. See git history for details.

## [0.6.1] — 2026-04

### Changed

- Bundled OAuth credentials hit Google's 100-user audience cap while awaiting verification. README now directs new users to the Advanced Setup (custom GCP project) workaround until verification completes.

<!-- Older entries not backfilled; see git tags/releases for pre-0.6.1 history. -->
