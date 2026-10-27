"""Campaign URL options on draft_campaign and update_campaign (#105).

tracking_url_template, final_url_suffix and url_custom_parameters. None leaves
a value unchanged, an empty string or list clears it, and the update preview
shows the current values plus an example click URL.
"""

from __future__ import annotations

import pytest

from adloop.ads import write
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_ads_write import (
    _FakeClient,
    _FakeGoogleAdsService,
    _FakeMutateOperationResponse,
    _FakePathService,
)

CAMPAIGN = "1001"
TEMPLATE = "{lpurl}?utm_source=google&utm_medium=cpc&utm_content={_grp}"


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="1234567890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), max_daily_budget=500),
    )


def _current(monkeypatch, *, template="", suffix="", params=(), final_url="https://example.com/landing",
             found=True):
    queries: list[str] = []

    def execute_query(_config, _cid, query):
        queries.append(query)
        if "FROM ad_group_ad" in query:
            return [{"ad_group_ad.ad.final_urls": [final_url]}] if final_url else []
        if not found:
            return []
        return [{
            "campaign.tracking_url_template": template,
            "campaign.final_url_suffix": suffix,
            "campaign.url_custom_parameters": list(params),
        }]

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)
    return queries


def _update(config, **kwargs):
    return write.update_campaign(config, customer_id="1234567890", campaign_id=CAMPAIGN, **kwargs)


def _draft(config, **kwargs):
    return write.draft_campaign(
        config,
        customer_id="1234567890",
        campaign_name="Search",
        daily_budget=20,
        bidding_strategy="TARGET_SPEND",
        geo_target_ids=["2840"],
        language_ids=["1000"],
        **kwargs,
    )


class TestValidation:
    @pytest.mark.parametrize("tag", [
        "{lpurl}", "{LPURL+2}", "{lpurl+3}", "{unescapedlpurl}", "{escapedlpurl}",
        "{escapedlpurl+2}", "{escapedlpurl+3}", "{unescapedlpurl+2}",
    ])
    def test_any_lpurl_variant_is_accepted(self, config, tag):
        result = _draft(config, tracking_url_template=f"https://t.example/?u={tag}")

        assert result["changes"]["tracking_url_template"] == f"https://t.example/?u={tag}"

    @pytest.mark.parametrize("tag", ["{lpurl+4}", "{lpurl2}", "{escaped_lpurl}"])
    def test_lookalike_tags_are_refused(self, config, tag):
        result = _draft(config, tracking_url_template=f"https://t.example/?u={tag}")

        assert result["error"] == "Validation failed"

    def test_the_example_fills_every_variant(self):
        example = write._example_click_urls(
            "https://example.com/p", {"tracking_url_template": "https://t.example/?u={escapedlpurl+2}"}
        )

        assert example["tracking_url"] == "https://t.example/?u=https://example.com/p"

    def test_a_template_without_lpurl_is_refused(self, config):
        result = _draft(config, tracking_url_template="https://example.com/?utm_source=google")

        assert result["error"] == "Validation failed"
        assert any("{lpurl}" in d for d in result["details"])

    def test_custom_parameter_lengths(self, config):
        result = _draft(config, url_custom_parameters=[
            {"key": "k" * 9, "value": "v"},
            {"key": "my-key", "value": "v"},
            {"key": "ok", "value": "v" * 251},
            {"key": "", "value": "x"},
        ])

        details = result["details"]
        assert any("'kkkkkkkkk' must be alphanumeric, at most 8" in d for d in details)
        assert any("'my-key' must be alphanumeric" in d for d in details)
        assert any("longer than 250" in d for d in details)
        assert any("needs a key and a value" in d for d in details)

    def test_a_leading_underscore_on_a_key_is_dropped(self, config):
        """{_grp} in the template is key "grp"; people often type the underscore."""
        result = _draft(config, url_custom_parameters=[{"key": "_grp", "value": "b2c"}])

        assert result["changes"]["url_custom_parameters"] == [{"key": "grp", "value": "b2c"}]

    def test_invalid_input_never_reads_the_account(self, config):
        # The suite-wide no_live_gaql fixture raises if anything is read.
        result = _update(config, tracking_url_template="https://example.com/")

        assert result["error"] == "Validation failed"


class TestDraftCampaign:
    def test_omitted_leaves_the_preview_unchanged(self, config):
        changes = _draft(config)["changes"]

        for field in ("tracking_url_template", "final_url_suffix", "url_custom_parameters"):
            assert field not in changes

    def test_empty_values_have_nothing_to_clear_on_a_new_campaign(self, config):
        changes = _draft(config, tracking_url_template="", final_url_suffix="",
                         url_custom_parameters=[])["changes"]

        assert "tracking_url_template" not in changes
        assert "url_custom_parameters" not in changes

    def test_all_three_are_planned(self, config):
        changes = _draft(
            config, tracking_url_template=TEMPLATE, final_url_suffix="?gclsrc=aw",
            url_custom_parameters=[{"key": "grp", "value": "b2c"}],
        )["changes"]

        assert changes["tracking_url_template"] == TEMPLATE
        assert changes["final_url_suffix"] == "gclsrc=aw"
        assert changes["url_custom_parameters"] == [{"key": "grp", "value": "b2c"}]


class TestUpdateCampaign:
    def test_preview_shows_current_values_and_an_example_click(self, config, monkeypatch):
        _current(monkeypatch, suffix="a=1")

        result = _update(config, tracking_url_template=TEMPLATE,
                         url_custom_parameters=[{"key": "grp", "value": "b2c"}])

        changes = result["changes"]
        assert changes["current_tracking_url_template"] == ""
        assert changes["tracking_url_template"] == TEMPLATE
        assert changes["current_url_custom_parameters"] == []
        assert "final_url_suffix" not in changes  # not given: unchanged
        assert result["example_click"] == {
            "final_url": "https://example.com/landing",
            "landing_page_url": "https://example.com/landing?a=1",
            "tracking_url": (
                "https://example.com/landing?utm_source=google&utm_medium=cpc&utm_content=b2c"
            ),
        }
        assert result["requires_double_confirm"] is False
        assert "warnings" not in result

    def test_an_empty_value_is_shown_as_a_removal(self, config, monkeypatch):
        _current(monkeypatch, template=TEMPLATE, params=[{"key": "grp", "value": "b2c"}])

        result = _update(config, tracking_url_template="", url_custom_parameters=[])

        assert result["changes"]["tracking_url_template"] == ""
        assert result["changes"]["current_tracking_url_template"] == TEMPLATE
        assert result["changes"]["url_custom_parameters"] == []
        assert any("Removes the campaign's tracking_url_template" in w for w in result["warnings"])
        assert any("Removes the campaign's url_custom_parameters" in w for w in result["warnings"])
        assert "tracking_url" not in result["example_click"]

    def test_clearing_something_already_empty_is_not_a_removal(self, config, monkeypatch):
        _current(monkeypatch)

        result = _update(config, final_url_suffix="")

        assert "warnings" not in result

    def test_no_ads_yet_means_no_example(self, config, monkeypatch):
        _current(monkeypatch, final_url="")

        result = _update(config, final_url_suffix="utm_medium=cpc")

        assert "example_click" not in result
        assert result["changes"]["final_url_suffix"] == "utm_medium=cpc"

    def test_unknown_campaign(self, config, monkeypatch):
        _current(monkeypatch, found=False)

        assert _update(config, final_url_suffix="a=1") == {
            "error": f"Campaign {CAMPAIGN} was not found in this account"
        }


def _client(service):
    return _FakeClient({
        "GoogleAdsService": service,
        "CampaignService": _FakePathService("campaigns"),
        "CampaignBudgetService": _FakePathService("campaignBudgets"),
        "AdGroupService": _FakePathService("adGroups"),
    })


class TestApply:
    def test_create_sets_all_three(self):
        service = _FakeGoogleAdsService([
            _FakeMutateOperationResponse("campaign_budget_result", "customers/1/campaignBudgets/1"),
            _FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/2"),
            _FakeMutateOperationResponse("ad_group_result", "customers/1/adGroups/3"),
        ])

        write._apply_create_campaign(_client(service), "1234567890", {
            "campaign_name": "Search",
            "daily_budget": 20,
            "bidding_strategy": "TARGET_SPEND",
            "channel_type": "SEARCH",
            "geo_target_ids": [],
            "language_ids": [],
            "tracking_url_template": TEMPLATE,
            "final_url_suffix": "gclsrc=aw",
            "url_custom_parameters": [{"key": "grp", "value": "b2c"}],
        })

        campaign = service.operations[1].campaign_operation.create
        assert campaign.tracking_url_template == TEMPLATE
        assert campaign.final_url_suffix == "gclsrc=aw"
        assert [(p.key, p.value) for p in campaign.url_custom_parameters] == [("grp", "b2c")]

    def test_update_masks_only_the_fields_given(self):
        service = _FakeGoogleAdsService(
            [_FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/1")]
        )

        write._apply_update_campaign(_client(service), "1234567890", {
            "campaign_id": CAMPAIGN,
            "current_tracking_url_template": "",
            "tracking_url_template": TEMPLATE,
        })

        operation = service.operations[0].campaign_operation
        assert list(operation.update_mask.paths) == ["tracking_url_template"]
        assert operation.update.tracking_url_template == TEMPLATE

    def test_update_clears_with_empty_values_under_the_mask(self):
        service = _FakeGoogleAdsService(
            [_FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/1")]
        )

        write._apply_update_campaign(_client(service), "1234567890", {
            "campaign_id": CAMPAIGN,
            "tracking_url_template": "",
            "final_url_suffix": "",
            "url_custom_parameters": [],
        })

        operation = service.operations[0].campaign_operation
        assert set(operation.update_mask.paths) == {
            "tracking_url_template", "final_url_suffix", "url_custom_parameters",
        }
        assert operation.update.tracking_url_template == ""
        assert list(operation.update.url_custom_parameters) == []


@pytest.mark.asyncio
async def test_both_tools_document_the_parameters():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("draft_campaign", "update_campaign"):
        properties = tools[name].parameters["properties"]
        for param in ("tracking_url_template", "final_url_suffix", "url_custom_parameters"):
            assert properties[param].get("description"), (name, param)
