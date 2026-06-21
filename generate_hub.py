#!/usr/bin/env python3
"""
generate_hub.py
Generates dashboards/TrainingHub.html — a self-contained training hub with:
  - Overview panel: summary stats, fitness indicators, weekly mileage
  - Runs panel: searchable run list with per-run analytics

No external HTML files are required. Imports shared logic from
generate_dashboards and generate_analytics.
"""

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

from generate_dashboards import (
    load_rows, fmt_pace, fmt_time, ga_time, is_interval,
    fmt_pace_from_s_per_km, weekly_mileage, render_spark,
    BEST_EFFORTS_CSS, GOAL_CSS,
    body_best_efforts, body_goal_dashboard,
)
from generate_analytics import (
    num, parse_date, session_loads, daily_series, ctl_atl_tsb,
    ANALYTICS_PANEL_CSS, body_analytics,
)


# ---------------------------------------------------------------------------
# Best effort column → display metadata
# ---------------------------------------------------------------------------

BEST_EFFORT_DEFS = [
    ("400m",          "best_400m_s",   400.0),
    ("800m",          "best_1/2mi_s",  804.672),
    ("1 km",          "best_1km_s",    1_000.0),
    ("1 mile",        "best_1mi_s",    1_609.344),
    ("2 miles",       "best_2mi_s",    3_218.69),
    ("5K",            "best_5k_s",     5_000.0),
    ("10K",           "best_10k_s",    10_000.0),
    ("15K",           "best_15k_s",    15_000.0),
    ("Half marathon", "best_half_s",   21_097.5),
]


# ---------------------------------------------------------------------------
# Heatmap helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def _interpolate_polyline(poly: list, step_m: float = 12.0) -> list:
    """Insert intermediate lat/lon points so no segment is longer than step_m metres.
    This fills the gaps left by the 250-point GPS simplification so the heatmap
    renders as a continuous line rather than discrete blobs when zoomed in."""
    if len(poly) < 2:
        return list(poly)
    result = [poly[0]]
    for i in range(1, len(poly)):
        lat0, lon0 = poly[i - 1][0], poly[i - 1][1]
        lat1, lon1 = poly[i][0], poly[i][1]
        mid_lat = (lat0 + lat1) / 2
        dlat_m = (lat1 - lat0) * 111_000
        dlon_m = (lon1 - lon0) * 111_000 * math.cos(math.radians(mid_lat))
        dist_m = math.hypot(dlat_m, dlon_m)
        if dist_m > step_m:
            n = int(dist_m / step_m)
            for j in range(1, n + 1):
                frac = j / (n + 1)
                result.append([lat0 + frac * (lat1 - lat0), lon0 + frac * (lon1 - lon0)])
        result.append([lat1, lon1])
    return result


def _heatmap_points(runs: list[dict], max_dist_km: float = 150.0) -> list:
    """Build heatmap points where each run contributes at most one point per ~100 m grid cell.
    This makes density proportional to distinct-run frequency, not raw GPS point count,
    so oval laps don't inflate an area to match a frequently-run local route."""
    centroids = []
    for r in runs:
        poly = r.get("gps_polyline", [])
        if len(poly) >= 5:
            lat_c = sum(p[0] for p in poly) / len(poly)
            lon_c = sum(p[1] for p in poly) / len(poly)
            centroids.append((lat_c, lon_c))

    if not centroids:
        return []

    lats = sorted(c[0] for c in centroids)
    lons = sorted(c[1] for c in centroids)
    med_lat = lats[len(lats) // 2]
    med_lon = lons[len(lons) // 2]

    points = []
    for r in runs:
        poly = r.get("gps_polyline", [])
        if len(poly) < 5:
            continue
        lat_c = sum(p[0] for p in poly) / len(poly)
        lon_c = sum(p[1] for p in poly) / len(poly)
        if _haversine_km(med_lat, med_lon, lat_c, lon_c) > max_dist_km:
            continue
        # Interpolate to fill gaps (250-pt simplification leaves ~20 m spacing).
        dense = _interpolate_polyline(poly, step_m=12.0)
        # Deduplicate: each ~11 m grid cell (4 decimal places) counts once per run.
        # Fine enough for smooth rendering; coarse enough to collapse repeated laps.
        seen: set = set()
        for p in dense:
            key = (round(p[0], 4), round(p[1], 4))
            if key not in seen:
                seen.add(key)
                points.append(p)

    return points


# ---------------------------------------------------------------------------
# Pace zone helpers
# ---------------------------------------------------------------------------

def _compute_threshold(rows: list[dict]) -> float | None:
    """
    Derive threshold speed (m/s) from the best 5K time across all runs.
    5K pace is a reliable proxy for lactate threshold for recreational runners.
    """
    best = None
    for row in rows:
        s = num(row, "best_5k_s")
        if s and s > 0:
            if best is None or s < best:
                best = s
    if best and best > 0:
        return round(5000.0 / best, 4)  # m/s
    return None


def _compute_pace_zones(km_splits: list[dict], threshold_mps: float) -> list[float]:
    """
    Time in each of 5 pace zones, computed from per-km splits.
    Each split covers exactly 1000 m, so time_s == seconds-per-km.
    Zone boundaries use the same speed-fraction-of-threshold model as Strava/TrainingPeaks.
    """
    if not threshold_mps or threshold_mps <= 0 or not km_splits:
        return [0.0, 0.0, 0.0, 0.0, 0.0]
    bounds = [0.77, 0.87, 0.93, 1.03]
    secs = [0.0, 0.0, 0.0, 0.0, 0.0]
    for s in km_splits:
        t = s.get("time_s")
        if not t or t <= 0:
            continue
        speed = 1000.0 / t       # m/s (1 km in t seconds)
        frac = speed / threshold_mps
        if frac < bounds[0]:
            secs[0] += t
        elif frac < bounds[1]:
            secs[1] += t
        elif frac < bounds[2]:
            secs[2] += t
        elif frac < bounds[3]:
            secs[3] += t
        else:
            secs[4] += t
    return [round(s, 1) for s in secs]


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def _build_runs(rows: list[dict], threshold_mps: float | None) -> list[dict]:
    """Convert enriched CSV rows to per-run dicts for the JS frontend."""
    runs = []
    for row in rows:
        dt = parse_date(row)
        if not dt:
            continue
        dist_m = num(row, "Distance") or 0
        dist_km = dist_m / 1000
        if dist_km <= 0:
            continue
        moving_s = num(row, "Moving Time") or 0
        gain = num(row, "Elevation Gain") or 0
        ga_s = ga_time(moving_s, gain, dist_km) if moving_s > 0 else 0.0

        # Lap splits (from FIT laps — kept for cadence/gain info)
        try:
            lap_splits = json.loads(row.get("fit_splits") or "[]")
        except (json.JSONDecodeError, TypeError):
            lap_splits = []
        for idx, lap in enumerate(lap_splits):
            t = lap.get("time_s")
            d = lap.get("dist_km")
            lap["idx"] = idx + 1
            lap["dist_str"] = f"{d:.2f}" if d else "—"
            lap["time_str"] = fmt_time(t) if t else "—"
            lap["pace_str"] = fmt_pace(t, d * 1000) if (t and d and d > 0) else "—"
            cad = lap.get("avg_cadence")
            lap["cadence_str"] = str(round(cad)) if cad else "—"
            asc = lap.get("ascent_m")
            lap["gain_str"] = f"+{round(asc)}m" if asc else "—"

        # Per-km splits (interpolated from record track in strava_compile.py)
        try:
            km_splits = json.loads(row.get("fit_km_splits") or "[]")
        except (json.JSONDecodeError, TypeError):
            km_splits = []
        for s in km_splits:
            t = s.get("time_s")
            if t and t > 0:
                mins = int(t // 60)
                secs_r = int(round(t % 60))
                if secs_r == 60:
                    mins += 1
                    secs_r = 0
                s["pace_str"] = f"{mins}:{secs_r:02d}/km"
                s["pace_s"] = round(t, 1)  # secs/km (= time_s since 1 km per split)
            else:
                s["pace_str"] = "—"
                s["pace_s"] = None

        # Pace zones: prefer km-split-based computation (correct units, personal threshold)
        pace_zones = _compute_pace_zones(km_splits, threshold_mps)

        # GPS polyline for map
        try:
            gps_polyline = json.loads(row.get("fit_gps_polyline") or "[]")
        except (json.JSONDecodeError, TypeError):
            gps_polyline = []

        # Over-distance metric stream
        try:
            dist_stream = json.loads(row.get("fit_distance_stream") or "[]")
        except (json.JSONDecodeError, TypeError):
            dist_stream = []

        # Per-run best efforts
        best_efforts = []
        for label, col, dist_be in BEST_EFFORT_DEFS:
            s = num(row, col)
            if s and s > 0:
                best_efforts.append({
                    "label": label,
                    "time":  fmt_time(s),
                    "pace":  fmt_pace(s, dist_be),
                })

        runs.append({
            "id":          len(runs),
            "name":        (row.get("Activity Name") or "Run").strip() or "Run",
            "description": (row.get("Activity Description") or "").strip(),
            "date_long":   dt.strftime("%d %b %Y"),
            "date_short":  dt.strftime("%b %d"),
            "weekday":     dt.strftime("%A"),
            "date_iso":    dt.strftime("%Y-%m-%d"),
            "dist_km":     round(dist_km, 2),
            "moving_s":    round(moving_s),
            "moving_str":  fmt_time(moving_s) if moving_s > 0 else "—",
            "pace_str":    fmt_pace(moving_s, dist_km * 1000) if moving_s > 0 else "—",
            "ga_pace_str": fmt_pace(ga_s, dist_km * 1000) if ga_s > 0 else "—",
            "gain":        round(gain),
            "descent":     round(num(row, "fit_total_descent_m") or 0),
            "cadence":     round(num(row, "fit_avg_cadence")) if num(row, "fit_avg_cadence") else None,
            "calories":    round(num(row, "fit_total_calories")) if num(row, "fit_total_calories") else None,
            "is_interval":  is_interval(row),
            "lap_splits":   lap_splits,
            "km_splits":    km_splits,
            "pace_zones":   pace_zones,
            "best_efforts": best_efforts,
            "gps_polyline": gps_polyline,
            "dist_stream": dist_stream,
        })

    runs.sort(key=lambda r: r["date_iso"], reverse=True)
    return runs


def _overview_stats(rows: list[dict], runs: list[dict]) -> dict:
    total_km = sum(r["dist_km"] for r in runs)
    total_s  = sum(r["moving_s"] for r in runs)
    today      = datetime.today().date()
    week_start = today - timedelta(days=today.weekday())
    this_week  = [r for r in runs if r["date_iso"] >= str(week_start)]
    longest    = max(runs, key=lambda r: r["dist_km"], default=None)

    loads   = session_loads(rows)
    daily   = daily_series(loads)
    fitness = ctl_atl_tsb(daily)
    cur     = fitness[-1] if fitness else {"ctl": 0.0, "atl": 0.0, "tsb": 0.0}
    tsb     = cur["tsb"]

    return {
        "total_runs":    len(runs),
        "total_km":      round(total_km, 1),
        "total_time":    fmt_time(total_s),
        "week_km":       round(sum(r["dist_km"] for r in this_week), 1),
        "week_runs":     len(this_week),
        "longest_km":    longest["dist_km"] if longest else 0,
        "longest_date":  longest["date_long"] if longest else "—",
        "ctl":           round(cur["ctl"], 1),
        "atl":           round(cur["atl"], 1),
        "tsb":           round(tsb, 1),
        "tsb_class":     "green" if tsb > 5 else ("amber" if tsb > -10 else "red"),
        "tsb_note":      "fresh" if tsb > 5 else ("building" if tsb > -10 else "fatigued"),
    }


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

HUB_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%}
body{font-family:system-ui,-apple-system,sans-serif;background:#1a1a1a;color:#eee}

.tab-bar{display:flex;align-items:center;background:#111;border-bottom:1px solid #2a2a2a;
  padding:0 1rem;height:44px;position:sticky;top:0;z-index:100;gap:2px}
.brand{font-size:14px;font-weight:700;color:#5cb85c;margin-right:14px;letter-spacing:-.02em;white-space:nowrap}
.tab{background:none;border:none;border-bottom:2px solid transparent;color:#555;padding:0 13px;
  height:44px;font-size:13px;cursor:pointer;transition:color .15s,border-color .15s}
.tab:hover{color:#ccc}
.tab.active{color:#eee;border-bottom-color:#5cb85c}

.panel{display:none}
.panel.active{display:flex;flex-direction:column}

/* Overview */
#panel-overview{height:calc(100vh - 44px);overflow-y:auto;padding:1rem}
.ov-section{margin-bottom:1.25rem}
.ov-section-title{font-size:11px;font-weight:600;color:#666;text-transform:uppercase;
  letter-spacing:.05em;margin:0 0 8px;padding-bottom:4px;border-bottom:1px solid #2a2a2a}
.ov-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.ov-card{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:12px 14px}
.ov-label{font-size:10px;color:#666;margin:0 0 4px}
.ov-value{font-size:20px;font-weight:700;color:#eee;margin:0;line-height:1.2}
.ov-sub{font-size:10px;color:#555;margin:3px 0 0}
.green{color:#5cb85c}.amber{color:#e0a020}.red{color:#d9534f}
.spark-wrap{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:10px 14px}
.spark-label{font-size:10px;color:#555;margin:0 0 6px}
.bars{display:flex;align-items:flex-end;gap:4px;height:48px}
.bar-col{display:flex;flex-direction:column;align-items:center;flex:1}
.bar-rect{width:100%;border-radius:2px 2px 0 0;background:#3a5a3a;min-height:2px}
.bar-rect.latest{background:#5cb85c}
.bar-wk{font-size:8px;color:#444;margin-top:3px;text-align:center}
.bar-km{font-size:8px;color:#666;margin-bottom:2px;text-align:center}

/* Runs panel */
#panel-runs{height:calc(100vh - 44px);overflow:hidden}
.runs-layout{display:flex;height:100%}
#run-list-pane{width:290px;min-width:220px;border-right:1px solid #2a2a2a;
  display:flex;flex-direction:column;overflow:hidden}
.run-search{padding:8px 10px;border-bottom:1px solid #2a2a2a;background:#111}
.run-search input{width:100%;background:#222;border:1px solid #3a3a3a;border-radius:6px;
  color:#eee;padding:6px 10px;font-size:12px;outline:none}
.run-search input::placeholder{color:#444}
.run-search input:focus{border-color:#5cb85c}
#run-list-items{overflow-y:auto;flex:1}

.run-item{padding:9px 12px;border-bottom:1px solid #1e1e1e;cursor:pointer;transition:background .1s}
.run-item:hover{background:#1f1f1f}
.run-item.active{background:#1b2a1b;border-left:3px solid #5cb85c;padding-left:9px}
.ri-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px}
.ri-date{font-size:10px;color:#555}
.ri-dist{font-size:14px;font-weight:700;color:#eee}
.ri-name{font-size:12px;color:#bbb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.ri-bottom{display:flex;align-items:center;gap:8px}
.ri-pace{font-size:11px;color:#777}
.ri-gain{font-size:10px;color:#4a9a4a}
.ri-tag{font-size:9px;background:#1e2e1e;color:#5cb85c;border-radius:4px;
  padding:1px 5px;border:1px solid #2a3a2a}

/* Run detail */
#run-detail-pane{flex:1;overflow-y:auto;padding:1.25rem}
.placeholder-msg{color:#333;font-size:13px;text-align:center;margin-top:4rem}
/* Run-detail top: name+stats on the left, Strava description on the right.
   The left column is pinned to the top; the description is bottom-aligned so the
   bottom of its last line (incl. the "more" button) sits on the stats-box bottom.
   Expanding the description grows the box upward into the space above it. */
.rd-top{display:flex;gap:1rem;margin-bottom:1.25rem}
.rd-left{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;align-self:flex-start}
.rd-header{margin-bottom:1rem}
.rd-name{font-size:20px;font-weight:700;color:#eee;margin:0 0 4px}
.rd-date{font-size:12px;color:#555}
.rd-desc{flex:0 1 360px;max-width:360px;align-self:flex-end;background:#1e1e1e;
  border:1px solid #2a2a2a;border-radius:8px;padding:8px 12px;font-size:12px;color:#aaa}
.rd-desc-text{white-space:pre-line;line-height:1.5;word-break:break-word}
.rd-desc-btn{background:none;border:none;color:#5cb85c;font-size:11px;font-weight:600;
  cursor:pointer;padding:3px 0 0}
.rd-desc-btn:hover{text-decoration:underline}
.rd-stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px}
.rds{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:10px 12px}
.rds-l{font-size:10px;color:#555;margin-bottom:3px}
.rds-v{font-size:15px;font-weight:600;color:#eee}
.rds-v-muted{font-size:13px;color:#777;font-weight:400}
.rd-section{margin-bottom:1.25rem}
.rd-section-title{font-size:11px;font-weight:600;color:#666;text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #2a2a2a}
.rd-table{width:100%;border-collapse:collapse;font-size:12px}
.rd-table th{font-size:10px;font-weight:600;color:#555;text-align:left;
  padding:4px 8px;border-bottom:1px solid #2a2a2a}
.rd-table th:not(:first-child){text-align:right}
.rd-table td{padding:6px 8px;border-bottom:1px solid #1e1e1e;color:#bbb}
.rd-table td:not(:first-child){text-align:right;font-variant-numeric:tabular-nums}
.rd-table tr:last-child td{border-bottom:none}
.pace-cell{color:#eee;font-weight:500}
.best-km{color:#5cb85c !important}
.leaflet-container,.leaflet-container *,.leaflet-container *::before,.leaflet-container *::after{box-sizing:content-box}

.dist-tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.dist-tab{background:#222;border:1px solid #3a3a3a;color:#888;font-size:11px;
  padding:4px 10px;border-radius:6px;cursor:pointer;transition:color .1s,border-color .1s}
.dist-tab:hover{color:#ccc}
.dist-tab.active{color:#eee;border-color:#5cb85c;background:#1b2a1b}
.dist-chart{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:8px;position:relative}
.dist-chart svg{display:block;width:100%;height:auto}
.dist-tip{position:absolute;pointer-events:none;background:#000;border:1px solid #3a3a3a;
  border-radius:6px;padding:4px 8px;font-size:11px;line-height:1.45;color:#eee;white-space:nowrap;
  transform:translate(-50%,calc(-100% - 10px));opacity:0;transition:opacity .08s;z-index:5}
.dist-tip b{color:#fff;font-weight:600}
.dist-tip .tip-d{color:#888}

/* Inline run map + "click to expand" affordance */
.rd-map-wrap{position:relative;margin-bottom:1.25rem;cursor:pointer}
.rd-map{height:460px;border-radius:8px;border:1px solid #3a3a3a}
.rd-map-hint{position:absolute;top:8px;right:8px;z-index:450;background:rgba(0,0,0,.6);
  color:#ddd;font-size:11px;padding:3px 9px;border-radius:6px;pointer-events:none;
  border:1px solid #3a3a3a}

/* Expanded analysis overlay (map + over-distance graph on one screen) */
.run-overlay{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:600;display:none}
.run-overlay.open{display:flex;justify-content:center}
.ro-box{display:flex;flex-direction:column;width:100%;max-width:1300px;height:100%;
  padding:12px 14px;gap:8px;overflow:hidden}
.ro-head{display:flex;justify-content:space-between;align-items:center;flex:0 0 auto}
.ro-title{font-size:15px;font-weight:600;color:#eee}
.ro-close{background:#222;border:1px solid #3a3a3a;color:#bbb;font-size:15px;line-height:1;
  width:30px;height:30px;border-radius:6px;cursor:pointer}
.ro-close:hover{color:#fff;border-color:#5cb85c}
/* Body: splits column on the left, map+graph fill the height on the right */
.ro-body{flex:1 1 auto;display:flex;gap:12px;min-height:0}
.ro-splits{flex:0 0 290px;overflow-y:auto;min-height:0}
.ro-splits .rd-table th,.ro-splits .rd-table td{padding:5px 5px}
.ro-main{flex:1 1 auto;display:flex;flex-direction:column;gap:8px;min-height:0}
.ro-map{flex:1 1 auto;min-height:200px;border-radius:8px;border:1px solid #3a3a3a}
.ro-tabs{flex:0 0 auto}
.run-overlay .dist-chart{flex:0 0 auto;cursor:crosshair}
.run-overlay .dist-chart svg{height:30vh}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

HUB_JS = r"""
var overviewMap = null;
var currentRunMap = null;
var selectedRunId = null;
// The expanded-analysis overlay owns the only map the chart cursor links to.
var overlayMap = null;
var overlayRunId = null;
var activeCursorMarker = null;   // moving dot that tracks the over-distance chart cursor

function initRunMap(r) {
  if (currentRunMap) { currentRunMap.remove(); currentRunMap = null; }
  if (!r || !r.gps_polyline || r.gps_polyline.length < 5 || typeof L === 'undefined') return;
  setTimeout(function() {
    try {
      var mapEl = document.getElementById('run-map-' + r.id);
      if (!mapEl) return;
      currentRunMap = L.map(mapEl, {zoomControl: true, preferCanvas: true});
      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: 'abcd', maxZoom: 19
      }).addTo(currentRunMap);
      var poly = L.polyline(r.gps_polyline, {color: '#5cb85c', weight: 3, opacity: 0.9}).addTo(currentRunMap);
      L.circleMarker(r.gps_polyline[0], {radius: 5, color: '#5cb85c', fillColor: '#fff', fillOpacity: 1, weight: 2}).addTo(currentRunMap);
      L.circleMarker(r.gps_polyline[r.gps_polyline.length - 1], {radius: 5, color: '#e07020', fillColor: '#e07020', fillOpacity: 1, weight: 2}).addTo(currentRunMap);
      // A plain click on the route opens the expanded map + graph analysis view.
      currentRunMap.on('click', function() { openRunOverlay(r.id); });
      currentRunMap.fitBounds(poly.getBounds(), {padding: [24, 24]});
    } catch(e) { console.warn('Run map init failed:', e); }
  }, 50);
}

// ── Expanded analysis overlay ───────────────────────────────────────────────

function openRunOverlay(runId) {
  // The inline map fires both Leaflet's click and the wrapper's onclick; ignore
  // the second call so we don't initialise the overlay map twice.
  if (document.getElementById('run-overlay').classList.contains('open')) return;
  var r = RUNS.find(function(x) { return x.id === runId; });
  if (!r || !r.gps_polyline || r.gps_polyline.length < 5 || typeof L === 'undefined') return;
  overlayRunId = runId;

  document.getElementById('ro-title').textContent = r.name + ' · ' + r.date_long;

  var avail = availableMetrics(r.dist_stream || []);
  document.getElementById('ro-tabs').innerHTML = avail.map(function(m, i) {
    return '<button class="dist-tab' + (i === 0 ? ' active' : '') +
      '" data-metric="' + m.key + '" onclick="selectOverlayMetric(\'' + m.key + '\')">' +
      m.label + '</button>';
  }).join('');
  document.getElementById('ro-splits').innerHTML = renderKmSplits(r);

  document.getElementById('run-overlay').classList.add('open');

  setTimeout(function() {
    try {
      if (overlayMap) { overlayMap.remove(); overlayMap = null; }
      var el = document.getElementById('ro-map');
      overlayMap = L.map(el, {zoomControl: true, preferCanvas: true});
      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
        subdomains: 'abcd', maxZoom: 19
      }).addTo(overlayMap);
      var poly = L.polyline(r.gps_polyline, {color: '#5cb85c', weight: 3, opacity: 0.9}).addTo(overlayMap);
      L.circleMarker(r.gps_polyline[0], {radius: 5, color: '#5cb85c', fillColor: '#fff', fillOpacity: 1, weight: 2}).addTo(overlayMap);
      L.circleMarker(r.gps_polyline[r.gps_polyline.length - 1], {radius: 5, color: '#e07020', fillColor: '#e07020', fillOpacity: 1, weight: 2}).addTo(overlayMap);
      activeCursorMarker = L.circleMarker(r.gps_polyline[0], {
        radius: 6, color: '#fff', fillColor: '#e07020', weight: 2,
        opacity: 0, fillOpacity: 0, pane: 'markerPane'
      }).addTo(overlayMap);
      var bounds = poly.getBounds();
      // invalidateSize first so Leaflet knows the real container size, then fit.
      overlayMap.invalidateSize();
      overlayMap.fitBounds(bounds, {padding: [24, 24]});
      if (avail.length) selectOverlayMetric(avail[0].key);
      // Re-fit once the flex layout has fully settled — the first paint can
      // report a stale container height, leaving the route partly off-screen.
      setTimeout(function() {
        if (overlayMap) { overlayMap.invalidateSize(); overlayMap.fitBounds(bounds, {padding: [24, 24]}); }
      }, 180);
    } catch(e) { console.warn('Overlay map init failed:', e); }
  }, 60);
}

function closeRunOverlay() {
  document.getElementById('run-overlay').classList.remove('open');
  if (overlayMap) { overlayMap.remove(); overlayMap = null; }
  activeCursorMarker = null;
  overlayRunId = null;
  DIST_STATE = null;
}

function selectOverlayMetric(metricKey) {
  if (overlayRunId === null) return;
  document.querySelectorAll('#ro-tabs .dist-tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.metric === metricKey);
  });
  drawDistMetric(overlayRunId, metricKey, {holderId: 'ro-chart', interactive: true});
}

function moveRunCursor(lat, lon) {
  if (!activeCursorMarker) return;
  if (lat == null || lon == null) { activeCursorMarker.setStyle({opacity: 0, fillOpacity: 0}); return; }
  activeCursorMarker.setLatLng([lat, lon]);
  activeCursorMarker.setStyle({opacity: 1, fillOpacity: 1});
}

function hideRunCursor() {
  if (activeCursorMarker) activeCursorMarker.setStyle({opacity: 0, fillOpacity: 0});
}

document.querySelectorAll('.tab').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var id = btn.dataset.panel;
    document.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('active'); });
    document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
    document.getElementById('panel-' + id).classList.add('active');
    btn.classList.add('active');
    if (id === 'overview' && overviewMap) {
      setTimeout(function() { overviewMap.invalidateSize(); }, 50);
    }
    if (id === 'runs' && selectedRunId !== null) {
      var r = RUNS.find(function(x) { return x.id === selectedRunId; });
      if (r) initRunMap(r);
    }
  });
});

var ZONE_COLORS = ['#3a7a3a', '#5cb85c', '#e0a020', '#e07020', '#d9534f'];
var ZONE_NAMES  = ['Z1 Easy', 'Z2 Endurance', 'Z3 Tempo', 'Z4 Threshold', 'Z5 Speed'];

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function paceZoneIdx(pace_s) {
  // pace_s = seconds per km; THRESHOLD_S_KM = threshold pace in secs/km
  if (!THRESHOLD_S_KM || !pace_s) return 0;
  // frac = lap_speed / threshold_speed = threshold_s_km / lap_s_km
  var frac = THRESHOLD_S_KM / pace_s;
  if (frac < 0.77) return 0;
  if (frac < 0.87) return 1;
  if (frac < 0.93) return 2;
  if (frac < 1.03) return 3;
  return 4;
}

// ── Run list ──────────────────────────────────────────────────────────────

function renderRunList(runs) {
  var el = document.getElementById('run-list-items');
  if (!runs.length) {
    el.innerHTML = '<p style="color:#444;padding:1rem;font-size:12px;text-align:center">No runs found</p>';
    return;
  }
  el.innerHTML = runs.map(function(r) {
    return '<div class="run-item' + (r.is_interval ? ' is-interval' : '') +
      '" data-id="' + r.id + '" onclick="showRun(' + r.id + ')">' +
      '<div class="ri-top">' +
        '<span class="ri-date">' + r.weekday.slice(0,3) + ' · ' + r.date_long + '</span>' +
        '<span class="ri-dist">' + r.dist_km + ' km</span>' +
      '</div>' +
      '<div class="ri-name">' + esc(r.name) + '</div>' +
      '<div class="ri-bottom">' +
        '<span class="ri-pace">' + r.pace_str + '</span>' +
        (r.gain > 10 ? '<span class="ri-gain">↑' + r.gain + 'm</span>' : '') +
        (r.is_interval ? '<span class="ri-tag">Intervals</span>' : '') +
      '</div></div>';
  }).join('');
}

function showRun(id) {
  selectedRunId = id;
  document.querySelectorAll('.run-item').forEach(function(el) { el.classList.remove('active'); });
  var item = document.querySelector('.run-item[data-id="' + id + '"]');
  if (item) { item.classList.add('active'); item.scrollIntoView({block:'nearest'}); }
  var r = RUNS.find(function(x) { return x.id === id; });
  if (!r) return;

  document.getElementById('run-detail-content').innerHTML = renderDetail(r);
  document.getElementById('run-detail-pane').scrollTop = 0;

  // draw the default over-distance metric if a stream exists
  if (r.dist_stream && r.dist_stream.length) {
    var av = availableMetrics(r.dist_stream);
    if (av.length) drawDistMetric(r.id, av[0].key, {holderId: 'dist-chart-' + r.id, interactive: false});
  }

  // Only init map if the Runs panel is currently visible
  var runsPanel = document.getElementById('panel-runs');
  if (runsPanel && runsPanel.classList.contains('active')) {
    initRunMap(r);
  }
}

// ── Run detail ────────────────────────────────────────────────────────────

function renderDetail(r) {
  var statsHtml =
    '<div class="rd-stats">' +
    stat('Distance', r.dist_km + ' km') +
    stat('Time', r.moving_str) +
    stat('Avg pace', r.pace_str) +
    statMuted('GA pace', r.ga_pace_str) +
    (r.gain > 0 ? stat('Elevation', '↑' + r.gain + ' m ↓' + r.descent + ' m') : '') +
    (r.cadence ? stat('Cadence', r.cadence + ' spm') : '') +
    (r.calories ? stat('Calories', r.calories + ' kcal') : '') +
    kmExtraStats(r) +
    '</div>';

  var mapHtml = (r.gps_polyline && r.gps_polyline.length >= 5)
    ? '<div class="rd-map-wrap" onclick="openRunOverlay(' + r.id + ')">' +
        '<div id="run-map-' + r.id + '" class="rd-map"></div>' +
        '<div class="rd-map-hint">⤢ Click map to expand</div>' +
      '</div>'
    : '';

  return '<div class="rd-top">' +
      '<div class="rd-left">' +
        '<div class="rd-header">' +
          '<h2 class="rd-name">' + esc(r.name) + '</h2>' +
          '<div class="rd-date">' + r.weekday + ', ' + r.date_long + '</div>' +
        '</div>' +
        statsHtml +
      '</div>' +
      renderDesc(r) +
    '</div>' +
    mapHtml +
    renderRunShape(r) +
    renderKmSplits(r) +
    renderPaceZones(r.pace_zones) +
    renderBestEfforts(r.best_efforts) +
    renderDistChart(r);
}

function stat(label, value) {
  return '<div class="rds"><div class="rds-l">' + label + '</div><div class="rds-v">' + value + '</div></div>';
}
function statMuted(label, value) {
  return '<div class="rds"><div class="rds-l">' + label + '</div><div class="rds-v rds-v-muted">' + value + '</div></div>';
}

// ── Strava description (top-right, collapsible) ─────────────────────────────

var DESC_LIMIT = 160;  // chars shown before the "more" toggle kicks in

function descBody(r, expanded) {
  var full = r.description || '';
  var isLong = full.length > DESC_LIMIT;
  var text = (isLong && !expanded) ? full.slice(0, DESC_LIMIT).replace(/\s+$/, '') + '…' : full;
  var btn = isLong
    ? '<button class="rd-desc-btn" onclick="toggleDesc(' + r.id + ')">' + (expanded ? 'less' : 'more') + '</button>'
    : '';
  return '<div class="rd-desc-text">' + esc(text) + '</div>' + btn;
}

function renderDesc(r) {
  if (!r.description) return '';
  return '<div class="rd-desc" id="rd-desc-' + r.id + '" data-expanded="0">' + descBody(r, false) + '</div>';
}

function toggleDesc(id) {
  var el = document.getElementById('rd-desc-' + id);
  if (!el) return;
  var expanded = el.dataset.expanded === '1';
  el.dataset.expanded = expanded ? '0' : '1';
  var r = RUNS.find(function(x) { return x.id === id; });
  if (r) el.innerHTML = descBody(r, !expanded);
}

function kmExtraStats(r) {
  var ks = r.km_splits;
  if (!ks || ks.length < 2) return '';
  var bestKm = ks.reduce(function(b,k){ return (k.pace_s && k.pace_s < b.pace_s) ? k : b; }, ks[0]);
  var out = bestKm.pace_s ? stat('Best km', bestKm.pace_str + ' (km ' + bestKm.km + ')') : '';

  var half = Math.floor(ks.length / 2);
  var first  = ks.slice(0, half);
  var second = ks.slice(half);
  if (first.length && second.length) {
    var fAvg = first.reduce(function(s,k){ return s + (k.pace_s || 0); }, 0) / first.length;
    var sAvg = second.reduce(function(s,k){ return s + (k.pace_s || 0); }, 0) / second.length;
    var diff = Math.round(sAvg - fAvg);
    var label, cls;
    if (diff < -3)      { label = '↗ Neg split ' + Math.abs(diff) + 's'; cls = 'color:#5cb85c'; }
    else if (diff > 3)  { label = '↘ Pos split +' + diff + 's'; cls = 'color:#e07020'; }
    else                { label = '↔ Even split'; cls = 'color:#777'; }
    out += '<div class="rds"><div class="rds-l">Split type</div>' +
           '<div class="rds-v" style="font-size:13px;' + cls + '">' + label + '</div></div>';
  }
  return out;
}

// ── Over-distance metric chart ────────────────────────────────────────────

var DIST_METRICS = [
  {key: 'pace', label: 'Pace',      color: '#5cb85c', invert: true},
  {key: 'elev', label: 'Elevation', color: '#8a6db5', invert: false},
  {key: 'hr',   label: 'Heart rate',color: '#d9534f', invert: false},
  {key: 'cad',  label: 'Cadence',   color: '#5b8fd9', invert: false}
];

function availableMetrics(stream) {
  return DIST_METRICS.filter(function(m) {
    return stream.some(function(p) { return p[m.key] != null; });
  });
}

function fmtMetric(key, v) {
  if (key === 'pace') {
    var mn = Math.floor(v / 60), sc = Math.round(v % 60);
    return mn + ':' + (sc < 10 ? '0' : '') + sc + '/km';
  }
  if (key === 'elev') return Math.round(v) + ' m';
  if (key === 'hr')   return Math.round(v) + ' bpm';
  if (key === 'cad')  return Math.round(v) + ' spm';
  return v;
}

// Holds the geometry of the currently-drawn chart so the hover handler can
// invert a mouse x back to a distance and look up the point's value + position.
var DIST_STATE = null;

function drawDistMetric(runId, metricKey, opts) {
  opts = opts || {};
  var holderId    = opts.holderId || ('dist-chart-' + runId);
  var interactive = !!opts.interactive;
  var r = RUNS.find(function(x) { return x.id === runId; });
  if (!r || !r.dist_stream || !r.dist_stream.length) return;
  var metric = DIST_METRICS.find(function(m) { return m.key === metricKey; });
  var stream = r.dist_stream;

  var W = 720, H = 200, padL = 44, padR = 12, padT = 12, padB = 24;
  var pts = stream.filter(function(p) { return p[metricKey] != null && p.d != null; });
  if (pts.length < 2) return;

  var xs = pts.map(function(p) { return p.d; });
  var ys = pts.map(function(p) { return p[metricKey]; });
  var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
  var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
  if (ymax === ymin) ymax = ymin + 1;
  var xspan = (xmax - xmin) || 1, yspan = (ymax - ymin) || 1;

  function px(d) { return padL + (d - xmin) / xspan * (W - padL - padR); }
  function py(v) {
    var t = (v - ymin) / yspan;
    if (metric.invert) t = 1 - t; // faster pace (smaller s/km) plotted higher
    return H - padB - t * (H - padT - padB);
  }

  // area + line path
  var line = pts.map(function(p, i) {
    return (i ? 'L' : 'M') + px(p.d).toFixed(1) + ',' + py(p[metricKey]).toFixed(1);
  }).join(' ');
  var area = 'M' + px(pts[0].d).toFixed(1) + ',' + (H - padB) +
    pts.map(function(p) { return 'L' + px(p.d).toFixed(1) + ',' + py(p[metricKey]).toFixed(1); }).join('') +
    'L' + px(pts[pts.length - 1].d).toFixed(1) + ',' + (H - padB) + 'Z';

  // y gridlines (3)
  var grid = '';
  for (var g = 0; g <= 2; g++) {
    var frac = g / 2;
    var vy = padT + frac * (H - padT - padB);
    var val = metric.invert ? (ymin + frac * yspan) : (ymax - frac * yspan);
    grid += '<line x1="' + padL + '" y1="' + vy.toFixed(1) + '" x2="' + (W - padR) +
      '" y2="' + vy.toFixed(1) + '" stroke="#2a2a2a" stroke-width="1"/>' +
      '<text x="' + (padL - 6) + '" y="' + (vy + 3).toFixed(1) +
      '" font-size="9" fill="#555" text-anchor="end">' + fmtMetric(metricKey, val) + '</text>';
  }
  // x labels at each whole km that fits
  var xlab = '';
  for (var km = Math.ceil(xmin); km <= Math.floor(xmax); km++) {
    if ((xmax - xmin) > 12 && km % 2 !== 0) continue;
    xlab += '<text x="' + px(km).toFixed(1) + '" y="' + (H - 8) +
      '" font-size="9" fill="#555" text-anchor="middle">' + km + '</text>';
  }

  // The interactive cursor (guide line + dot + tooltip) is only drawn when the
  // chart links to a visible map — i.e. inside the expanded overlay.
  var aspect = interactive ? ' preserveAspectRatio="none"' : '';
  var cursorSvg = interactive
    ? '<g id="' + holderId + '-cursor" style="opacity:0;pointer-events:none">' +
        '<line y1="' + padT + '" y2="' + (H - padB) + '" stroke="#888" stroke-width="1" stroke-dasharray="3,3"/>' +
        '<circle r="4.5" fill="' + metric.color + '" stroke="#fff" stroke-width="1.5"/>' +
      '</g>'
    : '';

  var gid = 'grad-' + holderId + '-' + metricKey;
  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '"' + aspect + ' xmlns="http://www.w3.org/2000/svg">' +
    '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="' + metric.color + '" stop-opacity="0.35"/>' +
      '<stop offset="1" stop-color="' + metric.color + '" stop-opacity="0.02"/>' +
    '</linearGradient></defs>' +
    grid +
    '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
    '<path d="' + line + '" fill="none" stroke="' + metric.color + '" stroke-width="1.6"/>' +
    xlab +
    cursorSvg +
    '</svg>';

  var holder = document.getElementById(holderId);
  if (!holder) return;
  holder.innerHTML = svg + (interactive ? '<div class="dist-tip" id="' + holderId + '-tip"></div>' : '');

  if (!interactive) {
    holder.onmousemove = null;
    holder.onmouseleave = null;
    return;
  }

  DIST_STATE = {
    holderId: holderId, runId: runId, metricKey: metricKey, label: metric.label, color: metric.color,
    pts: pts, xmin: xmin, xspan: xspan, ymin: ymin, yspan: yspan, invert: metric.invert,
    W: W, H: H, padL: padL, padR: padR, padT: padT, padB: padB,
    poly: r.gps_polyline || []
  };

  holder.onmousemove = function(e) { onDistHover(e, holder); };
  holder.onmouseleave = function() {
    var cur = document.getElementById(holderId + '-cursor');
    var tip = document.getElementById(holderId + '-tip');
    if (cur) cur.style.opacity = 0;
    if (tip) tip.style.opacity = 0;
    hideRunCursor();
  };
}

function onDistHover(e, holder) {
  var st = DIST_STATE;
  if (!st) return;
  var svg = holder.querySelector('svg');
  if (!svg) return;
  var sr = svg.getBoundingClientRect();
  var sx = (e.clientX - sr.left) / sr.width * st.W;   // mouse x in svg user units

  function px(d) { return st.padL + (d - st.xmin) / st.xspan * (st.W - st.padL - st.padR); }
  function py(v) {
    var t = (v - st.ymin) / st.yspan;
    if (st.invert) t = 1 - t;
    return st.H - st.padB - t * (st.H - st.padT - st.padB);
  }

  // nearest point by x position
  var best = st.pts[0], bestDx = Infinity;
  for (var i = 0; i < st.pts.length; i++) {
    var dx = Math.abs(px(st.pts[i].d) - sx);
    if (dx < bestDx) { bestDx = dx; best = st.pts[i]; }
  }

  var cx = px(best.d), cy = py(best[st.metricKey]);

  // move the svg cursor
  var cur = document.getElementById(st.holderId + '-cursor');
  if (cur) {
    cur.style.opacity = 1;
    var ln = cur.querySelector('line');
    var dot = cur.querySelector('circle');
    ln.setAttribute('x1', cx); ln.setAttribute('x2', cx);
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
  }

  // tooltip with exact x (distance) and y (metric value)
  var tip = document.getElementById(st.holderId + '-tip');
  if (tip) {
    tip.innerHTML = '<div class="tip-d">' + best.d.toFixed(2) + ' km</div>' +
                    '<div><b>' + fmtMetric(st.metricKey, best[st.metricKey]) + '</b></div>';
    var hr = holder.getBoundingClientRect();
    var scaleX = sr.width / st.W, scaleY = sr.height / st.H;
    tip.style.left = ((sr.left - hr.left) + cx * scaleX) + 'px';
    tip.style.top  = ((sr.top - hr.top) + cy * scaleY) + 'px';
    tip.style.opacity = 1;
  }

  // move the dot on the map to the matching position. Prefer the GPS coords
  // embedded in the stream point; fall back to interpolating along the polyline
  // by distance fraction for runs compiled before lat/lon was stored.
  if (best.lat != null && best.lon != null) {
    moveRunCursor(best.lat, best.lon);
  } else if (st.poly.length >= 2) {
    var frac = (best.d - st.xmin) / st.xspan;
    frac = Math.max(0, Math.min(1, frac));
    var fi = frac * (st.poly.length - 1);
    var lo = Math.floor(fi), hi = Math.min(lo + 1, st.poly.length - 1), t = fi - lo;
    var lat = st.poly[lo][0] + (st.poly[hi][0] - st.poly[lo][0]) * t;
    var lon = st.poly[lo][1] + (st.poly[hi][1] - st.poly[lo][1]) * t;
    moveRunCursor(lat, lon);
  } else {
    hideRunCursor();
  }
}

function selectDistMetric(runId, metricKey) {
  document.querySelectorAll('#dist-tabs-' + runId + ' .dist-tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.metric === metricKey);
  });
  drawDistMetric(runId, metricKey, {holderId: 'dist-chart-' + runId, interactive: false});
}

function renderDistChart(r) {
  if (!r.dist_stream || !r.dist_stream.length) return '';
  var avail = availableMetrics(r.dist_stream);
  if (!avail.length) return '';
  var tabs = avail.map(function(m, i) {
    return '<button class="dist-tab' + (i === 0 ? ' active' : '') +
      '" data-metric="' + m.key + '" onclick="selectDistMetric(' + r.id + ',\'' + m.key + '\')">' +
      m.label + '</button>';
  }).join('');
  return section('Over distance',
    '<div class="dist-tabs" id="dist-tabs-' + r.id + '">' + tabs + '</div>' +
    '<div id="dist-chart-' + r.id + '" class="dist-chart"></div>');
}

// ── Run shape chart ───────────────────────────────────────────────────────

function renderRunShape(r) {
  var ks = r.km_splits;
  if (!ks || ks.length < 2) return '';

  var paces  = ks.map(function(s){ return s.pace_s; }).filter(Boolean);
  if (!paces.length) return '';

  var minP = Math.min.apply(null, paces);
  var maxP = Math.max.apply(null, paces);
  var range = maxP - minP || 1;
  var H = 64;  // chart height in px

  var bars = ks.map(function(s) {
    if (!s.pace_s) return '<div style="flex:1"></div>';
    var heightPx = Math.round(((maxP - s.pace_s) / range * 0.7 + 0.3) * H);
    var zi = paceZoneIdx(s.pace_s);
    return '<div title="km ' + s.km + ': ' + s.pace_str + '" ' +
      'style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:0">' +
      '<div style="width:80%;max-width:20px;height:' + heightPx + 'px;' +
           'background:' + ZONE_COLORS[zi] + ';border-radius:2px 2px 0 0"></div>' +
      '</div>';
  }).join('');

  var avgS = r.moving_s / r.dist_km;
  var avgM = Math.floor(avgS / 60);
  var avgSec = Math.round(avgS % 60);
  var avgStr = avgM + ':' + (avgSec < 10 ? '0' : '') + avgSec + '/km avg';

  var xLabels = ks.map(function(s) {
    return '<div style="flex:1;text-align:center;font-size:8px;color:#444">' +
      (s.km % 5 === 0 || s.km === 1 ? s.km : '') + '</div>';
  }).join('');

  return section('Run shape',
    '<div style="font-size:9px;color:#555;margin-bottom:6px">' + avgStr + ' · ' + ks.length + ' km splits</div>' +
    '<div style="display:flex;align-items:flex-end;height:' + H + 'px;gap:1px">' + bars + '</div>' +
    '<div style="display:flex;margin-top:2px">' + xLabels + '</div>');
}

// ── Per-km splits table ───────────────────────────────────────────────────

function renderKmSplits(r) {
  var ks = r.km_splits;

  if (!ks || !ks.length) {
    // Fallback: show FIT lap splits if available
    return renderLapSplits(r.lap_splits);
  }

  var bestKm = ks.reduce(function(b,k){ return (k.pace_s && k.pace_s < b.pace_s) ? k : b; }, ks[0]);
  var hasElev = ks.some(function(s){ return s.gain_m != null || s.loss_m != null; });
  var hasCad  = ks.some(function(s){ return s.cad != null; });

  var rows = ks.map(function(s) {
    var isBest = bestKm.pace_s && s.km === bestKm.km;
    var zi = paceZoneIdx(s.pace_s);
    var dot = '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;' +
              'background:' + ZONE_COLORS[zi] + ';margin-right:4px;vertical-align:middle"></span>';
    var mins = Math.floor((s.time_s || 0) / 60);
    var secs = Math.round((s.time_s || 0) % 60);
    var timeStr = mins + ':' + (secs < 10 ? '0' : '') + secs;
    var elevCell = '';
    if (hasElev) {
      var g = s.gain_m || 0, l = s.loss_m || 0;
      elevCell = '<td><span style="color:#4a9a4a">↑' + g + '</span> ' +
                 '<span style="color:#c07a3a">↓' + l + '</span></td>';
    }
    var cadCell = hasCad ? '<td>' + (s.cad != null ? s.cad : '—') + '</td>' : '';
    return '<tr' + (isBest ? ' class="best-km"' : '') + '>' +
      '<td>' + s.km + '</td>' +
      '<td>' + timeStr + '</td>' +
      '<td class="pace-cell">' + dot + s.pace_str + (isBest ? ' ★' : '') + '</td>' +
      elevCell + cadCell +
    '</tr>';
  }).join('');

  var head = '<th>km</th><th>Time</th><th>Pace</th>' +
    (hasElev ? '<th>Elev</th>' : '') + (hasCad ? '<th>Cad</th>' : '');

  return section('Per km splits',
    '<table class="rd-table">' +
    '<thead><tr>' + head + '</tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

function renderLapSplits(splits) {
  if (!splits || !splits.length) return '';
  var rows = splits.map(function(lap) {
    return '<tr>' +
      '<td>' + lap.idx + '</td>' +
      '<td>' + lap.dist_str + ' km</td>' +
      '<td>' + lap.time_str + '</td>' +
      '<td class="pace-cell">' + lap.pace_str + '</td>' +
      '<td>' + lap.cadence_str + '</td>' +
      '<td>' + lap.gain_str + '</td>' +
    '</tr>';
  }).join('');
  return section('Lap splits',
    '<table class="rd-table">' +
    '<thead><tr><th>Lap</th><th>Dist</th><th>Time</th><th>Pace</th><th>Cadence</th><th>Gain</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

// ── Pace zones ────────────────────────────────────────────────────────────

function renderPaceZones(zones) {
  if (!zones || zones.every(function(z) { return z === 0; })) return '';
  var total = zones.reduce(function(a, b) { return a + b; }, 0) || 1;

  var bar = zones.map(function(s, i) {
    var pct = s / total * 100;
    if (pct < 0.5) return '';
    return '<div style="width:' + pct.toFixed(1) + '%;background:' + ZONE_COLORS[i] +
      ';height:100%;display:flex;align-items:center;justify-content:center;' +
      'font-size:9px;color:#000;font-weight:600">' +
      (pct > 8 ? pct.toFixed(0) + '%' : '') + '</div>';
  }).join('');

  var legend = zones.map(function(s, i) {
    if (s < 1) return '';
    return '<span style="display:inline-flex;align-items:center;gap:4px;font-size:10px;color:#888">' +
      '<span style="width:10px;height:10px;border-radius:2px;background:' + ZONE_COLORS[i] + ';display:inline-block"></span>' +
      ZONE_NAMES[i] + ' ' + (s / total * 100).toFixed(0) + '%</span>';
  }).join('');

  return section('Pace zones',
    '<div style="height:24px;border-radius:4px;overflow:hidden;display:flex;margin-bottom:6px">' + bar + '</div>' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap">' + legend + '</div>');
}

// ── Best efforts ──────────────────────────────────────────────────────────

function renderBestEfforts(efforts) {
  if (!efforts || !efforts.length) return '';
  var rows = efforts.map(function(e) {
    return '<tr><td>' + e.label + '</td><td>' + e.time + '</td><td class="pace-cell">' + e.pace + '</td></tr>';
  }).join('');
  return section('Best efforts in this run',
    '<table class="rd-table">' +
    '<thead><tr><th>Distance</th><th>Time</th><th>Pace</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>');
}

function section(title, body) {
  return '<div class="rd-section"><div class="rd-section-title">' + title + '</div>' + body + '</div>';
}

// ── Overview heatmap ──────────────────────────────────────────────────────

function initOverviewMap() {
  if (!HEATMAP_POINTS || !HEATMAP_POINTS.length || typeof L === 'undefined') return;
  try {
    var el = document.getElementById('overview-heatmap');
    if (!el) return;
    overviewMap = L.map(el, {zoomControl: true});
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
      subdomains: 'abcd', maxZoom: 19
    }).addTo(overviewMap);
    // Set a view first so getZoom()/getCenter() are valid for tuneHeatRadius below.
    overviewMap.fitBounds(L.latLngBounds(HEATMAP_POINTS), {padding: [20, 20]});
    if (typeof L.heatLayer !== 'undefined') {
      var heat = L.heatLayer(HEATMAP_POINTS, {
        radius: 7, blur: 8, maxZoom: 18, minOpacity: 0.2,
        gradient: {0.0: '#1a3a1a', 0.3: '#3a7a3a', 0.6: '#5cb85c', 0.85: '#e0a020', 1.0: '#d9534f'}
      }).addTo(overviewMap);
      // Points sit on an ~11 m dedup grid, but leaflet.heat uses a fixed *pixel*
      // radius. As you zoom in the gap between points grows past that radius and
      // the line breaks back into dots. Scale the radius with zoom so it always
      // spans roughly the point spacing (clamped so low zoom keeps the old look).
      var BASE_RADIUS = 7;
      function tuneHeatRadius() {
        var z = overviewMap.getZoom();
        var lat = overviewMap.getCenter().lat;
        var mPerPx = 156543.03392 * Math.cos(lat * Math.PI / 180) / Math.pow(2, z);
        var spacingPx = 11 / mPerPx;
        var radius = Math.max(BASE_RADIUS, Math.min(40, spacingPx * 0.8));
        // A bigger brush makes a single path's own interpolated points overlap
        // more, so the accumulated alpha saturates and even a lightly-visited
        // street drifts to red as you zoom in. Scale the intensity normalisation
        // (max) with the radius so the per-point contribution is divided back
        // down: at BASE_RADIUS max stays 1 (the low-zoom look), and colour now
        // tracks how often a route was actually run rather than the zoom level.
        var max = radius / BASE_RADIUS;
        heat.setOptions({radius: radius, blur: radius * 1.1, max: max});
      }
      overviewMap.on('zoomend', tuneHeatRadius);
      tuneHeatRadius();
    }
  } catch(e) { console.warn('Overview heatmap init failed:', e); }
}

// ── Search & init ─────────────────────────────────────────────────────────

document.getElementById('run-search').addEventListener('input', function() {
  var q = this.value.toLowerCase();
  var filtered = q ? RUNS.filter(function(r) {
    return r.name.toLowerCase().indexOf(q) >= 0 || r.date_long.toLowerCase().indexOf(q) >= 0;
  }) : RUNS;
  renderRunList(filtered);
  if (filtered.length) showRun(filtered[0].id);
  else document.getElementById('run-detail-content').innerHTML = '<p class="placeholder-msg">No runs match</p>';
});

// Close the expanded analysis overlay on backdrop click or Escape.
document.getElementById('run-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeRunOverlay();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeRunOverlay();
});

renderRunList(RUNS);
if (RUNS.length) showRun(RUNS[0].id);
initOverviewMap();
"""


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def generate(
    rows: list[dict],
    runs: list[dict],
    updated: str,
    threshold_mps: float | None,
    html_efforts: str,
    html_goals: str,
    html_analytics: str,
) -> str:
    stats          = _overview_stats(rows, runs)
    weeks          = weekly_mileage(rows)
    spark_cols     = render_spark(weeks)
    runs_json      = json.dumps(runs, ensure_ascii=False)
    heat_pts       = _heatmap_points(runs)
    heatmap_json   = json.dumps(heat_pts)

    # Embed threshold as seconds/km for JS zone colouring (null if unknown)
    if threshold_mps and threshold_mps > 0:
        threshold_s_km = round(1000.0 / threshold_mps, 2)
    else:
        threshold_s_km = "null"

    overview_html = f"""
<div class="ov-section">
  <p class="ov-section-title">All time</p>
  <div class="ov-grid">
    <div class="ov-card"><p class="ov-label">Total runs</p><p class="ov-value">{stats['total_runs']}</p></div>
    <div class="ov-card"><p class="ov-label">Total distance</p><p class="ov-value">{stats['total_km']}</p><p class="ov-sub">km</p></div>
    <div class="ov-card"><p class="ov-label">Moving time</p><p class="ov-value">{stats['total_time']}</p></div>
    <div class="ov-card"><p class="ov-label">Longest run</p><p class="ov-value">{stats['longest_km']}</p><p class="ov-sub">km &middot; {stats['longest_date']}</p></div>
  </div>
</div>

<div class="ov-section">
  <p class="ov-section-title">This week</p>
  <div class="ov-grid">
    <div class="ov-card"><p class="ov-label">Runs</p><p class="ov-value">{stats['week_runs']}</p></div>
    <div class="ov-card"><p class="ov-label">Distance</p><p class="ov-value">{stats['week_km']}</p><p class="ov-sub">km</p></div>
  </div>
</div>

<div class="ov-section">
  <p class="ov-section-title">Fitness &amp; form</p>
  <div class="ov-grid">
    <div class="ov-card"><p class="ov-label">Fitness (CTL)</p><p class="ov-value green">{stats['ctl']}</p><p class="ov-sub">42-day load</p></div>
    <div class="ov-card"><p class="ov-label">Fatigue (ATL)</p><p class="ov-value amber">{stats['atl']}</p><p class="ov-sub">7-day load</p></div>
    <div class="ov-card"><p class="ov-label">Form (TSB)</p><p class="ov-value {stats['tsb_class']}">{stats['tsb']:+.1f}</p><p class="ov-sub">{stats['tsb_note']}</p></div>
  </div>
</div>

<div class="ov-section">
  <p class="ov-section-title">All-time heatmap</p>
  <div id="overview-heatmap" style="height:420px;border-radius:8px;border:1px solid #3a3a3a;background:#111;overflow:hidden"></div>
</div>

<div class="ov-section">
  <p class="ov-section-title">Weekly mileage &mdash; Apr 2026 onwards</p>
  <div class="spark-wrap">
    <p class="spark-label">km per week (ISO weeks) &middot; updated {updated}</p>
    <div class="bars">
{spark_cols}
    </div>
  </div>
</div>
"""

    # Embedded dashboard panel CSS comes before hub CSS so hub body rules win.
    # ANALYTICS_PANEL_CSS has a body{padding:1rem} rule — the hub's body rule
    # (padding:0) comes later in the same <style> block and takes precedence.
    combined_css = BEST_EFFORTS_CSS + "\n" + GOAL_CSS + "\n" + ANALYTICS_PANEL_CSS + "\n" + HUB_CSS

    panel_style = "height:calc(100vh - 44px);overflow-y:auto;padding:1rem"

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Training Hub</title>\n"
        "<link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\"/>\n"
        "<style>\n" + combined_css + "</style>\n"
        "</head>\n"
        "<body>\n"

        "<div class=\"tab-bar\">\n"
        "  <span class=\"brand\">Training Hub</span>\n"
        "  <button class=\"tab active\" data-panel=\"overview\">Overview</button>\n"
        "  <button class=\"tab\" data-panel=\"efforts\">Best Efforts</button>\n"
        "  <button class=\"tab\" data-panel=\"goals\">Goals</button>\n"
        "  <button class=\"tab\" data-panel=\"analytics\">Analytics</button>\n"
        "  <button class=\"tab\" data-panel=\"runs\">Runs</button>\n"
        "</div>\n"

        "<div id=\"panel-overview\" class=\"panel active\" style=\"" + panel_style + "\">\n"
        + overview_html +
        "</div>\n"

        "<div id=\"panel-efforts\" class=\"panel\" style=\"" + panel_style + "\">\n"
        + html_efforts +
        "\n</div>\n"

        "<div id=\"panel-goals\" class=\"panel\" style=\"" + panel_style + "\">\n"
        + html_goals +
        "\n</div>\n"

        "<div id=\"panel-analytics\" class=\"panel\" style=\"" + panel_style + "\">\n"
        + html_analytics +
        "\n</div>\n"

        "<div id=\"panel-runs\" class=\"panel\">\n"
        "  <div class=\"runs-layout\">\n"
        "    <aside id=\"run-list-pane\">\n"
        "      <div class=\"run-search\">\n"
        "        <input type=\"text\" id=\"run-search\" placeholder=\"Filter runs…\">\n"
        "      </div>\n"
        "      <div id=\"run-list-items\"></div>\n"
        "    </aside>\n"
        "    <main id=\"run-detail-pane\">\n"
        "      <div id=\"run-detail-content\">\n"
        "        <p class=\"placeholder-msg\">Select a run to view details</p>\n"
        "      </div>\n"
        "    </main>\n"
        "  </div>\n"
        "</div>\n"

        "<div id=\"run-overlay\" class=\"run-overlay\">\n"
        "  <div class=\"ro-box\">\n"
        "    <div class=\"ro-head\">\n"
        "      <span id=\"ro-title\" class=\"ro-title\"></span>\n"
        "      <button class=\"ro-close\" onclick=\"closeRunOverlay()\" title=\"Close (Esc)\">&times;</button>\n"
        "    </div>\n"
        "    <div class=\"ro-body\">\n"
        "      <aside id=\"ro-splits\" class=\"ro-splits\"></aside>\n"
        "      <div class=\"ro-main\">\n"
        "        <div id=\"ro-map\" class=\"ro-map\"></div>\n"
        "        <div id=\"ro-tabs\" class=\"dist-tabs ro-tabs\"></div>\n"
        "        <div id=\"ro-chart\" class=\"dist-chart\"></div>\n"
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        "</div>\n"

        "<script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>\n"
        "<script src=\"https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js\"></script>\n"
        "<script>\n"
        "const RUNS = " + runs_json + ";\n"
        "const THRESHOLD_S_KM = " + str(threshold_s_km) + ";\n"
        "const HEATMAP_POINTS = " + heatmap_json + ";\n"
        + HUB_JS +
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    here    = Path(__file__).parent
    csv_dir = here / "csv_data"
    candidates = sorted(csv_dir.glob("*_strava.csv"))
    if not candidates:
        sys.exit(f"Error: no *_strava.csv found in {csv_dir}")
    csv_path = candidates[-1]
    print(f"Using: {csv_path.name}")

    rows = load_rows(csv_path)
    if not rows:
        sys.exit("Error: CSV is empty")

    try:
        date_str = csv_path.stem.split("_")[0]
        updated  = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, IndexError):
        updated  = datetime.today().strftime("%d %b %Y")

    threshold_mps = _compute_threshold(rows)
    if threshold_mps:
        threshold_pace = fmt_pace(1000.0, threshold_mps * 1000.0)
        print(f"Threshold pace (from best 5K): {threshold_pace}")
    else:
        print("No 5K best effort found; pace zones disabled")

    runs = _build_runs(rows, threshold_mps)
    print(f"Loaded {len(runs)} runs")

    print("Building dashboard panels…")
    html_efforts   = body_best_efforts(rows, updated)
    html_goals     = body_goal_dashboard(rows, updated)
    html_analytics = body_analytics(rows, updated)

    dashboards_dir = here / "dashboards"
    dashboards_dir.mkdir(exist_ok=True)

    out = dashboards_dir / "TrainingHub.html"
    out.write_text(
        generate(rows, runs, updated, threshold_mps, html_efforts, html_goals, html_analytics),
        encoding="utf-8",
    )
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
