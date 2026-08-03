#!/usr/bin/env python3
"""
localtime.py
Timezone localization for Strava activity dates, shared across the pipeline.

Strava's bulk-export "Activity Date" (and the FIT start time) are in UTC, so a
morning run done in a UTC-ahead timezone otherwise lands on the previous calendar
day. We resolve each run's own timezone from its first GPS coordinate, which also
stays correct across DST changes and runs done while travelling.

A recording with no GPS at all (a treadmill run logged by a wrist band) has no
coordinate to resolve against. Rather than leave it in UTC, `index_zones` lets a
caller prime the module with every GPS-carrying run in the export, and the
GPS-less run borrows the timezone of the nearest one in time: a treadmill run
happens wherever its owner was that week, and the neighbouring runs are the only
evidence of that in the data.
"""

import json
from bisect import bisect_left
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

_DATE_FMT = "%b %d, %Y, %I:%M:%S %p"

_tz_finder = None
_zone_index: list = []   # sorted [(utc_epoch, ZoneInfo)], primed by index_zones()


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


def index_zones(rows: list) -> None:
    """Prime the fallback used for runs with no GPS, from the rows that have some.

    Call once per build, before any parse_date. Leaving it uncalled is safe: the
    index stays empty and GPS-less runs keep their recorded UTC, which is what
    happened before this existed.
    """
    global _zone_index
    index = []
    for row in rows:
        coord = _first_coord(row)
        if not coord:
            continue
        try:
            dt = datetime.strptime(row.get("Activity Date", ""), _DATE_FMT)
        except (TypeError, ValueError):
            continue
        zone = _zone_for(round(coord[0], 2), round(coord[1], 2))
        if zone is not None:
            index.append((dt.replace(tzinfo=timezone.utc).timestamp(), zone))
    index.sort(key=lambda e: e[0])
    _zone_index = index


def _nearest_indexed_zone(dt: datetime):
    """Timezone of the GPS-carrying run closest in time, or None if none are indexed.

    Ties go to the earlier run so a rebuild stays byte-identical.
    """
    if not _zone_index:
        return None
    target = dt.replace(tzinfo=timezone.utc).timestamp()
    i = bisect_left(_zone_index, (target,))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(_zone_index):
            gap = abs(_zone_index[j][0] - target)
            if best is None or gap < best[0]:
                best = (gap, _zone_index[j][1])
    return best[1] if best else None


def parse_date(row: dict):
    try:
        dt = datetime.strptime(row.get("Activity Date", ""), _DATE_FMT)
    except ValueError:
        return None
    coord = _first_coord(row)
    if coord:
        zone = _zone_for(round(coord[0], 2), round(coord[1], 2))
    else:
        zone = _nearest_indexed_zone(dt)
    if zone is None:
        return dt  # nothing to localize against, leave as recorded (UTC)
    local = dt.replace(tzinfo=timezone.utc).astimezone(zone)
    return local.replace(tzinfo=None)
