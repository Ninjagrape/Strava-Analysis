import argparse
import bisect
import concurrent.futures
import gzip
import os
import tempfile
import time
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

SEMICIRCLES_TO_DEG = 180.0 / (2 ** 31)

# GPS polyline density scales with run distance (like fit_distance_stream) so a
# long run keeps the same per-km resolution as a short one, rather than being
# squeezed into a flat point cap. Floored for tiny runs, hard-capped for huge ones.
GPS_POINTS_PER_KM = 150     # match stream spatial resolution (~every 7 m)
GPS_MIN_POINTS    = 75      # floor so very short runs still draw smoothly
GPS_MAX_POINTS    = 2500    # hard cap for very long runs (kicks in past ~17 km)


def _polyline_point_budget(distance_m: float) -> int:
    """Target polyline point count for a run's distance, clamped to [floor, cap]."""
    target = round(GPS_POINTS_PER_KM * distance_m / 1000.0)
    return max(GPS_MIN_POINTS, min(GPS_MAX_POINTS, target))


def _simplify_polyline(coords: list, max_points: int = GPS_MAX_POINTS) -> list:
    """Downsample lat/lon coords to at most max_points via uniform stride."""
    if not coords:
        return []
    n = len(coords)
    if n <= max_points:
        return [[round(lat, 5), round(lon, 5)] for lat, lon in coords]
    step = (n - 1) / (max_points - 1)
    result = []
    for i in range(max_points):
        idx = min(int(round(i * step)), n - 1)
        lat, lon = coords[idx]
        result.append([round(lat, 5), round(lon, 5)])
    return result


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
PACE_ZONE_THRESHOLD_MPS       = None   # set to your threshold pace in m/s (e.g. 3.33 = 5:00/km); None = use activity avg speed
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


def _per_km_splits(track: list[tuple], elev_stream: list = None,
                   cad_stream: list = None) -> list[dict]:
    """
    Compute per-km split times by interpolating the cumulative distance track,
    plus per-km elevation gain/loss and average cadence.
    Returns [{km: N, time_s, gain_m, loss_m, cad}, ...] for each complete km.
    gain_m/loss_m/cad are omitted when the underlying stream has no data.
    """
    if len(track) < 2:
        return []
    start_dist = track[0][1]
    total_dist = track[-1][1] - start_dist
    if total_dist < 1000:
        return []

    elev_by_t = {t: v for t, v in (elev_stream or []) if v is not None}
    cad_by_t  = {t: v for t, v in (cad_stream or []) if v is not None}
    have_elev = bool(elev_by_t)
    have_cad  = bool(cad_by_t)

    n_km = int(total_dist // 1000)            # complete kms only
    gain    = [0.0] * n_km
    loss    = [0.0] * n_km
    cad_sum = [0.0] * n_km
    cad_cnt = [0]   * n_km

    # Accumulate elevation deltas and cadence into the km each sample falls in.
    prev_alt = None
    for epoch, dist in track:
        km_idx = int((dist - start_dist) // 1000)   # 0-based km bucket
        in_range = 0 <= km_idx < n_km
        if have_elev and epoch in elev_by_t:
            alt = elev_by_t[epoch]
            if prev_alt is not None and in_range:
                d = alt - prev_alt
                if d > 0:
                    gain[km_idx] += d
                else:
                    loss[km_idx] -= d
            prev_alt = alt
        if have_cad and in_range and epoch in cad_by_t:
            cad_sum[km_idx] += cad_by_t[epoch]
            cad_cnt[km_idx] += 1

    splits = []
    prev_crossing_epoch = track[0][0]
    km = 1
    target = start_dist + 1000.0

    for i in range(1, len(track)):
        epoch, dist = track[i]
        while dist >= target:
            ep0, d0 = track[i - 1]
            if dist > d0:
                frac = (target - d0) / (dist - d0)
                crossing_epoch = ep0 + frac * (epoch - ep0)
            else:
                crossing_epoch = epoch
            split_time = crossing_epoch - prev_crossing_epoch
            if split_time > 0:
                s = {"km": km, "time_s": round(split_time, 1)}
                idx = km - 1
                if have_elev and idx < n_km:
                    s["gain_m"] = round(gain[idx])
                    s["loss_m"] = round(loss[idx])
                if have_cad and idx < n_km and cad_cnt[idx] > 0:
                    s["cad"] = round(cad_sum[idx] / cad_cnt[idx])
                splits.append(s)
            prev_crossing_epoch = crossing_epoch
            km += 1
            target = start_dist + km * 1000.0
            if target > track[-1][1] + 1:
                break

    return splits

# Over-distance chart sampling: take a fixed number of samples *per km* so short
# and long runs get the same spatial resolution (rather than the same total count,
# which over-sampled short runs). STREAM_MAX_POINTS caps very long runs.
STREAM_POINTS_PER_KM  = 150     # ~one sample every 7 m
STREAM_MAX_POINTS     = 3000    # hard cap (kicks in past ~20 km)
STREAM_PACE_WINDOW_M  = 50.0    # min look-ahead distance for instantaneous pace (noise control)
GEO_GAP_MAX_M         = 60.0    # beyond this gap between fixes, snap the cursor rather than interpolate a chord (avoids cutting across a GPS dropout/pause)

def _distance_stream(track: list, elev_stream: list, hr_stream: list,
                     cad_stream: list, pos_stream: list) -> str:
    """
    Build an over-distance series for per-run charts, sampled at a constant
    ~STREAM_POINTS_PER_KM samples per km (uniform spatial resolution).
    track:       [(epoch_s, cumulative_dist_m)]
    elev_stream: [(epoch_s, altitude_m or None)]
    hr_stream:   [(epoch_s, hr_bpm or None)]
    cad_stream:  [(epoch_s, cadence_spm or None)]   (already doubled to both feet)
    pos_stream:  [(epoch_s, lat_deg or None, lon_deg or None)]
    Returns JSON list of points: {"d": km, "pace": s/km, "elev": m, "hr": bpm,
                                  "cad": spm, "lat": deg, "lon": deg}
    lat/lon let the per-run chart cursor map a distance back to a map position.
    Keys are omitted when the underlying stream has no data at all.
    """
    if len(track) < 2:
        return json.dumps([])

    start_dist = track[0][1]
    total_dist = track[-1][1] - start_dist
    if total_dist <= 0:
        return json.dumps([])

    # lookups by epoch for the optional streams
    elev_by_t = {t: v for t, v in elev_stream if v is not None}
    hr_by_t   = {t: v for t, v in hr_stream if v is not None}
    cad_by_t  = {t: v for t, v in cad_stream if v is not None}
    pos_by_t  = {t: (la, lo) for t, la, lo in pos_stream if la is not None and lo is not None}
    have_elev = bool(elev_by_t)
    have_hr   = bool(hr_by_t)
    have_cad  = bool(cad_by_t)
    have_pos  = bool(pos_by_t)

    # Distance-indexed GPS fixes. Each emitted point gets an on-route coordinate by
    # interpolating between the surrounding recorded fixes, so the map cursor tracks
    # at full GPS resolution instead of only on the ~quarter of samples whose epoch
    # happens to coincide with a position record.
    dist_by_epoch = {t: d for t, d in track}
    geo = sorted(
        (dist_by_epoch[t], la, lo)
        for t, la, lo in pos_stream
        if la is not None and lo is not None and t in dist_by_epoch
    )
    geo_d = [g[0] for g in geo]

    def pos_at(dist_m: float):
        """Interpolate a lat/lon on the recorded track at a cumulative distance.

        Snaps to the nearer fix (rather than cutting a chord) when the bracketing
        fixes straddle a GPS dropout or pause wider than GEO_GAP_MAX_M.
        """
        if not geo:
            return None
        k = bisect.bisect_left(geo_d, dist_m)
        if k <= 0:
            return geo[0][1], geo[0][2]
        if k >= len(geo):
            return geo[-1][1], geo[-1][2]
        d0, la0, lo0 = geo[k - 1]
        d1, la1, lo1 = geo[k]
        if d1 - d0 > GEO_GAP_MAX_M:
            return (la0, lo0) if (dist_m - d0) <= (d1 - dist_m) else (la1, lo1)
        f = (dist_m - d0) / (d1 - d0) if d1 > d0 else 0.0
        return la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f

    # Distance-indexed elevation, interpolated at full resolution like position above.
    # Elevation is recorded on only a fraction of samples, so attaching it by exact
    # epoch leaves most distance-resampled points without an altitude; interpolating
    # by distance restores a continuous elevation profile.
    elev_geo = sorted(
        (dist_by_epoch[t], v)
        for t, v in elev_stream
        if v is not None and t in dist_by_epoch
    )
    elev_geo_d = [e[0] for e in elev_geo]

    def elev_at(dist_m: float):
        """Interpolate altitude at a cumulative distance, snapping to the nearer fix
        across gaps wider than GEO_GAP_MAX_M (consistent with pos_at)."""
        if not elev_geo:
            return None
        k = bisect.bisect_left(elev_geo_d, dist_m)
        if k <= 0:
            return elev_geo[0][1]
        if k >= len(elev_geo):
            return elev_geo[-1][1]
        d0, v0 = elev_geo[k - 1]
        d1, v1 = elev_geo[k]
        if d1 - d0 > GEO_GAP_MAX_M:
            return v0 if (dist_m - d0) <= (d1 - dist_m) else v1
        f = (dist_m - d0) / (d1 - d0) if d1 > d0 else 0.0
        return v0 + (v1 - v0) * f

    n = len(track)
    # Sample spacing in metres: target density, widened if it would blow the cap.
    spacing = max(1000.0 / STREAM_POINTS_PER_KM, total_dist / STREAM_MAX_POINTS)

    def build_point(i: int) -> dict:
        epoch, dist_m = track[i]
        # instantaneous pace from a forward window of at least STREAM_PACE_WINDOW_M
        j = i
        while j + 1 < n and (track[j][1] - dist_m) < STREAM_PACE_WINDOW_M:
            j += 1
        dd = track[j][1] - dist_m
        dt = track[j][0] - epoch
        pace = (dt / (dd / 1000.0)) if dd > 0 and dt > 0 else None
        # clamp implausible pace (e.g. GPS jitter while stationary)
        if pace is not None and (pace < 120 or pace > 1200):
            pace = None
        p = {"d": round(dist_m / 1000.0, 3), "t": round(epoch - track[0][0], 1)}
        if pace is not None:
            p["pace"] = round(pace, 1)
        if have_elev:
            ev = elev_at(dist_m)
            if ev is not None:
                p["elev"] = round(ev, 1)
        if have_hr and epoch in hr_by_t:
            p["hr"] = round(hr_by_t[epoch])
        if have_cad and epoch in cad_by_t:
            p["cad"] = round(cad_by_t[epoch])
        if have_pos:
            ll = pos_at(dist_m)
            if ll is not None:
                p["lat"] = round(ll[0], 5)
                p["lon"] = round(ll[1], 5)
        return p

    pts = []
    next_dist = start_dist
    emitted_idx = -1
    for i in range(n):
        if track[i][1] >= next_dist or i == n - 1:
            if i != emitted_idx:
                pts.append(build_point(i))
                emitted_idx = i
            # advance the threshold past the current distance to keep the spacing
            while next_dist <= track[i][1]:
                next_dist += spacing
    return json.dumps(pts)

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

def _pace_zone_secs(speed_stream: list, threshold_mps: float) -> str:
    """
    Tally seconds spent in each of 5 pace zones from the per-record speed stream.
    Zones are defined as fractions of threshold speed (Z4 = threshold).
    Returns a JSON list [z1_s, z2_s, z3_s, z4_s, z5_s].
    """
    if not threshold_mps or threshold_mps <= 0 or len(speed_stream) < 2:
        return json.dumps([0, 0, 0, 0, 0])

    # speed multipliers relative to threshold (faster = higher)
    # Z1 <0.77, Z2 0.77-0.87, Z3 0.87-0.93, Z4 0.93-1.03, Z5 >1.03
    bounds = [0.77, 0.87, 0.93, 1.03]
    secs = [0.0, 0.0, 0.0, 0.0, 0.0]

    for i in range(1, len(speed_stream)):
        t_prev, _ = speed_stream[i - 1]
        t_cur, sp = speed_stream[i]
        dt = t_cur - t_prev
        if dt <= 0 or dt > 30 or sp is None:  # skip gaps/pauses
            continue
        frac = sp / threshold_mps
        if frac < bounds[0]:
            secs[0] += dt
        elif frac < bounds[1]:
            secs[1] += dt
        elif frac < bounds[2]:
            secs[2] += dt
        elif frac < bounds[3]:
            secs[3] += dt
        else:
            secs[4] += dt

    return json.dumps([round(s, 1) for s in secs])

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
        # pace zones (seconds per zone, JSON) from per-record speed stream
        "fit_pace_zone_secs": None,
        # per-km splits (interpolated from record track)
        "fit_km_splits": None,
        # GPS polyline [[lat, lon], ...] downsampled
        "fit_gps_polyline": None,
        # over-distance metric stream for per-run charts
        "fit_distance_stream": None,
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
    track = []          # (epoch_s, cumulative_dist_m)
    speed_stream = []   # (epoch_s, speed_mps) for pace-zone tally
    gps_coords = []     # (lat_deg, lon_deg)
    elev_stream = []    # (epoch_s, altitude_m)
    hr_stream   = []    # (epoch_s, hr_bpm)
    cad_stream  = []    # (epoch_s, cadence_spm, both feet)
    pos_stream  = []    # (epoch_s, lat_deg or None, lon_deg or None)

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
            sp = d.get("enhanced_speed")
            if sp is None:
                sp = d.get("speed")
            lat_sc = d.get("position_lat")
            lon_sc = d.get("position_long")
            lat = lat_sc * SEMICIRCLES_TO_DEG if lat_sc is not None else None
            lon = lon_sc * SEMICIRCLES_TO_DEG if lon_sc is not None else None
            if ts is not None and dm is not None:
                epoch = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
                track.append((epoch, float(dm)))
                speed_stream.append((epoch, float(sp) if sp is not None else None))
                pos_stream.append((epoch, lat, lon))
                # optional per-record metrics, keyed by the same epoch
                alt = d.get("enhanced_altitude")
                if alt is None:
                    alt = d.get("altitude")
                elev_stream.append((epoch, float(alt) if alt is not None else None))
                hr = d.get("heart_rate")
                hr_stream.append((epoch, float(hr) if hr is not None else None))
                cad = d.get("cadence")
                frac = d.get("fractional_cadence")
                if cad is not None:
                    cad_val = float(cad) + (float(frac) if frac is not None else 0.0)
                    cad_val *= 2  # running cadence is per-foot; double to both feet
                else:
                    cad_val = None
                cad_stream.append((epoch, cad_val))
            if lat is not None and lon is not None:
                gps_coords.append((lat, lon))

    stats["fit_record_count"]   = record_count
    stats["fit_splits"]         = json.dumps(laps)
    total_dist_m = (track[-1][1] - track[0][1]) if len(track) >= 2 else 0.0
    stats["fit_gps_polyline"]   = json.dumps(
        _simplify_polyline(gps_coords, _polyline_point_budget(total_dist_m)))
    stats["fit_distance_stream"] = _distance_stream(track, elev_stream, hr_stream, cad_stream, pos_stream)

    # Best efforts (sliding window over record track)
    efforts = _best_efforts(track)
    stats.update(efforts)

    # Per-km splits (interpolated from cumulative distance track)
    stats["fit_km_splits"] = json.dumps(_per_km_splits(track, elev_stream, cad_stream))

    # Pace zones from per-record speed stream
    if PACE_ZONE_THRESHOLD_MPS:
        threshold = PACE_ZONE_THRESHOLD_MPS
    else:
        # fall back to this activity's own average speed as a rough anchor
        threshold = stats["fit_avg_speed_mps"]
    stats["fit_pace_zone_secs"] = _pace_zone_secs(speed_stream, threshold)

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


# Keys that hold JSON-encoded streams; appended last so column order is stable.
_STREAM_KEYS = ("fit_splits", "fit_km_splits", "fit_gps_polyline", "fit_distance_stream")


def _fit_keys_from(stats: dict) -> list[str]:
    """Ordered parsed-stat column list: scalar fields first, stream fields last.

    parse_fit emits a mix of prefixes (fit_*, best_*, interval_best_*); take
    every key, not just fit_-prefixed ones, so no derived column is dropped.
    """
    scalars = [k for k in stats if k not in _STREAM_KEYS]
    return scalars + [k for k in _STREAM_KEYS if k in stats]


def _parse_one(task: tuple) -> tuple:
    """Worker: decompress (if needed) and parse one fit file.

    Runs in a separate process, so it takes/returns only picklable values.
    Returns (fit_id, stats|None, error_str|None, elapsed_s).
    """
    path_str, is_gz, tmp_dir_str = task
    path = Path(path_str)
    fit_id = extract_activity_id(path.name)
    start = time.perf_counter()
    try:
        fit_path = decompress_fit_gz(path, Path(tmp_dir_str)) if is_gz else path
        stats = parse_fit(fit_path)
        return (fit_id, stats, None, time.perf_counter() - start)
    except Exception as e:  # mirror the serial path: log and skip, never crash the pool
        return (fit_id, None, str(e), time.perf_counter() - start)


def load_prior_fit_cache(csv_dir: Path, activity_cols: set) -> dict:
    """Map fit_id -> {parsed column: value} from the most recent prior CSV.

    The enriched CSV is itself the cache: any activity already parsed there
    can be reused verbatim, so only genuinely new .fit files need parsing.
    Parsed columns are everything that is NOT a raw activities.csv column
    (fit_*, best_*, interval_best_* alike). Only rows carrying parsed data
    are cached.
    """
    cache: dict = {}
    candidates = sorted(csv_dir.glob("*_strava.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for prior in candidates:
        try:
            rows = load_activities_csv(prior)
        except Exception:
            continue
        for row in rows:
            fn = row.get("Filename", "")
            fit_id = extract_activity_id(fn) if fn else ""
            if not fit_id or fit_id in cache:
                continue
            fit_data = {k: v for k, v in row.items() if k not in activity_cols}
            if any(v not in (None, "") for v in fit_data.values()):
                cache[fit_id] = fit_data
        break  # most recent CSV is sufficient
    return cache


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
    parser.add_argument("--tmp",       default=str(Path(tempfile.gettempdir()) / "strava_fit"),
                        help="Temp dir for decompressed .fit files")
    parser.add_argument("--rebuild",   action="store_true",
                        help="Ignore the prior CSV cache and re-parse every .fit file")
    parser.add_argument("--workers",   type=int, default=None,
                        help="Parallel parse workers (default: CPU count)")
    parser.add_argument("--profile",   action="store_true",
                        help="Print decompress/parse timing breakdown")
    args = parser.parse_args()

    profile = args.profile or os.environ.get("STRAVA_PROFILE")

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
        # date_str = datetime.today().strftime("%Y-%m-%d")    # Using current date
        date_str = datetime.fromtimestamp(archive_path.stat().st_mtime).strftime("%Y-%m-%d")    # Using export file last edit date
        out_path = Path(__file__).parent / "csv_data" / f"{date_str}_strava.csv"
        
    activities_dir = export_dir / "activities"
    if not activities_dir.exists():
        activities_dir = export_dir

    print(f"Looking for .fit.gz files in: {activities_dir}")

    fit_gz_files = sorted(activities_dir.glob("*.fit.gz"))
    fit_files    = sorted(activities_dir.glob("*.fit"))
    print(f"Found {len(fit_gz_files)} .fit.gz and {len(fit_files)} .fit files")

    activities = load_activities_csv(csv_path)
    print(f"Loaded {len(activities)} rows from {csv_path.name}")

    # Incremental cache: reuse fit data already parsed into a prior CSV, so only
    # genuinely new .fit files need parsing. --rebuild forces a full re-parse.
    # Parsed columns are identified as those absent from activities.csv, so a
    # prior CSV's derived columns (fit_*, best_*, ...) are reused in full.
    csv_dir = Path(__file__).parent / "csv_data"
    activity_cols = set(activities[0].keys()) if activities else set()
    cache = {} if args.rebuild else load_prior_fit_cache(csv_dir, activity_cols)

    parsed = {}        # fit_id -> stats (cached or freshly parsed)
    to_parse = []      # (path, is_gz) for ids not covered by the cache
    seen = set()       # dedupe ids; .fit.gz wins over a bare .fit of the same id

    def consider(path: Path, is_gz: bool):
        fit_id = extract_activity_id(path.name)
        if fit_id in seen:
            return
        seen.add(fit_id)
        if fit_id in cache:
            parsed[fit_id] = cache[fit_id]
        else:
            to_parse.append((path, is_gz))

    for gz in fit_gz_files:
        consider(gz, True)
    for fit in fit_files:
        consider(fit, False)

    cached_count = len(parsed)
    workers = args.workers if args.workers and args.workers > 0 else (os.cpu_count() or 1)
    parse_times: list[tuple[float, str]] = []  # (elapsed_s, name) for --profile
    parse_start = time.perf_counter()

    if to_parse:
        if workers == 1 or len(to_parse) == 1:
            for path, is_gz in to_parse:
                fit_id, stats, err, elapsed = _parse_one((str(path), is_gz, str(tmp_dir)))
                parse_times.append((elapsed, path.name))
                if err:
                    print(f"  ERROR {path.name}: {err}")
                else:
                    parsed[fit_id] = stats
        else:
            tasks = [(str(path), is_gz, str(tmp_dir)) for path, is_gz in to_parse]
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
                for fit_id, stats, err, elapsed in ex.map(_parse_one, tasks):
                    if err:
                        print(f"  ERROR parsing {fit_id}: {err}")
                    else:
                        parsed[fit_id] = stats
                        parse_times.append((elapsed, fit_id))

    parse_wall = time.perf_counter() - parse_start
    print(f"Reused {cached_count} cached, parsed {len(to_parse)} new "
          f"(.fit total {len(parsed)})")

    if profile:
        print(f"[profile] parse stage: {parse_wall:.1f}s wall, "
              f"{workers} worker(s), {len(to_parse)} files parsed, "
              f"{cached_count} reused from cache")
        for elapsed, name in sorted(parse_times, reverse=True)[:5]:
            print(f"[profile]   slowest: {name} {elapsed:.2f}s")

    fit_keys = _fit_keys_from(next(iter(parsed.values()))) if parsed else []

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