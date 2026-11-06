"""Search Analytics: flexible queries plus analyses that are too row-heavy to do in chat.

Accuracy rules used throughout:
- Period comparisons end on the latest *complete* day. The API marks where fresh, still-changing data starts
  (metadata.firstIncompleteDate); comparing partial days against full ones understates the recent period.
- Aggregated positions are impression-weighted averages of row positions (an approximation of the UI's figure).
- Query-level rows exclude anonymised queries, so they add up to less than the property total.
"""

from __future__ import annotations

import datetime as dt
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from . import api
from .util import now_iso, previous_range, pt_today, r2, r4, resolve_range, write_csv

DIMENSIONS = {"date", "hour", "query", "page", "country", "device", "searchAppearance"}
SEARCH_TYPES = {"web", "image", "video", "news", "discover", "googleNews"}
OPERATORS = {"equals", "notEquals", "contains", "notContains", "includingRegex", "excludingRegex"}
FILTER_DIMENSIONS = {"query", "page", "country", "device", "searchAppearance"}
NO_QUERY_TYPES = {"discover", "googleNews"}  # these reports have no query dimension


# ---------------------------------------------------------------- request building
def build_body(start, end, dimensions=None, search_type="web", filters=None, aggregation="auto", data_state="all") -> dict:
    dims = list(dimensions or [])
    bad = [d for d in dims if d not in DIMENSIONS]
    if bad:
        raise ValueError(f"Unknown dimension(s) {bad}. Use: {sorted(DIMENSIONS)}")
    if search_type not in SEARCH_TYPES:
        raise ValueError(f"search_type must be one of {sorted(SEARCH_TYPES)}")
    if search_type in NO_QUERY_TYPES and "query" in dims:
        raise ValueError(f"The {search_type} report has no query dimension; use page, date, country or device.")
    if "hour" in dims:
        data_state = "hourly_all"  # hourly rows only exist in this data state
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dims,
            "type": search_type, "dataState": data_state}
    if aggregation and aggregation != "auto":
        body["aggregationType"] = aggregation
    if filters:
        clean = []
        for f in filters:
            dim, op, expr = f.get("dimension"), f.get("operator", "equals"), f.get("expression")
            if dim not in FILTER_DIMENSIONS or op not in OPERATORS or expr in (None, ""):
                raise ValueError(f"Bad filter {f}. dimension in {sorted(FILTER_DIMENSIONS)}, "
                                 f"operator in {sorted(OPERATORS)}, expression required.")
            clean.append({"dimension": dim, "operator": op, "expression": str(expr)})
        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": clean}]
    return body


def _row(dims: list[str], raw: dict) -> dict:
    out = {d: k for d, k in zip(dims, raw.get("keys", []))}
    out.update(clicks=raw.get("clicks", 0), impressions=raw.get("impressions", 0),
               ctr=r4(raw.get("ctr")), position=r2(raw.get("position")))
    return out


def source(site: str, body: dict, extra: dict | None = None) -> dict:
    """Provenance attached to results, so every number can be traced and re-queried (see verify_metric)."""
    info = {"api": "Search Console API searchanalytics.query", "site": site, "search_type": body.get("type"),
            "start_date": body.get("startDate"), "end_date": body.get("endDate"), "dimensions": body.get("dimensions"),
            "data_state": body.get("dataState"), "filters": (body.get("dimensionFilterGroups") or [{}])[0].get("filters"),
            "retrieved_at": now_iso(), "timezone": "America/Los_Angeles"}
    if extra:
        info.update(extra)
    return info


# ---------------------------------------------------------------- latest complete day
_complete_cache: dict = {}
_complete_lock = threading.Lock()


def latest_complete_date(site: str, search_type: str = "web") -> dt.date:
    key = (site, search_type)
    with _complete_lock:
        hit = _complete_cache.get(key)
        if hit and time.time() - hit[1] < 3600:
            return hit[0]
    today = pt_today()
    body = {"startDate": (today - dt.timedelta(days=10)).isoformat(), "endDate": today.isoformat(),
            "dimensions": ["date"], "type": search_type, "dataState": "all", "rowLimit": 25}
    data = api.search_analytics(site, body)
    first = (data.get("metadata") or {}).get("firstIncompleteDate")
    if first:
        end = dt.date.fromisoformat(first) - dt.timedelta(days=1)
    else:
        dates = [r["keys"][0] for r in data.get("rows", []) if r.get("keys")]
        end = dt.date.fromisoformat(max(dates)) if dates else today - dt.timedelta(days=3)
    with _complete_lock:
        _complete_cache[key] = (end, time.time())
    return end


def complete_range(site, start_date, end_date, days, search_type="web"):
    default_end = None if end_date else latest_complete_date(site, search_type)
    start, end = resolve_range(start_date, end_date, days, default_end=default_end)
    basis = "as requested" if end_date else "latest complete day (fresher partial days excluded)"
    return start, end, basis


# ---------------------------------------------------------------- core queries
def query(site: str, start_date=None, end_date=None, days=28, dimensions=None, search_type="web", filters=None,
          aggregation="auto", data_state="all", max_rows=1000, default_end=None) -> tuple[list[dict], dict]:
    start, end = resolve_range(start_date, end_date, days, default_end=default_end)
    body = build_body(start, end, dimensions, search_type, filters, aggregation, data_state)
    raw, truncated, metadata = api.search_analytics_rows(site, body, max_rows)
    rows = [_row(body["dimensions"], r) for r in raw]
    meta = {"site": site, "start_date": start.isoformat(), "end_date": end.isoformat(), "search_type": search_type,
            "dimensions": body["dimensions"], "data_state": body["dataState"], "rows": len(rows), "truncated": truncated}
    if metadata.get("firstIncompleteDate"):
        meta["first_incomplete_date"] = metadata["firstIncompleteDate"]
        meta["note_incomplete"] = f"Data from {metadata['firstIncompleteDate']} onward is still being processed and may change."
    if metadata.get("firstIncompleteHour"):
        meta["first_incomplete_hour"] = metadata["firstIncompleteHour"]
    if truncated:
        meta["note_truncated"] = "More rows exist than were fetched. Raise max_rows or narrow with filters."
    meta["_source"] = source(site, body)
    return rows, meta


def totals(site: str, start, end, search_type="web", filters=None, data_state="all") -> dict:
    body = build_body(start, end, [], search_type, filters, "auto", data_state)
    rows = api.search_analytics(site, {**body, "rowLimit": 1}).get("rows", [])
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr": None, "position": None}
    return _row([], rows[0])


def pct_change(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round((cur - prev) / prev * 100, 1)


def _keyed(rows: list[dict], dims: list[str]) -> dict:
    return {tuple(r[d] for d in dims): r for r in rows}


def _weighted_position(rows: list[dict]) -> float | None:
    impressions = sum(r["impressions"] for r in rows)
    if not impressions:
        return None
    return round(sum((r["position"] or 0) * r["impressions"] for r in rows) / impressions, 2)


# ---------------------------------------------------------------- overview + compare
def overview(site: str, start_date=None, end_date=None, days=28, search_type="web") -> dict:
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    p_start, p_end = previous_range(start, end)
    cur = totals(site, start, end, search_type)
    prev = totals(site, p_start, p_end, search_type)
    daily, _ = query(site, start.isoformat(), end.isoformat(), None, ["date"], search_type, max_rows=1000)
    top_pages, _ = query(site, start.isoformat(), end.isoformat(), None, ["page"], search_type, max_rows=10)
    result = {
        "site": site, "search_type": search_type,
        "period": {"start": start.isoformat(), "end": end.isoformat(), "end_basis": basis},
        "previous_period": {"start": p_start.isoformat(), "end": p_end.isoformat()},
        "totals": cur, "previous_totals": prev,
        "change_pct": {k: pct_change(cur.get(k), prev.get(k)) for k in ("clicks", "impressions", "ctr")},
        "position_change": None if cur.get("position") is None or prev.get("position") is None
        else round(cur["position"] - prev["position"], 2),
        "daily": daily,
        "top_pages": top_pages,
    }
    if search_type not in NO_QUERY_TYPES:
        top_queries, _ = query(site, start.isoformat(), end.isoformat(), None, ["query"], search_type, max_rows=10)
        result["top_queries"] = top_queries
        visible, meta = query(site, start.isoformat(), end.isoformat(), None, ["query"], search_type, max_rows=25000)
        shown = sum(r["clicks"] for r in visible)
        if cur.get("clicks"):
            result["clicks_from_visible_queries_pct"] = round(shown / cur["clicks"] * 100, 1)
            result["note_anonymised"] = ("The rest of the clicks come from anonymised queries Google doesn't report"
                                         + (", or from rows past the fetched limit." if meta["truncated"] else "."))
    result["_source"] = source(site, build_body(start, end, [], search_type))
    return result


def compare(site: str, dimension: str, start_date=None, end_date=None, days=28, previous_start=None,
            previous_end=None, search_type="web", filters=None, top_n=25, min_impressions=10) -> dict:
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    if previous_start and previous_end:
        p_start, p_end = resolve_range(previous_start, previous_end, None)
    else:
        p_start, p_end = previous_range(start, end)
    cur, _ = query(site, start.isoformat(), end.isoformat(), None, [dimension], search_type, filters, max_rows=25000)
    prev, _ = query(site, p_start.isoformat(), p_end.isoformat(), None, [dimension], search_type, filters, max_rows=25000)
    a = {r[dimension]: r for r in cur}
    b = {r[dimension]: r for r in prev}
    merged = []
    for key in set(a) | set(b):
        x, y = a.get(key) or {}, b.get(key) or {}
        if max(x.get("impressions", 0), y.get("impressions", 0)) < min_impressions:
            continue
        merged.append({
            dimension: key,
            "clicks": x.get("clicks", 0), "clicks_prev": y.get("clicks", 0),
            "clicks_delta": x.get("clicks", 0) - y.get("clicks", 0),
            "impressions": x.get("impressions", 0), "impressions_prev": y.get("impressions", 0),
            "position": x.get("position"), "position_prev": y.get("position"),
            "status": "new" if not y else "lost" if not x else "both",
        })
    merged.sort(key=lambda r: r["clicks_delta"])
    return {
        "site": site, "dimension": dimension, "search_type": search_type,
        "period": [start.isoformat(), end.isoformat()], "previous_period": [p_start.isoformat(), p_end.isoformat()],
        "end_basis": basis, "rows_compared": len(merged),
        "biggest_losers": [r for r in merged[:top_n] if r["clicks_delta"] < 0],
        "biggest_winners": [r for r in reversed(merged[-top_n:]) if r["clicks_delta"] > 0],
        "new_count": sum(1 for r in merged if r["status"] == "new"),
        "lost_count": sum(1 for r in merged if r["status"] == "lost"),
        "note": "Rows below min_impressions in both periods are skipped.",
    }


# ---------------------------------------------------------------- opportunities
def opportunities(site: str, start_date=None, end_date=None, days=28, min_impressions=100,
                  position_from=4.0, position_to=20.0, top_n=50, search_type="web") -> dict:
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    rows, meta = query(site, start.isoformat(), end.isoformat(), None, ["query", "page"], search_type, max_rows=50000)
    # Benchmark = this site's own median CTR per rounded position (no generic industry CTR curve).
    by_pos: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r["impressions"] >= min_impressions and r["position"]:
            by_pos[round(r["position"])].append(r["ctr"] or 0.0)
    median = {p: statistics.median(v) for p, v in by_pos.items() if len(v) >= 5}
    striking, low_ctr, gaps = [], [], []
    for r in rows:
        if r["impressions"] < min_impressions or not r["position"]:
            continue
        bench = median.get(round(r["position"]))
        if position_from <= r["position"] <= position_to:
            striking.append({**r, "site_median_ctr_at_position": r4(bench)})
        if r["position"] < position_from + 0.5 and bench is not None and (r["ctr"] or 0) < bench * 0.5:
            low_ctr.append({**r, "site_median_ctr_at_position": r4(bench)})
        if r["position"] > position_to:
            gaps.append(r)
    for bucket in (striking, low_ctr, gaps):
        bucket.sort(key=lambda r: -r["impressions"])
    return {
        **meta, "end_basis": basis,
        "striking_distance": striking[:top_n], "striking_distance_total": len(striking),
        "low_ctr_top_positions": low_ctr[:top_n], "low_ctr_total": len(low_ctr),
        "content_gaps": gaps[:top_n], "content_gaps_total": len(gaps),
        "method": (f"Striking distance: position {position_from}-{position_to}, >= {min_impressions} impressions. "
                   "Low CTR: top positions under half of this site's own median CTR at that position. "
                   f"Content gaps: demand exists (>= {min_impressions} impressions) but the page ranks below {position_to}."),
    }


def url_key(url: str) -> str:
    """Normalise a URL to spot variants of the same page (www, protocol, slash, parameters, case)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = parts.path.lower().rstrip("/") or "/"
    return host + path


def _variant_reasons(urls: list[str]) -> list[str]:
    parts = [urlsplit(u) for u in urls]
    reasons = []
    if len({p.scheme for p in parts}) > 1:
        reasons.append("http vs https")
    if len({(p.hostname or "").startswith("www.") for p in parts}) > 1:
        reasons.append("www vs non-www")
    if len({p.path.endswith("/") for p in parts if p.path not in ("", "/")}) > 1:
        reasons.append("trailing slash")
    if len({bool(p.query) for p in parts}) > 1 or len({p.query for p in parts}) > 1:
        reasons.append("query parameters")
    if len({p.path for p in parts}) > 1 and len({p.path.lower() for p in parts}) == 1:
        reasons.append("letter case")
    return reasons or ["same normalised URL"]


def cannibalization(site: str, start_date=None, end_date=None, days=90, min_impressions=50,
                    min_share=0.1, top_n=50, search_type="web") -> dict:
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    rows, meta = query(site, start.isoformat(), end.isoformat(), None, ["query", "page"], search_type, max_rows=50000)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["query"]].append(r)
    competing, variants = [], []
    for q, pages in groups.items():
        total = sum(p["impressions"] for p in pages)
        if total < min_impressions:
            continue
        strong = sorted((p for p in pages if p["impressions"] / total >= min_share), key=lambda p: -p["impressions"])
        if len(strong) < 2:
            continue
        entry = {"query": q, "total_impressions": total, "total_clicks": sum(p["clicks"] for p in pages),
                 "pages": [{"page": p["page"], "impressions": p["impressions"], "clicks": p["clicks"],
                            "position": p["position"], "share": round(p["impressions"] / total, 3)} for p in strong]}
        keys = defaultdict(list)
        for p in strong:
            keys[url_key(p["page"])].append(p["page"])
        dupes = [v for v in keys.values() if len(v) > 1]
        if dupes:
            entry["url_variants"] = [{"urls": v, "differ_by": _variant_reasons(v)} for v in dupes]
            variants.append(entry)
        if len(keys) >= 2:
            competing.append(entry)
    competing.sort(key=lambda g: -g["total_impressions"])
    variants.sort(key=lambda g: -g["total_impressions"])
    return {**meta, "end_basis": basis,
            "queries_with_competing_pages": len(competing), "competing_pages": competing[:top_n],
            "queries_split_across_url_variants": len(variants), "url_variant_splits": variants[:top_n],
            "method": (f"Queries with >= {min_impressions} impressions where 2+ URLs each get >= {int(min_share * 100)}%. "
                       "URL variants (www/non-www, http/https, trailing slash, parameters, case) are listed separately: "
                       "those are a canonical/redirect problem, not two pages competing.")}


# ---------------------------------------------------------------- traffic drops (exact decomposition)
def _decompose(c0, i0, c1, i1) -> tuple[float, float]:
    """clicks = impressions x CTR, so Δclicks = ΔI·CTR0 + I1·ΔCTR exactly."""
    ctr0 = c0 / i0 if i0 else 0.0
    ctr1 = c1 / i1 if i1 else 0.0
    return (i1 - i0) * ctr0, i1 * (ctr1 - ctr0)


def traffic_drops(site: str, dimension: str = "page", start_date=None, end_date=None, days=28, search_type="web",
                  min_clicks_prev=10, position_threshold=2.0, top_n=25, explain_top=5) -> dict:
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    p_start, p_end = previous_range(start, end)
    cur, _ = query(site, start.isoformat(), end.isoformat(), None, [dimension], search_type, max_rows=25000)
    prev, _ = query(site, p_start.isoformat(), p_end.isoformat(), None, [dimension], search_type, max_rows=25000)
    a, b = {r[dimension]: r for r in cur}, {r[dimension]: r for r in prev}
    drops = []
    for key, old in b.items():
        c0, i0, p0 = old["clicks"], old["impressions"], old["position"]
        if c0 < min_clicks_prev:
            continue
        new = a.get(key, {"clicks": 0, "impressions": 0, "position": None})
        c1, i1, p1 = new["clicks"], new["impressions"], new["position"]
        if c1 >= c0:
            continue
        impr_effect, ctr_effect = _decompose(c0, i0, c1, i1)
        pos_delta = None if p0 is None or p1 is None else round(p1 - p0, 2)
        if i1 == 0:
            cause = "disappeared"
        elif pos_delta is not None and pos_delta >= position_threshold:
            cause = "ranking_loss"
        elif ctr_effect <= impr_effect:
            cause = "ctr_drop"
        else:
            cause = "demand_drop"
        drops.append({dimension: key, "clicks_prev": c0, "clicks": c1, "clicks_lost": c0 - c1,
                      "clicks_change_pct": pct_change(c1, c0), "impressions_prev": i0, "impressions": i1,
                      "ctr_prev": r4(c0 / i0 if i0 else None), "ctr": r4(c1 / i1 if i1 else None),
                      "position_prev": p0, "position": p1, "position_change": pos_delta,
                      "loss_from_fewer_impressions": round(impr_effect, 1), "loss_from_lower_ctr": round(ctr_effect, 1),
                      "diagnosis": cause})
    drops.sort(key=lambda r: -r["clicks_lost"])
    top = drops[:top_n]
    other = "query" if dimension == "page" else "page" if dimension == "query" else None
    if other and explain_top and search_type not in NO_QUERY_TYPES:
        for item in top[:explain_top]:
            flt = [{"dimension": dimension, "operator": "equals", "expression": item[dimension]}]
            now_rows, _ = query(site, start.isoformat(), end.isoformat(), None, [other], search_type, flt, max_rows=1000)
            old_rows, _ = query(site, p_start.isoformat(), p_end.isoformat(), None, [other], search_type, flt, max_rows=1000)
            na, ob = {r[other]: r for r in now_rows}, {r[other]: r for r in old_rows}
            detail = []
            for k in set(na) | set(ob):
                x, y = na.get(k, {}), ob.get(k, {})
                detail.append({other: k, "clicks_prev": y.get("clicks", 0), "clicks": x.get("clicks", 0),
                               "position_prev": y.get("position"), "position": x.get("position")})
            detail.sort(key=lambda d: d["clicks"] - d["clicks_prev"])
            item[f"top_losing_{other}s"] = [d for d in detail[:5] if d["clicks"] < d["clicks_prev"]]
    causes = defaultdict(lambda: {"count": 0, "clicks_lost": 0})
    for d in drops:
        causes[d["diagnosis"]]["count"] += 1
        causes[d["diagnosis"]]["clicks_lost"] += d["clicks_lost"]
    return {
        "site": site, "dimension": dimension, "search_type": search_type,
        "period": [start.isoformat(), end.isoformat()], "previous_period": [p_start.isoformat(), p_end.isoformat()],
        "end_basis": basis, "dropping": len(drops), "total_clicks_lost": sum(d["clicks_lost"] for d in drops),
        "by_diagnosis": dict(causes), "drops": top,
        "diagnosis_key": {
            "ranking_loss": f"average position worsened by >= {position_threshold}",
            "ctr_drop": "position about the same, CTR fell (SERP features, competitors' snippets, title/description changes)",
            "demand_drop": "position about the same, fewer impressions (less search demand, seasonality, or lost long-tail queries)",
            "disappeared": "no impressions this period (check with inspect_url: deindexed, redirected, or no longer ranking)",
        },
        "method": "Click change split exactly into fewer impressions (ΔI × old CTR) and lower CTR (new I × ΔCTR). "
                  "Based on Search Console numbers only; it can't see the SERP itself.",
    }


# ---------------------------------------------------------------- content decay
def content_decay(site: str, dimension: str = "page", periods: int = 3, period_days: int = 30, search_type="web",
                  min_clicks=20, min_decline_pct=20.0, top_n=50, end_date=None) -> dict:
    end = dt.date.fromisoformat(end_date) if end_date else latest_complete_date(site, search_type)
    windows = []
    for k in range(periods):
        w_end = end - dt.timedelta(days=(periods - 1 - k) * period_days)
        w_start = w_end - dt.timedelta(days=period_days - 1)
        windows.append((w_start, w_end))
    data = []
    for w_start, w_end in windows:
        rows, _ = query(site, w_start.isoformat(), w_end.isoformat(), None, [dimension], search_type, max_rows=25000)
        data.append({r[dimension]: r for r in rows})
    decaying = []
    for key, first in data[0].items():
        if first["clicks"] < min_clicks:
            continue
        series = [d.get(key, {"clicks": 0, "impressions": 0, "position": None}) for d in data]
        clicks = [s["clicks"] for s in series]
        if not all(clicks[i] > clicks[i + 1] for i in range(len(clicks) - 1)):
            continue
        decline = (clicks[0] - clicks[-1]) / clicks[0] * 100
        if decline < min_decline_pct:
            continue
        decaying.append({dimension: key, "clicks_by_period": clicks,
                         "impressions_by_period": [s["impressions"] for s in series],
                         "position_by_period": [s["position"] for s in series],
                         "decline_pct": round(decline, 1), "clicks_lost": clicks[0] - clicks[-1]})
    decaying.sort(key=lambda r: -r["clicks_lost"])
    return {"site": site, "dimension": dimension, "search_type": search_type,
            "periods": [[s.isoformat(), e.isoformat()] for s, e in windows],
            "decaying": len(decaying), "results": decaying[:top_n],
            "method": f"Clicks fell in every one of {periods} consecutive {period_days}-day periods (oldest first), "
                      f"starting from >= {min_clicks} clicks, total decline >= {min_decline_pct}%."}


# ---------------------------------------------------------------- alerts
def alerts(site: str, days=7, search_type="web", min_impressions=100, min_clicks=20, position_drop=5.0,
           ctr_drop_pct=50.0, click_drop_pct=30.0, site_drop_pct=20.0, max_per_type=25) -> dict:
    start, end, basis = complete_range(site, None, None, days, search_type)
    p_start, p_end = previous_range(start, end)
    found: list[dict] = []
    cur_t, prev_t = totals(site, start, end, search_type), totals(site, p_start, p_end, search_type)
    change = pct_change(cur_t["clicks"], prev_t["clicks"])
    if change is not None and change <= -site_drop_pct:
        found.append({"type": "site_clicks_drop", "severity": "critical", "clicks_prev": prev_t["clicks"],
                      "clicks": cur_t["clicks"], "change_pct": change})
    if search_type not in NO_QUERY_TYPES:
        q_now, _ = query(site, start.isoformat(), end.isoformat(), None, ["query"], search_type, max_rows=25000)
        q_old, _ = query(site, p_start.isoformat(), p_end.isoformat(), None, ["query"], search_type, max_rows=25000)
        old = {r["query"]: r for r in q_old}
        pos_alerts, ctr_alerts = [], []
        for r in q_now:
            o = old.get(r["query"])
            if not o or r["impressions"] < min_impressions or o["impressions"] < min_impressions:
                continue
            if r["position"] and o["position"] and r["position"] - o["position"] >= position_drop:
                fell_off = o["position"] <= 10 < r["position"]
                pos_alerts.append({"type": "position_drop", "severity": "critical" if fell_off or r["position"] - o["position"] >= 10 else "warning",
                                   "query": r["query"], "position_prev": o["position"], "position": r["position"],
                                   "impressions": r["impressions"], "fell_off_page_one": fell_off})
            if o["ctr"] and r["ctr"] is not None and r["position"] and o["position"] and abs(r["position"] - o["position"]) < 2:
                drop = (r["ctr"] - o["ctr"]) / o["ctr"] * 100
                if drop <= -ctr_drop_pct:
                    ctr_alerts.append({"type": "ctr_collapse", "severity": "critical" if o["clicks"] >= 50 else "warning",
                                       "query": r["query"], "ctr_prev": o["ctr"], "ctr": r["ctr"], "change_pct": round(drop, 1),
                                       "position": r["position"], "clicks_prev": o["clicks"], "clicks": r["clicks"]})
        pos_alerts.sort(key=lambda x: -(x["position"] - x["position_prev"]))
        ctr_alerts.sort(key=lambda x: -x["clicks_prev"])
        found += pos_alerts[:max_per_type] + ctr_alerts[:max_per_type]
    p_now, _ = query(site, start.isoformat(), end.isoformat(), None, ["page"], search_type, max_rows=25000)
    p_old, _ = query(site, p_start.isoformat(), p_end.isoformat(), None, ["page"], search_type, max_rows=25000)
    now = {r["page"]: r for r in p_now}
    page_alerts = []
    for page, o in ((r["page"], r) for r in p_old):
        if o["clicks"] < min_clicks:
            continue
        r = now.get(page)
        if r is None or r["impressions"] == 0:
            page_alerts.append({"type": "page_disappeared", "severity": "critical", "page": page,
                                "clicks_prev": o["clicks"], "impressions_prev": o["impressions"]})
            continue
        drop = (r["clicks"] - o["clicks"]) / o["clicks"] * 100
        if drop <= -click_drop_pct:
            page_alerts.append({"type": "page_clicks_drop",
                                "severity": "critical" if drop <= -50 and o["clicks"] >= 100 else "warning",
                                "page": page, "clicks_prev": o["clicks"], "clicks": r["clicks"], "change_pct": round(drop, 1),
                                "position_prev": o["position"], "position": r["position"]})
    page_alerts.sort(key=lambda x: -x["clicks_prev"])
    found += page_alerts[: max_per_type * 2]
    found.sort(key=lambda x: 0 if x["severity"] == "critical" else 1)
    return {"site": site, "search_type": search_type, "period": [start.isoformat(), end.isoformat()],
            "previous_period": [p_start.isoformat(), p_end.isoformat()], "end_basis": basis,
            "critical": sum(1 for f in found if f["severity"] == "critical"),
            "warnings": sum(1 for f in found if f["severity"] == "warning"), "alerts": found,
            "thresholds": {"min_impressions": min_impressions, "min_clicks": min_clicks, "position_drop": position_drop,
                           "ctr_drop_pct": ctr_drop_pct, "click_drop_pct": click_drop_pct, "site_drop_pct": site_drop_pct}}


# ---------------------------------------------------------------- sections
def section_of(url: str, level: int, prefixes: list[str] | None, with_host: bool) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if prefixes:
        match = max((p for p in prefixes if path.startswith(p)), key=len, default="(other)")
        label = match
    else:
        segs = [s for s in path.split("/") if s]
        dirs = segs if path.endswith("/") else segs[:-1]
        chosen = dirs[:level]
        label = "/" + "/".join(chosen) + ("/" if chosen else "")
    return f"{(parts.hostname or '').lower()}{label}" if with_host else label


def sections(site: str, level=1, prefixes=None, start_date=None, end_date=None, days=28, search_type="web",
             compare_previous=True, top_n=50) -> dict:
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    rows, meta = query(site, start.isoformat(), end.isoformat(), None, ["page"], search_type, max_rows=50000)
    with_host = len({(urlsplit(r["page"]).hostname or "") for r in rows}) > 1
    prev_rows = []
    if compare_previous:
        p_start, p_end = previous_range(start, end)
        prev_rows, _ = query(site, p_start.isoformat(), p_end.isoformat(), None, ["page"], search_type, max_rows=50000)

    def group(rs):
        g = defaultdict(list)
        for r in rs:
            g[section_of(r["page"], level, prefixes, with_host)].append(r)
        return g

    now_g, old_g = group(rows), group(prev_rows)
    table = []
    for name, rs in now_g.items():
        clicks, impr = sum(r["clicks"] for r in rs), sum(r["impressions"] for r in rs)
        item = {"section": name, "pages": len(rs), "clicks": clicks, "impressions": impr,
                "ctr": r4(clicks / impr) if impr else None, "position": _weighted_position(rs)}
        if compare_previous:
            old = old_g.get(name, [])
            oc = sum(r["clicks"] for r in old)
            item.update(clicks_prev=oc, clicks_change_pct=pct_change(clicks, oc),
                        impressions_prev=sum(r["impressions"] for r in old), position_prev=_weighted_position(old))
        table.append(item)
    table.sort(key=lambda r: -r["clicks"])
    total_clicks = sum(r["clicks"] for r in table) or 1
    for item in table:
        item["share_of_clicks_pct"] = round(item["clicks"] / total_clicks * 100, 1)
    return {**meta, "end_basis": basis, "level": level, "prefixes": prefixes, "sections": table[:top_n],
            "section_count": len(table),
            "note": "Section position = impression-weighted average of its pages' positions (close to, not identical with, the UI)."}


# ---------------------------------------------------------------- query segments
QUESTION_WORDS = {
    "en": ["what", "how", "why", "when", "where", "which", "who", "whom", "whose", "can", "does", "do", "is", "are", "should", "will"],
    "nl": ["wat", "hoe", "waarom", "wanneer", "waar", "welke", "welk", "wie", "kan", "kun", "is", "zijn", "moet"],
    "sv": ["vad", "hur", "varför", "när", "var", "vilken", "vilket", "vilka", "vem", "kan", "är", "ska"],
    "pt": ["o que", "como", "porque", "por que", "porquê", "quando", "onde", "qual", "quais", "quem", "quanto", "é"],
    "fr": ["quoi", "comment", "pourquoi", "quand", "où", "quel", "quelle", "quels", "quelles", "qui", "est-ce", "combien", "peut"],
    "de": ["was", "wie", "warum", "wann", "wo", "welche", "welcher", "wer", "kann", "ist"],
}
_FRAGMENTS = {"yes", "no", "ok", "okay", "go on", "continue", "more", "tell me more", "and", "sure", "thanks", "why", "how so"}
_FRAGMENT_STARTS = ("what about ", "how about ", "and what ", "and how ", "tell me more", "go on")


def _question(q: str) -> bool:
    low = q.lower().strip()
    if low.endswith("?"):
        return True
    for words in QUESTION_WORDS.values():
        for w in words:
            if low == w or low.startswith(w + " "):
                return True
    return False


def segments(site: str, start_date=None, end_date=None, days=28, search_type="web", brand_terms=None, top_n=10) -> dict:
    if search_type in NO_QUERY_TYPES:
        raise ValueError(f"{search_type} has no query data.")
    start, end, basis = complete_range(site, start_date, end_date, days, search_type)
    rows, meta = query(site, start.isoformat(), end.isoformat(), None, ["query"], search_type, max_rows=50000)
    brands = [b.strip().lower() for b in (brand_terms or []) if b.strip()]
    seg: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        q = r["query"]
        low = q.lower().strip()
        words = len(low.split())
        if brands:
            seg["brand"]["branded" if any(b in low for b in brands) else "non-branded"].append(r)
        seg["intent"]["question" if _question(q) else "other"].append(r)
        seg["length"]["1 word" if words == 1 else "2 words" if words == 2 else "3 words" if words == 3
                      else "4-5 words" if words <= 5 else "6+ words"].append(r)
        pos = r["position"] or 999
        seg["results_page"]["page 1 (1-10)" if pos <= 10.5 else "page 2 (11-20)" if pos <= 20.5 else "page 3+"].append(r)
        if low in _FRAGMENTS or low.startswith(_FRAGMENT_STARTS):
            seg["conversational"]["fragment"].append(r)

    def summarise(rs):
        clicks, impr = sum(r["clicks"] for r in rs), sum(r["impressions"] for r in rs)
        return {"queries": len(rs), "clicks": clicks, "impressions": impr, "ctr": r4(clicks / impr) if impr else None,
                "position": _weighted_position(rs),
                "top_queries": [{"query": r["query"], "clicks": r["clicks"], "impressions": r["impressions"],
                                 "position": r["position"]} for r in sorted(rs, key=lambda r: -r["clicks"])[:top_n]]}

    out = {name: {label: summarise(rs) for label, rs in groups.items()} for name, groups in seg.items()}
    total = totals(site, start, end, search_type)
    shown = sum(r["clicks"] for r in rows)
    return {**meta, "end_basis": basis, "brand_terms": brands or None, "segments": out,
            "property_clicks": total["clicks"],
            "clicks_from_visible_queries_pct": round(shown / total["clicks"] * 100, 1) if total["clicks"] else None,
            "notes": [
                "Branded split needs brand_terms (or a 'brand_terms' property note)." if not brands else
                "Branded = query contains any brand term (substring, case-insensitive).",
                "Question detection covers English, Dutch, Swedish, Portuguese, French and German question words.",
                "'conversational' is a heuristic for chat-style fragments (e.g. 'yes', 'go on', 'what about ...'); "
                "it doesn't prove a query came from AI Mode.",
            ]}


# ---------------------------------------------------------------- multi-site dashboard
def dashboard(sites: list[dict], days=28, drop_pct=20.0) -> dict:
    def one(entry):
        site = entry["siteUrl"]
        if entry.get("permissionLevel") == "siteUnverifiedUser":
            return {"site": site, "status": "no access (unverified)"}
        try:
            start, end, _ = complete_range(site, None, None, days, "web")
            p_start, p_end = previous_range(start, end)
            cur, prev = totals(site, start, end), totals(site, p_start, p_end)
        except api.ApiError as exc:
            return {"site": site, "status": f"error: {exc.message[:120]}"}
        change = pct_change(cur["clicks"], prev["clicks"])
        status = ("dropping" if change is not None and change <= -drop_pct else
                  "growing" if change is not None and change >= drop_pct else "stable")
        return {"site": site, "period_end": end.isoformat(), "clicks": cur["clicks"], "clicks_prev": prev["clicks"],
                "clicks_change_pct": change, "impressions": cur["impressions"],
                "impressions_change_pct": pct_change(cur["impressions"], prev["impressions"]),
                "ctr": cur["ctr"], "position": cur["position"], "position_prev": prev["position"], "status": status}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(one, sites))
    rows.sort(key=lambda r: (r.get("status") != "dropping", -(r.get("clicks") or 0)))
    return {"properties": len(rows), "days": days,
            "dropping": sum(1 for r in rows if r.get("status") == "dropping"),
            "growing": sum(1 for r in rows if r.get("status") == "growing"), "dashboard": rows,
            "note": f"Each property compares its last {days} complete days with the {days} days before. "
                    f"'dropping'/'growing' = clicks changed by {drop_pct}% or more."}


# ---------------------------------------------------------------- verification
def verify(site: str, metric: str, expected: float, start_date=None, end_date=None, days=28, search_type="web",
           page=None, query_text=None, country=None, device=None, tolerance_pct=1.0) -> dict:
    if metric not in ("clicks", "impressions", "ctr", "position"):
        raise ValueError("metric must be clicks, impressions, ctr or position")
    start, end = resolve_range(start_date, end_date, days)
    filters = [{"dimension": d, "operator": "equals", "expression": v}
               for d, v in (("page", page), ("query", query_text), ("country", country), ("device", device)) if v]
    actual = totals(site, start, end, search_type, filters or None)[metric]
    if actual is None:
        verdict, diff = "no data", None
    else:
        diff = actual - expected
        allowed = max(abs(expected) * tolerance_pct / 100, 0.05 if metric == "position" else 0.0005 if metric == "ctr" else 0.5)
        verdict = "confirmed" if abs(diff) <= allowed else "mismatch"
    return {"site": site, "metric": metric, "expected": expected, "actual": actual,
            "difference": None if diff is None else round(diff, 4), "verdict": verdict,
            "checked_with": {"start_date": start.isoformat(), "end_date": end.isoformat(), "search_type": search_type,
                             "filters": filters, "data_state": "all"},
            "note": "Use the exact dates and filters of the original claim; recent days can change until they are final."}


def export(rows: list[dict], stem: str) -> str | None:
    if not rows:
        return None
    columns = list(rows[0].keys())
    return write_csv(stem, columns, [[r.get(c) for c in columns] for r in rows])

