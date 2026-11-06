"""search-console-mcp: MakashibK's read-only Google Search Console MCP server for Claude."""

from __future__ import annotations

import functools
import logging
import logging.handlers
import sys
from collections import Counter
from typing import Annotated, Literal
from urllib.parse import urlsplit

import requests
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import (__version__, api, auth, crawllogs, crux, db, indexing, inspector, livecheck, performance, playbook,
               report, robots, sitechecks, sitemap_fetch)
from .util import data_dir, out, parse_iso, pt_today

log = logging.getLogger("search_console_mcp")

INSTRUCTIONS = """Google Search Console tools (local; official Google APIs, live site checks and your own server logs).
Accuracy rules: report numbers exactly as returned; never attribute a change to a cause (algorithm update, competitor,
seasonality) unless the returned data shows it; say what the data can't tell. Use verify_metric before presenting a key number.
- Properties: pass 'sc-domain:example.com', a URL-prefix property, or just 'example.com'.
- Dates are Pacific Time. Comparisons end on the latest complete day; fresher partial days are excluded unless dates are given.
- Page indexing (no Google API): queue_from_sitemaps / queue_from_performance / queue_urls -> run_inspections or
  inspection_job (2,000 per property per day) -> indexing_report / list_inspected_urls / indexing_changes; diagnose_url explains one URL.
- Crawl stats (no Google API): import_access_logs (server logs) -> crawl_stats.
- Not available through any API: Request indexing, Removals, Links, Manual actions, Security issues, Achievements, Settings,
  Google's own robots.txt history. Core Web Vitals: core_web_vitals (CrUX) and pagespeed_audit. HTTPS: check_https.
- Read-only: nothing here can change Search Console (no sitemap submit/delete, no Request indexing, no property changes).
- Large tables: export_csv=true writes a CSV to Documents/search-console-mcp-exports."""

mcp = MCPServer("search-console-mcp", instructions=INSTRUCTIONS, version=__version__)

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE_LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

Site = Annotated[str, Field(description="Property: 'sc-domain:example.com', 'https://www.example.com/' or 'example.com'")]
Days = Annotated[int, Field(description="Days ending yesterday (PT); ignored if start_date is given", ge=1, le=490)]
OptDate = Annotated[str | None, Field(description="YYYY-MM-DD (Pacific Time)")]
SearchType = Annotated[Literal["web", "image", "video", "news", "discover", "googleNews"],
                       Field(description="Search Console search type; 'discover' = Discover report")]


def tool(annotations: ToolAnnotations = RO):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return out(fn(*args, **kwargs))
            except ToolError:
                raise
            except (auth.AuthError, api.WriteBlocked) as exc:
                raise ToolError(str(exc)) from exc
            except api.ApiError as exc:
                raise ToolError(f"Google API error {exc.status}: {exc.message}") from exc
            except (ValueError, KeyError) as exc:
                raise ToolError(str(exc)) from exc
            except requests.RequestException as exc:
                raise ToolError(f"Network error: {exc}") from exc
            except Exception as exc:  # last resort: surface the message instead of a generic failure
                log.exception("tool %s failed", fn.__name__)
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        return mcp.tool(annotations=annotations)(wrapper)
    return decorate


def _site(value: str) -> str:
    return api.resolve_site(value)


# ================================================================ status / properties
@tool()
def server_status() -> dict:
    """Server health: credentials, local database, inspection queues, background jobs, robots.txt watches, today's inspection quota use."""
    path = auth.credentials_path()
    with db.connect() as conn:
        queues = [dict(r) for r in conn.execute(
            "SELECT site_url, COUNT(*) AS queued, SUM(latest_id IS NOT NULL) AS inspected FROM url_queue GROUP BY site_url")]
        today = [dict(r) for r in conn.execute(
            "SELECT site_url, COUNT(*) AS inspections_today FROM inspections WHERE pt_date=? GROUP BY site_url",
            (pt_today().isoformat(),))]
        watches = [dict(r) for r in conn.execute("SELECT * FROM robots_watch")]
    return {
        "version": __version__,
        "mode": "read-only (Search Console scope: " + ", ".join(s.rsplit("/", 1)[-1] for s in auth.SCOPES)
                + "; nothing can be changed in Search Console)",
        "credentials_file": str(path), "credentials_found": path.is_file(), "credentials_type": auth.credentials_type(),
        "database": str(db.db_path()), "log_file": str(data_dir() / "server.log"),
        "inspection_daily_limit": inspector.daily_limit_default(),
        "queues": queues, "inspections_today_pt": today,
        "inspection_jobs": inspector.job_status(), "robots_watches": watches,
        "core_web_vitals_enabled": crux.enabled(),
    }


@tool()
def list_properties() -> dict:
    """All Search Console properties this Google account can access, with permission level."""
    sites = api.list_sites(force=True)
    return {"count": len(sites), "properties": sorted(sites, key=lambda s: s["siteUrl"])}


@tool()
def property_overview(site: Site, days: Days = 28) -> dict:
    """One-call overview: web + Discover performance vs previous period, sitemaps health, local indexing summary, robots.txt status."""
    site = _site(site)
    web = performance.overview(site, days=days, search_type="web")
    try:
        d_start, d_end, _ = performance.complete_range(site, None, None, days, "discover")
        discover = {"period": [d_start.isoformat(), d_end.isoformat()], **performance.totals(site, d_start, d_end, "discover")}
    except api.ApiError as exc:
        discover = {"error": exc.message}
    maps = api.list_sitemaps(site)
    idx = indexing.report(site, samples=2)
    host = site.split(":", 1)[1] if site.startswith("sc-domain:") else urlsplit(site).netloc
    rob_hist = robots.history(host, limit=1)
    return {
        "site": site,
        "web": {k: web[k] for k in ("period", "totals", "previous_totals", "change_pct", "position_change", "top_queries", "top_pages") if k in web},
        "discover_totals": discover,
        "sitemaps": {"count": len(maps),
                     "with_errors": [m["path"] for m in maps if int(m.get("errors", 0) or 0)],
                     "with_warnings": [m["path"] for m in maps if int(m.get("warnings", 0) or 0)],
                     "pending": [m["path"] for m in maps if m.get("isPending")],
                     "last_downloaded": sorted((m.get("lastDownloaded") for m in maps if m.get("lastDownloaded")), reverse=True)[:1]},
        "indexing_local": {k: idx[k] for k in ("urls_in_queue", "inspected", "indexed", "not_indexed", "not_yet_inspected")},
        "top_not_indexed_reasons": [{"reason": r["reason"], "pages": r["pages"]}
                                    for r in idx["why_pages_arent_indexed_and_indexed_reasons"] if r["indexed"] is False][:8],
        "robots_txt_last_check": rob_hist["checks"][0] if rob_hist["checks"] else "never checked (run robots_txt_check)",
    }


# ================================================================ performance
@tool()
def performance_query(
    site: Site,
    dimensions: Annotated[list[Literal["date", "hour", "query", "page", "country", "device", "searchAppearance"]] | None,
                          Field(description="Group-by dimensions. 'hour' gives hourly data for recent days. Empty = totals only")] = None,
    start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
    search_type: SearchType = "web",
    filters: Annotated[list[dict] | None, Field(description="AND filters: [{dimension: query|page|country|device|searchAppearance, "
                                                           "operator: equals|notEquals|contains|notContains|includingRegex|excludingRegex, expression}]")] = None,
    aggregation: Annotated[Literal["auto", "byPage", "byProperty"], Field(description="How results are aggregated")] = "auto",
    data_state: Annotated[Literal["all", "final"], Field(description="'all' matches the UI (includes fresh data); 'final' = finalised only")] = "all",
    max_rows: Annotated[int, Field(ge=1, le=100000, description="Rows to fetch (pages of 25,000)")] = 1000,
    return_rows: Annotated[int, Field(ge=0, le=5000, description="Rows to return in chat; the rest only in the CSV")] = 200,
    sort_by: Annotated[Literal["clicks", "impressions", "ctr", "position", "key"], Field(description="Sort rows")] = "clicks",
    export_csv: Annotated[bool, Field(description="Write all fetched rows to a CSV file")] = False,
) -> dict:
    """Search performance (clicks, impressions, CTR, position) for any dimension combination, search type, date range and filters."""
    site = _site(site)
    rows, meta = performance.query(site, start_date, end_date, days, dimensions, search_type, filters, aggregation, data_state, max_rows)
    if sort_by == "key":
        rows.sort(key=lambda r: [str(r.get(d)) for d in meta["dimensions"]])
    else:
        rows.sort(key=lambda r: (r.get(sort_by) is None, -(r.get(sort_by) or 0) if sort_by != "position" else (r.get(sort_by) or 0)))
    result = {**meta, "returned": min(return_rows, len(rows)), "data": rows[:return_rows]}
    if export_csv:
        result["csv_file"] = performance.export(rows, f"performance-{site}-{'-'.join(meta['dimensions']) or 'totals'}")
    return result


@tool()
def performance_overview(site: Site, start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
                         search_type: SearchType = "web") -> dict:
    """Totals vs the previous equal-length period, daily series, top queries and pages. Use search_type='discover' for Discover."""
    return performance.overview(_site(site), start_date, end_date, days, search_type)


@tool()
def compare_periods(
    site: Site,
    dimension: Annotated[Literal["query", "page", "country", "device", "searchAppearance"], Field(description="What to compare")] = "query",
    start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
    previous_start: OptDate = None, previous_end: OptDate = None,
    search_type: SearchType = "web",
    filters: Annotated[list[dict] | None, Field(description="Same format as performance_query filters")] = None,
    top_n: Annotated[int, Field(ge=1, le=500)] = 25,
    min_impressions: Annotated[int, Field(ge=0)] = 10,
) -> dict:
    """Winners and losers between two periods (default: previous equal-length period), with new and lost queries/pages."""
    return performance.compare(_site(site), dimension, start_date, end_date, days, previous_start, previous_end,
                               search_type, filters, top_n, min_impressions)


@tool()
def find_opportunities(site: Site, start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
                       min_impressions: Annotated[int, Field(ge=1)] = 100,
                       position_from: float = 4.0, position_to: float = 20.0,
                       top_n: Annotated[int, Field(ge=1, le=500)] = 50,
                       search_type: Annotated[Literal["web", "image", "video", "news"], Field(description="'image' = image-search quick wins")] = "web") -> dict:
    """Quick wins: striking-distance query/page pairs, top positions with low CTR (vs this site's own CTR by position), and content gaps (demand, but ranking below 20)."""
    return performance.opportunities(_site(site), start_date, end_date, days, min_impressions, position_from, position_to,
                                     top_n, search_type)


@tool()
def find_cannibalization(site: Site, start_date: OptDate = None, end_date: OptDate = None, days: Days = 90,
                         min_impressions: Annotated[int, Field(ge=1)] = 50,
                         min_share: Annotated[float, Field(gt=0, lt=1, description="Each competing page's minimum share of the query's impressions")] = 0.1,
                         top_n: Annotated[int, Field(ge=1, le=500)] = 50,
                         search_type: Annotated[Literal["web", "image", "video", "news"], Field(description="Search type")] = "web") -> dict:
    """Queries where 2+ of your URLs split impressions. URL variants (www/non-www, http/https, slash, parameters) are reported separately from genuinely competing pages."""
    return performance.cannibalization(_site(site), start_date, end_date, days, min_impressions, min_share, top_n, search_type)


@tool()
def traffic_drops(site: Site,
                  dimension: Annotated[Literal["page", "query", "country", "device"], Field(description="What lost traffic")] = "page",
                  start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
                  search_type: Annotated[Literal["web", "image", "video", "news", "discover", "googleNews"], Field()] = "web",
                  min_clicks_prev: Annotated[int, Field(ge=1)] = 10,
                  position_threshold: Annotated[float, Field(gt=0, description="Position worsening that counts as ranking loss")] = 2.0,
                  top_n: Annotated[int, Field(ge=1, le=500)] = 25,
                  explain_top: Annotated[int, Field(ge=0, le=20, description="For the N biggest drops, list the queries (or pages) that lost most")] = 5) -> dict:
    """Pages/queries that lost clicks vs the previous period, each diagnosed: ranking loss, CTR drop, demand drop or disappeared, with the click loss split exactly into fewer impressions vs lower CTR."""
    return performance.traffic_drops(_site(site), dimension, start_date, end_date, days, search_type, min_clicks_prev,
                                     position_threshold, top_n, explain_top)


@tool()
def content_decay(site: Site,
                  dimension: Annotated[Literal["page", "query"], Field()] = "page",
                  periods: Annotated[int, Field(ge=2, le=6, description="Consecutive periods that must each decline")] = 3,
                  period_days: Annotated[int, Field(ge=7, le=120)] = 30,
                  search_type: Annotated[Literal["web", "image", "video", "news", "discover", "googleNews"], Field()] = "web",
                  min_clicks: Annotated[int, Field(ge=1, description="Clicks needed in the oldest period")] = 20,
                  min_decline_pct: Annotated[float, Field(ge=0, le=100)] = 20.0,
                  top_n: Annotated[int, Field(ge=1, le=500)] = 50) -> dict:
    """Pages (or queries) whose clicks fell in every one of N consecutive periods: slow decay, not a one-off dip."""
    return performance.content_decay(_site(site), dimension, periods, period_days, search_type, min_clicks, min_decline_pct, top_n)


@tool()
def check_alerts(site: Site, days: Annotated[int, Field(ge=1, le=90, description="Compare the last N complete days with the N before")] = 7,
                 search_type: Annotated[Literal["web", "image", "video", "news", "discover", "googleNews"], Field()] = "web",
                 min_impressions: Annotated[int, Field(ge=1)] = 100, min_clicks: Annotated[int, Field(ge=1)] = 20,
                 position_drop: Annotated[float, Field(gt=0)] = 5.0, ctr_drop_pct: Annotated[float, Field(gt=0, le=100)] = 50.0,
                 click_drop_pct: Annotated[float, Field(gt=0, le=100)] = 30.0, site_drop_pct: Annotated[float, Field(gt=0, le=100)] = 20.0) -> dict:
    """Severity-rated alerts: site-wide click drop, query position drops (incl. falling off page one), CTR collapses at the same position, page click losses, pages that vanished."""
    return performance.alerts(_site(site), days, search_type, min_impressions, min_clicks, position_drop, ctr_drop_pct,
                              click_drop_pct, site_drop_pct)


@tool()
def section_performance(site: Site,
                        level: Annotated[int, Field(ge=1, le=5, description="Folder depth: 1 = /blog/, 2 = /blog/seo/")] = 1,
                        prefixes: Annotated[list[str] | None, Field(description="Custom sections by path prefix, e.g. ['/blog/', '/women/']")] = None,
                        start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
                        search_type: SearchType = "web", compare_previous: bool = True,
                        top_n: Annotated[int, Field(ge=1, le=500)] = 50) -> dict:
    """Performance per site section (URL folder or custom prefixes) with share of clicks and change vs the previous period. Domain properties are split per host."""
    return performance.sections(_site(site), level, prefixes, start_date, end_date, days, search_type, compare_previous, top_n)


@tool()
def query_segments(site: Site, start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
                   search_type: Annotated[Literal["web", "image", "video", "news"], Field()] = "web",
                   brand_terms: Annotated[list[str] | None, Field(description="Brand names/spellings; default = the property's brand_terms note")] = None,
                   top_n: Annotated[int, Field(ge=1, le=50)] = 10) -> dict:
    """Queries split into branded vs non-branded, questions (EN/NL/SV/PT/FR/DE), query length, results page, and chat-style fragments."""
    site = _site(site)
    if brand_terms is None:
        with db.connect() as conn:
            rows = conn.execute("SELECT text FROM notes WHERE site_url=? AND kind='brand_terms'", (site,)).fetchall()
        brand_terms = [t.strip() for r in rows for t in r["text"].split(",") if t.strip()] or None
    return performance.segments(site, start_date, end_date, days, search_type, brand_terms, top_n)


@tool()
def multi_site_dashboard(properties: Annotated[list[str] | None, Field(description="Default: every accessible property")] = None,
                         days: Annotated[int, Field(ge=7, le=90)] = 28,
                         drop_pct: Annotated[float, Field(gt=0, le=100, description="Change that flags a property as dropping/growing")] = 20.0) -> dict:
    """Health check across many properties at once: clicks, impressions, CTR, position vs previous period, flagged dropping/growing."""
    sites = api.list_sites(force=True)
    if properties:
        wanted = {_site(p) for p in properties}
        sites = [s for s in sites if s["siteUrl"] in wanted]
    if len(sites) > 60:
        raise ValueError(f"{len(sites)} properties; pass up to 60 in 'properties'.")
    return performance.dashboard(sites, days, drop_pct)


@tool(WRITE_LOCAL)
def weekly_report(site: Site, days: Annotated[int, Field(ge=7, le=90)] = 28,
                  top_n: Annotated[int, Field(ge=3, le=50)] = 10) -> dict:
    """Full report in one call (performance, alerts, drops, quick wins, content gaps, decay, cannibalisation, sitemaps, indexing, robots.txt), saved as a Markdown file."""
    return report.build(_site(site), days, top_n)


@tool()
def verify_metric(site: Site, metric: Literal["clicks", "impressions", "ctr", "position"], expected: float,
                  start_date: OptDate = None, end_date: OptDate = None, days: Days = 28,
                  search_type: SearchType = "web",
                  page: Annotated[str | None, Field(description="Exact page URL")] = None,
                  query: Annotated[str | None, Field(description="Exact query")] = None,
                  country: Annotated[str | None, Field(description="ISO-3166 alpha-3, e.g. nld")] = None,
                  device: Annotated[Literal["DESKTOP", "MOBILE", "TABLET"] | None, Field()] = None,
                  tolerance_pct: Annotated[float, Field(ge=0, le=50)] = 1.0) -> dict:
    """Re-query Google to confirm a number before presenting it (e.g. 'this page got 500 clicks')."""
    return performance.verify(_site(site), metric, expected, start_date, end_date, days, search_type, page, query,
                              country, device, tolerance_pct)


# ================================================================ sitemaps
@tool()
def list_sitemaps(site: Site, sitemap_index: Annotated[str | None, Field(description="Only children of this sitemap index")] = None) -> dict:
    """Sitemaps submitted for the property: last submitted/downloaded, pending, errors, warnings, submitted URL counts by type."""
    site = _site(site)
    maps = api.list_sitemaps(site, sitemap_index)
    return {"site": site, "count": len(maps), "sitemaps": maps}


@tool()
def get_sitemap(site: Site, sitemap_url: Annotated[str, Field(description="Full sitemap URL")]) -> dict:
    """Details of one submitted sitemap."""
    site = _site(site)
    return {"site": site, **api.get_sitemap(site, sitemap_url)}


# ================================================================ URL inspection / indexing
@tool(WRITE_LOCAL)
def inspect_url(site: Site, url: Annotated[str, Field(description="Full page URL")],
                language: Annotated[str, Field(description="Language for issue messages")] = "en-US") -> dict:
    """Live URL Inspection result from Google (index status, coverage reason, canonicals, last crawl, robots, sitemaps, referring URLs, rich results). Stored locally."""
    site = _site(site)
    parsed = inspector.inspect_one(site, url, language)
    return {"site": site, "url": url, **parsed, "reason_guide": playbook.lookup(parsed.get("coverage_state")),
            "quota_used_today": inspector.used_today(site)}


@tool(WRITE_LOCAL)
def diagnose_url(site: Site, url: Annotated[str, Field(description="Full page URL")],
                 use_cached_inspection: Annotated[bool, Field(description="Use the last stored inspection instead of a new one")] = False,
                 user_agent: Annotated[Literal["default", "googlebot_smartphone"],
                                       Field(description="UA for the live fetch. A Googlebot UA from your IP may be blocked by a WAF")] = "default") -> dict:
    """Why is this URL (not) indexed? Combines Google's inspection with a live fetch: redirect chain, status, noindex, canonicals, robots.txt rule, thin content."""
    site = _site(site)
    inspection = None
    if use_cached_inspection:
        rows = indexing.list_urls(site, url_contains=url, limit=50)["urls"]
        inspection = next((r for r in rows if r.get("url") == url), None)
    if inspection is None:
        inspection = inspector.inspect_one(site, url)
    live = livecheck.analyse(url, user_agent)
    return {"site": site, "url": url, "google": inspection, "live": live,
            "findings": livecheck.findings(live, inspection),
            "reason_guide": playbook.lookup(inspection.get("coverage_state"))}


@tool(WRITE_LOCAL)
def queue_urls(site: Site, urls: Annotated[list[str], Field(description="Full URLs inside the property")],
               label: Annotated[str, Field(description="Source label for filtering later")] = "manual") -> dict:
    """Add URLs to this property's local inspection queue."""
    site = _site(site)
    return {"site": site, **inspector.queue_urls(site, urls, label)}


@tool(WRITE_LOCAL)
def queue_from_sitemaps(site: Site,
                        sitemap_url: Annotated[str | None, Field(description="One sitemap or index; default = every sitemap submitted in Search Console")] = None,
                        max_urls: Annotated[int, Field(ge=1, le=500000)] = 50000) -> dict:
    """Download sitemaps (indexes, .gz) and queue every page URL for inspection, remembering which sitemap each URL came from."""
    site = _site(site)
    starts = [sitemap_url] if sitemap_url else [m["path"] for m in api.list_sitemaps(site)]
    if not starts:
        raise ValueError("No sitemaps submitted for this property. Pass sitemap_url.")
    summary, errors, origin = {}, [], {}
    collected: list[str] = []
    for start in starts:
        res = sitemap_fetch.collect_urls(start, max_urls=max_urls - len(collected))
        for url, child in res["urls"]:
            if url not in origin:
                origin[url] = child
                collected.append(url)
        summary[start] = {"sitemaps_read": res["sitemaps_read"], "urls": len(res["urls"]), "truncated": res["truncated"]}
        errors.extend(res["errors"])
        if len(collected) >= max_urls:
            break
    queued = inspector.queue_urls(site, collected, "sitemap", detail_for=origin.get)
    return {"site": site, "sitemaps": summary, "unique_urls": len(collected), **queued, "fetch_errors": errors[:20],
            "next": "run_inspections for a batch now, or inspection_job action=start to work through the queue in the background."}


@tool(WRITE_LOCAL)
def queue_from_performance(site: Site, days: Days = 90, min_impressions: Annotated[int, Field(ge=0)] = 1,
                           search_type: SearchType = "web",
                           max_urls: Annotated[int, Field(ge=1, le=100000)] = 25000) -> dict:
    """Queue every page that got impressions in Search (useful to inspect the pages Google actually shows)."""
    site = _site(site)
    rows, meta = performance.query(site, None, None, days, ["page"], search_type, max_rows=max_urls)
    urls = [r["page"] for r in rows if r["impressions"] >= min_impressions]
    return {"site": site, "pages_found": len(urls), **inspector.queue_urls(site, urls, "performance")}


@tool(WRITE_LOCAL)
def run_inspections(site: Site,
                    max_urls: Annotated[int, Field(ge=1, le=500)] = 100,
                    time_budget_seconds: Annotated[int, Field(ge=5, le=240, description="Stop after this long; call again to continue")] = 45,
                    stale_days: Annotated[int, Field(ge=0, le=365, description="Re-inspect URLs whose last inspection is older than this")] = 7,
                    concurrency: Annotated[int, Field(ge=1, le=10)] = 5,
                    urls: Annotated[list[str] | None, Field(description="Inspect exactly these URLs instead of the queue")] = None,
                    top_pages: Annotated[int | None, Field(ge=1, le=500, description="Inspect the top N pages by clicks (last 28 days)")] = None,
                    recrawled_since: Annotated[str | None, Field(description="YYYY-MM-DD: also report whether Google crawled each URL after this date")] = None) -> dict:
    """Inspect URLs now: the queue (never-inspected first, then stale), an explicit list, or your top pages. Respects the 2,000/day and 600/minute per-property quotas."""
    site = _site(site)
    if top_pages:
        rows, _ = performance.query(site, None, None, 28, ["page"], "web", max_rows=top_pages)
        urls = [r["page"] for r in sorted(rows, key=lambda r: -r["clicks"])[:top_pages]]
        inspector.queue_urls(site, urls, "performance")
    elif urls:
        inspector.queue_urls(site, urls, "manual")
    result = inspector.run_batch(site, max_urls, float(time_budget_seconds), stale_days, concurrency, urls=urls or None)
    if recrawled_since and urls:
        since = parse_iso(recrawled_since if "T" in recrawled_since else recrawled_since[:10] + "T00:00:00+00:00")
        if since is None:
            raise ValueError("recrawled_since must be YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC)")
        latest = indexing.latest_for(site, urls)
        checks = []
        for u in urls:
            row = latest.get(u, {})
            crawled = parse_iso(row.get("last_crawl_time"))
            checks.append({"url": u, "last_crawl_time": row.get("last_crawl_time"), "coverage_state": row.get("coverage_state"),
                           "recrawled_since": None if crawled is None else crawled >= since})
        result["recrawl_check"] = {"since_utc": since.isoformat(), "recrawled": sum(1 for c in checks if c["recrawled_since"]),
                                   "not_yet": sum(1 for c in checks if c["recrawled_since"] is False),
                                   "unknown": sum(1 for c in checks if c["recrawled_since"] is None), "urls": checks,
                                   "note": "Edited today? Pass the edit time (YYYY-MM-DDTHH:MM, UTC), not only the date."}
    elif recrawled_since:
        result["recrawl_check"] = "Pass urls (or top_pages) to check recrawls."
    return result


@tool(WRITE_LOCAL)
def inspection_job(site: Site, action: Annotated[Literal["start", "stop", "status"], Field(description="Background job control")] = "status",
                   daily_limit: Annotated[int, Field(ge=1, le=2000, description="Max inspections per Pacific-Time day for this property")] = 2000,
                   stale_days: Annotated[int, Field(ge=1, le=365, description="Re-inspect after this many days")] = 14) -> dict:
    """Background inspection while Claude Desktop is open: works through the queue within the daily quota and resumes after restarts."""
    site = _site(site)
    if action == "start":
        return inspector.set_job(site, True, daily_limit, stale_days)
    if action == "stop":
        return inspector.set_job(site, False, daily_limit, stale_days)
    return inspector.job_status(site)


@tool()
def indexing_report(site: Site,
                    sitemap: Annotated[str | None, Field(description="Only URLs from this sitemap (by our sitemap download or Google's sitemap list)")] = None,
                    url_contains: Annotated[str | None, Field(description="Substring filter on URL")] = None,
                    url_regex: Annotated[str | None, Field(description="Python regex on URL")] = None,
                    source: Annotated[Literal["sitemap", "performance", "manual", "inspected"] | None, Field(description="Queue source")] = None,
                    samples: Annotated[int, Field(ge=0, le=50)] = 5) -> dict:
    """Page-indexing report from local inspections: indexed vs not, every reason with source, samples, checks and fixes; canonicals, fetch/robots states, crawl age, rich results."""
    site = _site(site)
    return indexing.report(site, sitemap, url_contains, url_regex, source, samples)


@tool()
def list_inspected_urls(
    site: Site,
    coverage_state: Annotated[str | None, Field(description="Exact reason, e.g. 'Crawled - currently not indexed'")] = None,
    indexed: Annotated[bool | None, Field(description="True = indexed only, False = not indexed only")] = None,
    url_contains: str | None = None, url_regex: str | None = None,
    sitemap: Annotated[str | None, Field(description="Only URLs from this sitemap")] = None,
    rich_type: Annotated[str | None, Field(description="e.g. 'Product snippets', 'Merchant listings', 'Breadcrumbs', 'Review snippets'")] = None,
    has_rich_issues: bool | None = None,
    crawled_before_days: Annotated[int | None, Field(description="Only URLs last crawled more than N days ago")] = None,
    sort_by: Literal["last_crawl_time", "inspected_at", "url", "coverage_state", "rich_issue_count"] = "last_crawl_time",
    descending: bool = False,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
    export_csv: bool = False,
    with_performance_days: Annotated[int | None, Field(description="Add clicks/impressions per URL for the last N days")] = None,
) -> dict:
    """URL-level drill-down of stored inspections with filtering, sorting, paging and CSV export."""
    site = _site(site)
    result = indexing.list_urls(site, coverage_state, indexed, url_contains, url_regex, sitemap, rich_type, has_rich_issues,
                                crawled_before_days, sort_by, descending, limit, offset, export_csv)
    if with_performance_days and result["urls"]:
        rows, _ = performance.query(site, None, None, with_performance_days, ["page"], "web", max_rows=25000)
        perf = {r["page"]: r for r in rows}
        for item in result["urls"]:
            p = perf.get(item["url"])
            item["clicks"] = p["clicks"] if p else 0
            item["impressions"] = p["impressions"] if p else 0
            item["avg_position"] = p["position"] if p else None
        result["performance_days"] = with_performance_days
    return result


@tool()
def indexing_changes(site: Site, days: Annotated[int, Field(ge=1, le=365)] = 30,
                     limit: Annotated[int, Field(ge=1, le=1000)] = 200) -> dict:
    """URLs whose coverage reason changed between their last two inspections (dropped out of the index, newly indexed, ...), plus per-day inspection trend."""
    site = _site(site)
    return {**indexing.changes(site, days, limit), "inspection_trend": indexing.daily_trend(site)}


@tool()
def indexing_fix_guide(reason: Annotated[str | None, Field(description="A coverage reason; omit for all")] = None) -> dict:
    """What each Page indexing reason means, whether it's a Website or Google-systems issue, what to check and how to fix it."""
    return playbook.guide(reason)


# ================================================================ robots.txt
@tool(WRITE_LOCAL)
def robots_txt_check(host_or_url: Annotated[str, Field(description="Host, URL or sc-domain property, e.g. www.example.com")]) -> dict:
    """Fetch robots.txt like Googlebot: status and Google's interpretation, size vs the 500 KiB limit, groups, Googlebot rules, sitemaps, syntax warnings, and a diff if it changed since the last check. Saved to history."""
    return robots.check(host_or_url)


@tool(WRITE_LOCAL)
def robots_txt_check_property(site: Site, days: Days = 28,
                              max_hosts: Annotated[int, Field(ge=1, le=50)] = 20) -> dict:
    """Check robots.txt on every host that had Search traffic in the property (all subdomains of a Domain property)."""
    site = _site(site)
    rows, _ = performance.query(site, None, None, days, ["page"], "web", max_rows=25000)
    hosts = Counter(urlsplit(r["page"]).netloc.lower() for r in rows)
    if not hosts:
        hosts = Counter({site.split(":", 1)[1] if site.startswith("sc-domain:") else urlsplit(site).netloc: 0})
    results = []
    for host, clicks in hosts.most_common(max_hosts):
        rep = robots.check(host)
        results.append({"host": host, "pages_with_impressions": clicks, "status": rep["status"],
                        "interpretation": rep["google_interpretation"], "size_bytes": rep["size_bytes"],
                        "changed_since_last_check": rep["changed_since_last_check"],
                        "warnings": len(rep["warnings"]), "sitemaps": rep["sitemaps"], "check_id": rep["check_id"]})
    return {"site": site, "hosts_checked": len(results), "hosts": results}


@tool()
def robots_txt_history(host_or_url: str, limit: Annotated[int, Field(ge=1, le=500)] = 50) -> dict:
    """Every stored robots.txt check for a host (date, status, size, hash, response time) and each distinct version with first/last seen."""
    return robots.history(host_or_url, limit)


@tool()
def robots_txt_diff(from_check_id: int, to_check_id: int) -> dict:
    """Unified diff between two stored robots.txt checks (ids from robots_txt_history)."""
    return robots.diff_checks(from_check_id, to_check_id)


@tool()
def robots_txt_test(url: Annotated[str, Field(description="Full URL to test")],
                    user_agent: Annotated[str, Field(description="Googlebot, Googlebot-Image, Googlebot-News, Googlebot-Video, Storebot-Google, AdsBot-Google, Google-Extended, or any token")] = "Googlebot",
                    check_id: Annotated[int | None, Field(description="Test against a stored version instead of the live file")] = None,
                    show_content: bool = False) -> dict:
    """Is this URL allowed for this crawler? Returns the deciding rule and line, using Google's matching rules."""
    if check_id is not None:
        text, row = robots.content_for(check_id, None)
        source = {"check_id": check_id, "checked_at": row.get("checked_at")}
    else:
        host, robots_url = robots.robots_url_for(f"{urlsplit(url).scheme}://{urlsplit(url).netloc}")
        fetched = robots.fetch(robots_url)
        if not (fetched["status"] and 200 <= fetched["status"] < 300):
            return {"url": url, "robots_url": robots_url, "status": fetched["status"],
                    "result": robots.interpretation(fetched["status"], fetched["error"])}
        text = robots.decode(fetched["content"])
        source = {"robots_url": robots_url, "status": fetched["status"], "live": True}
    parsed = robots.parse(text)
    rules, group = robots.select_rules(parsed, user_agent)
    verdict = robots.evaluate(rules, url)
    result = {"url": url, "user_agent": user_agent, "obeys_group": group, **verdict, **source}
    if show_content:
        result["content"] = text[:20000]
    return result


@tool(WRITE_LOCAL)
def robots_txt_watch(action: Annotated[Literal["add", "remove", "list"], Field(description="Watch list control")] = "list",
                     host_or_url: str | None = None,
                     interval_hours: Annotated[int, Field(ge=1, le=168)] = 24) -> dict:
    """Keep checking robots.txt for a host in the background (while Claude Desktop is open) to build version history."""
    with db.connect() as conn:
        if action in ("add", "remove"):
            if not host_or_url:
                raise ValueError("host_or_url is required")
            host, _ = robots.robots_url_for(host_or_url)
            if action == "add":
                conn.execute("INSERT INTO robots_watch (host, interval_hours, added_at) VALUES (?,?,datetime('now')) "
                             "ON CONFLICT(host) DO UPDATE SET interval_hours=excluded.interval_hours", (host, interval_hours))
            else:
                conn.execute("DELETE FROM robots_watch WHERE host=?", (host,))
        rows = [dict(r) for r in conn.execute("SELECT * FROM robots_watch ORDER BY host")]
    if action == "add":
        inspector.ensure_worker()
    return {"watching": rows}


# ================================================================ site checks
@tool()
def check_redirects(urls: Annotated[list[str] | None, Field(description="URLs to trace (max 500)")] = None,
                    pairs: Annotated[list[dict] | None, Field(description="Migration map [{from, to}]: verifies each old URL ends at its new URL")] = None,
                    concurrency: Annotated[int, Field(ge=1, le=16)] = 8) -> dict:
    """Trace redirect chains in bulk: every hop, final status, chains, loops, temporary redirects, https->http; validates migration redirect maps."""
    return sitechecks.check_redirects(urls, pairs, concurrency)


@tool()
def check_https(host_or_url: Annotated[str, Field(description="Host or URL")],
                urls: Annotated[list[str] | None, Field(description="Optional URLs to confirm they end on https (max 100)")] = None) -> dict:
    """HTTPS health (Search Console's HTTPS report has no API): certificate validity/expiry/names, http->https redirect, HSTS, mixed content."""
    return sitechecks.check_https(host_or_url, urls)


@tool()
def check_dns(domain: Annotated[str, Field(description="Domain or sc-domain property")],
              include_www: bool = True) -> dict:
    """DNS records (A, AAAA, CNAME, MX, NS, TXT, CAA) plus Google site-verification tokens found in DNS TXT and on the home page."""
    return sitechecks.check_dns(domain, include_www)


@tool()
def image_audit(urls: Annotated[list[str], Field(description="Pages to audit (max 10)")],
                check_files: Annotated[bool, Field(description="Fetch image headers for size/broken checks (up to 30 per page)")] = True,
                max_images: Annotated[int, Field(ge=1, le=300)] = 60,
                large_kb: Annotated[int, Field(ge=50, le=5000)] = 300) -> dict:
    """Image SEO audit of pages: alt text, file names, width/height, lazy loading near the top, formats, file size, broken images, og:image and structured-data images."""
    return sitechecks.image_audit(urls, check_files, max_images, large_kb)


@tool()
def pagespeed_audit(url: str, strategy: Literal["MOBILE", "DESKTOP"] = "MOBILE",
                    categories: Annotated[list[Literal["PERFORMANCE", "SEO", "ACCESSIBILITY", "BEST_PRACTICES"]] | None,
                                          Field(description="Lighthouse categories")] = None) -> dict:
    """PageSpeed Insights: Lighthouse scores, lab metrics, top opportunities by savings, failing SEO/other audits, and real-user field data. Uses PAGESPEED_API_KEY or CRUX_API_KEY if set."""
    return sitechecks.pagespeed(url, strategy, categories)


# ================================================================ notes
@tool(WRITE_LOCAL)
def property_notes(site: Site, action: Literal["list", "add", "delete"] = "list",
                   kind: Annotated[Literal["note", "brand_terms", "migration", "decision", "change"],
                                   Field(description="brand_terms = comma-separated brand names used by query_segments")] = "note",
                   text: str | None = None, note_id: int | None = None) -> dict:
    """Context saved with a property for future chats: brand terms, migration dates, decisions, site changes."""
    site = _site(site)
    with db.connect() as conn:
        if action == "add":
            if not text:
                raise ValueError("text is required")
            conn.execute("INSERT INTO notes (site_url, kind, text, created_at) VALUES (?,?,?,datetime('now'))", (site, kind, text))
        elif action == "delete":
            if note_id is None:
                raise ValueError("note_id is required")
            conn.execute("DELETE FROM notes WHERE id=? AND site_url=?", (note_id, site))
        rows = [dict(r) for r in conn.execute("SELECT id, kind, text, created_at FROM notes WHERE site_url=? ORDER BY id", (site,))]
    return {"site": site, "notes": rows}


# ================================================================ crawl stats from server logs
@tool(WRITE_LOCAL)
def import_access_logs(paths: Annotated[list[str] | None, Field(description="Log files or patterns on this PC, e.g. C:\\logs\\access.log*; .gz is fine. Omit to see import progress")] = None,
                       host: Annotated[str | None, Field(description="Host the log belongs to (unless the log has a virtual-host field)")] = None,
                       response_time_unit: Annotated[Literal["auto", "seconds", "milliseconds", "microseconds", "none"],
                                                     Field(description="Unit of a trailing response-time field; 'auto' only reads decimal seconds (nginx $request_time)")] = "auto",
                       verify: Annotated[bool, Field(description="Verify Google IPs against Google's published ranges")] = True) -> dict:
    """Import Apache/Nginx/Varnish access logs to rebuild Crawl stats. Runs in the background; already-imported files are skipped."""
    if not paths:
        return crawllogs.import_status()
    return crawllogs.start_import(paths, host, response_time_unit, verify)


@tool()
def crawl_stats(host: Annotated[str | None, Field(description="Host, or omit/'all' for every imported host")] = None,
                start_date: OptDate = None, end_date: OptDate = None,
                verification: Annotated[Literal["exclude_fake", "verified_only", "all", "fake_only"],
                                        Field(description="Which hits count; fake = Google user agent from a non-Google IP")] = "exclude_fake",
                bot_types: Annotated[list[str] | None, Field(description="e.g. ['Smartphone','Desktop','Image','AdsBot','StoreBot']")] = None,
                examples: Annotated[int, Field(ge=0, le=50, description="Example URLs per response type")] = 5,
                top_urls: Annotated[int, Field(ge=0, le=1000, description="Most-crawled URLs")] = 0,
                response: Annotated[str | None, Field(description="Limit top_urls to one response, e.g. 'Not found (404)'")] = None) -> dict:
    """Crawl stats from your logs: requests, download size, response time per day; breakdowns by response, file type, Googlebot type, purpose and host; robots.txt fetches; example URLs."""
    return crawllogs.stats(host, start_date, end_date, verification, bot_types, examples, top_urls, response)


# ================================================================ Core Web Vitals (CrUX)
@tool()
def core_web_vitals(target: Annotated[str, Field(description="Origin (https://www.example.com) or full page URL")],
                    form_factor: Annotated[Literal["PHONE", "DESKTOP", "TABLET", "ALL"], Field(description="Device class")] = "PHONE",
                    history: Annotated[bool, Field(description="Weekly history instead of the latest 28-day window")] = False) -> dict:
    """Core Web Vitals field data (LCP, INP, CLS, FCP, TTFB at p75) from the Chrome UX Report, the data behind Search Console's CWV report. Needs CRUX_API_KEY."""
    if "://" not in target:
        target = "https://" + target
    return crux.query(target, form_factor, history)


# ================================================================ entry point
def _configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stderr = logging.StreamHandler(sys.stderr)  # stdout is reserved for the MCP protocol
    stderr.setLevel(logging.WARNING)
    root.addHandler(stderr)
    try:
        handler = logging.handlers.RotatingFileHandler(data_dir() / "server.log", maxBytes=1_000_000, backupCount=3,
                                                       encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:
        pass


def main() -> None:
    _configure_logging()
    log.info("search-console-mcp %s starting", __version__)
    inspector.start_worker_if_needed()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
