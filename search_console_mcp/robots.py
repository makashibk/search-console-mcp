"""robots.txt: fetch like Googlebot, parse and match with Google's documented rules, keep history.

Matching follows Google's robots.txt documentation and RFC 9309:
- the crawler obeys one group: the most specific matching user-agent; groups with the same
  user-agent are merged; '*' is the fallback (AdsBot crawlers ignore '*');
- the longest matching rule path wins; on a tie, allow wins;
- '*' matches any sequence of characters, '$' anchors the end;
- content past 500 KiB is ignored; 4xx (except 429) means no restrictions;
  5xx, 429 or unreachable means Google treats the site as fully disallowed for now.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from . import __version__, db
from .util import now_iso

MAX_BYTES = 500 * 1024
MAX_REDIRECTS = 5
UA = f"Mozilla/5.0 (compatible; search-console-mcp/{__version__}; robots.txt monitor)"

# Fallback chains: a crawler obeys the first token that has a group, then '*'.
CRAWLER_TOKENS = {
    "googlebot": ["googlebot"],
    "googlebot-image": ["googlebot-image", "googlebot"],
    "googlebot-news": ["googlebot-news", "googlebot"],
    "googlebot-video": ["googlebot-video", "googlebot"],
    "storebot-google": ["storebot-google", "googlebot"],
    "google-inspectiontool": ["google-inspectiontool", "googlebot"],
    "googleother": ["googleother"],
    "google-extended": ["google-extended"],
    "adsbot-google": ["adsbot-google"],
    "adsbot-google-mobile": ["adsbot-google-mobile", "adsbot-google"],
    "mediapartners-google": ["mediapartners-google"],
}
NO_STAR_FALLBACK = {"adsbot-google", "adsbot-google-mobile"}
KNOWN_FIELDS = {"user-agent", "allow", "disallow", "sitemap"}
IGNORED_BY_GOOGLE = {"crawl-delay", "noindex", "host", "clean-param", "request-rate", "visit-time"}


def robots_url_for(value: str) -> tuple[str, str]:
    """Return (host, robots_url) for a host, URL, or sc-domain property string."""
    value = value.strip()
    if value.startswith("sc-domain:"):
        value = value.split(":", 1)[1]
    if "://" not in value:
        value = "https://" + value.strip("/")
    parts = urlsplit(value)
    scheme = parts.scheme or "https"
    netloc = parts.netloc.lower()
    return netloc, urlunsplit((scheme, netloc, "/robots.txt", "", ""))


def fetch(robots_url: str) -> dict:
    chain = []
    url = robots_url
    started = time.monotonic()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=20, allow_redirects=False, stream=True)
            body = resp.raw.read(MAX_BYTES * 4 + 1, decode_content=True) if resp.status_code < 300 else b""
            resp.close()
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                target = requests.compat.urljoin(url, resp.headers.get("Location", ""))
                chain.append({"url": url, "status": resp.status_code, "location": target})
                url = target
                continue
            return {
                "final_url": url, "status": resp.status_code, "redirects": chain, "content": body,
                "content_type": resp.headers.get("Content-Type", ""), "fetch_ms": int((time.monotonic() - started) * 1000),
                "error": None,
            }
        return {"final_url": url, "status": None, "redirects": chain, "content": b"",
                "fetch_ms": int((time.monotonic() - started) * 1000),
                "error": f"More than {MAX_REDIRECTS} redirects; Google treats this as robots.txt not found (4xx)."}
    except requests.RequestException as exc:
        return {"final_url": url, "status": None, "redirects": chain, "content": b"",
                "fetch_ms": int((time.monotonic() - started) * 1000), "error": f"Fetch failed: {exc}"}


def interpretation(status: int | None, error: str | None) -> str:
    if status is not None and 200 <= status < 300:
        return "parsed: rules apply"
    if error and "redirects" in error:
        return "treated as 404: no crawl restrictions"
    if status == 429 or (status is not None and status >= 500) or status is None:
        return "unreachable (5xx/429/network): Google stops crawling the site for now; after a long outage it may use the last cached copy"
    if 400 <= status < 500:
        return "4xx: Google treats this as no robots.txt, everything may be crawled"
    return f"unexpected status {status}"


def decode(content: bytes) -> str:
    text = content[:MAX_BYTES].decode("utf-8", errors="replace")
    return text[1:] if text.startswith("\ufeff") else text


def parse(text: str) -> dict:
    groups: list[dict] = []
    sitemaps: list[dict] = []
    warnings: list[dict] = []
    current = None
    last_was_agent = False
    for number, raw in enumerate(re.split(r"\r\n|\r|\n", text), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            warnings.append({"line": number, "text": raw.strip()[:200], "issue": "no ':' separator, line ignored"})
            continue
        field, value = line.split(":", 1)
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if current is None or not last_was_agent:
                current = {"agents": [], "rules": [], "line": number}
                groups.append(current)
            current["agents"].append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if field == "sitemap":
            sitemaps.append({"line": number, "url": value})
            if not re.match(r"^https?://", value, re.I):
                warnings.append({"line": number, "text": value[:200], "issue": "sitemap URL must be absolute"})
            continue
        if field in ("allow", "disallow"):
            if current is None:
                warnings.append({"line": number, "text": raw.strip()[:200], "issue": "rule before any user-agent, ignored"})
                continue
            if value and not value.startswith(("/", "*")):
                warnings.append({"line": number, "text": raw.strip()[:200], "issue": "path should start with / or *"})
            current["rules"].append({"type": field, "path": value, "line": number})
            continue
        if field in IGNORED_BY_GOOGLE:
            warnings.append({"line": number, "text": raw.strip()[:200], "issue": f"'{field}' is ignored by Google"})
        else:
            warnings.append({"line": number, "text": raw.strip()[:200], "issue": f"unknown directive '{field}'"})
    return {"groups": groups, "sitemaps": sitemaps, "warnings": warnings}


def tokens_for(user_agent: str) -> list[str]:
    key = user_agent.strip().lower()
    return CRAWLER_TOKENS.get(key, [key])


def select_rules(parsed: dict, user_agent: str = "Googlebot") -> tuple[list[dict], str]:
    """Merged rules of the group the crawler obeys, and which user-agent line it matched."""
    key = user_agent.strip().lower()
    for token in tokens_for(user_agent):
        matched = [g for g in parsed["groups"] if token in g["agents"]]
        if matched:
            return [r for g in matched for r in g["rules"]], token
    if key not in NO_STAR_FALLBACK:
        matched = [g for g in parsed["groups"] if "*" in g["agents"]]
        if matched:
            return [r for g in matched for r in g["rules"]], "*"
    return [], "(no matching group: everything allowed)"


def _pattern(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    core = path[:-1] if anchored else path
    body = "".join(".*" if ch == "*" else re.escape(ch) for ch in core)
    return re.compile(body + ("$" if anchored else ""))


def evaluate(rules: list[dict], url_or_path: str) -> dict:
    parts = urlsplit(url_or_path)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "") if "://" in url_or_path else url_or_path
    if target == "/robots.txt":
        return {"allowed": True, "rule": None, "reason": "/robots.txt is always allowed"}
    best = None
    for rule in rules:
        if not rule["path"]:
            continue  # empty Allow/Disallow means no rule
        if _pattern(rule["path"]).match(target):
            if (best is None or len(rule["path"]) > len(best["path"])
                    or (len(rule["path"]) == len(best["path"]) and rule["type"] == "allow")):
                best = rule
    if best is None:
        return {"allowed": True, "rule": None, "reason": "no rule matches"}
    return {"allowed": best["type"] == "allow", "rule": best,
            "reason": "longest matching rule wins (allow wins ties)"}


# ---------------------------------------------------------------- monitoring / history
def check(value: str) -> dict:
    host, url = robots_url_for(value)
    result = fetch(url)
    content = result["content"] or b""
    size = len(content)
    digest = hashlib.sha256(content).hexdigest() if result["status"] and 200 <= result["status"] < 300 else None
    with db.connect() as conn:
        prev = conn.execute(
            "SELECT id, sha256, checked_at, status_code FROM robots_checks WHERE host=? ORDER BY id DESC LIMIT 1",
            (host,)).fetchone()
        changed = int(prev is None or prev["sha256"] != digest or prev["status_code"] != result["status"])
        if digest:
            conn.execute("INSERT OR IGNORE INTO robots_contents (sha256, content, first_seen) VALUES (?,?,?)",
                         (digest, decode(content), now_iso()))
        cur = conn.execute(
            "INSERT INTO robots_checks (host, url, checked_at, status_code, final_url, redirects, size_bytes, sha256, fetch_ms, error, changed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (host, url, now_iso(), result["status"], result["final_url"], json.dumps(result["redirects"]) if result["redirects"] else None,
             size, digest, result["fetch_ms"], result["error"], changed))
        check_id = cur.lastrowid
        previous_text = None
        if prev is not None and prev["sha256"] and prev["sha256"] != digest:
            row = conn.execute("SELECT content FROM robots_contents WHERE sha256=?", (prev["sha256"],)).fetchone()
            previous_text = row["content"] if row else None
    text = decode(content) if digest else ""
    parsed = parse(text) if digest else {"groups": [], "sitemaps": [], "warnings": []}
    googlebot_rules, matched = select_rules(parsed, "Googlebot")
    report = {
        "check_id": check_id,
        "host": host,
        "robots_url": url,
        "final_url": result["final_url"],
        "status": result["status"],
        "google_interpretation": interpretation(result["status"], result["error"]),
        "error": result["error"],
        "redirects": result["redirects"],
        "size_bytes": size,
        "over_500kib_limit": size > MAX_BYTES,
        "fetch_ms": result["fetch_ms"],
        "sha256": digest,
        "changed_since_last_check": bool(changed) if prev is not None else None,
        "previous_check": dict(prev) if prev is not None else None,
        "groups": [{"user_agents": g["agents"], "rules": len(g["rules"]), "line": g["line"]} for g in parsed["groups"]],
        "googlebot_obeys_group": matched,
        "googlebot_rules": [{"type": r["type"], "path": r["path"], "line": r["line"]} for r in googlebot_rules][:200],
        "sitemaps": [s["url"] for s in parsed["sitemaps"]],
        "warnings": parsed["warnings"][:100],
    }
    if previous_text is not None:
        report["diff_vs_previous"] = diff_text(previous_text, text)
    return report


def diff_text(old: str, new: str, max_lines: int = 300) -> list[str]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "previous", "current", lineterm="", n=1))
    return lines[:max_lines]


def history(value: str, limit: int = 50) -> dict:
    host, _ = robots_url_for(value)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, checked_at, status_code, size_bytes, sha256, fetch_ms, error, changed, final_url "
            "FROM robots_checks WHERE host=? ORDER BY id DESC LIMIT ?", (host, limit)).fetchall()
        versions = conn.execute(
            "SELECT c.sha256, MIN(c.checked_at) AS first_seen, MAX(c.checked_at) AS last_seen, COUNT(*) AS checks, "
            "MAX(c.size_bytes) AS size_bytes FROM robots_checks c WHERE c.host=? AND c.sha256 IS NOT NULL "
            "GROUP BY c.sha256 ORDER BY first_seen DESC", (host,)).fetchall()
    return {"host": host, "checks": [dict(r) for r in rows], "versions": [dict(v) for v in versions]}


def content_for(check_id: int | None, value: str | None) -> tuple[str, dict]:
    with db.connect() as conn:
        if check_id is not None:
            row = conn.execute("SELECT * FROM robots_checks WHERE id=?", (check_id,)).fetchone()
        else:
            host, _ = robots_url_for(value or "")
            row = conn.execute("SELECT * FROM robots_checks WHERE host=? AND sha256 IS NOT NULL ORDER BY id DESC LIMIT 1",
                               (host,)).fetchone()
        if row is None or not row["sha256"]:
            return "", dict(row) if row else {}
        content = conn.execute("SELECT content FROM robots_contents WHERE sha256=?", (row["sha256"],)).fetchone()
    return (content["content"] if content else ""), dict(row)


def diff_checks(old_id: int, new_id: int) -> dict:
    old, old_row = content_for(old_id, None)
    new, new_row = content_for(new_id, None)
    if not old_row or not new_row:
        raise ValueError("Unknown check id. Use robots_txt_history to list check ids.")
    return {"from": old_row.get("checked_at"), "to": new_row.get("checked_at"), "diff": diff_text(old, new)}
