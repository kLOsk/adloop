"""Tests for server error formatting and MCP instructions field."""

from adloop.ads.gaql import _parse_gaql_error
from adloop.server import _build_orchestration_instructions, _structured_error


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
