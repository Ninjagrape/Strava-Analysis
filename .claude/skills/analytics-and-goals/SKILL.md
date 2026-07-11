---
name: analytics-and-goals
description: Debugging and extending the Strava-Analysis training-analytics numbers. Use when CTL/ATL/TSB, ACWR, monotony/strain, critical speed, VDOT, pace-zone distribution, Riegel predictions, training targets, goal gaps, or best-effort tables look wrong (spikes, zeros, missing months, implausible paces); when adding a new analytics metric; when the Analytics, Best Efforts, Goals, or Overview panels disagree with each other; or when a CSV reader dies with "field larger than field limit".
---
> Values in this skill are snapshots (as of 2026-07), re-verify with the grep recipes before relying on them.

## When to use

- A fitness/fatigue number (CTL, ATL, TSB, ACWR, monotony, strain) looks wrong or spikes.
- Critical speed, VDOT, pace-zone split, Riegel prediction, training target, or goal gap is implausible.
- Best-efforts podium shows the wrong run, pace, or grade-adjusted time.
- You are adding a metric to the Analytics/Goals panels or refactoring the math modules.

## When NOT to use

Route by symptom instead:
- Chart rendering, SVG/JS, tabs, layout of TrainingHub.html → `.claude/skills/hub-ui/SKILL.md`
- Input data wrong (missing `best_*_s` columns, bad splits, stream density, elevation) → `.claude/skills/compile-and-streams/SKILL.md`
- Verifying a change end-to-end (baselines, no automated tests exist) → `.claude/skills/validation-playbook/SKILL.md`
- Segment efforts/PRs → `.claude/skills/segment-detection/SKILL.md`

## Mental model

Two `lib/` modules compute every "derived number", both reading the same enriched CSV rows (list of string-keyed dicts from `csv.DictReader`):

1. **`lib/generate_analytics.py`, longitudinal fitness.** Per-session pace-based load (`session_loads`) → gap-filled daily series (`daily_series`) → exponentially smoothed CTL/ATL/TSB (`ctl_atl_tsb`), ACWR (`acwr_series`), Foster monotony/strain (`monotony_strain`); plus critical-speed model (`fit_critical_speed`), CS-anchored pace zones, monthly VDOT trend (`vdot_trend`), cadence trend, calendar heatmap. Deliberately HR-free: the source device records no heart rate or power, so nothing in this module reads an HR column, load is grade-adjusted distance × intensity². There is no "HR-less degradation path" because HR is never an input (as of 2026-07; `rg -n "heart" lib/generate_analytics.py` returns only comment, docstring, and UI-text hits, no code path reads an HR value).
2. **`lib/generate_dashboards.py`, per-distance performance.** Best-effort podiums per distance band (`top3_for_band` over `DISTANCE_BANDS`), Riegel race predictions (`riegel`, `fit_riegel`, `_predict_race_time`), training-target pace ranges (`derive_training_target`), race-goal cards from `cache/config.json`, weekly mileage.

Both are imported by `generate_hub.py` and embedded as tab bodies (`body_analytics`, `body_best_efforts`, `body_goal_dashboard`, `overview_sections`); each also has a standalone `main()`.

As of 2026-07-10 the Analytics panel is split into themed sub-tabs (Injury risk / Paces / Trends, `setSubTab` in `generate()`), and `body_analytics(rows, updated, runs=None)` takes the hub's classified runs list to render `pace_progression_section(runs)`: per-run grade-adjusted pace (`ga_time(moving_s, gain, dist_km)/dist_km`) bucketed by `run_type` (types with ≥3 runs, `misc` excluded), scattered over real-date x with a trailing 5-run mean trend (`_rolling_trend`, `_pace_prog_chart`; y inverted so faster is up). The type selector reuses `_ranged`/`setRange`; colours mirror the hub's `RUN_TYPE_COLOR`. The standalone `main()` passes no runs, so that build simply omits the section. Because classification is era-relative (`_threshold_at`), sparse types can show a "current" rolling pace that is months old and slower than a denser type's — that is data sparsity, not a bug.

**Three live "threshold" definitions plus one legacy, do not conflate them (as of 2026-07):**
- **Hub threshold speed** (`_compute_threshold` in `generate_hub.py`): as of 2026-07-09 this delegates to `robust_threshold_mps(rows)` in `lib/generate_analytics.py`, the 5K-equivalent read off a multi-point grade-adjusted Riegel fit (`fit_current_curve`, `fit_riegel` over GA bests at `_CURVE_FIT_DISTS` = 1 km/1 mi/5 k/10 k/15 k/half), falling back to the single raw best 5K only when under 2 points fit. This replaced the old single-`5000/best_5k_s` anchor so one downhill/one-off 5K no longer skews classification. `_threshold_at` still gives each run a contemporaneous value, but now by scaling that robust anchor by the run era's fastest-5K vs the all-time-best-5K ratio (`THRESHOLD_WINDOW_DAYS = 60`, widening ×2/×4/×8), so recent runs get exactly the anchor and older/less-fit eras a proportionally slower one. Feeds `_compute_pace_zones` (speed-fraction bounds `[0.77, 0.87, 0.93, 1.03]`) whose zone shares drive `_classify_run` (race/threshold/tempo/long/recovery/easy). This fitted-curve proxy is the shared linchpin of run classification, and the Analytics "Current paces" card renders the same curve.
- **Analytics load threshold** (`session_loads`): the 15th-percentile (fast-end) grade-adjusted pace across all runs, not the best 5K.
- **Analytics display zones** (`pace_zones_from_cs`): anchored to fitted critical speed, not to either of the above.
- **Legacy compile-side zones** (`PACE_ZONE_THRESHOLD_MPS` feeding `_pace_zone_secs` in `strava_compile.py`): writes the `fit_pace_zone_secs` column, which nothing in `generate_hub.py` currently reads (as of 2026-07, verified by grep); superseded by the hub-side zones above.

**Grade adjustment** underpins nearly everything: `minetti_cost(g)` is the Minetti metabolic-cost quintic `155.4g⁵ − 30.4g⁴ − 43.3g³ + 46.3g² + 19.5g + 3.6`; `ga_time(raw_s, gain, dist_km)` scales a time by `COST_FLAT / minetti_cost(grade)` with `grade = (gain/2)/(dist_km*1000)` (half the one-way ascent ratio). **These were previously duplicated across the two modules and had begun to cosmetically diverge; as of 2026-07-08 they are defined once in `lib/grade.py`** (`minetti_cost`, `COST_FLAT`, `ga_time`), and both `lib/generate_analytics.py` and `lib/generate_dashboards.py` do `from grade import minetti_cost, COST_FLAT, ga_time`. Editing `lib/grade.py` now updates both callers at once; grep recipe in Verification.

## Key files and functions

| Metric | Function | Module | Formula sketch | Key constants (as of 2026-07) | Sane range |
|---|---|---|---|---|---|
| Session load | `session_loads` | analytics | `dist_km × 10 × intensity²`; intensity = threshold_pace / GA pace, clamped | clamp [0.5, 1.5]; threshold = 15th-pctile GA pace | 10 km at threshold pace ≈ 100; an easy ~10 km (6:18–6:48/km vs ~5:09/km threshold) ≈ 57–67 |
| CTL / ATL / TSB | `ctl_atl_tsb` | analytics | EWMA of daily load; TSB = prev CTL − prev ATL | k = 1−exp(−1/42) (CTL), 1−exp(−1/7) (ATL) | CTL/ATL 0–120; TSB ~−30..+20 (domain heuristic) |
| ACWR | `acwr_series` | analytics | 7-day load sum ÷ (28-day daily mean × 7) | windows 7 / 28 days | healthy ~0.8–1.3 (code band + domain heuristic) |
| Monotony / strain | `monotony_strain` | analytics | 7-day mean/SD; strain = 7-day sum × monotony | window 7 days; UI flags mono ≥1.5 amber, ≥2.0 red | monotony ~0.5–2.5 (domain heuristic) |
| Critical speed | `fit_critical_speed` | analytics | OLS on `dist = CS·t + D'` over GA bests | only points ≥ 1200 m; needs ≥ 2 | CS ~2.5–5 m/s, D' ~100–600 m, r² near 1 (domain heuristic) |
| CS pace zones | `pace_zones_from_cs` | analytics | zone edges as multiples of CS pace | 1.30, 1.155, 1.08, 1.02, 0.97, 0.85 | 5 zones |
| VDOT | `daniels_vo2max`, `vdot_trend` | analytics | `(−4.60 + 0.182258v + 0.000104v²) / pct_max`, v in m/min; monthly max over 1mi/2mi/5K/10K GA bests | pct_max = 0.8 + 0.1894393e^(−0.012778t) + 0.2989558e^(−0.1932605t), t in min | recreational ~35–60 (domain heuristic) |
| GA time | `minetti_cost`, `ga_time` | `lib/grade.py` (shared, imported by both) | `raw_s × COST_FLAT/minetti_cost((gain/2)/dist_m)` | Minetti quintic above | GA ≤ raw on net-uphill runs |
| Best efforts | `top3_for_band` | dashboards | sort `best_*_s` ascending, top 3; GA shown alongside | `is_interval`: (elapsed−moving)/elapsed ≥ 0.20 excluded unless band allows |, |
| Riegel | `riegel`, `fit_riegel` | dashboards | `t1·(d2/d1)^1.06`; fitted `T = a·D^b` via log-log OLS | b clamped [1.0, 1.15], fallback 1.06 | b 1.0–1.15 by construction |
| Training targets | `derive_training_target` | dashboards | ≥1500 m: fitted-curve pace ×0.97–1.0; reps: % of predicted 5K pace | 1 km ×0.96–0.98, 800 m ×0.93–0.96, 400 m ×0.88–0.93 | targets faster than 5K pace for reps |
| Race prediction | `_predict_race_time` | dashboards | Riegel from GA best-10K anchor; falls back to fitted curve | anchor = fastest GA 10K |, |
| Run classification | `_classify_run` | generate_hub.py | zone-share gates then distance gates | `RACE_Z5_SHARE 0.40`, `THRESHOLD_Z4_SHARE 0.30`, `TEMPO_Z34_SHARE 0.35`, `RECOVERY_Z1_SHARE 0.70`, `LONG_RUN_MIN_KM 10.0` | one label per run |

Input columns (all metrics): `Distance` (metres, despite some `dist_km`-named variables), `Moving Time`, `Elapsed Time`, `Elevation Gain`, `Activity Date` (UTC), `best_*_s`, `fit_avg_cadence`, `fit_gps_polyline` (for timezone lookup only).

## Tunables

Most numbers above are **inline literals, not module constants**, `42`/`7` in `ctl_atl_tsb`, `0.15` percentile and the `×10`/`²` in `session_loads`, `1200` in `fit_critical_speed`, `1.06` in `riegel`, the zone multipliers in `pace_zones_from_cs`, `0.20` in `is_interval`. Named tunables: `CS_DISTANCES`, `DAILY_RANGES`/`MONTHLY_RANGES`/`CAL_RANGES`, `MAX_AS_OF_DAYS = 7` (analytics); `TRAINING_DEFS`, `DISTANCE_BANDS`, `WEEKLY_RANGES`, `_RACE_BAR_HALF_WINDOW_S_PER_KM = 60.0` (dashboards); `THRESHOLD_WINDOW_DAYS = 60` and the `_classify_run` share constants (hub). No cache guards any of this, analytics/dashboards recompute from the CSV on every hub run, so edits take effect immediately (only segments are cached).

**As-of date preview (rest-day forecast, as of 2026-07):** the injury tiles and the Overview "Plan for next 7 days" card carry an optional date picker (default = latest-run date = no change). `as_of_blocks(rows)` (analytics) and `_overview_asof_blocks(runs, rows, threshold_mps)` (hub) precompute one display block per rest-day offset `0..MAX_AS_OF_DAYS` by re-running the *same* CTL/ATL/TSB/ACWR/monotony/strain and `_load_recommendation` functions over `extend_daily(base_daily, k)` (the daily series padded with `k` zero-load days). Blocks are embedded as `AS_OF_INJURY` / `AS_OF_OVERVIEW` JS arrays and swapped into the DOM on date change; **no metric math runs in the browser**. The Analytics injury charts (fitness/ACWR/strain) also update: `_asof_chart_variants(base_daily)` pre-renders each `_ranged` chart group per offset (`CHART_FITNESS`/`CHART_ACWR`/`CHART_STRAIN`), and the handler swaps the chart container HTML while preserving the active range button. The Overview history charts stay untouched. Block 0 is byte-identical to the static render (numeric fields are Python-preformatted strings to avoid JS rounding drift). Class/note logic is shared via `_tsb_class`/`_acwr_class`/`_mono_class`/`_strain_class` etc.; the Overview keeps its own terse `_ov_tsb_note`. `MAX_AS_OF_DAYS = 7` = Strava's one-packet-per-week export cap.

## Playbooks

1. **A fitness number looks wrong.** Trace: locate the function in the table → check its input columns on the suspect rows (raise `csv.field_size_limit` first, see Pitfalls) → compare against the sane range. Common causes, in order: `Elevation Gain` outlier inflating `ga_time`; a date landing on the wrong day because `timezonefinder` is not installed (UTC dates shift evening runs); a bogus `best_*_s` from stage 1 (route to compile-and-streams); the wrong "threshold" definition assumed (see Mental model).
2. **Add a new analytics metric.** Compute it as a pure function in `lib/generate_analytics.py` taking `rows` or the `daily` series (mirror `acwr_series`); sanity-check its output range in a scratchpad script against the table's style of bounds; then hand rendering to `.claude/skills/hub-ui/SKILL.md` (add a section in `generate()` and it flows into the hub via `body_analytics` automatically).
3. **Best effort missing or wrong.** `top3_for_band` only reads `best_*_s` columns, it computes nothing. Wrong/missing values are a stage-1 (compile) problem: route to `.claude/skills/compile-and-streams/SKILL.md`. Exception: a run missing from a band it should win may be the interval filter (`is_interval` ≥ 0.20 pause fraction), check `Elapsed Time` vs `Moving Time` on that row.
4. **Verify a refactor changed nothing.** From the repo root, dump key series before and after, then diff:

```python
# scratchpad/metrics_snapshot.py, run: python scratchpad/metrics_snapshot.py > before.json
import csv, json, sys
from pathlib import Path
sys.path.insert(0, "lib")
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
import generate_analytics as ga, generate_dashboards as gd
rows = ga.load_rows(sorted(Path("csv_data").glob("*_strava.csv"))[-1])
daily = ga.daily_series(ga.session_loads(rows))
pts = ga.best_effort_points(rows)
print(json.dumps({
    "n_rows": len(rows),
    "ctl_atl_tsb_last5": [{k: round(x[k], 6) for k in ("ctl", "atl", "tsb")} for x in ga.ctl_atl_tsb(daily)[-5:]],
    "acwr_last5": [round(x["acwr"], 6) for x in ga.acwr_series(daily)[-5:]],
    "strain_last": round(ga.monotony_strain(daily)[-1]["strain"], 6),
    "cs_dprime_r2": ga.fit_critical_speed(pts),
    "vdot_trend": ga.vdot_trend(rows),
    "riegel_ab": gd.fit_riegel([(d, t) for d, t in pts if d >= 1609]),
}, default=str, indent=1))
```

Re-run after the change into `after.json` and diff; any drift beyond float noise is a behaviour change. Then follow `.claude/skills/validation-playbook/SKILL.md` for the full pipeline check.

## Verification

- Function inventory still true: `rg -n "^def " lib/generate_analytics.py lib/generate_dashboards.py`
- Shared grade module, not duplicated (expect 1 hit each, both in lib/grade.py): `rg -n "^def minetti_cost|^def ga_time" lib/`
- Field-limit raise (expect exactly these 3 files as of 2026-07-08): `rg -rn "field_size_limit" .`, snapshot: `lib/generate_analytics.py`, `lib/generate_dashboards.py`, and `strava_compile.py`, all `csv.field_size_limit(min(sys.maxsize, 2**31 - 1))`
- CTL/ATL constants: `rg -n "exp\(-1 / 4?2\)|exp\(-1 / 7\)" lib/generate_analytics.py`
- Hub threshold chain: `rg -n "_compute_threshold|THRESHOLD_WINDOW_DAYS|bounds = \[0.77|_classify_run" generate_hub.py`
- Riegel clamp and exponent: `rg -n "1.06|1.15" lib/generate_dashboards.py`
- Live baseline (as of 2026-07-09): hub prints `Threshold pace (from fitness curve): 4:52/km` on 79 runs (the multi-point fitted anchor; was `from best 5K: 4:48/km` before the 2026-07-09 robust-anchor change).

## Pitfalls

- **`csv.field_size_limit`**: enriched-CSV stream cells (`fit_distance_stream` etc.) exceed the stdlib default of 128 KB per field. `lib/generate_analytics.py`, `lib/generate_dashboards.py`, and `strava_compile.py` all raise it to `min(sys.maxsize, 2**31 - 1)` immediately after importing `csv` (as of 2026-07-08), any new reader script must do the same or `csv.DictReader` raises "field larger than field limit".
- **Minetti, now shared**: `minetti_cost`/`ga_time`/`COST_FLAT` used to be duplicated in `lib/generate_analytics.py` and `lib/generate_dashboards.py` and had begun to drift cosmetically (spacing in `minetti_cost`, a docstring on only the dashboards `ga_time`). As of 2026-07-08 they live once in `lib/grade.py`, imported by both, so the drift risk is gone; a fix there now reaches the Analytics tab and the Best Efforts/Goals tabs identically. Verify with the grep above.
- **`timezonefinder` is optional**: `_get_tz_finder` catches `ImportError` and sets a `False` sentinel; `parse_date` then leaves `Activity Date` as recorded (UTC). Verified fallback: no crash, but evening runs in UTC-ahead timezones land on the previous calendar day, shifting daily load, ACWR windows, and monthly VDOT buckets. If day-boundary numbers look off, check the library is installed before debugging math.
- **Sparse-data crash**: `fit_critical_speed` returns `(None, None, None)` with fewer than 2 GA best efforts ≥ 1200 m, but `generate()` formats `{cs:.2f}`/`{dprime:.0f}`/`{r2:.3f}` unguarded, a near-empty CSV raises TypeError in the analytics panel (as of 2026-07).
- **Two Riegel paths can disagree**: the predictions table and race cards use fixed-exponent `riegel()` (b = 1.06) anchored on the GA best 10K; training targets use the fitted `(a, b)` curve. A "prediction" and a "target" at the same distance are not supposed to match exactly.
- **HR columns exist but are unused**: the CSV carries `fit_avg_heart_rate` etc., yet no analytics metric reads them, do not "fix" a load number by reaching for HR without redesigning the load model.
- **Variable-name trap**: in `session_loads`' reference-pace loop, the local `dist_km` actually holds metres (it is divided by 1000 at use). The math is correct; naive edits trusting the name are not.
- **Month labels lack years**: `vdot_trend`/`cadence_trend` label buckets with `%b` only, so multi-year data repeats "Jan", "Feb"..., the series is still correctly ordered by (year, month).
