"""Tests for GA4 audiences: list, create, rename, archive, and the Google Ads readiness check."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from google.analytics.admin_v1alpha import types

from adloop.ads import write as ads_write
from adloop.config import AdLoopConfig, GA4Config, SafetyConfig
from adloop.ga4 import audiences
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore

PROPERTY = "properties/123456"


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(ga4=GA4Config(property_id=PROPERTY),
                        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False))


def _audience(n, name, include=("session_start",), exclude=(), days=30, description="d"):
    """A real Audience message, built the way the apply builds it."""
    message = audiences.build_audience({
        "display_name": name, "description": description, "membership_duration_days": days,
        "include_events": list(include), "exclude_events": list(exclude), "exclude_permanently": False,
    })
    message.name = f"{PROPERTY}/audiences/{n}"
    return message


def _clients(existing=(), links=(), signals="GOOGLE_SIGNALS_ENABLED", level="GOOGLE_ANALYTICS_STANDARD"):
    # Autospec: a call Google's client would reject (a wrong argument name)
    # fails here too.
    from google.analytics.admin_v1alpha import AnalyticsAdminServiceClient

    alpha, beta = create_autospec(AnalyticsAdminServiceClient, instance=True), MagicMock()
    alpha.list_audiences.return_value = list(existing)
    by_name = {a.name: a for a in existing}
    alpha.get_audience.side_effect = lambda name: by_name[name]
    alpha.list_audiences.side_effect = None
    alpha.get_google_signals_settings.return_value = SimpleNamespace(state=SimpleNamespace(name=signals))
    alpha.create_audience.side_effect = lambda parent, audience: audience
    alpha.update_audience.side_effect = lambda audience, update_mask: audience
    alpha.archive_audience.return_value = None
    beta.list_google_ads_links.return_value = list(links)
    beta.get_property.return_value = SimpleNamespace(service_level=SimpleNamespace(name=level))
    return alpha, beta


def _with(alpha, beta):
    first = patch("adloop.ga4.client.get_admin_alpha_client", return_value=alpha)
    second = patch("adloop.ga4.client.get_admin_client", return_value=beta)

    class _Both:
        def __enter__(self):
            first.start()
            second.start()

        def __exit__(self, *_exc):
            first.stop()
            second.stop()

    return _Both()


def _link(customer_id="1234567890", personalization=True):
    return SimpleNamespace(customer_id=customer_id, ads_personalization_enabled=personalization)


def _status(result_or_checks, check):
    checks = result_or_checks["google_ads_readiness"] if isinstance(result_or_checks, dict) else result_or_checks
    return [c["status"] for c in checks if c["check"] == check]


def test_list_shows_events_slots_and_readiness(config):
    alpha, beta = _clients(
        [_audience(1, "Visitors"), _audience(2, "Non-buyers", exclude=("purchase",))], links=[_link()])
    with _with(alpha, beta):
        result = audiences.list_ga4_audiences(config, property_id=PROPERTY)
    assert [a["display_name"] for a in result["audiences"]] == ["Non-buyers", "Visitors"]
    assert result["audiences"][0]["include_events"] == ["session_start"]
    assert result["audiences"][0]["exclude_events"] == ["purchase"]
    assert "exclude_events" not in result["audiences"][1]
    assert result["usage"] == {"used": 2, "limit": 100}
    assert _status(result, "google_ads_link") == ["ok"] and _status(result, "google_signals") == ["ok"]
    assert result["insights"] == []


def test_an_audience_with_other_conditions_is_shown_as_custom(config):
    built_elsewhere = _audience(3, "Pricing page")
    event = built_elsewhere.filter_clauses[0].simple_filter.filter_expression.and_group.filter_expressions[0] \
        .or_group.filter_expressions[0].event_filter
    event.event_parameter_filter_expression = types.AudienceFilterExpression(
        and_group=types.AudienceFilterExpressionList(filter_expressions=[
            types.AudienceFilterExpression(dimension_or_metric_filter=types.AudienceDimensionOrMetricFilter(
                field_name="page_location"))]))
    alpha, beta = _clients([built_elsewhere], links=[_link()])
    with _with(alpha, beta):
        entry = audiences.list_ga4_audiences(config, property_id=PROPERTY)["audiences"][0]
    assert entry["custom_conditions"] is True and "include_events" not in entry
    assert "page_location" in str(entry["conditions"])


@pytest.mark.parametrize("links,signals,check,needle", [
    ([], "GOOGLE_SIGNALS_ENABLED", "google_ads_link", "No Google Ads account is linked"),
    ([_link(personalization=False)], "GOOGLE_SIGNALS_ENABLED", "ads_personalization", "personalized advertising switched off"),
    ([_link()], "GOOGLE_SIGNALS_DISABLED", "google_signals", "Google signals are off"),
])
def test_readiness_warns_about_what_keeps_audiences_out_of_google_ads(config, links, signals, check, needle):
    alpha, beta = _clients(links=links, signals=signals)
    with _with(alpha, beta):
        result = audiences.list_ga4_audiences(config, property_id=PROPERTY)
    assert _status(result, check) == ["warn"]
    assert any(needle in insight for insight in result["insights"])


def test_an_unreadable_setting_is_not_reported_as_fine(config):
    alpha, beta = _clients(links=[_link()])
    alpha.get_google_signals_settings.side_effect = RuntimeError("v1alpha changed")
    beta.list_google_ads_links.side_effect = RuntimeError("denied")
    with _with(alpha, beta):
        checks = audiences.ads_readiness(config, "123456")
    assert _status(checks, "google_signals") == ["info"] and _status(checks, "google_ads_link") == ["info"]


def test_create_previews_the_audience_and_sends_what_ga4_expects(config):
    alpha, beta = _clients(links=[_link(personalization=False)])
    with _with(alpha, beta):
        preview = audiences.draft_ga4_audience(
            config, property_id=PROPERTY, display_name="Visitors without purchase",
            include_events=["session_start", " page_view ", "session_start"], exclude_events=["purchase"],
            membership_duration_days=90)
        assert preview["operation"] == "ga4_create_audience" and preview["platform"] == "ga4"
        assert preview["changes"]["include_events"] == ["session_start", "page_view"]
        assert preview["changes"]["description"] == "Users who sent session_start or page_view, without purchase; 90 days."
        assert any("cannot be changed after creation" in w for w in preview["warnings"])
        assert any("personalized advertising switched off" in w for w in preview["warnings"])
        assert any("consent" in w for w in preview["warnings"])

        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_SUCCESS"
        alpha.create_audience.assert_not_called()
        applied = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    assert applied["status"] == "APPLIED"
    sent = alpha.create_audience.call_args.kwargs
    assert sent["parent"] == PROPERTY
    body = types.Audience.to_dict(sent["audience"], use_integers_for_enums=False)
    assert body["membership_duration_days"] == 90
    assert body["exclusion_duration_mode"] == "EXCLUDE_TEMPORARILY"
    include, exclude = body["filter_clauses"]
    assert (include["clause_type"], exclude["clause_type"]) == ("INCLUDE", "EXCLUDE")
    assert include["simple_filter"]["scope"] == "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS"
    # An AND group on top, one OR group in it, the event filters inside that.
    groups = include["simple_filter"]["filter_expression"]["and_group"]["filter_expressions"]
    assert len(groups) == 1
    assert [e["event_filter"]["event_name"] for e in groups[0]["or_group"]["filter_expressions"]] == [
        "session_start", "page_view"]


@pytest.mark.parametrize("kwargs,needle", [
    ({"display_name": "", "include_events": ["purchase"]}, "display_name is required"),
    ({"display_name": "A"}, "say who joins"),
    ({"display_name": "A", "include_pages": ["/"]}, "part of every URL"),
    ({"display_name": "A", "include_pages": ["/pricing"], "exclude_pages": ["/pricing"]}, "both included and excluded"),
    ({"display_name": "A", "include_events": ["purchase"], "sequence": ["a", "b"]}, "either sequence or include"),
    ({"display_name": "A", "sequence": ["page_view"]}, "needs 2 to 10 steps"),
    ({"display_name": "A", "sequence": [{"event": "a", "page": "/x"}, "b"]}, "exactly one of event or page"),
    ({"display_name": "A", "sequence": [{"event": "a", "within_minutes": 5}, "b"]}, "from the second step on"),
    ({"display_name": "A", "sequence": ["a", {"event": "b", "steps": 1}]}, "unknown key"),
    ({"display_name": "A", "include_events": ["Page View"]}, "is not a GA4 event name"),
    ({"display_name": "A", "include_events": ["purchase"], "exclude_events": ["purchase"]}, "both included and excluded"),
    ({"display_name": "A", "include_events": ["purchase"], "membership_duration_days": 541}, "between 1 and 540"),
    ({"display_name": "A", "include_events": ["purchase"], "exclude_permanently": True}, "needs exclude_events"),
])
def test_create_refuses_bad_input_before_reading_anything(config, kwargs, needle):
    alpha, beta = _clients()
    with _with(alpha, beta):
        result = audiences.draft_ga4_audience(config, property_id=PROPERTY, **kwargs)
    assert any(needle in d for d in result["details"])
    alpha.list_audiences.assert_not_called()


def test_create_refuses_a_taken_name_and_a_full_property(config):
    alpha, beta = _clients([_audience(1, "Visitors")])
    with _with(alpha, beta):
        taken = audiences.draft_ga4_audience(config, property_id=PROPERTY, display_name="Visitors",
                                             include_events=["session_start"])
    assert "already exists" in taken["error"]
    alpha, beta = _clients([_audience(n, f"A{n}") for n in range(100)])
    with _with(alpha, beta):
        full = audiences.draft_ga4_audience(config, property_id=PROPERTY, display_name="One more",
                                            include_events=["session_start"])
    assert "All 100 audience slots" in full["error"]


def test_dry_run_refuses_a_name_taken_after_the_preview(config):
    alpha, beta = _clients()
    with _with(alpha, beta):
        preview = audiences.draft_ga4_audience(config, property_id=PROPERTY, display_name="Buyers",
                                               include_events=["purchase"])
        alpha.list_audiences.return_value = [_audience(9, "Buyers", include=("purchase",))]
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
    assert result["status"] == "DRY_RUN_FAILED" and "in the meantime" in result["error"]


def test_update_changes_name_and_description_only(config):
    alpha, beta = _clients([_audience(1, "Visitors"), _audience(2, "Buyers", include=("purchase",))])
    with _with(alpha, beta):
        events = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="update", audience="Visitors",
                                              include_events=["purchase"])
        taken = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="update", audience="Visitors",
                                             display_name="Buyers")
        same = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="update", audience="1",
                                            display_name="Visitors")
        preview = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="update", audience="Visitors",
                                               display_name="All visitors")
        assert "fixes an audience's conditions at creation" in events["error"]
        assert "already exists" in taken["error"]
        assert same["status"] == "NO_CHANGE"
        assert preview["changes"]["current_display_name"] == "Visitors"
        ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        applied = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    assert applied["status"] == "APPLIED"
    sent = alpha.update_audience.call_args.kwargs
    assert list(sent["update_mask"].paths) == ["display_name"]
    assert sent["audience"].name == f"{PROPERTY}/audiences/1" and sent["audience"].display_name == "All visitors"


def test_archive_needs_a_double_confirmation_and_stops_when_the_audience_changed(config):
    existing = _audience(1, "Visitors")
    alpha, beta = _clients([existing])
    with _with(alpha, beta):
        preview = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="archive", audience="Visitors")
        assert preview["requires_double_confirm"] is True
        assert "cannot be restored" in preview["warnings"][0]
        missing = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="archive", audience="Nope")
        assert missing["error"] == "No audience matches."

        existing.display_name = "Renamed in GA4"
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
    assert result["status"] == "DRY_RUN_FAILED" and "changed in GA4 after the preview" in result["error"]
    alpha.archive_audience.assert_not_called()


def test_the_tools_are_registered_under_the_ga4_toolset():
    import asyncio

    from adloop.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "ga4" in tools["list_ga4_audiences"].tags and "ga4" in tools["draft_ga4_audience"].tags
    assert tools["draft_ga4_audience"].annotations.destructiveHint


def test_archive_sends_the_request_googles_client_accepts(config):
    alpha, beta = _clients([_audience(1, "Visitors")])
    with _with(alpha, beta):
        preview = audiences.draft_ga4_audience(config, property_id=PROPERTY, action="archive", audience="Visitors")
        ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        applied = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    assert applied["status"] == "APPLIED"
    alpha.archive_audience.assert_called_once_with(request={"name": f"{PROPERTY}/audiences/1"})


def test_ga4s_own_audiences_are_marked_predefined_not_empty(config):
    builtin = types.Audience(name=f"{PROPERTY}/audiences/5", display_name="All Users", description="All users",
                             membership_duration_days=540)
    alpha, beta = _clients([builtin], links=[_link()])
    with _with(alpha, beta):
        entry = audiences.list_ga4_audiences(config, property_id=PROPERTY)["audiences"][0]
    assert entry["predefined"] is True and "include_events" not in entry


def test_pages_become_page_view_filters_on_the_url_and_read_back(config):
    alpha, beta = _clients(links=[_link()])
    with _with(alpha, beta):
        preview = audiences.draft_ga4_audience(
            config, property_id=PROPERTY, display_name="Pricing, no sign-up",
            include_pages=["/pricing", " /preise "], exclude_events=["sign_up"])
        assert preview["changes"]["include_pages"] == ["/pricing", "/preise"]
        assert "include_events" not in preview["changes"]
        assert preview["changes"]["description"] == (
            "Users who viewed a page containing '/pricing' or '/preise', without sign_up; 30 days.")
        assert any("full URL contains" in w for w in preview["warnings"])
        ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        applied = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    assert applied["status"] == "APPLIED"
    sent = alpha.create_audience.call_args.kwargs["audience"]
    body = types.Audience.to_dict(sent, use_integers_for_enums=False)
    first = body["filter_clauses"][0]["simple_filter"]["filter_expression"]["and_group"]["filter_expressions"][0][
        "or_group"]["filter_expressions"][0]["event_filter"]
    assert first["event_name"] == "page_view"
    # The shape of Google's own sample: AND group, OR group, the parameter filter.
    field = first["event_parameter_filter_expression"]["and_group"]["filter_expressions"][0]["or_group"][
        "filter_expressions"][0]["dimension_or_metric_filter"]
    assert field["field_name"] == "page_location"
    assert field["string_filter"] == {"match_type": "CONTAINS", "value": "/pricing", "case_sensitive": False}
    assert field["at_any_point_in_time"] is True

    entry = audiences.audience_entry(sent)
    assert entry["include_pages"] == ["/pricing", "/preise"] and entry["exclude_events"] == ["sign_up"]
    assert "include_events" not in entry and "custom_conditions" not in entry


def test_a_sequence_becomes_ordered_steps_and_reads_back(config):
    steps = ["session_start", {"page": "/pricing"},
             {"event": "begin_checkout", "directly_followed": True, "within_minutes": 30}]
    alpha, beta = _clients(links=[_link()])
    with _with(alpha, beta):
        preview = audiences.draft_ga4_audience(
            config, property_id=PROPERTY, display_name="Checkout starters", sequence=steps,
            exclude_events=["purchase"], exclude_permanently=True)
        assert preview["changes"]["sequence"] == [
            {"event": "session_start"}, {"page": "/pricing"},
            {"event": "begin_checkout", "directly_followed": True, "within_minutes": 30}]
        assert preview["changes"]["description"].startswith(
            "Users who sent session_start, then viewed a page containing '/pricing', then sent begin_checkout")
        assert len(preview["changes"]["description"]) <= 150
        assert any("in this order" in w for w in preview["warnings"])
        ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    sent = alpha.create_audience.call_args.kwargs["audience"]
    include, exclude = sent.filter_clauses
    assert include.clause_type.name == "INCLUDE" and exclude.clause_type.name == "EXCLUDE"
    built = include.sequence_filter.sequence_steps
    assert [s.scope.name for s in built] == ["AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS"] * 3
    assert [s.immediately_follows for s in built] == [False, False, True]
    assert built[2].constraint_duration.total_seconds() == 1800
    assert built[1].filter_expression.and_group.filter_expressions[0].or_group.filter_expressions[
        0].event_filter.event_name == "page_view"

    entry = audiences.audience_entry(sent)
    assert entry["sequence"] == preview["changes"]["sequence"]
    assert entry["exclude_events"] == ["purchase"] and entry["exclude_permanently"] is True
    assert "custom_conditions" not in entry


def test_a_page_view_filter_on_another_parameter_stays_custom():
    message = audiences.build_audience({"display_name": "x", "description": "d", "membership_duration_days": 30,
                                        "include_pages": ["/pricing"]})
    message.name = f"{PROPERTY}/audiences/9"
    field = message.filter_clauses[0].simple_filter.filter_expression.and_group.filter_expressions[0].or_group.\
        filter_expressions[0].event_filter.event_parameter_filter_expression.and_group.filter_expressions[0].\
        or_group.filter_expressions[0].dimension_or_metric_filter
    field.field_name = "page_title"
    entry = audiences.audience_entry(message)
    assert entry["custom_conditions"] is True and "include_pages" not in entry
