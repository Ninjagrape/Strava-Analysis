#!/usr/bin/env python3
"""
localtime.py
Timezone localization for Strava activity dates, shared across the pipeline.

Strava's bulk-export "Activity Date" (and the FIT start time) are in UTC, so a
morning run done in a UTC-ahead timezone otherwise lands on the previous calendar
day. We resolve each run's own timezone from its first GPS coordinate, which also
stays correct across DST changes and runs done while travelling.
"""

import json
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

_tz_finder = None


def _get_tz_finder():
    global _tz_finder
    if _tz_finder is None:
        try:
            from timezonefinder import TimezoneFinder
            _tz_finder = TimezoneFinder()
        except ImportError:
            _tz_finder = False  # sentinel: library unavailable, skip conversion
    return _tz_finder


@lru_cache(maxsize=4096)
def _zone_for(lat_round: float, lon_round: float):
    finder = _get_tz_finder()
    if not finder:
        return None
    name = finder.timezone_at(lat=lat_round, lng=lon_round)
    return ZoneInfo(name) if name else None


def _first_coord(row: dict):
    try:
        pts = json.loads(row.get("fit_gps_polyline") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if pts and isinstance(pts[0], (list, tuple)) and len(pts[0]) >= 2:
        return float(pts[0][0]), float(pts[0][1])
    return None


def parse_date(row: dict):
    try:
        dt = datetime.strptime(row.get("Activity Date", ""), "%b %d, %Y, %I:%M:%S %p")
    except ValueError:
        return None
    coord = _first_coord(row)
    if not coord:
        return dt  # no GPS to localize against, leave as recorded (UTC)
    zone = _zone_for(round(coord[0], 2), round(coord[1], 2))
    if zone is None:
        return dt
    local = dt.replace(tzinfo=timezone.utc).astimezone(zone)
    return local.replace(tzinfo=None)
