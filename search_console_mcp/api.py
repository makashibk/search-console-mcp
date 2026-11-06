"""Thin, read-only client for the Search Console API (searchconsole v1 + webmasters v3 paths)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

from google.auth.exceptions import RefreshError, TransportError

from . import auth
from .util import enc

log = logging.getLogger(__name__)

ROOT = "https://searchconsole.googleapis.com"
WM = ROOT + "/webmasters/v3"
INSPECT_URL = ROOT + "/v1/urlInspection/index:inspect"
MAX_PAGE_ROWS = 25000  # API maximum rows per Search Analytics request


class ApiError(Exception):
    def __init__(self, status: int, message: str, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message

    @property
    def is_quota(self) -> bool:
        return self.status == 429 or self.reason in ("rateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED")


def _explain(status: int, message: str) -> str:
    low = message.lower()
    if "insufficient authentication scopes" in low or "access_token_scope_insufficient" in low:
        return "Your Google login lacks the Search Console scope. " + auth.LOGIN_HELP
    if "has not been used in project" in low or "service_disabled" in low or "is disabled" in low:
        return message + " -> Enable the Google Search Console API in your Cloud project, wait a minute, retry."
    if status == 403 and ("permission" in low or "not have" in low):
        return message + " -> The signed-in Google account has no access to this property. Check the exact property with list_properties."
    if status == 404:
        return message + " -> Property or sitemap not found. Use the exact property string from list_properties (e.g. sc-domain:example.com or https://www.example.com/)."
    return message


class WriteBlocked(Exception):
    """A request that could change something in Search Console was stopped before it left this machine."""


_API_HOST = urlsplit(ROOT).netloc
# The only POST endpoints that read data: Search Analytics query and URL Inspection.
_READ_ONLY_POSTS = (
    re.compile(r"/webmasters/v3/sites/[^/]+/searchAnalytics/query"),
    re.compile(r"/v1/urlInspection/index:inspect"),
)


def guard(method: str, url: str) -> None:
    """Read-only guard: allow GET and the two read-only POST endpoints on the Search Console API, nothing else.

    Runs before every request, so anything else (PUT, DELETE, PATCH, other POSTs, other hosts) never leaves the machine.
    """
    parts = urlsplit(url)
    verb = method.upper()
    on_api = parts.scheme == "https" and parts.netloc == _API_HOST
    if on_api and verb == "GET":
        return
    if on_api and verb == "POST" and any(rule.fullmatch(parts.path) for rule in _READ_ONLY_POSTS):
        return
    raise WriteBlocked(f"Blocked {verb} {url}: this server is read-only. It only sends GET requests, "
                       "Search Analytics queries and URL Inspections to the Search Console API.")


def request(method: str, url: str, *, json_body=None, params=None, timeout: int = 60, retries: int = 2,
            retry_429: int = 0):
    guard(method, url)
    attempt = 0
    reloaded = False
    waited_429 = 0
    while True:
        attempt += 1
        try:
            resp = auth.session().request(method, url, json=json_body, params=params, timeout=timeout)
        except RefreshError as exc:
            if not reloaded:
                reloaded = True
                auth.reload()  # the user may have signed in again since startup
                continue
            raise auth.AuthError(auth.describe_refresh_error(exc)) from exc
        except (TransportError, OSError) as exc:
            if attempt <= retries:
                time.sleep(2 * attempt)
                continue
            raise ApiError(0, f"Network error calling Google: {exc}") from exc
        if resp.status_code == 401 and not reloaded:
            reloaded = True
            auth.reload()
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt <= retries:
            time.sleep(2 * attempt)
            continue
        if resp.status_code == 429 and waited_429 < retry_429:
            waited_429 += 1
            time.sleep(min(30, 2 ** waited_429))  # per-minute limits: back off 2s, 4s, 8s...
            continue
        if resp.status_code >= 400:
            message, reason = resp.text[:500], ""
            try:
                err = resp.json().get("error", {})
                message = err.get("message", message)
                reason = err.get("status", "") or ((err.get("errors") or [{}])[0].get("reason", ""))
            except ValueError:
                pass
            raise ApiError(resp.status_code, _explain(resp.status_code, message), reason)
        if not resp.content:
            return {}
        return resp.json()


# ---------------------------------------------------------------- properties
_sites_cache: dict = {"at": 0.0, "sites": None}
_sites_lock = threading.Lock()


def list_sites(force: bool = False) -> list[dict]:
    with _sites_lock:
        if force or _sites_cache["sites"] is None or time.time() - _sites_cache["at"] > 600:
            data = request("GET", f"{WM}/sites")
            _sites_cache["sites"] = data.get("siteEntry", [])
            _sites_cache["at"] = time.time()
        return list(_sites_cache["sites"])


def _candidates(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("sc-domain:"):
        return [value]
    if "://" not in value:
        host = value.strip("/").split("/")[0]
        bare = host[4:] if host.startswith("www.") else host
        return [f"sc-domain:{bare}", f"https://{host}/", f"https://www.{bare}/", f"http://{host}/", f"http://www.{bare}/"]
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    with_slash = value if value.endswith("/") else value + "/"
    return [value, with_slash, f"{parts.scheme}://{host}/", f"sc-domain:{bare}"]


def resolve_site(value: str) -> str:
    """Map 'example.com', a URL, or an exact property string to a property the user can access."""
    sites = list_sites()
    owned = {s["siteUrl"]: s for s in sites}
    if value in owned:
        return value
    for candidate in _candidates(value):
        if candidate in owned:
            return candidate
    # URL-prefix properties: pick the longest prefix that contains the given URL.
    if "://" in value:
        prefixes = [s for s in owned if not s.startswith("sc-domain:") and value.startswith(s)]
        if prefixes:
            return max(prefixes, key=len)
    if not sites:
        raise ValueError("This Google account has no Search Console properties.")
    names = ", ".join(sorted(owned)[:30])
    raise ValueError(f"No accessible property matches {value!r}. Available: {names}")


def site_covers(site: str, url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if site.startswith("sc-domain:"):
        domain = site.split(":", 1)[1].lower()
        return host == domain or host.endswith("." + domain)
    return url.startswith(site)


# ---------------------------------------------------------------- search analytics
_sa_cache: dict = {}
_sa_cache_lock = threading.Lock()
SA_CACHE_SECONDS = 600


def search_analytics(site: str, body: dict) -> dict:
    """One Search Analytics call. Identical requests within 10 minutes are served from memory."""
    key = (site, json.dumps(body, sort_keys=True))
    now = time.time()
    with _sa_cache_lock:
        hit = _sa_cache.get(key)
        if hit and now - hit[0] < SA_CACHE_SECONDS:
            return hit[1]
    data = request("POST", f"{WM}/sites/{enc(site)}/searchAnalytics/query", json_body=body, timeout=90, retry_429=4)
    with _sa_cache_lock:
        if len(_sa_cache) > 500:
            for old_key in sorted(_sa_cache, key=lambda k: _sa_cache[k][0])[:250]:
                _sa_cache.pop(old_key, None)
        _sa_cache[key] = (now, data)
    return data


def clear_cache() -> None:
    with _sa_cache_lock:
        _sa_cache.clear()


def search_analytics_rows(site: str, body: dict, max_rows: int) -> tuple[list[dict], bool, dict]:
    """Fetch up to max_rows rows, paging with startRow. Returns (rows, truncated, metadata)."""
    rows: list[dict] = []
    start = 0
    metadata: dict = {}
    wanted = max_rows + 1  # one extra row tells us whether more data exists
    while len(rows) < wanted:
        page_size = min(MAX_PAGE_ROWS, wanted - len(rows))
        data = search_analytics(site, {**body, "rowLimit": page_size, "startRow": start})
        if not metadata:
            metadata = data.get("metadata", {}) or {}
        batch = data.get("rows", [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += len(batch)
    return rows[:max_rows], len(rows) > max_rows, metadata


# ---------------------------------------------------------------- sitemaps
def list_sitemaps(site: str, sitemap_index: str | None = None) -> list[dict]:
    params = {"sitemapIndex": sitemap_index} if sitemap_index else None
    return request("GET", f"{WM}/sites/{enc(site)}/sitemaps", params=params).get("sitemap", [])


def get_sitemap(site: str, feedpath: str) -> dict:
    return request("GET", f"{WM}/sites/{enc(site)}/sitemaps/{enc(feedpath)}")


# ---------------------------------------------------------------- url inspection
def inspect(site: str, url: str, language: str = "en-US") -> dict:
    body = {"inspectionUrl": url, "siteUrl": site, "languageCode": language}
    return request("POST", INSPECT_URL, json_body=body, timeout=60, retries=1).get("inspectionResult", {})
