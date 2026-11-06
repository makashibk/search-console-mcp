"""Checks on the live site: redirects, HTTPS/TLS, DNS, images, PageSpeed. Public web data only."""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

from . import __version__

UA = f"Mozilla/5.0 (compatible; search-console-mcp/{__version__}; site check)"
REDIRECT_CODES = (301, 302, 303, 307, 308)


# ---------------------------------------------------------------- redirects
def chain(url: str, max_hops: int = 10, timeout: int = 20) -> dict:
    hops, seen, current = [], set(), url
    for _ in range(max_hops + 1):
        seen.add(current)
        t0 = time.monotonic()
        try:
            resp = requests.get(current, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=False, stream=True)
        except requests.RequestException as exc:
            hops.append({"url": current, "error": str(exc)[:200]})
            return {"hops": hops, "final_url": None, "final_status": None, "error": "request failed"}
        status, location = resp.status_code, resp.headers.get("Location")
        resp.close()
        hop = {"url": current, "status": status, "ms": int((time.monotonic() - t0) * 1000)}
        if status in REDIRECT_CODES and location:
            nxt = urljoin(current, location)
            hop["location"] = nxt
            hops.append(hop)
            if nxt in seen:
                return {"hops": hops, "final_url": None, "final_status": None, "error": "redirect loop"}
            current = nxt
            continue
        hops.append(hop)
        return {"hops": hops, "final_url": current, "final_status": status, "error": None}
    return {"hops": hops, "final_url": None, "final_status": None, "error": f"more than {max_hops} redirects"}


def _norm(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{(p.hostname or '').lower()}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def check_redirects(urls: list[str] | None = None, pairs: list[dict] | None = None, concurrency: int = 8) -> dict:
    jobs = [(u, None) for u in (urls or [])] + [(p.get("from"), p.get("to")) for p in (pairs or []) if p.get("from")]
    if not jobs:
        raise ValueError("Pass urls or pairs [{from, to}].")
    if len(jobs) > 500:
        raise ValueError("At most 500 URLs per call.")

    def run(job):
        source, expected = job
        res = chain(source)
        hops = res["hops"]
        redirects = sum(1 for h in hops if h.get("status") in REDIRECT_CODES)
        issues = []
        if res["error"]:
            issues.append(res["error"])
        if redirects > 1:
            issues.append(f"chain of {redirects} redirects (should be 1)")
        if any(h.get("status") in (302, 303, 307) for h in hops):
            issues.append("temporary redirect used (302/303/307); use 301/308 for permanent moves")
        if any(h["url"].startswith("https://") and h.get("location", "").startswith("http://") for h in hops):
            issues.append("redirects from https to http")
        if res["final_status"] and res["final_status"] != 200:
            issues.append(f"final status {res['final_status']}")
        item = {"url": source, "final_url": res["final_url"], "final_status": res["final_status"],
                "redirects": redirects, "chain": hops, "issues": issues}
        if expected:
            item["expected"] = expected
            item["matches_expected"] = bool(res["final_url"]) and _norm(res["final_url"]) == _norm(expected)
            if not item["matches_expected"]:
                issues.append("does not end at the expected URL")
        return item

    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 16))) as pool:
        results = list(pool.map(run, jobs))
    summary = {"checked": len(results),
               "ok": sum(1 for r in results if not r["issues"]),
               "chains": sum(1 for r in results if r["redirects"] > 1),
               "loops_or_errors": sum(1 for r in results if r["final_status"] is None),
               "non_200_final": sum(1 for r in results if r["final_status"] not in (None, 200))}
    if pairs:
        summary["mapping_mismatches"] = sum(1 for r in results if r.get("matches_expected") is False)
    results.sort(key=lambda r: (not r["issues"], r["url"]))
    return {"summary": summary, "results": results}


# ---------------------------------------------------------------- HTTPS / TLS
class _ResourceParser(HTMLParser):
    ACTIVE = {("script", "src"), ("link", "href"), ("iframe", "src"), ("form", "action"), ("object", "data"), ("embed", "src")}
    PASSIVE = {("img", "src"), ("img", "srcset"), ("source", "src"), ("source", "srcset"), ("video", "src"),
               ("audio", "src"), ("video", "poster")}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.insecure: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link" and "stylesheet" not in a.get("rel", "").lower() and "preload" not in a.get("rel", "").lower():
            return  # plain links (canonical, alternate) are not loaded resources
        for attr, value in a.items():
            if (tag, attr) in self.ACTIVE | self.PASSIVE and "http://" in value:
                kind = "active (blocked by browsers)" if (tag, attr) in self.ACTIVE else "passive"
                self.insecure.append({"tag": tag, "attribute": attr, "url": value[:300], "type": kind})


def tls_info(host: str, port: int = 443) -> dict:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                version = tls.version()
    except ssl.SSLCertVerificationError as exc:
        return {"valid": False, "error": exc.verify_message or str(exc)}
    except (OSError, ssl.SSLError) as exc:
        return {"valid": False, "error": f"TLS connection failed: {exc}"}
    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    issuer = {k: v for part in cert.get("issuer", ()) for k, v in part}
    names = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    return {"valid": True, "tls_version": version, "issuer": issuer.get("organizationName") or issuer.get("commonName"),
            "valid_until": cert["notAfter"], "days_left": int((expires - time.time()) // 86400), "names": names[:50]}


def check_https(host_or_url: str, urls: list[str] | None = None) -> dict:
    value = host_or_url if "://" in host_or_url else "https://" + host_or_url.strip("/")
    host = (urlsplit(value).hostname or "").lower()
    if not host:
        raise ValueError("Pass a host or URL.")
    result: dict = {"host": host, "tls": tls_info(host)}
    issues = []
    tls = result["tls"]
    if not tls["valid"]:
        issues.append(f"certificate problem: {tls['error']}")
    elif tls["days_left"] < 21:
        issues.append(f"certificate expires in {tls['days_left']} days")
    http = chain(f"http://{host}/")
    first = http["hops"][0] if http["hops"] else {}
    result["http_to_https"] = {"first_status": first.get("status"), "first_location": first.get("location"),
                               "final_url": http["final_url"], "redirects": len(http["hops"]) - 1}
    if not (http["final_url"] or "").startswith("https://"):
        issues.append("http:// does not end on https://")
    elif first.get("status") not in (301, 308):
        issues.append(f"http -> https uses {first.get('status')} instead of a permanent 301/308")
    try:
        resp = requests.get(f"https://{host}/", headers={"User-Agent": UA}, timeout=30)
        hsts = resp.headers.get("Strict-Transport-Security")
        result["hsts"] = hsts
        if not hsts:
            issues.append("no Strict-Transport-Security (HSTS) header")
        parser = _ResourceParser()
        if "html" in resp.headers.get("Content-Type", ""):
            parser.feed(resp.text[:3_000_000])
        result["mixed_content"] = parser.insecure[:100]
        if any(i["type"].startswith("active") for i in parser.insecure):
            issues.append("active mixed content (http:// scripts/styles/iframes) on the home page")
        elif parser.insecure:
            issues.append("passive mixed content (http:// images/media) on the home page")
    except requests.RequestException as exc:
        issues.append(f"https home page fetch failed: {exc}")
    if urls:
        checked = []
        for u in urls[:100]:
            c = chain(u)
            checked.append({"url": u, "final_url": c["final_url"], "final_status": c["final_status"],
                            "https_final": bool(c["final_url"] and c["final_url"].startswith("https://"))})
        result["urls"] = checked
        bad = [c for c in checked if not c["https_final"]]
        if bad:
            issues.append(f"{len(bad)} of {len(checked)} URLs don't end on https")
    result["issues"] = issues
    result["note"] = "Search Console's HTTPS report has no API; this checks the same things directly."
    return result


# ---------------------------------------------------------------- DNS + verification
DNS_TYPES = {"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28, "CAA": 257}


def _doh(name: str, rtype: str) -> dict:
    endpoints = [("https://dns.google/resolve", {}),
                 ("https://cloudflare-dns.com/dns-query", {"Accept": "application/dns-json"})]
    last_error = None
    for url, headers in endpoints:
        try:
            resp = requests.get(url, params={"name": name, "type": rtype}, headers={"User-Agent": UA, **headers}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
    raise ValueError(f"DNS lookup failed for {name} {rtype}: {last_error}")


def _txt(value: str) -> str:
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', value)) or value.strip('"')


def check_dns(domain: str, include_www: bool = True) -> dict:
    raw = domain.strip()
    if raw.startswith("sc-domain:"):
        raw = raw.split(":", 1)[1]
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    domain = raw.strip("/").lower()
    if not domain:
        raise ValueError("Pass a domain, URL or sc-domain property.")
    names = [domain] + ([f"www.{domain}"] if include_www and not domain.startswith("www.") else [])
    records: dict = {}
    for name in names:
        records[name] = {}
        for rtype, code in DNS_TYPES.items():
            if name != domain and rtype not in ("A", "AAAA", "CNAME"):
                continue
            data = _doh(name, rtype)
            answers = [a for a in data.get("Answer", []) if a.get("type") == code]
            values = [_txt(a["data"]) if rtype == "TXT" else a["data"] for a in answers]
            if values:
                records[name][rtype] = values
            if data.get("Status") == 3:
                records[name]["status"] = "NXDOMAIN (name does not exist)"
    txt = records.get(domain, {}).get("TXT", [])
    google_tokens = [t.split("=", 1)[1] for t in txt if t.lower().startswith("google-site-verification=")]
    meta_tokens = []
    for base in (f"https://{domain}/", f"https://www.{domain}/") if include_www else (f"https://{domain}/",):
        try:
            html = requests.get(base, headers={"User-Agent": UA}, timeout=20).text[:500_000]
            meta_tokens += [{"page": base, "token": m} for m in re.findall(
                r'<meta[^>]+name=["\']google-site-verification["\'][^>]+content=["\']([^"\']+)', html, flags=re.I)]
        except requests.RequestException:
            pass
    return {"domain": domain, "records": records,
            "google_verification": {"dns_txt_tokens": google_tokens, "html_meta_tags": meta_tokens,
                                    "note": "Search Console can also verify with an HTML file, Google Analytics, "
                                            "Tag Manager or a CNAME; those can't be detected from outside."},
            "resolver": "DNS-over-HTTPS (dns.google, fallback cloudflare-dns.com)"}


# ---------------------------------------------------------------- image audit
_GENERIC = re.compile(r"^(img|image|dsc|dscn|photo|pic|picture|untitled|screenshot|whatsapp|unnamed|placeholder|file)[-_ ]?\d*$|^[a-f0-9]{16,}$", re.I)
MODERN = {"webp", "avif", "svg"}


class _ImageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[dict] = []
        self.og_image = None
        self.jsonld: list[str] = []
        self._in_ld = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "img":
            self.images.append(a)
        elif tag == "meta" and a.get("property", "").lower() == "og:image":
            self.og_image = a.get("content")
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld, self._ld_buf = True, []

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.jsonld.append("".join(self._ld_buf))


def _schema_images(blocks: list[str]) -> list[str]:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "image":
                    for item in (v if isinstance(v, list) else [v]):
                        if isinstance(item, str):
                            found.append(item)
                        elif isinstance(item, dict) and item.get("url"):
                            found.append(item["url"])
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return found


def _file_info(url: str) -> dict:
    try:
        resp = requests.head(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        if resp.status_code >= 400 or not resp.headers.get("Content-Length"):
            # many servers mishandle HEAD (405/501/403); confirm with a GET before calling an image broken
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=20, stream=True)
            resp.close()
        size = resp.headers.get("Content-Length")
        return {"status": resp.status_code, "content_type": resp.headers.get("Content-Type"),
                "bytes": int(size) if size and size.isdigit() else None}
    except requests.RequestException as exc:
        return {"status": None, "error": str(exc)[:150]}


def image_audit(urls: list[str], check_files: bool = True, max_images: int = 60, large_kb: int = 300) -> dict:
    pages = []
    for page_url in urls[:10]:
        try:
            resp = requests.get(page_url, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as exc:
            pages.append({"url": page_url, "error": str(exc)[:200]})
            continue
        parser = _ImageParser()
        parser.feed(resp.text[:5_000_000])
        images = []
        for index, a in enumerate(parser.images[:max_images]):
            src = a.get("src", "")
            lazy_src = a.get("data-src") or a.get("data-original") or a.get("data-lazy")
            real = lazy_src if (not src or src.startswith("data:")) and lazy_src else src
            absolute = urljoin(resp.url, real) if real else None
            name = (urlsplit(absolute).path.rsplit("/", 1)[-1] if absolute else "")
            stem, _, ext = name.rpartition(".")
            issues = []
            if "alt" not in a:
                issues.append("missing alt attribute")
            elif not a["alt"].strip():
                issues.append("empty alt (fine only for decorative images)")
            if not (a.get("width") and a.get("height")):
                issues.append("no width/height attributes (layout shift risk)")
            if a.get("loading", "").lower() == "lazy" and index < 2:
                issues.append("lazy-loaded near the top (can delay LCP)")
            if (not src or src.startswith("data:")) and lazy_src:
                issues.append("JavaScript lazy-load (data-src); Google renders JS but native loading='lazy' is safer")
            if stem and _GENERIC.match(stem):
                issues.append("generic file name")
            if ext and ext.lower() not in MODERN:
                issues.append(f"legacy format .{ext.lower()} (consider WebP/AVIF)")
            images.append({"src": absolute, "alt": a.get("alt"), "width": a.get("width"), "height": a.get("height"),
                           "loading": a.get("loading"), "srcset": bool(a.get("srcset")), "issues": issues})
        if check_files:
            targets = [i for i in images if i["src"] and i["src"].startswith("http")][:30]
            with ThreadPoolExecutor(max_workers=6) as pool:
                for img, info in zip(targets, pool.map(lambda i: _file_info(i["src"]), targets)):
                    img["file"] = info
                    if info.get("status") and info["status"] >= 400:
                        img["issues"].append(f"broken image ({info['status']})")
                    if info.get("bytes") and info["bytes"] > large_kb * 1024:
                        img["issues"].append(f"large file ({info['bytes'] // 1024} KB)")
        counts: dict = {}
        for img in images:
            for issue in img["issues"]:
                key = issue.split(" (")[0]
                counts[key] = counts.get(key, 0) + 1
        pages.append({"url": page_url, "final_url": resp.url, "status": resp.status_code,
                      "images_found": len(parser.images), "images_checked": len(images),
                      "og_image": parser.og_image, "structured_data_images": _schema_images(parser.jsonld)[:20],
                      "issue_counts": counts, "images": images})
    return {"pages": pages}


# ---------------------------------------------------------------- PageSpeed Insights
PSI = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
LAB = ["first-contentful-paint", "largest-contentful-paint", "total-blocking-time", "cumulative-layout-shift",
       "speed-index", "server-response-time"]


def pagespeed(url: str, strategy: str = "MOBILE", categories: list[str] | None = None) -> dict:
    cats = [c.upper().replace("-", "_") for c in (categories or ["PERFORMANCE", "SEO"])]
    params = [("url", url), ("strategy", strategy.upper())] + [("category", c) for c in cats]
    key = os.environ.get("PAGESPEED_API_KEY") or os.environ.get("CRUX_API_KEY")
    if key:
        params.append(("key", key))
    resp = requests.get(PSI, params=params, timeout=150)
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", resp.text[:300])
        except ValueError:
            message = resp.text[:300]
        hint = "" if key else " Without an API key PageSpeed allows very few requests; set PAGESPEED_API_KEY (or CRUX_API_KEY)."
        raise ValueError(f"PageSpeed API error {resp.status_code}: {message}.{hint}")
    data = resp.json()
    lh = data.get("lighthouseResult", {})
    if lh.get("runtimeError", {}).get("code") not in (None, "NO_ERROR"):
        raise ValueError(f"Lighthouse could not test the page: {lh['runtimeError'].get('message')}")
    audits = lh.get("audits", {})
    scores = {k: round(v["score"] * 100) for k, v in lh.get("categories", {}).items() if v.get("score") is not None}
    lab = {a: {"value": audits[a].get("displayValue"), "numeric": audits[a].get("numericValue"),
               "score": audits[a].get("score")} for a in LAB if a in audits}
    opportunities = []
    for audit in audits.values():
        savings = audit.get("metricSavings") or {}
        overall = (audit.get("details") or {}).get("overallSavingsMs")
        if audit.get("score") is not None and audit["score"] < 0.9 and (overall or any(savings.values())):
            opportunities.append({"id": audit.get("id"), "title": audit.get("title"), "display": audit.get("displayValue"),
                                  "savings_ms": overall, "metric_savings": {k: v for k, v in savings.items() if v}})
    opportunities.sort(key=lambda o: -((o["savings_ms"] or 0) + sum(o["metric_savings"].values())))
    failing = {}
    for cat_id, cat in lh.get("categories", {}).items():
        if cat_id == "performance":
            continue
        refs = [r["id"] for r in cat.get("auditRefs", [])]
        bad = [{"id": r, "title": audits[r].get("title")} for r in refs
               if r in audits and audits[r].get("score") is not None and audits[r]["score"] < 1
               and audits[r].get("scoreDisplayMode") not in ("notApplicable", "manual", "informative")]
        if bad:
            failing[cat_id] = bad[:20]

    def field(block):
        if not block or not block.get("metrics"):
            return None
        return {"overall": block.get("overall_category"),
                "metrics": {k: {"p75": v.get("percentile"), "rating": v.get("category")} for k, v in block["metrics"].items()}}

    return {"url": url, "final_url": lh.get("finalUrl"), "strategy": strategy.upper(), "lighthouse": lh.get("lighthouseVersion"),
            "scores": scores, "lab": lab, "opportunities": opportunities[:12], "failing_audits": failing,
            "field_data_url": field(data.get("loadingExperience")),
            "field_data_origin": field(data.get("originLoadingExperience")),
            "note": "Lab data is one simulated run (varies between runs); field data is real Chrome users over 28 days."}
