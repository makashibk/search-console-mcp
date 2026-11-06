"""Shared test fixtures: a fake Search Console API and a local HTTP server stand in for Google and the website."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import pytest


# ------------------------------------------------------------------ local website
class Site(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="session")
def web():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ------------------------------------------------------------------ fake Google
class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


class FakeGoogle:
    def __init__(self, site_url):
        self.site_url = site_url
        self.calls = []
        self.sitemaps = {}
        self.inspect_calls = 0
        self.quota_after = None
        self.states = {}
        self.analytics_override = None

    def request(self, method, url, json=None, params=None, timeout=None):
        self.calls.append((method, url, json, params))
        path = urlsplit(url).path
        if path == "/webmasters/v3/sites" and method == "GET":
            return FakeResp(200, {"siteEntry": [{"siteUrl": self.site_url, "permissionLevel": "siteOwner"},
                                                {"siteUrl": "https://other.example/", "permissionLevel": "siteFullUser"}]})
        m = re.match(r"^/webmasters/v3/sites/([^/]+)/searchAnalytics/query$", path)
        if m:
            if unquote(m.group(1)) != self.site_url:
                return FakeResp(403, {"error": {"message": "User does not have sufficient permission for site", "status": "PERMISSION_DENIED"}})
            return FakeResp(200, self.analytics(json))
        m = re.match(r"^/webmasters/v3/sites/([^/]+)/sitemaps(?:/(.+))?$", path)
        if m:
            feed = unquote(m.group(2)) if m.group(2) else None
            if method == "GET" and feed is None:
                return FakeResp(200, {"sitemap": list(self.sitemaps.values())})
            if method == "PUT":
                self.sitemaps[feed] = {"path": feed, "lastSubmitted": "2026-10-01T00:00:00Z", "isPending": True,
                                       "isSitemapsIndex": False, "errors": "0", "warnings": "0"}
                return FakeResp(200)
            if method == "DELETE":
                self.sitemaps.pop(feed, None)
                return FakeResp(204)
            if method == "GET":
                return FakeResp(200, self.sitemaps[feed]) if feed in self.sitemaps else FakeResp(404, {"error": {"message": "Sitemap not found", "status": "NOT_FOUND"}})
        if path == "/v1/urlInspection/index:inspect":
            self.inspect_calls += 1
            if self.quota_after is not None and self.inspect_calls > self.quota_after:
                return FakeResp(429, {"error": {"message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
            return FakeResp(200, {"inspectionResult": self.inspection(json["inspectionUrl"])})
        return FakeResp(404, {"error": {"message": f"unknown {method} {path}"}})

    def analytics(self, body):
        dims = body["dimensions"]
        if dims == ["date"] and body.get("rowLimit") == 25:  # latest-complete-day probe
            return {"rows": [{"keys": ["2026-09-30"], "clicks": 1, "impressions": 1, "ctr": 1, "position": 1}],
                    "metadata": {"firstIncompleteDate": "2026-10-03"}}
        if self.analytics_override:
            return self.analytics_override(body)
        total = 30000 if dims == ["query", "page"] else 40
        start, limit = body.get("startRow", 0), body["rowLimit"]
        factor = __import__("datetime").date.fromisoformat(body["startDate"]).toordinal() % 3 + 1  # periods differ
        rows = []
        for i in range(start, min(total, start + limit)):
            keys = []
            for d in dims:
                keys.append({"date": f"2026-09-{(i % 28) + 1:02d}", "query": f"query {i % 200}",
                             "page": f"https://www.shop.example/p{i % 37}", "country": "nld", "device": "MOBILE",
                             "hour": f"2026-10-01T{i % 24:02d}:00:00-07:00", "searchAppearance": "PRODUCT_SNIPPETS"}[d])
            clicks = (100 - i % 100) * factor
            rows.append({"keys": keys, "clicks": clicks, "impressions": (1000 + i) * factor,
                         "ctr": clicks / ((1000 + i) * factor), "position": 1 + (i % 25)})
        if not dims:
            rows = [{"clicks": 500, "impressions": 20000, "ctr": 0.025, "position": 9.4}]
        return {"rows": rows, "responseAggregationType": "byProperty"}

    def inspection(self, url):
        state = self.states.get(url)
        if state is None:
            n = int(re.sub(r"\D", "", url.rsplit("/", 1)[-1]) or 0)
            state = ["Submitted and indexed", "Crawled - currently not indexed", "Excluded by \u2018noindex\u2019 tag",
                     "Duplicate, Google chose different canonical than user", "Not found (404)"][n % 5]
        idx = {
            "verdict": "PASS" if "indexed" in state and "not" not in state else "NEUTRAL",
            "coverageState": state,
            "robotsTxtState": "ALLOWED",
            "indexingState": "BLOCKED_BY_META_TAG" if "noindex" in state else "INDEXING_ALLOWED",
            "pageFetchState": "NOT_FOUND" if "404" in state else "SUCCESSFUL",
            "lastCrawlTime": "2026-09-20T10:00:00Z",
            "crawledAs": "MOBILE",
            "googleCanonical": url + "?v=other" if "different canonical" in state else url,
            "userCanonical": url,
            "sitemap": ["https://www.shop.example/sitemap.xml"],
            "referringUrls": ["https://www.shop.example/category"],
        }
        rich = {"verdict": "PASS", "detectedItems": [
            {"richResultType": "Product snippets", "items": [{"name": "Item", "issues": [
                {"issueMessage": "Missing field \"review\"", "severity": "WARNING"}]}]},
            {"richResultType": "Breadcrumbs", "items": [{"name": "Unnamed item", "issues": []}]}]}
        return {"inspectionResultLink": "https://search.google.com/search-console/inspect?x", "indexStatusResult": idx,
                "richResultsResult": rich}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_CONSOLE_MCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SEARCH_CONSOLE_MCP_EXPORT_DIR", str(tmp_path / "exports"))
    from search_console_mcp import api, auth, db, inspector, performance
    db._initialised.clear()
    api._sites_cache.update(at=0.0, sites=None)
    api.clear_cache()
    performance._complete_cache.clear()
    fake = FakeGoogle("sc-domain:shop.example")
    monkeypatch.setattr(auth, "session", lambda: fake)
    monkeypatch.setattr(inspector, "REQUESTS_PER_SECOND", 1000.0)
    inspector._limiter.interval = 0.0
    return fake


def call(name, **kwargs):
    from search_console_mcp import server
    fn = getattr(server, name)
    return json.loads(fn(**kwargs))
