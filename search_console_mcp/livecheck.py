"""Fetch a URL the way a crawler would and extract the signals that decide indexing."""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

from . import __version__, robots

BROWSER_UA = f"Mozilla/5.0 (compatible; search-console-mcp/{__version__}; live URL check)"
GOOGLEBOT_SMARTPHONE_UA = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36 "
                           "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
MAX_HOPS = 10
MAX_BODY = 3 * 1024 * 1024


class _HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self._in_title = False
        self.meta_robots: list[str] = []
        self.canonicals: list[str] = []
        self.hreflang = 0
        self.meta_refresh = None
        self.h1 = 0
        self.text_chars = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name in ("robots", "googlebot"):
                self.meta_robots.append(f"{name}: {a.get('content', '')}")
            if a.get("http-equiv", "").lower() == "refresh":
                self.meta_refresh = a.get("content")
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel and a.get("href"):
                self.canonicals.append(a["href"])
            if "alternate" in rel and a.get("hreflang"):
                self.hreflang += 1
        elif tag == "h1":
            self.h1 += 1
        elif tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        elif not self._skip:
            self.text_chars += len(data.strip())


def _link_header_canonical(value: str | None) -> list[str]:
    if not value:
        return []
    return re.findall(r'<([^>]+)>\s*;\s*rel="?canonical"?', value, flags=re.I)


def fetch(url: str, user_agent: str = "default") -> dict:
    ua = GOOGLEBOT_SMARTPHONE_UA if user_agent == "googlebot_smartphone" else BROWSER_UA
    hops = []
    current = url
    started = time.monotonic()
    try:
        for _ in range(MAX_HOPS + 1):
            t0 = time.monotonic()
            resp = requests.get(current, headers={"User-Agent": ua, "Accept": "text/html,*/*"}, timeout=30,
                                allow_redirects=False, stream=True)
            hop = {"url": current, "status": resp.status_code, "ms": int((time.monotonic() - t0) * 1000)}
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                hop["location"] = urljoin(current, resp.headers["Location"])
                hops.append(hop)
                resp.close()
                if hop["location"] in [h["url"] for h in hops]:
                    return {"hops": hops, "error": "redirect loop", "final": None}
                current = hop["location"]
                continue
            body = resp.raw.read(MAX_BODY, decode_content=True)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            resp.close()
            hops.append(hop)
            return {"hops": hops, "error": None, "final": {"url": current, "status": resp.status_code,
                    "headers": headers, "body": body, "encoding": resp.encoding or "utf-8"},
                    "total_ms": int((time.monotonic() - started) * 1000)}
        return {"hops": hops, "error": f"more than {MAX_HOPS} redirects", "final": None}
    except requests.RequestException as exc:
        return {"hops": hops, "error": f"fetch failed: {exc}", "final": None}


def analyse(url: str, user_agent: str = "default") -> dict:
    fetched = fetch(url, user_agent)
    result = {"requested_url": url, "user_agent": user_agent, "redirect_chain": fetched["hops"],
              "redirect_hops": max(0, len(fetched["hops"]) - 1), "error": fetched["error"]}
    final = fetched.get("final")
    if final:
        headers = final["headers"]
        ctype = headers.get("content-type", "")
        result.update({
            "final_url": final["url"], "status": final["status"], "content_type": ctype,
            "x_robots_tag": headers.get("x-robots-tag"),
            "canonical_from_header": _link_header_canonical(headers.get("link")),
            "response_ms": fetched.get("total_ms"),
        })
        if "html" in ctype.lower() or final["body"][:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            parser = _HeadParser()
            try:
                parser.feed(final["body"].decode(final["encoding"] or "utf-8", errors="replace"))
            except Exception:  # malformed HTML should not break the check
                pass
            result.update({
                "title": (parser.title or "").strip()[:300] or None,
                "meta_robots": parser.meta_robots,
                "canonical_tags": [urljoin(final["url"], c) for c in parser.canonicals],
                "hreflang_alternates": parser.hreflang,
                "meta_refresh": parser.meta_refresh,
                "h1_count": parser.h1,
                "visible_text_chars": parser.text_chars,
            })
    target = result.get("final_url") or url
    host, robots_url = robots.robots_url_for(f"{urlsplit(target).scheme}://{urlsplit(target).netloc}")
    fetched_robots = robots.fetch(robots_url)
    rules_status = robots.interpretation(fetched_robots["status"], fetched_robots["error"])
    if fetched_robots["status"] and 200 <= fetched_robots["status"] < 300:
        parsed = robots.parse(robots.decode(fetched_robots["content"]))
        rules, group = robots.select_rules(parsed, "Googlebot")
        verdict = robots.evaluate(rules, url)
        result["robots_txt"] = {"url": robots_url, "status": fetched_robots["status"], "googlebot_group": group,
                                "requested_url_allowed": verdict["allowed"], "matched_rule": verdict["rule"]}
        if result.get("final_url") and result["final_url"] != url:
            result["robots_txt"]["final_url_allowed"] = robots.evaluate(rules, result["final_url"])["allowed"]
    else:
        result["robots_txt"] = {"url": robots_url, "status": fetched_robots["status"], "interpretation": rules_status}
    return result


def findings(live: dict, inspection: dict | None) -> list[str]:
    notes: list[str] = []
    url = live["requested_url"]
    if live.get("error"):
        notes.append(f"Live fetch problem: {live['error']}.")
    if live.get("redirect_hops", 0) > 1:
        notes.append(f"Redirect chain of {live['redirect_hops']} hops; point links and sitemaps straight at the final URL.")
    status = live.get("status")
    if status and status >= 400:
        notes.append(f"Live status is {status}.")
    robots_meta = " ".join(live.get("meta_robots", []) + [live.get("x_robots_tag") or ""]).lower()
    if "noindex" in robots_meta:
        notes.append("Live page has noindex (meta robots or X-Robots-Tag).")
    canon = (live.get("canonical_tags") or []) + (live.get("canonical_from_header") or [])
    final_url = live.get("final_url") or url
    if len(set(canon)) > 1:
        notes.append(f"Multiple different canonicals declared: {sorted(set(canon))}.")
    if canon and canon[0] != final_url:
        notes.append(f"Canonical points elsewhere: {canon[0]}.")
    if not canon and status == 200:
        notes.append("No canonical declared on the live page.")
    rt = live.get("robots_txt", {})
    if rt.get("requested_url_allowed") is False:
        notes.append(f"robots.txt blocks Googlebot (rule line {rt.get('matched_rule', {}).get('line')}).")
    if status == 200 and live.get("visible_text_chars") is not None and live["visible_text_chars"] < 300:
        notes.append("Very little visible text in the HTML (possible thin page, soft 404, or JS-only content).")
    if inspection:
        if inspection.get("google_canonical") and canon and inspection["google_canonical"] != canon[0]:
            notes.append(f"Google's chosen canonical ({inspection['google_canonical']}) differs from the live canonical ({canon[0]}).")
        if inspection.get("indexing_state") in ("BLOCKED_BY_META_TAG", "BLOCKED_BY_HTTP_HEADER") and "noindex" not in robots_meta:
            notes.append("Google last saw noindex, but the live page no longer has it: recrawl will clear it (Request indexing in the UI).")
        if inspection.get("page_fetch_state") not in (None, "SUCCESSFUL") and status == 200:
            notes.append(f"Google's last fetch was {inspection['page_fetch_state']}, but the live page returns 200 now.")
    if not notes:
        notes.append("No blocking signal found on the live page.")
    return notes
