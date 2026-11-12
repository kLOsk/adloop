"""geo_exclude_ids and ad_schedule on draft_campaign and update_campaign.

On create they go in the campaign's atomic mutate. On update they replace the
whole list: None leaves it, [] clears it, the preview shows the current values
and apply refuses if those moved. Plans run through
``confirm_and_apply(dry_run=True)`` with real protos and the real validate-only
wrapper; only the services are fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.approval import confirmation_message

CAMPAIGN = "1001"
MONDAY = {"day_of_week": "MONDAY", "start_hour": 8, "end_hour": 17}
TUESDAY = {"day_of_week": "TUESDAY", "start_hour": 9, "start_minute": 30, "end_hour": 12}


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


class _Service:
    def __init__(self, client, name):
        self._client = client
        self._name = name

    def search(self, *, customer_id, query):
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return iter(self._client.window_rows)
        if "campaign_criterion.negative = TRUE" in query:
            return iter(self._client.exclude_rows)
        if "campaign_criterion.negative = FALSE" in query:
            return iter(self._client.target_rows)
        if "FROM campaign WHERE" in query:
            return iter(self._client.campaign_rows)
        return iter([])

    def __getattr__(self, attr):
        if attr.endswith("_path"):
            noun = attr.removesuffix("_path")
            plural = {"campaign": "campaigns", "ad_group": "adGroups", "campaign_budget": "campaignBudgets"}[noun]
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
        self.window_rows = []
        self.exclude_rows = []
        self.target_rows = []
        self.campaign_rows = []

    def get_service(self, name, *_args, **_kwargs):
        return _Service(self, name)

    def operations(self):
        assert len(self.sent) == 1 and self.sent[0].validate_only is True
        return list(self.sent[0].mutate_operations)

    def removed(self):
        return [op.campaign_criterion_operation.remove for op in self.operations()
                if op.campaign_criterion_operation.remove]

    def created(self):
        return [op.campaign_criterion_operation.create for op in self.operations()
                if not op.campaign_criterion_operation.remove and op.campaign_criterion_operation.create.campaign]

    def order(self):
        """The request's operations in the order sent, as short tuples."""
        summary = []
        for op in self.operations():
            criterion_op = op.campaign_criterion_operation
            created = criterion_op.create
            if op.campaign_operation.update.resource_name:
                summary.append(("campaign", *op.campaign_operation.update_mask.paths))
            elif criterion_op.remove:
                summary.append(("remove", criterion_op.remove.rsplit("~", 1)[-1]))
            elif created.location.geo_target_constant:
                geo_id = created.location.geo_target_constant.rsplit("/", 1)[-1]
                summary.append(("exclude" if created.negative else "target", geo_id))
            elif created.language.language_constant:
                summary.append(("language", created.language.language_constant.rsplit("/", 1)[-1]))
            else:
                window = created.ad_schedule
                summary.append(("window", window.day_of_week.name, window.start_hour, window.end_hour))
        return summary


@pytest.fixture
def client(monkeypatch):
    fake = _Client()
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _cfg: fake)
    # conftest stubs this out; these tests are about the real path.
    monkeypatch.setattr(
        write, "_validate_with_google",
        lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
    )
    return fake


def _window_row(criterion_id, **window):
    return {
        "campaign_criterion.criterion_id": criterion_id,
        "campaign_criterion.ad_schedule.day_of_week": window["day_of_week"],
        "campaign_criterion.ad_schedule.start_hour": window["start_hour"],
        "campaign_criterion.ad_schedule.start_minute": window.get("start_minute", "ZERO"),
        "campaign_criterion.ad_schedule.end_hour": window["end_hour"],
        "campaign_criterion.ad_schedule.end_minute": window.get("end_minute", "ZERO"),
    }


def _account(monkeypatch, *, windows=(), excludes=(), status="ENABLED", found=True):
    """GAQL answers for the time zone, the campaign, its windows and exclusions."""
    def execute_query(_config, _cid, query):
        if "FROM customer" in query:
            return [{"customer.currency_code": "USD", "customer.time_zone": "America/Chicago"}]
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return list(windows)
        if "campaign_criterion.negative = TRUE" in query:
            return [{"campaign_criterion.location.geo_target_constant": f"geoTargetConstants/{g}"} for g in excludes]
        if "FROM campaign WHERE campaign.id" in query:
            if not found:
                return []
            # Also answers update_campaign's keyword match type read (#117).
            return [{"campaign.name": "Brand", "campaign.status": status, "customer.time_zone": "America/Chicago",
                     "campaign.keyword_match_type": "UNSPECIFIED",
                     "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS"}]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("adloop.ads.gaql.execute_query", execute_query)


def _draft(config, **kwargs):
    return write.draft_campaign(
        config, customer_id="1234567890", campaign_name="Search", daily_budget=20,
        bidding_strategy="MAXIMIZE_CONVERSIONS", geo_target_ids=["21137"], language_ids=["1000"], **kwargs,
    )


def _update(config, **kwargs):
    return write.update_campaign(config, customer_id="1234567890", campaign_id=CAMPAIGN, **kwargs)


def _dry_run(config, preview):
    assert "plan_id" in preview, preview
    return write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)


def _apply_check(config, preview):
    result = _dry_run(config, preview)
    assert result["status"] == "DRY_RUN_SUCCESS", result
    return result


class TestDraftCampaign:
    def test_both_go_in_the_one_atomic_mutate(self, config, client, monkeypatch):
        _account(monkeypatch)
        preview = _draft(config, geo_exclude_ids=["1014962"], ad_schedule=[MONDAY])
        assert preview["changes"]["geo_exclude_ids"] == ["1014962"]
        _apply_check(config, preview)

        criteria = client.created()
        negative = [c for c in criteria if c.negative]
        assert [c.location.geo_target_constant for c in negative] == ["geoTargetConstants/1014962"]
        assert negative[0].campaign == "customers/1234567890/campaigns/-2"
        windows = [c for c in criteria if c.ad_schedule.start_hour]
        assert windows[0].campaign == "customers/1234567890/campaigns/-2"
        assert (windows[0].ad_schedule.start_hour, windows[0].ad_schedule.end_hour) == (8, 17)
        assert windows[0].ad_schedule.day_of_week == client.enums.DayOfWeekEnum.MONDAY

    def test_a_schedule_only_warns_because_the_campaign_is_created_paused(self, config, client, monkeypatch):
        _account(monkeypatch)
        preview = _draft(config, ad_schedule=[MONDAY])
        assert preview["requires_double_confirm"] is False
        assert preview["changes"]["account_time_zone"] == "America/Chicago"
        assert any("account time zone (America/Chicago)" in w for w in preview["warnings"])
        assert any("created PAUSED" in w and "Once it is enabled it serves only inside these windows" in w
                   for w in preview["warnings"])
        assert not any("stops serving" in w for w in preview["warnings"])
        dialog = confirmation_message(preview_store.get_plan(preview["plan_id"]))
        assert "cannot be undone" not in dialog

    def test_exclusions_alone_need_no_double_confirmation_or_read(self, config, client):
        # no_live_gaql from conftest fails this test if the draft queries anything.
        preview = _draft(config, geo_exclude_ids=["1014962"])
        assert preview["requires_double_confirm"] is False

    def test_without_them_the_plan_is_unchanged(self, config, client):
        preview = _draft(config)
        assert "geo_exclude_ids" not in preview["changes"]
        assert "ad_schedule" not in preview["changes"]
        assert preview["requires_double_confirm"] is False

    def test_schedule_rules_match_add_ad_schedule(self, config):
        overlapping = [MONDAY, {"day_of_week": "MONDAY", "start_hour": 16, "end_hour": 20}]
        result = _draft(config, ad_schedule=overlapping)
        assert any("overlaps" in d for d in result["details"])
        result = _draft(config, ad_schedule=[{"day_of_week": "MONDAY", "start_hour": 8.5, "end_hour": 17}])
        assert any("whole numbers" in d for d in result["details"])
        seven = [{"day_of_week": "MONDAY", "start_hour": h, "end_hour": h + 1} for h in range(7)]
        result = _draft(config, ad_schedule=seven)
        assert any("at most 6" in d for d in result["details"])

    def test_exclusions_are_checked(self, config):
        result = _draft(config, geo_exclude_ids=["21137"])
        assert any("overlap geo_target_ids" in d for d in result["details"])
        result = _draft(config, geo_exclude_ids=["Los Angeles"])
        assert any("must be numeric" in d for d in result["details"])

    def test_results_are_sorted_by_kind(self, monkeypatch):
        window = {"day_of_week": "MONDAY", "start_hour": 8, "start_minute": 0, "end_hour": 17, "end_minute": 0}

        class _Mutating(_Service):
            def mutate(self, *, customer_id, mutate_operations):
                return SimpleNamespace(mutate_operation_responses=[f"r{i}" for i in range(len(mutate_operations))])

        fake = _Client()
        fake.get_service = lambda name, *_a, **_k: _Mutating(fake, name)
        monkeypatch.setattr(write, "_extract_resource_name", lambda response: response)
        results = write._apply_create_campaign(fake, "1", {
            "campaign_name": "C", "daily_budget": 1, "bidding_strategy": "MAXIMIZE_CONVERSIONS",
            "channel_type": "SEARCH", "ad_group_name": "A", "geo_target_ids": ["1"], "language_ids": ["2"],
            "geo_exclude_ids": ["3"], "ad_schedule": [window],
        })
        assert results == {
            "campaign_budget": "r0", "campaign": "r1", "ad_group": "r2", "geo_targets": ["r3"],
            "language_targets": ["r4"], "geo_excludes": ["r5"], "ad_schedule": ["r6"],
        }


class TestUpdateGeoExcludes:
    def test_replaces_and_only_touches_what_differs(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111", "222"])
        client.exclude_rows = [_location_row("111"), _location_row("222")]
        preview = _update(config, geo_exclude_ids=["222", "333"])
        assert preview["changes"]["current_geo_exclude_ids"] == ["111", "222"]
        assert preview["changes"]["geo_exclude_ids"] == ["222", "333"]
        assert preview["requires_double_confirm"] is True
        assert any("['111']" in w and "serve in those locations again" in w for w in preview["warnings"])

        _apply_check(config, preview)
        assert client.removed() == [f"customers/1/campaignCriteria/{CAMPAIGN}~111"]
        added = client.created()
        assert [c.location.geo_target_constant for c in added] == ["geoTargetConstants/333"]
        assert added[0].negative is True

    def test_empty_list_clears_them(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111"])
        client.exclude_rows = [_location_row("111")]
        preview = _update(config, geo_exclude_ids=[])
        assert preview["requires_double_confirm"] is True
        _apply_check(config, preview)
        assert client.removed() == [f"customers/1/campaignCriteria/{CAMPAIGN}~111"]
        assert client.created() == []

    def test_adding_only_needs_no_double_confirmation(self, config, client, monkeypatch):
        _account(monkeypatch)
        preview = _update(config, geo_exclude_ids=["333"])
        assert preview["requires_double_confirm"] is False
        assert "warnings" not in preview

    def test_blank_entries_are_refused_instead_of_clearing(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111"])
        result = _update(config, geo_exclude_ids=[" "])
        assert any("must be numeric" in d for d in result["details"])

    def test_overlap_with_new_targets_is_refused(self, config):
        result = _update(config, geo_target_ids=["2840"], geo_exclude_ids=["2840"])
        assert any("overlap geo_target_ids" in d for d in result["details"])

    def test_same_list_is_nothing_to_change(self, config, monkeypatch):
        _account(monkeypatch, excludes=["111"])
        result = _update(config, geo_exclude_ids=["111"])
        assert "nothing to change" in result["error"]

    def test_apply_refuses_when_they_changed_after_the_preview(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111"])
        client.exclude_rows = [_location_row("111"), _location_row("999")]
        result = _dry_run(config, _update(config, geo_exclude_ids=["333"]))
        assert result["status"] == "DRY_RUN_FAILED"
        assert "changed after this preview" in str(result)
        assert client.sent == []

    def test_replacing_positive_geos_no_longer_promises_to_preserve_them(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111"])
        client.exclude_rows = [_location_row("111")]
        preview = _update(config, geo_target_ids=["2840"], geo_exclude_ids=[])
        assert "preserved_negative_geo_target_ids" not in preview
        assert not any("PRESERVED" in w for w in preview["warnings"])

    def test_omitted_leaves_them_preserved(self, config, monkeypatch):
        _account(monkeypatch, excludes=["111"])
        preview = _update(config, geo_target_ids=["2840"])
        assert preview["preserved_negative_geo_target_ids"] == ["111"]
        assert "geo_exclude_ids" not in preview["changes"]


def _location_row(geo_id):
    return SimpleNamespace(campaign_criterion=SimpleNamespace(
        resource_name=f"customers/1/campaignCriteria/{CAMPAIGN}~{geo_id}",
        location=SimpleNamespace(geo_target_constant=f"geoTargetConstants/{geo_id}"),
    ))


def _criterion_row(criterion_id):
    return SimpleNamespace(campaign_criterion=SimpleNamespace(
        resource_name=f"customers/1/campaignCriteria/{CAMPAIGN}~{criterion_id}",
        criterion_id=criterion_id,
    ))


class TestUpdateAdSchedule:
    def test_first_schedule_needs_a_double_confirmation_like_add_ad_schedule(self, config, client, monkeypatch):
        _account(monkeypatch)
        preview = _update(config, ad_schedule=[MONDAY])
        assert preview["requires_double_confirm"] is True
        assert any("'Brand' has no ad schedule" in w and "stops serving" in w for w in preview["warnings"])
        assert any("account time zone (America/Chicago)" in w for w in preview["warnings"])
        assert preview["changes"]["current_ad_schedule"] == []
        assert "cannot be undone" in confirmation_message(preview_store.get_plan(preview["plan_id"]))
        _apply_check(config, preview)
        assert client.removed() == []
        assert client.created()[0].ad_schedule.start_hour == 8

    def test_clearing_is_flagged_and_needs_a_double_confirmation(self, config, client, monkeypatch):
        _account(monkeypatch, windows=[_window_row(5, **MONDAY)])
        client.window_rows = [_criterion_row(5)]
        preview = _update(config, ad_schedule=[])
        assert preview["requires_double_confirm"] is True
        assert any("serves at all hours" in w for w in preview["warnings"])
        assert preview["changes"]["current_ad_schedule"][0]["day_of_week"] == "MONDAY"
        _apply_check(config, preview)
        assert client.removed() == [f"customers/1/campaignCriteria/{CAMPAIGN}~5"]
        assert client.created() == []

    def test_replacing_keeps_identical_windows_and_shows_the_current_ones(self, config, client, monkeypatch):
        _account(monkeypatch, windows=[_window_row(5, **MONDAY), _window_row(6, **{**TUESDAY, "start_minute": "THIRTY"})])
        client.window_rows = [_criterion_row(5), _criterion_row(6)]
        new = {"day_of_week": "FRIDAY", "start_hour": 10, "end_hour": 14}
        preview = _update(config, ad_schedule=[MONDAY, new])
        assert preview["requires_double_confirm"] is False
        assert any("Replaces the 2 current ad schedule windows" in w for w in preview["warnings"])
        assert len(preview["changes"]["current_ad_schedule"]) == 2
        _apply_check(config, preview)
        assert client.removed() == [f"customers/1/campaignCriteria/{CAMPAIGN}~6"]
        assert [c.ad_schedule.day_of_week for c in client.created()] == [client.enums.DayOfWeekEnum.FRIDAY]

    def test_a_replacement_uses_the_same_window_rules(self, config):
        result = _update(config, ad_schedule=[{"day_of_week": "SOMEDAY", "start_hour": 8, "end_hour": 17}])
        assert any("day_of_week must be one of" in d for d in result["details"])

    def test_same_windows_are_nothing_to_change(self, config, monkeypatch):
        _account(monkeypatch, windows=[_window_row(5, **MONDAY)])
        assert "nothing to change" in _update(config, ad_schedule=[MONDAY])["error"]

    def test_clearing_nothing_is_nothing_to_change(self, config, monkeypatch):
        _account(monkeypatch)
        assert "nothing to change" in _update(config, ad_schedule=[])["error"]

    def test_a_no_op_next_to_another_change_says_so(self, config, monkeypatch):
        _account(monkeypatch)
        preview = _update(config, ad_schedule=[], geo_exclude_ids=["333"])
        assert "ad_schedule" not in preview["changes"]
        assert any("already has these windows" in w for w in preview["warnings"])

    def test_apply_refuses_when_the_windows_changed_after_the_preview(self, config, client, monkeypatch):
        _account(monkeypatch, windows=[_window_row(5, **MONDAY)])
        client.window_rows = []  # someone removed the window: these would now be a first schedule
        result = _dry_run(config, _update(config, ad_schedule=[TUESDAY]))
        assert result["status"] == "DRY_RUN_FAILED"
        assert "changed after this preview" in str(result)
        assert client.sent == []

    def test_paused_campaign_says_when_it_applies(self, config, monkeypatch):
        _account(monkeypatch, status="PAUSED")
        preview = _update(config, ad_schedule=[MONDAY])
        assert any("The campaign is PAUSED" in w for w in preview["warnings"])

    @pytest.mark.parametrize("kwargs", [{"ad_schedule": [MONDAY]}, {"geo_exclude_ids": ["333"]}])
    def test_unknown_and_removed_campaigns_are_refused(self, config, monkeypatch, kwargs):
        _account(monkeypatch, found=False)
        assert "was not found" in _update(config, **kwargs)["error"]
        _account(monkeypatch, status="REMOVED")
        assert "is removed" in _update(config, **kwargs)["error"]


class TestOneRequestOrder:
    """update_campaign sends one mutate, in this order: the campaign's own
    fields, exclusion removes, positive geo removes, positive geo creates,
    languages, exclusion creates, then schedule removes before schedule creates.

    A location criterion's id is its geo target constant, so a location moving
    between the targets and the exclusions is the same criterion removed and
    created again: the remove has to come first in the request.
    """

    def test_a_location_moving_from_excluded_to_targeted_is_removed_first(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111", "222"])
        client.exclude_rows = [_location_row("111"), _location_row("222")]
        client.target_rows = [_location_row("2840")]
        preview = _update(config, geo_target_ids=["2840", "111"], geo_exclude_ids=["222"])
        assert preview["requires_double_confirm"] is True  # 111 is no longer excluded
        _apply_check(config, preview)
        assert client.order() == [
            ("remove", "111"),  # the exclusion
            ("remove", "2840"),  # positive targets are replaced as a whole
            ("target", "2840"),
            ("target", "111"),
        ]

    def test_a_location_moving_from_targeted_to_excluded_is_removed_first(self, config, client, monkeypatch):
        _account(monkeypatch)
        client.target_rows = [_location_row("111"), _location_row("2840")]
        _apply_check(config, _update(config, geo_target_ids=["2840"], geo_exclude_ids=["111"]))
        assert client.order() == [
            ("remove", "111"),  # the positive target
            ("remove", "2840"),
            ("target", "2840"),
            ("exclude", "111"),
        ]

    def test_a_shifted_window_is_removed_before_its_replacement_is_added(self, config, client, monkeypatch):
        _account(monkeypatch, windows=[_window_row(5, **MONDAY), _window_row(6, **{**TUESDAY, "start_minute": "THIRTY"})])
        client.window_rows = [_criterion_row(5), _criterion_row(6)]
        later = {"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 18}  # overlaps the 8-17 window
        _apply_check(config, _update(config, ad_schedule=[later, TUESDAY]))
        assert client.order() == [("remove", "5"), ("window", "MONDAY", 9, 18)]

    def test_a_full_day_of_windows_is_swapped_removals_first(self, config, client, monkeypatch):
        # Six windows is Google's limit for a day: adding before removing would exceed it.
        old = [{"day_of_week": "MONDAY", "start_hour": 2 * i, "end_hour": 2 * i + 1} for i in range(6)]
        new = [{"day_of_week": "MONDAY", "start_hour": 2 * i + 1, "end_hour": 2 * i + 2} for i in range(6)]
        _account(monkeypatch, windows=[_window_row(i + 1, **window) for i, window in enumerate(old)])
        client.window_rows = [_criterion_row(i + 1) for i in range(6)]
        _apply_check(config, _update(config, ad_schedule=new))
        assert [step[0] for step in client.order()] == ["remove"] * 6 + ["window"] * 6

    def test_broad_match_and_both_replacements_go_in_one_request(self, config, client, monkeypatch):
        _account(monkeypatch, excludes=["111"], windows=[_window_row(5, **MONDAY)])
        client.exclude_rows = [_location_row("111")]
        client.target_rows = [_location_row("2840")]
        client.window_rows = [_criterion_row(5)]
        client.campaign_rows = [SimpleNamespace(campaign=SimpleNamespace(
            bidding_strategy_type="MAXIMIZE_CONVERSIONS", keyword_match_type="UNSPECIFIED"))]
        preview = _update(config, keyword_match_type="BROAD", geo_target_ids=["2840", "111"],
                          geo_exclude_ids=["222"], ad_schedule=[TUESDAY])
        assert preview["changes"]["keyword_match_type"] == "BROAD"
        assert preview["changes"]["geo_exclude_ids"] == ["222"]
        assert preview["changes"]["ad_schedule"][0]["day_of_week"] == "TUESDAY"
        assert preview["requires_double_confirm"] is True
        _apply_check(config, preview)
        assert client.order() == [
            ("campaign", "keyword_match_type"),
            ("remove", "111"),
            ("remove", "2840"),
            ("target", "2840"),
            ("target", "111"),
            ("exclude", "222"),
            ("remove", "5"),
            ("window", "TUESDAY", 9, 12),
        ]


@pytest.mark.asyncio
async def test_the_mcp_tools_take_both_parameters():
    from adloop.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("draft_campaign", "update_campaign"):
        properties = tools[name].parameters["properties"]
        for param in ("geo_exclude_ids", "ad_schedule"):
            assert properties[param].get("description"), (name, param)
        assert not {"geo_exclude_ids", "ad_schedule"} & set(tools[name].parameters.get("required", []))
    assert "REPLACES" in tools["update_campaign"].parameters["properties"]["ad_schedule"]["description"]
