"""Crawl stats rebuilt from web server access logs (Search Console has no Crawl stats API).

Supports Apache/Nginx/Varnish "combined" style logs, optionally with a leading virtual host and a trailing
response-time field, plain or .gz. Hits claiming to be Google are verified against Google's published crawler
IP ranges (fallback: reverse + forward DNS), so fake Googlebots are counted separately.
"""

from __future__ import annotations

import datetime as dt
import glob
import gzip
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections import defaultdict
from urllib.parse import urlsplit

import requests

from . import db
from .util import PT, data_dir, now_iso

log = logging.getLogger(__name__)

RANGE_URLS = [
    "https://developers.google.com/static/search/apis/ipranges/googlebot.json",
    "https://developers.google.com/static/search/apis/ipranges/special-crawlers.json",
    "https://developers.google.com/static/search/apis/ipranges/user-triggered-fetchers.json",
    "https://developers.google.com/static/search/apis/ipranges/user-triggered-fetchers-google.json",
]
GOOGLE_RDNS = (".googlebot.com", ".google.com", ".googleusercontent.com")
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

LINE = re.compile(
    r'^(?:(?P<vhost>[A-Za-z0-9.-]+(?::\d+)?)\s+)?'
    r'(?P<ip>[0-9A-Fa-f:.]+)\s+\S+\s+\S+\s+'
    r'\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<target>\S+)(?:\s+[^"]*)?"\s+'
    r'(?P<status>\d{3})\s+(?P<bytes>\d+|-)'
    r'(?:\s+"(?P<ref>(?:[^"\\]|\\.)*)"\s+"(?P<ua>(?:[^"\\]|\\.)*)")?'
    r'(?P<rest>.*)$')
RT = re.compile(r'(?:rt=|request_time=|\s|")(\d+(?:\.\d+)?)(?:"|\s|$)')


# ---------------------------------------------------------------- classification
def bot_type(ua: str | None) -> str | None:
    if not ua:
        return None
    u = ua.lower()
    if "googlebot-image" in u:
        return "Image"
    if "googlebot-video" in u:
        return "Video"
    if "googlebot-news" in u:
        return "News"
    if "storebot-google" in u:
        return "StoreBot"
    if "adsbot-google" in u:
        return "AdsBot"
    if "mediapartners-google" in u:
        return "AdSense"
    if "google-inspectiontool" in u:
        return "Inspection tool"
    if "googleother" in u:
        return "GoogleOther"
    if "googlebot" in u:
        return "Smartphone" if ("mobile" in u or "android" in u) else "Desktop"
    if any(t in u for t in ("feedfetcher-google", "google-read-aloud", "google-site-verification", "googleproducer")):
        return "User-triggered fetcher"
    if "google" in u and any(t in u for t in ("bot", "crawler", "fetcher", "apis-google")):
        return "Other Google agent"
    return None


def response_label(status: int) -> str:
    if status == 200:
        return "OK (200)"
    if status == 301:
        return "Moved permanently (301)"
    if status == 302:
        return "Moved temporarily (302)"
    if status == 304:
        return "Not modified (304)"
    if status in (303, 307, 308):
        return "Moved (other)"
    if status in (401, 407):
        return "Unauthorized (401/407)"
    if status == 404:
        return "Not found (404)"
    if status == 410:
        return "Gone (410)"
    if 400 <= status < 500:
        return "Other client error (4xx)"
    if status >= 500:
        return "Server error (5xx)"
    return f"Other ({status})"


_EXT = {
    "html": "HTML", "htm": "HTML", "php": "HTML", "asp": "HTML", "aspx": "HTML", "jsp": "HTML",
    "css": "CSS", "js": "JavaScript", "mjs": "JavaScript", "json": "JSON", "pdf": "PDF",
    "xml": "Other XML", "rss": "Syndication", "atom": "Syndication", "kml": "Geographic data",
    "jpg": "Image", "jpeg": "Image", "png": "Image", "gif": "Image", "webp": "Image", "avif": "Image",
    "svg": "Image", "ico": "Image", "bmp": "Image", "tif": "Image", "tiff": "Image",
    "mp4": "Video", "webm": "Video", "mov": "Video", "m3u8": "Video", "mp3": "Audio", "ogg": "Audio", "wav": "Audio",
}


def file_type(path: str) -> str:
    if path == "/robots.txt":
        return "robots.txt"
    clean = path.split("?", 1)[0]
    name = clean.rsplit("/", 1)[-1]
    if "." not in name:
        return "HTML"
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "xml" and "feed" in clean.lower():
        return "Syndication"
    return _EXT.get(ext, "Other file type")


def parse_time(value: str) -> dt.datetime:
    day, mon, rest = value.split("/", 2)
    year, hh, mm, ss_tz = rest.split(":", 3)
    ss, tz = ss_tz.split(" ", 1) if " " in ss_tz else (ss_tz, "+0000")
    sign = -1 if tz.startswith("-") else 1
    offset = dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    return dt.datetime(int(year), MONTHS[mon], int(day), int(hh), int(mm), int(ss), tzinfo=dt.timezone(offset))


def response_ms(rest: str, unit: str) -> float | None:
    if unit == "none" or not rest:
        return None
    m = RT.search(" " + rest)
    if not m:
        return None
    raw = m.group(1)
    value = float(raw)
    if unit == "auto":
        # Only a decimal value (nginx $request_time, seconds) is safe to guess; a bare integer could be
        # Apache %D (microseconds) or mod_logio byte counts, so integers need an explicit unit.
        return value * 1000 if "." in raw else None
    if unit == "seconds":
        return value * 1000
    if unit == "milliseconds":
        return value
    if unit == "microseconds":
        return value / 1000
    return None


# ---------------------------------------------------------------- Googlebot verification
class Verifier:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.networks = self._load_ranges() if enabled else None
        self.cache: dict[str, str] = {}
        self.dns_lookups = 0

    @staticmethod
    def _load_ranges():
        cache = data_dir() / "google-crawler-ranges.json"
        cached = None
        if cache.exists():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
        fresh = cached and time.time() - cached.get("fetched_at", 0) < 7 * 86400
        if not fresh:
            prefixes, ok = [], 0
            for url in RANGE_URLS:
                try:
                    resp = requests.get(url, timeout=20)
                    if resp.status_code == 200:
                        for p in resp.json().get("prefixes", []):
                            prefixes.append(p.get("ipv4Prefix") or p.get("ipv6Prefix"))
                        ok += 1
                except (requests.RequestException, ValueError):
                    continue
            if ok:
                cached = {"fetched_at": time.time(), "prefixes": [p for p in prefixes if p]}
                cache.write_text(json.dumps(cached), encoding="utf-8")
        if not cached:
            return None
        nets = []
        for p in cached["prefixes"]:
            try:
                nets.append(ipaddress.ip_network(p, strict=False))
            except ValueError:
                continue
        return nets or None

    def check(self, ip: str) -> str:
        if not self.enabled:
            return "not checked"
        hit = self.cache.get(ip)
        if hit:
            return hit
        status = "not checked"
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            self.cache[ip] = "not checked"
            return "not checked"
        if self.networks is not None:
            status = "verified" if any(addr in n for n in self.networks if n.version == addr.version) else "not google ip"
        elif self.dns_lookups < 300:
            self.dns_lookups += 1
            try:
                name = socket.gethostbyaddr(ip)[0].lower()
                if name.endswith(GOOGLE_RDNS) and ip in socket.gethostbyname_ex(name)[2]:
                    status = "verified"
                else:
                    status = "not google ip"
            except (OSError, socket.herror, socket.gaierror):
                status = "not checked"
        self.cache[ip] = status
        return status


# ---------------------------------------------------------------- import
_import_lock = threading.Lock()
_import_thread: threading.Thread | None = None


def _signature(path: str) -> str:
    st = os.stat(path)
    with open(path, "rb") as handle:
        head = hashlib.sha1(handle.read(65536)).hexdigest()
    return f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{head}"


def _open(path: str):
    raw = open(path, "rb")
    magic = raw.read(2)
    raw.seek(0)
    stream = gzip.GzipFile(fileobj=raw) if magic == b"\x1f\x8b" else raw
    return raw, io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="")


def _flush(daily: dict, urls: dict, seen_new: dict) -> None:
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO crawl_daily (host, pt_date, response, file_type, bot_type, purpose, verification, requests, bytes, rt_sum_ms, rt_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(host, pt_date, response, file_type, bot_type, purpose, verification) DO UPDATE SET "
            "requests=crawl_daily.requests+excluded.requests, bytes=crawl_daily.bytes+excluded.bytes, "
            "rt_sum_ms=crawl_daily.rt_sum_ms+excluded.rt_sum_ms, rt_count=crawl_daily.rt_count+excluded.rt_count",
            [(*k, *v) for k, v in daily.items()])
        conn.executemany(
            "INSERT INTO crawl_urls (host, url, response, bot_type, verification, hits, bytes, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(host, url, response, bot_type, verification) DO UPDATE SET "
            "hits=crawl_urls.hits+excluded.hits, bytes=crawl_urls.bytes+excluded.bytes, "
            "first_seen=MIN(crawl_urls.first_seen, excluded.first_seen), last_seen=MAX(crawl_urls.last_seen, excluded.last_seen)",
            [(*k, *v) for k, v in urls.items()])
        conn.executemany("INSERT OR IGNORE INTO crawl_seen (host, url, first_seen) VALUES (?,?,?)",
                         [(h, u, d) for (h, u), d in seen_new.items()])
    daily.clear()
    urls.clear()
    seen_new.clear()


def _seen_for(host: str, cache: dict) -> set:
    if host not in cache:
        with db.connect() as conn:
            cache[host] = {r["url"] for r in conn.execute("SELECT url FROM crawl_seen WHERE host=?", (host,))}
    return cache[host]


def import_file(import_id: int, path: str, host_hint: str | None, unit: str, verifier: Verifier) -> None:
    daily: dict = defaultdict(lambda: [0, 0, 0.0, 0])
    urls: dict = {}
    seen_new: dict = {}
    seen_cache: dict = {}
    counts = {"lines": 0, "google": 0, "verified": 0, "not google ip": 0, "not checked": 0, "unparsed": 0}
    size = os.path.getsize(path)
    raw, text = _open(path)
    try:
        for line in text:
            counts["lines"] += 1
            if counts["lines"] % 100000 == 0:
                with db.connect() as conn:
                    conn.execute("UPDATE crawl_imports SET bytes_read=?, lines=?, google_lines=? WHERE id=?",
                                 (raw.tell(), counts["lines"], counts["google"], import_id))
            if "oogle" not in line and "OOGLE" not in line:
                continue
            m = LINE.match(line.rstrip("\r\n"))
            if not m:
                counts["unparsed"] += 1
                continue
            kind = bot_type(m.group("ua"))
            if kind is None:
                continue
            counts["google"] += 1
            target = m.group("target")
            host = (m.group("vhost") or host_hint or "(unknown host)").split(":", 1)[0].lower()
            if target.startswith(("http://", "https://")):
                parts = urlsplit(target)
                host = (parts.hostname or host).lower()
                target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            try:
                when = parse_time(m.group("time")).astimezone(PT).date().isoformat()
            except (ValueError, KeyError):
                counts["unparsed"] += 1
                continue
            status = int(m.group("status"))
            size_b = 0 if m.group("bytes") == "-" else int(m.group("bytes"))
            ms = response_ms(m.group("rest") or "", unit)
            verification = verifier.check(m.group("ip"))
            counts[verification] += 1
            seen = _seen_for(host, seen_cache)
            if target in seen:
                purpose = "Refresh"
            else:
                purpose = "Discovery"
                seen.add(target)
                seen_new[(host, target)] = when
            label = response_label(status)
            agg = daily[(host, when, label, file_type(target), kind, purpose, verification)]
            agg[0] += 1
            agg[1] += size_b
            if ms is not None:
                agg[2] += ms
                agg[3] += 1
            key = (host, target[:2000], label, kind, verification)
            item = urls.get(key)
            if item is None:
                urls[key] = [1, size_b, when, when]
            else:
                item[0] += 1
                item[1] += size_b
                item[2] = min(item[2], when)
                item[3] = max(item[3], when)
            if len(urls) > 200000:
                _flush(daily, urls, seen_new)
        _flush(daily, urls, seen_new)
    finally:
        text.close()
        raw.close()
    with db.connect() as conn:
        conn.execute(
            "UPDATE crawl_imports SET status='done', finished_at=?, bytes_read=?, lines=?, google_lines=?, verified=?, "
            "not_google_ip=?, unchecked=?, unparsed=? WHERE id=?",
            (now_iso(), size, counts["lines"], counts["google"], counts["verified"], counts["not google ip"],
             counts["not checked"], counts["unparsed"], import_id))


def _worker(jobs: list[tuple[int, str, str | None, str, bool]]) -> None:
    verifier = None
    for import_id, path, host, unit, verify in jobs:
        try:
            if verifier is None or verifier.enabled != verify:
                verifier = Verifier(verify)
            with db.connect() as conn:
                conn.execute("UPDATE crawl_imports SET status='running', started_at=? WHERE id=?", (now_iso(), import_id))
            import_file(import_id, path, host, unit, verifier)
        except Exception as exc:  # record and continue with the next file
            log.exception("log import failed: %s", path)
            with db.connect() as conn:
                conn.execute("UPDATE crawl_imports SET status='failed', finished_at=?, message=? WHERE id=?",
                             (now_iso(), str(exc)[:500], import_id))


def start_import(paths: list[str], host: str | None, unit: str = "auto", verify: bool = True, wait: bool = False) -> dict:
    global _import_thread
    files: list[str] = []
    missing = []
    for p in paths:
        matches = sorted(glob.glob(os.path.expanduser(p)))
        if matches:
            files.extend(m for m in matches if os.path.isfile(m))
        else:
            missing.append(p)
    if not files:
        raise ValueError(f"No log files found for {paths}.")
    queued, skipped = [], []
    jobs = []
    with db.connect() as conn:
        for f in files:
            sig = _signature(f)
            done = conn.execute("SELECT id FROM crawl_imports WHERE signature=? AND status='done'", (sig,)).fetchone()
            if done:
                skipped.append({"path": f, "reason": f"already imported (import {done['id']})"})
                continue
            cur = conn.execute(
                "INSERT INTO crawl_imports (path, signature, host_hint, status, size_bytes, bytes_read, lines, google_lines) "
                "VALUES (?,?,?,?,?,0,0,0)", (f, sig, host, "queued", os.path.getsize(f)))
            jobs.append((cur.lastrowid, f, host, unit, verify))
            queued.append({"import_id": cur.lastrowid, "path": f, "size_mb": round(os.path.getsize(f) / 1048576, 1)})
    if jobs:
        with _import_lock:
            if wait:
                _worker(jobs)
            else:
                previous = _import_thread

                def run():
                    if previous is not None and previous.is_alive():
                        previous.join()
                    _worker(jobs)

                _import_thread = threading.Thread(target=run, name="search-console-log-import", daemon=True)
                _import_thread.start()
    return {"queued": queued, "skipped": skipped, "not_found": missing,
            "next": "Imports run in the background; call import_access_logs with no paths to see progress, then crawl_stats."}


def import_status(limit: int = 20) -> dict:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM crawl_imports ORDER BY id DESC LIMIT ?", (limit,))]
    for r in rows:
        r.pop("signature", None)
        if r.get("size_bytes"):
            r["progress_pct"] = round(min(100.0, (r.get("bytes_read") or 0) / r["size_bytes"] * 100), 1)
        g = r.get("google_lines") or 0
        if r.get("status") == "done" and g and (r.get("not_google_ip") or 0) / g > 0.5:
            r["warning"] = ("Most hits claiming to be Google came from non-Google IPs. Either bots are faking Googlebot, "
                            "or the log records a proxy/CDN IP (Varnish, Cloudflare, load balancer) instead of the client IP. "
                            "Fix the log's real-IP setting, or re-import with verify=false.")
    return {"imports": rows}


# ---------------------------------------------------------------- reports
VERIFICATION_FILTERS = {
    "exclude_fake": ("verification <> 'not google ip'", "verified + not checked (fake Googlebots excluded)"),
    "verified_only": ("verification = 'verified'", "verified Google IPs only"),
    "all": ("1=1", "everything claiming to be Google"),
    "fake_only": ("verification = 'not google ip'", "claims Google user agent, IP is not Google's"),
}


def stats(host: str | None = None, start_date: str | None = None, end_date: str | None = None,
          verification: str = "exclude_fake", bot_types: list[str] | None = None, examples: int = 5,
          top_urls: int = 0, response: str | None = None) -> dict:
    if verification not in VERIFICATION_FILTERS:
        raise ValueError(f"verification must be one of {list(VERIFICATION_FILTERS)}")
    where = [VERIFICATION_FILTERS[verification][0]]
    args: list = []
    if host and host != "all":
        where.append("host = ?")
        args.append(host.lower())
    if start_date:
        where.append("pt_date >= ?")
        args.append(start_date)
    if end_date:
        where.append("pt_date <= ?")
        args.append(end_date)
    if bot_types:
        where.append(f"bot_type IN ({','.join('?' * len(bot_types))})")
        args.extend(bot_types)
    clause = " AND ".join(where)
    with db.connect() as conn:
        total = dict(conn.execute(
            f"SELECT SUM(requests) AS requests, SUM(bytes) AS bytes, SUM(rt_sum_ms) AS rt, SUM(rt_count) AS rtn, "
            f"MIN(pt_date) AS first_day, MAX(pt_date) AS last_day FROM crawl_daily WHERE {clause}", args).fetchone())
        if not total["requests"]:
            return {"host": host or "all", "requests": 0,
                    "note": "No crawl data for these filters. Import logs with import_access_logs first."}
        daily = [dict(r) for r in conn.execute(
            f"SELECT pt_date AS date, SUM(requests) AS requests, SUM(bytes) AS bytes, "
            f"ROUND(SUM(rt_sum_ms)/NULLIF(SUM(rt_count),0),1) AS avg_response_ms, "
            f"SUM(CASE WHEN response='Server error (5xx)' THEN requests ELSE 0 END) AS server_errors "
            f"FROM crawl_daily WHERE {clause} GROUP BY pt_date ORDER BY pt_date", args)]

        def breakdown(column):
            rows = [dict(r) for r in conn.execute(
                f"SELECT {column} AS value, SUM(requests) AS requests, "
                f"ROUND(SUM(rt_sum_ms)/NULLIF(SUM(rt_count),0),1) AS avg_response_ms "
                f"FROM crawl_daily WHERE {clause} GROUP BY {column} ORDER BY requests DESC", args)]
            for r in rows:
                r["pct"] = round(r["requests"] / total["requests"] * 100, 2)
            return rows

        result = {
            "host": host or "all", "verification_filter": VERIFICATION_FILTERS[verification][1],
            "first_day": total["first_day"], "last_day": total["last_day"],
            "total_crawl_requests": total["requests"], "total_download_bytes": total["bytes"],
            "avg_response_ms": round(total["rt"] / total["rtn"], 1) if total["rtn"] else None,
            "by_response": breakdown("response"), "by_file_type": breakdown("file_type"),
            "by_googlebot_type": breakdown("bot_type"), "by_purpose": breakdown("purpose"),
            "by_host": breakdown("host"), "by_verification": breakdown("verification"),
            "daily": daily,
        }
        robots = [dict(r) for r in conn.execute(
            f"SELECT pt_date AS date, response, SUM(requests) AS fetches FROM crawl_daily WHERE {clause} "
            f"AND file_type='robots.txt' GROUP BY pt_date, response ORDER BY pt_date", args)]
        result["host_status_from_logs"] = {
            "robots_txt_fetches": robots,
            "server_error_days": [d for d in daily if d["server_errors"]],
            "note": "Search Console's host status also covers DNS and connection failures that never reach your server, "
                    "so logs can't show those.",
        }
        if examples:
            url_where = [VERIFICATION_FILTERS[verification][0]]
            url_args: list = []
            if host and host != "all":
                url_where.append("host = ?")
                url_args.append(host.lower())
            if bot_types:
                url_where.append(f"bot_type IN ({','.join('?' * len(bot_types))})")
                url_args.extend(bot_types)
            uc = " AND ".join(url_where)
            ex = {}
            for row in result["by_response"]:
                ex[row["value"]] = [dict(r) for r in conn.execute(
                    f"SELECT host, url, SUM(hits) AS hits, MAX(last_seen) AS last_seen FROM crawl_urls "
                    f"WHERE {uc} AND response=? GROUP BY host, url ORDER BY hits DESC LIMIT ?",
                    url_args + [row["value"], examples])]
            result["example_urls_by_response"] = ex
            if top_urls:
                rc = uc + (" AND response = ?" if response else "")
                ra = url_args + ([response] if response else [])
                result["top_crawled_urls"] = [dict(r) for r in conn.execute(
                    f"SELECT host, url, response, SUM(hits) AS hits, SUM(bytes) AS bytes, MIN(first_seen) AS first_seen, "
                    f"MAX(last_seen) AS last_seen FROM crawl_urls WHERE {rc} GROUP BY host, url, response "
                    f"ORDER BY hits DESC LIMIT ?", ra + [top_urls])]
    result["notes"] = [
        "Purpose: Discovery = first time this URL appears in your imported logs, else Refresh (approximation of Search Console's purpose).",
        "Example and top URLs cover all imported days, not only the date filter.",
        "Response time needs a timing field in the log format (e.g. nginx $request_time or Apache %D).",
    ]
    return result
