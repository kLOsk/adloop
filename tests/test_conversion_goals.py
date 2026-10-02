"""Tests for the conversion goal tools (read config, plan biddability)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import conversion_goals as cg
from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="123-456-7890"))


def _customer_goal(category="PURCHASE", origin="WEBSITE", biddable=True):
    return SimpleNamespace(
        customer_conversion_goal=SimpleNamespace(
            category=category, origin=origin, biddable=biddable
        )
    )


def _campaign_goal(campaign_id=16454984318, *, name="Suche", category="PURCHASE",
                   origin="WEBSITE", biddable=True):
    return SimpleNamespace(
        campaign=SimpleNamespace(id=campaign_id, name=name),
        campaign_conversion_goal=SimpleNamespace(
            category=category, origin=origin, biddable=biddable
        ),
    )


def _goal_config(campaign_id=16454984318, *, name="Suche", level="CUSTOMER", custom=None):
    return SimpleNamespace(
        campaign=SimpleNamespace(id=campaign_id, name=name),
        conversion_goal_campaign_config=SimpleNamespace(
            goal_config_level=level, custom_conversion_goal=custom
        ),
    )


def _custom_goal(goal_id=7, name="Käufe", status="ENABLED"):
    return SimpleNamespace(
        custom_conversion_goal=SimpleNamespace(id=goal_id, name=name, status=status)
    )


class _ReadClient:
    """GoogleAdsService stub that answers per resource, optionally failing one."""

    def __init__(self, *, fail_part: str = ""):
        self._fail_part = fail_part
        self.queries: list[str] = []
        self._fixtures = {
            "customer_conversion_goal": [
                _customer_goal("PURCHASE", "WEBSITE", True),
                _customer_goal("SIGNUP", "WEBSITE", False),
            ],
            "campaign_conversion_goal": [
                _campaign_goal(16454984318, category="PURCHASE", biddable=True),
                _campaign_goal(16454984318, category="SIGNUP", biddable=False),
            ],
            "conversion_goal_campaign_config": [
                _goal_config(16454984318, level="CAMPAIGN", custom="Set 1")
            ],
            "custom_conversion_goal": [_custom_goal()],
        }

    def _search(self, customer_id, query):
        self.queries.append(query)
        for key, rows in self._fixtures.items():
            if f"FROM {key}" in query:
                if self._fail_part == key:
                    raise RuntimeError(f"query failed for {key}")
                return rows
        raise AssertionError(f"unexpected query: {query}")

    def get_service(self, name):
        if name == "GoogleAdsService":
            return SimpleNamespace(search=self._search)
        return SimpleNamespace(
            mutate_customer_conversion_goals=self._mutate_customer,
            mutate_campaign_conversion_goals=self._mutate_campaign,
        )

    def _mutate_customer(self, request=None, **kwargs):
        self.customer_request = request
        return SimpleNamespace(results=[SimpleNamespace(resource_name="ok") for _ in request.operations])

    def _mutate_campaign(self, request=None, **kwargs):
        self.campaign_request = request
        return SimpleNamespace(results=[SimpleNamespace(resource_name="ok") for _ in request.operations])


class TestReadConversionGoals:
    def test_reads_all_four_parts(self):
        client = _ReadClient()

        state = cg.read_conversion_goals(client, "1586230693")

        assert [g["category"] for g in state["customer_goals"]] == ["PURCHASE", "SIGNUP"]
        assert state["customer_goals"][1]["biddable"] is False
        assert state["total_customer_goals"] == 2
        campaign = state["campaigns"][0]
        assert campaign["campaign_id"] == "16454984318"
        assert len(campaign["goals"]) == 2
        assert campaign["goal_config"]["goal_config_level"] == "CAMPAIGN"
        assert campaign["goal_config"]["custom_conversion_goal"] == "Set 1"
        assert state["custom_goals"] == [{"id": "7", "name": "Käufe", "status": "ENABLED"}]
        assert state["errors"] == []

    def test_one_failing_query_does_not_hide_the_rest(self):
        client = _ReadClient(fail_part="custom_conversion_goal")

        state = cg.read_conversion_goals(client, "1586230693")

        assert state["total_customer_goals"] == 2
        assert state["custom_goals"] == []
        assert state["errors"][0]["part"] == "custom_goals"
        assert "query failed" in state["errors"][0]["error"]

    def test_campaign_filter_is_passed_through(self):
        client = _ReadClient()

        cg.read_conversion_goals(client, "1586230693", campaign_id="16454984318")

        assert any("WHERE campaign.id = 16454984318" in q for q in client.queries)


class TestGoalPlanning:
    def test_pairs_each_goal_with_its_current_value(self):
        current = [{"category": "PURCHASE", "origin": "WEBSITE", "biddable": True}]

        changes, warnings = cg.plan_goal_changes(
            current, [{"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}]
        )

        assert changes == [
            {"category": "PURCHASE", "origin": "WEBSITE", "biddable": False, "before": True}
        ]
        assert warnings == []

    def test_unknown_pair_is_reported(self):
        changes, unknown = cg.plan_goal_changes(
            [], [{"category": "STORE_VISIT", "origin": "STORE", "biddable": True}]
        )

        assert changes[0]["before"] is None
        assert unknown == ["STORE_VISIT/STORE"]

    def test_resource_names_use_the_documented_formats(self):
        assert cg.goal_resource_name("1586230693", "customer", "PURCHASE", "WEBSITE") == (
            "customers/1586230693/customerConversionGoals/PURCHASE~WEBSITE"
        )
        assert cg.goal_resource_name(
            "1586230693", "campaign", "SIGNUP", "WEBSITE", campaign_id="16454984318"
        ) == (
            "customers/1586230693/campaignConversionGoals/16454984318~SIGNUP~WEBSITE"
        )


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


def _draft(config, monkeypatch, *, client=None, **kwargs):
    state_client = client or _ReadClient()
    monkeypatch.setattr(
        "adloop.ads.client.get_ads_client", lambda _config: state_client
    )
    return write.draft_conversion_goal_settings(config, **kwargs)


class TestDraftConversionGoalSettings:
    def test_customer_level_plan_shows_before_and_after(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            level="customer",
            goals=[{"category": "purchase", "origin": "website", "biddable": False}],
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["operation"] == "update_conversion_goals"
        assert result["changes"]["level"] == "customer"
        assert result["changes"]["goals"] == [
            {"category": "PURCHASE", "origin": "WEBSITE", "biddable": False, "before": True}
        ]
        assert "campaign_id" not in result["changes"]

    def test_campaign_level_plan_keeps_the_campaign(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            level="campaign",
            campaign_id="16454984318",
            goals=[{"category": "SIGNUP", "origin": "WEBSITE", "biddable": True}],
        )

        assert result["changes"]["level"] == "campaign"
        assert result["changes"]["campaign_id"] == "16454984318"
        assert result["changes"]["goals"][0]["before"] is False

    def test_campaign_level_requires_a_numeric_campaign(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            level="campaign",
            goals=[{"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}],
        )

        assert "campaign_id is required" in " ".join(result["details"])

    def test_unknown_campaign_is_refused(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            level="campaign",
            campaign_id="999",
            goals=[{"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}],
        )

        assert "No conversion goals found for campaign 999" in result["error"]

    def test_invalid_level_is_refused(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            level="account",
            goals=[{"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}],
        )

        assert "level must be 'customer' or 'campaign'" in " ".join(result["details"])

    def test_goal_without_biddable_is_refused(self, config, monkeypatch):
        result = _draft(
            config, monkeypatch,
            goals=[{"category": "PURCHASE", "origin": "WEBSITE"}],
        )

        assert "needs biddable" in " ".join(result["details"])

    def test_empty_goal_list_is_refused(self, config, monkeypatch):
        result = _draft(config, monkeypatch, goals=[])

        assert "At least one goal is required" in " ".join(result["details"])

    def test_blocked_operation_is_refused(self, monkeypatch):
        blocked = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(blocked_operations=["update_conversion_goals"]),
        )
        result = write.draft_conversion_goal_settings(
            blocked,
            goals=[{"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}],
        )

        assert "blocked by configuration" in result["error"]

    def test_draft_does_not_mutate(self, config, monkeypatch):
        client = _ReadClient()
        _draft(
            config, monkeypatch, client=client,
            goals=[{"category": "PURCHASE", "origin": "WEBSITE", "biddable": False}],
        )

        assert not hasattr(client, "customer_request")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class _MutateClient:
    def __init__(self, *, fail_index=None):
        base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.customer_request = None
        self.campaign_request = None
        self._fail_index = fail_index
        self._services = {
            "CustomerConversionGoalService": SimpleNamespace(
                mutate_customer_conversion_goals=self._mutate_customer
            ),
            "CampaignConversionGoalService": SimpleNamespace(
                mutate_campaign_conversion_goals=self._mutate_campaign
            ),
            "GoogleAdsService": SimpleNamespace(
                search=lambda customer_id, query: []
            ),
        }

    def get_service(self, name):
        return self._services[name]

    def _results(self, operations):
        results = []
        for index, operation in enumerate(operations):
            resource = "" if index == self._fail_index else operation.update.resource_name
            results.append(SimpleNamespace(resource_name=resource))
        return results

    def _mutate_customer(self, request=None, **kwargs):
        self.customer_request = request
        return SimpleNamespace(results=self._results(request.operations), partial_failure_error=None)

    def _mutate_campaign(self, request=None, **kwargs):
        self.campaign_request = request
        return SimpleNamespace(results=self._results(request.operations), partial_failure_error=None)


class TestApplyConversionGoals:
    def test_customer_update_builds_the_documented_resource(self):
        client = _MutateClient()

        result = write._apply_conversion_goals(
            client,
            "1586230693",
            {
                "level": "customer",
                "goals": [
                    {"category": "SIGNUP", "origin": "WEBSITE", "biddable": False}
                ],
            },
        )

        operation = client.customer_request.operations[0]
        assert operation.update.resource_name == (
            "customers/1586230693/customerConversionGoals/SIGNUP~WEBSITE"
        )
        assert operation.update.biddable is False
        assert list(operation.update_mask.paths) == ["biddable"]
        assert result["updated_count"] == 1
        assert "readback" in result

    def test_campaign_update_builds_the_composite_resource(self):
        client = _MutateClient()

        write._apply_conversion_goals(
            client,
            "1586230693",
            {
                "level": "campaign",
                "campaign_id": "16454984318",
                "goals": [
                    {"category": "PURCHASE", "origin": "WEBSITE", "biddable": True}
                ],
            },
        )

        assert client.campaign_request.operations[0].update.resource_name == (
            "customers/1586230693/campaignConversionGoals/16454984318~PURCHASE~WEBSITE"
        )

    def test_a_rejected_batch_surfaces_as_an_error(self):
        """These mutate requests have no partial_failure: one bad goal rejects
        the whole batch, so the applier must not pretend otherwise."""
        client = _MutateClient()
        client.get_service("CustomerConversionGoalService").mutate_customer_conversion_goals = (
            lambda request=None, **kwargs: (_ for _ in ()).throw(
                RuntimeError("goal rejected by the API")
            )
        )

        with pytest.raises(RuntimeError):
            write._apply_conversion_goals(
                client,
                "1586230693",
                {
                    "level": "customer",
                    "goals": [
                        {"category": "SIGNUP", "origin": "WEBSITE", "biddable": False}
                    ],
                },
            )


class TestConversionGoalToolRegistration:
    @pytest.mark.asyncio
    async def test_annotations_and_tags(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        assert tools["get_conversion_goals"].annotations.read_only_hint is True
        assert tools["draft_conversion_goal_settings"].annotations.read_only_hint is False
        assert tools["draft_conversion_goal_settings"].annotations.destructive_hint is False
        for name in ("get_conversion_goals", "draft_conversion_goal_settings"):
            assert set(tools[name].tags) == {"ads"}, name

    @pytest.mark.asyncio
    async def test_schema_documents_the_parameters(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        properties = tools["draft_conversion_goal_settings"].parameters["properties"]
        for param in ("goals", "level", "campaign_id"):
            assert properties[param].get("description"), param
