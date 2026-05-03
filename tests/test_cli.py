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

    def test_no_credentials_path_uses_bundled_comment(self):
        text = self._generate(credentials_path="")
        assert "Using built-in credentials" in text
        parsed = yaml.safe_load(text)
        assert "credentials_path" not in parsed.get("google", {})