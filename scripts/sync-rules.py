#!/usr/bin/env python3
"""Sync orchestration rules + slash commands into all canonical locations.

Reads ``.cursor/rules/adloop.mdc`` (the canonical, Cursor-flavoured source),
strips Cursor frontmatter, prepends Claude Code frontmatter, and writes the
result to:

  - ``.claude/rules/adloop.md``        — for Claude Code in *this* repo
  - ``src/adloop/rules/adloop.md``     — bundled with the wheel for
                                         ``adloop install-rules`` to install
                                         globally on user machines

Also copies ``.claude/commands/*.md`` into ``src/adloop/rules/commands/`` so
slash commands ship with the package.

Run this after editing ``adloop.mdc`` or any command file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CURSOR_RULES = REPO_ROOT / ".cursor" / "rules" / "adloop.mdc"
CLAUDE_RULES = REPO_ROOT / ".claude" / "rules" / "adloop.md"
CLAUDE_COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"

PACKAGE_RULES_DIR = REPO_ROOT / "src" / "adloop" / "rules"
PACKAGE_RULES = PACKAGE_RULES_DIR / "adloop.md"
PACKAGE_COMMANDS_DIR = PACKAGE_RULES_DIR / "commands"

CLAUDE_FRONTMATTER = """\
---
description: AdLoop MCP orchestration — Google Ads + GA4 + codebase intelligence
---
"""


def extract_body(content: str) -> str:
    """Strip YAML frontmatter (--- ... ---) and return the body."""
    if not content.startswith("---"):
        return content
    end = content.index("---", 3)
    return content[end + 3:].lstrip("\n")


def sync_rules() -> str:
    """Sync the rules content. Returns the rendered Claude-format content."""
    if not CURSOR_RULES.exists():
        raise FileNotFoundError(f"Canonical rules not found: {CURSOR_RULES}")

    body = extract_body(CURSOR_RULES.read_text())
    rendered = CLAUDE_FRONTMATTER + "\n" + body

    CLAUDE_RULES.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_RULES.write_text(rendered)

    PACKAGE_RULES_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_RULES.write_text(rendered)

    return rendered


def sync_commands() -> int:
    """Mirror .claude/commands/*.md into the package. Returns count copied."""
    if not CLAUDE_COMMANDS_DIR.is_dir():
        return 0

    PACKAGE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    # Remove stale commands that no longer exist in the source
    source_names = {p.name for p in CLAUDE_COMMANDS_DIR.glob("*.md")}
    for stale in PACKAGE_COMMANDS_DIR.glob("*.md"):
        if stale.name not in source_names:
            stale.unlink()

    count = 0
    for cmd in CLAUDE_COMMANDS_DIR.glob("*.md"):
        shutil.copy2(cmd, PACKAGE_COMMANDS_DIR / cmd.name)
        count += 1
    return count


def main() -> None:
    sync_rules()
    cmd_count = sync_commands()

    print(f"Synced rules: {CURSOR_RULES.relative_to(REPO_ROOT)}")
    print(f"          -> {CLAUDE_RULES.relative_to(REPO_ROOT)}")
    print(f"          -> {PACKAGE_RULES.relative_to(REPO_ROOT)}")
    print(
        f"Synced {cmd_count} command(s):"
        f" {CLAUDE_COMMANDS_DIR.relative_to(REPO_ROOT)}"
    )
    print(f"            -> {PACKAGE_COMMANDS_DIR.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
