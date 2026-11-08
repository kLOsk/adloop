"""Microsoft Advertising management against a stateful fake Campaign Management API.

Every write goes through draft → preview → confirm_and_apply. The fake keeps
campaigns, criteria, ad groups, keywords, ads, negatives, shared lists and
extensions in memory and applies writes to them, so the tests check both the
exact REST bodies Microsoft receives and the state that results.
"""

from __future__ import annotations

import copy
import gzip
import json
from unittest.mock import patch

import pytest

from adloop.ads import write as ads_write
from adloop.config import AdLoopConfig, MicrosoftAdsConfig, SafetyConfig
from adloop.microsoft_ads import manage, read
from adloop.microsoft_ads.auth import MicrosoftAdsApiError
from adloop.safety import preview as preview_store
from adloop.safety.preview import InMemoryPlanStore

ACCOUNT = "100200300"
CUSTOMER = "900800700"
CAMPAIGN = "500100200"


def _text(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class FakeMicrosoft:
    def __init__(self):
        self.account = {"Id": int(ACCOUNT), "Name": "Acme Plumbing", "CurrencyCode": "USD",
                        "TimeZone": "PacificTimeUSCanadaTijuana", "AccountLifeCycleStatus": "Active",
                        "ParentCustomerId": int(CUSTOMER)}
        self.campaigns = {
            CAMPAIGN: {"Id": int(CAMPAIGN), "Name": "Acme Plumbing - Search", "Status": "Active", "DailyBudget": 35.0,
                       "BudgetType": "DailyBudgetStandard", "BudgetId": None, "CampaignType": "Search",
                       "BiddingScheme": {"Type": "EnhancedCpc"}, "TrackingUrlTemplate": None, "FinalUrlSuffix": None},
            "600": {"Id": 600, "Name": "Brand", "Status": "Active", "DailyBudget": 10.0, "BudgetId": None,
                    "BiddingScheme": {"Type": "MaxConversions", "MaxCpc": {"Amount": 12.0}}},
        }
        self.criteria = {
            CAMPAIGN: [
                {"Id": 1001, "Type": "BiddableCampaignCriterion", "CampaignId": int(CAMPAIGN),
                 "Criterion": {"Type": "Location", "LocationId": 190, "DisplayName": "Springfield, Illinois"}},
                {"Id": 1002, "Type": "NegativeCampaignCriterion", "CampaignId": int(CAMPAIGN),
                 "Criterion": {"Type": "Location", "LocationId": 191, "DisplayName": "Los Angeles, California"}},
                {"Id": 1003, "Type": "BiddableCampaignCriterion", "CampaignId": int(CAMPAIGN),
                 "Criterion": {"Type": "LocationIntent", "IntentOption": "PeopleInOrSearchingForOrViewingPages"}},
            ],
            "600": [],
        }
        self.ad_groups = {
            "11": {"Id": 11, "Name": "Drains", "Status": "Active", "CpcBid": {"Amount": 8.0},
                   "Network": "OwnedAndOperatedAndSyndicatedSearch", "AudienceAdsBidAdjustment": None, "_campaign": CAMPAIGN},
            "12": {"Id": 12, "Name": "Water heaters", "Status": "Paused", "CpcBid": {"Amount": 6.0},
                   "Network": "OwnedAndOperatedOnly", "_campaign": CAMPAIGN},
            "21": {"Id": 21, "Name": "Brand terms", "Status": "Active", "CpcBid": {"Amount": 3.0}, "_campaign": "600"},
        }
        self.keywords = {
            "101": {"Id": 101, "Text": "drain cleaning", "MatchType": "Phrase", "Bid": {"Amount": None}, "Status": "Active", "_group": "11"},
            "102": {"Id": 102, "Text": "plumber", "MatchType": "Broad", "Bid": {"Amount": 9.0}, "Status": "Active", "_group": "11"},
            "103": {"Id": 103, "Text": "water heater", "MatchType": "Exact", "Bid": {"Amount": None}, "Status": "Active", "_group": "12"},
            "201": {"Id": 201, "Text": "acme plumbing", "MatchType": "Exact", "Bid": {"Amount": None}, "Status": "Active", "_group": "21"},
        }
        self.ads = {
            "301": {"Id": 301, "Type": "ResponsiveSearch", "Status": "Active", "_group": "11",
                    "Headlines": [{"Asset": {"Type": "TextAsset", "Text": t}} for t in ("Drain Cleaning", "Same Day", "Call Now")],
                    "Descriptions": [{"Asset": {"Type": "TextAsset", "Text": d}} for d in ("Licensed.", "Local.")],
                    "FinalUrls": ["https://example.com/drain"], "Path1": "drain", "Path2": None},
        }
        self.negatives = {("Campaign", CAMPAIGN): [{"Id": 501, "Text": "free", "MatchType": "Phrase"}]}
        self.lists = {"900": {"Id": 900, "Name": "Junk", "Type": "NegativeKeywordList", "ItemCount": 2}}
        self.items = {"900": [{"Id": 9001, "Type": "NegativeKeyword", "Text": "jobs", "MatchType": "Phrase"},
                              {"Id": 9002, "Type": "NegativeKeyword", "Text": "diy", "MatchType": "Phrase"}]}
        self.associations = [{"SharedEntityId": 900, "EntityId": int(CAMPAIGN)}]
        self.extensions = {
            "7001": {"Id": 7001, "Type": "SitelinkAdExtension", "DisplayText": "Drain Cleaning",
                     "FinalUrls": ["https://example.com/drain"]},
            "7002": {"Id": 7002, "Type": "CalloutAdExtension", "Text": "Licensed"},
        }
        self.ext_links = [("7001", CAMPAIGN, "Campaign"), ("7002", CAMPAIGN, "Campaign")]
        self.media: dict[str, dict] = {}
        self.goals = [{"Id": 9, "Name": "Lead", "Type": "Event", "Status": "Active", "ExcludeFromBidding": False}]
        # Accounts outside Microsoft's in-store pilot cannot ask for InStoreTransaction goals.
        self.in_store_pilot = True
        self.calls: list[dict] = []
        self.fail: dict[tuple, Exception] = {}
        # Whether Microsoft refuses one "Targets" query for every criterion type.
        self.refuse_targets_query = False
        self._next = 5000

    def _id(self) -> int:
        self._next += 1
        return self._next

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] in ("PUT", "DELETE") or c["path"] in self._write_paths]

    _write_paths = {"Campaigns", "CampaignCriterions", "AdGroups", "Keywords", "Ads", "EntityNegativeKeywords",
                    "SharedEntity", "ListItems", "SharedEntityAssociations/Set", "SharedEntityAssociations",
                    "AdExtensions", "AdExtensionsAssociations/Set", "AdExtensionsAssociations", "Media"}

    @staticmethod
    def _pub(obj: dict) -> dict:
        return {k: copy.deepcopy(v) for k, v in obj.items() if not k.startswith("_")}

    def _listed_extension(self, ext_id: str) -> dict:
        """An extension as the association query returns it: image extensions come
        without ImageMediaIds, Images, DisplayText and Layouts (checked live)."""
        ext = copy.deepcopy(self.extensions[ext_id])
        if ext.get("Type") == "ImageAdExtension":
            for field in ("Images", "DisplayText", "Layouts"):
                ext.pop(field, None)
            ext["ImageMediaIds"] = None
        return ext

    def __call__(self, config, service, path, body=None, *, method="POST", account_id="", customer_id="", mutation=False):
        body = copy.deepcopy(body or {})
        self.calls.append({"service": service, "path": path, "method": method, "body": body})
        if (method, path) in self.fail:
            raise self.fail[(method, path)]
        if service == "customer" and path == "Account/Query":
            return {"Account": dict(self.account)}
        # -------- reads
        if path == "Campaigns/QueryByAccountId":
            # Like Microsoft: optional fields come back only when requested.
            asked = {x.strip() for x in str(body.get("ReturnAdditionalFields") or "").split(",")}
            out = []
            for c in self.campaigns.values():
                c = self._pub(c)
                if "TargetCpaInMaxConversion" not in asked and isinstance(c.get("BiddingScheme"), dict):
                    c["BiddingScheme"].pop("TargetCpa", None)
                if "AdScheduleUseSearcherTimeZone" not in asked:
                    c.pop("AdScheduleUseSearcherTimeZone", None)
                out.append(c)
            return {"Campaigns": out}
        if path == "CampaignCriterions/QueryByIds":
            kind = body["CriterionType"]
            if kind == "Targets" and self.refuse_targets_query:
                raise MicrosoftAdsApiError("InvalidCriterionType", status=400, codes=["InvalidCriterionType"])
            return {"CampaignCriterions": [copy.deepcopy(c) for c in self.criteria.get(str(body["CampaignId"]), [])
                                           if kind == "Targets" or c["Criterion"]["Type"].replace("Criterion", "") == kind]}
        if path == "AdGroups/QueryByCampaignId":
            return {"AdGroups": [self._pub(g) for g in self.ad_groups.values() if g["_campaign"] == str(body["CampaignId"])]}
        if path == "Keywords/QueryByAdGroupId":
            return {"Keywords": [self._pub(k) for k in self.keywords.values() if k["_group"] == str(body["AdGroupId"])]}
        if path == "Ads/QueryByAdGroupId":
            return {"Ads": [self._pub(a) for a in self.ads.values() if a["_group"] == str(body["AdGroupId"])]}
        if path == "ConversionGoals/QueryByIds":
            if not self.in_store_pilot and "InStoreTransaction" in body["ConversionGoalTypes"]:
                raise MicrosoftAdsApiError(
                    "InStoreTransactionPilotNotEnabledForCustomer: In-Store Transaction pilot not enabled",
                    status=400, codes=["InStoreTransactionPilotNotEnabledForCustomer", "5359"])
            return {"ConversionGoals": copy.deepcopy(self.goals)}
        if path == "NegativeKeywords/QueryByEntityIds":
            key = (body["EntityType"], str(body["EntityIds"][0]))
            return {"EntityNegativeKeywords": [{"EntityId": body["EntityIds"][0], "EntityType": key[0],
                                                "NegativeKeywords": copy.deepcopy(self.negatives.get(key, []))}]}
        if path == "SharedEntities/Query":
            return {"SharedEntities": copy.deepcopy(list(self.lists.values()))}
        if path == "ListItems/QueryBySharedList":
            return {"ListItems": copy.deepcopy(self.items.get(str(body["SharedList"]["Id"]), []))}
        if path == "SharedEntityAssociations/QueryByEntityIds":
            ids = {int(i) for i in body["EntityIds"]}
            return {"Associations": [dict(a, EntityType="Campaign", SharedEntityType="NegativeKeywordList")
                                     for a in self.associations if a["EntityId"] in ids]}
        if path == "AdExtensionsAssociations/Query":
            # One collection per requested entity, at most 100 entities, like Microsoft.
            assert len(body["EntityIds"]) <= 100
            wanted = {x.strip() for x in body["AdExtensionType"].split(",")}
            collections = []
            for entity in body["EntityIds"]:
                links = [l for l in self.ext_links if l[1] == str(entity) and l[2] == body["AssociationType"]
                         and self.extensions[l[0]]["Type"] in wanted]
                collections.append({"AdExtensionAssociations": [
                    {"AdExtension": self._listed_extension(e), "AssociationType": a, "EntityId": int(o),
                     "EditorialStatus": "Active"} for e, o, a in links]} if links else None)
            return {"AdExtensionAssociationCollection": collections}
        if path == "AdExtensions/QueryByIds":
            # Like Microsoft: Images, DisplayText and Layouts only when asked for,
            # and an unknown additional field fails the whole request.
            extra = {x.strip() for x in (body.get("ReturnAdditionalFields") or "").split(",") if x.strip()}
            if extra - {"Images", "DisplayText", "Layouts", "LogoImages"}:
                raise MicrosoftAdsApiError("NullRequest: The request message is null.", status=400)
            out = []
            for i in body["AdExtensionIds"]:
                ext = self.extensions.get(str(i))
                if ext is None:
                    out.append(None)
                    continue
                shown = self._listed_extension(str(i))
                if "Images" in extra and ext["Type"] == "ImageAdExtension":
                    shown["Images"] = [{"Asset": {"Type": "ImageAsset", "Id": str(m), "SubType": "LandscapeImageMedia"}}
                                       for m in ext.get("ImageMediaIds") or []]
                for field in ("DisplayText", "Layouts"):
                    if field in extra and field in ext:
                        shown[field] = ext[field]
                out.append(shown)
            return {"AdExtensions": out, "PartialErrors": []}
        if path == "MediaMetaData/QueryByIds":
            width, height = 1200, 628
            return {"MediaMetaData": [
                {"Id": str(m), "Type": "Image", "MediaType": "GenericImage", "Representations": [
                    {"Type": "ImageMediaRepresentation", "Name": "Original", "Width": width, "Height": height,
                     "Url": f"https://www.bing.com/th?id=OADD2.{m}"}]} for m in body["MediaIds"]]}
        if path == "GeoLocationsFileUrl/Query":
            return {"FileUrl": "https://geo.example/locations.csv.gz?sig=SECRET", "LastModifiedTimeUtc": "2026-10-01T00:00:00"}
        # -------- writes
        if path == "Campaigns":
            if method == "POST":
                ids = []
                for c in body["Campaigns"]:
                    cid = self._id()
                    self.campaigns[str(cid)] = {"Id": cid, **c}
                    self.criteria[str(cid)] = []
                    ids.append(cid)
                return {"CampaignIds": ids, "PartialErrors": []}
            if method == "PUT":
                for c in body["Campaigns"]:
                    self.campaigns[str(c["Id"])].update({k: v for k, v in c.items() if k != "Id"})
                return {"PartialErrors": []}
            for cid in body["CampaignIds"]:
                self.campaigns.pop(str(cid))
            return {"PartialErrors": []}
        if path == "CampaignCriterions":
            if method == "POST":
                # Microsoft refuses overlapping DayTime windows in a campaign,
                # and more than seven on one day.
                for c in body["CampaignCriterions"]:
                    if c["Criterion"]["Type"] == "DayTimeCriterion":
                        day = c["Criterion"]["Day"]
                        on_day = sum(1 for e in self.criteria.get(str(c["CampaignId"]), [])
                                     if e["Criterion"]["Type"] == "DayTime" and e["Criterion"]["Day"] == day)
                        if on_day >= 7:
                            raise MicrosoftAdsApiError("CampaignCriterionDayTimeLimit: at most seven per day",
                                                       status=400, codes=["CampaignCriterionDayTimeLimit"])
                        self.criteria.setdefault(str(c["CampaignId"]), []).append(
                            {"Id": -1, "Criterion": {"Type": "DayTime", "Day": day}})  # counted, removed below
                self.criteria[str(body["CampaignCriterions"][0]["CampaignId"])] = [
                    e for e in self.criteria.get(str(body["CampaignCriterions"][0]["CampaignId"]), []) if e["Id"] != -1]
                for c in body["CampaignCriterions"]:
                    if c["Criterion"]["Type"] == "DayTimeCriterion":
                        span = manage._block_span(c["Criterion"])
                        for e in self.criteria.get(str(c["CampaignId"]), []):
                            if e["Criterion"]["Type"] == "DayTime":
                                other = manage._block_span(e["Criterion"])
                                if other[0] == span[0] and span[1] < other[2] and other[1] < span[2]:
                                    raise MicrosoftAdsApiError("CampaignCriterionDayTimeOverlap: day and time ranges overlap",
                                                               status=400, codes=["CampaignCriterionDayTimeOverlap"])
                ids = []
                for c in body["CampaignCriterions"]:
                    crit = copy.deepcopy(c)
                    crit["Id"] = self._id()
                    crit["Criterion"]["Type"] = crit["Criterion"]["Type"].replace("Criterion", "")
                    self.criteria.setdefault(str(c["CampaignId"]), []).append(crit)
                    ids.append(crit["Id"])
                return {"CampaignCriterionIds": ids, "PartialErrors": []}
            if method == "PUT":
                for c in body["CampaignCriterions"]:
                    for existing in self.criteria[str(c["CampaignId"])]:
                        if str(existing["Id"]) == str(c["Id"]):
                            existing["Criterion"].update({k: v for k, v in c["Criterion"].items() if k != "Type"})
                return {"PartialErrors": []}
            drop = {int(i) for i in body["CampaignCriterionIds"]}
            self.criteria[str(body["CampaignId"])] = [c for c in self.criteria[str(body["CampaignId"])] if c["Id"] not in drop]
            return {"PartialErrors": []}
        if path == "AdGroups":
            if method == "POST":
                ids = []
                for g in body["AdGroups"]:
                    gid = self._id()
                    self.ad_groups[str(gid)] = {"Id": gid, **g, "_campaign": str(body["CampaignId"])}
                    ids.append(gid)
                return {"AdGroupIds": ids, "PartialErrors": []}
            if method == "PUT":
                for g in body["AdGroups"]:
                    self.ad_groups[str(g["Id"])].update({k: v for k, v in g.items() if k != "Id"})
                return {"PartialErrors": []}
            for gid in body["AdGroupIds"]:
                self.ad_groups.pop(str(gid))
            return {"PartialErrors": []}
        if path == "Keywords":
            if method == "POST":
                ids = []
                for k in body["Keywords"]:
                    kid = self._id()
                    self.keywords[str(kid)] = {"Id": kid, **k, "_group": str(body["AdGroupId"])}
                    ids.append(kid)
                return {"KeywordIds": ids, "PartialErrors": []}
            if method == "PUT":
                for k in body["Keywords"]:
                    self.keywords[str(k["Id"])].update({x: v for x, v in k.items() if x != "Id"})
                return {"PartialErrors": []}
            for kid in body["KeywordIds"]:
                self.keywords.pop(str(kid))
            return {"PartialErrors": []}
        if path == "Ads":
            if method == "POST":
                ids = []
                for a in body["Ads"]:
                    aid = self._id()
                    self.ads[str(aid)] = {"Id": aid, **a, "_group": str(body["AdGroupId"])}
                    ids.append(aid)
                return {"AdIds": ids, "PartialErrors": []}
            if method == "PUT":
                for a in body["Ads"]:
                    self.ads[str(a["Id"])].update({k: v for k, v in a.items() if k != "Id"})
                return {"PartialErrors": []}
            for aid in body["AdIds"]:
                self.ads.pop(str(aid))
            return {"PartialErrors": []}
        if path == "EntityNegativeKeywords":
            entry = body["EntityNegativeKeywords"][0]
            key = (entry["EntityType"], str(entry["EntityId"]))
            if method == "POST":
                for n in entry["NegativeKeywords"]:
                    self.negatives.setdefault(key, []).append({"Id": self._id(), **n})
                return {"NegativeKeywordIds": [{"Ids": []}], "NestedPartialErrors": []}
            drop = {n["Id"] for n in entry["NegativeKeywords"]}
            self.negatives[key] = [n for n in self.negatives.get(key, []) if n["Id"] not in drop]
            return {"NestedPartialErrors": []}
        if path == "SharedEntity":
            lid = self._id()
            self.lists[str(lid)] = {"Id": lid, **body["SharedEntity"], "ItemCount": len(body["ListItems"])}
            self.items[str(lid)] = [{"Id": self._id(), **i} for i in body["ListItems"]]
            return {"SharedEntityId": lid, "ListItemIds": [], "PartialErrors": []}
        if path == "ListItems":
            lid = str(body["SharedList"]["Id"])
            if method == "POST":
                self.items[lid] += [{"Id": self._id(), **i} for i in body["ListItems"]]
                return {"ListItemIds": [], "PartialErrors": []}
            drop = {int(i) for i in body["ListItemIds"]}
            self.items[lid] = [i for i in self.items[lid] if i["Id"] not in drop]
            return {"PartialErrors": []}
        if path == "SharedEntityAssociations/Set":
            for a in body["Associations"]:
                self.associations.append({"SharedEntityId": a["SharedEntityId"], "EntityId": a["EntityId"]})
            return {"PartialErrors": []}
        if path == "SharedEntityAssociations" and method == "DELETE":
            drop = {(a["SharedEntityId"], a["EntityId"]) for a in body["Associations"]}
            self.associations = [a for a in self.associations if (a["SharedEntityId"], a["EntityId"]) not in drop]
            return {"PartialErrors": []}
        if path == "Media" and method == "POST":
            assert len(body["Media"]) <= 10
            ids = []
            for m in body["Media"]:
                mid = self._id()
                self.media[str(mid)] = m
                ids.append(mid)
            return {"MediaIds": ids}
        if path == "AdExtensions":
            identities = []
            for e in body["AdExtensions"]:
                eid = self._id()
                self.extensions[str(eid)] = {"Id": eid, **e}
                identities.append({"Id": eid, "Version": 1})
            return {"AdExtensionIdentities": identities, "NestedPartialErrors": []}
        if path == "AdExtensionsAssociations/Set":
            for a in body["AdExtensionIdToEntityIdAssociations"]:
                self.ext_links.append((str(a["AdExtensionId"]), str(a["EntityId"]), body["AssociationType"]))
            return {"PartialErrors": []}
        if path == "AdExtensionsAssociations" and method == "DELETE":
            drop = {(str(a["AdExtensionId"]), str(a["EntityId"]), body["AssociationType"])
                    for a in body["AdExtensionIdToEntityIdAssociations"]}
            self.ext_links = [l for l in self.ext_links if l not in drop]
            return {"PartialErrors": []}
        raise AssertionError(f"unexpected Microsoft call {method} {service} {path}")


@pytest.fixture
def api():
    fake = FakeMicrosoft()
    with patch("adloop.microsoft_ads.client.ms_request", side_effect=fake):
        yield fake


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    preview_store.set_plan_store(InMemoryPlanStore())
    read._account_cache.clear()
    read._targets_query["refused"] = False
    read._GEO_CACHE.clear()
    monkeypatch.setattr("adloop.ads.write._validate_urls", lambda urls, timeout=10: ({u: None for u in urls}, {}))
    yield
    preview_store.set_plan_store(InMemoryPlanStore())


@pytest.fixture
def config(tmp_path):
    return AdLoopConfig(
        microsoft_ads=MicrosoftAdsConfig(developer_token="dt", client_id="app", customer_id=CUSTOMER, ad_account_id=ACCOUNT),
        safety=SafetyConfig(max_daily_budget=100.0, max_bid_increase_pct=100, require_dry_run=False,
                            log_file=str(tmp_path / "audit.log")),
    )


def _apply(config, preview):
    assert "plan_id" in preview, preview
    dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
    assert dry["status"] == "DRY_RUN_SUCCESS", dry
    result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
    assert result["status"] == "APPLIED", result
    return result["result"]


def _payload(preview) -> dict:
    """What the dry run and the apply read: the plan's changes plus its apply-only payload."""
    return preview_store.get_plan(preview["plan_id"]).apply_payload()


def _approval_surfaces(config, preview) -> str:
    """Everything a person (or the audit log) sees of a plan: preview, dialog and log entries."""
    from adloop.safety.approval import confirmation_message

    from pathlib import Path

    plan = preview_store.get_plan(preview["plan_id"])
    log = Path(config.safety.log_file)
    return (_text(preview) + _text(plan.to_preview()) + confirmation_message(plan)
            + (log.read_text() if log.exists() else ""))


def _last(api, path, method=None):
    return [c for c in api.calls if c["path"] == path and (method is None or c["method"] == method)][-1]


# ---------------------------------------------------------------------------
# Campaign settings
# ---------------------------------------------------------------------------


class TestCampaignSettings:
    def test_move_to_max_conversions_with_a_target(self, api, config):
        preview = manage.update_microsoft_campaign(config, campaign_id="600", bidding_strategy="MaxConversions", target_cpa=40)
        assert preview["platform"] == "microsoft" and preview["operation"] == "microsoft_update_campaign"
        result = _apply(config, preview)
        body = _last(api, "Campaigns", "PUT")["body"]
        assert body == {"AccountId": ACCOUNT, "Campaigns": [{"Id": "600", "BiddingScheme": {
            "Type": "MaxConversions", "TargetCpa": 40.0, "MaxCpc": {"Amount": 12.0}}}]}
        assert result["updated"] == ["BiddingScheme"]
        assert api.campaigns["600"]["BiddingScheme"]["TargetCpa"] == 40.0

    def test_strategy_change_warns_and_targetcpa_maps(self, api, config):
        preview = manage.update_microsoft_campaign(config, campaign_id="600", bidding_strategy="TargetCpa", target_cpa=30)
        assert _payload(preview)["update"]["BiddingScheme"]["Type"] == "MaxConversions"
        switched = manage.update_microsoft_campaign(config, campaign_id="600", bidding_strategy="MaxClicks")
        assert any("learning period" in w for w in switched["warnings"])

    def test_manual_bidding_refused_with_broad_keywords(self, api, config):
        result = manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, bidding_strategy="ManualCpc")
        assert "broad-match" in _text(result)

    def test_max_cpc_cannot_be_added(self, api, config):
        api.campaigns["600"]["BiddingScheme"] = {"Type": "MaxConversions"}
        assert "no longer lets a campaign add a maximum CPC" in _text(
            manage.update_microsoft_campaign(config, campaign_id="600", max_cpc=5))

    def test_max_conversions_without_goals_warns(self, api, config):
        api.goals = []
        preview = manage.update_microsoft_campaign(config, campaign_id="600", target_cpa=25)
        assert any("nothing to optimize" in w for w in preview["warnings"])

    def test_tracking_template_rules(self, api, config):
        assert "must start with" in _text(manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, tracking_url_template="track.me"))
        preview = manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, tracking_url_template="{lpurl}?src=bing",
                                                   final_url_suffix="utm_source=bing&utm_medium=cpc")
        assert _payload(preview)["update"] == {"Id": CAMPAIGN, "TrackingUrlTemplate": "{lpurl}?src=bing",
                                                "FinalUrlSuffix": "utm_source=bing&utm_medium=cpc"}
        api.campaigns[CAMPAIGN]["FinalUrlSuffix"] = "utm_source=bing"
        removal = manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, final_url_suffix="")
        assert _payload(removal)["update"]["FinalUrlSuffix"] == "" and any("removed" in w for w in removal["warnings"])

    def test_name_rules_and_nothing_to_change(self, api, config):
        assert "already named" in _text(manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, name="Brand"))
        assert "Nothing to change" in manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, name="Acme Plumbing - Search")["error"]
        assert "was not found" in manage.update_microsoft_campaign(config, campaign_id="999", name="X")["error"]

    def test_edit_in_microsoft_after_the_draft_blocks_the_apply(self, api, config):
        preview = manage.update_microsoft_campaign(config, campaign_id="600", target_cpa=40)
        api.campaigns["600"]["BiddingScheme"] = {"Type": "MaxClicks"}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "BiddingScheme changed since the draft" in result["error"]
        assert not [c for c in api.calls if c["method"] == "PUT"]


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------


class TestTargeting:
    def test_location_replace_adds_before_it_removes(self, api, config):
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, location_ids=["200", "201"],
                                                             excluded_location_ids=[])
        shown = preview["changes"]["changes"]
        assert shown["locations"]["removed"] == ["Springfield, Illinois"]
        assert shown["excluded_locations"]["to"] == []
        _apply(config, preview)
        writes = [c for c in api.calls if c["path"] == "CampaignCriterions"]
        assert [c["method"] for c in writes] == ["POST", "DELETE"]
        assert writes[0]["body"]["CriterionType"] == "Targets"
        assert writes[0]["body"]["CampaignCriterions"][0] == {
            "Type": "BiddableCampaignCriterion", "CampaignId": CAMPAIGN,
            "Criterion": {"Type": "LocationCriterion", "LocationId": 200},
            "CriterionBid": {"Type": "BidMultiplier", "Multiplier": 0}}
        assert sorted(writes[1]["body"]["CampaignCriterionIds"]) == [1001, 1002]
        locations = {c["Criterion"].get("LocationId") for c in api.criteria[CAMPAIGN] if c["Criterion"]["Type"] == "Location"}
        assert locations == {200, 201}

    def test_location_guards(self, api, config):
        assert "serve everywhere" in _text(manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, location_ids=[]))
        assert "both included and excluded" in _text(manage.update_microsoft_campaign_targeting(
            config, campaign_id=CAMPAIGN, location_ids=["5"], excluded_location_ids=["5"]))
        assert "are numbers" in _text(manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, location_ids=["Springfield"]))
        assert "Nothing to change" in manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN)["error"]
        assert "already matches" in manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, location_ids=["190"])["error"]

    def test_intent_updates_the_existing_criterion(self, api, config):
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, location_intent="PeopleIn")
        _apply(config, preview)
        put = _last(api, "CampaignCriterions", "PUT")["body"]["CampaignCriterions"][0]
        assert put["Id"] == "1003" and put["Criterion"] == {"Type": "LocationIntentCriterion", "IntentOption": "PeopleIn"}
        assert "CriterionBid" not in put

    def test_intent_is_added_when_missing(self, api, config):
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id="600", location_intent="PeopleIn")
        assert _payload(preview)["operations"]["add"][0]["Criterion"]["Type"] == "LocationIntentCriterion"
        wide = manage.update_microsoft_campaign_targeting(config, campaign_id="600", location_intent="PeopleInOrSearchingForOrViewingPages")
        assert any("outside the locations" in w for w in wide["warnings"])

    def test_first_schedule_is_double_confirmed(self, api, config):
        schedule = [{"day_of_week": day, "start_hour": 6, "end_hour": 16, "end_minute": 30}
                    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")]
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=schedule)
        assert preview["requires_double_confirm"] is True
        assert preview["changes"]["changes"]["ad_schedule"]["to"][0] == "Mon 06:00–16:30"
        assert any("PacificTimeUSCanadaTijuana" in w for w in preview["warnings"])
        _apply(config, preview)
        sent = _last(api, "CampaignCriterions", "POST")["body"]["CampaignCriterions"]
        assert sent[0]["Criterion"] == {"Type": "DayTimeCriterion", "Day": "Monday", "FromHour": 6, "FromMinute": "Zero",
                                        "ToHour": 16, "ToMinute": "Thirty"}
        # Replacing the schedule deletes the old windows; [] clears it.
        cleared = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=[])
        assert cleared["requires_double_confirm"] is False
        ops = _payload(cleared)["operations"]
        assert len(ops["schedule_delete"]) == 5 and ops["schedule_add_first"] == ops["schedule_add_after"] == []

    def test_schedule_validation(self, api, config):
        bad = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=[
            {"day_of_week": "Monday", "start_hour": 8, "end_hour": 12},
            {"day_of_week": "Monday", "start_hour": 11, "end_hour": 14},
            {"day_of_week": "Funday", "start_hour": 1, "end_hour": 2},
            {"day_of_week": "Tuesday", "start_hour": 8, "end_hour": 9, "start_minute": 10},
            {"day_of_week": "Wednesday", "start_hour": 9, "end_hour": 8},
            {"day_of_week": "Thursday", "start_hour": 9, "end_hour": 24, "end_minute": 15},
        ])
        text = _text(bad)
        for fragment in ("overlap", "Funday", "minutes are 0, 15, 30 or 45", "must end after it starts", "24 only with minute 0"):
            assert fragment in text

    def test_targeting_changed_after_the_draft(self, api, config):
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, location_intent="PeopleIn")
        api.criteria[CAMPAIGN].append({"Id": 1010, "Type": "BiddableCampaignCriterion", "CampaignId": int(CAMPAIGN),
                                       "Criterion": {"Type": "Location", "LocationId": 300, "DisplayName": "Davis"}})
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_FAILED" and "targeting changed" in dry["error"]


# ---------------------------------------------------------------------------
# New campaign
# ---------------------------------------------------------------------------


class TestNewCampaign:
    def test_created_paused_then_targeted(self, api, config):
        preview = manage.draft_microsoft_campaign(config, name="Springfield Water Heaters", daily_budget=40, target_cpa=60,
                                                  languages=["English"], location_ids=["190"], excluded_location_ids=["191"])
        changes = preview["changes"]
        assert changes["status_on_create"] == "Paused" and changes["monthly_maximum"] == 1216.0
        assert any("5× the target CPA" in w for w in preview["warnings"])
        result = _apply(config, preview)
        campaign = _last(api, "Campaigns", "POST")["body"]["Campaigns"][0]
        assert campaign == {"Name": "Springfield Water Heaters", "BudgetType": "DailyBudgetStandard", "DailyBudget": 40.0,
                            "CampaignType": "Search", "Languages": ["English"], "Status": "Paused",
                            "TimeZone": "PacificTimeUSCanadaTijuana",
                            "BiddingScheme": {"Type": "MaxConversions", "TargetCpa": 60.0}}
        criteria = _last(api, "CampaignCriterions", "POST")["body"]["CampaignCriterions"]
        assert {c["CampaignId"] for c in criteria} == {result["campaign_id"]}
        assert [c["Type"] for c in criteria] == ["BiddableCampaignCriterion", "NegativeCampaignCriterion", "BiddableCampaignCriterion"]
        assert criteria[2]["Criterion"] == {"Type": "LocationIntentCriterion", "IntentOption": "PeopleIn"}

    def test_guards(self, api, config):
        no_lang = manage.draft_microsoft_campaign(config, name="X", daily_budget=10, location_ids=["190"])
        assert "languages is required" in _text(no_lang)
        no_geo = manage.draft_microsoft_campaign(config, name="X", daily_budget=10, languages=["English"])
        assert "location_ids is required" in _text(no_geo)
        assert "exceeds maximum" in manage.draft_microsoft_campaign(config, name="X", daily_budget=500, languages=["English"], location_ids=["190"])["error"]
        assert "already exists" in _text(manage.draft_microsoft_campaign(config, name="brand", daily_budget=10, languages=["English"], location_ids=["190"]))
        every = manage.draft_microsoft_campaign(config, name="X", daily_budget=10, languages=["All"], location_ids=["190"])
        assert "reaches every language" in _text(every)
        manual_target = manage.draft_microsoft_campaign(config, name="X", daily_budget=10, languages=["English"],
                                                        location_ids=["190"], bidding_strategy="ManualCpc", target_cpa=30)
        assert "target_cpa goes with MaxConversions" in _text(manual_target)

    def test_conversion_bidding_without_a_goal_is_refused_at_the_draft(self, api, config):
        # Seen live: the dry run passed, then Microsoft refused the create with
        # ConversionGoalCriteriaNotMetForBiddingScheme.
        api.goals = []
        refused = manage.draft_microsoft_campaign(config, name="New", daily_budget=10, languages=["English"],
                                                  location_ids=["190"])
        assert "plan_id" not in refused and "needs an active conversion goal" in _text(refused)
        assert "MaxClicks, EnhancedCpc or ManualCpc" in _text(refused)
        clicks = manage.draft_microsoft_campaign(config, name="New", daily_budget=10, languages=["English"],
                                                 location_ids=["190"], bidding_strategy="MaxClicks")
        assert "plan_id" in clicks

    def test_a_goal_removed_after_the_draft_fails_the_dry_run(self, api, config):
        preview = manage.draft_microsoft_campaign(config, name="New", daily_budget=10, languages=["English"],
                                                  location_ids=["190"])
        api.goals = []
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_FAILED" and "needs an active conversion goal" in dry["error"]
        assert not api.writes()

    def test_the_audience_network_default_is_named(self, api, config):
        preview = manage.draft_microsoft_campaign(config, name="New", daily_budget=10, languages=["English"],
                                                  location_ids=["190"])
        assert any("Microsoft Audience Network" in w and "-100" in w for w in preview["warnings"])

    def test_failed_targeting_step_names_the_created_campaign_and_retry_is_refused(self, api, config):
        preview = manage.draft_microsoft_campaign(config, name="Half done", daily_budget=10, languages=["English"], location_ids=["190"])
        api.fail[("POST", "CampaignCriterions")] = MicrosoftAdsApiError("Microsoft refused: InvalidLocationId", status=400)
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "PARTIAL_APPLY"
        assert "created campaign" in result["error"] and "(paused)" in result["error"] and "InvalidLocationId" in result["error"]
        del api.fail[("POST", "CampaignCriterions")]
        # The plan is retired: it cannot be replayed into a second campaign.
        retry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "No pending plan" in retry["error"]
        assert sum(1 for c in api.campaigns.values() if c["Name"] == "Half done") == 1
        # A fresh draft of the same campaign is refused by name.
        assert "already exists" in _text(manage.draft_microsoft_campaign(
            config, name="Half done", daily_budget=10, languages=["English"], location_ids=["190"]))


# ---------------------------------------------------------------------------
# Ad groups and keywords
# ---------------------------------------------------------------------------


class TestAdGroupsAndKeywords:
    def test_ad_group_with_keywords(self, api, config):
        preview = manage.draft_microsoft_ad_group(config, campaign_id=CAMPAIGN, name="Sewer", cpc_bid=7.5,
                                                  keywords=["sewer repair", {"text": "sewer line", "match_type": "exact", "bid": 9}])
        result = _apply(config, preview)
        group = _last(api, "AdGroups", "POST")["body"]
        assert group == {"CampaignId": CAMPAIGN, "AdGroups": [{"Name": "Sewer", "Network": "OwnedAndOperatedOnly",
                                                              "Status": "Active", "CpcBid": {"Amount": 7.5}}]}
        keywords = _last(api, "Keywords", "POST")["body"]
        assert keywords["AdGroupId"] == result["ad_group_id"]
        assert keywords["Keywords"] == [{"Text": "sewer repair", "MatchType": "Phrase", "Status": "Active"},
                                        {"Text": "sewer line", "MatchType": "Exact", "Status": "Active", "Bid": {"Amount": 9.0}}]

    def test_ad_group_guards(self, api, config):
        assert "needs cpc_bid" in _text(manage.draft_microsoft_ad_group(config, campaign_id=CAMPAIGN, name="No bid"))
        assert "Broad match" in _text(manage.draft_microsoft_ad_group(config, campaign_id=CAMPAIGN, name="B", cpc_bid=5,
                                                                    keywords=[{"text": "plumbing", "match_type": "Broad"}]))
        assert "already has an ad group" in _text(manage.draft_microsoft_ad_group(config, campaign_id=CAMPAIGN, name="drains", cpc_bid=5))
        ok = manage.draft_microsoft_ad_group(config, campaign_id="600", name="Auto", keywords=[{"text": "acme", "match_type": "Broad"}])
        assert "plan_id" in ok  # automated bidding allows broad and needs no bid
        partners = manage.draft_microsoft_ad_group(config, campaign_id="600", name="Partners", network="OwnedAndOperatedAndSyndicatedSearch")
        assert any("Syndicated" in w for w in partners["warnings"])

    def test_ad_group_update(self, api, config):
        preview = manage.update_microsoft_ad_group(config, ad_group_id="11", cpc_bid=10, network="OwnedAndOperatedOnly",
                                                   audience_ads_bid_adjustment=-100)
        assert preview["changes"]["changes"]["audience_network"]["meaning"] == "off"
        _apply(config, preview)
        body = _last(api, "AdGroups", "PUT")["body"]
        assert body == {"CampaignId": CAMPAIGN, "UpdateAudienceAdsBidAdjustment": True, "AdGroups": [
            {"Id": "11", "CpcBid": {"Amount": 10.0}, "Network": "OwnedAndOperatedOnly", "AudienceAdsBidAdjustment": -100}]}
        assert "exceeds maximum" in manage.update_microsoft_ad_group(config, ad_group_id="11", cpc_bid=30)["error"]
        auto = manage.update_microsoft_ad_group(config, ad_group_id="21", cpc_bid=4)
        assert any("ignores the ad group's default bid" in w for w in auto["warnings"])

    def test_keywords_skip_duplicates_and_refuse_broad_on_manual(self, api, config):
        preview = manage.draft_microsoft_keywords(config, ad_group_id="11", keywords=[
            "drain cleaning", {"text": "Drain Cleaning", "match_type": "Exact"}, "clogged drain", "clogged drain"])
        assert any("already in the ad group" in w for w in preview["warnings"])
        _apply(config, preview)
        sent = _last(api, "Keywords", "POST")["body"]["Keywords"]
        assert [(k["Text"], k["MatchType"]) for k in sent] == [("Drain Cleaning", "Exact"), ("clogged drain", "Phrase")]
        assert "already in the ad group" in manage.draft_microsoft_keywords(config, ad_group_id="11", keywords=["drain cleaning"])["error"]
        assert "Broad match" in _text(manage.draft_microsoft_keywords(config, ad_group_id="11", keywords=[{"text": "x", "match_type": "broad"}]))
        assert "1–100" in _text(manage.draft_microsoft_keywords(config, ad_group_id="11", keywords=["k" * 101]))
        paused = manage.draft_microsoft_keywords(config, ad_group_id="12", keywords=["tankless"])
        assert any("Paused" in w for w in paused["warnings"])

    def test_keyword_bids(self, api, config):
        preview = manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[
            {"keyword_id": "101", "bid": 12}, {"keyword_id": "102", "bid": None}])
        shown = {b["keyword"]: b for b in preview["changes"]["bids"]}
        assert shown["drain cleaning [Phrase]"]["from"] == 8.0  # ad group default applies today
        assert shown["plumber [Broad]"]["to"] == "ad group default 8.0"
        _apply(config, preview)
        assert _last(api, "Keywords", "PUT")["body"] == {"AdGroupId": "11", "Keywords": [
            {"Id": "101", "Bid": {"Amount": 12.0}}, {"Id": "102", "Bid": {"Amount": None}}]}
        assert "exceeds maximum" in manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[{"keyword_id": "101", "bid": 40}])["error"]
        assert "not in ad group" in _text(manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[{"keyword_id": "103", "bid": 5}]))

    def test_keyword_bid_changed_after_the_draft(self, api, config):
        preview = manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[{"keyword_id": "102", "bid": 10}])
        api.keywords["102"]["Bid"] = {"Amount": 11.0}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "bid changed since the draft" in result["error"]


# ---------------------------------------------------------------------------
# Negative keywords
# ---------------------------------------------------------------------------


class TestNegatives:
    def test_read_lists_direct_and_shared(self, api, config):
        result = manage.get_microsoft_negative_keywords(config)
        assert result["direct_negatives"][0]["remove_id"] == f"Campaign~{CAMPAIGN}~501"
        junk = result["negative_keyword_lists"][0]
        assert junk["attached_to"] == ["Acme Plumbing - Search"] and junk["keywords"][0]["remove_id"] == "900~9001"
        assert any("shared list keeps them reusable" in i for i in result["insights"])

    def test_direct_negatives_dedupe_and_flag_conflicts(self, api, config):
        preview = manage.add_microsoft_negative_keywords(config, campaign_id=CAMPAIGN, keywords=["free", "plumber", "careers"])
        assert preview["changes"]["negatives"] == ["plumber [Phrase]", "careers [Phrase]"]
        assert any("block active keywords" in w and "plumber" in w for w in preview["warnings"])
        _apply(config, preview)
        assert _last(api, "EntityNegativeKeywords", "POST")["body"] == {"EntityNegativeKeywords": [
            {"EntityId": int(CAMPAIGN), "EntityType": "Campaign",
             "NegativeKeywords": [{"Text": "plumber", "MatchType": "Phrase"}, {"Text": "careers", "MatchType": "Phrase"}]}]}
        assert "exactly one" in _text(manage.add_microsoft_negative_keywords(config, keywords=["x"]))
        assert "Broad" not in manage.NEGATIVE_MATCH_TYPES
        assert "match_type for negatives" in _text(manage.add_microsoft_negative_keywords(config, campaign_id=CAMPAIGN, keywords=["x"], match_type="Broad"))

    def test_new_list_is_created_then_attached(self, api, config):
        preview = manage.propose_microsoft_negative_keyword_list(config, list_name="Jobs and DIY", keywords=["hiring", "apprentice"],
                                                                 campaign_ids=[CAMPAIGN, "600"])
        result = _apply(config, preview)
        create = _last(api, "SharedEntity")["body"]
        assert create == {"SharedEntity": {"Type": "NegativeKeywordList", "Name": "Jobs and DIY"}, "SharedEntityScope": "Account",
                          "ListItems": [{"Type": "NegativeKeyword", "Text": "hiring", "MatchType": "Phrase"},
                                        {"Type": "NegativeKeyword", "Text": "apprentice", "MatchType": "Phrase"}]}
        attach = _last(api, "SharedEntityAssociations/Set")["body"]
        assert attach["Associations"][0] == {"EntityId": int(CAMPAIGN), "EntityType": "Campaign",
                                             "SharedEntityId": int(result["list_id"]), "SharedEntityType": "NegativeKeywordList"}

    def test_list_guards_and_partial_failure(self, api, config):
        assert "already exists" in _text(manage.propose_microsoft_negative_keyword_list(config, list_name="junk", keywords=["x"]))
        api.associations += [{"SharedEntityId": 1000 + i, "EntityId": 600} for i in range(20)]
        assert "20 lists" in _text(manage.propose_microsoft_negative_keyword_list(config, list_name="More", keywords=["x"], campaign_ids=["600"]))
        unattached = manage.propose_microsoft_negative_keyword_list(config, list_name="Loose", keywords=["x"])
        assert any("blocks nothing until attached" in w for w in unattached["warnings"])
        preview = manage.propose_microsoft_negative_keyword_list(config, list_name="Halfway", keywords=["x"], campaign_ids=[CAMPAIGN])
        api.fail[("POST", "SharedEntityAssociations/Set")] = MicrosoftAdsApiError("boom", status=400)
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "PARTIAL_APPLY" and "created list" in result["error"] and "boom" in result["error"]
        del api.fail[("POST", "SharedEntityAssociations/Set")]
        assert "No pending plan" in ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)["error"]

    def test_append_to_list_skips_existing(self, api, config):
        preview = manage.add_to_microsoft_negative_keyword_list(config, list_id="900", keywords=["jobs", "salary"], match_type="Exact")
        assert preview["changes"]["negatives"] == ["jobs [Exact]", "salary [Exact]"]  # Exact differs from the Phrase item
        _apply(config, preview)
        body = _last(api, "ListItems", "POST")["body"]
        assert body["SharedList"] == {"Type": "NegativeKeywordList", "Id": 900}
        assert "already in the list" in manage.add_to_microsoft_negative_keyword_list(config, list_id="900", keywords=["jobs"])["error"]
        assert "was not found" in manage.add_to_microsoft_negative_keyword_list(config, list_id="1", keywords=["x"])["error"]

    def test_attach_and_detach(self, api, config):
        attach = manage.attach_microsoft_negative_keyword_list(config, list_id="900", campaign_ids=[CAMPAIGN, "600"])
        assert _payload(attach)["campaign_ids"] == ["600"]
        _apply(config, attach)
        detach = manage.detach_microsoft_negative_keyword_list(config, list_id="900", campaign_ids=[CAMPAIGN])
        _apply(config, detach)
        assert _last(api, "SharedEntityAssociations", "DELETE")["body"]["Associations"][0]["EntityId"] == int(CAMPAIGN)
        assert {a["EntityId"] for a in api.associations if a["SharedEntityId"] == 900} == {600}
        assert "already detached" in manage.detach_microsoft_negative_keyword_list(config, list_id="900", campaign_ids=[CAMPAIGN])["error"]


# ---------------------------------------------------------------------------
# Responsive search ads
# ---------------------------------------------------------------------------


_HEADLINES = ["Drain Cleaning From $69", "Same-Day Plumbers", "Licensed & Insured", "Springfield Drain Experts",
              "Upfront Pricing", "Call Acme Plumbing", "Camera Inspections", "Local Since 2008"]


class TestResponsiveSearchAds:
    def test_create_paused_with_pins_normalised(self, api, config):
        heads = [{"text": _HEADLINES[0], "pinned_field": "HEADLINE_1"}] + _HEADLINES[1:]
        preview = manage.draft_microsoft_responsive_search_ad(config, ad_group_id="11", headlines=heads,
                                                              descriptions=["Licensed plumbers, upfront prices.", "Book online today."],
                                                              final_url="https://example.com/drain", path1="drain", path2="cleaning")
        assert any("pinned" in w for w in preview["warnings"])
        _apply(config, preview)
        ad = _last(api, "Ads", "POST")["body"]["Ads"][0]
        assert ad["Type"] == "ResponsiveSearch" and ad["Status"] == "Paused"
        assert ad["Headlines"][0] == {"Asset": {"Type": "TextAsset", "Text": _HEADLINES[0]}, "PinnedField": "Headline1"}
        assert ad["Descriptions"][1] == {"Asset": {"Type": "TextAsset", "Text": "Book online today."}}
        assert ad["FinalUrls"] == ["https://example.com/drain"] and (ad["Path1"], ad["Path2"]) == ("drain", "cleaning")

    def test_validation(self, api, config):
        bad = manage.draft_microsoft_responsive_search_ad(
            config, ad_group_id="11", headlines=["x" * 31, "Same", "Same"], descriptions=["only one"],
            final_url="https://example.com/drain", path1="a/b")
        text = _text(bad)
        for fragment in ("1–30 characters", "repeat the same text", "2–4 descriptions", "without '/'"):
            assert fragment in text
        unfillable = manage.draft_microsoft_responsive_search_ad(
            config, ad_group_id="11", headlines=[{"text": h, "pinned_field": "HEADLINE_1"} for h in _HEADLINES[:3]],
            descriptions=["One.", "Two."], final_url="https://example.com/drain")
        assert "Nothing can fill Headline2" in _text(unfillable)
        assert "path2 needs path1" in _text(manage.draft_microsoft_responsive_search_ad(
            config, ad_group_id="11", headlines=_HEADLINES[:3], descriptions=["One.", "Two."],
            final_url="https://example.com/drain", path2="x"))

    def test_unreachable_landing_page(self, api, config, monkeypatch):
        monkeypatch.setattr("adloop.ads.write._validate_urls", lambda urls, timeout=10: ({u: "HTTP 404" for u in urls}, {}))
        result = manage.draft_microsoft_responsive_search_ad(config, ad_group_id="11", headlines=_HEADLINES[:3],
                                                             descriptions=["One.", "Two."], final_url="https://example.com/gone")
        assert "not reachable" in _text(result)

    def test_identical_ad_is_refused_at_dry_run(self, api, config):
        preview = manage.draft_microsoft_responsive_search_ad(config, ad_group_id="11", headlines=["Drain Cleaning", "Same Day", "Call Now"],
                                                              descriptions=["Licensed.", "Local."], final_url="https://example.com/drain")
        dry = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_FAILED" and "already has an RSA" in dry["error"]

    def test_update_replaces_only_the_lists_given(self, api, config):
        preview = manage.update_microsoft_responsive_search_ad(config, ad_id="301", headlines=_HEADLINES[:4], path2="")
        assert preview["changes"]["changes"]["headlines"]["from"] == ["Drain Cleaning", "Same Day", "Call Now"]
        assert any("editorial review" in w for w in preview["warnings"])
        _apply(config, preview)
        update = _last(api, "Ads", "PUT")["body"]
        assert update["AdGroupId"] == "11" and set(update["Ads"][0]) == {"Type", "Id", "Headlines", "Path2"}
        assert update["Ads"][0]["Path2"] == ""
        assert "Nothing to change" in manage.update_microsoft_responsive_search_ad(config, ad_id="301")["error"]


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


class TestExtensions:
    def test_sitelinks_created_and_linked_to_the_campaign(self, api, config):
        preview = manage.draft_microsoft_sitelinks(config, campaign_id=CAMPAIGN, sitelinks=[
            {"link_text": "Drain Cleaning", "final_url": "https://example.com/drain"},  # already linked
            {"link_text": "Water Heaters", "final_url": "https://example.com/water-heaters",
             "description1": "Same-day installs", "description2": "Tank and tankless"},
        ])
        joined = " ".join(preview["warnings"])
        assert "Skipped 1" in joined and "Four or more" in joined
        result = _apply(config, preview)
        created = _last(api, "AdExtensions")["body"]
        assert created == {"AccountId": ACCOUNT, "AdExtensions": [{
            "Type": "SitelinkAdExtension", "DisplayText": "Water Heaters", "FinalUrls": ["https://example.com/water-heaters"],
            "Description1": "Same-day installs", "Description2": "Tank and tankless"}]}
        link = _last(api, "AdExtensionsAssociations/Set")["body"]
        assert link["AssociationType"] == "Campaign"
        assert link["AdExtensionIdToEntityIdAssociations"] == [{"AdExtensionId": int(result["extension_ids"][0]), "EntityId": int(CAMPAIGN)}]

    def test_sitelink_rules(self, api, config):
        bad = manage.draft_microsoft_sitelinks(config, campaign_id=CAMPAIGN, sitelinks=[
            {"link_text": "A very long sitelink text here", "final_url": "https://x.com", "description1": "d"},
        ])
        text = _text(bad)
        assert "1–25 characters" in text and "both descriptions or neither" in text

    def test_account_scope_links_to_the_account(self, api, config):
        preview = manage.draft_microsoft_callouts(config, scope="account", callouts=["Licensed", "Upfront Pricing"])
        _apply(config, preview)
        link = _last(api, "AdExtensionsAssociations/Set")["body"]
        assert link["AssociationType"] == "Account" and link["AdExtensionIdToEntityIdAssociations"][0]["EntityId"] == int(ACCOUNT)
        assert "not both" in _text(manage.draft_microsoft_callouts(config, campaign_id=CAMPAIGN, scope="account", callouts=["x"]))

    def test_callout_and_snippet_rules(self, api, config):
        assert "longer than 25" in _text(manage.draft_microsoft_callouts(config, campaign_id=CAMPAIGN, callouts=["x" * 26]))
        one = manage.draft_microsoft_callouts(config, campaign_id="600", callouts=["24/5 Service"])
        assert any("at least 2 callouts" in w for w in one["warnings"])
        assert "already on" in manage.draft_microsoft_callouts(config, campaign_id=CAMPAIGN, callouts=["Licensed"])["error"]
        assert "3–10 values" in _text(manage.draft_microsoft_structured_snippet(config, campaign_id=CAMPAIGN, header="Services", values=["a", "b"]))
        snippet = manage.draft_microsoft_structured_snippet(config, campaign_id=CAMPAIGN, header="Services",
                                                            values=["Drain Cleaning", "Water Heaters", "Repiping"])
        assert _payload(snippet)["extensions"][0] == {"Type": "StructuredSnippetAdExtension", "Header": "Services",
                                                       "Values": ["Drain Cleaning", "Water Heaters", "Repiping"]}

    def test_one_call_extension_per_campaign(self, api, config):
        preview = manage.draft_microsoft_call_extension(config, campaign_id=CAMPAIGN, phone_number="(217) 555-0100", country_code="us")
        assert _payload(preview)["extensions"][0] == {"Type": "CallAdExtension", "PhoneNumber": "(217) 555-0100",
                                                       "CountryCode": "US", "IsCallOnly": False}
        _apply(config, preview)
        again = manage.draft_microsoft_call_extension(config, campaign_id=CAMPAIGN, phone_number="(217) 555-0199", country_code="US")
        assert "already has a call extension" in _text(again)
        assert "two-letter" in _text(manage.draft_microsoft_call_extension(config, campaign_id="600", phone_number="1", country_code="USA"))

    def test_read_extensions(self, api, config):
        result = manage.get_microsoft_ad_extensions(config)
        rows = {r["content"]: r for r in result["extensions"]}
        assert rows["Drain Cleaning"]["type"] == "Sitelink" and rows["Drain Cleaning"]["remove_id"] == f"7001~{CAMPAIGN}~Campaign"
        assert any("'Brand' has no sitelinks" in i for i in result["insights"])


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


class TestRemoval:
    @pytest.mark.parametrize("entity_type,entity_id,path,body", [
        ("keyword", "102", "Keywords", {"AdGroupId": "11", "KeywordIds": ["102"]}),
        ("ad", "301", "Ads", {"AdGroupId": "11", "AdIds": ["301"]}),
        ("ad_group", "12", "AdGroups", {"CampaignId": CAMPAIGN, "AdGroupIds": ["12"]}),
        ("campaign", "600", "Campaigns", {"AccountId": ACCOUNT, "CampaignIds": ["600"]}),
        ("negative_keyword", f"Campaign~{CAMPAIGN}~501", "EntityNegativeKeywords", {"EntityNegativeKeywords": [
            {"EntityId": int(CAMPAIGN), "EntityType": "Campaign", "NegativeKeywords": [{"Id": 501}]}]}),
        ("negative_list_item", "900~9001", "ListItems", {"ListItemIds": [9001], "SharedList": {"Type": "NegativeKeywordList", "Id": 900},
                                                         "SharedEntityScope": "Account"}),
        ("campaign_criterion", f"{CAMPAIGN}~1002", "CampaignCriterions", {"CampaignCriterionIds": [1002], "CampaignId": int(CAMPAIGN),
                                                                         "CriterionType": "Targets"}),
        ("extension_association", f"7001~{CAMPAIGN}~Campaign", "AdExtensionsAssociations", {
            "AccountId": ACCOUNT, "AssociationType": "Campaign",
            "AdExtensionIdToEntityIdAssociations": [{"AdExtensionId": 7001, "EntityId": int(CAMPAIGN)}]}),
    ])
    def test_each_removal_sends_its_delete(self, api, config, entity_type, entity_id, path, body):
        preview = manage.remove_microsoft_entity(config, entity_type=entity_type, entity_id=entity_id)
        assert preview["requires_double_confirm"] is True and preview["operation"] == "microsoft_remove_entity"
        _apply(config, preview)
        call = _last(api, path, "DELETE")
        assert call["body"] == body

    def test_bad_ids(self, api, config):
        assert "entity_id format" in _text(manage.remove_microsoft_entity(config, entity_type="negative_keyword", entity_id="501"))
        assert "was not found" in manage.remove_microsoft_entity(config, entity_type="keyword", entity_id="999")["error"]
        assert "entity_type must be one of" in _text(manage.remove_microsoft_entity(config, entity_type="account", entity_id="1"))

    def test_entity_gone_before_apply(self, api, config):
        preview = manage.remove_microsoft_entity(config, entity_type="keyword", entity_id="102")
        api.keywords.pop("102")
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert "no longer exists" in result["error"]


# ---------------------------------------------------------------------------
# Location search and the gate
# ---------------------------------------------------------------------------


_GEO_CSV = (
    "Location Id,Bing Display Name,Location Type,Status\n"
    "190,\"Springfield, Illinois, United States\",City,Active\n"
    "191,\"Springfield County, California, United States\",County,Active\n"
    "192,\"West Springfield, Illinois, United States\",City,PendingDeprecation\n"
    "200,\"Davis, California, United States\",City,Active\n"
).encode()


class _File:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


class TestLocationsAndGate:
    def test_search_downloads_once_and_ranks(self, api, config, tmp_path, monkeypatch):
        monkeypatch.setattr(read, "_geo_cache_path", lambda: tmp_path / "geo.csv")
        with patch("requests.get", return_value=_File(gzip.compress(_GEO_CSV))) as get:
            first = read.search_microsoft_locations(config, query="springfield")
            read.search_microsoft_locations(config, query="davis")
        assert get.call_count == 1
        names = [r["name"] for r in first["locations"]]
        assert names[0].startswith("Springfield,") and names[-1].startswith("West Springfield")
        assert first["locations"][0]["location_id"] == "190"
        cities = read.search_microsoft_locations(config, query="springfield", location_type="county")
        assert [r["location_id"] for r in cities["locations"]] == ["191"]
        # A fresh process reads the cached file instead of downloading again.
        read._GEO_CACHE.clear()
        with patch("requests.get", side_effect=AssertionError("no download")):
            assert read.search_microsoft_locations(config, query="davis")["total_matches"] == 1

    def test_download_error_hides_the_link(self, api, config, tmp_path, monkeypatch):
        import requests

        monkeypatch.setattr(read, "_geo_cache_path", lambda: tmp_path / "geo.csv")
        with patch("requests.get", side_effect=requests.ConnectionError("https://geo.example/locations.csv.gz?sig=SECRET")):
            with pytest.raises(MicrosoftAdsApiError) as err:
                read.search_microsoft_locations(config, query="davis")
        assert "SECRET" not in str(err.value)

    def test_two_phase_apply_and_blocked_operations(self, api, config):
        config.safety.two_phase_apply = True
        preview = manage.draft_microsoft_callouts(config, campaign_id="600", callouts=["Licensed", "Insured"])
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)["status"] == "DRY_RUN_REQUIRED"
        assert not [c for c in api.calls if c["path"] == "AdExtensions"]
        config.safety.blocked_operations = ["microsoft_remove_entity"]
        assert "blocked" in manage.remove_microsoft_entity(config, entity_type="keyword", entity_id="101")["error"]

    def test_server_tools_return_previews(self, api, config):
        from adloop import runtime, server

        runtime.set_default_config(config)
        try:
            preview = server.draft_microsoft_keywords(ad_group_id="11", keywords=["clogged drain"])
            assert preview["status"] == "PENDING_CONFIRMATION" and preview["platform"] == "microsoft"
            assert server.get_microsoft_negative_keywords()["negative_keyword_lists"][0]["name"] == "Junk"
        finally:
            runtime.set_default_config(None)


class TestRoundThreeFindings:
    """Codex review round three (adloop-audit-2026-11-04.md), Microsoft findings 4–7."""

    def _no_put(self, api, path):
        return not [c for c in api.calls if c["path"] == path and c["method"] in ("PUT", "POST")]

    # -- finding 4: effective bids, at draft and at apply
    def test_cap_lowered_after_the_dry_run_refuses_the_bid_raise(self, api, config):
        preview = manage.update_microsoft_ad_group(config, ad_group_id="11", cpc_bid=12)
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        config.safety.max_bid_increase_pct = 0
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "exceeds maximum" in _text(result)
        assert self._no_put(api, "AdGroups") and api.ad_groups["11"]["CpcBid"] == {"Amount": 8.0}

    def test_keyword_bid_raise_rechecked_against_the_cap_at_apply(self, api, config):
        preview = manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[{"keyword_id": "102", "bid": 12}])
        config.safety.max_bid_increase_pct = 10
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "exceeds maximum" in _text(result)
        assert self._no_put(api, "Keywords")

    def test_removing_an_override_is_capped_by_the_inherited_bid(self, api, config):
        # A $1 keyword bid removed inherits the $8 default: an 700% raise.
        api.keywords["102"]["Bid"] = {"Amount": 1.0}
        result = manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[{"keyword_id": "102", "bid": None}])
        assert "plan_id" not in result and "exceeds maximum" in _text(result)

    def test_default_bid_changed_after_the_preview_refuses_the_inheriting_keyword(self, api, config):
        preview = manage.update_microsoft_keyword_bids(config, ad_group_id="11", bids=[{"keyword_id": "102", "bid": None}])
        assert "plan_id" in preview  # $9 → inherits $8
        api.ad_groups["11"]["CpcBid"] = {"Amount": 80.0}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "default bid changed since the draft" in _text(result)
        assert self._no_put(api, "Keywords") and api.keywords["102"]["Bid"] == {"Amount": 9.0}

    # -- finding 5: broad match re-checked at apply
    def test_campaign_switched_to_manual_after_the_dry_run_refuses_broad_keywords(self, api, config):
        preview = manage.draft_microsoft_keywords(config, ad_group_id="21", keywords=[{"text": "acme plumbers", "match_type": "Broad"}])
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        api.campaigns["600"]["BiddingScheme"] = {"Type": "ManualCpc"}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "Broad match" in _text(result)
        assert self._no_put(api, "Keywords")

    def test_campaign_switched_to_manual_refuses_a_new_ad_group_with_broad_keywords(self, api, config):
        preview = manage.draft_microsoft_ad_group(config, campaign_id="600", name="Auto",
                                                  keywords=[{"text": "acme", "match_type": "Broad"}])
        api.campaigns["600"]["BiddingScheme"] = {"Type": "ManualCpc"}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED"
        assert "Broad match" in _text(result) and "needs a default bid" in _text(result)
        assert self._no_put(api, "AdGroups")

    def test_broad_keyword_added_before_a_pending_manual_switch_refuses_it(self, api, config):
        preview = manage.update_microsoft_campaign(config, campaign_id="600", bidding_strategy="ManualCpc")
        assert "plan_id" in preview, preview
        api.keywords["299"] = {"Id": 299, "Text": "acme plumbers", "MatchType": "Broad", "Bid": {"Amount": None},
                               "Status": "Active", "_group": "21"}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "broad-match" in _text(result)
        assert not [c for c in api.calls if c["path"] == "Campaigns" and c["method"] == "PUT"]

    # -- finding 6: every bid and line of copy is on the approval surfaces
    def test_dialog_shows_keyword_bid_overrides(self, api, config):
        from adloop.safety.approval import confirmation_message

        preview = manage.draft_microsoft_ad_group(config, campaign_id=CAMPAIGN, name="Sewer", cpc_bid=5,
                                                  keywords=[{"text": "sewer line", "match_type": "exact", "bid": 25}])
        plan = preview_store.get_plan(preview["plan_id"])
        assert "sewer line [Exact] bid 25.0" in _text(plan.changes) and "keyword_rows" not in _text(plan.changes)
        assert "bid 25.0" in confirmation_message(plan)
        assert plan.apply_only_payload["keyword_rows"][0]["Bid"] == {"Amount": 25.0}

    def test_dialog_shows_sitelink_descriptions(self, api, config):
        from adloop.safety.approval import confirmation_message

        preview = manage.draft_microsoft_sitelinks(config, campaign_id="600", sitelinks=[
            {"link_text": "Water Heaters", "final_url": "https://example.com/water-heaters",
             "description1": "Same-day installs", "description2": "Tank and tankless"}])
        plan = preview_store.get_plan(preview["plan_id"])
        assert "Same-day installs / Tank and tankless" in _text(plan.changes) and '"extensions"' not in _text(plan.changes)
        assert "Tank and tankless" in confirmation_message(plan)

    # -- finding 7: a partial create keeps its IDs and is never replayed
    def test_partial_extension_create_reports_ids_and_retires_the_plan(self, api, config):
        preview = manage.draft_microsoft_callouts(config, campaign_id="600", callouts=["Upfront Pricing", "Bad Callout"])
        original = api.__call__

        def partial(cfg, service, path, body=None, **kw):
            if path == "AdExtensions":
                first = {**body, "AdExtensions": body["AdExtensions"][:1]}
                done = original(cfg, service, path, first, **kw)
                return {"AdExtensionIdentities": [done["AdExtensionIdentities"][0], None],
                        "NestedPartialErrors": [{"Index": 1, "BatchErrors": [
                            {"Index": 0, "ErrorCode": "CampaignServiceInvalidAdExtensionText", "Message": "Invalid callout text."}]}]}
            return original(cfg, service, path, body, **kw)

        with patch("adloop.microsoft_ads.client.ms_request", side_effect=partial):
            result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
            again = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "PARTIAL_APPLY", result
        created = [i for ids in result["created"].values() for i in ids]
        assert len(created) == 1 and created[0] in api.extensions
        assert "Invalid callout text" in result["error"]
        assert "No pending plan" in _text(again)
        assert sum(1 for e in api.extensions.values() if e.get("Text") == "Upfront Pricing") == 1


class TestUpstreamAuditFindings:
    """Codex audit of the upstream ports (adloop-audit-2026-11-04-upstream.md), Microsoft findings."""

    @staticmethod
    def _window(api, crit_id, day, start, end, campaign=CAMPAIGN):
        api.criteria[campaign].append({"Id": crit_id, "Type": "BiddableCampaignCriterion", "CampaignId": int(campaign),
                                       "Criterion": {"Type": "DayTime", "Day": day, "FromHour": start, "FromMinute": "Zero",
                                                     "ToHour": end, "ToMinute": "Zero"}})

    def _schedule(self, api):
        return sorted((c["Criterion"]["Day"], c["Criterion"]["FromHour"], c["Criterion"]["ToHour"])
                      for c in api.criteria[CAMPAIGN] if c["Criterion"]["Type"] == "DayTime")

    # -- finding 2: target CPA is read, shown and pinned
    def test_target_cpa_is_shown_and_a_later_change_blocks_the_apply(self, api, config):
        api.campaigns["600"]["BiddingScheme"] = {"Type": "MaxConversions", "TargetCpa": 30.0}
        preview = manage.update_microsoft_campaign(config, campaign_id="600", target_cpa=40)
        assert "30" in _text(preview["changes"]["changes"]["bidding"]["from"])
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)["status"] == "DRY_RUN_SUCCESS"
        api.campaigns["600"]["BiddingScheme"]["TargetCpa"] = 60.0
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "BiddingScheme changed since the draft" in _text(result)
        assert api.campaigns["600"]["BiddingScheme"]["TargetCpa"] == 60.0

    # -- finding 4: whose clock the schedule follows
    def test_searcher_time_zone_schedules_say_so(self, api, config):
        api.campaigns[CAMPAIGN]["AdScheduleUseSearcherTimeZone"] = True
        preview = manage.update_microsoft_campaign_targeting(
            config, campaign_id=CAMPAIGN, ad_schedule=[{"day_of_week": "Monday", "start_hour": 9, "end_hour": 17}])
        assert preview["changes"]["changes"]["ad_schedule"]["hours_in"] == "each searcher's own time zone"
        assert any("each searcher's own time zone" in w for w in preview["warnings"])
        assert not any("not the searcher's" in w for w in preview["warnings"])

    def test_time_zone_mode_changed_after_the_preview_blocks_the_apply(self, api, config):
        preview = manage.update_microsoft_campaign_targeting(
            config, campaign_id=CAMPAIGN, ad_schedule=[{"day_of_week": "Monday", "start_hour": 9, "end_hour": 17}])
        assert "account time zone" in preview["changes"]["changes"]["ad_schedule"]["hours_in"]
        api.campaigns[CAMPAIGN]["AdScheduleUseSearcherTimeZone"] = True
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "AdScheduleUseSearcherTimeZone" in _text(result)
        assert self._schedule(api) == []

    # -- finding 5: replacing windows Microsoft will accept
    def test_overlapping_replacement_deletes_before_it_adds(self, api, config):
        self._window(api, 7001, "Monday", 9, 17)
        preview = manage.update_microsoft_campaign_targeting(
            config, campaign_id=CAMPAIGN, ad_schedule=[{"day_of_week": "Monday", "start_hour": 10, "end_hour": 18}])
        assert any("no schedule and serves at all hours" in w for w in preview["warnings"])
        _apply(config, preview)
        methods = [c["method"] for c in api.calls if c["path"] == "CampaignCriterions" and c["method"] != "GET"
                   and "QueryByIds" not in c["path"]]
        assert methods == ["DELETE", "POST"]
        assert self._schedule(api) == [("Monday", 10, 18)]

    def test_unchanged_windows_stay_and_disjoint_ones_go_in_first(self, api, config):
        self._window(api, 7001, "Monday", 9, 17)
        self._window(api, 7002, "Tuesday", 9, 12)
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=[
            {"day_of_week": "Monday", "start_hour": 9, "end_hour": 17},     # kept
            {"day_of_week": "Tuesday", "start_hour": 13, "end_hour": 17},   # no overlap: first
        ])
        ops = _payload(preview)["operations"]
        assert len(ops["schedule_add_first"]) == 1 and ops["schedule_delete"] == ["7002"] and ops["schedule_add_after"] == []
        assert not any("serves at all hours" in w for w in preview["warnings"])
        _apply(config, preview)
        assert self._schedule(api) == [("Monday", 9, 17), ("Tuesday", 13, 17)]
        assert not any(c["method"] == "DELETE" and 7001 in (c["body"].get("CampaignCriterionIds") or []) for c in api.calls)

    def test_identical_schedule_is_nothing_to_change(self, api, config):
        self._window(api, 7001, "Monday", 9, 17)
        result = manage.update_microsoft_campaign_targeting(
            config, campaign_id=CAMPAIGN, ad_schedule=[{"day_of_week": "Monday", "start_hour": 9, "end_hour": 17}])
        assert "already matches" in result["error"]

    def test_failure_after_the_old_windows_are_gone_is_reported(self, api, config):
        self._window(api, 7001, "Monday", 9, 17)
        preview = manage.update_microsoft_campaign_targeting(
            config, campaign_id=CAMPAIGN, ad_schedule=[{"day_of_week": "Monday", "start_hour": 10, "end_hour": 18}])
        api.fail[("POST", "CampaignCriterions")] = MicrosoftAdsApiError("Internal error", status=400)
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "PARTIAL_APPLY", result
        assert "serves at all hours" in result["error"] and "removed 1 schedule window" in result["error"]
        assert self._schedule(api) == []

    # -- finding 3: a create whose answer was lost is never replayed
    @pytest.mark.parametrize("draft", [
        lambda: manage.draft_microsoft_callouts(CFG[0], campaign_id="600", callouts=["Upfront Pricing", "Licensed Pros"]),
        lambda: manage.draft_microsoft_keywords(CFG[0], ad_group_id="11", keywords=["clogged drain"]),
        lambda: manage.propose_microsoft_negative_keyword_list(CFG[0], list_name="Jobs", keywords=["careers"],
                                                               campaign_ids=["600"]),
    ])
    def test_lost_create_response_retires_the_plan(self, api, config, draft):
        CFG[0] = config
        preview = draft()
        assert "plan_id" in preview, preview
        original = api.__call__

        def lose(cfg, service, path, body=None, **kw):
            result = original(cfg, service, path, body, **kw)
            if kw.get("method", "POST") == "POST" and path in ("AdExtensions", "Keywords", "SharedEntity"):
                raise MicrosoftAdsApiError("Could not reach Microsoft Advertising (ReadTimeout); "
                                           "the change may or may not have been made.")
            return result

        with patch("adloop.microsoft_ads.client.ms_request", side_effect=lose):
            first = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
            again = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert first["status"] == "UNKNOWN_OUTCOME", first
        assert "No pending plan" in _text(again)


CFG = [None]


class TestKeywordStatusOnCreate:
    """Finding 9: the approval surfaces say keywords are created Active."""

    def test_keywords_in_a_live_ad_group_say_they_serve_immediately(self, api, config):
        preview = manage.draft_microsoft_keywords(config, ad_group_id="11", keywords=["clogged drain"])
        shown = preview_store.get_plan(preview["plan_id"]).changes
        assert shown["status_on_create"] == "Active"
        assert shown["ad_group_status"] == "Active" and shown["campaign_status"] == "Active"
        assert any("can serve as soon as this is applied" in w for w in preview["warnings"])

    def test_keywords_in_a_paused_ad_group_say_when_they_serve(self, api, config):
        preview = manage.draft_microsoft_keywords(config, ad_group_id="12", keywords=["tankless"])
        assert preview["changes"]["ad_group_status"] == "Paused"
        assert any("serve once both are active" in w for w in preview["warnings"])
        assert not any("as soon as this is applied" in w for w in preview["warnings"])

    def test_a_new_ad_group_states_its_status(self, api, config):
        preview = manage.draft_microsoft_ad_group(config, campaign_id=CAMPAIGN, name="Sewer", cpc_bid=5,
                                                  keywords=["sewer line"])
        assert preview["changes"]["status_on_create"].startswith("Active")


class TestScheduleDeltaFindings:
    """Codex delta audit (adloop-audit-2026-11-04-delta.md): findings 2, 4 and 7."""

    _window = staticmethod(TestUpstreamAuditFindings._window)
    _schedule = TestUpstreamAuditFindings._schedule

    def test_six_disjoint_windows_replaced_by_six_others_stay_within_seven_a_day(self, api, config):
        # Finding 4: adding all six new windows first would ask for twelve on Monday.
        for i, start in enumerate(range(0, 12, 2)):
            self._window(api, 7100 + i, "Monday", start, start + 1)
        new = [{"day_of_week": "Monday", "start_hour": h, "end_hour": h + 1} for h in range(12, 24, 2)]
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=new)
        ops = _payload(preview)["operations"]
        assert len(ops["schedule_add_first"]) == 1 and len(ops["schedule_add_after"]) == 5
        assert not any("serves at all hours" in w for w in preview["warnings"])
        _apply(config, preview)
        assert self._schedule(api) == [("Monday", h, h + 1) for h in range(12, 24, 2)]

    def test_a_failure_names_the_windows_that_stay(self, api, config):
        # Finding 7: Monday is kept, so the campaign is not unscheduled.
        self._window(api, 7001, "Monday", 9, 17)
        self._window(api, 7002, "Tuesday", 9, 17)
        preview = manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=[
            {"day_of_week": "Monday", "start_hour": 9, "end_hour": 17},
            {"day_of_week": "Tuesday", "start_hour": 10, "end_hour": 18}])
        api.fail[("POST", "CampaignCriterions")] = MicrosoftAdsApiError("Internal error", status=400)
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "PARTIAL_APPLY", result
        assert "keeps Mon 09:00–17:00" in result["error"] and "serves at all hours" not in result["error"]
        assert self._schedule(api) == [("Monday", 9, 17)]


class TestGoalsWithoutInStorePilot:
    """The MaxConversions goal check must work for accounts outside Microsoft's in-store pilot."""

    def test_counted_goals_are_counted(self, api, config):
        api.in_store_pilot = False
        assert manage._counted_goals(config, ACCOUNT, CUSTOMER) == 1
        api.goals = []
        assert manage._counted_goals(config, ACCOUNT, CUSTOMER) == 0

    def test_max_conversions_without_goals_warns_or_refuses(self, api, config):
        api.in_store_pilot = False
        api.goals = []
        # Already on MaxConversions: a target change only warns.
        update = manage.update_microsoft_campaign(config, campaign_id="600", target_cpa=25)
        assert any("nothing to optimize" in w for w in update["warnings"])
        # A switch to it, or a new campaign on it, is refused: Microsoft would refuse it.
        switch = manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, bidding_strategy="MaxConversions")
        assert "needs an active conversion goal" in _text(switch) and "plan_id" not in switch
        new = manage.draft_microsoft_campaign(config, name="New", daily_budget=10, languages=["English"],
                                              location_ids=["190"])
        assert "needs an active conversion goal" in _text(new)

    def test_an_unreadable_goal_list_is_unknown(self, api, config):
        api.fail[("POST", "ConversionGoals/QueryByIds")] = MicrosoftAdsApiError("Internal error", status=500)
        assert manage._counted_goals(config, ACCOUNT, CUSTOMER) == -1


# ---------------------------------------------------------------------------
# Image extensions
# ---------------------------------------------------------------------------


def _png(width: int, height: int, *, animated: bool = False, pad: int = 0) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if animated:  # APNG: the animation control chunk comes before the image data
        out += chunk(b"acTL", struct.pack(">II", 2, 0))
    if pad:
        out += chunk(b"tEXt", b"pad\x00" + b"x" * pad)
    return out + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b"")


def _jpeg(width: int, height: int) -> bytes:
    import struct

    return b"\xff\xd8\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00\xff\xd9"


def _gif(width: int, height: int, frames: int = 1) -> bytes:
    import struct

    frame = b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00" + b"\x02\x01\x00\x00"
    control = b"\x21\xf9\x04\x00\x0a\x00\x00\x00"  # graphic control extension (frame delay)
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + (control + frame) * frames + b"\x3b"


def _file(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


class TestImageExtensions:
    @pytest.fixture
    def web(self, monkeypatch):
        """Public image URLs served from a dict the test can change."""
        from adloop.ads.image_fetch import FetchedImage

        pages: dict[str, bytes] = {}
        monkeypatch.setattr("adloop.ads.image_fetch.fetch_image_bytes",
                            lambda url: FetchedImage(url=url, final_url=url, data=pages[url]))
        return pages

    def _image_link(self, api, ext_id: str, owner: str, association: str, alt: str = "Technician at a drain"):
        api.extensions[ext_id] = {"Id": int(ext_id), "Type": "ImageAdExtension", "AlternativeText": alt,
                                  "ImageMediaIds": [int(ext_id) + 100], "Description": "AdLoop image van",
                                  "Layouts": ["SearchSingle"]}
        api.ext_links.append((ext_id, owner, association))

    def test_bad_images_and_fields_are_refused_with_the_reason(self, api, config, tmp_path):
        good = _file(tmp_path, "van.png", _png(1200, 628))

        def refused(**kw):
            kw.setdefault("alt_text", "Plumbing van")
            return _text(manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, **kw))

        assert "703x368" in refused(image_paths=[_file(tmp_path, "small.png", _png(600, 314))])
        square = refused(image_paths=[_file(tmp_path, "square.jpg", _jpeg(1200, 1200))])
        assert "1.00:1" in square and "1.91:1" in square and "Crop it to 1200x628" in square
        assert "maximum (width 2592, height 2048)" in refused(image_paths=[_file(tmp_path, "huge.png", _png(3000, 1571))])
        assert "animated images are not accepted" in refused(image_paths=[_file(tmp_path, "spin.gif", _gif(1200, 628, frames=2))])
        assert "animated images are not accepted" in refused(image_paths=[_file(tmp_path, "spin.png", _png(1200, 628, animated=True))])
        assert "5 MB limit" in refused(image_paths=[_file(tmp_path, "heavy.png", _png(1200, 628, pad=5 * 1024 * 1024))])
        assert "Unsupported image type" in refused(image_paths=[_file(tmp_path, "photo.bmp", b"BM" + b"\x00" * 64)])
        assert "twice" in refused(image_paths=[good, good])
        assert "alt_text is required" in refused(image_paths=[good], alt_text=" ")
        assert "alt_text is 36 characters" in refused(image_paths=[good], alt_text="x" * 36)
        assert "display_text is 36 characters" in refused(image_paths=[good], display_text="y" * 36)
        assert "At least one image" in refused()
        assert "At most 10 images" in refused(image_paths=[good] * 11)
        # A still GIF at the right size passes the image checks.
        still = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Van",
                                                       image_paths=[_file(tmp_path, "still.gif", _gif(1200, 628))])
        assert "plan_id" in still, still
        both = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, ad_group_id="11", alt_text="Van",
                                                      image_paths=[good])
        assert "not both" in _text(both)
        assert "was not found" in _text(manage.draft_microsoft_image_extension(
            config, ad_group_id="999", alt_text="Van", image_paths=[good]))
        assert not [c for c in api.calls if c["path"] in ("Media", "AdExtensions")]

    def test_preview_shows_images_target_and_alt_text(self, api, config, tmp_path, web):
        from adloop.safety.approval import confirmation_message
        from adloop.safety.preview import get_plan

        web["https://cdn.example.com/team.jpg"] = _jpeg(1406, 736)
        preview = manage.draft_microsoft_image_extension(
            config, ad_group_id="11", image_paths=[_file(tmp_path, "van.png", _png(1200, 628))],
            image_urls=["https://cdn.example.com/team.jpg"], alt_text="Plumber clearing a drain",
            display_text="Same-day drain service", final_url="https://example.com/drain")
        assert preview["operation"] == "microsoft_add_image_extension" and preview["entity_type"] == "ad_group"
        changes = preview["changes"]
        assert changes["entity_name"] == "ad group 'Drains' in campaign 'Acme Plumbing - Search'"
        payload = _payload(preview)
        assert payload["association_type"] == "AdGroup" and payload["entity_id"] == "11"
        assert not {"association_type", "image_payloads", "ad_account_id", "customer_id"} & set(changes)
        assert changes["images"][0].endswith("PNG 1200x628 (1.91:1), 0.1 KB")
        assert changes["images"][1] == "https://cdn.example.com/team.jpg: JPEG 1406x736 (1.91:1), 0.0 KB"
        assert changes["alt_text"] == "Plumber clearing a drain" and changes["display_text"] == "Same-day drain service"
        assert changes["final_url"] == "https://example.com/drain"
        assert changes["layouts"] == ["SearchSingle", "SearchMultiple"]
        warnings = " ".join(preview["warnings"])
        assert "Audience Network" in warnings and "multi-image layout" in warnings and "PNG images to JPEG" in warnings
        # The approval dialog shows the images, never the pinned payloads.
        message = confirmation_message(get_plan(preview["plan_id"]))
        assert "1200x628" in message and "image payloads" not in message and "sha256" not in message
        assert "sha256" not in _approval_surfaces(config, preview)
        plain = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Van",
                                                       image_paths=[_file(tmp_path, "van2.png", _png(1910, 1000))])
        assert plain["changes"]["layouts"] == ["SearchSingle"] and "display_text" not in plain["changes"]
        assert plain["changes"]["final_url"] == "the ad's own final URL"
        assert not [c for c in api.calls if c["path"] in ("Media", "AdExtensions")]

    def test_apply_uploads_media_then_creates_then_links(self, api, config, tmp_path, web):
        import base64

        png, jpeg = _png(1200, 628), _jpeg(1406, 736)
        web["https://cdn.example.com/team.jpg"] = jpeg
        preview = manage.draft_microsoft_image_extension(
            config, ad_group_id="11", image_paths=[_file(tmp_path, "van.png", png)],
            image_urls=["https://cdn.example.com/team.jpg"], alt_text="Plumber clearing a drain",
            display_text="Same-day drain service", final_url="https://example.com/drain")
        payload = _payload(preview)
        result = _apply(config, preview)
        writes = [c["path"] for c in api.writes()]
        assert writes == ["Media", "AdExtensions", "AdExtensionsAssociations/Set"]
        media = _last(api, "Media", "POST")["body"]
        assert media["AccountId"] == ACCOUNT
        assert [(m["Type"], m["MediaType"]) for m in media["Media"]] == [("Image", "GenericImage")] * 2
        assert [base64.b64decode(m["Data"]) for m in media["Media"]] == [png, jpeg]
        assert result["media_ids"] == [str(i) for i in sorted(int(i) for i in api.media)]
        created = _last(api, "AdExtensions")["body"]["AdExtensions"]
        first_media = int(result["media_ids"][0])
        assert created[0] == {
            "Type": "ImageAdExtension", "AlternativeText": "Plumber clearing a drain",
            "Description": payload["image_payloads"][0]["name"][:100],
            "ImageMediaIds": [first_media],
            "Images": [{"Asset": {"Type": "ImageAsset", "Id": first_media, "SubType": "LandscapeImageMedia"}}],
            "Layouts": ["SearchSingle", "SearchMultiple"], "DisplayText": "Same-day drain service",
            "FinalUrls": ["https://example.com/drain"]}
        assert created[1]["ImageMediaIds"] == [int(result["media_ids"][1])]
        link = _last(api, "AdExtensionsAssociations/Set")["body"]
        assert link["AssociationType"] == "AdGroup"
        assert link["AdExtensionIdToEntityIdAssociations"] == [
            {"AdExtensionId": int(i), "EntityId": 11} for i in result["extension_ids"]]
        assert result["linked_to"].startswith("ad group 'Drains'")
        # The new extensions now show up on the ad group.
        rows = [r for r in manage.get_microsoft_ad_extensions(config)["extensions"] if r["type"] == "Image"]
        assert sorted(r["extension_id"] for r in rows) == sorted(result["extension_ids"])
        # Account and campaign scope link to those entities.
        account = manage.draft_microsoft_image_extension(config, scope="account", alt_text="Van",
                                                         image_paths=[_file(tmp_path, "acct.png", _png(1300, 681))])
        _apply(config, account)
        assert _last(api, "AdExtensionsAssociations/Set")["body"]["AssociationType"] == "Account"
        assert _last(api, "AdExtensionsAssociations/Set")["body"]["AdExtensionIdToEntityIdAssociations"][0]["EntityId"] == int(ACCOUNT)
        assert "FinalUrls" not in _last(api, "AdExtensions")["body"]["AdExtensions"][0]
        campaign = manage.draft_microsoft_image_extension(config, campaign_id="600", alt_text="Van",
                                                          image_paths=[_file(tmp_path, "brand.png", _png(1400, 733))])
        _apply(config, campaign)
        assert _last(api, "AdExtensionsAssociations/Set")["body"]["AssociationType"] == "Campaign"

    @pytest.mark.parametrize("failing,created_keys", [
        ("AdExtensions", {"media_ids", "done"}),
        ("AdExtensionsAssociations/Set", {"media_ids", "extension_ids", "done"}),
    ])
    def test_a_later_step_failing_reports_what_exists(self, api, config, tmp_path, failing, created_keys):
        preview = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Van",
                                                         image_paths=[_file(tmp_path, "van.png", _png(1200, 628))])
        api.fail[("POST", failing)] = MicrosoftAdsApiError("CampaignServiceImageAdExtensionPilotNotEnabledForCustomer",
                                                           status=400, codes=["CampaignServiceImageAdExtensionPilotNotEnabledForCustomer"])
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        again = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result["status"] == "PARTIAL_APPLY", result
        assert set(result["created"]) == created_keys
        assert result["created"]["media_ids"] == list(api.media)
        if "extension_ids" in created_keys:
            assert result["created"]["extension_ids"][0] in api.extensions
        assert "PilotNotEnabled" in result["error"] and "uploaded 1 image(s)" in result["error"]
        assert "No pending plan" in _text(again)
        assert len(api.media) == 1

    def test_a_refused_upload_changes_nothing_and_keeps_the_plan(self, api, config, tmp_path):
        preview = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Van",
                                                         image_paths=[_file(tmp_path, "van.png", _png(1200, 628))])
        api.fail[("POST", "Media")] = MicrosoftAdsApiError("CampaignServiceImageOverweight", status=400,
                                                           codes=["CampaignServiceImageOverweight"])
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "PARTIAL_APPLY" and "ImageOverweight" in _text(result), result
        assert not [c for c in api.calls if c["path"] == "AdExtensions"] and not api.media
        # Nothing was created, so the plan stays pending and can be applied once the cause is fixed.
        del api.fail[("POST", "Media")]
        assert ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)["status"] == "APPLIED"

    def test_dry_run_rechecks_every_image(self, api, config, tmp_path, web):
        web["https://cdn.example.com/team.jpg"] = _jpeg(1406, 736)
        from_url = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Team",
                                                          image_urls=["https://cdn.example.com/team.jpg"])
        web["https://cdn.example.com/team.jpg"] = _jpeg(1200, 628)  # replaced on the server
        dry = ads_write.confirm_and_apply(config, plan_id=from_url["plan_id"], dry_run=True)
        assert dry["status"] != "DRY_RUN_SUCCESS" and "changed since the preview" in _text(dry)
        path = _file(tmp_path, "van.png", _png(1200, 628))
        from_file = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Van", image_paths=[path])
        dry = ads_write.confirm_and_apply(config, plan_id=from_file["plan_id"], dry_run=True)
        assert dry["status"] == "DRY_RUN_SUCCESS" and dry["checks"]["images_verified"] == 1, dry
        (tmp_path / "van.png").write_bytes(_png(1200, 628, pad=10))
        assert "changed since the preview" in _text(ads_write.confirm_and_apply(config, plan_id=from_file["plan_id"], dry_run=False))
        assert not api.media and not [c for c in api.calls if c["path"] == "Media"]

    def test_link_limits(self, api, config, tmp_path):
        image = _file(tmp_path, "van.png", _png(1200, 628))
        for n in range(6):
            self._image_link(api, str(7100 + n), "600", "Campaign")
        warned = manage.draft_microsoft_image_extension(config, campaign_id="600", alt_text="Van", image_paths=[image])
        assert any("would have 7 image extensions" in w for w in warned["warnings"])
        for n in range(6, 25):
            self._image_link(api, str(7100 + n), "600", "Campaign")
        refused = manage.draft_microsoft_image_extension(config, campaign_id="600", alt_text="Van", image_paths=[image])
        assert "has 25 image extension(s)" in _text(refused) and "over Microsoft's 25" in _text(refused)
        # Links added after the draft are caught by the dry run.
        api.ext_links = [l for l in api.ext_links if l[1] != "600"]
        preview = manage.draft_microsoft_image_extension(config, campaign_id="600", alt_text="Van", image_paths=[image])
        for n in range(25):
            self._image_link(api, str(7200 + n), "600", "Campaign")
        assert "now has 25" in _text(ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True))

    def test_read_lists_image_extensions_at_every_level(self, api, config):
        assert any("No image extensions are linked" in i for i in manage.get_microsoft_ad_extensions(config)["insights"])
        self._image_link(api, "7003", "11", "AdGroup")
        self._image_link(api, "7004", ACCOUNT, "Account", alt="Our team")
        before = len(api.calls)
        result = manage.get_microsoft_ad_extensions(config)
        rows = {r["extension_id"]: r for r in result["extensions"]}
        group = rows["7003"]
        assert group["type"] == "Image" and group["level"] == "ad_group" and group["entity"] == "Drains"
        assert group["campaign"] == "Acme Plumbing - Search" and group["media_ids"] == ["7103"]
        assert group["alt_text"] == "Technician at a drain" and group["layouts"] == ["SearchSingle"]
        assert group["remove_id"] == "7003~11~AdGroup"
        assert rows["7004"]["level"] == "account" and rows["7004"]["remove_id"] == f"7004~{ACCOUNT}~Account"
        assert rows["7001"]["level"] == "campaign"  # the sitelink is still there
        assert not any("No image extensions" in i for i in result["insights"])
        # Ad groups are asked for together, not one request each.
        group_queries = [c for c in api.calls[before:] if c["path"] == "AdExtensionsAssociations/Query"
                         and c["body"]["AssociationType"] == "AdGroup"]
        assert len(group_queries) == 1 and sorted(group_queries[0]["body"]["EntityIds"]) == [11, 12, 21]
        assert "ImageAdExtension" in group_queries[0]["body"]["AdExtensionType"]

    def test_read_shows_which_picture_each_image_extension_holds(self, api, config, tmp_path):
        # The association query leaves the images out (as Microsoft does live), so
        # the read asks for them by id and then looks up each picture.
        preview = manage.draft_microsoft_image_extension(
            config, campaign_id=CAMPAIGN, alt_text="Technician at a drain", display_text="Drain cleaning",
            image_paths=[_file(tmp_path, f"d{i}.png", _png(1200, 628, pad=i)) for i in range(3)])
        result = _apply(config, preview)
        before = len(api.calls)
        rows = {r["extension_id"]: r for r in manage.get_microsoft_ad_extensions(config, campaign_id=CAMPAIGN)["extensions"]
                if r["type"] == "Image"}
        assert sorted(rows) == sorted(result["extension_ids"])
        for ext_id, media_id in zip(result["extension_ids"], result["media_ids"], strict=True):
            row = rows[ext_id]
            assert row["media_ids"] == [media_id]
            assert row["display_text"] == "Drain cleaning" and row["layouts"] == ["SearchSingle", "SearchMultiple"]
            assert row["images"] == [{"media_id": media_id, "media_type": "GenericImage", "width": 1200, "height": 628,
                                      "url": f"https://www.bing.com/th?id=OADD2.{media_id}"}]
        by_id = [c for c in api.calls[before:] if c["path"] == "AdExtensions/QueryByIds"]
        assert len(by_id) == 1 and by_id[0]["body"]["ReturnAdditionalFields"] == "Images,DisplayText,Layouts"
        assert sorted(map(str, by_id[0]["body"]["AdExtensionIds"])) == sorted(result["extension_ids"])
        media = [c for c in api.calls[before:] if c["path"] == "MediaMetaData/QueryByIds"]
        assert len(media) == 1 and sorted(map(str, media[0]["body"]["MediaIds"])) == sorted(result["media_ids"])

    def test_read_keeps_the_rows_when_image_details_fail(self, api, config):
        self._image_link(api, "7003", "11", "AdGroup")
        api.fail[("POST", "MediaMetaData/QueryByIds")] = MicrosoftAdsApiError("Internal error", status=500)
        result = manage.get_microsoft_ad_extensions(config)
        row = next(r for r in result["extensions"] if r["extension_id"] == "7003")
        assert row["remove_id"] == "7003~11~AdGroup" and "images" not in row
        assert any("Image details could not be read" in i for i in result["insights"])

    def test_large_images_get_no_size_warning(self, api, config, tmp_path):
        # Microsoft's Image reference names a 75 KB cap that it does not enforce:
        # 125-300 KB JPEGs uploaded on a live account. Only 5 MB is a limit.
        big = _file(tmp_path, "big.png", _png(1200, 628, pad=300_000))
        preview = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Van", image_paths=[big])
        assert "plan_id" in preview
        assert not [w for w in preview["warnings"] if "KB" in w or "base64" in w]

    def test_unlinking_an_image_extension(self, api, config):
        self._image_link(api, "7003", "11", "AdGroup")
        preview = manage.remove_microsoft_entity(config, entity_type="extension_association", entity_id="7003~11~AdGroup")
        assert any("unlinks the extension only" in w for w in preview["warnings"])
        _apply(config, preview)
        assert _last(api, "AdExtensionsAssociations", "DELETE")["body"] == {
            "AccountId": ACCOUNT, "AssociationType": "AdGroup",
            "AdExtensionIdToEntityIdAssociations": [{"AdExtensionId": 7003, "EntityId": 11}]}
        assert ("7003", "11", "AdGroup") not in api.ext_links and "7003" in api.extensions
        bad = manage.remove_microsoft_entity(config, entity_type="extension_association", entity_id="7003~11~Keyword")
        assert "Account|Campaign|AdGroup" in _text(bad)

    def test_server_tool_and_hosted_paths(self, api, config, tmp_path, monkeypatch):
        from adloop import runtime, server

        runtime.set_default_config(config)
        try:
            preview = server.draft_microsoft_image_extension(
                ad_group_id="11", alt_text="Van", image_paths=[_file(tmp_path, "van.png", _png(1200, 628))])
            assert preview["status"] == "PENDING_CONFIRMATION" and preview["platform"] == "microsoft"
            monkeypatch.setattr("adloop.runtime.deployment_mode", lambda: "server")
            hosted = server.draft_microsoft_image_extension(ad_group_id="11", alt_text="Van", image_paths=["/etc/hosts"])
            assert "hosted server" in hosted["error"]
        finally:
            runtime.set_default_config(None)

    def test_uploaded_images_are_used_by_id(self, api, config, tmp_path):
        import base64

        from adloop import uploads

        uploads.set_upload_store(uploads.LocalUploadStore(tmp_path / "uploads"))
        try:
            data = _png(1200, 628)
            upload = uploads.get_upload_store().put(uploads.current_tenant(), "crew.png", data)
            preview = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Our crew",
                                                             upload_ids=[upload.upload_id])
            assert preview["changes"]["images"] == [f"upload {upload.upload_id}: PNG 1200x628 (1.91:1), 0.1 KB"]
            _apply(config, preview)
            assert base64.b64decode(_last(api, "Media", "POST")["body"]["Media"][0]["Data"]) == data
            missing = manage.draft_microsoft_image_extension(config, campaign_id=CAMPAIGN, alt_text="Our crew",
                                                             upload_ids=["up_missing0000"])
            assert "Upload 1: upload up_missing0000 was not found" in _text(missing) and "plan_id" not in missing
        finally:
            uploads.set_upload_store(None)


# ---------------------------------------------------------------------------
# Upstream review (#130)
# ---------------------------------------------------------------------------


class TestReviewBroadMatch:
    """Broad match only with conversion-based bidding; Maximize Clicks does not count."""

    def test_broad_keywords_are_refused_on_maximize_clicks(self, api, config):
        api.campaigns["600"]["BiddingScheme"] = {"Type": "MaxClicks"}
        refused = manage.draft_microsoft_keywords(config, ad_group_id="21",
                                                  keywords=[{"text": "acme", "match_type": "broad"}])
        assert "Broad match needs conversion-based bidding" in _text(refused) and "plan_id" not in refused

    @pytest.mark.parametrize("scheme", ["MaxConversions", "MaxConversionValue", "TargetCpa", "TargetRoas",
                                        "MaxConversionValueBiddingScheme"])
    def test_broad_keywords_are_allowed_on_conversion_bidding(self, api, config, scheme):
        api.campaigns["600"]["BiddingScheme"] = {"Type": scheme}
        preview = manage.draft_microsoft_keywords(config, ad_group_id="21",
                                                  keywords=[{"text": "acme", "match_type": "broad"}])
        assert "plan_id" in preview, preview

    def test_moving_to_maximize_clicks_with_broad_keywords_is_refused(self, api, config):
        # CAMPAIGN has the active broad keyword 'plumber'.
        result = manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, bidding_strategy="MaxClicks")
        assert "broad match needs conversion-based bidding" in _text(result) and "plan_id" not in result

    def test_a_broad_keyword_added_after_a_maximize_clicks_draft_fails_the_apply(self, api, config):
        preview = manage.update_microsoft_campaign(config, campaign_id="600", bidding_strategy="MaxClicks")
        api.keywords["299"] = {"Id": 299, "Text": "acme", "MatchType": "Broad", "Bid": {"Amount": None},
                               "Status": "Active", "_group": "21"}
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=False)
        assert result.get("status") != "APPLIED" and "now has 1 active broad-match" in _text(result)

    def test_the_keyword_read_flags_broad_on_maximize_clicks(self, api, config):
        api.campaigns["600"]["BiddingScheme"] = {"Type": "MaxClicks"}
        api.keywords["299"] = {"Id": 299, "Text": "acme", "MatchType": "Broad", "Bid": {"Amount": None},
                               "Status": "Active", "_group": "21"}
        insights = " ".join(read.get_microsoft_keywords(config, campaign_id="600")["insights"])
        assert "without conversion-based bidding" in insights and "Maximize Clicks" in insights


class TestReviewCriterionRemoval:
    def test_the_draft_names_the_criterion(self, api, config):
        preview = manage.remove_microsoft_entity(config, entity_type="campaign_criterion", entity_id=f"{CAMPAIGN}~1002")
        changes = preview["changes"]
        assert changes["entity_name"] == "location Los Angeles, California (excluded)"
        assert changes["campaign_name"] == "Acme Plumbing - Search" and changes["criterion_type"] == "Location"
        assert "request" not in changes

    def test_the_last_targeted_location_cannot_be_removed(self, api, config):
        result = manage.remove_microsoft_entity(config, entity_type="campaign_criterion", entity_id=f"{CAMPAIGN}~1001")
        assert "last targeted location" in _text(result) and "update_microsoft_campaign_targeting" in _text(result)
        assert "plan_id" not in result

    def test_a_location_with_others_left_can_go_but_not_once_it_is_the_last(self, api, config):
        api.criteria[CAMPAIGN].append({"Id": 1004, "Type": "BiddableCampaignCriterion", "CampaignId": int(CAMPAIGN),
                                       "Criterion": {"Type": "Radius", "Name": "Downtown", "Radius": 10,
                                                     "RadiusUnit": "Miles"}})
        preview = manage.remove_microsoft_entity(config, entity_type="campaign_criterion", entity_id=f"{CAMPAIGN}~1001")
        assert preview["changes"]["entity_name"] == "location Springfield, Illinois (targeted)"
        api.criteria[CAMPAIGN] = [c for c in api.criteria[CAMPAIGN] if c["Id"] != 1004]
        result = ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        assert result["status"] == "DRY_RUN_FAILED" and "last targeted location" in result["error"]

    def test_an_unknown_criterion_is_reported(self, api, config):
        assert "has no target criterion 4242" in _text(manage.remove_microsoft_entity(
            config, entity_type="campaign_criterion", entity_id=f"{CAMPAIGN}~4242"))


class TestReviewPayloads:
    """Request bodies, snapshots and ids live in apply_only_payload, never on an approval surface."""

    @pytest.mark.parametrize("draft", [
        lambda c: manage.update_microsoft_campaign(c, campaign_id="600", target_cpa=40),
        lambda c: manage.update_microsoft_campaign_targeting(c, campaign_id=CAMPAIGN, location_ids=["190", "200"]),
        lambda c: manage.draft_microsoft_campaign(c, name="New", daily_budget=10, languages=["English"],
                                                  location_ids=["190"]),
        lambda c: manage.draft_microsoft_ad_group(c, campaign_id=CAMPAIGN, name="Sewer", cpc_bid=5, keywords=["sewer"]),
        lambda c: manage.update_microsoft_ad_group(c, ad_group_id="11", cpc_bid=9),
        lambda c: manage.draft_microsoft_keywords(c, ad_group_id="11", keywords=["clogged drain"]),
        lambda c: manage.update_microsoft_keyword_bids(c, ad_group_id="11", bids=[{"keyword_id": "102", "bid": 10}]),
        lambda c: manage.add_microsoft_negative_keywords(c, campaign_id=CAMPAIGN, keywords=["cheap"]),
        lambda c: manage.propose_microsoft_negative_keyword_list(c, list_name="New list", keywords=["cheap"],
                                                                 campaign_ids=["600"]),
        lambda c: manage.update_microsoft_responsive_search_ad(c, ad_id="301", path2="fast"),
        lambda c: manage.draft_microsoft_callouts(c, campaign_id="600", callouts=["Licensed", "Insured"]),
        lambda c: manage.remove_microsoft_entity(c, entity_type="keyword", entity_id="102"),
    ])
    def test_payloads_stay_off_every_approval_surface(self, api, config, draft):
        preview = draft(config)
        assert "plan_id" in preview, preview
        ads_write.confirm_and_apply(config, plan_id=preview["plan_id"], dry_run=True)
        plan = preview_store.get_plan(preview["plan_id"])
        hidden = {"update", "before", "operations", "campaign", "criteria", "ad_group", "keyword_rows",
                  "negative_rows", "request", "image_payloads", "extensions", "entity_kind", "ad",
                  "update_audience_adjustment", "ad_account_id", "customer_id", "campaign_ids"}
        assert not hidden & set(plan.changes), sorted(hidden & set(plan.changes))
        assert {"ad_account_id", "customer_id"} <= set(plan.apply_only_payload)
        surfaces = _approval_surfaces(config, preview)
        for marker in ('"Type": "BiddableCampaignCriterion"', '"MatchType":', '"Asset":', "customer id", "ad account id"):
            assert marker not in surfaces, marker


class TestReviewAccountScans:
    def _reads(self, api, path):
        return sum(1 for c in api.calls if c["path"] == path)

    def test_an_rsa_update_reads_only_its_own_ad_group_after_the_draft(self, api, config):
        api.ad_groups["13"] = {"Id": 13, "Name": "Sewers", "Status": "Active", "_campaign": CAMPAIGN}
        api.ad_groups["22"] = {"Id": 22, "Name": "Brand two", "Status": "Active", "_campaign": "600"}
        preview = manage.update_microsoft_responsive_search_ad(config, ad_id="301", path2="fast")
        api.calls.clear()
        _apply(config, preview)
        # Dry run and apply: one ad group list of the ad's campaign and one ad list of its group each.
        assert self._reads(api, "AdGroups/QueryByCampaignId") == 2
        assert self._reads(api, "Ads/QueryByAdGroupId") == 2

    def test_a_keyword_removal_reads_only_its_own_ad_group_after_the_draft(self, api, config):
        preview = manage.remove_microsoft_entity(config, entity_type="keyword", entity_id="103")
        api.calls.clear()
        _apply(config, preview)
        assert self._reads(api, "AdGroups/QueryByCampaignId") == 2
        assert {c["body"]["AdGroupId"] for c in api.calls if c["path"] == "Keywords/QueryByAdGroupId"} == {"12"}

    def test_campaign_reads_ask_once_per_campaign_for_its_targeting(self, api, config):
        read.get_microsoft_campaigns(config)
        queries = [c["body"] for c in api.calls if c["path"] == "CampaignCriterions/QueryByIds"]
        assert [q["CriterionType"] for q in queries] == ["Targets"] * len(api.campaigns)

    def test_a_refused_targets_query_falls_back_to_one_per_type(self, api, config):
        api.refuse_targets_query = True
        rows = {r["campaign_id"]: r for r in read.get_microsoft_campaigns(config)["campaigns"]}
        assert rows[CAMPAIGN]["locations"]["included"] == ["Springfield, Illinois"]
        kinds = [c["body"]["CriterionType"] for c in api.calls if c["path"] == "CampaignCriterions/QueryByIds"]
        assert kinds.count("Targets") == 1 and kinds.count("Location") == len(api.campaigns)


class TestReviewReads:
    def test_radius_targets_are_named(self, api, config):
        api.criteria[CAMPAIGN].append({"Id": 1004, "Type": "BiddableCampaignCriterion", "CampaignId": int(CAMPAIGN),
                                       "Criterion": {"Type": "Radius", "Name": "Downtown", "Radius": 10,
                                                     "RadiusUnit": "Miles"}})
        rows = {r["campaign_id"]: r for r in read.get_microsoft_campaigns(config)["campaigns"]}
        assert "Downtown (10 Miles)" in rows[CAMPAIGN]["locations"]["included"]

    def test_an_ad_group_without_its_own_audience_setting_inherits_the_campaign(self, api, config):
        api.campaigns[CAMPAIGN]["AudienceAdsBidAdjustment"] = -100
        api.ad_groups["11"]["AudienceAdsBidAdjustment"] = None
        rows = {r["ad_group_id"]: r for r in read.get_microsoft_ad_groups(config)["ad_groups"]}
        assert rows["11"]["audience_network"] == {"on": False, "bid_adjustment": -100, "set_on": "campaign"}
        api.campaigns[CAMPAIGN]["AudienceAdsBidAdjustment"] = None
        rows = {r["ad_group_id"]: r for r in read.get_microsoft_ad_groups(config)["ad_groups"]}
        assert rows["11"]["audience_network"]["on"] and rows["11"]["audience_network"]["set_on"] == "Microsoft default"
        insights = " ".join(read.get_microsoft_ad_groups(config)["insights"])
        assert "Audience Network" in insights


class TestReviewValidation:
    def test_target_cpa_and_max_cpc_on_a_manual_campaign_are_refused(self, api, config):
        # CAMPAIGN is EnhancedCpc: neither value would reach Microsoft.
        assert "target_cpa goes with MaxConversions" in _text(
            manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, target_cpa=30))
        assert "max_cpc caps" in _text(manage.update_microsoft_campaign(config, campaign_id=CAMPAIGN, max_cpc=5))
        assert "max_cpc caps" in _text(manage.update_microsoft_campaign(
            config, campaign_id=CAMPAIGN, bidding_strategy="ManualCpc", max_cpc=5))

    def test_seven_schedule_windows_a_day_are_allowed(self, api, config):
        seven = [{"day_of_week": "Monday", "start_hour": h, "end_hour": h + 1} for h in range(8, 15)]
        assert "plan_id" in manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=seven)
        eight = seven + [{"day_of_week": "Monday", "start_hour": 20, "end_hour": 21}]
        assert "At most 7 ad_schedule windows on Monday" in _text(
            manage.update_microsoft_campaign_targeting(config, campaign_id=CAMPAIGN, ad_schedule=eight))


class TestHandlerTable:
    def test_every_operation_has_a_check_and_an_apply(self):
        assert manage.OPERATIONS == frozenset(manage.HANDLERS)
        for op, handler in manage.HANDLERS.items():
            assert op.startswith("microsoft_") and callable(handler.preflight) and callable(handler.apply)

    def test_an_unknown_operation_is_refused(self, config):
        from adloop.safety.preview import ChangePlan

        plan = ChangePlan(operation="microsoft_unknown", apply_only_payload={"ad_account_id": ACCOUNT})
        with pytest.raises(ValueError, match="Unknown Microsoft operation"):
            manage.preflight(config, plan)
        with pytest.raises(ValueError, match="Unknown Microsoft operation"):
            manage.apply_plan(config, plan)
