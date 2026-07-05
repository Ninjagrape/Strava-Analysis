# Tunables: detection constants in lib/generate_segments.py

> Values are snapshots (as of 2026-07), re-verify with the grep recipes.

All constants live near the top of `lib/generate_segments.py` unless noted. A quick way
to list every module-level constant: `rg -n "^[A-Z][A-Z0-9_]+ *=" lib/generate_segments.py`.
"Raise" and "Lower" describe the practical consequence of moving the value in that
direction. Any change to a constant that appears in the params token rebuilds
automatically; any change to one that does NOT appear in the token (marked "not in
token" below) requires a manual version-token bump, see "The params token" at the end.

## Spatial resolution (stage 2)

Verify: `rg -n "^CELL_M|^DENSIFY_M" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning | Raise it | Lower it |
|---|---|---|---|---|---|
| `CELL_M` | 25.0 | m | Grid cell size; the unit of all spatial matching | Sloppier matching: parallel paths merge, coverage rises, false matches | Stricter: GPS wobble breaks cell sequences, coverage drops, segments vanish |
| `DENSIFY_M` | 15.0 | m | Max gap between track points after interpolation (not in token) | Sparse tracks skip cells, breaking corridor continuity | More points, slower build, little accuracy gain |

Detection is tuned for ~20 m GPS spacing; the compile step already delivers ~7 m
streams, so treat these two as load-bearing and coupled.

## Core effort gates (stage 4)

Verify: `rg -n "^MIN_RUNS|^MIN_SPAN_DAYS|^MIN_LEN_M|^MATCH_COVER|DIST_LO" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning | Raise it | Lower it |
|---|---|---|---|---|---|
| `MIN_RUNS` | 2 | runs | Distinct runs that must complete a segment (also the corridor-mining transition floor) | Fewer, higher-confidence segments; corridors fragment less often but rarer | One-off routes become "segments"; mining graph explodes |
| `MIN_SPAN_DAYS` | 14 | days | First-to-last effort span required | Segments appear later after you start a new route | Two runs in one week qualify; noise |
| `MIN_LEN_M` | 400.0 | m | Length floor for plain (non-climb, non-loop) segments | Short repeated stretches disappear | Trivial stretches clutter the panel |
| `MATCH_COVER` | 0.80 | fraction | Corridor cells a run's window must visit to complete | GPS drift fails real completions; effort counts drop | Partial traversals count; times incomparable |
| `DIST_LO, DIST_HI` | 0.70, 1.40 | ratio | Allowed window distance vs segment length (single tuple-assignment line) | Narrowing rejects detours/GPS noise but drops honest efforts | Widening lets a window wander far off the corridor |

## Climb constants (stage 5)

Verify: `rg -n "^CLIMB_|^SUBCLIMB_|SALVAGE_" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning | Raise it | Lower it |
|---|---|---|---|---|---|
| `CLIMB_MIN_GAIN` | 15.0 | m | Net ascent required to classify "climb" | Gentle hills become plain segments (and hit the 400 m floor) | Flat noise classifies as climbs |
| `CLIMB_MIN_GRADE` | 0.025 | fraction | Average grade required to classify "climb" | Same as above | Same as above |
| `CLIMB_MIN_LEN_M` | 150.0 | m | Length floor for climbs (also the corridor-piece floor and the salvage window floor) | Short steep benchmarks (e.g. ~210 m John St) vanish | Tiny ramps qualify |
| `CLIMB_REV_COVER` | 0.75 | fraction | Looser coverage when re-matching a descent corridor's reverse as a climb | Ascents on a slightly different line fail; uphill benchmarks lost | Reverse matches get sloppy |
| `SUBCLIMB_MIN_GRADE` | 0.06 | fraction | Steepest sub-window surfaced as its own climb only when this steep | Fewer "burn" sub-climbs | Mild pitches duplicated out of parents |
| `SUBCLIMB_STEEPER` | 1.6 | ratio | Sub-window must be this many times the parent's grade | Same | Same |
| `SUBCLIMB_MAX_FRAC` | 0.75 | fraction | Sub-window must be no longer than this fraction of parent | Near-whole-parent duplicates appear | Only very short pitches extracted |
| `CLIMB_EXT_MAX_M` | 400.0 | m | Max growth past each end of a clipped climb toward foot/crest | Extensions can swallow adjacent terrain (still gated by effort count) | Climbs stay clipped at junctions |
| `CLIMB_EXT_DROP_TOL` | 1.5 | m | Elevation noise tolerated while deciding ground still drops/rises | GPS spikes stop mattering, but real dips get skated over | Noise ends extensions early |
| `CLIMB_EXT_REVERSE_M` | 35.0 | m | A reversal must be sustained this far before extension stops | Extension runs past the true crest/foot | Mid-hill dips truncate the climb |
| `SALVAGE_CLIMB_MAX_M` | 650.0 | m | Cap on the salvage sub-window (beyond it, it is a route, not a hill) | Salvage can return whole-route "climbs" | Long shared climbs stay buried |
| `SALVAGE_MIN_OVERLAP` | 6 | cells | Cells a run must share with a failed corridor to be match-tested (prefilter) | Faster salvage, may miss marginal runs | Slower salvage (O(n^2) scan over more runs) |

## Loop constants (stage 6)

Verify: `rg -n "^LOOP_" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning | Raise it | Lower it |
|---|---|---|---|---|---|
| `LOOP_CLOSE_M` | 60.0 | m | Drawn endpoints closer than this classify a corridor as a loop | Out-and-backs misread as loops | Real loops with a small GPS gap classify as segments |
| `LOOP_MIN_LEN_M` | 600.0 | m | Phase 1 floor for identifying a distinct loop | Small circuits never identified | Sub-loops fragment bigger circuits |
| `LOOP_LAP_MIN` | 300.0 | m | Phase 2 floor when re-scanning runs to count individual laps | Short laps of a small circuit uncounted | Micro-loops counted as laps |
| `LOOP_MIN_SEG_M` | 350.0 | m | Floor on a loop's drawn single lap (below general MIN_LEN_M; e.g. Colonnade at 392 m closes a touch short) | Legit small circuits rejected | Tiny circuits qualify |
| `LOOP_MIN_CELLS` | 8 | cells | Min cells between a loop's two near-coincident points | Small loops missed entirely | Trivial self-touches read as loops |
| `LOOP_UNIQUE_FRAC` | 0.80 | fraction | A loop visits >= this fraction of its cells only once | Loops with shared in/out spurs rejected | Out-and-back retraces read as loops |
| `LOOP_CLUSTER_M` | 120.0 | m | Same-loop centroid distance across runs | Distinct nearby circuits merge | The same circuit splits into several clusters, each under-gated |
| `LOOP_LEN_RATIO` | 1.35 | ratio | Same-loop length agreement (also the phase-2 lap gate) | 1.5-lap passes pollute lap counts | Honest lap-length variation excluded, undercounting |
| `LOOP_DEDUPE_M` | 120.0 | m | Loops with drawn centroids within this are one circuit at different lap scales (canonicalize) | Distinct adjacent circuits collapse | Multi-scale duplicates of one circuit rendered |
| `LOOP_MIN_ROUNDNESS` | 0.20 | isoperimetric quotient | Floor for a drawn loop; below it is a there-and-back sliver | Skinny but real circuits (long thin blocks) rejected | Parallel-path there-and-backs render as loops |
| `LOOP_JOIN_M` | 15.0 (2026-07-04) | m | A loop candidate's two ends must refine to within this to count as a closure; wider is two parallel streets the grid can't separate (Shirley Rd vs Belmont Ave, ~40 m), not a real return. Genuine closures refine to <6 m, artifacts 18-45 m | Real loops that close loosely (>15 m GPS drift) dropped; do not exceed the ~18 m artifact floor | Parallel-street half-loops truncate the real circuit again |
| `LOOP_OVERLAP_FRAC` | 0.60 | fraction | A non-loop covering this much of a loop's cells is a fragment, dropped | 3/4-loop fragments survive next to the loop | Real climbs that share part of a loop get dropped |
| `LOOP_THRU_COV` | 0.50 | fraction | Coverage of a loop by a comparable-length straight segment that marks it as a false up-and-back loop | False parallel-way loops survive | Real loops with a long through-corridor dropped |
| `LOOP_THRU_LENRATIO` | 0.90 | ratio | The covering segment must also be >= this fraction of the loop's length | Same as above | Same as above |

Also hard-coded in loop scanning: a 12000 m cap on any single loop candidate
(`rg -n "12000" lib/generate_segments.py`).

## Chain / corridor constants (stage 3, 9)

Verify: `rg -n "^CHAIN_SELFCROSS_M|^CORRIDOR_END_TOL_M|MAX_PATH_CELLS|^MAX_SEGMENTS|CONSENSUS_N" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning | Raise it | Lower it |
|---|---|---|---|---|---|
| `CHAIN_SELFCROSS_M` | 60.0 | m | A mined chain approaching a much-earlier cell this closely is split (line+loop) | Chains split on near-passes that aren't loops | Line+loop chains survive as one bogus segment |
| `CORRIDOR_END_TOL_M` | 80.0 | m | Two point-to-point segments whose starts and ends both align within this are the same stretch (dedupe) | Distinct nearby stretches merge | GPS-split duplicates of one stretch both render |
| `MAX_PATH_CELLS` | 2400 | cells | ~60 km guard on a single grown corridor (defined mid-file above `_mine_chains`; not in token) | Slower mining, longer overshoots | Long legitimate corridors truncated |
| `MAX_SEGMENTS` | 100 | segments | Cap on rendered segments after the `(-n_efforts, -length_m)` sort (not in token) | Bigger panel | Low-effort segments silently fall off |
| `CONSENSUS_N` | 64 | points | Resample count for medoid shape comparison (defined mid-file; not in token) | Slower medoid selection | Coarser shape comparison |

## Anchor constants (stages 7-8)

Verify: `rg -n "^ANCHOR_|^AUTO_ANCHOR_" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning | Raise it | Lower it |
|---|---|---|---|---|---|
| `ANCHOR_TOL_M` | 25.0 | m | A run point this close to the anchor line counts as "on" it | Off-line passes count; sloppy efforts | GPS wobble fails real laps |
| `ANCHOR_COVER` | 0.80 | fraction | Fraction of the line a window must cover to complete | Real laps with a shortcut fail | Partial passes count |
| `ANCHOR_NEAR_M` | 130.0 | m | Cleanest instance must be within this of the anchor centre | Wrong nearby loop can seed the line | No instance found; anchor yields nothing |
| `ANCHOR_LINE_STEP` | 10.0 | m | Resample spacing of the derived line | Coarser coverage counting | Slower matching |
| `ANCHOR_CLOSE_M` | 55.0 | m | A lap's start and end must be this close (real loops close) | Non-lap passes count as laps | Laps with a small closure gap rejected |
| `AUTO_ANCHOR_MAX_CANDIDATES` | 25 | candidates | Cap on auto-anchor loops tried (most runs first) | Slower rebuild (span-matching every candidate vs all runs) | Recoverable loops missed |
| `AUTO_ANCHOR_MIN_RUNS` | 3 | runs | Distinct line-matched runs an auto-anchor needs (higher than MIN_RUNS because embedded traversal is a weaker signal) | Fewer recovered loops | 2-run incidental loops flood in |
| `AUTO_ANCHOR_MINED_GAP_M` | 120.0 | m | Skip auto-anchor candidates this close to a mined loop | Legit distinct nearby loop skipped | Duplicate re-timings of mined loops |

## Cache reuse / pruning and naming/snapping constants (stage 10)

These affect naming and road snapping, not which segments exist. Symptoms in this area
route to `.claude/skills/external-apis/SKILL.md`. None are in the params token.
Verify: `rg -n "^NAME_REUSE_M|^MATCH_REUSE_M|^CACHE_PRUNE_M|GEO_MEMO_DP|^MATCH_|^SNAP_" lib/generate_segments.py`

| Constant | Value (as of 2026-07) | Unit | Meaning |
|---|---|---|---|
| `NAME_REUSE_M` | 40.0 | m | Reuse a cached name whose centroid is within this (medoid drift tolerance) |
| `MATCH_REUSE_M` | 50.0 | m | Reuse a cached road snap within this |
| `CACHE_PRUNE_M` | 150.0 | m | Drop name/match cache entries farther than this from every current segment |
| `GEO_MEMO_DP` | 4 | decimals | ~11 m rounding to dedupe reverse-geocode points within one build |
| `MATCH_SNAP_M` | 35.0 | m | Resampled point farther than this from any OSM path is dropped |
| `MATCH_STEP_M` | 20.0 | m | Resample spacing before snapping |
| `MATCH_MAX_DEV_M` | 12.0 | m | Mean deviation above which a snap is rejected (kept raw trace) |
| `MATCH_TURN_DEG` | 55.0 | degrees | Direction change counting as a "reversal" |
| `MATCH_MAX_TURN_GAIN` | 0.06 | reversals/point | Extra reversal rate above which a snap is a zig-zag artefact |
| `SNAP_INDEX_CELL_M` | 60.0 | m | Grid cell for the shared-graph edge index (> MATCH_SNAP_M by design) |
| `SNAP_UNION_MAX_DEG` | 0.6 | degrees | Above this span (~65 km) the union Overpass fetch falls back to per-segment |
| `MATCH_EXCLUDE` | motorway, motorway_link, trunk, trunk_link, construction, proposed, raceway | set | highway= values excluded from the walk graph |

Paths and endpoints (verify `rg -n "^CACHE_DIR|_CACHE |^NOMINATIM_URL|^OVERPASS_URL|^USER_AGENT" lib/generate_segments.py`):
`SEG_CACHE = cache/segments_cache.json`, `GEO_CACHE = cache/segment_geocode_cache.json`,
`MATCH_CACHE = cache/segment_match_cache.json`, `STARS_CACHE = cache/segment_stars.json`
(may not exist on disk), Nominatim reverse endpoint, Overpass interpreter endpoint, and
`USER_AGENT = "Strava-Analysis-Hub/1.0 (personal training dashboard)"`.

Env toggles (as of 2026-07; `rg -n "STRAVA_SEG_DEBUG|STRAVA_PROFILE" lib/generate_segments.py`):
`STRAVA_SEG_DEBUG=1` prints per-segment match/reject diagnostics; `STRAVA_PROFILE=1`
prints per-stage timings.

## The params token

`_runs_signature` (verify: `rg -n "def _runs_signature" -A 20 lib/generate_segments.py`)
hashes each run's `date_iso|dist_km|len(gps_polyline)` plus polyline endpoints, then
one params token string. With the current constant values the token evaluates to
(as of 2026-07-02):

```
params:25.0,2,400.0,0.8,loops:8,0.8,120.0,1.35,350.0,120.0,refine2,round1,medoid1,laps1,merge1,twophase2,matchturn1,loopfloor1,climbrev1,loopscale1,loopdedupe1,shape1:150.0,0.2,0.6,60.0,thruloop1:0.5,0.9,anchors:<load_config().segment_anchors>,25.0,0.8,55.0,autoanchor2:25,3,120.0,locdedupe1,close1,anchorline2,elevprofile1,runtype1,nodecimate1,subclimb1:0.06,1.6,0.75,climbext3noloss:400.0,1.5,35.0,salvageclimb1:650.0,6
```

The `anchors:` component interpolates `load_config().segment_anchors` from
`cache/config.json`, so editing `segment_anchors` in config changes the signature and
self-invalidates the cache, no manual bump needed for config edits.

Embedded version tokens (as of 2026-07): `refine2, round1, medoid1, laps1, merge1,
twophase2, matchturn1, loopfloor1, climbrev1, loopscale1, loopdedupe1, shape1,
thruloop1, autoanchor2, locdedupe1, close1, anchorline2, elevprofile1, runtype1,
nodecimate1, subclimb1, climbext3noloss, salvageclimb1`.

### The bump-and-delete procedure

Any behaviour-affecting change to detection logic (or to a constant NOT interpolated
into the token, such as `DENSIFY_M`, `MIN_SPAN_DAYS`, `DIST_LO/DIST_HI`,
`CLIMB_MIN_GAIN`, `CLIMB_MIN_GRADE`, `CLIMB_REV_COVER`, `LOOP_CLOSE_M`,
`LOOP_MIN_LEN_M`, `LOOP_LAP_MIN`, `CORRIDOR_END_TOL_M`,
`MAX_PATH_CELLS`, `MAX_SEGMENTS`, `ANCHOR_NEAR_M`, `ANCHOR_LINE_STEP`) must be made
visible to the cache:

1. Bump one version token inside the `_runs_signature` f-string, e.g.
   `salvageclimb1` → `salvageclimb2` (pick the token nearest the code you changed, or
   add a new one for a new feature).
2. Delete `cache/segments_cache.json`. The signature change alone would force a
   rebuild, but deleting guarantees no stale serve even if the bump is later reverted
   or mis-typed.
3. Rebuild (`python generate_hub.py` from the repo root) and verify per the
   Verification section of `SKILL.md` (detected N, then loaded N from cache, then a
   deterministic re-detect).

Constants that ARE interpolated (their value literally appears in the token, so
changing them self-invalidates): `CELL_M`, `MIN_RUNS`, `MIN_LEN_M`, `MATCH_COVER`,
`LOOP_MIN_CELLS`, `LOOP_UNIQUE_FRAC`, `LOOP_LEN_RATIO`, `LOOP_CLUSTER_M`, `LOOP_MIN_SEG_M`,
`LOOP_DEDUPE_M`, `LOOP_JOIN_M`, `CLIMB_MIN_LEN_M`, `LOOP_MIN_ROUNDNESS`, `LOOP_OVERLAP_FRAC`,
`CHAIN_SELFCROSS_M`, `LOOP_THRU_COV`, `LOOP_THRU_LENRATIO`, `ANCHOR_TOL_M`,
`ANCHOR_COVER`, `ANCHOR_CLOSE_M`, `AUTO_ANCHOR_MAX_CANDIDATES`,
`AUTO_ANCHOR_MIN_RUNS`, `AUTO_ANCHOR_MINED_GAP_M`, `SUBCLIMB_MIN_GRADE`,
`SUBCLIMB_STEEPER`, `SUBCLIMB_MAX_FRAC`, `CLIMB_EXT_MAX_M`, `CLIMB_EXT_DROP_TOL`,
`CLIMB_EXT_REVERSE_M`, `SALVAGE_CLIMB_MAX_M`, `SALVAGE_MIN_OVERLAP`, plus the
`segment_anchors` config. When in doubt, bump a token anyway, a spurious rebuild
costs seconds; a stale cache costs a debugging session.
