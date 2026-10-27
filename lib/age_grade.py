#!/usr/bin/env python3
"""
age_grade.py
WMA / Alan Jones age grading against the 2025 road standards in age_grade_2025.json.

Age grade % = open-class standard / (raw time * age factor). Raw time, not
grade-adjusted time, because that is the convention every published age-graded
result uses. A missing or corrupt table degrades to None everywhere so a bad data
file never costs the user the rest of the hub.
"""

import json
from datetime import date
from pathlib import Path

TABLE_PATH = Path(__file__).resolve().parent / "age_grade_2025.json"

# (floor %, label), checked in order
LEVELS = [
    (100.0, "world record"),
    (90.0, "world class"),
    (80.0, "national class"),
    (70.0, "regional class"),
    (60.0, "local class"),
]

_table: dict | None = None
_loaded = False


def _load() -> dict:
    global _table, _loaded
    if not _loaded:
        _loaded = True
        try:
            raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
            _table = raw if isinstance(raw, dict) else None
        except (OSError, json.JSONDecodeError) as e:
            print(f"[age_grade] {TABLE_PATH.name} is unreadable, age grading disabled ({e})")
    return _table or {}


def supported(col: str, sex: str | None) -> bool:
    return isinstance(_load().get(sex or "", {}).get(col), dict)


def age_at_run(age_now: int, run_date: date, latest_run_date: date) -> int:
    """Back-date the configured age to a past run. Anchored to the newest run in the
    data rather than the wall clock so a build is reproducible. An approximation:
    without a birth date it can be off by one around a birthday, which barely moves
    the factor."""
    years_back = (latest_run_date - run_date).days // 365.25
    return age_now - int(years_back)


def age_grade(col: str, time_s: float, age: int, sex: str | None) -> float | None:
    if time_s is None or time_s <= 0:
        return None
    entry = _load().get(sex or "", {}).get(col)
    try:
        factors = entry["factors"]
        idx = min(max(age - int(entry["age_min"]), 0), len(factors) - 1)
        denom = time_s * float(factors[idx])
        return float(entry["open_s"]) / denom * 100.0 if denom > 0 else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def level(pct: float) -> str:
    for floor, label in LEVELS:
        if pct >= floor:
            return label
    return "recreational"
