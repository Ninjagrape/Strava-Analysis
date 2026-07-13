---
name: validation-playbook
description: Manual validation playbook for the Strava-Analysis repo, which has no automated tests. Encodes the departing engineer's regenerate-compare-eyeball procedure that proves a change broke nothing. Use when verifying, checking, or testing a change before declaring it done, when running regression checks after editing segment detection, compile/streams, hub UI, or analytics code, when confirming two builds are deterministic or byte-identical, or when deciding which validation steps a given change type requires.
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- You just changed code anywhere in the pipeline and must prove nothing regressed before saying "done".
- You need a regression check after editing detection constants, stream computation, hub HTML/JS, or analytics formulas.
- You want to know whether a refactor was truly behaviour-preserving (byte-diff procedure below).
- Counts changed (segments, efforts, rows) and you must decide whether that is growth or breakage.

## When NOT to use

Route by symptom instead:
- Something is already known-broken in segment detection → `.claude/skills/segment-detection/SKILL.md`.
- Output did not change after an edit, or unsure which cache to delete → `.claude/skills/caches-and-invalidation/SKILL.md`.
- Wrong raw data (distance, elevation, splits) in the CSV → `.claude/skills/compile-and-streams/SKILL.md`.
- No context on the repo at all → `.claude/skills/project-map/SKILL.md` first.

## Mental model

There are no automated tests. Validation is: **regenerate, compare invariants and baselines, eyeball the HTML.**

Two kinds of check, do not confuse them:

1. **Durable invariants**, hold regardless of how much data has accumulated:
   (a) *Determinism*: two consecutive builds on identical inputs produce the identical segment set, and (same calendar day) a byte-identical `dashboards/TrainingHub.html`, verified live 2026-07-02, two consecutive `python generate_hub.py` runs produced identical SHA256 hashes.
   (b) *Stream elevation density* ~1.0 (every stream point carries an `elev` value).
   (c) *Analytics in plausible ranges* (CTL/ATL/TSB tens not thousands, VDOT ~30-70, paces ~3-8 min/km).
   (d) *Clean pipeline run*: `python main.py --profile` exits 0, no tracebacks or warnings.

2. **Snapshot baselines**, exact values true on 2026-07-02 for a GROWING dataset (79 runs then). 73 segments and 29 Colonnade efforts WILL grow as runs are added. Use them as "same or explainably larger", never as fixed expected values. Re-derive with the snippets in Baselines before trusting any number here.

**Byte-diffability (verified 2026-07-02, code read + live double-build):** the hub HTML embeds no run timestamp, the "updated" stamp is parsed from the CSV *filename* date in `main()` of `generate_hub.py` (`csv_path.stem.split("_")[0]`), so identical inputs give identical HTML. Two wall-clock dependencies exist: `datetime.today()` in `_overview_stats` (the "this week" window) and a `datetime.today()` fallback for the stamp only when the CSV filename has no date. Consequence: byte-diffing is valid **within one calendar day**; across midnight the this-week stats legitimately shift. The enriched CSV is likewise deterministic (rows from `sorted(...glob("*.fit.gz"))`, no timestamps written; its *filename* date comes from the export archive's mtime).

**External oracle:** Strava's own website is ground truth for segment times and best efforts, open the same run on strava.com and compare splits/times. Caveat: Strava has runs newer than the last local export, so Strava-side totals and effort counts may legitimately exceed local ones.

## Key files and functions

| What | Where |
|---|---|
| Pipeline entry (runs both stages) | `main.py`, `python main.py --profile` (forwards flags only to compile; `--profile` sets `STRAVA_PROFILE=1`) |
| Stage 1 output = parse cache | `csv_data/YYYY-MM-DD_strava.csv` (latest file wins; `--rebuild` re-parses all .fit) |
| Stage 2 output | `dashboards/TrainingHub.html`, written once via `out.write_text(html, encoding="utf-8")` in `generate_hub.py` `main()` |
| Segment results + signature guard | `cache/segments_cache.json`, dict with keys `signature`, `segments` (as of 2026-07-02) |
| Params token to bump on detection changes | `_runs_signature` in `lib/generate_segments.py` |
| Analytics functions for before/after diffs | `session_loads`, `daily_series`, `ctl_atl_tsb` in `lib/generate_analytics.py` (called this way by `_overview_stats`, the Overview tab, in `generate_hub.py`); `vdot_trend` is called separately by `generate()` in `lib/generate_analytics.py` (Analytics tab, reached via `body_analytics()`) |

## Baselines

All measured 2026-07-02 on 79 runs. Re-derive before relying on any of them.

| Baseline | Value (as of 2026-07-02) | Re-derive |
|---|---|---|
| Runs / enriched CSV shape | 79 rows x 153 columns | snippet B below |
| Total segments in cache | 73 | snippet A below |
| Colonnade loop | `The Colonnade loop (Waverton)`, type loop, 392 m, **29** efforts | snippet A below |
| Stream elevation density | exactly 1.0 on the latest run | snippet B below |
| Pipeline runtime | ~4.5 s total (compile ~2.8 s, hub ~1.7 s) | `python main.py --profile`, read the `[time]` lines |
| Hub HTML size | ~11.5 MB (as of 2026-07-11, 85 runs; was ~9.0 MB on 2026-07-02 at 79 runs — the heatmap revamp that day also re-based this number: heat points now serialise at 5 dp, so size moved for reasons beyond run growth) | `ls -l dashboards/TrainingHub.html` (or the `[profile] HTML size` line) |
| Heatmap clusters / points | 1 cluster, 39,264 points (as of 2026-07-11, 85 runs; heatmap phase ~0.14 s). Cluster count grows only when runs exist > `HEAT_CLUSTER_KM` (30 km) apart; a new remote cluster also adds one ~1.1 s Nominatim call on its first online build | `STRAVA_PROFILE=1 python generate_hub.py`, read the `[profile] heatmap` line |

**Snippet A, segment count + Colonnade (run from repo root, bash):**

```bash
python - <<'PY'
import json
segs = json.load(open("cache/segments_cache.json", encoding="utf-8"))["segments"]
print(len(segs), "segments")
for s in segs:
    if "olonnade" in s["name"]:
        print(s["name"], "|", s["type"], "|", s["length_m"], "m |", len(s["efforts"]), "efforts")
PY
```

**Snippet B, CSV shape + stream elevation density (field-size limit is mandatory):**

```bash
python - <<'PY'
import csv, json, sys, glob
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))   # stream cells exceed the default limit
path = sorted(glob.glob("csv_data/*_strava.csv"))[-1]
rows = list(csv.DictReader(open(path, encoding="utf-8")))
print(path, "->", len(rows), "rows x", len(rows[0]), "cols")
pts = json.loads(rows[-1]["fit_distance_stream"] or "[]")
ok = sum(1 for p in pts if p.get("elev") is not None)   # key is 'elev', not 'e'
print("elev density:", (ok / len(pts)) if pts else "n/a", f"({ok}/{len(pts)} points)")
PY
```

**Snippet C, dump the full segment list to a file (for before/after diffs):**

```bash
python - <<'PY'
import json
segs = json.load(open("cache/segments_cache.json", encoding="utf-8"))["segments"]
for s in sorted(segs, key=lambda s: s["name"]):
    print(f'{s["type"]:<8} {s["length_m"]:>6} m {len(s["efforts"]):>4} efforts  {s["name"]}')
PY
```

## Playbooks

Pick the checklist for what you changed. Every checklist ends with a clean `python main.py --profile` run.

**1. Changed segment detection (`lib/generate_segments.py` logic or constants)**
1. Bump a version token in the `_runs_signature` params string AND delete `cache/segments_cache.json` (rules: `.claude/skills/caches-and-invalidation/SKILL.md`).
2. Dump the pre-change segment list (snippet C, from a stash or the old cache) to the scratchpad.
3. Rebuild (`python generate_hub.py`); expect `Segments: detected N benchmark segments`.
4. Delete the cache and rebuild AGAIN: the segment list must be identical both times (determinism is the invariant, not any fixed N).
5. Diff the new snippet-C dump against the pre-change dump. Explain every changed line intentionally, a segment appearing, vanishing, or changing type/length/efforts must be exactly what your change was supposed to do.
6. Confirm the Colonnade loop is present with a plausible effort count (29 as of 2026-07-02; equal or higher later).
7. Open the hub's Segments tab and spot-check 2-3 segment geometries on the map (lines follow streets, loops close, climbs point uphill).

**2. Changed compile / streams (`strava_compile.py`)**
1. Run `python main.py --rebuild` (the enriched CSV is the parse cache; without `--rebuild` old rows keep old values silently).
2. Snippet B: elevation density still ~1.0; row count unchanged (79 as of 2026-07-02) unless new runs were exported; column count/list unchanged (153 as of 2026-07-02) unless your change intentionally adds columns.
3. Spot-check one run's km splits and best efforts against the same activity on strava.com (external oracle; small differences in smoothing are normal, order-of-magnitude differences are bugs).

**3. Changed hub UI (`generate_hub.py` HTML/JS/CSS, tab layout)**
1. Regenerate: `python generate_hub.py` (~1.7 s as of 2026-07-02; the hub is always rebuilt from scratch).
2. Open `dashboards/TrainingHub.html` in a browser. All 6 tabs render: Overview, Best Efforts, Goals, Analytics, Runs, Segments.
3. Devtools console shows zero JS errors on load and while clicking through each tab.
4. Open one run's detail view and one segment's detail view; both populate.

**4. Changed analytics / dashboards formulas (`lib/generate_analytics.py`, `lib/generate_dashboards.py`)**
1. BEFORE changing code, dump reference series to the scratchpad:
```bash
python - <<'PY'
import sys; sys.path.insert(0, "lib")
import csv, glob
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
from generate_analytics import session_loads, daily_series, ctl_atl_tsb, vdot_trend
rows = list(csv.DictReader(open(sorted(glob.glob("csv_data/*_strava.csv"))[-1], encoding="utf-8")))
for d in ctl_atl_tsb(daily_series(session_loads(rows)))[-14:]: print(d)
for v in vdot_trend(rows)[-5:]: print(v)
for c in ("best_5k_s", "best_10k_s"):
    print(c, sorted(float(r[c]) for r in rows if r.get(c))[:3])
PY
```
2. After the change, rerun the same dump and diff. Series must be unchanged except where your formula change intentionally moves them, and moved values must stay plausible (invariant c).
3. `minetti_cost`/`ga_time`/`COST_FLAT` live once in `lib/grade.py` (shared by both lib modules since 2026-07-08); a change there must move both dumps identically.

**5. Pure refactor (no behaviour change intended)**
1. Build the artifact BEFORE the refactor and record its hash (PowerShell: `Get-FileHash dashboards\TrainingHub.html`).
2. Apply the refactor, rebuild on the same inputs, hash again. **The HTML is byte-diffable** (verified 2026-07-02: identical SHA256 across two consecutive builds), hashes must match exactly, provided both builds run on the same calendar day (the this-week window in `_overview_stats` uses `datetime.today()`). If a compile-stage refactor: also `--rebuild` and byte-compare the enriched CSV the same way.
3. Any hash mismatch means the refactor changed behaviour: `git diff --no-index old.html new.html` to find where, and either fix or reclassify the change as behavioural (then use the matching playbook above).
4. `python main.py --profile` exits 0 with runtimes in the normal band (~4.5 s total as of 2026-07-02; a 10x jump is a regression even if output matches).

## Verification

1. Determinism claim still true: build twice, compare, `python generate_hub.py; Get-FileHash dashboards\TrainingHub.html` (twice, same day) must give equal hashes.
2. "Updated" stamp still filename-derived (not wall clock): `grep -n "csv_path.stem.split" generate_hub.py` and `grep -n "datetime.today" generate_hub.py`, today() should appear only in `_overview_stats` and the filename-fallback branch.
3. Baseline numbers current: rerun snippets A and B; update stamped values here if they have grown.
4. Cache shape unchanged: snippet A fails loudly if `segments_cache.json` no longer has the `signature`/`segments` structure.

## Pitfalls

- **Validating against a stale cache is the #1 false positive.** `Segments: loaded N from cache` means your detection change was NOT exercised. Bump the params token and delete `cache/segments_cache.json` first, full rules in `.claude/skills/caches-and-invalidation/SKILL.md`. Same trap in stage 1: without `--rebuild`, old CSV rows keep old values.
- **Absolute baselines rot.** 73 segments / 29 Colonnade efforts / 79 runs were true on 2026-07-02 and grow with every export. Months later, "more than baseline" is expected; "fewer than baseline" is the red flag.
- **Skipping the second determinism build.** One successful build proves nothing about flakiness; the delete-and-rebuild-again step in playbook 1 is what catches nondeterministic detection.
- **Blank maps offline are not a bug.** Leaflet tiles load from the internet; without a connection the Segments/Runs maps render grey. Judge geometry only when online.
- **Byte-diff across midnight fails legitimately** (this-week window). Rerun both builds on the same day before concluding a refactor changed behaviour.
- **Reading the enriched CSV without raising `csv.field_size_limit`** throws "field larger than field limit". Every reader snippet here includes the raise; keep it when adapting them.
