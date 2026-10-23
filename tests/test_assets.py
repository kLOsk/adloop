"""Asset extensions: one scope rule, ad-group and account links, in-place edits.

Based on the design and tests of PR #95 (Illia Sapryga).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import assets, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="1234567890"),
                        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")))


def _gaql(rows):
    return patch("adloop.ads.gaql.execute_query", return_value=rows)


# ---------------------------------------------------------------------------
# Fakes with real Google Ads types
# ---------------------------------------------------------------------------


class _Paths:
    def __getattr__(self, name):
        if name.endswith("_path"):
            kind = name[: -len("_path")]
            return lambda cid, eid: f"customers/{cid}/{kind}s/{eid}"
        raise AttributeError(name)


class _GoogleAds(_Paths):
    def __init__(self, rows=None, n=0):
        self.rows, self.operations, self.n, self.requests = rows or [], None, n, []

    def search(self, customer_id, query):
        self.query = query
        return iter(self.rows)

    def mutate(self, customer_id=None, mutate_operations=None, request=None):
        if request is not None:  # sent through ValidateOnlyClient
            self.requests.append(request)
            mutate_operations = request.mutate_operations
        self.operations = list(mutate_operations)
        responses = []
        for op in self.operations:
            which = op._pb.WhichOneof("operation")
            result = which.replace("_operation", "_result")
            responses.append(SimpleNamespace(**{
                f: SimpleNamespace(resource_name=f"res/{f}" if f == result else "")
                for f in ("asset_result", "campaign_asset_result", "ad_group_asset_result",
                          "customer_asset_result")
            }))
        return SimpleNamespace(mutate_operation_responses=responses)


class _AssetService(_Paths):
    def __init__(self):
        self.operations, self.requests = None, []

    def mutate_assets(self, customer_id=None, operations=None, request=None):
        if request is not None:
            self.requests.append(request)
            operations = request.operations
        self.operations = list(operations)
        return SimpleNamespace(results=[SimpleNamespace(resource_name="customers/1/assets/7")])


class _CriterionService(_Paths):
    def __init__(self):
        self.operations, self.requests = None, []

    def mutate_campaign_criteria(self, customer_id=None, operations=None, request=None):
        if request is not None:
            self.requests.append(request)
            operations = request.operations
        self.operations = list(operations)
        return SimpleNamespace(results=[SimpleNamespace(resource_name=f"res/{i}") for i in range(len(operations))])


class _Client:
    def __init__(self, rows=None):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True,
                               version=GOOGLE_ADS_API_VERSION)
        self.enums, self.get_type = base.enums, base.get_type
        self.ads, self.assets, self.criteria = _GoogleAds(rows), _AssetService(), _CriterionService()

    def get_service(self, name):
        return {"GoogleAdsService": self.ads, "AssetService": self.assets,
                "CampaignCriterionService": self.criteria}.get(name) or _Paths()

    def sent(self):
        """Every request the dry-run wrapper sent."""
        return self.ads.requests + self.assets.requests + self.criteria.requests


def _row(**fields):
    """A search row: asset.<path> attributes, e.g. callout_text=... ."""
    callout = SimpleNamespace(callout_text=fields.get("callout_text", ""))
    sitelink = SimpleNamespace(link_text=fields.get("link_text", ""),
                               description1=fields.get("description1", ""),
                               description2=fields.get("description2", ""))
    snippet = SimpleNamespace(header=fields.get("header", ""), values=fields.get("values", []))
    return SimpleNamespace(asset=SimpleNamespace(
        id=7, callout_asset=callout, sitelink_asset=sitelink, structured_snippet_asset=snippet,
        final_urls=fields.get("final_urls", []),
    ))


# ---------------------------------------------------------------------------
# One scope rule (review point on #95: the same for every asset tool)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope,campaign,ad_group,expected", [
    ("", "1001", "", "campaign"),
    ("", "", "2002", "ad_group"),
    ("account", "", "", "account"),
    ("campaign", " 1001 ", "", "campaign"),
])
def test_scope_follows_the_id_and_account_is_explicit(scope, campaign, ad_group, expected):
    resolved, campaign_id, _, errors = assets.resolve_scope(scope, campaign, ad_group)
    assert (resolved, errors) == (expected, [])
    assert campaign_id == campaign.strip()  # stored stripped


@pytest.mark.parametrize("scope,campaign,ad_group,needle", [
    ("", "", "", "scope='account'"),
    ("", "1001", "2002", "not both"),
    ("account", "1001", "", "leave campaign_id"),
    ("campaign", "abc", "", "numeric"),
    ("ad_group", "", "", "ad_group_id is required"),
    ("everywhere", "1001", "", "scope must be one of"),
])
def test_scope_refusals(scope, campaign, ad_group, needle):
    assert needle in " ".join(assets.resolve_scope(scope, campaign, ad_group)[3])


def test_business_names_cannot_go_on_an_ad_group(config):
    result = assets.draft_business_name_asset(config, customer_id="1", business_name="Bohne", scope="ad_group")
    assert "scope must be one of ['campaign', 'account']" in result["details"][0]


def test_drafts_carry_the_scope_and_warn_for_the_account(config):
    by_ad_group = assets.draft_callouts(config, customer_id="1", ad_group_id="2002", callouts=["Free Shipping"])
    assert (by_ad_group["entity_type"], by_ad_group["entity_id"]) == ("ad_group_asset", "2002")
    account = assets.draft_structured_snippets(
        config, customer_id="1", scope="account", snippets=[{"header": "Brands", "values": ["A", "B", "C"]}])
    assert account["entity_type"] == "customer_asset" and "Account-level" in account["warnings"][0]
    # write.draft_callouts still works as before for campaign callouts.
    legacy = write.draft_callouts(config, customer_id="1", campaign_id="1001", callouts=["24/7 Support"])
    assert legacy["changes"]["scope"] == "campaign"


def test_create_links_at_the_plans_scope_in_one_request():
    client = _Client()
    assets._apply_create_callouts(client, "1", {"scope": "ad_group", "ad_group_id": "2002",
                                                "callouts": ["Free Shipping", "Same Day"]})
    ops = client.ads.operations
    assert [op._pb.WhichOneof("operation") for op in ops] == [
        "asset_operation", "asset_operation", "ad_group_asset_operation", "ad_group_asset_operation"]
    assert ops[2].ad_group_asset_operation.create.ad_group == "customers/1/ad_groups/2002"

    client = _Client()
    assets._apply_create_business_name_asset(client, "1", {"scope": "account", "business_name": "Bohne"})
    link = client.ads.operations[1].customer_asset_operation.create
    assert link.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_NAME

    # A plan from before scopes existed links to its campaign, as it always did.
    client = _Client()
    assets._apply_create_callouts(client, "1", {"campaign_id": "1001", "callouts": ["X"]})
    assert client.ads.operations[1]._pb.WhichOneof("operation") == "campaign_asset_operation"


def test_account_links_check_the_asset_type(config):
    rows = [{"asset.id": 5, "asset.type": "CALLOUT", "asset.name": ""},
            {"asset.id": 6, "asset.type": "IMAGE", "asset.name": "logo"}]
    with _gaql(rows):
        wrong = assets.link_asset_to_customer(config, customer_id="1", links=[
            {"asset_id": "5", "field_type": "CALLOUT"}, {"asset_id": "6", "field_type": "SITELINK"}])
        ok = assets.link_asset_to_customer(config, customer_id="1", links=[
            {"asset_id": "5", "field_type": "callout"}, {"asset_id": "5", "field_type": "CALLOUT"},
            {"asset_id": "6", "field_type": "BUSINESS_LOGO"}])
    assert "SITELINK needs a SITELINK asset" in wrong["details"][0]
    assert [link["asset_id"] for link in ok["changes"]["links"]] == ["5", "6"]
    assert "BUSINESS_MESSAGE" not in assets.CUSTOMER_ASSET_FIELD_TYPES


# ---------------------------------------------------------------------------
# In-place edits, pinned at preview and re-checked at apply
# ---------------------------------------------------------------------------


def test_callout_edit_previews_old_and_new_and_refuses_the_wrong_type(config):
    with _gaql([{"asset.id": 7, "asset.type": "CALLOUT", "asset.callout_asset.callout_text": "Free Shipping"}]):
        preview = assets.update_callout(config, customer_id="1", asset_id=" 7 ", callout_text="Free Returns")
        same = assets.update_callout(config, customer_id="1", asset_id="7", callout_text="Free Shipping")
    assert preview["changes"] == {"asset_id": "7", "callout_text": "Free Returns",
                                  "previous": {"callout_text": "Free Shipping"}}
    assert "nothing to change" in same["error"]
    with _gaql([{"asset.id": 7, "asset.type": "SITELINK"}]):
        assert "not a CALLOUT" in assets.update_callout(config, customer_id="1", asset_id="7", callout_text="X")["error"]
    with _gaql([]) as q:
        assert "not found" in assets.update_callout(config, customer_id="1", asset_id="7", callout_text="X")["error"]
    q.reset_mock()
    with _gaql([]) as q:
        assets.update_callout(config, customer_id="1", asset_id="7 OR 1=1", callout_text="X")
    q.assert_not_called()


def test_callout_apply_checks_the_asset_is_unchanged_first():
    changes = {"asset_id": "7", "callout_text": "Free Returns", "previous": {"callout_text": "Free Shipping"}}
    client = _Client(rows=[_row(callout_text="Free Shipping")])
    assets._apply_update_callout(client, "1", changes)
    op = client.assets.operations[0]
    assert op.update.callout_asset.callout_text == "Free Returns"
    assert list(op.update_mask.paths) == ["callout_asset.callout_text"]

    edited_meanwhile = _Client(rows=[_row(callout_text="Someone else's text")])
    with pytest.raises(RuntimeError, match="changed in Google Ads after this preview"):
        assets._apply_update_callout(edited_meanwhile, "1", changes)
    assert edited_meanwhile.assets.operations is None


def _sitelink_rows(**over):
    base = {"asset.id": 7, "asset.type": "SITELINK", "asset.sitelink_asset.link_text": "Pricing",
            "asset.sitelink_asset.description1": "Plans for everyone",
            "asset.sitelink_asset.description2": "Cancel anytime",
            "asset.final_urls": ["https://example.com/pricing"]}
    return [{**base, **over}]


def test_sitelink_edit_changes_only_what_differs(config):
    with _gaql(_sitelink_rows()), patch("adloop.ads.write._validate_urls", return_value=({}, {})):
        preview = assets.update_sitelink(config, customer_id="1", asset_id="7", link_text="Pricing",
                                         final_url="https://example.com/plans")
    assert preview["changes"] == {"asset_id": "7", "final_url": "https://example.com/plans",
                                  "previous": {"final_url": "https://example.com/pricing"}}
    with _gaql(_sitelink_rows(**{"asset.sitelink_asset.description1": "", "asset.sitelink_asset.description2": ""})):
        lone = assets.update_sitelink(config, customer_id="1", asset_id="7", description1="Only one")
    assert "must both be set" in lone["details"][0]
    with _gaql(_sitelink_rows()), patch("adloop.ads.write._validate_urls",
                                        return_value=({"https://dead.example/": "404"}, {})):
        dead = assets.update_sitelink(config, customer_id="1", asset_id="7", final_url="https://dead.example/")
    assert "not reachable" in dead["details"][0]


def test_sitelink_apply_masks_only_the_changed_fields():
    changes = {"asset_id": "7", "final_url": "https://example.com/plans",
               "previous": {"final_url": "https://example.com/pricing"}}
    client = _Client(rows=[_row(final_urls=["https://example.com/pricing"])])
    assets._apply_update_sitelink(client, "1", changes)
    op = client.assets.operations[0]
    assert list(op.update_mask.paths) == ["final_urls"] and list(op.update.final_urls) == ["https://example.com/plans"]


def test_structured_snippets_are_edited_in_place_not_swapped(config):
    row = {"asset.id": 7, "asset.type": "STRUCTURED_SNIPPET",
           "asset.structured_snippet_asset.header": "Brands",
           "asset.structured_snippet_asset.values": ["A", "B", "C"]}
    with _gaql([row]):
        preview = assets.update_structured_snippet(config, customer_id="1", asset_id="7", values=["A", "B", "D"])
        same = assets.update_structured_snippet(config, customer_id="1", asset_id="7", values=["A", "B", "C"])
    assert preview["changes"]["header"] == "Brands"  # empty header keeps the current one
    assert preview["requires_double_confirm"] is False and "In-place edit" in preview["warnings"][0]
    assert "already has" in same["error"]

    client = _Client(rows=[_row(header="Brands", values=["A", "B", "C"])])
    assets._apply_update_structured_snippet(client, "1", preview["changes"])
    op = client.assets.operations[0]
    assert list(op.update.structured_snippet_asset.values) == ["A", "B", "D"]
    assert set(op.update_mask.paths) == {"structured_snippet_asset.header", "structured_snippet_asset.values"}


def test_ad_group_asset_links_can_be_removed(config):
    with _gaql([]):
        preview = write.remove_entity(config, customer_id="1", entity_type="ad_group_asset", entity_id="2002,7,CALLOUT")
    assert preview["entity_id"] == "2002~7~CALLOUT"
    client = _Client()
    write._apply_remove(client, "1", "ad_group_asset", "2002~7~CALLOUT")
    assert client.ads.operations[0].ad_group_asset_operation.remove == "customers/1/adGroupAssets/2002~7~CALLOUT"
    with pytest.raises(ValueError, match="adGroupId~assetId~fieldType"):
        write._apply_remove(client, "1", "ad_group_asset", "x~7")


# ---------------------------------------------------------------------------
# Snippet headers in every language Google lists
# ---------------------------------------------------------------------------


def test_google_translated_headers_are_accepted_in_their_exact_spelling(config):
    from adloop.ads.snippet_headers import (
        LOCALIZED_HEADERS,
        canonical_header,
        english_header,
    )

    assert all(len(headers) == 13 for _language, headers in LOCALIZED_HEADERS.values())
    assert (canonical_header("typen"), english_header("Typen")) == ("Typen", "Types")
    # Exact spellings are kept even where two listed headers differ only by case.
    assert canonical_header("модели") == "модели" and canonical_header("Модели") == "Модели"
    assert canonical_header("Featured hotels") == "Featured hotels"
    assert canonical_header("Freistellung") is None

    german = assets.draft_structured_snippets(
        config, customer_id="1", campaign_id="1001",
        snippets=[{"header": "dienstleistungen", "values": ["Beratung", "Planung", "Montage"]}])
    assert german["changes"]["snippets"][0]["header"] == "Dienstleistungen"
    wrong = assets.draft_structured_snippets(
        config, customer_id="1", campaign_id="1001",
        snippets=[{"header": "Leistungen", "values": ["A", "B", "C"]}])
    assert "'Leistungen' is not one of Google's snippet headers" in wrong["details"][0]


def test_an_update_keeps_the_header_google_already_holds(config):
    row = {"asset.id": 7, "asset.type": "STRUCTURED_SNIPPET",
           "asset.structured_snippet_asset.header": "Typen",
           "asset.structured_snippet_asset.values": ["SOKA-Bau", "PQ-Zertifikat", "UB Krankenkasse"]}
    with _gaql([row]):
        kept = assets.update_structured_snippet(
            config, customer_id="1", asset_id="7", values=["SOKA-Bau", "PQ-Zertifikat", "A1-Bescheinigung"])
        explicit = assets.update_structured_snippet(
            config, customer_id="1", asset_id="7", header="Typen", values=["A", "B", "C"])
    assert kept["changes"]["header"] == "Typen" and explicit["changes"]["header"] == "Typen"

    # Even a header outside the table is kept when left unchanged: Google stores it.
    legacy = {**row, "asset.structured_snippet_asset.header": "Sorten"}
    with _gaql([legacy]):
        result = assets.update_structured_snippet(config, customer_id="1", asset_id="7", values=["A", "B", "C"])
        changed = assets.update_structured_snippet(
            config, customer_id="1", asset_id="7", header="Arten", values=["A", "B", "C"])
    assert result["changes"]["header"] == "Sorten"
    assert "'Arten' is not one of" in changed["details"][0]
