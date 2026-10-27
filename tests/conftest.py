"""Suite-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Give every test its own empty home directory.

    Code that resolves ``~`` (Path.home, expanduser) must never reach the
    developer's real one: the setup wizard used to install the rules into the
    real ``~/.claude/CLAUDE.md`` (#113), and token, config and audit-log paths
    all live under ``~/.adloop``. HOME covers POSIX, USERPROFILE Windows.
    ``cli._ADLOOP_DIR`` is computed at import time, so it is pointed here too.
    """
    from adloop import cli

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(cli, "_ADLOOP_DIR", home / ".adloop")
    monkeypatch.setattr(cli, "_CONFIG_PATH", home / ".adloop" / "config.yaml")
    return home


@pytest.fixture(autouse=True)
def offline_google_validation(monkeypatch):
    """Keep Google Ads dry runs offline.

    A dry run sends the plan to Google with validate_only=True, which would
    build a real Ads client from the developer's own config and reach the
    network. Tests that exercise that path patch the Ads client themselves
    and restore the real function (see test_validate_only.py).
    """
    from adloop.ads import write

    monkeypatch.setattr(
        write,
        "_validate_with_google",
        lambda _config, _plan: {"validated_calls": 1, "skipped_calls": 0},
    )


@pytest.fixture(autouse=True)
def no_live_gaql(monkeypatch):
    """Fail loudly instead of querying a real Google Ads account.

    Tests that need GAQL rows patch ``adloop.ads.gaql.execute_query``
    themselves, which takes precedence over this default.
    """

    def refuse(*_args, **_kwargs):
        raise RuntimeError("tests must not query Google Ads; patch execute_query")

    monkeypatch.setattr("adloop.ads.gaql.execute_query", refuse)
