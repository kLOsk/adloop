"""Tests for the `adloop init` wizard helpers in `adloop.cli`."""

from __future__ import annotations

import yaml

from adloop.cli import _generate_config_yaml


class TestGenerateConfigYaml:
    """YAML generation in the init wizard must produce parseable output."""

    def _generate(self, **overrides):
        defaults = {
            "project_id": "",
            "credentials_path": "",
            "property_id": "123456789",
            "developer_token": "abc123",
            "customer_id": "123-456-7890",
            "login_customer_id": "987-654-3210",
            "max_daily_budget": 50.0,
            "require_dry_run": True,
        }
        defaults.update(overrides)
        return _generate_config_yaml(**defaults)

    def test_windows_credentials_path_parses(self):
        """Regression for Windows backslash paths breaking YAML parsing.

        Previously the wizard wrote `credentials_path: "c:\\Users\\..."` which
        YAML interpreted as a `\\U` Unicode escape sequence and raised
        ScannerError. Single quotes treat backslashes literally.
        """
        win_path = r"c:\Users\user\.adloop\credentials.json"
        text = self._generate(credentials_path=win_path)
        parsed = yaml.safe_load(text)
        assert parsed["google"]["credentials_path"] == win_path

    def test_posix_credentials_path_parses(self):
        posix_path = "/home/user/.adloop/credentials.json"
        text = self._generate(credentials_path=posix_path)
        parsed = yaml.safe_load(text)
        assert parsed["google"]["credentials_path"] == posix_path

    def test_path_with_embedded_apostrophe_parses(self):
        """YAML single-quoted strings escape `'` by doubling — make sure we do."""
        weird_path = r"c:\Users\o'brien\.adloop\credentials.json"
        text = self._generate(credentials_path=weird_path)
        parsed = yaml.safe_load(text)
        assert parsed["google"]["credentials_path"] == weird_path

    def test_no_credentials_path_comment(self):
        text = self._generate(credentials_path="")
        assert "credentials_path resolved from" in text
        parsed = yaml.safe_load(text)
        assert "credentials_path" not in parsed.get("google", {})

class TestToolsetSnippets:
    """MCP client snippets must carry ADLOOP_TOOLSETS when a subset is chosen."""

    def test_json_snippets_without_toolsets_have_no_env(self):
        import json

        from adloop.cli import _generate_claude_json_snippet, _generate_cursor_snippet

        for snippet in (_generate_cursor_snippet(), _generate_claude_json_snippet()):
            parsed = json.loads(snippet)
            assert "env" not in parsed["mcpServers"]["adloop"]

    def test_json_snippets_with_toolsets_are_valid_and_carry_env(self):
        import json

        from adloop.cli import _generate_claude_json_snippet, _generate_cursor_snippet

        for snippet in (
            _generate_cursor_snippet("ads,ga4"),
            _generate_claude_json_snippet("ads,ga4"),
        ):
            parsed = json.loads(snippet)
            assert parsed["mcpServers"]["adloop"]["env"] == {
                "ADLOOP_TOOLSETS": "ads,ga4"
            }

    def test_claude_code_command_injects_env_before_the_separator(self):
        from adloop.cli import _generate_claude_code_snippet

        plain = _generate_claude_code_snippet()
        assert "ADLOOP_TOOLSETS" not in plain

        cmd = _generate_claude_code_snippet("ads,ga4")
        assert "--env ADLOOP_TOOLSETS=ads,ga4" in cmd
        assert cmd.index("--env") < cmd.index(" -- ")


class TestPromptToolsets:
    def test_default_is_the_full_catalog(self, monkeypatch):
        from adloop import cli

        answers = iter([""])  # Enter on "Expose the full toolset?" [Y/n]
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        assert cli._prompt_toolsets() == ""

    def test_invalid_slugs_reprompt_then_normalize(self, monkeypatch):
        from adloop import cli

        answers = iter(["n", "ads, bogus", "ADS, ga4, ads"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        # Deduped, lowercased, comma-joined — the exact env-var format.
        assert cli._prompt_toolsets() == "ads,ga4"


class TestGenerateConfigYamlOptionalServices:
    def test_optional_sections_parse_with_values(self):
        content = _generate_config_yaml(
            project_id="",
            credentials_path="",
            property_id="123",
            developer_token="tok",
            customer_id="111-222-3333",
            login_customer_id="999-888-7777",
            max_daily_budget=50.0,
            require_dry_run=True,
            gsc_site_url="sc-domain:example.com",
            gtm_account_id="6001",
            gtm_container_id="7001",
            pagespeed_api_key="AIzaKey",
        )
        parsed = yaml.safe_load(content)
        assert parsed["gsc"]["site_url"] == "sc-domain:example.com"
        assert parsed["gtm"] == {"account_id": "6001", "container_id": "7001"}
        assert parsed["pagespeed"]["api_key"] == "AIzaKey"

    def test_optional_sections_default_to_empty_but_present(self):
        content = _generate_config_yaml(
            project_id="",
            credentials_path="",
            property_id="123",
            developer_token="tok",
            customer_id="111-222-3333",
            login_customer_id="999-888-7777",
            max_daily_budget=50.0,
            require_dry_run=True,
        )
        parsed = yaml.safe_load(content)
        # Sections always render (mirrors config.yaml.example) so users
        # can fill them in later without guessing key names.
        assert parsed["gsc"]["site_url"] == ""
        assert parsed["gtm"]["container_id"] == ""
        assert parsed["pagespeed"]["api_key"] == ""
        assert parsed["reddit"]["client_id"] == ""
        assert parsed["reddit"]["token_path"] == "~/.adloop/reddit_token.json"

    def test_reddit_section_round_trips_through_load_config(self, tmp_path):
        from adloop.config import load_config

        content = _generate_config_yaml(
            project_id="",
            credentials_path="",
            property_id="123",
            developer_token="tok",
            customer_id="111-222-3333",
            login_customer_id="",
            max_daily_budget=50.0,
            require_dry_run=True,
            reddit_client_id="app-id",
            reddit_client_secret="app-secret",
            reddit_ad_account_id="a2_abc",
            reddit_username="daniel",
        )
        path = tmp_path / "config.yaml"
        path.write_text(content)
        cfg = load_config(str(path))
        assert cfg.reddit.client_id == "app-id"
        assert cfg.reddit.client_secret == "app-secret"
        assert cfg.reddit.ad_account_id == "a2_abc"
        assert cfg.reddit.username == "daniel"
        assert cfg.reddit.token_path == "~/.adloop/reddit_token.json"


class TestWizardOptionalServiceSteps:
    def test_reddit_step_declined_keeps_existing_values(self, monkeypatch):
        from adloop import cli

        answers = iter(["n"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        existing = {"client_id": "kept-id", "client_secret": "kept-secret", "ad_account_id": "a2_kept", "username": "u"}
        result = cli._wizard_reddit_step(lambda section, key, fallback="": existing.get(key, ""))
        assert result == existing

    def test_reddit_step_authorizes_and_picks_single_account(self, monkeypatch):
        from adloop import cli
        from adloop.reddit import auth as reddit_auth, read as reddit_read

        authorized = {}
        monkeypatch.setattr(
            reddit_auth, "run_local_authorization",
            lambda cfg, **kw: authorized.update(client_id=cfg.reddit.client_id) or {"refresh_token": "rt"},
        )
        monkeypatch.setattr(
            reddit_read, "list_reddit_accounts",
            lambda cfg: {"accounts": [{"ad_account_id": "a2_one", "name": "Acme", "currency": "EUR"}]},
        )
        answers = iter(["y", "app-id", "app-secret", "daniel", "y"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        result = cli._wizard_reddit_step(lambda *a, **k: "")
        assert authorized == {"client_id": "app-id"}
        assert result == {
            "client_id": "app-id", "client_secret": "app-secret",
            "ad_account_id": "a2_one", "username": "daniel",
        }

    def test_reddit_step_survives_failed_authorization(self, monkeypatch):
        from adloop import cli
        from adloop.reddit import auth as reddit_auth

        def boom(cfg, **kw):
            raise reddit_auth.RedditAuthError("denied", error_code="access_denied")

        monkeypatch.setattr(reddit_auth, "run_local_authorization", boom)
        answers = iter(["y", "app-id", "app-secret", ""])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        result = cli._wizard_reddit_step(lambda *a, **k: "")
        assert result["client_id"] == "app-id"
        assert result["ad_account_id"] == ""

    def test_gsc_step_declined_keeps_existing_value(self, monkeypatch):
        from adloop import cli

        answers = iter(["n"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        result = cli._wizard_gsc_step(
            True, lambda section, key, fallback="": "sc-domain:kept.com"
        )
        assert result == "sc-domain:kept.com"

    def test_gsc_step_discovers_and_picks_from_choices(self, monkeypatch):
        from adloop import cli
        from adloop.gsc import reports

        monkeypatch.setattr(
            reports,
            "list_gsc_sites",
            lambda cfg: {
                "sites": [
                    {"site_url": "https://a.com/", "permission_level": "siteOwner"},
                    {"site_url": "sc-domain:b.com", "permission_level": "siteFullUser"},
                ]
            },
        )
        answers = iter(["y", "2"])  # configure? yes → pick the second site
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        result = cli._wizard_gsc_step(True, lambda *a, **k: "")
        assert result == "sc-domain:b.com"

    def test_gtm_step_auto_picks_single_account_and_web_container(self, monkeypatch):
        from adloop import cli
        from adloop.gtm import read as gtm_read

        monkeypatch.setattr(
            gtm_read,
            "list_accounts",
            lambda cfg: {"accounts": [{"account_id": "6001", "name": "Main"}]},
        )
        monkeypatch.setattr(
            gtm_read,
            "list_containers",
            lambda cfg, *, account_id: {
                "containers": [
                    {
                        "container_id": "7001",
                        "public_id": "GTM-ABC1234",
                        "name": "Web",
                        "usage_context": ["web"],
                    },
                    {
                        "container_id": "7002",
                        "public_id": "GTM-XYZ9876",
                        "name": "App",
                        "usage_context": ["android"],
                    },
                ]
            },
        )
        answers = iter(["y"])  # configure? yes → everything else auto-picked
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        assert cli._wizard_gtm_step(True, lambda *a, **k: "") == ("6001", "7001")
