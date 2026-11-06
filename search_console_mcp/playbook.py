"""Page indexing reasons as Search Console names them, with source, checks and fixes.

The URL Inspection API returns the same coverage strings the Page indexing report shows.
"""

from __future__ import annotations

import re

_P = {
    "submitted and indexed": {
        "indexed": True, "source": "-",
        "meaning": "Indexed and listed in a submitted sitemap.",
        "check": ["Nothing to fix. Watch clicks/impressions for the page."],
        "fix": [],
    },
    "indexed, not submitted in sitemap": {
        "indexed": True, "source": "-",
        "meaning": "Indexed, but not in any sitemap Google knows for this property.",
        "check": ["Should this URL be indexed at all (parameters, filters, old URLs)?"],
        "fix": ["Add wanted URLs to the XML sitemap.", "If unwanted: canonical to the main URL or noindex, and remove internal links to it."],
    },
    "indexed, though blocked by robots.txt": {
        "indexed": True, "source": "Website",
        "meaning": "Indexed from links even though robots.txt blocks crawling, so Google can't read its content.",
        "check": ["Which robots.txt rule blocks it (robots_txt_test)."],
        "fix": ["To remove it: allow crawling and add noindex; Google must crawl to see noindex.", "To keep it: unblock it in robots.txt."],
    },
    "page indexed without content": {
        "indexed": True, "source": "Website",
        "meaning": "Indexed, but Google couldn't read the content (blocked resources, cloaking, empty render).",
        "check": ["Live URL Inspection screenshot/HTML in Search Console.", "Is the main content rendered by JavaScript that fails?"],
        "fix": ["Serve the content in HTML or fix rendering; make sure CSS/JS aren't blocked."],
    },
    "server error (5xx)": {
        "indexed": False, "source": "Website",
        "meaning": "Google got a 5xx when crawling.",
        "check": ["Server/Varnish/PHP-FPM logs at the last crawl time.", "WAF or rate limiting returning 5xx to bots.", "Does diagnose_url get a 200 now?"],
        "fix": ["Fix the server error or capacity issue, then validate the fix in Search Console."],
    },
    "redirect error": {
        "indexed": False, "source": "Website",
        "meaning": "Redirect loop, chain too long, empty/bad redirect URL, or a redirect URL that's too long.",
        "check": ["Full redirect chain (diagnose_url shows every hop)."],
        "fix": ["Redirect in one hop straight to the final 200 URL.", "Remove loops and update internal links and sitemaps to the final URL."],
    },
    "blocked by robots.txt": {
        "indexed": False, "source": "Website",
        "meaning": "Crawling is blocked by robots.txt.",
        "check": ["Matching rule and line (robots_txt_test).", "Is the block intended (filters, search, cart)?"],
        "fix": ["If the page should rank, remove or narrow the rule.", "If intended, remove the URL from sitemaps."],
    },
    "excluded by 'noindex' tag": {
        "indexed": False, "source": "Website",
        "meaning": "A meta robots or X-Robots-Tag noindex was found.",
        "check": ["Meta robots in HTML and the X-Robots-Tag header (diagnose_url).", "Magento: Design > HTML Head > Default Robots, layered-navigation or SEO extension rules."],
        "fix": ["If the page should rank, remove noindex.", "If intended, remove it from sitemaps."],
    },
    "soft 404": {
        "indexed": False, "source": "Website",
        "meaning": "Returns 200 but looks like an error or empty page (e.g. empty category, out-of-stock template, no results).",
        "check": ["Live page content: empty listing, 'not found' text, thin template."],
        "fix": ["Return a real 404/410 for gone pages, or 301 to a close replacement.", "Otherwise add substantial content."],
    },
    "blocked due to unauthorized request (401)": {
        "indexed": False, "source": "Website",
        "meaning": "Google got 401 (login or HTTP auth).",
        "check": ["Staging auth, customer-only pages, or bot protection on this path."],
        "fix": ["Remove auth for public pages; keep private pages out of sitemaps and internal links."],
    },
    "not found (404)": {
        "indexed": False, "source": "Website",
        "meaning": "Google got 404.",
        "check": ["Was the page removed on purpose? Where does Google find it (referring URLs, sitemaps)?"],
        "fix": ["If it has a replacement, 301 to it.", "If gone, leave the 404 and remove links and sitemap entries."],
    },
    "blocked due to access forbidden (403)": {
        "indexed": False, "source": "Website",
        "meaning": "Google got 403.",
        "check": ["WAF/CDN/bot rules blocking Googlebot (verify Googlebot by reverse DNS, don't block by UA).", "Folder permissions."],
        "fix": ["Allow verified Googlebot; return 200 for public pages."],
    },
    "blocked due to other 4xx issue": {
        "indexed": False, "source": "Website",
        "meaning": "Google got a 4xx other than 401, 403 or 404 (e.g. 410, 429).",
        "check": ["Exact status the server returns (diagnose_url). 429 means rate limiting."],
        "fix": ["Return 200 for wanted pages; relax rate limits for verified Googlebot."],
    },
    "alternate page with proper canonical tag": {
        "indexed": False, "source": "Website",
        "meaning": "This URL canonicalises to another URL and Google agreed. Usually correct.",
        "check": ["Is the canonical target the page you want indexed?"],
        "fix": ["Usually nothing. Keep these URLs out of sitemaps."],
    },
    "duplicate without user-selected canonical": {
        "indexed": False, "source": "Website",
        "meaning": "Google sees a duplicate and you declared no canonical; it chose another URL.",
        "check": ["Which URL Google picked (google_canonical).", "Magento: category-path product URLs, ?___store, sort/limit/filter parameters."],
        "fix": ["Add rel=canonical to the preferred URL; link internally only to the preferred URL; keep variants out of sitemaps."],
    },
    "duplicate, google chose different canonical than user": {
        "indexed": False, "source": "Google systems",
        "meaning": "You declared a canonical, but Google picked another URL.",
        "check": ["Compare user_canonical vs google_canonical.", "Near-identical content, internal links, hreflang and sitemaps pointing at the other URL, redirects."],
        "fix": ["Align every signal (canonical, internal links, sitemap, hreflang, redirects) on one URL.", "Or make the pages genuinely different."],
    },
    "page with redirect": {
        "indexed": False, "source": "Website",
        "meaning": "The URL redirects; the target is what gets indexed. Normal for old URLs.",
        "check": ["Is the redirect intended? Is the URL still in sitemaps or internal links?"],
        "fix": ["Update sitemaps and internal links to the final URL."],
    },
    "crawled - currently not indexed": {
        "indexed": False, "source": "Google systems",
        "meaning": "Google crawled it but chose not to index it for now, usually a quality, duplication or value decision.",
        "check": ["Thin or duplicate content (e.g. near-identical product variants, empty filters).", "Weak internal linking; impressions/clicks history of similar pages."],
        "fix": ["Improve unique content; consolidate near-duplicates with canonicals or 301s.", "Link to it from strong pages; keep it in the sitemap. Re-inspect after changes; resubmitting alone rarely helps."],
    },
    "discovered - currently not indexed": {
        "indexed": False, "source": "Google systems",
        "meaning": "Google knows the URL but hasn't crawled it yet, often crawl budget or server load.",
        "check": ["How many low-value URLs compete for crawl (filters, parameters, sort orders).", "Server response times and errors in your logs.", "Internal link depth to the URL."],
        "fix": ["Cut crawlable low-value URLs (robots.txt for facets, fewer parameter links).", "Speed up responses; link to important pages from category and home pages."],
    },
    "url is unknown to google": {
        "indexed": False, "source": "Google systems",
        "meaning": "Google hasn't discovered this URL.",
        "check": ["Is it in a submitted sitemap and linked internally?"],
        "fix": ["Add it to the sitemap and link to it from relevant pages."],
    },
}

_ALIASES = {
    "url blocked by robots.txt": "blocked by robots.txt",
    "submitted url blocked by robots.txt": "blocked by robots.txt",
    "excluded by noindex tag": "excluded by 'noindex' tag",
    "submitted url marked 'noindex'": "excluded by 'noindex' tag",
}


def normalise(state: str | None) -> str:
    if not state:
        return ""
    text = state.lower().strip()
    text = text.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"[\u2013\u2014]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return _ALIASES.get(text, text)


def lookup(state: str | None) -> dict | None:
    return _P.get(normalise(state))


def is_indexed(state: str | None, verdict: str | None) -> bool | None:
    entry = lookup(state)
    if entry is not None:
        return entry["indexed"]
    if not state:
        return None
    low = normalise(state)
    if "not indexed" in low:
        return False
    if low.startswith("indexed") or low.startswith("submitted and indexed"):
        return True
    return verdict == "PASS" if verdict else None


def source(state: str | None) -> str:
    entry = lookup(state)
    return entry["source"] if entry else "unknown"


def guide(state: str | None = None) -> dict:
    if state:
        entry = lookup(state)
        if not entry:
            return {"reason": state, "known": False, "known_reasons": sorted(_P)}
        return {"reason": state, **entry}
    return {name: {k: v for k, v in entry.items()} for name, entry in _P.items()}
