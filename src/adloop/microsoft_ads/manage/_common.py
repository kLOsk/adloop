"""Constants and helpers every Microsoft Advertising management area shares."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from adloop.microsoft_ads.auth import MicrosoftAdsApiError
from adloop.microsoft_ads.client import campaign_request, fault_details
from adloop.microsoft_ads.read import (
    CONVERSION_BASED_STRATEGIES,
    _ad_groups,
    _conversion_goals,
    _customer,
    _keywords,
    account_meta,
)
from adloop.microsoft_ads.write import (
    find_entity,
    store_plan_preview,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan


# The strategies a draft can set.
STRATEGIES = ("ManualCpc", "EnhancedCpc", "MaxClicks", "MaxConversions")
# Broad match needs one of these (read.CONVERSION_BASED_STRATEGIES); Maximize
# Clicks is automated but not conversion-based, so it does not count.
_CONVERSION_BASED = CONVERSION_BASED_STRATEGIES
_MANUAL = {"ManualCpc", "EnhancedCpc"}
# Strategies that take a maximum CPC cap.
_CAPPED = {"MaxClicks", "MaxConversions"}
_ALTERNATIVES = "MaxClicks or EnhancedCpc"
# Microsoft retired manual CPC for search campaigns in 2021: it accepts a
# ManualCpc scheme and stores EnhancedCpc instead. Drafts ask for EnhancedCpc
# right away, so the preview shows what the account will have.
_MANUAL_RETIRED = (
    "Microsoft retired manual CPC bidding for search campaigns and stores it as Enhanced CPC: your own "
    "bids, which Microsoft raises or lowers in auctions it expects to convert or not. This uses EnhancedCpc."
)


def search_strategy(strategy: str) -> tuple[str, str | None]:
    """The strategy Microsoft will actually store, and a note when it differs."""
    if strategy == "ManualCpc":
        return "EnhancedCpc", _MANUAL_RETIRED
    return strategy, None
MATCH_TYPES = ("Exact", "Phrase", "Broad")
NEGATIVE_MATCH_TYPES = ("Exact", "Phrase")
NETWORKS = ("OwnedAndOperatedOnly", "OwnedAndOperatedAndSyndicatedSearch")
INTENTS = ("PeopleIn", "PeopleInOrSearchingForOrViewingPages")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MINUTES = {0: "Zero", 15: "Fifteen", 30: "Thirty", 45: "FortyFive"}
_PINS = {
    "headline_1": "Headline1", "headline_2": "Headline2", "headline_3": "Headline3",
    "headline1": "Headline1", "headline2": "Headline2", "headline3": "Headline3",
    "description_1": "Description1", "description_2": "Description2",
    "description1": "Description1", "description2": "Description2",
}
_EXTENSION_TYPES = ("SitelinkAdExtension, CalloutAdExtension, StructuredSnippetAdExtension, CallAdExtension, "
                    "ImageAdExtension")
_LIST_TYPE = "NegativeKeywordList"
_MAX_LISTS_PER_CAMPAIGN = 20
_CHUNK = 100


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ctx(config: AdLoopConfig, ad_account_id: str) -> tuple[dict, str, str]:
    meta = account_meta(config, ad_account_id)
    return meta, meta["ad_account_id"], _customer(config, meta)


def _clean(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, int)):
        values = [values]
    return [str(v).strip() for v in values if str(v).strip()]


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _check_url(url: str, errors: list[str], warnings: list[str], field: str = "final_url") -> None:
    if not url.startswith(("http://", "https://")):
        errors.append(f"{field} must start with http:// or https://")
        return
    from adloop.ads.write import _validate_urls

    bad, inconclusive = _validate_urls([url])
    if bad.get(url):
        errors.append(f"{field} '{url}' is not reachable: {bad[url]}")
    warnings.extend(inconclusive.values())


_ID_FIELDS = ("CampaignIds", "AdGroupIds", "KeywordIds", "AdIds", "CampaignCriterionIds", "ListItemIds", "MediaIds")


def _created_ids(payload: dict) -> list[str]:
    """IDs a create reported as done (failed items come back as null)."""
    ids: list[str] = []
    for field in _ID_FIELDS:
        ids += [str(i) for i in payload.get(field) or [] if i is not None]
    ids += [str((i or {}).get("Id")) for i in payload.get("AdExtensionIdentities") or [] if i and i.get("Id")]
    if payload.get("SharedEntityId"):
        ids.append(str(payload["SharedEntityId"]))
    for group in payload.get("NegativeKeywordIds") or []:
        ids += [str(i) for i in (group or {}).get("Ids") or [] if i is not None]
    return ids


def _partial_errors(payload: dict) -> list[dict]:
    entries = [e for e in payload.get("PartialErrors") or [] if e]
    for nested in payload.get("NestedPartialErrors") or []:
        if not isinstance(nested, dict):
            continue
        entries.extend(e for e in nested.get("BatchErrors") or [] if e)
        if nested.get("ErrorCode") or nested.get("Message"):
            entries.append(nested)
    return entries


def _raise_partial(payload: dict, what: str) -> None:
    entries = _partial_errors(payload)
    if not entries:
        return
    _codes, message, _tracking = fault_details({"PartialErrors": entries})
    created = _created_ids(payload)
    if created:
        # Some items were created and some refused: replaying the plan would
        # create the successful ones again.
        from adloop.safety.outcomes import PartialApplyError

        raise PartialApplyError(
            f"Microsoft created part of the {what} (ids {created}) and refused the rest: {message or entries}",
            created={what: created},
        )
    raise MicrosoftAdsApiError(f"Microsoft refused the {what}: {message or entries}")


def _call(config: AdLoopConfig, changes: dict, method: str, path: str, body: dict, what: str) -> dict:
    try:
        payload = campaign_request(
            config, path, body, account_id=changes["ad_account_id"], customer_id=changes.get("customer_id", ""),
            method=method, mutation=True,
        )
    except MicrosoftAdsApiError as exc:
        if method == "POST" and (exc.status == 0 or exc.status >= 500):
            # The request may have reached Microsoft and created something:
            # these creates have no idempotency, so replaying the plan could
            # create it twice. Retire the plan and say so.
            from adloop.safety.outcomes import PartialApplyError

            raise PartialApplyError(
                f"Microsoft did not confirm the {what} ({exc}); it may or may not have been created. "
                "Check the account before drafting it again.",
                unknown=True,
            ) from None
        raise
    _raise_partial(payload, what)
    return payload


class _Progress:
    """What a multi-step apply already changed, for an honest error.

    Once one step has changed the account, a failure in a later step retires
    the plan (PartialApplyError) instead of leaving it to be replayed.
    """

    def __init__(self) -> None:
        self.done: list[str] = []
        # IDs of what the earlier steps created, by kind, for the error.
        self.created: dict[str, list[str]] = {}

    def step(self, what: str, fn):
        from adloop.safety.outcomes import PartialApplyError

        try:
            return fn()
        except PartialApplyError as exc:
            if self.done:
                raise PartialApplyError(
                    f"{exc} (after: {'; '.join(self.done)})",
                    created={**self.created, **exc.created, "done": list(self.done)}, unknown=exc.unknown,
                ) from None
            raise
        except Exception as exc:
            if self.done:
                raise PartialApplyError(
                    f"{what} failed after these steps succeeded: {'; '.join(self.done)}. "
                    f"Microsoft said: {exc}. Check the account before drafting again.",
                    created={**self.created, "done": list(self.done)},
                ) from None
            raise


def _plan(operation: str, entity_type: str, entity_id: str, account: str, customer: str, changes: dict,
          payload: dict | None = None, *, warnings: list[str] | None = None, double: bool = False) -> dict:
    """Store a plan: ``changes`` for the person approving, ``payload`` for the dry run and the apply."""
    return store_plan_preview(
        operation, entity_type, entity_id, account, changes,
        {"ad_account_id": account, "customer_id": customer, **(payload or {})},
        warnings=warnings, double=double,
    )


def _chunks(items: list, size: int = _CHUNK) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Bidding
# ---------------------------------------------------------------------------


def bidding_scheme(strategy: str, *, target_cpa: float | None, max_cpc: float | None) -> dict:
    scheme: dict[str, Any] = {"Type": strategy}
    if strategy == "MaxConversions" and target_cpa is not None:
        scheme["TargetCpa"] = round(float(target_cpa), 2)
    if strategy in _CAPPED and max_cpc is not None:
        scheme["MaxCpc"] = {"Amount": round(float(max_cpc), 2)}
    return scheme


def _strategy_name(scheme: dict | None) -> str:
    return str((scheme or {}).get("Type") or "").replace("BiddingScheme", "")


def _counted_goals(config: AdLoopConfig, account: str, customer: str) -> int:
    try:
        goals = _conversion_goals(config, account, customer)
    except MicrosoftAdsApiError:
        return -1
    return sum(1 for g in goals if g.get("Status") == "Active" and not g.get("ExcludeFromBidding"))


def _no_goal_refusal(strategy: str) -> str:
    return (
        f"{strategy} needs an active conversion goal that counts toward Conversions, and this account has "
        "none: Microsoft refuses the campaign (ConversionGoalCriteriaNotMetForBiddingScheme). Set up a "
        "conversion goal first (get_microsoft_conversion_setup shows what exists), or use "
        f"{_ALTERNATIVES} for now."
    )


def _goals_unknown_warning(strategy: str) -> str:
    return (f"The conversion goals could not be read; Microsoft refuses {strategy} without an active "
            "conversion goal that counts toward Conversions.")


def _broad_refusal(count: int, strategy: str) -> str:
    return (
        f"The campaign has {count} active broad-match keyword(s), and broad match needs conversion-based "
        f"bidding (MaxConversions, MaxConversionValue or a target CPA/ROAS); {strategy} would buy whatever "
        "a loose match brings. Change them to phrase or exact first."
    )


# ---------------------------------------------------------------------------
# Shared by several areas
# ---------------------------------------------------------------------------

def _active_broad_keywords(config, account: str, customer: str, campaign_id: str) -> list[dict]:
    return [
        k for g in _ad_groups(config, account, customer, campaign_id)
        for k in _keywords(config, account, customer, str(g.get("Id")))
        if k.get("MatchType") == "Broad" and k.get("Status") == "Active"
    ]


def _check_bid(current: float | None, proposed: float | None, config) -> None:
    """Bid-increase cap against the current effective bid (raises SafetyViolation)."""
    from adloop.safety.guards import check_bid_increase

    if current and proposed is not None:
        check_bid_increase(float(current), float(proposed), config.safety)


def _require_unchanged(entity: dict, before: dict, what: str) -> None:
    for key, value in (before or {}).items():
        if not _same(entity.get(key), value):
            raise ValueError(f"The {what}'s {key} changed since the draft; draft again.")


class Step:
    """What a dry-run check or an apply works with: the plan, its payload, the account, the checks."""

    def __init__(self, config: AdLoopConfig, plan: ChangePlan):
        self.config = config
        self.plan = plan
        self.c = plan.apply_payload()
        self.account = self.c["ad_account_id"]
        self.customer = self.c.get("customer_id", "")
        self.checks: dict[str, Any] = {"ad_account_id": self.account}

    def entity(self, kind: str, eid: str) -> dict:
        """The entity and its parents, read again; refuses one that is gone.

        The plan's stored campaign and ad group keep this to three reads, not
        a scan of every ad group in the account.
        """
        parent = self.c.get("ad_group_id", "") if kind in ("keyword", "ad") else ""
        found = find_entity(self.config, self.account, self.customer, kind, eid, parent_id=parent or "",
                            campaign_id=self.c.get("campaign_id", "") if kind != "campaign" else "")
        if not found:
            raise ValueError(f"{kind.replace('_', ' ')} '{eid}' no longer exists.")
        return found


class Handler(NamedTuple):
    """One operation: its dry-run check (sends nothing) and its apply."""

    preflight: Callable[[Step], None]
    apply: Callable[[Step, _Progress], dict]
