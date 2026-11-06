"""Core Web Vitals from the Chrome UX Report (CrUX) API.

Search Console's Core Web Vitals report uses CrUX field data, but Search Console has no API for it.
CrUX needs an API key (CRUX_API_KEY) from a Cloud project with the Chrome UX Report API enabled.
"""

from __future__ import annotations

import os

import requests

ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:"
# Official "good" / "poor" thresholds at the 75th percentile.
THRESHOLDS = {
    "largest_contentful_paint": (2500, 4000, "ms"),
    "interaction_to_next_paint": (200, 500, "ms"),
    "cumulative_layout_shift": (0.1, 0.25, ""),
    "first_contentful_paint": (1800, 3000, "ms"),
    "experimental_time_to_first_byte": (800, 1800, "ms"),
}
CORE = ("largest_contentful_paint", "interaction_to_next_paint", "cumulative_layout_shift")


def enabled() -> bool:
    return bool(os.environ.get("CRUX_API_KEY"))


def _rate(metric: str, p75) -> str | None:
    if p75 is None or metric not in THRESHOLDS:
        return None
    good, poor, _ = THRESHOLDS[metric]
    value = float(p75)
    return "good" if value <= good else "poor" if value > poor else "needs improvement"


def _date(d: dict | None) -> str | None:
    if not d:
        return None
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def query(target: str, form_factor: str = "PHONE", history: bool = False) -> dict:
    key = os.environ.get("CRUX_API_KEY")
    if not key:
        raise ValueError("Core Web Vitals needs CRUX_API_KEY in the server config (Chrome UX Report API key).")
    body: dict = {}
    if target.rstrip("/").count("/") <= 2:
        body["origin"] = target.rstrip("/")
    else:
        body["url"] = target
    if form_factor and form_factor != "ALL":
        body["formFactor"] = form_factor
    resp = requests.post(ENDPOINT + ("queryHistoryRecord" if history else "queryRecord"), params={"key": key},
                         json=body, timeout=30)
    if resp.status_code == 404:
        return {"target": target, "form_factor": form_factor, "data": None,
                "note": "CrUX has no data for this origin/URL (not enough Chrome traffic). Try the origin instead of a URL."}
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", resp.text[:300])
        except ValueError:
            message = resp.text[:300]
        raise ValueError(f"CrUX API error {resp.status_code}: {message}")
    record = resp.json().get("record", {})
    metrics = record.get("metrics", {})
    out = {"target": target, "level": "origin" if "origin" in body else "url", "form_factor": form_factor}
    if not history:
        period = record.get("collectionPeriod", {})
        out["collection_period"] = [_date(period.get("firstDate")), _date(period.get("lastDate"))]
        table = {}
        for name, data in metrics.items():
            p75 = data.get("percentiles", {}).get("p75")
            hist = data.get("histogram", [])
            table[name] = {"p75": p75, "unit": THRESHOLDS.get(name, (0, 0, ""))[2], "rating": _rate(name, p75),
                           "good_pct": round(hist[0]["density"] * 100, 1) if len(hist) == 3 else None,
                           "poor_pct": round(hist[2]["density"] * 100, 1) if len(hist) == 3 else None}
        out["metrics"] = table
        rated = [table[m]["rating"] for m in CORE if m in table]
        out["core_web_vitals_pass"] = (all(r == "good" for r in rated) if len(rated) == 3 else None)
        return out
    periods = [[_date(p.get("firstDate")), _date(p.get("lastDate"))] for p in record.get("collectionPeriods", [])]
    series = {}
    for name, data in metrics.items():
        p75s = data.get("percentilesTimeseries", {}).get("p75s", [])
        series[name] = [{"period_end": periods[i][1] if i < len(periods) else None, "p75": v, "rating": _rate(name, v)}
                        for i, v in enumerate(p75s)]
    out["periods"] = len(periods)
    out["history"] = series
    return out
