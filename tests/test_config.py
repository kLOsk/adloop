"""Tests for config loading and validation."""

from adloop.config import AdLoopConfig, load_config


class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path):
        config = load_config(str(tmp_path / "nonexistent.yaml"))
        assert isinstance(config, AdLoopConfig)
        assert config.safety.max_daily_budget == 50.0
        assert config.safety.require_dry_run is True

    def test_loads_from_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "safety:\n"
            "  max_daily_budget: 25.0\n"
            "  require_dry_run: false\n"
            "  two_phase_apply: true\n"
            "ads:\n"
            "  customer_id: '123-456-7890'\n"
        )
        config = load_config(str(config_file))
        assert config.safety.max_daily_budget == 25.0
        assert config.safety.require_dry_run is False
        assert config.safety.two_phase_apply is True
        assert config.ads.customer_id == "123-456-7890"

    def test_two_phase_apply_defaults_off(self, tmp_path):
        config = load_config(str(tmp_path / "nonexistent.yaml"))
        assert config.safety.two_phase_apply is False

    def test_missing_sections_use_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ga4:\n  property_id: 'properties/123'\n")
        config = load_config(str(config_file))
        assert config.ga4.property_id == "properties/123"
        assert config.ads.developer_token == ""
        assert config.safety.max_daily_budget == 50.0

    def test_source_path_is_recorded_when_file_exists(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("safety:\n  require_dry_run: false\n")
        config = load_config(str(config_file))
        assert config.source_path == str(config_file)

    def test_source_path_is_recorded_when_file_missing(self, tmp_path):
        missing = tmp_path / "nope.yaml"
        config = load_config(str(missing))
        assert config.source_path == str(missing)

    def test_source_path_expands_tilde_and_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADLOOP_CONFIG", str(tmp_path / "from-env.yaml"))
        config = load_config()
        assert config.source_path == str(tmp_path / "from-env.yaml")


class TestBlankValuesFallBackToDefaults:
    """An explicit empty value must mean "unset", not "use empty" (issue #65)."""

    def _write(self, tmp_path, body):
        path = tmp_path / "config.yaml"
        path.write_text(body)
        return path

    def test_blank_token_path_uses_the_default(self, tmp_path):
        from adloop.config import load_config

        # Path("") is Path("."), the cwd always exists, so adloop read the
        # working directory as a token file and died with a message that
        # pointed nowhere near the config.
        path = self._write(tmp_path, 'google:\n  token_path: ""\n')
        assert load_config(str(path)).google.token_path == "~/.adloop/token.json"

    def test_whitespace_only_token_path_uses_the_default(self, tmp_path):
        from adloop.config import load_config

        path = self._write(tmp_path, 'google:\n  token_path: "   "\n')
        assert load_config(str(path)).google.token_path == "~/.adloop/token.json"

    def test_blank_log_file_uses_the_default(self, tmp_path):
        from adloop.config import load_config

        path = self._write(tmp_path, 'safety:\n  log_file: ""\n')
        assert load_config(str(path)).safety.log_file == "~/.adloop/audit.log"

    def test_a_real_value_still_wins(self, tmp_path):
        from adloop.config import load_config

        path = self._write(tmp_path, 'google:\n  token_path: "/tmp/t.json"\n')
        assert load_config(str(path)).google.token_path == "/tmp/t.json"

    def test_numeric_customer_id_arrives_as_text(self, tmp_path):
        from adloop.config import load_config

        path = self._write(tmp_path, "ads:\n  customer_id: 1234567890\n")
        assert load_config(str(path)).ads.customer_id == "1234567890"
