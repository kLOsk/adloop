"""Tests for GTM write tools — create / update / delete tags + triggers + publish."""
from __future__ import annotations

import pytest

from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store._pending_plans.clear()
    yield
    preview_store._pending_plans.clear()


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(require_dry_run=True),
    )


# ---------------------------------------------------------------------------
# draft_gtm_tag — creation validation
# ---------------------------------------------------------------------------


class TestDraftGtmTag:
    def test_invalid_tag_type_rejected(self, config):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            name="Bad Tag",
            tag_type="not_a_real_type",
        )
        assert result["error"] == "Validation failed"

    def test_known_tag_types_accepted(self, config):
        """awcc, awud, etc. that the validator now recognizes as valid."""
        from adloop.gtm.write import draft_gtm_tag

        for tag_type in ("googtag", "gclidw", "html", "gaawe", "awcc", "awud"):
            result = draft_gtm_tag(
                config,
                account_id="6228172353",
                container_id="183580785",
                name=f"test-{tag_type}",
                tag_type=tag_type,
            )
            assert "error" not in result, f"{tag_type} rejected: {result}"

    def test_long_name_rejected(self, config):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            name="X" * 201,
            tag_type="html",
        )
        assert result["error"] == "Validation failed"

    def test_persists_parameters_and_triggers(self, config):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            name="GADS - Config - All Pages",
            tag_type="googtag",
            parameters=[{"type": "TEMPLATE", "key": "tagId",
                         "value": "AW-11437481610"}],
            firing_trigger_ids=["2147479573"],
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["name"] == "GADS - Config - All Pages"
        assert plan.changes["type"] == "googtag"
        assert plan.changes["parameters"][0]["value"] == "AW-11437481610"
        assert plan.changes["firing_trigger_ids"] == ["2147479573"]


# ---------------------------------------------------------------------------
# draft_gtm_trigger
# ---------------------------------------------------------------------------


class TestDraftGtmTrigger:
    def test_invalid_trigger_type_rejected(self, config):
        from adloop.gtm.write import draft_gtm_trigger

        result = draft_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            name="Bad Trigger",
            trigger_type="madeup_event",
        )
        assert result["error"] == "Validation failed"

    def test_custom_event_requires_name(self, config):
        from adloop.gtm.write import draft_gtm_trigger

        result = draft_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            name="Custom Event Trigger",
            trigger_type="customEvent",
        )
        assert result["error"] == "Validation failed"
        assert any("custom_event_name" in d for d in result["details"])

    def test_link_click_with_filter(self, config):
        from adloop.gtm.write import draft_gtm_trigger

        result = draft_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            name="TRG - Click to Call - All Pages",
            trigger_type="linkClick",
            filters=[{
                "type": "STARTS_WITH",
                "parameter": [
                    {"type": "TEMPLATE", "key": "arg0",
                     "value": "{{Click URL}}"},
                    {"type": "TEMPLATE", "key": "arg1", "value": "tel:"},
                ],
            }],
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["type"] == "linkClick"
        assert plan.changes["filters"][0]["type"] == "STARTS_WITH"


# ---------------------------------------------------------------------------
# draft_update_gtm_tag — partial update
# ---------------------------------------------------------------------------


class TestDraftUpdateGtmTag:
    def test_tag_id_required(self, config):
        from adloop.gtm.write import draft_update_gtm_tag

        result = draft_update_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            tag_id="",
            name="x",
        )
        assert "tag_id is required" in result["error"]

    def test_no_fields_to_update_rejected(self, config):
        from adloop.gtm.write import draft_update_gtm_tag

        result = draft_update_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            tag_id="17",
        )
        assert "No fields to update" in result["error"]

    def test_invalid_tag_type_rejected(self, config):
        from adloop.gtm.write import draft_update_gtm_tag

        result = draft_update_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            tag_id="17",
            tag_type="not_a_real_type",
        )
        assert result["error"] == "Validation failed"

    def test_partial_update_persists_only_passed_fields(self, config):
        from adloop.gtm.write import draft_update_gtm_tag

        result = draft_update_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            tag_id="17",
            name="GADS - Event - Qualified Call - All Pages",
            paused=False,
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["name"] == "GADS - Event - Qualified Call - All Pages"
        assert plan.changes["paused"] is False
        assert "parameters" not in plan.changes
        assert "firing_trigger_ids" not in plan.changes


# ---------------------------------------------------------------------------
# draft_update_gtm_trigger — partial update
# ---------------------------------------------------------------------------


class TestDraftUpdateGtmTrigger:
    def test_trigger_id_required(self, config):
        from adloop.gtm.write import draft_update_gtm_trigger

        result = draft_update_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            trigger_id="",
            name="x",
        )
        assert "trigger_id is required" in result["error"]

    def test_no_fields_to_update_rejected(self, config):
        from adloop.gtm.write import draft_update_gtm_trigger

        result = draft_update_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            trigger_id="14",
        )
        assert "No fields to update" in result["error"]

    def test_rename_only(self, config):
        from adloop.gtm.write import draft_update_gtm_trigger

        result = draft_update_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            trigger_id="14",
            name="TRG - Submit Form - Contacts",
        )
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.changes["name"] == "TRG - Submit Form - Contacts"
        assert "filters" not in plan.changes


# ---------------------------------------------------------------------------
# draft_delete_gtm_tag / draft_delete_gtm_trigger
# ---------------------------------------------------------------------------


class TestDraftDeleteGtm:
    def test_delete_tag_emits_warning(self, config):
        from adloop.gtm.write import draft_delete_gtm_tag

        result = draft_delete_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            tag_id="17",
        )
        assert "warnings" in result
        assert any("irreversible" in w.lower() for w in result["warnings"])
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "delete_gtm_tag"
        assert plan.entity_id == "17"

    def test_delete_tag_id_required(self, config):
        from adloop.gtm.write import draft_delete_gtm_tag

        result = draft_delete_gtm_tag(
            config,
            account_id="6228172353",
            container_id="183580785",
            tag_id="",
        )
        assert "tag_id is required" in result["error"]

    def test_delete_trigger_emits_warning(self, config):
        from adloop.gtm.write import draft_delete_gtm_trigger

        result = draft_delete_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            trigger_id="14",
        )
        assert "warnings" in result
        assert any("irreversible" in w.lower() for w in result["warnings"])

    def test_delete_trigger_id_required(self, config):
        from adloop.gtm.write import draft_delete_gtm_trigger

        result = draft_delete_gtm_trigger(
            config,
            account_id="6228172353",
            container_id="183580785",
            trigger_id="",
        )
        assert "trigger_id is required" in result["error"]


# ---------------------------------------------------------------------------
# publish_gtm_workspace
# ---------------------------------------------------------------------------


class TestPublishGtmWorkspace:
    def test_publish_emits_irreversible_warning(self, config):
        from adloop.gtm.write import publish_gtm_workspace

        result = publish_gtm_workspace(
            config,
            account_id="6228172353",
            container_id="183580785",
            version_name="Test publish",
        )
        assert "warnings" in result
        assert any("LIVE" in w for w in result["warnings"])
        plan = preview_store._pending_plans[result["plan_id"]]
        assert plan.operation == "publish_gtm_workspace"


# ---------------------------------------------------------------------------
# MCP registration of all 7 GTM-write tools + dispatch wiring
# ---------------------------------------------------------------------------


class TestGtmWriteMCPRegistration:
    @pytest.fixture(scope="class")
    def tools_by_name(self):
        import asyncio
        from adloop.server import mcp

        async def _list():
            return await mcp.list_tools()

        tools = asyncio.run(_list())
        return {t.name: t for t in tools}

    def test_seven_gtm_write_tools_registered(self, tools_by_name):
        for name in (
            "draft_gtm_tag",
            "draft_gtm_trigger",
            "publish_gtm_workspace",
            "draft_update_gtm_tag",
            "draft_update_gtm_trigger",
            "draft_delete_gtm_tag",
            "draft_delete_gtm_trigger",
        ):
            assert name in tools_by_name, f"{name} not registered"

    def test_dispatch_routes_all_gtm_ops(self):
        import inspect
        from adloop.ads import write

        src = inspect.getsource(write._execute_plan)
        for op in (
            "create_gtm_tag", "create_gtm_trigger", "publish_gtm_workspace",
            "update_gtm_tag", "update_gtm_trigger",
            "delete_gtm_tag", "delete_gtm_trigger",
        ):
            assert f'"{op}"' in src, f"dispatch missing {op}"
