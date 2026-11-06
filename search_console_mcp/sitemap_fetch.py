"""Download sitemaps (XML, .gz, sitemap indexes, plain text) and extract page URLs."""

from __future__ import annotations

import gzip
import io
import xml.etree.ElementTree as ET
from collections import deque

import requests

from . import __version__

UA = f"Mozilla/5.0 (compatible; search-console-mcp/{__version__}; sitemap reader)"
MAX_SITEMAP_BYTES = 60 * 1024 * 1024  # protocol limit is 50 MB uncompressed


def _download(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=60, stream=True)
    resp.raise_for_status()
    data = resp.raw.read(MAX_SITEMAP_BYTES + 1, decode_content=True)
    resp.close()
    if data[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
            data = handle.read(MAX_SITEMAP_BYTES + 1)
    if len(data) > MAX_SITEMAP_BYTES:
        raise ValueError("sitemap larger than 50 MB uncompressed")
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(data: bytes) -> tuple[str, list[str]]:
    """Return ('index'|'urlset'|'text', locs)."""
    head = data.lstrip()[:200].lower()
    if not head.startswith(b"<"):
        lines = [line.strip() for line in data.decode("utf-8", errors="replace").splitlines()]
        return "text", [line for line in lines if line.startswith(("http://", "https://"))]
    kind = None
    locs: list[str] = []
    for _, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
        name = _local(elem.tag)
        if kind is None and name in ("sitemapindex", "urlset"):
            kind = name
        if name == "loc" and elem.text:
            locs.append(elem.text.strip())
        if name in ("url", "sitemap"):
            elem.clear()
    if kind is None:
        root = ET.fromstring(data)
        kind = _local(root.tag)
    return ("index" if kind == "sitemapindex" else "urlset"), locs


def collect_urls(start_url: str, max_urls: int = 50000, max_sitemaps: int = 500) -> dict:
    """Walk a sitemap or sitemap index. Each URL is returned with the child sitemap it came from."""
    queue = deque([start_url])
    seen_maps: set[str] = set()
    seen_urls: set[str] = set()
    urls: list[tuple[str, str]] = []
    errors: list[dict] = []
    per_sitemap: dict[str, int] = {}
    while queue and len(seen_maps) < max_sitemaps and len(urls) < max_urls:
        current = queue.popleft()
        if current in seen_maps:
            continue
        seen_maps.add(current)
        try:
            kind, locs = parse_sitemap(_download(current))
        except (requests.RequestException, ET.ParseError, ValueError, OSError) as exc:
            errors.append({"sitemap": current, "error": str(exc)[:300]})
            continue
        if kind == "index":
            queue.extend(loc for loc in locs if loc not in seen_maps)
            per_sitemap[current] = 0
            continue
        count = 0
        for loc in locs:
            if loc in seen_urls:
                continue
            seen_urls.add(loc)
            urls.append((loc, current))
            count += 1
            if len(urls) >= max_urls:
                break
        per_sitemap[current] = count
    return {
        "urls": urls,
        "sitemaps_read": len(seen_maps),
        "per_sitemap": per_sitemap,
        "errors": errors,
        "truncated": bool(queue) or len(urls) >= max_urls,
    }
