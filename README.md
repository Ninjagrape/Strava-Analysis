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
python strava_compile.py
```

The output CSV is written to the same directory as the script, named `YYYY-MM-DD_strava.csv`.

### Options

| Flag | Default | Description |
|---|---|---|
| `--downloads` | `~/Downloads` | Folder to search for the Strava export zip or folder |
| `--archive` | auto-detected | Override: path to a specific export zip or extracted folder |
| `--csv` | auto-detected | Override: path to a specific `activities.csv` |
| `--out` | `YYYY-MM-DD_strava.csv` next to script | Override: output CSV path |
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
INTERVAL_REST_RATIO_THRESHOLD = 0.20   # flag session if rest >= 20% of elapsed time
INTERVAL_SPEED_THRESHOLD_MPS  = 2.0    # below this speed = rest (~8:20/km)
INTERVAL_MIN_REST_DURATION_S  = 5      # consecutive slow seconds before closing a rep
```

If reps are being split in two, increase `INTERVAL_MIN_REST_DURATION_S`. If continuous
runs are being falsely flagged, increase `INTERVAL_REST_RATIO_THRESHOLD` or decrease
`INTERVAL_SPEED_THRESHOLD_MPS`.

## File structure

```
Strava-Analysis/
├── strava_compile.py       # main script
├── YYYY-MM-DD_strava.csv   # output (gitignored)
└── README.md
```

Decompressed `.fit` files are written to `/tmp/strava_fit/` and are not kept after the run.
The Strava export zip itself stays in your Downloads folder and is not modified.