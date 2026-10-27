"""Install AdLoop orchestration rules + slash commands into Claude clients.

Public API:

- :func:`detect_clients` — return the Claude installations we can target.
- :func:`install_rules` — write the managed rules block (idempotent).
- :func:`update_rules` — refresh an existing managed block.
- :func:`uninstall_rules` — remove the managed block, leaving the rest of
  the user's CLAUDE.md untouched.

Idempotency is handled by a sentinel-comment block:

    <!-- adloop:rules:start vX.Y.Z -->
    ... managed content ...
    <!-- adloop:rules:end -->

The sentinel includes the AdLoop version that wrote it so we can detect drift
on upgrade. Anything outside the sentinel pair is preserved verbatim.

Two install modes:

- **inline** (default): the full rules text is pasted between the sentinels
  in ``~/.claude/CLAUDE.md``. Reliable, but loaded into every Claude Code
  session in every repository (:func:`rules_token_estimate`, about 30K tokens).
- **lazy**: a small directive is written between the sentinels pointing at
  ``~/.adloop/rules/adloop.md`` (which contains the full rules). The LLM is
  instructed to read the rules file only when AdLoop tools are in scope.
  The file deliberately lives outside ``~/.claude/``: Claude Code loads every
  file in ``~/.claude/rules/`` into every session, which is where lazy mode
  used to write it (#114). Cheaper baseline cost, less reliable.

Slash commands are installed namespaced (``adloop-*``) so they don't collide
with user-defined commands of the same name.

Claude Desktop has no programmatic rules concept — for that client we return
``InstructionResult`` describing what the user must paste into claude.ai
project instructions manually.
"""

from __future__ import annotations

import importlib.resources
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from adloop import __version__

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENTINEL_START_PREFIX = "<!-- adloop:rules:start"
SENTINEL_END = "<!-- adloop:rules:end -->"

# Match any version, so we can find/replace blocks written by older versions.
_SENTINEL_BLOCK_RE = re.compile(
    r"<!-- adloop:rules:start[^>]*-->.*?<!-- adloop:rules:end -->\n?",
    re.DOTALL,
)

InstallMode = Literal["inline", "lazy"]

# Where lazy mode keeps the full rules, relative to the home directory. Not
# under ~/.claude/: Claude Code auto-loads everything in ~/.claude/rules/.
LAZY_RULES_RELATIVE = Path(".adloop") / "rules" / "adloop.md"
# Where lazy mode wrote them before #114. Cleaned up when it is ours.
_LEGACY_LAZY_RULES_RELATIVE = Path(".claude") / "rules" / "adloop.md"
_RULES_HEADING = "# AdLoop — AI Orchestration Rules"

# The directive used in lazy mode. Kept short to minimise baseline token cost.
_LAZY_DIRECTIVE = """\
**AdLoop orchestration**: if MCP tools like `get_campaign_performance`,
`run_gaql`, `draft_responsive_search_ad`, or other AdLoop tools are available
in this session, read `~/.adloop/rules/adloop.md` for the full safety rules,
orchestration patterns, and GAQL reference before using them. Skip this read
if AdLoop is not active in the current session.
"""

# A short prelude printed inline above the rules content.
_INLINE_PRELUDE = """\
The block below contains AdLoop's MCP orchestration guide. It is loaded
automatically because `adloop install-rules` placed it here. To remove or
update, run `adloop uninstall-rules` or `adloop update-rules`.
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ClaudeClient:
    """A detected Claude installation we can target.

    Attributes:
        name: Stable identifier ("claude_code", "claude_desktop").
        display_name: Human-friendly name for prompts.
        rules_target: Path to the markdown file that should contain the
            managed sentinel block. May be ``None`` for clients (like
            Claude Desktop) that have no programmatic rules concept.
        commands_dir: Where to install slash commands. ``None`` if the
            client doesn't support them.
        notes: Any caveats to surface during install (e.g. manual steps).
    """

    name: str
    display_name: str
    rules_target: Path | None
    commands_dir: Path | None = None
    notes: str = ""


@dataclass
class InstallResult:
    """Outcome of an install/update/uninstall operation."""

    client: str
    action: Literal["installed", "updated", "uninstalled", "skipped", "manual"]
    rules_target: Path | None = None
    commands_installed: list[str] = field(default_factory=list)
    commands_removed: list[str] = field(default_factory=list)
    instructions: str = ""  # human-readable next-steps message


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_clients(home: Path | None = None) -> list[ClaudeClient]:
    """Return the Claude installations we can detect on this machine.

    The Cursor client is intentionally never returned — Cursor handles
    workspace rules natively via ``.cursor/rules/`` and needs no global install.
    """
    home = home or Path.home()
    detected: list[ClaudeClient] = []

    # Claude Code (CLI) — presence of ~/.claude/ or ~/.claude/CLAUDE.md.
    claude_code_dir = home / ".claude"
    if claude_code_dir.exists() or _claude_code_binary_present():
        detected.append(
            ClaudeClient(
                name="claude_code",
                display_name="Claude Code (CLI)",
                rules_target=claude_code_dir / "CLAUDE.md",
                commands_dir=claude_code_dir / "commands",
            )
        )

    # Claude Desktop — best-effort detection across platforms. We only surface
    # this client to print manual instructions, since claude.ai has no
    # programmatic rules location we can write to.
    desktop_dirs = [
        home / "Library" / "Application Support" / "Claude",  # macOS
        home / ".config" / "Claude",                          # Linux
        home / "AppData" / "Roaming" / "Claude",              # Windows
    ]
    if any(p.exists() for p in desktop_dirs):
        detected.append(
            ClaudeClient(
                name="claude_desktop",
                display_name="Claude Desktop",
                rules_target=None,
                commands_dir=None,
                notes=(
                    "Claude Desktop has no programmatic rules location. "
                    "Manual paste required — see install output."
                ),
            )
        )

    return detected


def _claude_code_binary_present() -> bool:
    """Return True if the `claude` CLI is on PATH (covers fresh installs)."""
    return shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# Bundled-content access
# ---------------------------------------------------------------------------


def _read_bundled_rules() -> str:
    """Load the bundled rules markdown shipped with the package.

    Returns the file as-is (with its YAML frontmatter intact) since lazy mode
    writes it to ``~/.adloop/rules/adloop.md`` where the frontmatter is
    meaningful. For inline mode, use :func:`_read_bundled_rules_body`.
    """
    ref = importlib.resources.files("adloop.rules").joinpath("adloop.md")
    with importlib.resources.as_file(ref) as p:
        return Path(p).read_text()


def _read_bundled_rules_body() -> str:
    """Like :func:`_read_bundled_rules` but with YAML frontmatter stripped.

    Used when the rules content is embedded inline in ``~/.claude/CLAUDE.md``
    (which is itself a rules file and shouldn't contain nested frontmatter).
    """
    text = _read_bundled_rules()
    if not text.startswith("---"):
        return text
    end = text.index("---", 3)
    return text[end + 3:].lstrip("\n")


def rules_token_estimate() -> int:
    """Rough token cost of the inline rules, measured from the bundled file.

    About four bytes per token, rounded to the nearest thousand, so the
    wizard and README never quote a stale hard-coded figure again.
    """
    size = len(_read_bundled_rules_body().encode("utf-8"))
    return max(1000, round(size / 4 / 1000) * 1000)


def _home_of(rules_target: Path) -> Path:
    """``~/.claude/CLAUDE.md`` -> ``~``."""
    return rules_target.parent.parent


def _remove_if_ours(path: Path) -> None:
    """Delete a rules file AdLoop wrote; never a file the user wrote."""
    if not path.is_file():
        return
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return
    if _RULES_HEADING not in text:
        return
    path.unlink()
    try:
        path.parent.rmdir()  # only if empty
    except OSError:
        pass


def _list_bundled_commands() -> list[tuple[str, str]]:
    """Return ``[(filename, content), ...]`` for every bundled slash command."""
    commands: list[tuple[str, str]] = []
    try:
        cmd_pkg = importlib.resources.files("adloop.rules").joinpath("commands")
    except (FileNotFoundError, ModuleNotFoundError):
        return commands

    with importlib.resources.as_file(cmd_pkg) as p:
        cmd_dir = Path(p)
        if not cmd_dir.is_dir():
            return commands
        for md in sorted(cmd_dir.glob("*.md")):
            commands.append((md.name, md.read_text()))
    return commands


# ---------------------------------------------------------------------------
# Sentinel-block helpers
# ---------------------------------------------------------------------------


def _sentinel_start(version: str = __version__) -> str:
    return f"<!-- adloop:rules:start v{version} -->"


def _build_managed_block(mode: InstallMode, rules_target_path: Path) -> str:
    """Return the full sentinel-bracketed block to write."""
    start = _sentinel_start()
    if mode == "lazy":
        body = _LAZY_DIRECTIVE
    else:
        body = _INLINE_PRELUDE + "\n" + _read_bundled_rules_body()
    return f"{start}\n\n{body}\n\n{SENTINEL_END}\n"


def _replace_or_append_block(existing: str, new_block: str) -> str:
    """Idempotent merge: replace any existing managed block, else append."""
    if _SENTINEL_BLOCK_RE.search(existing):
        return _SENTINEL_BLOCK_RE.sub(new_block, existing, count=1)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing and not existing.endswith("\n\n"):
        existing += "\n"
    return existing + new_block


def _strip_block(existing: str) -> str:
    """Return ``existing`` with the managed block removed (idempotent)."""
    cleaned = _SENTINEL_BLOCK_RE.sub("", existing, count=1)
    # Collapse any triple+ blank lines left behind by the strip.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def install_rules(
    *,
    mode: InstallMode = "inline",
    install_commands: bool = True,
    home: Path | None = None,
) -> list[InstallResult]:
    """Install (or refresh) the rules block on every detected client."""
    return _apply_to_clients(
        clients=detect_clients(home=home),
        mode=mode,
        install_commands=install_commands,
        action="install",
    )


def update_rules(
    *,
    mode: InstallMode | None = None,
    install_commands: bool = True,
    home: Path | None = None,
) -> list[InstallResult]:
    """Refresh the managed block for an already-installed client.

    If ``mode`` is None, preserve whichever mode the existing block uses
    (inline vs lazy detected by the directive content). New installs default
    to inline.
    """
    return _apply_to_clients(
        clients=detect_clients(home=home),
        mode=mode,
        install_commands=install_commands,
        action="update",
    )


def uninstall_rules(
    *,
    remove_commands: bool = True,
    home: Path | None = None,
) -> list[InstallResult]:
    """Remove the managed block + namespaced slash commands from each client.

    Anything outside the sentinel block in CLAUDE.md is preserved verbatim.
    Only ``adloop-*.md`` commands are removed — user-authored commands are
    never touched.
    """
    results: list[InstallResult] = []
    for client in detect_clients(home=home):
        if client.rules_target is None:
            results.append(
                InstallResult(
                    client=client.name,
                    action="manual",
                    instructions=_manual_uninstall_instructions(client),
                )
            )
            continue

        result = InstallResult(client=client.name, action="skipped")
        result.rules_target = client.rules_target

        # Remove sentinel block from CLAUDE.md
        if client.rules_target.exists():
            existing = client.rules_target.read_text()
            cleaned = _strip_block(existing)
            if cleaned != existing:
                if cleaned.strip() == "":
                    client.rules_target.unlink()
                else:
                    client.rules_target.write_text(cleaned)
                result.action = "uninstalled"

        # Remove the lazy-mode rules file, at its current and its old place.
        home = _home_of(client.rules_target)
        _remove_if_ours(home / LAZY_RULES_RELATIVE)
        _remove_if_ours(home / _LEGACY_LAZY_RULES_RELATIVE)

        # Remove namespaced slash commands
        if remove_commands and client.commands_dir and client.commands_dir.exists():
            for stale in client.commands_dir.glob("adloop-*.md"):
                stale.unlink()
                result.commands_removed.append(stale.name)
            if result.commands_removed:
                result.action = "uninstalled"

        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _apply_to_clients(
    *,
    clients: list[ClaudeClient],
    mode: InstallMode | None,
    install_commands: bool,
    action: Literal["install", "update"],
) -> list[InstallResult]:
    results: list[InstallResult] = []
    for client in clients:
        if client.rules_target is None:
            results.append(
                InstallResult(
                    client=client.name,
                    action="manual",
                    instructions=_manual_install_instructions(client, mode or "inline"),
                )
            )
            continue

        rules_target = client.rules_target
        rules_target.parent.mkdir(parents=True, exist_ok=True)

        existing = rules_target.read_text() if rules_target.exists() else ""
        had_block = bool(_SENTINEL_BLOCK_RE.search(existing))

        # Resolve mode: explicit > existing-mode-detection > inline default.
        resolved_mode: InstallMode
        if mode is not None:
            resolved_mode = mode
        elif had_block:
            resolved_mode = "lazy" if _existing_block_is_lazy(existing) else "inline"
        else:
            resolved_mode = "inline"

        # In lazy mode the full rules go to a file Claude Code does not load
        # on its own. A file from an older lazy install under ~/.claude/rules/
        # would be loaded into every session, so it goes either way; inline
        # mode needs no lazy file at all.
        home = _home_of(rules_target)
        _remove_if_ours(home / _LEGACY_LAZY_RULES_RELATIVE)
        lazy_rules_file = home / LAZY_RULES_RELATIVE
        if resolved_mode == "lazy":
            lazy_rules_file.parent.mkdir(parents=True, exist_ok=True)
            lazy_rules_file.write_text(_read_bundled_rules())
        else:
            _remove_if_ours(lazy_rules_file)

        new_block = _build_managed_block(resolved_mode, rules_target)
        merged = _replace_or_append_block(existing, new_block)
        rules_target.write_text(merged)

        result = InstallResult(
            client=client.name,
            action="updated" if had_block else "installed",
            rules_target=rules_target,
        )

        # Slash commands.
        if install_commands and client.commands_dir is not None:
            client.commands_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in _list_bundled_commands():
                target = client.commands_dir / f"adloop-{filename}"
                target.write_text(content)
                result.commands_installed.append(target.name)

        results.append(result)
    return results


def _existing_block_is_lazy(existing: str) -> bool:
    """Heuristic: lazy blocks contain the directive sentence; inline don't."""
    match = _SENTINEL_BLOCK_RE.search(existing)
    if not match:
        return False
    block = match.group(0)
    # Lazy blocks are short and reference the rules file path (the current one,
    # or the ~/.claude/rules/ one older versions wrote).
    mentions_file = "~/.adloop/rules/adloop.md" in block or "~/.claude/rules/adloop.md" in block
    return mentions_file and len(block) < 2000


# ---------------------------------------------------------------------------
# Manual-instruction builders (Claude Desktop)
# ---------------------------------------------------------------------------


def _manual_install_instructions(client: ClaudeClient, mode: InstallMode) -> str:
    if mode == "lazy":
        body = (
            "Lazy mode is not meaningful for Claude Desktop (claude.ai). "
            "Falling back to inline.\n\n"
        )
    else:
        body = ""

    rules = _read_bundled_rules_body()
    return (
        f"{body}"
        f"To use AdLoop's orchestration rules in {client.display_name}, "
        "open your project on https://claude.ai, go to Project settings → "
        "Custom instructions, and paste the contents below.\n\n"
        "Re-paste after every `adloop update-rules` to stay in sync.\n\n"
        "--- BEGIN ADLOOP RULES ---\n"
        f"{rules}\n"
        "--- END ADLOOP RULES ---\n"
    )


def _manual_uninstall_instructions(client: ClaudeClient) -> str:
    return (
        f"To uninstall AdLoop's rules from {client.display_name}, open the "
        "project on https://claude.ai, go to Project settings → Custom "
        "instructions, and remove the AdLoop section."
    )
