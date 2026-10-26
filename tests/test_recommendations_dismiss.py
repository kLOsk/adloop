"""Tests for draft_dismiss_recommendations and its dry run / apply."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import recommendations as rec
from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig

CUSTOMER = "1234567890"
BROAD = "3401876612"
BUDGET = "3401876613"
KEYWORD = "3401876614"


def _resource(rec_id: str, customer: str = CUSTOMER) -> str:
    return f"customers/{customer}/recommendations/{rec_id}"


def _row(rec_id: str, rec_type: str, campaign: str = "11") -> dict:
    """One row as get_recommendations returns it."""
    return {
        "recommendation.id": rec_id,
        "recommendation.resource_name": _resource(rec_id),
        "recommendation.type": rec_type,
        "recommendation.campaign": f"customers/{CUSTOMER}/campaigns/{campaign}",
        "recommendation.ad_group": "",
        "recommendation.dismissed": False,
        "estimated_improvement": {"clicks": 40.0, "cost": 120.5, "conversions": 0.4},
    }


ACTIVE = [
    _row(BROAD, "USE_BROAD_MATCH_KEYWORD"),
    _row(BUDGET, "CAMPAIGN_BUDGET"),
    _row(KEYWORD, "KEYWORD", campaign="12"),
]


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id=CUSTOMER))


@pytest.fixture
def active(monkeypatch):
    """Patch the live read; tests edit the list to simulate changes."""
    rows = list(ACTIVE)
    calls: list[str] = []

    def fake_get_recommendations(_config, *, customer_id="", **_kwargs):
        calls.append(customer_id)
        return {"recommendations": list(rows)}

    monkeypatch.setattr("adloop.ads.read.get_recommendations", fake_get_recommendations)
    return SimpleNamespace(rows=rows, calls=calls)


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


class TestDraft:
    def test_preview_names_each_recommendation_with_its_impact(self, config, active):
        result = rec.draft_dismiss_recommendations(
            config, recommendation_ids=[BROAD, BUDGET]
        )

        assert result["operation"] == "dismiss_recommendations"
        assert result["requires_double_confirm"] is False
        planned = result["changes"]["recommendations"]
        assert [r["recommendation_id"] for r in planned] == [BROAD, BUDGET]
        assert planned[0]["type"] == "USE_BROAD_MATCH_KEYWORD"
        assert planned[0]["resource_name"] == _resource(BROAD)
        assert planned[0]["campaign"].endswith("/campaigns/11")
        assert planned[0]["estimated_improvement"]["clicks"] == 40.0
        assert result["changes"]["count"] == 2
        assert "show a dismissed recommendation again" in result["changes"]["note"]

    def test_accepts_resource_names_and_collapses_duplicates(self, config, active):
        result = rec.draft_dismiss_recommendations(
            config, recommendation_ids=[_resource(KEYWORD), KEYWORD, " ", KEYWORD]
        )

        assert [r["recommendation_id"] for r in result["changes"]["recommendations"]] == [
            KEYWORD
        ]

    def test_unknown_or_already_dismissed_ids_are_refused_by_name(self, config, active):
        result = rec.draft_dismiss_recommendations(
            config, recommendation_ids=[BROAD, "999"]
        )

        assert result["error"] == "Validation failed"
        assert "999" in result["details"][0]
        assert "already dismissed" in result["details"][0]

    def test_a_resource_name_from_another_account_is_refused(self, config, active):
        result = rec.draft_dismiss_recommendations(
            config, recommendation_ids=[_resource(BROAD, customer="5555555555")]
        )

        assert result["error"] == "Validation failed"
        assert "5555555555" in result["details"][0]
        assert active.calls == [], "nothing may be read for invalid input"

    @pytest.mark.parametrize("bad", ["abc def", "1/2", "x' OR 1=1", "customers/1/campaigns/2"])
    def test_malformed_ids_are_refused_before_any_read(self, config, active, bad):
        result = rec.draft_dismiss_recommendations(config, recommendation_ids=[bad])

        assert result["error"] == "Validation failed"
        assert active.calls == []

    def test_an_empty_list_is_refused(self, config, active):
        result = rec.draft_dismiss_recommendations(config, recommendation_ids=[])

        assert result == {
            "error": "Validation failed",
            "details": ["At least one recommendation_id is required"],
        }

    def test_blocked_operation_is_refused(self, active):
        config = AdLoopConfig(
            ads=AdsConfig(customer_id=CUSTOMER),
            safety=SafetyConfig(blocked_operations=["dismiss_recommendations"]),
        )

        result = rec.draft_dismiss_recommendations(config, recommendation_ids=[BROAD])

        assert "blocked" in result["error"]


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_rereads_instead_of_sending_to_google(self, config, active, monkeypatch):
        def never(*_args, **_kwargs):
            raise AssertionError("DismissRecommendation has no validate-only mode")

        monkeypatch.setattr(write, "_validate_with_google", never)
        plan_id = rec.draft_dismiss_recommendations(
            config, recommendation_ids=[BROAD, BUDGET]
        )["plan_id"]

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"] == {"recommendations_checked": 2, "still_active": 2}
        assert "no validate-only mode for dismissing recommendations" in result["note"]
        assert len(active.calls) == 2  # draft + dry run

    def test_dry_run_fails_when_a_recommendation_went_away(self, config, active):
        plan_id = rec.draft_dismiss_recommendations(
            config, recommendation_ids=[BROAD, BUDGET]
        )["plan_id"]
        active.rows[:] = [r for r in active.rows if r["recommendation.id"] != BUDGET]

        result = write.confirm_and_apply(config, plan_id=plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_FAILED"
        assert BUDGET in result["error"]
        assert BROAD not in result["error"]
        assert "re-checked the target against Google Ads" in result["message"]

    def test_the_validate_only_client_still_refuses_the_call(self):
        """Fail closed: even if a dry run reached the applier, nothing is dismissed."""
        from adloop.ads.validate_only import ValidateOnlyClient, ValidateOnlyFailure

        dismissed: list = []
        inner = SimpleNamespace(
            get_service=lambda _name: SimpleNamespace(
                dismiss_recommendation=lambda **kw: dismissed.append(kw)
            )
        )
        service = ValidateOnlyClient(inner).get_service("RecommendationService")

        with pytest.raises(ValidateOnlyFailure, match="no validate-only mode"):
            service.dismiss_recommendation(request=None)
        assert dismissed == []


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class _DismissClient:
    """Real proto types, a fake RecommendationService."""

    def __init__(self, *, fail_index: int | None = None):
        base = GoogleAdsClient(
            credentials=None, developer_token="test-token",
            use_proto_plus=True, version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.request = None
        self._fail_index = fail_index

    def get_service(self, name):
        assert name == "RecommendationService"
        return SimpleNamespace(dismiss_recommendation=self._dismiss)

    def _dismiss(self, request=None):
        self.request = request
        results = [
            SimpleNamespace(resource_name="" if i == self._fail_index else op.resource_name)
            for i, op in enumerate(request.operations)
        ]
        error = (
            SimpleNamespace(code=3, message="Recommendation not found.", details=[])
            if self._fail_index is not None
            else None
        )
        return SimpleNamespace(results=results, partial_failure_error=error)


def _changes(*ids: str) -> dict:
    return {
        "recommendations": [
            {"recommendation_id": i, "resource_name": _resource(i)} for i in ids
        ]
    }


class TestApply:
    def test_sends_one_partial_failure_request_with_every_resource_name(self):
        client = _DismissClient()

        result = rec._apply_dismiss_recommendations(client, CUSTOMER, _changes(BROAD, BUDGET))

        assert client.request.customer_id == CUSTOMER
        assert client.request.partial_failure is True
        assert [op.resource_name for op in client.request.operations] == [
            _resource(BROAD), _resource(BUDGET)
        ]
        assert result == {
            "dismissed": [_resource(BROAD), _resource(BUDGET)],
            "dismissed_count": 2,
        }

    def test_a_failed_operation_is_reported_by_recommendation_id(self):
        client = _DismissClient(fail_index=1)

        result = rec._apply_dismiss_recommendations(client, CUSTOMER, _changes(BROAD, BUDGET))

        assert result["dismissed"] == [_resource(BROAD)]
        assert result["partial_failure"] is True
        assert result["failed"][0]["recommendation_id"] == BUDGET
        assert result["partial_failure_message"] == "Recommendation not found."

    def test_the_operation_is_dispatched(self):
        client = _DismissClient()
        plan = SimpleNamespace(
            operation="dismiss_recommendations",
            apply_payload=lambda: _changes(KEYWORD),
        )

        result = write._dispatch_ads_plan(client, CUSTOMER, plan)

        assert result["dismissed_count"] == 1


# ---------------------------------------------------------------------------
# get_recommendations exposes the ID
# ---------------------------------------------------------------------------


class TestReadExposesId:
    def test_rows_carry_id_and_resource_name(self, config, monkeypatch):
        from adloop.ads import read

        metrics = SimpleNamespace(impressions=0, clicks=0, cost_micros=0, conversions=0)
        proto = SimpleNamespace(
            recommendation=SimpleNamespace(
                resource_name=_resource(BROAD),
                type_=SimpleNamespace(name="USE_BROAD_MATCH_KEYWORD"),
                campaign=f"customers/{CUSTOMER}/campaigns/11",
                ad_group="",
                dismissed=False,
                impact=SimpleNamespace(base_metrics=metrics, potential_metrics=metrics),
            )
        )
        client = SimpleNamespace(
            get_service=lambda _name: SimpleNamespace(search=lambda **_kw: [proto])
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: client)

        rows = read.get_recommendations(config, customer_id=CUSTOMER)["recommendations"]

        assert rows[0]["recommendation.id"] == BROAD
        assert rows[0]["recommendation.resource_name"] == _resource(BROAD)


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_write_annotations_and_documented_parameters(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        tool = tools["draft_dismiss_recommendations"]
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.parameters["properties"]["recommendation_ids"].get("description")
        assert "recommendation.id" in tools["get_recommendations"].description
