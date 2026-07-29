"""Tests for server error formatting and MCP instructions field."""

import pytest

from adloop.ads.gaql import _parse_gaql_error
from adloop.server import (
    _build_orchestration_instructions,
    _coerce_json_string_to_list,
    _structured_error,
)


def test_structured_error_steers_deleted_oauth_client_to_cloud_or_byo():
    """Users on <= v0.9 bundled credentials hit this the moment the retired
    shared OAuth project is deleted — the error must route them to AdLoop
    Cloud or their own project, not leave a raw Google error."""
    error = Exception(
        "('deleted_client: The OAuth client was deleted.', "
        "{'error': 'deleted_client', "
        "'error_description': 'The OAuth client was deleted.'})"
    )

    result = _structured_error("get_campaign_performance", error)

    assert result["auth_error"] == "OAUTH_CLIENT_DELETED_OR_INVALID"
    assert "getadloop.com" in result["hint"]
    assert "adloop init" in result["hint"]


def test_structured_error_detects_invalid_client_secret():
    error = Exception(
        "('invalid_client: Unauthorized', {'error': 'invalid_client', "
        "'error_description': 'Unauthorized'})"
    )

    result = _structured_error("run_ga4_report", error)

    assert result["auth_error"] == "OAUTH_CLIENT_DELETED_OR_INVALID"
    assert "credentials.json" in result["hint"]


def test_structured_error_detects_invalid_developer_token():
    error = Exception(
        "errors { error_code { authentication_error: DEVELOPER_TOKEN_INVALID } "
        'message: "The developer token is not valid." }'
    )

    result = _structured_error("list_accounts", error)

    assert result["error"] == "Google Ads authentication failed — developer token is invalid."
    assert result["auth_error"] == "DEVELOPER_TOKEN_INVALID"
    assert "ads.developer_token" in result["hint"]


def test_structured_error_detects_test_only_developer_token():
    error = Exception(
        "errors { error_code { authorization_error: DEVELOPER_TOKEN_NOT_APPROVED } "
        'message: "The developer token is only approved for use with test accounts." }'
    )

    result = _structured_error("list_accounts", error)

    assert result["error"] == (
        "Google Ads authorization failed — developer token is not approved "
        "for production accounts."
    )
    assert result["auth_error"] == "DEVELOPER_TOKEN_NOT_APPROVED"
    assert "test accounts" in result["hint"]


def test_structured_error_detects_revoked_oauth_token():
    error = Exception("invalid_grant: Token has been expired or revoked.")

    result = _structured_error("health_check", error)

    assert result["error"] == "Authentication failed — OAuth token expired or revoked."
    assert result["auth_error"] == "INVALID_GRANT"
    assert "~/.adloop/token.json" in result["hint"]


def test_parse_gaql_error_detects_invalid_developer_token():
    error = Exception(
        "errors { error_code { authentication_error: DEVELOPER_TOKEN_INVALID } "
        'message: "The developer token is not valid." }'
    )

    result = _parse_gaql_error(error)

    assert result.startswith("DEVELOPER_TOKEN_INVALID:")
    assert "ads.developer_token" in result


def test_parse_gaql_error_detects_test_only_developer_token():
    error = Exception(
        "errors { error_code { authorization_error: DEVELOPER_TOKEN_NOT_APPROVED } "
        'message: "The developer token is only approved for use with test accounts." }'
    )

    result = _parse_gaql_error(error)

    assert result.startswith("DEVELOPER_TOKEN_NOT_APPROVED:")
    assert "test accounts" in result


# ---------------------------------------------------------------------------
# MCP InitializeResult.instructions — orchestration hint
# ---------------------------------------------------------------------------


class TestOrchestrationInstructions:
    """The MCP `instructions` field is sent during the initialize handshake
    and (per spec) MAY be injected into the LLM's system prompt by clients
    that honor it. We send a compact must-knows summary, NOT the full ruleset.
    """

    def test_instructions_cover_safety_essentials(self):
        text = _build_orchestration_instructions()

        # Must mention the two-step write pattern.
        assert "PREVIEW" in text or "preview" in text
        assert "plan_id" in text
        assert "confirm_and_apply" in text

        # Must mention dry_run defaults.
        assert "dry_run" in text

    def test_instructions_cover_pre_write_checks(self):
        text = _build_orchestration_instructions()
        # Most expensive mistake we want to prevent.
        assert "BROAD" in text and "Smart Bidding" in text
        # Second-most expensive: dead URLs.
        assert "final_url" in text or "URL" in text
        # Avoid throwing budget at broken tracking.
        assert "tracking" in text.lower() or "conversions" in text.lower()

    def test_instructions_cover_data_literacy(self):
        text = _build_orchestration_instructions()
        # GDPR consent gap is the #1 source of misdiagnosed "tracking issues".
        assert "GDPR" in text or "consent" in text.lower()
        # Geo / language targeting is mandatory.
        assert "geo" in text.lower()
        assert "language" in text.lower()

    def test_instructions_point_at_full_ruleset(self):
        text = _build_orchestration_instructions()
        # The hint should tell the model where the full rules live.
        assert "install-rules" in text or "adloop.mdc" in text or "CLAUDE.md" in text

    def test_instructions_are_compact_not_full_rules(self):
        # Spec describes `instructions` as a "hint" — not a manual.
        # Hard cap so we don't accidentally regress to dumping 50KB through
        # the handshake.
        text = _build_orchestration_instructions()
        assert len(text) < 5_000, (
            f"instructions field is {len(text)} bytes — should be a compact "
            f"hint (<5KB). For full rules use install-rules."
        )

    def test_instructions_are_attached_to_mcp_server(self):
        # FastMCP forwards the constructor arg into the wire-protocol
        # InitializeResult.instructions. Sanity check it's actually wired.
        from adloop.server import mcp

        instructions = getattr(mcp, "instructions", None)
        assert instructions is not None
        assert isinstance(instructions, str)
        assert len(instructions) > 100, (
            "MCP instructions field should be the compact orchestration hint, "
            "not a placeholder"
        )


class TestListParamJsonStringCoercion:
    """Regression for issue #28 — some MCP clients (e.g. Cowork) serialize
    list-typed tool arguments as JSON-encoded strings rather than native
    arrays, causing Pydantic to reject the call with ``Input should be a
    valid list``. ``_coerce_json_string_to_list`` runs as a ``BeforeValidator``
    on every tool param annotated with ``_StrList``/``_StrListOpt``/
    ``_DictList``/``_DictListOpt`` and decodes the JSON before the standard
    list validator runs.
    """

    def test_passes_through_native_list(self):
        assert _coerce_json_string_to_list(["a", "b"]) == ["a", "b"]

    def test_passes_through_none(self):
        assert _coerce_json_string_to_list(None) is None

    def test_decodes_json_encoded_str_list_of_strings(self):
        assert _coerce_json_string_to_list('["pagePath"]') == ["pagePath"]

    def test_decodes_json_encoded_str_list_with_multiple_metrics(self):
        encoded = '["sessions", "totalUsers", "screenPageViews", "bounceRate"]'
        assert _coerce_json_string_to_list(encoded) == [
            "sessions",
            "totalUsers",
            "screenPageViews",
            "bounceRate",
        ]

    def test_decodes_json_encoded_str_list_of_dicts(self):
        encoded = '[{"text": "shoes", "match_type": "EXACT"}]'
        assert _coerce_json_string_to_list(encoded) == [
            {"text": "shoes", "match_type": "EXACT"}
        ]

    def test_non_list_json_string_passes_through_unchanged(self):
        # ``'42'`` and ``'"pagePath"'`` decode to non-list values. We must
        # NOT pretend they're lists — let Pydantic's normal list validator
        # raise the standard error so the client sees a useful schema
        # violation rather than a silently-mangled payload.
        assert _coerce_json_string_to_list("42") == "42"
        assert _coerce_json_string_to_list('"pagePath"') == '"pagePath"'

    def test_invalid_json_string_passes_through_unchanged(self):
        # Bare strings like ``pagePath`` aren't valid JSON. Pass them through
        # so Pydantic emits the standard ``Input should be a valid list``
        # error (which is the correct response — that input is genuinely
        # not a list, JSON-encoded or otherwise).
        assert _coerce_json_string_to_list("pagePath") == "pagePath"

    def test_empty_string_passes_through_unchanged(self):
        assert _coerce_json_string_to_list("") == ""


class TestListParamJsonStringCoercionEndToEnd:
    """End-to-end: the BeforeValidator must fire through Pydantic when the
    annotated types are used in a model. If this ever breaks (e.g. because
    we accidentally drop the ``Annotated`` wrapper), the unit-level tests
    above would still pass but the real-world fix would be silently gone.
    """

    def _model_with_str_list(self):
        from pydantic import BaseModel

        from adloop.server import _StrList, _StrListOpt

        class _M(BaseModel):
            required: _StrList
            optional: _StrListOpt = None

        return _M

    def test_pydantic_accepts_json_encoded_string_for_required_list(self):
        model_cls = self._model_with_str_list()
        m = model_cls(required='["pagePath"]')
        assert m.required == ["pagePath"]

    def test_pydantic_accepts_json_encoded_string_for_optional_list(self):
        model_cls = self._model_with_str_list()
        m = model_cls(required=["a"], optional='["b", "c"]')
        assert m.optional == ["b", "c"]

    def test_pydantic_still_rejects_non_list_strings(self):
        model_cls = self._model_with_str_list()
        with pytest.raises(Exception, match="valid list"):
            model_cls(required="pagePath")  # bare string, not JSON

    def test_pydantic_still_accepts_native_arrays(self):
        model_cls = self._model_with_str_list()
        m = model_cls(required=["a", "b"])
        assert m.required == ["a", "b"]


class TestNoStaleModuleConfig:
    """The multi-tenant runtime refactor removed the module-level ``_config``
    global from server.py — every tool must resolve config per-call via
    ``current_config()``. A contribution written against the old architecture
    merges textually clean and then NameErrors at call time (swallowed into an
    error dict by ``_safe``), which is exactly how add_negative_locations
    shipped broken. Guard the whole class of bug statically, plus one
    behavioral call-through of the affected tool.
    """

    def test_server_module_has_no_stale_config_references(self):
        import inspect
        import re

        import adloop.server as server_module

        source = inspect.getsource(server_module)
        stale = [
            f"line {source[: m.start()].count(chr(10)) + 1}"
            for m in re.finditer(r"(?<![A-Za-z0-9_])_config\b", source)
        ]
        assert not stale, (
            "server.py references the removed module-level _config global at "
            f"{', '.join(stale)}; use current_config() instead"
        )

    @pytest.mark.asyncio
    async def test_add_negative_locations_tool_resolves_runtime_config(self):
        from adloop import runtime
        from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
        from adloop.safety import preview as preview_store
        from adloop.safety.preview import InMemoryPlanStore
        from adloop.server import mcp

        preview_store.set_plan_store(InMemoryPlanStore())
        runtime.set_default_config(
            AdLoopConfig(
                ads=AdsConfig(customer_id="123-456-7890"),
                safety=SafetyConfig(require_dry_run=True),
            )
        )
        try:
            tool = await mcp.get_tool("add_negative_locations")
            result = tool.fn(campaign_id="1001", geo_target_ids=["2840"])
            assert "error" not in result, result.get("error")
            assert result["operation"] == "add_negative_locations"
            assert result["plan_id"]
        finally:
            runtime.set_default_config(None)
            preview_store.set_plan_store(InMemoryPlanStore())

    @pytest.mark.asyncio
    async def test_update_responsive_search_ad_tool_resolves_runtime_config(self):
        from adloop import runtime
        from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
        from adloop.safety import preview as preview_store
        from adloop.safety.preview import InMemoryPlanStore
        from adloop.server import mcp

        preview_store.set_plan_store(InMemoryPlanStore())
        runtime.set_default_config(
            AdLoopConfig(
                ads=AdsConfig(customer_id="123-456-7890"),
                safety=SafetyConfig(require_dry_run=True),
            )
        )
        try:
            tool = await mcp.get_tool("update_responsive_search_ad")
            # A path-only edit avoids URL reachability checks and the
            # learning-reset warning — keeps the call-through hermetic.
            result = tool.fn(ad_id="999", path1="Pricing")
            assert "error" not in result, result.get("error")
            assert result["operation"] == "update_responsive_search_ad"
            assert result["plan_id"]
            # headlines/descriptions accept both plain strings and pinned dicts
            # via the _StrOrDictListOpt coercion alias.
            result2 = tool.fn(
                ad_id="1000",
                headlines=[
                    "Fast Free Shipping",
                    {"text": "Shop the Sale", "pinned_field": "HEADLINE_1"},
                    "Save 20% Now",
                ],
            )
            assert "error" not in result2, result2.get("error")
            assert result2.get("warnings"), "text replace must warn"
        finally:
            runtime.set_default_config(None)
            preview_store.set_plan_store(InMemoryPlanStore())


class TestToolsets:
    """ADLOOP_TOOLSETS taxonomy + filtering (shared contract with AdLoop Cloud)."""

    def test_toolset_slugs_match_the_cross_repo_contract(self):
        from adloop.server import TOOLSETS

        assert list(TOOLSETS) == [
            "ads", "ga4", "tracking", "gtm", "gsc", "web", "merchant",
        ]

    @pytest.mark.asyncio
    async def test_every_tool_carries_exactly_one_toolset_tag(self):
        """New tools MUST be tagged — untagged tools would silently vanish
        for every ADLOOP_TOOLSETS user and every restricted cloud key."""
        from adloop.server import TOOLSETS, mcp

        known = set(TOOLSETS) | {"core"}
        for tool in await mcp.list_tools():
            hits = (tool.tags or set()) & known
            assert len(hits) == 1, f"{tool.name}: toolset tags {hits or 'MISSING'}"

    @pytest.mark.asyncio
    async def test_selection_exposes_chosen_sets_plus_core(self, monkeypatch):
        from adloop import server

        monkeypatch.setenv("ADLOOP_TOOLSETS", "ga4")
        saved = list(server.mcp._transforms)
        server._apply_toolsets_env()
        try:
            names = {t.name for t in await server.mcp.list_tools()}
            assert "run_ga4_report" in names
            assert "draft_key_event" in names
            # Core survives every selection.
            assert "health_check" in names
            assert "confirm_and_apply" in names
            # Everything else is gone.
            assert "get_campaign_performance" not in names
            assert "list_gtm_accounts" not in names
        finally:
            server.mcp._transforms[:] = saved

    def test_unknown_slug_fails_loudly_with_the_valid_list(self, monkeypatch):
        from adloop import server

        monkeypatch.setenv("ADLOOP_TOOLSETS", "ads,anlytics")
        with pytest.raises(ValueError, match="anlytics") as exc:
            server._apply_toolsets_env()
        assert "ads, ga4, tracking" in str(exc.value)

    def test_unset_env_leaves_the_full_catalog(self, monkeypatch):
        from adloop import server

        monkeypatch.delenv("ADLOOP_TOOLSETS", raising=False)
        before = list(server.mcp._transforms)
        server._apply_toolsets_env()
        assert server.mcp._transforms == before
