"""Shared helpers: data paths, Pacific-time dates, compact JSON, CSV export."""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

# Search Console reports dates in Pacific Time, and daily API quotas reset on PT days.
PT = ZoneInfo("America/Los_Angeles")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return now_utc().replace(microsecond=0).isoformat()


def pt_today() -> dt.date:
    return dt.datetime.now(PT).date()


def pt_date_of(ts: dt.datetime) -> str:
    return ts.astimezone(PT).date().isoformat()


def next_pt_midnight_utc() -> dt.datetime:
    now_pt = dt.datetime.now(PT)
    tomorrow = (now_pt + dt.timedelta(days=1)).date()
    midnight_pt = dt.datetime.combine(tomorrow, dt.time(0, 5), tzinfo=PT)
    return midnight_pt.astimezone(dt.timezone.utc)


def parse_iso(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        value = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def parse_day(value: str, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}") from None


def resolve_range(start_date: str | None, end_date: str | None, days: int | None,
                  default_end: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Return (start, end). Defaults to the last `days` days ending `default_end` (else yesterday, PT)."""
    if end_date:
        end = parse_day(end_date, "end_date")
    else:
        end = default_end or pt_today() - dt.timedelta(days=1)
    if start_date:
        start = parse_day(start_date, "start_date")
    else:
        span = days if days and days > 0 else 28
        start = end - dt.timedelta(days=span - 1)
    if start > end:
        raise ValueError("start_date is after end_date")
    return start, end


def previous_range(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    length = (end - start).days + 1
    prev_end = start - dt.timedelta(days=1)
    return prev_end - dt.timedelta(days=length - 1), prev_end


def enc(value: str) -> str:
    """Percent-encode a property or sitemap URL for use inside an API path."""
    return quote(value, safe="")


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "search-console-mcp"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "search-console-mcp"


def data_dir() -> Path:
    path = Path(os.environ.get("SEARCH_CONSOLE_MCP_DATA_DIR") or _default_data_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_dir() -> Path:
    path = Path(os.environ.get("SEARCH_CONSOLE_MCP_EXPORT_DIR") or Path.home() / "Documents" / "search-console-mcp-exports")
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80] or "export"


def write_csv(stem: str, columns: list[str], rows: list[list]) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = export_dir() / f"{safe_name(stem)}-{stamp}.csv"
    # utf-8-sig so Excel on Windows opens non-ASCII queries correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return str(path)


def out(obj) -> str:
    """Compact JSON for tool output (keeps token use down on large tables)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def r4(value) -> float | None:
    return None if value is None else round(float(value), 4)


def r2(value) -> float | None:
    return None if value is None else round(float(value), 2)
