---
name: external-apis
description: Nominatim reverse-geocoding and Overpass road-snapping in lib/generate_segments.py, plus the hub's own Nominatim caller for heatmap hotspot naming in generate_hub.py. Use when a segment rebuild is suddenly slow (minutes instead of seconds), when segment names are wrong, generic, or show raw coordinates ("Loop near -33.843, 151.193"), when heatmap hotspot chips show a bearing/distance label instead of a place name, when drawn lines ignore roads or zig-zag between parallel streets, when a build must work offline or an OSM API is down, or when touching _name_segments, _reverse_geocode, _match_segments, _snap_poly, _hotspot_name, or their caches and rate limits.
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- Segment rebuilds take minutes (network) instead of seconds (compute).
- Segment names are wrong, generic, duplicated, or raw coordinates.
- A heatmap hotspot chip shows a bearing/distance label ("7,824 km NE") instead of a place name.
- A drawn line does not follow real streets, or refuses to snap.
- You need to know what breaks (and what does not) with no network.
- You are editing naming, snapping, hotspot naming, or the caches they write.

## When NOT to use

Route by symptom instead:
- Segment shape/length/classification/effort-count wrong (pre-naming geometry) → `.claude/skills/segment-detection/SKILL.md`
- Which cache file to delete and when → `.claude/skills/caches-and-invalidation/SKILL.md`
- Running the pipeline, profiling flags → `.claude/skills/run-and-regenerate/SKILL.md`
- Orientation, jargon (geo_key, medoid) → `.claude/skills/project-map/SKILL.md`

## Mental model

After detection, `build_segments` in `lib/generate_segments.py` runs exactly two network phases, both cache-first and both designed to make zero requests on a warm cache:

1. **Naming, Nominatim** (`NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"`). `_name_segments` walks segments in order: curated name already set (anchors) → skip; exact `_geo_key` hit in `cache/segment_geocode_cache.json` → use it; a cached name whose key-centroid is within `NAME_REUSE_M = 40.0` m (`_nearest_name` over `_located_cache`) → reuse, no network; else `_derive_name` queries Nominatim for the polyline's midpoint, start, and end, composing "road climb", "feature loop", "A to B (suburb)" style labels. `_reverse_geocode` memoises points per build at `GEO_MEMO_DP = 4` decimals (~11 m) so overlapping segments share lookups, and `time.sleep(1.1)` fires only after a real network call, that ≤1 req/s pause is the Nominatim usage policy and the hard floor on cold naming.
2. **Snapping, Overpass** (`OVERPASS_URL = "https://overpass-api.de/api/interpreter"`). `_match_segments` walks segments: exact key hit in `cache/segment_match_cache.json` (including `[]` = cached failure) → done; `_nearest_match` within `MATCH_REUSE_M = 50.0` m → reuse (a non-empty snap only if it still fits the current line: 0.7–1.4× length and mean deviation ≤ `MATCH_MAX_DEV_M`; a cached `[]` is reused as-is); else network. The network path fetches ONE union bbox for all segments (`_union_bbox`, pad 0.0015°) via `_overpass_ways` (an unfiltered `way[highway]` query), builds a walk graph via `_build_walk_graph` (which is where the `MATCH_EXCLUDE` highway types are dropped), and indexes its edges into a `SNAP_INDEX_CELL_M = 60.0` m grid (`_index_edges`, 3×3-cell lookup). `_snap_poly` then resamples the drawn line every `MATCH_STEP_M = 20.0` m and projects each point onto the nearest edge within `MATCH_SNAP_M = 35.0` m (projection, not routing). If the segments span more than `SNAP_UNION_MAX_DEG = 0.6` (~65 km) in lat or lon, it falls back to per-segment `_match_polyline` fetches (one 1.0 s pause each; the shared fetch pauses once). A snap is kept only if it holds 0.7–1.4× the benchmark length, deviates ≤ `MATCH_MAX_DEV_M = 12.0` m, and adds ≤ `MATCH_MAX_TURN_GAIN = 0.06` reversals-per-point over the raw trace, otherwise the raw trace is drawn and `[]` is cached.
3. **Heatmap hotspot naming, Nominatim, new 2026-07-11, granularity reworked 2026-07-13** (`generate_hub.py`, Overview tab). `_hotspot_name(lat, lon, home_country, cache)` names every non-home heat cluster: tolerant cache lookup in `cache/heatmap_geocode_cache.json` (nearest entry within `HEAT_NAME_REUSE_KM = 8.0` km, tightened from 25.0 so suburb-level names can't bleed across neighbours) → reuse, no network; else `_reverse_geocode(lat, lon, zoom=12, lang="en")`. **Zoom is 12, not 8**: zoom 8 collapsed rural spots to their state (Copacabana NSW → "New South Wales"); at zoom 12 Nominatim returns suburb + city + state in one response, so the field-preference order alone selects the label. The order is gated on whether the cluster shares home's country (`home_country`, resolved once per build by `_home_country` and cached under the reserved `"home_country"` key, which `_hotspot_name`'s coordinate parse skips): domestic clusters use `HEAT_FIELDS_DOMESTIC` (suburb, town, village, hamlet, neighbourhood, municipality, city, county, state_district, state, country) → "Copacabana"; foreign clusters use `HEAT_FIELDS_FOREIGN` (city, town, municipality, county, state_district, state, country), skipping the suburb because a ward name where you don't live is meaningless → "Kyoto", not "Nakagyo Ward". If `home_country` is unknown (home geocode failed), every cluster takes the foreign order, the safe default. `_home_country` is only fetched when at least one remote cluster exists. On any exception, `_hotspot_name` returns None and the caller falls back to `_bearing_label` (an 8-way compass string from home, e.g. "7,824 km NE"); the failure is NOT cached, so the next online build retries and heals. Home is always labeled "Home" and never named this way. The Overview chip shows the cluster's total km, not its run count (tooltip still carries runs/km/date range). This caller shares `_reverse_geocode` with segment naming but is a distinct call site with its own cache file, own reuse radius, and its own `lang` parameter usage; segment naming does not pass `lang` and its results are unaffected. The 1.1 s politeness sleep after every real Nominatim call governs this caller too, there is no separate rate limit.

**Concurrency (verified in `build_segments`)**: the two phases run in a `ThreadPoolExecutor(max_workers=2)` because they hit different hosts, so their politeness sleeps overlap in wall time. Before submitting, `build_segments` snapshots every polyline (`orig_polys = {id(s): list(s["polyline"]) ...}`) and passes it to both functions; the match thread overwrites `s["polyline"]` while the name thread reads only the snapshot, so keys, centroids, and names cannot race. `_disambiguate_names` runs after both threads join.

**Why location-tolerant reuse exists**: a segment's drawn line comes from its medoid effort, and adding one run can change which effort is the medoid, shifting the polyline beyond `_geo_key`'s ~11 m rounding, so exact keys structurally miss between rebuilds. `_located_cache` re-reads every cached entry as (centroid, value) and the `NAME_REUSE_M`/`MATCH_REUSE_M` lookups check those before any network call; `_prune_cache` keeps nearby orphaned keys alive precisely because they feed this. Before this existed, every rebuild re-queried drifted segments and took 200–500 s (fixed 2026-06-30). Today a signature cache hit is ~0.00 s and the whole pipeline runs in ~4.5 s (measured 2026-07-02); UNVERIFIED: warm-cache detection recompute floor ~5–6 s, per repo memory notes.

## Key files and functions

All in `lib/generate_segments.py` (as of 2026-07):

| Function | Role |
|---|---|
| `_name_segments`, `_derive_name`, `_reverse_geocode` | Naming phase; per-build memo; label composition |
| `_nearest_name`, `_located_cache`, `_fallback_name`, `_disambiguate_names` | Tolerant reuse, offline fallback, duplicate-name suffixing |
| `_match_segments`, `_nearest_match` | Snapping phase orchestration and tolerant reuse |
| `_union_bbox`, `_bbox_span_ok`, `_overpass_ways`, `_build_walk_graph` | Shared fetch and walk graph |
| `_index_edges`, `_snap_poly`, `_match_polyline` | Edge grid, point-to-edge snapping, per-segment fallback |
| `_prune_cache`, `_load_json`, `_save_json` | Cache hygiene and IO (details: caches-and-invalidation) |

Constants grep: `grep -n "NOMINATIM_URL\|OVERPASS_URL\|USER_AGENT\|NAME_REUSE_M\|MATCH_REUSE_M\|GEO_MEMO_DP\|SNAP_INDEX_CELL_M\|SNAP_UNION_MAX_DEG\|MATCH_SNAP_M\|MATCH_STEP_M\|MATCH_MAX_DEV_M\|MATCH_MAX_TURN_GAIN" lib/generate_segments.py`. Requests send `USER_AGENT = "Strava-Analysis-Hub/1.0 (personal training dashboard)"`.

## Playbooks

### 1. Segment rebuild suddenly slow

1. Confirm it is a rebuild at all: `Segments: loaded N from cache` means no network ran, the slowness is elsewhere.
2. Run with `STRAVA_PROFILE=1` (or `python main.py --profile`) and read the `build_segments/name+match` lap. Compute laps slow instead → segment-detection skill.
3. Slow name+match means cache misses. Count them: compare segment count vs entries in `cache/segment_geocode_cache.json` / `segment_match_cache.json` (101 keys each as of 2026-07-02, 73 segments). Many brand-new segments (detection change, new area) legitimately need network at ~1.1 s per uncached Nominatim point.
4. Misses on OLD segments mean drift beyond the reuse radii, check whether something moved polylines wholesale (density change, medoid logic change). Bumping `NAME_REUSE_M`/`MATCH_REUSE_M` is a last resort; understand the drift first.
5. Reference timings (2026-07-02): warm signature hit 0.00 s; whole pipeline 4.5 s.

### 2. Segment names wrong or generic

1. `"Loop near -33.843, 151.193"` style = `_fallback_name`: the Nominatim lookup failed AND no cached name sat within 120 m. Check network, then rebuild (failures are NOT written to the geocode cache, so the next segments rebuild retries).
2. Wrong-but-real name: inspect `cache/segment_geocode_cache.json` (key = 3 rounded "lat,lon" points joined by `|`, value = name). A bad entry can be reused for 40 m around it. Delete that entry (or the file) AND `cache/segments_cache.json`, names are baked into cached segments (see caches-and-invalidation).
3. `"Name · 1.2 km"` suffixes come from `_disambiguate_names` on duplicate names, cosmetic, not a cache bug.
4. Naming rules live in `_derive_name`: loops prefer a leisure/park feature name from the midpoint, climbs prefer the road, point-to-points use "first road to last road", suburb appended in parens.

### 3. No network / API down

- Everything except naming and snapping works offline: detection, efforts, hub HTML are pure compute; a signature cache hit never touches the network at all.
- Naming degrades per segment to the nearest cached name within 120 m, else `_fallback_name` coordinates. Failures are not cached, names heal on the next online rebuild.
- Snapping degrades to drawing the raw GPS trace, but `_match_segments` caches `[]` for each segment it failed, and cached `[]` is trusted forever. An offline REBUILD therefore poisons the match cache: those segments stay unsnapped on every future build until their entries (or the file, plus `segments_cache.json`) are deleted. Avoid forcing a segments rebuild while offline.

### 4. Faster cold rebuild?

## Third caller: Open-Meteo elevation (`lib/elevation.py`)

`backfill_elevation(runs)` runs in the hub between `_build_runs` and `build_segments`, and only for runs that have a GPS track and **no altitude at all** (the Mi Fitness / Zepp band records none: no `altitude` field on any record, session ascent/descent 0). It reproduces what Strava does server-side, looking the route up against a DEM.

- Endpoint `https://api.open-meteo.com/v1/elevation`, Copernicus GLO-90, no key for non-commercial use, attribution Copernicus / Open-Meteo (`https://doi.org/10.5270/ESA-c5d3d65`). Response is `{"elevation": [...]}` in request order.
- **Billing is per coordinate, not per request**: 600 coordinates across six `DEM_BATCH`=100 calls tripped a 429 immediately (2026-08-02). `DEM_SLEEP_S` = 12 s between batches (~500 coords/min) and `_request` waits a 429 out (`Retry-After`, else `DEM_RETRY_S`, `DEM_RETRIES` attempts). Do not shorten these.
- Anchored lookups, not per point: `DEM_ANCHOR_M` = 50 m along the route (two samples per 90 m cell), interpolated across stream points by cumulative distance the way `elev_at` does. The cache key is the rounded coordinate itself (`DEM_KEY_DP` = 4, ~11 m) so key and request can never disagree.
- `DEM_MIN_COVER` = 0.80: a route whose anchors only partly resolved is refused outright rather than straight-lined through a hill. `DEM_SMOOTH_W` = 7 anchors: unsmoothed cell-boundary jitter invented 68 m of gain on a route Strava scores at 2 m; smoothed, the five affected runs land at 171/73/58 m against Strava's 187/95/61 m.
- Fully degradable: any failure leaves the run elevation-less and the build continues. `STRAVA_DEM_OFF=1` skips the network and uses `cache/elevation_dem_cache.json` alone.

The cold floor is Nominatim's ToS: ≤1 req/s, enforced by `time.sleep(1.1)` after each real call. Do not lower the sleep, batch differently to exceed 1 req/s, or rotate identifiers, never violate rate limits. Legitimate levers, all already implemented: the per-build memo (`GEO_MEMO_DP`), tolerant reuse (`NAME_REUSE_M`/`MATCH_REUSE_M`), the single union Overpass fetch, and running both phases concurrently. The practical answer is: keep the geocode/match caches, and only ever delete `cache/segments_cache.json` (caches-and-invalidation, playbook 2).

## Verification

- Sleep sites: `grep -n "time.sleep" lib/generate_segments.py` → exactly three (1.1 s Nominatim; 1.0 s shared Overpass fetch; 1.0 s per-segment fallback), all after real network calls only.
- Snapshot rule intact: `grep -n "orig_polys" lib/generate_segments.py` → built before the executor, passed to both `_name_segments` and `_match_segments`, both of which read `polys[id(s)]` for keys/centroids.
- Cached-failure semantics: `grep -n "matched or \[\]" lib/generate_segments.py` and the `if cache[key]:` guard in `_match_segments`, `[]` is stored and skipped without retry.
- After a naming/snapping change: force a segments rebuild, run twice; second run must print `Segments: loaded N from cache` and make zero network calls (name+match lap ≈ 0 under `STRAVA_PROFILE=1`).

## Pitfalls

- **Polyline snapshot rule**: anything added to the name+match phase that reads `s["polyline"]` must read the `orig_polys` snapshot instead, the match thread overwrites `s["polyline"]` concurrently, and even single-threaded, post-snap lines would change `_geo_key`s.
- **Cached `[]` means "never retry"**: a segment with an empty match-cache entry stays unsnapped until that entry is deleted (verified: exact-key hit with falsy value `continue`s, and `_nearest_match` returns a nearby `[]` as-is). 90 of 101 match entries were `[]` on 2026-07-02, mostly legitimate (snap rejected by the quality gates), not errors.
- Deleting geocode/match caches without also deleting `segments_cache.json` changes nothing: naming and snapping only run inside a segments rebuild, and their results are baked into the segments cache.
- Nominatim failures are per-point but `_derive_name` aborts the whole segment's label on the first failed point (returns `None`), one flaky call can genericise a name; it self-heals next rebuild because name failures are not cached.
- The shared Overpass fetch is lazy: builds where every segment is cached or reused skip it entirely, do not "warm" it eagerly, that adds a pointless area download.
- `_geo_key` rounds to 4 dp (~11 m): hand-computing keys with more precision will never hit the cache.
