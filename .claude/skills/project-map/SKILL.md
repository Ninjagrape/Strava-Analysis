---
name: project-map
description: Orientation map for the Strava-Analysis repo. Explains the two-stage pipeline (strava_compile.py builds an enriched CSV from a Strava export, generate_hub.py renders dashboards/TrainingHub.html), which module owns what, where inputs/caches/outputs live, and project jargon (effort, corridor, chain, loop, anchor, medoid, geo_key, params token). Use when starting any task in this repo with no prior context, when unsure which file owns a behaviour, when a term like "corridor" or "geo_key" is unclear, when deciding which sibling skill to load, or when a symptom cannot yet be attributed to a stage (compile vs hub vs segments).
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- You are new to this repo and need the lay of the land before touching anything.
- You need to know which file owns a feature, constant, or output artifact.
- You hit an unfamiliar project term (corridor, medoid, params token, ...).
- You have a symptom but do not yet know which pipeline stage produced it.

> **Cheaper than reading files:** `graphify-out/` holds a prebuilt knowledge graph of this repo (`graph.json`, `GRAPH_REPORT.md`, `graph.html`). For structural questions (call graphs, file ownership, module relationships), query it via the `/graphify` skill before opening source files; whole-file reads of the big modules cost tokens that grow with the repo. Re-run `/graphify` after structural changes.

## When NOT to use

This skill only orients. For actual work, route by task:

| Task or symptom | Load |
|---|---|
| Run the pipeline, regenerate outputs, profiling flags | `.claude/skills/run-and-regenerate/SKILL.md` |
| Segment missing / duplicated / wrong shape, detection constants | `.claude/skills/segment-detection/SKILL.md` |
| Stale results, cache files, `_runs_signature`, when to delete what | `.claude/skills/caches-and-invalidation/SKILL.md` |
| Nominatim / Overpass calls, slow rebuilds, segment names, map-matching | `.claude/skills/external-apis/SKILL.md` |
| .fit parsing, enriched CSV columns, stream density, best efforts | `.claude/skills/compile-and-streams/SKILL.md` |
| HTML/JS/CSS of TrainingHub.html, tabs, maps, charts | `.claude/skills/hub-ui/SKILL.md` |
| CTL/ATL/TSB, ACWR, critical speed, VDOT, Riegel, goals panel | `.claude/skills/analytics-and-goals/SKILL.md` |
| Verifying a change is correct (no automated tests exist) | `.claude/skills/validation-playbook/SKILL.md` |

## Mental model

Two-stage batch pipeline, orchestrated by `main.py` (37 lines, as of 2026-07):

```
Strava export zip (~/Downloads/export_*.zip: activities.csv + *.fit.gz)
        │  python strava_compile.py          (stage 1, "compile")
        ▼
csv_data/YYYY-MM-DD_strava.csv               (enriched CSV: 153 columns as of 2026-07)
        │  python generate_hub.py            (stage 2, "hub")
        ▼
dashboards/TrainingHub.html                  (single self-contained file, ~9 MB as of 2026-07)
```

The hub HTML has 6 tabs: Overview, Best Efforts, Goals, Analytics, Runs, Segments.

Three load-bearing ideas:

1. **The enriched CSV IS the parse cache.** `strava_compile.py` skips .fit files whose activity ids already appear in the latest `csv_data/*_strava.csv` unless `--rebuild` is passed (the `--rebuild` flag's help text: "Ignore the prior CSV cache and re-parse every .fit file"). Consequence: changing any stream/density constant silently does nothing to old rows until you `--rebuild`. As of 2026-07-08 the cache is LIVE: `strava_compile.py` now raises `csv.field_size_limit`, so `load_prior_fit_cache` successfully reads the prior CSV and reuses its rows (a normal compile reports "Reused 79 cached, parsed 0 new"); `--rebuild` is genuinely mandatory after any parse-logic or density change, full story in `.claude/skills/compile-and-streams/SKILL.md`. Grep: `grep -n "rebuild" strava_compile.py`.
2. **The hub is fully regenerated every run.** `generate_hub.py` writes `dashboards/TrainingHub.html` from scratch; HTML is never patched incrementally. Any hub change is safe to test by just re-running stage 2 (~1.7 s as of 2026-07).
3. **Segments are a semi-independent subsystem.** `build_segments(runs)` in `lib/generate_segments.py` mines recurring routes and keeps its own JSON caches in `cache/` (`segments_cache.json`, `segment_geocode_cache.json`, `segment_match_cache.json`, `segment_stars.json`), guarded by a signature hash (`_runs_signature`). Stale-looking segment output usually means a cache hit, not broken detection. See `.claude/skills/caches-and-invalidation/SKILL.md`.

### Glossary

- **Enriched CSV**: the Strava export's `activities.csv` rows plus ~56 computed `fit_*`/`best_*`/`interval_*` columns appended by stage 1. Stream cells can exceed 1 MB, so readers need `csv.field_size_limit` raised (see Pitfalls).
- **dist_stream**: the `fit_distance_stream` CSV column, a JSON list of per-run sample points, each a dict with keys `d, t, pace, elev, lat, lon` (plus `hr` and `cad` when present in the record; elevation key is `elev`, not `e`; as of 2026-07).
- **Effort**: one run's timed completion of a segment (fields like `time_s`, `pace_str`, `date_iso`).
- **Chain**: a mined sequence of adjacent grid cells (the `CELL_M = 25.0` m grid, as of 2026-07) shared by multiple runs; the raw material segments are cut from.
- **Corridor**: a point-to-point stretch of a chain, candidate for a line segment or climb.
- **Loop**: a closed segment (start/end within `LOOP_CLOSE_M = 60.0` m, as of 2026-07); laps are counted per run.
- **Anchor**: a derived reference line for a loop that runs traverse mid-route without closing a lap; auto-detected, or pinned via `segment_anchors` in `cache/config.json`.
- **Medoid**: the single real lap whose track best represents a loop, chosen by `_medoid_index` (line segments instead draw from the effort nearest the median distance). Either way the drawn line comes from one real run, so it drifts as runs are added, which is why the geo caches use distance-tolerant reuse (`NAME_REUSE_M`, `MATCH_REUSE_M`).
- **geo_key**: cache key for a segment's line, three "lat,lon" points rounded to 4 dp (~11 m, as of 2026-07) joined by `|`. The 4 dp is hard-coded in `_geo_key`; it matches `GEO_MEMO_DP = 4` by design, not by reference.
- **params token**: the constants-and-version string hashed into `_runs_signature` alongside per-run metadata; bump it (add/change a version token like `refine2`) whenever detection logic changes, because the signature does NOT hash `dist_stream`.
- **Threshold speed**: the reference speed pace zones are fractions of; the hub derives it from the best 5K (it prints e.g. `Threshold pace (from best 5K): 4:48/km`), with zone bounds `[0.77, 0.87, 0.93, 1.03]` in `_compute_pace_zones` in `generate_hub.py` (as of 2026-07). Grep: `grep -n "bounds = \[0.77" generate_hub.py`.

## Key files and functions

| File | Lines (as of 2026-07) | Single responsibility |
|---|---|---|
| `main.py` | 37 | Runs the two stages via a `STEPS` tuple; forwards CLI args only to the compile step; `--profile` also sets `STRAVA_PROFILE=1` in the env for the hub. |
| `strava_compile.py` | 966 | Stage 1. Finds the newest `export_*.zip`/folder in Downloads, unzips, decompresses `.fit.gz`, parses with `fitparse` (hard requirement, `SystemExit` if missing), computes best efforts / splits / streams, writes the enriched CSV. Entry: `main()`. |
| `generate_hub.py` | 2956 | Stage 2. Reads the latest enriched CSV, builds run dicts (`_build_runs`), classifies runs (`_classify_run`), computes pace zones (`_compute_pace_zones`), assembles all six tabs, writes `dashboards/TrainingHub.html`. Inserts `lib/` on `sys.path` so plain imports work. |
| `lib/generate_segments.py` | 2475 | Segment mining + Segments tab. Public surface used by the hub: `build_segments(runs)`, `body_segments(segments, updated)`, `SEGMENTS_CSS`. Owns all `cache/segment*` JSON files and the Nominatim/Overpass network code. |
| `lib/generate_analytics.py` | 1183 | Analytics tab math: `ctl_atl_tsb`, `acwr_series`, `monotony_strain`, `fit_critical_speed`, `daniels_vo2max`/`vdot_trend`; timezone-corrects dates via optional `timezonefinder`. |
| `lib/generate_dashboards.py` | 958 | Best Efforts + Goals tabs: `riegel`, `fit_riegel`, `derive_training_target`, `_predict_race_time`, shared formatters (`fmt_pace`, `fmt_time`, `ga_time`). |
| `lib/config.py` | 88 | `load_config()` reads optional `cache/config.json` into frozen dataclasses (`Race`, `SegmentAnchor`); never raises, degrades to an empty `Config`. |
| `README.md` | 549 | Long-form docs: output column reference, classification rules, methodology. |

### Data artifact map

| Artifact | Role | Location |
|---|---|---|
| `export_*.zip` or `export_*/` | Input: Strava bulk export (`activities.csv` + `activities/*.fit.gz`) | `~/Downloads` by default (`--downloads`/`--archive` override) |
| `csv_data/YYYY-MM-DD_strava.csv` | Stage-1 output AND parse cache (latest file wins) | `csv_data/`, gitignored |
| `cache/segments_cache.json` | Detected segments + `signature` guard (73 segments as of 2026-07). NOT in `csv_data/`. | `cache/`, gitignored |
| `cache/segment_geocode_cache.json` | geo_key → segment name (Nominatim results) | `cache/` |
| `cache/segment_match_cache.json` | geo_key → map-matched polyline (Overpass; `[]` = "no snap" is cached too) | `cache/` |
| `cache/segment_stars.json` | User-starred geo_keys; may not exist until something is starred | `cache/` |
| `cache/config.json` | Optional user config (races, segment anchors); template at `config.example.json` | `cache/` |
| `dashboards/TrainingHub.html` | Final output, fully self-contained | `dashboards/`, gitignored |

## Playbooks

1. **Orient on a fresh task**: read this skill; run `python main.py --profile` to confirm the pipeline is green end-to-end (whole run ~4.5 s on 79 runs as of 2026-07); note which stage's timing bracket your task falls in; load the sibling skill from the routing table above.
2. **Attribute a symptom to a stage**: wrong/missing raw data (distance, elevation, splits, stream density) → stage 1, `.claude/skills/compile-and-streams/SKILL.md`. Wrong rendering/classification/derived metric → stage 2. Segment-specific → `lib/generate_segments.py`, and check caches first (`.claude/skills/caches-and-invalidation/SKILL.md`).
3. **Find where a value comes from**: grep the constant name across `strava_compile.py`, `generate_hub.py`, `lib/*.py` (e.g. `grep -rn "MAX_SEGMENTS" lib/`). Constants live at module top with explanatory comments; there is no shared settings module apart from `lib/config.py` (user config only).

## Verification

- Pipeline shape still true: `grep -n "STEPS" main.py` should show exactly two steps (compile with `forward_args=True`, hub with `False`).
- Hub public surface of segments unchanged: `grep -n "build_segments\|body_segments\|SEGMENTS_CSS" generate_hub.py lib/generate_segments.py`.
- Cache locations: `grep -n "CACHE_DIR" lib/generate_segments.py` (must resolve to `<repo>/cache`, sibling of `lib/`).
- Artifact presence after a run: `ls csv_data cache dashboards` shows a dated CSV, the three-or-four `segment*` JSONs plus `config.json`, and `TrainingHub.html`.

## Pitfalls

- `GPS_MAX_POINTS` is **2500** (as of 2026-07), not 600 as some older notes claim: `grep -n "GPS_MAX_POINTS" strava_compile.py`.
- `segments_cache.json` lives in `cache/`, never in `csv_data/`: `grep -n "SEG_CACHE" lib/generate_segments.py`.
- Reading the enriched CSV with stdlib `csv` fails with "field larger than field limit" unless you first call `csv.field_size_limit(min(sys.maxsize, 2**31 - 1))`. `lib/generate_analytics.py`, `lib/generate_dashboards.py`, and `strava_compile.py` all do this today (as of 2026-07-08): `grep -rn "field_size_limit" .`
- Stream points key elevation as `elev` (`d, t, pace, elev, lat, lon`), not `e`.
- `_runs_signature` hashes per-run `date_iso|dist_km|len(gps_polyline)` + endpoints + the params token; it does NOT hash `dist_stream`, so stream-content changes alone will not invalidate `cache/segments_cache.json`.
- `minetti_cost`/`ga_time`/`COST_FLAT` were deliberately duplicated in both `lib/generate_analytics.py` and `lib/generate_dashboards.py`; as of 2026-07-08 they are defined once in `lib/grade.py` and imported by both.
- `DIST_LO, DIST_HI = 0.70, 1.40` is one tuple-assignment line in `lib/generate_segments.py`, not two definitions; naive constant-grep patterns can miss it.
