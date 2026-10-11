"""Missing-default errors point to where the user can actually fix them."""

import pytest

from adloop import runtime
from adloop.config import AdLoopConfig


@pytest.fixture
def hosted():
    runtime.set_deployment_mode("server")
    yield
    runtime.set_deployment_mode("local")


def test_self_hosted_hint_names_the_config_key():
    hint = runtime.default_setting_hint("gsc.site_url", "Settings → Google & accounts")
    assert "gsc.site_url" in hint and "config.yaml" in hint


def test_hosted_hint_names_the_dashboard_and_never_the_config_file(hosted):
    hint = runtime.default_setting_hint("gsc.site_url", "Settings → Google & accounts")
    assert "Settings → Google & accounts" in hint and "AdLoop Cloud dashboard" in hint
    assert "config" not in hint


def test_search_console_without_a_site_explains_the_hosted_fix(hosted):
    from adloop.gsc.reports import run_gsc_report

    result = run_gsc_report(AdLoopConfig(), site_url="")

    assert "list_gsc_sites" in result["error"]
    assert "AdLoop Cloud dashboard" in result["error"]
    assert "config.yaml" not in result["error"]


def test_reddit_without_an_account_explains_the_hosted_fix(hosted):
    from adloop.reddit.read import resolve_account

    with pytest.raises(ValueError) as excinfo:
        resolve_account(AdLoopConfig(), "")

    assert "Settings → Reddit Ads" in str(excinfo.value)
    assert "in the config" not in str(excinfo.value)
