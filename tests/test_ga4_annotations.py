"""Tests for GA4 annotations: listing, report attachment, drafts and applies."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adloop.config import AdLoopConfig, GA4Config, SafetyConfig
from adloop.ga4 import annotations
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ga4=GA4Config(property_id="properties/123456"), safety=SafetyConfig())


def _date(text):
    if not text:
        return SimpleNamespace(year=0, month=0, day=0)
    d = date.fromisoformat(text)
    return SimpleNamespace(year=d.year, month=d.month, day=d.day)


def _annotation(n, title, start, end="", color="BLUE", description="", system=False):
    single = not end or end == start
    return SimpleNamespace(
        name=f"properties/123456/reportingDataAnnotations/{n}", title=title, description=description,
        color=SimpleNamespace(name=color), system_generated=system,
        annotation_date=_date(start if single else ""),
        annotation_date_range=SimpleNamespace(start_date=_date("" if single else start), end_date=_date("" if single else end)),
    )


def _client(*items):
    client = MagicMock()
    client.list_reporting_data_annotations.return_value = list(items)
    by_name = {a.name: a for a in items}
    client.get_reporting_data_annotation.side_effect = lambda name: by_name[name]
    return client


def _with(client):
    return patch("adloop.ga4.client.get_admin_alpha_client", return_value=client)


RELAUNCH = _annotation(1, "Site relaunch", "2026-10-03", description="New checkout")
SALE = _annotation(2, "Autumn sale", "2026-10-20", "2026-10-26", color="GREEN")
GOOGLE = _annotation(3, "Data outage", "2026-09-01", color="ORANGE", system=True)


class TestList:
    def test_newest_first_with_plain_dates(self, config):
        with _with(_client(RELAUNCH, SALE, GOOGLE)):
            result = annotations.list_ga4_annotations(config, property_id="123456")
        assert [a["title"] for a in result["annotations"]] == ["Autumn sale", "Site relaunch", "Data outage"]
        assert result["annotations"][0] == {
            "annotation": "properties/123456/reportingDataAnnotations/2", "title": "Autumn sale",
            "color": "GREEN", "start_date": "2026-10-20", "end_date": "2026-10-26",
        }
        assert result["annotations"][1]["date"] == "2026-10-03"
        assert result["annotations"][2]["system_generated"] is True

    def test_date_range_keeps_overlapping_ones(self, config):
        with _with(_client(RELAUNCH, SALE, GOOGLE)):
            result = annotations.list_ga4_annotations(
                config, property_id="123456", date_range_start="2026-10-25", date_range_end="2026-10-31",
            )
        assert [a["title"] for a in result["annotations"]] == ["Autumn sale"]

    def test_api_errors_come_back_as_a_message(self, config):
        client = MagicMock()
        client.list_reporting_data_annotations.side_effect = RuntimeError("404 Method not found")
        with _with(client):
            result = annotations.list_ga4_annotations(config, property_id="123456")
        assert result == {"error": "GA4 annotations API (v1alpha) error: 404 Method not found"}


class TestReportAttachment:
    def test_time_series_report_carries_annotations_in_its_dates(self, config):
        from adloop.ga4 import reports

        response = SimpleNamespace(
            dimension_headers=[SimpleNamespace(name="date")], metric_headers=[SimpleNamespace(name="sessions")],
            rows=[], row_count=0, metadata=SimpleNamespace(time_zone="Europe/Berlin"),
        )
        data = MagicMock()
        data.run_report.return_value = response
        with patch("adloop.ga4.client.get_data_client", return_value=data), _with(_client(RELAUNCH, SALE)):
            result = reports.run_ga4_report(
                config, property_id="properties/123456", dimensions=["date"], metrics=["sessions"],
                date_range_start="2026-10-01", date_range_end="2026-10-10", include_annotations=True,
            )
        assert result["annotations"] == [
            {"title": "Site relaunch", "color": "BLUE", "description": "New checkout", "date": "2026-10-03"},
        ]

    def test_reports_without_a_timeline_or_without_the_flag_make_no_extra_call(self, config):
        from adloop.ga4 import reports

        response = SimpleNamespace(dimension_headers=[], metric_headers=[], rows=[], row_count=0)
        data = MagicMock()
        data.run_report.return_value = response
        alpha = _client(RELAUNCH)
        with patch("adloop.ga4.client.get_data_client", return_value=data), _with(alpha):
            reports.run_ga4_report(config, property_id="properties/123456", dimensions=["country"],
                                   metrics=["sessions"], include_annotations=True)
            reports.run_ga4_report(config, property_id="properties/123456", dimensions=["date"], metrics=["sessions"])
        alpha.list_reporting_data_annotations.assert_not_called()

    def test_relative_dates_resolve(self):
        today = date(2026, 11, 8)
        assert annotations.resolve_date("today", today) == today
        assert annotations.resolve_date("yesterday", today) == date(2026, 11, 7)
        assert annotations.resolve_date("28daysAgo", today) == date(2026, 10, 11)
        assert annotations.resolve_date("2026-01-02", today) == date(2026, 1, 2)
        assert annotations.resolve_date("someday", today) is None


class TestDrafts:
    def test_create_preview(self, config):
        with _with(_client(RELAUNCH)):
            preview = annotations.draft_ga4_annotation(
                config, property_id="123456", title="Price change", date_="2026-11-01", color="red",
            )
        assert preview["operation"] == "ga4_create_annotation"
        assert preview["platform"] == "ga4"
        assert preview["changes"] == {
            "property_id": "123456", "title": "Price change", "description": "", "color": "RED",
            "start_date": "2026-11-01", "end_date": "2026-11-01",
        }

    def test_duplicate_create_is_warned_about(self, config):
        with _with(_client(RELAUNCH)):
            preview = annotations.draft_ga4_annotation(
                config, property_id="123456", title="Site relaunch", date_="2026-10-03",
            )
        assert "already exists" in preview["warnings"][0]

    @pytest.mark.parametrize("kwargs, message", [
        ({"title": "x" * 61, "date_": "2026-11-01"}, "allows 60"),
        ({"title": "ok", "date_": "2026-11-01", "description": "y" * 151}, "allows 150"),
        ({"title": "ok", "date_": "2026-11-01", "color": "ORANGE"}, "reserved"),
        ({"title": "ok", "date_": "01.11.2026"}, "YYYY-MM-DD"),
        ({"title": "ok", "date_": "2026-11-05", "end_date": "2026-11-01"}, "not be before"),
        ({"title": "ok"}, "date is required"),
        ({"action": "delete"}, "annotation is required"),
        ({"action": "rename"}, "action must be"),
    ])
    def test_validation(self, config, kwargs, message):
        result = annotations.draft_ga4_annotation(config, property_id="123456", **kwargs)
        assert message in str(result)

    def test_update_shows_before_and_after_of_changed_fields_only(self, config):
        with _with(_client(RELAUNCH)):
            preview = annotations.draft_ga4_annotation(
                config, property_id="123456", action="update", annotation="1", title="Site relaunch v2",
            )
        changes = preview["changes"]
        assert preview["operation"] == "ga4_update_annotation"
        assert changes["title"] == "Site relaunch v2"
        assert changes["current_title"] == "Site relaunch"
        assert "color" not in changes and "current_color" not in changes
        assert changes["after"]["description"] == "New checkout"

    def test_googles_own_annotations_are_refused(self, config):
        with _with(_client(GOOGLE)):
            result = annotations.draft_ga4_annotation(config, property_id="123456", action="delete", annotation="3")
        assert "created by Google" in result["error"]

    def test_delete_needs_double_confirmation(self, config):
        with _with(_client(SALE)):
            preview = annotations.draft_ga4_annotation(config, property_id="123456", action="delete", annotation="2")
        assert preview["requires_double_confirm"] is True
        assert preview["operation"] == "ga4_delete_annotation"

    def test_unchanged_update_needs_no_plan(self, config):
        with _with(_client(RELAUNCH)):
            result = annotations.draft_ga4_annotation(
                config, property_id="123456", action="update", annotation="1", title="Site relaunch",
            )
        assert result["status"] == "NO_CHANGE"

    def test_respects_blocked_operations(self, config):
        config.safety.blocked_operations = ["ga4_delete_annotation"]
        result = annotations.draft_ga4_annotation(config, property_id="123456", action="delete", annotation="1")
        assert "blocked" in result["error"]


class TestApply:
    def test_create_sends_a_single_day_or_a_range(self, config):
        client = _client()
        client.create_reporting_data_annotation.return_value = RELAUNCH
        with _with(client):
            annotations._apply_create(config, {
                "property_id": "123456", "title": "Sale", "description": "", "color": "GREEN",
                "start_date": "2026-10-20", "end_date": "2026-10-26",
            })
        sent = client.create_reporting_data_annotation.call_args.kwargs["reporting_data_annotation"]
        assert sent.annotation_date_range.start_date.day == 20
        assert sent.annotation_date_range.end_date.day == 26
        assert sent.color.name == "GREEN"

    def test_update_masks_only_changed_fields_and_refuses_stale_plans(self, config):
        client = _client(RELAUNCH)
        client.update_reporting_data_annotation.return_value = RELAUNCH
        with _with(client):
            preview = annotations.draft_ga4_annotation(
                config, property_id="123456", action="update", annotation="1", color="RED", date_="2026-10-04",
            )
            annotations._apply_update(config, preview["changes"])
        mask = list(client.update_reporting_data_annotation.call_args.kwargs["update_mask"].paths)
        assert mask == ["color", "annotation_date", "annotation_date_range"]

        stale = _annotation(1, "Renamed in the GA4 UI", "2026-10-03", description="New checkout")
        with _with(_client(stale)), pytest.raises(RuntimeError, match="changed in GA4 after the preview"):
            annotations._apply_update(config, preview["changes"])

    def test_dry_run_through_confirm_and_apply(self, config):
        from adloop.ads import write as ads_write

        with _with(_client(SALE)):
            preview = annotations.draft_ga4_annotation(config, property_id="123456", action="delete", annotation="2")
            result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"] == {"annotation_unchanged": True}
        assert "Google Analytics" in result["message"]
