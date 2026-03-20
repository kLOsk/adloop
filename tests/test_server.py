"""Tests for server error formatting."""

from adloop.ads.gaql import _parse_gaql_error
from adloop.server import _structured_error


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
