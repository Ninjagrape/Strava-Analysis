---
name: segment-detection
description: Debugging and extending the Strava-Analysis segment-detection engine (lib/generate_segments.py). Use when segments are wrong, missing, duplicated, or misnamed; when effort counts changed unexpectedly; when a climb is misclassified or shows 0% grade / flat; when a loop is drawn through buildings; when results do not change after a code edit (stale segments_cache.json); or when changing detection logic or tunables (CELL_M, MATCH_COVER, MIN_RUNS, climb and loop thresholds). Covers the full build_segments() pipeline - geo-tracks and 25 m cell sequences, corridor mining, effort-matching gates, climb classification, extension and salvage, loop detection and dedupe, anchored loops, and cache invalidation via the _runs_signature params token.
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- A segment is missing, duplicated, misclassified (loop vs climb vs segment), or has the wrong shape or length.
- Effort ("attempt") counts changed, or a PR looks wrong.
- You edited detection code and the output did not change (almost always the stale-cache trap; see playbook a).
- You are changing any detection constant or algorithm in `lib/generate_segments.py`.

## When NOT to use

Route by symptom instead:
- Segment names wrong, road-snapping wrong, Nominatim/Overpass failures, slow rebuilds from network calls → `.claude/skills/external-apis/SKILL.md`
- Which cache file to delete, and when deletion is safe → `.claude/skills/caches-and-invalidation/SKILL.md`
- Bad input streams: missing or zeroed elevation, sparse GPS, `dist_stream` problems → `.claude/skills/compile-and-streams/SKILL.md`
- Verifying output after a change (baselines, counts) → `.claude/skills/validation-playbook/SKILL.md`

## Mental model

Each run carries a `dist_stream`: a list of points `{d, t, pace, elev, lat, lon}` (`d` in km, elevation key is `elev`, not `e`). `_geo_track` turns it into a "geo-track", ordered points with cumulative metres and seconds, and `_densify` interpolates so consecutive points are at most `DENSIFY_M` (15 m, as of 2026-07) apart. `_cell_factory` quantises positions onto a square grid of roughly `CELL_M` (25 m) cells; `_cell_sequence` reduces a track to its de-duplicated ordered list of cells (the "cell sequence").

Detection is then entirely about finding cell paths that many runs share. A "chain" is an ordered cell list mined from transitions that at least `MIN_RUNS` runs take (`_mine_chains`); after self-crossing splits (`_split_chain_at_loop`) each resulting point-to-point stretch is a "corridor", the candidate for a line segment or climb. An "effort" is one run's timed traversal of a corridor, found by window matching over that run's cell sequence (`_match_run`): a window that enters near the corridor's first cell, exits near its last, covers at least `MATCH_COVER` of its cells, and travels a plausible distance (`DIST_LO`–`DIST_HI` times the corridor length). Everything downstream, climbs, loops, anchored loops, dedupe, is variations of this window matching over cell sequences, plus elevation and shape tests on the matched sub-track.

Jargon used throughout:
- **cell**: one ~25 m grid square; the unit of spatial matching.
- **chain**: the raw mined cell sequence from `_mine_chains`; **corridor**: a point-to-point stretch of a chain after loop-splitting, the unit efforts are timed against.
- **effort**: one run's timed completion of a segment (a run can contribute several laps).
- **medoid**: of a set of loop traversals, the one whose shape is closest to all the others; used as the drawn line because it follows real streets (an average would cut corners).
- **anchor line**: a fixed polyline derived from the cleanest real lap of a loop, against which every run is window-matched Strava-style (entry point anywhere on the loop).
- **isoperimetric roundness**: `4*pi*Area / Perimeter^2` of a traversal (1.0 = circle, ~0 = out-and-back sliver); used to reject fake "loops" that are really there-and-backs.

## Key files and functions

All detection lives in `lib/generate_segments.py`. Entry point: `build_segments(runs)`, called by `generate_hub.py` (root). Inputs come from `strava_compile.py` via the enriched CSV.

| Function | Role |
|---|---|
| `build_segments` | Orchestrator; cache check, all stages in order (see pipeline-stages.md) |
| `_runs_signature` | SHA1 over run metadata + a params token string; the cache key |
| `_geo_track`, `_densify`, `_cell_sequence` | Stream → geo-track → cell sequence |
| `_mine_chains`, `_split_chain_at_loop` | Corridor mining and line+loop splitting |
| `_collect_efforts`, `_match_run` | Window matching, one fastest effort per run |
| `_segment_from_efforts` | Gates (runs/span/length/roundness), classification, segment dict |
| `_extend_climb_chain`, `_steep_subclimb`, `_salvage_climb_from_corridor` | Climb growth, steep-pitch extraction, buried-climb recovery |
| `_detect_loops`, `_run_loops`, `_cluster_loops` | Two-phase closed-loop detection |
| `_derive_anchor_line`, `_match_anchor_spans`, `_anchored_segment` | Fixed-line loop matching |
| `_auto_anchor_segments`, `_build_anchored_segments` | Auto and config-declared anchors |
| `_canonicalize_loops`, `_dedupe`, `_drop_loop_fragments`, `_drop_through_loops` | Final filtering |
| `_name_segments`, `_match_segments` | Naming and road-snapping (details: external-apis skill) |

Caches (all under `cache/`, path constants `SEG_CACHE`, `GEO_CACHE`, `MATCH_CACHE`, `STARS_CACHE`): `segments_cache.json` (signature + final segments), `segment_geocode_cache.json` (names), `segment_match_cache.json` (snapped polylines), `segment_stars.json` (user stars; may not exist). Config anchors live in `cache/config.json` via `load_config()` (`lib/config.py`).

Env toggles: `STRAVA_SEG_DEBUG=1` prints per-segment match/reject diagnostics; `STRAVA_PROFILE=1` prints per-stage timings.

## Tunables

Full table with meanings, units, and what breaks when you move each: `tunables.md`. Headlines (as of 2026-07; verify with `rg -n "^CELL_M|^MIN_RUNS|^MATCH_COVER|^MIN_LEN_M|^MIN_SPAN_DAYS" lib/generate_segments.py`):

- `CELL_M = 25.0`, `DENSIFY_M = 15.0`, the spatial resolution everything is built on.
- `MIN_RUNS = 2`, `MIN_SPAN_DAYS = 14`, `MIN_LEN_M = 400.0`, `MATCH_COVER = 0.80`, `DIST_LO, DIST_HI = 0.70, 1.40` (one tuple-assignment line), the core gates.
- Climb gates: `CLIMB_MIN_GAIN = 15.0` m net, `CLIMB_MIN_GRADE = 0.025`, floor `CLIMB_MIN_LEN_M = 150.0`.
- **Any behaviour-affecting change requires bumping a version token inside `_runs_signature` and deleting `cache/segments_cache.json`**, see tunables.md "The params token".

## Playbooks

Symptom-driven procedures in `debugging-playbook.md`:
(a) results didn't change after my code change · (b) segment disappeared · (c) effort count dropped/jumped · (d) climb shows grade 0% / flat · (e) duplicate or overlapping segments · (f) loop drawn through a building · (g) an expected segment is missing (gates walkthrough) · (h) rebuild slow. Includes a scratchpad snippet for dumping `cache/segments_cache.json`.

## Verification

After any detection change:
1. Bump one version token in the `_runs_signature` params string (e.g. `salvageclimb1` → `salvageclimb2`) and delete `cache/segments_cache.json`.
2. Run `python generate_hub.py` (or `python main.py`) from the repo root. Watch for `Segments: detected N benchmark segments`.
3. Run it again: it must print `Segments: loaded N from cache` with the same N. Then delete the cache and rebuild once more, the segment list must be identical (determinism given the same data is the invariant, not any fixed count).
4. Check baselines against `.claude/skills/validation-playbook/SKILL.md`. Snapshot (as of 2026-07-02): 73 segments total; `The Colonnade loop (Waverton)`, type loop, 392 m, 29 efforts. Counts grow as new runs are added.

## Pitfalls

- **Stale cache**: `_runs_signature` hashes only `date_iso|dist_km|len(gps_polyline)` + polyline endpoints per run, plus the params token. It does NOT hash `dist_stream` contents or your code. Code edits silently serve cached results (playbook a).
- The segments cache is `cache/segments_cache.json`, not `csv_data/` (old notes are wrong). Old baselines of 14 segments / 24 Colonnade efforts are obsolete; current snapshot above.
- Salvaged climbs (`_salvage_climb_from_corridor`) compete in `_dedupe` by effort count and INTENTIONALLY reshape neighbouring climbs. Accepted behaviour, do not "fix" it.
- A climb with missing elevation classifies as a plain "segment" with grade 0. That is an upstream stream bug (elevation must be distance-interpolated), not a detection bug, route to compile-and-streams.
- Loop vs non-loop segments over the same ground, and a climb vs its reverse descent, are deliberately kept as separate benchmarks in `_dedupe`.
- `MAX_SEGMENTS = 100` caps output after sorting by `(-n_efforts, -length_m)`; low-effort segments can silently fall off the end when new segments appear.
- Editing `segment_anchors` in `cache/config.json` changes the signature automatically (it is interpolated into the params token), a rebuild follows without any manual bump.
