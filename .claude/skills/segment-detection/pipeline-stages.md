# Pipeline stages: build_segments() walkthrough

> Values are snapshots (as of 2026-07), re-verify with the grep recipes.

This file walks `build_segments(runs)` in `lib/generate_segments.py` in true execution
order, as read from the source. Each stage lists its purpose, key functions, inputs and
outputs, and every gate or filter with its constant. Jargon (cell, corridor, effort,
medoid, anchor line, roundness) follows the glossary in `SKILL.md`.

Overview of the order inside `build_segments` (verify with
`rg -n "def build_segments" -A 160 lib/generate_segments.py`):

1. Cache check via `_runs_signature`
2. Geo-tracks, densify, cell sequences
3. Corridor mining and loop splitting
4. Effort collection and gates (per corridor)
5. Climb handling: classify, extend, reverse, sub-climb, salvage
6. Closed-loop detection
7. Auto-anchored loops
8. Config anchors
9. Canonicalize, dedupe, fragment/through-loop drops, sort, cap
10. Naming, road snapping, cache write, stars

---

## Stage 1: cache check (`_runs_signature`)

Purpose: skip the whole rebuild when neither the run set nor the detection parameters
changed.

`_runs_signature(runs)` computes a SHA1 over, per run (sorted by `date_iso`):
`date_iso|dist_km|len(gps_polyline)` plus the polyline's first and last points when
present, followed by one long "params token" f-string that interpolates most detection
constants and a set of hand-bumped version tokens (`refine2`, `salvageclimb1`, ...).
See `tunables.md`, section "The params token", for the full current token.

`build_segments` loads `cache/segments_cache.json` (path constant `SEG_CACHE`; verify
`rg -n "SEG_CACHE" lib/generate_segments.py`). If the stored `signature` equals the
fresh one it prints `Segments: loaded N from cache` and returns
`_apply_stars(cached["segments"])`, nothing downstream runs.

Critical property: the signature does NOT hash `dist_stream` contents or any code.
Editing detection logic without bumping a version token silently serves stale cached
results (playbook item 1 in `debugging-playbook.md`).

Inputs: the `runs` list from `generate_hub.py`. Output on hit: the cached segment list
with fresh `geo_key`/`starred` stamps. On miss: fall through to stage 2.

## Stage 2: geo-tracks, densify, cell sequences

Purpose: turn each run's raw distance stream into a spatial representation detection
can compare across runs.

- `_geo_track(run)` reads `run["dist_stream"]` (points `{d, t, pace, elev, lat, lon}`,
  `d` in km, elevation key `elev`) into ordered dicts `{lat, lon, d, t, elev}` with `d`
  in cumulative metres. Cumulative seconds come from the stream's `t` field; when `t`
  is missing (older cached rows) it integrates pace over distance as a fallback.
- `_densify(pts)` interpolates so consecutive points are at most `DENSIFY_M` (15.0 m,
  as of 2026-07; `rg -n "^DENSIFY_M" lib/generate_segments.py`) apart.
- Tracks shorter than 10 points are discarded (hard-coded literal inside
  `build_segments`; `rg -n "len\(tr\) >= 10" lib/generate_segments.py`). If no track
  survives, `build_segments` returns `[]`.
- `ref_lat` is the median of each track's first-point latitude; `_cell_factory(ref_lat)`
  returns a function quantising (lat, lon) onto a square grid of roughly `CELL_M`
  (25.0 m, as of 2026-07; `rg -n "^CELL_M" lib/generate_segments.py`) cells.
- `_build_point_grid(tracks, ref_lat)` builds `anchor_idx`, a shared cell-to-run-ids
  index at `ANCHOR_TOL_M` resolution (25.0 m, as of 2026-07;
  `rg -n "^ANCHOR_TOL_M" lib/generate_segments.py`), used later by the anchor stages.
- `_cell_sequence(track, cell)` reduces each track to its de-duplicated ordered list of
  `(cell, point)` pairs, one entry each time the track enters a new cell.

Outputs: `tracks` (run id → geo-track), `seqs` (run id → cell sequence), `cell_seqs`
(list of `(run_id, [cell, ...])`), `seqcells` (run id → frozenset of cells).

## Stage 3: corridor mining and loop splitting

Purpose: find ordered cell paths ("corridors" or "chains") that many runs share.

`_mine_chains(cell_seqs)`:
- Tallies every directed cell transition `(a, b)` with the set of runs that take it.
- Only transitions taken by at least `MIN_RUNS` runs (2, as of 2026-07;
  `rg -n "^MIN_RUNS" lib/generate_segments.py`) enter the successor/predecessor graph.
- Seeds on the busiest transitions and greedily grows in both directions, always
  following the continuation the seed run group keeps taking (score = overlap between
  the seed runs and the runs on the candidate edge, which must stay >= `MIN_RUNS`).
  Growth deliberately crosses junctions, which is also why corridors can overshoot a
  divergence (see salvage, stage 5).
- Growth stops at `MAX_PATH_CELLS` cells (2400, ~60 km, as of 2026-07;
  `rg -n "MAX_PATH_CELLS" lib/generate_segments.py`), at a closed loop, or at a
  figure-eight self-intersection.

`_split_chain_at_loop(chain)` then splits any chain that wraps a loop: a later cell
centre within `CHAIN_SELFCROSS_M` (60.0 m, as of 2026-07;
`rg -n "^CHAIN_SELFCROSS_M" lib/generate_segments.py`) of a much-earlier one (index gap
at least `LOOP_MIN_CELLS`, 8 as of 2026-07;
`rg -n "^LOOP_MIN_CELLS" lib/generate_segments.py`) marks where the chain starts
wrapping. The straight pieces are kept (recursively re-split); the loop span itself is
dropped here because loops are detected separately in stage 6.

Output: candidate corridors (ordered cell lists).

## Stage 4: effort collection and gates

Purpose: time every run's traversal of each corridor and keep only corridors that make
a credible benchmark.

Per split corridor piece, in `build_segments`:
- Pieces shorter than `CLIMB_MIN_LEN_M` (150.0 m, as of 2026-07;
  `rg -n "^CLIMB_MIN_LEN_M" lib/generate_segments.py`) are skipped outright (the
  corridor floor is the climb floor; the full `MIN_LEN_M` floor is re-applied later by
  type).
- `_collect_efforts(chain, seg_len0, seqs)` calls `_match_run` for every run.

`_match_run(chain_cells, seg_len, seq, cover_min=MATCH_COVER)` returns the single
fastest completion within one run, or None. A completion must:
- enter within the 3x3 cell neighbourhood of the corridor's first cell and exit within
  the neighbourhood of its last cell, in order;
- travel a plausible distance: between `seg_len * DIST_LO` and `seg_len * DIST_HI`
  (`DIST_LO, DIST_HI = 0.70, 1.40`, one tuple-assignment line, as of 2026-07;
  `rg -n "DIST_LO" lib/generate_segments.py`);
- cover at least `cover_min` of the corridor's cells (`MATCH_COVER = 0.80`, as of
  2026-07; `rg -n "^MATCH_COVER" lib/generate_segments.py`);
- have positive elapsed time. The fastest qualifying window wins. With
  `STRAVA_SEG_DEBUG=1`, rejects are tallied per gate (`no_zone`/`dist`/`cover`/`slow`).

If fewer than `MIN_RUNS` efforts are found, the corridor is handed to
`_salvage_climb_from_corridor` (stage 5) and otherwise dropped. Corridors that pass go
to `_segment_from_efforts(efforts, run_by_id)`, which applies the remaining gates:
- distinct runs >= `MIN_RUNS` (2, as of 2026-07). Lap repeats within one session count
  as separate efforts but not separate runs, so a lap-heavy day cannot qualify a
  segment alone.
- segment length = median effort distance; classification via `_classify`: "loop" when
  the drawn endpoints are within `LOOP_CLOSE_M` (60.0 m, as of 2026-07;
  `rg -n "^LOOP_CLOSE_M" lib/generate_segments.py`) and length >= `LOOP_MIN_LEN_M`
  (600.0 m, as of 2026-07); "climb" when net ascent >= `CLIMB_MIN_GAIN` (15.0 m, as of
  2026-07; `rg -n "^CLIMB_MIN_GAIN" lib/generate_segments.py`) and average grade >=
  `CLIMB_MIN_GRADE` (0.025, as of 2026-07); otherwise "segment".
- type-aware length floor: `LOOP_MIN_SEG_M` (350.0 m) for loops, `CLIMB_MIN_LEN_M`
  (150.0 m) for climbs, `MIN_LEN_M` (400.0 m, as of 2026-07;
  `rg -n "^MIN_LEN_M" lib/generate_segments.py`) for plain segments.
- loops must have isoperimetric roundness >= `LOOP_MIN_ROUNDNESS` (0.20, as of
  2026-07; `rg -n "^LOOP_MIN_ROUNDNESS" lib/generate_segments.py`), else they are
  rejected as there-and-back slivers.
- the efforts must span at least `MIN_SPAN_DAYS` (14, as of 2026-07;
  `rg -n "^MIN_SPAN_DAYS" lib/generate_segments.py`) between first and last date.

Output per surviving corridor: a segment dict (type, length, grade, polyline drawn from
the reference effort, the one nearest the median distance, plus effort rows, PR, and
span). The elevation numbers come from `_net_gain`, which uses the first and last
points that actually carry elevation, so a single missing sample does not zero it.

## Stage 5: climb handling

All of this happens inside the corridor loop of `build_segments`, immediately after a
corridor segment is accepted.

Classification (already applied above): net gain >= `CLIMB_MIN_GAIN` and grade >=
`CLIMB_MIN_GRADE`. A climb whose input stream has no usable elevation classifies as a
plain "segment" with grade 0, that is an upstream stream bug, not a detection bug
(route to `.claude/skills/compile-and-streams/SKILL.md`).

Extension (`_extend_climb_chain`): corridor mining stops at junctions, which can clip a
climb short of the hill's real foot or crest. For each accepted climb the reference
run's cell sequence is walked backward while the ground keeps dropping (toward the
foot) and forward while it keeps rising (toward the crest), each end limited to
`CLIMB_EXT_MAX_M` (400.0 m, as of 2026-07;
`rg -n "CLIMB_EXT_MAX_M" lib/generate_segments.py`). GPS noise up to
`CLIMB_EXT_DROP_TOL` (1.5 m) is tolerated, and a reversal only stops the walk when
sustained over `CLIMB_EXT_REVERSE_M` (35.0 m). Three variants are tried, both ends,
foot only, crest only, and each is re-timed with `_collect_efforts`; the longest
extension that keeps at least the original effort count replaces the segment. An
extension that crosses a divergence junction sheds efforts and is rejected, so the
climb settles at the junction the runs share.

Descent reversal (`CLIMB_REV_COVER`): when the corridor's reference effort has
`net_elev <= -CLIMB_MIN_GAIN` (it is really a descent), the reversed cell chain is
re-matched with the looser coverage `CLIMB_REV_COVER` (0.75, as of 2026-07;
`rg -n "^CLIMB_REV_COVER" lib/generate_segments.py`) because ascents take a slightly
different line than the downhill cells. If the reverse classifies as a climb it is
added as a separate benchmark; a climb and its descent are deliberately kept distinct.

Sub-climb extraction (`_steep_subclimb`): the steepest contiguous window (at least
`CLIMB_MIN_LEN_M`) of the corridor's reference lap is surfaced as its own climb only
when its grade >= `SUBCLIMB_MIN_GRADE` (0.06, as of 2026-07;
`rg -n "^SUBCLIMB_MIN_GRADE" lib/generate_segments.py`), it is at least
`SUBCLIMB_STEEPER` (1.6) times the parent's grade, and it is no longer than
`SUBCLIMB_MAX_FRAC` (0.75) of the parent. The window is re-matched across all runs
like any corridor, so its attempts and PR are real efforts on that pitch.

Salvage (`_salvage_climb_from_corridor`): a corridor grown past a divergence junction
(runs split two ways at the top) is longer than any single run's traversal, so no run
covers `MATCH_COVER` of it, it collects fewer than `MIN_RUNS` efforts, and it would be
dropped, burying the shared climb up to the junction inside it. Salvage scans such a
failed corridor for the sub-window that the most runs complete and that still
classifies as a climb. Prefilter: a run must share at least `SALVAGE_MIN_OVERLAP`
(6, as of 2026-07; `rg -n "SALVAGE_MIN_OVERLAP" lib/generate_segments.py`) of the
corridor's cells to be match-tested. Windows are bounded by `CLIMB_MIN_LEN_M` below
and `SALVAGE_CLIMB_MAX_M` (650.0 m, as of 2026-07;
`rg -n "SALVAGE_CLIMB_MAX_M" lib/generate_segments.py`) above; the winner maximises
`(n_efforts, length_m)`. Every salvaged window still clears the full climb gates via
`_segment_from_efforts`, so salvage can only add real benchmarks, never relax the bar.
Salvaged climbs then compete in `_dedupe` by effort count and INTENTIONALLY reshape
neighbouring climbs (a higher-support salvaged window can displace or absorb an
adjacent mined climb). This is accepted behaviour, do not "fix" it.

## Stage 6: closed-loop detection

Purpose: find circuits (self-closing routes), which corridor mining cannot represent.

`_detect_loops(seqs, run_by_id)` is two-phase:
- Phase 1 (identify): `_run_loops(seq, LOOP_MIN_LEN_M)` finds, per run, minimal closed
  sub-loops of at least `LOOP_MIN_LEN_M` (600.0 m, as of 2026-07;
  `rg -n "^LOOP_MIN_LEN_M" lib/generate_segments.py`), the high floor stops sub-loops
  of a bigger circuit fragmenting it. A loop closes when the current cell lands in the
  3x3 neighbourhood of a cell visited at least `LOOP_MIN_CELLS` (8) entries earlier,
  its distance is between the floor and a hard-coded 12000 m cap
  (`rg -n "12000" lib/generate_segments.py`), and at least `LOOP_UNIQUE_FRAC` (0.80,
  as of 2026-07; `rg -n "^LOOP_UNIQUE_FRAC" lib/generate_segments.py`) of its cells are
  visited only once (rejecting out-and-back retraces). `_refine_loop_ends` nudges the
  endpoints to the tightest closure, and a candidate is kept only if that closure is
  within `LOOP_JOIN_M` (15.0 m, as of 2026-07-04) — a wider "closure" is two parallel
  streets the 25 m grid can't tell apart (Shirley Rd vs Belmont Ave), not a real return,
  and would truncate the circuit to its tighter half. Genuine candidates are then grouped
  by centroid (`_group_loop_candidates`, one group per physical loop) and `_dominant_laps`
  returns, per group, the disjoint laps in the length band with the most instances (ties
  to the longer band). This surfaces the full circuit a runner repeats rather than the
  tightest closure — the earlier shortest-per-window rule mined only the half-loop.
- Clustering: `_cluster_loops` groups loop instances across runs when centroids are
  within `LOOP_CLUSTER_M` (120.0 m, as of 2026-07;
  `rg -n "^LOOP_CLUSTER_M" lib/generate_segments.py`) and lengths agree within
  `LOOP_LEN_RATIO` (1.35). Clusters with at least `MIN_RUNS` distinct runs identify a
  loop.
- Phase 2 (count laps): every run is re-scanned with the lower floor `LOOP_LAP_MIN`
  (300.0 m, as of 2026-07; `rg -n "^LOOP_LAP_MIN" lib/generate_segments.py`) so
  individual laps of a short circuit are all counted. The lap length is re-derived
  from the laps actually at that location (median), then laps within `LOOP_LEN_RATIO`
  of it become the segment's efforts.

Each identified cluster goes through `_segment_from_efforts(..., force_type="loop")`,
which draws the medoid effort (the traversal whose shape is closest to all the others,
after dropping low-roundness slivers) so the line follows real streets, and applies the
loop floor (`LOOP_MIN_SEG_M`, 350.0 m) and the roundness gate (`LOOP_MIN_ROUNDNESS`,
0.20).

`_detect_loops` returns both the loop segments and ALL phase-1 clusters (including
those below the run gate); the sub-gate clusters seed stage 7.

## Stage 7: auto-anchored loops

Purpose: recover loops that are real routes but rarely self-cross as closed laps,
typically a loop usually run inside a longer route.

First `build_segments` computes `loop_insts` once: every self-crossing loop instance in
every run at all scales (floor `LOOP_LAP_MIN`, cap 12000 m), via `_loop_instances`.

`_auto_anchor_segments(clusters, mined_loops, loop_insts, ...)`:
- Candidates are phase-1 clusters with at least 1 but fewer than `MIN_RUNS` distinct
  runs, sorted by run count, capped at `AUTO_ANCHOR_MAX_CANDIDATES` (25, as of
  2026-07; `rg -n "AUTO_ANCHOR_MAX_CANDIDATES" lib/generate_segments.py`).
- Candidates whose centroid is within `AUTO_ANCHOR_MINED_GAP_M` (120.0 m, as of
  2026-07; `rg -n "AUTO_ANCHOR_MINED_GAP_M" lib/generate_segments.py`) of an
  already-mined loop are skipped (an anchor there would only re-time the same circuit).
- `_anchored_segment` builds the segment: `_derive_anchor_line` picks the most typical
  (medoid) self-crossing instance near the candidate's centre (within `ANCHOR_NEAR_M`,
  130.0 m, and 0.7–1.4x the candidate length), after dropping low-roundness slivers;
  the roundest lap is deliberately NOT chosen because corner-cutting raises roundness.
  The instance is resampled at `ANCHOR_LINE_STEP` (10.0 m) into the fixed anchor line.
- `_anchor_eligible_runs` uses the stage-2 point grid to keep only runs that could
  cover >= `ANCHOR_COVER` of the line (an exact prefilter, no false negatives).
- `_match_anchor_spans` slides a distance-bounded window over each eligible track: a
  window qualifies when its start and end are within `ANCHOR_CLOSE_M` (55.0 m, as of
  2026-07; `rg -n "^ANCHOR_CLOSE_M" lib/generate_segments.py`) of each other (a real
  lap closes), its distance is 0.80–1.45x the loop length, and its points cover at
  least `ANCHOR_COVER` (0.80, as of 2026-07;
  `rg -n "^ANCHOR_COVER" lib/generate_segments.py`) of the line's resampled points
  within `ANCHOR_TOL_M` (25.0 m). The entry point may be anywhere on the loop
  (Strava-style). Fastest non-overlapping spans are kept, so lap repeats count.
- Gates: at least `AUTO_ANCHOR_MIN_RUNS` (3, as of 2026-07;
  `rg -n "AUTO_ANCHOR_MIN_RUNS" lib/generate_segments.py`) distinct line-matched runs
  over `MIN_SPAN_DAYS`, applied through `_segment_from_efforts(force_type="loop")`.
- The drawn polyline is the clean derived line; the segment is marked
  `anchored: True`, `curated: False`. Each accepted anchor is added to the mined-loop
  centroid list so two anchors cannot stack at one spot.

## Stage 8: config anchors

`_build_anchored_segments` runs the same `_anchored_segment` machinery for every entry
in `load_config().segment_anchors` (from `cache/config.json` via `lib/config.py`), but
with relaxed gates: `min_runs=1, min_span=0, min_efforts=2`, a pinned curated name, and
`curated=True`. Curated anchors are kept verbatim and never deduped away (see stage 9).
Because `segment_anchors` is interpolated into the params token, editing it changes the
signature and triggers a rebuild automatically.

## Stage 9: canonicalize, dedupe, drop, sort, cap

Applied in this exact order:

1. `_canonicalize_loops`: loops whose drawn centroids are within `LOOP_DEDUPE_M`
   (120.0 m, as of 2026-07; `rg -n "^LOOP_DEDUPE_M" lib/generate_segments.py`) are the
   same circuit at different lap scales. The shortest (closest to one lap) is kept;
   co-located loops up to 1.25x its length have their efforts pooled in via
   `_absorb_efforts` (one run is never double-counted); longer multi-lap passes are
   discarded, so no loop renders longer than one lap.
2. `_dedupe`: segments are ranked mined-before-auto-anchored, then by
   `(-n_efforts, -length_m)`. Curated anchors are always kept. A segment is dropped as
   a duplicate of a kept one when they trace the same directed corridor (both endpoints
   align within `CORRIDOR_END_TOL_M`, 80.0 m, as of 2026-07;
   `rg -n "^CORRIDOR_END_TOL_M" lib/generate_segments.py`, lengths within ~30%), or
   when they share > 70% of the smaller one's effort run-ids AND sit at the same place
   (centroids < 250 m, hard-coded) with comparable lengths (ratio > 0.6; a near-total
   0.9 run overlap relaxes this to > 0.7). Deliberately never merged: a loop vs a
   non-loop over the same ground, and a climb vs its reverse descent
   (`_is_reverse_corridor`). A short climb inside a longer climb also survives (the
   length-ratio condition).
3. `_drop_loop_fragments`: a non-loop segment whose footprint covers >=
   `LOOP_OVERLAP_FRAC` (0.60, as of 2026-07;
   `rg -n "^LOOP_OVERLAP_FRAC" lib/generate_segments.py`) of a detected loop's cells is
   a loop fragment (a 3/4-loop or a line+loop leftover) and is dropped.
4. `_drop_through_loops`: a "loop" that a straight segment of comparable length
   (length ratio >= `LOOP_THRU_LENRATIO`, 0.90, as of 2026-07) covers to >=
   `LOOP_THRU_COV` (0.50, as of 2026-07;
   `rg -n "^LOOP_THRU_COV" lib/generate_segments.py`) of its extent is really an
   up-and-back through two near-parallel ways, and is dropped.
5. Sort by `(-n_efforts, -length_m)` and truncate to `MAX_SEGMENTS` (100, as of
   2026-07; `rg -n "^MAX_SEGMENTS" lib/generate_segments.py`). Low-effort segments can
   silently fall off this cap when new segments appear.

## Stage 10: naming, snapping, cache write

Naming (`_name_segments`, Nominatim reverse geocoding) and road snapping
(`_match_segments`, Overpass map-matching) run concurrently in a two-worker
`ThreadPoolExecutor`; both key their caches on a snapshot of the pre-snap polylines so
they cannot race. `_disambiguate_names` then suffixes the length onto any duplicate
names, and positional `id`s are assigned. Details, failure modes, and the
location-tolerant cache reuse live in `.claude/skills/external-apis/SKILL.md`, route
naming/snapping/slow-network symptoms there.

Finally the result is written to `cache/segments_cache.json` as
`{"signature": sig, "segments": [...]}` (before stars are stamped, so the cache file
never contains `geo_key`/`starred`), `Segments: detected N benchmark segments` is
printed, and `_apply_stars(segments)` stamps each segment with a geometric `geo_key`
and a `starred` flag read from `cache/segment_stars.json` (`STARS_CACHE`; the file may
not exist). Stars are applied fresh on every build and on every cache load.

Under `STRAVA_PROFILE=1`, each stage prints a `[profile] build_segments/<stage>: Ns`
timing line; under `STRAVA_SEG_DEBUG=1`, a final per-segment table is printed.
