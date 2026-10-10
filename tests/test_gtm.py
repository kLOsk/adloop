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
# list_accounts — empty result must carry an anti-misreading insight
# ---------------------------------------------------------------------------


class TestListAccountsEmpty:
    def test_empty_account_list_warns_against_healthy_misreading(self):
        """Regression: a model asked to audit tags reported 'Tag Manager
        is ok' for a Google account that has no GTM at all — an empty
        list must say so explicitly instead of reading like a clean
        audit."""
        from unittest.mock import MagicMock

        from adloop.config import AdLoopConfig
        from adloop.gtm.read import list_accounts

        client = MagicMock()
        client.accounts.return_value.list.return_value.execute.return_value = {}
        with patch("adloop.gtm.client.get_gtm_client", return_value=client):
            result = list_accounts(AdLoopConfig())

        assert result["count"] == 0
        assert any("NO Tag Manager" in i for i in result["insights"])

    def test_populated_account_list_has_no_insights(self):
        from unittest.mock import MagicMock

        from adloop.config import AdLoopConfig
        from adloop.gtm.read import list_accounts

        client = MagicMock()
        client.accounts.return_value.list.return_value.execute.return_value = {
            "account": [{"accountId": "600", "name": "Main", "path": "accounts/600"}]
        }
        with patch("adloop.gtm.client.get_gtm_client", return_value=client):
            result = list_accounts(AdLoopConfig())

        assert result["count"] == 1
        assert "insights" not in result


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

    def test_gtm_paused_but_firing(self):
        # NOT in codebase + only paused tag(s) + ga4 still fires: the event
        # reaches GA4 from another source while its GTM tag lies dormant.
        # Previously fell through to "unknown".
        c = _container([_ga4_event_tag("T", "legacy_event", paused=True)])
        g = _ga4_response({"legacy_event": 12})
        result = self._run(c, g, [])
        assert self._status_for(result, "legacy_event") == "gtm_paused_but_firing"
        assert any("paused" in i and "another source" in i for i in result["insights"])

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


# ---------------------------------------------------------------------------
# get_workspace_diff — every Entity kind, not just tags and triggers
# ---------------------------------------------------------------------------


class TestWorkspaceDiff:
    class _Req:
        def __init__(self, value):
            self._value = value

        def execute(self):
            return self._value

    class _Ws:
        def __init__(self, status):
            self._status = status

        def getStatus(self, path):
            return TestWorkspaceDiff._Req(self._status)

    class _Client:
        def __init__(self, status):
            self._status = status

        def accounts(self):
            return self

        def containers(self):
            return self

        def workspaces(self):
            return TestWorkspaceDiff._Ws(self._status)

    def _diff(self, status):
        from adloop.config import AdLoopConfig, GtmConfig
        from adloop.gtm import read

        config = AdLoopConfig(
            gtm=GtmConfig(account_id="1", container_id="2"),
        )
        client = self._Client(status)
        with patch("adloop.gtm.client.get_gtm_client", lambda _cfg: client):
            return read.get_workspace_diff(
                config, account_id="1", container_id="2", workspace_id="12"
            )

    def test_a_custom_template_change_is_listed(self):
        """Regression: a template-only workspace reported a count but no row."""
        out = self._diff({"workspaceChange": [
            {"changeStatus": "added", "customTemplate": {
                "templateId": "7", "name": "Call tracking", "fingerprint": "f"}},
        ]})
        assert out["change_count"] == 1
        assert [c["entity_kind"] for c in out["changes"]] == ["customTemplate"]
        assert out["changes"][0]["entity_id"] == "7"
        assert out["changes"][0]["name"] == "Call tracking"

    def test_a_gtag_config_change_is_listed(self):
        out = self._diff({"workspaceChange": [
            {"changeStatus": "added", "gtagConfig": {
                "gtagConfigId": "9", "type": "googtag"}},
        ]})
        assert [c["entity_kind"] for c in out["changes"]] == ["gtagConfig"]
        assert out["changes"][0]["entity_id"] == "9"


# ---------------------------------------------------------------------------
# Read client fake for workspace reads, version diff and built-ins
# ---------------------------------------------------------------------------


class _ReadReq:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeReadGTM:
    """Read-only slice of tagmanager v2 that records every call made."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.live: dict = {}
        self.versions_by_id: dict[str, dict] = {}
        self.headers: list[dict] = []
        self.workspaces_list: list[dict] = []
        # workspace_id -> kind -> list of pages (each page a list of entities)
        self.ws_pages: dict[str, dict[str, list[list[dict]]]] = {}

    def accounts(self):
        return self

    def containers(self):
        return self

    def versions(self):
        fake = self

        class _V:
            def live(self, parent):
                def run():
                    fake.calls.append(("versions.live", parent))
                    return fake.live
                return _ReadReq(run)

            def get(self, path):
                def run():
                    fake.calls.append(("versions.get", path))
                    return fake.versions_by_id[path.rsplit("/", 1)[-1]]
                return _ReadReq(run)

        return _V()

    def version_headers(self):
        fake = self

        class _H:
            def list(self, parent, pageToken=None):
                def run():
                    fake.calls.append(("version_headers.list", parent))
                    return {"containerVersionHeader": fake.headers}
                return _ReadReq(run)

        return _H()

    def workspaces(self):
        fake = self

        def lister(kind):
            def factory():
                class _L:
                    def list(self, parent, pageToken=None):
                        def run():
                            fake.calls.append((f"{kind}.list", parent, pageToken))
                            ws_id = parent.rsplit("/", 1)[-1]
                            pages = fake.ws_pages.get(ws_id, {}).get(kind, [[]])
                            idx = int(pageToken or 0)
                            resp = {kind: pages[idx]}
                            if idx + 1 < len(pages):
                                resp["nextPageToken"] = str(idx + 1)
                            return resp
                        return _ReadReq(run)
                return _L()
            return factory

        class _W:
            def list(self, parent, pageToken=None):
                def run():
                    fake.calls.append(("workspaces.list", parent))
                    return {"workspace": fake.workspaces_list}
                return _ReadReq(run)

            tags = staticmethod(lister("tag"))
            triggers = staticmethod(lister("trigger"))
            variables = staticmethod(lister("variable"))
            built_in_variables = staticmethod(lister("builtInVariable"))

        return _W()


@pytest.fixture
def read_fake():
    fake = FakeReadGTM()
    with patch("adloop.gtm.client.get_gtm_client", return_value=fake):
        yield fake


def _read_config():
    from adloop.config import AdLoopConfig, GtmConfig

    return AdLoopConfig(gtm=GtmConfig(account_id="1", container_id="2"))


def _tag(tid, name, ttype="gaawe", params=None, triggers=None, **extra):
    return {
        "tagId": tid, "name": name, "type": ttype,
        "parameter": params or [],
        "firingTriggerId": triggers or [],
        "fingerprint": f"fp-{tid}", "path": f"p/{tid}",
        **extra,
    }


class TestWorkspaceAwareReads:
    def test_live_tag_list_is_a_single_call(self, read_fake):
        from adloop.gtm import read

        read_fake.live = {"containerVersionId": "9", "tag": [_tag("1", "A")],
                          "trigger": []}
        out = read.list_tags(_read_config(), account_id="1", container_id="2")
        assert out["source"] == "live"
        assert out["container_version_id"] == "9"
        assert [c[0] for c in read_fake.calls] == ["versions.live"]

    def test_workspace_tag_list_shows_drafted_tags_across_pages(self, read_fake):
        from adloop.gtm import read

        read_fake.ws_pages["12"] = {
            "tag": [[_tag("1", "Live tag")],
                    [_tag("2", "Just drafted", triggers=["5"])]],
            "trigger": [[{"triggerId": "5", "name": "Thanks", "type": "pageview"}]],
        }
        out = read.list_tags(_read_config(), account_id="1", container_id="2",
                             workspace_id="12")

        assert out["source"] == "workspace"
        assert out["workspace_id"] == "12"
        assert out["container_version_id"] is None
        assert [t["name"] for t in out["tags"]] == ["Live tag", "Just drafted"]
        assert out["tags"][1]["firing_triggers"][0]["name"] == "Thanks"
        assert not any(c[0] == "versions.live" for c in read_fake.calls)
        assert all(c[1].endswith("/workspaces/12") for c in read_fake.calls)

    def test_workspace_get_tag_not_found_names_the_workspace(self, read_fake):
        from adloop.gtm import read

        read_fake.ws_pages["12"] = {"tag": [[_tag("1", "A")]], "trigger": [[]]}
        out = read.get_tag(_read_config(), account_id="1", container_id="2",
                           tag_id="99", workspace_id="12")
        assert "workspace 12" in out["error"]
        assert out["available_tag_ids"] == ["1"]

    def test_workspace_get_trigger_lists_tags_using_it(self, read_fake):
        from adloop.gtm import read

        read_fake.ws_pages["12"] = {
            "trigger": [[{"triggerId": "5", "name": "Thanks", "type": "pageview"}]],
            "tag": [[_tag("1", "Lead", triggers=["5"]), _tag("2", "Other")]],
        }
        out = read.get_trigger(_read_config(), account_id="1", container_id="2",
                               trigger_id="5", workspace_id="12")
        assert out["source"] == "workspace"
        assert out["used_by_tags"] == [{"tag_id": "1", "name": "Lead"}]

    def test_workspace_trigger_list(self, read_fake):
        from adloop.gtm import read

        read_fake.ws_pages["12"] = {
            "trigger": [[{"triggerId": "5", "name": "Thanks", "type": "pageview"}]],
        }
        out = read.list_triggers(_read_config(), account_id="1", container_id="2",
                                 workspace_id="12")
        assert out["count"] == 1
        assert [c[0] for c in read_fake.calls] == ["trigger.list"]

    def test_live_variables_take_built_ins_from_the_live_version(self, read_fake):
        from adloop.gtm import read

        read_fake.live = {
            "containerVersionId": "9",
            "variable": [{"variableId": "3", "name": "DL - value", "type": "v"}],
            "builtInVariable": [{"name": "Page URL", "type": "pageUrl"}],
        }
        out = read.list_variables(_read_config(), account_id="1", container_id="2")
        assert out["built_in"] == [{"name": "Page URL", "type": "pageUrl"}]
        assert out["built_in_source"] == "live_version"
        assert [c[0] for c in read_fake.calls] == ["versions.live"]

    def test_built_in_fallback_reads_the_default_workspace_not_the_first(
        self, read_fake
    ):
        """Regression: built-ins came from workspaces[0], which can be any
        parallel draft rather than the Default Workspace."""
        from adloop.gtm import read

        read_fake.live = {"containerVersionId": "9", "variable": []}
        read_fake.workspaces_list = [
            {"workspaceId": "30", "name": "Experiment"},
            {"workspaceId": "12", "name": "Default Workspace"},
        ]
        read_fake.ws_pages["12"] = {
            "builtInVariable": [[{"name": "Click URL", "type": "clickUrl"}]],
        }
        out = read.list_variables(_read_config(), account_id="1", container_id="2")

        biv_calls = [c for c in read_fake.calls if c[0] == "builtInVariable.list"]
        assert [c[1].rsplit("/", 1)[-1] for c in biv_calls] == ["12"]
        assert out["built_in"] == [{"name": "Click URL", "type": "clickUrl"}]
        assert "Default Workspace" in out["built_in_source"]

    def test_workspace_variables_use_the_given_workspace(self, read_fake):
        from adloop.gtm import read

        read_fake.ws_pages["30"] = {
            "variable": [[{"variableId": "4", "name": "New var", "type": "c"}]],
            "builtInVariable": [[{"name": "Form ID", "type": "formId"}]],
        }
        out = read.list_variables(_read_config(), account_id="1", container_id="2",
                                  workspace_id="30")
        assert out["source"] == "workspace"
        assert [v["name"] for v in out["custom_variables"]] == ["New var"]
        assert out["built_in"] == [{"name": "Form ID", "type": "formId"}]
        assert not any(c[0] in ("versions.live", "workspaces.list")
                       for c in read_fake.calls)


class TestVersionHistory:
    def test_versions_are_newest_first_and_claim_no_author(self, read_fake):
        from adloop.gtm import read

        read_fake.headers = [
            {"containerVersionId": "2", "name": "two"},
            {"containerVersionId": "10", "name": "ten"},
            {"containerVersionId": "7", "name": "seven"},
        ]
        out = read.list_versions(_read_config(), account_id="1", container_id="2",
                                 page_size=2)
        assert [v["container_version_id"] for v in out["versions"]] == ["10", "7"]
        assert "no creation/publish time and no author" in out["note"]


# ---------------------------------------------------------------------------
# Version diff
# ---------------------------------------------------------------------------


def _version(vid, *, tags=(), triggers=(), variables=(), built_ins=(), name=None):
    return {
        "containerVersionId": vid, "name": name or f"v{vid}",
        "path": f"accounts/1/containers/2/versions/{vid}",
        "fingerprint": f"vfp-{vid}",
        "tag": list(tags), "trigger": list(triggers), "variable": list(variables),
        "builtInVariable": [{"name": b, "type": b} for b in built_ins],
    }


class TestDiffContainerVersions:
    def test_added_removed_and_changed_with_fields(self):
        from adloop.gtm.read import diff_container_versions

        old = _version("1", tags=[
            _tag("1", "GA4 lead", params=[
                {"type": "template", "key": "eventName", "value": "lead"}]),
            _tag("2", "Old pixel", ttype="img"),
        ], built_ins=["pageUrl"])
        new = _version("2", tags=[
            {**_tag("1", "GA4 lead v2", params=[
                {"type": "template", "key": "eventName", "value": "generate_lead"}]),
             "fingerprint": "changed-but-irrelevant", "path": "elsewhere"},
            _tag("3", "Ads conversion", ttype="awct"),
        ], built_ins=["pageUrl", "clickUrl"])

        diff = diff_container_versions(old, new)

        assert diff["tags"]["added"] == [
            {"id": "3", "name": "Ads conversion", "type": "awct"}]
        assert diff["tags"]["removed"] == [
            {"id": "2", "name": "Old pixel", "type": "img"}]
        changed = diff["tags"]["changed"][0]
        assert changed["previous_name"] == "GA4 lead"
        assert changed["changed_fields"] == ["name", "parameter"]
        assert changed["changes"]["parameters"] == {
            "eventName": {"from": "lead", "to": "generate_lead"}}
        assert diff["built_in_variables"] == {"enabled": ["clickUrl"], "disabled": []}
        assert diff["summary"]["tags"] == {"added": 1, "removed": 1, "changed": 1}
        assert diff["is_identical"] is False

    def test_trigger_conditions_are_rendered_as_text(self):
        from adloop.gtm.read import diff_container_versions

        def trig(value):
            return {"triggerId": "5", "name": "Thanks", "type": "pageview",
                    "filter": [{"type": "contains", "parameter": [
                        {"key": "arg0", "value": "{{Page Path}}"},
                        {"key": "arg1", "value": value}]}]}

        diff = diff_container_versions(
            _version("1", triggers=[trig("/thanks")]),
            _version("2", triggers=[trig("/thank-you")]),
        )
        change = diff["triggers"]["changed"][0]["changes"]["filter"]
        assert change == {"from": ["{{Page Path}} contains /thanks"],
                          "to": ["{{Page Path}} contains /thank-you"]}

    def test_only_volatile_fields_differing_is_identical(self):
        from adloop.gtm.read import diff_container_versions

        old = _version("1", tags=[_tag("1", "A")])
        new = _version("2", tags=[{**_tag("1", "A"), "fingerprint": "x",
                                   "tagManagerUrl": "u"}])
        assert diff_container_versions(old, new)["is_identical"] is True


class TestDiffVersions:
    def test_default_compares_live_with_the_version_before_it(self, read_fake):
        from adloop.gtm import read

        read_fake.live = _version("10", tags=[_tag("1", "A"), _tag("2", "B")])
        read_fake.headers = [
            {"containerVersionId": "11", "name": "newer draft version"},
            {"containerVersionId": "10"},
            {"containerVersionId": "9", "deleted": True},
            {"containerVersionId": "8"},
            {"containerVersionId": "3"},
        ]
        read_fake.versions_by_id["8"] = _version("8", tags=[_tag("1", "A")])

        out = read.diff_versions(_read_config(), account_id="1", container_id="2")

        assert out["from_version"] == {"container_version_id": "8", "name": "v8",
                                       "is_live": False}
        assert out["to_version"]["is_live"] is True
        assert out["tags"]["added"][0]["name"] == "B"
        assert out["api_calls"] == 3
        assert [c[0] for c in read_fake.calls] == [
            "versions.live", "version_headers.list", "versions.get"]
        assert "does not mark which versions were published" in out["note"]

    def test_explicit_ids_use_two_gets_and_nothing_else(self, read_fake):
        from adloop.gtm import read

        read_fake.versions_by_id["4"] = _version("4")
        read_fake.versions_by_id["6"] = _version("6", tags=[_tag("1", "A")])
        out = read.diff_versions(_read_config(), account_id="1", container_id="2",
                                 from_version_id="4", to_version_id="6")
        assert [c[0] for c in read_fake.calls] == ["versions.get", "versions.get"]
        assert out["api_calls"] == 2
        assert "is_live" not in out["to_version"]
        assert "note" not in out

    def test_explicit_from_against_live(self, read_fake):
        from adloop.gtm import read

        read_fake.live = _version("10")
        read_fake.versions_by_id["4"] = _version("4")
        out = read.diff_versions(_read_config(), account_id="1", container_id="2",
                                 from_version_id="4")
        assert [c[0] for c in read_fake.calls] == ["versions.live", "versions.get"]
        assert out["is_identical"] is True

    def test_no_older_version_is_an_error(self, read_fake):
        from adloop.gtm import read

        read_fake.live = _version("1")
        read_fake.headers = [{"containerVersionId": "1"}]
        out = read.diff_versions(_read_config(), account_id="1", container_id="2")
        assert "No version older than 1" in out["error"]

    def test_unpublished_container_is_an_error(self, read_fake):
        from adloop.gtm import read

        read_fake.live = {}
        out = read.diff_versions(_read_config(), account_id="1", container_id="2")
        assert "no live" in out["error"]
