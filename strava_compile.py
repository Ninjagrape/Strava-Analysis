"""
strava_compile.py

Unzips Strava .fit.gz files, parses them, and merges with activities.csv
into a single enriched CSV.

Usage:
    python strava_compile.py --archive /path/to/strava_export --csv activities.csv --out enriched.csv

Strava export folder structure expected:
    export_xxx/
        activities.csv
        activities/
            123456789.fit.gz
            123456789.gpx.gz   (ignored)
            ...
"""

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

def unzip_archive(archive_path: Path, out_dir: Path):
    """If the strava export is a .zip, extract it first."""
    if archive_path.suffix == ".zip":
        print(f"Extracting zip archive to {out_dir} ...")
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(out_dir)
        # find the extracted folder
        subdirs = [d for d in out_dir.iterdir() if d.is_dir()]
        return subdirs[0] if subdirs else out_dir
    return archive_path  # already a folder


def decompress_fit_gz(gz_path: Path, dest_dir: Path) -> Path:
    """Decompress a .fit.gz file, return path to .fit file."""
    fit_path = dest_dir / gz_path.stem  # removes .gz, leaves .fit
    if not fit_path.exists():
        with gzip.open(gz_path, "rb") as f_in, open(fit_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    return fit_path


def parse_fit(fit_path: Path) -> dict:
    """
    Parse a .fit file and return aggregated stats as a dict.
    Pulls from the session message (summary) where possible,
    falling back to record-level aggregation.
    """
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
        "fit_splits": [],       # list of per-km lap dicts if present
    }

    try:
        ff = FitFile(str(fit_path))
        messages = list(ff.get_messages())
    except Exception as e:
        stats["fit_parse_error"] = str(e)
        return stats

    record_count = 0
    laps = []

    for msg in messages:
        name = msg.name

        if name == "session":
            d = {f.name: f.value for f in msg.fields}
            stats["fit_sport"]           = str(d.get("sport", ""))
            stats["fit_start_time"]      = str(d.get("start_time", ""))
            dist = d.get("total_distance")
            stats["fit_total_distance_km"] = round(dist / 1000, 4) if dist else None
            stats["fit_moving_time_s"]   = d.get("total_timer_time")
            stats["fit_elapsed_time_s"]  = d.get("total_elapsed_time")
            stats["fit_avg_speed_mps"]   = d.get("avg_speed")
            stats["fit_max_speed_mps"]   = d.get("max_speed")
            stats["fit_avg_heart_rate"]  = d.get("avg_heart_rate")
            stats["fit_max_heart_rate"]  = d.get("max_heart_rate")
            stats["fit_avg_cadence"]     = d.get("avg_running_cadence") or d.get("avg_cadence")
            stats["fit_total_ascent_m"]  = d.get("total_ascent")
            stats["fit_total_descent_m"] = d.get("total_descent")
            stats["fit_avg_power"]       = d.get("avg_power")
            stats["fit_total_calories"]  = d.get("total_calories")
            stats["fit_training_stress_score"] = d.get("training_stress_score")

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
            # compute pace min/km
            if lap["dist_km"] and lap["time_s"] and lap["dist_km"] > 0:
                lap["pace_min_km"] = round(lap["time_s"] / 60 / lap["dist_km"], 3)
            laps.append(lap)

        elif name == "record":
            record_count += 1

    stats["fit_record_count"] = record_count
    stats["fit_splits"] = json.dumps(laps)  # store as JSON string in CSV
    return stats


def load_activities_csv(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def extract_activity_id(filename: str) -> str:
    """Extract numeric activity ID from filename like 123456789.fit or 123456789.fit.gz"""
    stem = Path(filename).stem  # removes last extension
    stem = Path(stem).stem      # removes .fit if it was .fit.gz
    return stem


def match_fit_to_activity(activities: list[dict], fit_stats: dict, fit_id: str) -> dict | None:
    """
    Try to match a parsed fit file to an activities.csv row by activity ID
    embedded in the filename (Strava export uses activity ID as filename).
    Falls back to timestamp matching if needed.
    """
    for act in activities:
        # Strava CSV has a 'Filename' column like activities/123456789.fit.gz
        fn = act.get("Filename", "")
        if fit_id in fn:
            return act
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compile Strava .fit files with activities.csv")
    parser.add_argument("--archive", required=True, help="Path to Strava export folder or .zip")
    parser.add_argument("--csv",     required=True, help="Path to activities.csv")
    parser.add_argument("--out",     default="enriched_activities.csv", help="Output CSV path")
    parser.add_argument("--sport",   default="running", help="Filter by sport (running/cycling/all). Default: running")
    parser.add_argument("--tmp",     default="/tmp/strava_fit", help="Temp dir for decompressed .fit files")
    args = parser.parse_args()

    archive_path = Path(args.archive)
    csv_path = Path(args.csv)
    out_path = Path(args.out)
    tmp_dir = Path(args.tmp)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: extract zip if needed
    if archive_path.suffix == ".zip":
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir(exist_ok=True)
        export_dir = unzip_archive(archive_path, extract_dir)
    else:
        export_dir = archive_path

    activities_dir = export_dir / "activities"
    if not activities_dir.exists():
        # some exports have a flat structure
        activities_dir = export_dir

    print(f"Looking for .fit.gz files in: {activities_dir}")

    # Step 2: find all .fit.gz files
    fit_gz_files = sorted(activities_dir.glob("*.fit.gz"))
    fit_files    = sorted(activities_dir.glob("*.fit"))
    print(f"Found {len(fit_gz_files)} .fit.gz and {len(fit_files)} .fit files")

    # Step 3: load activities CSV
    activities = load_activities_csv(csv_path)
    print(f"Loaded {len(activities)} rows from {csv_path.name}")

    # Step 4: parse each fit file
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

    # Step 5: merge with activities CSV
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

        # filter by sport
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

    # Step 6: write output
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

    # Quick summary of new columns added
    new_cols = [k for k in fieldnames if k.startswith("fit_")]
    print(f"\nNew columns from .fit files ({len(new_cols)}):")
    for c in new_cols:
        print(f"  {c}")


if __name__ == "__main__":
    main()