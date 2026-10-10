"""Tests for the Google Tag Manager write path (adloop.gtm.write).

The GTM client is replaced by an in-memory fake so apply handlers run end to
end — create/update/delete/publish bodies, fingerprints, and the refusals —
without any network access.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from adloop.config import AdLoopConfig, GtmConfig, SafetyConfig
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore

ACCOUNT, CONTAINER, WS = "6000001", "7000001", "12"
WS_PATH = f"accounts/{ACCOUNT}/containers/{CONTAINER}/workspaces/{WS}"


# ---------------------------------------------------------------------------
# Fake Tag Manager client
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGTM:
    """Just enough of tagmanager v2 for the write module."""

    def __init__(self):
        self.workspaces_list = [{"workspaceId": WS, "name": "Default Workspace"}]
        self.tags: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.status: dict = {"workspaceChange": []}
        self.create_version_response: dict = {
            "containerVersion": {
                "path": f"accounts/{ACCOUNT}/containers/{CONTAINER}/versions/42",
                "containerVersionId": "42",
                "name": "v",
            },
            "newWorkspacePath": f"accounts/{ACCOUNT}/containers/{CONTAINER}/workspaces/13",
        }
        # The version that is live before a publish (rollback target).
        self.live_version: dict = {
            "path": f"accounts/{ACCOUNT}/containers/{CONTAINER}/versions/41",
            "containerVersionId": "41",
            "name": "live before",
        }
        # Stored container versions by id (versions.get), for rollbacks.
        self.version_bodies: dict[str, dict] = {}
        self.publish_response: dict | None = None
        self.quick_preview_response: dict = {}
        self.publish_error: str = ""
        self.calls: list[tuple] = []

    # chain -------------------------------------------------------------
    def accounts(self):
        return self

    def containers(self):
        return self

    def workspaces(self):
        return _Workspaces(self)

    def versions(self):
        return _Versions(self)


class _Versions:
    def __init__(self, fake):
        self.f = fake

    def publish(self, path, fingerprint=None):
        def run():
            self.f.calls.append(("publish", path, fingerprint))
            if self.f.publish_error:
                raise RuntimeError(self.f.publish_error)
            if self.f.publish_response is not None:
                return copy.deepcopy(self.f.publish_response)
            published = self.f.version_bodies.get(path.rsplit("/", 1)[-1])
            if published is not None:
                self.f.live_version = copy.deepcopy(published)
                return {"containerVersion": copy.deepcopy(published)}
            return {"containerVersion": {"path": path}}
        return _Req(run)

    def get(self, path):
        def run():
            self.f.calls.append(("get_version", path))
            vid = path.rsplit("/", 1)[-1]
            if vid not in self.f.version_bodies:
                raise RuntimeError(f"404 {path}")
            return copy.deepcopy(self.f.version_bodies[vid])
        return _Req(run)

    def live(self, parent):
        def run():
            self.f.calls.append(("live", parent))
            if self.f.live_version is None:
                raise RuntimeError("404 no live version")
            # The documented shape: versions.live answers with a bare
            # ContainerVersion, not with a containerVersion wrapper.
            return copy.deepcopy(self.f.live_version)
        return _Req(run)


class _Workspaces:
    def __init__(self, fake):
        self.f = fake

    def list(self, parent):
        return _Req(lambda: {"workspace": self.f.workspaces_list})

    def get(self, path):
        return _Req(lambda: {"path": path})

    def getStatus(self, path):
        return _Req(lambda: copy.deepcopy(self.f.status))

    def create_version(self, path, body):
        def run():
            self.f.calls.append(("create_version", path, body))
            return copy.deepcopy(self.f.create_version_response)
        return _Req(run)

    def quick_preview(self, path):
        def run():
            self.f.calls.append(("quick_preview", path))
            return copy.deepcopy(self.f.quick_preview_response)
        return _Req(run)

    def tags(self):
        return _Entities(self.f, self.f.tags, "tag")

    def triggers(self):
        return _Entities(self.f, self.f.triggers, "trigger")


class _Entities:
    def __init__(self, fake, store, kind):
        self.f, self.store, self.kind = fake, store, kind

    def _id(self, path):
        return path.rsplit("/", 1)[-1]

    def get(self, path):
        def run():
            if self._id(path) not in self.store:
                raise RuntimeError(f"404 {path}")
            return copy.deepcopy(self.store[self._id(path)])
        return _Req(run)

    def list(self, parent):
        return _Req(lambda: {self.kind: list(copy.deepcopy(self.store).values())})

    def create(self, parent, body):
        def run():
            new_id = str(100 + len(self.store))
            self.f.calls.append((f"create_{self.kind}", parent, copy.deepcopy(body)))
            self.store[new_id] = {**body, f"{self.kind}Id": new_id, "fingerprint": "f0"}
            return self.store[new_id]
        return _Req(run)

    def update(self, path, body, fingerprint=None):
        def run():
            self.f.calls.append((f"update_{self.kind}", path, copy.deepcopy(body), fingerprint))
            self.store[self._id(path)] = {**body, "fingerprint": "f-new"}
            return self.store[self._id(path)]
        return _Req(run)

    def delete(self, path):
        def run():
            self.f.calls.append((f"delete_{self.kind}", path))
            self.store.pop(self._id(path))
            return {}
        return _Req(run)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_plan_store():
    preview_store.set_plan_store(InMemoryPlanStore())
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def fake():
    f = FakeGTM()
    with patch("adloop.gtm.client.get_gtm_write_client", return_value=f):
        yield f


def _config(tmp_path, *, write=True, html=False, blocked=None, require_dry_run=False):
    return AdLoopConfig(
        gtm=GtmConfig(
            account_id=ACCOUNT,
            container_id=CONTAINER,
            write_enabled=write,
            allow_custom_html=html,
        ),
        safety=SafetyConfig(
            require_dry_run=require_dry_run,
            log_file=str(tmp_path / "audit.log"),
            blocked_operations=blocked or [],
        ),
    )


def _ids():
    return {"account_id": ACCOUNT, "container_id": CONTAINER}


def _apply(config, plan_id, dry_run=False):
    from adloop.ads.write import confirm_and_apply

    return confirm_and_apply(config, plan_id=plan_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Opt-in: config gate + OAuth scopes
# ---------------------------------------------------------------------------


class TestOptIn:
    def test_drafts_refuse_when_writes_disabled(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        with pytest.raises(RuntimeError, match="write_enabled"):
            draft_gtm_tag(_config(tmp_path, write=False), **_ids(),
                          name="x", tag_type="gaawe")
        assert fake.calls == []

    def test_hosted_writes_disabled_points_at_the_adloop_cloud_side(
        self, tmp_path, fake
    ):
        """Hosted users have no config file to edit and no server to restart."""
        from adloop import runtime
        from adloop.gtm.write import draft_gtm_tag

        runtime.set_deployment_mode("server")
        try:
            with pytest.raises(RuntimeError) as excinfo:
                draft_gtm_tag(_config(tmp_path, write=False), **_ids(),
                              name="x", tag_type="gaawe")
        finally:
            runtime.set_deployment_mode("local")

        message = str(excinfo.value)
        assert "AdLoop Cloud" in message
        # Nothing for the user to edit or restart on the hosted side.
        assert "restart the MCP server" not in message

    @pytest.mark.parametrize("raw,expected", [
        ("true", True), (True, True), ("yes", True),
        ("false", False), (False, False), ("no", False), ("", False), (None, False),
    ])
    def test_config_flags_parse_strictly(self, tmp_path, raw, expected):
        import yaml

        from adloop.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"gtm": {"write_enabled": raw,
                                                "allow_custom_html": raw}}))
        cfg = load_config(str(path))
        assert cfg.gtm.write_enabled is expected
        assert cfg.gtm.allow_custom_html is expected

    def test_write_scopes_are_not_requested_by_default(self):
        from adloop.auth import _ALL_SCOPES, _GTM_WRITE_SCOPES, _requested_scopes

        scopes = _requested_scopes(AdLoopConfig())
        assert scopes == list(_ALL_SCOPES)
        assert not set(_GTM_WRITE_SCOPES) & set(scopes)

    def test_write_scopes_requested_only_when_enabled(self):
        from adloop.auth import _GTM_WRITE_SCOPES, _requested_scopes

        cfg = AdLoopConfig(gtm=GtmConfig(write_enabled=True))
        assert set(_GTM_WRITE_SCOPES) <= set(_requested_scopes(cfg))

    def test_existing_token_without_write_scopes_triggers_reconsent(self, tmp_path):
        """Enabling writes with an old read-only token must discard the token
        (Google refuses scope expansion on refresh) rather than 403 later."""
        import json

        from adloop.auth import _ALL_SCOPES, _oauth_flow

        token = tmp_path / "token.json"
        token.write_text(json.dumps({"scopes": list(_ALL_SCOPES)}))
        creds_path = tmp_path / "credentials.json"
        creds_path.write_text("{}")
        cfg = AdLoopConfig(gtm=GtmConfig(write_enabled=True))
        cfg.google.token_path = str(token)

        with patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as flow, \
                patch("adloop.auth._run_oauth_with_fallback") as run:
            run.return_value.scopes = []
            with pytest.raises(RuntimeError, match="not granted"):
                _oauth_flow(cfg, creds_path)
        assert not token.exists()
        requested = flow.call_args.args[1]
        assert "https://www.googleapis.com/auth/tagmanager.publish" in requested

    def test_hosted_provider_without_write_support_gets_capability_error(self):
        from adloop import auth

        class ReadOnlyProvider:
            def gtm_credentials(self, config):  # pragma: no cover - unused
                return object()

        saved = auth.get_credentials_provider()
        auth.set_credentials_provider(ReadOnlyProvider())
        try:
            with pytest.raises(RuntimeError, match="gtm_write_credentials"):
                auth.get_gtm_write_credentials(
                    AdLoopConfig(gtm=GtmConfig(write_enabled=True))
                )
        finally:
            auth.set_credentials_provider(saved)

    def test_blocked_operation_is_respected(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        cfg = _config(tmp_path, blocked=["gtm_publish_workspace"])
        result = draft_publish_gtm_workspace(cfg, **_ids())
        assert "blocked" in result["error"]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TestTagTypes:
    @pytest.mark.parametrize("bad", [
        "google_analytics_4_event", "google_ads_conversion_tracking",
        "awcr", "ua", "gaawc", "GAAWE",
    ])
    def test_non_canonical_types_rejected_at_draft(self, tmp_path, fake, bad):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(_config(tmp_path), **_ids(), name="x", tag_type=bad)
        assert result["error"] == "Validation failed"
        assert fake.calls == []

    @pytest.mark.parametrize("ok", ["gaawe", "awct", "sp", "googtag", "cvt_ABC123"])
    def test_canonical_and_gallery_types_accepted(self, tmp_path, fake, ok):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(_config(tmp_path), **_ids(), name="x", tag_type=ok,
                               firing_trigger_ids=["2147479553"])
        assert result["status"] == "PENDING_CONFIRMATION"
        assert result["platform"] == "gtm"

    def test_ads_remarketing_id_matches_read_module(self):
        from adloop.gtm.read import ADS_REMARKETING_TAG
        from adloop.gtm.write import KNOWN_TAG_TYPES

        assert ADS_REMARKETING_TAG in KNOWN_TAG_TYPES


class TestCreateTag:
    def test_create_then_apply_sends_exact_previewed_body(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        cfg = _config(tmp_path)
        preview = draft_gtm_tag(
            cfg, **_ids(), name="GA4 - form_submit", tag_type="gaawe",
            parameters=[{"type": "TEMPLATE", "key": "eventName", "value": "form_submit"}],
            firing_trigger_ids=["2147479553"],
        )
        assert preview["changes"]["workspace_id"] == WS
        assert any("publish" in w for w in preview["warnings"])
        assert fake.calls == []  # draft sends nothing

        result = _apply(cfg, preview["plan_id"])
        assert result["status"] == "APPLIED"
        op, parent, body = fake.calls[0]
        assert op == "create_tag" and parent == WS_PATH
        assert body == preview["changes"]["tag"]
        assert result["result"]["published"] is False

    def test_missing_firing_trigger_warns(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        preview = draft_gtm_tag(_config(tmp_path), **_ids(), name="x", tag_type="gaawe")
        assert any("never fire" in w for w in preview["warnings"])

    def test_malformed_parameters_rejected(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(_config(tmp_path), **_ids(), name="x", tag_type="gaawe",
                               parameters=[{"value": "no key"}])
        assert result["error"] == "Validation failed"

    def test_explicit_unknown_workspace_errors(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        with pytest.raises(ValueError, match="not found"):
            draft_gtm_tag(_config(tmp_path), **_ids(), workspace_id="999",
                          name="x", tag_type="gaawe")

    def test_ambiguous_workspaces_require_explicit_id(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.workspaces_list = [{"workspaceId": "1", "name": "A"},
                                {"workspaceId": "2", "name": "B"}]
        with pytest.raises(ValueError, match="pass workspace_id"):
            draft_gtm_tag(_config(tmp_path), **_ids(), name="x", tag_type="gaawe")


class TestCustomHtmlGate:
    def test_create_custom_html_refused_by_default(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(_config(tmp_path), **_ids(), name="x", tag_type="html",
                               parameters=[{"type": "TEMPLATE", "key": "html",
                                            "value": "<script>1</script>"}])
        assert result["gate"] == "gtm.allow_custom_html"
        assert "plan_id" not in result
        assert fake.calls == []

    def test_create_custom_html_allowed_when_opted_in(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        result = draft_gtm_tag(_config(tmp_path, html=True), **_ids(), name="x",
                               tag_type="html", firing_trigger_ids=["2147479553"])
        assert result["status"] == "PENDING_CONFIRMATION"

    def test_editing_existing_custom_html_refused(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["5"] = {"tagId": "5", "name": "Chat", "type": "html",
                          "fingerprint": "f1", "parameter": []}
        result = draft_gtm_tag(_config(tmp_path), **_ids(), tag_id="5",
                               firing_trigger_ids=["2147479553"])
        assert result["gate"] == "gtm.allow_custom_html"

    def test_pausing_custom_html_is_always_allowed(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["5"] = {"tagId": "5", "name": "Chat", "type": "html",
                          "fingerprint": "f1", "paused": False}
        cfg = _config(tmp_path)
        preview = draft_gtm_tag(cfg, **_ids(), tag_id="5", paused=True)
        assert preview["changes"]["patch"] == {"paused": True}
        assert _apply(cfg, preview["plan_id"])["status"] == "APPLIED"

    def test_gate_rechecked_at_apply(self, tmp_path, fake):
        """A plan drafted while allowed must not apply after the flag is
        turned off."""
        from adloop.gtm.write import draft_gtm_tag

        preview = draft_gtm_tag(_config(tmp_path, html=True), **_ids(), name="x",
                                tag_type="html", firing_trigger_ids=["2147479553"])
        result = _apply(_config(tmp_path, html=False), preview["plan_id"])
        assert "Custom HTML" in result["error"]
        assert fake.calls == []


class TestUpdateTag:
    EXISTING = {
        "tagId": "7", "name": "Ads conv", "type": "awct", "fingerprint": "f1",
        "parameter": [
            {"type": "TEMPLATE", "key": "conversionId", "value": "123"},
            {"type": "TEMPLATE", "key": "conversionLabel", "value": "old"},
        ],
        "firingTriggerId": ["20"],
        # Fields the tool does not manage — must survive the update.
        "priority": {"type": "INTEGER", "key": "priority", "value": "10"},
        "consentSettings": {"consentStatus": "needed"},
        "tagFiringOption": "oncePerEvent",
        "parentFolderId": "3",
        "scheduleStartMs": "1700000000000",
        "monitoringMetadata": {"type": "MAP"},
        "setupTag": [{"tagName": "Linker"}],
    }

    def test_update_preserves_unmanaged_fields_and_merges_parameters(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["7"] = copy.deepcopy(self.EXISTING)
        cfg = _config(tmp_path)
        preview = draft_gtm_tag(
            cfg, **_ids(), tag_id="7",
            parameters=[{"type": "TEMPLATE", "key": "conversionLabel", "value": "new"}],
        )
        assert preview["changes"]["before"]["parameter"][1]["value"] == "old"

        _apply(cfg, preview["plan_id"])
        op, path, body, fingerprint = fake.calls[0]
        assert op == "update_tag" and path == f"{WS_PATH}/tags/7"
        assert fingerprint == "f1"
        for field in ("priority", "consentSettings", "tagFiringOption",
                      "parentFolderId", "scheduleStartMs", "monitoringMetadata",
                      "setupTag", "firingTriggerId"):
            assert body[field] == self.EXISTING[field], field
        params = {p["key"]: p["value"] for p in body["parameter"]}
        assert params == {"conversionId": "123", "conversionLabel": "new"}

    def test_changed_since_preview_refuses(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["7"] = copy.deepcopy(self.EXISTING)
        cfg = _config(tmp_path)
        preview = draft_gtm_tag(cfg, **_ids(), tag_id="7", name="Renamed")
        fake.tags["7"]["fingerprint"] = "f2"  # a human edited it in the UI

        result = _apply(cfg, preview["plan_id"])
        assert "modified in Tag Manager" in result["error"]
        assert not any(c[0] == "update_tag" for c in fake.calls)

    def test_type_change_refused(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["7"] = copy.deepcopy(self.EXISTING)
        result = draft_gtm_tag(_config(tmp_path), **_ids(), tag_id="7", tag_type="gaawe")
        assert "cannot" in result["error"]

    def test_no_op_update_refused(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["7"] = copy.deepcopy(self.EXISTING)
        result = draft_gtm_tag(_config(tmp_path), **_ids(), tag_id="7", name="Ads conv")
        assert "No changes" in result["error"]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class TestTriggers:
    @pytest.mark.parametrize("bad", ["dom_ready", "window_loaded", "scroll_depth",
                                     "youtube_video", "javascript_error"])
    def test_snake_case_types_rejected(self, tmp_path, fake, bad):
        from adloop.gtm.write import draft_gtm_trigger

        result = draft_gtm_trigger(_config(tmp_path), **_ids(), name="t", trigger_type=bad)
        assert result["error"] == "Validation failed"

    def test_custom_event_adds_event_filter(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_trigger

        cfg = _config(tmp_path)
        preview = draft_gtm_trigger(cfg, **_ids(), name="lead", trigger_type="customEvent",
                                    custom_event_name="generate_lead")
        _apply(cfg, preview["plan_id"])
        body = fake.calls[0][2]
        cond = body["customEventFilter"][0]["parameter"]
        assert cond[0]["value"] == "{{_event}}" and cond[1]["value"] == "generate_lead"

    def test_custom_event_requires_name(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_trigger

        result = draft_gtm_trigger(_config(tmp_path), **_ids(), name="t",
                                   trigger_type="customEvent")
        assert result["error"] == "Validation failed"

    def test_custom_event_name_cannot_change_on_an_existing_trigger(
        self, tmp_path, fake
    ):
        """It was dropped silently: the rename applied and the event did not."""
        from adloop.gtm.write import draft_gtm_trigger

        fake.triggers["5"] = {
            "triggerId": "5", "name": "lead", "type": "customEvent",
            "fingerprint": "t1",
        }
        result = draft_gtm_trigger(_config(tmp_path), **_ids(), trigger_id="5",
                                   name="renamed", custom_event_name="b")
        assert result["error"] == "Validation failed"
        assert any("custom_event_name" in d for d in result["details"])

    def test_update_preserves_unmanaged_fields(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_trigger

        fake.triggers["20"] = {
            "triggerId": "20", "name": "Tel clicks", "type": "click", "fingerprint": "t1",
            "filter": [{"type": "CONTAINS", "parameter": []}],
            "waitForTags": {"type": "BOOLEAN", "key": "waitForTags", "value": "true"},
            "checkValidation": {"type": "BOOLEAN", "value": "false"},
            "parentFolderId": "3",
        }
        cfg = _config(tmp_path)
        new_filter = [{"type": "CONTAINS", "parameter": [
            {"type": "TEMPLATE", "key": "arg0", "value": "{{Click URL}}"},
            {"type": "TEMPLATE", "key": "arg1", "value": "tel:"}]}]
        preview = draft_gtm_trigger(cfg, **_ids(), trigger_id="20", filters=new_filter)
        _apply(cfg, preview["plan_id"])
        _, _, body, fingerprint = fake.calls[0]
        assert fingerprint == "t1"
        assert body["filter"] == new_filter
        for field in ("waitForTags", "checkValidation", "parentFolderId", "type"):
            assert body[field] == fake_value(field)

    def test_malformed_filter_rejected(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_trigger

        result = draft_gtm_trigger(_config(tmp_path), **_ids(), name="t",
                                   trigger_type="click", filters=[{"value": "x"}])
        assert result["error"] == "Validation failed"


def fake_value(field):
    return {
        "waitForTags": {"type": "BOOLEAN", "key": "waitForTags", "value": "true"},
        "checkValidation": {"type": "BOOLEAN", "value": "false"},
        "parentFolderId": "3",
        "type": "click",
    }[field]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_referenced_trigger_refused_with_tags_listed(self, tmp_path, fake):
        from adloop.gtm.write import draft_delete_gtm_entity

        fake.triggers["20"] = {"triggerId": "20", "name": "T", "fingerprint": "t1"}
        fake.tags["7"] = {"tagId": "7", "name": "Uses T", "firingTriggerId": ["20"]}
        result = draft_delete_gtm_entity(_config(tmp_path), **_ids(),
                                         entity_type="trigger", entity_id="20")
        assert result["referenced_by"] == [{"tag_id": "7", "name": "Uses T"}]

    def test_delete_tag_applies_with_fingerprint_check(self, tmp_path, fake):
        from adloop.gtm.write import draft_delete_gtm_entity

        fake.tags["7"] = {"tagId": "7", "name": "Old", "type": "gaawe", "fingerprint": "f1"}
        cfg = _config(tmp_path)
        preview = draft_delete_gtm_entity(cfg, **_ids(), entity_type="tag", entity_id="7")
        assert preview["requires_double_confirm"] is True
        _apply(cfg, preview["plan_id"])
        assert ("delete_tag", f"{WS_PATH}/tags/7") in fake.calls

    def test_invalid_entity_type(self, tmp_path, fake):
        from adloop.gtm.write import draft_delete_gtm_entity

        result = draft_delete_gtm_entity(_config(tmp_path), **_ids(),
                                         entity_type="variable", entity_id="1")
        assert "tag' or 'trigger" in result["error"]


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


# The id field each Entity kind carries, as the v2 discovery document defines
# it: a custom template calls its id ``templateId``.
_ENTITY_ID_FIELDS = {"customTemplate": "templateId"}


def _change(kind, entity_id, status="added", **extra):
    return {"changeStatus": status,
            kind: {_ENTITY_ID_FIELDS.get(kind, f"{kind}Id"): entity_id,
                   "fingerprint": "f", **extra}}


class TestPublish:
    def test_nothing_pending_refused(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        result = draft_publish_gtm_workspace(_config(tmp_path), **_ids())
        assert "no pending changes" in result["error"]

    def test_merge_conflict_refused(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="x", type="gaawe")],
                       "mergeConflict": [{"entityInWorkspace": {}}]}
        result = draft_publish_gtm_workspace(_config(tmp_path), **_ids())
        assert "merge conflicts" in result["error"]

    def test_custom_html_in_workspace_blocks_publish(self, tmp_path, fake):
        """Covers HTML added by a human in the UI, not just via AdLoop."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [
            _change("tag", "9", status="updated", name="Chat widget", type="html"),
        ]}
        result = draft_publish_gtm_workspace(_config(tmp_path), **_ids())
        assert result["gate"] == "gtm.allow_custom_html"
        assert result["custom_html_tags"][0]["name"] == "Chat widget"

    def test_deleting_custom_html_does_not_block_publish(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [
            _change("tag", "9", status="deleted", name="Old chat", type="html"),
        ]}
        result = draft_publish_gtm_workspace(_config(tmp_path), **_ids())
        assert result["status"] == "PENDING_CONFIRMATION"

    def test_the_custom_html_gate_is_rechecked_at_apply(self, tmp_path, fake):
        """The flag can be switched off after the draft, and an apply needs no
        dry run, so the stored preview has to be checked again."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [
            _change("tag", "9", status="updated", name="Chat widget", type="html"),
        ]}
        preview = draft_publish_gtm_workspace(_config(tmp_path, html=True), **_ids())
        assert preview["status"] == "PENDING_CONFIRMATION"

        result = _apply(_config(tmp_path), preview["plan_id"])
        assert "involves a Custom HTML tag" in result["error"]
        assert not [
            c for c in fake.calls if c[0] in ("create_version", "publish")
        ]

    def test_a_template_only_workspace_is_not_reported_as_empty(
        self, tmp_path, fake
    ):
        """A workspace whose only change is a custom template has work to do."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [
            _change("customTemplate", "7", name="Call tracking"),
        ]}
        preview = draft_publish_gtm_workspace(_config(tmp_path), **_ids())
        pending = preview["changes"]["pending_changes"]
        assert [c["entity_kind"] for c in pending] == ["customTemplate"]
        # The id comes from ``templateId``, the field the API actually sends.
        assert pending[0]["entity_id"] == "7"
        assert pending[0]["name"] == "Call tracking"

    @pytest.mark.parametrize("kind", ["customTemplate", "gtagConfig"])
    def test_a_change_outside_tags_pins_the_digest(self, tmp_path, fake, kind):
        """Every Entity kind counts: a template added in the GTM UI after the
        preview must not go live through the publish."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [
            _change("tag", "1", name="GA4", type="gaawe"),
        ]}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())

        fake.status["workspaceChange"].append(
            _change(kind, "7", name="Added in the UI")
        )
        result = _apply(cfg, preview["plan_id"])
        assert "differ from what was previewed" in result["error"]
        assert not [
            c for c in fake.calls if c[0] in ("create_version", "publish")
        ]

    def test_publish_happy_path(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids(), version_name="Add GA4 event")
        assert preview["changes"]["pending_changes"][0]["name"] == "GA4"
        assert any("LIVE" in w for w in preview["warnings"])

        result = _apply(cfg, preview["plan_id"])
        assert result["result"]["published"] is True
        assert result["result"]["version_id"] == "42"
        # Reads (the live-version lookup) may sit in between; only the order of
        # the two mutations matters.
        mutations = [c for c in fake.calls if c[0] in ("create_version", "publish")]
        assert [c[0] for c in mutations] == ["create_version", "publish"]
        assert mutations[0][2]["name"] == "Add GA4 event"

    def test_publish_records_the_previous_live_version(self, tmp_path, fake):
        """Rollback is one step: the result names the version that was live."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())

        assert preview["changes"]["previous_live_version_id"] == "41"
        assert any("live version 41" in w for w in preview["warnings"])

        result = _apply(cfg, preview["plan_id"])
        assert result["result"]["previous_live_version_id"] == "41"
        assert result["result"]["previous_live_version_name"] == "live before"

    def test_a_container_without_a_live_version_still_publishes(
        self, tmp_path, fake
    ):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.live_version = None
        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())

        assert preview["changes"]["previous_live_version_id"] is None
        result = _apply(cfg, preview["plan_id"])
        assert result["result"]["published"] is True
        assert result["result"]["previous_live_version_id"] is None

    def test_a_wrapped_live_version_is_still_read(self, tmp_path, fake):
        """Defensive: if a response ever carries the version under
        ``containerVersion``, the rollback id must not silently become None."""
        from adloop.gtm.write import _live_version

        fake.live_version = {
            "containerVersion": {
                "path": f"accounts/{ACCOUNT}/containers/{CONTAINER}/versions/40",
                "containerVersionId": "40",
                "name": "wrapped",
            }
        }

        assert _live_version(fake, ACCOUNT, CONTAINER) == {
            "version_id": "40",
            "version_name": "wrapped",
        }

    def test_quick_preview_compiler_error_is_caught_in_the_dry_run(
        self, tmp_path, fake
    ):
        """The dry run compiles the workspace, so broken code is found before a
        version exists at all."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        fake.quick_preview_response = {"compilerError": True}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())

        result = _apply(cfg, preview["plan_id"], dry_run=True)

        assert result["status"] == "DRY_RUN_FAILED"
        assert "quick preview reports compiler errors" in result["error"]
        assert not any(c[0] == "create_version" for c in fake.calls)

    def test_dry_run_reports_the_quick_preview_and_live_version(
        self, tmp_path, fake
    ):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())

        result = _apply(cfg, preview["plan_id"], dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"]["quick_preview"] == "compiles"
        assert result["checks"]["previous_live_version_id"] == "41"
        assert "quick preview" in result["note"]
        assert not any(c[0] == "create_version" for c in fake.calls)

    def test_a_failed_publish_names_the_version_it_created(self, tmp_path, fake):
        """create_version succeeded, publish did not: the created version id has
        to be in the error, or the work is lost."""
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        fake.publish_error = "403 insufficient permissions"
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())

        result = _apply(cfg, preview["plan_id"])

        assert "created version 42" in result["error"]
        assert "versions/42" in result["error"]
        assert "live version is still 41" in result["error"]

    def test_workspace_changed_since_preview_refuses(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())
        fake.status["workspaceChange"].append(_change("tag", "2", name="Sneaky", type="img"))

        result = _apply(cfg, preview["plan_id"])
        assert "differ from what was previewed" in result["error"]
        assert not [
            c for c in fake.calls if c[0] in ("create_version", "publish")
        ]

    def test_compiler_error_publishes_nothing(self, tmp_path, fake):
        from adloop.gtm.write import draft_publish_gtm_workspace

        fake.status = {"workspaceChange": [_change("tag", "1", name="GA4", type="gaawe")]}
        fake.create_version_response = {"compilerError": True}
        cfg = _config(tmp_path)
        preview = draft_publish_gtm_workspace(cfg, **_ids())
        result = _apply(cfg, preview["plan_id"])
        assert "compiler errors" in result["error"]
        assert not any(c[0] == "publish" for c in fake.calls)


# ---------------------------------------------------------------------------
# Rollback: republish an older version
# ---------------------------------------------------------------------------

VERSIONS = f"accounts/{ACCOUNT}/containers/{CONTAINER}/versions"


def _vtag(tid, name, ttype="gaawe", event=None):
    params = [{"type": "template", "key": "eventName", "value": event}] if event else []
    return {"tagId": tid, "name": name, "type": ttype, "parameter": params,
            "fingerprint": f"fp{tid}"}


@pytest.fixture
def versions(fake):
    """Live version 41 adds a tag and changes another relative to version 40."""
    fake.live_version = {
        "path": f"{VERSIONS}/41", "containerVersionId": "41", "name": "live before",
        "fingerprint": "v41",
        "tag": [_vtag("1", "GA4 lead", event="generate_lead"),
                _vtag("2", "Broken pixel", "img")],
    }
    fake.version_bodies["40"] = {
        "path": f"{VERSIONS}/40", "containerVersionId": "40", "name": "known good",
        "fingerprint": "v40",
        "tag": [_vtag("1", "GA4 lead", event="lead")],
    }
    fake.version_bodies["41"] = copy.deepcopy(fake.live_version)
    return fake


class TestRollback:
    def test_refused_when_writes_disabled(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        with pytest.raises(RuntimeError, match="write_enabled"):
            draft_rollback_gtm_version(_config(tmp_path, write=False), **_ids(),
                                       version_id="40")
        assert versions.calls == []

    def test_blocked_operation_is_respected(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path, blocked=["gtm_rollback_version"])
        result = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        assert "blocked" in result["error"]
        assert versions.calls == []

    def test_preview_diffs_live_against_target_in_two_calls(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        preview = draft_rollback_gtm_version(_config(tmp_path), **_ids(),
                                             version_id="40")

        assert preview["requires_double_confirm"] is True
        assert preview["operation"] == "gtm_rollback_version"
        changes = preview["changes"]
        assert changes["previous_live_version_id"] == "41"
        assert changes["target_version_id"] == "40"
        diff = changes["diff_from_live"]
        assert diff["tags"]["removed"] == [
            {"id": "2", "name": "Broken pixel", "type": "img"}]
        assert diff["tags"]["changed"][0]["changes"]["parameters"] == {
            "eventName": {"from": "generate_lead", "to": "lead"}}
        assert any("Reversible" in w and "41" in w for w in preview["warnings"])
        assert any("Workspaces are not changed" in w for w in preview["warnings"])
        assert [c[0] for c in versions.calls] == ["live", "get_version"]

    def test_apply_publishes_the_pinned_target_and_is_reversible(
        self, tmp_path, versions
    ):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path)
        preview = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        versions.calls.clear()

        result = _apply(cfg, preview["plan_id"])["result"]

        assert result["published"] is True
        assert result["version_id"] == "40"
        assert result["previous_live_version_id"] == "41"
        assert result["previous_live_version_name"] == "live before"
        publishes = [c for c in versions.calls if c[0] == "publish"]
        assert publishes == [("publish", f"{VERSIONS}/40", "v40")]
        # Apply costs two calls: the live re-check and the publish.
        assert [c[0] for c in versions.calls] == ["live", "publish"]

    def test_rollback_of_the_rollback_restores_the_previous_live(
        self, tmp_path, versions
    ):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path)
        first = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        undo_id = _apply(cfg, first["plan_id"])["result"]["previous_live_version_id"]

        undo = draft_rollback_gtm_version(cfg, **_ids(), version_id=undo_id)
        assert undo["changes"]["previous_live_version_id"] == "40"
        assert _apply(cfg, undo["plan_id"])["result"]["version_id"] == "41"

    def test_someone_publishing_in_between_refuses_apply(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path)
        preview = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        versions.live_version = {**versions.live_version, "containerVersionId": "43"}

        result = _apply(cfg, preview["plan_id"])
        assert "live version is now 43" in result["error"]
        assert not any(c[0] == "publish" for c in versions.calls)

    def test_dry_run_rechecks_live_and_target_and_publishes_nothing(
        self, tmp_path, versions
    ):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path)
        preview = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        result = _apply(cfg, preview["plan_id"], dry_run=True)

        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"]["live_version_unchanged"] is True
        assert result["checks"]["target_version_unchanged"] is True
        assert "quick preview" not in result["note"]
        assert not any(c[0] == "publish" for c in versions.calls)

    def test_target_modified_after_preview_fails_the_dry_run(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path)
        preview = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        versions.version_bodies["40"]["fingerprint"] = "edited"

        result = _apply(cfg, preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "modified after the preview" in result["error"]

    def test_already_live_version_is_refused(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        result = draft_rollback_gtm_version(_config(tmp_path), **_ids(),
                                            version_id="41")
        assert "already the live version" in result["error"]

    def test_archived_version_is_refused(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        versions.version_bodies["40"]["deleted"] = True
        result = draft_rollback_gtm_version(_config(tmp_path), **_ids(),
                                            version_id="40")
        assert "archived" in result["error"]

    def test_compiler_error_reports_the_unchanged_live_version(
        self, tmp_path, versions
    ):
        from adloop.gtm.write import draft_rollback_gtm_version

        cfg = _config(tmp_path)
        preview = draft_rollback_gtm_version(cfg, **_ids(), version_id="40")
        versions.publish_response = {"compilerError": True}
        result = _apply(cfg, preview["plan_id"])
        assert "compiler errors" in result["error"]
        assert "still 41" in result["error"]

    def test_custom_html_coming_back_is_gated(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        versions.version_bodies["40"]["tag"].append(_vtag("9", "Old script", "html"))
        refused = draft_rollback_gtm_version(_config(tmp_path), **_ids(),
                                             version_id="40")
        assert refused["gate"] == "gtm.allow_custom_html"
        assert refused["custom_html_tags"][0]["name"] == "Old script"

        allowed = draft_rollback_gtm_version(_config(tmp_path, html=True), **_ids(),
                                             version_id="40")
        assert allowed["status"] == "PENDING_CONFIRMATION"
        # The gate is re-checked at apply when the flag is switched off.
        result = _apply(_config(tmp_path), allowed["plan_id"])
        assert "Custom HTML" in result["error"]
        assert not any(c[0] == "publish" for c in versions.calls)

    def test_removing_custom_html_is_not_gated(self, tmp_path, versions):
        from adloop.gtm.write import draft_rollback_gtm_version

        versions.live_version["tag"].append(_vtag("9", "New script", "html"))
        preview = draft_rollback_gtm_version(_config(tmp_path), **_ids(),
                                             version_id="40")
        assert preview["status"] == "PENDING_CONFIRMATION"


# ---------------------------------------------------------------------------
# confirm_and_apply integration: dry run = preflight, audit, errors
# ---------------------------------------------------------------------------


class TestConfirmAndApply:
    def test_dry_run_runs_preflight_and_sends_nothing(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["7"] = {"tagId": "7", "name": "A", "type": "gaawe", "fingerprint": "f1"}
        cfg = _config(tmp_path)
        preview = draft_gtm_tag(cfg, **_ids(), tag_id="7", name="B")
        result = _apply(cfg, preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_SUCCESS"
        assert result["checks"]["fingerprint_unchanged"] is True
        assert "Google Tag Manager" in result["note"]
        assert "nothing was changed live" in result["note"]
        assert fake.calls == []

    def test_dry_run_catches_stale_plan(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        fake.tags["7"] = {"tagId": "7", "name": "A", "type": "gaawe", "fingerprint": "f1"}
        cfg = _config(tmp_path)
        preview = draft_gtm_tag(cfg, **_ids(), tag_id="7", name="B")
        fake.tags["7"]["fingerprint"] = "f2"
        result = _apply(cfg, preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED"
        assert "Google Tag Manager" in result["message"]

    def test_insufficient_scope_error_is_actionable(self, tmp_path, fake):
        from adloop.gtm.write import draft_gtm_tag

        cfg = _config(tmp_path)
        preview = draft_gtm_tag(cfg, **_ids(), name="x", tag_type="gaawe",
                                firing_trigger_ids=["2147479553"])

        def boom(*a, **k):
            raise Exception("<HttpError 403: Request had insufficient authentication scopes.>")

        with patch.object(_Entities, "create", boom):
            result = _apply(cfg, preview["plan_id"])
        assert "token.json" in result["error"]

    def test_applied_write_is_audit_logged(self, tmp_path, fake):
        import json

        from adloop.gtm.write import draft_gtm_tag

        cfg = _config(tmp_path)
        preview = draft_gtm_tag(cfg, **_ids(), name="x", tag_type="gaawe",
                                firing_trigger_ids=["2147479553"])
        _apply(cfg, preview["plan_id"])
        lines = (tmp_path / "audit.log").read_text().strip().splitlines()
        entry = json.loads(lines[-1])
        assert entry["operation"] == "gtm_create_tag"
        assert entry["result"] == "success"


# ---------------------------------------------------------------------------
# Server wrappers
# ---------------------------------------------------------------------------


class TestServerRegistration:
    @pytest.mark.asyncio
    async def test_write_tools_registered_under_gtm_toolset(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("draft_gtm_tag", "draft_gtm_trigger",
                     "draft_delete_gtm_entity", "draft_publish_gtm_workspace",
                     "draft_rollback_gtm_version", "get_gtm_version_diff"):
            assert tools[name].tags == {"gtm"}, name
        assert tools["draft_delete_gtm_entity"].annotations.destructiveHint
        assert tools["draft_publish_gtm_workspace"].annotations.destructiveHint
        assert tools["draft_rollback_gtm_version"].annotations.destructive_hint
        assert tools["get_gtm_version_diff"].annotations.read_only_hint

    @pytest.mark.asyncio
    async def test_gtm_reads_take_an_optional_workspace_id(self):
        from adloop.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("list_gtm_tags", "get_gtm_tag", "list_gtm_triggers",
                     "get_gtm_trigger", "list_gtm_variables"):
            props = tools[name].parameters["properties"]
            assert "workspace_id" in props, name
            assert "workspace_id" not in tools[name].parameters.get("required", [])

    @pytest.mark.asyncio
    async def test_json_string_list_params_are_coerced(self, tmp_path, fake):
        """Issue #28: some clients send list params as JSON strings."""
        import json

        from adloop import runtime
        from adloop.server import mcp

        runtime.set_default_config(_config(tmp_path))
        try:
            result = await mcp.call_tool("draft_gtm_tag", {
                "name": "x", "tag_type": "gaawe",
                "parameters": json.dumps([{"type": "TEMPLATE", "key": "eventName",
                                           "value": "lead"}]),
                "firing_trigger_ids": json.dumps(["2147479553"]),
            })
        finally:
            runtime.set_default_config(None)
        payload = result.structured_content
        assert payload["status"] == "PENDING_CONFIRMATION", payload
        assert payload["changes"]["tag"]["firingTriggerId"] == ["2147479553"]
