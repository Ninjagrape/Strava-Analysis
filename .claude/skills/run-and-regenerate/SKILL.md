---
name: run-and-regenerate
description: How to run the Strava-Analysis pipeline and regenerate exactly the right outputs after a change, including the "I changed X, so run Y" decision table. Use when unsure which command to run after editing code, when a change is not showing up in the browser, when deciding whether --rebuild or a cache delete is needed, when the pipeline seems slow and you need to attribute the time to a phase, or when running the project for the first time.
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- You edited something and need to know the minimal command(s) to see the effect.
- A change is not visible in `dashboards/TrainingHub.html` and you suspect you regenerated the wrong stage.
- You need runtimes to judge whether a run is abnormally slow, or to find which phase the time goes to.
- You are running the pipeline for the first time on a fresh export.

## When NOT to use

- What the compile stage actually does, `--rebuild` semantics, stream density → `.claude/skills/compile-and-streams/SKILL.md`
- Which cache file to delete and when, `_runs_signature`, params token → `.claude/skills/caches-and-invalidation/SKILL.md`
- Slow *segment* rebuilds (Nominatim/Overpass network waits) → `.claude/skills/external-apis/SKILL.md`
- Segment output wrong (not just stale) → `.claude/skills/segment-detection/SKILL.md`
- Judging whether regenerated output is *correct* → `.claude/skills/validation-playbook/SKILL.md`
- Repo orientation → `.claude/skills/project-map/SKILL.md`

## Mental model

Three entry points, two stages:

- `python main.py`, runs both stages via its `STEPS` tuple: `strava_compile.py` then `generate_hub.py`. CLI args are forwarded **only to the compile step** (`forward_args=True` for compile, `False` for hub). Special case: `main.py --profile` also sets `STRAVA_PROFILE=1` in the environment so the hub profiles too. Grep: `grep -n "STEPS\|forward_args\|STRAVA_PROFILE" main.py`.
- `python strava_compile.py [flags]`, stage 1 only: export → `csv_data/YYYY-MM-DD_strava.csv`.
- `python generate_hub.py`, stage 2 only: newest CSV → `dashboards/TrainingHub.html`, rebuilt from scratch every run. It parses **no CLI flags** (env vars only); `python generate_hub.py --profile` silently does nothing.

Expected runtimes (live run 2026-07-02, 79 runs): total ~4.5 s; compile ~2.8 s; hub ~1.7 s; segments ~0 s on a warm signature hit (prints `Segments: loaded 73 from cache`). A **cold** segment rebuild is network-bound (rate-limited Nominatim/Overpass calls, minutes not seconds), see `.claude/skills/external-apis/SKILL.md` before assuming a hang.

### strava_compile.py flags (all 9, as of 2026-07)

Grep: `grep -n "add_argument" strava_compile.py`.

| Flag | Default | Meaning |
|---|---|---|
| `--downloads` | `~/Downloads` | Folder scanned for the newest `export_*.zip` / `export_*/` |
| `--archive` | None | Skip discovery: explicit path to an export folder or .zip |
| `--csv` | None | Override path to `activities.csv` |
| `--out` | None | Override output CSV path (default `csv_data/YYYY-MM-DD_strava.csv`, dated by export mtime) |
| `--sport` | `running` | Filter: running / cycling / all (substring match) |
| `--tmp` | `<system temp>/strava_fit` | Temp dir for decompressed `.fit` files |
| `--rebuild` | off | Ignore the prior-CSV parse cache; re-parse every `.fit` |
| `--workers` | None (= CPU count) | Parallel parse workers |
| `--profile` | off | Print decompress/parse timing breakdown |

### Env toggles

- `STRAVA_PROFILE=1`, per-phase timings in both compile and hub (hub: `_build_runs`, `build_segments`, panels, HTML size). PowerShell: `$env:STRAVA_PROFILE="1"; python generate_hub.py`.
- `STRAVA_SEG_DEBUG=1`, per-segment match/reject diagnostics from `lib/generate_segments.py`.
- `STRAVA_SEG_REBUILD=1`, force a segment rebuild: `build_segments` ignores a matching `segments_cache.json` and re-detects (then re-saves). Equivalent to `python main.py --rebuild-segments`. Cleaner than hand-deleting the cache file.

Grep: `grep -rn "STRAVA_PROFILE\|STRAVA_SEG_DEBUG\|STRAVA_SEG_REBUILD" generate_hub.py lib/generate_segments.py strava_compile.py`.

## Key files and functions

| File | Invocation surface |
|---|---|
| `main.py` | `STEPS` tuple, `run_step()`; forwards `sys.argv[1:]` to compile only |
| `strava_compile.py` | `main()` with the argparse block above; honours `STRAVA_PROFILE` as well as `--profile` |
| `generate_hub.py` | `main()`; no argparse; reads the lexicographically **last** `csv_data/*_strava.csv`; writes `dashboards/TrainingHub.html` |
| `lib/generate_segments.py` | Called by the hub via `build_segments(runs)`; owns `cache/segments_cache.json` and the signature check |

## Playbooks

1. **THE DECISION TABLE, "I changed X → run Y"**:

   | I changed... | Run |
   |---|---|
   | Stream/density/parse logic in `strava_compile.py` (`STREAM_*`, `GPS_*`, best efforts, splits, intervals) | `python strava_compile.py --rebuild` then `python generate_hub.py`. Semantics of `--rebuild` and why it is mandatory: `.claude/skills/compile-and-streams/SKILL.md`. `GPS_*` edits change `len(gps_polyline)`, so segments self-invalidate via `_runs_signature`. `STREAM_*`-only edits (plus `GEO_GAP_MAX_M` and `STREAM_PACE_WINDOW_M`) change only `dist_stream`, which the signature never hashes, so you must ALSO force a segment rebuild: `STRAVA_SEG_REBUILD=1 python generate_hub.py` (or delete `cache/segments_cache.json`); the two together are `python main.py --rebuild-all`. Edge cases → `.claude/skills/caches-and-invalidation/SKILL.md` |
   | Segment-detection logic or tunables in `lib/generate_segments.py` | Force a rebuild: `python main.py --rebuild-segments` (or `STRAVA_SEG_REBUILD=1 python generate_hub.py`), which re-detects regardless of signature and re-saves the cache. For a durable invalidation (so future runs without the flag do not serve stale results, e.g. on another checkout), ALSO bump a version token in the `_runs_signature` params string. The signature does not hash `dist_stream`, so relying on it alone serves stale results |
   | Hub HTML/JS/CSS in `generate_hub.py` | `python generate_hub.py` only (~1.7 s; the HTML is fully regenerated every run) |
   | Analytics or dashboards formulas in `lib/generate_analytics.py` / `lib/generate_dashboards.py` | `python generate_hub.py` only |
   | Nothing, new Strava export downloaded | `python main.py` (full pipeline; discovery picks the newest-mtime export in `~/Downloads`) |

2. **See my change in the browser**: run the command(s) from the decision table, then open `dashboards/TrainingHub.html` directly (double-click / `file://`, it is a single self-contained file, no server). Hard-refresh (Ctrl+F5) if the browser cached the old file. The page itself works offline, but Leaflet map tiles are fetched live, so maps need internet. If the change still is not visible, suspect a stale cache (`.claude/skills/caches-and-invalidation/SKILL.md`), then verify you edited the stage that owns the behaviour (`.claude/skills/project-map/SKILL.md`).
3. **Which phase is slow**: run `python main.py --profile` and read the brackets: `[time] Compile Strava export: ...` vs `[time] Generate hub ...`, plus `[profile]` lines inside each. Compile slow → `--workers`, slowest-file list, `.claude/skills/compile-and-streams/SKILL.md`. Hub slow with `build_segments` dominating and cache-miss prints → network-bound cold rebuild, `.claude/skills/external-apis/SKILL.md`. Anything wildly above the reference runtimes on ~79 runs deserves investigation, not patience.
4. **First run on a new machine**: `pip install fitparse` (hard dependency; compile exits without it; `timezonefinder` optional), put the Strava `export_*.zip` in `~/Downloads`, run `python main.py`. Optional user config: copy `config.example.json` to `cache/config.json`.

## Verification

- Pipeline green: `python main.py --profile` exits 0 and ends with `All done in ~4.5s.` (79 runs, as of 2026-07-02); compile prints `Rows: 79, Columns: 153`, hub prints `Loaded 79 runs` and `Segments: loaded 73 from cache` on a warm hit.
- Forwarding still true: `grep -n "MAIN_ONLY_FLAGS\|compile_args\|steps = " main.py`, exactly two steps; orchestration-only flags (`--rebuild-segments`, `--rebuild-all`) are stripped from `compile_args`, and `--rebuild-all` appends `--rebuild` for compile.
- Hub still env-only: `grep -n "argparse\|add_argument" generate_hub.py` returns nothing (the hub takes no CLI flags; `main.py` passes it no args and drives it via env).
- Output freshness: check `dashboards/TrainingHub.html` mtime changed after the run.
- Whether the regenerated output is *correct*: `.claude/skills/validation-playbook/SKILL.md`.

## Pitfalls

- `python generate_hub.py --profile` does nothing, the hub reads env vars only; use `STRAVA_PROFILE=1` (or run via `main.py --profile`, which sets it for you).
- `python main.py` forwards compile flags to compile (`main.py --rebuild --workers 4`) and translates its own `--rebuild-segments` / `--rebuild-all` into `STRAVA_SEG_REBUILD` for the hub. A bare compile flag still cannot otherwise reach the hub; use the env var or the two orchestration flags.
- `Reused N cached, parsed 0 new` from compile is the normal warm state as of 2026-07-08 (the parse cache is live). `Reused 0 cached, parsed N new` means a real re-parse: either `--rebuild`, or the first compile after adding new runs. Details in `.claude/skills/compile-and-streams/SKILL.md`.
- `Segments: loaded N from cache` after a detection edit means your change was NOT applied, signature hit on stale params; see decision-table row 2.
- The hub picks the lexicographically last `csv_data/*_strava.csv`; dated names sort correctly, but a hand-named `--out` file (e.g. `test_strava.csv`) can shadow or hide the real newest data.
- Compiling an old export writes a file dated by that export's mtime, which can sort *behind* an existing newer-dated CSV, the hub will then silently ignore it.
- Cold segment rebuilds sleep deliberately (~1.1 s per real Nominatim call); do not kill the run or "optimise" the sleeps, see `.claude/skills/external-apis/SKILL.md`.
- The `Detecting benchmark segments…` ellipsis renders as mojibake in the Windows console; cosmetic only.
