"""Text assets: explicit link scope, in-place edits, the atomic snippet swap,
account-level links, and id validation before anything reaches GAQL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import assets, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads.validate_only import ValidateOnlyClient
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import ChangePlan, get_plan, store_plan

CID = "1234567890"
OLD_LINK = f"customers/{CID}/campaignAssets/42~555~STRUCTURED_SNIPPET"


# ---------------------------------------------------------------------------
# Fakes: real google-ads message types, recorded service calls
# ---------------------------------------------------------------------------

_COLLECTIONS = {"asset_path": "assets", "campaign_path": "campaigns", "ad_group_path": "adGroups"}


class _Response:
    """A MutateOperationResponse: only the result matching the operation is set."""

    def __init__(self, result_field: str, name: str):
        self._field = result_field
        self._name = name

    def __getattr__(self, attr):
        if attr.endswith("_result"):
            return SimpleNamespace(resource_name=self._name if attr == self._field else "")
        raise AttributeError(attr)


def _respond(op, index: int) -> _Response:
    kind = type(op).pb(op).WhichOneof("operation")
    field = kind.removesuffix("_operation") + "_result"
    return _Response(field, f"customers/{CID}/{field}/{index}")


class _Recorder:
    def __init__(self, search_rows=None):
        self.mutates: list[tuple[str, str, list]] = []
        self.queries: list[str] = []
        self.search_rows = search_rows or []
        self.validate_only: list[bool] = []


class _FakeService:
    def __init__(self, rec: _Recorder, name: str):
        self._rec = rec
        self._name = name

    def __getattr__(self, attr):
        rec = self._rec
        if attr in _COLLECTIONS:
            return lambda cid, entity_id: f"customers/{cid}/{_COLLECTIONS[attr]}/{entity_id}"
        if attr == "search":
            def search(customer_id, query):
                rec.queries.append(query)
                return list(rec.search_rows)
            return search
        if attr.startswith("mutate"):
            def mutate(request=None, **kwargs):
                if request is not None:
                    ops = list(getattr(request, "mutate_operations", None) or request.operations)
                    rec.validate_only.append(bool(request.validate_only))
                else:
                    ops = list(kwargs.get("mutate_operations") or kwargs.get("operations"))
                    rec.validate_only.append(False)
                rec.mutates.append((self._name, attr, ops))
                if attr == "mutate":
                    return SimpleNamespace(
                        mutate_operation_responses=[_respond(op, i) for i, op in enumerate(ops)],
                        partial_failure_error=None,
                    )
                return SimpleNamespace(
                    results=[SimpleNamespace(resource_name=f"customers/{CID}/assets/{i}") for i, _ in enumerate(ops)],
                    partial_failure_error=None,
                )
            return mutate
        raise AttributeError(attr)


class _FakeClient:
    def __init__(self, rec: _Recorder):
        base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.rec = rec

    def get_service(self, name, *args, **kwargs):
        return _FakeService(self.rec, name)


def _link_row(resource=OLD_LINK, link="campaign_asset"):
    return SimpleNamespace(**{link: SimpleNamespace(resource_name=resource)})


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")),
    )


@pytest.fixture
def gaql(monkeypatch):
    """Patch execute_query; set ``.rows`` and read ``.queries``."""
    state = SimpleNamespace(rows=[], queries=[])

    def fake(_config, _customer_id, query):
        state.queries.append(query)
        return state.rows

    monkeypatch.setattr("adloop.ads.gaql.execute_query", fake)
    return state


# ---------------------------------------------------------------------------
# Scope: campaign by default, ad group by name, account only on request
# ---------------------------------------------------------------------------


class TestScope:
    def test_missing_campaign_id_is_an_error_not_an_account_link(self, config):
        result = assets.draft_callouts(config, customer_id=CID, callouts=["Free Quotes"])

        assert result["error"] == "Validation failed"
        assert any("scope='account'" in d for d in result["details"])

    def test_account_scope_must_be_explicit(self, config):
        result = assets.draft_callouts(
            config, customer_id=CID, scope="account", callouts=["Free Quotes"]
        )

        assert result["entity_type"] == "customer_asset"
        assert result["changes"]["scope"] == "account"
        assert "every eligible campaign" in result["warnings"][0]

    def test_account_scope_refuses_campaign_or_ad_group_ids(self, config):
        result = assets.draft_callouts(
            config, customer_id=CID, scope="account", campaign_id="42", callouts=["Free Quotes"]
        )

        assert result["error"] == "Validation failed"
        assert any("leave campaign_id and ad_group_id empty" in d for d in result["details"])

    def test_ad_group_scope_links_to_the_ad_group(self, config):
        result = assets.draft_structured_snippets(
            config,
            customer_id=CID,
            scope="ad_group",
            ad_group_id="777",
            snippets=[{"header": "Services", "values": ["A", "B", "C"]}],
        )

        assert result["entity_type"] == "ad_group_asset"
        assert result["entity_id"] == "777"
        assert result["changes"]["scope"] == "ad_group"
        assert "warnings" not in result

    def test_ad_group_id_without_ad_group_scope_points_at_the_scope(self, config):
        result = assets.draft_callouts(
            config, customer_id=CID, ad_group_id="777", callouts=["Free Quotes"]
        )

        assert any("scope='ad_group'" in d for d in result["details"])

    @pytest.mark.parametrize("bad", ["42 OR 1=1", "abc", "-1", "4 2"])
    def test_ids_must_be_numeric(self, config, bad):
        result = assets.draft_callouts(
            config, customer_id=CID, campaign_id=bad, callouts=["Free Quotes"]
        )

        assert any("must be numeric" in d for d in result["details"])

    def test_unknown_scope_is_refused(self, config):
        result = assets.draft_callouts(
            config, customer_id=CID, scope="customer", callouts=["Free Quotes"]
        )

        assert "scope must be one of" in result["details"][0]

    def test_business_name_cannot_link_to_an_ad_group(self, config):
        result = assets.draft_business_name_asset(
            config, customer_id=CID, scope="ad_group", business_name="Acme Plumbing"
        )

        assert "scope must be one of ['campaign', 'account']" in result["details"][0]

    def test_business_name_length_and_account_opt_in(self, config):
        too_long = assets.draft_business_name_asset(
            config, customer_id=CID, campaign_id="42", business_name="A" * 26
        )
        account = assets.draft_business_name_asset(
            config, customer_id=CID, scope="account", business_name="Acme Plumbing"
        )

        assert "max 25" in too_long["details"][0]
        assert account["changes"] == {
            "scope": "account",
            "campaign_id": "",
            "business_name": "Acme Plumbing",
        }


class TestScopedApply:
    def test_ad_group_callouts_create_and_link_in_one_request(self):
        rec = _Recorder()
        client = _FakeClient(rec)

        result = assets._apply_create_callouts(
            client, CID, {"scope": "ad_group", "ad_group_id": "777", "callouts": ["A", "B"]}
        )

        [(service, method, ops)] = rec.mutates
        assert (service, method) == ("GoogleAdsService", "mutate")
        links = [op.ad_group_asset_operation.create for op in ops[2:]]
        assert [link.asset for link in links] == [
            f"customers/{CID}/assets/-1",
            f"customers/{CID}/assets/-2",
        ]
        assert all(link.ad_group == f"customers/{CID}/adGroups/777" for link in links)
        assert all(link.field_type == client.enums.AssetFieldTypeEnum.CALLOUT for link in links)
        assert len(result["assets"]) == 2 and len(result["ad_group_assets"]) == 2

    def test_account_snippets_link_through_customer_asset(self):
        rec = _Recorder()
        client = _FakeClient(rec)

        result = assets._apply_create_structured_snippets(
            client,
            CID,
            {"scope": "account", "snippets": [{"header": "Brands", "values": ["A", "B", "C"]}]},
        )

        ops = rec.mutates[0][2]
        link = ops[1].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET
        assert result["customer_assets"]

    def test_plans_without_a_scope_still_link_to_the_campaign(self):
        rec = _Recorder()
        client = _FakeClient(rec)

        assets._apply_create_callouts(client, CID, {"campaign_id": "42", "callouts": ["A"]})

        link = rec.mutates[0][2][1].campaign_asset_operation.create
        assert link.campaign == f"customers/{CID}/campaigns/42"

    def test_business_name_is_a_text_asset_linked_as_business_name(self):
        rec = _Recorder()
        client = _FakeClient(rec)

        assets._apply_create_business_name_asset(
            client, CID, {"scope": "campaign", "campaign_id": "42", "business_name": "Acme"}
        )

        create, link = rec.mutates[0][2]
        assert create.asset_operation.create.text_asset.text == "Acme"
        assert create.asset_operation.create.type_ == client.enums.AssetTypeEnum.TEXT
        assert (
            link.campaign_asset_operation.create.field_type
            == client.enums.AssetFieldTypeEnum.BUSINESS_NAME
        )


# ---------------------------------------------------------------------------
# In-place edits: callout and sitelink
# ---------------------------------------------------------------------------


class TestInPlaceEdits:
    def test_callout_preview_shows_old_and_new_text(self, config, gaql):
        gaql.rows = [{"asset.type": "CALLOUT", "asset.callout_asset.callout_text": "Free Quote"}]

        result = assets.update_callout(
            config, customer_id=CID, asset_id="555", callout_text="Free Estimates"
        )

        assert result["operation"] == "update_callout"
        assert result["changes"]["callout_text"] == "Free Estimates"
        assert result["changes"]["previous"] == {"callout_text": "Free Quote"}
        assert result["requires_double_confirm"] is False
        assert "WHERE asset.id = 555" in gaql.queries[0]

    def test_callout_wrong_type_or_missing_asset_is_refused(self, config, gaql):
        gaql.rows = [{"asset.type": "SITELINK"}]
        wrong = assets.update_callout(config, customer_id=CID, asset_id="555", callout_text="X")
        gaql.rows = []
        missing = assets.update_callout(config, customer_id=CID, asset_id="555", callout_text="X")

        assert "not a CALLOUT" in wrong["error"]
        assert "not found" in missing["error"]

    def test_non_numeric_asset_id_never_reaches_gaql(self, config, gaql):
        result = assets.update_callout(
            config, customer_id=CID, asset_id="555 OR asset.id > 0", callout_text="X"
        )

        assert any("must be numeric" in d for d in result["details"])
        assert gaql.queries == []

    def test_callout_apply_updates_the_asset_with_a_field_mask(self):
        rec = _Recorder()
        client = _FakeClient(rec)

        assets._apply_update_callout(client, CID, {"asset_id": "555", "callout_text": "Free Estimates"})

        [(service, method, [op])] = rec.mutates
        assert (service, method) == ("AssetService", "mutate_assets")
        assert op.update.resource_name == f"customers/{CID}/assets/555"
        assert op.update.callout_asset.callout_text == "Free Estimates"
        assert list(op.update_mask.paths) == ["callout_asset.callout_text"]

    def _sitelink_row(self, **overrides):
        row = {
            "asset.type": "SITELINK",
            "asset.sitelink_asset.link_text": "Pricing",
            "asset.sitelink_asset.description1": "",
            "asset.sitelink_asset.description2": "",
            "asset.final_urls": ["https://example.com/pricing"],
        }
        row.update(overrides)
        return row

    def test_sitelink_changes_only_what_differs(self, config, gaql, monkeypatch):
        gaql.rows = [self._sitelink_row()]
        checked = []
        monkeypatch.setattr(
            write, "_validate_urls", lambda urls: (checked.extend(urls) or {u: None for u in urls}, {})
        )

        result = assets.update_sitelink(
            config,
            customer_id=CID,
            asset_id="556",
            link_text="Pricing",
            final_url="https://example.com/plans",
        )

        assert result["changes"] == {
            "asset_id": "556",
            "final_url": "https://example.com/plans",
            "previous": {"final_url": "https://example.com/pricing"},
        }
        assert checked == ["https://example.com/plans"]

    def test_sitelink_needs_both_description_lines(self, config, gaql):
        gaql.rows = [self._sitelink_row()]

        result = assets.update_sitelink(
            config, customer_id=CID, asset_id="556", description1="Plans from $9"
        )

        assert "must both be set" in result["details"][0]

    def test_sitelink_apply_masks_only_changed_fields(self):
        rec = _Recorder()
        client = _FakeClient(rec)

        assets._apply_update_sitelink(
            client,
            CID,
            {"asset_id": "556", "link_text": "Plans", "final_url": "https://example.com/plans"},
        )

        op = rec.mutates[0][2][0]
        assert set(op.update_mask.paths) == {"sitelink_asset.link_text", "final_urls"}
        assert list(op.update.final_urls) == ["https://example.com/plans"]


# ---------------------------------------------------------------------------
# Structured snippet swap: double confirm, one atomic request
# ---------------------------------------------------------------------------

_NEW_SNIPPET = {"header": "Services", "values": ["Drains", "Water Heaters", "Repiping"]}


def _swap_changes(**overrides):
    changes = {
        "scope": "campaign",
        "campaign_id": "42",
        "ad_group_id": "",
        "old_asset_id": "555",
        "old_link": OLD_LINK,
        "snippet": _NEW_SNIPPET,
    }
    changes.update(overrides)
    return changes


class TestSnippetSwap:
    def test_draft_requires_double_confirm_and_shows_the_old_snippet(self, config, gaql):
        gaql.rows = [{
            "campaign_asset.resource_name": OLD_LINK,
            "asset.structured_snippet_asset.header": "Services",
            "asset.structured_snippet_asset.values": ["Drains", "Leaks", "Sewer"],
        }]

        result = assets.update_structured_snippet(
            config, customer_id=CID, asset_id="555", campaign_id="42", **_NEW_SNIPPET
        )

        assert result["requires_double_confirm"] is True
        assert result["changes"]["old_link"] == OLD_LINK
        assert result["changes"]["previous"]["values"] == ["Drains", "Leaks", "Sewer"]
        assert "swap" in result["warnings"][0]
        query = gaql.queries[0]
        assert "FROM campaign_asset" in query
        assert "asset.id = 555" in query and "campaign.id = 42" in query
        assert "campaign_asset.status != 'REMOVED'" in query

    def test_draft_refuses_when_the_old_link_does_not_exist(self, config, gaql):
        gaql.rows = []

        result = assets.update_structured_snippet(
            config, customer_id=CID, asset_id="555", scope="ad_group", ad_group_id="777", **_NEW_SNIPPET
        )

        assert "no link to replace" in result["error"]
        assert "ad group 777" in result["error"]
        assert "plan_id" not in result

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"asset_id": "555'", "campaign_id": "42"},
            {"asset_id": "555", "campaign_id": "42) OR (1=1"},
        ],
    )
    def test_draft_validates_ids_before_querying(self, config, gaql, kwargs):
        result = assets.update_structured_snippet(config, customer_id=CID, **kwargs, **_NEW_SNIPPET)

        assert any("must be numeric" in d for d in result["details"])
        assert gaql.queries == []

    def test_apply_sends_create_link_and_unlink_in_one_mutate(self):
        rec = _Recorder(search_rows=[_link_row()])
        client = _FakeClient(rec)

        result = assets._apply_update_structured_snippet(client, CID, _swap_changes())

        [(service, method, ops)] = rec.mutates
        assert (service, method) == ("GoogleAdsService", "mutate")
        create, link, unlink = ops
        assert create.asset_operation.create.structured_snippet_asset.header == "Services"
        assert link.campaign_asset_operation.create.asset == f"customers/{CID}/assets/-1"
        assert link.campaign_asset_operation.create.campaign == f"customers/{CID}/campaigns/42"
        assert unlink.campaign_asset_operation.remove == OLD_LINK
        assert result["old_link_removed"]
        assert result["new_asset"] and result["new_link"]

    def test_ad_group_swap_unlinks_the_ad_group_link(self):
        old = f"customers/{CID}/adGroupAssets/777~555~STRUCTURED_SNIPPET"
        rec = _Recorder(search_rows=[_link_row(old, "ad_group_asset")])
        client = _FakeClient(rec)

        assets._apply_update_structured_snippet(
            client, CID, _swap_changes(scope="ad_group", campaign_id="", ad_group_id="777")
        )

        ops = rec.mutates[0][2]
        assert ops[1].ad_group_asset_operation.create.ad_group == f"customers/{CID}/adGroups/777"
        assert ops[2].ad_group_asset_operation.remove == old
        assert "ad_group.id = 777" in rec.queries[0]

    def test_missing_old_link_at_apply_sends_nothing(self):
        rec = _Recorder(search_rows=[])
        client = _FakeClient(rec)

        with pytest.raises(ValueError, match="no longer linked"):
            assets._apply_update_structured_snippet(client, CID, _swap_changes())

        assert rec.mutates == []

    def test_dry_run_validates_the_whole_swap_in_one_call(self, config, monkeypatch):
        rec = _Recorder(search_rows=[_link_row()])
        monkeypatch.setattr(
            write, "_validate_with_google", lambda c, p: write._execute_plan(c, p, validate_only=True)
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: _FakeClient(rec))
        plan = ChangePlan(
            operation="update_structured_snippet",
            entity_type="campaign_asset",
            entity_id="42",
            customer_id=CID,
            changes=_swap_changes(),
            requires_double_confirm=True,
        )
        store_plan(plan)

        result = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS", result
        assert result["checks"] == {"validated_calls": 1, "skipped_calls": 0}
        assert rec.validate_only == [True]
        assert len(rec.mutates[0][2]) == 3

    def test_dry_run_fails_when_the_old_link_is_gone(self, config, monkeypatch):
        rec = _Recorder(search_rows=[])
        monkeypatch.setattr(
            write, "_validate_with_google", lambda c, p: write._execute_plan(c, p, validate_only=True)
        )
        monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: _FakeClient(rec))
        plan = ChangePlan(
            operation="update_structured_snippet",
            entity_type="campaign_asset",
            entity_id="42",
            customer_id=CID,
            changes=_swap_changes(),
        )
        store_plan(plan)

        result = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=True)

        assert result["status"] == "DRY_RUN_FAILED"
        assert "no longer linked" in result["error"]
        assert rec.mutates == []
        assert get_plan(plan.plan_id).dry_run_result is None


class TestLinkQuery:
    @pytest.mark.parametrize(
        "args",
        [
            ("555 OR 1=1", "STRUCTURED_SNIPPET", "campaign", "42", ""),
            ("555", "STRUCTURED_SNIPPET", "campaign", "42'", ""),
            ("555", "STRUCTURED_SNIPPET", "ad_group", "", "7 7"),
            ("555", "SNIPPET' OR '1'='1", "account", "", ""),
            ("555", "STRUCTURED_SNIPPET", "customer", "", ""),
        ],
    )
    def test_bad_input_raises_before_any_search(self, args):
        rec = _Recorder(search_rows=[_link_row()])

        with pytest.raises(ValueError):
            assets._find_asset_link(_FakeClient(rec), CID, *args)

        assert rec.queries == []

    def test_account_link_query_filters_out_removed_links(self):
        query = assets._asset_link_query("555", "CALLOUT", "account")

        assert query == (
            "SELECT customer_asset.resource_name FROM customer_asset WHERE "
            "asset.id = 555 AND customer_asset.field_type = 'CALLOUT' "
            "AND customer_asset.status != 'REMOVED'"
        )


# ---------------------------------------------------------------------------
# link_asset_to_customer: explicit allowlist, asset type checked
# ---------------------------------------------------------------------------


class TestLinkAssetToCustomer:
    @pytest.mark.parametrize("field_type", ["HEADLINE", "MARKETING_IMAGE", "AD_IMAGE", "LEAD_FORM", "LOGO"])
    def test_field_types_customer_asset_rejects_are_refused(self, config, gaql, field_type):
        result = assets.link_asset_to_customer(
            config, customer_id=CID, links=[{"asset_id": "555", "field_type": field_type}]
        )

        assert "cannot be linked at account level" in result["details"][0]
        assert gaql.queries == []

    def test_allowlist_is_the_documented_customer_asset_set(self):
        assert set(assets._CUSTOMER_ASSET_FIELD_TYPES) == {
            "BUSINESS_NAME", "BUSINESS_LOGO", "CALL", "CALLOUT", "HOTEL_CALLOUT",
            "MOBILE_APP", "PRICE", "PROMOTION", "SITELINK", "STRUCTURED_SNIPPET",
        }

    def test_non_numeric_asset_id_is_refused_before_gaql(self, config, gaql):
        result = assets.link_asset_to_customer(
            config, customer_id=CID, links=[{"asset_id": "1) OR (1=1", "field_type": "CALLOUT"}]
        )

        assert "must be numeric" in result["details"][0]
        assert gaql.queries == []

    def test_asset_type_must_fit_the_field_type(self, config, gaql):
        gaql.rows = [{"asset.id": 555, "asset.type": "IMAGE", "asset.name": "logo.png"}]

        result = assets.link_asset_to_customer(
            config, customer_id=CID, links=[{"asset_id": "555", "field_type": "CALLOUT"}]
        )

        assert "needs a CALLOUT asset" in result["details"][0]

    def test_valid_links_preview_and_apply(self, config, gaql):
        gaql.rows = [
            {"asset.id": 555, "asset.type": "IMAGE", "asset.name": "logo.png"},
            {"asset.id": 556, "asset.type": "TEXT", "asset.name": ""},
        ]

        result = assets.link_asset_to_customer(
            config,
            customer_id=CID,
            links=[
                {"asset_id": "555", "field_type": "business_logo"},
                {"asset_id": "556", "field_type": "BUSINESS_NAME"},
                {"asset_id": "556", "field_type": "BUSINESS_NAME"},
            ],
        )

        assert result["operation"] == "link_asset_to_customer"
        assert [link["field_type"] for link in result["changes"]["links"]] == [
            "BUSINESS_LOGO",
            "BUSINESS_NAME",
        ]
        assert "asset.id IN (555, 556)" in gaql.queries[0]

        rec = _Recorder()
        client = _FakeClient(rec)
        applied = assets._apply_link_asset_to_customer(client, CID, result["changes"])
        ops = rec.mutates[0][2]
        assert ops[0].customer_asset_operation.create.asset == f"customers/{CID}/assets/555"
        assert (
            ops[0].customer_asset_operation.create.field_type
            == client.enums.AssetFieldTypeEnum.BUSINESS_LOGO
        )
        assert len(applied["customer_assets"]) == 2


# ---------------------------------------------------------------------------
# Wiring: dispatch, validate-only, remove_entity, server registration
# ---------------------------------------------------------------------------

_NEW_OPERATIONS = {
    "create_callouts": "_apply_create_callouts",
    "create_structured_snippets": "_apply_create_structured_snippets",
    "create_business_name_asset": "_apply_create_business_name_asset",
    "link_asset_to_customer": "_apply_link_asset_to_customer",
    "update_callout": "_apply_update_callout",
    "update_sitelink": "_apply_update_sitelink",
    "update_structured_snippet": "_apply_update_structured_snippet",
}


@pytest.mark.parametrize(("operation", "handler"), sorted(_NEW_OPERATIONS.items()))
def test_operations_are_dispatched_to_the_assets_module(monkeypatch, operation, handler):
    monkeypatch.setattr(assets, handler, lambda _c, _cid, changes: {"ran": operation, **changes})

    result = write._dispatch_ads_plan(
        object(), CID, SimpleNamespace(operation=operation, changes={"x": 1})
    )

    assert result == {"ran": operation, "x": 1}


def test_in_place_edits_are_sent_validate_only():
    rec = _Recorder()
    client = ValidateOnlyClient(_FakeClient(rec))

    assets._apply_update_callout(client, CID, {"asset_id": "555", "callout_text": "Free"})

    assert rec.validate_only == [True]
    assert client.validated_calls == 1


def test_remove_entity_accepts_ad_group_asset_links(config):
    preview = write.remove_entity(
        config, customer_id=CID, entity_type="ad_group_asset", entity_id="777,555,CALLOUT"
    )
    rec = _Recorder()

    write._apply_remove(_FakeClient(rec), CID, "ad_group_asset", preview["entity_id"])

    assert preview["requires_double_confirm"] is True
    op = rec.mutates[0][2][0]
    assert op.ad_group_asset_operation.remove == f"customers/{CID}/adGroupAssets/777~555~CALLOUT"


@pytest.mark.asyncio
async def test_server_registers_the_asset_tools():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    names = (
        "draft_callouts",
        "draft_structured_snippets",
        "draft_business_name_asset",
        "link_asset_to_customer",
        "update_callout",
        "update_sitelink",
        "update_structured_snippet",
    )
    for name in names:
        assert "ads" in tools[name].tags, name
    destructive = {n for n in names if tools[n].annotations.destructive_hint}
    assert destructive == {"update_structured_snippet"}
    for name in ("draft_callouts", "draft_structured_snippets"):
        params = tools[name].parameters
        assert "campaign_id" not in params.get("required", []), name
        assert params["properties"]["scope"]["default"] == "campaign", name
        assert "description" in params["properties"]["scope"], name
