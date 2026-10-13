"""Performance Max asset group asset changes: draft checks and the atomic mutate."""

import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient

from adloop import runtime, uploads
from adloop.ads import pmax
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore
from adloop.uploads import LocalUploadStore


def _png(width, height, salt=b""):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + salt


@pytest.fixture(autouse=True)
def _env(tmp_path):
    preview_store.set_plan_store(InMemoryPlanStore())
    uploads.set_upload_store(LocalUploadStore(tmp_path / "uploads"))
    runtime.set_default_config(AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "a.log"))))
    yield
    uploads.set_upload_store(None)
    runtime.set_default_config(None)


def _links(**counts):
    """Existing links: counts per field type, texts numbered."""
    links, n = [], 0
    for field_type, count in counts.items():
        for i in range(count):
            n += 1
            text = f"{field_type.lower()} {i}"
            if field_type == "DESCRIPTION":
                text = "Short" if i == 0 else f"Long description {i} " + "x" * 60
            links.append({"resource_name": f"customers/1/assetGroupAssets/9~{n}~{field_type}",
                          "field_type": field_type, "asset_id": str(n), "text": text})
    return links


def _state(links, channel="PERFORMANCE_MAX"):
    return {"asset_group_name": "Spring", "asset_group_status": "ENABLED", "campaign_id": "5",
            "campaign_name": "PMax DE", "channel": channel, "links": links}


_FULL = dict(HEADLINE=3, LONG_HEADLINE=1, DESCRIPTION=2, MARKETING_IMAGE=1, SQUARE_MARKETING_IMAGE=1, LOGO=1)


def _draft(links=None, channel="PERFORMANCE_MAX", **kwargs):
    with patch.object(pmax, "asset_group_state", return_value=_state(links if links is not None else _links(**_FULL), channel)):
        return pmax.draft_asset_group_assets(AdLoopConfig(), customer_id="1", asset_group_id="9", **kwargs)


def test_images_are_sorted_by_aspect_ratio():
    store = uploads.get_upload_store()
    landscape = store.put("local", "wide.png", _png(1200, 628))
    square = store.put("local", "sq.png", _png(1200, 1200, b"s"))
    portrait = store.put("local", "tall.png", _png(960, 1200, b"p"))
    logo = store.put("local", "logo.png", _png(512, 512, b"l"))
    preview = _draft(images=[
        {"upload_id": landscape.upload_id}, {"upload_id": square.upload_id},
        {"upload_id": portrait.upload_id}, {"upload_id": logo.upload_id, "role": "logo"},
    ])
    types = [i["field_type"] for i in preview["changes"]["add_images"]]
    assert types == ["MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "PORTRAIT_MARKETING_IMAGE", "LOGO"]
    assert preview["changes"]["counts_after"]["MARKETING_IMAGE"] == 2
    assert any("brand guidelines" in w for w in preview["warnings"])


def test_odd_ratios_and_small_images_are_refused():
    store = uploads.get_upload_store()
    odd = store.put("local", "odd.png", _png(1000, 700))
    small = store.put("local", "small.png", _png(200, 200, b"x"))
    result = _draft(images=[{"upload_id": odd.upload_id}, {"upload_id": small.upload_id}, {"url": "x", "upload_id": "y"}])
    details = " ".join(result["details"])
    assert "1000x700 is not 1.91:1, 1:1 or 4:5" in details
    assert "200x200 is too small for SQUARE_MARKETING_IMAGE" in details
    assert "exactly one of url or upload_id" in details


def test_text_limits_and_duplicates():
    result = _draft(headlines=["x" * 31])
    assert "is 31 characters; the limit is 30" in result["details"][0]
    preview = _draft(headlines=["headline 0", "Fresh offer"])
    assert preview["changes"]["add_texts"] == {"headline": ["Fresh offer"]}
    assert any("already in the asset group" in w for w in preview["warnings"])


def test_removals_cannot_drop_below_minimum_but_replacements_can():
    refused = _draft(remove_asset_ids=["1"])  # one of three headlines
    assert "HEADLINE would drop to 2" in refused["details"][0]
    replaced = _draft(remove_asset_ids=["1"], headlines=["New headline"])
    assert replaced["changes"]["remove"][0]["asset_id"] == "1"
    assert replaced["changes"]["counts_after"]["HEADLINE"] == 3


def test_maximums_and_the_short_description_rule():
    too_many = _draft(links=_links(**{**_FULL, "HEADLINE": 15}), headlines=["One more"])
    assert "HEADLINE would have 16 assets" in too_many["details"][0]
    # Removing the only short description without a short replacement.
    no_short = _draft(remove_asset_ids=["5"], descriptions=["y" * 80])
    assert any("60 characters or fewer" in d for d in no_short["details"])


def test_wrong_campaign_type_and_unknown_assets():
    assert "not Performance Max" in _draft(channel="SEARCH", headlines=["Hi"])["error"]
    unknown = _draft(remove_asset_ids=["404"])
    assert "not linked to asset group 9: 404" in unknown["details"][0]
    nothing = _draft()
    assert "Nothing to change" in nothing["details"][0]


class _FakeClient:
    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
        self.enums, self.get_type, self.copy_from = base.enums, base.get_type, base.copy_from
        self.sent = []
        self._service = SimpleNamespace(
            asset_group_path=GoogleAdsServiceClient.asset_group_path,
            asset_path=GoogleAdsServiceClient.asset_path,
            mutate=lambda customer_id, mutate_operations: self.sent.extend(mutate_operations),
        )

    def get_service(self, name):
        return self._service


def test_operations_create_link_and_unlink_in_one_request():
    store = uploads.get_upload_store()
    image = store.put("local", "wide.png", _png(1200, 628))
    preview = _draft(remove_asset_ids=["1"], headlines=["New headline"], images=[{"upload_id": image.upload_id}],
                     youtube_video_ids=["dQw4w9WgXcQ"])
    client = _FakeClient()
    result = pmax.apply_asset_group_assets(client, "1", preview["changes"])

    kinds = [op._pb.WhichOneof("operation") for op in client.sent]
    assert kinds == ["asset_operation", "asset_group_asset_operation"] * 3 + ["asset_group_asset_operation"]
    temp_paths = [op.asset_operation.create.resource_name for op in client.sent if op._pb.WhichOneof("operation") == "asset_operation"]
    assert temp_paths == ["customers/1/assets/-1", "customers/1/assets/-2", "customers/1/assets/-3"]
    link = client.sent[1].asset_group_asset_operation.create
    assert link.asset == "customers/1/assets/-1"
    assert link.asset_group == "customers/1/assetGroups/9"
    assert link.field_type == client.enums.AssetFieldTypeEnum.HEADLINE
    assert client.sent[2].asset_operation.create.image_asset.full_size.width_pixels == 1200
    assert client.sent[4].asset_operation.create.youtube_video_asset.youtube_video_id == "dQw4w9WgXcQ"
    assert client.sent[6].asset_group_asset_operation.remove == "customers/1/assetGroupAssets/9~1~HEADLINE"
    assert result["assets_added"] == 3 and result["assets_removed"] == 1


_SIGNAL_ROWS = [
    {"asset_group_signal.resource_name": "customers/1/assetGroupSignals/9~11", "asset_group_signal.search_theme.text": "running shoes",
     "asset_group_signal.approval_status": "APPROVED"},
    {"asset_group_signal.resource_name": "customers/1/assetGroupSignals/9~12", "asset_group_signal.audience.audience": "customers/1/audiences/77",
     "asset_group_signal.approval_status": "APPROVED"},
]
_AUDIENCES = [{"audience.id": 77, "audience.name": "Buyers"}, {"audience.id": 78, "audience.name": "Visitors"}]


def _gaql(signal_rows=_SIGNAL_ROWS):
    def fake(config, customer_id, query):
        return signal_rows if "FROM asset_group_signal" in query else _AUDIENCES
    return patch("adloop.ads.gaql.execute_query", side_effect=fake)


def test_signals_read_joins_audience_names():
    with _gaql():
        result = pmax.get_asset_group_signals(AdLoopConfig(), customer_id="1", asset_group_id="9")
    assert result["search_themes"][0] == {"signal_id": "9~11", "approval_status": "APPROVED", "disapproval_reasons": [],
                                          "search_theme": "running shoes"}
    assert result["audience_signals"][0]["audience_name"] == "Buyers"
    assert [a["audience_id"] for a in result["available_audiences"]] == ["77", "78"]


def test_signal_draft_skips_known_themes_and_guards_the_single_audience():
    with _gaql():
        preview = pmax.draft_asset_group_signals(
            AdLoopConfig(), customer_id="1", asset_group_id="9", add_search_themes=["Running Shoes", "trail shoes"],
        )
        assert preview["changes"]["add_search_themes"] == ["trail shoes"]
        assert any("already on the asset group" in w for w in preview["warnings"])

        clash = pmax.draft_asset_group_signals(AdLoopConfig(), customer_id="1", asset_group_id="9", audience_id="78")
        assert "remove_signal_ids=['9~12']" in clash["details"][0]
        swap = pmax.draft_asset_group_signals(
            AdLoopConfig(), customer_id="1", asset_group_id="9", audience_id="78", remove_signal_ids=["9~12"],
        )
        assert swap["changes"]["audience_id"] == "78" and swap["changes"]["remove"][0]["label"] == "Buyers"

        unknown = pmax.draft_asset_group_signals(AdLoopConfig(), customer_id="1", asset_group_id="9", audience_id="99")
        assert "not an enabled audience" in unknown["details"][0]


def test_signal_operations():
    client = _FakeClient()
    pmax.apply_asset_group_signals(client, "1", {
        "asset_group_id": "9", "add_search_themes": ["trail shoes"], "audience_id": "78",
        "remove": [{"signal_id": "9~12", "label": "Buyers"}],
    })
    assert client.sent[0].asset_group_signal_operation.remove == "customers/1/assetGroupSignals/9~12"
    assert client.sent[1].asset_group_signal_operation.create.search_theme.text == "trail shoes"
    assert client.sent[2].asset_group_signal_operation.create.audience.audience == "customers/1/audiences/78"
    assert client.sent[2].asset_group_signal_operation.create.asset_group == "customers/1/assetGroups/9"


def test_asset_groups_can_be_paused_enabled_and_removed():
    from adloop.ads import write

    preview = write.pause_entity(AdLoopConfig(), customer_id="1", entity_type="asset_group", entity_id="9")
    assert preview["operation"] == "pause_entity" and preview["entity_type"] == "asset_group"

    sent = []
    service = SimpleNamespace(
        asset_group_path=GoogleAdsServiceClient.asset_group_path,
        mutate_asset_groups=lambda customer_id, operations: sent.extend(operations) or SimpleNamespace(
            results=[SimpleNamespace(resource_name="customers/1/assetGroups/9")]),
    )
    client = _FakeClient()
    client.get_service = lambda name: service
    write._apply_status_change(client, "1", "asset_group", "9", "PAUSED")
    assert sent[0].update.status == client.enums.AssetGroupStatusEnum.PAUSED
    assert list(sent[0].update_mask.paths) == ["status"]
    write._apply_remove(client, "1", "asset_group", "9")
    assert sent[1].remove == "customers/1/assetGroups/9"
