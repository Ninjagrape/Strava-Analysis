---
name: compile-and-streams
description: Stage 1 of the Strava-Analysis pipeline - strava_compile.py parses a Strava bulk export (activities.csv + .fit.gz files) into the enriched CSV in csv_data/. Use when raw run data is wrong or missing (distance, elevation, HR, cadence, km splits, best efforts, interval reps), when net_elev reads 0 or climbs show as flat / 0% grade, when a reader crashes with "_csv.Error: field larger than field limit", when new runs do not appear after downloading a fresh export, when compile is slow, or when changing stream/polyline density constants or reasoning about the prior-CSV parse cache and --rebuild.
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- A per-run number is wrong at the source: distance, elevation, pace, HR, cadence, splits, best efforts, interval reps.
- `net_elev` is 0 downstream, or segments/climbs read as 0% grade or flat.
- Any reader of the enriched CSV dies with `_csv.Error: field larger than field limit (131072)`.
- A new run is missing from the dashboard after downloading a fresh export.
- You are changing stream density, polyline density, best-effort, split, or interval-detection logic.

## When NOT to use

- Which command to run after a change, flags, runtimes → `.claude/skills/run-and-regenerate/SKILL.md`
- Segments wrong despite correct streams → `.claude/skills/segment-detection/SKILL.md`
- Which cache to delete / stale outputs → `.claude/skills/caches-and-invalidation/SKILL.md`
- Hub rendering, tabs, charts → `.claude/skills/hub-ui/SKILL.md`
- Derived metrics (CTL, VDOT, Riegel) → `.claude/skills/analytics-and-goals/SKILL.md`
- Verifying a change end-to-end → `.claude/skills/validation-playbook/SKILL.md`

## Mental model

`strava_compile.py` `main()` runs eight steps, in order:

1. **Locate export**: `find_strava_export` picks the newest-by-mtime `export_*.zip` in `--downloads` (default `~/Downloads`); falls back to the newest `export_*/` folder. `--archive` bypasses discovery entirely.
2. **Unzip** (if `.zip`): `unzip_archive` extracts into `<--tmp>/extracted` (tmp default: `%TEMP%/strava_fit`) and returns the subdir containing `activities.csv`.
3. **Load metadata**: `load_activities_csv` reads the export's `activities.csv` (plain `csv.DictReader`, utf-8-sig).
4. **Load parse cache**: unless `--rebuild`, `load_prior_fit_cache` maps activity id → parsed columns from the single most-recent-by-mtime `csv_data/*_strava.csv`. See the verified semantics below, this cache is live as of 2026-07-08.
5. **Parallel parse**: every `.fit.gz`/`.fit` in `<export>/activities/` whose id missed the cache is decompressed (`decompress_fit_gz`, skipped if the `.fit` already sits in tmp) and parsed by `parse_fit` in a `ProcessPoolExecutor` (`--workers`, default CPU count). `.fit.gz` wins over a bare `.fit` with the same id.
6. **Merge**: each `activities.csv` row is joined to its parsed stats via `extract_activity_id(row["Filename"])` (double `.stem`, so `activities/1234.fit.gz` → `1234`), then filtered by `--sport` (substring match against "Activity Type" and `fit_sport`; default `running`).
7. **Write**: `csv_data/YYYY-MM-DD_strava.csv`, dated by the **export archive's mtime**, not today (`--out` overrides). Re-compiling the same export overwrites the same file.
8. Stream columns (`_STREAM_KEYS`: `fit_splits, fit_km_splits, fit_gps_polyline, fit_distance_stream`) are ordered last so column order stays stable.

### Parse-cache semantics (VERIFIED 2026-07-09, re-armed 2026-07-08)

The enriched CSV is *designed* to be the parse cache: a cache hit for an activity id copies that row's parsed columns verbatim (as strings) and skips the `.fit` parse. Reuse fires for an id only when ALL of: `--rebuild` not passed; the newest prior CSV loads without exception; that CSV has a row whose `Filename` yields the same id; and the row has at least one non-empty non-`activities.csv` column. Only the newest prior CSV is consulted (`break` after the first readable one).

**Why the cache is live as of 2026-07-08**: `strava_compile.py` now raises `csv.field_size_limit(min(sys.maxsize, 2**31 - 1))` at module top (added `import sys` and the call right after the `from datetime import datetime` line). Enriched stream cells exceed Python's 131,072-char default (measured: max cell 312,233 chars; 14 cells over), so before this fix `load_prior_fit_cache`'s read via `load_activities_csv` (plain `csv.DictReader`) raised `_csv.Error: field larger than field limit`, swallowed by a bare `except Exception: continue`, and the cache loaded empty. Now the read succeeds: a normal compile reports "Reused 79 cached, parsed 0 new"; a `--rebuild` compile parses all 79 fresh; the two outputs are byte-identical (verified). Grep: `grep -n "field_size_limit" strava_compile.py` (should show a hit near the top of the file).

**Changes that REQUIRE `--rebuild`**: anything inside `parse_fit` or its helpers, stream/polyline density constants, `_distance_stream`, `_best_efforts`, `_per_km_splits`, `_detect_reps`, `_pace_zone_secs`. A cache hit copies old values verbatim, so without `--rebuild` such changes silently do nothing to previously-parsed rows. This is now genuinely load-bearing: the cache is live, so a parse-logic edit without `--rebuild` will silently keep serving old values from prior runs.

### Stream construction (`_distance_stream`)

Builds `fit_distance_stream`: a JSON list of points `{d, t, pace, elev, hr, cad, lat, lon}` (`d` in km, elevation key is `elev`, NOT `e`), resampled **over distance** at `spacing = max(1000/STREAM_POINTS_PER_KM, total_dist/STREAM_MAX_POINTS)`, uniform ~7 m spatial resolution regardless of run length, capped at 3000 points past ~20 km. Each emitted point is a real `record` (epoch, cumulative distance); pace comes from a forward window of ≥ `STREAM_PACE_WINDOW_M` (50 m) and is nulled outside 120–1200 s/km.

**IRON LAW**: elevation and lat/lon attach to resampled points by **cumulative-distance interpolation** (`elev_at` / `pos_at`: bisect on distance-sorted fixes, linear interpolation, snapping to the nearer fix across gaps wider than `GEO_GAP_MAX_M` = 60 m), NEVER by exact epoch match. Elevation is recorded on only a fraction of records, so epoch matching leaves most resampled points elevation-less; downstream `net_elev` then silently collapses to 0 and every climb misclassifies as flat. This shipped in June 2026 after a density increase, the symptom was ~all segments reading 0% grade. HR and cadence DO attach by exact epoch (`epoch in hr_by_t`), which is safe only because emitted points are real record epochs and HR/cadence appear on most records; any genuinely sparse channel must use the `elev_at` pattern.

## Key files and functions

All stage-1 code lives in `strava_compile.py` (entry `main()`; 966 lines as of 2026-07).

| Function / name | Role |
|---|---|
| `find_strava_export`, `unzip_archive`, `find_activities_csv` | Export discovery and extraction |
| `decompress_fit_gz` | .gz → .fit into tmp; skips if target exists |
| `load_prior_fit_cache`, `extract_activity_id` | Prior-CSV parse cache (see semantics above) |
| `_parse_one` | Process-pool worker: decompress + `parse_fit`, never raises |
| `parse_fit` | One .fit → dict of all derived columns (session, laps, records) |
| `_best_efforts` + `BEST_EFFORT_DISTANCES` | Sliding two-pointer window over (epoch, cum-dist) track; 11 targets 400m–half; `None` when the run is shorter than the target |
| `_detect_reps`, `_interval_best_efforts` | Interval reps: only when session rest ratio ≥ `INTERVAL_REST_RATIO_THRESHOLD` (0.20); a rep ends after ≥ `INTERVAL_MIN_REST_DURATION_S` (5) consecutive records below `INTERVAL_SPEED_THRESHOLD_MPS` (2.0); needs ≥ 2 reps to populate |
| `_pace_zone_secs` | Per-zone seconds vs threshold speed; bounds `[0.77, 0.87, 0.93, 1.03]`; threshold = `PACE_ZONE_THRESHOLD_MPS` (None as of 2026-07) falling back to the activity's own avg speed |
| `_per_km_splits` | Interpolated km crossings + per-km gain/loss/cadence |
| `_distance_stream`, `pos_at`, `elev_at` | Over-distance resampler (see IRON LAW) |
| `_simplify_polyline`, `_polyline_point_budget` | `fit_gps_polyline` downsampling |

**Output schema**: 79 rows × 153 columns as of 2026-07-02 (97 original Strava columns + derived columns; 23 start with `fit_`; 4 heavy stream columns last). Full column reference: `README.md`. Stream point keys: `d, t, pace, elev, lat, lon` (+ `hr`, `cad` when present).

## Tunables

All module-level in `strava_compile.py` (as of 2026-07). Grep: `grep -n "STREAM_\|GPS_\|GEO_GAP\|INTERVAL_\|PACE_ZONE" strava_compile.py`.

| Constant | Value | Meaning |
|---|---|---|
| `STREAM_POINTS_PER_KM` | 150 | dist_stream samples per km (~every 7 m) |
| `STREAM_MAX_POINTS` | 3000 | dist_stream cap (bites past ~20 km) |
| `STREAM_PACE_WINDOW_M` | 50.0 | min look-ahead for instantaneous pace |
| `GEO_GAP_MAX_M` | 60.0 | wider fix gap → snap, don't interpolate a chord |
| `GPS_POINTS_PER_KM` | 150 | polyline points per km |
| `GPS_MIN_POINTS` / `GPS_MAX_POINTS` | 75 / 2500 | polyline floor / cap (cap bites past ~17 km) |

Changing any of these requires `--rebuild` (see cache semantics), but they split by what they touch and thus by how they reach the segment cache:

- `GPS_POINTS_PER_KM` / `GPS_MIN_POINTS` / `GPS_MAX_POINTS` alter `gps_polyline`, and `len(gps_polyline)` is hashed by `_runs_signature`, so after `--rebuild` the segment cache self-invalidates.
- `STREAM_POINTS_PER_KM`, `STREAM_MAX_POINTS`, `STREAM_PACE_WINDOW_M`, and `GEO_GAP_MAX_M` alter only `dist_stream`, which the signature does NOT hash. After `--rebuild` you must ALSO manually delete `cache/segments_cache.json` (or bump a params-token version), or detection silently keeps using stale stream inputs.

See `.claude/skills/caches-and-invalidation/SKILL.md`.

## Playbooks

1. **`net_elev` is 0 / climbs flat**: first measure elevation density on the newest CSV:
   ```python
   import csv, json, sys, glob
   csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
   path = sorted(glob.glob("csv_data/*_strava.csv"))[-1]
   with open(path, newline="", encoding="utf-8-sig") as f:
       for row in list(csv.DictReader(f))[-5:]:
           pts = json.loads(row["fit_distance_stream"] or "[]")
           n = sum(1 for p in pts if p.get("elev") is not None)
           print(row["fit_start_time"], len(pts), n, round(n / len(pts), 3) if pts else "-")
   ```
   Expect density ~1.0 (verified 1.0 on all recent runs, 2026-07-02). Density ≪ 1.0 means elevation regressed to exact-epoch attachment, restore the `elev_at` distance-interpolation pattern, then `python strava_compile.py --rebuild` and re-run the hub. If density is fine, the bug is downstream (segment-detection skill).
2. **`_csv.Error: field larger than field limit`**: the reader is missing `csv.field_size_limit(min(sys.maxsize, 2**31 - 1))`. Set in `lib/generate_analytics.py`, `lib/generate_dashboards.py`, and `strava_compile.py` (as of 2026-07-08): `grep -rn "field_size_limit" .`, add the same line (plus `import sys`) near the top of the failing reader. `generate_hub.py` works without calling it because it imports `load_rows` from `lib/generate_dashboards.py`, whose module-level call raises the limit as an import side effect. `strava_compile.py` now raises it too, which is what re-armed its parse cache (see above).
3. **New runs not appearing**: (a) confirm discovery picked the export you think, compile prints `Found Strava export zip: export_*.zip`, chosen by newest mtime in `--downloads`; a stale zip with a newer mtime wins. (b) Check the run survives the `--sport` filter (default `running`, substring match). (c) Check `Matched N/M activities` output, an unmatched row means its `Filename` id has no `.fit` in `<export>/activities/`. (d) Remember the output file is dated by archive mtime; the hub reads the lexicographically last `csv_data/*_strava.csv`, so an old export can write "behind" a newer-dated file.
4. **Parse slow**: run `python strava_compile.py --profile` (or `STRAVA_PROFILE=1`), prints wall time, worker count, and the 5 slowest files. Tune `--workers N` (default CPU count; 28 workers, 79 files: parse phase ~2.3 s inside a ~2.8 s total compile, 2026-07-02). One pathological .fit (slowest seen: 1.21 s) dominates when workers ≫ files remaining.

## Verification

- Full pass: `python strava_compile.py --rebuild --profile` exits 0 and prints `Rows: 79, Columns: 153` (counts grow with new runs).
- Cache line: `Reused X cached, parsed Y new`, as of 2026-07-08 a normal (non-`--rebuild`) compile reuses all previously-parsed rows and only parses new .fit files, e.g. `Reused 79 cached, parsed 0 new` when nothing changed.
- Elevation density snippet from playbook 1 prints ~1.0.
- Constants unchanged: `grep -n "STREAM_POINTS_PER_KM\|STREAM_MAX_POINTS\|GPS_MAX_POINTS" strava_compile.py`.
- End-to-end checks: `.claude/skills/validation-playbook/SKILL.md`.

## Pitfalls

- Stream elevation key is `elev`, not `e`; missing keys are omitted per-point, not nulled.
- The parse cache is live as of 2026-07-08; `Reused N cached, parsed 0 new` on an unchanged export is expected, not a sign your `--rebuild` failed to do anything. Because the cache is live, parse-logic or density edits now genuinely need `--rebuild` to reach previously-parsed rows, a cache hit silently keeps serving old values otherwise.
- Cached rows (when reuse works) are strings copied verbatim from the prior CSV, never re-derived.
- Output CSV date = export archive mtime, not run date; two compiles of the same export overwrite one file.
- `decompress_fit_gz` skips decompression when the `.fit` already exists in `--tmp`; a corrupt half-written tmp file persists until deleted.
- A 3002-point stream is normal for long runs: `STREAM_MAX_POINTS` widens spacing rather than truncating, and endpoints are always emitted.
- `--sport` is a substring match ("running" matches "Trail Running"); `--sport all` disables filtering.
- HR/cadence epoch-attachment is only safe for dense channels; copy `elev_at`, not `epoch in hr_by_t`, for anything sparse.
