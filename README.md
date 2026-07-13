# Strava Analysis

Processes a Strava data export into an enriched CSV, merging `activities.csv` metadata with
per-second record data parsed from each activity's `.fit.gz` file. Computes best efforts
and detects interval reps from the raw GPS/distance stream, then builds a self-contained
training hub (`dashboards/TrainingHub.html`) with best-efforts, goals, analytics, an all-time GPS heatmap that clusters your runs into named hotspots you can jump between, auto-detected benchmark segments, and a per-run browser that classifies each run
by training type and can be searched, sorted, and filtered by stats. Each run has its Strava description, a route
map, per-km splits (with elevation gain/loss and cadence), and an over-distance
pace/elevation/heart-rate/cadence profile that shows the exact distance and value as you
hover along it. Clicking a route opens an expanded view where tracing the graph also moves
a marker along the map.

## Requirements

Python 3.10+ and one third-party library:

```bash
pip install fitparse
```

`timezonefinder` is an optional extra. Install it (`pip install timezonefinder`) and each run's dates and times are resolved to the activity's own local timezone from its GPS start point; without it, dates fall back to UTC.

## Usage

Drop a Strava export zip (`export_*.zip`) into your Downloads folder, then run:

```bash
python main.py
```

This runs both steps in sequence — compile the export, then generate the hub — and writes all output to `csv_data/` and `dashboards/`. It exits early with an error if any step fails. When it finishes, open `dashboards/TrainingHub.html` in a browser.

### Running steps individually

```bash
python strava_compile.py   # step 1: export → enriched CSV
python generate_hub.py     # step 2: CSV → dashboards/TrainingHub.html
```

The output CSV is written to `csv_data/YYYY-MM-DD_strava.csv` (the subdirectory is created automatically). The date is taken from the export zip's file modification time — i.e. when you downloaded it from Strava — so the filename reflects the export generation date rather than the day you ran the script.

### Rebuild controls

Both the `.fit` parse (compile) and segment detection (hub) are cached, so a normal run reuses prior work and only processes what changed. Three `main.py` flags force a rebuild when you have edited the logic behind a cache:

| Command | Re-parses every `.fit` | Re-detects segments |
|---|---|---|
| `python main.py --rebuild` | yes | no |
| `python main.py --rebuild-segments` | no | yes |
| `python main.py --rebuild-all` | yes | yes |

`--rebuild` is forwarded to the compile step (see [Incremental compilation](#incremental-compilation)); `--rebuild-segments` forces segment re-detection via the `STRAVA_SEG_REBUILD` environment variable. For a hub-only regenerate (no recompile), set it directly:

```bash
STRAVA_SEG_REBUILD=1 python generate_hub.py   # ignore segments_cache.json and re-detect
```

### Personalization (optional)

Everything is derived from your export, so no setup is required. To add race-goal cards to the
Goals tab, copy `config.example.json` to `cache/config.json` and list your races (name, distance,
date, target time, optional pace wave). Without a config the Goals tab shows generic Riegel/VDOT
predictions only — no personal targets.

`cache/config.json` is gitignored, so your goals stay local. It also accepts an optional
`segment_anchors` list, but you rarely need it: recurring loops are now auto-detected even when
you only ever run them inside a longer route (see [Benchmark segments](#benchmark-segments-generate_segmentspy)). Use an anchor only to pin or rename a loop the detector misses.

### Options

| Flag | Default | Description |
|---|---|---|
| `--downloads` | `~/Downloads` | Folder to search for the Strava export zip or folder |
| `--archive` | auto-detected | Override: path to a specific export zip or extracted folder |
| `--csv` | auto-detected | Override: path to a specific `activities.csv` |
| `--out` | `csv_data/YYYY-MM-DD_strava.csv` | Override: output CSV path |
| `--sport` | `running` | Filter by sport. Options: `running`, `cycling`, `all` |
| `--tmp` | `<system temp>/strava_fit` | Temp directory for decompressed `.fit` files |
| `--rebuild` | off | Ignore the prior CSV cache and re-parse every `.fit` file (see Incremental compilation) |
| `--workers` | CPU count | Number of parallel parse worker processes |
| `--profile` | off | Print decompress/parse timing and hub-generation breakdown |

### Incremental compilation

Parsing `.fit` files is the slowest part of the pipeline, so `strava_compile.py` only parses
files it hasn't seen before. The most recent `csv_data/*_strava.csv` doubles as a cache: any
activity already present there is reused verbatim, and only newly added `.fit` files are
parsed. Those new files are parsed in parallel across CPU cores. A repeat run over an
unchanged archive parses nothing and finishes near-instantly.

Run with `--rebuild` to discard the cache and re-parse everything. This is required after any
change to the parser's output columns (`parse_fit` in `strava_compile.py`), since the cache
would otherwise keep serving the old schema.

### How to get your Strava export

Go to **Settings > My Account > Download or Delete Your Account > Request Your Archive**.
Strava emails a link within ~24 hours. Download the zip and leave it in your Downloads folder.

## Output columns

The output CSV contains all original `activities.csv` columns plus the following columns
derived from the `.fit` files.

### Session summary (`fit_*`)

| Column | Description |
|---|---|
| `fit_file` | Activity ID (stem of the `.fit` filename) |
| `fit_sport` | Sport type as recorded by the device |
| `fit_start_time` | Activity start timestamp |
| `fit_total_distance_km` | Total distance in kilometres |
| `fit_moving_time_s` | Timer time in seconds (excludes auto-paused periods) |
| `fit_elapsed_time_s` | Wall-clock elapsed time in seconds |
| `fit_avg_speed_mps` | Average speed in m/s |
| `fit_max_speed_mps` | Max speed in m/s |
| `fit_avg_heart_rate` | Average heart rate (bpm) |
| `fit_max_heart_rate` | Max heart rate (bpm) |
| `fit_avg_cadence` | Average running cadence (steps/min, one foot) |
| `fit_total_ascent_m` | Total elevation gain in metres |
| `fit_total_descent_m` | Total elevation loss in metres |
| `fit_avg_power` | Average power in watts (if recorded) |
| `fit_total_calories` | Total calories |
| `fit_training_stress_score` | Training Stress Score (if recorded) |
| `fit_record_count` | Number of raw record messages parsed |
| `fit_splits` | JSON array of per-lap data (see below) |
| `fit_pace_zone_secs` | JSON array `[z1_s, z2_s, z3_s, z4_s, z5_s]` — seconds spent in each of 5 pace zones, tallied from the per-record speed stream |
| `fit_km_splits` | JSON array of per-km splits with elevation gain/loss and cadence (see below) |
| `fit_gps_polyline` | JSON array of `[lat, lon]` route points (downsampled) for maps |
| `fit_distance_stream` | JSON array of over-distance samples — pace/elevation/HR/cadence + position — for per-run charts (see below) |

### Lap splits (`fit_splits`)

Stored as a JSON array in the `fit_splits` column. Each element is an object with:

```json
{
  "dist_km": 1.002,
  "time_s": 312.4,
  "avg_speed_mps": 3.21,
  "avg_hr": 158,
  "avg_cadence": 84,
  "ascent_m": 3,
  "descent_m": 1,
  "pace_min_km": 5.196
}
```

Note: lap distance/speed fields are populated by the device and may be absent for some
Garmin models. Best efforts (below) are computed from raw record messages and are reliable
regardless.

### Per-km splits (`fit_km_splits`)

Per-kilometre splits interpolated from the cumulative-distance record stream (independent of
device laps). Each element covers one complete km:

```json
{
  "km": 1,
  "time_s": 312.4,
  "gain_m": 11,
  "loss_m": 3,
  "cad": 168
}
```

`gain_m`/`loss_m` (per-km elevation gain/loss in metres) and `cad` (average cadence, both
feet) are omitted when the underlying stream has no data — e.g. treadmill runs.

### Per-run streams (`fit_gps_polyline`, `fit_distance_stream`)

`fit_gps_polyline` is a `[[lat, lon], ...]` array used to draw the route on the per-run map
and to build the all-time heatmap. It is downsampled **by distance** — ~150 points per km,
floored at 75 points for very short runs and hard-capped at 2500 for very long ones — so a
long run keeps the same per-km resolution as a short one instead of being squeezed into a
flat point cap. The density and bounds are tunable via `GPS_POINTS_PER_KM`, `GPS_MIN_POINTS`,
and `GPS_MAX_POINTS` near the top of `strava_compile.py`.

`fit_distance_stream` powers the over-distance charts. It is sampled **by distance** —
~150 samples per km (≈ every 7 m), capped at 3000 points — so short and long runs get the
same spatial resolution rather than the same total count. Each point carries whichever
metrics the run recorded:

```json
{ "d": 1.42, "pace": 312.5, "elev": 41.2, "hr": 155, "cad": 168, "lat": -33.84, "lon": 151.21 }
```

`d` is distance in km and `pace` is seconds/km; `lat`/`lon` let the chart cursor map a
distance back to a position on the route map. Keys are omitted when a metric isn't recorded.
The sampling density and cap are tunable via `STREAM_POINTS_PER_KM` and `STREAM_MAX_POINTS`
at the top of the stream section in `strava_compile.py`.

### Best efforts

Computed via a sliding window over the second-by-second record stream. Each represents the
fastest continuous segment of exactly that distance anywhere in the activity. Pace columns
are in `M:SS/km` format.

| Column pair | Distance |
|---|---|
| `best_400m_s` / `best_400m_pace` | 400 m |
| `best_1/2mi_s` / `best_1/2mi_pace` | 804.7 m (half mile) |
| `best_1km_s` / `best_1km_pace` | 1 km |
| `best_1mi_s` / `best_1mi_pace` | 1609.3 m (1 mile) |
| `best_2mi_s` / `best_2mi_pace` | 3218.7 m (2 miles) |
| `best_5k_s` / `best_5k_pace` | 5 km |
| `best_10k_s` / `best_10k_pace` | 10 km |
| `best_15k_s` / `best_15k_pace` | 15 km |
| `best_10mi_s` / `best_10mi_pace` | 16093.4 m (10 miles) |
| `best_20k_s` / `best_20k_pace` | 20 km |
| `best_half_s` / `best_half_pace` | 21097.5 m (half marathon) |

Columns are `None`/empty for any distance longer than the activity total.

### Interval detection

For sessions where rest time is at least 20% of elapsed time (i.e. the watch was paused or
the athlete was stationary for a significant portion), the script attempts to detect
individual effort reps by speed threshold segmentation.

| Column | Description |
|---|---|
| `interval_rep_count` | Number of reps detected |
| `interval_rep_distances` | JSON array of each rep's distance in metres, for sanity-checking |
| `interval_best_400m_s` / `interval_best_400m_pace` | Fastest rep covering >= 400 m |
| `interval_best_800m_s` / `interval_best_800m_pace` | Fastest rep covering >= 800 m |
| `interval_best_1km_s` / `interval_best_1km_pace` | Fastest rep covering >= 1 km |
| `interval_best_1mi_s` / `interval_best_1mi_pace` | Fastest rep covering >= 1609 m |

These columns are empty for continuous runs. If fewer than 2 reps are detected the session
is treated as a false positive and columns are also left empty.

The detection constants are at the top of the script and are easy to tune:

```python
PACE_ZONE_THRESHOLD_MPS       = None   # threshold pace for pace zones (e.g. 3.33 = 5:00/km); None = use activity avg speed
INTERVAL_REST_RATIO_THRESHOLD = 0.20   # flag session if rest >= 20% of elapsed time
INTERVAL_SPEED_THRESHOLD_MPS  = 2.0    # below this speed = rest (~8:20/km)
INTERVAL_MIN_REST_DURATION_S  = 5      # consecutive slow seconds before closing a rep
```

If reps are being split in two, increase `INTERVAL_MIN_REST_DURATION_S`. If continuous
runs are being falsely flagged, increase `INTERVAL_REST_RATIO_THRESHOLD` or decrease
`INTERVAL_SPEED_THRESHOLD_MPS`.

## The training hub (`generate_hub.py`)

Once you have a `csv_data/YYYY-MM-DD_strava.csv`, `generate_hub.py` builds a single self-contained `dashboards/TrainingHub.html` — no server required, open it directly in a browser. It imports the analysis logic from `generate_dashboards.py` and `generate_analytics.py` and stitches everything into one tabbed page (the all-time heatmap and per-run maps use [Leaflet](https://leafletjs.com/), loaded from a CDN, so the maps need an internet connection):

| Tab | Contents |
|---|---|
| Overview | Summary stats, fitness/fatigue/form (CTL/ATL/TSB) indicators, a "Plan for next 7 days" load recommendation (target weekly distance with safe range, longest run, climb, and easy-pace ceiling, derived from your ACWR and current form), a "Preview after rest days" date control that decays the CTL/ATL/TSB tiles and the plan card forward up to 7 days as if no new activity were logged (history charts unaffected), and an all-time GPS heatmap that clusters runs into named hotspots with a chip bar to jump between them, off-screen edge markers, and a 90d/1y/All time filter |
| Best Efforts | Top-3 best efforts per distance band (400m → half marathon), with raw and grade-adjusted pace. Each card is clickable and opens that run's expanded map and analysis view |
| Goals | Race-goal gap cards, training targets vs current bests, Riegel race predictions, and weekly mileage |
| Analytics | Training load/fitness/fatigue (CTL/ATL/TSB), ACWR, training strain, critical-speed model, pace-zone distribution, VDOT trend, cadence trend, calendar heatmap, and a weekly/monthly distance/elevation/time totals chart. Most charts have independent time-range toggles (3M/6M/1Y/All). The tab is organised into Injury risk, Paces, Trends, and Compare sub-tabs, the last a stat-comparison tool that scatters or distributes any two run metrics against each other |
| Runs | Run list where each run is tagged with a training-type badge (see [Run classification](#run-classification)) and can be searched by name/date, filtered by type chip and by min/max ranges on distance, time, speed, and elevation, and sorted by date, distance, time, speed, or elevation (ascending or descending). Selecting a run shows its Strava description (with a more/less toggle), stats, a route map, per-km splits (with elevation gain/loss and cadence), pace zones, best efforts, and over-distance pace/elevation/HR/cadence charts that label the exact distance and value on hover. Hovering a best-effort row highlights that effort's stretch in gold on both the route map and the over-distance chart. Recording pauses (auto-pause, manual pause, or a stopped-then-resumed session) are marked consistently on both views: on the over-distance graph the line segment spanning each pause is drawn as an amber dotted stretch (replacing the solid line) and labelled with the stop's duration, and on the route map the stretch traversed during each pause — including any positional jump from a stopped-then-resumed session — is drawn as an amber dotted polyline with a marker at the stop. Runs that paused also show a "Pauses" stat. Clicking the map opens an expanded view with the route and over-distance graph on one screen — tracing the graph there also moves a marker along the route |
| Segments | Auto-detected benchmark routes you repeat — loops, climbs, and point-to-point stretches — each with a route map, every timed attempt, a personal record, and a trend chart of time over date. Clicking a segment opens an interactive map and attempt list (see [Benchmark segments](#benchmark-segments-generate_segmentspy)) |

### Run classification

Every run in the Runs tab is sorted into one training category, shown as a coloured badge in
the list and usable as a one-click filter chip. Classification happens in `generate_hub.py`
(`_classify_run`) from data already in the CSV, so **no `--rebuild` is needed** — a plain
`python generate_hub.py` re-tags everything. The categories are judged **relative to your own
data** (your distance distribution and your personal threshold pace) rather than fixed cutoffs,
so they track your fitness over time.

Each run gets the first category it matches, in priority order:

| Category | Rule |
|---|---|
| **Misc** | Distance below `MISC_MAX_KM` — suspiciously short efforts (stair sprints for a segment record, an elevation challenge on one flight of steps). Checked first, so these never count as intervals |
| **Intervals** | Flagged as an interval session (rest ≥ 20% of elapsed time, the same `is_interval` signal used elsewhere) and at least `MISC_MAX_KM` long |
| **Race** | A sustained hard effort — zone-5 time (faster than threshold) is at least `RACE_Z5_SHARE` of the run's zoned time |
| **Threshold** | Zone-4 (threshold) time is at least `THRESHOLD_Z4_SHARE` of zoned time, or combined zone-4 + zone-5 time is at least `THRESHOLD_Z45_SHARE` — so a fast or downhill sustained effort that spills into zone 5 without qualifying as a race still counts |
| **Tempo** | Combined zone-3 + zone-4 time is at least `TEMPO_Z34_SHARE` of zoned time |
| **Long** | Distance is in the top quarter of your runs *and* ≥ `LONG_RUN_MIN_RATIO` × your median run *and* ≥ `LONG_RUN_MIN_KM` |
| **Recovery** | Mostly easy — zone-1 time is at least `RECOVERY_Z1_SHARE` of zoned time |
| **Easy** | The fallback for everything else (also where runs land when there's no personal threshold yet, so the pace zones are empty) |

Because the intensity categories (Race/Threshold/Tempo) require a *sustained* share of hard
running, an easy long run falls through to **Long**, while a genuine workout outranks distance.
The thresholds are tunable constants near the top of the run-type section in `generate_hub.py`:

```python
MISC_MAX_KM         = 1.0    # below this = miscellaneous (stair sprints, elevation challenges, etc.)
LONG_RUN_PERCENTILE = 0.75   # distance at/above this percentile of all runs = long candidate
LONG_RUN_MIN_RATIO  = 1.30   # ...and at least this multiple of the median run distance
LONG_RUN_MIN_KM     = 10.0   # ...and never below this absolute floor
RACE_Z5_SHARE       = 0.40   # >= this share of zoned time in zone 5 (frac>=1.03) = race
THRESHOLD_Z4_SHARE  = 0.30   # >= this share in zone 4 (0.93-1.03) = threshold session
THRESHOLD_Z45_SHARE = 0.50   # ...or >= this combined share in zones 4-5 = threshold session
TEMPO_Z34_SHARE     = 0.35   # >= this combined share in zones 3-4 = tempo session
RECOVERY_Z1_SHARE   = 0.70   # >= this share in zone 1 (frac<0.77) = recovery
```

The pace zones themselves are anchored to your threshold speed — a grade-adjusted power-law curve
fit across your best efforts at several endurance distances (1 mile through half marathon) rather
than a single raw best-5K time, which is only used as a fallback when there are too few efforts to
fit the curve; see [Time in pace zones](#analytics-dashboard-generate_analyticspy) for the zone
boundaries.

### Standalone dashboard scripts

`lib/generate_dashboards.py` and `lib/generate_analytics.py` can also be run on their own to emit individual HTML files into `dashboards/` (created automatically):

| Script | Output file | Contents |
|---|---|---|
| `lib/generate_dashboards.py` | `dashboards/top_runs_by_distance.html` | Top-3 best efforts per distance band (400m → half marathon), with raw and grade-adjusted pace |
| `lib/generate_dashboards.py` | `dashboards/goal_dashboard.html` | Race-goal gap cards, training targets vs current bests, and Riegel race predictions |
| `lib/generate_analytics.py` | `dashboards/analytics_dashboard.html` | Training load/fitness/fatigue (CTL/ATL/TSB), ACWR, training strain, critical-speed model, pace-zone distribution, pace progression by run type, a current-paces card, VDOT trend, cadence trend, calendar heatmap, and a stat-comparison tool |

### Goal dashboard sections

| Section | Description |
|---|---|
| Race goals | Predicted finish time vs target for upcoming races, with a progress bar |
| Training targets vs current bests | Data-derived target pace range for every distance (400m → half); gap column shows where fitness falls off relative to your personal Riegel curve |
| Riegel race predictions | Extrapolated race times anchored on your best grade-adjusted 10K |

### Training target methodology

Targets are derived entirely from your own data — there are no hardcoded pace constants. The pipeline has three stages.

#### 1. Grade adjustment (Minetti formula)

All effort times are corrected for elevation before any analysis. The Minetti metabolic cost model gives the energy cost of running at grade *g* (rise/run):

```
cost(g) = 155.4g⁵ − 30.4g⁴ − 43.3g³ + 46.3g² + 19.5g + 3.6
```

A raw effort time is scaled by `cost(0) / cost(g)` to produce the equivalent flat-ground time. Grade is computed as `elevation_gain / (2 × distance_m)` — the factor of 2 treats elevation gain as a one-way uphill contribution (conservative, since descents provide only partial recovery). The `minetti_cost` and `ga_time` helpers live in `lib/grade.py` and are shared by both dashboard modules.

#### 2. Personal Riegel curve fitting

The standard Riegel endurance formula predicts finish time at distance *D* from a known time at distance *D₁*:

```
T₂ = T₁ × (D₂ / D₁)^b
```

The conventional exponent *b = 1.06* is a population average. This script fits your personal *b* by running log-log least squares regression on your GA best efforts at 1 mile, 5K, 10K, 15K, and half marathon:

```
log(T) = log(a) + b × log(D)   →   solve for a, b
```

The fitted *b* is clamped to [1.0, 1.15] for physiological sanity. A value above 1.06 means you fall off faster than the average runner as distance increases; below 1.06 means your endurance scales better than average. The subtitle of the training targets section in `goal_dashboard.html` shows your current fitted *b* and an interpretation.

Functions: `fit_riegel(points)` → `(a, b)`

#### 3. Hybrid target derivation

Targets are computed from the fitted curve using two different methods depending on distance, because Riegel extrapolation from race distances becomes unreliable for very short repetition efforts:

**Race distances (≥ 1500m — 1 mile, 5K, 10K, 15K, half marathon)**

```
target_hi = a × D^b / (D / 1000)   # the curve's predicted pace (s/km)
target_lo = target_hi × 0.97       # 3% faster: improvement goal
```

Every distance on this part of the table shares the same curve, so gaps are directly comparable. A 15K that's 40 s/km below the curve predicted from your 5K fitness is immediately visible.

**Short repetition efforts (400m, 800m, 1km)**

Targets are expressed as a percentage of the curve-predicted 5K pace:

| Distance | Target range | Rationale |
|---|---|---|
| 1km reps | 96–98% of 5K pace | Slightly faster than 5K, VO2max zone |
| 800m reps | 93–96% of 5K pace | Faster, high-intensity |
| 400m reps | 88–93% of 5K pace | Fastest reps, speed-endurance zone |

Function: `derive_training_target(riegel_a, riegel_b, target_dist_m)` → `(lo_s_per_km, hi_s_per_km)`

#### Reading the training targets table

| Status | Meaning |
|---|---|
| **exceeds** (green) | Current best is faster than even the aggressive target (target_lo) |
| **on target** (green) | Current best falls within the target range |
| **gap** (orange) | Current best is slower than the Riegel prediction — shows where fitness is falling behind the curve |

### Pause / gap detection

Runs where the watch stopped recording — auto-pause, a manual pause, or a stopped-then-resumed
session — have each pause marked on the per-run route map and over-distance graph. Detection runs
in `generate_hub.py` from the already-compiled `fit_distance_stream` (it uses the per-sample
elapsed-time field `t`), so **no `--rebuild` is required** — a plain `python generate_hub.py`
picks it up. The stream is sampled by distance, so during a pause no distance accrues and the
stop collapses into a single sample whose elapsed time jumps; that jump is the signal.

A sample is treated as a pause when its time gap clears an adaptive floor: at least
`PAUSE_MIN_GAP_S` seconds, and at least `PAUSE_GAP_MULTIPLIER` times the run's median sample
interval. The multiplier makes the threshold scale with the run, so genuinely slow running (which
lengthens every sample's interval uniformly) is not mistaken for a stop. Across a representative
export the sum of detected pause durations per run matches that session's rest time
(`fit_elapsed_time_s − fit_moving_time_s`) closely.

Both views render a pause in the same amber dotted style. On the over-distance graph the
line segment that crosses each pause is replaced by an amber dotted stretch (the pause's
distorted pace sample is also excluded from the y-axis scale so the real running pace fills
the chart), labelled with the stop's duration. On the route map each detected pause is
mapped to its nearest GPS points before and after the stop, and the polyline stretch between
them is drawn as an amber dotted segment. This covers stopped-then-resumed sessions where the
athlete moved between stopping and resuming: that traversal is part of the recorded route, so
it simply dots along the polyline rather than needing a separate straight connector.

| Constant | Default | Description |
|---|---|---|
| `PAUSE_MIN_GAP_S` | `20` | Absolute floor (seconds); shorter gaps are ignored |
| `PAUSE_GAP_MULTIPLIER` | `5` | A gap also has to be at least this many times the run's median sample interval |

If real stops are being missed, lower `PAUSE_GAP_MULTIPLIER` or `PAUSE_MIN_GAP_S`; if slow
segments are being flagged, raise them. The constants are at the top of the pause-detection
section in `generate_hub.py`.

## Benchmark segments (`generate_segments.py`)

The Segments tab mines recurring routes from your GPS tracks and turns them into Strava-style
benchmarks — no manual segment creation, no Strava segment API. For each detected route it
collects every run that completed it, times each attempt, finds the personal record, and draws
the route on a map. A segment only qualifies once it has been completed by at least
`MIN_RUNS` (2) distinct runs, spanning `MIN_SPAN_DAYS` (14) days, over a length of at least
`MIN_LEN_M` (400 m), so one-off routes and short connectors never clutter the panel.

Three kinds are detected:

| Type | What it is | How it is found |
|---|---|---|
| **Segment** | A repeated point-to-point stretch | Corridor mining: the busiest directed cell-to-cell transitions across all runs are grown into maximal shared corridors, then every run is matched against each corridor with a distance-bounded sliding window |
| **Climb** | A corridor with sustained ascent | A corridor whose net gain and average grade clear `CLIMB_MIN_GAIN` / `CLIMB_MIN_GRADE`; the benchmark is oriented uphill so it times the climb |
| **Loop** | A closed circuit you lap | Self-crossing detection per run (where the track returns near an earlier point), clustered across runs by centroid and length. A two-pass scan identifies the loop at a stable scale, then re-scans each run at a lower floor so repeats within one session each count as a lap |

A loop you usually run *inside* a longer route never self-crosses, so closed-loop mining can't
see it. These are recovered automatically: any loop cluster that falls short of the `MIN_RUNS`
closed-lap gate is promoted to an **auto-anchor** — a line is derived from the cleanest
self-crossing instance and matched off that fixed line across every run (the way Strava counts
efforts), so traversals embedded in longer routes still count. The normal run/span/length gates
still apply. As a last resort, a loop that never self-crosses in *any* run (so no line can be
derived) can be pinned in `cache/config.json` under `segment_anchors` — an approximate centre and
length, plus a curated name.

### Timing and ranking

Each attempt's elapsed time comes from the per-record time stream; pace and grade-adjusted
pace (via the same Minetti model as the rest of the hub) are shown per attempt, and the
fastest attempt is flagged as the PR. Repeats of a loop within a single session are numbered
`(lap 2)`, `(lap 3)`, … so they read correctly in the attempt list.

In the trend chart each attempt dot is coloured by the run's training type (see
[Run classification](#run-classification)), so you can see at a glance whether a fast effort
came from a race, a tempo, or an easy day. Attempts are connected by one line per type rather
than a single chronological line, the PR keeps a gold ring, and the expanded segment view adds
a legend of the types present and shows the type on hover.

### Drawing the route line

A segment's drawn line is a single real run — the **medoid** effort, the one whose shape is
most typical of the set — rather than an average of all runs, which would round off street
corners. That line is then snapped onto the OpenStreetMap walking network (via Overpass) so it
follows real paths instead of one run's GPS wobble. The snap is only trusted when it stays
faithful to the actual run; it is discarded in favour of the raw trace when it:

- strays too far in length (outside 0.7–1.4× the benchmark), or
- drifts too far from the real trace (`MATCH_MAX_DEV_M`, mean), or
- becomes **jaggier than the run itself** — more sharp direction-reversals per point than the
  raw trace by `MATCH_MAX_TURN_GAIN`. This catches the snap flickering between two parallel
  paths a few metres apart (e.g. a footway and the road beside it), which draws a zig-zag no
  run ever ran.

Segment names come from OpenStreetMap reverse geocoding (Nominatim) — nearby road, feature, and
suburb names compose labels like *"Shirley Road to Cable Street (Wollstonecraft)"* or
*"Morton Street loop (Waverton)"*. Network lookups (Overpass and Nominatim) are rate-limited and
**cached on disk** in `cache/`, and the whole detection result is cached on a signature of the
run set, so a rebuild over unchanged runs reuses everything and makes no network calls. To
force a fresh detection after changing detection logic, set `STRAVA_SEG_REBUILD=1` (or run
`python main.py --rebuild-segments`); see [Rebuild controls](#rebuild-controls). All detection
thresholds are tunable constants at the top of `generate_segments.py`.

### Starring and filtering segments

Each segment card has a star toggle. Stars are saved instantly to the browser's `localStorage`
(keyed by a stable geo-key, so they stay attached to the same route across rebuilds) and persist
between dashboard loads. Above the grid, type chips (**All / Segment / Climb / Loop**) and a
**Starred only** toggle filter the visible cards. Because the dashboard is a static file and
can't write to disk itself, **Export stars** downloads `segment_stars.json`; drop it into `cache/`
and the next build reads it so stars also survive a full regenerate on a fresh browser.

## Analytics dashboard (`generate_analytics.py`)

All analyses use pace, distance, and elevation — no heart rate or power required. These analyses appear in the hub's Analytics tab; running the script standalone outputs `dashboards/analytics_dashboard.html`.

### Sections

| Section | Description |
|---|---|
| Fitness, fatigue and form | CTL (42-day), ATL (7-day), and TSB (form) computed from a pace-based load score — the HR-free equivalent of Strava's Fitness & Freshness |
| Acute:Chronic Workload Ratio | 7-day acute load vs 28-day chronic baseline; green 0.8–1.3 sweet spot, red = injury-risk spike |
| Training strain & monotony (Foster) | Monotony = weekly mean / SD; strain = weekly load × monotony; high strain with low variation is the classic overtraining signature |
| Critical speed model | Running analog of Strava's power curve: `distance = CS × time + D'`. Fits CS (sustainable threshold pace) and D' (finite anaerobic distance) to all best-effort distances ≥ 1200 m |
| Time in pace zones | Proportion of total moving time in 5 zones anchored to critical speed (Z4 = threshold); aims for a polarised Z1/Z2 + Z4/Z5 distribution |
| VO₂max estimate (VDOT) | Monthly best Daniels VDOT from grade-adjusted 1mi–10K efforts; useful as a fitness trend rather than an absolute figure |
| Average cadence by month | Steps/min (one foot); rising cadence at the same pace signals improving running economy |
| Pace progression | Grade-adjusted pace over time per run type (tempo/threshold/race/intervals/long/easy/recovery), each with a rolling trend line |
| Compare stats | Interactive tool to scatter, plot over time, or show the distribution of any two of your run metrics (distance, pace, elevation, time, cadence, calories, HR), coloured by run type |
| Training log calendar | Daily training load heatmap — darker green = higher load, gaps are rest days |
| Distance, elevation & time | Per-week or per-month totals for distance, elevation gain, or moving time, gap-filled so rest periods drop to the axis. Time window (3M/6M/1Y/All) and granularity (weekly/monthly) are independent toggles |

### Load score

The CTL/ATL/TSB calculation uses a pace-based stress score because no HR or power data is available. Each session's load is:

```
load = grade_adjusted_distance_km × 10 × intensity²
```

where `intensity = threshold_pace / session_GA_pace`, clamped to [0.5, 1.5]. The threshold pace is the 15th-percentile fastest GA pace across all sessions. This mirrors the TRIMP-less load that Runalyze uses when HR is absent.

### Next-week load recommendation

The "Plan for next 7 days" card on the Overview tab turns the ACWR injury model into a concrete weekly plan. It picks a target training load for the coming week from the acute:chronic workload ratio (acute = trailing 7-day load, chronic = trailing 28-day load scaled to a week) and current form (TSB):

| Condition | Action | Target load |
|---|---|---|
| TSB < -25 or ACWR > 1.5 | Back off and recover | 0.80 × chronic |
| TSB < -10 or ACWR > 1.3 | Hold steady | chronic |
| TSB > 12 and ACWR < 1.1 | Build, room to spare | max(acute, chronic) × 1.10 |
| otherwise | Build gradually | max(acute, chronic) × 1.05 |

The target is capped at the 1.3 × chronic injury-risk ceiling. It is then translated back into real planning units using your own recent 28-day load-per-km and elevation-per-km ratios, so the figures match how you actually run: target weekly distance (with the 0.8–1.3 sweet-spot range), longest single run (~40% of the week), expected climb, and an easy-pace ceiling (~85% of threshold velocity). The card needs at least a 28-day baseline (14+ days of load) before it appears. Guidance only, not a substitute for how your body feels.

Functions: `_load_recommendation(...)` and `_load_advice(tsb, acwr)` in `generate_hub.py`.

### Critical speed model

Points at ≥ 1200 m are fitted via linear regression of distance on time (`dist = CS × t + D'`). The slope CS is your aerobic threshold in m/s; D' (intercept) is the finite work reserve above threshold. The dashboard table compares actual best times to the model's predicted times at each distance.

## File structure

```
Strava-Analysis/
├── main.py                     # one-command entrypoint: compile + generate hub
├── strava_compile.py           # step 1 — processes Strava export → enriched CSV
├── generate_hub.py             # step 2 — builds the consolidated hub (TrainingHub.html)
├── lib/                        # support modules imported by generate_hub.py
│   ├── generate_dashboards.py      # best-efforts/goal logic; also runs standalone
│   ├── generate_analytics.py       # analytics logic; also runs standalone
│   ├── generate_segments.py        # benchmark-segment detection for the hub's Segments tab
│   ├── localtime.py                # resolves each run's local timezone from GPS + Activity Date (optional timezonefinder)
│   ├── grade.py                    # shared Minetti grade-adjustment helpers (minetti_cost, ga_time)
│   └── config.py                   # loads optional cache/config.json (races + segment anchors)
├── config.example.json         # sample personalization file; copy to cache/config.json
├── csv_data/                   # output CSVs (gitignored)
│   └── YYYY-MM-DD_strava.csv
├── cache/                      # detection caches + personal config/stars (gitignored)
│   ├── config.json                 # optional personalization (races + segment anchors)
│   ├── segment_stars.json          # user-starred segments (geo-keys); read at build
│   ├── segments_cache.json         # detected segments, keyed on a run-set signature
│   ├── segment_geocode_cache.json  # cached OSM (Nominatim) segment names
│   └── segment_match_cache.json    # cached OSM (Overpass) map-matched route lines
├── dashboards/                 # output HTML dashboards (gitignored)
│   ├── TrainingHub.html        # the consolidated training hub (main output)
│   ├── top_runs_by_distance.html   # only if lib/generate_dashboards.py is run alone
│   ├── goal_dashboard.html         # only if lib/generate_dashboards.py is run alone
│   └── analytics_dashboard.html    # only if lib/generate_analytics.py is run alone
└── README.md
```

Decompressed `.fit` files are written to a `strava_fit/` folder in the system temp directory
(override with `--tmp`) and are not kept after the run.
The Strava export zip itself stays in your Downloads folder and is not modified.