"""Tests for the PMax campaign + asset group builders (no network)."""

from __future__ import annotations

import struct
import zlib
from unittest.mock import MagicMock, patch

import pytest

from adloop.ads import pmax_write
from adloop.ads.write import _apply_remove, remove_entity, validate_plan_only
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety.preview import get_plan, remove_plan


def make_png(path, w, h):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80" * (w * 3) for _ in range(h))
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def config():
    return AdLoopConfig(
        ads=AdsConfig(customer_id="3865591631", developer_token="test"),
        safety=SafetyConfig(max_daily_budget=50.0, require_dry_run=True),
    )


@pytest.fixture
def real_client():
    """A real (offline) client so proto field names are checked for real."""
    from google.ads.googleads.client import GoogleAdsClient

    creds = MagicMock()
    return GoogleAdsClient(
        credentials=creds, developer_token="x", version="v23", use_proto_plus=True
    )


@pytest.fixture
def imgs(tmp_path):
    return {
        "landscape": [make_png(tmp_path / "land.png", 1200, 628)],
        "square": [make_png(tmp_path / "sq.png", 600, 600)],
        "logo": [make_png(tmp_path / "logo.png", 256, 256)],
    }


def asset_kwargs(imgs, **over):
    kw = dict(
        campaign_id="123",
        asset_group_name="AG1",
        final_url="https://example.com/x/",
        headlines=["One", "Two", "Three"],
        long_headlines=["A long headline"],
        descriptions=["Short one", "Another description"],
        business_name="Senior Care Consulting",
        images=imgs,
        call_to_action="CONTACT_US",
        search_themes=["senior living advisor"],
    )
    kw.update(over)
    return kw


GEO = ["1023191", "1023200"]


class TestDraftPmaxCampaign:
    def test_plan_is_paused_presence_maxconv(self, config):
        r = pmax_write.draft_pmax_campaign(
            config, customer_id="3865591631", campaign_name="SCC | PMax",
            daily_budget=16.4, geo_target_ids=GEO, language_ids=["1000"],
            brand_exclusions=["Senior Care Consulting"],
        )
        assert r["status"] == "PENDING_CONFIRMATION"
        ch = r["changes"]
        assert ch["status"] == "PAUSED"
        assert ch["channel_type"] == "PERFORMANCE_MAX"
        assert ch["bidding_strategy"] == "MAXIMIZE_CONVERSIONS"
        assert ch["geo_target_type"] == "PRESENCE"
        assert ch["final_url_expansion"] == "OFF"
        remove_plan(r["plan_id"])

    def test_requires_geo_language_budget_name(self, config):
        r = pmax_write.draft_pmax_campaign(config, customer_id="1")
        assert r["error"] == "Validation failed"
        assert len(r["details"]) == 4

    def test_budget_cap_enforced(self, config):
        r = pmax_write.draft_pmax_campaign(
            config, customer_id="1", campaign_name="x", daily_budget=500,
            geo_target_ids=GEO, language_ids=["1000"],
        )
        assert "exceeds maximum" in r["error"]

    def test_blocked_operation(self):
        cfg = AdLoopConfig(
            ads=AdsConfig(customer_id="1", developer_token="t"),
            safety=SafetyConfig(blocked_operations=["create_pmax_campaign"]),
        )
        r = pmax_write.draft_pmax_campaign(
            cfg, customer_id="1", campaign_name="x", daily_budget=5,
            geo_target_ids=GEO, language_ids=["1000"],
        )
        assert "blocked" in r["error"]

    def test_no_status_parameter(self):
        import inspect

        assert "status" not in inspect.signature(pmax_write.draft_pmax_campaign).parameters

    def test_enabled_is_refused_at_build(self, real_client):
        with pytest.raises(ValueError, match="PAUSED"):
            pmax_write.build_campaign_ops(
                real_client, "1", {"status": "ENABLED", "campaign_name": "x", "daily_budget": 1}
            )


class TestCampaignOps:
    def changes(self):
        return {
            "campaign_name": "SCC | PMax", "daily_budget": 16.4, "status": "PAUSED",
            "geo_target_ids": GEO, "language_ids": ["1000"],
        }

    def test_ops_content(self, real_client):
        from google.ads.googleads.v23.enums.types import campaign_status, positive_geo_target_type

        brands = [{"name": "B", "id": "/g/123", "urls": []}]
        ops, labels = pmax_write.build_campaign_ops(real_client, "1", self.changes(), brands)
        assert labels == [
            "campaign_budget", "campaign", "geo_target", "geo_target",
            "language_target", "brand_shared_set", "brand_criterion", "campaign_brand_list",
        ]
        budget = ops[0].campaign_budget_operation.create
        assert budget.amount_micros == 16_400_000
        assert budget.explicitly_shared is False
        c = ops[1].campaign_operation.create
        assert c.status == campaign_status.CampaignStatusEnum.CampaignStatus.PAUSED
        assert c.advertising_channel_type.name == "PERFORMANCE_MAX"
        assert "maximize_conversions" in c
        assert c.maximize_conversions.target_cpa_micros == 0
        assert c.geo_target_type_setting.positive_geo_target_type == (
            positive_geo_target_type.PositiveGeoTargetTypeEnum.PositiveGeoTargetType.PRESENCE
        )
        opted = {s.asset_automation_type.name: s.asset_automation_status.name
                 for s in c.asset_automation_settings}
        assert opted["FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"] == "OPTED_OUT"
        assert opted["TEXT_ASSET_AUTOMATION"] == "OPTED_OUT"
        assert set(opted.values()) == {"OPTED_OUT"}
        assert ops[2].campaign_criterion_operation.create.location.geo_target_constant == (
            "geoTargetConstants/1023191"
        )
        assert ops[4].campaign_criterion_operation.create.language.language_constant == (
            "languageConstants/1000"
        )
        assert ops[5].shared_set_operation.create.type_.name == "BRANDS"
        assert ops[6].shared_criterion_operation.create.brand.entity_id == "/g/123"
        assert c.brand_guidelines_enabled is False
        neg = ops[7].campaign_criterion_operation.create
        assert neg.negative is True
        assert neg.brand_list.shared_set.endswith("/sharedSets/-3")

    def test_no_brand_ops_without_brands(self, real_client):
        _ops, labels = pmax_write.build_campaign_ops(real_client, "1", self.changes(), [])
        assert "brand_shared_set" not in labels

    def test_apply_and_validate_only_flag(self, real_client):
        svc = MagicMock()
        paths = MagicMock(
            campaign_budget_path=lambda c, i: f"customers/{c}/campaignBudgets/{i}",
            campaign_path=lambda c, i: f"customers/{c}/campaigns/{i}",
            shared_set_path=lambda c, i: f"customers/{c}/sharedSets/{i}",
        )
        paths.suggest_brands.return_value = MagicMock(brands=[])
        real_client.get_service = MagicMock(
            side_effect=lambda n: svc if n == "GoogleAdsService" else paths
        )
        svc.mutate.return_value = MagicMock(mutate_operation_responses=[])
        out = pmax_write.apply_create_pmax_campaign(
            real_client, "1", self.changes(), validate_only=True
        )
        req = svc.mutate.call_args.kwargs["request"]
        assert req.validate_only is True
        assert out["validate_only"] is True

    def test_brand_resolution_reports_unresolved(self):
        client = MagicMock()
        brand = MagicMock(); brand.name = "Senior Care Consulting"; brand.id = "/g/1"; brand.urls = []
        client.get_service.return_value.suggest_brands.side_effect = [
            MagicMock(brands=[brand]), MagicMock(brands=[]),
        ]
        ok, bad = pmax_write._resolve_brand_ids(
            client, "1", ["Senior Care Consulting", "Nobody"]
        )
        assert [b["id"] for b in ok] == ["/g/1"]
        assert bad == ["Nobody"]


class TestDraftPmaxAssetGroup:
    def test_valid_plan(self, config, imgs):
        r = pmax_write.draft_pmax_asset_group(
            config, customer_id="3865591631", **asset_kwargs(imgs)
        )
        assert r["status"] == "PENDING_CONFIRMATION"
        assert len(r["changes"]["images"]) == 3
        assert r["changes"]["status"] == "PAUSED"
        remove_plan(r["plan_id"])

    def test_lists_every_unmet_minimum(self, config):
        r = pmax_write.draft_pmax_asset_group(
            config, customer_id="1", campaign_id="1", asset_group_name="x",
            final_url="https://e.com", headlines=["a"], long_headlines=[],
            descriptions=["d" * 80], business_name="b" * 30, images={},
        )
        text = "\n".join(r["details"])
        assert "headlines: need at least 3" in text
        assert "long_headlines: need at least 1" in text
        assert "descriptions: need at least 2" in text
        assert "at least one must be 60" in text
        assert "business_name" in text
        assert "images.landscape: need at least 1" in text
        assert "images.square: need at least 1" in text
        assert "images.logo: need at least 1" in text

    def test_text_too_long(self, config, imgs):
        r = pmax_write.draft_pmax_asset_group(
            config, customer_id="1",
            **asset_kwargs(imgs, headlines=["x" * 31, "b", "c"]),
        )
        assert any("31 chars, max 30" in d for d in r["details"])

    def test_bad_aspect_and_size_caught_before_upload(self, config, tmp_path, imgs):
        bad = dict(imgs)
        bad["landscape"] = [make_png(tmp_path / "wide.png", 1000, 1000)]
        bad["square"] = [make_png(tmp_path / "tiny.png", 200, 200)]
        r = pmax_write.draft_pmax_asset_group(config, customer_id="1", **asset_kwargs(bad))
        text = "\n".join(r["details"])
        assert "aspect 1000:1000" in text and "needs 1.91" in text
        assert "200x200 is below the 300x300" in text

    def test_missing_file_and_unknown_kind(self, config, imgs):
        bad = dict(imgs)
        bad["logo"] = ["C:/nope/missing.png"]
        bad["video"] = ["x"]
        r = pmax_write.draft_pmax_asset_group(config, customer_id="1", **asset_kwargs(bad))
        text = "\n".join(r["details"])
        assert "does not exist" in text
        assert "unknown image kind 'video'" in text

    def test_landscape_logo_4_to_1_optional_and_validated(self, config, imgs, tmp_path):
        ok = dict(imgs); ok["landscape_logo"] = [make_png(tmp_path / "wl.png", 1024, 256)]
        r = pmax_write.draft_pmax_asset_group(config, customer_id="1", **asset_kwargs(ok))
        assert r["status"] == "PENDING_CONFIRMATION"
        remove_plan(r["plan_id"])
        bad = dict(imgs); bad["landscape_logo"] = [make_png(tmp_path / "wl2.png", 600, 300)]
        r = pmax_write.draft_pmax_asset_group(config, customer_id="1", **asset_kwargs(bad))
        assert any("needs 4.0" in d for d in r["details"])


class TestAssetGroupOps:
    def test_ops_content(self, config, imgs, real_client):
        r = pmax_write.draft_pmax_asset_group(
            config, customer_id="1", audience_id="777", **asset_kwargs(imgs)
        )
        ch = get_plan(r["plan_id"]).changes
        remove_plan(r["plan_id"])
        ops, labels = pmax_write.build_asset_group_ops(real_client, "1", ch)
        assert labels.count("asset_group") == 1
        # 3 headlines + 1 long + 2 desc + name + CTA + 3 images
        assert labels.count("asset") == 11
        assert labels.count("asset_group_asset") == 11
        assert labels.count("search_theme") == 1
        assert labels.count("audience_signal") == 1
        # asset group must come after all assets and before every link
        ag_i = labels.index("asset_group")
        assert all(l == "asset" for l in labels[:ag_i])
        assert all(l != "asset" for l in labels[ag_i + 1:])
        ag = ops[ag_i].asset_group_operation.create
        assert ag.status.name == "PAUSED"
        assert list(ag.final_urls) == ["https://example.com/x/"]
        fields = sorted(
            o.asset_group_asset_operation.create.field_type.name
            for o in ops if o.asset_group_asset_operation.create.asset
        )
        assert fields.count("HEADLINE") == 3
        assert "SQUARE_MARKETING_IMAGE" in fields and "MARKETING_IMAGE" in fields
        assert "LOGO" in fields and "BUSINESS_NAME" in fields
        assert "CALL_TO_ACTION_SELECTION" in fields

    def test_apply_validate_only(self, config, imgs, real_client):
        r = pmax_write.draft_pmax_asset_group(config, customer_id="1", **asset_kwargs(imgs))
        ch = get_plan(r["plan_id"]).changes
        remove_plan(r["plan_id"])
        svc = MagicMock()
        real_client.get_service = MagicMock(
            side_effect=lambda n: svc if n == "GoogleAdsService" else MagicMock(
                asset_path=lambda c, i: f"customers/{c}/assets/{i}",
                asset_group_path=lambda c, i: f"customers/{c}/assetGroups/{i}",
                campaign_path=lambda c, i: f"customers/{c}/campaigns/{i}",
            )
        )
        svc.mutate.return_value = MagicMock(mutate_operation_responses=[])
        out = pmax_write.apply_create_pmax_asset_group(real_client, "1", ch, validate_only=True)
        assert svc.mutate.call_args.kwargs["request"].validate_only is True
        assert out["validate_only"] is True

    def test_apply_revalidates_images(self, imgs, real_client, tmp_path):
        ch = {"images": [{"kind": "square", "path": str(tmp_path / "gone.png")}]}
        with pytest.raises(ValueError, match="does not exist"):
            pmax_write.apply_create_pmax_asset_group(real_client, "1", ch)


class TestValidatePlanOnly:
    def test_rejects_non_pmax_and_unknown(self, config):
        assert "No pending plan" in validate_plan_only(config, plan_id="nope")["error"]

    def test_routes_pmax_plan_with_validate_only(self, config):
        r = pmax_write.draft_pmax_campaign(
            config, customer_id="3865591631", campaign_name="x", daily_budget=5,
            geo_target_ids=GEO, language_ids=["1000"],
        )
        with patch("adloop.ads.client.get_ads_client"), patch(
            "adloop.ads.pmax_write.apply_create_pmax_campaign", return_value={"validate_only": True}
        ) as m:
            out = validate_plan_only(config, plan_id=r["plan_id"])
        assert out["status"] == "VALIDATE_ONLY_OK"
        assert m.call_args.kwargs["validate_only"] is True
        assert get_plan(r["plan_id"]) is not None  # stays pending
        remove_plan(r["plan_id"])


class TestRemoveAdGroupAd:
    def test_draft(self, config):
        r = remove_entity(config, customer_id="1", entity_type="ad_group_ad", entity_id="11~22")
        assert r["entity_type"] == "ad_group_ad"
        assert r["changes"] == {"action": "REMOVE"}
        remove_plan(r["plan_id"])

    def test_apply_builds_ad_group_ad_resource(self):
        client = MagicMock()
        op = MagicMock()
        client.get_type.return_value = op
        svc = client.get_service.return_value
        svc.mutate_ad_group_ads.return_value = MagicMock(results=[MagicMock(resource_name="rn")])
        out = _apply_remove(client, "1", "ad_group_ad", "11~22")
        assert op.remove == "customers/1/adGroupAds/11~22"
        assert out == {"resource_name": "rn"}
        client.get_service.assert_called_with("AdGroupAdService")


class TestServerGuards:
    def test_new_tools_are_customer_locked(self, monkeypatch):
        from adloop import server

        for name in ("draft_pmax_campaign", "draft_pmax_asset_group", "draft_pmax_url_expansion"):
            assert name in server._WRITE_TOOL_NAMES
            monkeypatch.setenv("ADLOOP_ALLOWED_CUSTOMER_ID", "3865591631")
            with pytest.raises(RuntimeError, match="restricted to customer_id"):
                server._enforce_write_customer(name, {"customer_id": "999"})
            kw = {}
            server._enforce_write_customer(name, kw)
            assert kw["customer_id"] == "3865591631"


class TestRemoveAssetGroup:
    def test_draft_accepts_asset_group(self, config):
        r = remove_entity(config, customer_id="3865591631", entity_type="asset_group", entity_id="6768635345")
        assert r["entity_type"] == "asset_group"
        assert r["changes"] == {"action": "REMOVE"}
        remove_plan(r["plan_id"])

    def test_apply_builds_asset_group_remove_op(self, real_client):
        svc = MagicMock()
        real_client.get_service = MagicMock(return_value=svc)
        out = _apply_remove(real_client, "3865591631", "asset_group", "6768635345")
        req = svc.mutate.call_args.kwargs["request"]
        assert req.validate_only is False
        assert req.mutate_operations[0].asset_group_operation.remove == "customers/3865591631/assetGroups/6768635345"
        assert out["status"] == "removed"

    def test_validate_only_routes_asset_group_removal(self, config, real_client):
        svc = MagicMock()
        real_client.get_service = MagicMock(return_value=svc)
        r = remove_entity(config, customer_id="386-559-1631", entity_type="asset_group", entity_id="6768635345")
        with patch("adloop.ads.client.get_ads_client", return_value=real_client):
            out = validate_plan_only(config, plan_id=r["plan_id"])
        assert out["status"] == "VALIDATE_ONLY_OK"
        assert svc.mutate.call_args.kwargs["request"].validate_only is True
        assert out["result"]["status"] == "validated"
        assert get_plan(r["plan_id"]) is not None
        remove_plan(r["plan_id"])

    def test_validate_only_still_rejects_other_removals(self, config):
        r = remove_entity(config, customer_id="1", entity_type="campaign", entity_id="5")
        assert "error" in validate_plan_only(config, plan_id=r["plan_id"])
        remove_plan(r["plan_id"])


class TestUrlExpansion:
    def test_read_reports_state(self, config):
        from adloop.ads import pmax

        rows = [{
            "campaign.id": 24427159679, "campaign.name": "x", "campaign.status": "PAUSED",
            "campaign.asset_automation_settings": [
                {"asset_automation_type": "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
                 "asset_automation_status": "OPTED_OUT"},
                {"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_IN"},
            ],
        }]
        with patch("adloop.ads.gaql.execute_query", return_value=rows) as q:
            out = pmax.get_pmax_url_expansion(config, customer_id="1", campaign_id="24427159679")
        assert "campaign.id = 24427159679" in q.call_args.args[2]
        assert "url_expansion_opt_out" not in q.call_args.args[2]
        assert out["campaigns"][0]["final_url_expansion"] == "OPTED_OUT"

    def test_read_not_set_and_bad_id(self, config):
        from adloop.ads import pmax

        with patch("adloop.ads.gaql.execute_query", return_value=[{"campaign.id": 1}]):
            assert pmax.get_pmax_url_expansion(config, customer_id="1")["campaigns"][0]["final_url_expansion"] == "NOT_SET"
        assert "error" in pmax.get_pmax_url_expansion(config, customer_id="1", campaign_id="1 OR 1=1")

    def test_draft(self, config):
        r = pmax_write.draft_pmax_url_expansion(config, customer_id="1", campaign_id="9")
        assert r["changes"]["status"] == "OPTED_OUT"
        remove_plan(r["plan_id"])
        assert "error" in pmax_write.draft_pmax_url_expansion(config, customer_id="1", campaign_id="x")

    def test_apply_preserves_other_settings_and_sets_mask(self, real_client):
        svc = MagicMock()
        paths = MagicMock(campaign_path=lambda c, i: f"customers/{c}/campaigns/{i}")
        real_client.get_service = MagicMock(side_effect=lambda n: svc if n == "GoogleAdsService" else paths)
        other = real_client.get_type("Campaign").AssetAutomationSetting()
        other.asset_automation_type = real_client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION
        other.asset_automation_status = real_client.enums.AssetAutomationStatusEnum.OPTED_IN
        row = MagicMock(); row.campaign.asset_automation_settings = [other]
        svc.search_stream.return_value = [MagicMock(results=[row])]
        out = pmax_write.apply_update_pmax_url_expansion(
            real_client, "1", {"campaign_id": "9", "status": "OPTED_OUT"}, validate_only=True
        )
        req = svc.mutate.call_args.kwargs["request"]
        assert req.validate_only is True
        u = req.mutate_operations[0].campaign_operation
        got = {s.asset_automation_type.name: s.asset_automation_status.name for s in u.update.asset_automation_settings}
        assert got == {"TEXT_ASSET_AUTOMATION": "OPTED_IN", "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "OPTED_OUT"}
        assert list(u.update_mask.paths) == ["asset_automation_settings"]
        assert out["status"] == "OPTED_OUT"

    def test_read_parses_text_proto_strings_as_returned_by_execute_query(self, config):
        from adloop.ads import pmax

        rows = [{
            "campaign.id": 1, "campaign.name": "n", "campaign.status": "PAUSED",
            "campaign.asset_automation_settings": [
                "asset_automation_type: FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION\nasset_automation_status: OPTED_OUT\n",
                "asset_automation_type: TEXT_ASSET_AUTOMATION\nasset_automation_status: OPTED_IN\n",
            ],
        }]
        with patch("adloop.ads.gaql.execute_query", return_value=rows):
            c = pmax.get_pmax_url_expansion(config, customer_id="1")["campaigns"][0]
        assert c["final_url_expansion"] == "OPTED_OUT"
        assert c["asset_automation_settings"]["TEXT_ASSET_AUTOMATION"] == "OPTED_IN"
