#!/usr/bin/env python3
"""
dedupe.py
Collapses one physical run recorded by two apps into a single activity.

Runs are logged with Nike Run Club and Mi Fitness at the same time (Mi does not
reliably reach Strava, so NRC is the backstop). When both sync, one run arrives
in the bulk export as two activities and every downstream total counts it twice:
distance, training load, CTL/ATL/TSB, best efforts, segment efforts.

The test is simultaneity, not similarity. Two recordings of one run cover almost
exactly the same wall-clock window, while two genuinely different runs done back
to back cannot overlap at all by construction. That is what keeps a warm-up plus
race, or two laps of the same course started minutes apart, safely separate.

Matching uses the raw UTC "Activity Date", never a localized one: localtime.parse_date
resolves a run's timezone from its first GPS coordinate and falls back to UTC when
there is none, so a GPS-less recording and its GPS-carrying twin would appear a
whole timezone apart and never match.

Grouping is union-find rather than pairwise, so a run captured by three apps
collapses to one survivor instead of leaving two.

Overrides from cache/config.json (see config.DuplicateOverrides) are applied in
this order, later steps outranking earlier ones:
  1. not_duplicates  - pairs that must never be reported as duplicates
  2. force_duplicate - loser -> winner pairs the heuristic missed
  3. force_primary   - pins which member of a group survives
"""

import json
import math
from datetime import datetime, timezone

try:  # the hub puts lib/ on sys.path, so the flat import wins there
    from config import DuplicateOverrides, load_config
    from localtime import _first_coord
except ImportError:  # imported as a package, e.g. "import lib.dedupe" from the repo root
    from lib.config import DuplicateOverrides, load_config
    from lib.localtime import _first_coord

# Matching thresholds. All five conditions must hold for two rows to be duplicates.
DUP_START_MAX_S = 300.0      # max gap between the two recorded start times
DUP_OVERLAP_FRAC = 0.60      # min elapsed-window overlap as a fraction of the shorter window
DUP_DIST_TOL = 0.10          # max relative distance difference
DUP_TIME_TOL = 0.15          # max relative moving-time difference
DUP_START_M = 250.0          # max distance between the two first GPS fixes
DUP_CENTROID_M = 500.0       # max distance between the two route centroids

DUP_MIN_POLY_PTS = 5         # below this a polyline is not a usable route

_DATE_FMT = "%b %d, %Y, %I:%M:%S %p"
_EARTH_R_M = 6371008.8


def _num(value, default: float = 0.0) -> float:
    """Blank and malformed CSV cells are the norm here, so never let one raise."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _start_epoch(row: dict) -> float | None:
    try:
        dt = datetime.strptime(row.get("Activity Date", "") or "", _DATE_FMT)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _hr_rank(row: dict) -> int:
    """2 = per-point HR stream, 1 = scalar average only, 0 = none.

    Probes the stream cell with a substring test rather than json.loads: these
    cells run to tens of kilobytes and the answer is a yes/no.
    """
    if '"hr":' in (row.get("fit_distance_stream") or ""):
        return 2
    if _num(row.get("fit_avg_heart_rate")) > 0 or _num(row.get("Average Heart Rate")) > 0:
        return 1
    return 0


def _parse_polyline(raw: str) -> list[tuple[float, float]]:
    try:
        pts = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                out.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    return out


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(h)))


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


class _Run:
    """One CSV row reduced to the fields duplicate matching needs.

    The polyline is parsed lazily: only pairs that survive the cheap time and
    distance tests, plus the members of an actual duplicate group, ever need
    real coordinates.
    """

    __slots__ = ("row", "aid", "start", "elapsed", "moving", "distance",
                 "records", "hr_rank", "poly_raw", "stream_raw", "_coords")

    def __init__(self, row: dict, aid: str, start: float, elapsed: float,
                 moving: float, distance: float) -> None:
        self.row = row
        self.aid = aid
        self.start = start
        self.elapsed = elapsed
        self.moving = moving
        self.distance = distance
        self.records = _num(row.get("fit_record_count"))
        self.hr_rank = _hr_rank(row)
        self.poly_raw = row.get("fit_gps_polyline") or ""
        self.stream_raw = row.get("fit_distance_stream") or ""
        self._coords = None

    @property
    def coords(self) -> list[tuple[float, float]]:
        if self._coords is None:
            self._coords = _parse_polyline(self.poly_raw)
        return self._coords

    @property
    def has_gps(self) -> bool:
        return len(self.coords) >= DUP_MIN_POLY_PTS


def _build_run(row: dict) -> _Run | None:
    """None for any row that cannot be matched on, rather than raising."""
    aid = str(row.get("Activity ID", "") or "").strip()
    if not aid:
        return None
    start = _start_epoch(row)
    if start is None:
        return None
    distance = _num(row.get("Distance"))
    if distance <= 0:
        return None
    moving = _num(row.get("Moving Time"))
    elapsed = _num(row.get("Elapsed Time")) or moving
    return _Run(row, aid, start, elapsed, moving, distance)


def _overlap_frac(a: _Run, b: _Run) -> float:
    shorter = min(a.elapsed, b.elapsed)
    if shorter <= 0:
        return 0.0
    overlap = min(a.start + a.elapsed, b.start + b.elapsed) - max(a.start, b.start)
    return max(0.0, overlap) / shorter


def _rel_gap(x: float, y: float) -> float:
    """Difference relative to the smaller value; infinite when that is not positive,
    so a missing figure can never look like agreement."""
    lo = min(x, y)
    if lo <= 0:
        return float("inf")
    return abs(x - y) / lo


def _geo_matches(a: _Run, b: _Run) -> bool:
    """Condition 5, satisfied by default when either side lacks a usable polyline.

    A wrist band with no GPS still records the same physical run, and conditions
    1-4 already carry that case, so absence of route data must not be read as
    disagreement.
    """
    if not (a.has_gps and b.has_gps):
        return True
    start_a, start_b = _first_coord(a.row), _first_coord(b.row)
    if not start_a or not start_b:
        return True
    if _haversine_m(start_a, start_b) > DUP_START_M:
        return False
    return _haversine_m(_centroid(a.coords), _centroid(b.coords)) <= DUP_CENTROID_M


def _failed_conditions(a: _Run, b: _Run) -> list[str]:
    """Names of the matching conditions this pair fails; empty means duplicate.

    The geometry test is only reached when nothing cheaper failed, so its polyline
    parse is never paid for a pair that was already rejected.
    """
    failed = []
    if abs(a.start - b.start) > DUP_START_MAX_S:
        failed.append("start_gap")
    if _overlap_frac(a, b) < DUP_OVERLAP_FRAC:
        failed.append("window_overlap")
    if _rel_gap(a.distance, b.distance) > DUP_DIST_TOL:
        failed.append("distance")
    if _rel_gap(a.moving, b.moving) > DUP_TIME_TOL:
        failed.append("moving_time")
    if not failed and not _geo_matches(a, b):
        failed.append("geometry")
    return failed


def _matching_pairs(runs: list[_Run]) -> list[tuple[str, str]]:
    """Compare each run only against later ones still inside the start window,
    which keeps the sweep linear as the export grows."""
    pairs = []
    for i, a in enumerate(runs):
        for b in runs[i + 1:]:
            if b.start - a.start > DUP_START_MAX_S:
                break
            if not _failed_conditions(a, b):
                pairs.append((a.aid, b.aid))
    return pairs


class _UnionFind:
    """Grouping, not pairing: three recordings of one run must leave one survivor."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in sorted(self._parent):
            out.setdefault(self.find(item), []).append(item)
        return out


def _winner_key(run: _Run) -> tuple:
    """Lexicographic winner-selection key; the smallest key wins.

    Rule 1 is the one that looks backwards and is not: a record without GPS can
    never win, however rich its heart-rate data. localtime.parse_date localizes a
    run's date from its first coordinate, so a GPS-less winner shifts a UTC+10
    morning run back a calendar day and takes its map, its heatmap contribution
    and all its segment efforts with it. Route data is load-bearing, HR is
    descriptive.

    Stream and polyline sizes stand in for point counts: they are monotone in the
    thing being ranked and cost nothing, where parsing tens of kilobytes per row
    would.

    The Activity ID tie-break is mandatory, not decoration. The live NRC/Mi pair
    ties on every earlier rule, so without it the survivor would depend on
    iteration order and rebuilds would stop being byte-identical.
    """
    return (
        0 if run.has_gps else 1,
        -run.hr_rank,
        -len(run.stream_raw),
        -run.records,
        -len(run.poly_raw),
        run.aid,
    )


def _select_winner(members: list[str], by_id: dict[str, _Run]) -> str:
    return min(members, key=lambda aid: _winner_key(by_id[aid]))


def _forced_pairs(overrides: DuplicateOverrides,
                  by_id: dict[str, _Run]) -> list[tuple[str, str]]:
    """force_duplicate entries that name two distinct activities actually present."""
    return [(loser, winner) for loser, winner in overrides.force_duplicate
            if loser != winner and loser in by_id and winner in by_id]


def find_duplicates(rows: list[dict],
                    overrides: DuplicateOverrides | None = None) -> dict[str, str]:
    """Map each duplicate loser's Activity ID to its group's winner Activity ID.

    Winners are absent from the mapping, so a caller can drop every key and keep
    exactly one activity per physical run. Rows with an unusable date, ID or
    distance are skipped silently.
    """
    if overrides is None:
        overrides = load_config().duplicates

    runs = sorted((r for r in (_build_run(row) for row in rows) if r),
                  key=lambda r: r.start)
    by_id = {r.aid: r for r in runs}

    blocked = {frozenset(p) for p in overrides.not_duplicates if len(set(p)) == 2}
    forced = _forced_pairs(overrides, by_id)
    blocked -= {frozenset(p) for p in forced}  # step 2 outranks step 1

    uf = _UnionFind()
    for run in runs:
        uf.add(run.aid)
    for a, b in _matching_pairs(runs):
        if frozenset((a, b)) not in blocked:
            uf.union(a, b)
    for loser, winner in forced:
        uf.union(loser, winner)

    # Roots are only stable once every union is done, so pinning happens last.
    pinned: dict[str, str] = {}
    for _, winner in forced:
        pinned.setdefault(uf.find(winner), winner)
    for aid in overrides.force_primary:
        if aid in by_id:
            pinned[uf.find(aid)] = aid

    mapping: dict[str, str] = {}
    for root, members in uf.groups().items():
        if len(members) < 2:
            continue
        winner = pinned.get(root) or _select_winner(members, by_id)
        for aid in members:
            # A blocked pair can still share a group through a third recording;
            # it just must never be reported as a duplicate of its partner.
            if aid != winner and frozenset((aid, winner)) not in blocked:
                mapping[aid] = winner
    return mapping
