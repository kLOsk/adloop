"""Tests for the keyword match type change tool."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient

from adloop.ads import write
from adloop.ads.client import GOOGLE_ADS_API_VERSION
from adloop.config import AdLoopConfig, AdsConfig, SafetyConfig


@pytest.fixture
def config() -> AdLoopConfig:
    return AdLoopConfig(ads=AdsConfig(customer_id="123-456-7890"))


def _keyword_row(criterion_id, *, text="escape room", match_type="BROAD", status="ENABLED"):
    return {
        "ad_group.id": 111,
        "ad_group.name": "Suche",
        "ad_group_criterion.criterion_id": criterion_id,
        "ad_group_criterion.keyword.text": text,
        "ad_group_criterion.keyword.match_type": match_type,
        "ad_group_criterion.status": status,
    }


def _patch_rows(monkeypatch, rows):
    import adloop.ads.gaql as gaql

    monkeypatch.setattr(gaql, "execute_query", lambda *_a, **_k: [dict(r) for r in rows])


class TestDraftUpdateKeywordMatchTypes:
    def test_plans_the_change_with_keyword_text_and_before_value(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_keyword_row(555, text="escape room münchen")])
        monkeypatch.setattr(write, "_check_broad_match_safety", lambda *a, **k: [])

        result = write.draft_update_keyword_match_types(
            config,
            ad_group_id="111",
            updates=[{"criterion_id": "555", "match_type": "phrase"}],
        )

        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["operation"] == "update_keyword_match_types"
        assert result["changes"]["keywords"] == [
            {
                "criterion_id": "555",
                "keyword": "escape room münchen",
                "status": "ENABLED",
                "match_type_before": "BROAD",
                "match_type": "PHRASE",
            }
        ]

    def test_unknown_criterion_is_refused(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_keyword_row(555)])

        result = write.draft_update_keyword_match_types(
            config, ad_group_id="111",
            updates=[{"criterion_id": "999", "match_type": "EXACT"}],
        )

        assert "not keywords of ad group" in " ".join(result["details"])

    def test_already_matching_keyword_is_skipped(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_keyword_row(555, match_type="PHRASE")])

        result = write.draft_update_keyword_match_types(
            config, ad_group_id="111",
            updates=[{"criterion_id": "555", "match_type": "PHRASE"}],
        )

        assert result["error"] == "Nothing to change"
        assert "already uses PHRASE" in " ".join(result["details"])

    def test_invalid_match_type_is_refused(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_keyword_row(555)])

        result = write.draft_update_keyword_match_types(
            config, ad_group_id="111",
            updates=[{"criterion_id": "555", "match_type": "FUZZY"}],
        )

        assert "invalid" in " ".join(result["details"])

    def test_non_numeric_criterion_is_refused(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_keyword_row(555)])

        result = write.draft_update_keyword_match_types(
            config, ad_group_id="111",
            updates=[{"criterion_id": "abc", "match_type": "EXACT"}],
        )

        assert "must be the numeric id" in " ".join(result["details"])

    def test_ad_group_without_keywords_is_refused(self, config, monkeypatch):
        _patch_rows(monkeypatch, [])

        result = write.draft_update_keyword_match_types(
            config, ad_group_id="111",
            updates=[{"criterion_id": "555", "match_type": "EXACT"}],
        )

        assert "No keywords found in ad group 111" in result["error"]

    def test_broad_match_warning_is_attached(self, config, monkeypatch):
        _patch_rows(monkeypatch, [_keyword_row(555, match_type="PHRASE")])
        monkeypatch.setattr(
            write, "_check_broad_match_safety",
            lambda *a, **k: ["DANGEROUS: Broad Match without Smart Bidding"],
        )

        result = write.draft_update_keyword_match_types(
            config, ad_group_id="111",
            updates=[{"criterion_id": "555", "match_type": "BROAD"}],
        )

        assert "DANGEROUS" in result["changes"]["warnings"][0]

    def test_blocked_operation_is_refused(self, monkeypatch):
        blocked = AdLoopConfig(
            ads=AdsConfig(customer_id="123-456-7890"),
            safety=SafetyConfig(blocked_operations=["update_keyword_match_types"]),
        )
        result = write.draft_update_keyword_match_types(
            blocked, ad_group_id="111",
            updates=[{"criterion_id": "555", "match_type": "EXACT"}],
        )

        assert "blocked by configuration" in result["error"]


class _FakeCriterionClient:
    """Real proto types, fake mutation + readback, records call order."""

    def __init__(self, *, fail_index=None, readback_rows=None):
        base = GoogleAdsClient(
            credentials=None, developer_token="test-token",
            use_proto_plus=True, version=GOOGLE_ADS_API_VERSION,
        )
        self.enums = base.enums
        self.get_type = base.get_type
        self.request = None
        self.order: list[str] = []
        self._fail_index = fail_index
        self._readback_rows = readback_rows or []
        self._services = {
            "AdGroupCriterionService": SimpleNamespace(
                mutate_ad_group_criteria=self._mutate
            ),
            "GoogleAdsService": SimpleNamespace(search=self._search),
        }

    def get_service(self, name):
        return self._services[name]

    def _mutate(self, request=None, **kwargs):
        self.order.append("mutate")
        self.request = request
        results = []
        for index, operation in enumerate(request.operations):
            resource = "" if index == self._fail_index else operation.update.resource_name
            results.append(SimpleNamespace(resource_name=resource))
        return SimpleNamespace(results=results, partial_failure_error=None)

    def _search(self, customer_id, query):
        self.order.append("readback")
        return [
            SimpleNamespace(
                ad_group=SimpleNamespace(id=111, name="Suche"),
                ad_group_criterion=SimpleNamespace(
                    criterion_id=row["criterion_id"],
                    keyword=SimpleNamespace(
                        text=row["text"], match_type=row["match_type"]
                    ),
                    status="ENABLED",
                ),
            )
            for row in self._readback_rows
        ]


def _changes(**overrides):
    changes = {
        "ad_group_id": "111",
        "keywords": [
            {
                "criterion_id": "555",
                "keyword": "escape room münchen",
                "status": "ENABLED",
                "match_type_before": "BROAD",
                "match_type": "PHRASE",
            }
        ],
    }
    changes.update(overrides)
    return changes


class TestApplyKeywordMatchTypes:
    def test_mutation_uses_the_documented_field_and_path(self):
        client = _FakeCriterionClient(
            readback_rows=[{"criterion_id": 555, "text": "escape room münchen",
                            "match_type": "PHRASE"}]
        )

        result = write._apply_update_keyword_match_types(client, "1234567890", _changes())

        operation = client.request.operations[0]
        assert operation.update.resource_name == (
            "customers/1234567890/adGroupCriteria/111~555"
        )
        assert operation.update.keyword.match_type == client.enums.KeywordMatchTypeEnum.PHRASE
        assert list(operation.update_mask.paths) == ["keyword.match_type"]
        assert result["updated_count"] == 1
        assert client.order == ["mutate", "readback"]

    def test_readback_reports_the_new_match_type(self):
        client = _FakeCriterionClient(
            readback_rows=[{"criterion_id": 555, "text": "escape room münchen",
                            "match_type": "PHRASE"}]
        )

        result = write._apply_update_keyword_match_types(client, "1234567890", _changes())

        assert result["readback"]["keywords"][0][
            "ad_group_criterion.keyword.match_type"
        ] == "PHRASE"

    def test_a_rejected_keyword_is_reported_per_keyword(self):
        client = _FakeCriterionClient(fail_index=1)
        changes = _changes(
            keywords=[
                {"criterion_id": "555", "keyword": "a", "status": "ENABLED",
                 "match_type_before": "BROAD", "match_type": "PHRASE"},
                {"criterion_id": "556", "keyword": "b", "status": "ENABLED",
                 "match_type_before": "BROAD", "match_type": "EXACT"},
            ]
        )

        result = write._apply_update_keyword_match_types(client, "1234567890", changes)

        assert result["updated_count"] == 1
        assert result["partial_failure"] is True
        assert result["failed"][0]["criterion_id"] == "556"


class TestKeywordMatchTypeToolRegistration:
    @pytest.mark.asyncio
    async def test_annotations_and_schema(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        tool = tools["update_keyword_match_types"]
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert set(tool.tags) == {"ads"}
        for param in ("ad_group_id", "updates"):
            assert tool.parameters["properties"][param].get("description"), param
