---
name: caches-and-invalidation
description: Cache inventory and invalidation rules for the Strava-Analysis repo. Use when output did not change after a code or data edit, when deciding which cache file to delete (or whether deleting is safe), when segments look stale or a rebuild will not trigger, when a cache JSON is corrupt or hand-edited, or when changing constants and unsure what to invalidate. Covers all five caches - cache/segments_cache.json (keyed on _runs_signature), segment_geocode_cache.json, segment_match_cache.json, segment_stars.json, and the enriched csv_data CSV which is itself the .fit parse cache - plus the master change-type to required-action table and _prune_cache behaviour.
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- You changed code or data and the output (segments, names, drawn lines, CSV columns) did not change.
- You need to know which cache file to delete for a given change, or whether deletion is safe.
- A cache JSON looks corrupt, hand-edited, or is missing.
- You are adding a new tunable and need to know how to make the cache notice it.

## When NOT to use

Route by symptom instead:
- Detection logic itself (segment wrong shape, missing, misclassified) → `.claude/skills/segment-detection/SKILL.md`
- Nominatim/Overpass behaviour, slow network rebuilds, name quality, snapping quality → `.claude/skills/external-apis/SKILL.md`
- CSV contents, stream density, .fit parsing details → `.claude/skills/compile-and-streams/SKILL.md`
- Running the pipeline and its flags → `.claude/skills/run-and-regenerate/SKILL.md`
- General orientation → `.claude/skills/project-map/SKILL.md`

## Mental model

There are five caches. Four are JSON files under `cache/` owned by `lib/generate_segments.py`; the fifth is the enriched CSV itself, owned by `strava_compile.py`. None of them hash source code, every one can silently serve stale results after a code edit.

## Key files and functions

Path constants (as of 2026-07; grep: `grep -n "CACHE_DIR\|SEG_CACHE\|GEO_CACHE\|MATCH_CACHE\|STARS_CACHE" lib/generate_segments.py`):

| # | File | Key | Invalidated by | Notes |
|---|---|---|---|---|
| 1 | `cache/segments_cache.json` (`SEG_CACHE`) | one SHA1 from `_runs_signature` | signature mismatch, deleting the file, or `STRAVA_SEG_REBUILD=1` (`build_segments` ignores a matching cache and re-detects, then re-saves under the same signature) | `{"signature": ..., "segments": [...]}`; 73 segments as of 2026-07-02. Names and snapped polylines are BAKED IN (see Pitfalls). Force a rebuild without editing anything: `STRAVA_SEG_REBUILD=1 python generate_hub.py` or `python main.py --rebuild-segments`. |
| 2 | `cache/segment_geocode_cache.json` (`GEO_CACHE`) | `_geo_key` per segment (3 "lat,lon" points at 4 dp ≈ 11 m, joined by `\|`) | never automatically; only pruning and deletion | value = name string; tolerant reuse within `NAME_REUSE_M = 40.0` m |
| 3 | `cache/segment_match_cache.json` (`MATCH_CACHE`) | `_geo_key` | never automatically; only pruning and deletion | value = snapped `[lat,lon]` list, or `[]` = cached snap FAILURE, reused too |
| 4 | `cache/segment_stars.json` (`STARS_CACHE`) | list of user-starred geo_keys | n/a, read fresh every build by `_apply_stars`, even on a segments-cache hit | may be absent until something is starred (absent as of 2026-07-02) |
| 5 | `csv_data/YYYY-MM-DD_strava.csv` | activity id (`Filename` column) via `load_prior_fit_cache` in `strava_compile.py` | `--rebuild` only | the enriched CSV IS the .fit parse cache, and as of 2026-07-08 it is LIVE: `strava_compile.py` now raises `csv.field_size_limit(min(sys.maxsize, 2**31 - 1))` at module top, so `load_prior_fit_cache` reads the prior CSV successfully and reuses its rows verbatim (a normal compile reports "Reused 79 cached, parsed 0 new"). `--rebuild` is genuinely mandatory after parse-logic or stream/polyline-density changes, old rows otherwise keep old values silently. Full story: `.claude/skills/compile-and-streams/SKILL.md`. |

**What `_runs_signature` hashes** (verbatim body in `lib/generate_segments.py`, grep: `grep -n "_runs_signature" lib/generate_segments.py`): per run, sorted by date, the string `date_iso|dist_km|len(gps_polyline)` plus the polyline's first and last points; then one "params token" string interpolating detection constants, version tokens (`refine2`, `salvageclimb1`, ...), and `load_config().segment_anchors`. It does **NOT** hash `dist_stream` contents, elevation, or any source code. This is THE stale-cache trap: detection reads `dist_stream` (`_geo_track`), but the signature only sees `gps_polyline` length and endpoints.

**Pruning** (`_prune_cache`): whenever `_name_segments` or `_match_segments` writes cache #2 or #3, entries whose key-centroid is farther than `CACHE_PRUNE_M = 150.0` m from every current segment are dropped (anti-bloat); unparseable keys are kept untouched. Nearby orphans survive on purpose, they feed the tolerant reuse.

## The master invalidation table

| You changed... | Required action |
|---|---|
| Detection logic or any detection tunable in `lib/generate_segments.py` | Bump a version token in the `_runs_signature` params string (e.g. `salvageclimb1` → `salvageclimb2`) AND delete `cache/segments_cache.json` (belt and braces: the bump alone suffices if the constant is interpolated, but not all are). |
| GPS polyline density (`GPS_POINTS_PER_KM`, `GPS_MIN_POINTS`, `GPS_MAX_POINTS` in `strava_compile.py`) | `python strava_compile.py --rebuild`. Verified: `len(gps_polyline)` is hashed per run, so the new CSV changes the signature and the segments cache invalidates itself. |
| Stream density (`STREAM_POINTS_PER_KM`, `STREAM_MAX_POINTS`, `STREAM_PACE_WINDOW_M`, `GEO_GAP_MAX_M` in `strava_compile.py`) | `--rebuild` AND delete `cache/segments_cache.json`. Verified: `dist_stream` (what detection actually consumes) is NOT hashed; if the GPS polyline constants are untouched the signature does not change and stale segments are served. |
| Naming logic (`_derive_name`, `_name_segments`) | Delete `cache/segment_geocode_cache.json` AND force a segments rebuild (delete `cache/segments_cache.json`), names are baked into cached segments, so clearing the geocode cache alone does nothing on a signature hit. |
| Snapping logic (`_snap_poly`, `_match_segments`, match gates) | Delete `cache/segment_match_cache.json` AND force a segments rebuild, same baking reason. |
| HTML/JS/CSS only (hub rendering, `body_segments` markup) | Nothing. The hub is fully regenerated every run; just re-run `python generate_hub.py`. |
| Added new runs (fresh Strava export) | Nothing. Compile reuses cached rows for existing activity ids and parses only the new .fit files (parse cache is live, see the inventory), and the new run rows change the signature automatically. |
| `segment_anchors` in `cache/config.json` | Nothing. Verified: the params token interpolates `load_config().segment_anchors`, so the signature changes and a rebuild follows automatically. |
| Starred/unstarred a segment | Nothing. `_apply_stars` reads `segment_stars.json` fresh on every build, including cache hits. |

Wherever a row says "force a segments rebuild" or "delete `cache/segments_cache.json`", the cleaner one-shot equivalent is `STRAVA_SEG_REBUILD=1 python generate_hub.py` (or `python main.py --rebuild-segments`): it re-detects regardless of signature and re-saves the cache, no file deletion or token bump needed. The token bump is still the right move when you want the invalidation to be durable across checkouts (a re-save keeps the old signature, so a later run without the flag on a machine with a stale cache would still hit it). `--rebuild-all` does the CSV re-parse and the segment rebuild together.

## Playbooks

### 1. Output didn't change after my change

1. Which artifact is stale? Hub HTML → it can't be (regenerated every run); check you re-ran `python generate_hub.py` and are opening `dashboards/TrainingHub.html`.
2. Segments stale? Look at the console line: `Segments: loaded N from cache` = signature hit, your change was never exercised. Apply the master table row for your change type, then expect `Segments: detected N benchmark segments`.
3. CSV-derived values stale (splits, streams, best efforts)? Compile prints `Reused X cached, parsed Y new`; as of 2026-07-08 the parse cache is live, so a normal compile genuinely reuses prior rows and only new .fit files get parsed (see the inventory and `.claude/skills/compile-and-streams/SKILL.md`). Run `python strava_compile.py --rebuild` to force a full re-parse when you've changed parse logic or density constants, then check whether your change also needs a segments-cache delete (master table).
4. Names or drawn lines stale? Those live in caches #2/#3 but are also baked into cache #1, delete the relevant geo cache AND `segments_cache.json`.

### 2. Force a truly cold segment rebuild

1. Delete `cache/segments_cache.json` only → detection re-runs (a few seconds of compute) but names and snaps come from caches #2/#3, so no network. This is almost always what you want.
2. Delete all three of `segments_cache.json`, `segment_geocode_cache.json`, `segment_match_cache.json` → detection plus full re-geocode and re-snap. Expected cost: minutes, floored by Nominatim's 1 req/s policy (historically 200–500 s before tolerant reuse existed). See `.claude/skills/external-apis/SKILL.md` before doing this, it is rarely necessary.
3. Never delete `cache/config.json` (user config, not a cache) or `segment_stars.json` (user data, stars are unrecoverable).

### 3. Corrupt or hand-edited cache file

`_load_json` returns `None` on `FileNotFoundError` or `JSONDecodeError`, which every caller treats as a cold cache, so any `cache/segment*.json` (except `segment_stars.json`, which is user data) is safe to delete or truncate; it regenerates on the next `python generate_hub.py`. A corrupt `segments_cache.json` just means one detection re-run. A corrupt geocode/match cache means a network rebuild (see cost above). `_save_json` degrades gracefully too: a write failure prints `Segments: could not write ...` and the build still returns.

## Verification

- Signature fields unchanged: `grep -n "date_iso.*dist_km.*len(poly)\|params:" lib/generate_segments.py`, per-run string plus params token, no `dist_stream`.
- Prune radius: `grep -n "CACHE_PRUNE_M" lib/generate_segments.py` → `150.0` (as of 2026-07).
- Stars applied on both return paths: `grep -n "_apply_stars" lib/generate_segments.py` → cache-hit return and fresh-build return both call it.
- Parse-cache reuse: `grep -n "load_prior_fit_cache\|Reused" strava_compile.py`; the hub picks the latest CSV by sorted filename (`candidates[-1]` in `generate_hub.py` `main()`), the compile cache picks by mtime, keep dated filenames intact.
- After any invalidation: run twice; second run must print `Segments: loaded N from cache` with the same N.

## Pitfalls

- **The stale-cache trap**: `_runs_signature` hashes `date_iso|dist_km|len(gps_polyline)` + endpoints + params token, never `dist_stream`, never code. Any edit not reflected in those is silently ignored on the next run.
- **Names/snaps are baked into `segments_cache.json`.** `_name_segments`/`_match_segments` only run inside a segments rebuild; on a signature hit the cached segments (with old names and old snapped polylines) are returned as-is. Deleting cache #2 or #3 without #1 changes nothing.
- **Deleting geocode/match caches unnecessarily is expensive**: every uncached segment costs real Nominatim calls at ≥1.1 s each. Prefer deleting only `segments_cache.json`.
- **Cached `[]` in the match cache means "snap failed here, don't retry"**: the segment stays unsnapped forever until that entry (or the file) is deleted. 90 of 101 entries were `[]` as of 2026-07-02. An offline build writes fresh `[]` failures too (see external-apis Pitfalls).
- The parse cache is live as of 2026-07-08: `strava_compile.py` raises `csv.field_size_limit`, so `load_prior_fit_cache` reads the prior CSV's oversized stream cells successfully and reuses rows for activity ids it already has. Every compile is incremental (only new .fit files parsed), not a full re-parse. `--rebuild` is mandatory after parse-logic or density changes, since a cache hit copies old values verbatim. Full story: `.claude/skills/compile-and-streams/SKILL.md`.
- `_prune_cache` can legitimately shrink geocode/match caches on any dirty write; a smaller file after a build is not corruption.
