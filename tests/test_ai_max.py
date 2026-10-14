"""Tests for the AI Max control tools (read plan, safe-order apply)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import ai_max, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="123-456-7890"))


def _campaign_row(
    campaign_id=16454984318,
    *,
    name="Escape Room München (ohne Display)",
    status="ENABLED",
    channel="SEARCH",
    enable_ai_max=False,
    bundling="NOT_REQUIRED",
    automation=(("TEXT_ASSET_AUTOMATION", "OPTED_IN"),),
    shape="whole",
):
    row = {
        "campaign.id": campaign_id,
        "campaign.name": name,
        "campaign.status": status,
        "campaign.advertising_channel_type": channel,
        "campaign.ai_max_setting.enable_ai_max": enable_ai_max,
        "campaign.ai_max_setting.bundling_required": bundling,
    }
    if automation:
        if shape == "whole":
            # What GAQL returns for the whole message field.
            row["campaign.asset_automation_settings"] = [
                {"asset_automation_type": asset_type, "asset_automation_status": status}
                for asset_type, status in automation
            ]
        else:
            # Sub-field selection shape (kept only as a fallback path).
            types, statuses = zip(*automation)
            row["campaign.asset_automation_settings.asset_automation_type"] = list(types)
            row["campaign.asset_automation_settings.asset_automation_status"] = list(statuses)
    return row


def _ad_group_row(group_id, *, name="Suche", status="ENABLED", disable=False, campaign=16454984318):
    return {
        "campaign.id": campaign,
        "ad_group.id": group_id,
        "ad_group.name": name,
        "ad_group.status": status,
        "ad_group.ai_max_ad_group_setting.disable_search_term_matching": disable,
    }


def _patch_search(monkeypatch, campaign_rows, ad_group_rows):
    """Patch ai_max._search so both the read and the readback use fixtures."""
    def _fake(_service, _cid, query):
        return list(campaign_rows) if "FROM campaign" in query else list(ad_group_rows)

    monkeypatch.setattr(ai_max, "_search", _fake)


def _read_client():
    """Minimal client stub: _search is patched, only get_service matters."""
    return SimpleNamespace(
        get_service=lambda _name: SimpleNamespace(search=lambda **_kwargs: [])
    )


class TestReadState:
    def test_flattens_campaign_and_ad_group_rows(self, monkeypatch):
        _patch_search(
            monkeypatch,
            [_campaign_row(automation=(("TEXT_ASSET_AUTOMATION", "OPTED_IN"),
                                       ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_IN")))],
            [_ad_group_row(111), _ad_group_row(222, status="PAUSED", disable=True)],
        )

        state = ai_max.read_ai_max_state(_read_client(), "1234567890", campaign_id="16454984318")

        assert state["total_campaigns"] == 1
        campaign = state["campaigns"][0]
        assert campaign["campaign_id"] == "16454984318"
        assert campaign["enable_ai_max"] is False
        assert campaign["bundling_required"] == "NOT_REQUIRED"
        assert campaign["asset_automation_settings"] == [
            {"asset_automation_type": "TEXT_ASSET_AUTOMATION",
             "asset_automation_status": "OPTED_IN"},
            {"asset_automation_type": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
             "asset_automation_status": "OPTED_IN"},
        ]
        assert [g["ad_group_id"] for g in campaign["ad_groups"]] == ["111", "222"]
        assert campaign["ad_groups"][1]["disable_search_term_matching"] is True

    def test_rows_for_another_campaign_are_dropped(self, monkeypatch):
        """The draft plans on campaigns[0]; a row for a different campaign must
        never be able to become that first entry."""
        _patch_search(monkeypatch, [_campaign_row(999)], [_ad_group_row(111, campaign=999)])

        state = ai_max.read_ai_max_state(
            _read_client(), "1234567890", campaign_id="16454984318"
        )

        assert state["campaigns"] == []
        assert state["total_ad_groups"] == 0

    def test_read_rejects_a_non_numeric_campaign_id(self):
        with pytest.raises(ValueError, match="numeric"):
            ai_max.read_ai_max_state(_read_client(), "1234567890", campaign_id="abc")

    def test_get_ai_max_settings_validates_the_id_before_gaql(self, config):
        result = ai_max.get_ai_max_settings(config, campaign_id="Escape Room")

        assert result == {"error": "campaign_id must be a numeric ID"}

    def test_reads_the_whole_asset_automation_field(self, monkeypatch):
        _patch_search(
            monkeypatch,
            [_campaign_row(automation=(
                ("TEXT_ASSET_AUTOMATION", "OPTED_IN"),
                ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_OUT"),
            ))],
            [],
        )

        state = ai_max.read_ai_max_state(_read_client(), "1234567890", campaign_id="16454984318")

        settings = state["campaigns"][0]["asset_automation_settings"]
        assert {s["asset_automation_type"]: s["asset_automation_status"] for s in settings} == {
            "TEXT_ASSET_AUTOMATION": "OPTED_IN",
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "OPTED_OUT",
        }

    def test_query_selects_the_message_not_its_subfields(self):
        """Regression: GAQL answers the two sub-fields with
        PROHIBITED_FIELD_IN_SELECT_CLAUSE — only the message field is selectable."""
        query = ai_max._CAMPAIGN_SELECT

        assert "campaign.asset_automation_settings" in query
        assert "campaign.asset_automation_settings.asset_automation_type" not in query
        assert "campaign.asset_automation_settings.asset_automation_status" not in query

    def test_still_handles_the_parallel_subfield_shape(self, monkeypatch):
        _patch_search(
            monkeypatch,
            [_campaign_row(
                automation=(
                    ("TEXT_ASSET_AUTOMATION", "OPTED_IN"),
                    ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_OUT"),
                ),
                shape="subfields",
            )],
            [],
        )

        state = ai_max.read_ai_max_state(_read_client(), "1234567890", campaign_id="16454984318")

        settings = state["campaigns"][0]["asset_automation_settings"]
        assert {s["asset_automation_type"]: s["asset_automation_status"] for s in settings} == {
            "TEXT_ASSET_AUTOMATION": "OPTED_IN",
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "OPTED_OUT",
        }


class TestMergeAssetAutomation:
    def test_keeps_untouched_types(self):
        current = [
            {"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_IN"},
            {"asset_automation_type": "GENERATE_IMAGE_ENHANCEMENT", "asset_automation_status": "OPTED_IN"},
        ]

        merged = ai_max.merge_asset_automation(
            current, {ai_max.TEXT_ASSET_AUTOMATION: "OPTED_OUT"}
        )

        by_type = {m["asset_automation_type"]: m["asset_automation_status"] for m in merged}
        assert by_type["TEXT_ASSET_AUTOMATION"] == "OPTED_OUT"
        assert by_type["GENERATE_IMAGE_ENHANCEMENT"] == "OPTED_IN"

    def test_adds_a_type_that_was_not_there_yet(self):
        merged = ai_max.merge_asset_automation(
            [], {ai_max.FINAL_URL_EXPANSION: "OPTED_OUT"}
        )

        assert merged == [
            {
                "asset_automation_type": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
                "asset_automation_status": "OPTED_OUT",
            }
        ]

    def test_unchanged_leaves_the_list_alone(self):
        current = [{"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_IN"}]

        merged = ai_max.merge_asset_automation(
            current, {ai_max.TEXT_ASSET_AUTOMATION: "UNCHANGED"}
        )

        assert merged == current

    def test_entries_without_a_status_survive_the_merge(self):
        """AUTOMATED_VIDEO_CRAWL carries a nested setting instead of a status."""
        current = [
            {
                "asset_automation_type": "AUTOMATED_VIDEO_CRAWL",
                "automated_video_crawl_setting": {
                    "automated_video_crawl_infos": [{"url": "https://example.com"}]
                },
            }
        ]

        merged = ai_max.merge_asset_automation(
            current, {ai_max.TEXT_ASSET_AUTOMATION: "OPTED_OUT"}
        )

        by_type = {m["asset_automation_type"]: m for m in merged}
        assert by_type["AUTOMATED_VIDEO_CRAWL"]["automated_video_crawl_setting"] == {
            "automated_video_crawl_infos": [{"url": "https://example.com"}]
        }
        assert by_type["TEXT_ASSET_AUTOMATION"]["asset_automation_status"] == "OPTED_OUT"


class TestPlanTargets:
    def _campaign(self, *groups):
        return {"ad_groups": list(groups)}

    def test_default_takes_active_and_paused(self):
        campaign = self._campaign(
            {"ad_group_id": "1", "status": "ENABLED"},
            {"ad_group_id": "2", "status": "PAUSED"},
        )

        targets, warnings = ai_max.plan_targets(
            campaign, disable_search_term_matching=True,
            ad_group_ids=None, include_paused_ad_groups=True,
        )

        assert [t["ad_group_id"] for t in targets] == ["1", "2"]
        assert warnings == []

    def test_paused_can_be_excluded(self):
        campaign = self._campaign(
            {"ad_group_id": "1", "status": "ENABLED"},
            {"ad_group_id": "2", "status": "PAUSED"},
        )

        targets, warnings = ai_max.plan_targets(
            campaign, disable_search_term_matching=True,
            ad_group_ids=None, include_paused_ad_groups=False,
        )

        assert [t["ad_group_id"] for t in targets] == ["1"]
        assert "paused ad group(s) skipped" in warnings[0]

    def test_removed_is_never_targeted(self):
        campaign = self._campaign(
            {"ad_group_id": "1", "status": "ENABLED"},
            {"ad_group_id": "2", "status": "REMOVED"},
        )

        targets, warnings = ai_max.plan_targets(
            campaign, disable_search_term_matching=True,
            ad_group_ids=["1", "2"], include_paused_ad_groups=True,
        )

        assert [t["ad_group_id"] for t in targets] == ["1"]

    def test_unknown_ids_are_reported_not_planned(self):
        campaign = self._campaign({"ad_group_id": "1", "status": "ENABLED"})

        targets, warnings = ai_max.plan_targets(
            campaign, disable_search_term_matching=True,
            ad_group_ids=["1", "999"], include_paused_ad_groups=True,
        )

        assert [t["ad_group_id"] for t in targets] == ["1"]
        assert "999" in warnings[0]


# ---------------------------------------------------------------------------
# Draft plans
# ---------------------------------------------------------------------------


def _draft(config, monkeypatch, *, campaign_rows=None, ad_group_rows=None, **kwargs):
    _patch_search(
        monkeypatch,
        campaign_rows
        if campaign_rows is not None
        else [_campaign_row(automation=(("TEXT_ASSET_AUTOMATION", "OPTED_OUT"),))],
        ad_group_rows if ad_group_rows is not None else [_ad_group_row(111), _ad_group_row(222, status="PAUSED")],
    )
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: _read_client())
    return write.draft_ai_max_settings(config, campaign_id="16454984318", **kwargs)


class TestDraftAiMaxSettings:
    def test_plans_both_switches_with_before_values(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            enable_ai_max=True,
            disable_search_term_matching=True,
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["operation"] == "update_ai_max_settings"
        changes = result["changes"]
        assert changes["enable_ai_max"] is True
        assert changes["ai_max_before"] is False
        assert changes["disable_search_term_matching"] is True
        assert [g["ad_group_id"] for g in changes["ad_groups"]] == ["111", "222"]
        assert changes["ad_groups"][1]["before"] is False
        assert changes["ad_groups"][1]["after"] is True

    def test_turning_ai_max_off_is_planned_too(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(enable_ai_max=True)],
            enable_ai_max=False,
        )

        assert result["changes"]["enable_ai_max"] is False
        assert result["changes"]["ai_max_before"] is True

    def test_unchanged_switches_are_omitted(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(
                enable_ai_max=True,
                automation=(("TEXT_ASSET_AUTOMATION", "OPTED_OUT"),),
            )],
            disable_search_term_matching=False,
        )

        assert "enable_ai_max" not in result["changes"]
        assert result["changes"]["disable_search_term_matching"] is False
        # Turning matching back on under AI Max is explicit, so it asks twice.
        assert result["requires_double_confirm"] is True

    def test_refuses_ai_max_while_matching_stays_on_unmentioned(
        self, config, monkeypatch
    ):
        """Naming only enable_ai_max leaves the ad group switch unnoticed."""
        result = _draft(config, monkeypatch, enable_ai_max=True)

        assert "Refusing" in result["error"]
        assert any("search term matching" in d for d in result["details"])
        assert "disable_search_term_matching=false" in result["hint"]
        assert "plan_id" not in result

    def test_refuses_a_campaign_automation_that_is_simply_left_on(
        self, config, monkeypatch
    ):
        """The safe state must also cover automations nobody asked about."""
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(automation=(
                ("TEXT_ASSET_AUTOMATION", "OPTED_IN"),
                ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_OUT"),
            ))],
            enable_ai_max=True,
            disable_search_term_matching=True,
        )

        assert "Refusing" in result["error"]
        assert any(
            "TEXT_ASSET_AUTOMATION" in d and "text_asset_automation" in d
            for d in result["details"]
        )
        assert not any("FINAL_URL_EXPANSION" in d for d in result["details"])

    def test_no_refusal_and_no_double_confirm_when_both_are_set(
        self, config, monkeypatch
    ):
        result = _draft(
            config, monkeypatch,
            enable_ai_max=True,
            disable_search_term_matching=True,
        )

        assert result["requires_double_confirm"] is False
        assert not result["changes"].get("warnings")

    def test_explicit_switches_to_ai_max_with_automation_are_allowed(
        self, config, monkeypatch
    ):
        """Someone who really wants AI Max automating must be able to say so."""
        result = _draft(
            config, monkeypatch,
            enable_ai_max=True,
            disable_search_term_matching=False,
            text_asset_automation="OPTED_IN",
            final_url_expansion="OPTED_IN",
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["requires_double_confirm"] is True
        warnings = result["changes"]["warnings"]
        assert any("disable_search_term_matching=false" in w for w in warnings)
        assert any("TEXT_ASSET_AUTOMATION" in w for w in warnings)
        assert any("FINAL_URL_EXPANSION" in w for w in warnings)

    def test_explicit_opt_in_needs_a_second_confirmation_only(
        self, config, monkeypatch
    ):
        result = _draft(
            config, monkeypatch,
            enable_ai_max=True,
            disable_search_term_matching=True,
            text_asset_automation="OPTED_IN",
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["requires_double_confirm"] is True
        assert result["changes"]["ad_groups"][0]["after"] is True

    def test_enabling_ai_max_on_a_campaign_without_ad_groups_is_not_a_risk(
        self, config, monkeypatch
    ):
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(automation=(("TEXT_ASSET_AUTOMATION", "OPTED_OUT"),))],
            ad_group_rows=[],
            enable_ai_max=True,
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["requires_double_confirm"] is False

    def test_groups_left_out_on_purpose_are_a_warning_not_a_refusal(
        self, config, monkeypatch
    ):
        """``include_paused_ad_groups=false`` is a decision the caller made.

        Refusing it with "pass disable_search_term_matching=true" (which they
        did pass) would be both wrong and unhelpful.
        """
        result = _draft(
            config, monkeypatch,
            enable_ai_max=True,
            disable_search_term_matching=True,
            include_paused_ad_groups=False,
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["requires_double_confirm"] is True
        warning = " ".join(result["changes"]["warnings"])
        assert "outside the selection" in warning
        assert "include_paused_ad_groups=true" in warning

    def test_an_explicit_ad_group_selection_warns_about_the_others(
        self, config, monkeypatch
    ):
        result = _draft(
            config, monkeypatch,
            enable_ai_max=True,
            disable_search_term_matching=True,
            ad_group_ids=["111"],
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["requires_double_confirm"] is True
        assert "outside the selection" in " ".join(result["changes"]["warnings"])

    def test_automation_left_on_while_ai_max_stays_off_is_not_a_risk(
        self, config, monkeypatch
    ):
        """The rule is about AI Max on; automations alone are untouched."""
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(automation=(("TEXT_ASSET_AUTOMATION", "OPTED_IN"),))],
            final_url_expansion="OPTED_IN",
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["requires_double_confirm"] is False

    def test_asset_automation_keeps_other_types(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(automation=(
                ("TEXT_ASSET_AUTOMATION", "OPTED_IN"),
                ("GENERATE_IMAGE_ENHANCEMENT", "OPTED_IN"),
            ))],
            text_asset_automation="OPTED_OUT",
        )

        changes = result["changes"]
        assert changes["asset_automation_changed"] == ["TEXT_ASSET_AUTOMATION"]
        full = changes["_asset_automation_settings_full"]
        by_type = {item["asset_automation_type"]: item["asset_automation_status"] for item in full}
        assert by_type[ai_max.TEXT_ASSET_AUTOMATION] == "OPTED_OUT"
        assert by_type["GENERATE_IMAGE_ENHANCEMENT"] == "OPTED_IN"

    def test_final_url_expansion_uses_the_v25_type_name(self, config, monkeypatch):
        result = _draft(config, monkeypatch, final_url_expansion="OPTED_OUT")

        assert result["changes"]["asset_automation_changed"] == [
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"
        ]

    def test_explicit_ad_group_selection(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            disable_search_term_matching=True,
            ad_group_ids=["222"],
        )

        assert [g["ad_group_id"] for g in result["changes"]["ad_groups"]] == ["222"]

    def test_paused_can_be_left_out(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            disable_search_term_matching=True,
            include_paused_ad_groups=False,
        )

        assert [g["ad_group_id"] for g in result["changes"]["ad_groups"]] == ["111"]

    def test_wrong_channel_type_is_refused(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(channel="PERFORMANCE_MAX")],
            enable_ai_max=True,
        )

        assert "are for Search campaigns" in result["error"]

    def test_removed_campaign_is_refused(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(status="REMOVED")],
            enable_ai_max=True,
        )

        assert "REMOVED" in result["error"]

    def test_unknown_campaign_is_refused(self, config, monkeypatch):
        result = _draft(config, monkeypatch, campaign_rows=[], enable_ai_max=True)

        assert "was not found" in result["error"]

    def test_a_row_for_another_campaign_cannot_be_planned(self, config, monkeypatch):
        """Defence in depth: the read drops foreign rows, so the draft reports
        the campaign as not found instead of planning the wrong one."""
        result = _draft(
            config, monkeypatch,
            campaign_rows=[_campaign_row(999)],
            ad_group_rows=[_ad_group_row(111, campaign=999)],
            enable_ai_max=True,
        )

        assert "was not found" in result["error"]
        assert "plan_id" not in result

    def test_nothing_to_change_is_refused(self, config, monkeypatch):
        result = _draft(config, monkeypatch)

        assert result["error"] == "Validation failed"
        assert "Nothing to change" in " ".join(result["details"])

    def test_unknown_automation_value_is_refused(self, config, monkeypatch):
        result = _draft(config, monkeypatch, text_asset_automation="MAYBE")

        assert "text_asset_automation" in " ".join(result["details"])

    def test_blocked_operation_is_refused(self, monkeypatch):
        blocked = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(blocked_operations=["update_ai_max_settings"]),
        )
        result = write.draft_ai_max_settings(
            blocked, campaign_id="16454984318", enable_ai_max=True
        )

        assert "blocked by configuration" in result["error"]

    def test_prepare_brand_exclusions_plans_the_standard_state(self, config, monkeypatch):
        _patch_search(
            monkeypatch,
            [_campaign_row(automation=(("TEXT_ASSET_AUTOMATION", "OPTED_IN"),
                                       ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", "OPTED_IN")))],
            [_ad_group_row(111)],
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: _read_client())

        result = write.draft_prepare_brand_exclusions(
            config, campaign_id="16454984318"
        )

        changes = result["changes"]
        assert result["purpose"] == "brand_exclusions"
        assert changes["enable_ai_max"] is True
        assert changes["disable_search_term_matching"] is True
        assert set(changes["asset_automation_changed"]) == {
            "TEXT_ASSET_AUTOMATION",
            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
        }
        full = {i["asset_automation_type"]: i["asset_automation_status"]
                for i in changes["_asset_automation_settings_full"]}
        assert full["TEXT_ASSET_AUTOMATION"] == "OPTED_OUT"
        assert full["FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"] == "OPTED_OUT"

    def test_draft_does_not_mutate(self, config, monkeypatch):
        calls = []
        _patch_search(
            monkeypatch,
            [_campaign_row(automation=(("TEXT_ASSET_AUTOMATION", "OPTED_OUT"),))],
            [_ad_group_row(111)],
        )
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client",
            lambda _config: SimpleNamespace(
                get_service=lambda name: calls.append(name) or SimpleNamespace()
            ),
        )

        result = write.draft_ai_max_settings(
            config,
            campaign_id="16454984318",
            enable_ai_max=True,
            disable_search_term_matching=True,
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        # Only the read service is touched while planning.
        assert set(calls) == {"GoogleAdsService"}


# ---------------------------------------------------------------------------
# Applier: safe order, partial failure, rollback, readback
# ---------------------------------------------------------------------------


class _FakeMutateClient:
    """Real proto types with fake mutate services; records call order."""

    def __init__(self, *, fail_ad_group_ids=(), fail_campaign=False):
        base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.order: list[str] = []
        self.ad_group_requests = []
        self.campaign_operations = []
        self._fail_ad_group_ids = set(fail_ad_group_ids)
        self._fail_campaign = fail_campaign
        self._services = {
            "AdGroupService": SimpleNamespace(
                ad_group_path=lambda cid, gid: f"customers/{cid}/adGroups/{gid}",
                mutate_ad_groups=self._mutate_ad_groups,
            ),
            "CampaignService": SimpleNamespace(
                campaign_path=lambda cid, cid2: f"customers/{cid}/campaigns/{cid2}",
                mutate_campaigns=self._mutate_campaigns,
            ),
            "GoogleAdsService": SimpleNamespace(search=lambda **_kwargs: []),
        }

    def get_service(self, name):
        return self._services[name]

    def _mutate_ad_groups(self, request):
        self.order.append("ad_groups")
        self.ad_group_requests.append(request)
        results = []
        for operation in request.operations:
            resource = operation.update.resource_name
            group_id = resource.rsplit("/", 1)[-1]
            results.append(
                SimpleNamespace(resource_name="" if group_id in self._fail_ad_group_ids else resource)
            )
        return SimpleNamespace(results=results, partial_failure_error=None)

    def _mutate_campaigns(self, customer_id=None, operations=None):
        self.order.append("campaign")
        if self._fail_campaign:
            raise RuntimeError("campaign rejected by the API")
        self.campaign_operations.extend(operations)
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=operations[0].update.resource_name)]
        )


def _changes(**overrides):
    changes = {
        "campaign_id": "16454984318",
        "enable_ai_max": True,
        "ai_max_before": False,
        "disable_search_term_matching": True,
        "ad_groups": [
            {"ad_group_id": "111", "ad_group_name": "Suche", "status": "ENABLED",
             "before": False, "after": True},
            {"ad_group_id": "222", "ad_group_name": "Pause", "status": "PAUSED",
             "before": False, "after": True},
        ],
        "asset_automation_changed": ["TEXT_ASSET_AUTOMATION"],
        "_asset_automation_settings_full": [
            {"asset_automation_type": "TEXT_ASSET_AUTOMATION",
             "asset_automation_status": "OPTED_OUT"},
            {"asset_automation_type": "GENERATE_IMAGE_ENHANCEMENT",
             "asset_automation_status": "OPTED_IN"},
        ],
    }
    changes.update(overrides)
    return changes


class TestApplyAiMaxSettings:
    def test_ad_groups_are_mutated_before_the_campaign(self, monkeypatch):
        client = _FakeMutateClient()
        _patch_search(monkeypatch, [_campaign_row(enable_ai_max=True)], [_ad_group_row(111, disable=True)])

        result = write._apply_ai_max_settings(client, "1234567890", _changes())

        assert client.order == ["ad_groups", "campaign"]
        assert result["completed_steps"] == ["ad_groups", "campaign"]
        assert result["readback"]["campaigns"][0]["enable_ai_max"] is True

    def test_ad_group_mutation_uses_the_v25_field(self):
        client = _FakeMutateClient()

        write._apply_ai_max_settings(client, "1234567890", _changes())

        operation = client.ad_group_requests[0].operations[0]
        assert operation.update.resource_name == "customers/1234567890/adGroups/111"
        assert operation.update.ai_max_ad_group_setting.disable_search_term_matching is True
        assert list(operation.update_mask.paths) == [
            "ai_max_ad_group_setting.disable_search_term_matching"
        ]

    def test_campaign_mutation_carries_both_paths_and_the_full_list(self):
        client = _FakeMutateClient()

        write._apply_ai_max_settings(client, "1234567890", _changes())

        operation = client.campaign_operations[0]
        assert list(operation.update_mask.paths) == [
            "ai_max_setting.enable_ai_max",
            "asset_automation_settings",
        ]
        assert operation.update.ai_max_setting.enable_ai_max is True
        settings = operation.update.asset_automation_settings
        assert len(settings) == 2  # untouched type still travels along
        assert settings[0].asset_automation_status == client.enums.AssetAutomationStatusEnum.OPTED_OUT

    def test_ad_group_failure_blocks_the_campaign_step_and_rolls_back(self, monkeypatch):
        client = _FakeMutateClient(fail_ad_group_ids={"222"})
        _patch_search(monkeypatch, [_campaign_row()], [_ad_group_row(111)])

        result = write._apply_ai_max_settings(client, "1234567890", _changes())

        assert "campaign" not in client.order
        assert result["failed_step"] == "ad_groups"
        assert result["partial_failure"] is True
        assert "AI Max stays as it was" in result["message"]
        assert result["rollback"]["attempted"] is True
        assert result["rollback"]["restored"] == ["111"]
        # the rollback restores the previous value, it does not repeat the target
        rollback_op = client.ad_group_requests[-1].operations[0]
        assert rollback_op.update.ai_max_ad_group_setting.disable_search_term_matching is False

    def test_campaign_failure_rolls_the_ad_groups_back(self, monkeypatch):
        client = _FakeMutateClient(fail_campaign=True)
        _patch_search(monkeypatch, [_campaign_row()], [_ad_group_row(111), _ad_group_row(222)])

        result = write._apply_ai_max_settings(client, "1234567890", _changes())

        assert result["failed_step"] == "campaign"
        assert result["partial_failure"] is True
        assert "campaign rejected" in result["error"]
        assert set(result["rollback"]["restored"]) == {"111", "222"}
        assert "readback" in result

    def test_only_ad_group_changes_skip_the_campaign_call(self):
        client = _FakeMutateClient()

        write._apply_ai_max_settings(
            client,
            "1234567890",
            _changes(enable_ai_max=None, asset_automation_changed=None,
                     _asset_automation_settings_full=None),
        )

        assert client.order == ["ad_groups"]

    def test_only_campaign_changes_skip_the_ad_group_call(self):
        client = _FakeMutateClient()

        write._apply_ai_max_settings(
            client, "1234567890",
            _changes(disable_search_term_matching=None, ad_groups=[]),
        )

        assert client.order == ["campaign"]

    def test_clean_run_reports_no_partial_failure(self, monkeypatch):
        client = _FakeMutateClient()
        _patch_search(monkeypatch, [_campaign_row(enable_ai_max=True)], [_ad_group_row(111, disable=True)])

        result = write._apply_ai_max_settings(client, "1234567890", _changes())

        assert "partial_failure" not in result
        assert result["ad_group_mutation"]["count"] == 2

    def test_status_less_and_nested_settings_are_sent_back_intact(self):
        """A replace-style update must not lose settings the tool did not touch."""
        client = _FakeMutateClient()

        write._apply_ai_max_settings(
            client,
            "1234567890",
            _changes(
                enable_ai_max=None,
                disable_search_term_matching=None,
                ad_groups=[],
                asset_automation_changed=["TEXT_ASSET_AUTOMATION"],
                _asset_automation_settings_full=[
                    {"asset_automation_type": "TEXT_ASSET_AUTOMATION",
                     "asset_automation_status": "OPTED_OUT"},
                    {
                        "asset_automation_type": "AUTOMATED_VIDEO_CRAWL",
                        "automated_video_crawl_setting": {
                            "automated_video_crawl_infos": [
                                {"url": "https://example.com", "source_platform": "YOUTUBE",
                                 "enabled": True}
                            ]
                        },
                    },
                ],
            ),
        )

        settings = client.campaign_operations[0].update.asset_automation_settings
        assert len(settings) == 2
        text_entry, crawl_entry = settings
        assert text_entry.asset_automation_type == client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION
        assert text_entry.asset_automation_status == client.enums.AssetAutomationStatusEnum.OPTED_OUT
        assert crawl_entry.asset_automation_type == client.enums.AssetAutomationTypeEnum.AUTOMATED_VIDEO_CRAWL
        infos = crawl_entry.automated_video_crawl_setting.automated_video_crawl_infos
        assert len(infos) == 1
        assert infos[0].url == "https://example.com"
        assert infos[0].source_platform == client.enums.VideoCrawlSourcePlatformEnum.YOUTUBE
        assert infos[0].enabled is True


class TestAiMaxToolRegistration:
    @pytest.mark.asyncio
    async def test_annotations_and_tags(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        assert tools["get_ai_max_settings"].annotations.read_only_hint is True
        for name in ("draft_ai_max_settings", "draft_prepare_brand_exclusions"):
            assert tools[name].annotations.read_only_hint is False, name
            assert tools[name].annotations.destructive_hint is False, name
        for name in ("get_ai_max_settings", "draft_ai_max_settings",
                     "draft_prepare_brand_exclusions"):
            assert set(tools[name].tags) - {"ads:read", "ads:write", "ads:plan"} == {"ads"}, name

    @pytest.mark.asyncio
    async def test_schema_documents_the_parameters(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        properties = tools["draft_ai_max_settings"].parameters["properties"]
        for param in ("campaign_id", "enable_ai_max", "disable_search_term_matching",
                      "ad_group_ids", "include_paused_ad_groups",
                      "text_asset_automation", "final_url_expansion"):
            assert properties[param].get("description"), param

        final_url = properties["final_url_expansion"]["description"]
        assert "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION" in final_url
