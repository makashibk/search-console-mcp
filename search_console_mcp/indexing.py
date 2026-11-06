"""Page-indexing style reports built from the latest stored inspection of each URL."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict

from . import db, playbook
from .util import now_utc, parse_iso, write_csv

COLUMNS = ["url", "coverage_state", "is_indexed", "verdict", "page_fetch_state", "indexing_state", "robots_txt_state",
           "last_crawl_time", "crawled_as", "google_canonical", "user_canonical", "sitemaps", "referring_urls",
           "rich_results", "rich_issue_count", "inspected_at", "source", "source_detail", "error"]


def _latest_rows(site: str, sitemap: str | None = None, url_contains: str | None = None,
                 url_regex: str | None = None, source: str | None = None) -> list[dict]:
    sql = ("SELECT q.url, q.source, q.source_detail, i.* FROM url_queue q JOIN inspections i ON i.id = q.latest_id "
           "WHERE q.site_url = ?")
    args: list = [site]
    if url_contains:
        sql += " AND q.url LIKE ?"
        args.append(f"%{url_contains}%")
    if source:
        sql += " AND q.source = ?"
        args.append(source)
    if sitemap:
        sql += " AND (q.source_detail = ? OR i.sitemaps LIKE ?)"
        args.extend([sitemap, f"%{json.dumps(sitemap)[1:-1]}%"])
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if url_regex:
        pattern = re.compile(url_regex)
        rows = [r for r in rows if pattern.search(r["url"])]
    for r in rows:
        r.pop("raw", None)
        for key in ("sitemaps", "referring_urls", "rich_results"):
            try:
                r[key] = json.loads(r[key]) if r.get(key) else []
            except ValueError:
                r[key] = []
        r["is_indexed"] = None if r.get("is_indexed") is None else bool(r["is_indexed"])
    return rows


def _crawl_age_bucket(ts: str | None) -> str:
    when = parse_iso(ts)
    if when is None:
        return "never crawled / unknown"
    age = (now_utc() - when).days
    if age <= 7:
        return "0-7 days"
    if age <= 30:
        return "8-30 days"
    if age <= 90:
        return "31-90 days"
    return "over 90 days"


def report(site: str, sitemap=None, url_contains=None, url_regex=None, source=None, samples: int = 5) -> dict:
    rows = _latest_rows(site, sitemap, url_contains, url_regex, source)
    with db.connect() as conn:
        queued = conn.execute("SELECT COUNT(*) AS n, SUM(latest_id IS NULL) AS never FROM url_queue WHERE site_url=?",
                              (site,)).fetchone()
    indexed = [r for r in rows if r["is_indexed"] is True]
    not_indexed = [r for r in rows if r["is_indexed"] is False]
    errored = [r for r in rows if r.get("error")]

    reasons = defaultdict(list)
    for r in rows:
        if r.get("error"):
            continue
        reasons[r.get("coverage_state") or "(none)"].append(r)
    reason_table = []
    for state, items in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        entry = playbook.lookup(state) or {}
        reason_table.append({
            "reason": state,
            "pages": len(items),
            "indexed": entry.get("indexed", playbook.is_indexed(state, None)),
            "source": entry.get("source", "unknown"),
            "sample_urls": [i["url"] for i in items[:samples]],
            "what_it_means": entry.get("meaning"),
            "check": entry.get("check"),
            "fix": entry.get("fix"),
        })

    canon_other = [r for r in rows if r.get("google_canonical") and r["google_canonical"] != r["url"]]
    canon_conflict = [r for r in rows if r.get("google_canonical") and r.get("user_canonical")
                      and r["google_canonical"] != r["user_canonical"]]
    not_in_sitemap_indexed = [r for r in indexed if not r["sitemaps"]]

    rich_types: dict[str, dict] = {}
    issue_counter: Counter = Counter()
    for r in rows:
        for item in r["rich_results"]:
            t = rich_types.setdefault(item.get("type") or "unknown", {"pages": 0, "pages_with_errors": 0, "pages_with_warnings": 0})
            t["pages"] += 1
            t["pages_with_errors"] += 1 if item.get("errors") else 0
            t["pages_with_warnings"] += 1 if item.get("warnings") else 0
            for msg in item.get("issues", []):
                issue_counter[f"{item.get('type')}: {msg}"] += 1

    return {
        "site": site,
        "filters": {k: v for k, v in {"sitemap": sitemap, "url_contains": url_contains, "url_regex": url_regex,
                                      "source": source}.items() if v},
        "urls_in_queue": queued["n"] or 0,
        "not_yet_inspected": queued["never"] or 0,
        "inspected": len(rows),
        "indexed": len(indexed),
        "not_indexed": len(not_indexed),
        "unknown_or_error": len(rows) - len(indexed) - len(not_indexed),
        "inspection_errors": len(errored),
        "why_pages_arent_indexed_and_indexed_reasons": reason_table,
        "page_fetch_state": dict(Counter(r.get("page_fetch_state") or "(none)" for r in rows).most_common()),
        "indexing_state": dict(Counter(r.get("indexing_state") or "(none)" for r in rows).most_common()),
        "robots_txt_state": dict(Counter(r.get("robots_txt_state") or "(none)" for r in rows).most_common()),
        "crawled_as": dict(Counter(r.get("crawled_as") or "(none)" for r in rows).most_common()),
        "last_crawl_age": dict(Counter(_crawl_age_bucket(r.get("last_crawl_time")) for r in rows).most_common()),
        "canonical": {
            "google_chose_other_url": len(canon_other),
            "google_vs_declared_conflict": len(canon_conflict),
            "samples": [{"url": r["url"], "google": r["google_canonical"], "declared": r.get("user_canonical")}
                        for r in canon_conflict[:samples]],
        },
        "indexed_but_not_in_any_sitemap": {"count": len(not_in_sitemap_indexed),
                                           "samples": [r["url"] for r in not_in_sitemap_indexed[:samples]]},
        "rich_results": {"by_type": rich_types, "top_issues": issue_counter.most_common(20)},
        "note": "Built from this server's own URL Inspection results (latest per URL), not Google's Page indexing report. "
                "Coverage reason names match Search Console.",
    }


def list_urls(site: str, coverage_state=None, indexed=None, url_contains=None, url_regex=None, sitemap=None,
              rich_type=None, has_rich_issues=None, crawled_before_days=None, sort_by="last_crawl_time",
              descending=False, limit=100, offset=0, export_csv=False) -> dict:
    rows = _latest_rows(site, sitemap, url_contains, url_regex)
    if coverage_state:
        wanted = playbook.normalise(coverage_state)
        rows = [r for r in rows if playbook.normalise(r.get("coverage_state")) == wanted]
    if indexed is not None:
        rows = [r for r in rows if r["is_indexed"] is indexed]
    if rich_type:
        rows = [r for r in rows if any((i.get("type") or "").lower() == rich_type.lower() for i in r["rich_results"])]
    if has_rich_issues is not None:
        rows = [r for r in rows if bool(r.get("rich_issue_count")) is has_rich_issues]
    if crawled_before_days:
        cutoff = now_utc() - dt.timedelta(days=int(crawled_before_days))
        floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        rows = [r for r in rows if (parse_iso(r.get("last_crawl_time")) or floor) < cutoff]
    if sort_by not in ("last_crawl_time", "inspected_at", "url", "coverage_state", "rich_issue_count"):
        raise ValueError("sort_by must be last_crawl_time, inspected_at, url, coverage_state or rich_issue_count")
    rows.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or ""), reverse=descending)
    total = len(rows)
    result = {"site": site, "total_matching": total, "offset": offset, "returned": 0, "urls": []}
    if export_csv and rows:
        result["csv_file"] = write_csv(f"indexing-{site}", COLUMNS,
                                       [[json.dumps(r.get(c)) if isinstance(r.get(c), (list, dict)) else r.get(c)
                                         for c in COLUMNS] for r in rows])
    page = rows[offset: offset + max(1, min(limit, 1000))]
    result["urls"] = [{c: r.get(c) for c in COLUMNS if c not in ("source_detail",) and r.get(c) not in (None, [], "")}
                      for r in page]
    result["returned"] = len(page)
    return result


def changes(site: str, days: int = 30, limit: int = 200) -> dict:
    since = (now_utc() - dt.timedelta(days=days)).replace(microsecond=0).isoformat()
    sql = """
        WITH ranked AS (
            SELECT url, coverage_state, inspected_at, last_crawl_time,
                   ROW_NUMBER() OVER (PARTITION BY url ORDER BY id DESC) AS rn
            FROM inspections WHERE site_url = ? AND error IS NULL
        )
        SELECT cur.url, prev.coverage_state AS before, cur.coverage_state AS after,
               prev.inspected_at AS before_at, cur.inspected_at AS after_at, cur.last_crawl_time
        FROM ranked cur JOIN ranked prev ON prev.url = cur.url AND prev.rn = 2
        WHERE cur.rn = 1 AND cur.inspected_at >= ? AND IFNULL(cur.coverage_state,'') <> IFNULL(prev.coverage_state,'')
        ORDER BY cur.inspected_at DESC
    """
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, (site, since)).fetchall()]
    transitions = Counter(f"{r['before']} -> {r['after']}" for r in rows)
    newly_lost = [r for r in rows if playbook.is_indexed(r["before"], None) and playbook.is_indexed(r["after"], None) is False]
    newly_won = [r for r in rows if playbook.is_indexed(r["before"], None) is False and playbook.is_indexed(r["after"], None)]
    return {"site": site, "window_days": days, "changed_urls": len(rows),
            "dropped_out_of_index": len(newly_lost), "newly_indexed": len(newly_won),
            "transitions": transitions.most_common(30), "urls": rows[:limit]}


def daily_trend(site: str) -> list[dict]:
    """Per inspection day: how many URLs were inspected and how many were indexed (from that day's runs)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT pt_date, COUNT(*) AS inspected, SUM(is_indexed=1) AS indexed, SUM(is_indexed=0) AS not_indexed, "
            "SUM(error IS NOT NULL) AS errors FROM inspections WHERE site_url=? GROUP BY pt_date ORDER BY pt_date",
            (site,)).fetchall()
    return [dict(r) for r in rows]


def latest_for(site: str, urls: list[str]) -> dict[str, dict]:
    """Latest stored inspection fields for specific URLs."""
    found: dict[str, dict] = {}
    with db.connect() as conn:
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            sql = ("SELECT q.url, i.last_crawl_time, i.coverage_state, i.inspected_at, i.error FROM url_queue q "
                   "JOIN inspections i ON i.id = q.latest_id WHERE q.site_url = ? AND q.url IN "
                   f"({','.join('?' * len(chunk))})")
            for r in conn.execute(sql, [site, *chunk]):
                found[r["url"]] = dict(r)
    return found
