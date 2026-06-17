# Strava Analysis

Processes a Strava data export into an enriched CSV, merging `activities.csv` metadata with
per-second record data parsed from each activity's `.fit.gz` file. Computes best efforts
and detects interval reps from the raw GPS/distance stream.

## Requirements

Python 3.10+ and one third-party library:

```bash
pip install fitparse
```

## Usage

Drop a Strava export zip (`export_*.zip`) into your Downloads folder, then run:

```bash
python main.py
```

This runs all three steps in sequence — compile the export, generate the dashboards, generate the analytics — and writes all output to `csv_data/` and `dashboards/`. It exits early with an error if any step fails.

### Running steps individually

```bash
python strava_compile.py
```

The output CSV is written to `csv_data/YYYY-MM-DD_strava.csv` (the subdirectory is created automatically). The date is taken from the export zip's file modification time — i.e. when you downloaded it from Strava — so the filename reflects the export generation date rather than the day you ran the script.

### Options

| Flag | Default | Description |
|---|---|---|
| `--downloads` | `~/Downloads` | Folder to search for the Strava export zip or folder |
| `--archive` | auto-detected | Override: path to a specific export zip or extracted folder |
| `--csv` | auto-detected | Override: path to a specific `activities.csv` |
| `--out` | `csv_data/YYYY-MM-DD_strava.csv` | Override: output CSV path |
| `--sport` | `running` | Filter by sport. Options: `running`, `cycling`, `all` |
| `--tmp` | `/tmp/strava_fit` | Temp directory for decompressed `.fit` files |

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

## Generating dashboards

Once you have a `csv_data/YYYY-MM-DD_strava.csv`, you can run the dashboard scripts individually:

```bash
python generate_dashboards.py
python generate_analytics.py
```

Both scripts write self-contained HTML files into the `dashboards/` subdirectory (created automatically). Open any file directly in a browser — no server required.

| Script | Output file | Contents |
|---|---|---|
| `generate_dashboards.py` | `dashboards/top_runs_by_distance.html` | Top-3 best efforts per distance band (400m → half marathon), with raw and grade-adjusted pace |
| `generate_dashboards.py` | `dashboards/goal_dashboard.html` | Race-goal gap cards, training targets vs current bests, Riegel race predictions, and a weekly mileage sparkline |
| `generate_analytics.py` | `dashboards/analytics_dashboard.html` | Training load/fitness/fatigue (CTL/ATL/TSB), ACWR, training strain, critical-speed model, pace-zone distribution, VDOT trend, cadence trend, and calendar heatmap |

### Goal dashboard sections

| Section | Description |
|---|---|
| Race goals | Predicted finish time vs target for upcoming races, with a progress bar |
| Training targets vs current bests | Data-derived target pace range for every distance (400m → half); gap column shows where fitness falls off relative to your personal Riegel curve |
| Riegel race predictions | Extrapolated race times anchored on your best grade-adjusted 10K |
| Weekly mileage | Sparkline of km/week from April 2026 onwards |

### Training target methodology

Targets are derived entirely from your own data — there are no hardcoded pace constants. The pipeline has three stages.

#### 1. Grade adjustment (Minetti formula)

All effort times are corrected for elevation before any analysis. The Minetti metabolic cost model gives the energy cost of running at grade *g* (rise/run):

```
cost(g) = 155.4g⁵ − 30.4g⁴ − 43.3g³ + 46.3g² + 19.5g + 3.6
```

A raw effort time is scaled by `cost(0) / cost(g)` to produce the equivalent flat-ground time. Grade is computed as `elevation_gain / (2 × distance_m)` — the factor of 2 treats elevation gain as a one-way uphill contribution (conservative, since descents provide only partial recovery).

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

## Analytics dashboard (`generate_analytics.py`)

Run after `generate_dashboards.py`. All analyses use pace, distance, and elevation — no heart rate or power required. Outputs `dashboards/analytics_dashboard.html`.

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
| Training log calendar | Daily training load heatmap — darker green = higher load, gaps are rest days |

### Load score

The CTL/ATL/TSB calculation uses a pace-based stress score because no HR or power data is available. Each session's load is:

```
load = grade_adjusted_distance_km × 10 × intensity²
```

where `intensity = threshold_pace / session_GA_pace`, clamped to [0.5, 1.5]. The threshold pace is the 15th-percentile fastest GA pace across all sessions. This mirrors the TRIMP-less load that Runalyze uses when HR is absent.

### Critical speed model

Points at ≥ 1200 m are fitted via linear regression of distance on time (`dist = CS × t + D'`). The slope CS is your aerobic threshold in m/s; D' (intercept) is the finite work reserve above threshold. The dashboard table compares actual best times to the model's predicted times at each distance.

## File structure

```
Strava-Analysis/
├── main.py                     # one-command entrypoint: compile + all dashboards
├── strava_compile.py           # step 1 — processes Strava export → enriched CSV
├── generate_dashboards.py      # step 2 — generates best-efforts and goal dashboards
├── generate_analytics.py       # step 3 — generates training analytics dashboard
├── csv_data/                   # output CSVs (gitignored)
│   └── YYYY-MM-DD_strava.csv
├── dashboards/                 # output HTML dashboards (gitignored)
│   ├── top_runs_by_distance.html
│   ├── goal_dashboard.html
│   └── analytics_dashboard.html
└── README.md
```

Decompressed `.fit` files are written to `/tmp/strava_fit/` and are not kept after the run.
The Strava export zip itself stays in your Downloads folder and is not modified.