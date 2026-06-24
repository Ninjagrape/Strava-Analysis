#!/usr/bin/env python3
"""
generate_segments.py
Automatically mines recurring route segments from GPS tracks so they can act as
fitness benchmarks (e.g. the Coal Loader loop, a Shirley Rd climb). For each
detected segment it collects every run that completed it, times each attempt,
and renders a "Segments" panel for the training hub.

Public surface used by generate_hub.py:
  - build_segments(runs)            -> list[dict]
  - body_segments(segments, updated)-> str   (inner HTML for the panel)
  - SEGMENTS_CSS                    : str
"""

import json
import math
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

from generate_dashboards import fmt_time, fmt_pace, ga_time

# ---------------------------------------------------------------------------
# Tunable detection parameters (conservative: few, high-confidence benchmarks)
# ---------------------------------------------------------------------------

CELL_M          = 25.0    # spatial grid cell size for matching
DENSIFY_M       = 15.0    # max gap between track points (interpolate finer)
MIN_RUNS        = 4       # a segment must be completed by >= this many runs
MIN_SPAN_DAYS   = 14      # ... spanning >= this many days
MIN_LEN_M       = 400.0   # ... and be at least this long
MATCH_COVER     = 0.80    # fraction of segment cells a run must visit to "complete"
DIST_LO, DIST_HI = 0.70, 1.40   # allowed traversal distance vs segment length
LOOP_CLOSE_M    = 60.0    # start/end closer than this -> loop
LOOP_MIN_LEN_M  = 600.0   # phase 1: floor for *identifying* a distinct loop (high enough
                          # that sub-loops of a bigger circuit don't fragment it)
LOOP_LAP_MIN    = 300.0   # phase 2: floor when re-scanning a run to count individual laps
CLIMB_MIN_GAIN  = 15.0    # metres of net ascent for a "climb"
CLIMB_MIN_GRADE = 0.025   # ... and average grade
LOOP_MIN_CELLS  = 8       # min cells between a loop's two near-coincident points
LOOP_UNIQUE_FRAC = 0.80   # a loop visits >= this fraction of its cells only once
LOOP_CLUSTER_M  = 120.0   # two runs' loops are the same loop if centroids within this
LOOP_LEN_RATIO  = 1.35    # ... and their lengths agree within this ratio
MAX_SEGMENTS    = 40      # safety cap on rendered segments

# Anchored segments: named routes that recur too rarely as *closed* loops to be mined.
# Strava counts these off a fixed segment line, mining needs >= MIN_RUNS closed laps,
# so a loop you usually run inside a longer route (no GPS self-crossing) stays invisible.
# Each anchor pins the loop by approximate centre + length; the drawn line is derived
# from the cleanest matching run, and every run is matched against it with a distance-
# bounded sliding window so the entry point can be anywhere on the loop (Strava-style).
ANCHORS = [
    {"name": "Shirley Belmont Loop", "cen": (-33.8323, 151.1947), "len_m": 1080},
]
ANCHOR_TOL_M    = 25.0    # a run point this close to the line counts as "on" it
ANCHOR_COVER    = 0.80    # fraction of the line a window must cover to complete
ANCHOR_NEAR_M   = 130.0   # cleanest instance must be within this of the anchor centre
ANCHOR_LINE_STEP = 10.0   # resample spacing of the derived line
ANCHOR_CLOSE_M  = 55.0    # a lap's start and end must be this close (a real loop closes;
                          # a pass that starts at home and ends mid-road is not a lap)

CACHE_DIR       = Path(__file__).parent / "csv_data"
SEG_CACHE       = CACHE_DIR / "segments_cache.json"
GEO_CACHE       = CACHE_DIR / "segment_geocode_cache.json"
MATCH_CACHE     = CACHE_DIR / "segment_match_cache.json"

NOMINATIM_URL   = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
USER_AGENT      = "Strava-Analysis-Hub/1.0 (personal training dashboard)"

# highway= values that are not runnable; everything else (residential, footway, path,
# pedestrian, service, steps, track, …) is kept as graph edges for map-matching.
MATCH_EXCLUDE   = {"motorway", "motorway_link", "trunk", "trunk_link",
                   "construction", "proposed", "raceway"}
MATCH_SNAP_M    = 35.0    # a resampled point further than this from any path is dropped
MATCH_STEP_M    = 20.0    # resample spacing before snapping
MATCH_MAX_DEV_M = 12.0    # if the matched line strays this far (mean) from the actual
                          # trace it snapped to the wrong roads, so keep the raw trace
MATCH_TURN_DEG  = 55.0    # a direction change sharper than this counts as a "reversal"
MATCH_MAX_TURN_GAIN = 0.06  # if snapping adds more reversals-per-point than this above the
                            # raw trace it has flickered between adjacent ways (a zig-zag no
                            # run made), so keep the smooth raw trace instead


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def _cell_factory(ref_lat):
    """Return a function mapping (lat, lon) -> integer grid cell (~CELL_M square)."""
    dlat = CELL_M / 111320.0
    dlon = CELL_M / (111320.0 * max(0.1, math.cos(math.radians(ref_lat))))

    def cell(lat, lon):
        return (int(math.floor(lat / dlat)), int(math.floor(lon / dlon)))

    return cell


def _neighbours(c):
    """3x3 block of cells around c (~ one cell of slack on each side)."""
    return {(c[0] + dr, c[1] + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)}


# ---------------------------------------------------------------------------
# Per-run geo-track construction (lat, lon, cumulative metres, seconds, elev)
# ---------------------------------------------------------------------------

def _geo_track(run):
    """Ordered list of dicts {lat, lon, d, t, elev} from a run's distance stream.

    Cumulative seconds come from the stream's `t` field when present (added by
    strava_compile.py); for older cached rows lacking `t` we integrate pace over
    distance as a fallback so the feature still works before a --rebuild.
    """
    stream = run.get("dist_stream") or []
    raw = []
    have_t = any("t" in p for p in stream)
    cum_t = 0.0
    prev_d = None
    prev_pace = None
    for p in stream:
        d = p.get("d")
        if d is None:
            continue
        d_m = d * 1000.0
        pace = p.get("pace")
        if have_t:
            t = p.get("t")
        else:
            if prev_d is not None and prev_pace is not None and pace is not None:
                cum_t += (prev_pace + pace) / 2.0 * (d_m - prev_d) / 1000.0
            t = cum_t
        prev_d, prev_pace = d_m, pace
        if p.get("lat") is None or p.get("lon") is None or t is None:
            continue
        raw.append({"lat": p["lat"], "lon": p["lon"], "d": d_m, "t": t,
                    "elev": p.get("elev")})
    return _densify(raw)


def _densify(pts):
    """Insert interpolated points so consecutive samples are <= DENSIFY_M apart.
    Keeps the cell sequence continuous for long runs whose stream spacing widens."""
    if len(pts) < 2:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        gap = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        if gap > DENSIFY_M:
            n = int(gap / DENSIFY_M)
            for j in range(1, n + 1):
                f = j / (n + 1)
                out.append({
                    "lat": a["lat"] + f * (b["lat"] - a["lat"]),
                    "lon": a["lon"] + f * (b["lon"] - a["lon"]),
                    "d":   a["d"] + f * (b["d"] - a["d"]),
                    "t":   a["t"] + f * (b["t"] - a["t"]),
                    "elev": a["elev"] if a["elev"] is not None else b["elev"],
                })
        out.append(b)
    return out


def _cell_sequence(track, cell):
    """De-duplicated ordered list of (cell, point) — one entry per cell first-visit run."""
    seq = []
    last = None
    for p in track:
        c = cell(p["lat"], p["lon"])
        if c != last:
            seq.append((c, p))
            last = c
    return seq


# ---------------------------------------------------------------------------
# Corridor mining: hot directed transitions -> maximal chains + cycles
# ---------------------------------------------------------------------------

MAX_PATH_CELLS = 2400   # ~60 km guard on a single grown corridor


def _mine_chains(cell_seqs):
    """cell_seqs: list of (run_id, [cell,...]). Returns candidate corridors (ordered
    cell lists). Built by seeding on the busiest directed transitions and greedily
    growing in both directions, always following the continuation that the seed run
    group keeps taking. Growth crosses junctions (unlike a plain chain contraction,
    which fragments a route into short pieces wherever other routes cross it)."""
    trans = defaultdict(set)            # (a,b) -> {run_id}
    for rid, cells in cell_seqs:
        for a, b in zip(cells, cells[1:]):
            if a != b:
                trans[(a, b)].add(rid)

    succ = defaultdict(list)
    pred = defaultdict(list)
    for a, b in trans:
        if len(trans[(a, b)]) >= MIN_RUNS:
            succ[a].append(b)
            pred[b].append(a)
    if not succ:
        return []

    seeds = sorted((ab for ab in trans if len(trans[ab]) >= MIN_RUNS),
                   key=lambda ab: len(trans[ab]), reverse=True)

    chains = []
    covered = set()
    for (a0, b0) in seeds:
        if (a0, b0) in covered:
            continue
        seed_runs = trans[(a0, b0)]

        def grow(start, nxt, forward):
            path = [start, nxt]
            in_path = {start, nxt}
            cur = nxt
            while len(path) < MAX_PATH_CELLS:
                neigh = succ[cur] if forward else pred[cur]
                best, best_score = None, 0
                for c in neigh:
                    edge = (cur, c) if forward else (c, cur)
                    score = len(seed_runs & trans[edge])
                    if score >= MIN_RUNS and score > best_score:
                        best, best_score = c, score
                if best is None:
                    break
                if best == path[0]:          # closed loop
                    path.append(best)
                    break
                if best in in_path:          # avoid figure-eight tangles
                    break
                path.append(best)
                in_path.add(best)
                cur = best
            return path

        fwd = grow(a0, b0, True)             # [a0, b0, ...]
        if fwd[-1] == fwd[0]:                # forward growth closed a loop
            chain = fwd
        else:
            bwd = grow(b0, a0, False)        # [b0, a0, ...] (backward from a0)
            chain = list(reversed(bwd[2:])) + fwd   # prepend the backward extension
        for a, b in zip(chain, chain[1:]):
            covered.add((a, b))
        if len(chain) >= 2:
            chains.append(chain)

    return chains


# ---------------------------------------------------------------------------
# Match every run to a candidate chain and time the traversal
# ---------------------------------------------------------------------------

def _chain_length_m(chain, ref_lat):
    """Rough length from cell centres (provisional, before a reference run is chosen)."""
    dlat = CELL_M / 111320.0
    dlon = CELL_M / (111320.0 * max(0.1, math.cos(math.radians(ref_lat))))
    pts = [((r + 0.5) * dlat, (c + 0.5) * dlon) for r, c in chain]
    return sum(_haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
               for i in range(1, len(pts)))


def _match_run(chain_cells, seg_len, seq):
    """Return the fastest completion of a chain within one run's cell sequence, or None.

    seq: list of (cell, point). A completion enters near the chain's first cell,
    exits near its last cell (in order), covers >= MATCH_COVER of the chain's
    cells in between, and travels a plausible distance."""
    cells = [c for c, _ in seq]
    pts = [p for _, p in seq]
    chain_set = set(chain_cells)
    n_chain = len(chain_set)
    start_zone = _neighbours(chain_cells[0])
    end_zone = _neighbours(chain_cells[-1])

    start_idx = [i for i, c in enumerate(cells) if c in start_zone]
    end_idx = [i for i, c in enumerate(cells) if c in end_zone]
    if not start_idx or not end_idx:
        return None

    best = None
    for sp in start_idx:
        for ep in end_idx:
            if ep <= sp:
                continue
            dist = pts[ep]["d"] - pts[sp]["d"]
            if dist < seg_len * DIST_LO or dist > seg_len * DIST_HI:
                continue
            covered = len(chain_set & set(cells[sp:ep + 1])) / n_chain
            if covered < MATCH_COVER:
                continue
            t = pts[ep]["t"] - pts[sp]["t"]
            if t <= 0:
                continue
            if best is None or t < best["time_s"]:
                sub = pts[sp:ep + 1]
                gain = sum(max(0.0, (sub[k]["elev"] or 0) - (sub[k - 1]["elev"] or 0))
                           for k in range(1, len(sub))
                           if sub[k]["elev"] is not None and sub[k - 1]["elev"] is not None)
                best = {"time_s": t, "dist_m": dist, "sub": sub,
                        "net_elev": ((sub[-1]["elev"] or 0) - (sub[0]["elev"] or 0))
                                    if sub[0]["elev"] is not None and sub[-1]["elev"] is not None
                                    else 0.0,
                        "gain_m": gain}
    return best


def _simplify(poly, max_pts=120):
    if len(poly) <= max_pts:
        return poly
    stride = len(poly) / max_pts
    out = [poly[int(i * stride)] for i in range(max_pts)]
    out[-1] = poly[-1]
    return out


# ---------------------------------------------------------------------------
# Build segments from runs
# ---------------------------------------------------------------------------

def _classify(length_m, net_elev, gain_m, endpoints_close):
    grade = (net_elev / length_m) if length_m else 0.0
    if endpoints_close and length_m >= LOOP_MIN_LEN_M:
        return "loop"
    if net_elev >= CLIMB_MIN_GAIN and grade >= CLIMB_MIN_GRADE:
        return "climb"
    return "segment"


def _runs_signature(runs):
    h = hashlib.sha1()
    for r in sorted(runs, key=lambda x: x.get("date_iso", "")):
        poly = r.get("gps_polyline") or []
        sig = f"{r.get('date_iso')}|{r.get('dist_km')}|{len(poly)}"
        if poly:
            sig += f"|{poly[0]}|{poly[-1]}"
        h.update(sig.encode("utf-8"))
    h.update(f"params:{CELL_M},{MIN_RUNS},{MIN_LEN_M},{MATCH_COVER},"
             f"loops:{LOOP_MIN_CELLS},{LOOP_UNIQUE_FRAC},{LOOP_CLUSTER_M},{LOOP_LEN_RATIO},refine2,round1,medoid1,laps1,merge1,twophase2,matchturn1,"
             f"anchors:{ANCHORS},{ANCHOR_TOL_M},{ANCHOR_COVER},{ANCHOR_CLOSE_M},close1,anchorline2,elevprofile1".encode())
    return h.hexdigest()


def build_segments(runs):
    """Detect recurring benchmark segments across all runs. Cached on the run set."""
    sig = _runs_signature(runs)
    cached = _load_json(SEG_CACHE)
    if cached and cached.get("signature") == sig:
        print(f"Segments: loaded {len(cached['segments'])} from cache")
        return cached["segments"]

    # Build per-run geo-tracks and cell sequences (sharing one grid reference lat).
    tracks = {}
    for r in runs:
        tr = _geo_track(r)
        if len(tr) >= 10:
            tracks[r["id"]] = tr
    if not tracks:
        return []

    all_lats = [p["lat"] for tr in tracks.values() for p in tr[:1]]
    ref_lat = sorted(all_lats)[len(all_lats) // 2] if all_lats else -33.83
    cell = _cell_factory(ref_lat)

    seqs = {rid: _cell_sequence(tr, cell) for rid, tr in tracks.items()}
    cell_seqs = [(rid, [c for c, _ in s]) for rid, s in seqs.items()]

    chains = _mine_chains(cell_seqs)
    run_by_id = {r["id"]: r for r in runs}

    segments = []

    # Point-to-point corridors (climbs, repeated stretches).
    for chain in chains:
        seg_len0 = _chain_length_m(chain, ref_lat)
        if seg_len0 < MIN_LEN_M:
            continue
        efforts = _collect_efforts(chain, seg_len0, seqs)
        if len(efforts) < MIN_RUNS:
            continue
        # Orient climbs uphill: if this corridor's reference traversal is a notable
        # descent, re-match the reversed direction so the benchmark times the climb.
        ref = min(efforts, key=lambda e: abs(e["dist_m"] - _median(efforts)))
        if ref["net_elev"] <= -CLIMB_MIN_GAIN:
            rev = _collect_efforts(chain[::-1], seg_len0, seqs)
            if len(rev) >= MIN_RUNS:
                chain, efforts = chain[::-1], rev
        seg = _segment_from_efforts(efforts, run_by_id)
        if seg:
            segments.append(seg)

    # Closed loops (the Coal Loader, Belmont/Shirley loops) — found by detecting
    # where each run returns near an earlier point, then clustering across runs.
    segments.extend(_detect_loops(seqs, run_by_id))

    # Anchored loops: named routes mining can't recover, matched off a fixed line.
    segments.extend(_build_anchored_segments(seqs, tracks, ref_lat, run_by_id))

    segments = _dedupe(segments)
    # Best benchmarks first: most-run, then longest.
    segments.sort(key=lambda s: (-s["n_efforts"], -s["length_m"]))
    segments = segments[:MAX_SEGMENTS]

    _name_segments(segments)
    _disambiguate_names(segments)
    _match_segments(segments)
    for i, s in enumerate(segments):
        s["id"] = i

    _save_json(SEG_CACHE, {"signature": sig, "segments": segments})
    print(f"Segments: detected {len(segments)} benchmark segments")
    return segments


def _median(efforts):
    d = sorted(e["dist_m"] for e in efforts)
    return d[len(d) // 2]


def _collect_efforts(chain_cells, seg_len_est, seqs):
    efforts = []
    for rid, seq in seqs.items():
        m = _match_run(chain_cells, seg_len_est, seq)
        if m:
            m["run_id"] = rid
            efforts.append(m)
    return efforts


def _segment_from_efforts(efforts, run_by_id, force_type=None,
                          min_runs=MIN_RUNS, min_span=MIN_SPAN_DAYS):
    """Turn per-run traversals into a segment dict, or None if it fails the conservative
    length / span filters. `efforts` may contain several entries for one run when the
    route was repeated within a session (laps) — each becomes its own attempt, but the
    run-count and span gates are measured on distinct runs so one lap-heavy day can't
    qualify a segment on its own. Anchored (user-declared) segments relax these gates,
    since a pinned loop is legitimate even when its only valid laps are one session's."""
    distinct_runs = {e["run_id"] for e in efforts}
    if len(distinct_runs) < min_runs:
        return None
    seg_len = _median(efforts)
    if seg_len < MIN_LEN_M:
        return None
    if force_type == "loop":
        # Draw a single real run, the most representative one, so the line follows the
        # actual streets. Restrict to efforts near the typical length, drop pinched
        # out-and-back tracings (low roundness), then take the medoid of what remains.
        band = [e for e in efforts if 0.80 * seg_len <= e["dist_m"] <= 1.25 * seg_len] or efforts
        best_iso = max(_loop_roundness(e["sub"]) for e in band)
        good = [e for e in band if _loop_roundness(e["sub"]) >= max(0.33, 0.6 * best_iso)] or band
        midx = _medoid_index([e["sub"] for e in good])
        ref = good[midx] if midx is not None else max(good, key=lambda e: _loop_roundness(e["sub"]))
        poly = _simplify([[p["lat"], p["lon"]] for p in ref["sub"]])
    else:
        ref = min(efforts, key=lambda e: abs(e["dist_m"] - seg_len))
        poly = _simplify([[p["lat"], p["lon"]] for p in ref["sub"]])
    if len(poly) < 2:
        return None
    endpoints_close = _haversine_m(poly[0][0], poly[0][1], poly[-1][0], poly[-1][1]) < LOOP_CLOSE_M
    net_elev, gain_m = ref["net_elev"], ref["gain_m"]
    seg_type = force_type or _classify(seg_len, net_elev, gain_m, endpoints_close)
    len_km = seg_len / 1000.0

    # Number repeated efforts within one run so laps in a single session read as
    # "… (lap 2)", ordered by when each lap happened.
    lap_no = {}
    per_run = defaultdict(list)
    for e in efforts:
        per_run[e["run_id"]].append(e)
    for es in per_run.values():
        if len(es) > 1:
            for k, e in enumerate(sorted(es, key=lambda x: x["sub"][0]["t"] if x.get("sub") else 0), 1):
                lap_no[id(e)] = k

    rows = []
    for e in efforts:
        r = run_by_id[e["run_id"]]
        t = e["time_s"]
        gx = ga_time(t, e["gain_m"], len_km) if len_km else t
        name = r["name"]
        if id(e) in lap_no:
            name = f"{name} (lap {lap_no[id(e)]})"
        rows.append({
            "run_id":   e["run_id"],
            "date_iso": r["date_iso"],
            "date_long": r["date_long"],
            "name":     name,
            "time_s":   round(t, 1),
            "time_str": fmt_time(t),
            "pace_str": fmt_pace(t, seg_len),
            "ga_pace_str": fmt_pace(gx, seg_len) if gx > 0 else "—",
        })
    rows.sort(key=lambda x: x["date_iso"])
    if _span_days(rows) < min_span:
        return None
    pr = min(rows, key=lambda x: x["time_s"])
    return {
        "type":      seg_type,
        "length_m":  round(seg_len),
        "length_str": f"{len_km:.2f} km",
        "gain_m":    round(gain_m),
        "grade":     round((net_elev / seg_len) * 100, 1) if seg_len else 0.0,
        "polyline":  [[round(a, 5), round(b, 5)] for a, b in poly],
        "elev_profile": _elev_profile(ref["sub"]),
        "efforts":   rows,
        "n_efforts": len(rows),
        "span_days": _span_days(rows),
        "pr_time_s": pr["time_s"],
        "pr_time_str": pr["time_str"],
        "pr_date":   pr["date_long"],
    }


def _elev_profile(sub, max_pts=64):
    """Elevation-over-distance samples [[metres_from_start, elev_m], ...] for a segment's
    reference lap, downsampled with a uniform stride so the payload stays small. Returns []
    when the lap has no usable elevation stream."""
    raw = [[p["d"] - sub[0]["d"], p["elev"]] for p in sub if p.get("elev") is not None]
    if len(raw) < 2:
        return []
    step = max(1, len(raw) // max_pts)
    out = [[round(raw[i][0]), round(raw[i][1], 1)] for i in range(0, len(raw), step)]
    last = [round(raw[-1][0]), round(raw[-1][1], 1)]
    if out[-1] != last:
        out.append(last)
    return out


def _loop_record(cells, pts, i, j):
    sub_pts = pts[i:j + 1]
    return {
        "cells": frozenset(cells[i:j + 1]),
        "time_s": sub_pts[-1]["t"] - sub_pts[0]["t"],
        "dist_m": pts[j]["d"] - pts[i]["d"],
        "sub": sub_pts,
        "net_elev": ((sub_pts[-1]["elev"] or 0) - (sub_pts[0]["elev"] or 0))
                    if sub_pts[0]["elev"] is not None and sub_pts[-1]["elev"] is not None else 0.0,
        "gain_m": sum(max(0.0, (sub_pts[k]["elev"] or 0) - (sub_pts[k - 1]["elev"] or 0))
                      for k in range(1, len(sub_pts))
                      if sub_pts[k]["elev"] is not None and sub_pts[k - 1]["elev"] is not None),
    }


def _run_loops(seq, min_len=LOOP_MIN_LEN_M):
    """Minimal closed sub-loops inside one run's cell sequence, each at least `min_len`.

    As the track advances we remember the most recent index each cell was visited.
    Whenever the current cell lands in the neighbourhood of a cell visited a little
    earlier, the span between them closes a loop. Taking the *most recent* prior visit
    yields the tightest loop. Of overlapping candidates we keep the shortest (the tightest
    real lap); candidates over disjoint time windows are separate laps and are all kept,
    so repeating a loop within one session yields one entry per lap. Loops must be long
    enough and mostly single-pass (not an out-and-back retrace)."""
    cells = [c for c, _ in seq]
    pts = [p for _, p in seq]
    last_pos = {}
    cand = []
    for j, c in enumerate(cells):
        for nb in _neighbours(c):
            i = last_pos.get(nb)
            if i is not None and j - i >= LOOP_MIN_CELLS:
                d = pts[j]["d"] - pts[i]["d"]
                if min_len <= d <= 12000:
                    sub = cells[i:j + 1]
                    if len(set(sub)) / len(sub) >= LOOP_UNIQUE_FRAC:
                        cand.append((i, j, d))
        last_pos[c] = j

    cand.sort(key=lambda x: x[2])   # shortest (tightest lap) first
    loops, kept = [], []            # kept: index intervals already taken
    for i, j, d in cand:
        if any(i < kj and j > ki for ki, kj in kept):   # same time window as a kept lap
            continue
        kept.append((i, j))
        ri, rj = _refine_loop_ends(pts, i, j)
        loops.append(_loop_record(cells, pts, ri, rj))
    return loops


def _refine_loop_ends(pts, i, j, window=4):
    """Nudge the loop's start/end indices within a small window to the pair of points
    that come closest together, so the rendered loop actually closes on itself (at the
    real self-crossing, e.g. the foot of a ramp) instead of leaving a ~one-cell gap."""
    best = (i, j)
    best_d = _haversine_m(pts[i]["lat"], pts[i]["lon"], pts[j]["lat"], pts[j]["lon"])
    lo_i, hi_i = max(0, i - window), min(len(pts) - 1, i + window)
    lo_j, hi_j = max(0, j - window), min(len(pts) - 1, j + window)
    for a in range(lo_i, hi_i + 1):
        for b in range(lo_j, hi_j + 1):
            if b - a < LOOP_MIN_CELLS:
                continue
            d = _haversine_m(pts[a]["lat"], pts[a]["lon"], pts[b]["lat"], pts[b]["lon"])
            if d < best_d:
                best_d, best = d, (a, b)
    return best


def _loop_centroid(sub):
    n = len(sub) or 1
    return (sum(p["lat"] for p in sub) / n, sum(p["lon"] for p in sub) / n)


def _loop_roundness(sub):
    """Isoperimetric quotient 4*pi*Area / Perimeter**2 of a traversal's ground track
    (1.0 = circle, ~0 = a there-and-back sliver). Used to prefer a genuine loop shape
    over a pinched out-and-back when choosing which effort to draw."""
    if len(sub) < 4:
        return 0.0
    lat0 = sub[0]["lat"]
    mx = 111320.0 * math.cos(math.radians(lat0))
    xs = [(p["lon"] - sub[0]["lon"]) * mx for p in sub]
    ys = [(p["lat"] - lat0) * 111320.0 for p in sub]
    area = abs(sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
                   for i in range(len(xs) - 1))) / 2.0
    perim = sum(math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i])
                for i in range(len(xs) - 1))
    return (4 * math.pi * area / (perim * perim)) if perim else 0.0


# --- Consensus loop path (average all efforts into one smooth medial line) ------
# A single run's GPS wanders to the edge of the path; averaging the runs that share a
# loop cancels that noise. More raw samples would not help (the streams are already
# dense) — the win is combining runs, the same idea Strava approximates by road-snapping.

CONSENSUS_N = 64   # points the consensus loop is resampled to


def _ll_to_xy(points, lat0, lon0):
    mx = 111320.0 * math.cos(math.radians(lat0))
    return [((p["lon"] - lon0) * mx, (p["lat"] - lat0) * 111320.0) for p in points]


def _xy_to_ll(xy, lat0, lon0):
    mx = 111320.0 * math.cos(math.radians(lat0))
    return [[lat0 + y / 111320.0, lon0 + x / mx] for x, y in xy]


def _resample_closed(xy, n):
    """Resample a closed loop to n points spaced equally by arc length."""
    pts = xy + [xy[0]] if xy[0] != xy[-1] else xy[:]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    total = cum[-1]
    if total <= 0:
        return [xy[0]] * n
    out, j, step = [], 0, total / n
    for i in range(n):
        target = i * step
        while j < len(cum) - 2 and cum[j + 1] < target:
            j += 1
        seg = cum[j + 1] - cum[j]
        f = (target - cum[j]) / seg if seg > 0 else 0.0
        out.append((pts[j][0] + f * (pts[j + 1][0] - pts[j][0]),
                    pts[j][1] + f * (pts[j + 1][1] - pts[j][1])))
    return out


def _signed_area(xy):
    return sum(xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1]
               for i in range(len(xy))) / 2.0


def _best_roll(ref, eff):
    """Cyclic offset of eff that best lines it up with ref (handles differing start points)."""
    n = len(ref)
    best_k, best_c = 0, None
    for k in range(n):
        c = 0.0
        for i in range(n):
            dx = ref[i][0] - eff[(i + k) % n][0]
            dy = ref[i][1] - eff[(i + k) % n][1]
            c += dx * dx + dy * dy
        if best_c is None or c < best_c:
            best_c, best_k = c, k
    return best_k


def _medoid_index(subs):
    """Index of the most representative effort: the one whose loop shape is closest to
    all the others. Averaging the runs rounds off the street corners (the drawn line ends
    up cutting across blocks), so instead we draw a single real run, the one most typical
    of the set, which by definition follows the actual streets. Each effort is resampled,
    flipped to a common orientation and rotated to a common start so shapes are comparable;
    we then pick the effort with the smallest total distance to the rest."""
    usable = [i for i, s in enumerate(subs) if len(s) >= 4]
    if len(usable) < 2:
        return usable[0] if usable else None
    allp = [p for i in usable for p in subs[i]]
    lat0 = sum(p["lat"] for p in allp) / len(allp)
    lon0 = sum(p["lon"] for p in allp) / len(allp)

    reps = {}
    for i in usable:
        r = _resample_closed(_ll_to_xy(subs[i], lat0, lon0), CONSENSUS_N)
        if _signed_area(r) < 0:          # normalise traversal direction
            r = r[::-1]
        reps[i] = r
    template = max(reps.values(), key=lambda r: abs(_signed_area(r)))
    aligned = {}
    for i, r in reps.items():
        k = _best_roll(template, r)
        aligned[i] = [r[(t + k) % CONSENSUS_N] for t in range(CONSENSUS_N)]

    best_i, best_cost = usable[0], None
    for i in usable:
        cost = 0.0
        for jx in usable:
            if jx == i:
                continue
            cost += sum(math.hypot(aligned[i][t][0] - aligned[jx][t][0],
                                   aligned[i][t][1] - aligned[jx][t][1])
                        for t in range(CONSENSUS_N))
        if best_cost is None or cost < best_cost:
            best_cost, best_i = cost, i
    return best_i


def _cluster_loops(per_run):
    """Group loop instances by geographic centroid and length. A running-mean centroid
    keeps the same circuit together despite small path variation; clustering on location
    rather than exact cell overlap groups a loop across days even when approached
    slightly differently or run inside a longer session."""
    clusters = []   # each: {"cen": (lat,lon), "med_d": float, "members": [loop,...]}
    for lp in sorted(per_run, key=lambda x: -x["dist_m"]):
        placed = False
        for cl in clusters:
            ratio = lp["dist_m"] / cl["med_d"] if cl["med_d"] else 0
            near = _haversine_m(lp["cen"][0], lp["cen"][1],
                                cl["cen"][0], cl["cen"][1]) <= LOOP_CLUSTER_M
            if near and (1.0 / LOOP_LEN_RATIO) <= ratio <= LOOP_LEN_RATIO:
                cl["members"].append(lp)
                n = len(cl["members"])
                cl["cen"] = (sum(m["cen"][0] for m in cl["members"]) / n,
                             sum(m["cen"][1] for m in cl["members"]) / n)
                ds = sorted(m["dist_m"] for m in cl["members"])
                cl["med_d"] = ds[len(ds) // 2]
                placed = True
                break
        if not placed:
            clusters.append({"cen": lp["cen"], "med_d": lp["dist_m"], "members": [lp]})
    return clusters


def _detect_loops(seqs, run_by_id):
    """Two passes. Phase 1 identifies distinct loops at a stable scale (a higher floor so
    a small sub-loop of a bigger circuit can't fragment it). Phase 2 re-scans each run
    with a low floor to find every individual lap and assigns each lap to the identifying
    loop it falls in (same place, comparable length) — so repeating a short loop within a
    session counts as several attempts without the short floor breaking identification."""
    def scan(min_len):
        out = []
        for rid, seq in seqs.items():
            for lp in _run_loops(seq, min_len):
                lp["run_id"] = rid
                lp["cen"] = _loop_centroid(lp["sub"])
                out.append(lp)
        return out

    ident = _cluster_loops(scan(LOOP_MIN_LEN_M))
    ident = [cl for cl in ident if len({m["run_id"] for m in cl["members"]}) >= MIN_RUNS]

    laps = scan(LOOP_LAP_MIN)
    segments = []
    for cl in ident:
        members = [lp for lp in laps
                   if _haversine_m(lp["cen"][0], lp["cen"][1], cl["cen"][0], cl["cen"][1]) <= LOOP_CLUSTER_M
                   and (1.0 / LOOP_LEN_RATIO) <= (lp["dist_m"] / cl["med_d"] if cl["med_d"] else 0) <= LOOP_LEN_RATIO]
        # Each run's laps are already distinct in time (from _run_loops); fall back to the
        # phase-1 members if the lap re-scan somehow found fewer runs.
        if len({m["run_id"] for m in members}) < MIN_RUNS:
            members = cl["members"]
        seg = _segment_from_efforts(members, run_by_id, force_type="loop")
        if seg:
            segments.append(seg)
    return segments


# ---------------------------------------------------------------------------
# Anchored segments (fixed-line matching, the way Strava counts efforts)
# ---------------------------------------------------------------------------

def _loop_instances(seq):
    """Every self-crossing loop candidate in one run, at all scales, used to derive an
    anchor's drawn line from the user's cleanest pass. Each: {dist_m, round, cen, sub}."""
    cells = [c for c, _ in seq]
    pts = [p for _, p in seq]
    last_pos = {}
    out = []
    for j, c in enumerate(cells):
        for nb in _neighbours(c):
            i = last_pos.get(nb)
            if i is not None and j - i >= LOOP_MIN_CELLS:
                d = pts[j]["d"] - pts[i]["d"]
                if LOOP_LAP_MIN <= d <= 12000:
                    sub = cells[i:j + 1]
                    if len(set(sub)) / len(sub) >= LOOP_UNIQUE_FRAC:
                        out.append({"dist_m": d, "round": _loop_roundness(pts[i:j + 1]),
                                    "cen": _loop_centroid(pts[i:j + 1]), "sub": pts[i:j + 1]})
        last_pos[c] = j
    return out


def _derive_anchor_line(anchor, seqs):
    """The most typical run instance near the anchor's centre and length becomes its line.

    Picking the *roundest* instance backfires: cutting a corner straight across a block
    raises a loop's isoperimetric roundness, so the roundest lap is usually the one that
    strays off the streets and across buildings. Instead drop the degenerate out-and-back
    slivers (a low-roundness floor) and take the medoid of what remains — the lap whose
    shape is most typical of the set, which by definition follows the actual roads. This
    mirrors how mined loops choose their drawn line in _segment_from_efforts."""
    cen, ln = anchor["cen"], anchor["len_m"]
    cands = []
    for seq in seqs.values():
        for c in _loop_instances(seq):
            if (_haversine_m(c["cen"][0], c["cen"][1], cen[0], cen[1]) <= ANCHOR_NEAR_M
                    and 0.7 * ln <= c["dist_m"] <= 1.4 * ln):
                cands.append(c)
    if not cands:
        return None
    best_iso = max(c["round"] for c in cands)
    good = [c for c in cands if c["round"] >= max(0.33, 0.6 * best_iso)] or cands
    midx = _medoid_index([c["sub"] for c in good])
    return good[midx] if midx is not None else max(good, key=lambda c: c["round"])


def _resample_ll(pts, step):
    """Even ~`step` m spacing along a list of {lat,lon} points -> [(lat,lon), ...]."""
    out = [(pts[0]["lat"], pts[0]["lon"])]
    for a, b in zip(pts, pts[1:]):
        seg = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        if seg <= 0:
            continue
        n = max(1, int(seg / step))
        for k in range(1, n + 1):
            f = k / n
            out.append((a["lat"] + f * (b["lat"] - a["lat"]),
                        a["lon"] + f * (b["lon"] - a["lon"])))
    return out


def _match_anchor_spans(line_pts, loop_len, track, ref_lat):
    """All distance-bounded windows of one run's track that cover >= ANCHOR_COVER of the
    loop line. The window is grown to roughly the loop length, so a run merely clipping
    part of the line can't qualify, and the entry point may be anywhere on the loop. Lap
    repeats in one session yield several non-overlapping windows. Returns (i0, i1) pairs."""
    dlat = ANCHOR_TOL_M / 111320.0
    dlon = ANCHOR_TOL_M / (111320.0 * max(0.1, math.cos(math.radians(ref_lat))))
    grid = defaultdict(list)
    for idx, (la, lo) in enumerate(line_pts):
        grid[(int(la / dlat), int(lo / dlon))].append(idx)
    n_line = len(line_pts)

    # For each track point, the set of line indices it sits on (within ANCHOR_TOL_M).
    cover = []
    for p in track:
        kr, kc = int(p["lat"] / dlat), int(p["lon"] / dlon)
        on = set()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for idx in grid.get((kr + dr, kc + dc), ()):
                    if _haversine_m(p["lat"], p["lon"], line_pts[idx][0], line_pts[idx][1]) <= ANCHOR_TOL_M:
                        on.add(idx)
        cover.append(on)

    from collections import Counter
    cnt = Counter()
    n = len(track)
    b = 0
    spans = []
    for a in range(n):
        if b < a:
            b = a
            cnt = Counter()
            for idx in cover[a]:
                cnt[idx] += 1
        while b + 1 < n and (track[b]["d"] - track[a]["d"]) < loop_len * 0.95:
            b += 1
            for idx in cover[b]:
                cnt[idx] += 1
        wdist = track[b]["d"] - track[a]["d"]
        closes = _haversine_m(track[a]["lat"], track[a]["lon"],
                              track[b]["lat"], track[b]["lon"]) <= ANCHOR_CLOSE_M
        if (closes and loop_len * 0.80 <= wdist <= loop_len * 1.45
                and len(cnt) / n_line >= ANCHOR_COVER):
            spans.append((a, b, track[b]["t"] - track[a]["t"]))
        for idx in cover[a]:
            cnt[idx] -= 1
            if cnt[idx] <= 0:
                del cnt[idx]

    spans.sort(key=lambda x: x[2])           # fastest first
    kept, used = [], []
    for a, b, _t in spans:
        if any(not (b <= ka or a >= kb) for ka, kb in used):
            continue
        used.append((a, b))
        kept.append((a, b))
    return kept


def _span_effort(track, a, b, rid):
    """Build an effort record (compatible with _segment_from_efforts) from a track span."""
    sub = track[a:b + 1]
    gain = sum(max(0.0, (sub[k]["elev"] or 0) - (sub[k - 1]["elev"] or 0))
               for k in range(1, len(sub))
               if sub[k]["elev"] is not None and sub[k - 1]["elev"] is not None)
    net = ((sub[-1]["elev"] or 0) - (sub[0]["elev"] or 0)) \
        if sub[0]["elev"] is not None and sub[-1]["elev"] is not None else 0.0
    return {"run_id": rid, "time_s": sub[-1]["t"] - sub[0]["t"],
            "dist_m": sub[-1]["d"] - sub[0]["d"], "sub": sub, "net_elev": net, "gain_m": gain}


def _build_anchored_segments(seqs, tracks, ref_lat, run_by_id):
    """Fixed-line segments for the ANCHORS that mining can't recover (too few closed laps)."""
    segs = []
    for anchor in ANCHORS:
        inst = _derive_anchor_line(anchor, seqs)
        if not inst:
            continue
        line = _resample_ll(inst["sub"], ANCHOR_LINE_STEP)
        loop_len = inst["dist_m"]
        efforts = []
        for rid, track in tracks.items():
            for a, b in _match_anchor_spans(line, loop_len, track, ref_lat):
                efforts.append(_span_effort(track, a, b, rid))
        if len(efforts) < 2:        # need a couple of real laps to be worth showing
            continue
        # A pinned loop is legitimate even when its only valid laps are one session's,
        # so the distinct-run and span gates are relaxed (closure already filters non-laps).
        seg = _segment_from_efforts(efforts, run_by_id, force_type="loop",
                                    min_runs=1, min_span=0)
        if not seg:
            continue
        # Draw the clean derived line (not a re-derived medoid) and pin the curated name.
        seg["name"] = anchor["name"]
        seg["polyline"] = [[round(la, 5), round(lo, 5)] for la, lo in _simplify(line)]
        seg["anchored"] = True
        segs.append(seg)
    return segs


def _span_days(rows):
    if len(rows) < 2:
        return 0
    try:
        from datetime import datetime
        ds = [datetime.strptime(r["date_iso"], "%Y-%m-%d") for r in rows]
        return (max(ds) - min(ds)).days
    except (ValueError, KeyError):
        return 0


def _dedupe(segments):
    """Drop segments that substantially overlap another with more attempts.
    Overlap = sharing >70% of effort run-ids and similar length."""
    kept = []
    order = sorted(segments, key=lambda s: (-s["n_efforts"], -s["length_m"]))
    for s in order:
        if s.get("anchored"):        # curated fixed-line segments are always kept
            kept.append(s)
            continue
        s_runs = {e["run_id"] for e in s["efforts"]}
        dup = False
        for k in kept:
            # A loop and a point-to-point benchmark over the same ground are different
            # comparisons (one times a circuit, the other a stretch) — keep both.
            if (s["type"] == "loop") != (k["type"] == "loop"):
                continue
            k_runs = {e["run_id"] for e in k["efforts"]}
            inter = len(s_runs & k_runs)
            smaller = min(len(s_runs), len(k_runs))
            if smaller and inter / smaller > 0.7:
                lo, hi = sorted((s["length_m"], k["length_m"]))
                ca, cb = _poly_centroid(s["polyline"]), _poly_centroid(k["polyline"])
                same_place = _haversine_m(ca[0], ca[1], cb[0], cb[1]) < 250
                # Near-total run overlap AND same location means it's the one feature
                # reached two ways (e.g. a climb approached from different streets, so one
                # is longer) — merge regardless of length, keeping the higher-effort one.
                # The location check stops distinct routes run on the same days merging.
                near_total = inter / smaller >= 0.9 and same_place and s["type"] != "loop"
                if near_total or (hi and lo / hi > 0.6):
                    dup = True
                    break
        if not dup:
            kept.append(s)
    return kept


# ---------------------------------------------------------------------------
# Naming via cached OSM reverse geocoding (Nominatim)
# ---------------------------------------------------------------------------

def _geo_key(poly):
    a, mid, b = poly[0], poly[len(poly) // 2], poly[-1]
    return "|".join(f"{p[0]:.4f},{p[1]:.4f}" for p in (a, mid, b))


def _reverse_geocode(lat, lon, zoom=18):
    params = urllib.parse.urlencode({
        "format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "zoom": zoom, "addressdetails": 1,
    })
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _located_names(cache):
    """(centroid, name) for every cached entry, so a name survives small polyline jitter
    between rebuilds. The cache is keyed on exact endpoints, which miss when the drawn
    line shifts run-to-run; matching by location instead keeps names stable."""
    out = []
    for k, name in cache.items():
        try:
            pts = [tuple(float(x) for x in p.split(",")) for p in k.split("|")]
            out.append(((sum(p[0] for p in pts) / len(pts),
                         sum(p[1] for p in pts) / len(pts)), name))
        except (ValueError, IndexError):
            continue
    return out


def _nearest_name(cen, located, max_m):
    best, best_d = None, max_m
    for (la, lo), name in located:
        d = _haversine_m(cen[0], cen[1], la, lo)
        if d <= best_d:
            best, best_d = name, d
    return best


def _name_segments(segments):
    cache = _load_json(GEO_CACHE) or {}
    located = _located_names(cache)
    dirty = False
    for s in segments:
        if s.get("name"):            # anchored segments carry a curated name already
            continue
        key = _geo_key(s["polyline"])
        if key in cache:
            s["name"] = cache[key]
            continue
        name = _derive_name(s)
        if name:
            cache[key] = name
            s["name"] = name
            located.append((_poly_centroid(s["polyline"]), name))
            dirty = True
        else:
            # Network lookup failed (offline / rate-limited): reuse the nearest name we
            # already resolved before showing raw coordinates.
            s["name"] = _nearest_name(_poly_centroid(s["polyline"]), located, 120.0) \
                or _fallback_name(s)
    if dirty:
        _save_json(GEO_CACHE, cache)


def _derive_name(s):
    """Query OSM for nearby road / feature names and compose a label. Returns
    None on any network failure (caller falls back to a generic name)."""
    poly = s["polyline"]
    a, mid, b = poly[0], poly[len(poly) // 2], poly[-1]
    roads = []
    features = []
    suburb = None
    try:
        for i, (lat, lon) in enumerate((mid, a, b)):
            data = _reverse_geocode(lat, lon)
            time.sleep(1.1)   # honour Nominatim usage policy (<= 1 req/s)
            addr = data.get("address", {}) or {}
            road = addr.get("road") or addr.get("pedestrian") or addr.get("footway")
            if road:
                roads.append(road)
            suburb = suburb or addr.get("suburb") or addr.get("neighbourhood") or addr.get("village")
            top = data.get("name")
            cat = data.get("category") or data.get("type")
            if i == 0 and top and top not in roads and cat in (
                "leisure", "tourism", "park", "nature_reserve", "attraction",
                "garden", "recreation_ground", "common", "heritage"):
                features.append(top)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    road = _most_common(roads)
    feature = features[0] if features else None
    seg_type = s["type"]
    if seg_type == "loop":
        base = feature or road or (suburb and f"{suburb}")
        label = f"{base} loop" if base else None
    elif seg_type == "climb":
        base = road or feature
        label = f"{base} climb" if base else None
    else:
        uniq = _ordered_unique(roads)
        if len(uniq) >= 2:
            label = f"{uniq[0]} to {uniq[-1]}"
        else:
            label = road or feature
    if label and suburb and suburb.lower() not in label.lower():
        label = f"{label} ({suburb})"
    return label


def _disambiguate_names(segments):
    """Append the length to any segments that ended up with an identical name."""
    counts = defaultdict(int)
    for s in segments:
        counts[s["name"]] += 1
    for s in segments:
        if counts[s["name"]] > 1:
            s["name"] = f"{s['name']} · {s['length_str']}"


def _fallback_name(s):
    mid = s["polyline"][len(s["polyline"]) // 2]
    kind = {"loop": "Loop", "climb": "Climb", "segment": "Segment"}[s["type"]]
    return f"{kind} near {mid[0]:.3f}, {mid[1]:.3f}"


def _most_common(items):
    if not items:
        return None
    counts = defaultdict(int)
    for it in items:
        counts[it] += 1
    return max(counts, key=counts.get)


def _ordered_unique(items):
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# OSM map-matching: snap a segment's GPS trace onto the OpenStreetMap path network
# (via Overpass) and route along it, so the drawn line follows real streets instead of
# a single run's GPS wobble. Cached on disk; falls back to the raw trace when offline.
# ---------------------------------------------------------------------------

def _match_segments(segments):
    cache = _load_json(MATCH_CACHE) or {}
    dirty = False
    for s in segments:
        key = _geo_key(s["polyline"])
        if key in cache:
            if cache[key]:
                s["polyline"] = cache[key]
            continue
        matched = _match_polyline(s["polyline"], closed=(s["type"] == "loop"))
        # Only trust a match that keeps roughly the benchmark length, stays close to the
        # actual trace, and is no jaggier than the real run. Otherwise the snap wandered
        # onto the wrong roads or flickered between adjacent parallel ways (a zig-zag the
        # runner never ran), and the smooth raw trace is more faithful.
        if matched:
            ml = _polyline_len(matched)
            dev = _polyline_deviation(matched, s["polyline"])
            turn_gain = _reversal_rate(matched) - _reversal_rate(s["polyline"])
            if (not (0.7 * s["length_m"] <= ml <= 1.4 * s["length_m"])
                    or dev > MATCH_MAX_DEV_M
                    or turn_gain > MATCH_MAX_TURN_GAIN):
                matched = None
        cache[key] = matched or []
        dirty = True
        if matched:
            s["polyline"] = matched
        time.sleep(1.0)   # be polite to the public Overpass endpoint
    if dirty:
        _save_json(MATCH_CACHE, cache)


def _overpass_ways(bbox):
    s, w, n, e = bbox
    query = (f"[out:json][timeout:25];"
             f"way[highway]({s},{w},{n},{e});(._;>;);out;")
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_walk_graph(osm):
    """Return (node_coords, adjacency) for runnable ways only."""
    coords = {}
    for el in osm.get("elements", []):
        if el.get("type") == "node":
            coords[el["id"]] = (el["lat"], el["lon"])
    adj = defaultdict(list)
    for el in osm.get("elements", []):
        if el.get("type") != "way":
            continue
        hw = el.get("tags", {}).get("highway")
        if not hw or hw in MATCH_EXCLUDE:
            continue
        nd = [n for n in el.get("nodes", []) if n in coords]
        for a, b in zip(nd, nd[1:]):
            d = _haversine_m(coords[a][0], coords[a][1], coords[b][0], coords[b][1])
            adj[a].append((b, d))
            adj[b].append((a, d))
    return coords, adj


def _walk_edges(adj):
    """Unique undirected edges (node-id pairs) of the walking graph."""
    seen, edges = set(), []
    for u in adj:
        for v, _ in adj[u]:
            key = (u, v) if u < v else (v, u)
            if key not in seen:
                seen.add(key)
                edges.append(key)
    return edges


def _polyline_len(poly):
    return sum(_haversine_m(poly[i - 1][0], poly[i - 1][1], poly[i][0], poly[i][1])
               for i in range(1, len(poly)))


def _poly_centroid(poly):
    n = len(poly) or 1
    return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)


def _reversal_rate(poly):
    """Fraction of vertices where the path turns sharper than MATCH_TURN_DEG. A smooth run
    has few; a snap that flickers between two parallel ways spikes this, so comparing the
    matched line's rate against the raw trace's flags zig-zag artefacts."""
    if len(poly) < 3:
        return 0.0
    sharp = 0
    for i in range(2, len(poly)):
        ax, ay = poly[i - 1][1] - poly[i - 2][1], poly[i - 1][0] - poly[i - 2][0]
        bx, by = poly[i][1] - poly[i - 1][1], poly[i][0] - poly[i - 1][0]
        ang = abs(math.degrees(math.atan2(ax * by - ay * bx, ax * bx + ay * by)))
        if ang > MATCH_TURN_DEG:
            sharp += 1
    return sharp / (len(poly) - 2)


def _polyline_deviation(a, b):
    """Mean distance (m) from each point of `a` to the nearest segment of polyline `b`."""
    lat0, lon0 = a[0][0], a[0][1]
    mx = 111320.0 * math.cos(math.radians(lat0))

    def xy(p):
        return ((p[1] - lon0) * mx, (p[0] - lat0) * 111320.0)

    bxy = [xy(p) for p in b]
    total = 0.0
    for p in a:
        px, py = xy(p)
        best = None
        for k in range(1, len(bxy)):
            ax, ay = bxy[k - 1]
            bx, by = bxy[k]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            qx, qy = ax + t * dx, ay + t * dy
            d = math.hypot(px - qx, py - qy)
            if best is None or d < best:
                best = d
        total += best or 0.0
    return total / len(a) if a else 0.0


def _resample_track(poly, step):
    pts = [(p[0], p[1]) for p in poly]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + _haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    total = cum[-1]
    if total <= 0:
        return pts
    out, j, t = [], 0, 0.0
    while t <= total:
        while j < len(cum) - 1 and cum[j + 1] < t:
            j += 1
        nxt = min(j + 1, len(pts) - 1)
        seg = cum[nxt] - cum[j]
        f = (t - cum[j]) / seg if seg > 0 else 0.0
        out.append((pts[j][0] + f * (pts[nxt][0] - pts[j][0]),
                    pts[j][1] + f * (pts[nxt][1] - pts[j][1])))
        t += step
    return out


def _match_polyline(poly, closed):
    """Snap a polyline onto the OSM walking network by projecting each point to the
    nearest path edge (not routing between snaps — that detours wildly when the trace
    runs between parallel streets). The result follows real roads while preserving the
    run's shape and length. Returns None on failure (offline, no nearby paths, sparse)."""
    if len(poly) < 3:
        return None
    lats = [a for a, _ in poly]
    lons = [b for _, b in poly]
    pad = 0.0015
    bbox = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
    try:
        osm = _overpass_ways(bbox)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None

    coords, adj = _build_walk_graph(osm)
    edges = _walk_edges(adj)
    if len(edges) < 3:
        return None

    # Work in local metres about the trace's centre.
    lat0 = sum(lats) / len(lats)
    lon0 = sum(lons) / len(lons)
    mx = 111320.0 * math.cos(math.radians(lat0))

    def to_xy(lat, lon):
        return ((lon - lon0) * mx, (lat - lat0) * 111320.0)

    exy = [(to_xy(*coords[a]), to_xy(*coords[b])) for a, b in edges]

    pts = []
    for la, lo in _resample_track(poly, MATCH_STEP_M):
        px, py = to_xy(la, lo)
        best_d2, best_q = None, None
        for (ax, ay), (bx, by) in exy:
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            qx, qy = ax + t * dx, ay + t * dy
            d2 = (px - qx) ** 2 + (py - qy) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2, best_q = d2, (qx, qy)
        if best_d2 is None or best_d2 > MATCH_SNAP_M ** 2:
            continue
        qx, qy = best_q
        c = [round(lat0 + qy / 111320.0, 5), round(lon0 + qx / mx, 5)]
        if not pts or pts[-1] != c:
            pts.append(c)

    if closed and pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    if len(pts) < 4:
        return None
    return _simplify(pts, 200)


# ---------------------------------------------------------------------------
# JSON cache IO
# ---------------------------------------------------------------------------

def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_json(path, data):
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"Segments: could not write {path.name}: {e}")


# ---------------------------------------------------------------------------
# Panel HTML
# ---------------------------------------------------------------------------

TYPE_BADGE = {
    "loop":    ("Loop", "#5cb85c"),
    "climb":   ("Climb", "#e0a020"),
    "segment": ("Segment", "#5a9fd4"),
}


def body_segments(segments, updated):
    if not segments:
        return (
            "<div class=\"seg-empty\">"
            "<p class=\"seg-empty-title\">No benchmark segments yet</p>"
            "<p class=\"seg-empty-sub\">Segments appear once you have repeated a route at "
            f"least {MIN_RUNS} times over {MIN_SPAN_DAYS}+ days. Keep running your regular "
            "loops and climbs and they will show up here automatically.</p>"
            "</div>"
        )

    head = (
        "<div class=\"seg-head\">"
        "<p class=\"seg-head-title\">Benchmark segments</p>"
        f"<p class=\"seg-head-sub\">Auto-detected routes you repeat &middot; {len(segments)} "
        f"segments &middot; updated {updated}</p>"
        "</div>"
    )

    cards = []
    for s in segments:
        label, colour = TYPE_BADGE.get(s["type"], TYPE_BADGE["segment"])
        n = s["n_efforts"]
        climb_meta = (f"<span class=\"seg-meta\">&uarr; {s['gain_m']} m &middot; "
                      f"{s['grade']}%</span>") if s["type"] == "climb" else ""
        rows = "".join(
            "<tr" + (" class=\"seg-pr\"" if e["time_s"] == s["pr_time_s"] else "") + ">"
            f"<td>{e['date_long']}</td>"
            f"<td class=\"seg-name-cell\">{_esc(e['name'])}</td>"
            f"<td>{e['time_str']}{' 🏆' if e['time_s'] == s['pr_time_s'] else ''}</td>"
            f"<td>{e['pace_str']}</td>"
            f"<td>{e['ga_pace_str']}</td>"
            "</tr>"
            for e in sorted(s["efforts"], key=lambda x: x["date_iso"], reverse=True)
        )
        cards.append(
            f"<div class=\"seg-card\" data-seg=\"{s['id']}\">"
            "  <div class=\"seg-card-head\">"
            f"    <span class=\"seg-badge\" style=\"color:{colour};border-color:{colour}\">{label}</span>"
            f"    <span class=\"seg-title\" onclick=\"openSegOverlay({s['id']})\" "
            f"title=\"Open interactive map\">{_esc(s['name'])}</span>"
            "  </div>"
            "  <div class=\"seg-stats\">"
            f"    <span class=\"seg-meta\">{s['length_str']}</span>{climb_meta}"
            f"    <span class=\"seg-meta\">{n} attempts</span>"
            f"    <span class=\"seg-pr-stat\">PR {s['pr_time_str']} &middot; {s['pr_date']}</span>"
            "  </div>"
            "  <div class=\"seg-body\">"
            f"    <div class=\"seg-map\" id=\"seg-map-{s['id']}\"></div>"
            f"    <div class=\"seg-trend\" id=\"seg-trend-{s['id']}\"></div>"
            "  </div>"
            f"  <details class=\"seg-attempts\"><summary>{n} attempts</summary>"
            "    <table class=\"seg-table\"><thead><tr>"
            "<th>Date</th><th>Run</th><th>Time</th><th>Pace</th><th>GA pace</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
            "  </details>"
            "</div>"
        )

    return head + "<div class=\"seg-grid\">" + "".join(cards) + "</div>"


def _esc(text):
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

SEGMENTS_CSS = """
#panel-segments{height:calc(100vh - 44px);overflow-y:auto;padding:1rem}
.seg-head{margin-bottom:1rem}
.seg-head-title{font-size:18px;font-weight:700;color:#eee;margin:0 0 2px}
.seg-head-sub{font-size:11px;color:#666;margin:0}
.seg-empty{max-width:480px;margin:4rem auto;text-align:center}
.seg-empty-title{font-size:16px;font-weight:600;color:#bbb;margin:0 0 8px}
.seg-empty-sub{font-size:13px;color:#666;line-height:1.6;margin:0}
.seg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.seg-card{background:#222;border:1px solid #3a3a3a;border-radius:10px;padding:14px;display:flex;flex-direction:column}
.seg-card-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.seg-badge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  padding:2px 8px;border-radius:999px;border:1px solid currentColor;background:rgba(255,255,255,.04)}
.seg-title{font-size:15px;font-weight:700;color:#eee;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;cursor:pointer}
.seg-title:hover{color:#5cb85c;text-decoration:underline}
.seg-stats{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;margin-bottom:10px}
.seg-meta{font-size:11px;color:#888}
.seg-pr-stat{font-size:11px;color:#5cb85c;font-weight:600;margin-left:auto}
.seg-body{display:flex;gap:10px;margin-bottom:8px}
.seg-map{width:46%;min-width:130px;height:150px;border-radius:8px;border:1px solid #333;background:#111;overflow:hidden}
.seg-trend{flex:1;height:150px;background:#1c1c1c;border:1px solid #333;border-radius:8px;position:relative}
.seg-trend svg{display:block;width:100%;height:100%}
.seg-attempts{font-size:12px}
.seg-attempts summary{cursor:pointer;color:#5cb85c;font-size:11px;font-weight:600;padding:4px 0;outline:none}
.seg-attempts summary:hover{text-decoration:underline}
.seg-table{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:6px}
.seg-table th{font-size:9.5px;font-weight:600;color:#555;text-align:left;padding:4px 6px;border-bottom:1px solid #2a2a2a}
.seg-table th:not(:first-child):not(:nth-child(2)){text-align:right}
.seg-table td{padding:5px 6px;border-bottom:1px solid #1e1e1e;color:#bbb}
.seg-table td:not(:first-child):not(:nth-child(2)){text-align:right;font-variant-numeric:tabular-nums}
.seg-table .seg-name-cell{color:#999;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
.seg-table tr.seg-pr td{color:#5cb85c;font-weight:600}
.seg-trend-tip{position:absolute;pointer-events:none;background:#000;border:1px solid #3a3a3a;
  border-radius:6px;padding:3px 7px;font-size:10.5px;color:#eee;white-space:nowrap;
  transform:translate(-50%,calc(-100% - 8px));opacity:0;transition:opacity .08s;z-index:5}

/* Route endpoint markers: green start disc, checkered finish flag for the end */
.seg-mk{width:100%;height:100%;box-sizing:border-box;border-radius:50%;
  border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.55)}
.seg-mk-start{background:#2ecc40}
.seg-mk-end{background-color:#fff;
  background-image:
    linear-gradient(45deg,#111 25%,transparent 25%,transparent 75%,#111 75%),
    linear-gradient(45deg,#111 25%,transparent 25%,transparent 75%,#111 75%);
  background-size:6px 6px;background-position:0 0,3px 3px}

/* Expanded segment overlay (interactive map + trend) */
.seg-overlay{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:600;display:none}
.seg-overlay.open{display:flex;justify-content:center}
.so-box{display:flex;flex-direction:column;width:100%;max-width:1300px;height:100%;
  padding:12px 14px;gap:8px;overflow:hidden}
.so-head{display:flex;justify-content:space-between;align-items:center;flex:0 0 auto;gap:10px}
.so-title{font-size:16px;font-weight:700;color:#eee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.so-close{background:#222;border:1px solid #3a3a3a;color:#bbb;font-size:15px;line-height:1;
  width:30px;height:30px;border-radius:6px;cursor:pointer;flex:0 0 auto}
.so-close:hover{color:#fff;border-color:#5cb85c}
.so-body{flex:1 1 auto;display:flex;gap:12px;min-height:0}
.so-map{flex:1 1 auto;min-height:200px;border-radius:8px;border:1px solid #3a3a3a;background:#111}
.so-side{flex:0 0 440px;max-width:46%;display:flex;flex-direction:column;gap:10px;min-height:0}
.so-trend{flex:0 0 auto;height:190px;background:#1c1c1c;border:1px solid #333;border-radius:8px;position:relative}
.so-trend svg{display:block;width:100%;height:100%}
.so-elev{flex:0 0 auto;height:150px;background:#1c1c1c;border:1px solid #333;border-radius:8px;position:relative}
.so-elev svg{display:block;width:100%;height:100%}
.so-elev-empty{display:flex;align-items:center;justify-content:center;height:100%;font-size:11px;color:#555}
.so-sub-label{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:.04em;margin:2px 0 -2px}
.so-stats{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:#aaa}
.so-stats b{color:#eee;font-weight:600}
.so-hint{font-size:10px;color:#666;margin-top:-2px}
.so-attempts{flex:1 1 auto;overflow-y:auto;min-height:120px;border-top:1px solid #2a2a2a;padding-top:6px}
.so-att-head{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:.04em;margin:2px 0 6px}
.so-att{padding:7px 9px;border:1px solid #2e2e2e;border-radius:7px;margin-bottom:6px;
  cursor:pointer;background:#1e1e1e;transition:border-color .1s,background .1s}
.so-att:hover{border-color:#5cb85c;background:#232a23}
.so-att-dead{cursor:default;opacity:.55}
.so-att-dead:hover{border-color:#2e2e2e;background:#1e1e1e}
.so-att-pr{border-color:#3a5a3a}
.so-att-row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.so-att-time{font-size:13px;font-weight:700;color:#eee;font-variant-numeric:tabular-nums}
.so-att-pr .so-att-time{color:#5cb85c}
.so-att-name{font-size:11.5px;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.so-att-sub{margin-top:2px;font-size:10.5px;color:#777}
@media(max-width:760px){.so-body{flex-direction:column}.so-side{flex:0 0 auto;max-width:none}}
"""
