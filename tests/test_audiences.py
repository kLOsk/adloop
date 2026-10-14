"""Google Ads audiences (#48): reads, draft rules and the atomic mutate."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient

from adloop import runtime
from adloop.ads import audiences
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


@pytest.fixture(autouse=True)
def _env(tmp_path):
    preview_store.set_plan_store(InMemoryPlanStore())
    runtime.set_default_config(AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "a.log"))))
    yield
    runtime.set_default_config(None)


_LISTS = [
    {"user_list.id": 11, "user_list.name": "Buyers 30d", "user_list.type": "RULE_BASED", "user_list.membership_status": "OPEN",
     "user_list.size_for_search": 4200, "user_list.size_for_display": 9000, "user_list.eligible_for_search": True,
     "user_list.eligible_for_display": True, "user_list.membership_life_span": 30, "user_list.account_user_list_status": "ENABLED"},
    {"user_list.id": 12, "user_list.name": "Newsletter", "user_list.type": "CRM_BASED", "user_list.membership_status": "OPEN",
     "user_list.size_for_search": 300, "user_list.size_for_display": 800, "user_list.eligible_for_search": True,
     "user_list.eligible_for_display": True, "user_list.membership_life_span": 540, "user_list.account_user_list_status": "ENABLED"},
    {"user_list.id": 13, "user_list.name": "Old list", "user_list.type": "RULE_BASED", "user_list.membership_status": "CLOSED",
     "user_list.size_for_search": 2000, "user_list.size_for_display": 3000, "user_list.eligible_for_search": True,
     "user_list.eligible_for_display": True, "user_list.membership_life_span": 30, "user_list.account_user_list_status": "ENABLED"},
]
_CUSTOM = [{"custom_audience.id": 21, "custom_audience.name": "Competitor searchers", "custom_audience.type": "SEARCH"}]
_COMBINED = [{"combined_audience.id": 31, "combined_audience.name": "Buyers and runners"}]
_SEGMENTS = [{"user_interest.user_interest_id": 80101, "user_interest.name": "Running Shoes", "user_interest.taxonomy_type": "IN_MARKET",
              "user_interest.launched_to_all": True}]


def _gaql(channel="SEARCH", restrictions=None, campaign_criteria=(), ad_group_criteria=()):
    sent = []

    def fake(config, customer_id, query):
        sent.append(query)
        if "FROM user_list" in query:
            return _LISTS
        if "FROM custom_audience" in query:
            return _CUSTOM
        if "FROM combined_audience" in query:
            return _COMBINED
        if "FROM user_interest" in query:
            return _SEGMENTS
        if "FROM ad_group_criterion" in query:
            return list(ad_group_criteria)
        if "FROM campaign_criterion" in query:
            return list(campaign_criteria)
        if "FROM ad_group" in query:
            return [{"ad_group.id": 7, "ad_group.name": "Shoes", "ad_group.targeting_setting.target_restrictions": restrictions or [],
                     "campaign.id": 5, "campaign.name": "Search DE", "campaign.advertising_channel_type": channel}]
        if "FROM campaign" in query:
            return [{"campaign.id": 5, "campaign.name": "Search DE", "campaign.advertising_channel_type": channel,
                     "campaign.targeting_setting.target_restrictions": restrictions or []}]
        raise AssertionError(query)

    return patch("adloop.ads.gaql.execute_query", side_effect=fake), sent


def _draft(gaql_kwargs=None, **kwargs):
    ctx, _sent = _gaql(**(gaql_kwargs or {}))
    with ctx:
        return audiences.draft_audience_attachment(AdLoopConfig(), customer_id="1", **kwargs)


def test_lists_skip_closed_accounts_and_flag_small_and_closed_lists():
    ctx, _ = _gaql()
    with ctx:
        result = audiences.get_audience_lists(AdLoopConfig(), customer_id="1")
    assert [u["user_list_id"] for u in result["user_lists"]] == ["11", "12", "13"]
    assert result["custom_segments"][0]["custom_audience_id"] == "21"
    assert result["combined_segments"][0]["name"] == "Buyers and runners"
    assert any("Newsletter" in i and "1,000" in i for i in result["insights"])
    assert any("Old list" in i for i in result["insights"])


def test_segment_search_matches_case_insensitively_and_ignores_launched_to_all():
    rows = [
        {"user_interest.user_interest_id": 80276, "user_interest.name": "Software", "user_interest.taxonomy_type": "IN_MARKET"},
        {"user_interest.user_interest_id": 80279, "user_interest.name": "Business & Productivity Software", "user_interest.taxonomy_type": "IN_MARKET"},
        {"user_interest.user_interest_id": 92000, "user_interest.name": "Shoppers", "user_interest.taxonomy_type": "AFFINITY"},
    ]
    sent = []

    def fake(config, customer_id, query):
        sent.append(query)
        return rows

    with patch("adloop.ads.gaql.execute_query", side_effect=fake):
        result = audiences.search_audience_segments(AdLoopConfig(), customer_id="1", query="software", kind="in_market")
        assert "error" in audiences.search_audience_segments(AdLoopConfig(), customer_id="1", query="x")
    # Names starting with the query come first.
    assert [s["name"] for s in result["segments"]] == ["Software", "Business & Productivity Software"]
    assert result["segments"][0] == {"segment_id": "80276", "name": "Software", "kind": "in_market"}
    assert "IN ('IN_MARKET')" in sent[0] and "LIKE" not in sent[0]


def test_observation_is_the_default_and_written_explicitly():
    preview = _draft(campaign_id="5", user_list_ids=["11"], segment_ids=["80101"])
    changes = preview["changes"]
    assert changes["set_audience_mode"] == "observation"
    assert changes["audience_mode_before"] == "not set"
    assert [a["name"] for a in changes["audiences"]] == ["Buyers 30d", "Running Shoes"]
    assert preview["requires_double_confirm"] is False


def test_targeting_needs_double_confirmation_and_a_loud_warning():
    preview = _draft(campaign_id="5", user_list_ids=["11"], mode="targeting")
    assert preview["requires_double_confirm"] is True
    assert preview["warnings"][0].startswith("TARGETING narrows delivery")


def test_mode_already_observation_is_left_alone():
    preview = _draft(gaql_kwargs={"restrictions": [{"targeting_dimension": "AUDIENCE", "bid_only": True}]},
                     campaign_id="5", user_list_ids=["11"])
    assert preview["changes"]["set_audience_mode"] is None


def test_compatibility_matrix_is_enforced_at_preview():
    search = _draft(campaign_id="5", custom_audience_ids=["21"])
    assert "Custom segments cannot be attached to SEARCH" in search["details"][0]
    display = _draft(gaql_kwargs={"channel": "DISPLAY"}, campaign_id="5", combined_audience_ids=["31"])
    assert "Search-only" in display["details"][0]
    pmax = _draft(gaql_kwargs={"channel": "PERFORMANCE_MAX"}, campaign_id="5", user_list_ids=["11"])
    assert "draft_asset_group_signals" in pmax["error"]


def test_small_lists_cannot_target_search_but_can_observe():
    refused = _draft(campaign_id="5", user_list_ids=["12"], mode="targeting")
    assert "300 Search users" in refused["details"][0]
    observed = _draft(campaign_id="5", user_list_ids=["12"])
    assert any("takes effect from 1,000" in w for w in observed["warnings"])


def test_unknown_audiences_and_bad_ids_are_refused():
    assert "numeric" in _draft(campaign_id="5", user_list_ids=["abc"])["details"][0]
    assert "not an audience of this account" in _draft(campaign_id="5", user_list_ids=["99"])["details"][0]
    assert "exactly one" in _draft(campaign_id="5", ad_group_id="7", user_list_ids=["11"])["details"][0]


def test_audiences_cannot_sit_at_both_levels_but_exclusions_can():
    campaign_level = [{"campaign_criterion.criterion_id": 900, "campaign_criterion.type": "USER_LIST", "campaign_criterion.negative": False,
                       "campaign_criterion.user_list.user_list": "customers/1/userLists/13"}]
    refused = _draft(gaql_kwargs={"campaign_criteria": campaign_level}, ad_group_id="7", user_list_ids=["11"])
    assert "both campaign and ad group level" in refused["details"][0]
    excluded = _draft(gaql_kwargs={"campaign_criteria": campaign_level}, ad_group_id="7", user_list_ids=["11"], exclude=True)
    assert excluded["changes"]["exclude"] is True
    assert excluded["changes"]["set_audience_mode"] is None


def test_already_attached_audiences_are_skipped():
    attached = [{"campaign_criterion.criterion_id": 900, "campaign_criterion.type": "USER_LIST", "campaign_criterion.negative": False,
                 "campaign_criterion.user_list.user_list": "customers/1/userLists/11"}]
    preview = _draft(gaql_kwargs={"campaign_criteria": attached}, campaign_id="5", user_list_ids=["11", "13"])
    assert [a["audience_id"] for a in preview["changes"]["audiences"]] == ["13"]
    assert any("already attached" in w for w in preview["warnings"])
    assert "Nothing to attach" in _draft(gaql_kwargs={"campaign_criteria": attached}, campaign_id="5", user_list_ids=["11"])["error"]


def test_targeting_read_lists_audiences_with_remove_ids_and_mode():
    attached = [
        {"campaign_criterion.criterion_id": 900, "campaign_criterion.type": "USER_LIST", "campaign_criterion.negative": False,
         "campaign_criterion.user_list.user_list": "customers/1/userLists/11"},
        {"campaign_criterion.criterion_id": 901, "campaign_criterion.type": "USER_INTEREST", "campaign_criterion.negative": True,
         "campaign_criterion.user_interest.user_interest_category": "customers/1/userInterests/80101"},
    ]
    ctx, _ = _gaql(restrictions=[{"targeting_dimension": "AUDIENCE", "bid_only": False}], campaign_criteria=attached)
    with ctx:
        result = audiences.get_audience_targeting(AdLoopConfig(), customer_id="1", campaign_id="5")
    assert result["audience_mode"] == "targeting"
    assert result["audiences"] == [{"kind": "user_list", "audience_id": "11", "negative": False, "remove_id": "5~900", "name": "Buyers 30d"}]
    assert result["exclusions"][0]["name"] == "Running Shoes"
    assert any("Targeting mode" in i for i in result["insights"])


class _FakeClient:
    def __init__(self):
        base = GoogleAdsClient(credentials=None, developer_token="t", use_proto_plus=True, version=GOOGLE_ADS_API_VERSION)
        self.enums, self.get_type = base.enums, base.get_type
        self.sent = []
        self._service = SimpleNamespace(
            campaign_path=GoogleAdsServiceClient.campaign_path,
            ad_group_path=GoogleAdsServiceClient.ad_group_path,
            mutate=lambda customer_id, mutate_operations: self.sent.extend(mutate_operations),
        )

    def get_service(self, name):
        return self._service


def test_apply_sets_the_mode_keeping_other_dimensions_then_attaches_in_one_request():
    client = _FakeClient()
    result = audiences.apply_audience_criteria(client, "1", {
        "level": "campaign", "campaign_id": "5", "ad_group_id": None, "exclude": False,
        "audiences": [{"kind": "user_list", "audience_id": "11"}, {"kind": "segment", "audience_id": "80101"},
                      {"kind": "combined_audience", "audience_id": "31"}],
        "set_audience_mode": "observation", "audience_mode_before": "not set",
        "target_restrictions": [{"targeting_dimension": "TOPIC", "bid_only": True}],
    })
    kinds = [op._pb.WhichOneof("operation") for op in client.sent]
    assert kinds == ["campaign_operation"] + ["campaign_criterion_operation"] * 3
    update = client.sent[0].campaign_operation
    assert update.update.resource_name == "customers/1/campaigns/5"
    assert list(update.update_mask.paths) == ["targeting_setting.target_restrictions"]
    restrictions = update.update.targeting_setting.target_restrictions
    dimension = client.enums.TargetingDimensionEnum
    assert [(r.targeting_dimension, r.bid_only) for r in restrictions] == [(dimension.TOPIC, True), (dimension.AUDIENCE, True)]
    first = client.sent[1].campaign_criterion_operation.create
    assert first.campaign == "customers/1/campaigns/5"
    assert first.user_list.user_list == "customers/1/userLists/11"
    assert client.sent[2].campaign_criterion_operation.create.user_interest.user_interest_category == "customers/1/userInterests/80101"
    assert client.sent[3].campaign_criterion_operation.create.combined_audience.combined_audience == "customers/1/combinedAudiences/31"
    assert result["audiences_attached"] == 3 and result["audience_mode"] == "observation"


def test_apply_exclusions_at_ad_group_level_without_touching_the_mode():
    client = _FakeClient()
    audiences.apply_audience_criteria(client, "1", {
        "level": "ad_group", "campaign_id": "5", "ad_group_id": "7", "exclude": True,
        "audiences": [{"kind": "user_list", "audience_id": "11"}],
        "set_audience_mode": None, "audience_mode_before": "observation", "target_restrictions": [],
    })
    assert [op._pb.WhichOneof("operation") for op in client.sent] == ["ad_group_criterion_operation"]
    criterion = client.sent[0].ad_group_criterion_operation.create
    assert criterion.ad_group == "customers/1/adGroups/7"
    assert criterion.negative is True


def test_targeting_mode_is_written_as_bid_only_false():
    client = _FakeClient()
    audiences.apply_audience_criteria(client, "1", {
        "level": "ad_group", "campaign_id": "5", "ad_group_id": "7", "exclude": False,
        "audiences": [{"kind": "custom_audience", "audience_id": "21"}],
        "set_audience_mode": "targeting", "audience_mode_before": "observation", "target_restrictions": [],
    })
    update = client.sent[0].ad_group_operation.update
    assert update.resource_name == "customers/1/adGroups/7"
    assert update.targeting_setting.target_restrictions[0].bid_only is False
    assert client.sent[1].ad_group_criterion_operation.create.custom_audience.custom_audience == "customers/1/customAudiences/21"
