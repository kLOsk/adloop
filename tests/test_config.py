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
            "ads:\n"
            "  customer_id: '123-456-7890'\n"
        )
        config = load_config(str(config_file))
        assert config.safety.max_daily_budget == 25.0
        assert config.safety.require_dry_run is False
        assert config.ads.customer_id == "123-456-7890"

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
