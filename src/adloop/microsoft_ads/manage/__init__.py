"""Microsoft Advertising management: campaigns, targeting, ad groups, keywords,
negatives, responsive search ads, ad extensions (image extensions included)
and removals.

Every function here drafts a :class:`ChangePlan` (platform ``microsoft``,
operation ``microsoft_*``) and returns its preview; nothing changes until
``confirm_and_apply``. :mod:`adloop.microsoft_ads.write` routes these
operations to :func:`preflight` and :func:`apply_plan` below.

Microsoft's Campaign Management API has no validate-only mode, so:

- **Updates** store a snapshot of every field they change; the dry run and
  the apply re-read the entity and refuse when one of those fields was
  edited in Microsoft Advertising since the preview.
- **Creates** re-check their parents, the safety caps and (for named
  objects) that the name is still free, so a retried apply after a partial
  failure refuses instead of creating a second copy.
- **Multi-step applies** (a campaign and then its targeting, a list and then
  its attachments) report exactly what was created when a later step fails.

A plan's ``changes`` hold what the person approves (names, before → after);
request bodies, snapshots and ids live in ``ChangePlan.apply_only_payload``,
which no preview, dialog, card or audit entry shows. The dry run and the
apply read both through ``plan.apply_payload()``.

REST conventions (learn.microsoft.com/advertising/campaign-management-service):
add is POST, update PUT, delete DELETE on the collection path; polymorphic
objects carry a ``Type`` discriminator; on update an omitted field stays as
it is and a list that is sent replaces the old one.

The code is split by area (campaigns, targeting, ad_groups, negatives, ads,
extensions, removal). Each area module drafts its plans and registers its
operations in ``HANDLERS``: operation -> (dry-run check, apply). The
package merges them into one table, so :func:`preflight` and
:func:`apply_plan` look an operation up instead of walking a chain of
branches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.microsoft_ads.manage import (
    ad_groups,
    ads,
    campaigns,
    extensions,
    negatives,
    removal,
    targeting,
)
from adloop.microsoft_ads.manage._common import (
    MATCH_TYPES,
    NEGATIVE_MATCH_TYPES,
    STRATEGIES,
    Handler,
    Step,
    _counted_goals,
    _Progress,
    _raise_partial,
    bidding_scheme,
)
from adloop.microsoft_ads.manage.ad_groups import (
    draft_microsoft_ad_group,
    draft_microsoft_keywords,
    update_microsoft_ad_group,
    update_microsoft_keyword_bids,
)
from adloop.microsoft_ads.manage.ads import (
    draft_microsoft_responsive_search_ad,
    update_microsoft_responsive_search_ad,
)
from adloop.microsoft_ads.manage.campaigns import (
    draft_microsoft_campaign,
    update_microsoft_campaign,
)
from adloop.microsoft_ads.manage.extensions import (
    draft_microsoft_call_extension,
    draft_microsoft_callouts,
    draft_microsoft_image_extension,
    draft_microsoft_sitelinks,
    draft_microsoft_structured_snippet,
    get_microsoft_ad_extensions,
)
from adloop.microsoft_ads.manage.negatives import (
    add_microsoft_negative_keywords,
    add_to_microsoft_negative_keyword_list,
    attach_microsoft_negative_keyword_list,
    detach_microsoft_negative_keyword_list,
    get_microsoft_negative_keywords,
    propose_microsoft_negative_keyword_list,
)
from adloop.microsoft_ads.manage.removal import (
    REMOVABLE,
    remove_microsoft_entity,
)
from adloop.microsoft_ads.manage.targeting import (
    _block_span,
    _current_targeting,
    update_microsoft_campaign_targeting,
)

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig
    from adloop.safety.preview import ChangePlan

HANDLERS: dict[str, Handler] = {
    **campaigns.HANDLERS,
    **targeting.HANDLERS,
    **ad_groups.HANDLERS,
    **negatives.HANDLERS,
    **ads.HANDLERS,
    **extensions.HANDLERS,
    **removal.HANDLERS,
}
OPERATIONS = frozenset(HANDLERS)


def _handler(plan: ChangePlan) -> Handler:
    handler = HANDLERS.get(plan.operation)
    if handler is None:
        raise ValueError(f"Unknown Microsoft operation: {plan.operation}")
    return handler


def preflight(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Re-read what the plan touches and re-check the gates; sends nothing."""
    step = Step(config, plan)
    _handler(plan).preflight(step)
    step.checks["checked"] = plan.operation.replace("microsoft_", "")
    return step.checks


def apply_plan(config: AdLoopConfig, plan: ChangePlan) -> dict:
    """Execute a management plan (after :func:`preflight`, which the caller runs first)."""
    return _handler(plan).apply(Step(config, plan), _Progress())


__all__ = [
    "HANDLERS",
    "Handler",
    "MATCH_TYPES",
    "NEGATIVE_MATCH_TYPES",
    "OPERATIONS",
    "REMOVABLE",
    "STRATEGIES",
    "Step",
    "_Progress",
    "_block_span",
    "_counted_goals",
    "_current_targeting",
    "_raise_partial",
    "add_microsoft_negative_keywords",
    "add_to_microsoft_negative_keyword_list",
    "apply_plan",
    "attach_microsoft_negative_keyword_list",
    "bidding_scheme",
    "detach_microsoft_negative_keyword_list",
    "draft_microsoft_ad_group",
    "draft_microsoft_call_extension",
    "draft_microsoft_callouts",
    "draft_microsoft_campaign",
    "draft_microsoft_image_extension",
    "draft_microsoft_keywords",
    "draft_microsoft_responsive_search_ad",
    "draft_microsoft_sitelinks",
    "draft_microsoft_structured_snippet",
    "get_microsoft_ad_extensions",
    "get_microsoft_negative_keywords",
    "preflight",
    "propose_microsoft_negative_keyword_list",
    "remove_microsoft_entity",
    "update_microsoft_ad_group",
    "update_microsoft_campaign",
    "update_microsoft_campaign_targeting",
    "update_microsoft_keyword_bids",
    "update_microsoft_responsive_search_ad",
]
