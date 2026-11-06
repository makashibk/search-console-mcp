"""Offline tests: a fake Search Console API and a local HTTP server stand in for Google and the website."""

import gzip
import json
import os
import time

import pytest

from conftest import Site, call


# ------------------------------------------------------------------ robots.txt rules (Google's documented examples)
def test_robots_precedence_examples():
    from search_console_mcp import robots
    cases = [
        ("allow: /p\ndisallow: /", "https://example.com/page", True),
        ("allow: /folder\ndisallow: /folder", "http://example.com/folder/page", True),
        ("allow: /page\ndisallow: /*.htm", "https://example.com/page.htm", False),
        ("allow: /page\ndisallow: /*.ph", "https://example.com/page.php5", True),  # equal length: allow wins
        ("allow: /$\ndisallow: /", "http://example.com/", True),
        ("allow: /$\ndisallow: /", "http://example.com/page.htm", False),
        ("disallow: /*.php$", "https://example.com/filename.php", False),
        ("disallow: /*.php$", "https://example.com/filename.php?parameters", True),
        ("disallow: /*?", "https://example.com/c?dir=asc", False),
        ("disallow: /fish*.php", "https://example.com/fishheads/catfish.php?p", False),
        ("disallow: /fish*.php", "https://example.com/Fish.PHP", True),
        ("disallow:", "https://example.com/anything", True),
    ]
    for rules, url, expected in cases:
        parsed = robots.parse("user-agent: *\n" + rules)
        selected, _ = robots.select_rules(parsed, "Googlebot")
        assert robots.evaluate(selected, url)["allowed"] is expected, (rules, url)


def test_robots_group_selection_and_warnings():
    from search_console_mcp import robots
    text = ("disallow: /early\nuser-agent: googlebot-news\ndisallow: /news-only\n\nuser-agent: *\ndisallow: /star\n\n"
            "user-agent: googlebot\nuser-agent: storebot-google\ndisallow: /gb\ncrawl-delay: 5\nnoindex: /x\n"
            "sitemap: https://example.com/sitemap.xml\nsitemap: /relative.xml\nfoo bar\n")
    parsed = robots.parse(text)
    assert robots.select_rules(parsed, "Googlebot-News")[1] == "googlebot-news"
    assert robots.select_rules(parsed, "Googlebot")[1] == "googlebot"
    assert robots.select_rules(parsed, "Googlebot-Image")[1] == "googlebot"  # falls back to googlebot group
    assert robots.select_rules(parsed, "Otherbot")[1] == "*"
    assert robots.select_rules(parsed, "AdsBot-Google")[0] == []  # AdsBot ignores '*'
    rules, _ = robots.select_rules(parsed, "Storebot-Google")
    assert robots.evaluate(rules, "https://example.com/gb/x")["allowed"] is False
    issues = " | ".join(w["issue"] for w in parsed["warnings"])
    for expected in ("rule before any user-agent", "'crawl-delay' is ignored by Google", "'noindex' is ignored by Google",
                     "sitemap URL must be absolute", "no ':' separator"):
        assert expected in issues
    assert robots.evaluate([], "https://example.com/robots.txt")["allowed"] is True


def test_robots_check_history_and_diff(env, web):
    from search_console_mcp import robots
    Site.routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /checkout/\n")
    first = robots.check(web)
    assert first["status"] == 200 and first["changed_since_last_check"] is None
    assert first["googlebot_rules"][0]["path"] == "/checkout/"
    second = robots.check(web)
    assert second["changed_since_last_check"] is False
    Site.routes["/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /checkout/\nDisallow: /*?dir=\n")
    third = robots.check(web)
    assert third["changed_since_last_check"] is True
    assert any(line.startswith("+Disallow: /*?dir=") for line in third["diff_vs_previous"])
    hist = robots.history(web)
    assert len(hist["checks"]) == 3 and len(hist["versions"]) == 2
    assert robots.diff_checks(first["check_id"], third["check_id"])["diff"]
    Site.routes["/robots.txt"] = (503, {}, b"down")
    down = robots.check(web)
    assert "unreachable" in down["google_interpretation"] and down["changed_since_last_check"] is True
    Site.routes.pop("/robots.txt")
    missing = robots.check(web)
    assert "4xx" in missing["google_interpretation"]


# ------------------------------------------------------------------ properties + performance
def test_resolve_site_variants(env):
    from search_console_mcp import api
    assert api.resolve_site("shop.example") == "sc-domain:shop.example"
    assert api.resolve_site("https://www.shop.example/some/page") == "sc-domain:shop.example"
    assert api.resolve_site("other.example") == "https://other.example/"
    with pytest.raises(ValueError):
        api.resolve_site("nothere.example")


def test_performance_query_pagination_filters_and_csv(env, tmp_path):
    res = call("performance_query", site="shop.example", dimensions=["query", "page"], max_rows=30000, return_rows=5,
               filters=[{"dimension": "page", "operator": "contains", "expression": "/p1"}], export_csv=True)
    assert res["rows"] == 30000 and res["returned"] == 5 and not res["truncated"]
    assert os.path.isfile(res["csv_file"])
    bodies = [c[2] for c in env.calls if c[2] and "dimensions" in c[2]]
    assert [b["startRow"] for b in bodies] == [0, 25000]
    assert bodies[0]["dimensionFilterGroups"][0]["filters"][0]["operator"] == "contains"
    hourly = call("performance_query", site="shop.example", dimensions=["hour"], days=3)
    assert hourly["data_state"] == "hourly_all"
    with pytest.raises(Exception):
        call("performance_query", site="shop.example", filters=[{"dimension": "page", "operator": "like", "expression": "x"}])


def test_overview_compare_opportunities_cannibalization(env):
    ov = call("performance_overview", site="shop.example", days=28)
    assert ov["totals"]["clicks"] == 500 and len(ov["daily"]) == 40 and "top_queries" in ov
    assert ov["period"]["end"] == "2026-10-02" and "complete" in ov["period"]["end_basis"]
    assert ov["clicks_from_visible_queries_pct"] is not None and ov["_source"]["api"].startswith("Search Console")
    disc = call("performance_overview", site="shop.example", search_type="discover")
    assert "top_queries" not in disc
    assert any(c[2] and c[2].get("type") == "discover" for c in env.calls)
    cmp = call("compare_periods", site="shop.example", dimension="page", top_n=3)
    assert cmp["period"][1] == "2026-10-02"  # aligned to the day before Google's firstIncompleteDate
    assert len(cmp["biggest_losers"]) + len(cmp["biggest_winners"]) > 0
    assert all(r["clicks_delta"] < 0 for r in cmp["biggest_losers"])
    opp = call("find_opportunities", site="shop.example", min_impressions=1000)
    assert opp["striking_distance_total"] > 0
    assert all(4 <= r["position"] <= 20 for r in opp["striking_distance"])


def test_cannibalization_controlled(env, monkeypatch):
    from search_console_mcp import performance
    rows = [
        {"query": "blue chair", "page": "https://www.shop.example/a", "clicks": 30, "impressions": 600, "ctr": 0.05, "position": 4.0},
        {"query": "blue chair", "page": "https://www.shop.example/b", "clicks": 5, "impressions": 300, "ctr": 0.016, "position": 8.0},
        {"query": "blue chair", "page": "https://www.shop.example/c", "clicks": 0, "impressions": 5, "ctr": 0.0, "position": 40.0},
        {"query": "oak table", "page": "https://www.shop.example/t", "clicks": 50, "impressions": 900, "ctr": 0.05, "position": 2.0},
        {"query": "rare", "page": "https://www.shop.example/r1", "clicks": 0, "impressions": 10, "ctr": 0.0, "position": 9.0},
        {"query": "rare", "page": "https://www.shop.example/r2", "clicks": 0, "impressions": 10, "ctr": 0.0, "position": 9.0},
    ]
    monkeypatch.setattr(performance, "query", lambda *a, **k: (rows, {"rows": len(rows)}))
    can = call("find_cannibalization", site="shop.example", min_impressions=50)
    assert can["queries_with_competing_pages"] == 1
    hit = can["competing_pages"][0]
    assert hit["query"] == "blue chair" and [p["page"][-1] for p in hit["pages"]] == ["a", "b"]


# ------------------------------------------------------------------ sitemaps
def test_list_and_get_sitemaps(env):
    path = "https://www.shop.example/sitemap.xml"
    env.sitemaps = {path: {"path": path, "isPending": False, "errors": "0", "warnings": "1"}}
    listed = call("list_sitemaps", site="shop.example")
    assert listed["count"] == 1 and listed["sitemaps"][0]["path"] == path
    got = call("get_sitemap", site="shop.example", sitemap_url=path)
    assert got["path"] == path and got["warnings"] == "1"
    assert [c[0] for c in env.calls if "/sitemaps" in c[1]] == ["GET", "GET"]  # only reads reach Google


def test_sitemap_download_index_gzip_text(web):
    from search_console_mcp import sitemap_fetch
    child1 = ("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
              + "".join(f"<url><loc>https://www.shop.example/p{i}</loc></url>" for i in range(5)) + "</urlset>").encode()
    Site.routes["/sm-products.xml.gz"] = (200, {"Content-Type": "application/gzip"}, gzip.compress(child1))
    Site.routes["/sm-pages.txt"] = (200, {}, b"https://www.shop.example/about\nhttps://www.shop.example/p1\n")
    index = (f"<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><sitemap><loc>{web}/sm-products.xml.gz</loc></sitemap>"
             f"<sitemap><loc>{web}/sm-pages.txt</loc></sitemap><sitemap><loc>{web}/missing.xml</loc></sitemap></sitemapindex>").encode()
    Site.routes["/sm-index.xml"] = (200, {}, index)
    res = sitemap_fetch.collect_urls(f"{web}/sm-index.xml")
    urls = [u for u, _ in res["urls"]]
    assert len(urls) == 6 and "https://www.shop.example/about" in urls  # p1 de-duplicated
    assert dict(res["urls"])["https://www.shop.example/p0"].endswith("sm-products.xml.gz")
    assert len(res["errors"]) == 1


# ------------------------------------------------------------------ inspections + reports
def _queue(n=25):
    return call("queue_urls", site="shop.example", urls=[f"https://www.shop.example/p{i}" for i in range(n)]
                + ["https://elsewhere.example/x"])


def test_inspection_batch_report_and_listing(env):
    q = _queue()
    assert q["newly_queued"] == 25 and q["outside_property"] == 1
    run = call("run_inspections", site="shop.example", max_urls=25, concurrency=4)
    assert run["inspected"] == 25 and run["stopped"] == "done" and run["never_inspected"] == 0
    rep = call("indexing_report", site="shop.example")
    assert rep["inspected"] == 25 and rep["indexed"] == 5 and rep["not_indexed"] == 20
    reasons = {r["reason"]: r for r in rep["why_pages_arent_indexed_and_indexed_reasons"]}
    assert reasons["Crawled - currently not indexed"]["source"] == "Google systems"
    assert reasons["Excluded by \u2018noindex\u2019 tag"]["source"] == "Website"
    assert reasons["Excluded by \u2018noindex\u2019 tag"]["fix"]
    assert rep["canonical"]["google_vs_declared_conflict"] == 5
    assert rep["rich_results"]["by_type"]["Product snippets"]["pages_with_warnings"] == 25
    lst = call("list_inspected_urls", site="shop.example", coverage_state="crawled – currently not indexed", limit=2)
    assert lst["total_matching"] == 5 and lst["returned"] == 2
    idx = call("list_inspected_urls", site="shop.example", indexed=True, with_performance_days=28, export_csv=True)
    assert idx["total_matching"] == 5 and "clicks" in idx["urls"][0] and os.path.isfile(idx["csv_file"])
    rich = call("list_inspected_urls", site="shop.example", rich_type="breadcrumbs")
    assert rich["total_matching"] == 25
    by_sitemap = call("indexing_report", site="shop.example", sitemap="https://www.shop.example/sitemap.xml")
    assert by_sitemap["inspected"] == 25


def test_changes_and_stale_reinspection(env):
    _queue(5)
    call("run_inspections", site="shop.example")
    env.states["https://www.shop.example/p0"] = "Crawled - currently not indexed"   # was indexed
    env.states["https://www.shop.example/p1"] = "Submitted and indexed"             # was not indexed
    again = call("run_inspections", site="shop.example", stale_days=0)
    assert again["inspected"] == 5
    ch = call("indexing_changes", site="shop.example")
    assert ch["changed_urls"] == 2 and ch["dropped_out_of_index"] == 1 and ch["newly_indexed"] == 1
    assert ch["inspection_trend"][0]["inspected"] == 10


def test_quota_stop_and_daily_limit(env, monkeypatch):
    _queue(30)
    env.quota_after = 7
    run = call("run_inspections", site="shop.example", max_urls=30, concurrency=1)
    assert run["inspected"] == 7 and "quota" in run["stopped"].lower()
    monkeypatch.setenv("SEARCH_CONSOLE_MCP_INSPECT_DAILY_LIMIT", "7")
    env.quota_after = None
    blocked = call("run_inspections", site="shop.example")
    assert blocked["inspected"] == 0 and "daily inspection limit" in blocked["stopped"]


def test_time_budget(env, monkeypatch):
    from search_console_mcp import api as api_mod
    _queue(20)
    real = api_mod.inspect

    def slow(site, url, language="en-US"):
        time.sleep(0.4)
        return real(site, url, language)

    monkeypatch.setattr(api_mod, "inspect", slow)
    run = call("run_inspections", site="shop.example", max_urls=20, time_budget_seconds=5, concurrency=1)
    assert 5 <= run["inspected"] < 20 and "time budget" in run["stopped"]


def test_background_job(env, monkeypatch):
    from search_console_mcp import inspector
    _queue(12)
    job = call("inspection_job", site="shop.example", action="start", daily_limit=10, stale_days=30)
    assert job["job"]["active"] == 1
    inspector._Worker().tick()
    status = call("inspection_job", site="shop.example", action="status")
    assert status["quota_used_today"] == 10 and status["never_inspected"] == 2
    assert status["job"]["paused_until"]  # paused until the next PT day
    call("inspection_job", site="shop.example", action="stop")
    assert call("inspection_job", site="shop.example")["job"]["active"] == 0


def test_diagnose_url_live(env, web):
    page = (b"<html><head><title>Blue chair</title><meta name='robots' content='noindex,follow'>"
            b"<link rel='canonical' href='/chair-blue'></head><body><h1>Chair</h1><p>short</p></body></html>")
    Site.routes["/old-chair"] = (301, {"Location": "/chair"}, b"")
    Site.routes["/chair"] = (200, {"Content-Type": "text/html; charset=utf-8", "X-Robots-Tag": "noarchive"}, page)
    Site.routes["/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /old-\n")
    from search_console_mcp import api
    env.site_url = f"{web}/"  # a URL-prefix property that covers the local test site
    api._sites_cache.update(at=0.0, sites=None)
    res = call("diagnose_url", site=f"{web}/", url=f"{web}/old-chair")
    live = res["live"]
    assert live["redirect_hops"] == 1 and live["final_url"] == f"{web}/chair" and live["status"] == 200
    assert live["meta_robots"] == ["robots: noindex,follow"] and live["canonical_tags"] == [f"{web}/chair-blue"]
    assert live["robots_txt"]["requested_url_allowed"] is False
    text = " ".join(res["findings"])
    assert "noindex" in text and "Canonical points elsewhere" in text and "robots.txt blocks" in text


def test_auth_reload_on_refresh_error(env, monkeypatch):
    from google.auth.exceptions import RefreshError
    from search_console_mcp import api, auth
    state = {"n": 0, "reloads": 0}

    class Broken:
        def request(self, *a, **k):
            state["n"] += 1
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(auth, "session", lambda: Broken())
    monkeypatch.setattr(auth, "reload", lambda: state.__setitem__("reloads", state["reloads"] + 1))
    with pytest.raises(auth.AuthError) as err:
        api.request("GET", api.WM + "/sites")
    assert state["reloads"] == 1 and "expired or was revoked" in str(err.value)


def test_api_error_mapping(env):
    from search_console_mcp import api
    assert "Search Console scope" in api._explain(403, "Request had insufficient authentication scopes.")
    assert "Enable the Google Search Console API" in api._explain(403, "Google Search Console API has not been used in project 1 before or it is disabled.")
    with pytest.raises(Exception) as err:
        call("get_sitemap", site="shop.example", sitemap_url="https://www.shop.example/nope.xml")
    assert "404" in str(err.value)


def test_queue_from_sitemaps_tool_and_filter_by_sitemap(env, web):
    from search_console_mcp import api
    env.site_url = f"{web}/"
    api._sites_cache.update(at=0.0, sites=None)
    a = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
         + "".join(f"<url><loc>{web}/prod/p{i}</loc></url>" for i in range(4)) + "</urlset>").encode()
    b = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
         + "".join(f"<url><loc>{web}/cat/c{i}</loc></url>" for i in range(3)) + "</urlset>").encode()
    Site.routes["/sitemap-products.xml"] = (200, {}, a)
    Site.routes["/sitemap-categories.xml"] = (200, {}, b)
    env.sitemaps = {f"{web}/sitemap-products.xml": {"path": f"{web}/sitemap-products.xml"},
                    f"{web}/sitemap-categories.xml": {"path": f"{web}/sitemap-categories.xml"}}
    res = call("queue_from_sitemaps", site=f"{web}/")
    assert res["unique_urls"] == 7 and res["newly_queued"] == 7
    call("run_inspections", site=f"{web}/", max_urls=50)
    only_products = call("indexing_report", site=f"{web}/", sitemap=f"{web}/sitemap-products.xml")
    assert only_products["inspected"] == 4
    ov = call("property_overview", site=f"{web}/")
    assert ov["sitemaps"]["count"] == 2 and ov["indexing_local"]["inspected"] == 7
    assert "robots" in json.dumps(ov["robots_txt_last_check"]) or ov["robots_txt_last_check"]


def test_robots_tools(env, web):
    Site.routes["/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /search\nAllow: /search/help\n")
    first = call("robots_txt_check", host_or_url=web)
    t = call("robots_txt_test", url=f"{web}/search?q=chair")
    assert t["allowed"] is False and t["rule"]["path"] == "/search"
    assert call("robots_txt_test", url=f"{web}/search/help")["allowed"] is True
    Site.routes["/robots.txt"] = (200, {}, b"User-agent: *\nDisallow: /\n")
    second = call("robots_txt_check", host_or_url=web)
    old = call("robots_txt_test", url=f"{web}/shop", check_id=first["check_id"])
    assert old["allowed"] is True and old["check_id"] == first["check_id"]
    assert call("robots_txt_test", url=f"{web}/shop")["allowed"] is False
    diff = call("robots_txt_diff", from_check_id=first["check_id"], to_check_id=second["check_id"])
    assert "+Disallow: /" in diff["diff"]
    watch = call("robots_txt_watch", action="add", host_or_url=web, interval_hours=6)
    assert watch["watching"][0]["interval_hours"] == 6
    assert call("robots_txt_watch", action="remove", host_or_url=web)["watching"] == []


def test_robots_check_property_hosts(env, monkeypatch):
    from search_console_mcp import robots
    seen = []
    monkeypatch.setattr(robots, "check", lambda host: seen.append(host) or {
        "status": 200, "google_interpretation": "parsed", "size_bytes": 10, "changed_since_last_check": None,
        "warnings": [], "sitemaps": [], "check_id": len(seen)})
    res = call("robots_txt_check_property", site="shop.example")
    assert res["hosts_checked"] == 1 and seen == ["www.shop.example"]


def test_core_web_vitals_parsing(env, monkeypatch):
    from search_console_mcp import crux
    monkeypatch.setenv("CRUX_API_KEY", "k")

    class R:
        status_code = 200
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload

    latest = {"record": {"metrics": {
        "largest_contentful_paint": {"histogram": [{"density": 0.7}, {"density": 0.2}, {"density": 0.1}], "percentiles": {"p75": 2300}},
        "interaction_to_next_paint": {"histogram": [{"density": 0.8}, {"density": 0.15}, {"density": 0.05}], "percentiles": {"p75": 260}},
        "cumulative_layout_shift": {"histogram": [{"density": 0.9}, {"density": 0.05}, {"density": 0.05}], "percentiles": {"p75": "0.05"}}},
        "collectionPeriod": {"firstDate": {"year": 2026, "month": 9, "day": 1}, "lastDate": {"year": 2026, "month": 9, "day": 28}}}}
    hist = {"record": {"metrics": {"largest_contentful_paint": {"percentilesTimeseries": {"p75s": [2600, 2400]}}},
                       "collectionPeriods": [{"firstDate": {"year": 2026, "month": 8, "day": 1}, "lastDate": {"year": 2026, "month": 8, "day": 28}},
                                             {"firstDate": {"year": 2026, "month": 8, "day": 8}, "lastDate": {"year": 2026, "month": 9, "day": 4}}]}}
    sent = []
    monkeypatch.setattr(crux.requests, "post", lambda url, params, json, timeout: sent.append((url, json)) or R(hist if "History" in url else latest))
    res = call("core_web_vitals", target="www.shop.example")
    assert sent[0][1] == {"origin": "https://www.shop.example", "formFactor": "PHONE"}
    assert res["metrics"]["interaction_to_next_paint"]["rating"] == "needs improvement"
    assert res["metrics"]["cumulative_layout_shift"]["rating"] == "good" and res["core_web_vitals_pass"] is False
    h = call("core_web_vitals", target="https://www.shop.example/p1", history=True, form_factor="ALL")
    assert sent[1][1] == {"url": "https://www.shop.example/p1"}
    assert h["history"]["largest_contentful_paint"][1] == {"period_end": "2026-09-04", "p75": 2400, "rating": "good"}
