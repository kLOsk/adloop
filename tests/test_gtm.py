"""Tests for the Google Tag Manager integration — parsers + audit_event_coverage."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop.crossref import audit_event_coverage
from adloop.gtm.read import (
    _BUILT_IN_TRIGGERS,
    GA4_EVENT_TAG,
    _element_visibility_summary,
    _params_dict,
    _parse_trigger,
    _parse_variable,
    _resolve_trigger,
    _summarize_filter,
    _trigger_group_member_ids,
)


# ---------------------------------------------------------------------------
# _params_dict — flatten parameter[] arrays into a {key: value} dict
# ---------------------------------------------------------------------------


class TestParamsDict:
    def test_value_only_param(self):
        tag = {"parameter": [{"type": "template", "key": "tagId", "value": "G-XXX"}]}
        assert _params_dict(tag) == {"tagId": "G-XXX"}

    def test_list_param(self):
        tag = {
            "parameter": [
                {"key": "ids", "type": "list", "list": [{"value": "a"}, {"value": "b"}]}
            ]
        }
        result = _params_dict(tag)
        assert result["ids"] == [{"value": "a"}, {"value": "b"}]

    def test_map_param(self):
        tag = {
            "parameter": [
                {"key": "settings", "type": "map", "map": [{"key": "k", "value": "v"}]}
            ]
        }
        result = _params_dict(tag)
        assert result["settings"] == [{"key": "k", "value": "v"}]

    def test_skips_keyless_params(self):
        tag = {"parameter": [{"value": "orphan"}, {"key": "good", "value": "ok"}]}
        assert _params_dict(tag) == {"good": "ok"}

    def test_empty_parameter_list(self):
        assert _params_dict({"parameter": []}) == {}

    def test_no_parameter_key(self):
        assert _params_dict({}) == {}


# ---------------------------------------------------------------------------
# _summarize_filter — render variable [NOT] OP value, including negate flag
# ---------------------------------------------------------------------------


class TestSummarizeFilter:
    def test_basic_contains(self):
        f = {
            "type": "contains",
            "parameter": [
                {"key": "arg0", "value": "{{Page Path}}"},
                {"key": "arg1", "value": "service-promotions"},
            ],
        }
        assert _summarize_filter(f) == "{{Page Path}} contains service-promotions"

    def test_negate_true_renders_NOT(self):
        f = {
            "type": "contains",
            "parameter": [
                {"key": "arg0", "value": "{{Form ID}}"},
                {"key": "arg1", "value": "newsletter"},
                {"key": "negate", "value": "true"},
            ],
        }
        assert _summarize_filter(f) == "{{Form ID}} NOT contains newsletter"

    def test_negate_false_no_prefix(self):
        f = {
            "type": "equals",
            "parameter": [
                {"key": "arg0", "value": "{{Event}}"},
                {"key": "arg1", "value": "click"},
                {"key": "negate", "value": "false"},
            ],
        }
        assert _summarize_filter(f) == "{{Event}} equals click"

    def test_arbitrary_op_preserved(self):
        f = {
            "type": "matchRegex",
            "parameter": [
                {"key": "arg0", "value": "{{Page URL}}"},
                {"key": "arg1", "value": "^https://"},
            ],
        }
        assert _summarize_filter(f) == "{{Page URL}} matchRegex ^https://"

    def test_missing_args_render_question_mark(self):
        f = {"type": "contains", "parameter": []}
        assert _summarize_filter(f) == "? contains ?"

    def test_missing_type_renders_question_mark(self):
        f = {
            "parameter": [
                {"key": "arg0", "value": "{{X}}"},
                {"key": "arg1", "value": "y"},
            ]
        }
        assert _summarize_filter(f) == "{{X}} ? y"


# ---------------------------------------------------------------------------
# _resolve_trigger — built-in IDs (>= 2147479553) get readable names
# ---------------------------------------------------------------------------


class TestResolveTrigger:
    def test_custom_trigger_in_dict(self):
        by_id = {"42": {"name": "My Trigger", "type": "click"}}
        assert _resolve_trigger(by_id, "42") == {
            "id": "42",
            "name": "My Trigger",
            "type": "click",
        }

    def test_built_in_all_pages(self):
        result = _resolve_trigger({}, "2147479553")
        assert result["id"] == "2147479553"
        assert "All Pages" in result["name"]
        assert result["name"].startswith("(built-in)")
        assert result["type"] == "pageview"

    def test_built_in_initialization(self):
        result = _resolve_trigger({}, "2147479573")
        assert "Initialization" in result["name"]
        assert result["type"] == "init"

    def test_built_in_consent(self):
        result = _resolve_trigger({}, "2147479572")
        assert "Consent" in result["name"]
        assert result["type"] == "consentInit"

    def test_unknown_built_in_id(self):
        result = _resolve_trigger({}, "9999999999")
        assert result["id"] == "9999999999"
        assert "unknown" in result["name"].lower()
        assert result["type"] is None

    def test_built_in_dict_complete(self):
        # Sanity: every entry in _BUILT_IN_TRIGGERS resolves cleanly
        for tid in _BUILT_IN_TRIGGERS:
            result = _resolve_trigger({}, tid)
            assert result["name"].startswith("(built-in)")
            assert result["type"] is not None


# ---------------------------------------------------------------------------
# _trigger_group_member_ids — extract triggerIds list from a triggerGroup
# ---------------------------------------------------------------------------


class TestTriggerGroupMemberIds:
    def test_extracts_member_ids(self):
        trigger = {
            "type": "triggerGroup",
            "parameter": [
                {
                    "key": "triggerIds",
                    "type": "list",
                    "list": [
                        {"type": "triggerReference", "value": "9"},
                        {"type": "triggerReference", "value": "21"},
                    ],
                }
            ],
        }
        assert _trigger_group_member_ids(trigger) == ["9", "21"]

    def test_empty_when_no_triggerIds_param(self):
        trigger = {"type": "triggerGroup", "parameter": []}
        assert _trigger_group_member_ids(trigger) == []

    def test_empty_when_list_is_empty(self):
        trigger = {
            "type": "triggerGroup",
            "parameter": [{"key": "triggerIds", "type": "list", "list": []}],
        }
        assert _trigger_group_member_ids(trigger) == []

    def test_skips_items_without_value(self):
        trigger = {
            "type": "triggerGroup",
            "parameter": [
                {
                    "key": "triggerIds",
                    "type": "list",
                    "list": [
                        {"type": "triggerReference", "value": "1"},
                        {"type": "triggerReference"},  # missing value
                    ],
                }
            ],
        }
        assert _trigger_group_member_ids(trigger) == ["1"]


# ---------------------------------------------------------------------------
# _element_visibility_summary — selector + timing for elementVisibility triggers
# ---------------------------------------------------------------------------


class TestElementVisibilitySummary:
    def test_id_selector_uppercase(self):
        # GTM returns selectorType="ID" (uppercase) — the regression case
        trigger = {
            "type": "elementVisibility",
            "parameter": [
                {"key": "selectorType", "value": "ID"},
                {"key": "elementId", "value": "form-success"},
                {"key": "firingFrequency", "value": "ONCE"},
                {"key": "onScreenRatio", "value": "10"},
            ],
        }
        result = _element_visibility_summary(trigger)
        assert result["selector_type"] == "ID"
        assert result["selector"] == "form-success"
        assert result["firing_frequency"] == "ONCE"
        assert result["on_screen_ratio"] == "10"

    def test_id_selector_lowercase(self):
        # Defensive: case-insensitive match
        trigger = {
            "type": "elementVisibility",
            "parameter": [
                {"key": "selectorType", "value": "id"},
                {"key": "elementId", "value": "x"},
            ],
        }
        result = _element_visibility_summary(trigger)
        assert result["selector"] == "x"

    def test_css_selector(self):
        trigger = {
            "type": "elementVisibility",
            "parameter": [
                {"key": "selectorType", "value": "CSS"},
                {"key": "elementSelector", "value": "#root .success"},
                {"key": "useDomChangeListener", "value": "true"},
            ],
        }
        result = _element_visibility_summary(trigger)
        assert result["selector_type"] == "CSS"
        assert result["selector"] == "#root .success"
        assert result["use_dom_change_listener"] == "true"

    def test_missing_fields_return_none(self):
        result = _element_visibility_summary({"parameter": []})
        # selectorType is None → falls through to elementSelector lookup → also None
        assert result["selector"] is None
        assert result["selector_type"] is None
        assert result["firing_frequency"] is None


# ---------------------------------------------------------------------------
# _parse_trigger — type-specific dispatch
# ---------------------------------------------------------------------------


class TestParseTrigger:
    def test_basic_trigger_no_extras(self):
        trigger = {
            "triggerId": "5",
            "name": "Click Trigger",
            "type": "click",
            "filter": [],
        }
        result = _parse_trigger(trigger)
        assert result["trigger_id"] == "5"
        assert result["name"] == "Click Trigger"
        assert result["type"] == "click"
        assert "group_member_trigger_ids" not in result
        assert "element_visibility" not in result

    def test_trigger_group_adds_member_ids(self):
        trigger = {
            "triggerId": "10",
            "name": "Group",
            "type": "triggerGroup",
            "filter": [],
            "parameter": [
                {
                    "key": "triggerIds",
                    "type": "list",
                    "list": [{"value": "1"}, {"value": "2"}],
                }
            ],
        }
        result = _parse_trigger(trigger)
        assert result["group_member_trigger_ids"] == ["1", "2"]

    def test_element_visibility_adds_block(self):
        trigger = {
            "triggerId": "7",
            "name": "Visibility",
            "type": "elementVisibility",
            "filter": [],
            "parameter": [
                {"key": "selectorType", "value": "ID"},
                {"key": "elementId", "value": "thanks"},
            ],
        }
        result = _parse_trigger(trigger)
        assert "element_visibility" in result
        assert result["element_visibility"]["selector"] == "thanks"

    def test_filters_parsed_to_text(self):
        trigger = {
            "triggerId": "1",
            "name": "X",
            "type": "click",
            "filter": [
                {
                    "type": "contains",
                    "parameter": [
                        {"key": "arg0", "value": "{{Page Path}}"},
                        {"key": "arg1", "value": "/x"},
                    ],
                }
            ],
        }
        result = _parse_trigger(trigger)
        assert result["filters"] == ["{{Page Path}} contains /x"]

    def test_wait_for_tags_extracted_from_dict(self):
        trigger = {
            "triggerId": "1",
            "name": "X",
            "type": "click",
            "waitForTags": {"value": "true"},
        }
        result = _parse_trigger(trigger)
        assert result["wait_for_tags"] == "true"


# ---------------------------------------------------------------------------
# _parse_variable
# ---------------------------------------------------------------------------


class TestParseVariable:
    def test_basic_variable(self):
        variable = {
            "variableId": "14",
            "name": "DLV - promo",
            "type": "v",
            "parameter": [{"key": "name", "value": "promo_name"}],
            "formatValue": {},
        }
        result = _parse_variable(variable)
        assert result["variable_id"] == "14"
        assert result["name"] == "DLV - promo"
        assert result["type"] == "v"
        assert result["parameters"] == {"name": "promo_name"}


# ---------------------------------------------------------------------------
# audit_event_coverage — status determination + insights
# ---------------------------------------------------------------------------


def _container(tags=None):
    """Helper: build the dict shape `get_live_container` returns."""
    return {
        "account_id": "A",
        "container_id": "C",
        "container_version_id": "1",
        "container_version_name": None,
        "fingerprint": "f",
        "tags": tags or [],
        "trigger_count": 0,
        "variable_count": 0,
    }


def _ga4_response(events: dict[str, int]):
    """Helper: build the dict shape `get_tracking_events` returns."""
    return {
        "rows": [{"eventName": k, "eventCount": str(v)} for k, v in events.items()],
    }


def _ga4_event_tag(name: str, event_name: str, paused: bool = False):
    """Helper: build a parsed GA4 event tag."""
    return {
        "tag_id": name,
        "name": name,
        "type": GA4_EVENT_TAG,
        "event_name": event_name,
        "paused": paused,
        "firing_triggers": [],
        "blocking_triggers": [],
        "parameters": {},
    }


@pytest.fixture
def patch_gtm_and_ga4():
    """Patch the two external calls that audit_event_coverage makes."""

    def _patch(container_dict, ga4_dict):
        return (
            patch("adloop.gtm.read.get_live_container", return_value=container_dict),
            patch("adloop.ga4.tracking.get_tracking_events", return_value=ga4_dict),
        )

    return _patch


class TestAuditEventCoverageStatuses:
    """Each test forces one specific status code into the matrix."""

    def _run(self, container, ga4, expected_events):
        with (
            patch("adloop.gtm.read.get_live_container", return_value=container),
            patch(
                "adloop.ga4.tracking.get_tracking_events", return_value=ga4
            ),
        ):
            return audit_event_coverage(
                config=None,
                expected_events=expected_events,
                gtm_account_id="A",
                gtm_container_id="C",
                date_range_start="2026-04-01",
                date_range_end="2026-04-30",
            )

    def _status_for(self, result, event_name):
        for row in result["matrix"]:
            if row["event_name"] == event_name:
                return row["status"]
        raise AssertionError(f"event {event_name} not in matrix")

    def test_ok_status(self):
        # codebase + active tag + ga4 fires
        c = _container([_ga4_event_tag("T", "purchase")])
        g = _ga4_response({"purchase": 5})
        result = self._run(c, g, ["purchase"])
        assert self._status_for(result, "purchase") == "ok"

    def test_no_tag_no_fire(self):
        # codebase event, no tag, no ga4
        result = self._run(_container([]), _ga4_response({}), ["my_custom_event"])
        assert self._status_for(result, "my_custom_event") == "no_tag_no_fire"

    def test_tag_paused(self):
        # codebase + tag exists + paused (no ga4 fires either)
        c = _container([_ga4_event_tag("T", "lead", paused=True)])
        result = self._run(c, _ga4_response({}), ["lead"])
        assert self._status_for(result, "lead") == "tag_paused"

    def test_tag_active_but_not_firing(self):
        # codebase + active tag + ga4 reports zero
        c = _container([_ga4_event_tag("T", "signup")])
        result = self._run(c, _ga4_response({}), ["signup"])
        assert self._status_for(result, "signup") == "tag_active_but_not_firing"

    def test_ok_auto_collected(self):
        # codebase event matches a GA4 auto event, no tag, ga4 fires
        result = self._run(
            _container([]),
            _ga4_response({"scroll": 100}),
            ["scroll"],
        )
        assert self._status_for(result, "scroll") == "ok_auto_collected"

    def test_ga4_fires_no_tag(self):
        # codebase event fires in GA4 but no tag, NOT auto event
        result = self._run(
            _container([]),
            _ga4_response({"my_custom": 3}),
            ["my_custom"],
        )
        assert self._status_for(result, "my_custom") == "ga4_fires_no_tag"

    def test_gtm_only_firing(self):
        # tag exists + active + fires + NOT in codebase
        c = _container([_ga4_event_tag("T", "newsletter_signup")])
        g = _ga4_response({"newsletter_signup": 7})
        result = self._run(c, g, [])
        assert self._status_for(result, "newsletter_signup") == "gtm_only_firing"

    def test_gtm_only_not_firing(self):
        # tag exists + NOT in codebase + no ga4 fires
        c = _container([_ga4_event_tag("T", "stale_event")])
        result = self._run(c, _ga4_response({}), [])
        assert self._status_for(result, "stale_event") == "gtm_only_not_firing"

    def test_auto_event_only(self):
        # auto event fires + no tag + not in codebase
        result = self._run(
            _container([]),
            _ga4_response({"page_view": 100}),
            [],
        )
        assert self._status_for(result, "page_view") == "auto_event_only"

    def test_ga4_only_non_auto(self):
        # ga4 fires + no tag + not in codebase + not auto
        result = self._run(
            _container([]),
            _ga4_response({"third_party_event": 4}),
            [],
        )
        assert self._status_for(result, "third_party_event") == "ga4_only"


class TestAuditEventCoverageInsights:
    def _run(self, container, ga4, expected_events):
        with (
            patch("adloop.gtm.read.get_live_container", return_value=container),
            patch("adloop.ga4.tracking.get_tracking_events", return_value=ga4),
        ):
            return audit_event_coverage(
                config=None,
                expected_events=expected_events,
                gtm_account_id="A",
                gtm_container_id="C",
                date_range_start="2026-04-01",
                date_range_end="2026-04-30",
            )

    def test_no_tag_no_fire_generates_insight(self):
        result = self._run(_container([]), _ga4_response({}), ["missing_event"])
        assert any("NO GTM tag" in s for s in result["insights"])
        assert any("missing_event" in s for s in result["insights"])

    def test_paused_tag_generates_insight(self):
        c = _container([_ga4_event_tag("T", "x", paused=True)])
        result = self._run(c, _ga4_response({}), ["x"])
        assert any("PAUSED" in s for s in result["insights"])

    def test_dynamic_event_tag_generates_insight(self):
        c = _container([_ga4_event_tag("T", "{{Event}}")])
        result = self._run(c, _ga4_response({}), [])
        assert any("DYNAMIC" in s for s in result["insights"])
        # Dynamic event tags should not appear in the matrix as real events
        assert all(row["event_name"] != "{{Event}}" for row in result["matrix"])
        assert len(result["dynamic_event_tags"]) == 1

    def test_custom_html_tag_generates_insight(self):
        # Custom HTML tag in the container
        html_tag = {
            "tag_id": "5",
            "name": "FB Pixel",
            "type": "html",
            "event_name": None,
            "paused": False,
            "firing_triggers": [],
            "blocking_triggers": [],
            "parameters": {"html": "<script>fbq('init', 'X')</script>"},
        }
        c = _container([html_tag])
        result = self._run(c, _ga4_response({}), [])
        assert any("Custom HTML" in s for s in result["insights"])
        assert len(result["custom_html_tags"]) == 1


class TestAuditEventCoverageMatrixShape:
    def _run(self, container, ga4, expected_events):
        with (
            patch("adloop.gtm.read.get_live_container", return_value=container),
            patch("adloop.ga4.tracking.get_tracking_events", return_value=ga4),
        ):
            return audit_event_coverage(
                config=None,
                expected_events=expected_events,
                gtm_account_id="A",
                gtm_container_id="C",
                date_range_start="2026-04-01",
                date_range_end="2026-04-30",
            )

    def test_returns_required_fields(self):
        result = self._run(_container([]), _ga4_response({}), [])
        assert "container" in result
        assert "matrix" in result
        assert "insights" in result
        assert "date_range" in result
        assert result["date_range"] == {"start": "2026-04-01", "end": "2026-04-30"}

    def test_container_summary_has_tag_type_breakdown(self):
        # Mixed tag types should be tallied in other_tag_types
        misc_tag = {
            "tag_id": "9",
            "name": "Linker",
            "type": "gclidw",
            "event_name": None,
            "paused": False,
            "firing_triggers": [],
            "blocking_triggers": [],
            "parameters": {},
        }
        c = _container([_ga4_event_tag("T", "x"), misc_tag])
        result = self._run(c, _ga4_response({}), [])
        assert result["container"]["ga4_event_tag_count"] == 1
        assert result["container"]["other_tag_types"]["gclidw"] == 1

    def test_ga4_error_short_circuits(self):
        with (
            patch("adloop.gtm.read.get_live_container", return_value=_container([])),
            patch(
                "adloop.ga4.tracking.get_tracking_events",
                return_value={"error": "GA4 unauthorized"},
            ),
        ):
            result = audit_event_coverage(
                config=None,
                expected_events=["x"],
                gtm_account_id="A",
                gtm_container_id="C",
            )
        assert "error" in result
        assert "GA4" in result["error"]

    def test_matrix_sorted_alphabetically(self):
        # Multiple events should come back in sorted order
        result = self._run(
            _container([]), _ga4_response({"zzz": 1, "aaa": 1}), ["mmm"]
        )
        names = [row["event_name"] for row in result["matrix"]]
        assert names == sorted(names)
