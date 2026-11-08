"""Tests for the GA4 → Google Ads conversion tracking setup."""

from __future__ import annotations

import re
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop import conversion_setup as setup
from adloop.ads import write as ads_write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, GA4Config, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore

CID = "1234567890"
PID = "123456"


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id=CID),
        ga4=GA4Config(property_id=f"properties/{PID}"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False, two_phase_apply=True),
    )


def _action(action_id, event, status="HIDDEN", primary=False, category="SIGNUP"):
    return {
        "id": str(action_id), "name": f"example.com (web) {event}", "status": status, "primary": primary,
        "category": category, "origin": "WEBSITE", "event": event, "property_id": PID,
    }


class World:
    """GA4 and Google Ads as the setup sees them; real applies change it."""

    def __init__(self, *, linked=True, key_events=("sign_up", "purchase"), counts=None, auto_tagging=True,
                 actions=(), goals=None, retention="FOURTEEN_MONTHS"):
        self.linked = linked
        self.key_events = set(key_events)
        self.counts = {"sign_up": 12, "page_view": 400} if counts is None else counts
        self.auto_tagging = auto_tagging
        self.actions = {a["id"]: dict(a) for a in actions}
        self.goals = dict(goals or {})
        self.retention = retention
        self.mutations: list[tuple[str, bool]] = []
        self.fail = set()
        self.admin = self._admin()
        base = GoogleAdsClient(credentials=None, developer_token="test-token", use_proto_plus=True,
                               version=GOOGLE_ADS_API_VERSION)
        self.ads = SimpleNamespace(enums=base.enums, get_type=base.get_type, get_service=self._service)

    # GA4 Admin -------------------------------------------------------------
    def _admin(self):
        admin = MagicMock()
        admin.get_property.return_value = SimpleNamespace(
            display_name="example.com", service_level=SimpleNamespace(name="GOOGLE_ANALYTICS_STANDARD"))
        admin.list_google_ads_links.side_effect = lambda parent: (
            [SimpleNamespace(customer_id=CID)] if self.linked else [])
        admin.list_key_events.side_effect = lambda parent: [SimpleNamespace(event_name=e) for e in self.key_events]
        admin.get_data_retention_settings.side_effect = lambda name: SimpleNamespace(
            event_data_retention=SimpleNamespace(name=self.retention),
            user_data_retention=SimpleNamespace(name="FOURTEEN_MONTHS"),
            reset_user_data_on_new_activity=True,
        )

        def create_link(parent, google_ads_link):
            if "link" in self.fail:
                raise RuntimeError("PERMISSION_DENIED: no admin access to the Google Ads account")
            self.linked = True
            self.mutations.append(("link", False))

        def create_key_event(parent, key_event):
            self.key_events.add(key_event.event_name)
            self.mutations.append((f"key_event:{key_event.event_name}", False))

        def update_retention(data_retention_settings, update_mask):
            self.retention = data_retention_settings.event_data_retention.name
            self.mutations.append(("retention", False))
            return SimpleNamespace(name=data_retention_settings.name,
                                   event_data_retention=data_retention_settings.event_data_retention,
                                   user_data_retention=data_retention_settings.user_data_retention,
                                   reset_user_data_on_new_activity=True)

        admin.create_google_ads_link.side_effect = create_link
        admin.create_key_event.side_effect = create_key_event
        admin.update_data_retention_settings.side_effect = update_retention
        return admin

    # Google Ads ------------------------------------------------------------
    def query(self, config, customer_id, query):
        if "FROM customer_conversion_goal" in query:
            rows = [
                {"customer_conversion_goal.category": c, "customer_conversion_goal.origin": o,
                 "customer_conversion_goal.biddable": b}
                for (c, o), b in self.goals.items()
            ]
            match = re.search(r"category = '(\w+)' AND customer_conversion_goal.origin = '(\w+)'", query)
            return [r for r in rows if not match or (
                r["customer_conversion_goal.category"], r["customer_conversion_goal.origin"]) == match.groups()]
        if "FROM conversion_action" in query:
            match = re.search(r"conversion_action.id = (\d+)", query)
            chosen = [self.actions[match.group(1)]] if match and match.group(1) in self.actions else (
                [] if match else list(self.actions.values()))
            return [
                {
                    "conversion_action.id": a["id"], "conversion_action.name": a["name"],
                    "conversion_action.status": a["status"], "conversion_action.primary_for_goal": a["primary"],
                    "conversion_action.category": a["category"], "conversion_action.origin": a["origin"],
                    "conversion_action.google_analytics_4_settings.event_name": a["event"],
                    "conversion_action.google_analytics_4_settings.property_id": a["property_id"],
                }
                for a in chosen if a["status"] != "REMOVED"
            ]
        if "FROM customer" in query:
            return [{"customer.descriptive_name": "Example Shop", "customer.auto_tagging_enabled": self.auto_tagging}]
        raise AssertionError(query)

    def _service(self, name):
        def mutate(request):
            validate = bool(request.validate_only)
            if name in self.fail:
                raise RuntimeError(f"{name} refused")
            self.mutations.append((name, validate))
            if not validate:
                self._apply(name, request)
            return SimpleNamespace(partial_failure_error=None, results=[])

        method = {
            "CustomerService": "mutate_customer",
            "ConversionActionService": "mutate_conversion_actions",
            "CustomerConversionGoalService": "mutate_customer_conversion_goals",
        }[name]
        return SimpleNamespace(**{method: mutate})

    def _apply(self, name, request):
        if name == "CustomerService":
            self.auto_tagging = request.operation.update.auto_tagging_enabled
        elif name == "ConversionActionService":
            op = request.operations[0]
            action = self.actions[op.update.resource_name.rsplit("/", 1)[-1]]
            if "status" in op.update_mask.paths:
                action["status"] = op.update.status.name
            if "primary_for_goal" in op.update_mask.paths:
                action["primary"] = op.update.primary_for_goal
        elif name == "CustomerConversionGoalService":
            op = request.operations[0]
            category, origin = op.update.resource_name.rsplit("/", 1)[-1].split("~")
            self.goals[(category, origin)] = op.update.biddable

    def report(self, config, **kwargs):
        return {"rows": [{"eventName": e, "eventCount": str(n)} for e, n in self.counts.items()]}

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch("adloop.ga4.client.get_admin_client", return_value=self.admin))
        stack.enter_context(patch("adloop.ads.gaql.execute_query", side_effect=self.query))
        stack.enter_context(patch("adloop.ga4.reports.run_ga4_report", side_effect=self.report))
        stack.enter_context(patch("adloop.ads.client.get_ads_client", return_value=self.ads))
        return stack


def _draft(config, world, **kwargs):
    with world.patches():
        return setup.draft_conversion_tracking_setup(config, customer_id=CID, property_id=PID, **kwargs)


def _confirm(config, world, plan_id, dry_run):
    with world.patches():
        return ads_write.confirm_and_apply(config, plan_id=plan_id, dry_run=dry_run)


def _linked_but_hidden():
    return World(
        actions=[_action(11, "sign_up"), _action(12, "purchase", category="PURCHASE")],
        goals={("SIGNUP", "WEBSITE"): False, ("PURCHASE", "WEBSITE"): False},
    )


def test_hidden_imports_are_imported_and_made_biddable(config):
    preview = _draft(config, _linked_but_hidden())
    assert preview["operation"] == "conversion_tracking_setup"
    assert preview["customer_id"] == CID
    assert preview["changes"]["steps"] == [
        "Google Ads: import 'sign_up' (“example.com (web) sign_up”) as a primary conversion",
        "Google Ads: bid on the “Sign-up” goal in campaigns that use the account's goals",
    ]
    assert "GA4 property linked to 123-456-7890" in preview["changes"]["already_done"]
    # purchase never fired, so the default leaves it out and says so.
    assert any("purchase" in w and "did not fire" in w for w in preview["warnings"])
    assert "actions" not in preview["changes"]


def test_a_fresh_setup_plans_every_missing_step(config):
    world = World(linked=False, key_events=(), auto_tagging=False, retention="TWO_MONTHS")
    preview = _draft(config, world, events=["sign_up"], data_retention="14")
    steps = preview["changes"]["steps"]
    assert steps[0].startswith("Google Analytics: link example.com to Google Ads Example Shop (123-456-7890)")
    assert steps[1].startswith("Google Ads: turn on auto-tagging")
    assert steps[2] == "Google Analytics: mark 'sign_up' as a key event (every event)"
    assert steps[3] == "Google Analytics: keep event data for 14 months instead of 2"
    assert any("Personalized advertising stays off" in w for w in preview["warnings"])
    # Google Ads creates the conversion action later; the plan says so instead of failing.
    assert any(w.startswith("After this: 'sign_up'") for w in preview["warnings"])


def test_everything_in_place_is_no_change(config):
    world = World(actions=[_action(11, "sign_up", status="ENABLED", primary=True)],
                  goals={("SIGNUP", "WEBSITE"): True})
    result = _draft(config, world)
    assert result["status"] == "NO_CHANGE"
    assert "'sign_up' imported as a primary conversion" in result["already_done"]


def test_a_secondary_import_is_promoted(config):
    world = World(actions=[_action(11, "sign_up", status="ENABLED", primary=False)],
                  goals={("SIGNUP", "WEBSITE"): True})
    preview = _draft(config, world)
    assert preview["changes"]["steps"] == [
        "Google Ads: make “example.com (web) sign_up” a primary conversion",
    ]


def test_secondary_on_request_skips_the_goal(config):
    preview = _draft(config, _linked_but_hidden(), primary=False)
    assert preview["changes"]["steps"] == [
        "Google Ads: import 'sign_up' (“example.com (web) sign_up”) as a secondary conversion",
    ]


def test_without_fired_key_events_it_asks_for_events(config):
    world = World(counts={"page_view": 40})
    result = _draft(config, world)
    assert "Name the events" in result["error"]
    assert result["key_events"] == ["purchase", "sign_up"]


def test_bad_event_names_and_shortening_are_refused(config):
    assert _draft(config, World(), events=["sign up"])["error"] == "Validation failed"
    result = _draft(config, World(retention="FOURTEEN_MONTHS"), events=["sign_up"], data_retention=2)
    assert "only lengthens" in result["error"]


def test_dry_run_validates_ads_steps_and_changes_nothing(config):
    world = _linked_but_hidden()
    preview = _draft(config, world)
    dry = _confirm(config, world, preview["plan_id"], True)
    assert dry["status"] == "DRY_RUN_SUCCESS"
    assert dry["checks"]["validated_calls"] == 2
    assert [r["result"] for r in dry["checks"]["steps"]] == ["validated", "validated"]
    assert "re-read" in dry["note"]
    assert world.mutations == [("ConversionActionService", True), ("CustomerConversionGoalService", True)]
    assert world.actions["11"]["status"] == "HIDDEN"


def test_apply_runs_the_steps_in_order(config):
    world = World(linked=False, key_events=(), auto_tagging=False, retention="TWO_MONTHS")
    preview = _draft(config, world, events=["sign_up"], data_retention=14)
    _confirm(config, world, preview["plan_id"], True)
    assert world.mutations == [("CustomerService", True)]  # GA4 steps were only re-read
    applied = _confirm(config, world, preview["plan_id"], False)
    assert applied["status"] == "APPLIED"
    assert [r["result"] for r in applied["result"]["steps"]] == ["done"] * 4
    assert world.linked and world.auto_tagging and "sign_up" in world.key_events
    assert world.retention == "FOURTEEN_MONTHS"
    link = world.admin.create_google_ads_link.call_args.kwargs["google_ads_link"]
    assert link.customer_id == CID and link.ads_personalization_enabled is False


def test_import_apply_enables_the_action_as_primary(config):
    world = _linked_but_hidden()
    preview = _draft(config, world)
    _confirm(config, world, preview["plan_id"], True)
    applied = _confirm(config, world, preview["plan_id"], False)
    assert applied["status"] == "APPLIED"
    assert world.actions["11"]["status"] == "ENABLED" and world.actions["11"]["primary"] is True
    assert world.goals[("SIGNUP", "WEBSITE")] is True
    assert world.actions["12"]["status"] == "HIDDEN"
    assert "check_key_event_imports" in applied["result"]["next_step"]


def test_a_failed_step_keeps_the_plan_and_a_retry_skips_finished_steps(config):
    world = _linked_but_hidden()
    preview = _draft(config, world)
    _confirm(config, world, preview["plan_id"], True)
    world.fail.add("CustomerConversionGoalService")
    failed = _confirm(config, world, preview["plan_id"], False)
    assert "Step 2 of 2 failed" in failed["error"]
    assert "Confirming the plan again continues" in failed["error"]
    assert world.actions["11"]["status"] == "ENABLED"

    world.fail.clear()
    retried = _confirm(config, world, preview["plan_id"], False)
    assert retried["status"] == "APPLIED"
    assert [r["result"] for r in retried["result"]["steps"]] == ["already done", "done"]


def test_a_failing_ga4_link_is_reported_with_its_cause(config):
    world = World(linked=False)
    world.actions = {"11": _action(11, "sign_up", status="ENABLED", primary=True)}
    world.goals = {("SIGNUP", "WEBSITE"): True}
    preview = _draft(config, world)
    _confirm(config, world, preview["plan_id"], True)
    world.fail.add("link")
    failed = _confirm(config, world, preview["plan_id"], False)
    assert "Step 1 of 1 failed" in failed["error"] and "PERMISSION_DENIED" in failed["error"]


def test_blocked_operations_stop_the_draft(config):
    config.safety.blocked_operations = ["update_conversion_action"]
    result = _draft(config, _linked_but_hidden())
    assert "blocked" in result["error"]
