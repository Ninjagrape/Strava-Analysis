#!/usr/bin/env python3
"""
generate_hub.py
Generates dashboards/TrainingHub.html — a self-contained training hub with:
  - Overview panel: summary stats, fitness indicators, weekly mileage
  - Runs panel: searchable run list with per-run analytics

No external HTML files are required. Imports shared logic from
generate_dashboards and generate_analytics.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROFILE = bool(os.environ.get("STRAVA_PROFILE"))

# Support modules live in lib/; put it on the path so the plain imports below
# resolve whether the hub is run directly or invoked via main.py.
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from generate_dashboards import (
    load_rows, fmt_pace, fmt_time, ga_time, is_interval,
    fmt_pace_from_s_per_km, weekly_mileage, weekly_runs, render_mileage,
    BEST_EFFORTS_CSS, GOAL_CSS,
    body_best_efforts, body_goal_dashboard,
)
from generate_analytics import (
    num, parse_date, session_loads, daily_series, ctl_atl_tsb,
    ANALYTICS_PANEL_CSS, body_analytics, overview_sections,
)
from generate_segments import build_segments, body_segments, SEGMENTS_CSS


# ---------------------------------------------------------------------------
# Best effort column → display metadata
# ---------------------------------------------------------------------------

BEST_EFFORT_DEFS = [
    ("400m",          "best_400m_s",   400.0),
    ("800m",          "best_1/2mi_s",  804.672),
    ("1 km",          "best_1km_s",    1_000.0),
    ("1 mile",        "best_1mi_s",    1_609.344),
    ("2 miles",       "best_2mi_s",    3_218.69),
    ("5K",            "best_5k_s",     5_000.0),
    ("10K",           "best_10k_s",    10_000.0),
    ("15K",           "best_15k_s",    15_000.0),
    ("Half marathon", "best_half_s",   21_097.5),
]


# ---------------------------------------------------------------------------
# Heatmap helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def _interpolate_polyline(poly: list, step_m: float = 12.0) -> list:
    """Insert intermediate lat/lon points so no segment is longer than step_m metres.
    This fills the gaps left by the 250-point GPS simplification so the heatmap
    renders as a continuous line rather than discrete blobs when zoomed in."""
    if len(poly) < 2:
        return list(poly)
    result = [poly[0]]
    for i in range(1, len(poly)):
        lat0, lon0 = poly[i - 1][0], poly[i - 1][1]
        lat1, lon1 = poly[i][0], poly[i][1]
        mid_lat = (lat0 + lat1) / 2
        dlat_m = (lat1 - lat0) * 111_000
        dlon_m = (lon1 - lon0) * 111_000 * math.cos(math.radians(mid_lat))
        dist_m = math.hypot(dlat_m, dlon_m)
        if dist_m > step_m:
            n = int(dist_m / step_m)
            for j in range(1, n + 1):
                frac = j / (n + 1)
                result.append([lat0 + frac * (lat1 - lat0), lon0 + frac * (lon1 - lon0)])
        result.append([lat1, lon1])
    return result


def _heatmap_points(runs: list[dict], max_dist_km: float = 150.0) -> list:
    """Build heatmap points where each run contributes at most one point per ~100 m grid cell.
    This makes density proportional to distinct-run frequency, not raw GPS point count,
    so oval laps don't inflate an area to match a frequently-run local route."""
    centroids = []
    for r in runs:
        poly = r.get("gps_polyline", [])
        if len(poly) >= 5:
            lat_c = sum(p[0] for p in poly) / len(poly)
            lon_c = sum(p[1] for p in poly) / len(poly)
            centroids.append((lat_c, lon_c))

    if not centroids:
        return []

    lats = sorted(c[0] for c in centroids)
    lons = sorted(c[1] for c in centroids)
    med_lat = lats[len(lats) // 2]
    med_lon = lons[len(lons) // 2]

    points = []
    for r in runs:
        poly = r.get("gps_polyline", [])
        if len(poly) < 5:
            continue
        lat_c = sum(p[0] for p in poly) / len(poly)
        lon_c = sum(p[1] for p in poly) / len(poly)
        if _haversine_km(med_lat, med_lon, lat_c, lon_c) > max_dist_km:
            continue
        # Interpolate to fill gaps (250-pt simplification leaves ~20 m spacing).
        dense = _interpolate_polyline(poly, step_m=12.0)
        # Deduplicate: each ~11 m grid cell (4 decimal places) counts once per run.
        # Fine enough for smooth rendering; coarse enough to collapse repeated laps.
        seen: set = set()
        for p in dense:
            key = (round(p[0], 4), round(p[1], 4))
            if key not in seen:
                seen.add(key)
                points.append(p)

    return points


# ---------------------------------------------------------------------------
# Pace zone helpers
# ---------------------------------------------------------------------------

def _compute_threshold(rows: list[dict]) -> float | None:
    """
    Derive threshold speed (m/s) from the best 5K time across all runs.
    5K pace is a reliable proxy for lactate threshold for recreational runners.
    """
    best = None
    for row in rows:
        s = num(row, "best_5k_s")
        if s and s > 0:
            if best is None or s < best:
                best = s
    if best and best > 0:
        return round(5000.0 / best, 4)  # m/s
    return None


THRESHOLD_WINDOW_DAYS = 60   # centered window for contemporaneous fitness


def _build_threshold_curve(rows: list[dict]) -> list[tuple[datetime, float]]:
    """(date, best_5k_s) for every row carrying a 5K effort, sorted by date."""
    efforts = []
    for row in rows:
        dt = parse_date(row)
        s = num(row, "best_5k_s")
        if dt and s and s > 0:
            efforts.append((dt, s))
    efforts.sort(key=lambda e: e[0])
    return efforts


def _threshold_at(dt: datetime, efforts: list[tuple[datetime, float]],
                  fallback: float | None) -> float | None:
    """Threshold m/s from the fastest 5K within a centered window of `dt`.

    Widens the window progressively (60 -> 120 -> 240 -> 480 days) when no 5K
    falls in range, so sparse eras stay anchored to a nearby effort rather than
    the all-time best. Uses `fallback` (all-time best) only when no effort
    exists at all, so a run's pace zones reflect fitness *at the time it was run*.
    """
    if not efforts:
        return fallback
    for mult in (1, 2, 4, 8):
        span = THRESHOLD_WINDOW_DAYS * mult
        best = min((s for d, s in efforts if abs((d - dt).days) <= span), default=None)
        if best:
            return round(5000.0 / best, 4)
    return fallback


def _compute_pace_zones(km_splits: list[dict], threshold_mps: float) -> list[float]:
    """
    Time in each of 5 pace zones, computed from per-km splits.
    Each split covers exactly 1000 m, so time_s == seconds-per-km.
    Zone boundaries use the same speed-fraction-of-threshold model as Strava/TrainingPeaks.
    """
    if not threshold_mps or threshold_mps <= 0 or not km_splits:
        return [0.0, 0.0, 0.0, 0.0, 0.0]
    bounds = [0.77, 0.87, 0.93, 1.03]
    secs = [0.0, 0.0, 0.0, 0.0, 0.0]
    for s in km_splits:
        t = s.get("time_s")
        if not t or t <= 0:
            continue
        speed = 1000.0 / t       # m/s (1 km in t seconds)
        frac = speed / threshold_mps
        if frac < bounds[0]:
            secs[0] += t
        elif frac < bounds[1]:
            secs[1] += t
        elif frac < bounds[2]:
            secs[2] += t
        elif frac < bounds[3]:
            secs[3] += t
        else:
            secs[4] += t
    return [round(s, 1) for s in secs]


# ---------------------------------------------------------------------------
# Run-type classification (relative to the runner's own data)
# ---------------------------------------------------------------------------

MISC_MAX_KM         = 1.0    # below this = miscellaneous (stair sprints, elevation challenges, etc.)
LONG_RUN_PERCENTILE = 0.75   # distance at/above this percentile of all runs = long candidate
LONG_RUN_MIN_RATIO  = 1.30   # ...and at least this multiple of the median run distance
LONG_RUN_MIN_KM     = 10.0   # ...and never below this absolute floor
RACE_Z5_SHARE       = 0.40   # >= this share of zoned time in zone 5 (frac>=1.03) = race
THRESHOLD_Z4_SHARE  = 0.30   # >= this share in zone 4 (0.93-1.03) = threshold session
TEMPO_Z34_SHARE     = 0.35   # >= this combined share in zones 3-4 = tempo session
RECOVERY_Z1_SHARE   = 0.70   # >= this share in zone 1 (frac<0.77) = recovery


def _classify_run(run: dict, dist_p75: float, dist_median: float) -> str:
    """Assign one training category to a run.

    Intensity buckets (race/threshold/tempo) use the share of zoned time from
    `pace_zones`; they require a *sustained* share, so an easy long run falls
    through to "long" while a genuine workout outranks distance. Distance is
    judged relative to the runner's own distribution, so "long" tracks fitness
    rather than a fixed cutoff. Falls back to "easy" when there's no signal
    (e.g. no personal threshold yet, so the zone times are all zero).

    Suspiciously short efforts (stair sprints for a segment record, an elevation
    challenge done on one flight of steps, etc.) are bucketed as "misc" before
    anything else, which also keeps them out of the intervals bucket.
    """
    if run["dist_km"] < MISC_MAX_KM:
        return "misc"

    if run["is_interval"]:
        return "intervals"

    zones = run.get("pace_zones") or []
    total = sum(zones)
    z1 = z3 = z4 = z5 = 0.0
    if total > 0:
        z1, _z2, z3, z4, z5 = (z / total for z in zones)
        if z5 >= RACE_Z5_SHARE:
            return "race"
        if z4 >= THRESHOLD_Z4_SHARE:
            return "threshold"
        if (z3 + z4) >= TEMPO_Z34_SHARE:
            return "tempo"

    if dist_median > 0 and run["dist_km"] >= LONG_RUN_MIN_KM \
            and run["dist_km"] >= dist_p75 \
            and run["dist_km"] >= LONG_RUN_MIN_RATIO * dist_median:
        return "long"

    if z1 >= RECOVERY_Z1_SHARE:
        return "recovery"

    return "easy"


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (q in [0,1])."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
# Pause / gap detection
# ---------------------------------------------------------------------------

# A pause is a recording gap (auto-pause, manual pause, or a stopped-then-resumed
# session). fit_distance_stream is sampled by distance (~every 20 m) and each point
# carries `t` = elapsed seconds since the run start, so a pause collapses into a
# single inter-sample step whose `t` jumps while `d` barely moves. We flag a step as
# a pause when its time delta clears an adaptive floor: at least PAUSE_MIN_GAP_S
# absolute, and at least PAUSE_GAP_MULTIPLIER × the run's median step time. The
# multiplier keeps genuinely slow running (which raises every step's dt uniformly)
# from being mistaken for a stop.
#
# A single real stop that creeps forward (GPS drift while standing, or a slow walk)
# emits several consecutive over-floor steps, so we coalesce adjacent flagged steps
# into one pause. The merged pause carries `d` (stop distance) and `d2` (resume
# distance), letting the chart dash and label the whole slow stretch as one event
# instead of stacking a marker per sample. On an interval session this is the
# difference between 9 markers and ~22.
PAUSE_MIN_GAP_S      = 20    # absolute floor (seconds); ignore steps shorter than this
PAUSE_GAP_MULTIPLIER = 5     # ... and require dt >= this × the run's median step dt
# Only dot the route across a pause when the run genuinely relocated during it. Every
# stream point now carries an interpolated coordinate, so stop and resume are never
# exactly equal; without a floor, a plain stationary stop (one ~7 m sampling step)
# would dot the line. Below this, the pause is a lone marker; at/above it the
# stop→resume stretch is dotted (slow movement during the pause, or a stop-and-
# resume-elsewhere gap the polyline hops across).
PAUSE_MOVE_MIN_M     = 30.0
# Half-window (metres of route arc length) searched for the polyline vertex nearest a
# pause's STOP coordinate. Generous enough to absorb GPS-vs-device distance disagreement
# over one inter-pause stretch (measured ~12% on a real run), tight enough not to snap
# onto another pass of an out-and-back/lap route.
PAUSE_SNAP_WINDOW_M  = 200.0
# Must match HUB_JS's PACE_WIN_KM (the pace chart's backward pause extension) — the
# map's dotted stretch is extended backward by the same amount so both visualisations
# agree on where a pause "starts". Change both together.
MAP_PACE_WIN_KM      = 0.05


def _nearest_coord(dist_stream: list[dict], start_idx: int, step: int) -> dict | None:
    """Walk the stream from start_idx in the given direction (+1/-1) until a point
    carrying coordinates is found. Returns that point, or None if none exists."""
    i = start_idx
    n = len(dist_stream)
    while 0 <= i < n:
        p = dist_stream[i]
        if p.get("lat") is not None and p.get("lon") is not None:
            return p
        i += step
    return None


def _stream_arc_m(dist_stream: list[dict], a: int, b: int) -> float:
    """Ground distance (metres) covered along the stream's own coordinates between
    indices a and b inclusive. During an auto-pause the recorded distance is frozen
    while the runner keeps moving, so this path length — not the stop→resume straight
    line — is how far the route physically advanced across the pause."""
    total = 0.0
    prev = None
    for k in range(max(0, a), min(len(dist_stream), b + 1)):
        p = dist_stream[k]
        if p.get("lat") is None or p.get("lon") is None:
            continue
        if prev is not None:
            total += _haversine_km(prev[0], prev[1], p["lat"], p["lon"]) * 1000.0
        prev = (p["lat"], p["lon"])
    return total


def _detect_pauses(dist_stream: list[dict]) -> list[dict]:
    """Detect recording pauses from an over-distance stream.

    Returns a list of one dict per pause:
        {"d": km, "d2": km, "dur": seconds[, "lat", "lon"][, "rlat", "rlon"]}
    `d` is the distance at which recording stopped and `d2` where it resumed (for the
    graph, which dashes and labels the whole [d, d2] span). Consecutive over-floor
    steps are coalesced into one pause, so a stop that creeps forward is a single
    event, not one marker per sample. `lat`/`lon` is the stop location on the route
    map (omitted for GPS-less runs, which still get graph marks). `rlat`/`rlon` are
    the resume location, present when the route advanced between stopping and resuming,
    letting the map dot that stretch — whether slow movement during the pause or a
    stop-and-resume-elsewhere gap (the polyline hops near-straight across the latter).
    A plain stationary pause where stop and resume coincide has no resume point and
    shows as a lone marker.
    """
    if not dist_stream or len(dist_stream) < 5:
        return []

    dts = [dist_stream[i].get("t", 0) - dist_stream[i - 1].get("t", 0)
           for i in range(1, len(dist_stream))]
    dts_sorted = sorted(dts)
    median_dt = dts_sorted[len(dts_sorted) // 2]
    if median_dt <= 0:
        return []

    floor = max(PAUSE_MIN_GAP_S, PAUSE_GAP_MULTIPLIER * median_dt)

    # Group consecutive over-floor steps: each group is (start_i, end_i, total_dt),
    # where the step i is the gap between dist_stream[i-1] (before) and [i] (after).
    groups = []
    for i in range(1, len(dist_stream)):
        dt = dist_stream[i].get("t", 0) - dist_stream[i - 1].get("t", 0)
        if dt < floor:
            continue
        if groups and i == groups[-1][1] + 1:
            g = groups[-1]
            groups[-1] = (g[0], i, g[2] + dt)
        else:
            groups.append((i, i, dt))

    pauses = []
    for start_i, end_i, total_dt in groups:
        first_prev = dist_stream[start_i - 1]
        last_cur = dist_stream[end_i]
        pause = {
            "d":   round(first_prev.get("d", 0.0), 3),
            "d2":  round(last_cur.get("d", 0.0), 3),
            "dur": round(total_dt),
            # Ground actually covered stop→resume; consumed by _paused_route_ranges to
            # place the dotted-route end, then stripped before the pause is serialised.
            "move_m": round(_stream_arc_m(dist_stream, start_i - 1, end_i), 1),
        }
        # Stop / resume locations: nearest coordinate-bearing samples outside the group
        # (coords are sparse, so we walk outward). `stop` places the marker; `stop`->
        # `resume` is the route stretch the map dots, mirroring the dashed pause segment
        # on the over-distance chart.
        stop   = _nearest_coord(dist_stream, start_i - 1, -1) or _nearest_coord(dist_stream, end_i, +1)
        resume = _nearest_coord(dist_stream, end_i, +1)
        if stop is not None:
            pause["lat"] = stop["lat"]
            pause["lon"] = stop["lon"]
        # Tag the resume point only when the route advanced a meaningful distance
        # between stopping and resuming, so the map dots that stretch. This covers
        # slow movement during a pause and stop-and-resume-elsewhere gaps (the polyline
        # hops near-straight across the latter). A stationary stop stays a lone marker.
        if stop is not None and resume is not None and \
           _haversine_km(stop["lat"], stop["lon"],
                         resume["lat"], resume["lon"]) * 1000 >= PAUSE_MOVE_MIN_M:
            pause["rlat"] = resume["lat"]
            pause["rlon"] = resume["lon"]
        pauses.append(pause)
    return pauses


def _polyline_cum_m(line: list) -> list[float]:
    """Cumulative flat-earth arc length (metres) along a lat/lon polyline."""
    cum = [0.0]
    for i in range(1, len(line)):
        dy = (line[i][0] - line[i - 1][0]) * 111320.0
        dx = (line[i][1] - line[i - 1][1]) * 111320.0 * math.cos(math.radians(line[i - 1][0]))
        cum.append(cum[-1] + math.hypot(dx, dy))
    return cum


def _idx_at_arc_m(cum: list[float], target_m: float) -> int:
    """First polyline index whose cumulative arc length >= target_m (clamped)."""
    target_m = max(0.0, min(cum[-1], target_m))
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < target_m:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _nearest_poly_idx(line: list, lat: float, lon: float, lo: int, hi: int) -> int:
    """Nearest polyline vertex to (lat, lon) within the closed index window [lo, hi].
    Bounding the search stops it snapping to another pass of a route that revisits
    the same spot (out-and-back intervals, laps)."""
    lo = max(0, lo)
    hi = min(len(line) - 1, hi)
    best, best_d = lo, float("inf")
    for i in range(lo, hi + 1):
        d = _haversine_km(line[i][0], line[i][1], lat, lon)
        if d < best_d:
            best_d, best = d, i
    return best


def _paused_route_ranges(gps_polyline: list, pauses: list[dict]) -> list[list[int]]:
    """Polyline index ranges [i0, i1] the map draws dotted, one per pause.

    A pause's stream distance (`d`) cannot be projected onto the polyline by a global
    distance ratio: the stream, the polyline arc length, and Strava's official distance
    are three different bases, and ground covered while paused adds arc length without
    advancing `d`, so ratio error compounds pause after pause on an interval session.
    Instead each stop/resume is snapped to the vertex nearest its own GPS coordinate
    (the same coordinates the amber pause markers use). The nearest search is bounded
    to PAUSE_SNAP_WINDOW_M of arc around a coarse guess anchored at the previous
    pause's resolved position, which disambiguates repeated passes on lapped routes
    and stops error accumulating across pauses. The resume end is placed by stepping the
    stop's arc length forward by the ground actually covered during the pause (`move_m`,
    measured along the stream coords); this dots a move-during-pause meander without a
    GPS-proximity match, which on an out-and-back meander snaps to the return-leg pass.
    """
    line = gps_polyline
    if not pauses or len(line) < 2:
        return []
    cum = _polyline_cum_m(line)
    if cum[-1] <= 0:
        return []

    anchor_idx, anchor_d = 0, 0.0
    raw = []
    for p in pauses:
        if p.get("lat") is None or p.get("lon") is None:
            continue  # GPS-less streams never reach the map; skip defensively

        guess0 = cum[anchor_idx] + max(0.0, p["d"] - anchor_d) * 1000.0
        i0 = _nearest_poly_idx(line, p["lat"], p["lon"],
                               _idx_at_arc_m(cum, guess0 - PAUSE_SNAP_WINDOW_M),
                               _idx_at_arc_m(cum, guess0 + PAUSE_SNAP_WINDOW_M))
        # Instantaneous pace looks ahead ~50 m, so samples before the stop already read
        # slow; extend backward so the dotted route covers the chart's dashed dip.
        i0_back = _idx_at_arc_m(cum, cum[i0] - MAP_PACE_WIN_KM * 1000.0)

        d2 = p.get("d2", p["d"])
        arc_gap = max(0.0, d2 - p["d"]) * 1000.0
        if p.get("rlat") is not None and p.get("rlon") is not None:
            # Advance i1 by the ground actually covered during the pause (measured along
            # the stream's own coordinates in _detect_pauses). Decimation preserves arc
            # length between the vertices it keeps, so stepping cum[i0] forward by that
            # path length lands on the resume even when the runner wandered out and back.
            # Matching the resume coordinate by GPS proximity instead grabs the RETURN-leg
            # pass on such a meander (Power Pyramid pause 3) and overshoots up the street.
            i1 = _idx_at_arc_m(cum, cum[i0] + p.get("move_m", arc_gap))
        else:
            i1 = _idx_at_arc_m(cum, cum[i0] + arc_gap)

        if i1 > i0_back:
            raw.append([i0_back, i1])
        anchor_idx, anchor_d = i1, d2

    raw.sort(key=lambda rg: rg[0])
    merged: list[list[int]] = []
    for rg in raw:
        if merged and rg[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], rg[1])
        else:
            merged.append(rg)
    return merged


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def _build_runs(rows: list[dict], threshold_mps: float | None) -> list[dict]:
    """Convert enriched CSV rows to per-run dicts for the JS frontend."""
    efforts = _build_threshold_curve(rows)
    runs = []
    for row in rows:
        dt = parse_date(row)
        if not dt:
            continue
        dist_m = num(row, "Distance") or 0
        dist_km = dist_m / 1000
        if dist_km <= 0:
            continue
        moving_s = num(row, "Moving Time") or 0
        gain = num(row, "Elevation Gain") or 0
        ga_s = ga_time(moving_s, gain, dist_km) if moving_s > 0 else 0.0

        # Lap splits (from FIT laps — kept for cadence/gain info)
        try:
            lap_splits = json.loads(row.get("fit_splits") or "[]")
        except (json.JSONDecodeError, TypeError):
            lap_splits = []
        for idx, lap in enumerate(lap_splits):
            t = lap.get("time_s")
            d = lap.get("dist_km")
            lap["idx"] = idx + 1
            lap["dist_str"] = f"{d:.2f}" if d else "—"
            lap["time_str"] = fmt_time(t) if t else "—"
            lap["pace_str"] = fmt_pace(t, d * 1000) if (t and d and d > 0) else "—"
            cad = lap.get("avg_cadence")
            lap["cadence_str"] = str(round(cad)) if cad else "—"
            asc = lap.get("ascent_m")
            lap["gain_str"] = f"+{round(asc)}m" if asc else "—"

        # Per-km splits (interpolated from record track in strava_compile.py)
        try:
            km_splits = json.loads(row.get("fit_km_splits") or "[]")
        except (json.JSONDecodeError, TypeError):
            km_splits = []
        for s in km_splits:
            t = s.get("time_s")
            if t and t > 0:
                mins = int(t // 60)
                secs_r = int(round(t % 60))
                if secs_r == 60:
                    mins += 1
                    secs_r = 0
                s["pace_str"] = f"{mins}:{secs_r:02d}/km"
                s["pace_s"] = round(t, 1)  # secs/km (= time_s since 1 km per split)
            else:
                s["pace_str"] = "—"
                s["pace_s"] = None

        # Pace zones: prefer km-split-based computation (correct units, personal threshold).
        # The threshold tracks contemporaneous fitness, so an early effort is judged
        # against the runner's 5K form at the time rather than today's all-time best.
        run_threshold = _threshold_at(dt, efforts, threshold_mps)
        pace_zones = _compute_pace_zones(km_splits, run_threshold)

        # GPS polyline for map
        try:
            gps_polyline = json.loads(row.get("fit_gps_polyline") or "[]")
        except (json.JSONDecodeError, TypeError):
            gps_polyline = []

        # Over-distance metric stream
        try:
            dist_stream = json.loads(row.get("fit_distance_stream") or "[]")
        except (json.JSONDecodeError, TypeError):
            dist_stream = []

        # Recording pauses/gaps derived from the stream's elapsed-time field, plus the
        # polyline index ranges the map draws dotted for them.
        pauses = _detect_pauses(dist_stream)
        pause_ranges = _paused_route_ranges(gps_polyline, pauses)
        for p in pauses:
            p.pop("move_m", None)  # internal to range placement; keep it out of the JSON

        # Per-run best efforts
        best_efforts = []
        for label, col, dist_be in BEST_EFFORT_DEFS:
            s = num(row, col)
            if s and s > 0:
                best_efforts.append({
                    "label": label,
                    "time":  fmt_time(s),
                    "pace":  fmt_pace(s, dist_be),
                })

        runs.append({
            "id":          len(runs),
            "name":        (row.get("Activity Name") or "Run").strip() or "Run",
            "description": (row.get("Activity Description") or "").strip(),
            "date_long":   dt.strftime("%d %b %Y"),
            "date_short":  dt.strftime("%b %d"),
            "weekday":     dt.strftime("%A"),
            "date_iso":    dt.strftime("%Y-%m-%d"),
            "dist_km":     round(dist_km, 2),
            "moving_s":    round(moving_s),
            "moving_str":  fmt_time(moving_s) if moving_s > 0 else "—",
            "pace_str":    fmt_pace(moving_s, dist_km * 1000) if moving_s > 0 else "—",
            "ga_pace_str": fmt_pace(ga_s, dist_km * 1000) if ga_s > 0 else "—",
            "gain":        round(gain),
            "descent":     round(num(row, "fit_total_descent_m") or 0),
            "cadence":     round(num(row, "fit_avg_cadence")) if num(row, "fit_avg_cadence") else None,
            "calories":    round(num(row, "fit_total_calories")) if num(row, "fit_total_calories") else None,
            "is_interval":  is_interval(row),
            "lap_splits":   lap_splits,
            "km_splits":    km_splits,
            "pace_zones":   pace_zones,
            "best_efforts": best_efforts,
            "gps_polyline": gps_polyline,
            "dist_stream": dist_stream,
            "pauses": pauses,
            "pause_ranges": pause_ranges,
        })

    # Classify each run once the full distance distribution is known, so "long"
    # is judged relative to the runner's own history rather than a fixed cutoff.
    dists = sorted(r["dist_km"] for r in runs)
    dist_median = _percentile(dists, 0.50)
    dist_p75 = _percentile(dists, LONG_RUN_PERCENTILE)
    for r in runs:
        r["run_type"] = _classify_run(r, dist_p75, dist_median)

    runs.sort(key=lambda r: r["date_iso"], reverse=True)
    return runs


def _load_recommendation(
    runs: list[dict], daily: list[tuple], fitness: list[dict],
    threshold_mps: float | None,
) -> dict | None:
    """Recommend next week's training in real units from the ACWR injury model.

    The acute:chronic workload ratio (acute = trailing 7-day load, chronic =
    trailing 28-day load scaled to a week) has a well-documented injury "sweet
    spot" around 0.8-1.3; ratios above ~1.5 carry markedly higher soft-tissue
    injury risk. We turn that into a concrete target load for the coming week,
    nudged by current form (TSB), then translate it back into the numbers a
    runner actually plans by: weekly distance, longest run, climb and an easy
    pace ceiling. The translation uses *your own* recent distance/load and
    elevation/distance ratios so the km figures match how you actually run.

    Returns None when there isn't enough history (no 28-day baseline yet).
    """
    if not daily:
        return None
    loads = [l for _, l in daily]
    acute = sum(loads[-7:])
    chronic_window = loads[-28:]
    chronic = sum(chronic_window) / len(chronic_window) * 7 if chronic_window else 0.0
    if chronic <= 0 or len(loads) < 14:
        return None
    acwr = acute / chronic

    cur = fitness[-1] if fitness else {"ctl": 0.0, "atl": 0.0, "tsb": 0.0}
    tsb = cur["tsb"]

    rec_low  = 0.8 * chronic   # detraining floor of the sweet spot
    rec_high = 1.3 * chronic   # injury-risk ceiling of the sweet spot

    # When building, step up from whichever is higher — last week's actual load
    # or your established baseline — so a runner who's been progressing isn't
    # dragged back toward a lagging chronic average. The injury ceiling
    # (rec_high) is the hard guardrail; the small step is itself the ramp limit.
    build_base = max(acute, chronic)
    if tsb < -25 or acwr > 1.5:
        headline, risk, target = "Back off and recover", "red", 0.80 * chronic
    elif tsb < -10 or acwr > 1.3:
        headline, risk, target = "Hold steady — let fatigue settle", "amber", chronic
    elif tsb > 12 and acwr < 1.1:
        headline, risk, target = "Build — you have room", "green", build_base * 1.10
    else:
        headline, risk, target = "Build gradually", "green", build_base * 1.05

    target = min(target, rec_high)           # never exceed the injury-risk ceiling
    target = max(target, 0.0)

    # ── Translate load → real units using the runner's own recent profile ──
    last_date   = daily[-1][0]
    win28_start = str(last_date - timedelta(days=27))
    win7_start  = str(last_date - timedelta(days=6))
    recent = [r for r in runs if r["date_iso"] >= win28_start]
    km28   = sum(r["dist_km"] for r in recent)
    gain28 = sum(r["gain"] for r in recent)
    load28 = sum(chronic_window)
    # load per km from real history (captures their typical intensity + terrain);
    # fall back to the easy-running approximation (load ≈ km*10) if no recent km.
    load_per_km = (load28 / km28) if km28 > 0 else 10.0
    gain_per_km = (gain28 / km28) if km28 > 0 else 0.0

    def km(load_val: float) -> int:
        return round(load_val / load_per_km) if load_per_km > 0 else 0
    def metres(km_val: float) -> int:
        return int(round(km_val * gain_per_km / 10.0) * 10)

    target_km = km(target)
    low_km, high_km = km(rec_low), km(rec_high)
    # Longest single run: up to ~40% of the week — the upper end of the common
    # 30-40% guideline, enough room for a proper long run without one session
    # dominating the week.
    long_run_km = round(target_km * 0.40)
    last7_km = round(sum(r["dist_km"] for r in runs if r["date_iso"] >= win7_start), 1)

    # Easy-pace ceiling: most weekly volume should sit at an aerobic pace, ~85%
    # of threshold velocity. Skip if we have no threshold estimate.
    easy_pace = thr_pace = None
    if threshold_mps and threshold_mps > 0:
        thr_pace  = fmt_pace_from_s_per_km(1000.0 / threshold_mps)
        easy_pace = fmt_pace_from_s_per_km(1000.0 / (threshold_mps * 0.85))

    return {
        "acwr":         round(acwr, 2),
        "acwr_class":   "red" if acwr > 1.5 else ("amber" if (acwr > 1.3 or acwr < 0.8) else "green"),
        "headline":     headline,
        "risk":         risk,
        # human-facing primary numbers
        "target_km":    target_km,
        "low_km":       low_km,
        "high_km":      high_km,
        "long_run_km":  long_run_km,
        "target_gain":  metres(target_km),
        "gain_per_km":  round(gain_per_km),
        "easy_pace":    easy_pace,
        "thr_pace":     thr_pace,
        "last7_km":     last7_km,
        # technical figures shown as sub-text
        "target_load":  round(target),
        "load_low":     round(rec_low),
        "load_high":    round(rec_high),
        "last_week":    round(acute),
        "advice":       _load_advice(tsb, acwr),
    }


def _load_advice(tsb: float, acwr: float) -> str:
    """One-paragraph rationale tying the numbers to injury-prevention guidance."""
    bits = []
    if acwr > 1.5:
        bits.append("Your last 7 days sit well above your 4-week baseline "
                    "(ACWR &gt; 1.5) — the classic high injury-risk zone — so pull the coming week down.")
    elif acwr > 1.3:
        bits.append("Acute load is running ahead of your baseline; ease off "
                    "slightly to bring ACWR back under 1.3.")
    elif acwr < 0.8:
        bits.append("You've been training below your baseline, so there's room "
                    "to add load without spiking risk.")
    else:
        bits.append("Your acute:chronic balance sits in the safe 0.8–1.3 sweet spot.")
    if tsb < -25:
        bits.append("Form is deeply negative — prioritise recovery before building again.")
    elif tsb < -10:
        bits.append("You're carrying fatigue, so keep most runs easy.")
    elif tsb > 12:
        bits.append("You're well rested and can absorb a bigger week.")
    bits.append("Keep the week-on-week increase under ~10% to stay injury-safe.")
    return " ".join(bits)


def _recommendation_html(rec: dict | None) -> str:
    """Render the 'Plan for next 7 days' overview section (or nothing)."""
    if not rec:
        return ""
    pace_card = ""
    if rec["easy_pace"]:
        pace_card = (
            f'<div class="ov-card"><p class="ov-label">Easy pace ceiling</p>'
            f'<p class="ov-value">{rec["easy_pace"]}<span class="ov-unit">/km</span></p>'
            f'<p class="ov-sub">keep most runs easier &middot; threshold {rec["thr_pace"]}</p></div>'
        )
    elev_card = ""
    if rec["gain_per_km"] > 0:
        elev_card = (
            f'<div class="ov-card"><p class="ov-label">Climb budget</p>'
            f'<p class="ov-value">{rec["target_gain"]}<span class="ov-unit">m</span></p>'
            f'<p class="ov-sub">&asymp; {rec["gain_per_km"]} m/km at your usual terrain</p></div>'
        )
    return f"""
<div class="ov-section">
  <p class="ov-section-title">Plan for next 7 days</p>
  <div class="ov-rec">
    <div class="ov-rec-head">
      <span class="ov-rec-badge {rec['risk']}">{rec['headline']}</span>
      <span class="ov-rec-acwr">workload ratio <b class="{rec['acwr_class']}">{rec['acwr']:.2f}</b> &middot; target load {rec['target_load']}</span>
    </div>
    <div class="ov-grid">
      <div class="ov-card"><p class="ov-label">Weekly distance</p><p class="ov-value {rec['risk']}">{rec['target_km']}<span class="ov-unit">km</span></p><p class="ov-sub">safe range {rec['low_km']}&ndash;{rec['high_km']} km &middot; last week {rec['last7_km']} km</p></div>
      <div class="ov-card"><p class="ov-label">Longest run</p><p class="ov-value">{rec['long_run_km']}<span class="ov-unit">km</span></p><p class="ov-sub">cap one run at ~⅓ of the week</p></div>
      {pace_card}
      {elev_card}
    </div>
    <p class="ov-rec-advice">{rec['advice']}</p>
    <p class="ov-rec-foot">Distance, climb and pace are translated from a TSS-style training-load target (shown above) using your own recent running. Guidance only &mdash; not a substitute for how your body feels.</p>
  </div>
</div>
"""


def _overview_stats(rows: list[dict], runs: list[dict], threshold_mps: float | None = None) -> dict:
    total_km = sum(r["dist_km"] for r in runs)
    total_s  = sum(r["moving_s"] for r in runs)
    today      = datetime.today().date()
    week_start = today - timedelta(days=today.weekday())
    this_week  = [r for r in runs if r["date_iso"] >= str(week_start)]
    longest    = max(runs, key=lambda r: r["dist_km"], default=None)

    loads   = session_loads(rows)
    daily   = daily_series(loads)
    fitness = ctl_atl_tsb(daily)
    cur     = fitness[-1] if fitness else {"ctl": 0.0, "atl": 0.0, "tsb": 0.0}
    tsb     = cur["tsb"]
    rec     = _load_recommendation(runs, daily, fitness, threshold_mps)

    return {
        "rec":           rec,
        "total_runs":    len(runs),
        "total_km":      round(total_km, 1),
        "total_time":    fmt_time(total_s),
        "week_km":       round(sum(r["dist_km"] for r in this_week), 1),
        "week_runs":     len(this_week),
        "longest_km":    longest["dist_km"] if longest else 0,
        "longest_date":  longest["date_long"] if longest else "—",
        "ctl":           round(cur["ctl"], 1),
        "atl":           round(cur["atl"], 1),
        "tsb":           round(tsb, 1),
        "tsb_class":     "green" if tsb > 5 else ("amber" if tsb > -10 else "red"),
        "tsb_note":      "fresh" if tsb > 5 else ("building" if tsb > -10 else "fatigued"),
    }


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

HUB_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%}
body{font-family:system-ui,-apple-system,sans-serif;background:#1a1a1a;color:#eee}

.tab-bar{display:flex;align-items:center;background:#111;border-bottom:1px solid #2a2a2a;
  padding:0 1rem;height:44px;position:sticky;top:0;z-index:100;gap:2px}
.brand{font-size:14px;font-weight:700;color:#5cb85c;margin-right:14px;letter-spacing:-.02em;white-space:nowrap}
.tab{background:none;border:none;border-bottom:2px solid transparent;color:#555;padding:0 13px;
  height:44px;font-size:13px;cursor:pointer;transition:color .15s,border-color .15s}
.tab:hover{color:#ccc}
.tab.active{color:#eee;border-bottom-color:#5cb85c}

.panel{display:none}
.panel.active{display:flex;flex-direction:column}

/* Overview */
#panel-overview{height:calc(100vh - 44px);overflow-y:auto;padding:1rem}
.ov-section{margin-bottom:1.25rem}
.ov-section-title{font-size:11px;font-weight:600;color:#666;text-transform:uppercase;
  letter-spacing:.05em;margin:0 0 8px;padding-bottom:4px;border-bottom:1px solid #2a2a2a}
.ov-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.ov-card{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:12px 14px}
.ov-label{font-size:10px;color:#666;margin:0 0 4px}
.ov-value{font-size:20px;font-weight:700;color:#eee;margin:0;line-height:1.2}
.ov-unit{font-size:12px;font-weight:600;color:#888;margin-left:3px}
.ov-sub{font-size:10px;color:#555;margin:3px 0 0}
.green{color:#5cb85c}.amber{color:#e0a020}.red{color:#d9534f}
.ov-rec{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:12px 14px}
.ov-rec-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.ov-rec-badge{font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;border:1px solid currentColor}
.ov-rec-badge.green{background:rgba(92,184,92,.12)}.ov-rec-badge.amber{background:rgba(224,160,32,.12)}.ov-rec-badge.red{background:rgba(217,83,79,.12)}
.ov-rec-acwr{font-size:11px;color:#888}
.ov-rec .ov-grid{margin-bottom:10px}
.ov-rec-advice{font-size:12px;color:#bbb;line-height:1.55;margin:0 0 8px}
.ov-rec-foot{font-size:10px;color:#555;margin:0;line-height:1.5}
.spark-wrap{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:10px 14px}
.spark-label{font-size:10px;color:#555;margin:0 0 6px}
.bars{display:flex;align-items:flex-end;gap:4px;height:48px}
.bar-col{display:flex;flex-direction:column;align-items:center;flex:1}
.bar-rect{width:100%;border-radius:2px 2px 0 0;background:#3a5a3a;min-height:2px}
.bar-rect.latest{background:#5cb85c}
.bar-wk{font-size:8px;color:#444;margin-top:3px;text-align:center}
.bar-km{font-size:8px;color:#666;margin-bottom:2px;text-align:center}

/* Runs panel */
#panel-runs{height:calc(100vh - 44px);overflow:hidden}
.runs-layout{display:flex;height:100%}
#run-list-pane{width:290px;min-width:220px;border-right:1px solid #2a2a2a;
  display:flex;flex-direction:column;overflow:hidden}
.run-search{padding:8px 10px;border-bottom:1px solid #2a2a2a;background:#111}
.run-search input{width:100%;background:#222;border:1px solid #3a3a3a;border-radius:6px;
  color:#eee;padding:6px 10px;font-size:12px;outline:none}
.run-search input::placeholder{color:#444}
.run-search input:focus{border-color:#5cb85c}
#run-list-items{overflow-y:auto;flex:1}

.run-item{padding:9px 12px;border-bottom:1px solid #1e1e1e;cursor:pointer;transition:background .1s}
.run-item:hover{background:#1f1f1f}
.run-item.active{background:#1b2a1b;border-left:3px solid #5cb85c;padding-left:9px}
.ri-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px}
.ri-date{font-size:10px;color:#555}
.ri-dist{font-size:14px;font-weight:700;color:#eee}
.ri-name{font-size:12px;color:#bbb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.ri-bottom{display:flex;align-items:center;gap:8px}
.ri-pace{font-size:11px;color:#777}
.ri-gain{font-size:10px;color:#4a9a4a}
.ri-tag{font-size:9px;border-radius:4px;padding:1px 5px;border:1px solid currentColor;
  background:rgba(255,255,255,.04);white-space:nowrap}
/* Per-category colours, shared by run-list badges (.rt-*) and filter chips. */
.rt-recovery{color:#7aa7d6}
.rt-easy{color:#5cb85c}
.rt-long{color:#3fb6a8}
.rt-tempo{color:#e0a020}
.rt-threshold{color:#e8772e}
.rt-intervals{color:#9b7ede}
.rt-race{color:#d9534f}
.rt-misc{color:#888}

/* Filter chips */
.run-chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.chip{font-size:10px;padding:2px 8px;border-radius:999px;cursor:pointer;
  background:#222;border:1px solid #3a3a3a;color:#888;transition:background .1s,color .1s}
.chip:hover{background:#2a2a2a;color:#ccc}
.chip.active{background:#1b2a1b;border-color:#5cb85c;color:#eee}

/* Sort + stat-range filters */
.run-sort{display:flex;gap:4px;margin-top:8px;align-items:center}
.run-sort select{flex:1;background:#222;border:1px solid #3a3a3a;border-radius:6px;
  color:#eee;padding:4px 6px;font-size:11px;outline:none}
.run-sort select:focus{border-color:#5cb85c}
.run-sort button{background:#222;border:1px solid #3a3a3a;border-radius:6px;color:#888;
  width:26px;padding:4px 0;font-size:12px;cursor:pointer;transition:color .1s,border-color .1s}
.run-sort button:hover{color:#ccc}
.run-sort button.active{border-color:#5cb85c;color:#eee}
.run-stat-filters{margin-top:8px;display:flex;flex-direction:column;gap:4px}
.run-stat-filters[hidden]{display:none}
.sf-row{display:flex;align-items:center;gap:4px}
.sf-label{font-size:10px;color:#888;width:38px;flex:none}
.sf-row input{flex:1;width:0;background:#222;border:1px solid #3a3a3a;border-radius:4px;
  color:#eee;padding:3px 5px;font-size:10px;outline:none}
.sf-row input:focus{border-color:#5cb85c}
.sf-unit{font-size:9px;color:#555;width:32px;flex:none}

/* Run detail */
#run-detail-pane{flex:1;overflow-y:auto;padding:1.25rem}
.placeholder-msg{color:#333;font-size:13px;text-align:center;margin-top:4rem}
/* Run-detail top: name+stats on the left, Strava description on the right.
   The left column is pinned to the top; the description is bottom-aligned so the
   bottom of its last line (incl. the "more" button) sits on the stats-box bottom.
   Expanding the description grows the box upward into the space above it. */
.rd-top{display:flex;gap:1rem;margin-bottom:1.25rem}
.rd-left{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;align-self:flex-start}
.rd-header{margin-bottom:1rem}
.rd-name{font-size:20px;font-weight:700;color:#eee;margin:0 0 4px}
.rd-date{font-size:12px;color:#555}
.rd-desc{flex:0 1 360px;max-width:360px;align-self:flex-end;background:#1e1e1e;
  border:1px solid #2a2a2a;border-radius:8px;padding:8px 12px;font-size:12px;color:#aaa}
.rd-desc-text{white-space:pre-line;line-height:1.5;word-break:break-word}
.rd-desc-btn{background:none;border:none;color:#5cb85c;font-size:11px;font-weight:600;
  cursor:pointer;padding:3px 0 0}
.rd-desc-btn:hover{text-decoration:underline}
.rd-stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px}
.rds{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:10px 12px}
.rds-l{font-size:10px;color:#555;margin-bottom:3px}
.rds-v{font-size:15px;font-weight:600;color:#eee}
.rds-v-muted{font-size:13px;color:#777;font-weight:400}
.rd-section{margin-bottom:1.25rem}
.rd-section-title{font-size:11px;font-weight:600;color:#666;text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #2a2a2a}
.rd-table{width:100%;border-collapse:collapse;font-size:12px}
.rd-table th{font-size:10px;font-weight:600;color:#555;text-align:left;
  padding:4px 8px;border-bottom:1px solid #2a2a2a}
.rd-table th:not(:first-child){text-align:right}
.rd-table td{padding:6px 8px;border-bottom:1px solid #1e1e1e;color:#bbb}
.rd-table td:not(:first-child){text-align:right;font-variant-numeric:tabular-nums}
.rd-table tr:last-child td{border-bottom:none}
.pace-cell{color:#eee;font-weight:500}
.best-km{color:#5cb85c !important}
.leaflet-container,.leaflet-container *,.leaflet-container *::before,.leaflet-container *::after{box-sizing:content-box}
/* Leaflet's control corners default to z-index 1000, above the expand overlays
   (700/600), so a background map's zoom buttons poke through the dark backdrop.
   Drop them below the overlays. Controls on an overlay's own map still show: the
   overlay is its own stacking context, so they paint above its backdrop. */
.leaflet-top,.leaflet-bottom{z-index:400}

.dist-tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.dist-tab{background:#222;border:1px solid #3a3a3a;color:#888;font-size:11px;
  padding:4px 10px;border-radius:6px;cursor:pointer;transition:color .1s,border-color .1s}
.dist-tab:hover{color:#ccc}
.dist-tab.active{color:#eee;border-color:#5cb85c;background:#1b2a1b}
.dist-chart{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:8px;position:relative;cursor:crosshair}
.dist-chart svg{display:block;width:100%;height:auto}
.dist-tip{position:absolute;pointer-events:none;background:#000;border:1px solid #3a3a3a;
  border-radius:6px;padding:4px 8px;font-size:11px;line-height:1.45;color:#eee;white-space:nowrap;
  transform:translate(-50%,calc(-100% - 10px));opacity:0;transition:opacity .08s;z-index:5}
.dist-tip b{color:#fff;font-weight:600}
.dist-tip .tip-d{color:#888}

/* Inline run map + "click to expand" affordance */
.rd-map-wrap{position:relative;margin-bottom:1.25rem;cursor:pointer}
.rd-map{height:460px;border-radius:8px;border:1px solid #3a3a3a}
.rd-map-hint{position:absolute;top:8px;right:8px;z-index:450;background:rgba(0,0,0,.6);
  color:#ddd;font-size:11px;padding:3px 9px;border-radius:6px;pointer-events:none;
  border:1px solid #3a3a3a}

/* Expanded analysis overlay (map + over-distance graph on one screen) */
.run-overlay{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:700;display:none}
.run-overlay.open{display:flex;justify-content:center}
.ro-box{display:flex;flex-direction:column;width:100%;max-width:1300px;height:100%;
  padding:12px 14px;gap:8px;overflow:hidden}
.ro-head{display:flex;justify-content:space-between;align-items:flex-start;flex:0 0 auto;gap:10px}
.ro-head-text{display:flex;flex-direction:column;gap:3px;min-width:0}
.ro-title{font-size:15px;font-weight:600;color:#eee}
.ro-stats{display:flex;flex-wrap:wrap;gap:3px 14px;font-size:12px;color:#9a9a9a}
.ro-stats b{color:#eee;font-weight:600}
.ro-close{background:#222;border:1px solid #3a3a3a;color:#bbb;font-size:15px;line-height:1;
  width:30px;height:30px;border-radius:6px;cursor:pointer}
.ro-close:hover{color:#fff;border-color:#5cb85c}
/* Body: splits column on the left, map+graph fill the height on the right */
.ro-body{flex:1 1 auto;display:flex;gap:12px;min-height:0}
.ro-splits{flex:0 0 290px;overflow-y:auto;min-height:0}
.ro-splits .rd-table th,.ro-splits .rd-table td{padding:5px 5px}
.ro-main{flex:1 1 auto;display:flex;flex-direction:column;gap:8px;min-height:0}
.ro-map{flex:1 1 auto;min-height:200px;border-radius:8px;border:1px solid #3a3a3a}
.ro-tabs{flex:0 0 auto}
.run-overlay .dist-chart{flex:0 0 auto;cursor:crosshair}
.run-overlay .dist-chart svg{height:30vh}
.ro-zoom-hint{flex:0 0 auto;font-size:10px;color:#666;text-align:center;margin-top:4px}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

HUB_JS = r"""
var overviewMap = null;
var currentRunMap = null;
var selectedRunId = null;
// The expanded-analysis overlay owns the only map the chart cursor links to.
var overlayMap = null;
var overlayRunId = null;
var activeCursorMarker = null;   // moving dot that tracks the over-distance chart cursor

function initRunMap(r) {
  if (currentRunMap) { currentRunMap.remove(); currentRunMap = null; }
  if (!r || !r.gps_polyline || r.gps_polyline.length < 5 || typeof L === 'undefined') return;
  setTimeout(function() {
    try {
      var mapEl = document.getElementById('run-map-' + r.id);
      if (!mapEl) return;
      currentRunMap = L.map(mapEl, {zoomControl: true, preferCanvas: true});
      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: 'abcd', maxZoom: 19
      }).addTo(currentRunMap);
      var bounds = drawRunRoute(currentRunMap, r);
      addPauseMarkers(currentRunMap, r);
      // A plain click on the route opens the expanded map + graph analysis view.
      currentRunMap.on('click', function() { openRunOverlay(r.id); });
      currentRunMap.fitBounds(bounds, {padding: [24, 24]});
    } catch(e) { console.warn('Run map init failed:', e); }
  }, 50);
}

// ── Expanded analysis overlay ───────────────────────────────────────────────

function openRunOverlay(runId) {
  // The inline map fires both Leaflet's click and the wrapper's onclick; ignore
  // the second call so we don't initialise the overlay map twice.
  if (document.getElementById('run-overlay').classList.contains('open')) return;
  var r = RUNS.find(function(x) { return x.id === runId; });
  if (!r || !r.gps_polyline || r.gps_polyline.length < 5 || typeof L === 'undefined') return;
  overlayRunId = runId;

  document.getElementById('ro-title').textContent = r.name + ' · ' + r.date_long;
  document.getElementById('ro-stats').innerHTML = roStats(r);

  var avail = availableMetrics(r.dist_stream || []);
  document.getElementById('ro-tabs').innerHTML = avail.map(function(m, i) {
    return '<button class="dist-tab' + (i === 0 ? ' active' : '') +
      '" data-metric="' + m.key + '" onclick="selectOverlayMetric(\'' + m.key + '\')">' +
      m.label + '</button>';
  }).join('');
  document.getElementById('ro-splits').innerHTML =
    renderKmSplits(r) + renderBestEfforts(r.best_efforts) + renderRunSegments(r);

  // Start each overlay at the full-run view rather than inheriting the last
  // run's zoom window (the holder is reused across runs).
  var roHolder = document.getElementById('ro-chart');
  if (roHolder) roHolder._zoom = null;

  document.getElementById('run-overlay').classList.add('open');

  setTimeout(function() {
    try {
      if (overlayMap) { overlayMap.remove(); overlayMap = null; }
      var el = document.getElementById('ro-map');
      overlayMap = L.map(el, {zoomControl: true, preferCanvas: true});
      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: 'abcd', maxZoom: 19
      }).addTo(overlayMap);
      var bounds = drawRunRoute(overlayMap, r);
      addPauseMarkers(overlayMap, r);
      activeCursorMarker = L.circleMarker(r.gps_polyline[0], {
        radius: 6, color: '#fff', fillColor: '#e07020', weight: 2,
        opacity: 0, fillOpacity: 0, pane: 'markerPane'
      }).addTo(overlayMap);
      // invalidateSize first so Leaflet knows the real container size, then fit.
      overlayMap.invalidateSize();
      overlayMap.fitBounds(bounds, {padding: [24, 24]});
      if (avail.length) selectOverlayMetric(avail[0].key);
      // Re-fit once the flex layout has fully settled — the first paint can
      // report a stale container height, leaving the route partly off-screen.
      setTimeout(function() {
        if (overlayMap) { overlayMap.invalidateSize(); overlayMap.fitBounds(bounds, {padding: [24, 24]}); }
      }, 180);
    } catch(e) { console.warn('Overlay map init failed:', e); }
  }, 60);
}

function closeRunOverlay() {
  document.getElementById('run-overlay').classList.remove('open');
  if (overlayMap) { overlayMap.remove(); overlayMap = null; }
  activeCursorMarker = null;
  overlayRunId = null;
}

function selectOverlayMetric(metricKey) {
  if (overlayRunId === null) return;
  document.querySelectorAll('#ro-tabs .dist-tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.metric === metricKey);
  });
  drawDistMetric(overlayRunId, metricKey, {holderId: 'ro-chart', interactive: true});
}

function moveRunCursor(lat, lon) {
  if (!activeCursorMarker) return;
  if (lat == null || lon == null) { activeCursorMarker.setStyle({opacity: 0, fillOpacity: 0}); return; }
  activeCursorMarker.setLatLng([lat, lon]);
  activeCursorMarker.setStyle({opacity: 1, fillOpacity: 1});
}

function hideRunCursor() {
  if (activeCursorMarker) activeCursorMarker.setStyle({opacity: 0, fillOpacity: 0});
}

// Format a pause duration in seconds as m:ss (or h:mm:ss for long stops).
function fmtPauseDur(s) {
  s = Math.round(s);
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  var mm = (h && m < 10 ? '0' : '') + m, ss = (sec < 10 ? '0' : '') + sec;
  return (h ? h + ':' + mm + ':' + ss : m + ':' + ss);
}

// Instantaneous pace uses a forward look-ahead window (STREAM_PACE_WINDOW_M in
// strava_compile.py, ~50 m), so samples this far BEFORE a stop already read as slow.
// The pace chart extends each pause span backward by this much; the route map's
// equivalent extension is precomputed in Python (MAP_PACE_WIN_KM in generate_hub.py,
// kept in sync) so the dotted dip and the dotted route cover the same ground.
var PACE_WIN_KM = 0.05;

// Polyline index ranges [i0, i1] to draw dotted, one per pause. Computed in Python
// (_paused_route_ranges in generate_hub.py) by snapping each pause's stop coord onto
// the run's gps_polyline then stepping forward by the ground covered during the pause,
// anchored sequentially pause-to-pause so index-mapping error can't accumulate across
// a multi-pause interval session.
function pausedRouteRanges(r) {
  return r.pause_ranges || [];
}

// Draw the run's route, breaking the solid green line into dotted amber stretches
// wherever the run moved during a pause (mirrors the dotted pause segments on the
// over-distance chart). Adds start/end markers and returns the route bounds.
function drawRunRoute(map, r) {
  var line = r.gps_polyline;
  var GREEN = {color: '#5cb85c', weight: 3, opacity: 0.9};
  var DOTS  = {color: '#f0ad4e', weight: 3, opacity: 0.9, dashArray: '2,6'};
  var ranges = pausedRouteRanges(r);
  var bounds;
  if (!ranges.length) {
    bounds = L.polyline(line, GREEN).addTo(map).getBounds();
  } else {
    bounds = L.latLngBounds([]);
    var cursor = 0;
    ranges.forEach(function(rg) {
      // Solid up to the pause; ranges share their boundary vertex so the line stays
      // visually continuous across the solid→dotted→solid transitions.
      if (rg[0] > cursor) {
        bounds.extend(L.polyline(line.slice(cursor, rg[0] + 1), GREEN).addTo(map).getBounds());
      }
      bounds.extend(L.polyline(line.slice(rg[0], rg[1] + 1), DOTS).addTo(map).getBounds());
      cursor = Math.max(cursor, rg[1]);
    });
    if (cursor < line.length - 1) {
      bounds.extend(L.polyline(line.slice(cursor), GREEN).addTo(map).getBounds());
    }
  }
  L.circleMarker(line[0], {radius: 5, color: '#5cb85c', fillColor: '#fff', fillOpacity: 1, weight: 2}).addTo(map);
  L.circleMarker(line[line.length - 1], {radius: 5, color: '#e07020', fillColor: '#e07020', fillOpacity: 1, weight: 2}).addTo(map);
  return bounds;
}

// Drop an amber marker where the run paused. The paused stretch of route itself is
// drawn dotted by drawRunRoute (including stop-and-resume-elsewhere gaps, where the
// polyline already hops near-straight across the missing stretch), so no separate
// connector is needed here.
function addPauseMarkers(map, r) {
  if (!r || !r.pauses || !r.pauses.length || typeof L === 'undefined') return;
  r.pauses.forEach(function(p) {
    if (p.lat == null || p.lon == null) return;
    L.circleMarker([p.lat, p.lon], {
      radius: 5, color: '#f0ad4e', fillColor: '#f0ad4e', fillOpacity: 0.9, weight: 2, pane: 'markerPane'
    }).bindTooltip('Paused ' + fmtPauseDur(p.dur), {direction: 'top'}).addTo(map);
  });
}

// Switch tabs and mirror the choice in the URL hash so a refresh restores it.
// `updateHash` is false when we're restoring from the hash on load.
function selectTab(id, updateHash) {
  var panel = document.getElementById('panel-' + id);
  var btn = document.querySelector('.tab[data-panel="' + id + '"]');
  if (!panel || !btn) return;
  document.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('active'); });
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  panel.classList.add('active');
  btn.classList.add('active');
  if (updateHash !== false) location.hash = id;
  if (id === 'overview' && overviewMap) {
    setTimeout(function() { overviewMap.invalidateSize(); }, 50);
  }
  if (id === 'runs' && selectedRunId !== null) {
    var r = RUNS.find(function(x) { return x.id === selectedRunId; });
    if (r) initRunMap(r);
  }
  if (id === 'segments') initSegments();
}

document.querySelectorAll('.tab').forEach(function(btn) {
  btn.addEventListener('click', function() { selectTab(btn.dataset.panel); });
});

// Restore the tab named in the URL hash (e.g. #segments) on load.
(function() {
  var id = (location.hash || '').replace(/^#/, '');
  if (id && document.getElementById('panel-' + id)) selectTab(id, false);
})();

// ── Benchmark segments ────────────────────────────────────────────────────

var segMaps = {};
var segInit = false;

function initSegments() {
  if (segInit || typeof L === 'undefined' || !SEGMENTS || !SEGMENTS.length) {
    // Hidden Leaflet maps need a size recompute once their panel is shown.
    Object.keys(segMaps).forEach(function(k) { segMaps[k].invalidateSize(); });
    return;
  }
  segInit = true;
  setTimeout(function() {
    SEGMENTS.forEach(function(seg) {
      try { initSegMap(seg); } catch(e) { console.warn('seg map failed', e); }
      try { drawSegTrend(seg); } catch(e) { console.warn('seg trend failed', e); }
    });
  }, 50);
}

// ── Segment starring + type/starred filtering ─────────────────────────────
// Stars live in localStorage (instant, survive reloads) keyed by each segment's
// stable geo_key, so they stick to the same route across rebuilds even though the
// positional id shifts. The Python build also reads cache/segment_stars.json and
// pre-stars matching cards; localStorage is authoritative once it exists and is
// seeded from those pre-starred cards on the first load in a given browser.
var SEG_STAR_KEY = 'strava.segStars';
var SEG_STARONLY_KEY = 'strava.segStarredOnly';
var SEG_RANGE_KEY = 'strava.segRange';
var activeSegType = 'all';
// Trend window: how far back the performance charts scale. Anchored at each
// segment's most recent attempt, so the window is never empty. 'max' = full history.
var activeSegRange = 'max';
var SEG_RANGES = [
  {key: '1m', label: '1M', days: 30},
  {key: '3m', label: '3M', days: 91},
  {key: '6m', label: '6M', days: 183},
  {key: '1y', label: '1Y', days: 365},
  {key: 'max', label: 'Max', days: null}
];
var segStarredOnly = false;
var segStars = null;                // lazily-initialised Set of starred geo_keys

function loadSegStars() {
  try {
    var raw = localStorage.getItem(SEG_STAR_KEY);
    if (raw != null) return new Set(JSON.parse(raw));
  } catch (e) {}
  var seed = [];
  document.querySelectorAll('#panel-segments .seg-card .seg-star.on').forEach(function(b) {
    var card = b.closest('.seg-card');
    if (card && card.dataset.geo) seed.push(card.dataset.geo);
  });
  var set = new Set(seed);
  saveSegStars(set);
  return set;
}

function saveSegStars(set) {
  try { localStorage.setItem(SEG_STAR_KEY, JSON.stringify(Array.from(set))); } catch (e) {}
}

function toggleSegStar(btn) {
  if (!segStars) segStars = loadSegStars();
  var card = btn.closest('.seg-card');
  if (!card) return;
  var key = card.dataset.geo || '';
  if (segStars.has(key)) {
    segStars.delete(key); btn.classList.remove('on'); btn.setAttribute('aria-pressed', 'false');
  } else {
    segStars.add(key); btn.classList.add('on'); btn.setAttribute('aria-pressed', 'true');
  }
  saveSegStars(segStars);
  applySegFilters();
}

function applySegFilters() {
  if (!segStars) segStars = loadSegStars();
  document.querySelectorAll('#panel-segments .seg-card').forEach(function(card) {
    var okType = activeSegType === 'all' || card.dataset.type === activeSegType;
    var okStar = !segStarredOnly || segStars.has(card.dataset.geo || '');
    card.classList.toggle('seg-hidden', !(okType && okStar));
  });
}

// Type chips (All / Segment / Climb / Loop), only for types that actually occur.
function buildSegChips() {
  var bar = document.getElementById('seg-chips');
  if (!bar || typeof SEGMENTS === 'undefined' || !SEGMENTS) return;
  var counts = {};
  SEGMENTS.forEach(function(s) { counts[s.type] = (counts[s.type] || 0) + 1; });
  var defs = [{key: 'all', label: 'All'}, {key: 'segment', label: 'Segment'},
              {key: 'climb', label: 'Climb'}, {key: 'loop', label: 'Loop'}];
  bar.innerHTML = defs.filter(function(d) { return d.key === 'all' || counts[d.key]; })
    .map(function(d) {
      var n = d.key === 'all' ? SEGMENTS.length : counts[d.key];
      return '<span class="chip' + (d.key === activeSegType ? ' active' : '') +
        '" data-type="' + d.key + '">' + d.label + ' ' + n + '</span>';
    }).join('');
  bar.querySelectorAll('.chip').forEach(function(chip) {
    chip.addEventListener('click', function() {
      activeSegType = this.getAttribute('data-type');
      bar.querySelectorAll('.chip').forEach(function(c) { c.classList.remove('active'); });
      this.classList.add('active');
      applySegFilters();
    });
  });
}

// Download the current stars as segment_stars.json for the user to drop into cache/
// so a full rebuild preserves them (the browser can't write there itself over file://).
function exportSegStars() {
  if (!segStars) segStars = loadSegStars();
  var blob = new Blob([JSON.stringify(Array.from(segStars), null, 2)], {type: 'application/json'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = 'segment_stars.json';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
}

function initSegControls() {
  if (!document.getElementById('seg-chips')) return;   // empty-state panel: nothing to wire
  segStars = loadSegStars();
  // localStorage is authoritative once seeded, so reconcile every card's button to it.
  document.querySelectorAll('#panel-segments .seg-card').forEach(function(card) {
    var btn = card.querySelector('.seg-star');
    if (!btn) return;
    var on = segStars.has(card.dataset.geo || '');
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  buildSegChips();
  var tog = document.getElementById('seg-star-toggle');
  try { segStarredOnly = localStorage.getItem(SEG_STARONLY_KEY) === '1'; } catch (e) {}
  if (tog) {
    tog.classList.toggle('active', segStarredOnly);
    tog.addEventListener('click', function() {
      segStarredOnly = !segStarredOnly;
      this.classList.toggle('active', segStarredOnly);
      try { localStorage.setItem(SEG_STARONLY_KEY, segStarredOnly ? '1' : '0'); } catch (e) {}
      applySegFilters();
    });
  }
  var exp = document.getElementById('seg-export');
  if (exp) exp.addEventListener('click', exportSegStars);
  initSegRange();
  applySegFilters();
}

// Days for the active window, or null for 'Max' (full history).
function segRangeDays() {
  for (var i = 0; i < SEG_RANGES.length; i++) {
    if (SEG_RANGES[i].key === activeSegRange) return SEG_RANGES[i].days;
  }
  return null;
}

// Re-scale every trend chart to the current window. Cheap enough to run on each
// toggle; thumbnails clip to the window, the open overlay re-renders scrollable.
function refreshSegTrends() {
  if (typeof SEGMENTS === 'undefined' || !SEGMENTS) return;
  SEGMENTS.forEach(function(seg) {
    try { drawSegTrend(seg); } catch (e) {}
  });
  if (segOverlayOpenId != null) {
    var s = SEGMENTS.find(function(x) { return x.id === segOverlayOpenId; });
    if (s) renderSegTrend(s, document.getElementById('so-trend'),
                          {W: 440, H: 190, tipId: 'so-tip', legend: true, scroll: true});
  }
}

function initSegRange() {
  var bar = document.getElementById('seg-range');
  if (!bar) return;
  try { var saved = localStorage.getItem(SEG_RANGE_KEY); if (saved) activeSegRange = saved; } catch (e) {}
  bar.querySelectorAll('.chip').forEach(function(chip) {
    chip.classList.toggle('active', chip.getAttribute('data-range') === activeSegRange);
    chip.addEventListener('click', function() {
      activeSegRange = this.getAttribute('data-range');
      try { localStorage.setItem(SEG_RANGE_KEY, activeSegRange); } catch (e) {}
      var self = this;
      bar.querySelectorAll('.chip').forEach(function(c) { c.classList.toggle('active', c === self); });
      refreshSegTrends();
    });
  });
}

// Start = green disc, End = checkered finish flag. divIcons so the same markup
// styles both the thumbnail and the expanded overlay map (sized via `px`).
function addSegEndpoints(map, poly, px) {
  if (!poly || poly.length < 2) return;
  function mk(latlng, cls) {
    return L.marker(latlng, {
      interactive: false, keyboard: false,
      icon: L.divIcon({
        className: '',
        html: '<div class="seg-mk ' + cls + '"></div>',
        iconSize: [px, px], iconAnchor: [px / 2, px / 2]
      })
    }).addTo(map);
  }
  mk(poly[0], 'seg-mk-start');
  mk(poly[poly.length - 1], 'seg-mk-end');
}

function initSegMap(seg) {
  var el = document.getElementById('seg-map-' + seg.id);
  if (!el || !seg.polyline || seg.polyline.length < 2) return;
  var map = L.map(el, {zoomControl: false, attributionControl: false,
                       dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
                       boxZoom: false, keyboard: false, tap: false, preferCanvas: true});
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd', maxZoom: 19
  }).addTo(map);
  var colour = seg.type === 'climb' ? '#e0a020' : (seg.type === 'segment' ? '#5a9fd4' : '#5cb85c');
  var poly = L.polyline(seg.polyline, {color: colour, weight: 3, opacity: 0.95}).addTo(map);
  addSegEndpoints(map, seg.polyline, 16);
  map.invalidateSize();
  map.fitBounds(poly.getBounds(), {padding: [12, 12]});
  segMaps[seg.id] = map;
}

function drawSegTrend(seg) {
  renderSegTrend(seg, document.getElementById('seg-trend-' + seg.id),
                 {W: 360, H: 150, tipId: 'seg-tip-' + seg.id});
}

function renderSegTrend(seg, holder, opts) {
  if (!holder) return;
  opts = opts || {};
  var scroll = !!opts.scroll;
  var all = seg.efforts.slice().sort(function(a, b) {
    return a.date_iso < b.date_iso ? -1 : (a.date_iso > b.date_iso ? 1 : 0);
  });
  if (all.length < 2) { holder.innerHTML = ''; return; }

  var tipId = opts.tipId || ('seg-tip-' + seg.id);
  var W = opts.W || 360, H = opts.H || 150, padL = 40, padR = 10, padT = 14, padB = 22;
  if (scroll && holder.clientWidth) W = holder.clientWidth;   // fit the window to the real box
  var innerW = W - padL - padR;

  var tminAll = Date.parse(all[0].date_iso), tmaxAll = Date.parse(all[all.length - 1].date_iso);
  if (tmaxAll === tminAll) tmaxAll = tminAll + 1;
  var days = (opts.rangeDays !== undefined) ? opts.rangeDays : segRangeDays();
  var windowMs = days ? days * 86400000 : null;
  var spanAll = tmaxAll - tminAll;

  // Pick the plotted efforts, the x-domain, and the drawing width:
  //  - overlay with more history than the window: draw everything wide, scroll to the latest;
  //  - thumbnail with a window: clip to the most recent window (anchored at the last attempt);
  //  - otherwise (Max, or history shorter than the window): the full range fills the chart.
  var efforts, domMin, domMax, contentW, scrollMode = false;
  if (scroll && windowMs && spanAll > windowMs) {
    scrollMode = true;
    efforts = all;
    domMin = tminAll; domMax = tmaxAll;
    contentW = innerW * (spanAll / windowMs);
  } else if (!scroll && windowMs) {
    domMin = Math.max(tmaxAll - windowMs, tminAll); domMax = tmaxAll;
    efforts = all.filter(function(e) { return Date.parse(e.date_iso) >= domMin; });
    contentW = innerW;
  } else {
    efforts = all;
    domMin = tminAll; domMax = tmaxAll;
    contentW = innerW;
  }
  if (domMax === domMin) domMax = domMin + 1;
  var VBW = padL + contentW + padR;   // logical/pixel width of the drawn chart

  var ts = efforts.map(function(e) { return Date.parse(e.date_iso); });
  var ys = efforts.map(function(e) { return e.time_s; });
  var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
  // Pad the time axis so faster (smaller) times sit higher with a little headroom.
  var ylo = ymin - (ymax - ymin) * 0.12 - 1, yhi = ymax + (ymax - ymin) * 0.12 + 1;

  function px(t) { return padL + (t - domMin) / (domMax - domMin) * contentW; }
  function py(v) { return padT + (v - ylo) / (yhi - ylo) * (H - padT - padB); } // smaller time = higher

  var prT = seg.pr_time_s;
  // Gridlines span the full drawn width; labels are kept separate so the scroll view can
  // pin them to a fixed left axis instead of letting them scroll away.
  var gridLines = '', gridLabels = '';
  for (var g = 0; g <= 2; g++) {
    var vy = padT + g / 2 * (H - padT - padB);
    var val = ylo + g / 2 * (yhi - ylo);  // match py(): fastest (small) sits at the top
    gridLines += '<line x1="' + padL + '" y1="' + vy.toFixed(1) + '" x2="' + (VBW - padR) +
      '" y2="' + vy.toFixed(1) + '" stroke="#2a2a2a" stroke-width="1"/>';
    gridLabels += '<text x="' + (padL - 5) + '" y="' + (vy + 3).toFixed(1) +
      '" font-size="9" fill="#555" text-anchor="end">' + fmtSegTime(val) + '</text>';
  }
  // One connecting line per run type, so attempts only join others of the same kind.
  var byType = {};
  efforts.forEach(function(e, i) {
    var k = e.run_type || 'misc';
    (byType[k] || (byType[k] = [])).push(i);
  });
  var lines = RUN_TYPES.filter(function(t) { return (byType[t.key] || []).length > 1; })
    .map(function(t) {
      var d = byType[t.key].map(function(i, j) {
        return (j ? 'L' : 'M') + px(ts[i]).toFixed(1) + ',' + py(ys[i]).toFixed(1);
      }).join(' ');
      return '<path d="' + d + '" fill="none" stroke="' + (RUN_TYPE_COLOR[t.key] || '#888') +
        '" stroke-width="1.6" opacity="0.85"/>';
    }).join('');
  var dots = efforts.map(function(e, i) {
    var isPr = e.time_s === prT;
    var col = RUN_TYPE_COLOR[e.run_type] || '#888';
    var cx = px(ts[i]).toFixed(1), cy = py(ys[i]).toFixed(1);
    // Visible dot coloured by run type, plus a larger transparent circle so hovering is
    // forgiving. The PR keeps a bigger radius and a gold ring so it stays obvious.
    return '<circle cx="' + cx + '" cy="' + cy +
      '" r="' + (isPr ? 4.5 : 3) + '" fill="' + col +
      '" stroke="' + (isPr ? '#ffd700' : '#1c1c1c') +
      '" stroke-width="' + (isPr ? 2 : 1.5) + '" pointer-events="none"/>' +
      '<circle cx="' + cx + '" cy="' + cy + '" r="11" fill="transparent"' +
      ' data-d="' + esc(e.date_long) + '" data-t="' + esc(e.time_str) + '"' +
      ' data-p="' + esc(e.pace_str || '') + '"' +
      ' data-rt="' + esc(RUN_TYPE_LABEL[e.run_type] || '') + '"' +
      (isPr ? ' data-pr="1"' : '') +
      ' class="seg-dot" style="cursor:pointer"/>';
  }).join('');
  // date labels at first and last plotted attempt
  var xlab = '<text x="' + padL + '" y="' + (H - 7) + '" font-size="9" fill="#555" text-anchor="start">' +
      shortDate(efforts[0].date_iso) + '</text>' +
    '<text x="' + (VBW - padR) + '" y="' + (H - 7) + '" font-size="9" fill="#555" text-anchor="end">' +
      shortDate(efforts[efforts.length - 1].date_iso) + '</text>';

  // Legend (overlay only): only the run types actually present, in RUN_TYPES order.
  var legend = '';
  if (opts.legend) {
    var present = {};
    efforts.forEach(function(e) { present[e.run_type || 'misc'] = 1; });
    var items = RUN_TYPES.filter(function(t) { return present[t.key]; }).map(function(t) {
      return '<span class="seg-leg-item"><i style="background:' +
        (RUN_TYPE_COLOR[t.key] || '#888') + '"></i>' + t.label + '</span>';
    }).join('');
    legend = '<div class="seg-trend-legend">' + items + '</div>';
  }

  if (scrollMode) {
    // Chart drawn wider than its box; a horizontal scrollbar reaches earlier attempts.
    // The y-axis labels sit in a separate pinned SVG so they don't scroll off.
    holder.innerHTML =
      '<div class="seg-trend-scroll">' +
        '<svg width="' + VBW + '" height="' + H + '" viewBox="0 0 ' + VBW + ' ' + H +
        '" style="width:' + VBW + 'px;height:' + H + 'px" xmlns="http://www.w3.org/2000/svg">' +
        gridLines + xlab + lines + dots + '</svg>' +
      '</div>' +
      '<svg class="seg-trend-yaxis" width="' + padL + '" height="' + H +
        '" viewBox="0 0 ' + padL + ' ' + H + '" style="width:' + padL + 'px;height:' + H + 'px">' +
        '<rect x="0" y="0" width="' + padL + '" height="' + H + '" fill="#1c1c1c"/>' +
        gridLabels + '</svg>' +
      legend +
      '<div class="seg-trend-tip" id="' + tipId + '"></div>';
    var sc = holder.querySelector('.seg-trend-scroll');
    if (sc) sc.scrollLeft = sc.scrollWidth;   // open on the most recent window
  } else {
    holder.innerHTML =
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">' +
      gridLines + gridLabels + xlab + lines +
      dots + '</svg>' + legend +
      '<div class="seg-trend-tip" id="' + tipId + '"></div>';
  }

  var tip = document.getElementById(tipId);
  holder.querySelectorAll('.seg-dot').forEach(function(c) {
    c.addEventListener('mouseenter', function() {
      var r = holder.getBoundingClientRect();
      var cr = c.getBoundingClientRect();
      var pace = c.getAttribute('data-p');
      var rt = c.getAttribute('data-rt');
      tip.innerHTML = '<b>' + c.getAttribute('data-t') +
        (c.getAttribute('data-pr') ? ' 🏆' : '') + '</b>' +
        (pace ? ' · ' + pace + '/km' : '') +
        '<br>' + c.getAttribute('data-d') +
        (rt ? '<br><span style="color:#888">' + rt + '</span>' : '');
      tip.style.left = (cr.left - r.left + cr.width / 2) + 'px';
      tip.style.top = (cr.top - r.top + cr.height / 2) + 'px';
      tip.style.opacity = '1';
    });
    c.addEventListener('mouseleave', function() { tip.style.opacity = '0'; });
  });
}

// Elevation-over-distance profile for one segment's reference lap. Area chart with a
// vertical cursor that snaps to the nearest sample on hover.
function renderElevProfile(seg, holder, opts) {
  if (!holder) return;
  opts = opts || {};
  var prof = seg.elev_profile || [];
  if (prof.length < 2) {
    holder.innerHTML = '<div class="so-elev-empty">No elevation data for this segment</div>';
    return;
  }
  var W = opts.W || 440, H = opts.H || 150, padL = 40, padR = 10, padT = 12, padB = 20;
  var tipId = opts.tipId || 'so-elev-tip';
  var ds = prof.map(function(p) { return p[0]; });
  var es = prof.map(function(p) { return p[1]; });
  // Cumulative length of the drawn route, so a distance on the chart maps to a point on the map.
  var mapPoly = seg.polyline || [];
  var cum = [0], totalLen = 0;
  if (typeof L !== 'undefined') {
    for (var ci = 1; ci < mapPoly.length; ci++) {
      totalLen += L.latLng(mapPoly[ci - 1]).distanceTo(L.latLng(mapPoly[ci]));
      cum.push(totalLen);
    }
  }
  var dmin = ds[0], dmax = Math.max.apply(null, ds);
  var emin = Math.min.apply(null, es), emax = Math.max.apply(null, es);
  if (dmax === dmin) dmax = dmin + 1;
  var span = emax - emin;
  var elo = emin - span * 0.12 - 0.5, ehi = emax + span * 0.12 + 0.5;
  if (ehi === elo) ehi = elo + 1;

  function px(d) { return padL + (d - dmin) / (dmax - dmin) * (W - padL - padR); }
  function py(e) { return padT + (ehi - e) / (ehi - elo) * (H - padT - padB); }

  var grid = '';
  for (var g = 0; g <= 2; g++) {
    var val = elo + g / 2 * (ehi - elo);
    var vy = py(val);
    grid += '<line x1="' + padL + '" y1="' + vy.toFixed(1) + '" x2="' + (W - padR) +
      '" y2="' + vy.toFixed(1) + '" stroke="#2a2a2a" stroke-width="1"/>' +
      '<text x="' + (padL - 5) + '" y="' + (vy + 3).toFixed(1) +
      '" font-size="9" fill="#555" text-anchor="end">' + Math.round(val) + 'm</text>';
  }
  var lpath = prof.map(function(p, i) {
    return (i ? 'L' : 'M') + px(p[0]).toFixed(1) + ',' + py(p[1]).toFixed(1);
  }).join(' ');
  var area = 'M' + px(dmin).toFixed(1) + ',' + (H - padB).toFixed(1) + ' ' +
    prof.map(function(p) { return 'L' + px(p[0]).toFixed(1) + ',' + py(p[1]).toFixed(1); }).join(' ') +
    ' L' + px(dmax).toFixed(1) + ',' + (H - padB).toFixed(1) + ' Z';
  var xlab = '<text x="' + padL + '" y="' + (H - 6) + '" font-size="9" fill="#555" text-anchor="start">0</text>' +
    '<text x="' + (W - padR) + '" y="' + (H - 6) + '" font-size="9" fill="#555" text-anchor="end">' +
      (dmax / 1000).toFixed(2) + ' km</text>';

  holder.innerHTML =
    '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">' +
    grid + xlab +
    '<path d="' + area + '" fill="#8a6db5" fill-opacity="0.16" stroke="none"/>' +
    '<path d="' + lpath + '" fill="none" stroke="#8a6db5" stroke-width="1.6" opacity="0.95"/>' +
    '<line id="' + tipId + '-cur" x1="0" y1="' + padT + '" x2="0" y2="' + (H - padB) +
      '" stroke="#8a6db5" stroke-width="1" opacity="0"/>' +
    '<rect x="' + padL + '" y="' + padT + '" width="' + (W - padL - padR) + '" height="' + (H - padT - padB) +
      '" fill="transparent" class="so-elev-hit" style="cursor:crosshair"/>' +
    '</svg>' +
    '<div class="seg-trend-tip" id="' + tipId + '"></div>';

  var tip = document.getElementById(tipId);
  var cursor = document.getElementById(tipId + '-cur');
  var hit = holder.querySelector('.so-elev-hit');
  var svg = holder.querySelector('svg');
  hit.addEventListener('mousemove', function(ev) {
    var rect = svg.getBoundingClientRect();
    var fx = (ev.clientX - rect.left) / rect.width * W;
    var d = dmin + (fx - padL) / (W - padL - padR) * (dmax - dmin);
    d = Math.max(dmin, Math.min(dmax, d));
    var best = 0, bd = Infinity;
    for (var i = 0; i < ds.length; i++) {
      var dd = Math.abs(ds[i] - d);
      if (dd < bd) { bd = dd; best = i; }
    }
    var sx = px(ds[best]), sy = py(es[best]);
    cursor.setAttribute('x1', sx.toFixed(1));
    cursor.setAttribute('x2', sx.toFixed(1));
    cursor.setAttribute('opacity', '1');
    var hr = holder.getBoundingClientRect();
    tip.innerHTML = '<b>' + Math.round(es[best]) + ' m</b> · ' + (ds[best] / 1000).toFixed(2) + ' km';
    tip.style.left = (sx / W * hr.width) + 'px';
    tip.style.top = (sy / H * hr.height) + 'px';
    tip.style.opacity = '1';
    // Mirror the cursor onto the route map at the matching distance along the line.
    if (typeof L !== 'undefined' && segOverlayMap && mapPoly.length >= 2 && totalLen > 0) {
      var frac = (ds[best] - dmin) / (dmax - dmin);
      var ll = latlngAtCum(mapPoly, cum, frac * totalLen);
      if (!segElevMarker) {
        segElevMarker = L.circleMarker(ll, {radius: 6, color: '#fff', weight: 2,
          fillColor: '#8a6db5', fillOpacity: 1, pane: 'markerPane'}).addTo(segOverlayMap);
      } else {
        segElevMarker.setLatLng(ll);
        if (!segOverlayMap.hasLayer(segElevMarker)) segElevMarker.addTo(segOverlayMap);
      }
    }
  });
  hit.addEventListener('mouseleave', function() {
    tip.style.opacity = '0';
    cursor.setAttribute('opacity', '0');
    if (segElevMarker && segOverlayMap && segOverlayMap.hasLayer(segElevMarker)) {
      segOverlayMap.removeLayer(segElevMarker);
    }
  });
}

// Point on a route polyline at a given cumulative distance (metres), linearly interpolated.
function latlngAtCum(poly, cum, target) {
  for (var j = 1; j < poly.length; j++) {
    if (cum[j] >= target) {
      var segLen = cum[j] - cum[j - 1];
      var t = segLen > 0 ? (target - cum[j - 1]) / segLen : 0;
      return [poly[j - 1][0] + (poly[j][0] - poly[j - 1][0]) * t,
              poly[j - 1][1] + (poly[j][1] - poly[j - 1][1]) * t];
    }
  }
  return poly[poly.length - 1];
}

var segOverlayMap = null;
var segElevMarker = null;
var segOverlayOpenId = null;

function openSegOverlay(id) {
  var seg = SEGMENTS.find(function(s) { return s.id === id; });
  if (!seg) return;
  segOverlayOpenId = id;
  document.getElementById('so-title').textContent = seg.name || 'Segment';

  var bits = [
    '<span>' + esc(seg.length_str) + '</span>',
    '<span><b>' + seg.n_efforts + '</b> attempts</span>',
    '<span>PR <b>' + esc(seg.pr_time_str) + '</b> · ' + esc(seg.pr_date) + '</span>'
  ];
  if (seg.type === 'climb') {
    bits.splice(1, 0, '<span>&uarr; ' + seg.gain_m + ' m · ' + seg.grade + '%</span>');
  }
  document.getElementById('so-stats').innerHTML = bits.join('');
  document.getElementById('so-attempts').innerHTML = segAttemptList(seg);

  document.getElementById('seg-overlay').classList.add('open');
  setTimeout(function() {
    var el = document.getElementById('so-map');
    if (segOverlayMap) { segOverlayMap.remove(); segOverlayMap = null; }
    segElevMarker = null;
    el.innerHTML = '';
    if (typeof L === 'undefined' || !seg.polyline || seg.polyline.length < 2) return;
    var map = L.map(el, {preferCanvas: true});
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      subdomains: 'abcd', maxZoom: 19
    }).addTo(map);
    var colour = seg.type === 'climb' ? '#e0a020' : (seg.type === 'segment' ? '#5a9fd4' : '#5cb85c');
    var poly = L.polyline(seg.polyline, {color: colour, weight: 4, opacity: 0.95}).addTo(map);
    addSegEndpoints(map, seg.polyline, 22);
    map.invalidateSize();
    map.fitBounds(poly.getBounds(), {padding: [30, 30]});
    segOverlayMap = map;
    renderSegTrend(seg, document.getElementById('so-trend'), {W: 440, H: 190, tipId: 'so-tip', legend: true, scroll: true});
    renderElevProfile(seg, document.getElementById('so-elev'), {W: 440, H: 150, tipId: 'so-elev-tip'});
  }, 60);
}

function closeSegOverlay() {
  document.getElementById('seg-overlay').classList.remove('open');
  if (segOverlayMap) { segOverlayMap.remove(); segOverlayMap = null; }
  segElevMarker = null;
  segOverlayOpenId = null;
}

// Compact run stats bar reused as the summary header on the expanded run overlay.
function roStats(r) {
  var bits = [
    '<span><b>' + r.dist_km + '</b> km</span>',
    '<span><b>' + r.moving_str + '</b></span>',
    '<span>' + r.pace_str + '/km</span>'
  ];
  if (r.ga_pace_str && r.ga_pace_str !== '—') bits.push('<span>GA ' + r.ga_pace_str + '/km</span>');
  if (r.gain > 0) bits.push('<span>&uarr; ' + r.gain + ' m</span>');
  if (r.cadence) bits.push('<span>' + r.cadence + ' spm</span>');
  var ps = pauseSummary(r);
  if (ps) bits.push('<span style="color:#f0ad4e">&#9208; ' + ps + '</span>');
  return bits.join('');
}

// "2 pauses · 3:45 total" for runs that paused, else ''. Used in the stats rows
// to make the map/graph pause marks discoverable.
function pauseSummary(r) {
  if (!r.pauses || !r.pauses.length) return '';
  var total = r.pauses.reduce(function(a, p) { return a + p.dur; }, 0);
  var n = r.pauses.length;
  return n + ' pause' + (n > 1 ? 's' : '') + ' · ' + fmtPauseDur(total) + ' total';
}

// Attempt list inside the segment overlay: each row is one run that ran the segment.
// Clicking a row opens that run's expanded analysis (same screen as the run page map).
function segAttemptList(seg) {
  var efforts = seg.efforts.slice().sort(function(a, b) {
    return a.date_iso < b.date_iso ? 1 : (a.date_iso > b.date_iso ? -1 : 0);
  });
  return '<div class="so-att-head">' + efforts.length + ' attempts · newest first</div>' +
    efforts.map(function(e) {
      var isPr = e.time_s === seg.pr_time_s;
      var r = RUNS.find(function(x) { return x.id === e.run_id; });
      var clickable = r && r.gps_polyline && r.gps_polyline.length >= 5;
      return '<div class="so-att' + (isPr ? ' so-att-pr' : '') + (clickable ? '' : ' so-att-dead') + '"' +
        (clickable ? ' onclick="openRunFromSeg(' + e.run_id + ')" title="Open run analysis"' : '') + '>' +
        '<div class="so-att-row">' +
          '<span class="so-att-time">' + e.time_str + (isPr ? ' 🏆' : '') + '</span>' +
          '<span class="so-att-name">' + esc(e.name) + '</span>' +
        '</div>' +
        '<div class="so-att-row so-att-sub">' +
          '<span>' + e.date_long + '</span>' +
          '<span>' + e.pace_str + '/km' + (e.ga_pace_str && e.ga_pace_str !== '—' ? ' · GA ' + e.ga_pace_str : '') + '</span>' +
        '</div>' +
      '</div>';
    }).join('');
}

function openRunFromSeg(runId) {
  // Layered above the segment overlay; closing the run overlay returns here.
  openRunOverlay(runId);
}

function fmtSegTime(s) {
  s = Math.max(0, Math.round(s));
  var m = Math.floor(s / 60), sec = s % 60;
  return m + ':' + (sec < 10 ? '0' : '') + sec;
}

function shortDate(iso) {
  var d = new Date(iso);
  return d.toLocaleDateString('en-AU', {month: 'short', year: '2-digit'});
}

var ZONE_COLORS = ['#3a7a3a', '#5cb85c', '#e0a020', '#e07020', '#d9534f'];
var ZONE_NAMES  = ['Z1 Easy', 'Z2 Endurance', 'Z3 Tempo', 'Z4 Threshold', 'Z5 Speed'];

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function paceZoneIdx(pace_s) {
  // pace_s = seconds per km; THRESHOLD_S_KM = threshold pace in secs/km
  if (!THRESHOLD_S_KM || !pace_s) return 0;
  // frac = lap_speed / threshold_speed = threshold_s_km / lap_s_km
  var frac = THRESHOLD_S_KM / pace_s;
  if (frac < 0.77) return 0;
  if (frac < 0.87) return 1;
  if (frac < 0.93) return 2;
  if (frac < 1.03) return 3;
  return 4;
}

// ── Run list ──────────────────────────────────────────────────────────────

// Category metadata, ordered as it appears in the filter bar. The key matches
// run_type from _classify_run(); cls is the CSS suffix for the badge/chip colour.
var RUN_TYPES = [
  {key: 'recovery',  label: 'Recovery'},
  {key: 'easy',      label: 'Easy'},
  {key: 'long',      label: 'Long'},
  {key: 'tempo',     label: 'Tempo'},
  {key: 'threshold', label: 'Threshold'},
  {key: 'intervals', label: 'Intervals'},
  {key: 'race',      label: 'Race'},
  {key: 'misc',      label: 'Misc'}
];
var RUN_TYPE_LABEL = RUN_TYPES.reduce(function(m, t) { m[t.key] = t.label; return m; }, {});
// Per-type dot colours for the segment trend chart; mirror the .rt-* CSS palette.
var RUN_TYPE_COLOR = {
  recovery:'#7aa7d6', easy:'#5cb85c', long:'#3fb6a8', tempo:'#e0a020',
  threshold:'#e8772e', intervals:'#9b7ede', race:'#d9534f', misc:'#888'
};

function runTypeBadge(t) {
  if (!t || !RUN_TYPE_LABEL[t]) return '';
  return '<span class="ri-tag rt-' + t + '">' + RUN_TYPE_LABEL[t] + '</span>';
}

function renderRunList(runs) {
  var el = document.getElementById('run-list-items');
  if (!runs.length) {
    el.innerHTML = '<p style="color:#444;padding:1rem;font-size:12px;text-align:center">No runs found</p>';
    return;
  }
  el.innerHTML = runs.map(function(r) {
    return '<div class="run-item' + (r.is_interval ? ' is-interval' : '') +
      '" data-id="' + r.id + '" onclick="showRun(' + r.id + ')">' +
      '<div class="ri-top">' +
        '<span class="ri-date">' + r.weekday.slice(0,3) + ' · ' + r.date_long + '</span>' +
        '<span class="ri-dist">' + r.dist_km + ' km</span>' +
      '</div>' +
      '<div class="ri-name">' + esc(r.name) + '</div>' +
      '<div class="ri-bottom">' +
        '<span class="ri-pace">' + r.pace_str + '</span>' +
        (r.gain > 10 ? '<span class="ri-gain">↑' + r.gain + 'm</span>' : '') +
        runTypeBadge(r.run_type) +
      '</div></div>';
  }).join('');
}

function showRun(id) {
  selectedRunId = id;
  document.querySelectorAll('.run-item').forEach(function(el) { el.classList.remove('active'); });
  var item = document.querySelector('.run-item[data-id="' + id + '"]');
  if (item) { item.classList.add('active'); item.scrollIntoView({block:'nearest'}); }
  var r = RUNS.find(function(x) { return x.id === id; });
  if (!r) return;

  document.getElementById('run-detail-content').innerHTML = renderDetail(r);
  document.getElementById('run-detail-pane').scrollTop = 0;

  // draw the default over-distance metric if a stream exists
  if (r.dist_stream && r.dist_stream.length) {
    var av = availableMetrics(r.dist_stream);
    if (av.length) drawDistMetric(r.id, av[0].key, {holderId: 'dist-chart-' + r.id, interactive: true});
  }

  // Only init map if the Runs panel is currently visible
  var runsPanel = document.getElementById('panel-runs');
  if (runsPanel && runsPanel.classList.contains('active')) {
    initRunMap(r);
  }
}

// ── Run detail ────────────────────────────────────────────────────────────

function renderDetail(r) {
  var statsHtml =
    '<div class="rd-stats">' +
    stat('Distance', r.dist_km + ' km') +
    stat('Time', r.moving_str) +
    stat('Avg pace', r.pace_str) +
    statMuted('GA pace', r.ga_pace_str) +
    (r.gain > 0 ? stat('Elevation', '↑' + r.gain + ' m ↓' + r.descent + ' m') : '') +
    (r.cadence ? stat('Cadence', r.cadence + ' spm') : '') +
    (r.calories ? stat('Calories', r.calories + ' kcal') : '') +
    (pauseSummary(r) ? stat('Pauses', pauseSummary(r)) : '') +
    kmExtraStats(r) +
    '</div>';

  var mapHtml = (r.gps_polyline && r.gps_polyline.length >= 5)
    ? '<div class="rd-map-wrap" onclick="openRunOverlay(' + r.id + ')">' +
        '<div id="run-map-' + r.id + '" class="rd-map"></div>' +
        '<div class="rd-map-hint">⤢ Click map to expand</div>' +
      '</div>'
    : '';

  return '<div class="rd-top">' +
      '<div class="rd-left">' +
        '<div class="rd-header">' +
          '<h2 class="rd-name">' + esc(r.name) + '</h2>' +
          '<div class="rd-date">' + r.weekday + ', ' + r.date_long + '</div>' +
        '</div>' +
        statsHtml +
      '</div>' +
      renderDesc(r) +
    '</div>' +
    mapHtml +
    renderRunShape(r) +
    renderKmSplits(r) +
    renderPaceZones(r.pace_zones) +
    renderBestEfforts(r.best_efforts) +
    renderDistChart(r);
}

function stat(label, value) {
  return '<div class="rds"><div class="rds-l">' + label + '</div><div class="rds-v">' + value + '</div></div>';
}
function statMuted(label, value) {
  return '<div class="rds"><div class="rds-l">' + label + '</div><div class="rds-v rds-v-muted">' + value + '</div></div>';
}

// ── Strava description (top-right, collapsible) ─────────────────────────────

var DESC_LIMIT = 160;  // chars shown before the "more" toggle kicks in

function descBody(r, expanded) {
  var full = r.description || '';
  var isLong = full.length > DESC_LIMIT;
  var text = (isLong && !expanded) ? full.slice(0, DESC_LIMIT).replace(/\s+$/, '') + '…' : full;
  var btn = isLong
    ? '<button class="rd-desc-btn" onclick="toggleDesc(' + r.id + ')">' + (expanded ? 'less' : 'more') + '</button>'
    : '';
  return '<div class="rd-desc-text">' + esc(text) + '</div>' + btn;
}

function renderDesc(r) {
  if (!r.description) return '';
  return '<div class="rd-desc" id="rd-desc-' + r.id + '" data-expanded="0">' + descBody(r, false) + '</div>';
}

function toggleDesc(id) {
  var el = document.getElementById('rd-desc-' + id);
  if (!el) return;
  var expanded = el.dataset.expanded === '1';
  el.dataset.expanded = expanded ? '0' : '1';
  var r = RUNS.find(function(x) { return x.id === id; });
  if (r) el.innerHTML = descBody(r, !expanded);
}

function kmExtraStats(r) {
  var ks = r.km_splits;
  if (!ks || ks.length < 2) return '';
  var bestKm = ks.reduce(function(b,k){ return (k.pace_s && k.pace_s < b.pace_s) ? k : b; }, ks[0]);
  var out = bestKm.pace_s ? stat('Best km', bestKm.pace_str + ' (km ' + bestKm.km + ')') : '';

  var half = Math.floor(ks.length / 2);
  var first  = ks.slice(0, half);
  var second = ks.slice(half);
  if (first.length && second.length) {
    var fAvg = first.reduce(function(s,k){ return s + (k.pace_s || 0); }, 0) / first.length;
    var sAvg = second.reduce(function(s,k){ return s + (k.pace_s || 0); }, 0) / second.length;
    var diff = Math.round(sAvg - fAvg);
    var label, cls;
    if (diff < -3)      { label = '↗ Neg split ' + Math.abs(diff) + 's'; cls = 'color:#5cb85c'; }
    else if (diff > 3)  { label = '↘ Pos split +' + diff + 's'; cls = 'color:#e07020'; }
    else                { label = '↔ Even split'; cls = 'color:#777'; }
    out += '<div class="rds"><div class="rds-l">Split type</div>' +
           '<div class="rds-v" style="font-size:13px;' + cls + '">' + label + '</div></div>';
  }
  return out;
}

// ── Over-distance metric chart ────────────────────────────────────────────

var DIST_METRICS = [
  {key: 'pace', label: 'Pace',      color: '#5cb85c', invert: true},
  {key: 'elev', label: 'Elevation', color: '#8a6db5', invert: false},
  {key: 'hr',   label: 'Heart rate',color: '#d9534f', invert: false},
  {key: 'cad',  label: 'Cadence',   color: '#5b8fd9', invert: false}
];

function availableMetrics(stream) {
  return DIST_METRICS.filter(function(m) {
    return stream.some(function(p) { return p[m.key] != null; });
  });
}

function fmtMetric(key, v) {
  if (key === 'pace') {
    var mn = Math.floor(v / 60), sc = Math.round(v % 60);
    return mn + ':' + (sc < 10 ? '0' : '') + sc + '/km';
  }
  if (key === 'elev') return Math.round(v) + ' m';
  if (key === 'hr')   return Math.round(v) + ' bpm';
  if (key === 'cad')  return Math.round(v) + ' spm';
  return v;
}

// Each interactive chart stashes its geometry on its own holder element (as
// holder._distState) so the hover handler can invert a mouse x back to a
// distance and look up the point's value + position. Storing it per-holder
// (rather than one global) lets the inline detail chart and the overlay chart
// both stay interactive without clobbering each other's state.

function drawDistMetric(runId, metricKey, opts) {
  opts = opts || {};
  var holderId    = opts.holderId || ('dist-chart-' + runId);
  var interactive = !!opts.interactive;
  var r = RUNS.find(function(x) { return x.id === runId; });
  if (!r || !r.dist_stream || !r.dist_stream.length) return;
  var metric = DIST_METRICS.find(function(m) { return m.key === metricKey; });
  var stream = r.dist_stream;

  var W = 720, H = 200, padL = 44, padR = 12, padT = 12, padB = 24;
  var pts = stream.filter(function(p) { return p[metricKey] != null && p.d != null; });
  if (pts.length < 2) return;

  // Distance-ordered list of points that carry GPS coords (taken from the full
  // stream, not the metric-filtered pts). Used to position the map cursor for
  // hovered points that lack their own lat/lon (GPS dropouts), so it stays
  // monotonic along the route instead of snapping into the count-based polyline.
  var geo = stream.filter(function(p) { return p.lat != null && p.lon != null && p.d != null; })
                  .map(function(p) { return {d: p.d, lat: p.lat, lon: p.lon}; });

  // Pause spans [d, d2] (stop -> resume distance). Samples inside a span carry a
  // forward-window pace measured across the gap, so they spike; we dash the line over
  // the whole span and exclude those samples from the y-scale so they don't dominate.
  // The span is extended backward by PACE_WIN_KM because instantaneous pace uses a
  // forward look-ahead window (STREAM_PACE_WINDOW_M in strava_compile.py, ~50 m), so
  // samples that much distance BEFORE the stop already have contaminated pace and would
  // otherwise render as a solid-green descent offset from the dashed dip.
  var EPS = 1e-6;
  var pauseRanges = (r.pauses || []).map(function(p) {
    return [p.d - PACE_WIN_KM, p.d2 != null ? p.d2 : p.d];
  });
  function isPausePoint(d) {
    return pauseRanges.some(function(rg) { return d >= rg[0] - EPS && d <= rg[1] + EPS; });
  }
  function segIsPaused(d0, d1) {
    return pauseRanges.some(function(rg) { return !(d1 < rg[0] - EPS || d0 > rg[1] + EPS); });
  }

  var xs = pts.map(function(p) { return p.d; });
  var scaleYs = pts.filter(function(p) { return !isPausePoint(p.d); })
                   .map(function(p) { return p[metricKey]; });
  if (scaleYs.length < 2) scaleYs = pts.map(function(p) { return p[metricKey]; });
  var dataMin = Math.min.apply(null, xs), dataMax = Math.max.apply(null, xs);
  // A pause can sit in the no-pace tail (the last ~STREAM_PACE_WINDOW_M carry no
  // forward pace) or the lead-in, so its span falls outside the pace samples'
  // range. Widen the x-domain to each pause's full span so a boundary pause and
  // its dotted segment/label stay on-chart instead of being clipped away.
  pauseRanges.forEach(function(rg) {
    if (rg[0] < dataMin) dataMin = Math.max(0, rg[0]);
    if (rg[1] > dataMax) dataMax = rg[1];
  });
  var ymin = Math.min.apply(null, scaleYs), ymax = Math.max.apply(null, scaleYs);
  if (ymax === ymin) ymax = ymin + 1;
  // Pad the y-band so the fastest/slowest real samples don't sit flush against the
  // axis edges — the slowest green pace gets breathing room below it rather than
  // resting on the baseline. Paused spikes are already excluded from ymin/ymax.
  var ypad = (ymax - ymin) * 0.06;
  ymin -= ypad; ymax += ypad;

  // Horizontal zoom window (driven by the scroll wheel). Stored on the holder so
  // it survives redraws — e.g. switching metric tabs keeps you zoomed on the same
  // segment — and is cleared when a fresh overlay opens or on double-click.
  var holder = document.getElementById(holderId);
  var xmin = dataMin, xmax = dataMax;
  if (interactive && holder && holder._zoom) {
    xmin = Math.max(dataMin, holder._zoom.lo);
    xmax = Math.min(dataMax, holder._zoom.hi);
    if (xmax - xmin < 1e-6) { xmin = dataMin; xmax = dataMax; holder._zoom = null; }
  }
  var xspan = (xmax - xmin) || 1, yspan = (ymax - ymin) || 1;

  function px(d) { return padL + (d - xmin) / xspan * (W - padL - padR); }
  function py(v) {
    var t = (v - ymin) / yspan;
    // Paused samples are excluded from the y-scale (their pace spikes across the gap),
    // so they can fall outside [ymin, ymax]; pin them to the band edge so the dotted
    // orange line stays on-chart instead of shooting off the top/bottom.
    if (t < 0) t = 0; else if (t > 1) t = 1;
    if (metric.invert) t = 1 - t; // faster pace (smaller s/km) plotted higher
    return H - padB - t * (H - padT - padB);
  }

  // area + line path. The line is split into a solid path (normal running) and a
  // dashed path (segments crossing a pause): rather than overlaying a separate
  // marker, the paused portion of the line itself is drawn dotted. The pen lifts
  // (fresh 'M') across pauses so the solid stroke is broken, not overdrawn.
  function pt(p) { return px(p.d).toFixed(1) + ',' + py(p[metricKey]).toFixed(1); }
  var solidPath = '', dashPath = '', prevPaused = null;
  for (var li = 0; li < pts.length; li++) {
    if (li > 0) {
      var paused = segIsPaused(pts[li - 1].d, pts[li].d);
      if (paused) {
        dashPath += 'M' + pt(pts[li - 1]) + 'L' + pt(pts[li]) + ' ';
      } else {
        if (prevPaused !== false) solidPath += 'M' + pt(pts[li - 1]) + ' ';
        solidPath += 'L' + pt(pts[li]) + ' ';
      }
      prevPaused = paused;
    }
  }
  // A pause beyond the last pace sample (or before the first) has no pts to dash
  // through, so the loop above skips it. Draw an explicit dotted connector held at
  // the nearest real pace so an end-of-run or start-of-run pause still shows a
  // dotted segment reaching its resume distance.
  var firstP = pts[0], lastP = pts[pts.length - 1];
  (r.pauses || []).forEach(function(p) {
    var lo = p.d - PACE_WIN_KM, hi = (p.d2 != null ? p.d2 : p.d);
    if (hi > lastP.d + EPS) {
      dashPath += 'M' + pt(lastP) + 'L' + px(hi).toFixed(1) + ',' + py(lastP[metricKey]).toFixed(1) + ' ';
    } else if (lo < firstP.d - EPS) {
      dashPath += 'M' + px(lo).toFixed(1) + ',' + py(firstP[metricKey]).toFixed(1) + 'L' + pt(firstP) + ' ';
    }
  });
  var area = 'M' + px(pts[0].d).toFixed(1) + ',' + (H - padB) +
    pts.map(function(p) { return 'L' + px(p.d).toFixed(1) + ',' + py(p[metricKey]).toFixed(1); }).join('') +
    'L' + px(pts[pts.length - 1].d).toFixed(1) + ',' + (H - padB) + 'Z';

  // y gridlines (3)
  var grid = '';
  for (var g = 0; g <= 2; g++) {
    var frac = g / 2;
    var vy = padT + frac * (H - padT - padB);
    var val = metric.invert ? (ymin + frac * yspan) : (ymax - frac * yspan);
    grid += '<line x1="' + padL + '" y1="' + vy.toFixed(1) + '" x2="' + (W - padR) +
      '" y2="' + vy.toFixed(1) + '" stroke="#2a2a2a" stroke-width="1"/>' +
      '<text x="' + (padL - 6) + '" y="' + (vy + 3).toFixed(1) +
      '" font-size="9" fill="#555" text-anchor="end">' + fmtMetric(metricKey, val) + '</text>';
  }
  // x labels at each whole km within the visible (possibly zoomed) window
  var xlab = '';
  for (var km = Math.ceil(xmin); km <= Math.floor(xmax); km++) {
    if ((xmax - xmin) > 12 && km % 2 !== 0) continue;
    xlab += '<text x="' + px(km).toFixed(1) + '" y="' + (H - 8) +
      '" font-size="9" fill="#555" text-anchor="middle">' + km + '</text>';
  }

  // Pause labels: the paused stretch of the line is drawn dotted (dashPath); here we
  // just add the stop's duration centered over each pause span. Only spans overlapping
  // the visible (possibly zoomed) window.
  var pauseSvg = '';
  (r.pauses || []).forEach(function(p) {
    var lo = p.d, hi = (p.d2 != null ? p.d2 : p.d);
    if (hi < xmin || lo > xmax) return;
    var mid = Math.min(xmax, Math.max(xmin, (lo + hi) / 2));
    // Keep the centered label inside the canvas so a pause near the right edge
    // (e.g. an end-of-run stop) isn't truncated at the SVG boundary.
    var x = Math.min(W - 18, Math.max(padL + 2, px(mid))).toFixed(1);
    pauseSvg += '<text x="' + x + '" y="' + (padT + 8) +
      '" font-size="8" fill="#f0ad4e" text-anchor="middle">&#9208; ' + fmtPauseDur(p.dur) + '</text>';
  });

  // The interactive cursor (guide line + dot + tooltip) is only drawn when the
  // chart links to a visible map — i.e. inside the expanded overlay.
  var aspect = interactive ? ' preserveAspectRatio="none"' : '';
  var cursorSvg = interactive
    ? '<g id="' + holderId + '-cursor" style="opacity:0;pointer-events:none">' +
        '<line y1="' + padT + '" y2="' + (H - padB) + '" stroke="#888" stroke-width="1" stroke-dasharray="3,3"/>' +
        '<circle r="4.5" fill="' + metric.color + '" stroke="#fff" stroke-width="1.5"/>' +
      '</g>'
    : '';

  var gid = 'grad-' + holderId + '-' + metricKey;
  var clipId = 'clip-' + holderId + '-' + metricKey;
  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '"' + aspect + ' xmlns="http://www.w3.org/2000/svg">' +
    '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="' + metric.color + '" stop-opacity="0.35"/>' +
      '<stop offset="1" stop-color="' + metric.color + '" stop-opacity="0.02"/>' +
    '</linearGradient>' +
    '<clipPath id="' + clipId + '"><rect x="' + padL + '" y="' + padT +
      '" width="' + (W - padL - padR) + '" height="' + (H - padT - padB) + '"/></clipPath>' +
    '</defs>' +
    grid +
    '<g clip-path="url(#' + clipId + ')">' +
      '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
      '<path d="' + solidPath + '" fill="none" stroke="' + metric.color + '" stroke-width="1.6"/>' +
      (dashPath ? '<path d="' + dashPath + '" fill="none" stroke="#f0ad4e" stroke-width="1.6" stroke-dasharray="2,3"/>' : '') +
      pauseSvg +
    '</g>' +
    xlab +
    cursorSvg +
    '</svg>';

  if (!holder) return;
  holder.innerHTML = svg + (interactive ? '<div class="dist-tip" id="' + holderId + '-tip"></div>' : '');

  if (!interactive) {
    holder.onmousemove = null;
    holder.onmouseleave = null;
    holder.onwheel = null;
    holder.ondblclick = null;
    return;
  }

  holder._distState = {
    holderId: holderId, runId: runId, metricKey: metricKey, label: metric.label, color: metric.color,
    pts: pts, xmin: xmin, xspan: xspan, ymin: ymin, yspan: yspan, invert: metric.invert,
    dataMin: dataMin, dataMax: dataMax,
    W: W, H: H, padL: padL, padR: padR, padT: padT, padB: padB,
    geo: geo,
    poly: r.gps_polyline || []
  };

  holder.onmousemove = function(e) { onDistHover(e, holder); };
  holder.onmouseleave = function() {
    var cur = document.getElementById(holderId + '-cursor');
    var tip = document.getElementById(holderId + '-tip');
    if (cur) cur.style.opacity = 0;
    if (tip) tip.style.opacity = 0;
    hideRunCursor();
  };
  // Scroll wheel zooms the horizontal (distance) axis around the cursor so long
  // runs can be inspected segment by segment; double-click restores the full run.
  holder.onwheel = function(e) { onDistWheel(e, holder); };
  holder.ondblclick = function() {
    if (!holder._zoom) return;
    holder._zoom = null;
    drawDistMetric(runId, metricKey, {holderId: holderId, interactive: true});
  };
}

function onDistWheel(e, holder) {
  var st = holder._distState;
  if (!st) return;
  e.preventDefault();
  var svg = holder.querySelector('svg');
  if (!svg) return;
  var sr = svg.getBoundingClientRect();
  var sx = (e.clientX - sr.left) / sr.width * st.W;   // mouse x in svg user units

  var plotW = st.W - st.padL - st.padR;
  var fracX = (sx - st.padL) / plotW;
  fracX = Math.max(0, Math.min(1, fracX));
  var dUnder = st.xmin + fracX * st.xspan;            // distance under the cursor

  var dataSpan = st.dataMax - st.dataMin;
  var minSpan = Math.min(dataSpan, 0.2);              // don't zoom tighter than ~200 m
  var newSpan = st.xspan * (e.deltaY < 0 ? 0.82 : 1.22);

  if (newSpan >= dataSpan) {                          // zoomed all the way back out
    if (!holder._zoom) return;
    holder._zoom = null;
    drawDistMetric(st.runId, st.metricKey, {holderId: st.holderId, interactive: true});
    onDistHover(e, holder);
    return;
  }
  if (newSpan < minSpan) newSpan = minSpan;

  // keep the distance under the cursor pinned in place while the window shrinks
  var newLo = dUnder - fracX * newSpan;
  var newHi = newLo + newSpan;
  if (newLo < st.dataMin) { newLo = st.dataMin; newHi = newLo + newSpan; }
  if (newHi > st.dataMax) { newHi = st.dataMax; newLo = newHi - newSpan; }
  newLo = Math.max(st.dataMin, newLo);

  holder._zoom = {lo: newLo, hi: newHi};
  drawDistMetric(st.runId, st.metricKey, {holderId: st.holderId, interactive: true});
  onDistHover(e, holder);                             // resync cursor to the new scale
}

function onDistHover(e, holder) {
  var st = holder._distState;
  if (!st) return;
  var svg = holder.querySelector('svg');
  if (!svg) return;
  var sr = svg.getBoundingClientRect();
  var sx = (e.clientX - sr.left) / sr.width * st.W;   // mouse x in svg user units

  function px(d) { return st.padL + (d - st.xmin) / st.xspan * (st.W - st.padL - st.padR); }
  function py(v) {
    var t = (v - st.ymin) / st.yspan;
    if (t < 0) t = 0; else if (t > 1) t = 1;  // pin paused samples to the band edge (see paint py)
    if (st.invert) t = 1 - t;
    return st.H - st.padB - t * (st.H - st.padT - st.padB);
  }

  // nearest point by x position
  var best = st.pts[0], bestDx = Infinity;
  for (var i = 0; i < st.pts.length; i++) {
    var dx = Math.abs(px(st.pts[i].d) - sx);
    if (dx < bestDx) { bestDx = dx; best = st.pts[i]; }
  }

  var cx = px(best.d), cy = py(best[st.metricKey]);

  // move the svg cursor
  var cur = document.getElementById(st.holderId + '-cursor');
  if (cur) {
    cur.style.opacity = 1;
    var ln = cur.querySelector('line');
    var dot = cur.querySelector('circle');
    ln.setAttribute('x1', cx); ln.setAttribute('x2', cx);
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
  }

  // tooltip with exact x (distance) and y (metric value)
  var tip = document.getElementById(st.holderId + '-tip');
  if (tip) {
    tip.innerHTML = '<div class="tip-d">' + best.d.toFixed(2) + ' km</div>' +
                    '<div><b>' + fmtMetric(st.metricKey, best[st.metricKey]) + '</b></div>';
    var hr = holder.getBoundingClientRect();
    var scaleX = sr.width / st.W, scaleY = sr.height / st.H;
    tip.style.left = ((sr.left - hr.left) + cx * scaleX) + 'px';
    tip.style.top  = ((sr.top - hr.top) + cy * scaleY) + 'px';
    tip.style.opacity = 1;
  }

  // move the dot on the map to the matching position. Prefer the GPS coords
  // embedded in the stream point. If this point lacks them (a GPS dropout that
  // still recorded distance), snap to the nearest point by distance that does
  // carry coords. Those are real recorded samples on the route, so the marker
  // can't cut across a corner (no interpolating a chord through buildings), and
  // since the list is distance-ordered the cursor still never moves backward.
  // Only runs with no per-point coords at all fall back to the count-based
  // polyline (approximate but still monotonic).
  if (best.lat != null && best.lon != null) {
    moveRunCursor(best.lat, best.lon);
  } else if (st.geo && st.geo.length >= 1) {
    var g = st.geo, d = best.d;
    var hi = 0;
    while (hi < g.length && g[hi].d < d) hi++;
    var near;
    if (hi <= 0) near = g[0];
    else if (hi >= g.length) near = g[g.length - 1];
    else near = (d - g[hi - 1].d <= g[hi].d - d) ? g[hi - 1] : g[hi];
    moveRunCursor(near.lat, near.lon);
  } else if (st.poly.length >= 2) {
    var dataSpan = (st.dataMax - st.dataMin) || 1;
    var frac = (best.d - st.dataMin) / dataSpan;
    frac = Math.max(0, Math.min(1, frac));
    var fi = frac * (st.poly.length - 1);
    var lo = Math.floor(fi), phi = Math.min(lo + 1, st.poly.length - 1), pt = fi - lo;
    var lat = st.poly[lo][0] + (st.poly[phi][0] - st.poly[lo][0]) * pt;
    var lon = st.poly[lo][1] + (st.poly[phi][1] - st.poly[lo][1]) * pt;
    moveRunCursor(lat, lon);
  } else {
    hideRunCursor();
  }
}

function selectDistMetric(runId, metricKey) {
  document.querySelectorAll('#dist-tabs-' + runId + ' .dist-tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.metric === metricKey);
  });
  drawDistMetric(runId, metricKey, {holderId: 'dist-chart-' + runId, interactive: true});
}

function renderDistChart(r) {
  if (!r.dist_stream || !r.dist_stream.length) return '';
  var avail = availableMetrics(r.dist_stream);
  if (!avail.length) return '';
  var tabs = avail.map(function(m, i) {
    return '<button class="dist-tab' + (i === 0 ? ' active' : '') +
      '" data-metric="' + m.key + '" onclick="selectDistMetric(' + r.id + ',\'' + m.key + '\')">' +
      m.label + '</button>';
  }).join('');
  return section('Over distance',
    '<div class="dist-tabs" id="dist-tabs-' + r.id + '">' + tabs + '</div>' +
    '<div id="dist-chart-' + r.id + '" class="dist-chart"></div>');
}

// ── Run shape chart ───────────────────────────────────────────────────────

function renderRunShape(r) {
  var ks = r.km_splits;
  if (!ks || ks.length < 2) return '';

  var paces  = ks.map(function(s){ return s.pace_s; }).filter(Boolean);
  if (!paces.length) return '';

  var minP = Math.min.apply(null, paces);
  var maxP = Math.max.apply(null, paces);
  var range = maxP - minP || 1;
  var H = 64;  // chart height in px

  var bars = ks.map(function(s) {
    if (!s.pace_s) return '<div style="flex:1"></div>';
    var heightPx = Math.round(((maxP - s.pace_s) / range * 0.7 + 0.3) * H);
    var zi = paceZoneIdx(s.pace_s);
    return '<div title="km ' + s.km + ': ' + s.pace_str + '" ' +
      'style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:0">' +
      '<div style="width:80%;max-width:20px;height:' + heightPx + 'px;' +
           'background:' + ZONE_COLORS[zi] + ';border-radius:2px 2px 0 0"></div>' +
      '</div>';
  }).join('');

  var avgS = r.moving_s / r.dist_km;
  var avgM = Math.floor(avgS / 60);
  var avgSec = Math.round(avgS % 60);
  var avgStr = avgM + ':' + (avgSec < 10 ? '0' : '') + avgSec + '/km avg';

  var xLabels = ks.map(function(s) {
    return '<div style="flex:1;text-align:center;font-size:8px;color:#444">' +
      (s.km % 5 === 0 || s.km === 1 ? s.km : '') + '</div>';
  }).join('');

  return section('Run shape',
    '<div style="font-size:9px;color:#555;margin-bottom:6px">' + avgStr + ' · ' + ks.length + ' km splits</div>' +
    '<div style="display:flex;align-items:flex-end;height:' + H + 'px;gap:1px">' + bars + '</div>' +
    '<div style="display:flex;margin-top:2px">' + xLabels + '</div>');
}

// ── Per-km splits table ───────────────────────────────────────────────────

function renderKmSplits(r) {
  var ks = r.km_splits;

  if (!ks || !ks.length) {
    // Fallback: show FIT lap splits if available
    return renderLapSplits(r.lap_splits);
  }

  var bestKm = ks.reduce(function(b,k){ return (k.pace_s && k.pace_s < b.pace_s) ? k : b; }, ks[0]);
  var hasElev = ks.some(function(s){ return s.gain_m != null || s.loss_m != null; });
  var hasCad  = ks.some(function(s){ return s.cad != null; });

  var rows = ks.map(function(s) {
    var isBest = bestKm.pace_s && s.km === bestKm.km;
    var zi = paceZoneIdx(s.pace_s);
    var dot = '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;' +
              'background:' + ZONE_COLORS[zi] + ';margin-right:4px;vertical-align:middle"></span>';
    var mins = Math.floor((s.time_s || 0) / 60);
    var secs = Math.round((s.time_s || 0) % 60);
    var timeStr = mins + ':' + (secs < 10 ? '0' : '') + secs;
    var elevCell = '';
    if (hasElev) {
      var g = s.gain_m || 0, l = s.loss_m || 0;
      elevCell = '<td><span style="color:#4a9a4a">↑' + g + '</span> ' +
                 '<span style="color:#c07a3a">↓' + l + '</span></td>';
    }
    var cadCell = hasCad ? '<td>' + (s.cad != null ? s.cad : '—') + '</td>' : '';
    return '<tr' + (isBest ? ' class="best-km"' : '') + '>' +
      '<td>' + s.km + '</td>' +
      '<td>' + timeStr + '</td>' +
      '<td class="pace-cell">' + dot + s.pace_str + (isBest ? ' ★' : '') + '</td>' +
      elevCell + cadCell +
    '</tr>';
  }).join('');

  var head = '<th>km</th><th>Time</th><th>Pace</th>' +
    (hasElev ? '<th>Elev</th>' : '') + (hasCad ? '<th>Cad</th>' : '');

  return section('Per km splits',
    '<table class="rd-table">' +
    '<thead><tr>' + head + '</tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

function renderLapSplits(splits) {
  if (!splits || !splits.length) return '';
  var rows = splits.map(function(lap) {
    return '<tr>' +
      '<td>' + lap.idx + '</td>' +
      '<td>' + lap.dist_str + ' km</td>' +
      '<td>' + lap.time_str + '</td>' +
      '<td class="pace-cell">' + lap.pace_str + '</td>' +
      '<td>' + lap.cadence_str + '</td>' +
      '<td>' + lap.gain_str + '</td>' +
    '</tr>';
  }).join('');
  return section('Lap splits',
    '<table class="rd-table">' +
    '<thead><tr><th>Lap</th><th>Dist</th><th>Time</th><th>Pace</th><th>Cadence</th><th>Gain</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

// ── Pace zones ────────────────────────────────────────────────────────────

function renderPaceZones(zones) {
  if (!zones || zones.every(function(z) { return z === 0; })) return '';
  var total = zones.reduce(function(a, b) { return a + b; }, 0) || 1;

  var bar = zones.map(function(s, i) {
    var pct = s / total * 100;
    if (pct < 0.5) return '';
    return '<div style="width:' + pct.toFixed(1) + '%;background:' + ZONE_COLORS[i] +
      ';height:100%;display:flex;align-items:center;justify-content:center;' +
      'font-size:9px;color:#000;font-weight:600">' +
      (pct > 8 ? pct.toFixed(0) + '%' : '') + '</div>';
  }).join('');

  var legend = zones.map(function(s, i) {
    if (s < 1) return '';
    return '<span style="display:inline-flex;align-items:center;gap:4px;font-size:10px;color:#888">' +
      '<span style="width:10px;height:10px;border-radius:2px;background:' + ZONE_COLORS[i] + ';display:inline-block"></span>' +
      ZONE_NAMES[i] + ' ' + (s / total * 100).toFixed(0) + '%</span>';
  }).join('');

  return section('Pace zones',
    '<div style="height:24px;border-radius:4px;overflow:hidden;display:flex;margin-bottom:6px">' + bar + '</div>' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap">' + legend + '</div>');
}

// ── Best efforts ──────────────────────────────────────────────────────────

function renderBestEfforts(efforts) {
  if (!efforts || !efforts.length) return '';
  var rows = efforts.map(function(e) {
    return '<tr><td>' + e.label + '</td><td>' + e.time + '</td><td class="pace-cell">' + e.pace + '</td></tr>';
  }).join('');
  return section('Best efforts in this run',
    '<table class="rd-table">' +
    '<thead><tr><th>Distance</th><th>Time</th><th>Pace</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

// ── Segment efforts in this run ─────────────────────────────────────────────

// Benchmark segments this run ran, with this run's time/pace on each. A run can
// run a segment more than once (laps), so efforts are listed individually. Each
// row opens that segment's overlay (closing the run overlay first, since the
// segment overlay sits below it in the stack).
function renderRunSegments(r) {
  if (typeof SEGMENTS === 'undefined' || !SEGMENTS || !SEGMENTS.length) return '';
  var rows = '';
  SEGMENTS.forEach(function(seg) {
    seg.efforts.forEach(function(e) {
      if (e.run_id !== r.id) return;
      var isPr = e.time_s === seg.pr_time_s;
      var dotColor = seg.type === 'climb' ? '#e0a020'
                   : (seg.type === 'segment' ? '#5a9fd4' : '#5cb85c');
      var dot = '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;' +
                'background:' + dotColor + ';margin-right:5px;vertical-align:middle"></span>';
      var lap = /\(lap \d+\)/.exec(e.name);
      var label = esc(seg.name || 'Segment') + (lap ? ' ' + lap[0] : '');
      rows += '<tr style="cursor:pointer" onclick="openSegFromRun(' + seg.id + ')" ' +
        'title="Open segment">' +
        '<td>' + dot + label + '</td>' +
        '<td>' + e.time_str + (isPr ? ' 🏆' : '') + '</td>' +
        '<td class="pace-cell">' + e.pace_str + '</td>' +
      '</tr>';
    });
  });
  if (!rows) return '';
  return section('Segment efforts',
    '<table class="rd-table">' +
    '<thead><tr><th>Segment</th><th>Time</th><th>Pace</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

function openSegFromRun(segId) {
  closeRunOverlay();
  openSegOverlay(segId);
}

function section(title, body) {
  return '<div class="rd-section"><div class="rd-section-title">' + title + '</div>' + body + '</div>';
}

// ── Overview heatmap ──────────────────────────────────────────────────────

function initOverviewMap() {
  if (!HEATMAP_POINTS || !HEATMAP_POINTS.length || typeof L === 'undefined') return;
  try {
    var el = document.getElementById('overview-heatmap');
    if (!el) return;
    overviewMap = L.map(el, {zoomControl: true});
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
      subdomains: 'abcd', maxZoom: 19
    }).addTo(overviewMap);
    // Set a view first so getZoom()/getCenter() are valid for tuneHeatRadius below.
    overviewMap.fitBounds(L.latLngBounds(HEATMAP_POINTS), {padding: [20, 20]});
    if (typeof L.heatLayer !== 'undefined') {
      var heat = L.heatLayer(HEATMAP_POINTS, {
        radius: 7, blur: 8, maxZoom: 18, minOpacity: 0.2,
        gradient: {0.0: '#1a3a1a', 0.3: '#3a7a3a', 0.6: '#5cb85c', 0.85: '#e0a020', 1.0: '#d9534f'}
      }).addTo(overviewMap);
      // Points sit on an ~11 m dedup grid, but leaflet.heat uses a fixed *pixel*
      // radius. As you zoom in the gap between points grows past that radius and
      // the line breaks back into dots. Scale the radius with zoom so it always
      // spans roughly the point spacing (clamped so low zoom keeps the old look).
      var BASE_RADIUS = 7;
      function tuneHeatRadius() {
        var z = overviewMap.getZoom();
        var lat = overviewMap.getCenter().lat;
        var mPerPx = 156543.03392 * Math.cos(lat * Math.PI / 180) / Math.pow(2, z);
        var spacingPx = 11 / mPerPx;
        var radius = Math.max(BASE_RADIUS, Math.min(40, spacingPx * 0.8));
        // A bigger brush makes a single path's own interpolated points overlap
        // more, so the accumulated alpha saturates and even a lightly-visited
        // street drifts to red as you zoom in. Scale the intensity normalisation
        // (max) with the radius so the per-point contribution is divided back
        // down: at BASE_RADIUS max stays 1 (the low-zoom look), and colour now
        // tracks how often a route was actually run rather than the zoom level.
        var max = radius / BASE_RADIUS;
        heat.setOptions({radius: radius, blur: radius * 1.1, max: max});
      }
      overviewMap.on('zoomend', tuneHeatRadius);
      tuneHeatRadius();
    }
  } catch(e) { console.warn('Overview heatmap init failed:', e); }
}

// ── Search & init ─────────────────────────────────────────────────────────

var activeType = 'all';
var sortKey = 'date';
var sortDir = -1;            // -1 = descending (largest/most-recent first), 1 = ascending
var statFilters = {};        // metric key -> {min, max}

// Numeric accessors for sorting; speed in km/h, time as seconds, elevation in m.
var SORT_ACCESS = {
  date:  function(r) { return r.date_iso; },
  dist:  function(r) { return r.dist_km; },
  time:  function(r) { return r.moving_s; },
  speed: function(r) { return r.moving_s > 0 ? r.dist_km / (r.moving_s / 3600) : 0; },
  elev:  function(r) { return r.gain; }
};

// Metrics exposed as min/max range filters (date is sort-only).
var FILTER_METRICS = [
  {key: 'dist',  label: 'Dist',  unit: 'km',   val: function(r) { return r.dist_km; }},
  {key: 'time',  label: 'Time',  unit: 'min',  val: function(r) { return r.moving_s / 60; }},
  {key: 'speed', label: 'Speed', unit: 'km/h', val: function(r) { return r.moving_s > 0 ? r.dist_km / (r.moving_s / 3600) : 0; }},
  {key: 'elev',  label: 'Elev',  unit: 'm',    val: function(r) { return r.gain; }}
];

function passesStatFilters(r) {
  for (var i = 0; i < FILTER_METRICS.length; i++) {
    var m = FILTER_METRICS[i], f = statFilters[m.key];
    if (!f) continue;
    var v = m.val(r);
    if (f.min != null && v < f.min) return false;
    if (f.max != null && v > f.max) return false;
  }
  return true;
}

function applyRunFilters() {
  var q = (document.getElementById('run-search').value || '').toLowerCase();
  var filtered = RUNS.filter(function(r) {
    var matchType = activeType === 'all' || r.run_type === activeType;
    var matchText = !q || r.name.toLowerCase().indexOf(q) >= 0 ||
      r.date_long.toLowerCase().indexOf(q) >= 0;
    return matchType && matchText && passesStatFilters(r);
  });
  var acc = SORT_ACCESS[sortKey] || SORT_ACCESS.date;
  filtered.sort(function(a, b) {
    var va = acc(a), vb = acc(b);
    return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir;
  });
  renderRunList(filtered);
  if (filtered.length) showRun(filtered[0].id);
  else document.getElementById('run-detail-content').innerHTML = '<p class="placeholder-msg">No runs match</p>';
}

// Render the min/max range inputs and wire them to the filter state.
function buildStatFilters() {
  var box = document.getElementById('run-stat-filters');
  box.innerHTML = FILTER_METRICS.map(function(m) {
    return '<div class="sf-row">' +
      '<span class="sf-label">' + m.label + '</span>' +
      '<input type="number" class="sf-min" data-key="' + m.key + '" placeholder="min" step="any" min="0">' +
      '<input type="number" class="sf-max" data-key="' + m.key + '" placeholder="max" step="any" min="0">' +
      '<span class="sf-unit">' + m.unit + '</span>' +
    '</div>';
  }).join('');
  box.querySelectorAll('input').forEach(function(inp) {
    inp.addEventListener('input', function() {
      var key = this.getAttribute('data-key');
      var f = statFilters[key] || (statFilters[key] = {min: null, max: null});
      var v = this.value === '' ? null : parseFloat(this.value);
      if (this.classList.contains('sf-min')) f.min = v; else f.max = v;
      applyRunFilters();
    });
  });
}

document.getElementById('run-sort-key').addEventListener('change', function() {
  sortKey = this.value;
  applyRunFilters();
});
document.getElementById('run-sort-dir').addEventListener('click', function() {
  sortDir = -sortDir;
  this.textContent = sortDir < 0 ? '↓' : '↑';
  applyRunFilters();
});
document.getElementById('run-filter-toggle').addEventListener('click', function() {
  var box = document.getElementById('run-stat-filters');
  box.hidden = !box.hidden;
  this.classList.toggle('active', !box.hidden);
});

// Build the filter chip bar — only categories that actually occur, each with a count.
function buildRunChips() {
  var counts = RUNS.reduce(function(m, r) { m[r.run_type] = (m[r.run_type] || 0) + 1; return m; }, {});
  var chips = ['<span class="chip active" data-type="all">All ' + RUNS.length + '</span>'];
  RUN_TYPES.forEach(function(t) {
    if (counts[t.key]) {
      chips.push('<span class="chip rt-' + t.key + '" data-type="' + t.key + '">' +
        t.label + ' ' + counts[t.key] + '</span>');
    }
  });
  var bar = document.getElementById('run-chips');
  bar.innerHTML = chips.join('');
  bar.querySelectorAll('.chip').forEach(function(chip) {
    chip.addEventListener('click', function() {
      activeType = this.getAttribute('data-type');
      bar.querySelectorAll('.chip').forEach(function(c) { c.classList.remove('active'); });
      this.classList.add('active');
      applyRunFilters();
    });
  });
}
buildRunChips();
buildStatFilters();
initSegControls();

document.getElementById('run-search').addEventListener('input', applyRunFilters);

// Close the expanded analysis overlay on backdrop click or Escape.
document.getElementById('run-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeRunOverlay();
});
document.getElementById('seg-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeSegOverlay();
});
document.addEventListener('keydown', function(e) {
  if (e.key !== 'Escape') return;
  // Close the topmost overlay first so a run opened from a segment returns to it.
  if (document.getElementById('run-overlay').classList.contains('open')) closeRunOverlay();
  else closeSegOverlay();
});

applyRunFilters();
initOverviewMap();
"""


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def generate(
    rows: list[dict],
    runs: list[dict],
    updated: str,
    threshold_mps: float | None,
    html_efforts: str,
    html_goals: str,
    html_analytics: str,
    segments: list[dict],
    html_segments: str,
) -> str:
    stats          = _overview_stats(rows, runs, threshold_mps)
    mileage_html   = render_mileage(weekly_runs(rows))
    t0             = time.perf_counter()
    runs_json      = json.dumps(runs, ensure_ascii=False)
    if PROFILE:
        print(f"[profile] runs json.dumps: {time.perf_counter() - t0:.2f}s "
              f"({len(runs_json) / 1_048_576:.1f} MB)")
    t0             = time.perf_counter()
    heat_pts       = _heatmap_points(runs)
    heatmap_json   = json.dumps(heat_pts)
    if PROFILE:
        print(f"[profile] heatmap: {time.perf_counter() - t0:.2f}s ({len(heat_pts)} points)")

    # Embed threshold as seconds/km for JS zone colouring (null if unknown)
    if threshold_mps and threshold_mps > 0:
        threshold_s_km = round(1000.0 / threshold_mps, 2)
    else:
        threshold_s_km = "null"

    rec_html = _recommendation_html(stats.get("rec"))
    heatmap_html = (
        '<div class="ov-section">\n'
        '  <p class="ov-section-title">All-time heatmap</p>\n'
        '  <div id="overview-heatmap" style="height:420px;border-radius:8px;'
        'border:1px solid #3a3a3a;background:#111;overflow:hidden"></div>\n'
        '</div>'
    )
    # Distance/elevation/time, pace zones and training-log sections, relocated
    # here from the Analytics tab. The all-time heatmap is placed just above the
    # pace-zone section, between it and the distance/elevation/time chart.
    extra_html = overview_sections(rows, updated, heatmap_html)

    overview_html = f"""
<div class="ov-section">
  <p class="ov-section-title">All time</p>
  <div class="ov-grid">
    <div class="ov-card"><p class="ov-label">Total runs</p><p class="ov-value">{stats['total_runs']}</p></div>
    <div class="ov-card"><p class="ov-label">Total distance</p><p class="ov-value">{stats['total_km']}</p><p class="ov-sub">km</p></div>
    <div class="ov-card"><p class="ov-label">Moving time</p><p class="ov-value">{stats['total_time']}</p></div>
    <div class="ov-card"><p class="ov-label">Longest run</p><p class="ov-value">{stats['longest_km']}</p><p class="ov-sub">km &middot; {stats['longest_date']}</p></div>
  </div>
</div>

<div class="ov-section">
  <p class="ov-section-title">This week</p>
  <div class="ov-grid">
    <div class="ov-card"><p class="ov-label">Runs</p><p class="ov-value">{stats['week_runs']}</p></div>
    <div class="ov-card"><p class="ov-label">Distance</p><p class="ov-value">{stats['week_km']}</p><p class="ov-sub">km</p></div>
  </div>
</div>

<div class="ov-section">
  <p class="ov-section-title">Fitness &amp; form</p>
  <div class="ov-grid">
    <div class="ov-card"><p class="ov-label">Fitness (CTL)</p><p class="ov-value green">{stats['ctl']}</p><p class="ov-sub">42-day load</p></div>
    <div class="ov-card"><p class="ov-label">Fatigue (ATL)</p><p class="ov-value amber">{stats['atl']}</p><p class="ov-sub">7-day load</p></div>
    <div class="ov-card"><p class="ov-label">Form (TSB)</p><p class="ov-value {stats['tsb_class']}">{stats['tsb']:+.1f}</p><p class="ov-sub">{stats['tsb_note']}</p></div>
  </div>
</div>
{rec_html}
{extra_html}

<div class="ov-section">
  <p class="ov-section-title">Weekly mileage</p>
  {mileage_html}
</div>
"""

    # Embedded dashboard panel CSS comes before hub CSS so hub body rules win.
    # ANALYTICS_PANEL_CSS has a body{padding:1rem} rule — the hub's body rule
    # (padding:0) comes later in the same <style> block and takes precedence.
    combined_css = (BEST_EFFORTS_CSS + "\n" + GOAL_CSS + "\n" + ANALYTICS_PANEL_CSS
                    + "\n" + SEGMENTS_CSS + "\n" + HUB_CSS)

    panel_style = "height:calc(100vh - 44px);overflow-y:auto;padding:1rem"

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Training Hub</title>\n"
        "<link rel=\"icon\" href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>%F0%9F%8F%83</text></svg>\"/>\n"
        "<link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\"/>\n"
        "<style>\n" + combined_css + "</style>\n"
        "</head>\n"
        "<body>\n"

        "<div class=\"tab-bar\">\n"
        "  <span class=\"brand\">Training Hub</span>\n"
        "  <button class=\"tab active\" data-panel=\"overview\">Overview</button>\n"
        "  <button class=\"tab\" data-panel=\"efforts\">Best Efforts</button>\n"
        "  <button class=\"tab\" data-panel=\"segments\">Segments</button>\n"
        "  <button class=\"tab\" data-panel=\"goals\">Goals</button>\n"
        "  <button class=\"tab\" data-panel=\"analytics\">Analytics</button>\n"
        "  <button class=\"tab\" data-panel=\"runs\">Runs</button>\n"
        "</div>\n"

        "<div id=\"panel-overview\" class=\"panel active\" style=\"" + panel_style + "\">\n"
        + overview_html +
        "</div>\n"

        "<div id=\"panel-efforts\" class=\"panel\" style=\"" + panel_style + "\">\n"
        + html_efforts +
        "\n</div>\n"

        "<div id=\"panel-segments\" class=\"panel\">\n"
        + html_segments +
        "\n</div>\n"

        "<div id=\"panel-goals\" class=\"panel\" style=\"" + panel_style + "\">\n"
        + html_goals +
        "\n</div>\n"

        "<div id=\"panel-analytics\" class=\"panel\" style=\"" + panel_style + "\">\n"
        + html_analytics +
        "\n</div>\n"

        "<div id=\"panel-runs\" class=\"panel\">\n"
        "  <div class=\"runs-layout\">\n"
        "    <aside id=\"run-list-pane\">\n"
        "      <div class=\"run-search\">\n"
        "        <input type=\"text\" id=\"run-search\" placeholder=\"Filter runs…\">\n"
        "        <div class=\"run-chips\" id=\"run-chips\"></div>\n"
        "        <div class=\"run-sort\">\n"
        "          <select id=\"run-sort-key\">\n"
        "            <option value=\"date\">Date</option>\n"
        "            <option value=\"dist\">Distance</option>\n"
        "            <option value=\"time\">Time</option>\n"
        "            <option value=\"speed\">Speed</option>\n"
        "            <option value=\"elev\">Elevation</option>\n"
        "          </select>\n"
        "          <button id=\"run-sort-dir\" title=\"Ascending / descending\">↓</button>\n"
        "          <button id=\"run-filter-toggle\" title=\"Filter by stats\">⚙</button>\n"
        "        </div>\n"
        "        <div class=\"run-stat-filters\" id=\"run-stat-filters\" hidden></div>\n"
        "      </div>\n"
        "      <div id=\"run-list-items\"></div>\n"
        "    </aside>\n"
        "    <main id=\"run-detail-pane\">\n"
        "      <div id=\"run-detail-content\">\n"
        "        <p class=\"placeholder-msg\">Select a run to view details</p>\n"
        "      </div>\n"
        "    </main>\n"
        "  </div>\n"
        "</div>\n"

        "<div id=\"run-overlay\" class=\"run-overlay\">\n"
        "  <div class=\"ro-box\">\n"
        "    <div class=\"ro-head\">\n"
        "      <div class=\"ro-head-text\">\n"
        "        <span id=\"ro-title\" class=\"ro-title\"></span>\n"
        "        <div id=\"ro-stats\" class=\"ro-stats\"></div>\n"
        "      </div>\n"
        "      <button class=\"ro-close\" onclick=\"closeRunOverlay()\" title=\"Close (Esc)\">&times;</button>\n"
        "    </div>\n"
        "    <div class=\"ro-body\">\n"
        "      <aside id=\"ro-splits\" class=\"ro-splits\"></aside>\n"
        "      <div class=\"ro-main\">\n"
        "        <div id=\"ro-map\" class=\"ro-map\"></div>\n"
        "        <div id=\"ro-tabs\" class=\"dist-tabs ro-tabs\"></div>\n"
        "        <div id=\"ro-chart\" class=\"dist-chart\"></div>\n"
        "        <div class=\"ro-zoom-hint\">Scroll to zoom the distance axis · double-click to reset</div>\n"
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        "</div>\n"

        "<div id=\"seg-overlay\" class=\"seg-overlay\">\n"
        "  <div class=\"so-box\">\n"
        "    <div class=\"so-head\">\n"
        "      <span id=\"so-title\" class=\"so-title\"></span>\n"
        "      <button class=\"so-close\" onclick=\"closeSegOverlay()\" title=\"Close (Esc)\">&times;</button>\n"
        "    </div>\n"
        "    <div class=\"so-body\">\n"
        "      <div id=\"so-map\" class=\"so-map\"></div>\n"
        "      <div class=\"so-side\">\n"
        "        <p class=\"so-sub-label\">Finish time over attempts</p>\n"
        "        <div id=\"so-trend\" class=\"so-trend\"></div>\n"
        "        <p class=\"so-sub-label\">Elevation over distance</p>\n"
        "        <div id=\"so-elev\" class=\"so-elev\"></div>\n"
        "        <p class=\"so-hint\">Hover a point for its time, pace and date · scroll or drag the map to zoom and pan</p>\n"
        "        <div id=\"so-stats\" class=\"so-stats\"></div>\n"
        "        <div id=\"so-attempts\" class=\"so-attempts\"></div>\n"
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        "</div>\n"

        "<script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>\n"
        "<script src=\"https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js\"></script>\n"
        "<script>\n"
        "const RUNS = " + runs_json + ";\n"
        "const SEGMENTS = " + json.dumps(segments, ensure_ascii=False) + ";\n"
        "const THRESHOLD_S_KM = " + str(threshold_s_km) + ";\n"
        "const HEATMAP_POINTS = " + heatmap_json + ";\n"
        + HUB_JS +
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    here    = Path(__file__).parent
    csv_dir = here / "csv_data"
    candidates = sorted(csv_dir.glob("*_strava.csv"))
    if not candidates:
        sys.exit(f"Error: no *_strava.csv found in {csv_dir}")
    csv_path = candidates[-1]
    print(f"Using: {csv_path.name}")

    rows = load_rows(csv_path)
    if not rows:
        sys.exit("Error: CSV is empty")

    try:
        date_str = csv_path.stem.split("_")[0]
        updated  = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, IndexError):
        updated  = datetime.today().strftime("%d %b %Y")

    threshold_mps = _compute_threshold(rows)
    if threshold_mps:
        threshold_pace = fmt_pace(1000.0, threshold_mps * 1000.0)
        print(f"Threshold pace (from best 5K): {threshold_pace}")
    else:
        print("No 5K best effort found; pace zones disabled")

    t0 = time.perf_counter()
    runs = _build_runs(rows, threshold_mps)
    if PROFILE:
        print(f"[profile] _build_runs: {time.perf_counter() - t0:.2f}s ({len(runs)} runs)")
    print(f"Loaded {len(runs)} runs")

    print("Detecting benchmark segments…")
    t0 = time.perf_counter()
    segments = build_segments(runs)
    if PROFILE:
        print(f"[profile] build_segments: {time.perf_counter() - t0:.2f}s ({len(segments)} segments)")

    print("Building dashboard panels…")
    t0 = time.perf_counter()
    html_efforts   = body_best_efforts(rows, updated)
    html_goals     = body_goal_dashboard(rows, updated)
    html_analytics = body_analytics(rows, updated)
    html_segments  = body_segments(segments, updated)
    if PROFILE:
        print(f"[profile] panels (efforts/goals/analytics): {time.perf_counter() - t0:.2f}s")

    dashboards_dir = here / "dashboards"
    dashboards_dir.mkdir(exist_ok=True)

    out = dashboards_dir / "TrainingHub.html"
    html = generate(rows, runs, updated, threshold_mps, html_efforts, html_goals,
                    html_analytics, segments, html_segments)
    out.write_text(html, encoding="utf-8")
    if PROFILE:
        print(f"[profile] HTML size: {len(html.encode('utf-8')) / 1_048_576:.1f} MB")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
