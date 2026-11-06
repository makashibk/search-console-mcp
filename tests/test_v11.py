"""Tests for the v1.1 features. Reuses the fake Google API and local web server from test_server.py."""

import gzip
import os
import shutil
import ssl
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

from conftest import FakeResp, Site, call

CUR, PREV = "2026-09-05", "2026-08-08"  # 28-day windows ending 2026-10-02 (the fake's last complete day)


def rows_for(dims, data):
    return {"rows": [{"keys": [r[d] for d in dims], "clicks": r["clicks"], "impressions": r["impressions"],
                      "ctr": r["clicks"] / r["impressions"] if r["impressions"] else 0, "position": r["position"]}
                     for r in data]}


# ------------------------------------------------------------------ traffic drops
def test_traffic_drops_exact_decomposition(env):
    current = [
        {"page": "https://www.shop.example/a", "clicks": 50, "impressions": 1000, "position": 3.0},   # CTR drop
        {"page": "https://www.shop.example/b", "clicks": 50, "impressions": 1000, "position": 4.2},   # demand drop
        {"page": "https://www.shop.example/c", "clicks": 20, "impressions": 900, "position": 9.0},    # ranking loss
    ]
    previous = [
        {"page": "https://www.shop.example/a", "clicks": 100, "impressions": 1000, "position": 3.0},
        {"page": "https://www.shop.example/b", "clicks": 100, "impressions": 2000, "position": 4.0},
        {"page": "https://www.shop.example/c", "clicks": 80, "impressions": 1000, "position": 3.0},
        {"page": "https://www.shop.example/d", "clicks": 40, "impressions": 500, "position": 5.0},    # disappeared
    ]

    def fake(body):
        if body["dimensions"] == ["page"]:
            return rows_for(["page"], current if body["startDate"] == CUR else previous)
        if body["dimensions"] == ["query"]:
            q = [{"query": "chair", "clicks": 10 if body["startDate"] == CUR else 40, "impressions": 300, "position": 5.0}]
            return rows_for(["query"], q)
        return {"rows": []}

    env.analytics_override = fake
    res = call("traffic_drops", site="shop.example", explain_top=2)
    by = {d["page"].rsplit("/", 1)[-1]: d for d in res["drops"]}
    assert by["a"]["diagnosis"] == "ctr_drop" and by["b"]["diagnosis"] == "demand_drop"
    assert by["c"]["diagnosis"] == "ranking_loss" and by["d"]["diagnosis"] == "disappeared"
    for d in res["drops"]:  # the two effects add up exactly to the click change
        assert round(d["loss_from_fewer_impressions"] + d["loss_from_lower_ctr"], 6) == d["clicks"] - d["clicks_prev"]
    assert by["b"]["loss_from_fewer_impressions"] == -50.0 and by["b"]["loss_from_lower_ctr"] == 0.0
    assert res["total_clicks_lost"] == 50 + 50 + 60 + 40
    assert res["drops"][0]["top_losing_querys"][0]["query"] == "chair"


# ------------------------------------------------------------------ content decay
def test_content_decay(env):
    series = {"2026-07-05": {"x": 100, "y": 100, "z": 15}, "2026-08-04": {"x": 70, "y": 120, "z": 10},
              "2026-09-03": {"x": 40, "y": 50, "z": 5}}

    def fake(body):
        data = series.get(body["startDate"], {})
        return rows_for(["page"], [{"page": f"https://www.shop.example/{k}", "clicks": v, "impressions": v * 20,
                                    "position": 5.0} for k, v in data.items()])

    env.analytics_override = fake
    res = call("content_decay", site="shop.example")
    assert res["periods"] == [["2026-07-05", "2026-08-03"], ["2026-08-04", "2026-09-02"], ["2026-09-03", "2026-10-02"]]
    assert [r["page"][-1] for r in res["results"]] == ["x"]
    assert res["results"][0]["clicks_by_period"] == [100, 70, 40] and res["results"][0]["decline_pct"] == 60.0


# ------------------------------------------------------------------ alerts
def test_alerts(env):
    cur = "2026-09-26"

    def fake(body):
        now = body["startDate"] == cur
        if body["dimensions"] == []:
            return {"rows": [{"clicks": 500 if now else 1000, "impressions": 20000, "ctr": 0.03, "position": 8.0}]}
        if body["dimensions"] == ["query"]:
            return rows_for(["query"], [
                {"query": "q1", "clicks": 5 if now else 60, "impressions": 1000, "position": 12.0 if now else 3.0},
                {"query": "q2", "clicks": 30 if now else 100, "impressions": 1000, "position": 4.5 if now else 4.0}])
        if body["dimensions"] == ["page"]:
            pages = [{"page": "https://www.shop.example/p1", "clicks": 50 if now else 200, "impressions": 4000, "position": 5.0}]
            if not now:
                pages.append({"page": "https://www.shop.example/p2", "clicks": 30, "impressions": 600, "position": 6.0})
            return rows_for(["page"], pages)
        return {"rows": []}

    env.analytics_override = fake
    res = call("check_alerts", site="shop.example")
    types = {(a["type"], a.get("query") or a.get("page", "")[-2:]): a for a in res["alerts"]}
    assert types[("site_clicks_drop", "")]["severity"] == "critical"
    assert types[("position_drop", "q1")]["fell_off_page_one"] is True
    assert types[("ctr_collapse", "q2")]["change_pct"] == -70.0
    assert types[("page_clicks_drop", "p1")]["severity"] == "critical"
    assert types[("page_disappeared", "p2")]["severity"] == "critical"
    assert res["alerts"][0]["severity"] == "critical" and res["period"] == ["2026-09-26", "2026-10-02"]


# ------------------------------------------------------------------ sections + segments
def test_sections(env):
    pages = [{"page": "https://www.shop.example/blog/a", "clicks": 30, "impressions": 100, "position": 2.0},
             {"page": "https://www.shop.example/blog/", "clicks": 10, "impressions": 300, "position": 6.0},
             {"page": "https://www.shop.example/shop/x.html", "clicks": 50, "impressions": 500, "position": 4.0},
             {"page": "https://www.shop.example/contact", "clicks": 10, "impressions": 100, "position": 1.0}]
    env.analytics_override = lambda body: rows_for(["page"], pages)
    res = call("section_performance", site="shop.example")
    by = {s["section"]: s for s in res["sections"]}
    assert set(by) == {"/blog/", "/shop/", "/"}
    assert by["/blog/"]["clicks"] == 40 and by["/blog/"]["position"] == 5.0  # (2*100 + 6*300) / 400
    assert by["/shop/"]["share_of_clicks_pct"] == 50.0
    custom = call("section_performance", site="shop.example", prefixes=["/blog/"], compare_previous=False)
    assert {s["section"] for s in custom["sections"]} == {"/blog/", "(other)"}


def test_query_segments_with_brand_note(env):
    queries = [{"query": "makashibk hyva", "clicks": 40, "impressions": 100, "position": 1.0},
               {"query": "how to install hyva", "clicks": 10, "impressions": 400, "position": 8.0},
               {"query": "hoe werkt magento", "clicks": 5, "impressions": 200, "position": 15.0},
               {"query": "yes", "clicks": 1, "impressions": 50, "position": 3.0},
               {"query": "magento 2 hyva theme development services", "clicks": 4, "impressions": 300, "position": 25.0}]

    def fake(body):
        if body["dimensions"] == ["query"]:
            return rows_for(["query"], queries)
        return {"rows": [{"clicks": 80, "impressions": 2000, "ctr": 0.04, "position": 9.0}]}

    env.analytics_override = fake
    call("property_notes", site="shop.example", action="add", kind="brand_terms", text="MakashibK, makashibk")
    res = call("query_segments", site="shop.example")
    seg = res["segments"]
    assert seg["brand"]["branded"]["queries"] == 1 and seg["brand"]["non-branded"]["clicks"] == 20
    assert seg["intent"]["question"]["queries"] == 2  # English + Dutch question
    assert seg["conversational"]["fragment"]["top_queries"][0]["query"] == "yes"
    assert seg["length"]["6+ words"]["queries"] == 1 and seg["results_page"]["page 3+"]["queries"] == 1
    assert res["clicks_from_visible_queries_pct"] == 75.0


# ------------------------------------------------------------------ dashboard, verify, report, notes
def test_dashboard_and_verify(env):
    dash = call("multi_site_dashboard")
    rows = {r["site"]: r for r in dash["dashboard"]}
    assert rows["sc-domain:shop.example"]["clicks"] == 500
    assert rows["https://other.example/"]["status"].startswith("error")
    ok = call("verify_metric", site="shop.example", metric="clicks", expected=500, start_date="2026-09-01", end_date="2026-09-28")
    assert ok["verdict"] == "confirmed" and ok["actual"] == 500
    bad = call("verify_metric", site="shop.example", metric="clicks", expected=400, page="https://www.shop.example/p1")
    assert bad["verdict"] == "mismatch" and bad["checked_with"]["filters"][0]["dimension"] == "page"


def test_weekly_report_file(env):
    res = call("weekly_report", site="shop.example", top_n=3)
    assert os.path.isfile(res["report_file"]) and res["failed_parts"] == {}
    text = open(res["report_file"], encoding="utf-8").read()
    for heading in ("# SEO report", "## Performance", "## Alerts", "## Traffic drops", "## Striking distance",
                    "## Content gaps", "## Content decay", "## Cannibalisation", "## Sitemaps"):
        assert heading in text


def test_notes_crud(env):
    added = call("property_notes", site="shop.example", action="add", kind="migration", text="Moved to Hyva on 2026-09-01")
    note_id = added["notes"][0]["id"]
    assert call("property_notes", site="shop.example")["notes"][0]["kind"] == "migration"
    assert call("property_notes", site="shop.example", action="delete", note_id=note_id)["notes"] == []


def test_performance_query_flags_incomplete(env):
    env.analytics_override = lambda body: {**rows_for(["date"], [{"date": "2026-10-04", "clicks": 3, "impressions": 9, "position": 2.0}]),
                                           "metadata": {"firstIncompleteDate": "2026-10-03"}}
    res = call("performance_query", site="shop.example", dimensions=["date"], days=7)
    assert res["first_incomplete_date"] == "2026-10-03" and "still being processed" in res["note_incomplete"]


def test_cannibalization_url_variants(env):
    rows = [{"query": "hyva theme", "page": "https://www.shop.example/hyva", "clicks": 20, "impressions": 500, "position": 3.0},
            {"query": "hyva theme", "page": "https://shop.example/hyva/", "clicks": 10, "impressions": 400, "position": 5.0},
            {"query": "oak", "page": "https://www.shop.example/oak-a", "clicks": 5, "impressions": 300, "position": 4.0},
            {"query": "oak", "page": "https://www.shop.example/oak-b", "clicks": 5, "impressions": 300, "position": 6.0}]
    env.analytics_override = lambda body: rows_for(["query", "page"], rows)
    res = call("find_cannibalization", site="shop.example", min_impressions=100)
    assert [c["query"] for c in res["competing_pages"]] == ["oak"]
    split = res["url_variant_splits"][0]
    assert split["query"] == "hyva theme"
    assert set(split["url_variants"][0]["differ_by"]) == {"www vs non-www", "trailing slash"}


def test_recrawl_check(env):
    urls = ["https://www.shop.example/p0", "https://www.shop.example/p1"]
    yes = call("run_inspections", site="shop.example", urls=urls, recrawled_since="2026-09-15")
    assert yes["recrawl_check"]["recrawled"] == 2  # fake last crawl = 2026-09-20T10:00Z
    no = call("run_inspections", site="shop.example", urls=urls, recrawled_since="2026-09-20T11:00", stale_days=0)
    assert no["recrawl_check"]["not_yet"] == 2


# ------------------------------------------------------------------ site checks
def test_redirect_chains_and_migration_map(env, web):
    Site.routes["/old"] = (301, {"Location": "/mid"}, b"")
    Site.routes["/mid"] = (301, {"Location": "/new"}, b"")
    Site.routes["/new"] = (200, {}, b"ok")
    Site.routes["/old2"] = (302, {"Location": "/new2"}, b"")
    Site.routes["/new2"] = (200, {}, b"ok")
    Site.routes["/loop"] = (301, {"Location": "/loop"}, b"")
    res = call("check_redirects", pairs=[{"from": f"{web}/old", "to": f"{web}/new"},
                                         {"from": f"{web}/old2", "to": f"{web}/elsewhere"}], urls=[f"{web}/loop"])
    by = {r["url"].rsplit("/", 1)[-1]: r for r in res["results"]}
    assert by["old"]["matches_expected"] is True and by["old"]["redirects"] == 2
    assert any("chain of 2" in i for i in by["old"]["issues"])
    assert by["old2"]["matches_expected"] is False and any("temporary" in i for i in by["old2"]["issues"])
    assert by["loop"]["final_status"] is None and "redirect loop" in by["loop"]["issues"]
    assert res["summary"]["mapping_mismatches"] == 1 and res["summary"]["chains"] == 1


def test_tls_self_signed_and_mixed_content(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to create a throw-away certificate")
    from search_console_mcp import sitechecks
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "2", "-subj", "/CN=localhost"], check=True, capture_output=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        info = sitechecks.tls_info("127.0.0.1", server.server_address[1])
        assert info["valid"] is False and "self" in info["error"].lower()
    finally:
        server.shutdown()
    parser = sitechecks._ResourceParser()
    parser.feed('<script src="http://cdn.x/a.js"></script><img src="http://cdn.x/i.png">'
                '<link rel="canonical" href="http://x/"><link rel="stylesheet" href="http://cdn.x/s.css">')
    kinds = sorted(i["type"].split()[0] for i in parser.insecure)
    assert kinds == ["active", "active", "passive"]  # canonical links are not loaded resources


def test_dns_and_verification(monkeypatch):
    from search_console_mcp import sitechecks

    class R:
        def __init__(self, payload=None, text=""):
            self.status_code, self.payload, self.text = 200, payload, text
        def json(self):
            return self.payload

    def fake_get(url, params=None, headers=None, timeout=None):
        if "dns.google" in url:
            t = params["type"]
            answers = {"A": [{"type": 1, "data": "203.0.113.5"}],
                       "TXT": [{"type": 16, "data": '"google-site-verification=abc123"'}, {"type": 16, "data": '"v=spf1 -all"'}]}
            return R({"Status": 0, "Answer": answers.get(t, [])})
        return R(text='<html><head><meta name="google-site-verification" content="tok999"></head></html>')

    monkeypatch.setattr(sitechecks.requests, "get", fake_get)
    res = sitechecks.check_dns("sc-domain:shop.example")
    assert res["records"]["shop.example"]["A"] == ["203.0.113.5"]
    assert res["google_verification"]["dns_txt_tokens"] == ["abc123"]
    assert res["google_verification"]["html_meta_tags"][0]["token"] == "tok999"


def test_image_audit(web):
    page = (b"<html><head><meta property='og:image' content='/img/og.jpg'>"
            b"<script type='application/ld+json'>{\"@type\":\"Product\",\"image\":[\"https://x/p.jpg\"]}</script></head><body>"
            b"<img src='/img/IMG_1234.jpg' loading='lazy'>"
            b"<img src='data:image/gif;base64,R0lG' data-src='/img/missing.png' alt=''>"
            b"<img src='/img/blue-oak-chair.webp' alt='Blue oak chair' width='800' height='600'>"
            b"</body></html>")
    Site.routes["/product"] = (200, {"Content-Type": "text/html"}, page)
    Site.routes["/img/IMG_1234.jpg"] = (200, {"Content-Type": "image/jpeg"}, b"x" * (400 * 1024))
    Site.routes["/img/blue-oak-chair.webp"] = (200, {"Content-Type": "image/webp"}, b"x" * 1000)
    from search_console_mcp import sitechecks
    res = sitechecks.image_audit([f"{web}/product"])["pages"][0]
    imgs = {i["src"].rsplit("/", 1)[-1]: i for i in res["images"]}
    first = " | ".join(imgs["IMG_1234.jpg"]["issues"])
    for expected in ("missing alt", "no width/height", "lazy-loaded near the top", "generic file name", "legacy format .jpg", "large file"):
        assert expected in first
    lazy = " | ".join(imgs["missing.png"]["issues"])
    assert "JavaScript lazy-load" in lazy and "broken image (404)" in lazy and "empty alt" in lazy
    assert imgs["blue-oak-chair.webp"]["issues"] == []
    assert res["og_image"] == "/img/og.jpg" and res["structured_data_images"] == ["https://x/p.jpg"]


def test_pagespeed_parsing(monkeypatch):
    from search_console_mcp import sitechecks
    payload = {"lighthouseResult": {
        "lighthouseVersion": "13.0.0", "finalUrl": "https://shop.example/",
        "categories": {"performance": {"score": 0.42, "auditRefs": []},
                       "seo": {"score": 0.9, "auditRefs": [{"id": "meta-description"}, {"id": "is-crawlable"}]}},
        "audits": {
            "largest-contentful-paint": {"id": "largest-contentful-paint", "displayValue": "5.1 s", "numericValue": 5100, "score": 0.1},
            "render-blocking-resources": {"id": "render-blocking-resources", "title": "Eliminate render-blocking resources",
                                          "score": 0.3, "details": {"type": "opportunity", "overallSavingsMs": 1200}},
            "unused-javascript": {"id": "unused-javascript", "title": "Reduce unused JavaScript", "score": 0.5,
                                  "metricSavings": {"LCP": 2400, "FCP": 300}},
            "meta-description": {"id": "meta-description", "title": "Document has a meta description", "score": 0,
                                 "scoreDisplayMode": "binary"},
            "is-crawlable": {"id": "is-crawlable", "title": "Page isn't blocked", "score": 1, "scoreDisplayMode": "binary"}}},
        "loadingExperience": {"overall_category": "SLOW",
                              "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4200, "category": "SLOW"}}}}
    sent = {}

    class R:
        status_code = 200
        def json(self):
            return payload

    monkeypatch.setattr(sitechecks.requests, "get", lambda url, params, timeout: sent.update(params=params) or R())
    res = sitechecks.pagespeed("https://shop.example/")
    assert ("category", "PERFORMANCE") in sent["params"] and ("strategy", "MOBILE") in sent["params"]
    assert res["scores"] == {"performance": 42, "seo": 90}
    assert [o["id"] for o in res["opportunities"]] == ["unused-javascript", "render-blocking-resources"]
    assert res["failing_audits"]["seo"] == [{"id": "meta-description", "title": "Document has a meta description"}]
    assert res["field_data_url"]["metrics"]["LARGEST_CONTENTFUL_PAINT_MS"]["p75"] == 4200


# ------------------------------------------------------------------ crawl stats from logs
def test_crawl_logs_import_and_stats(env, tmp_path, monkeypatch):
    import ipaddress
    from search_console_mcp import crawllogs
    monkeypatch.setattr(crawllogs.Verifier, "_load_ranges", staticmethod(lambda: [ipaddress.ip_network("66.249.64.0/19")]))
    phone = "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    desktop = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    image = "Googlebot-Image/1.0"
    browser = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0 Safari/537.36"

    def line(ip, when, path, status, size, ua, rt="0.120", ref="-"):
        return f'{ip} - - [{when}] "GET {path} HTTP/1.1" {status} {size} "{ref}" "{ua}" {rt}\n'

    lines = [
        line("66.249.66.1", "01/Oct/2026:10:00:00 +0000", "/robots.txt", 200, 120, phone, "0.010"),
        line("66.249.66.1", "01/Oct/2026:10:00:05 +0000", "/women/dresses", 200, 50000, phone, "0.250"),
        line("66.249.66.2", "01/Oct/2026:11:00:00 +0000", "/women/dresses", 200, 50000, desktop, "0.150"),
        line("66.249.66.2", "01/Oct/2026:11:00:10 +0000", "/static/app.js", 200, 9000, phone, "0.020"),
        line("66.249.66.3", "02/Oct/2026:09:00:00 +0000", "/old-page", 404, 300, phone, "0.030"),
        line("66.249.66.3", "02/Oct/2026:09:00:01 +0000", "/media/chair.jpg", 200, 70000, image, "0.040"),
        line("66.249.66.3", "02/Oct/2026:09:05:00 +0000", "/checkout", 503, 0, phone, "1.500"),
        line("1.2.3.4", "02/Oct/2026:12:00:00 +0000", "/wp-login.php", 404, 100, desktop),  # fake Googlebot
        line("8.8.4.4", "02/Oct/2026:12:00:00 +0000", "/women/dresses", 200, 50000, browser, ref="https://www.google.com/"),
        "garbage line mentioning Google but not a log line\n",
    ]
    plain = tmp_path / "access.log"
    plain.write_text("".join(lines[:5]), encoding="utf-8")
    gz = tmp_path / "access.log.1.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as handle:
        handle.write("".join(lines[5:]))
    res = crawllogs.start_import([str(tmp_path / "access.log*")], "www.shop.example", wait=True)
    assert len(res["queued"]) == 2
    status = crawllogs.import_status()["imports"]
    assert all(i["status"] == "done" for i in status)
    stats = crawllogs.stats("www.shop.example")
    assert stats["total_crawl_requests"] == 7  # fake Googlebot and browser excluded
    resp = {r["value"]: r["requests"] for r in stats["by_response"]}
    assert resp == {"OK (200)": 5, "Not found (404)": 1, "Server error (5xx)": 1}
    types = {r["value"]: r["requests"] for r in stats["by_file_type"]}
    assert types["robots.txt"] == 1 and types["JavaScript"] == 1 and types["Image"] == 1 and types["HTML"] == 4
    bots = {r["value"]: r["requests"] for r in stats["by_googlebot_type"]}
    assert bots == {"Smartphone": 5, "Desktop": 1, "Image": 1}
    purpose = {r["value"]: r["requests"] for r in stats["by_purpose"]}
    assert purpose == {"Discovery": 6, "Refresh": 1}  # /women/dresses seen twice
    assert stats["avg_response_ms"] == round((10 + 250 + 150 + 20 + 30 + 40 + 1500) / 7, 1)
    assert [d["date"] for d in stats["daily"]] == ["2026-10-01", "2026-10-02"]
    assert stats["host_status_from_logs"]["server_error_days"][0]["date"] == "2026-10-02"
    fake = crawllogs.stats("www.shop.example", verification="fake_only")
    assert fake["total_crawl_requests"] == 1
    assert stats["example_urls_by_response"]["Not found (404)"][0]["url"] == "/old-page"
    again = crawllogs.start_import([str(plain)], "www.shop.example", wait=True)
    assert again["queued"] == [] and "already imported" in again["skipped"][0]["reason"]
    tool = call("crawl_stats", host="www.shop.example", top_urls=3)
    assert tool["top_crawled_urls"][0]["url"] == "/women/dresses"


def test_log_line_formats():
    from search_console_mcp import crawllogs
    vhost = 'shop.example:443 66.249.66.1 - - [05/Oct/2026:01:02:03 +0200] "GET https://shop.example/a?b=1 HTTP/2.0" 301 0 "-" "Googlebot/2.1" 123456'
    m = crawllogs.LINE.match(vhost)
    assert m and m.group("vhost") == "shop.example:443" and m.group("ip") == "66.249.66.1"
    assert crawllogs.response_ms(m.group("rest"), "auto") is None  # integer: unit must be explicit
    assert crawllogs.response_ms(m.group("rest"), "microseconds") == 123.456
    assert crawllogs.parse_time("05/Oct/2026:01:02:03 +0200").isoformat() == "2026-10-05T01:02:03+02:00"
    assert crawllogs.bot_type("AdsBot-Google-Mobile") == "AdsBot" and crawllogs.bot_type("Storebot-Google/1.0") == "StoreBot"
    assert crawllogs.bot_type("Mozilla/5.0 Chrome") is None
    assert crawllogs.response_label(308) == "Moved (other)" and crawllogs.response_label(410) == "Gone (410)"
    assert crawllogs.file_type("/feed/rss.xml") == "Syndication" and crawllogs.file_type("/sitemap.xml") == "Other XML"


def test_cache_and_429_backoff(env, monkeypatch):
    from search_console_mcp import api
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    body = {"startDate": "2026-09-01", "endDate": "2026-09-28", "dimensions": ["page"], "type": "web", "dataState": "all", "rowLimit": 10}
    before = len(env.calls)
    api.search_analytics("sc-domain:shop.example", body)
    api.search_analytics("sc-domain:shop.example", body)
    assert len(env.calls) - before == 1  # second identical call served from the 10-minute cache
    real = env.request
    state = {"n": 0}

    def flaky(method, url, **kw):
        if "searchAnalytics" in url and state["n"] < 2:
            state["n"] += 1
            return FakeResp(429, {"error": {"message": "Rate limit", "status": "RESOURCE_EXHAUSTED"}})
        return real(method, url, **kw)

    env.request = flaky
    data = api.search_analytics("sc-domain:shop.example", {**body, "rowLimit": 11})
    assert state["n"] == 2 and data["rows"]  # two 429s, then success
