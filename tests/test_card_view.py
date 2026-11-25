"""The approval card's readable view of a plan (safety/card_view.py)."""

import json
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastmcp import Client
from mcp.client import advertise

from adloop import runtime, server
from adloop.ads import currency as currency_module
from adloop.config import AdLoopConfig, SafetyConfig
from adloop.safety import approval
from adloop.safety.approval import META_KEY, UI_EXTENSION_ID, card_html
from adloop.safety.card_view import MAX_ITEMS, card_view, label, lookup_ids
from adloop.safety.preview import ChangePlan

CARDS = [advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})]


class Account:
    """A lookup that knows one account."""

    def __init__(self, currency="EUR", names=None):
        self._currency, self._names = currency, names or {}
        self.currency_calls = 0

    def currency(self):
        self.currency_calls += 1
        return self._currency

    def name(self, kind, entity_id):
        return self._names.get((kind, entity_id), "")


def rows(changes, lookup=None):
    return card_view(changes, lookup)["rows"]


def by_label(changes, lookup=None):
    return {row.get("label", "unset"): row for row in rows(changes, lookup)}


# -- the card that started this: a new ad group with keywords ---------------


def test_a_new_ad_group_reads_as_a_campaign_a_name_and_a_keyword_table():
    changes = {
        "campaign_id": "5550001",
        "ad_group_name": "Analytics tools",
        "keywords": [{"text": "analytics tool", "match_type": "PHRASE"},
                     {"text": "web analytics tool", "match_type": "PHRASE"}],
        "cpc_bid_micros": 0,
    }
    names = {("campaign", "5550001"): "Search - Tools"}
    assert rows(changes, Account(names=names)) == [
        {"label": "Campaign", "kind": "text", "text": "Search - Tools", "hint": "ID 5550001"},
        {"label": "Ad group name", "kind": "text", "text": "Analytics tools"},
        {"label": "Keywords", "kind": "table", "columns": ["Text", "Match type"],
         "rows": [["analytics tool", "PHRASE"], ["web analytics tool", "PHRASE"]]},
        {"kind": "unset", "labels": ["CPC bid"]},
    ]


def test_without_a_lookup_the_id_stays_an_id():
    assert rows({"campaign_id": "999"}) == [{"label": "Campaign ID", "kind": "text", "text": "999"}]


# -- labels -----------------------------------------------------------------


@pytest.mark.parametrize("key, expected", [
    ("cpc_bid_micros", "CPC bid"),
    ("final_url", "Final URL"),
    ("geo_target_ids", "Geo target IDs"),
    ("firingTriggerId", "Firing trigger ID"),
    ("target_roas", "Target ROAS"),
    # Keys that are data keep their spelling.
    ("USD", "USD"),
    ("LONG_HEADLINE", "LONG HEADLINE"),
    ("Job Close", "Job Close"),
])
def test_labels(key, expected):
    assert label(key) == expected


# -- names next to IDs ------------------------------------------------------


def test_an_id_and_its_name_become_one_row_whichever_comes_first():
    expected = [{"label": "Campaign", "kind": "text", "text": "Brand - DE", "hint": "ID 1001"}]
    assert rows({"campaign_id": "1001", "campaign_name": "Brand - DE"}) == expected
    assert rows({"campaign_name": "Brand - DE", "campaign_id": "1001"}) == expected


def test_a_name_without_an_id_is_a_plain_row():
    assert rows({"campaign_name": "Brand - DE"}) == [
        {"label": "Campaign name", "kind": "text", "text": "Brand - DE"}]


def test_id_lists_get_the_names_the_lookup_knows():
    names = {("campaign", "1"): "Brand"}
    assert rows({"campaign_ids": ["1", "2"]}, Account(names=names)) == [
        {"label": "Campaign IDs", "kind": "list", "items": ["Brand (ID 1)", "2"]}]


def test_lookup_ids_are_the_numeric_ids_shown_without_a_name():
    assert lookup_ids({"campaign_id": "1001", "ad_group_id": "", "campaign_ids": ["7", "7", "x y"]}) == {
        "campaign": ["1001", "7"], "ad_group": []}
    assert lookup_ids({"campaign_id": "1001", "campaign_name": "Brand"}) == {"campaign": [], "ad_group": []}
    # A Reddit ID or anything else that is not a number never reaches a query.
    assert lookup_ids({"campaign_id": "c1 OR 1=1", "ad_group_id": "t5_abc"}) == {"campaign": [], "ad_group": []}


# -- before -> after --------------------------------------------------------


@pytest.mark.parametrize("changes, title", [
    ({"current_daily_budget": 20.0, "daily_budget": 30}, "Daily budget"),
    ({"current_daily_budget": 20.0, "new_daily_budget": 30}, "Daily budget"),
    ({"daily_budget_before": 20.0, "daily_budget_after": 30}, "Daily budget"),
    ({"daily_budget": 30, "previous": {"daily_budget": 20.0}}, "Daily budget"),
    ({"set": {"daily_budget": 30}, "previous": {"daily_budget": 20.0}}, "Daily budget"),
    ({"patch": {"daily_budget": 30}, "before": {"daily_budget": 20.0}}, "Daily budget"),
    ({"changes": {"daily_budget": {"from": 20.0, "to": 30}}}, "Daily budget"),
    ({"display": {"daily_budget": {"from": 20.0, "to": 30}}}, "Daily budget"),
])
def test_every_before_after_convention_becomes_one_change_row(changes, title):
    assert rows(changes, Account()) == [
        {"label": title, "kind": "change", "from": "20.00 EUR", "to": "30.00 EUR"}]


def test_a_before_and_an_after_object_pair_up_field_by_field():
    assert rows({"before": {"paused": False, "name": "Chat"}, "after": {"paused": True, "name": "Chat"}}) == [
        {"label": "Paused", "kind": "change", "from": "No", "to": "Yes"},
        {"label": "Name", "kind": "text", "text": "Chat"}]
    # An "after" on its own is the applier's copy of what the rows above it already say.
    assert rows({"title": "v2", "current_title": "v1", "after": {"title": "v2"}}) == [
        {"label": "Title", "kind": "change", "from": "v1", "to": "v2"}]


def test_status_changes_and_other_irregular_pairs():
    assert rows({"target_status": "PAUSED", "current_status": "ENABLED"}) == [
        {"label": "Status", "kind": "change", "from": "ENABLED", "to": "PAUSED"}]
    assert rows({"audience_mode_before": "not set", "set_audience_mode": "observation"}) == [
        {"label": "Audience mode", "kind": "change", "from": "not set", "to": "observation"}]
    assert rows({"enable_ai_max": True, "ai_max_before": False}) == [
        {"label": "AI max", "kind": "change", "from": "No", "to": "Yes"}]


def test_an_unchanged_value_is_not_dressed_up_as_a_change():
    assert rows({"current_title": "Relaunch", "title": "Relaunch"}) == [
        {"label": "Title", "kind": "text", "text": "Relaunch"}]


def test_a_change_keeps_its_own_currency_and_explains_itself():
    changes = {"display": {"budget": {"from": 20.0, "to": 40.0, "goal_type": "DAILY_SPEND", "currency": "USD"}},
               "patch": {"goal_value": 40000000}, "daily_equivalent": 40.0}
    view = rows(changes, Account(currency="EUR"))
    assert view[0] == {"label": "Budget", "kind": "change", "from": "20.00 USD", "to": "40.00 USD",
                       "hint": "Goal type: DAILY_SPEND"}
    # The amount next to the display is in the display's currency, not the account's.
    assert view[1] == {"label": "Daily equivalent", "kind": "text", "text": "40.00 USD"}
    # The request body is still there, folded.
    assert view[2]["kind"] == "code" and view[2]["folded"] is True and "40000000" in view[2]["text"]


def test_list_changes_show_what_was_added_and_removed():
    changes = {"changes": {"locations": {"from": ["A"], "to": ["B", "C"], "added": ["B", "C"], "removed": ["A"]}}}
    assert rows(changes) == [{"label": "Locations", "kind": "group", "rows": [
        {"label": "Added", "kind": "list", "items": ["B", "C"]},
        {"label": "Removed", "kind": "text", "text": "A"},
    ]}]


def test_list_changes_without_a_diff_show_before_and_after():
    view = rows({"values": ["A", "B", "D"], "previous": {"values": ["A", "B", "C"]}})
    assert view == [{"label": "Values", "kind": "group", "rows": [
        {"label": "Before", "kind": "list", "items": ["A", "B", "C"]},
        {"label": "After", "kind": "list", "items": ["A", "B", "D"]},
    ]}]


def test_a_value_set_for_the_first_time_or_cleared():
    assert rows({"current_ad_schedule": [], "ad_schedule": [
        {"day_of_week": "MONDAY", "start_hour": 8, "start_minute": 0, "end_hour": 17, "end_minute": 30},
        {"day_of_week": "TUESDAY", "start_hour": 8, "end_hour": 17},
    ]}) == [{"label": "Ad schedule", "kind": "list", "hint": "was not set",
             "items": ["Monday 08:00–17:30", "Tuesday 08:00–17:00"]}]
    assert rows({"current_geo_exclude_ids": ["1", "2"], "geo_exclude_ids": []}) == [
        {"label": "Geo exclude IDs", "kind": "group", "rows": [
            {"label": "Before", "kind": "list", "items": ["1", "2"]},
            {"label": "After", "kind": "text", "text": "–"},
        ]}]


# -- money ------------------------------------------------------------------


def test_amounts_carry_the_account_currency_and_micros_are_divided():
    account = Account(currency="USD")
    view = by_label({"daily_budget": 50, "max_cpc": 1.75, "cpc_bid_micros": 2_500_000, "target_roas": 4.0}, account)
    assert view["Daily budget"]["text"] == "50.00 USD"
    assert view["Max CPC"]["text"] == "1.75 USD"
    assert view["CPC bid"]["text"] == "2.50 USD"
    assert view["Target ROAS"]["text"] == "4"
    assert account.currency_calls == 1  # asked once, however many amounts


def test_the_currency_is_only_looked_up_when_an_amount_needs_it():
    account = Account()
    rows({"campaign_name": "Brand", "keywords": ["a", "b"]}, account)
    rows({"daily_budget": 40.0, "currency": "USD"}, account)
    assert account.currency_calls == 0


def test_a_currency_next_to_the_amount_wins_and_is_not_repeated():
    assert rows({"daily_budget": 40.0, "currency": "USD"}, Account(currency="EUR")) == [
        {"label": "Daily budget", "kind": "text", "text": "40.00 USD"}]
    # With no amount to carry it, the currency is a row of its own.
    assert rows({"name": "Lead", "currency_code": "USD"}) == [
        {"label": "Name", "kind": "text", "text": "Lead"},
        {"label": "Currency code", "kind": "text", "text": "USD"}]


def test_an_unknown_currency_shows_the_number_and_never_a_guess():
    assert rows({"daily_budget": 50}, Account(currency="")) == [
        {"label": "Daily budget", "kind": "text", "text": "50.00"}]


def test_values_are_amounts_only_next_to_a_currency():
    assert by_label({"default_value": 250.0, "currency_code": "USD"})["Default value"]["text"] == "250.00 USD"
    assert by_label({"default_value": 250.0})["Default value"]["text"] == "250"


def test_percentages_and_a_zero_discount():
    assert rows({"percent_off": 15.0, "money_off": 0.0}) == [
        {"label": "Percent off", "kind": "text", "text": "15%"},
        {"kind": "unset", "labels": ["Money off"]}]


def test_a_bid_of_zero_inside_a_change_is_shown():
    assert rows({"current_max_cpc": 1.5, "max_cpc": 0}, Account()) == [
        {"label": "Max CPC", "kind": "change", "from": "1.50 EUR", "to": "0.00 EUR"}]


# -- lists and tables -------------------------------------------------------


def test_one_column_is_a_list_and_a_single_item_is_text():
    headlines = [{"text": "Free Quote", "pinned_field": None}, {"text": "Call Today", "pinned_field": None}]
    assert rows({"headlines": headlines}) == [
        {"label": "Headlines", "kind": "list", "items": ["Free Quote", "Call Today"]}]
    assert rows({"geo_target_ids": ["2276"]}) == [{"label": "Geo target IDs", "kind": "text", "text": "2276"}]


def test_pins_add_a_column():
    headlines = [{"text": "Acme", "pinned_field": "HEADLINE_1"}, {"text": "Call Today", "pinned_field": None}]
    assert rows({"headlines": headlines}) == [
        {"label": "Headlines", "kind": "table", "columns": ["Text", "Pinned field"],
         "rows": [["Acme", "HEADLINE_1"], ["Call Today", ""]]}]


def test_wide_items_become_blocks_headed_by_their_name():
    changes = {"sitelinks": [{"final_url": "https://example.com/p", "link_text": "Pricing",
                              "description1": "Plans", "description2": "From 9 EUR"}]}
    assert rows(changes) == [{"label": "Sitelinks", "kind": "stack", "items": [
        {"title": "Pricing", "lines": ["Final URL: https://example.com/p", "Description1: Plans",
                                       "Description2: From 9 EUR"]}]}]


def test_table_amounts_use_the_currency_of_the_plan():
    changes = {"currency": "USD", "bids": [{"keyword": "plumber", "from": 8.0, "to": 12.0}]}
    assert rows(changes) == [{"label": "Bids", "kind": "table", "columns": ["Keyword", "From", "To"],
                              "rows": [["plumber", "8.00 USD", "12.00 USD"]]}]


def test_long_lists_are_capped_and_say_how_many_are_missing():
    view = rows({"keywords": [f"k{i}" for i in range(MAX_ITEMS + 5)]})[0]
    assert len(view["items"]) == MAX_ITEMS and view["more"] == 5


def test_nested_objects_are_groups_with_their_own_currency():
    changes = {"bidding": {"strategy": "MaxConversions", "target_cpa": 60.0}, "currency": "USD"}
    assert rows(changes) == [{"label": "Bidding", "kind": "group", "rows": [
        {"label": "Strategy", "kind": "text", "text": "MaxConversions"},
        {"label": "Target CPA", "kind": "text", "text": "60.00 USD"}]}]


def test_deep_nesting_ends_in_one_line_or_in_json_but_never_in_nothing():
    deep = {"a": {"b": {"flat": {"added": 0, "removed": 1}, "tree": {"x": {"y": [1, 2]}}}}}
    inner = rows(deep)[0]["rows"][0]["rows"]
    assert inner[0] == {"label": "Flat", "kind": "text", "text": "Added: 0, Removed: 1"}
    assert inner[1]["kind"] == "code" and '"y"' in inner[1]["text"]


# -- what is left out, and what is not --------------------------------------


def test_bookkeeping_is_left_out():
    changes = {"name": "x", "fingerprint": "abc", "after": {"name": "x"}, "pinned": {"name": "w"},
               "resource_name": "customers/1/x/2", "budget_resource": "customers/1/b/3",
               "_full": [1], "warnings": ["careful"], "account": "Acme (1)", "entity_name": "x"}
    assert rows(changes) == [{"label": "Name", "kind": "text", "text": "x"}]


def test_empty_fields_are_named_once_not_dropped():
    assert rows({"name": "x", "target_cpa": None, "keywords": [], "path1": "", "consent": {}}) == [
        {"label": "Name", "kind": "text", "text": "x"},
        {"kind": "unset", "labels": ["Target CPA", "Keywords", "Path1", "Consent"]}]


def test_a_display_is_shown_in_place_of_the_payload_it_describes():
    changes = {"ad_account_id": "a2", "payload": {"name": "Q4", "objective": "CONVERSIONS", "pixel": "p1"},
               "display": {"campaign_name": "Q4", "budget": 20.0, "goal_type": None, "currency": "EUR"}}
    view = rows(changes)
    assert view[:3] == [
        {"label": "Ad account ID", "kind": "text", "text": "a2"},
        {"label": "Campaign name", "kind": "text", "text": "Q4"},
        {"label": "Budget", "kind": "text", "text": "20.00 EUR"}]
    assert view[3]["label"] == "Payload" and view[3]["folded"] is True and '"pixel": "p1"' in view[3]["text"]
    assert view[4] == {"kind": "unset", "labels": ["Goal type"]}


def _leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    elif isinstance(value, str) and value:
        yield value


@pytest.mark.parametrize("changes", [
    {"campaign_id": "1", "ad_group_name": "Shoes", "keywords": [{"text": "buy shoes", "match_type": "EXACT"}]},
    {"tag": {"name": "GA4 lead", "type": "gaawe", "parameter": [{"type": "TEMPLATE", "key": "eventName",
                                                              "value": "lead"}], "firingTriggerId": ["12"]}},
    {"post": {"headline": "Ship faster", "body": "Try it"}, "ad": {"name": "Ad 1"},
     "display": {"headline": "Ship faster", "click_url": "https://example.com"}},
    {"recommendations": [{"recommendation_id": "3", "type": "KEYWORD", "campaign": "customers/1/campaigns/11",
                          "estimated_improvement": {"clicks": 40}}], "note": "Nothing changes."},
    {"diff_from_live": {"tags": {"changed": [{"id": "1", "name": "GA4", "changes": {"parameters": {
        "event_name": {"from": "generate_lead", "to": "lead"}}}}]}}},
])
def test_every_text_a_plan_carries_is_on_the_card(changes):
    shown = json.dumps(card_view(changes), ensure_ascii=False)
    for text in _leaves(changes):
        assert json.dumps(text, ensure_ascii=False)[1:-1] in shown, text


def test_odd_input_does_not_break_the_view():
    for changes in ({}, None, {"a": [[1, 2], [3]]}, {"a": [1, {"b": 2}]}, {1: "x"},
                    {"ad_schedule": [{"day_of_week": "MONDAY", "start_hour": "x", "end_hour": None}]}):
        json.dumps(card_view(changes))


def test_a_lookup_that_fails_costs_only_the_name():
    class Broken:
        def currency(self):
            raise RuntimeError("no API")

        def name(self, kind, entity_id):
            raise RuntimeError("no API")

    assert rows({"campaign_id": "5", "daily_budget": 20}, Broken()) == [
        {"label": "Campaign ID", "kind": "text", "text": "5"},
        {"label": "Daily budget", "kind": "text", "text": "20.00"}]


# -- how the view reaches the card ------------------------------------------


@pytest.fixture
def config(tmp_path):
    runtime.set_default_config(
        AdLoopConfig(safety=SafetyConfig(log_file=str(tmp_path / "audit.log"), require_dry_run=False)))
    currency_module._cache.clear()
    yield
    runtime.set_default_config(None)
    currency_module._cache.clear()


def _plan(**changes) -> ChangePlan:
    return ChangePlan(operation="create_ad_group", entity_type="ad_group", customer_id="1234567890", changes=changes)


def test_names_and_currency_come_from_one_query(config):
    sent = []

    def fake(_config, customer_id, query):
        sent.append((customer_id, query))
        return [{"customer.currency_code": "USD", "campaign.id": 77, "campaign.name": "Brand"}]

    with patch("adloop.ads.gaql.execute_query", side_effect=fake):
        view = approval._card_view(_plan(campaign_id="77", cpc_bid_micros=1_500_000))

    assert view["rows"] == [
        {"label": "Campaign", "kind": "text", "text": "Brand", "hint": "ID 77"},
        {"label": "CPC bid", "kind": "text", "text": "1.50 USD"}]
    assert len(sent) == 1
    assert sent[0][0] == "1234567890" and "campaign.id IN (77)" in sent[0][1]


def test_an_ad_group_query_also_names_its_campaign(config):
    def fake(_config, _customer_id, query):
        assert "FROM ad_group WHERE ad_group.id IN (5)" in query
        return [{"customer.currency_code": "EUR", "campaign.id": 9, "campaign.name": "Brand",
                 "ad_group.id": 5, "ad_group.name": "Shoes"}]

    with patch("adloop.ads.gaql.execute_query", side_effect=fake):
        view = approval._card_view(_plan(campaign_id="9", ad_group_id="5"))

    assert [(r["label"], r["text"], r["hint"]) for r in view["rows"]] == [
        ("Campaign", "Brand", "ID 9"), ("Ad group", "Shoes", "ID 5")]


def test_a_plan_that_needs_nothing_asks_nothing(config):
    with patch("adloop.ads.gaql.execute_query", side_effect=AssertionError("no query expected")):
        view = approval._card_view(_plan(campaign_id="7", campaign_name="Brand", keywords=["a"]))
    assert view["rows"][0]["text"] == "Brand"


def test_other_platforms_never_query_google_ads(config):
    plan = ChangePlan(operation="reddit_update_ad_group", platform="reddit", customer_id="a2_acct",
                      changes={"campaign_id": "123", "daily_budget": 20})
    with patch("adloop.ads.gaql.execute_query", side_effect=AssertionError("no query expected")):
        view = approval._card_view(plan)
    assert [r["text"] for r in view["rows"]] == ["123", "20.00"]


def test_a_failed_lookup_leaves_the_id_and_the_plain_number(config):
    with patch("adloop.ads.gaql.execute_query", side_effect=RuntimeError("API down")):
        view = approval._card_view(_plan(campaign_id="77", cpc_bid_micros=1_500_000))
    assert [(r["label"], r["text"]) for r in view["rows"]] == [("Campaign ID", "77"), ("CPC bid", "1.50")]


def test_get_currency_code_still_falls_back_to_eur(config):
    with patch("adloop.ads.gaql.execute_query", side_effect=RuntimeError("API down")):
        assert currency_module.account_currency(AdLoopConfig(), "1234567890") == ""
        assert currency_module.get_currency_code(AdLoopConfig(), "1234567890") == "EUR"


@pytest.mark.asyncio
async def test_card_apps_get_the_view_next_to_the_code_and_the_model_does_not(config):
    async with Client(server.mcp, extensions=CARDS) as client:
        result = await client.call_tool(
            "pause_entity", {"entity_type": "campaign", "entity_id": "123", "customer_id": "1234567890"},
            raise_on_error=False)
    meta = result.meta[META_KEY]
    assert meta["code"] and meta["view"]["rows"] == [{"label": "Target status", "kind": "text", "text": "PAUSED"}]
    assert "view" not in result.structured_content
    assert '"rows"' not in result.content[0].text


@pytest.mark.asyncio
async def test_a_plan_the_view_cannot_describe_still_gets_its_card(config, monkeypatch):
    import adloop.safety.card_view as card_view_module

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(card_view_module, "card_view", broken)
    async with Client(server.mcp, extensions=CARDS) as client:
        result = await client.call_tool(
            "pause_entity", {"entity_type": "campaign", "entity_id": "123", "customer_id": "1234567890"},
            raise_on_error=False)
    meta = result.meta[META_KEY]
    assert meta["code"] and "view" not in meta
    assert result.structured_content["approval"] == "card"


# -- the card itself --------------------------------------------------------


def test_the_card_draws_every_kind_of_row_and_keeps_the_old_listing():
    html = card_html()
    for kind in ("change", "list", "table", "stack", "group", "code", "unset"):
        assert f'row.kind === "{kind}"' in html, kind
    assert "view: meta.view" in html
    # An older server sends no view: the raw fields are still listed.
    assert "forEach(([k, v]) => row(dl, k, v))" in html
    # The card only ever writes text, never markup, from a plan.
    assert "innerHTML" not in html


def test_the_card_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = re.search(r"<script>(.*)</script>", card_html(), re.S).group(1)
    path = tmp_path / "card.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
