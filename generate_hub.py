#!/usr/bin/env python3
"""
generate_hub.py
Generates dashboards/index.html — a self-contained training hub with:
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
.rd-header{margin-bottom:1rem}
.rd-name{font-size:20px;font-weight:700;color:#eee;margin:0 0 4px}
.rd-date{font-size:12px;color:#555}
.rd-stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;margin-bottom:1.25rem}
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
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

HUB_JS = r"""
var overviewMap = null;
var currentRunMap = null;
var selectedRunId = null;

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
      currentRunMap.fitBounds(poly.getBounds(), {padding: [24, 24]});
    } catch(e) { console.warn('Run map init failed:', e); }
  }, 50);
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
    ? '<div id="run-map-' + r.id + '" style="height:260px;border-radius:8px;border:1px solid #3a3a3a;margin-bottom:1.25rem"></div>'
    : '';

  return '<div class="rd-header">' +
      '<h2 class="rd-name">' + esc(r.name) + '</h2>' +
      '<div class="rd-date">' + r.weekday + ', ' + r.date_long + '</div>' +
    '</div>' +
    statsHtml +
    mapHtml +
    renderRunShape(r) +
    renderKmSplits(r) +
    renderPaceZones(r.pace_zones) +
    renderBestEfforts(r.best_efforts);
}

function stat(label, value) {
  return '<div class="rds"><div class="rds-l">' + label + '</div><div class="rds-v">' + value + '</div></div>';
}
function statMuted(label, value) {
  return '<div class="rds"><div class="rds-l">' + label + '</div><div class="rds-v rds-v-muted">' + value + '</div></div>';
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

  var rows = ks.map(function(s) {
    var isBest = bestKm.pace_s && s.km === bestKm.km;
    var zi = paceZoneIdx(s.pace_s);
    var dot = '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;' +
              'background:' + ZONE_COLORS[zi] + ';margin-right:4px;vertical-align:middle"></span>';
    var mins = Math.floor((s.time_s || 0) / 60);
    var secs = Math.round((s.time_s || 0) % 60);
    var timeStr = mins + ':' + (secs < 10 ? '0' : '') + secs;
    return '<tr' + (isBest ? ' class="best-km"' : '') + '>' +
      '<td>' + s.km + '</td>' +
      '<td>' + timeStr + '</td>' +
      '<td class="pace-cell">' + dot + s.pace_str + (isBest ? ' ★' : '') + '</td>' +
    '</tr>';
  }).join('');

  return section('Per km splits',
    '<table class="rd-table">' +
    '<thead><tr><th>km</th><th>Time</th><th>Pace</th></tr></thead>' +
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
    if (typeof L.heatLayer !== 'undefined') {
      L.heatLayer(HEATMAP_POINTS, {
        radius: 7, blur: 8, maxZoom: 18, minOpacity: 0.2,
        gradient: {0.0: '#1a3a1a', 0.3: '#3a7a3a', 0.6: '#5cb85c', 0.85: '#e0a020', 1.0: '#d9534f'}
      }).addTo(overviewMap);
    }
    overviewMap.fitBounds(L.latLngBounds(HEATMAP_POINTS), {padding: [20, 20]});
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

    out = dashboards_dir / "index.html"
    out.write_text(
        generate(rows, runs, updated, threshold_mps, html_efforts, html_goals, html_analytics),
        encoding="utf-8",
    )
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
