"""Campaign-level broad match on update_campaign (#106).

BROAD makes every keyword in the campaign match as broad, so turning it on
follows the BROAD-keyword rule (Smart Bidding only) and needs a double
confirmation. UNSPECIFIED turns it off, the safe direction.
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


def _current(monkeypatch, *, match_type="UNSPECIFIED", bidding="MAXIMIZE_CONVERSIONS", found=True):
    def execute_query(_config, _cid, query):
        if "keyword_match_type" not in query:
            # update_campaign's shared budget / portfolio read for a bidding change.
            return [{"campaign.id": CAMPAIGN, "campaign.campaign_budget": "",
                     "campaign.bidding_strategy": ""}]
        if not found:
            return []
        return [{"campaign.keyword_match_type": match_type,
                 "campaign.bidding_strategy_type": bidding}]

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)


def _update(config, **kwargs):
    return write.update_campaign(config, customer_id="1234567890", campaign_id=CAMPAIGN, **kwargs)


class TestTurningItOn:
    @pytest.mark.parametrize("bidding", sorted(write._SMART_BIDDING_STRATEGIES))
    def test_smart_bidding_allows_it_with_a_double_confirmation(self, config, monkeypatch, bidding):
        _current(monkeypatch, bidding=bidding)

        result = _update(config, keyword_match_type="broad")

        assert result["changes"]["current_keyword_match_type"] == "UNSPECIFIED"
        assert result["changes"]["keyword_match_type"] == "BROAD"
        assert result["requires_double_confirm"] is True
        warning = next(w for w in result["warnings"] if "CONVERTS" in w)
        assert "shows the originals as Removed" in warning
        assert "does not restore them" in warning
        assert "AI Max" in warning

    @pytest.mark.parametrize("bidding", ["MANUAL_CPC", "TARGET_SPEND"])
    def test_refused_without_smart_bidding(self, config, monkeypatch, bidding):
        _current(monkeypatch, bidding=bidding)

        result = _update(config, keyword_match_type="BROAD")

        assert result["error"] == "Validation failed"
        assert f"this campaign uses {bidding}" in result["details"][0]

    def test_a_smart_bidding_switch_in_the_same_plan_counts(self, config, monkeypatch):
        _current(monkeypatch, bidding="TARGET_SPEND")

        result = _update(config, keyword_match_type="BROAD", bidding_strategy="MAXIMIZE_CONVERSIONS")

        assert result["changes"]["keyword_match_type"] == "BROAD"
        assert result["changes"]["bidding_strategy"] == "MAXIMIZE_CONVERSIONS"
        assert result["requires_double_confirm"] is True

    def test_a_non_smart_switch_in_the_same_plan_is_refused(self, config, monkeypatch):
        _current(monkeypatch, bidding="MAXIMIZE_CONVERSIONS")

        result = _update(config, keyword_match_type="BROAD", bidding_strategy="MANUAL_CPC")

        assert result["error"] == "Validation failed"

    def test_already_broad_is_not_a_widening_change(self, config, monkeypatch):
        _current(monkeypatch, match_type="BROAD")

        result = _update(config, keyword_match_type="BROAD")

        assert result["requires_double_confirm"] is False
        assert "warnings" not in result


class TestTurningItOff:
    def test_single_confirmation_and_says_keywords_stay_broad(self, config, monkeypatch):
        _current(monkeypatch, match_type="BROAD", bidding="MAXIMIZE_CONVERSIONS")

        result = _update(config, keyword_match_type="UNSPECIFIED")

        assert result["changes"]["current_keyword_match_type"] == "BROAD"
        assert result["changes"]["keyword_match_type"] == "UNSPECIFIED"
        assert result["requires_double_confirm"] is False
        (warning,) = result["warnings"]
        assert "keywords already converted stay broad match" in warning

    def test_already_off_says_nothing(self, config, monkeypatch):
        _current(monkeypatch)

        assert "warnings" not in _update(config, keyword_match_type="UNSPECIFIED")


class TestLeavingSmartBidding:
    @pytest.mark.parametrize("bidding", ["MANUAL_CPC", "TARGET_SPEND"])
    def test_a_broad_campaign_cannot_leave_smart_bidding_alone(self, config, monkeypatch, bidding):
        _current(monkeypatch, match_type="BROAD")

        result = _update(config, bidding_strategy=bidding)

        assert result["error"] == "Validation failed"
        assert 'keyword_match_type="UNSPECIFIED"' in result["details"][0]

    def test_allowed_when_the_same_call_turns_broad_off(self, config, monkeypatch):
        _current(monkeypatch, match_type="BROAD")

        result = _update(config, bidding_strategy="MANUAL_CPC", keyword_match_type="UNSPECIFIED")

        assert result["changes"]["bidding_strategy"] == "MANUAL_CPC"
        assert result["changes"]["keyword_match_type"] == "UNSPECIFIED"
        assert any("Under MANUAL_CPC" in w for w in result["warnings"])

    def test_a_campaign_without_broad_moves_freely(self, config, monkeypatch):
        _current(monkeypatch)

        result = _update(config, bidding_strategy="MANUAL_CPC")

        assert result["changes"]["bidding_strategy"] == "MANUAL_CPC"


class TestValidation:
    def test_other_values_are_refused_before_any_read(self, config):
        # The suite-wide no_live_gaql fixture raises if anything is read.
        result = _update(config, keyword_match_type="EXACT")

        assert result["error"] == "Validation failed"
        assert any("BROAD" in d and "UNSPECIFIED" in d for d in result["details"])

    def test_unknown_campaign(self, config, monkeypatch):
        _current(monkeypatch, found=False)

        assert _update(config, keyword_match_type="UNSPECIFIED") == {
            "error": f"Campaign {CAMPAIGN} was not found in this account"
        }


def _row(bidding, match_type):
    from types import SimpleNamespace

    return SimpleNamespace(campaign=SimpleNamespace(
        bidding_strategy_type=SimpleNamespace(name=bidding),
        keyword_match_type=SimpleNamespace(name=match_type),
    ))


class _ReadingService(_FakeGoogleAdsService):
    def __init__(self, row):
        super().__init__([_FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/1")])
        self.row = row
        self.queries = []

    def search(self, customer_id, query):
        self.queries.append(query)
        return [self.row]


def _client(service):
    return _FakeClient({"GoogleAdsService": service, "CampaignService": _FakePathService("campaigns")})


class TestApplyRecheck:
    def test_broad_on_is_refused_if_bidding_moved_off_smart_since_the_preview(self):
        service = _ReadingService(_row("MANUAL_CPC", "UNSPECIFIED"))

        with pytest.raises(RuntimeError, match="nothing was sent"):
            write._apply_update_campaign(_client(service), "1234567890", {
                "campaign_id": CAMPAIGN, "keyword_match_type": "BROAD",
            })

        assert service.operations is None

    def test_leaving_smart_bidding_is_refused_if_broad_came_on_since_the_preview(self):
        service = _ReadingService(_row("MAXIMIZE_CONVERSIONS", "BROAD"))

        with pytest.raises(RuntimeError, match="UNSPECIFIED"):
            write._apply_update_campaign(_client(service), "1234567890", {
                "campaign_id": CAMPAIGN, "bidding_strategy": "MANUAL_CPC",
            })

        assert service.operations is None

    def test_still_smart_goes_through(self):
        service = _ReadingService(_row("TARGET_CPA", "UNSPECIFIED"))

        write._apply_update_campaign(_client(service), "1234567890", {
            "campaign_id": CAMPAIGN, "keyword_match_type": "BROAD",
        })

        assert "campaign.bidding_strategy_type" in service.queries[0]
        assert list(service.operations[0].campaign_operation.update_mask.paths) == ["keyword_match_type"]

    def test_turning_it_off_needs_no_read(self):
        # _FakeGoogleAdsService.search raises: nothing to re-check here.
        service = _FakeGoogleAdsService(
            [_FakeMutateOperationResponse("campaign_result", "customers/1/campaigns/1")]
        )

        write._apply_update_campaign(_client(service), "1234567890", {
            "campaign_id": CAMPAIGN, "keyword_match_type": "UNSPECIFIED",
        })


class TestApply:
    @pytest.mark.parametrize("value", ["BROAD", "UNSPECIFIED"])
    def test_update_sets_the_field_under_its_mask(self, value):
        service = _ReadingService(_row("MAXIMIZE_CONVERSIONS", "UNSPECIFIED"))
        client = _client(service)

        write._apply_update_campaign(client, "1234567890", {
            "campaign_id": CAMPAIGN,
            "current_keyword_match_type": "BROAD" if value == "UNSPECIFIED" else "UNSPECIFIED",
            "keyword_match_type": value,
        })

        operation = service.operations[0].campaign_operation
        assert list(operation.update_mask.paths) == ["keyword_match_type"]
        assert operation.update.keyword_match_type == getattr(
            client.enums.CampaignKeywordMatchTypeEnum, value
        )


@pytest.mark.asyncio
async def test_update_campaign_documents_the_parameter():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["update_campaign"].parameters["properties"]["keyword_match_type"].get("description")
    assert "keyword_match_type" not in tools["draft_campaign"].parameters["properties"]
