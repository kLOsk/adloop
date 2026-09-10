"""Reddit OAuth: token refresh, rotation, the local token file, error mapping."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig, RedditConfig
from adloop.reddit import auth


class _Resp:
    def __init__(self, status: int, payload: dict | None):
        self.status_code = status
        self._payload = payload
        self.content = b"x" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _creds(**overrides) -> auth.RedditCredentials:
    kwargs = dict(
        client_id="app", client_secret="secret", refresh_token="rt-1",
        user_agent="python:adloop.test:v0 (by /u/tester)",
    )
    kwargs.update(overrides)
    return auth.RedditCredentials(**kwargs)


class TestRedditCredentials:
    def test_token_refreshes_once_and_caches(self):
        calls: list[dict] = []

        def fake_post(url, *, auth=None, data=None, headers=None, timeout=None):
            calls.append({"url": url, "auth": auth, "data": data, "headers": headers})
            return _Resp(200, {"access_token": "at-1", "expires_in": 3600, "token_type": "bearer"})

        creds = _creds()
        with patch("requests.post", side_effect=fake_post):
            assert creds.token() == "at-1"
            assert creds.token() == "at-1"

        assert len(calls) == 1
        assert calls[0]["url"] == auth.OAUTH_TOKEN_URL
        assert calls[0]["auth"] == ("app", "secret")
        assert calls[0]["data"] == {"grant_type": "refresh_token", "refresh_token": "rt-1"}
        # Reddit throttles default user agents on the token endpoint too.
        assert calls[0]["headers"]["User-Agent"].startswith("python:adloop.test")

    def test_expired_token_is_refreshed(self):
        creds = _creds(access_token="stale", expires_at=time.time() - 1)
        with patch("requests.post", return_value=_Resp(200, {"access_token": "fresh", "expires_in": 60})):
            assert creds.token() == "fresh"

    def test_rotated_refresh_token_is_absorbed(self):
        creds = _creds()
        with patch(
            "requests.post",
            return_value=_Resp(200, {"access_token": "at", "expires_in": 3600, "refresh_token": "rt-2"}),
        ):
            payload = creds.refresh()
        assert payload["refresh_token"] == "rt-2"
        assert creds.refresh_token == "rt-2"

    def test_invalid_grant_raises_auth_error_with_code(self):
        creds = _creds()
        with patch("requests.post", return_value=_Resp(400, {"error": "invalid_grant"})):
            with pytest.raises(auth.RedditAuthError) as exc:
                creds.refresh()
        assert exc.value.error_code == "invalid_grant"
        assert not isinstance(exc.value, RuntimeError)

    def test_invalid_grant_in_200_body_is_still_an_error(self):
        """Reddit answers some token failures with HTTP 200 + error field."""
        creds = _creds()
        with patch("requests.post", return_value=_Resp(200, {"error": "invalid_grant"})):
            with pytest.raises(auth.RedditAuthError, match="invalid_grant"):
                creds.refresh()

    def test_bad_client_credentials(self):
        creds = _creds()
        with patch("requests.post", return_value=_Resp(401, None)):
            with pytest.raises(auth.RedditAuthError, match="client id"):
                creds.refresh()

    def test_missing_refresh_token_fails_at_construction(self):
        with pytest.raises(auth.RedditAuthError) as exc:
            _creds(refresh_token="")
        assert exc.value.error_code == "missing_refresh_token"


class TestUserAgent:
    def test_explicit_user_agent_wins(self):
        cfg = AdLoopConfig(reddit=RedditConfig(user_agent="web:acme:v1 (by /u/acme)"))
        assert auth.build_user_agent(cfg) == "web:acme:v1 (by /u/acme)"

    def test_built_from_client_id_and_username(self):
        cfg = AdLoopConfig(reddit=RedditConfig(client_id="abc", username="u/daniel"))
        ua = auth.build_user_agent(cfg)
        assert ua.startswith("python:adloop.abc:v")
        assert ua.endswith("(by /u/daniel)")


class TestLocalTokenFile:
    def test_local_credentials_read_token_file_and_cache(self, tmp_path):
        token_path = tmp_path / "reddit_token.json"
        token_path.write_text(json.dumps({"refresh_token": "rt-file"}))
        cfg = AdLoopConfig(
            reddit=RedditConfig(client_id="app", client_secret="s", token_path=str(token_path))
        )
        auth.reset_local_cache()
        first = auth.local_credentials(cfg)
        second = auth.local_credentials(cfg)
        assert first is second
        assert first.refresh_token == "rt-file"

    def test_missing_token_file_points_to_wizard(self, tmp_path):
        cfg = AdLoopConfig(
            reddit=RedditConfig(client_id="app", client_secret="s", token_path=str(tmp_path / "nope.json"))
        )
        auth.reset_local_cache()
        with pytest.raises(auth.RedditAuthError, match="adloop init"):
            auth.local_credentials(cfg)

    def test_rotation_is_persisted_to_the_file(self, tmp_path):
        token_path = tmp_path / "reddit_token.json"
        token_path.write_text(json.dumps({"refresh_token": "rt-old", "scope": "adsread adsedit"}))
        creds = auth.LocalRedditCredentials(
            token_path=token_path, client_id="app", client_secret="s",
            refresh_token="rt-old", user_agent="ua",
        )
        with patch(
            "requests.post",
            return_value=_Resp(200, {"access_token": "at", "expires_in": 3600, "refresh_token": "rt-new"}),
        ):
            creds.refresh()
        stored = json.loads(token_path.read_text())
        assert stored["refresh_token"] == "rt-new"
        assert stored["scope"] == "adsread adsedit"

    def test_dead_grant_removes_the_file(self, tmp_path):
        token_path = tmp_path / "reddit_token.json"
        token_path.write_text(json.dumps({"refresh_token": "rt-old"}))
        creds = auth.LocalRedditCredentials(
            token_path=token_path, client_id="app", client_secret="s",
            refresh_token="rt-old", user_agent="ua",
        )
        with patch("requests.post", return_value=_Resp(400, {"error": "invalid_grant"})):
            with pytest.raises(auth.RedditAuthError):
                creds.refresh()
        assert not token_path.exists()


class TestAuthorizeUrl:
    def test_authorize_url_requests_permanent_grant_and_scopes(self):
        url = auth.authorize_url("app-id", "state123")
        assert url.startswith(auth.OAUTH_AUTHORIZE_URL + "?")
        assert "duration=permanent" in url
        assert "scope=adsread%2Cadsedit" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback" in url
        assert "state=state123" in url

    def test_exchange_code_strips_reddit_fragment_suffix(self):
        calls = []

        def fake_post(url, *, auth=None, data=None, headers=None, timeout=None):
            calls.append(data)
            return _Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

        with patch("requests.post", side_effect=fake_post):
            auth.exchange_code(
                client_id="a", client_secret="b", code="CODE#_",
                redirect_uri=auth.LOCAL_REDIRECT_URI, user_agent="ua",
            )
        assert calls[0]["code"] == "CODE"
        assert calls[0]["grant_type"] == "authorization_code"
