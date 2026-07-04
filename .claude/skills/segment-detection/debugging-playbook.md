# Debugging playbook: segment-detection symptoms

> Values are snapshots (as of 2026-07), re-verify with the grep recipes.

Numbered procedures for the eight symptoms promised in `SKILL.md`. Each gives the
ordered hypotheses (most likely first), a concrete check, and the fix. Stage names and
constants refer to `pipeline-stages.md` and `tunables.md` in this directory; all code
is in `lib/generate_segments.py`.

Two tools you will use repeatedly:

Debug/profile toggles. In PowerShell:

```powershell
$env:STRAVA_SEG_DEBUG = "1"; python generate_hub.py
$env:STRAVA_PROFILE = "1"; python generate_hub.py
```

In bash: `STRAVA_SEG_DEBUG=1 python generate_hub.py`. Both are read at import time
(`rg -n "STRAVA_SEG_DEBUG|STRAVA_PROFILE" lib/generate_segments.py`). Remember to
delete `cache/segments_cache.json` first, or a signature hit will skip detection and
print no diagnostics at all.

Cache dump snippet. Save as a file in your scratchpad directory (never in the repo)
and run it from the repo root. Key names are the real cache JSON keys: top level is
`{"signature": ..., "segments": [...]}`, and each segment carries `name`, `type`,
`length_m`, `n_efforts`, `efforts` (a list of per-attempt dicts with `date_iso`,
`run_id`, `time_s`, `time_str`, `pace_str`, `ga_pace_str`, `name`, `run_type`,
`date_long`), plus `grade`, `gain_m`, `span_days`, `roundness`, `polyline`,
`pr_time_s`, `pr_time_str`, `pr_date`, `length_str`, `id`. Note `geo_key` and
`starred` are NOT in the file, `_apply_stars` stamps them at load time.

```python
import json
from pathlib import Path

data = json.loads(Path("cache/segments_cache.json").read_text(encoding="utf-8"))
segs = data["segments"]
print(f"signature={data['signature'][:12]}...  {len(segs)} segments")
for s in sorted(segs, key=lambda s: -s["n_efforts"]):
    print(f"{s['name'][:42]:42s} {s['type']:7s} {s['length_m']:5d} m "
          f"efforts={s['n_efforts']:3d} span={s['span_days']:4d}d grade={s['grade']}%")
```

Baseline for sanity checks (as of 2026-07-02, verified against live data): 73 segments
total; `The Colonnade loop (Waverton)`, type `loop`, 392 m, 29 efforts. These counts
GROW as new runs are added; the invariant to test is determinism (same data, same
params → identical output), never a fixed count.

---

## 1 (a). Results didn't change after my code change

Hypotheses, most likely first:
1. Stale cache. `_runs_signature` hashes only `date_iso|dist_km|len(gps_polyline)` +
   polyline endpoints per run, plus the params token. It does NOT hash your code or
   `dist_stream` contents, so a logic edit leaves the signature unchanged and the
   cached result is served (verify the body:
   `rg -n "def _runs_signature" -A 20 lib/generate_segments.py`).
2. You changed a constant that is not interpolated into the token (e.g. `DENSIFY_M`,
   `MIN_SPAN_DAYS`, `DIST_LO/DIST_HI`, `CLIMB_MIN_GAIN`, full list in `tunables.md`,
   "The params token").
3. You are looking at a stale `dashboards/TrainingHub.html` (hub not regenerated).

Check: run `python generate_hub.py` and read the segments line. `Segments: loaded N
from cache` means a signature hit, detection never ran. `Segments: detected N
benchmark segments` means it really rebuilt.

Fix: bump one version token inside the `_runs_signature` f-string (e.g.
`salvageclimb1` → `salvageclimb2`), delete `cache/segments_cache.json`, rerun.
Procedure and token list: `tunables.md`, "The params token".

## 2 (b). A segment disappeared

Hypotheses:
1. It fell off the `MAX_SEGMENTS` cap (100, as of 2026-07;
   `rg -n "^MAX_SEGMENTS" lib/generate_segments.py`). Segments are sorted by
   `(-n_efforts, -length_m)` before truncation, so a low-effort segment silently drops
   when new higher-effort segments appear.
2. It was merged or dropped in stage 9: absorbed by `_canonicalize_loops` (co-located
   loop at a different lap scale), deduped by `_dedupe` (same directed corridor or
   >70% run overlap at the same place), dropped by `_drop_loop_fragments` or
   `_drop_through_loops`.
3. A salvaged climb reshaped it. Salvaged climbs compete in `_dedupe` by effort count
   and INTENTIONALLY reshape or displace neighbouring climbs, accepted behaviour, do
   not "fix" it.
4. The data changed (a run edited/removed upstream) and it now fails a gate, go to
   procedure 7.

Check: delete `cache/segments_cache.json`, rebuild with `STRAVA_SEG_DEBUG=1`, and read
the diagnostics: every stage-9 drop prints a `[seg-debug] drop ...` line naming the
reason (oversized loop, loop-fragment, through-loop), and the final `[seg-debug] final
segments` table shows everything that survived, pre-cap. If the segment appears in the
final table but not in the hub, it was cut by the cap (compare its rank against
`MAX_SEGMENTS`).

Fix: none needed if a merge pooled its efforts into a kept segment (check the kept
segment's attempt list with the dump snippet). If the cap cut it, raise
`MAX_SEGMENTS` (not in the params token, bump a version token) or accept it. If a
dedupe rule mis-fired, tune the relevant constant (`CORRIDOR_END_TOL_M`,
`LOOP_DEDUPE_M`, `LOOP_OVERLAP_FRAC`, `LOOP_THRU_COV`) per `tunables.md`.

## 3 (c). Effort count dropped or jumped

Hypotheses:
1. The data window grew, this is normal. The Colonnade baseline moved from ~24 to 29
   efforts as of 2026-07-02 simply because more runs entered the dataset. Effort
   counts are expected to grow; determinism, not any fixed count, is the invariant.
2. Lap counting changed: efforts include repeated laps within one session (rows named
   "... (lap N)"), so a single lap-heavy run can add several efforts. Loop lap
   membership is gated by `LOOP_LEN_RATIO` around the re-derived lap median in
   `_detect_loops` (`rg -n "^LOOP_LEN_RATIO" lib/generate_segments.py`).
3. Efforts were pooled by a merge: `_absorb_efforts` adds a dropped duplicate's
   efforts (for runs not already present) during `_canonicalize_loops` and never
   double-counts a run.
4. A real regression: a detection change altered matching. Verify determinism,
   delete the cache, rebuild twice with identical data; the two segment lists must be
   identical.

Check: rebuild with `STRAVA_SEG_DEBUG=1`. For loops, the `[seg-debug] loop @(...)`
lines show `phase1_med`, `lap_med`, laps per run, and every excluded lap with its
length ratio against the gate. For corridors, the `[seg-debug] corridor ~Nm` lines
tally rejects by gate (`zone`/`dist`/`cover`/`slow`). Compare against the dump
snippet's per-segment counts from the previous cache (copy the old
`segments_cache.json` aside before deleting it).

Fix: if the excluded laps are legitimate, widen `LOOP_LEN_RATIO`; if a coverage gate
rejects honest traversals, inspect `MATCH_COVER` / `DIST_LO, DIST_HI` per
`tunables.md`. If two rebuilds on the same data differ, that is a genuine bug in your
change, bisect it before touching constants.

## 4 (d). A climb shows 0% grade / classifies as a flat segment

Hypotheses:
1. Upstream stream bug: the run's `dist_stream` points carry no usable `elev`, so
   `_net_gain` returns `(0.0, 0.0)` when fewer than 2 elevation-bearing points exist
   (`rg -n "def _net_gain" lib/generate_segments.py`), `_classify` sees zero net gain,
   and the corridor classifies as a plain "segment" with grade 0. This is not a
   detection bug. Elevation must attach to stream points by cumulative-distance
   interpolation, never exact epoch matching (iron law 4 in the project `CLAUDE.md`).
2. The reference effort happens to be an elevation-poor run while other efforts have
   elevation (grade comes from the reference effort's `net_elev`, the effort nearest
   the median distance).

Check: scratchpad snippet against the latest enriched CSV. Remember the field-size
iron law and that the elevation key is `elev`, not `e`:

```python
import csv, sys, json
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
from pathlib import Path
path = sorted(Path("csv_data").glob("*_strava.csv"))[-1]
with open(path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        stream = json.loads(row["fit_distance_stream"] or "[]")
        if not stream:
            continue
        with_elev = sum(1 for p in stream if p.get("elev") is not None)
        if with_elev < len(stream) * 0.5:
            print(row["fit_start_time"], f"{with_elev}/{len(stream)} points have elev")
```

A healthy dataset shows density 1.0 (verified 2026-07-02 on the 5 most recent runs).

Fix: route to `.claude/skills/compile-and-streams/SKILL.md` and fix elevation
attachment in `strava_compile.py`, then `python strava_compile.py --rebuild` (the
enriched CSV is the parse cache), then bump a token / delete the segments cache and
regenerate the hub.

## 5 (e). Duplicate or overlapping segments

Hypotheses:
1. Intentional pairs, check these before "fixing" anything: a climb and its reverse
   descent are deliberately distinct (`_is_reverse_corridor`); a loop and a
   point-to-point segment over the same ground are deliberately distinct (they time
   different things); a short steep climb inside a longer climb on the same road is
   deliberately kept (the length-ratio guard in `_dedupe`).
2. A genuine miss of `_same_directed_corridor`: the same stretch split into two
   because runs landed in different start/end cells and the endpoints diverge by more
   than `CORRIDOR_END_TOL_M` (80.0 m, as of 2026-07;
   `rg -n "^CORRIDOR_END_TOL_M" lib/generate_segments.py`) or lengths differ beyond
   the ~30% band.
3. Two co-located loops survived because their drawn centroids sit farther apart than
   `LOOP_DEDUPE_M` (120.0 m, as of 2026-07), common when one drawn line snapped to
   roads and the other did not.
4. An auto-anchor duplicated a mined loop because their centroids exceed
   `AUTO_ANCHOR_MINED_GAP_M` (120.0 m, as of 2026-07).

Check: use the dump snippet, find both segments, and compare `type`, `length_m`, and
polyline endpoints. Compute the two centroid distances with a few lines of scratchpad
Python (mean of polyline lats/lons, haversine) and compare against the constant that
should have merged them.

Fix: for case 1, nothing, document, don't merge. Otherwise widen the specific
tolerance (`CORRIDOR_END_TOL_M`, `LOOP_DEDUPE_M`, or `AUTO_ANCHOR_MINED_GAP_M`) per
`tunables.md`, bump a token if the constant is not interpolated, delete the cache,
rebuild, and confirm no over-merge appeared elsewhere in the final SEG_DEBUG table.

## 6 (f). A loop is drawn through a building

Hypotheses:
1. Road snapping failed or was rejected for this segment, so the raw medoid GPS trace
   is drawn. The snap is rejected when it strays (`MATCH_MAX_DEV_M`), zig-zags
   (`MATCH_MAX_TURN_GAIN`), or changes length beyond 0.7–1.4x; a failure is cached as
   an empty list `[]` in `cache/segment_match_cache.json` and reused.
2. The medoid effort itself cuts a corner (GPS drift). The drawn line is a single real
   run chosen by `_medoid_index`, so one bad-GPS lap can only win when the good laps
   were filtered out.
3. For anchored loops, the anchor line derives from one instance
   (`_derive_anchor_line`), which can embed that instance's GPS error.

Check: find the segment's entry in `cache/segment_match_cache.json` (keys are geo_key
strings of three rounded "lat,lon" points joined by `|`; ~90% of values were `[]`, a
cached "no snap kept", as measured 2026-07-02). An empty value means the drawn line is
the raw trace and the fix is on the snapping side.

Fix: route to `.claude/skills/external-apis/SKILL.md`, snapping tolerances
(`MATCH_SNAP_M`, `MATCH_MAX_DEV_M`, `MATCH_MAX_TURN_GAIN`), Overpass failures, and
cache-entry surgery live there. Deleting the one stale key from
`segment_match_cache.json` forces a re-snap of just that segment on the next rebuild.

## 7 (g). An expected segment is missing (gates walkthrough)

Work through the gates in pipeline order; rebuild with `STRAVA_SEG_DEBUG=1` (cache
deleted) and read the diagnostics at each step.

1. Input: does the route appear in enough runs? `MIN_RUNS` distinct runs (2, as of
   2026-07) spanning `MIN_SPAN_DAYS` (14) are required; anchored paths need
   `AUTO_ANCHOR_MIN_RUNS` (3).
2. Corridor mining: every directed cell transition on the route must be shared by
   `MIN_RUNS` runs. Two runs on opposite sides of a wide road can land in different
   25 m cells and never share transitions.
3. Length floors: corridor pieces under `CLIMB_MIN_LEN_M` (150.0 m) are skipped;
   after classification the floor is `MIN_LEN_M` (400.0 m) for flat segments,
   `LOOP_MIN_SEG_M` (350.0 m) for loops.
4. Effort matching: the `[seg-debug] corridor ~Nm` lines show why eligible runs were
   rejected, `zone` (never entered/exited near the endpoints), `dist` (window
   outside `DIST_LO`–`DIST_HI` × length), `cover` (below `MATCH_COVER`), `slow`
   (non-positive time). A corridor with fewer than `MIN_RUNS` matches is dropped
   (salvage may still recover an embedded climb).
5. Loops: the `[seg-debug] loop @(...)` lines show cluster membership and excluded
   laps; the `reject sliver loop` line fires when roundness is below
   `LOOP_MIN_ROUNDNESS` (0.20, as of 2026-07). Also check the phase-1 floor
   `LOOP_MIN_LEN_M` (600.0 m) and `LOOP_UNIQUE_FRAC` (0.80) for out-and-back
   retraces.
6. Stage 9 drops: look for `[seg-debug] drop ...` lines (dedupe, fragments,
   through-loops, oversized laps).
7. The cap: present in the final table but not the hub means `MAX_SEGMENTS` cut it.

Fix: whichever gate the diagnostics name. Prefer understanding why the honest
traversal fails (usually GPS spacing vs `CELL_M`, or a coverage gate) over blanket
loosening; each constant's blast radius is in `tunables.md`. Always bump-and-delete
after the change.

## 8 (h). Rebuild is slow

Hypotheses:
1. Geocode/match cache misses, not compute. Nominatim calls sleep 1.1 s each and
   Overpass 1.0 s (`rg -n "time.sleep" lib/generate_segments.py`); a rebuild that
   suddenly takes minutes is re-naming/re-snapping segments whose drawn lines drifted
   past their cache keys. Historical incidents of 200–500 s rebuilds were exactly
   this. Do NOT "optimise" the sleeps away, they honour the providers' terms.
2. Genuine compute growth (many more runs or a pathological corridor), which is rare:
   the full pipeline was ~4.5 s with 79 runs (as of 2026-07-02).

Check: run with `STRAVA_PROFILE=1` and read the `[profile] build_segments/<stage>`
lines. A slow `name+match` stage is network (hypothesis 1); slow `mine_chains` /
`corridors` / `auto_anchor` stages are compute.

Fix: for network, route to `.claude/skills/external-apis/SKILL.md` (location-tolerant
reuse via `NAME_REUSE_M`/`MATCH_REUSE_M` and the shared union Overpass fetch are the
existing mitigations; verify they are being hit, not bypassed). For compute, profile
which stage regressed and inspect the most recent change to it.
