"""Batch URL Inspection with quota accounting, local history and an optional background worker."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import api, auth, db, playbook, robots
from .util import next_pt_midnight_utc, now_iso, now_utc, parse_iso, pt_date_of, pt_today

log = logging.getLogger(__name__)

GOOGLE_DAILY_LIMIT = 2000      # URL Inspection quota per property per day
REQUESTS_PER_SECOND = 8.0      # stays under the 600/minute per-property limit


def daily_limit_default() -> int:
    try:
        return max(1, min(GOOGLE_DAILY_LIMIT, int(os.environ.get("SEARCH_CONSOLE_MCP_INSPECT_DAILY_LIMIT", GOOGLE_DAILY_LIMIT))))
    except ValueError:
        return GOOGLE_DAILY_LIMIT


class _RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_limiter = _RateLimiter(REQUESTS_PER_SECOND)
_site_locks: dict[str, threading.Lock] = {}
_site_locks_guard = threading.Lock()


def site_lock(site: str) -> threading.Lock:
    with _site_locks_guard:
        return _site_locks.setdefault(site, threading.Lock())


# ---------------------------------------------------------------- parsing + storage
def parse_result(result: dict) -> dict:
    index = result.get("indexStatusResult", {}) or {}
    rich = result.get("richResultsResult", {}) or {}
    rich_types = []
    issue_count = 0
    for detected in rich.get("detectedItems", []) or []:
        errors = warnings = 0
        messages = []
        items = detected.get("items", []) or []
        for item in items:
            for issue in item.get("issues", []) or []:
                if issue.get("severity") == "ERROR":
                    errors += 1
                else:
                    warnings += 1
                if issue.get("issueMessage") and issue["issueMessage"] not in messages:
                    messages.append(issue["issueMessage"])
        issue_count += errors + warnings
        rich_types.append({"type": detected.get("richResultType"), "items": len(items),
                           "errors": errors, "warnings": warnings, "issues": messages[:10]})
    coverage = index.get("coverageState")
    return {
        "verdict": index.get("verdict"),
        "coverage_state": coverage,
        "is_indexed": playbook.is_indexed(coverage, index.get("verdict")),
        "indexing_state": index.get("indexingState"),
        "page_fetch_state": index.get("pageFetchState"),
        "robots_txt_state": index.get("robotsTxtState"),
        "last_crawl_time": index.get("lastCrawlTime"),
        "crawled_as": index.get("crawledAs"),
        "google_canonical": index.get("googleCanonical"),
        "user_canonical": index.get("userCanonical"),
        "sitemaps": index.get("sitemap", []) or [],
        "referring_urls": index.get("referringUrls", []) or [],
        "rich_verdict": rich.get("verdict"),
        "rich_results": rich_types,
        "rich_issue_count": issue_count,
        "inspection_link": result.get("inspectionResultLink"),
    }


def store(site: str, url: str, parsed: dict | None, raw: dict | None, error: str | None) -> int:
    when = now_utc()
    parsed = parsed or {}
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO inspections (site_url, url, inspected_at, pt_date, verdict, coverage_state, is_indexed, indexing_state, "
            "page_fetch_state, robots_txt_state, last_crawl_time, crawled_as, google_canonical, user_canonical, sitemaps, "
            "referring_urls, rich_verdict, rich_results, rich_issue_count, error, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (site, url, when.replace(microsecond=0).isoformat(), pt_date_of(when), parsed.get("verdict"),
             parsed.get("coverage_state"),
             None if parsed.get("is_indexed") is None else int(parsed["is_indexed"]),
             parsed.get("indexing_state"), parsed.get("page_fetch_state"), parsed.get("robots_txt_state"),
             parsed.get("last_crawl_time"), parsed.get("crawled_as"), parsed.get("google_canonical"),
             parsed.get("user_canonical"), json.dumps(parsed.get("sitemaps", [])),
             json.dumps(parsed.get("referring_urls", [])), parsed.get("rich_verdict"),
             json.dumps(parsed.get("rich_results", [])), parsed.get("rich_issue_count"), error,
             zlib.compress(json.dumps(raw).encode()) if raw else None))
        inspection_id = cur.lastrowid
        conn.execute(
            "INSERT INTO url_queue (site_url, url, source, added_at, last_inspected_at, latest_id) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(site_url, url) DO UPDATE SET last_inspected_at=excluded.last_inspected_at, latest_id=excluded.latest_id",
            (site, url, "inspected", now_iso(), when.replace(microsecond=0).isoformat(), inspection_id))
    return inspection_id


def used_today(site: str) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM inspections WHERE site_url=? AND pt_date=?",
                           (site, pt_today().isoformat())).fetchone()
    return int(row["n"])


def inspect_one(site: str, url: str, language: str = "en-US") -> dict:
    if not api.site_covers(site, url):
        raise ValueError(f"{url} is not inside property {site}.")
    _limiter.wait()
    try:
        raw = api.inspect(site, url, language)
    except api.ApiError as exc:
        if exc.is_quota:
            raise
        store(site, url, None, None, exc.message[:500])
        raise
    parsed = parse_result(raw)
    store(site, url, parsed, raw, None)
    return parsed


# ---------------------------------------------------------------- queue
def queue_urls(site: str, urls: list[str], source: str, detail_for=None) -> dict:
    outside = 0
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM url_queue WHERE site_url=?", (site,)).fetchone()["n"]
        for url in urls:
            url = url.strip()
            if not url:
                continue
            if not api.site_covers(site, url):
                outside += 1
                continue
            detail = detail_for(url) if detail_for else None
            conn.execute(
                "INSERT INTO url_queue (site_url, url, source, source_detail, added_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(site_url, url) DO UPDATE SET "
                "source=CASE WHEN url_queue.source='inspected' THEN excluded.source ELSE url_queue.source END, "
                "source_detail=COALESCE(excluded.source_detail, url_queue.source_detail)",
                (site, url, source, detail, now_iso()))
        total = conn.execute("SELECT COUNT(*) AS n FROM url_queue WHERE site_url=?", (site,)).fetchone()["n"]
    return {"submitted": len(urls), "newly_queued": total - before, "outside_property": outside, "queue_total": total}


def due_urls(site: str, limit: int, stale_days: int) -> list[str]:
    # stale_days=0 means "re-inspect everything", so push the cutoff into the future.
    delta = dt.timedelta(days=stale_days) if stale_days > 0 else -dt.timedelta(days=1)
    cutoff = (now_utc() - delta).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT url FROM url_queue WHERE site_url=? AND (last_inspected_at IS NULL OR last_inspected_at < ?) "
            "ORDER BY last_inspected_at IS NOT NULL, last_inspected_at, added_at LIMIT ?",
            (site, cutoff, limit)).fetchall()
    return [r["url"] for r in rows]


def queue_status(site: str, stale_days: int = 14) -> dict:
    cutoff = (now_utc() - dt.timedelta(days=stale_days)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(last_inspected_at IS NULL) AS never, "
            "SUM(last_inspected_at IS NOT NULL AND last_inspected_at < ?) AS stale FROM url_queue WHERE site_url=?",
            (cutoff, site)).fetchone()
    return {"queue_total": row["total"] or 0, "never_inspected": row["never"] or 0,
            f"older_than_{stale_days}_days": row["stale"] or 0}


# ---------------------------------------------------------------- batch runs
def run_batch(site: str, max_urls: int = 100, time_budget: float = 45.0, stale_days: int = 7,
              concurrency: int = 5, language: str = "en-US", urls: list[str] | None = None,
              daily_limit: int | None = None) -> dict:
    lock = site_lock(site)
    if not lock.acquire(blocking=False):
        return {"site": site, "inspected": 0,
                "stopped": "another inspection run for this property is in progress (background job?)"}
    try:
        limit = daily_limit or daily_limit_default()
        quota_left = max(0, limit - used_today(site))
        if quota_left == 0:
            return {"site": site, "inspected": 0, "quota_used_today": used_today(site),
                    "stopped": f"daily inspection limit reached ({limit}); resets at midnight Pacific Time"}
        cap = min(max_urls, quota_left)
        todo = (urls if urls is not None else due_urls(site, cap, stale_days))[:cap]
        concurrency = max(1, min(concurrency, 10))
        started = time.monotonic()
        states: dict[str, int] = {}
        errors: list[dict] = []
        stopped = None
        done = 0
        pending: dict = {}
        source = iter(todo)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            def submit_next() -> bool:
                url = next(source, None)
                if url is None:
                    return False
                pending[pool.submit(inspect_one, site, url, language)] = url
                return True

            def collect(future) -> None:
                nonlocal done, stopped
                url = pending.pop(future)
                if future.cancelled():
                    return
                try:
                    parsed = future.result()
                    state = parsed.get("coverage_state") or "(no coverage state)"
                    states[state] = states.get(state, 0) + 1
                    done += 1
                except api.ApiError as exc:
                    if exc.is_quota:
                        stopped = "Google quota hit (429); stopped. The daily quota resets at midnight Pacific Time."
                        mark_paused(site, stopped)
                    else:
                        errors.append({"url": url, "error": exc.message[:200]})
                except auth.AuthError as exc:
                    stopped = str(exc)
                except ValueError as exc:
                    errors.append({"url": url, "error": str(exc)[:200]})

            for _ in range(concurrency):
                if not submit_next():
                    break
            while pending:
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    collect(future)
                if stopped is None and time.monotonic() - started > time_budget:
                    stopped = "time budget reached; call again to continue"
                if stopped is not None:
                    for future in list(pending):
                        future.cancel()
                    for future in list(pending):
                        if not future.cancelled():
                            wait([future])
                        collect(future)
                    break
                while len(pending) < concurrency and submit_next():
                    pass

        used = used_today(site)
        return {
            "site": site,
            "inspected": done,
            "error_count": len(errors),
            "errors": errors[:20],
            "coverage_in_this_run": dict(sorted(states.items(), key=lambda kv: -kv[1])),
            "stopped": stopped or "done",
            "seconds": round(time.monotonic() - started, 1),
            "quota_used_today": used,
            "quota_left_today": max(0, limit - used),
            **({} if urls is not None else queue_status(site, stale_days)),
        }
    finally:
        lock.release()


# ---------------------------------------------------------------- background jobs
def mark_paused(site: str, message: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE inspection_jobs SET paused_until=?, last_message=?, updated_at=? WHERE site_url=?",
                     (next_pt_midnight_utc().replace(microsecond=0).isoformat(), message, now_iso(), site))


def set_job(site: str, active: bool, daily_limit: int, stale_days: int) -> dict:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO inspection_jobs (site_url, active, daily_limit, stale_days, created_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(site_url) DO UPDATE SET active=excluded.active, daily_limit=excluded.daily_limit, "
            "stale_days=excluded.stale_days, updated_at=excluded.updated_at, paused_until=NULL",
            (site, int(active), daily_limit, stale_days, now_iso(), now_iso()))
    ensure_worker()
    return job_status(site)


def job_status(site: str | None = None) -> dict | list:
    with db.connect() as conn:
        if site:
            row = conn.execute("SELECT * FROM inspection_jobs WHERE site_url=?", (site,)).fetchone()
            if row is None:
                return {"site": site, "job": None, **queue_status(site), "quota_used_today": used_today(site)}
            job = dict(row)
            return {"site": site, "job": job, **queue_status(site, job["stale_days"]), "quota_used_today": used_today(site)}
        rows = conn.execute("SELECT * FROM inspection_jobs").fetchall()
    return [dict(r) for r in rows]


class _Worker(threading.Thread):
    def __init__(self):
        super().__init__(name="search-console-worker", daemon=True)
        self.stop_event = threading.Event()

    def run(self) -> None:
        time.sleep(10)
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:  # never let the worker die
                log.exception("background worker tick failed")
            self.stop_event.wait(30)

    def tick(self) -> None:
        with db.connect() as conn:
            jobs = [dict(r) for r in conn.execute("SELECT * FROM inspection_jobs WHERE active=1").fetchall()]
            watches = [dict(r) for r in conn.execute("SELECT * FROM robots_watch").fetchall()]
        for job in jobs:
            paused = parse_iso(job.get("paused_until"))
            if paused and now_utc() < paused:
                continue
            if not due_urls(job["site_url"], 1, job["stale_days"]):
                continue
            result = run_batch(job["site_url"], max_urls=60, time_budget=90, stale_days=job["stale_days"],
                               concurrency=4, daily_limit=job["daily_limit"])
            message = f"{now_iso()}: inspected {result.get('inspected', 0)}, {result.get('stopped')}"
            with db.connect() as conn:
                conn.execute("UPDATE inspection_jobs SET last_message=?, updated_at=? WHERE site_url=?",
                             (message, now_iso(), job["site_url"]))
            if result.get("quota_left_today") == 0 and "quota" not in str(result.get("stopped")):
                mark_paused(job["site_url"], "daily limit reached; resumes after midnight Pacific Time")
        for watch in watches:
            with db.connect() as conn:
                last = conn.execute("SELECT checked_at FROM robots_checks WHERE host=? ORDER BY id DESC LIMIT 1",
                                    (watch["host"],)).fetchone()
            last_at = parse_iso(last["checked_at"]) if last else None
            if last_at is None or now_utc() - last_at >= dt.timedelta(hours=watch["interval_hours"]):
                robots.check(watch["host"])


_worker: _Worker | None = None
_worker_lock = threading.Lock()


def ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = _Worker()
            _worker.start()


def start_worker_if_needed() -> None:
    try:
        with db.connect() as conn:
            jobs = conn.execute("SELECT COUNT(*) AS n FROM inspection_jobs WHERE active=1").fetchone()["n"]
            watches = conn.execute("SELECT COUNT(*) AS n FROM robots_watch").fetchone()["n"]
        if jobs or watches:
            ensure_worker()
    except Exception:
        log.exception("could not start background worker")
