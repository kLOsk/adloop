"""Budget estimation and keyword discovery via Google Ads Keyword Planner."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


_DEFAULT_MAX_CPC_MICROS = 1_000_000  # 1.00 in account currency


def estimate_budget(
    config: AdLoopConfig,
    *,
    keywords: list[dict],
    daily_budget: float = 0,
    geo_target_id: str = "2276",
    language_id: str = "1000",
    forecast_days: int = 30,
    customer_id: str = "",
) -> dict:
    """Forecast clicks, impressions, and cost for a set of keywords.

    Uses KeywordPlanIdeaService.GenerateKeywordForecastMetrics to estimate
    campaign performance without creating anything. Useful for budget planning
    before launching a new campaign.

    keywords: list of {"text": str, "match_type": "EXACT|PHRASE|BROAD", "max_cpc": float (optional)}
    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK, 2250=France)
    language_id: language constant (1000=English, 1001=German, 1002=French, 1003=Spanish)
    forecast_days: number of days to forecast (default 30)
    """
    from adloop.ads.client import get_ads_client, normalize_customer_id

    if not keywords:
        return {"error": "At least one keyword is required"}

    client = get_ads_client(config)
    cid = normalize_customer_id(customer_id or config.ads.customer_id)

    googleads_service = client.get_service("GoogleAdsService")
    kp_service = client.get_service("KeywordPlanIdeaService")

    campaign = client.get_type("CampaignToForecast")
    campaign.keyword_plan_network = (
        client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    )

    max_bid = max(
        (int(kw.get("max_cpc", 0) * 1_000_000) for kw in keywords),
        default=_DEFAULT_MAX_CPC_MICROS,
    )
    if max_bid <= 0:
        max_bid = _DEFAULT_MAX_CPC_MICROS
    campaign.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = max_bid

    geo_modifier = client.get_type("CriterionBidModifier")
    geo_modifier.geo_target_constant = googleads_service.geo_target_constant_path(
        geo_target_id
    )
    campaign.geo_modifiers.append(geo_modifier)

    campaign.language_constants.append(
        googleads_service.language_constant_path(language_id)
    )

    ad_group = client.get_type("ForecastAdGroup")

    for kw in keywords:
        text = kw.get("text", "")
        if not text:
            continue
        match_type = kw.get("match_type", "BROAD").upper()
        cpc_micros = int(kw.get("max_cpc", 0) * 1_000_000) or _DEFAULT_MAX_CPC_MICROS

        biddable = client.get_type("BiddableKeyword")
        biddable.max_cpc_bid_micros = cpc_micros
        biddable.keyword.text = text
        biddable.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, match_type, client.enums.KeywordMatchTypeEnum.BROAD
        )
        ad_group.biddable_keywords.append(biddable)

    campaign.ad_groups.append(ad_group)

    request = client.get_type("GenerateKeywordForecastMetricsRequest")
    request.customer_id = cid
    request.campaign = campaign

    tomorrow = date.today() + timedelta(days=1)
    end_date = date.today() + timedelta(days=forecast_days)
    request.forecast_period.start_date = tomorrow.isoformat()
    request.forecast_period.end_date = end_date.isoformat()

    response = kp_service.generate_keyword_forecast_metrics(request=request)
    metrics = response.campaign_forecast_metrics

    # KeywordForecastMetrics fields are ``optional`` in v23, so the SDK
    # returns ``None`` for unset fields and the actual integer (including
    # 0) otherwise. Falsy checks like ``int(v) if v else None`` would
    # silently map a real 0-click or 0-cost forecast to None and the
    # caller couldn't tell "no data" apart from "data says zero" — same
    # bug class as discover_keywords (issue Bug 2). Use ``is not None``
    # throughout and the shared ``_micros_to_currency`` helper for the
    # micros→currency conversions.
    clicks = getattr(metrics, "clicks", None)
    impressions = getattr(metrics, "impressions", None)
    avg_cpc_micros = getattr(metrics, "average_cpc_micros", None)
    cost_micros = getattr(metrics, "cost_micros", None)
    ctr = getattr(metrics, "click_through_rate", None)

    total_cost = _micros_to_currency(cost_micros)
    avg_cpc = _micros_to_currency(avg_cpc_micros)

    days = max(forecast_days, 1)
    daily = {
        "clicks": round(clicks / days, 1) if clicks is not None else None,
        "impressions": (
            round(impressions / days, 1) if impressions is not None else None
        ),
        "cost": round(total_cost / days, 2) if total_cost is not None else None,
    }

    insights = []
    # Only emit the headline insight when there's actually something to
    # report (real clicks AND real cost) — a zero-click forecast gets its
    # own dedicated insight below, and a None forecast shouldn't trigger
    # either path.
    if total_cost is not None and clicks is not None and clicks > 0:
        insights.append(
            f"Estimated {clicks:.0f} clicks over {forecast_days} days at "
            f"~{avg_cpc} avg CPC. Total estimated cost: {total_cost:.2f}."
        )
    if daily_budget > 0 and daily["cost"] is not None and daily["cost"] > 0:
        if daily_budget < daily["cost"]:
            capture_pct = round(daily_budget / daily["cost"] * 100)
            insights.append(
                f"Daily budget of {daily_budget:.2f} would capture ~{capture_pct}% "
                f"of available traffic (estimated daily cost: {daily['cost']:.2f})."
            )
        else:
            insights.append(
                f"Daily budget of {daily_budget:.2f} is sufficient to capture "
                f"most available traffic (estimated daily cost: {daily['cost']:.2f})."
            )

    if (
        impressions is not None
        and clicks is not None
        and impressions > 0
        and clicks == 0
    ):
        insights.append(
            "Forecast shows impressions but zero clicks — keywords may be too "
            "generic or CPCs too low for competitive positions."
        )

    return {
        "forecast_period": {
            "start": tomorrow.isoformat(),
            "end": end_date.isoformat(),
        },
        "estimated_clicks": clicks,
        "estimated_impressions": impressions,
        "estimated_cost": total_cost,
        "estimated_avg_cpc": avg_cpc,
        "estimated_ctr": round(ctr, 4) if ctr is not None else None,
        "daily_estimates": daily,
        "keywords_used": len([kw for kw in keywords if kw.get("text")]),
        "insights": insights,
    }


_COMPETITION_LABELS = {0: "UNSPECIFIED", 1: "LOW", 2: "MEDIUM", 3: "HIGH"}


_KEYWORD_IDEAS_REST_URL = (
    "https://googleads.googleapis.com/{version}/customers/{cid}:generateKeywordIdeas"
)


def _maybe_int(value: object) -> int | None:
    """Parse a JSON int64 field into an int, preserving legitimate 0.

    REST returns ``int64`` fields as JSON strings per the proto3 JSON spec
    (e.g. ``"avgMonthlySearches": "0"``). Falsy checks like
    ``int(v) if v else None`` would map a real ``0`` to ``None`` and
    silently lose data — e.g. a niche keyword with no recorded competition
    or a bid range whose low bound is 0 would disappear from the output.

    Treat only ``None`` and empty string as "missing"; everything that
    parses cleanly as an int (including ``"0"`` and ``0``) is preserved
    exactly. Anything else falls back to ``None`` rather than crashing
    the whole response.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _micros_to_currency(micros: int | None) -> float | None:
    """Convert micros to a 2-dp currency float, preserving ``0`` and ``None``.

    A bid bound of 0 micros is meaningful (Google can return 0 for the low
    end of a competitive range or when no bid data is available for a
    keyword) and must not be collapsed to ``None``.
    """
    if micros is None:
        return None
    return round(micros / 1_000_000, 2)


def _build_keyword_ideas_rest_body(
    *,
    language_id: str,
    geo_target_id: str,
    page_size: int,
    seed_keywords: list[str],
    url: str,
    page_token: str = "",
) -> dict:
    """Build the JSON body for the REST generateKeywordIdeas endpoint.

    Schema follows google-ads REST v23 (camelCase). Exactly one of
    ``keywordSeed`` / ``urlSeed`` / ``keywordAndUrlSeed`` is set based on
    which inputs were provided.
    """
    body: dict = {
        "language": f"languageConstants/{language_id}",
        "geoTargetConstants": [f"geoTargetConstants/{geo_target_id}"],
        "keywordPlanNetwork": "GOOGLE_SEARCH",
        "pageSize": page_size,
    }
    if seed_keywords and url:
        body["keywordAndUrlSeed"] = {"url": url, "keywords": list(seed_keywords)}
    elif seed_keywords:
        body["keywordSeed"] = {"keywords": list(seed_keywords)}
    else:
        body["urlSeed"] = {"url": url}
    if page_token:
        body["pageToken"] = page_token
    return body


def _post_keyword_ideas_rest_page(
    config: AdLoopConfig, cid: str, body: dict
) -> dict:
    """POST a single page request to the REST generateKeywordIdeas endpoint.

    Issue #37: ``KeywordPlanIdeaService.GenerateKeywordIdeas`` over gRPC sits
    in a tight quota bucket that exhausts after a small number of sequential
    calls and returns ``RESOURCE_EXHAUSTED`` regardless of QPS. The REST v23
    endpoint for the same method lives in a separate, much larger quota
    bucket, so this swap eliminates the 429s that made ``discover_keywords``
    unusable for any multi-geo or repeat-call workflow. Filed against
    google-ads-python; see https://github.com/kLOsk/adloop/issues/37.

    Re-raises HTTP 429s as a string-formatted error so the existing
    ``call_with_retry`` helper can apply exponential backoff and re-attempt.
    """
    import requests
    from google.auth.transport.requests import AuthorizedSession

    from adloop.ads.client import GOOGLE_ADS_API_VERSION
    from adloop.auth import get_ads_credentials

    credentials = get_ads_credentials(config)
    session = AuthorizedSession(credentials)

    headers = {
        "developer-token": config.ads.developer_token,
        "Content-Type": "application/json",
    }
    if config.ads.login_customer_id:
        headers["login-customer-id"] = config.ads.login_customer_id.replace("-", "")

    url = _KEYWORD_IDEAS_REST_URL.format(version=GOOGLE_ADS_API_VERSION, cid=cid)
    response = session.post(url, json=body, headers=headers, timeout=60)

    if response.status_code == 429:
        # Surface as a RESOURCE_EXHAUSTED string so call_with_retry recognises
        # this as a rate-limit error and backs off. The REST bucket is much
        # larger than gRPC so this branch should be rare, but handle it.
        raise requests.HTTPError(
            f"RESOURCE_EXHAUSTED (HTTP 429) from REST generateKeywordIdeas: "
            f"{response.text[:500]}"
        )
    response.raise_for_status()
    return response.json()


def discover_keywords(
    config: AdLoopConfig,
    *,
    seed_keywords: list[str] = [],  # noqa: B006 — mutable default required for MCP JSON schema (array, not anyOf)
    url: str = "",
    geo_target_id: str = "2276",
    language_id: str = "1000",
    page_size: int = 50,
    customer_id: str = "",
) -> dict:
    """Discover new keyword ideas using Google Ads Keyword Planner.

    Mirrors the "Discover new keywords" workflow in the Keyword Planner UI:
    - Start with keywords: provide seed_keywords (one or more terms)
    - Start with a website: provide url (a landing page or full site URL)
    - Both together: keywords + url for more targeted ideas

    Returns keyword ideas with avg monthly searches, competition level,
    and top-of-page bid range.

    seed_keywords: list of seed terms, e.g. ["running shoes", "trail running"]
    url: a page or site URL to extract keyword ideas from
    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK)
    language_id: language constant (1000=English, 1001=German, 1002=French)
    page_size: max number of keyword ideas to return (default 50, max 1000)

    Network: this tool intentionally bypasses the google-ads gRPC client for
    KeywordPlanIdeaService and calls the v23 REST endpoint directly. The
    gRPC quota bucket for this single method exhausts almost immediately
    under sequential single-geo calls (issue #37); REST sits in a separate,
    much larger bucket and works without issue. All other Ads tools still
    use the gRPC client.
    """
    from adloop.ads.client import call_with_retry, normalize_customer_id

    seed_keywords = list(seed_keywords)
    if not seed_keywords and not url:
        return {"error": "Provide at least one of: seed_keywords or url"}

    cid = normalize_customer_id(customer_id or config.ads.customer_id)
    capped_page_size = min(max(1, page_size), 1000)

    ideas: list[dict] = []
    page_token = ""
    while True:
        body = _build_keyword_ideas_rest_body(
            language_id=language_id,
            geo_target_id=geo_target_id,
            page_size=capped_page_size,
            seed_keywords=seed_keywords,
            url=url,
            page_token=page_token,
        )
        payload = call_with_retry(_post_keyword_ideas_rest_page, config, cid, body)

        for idea in payload.get("results", []):
            metrics = idea.get("keywordIdeaMetrics", {}) or {}
            competition = metrics.get("competition") or "UNSPECIFIED"

            # REST returns int64 fields as JSON strings; ``_maybe_int`` and
            # ``_micros_to_currency`` preserve legitimate 0 values that
            # falsy checks would otherwise silently drop. See the helper
            # docstrings for the failure modes this defends against.
            ideas.append({
                "keyword": idea.get("text", ""),
                "avg_monthly_searches": _maybe_int(metrics.get("avgMonthlySearches")),
                "competition": competition,
                "competition_index": _maybe_int(metrics.get("competitionIndex")),
                "low_top_of_page_bid": _micros_to_currency(
                    _maybe_int(metrics.get("lowTopOfPageBidMicros"))
                ),
                "high_top_of_page_bid": _micros_to_currency(
                    _maybe_int(metrics.get("highTopOfPageBidMicros"))
                ),
            })

        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            break

    # Sort by avg monthly searches descending (None last)
    ideas.sort(key=lambda x: x["avg_monthly_searches"] or 0, reverse=True)

    insights = []
    if ideas:
        high_competition = [i for i in ideas if i["competition"] == "HIGH"]
        low_competition = [i for i in ideas if i["competition"] == "LOW"]
        if high_competition:
            insights.append(
                f"{len(high_competition)} high-competition keyword(s) — expect "
                f"higher CPCs and harder positioning."
            )
        if low_competition:
            insights.append(
                f"{len(low_competition)} low-competition keyword(s) — good "
                f"opportunities for early traction at lower cost."
            )
        with_volume = [i for i in ideas if i["avg_monthly_searches"]]
        if with_volume:
            top = with_volume[0]
            insights.append(
                f"Highest-volume idea: '{top['keyword']}' with ~{top['avg_monthly_searches']:,} "
                f"avg monthly searches."
            )

    return {
        "keyword_ideas": ideas,
        "total_ideas": len(ideas),
        "seed_keywords": seed_keywords,
        "seed_url": url,
        "insights": insights,
    }
