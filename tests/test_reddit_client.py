"""Reddit REST client: auth header, refresh-once on 401, 429 handling, pagination, errors."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adloop import auth as adloop_auth
from adloop.config import AdLoopConfig, RedditConfig
from adloop.reddit import client
from adloop.reddit.auth import RedditApiError, RedditAuthError, RedditCredentials


class _Resp:
    def __init__(self, status: int, payload=None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"x" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _FakeCreds(RedditCredentials):
    """Credentials with a canned token; counts refreshes instead of calling Reddit."""

    def __init__(self):
        super().__init__(
            client_id="app", client_secret="s", refresh_token="rt",
            user_agent="python:adloop.test:v0 (by /u/tester)",
            access_token="at-1", expires_at=10**12,
        )
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1
        self.access_token = f"at-{self.refreshes + 1}"
        self.expires_at = 10**12
        return {"access_token": self.access_token}


class _Provider:
    def __init__(self, creds):
        self.creds = creds

    def ga4_credentials(self, config):  # pragma: no cover
        raise AssertionError

    def ads_credentials(self, config):  # pragma: no cover
        raise AssertionError

    def reddit_credentials(self, config):
        return self.creds


@pytest.fixture
def creds():
    fake = _FakeCreds()
    original = adloop_auth.get_credentials_provider()
    adloop_auth.set_credentials_provider(_Provider(fake))
    try:
        yield fake
    finally:
        adloop_auth.set_credentials_provider(original)


@pytest.fixture
def config():
    return AdLoopConfig(reddit=RedditConfig(client_id="app", client_secret="s", ad_account_id="a2_x"))


class TestRequest:
    def test_sends_bearer_and_user_agent(self, config, creds):
        seen = {}

        def fake(method, url, *, params=None, json=None, headers=None, timeout=None):
            seen.update(method=method, url=url, headers=headers, params=params)
            return _Resp(200, {"data": {"id": "a2_x"}})

        with patch("requests.request", side_effect=fake):
            payload = client.reddit_get(config, "ad_accounts/a2_x", {"x": 1})
        assert payload["data"]["id"] == "a2_x"
        assert seen["url"] == f"{client.API_BASE}/ad_accounts/a2_x"
        assert seen["headers"]["Authorization"] == "Bearer at-1"
        assert seen["headers"]["User-Agent"].startswith("python:adloop.test")
        assert seen["params"] == {"x": 1}

    def test_refreshes_once_on_401_then_fails(self, config, creds):
        responses = iter([_Resp(401, {"message": "expired"}), _Resp(401, {"message": "still bad"})])
        with patch("requests.request", side_effect=lambda *a, **k: next(responses)):
            with pytest.raises(RedditAuthError, match="even after a refresh"):
                client.reddit_get(config, "me")
        assert creds.refreshes == 1

    def test_401_then_success_after_refresh(self, config, creds):
        responses = iter([_Resp(401, None), _Resp(200, {"data": {"ok": True}})])
        tokens = []

        def fake(method, url, *, params=None, json=None, headers=None, timeout=None):
            tokens.append(headers["Authorization"])
            return next(responses)

        with patch("requests.request", side_effect=fake):
            assert client.reddit_get(config, "me")["data"]["ok"] is True
        assert tokens == ["Bearer at-1", "Bearer at-2"]

    def test_short_rate_limit_is_waited_out(self, config, creds):
        responses = iter([
            _Resp(429, {}, {"RateLimit": '"ads-reporting";r=0;t=1'}),
            _Resp(200, {"data": []}),
        ])
        with patch("requests.request", side_effect=lambda *a, **k: next(responses)), \
             patch("adloop.reddit.client.time.sleep") as sleep:
            client.reddit_get(config, "ad_accounts/a2_x/campaigns")
        sleep.assert_called_once_with(1)

    def test_long_rate_limit_surfaces_reset_seconds(self, config, creds):
        with patch(
            "requests.request",
            return_value=_Resp(429, {}, {"RateLimit": '"ads-reporting";r=0;t=45'}),
        ), patch("adloop.reddit.client.time.sleep") as sleep:
            with pytest.raises(RedditApiError) as exc:
                client.reddit_get(config, "ad_accounts/a2_x/reports")
        assert exc.value.status == 429
        assert exc.value.reset_seconds == 45
        assert "do not retry in a loop" in str(exc.value)
        sleep.assert_not_called()

    def test_403_names_scope_and_role(self, config, creds):
        with patch(
            "requests.request",
            return_value=_Resp(403, {"error": {"message": "insufficient scope"}}),
        ):
            with pytest.raises(RedditApiError) as exc:
                client.reddit_patch(config, "campaigns/c1", {"data": {}})
        assert exc.value.status == 403
        assert "insufficient scope" in str(exc.value)
        assert "adsedit" in str(exc.value)

    def test_other_errors_carry_status_url_and_detail(self, config, creds):
        with patch(
            "requests.request",
            return_value=_Resp(400, {"errors": [{"message": "name is required"}]}),
        ):
            with pytest.raises(RedditApiError, match="400 for .*campaigns: name is required"):
                client.reddit_post(config, "ad_accounts/a2_x/campaigns", {"data": {}})

    def test_reddit_errors_are_not_runtime_errors(self):
        """_safe swallows RuntimeError without hints; these must reach _structured_error."""
        assert not issubclass(RedditApiError, RuntimeError)
        assert not issubclass(RedditAuthError, RuntimeError)


class TestPagination:
    def test_follows_next_url_verbatim(self, config, creds):
        urls = []

        def fake(method, url, *, params=None, json=None, headers=None, timeout=None):
            urls.append((url, params))
            if "page.token" in url:
                return _Resp(200, {"data": [{"id": "c2"}], "pagination": {}})
            return _Resp(
                200,
                {
                    "data": [{"id": "c1"}],
                    "pagination": {"next_url": f"{client.API_BASE}/ad_accounts/a2_x/campaigns?page.token=T2"},
                },
            )

        with patch("requests.request", side_effect=fake):
            rows = client.reddit_get_all(config, "ad_accounts/a2_x/campaigns")
        assert [r["id"] for r in rows] == ["c1", "c2"]
        assert urls[0][1] == {"page.size": 100}
        assert urls[1][0].endswith("page.token=T2")
        assert urls[1][1] is None


class TestMoney:
    def test_from_micro(self):
        assert client.from_micro(12_345_678) == 12.35
        assert client.from_micro("2000000") == 2.0
        assert client.from_micro(None) is None
        assert client.from_micro("") is None

    def test_to_micro(self):
        assert client.to_micro(12.34) == 12_340_000
        assert client.to_micro(5) == 5_000_000

    def test_rate_limit_header_parsing(self):
        assert client._rate_limit_reset({"RateLimit": '"a";r=3;t=12,"b";r=0;t=7'}) == 7
        assert client._rate_limit_reset({}) is None
