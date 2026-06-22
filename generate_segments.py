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
LOOP_MIN_LEN_M  = 600.0
CLIMB_MIN_GAIN  = 15.0    # metres of net ascent for a "climb"
CLIMB_MIN_GRADE = 0.025   # ... and average grade
LOOP_JACCARD    = 0.55    # cell-overlap to treat two runs' loops as the same loop
MAX_SEGMENTS    = 40      # safety cap on rendered segments

CACHE_DIR       = Path(__file__).parent / "csv_data"
SEG_CACHE       = CACHE_DIR / "segments_cache.json"
GEO_CACHE       = CACHE_DIR / "segment_geocode_cache.json"

NOMINATIM_URL   = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT      = "Strava-Analysis-Hub/1.0 (personal training dashboard)"


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
    h.update(f"params:{CELL_M},{MIN_RUNS},{MIN_LEN_M},{MATCH_COVER}".encode())
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

    segments = _dedupe(segments)
    # Best benchmarks first: most-run, then longest.
    segments.sort(key=lambda s: (-s["n_efforts"], -s["length_m"]))
    segments = segments[:MAX_SEGMENTS]

    _name_segments(segments)
    _disambiguate_names(segments)
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


def _segment_from_efforts(efforts, run_by_id, force_type=None):
    """Turn a set of per-run traversals into a segment dict, or None if it fails
    the conservative length / span filters."""
    if len(efforts) < MIN_RUNS:
        return None
    seg_len = _median(efforts)
    if seg_len < MIN_LEN_M:
        return None
    ref = min(efforts, key=lambda e: abs(e["dist_m"] - seg_len))
    poly = _simplify([[p["lat"], p["lon"]] for p in ref["sub"]])
    if len(poly) < 2:
        return None
    endpoints_close = _haversine_m(poly[0][0], poly[0][1], poly[-1][0], poly[-1][1]) < LOOP_CLOSE_M
    net_elev, gain_m = ref["net_elev"], ref["gain_m"]
    seg_type = force_type or _classify(seg_len, net_elev, gain_m, endpoints_close)
    len_km = seg_len / 1000.0

    rows = []
    for e in efforts:
        r = run_by_id[e["run_id"]]
        t = e["time_s"]
        gx = ga_time(t, e["gain_m"], len_km) if len_km else t
        rows.append({
            "run_id":   e["run_id"],
            "date_iso": r["date_iso"],
            "date_long": r["date_long"],
            "name":     r["name"],
            "time_s":   round(t, 1),
            "time_str": fmt_time(t),
            "pace_str": fmt_pace(t, seg_len),
            "ga_pace_str": fmt_pace(gx, seg_len) if gx > 0 else "—",
        })
    rows.sort(key=lambda x: x["date_iso"])
    if _span_days(rows) < MIN_SPAN_DAYS:
        return None
    pr = min(rows, key=lambda x: x["time_s"])
    return {
        "type":      seg_type,
        "length_m":  round(seg_len),
        "length_str": f"{len_km:.2f} km",
        "gain_m":    round(gain_m),
        "grade":     round((net_elev / seg_len) * 100, 1) if seg_len else 0.0,
        "polyline":  [[round(a, 5), round(b, 5)] for a, b in poly],
        "efforts":   rows,
        "n_efforts": len(rows),
        "span_days": _span_days(rows),
        "pr_time_s": pr["time_s"],
        "pr_time_str": pr["time_str"],
        "pr_date":   pr["date_long"],
    }


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


def _run_loops(seq):
    """All closed loops inside one run's cell sequence (at multiple scales). A loop
    returns near an earlier cell, is long enough, and (unlike an out-and-back) visits
    most of its cells only once. Nested near-duplicate loops are collapsed."""
    cells = [c for c, _ in seq]
    pts = [p for _, p in seq]
    pos = defaultdict(list)
    for idx, c in enumerate(cells):
        pos[c].append(idx)

    cand = []
    for ps in pos.values():
        if len(ps) < 2:
            continue
        i, j = ps[0], ps[-1]
        d = pts[j]["d"] - pts[i]["d"]
        if d < LOOP_MIN_LEN_M or d > 12000:
            continue
        sub = cells[i:j + 1]
        if len(set(sub)) / len(sub) < 0.70:   # mostly-retraced => out-and-back
            continue
        cand.append((i, j, d))

    # Collapse near-identical loops, keeping the larger of any heavily-overlapping pair.
    cand.sort(key=lambda x: -x[2])
    loops = []
    seen = []
    for i, j, d in cand:
        cs = frozenset(cells[i:j + 1])
        if any(len(cs & k) / max(1, min(len(cs), len(k))) > 0.8 for k in seen):
            continue
        seen.append(cs)
        loops.append(_loop_record(cells, pts, i, j))
    return loops


def _detect_loops(seqs, run_by_id):
    """Find loops repeated across runs by clustering every run's loops on cell
    overlap. Clustering compares against a fixed representative (not a growing
    union) so a small loop nested inside a longer run still matches."""
    per_run = []
    for rid, seq in seqs.items():
        for lp in _run_loops(seq):
            lp["run_id"] = rid
            per_run.append(lp)

    clusters = []   # each: {"rep": frozenset, "rep_d": float, "members": [loop,...]}
    for lp in sorted(per_run, key=lambda x: -x["dist_m"]):
        placed = False
        for cl in clusters:
            inter = len(lp["cells"] & cl["rep"])
            smaller = min(len(lp["cells"]), len(cl["rep"]))
            ratio = lp["dist_m"] / cl["rep_d"] if cl["rep_d"] else 0
            # Same geography AND comparable length — otherwise a small loop nested
            # inside a larger circuit would be mispriced at the cluster's length.
            if smaller and inter / smaller >= LOOP_JACCARD and DIST_LO <= ratio <= DIST_HI:
                cl["members"].append(lp)
                placed = True
                break
        if not placed:
            clusters.append({"rep": lp["cells"], "rep_d": lp["dist_m"], "members": [lp]})

    segments = []
    for cl in clusters:
        by_run = {}   # fastest loop per run
        for m in cl["members"]:
            if m["run_id"] not in by_run or m["time_s"] < by_run[m["run_id"]]["time_s"]:
                by_run[m["run_id"]] = m
        if len(by_run) < MIN_RUNS:
            continue
        seg = _segment_from_efforts(list(by_run.values()), run_by_id, force_type="loop")
        if seg:
            segments.append(seg)
    return segments


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
        s_runs = {e["run_id"] for e in s["efforts"]}
        dup = False
        for k in kept:
            k_runs = {e["run_id"] for e in k["efforts"]}
            inter = len(s_runs & k_runs)
            smaller = min(len(s_runs), len(k_runs))
            if smaller and inter / smaller > 0.7:
                lo, hi = sorted((s["length_m"], k["length_m"]))
                if hi and lo / hi > 0.6:
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


def _name_segments(segments):
    cache = _load_json(GEO_CACHE) or {}
    dirty = False
    for s in segments:
        key = _geo_key(s["polyline"])
        if key in cache:
            s["name"] = cache[key]
            continue
        name = _derive_name(s)
        if name:
            cache[key] = name
            s["name"] = name
            dirty = True
        else:
            s["name"] = _fallback_name(s)
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
            f"    <span class=\"seg-title\">{_esc(s['name'])}</span>"
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
.seg-title{font-size:15px;font-weight:700;color:#eee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
"""
