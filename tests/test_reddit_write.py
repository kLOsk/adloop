"""Reddit write tools: drafts, safety guards, preflight dry run, executors, gate integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop import runtime
from adloop.config import AdLoopConfig, RedditConfig, SafetyConfig
from adloop.reddit import read, write
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore


_LOG_FILE = ""


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path):
    global _LOG_FILE
    _LOG_FILE = str(tmp_path / "audit.log")
    preview_store.set_plan_store(InMemoryPlanStore())
    read.reset_account_meta_cache()
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


def _config(**safety) -> AdLoopConfig:
    kwargs = dict(
        max_daily_budget=50.0, max_bid_increase_pct=100, require_dry_run=False, log_file=_LOG_FILE,
    )
    kwargs.update(safety)
    return AdLoopConfig(
        reddit=RedditConfig(client_id="app", client_secret="s", ad_account_id="a2_acct"),
        safety=SafetyConfig(**kwargs),
    )


_ACCOUNT = {"data": {"id": "a2_acct", "name": "Acme", "currency": "EUR", "time_zone_id": "Europe/Berlin"}}


def _fake_api(routes: dict):
    calls: list[tuple[str, str, dict | None]] = []

    def fake(config, method, path, *, params=None, json_body=None, absolute_url=""):
        target = absolute_url or path
        calls.append((method, target, json_body))
        for (m, suffix), payload in routes.items():
            if m == method and target.rstrip("/").endswith(suffix):
                return payload(json_body) if callable(payload) else payload
        raise AssertionError(f"unexpected Reddit call {method} {target}")

    return calls, patch("adloop.reddit.client.reddit_request", side_effect=fake)


def _no_scope_check():
    return patch("adloop.reddit.write._require_scope_for_writes", lambda config: None)


_CAMPAIGN = {"data": {"id": "c1", "name": "Launch", "configured_status": "ACTIVE",
                       "effective_status": "ACTIVE", "is_campaign_budget_optimization": False,
                       "objective": "CONVERSIONS"}}
_CBO_CAMPAIGN = {"data": {"id": "c9", "name": "CBO", "configured_status": "ACTIVE",
                           "effective_status": "ACTIVE", "is_campaign_budget_optimization": True,
                           "goal_type": "DAILY_SPEND", "goal_value": 20_000_000,
                           "bid_strategy": "MAXIMIZE_VOLUME", "optimization_goal": "PURCHASE"}}
_AD_GROUP = {"data": {"id": "g1", "name": "DE", "campaign_id": "c1", "configured_status": "ACTIVE",
                       "goal_type": "DAILY_SPEND", "goal_value": 20_000_000, "bid_value": 1_000_000,
                       "is_campaign_budget_optimization": False,
                       "targeting": {"geolocations": ["DE"], "communities": ["r/python"]}}}


class TestStatusDrafts:
    def test_pause_draft_previews_current_and_target(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN})
        with ctx:
            preview = write.pause_reddit_entity(_config(), entity_type="campaign", entity_id="c1")
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert preview["platform"] == "reddit"
        assert preview["operation"] == "reddit_set_status"
        assert preview["customer_id"] == "a2_acct"
        assert preview["changes"]["current_status"] == "ACTIVE"
        assert preview["changes"]["target_status"] == "PAUSED"
        assert preview_store.get_plan(preview["plan_id"]) is not None

    def test_enable_warns_when_no_op(self):
        paused = {"data": dict(_CAMPAIGN["data"], configured_status="ACTIVE")}
        _, ctx = _fake_api({("GET", "campaigns/c1"): paused})
        with ctx:
            preview = write.enable_reddit_entity(_config(), entity_type="campaign", entity_id="c1")
        assert any("no-op" in w for w in preview["warnings"])

    def test_remove_is_archive_with_double_confirm(self):
        _, ctx = _fake_api({("GET", "ads/ad1"): {"data": {"id": "ad1", "name": "Hero", "configured_status": "ACTIVE"}}})
        with ctx:
            preview = write.remove_reddit_entity(_config(), entity_type="ad", entity_id="ad1")
        assert preview["operation"] == "reddit_archive_entity"
        assert preview["requires_double_confirm"] is True
        assert preview["changes"]["target_status"] == "ARCHIVED"

    def test_validation_errors(self):
        result = write.pause_reddit_entity(_config(), entity_type="keyword", entity_id="")
        assert result["error"] == "Validation failed"
        assert any("entity_type" in d for d in result["details"])
        assert any("entity_id" in d for d in result["details"])

    def test_missing_entity(self):
        _, ctx = _fake_api({("GET", "campaigns/nope"): {"data": {}}})
        with ctx:
            result = write.pause_reddit_entity(_config(), entity_type="campaign", entity_id="nope")
        assert "not found" in result["error"]

    def test_blocked_operation(self):
        cfg = _config(blocked_operations=["reddit_set_status"])
        result = write.pause_reddit_entity(cfg, entity_type="campaign", entity_id="c1")
        assert "blocked" in result["error"]


class TestUpdateDrafts:
    def test_ad_group_budget_within_cap_previews_old_new(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(_config(), ad_group_id="g1", daily_budget=40)
        assert preview["operation"] == "reddit_update_ad_group"
        assert preview["changes"]["patch"] == {"goal_value": 40_000_000}
        assert preview["changes"]["display"]["budget"] == {
            "from": 20.0, "to": 40.0, "goal_type": "DAILY_SPEND", "currency": "EUR",
        }
        assert any("exceeds 50%" in w for w in preview.get("warnings", []))

    def test_ad_group_budget_over_cap_is_refused(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_ad_group(_config(), ad_group_id="g1", daily_budget=80)
        assert "exceeds maximum 50.00" in result["error"]
        assert "EUR" in result["error"]

    def test_lifetime_budget_needs_end_time_and_uses_daily_equivalent(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            missing = write.update_reddit_ad_group(_config(), ad_group_id="g1", lifetime_budget=300)
            assert any("end_time" in d for d in missing["details"])
            # 300 over 10 days = 30/day, under the 50 cap, but goal_type differs from DAILY_SPEND.
            result = write.update_reddit_ad_group(
                _config(), ad_group_id="g1", lifetime_budget=300,
                start_time="2026-10-01", end_time="2026-10-11",
            )
        assert any("goal_type cannot change" in d for d in result["details"])

    def test_bid_increase_guard(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            ok = write.update_reddit_ad_group(_config(), ad_group_id="g1", bid_value=1.8)
            too_much = write.update_reddit_ad_group(_config(), ad_group_id="g1", bid_value=2.5)
        assert ok["changes"]["patch"] == {"bid_value": 1_800_000}
        assert "Bid increase 150%" in too_much["error"]

    def test_targeting_replaces_named_keys_and_preserves_others(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_ad_group(
                _config(), ad_group_id="g1", communities=["r/django", "r/flask"], gender="female",
            )
        patch_targeting = preview["changes"]["patch"]["targeting"]
        assert patch_targeting["communities"] == ["r/django", "r/flask"]
        assert patch_targeting["geolocations"] == ["DE"]
        assert patch_targeting["gender"] == "FEMALE"
        assert any("REPLACE" in w for w in preview["warnings"])

    def test_budget_on_cbo_ad_group_points_to_campaign(self):
        cbo_group = {"data": dict(_AD_GROUP["data"], is_campaign_budget_optimization=True)}
        _, ctx = _fake_api({("GET", "ad_groups/g1"): cbo_group, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_ad_group(_config(), ad_group_id="g1", daily_budget=10)
        assert any("update_reddit_campaign" in d for d in result["details"])

    def test_campaign_budget_only_for_cbo(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_campaign(_config(), campaign_id="c1", daily_budget=10)
        assert any("update_reddit_ad_group" in d for d in result["details"])

    def test_cbo_campaign_budget_change(self):
        _, ctx = _fake_api({("GET", "campaigns/c9"): _CBO_CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.update_reddit_campaign(_config(), campaign_id="c9", daily_budget=25, name="CBO v2")
        assert preview["changes"]["patch"] == {"name": "CBO v2", "goal_value": 25_000_000}
        assert preview["changes"]["display"]["name"] == {"from": "CBO", "to": "CBO v2"}

    def test_nothing_to_change(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.update_reddit_campaign(_config(), campaign_id="c1", name="Launch")
        assert "Nothing to change" in result["details"][0]


class TestCreationDrafts:
    def test_campaign_draft_defaults_to_paused_and_ad_group_budgets(self):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="conversions", funding_instrument_id="fi1",
            )
        payload = preview["changes"]["payload"]
        assert payload["configured_status"] == "PAUSED"
        assert payload["objective"] == "CONVERSIONS"
        assert payload["is_campaign_budget_optimization"] is False
        assert "goal_value" not in payload
        assert preview["changes"]["display"]["status_on_create"] == "PAUSED"
        assert any("firing pixel" in w for w in preview["warnings"])

    def test_campaign_draft_requires_objective_and_funding(self):
        result = write.draft_reddit_campaign(_config(), campaign_name="Q4")
        assert any("objective is required" in d for d in result["details"])
        assert any("funding_instrument_id" in d for d in result["details"])

    def test_cbo_campaign_requires_bid_pixel_and_checks_cap(self):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            missing = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
                campaign_budget_optimization=True, daily_budget=10,
            )
            over = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
                campaign_budget_optimization=True, daily_budget=99, bid_strategy="MAXIMIZE_VOLUME",
                bid_type="CPC", conversion_pixel_id="px1",
            )
            ok = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
                campaign_budget_optimization=True, daily_budget=30, bid_strategy="MAXIMIZE_VOLUME",
                bid_type="CPC", conversion_pixel_id="px1",
            )
        assert any("bid_strategy" in d for d in missing["details"])
        assert any("conversion_pixel_id" in d for d in missing["details"])
        assert "exceeds maximum" in over["error"]
        assert ok["changes"]["payload"]["goal_value"] == 30_000_000
        assert ok["changes"]["payload"]["goal_type"] == "DAILY_SPEND"

    def test_unknown_objective_warns_instead_of_failing(self):
        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="TRAFFIC", funding_instrument_id="fi1",
            )
        assert preview["status"] == "PENDING_CONFIRMATION"
        assert any("not in AdLoop's known list" in w for w in preview["warnings"])

    def test_ad_group_draft_requires_pixel_targeting_budget_bid(self):
        result = write.draft_reddit_ad_group(_config(), campaign_id="c1", ad_group_name="DE")
        details = " ".join(result["details"])
        assert "conversion_pixel_id" in details
        assert "Targeting is required" in details

    def test_ad_group_draft_builds_paused_payload(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            preview = write.draft_reddit_ad_group(
                _config(), campaign_id="c1", ad_group_name="DE devs", conversion_pixel_id="px1",
                daily_budget=20, bid_strategy="maximize_volume", bid_type="cpc",
                optimization_goal="sign_up", geolocations=["DE"], communities=["r/python"],
                languages=["DE"],
            )
        payload = preview["changes"]["payload"]
        assert payload["configured_status"] == "PAUSED"
        assert payload["goal_value"] == 20_000_000
        assert payload["bid_strategy"] == "MAXIMIZE_VOLUME"
        assert payload["optimization_goal"] == "SIGN_UP"
        assert payload["targeting"]["languages"] == ["de"]
        assert payload["targeting"]["communities"] == ["r/python"]
        assert not any("No language targeting" in w for w in preview["warnings"])

    def test_ad_group_draft_on_cbo_campaign_rejects_budget(self):
        _, ctx = _fake_api({("GET", "campaigns/c9"): _CBO_CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            result = write.draft_reddit_ad_group(
                _config(), campaign_id="c9", ad_group_name="X", conversion_pixel_id="px1",
                daily_budget=5, geolocations=["DE"],
            )
        assert any("campaign budget optimization" in d for d in result["details"])

    def test_ad_group_manual_bidding_needs_bid_value(self):
        result = write.draft_reddit_ad_group(
            _config(), campaign_id="c1", ad_group_name="X", conversion_pixel_id="px1",
            daily_budget=5, bid_strategy="MANUAL_BIDDING", bid_type="CPC", geolocations=["DE"],
        )
        assert any("MANUAL_BIDDING needs bid_value" in d for d in result["details"])

    def test_ad_draft_verifies_click_url_and_builds_post_and_ad(self):
        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/x": None}, {})):
            preview = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="Ship faster",
                click_url="https://example.com/x", call_to_action="Learn More", body="Try it",
            )
        changes = preview["changes"]
        assert changes["post"]["type"] == "TEXT"
        assert changes["post"]["headline"] == "Ship faster"
        assert changes["post"]["content"][0] == {
            "destination_url": "https://example.com/x", "call_to_action": "Learn More",
        }
        assert changes["ad"]["configured_status"] == "PAUSED"
        assert changes["ad"]["profile_id"] == "p1"
        assert any("PAUSED" in w for w in preview["warnings"])

    def test_ad_draft_rejects_unreachable_url(self):
        with patch("adloop.ads.write._validate_urls", return_value=({"https://example.com/404": "HTTP 404"}, {})):
            result = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="x", click_url="https://example.com/404",
            )
        assert any("not reachable" in d for d in result["details"])

    def test_ad_draft_image_needs_url(self):
        result = write.draft_reddit_ad(
            _config(), ad_group_id="g1", profile_id="p1", headline="x",
            click_url="https://example.com", post_type="image",
        )
        assert any("image_url" in d for d in result["details"])
        video = write.draft_reddit_ad(
            _config(), ad_group_id="g1", profile_id="p1", headline="x",
            click_url="https://example.com", post_type="video",
        )
        assert any("post_type" in d for d in video["details"])


class TestConfirmAndApplyIntegration:
    """The shared gate must route reddit_* plans away from the Google client."""

    def _paused_plan(self):
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN})
        with ctx:
            preview = write.pause_reddit_entity(_config(), entity_type="campaign", entity_id="c1")
        return preview["plan_id"]

    def test_dry_run_runs_preflight_and_marks_plan(self):
        from adloop.ads import write as ads_write

        plan_id = self._paused_plan()
        _, ctx = _fake_api({("GET", "campaigns/c1"): _CAMPAIGN, ("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx, _no_scope_check(), patch("adloop.ads.client.get_ads_client", side_effect=AssertionError("google touched")):
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"]["entity"] == "Launch"
        assert "validate-only" in result["note"]
        assert "Reddit Ads" in result["message"]
        assert preview_store.get_plan(plan_id).dry_run_result is not None

    def test_failed_preflight_keeps_two_phase_gate_closed(self):
        from adloop.ads import write as ads_write

        plan_id = self._paused_plan()
        cfg = _config(two_phase_apply=True)
        gone = {("GET", "campaigns/c1"): {"data": {}}, ("GET", "ad_accounts/a2_acct"): _ACCOUNT}
        _, ctx = _fake_api(gone)
        with ctx, _no_scope_check():
            result = ads_write.confirm_and_apply(cfg, plan_id=plan_id, dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "no longer exists" in result["error"]
        assert preview_store.get_plan(plan_id).dry_run_result is None
        with _no_scope_check():
            refused = ads_write.confirm_and_apply(cfg, plan_id=plan_id, dry_run=False)
        assert refused["status"] == "DRY_RUN_REQUIRED"

    def test_real_apply_patches_status_without_google_client(self):
        from adloop.ads import write as ads_write

        plan_id = self._paused_plan()
        calls, ctx = _fake_api({
            ("PATCH", "campaigns/c1"): lambda body: {"data": {"id": "c1", "configured_status": body["data"]["configured_status"], "effective_status": "PAUSED"}},
        })
        with ctx, _no_scope_check(), patch("adloop.ads.client.get_ads_client", side_effect=AssertionError("google touched")):
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert result["status"] == "APPLIED"
        assert result["result"]["configured_status"] == "PAUSED"
        assert calls[0][2] == {"data": {"configured_status": "PAUSED"}}
        assert preview_store.get_plan(plan_id) is None

    def test_create_ad_reports_orphan_post_when_ad_fails(self):
        from adloop.ads import write as ads_write
        from adloop.reddit.auth import RedditApiError

        _, ctx = _fake_api({("GET", "ad_groups/g1"): _AD_GROUP})
        with ctx, patch("adloop.ads.write._validate_urls", return_value=({"https://example.com": None}, {})):
            plan_id = write.draft_reddit_ad(
                _config(), ad_group_id="g1", profile_id="p1", headline="x", click_url="https://example.com",
            )["plan_id"]

        def failing_ad(body):
            raise RedditApiError("Reddit Ads API returned 400: bad ad", status=400)

        _, ctx = _fake_api({
            ("POST", "profiles/p1/posts"): {"data": {"id": "post1", "post_url": "https://reddit.com/p/post1"}},
            ("POST", "ad_accounts/a2_acct/ads"): failing_ad,
        })
        with ctx, _no_scope_check():
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert "post_id post1" in result["error"]
        assert "Reuse the post_id" in result["error"]

    def test_create_campaign_forces_paused(self):
        from adloop.ads import write as ads_write

        _, ctx = _fake_api({("GET", "ad_accounts/a2_acct"): _ACCOUNT})
        with ctx:
            plan_id = write.draft_reddit_campaign(
                _config(), campaign_name="Q4", objective="CLICKS", funding_instrument_id="fi1",
            )["plan_id"]
        plan = preview_store.get_plan(plan_id)
        plan.changes["payload"]["configured_status"] = "ACTIVE"  # tampered plan
        preview_store.store_plan(plan)

        posted = {}

        def create(body):
            posted.update(body["data"])
            return {"data": {"id": "c-new", "name": "Q4", "configured_status": "PAUSED"}}

        _, ctx = _fake_api({("POST", "ad_accounts/a2_acct/campaigns"): create})
        with ctx, _no_scope_check():
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=False)
        assert posted["configured_status"] == "PAUSED"
        assert result["result"]["campaign_id"] == "c-new"

    def test_scope_check_blocks_read_only_grant(self):
        from adloop import auth as adloop_auth
        from adloop.reddit.auth import RedditCredentials

        class _ReadOnlyCreds(RedditCredentials):
            granted_scopes = ["adsread"]

        class _Provider:
            def ga4_credentials(self, config):  # pragma: no cover
                raise AssertionError

            def ads_credentials(self, config):  # pragma: no cover
                raise AssertionError

            def reddit_credentials(self, config):
                return _ReadOnlyCreds(client_id="a", client_secret="b", refresh_token="rt", user_agent="ua")

        original = adloop_auth.get_credentials_provider()
        adloop_auth.set_credentials_provider(_Provider())
        try:
            from adloop.ads import write as ads_write

            plan_id = self._paused_plan()
            result = ads_write.confirm_and_apply(_config(), plan_id=plan_id, dry_run=True)
        finally:
            adloop_auth.set_credentials_provider(original)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "adsedit" in result["error"]


class TestServerWrappers:
    """Tool registration: tags, annotations, structured errors."""

    @pytest.mark.asyncio
    async def test_reddit_tools_are_tagged_and_annotated(self):
        from adloop.server import mcp

        tools = await mcp.list_tools()
        reddit = [t for t in tools if "reddit" in t.name]
        assert len(reddit) >= 17
        for tool in reddit:
            assert set(tool.tags) == {"reddit"}, tool.name
        writes = {t.name for t in reddit if not t.annotations.readOnlyHint}
        assert writes == {
            "pause_reddit_entity", "enable_reddit_entity", "remove_reddit_entity",
            "update_reddit_campaign", "update_reddit_ad_group",
            "draft_reddit_campaign", "draft_reddit_ad_group", "draft_reddit_ad",
        }
        destructive = {t.name for t in reddit if t.annotations.destructiveHint}
        assert destructive == {"remove_reddit_entity"}

    @pytest.mark.asyncio
    async def test_reddit_tools_never_use_account_id_parameter(self):
        """Merchant tools own ``account_id``; Reddit must always say ``ad_account_id``."""
        from adloop.server import mcp

        for tool in await mcp.list_tools():
            if "reddit" in tool.name:
                params = tool.parameters.get("properties", {})
                assert "account_id" not in params, tool.name

    def test_structured_error_reddit_invalid_grant_is_not_google_advice(self):
        from adloop.reddit.auth import RedditAuthError
        from adloop.server import _structured_error

        runtime.set_deployment_mode("local")
        parsed = _structured_error("get_reddit_campaigns", RedditAuthError("invalid_grant", error_code="invalid_grant"))
        assert parsed["auth_error"] == "REDDIT_INVALID_GRANT"
        assert "reddit_token.json" in parsed["hint"]
        assert "Google" not in parsed["hint"]

    def test_structured_error_reddit_rate_limit(self):
        from adloop.reddit.auth import RedditApiError
        from adloop.server import _structured_error

        parsed = _structured_error("x", RedditApiError("429", status=429, reset_seconds=30))
        assert parsed["auth_error"] == "REDDIT_RATE_LIMITED"
        assert parsed["reset_seconds"] == 30

    def test_safe_wrapper_returns_hint_for_reddit_errors(self):
        from adloop.reddit.auth import RedditApiError
        from adloop.server import _safe

        @_safe
        def boom():
            raise RedditApiError("forbidden", status=403)

        result = boom()
        assert result["auth_error"] == "REDDIT_FORBIDDEN"
        assert "hint" in result

    @pytest.mark.asyncio
    async def test_health_check_reports_not_configured_without_reddit(self):
        from adloop.server import mcp

        tool = await mcp.get_tool("health_check")
        runtime.set_default_config(AdLoopConfig())
        try:
            with patch("adloop.ga4.reports.get_account_summaries", return_value={"total_properties": 0}), \
                 patch("adloop.ads.gaql.execute_query", return_value=[]):
                status = tool.fn()
        finally:
            runtime.set_default_config(None)
        assert status["reddit"] == "not_configured"
