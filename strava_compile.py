import argparse
import gzip
import os
import zipfile
import shutil
import csv
import json
from pathlib import Path
from datetime import datetime

try:
    from fitparse import FitFile
except ImportError:
    raise SystemExit("fitparse not installed. Run: pip install fitparse")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_strava_export(downloads_dir: Path) -> Path:
    """Find the most recent Strava export zip or folder in Downloads."""
    # Look for zip files first
    zips = sorted(downloads_dir.glob("export_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if zips:
        print(f"Found Strava export zip: {zips[0].name}")
        return zips[0]
    # Fall back to extracted folders
    folders = sorted(
        [d for d in downloads_dir.iterdir() if d.is_dir() and d.name.startswith("export_")],
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    if folders:
        print(f"Found Strava export folder: {folders[0].name}")
        return folders[0]
    raise FileNotFoundError(
        f"No Strava export (export_*.zip or export_*/) found in {downloads_dir}"
    )

def find_activities_csv(export_dir: Path) -> Path:
    """Find activities.csv inside the export folder."""
    candidate = export_dir / "activities.csv"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"activities.csv not found in {export_dir}")

def unzip_archive(archive_path: Path, out_dir: Path) -> Path:
    """If the strava export is a .zip, extract it first."""
    if archive_path.suffix == ".zip":
        print(f"Extracting zip archive to {out_dir} ...")
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(out_dir)
        # Find the subdirectory that contains activities.csv (the export root)
        for candidate in sorted(out_dir.rglob("activities.csv")):
            return candidate.parent
        # Fall back to first subdir if activities.csv not found yet
        subdirs = [d for d in out_dir.iterdir() if d.is_dir()]
        return subdirs[0] if subdirs else out_dir
    return archive_path

def decompress_fit_gz(gz_path: Path, dest_dir: Path) -> Path:
    fit_path = dest_dir / gz_path.stem
    if not fit_path.exists():
        with gzip.open(gz_path, "rb") as f_in, open(fit_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    return fit_path

# ---------------------------------------------------------------------------
# Best efforts
# ---------------------------------------------------------------------------

BEST_EFFORT_DISTANCES = {
    "400m":  400,
    "1/2mi": 804.672,
    "1km":   1_000.0,
    "1mi":   1_609.344,
    "2mi":   3_218.69,
    "5k":    5_000.0,
    "10k":   10_000.0,
    "15k":   15_000.0,
    "10mi":  16_093.4,
    "20k":   20_000.0,
    "half":  21_097.5,
}

def _seconds_to_pace(seconds: float, distance_m: float) -> str:
    """Convert elapsed seconds over a distance to a M:SS/km pace string."""
    pace_sec_per_km = seconds / (distance_m / 1000.0)
    minutes = int(pace_sec_per_km // 60)
    secs = int(round(pace_sec_per_km % 60))
    if secs == 60:
        minutes += 1
        secs = 0
    return f"{minutes}:{secs:02d}"

def _best_efforts(records: list[tuple[float, float]]) -> dict:
    """
    Sliding window best-effort calculator.

    records: list of (timestamp_epoch_s, cumulative_distance_m), sorted by time.
    Returns a dict with best_<label>_s and best_<label>_pace for each target.
    """
    result = {}
    if len(records) < 2:
        for label in BEST_EFFORT_DISTANCES:
            result[f"best_{label}_s"] = None
            result[f"best_{label}_pace"] = None
        return result

    total_dist = records[-1][1] - records[0][1]

    for label, target_m in BEST_EFFORT_DISTANCES.items():
        if total_dist < target_m:
            result[f"best_{label}_s"] = None
            result[f"best_{label}_pace"] = None
            continue

        best_s = None
        left = 0
        n = len(records)

        for right in range(1, n):
            # Advance left pointer while window covers the target distance
            while left < right:
                span_dist = records[right][1] - records[left][1]
                if span_dist >= target_m:
                    span_time = records[right][0] - records[left][0]
                    if span_time > 0 and (best_s is None or span_time < best_s):
                        best_s = span_time
                    left += 1
                else:
                    break

        if best_s is not None:
            result[f"best_{label}_s"] = round(best_s, 1)
            result[f"best_{label}_pace"] = _seconds_to_pace(best_s, target_m)
        else:
            result[f"best_{label}_s"] = None
            result[f"best_{label}_pace"] = None

    return result

# ---------------------------------------------------------------------------
# Interval rep detection
# ---------------------------------------------------------------------------

# Tuning constants - adjust these if detection is over/under-sensitive
INTERVAL_REST_RATIO_THRESHOLD = 0.20   # flag session as intervals if rest >= 20% of elapsed time
INTERVAL_SPEED_THRESHOLD_MPS  = 2.0    # below this = rest (approx 8:20/km)
INTERVAL_MIN_REST_DURATION_S  = 5      # consecutive seconds below threshold to end a rep

INTERVAL_DISTANCES = {
    "400m": 400.0,
    "800m": 800.0,
    "1km":  1_000.0,
    "1mi":  1_609.344,
}


def _detect_reps(track: list[tuple[float, float]]) -> list[dict]:
    """
    Segment a record-level track into effort reps by speed threshold.

    track: list of (epoch_s, cumulative_dist_m), sorted by time.
    Returns a list of rep dicts with keys: dist_m, time_s, pace (M:SS/km).
    """
    if len(track) < 2:
        return []

    reps = []
    in_rep = False
    rep_start_idx = 0
    rest_streak = 0   # consecutive low-speed records

    for i in range(1, len(track)):
        dt = track[i][0] - track[i - 1][0]
        dd = track[i][1] - track[i - 1][1]
        speed = dd / dt if dt > 0 else 0.0

        if speed >= INTERVAL_SPEED_THRESHOLD_MPS:
            if not in_rep:
                in_rep = True
                rep_start_idx = i - 1
            rest_streak = 0
        else:
            if in_rep:
                rest_streak += 1
                if rest_streak >= INTERVAL_MIN_REST_DURATION_S:
                    # Close rep at the point rest began
                    end_idx = i - rest_streak
                    if end_idx > rep_start_idx:
                        dist_m = track[end_idx][1] - track[rep_start_idx][1]
                        time_s = track[end_idx][0] - track[rep_start_idx][0]
                        if dist_m > 0 and time_s > 0:
                            reps.append({
                                "dist_m": round(dist_m, 1),
                                "time_s": round(time_s, 1),
                                "pace":   _seconds_to_pace(time_s, dist_m),
                            })
                    in_rep = False
                    rest_streak = 0

    # Close any rep still open at end of track
    if in_rep:
        dist_m = track[-1][1] - track[rep_start_idx][1]
        time_s = track[-1][0] - track[rep_start_idx][0]
        if dist_m > 0 and time_s > 0:
            reps.append({
                "dist_m": round(dist_m, 1),
                "time_s": round(time_s, 1),
                "pace":   _seconds_to_pace(time_s, dist_m),
            })

    return reps


def _interval_best_efforts(reps: list[dict]) -> dict:
    """
    For each standard interval distance, find the fastest rep that covers
    at least that distance. Returns best_<label>_s and best_<label>_pace columns.
    """
    result = {
        "interval_rep_count":     len(reps),
        "interval_rep_distances": json.dumps([r["dist_m"] for r in reps]),
    }
    for label in INTERVAL_DISTANCES:
        result[f"interval_best_{label}_s"]    = None
        result[f"interval_best_{label}_pace"] = None

    for label, target_m in INTERVAL_DISTANCES.items():
        best_s = None
        for rep in reps:
            if rep["dist_m"] >= target_m and (best_s is None or rep["time_s"] < best_s):
                best_s = rep["time_s"]
        if best_s is not None:
            result[f"interval_best_{label}_s"]    = best_s
            result[f"interval_best_{label}_pace"] = _seconds_to_pace(best_s, target_m)

    return result

# -----------
# Build CSV
# -----------

def parse_fit(fit_path: Path) -> dict:
    stats = {
        "fit_file": fit_path.stem,
        "fit_sport": None,
        "fit_start_time": None,
        "fit_total_distance_km": None,
        "fit_moving_time_s": None,
        "fit_elapsed_time_s": None,
        "fit_avg_speed_mps": None,
        "fit_max_speed_mps": None,
        "fit_avg_heart_rate": None,
        "fit_max_heart_rate": None,
        "fit_avg_cadence": None,
        "fit_total_ascent_m": None,
        "fit_total_descent_m": None,
        "fit_avg_power": None,
        "fit_total_calories": None,
        "fit_avg_stride_length": None,
        "fit_training_stress_score": None,
        "fit_record_count": 0,
        "fit_splits": [],
        # best efforts
        "best_1km_s": None, "best_1km_pace": None,
        "best_1mi_s": None, "best_1mi_pace": None,
        "best_5k_s":  None, "best_5k_pace":  None,
        "best_10k_s": None, "best_10k_pace": None,
        "best_half_s": None, "best_half_pace": None,
        # interval rep detection (populated only for flagged sessions)
        "interval_rep_count":          None,
        "interval_rep_distances":      None,
        "interval_best_400m_s":        None, "interval_best_400m_pace": None,
        "interval_best_800m_s":        None, "interval_best_800m_pace": None,
        "interval_best_1km_s":         None, "interval_best_1km_pace":  None,
        "interval_best_1mi_s":         None, "interval_best_1mi_pace":  None,
    }

    try:
        ff = FitFile(str(fit_path))
        messages = list(ff.get_messages())
    except Exception as e:
        stats["fit_parse_error"] = str(e)
        return stats

    record_count = 0
    laps  = []
    track = []  # (epoch_s, cumulative_dist_m)

    # Session-level elapsed/timer times - read first so we can flag intervals
    session_elapsed = None
    session_timer   = None

    for msg in messages:
        name = msg.name

        if name == "session":
            d = {f.name: f.value for f in msg.fields}
            stats["fit_sport"]                 = str(d.get("sport", ""))
            stats["fit_start_time"]            = str(d.get("start_time", ""))
            dist = d.get("total_distance")
            stats["fit_total_distance_km"]     = round(dist / 1000, 4) if dist else None
            stats["fit_moving_time_s"]         = d.get("total_timer_time")
            stats["fit_elapsed_time_s"]        = d.get("total_elapsed_time")
            stats["fit_avg_speed_mps"]         = d.get("avg_speed")
            stats["fit_max_speed_mps"]         = d.get("max_speed")
            stats["fit_avg_heart_rate"]        = d.get("avg_heart_rate")
            stats["fit_max_heart_rate"]        = d.get("max_heart_rate")
            stats["fit_avg_cadence"]           = d.get("avg_running_cadence") or d.get("avg_cadence")
            stats["fit_total_ascent_m"]        = d.get("total_ascent")
            stats["fit_total_descent_m"]       = d.get("total_descent")
            stats["fit_avg_power"]             = d.get("avg_power")
            stats["fit_total_calories"]        = d.get("total_calories")
            stats["fit_training_stress_score"] = d.get("training_stress_score")
            session_elapsed = d.get("total_elapsed_time")
            session_timer   = d.get("total_timer_time")

        elif name == "lap":
            d = {f.name: f.value for f in msg.fields}
            dist = d.get("total_distance")
            lap = {
                "dist_km":       round(dist / 1000, 3) if dist else None,
                "time_s":        d.get("total_timer_time"),
                "avg_speed_mps": d.get("avg_speed"),
                "avg_hr":        d.get("avg_heart_rate"),
                "avg_cadence":   d.get("avg_running_cadence") or d.get("avg_cadence"),
                "ascent_m":      d.get("total_ascent"),
                "descent_m":     d.get("total_descent"),
            }
            if lap["dist_km"] and lap["time_s"] and lap["dist_km"] > 0:
                lap["pace_min_km"] = round(lap["time_s"] / 60 / lap["dist_km"], 3)
            laps.append(lap)

        elif name == "record":
            record_count += 1
            d  = {f.name: f.value for f in msg.fields}
            ts = d.get("timestamp")
            dm = d.get("distance")
            if ts is not None and dm is not None:
                epoch = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
                track.append((epoch, float(dm)))

    stats["fit_record_count"] = record_count
    stats["fit_splits"]       = json.dumps(laps)

    # Best efforts (sliding window over record track)
    efforts = _best_efforts(track)
    stats.update(efforts)

    # Interval detection - only if rest ratio is high enough
    is_interval_session = False
    if session_elapsed and session_timer and session_elapsed > 0:
        rest_ratio = (session_elapsed - session_timer) / session_elapsed
        is_interval_session = rest_ratio >= INTERVAL_REST_RATIO_THRESHOLD

    if is_interval_session:
        reps = _detect_reps(track)
        if len(reps) >= 2:
            stats.update(_interval_best_efforts(reps))

    return stats

def load_activities_csv(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def extract_activity_id(filename: str) -> str:
    stem = Path(filename).stem
    stem = Path(stem).stem
    return stem


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compile Strava .fit files with activities.csv")
    parser.add_argument("--downloads", default=str(Path.home() / "Downloads"),
                        help="Path to Downloads folder (default: ~/Downloads)")
    parser.add_argument("--archive",   default=None,
                        help="Override: path to Strava export folder or .zip")
    parser.add_argument("--csv",       default=None,
                        help="Override: path to activities.csv")
    parser.add_argument("--out",       default=None,
                        help="Override: output CSV path (default: alongside export, named YYYY-MM-DD_strava.csv)")
    parser.add_argument("--sport",     default="running",
                        help="Filter by sport (running/cycling/all). Default: running")
    parser.add_argument("--tmp",       default="/tmp/strava_fit",
                        help="Temp dir for decompressed .fit files")
    args = parser.parse_args()

    downloads_dir = Path(args.downloads)
    tmp_dir = Path(args.tmp)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: locate export
    if args.archive:
        archive_path = Path(args.archive)
    else:
        archive_path = find_strava_export(downloads_dir)

    # Determine the output directory (next to the export file/folder)
    export_parent = archive_path.parent

    # Step 2: extract zip if needed
    if archive_path.suffix == ".zip":
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir(exist_ok=True)
        export_dir = unzip_archive(archive_path, extract_dir)
    else:
        export_dir = archive_path

    # Step 3: locate activities.csv
    if args.csv:
        csv_path = Path(args.csv)
    else:
        csv_path = find_activities_csv(export_dir)

    # Step 4: determine output path
    if args.out:
        out_path = Path(args.out)
    else:
        date_str = datetime.today().strftime("%Y-%m-%d")
        out_path = Path(__file__).parent / f"{date_str}_strava.csv"
        
    activities_dir = export_dir / "activities"
    if not activities_dir.exists():
        activities_dir = export_dir

    print(f"Looking for .fit.gz files in: {activities_dir}")

    fit_gz_files = sorted(activities_dir.glob("*.fit.gz"))
    fit_files    = sorted(activities_dir.glob("*.fit"))
    print(f"Found {len(fit_gz_files)} .fit.gz and {len(fit_files)} .fit files")

    activities = load_activities_csv(csv_path)
    print(f"Loaded {len(activities)} rows from {csv_path.name}")

    parsed = {}

    for gz in fit_gz_files:
        fit_id = extract_activity_id(gz.name)
        try:
            fit_path = decompress_fit_gz(gz, tmp_dir)
            stats = parse_fit(fit_path)
            parsed[fit_id] = stats
        except Exception as e:
            print(f"  ERROR {gz.name}: {e}")

    for fit in fit_files:
        fit_id = extract_activity_id(fit.name)
        if fit_id not in parsed:
            try:
                stats = parse_fit(fit)
                parsed[fit_id] = stats
            except Exception as e:
                print(f"  ERROR {fit.name}: {e}")

    print(f"Successfully parsed {len(parsed)} .fit files")

    fit_keys = []
    if parsed:
        sample = next(iter(parsed.values()))
        fit_keys = [k for k in sample.keys() if k != "fit_splits"]
        fit_keys.append("fit_splits")

    enriched = []
    matched = 0

    for act in activities:
        fn = act.get("Filename", "")
        fit_id = extract_activity_id(fn) if fn else ""
        fit_data = parsed.get(fit_id, {})

        sport_filter = args.sport.lower()
        act_type = act.get("Activity Type", "").lower()
        fit_sport = fit_data.get("fit_sport", "").lower() if fit_data else ""

        if sport_filter != "all":
            if sport_filter not in act_type and sport_filter not in fit_sport:
                continue

        row = dict(act)
        for k in fit_keys:
            row[k] = fit_data.get(k, "")
        enriched.append(row)
        if fit_data:
            matched += 1

    print(f"Matched {matched}/{len(enriched)} activities to .fit files")

    if not enriched:
        print("No rows to write. Check your --sport filter or file paths.")
        return

    fieldnames = list(enriched[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enriched)

    print(f"\nDone. Enriched CSV written to: {out_path}")
    print(f"Rows: {len(enriched)}, Columns: {len(fieldnames)}")

    new_cols = [k for k in fieldnames if k.startswith("fit_")]
    print(f"\nNew columns from .fit files ({len(new_cols)}):")
    for c in new_cols:
        print(f"  {c}")


if __name__ == "__main__":
    main()