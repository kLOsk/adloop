"""Tests for ``update_responsive_search_ad`` — both the draft (preview) layer
and the ``_apply_update_rsa`` mutation layer.

The Google Ads client is faked: we never hit the network, but we do verify
that the AdOperation we build carries the correct resource_name, the correct
update_mask paths, and the right field values. URL reachability is also
faked so the tests don't depend on the public internet.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig
from adloop.safety import preview as preview_store


# ---------------------------------------------------------------------------
# Fake Google Ads client + AdService
# ---------------------------------------------------------------------------


class _FakeAdService:
    """Captures the operations passed to ``mutate_ads`` for assertion.

    Returns a fake response shaped like the real one so callers that read
    ``response.results[0].resource_name`` continue to work.
    """

    def __init__(self) -> None:
        self.captured_operations: list[object] | None = None
        self.captured_customer_id: str | None = None

    def ad_path(self, customer_id: str, ad_id: str) -> str:
        return f"customers/{customer_id}/ads/{ad_id}"

    def mutate_ads(
        self,
        customer_id: str,
        operations: list[object],
    ) -> object:
        self.captured_operations = operations
        self.captured_customer_id = customer_id
        first_op = operations[0]
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=first_op.update.resource_name)]
        )


class _FakeClient:
    """Shim around the real client to swap in our fake AdService.

    Reuses the real client's ``enums`` and ``get_type`` for proto wiring;
    only ``get_service`` is intercepted.
    """

    def __init__(self, ad_service: _FakeAdService):
        self._base = GoogleAdsClient(
            credentials=None,
            developer_token="test-token",
            use_proto_plus=True,
            version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = self._base.enums
        self.get_type = self._base.get_type
        self._services = {"AdService": ad_service}

    def get_service(self, name: str) -> object:
        return self._services[name]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_pending_plans():
    preview_store._pending_plans.clear()
    yield
    preview_store._pending_plans.clear()


@pytest.fixture(autouse=True)
def stub_url_validation(monkeypatch):
    """Default: every URL passes. Tests can override to inject failures."""
    monkeypatch.setattr(
        write,
        "_validate_urls",
        lambda urls, timeout=10: {u: None for u in urls},
    )


@pytest.fixture
def config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(
            require_dry_run=False,
            log_file=str(tmp_path / "audit.log"),
        ),
    )


@pytest.fixture
def dry_run_config(tmp_path) -> AdLoopConfig:
    return AdLoopConfig(
        ads=AdsConfig(customer_id="123-456-7890"),
        safety=SafetyConfig(
            require_dry_run=True,
            log_file=str(tmp_path / "audit.log"),
        ),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_rejects_missing_ad_id(self, config):
        result = write.update_responsive_search_ad(
            config, customer_id="1234567890", final_url="https://example.com"
        )
        assert result["error"] == "Validation failed"
        assert any("ad_id is required" in d for d in result["details"])

    def test_rejects_non_numeric_ad_id(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="abc123",
            path1="Pricing",
        )
        assert result["error"] == "Validation failed"
        assert any("numeric" in d for d in result["details"])

    def test_rejects_when_no_change_provided(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
        )
        assert result["error"] == "Validation failed"
        assert any("No changes specified" in d for d in result["details"])

    def test_no_change_error_mentions_headlines_and_descriptions(self, config):
        # The error message must hint at the headlines/descriptions options too —
        # otherwise users won't know they exist.
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
        )
        joined = " ".join(result["details"])
        assert "headlines" in joined
        assert "descriptions" in joined

    def test_rejects_path1_too_long(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="this-is-way-too-long-for-a-path",
        )
        assert result["error"] == "Validation failed"
        assert any("path1 must be 15 chars" in d for d in result["details"])

    def test_rejects_path2_too_long(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path2="X" * 16,
        )
        assert result["error"] == "Validation failed"
        assert any("path2 must be 15 chars" in d for d in result["details"])

    def test_accepts_path_at_max_length_15(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="X" * 15,
        )
        assert result.get("error") is None
        assert result["operation"] == "update_responsive_search_ad"

    def test_rejects_unreachable_url(self, config, monkeypatch):
        monkeypatch.setattr(
            write,
            "_validate_urls",
            lambda urls, timeout=10: {u: "HTTP 404" for u in urls},
        )
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            final_url="https://example.com/missing",
        )
        assert result["error"] == "URL validation failed"
        assert any("not reachable" in d for d in result["details"])

    def test_blocked_operation_rejected_before_validation(
        self, config, monkeypatch
    ):
        config.safety.blocked_operations = ["update_responsive_search_ad"]
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="OK",
        )
        assert "blocked" in result["error"]

    def test_url_validation_skipped_when_only_paths_changed(
        self, config, monkeypatch
    ):
        called = {"count": 0}

        def spy_validate(urls, timeout=10):
            called["count"] += 1
            return {u: None for u in urls}

        monkeypatch.setattr(write, "_validate_urls", spy_validate)
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Pricing",
        )
        assert result.get("error") is None
        # No URL was supplied — we shouldn't have hit the validator.
        assert called["count"] == 0

    def test_multiple_validation_errors_returned_together(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="abc",  # non-numeric
            path1="X" * 16,  # too long
        )
        assert result["error"] == "Validation failed"
        assert len(result["details"]) >= 2


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


class TestPlanConstruction:
    def test_plan_metadata(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Sale",
        )
        assert result["operation"] == "update_responsive_search_ad"
        assert result["entity_type"] == "ad"
        assert result["entity_id"] == "999"
        assert result["customer_id"] == "1234567890"
        assert result["status"] == "PENDING_CONFIRMATION"
        assert "plan_id" in result

    def test_plan_stored_for_retrieval(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Sale",
        )
        plan = preview_store.get_plan(result["plan_id"])
        assert plan is not None
        assert plan.operation == "update_responsive_search_ad"

    def test_url_only_change_does_not_include_paths(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            final_url="https://example.com/x",
        )
        changes = result["changes"]
        assert changes["final_url"] == "https://example.com/x"
        assert "path1" not in changes
        assert "path2" not in changes

    def test_path1_only_change_does_not_include_url_or_path2(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Pricing",
        )
        changes = result["changes"]
        assert changes["path1"] == "Pricing"
        assert "final_url" not in changes
        assert "path2" not in changes

    def test_path2_only_change_does_not_include_url_or_path1(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path2="Sacramento",
        )
        changes = result["changes"]
        assert changes["path2"] == "Sacramento"
        assert "final_url" not in changes
        assert "path1" not in changes

    def test_all_three_fields_set(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            final_url="https://example.com/x",
            path1="A",
            path2="B",
        )
        changes = result["changes"]
        assert changes["final_url"] == "https://example.com/x"
        assert changes["path1"] == "A"
        assert changes["path2"] == "B"

    def test_clear_path1_writes_empty_string_into_changes(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            clear_path1=True,
        )
        changes = result["changes"]
        # ``"path1" in changes`` is critical — apply uses presence to decide
        # whether to mutate. Empty string is the *value*, not "no change".
        assert "path1" in changes
        assert changes["path1"] == ""
        assert "path2" not in changes

    def test_clear_path2_writes_empty_string_into_changes(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            clear_path2=True,
        )
        changes = result["changes"]
        assert "path2" in changes
        assert changes["path2"] == ""
        assert "path1" not in changes

    def test_clear_path1_overrides_path1_argument(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Pricing",
            clear_path1=True,
        )
        # When clear_path1 is True we ignore the path1 string and clear it.
        assert result["changes"]["path1"] == ""

    def test_paths_are_stripped_of_whitespace(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="  Sale  ",
            path2=" \t Pricing\n",
        )
        assert result["changes"]["path1"] == "Sale"
        assert result["changes"]["path2"] == "Pricing"

    def test_ad_id_coerced_to_string(self, config):
        # FastMCP types ad_id as str, but defensive coercion is cheap.
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="X",
        )
        assert isinstance(result["changes"]["ad_id"], str)
        assert result["changes"]["ad_id"] == "999"


# ---------------------------------------------------------------------------
# Apply / mutate
# ---------------------------------------------------------------------------


class TestApply:
    def test_calls_mutate_ads_with_correct_resource_name(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "final_url": "https://example.com"},
        )

        op = ad_service.captured_operations[0]
        assert op.update.resource_name == "customers/1234567890/ads/999"

    def test_url_only_field_mask_has_only_final_urls(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "final_url": "https://example.com"},
        )

        op = ad_service.captured_operations[0]
        assert list(op.update_mask.paths) == ["final_urls"]
        assert list(op.update.final_urls) == ["https://example.com"]

    def test_path1_only_field_mask(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "path1": "Sale"},
        )

        op = ad_service.captured_operations[0]
        assert list(op.update_mask.paths) == ["responsive_search_ad.path1"]
        assert op.update.responsive_search_ad.path1 == "Sale"

    def test_path2_only_field_mask(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "path2": "Sacramento"},
        )

        op = ad_service.captured_operations[0]
        assert list(op.update_mask.paths) == ["responsive_search_ad.path2"]
        assert op.update.responsive_search_ad.path2 == "Sacramento"

    def test_all_three_fields_in_mask(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {
                "ad_id": "999",
                "final_url": "https://example.com",
                "path1": "Sale",
                "path2": "NE",
            },
        )

        op = ad_service.captured_operations[0]
        assert set(op.update_mask.paths) == {
            "final_urls",
            "responsive_search_ad.path1",
            "responsive_search_ad.path2",
        }

    def test_clear_path_writes_empty_string_to_proto(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "path1": ""},  # the "clear" semantic
        )

        op = ad_service.captured_operations[0]
        assert "responsive_search_ad.path1" in list(op.update_mask.paths)
        assert op.update.responsive_search_ad.path1 == ""

    def test_returns_resource_name(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        result = write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "path1": "X"},
        )

        assert result == {"resource_name": "customers/1234567890/ads/999"}

    def test_passes_customer_id_to_mutate_call(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {"ad_id": "999", "path1": "X"},
        )

        assert ad_service.captured_customer_id == "1234567890"


# ---------------------------------------------------------------------------
# Confirm-and-apply integration
# ---------------------------------------------------------------------------


class TestConfirmAndApplyIntegration:
    def test_dry_run_returns_dry_run_success(self, config):
        draft = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Pricing",
        )
        result = write.confirm_and_apply(
            config, plan_id=draft["plan_id"], dry_run=True
        )
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["operation"] == "update_responsive_search_ad"

    def test_require_dry_run_overrides_dry_run_false(self, dry_run_config):
        draft = write.update_responsive_search_ad(
            dry_run_config,
            customer_id="1234567890",
            ad_id="999",
            path1="Pricing",
        )
        result = write.confirm_and_apply(
            dry_run_config, plan_id=draft["plan_id"], dry_run=False
        )
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result.get("dry_run_forced_by") == "config.safety.require_dry_run"

    def test_unknown_plan_id_returns_error(self, config):
        result = write.confirm_and_apply(
            config, plan_id="does-not-exist", dry_run=True
        )
        assert "error" in result

    def test_apply_routes_to_update_rsa(self, config, monkeypatch):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client", lambda _cfg: client
        )

        draft = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            final_url="https://example.com",
            path1="Sale",
        )
        result = write.confirm_and_apply(
            config, plan_id=draft["plan_id"], dry_run=False
        )

        assert result["status"] == "APPLIED"
        assert result["operation"] == "update_responsive_search_ad"
        assert ad_service.captured_operations is not None
        assert len(ad_service.captured_operations) == 1

    def test_apply_writes_audit_log(self, config, monkeypatch, tmp_path):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client", lambda _cfg: client
        )

        draft = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Sale",
        )
        write.confirm_and_apply(
            config, plan_id=draft["plan_id"], dry_run=False
        )

        log_path = config.safety.log_file
        from pathlib import Path
        contents = Path(log_path).read_text()
        assert "update_responsive_search_ad" in contents
        assert "success" in contents

    def test_apply_writes_dry_run_audit_log(self, config):
        draft = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Sale",
        )
        write.confirm_and_apply(
            config, plan_id=draft["plan_id"], dry_run=True
        )

        from pathlib import Path
        contents = Path(config.safety.log_file).read_text()
        assert "dry_run_success" in contents
        assert '"dry_run": true' in contents

    def test_plan_removed_after_successful_apply(self, config, monkeypatch):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client", lambda _cfg: client
        )

        draft = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            path1="Sale",
        )
        plan_id = draft["plan_id"]
        assert preview_store.get_plan(plan_id) is not None

        write.confirm_and_apply(config, plan_id=plan_id, dry_run=False)
        assert preview_store.get_plan(plan_id) is None


# ---------------------------------------------------------------------------
# Headline / description mutation (in-place text replacement via AdService)
# ---------------------------------------------------------------------------


def _valid_headlines() -> list[str]:
    """Minimum-viable headline set: 3 entries, each under 30 chars."""
    return ["Beaver Brothers Plumbing", "Same-Day Service", "Call Today"]


def _valid_descriptions() -> list[str]:
    """Minimum-viable description set: 2 entries, each under 90 chars."""
    return [
        "Sacramento plumbing — same-day service. Call now.",
        "Licensed pros, free estimates. Beaver Brothers Plumbing.",
    ]


class TestHeadlineDescriptionValidation:
    """Covers the count / char-limit / pin-slot validation paths added when
    update_responsive_search_ad gained headlines & descriptions support.
    """

    def test_rejects_too_few_headlines(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=["Only one headline"],
            descriptions=_valid_descriptions(),
        )
        assert result["error"] == "Validation failed"
        assert any("at least 3 headlines" in d for d in result["details"])

    def test_rejects_too_many_headlines(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[f"H{i}" for i in range(16)],
            descriptions=_valid_descriptions(),
        )
        assert result["error"] == "Validation failed"
        assert any("Maximum 15 headlines" in d for d in result["details"])

    def test_rejects_headline_over_30_chars(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[
                "OK headline one",
                "OK headline two",
                "X" * 31,  # over the cap by 1
            ],
            descriptions=_valid_descriptions(),
        )
        assert result["error"] == "Validation failed"
        assert any("exceeds 30 chars" in d for d in result["details"])

    def test_accepts_headline_at_max_30_chars(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=["X" * 30, "OK", "Also OK"],
            descriptions=_valid_descriptions(),
        )
        assert result.get("error") is None

    def test_rejects_invalid_headline_pin(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[
                {"text": "Brand", "pinned_field": "HEADLINE_99"},
                "OK two",
                "OK three",
            ],
            descriptions=_valid_descriptions(),
        )
        assert result["error"] == "Validation failed"
        assert any(
            "HEADLINE_99" in d and "invalid" in d for d in result["details"]
        )

    def test_rejects_too_many_headlines_per_pin_slot(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[
                {"text": "Brand A", "pinned_field": "HEADLINE_1"},
                {"text": "Brand B", "pinned_field": "HEADLINE_1"},
                {"text": "Brand C", "pinned_field": "HEADLINE_1"},  # 3rd in same slot
            ],
            descriptions=_valid_descriptions(),
        )
        assert result["error"] == "Validation failed"
        assert any(
            "At most 2 headlines may pin to HEADLINE_1" in d
            for d in result["details"]
        )

    def test_rejects_too_few_descriptions(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
            descriptions=["Only one description here."],
        )
        assert result["error"] == "Validation failed"
        assert any("at least 2 descriptions" in d for d in result["details"])

    def test_rejects_too_many_descriptions(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
            descriptions=[f"Description {i} for the ad." for i in range(5)],
        )
        assert result["error"] == "Validation failed"
        assert any("Maximum 4 descriptions" in d for d in result["details"])

    def test_rejects_description_over_90_chars(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
            descriptions=[
                "Short OK description.",
                "Y" * 91,  # over the cap by 1
            ],
        )
        assert result["error"] == "Validation failed"
        assert any("exceeds 90 chars" in d for d in result["details"])

    def test_rejects_invalid_description_pin(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
            descriptions=[
                {"text": "Legal disclaimer.", "pinned_field": "DESCRIPTION_9"},
                "Second description.",
            ],
        )
        assert result["error"] == "Validation failed"
        assert any(
            "DESCRIPTION_9" in d and "invalid" in d for d in result["details"]
        )

    def test_rejects_too_many_descriptions_per_pin_slot(self, config):
        # Descriptions: cap is 1 per slot (vs 2 for headlines)
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
            descriptions=[
                {"text": "First desc.", "pinned_field": "DESCRIPTION_1"},
                {"text": "Second desc.", "pinned_field": "DESCRIPTION_1"},
            ],
        )
        assert result["error"] == "Validation failed"
        assert any(
            "At most 1 description may pin to DESCRIPTION_1" in d
            for d in result["details"]
        )

    def test_headlines_only_does_not_require_descriptions(self, config):
        # Common use case: rewriting just headlines without touching descriptions.
        # Wait — list-replace semantics mean if you provide only headlines,
        # Google still enforces the 3-15 cap on the new headlines but doesn't
        # touch descriptions at all. So validation should pass here.
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
        )
        assert result.get("error") is None
        assert "headlines" in result["changes"]
        assert "descriptions" not in result["changes"]

    def test_descriptions_only_does_not_require_headlines(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            descriptions=_valid_descriptions(),
        )
        assert result.get("error") is None
        assert "descriptions" in result["changes"]
        assert "headlines" not in result["changes"]


class TestHeadlineDescriptionPlanConstruction:
    def test_normalizes_string_headlines_to_dicts(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
        )
        for h in result["changes"]["headlines"]:
            assert isinstance(h, dict)
            assert h["text"]  # non-empty
            assert h["pinned_field"] is None  # unpinned

    def test_preserves_pinned_field_dict_input(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[
                {"text": "Beaver Brothers Plumbing", "pinned_field": "HEADLINE_1"},
                "Same-Day Service",
                "Call Today",
            ],
        )
        pinned = result["changes"]["headlines"][0]
        assert pinned == {
            "text": "Beaver Brothers Plumbing",
            "pinned_field": "HEADLINE_1",
        }

    def test_mixed_string_and_dict_entries_normalize(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[
                {"text": "Pinned brand", "pinned_field": "HEADLINE_1"},
                "Unpinned plain",
                "Third headline",
            ],
        )
        hs = result["changes"]["headlines"]
        assert hs[0]["pinned_field"] == "HEADLINE_1"
        assert hs[1]["pinned_field"] is None
        assert hs[2]["pinned_field"] is None

    def test_combined_headlines_paths_url(self, config):
        result = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=_valid_headlines(),
            descriptions=_valid_descriptions(),
            final_url="https://example.com/x",
            path1="Pricing",
            path2="Sale",
        )
        changes = result["changes"]
        assert changes["final_url"] == "https://example.com/x"
        assert changes["path1"] == "Pricing"
        assert changes["path2"] == "Sale"
        assert len(changes["headlines"]) == 3
        assert len(changes["descriptions"]) == 2


class TestHeadlineDescriptionApply:
    def test_headlines_field_mask_and_assets_populated(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {
                "ad_id": "999",
                "headlines": [
                    {"text": "Beaver Brothers Plumbing", "pinned_field": None},
                    {"text": "Same-Day Service", "pinned_field": None},
                    {"text": "Call Today", "pinned_field": None},
                ],
            },
        )

        op = ad_service.captured_operations[0]
        assert "responsive_search_ad.headlines" in list(op.update_mask.paths)
        # Each AdTextAsset should carry the text through to the proto
        headlines = list(op.update.responsive_search_ad.headlines)
        assert len(headlines) == 3
        assert headlines[0].text == "Beaver Brothers Plumbing"
        assert headlines[1].text == "Same-Day Service"
        assert headlines[2].text == "Call Today"

    def test_descriptions_field_mask_and_assets_populated(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {
                "ad_id": "999",
                "descriptions": [
                    {"text": "Desc one.", "pinned_field": None},
                    {"text": "Desc two.", "pinned_field": None},
                ],
            },
        )

        op = ad_service.captured_operations[0]
        assert "responsive_search_ad.descriptions" in list(op.update_mask.paths)
        descs = list(op.update.responsive_search_ad.descriptions)
        assert len(descs) == 2
        assert descs[0].text == "Desc one."

    def test_pinned_field_propagates_to_proto_enum(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {
                "ad_id": "999",
                "headlines": [
                    {"text": "Brand", "pinned_field": "HEADLINE_1"},
                    {"text": "Plain A", "pinned_field": None},
                    {"text": "Plain B", "pinned_field": None},
                ],
            },
        )

        op = ad_service.captured_operations[0]
        headlines = list(op.update.responsive_search_ad.headlines)
        # The pinned headline carries the HEADLINE_1 enum value; unpinned
        # carries UNSPECIFIED (the proto default).
        expected_pin = client.enums.ServedAssetFieldTypeEnum.HEADLINE_1
        assert headlines[0].pinned_field == expected_pin
        # Unpinned entries should not set pinned_field (proto default).
        unspecified = client.enums.ServedAssetFieldTypeEnum.UNSPECIFIED
        assert headlines[1].pinned_field == unspecified
        assert headlines[2].pinned_field == unspecified

    def test_combined_paths_headlines_descriptions_in_mask(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {
                "ad_id": "999",
                "path1": "Sale",
                "headlines": [
                    {"text": "H1", "pinned_field": None},
                    {"text": "H2", "pinned_field": None},
                    {"text": "H3", "pinned_field": None},
                ],
                "descriptions": [
                    {"text": "D1.", "pinned_field": None},
                    {"text": "D2.", "pinned_field": None},
                ],
            },
        )

        op = ad_service.captured_operations[0]
        paths = set(op.update_mask.paths)
        assert paths == {
            "responsive_search_ad.path1",
            "responsive_search_ad.headlines",
            "responsive_search_ad.descriptions",
        }

    def test_headlines_only_does_not_include_url_or_path_in_mask(self, config):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)

        write._apply_update_rsa(
            client,
            "1234567890",
            {
                "ad_id": "999",
                "headlines": [
                    {"text": "H1", "pinned_field": None},
                    {"text": "H2", "pinned_field": None},
                    {"text": "H3", "pinned_field": None},
                ],
            },
        )

        op = ad_service.captured_operations[0]
        paths = list(op.update_mask.paths)
        assert paths == ["responsive_search_ad.headlines"]
        # final_urls must remain empty when no URL change is requested
        assert list(op.update.final_urls) == []


class TestHeadlineDescriptionIntegration:
    """End-to-end via confirm_and_apply — the same path the MCP tool takes."""

    def test_headline_rewrite_routes_through_dispatch_to_apply(
        self, config, monkeypatch
    ):
        ad_service = _FakeAdService()
        client = _FakeClient(ad_service)
        monkeypatch.setattr(
            "adloop.ads.client.get_ads_client", lambda _cfg: client
        )

        draft = write.update_responsive_search_ad(
            config,
            customer_id="1234567890",
            ad_id="999",
            headlines=[
                "Replacement Headline One",
                "Replacement Headline Two",
                "Replacement Headline Three",
            ],
        )
        assert draft.get("error") is None

        result = write.confirm_and_apply(
            config, plan_id=draft["plan_id"], dry_run=False
        )
        assert result["status"] == "APPLIED"
        assert ad_service.captured_operations is not None
        op = ad_service.captured_operations[0]
        # Verify the round trip carried headlines all the way to the proto
        texts = [h.text for h in op.update.responsive_search_ad.headlines]
        assert texts == [
            "Replacement Headline One",
            "Replacement Headline Two",
            "Replacement Headline Three",
        ]
