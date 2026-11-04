"""Account-level sitelinks, and field_types on draft_image_assets.

Account level is explicit (scope="account"), never inferred from an empty
campaign_id. Plans run through ``confirm_and_apply(dry_run=True)`` with real
protos and the real validate-only wrapper; only the services are fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from tests.test_image_fetch import png

SITELINKS = [
    {"link_text": f"Link {n}", "final_url": f"https://example.com/{n}"} for n in range(4)
]


@pytest.fixture(autouse=True)
def _plans():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="1234567890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")),
    )


class _Service:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, attr):
        if attr.endswith("_path"):
            plural = {"asset": "assets", "campaign": "campaigns"}[attr.removesuffix("_path")]
            return lambda cid, entity: f"customers/{cid}/{plural}/{entity}"
        if attr.startswith("mutate"):
            def send(request=None, **_kwargs):
                self._client.sent.append(request)
                return SimpleNamespace(results=[], mutate_operation_responses=[], partial_failure_error=None)
            return send
        raise AttributeError(attr)


class _Client:
    """Real protos and enums; services record instead of reaching Google."""

    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True,
                               version=GOOGLE_ADS_API_VERSION)
        self.enums = base.enums
        self.get_type = base.get_type
        self.sent = []

    def get_service(self, *_args, **_kwargs):
        return _Service(self)

    def operations(self):
        assert len(self.sent) == 1 and self.sent[0].validate_only is True
        return list(self.sent[0].mutate_operations)


@pytest.fixture
def client(monkeypatch):
    fake = _Client()
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: fake)
    # conftest stubs this out; these tests are about the real path.
    monkeypatch.setattr(
        write, "_validate_with_google",
        lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
    )
    monkeypatch.setattr(write, "_validate_urls", lambda urls, timeout=10: ({u: None for u in urls}, {}))
    return fake


@pytest.fixture
def images(tmp_path):
    def write_png(count=1, width=1200, height=1200):
        paths = []
        for n in range(count):
            path = tmp_path / f"image-{n}.png"
            path.write_bytes(png(width, height, bytes([n])))
            paths.append(str(path))
        return paths
    return write_png


def _dry_run(config, preview):
    assert "plan_id" in preview, preview
    result = write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
    assert result["status"] == "DRY_RUN_SUCCESS", result


class TestSitelinks:
    def _draft(self, config, **kwargs):
        return write.draft_sitelinks(config, customer_id="1234567890", sitelinks=SITELINKS, **kwargs)

    def test_account_scope_links_through_customer_asset(self, config, client):
        preview = self._draft(config, scope="account")
        assert preview["entity_type"] == "customer_asset"
        assert preview["changes"]["scope"] == "account"
        assert any("every eligible campaign" in w for w in preview["warnings"])

        _dry_run(config, preview)
        links = client.operations()[4:]
        assert len(links) == 4
        link = links[0].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.SITELINK
        assert link.asset == "customers/1234567890/assets/-1"

    def test_a_campaign_id_still_links_to_the_campaign(self, config, client):
        preview = self._draft(config, campaign_id="1001")
        assert preview["entity_type"] == "campaign_asset"
        assert "warnings" not in preview
        _dry_run(config, preview)
        link = client.operations()[4].campaign_asset_operation.create
        assert link.campaign == "customers/1234567890/campaigns/1001"
        assert link.field_type == client.enums.AssetFieldTypeEnum.SITELINK

    @pytest.mark.parametrize("kwargs, needle", [
        ({}, "account-wide linking needs scope='account'"),
        ({"scope": "account", "campaign_id": "1001"}, "leave campaign_id"),
        ({"scope": "ad_group"}, "scope must be one of"),
        ({"campaign_id": "abc"}, "must be numeric"),
    ])
    def test_scope_refusals(self, config, kwargs, needle):
        result = self._draft(config, **kwargs)
        assert any(needle in d for d in result["details"]), result


class TestImageFieldTypes:
    def _draft(self, config, paths, **kwargs):
        return write.draft_image_assets(config, customer_id="1234567890", image_paths=paths, **kwargs)

    def test_a_campaign_link_defaults_to_ad_image(self, config, client, images):
        preview = self._draft(config, images(), campaign_id="1001")
        assert preview["changes"]["field_types"] == ["AD_IMAGE"]
        assert "warnings" not in preview
        _dry_run(config, preview)
        link = client.operations()[1].campaign_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.AD_IMAGE

    def test_field_types_apply_per_image_on_a_campaign(self, config, client, images):
        preview = self._draft(config, images(2), campaign_id="1001", field_types=["ad_image", "BUSINESS_LOGO"])
        assert preview["changes"]["field_types"] == ["AD_IMAGE", "BUSINESS_LOGO"]
        _dry_run(config, preview)
        ops = client.operations()
        kinds = client.enums.AssetFieldTypeEnum
        assert ops[2].campaign_asset_operation.create.field_type == kinds.AD_IMAGE
        assert ops[3].campaign_asset_operation.create.field_type == kinds.BUSINESS_LOGO

    def test_a_business_logo_can_go_on_the_account(self, config, client, images):
        with patch("adloop.ads.gaql.execute_query", return_value=[]):  # no account logo yet
            preview = self._draft(config, images(), scope="account", field_types=["BUSINESS_LOGO"])
        assert preview["entity_type"] == "customer_asset"
        assert any("every eligible campaign" in w for w in preview["warnings"])
        _dry_run(config, preview)
        link = client.operations()[1].customer_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.BUSINESS_LOGO

    def test_an_account_with_a_logo_is_refused_at_the_draft(self, config, images):
        # Google allows one; a second only fails at the dry run with RESOURCE_LIMIT
        # (ENABLED_FAVICON_ASSET_LINKS_PER_CUSTOMER), seen live on 2026-11-04.
        existing = [{"asset.id": 322647191710, "asset.name": "android-chrome-192x192.png"}]
        with patch("adloop.ads.gaql.execute_query", return_value=existing) as query:
            result = self._draft(config, images(), scope="account", field_types=["BUSINESS_LOGO"])
        assert "customer_asset.field_type = 'BUSINESS_LOGO'" in query.call_args.args[2]
        assert "customer_asset.status = 'ENABLED'" in query.call_args.args[2]
        [detail] = result["details"]
        assert "already has a business logo (asset 322647191710 'android-chrome-192x192.png')" in detail
        assert 'entity_id="322647191710~BUSINESS_LOGO"' in detail

    def test_two_logos_on_the_account_are_refused_without_a_read(self, config, images):
        result = self._draft(config, images(2), scope="account", field_types=["BUSINESS_LOGO"] * 2)
        assert result["details"] == ["Google allows one business logo per account; this draft links 2."]

    def test_a_campaign_logo_does_not_read_the_account(self, config, images):
        # conftest refuses any GAQL; campaigns took a second logo in the live dry run.
        preview = self._draft(config, images(), campaign_id="1001", field_types=["BUSINESS_LOGO"])
        assert preview["changes"]["field_types"] == ["BUSINESS_LOGO"]

    def test_account_scope_has_no_default_field_type(self, config, images):
        result = self._draft(config, images(), scope="account")
        assert any("needs field_types" in d for d in result["details"])

    def test_ad_image_is_refused_on_the_account(self, config, images):
        result = self._draft(config, images(), scope="account", field_types=["AD_IMAGE"])
        assert any("AD_IMAGE cannot be linked at account level" in d for d in result["details"])

    def test_other_field_types_are_refused(self, config, images):
        result = self._draft(config, images(), campaign_id="1001", field_types=["MARKETING_IMAGE"])
        assert any("'MARKETING_IMAGE' is not supported" in d for d in result["details"])

    def test_one_entry_per_image(self, config, images):
        result = self._draft(config, images(2), campaign_id="1001", field_types=["AD_IMAGE"])
        assert any("1 entries for 2 images" in d for d in result["details"])

    def test_an_empty_campaign_id_never_means_the_account(self, config, images):
        result = self._draft(config, images(), field_types=["BUSINESS_LOGO"])
        assert any("account-wide linking needs scope='account'" in d for d in result["details"])

    def test_a_plan_without_field_types_links_as_ad_image(self, client, images, monkeypatch):
        """Plans drafted before this change carry no scope and no field_type."""

        class _Responding(_Service):
            def mutate(self, *, customer_id, mutate_operations):
                client.sent.append(SimpleNamespace(validate_only=True, mutate_operations=mutate_operations))
                return SimpleNamespace(mutate_operation_responses=[])

        client.get_service = lambda *_a, **_k: _Responding(client)
        monkeypatch.setattr(write, "populate_image_asset", lambda *_a, **_k: None)
        write._apply_create_image_assets(client, "1", {"campaign_id": "1001", "images": [{"name": "old"}]})
        link = client.operations()[1].campaign_asset_operation.create
        assert link.field_type == client.enums.AssetFieldTypeEnum.AD_IMAGE
        assert link.campaign == "customers/1/campaigns/1001"


@pytest.mark.asyncio
async def test_the_mcp_tools_take_the_new_parameters():
    from adloop.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools["draft_sitelinks"].parameters["properties"]["scope"].get("description")
    assert "campaign_id" not in tools["draft_sitelinks"].parameters.get("required", [])
    assert "sitelinks" in tools["draft_sitelinks"].parameters.get("required", [])
    properties = tools["draft_image_assets"].parameters["properties"]
    assert properties["scope"].get("description") and properties["field_types"].get("description")
