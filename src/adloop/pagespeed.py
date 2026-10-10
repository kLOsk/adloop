"""PageSpeed Insights (Lighthouse lab data + CrUX field data) for landing pages.

No OAuth involved: the PSI API is key-based (and works keyless at very low
rates). Google fetches the URL from its own infrastructure, so there is no
SSRF surface on our side — but only public http(s) URLs make sense.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig

_PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Core Web Vitals thresholds (Google's "good" boundaries).
_CWV_GOOD = {"LARGEST_CONTENTFUL_PAINT_MS": 2500, "INTERACTION_TO_NEXT_PAINT": 200,
             "CUMULATIVE_LAYOUT_SHIFT_SCORE": 0.1}


def analyze_page_speed(
    config: AdLoopConfig,
    *,
    url: str = "",
    strategy: str = "mobile",
) -> dict:
    """Run PageSpeed Insights for a URL — Lighthouse lab + CrUX field data."""
    import requests

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {
            "error": "url must be a public http(s) URL, e.g. "
                     "https://example.com/landing-page"
        }
    strategy = strategy.lower()
    if strategy not in ("mobile", "desktop"):
        return {"error": "strategy must be 'mobile' or 'desktop'"}

    params: dict = {
        "url": url,
        "strategy": strategy.upper(),
        "category": "PERFORMANCE",
    }
    api_key = config.pagespeed.api_key
    if api_key:
        params["key"] = api_key

    # Lighthouse genuinely takes this long on heavy pages.
    response = requests.get(_PSI_ENDPOINT, params=params, timeout=90)
    if response.status_code != 200:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        hint = (
            " Keyless PSI calls are heavily rate-limited — set "
            "pagespeed.api_key in the config for a stable quota."
            if response.status_code == 429 and not api_key
            else ""
        )
        return {"error": f"PageSpeed API returned {response.status_code}: {detail}{hint}"}

    payload = response.json()
    lighthouse = payload.get("lighthouseResult", {}) or {}
    audits = lighthouse.get("audits", {}) or {}

    score = (lighthouse.get("categories", {}).get("performance", {}) or {}).get("score")
    result: dict = {
        "url": url,
        "strategy": strategy,
        "performance_score": round(score * 100) if score is not None else None,
        "lab": _lab_metrics(audits),
        "field": _select_field_data(payload),
        "top_opportunities": _opportunities(audits),
    }
    result["insights"] = _insights(result)
    return result


def _lab_metrics(audits: dict) -> dict:
    """Lighthouse lab measurements (simulated load, deterministic-ish)."""
    def _numeric(audit_id: str):
        return (audits.get(audit_id) or {}).get("numericValue")

    lcp_ms = _numeric("largest-contentful-paint")
    cls = _numeric("cumulative-layout-shift")
    tbt_ms = _numeric("total-blocking-time")
    fcp_ms = _numeric("first-contentful-paint")
    return {
        "lcp_seconds": round(lcp_ms / 1000, 2) if lcp_ms is not None else None,
        "cls": round(cls, 3) if cls is not None else None,
        "tbt_ms": round(tbt_ms) if tbt_ms is not None else None,
        "fcp_seconds": round(fcp_ms / 1000, 2) if fcp_ms is not None else None,
    }


def _select_field_data(payload: dict) -> dict:
    """URL-level CrUX field data, else the origin-wide data, labelled.

    PSI reports URL-level data in ``loadingExperience`` and origin-level data
    in ``originLoadingExperience``. When a URL has too little traffic,
    ``loadingExperience`` is empty or (``origin_fallback: true``) already
    carries the origin's numbers. Either way the result says which one it is,
    because origin data averages every page on the site.
    """
    url_level = payload.get("loadingExperience", {}) or {}
    origin_level = payload.get("originLoadingExperience", {}) or {}

    if url_level.get("metrics") and not url_level.get("origin_fallback"):
        field = _field_metrics(url_level)
        if field:
            field["scope"] = "url"
            return field

    source = (
        url_level
        if url_level.get("origin_fallback") and url_level.get("metrics")
        else origin_level
    )
    field = _field_metrics(source)
    if not field:
        return {}
    field["scope"] = "origin"
    origin = source.get("id") or origin_level.get("id")
    if origin:
        field["origin"] = origin
    field["note"] = (
        "Origin-wide field data: this URL has too little real-user traffic of "
        "its own, so these numbers cover all pages on the origin, not this page."
    )
    return field


def _field_metrics(loading_experience: dict) -> dict:
    """CrUX field data — real Chrome users over the last 28 days.

    Only present for URLs/origins with enough traffic; empty dict means
    "not enough real-user data", not "no problem".
    """
    metrics = loading_experience.get("metrics", {}) or {}
    out: dict = {}
    mapping = {
        "LARGEST_CONTENTFUL_PAINT_MS": ("lcp_p75_seconds", lambda v: round(v / 1000, 2)),
        "INTERACTION_TO_NEXT_PAINT": ("inp_p75_ms", lambda v: round(v)),
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("cls_p75", lambda v: round(v / 100, 3)),
        "FIRST_CONTENTFUL_PAINT_MS": ("fcp_p75_seconds", lambda v: round(v / 1000, 2)),
        "EXPERIMENTAL_TIME_TO_FIRST_BYTE": ("ttfb_p75_ms", lambda v: round(v)),
    }
    for api_name, (key, convert) in mapping.items():
        entry = metrics.get(api_name)
        if entry and entry.get("percentile") is not None:
            out[key] = convert(entry["percentile"])
            out[key.rsplit("_", 1)[0] + "_rating"] = entry.get("category", "")
    if loading_experience.get("overall_category"):
        out["overall_rating"] = loading_experience["overall_category"]
    return out


def _opportunities(audits: dict, limit: int = 5) -> list[dict]:
    """Lighthouse opportunity audits, largest estimated savings first."""
    opportunities = []
    for audit in audits.values():
        details = audit.get("details") or {}
        if details.get("type") != "opportunity":
            continue
        savings_ms = details.get("overallSavingsMs") or 0
        if savings_ms < 100:
            continue
        opportunities.append({
            "title": audit.get("title", ""),
            "estimated_savings_ms": round(savings_ms),
        })
    opportunities.sort(key=lambda o: o["estimated_savings_ms"], reverse=True)
    return opportunities[:limit]


def _insights(result: dict) -> list[str]:
    insights: list[str] = []
    score = result.get("performance_score")
    if score is not None and score < 50:
        insights.append(
            f"Performance score {score}/100 — a slow landing page depresses "
            f"Quality Score, raises CPCs, and loses paid clicks before the "
            f"page even renders. Fix this before optimizing ad copy."
        )

    field = result.get("field", {})
    if not field:
        insights.append(
            "No CrUX field data (not enough real-user traffic on this page or "
            "its origin). The lab metrics above are simulated and directional only."
        )
    else:
        origin_wide = field.get("scope") == "origin"
        if origin_wide:
            insights.append(
                "Field data is origin-wide (all pages of the site), because this "
                "page has too little real-user traffic of its own. Treat it as "
                "the site's typical experience, not this page's."
            )
        if field.get("overall_rating") == "SLOW":
            subject = "pages on this origin" if origin_wide else "this page"
            insights.append(
                f"Real Chrome users experience {subject} as SLOW (CrUX, last 28 "
                "days). This is measured user reality, not a simulation."
            )

    lab = result.get("lab", {})
    if (lab.get("lcp_seconds") or 0) > 2.5:
        insights.append(
            f"LCP {lab['lcp_seconds']}s exceeds the 2.5s 'good' threshold — "
            f"the largest content element renders too late."
        )
    return insights
