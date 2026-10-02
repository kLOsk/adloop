"""Conversion goal configuration — which conversions count toward bidding.

Google Ads decides bidding on *biddable* conversion goals. A goal is the pair
(category, origin); conversion actions that fall into that pair are either
optimized for or merely reported. The v25 resources (verified against the SDK):

    customers/{customer_id}/customerConversionGoals/{category}~{origin}
        biddable               — the account-wide default
    customers/{customer_id}/campaignConversionGoals/{campaign_id}~{category}~{origin}
        biddable               — the per-campaign override
    conversion_goal_campaign_config
        goal_config_level      — CUSTOMER or CAMPAIGN
        custom_conversion_goal — the named goal set a campaign uses, if any
    custom_conversion_goal
        name, conversion_actions[], status

Both goal resources are update-only: goals come into existence with the
conversion actions that define them, so there is nothing to create or delete —
only ``biddable`` to flip. Named goal sets (custom conversion goals) are
read-only here; creating and assigning them is a separate feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

CUSTOMER = "customer"
CAMPAIGN = "campaign"
LEVELS = (CUSTOMER, CAMPAIGN)

_CUSTOMER_GOAL_QUERY = """
    SELECT customer_conversion_goal.category,
           customer_conversion_goal.origin,
           customer_conversion_goal.biddable
    FROM customer_conversion_goal
    ORDER BY customer_conversion_goal.category, customer_conversion_goal.origin
"""

_CAMPAIGN_GOAL_QUERY = """
    SELECT campaign.id, campaign.name,
           campaign_conversion_goal.category,
           campaign_conversion_goal.origin,
           campaign_conversion_goal.biddable
    FROM campaign_conversion_goal
    {where}
    ORDER BY campaign.id, campaign_conversion_goal.category,
             campaign_conversion_goal.origin
"""

_GOAL_CONFIG_QUERY = """
    SELECT campaign.id, campaign.name,
           conversion_goal_campaign_config.goal_config_level,
           conversion_goal_campaign_config.custom_conversion_goal
    FROM conversion_goal_campaign_config
    {where}
    ORDER BY campaign.id
"""

_CUSTOM_GOAL_QUERY = """
    SELECT custom_conversion_goal.id, custom_conversion_goal.name,
           custom_conversion_goal.status
    FROM custom_conversion_goal
    ORDER BY custom_conversion_goal.name
"""


def get_conversion_goals(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """Read-only view of the conversion goal configuration."""
    from adloop.ads.client import get_ads_client, normalize_customer_id

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    return read_conversion_goals(
        get_ads_client(config), cid, campaign_id=str(campaign_id or "").strip()
    )


def read_conversion_goals(client: object, cid: str, *, campaign_id: str = "") -> dict:
    """Query all four goal resources, isolating failures per query.

    Each part is read on its own: a GAQL surprise in one query (this API
    family has produced PROHIBITED_FIELD_IN_SELECT_CLAUSE before) still returns
    the other parts and the raw error instead of failing the whole tool.
    """
    service = client.get_service("GoogleAdsService")
    where = f"WHERE campaign.id = {campaign_id}" if campaign_id else ""

    result: dict = {
        "customer_goals": [],
        "campaigns": [],
        "custom_goals": [],
        "errors": [],
    }

    customer_rows = _search_part(service, cid, _CUSTOMER_GOAL_QUERY, result, "customer_goals")
    campaign_rows = _search_part(
        service, cid, _CAMPAIGN_GOAL_QUERY.format(where=where), result, "campaign_goals"
    )
    config_rows = _search_part(
        service, cid, _GOAL_CONFIG_QUERY.format(where=where), result, "goal_config"
    )
    result["custom_goals"] = _search_part(
        service, cid, _CUSTOM_GOAL_QUERY, result, "custom_goals"
    )

    result["customer_goals"] = [
        {
            "category": row.get("customer_conversion_goal.category"),
            "origin": row.get("customer_conversion_goal.origin"),
            "biddable": row.get("customer_conversion_goal.biddable"),
        }
        for row in customer_rows
    ]

    campaigns: dict[str, dict] = {}
    for row in campaign_rows:
        key = str(row.get("campaign.id", ""))
        if not key:
            continue
        entry = campaigns.setdefault(
            key,
            {
                "campaign_id": key,
                "campaign_name": row.get("campaign.name"),
                "goals": [],
                "goal_config": None,
            },
        )
        entry["goals"].append(
            {
                "category": row.get("campaign_conversion_goal.category"),
                "origin": row.get("campaign_conversion_goal.origin"),
                "biddable": row.get("campaign_conversion_goal.biddable"),
            }
        )
    for row in config_rows:
        key = str(row.get("campaign.id", ""))
        entry = campaigns.get(key)
        if entry is None:
            continue
        entry["goal_config"] = {
            "goal_config_level": row.get("conversion_goal_campaign_config.goal_config_level"),
            "custom_conversion_goal": row.get(
                "conversion_goal_campaign_config.custom_conversion_goal"
            ),
        }

    result["campaigns"] = list(campaigns.values())
    result["total_customer_goals"] = len(result["customer_goals"])
    result["total_campaigns"] = len(result["campaigns"])
    result["custom_goals"] = [
        {
            "id": str(row.get("custom_conversion_goal.id", "")),
            "name": row.get("custom_conversion_goal.name"),
            "status": row.get("custom_conversion_goal.status"),
        }
        for row in result["custom_goals"]
    ]
    return result


def _search_part(
    service: object, cid: str, query: str, result: dict, label: str
) -> list[dict]:
    """Run one GAQL query; record the raw error instead of raising."""
    from adloop.ads.gaql import _extract_field, _parse_select_fields

    try:
        fields = _parse_select_fields(query)
        return [
            {field: _extract_field(row, field) for field in fields}
            for row in service.search(customer_id=cid, query=query)
        ]
    except Exception as exc:  # noqa: BLE001 — one failing query must not hide the rest
        result["errors"].append({"part": label, "error": str(exc)})
        return []


def goal_resource_name(customer_id: str, level: str, category: str, origin: str,
                       campaign_id: str = "") -> str:
    """Build the update-only resource name for one goal."""
    if level == CAMPAIGN:
        return (
            f"customers/{customer_id}/campaignConversionGoals/"
            f"{campaign_id}~{category}~{origin}"
        )
    return f"customers/{customer_id}/customerConversionGoals/{category}~{origin}"


def plan_goal_changes(
    current_goals: list[dict], requested: list[dict]
) -> tuple[list[dict], list[str]]:
    """Pair each requested goal with its current value.

    Returns (changes, unknown_pairs). The second list is what the caller needs
    to refuse the plan: the mutate requests for conversion goals have no
    ``partial_failure``, so one unknown pair would reject the whole batch.
    """
    current = {
        (goal.get("category"), goal.get("origin")): goal.get("biddable")
        for goal in current_goals
    }
    changes: list[dict] = []
    unknown: list[str] = []
    for goal in requested:
        category = goal["category"]
        origin = goal["origin"]
        before = current.get((category, origin))
        if (category, origin) not in current:
            unknown.append(f"{category}/{origin}")
        changes.append(
            {
                "category": category,
                "origin": origin,
                "biddable": bool(goal["biddable"]),
                "before": before,
            }
        )
    return changes, unknown


def mutate_conversion_goals(client: object, cid: str, changes: dict) -> dict:
    """Update the biddable flag of the planned goals — one request per level."""
    from google.protobuf import field_mask_pb2

    level = changes["level"]
    if level == CAMPAIGN:
        service = client.get_service("CampaignConversionGoalService")
        request = client.get_type("MutateCampaignConversionGoalsRequest")
        operation_type = "CampaignConversionGoalOperation"
    else:
        service = client.get_service("CustomerConversionGoalService")
        request = client.get_type("MutateCustomerConversionGoalsRequest")
        operation_type = "CustomerConversionGoalOperation"

    operations = []
    for goal in changes["goals"]:
        operation = client.get_type(operation_type)
        goal_message = operation.update
        goal_message.resource_name = goal_resource_name(
            cid, level, goal["category"], goal["origin"],
            campaign_id=changes.get("campaign_id", ""),
        )
        goal_message.biddable = bool(goal["biddable"])
        operation.update_mask = field_mask_pb2.FieldMask(paths=["biddable"])
        operations.append(operation)

    request.customer_id = cid
    request.operations.extend(operations)

    # Note: neither MutateCustomerConversionGoalsRequest nor
    # MutateCampaignConversionGoalsRequest has a partial_failure field — the
    # batch is all-or-nothing per level, which is why the draft refuses unknown
    # goal pairs before a plan is ever written.

    if level == CAMPAIGN:
        response = service.mutate_campaign_conversion_goals(request=request)
    else:
        response = service.mutate_customer_conversion_goals(request=request)
    return _split_results(client, response, changes["goals"])


def _split_results(client: object, response: object, goals: list[dict]) -> dict:
    from adloop.ads.write import _parse_partial_failure_per_op

    pf_error = getattr(response, "partial_failure_error", None)
    per_op_errors = _parse_partial_failure_per_op(client, pf_error)

    updated: list[dict] = []
    failed: list[dict] = []
    for index, result in enumerate(response.results):
        goal = goals[index] if index < len(goals) else {}
        entry = {
            "category": goal.get("category"),
            "origin": goal.get("origin"),
            "biddable": goal.get("biddable"),
        }
        if getattr(result, "resource_name", ""):
            updated.append(entry)
        else:
            failed.append(
                {
                    **entry,
                    "operation_index": index,
                    "error": per_op_errors.get(
                        index, "Unknown error (see partial_failure_message)"
                    ),
                }
            )

    out: dict = {"updated": updated, "updated_count": len(updated), "failed": failed}
    if failed:
        out["partial_failure"] = True
        message = getattr(pf_error, "message", "") if pf_error is not None else ""
        if message:
            out["partial_failure_message"] = message
    return out
