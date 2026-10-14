"""Call assets and campaign ad schedules (adloop.ads.assets)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import assets, write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.ads.validate_only import PLACEHOLDER, ValidateOnlyClient
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import ChangePlan, store_plan

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_plan_store():
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())
    yield
    preview_store.set_plan_store(preview_store.InMemoryPlanStore())


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(log_file=str(tmp_path / "audit.log")),
    )


class FakeGaql:
    """Answers execute_query by the FROM clause; records every query."""

    def __init__(self, **rows_by_resource):
        self.rows = rows_by_resource
        self.queries: list[str] = []

    def __call__(self, _config, _customer_id, query):
        self.queries.append(query)
        resource = query.split("FROM", 1)[1].split()[0]
        return list(self.rows.get(resource, []))


@pytest.fixture
def gaql(monkeypatch):
    def install(**rows):
        fake = FakeGaql(**rows)
        monkeypatch.setattr("adloop.ads.gaql.execute_query", fake)
        return fake

    return install


class FakeService:
    """Path helpers plus mutate methods that record their calls."""

    def __init__(self, client, name):
        self._client = client
        self._name = name

    def __getattr__(self, attr):
        if attr.endswith("_path"):
            kind = attr.removesuffix("_path")
            return lambda cid, entity_id: f"customers/{cid}/{kind}/{entity_id}"
        if attr.startswith("mutate"):
            def mutate(request=None, **kwargs):
                self._client.calls.append((self._name, attr, request, kwargs))
                ops = kwargs.get("operations") or kwargs.get("mutate_operations") or []
                names = [f"customers/1/{self._name}/{i}" for i in range(len(ops))]
                return SimpleNamespace(
                    results=[SimpleNamespace(resource_name=n) for n in names],
                    mutate_operation_responses=[_Response(n) for n in names],
                    partial_failure_error=None,
                )
            return mutate
        raise AttributeError(attr)


class _Response:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        if attr.endswith("_result"):
            return SimpleNamespace(resource_name=f"{self._name}/{attr}")
        raise AttributeError(attr)


class FakeAdsClient:
    """Real proto types and enums, fake services."""

    def __init__(self):
        base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.calls: list[tuple] = []

    def get_service(self, name):
        return FakeService(self, name)


def _mutate_operations(client):
    [(_service, _method, request, kwargs)] = client.calls
    return list(request.mutate_operations if request is not None else kwargs["mutate_operations"])


MONDAY_9_TO_5 = {"day_of_week": "monday", "start_hour": 9, "end_hour": 17}


# ---------------------------------------------------------------------------
# E.164 normalisation
# ---------------------------------------------------------------------------


class TestNormalizePhoneE164:
    @pytest.mark.parametrize(
        "phone,country,expected",
        [
            ("(916) 339-3676", "US", "+19163393676"),
            ("916.339.3676", "us", "+19163393676"),
            ("1 916 339 3676", "US", "+19163393676"),
            ("+1 916 339 3676", "US", "+19163393676"),
            ("416-555-0142", "CA", "+14165550142"),
            # Exactly one domestic trunk zero is dropped.
            ("020 7946 0958", "GB", "+442079460958"),
            ("030 1234567", "DE", "+49301234567"),
            # Italy keeps its leading zero in E.164.
            ("06 1234 5678", "IT", "+390612345678"),
            # "00" is the international access prefix, not a trunk zero.
            ("0044 20 7946 0958", "GB", "+442079460958"),
            ("00 1 916 339 3676", "US", "+19163393676"),
            # E.164 input for a country without a national-number rule.
            ("+48 22 123 45 67", "PL", "+48221234567"),
        ],
    )
    def test_normalizes_to_e164(self, phone, country, expected):
        assert assets._normalize_phone_e164(phone, country) == (expected, None)

    @pytest.mark.parametrize("country", ["", "  ", None])
    def test_country_code_is_required(self, country):
        normalized, err = assets._normalize_phone_e164("(916) 339-3676", country)
        assert normalized == ""
        assert "country_code is required" in err

    def test_country_code_must_be_two_letters(self):
        assert "2-letter" in assets._normalize_phone_e164("9163393676", "USA")[1]

    def test_national_number_for_unmapped_country_needs_e164(self):
        normalized, err = assets._normalize_phone_e164("22 123 45 67", "PL")
        assert normalized == ""
        assert "E.164" in err

    def test_number_must_match_the_country(self):
        _, err = assets._normalize_phone_e164("+44 20 7946 0958", "US")
        assert "does not match country_code US" in err
        _, err = assets._normalize_phone_e164("0044 20 7946 0958", "DE")
        assert "does not match country_code DE" in err

    @pytest.mark.parametrize("phone", ["", "()- ", "+"])
    def test_empty_number(self, phone):
        assert "empty" in assets._normalize_phone_e164(phone, "US")[1]

    def test_digit_count_is_checked(self):
        assert "7 to 15" in assets._normalize_phone_e164("+44 1234", "GB")[1]
        assert "7 to 15" in assets._normalize_phone_e164("+1 916 339 3676 12345", "US")[1]

    def test_north_american_numbers_need_the_area_code(self):
        assert "10 digits after +1" in assets._normalize_phone_e164("555-0142", "US")[1]
        assert "10 digits after +1" in assets._normalize_phone_e164("+1 916 339 36761", "CA")[1]

    def test_plus_only_allowed_first(self):
        assert "'+'" in assets._normalize_phone_e164("916+3393676", "US")[1]


# ---------------------------------------------------------------------------
# Ad schedule validation
# ---------------------------------------------------------------------------


class TestValidateAdSchedule:
    def test_normalizes_day_and_defaults_minutes(self):
        validated, errors = assets._validate_ad_schedule([MONDAY_9_TO_5])
        assert errors == []
        assert validated == [{
            "day_of_week": "MONDAY", "start_hour": 9, "start_minute": 0,
            "end_hour": 17, "end_minute": 0,
        }]

    def test_end_of_day_is_24(self):
        _, errors = assets._validate_ad_schedule(
            [{"day_of_week": "SUNDAY", "start_hour": 0, "end_hour": 24}]
        )
        assert errors == []

    @pytest.mark.parametrize(
        "entry,message",
        [
            ({"day_of_week": "FUNDAY", "start_hour": 9, "end_hour": 17}, "day_of_week"),
            ({"day_of_week": "MONDAY", "start_hour": 24, "end_hour": 24}, "start_hour"),
            ({"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 25}, "end_hour"),
            ({"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 17, "start_minute": 10}, "start_minute"),
            ({"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 17, "end_minute": 20}, "end_minute"),
            ({"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 24, "end_minute": 15}, "end_hour 24"),
            ({"day_of_week": "MONDAY", "start_hour": 17, "end_hour": 9}, "must be after"),
            ({"day_of_week": "MONDAY", "start_hour": "nine", "end_hour": 17}, "integers"),
            ("MONDAY 9-17", "object"),
        ],
    )
    def test_rejects(self, entry, message):
        validated, errors = assets._validate_ad_schedule([entry])
        assert validated == []
        assert any(message in e for e in errors), errors

    def test_overlapping_windows_on_one_day(self):
        _, errors = assets._validate_ad_schedule([
            MONDAY_9_TO_5,
            {"day_of_week": "MONDAY", "start_hour": 16, "end_hour": 20},
        ])
        assert any("overlaps" in e for e in errors)

    def test_adjacent_windows_do_not_overlap(self):
        _, errors = assets._validate_ad_schedule([
            MONDAY_9_TO_5,
            {"day_of_week": "MONDAY", "start_hour": 17, "end_hour": 20},
        ])
        assert errors == []

    def test_at_most_six_windows_per_day(self):
        entries = [
            {"day_of_week": "FRIDAY", "start_hour": h, "end_hour": h + 1}
            for h in range(0, 14, 2)
        ]
        _, errors = assets._validate_ad_schedule(entries)
        assert any("at most 6" in e for e in errors)


# ---------------------------------------------------------------------------
# draft_call_asset
# ---------------------------------------------------------------------------


class TestDraftCallAsset:
    def test_country_code_is_required(self, config):
        result = assets.draft_call_asset(
            config, customer_id="1234567890", phone_number="(916) 339-3676", campaign_id="42"
        )
        assert result["error"] == "Validation failed"
        assert any("country_code is required" in d for d in result["details"])

    def test_phone_number_is_required(self, config):
        result = assets.draft_call_asset(config, country_code="US", campaign_id="42")
        assert "phone_number is required" in result["details"]

    def test_campaign_scope(self, config):
        result = assets.draft_call_asset(
            config, customer_id="1234567890", phone_number="(916) 339-3676",
            country_code="us", campaign_id="42",
        )
        assert result["operation"] == "create_call_asset"
        assert result["entity_type"] == "campaign_asset"
        assert result["entity_id"] == "42"
        assert result["requires_double_confirm"] is False
        changes = result["changes"]
        assert changes["scope"] == "campaign"
        assert changes["phone_number"] == "+19163393676"
        assert changes["country_code"] == "US"

    def test_ad_group_scope(self, config):
        result = assets.draft_call_asset(
            config, phone_number="+19163393676", country_code="US", ad_group_id="777",
        )
        assert result["entity_type"] == "ad_group_asset"
        assert result["changes"]["scope"] == "ad_group"

    def test_account_scope_is_never_inferred(self, config):
        result = assets.draft_call_asset(config, phone_number="+19163393676", country_code="US")
        assert result["error"] == "Validation failed"
        assert any("scope='account'" in d for d in result["details"])

    def test_account_scope_by_explicit_opt_in(self, config):
        result = assets.draft_call_asset(
            config, customer_id="1234567890", phone_number="+19163393676",
            country_code="US", scope="account",
        )
        assert result["entity_type"] == "customer_asset"
        assert result["changes"]["scope"] == "account"
        assert any("every eligible campaign" in w for w in result["warnings"])

    @pytest.mark.parametrize(
        "kwargs,message",
        [
            ({"scope": "account", "campaign_id": "42"}, "omit campaign_id"),
            ({"campaign_id": "42", "ad_group_id": "7"}, "not both"),
            ({"scope": "campaign", "ad_group_id": "7"}, "needs campaign_id"),
            ({"scope": "ad_group", "campaign_id": "42"}, "needs ad_group_id"),
            ({"scope": "customer", "campaign_id": "42"}, "scope must be one of"),
            ({"campaign_id": "42 OR 1=1"}, "campaign_id must be a numeric ID"),
            ({"ad_group_id": "abc"}, "ad_group_id must be a numeric ID"),
            ({"campaign_id": "42", "call_conversion_action_id": "x1"}, "call_conversion_action_id"),
        ],
    )
    def test_invalid_scope_and_ids(self, config, kwargs, message):
        result = assets.draft_call_asset(
            config, phone_number="+19163393676", country_code="US", **kwargs
        )
        assert result["error"] == "Validation failed"
        assert any(message in d for d in result["details"]), result["details"]

    def test_schedule_is_validated(self, config):
        result = assets.draft_call_asset(
            config, phone_number="+19163393676", country_code="US", campaign_id="42",
            ad_schedule=[{"day_of_week": "MONDAY", "start_hour": 9, "end_hour": 17, "end_minute": 5}],
        )
        assert any("end_minute" in d for d in result["details"])

    def test_schedule_preview_names_the_account_time_zone(self, config, gaql):
        gaql(customer=[{"customer.time_zone": "America/Chicago"}])
        result = assets.draft_call_asset(
            config, phone_number="+19163393676", country_code="US", campaign_id="42",
            ad_schedule=[MONDAY_9_TO_5],
        )
        assert result["changes"]["ad_schedule"][0]["day_of_week"] == "MONDAY"
        assert any("America/Chicago" in w for w in result["warnings"])

    def test_blocked_operation(self, config):
        config.safety.blocked_operations = ["create_call_asset"]
        result = assets.draft_call_asset(
            config, phone_number="+19163393676", country_code="US", campaign_id="42"
        )
        assert "blocked" in result["error"]


# ---------------------------------------------------------------------------
# update_call_asset
# ---------------------------------------------------------------------------


def _call_asset_row(**overrides):
    row = {
        "asset.id": 555,
        "asset.type": "CALL",
        "asset.call_asset.phone_number": "(916) 885-1005",
        "asset.call_asset.country_code": "US",
        "asset.call_asset.call_conversion_action": "",
        "asset.call_asset.call_conversion_reporting_state": "USE_ACCOUNT_LEVEL_CALL_CONVERSION_ACTION",
    }
    row.update(overrides)
    return row


class TestUpdateCallAsset:
    def test_phone_number_requires_country_code(self, config, gaql):
        fake = gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(config, asset_id="555", phone_number="916-713-5818")
        assert result["error"] == "Validation failed"
        assert any("country_code is required" in d for d in result["details"])
        assert fake.queries == []

    def test_country_code_alone_is_refused(self, config, gaql):
        gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(config, asset_id="555", country_code="CA")
        assert any("together with phone_number" in d for d in result["details"])

    def test_non_numeric_asset_id_never_reaches_gaql(self, config, gaql):
        fake = gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(
            config, asset_id="555 OR asset.id > 0", phone_number="9167135818", country_code="US"
        )
        assert any("asset_id must be a numeric ID" in d for d in result["details"])
        assert fake.queries == []

    def test_unknown_asset(self, config, gaql):
        gaql(asset=[])
        result = assets.update_call_asset(
            config, asset_id="555", phone_number="9167135818", country_code="US"
        )
        assert "No asset with ID 555" in result["error"]

    def test_non_call_asset_is_refused(self, config, gaql):
        gaql(asset=[_call_asset_row(**{"asset.type": "SITELINK"})])
        result = assets.update_call_asset(
            config, asset_id="555", phone_number="9167135818", country_code="US"
        )
        assert "not a CALL asset" in result["error"]

    def test_phone_update_preview(self, config, gaql):
        fake = gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(
            config, customer_id="1234567890", asset_id="555",
            phone_number="(916) 713-5818", country_code="US",
        )
        assert "WHERE asset.id = 555" in fake.queries[0]
        assert result["operation"] == "update_call_asset"
        # In place: no links change, so no double confirmation.
        assert result["requires_double_confirm"] is False
        changes = result["changes"]
        assert changes["phone_number"] == "+19167135818"
        assert changes["country_code"] == "US"
        assert changes["current"]["phone_number"] == "(916) 885-1005"
        assert "ad_schedule" not in changes
        assert any("wherever this call asset is linked" in w for w in result["warnings"])

    def test_conversion_action_implies_resource_level(self, config, gaql):
        gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(config, asset_id="555", call_conversion_action_id="179")
        assert result["changes"]["call_conversion_action_id"] == "179"
        assert result["changes"]["call_conversion_reporting_state"] == (
            "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION"
        )

    def test_conversion_action_with_conflicting_state(self, config, gaql):
        gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(
            config, asset_id="555", call_conversion_action_id="179",
            call_conversion_reporting_state="DISABLED",
        )
        assert result["error"] == "Validation failed"

    def test_invalid_reporting_state(self, config, gaql):
        gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(
            config, asset_id="555", call_conversion_reporting_state="SOMETIMES"
        )
        assert any("invalid" in d for d in result["details"])

    def test_resource_level_without_any_action(self, config, gaql):
        gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(
            config, asset_id="555",
            call_conversion_reporting_state="use_resource_level_call_conversion_action",
        )
        assert any("needs a conversion action" in d for d in result["details"])

    def test_schedule_replace_and_clear(self, config, gaql):
        gaql(asset=[_call_asset_row()], customer=[{"customer.time_zone": "America/Los_Angeles"}])
        replaced = assets.update_call_asset(config, asset_id="555", ad_schedule=[MONDAY_9_TO_5])
        assert len(replaced["changes"]["ad_schedule"]) == 1
        assert any("America/Los_Angeles" in w for w in replaced["warnings"])

        cleared = assets.update_call_asset(config, asset_id="555", clear_ad_schedule=True)
        assert cleared["changes"]["ad_schedule"] == []

        both = assets.update_call_asset(
            config, asset_id="555", ad_schedule=[MONDAY_9_TO_5], clear_ad_schedule=True
        )
        assert any("not both" in d for d in both["details"])

    def test_no_changes(self, config, gaql):
        gaql(asset=[_call_asset_row()])
        result = assets.update_call_asset(config, asset_id="555")
        assert any("No changes specified" in d for d in result["details"])


# ---------------------------------------------------------------------------
# add_ad_schedule
# ---------------------------------------------------------------------------


def _campaign_row(status="ENABLED"):
    return {
        "campaign.id": 42,
        "campaign.name": "Search - Plumbing",
        "campaign.status": status,
        "customer.time_zone": "America/Chicago",
    }


def _schedule_row(day, start, end, criterion_id, start_minute="ZERO"):
    return {
        "campaign_criterion.criterion_id": criterion_id,
        "campaign_criterion.ad_schedule.day_of_week": day,
        "campaign_criterion.ad_schedule.start_hour": start,
        "campaign_criterion.ad_schedule.start_minute": start_minute,
        "campaign_criterion.ad_schedule.end_hour": end,
        "campaign_criterion.ad_schedule.end_minute": "ZERO",
    }


class TestAddAdSchedule:
    def test_non_numeric_campaign_id_never_reaches_gaql(self, config, gaql):
        fake = gaql(campaign=[_campaign_row()])
        result = assets.add_ad_schedule(
            config, campaign_id="42; DROP", schedule=[MONDAY_9_TO_5]
        )
        assert any("campaign_id must be a numeric ID" in d for d in result["details"])
        assert fake.queries == []

    def test_needs_a_campaign_and_a_window(self, config):
        assert "campaign_id is required" in assets.add_ad_schedule(
            config, schedule=[MONDAY_9_TO_5]
        )["details"]
        assert "schedule needs at least one entry" in assets.add_ad_schedule(
            config, campaign_id="42", schedule=[]
        )["details"]

    def test_unknown_or_removed_campaign(self, config, gaql):
        gaql(campaign=[])
        assert "No campaign with ID 42" in assets.add_ad_schedule(
            config, campaign_id="42", schedule=[MONDAY_9_TO_5]
        )["error"]
        gaql(campaign=[_campaign_row("REMOVED")])
        assert "REMOVED" in assets.add_ad_schedule(
            config, campaign_id="42", schedule=[MONDAY_9_TO_5]
        )["error"]

    def test_first_schedule_warns_that_other_hours_stop(self, config, gaql):
        gaql(campaign=[_campaign_row()], campaign_criterion=[])
        result = assets.add_ad_schedule(
            config, customer_id="1234567890", campaign_id="42", schedule=[MONDAY_9_TO_5]
        )
        assert result["operation"] == "add_ad_schedule"
        assert result["entity_type"] == "campaign_criterion"
        changes = result["changes"]
        assert changes["account_time_zone"] == "America/Chicago"
        assert changes["existing_schedule"] == []
        assert any("no ad schedule today" in w for w in result["warnings"])
        assert any("America/Chicago" in w for w in result["warnings"])

    def test_existing_windows_are_listed_with_remove_ids(self, config, gaql):
        gaql(
            campaign=[_campaign_row("PAUSED")],
            campaign_criterion=[_schedule_row("TUESDAY", 10, 18, 314272, "THIRTY")],
        )
        result = assets.add_ad_schedule(config, campaign_id="42", schedule=[MONDAY_9_TO_5])
        [existing] = result["changes"]["existing_schedule"]
        assert existing["start_minute"] == 30
        assert existing["remove_id"] == "42~314272"
        assert not any("no ad schedule today" in w for w in result["warnings"])
        assert any("PAUSED" in w for w in result["warnings"])

    def test_overlap_with_an_existing_window_is_refused(self, config, gaql):
        gaql(campaign=[_campaign_row()], campaign_criterion=[_schedule_row("MONDAY", 8, 12, 304272)])
        result = assets.add_ad_schedule(config, campaign_id="42", schedule=[MONDAY_9_TO_5])
        assert result["error"] == "Validation failed"
        assert any("existing interval 08:00-12:00 overlaps new" in d for d in result["details"])
        assert result["existing_schedule"][0]["remove_id"] == "42~304272"


# ---------------------------------------------------------------------------
# Apply functions (real proto types)
# ---------------------------------------------------------------------------


def _create_changes(scope, **overrides):
    changes = {
        "scope": scope,
        "campaign_id": "42" if scope == "campaign" else "",
        "ad_group_id": "777" if scope == "ad_group" else "",
        "phone_number": "+19163393676",
        "country_code": "US",
        "call_conversion_action_id": "",
        "ad_schedule": [],
    }
    changes.update(overrides)
    return changes


class TestApplyCreateCallAsset:
    @pytest.mark.parametrize(
        "scope,operation,result_field",
        [
            ("campaign", "campaign_asset_operation", "campaign_asset_result"),
            ("ad_group", "ad_group_asset_operation", "ad_group_asset_result"),
            ("account", "customer_asset_operation", "customer_asset_result"),
        ],
    )
    def test_asset_and_link_in_one_mutate(self, scope, operation, result_field):
        client = FakeAdsClient()
        result = assets._apply_create_call_asset(client, "1", _create_changes(scope))

        asset_op, link_op = _mutate_operations(client)
        asset = asset_op.asset_operation.create
        assert asset.resource_name == "customers/1/asset/-1"
        assert asset.call_asset.phone_number == "+19163393676"
        assert asset.call_asset.country_code == "US"
        assert link_op._pb.WhichOneof("operation") == operation
        link = getattr(link_op, operation).create
        assert link.asset == "customers/1/asset/-1"
        assert link.field_type == client.enums.AssetFieldTypeEnum.CALL
        if scope == "campaign":
            assert link.campaign == "customers/1/campaign/42"
        if scope == "ad_group":
            assert link.ad_group == "customers/1/ad_group/777"
        assert result["scope"] == scope
        assert result["link"].endswith(result_field)

    def test_conversion_action_and_schedule_on_the_asset(self):
        client = FakeAdsClient()
        assets._apply_create_call_asset(client, "1", _create_changes(
            "campaign",
            call_conversion_action_id="179",
            ad_schedule=[{
                "day_of_week": "MONDAY", "start_hour": 7, "start_minute": 30,
                "end_hour": 18, "end_minute": 0,
            }],
        ))
        call = _mutate_operations(client)[0].asset_operation.create.call_asset
        assert call.call_conversion_action == "customers/1/conversion_action/179"
        assert call.call_conversion_reporting_state == (
            client.enums.CallConversionReportingStateEnum.USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION
        )
        [window] = call.ad_schedule_targets
        assert window.day_of_week == client.enums.DayOfWeekEnum.MONDAY
        assert window.start_minute == client.enums.MinuteOfHourEnum.THIRTY
        assert (window.start_hour, window.end_hour) == (7, 18)

    @pytest.mark.parametrize(
        "changes,message",
        [
            (_create_changes("campaign", campaign_id=""), "campaign_id is required"),
            (_create_changes("ad_group", ad_group_id=""), "ad_group_id is required"),
            (_create_changes("customer"), "Unknown call asset scope"),
        ],
    )
    def test_refuses_an_incomplete_plan_before_sending(self, changes, message):
        client = FakeAdsClient()
        with pytest.raises(ValueError, match=message):
            assets._apply_create_call_asset(client, "1", changes)
        assert client.calls == []


class TestApplyUpdateCallAsset:
    def _op(self, client):
        [(service, method, _request, kwargs)] = client.calls
        assert (service, method) == ("AssetService", "mutate_assets")
        [op] = kwargs["operations"]
        return op

    def test_masks_only_the_changed_fields(self):
        client = FakeAdsClient()
        assets._apply_update_call_asset(client, "1", {
            "asset_id": "555",
            "current": {"phone_number": "(916) 885-1005"},
            "phone_number": "+19167135818",
            "country_code": "US",
            "call_conversion_action_id": "179",
            "call_conversion_reporting_state": "USE_RESOURCE_LEVEL_CALL_CONVERSION_ACTION",
        })
        op = self._op(client)
        assert op.update.resource_name == "customers/1/asset/555"
        assert op.update.call_asset.phone_number == "+19167135818"
        assert list(op.update_mask.paths) == [
            "call_asset.phone_number",
            "call_asset.country_code",
            "call_asset.call_conversion_action",
            "call_asset.call_conversion_reporting_state",
        ]

    def test_empty_schedule_clears_it(self):
        client = FakeAdsClient()
        assets._apply_update_call_asset(client, "1", {"asset_id": "555", "ad_schedule": []})
        op = self._op(client)
        assert list(op.update_mask.paths) == ["call_asset.ad_schedule_targets"]
        assert len(op.update.call_asset.ad_schedule_targets) == 0

    def test_plan_without_fields_is_refused(self):
        client = FakeAdsClient()
        with pytest.raises(ValueError, match="no fields"):
            assets._apply_update_call_asset(client, "1", {"asset_id": "555"})
        assert client.calls == []


class TestApplyAddAdSchedule:
    def test_one_criterion_per_window(self):
        client = FakeAdsClient()
        windows, _ = assets._validate_ad_schedule([
            MONDAY_9_TO_5,
            {"day_of_week": "TUESDAY", "start_hour": 9, "end_hour": 24},
        ])
        result = assets._apply_add_ad_schedule(client, "1", {"campaign_id": "42", "schedule": windows})

        [(service, method, _request, kwargs)] = client.calls
        assert (service, method) == ("CampaignCriterionService", "mutate_campaign_criteria")
        ops = kwargs["operations"]
        assert [op.create.campaign for op in ops] == ["customers/1/campaign/42"] * 2
        assert ops[1].create.ad_schedule.day_of_week == client.enums.DayOfWeekEnum.TUESDAY
        assert ops[1].create.ad_schedule.end_hour == 24
        assert len(result["campaign_criteria"]) == 2


# ---------------------------------------------------------------------------
# Dispatch and validate-only dry runs
# ---------------------------------------------------------------------------

_PLANS = {
    "create_call_asset": _create_changes("campaign"),
    "update_call_asset": {"asset_id": "555", "phone_number": "+19167135818", "country_code": "US"},
    "add_ad_schedule": {
        "campaign_id": "42",
        "schedule": assets._validate_ad_schedule([MONDAY_9_TO_5])[0],
    },
}


@pytest.mark.parametrize("operation", sorted(_PLANS))
def test_operations_are_registered_in_the_dispatch(operation):
    client = FakeAdsClient()
    plan = ChangePlan(operation=operation, customer_id="1", changes=_PLANS[operation])
    write._dispatch_ads_plan(client, "1", plan)
    assert len(client.calls) == 1


@pytest.mark.parametrize("operation", sorted(_PLANS))
def test_operations_validate_under_the_validate_only_client(operation):
    fake = FakeAdsClient()
    client = ValidateOnlyClient(fake)
    plan = ChangePlan(operation=operation, customer_id="1", changes=_PLANS[operation])

    result = write._dispatch_ads_plan(client, "1", plan)

    [(_service, _method, request, _kwargs)] = fake.calls
    assert request.validate_only is True
    assert (client.validated_calls, client.skipped_calls) == (1, 0)
    assert PLACEHOLDER in str(result)


@pytest.mark.parametrize("operation", sorted(_PLANS))
def test_confirm_and_apply_dry_run_sends_validate_only(operation, config, monkeypatch):
    fake = FakeAdsClient()
    monkeypatch.setattr(
        write, "_validate_with_google",
        lambda cfg, plan: write._execute_plan(cfg, plan, validate_only=True),
    )
    monkeypatch.setattr("adloop.ads.client.get_ads_client", lambda _config: fake)
    plan = ChangePlan(operation=operation, customer_id="1234567890", changes=_PLANS[operation])
    store_plan(plan)

    result = write.confirm_and_apply(config, plan_id=plan.plan_id, dry_run=True)

    assert result["status"] == "DRY_RUN_SUCCESS", result
    assert result["checks"] == {"validated_calls": 1, "skipped_calls": 0}
    assert all(request.validate_only for _, _, request, _ in fake.calls)


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_are_registered_as_ads_writes():
    from adloop.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("draft_call_asset", "update_call_asset", "add_ad_schedule"):
        tool = tools[name]
        assert tool.annotations.read_only_hint is False, name
        assert tool.tags == {"ads"}, name
        assert "Args:" not in tool.description, name
    required = tools["draft_call_asset"].parameters["required"]
    assert {"phone_number", "country_code"} <= set(required)
    assert "scope" in tools["draft_call_asset"].parameters["properties"]
