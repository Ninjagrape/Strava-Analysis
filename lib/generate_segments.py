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

import os
import json
import math
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from generate_dashboards import fmt_time, fmt_pace, ga_time
from config import load_config

# Per-phase timing inside build_segments, printed only under --profile (STRAVA_PROFILE).
PROFILE = bool(os.environ.get("STRAVA_PROFILE"))
SEG_DEBUG = bool(os.environ.get("STRAVA_SEG_DEBUG"))   # print per-segment match/reject diagnostics

# ---------------------------------------------------------------------------
# Tunable detection parameters (conservative: few, high-confidence benchmarks)
# ---------------------------------------------------------------------------

CELL_M          = 25.0    # spatial grid cell size for matching
DENSIFY_M       = 15.0    # max gap between track points (interpolate finer)
MIN_RUNS        = 2       # a segment must be completed by >= this many runs
MIN_SPAN_DAYS   = 14      # ... spanning >= this many days
MIN_LEN_M       = 400.0   # ... and be at least this long
MATCH_COVER     = 0.80    # fraction of segment cells a run must visit to "complete"
DIST_LO, DIST_HI = 0.70, 1.40   # allowed traversal distance vs segment length
LOOP_CLOSE_M    = 60.0    # start/end closer than this -> loop
LOOP_MIN_LEN_M  = 600.0   # phase 1: floor for *identifying* a distinct loop (high enough
                          # that sub-loops of a bigger circuit don't fragment it)
LOOP_LAP_MIN    = 300.0   # phase 2: floor when re-scanning a run to count individual laps
LOOP_MIN_SEG_M  = 350.0   # a loop's *drawn* single lap may be shorter than the point-to-point
                          # floor: a small lapped circuit (e.g. The Colonnade, ~400 m) is a
                          # legitimate benchmark, and its self-crossing closes a touch short
                          # of the true perimeter, so the median lap can dip just under 400.
CLIMB_MIN_GAIN  = 15.0    # metres of net ascent for a "climb"
CLIMB_MIN_GRADE = 0.025   # ... and average grade
CLIMB_MIN_LEN_M = 150.0   # a steep climb is a valid benchmark below the general MIN_LEN_M
                          # floor (the John St climb is ~210 m); flat stretches stay at MIN_LEN_M
CLIMB_REV_COVER = 0.75    # re-matching a descent corridor's reverse to time the climb uses
                          # a looser cell coverage than MATCH_COVER: the chain's cells come
                          # from the downhill runs, and ascents take a slightly different
                          # line, so 0.80 is too strict one way round. The climb still has to
                          # clear the full run-count, span and grade gates.
SUBCLIMB_MIN_GRADE = 0.06   # a corridor's steepest sub-window is surfaced as its own climb
                            # (e.g. the Russell St Burn, ~160 m @ 10.7%, inside the longer
                            # Milner->Russell corridor) only when this steep
SUBCLIMB_STEEPER   = 1.6    # ... and at least this many times the parent corridor's grade,
SUBCLIMB_MAX_FRAC  = 0.75   # ... and no longer than this fraction of the parent (else it is
                            # effectively the same benchmark and the parent already covers it).
CLIMB_EXT_MAX_M    = 400.0  # how far a clipped climb may be grown past each end to reach the
                            # hill's foot/crest (mining stops at junctions, not the real hill).
CLIMB_EXT_DROP_TOL = 1.5    # GPS-elevation noise tolerated while deciding the ground still
                            # drops toward the foot / rises toward the crest before stopping.
CLIMB_EXT_REVERSE_M = 35.0  # a reversal (descent past the crest / climb past the foot) must be
                            # sustained over this distance before we stop, so a small dip mid-
                            # hill doesn't cut the climb short of its real top or bottom.
SALVAGE_CLIMB_MAX_M = 650.0  # a corridor grown across a divergence junction (runs split two ways
                            # at the top, e.g. continue up Shirley vs turn onto Telopea) is longer
                            # than any single run's traversal, so it fails the whole-corridor
                            # effort gate and is dropped — with the shared climb up to the junction
                            # inside it. Salvage scans such a corridor for the sub-window the most
                            # runs actually complete that still classifies as a climb; this caps
                            # that window (beyond it the window is a whole route, not a hill).
SALVAGE_MIN_OVERLAP = 6     # a run must share this many of a failed corridor's cells to be worth
                            # match-testing during salvage (cheap prefilter before the O(n^2) scan).
LOOP_MIN_CELLS  = 8       # min cells between a loop's two near-coincident points
LOOP_UNIQUE_FRAC = 0.80   # a loop visits >= this fraction of its cells only once
LOOP_CLUSTER_M  = 120.0   # two runs' loops are the same loop if centroids within this
LOOP_LEN_RATIO  = 1.35    # ... and their lengths agree within this ratio
LOOP_DEDUPE_M   = 120.0   # two loops whose drawn centroids are within this are the same circuit
                          # recorded at different lap scales (e.g. a 400 m loop also caught as a
                          # 1.5-lap ~600 m pass); dedupe merges them, keeping the higher-effort
                          # version and pooling the other's unseen runs.
LOOP_MIN_ROUNDNESS = 0.20 # isoperimetric-quotient floor for a drawn loop; below this it is a
                          # thin sliver (a there-and-back on a parallel path the 25 m cell grid
                          # can't tell apart), not a real circuit. Tunable; printed under SEG_DEBUG.
LOOP_OVERLAP_FRAC = 0.60  # a non-loop segment whose footprint covers >= this fraction of a
                          # detected loop's cells is a loop fragment (e.g. a 3/4-loop), so drop it
LOOP_THRU_COV   = 0.50    # a "loop" a straight point-to-point segment covers this much of, while
LOOP_THRU_LENRATIO = 0.90 # being >= this fraction of the loop's own length, is really an up-and-back
                          # through two near-parallel ways (the segment is ~as long as the whole
                          # "loop", which a real circuit's through-corridors never are) — so drop it
CHAIN_SELFCROSS_M = 60.0  # within a mined chain, a later cell-centre this close to a much-earlier
                          # one (index gap >= LOOP_MIN_CELLS) is where the chain starts wrapping a
                          # loop; the chain is split there so a line+loop can't form one segment
CORRIDOR_END_TOL_M = 80.0 # two point-to-point segments whose starts and ends both align
                          # within this are the same stretch (GPS-split into two)
MAX_SEGMENTS    = 100      # safety cap on rendered segments

# The drawn line for a segment is the medoid effort, so it shifts run-to-run as new runs
# join. The name/match caches are keyed on the line's endpoints (_geo_key, ~11 m), so that
# drift misses the cache and re-hits Nominatim/Overpass even for a segment already named and
# snapped. These tolerances let a small drift reuse the nearby prior result instead, keeping
# outputs stable while removing nearly all the repeat network cost.
NAME_REUSE_M    = 40.0    # reuse a cached name when a prior entry's centroid is within this
MATCH_REUSE_M   = 50.0    # reuse a cached snap when a prior entry's centroid is within this
CACHE_PRUNE_M   = 150.0   # drop cache entries this far from every current segment (anti-bloat)

# Anchored segments: a loop you usually run *inside* a longer route never self-crosses, so
# closed-loop mining (which needs >= MIN_RUNS laps) can't see it. The fix is to derive a line
# from the cleanest self-crossing instance and match every run against it with a distance-
# bounded sliding window, so the entry point can be anywhere on the loop (Strava-style).
# _auto_anchor_segments does this automatically for loops that fall short of the closed-loop
# run gate; config.json `segment_anchors` are an explicit fallback for loops that never close
# in any run, or to pin a curated name. See AUTO_ANCHOR_MAX_CANDIDATES below.
ANCHOR_TOL_M    = 25.0    # a run point this close to the line counts as "on" it
ANCHOR_COVER    = 0.80    # fraction of the line a window must cover to complete
ANCHOR_NEAR_M   = 130.0   # cleanest instance must be within this of the anchor centre
ANCHOR_LINE_STEP = 10.0   # resample spacing of the derived line
ANCHOR_CLOSE_M  = 55.0    # a lap's start and end must be this close (a real loop closes;
                          # a pass that starts at home and ends mid-road is not a lap)
AUTO_ANCHOR_MAX_CANDIDATES = 25   # cap on auto-anchor loops tried (most self-crossings first),
                                  # bounds the cost of span-matching every candidate vs all runs
AUTO_ANCHOR_MIN_RUNS = 3          # an auto-anchor must be *traversed* (line-matched) by this many
                                  # distinct runs. Lower than MIN_RUNS (mined loops need closed
                                  # laps); an embedded traversal is a weaker per-instance signal,
                                  # but 3 distinct runs over MIN_SPAN_DAYS still means a real route,
                                  # and it rejects the 2-run incidental loops that flood a lower bar.
AUTO_ANCHOR_MINED_GAP_M = 120.0   # skip an auto-anchor candidate this close to a mined loop: the
                                  # loop is already detected, so an anchor there only re-times the
                                  # same circuit at a different scale (a duplicate/fragment)

CACHE_DIR       = Path(__file__).resolve().parent.parent / "cache"
SEG_CACHE       = CACHE_DIR / "segments_cache.json"
GEO_CACHE       = CACHE_DIR / "segment_geocode_cache.json"
MATCH_CACHE     = CACHE_DIR / "segment_match_cache.json"
STARS_CACHE     = CACHE_DIR / "segment_stars.json"   # user-starred segments (geo_keys); read at build

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
SNAP_INDEX_CELL_M = 60.0    # grid cell for the shared-graph edge index. > MATCH_SNAP_M so the
                            # 3x3 cell window round a query point always contains every edge
                            # within snap range, making the indexed snap identical to a full scan
SNAP_UNION_MAX_DEG = 0.6    # if all segments span more than this (~65 km) in lat or lon, the
                            # single union Overpass fetch would be too large, so fall back to the
                            # per-segment fetch path
GEO_MEMO_DP = 4             # decimals (~11 m) to dedupe reverse-geocode points within one build;
                            # matches _geo_key resolution and the project's name-stability radius


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


def _build_point_grid(tracks, ref_lat, cell_m=ANCHOR_TOL_M):
    """Shared spatial index over every run's track points: cell -> set of run ids present.

    Built once per rebuild at the on-line tolerance resolution, so the auto/config anchor
    paths can cheaply find which runs come near a candidate line instead of scanning every
    run's full track per candidate. Returns (grid, dlat, dlon)."""
    dlat = cell_m / 111320.0
    dlon = cell_m / (111320.0 * max(0.1, math.cos(math.radians(ref_lat))))
    grid: dict = defaultdict(set)
    for rid, track in tracks.items():
        for p in track:
            grid[(int(p["lat"] / dlat), int(p["lon"] / dlon))].add(rid)
    return grid, dlat, dlon


def _anchor_eligible_runs(line, anchor_idx, n_line):
    """Run ids whose track *could* cover >= ANCHOR_COVER of the line.

    For each line point, the 3x3 grid neighbourhood (cell = ANCHOR_TOL_M) holds every run
    with a point within tolerance, so the per-run count of distinct covered line indices is
    an upper bound on any window's coverage. Runs below ANCHOR_COVER here can never match,
    so dropping them is exact (no false negatives), while skipping their full O(n) scan."""
    grid, dlat, dlon = anchor_idx
    covered = defaultdict(set)
    for i, (la, lo) in enumerate(line):
        kr, kc = int(la / dlat), int(lo / dlon)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for rid in grid.get((kr + dr, kc + dc), ()):
                    covered[rid].add(i)
    return {rid for rid, idxs in covered.items() if len(idxs) / n_line >= ANCHOR_COVER}


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


def _split_chain_at_loop(chain):
    """A mined corridor that wraps a loop (a later cell returns near a much-earlier one) is a
    'line + loop' — e.g. a downhill then most of a circuit, or a loop then a climb. Split it at
    the loop so the straight pieces are timed on their own and the loop span is dropped (loops
    are detected separately by _detect_loops). Returns the non-loop sub-chains; a chain with no
    self-approach returns [chain]. Operates on the cell grid: cells within CHAIN_SELFCROSS_M are
    a few grid steps apart, so a small neighbourhood lookup finds the closure in O(n)."""
    n = len(chain)
    if n < LOOP_MIN_CELLS + 2:
        return [chain]
    rad = max(1, round(CHAIN_SELFCROSS_M / CELL_M))
    last_pos = {}
    span = None
    for j, (r0, c0) in enumerate(chain):
        for dr in range(-rad, rad + 1):
            for dc in range(-rad, rad + 1):
                i = last_pos.get((r0 + dr, c0 + dc))
                if i is not None and j - i >= LOOP_MIN_CELLS:
                    span = (i, j)
                    break
            if span:
                break
        if span:
            break
        last_pos[(r0, c0)] = j
    if span is None:
        return [chain]
    i, j = span
    out = []
    for piece in (chain[:i + 1], chain[j:]):
        if len(piece) >= 2:
            out.extend(_split_chain_at_loop(piece))
    return out


def _net_gain(sub):
    """(net_elev, gain_m) for a sub-track, tolerant of points missing elevation.

    net_elev is taken between the first and last points that actually carry an
    altitude (not the slice endpoints), so a single missing endpoint sample no longer
    forces the result to zero. gain_m sums positive deltas between consecutive
    elevation-bearing points.
    """
    elevs = [p["elev"] for p in sub if p.get("elev") is not None]
    if len(elevs) < 2:
        return 0.0, 0.0
    net = elevs[-1] - elevs[0]
    gain = sum(max(0.0, elevs[k] - elevs[k - 1]) for k in range(1, len(elevs)))
    return net, gain


def _steepest_window(sub, min_len=CLIMB_MIN_LEN_M):
    """Indices (i, j) of the contiguous window of `sub` (>= min_len metres) with the
    steepest average grade, or None. Used to pull a short steep pitch out of a longer
    corridor (e.g. the burn inside a gradual climb)."""
    n = len(sub)
    if n < 3:
        return None
    best = None
    for i in range(n):
        for j in range(i + 1, n):
            length = sub[j]["d"] - sub[i]["d"]
            if length < min_len:
                continue
            ei, ej = sub[i].get("elev"), sub[j].get("elev")
            if ei is None or ej is None:
                break  # extending j only makes it longer; grade already diluted past min_len
            grade = (ej - ei) / length
            if best is None or grade > best[0]:
                best = (grade, i, j)
            break  # shortest window from i that clears min_len is the steepest from i
    return (best[1], best[2]) if best else None


def _steep_subclimb(parent_sub, parent_grade, seqs, run_by_id, cell):
    """A separate steep-pitch climb extracted from a corridor's reference lap, or None.

    Surfaces only a window that is notably steeper than the parent and a real fraction
    shorter, then re-matches it across runs like any corridor so its attempts/PR are the
    runner's actual efforts on that pitch."""
    win = _steepest_window(parent_sub)
    if not win:
        return None
    sub = parent_sub[win[0]:win[1] + 1]
    win_len = sub[-1]["d"] - sub[0]["d"]
    net, _ = _net_gain(sub)
    grade = net / win_len if win_len else 0.0
    parent_len = parent_sub[-1]["d"] - parent_sub[0]["d"]
    if (grade < SUBCLIMB_MIN_GRADE
            or grade < SUBCLIMB_STEEPER * max(parent_grade, 0.0)
            or win_len > SUBCLIMB_MAX_FRAC * parent_len):
        return None
    win_cells, last = [], None
    for p in sub:
        c = cell(p["lat"], p["lon"])
        if c != last:
            win_cells.append(c)
            last = c
    if len(win_cells) < 3:
        return None
    efforts = _collect_efforts(win_cells, win_len, seqs)
    if len(efforts) < MIN_RUNS:
        return None
    return _segment_from_efforts(efforts, run_by_id, force_type="climb")


def _salvage_climb_from_corridor(piece, seqs, seqcells, run_by_id, ref_lat):
    """Recover a climb hidden inside a corridor that failed the whole-corridor effort gate.

    Corridor mining grows across junctions, so a corridor can run past a divergence (where the
    runner sometimes continues, sometimes turns off) and end up longer than any single run's
    traversal. No run then covers MATCH_COVER of it, so it collects zero efforts and is dropped
    — taking the shared climb up to the junction (which every run *does* complete) with it. This
    scans the corridor for the sub-window the most runs complete that still classifies as a
    climb, and returns that segment (or None). Ties break toward the longer window so the climb
    is drawn to its full shared extent, not clipped to the steepest pitch. Only for corridors
    already known to fail the full-length gate; the window still clears every climb gate via
    _segment_from_efforts, so this can only add real benchmarks, never relax the bar."""
    n = len(piece)
    if n < LOOP_MIN_CELLS:
        return None
    piece_cells = set(piece)
    elig = {rid: seqs[rid] for rid in seqs
            if len(seqcells[rid] & piece_cells) >= SALVAGE_MIN_OVERLAP}
    if len(elig) < MIN_RUNS:
        return None
    min_win_cells = max(3, round(CLIMB_MIN_LEN_M / CELL_M))
    best = None
    for i in range(n - 1):
        for j in range(i + min_win_cells, n + 1):
            length = _chain_length_m(piece[i:j], ref_lat)
            if length < CLIMB_MIN_LEN_M:
                continue
            if length > SALVAGE_CLIMB_MAX_M:
                break     # windows only get longer as j grows; past the cap none is a hill
            efforts = _collect_efforts(piece[i:j], length, elig)
            if len(efforts) < MIN_RUNS:
                continue
            seg = _segment_from_efforts(efforts, run_by_id)
            if not seg or seg["type"] != "climb":
                continue
            score = (seg["n_efforts"], seg["length_m"])
            if best is None or score > best[0]:
                best = (score, seg)
    return best[1] if best else None


def _extend_climb_chain(ref, seqs, do_foot=True, do_crest=True):
    """Grow a clipped climb corridor out to the hill's foot and crest, along the reference lap.

    Corridor mining stops where runs diverge (a junction), which can cut a climb short of the
    real hill. Walking the reference run's cell sequence back while the ground keeps dropping
    (toward the foot) and forward while it keeps rising (toward the crest) recovers the whole
    climb. `do_foot`/`do_crest` let the caller grow one end at a time, so an extension that
    would cross a junction and shed attempts can be tried on its own and rejected. Returns an
    extended cell chain, or None when nothing meaningful is added."""
    seq = seqs.get(ref["run_id"])
    if not seq:
        return None
    cells = [c for c, _ in seq]
    pts = [p for _, p in seq]
    sub = ref["sub"]
    try:                       # locate the matched span by object identity (sub is a slice of pts)
        sp = next(i for i, p in enumerate(pts) if p is sub[0])
        ep = next(i for i in range(sp, len(pts)) if pts[i] is sub[-1])
    except StopIteration:
        return None

    # Crest: walk forward to the highest point reachable before the ground turns down and stays
    # down for CLIMB_EXT_REVERSE_M (so a dip at a junction doesn't stop us short of the top).
    new_ep, peak, base = ep, pts[ep].get("elev"), pts[ep]["d"]
    j = ep
    while do_crest and j + 1 < len(pts) and pts[j + 1]["d"] - base <= CLIMB_EXT_MAX_M:
        j += 1
        e = pts[j].get("elev")
        if e is None:
            continue
        if peak is None or e >= peak - CLIMB_EXT_DROP_TOL:
            if peak is None or e > peak:
                peak = e
            new_ep = j
        elif pts[j]["d"] - pts[new_ep]["d"] >= CLIMB_EXT_REVERSE_M:
            break     # sustained descent past the crest

    # Foot: mirror image walking backward to the lowest point before a sustained rise.
    new_sp, low, base = sp, pts[sp].get("elev"), pts[sp]["d"]
    k = sp
    while do_foot and k - 1 >= 0 and base - pts[k - 1]["d"] <= CLIMB_EXT_MAX_M:
        k -= 1
        e = pts[k].get("elev")
        if e is None:
            continue
        if low is None or e <= low + CLIMB_EXT_DROP_TOL:
            if low is None or e < low:
                low = e
            new_sp = k
        elif pts[new_sp]["d"] - pts[k]["d"] >= CLIMB_EXT_REVERSE_M:
            break     # sustained climb past the foot

    if new_sp == sp and new_ep == ep:
        return None
    ext, last = [], None
    for c in cells[new_sp:new_ep + 1]:
        if c != last:
            ext.append(c)
            last = c
    return ext if len(ext) >= 3 else None


def _match_run(chain_cells, seg_len, seq, cover_min=MATCH_COVER, stats=None):
    """Return the fastest completion of a chain within one run's cell sequence, or None.

    seq: list of (cell, point). A completion enters near the chain's first cell,
    exits near its last cell (in order), covers >= cover_min of the chain's
    cells in between, and travels a plausible distance. When `stats` is given, the gate
    that rejected an otherwise-eligible run is tallied (for SEG_DEBUG diagnostics)."""
    cells = [c for c, _ in seq]
    pts = [p for _, p in seq]
    chain_set = set(chain_cells)
    n_chain = len(chain_set)
    start_zone = _neighbours(chain_cells[0])
    end_zone = _neighbours(chain_cells[-1])

    start_idx = [i for i, c in enumerate(cells) if c in start_zone]
    end_idx = [i for i, c in enumerate(cells) if c in end_zone]
    if not start_idx or not end_idx:
        if stats is not None:
            stats["no_zone"] += 1
        return None

    best = None
    passed_dist = passed_cover = False
    for sp in start_idx:
        for ep in end_idx:
            if ep <= sp:
                continue
            dist = pts[ep]["d"] - pts[sp]["d"]
            if dist < seg_len * DIST_LO or dist > seg_len * DIST_HI:
                continue
            passed_dist = True
            covered = len(chain_set & set(cells[sp:ep + 1])) / n_chain
            if covered < cover_min:
                continue
            passed_cover = True
            t = pts[ep]["t"] - pts[sp]["t"]
            if t <= 0:
                continue
            if best is None or t < best["time_s"]:
                sub = pts[sp:ep + 1]
                net, gain = _net_gain(sub)
                best = {"time_s": t, "dist_m": dist, "sub": sub,
                        "net_elev": net, "gain_m": gain}
    if stats is not None and best is None:
        stats["dist" if not passed_dist else "cover" if not passed_cover else "slow"] += 1
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
             f"loops:{LOOP_MIN_CELLS},{LOOP_UNIQUE_FRAC},{LOOP_CLUSTER_M},{LOOP_LEN_RATIO},{LOOP_MIN_SEG_M},{LOOP_DEDUPE_M},refine2,round1,medoid1,laps1,merge1,twophase2,matchturn1,loopfloor1,climbrev1,loopscale1,loopdedupe1,"
             f"shape1:{CLIMB_MIN_LEN_M},{LOOP_MIN_ROUNDNESS},{LOOP_OVERLAP_FRAC},{CHAIN_SELFCROSS_M},"
             f"thruloop1:{LOOP_THRU_COV},{LOOP_THRU_LENRATIO},"
             f"anchors:{load_config().segment_anchors},{ANCHOR_TOL_M},{ANCHOR_COVER},{ANCHOR_CLOSE_M},"
             f"autoanchor2:{AUTO_ANCHOR_MAX_CANDIDATES},{AUTO_ANCHOR_MIN_RUNS},{AUTO_ANCHOR_MINED_GAP_M},"
             f"locdedupe1,close1,anchorline2,elevprofile1,runtype1,nodecimate1,"
             f"subclimb1:{SUBCLIMB_MIN_GRADE},{SUBCLIMB_STEEPER},{SUBCLIMB_MAX_FRAC},"
             f"climbext3noloss:{CLIMB_EXT_MAX_M},{CLIMB_EXT_DROP_TOL},{CLIMB_EXT_REVERSE_M},"
             f"salvageclimb1:{SALVAGE_CLIMB_MAX_M},{SALVAGE_MIN_OVERLAP}".encode())
    return h.hexdigest()


def build_segments(runs):
    """Detect recurring benchmark segments across all runs. Cached on the run set."""
    sig = _runs_signature(runs)
    cached = _load_json(SEG_CACHE)
    if cached and cached.get("signature") == sig:
        print(f"Segments: loaded {len(cached['segments'])} from cache")
        return _apply_stars(cached["segments"])

    _t = time.perf_counter()
    def _lap(_label):
        nonlocal _t
        if PROFILE:
            print(f"[profile]   build_segments/{_label}: {time.perf_counter() - _t:.2f}s")
        _t = time.perf_counter()
    # Build per-run geo-tracks and cell sequences (sharing one grid reference lat).
    tracks = {}
    for r in runs:
        tr = _geo_track(r)
        if len(tr) >= 10:
            tracks[r["id"]] = tr
    if not tracks:
        return []
    _lap("geo_tracks")

    all_lats = [p["lat"] for tr in tracks.values() for p in tr[:1]]
    ref_lat = sorted(all_lats)[len(all_lats) // 2]   # tracks is non-empty here (guarded above)
    cell = _cell_factory(ref_lat)
    anchor_idx = _build_point_grid(tracks, ref_lat)

    seqs = {rid: _cell_sequence(tr, cell) for rid, tr in tracks.items()}
    cell_seqs = [(rid, [c for c, _ in s]) for rid, s in seqs.items()]
    seqcells = {rid: frozenset(c for c, _ in s) for rid, s in seqs.items()}
    _lap("cell_sequences")

    chains = _mine_chains(cell_seqs)
    run_by_id = {r["id"]: r for r in runs}
    _lap("mine_chains")

    segments = []

    # Point-to-point corridors (climbs, repeated stretches). A chain that wraps a loop is
    # split into its straight pieces first, so a line+loop can't form one segment. The floor
    # is the climb floor (a steep climb is a valid short benchmark); _segment_from_efforts
    # re-applies the full MIN_LEN_M floor to anything that doesn't classify as a climb.
    for raw_chain in chains:
        for chain in _split_chain_at_loop(raw_chain):
            seg_len0 = _chain_length_m(chain, ref_lat)
            if seg_len0 < CLIMB_MIN_LEN_M:
                continue
            efforts = _collect_efforts(chain, seg_len0, seqs)
            if len(efforts) < MIN_RUNS:
                # No run completes the whole corridor (it grew past a divergence junction). A
                # real climb up to that junction may still be embedded in it — recover it.
                salvaged = _salvage_climb_from_corridor(chain, seqs, seqcells, run_by_id, ref_lat)
                if salvaged:
                    segments.append(salvaged)
                continue
            ref = min(efforts, key=lambda e: abs(e["dist_m"] - _median(efforts)))
            seg = _segment_from_efforts(efforts, run_by_id)
            if seg:
                segments.append(seg)
                # A climb clipped short of the hill (mining stops at a junction) is grown to the
                # foot and crest, then re-timed across runs. Grow both ends, but never at the cost
                # of attempts: an extension that crosses a divergence junction (where the runner
                # sometimes turns off) sheds efforts, so try each end and keep the longest that
                # holds the attempt count — the climb then settles at the junction the runs share.
                if seg["type"] == "climb":
                    best, best_ref = seg, ref
                    for do_foot, do_crest in ((True, True), (True, False), (False, True)):
                        ext_chain = _extend_climb_chain(ref, seqs, do_foot, do_crest)
                        if not ext_chain:
                            continue
                        ext_efforts = _collect_efforts(ext_chain, _chain_length_m(ext_chain, ref_lat), seqs)
                        if len(ext_efforts) < MIN_RUNS:
                            continue
                        ext_seg = _segment_from_efforts(ext_efforts, run_by_id, force_type="climb")
                        if (ext_seg and ext_seg["length_m"] > best["length_m"]
                                and ext_seg["n_efforts"] >= seg["n_efforts"]):
                            best, best_ref = ext_seg, min(ext_efforts, key=lambda e: abs(e["dist_m"] - _median(ext_efforts)))
                    if best is not seg:
                        segments[-1] = seg = best
                        ref = best_ref
            # A hill is descended more often than climbed, so the ascent stays hidden behind
            # its busier downhill direction. When this corridor is a real descent, time the
            # reverse as a climb in its own right and add it as a separate benchmark. The
            # reverse uses a looser coverage (CLIMB_REV_COVER) because the chain's cells follow
            # the downhill line and ascents take a slightly different one.
            if seg and ref["net_elev"] <= -CLIMB_MIN_GAIN:
                rev = _collect_efforts(chain[::-1], seg_len0, seqs, cover_min=CLIMB_REV_COVER)
                seg_up = _segment_from_efforts(rev, run_by_id)
                if seg_up and seg_up["type"] == "climb":
                    segments.append(seg_up)
            # Pull a short steep pitch (the "burn") out of a longer corridor as its own
            # climb when it is markedly steeper than the whole stretch.
            if seg:
                parent_grade = ref["net_elev"] / ref["dist_m"] if ref["dist_m"] else 0.0
                sub_seg = _steep_subclimb(ref["sub"], parent_grade, seqs, run_by_id, cell)
                if sub_seg:
                    segments.append(sub_seg)

    _lap("corridors")

    # Closed loops (the Coal Loader, Belmont/Shirley loops) — found by detecting
    # where each run returns near an earlier point, then clustering across runs.
    loop_segs, loop_clusters = _detect_loops(seqs, run_by_id)
    segments.extend(loop_segs)
    _lap("detect_loops")

    # Every run's self-crossing loop instances, computed once. _derive_anchor_line filters
    # this set per candidate; recomputing it per candidate (the old behaviour) dominated the
    # whole rebuild because _loop_instances/_loop_roundness are expensive and candidate-blind.
    loop_insts = [c for seq in seqs.values() for c in _loop_instances(seq)]
    _lap("loop_instances")

    # Auto-anchored loops: clusters that fall short of the closed-loop run gate, recovered by
    # matching their derived line against every run (incl. runs where they never self-cross).
    # Skips locations already covered by a mined loop so it fills gaps, never fragments them.
    segments.extend(_auto_anchor_segments(loop_clusters, loop_segs, loop_insts, tracks, anchor_idx, ref_lat, run_by_id))
    _lap("auto_anchor")

    # Config anchors: explicit fallback for loops auto-detection can't recover, or curated names.
    segments.extend(_build_anchored_segments(loop_insts, tracks, anchor_idx, ref_lat, run_by_id))
    _lap("config_anchors")

    segments = _canonicalize_loops(segments)
    segments = _dedupe(segments)
    segments = _drop_loop_fragments(segments)
    segments = _drop_through_loops(segments)
    # Best benchmarks first: most-run, then longest.
    segments.sort(key=lambda s: (-s["n_efforts"], -s["length_m"]))
    segments = segments[:MAX_SEGMENTS]
    _lap("dedupe_sort")

    # Naming (Nominatim) and map-matching (Overpass) hit different hosts, so their per-host
    # politeness sleeps overlap in wall time when run concurrently. Both derive their cache
    # key from the pre-snap drawn line, so snapshot the polylines first: the match thread
    # overwrites s["polyline"] while the name thread reads only the snapshot, so the two are
    # independent (disjoint fields and disjoint cache files).
    orig_polys = {id(s): list(s["polyline"]) for s in segments}
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_name = ex.submit(_name_segments, segments, orig_polys)
        fut_match = ex.submit(_match_segments, segments, orig_polys)
        fut_name.result()
        fut_match.result()
    _disambiguate_names(segments)   # needs final names, so after the name thread joins
    _lap("name+match")
    for i, s in enumerate(segments):
        s["id"] = i

    if SEG_DEBUG:
        print("[seg-debug] final segments (name | type | length | efforts | distinct runs):")
        for s in segments:
            runs = len({e["run_id"] for e in s["efforts"]})
            rnd = f" roundness={s['roundness']:.2f}" if s.get("roundness") is not None else ""
            print(f"[seg-debug]   {s.get('name', '?')[:38]:38s} {s['type']:7s} "
                  f"{s['length_m']:5d}m  efforts={s['n_efforts']:3d}  runs={runs:3d}{rnd}")

    _save_json(SEG_CACHE, {"signature": sig, "segments": segments})
    print(f"Segments: detected {len(segments)} benchmark segments")
    return _apply_stars(segments)


def _apply_stars(segments):
    """Stamp each segment with a stable geo_key and a `starred` flag read from
    STARS_CACHE. The key is geometric (endpoints + midpoint, ~11 m) so stars stay
    attached to the same route across rebuilds even though the positional id shifts.
    Starred state is applied fresh on every build (never baked into segments_cache),
    so re-importing an exported segment_stars.json takes effect immediately."""
    raw = _load_json(STARS_CACHE)
    starred = set(raw) if isinstance(raw, list) else set()
    for s in segments:
        poly = s.get("polyline") or []
        s["geo_key"] = _geo_key(poly) if len(poly) >= 1 else ""
        s["starred"] = s["geo_key"] in starred
    return segments


def _median(efforts):
    d = sorted(e["dist_m"] for e in efforts)
    return d[len(d) // 2]


def _collect_efforts(chain_cells, seg_len_est, seqs, cover_min=MATCH_COVER):
    efforts = []
    stats = {"no_zone": 0, "dist": 0, "cover": 0, "slow": 0} if SEG_DEBUG else None
    for rid, seq in seqs.items():
        m = _match_run(chain_cells, seg_len_est, seq, cover_min, stats=stats)
        if m:
            m["run_id"] = rid
            efforts.append(m)
    if SEG_DEBUG and stats is not None and len(efforts) >= MIN_RUNS:
        print(f"[seg-debug] corridor ~{seg_len_est:.0f}m: matched={len(efforts)} "
              f"rejected zone={stats['no_zone']} dist={stats['dist']} "
              f"cover={stats['cover']} slow={stats['slow']}")
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
    # Type-aware length floor: a steep climb is a valid short benchmark, a flat stretch is not.
    min_len_for_type = (LOOP_MIN_SEG_M if seg_type == "loop"
                        else CLIMB_MIN_LEN_M if seg_type == "climb" else MIN_LEN_M)
    if seg_len < min_len_for_type:
        return None
    # A real loop encloses area. A there-and-back on a parallel path (the two legs land in
    # different cells, so cell-uniqueness lets it through) draws as a thin sliver — reject it.
    seg_roundness = None
    if seg_type == "loop":
        seg_roundness = _loop_roundness(ref["sub"])
        if seg_roundness < LOOP_MIN_ROUNDNESS:
            if SEG_DEBUG:
                print(f"[seg-debug] reject sliver loop @({poly[0][0]:.4f},{poly[0][1]:.4f}) "
                      f"len={seg_len:.0f}m roundness={seg_roundness:.2f} < {LOOP_MIN_ROUNDNESS}")
            return None
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
            "run_type": r.get("run_type", "misc"),
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
        "roundness": round(seg_roundness, 3) if seg_roundness is not None else None,
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
    net, gain = _net_gain(sub_pts)
    return {
        "cells": frozenset(cells[i:j + 1]),
        "time_s": sub_pts[-1]["t"] - sub_pts[0]["t"],
        "dist_m": pts[j]["d"] - pts[i]["d"],
        "sub": sub_pts,
        "net_elev": net,
        "gain_m": gain,
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


def _scan_loops(seqs, min_len):
    """All self-crossing loop candidates across every run, each tagged with run_id + centroid."""
    out = []
    for rid, seq in seqs.items():
        for lp in _run_loops(seq, min_len):
            lp["run_id"] = rid
            lp["cen"] = _loop_centroid(lp["sub"])
            out.append(lp)
    return out


def _detect_loops(seqs, run_by_id):
    """Two passes. Phase 1 identifies distinct loops at a stable scale (a higher floor so
    a small sub-loop of a bigger circuit can't fragment it). Phase 2 re-scans each run
    with a low floor to find every individual lap and assigns each lap to the identifying
    loop it falls in (same place, comparable length) — so repeating a short loop within a
    session counts as several attempts without the short floor breaking identification.

    Returns (segments, clusters): `clusters` are all phase-1 location clusters (before the
    run-count gate), reused by _auto_anchor_segments to recover the sub-gate ones."""
    clusters = _cluster_loops(_scan_loops(seqs, LOOP_MIN_LEN_M))
    ident = [cl for cl in clusters if len({m["run_id"] for m in cl["members"]}) >= MIN_RUNS]

    laps = _scan_loops(seqs, LOOP_LAP_MIN)
    segments = []
    for cl in ident:
        # Phase 1 identifies a loop only from traversals over LOOP_MIN_LEN_M, so for a circuit
        # shorter than that floor cl["med_d"] reflects abnormal multi-lap passes (a 1.5-lap
        # ~600 m run of a 400 m loop), not a single lap. Gating the lap re-scan on that inflated
        # length drops clean single laps (ratio below 1/LOOP_LEN_RATIO) and undercounts the loop.
        # Re-derive the lap length from the laps actually at this location (centroid only), then
        # gate on that, so the identifying length tracks one real lap.
        near = [lp for lp in laps
                if _haversine_m(lp["cen"][0], lp["cen"][1], cl["cen"][0], cl["cen"][1]) <= LOOP_CLUSTER_M]
        lap_med = sorted(lp["dist_m"] for lp in near)[len(near) // 2] if near else cl["med_d"]
        members = [lp for lp in near
                   if (1.0 / LOOP_LEN_RATIO) <= (lp["dist_m"] / lap_med if lap_med else 0) <= LOOP_LEN_RATIO]
        # Each run's laps are already distinct in time (from _run_loops); fall back to the
        # phase-1 members if the lap re-scan somehow found fewer runs.
        if len({m["run_id"] for m in members}) < MIN_RUNS:
            members = cl["members"]
        if SEG_DEBUG:
            _debug_loop_cluster(cl, near, members, lap_med)
        seg = _segment_from_efforts(members, run_by_id, force_type="loop")
        if seg:
            segments.append(seg)
    return segments, clusters


def _debug_loop_cluster(cl, near, members, lap_med):
    """SEG_DEBUG: report how an identified loop cluster's laps resolve into counted attempts."""
    kept = {id(lp) for lp in members}
    excl = [lp for lp in near if id(lp) not in kept]
    runs = sorted({m["run_id"] for m in members})
    per_run = defaultdict(int)
    for m in members:
        per_run[m["run_id"]] += 1
    mlens = sorted(round(m["dist_m"]) for m in members)
    nlens = sorted(round(lp["dist_m"]) for lp in near)
    print(f"[seg-debug] loop @({cl['cen'][0]:.4f},{cl['cen'][1]:.4f}) phase1_med={cl['med_d']:.0f}m "
          f"lap_med={lap_med:.0f}m -> {len(members)} laps over {len(runs)} runs "
          f"(laps/run: {dict(per_run)}) member_lens={mlens} near_lens={nlens}")
    for lp in excl:
        ratio = lp["dist_m"] / lap_med if lap_med else 0
        print(f"[seg-debug]   excluded lap run={lp['run_id']} dist={lp['dist_m']:.0f}m "
              f"ratio={ratio:.2f} (gate {1.0 / LOOP_LEN_RATIO:.2f}-{LOOP_LEN_RATIO:.2f})")


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


def _derive_anchor_line(anchor, loop_insts):
    """The most typical run instance near the anchor's centre and length becomes its line.

    Picking the *roundest* instance backfires: cutting a corner straight across a block
    raises a loop's isoperimetric roundness, so the roundest lap is usually the one that
    strays off the streets and across buildings. Instead drop the degenerate out-and-back
    slivers (a low-roundness floor) and take the medoid of what remains — the lap whose
    shape is most typical of the set, which by definition follows the actual roads. This
    mirrors how mined loops choose their drawn line in _segment_from_efforts."""
    cen, ln = anchor["cen"], anchor["len_m"]
    cands = []
    for c in loop_insts:
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
    net, gain = _net_gain(sub)
    return {"run_id": rid, "time_s": sub[-1]["t"] - sub[0]["t"],
            "dist_m": sub[-1]["d"] - sub[0]["d"], "sub": sub, "net_elev": net, "gain_m": gain}


def _anchored_segment(cen, len_m, loop_insts, tracks, anchor_idx, ref_lat, run_by_id,
                      min_runs, min_span, min_efforts, name=None, curated=False):
    """Build one fixed-line loop segment from a centre + length: derive the line from the
    cleanest self-crossing instance, then time every run's matching span (the entry point
    may be anywhere on the loop). Shared by the auto-anchor and config-anchor paths; the
    gates differ (auto applies near-normal run/span gates, config relaxes them). `curated`
    marks user-declared anchors, which are kept verbatim and never deduped away."""
    inst = _derive_anchor_line({"cen": cen, "len_m": len_m}, loop_insts)
    if not inst:
        return None
    line = _resample_ll(inst["sub"], ANCHOR_LINE_STEP)
    loop_len = inst["dist_m"]
    # Only runs that could cover >= ANCHOR_COVER of the line need the full sliding-window
    # scan; the shared point grid rejects the rest exactly (their coverage upper bound is
    # below the gate), which is most runs since a candidate loop sits in one small area.
    eligible = _anchor_eligible_runs(line, anchor_idx, len(line))
    efforts = []
    for rid in eligible:
        track = tracks[rid]
        for a, b in _match_anchor_spans(line, loop_len, track, ref_lat):
            efforts.append(_span_effort(track, a, b, rid))
    if len(efforts) < min_efforts:
        return None
    seg = _segment_from_efforts(efforts, run_by_id, force_type="loop",
                                min_runs=min_runs, min_span=min_span)
    if not seg:
        return None
    # Draw the clean derived line (not a re-derived medoid).
    seg["polyline"] = [[round(la, 5), round(lo, 5)] for la, lo in _simplify(line)]
    seg["anchored"] = True
    seg["curated"] = curated          # curated (config) anchors bypass dedupe; auto ones don't
    if name:                          # config anchors pin a curated name; auto ones geocode
        seg["name"] = name
    return seg


def _auto_anchor_segments(clusters, mined_loops, loop_insts, tracks, anchor_idx, ref_lat, run_by_id):
    """Recover loops that self-cross in too few runs to be mined as closed loops (e.g. a loop
    usually run inside a longer route). Each sub-gate cluster's centroid + median length seeds
    an anchor; line-matching then counts embedded traversals across all runs. Candidates near
    an already-mined loop are skipped (it would just re-time the same circuit), and the result
    must still clear AUTO_ANCHOR_MIN_RUNS distinct runs over MIN_SPAN_DAYS, so embedding can't
    manufacture spurious or duplicate benchmarks."""
    mined_cens = [_poly_centroid(s["polyline"]) for s in mined_loops]

    def near_mined(cen):
        return any(_haversine_m(cen[0], cen[1], mc[0], mc[1]) < AUTO_ANCHOR_MINED_GAP_M
                   for mc in mined_cens)

    shortfall = [cl for cl in clusters
                 if 1 <= len({m["run_id"] for m in cl["members"]}) < MIN_RUNS]
    shortfall.sort(key=lambda cl: -len({m["run_id"] for m in cl["members"]}))
    segs = []
    for cl in shortfall[:AUTO_ANCHOR_MAX_CANDIDATES]:
        if near_mined(cl["cen"]):
            continue
        seg = _anchored_segment(cl["cen"], cl["med_d"], loop_insts, tracks, anchor_idx, ref_lat, run_by_id,
                                min_runs=AUTO_ANCHOR_MIN_RUNS, min_span=MIN_SPAN_DAYS,
                                min_efforts=AUTO_ANCHOR_MIN_RUNS)
        if seg:
            mined_cens.append(_poly_centroid(seg["polyline"]))   # don't stack two at one spot
            segs.append(seg)
    return segs


def _build_anchored_segments(loop_insts, tracks, anchor_idx, ref_lat, run_by_id):
    """Config-declared fallback anchors: for loops auto-detection can't recover (they never
    self-cross in any run) or to pin a curated name. Gates are relaxed since a pinned loop is
    legitimate even when its only valid laps are one session's."""
    segs = []
    for anchor in load_config().segment_anchors:
        seg = _anchored_segment(anchor.center, anchor.len_m, loop_insts, tracks, anchor_idx, ref_lat, run_by_id,
                                min_runs=1, min_span=0, min_efforts=2,
                                name=anchor.name, curated=True)
        if seg:
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


def _same_directed_corridor(a, b):
    """True when two point-to-point segments trace the same ground the same way: both
    starts align and both ends align within CORRIDOR_END_TOL_M, and their lengths agree
    within ~30%. A reverse traversal fails (one's start aligns with the other's end), so a
    road run both ways stays two segments. Catches the same stretch split into two when
    different runs' GPS land in slightly different start/end cells."""
    pa, pb = a["polyline"], b["polyline"]
    if not pa or not pb:
        return False
    head = _haversine_m(pa[0][0], pa[0][1], pb[0][0], pb[0][1])
    tail = _haversine_m(pa[-1][0], pa[-1][1], pb[-1][0], pb[-1][1])
    if head > CORRIDOR_END_TOL_M or tail > CORRIDOR_END_TOL_M:
        return False
    lo, hi = sorted((a["length_m"], b["length_m"]))
    return hi > 0 and lo / hi > 0.7


def _is_reverse_corridor(a, b):
    """Two point-to-point segments over the same ground in opposite directions (one's start
    aligns with the other's end). A climb and its descent are distinct benchmarks, so dedupe
    must keep both."""
    pa, pb = a["polyline"], b["polyline"]
    if not pa or not pb:
        return False
    return (_haversine_m(pa[0][0], pa[0][1], pb[-1][0], pb[-1][1]) <= CORRIDOR_END_TOL_M
            and _haversine_m(pa[-1][0], pa[-1][1], pb[0][0], pb[0][1]) <= CORRIDOR_END_TOL_M)


def _absorb_efforts(keep, drop):
    """Pool a dropped duplicate loop's efforts into the kept segment for any run it adds, so the
    merged circuit's attempt count, span and PR reflect every lap recorded across the duplicates.
    Runs already in `keep` are left untouched (one run is one attempt, never double-counted)."""
    have = {e["run_id"] for e in keep["efforts"]}
    added = [e for e in drop["efforts"] if e["run_id"] not in have]
    if not added:
        return
    keep["efforts"].extend(added)
    keep["efforts"].sort(key=lambda x: x["date_iso"])
    keep["n_efforts"] = len(keep["efforts"])
    keep["span_days"] = _span_days(keep["efforts"])
    pr = min(keep["efforts"], key=lambda x: x["time_s"])
    keep["pr_time_s"], keep["pr_time_str"], keep["pr_date"] = pr["time_s"], pr["time_str"], pr["date_long"]


def _poly_coverage(target, by, tol=CELL_M):
    """Fraction of `target` polyline points that lie within `tol` of any point on `by`."""
    if not target:
        return 0.0
    return sum(1 for a in target
               if any(_haversine_m(a[0], a[1], b[0], b[1]) <= tol for b in by)) / len(target)


def _canonicalize_loops(segments):
    """Collapse loops at the same place to a single canonical lap. Loops whose centroids are
    within LOOP_DEDUPE_M are the same circuit recorded at different lap scales; keep the one
    closest to a single lap (the shortest that passed the loop gates) and pool attempts from
    co-located loops within ~1.25x of it (true laps). Longer passes (a ~1.5-lap traversal) are
    discarded, so no loop renders longer than one lap."""
    loops = [s for s in segments if s["type"] == "loop"]
    others = [s for s in segments if s["type"] != "loop"]
    clusters = []
    for lp in sorted(loops, key=lambda s: s["length_m"]):
        cen = _poly_centroid(lp["polyline"])
        for cl in clusters:
            if _haversine_m(cen[0], cen[1], cl["cen"][0], cl["cen"][1]) <= LOOP_DEDUPE_M:
                cl["members"].append(lp)
                break
        else:
            clusters.append({"cen": cen, "members": [lp]})
    canon = []
    for cl in clusters:
        members = sorted(cl["members"], key=lambda s: s["length_m"])
        keep = members[0]                       # shortest = closest to one lap
        for other in members[1:]:
            if other["length_m"] <= 1.25 * keep["length_m"]:
                _absorb_efforts(keep, other)
            elif SEG_DEBUG:
                print(f"[seg-debug] drop oversized loop len={other['length_m']}m vs "
                      f"lap {keep['length_m']}m at same place")
        canon.append(keep)
    return others + canon


def _drop_loop_fragments(segments):
    """Drop any non-loop segment that traces most of a loop (>= LOOP_OVERLAP_FRAC of the loop's
    extent) — a 3/4-loop, or the leftover of a line+loop split. A real climb covers only a small
    part of a loop, so it survives: an overlapping segment must be shorter than the loop."""
    loops = [s for s in segments if s["type"] == "loop"]
    out = []
    for s in segments:
        if s["type"] != "loop" and any(
                _poly_coverage(lp["polyline"], s["polyline"]) >= LOOP_OVERLAP_FRAC for lp in loops):
            if SEG_DEBUG:
                print(f"[seg-debug] drop loop-fragment {s['type']} len={s['length_m']}m "
                      f"(covers >= {LOOP_OVERLAP_FRAC:.0%} of a loop)")
            continue
        out.append(s)
    return out


def _drop_through_loops(segments):
    """Drop a 'loop' that a straight point-to-point segment of comparable length traces over the
    same ground. The runner went up-and-back through two near-parallel ways (close enough that the
    out and return legs look like a circuit), not around a block. A genuine loop's through-corridors
    are far shorter than its perimeter, so requiring the covering segment to be >= LOOP_THRU_LENRATIO
    of the loop's length spares real loops while catching the parallel-path false loop."""
    others = [s for s in segments if s["type"] != "loop"]
    out = []
    for s in segments:
        if s["type"] == "loop":
            hit = None
            for p in others:
                lo, hi = sorted((s["length_m"], p["length_m"]))
                if (hi and lo / hi >= LOOP_THRU_LENRATIO
                        and _poly_coverage(s["polyline"], p["polyline"]) >= LOOP_THRU_COV):
                    hit = p
                    break
            if hit is not None:
                if SEG_DEBUG:
                    print(f"[seg-debug] drop through-loop len={s['length_m']}m "
                          f"(straight segment len={hit['length_m']}m of similar length covers it)")
                continue
        out.append(s)
    return out


def _dedupe(segments):
    """Drop segments that substantially overlap another with more attempts.
    Overlap = sharing >70% of effort run-ids and similar length, or tracing the same
    directed corridor (same start and end) regardless of run-id overlap."""
    kept = []
    # Mined segments rank before auto-anchors (tier 0 vs 1) so a real mined loop always wins a
    # tie; auto-anchors only fill gaps. Curated config anchors are handled separately below.
    order = sorted(segments,
                   key=lambda s: (1 if (s.get("anchored") and not s.get("curated")) else 0,
                                  -s["n_efforts"], -s["length_m"]))
    for s in order:
        if s.get("curated"):         # user-declared fixed-line anchors are always kept
            kept.append(s)
            continue
        s_runs = {e["run_id"] for e in s["efforts"]}
        dup = False
        for k in kept:
            # A loop and a point-to-point benchmark over the same ground are different
            # comparisons (one times a circuit, the other a stretch) — keep both.
            if (s["type"] == "loop") != (k["type"] == "loop"):
                continue
            # Co-located loops (the same circuit at different lap scales) are already collapsed
            # by _canonicalize_loops before dedupe, so two loops reaching here are distinct.
            # A climb and its reverse descent run the same ground opposite ways — distinct
            # benchmarks, so never merge them.
            if s["type"] != "loop" and _is_reverse_corridor(s, k):
                continue
            # Same stretch, same direction, split into two near-identical point-to-point
            # segments because runs landed in slightly different start/end cells (so their
            # effort sets only partly overlap and the run-id test below misses them).
            if s["type"] != "loop" and _same_directed_corridor(s, k):
                dup = True
                break
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
                # ... but only when their lengths are comparable. A short climb that sits inside
                # a longer climb on the same road (the John St climb vs the longer one) shares
                # most runs yet is a distinct, much shorter benchmark, so keep both.
                near_total = (inter / smaller >= 0.9 and same_place and s["type"] != "loop"
                              and hi and lo / hi > 0.7)
                # Length-ratio overlap only merges when the two are at the same place. Two loops
                # far apart that merely share runs (people who run loop A often also run loop B)
                # are distinct benchmarks — without this a broad anchor would swallow a real loop.
                if near_total or (same_place and hi and lo / hi > 0.6):
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


def _reverse_geocode(lat, lon, zoom=18, memo=None):
    """Reverse-geocode one point. `memo` (a per-build dict) collapses points that round to the
    same ~11 m spot so overlapping segments don't re-query the same location, and the politeness
    sleep fires only on a real network call, not on a memo hit."""
    mk = (round(lat, GEO_MEMO_DP), round(lon, GEO_MEMO_DP)) if memo is not None else None
    if mk is not None and mk in memo:
        return memo[mk]
    params = urllib.parse.urlencode({
        "format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "zoom": zoom, "addressdetails": 1,
    })
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    time.sleep(1.1)   # honour Nominatim usage policy (<= 1 req/s); only after a real call
    if mk is not None:
        memo[mk] = data
    return data


def _located_cache(cache):
    """(centroid, value) for every cached entry, so a result survives small polyline jitter
    between rebuilds. The cache is keyed on exact endpoints (_geo_key), which miss when the
    drawn line shifts run-to-run; matching by the key's centroid instead keeps the lookup
    stable. Shared by the name cache (value = name) and the match cache (value = snapped
    polyline or [])."""
    out = []
    for k, val in cache.items():
        try:
            pts = [tuple(float(x) for x in p.split(",")) for p in k.split("|")]
            out.append(((sum(p[0] for p in pts) / len(pts),
                         sum(p[1] for p in pts) / len(pts)), val))
        except (ValueError, IndexError):
            continue
    return out


def _located_names(cache):
    """(centroid, name) for every cached name entry (see _located_cache)."""
    return _located_cache(cache)


def _prune_cache(cache, centroids, radius):
    """Drop cache entries whose location is far from every current segment, so keys orphaned
    by polyline drift don't accumulate without bound. Entries near a current segment are kept
    — they are exactly what the tolerant lookup reuses. Unparseable keys are kept untouched."""
    if not centroids:
        return cache
    kept = {}
    for k, v in cache.items():
        try:
            pts = [tuple(float(x) for x in p.split(",")) for p in k.split("|")]
            cen = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        except (ValueError, IndexError):
            kept[k] = v
            continue
        if any(_haversine_m(cen[0], cen[1], c[0], c[1]) <= radius for c in centroids):
            kept[k] = v
    return kept


def _nearest_name(cen, located, max_m):
    best, best_d = None, max_m
    for (la, lo), name in located:
        d = _haversine_m(cen[0], cen[1], la, lo)
        if d <= best_d:
            best, best_d = name, d
    return best


def _name_segments(segments, polys=None):
    """Name each segment from cached OSM lookups, querying Nominatim only when nothing nearby
    is cached. `polys` maps id(segment) -> the pre-snap drawn polyline; when given (the
    concurrent path) the cache key and centroid come from it, so a parallel _match_segments
    overwriting s["polyline"] can't change what this names."""
    cache = _load_json(GEO_CACHE) or {}
    located = _located_names(cache)
    geo_memo = {}                    # in-build dedupe of reverse-geocode points (see _reverse_geocode)
    dirty = False
    for s in segments:
        if s.get("name"):            # anchored segments carry a curated name already
            continue
        poly = polys[id(s)] if polys is not None else s["polyline"]
        cen = _poly_centroid(poly)
        key = _geo_key(poly)
        if key in cache:
            s["name"] = cache[key]
            continue
        # Drift reuse: the drawn line moved past the exact key, but a name we already
        # resolved sits within NAME_REUSE_M — it is the same road, so reuse it (no network).
        near = _nearest_name(cen, located, NAME_REUSE_M)
        if near:
            cache[key] = near
            s["name"] = near
            dirty = True
            continue
        name = _derive_name(poly, s["type"], geo_memo)
        if name:
            cache[key] = name
            s["name"] = name
            located.append((cen, name))
            dirty = True
        else:
            # Network lookup failed (offline / rate-limited): reuse the nearest name we
            # already resolved before showing raw coordinates.
            s["name"] = _nearest_name(cen, located, 120.0) or _fallback_name(poly, s["type"])
    if dirty:
        centroids = [_poly_centroid(polys[id(s)] if polys is not None else s["polyline"])
                     for s in segments]
        _save_json(GEO_CACHE, _prune_cache(cache, centroids, CACHE_PRUNE_M))


def _derive_name(poly, seg_type, memo=None):
    """Query OSM for nearby road / feature names and compose a label. Returns
    None on any network failure (caller falls back to a generic name)."""
    a, mid, b = poly[0], poly[len(poly) // 2], poly[-1]
    roads = []
    features = []
    suburb = None
    try:
        for i, (lat, lon) in enumerate((mid, a, b)):
            data = _reverse_geocode(lat, lon, memo=memo)
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


def _fallback_name(poly, seg_type):
    mid = poly[len(poly) // 2]
    kind = {"loop": "Loop", "climb": "Climb", "segment": "Segment"}[seg_type]
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

def _nearest_match(poly, located):
    """Reuse a cached snap for a polyline that drifted past its exact key. Returns the value
    to reuse (a snapped polyline, or [] for a cached failure) when a prior entry sits within
    MATCH_REUSE_M, else None (caller then queries Overpass). A non-empty snap is only reused
    if it still fits the current drawn line (length band + deviation), so drift can't graft a
    stale line onto a segment that has genuinely moved; a cached failure nearby is reused as-is
    (the same spot will fail to snap again)."""
    cen = _poly_centroid(poly)
    best, best_d = None, MATCH_REUSE_M
    for c, val in located:
        d = _haversine_m(cen[0], cen[1], c[0], c[1])
        if d <= best_d:
            best, best_d = val, d
    if best is None:
        return None
    if not best:                      # cached failure at this spot — reuse, skip Overpass
        return []
    plen = _polyline_len(poly)
    ml = _polyline_len(best)
    if plen and 0.7 * plen <= ml <= 1.4 * plen and _polyline_deviation(best, poly) <= MATCH_MAX_DEV_M:
        return best
    return None


def _match_segments(segments, polys=None):
    """Snap each segment's drawn line onto OSM roads, querying Overpass only when nothing
    nearby is cached. `polys` maps id(segment) -> the pre-snap drawn polyline; when given (the
    concurrent path) the cache key and snap source come from it rather than the live
    s["polyline"] this function overwrites."""
    cache = _load_json(MATCH_CACHE) or {}
    located = _located_cache(cache)
    src = {id(s): (polys[id(s)] if polys is not None else s["polyline"]) for s in segments}
    # Cold-cache win: one Overpass fetch over the union of all segments covers every per-segment
    # bbox (the snap only looks within MATCH_SNAP_M, far inside each segment's own pad), so a
    # single shared graph gives identical snaps while collapsing N area downloads into one. Built
    # lazily on first real need, and skipped entirely when every segment is cached/reused.
    ubbox = _union_bbox(src.values())
    use_shared = _bbox_span_ok(ubbox)
    shared_locate = None      # None = not built; False = fetch/graph failed; else locate(lat,lon)
    dirty = False
    for s in segments:
        poly = src[id(s)]
        key = _geo_key(poly)
        if key in cache:
            if cache[key]:
                s["polyline"] = cache[key]
            continue
        # Drift reuse: the drawn line moved past the exact key, but a snap we already
        # computed sits within MATCH_REUSE_M and still fits — reuse it (no network).
        reused = _nearest_match(poly, located)
        if reused is not None:
            cache[key] = reused
            dirty = True
            if reused:
                s["polyline"] = reused
            continue
        # Needs the road network. Snap against the shared graph, or fall back to a per-segment
        # fetch when the segments span too wide an area for one query.
        matched = None
        if use_shared:
            if shared_locate is None:
                try:
                    coords, adj = _build_walk_graph(_overpass_ways(ubbox))
                    edges = _graph_edges_latlon(coords, adj)
                    shared_locate = _index_edges(edges, ubbox) if len(edges) >= 3 else False
                except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
                    shared_locate = False
                time.sleep(1.0)   # one polite pause for the single shared fetch
            if shared_locate:
                matched = _snap_poly(poly, closed=(s["type"] == "loop"), candidates=shared_locate)
        else:
            matched = _match_polyline(poly, closed=(s["type"] == "loop"))
            time.sleep(1.0)   # per-segment fallback keeps its own pause
        # Only trust a match that keeps roughly the benchmark length, stays close to the
        # actual trace, and is no jaggier than the real run. Otherwise the snap wandered
        # onto the wrong roads or flickered between adjacent parallel ways (a zig-zag the
        # runner never ran), and the smooth raw trace is more faithful.
        if matched:
            ml = _polyline_len(matched)
            dev = _polyline_deviation(matched, poly)
            turn_gain = _reversal_rate(matched) - _reversal_rate(poly)
            if (not (0.7 * s["length_m"] <= ml <= 1.4 * s["length_m"])
                    or dev > MATCH_MAX_DEV_M
                    or turn_gain > MATCH_MAX_TURN_GAIN):
                matched = None
        cache[key] = matched or []
        located.append((_poly_centroid(poly), matched or []))
        dirty = True
        if matched:
            s["polyline"] = matched
    if dirty:
        centroids = [_poly_centroid(src[id(s)]) for s in segments]
        _save_json(MATCH_CACHE, _prune_cache(cache, centroids, CACHE_PRUNE_M))


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


def _union_bbox(polys, pad=0.0015):
    """Padded bounding box covering every point of every polyline. Pad matches the per-segment
    bbox pad so the shared fetch is a superset of what each segment would fetch on its own."""
    lats = [p[0] for poly in polys for p in poly]
    lons = [p[1] for poly in polys for p in poly]
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


def _bbox_span_ok(bbox):
    s, w, n, e = bbox
    return (n - s) <= SNAP_UNION_MAX_DEG and (e - w) <= SNAP_UNION_MAX_DEG


def _graph_edges_latlon(coords, adj):
    """Undirected walk-graph edges as ((lat,lon),(lat,lon)) pairs."""
    return [(coords[a], coords[b]) for a, b in _walk_edges(adj)]


def _index_edges(edges, bbox):
    """Bucket edges into a metric grid so each query point only tests nearby edges instead of the
    whole region. Cells are computed in a local frame about the bbox SW corner; an edge is filed in
    every cell its endpoint bounding box touches (graph edges are short, so 1-4 cells). Returns
    locate(lat,lon) -> the candidate edges in that point's 3x3 cell neighbourhood. With cell >
    MATCH_SNAP_M, any edge passing within snap range of a point is guaranteed to be in that window,
    so the indexed snap selects the same nearest edge a full scan would."""
    s, w, n, _e = bbox
    lat0, lon0 = s, w
    mx = 111320.0 * math.cos(math.radians((s + n) / 2))
    cell = SNAP_INDEX_CELL_M

    def cij(lat, lon):
        return (int(((lon - lon0) * mx) // cell), int(((lat - lat0) * 111320.0) // cell))

    idx = defaultdict(list)
    for ed in edges:
        (alat, alon), (blat, blon) = ed
        ci0, cj0 = cij(min(alat, blat), min(alon, blon))
        ci1, cj1 = cij(max(alat, blat), max(alon, blon))
        for ci in range(ci0, ci1 + 1):
            for cj in range(cj0, cj1 + 1):
                idx[(ci, cj)].append(ed)

    def locate(lat, lon):
        ci, cj = cij(lat, lon)
        out = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.extend(idx.get((ci + di, cj + dj), ()))
        return out

    return locate


def _snap_poly(poly, closed, candidates):
    """Snap a polyline onto the OSM walking network by projecting each point to the nearest path
    edge (not routing between snaps — that detours wildly when the trace runs between parallel
    streets). `candidates(lat, lon)` supplies the edges to test for a point: the whole graph for a
    single-segment fetch, or just the local cells from the shared index. Either way the nearest
    edge within MATCH_SNAP_M is the same, so the drawn line is identical. Returns None on failure
    (no nearby paths, too sparse)."""
    if len(poly) < 3:
        return None
    lats = [a for a, _ in poly]
    lons = [b for _, b in poly]
    lat0 = sum(lats) / len(lats)
    lon0 = sum(lons) / len(lons)
    mx = 111320.0 * math.cos(math.radians(lat0))

    def to_xy(lat, lon):
        return ((lon - lon0) * mx, (lat - lat0) * 111320.0)

    pts = []
    for la, lo in _resample_track(poly, MATCH_STEP_M):
        px, py = to_xy(la, lo)
        best_d2, best_q = None, None
        for (alat, alon), (blat, blon) in candidates(la, lo):
            ax, ay = to_xy(alat, alon)
            bx, by = to_xy(blat, blon)
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


def _match_polyline(poly, closed):
    """Single-segment map-match: fetch this segment's own bbox, then snap. Fallback for when the
    segments span too wide an area for the shared union fetch (see _match_segments)."""
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
    edges = _graph_edges_latlon(coords, adj)
    if len(edges) < 3:
        return None
    return _snap_poly(poly, closed, lambda la, lo: edges)


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
        "<div class=\"seg-filterbar\">"
        "  <div class=\"seg-chips\" id=\"seg-chips\"></div>"
        "  <div class=\"seg-range\" id=\"seg-range\">"
        "    <span class=\"seg-range-lbl\">Window</span>"
        "    <span class=\"chip\" data-range=\"1m\">1M</span>"
        "    <span class=\"chip\" data-range=\"3m\">3M</span>"
        "    <span class=\"chip\" data-range=\"6m\">6M</span>"
        "    <span class=\"chip\" data-range=\"1y\">1Y</span>"
        "    <span class=\"chip active\" data-range=\"max\">Max</span>"
        "  </div>"
        "  <div class=\"seg-filter-actions\">"
        "    <button id=\"seg-star-toggle\" class=\"seg-fbtn\" title=\"Show starred segments only\">"
        "&#9733; Starred only</button>"
        "    <button id=\"seg-export\" class=\"seg-fbtn\" title=\"Download segment_stars.json to drop in cache/ so stars survive a rebuild\">"
        "Export stars</button>"
        "  </div>"
        "</div>"
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
        geo_key = s.get("geo_key", "")
        starred = bool(s.get("starred"))
        star_cls = "seg-star on" if starred else "seg-star"
        cards.append(
            f"<div class=\"seg-card\" data-seg=\"{s['id']}\" data-type=\"{_esc(s['type'])}\" "
            f"data-geo=\"{_esc(geo_key)}\">"
            "  <div class=\"seg-card-head\">"
            f"    <span class=\"seg-badge\" style=\"color:{colour};border-color:{colour}\">{label}</span>"
            f"    <span class=\"seg-title\" onclick=\"openSegOverlay({s['id']})\" "
            f"title=\"Open interactive map\">{_esc(s['name'])}</span>"
            f"    <button class=\"{star_cls}\" onclick=\"toggleSegStar(this)\" "
            f"aria-pressed=\"{'true' if starred else 'false'}\" title=\"Star this segment\">"
            "&#9733;</button>"
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
.seg-filterbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin-top:10px}
.seg-chips{display:flex;flex-wrap:wrap;gap:4px}
.seg-chips .chip{font-size:11px;color:#aaa;background:#222;border:1px solid #3a3a3a;
  border-radius:999px;padding:3px 10px;cursor:pointer;user-select:none;transition:all .1s}
.seg-chips .chip:hover{border-color:#555;color:#ddd}
.seg-chips .chip.active{color:#111;font-weight:700}
.seg-chips .chip.active[data-type="all"]{background:#bbb;border-color:#bbb}
.seg-chips .chip.active[data-type="segment"]{background:#5a9fd4;border-color:#5a9fd4}
.seg-chips .chip.active[data-type="climb"]{background:#e0a020;border-color:#e0a020}
.seg-chips .chip.active[data-type="loop"]{background:#5cb85c;border-color:#5cb85c}
.seg-range{display:flex;align-items:center;flex-wrap:wrap;gap:4px}
.seg-range-lbl{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:.04em;margin-right:2px}
.seg-range .chip{font-size:11px;color:#aaa;background:#222;border:1px solid #3a3a3a;
  border-radius:999px;padding:3px 9px;cursor:pointer;user-select:none;transition:all .1s}
.seg-range .chip:hover{border-color:#555;color:#ddd}
.seg-range .chip.active{background:#5cb85c;border-color:#5cb85c;color:#111;font-weight:700}
.seg-filter-actions{display:flex;gap:6px;margin-left:auto}
.seg-fbtn{font-size:11px;color:#aaa;background:#222;border:1px solid #3a3a3a;border-radius:6px;
  padding:4px 10px;cursor:pointer;transition:all .1s}
.seg-fbtn:hover{border-color:#555;color:#ddd}
#seg-star-toggle.active{color:#f5c518;border-color:#f5c518;background:rgba(245,197,24,.08)}
.seg-card.seg-hidden{display:none}
.seg-star{margin-left:auto;flex:0 0 auto;background:none;border:none;cursor:pointer;
  font-size:18px;line-height:1;color:#4a4a4a;padding:0 2px;transition:color .1s,transform .1s}
.seg-star:hover{color:#f5c518;transform:scale(1.12)}
.seg-star.on{color:#f5c518}
.seg-empty{max-width:480px;margin:4rem auto;text-align:center}
.seg-empty-title{font-size:16px;font-weight:600;color:#bbb;margin:0 0 8px}
.seg-empty-sub{font-size:13px;color:#666;line-height:1.6;margin:0}
.seg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.seg-card{background:#222;border:1px solid #3a3a3a;border-radius:10px;padding:14px;display:flex;flex-direction:column}
.seg-card-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.seg-badge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  padding:2px 8px;border-radius:999px;border:1px solid currentColor;background:rgba(255,255,255,.04)}
.seg-title{font-size:15px;font-weight:700;color:#eee;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;cursor:pointer;flex:1;min-width:0}
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
.so-trend{flex:0 0 auto;background:#1c1c1c;border:1px solid #333;border-radius:8px;position:relative}
.so-trend svg{display:block;width:100%;height:190px}
.seg-trend-scroll{overflow-x:auto;overflow-y:hidden;width:100%}
.seg-trend-scroll svg{display:block}
.seg-trend-yaxis{position:absolute;left:0;top:0;pointer-events:none}
.seg-trend-legend{display:flex;flex-wrap:wrap;gap:4px 12px;padding:6px 10px 8px;border-top:1px solid #2a2a2a}
.seg-trend-legend .seg-leg-item{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;color:#999}
.seg-trend-legend .seg-leg-item i{width:9px;height:9px;border-radius:50%;display:inline-block}
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
