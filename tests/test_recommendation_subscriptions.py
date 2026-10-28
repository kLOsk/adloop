"""Auto-apply recommendation subscriptions: read, and pause only (#107)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from adloop.ads import recommendation_subscriptions as subs
from adloop.ads import write
from adloop.ads.validate_only import ValidateOnlyClient
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_ads_write import _FakeClient

CID = "1234567890"
TODAY = date(2026, 10, 27)


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id=CID),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")),
    )


def _row(rec_type, status, modified="2026-01-05 10:00:00"):
    return {
        "recommendation_subscription.resource_name": (
            f"customers/{CID}/recommendationSubscriptions/{rec_type}"
        ),
        "recommendation_subscription.type": rec_type,
        "recommendation_subscription.status": status,
        "recommendation_subscription.create_date_time": "2025-12-01 09:00:00",
        "recommendation_subscription.modify_date_time": modified,
    }


@pytest.fixture
def account(monkeypatch):
    rows = [
        _row("USE_BROAD_MATCH_KEYWORD", "ENABLED", modified="2026-10-20 08:30:00"),
        _row("KEYWORD", "ENABLED"),
        _row("RESPONSIVE_SEARCH_AD", "PAUSED"),
    ]
    queries: list[str] = []

    def execute_query(_config, _cid, query):
        queries.append(query)
        return list(rows)

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)
    return SimpleNamespace(rows=rows, queries=queries)


class TestRead:
    def test_lists_every_subscription_with_dates(self, config, account):
        result = subs.get_recommendation_subscriptions(config, customer_id=CID, today=TODAY)

        assert [s["type"] for s in result["subscriptions"]] == [
            "KEYWORD", "RESPONSIVE_SEARCH_AD", "USE_BROAD_MATCH_KEYWORD",
        ]
        assert result["subscriptions"][0] == {
            "type": "KEYWORD",
            "status": "ENABLED",
            "resource_name": f"customers/{CID}/recommendationSubscriptions/KEYWORD",
            "create_date_time": "2025-12-01 09:00:00",
            "modify_date_time": "2026-01-05 10:00:00",
        }
        assert result["enabled_count"] == 2
        assert "FROM recommendation_subscription" in account.queries[0]

    def test_insights_name_enabled_types_and_recent_changes(self, config, account):
        insights = subs.get_recommendation_subscriptions(
            config, customer_id=CID, today=TODAY
        )["insights"]

        assert insights[0].startswith(
            "2 recommendation type(s) auto-apply without a preview: KEYWORD, USE_BROAD_MATCH_KEYWORD."
        )
        # Only the one modified within RECENT_DAYS is flagged as a change.
        assert len(insights) == 2
        assert insights[1].startswith("USE_BROAD_MATCH_KEYWORD auto-apply was turned on or changed")

    def test_nothing_auto_applied(self, config, account):
        account.rows[:] = [_row("KEYWORD", "PAUSED")]

        result = subs.get_recommendation_subscriptions(config, customer_id=CID, today=TODAY)

        assert result["insights"] == ["No recommendation type is set to auto-apply."]

    def test_an_unparseable_date_is_not_flagged(self, config, account):
        account.rows[:] = [_row("KEYWORD", "ENABLED", modified="")]

        insights = subs.get_recommendation_subscriptions(
            config, customer_id=CID, today=TODAY
        )["insights"]

        assert len(insights) == 1


class TestDraft:
    def test_preview_lists_what_will_be_paused(self, config, account):
        result = subs.draft_recommendation_subscriptions(
            config, customer_id=CID, recommendation_types=["use_broad_match_keyword", "KEYWORD", "KEYWORD"],
        )

        assert result["operation"] == "pause_recommendation_subscriptions"
        assert result["requires_double_confirm"] is False
        changes = result["changes"]
        assert changes["count"] == 2
        assert changes["subscriptions"][0] == {
            "type": "USE_BROAD_MATCH_KEYWORD",
            "resource_name": f"customers/{CID}/recommendationSubscriptions/USE_BROAD_MATCH_KEYWORD",
            "current_status": "ENABLED",
            "status": "PAUSED",
            "modify_date_time": "2026-10-20 08:30:00",
        }

    @pytest.mark.parametrize("rec_type", ["RESPONSIVE_SEARCH_AD", "SITELINK_ASSET"])
    def test_already_paused_or_not_subscribed_is_refused_by_name(self, config, account, rec_type):
        result = subs.draft_recommendation_subscriptions(
            config, customer_id=CID, recommendation_types=["KEYWORD", rec_type],
        )

        assert result["error"] == "Validation failed"
        assert rec_type in result["details"][0]
        assert "KEYWORD," not in result["details"][0]

    @pytest.mark.parametrize("bad", [[], [""], ["keyword; DROP"]])
    def test_bad_input_is_refused_before_any_read(self, config, bad):
        # The suite-wide no_live_gaql fixture raises if anything is read.
        result = subs.draft_recommendation_subscriptions(
            config, customer_id=CID, recommendation_types=bad,
        )

        assert result["error"] == "Validation failed"

    def test_there_is_no_way_to_ask_for_enabled(self):
        import inspect

        params = inspect.signature(subs.draft_recommendation_subscriptions).parameters
        assert set(params) == {"config", "customer_id", "recommendation_types"}

    def test_blocked_operation_is_refused(self, account):
        config = AdLoopConfig(
            ads=AdsConfig(customer_id=CID),
            safety=SafetyConfig(blocked_operations=["pause_recommendation_subscriptions"]),
        )

        result = subs.draft_recommendation_subscriptions(
            config, customer_id=CID, recommendation_types=["KEYWORD"],
        )

        assert "error" in result and "Validation failed" not in result["error"]


class _SubscriptionService:
    def __init__(self):
        self.requests = []

    def mutate_recommendation_subscription(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=op.update.resource_name) for op in request.operations]
        )


def _changes(*types, status="PAUSED"):
    return {"subscriptions": [
        {"type": t, "resource_name": f"customers/{CID}/recommendationSubscriptions/{t}",
         "current_status": "ENABLED", "status": status}
        for t in types
    ]}


class TestApply:
    def test_one_request_pausing_every_type_under_a_status_mask(self):
        service = _SubscriptionService()
        client = _FakeClient({"RecommendationSubscriptionService": service})

        result = subs._apply_pause_recommendation_subscriptions(
            client, CID, _changes("KEYWORD", "USE_BROAD_MATCH_KEYWORD")
        )

        (request,) = service.requests
        assert request.customer_id == CID
        paused = client.enums.RecommendationSubscriptionStatusEnum.PAUSED
        for operation in request.operations:
            assert operation.update.status == paused
            assert list(operation.update_mask.paths) == ["status"]
        assert result["paused_count"] == 2

    def test_a_tampered_plan_still_only_pauses(self):
        service = _SubscriptionService()
        client = _FakeClient({"RecommendationSubscriptionService": service})

        subs._apply_pause_recommendation_subscriptions(client, CID, _changes("KEYWORD", status="ENABLED"))

        (operation,) = service.requests[0].operations
        assert operation.update.status == client.enums.RecommendationSubscriptionStatusEnum.PAUSED

    def test_the_dry_run_sends_it_validate_only(self):
        service = _SubscriptionService()
        client = ValidateOnlyClient(_FakeClient({"RecommendationSubscriptionService": service}))

        subs._apply_pause_recommendation_subscriptions(client, CID, _changes("KEYWORD"))

        assert service.requests[0].validate_only is True
        assert client.validated_calls == 1

    def test_each_operation_updates_the_planned_resource(self):
        service = _SubscriptionService()
        client = _FakeClient({"RecommendationSubscriptionService": service})
        changes = _changes("KEYWORD", "USE_BROAD_MATCH_KEYWORD")

        subs._apply_pause_recommendation_subscriptions(client, CID, changes)

        operations = service.requests[0].operations
        assert [op._pb.WhichOneof("operation") for op in operations] == ["update", "update"]
        assert [op.update.resource_name for op in operations] == [
            sub["resource_name"] for sub in changes["subscriptions"]
        ]

    def test_the_operation_is_dispatched(self):
        service = _SubscriptionService()
        plan = SimpleNamespace(
            operation="pause_recommendation_subscriptions",
            apply_payload=lambda: _changes("KEYWORD"),
        )

        result = write._dispatch_ads_plan(
            _FakeClient({"RecommendationSubscriptionService": service}), CID, plan
        )

        assert result["paused_count"] == 1


class TestTools:
    @pytest.mark.asyncio
    async def test_annotations_and_documented_parameters(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        read = tools["get_recommendation_subscriptions"]
        draft = tools["draft_recommendation_subscriptions"]
        assert read.annotations.read_only_hint is True
        assert draft.annotations.read_only_hint is not True
        assert read.parameters["properties"]["customer_id"].get("description")
        for param in ("recommendation_types", "customer_id"):
            assert draft.parameters["properties"][param].get("description"), param
        assert "status" not in draft.parameters["properties"]


class TestDryRunRoundTrip:
    def test_confirm_and_apply_dry_run_sends_the_pause_validate_only(self, config, account, monkeypatch):
        """Draft -> confirm_and_apply(dry_run=True) through the real validate-only path."""
        service = _SubscriptionService()
        monkeypatch.setattr(
            write, "_validate_with_google",
            lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
        )
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client",
            lambda _config: _FakeClient({"RecommendationSubscriptionService": service}),
        )
        preview = subs.draft_recommendation_subscriptions(
            config, customer_id=CID, recommendation_types=["KEYWORD"],
        )

        result = write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"] == {"validated_calls": 1, "skipped_calls": 0}
        (request,) = service.requests
        assert request.validate_only is True
        (operation,) = request.operations
        assert operation._pb.WhichOneof("operation") == "update"
        assert operation.update.resource_name == (
            f"customers/{CID}/recommendationSubscriptions/KEYWORD"
        )
        assert operation.update.status == (
            _FakeClient({}).enums.RecommendationSubscriptionStatusEnum.PAUSED
        )
