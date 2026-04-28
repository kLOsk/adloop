"""Tests for adloop.rules_install — global Claude rules installer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from adloop import __version__
from adloop import rules_install


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_claude_code_home(tmp_path: Path) -> Path:
    """Create a fake ~/.claude/ tree to make detect_clients() find Claude Code."""
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _setup_claude_desktop_home(tmp_path: Path) -> Path:
    """Create a fake macOS Claude Desktop dir."""
    (tmp_path / "Library" / "Application Support" / "Claude").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


# ---------------------------------------------------------------------------
# detect_clients
# ---------------------------------------------------------------------------


class TestDetectClients:
    def test_returns_empty_when_nothing_detected(self, tmp_path):
        # Bare home with no Claude dirs.
        result = rules_install.detect_clients(home=tmp_path)
        # Note: claude binary on PATH could still cause detection; allow that.
        assert all(c.name != "claude_desktop" for c in result)

    def test_detects_claude_code_via_directory(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        result = rules_install.detect_clients(home=tmp_path)
        names = [c.name for c in result]
        assert "claude_code" in names
        cc = next(c for c in result if c.name == "claude_code")
        assert cc.rules_target == tmp_path / ".claude" / "CLAUDE.md"
        assert cc.commands_dir == tmp_path / ".claude" / "commands"

    def test_detects_claude_desktop_macos_path(self, tmp_path):
        _setup_claude_desktop_home(tmp_path)
        result = rules_install.detect_clients(home=tmp_path)
        names = [c.name for c in result]
        assert "claude_desktop" in names
        cd = next(c for c in result if c.name == "claude_desktop")
        assert cd.rules_target is None  # manual-only client


# ---------------------------------------------------------------------------
# install_rules — inline mode
# ---------------------------------------------------------------------------


class TestInstallInline:
    def test_creates_claude_md_with_sentinel_block(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        results = rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )

        cc_result = next(r for r in results if r.client == "claude_code")
        assert cc_result.action == "installed"

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        body = claude_md.read_text()
        assert f"<!-- adloop:rules:start v{__version__} -->" in body
        assert "<!-- adloop:rules:end -->" in body
        # Inline mode should embed the actual rules content.
        assert "AdLoop" in body

    def test_preserves_existing_user_content_outside_block(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.write_text(
            "# My personal Claude instructions\n\nDo not use tabs.\n"
        )

        rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )

        body = claude_md.read_text()
        assert "# My personal Claude instructions" in body
        assert "Do not use tabs." in body
        assert "<!-- adloop:rules:start" in body

    def test_idempotent_install_does_not_duplicate_block(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )
        first = (tmp_path / ".claude" / "CLAUDE.md").read_text()

        results = rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )
        second = (tmp_path / ".claude" / "CLAUDE.md").read_text()

        # Exactly one start sentinel either way.
        assert first.count("<!-- adloop:rules:start") == 1
        assert second.count("<!-- adloop:rules:start") == 1

        # Second call should report as 'updated' since a block already exists.
        cc_result = next(r for r in results if r.client == "claude_code")
        assert cc_result.action == "updated"

    def test_inline_strips_yaml_frontmatter(self, tmp_path):
        # ~/.claude/CLAUDE.md is itself a rules file; nested frontmatter would
        # be confusing/invalid. Inline mode must strip it.
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )
        body = (tmp_path / ".claude" / "CLAUDE.md").read_text()
        # Find the managed block and assert it does not contain the frontmatter.
        block = re.search(
            r"<!-- adloop:rules:start.*?<!-- adloop:rules:end -->",
            body,
            re.DOTALL,
        )
        assert block is not None
        assert "description: AdLoop MCP orchestration" not in block.group(0)

    def test_install_commands_creates_namespaced_files(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="inline", install_commands=True, home=tmp_path
        )

        cmd_dir = tmp_path / ".claude" / "commands"
        installed = sorted(p.name for p in cmd_dir.glob("*.md"))
        assert installed, "expected at least one command installed"
        assert all(name.startswith("adloop-") for name in installed)


# ---------------------------------------------------------------------------
# install_rules — lazy mode
# ---------------------------------------------------------------------------


class TestInstallLazy:
    def test_writes_short_directive_in_claude_md(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="lazy", install_commands=False, home=tmp_path
        )

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        body = claude_md.read_text()
        # Lazy directive should be small (sentinels + ~5 lines of prose).
        assert "<!-- adloop:rules:start" in body
        assert "~/.claude/rules/adloop.md" in body
        # Sanity: directive block should be under ~1KB; full rules are >40KB.
        block_match = re.search(
            r"<!-- adloop:rules:start.*?<!-- adloop:rules:end -->",
            body,
            re.DOTALL,
        )
        assert block_match is not None
        assert len(block_match.group(0)) < 2000

    def test_writes_full_rules_to_sibling_file(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="lazy", install_commands=False, home=tmp_path
        )

        rules_file = tmp_path / ".claude" / "rules" / "adloop.md"
        assert rules_file.exists()
        # Should match the bundled rules content (frontmatter intact —
        # this is a Claude rules file, not a CLAUDE.md).
        bundled = rules_install._read_bundled_rules()
        assert rules_file.read_text() == bundled
        assert rules_file.read_text().startswith("---")


# ---------------------------------------------------------------------------
# update_rules
# ---------------------------------------------------------------------------


class TestUpdateRules:
    def test_update_preserves_existing_mode(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        # Install lazy first.
        rules_install.install_rules(
            mode="lazy", install_commands=False, home=tmp_path
        )

        # Update without specifying mode — should stay lazy.
        rules_install.update_rules(install_commands=False, home=tmp_path)

        body = (tmp_path / ".claude" / "CLAUDE.md").read_text()
        assert "~/.claude/rules/adloop.md" in body  # lazy directive marker
        block = re.search(
            r"<!-- adloop:rules:start.*?<!-- adloop:rules:end -->",
            body,
            re.DOTALL,
        )
        assert block is not None
        assert len(block.group(0)) < 2000  # still lazy

    def test_update_can_switch_modes_explicitly(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="lazy", install_commands=False, home=tmp_path
        )

        # Switch to inline.
        rules_install.update_rules(
            mode="inline", install_commands=False, home=tmp_path
        )

        body = (tmp_path / ".claude" / "CLAUDE.md").read_text()
        block = re.search(
            r"<!-- adloop:rules:start.*?<!-- adloop:rules:end -->",
            body,
            re.DOTALL,
        )
        assert block is not None
        assert len(block.group(0)) > 2000  # inline is large


# ---------------------------------------------------------------------------
# uninstall_rules
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_removes_block_but_keeps_other_content(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.write_text("# Mine\n\nKeep me.\n")
        rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )

        rules_install.uninstall_rules(remove_commands=False, home=tmp_path)

        body = claude_md.read_text()
        assert "Keep me." in body
        assert "<!-- adloop:rules:start" not in body
        assert "<!-- adloop:rules:end" not in body

    def test_removes_namespaced_commands_only(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        # User-authored command we must not touch.
        user_cmd = cmd_dir / "my-personal-cmd.md"
        user_cmd.write_text("# my command\n")

        rules_install.install_rules(
            mode="inline", install_commands=True, home=tmp_path
        )
        assert any(p.name.startswith("adloop-") for p in cmd_dir.glob("*.md"))

        rules_install.uninstall_rules(remove_commands=True, home=tmp_path)

        assert user_cmd.exists()  # untouched
        assert not list(cmd_dir.glob("adloop-*.md"))  # ours all gone

    def test_removes_lazy_rules_file(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        rules_install.install_rules(
            mode="lazy", install_commands=False, home=tmp_path
        )
        rules_file = tmp_path / ".claude" / "rules" / "adloop.md"
        assert rules_file.exists()

        rules_install.uninstall_rules(remove_commands=False, home=tmp_path)

        assert not rules_file.exists()

    def test_uninstall_when_block_missing_is_safe_noop(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        # No install — file doesn't even exist.
        results = rules_install.uninstall_rules(home=tmp_path)
        cc = next((r for r in results if r.client == "claude_code"), None)
        assert cc is not None
        assert cc.action == "skipped"

    def test_uninstall_removes_empty_claude_md(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        # Install with no other user content.
        rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert claude_md.exists()

        rules_install.uninstall_rules(remove_commands=False, home=tmp_path)

        # Empty CLAUDE.md should be cleaned up rather than left as a stub.
        assert not claude_md.exists()


# ---------------------------------------------------------------------------
# Sentinel parsing helpers
# ---------------------------------------------------------------------------


class TestSentinelHandling:
    def test_replaces_block_from_older_version(self, tmp_path):
        _setup_claude_code_home(tmp_path)
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        # Pretend a v0.5.0 block exists.
        claude_md.write_text(
            "user content\n\n"
            "<!-- adloop:rules:start v0.5.0 -->\nold body\n<!-- adloop:rules:end -->\n"
        )

        rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )

        body = claude_md.read_text()
        assert "user content" in body
        assert "old body" not in body
        assert f"<!-- adloop:rules:start v{__version__} -->" in body
        assert body.count("<!-- adloop:rules:start") == 1


# ---------------------------------------------------------------------------
# Claude Desktop manual instructions
# ---------------------------------------------------------------------------


class TestClaudeDesktop:
    def test_returns_manual_action_with_full_rules(self, tmp_path):
        _setup_claude_desktop_home(tmp_path)
        # Make sure Claude Code isn't also detected via PATH binary.
        results = rules_install.install_rules(
            mode="inline", install_commands=False, home=tmp_path
        )
        cd = next((r for r in results if r.client == "claude_desktop"), None)
        assert cd is not None
        assert cd.action == "manual"
        assert "claude.ai" in cd.instructions
        assert "BEGIN ADLOOP RULES" in cd.instructions


# ---------------------------------------------------------------------------
# Bundled-content access
# ---------------------------------------------------------------------------


class TestBundledContent:
    def test_bundled_rules_content_is_readable(self):
        body = rules_install._read_bundled_rules()
        assert body
        assert "AdLoop" in body
        # Should contain section headers we know exist.
        assert "Tool Inventory" in body or "Orchestration Patterns" in body

    def test_bundled_commands_present(self):
        cmds = rules_install._list_bundled_commands()
        assert cmds, "expected slash commands to be bundled"
        # We expect at least the canonical ones from .claude/commands/.
        names = [c[0] for c in cmds]
        assert "create-ad.md" in names or "analyze-performance.md" in names


# ---------------------------------------------------------------------------
# CLI entry point smoke
# ---------------------------------------------------------------------------


class TestCliRulesCommand:
    def test_install_via_cli_entry(self, tmp_path, monkeypatch):
        _setup_claude_code_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        from adloop.cli import run_rules_command

        rc = run_rules_command("install-rules", ["--no-commands"])
        assert rc == 0

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        assert "<!-- adloop:rules:start" in claude_md.read_text()

    def test_uninstall_via_cli_entry(self, tmp_path, monkeypatch):
        _setup_claude_code_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        from adloop.cli import run_rules_command

        run_rules_command("install-rules", ["--no-commands"])
        rc = run_rules_command("uninstall-rules", [])
        assert rc == 0

        # CLAUDE.md should be gone (nothing else was in it).
        assert not (tmp_path / ".claude" / "CLAUDE.md").exists()

    def test_unknown_subcommand_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from adloop.cli import run_rules_command

        rc = run_rules_command("not-a-real-cmd", [])
        assert rc == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_install_creates_claude_dir_if_missing(self, tmp_path):
        # Don't pre-create ~/.claude/. detect_clients only fires if the dir
        # OR the binary exists, so simulate the binary path.
        # We test the underlying install path via direct call.
        client = rules_install.ClaudeClient(
            name="claude_code",
            display_name="Claude Code",
            rules_target=tmp_path / ".claude" / "CLAUDE.md",
            commands_dir=tmp_path / ".claude" / "commands",
        )
        results = rules_install._apply_to_clients(
            clients=[client], mode="inline", install_commands=False, action="install"
        )
        assert results[0].action == "installed"
        assert (tmp_path / ".claude" / "CLAUDE.md").exists()

    def test_install_with_no_clients_is_empty_result(self, tmp_path, monkeypatch):
        # Bare home; ensure claude binary not detected.
        monkeypatch.setattr(rules_install, "_claude_code_binary_present", lambda: False)
        results = rules_install.install_rules(home=tmp_path, install_commands=False)
        # No detected clients -> no results.
        assert results == [] or all(r.action == "manual" for r in results)
