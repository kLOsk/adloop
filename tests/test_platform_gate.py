"""What every platform shares in the write gate: blocked operations, partial applies, labels."""

import json
import re

import pytest

from adloop.ads import write as ads_write
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety.approval import _PLATFORM_LABELS, card_html
from adloop.safety.guards import blocked_operation_error, blocked_plan_refusal, store_draft
from adloop.safety.outcomes import PartialApplyError
from adloop.safety.preview import ChangePlan, get_plan, remove_plan, store_plan


@pytest.fixture
def config(tmp_path):
    return AdLoopConfig(
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False)
    )


def _audit(config) -> list[dict]:
    with open(config.safety.log_file) as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def plan():
    plan = ChangePlan(
        operation="example_pause_entity", entity_type="campaign", entity_id="123",
        changes={"target_status": "PAUSED"},
    )
    store_plan(plan)
    yield plan
    remove_plan(plan.plan_id)


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def execute(_config, plan):
        calls.append(plan.plan_id)
        return {"status": "APPLIED"}

    monkeypatch.setattr(ads_write, "_execute_plan", execute)
    return calls


class TestBlockedOperations:
    def test_draft_refusal_names_the_operation(self, config):
        config.safety.blocked_operations = ["example_pause_entity"]
        assert blocked_operation_error("example_pause_entity", config.safety) == {
            "error": "Operation 'example_pause_entity' is blocked by configuration"
        }
        assert blocked_operation_error("example_enable_entity", config.safety) is None

    def test_store_draft_stores_the_plan_and_adds_the_warnings(self):
        plan = ChangePlan(operation="example_pause_entity", platform="reddit")
        preview = store_draft(plan, warnings=["Check the budget first."])
        assert get_plan(plan.plan_id) is plan
        assert preview["warnings"] == ["Check the budget first."]
        assert preview["platform"] == "reddit"
        assert "warnings" not in store_draft(ChangePlan(operation="example_pause_entity"))
        remove_plan(plan.plan_id)

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_an_operation_blocked_after_the_draft_is_refused(self, config, plan, executed, dry_run):
        config.safety.blocked_operations = ["example_pause_entity"]

        result = ads_write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=dry_run)

        assert result["status"] == "BLOCKED"
        assert "blocked by configuration" in result["error"]
        assert executed == []
        # The plan stays pending: it can still be discarded, or applied once unblocked.
        assert get_plan(plan.plan_id) is not None
        (entry,) = _audit(config)
        assert entry["result"] == "blocked" and entry["dry_run"] is dry_run
        assert entry["operation"] == "example_pause_entity"

    def test_the_refusal_is_none_while_the_operation_is_allowed(self, config, plan):
        assert blocked_plan_refusal(config, plan, dry_run=True) is None


class TestPartialApply:
    @pytest.mark.parametrize(
        ("unknown", "status", "logged"),
        [(False, "PARTIAL_APPLY", "partial_apply"), (True, "UNKNOWN_OUTCOME", "unknown_status")],
    )
    def test_a_partial_apply_retires_the_plan(self, config, plan, monkeypatch, unknown, status, logged):
        def execute(_config, _plan):
            raise PartialApplyError(
                "Created campaign 900; adding its locations failed.",
                created={"campaign_id": "900"}, unknown=unknown,
            )

        monkeypatch.setattr(ads_write, "_execute_plan", execute)

        result = ads_write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=False)

        assert result["status"] == status
        assert result["created"] == {"campaign_id": "900"}
        assert "Created campaign 900" in result["error"]
        assert get_plan(plan.plan_id) is None
        assert _audit(config)[-1]["result"] == logged

    def test_any_other_failure_keeps_the_plan(self, config, plan, monkeypatch):
        def execute(_config, _plan):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(ads_write, "_execute_plan", execute)

        result = ads_write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=False)

        assert result.get("status") not in ("PARTIAL_APPLY", "UNKNOWN_OUTCOME")
        assert get_plan(plan.plan_id) is not None


def test_the_card_labels_every_platform_the_dialog_does():
    html = card_html()
    table = re.search(r"const PLATFORMS = \{(.*?)\};", html).group(1)
    card_keys = set(re.findall(r"(\w+):", table))
    assert set(_PLATFORM_LABELS) <= card_keys
    prefix = re.search(r"replace\(/\^\(([a-z0-9|]+)\)_/", html).group(1).split("|")
    assert {p for p in _PLATFORM_LABELS if p != "google"} <= set(prefix)
