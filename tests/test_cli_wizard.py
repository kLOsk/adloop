"""The setup wizard: Google is optional, a workspace can run on Reddit alone."""

import builtins

import yaml

from adloop import cli


def test_reddit_only_setup_skips_every_google_step(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ADLOOP_DIR", tmp_path)
    monkeypatch.setattr(cli, "_CONFIG_PATH", tmp_path / "config.yaml")
    asked = []

    def prompt_bool(label, default=True):
        asked.append(label)
        return False if label == "Set up Google?" else default

    monkeypatch.setattr(cli, "_prompt_bool", prompt_bool)
    monkeypatch.setattr(cli, "_prompt", lambda label, default="", required=True: default)
    monkeypatch.setattr(cli, "_prompt_toolsets", lambda: None)

    def no_input(prompt=""):
        if "completed the steps above" in prompt:
            raise AssertionError("the Google Cloud instructions must not be shown")
        return ""

    monkeypatch.setattr(builtins, "input", no_input)
    seen = {}

    def reddit_step(_existing, required=False):
        seen["required"] = required
        return {"client_id": "app", "client_secret": "secret", "ad_account_id": "a2_acct", "username": "shop"}

    monkeypatch.setattr(cli, "_wizard_reddit_step", reddit_step)
    monkeypatch.setattr(cli, "_wizard_google_discovery", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Google sign-in")))

    cli.run_init_wizard()

    assert seen["required"] is True
    assert not any("Tag Manager container" in label for label in asked)
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["reddit"]["client_id"] == "app" and config["reddit"]["ad_account_id"] == "a2_acct"
    assert not config["google"].get("credentials_path")
    assert not config["ads"].get("customer_id")


def test_google_setup_still_runs_its_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ADLOOP_DIR", tmp_path)
    monkeypatch.setattr(cli, "_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(cli, "_prompt_bool", lambda label, default=True: True if label == "Set up Google?" else default)
    answers = {"Google Cloud Project ID": "my-project"}
    monkeypatch.setattr(cli, "_prompt", lambda label, default="", required=True: answers.get(label, default))
    monkeypatch.setattr(cli, "_prompt_credentials_path", lambda default="": str(tmp_path / "credentials.json"))
    monkeypatch.setattr(cli, "_prompt_customer_id", lambda label, default="", required=True: "")
    monkeypatch.setattr(cli, "_prompt_toolsets", lambda: None)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    monkeypatch.setattr(cli, "_wizard_google_discovery", lambda step, _e: (True, "properties/42", "1234567890", step + 1))
    monkeypatch.setattr(cli, "_wizard_gtm_step", lambda ok, _e: ("", ""))
    monkeypatch.setattr(cli, "_wizard_gsc_step", lambda ok, _e: "")
    seen = {}

    def reddit_step(_existing, required=False):
        seen["required"] = required
        return {"client_id": "", "client_secret": "", "ad_account_id": "", "username": ""}

    monkeypatch.setattr(cli, "_wizard_reddit_step", reddit_step)

    cli.run_init_wizard()

    assert seen["required"] is False
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["google"]["project_id"] == "my-project"
    assert config["ads"]["customer_id"] == "1234567890"
    assert config["ga4"]["property_id"] == "properties/42"


def test_tests_never_see_the_real_home(isolated_home):
    """conftest gives every test its own home (#113): the wizard once installed
    the rules into the developer's real ~/.claude/CLAUDE.md."""
    from pathlib import Path

    assert Path.home() == isolated_home
    assert cli._ADLOOP_DIR == isolated_home / ".adloop"


def test_the_wizard_installs_lazy_rules_by_default(isolated_home, monkeypatch):
    (isolated_home / ".claude").mkdir()
    monkeypatch.setattr(cli, "_prompt_bool", lambda label, default=True: default)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")

    cli._maybe_offer_global_rules_install()

    block = (isolated_home / ".claude" / "CLAUDE.md").read_text()
    assert "~/.adloop/rules/adloop.md" in block and len(block) < 2000
    assert (isolated_home / ".adloop" / "rules" / "adloop.md").exists()
    assert not (isolated_home / ".claude" / "rules").exists()
