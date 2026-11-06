"""One-call SEO report for a property, saved as Markdown in the exports folder."""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from . import api, indexing, performance, robots
from .util import export_dir, safe_name


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:120]


def _table(rows: list[dict], columns: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    out = ["| " + " | ".join(label for _, label in columns) + " |", "|" + "---|" * len(columns)]
    for r in rows:
        out.append("| " + " | ".join(_cell(r.get(key)) for key, _ in columns) + " |")
    out.append("")
    return out


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def build(site: str, days: int = 28, top_n: int = 10) -> dict:
    sections, errors = {}, {}

    def run(name, fn):
        try:
            sections[name] = fn()
        except Exception as exc:  # one failing part shouldn't sink the report
            errors[name] = str(exc)[:300]

    run("overview", lambda: performance.overview(site, days=days))
    run("alerts", lambda: performance.alerts(site, days=7))
    run("drops", lambda: performance.traffic_drops(site, days=days, top_n=top_n, explain_top=0))
    run("opportunities", lambda: performance.opportunities(site, days=days, top_n=top_n))
    run("decay", lambda: performance.content_decay(site, top_n=top_n))
    run("cannibalization", lambda: performance.cannibalization(site, days=90, top_n=top_n))
    run("sitemaps", lambda: api.list_sitemaps(site))
    run("indexing", lambda: indexing.report(site, samples=3))
    host = site.split(":", 1)[1] if site.startswith("sc-domain:") else urlsplit(site).netloc
    run("robots", lambda: robots.history(host, limit=2))

    lines = [f"# SEO report: {site}", "", f"Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} local time. "
             "All numbers come from the Search Console API and this server's own checks.", ""]
    ov = sections.get("overview")
    if ov:
        t, p = ov["totals"], ov["previous_totals"]
        lines += [f"## Performance ({ov['period']['start']} to {ov['period']['end']}, vs previous {days} days)", "",
                  f"- Clicks: {t['clicks']:,} ({_pct(ov['change_pct']['clicks'])}, was {p['clicks']:,})",
                  f"- Impressions: {t['impressions']:,} ({_pct(ov['change_pct']['impressions'])})",
                  f"- CTR: {t['ctr']} | Average position: {t['position']} (change {ov['position_change']})", ""]
        if ov.get("clicks_from_visible_queries_pct") is not None:
            lines += [f"Visible queries account for {ov['clicks_from_visible_queries_pct']}% of clicks; the rest are anonymised.", ""]
        lines += ["### Top pages", ""] + _table(ov.get("top_pages", []), [("page", "Page"), ("clicks", "Clicks"),
                                                                        ("impressions", "Impr."), ("position", "Pos.")])
    al = sections.get("alerts")
    if al:
        lines += [f"## Alerts (last 7 days): {al['critical']} critical, {al['warnings']} warnings", ""]
        lines += _table(al["alerts"][:top_n * 2], [("severity", "Severity"), ("type", "Type"), ("query", "Query"),
                                                  ("page", "Page"), ("change_pct", "Change %"), ("position", "Pos.")])
    dr = sections.get("drops")
    if dr:
        lines += [f"## Traffic drops: {dr['dropping']} pages lost {dr['total_clicks_lost']:,} clicks", ""]
        lines += _table(dr["drops"], [("page", "Page"), ("clicks_prev", "Before"), ("clicks", "After"),
                                      ("position_change", "Pos. change"), ("diagnosis", "Diagnosis")])
    op = sections.get("opportunities")
    if op:
        lines += [f"## Striking distance (positions 4-20): {op['striking_distance_total']} query/page pairs", ""]
        lines += _table(op["striking_distance"], [("query", "Query"), ("page", "Page"), ("impressions", "Impr."),
                                                  ("position", "Pos."), ("ctr", "CTR")])
        lines += [f"## Content gaps (impressions, ranking below 20): {op['content_gaps_total']}", ""]
        lines += _table(op["content_gaps"], [("query", "Query"), ("page", "Page"), ("impressions", "Impr."), ("position", "Pos.")])
    de = sections.get("decay")
    if de:
        lines += [f"## Content decay: {de['decaying']} pages declining 3 periods in a row", ""]
        lines += _table(de["results"], [("page", "Page"), ("clicks_by_period", "Clicks per 30 days"), ("decline_pct", "Decline %")])
    ca = sections.get("cannibalization")
    if ca:
        lines += [f"## Cannibalisation: {ca['queries_with_competing_pages']} queries with competing pages, "
                  f"{ca['queries_split_across_url_variants']} split across URL variants", ""]
        lines += _table([{"query": c["query"], "pages": ", ".join(p["page"] for p in c["pages"][:3]),
                          "impressions": c["total_impressions"]} for c in ca["competing_pages"]],
                        [("query", "Query"), ("pages", "Pages"), ("impressions", "Impr.")])
    sm = sections.get("sitemaps")
    if sm is not None:
        bad = [m for m in sm if int(m.get("errors", 0) or 0) or int(m.get("warnings", 0) or 0)]
        lines += [f"## Sitemaps: {len(sm)} submitted, {len(bad)} with errors or warnings", ""]
        lines += _table(bad, [("path", "Sitemap"), ("errors", "Errors"), ("warnings", "Warnings"), ("lastDownloaded", "Last read")])
    ix = sections.get("indexing")
    if ix and ix["inspected"]:
        lines += [f"## Indexing (from {ix['inspected']} inspected URLs): {ix['indexed']} indexed, {ix['not_indexed']} not indexed", ""]
        lines += _table([r for r in ix["why_pages_arent_indexed_and_indexed_reasons"] if r["indexed"] is False],
                        [("reason", "Reason"), ("pages", "Pages"), ("source", "Source")])
    rb = sections.get("robots")
    if rb and rb["checks"]:
        last = rb["checks"][0]
        lines += ["## robots.txt", "", f"Last checked {last['checked_at']}: status {last['status_code']}, "
                  f"{last['size_bytes']} bytes, {'changed' if last['changed'] else 'unchanged'} since the check before.", ""]
    if errors:
        lines += ["## Parts that could not be generated", ""] + [f"- {k}: {v}" for k, v in errors.items()] + [""]

    path = export_dir() / f"seo-report-{safe_name(site)}-{dt.datetime.now().strftime('%Y%m%d-%H%M')}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    summary = {
        "report_file": str(path),
        "clicks_change_pct": ov["change_pct"]["clicks"] if ov else None,
        "critical_alerts": al["critical"] if al else None,
        "pages_dropping": dr["dropping"] if dr else None,
        "striking_distance_pairs": op["striking_distance_total"] if op else None,
        "decaying_pages": de["decaying"] if de else None,
        "cannibalised_queries": ca["queries_with_competing_pages"] if ca else None,
        "failed_parts": errors,
    }
    return summary
