"""What the approval card shows: a plan's changes as readable rows.

A preview's ``changes`` is written for the applier and for the model: API
field names, IDs, micros, nested payloads. The card is read by the person
who approves, so this module turns the same data into rows the card renders
without knowing any operation: text, before → after, lists and tables.

The rules follow the conventions the write tools already use for their
previews (``current_x`` next to ``x``, ``x_before`` / ``x_after``,
``previous``, ``before`` + ``patch``, ``{"from": ..., "to": ...}`` maps,
``x_id`` next to ``x_name``), so a new tool that keeps to them gets a
readable card without a line here.

Nothing a person approves is dropped silently. Only bookkeeping is left out:
what the applier needs to check the plan (fingerprints, resource names, the
pinned earlier state) and the earlier values of fields that do not change.
A raw request body next to a ``display`` is kept, folded. Fields without a
value are named in one "not set" row, and a value no rule fits is shown as
it is.

This module does no I/O. Names and the account currency that a preview does
not carry come from a ``Lookup`` the caller provides.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

MAX_ITEMS = 200
_MAX_TEXT = 500
_MAX_DEPTH = 2

# Needed by the applier, meaningless to a reader.
_INTERNAL = frozenset({
    "fingerprint", "resource_name", "sha256", "workspace_digest",
    "target_fingerprint", "target_version_path",
})
# Top level only: shown elsewhere on the card, or the applier's own notes.
_TOP_SKIP = frozenset({"account", "entity_name", "warnings", "after", "pinned"})
# The request body of a plan that also carries a ``display`` in plain terms.
# The display comes first; the body stays available, folded, because a
# display may leave fields out.
_RAW_WITH_DISPLAY = frozenset({"payload", "patch", "post_patch", "post", "ad"})
# Before/after pairs whose names follow no pattern: new key -> (old key, label).
_NAMED_PAIRS = {
    "target_status": ("current_status", "status"),
    "set_audience_mode": ("audience_mode_before", "audience_mode"),
    "enable_ai_max": ("ai_max_before", "ai_max"),
    "asset_automation_settings": ("asset_automation_before", "asset_automation_settings"),
}
_CURRENCY_KEYS = ("currency", "currency_code")
# Amounts in the account's currency (or the one named next to them).
_MONEY = re.compile(
    r"(?:^|_)(?:budget|bid|bid_value|cpc|cpa|cpm|cpv|spend_cap|monthly_maximum|"
    r"daily_equivalent|money_off|orders_over_amount|price)$|_micros$"
)
# Amounts only when a currency stands next to them.
_MONEY_WITH_CURRENCY = frozenset({"default_value", "numeric_value", "value", "amount"})
_PERCENT = re.compile(r"(?:^|_)percent_off$|_pct$|_percent$")
_ACRONYMS = {
    "id": "ID", "ids": "IDs", "url": "URL", "urls": "URLs", "cpc": "CPC", "cpa": "CPA",
    "cpm": "CPM", "cpv": "CPV", "roas": "ROAS", "rsa": "RSA", "ai": "AI", "ga4": "GA4",
    "gtm": "GTM", "utm": "UTM", "pmax": "PMax", "kb": "KB", "html": "HTML", "mime": "MIME",
    "os": "OS",
}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# The field that heads an item shown as a block, when it has one.
_HEADING_KEYS = frozenset({"name", "title", "text", "headline", "header", "keyword", "link_text"})
# Names a lookup can add to a bare ID: changes key -> kind.
_LOOKUP_IDS = {"campaign_id": "campaign", "ad_group_id": "ad_group"}
_LOOKUP_ID_LISTS = {
    "campaign_ids": "campaign", "ad_group_ids": "ad_group",
    "geo_target_ids": "geo", "geo_exclude_ids": "geo", "preserved_negative_geo_target_ids": "geo",
    "language_ids": "language",
}
# Fields whose API name says less than what they hold.
_LABELS = {
    "geo_target_ids": "Locations", "geo_exclude_ids": "Excluded locations",
    "preserved_negative_geo_target_ids": "Excluded locations kept", "language_ids": "Languages",
}
# Fields that hold one of the platform's fixed values (PHRASE, MAXIMIZE_CONVERSIONS).
_ENUM_KEYS = frozenset({
    "status", "type", "category", "scope", "level", "objective", "occasion", "unit", "strategy", "kind",
    "channel", "network", "day_of_week", "call_to_action", "bid_type", "price_qualifier", "discount_modifier",
})
_ENUM_SUFFIXES = ("_status", "_type", "_types", "_option", "_strategy", "_mode", "_method", "_priority",
                  "_field", "_model", "_level", "_category", "_on_create")
_ENUM_VALUE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
# Google's name for a value, where its API name would mislead.
_ENUM_NAMES = {"TARGET_SPEND": "Maximize clicks"}
# A row the title of the card already says.
_SAID_BY_TITLE = {"remove_entity": frozenset({"action"})}
# What a removal names when the entity type is API vocabulary.
_ENTITY_NAMES = {
    "shared_criterion": "keyword from list", "campaign_criterion": "campaign targeting",
    "ad_group_criterion": "ad group targeting", "campaign_asset": "asset from campaign",
    "ad_group_asset": "asset from ad group", "customer_asset": "asset from account",
}
# Microsoft's previews write a pin into the headline: "Acme [pinned Headline1]".
_PINNED_SUFFIX = re.compile(r"\s*\[pinned ([A-Za-z]+?)(\d)\]$")
_PLATFORM_PREFIX = re.compile(r"^(?:reddit|microsoft|chatgpt|gtm|ga4)_")
_TRAILING_NUMBER = re.compile(r"^([a-z]+)(\d+)$")

# What happens when the change is applied, in the card's two languages.
# Only where the behaviour is fixed and the preview does not already warn
# about it.
_CREATED_PAUSED = (
    "The campaign is created paused. Nothing serves until you enable it.",
    "Die Kampagne wird pausiert angelegt. Es wird nichts ausgespielt, bis du sie aktivierst.",
)
_ASSETS_REVIEWED = (
    "The assets are created and linked. Google reviews them before they show with your ads.",
    "Die Assets werden angelegt und verknüpft. Google prüft sie, bevor sie mit deinen Anzeigen erscheinen.",
)
_EFFECTS: dict[str, tuple[str, str]] = {
    "create_campaign": _CREATED_PAUSED,
    "create_shopping_campaign": _CREATED_PAUSED,
    "create_ad_group": (
        "The ad group is created enabled, but it serves nothing until one of its ads is enabled.",
        "Die Anzeigengruppe wird aktiv angelegt, spielt aber nichts aus, bis eine ihrer Anzeigen aktiviert ist.",
    ),
    "create_responsive_search_ad": (
        "The ad is created paused. Once you enable it, Google reviews it before it serves.",
        "Die Anzeige wird pausiert angelegt. Sobald du sie aktivierst, prüft Google sie, bevor sie ausgespielt wird.",
    ),
    "add_keywords": (
        "The keywords are added enabled. In an ad group that is serving, they can trigger ads right away.",
        "Die Keywords werden aktiv hinzugefügt. In einer laufenden Anzeigengruppe können sie sofort Anzeigen auslösen.",
    ),
    "add_negative_keywords": (
        "Once applied, ads in this campaign no longer show for searches that match these keywords.",
        "Nach dem Anwenden erscheinen Anzeigen dieser Kampagne nicht mehr bei Suchanfragen, die zu diesen "
        "Keywords passen.",
    ),
    "add_to_negative_keyword_list": (
        "Once applied, every campaign that uses this list stops showing for searches that match these keywords.",
        "Nach dem Anwenden erscheinen alle Kampagnen mit dieser Liste nicht mehr bei Suchanfragen, die zu diesen "
        "Keywords passen.",
    ),
    "pause_entity": (
        "It stops serving as soon as this is applied. You can enable it again at any time.",
        "Die Auslieferung stoppt, sobald das angewendet ist. Du kannst es jederzeit wieder aktivieren.",
    ),
    "enable_entity": (
        "It can serve as soon as this is applied, provided everything above it is enabled too.",
        "Es kann ausgeliefert werden, sobald das angewendet ist, sofern alles Übergeordnete ebenfalls aktiv ist.",
    ),
    "remove_entity": (
        "This removes it for good and cannot be undone. Pausing is the reversible choice.",
        "Das entfernt es endgültig und lässt sich nicht rückgängig machen. Pausieren ist die umkehrbare Wahl.",
    ),
    "create_sitelinks": _ASSETS_REVIEWED,
    "create_callouts": _ASSETS_REVIEWED,
    "create_structured_snippets": _ASSETS_REVIEWED,
}
_RSA_KEEPS_HISTORY = (
    "The ad keeps its ID, its history and its learning.",
    "Die Anzeige behält ihre ID, ihren Verlauf und ihre Lernphase.",
)


class Lookup(Protocol):
    """Facts about the account that a preview does not carry."""

    def currency(self) -> str:
        """The account's currency code, "" when unknown."""
        ...

    def name(self, kind: str, entity_id: str) -> str:
        """The name of a campaign or ad group, "" when unknown."""
        ...


class _NoLookup:
    def currency(self) -> str:
        return ""

    def name(self, kind: str, entity_id: str) -> str:
        return ""


def lookup_ids(changes: dict[str, Any]) -> dict[str, list[str]]:
    """The campaign and ad group IDs a card would show without a name."""
    found: dict[str, list[str]] = {"campaign": [], "ad_group": [], "geo": [], "language": []}

    def add(kind: str, value: Any) -> None:
        text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        if text.isdigit() and text not in found[kind]:
            found[kind].append(text)

    for key, kind in _LOOKUP_IDS.items():
        if not _has_value(changes.get(f"{kind}_name")):
            add(kind, changes.get(key))
    for key, kind in _LOOKUP_ID_LISTS.items():
        for values in (changes.get(key), changes.get("current_" + key)):
            for value in values if isinstance(values, list) else ():
                add(kind, value)
    return found


def card_view(
    changes: dict[str, Any], lookup: Lookup | None = None, *, operation: str = "", entity_type: str = "",
) -> dict[str, Any]:
    """What an approval card shows for a plan: its title, its rows and what applying does."""
    changes = changes or {}
    builder = _Builder(lookup or _NoLookup())
    hidden = _SAID_BY_TITLE.get(operation, frozenset())
    rows = builder.rows({k: v for k, v in changes.items() if k not in hidden}, currency="", depth=0)
    if (ad := _ad_example(changes)) is not None:
        rows.insert(0, ad)
    view: dict[str, Any] = {"rows": rows}
    if title := _title(operation, entity_type, changes):
        view["title"] = title
    if effect := _effect(operation, changes):
        view["effect"] = {"en": effect[0], "de": effect[1]}
    return view


def _title(operation: str, entity_type: str, changes: dict) -> str:
    """'pause_entity' on a campaign -> 'Pause campaign'."""
    action = _PLATFORM_PREFIX.sub("", operation or "")
    if not action:
        return ""
    noun = _ENTITY_NAMES.get(entity_type or "") or (entity_type or "").replace("_", " ").strip()
    if action == "set_status":
        target = str(changes.get("target_status") or "").upper()
        verb = {"PAUSED": "pause", "ACTIVE": "enable", "ENABLED": "enable"}.get(target)
        action = f"{verb}_entity" if verb else action
    words = [w for w in action.split("_") if w]
    if "entity" in words and noun:
        words = [noun if w == "entity" else w for w in words]
    text = " ".join(_ACRONYMS.get(w, w) for w in words).replace("AI max", "AI Max")
    return text[:1].upper() + text[1:]


def _effect(operation: str, changes: dict) -> tuple[str, str] | None:
    if operation == "update_responsive_search_ad":
        # New text has its own warning on the preview; a URL or path edit is the free one.
        return None if ("headlines" in changes or "descriptions" in changes) else _RSA_KEEPS_HISTORY
    if operation in ("pause_entity", "enable_entity"):
        current, target = changes.get("current_status"), changes.get("target_status")
        if current and current == target:
            return None  # nothing changes, and the preview says so
    return _EFFECTS.get(operation)


def _texts(items: Any) -> list[tuple[str, str]]:
    """(text, pin) for each headline or description, whichever way it is written."""
    out = []
    for item in items if isinstance(items, list) else ():
        if isinstance(item, dict):
            text, pin = item.get("text"), item.get("pinned_field") or ""
        else:
            text, pin = item, ""
        if isinstance(text, str) and text.strip():
            if match := _PINNED_SUFFIX.search(text):
                text, pin = text[: match.start()], f"{match.group(1).upper()}_{match.group(2)}"
            out.append((text.strip(), str(pin)))
    return out


def _pick(assets: list[tuple[str, str]], slots: list[str]) -> list[str]:
    """One text per slot: the one pinned there, else the next unpinned one."""
    free = [text for text, pin in assets if not pin]
    picked = []
    for slot in slots:
        pinned = next((text for text, pin in assets if pin == slot), None)
        if pinned is None and free:
            pinned = free.pop(0)
        if pinned is not None and pinned not in picked:
            picked.append(pinned)
    return picked


def _ad_example(changes: dict) -> dict | None:
    """A search ad as it can appear, when the plan holds or changes one."""
    if "headlines" not in changes and "descriptions" not in changes and "_unchanged" not in changes:
        return None
    rest = changes.get("_unchanged") if isinstance(changes.get("_unchanged"), dict) else {}

    def now(key: str) -> Any:
        return changes[key] if key in changes else rest.get(key)

    headlines = _pick(_texts(now("headlines")), ["HEADLINE_1", "HEADLINE_2", "HEADLINE_3"])
    descriptions = _pick(_texts(now("descriptions")), ["DESCRIPTION_1", "DESCRIPTION_2"])
    url = now("final_url")
    if not headlines or not descriptions or not isinstance(url, str) or not url.strip():
        return None
    host = re.sub(r"^[a-z]+://", "", url.strip(), flags=re.I).split("/")[0].split("?")[0]
    host = host[4:] if host.lower().startswith("www.") else host
    paths = [str(p).strip() for p in (now("path1"), now("path2")) if isinstance(p, str) and p.strip()]
    return {
        "label": "Example", "kind": "ad",
        "url": _clip("/".join([host, *paths])),
        "headline": _clip(" | ".join(headlines)),
        "text": _clip(" ".join(descriptions)),
        "hint": "one of the combinations Google can show",
    }


def label(key: Any) -> str:
    """'cpc_bid_micros' -> 'CPC bid', 'firingTriggerId' -> 'Firing trigger ID'."""
    text = str(key)
    if text in _LABELS:
        return _LABELS[text]
    # A key that is data (a currency code, an asset type, a conversion name)
    # is not a field name: leave its spelling alone.
    if " " in text or not any(c.islower() for c in text):
        return text.replace("_", " ")
    if text.endswith("_micros"):
        text = text[: -len("_micros")]
    words = _CAMEL.sub("_", text).lower().split("_")
    # "path1" reads as "path 1"; "ga4" stays what it is.
    words = [_ACRONYMS.get(w) or _TRAILING_NUMBER.sub(r"\1 \2", w) for w in words if w]
    if not words:
        return str(key)
    if words[0] not in _ACRONYMS.values():
        words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words).replace("AI max", "AI Max")


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _single(value: Any) -> Any:
    """A list holding one plain value is that value; an empty one is nothing."""
    if isinstance(value, list):
        if not value:
            return None
        if len(value) == 1 and not isinstance(value[0], (dict, list)):
            return value[0]
    return value


def _list_diff(old: Any, new: Any) -> tuple[list, list, int] | None:
    """(added, removed, unchanged count) for two lists that share items, else None.

    Lists of plain values, or of ``{"text": ...}`` items that carry nothing
    else: a pin on a kept headline is a change this could not show, so
    pinned lists are left to the full before and after.
    """
    if not (isinstance(old, list) and isinstance(new, list)):
        return None

    def plain(items: list) -> list[str] | None:
        out = []
        for item in items:
            if isinstance(item, dict):
                if any(_has_value(v) for k, v in item.items() if k != "text") or not isinstance(item.get("text"), str):
                    return None
                item = item["text"]
            elif isinstance(item, (list, bool)) or item is None:
                return None
            out.append(str(item))
        return out

    before, after = plain(old), plain(new)
    if before is None or after is None:
        return None
    kept = [item for item in after if item in before]
    if not kept:
        return None
    return [i for i in after if i not in before], [i for i in before if i not in after], len(kept)


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _number(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_enum_key(key: str) -> bool:
    return key in _ENUM_KEYS or key.endswith(_ENUM_SUFFIXES)


def _enum(value: str) -> str:
    """'MAXIMIZE_CONVERSIONS' -> 'Maximize conversions'; anything else as it is."""
    if value in _ENUM_NAMES:
        return _ENUM_NAMES[value]
    if len(value) < 2 or not _ENUM_VALUE.match(value):
        return _clip(value)
    words = [_ACRONYMS.get(w, w) for w in value.lower().split("_")]
    if words[0] not in _ACRONYMS.values():
        words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)


def _is_schedule(value: Any) -> bool:
    return isinstance(value, dict) and {"day_of_week", "start_hour", "end_hour"} <= value.keys()


def _schedule(value: dict) -> str:
    def clock(prefix: str) -> str:
        try:
            return f"{int(value.get(prefix + '_hour') or 0):02d}:{int(value.get(prefix + '_minute') or 0):02d}"
        except (TypeError, ValueError):
            return str(value.get(prefix + "_hour"))

    return f"{str(value['day_of_week']).capitalize()} {clock('start')}–{clock('end')}"


def _is_change(value: Any) -> bool:
    return isinstance(value, dict) and "to" in value


def _is_change_map(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(_is_change(v) for v in value.values())


def _local_currency(data: dict, inherited: str) -> str:
    for key in _CURRENCY_KEYS:
        code = data.get(key)
        if isinstance(code, str) and code.strip():
            return code.strip()
    return inherited


class _Builder:
    def __init__(self, lookup: Lookup) -> None:
        self._lookup = lookup
        self._account_currency: str | None = None
        self._amounts = 0  # amounts written so far, to know whether a currency was shown

    # -- scalars ----------------------------------------------------------

    def _currency(self, local: str) -> str:
        if local:
            return local
        if self._account_currency is None:
            try:
                self._account_currency = str(self._lookup.currency() or "")
            except Exception:
                self._account_currency = ""
        return self._account_currency

    def _name(self, kind: str, entity_id: Any) -> str:
        try:
            return str(self._lookup.name(kind, str(entity_id)) or "")
        except Exception:
            return ""

    def _money_kind(self, key: str, currency: str, parent: str = "") -> str:
        """'money', 'percent' or '' for the number stored under ``key``."""
        if _PERCENT.search(key):
            return "percent"
        if _MONEY.search(key):
            return "money"
        if key in _MONEY_WITH_CURRENCY and (currency or _MONEY.search(parent.rstrip("s"))):
            return "money"
        if key in ("from", "to") and parent:
            return self._money_kind(parent.rstrip("s"), currency)
        return ""

    def _amount(self, key: str, value: int | float, currency: str) -> str:
        amount = value / 1_000_000 if key.endswith("_micros") else value
        code = self._currency(currency)
        self._amounts += 1
        return f"{amount:.2f} {code}".rstrip()

    def scalar(self, key: str, value: Any, currency: str, parent: str = "") -> str:
        """One value as text; "" when it has none."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if _is_number(value):
            kind = self._money_kind(key, currency, parent)
            if kind == "money":
                return self._amount(key if key not in ("from", "to") else parent, value, currency)
            if kind == "percent":
                return _number(value) + "%"
            return _number(value)
        if isinstance(value, str) and _is_enum_key(key if key not in ("from", "to") else parent):
            return _enum(value)
        return _clip(str(value))

    def _named(self, key: str, value: Any, currency: str) -> str:
        """A value as text, with its name when it is an ID of a list the lookup knows."""
        text = self.scalar(key, value, currency)
        kind = _LOOKUP_ID_LISTS.get(key)
        if kind and text and (found := self._name(kind, value)):
            return f"{found} (ID {text})"
        return text

    def inline(self, key: str, value: Any, currency: str, parent: str = "") -> str:
        """Any value on one line, for a table cell or a nested level."""
        if _is_schedule(value):
            return _schedule(value)
        if isinstance(value, dict):
            currency = _local_currency(value, currency)
            parts = [
                f"{label(k)}: {self.inline(str(k), v, currency, key)}"
                for k, v in value.items()
                if str(k) not in _INTERNAL and _has_value(v)
            ]
            return _clip(", ".join(parts))
        if isinstance(value, list):
            return _clip(", ".join(self.inline(key, v, currency, parent) for v in value if _has_value(v)))
        return self.scalar(key, value, currency, parent)

    # -- one field --------------------------------------------------------

    def value_rows(self, key: str, value: Any, currency: str, depth: int, name: str = "") -> list[dict]:
        """The row for one field: [] when it has no value."""
        title = name or label(key)
        if not _has_value(value):
            return []
        if _is_number(value) and value == 0 and self._money_kind(key, currency) in ("money", "percent"):
            # No bid, no discount: these read as "none", not as an amount of 0.
            return []
        if _is_schedule(value):
            return [{"label": title, "kind": "text", "text": _schedule(value)}]
        if _is_change(value):
            return self.change_rows(key, value.get("from"), value["to"], _local_currency(value, currency), depth,
                                    extra=value, name=name)
        if isinstance(value, dict):
            if depth >= _MAX_DEPTH:
                if not any(isinstance(v, (dict, list)) for v in value.values()):
                    return [{"label": title, "kind": "text", "text": self.inline(key, value, currency)}]
                return [self._code(title, value)]
            rows = self.rows(value, _local_currency(value, currency), depth + 1, parent=key)
            return [{"label": title, "kind": "group", "rows": rows}] if rows else []
        if isinstance(value, list):
            return self._list_rows(key, title, value, currency)
        return [{"label": title, "kind": "text", "text": self.scalar(key, value, currency)}]

    @staticmethod
    def _code(title: str, value: Any, folded: bool = False) -> dict:
        row = {"label": title, "kind": "code", "text": _clip(json.dumps(value, indent=2, default=str))}
        if folded:
            row["folded"] = True
        return row

    def _list_rows(self, key: str, title: str, values: list, currency: str) -> list[dict]:
        values = [v for v in values if _has_value(v)]
        if not values:
            return []
        more = max(0, len(values) - MAX_ITEMS)
        shown = values[:MAX_ITEMS]
        if all(isinstance(v, dict) and not _is_schedule(v) for v in shown):
            return [self._table(key, title, shown, currency, more)]
        items = [
            self._named(key, v, currency) if not isinstance(v, (dict, list)) else self.inline(key, v, currency)
            for v in shown
        ]
        if len(items) == 1 and not more:
            return [{"label": title, "kind": "text", "text": items[0]}]
        row: dict = {"label": title, "kind": "list", "items": items}
        if more:
            row["more"] = more
        return [row]

    def _table(self, key: str, title: str, items: list[dict], currency: str, more: int) -> dict:
        columns: list[str] = []
        for item in items:
            for k, v in item.items():
                if str(k) not in _INTERNAL and not str(k).startswith("_") and _has_value(v) and str(k) not in columns:
                    columns.append(str(k))

        def cell(item: dict, column: str) -> str:
            return self.inline(column, item.get(column), _local_currency(item, currency), key)

        row: dict
        if len(columns) <= 1:
            row = {"label": title, "kind": "list",
                   "items": [cell(item, columns[0]) if columns else "" for item in items]}
        elif len(columns) <= 3:
            row = {"label": title, "kind": "table", "columns": [label(c) for c in columns],
                   "rows": [[cell(item, c) for c in columns] for item in items]}
        else:
            # Too wide for a card: one block per item, headed by its name.
            head = next((c for c in columns if c in _HEADING_KEYS or c.endswith("_name")), columns[0])
            row = {"label": title, "kind": "stack", "items": [
                {"title": cell(item, head) or "–",
                 "lines": [f"{label(c)}: {cell(item, c)}" for c in columns if c != head and _has_value(item.get(c))]}
                for item in items
            ]}
        if more:
            row["more"] = more
        return row

    def change_rows(self, key: str, old: Any, new: Any, currency: str, depth: int,
                    extra: dict | None = None, name: str = "") -> list[dict]:
        """Before → after for one field."""
        title = name or label(key)
        extra = extra or {}
        if not _has_value(old) and not _has_value(new):
            return []
        added, removed = extra.get("added"), extra.get("removed")
        old, new = _single(old), _single(new)
        simple = not isinstance(old, (dict, list)) and not isinstance(new, (dict, list))
        if simple:
            if old == new:
                return self.value_rows(key, new, currency, depth, name=name)
            row = {"label": title, "kind": "change",
                   "from": self._named(key, old, currency), "to": self._named(key, new, currency)}
            hint = ", ".join(
                f"{label(k)}: {self.scalar(str(k), v, currency)}"
                for k, v in extra.items()
                if k not in ("from", "to", "added", "removed", *_CURRENCY_KEYS)
                and _has_value(v) and not isinstance(v, (dict, list))
            )
            if hint:
                row["hint"] = _clip(hint)
            return [row]
        diff = None if (isinstance(added, list) or isinstance(removed, list)) else _list_diff(old, new)
        if diff is not None:
            added, removed, kept = diff
            rows = [r for part, value in (("added", added), ("removed", removed))
                    for r in self.value_rows(key, value, currency, depth + 1, name=label(part))]
            rows.append({"label": "Unchanged", "kind": "text", "text": str(kept)})
            return [{"label": title, "kind": "group", "rows": rows}]
        if isinstance(added, list) or isinstance(removed, list):
            parts = [("added", added), ("removed", removed)]
        elif old == new:
            return self.value_rows(key, new, currency, depth, name=name)
        elif not _has_value(old):
            # Nothing before: the new value alone, marked as new.
            rows = self.value_rows(key, new, currency, depth, name=name)
            if rows:
                rows[0] = {**rows[0], "hint": "was not set"}
            return rows
        else:
            parts = [("before", old), ("after", new)]
        rows: list[dict] = []
        for part, value in parts:
            found = self.value_rows(key, value, currency, depth + 1, name=label(part))
            if not found and part in ("before", "after"):
                found = [{"label": label(part), "kind": "text", "text": "–"}]
            rows.extend(found)
        return [{"label": title, "kind": "group", "rows": rows}] if rows else []

    # -- one level --------------------------------------------------------

    def rows(self, data: dict, currency: str, depth: int, parent: str = "") -> list[dict]:
        top = depth == 0
        data = {str(k): v for k, v in data.items()}
        currency = _local_currency(data, currency)
        display = data.get("display") if isinstance(data.get("display"), dict) else None
        if display is not None:
            # Amounts next to the display (a daily equivalent) are in its currency.
            currency = _local_currency(display, currency)
            for change in display.values():
                if _is_change(change):
                    currency = _local_currency(change, currency)
        previous = data.get("previous") if isinstance(data.get("previous"), dict) else {}
        changed = data.get("set") if isinstance(data.get("set"), dict) and previous else {}
        patch, before = data.get("patch"), data.get("before")
        patched = isinstance(patch, dict) and isinstance(before, dict) and display is None
        # A plain before/after pair of objects: one change per field of "after".
        after = data.get("after")
        paired = isinstance(before, dict) and isinstance(after, dict) and not patched

        # new key -> (old key, label key)
        pairs: dict[str, tuple[str, str]] = {}
        for key in data:
            if key.startswith("current_"):
                base = key[len("current_"):]
                for new_key in (base, "new_" + base):
                    if new_key in data:
                        pairs[new_key] = (key, base)
                        break
            elif key.endswith("_before") and key[: -len("_before")] + "_after" in data:
                base = key[: -len("_before")]
                pairs[base + "_after"] = (key, base)
        for new_key, (old_key, name) in _NAMED_PAIRS.items():
            if new_key in data and old_key in data:
                pairs[new_key] = (old_key, name)
        old_keys = {old for old, _ in pairs.values()}

        out: list[dict] = []
        unset: list[str] = []
        done: set[str] = set()
        folded: list[dict] = []
        amounts_before = self._amounts

        def emit(key: str, rows: list[dict]) -> None:
            if rows:
                out.extend(rows)
            else:
                unset.append(label(key))

        for key, value in data.items():
            if key in done or key in old_keys or key in _INTERNAL or key.startswith("_") or key.endswith("_resource"):
                continue
            if key == "after" and paired:
                for field, new in after.items():
                    emit(field, self.change_rows(field, before.get(field), new, currency, depth))
                continue
            if top and key in _TOP_SKIP:
                continue
            if key in _CURRENCY_KEYS:
                continue  # decided after the loop: only shown when no amount carries it
            if display is not None and key in _RAW_WITH_DISPLAY:
                if _has_value(value):
                    folded.append(self._code(label(key), value, folded=True))
                continue
            if key == "property_id" and _has_value(data.get("property")):
                continue
            if (key == "previous" and previous) or (key == "before" and (patched or paired)):
                continue
            if key == "display" and display is not None:
                if _is_change_map(display):
                    for field, change in display.items():
                        emit(field, self.value_rows(field, change, currency, depth))
                else:
                    for row in self.rows(display, currency, depth, parent=parent):
                        if row["kind"] == "unset":
                            unset.extend(row["labels"])
                        else:
                            out.append(row)
                continue
            if key == "changes" and _is_change_map(value):
                for field, change in value.items():
                    emit(field, self.value_rows(field, change, currency, depth))
                continue
            if key == "patch" and patched:
                for field, new in patch.items():
                    emit(field, self.change_rows(field, before.get(field), new, currency, depth))
                continue
            if key == "set" and changed:
                for field, new in changed.items():
                    emit(field, self.change_rows(field, previous.get(field), new, currency, depth))
                continue
            if key in pairs:
                old_key, name = pairs[key]
                emit(name, self.change_rows(name, data.get(old_key), value, currency, depth))
                continue
            if key in previous and key not in changed:
                emit(key, self.change_rows(key, previous.get(key), value, currency, depth))
                continue
            merged = self._named_id(key, data, done)
            if merged is not None:
                out.append(merged)
                continue
            emit(key, self.value_rows(key, value, currency, depth))

        for key in _CURRENCY_KEYS:
            if _has_value(data.get(key)) and self._amounts == amounts_before:
                out.append({"label": label(key), "kind": "text", "text": str(data[key])})
        out.extend(folded)
        if unset:
            out.append({"kind": "unset", "labels": unset})
        return out

    def _named_id(self, key: str, data: dict, done: set[str]) -> dict | None:
        """'campaign_id' + 'campaign_name' as one row: the name, with the ID as a hint."""
        if key.endswith("_id"):
            base, id_key, name_key = key[:-3], key, key[:-3] + "_name"
        elif key.endswith("_name"):
            base, id_key, name_key = key[:-5], key[:-5] + "_id", key
        else:
            return None
        entity_id = data.get(id_key)
        if not _has_value(entity_id) or isinstance(entity_id, (dict, list)):
            return None
        name = data.get(name_key)
        if not (isinstance(name, str) and name.strip()):
            kind = _LOOKUP_IDS.get(id_key)
            name = self._name(kind, entity_id) if kind else ""
            if not name:
                return None
        done.update({id_key, name_key})
        return {"label": label(base), "kind": "text", "text": _clip(str(name)), "hint": f"ID {entity_id}"}
