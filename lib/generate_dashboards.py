#!/usr/bin/env python3
"""
generate_dashboards.py
Generates two HTML dashboards from a strava_compile.py enriched CSV:
  - top_runs_by_distance.html   best efforts by distance band (top 3 per band)
  - goal_dashboard.html         race goal gaps, speed targets, weekly mileage



 --out-dir ./output

The input CSV must be produced by strava_compile.py (it expects the
best_*_s columns, fit_avg_cadence, Elevation Gain, etc).
"""


import csv
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config import Race, load_config

# Enriched-CSV stream fields (fit_distance_stream) can exceed the default
# 128 KB per-field limit at higher sampling density.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


# ---------------------------------------------------------------------------
# Pace / time helpers
# ---------------------------------------------------------------------------

def minetti_cost(g: float) -> float:
    return 155.4*g**5 - 30.4*g**4 - 43.3*g**3 + 46.3*g**2 + 19.5*g + 3.6

COST_FLAT = minetti_cost(0)


def ga_time(raw_s: float, elev_gain_m: float, dist_km: float) -> float:
    """Grade-adjusted time using Minetti formula, half one-way ascent ratio."""
    if not dist_km:
        return raw_s
    grade = (elev_gain_m / 2) / (dist_km * 1000)
    return raw_s * (COST_FLAT / minetti_cost(grade))


def fmt_pace(s: float, dist_m: float) -> str:
    """Seconds over dist_m -> 'M:SS/km' string."""
    pace = s / (dist_m / 1000)
    m, sec = int(pace // 60), int(round(pace % 60))
    if sec == 60:
        m, sec = m + 1, 0
    return f"{m}:{sec:02d}/km"


def fmt_pace_bare(s: float, dist_m: float) -> str:
    """Seconds over dist_m -> 'M:SS' string (no /km suffix)."""
    return fmt_pace(s, dist_m).removesuffix("/km")


def fmt_time(s: float) -> str:
    """Seconds -> 'H:MM:SS' or 'M:SS' string."""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(round(s % 60))
    if sec == 60:
        m, sec = m + 1, 0
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%b %d, %Y, %I:%M:%S %p")
        return dt.strftime("%b %-d")
    except ValueError:
        return date_str[:6]


def fmt_pace_from_s_per_km(s_per_km: float) -> str:
    m, sec = int(s_per_km // 60), int(round(s_per_km % 60))
    if sec == 60:
        m, sec = m + 1, 0
    return f"{m}:{sec:02d}"


def fmt_target_range(lo_s: float, hi_s: float) -> str:
    """Format a target pace range in s/km as 'M:SS–M:SS/km'."""
    return f"{fmt_pace_from_s_per_km(lo_s)}–{fmt_pace_from_s_per_km(hi_s)}/km"


def riegel(t1: float, d1: float, d2: float) -> float:
    return t1 * (d2 / d1) ** 1.06


def fit_riegel(points: list[tuple[float, float]]) -> tuple[float, float]:
    """
    Fit T = a * D^b to (dist_m, ga_time_s) pairs via log-log least squares.
    Returns (a, b). Clamps b to [1.0, 1.15] for physiological sanity.
    Falls back to b=1.06 when fewer than 2 points are available.
    """
    if len(points) < 2:
        if points:
            d, t = points[0]
            return t / d ** 1.06, 1.06
        return 1.0, 1.06
    log_D = [math.log(d) for d, _ in points]
    log_T = [math.log(t) for _, t in points]
    n = len(log_D)
    mean_x = sum(log_D) / n
    mean_y = sum(log_T) / n
    ssxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(log_D, log_T))
    ssxx = sum((x - mean_x) ** 2 for x in log_D)
    b = ssxy / ssxx if ssxx else 1.06
    b = max(1.0, min(1.15, b))
    log_a = mean_y - b * mean_x
    return math.exp(log_a), b


def derive_training_target(riegel_a: float, riegel_b: float, target_dist_m: float) -> tuple:
    """
    Hybrid target pace range:
    - Distances >= 1500m: fitted Riegel curve — keeps race-distance targets proportionally
      consistent, so falloff at 15K/half vs 5K/10K shows up as a real gap.
    - Short reps (400m / 800m / 1km): percentage of curve-predicted 5K pace, because
      Riegel extrapolation to short distances gives physiologically unrealistic targets
      (it would project sub-4:30/km for 800m from a typical half-marathon anchor).

    Returns (lo_s_per_km, hi_s_per_km) where lo is faster.
    """
    pred_5k_pace = (riegel_a * 5000 ** riegel_b) / 5  # s/km from curve

    if target_dist_m >= 1500:
        pred_pace = (riegel_a * target_dist_m ** riegel_b) / (target_dist_m / 1000)
        return pred_pace * 0.97, pred_pace
    elif target_dist_m >= 900:   # 1km reps: 2–4% faster than 5K
        return pred_5k_pace * 0.96, pred_5k_pace * 0.98
    elif target_dist_m >= 700:   # 800m reps: 4–7% faster than 5K
        return pred_5k_pace * 0.93, pred_5k_pace * 0.96
    else:                          # 400m reps: 7–12% faster than 5K
        return pred_5k_pace * 0.88, pred_5k_pace * 0.93


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Training distance definitions — drives the training targets table
# ---------------------------------------------------------------------------

TRAINING_DEFS = [
    # (label, best_effort_col, dist_m, incl_intervals)
    ("400m reps",     "best_400m_s",   400,        True),
    ("800m reps",     "best_1/2mi_s",  804.672,    True),
    ("1km reps",      "best_1km_s",    1_000,      False),
    ("1 mile",        "best_1mi_s",    1_609.344,  False),
    ("5K",            "best_5k_s",     5_000,      False),
    ("10K",           "best_10k_s",    10_000,     False),
    ("15K",           "best_15k_s",    15_000,     False),
    ("Half marathon", "best_half_s",   21_097.5,   False),
]

# ---------------------------------------------------------------------------

DISTANCE_BANDS = [
    # (label,   best_effort_col,  target_m,  include_intervals)
    ("400m",    "best_400m_s",    400,        True),
    ("1/2 mile","best_1/2mi_s",   804.672,    True),
    ("1 km",    "best_1km_s",     1_000,      False),
    ("1 mile",  "best_1mi_s",     1_609.344,  False),
    ("2 miles", "best_2mi_s",     3_218.69,   False),
    ("5K",      "best_5k_s",      5_000,      False),
    ("10K",     "best_10k_s",     10_000,     False),
    ("15K",     "best_15k_s",     15_000,     False),
    ("10 miles","best_10mi_s",    16_093.4,   False),
    ("20K",     "best_20k_s",     20_000,     False),
    ("Half marathon", "best_half_s", 21_097.5, False),
]


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def is_interval(row: dict) -> bool:
    elapsed = float(row.get("Elapsed Time") or 0)
    moving  = float(row.get("Moving Time")  or 0)
    if not elapsed:
        return False
    return (elapsed - moving) / elapsed >= 0.20


def top3_for_band(
    rows: list[dict],
    col_s: str,
    target_m: float,
    incl_intervals: bool,
) -> list[dict]:
    efforts = []
    for r in rows:
        if not incl_intervals and is_interval(r):
            continue
        raw = r.get(col_s)
        if not raw:
            continue
        try:
            s = float(raw)
        except (TypeError, ValueError):
            continue
        if s <= 0:
            continue
        dist_km = float(r.get("Distance") or 0) / 1000
        gain    = float(r.get("Elevation Gain") or 0)
        ga_s    = ga_time(s, gain, dist_km)
        efforts.append({
            "s":           s,
            "raw_pace":    fmt_pace(s, target_m),
            "ga_pace":     fmt_pace(ga_s, target_m),
            "time_str":    fmt_time(s),
            "activity":    r.get("Activity Name", "").strip(),
            "date":        fmt_date(r.get("Activity Date", "")),
            "dist_km":     dist_km,
            "gain":        gain,
            "is_interval": is_interval(r),
        })
    efforts.sort(key=lambda x: x["s"])
    return efforts[:3]


def weekly_mileage(rows: list[dict]) -> list[tuple[str, float]]:
    """Returns list of (week_label, km) for every ISO week present, oldest first.
    The dashboard's range toggle slices this to 3M/6M/1Y/All."""
    weekly: dict[tuple, float] = defaultdict(float)
    for r in rows:
        try:
            dt = datetime.strptime(r.get("Activity Date", ""), "%b %d, %Y, %I:%M:%S %p")
        except ValueError:
            continue
        dist_km = float(r.get("Distance") or 0) / 1000
        weekly[dt.isocalendar()[:2]] += dist_km
    return [(f"W{wk}", km) for (yr, wk), km in sorted(weekly.items())]


def weekly_runs(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Returns (week_label, [run, ...]) for every ISO week present, oldest week
    first and oldest run first within each week. Each run carries the three
    metrics the bar breakdown can be sized by (distance, moving time, elevation
    gain), so the Runs view can split a week's count-height bar by any of them."""
    weekly: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        try:
            dt = datetime.strptime(r.get("Activity Date", ""), "%b %d, %Y, %I:%M:%S %p")
        except ValueError:
            continue
        weekly[dt.isocalendar()[:2]].append({
            "dt":   dt,
            "km":   float(r.get("Distance") or 0) / 1000,
            "dur":  float(r.get("Moving Time") or 0),
            "elev": float(r.get("Elevation Gain") or 0),
        })
    out = []
    for key in sorted(weekly):
        runs = sorted(weekly[key], key=lambda x: x["dt"])
        out.append((f"W{key[1]}", runs))
    return out


# ---------------------------------------------------------------------------
# Best efforts dashboard
# ---------------------------------------------------------------------------

MEDALS = ["🥇", "🥈", "🥉"]

SHARED_CSS = """
*{box-sizing:border-box;}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:1rem;background:#1a1a1a;color:#eee;}
h1{font-size:15px;font-weight:600;margin:0 0 1rem;color:#999;}
"""

BEST_EFFORTS_CSS = """
.band{margin:0 0 1rem;}
.band-header{background:#2a2a2a;border-radius:8px 8px 0 0;padding:7px 14px;border:1px solid #3a3a3a;border-bottom:none;}
.band-label{font-size:13px;font-weight:600;margin:0;color:#eee;}
.run-card{background:#222;border:1px solid #3a3a3a;padding:9px 14px;display:grid;grid-template-columns:26px 1fr auto;gap:0 10px;align-items:start;}
.run-card:last-child{border-radius:0 0 8px 8px;}
.run-card+.run-card{border-top:none;}
.medal{font-size:15px;line-height:1.7;}
.run-name{font-size:13px;font-weight:500;margin:0;color:#eee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.run-meta{font-size:11px;color:#666;margin:2px 0 0;}
.run-time{font-size:11px;color:#a07030;margin:2px 0 0;}
.pace-col{text-align:right;white-space:nowrap;}
.pace-raw{font-size:14px;font-weight:600;margin:0;color:#eee;}
.pace-ga{font-size:11px;color:#666;margin:2px 0 0;}
"""


def render_band(label: str, efforts: list[dict]) -> str:
    if not efforts:
        return ""
    last_idx = len(efforts) - 1
    cards = []
    for i, e in enumerate(efforts):
        extra_style = ' style="border-radius:0 0 8px 8px;"' if i == last_idx else ""
        meta = f"{e['date']} · {e['dist_km']:.1f}km run · {e['gain']:.0f}m gain"
        cards.append(f"""\
  <div class="run-card"{extra_style}>
    <span class="medal">{MEDALS[i]}</span>
    <div>
      <p class="run-name">{e['activity']}</p>
      <p class="run-meta">{meta}</p>
      <p class="run-time">{e['time_str']}</p>
    </div>
    <div class="pace-col">
      <p class="pace-raw">{e['raw_pace']}</p>
      <p class="pace-ga">GA {e['ga_pace']}</p>
    </div>
  </div>""")
    return f"""\
<div class="band">
  <div class="band-header"><p class="band-label">{label}</p></div>
{''.join(cards)}
</div>
"""


def generate_best_efforts(rows: list[dict], updated: str) -> str:
    bands_html = []
    for label, col_s, target_m, incl_intervals in DISTANCE_BANDS:
        efforts = top3_for_band(rows, col_s, target_m, incl_intervals)
        bands_html.append(render_band(label, efforts))

    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{SHARED_CSS}
{BEST_EFFORTS_CSS}
  </style>
</head>
<body>
<h1>Best efforts by distance — sliding window from GPS track · updated {updated}</h1>
{''.join(bands_html)}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Goal dashboard
# ---------------------------------------------------------------------------

GOAL_CSS = """
.subtitle{font-size:11px;color:#555;margin:0 0 1.25rem;}
.section{margin:0 0 1.25rem;}
.section-title{font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.05em;margin:0 0 .5rem;padding-bottom:4px;border-bottom:1px solid #2a2a2a;}

.race-card{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:12px 14px;margin-bottom:.75rem;}
.race-header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;}
.race-name{font-size:14px;font-weight:600;color:#eee;}
.race-date{font-size:11px;color:#555;}
.race-body{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px;}
.stat-label{font-size:10px;color:#555;margin:0 0 2px;}
.stat-value{font-size:16px;font-weight:700;margin:0;color:#eee;}
.stat-sub{font-size:10px;color:#666;margin:2px 0 0;}
.stat-value.green{color:#5cb85c;}
.stat-value.amber{color:#e0a020;}

.gap-labels{display:flex;justify-content:space-between;font-size:10px;color:#555;margin-bottom:3px;}
.bar-label-row{position:relative;height:30px;font-size:10px;}
.bar-label-row .current{position:absolute;top:0;transform:translateX(-50%);white-space:nowrap;color:#eee;}
.bar-label-row .target{position:absolute;top:10px;transform:translateX(-50%);white-space:nowrap;color:#aaa;font-weight:600;}
.bar-track{height:6px;background:#2a2a2a;border-radius:3px;overflow:visible;position:relative;}
.bar-fill{height:100%;border-radius:3px;}
.bar-fill.on-track{background:#5cb85c;}
.bar-fill.close{background:#e07020;}
.bar-target{position:absolute;top:-3px;bottom:-3px;width:2px;background:#fff;opacity:.5;border-radius:1px;}
.gap-note{font-size:10px;color:#666;margin-top:4px;font-style:italic;}

.speed-table{width:100%;border-collapse:collapse;}
.speed-table th{font-size:10px;font-weight:600;color:#555;text-align:left;padding:4px 6px;border-bottom:1px solid #2a2a2a;}
.speed-table th:not(:first-child){text-align:right;}
.speed-table td{font-size:12px;padding:6px 6px;border-bottom:1px solid #222;vertical-align:middle;}
.speed-table tr:last-child td{border-bottom:none;}
.speed-table td:not(:first-child){text-align:right;font-variant-numeric:tabular-nums;}
.dist-cell{font-weight:600;color:#eee;}
.target-cell{color:#888;}
.current-cell{font-weight:600;}
.current-cell.beat{color:#5cb85c;}
.current-cell.close{color:#e0a020;}
.current-cell.gap{color:#e07020;}
.delta-cell{font-size:11px;}
.delta-cell.beat{color:#5cb85c;}
.delta-cell.close{color:#e0a020;}
.delta-cell.gap{color:#e07020;}
.pill{display:inline-block;font-size:9px;padding:1px 5px;border-radius:10px;font-weight:600;margin-left:4px;vertical-align:middle;}
.pill.beat{background:#1a3a1a;color:#5cb85c;}
.pill.close{background:#3a2a0a;color:#e0a020;}
.pill.gap{background:#3a1a0a;color:#e07020;}

.pred-table{width:100%;border-collapse:collapse;}
.pred-table th{font-size:10px;font-weight:600;color:#555;text-align:left;padding:4px 6px;border-bottom:1px solid #2a2a2a;}
.pred-table th:not(:first-child){text-align:right;}
.pred-table td{font-size:12px;padding:6px 6px;border-bottom:1px solid #222;vertical-align:middle;}
.pred-table tr:last-child td{border-bottom:none;}
.pred-table td:not(:first-child){text-align:right;font-variant-numeric:tabular-nums;}
.pred-dist{font-weight:600;color:#eee;}
.pred-time{color:#eee;}
.pred-pace{color:#666;font-size:11px;}
.pred-target{font-size:10px;}
.pred-target.ok{color:#5cb85c;}
.pred-target.warn{color:#e0a020;}

.spark-wrap{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:10px 14px;}
.spark-label{font-size:10px;color:#555;margin:0 0 6px;}
.mi-tabs{display:flex;gap:6px;margin-bottom:8px;}
.mi-btn{background:#222;border:1px solid #3a3a3a;color:#888;font-size:10px;padding:3px 9px;border-radius:6px;cursor:pointer;}
.mi-btn:hover{color:#ccc;}
.mi-btn.active{color:#eee;border-color:#5cb85c;background:#1b2a1b;}
.bars{display:flex;align-items:flex-end;gap:4px;height:48px;}
.bar-col{display:flex;flex-direction:column;align-items:center;flex:1;}
.bar-rect{width:100%;border-radius:2px 2px 0 0;background:#3a5a3a;min-height:2px;}
.bar-rect.latest{background:#5cb85c;}
.bar-wk{font-size:8px;color:#444;margin-top:3px;text-align:center;}
.bar-km{font-size:8px;color:#666;margin-bottom:2px;text-align:center;}
.bar-cnt{font-size:8px;color:#666;margin-bottom:2px;text-align:center;}
/* Runs view: a count-height bar whose interior is split into one slice per run,
   each slice grown proportionally to the selected metric (dist/time/elev). */
.bar-rect.run{position:relative;background:transparent;overflow:hidden;}
.bar-stack{position:absolute;inset:0;display:flex;flex-direction:column-reverse;}
.bar-seg{width:100%;background:#3a5a3a;box-shadow:inset 0 -1px 0 rgba(0,0,0,0.45);min-height:1px;}
.bar-seg.alt{background:#46684a;}
.bar-seg.latest{background:#4f9d4f;}
.bar-seg.latest.alt{background:#5cb85c;}
.mi-metrics{display:none;gap:6px;margin-bottom:8px;}
.mi-group.view-runs .mi-metrics{display:flex;}
"""


def speed_status(current_pace_s_per_km: float, target_lo: float, target_hi: float):
    """
    current_pace_s_per_km: seconds per km for current best
    target_lo/hi: lower and upper bound of target range in seconds per km
    Returns (css_class, delta_str, pill_label)
    """
    delta = target_lo - current_pace_s_per_km  # positive = faster than lower bound
    delta_s = abs(int(delta))
    delta_m = delta_s // 60
    delta_sec = delta_s % 60
    delta_str = f"{delta_m}:{delta_sec:02d}s/km" if delta_m else f"{delta_sec}s/km"

    if current_pace_s_per_km <= target_lo:
        return "beat", f"-{delta_str}", "exceeds"
    elif current_pace_s_per_km <= target_hi:
        return "beat", f"-{delta_str}", "on target"
    else:
        over = int(current_pace_s_per_km - target_hi)
        over_m = over // 60
        over_sec = over % 60
        over_str = f"+{over_m}:{over_sec:02d}s/km" if over_m else f"+{over_sec}s/km"
        return "gap", over_str, "below target"


def render_speed_row(
    dist_label: str,
    target_range: str,
    current_pace_str: str,
    current_s_per_km: float,
    target_lo_s: float,
    target_hi_s: float,
) -> str:
    css, delta, pill = speed_status(current_s_per_km, target_lo_s, target_hi_s)
    return f"""\
    <tr>
      <td class="dist-cell">{dist_label}</td>
      <td class="target-cell">{target_range}</td>
      <td class="current-cell {css}">{current_pace_str}</td>
      <td class="delta-cell {css}">{delta} <span class="pill {css}">{pill}</span></td>
    </tr>"""


def render_pred_row(dist_label: str, time_str: str, pace_str: str, note: str, note_class: str) -> str:
    return f"""\
    <tr>
      <td class="pred-dist">{dist_label}</td>
      <td class="pred-time">{time_str}</td>
      <td class="pred-pace">{pace_str}</td>
      <td class="pred-target {note_class}">{note}</td>
    </tr>"""


def render_spark(weeks: list[tuple[str, float]]) -> str:
    if not weeks:
        return ""
    max_km = max(km for _, km in weeks) or 1
    cols = []
    for i, (label, km) in enumerate(weeks):
        pct = round(km / max_km * 100)
        is_latest = (i == len(weeks) - 1)
        extra = " latest" if is_latest else ""
        label_style = ' style="color:#5cb85c;"' if is_latest else ""
        cols.append(f"""\
    <div class="bar-col">
      <div class="bar-km">{km:.1f}</div>
      <div class="bar-rect{extra}" style="height:{pct}%;"></div>
      <div class="bar-wk"{label_style}>{label}</div>
    </div>""")
    return "\n".join(cols)


# Weeks shown per range button (ISO weeks). None = all available data.
WEEKLY_RANGES = [("3M", 13), ("6M", 26), ("1Y", 52), ("All", None)]
WEEKLY_DEFAULT = "6M"

# Runs-view metric toggle: button label, data-metric key (matches the data-*
# attribute on each segment) and default flag.
RUNS_METRICS = [("Dist", "km", True), ("Time", "dur", False), ("Elev", "elev", False)]


def render_runs_spark(weeks: list[tuple[str, list[dict]]]) -> str:
    """Bars whose height encodes the number of runs that week, with the interior
    split into one slice per run. Each slice starts sized by distance; the metric
    toggle re-grows them by duration or elevation. Earliest run sits at the
    bottom (column-reverse)."""
    if not weeks:
        return ""
    max_n = max(len(runs) for _, runs in weeks) or 1
    cols = []
    for i, (label, runs) in enumerate(weeks):
        n = len(runs)
        pct = round(n / max_n * 100)
        is_latest = (i == len(weeks) - 1)
        extra = " latest" if is_latest else ""
        label_style = ' style="color:#5cb85c;"' if is_latest else ""
        segs = []
        for j, run in enumerate(runs):
            shade = " alt" if j % 2 else ""
            segs.append(
                f'<div class="bar-seg{extra}{shade}" '
                f'data-km="{run["km"]:.3f}" data-dur="{run["dur"]:.0f}" '
                f'data-elev="{run["elev"]:.0f}" '
                f'style="flex-grow:{run["km"]:.3f}"></div>')
        cols.append(f"""\
    <div class="bar-col">
      <div class="bar-cnt">{n}</div>
      <div class="bar-rect run{extra}" style="height:{pct}%;">
        <div class="bar-stack">{"".join(segs)}</div>
      </div>
      <div class="bar-wk"{label_style}>{label}</div>
    </div>""")
    return "\n".join(cols)


_MILEAGE_TOGGLE_JS = """<script>
function miSync(g){
  var range = g.querySelector('.mi-range .mi-btn.active').getAttribute('data-range');
  var view  = g.querySelector('.mi-views .mi-btn.active').getAttribute('data-view');
  g.querySelectorAll('.mi-chart').forEach(function(c){
    c.style.display = (c.getAttribute('data-range') === range &&
                       c.getAttribute('data-view') === view) ? '' : 'none';
  });
  g.classList.toggle('view-runs', view === 'runs');
  g.querySelectorAll('.mi-label').forEach(function(l){
    l.style.display = (l.getAttribute('data-view') === view) ? '' : 'none';
  });
}
function setMileageRange(btn){
  var g = btn.closest('.mi-group');
  g.querySelectorAll('.mi-range .mi-btn').forEach(function(b){ b.classList.toggle('active', b === btn); });
  miSync(g);
}
function setMileageView(btn){
  var g = btn.closest('.mi-group');
  g.querySelectorAll('.mi-views .mi-btn').forEach(function(b){ b.classList.toggle('active', b === btn); });
  miSync(g);
}
function setMileageMetric(btn){
  var g = btn.closest('.mi-group');
  g.querySelectorAll('.mi-metrics .mi-btn').forEach(function(b){ b.classList.toggle('active', b === btn); });
  var key = btn.getAttribute('data-metric');
  g.querySelectorAll('.mi-chart[data-view="runs"] .bar-rect.run').forEach(function(bar){
    var segs = bar.querySelectorAll('.bar-seg'), total = 0;
    segs.forEach(function(s){ total += parseFloat(s.getAttribute('data-' + key)) || 0; });
    segs.forEach(function(s){
      var v = parseFloat(s.getAttribute('data-' + key)) || 0;
      s.style.flexGrow = total > 0 ? v : 1;  // all-zero week (e.g. flat runs by elev): equal slices
    });
  });
}
</script>"""


def render_mileage(weeks_runs: list[tuple[str, list[dict]]]) -> str:
    """Weekly bars wrapped in a 3M/6M/1Y/All range toggle plus a Km/Runs view
    toggle (self-contained: carries its own CSS classes and toggle script, so it
    works standalone and in the hub). Km view = total km per week; Runs view =
    bar height by run count, each bar split into per-run slices sized by the
    selected metric (distance/time/elevation)."""
    if not weeks_runs:
        return '<div class="spark-wrap"><p class="spark-label">No weekly data yet.</p></div>'
    range_btns, charts = [], []
    for label, n in WEEKLY_RANGES:
        active = label == WEEKLY_DEFAULT
        window = weeks_runs if n is None else weeks_runs[-n:]
        range_btns.append(
            f'<button class="mi-btn{" active" if active else ""}" '
            f'data-range="{label}" onclick="setMileageRange(this)">{label}</button>')
        km_window = [(lbl, sum(r["km"] for r in runs)) for lbl, runs in window]
        km_style = "" if active else ' style="display:none"'  # Km is the default view
        charts.append(
            f'<div class="mi-chart" data-range="{label}" data-view="km"{km_style}>'
            f'<div class="bars">{render_spark(km_window)}</div></div>')
        charts.append(
            f'<div class="mi-chart" data-range="{label}" data-view="runs" style="display:none">'
            f'<div class="bars">{render_runs_spark(window)}</div></div>')
    view_btns = (
        '<button class="mi-btn active" data-view="km" onclick="setMileageView(this)">Km</button>'
        '<button class="mi-btn" data-view="runs" onclick="setMileageView(this)">Runs</button>')
    metric_btns = "".join(
        f'<button class="mi-btn{" active" if default else ""}" '
        f'data-metric="{key}" onclick="setMileageMetric(this)">{lbl}</button>'
        for lbl, key, default in RUNS_METRICS)
    return (
        '<div class="spark-wrap mi-group">'
        f'<div class="mi-tabs mi-range">{"".join(range_btns)}</div>'
        f'<div class="mi-tabs mi-views">{view_btns}</div>'
        f'<div class="mi-metrics">{metric_btns}</div>'
        '<p class="spark-label mi-label" data-view="km">km per week (ISO weeks) · green = most recent</p>'
        '<p class="spark-label mi-label" data-view="runs" style="display:none">'
        'runs per week · bar height = run count, slices = each run\'s share of the selected metric</p>'
        f'{"".join(charts)}{_MILEAGE_TOGGLE_JS}</div>')


def _predict_race_time(race: Race, t10k_ga: float, riegel_a: float, riegel_b: float) -> float:
    """Predicted GA time for a configured race. Prefer the 10k-anchored Riegel (so cards
    agree with the predictions table); fall back to the user's fitted curve when no 10k
    effort exists yet."""
    if t10k_ga:
        return riegel(t10k_ga, 10_000, race.distance_m)
    if riegel_a:
        return riegel_a * race.distance_m ** riegel_b
    return 0.0


def _race_for_distance(races, dist_m: float, tol: float = 0.02) -> Race | None:
    """A configured race whose distance matches dist_m within tol (fractional)."""
    for r in races:
        if dist_m and abs(r.distance_m - dist_m) / dist_m <= tol:
            return r
    return None


_RACE_BAR_HALF_WINDOW_S_PER_KM = 60.0   # bar spans target pace ± this many s/km


def render_race_card(race: Race, pred_s: float) -> str:
    """One race-goal card, generic over single-time targets and pace-wave targets.
    The bar centres on the target pace (50%); the prediction marker sits left (slower)
    or right (faster) of it."""
    dist_m = race.distance_m
    meta = " · ".join(x for x in (race.date, race.notes) if x)
    pred_pace_s = pred_s / (dist_m / 1000) if dist_m else 0.0

    if race.target_wave_s:
        fast_s, slow_s = sorted(race.target_wave_s)   # fast = smaller time
        center_s = slow_s                             # gauge against the wave ceiling
        status = "green" if pred_s <= slow_s else "amber"
        target_label = f"{fmt_time(fast_s)}–{fmt_time(slow_s)}"
        target_sub = f"{fmt_pace_bare(slow_s, dist_m)}–{fmt_pace_bare(fast_s, dist_m)}/km"
        third_label = "vs ceiling"
        if pred_s > slow_s:
            gap_val, gap_sub = "+" + fmt_time(pred_s - slow_s), "over wave"
        elif pred_s < fast_s:
            gap_val, gap_sub = "-" + fmt_time(fast_s - pred_s), "ahead of wave"
        else:
            gap_val, gap_sub = "inside ✓", "inside wave"
    elif race.target_time_s:
        center_s = race.target_time_s
        status = "green" if pred_s <= center_s else "amber"
        target_label = fmt_time(center_s)
        target_sub = fmt_pace(center_s, dist_m)
        third_label = "Gap"
        gap = pred_s - center_s
        per_km = abs(gap) / (dist_m / 1000) if dist_m else 0
        gap_val = ("+" if gap >= 0 else "-") + fmt_time(abs(gap))
        gap_sub = f"{int(per_km)}s/km {'over' if gap > 0 else 'under'}"
    else:
        center_s = pred_s
        status = "amber"
        target_label, target_sub, third_label, gap_val, gap_sub = "—", "", "Gap", "—", ""

    center_pace_s = center_s / (dist_m / 1000) if dist_m else 0.0
    hw = _RACE_BAR_HALF_WINDOW_S_PER_KM

    def _pct(pace_s: float) -> int:
        return max(0, min(100, round((center_pace_s - pace_s) / (2 * hw) * 100 + 50)))

    bar_pct = _pct(pred_pace_s)
    fill_class = "on-track" if pred_pace_s <= center_pace_s else "close"

    return f"""\
  <div class="race-card">
    <div class="race-header">
      <span class="race-name">{race.name}</span>
      <span class="race-date">{meta}</span>
    </div>
    <div class="race-body">
      <div>
        <p class="stat-label">Target</p>
        <p class="stat-value">{target_label}</p>
        <p class="stat-sub">{target_sub}</p>
      </div>
      <div>
        <p class="stat-label">Predicted (Riegel)</p>
        <p class="stat-value {status}">{fmt_time(pred_s)}</p>
        <p class="stat-sub">{fmt_pace(pred_s, dist_m)}</p>
      </div>
      <div>
        <p class="stat-label">{third_label}</p>
        <p class="stat-value {status}">{gap_val}</p>
        <p class="stat-sub">{gap_sub}</p>
      </div>
    </div>
    <div>
      <div class="gap-labels">
        <span>slower</span>
        <span>faster</span>
      </div>
      <div class="bar-label-row">
        <span class="current" style="left:50%;">You: {fmt_pace(pred_s, dist_m)}</span>
        <span class="target" style="left:{bar_pct}%;">target: {fmt_pace(center_s, dist_m)}</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill {fill_class}" style="width:{bar_pct}%;"></div>
        <div class="bar-target" style="left:50%;"></div>
      </div>
    </div>
  </div>"""


def generate_goal_dashboard(rows: list[dict], updated: str) -> str:
    cfg = load_config()

    # --- Riegel predictions from GA best 10k ---
    best10k_efforts = top3_for_band(rows, "best_10k_s", 10_000, False)
    if not best10k_efforts:
        anchor_note = "no 10k effort found"
        t10k_ga = 0.0
    else:
        b = best10k_efforts[0]
        t10k_ga = ga_time(b["s"], b["gain"], b["dist_km"])
        anchor_note = (
            f"Riegel predictions anchored on GA 10k {fmt_time(t10k_ga)} "
            f"({fmt_pace(t10k_ga, 10_000)}) · {b['activity']} {b['date']} · "
            f"Minetti-adjusted · updated {updated}"
        )

    # --- Fit personal Riegel curve from GA best efforts ---
    # Uses 1 mile through half marathon to find the actual exponent b in T = a * D^b.
    # b > 1.06 → more falloff than average at long distances
    # b < 1.06 → better endurance scaling than average
    _RIEGEL_FIT_DEFS = [
        ("best_1mi_s",  1_609.344, False),
        ("best_5k_s",   5_000,    False),
        ("best_10k_s",  10_000,   False),
        ("best_15k_s",  15_000,   False),
        ("best_half_s", 21_097.5, False),
    ]
    _fit_points: list[tuple[float, float]] = []
    for _col, _dm, _incl in _RIEGEL_FIT_DEFS:
        _eff = top3_for_band(rows, _col, _dm, _incl)
        if _eff:
            _e = _eff[0]
            _fit_points.append((_dm, ga_time(_e["s"], _e["gain"], _e["dist_km"])))

    _riegel_a, _riegel_b = fit_riegel(_fit_points)

    if abs(_riegel_b - 1.06) < 0.005:
        _b_note = "matches standard"
    elif _riegel_b > 1.06:
        _b_note = "more falloff than avg at long distances"
    else:
        _b_note = "better endurance scaling than avg"
    training_anchor_note = (
        f"Targets from fitted Riegel curve · personal exponent b={_riegel_b:.3f} ({_b_note})"
        f" · fitted to {len(_fit_points)} distances"
    )

    speed_rows_html = []
    for label, col_s, dist_m, incl_intervals in TRAINING_DEFS:
        efforts = top3_for_band(rows, col_s, dist_m, incl_intervals)
        if not efforts:
            continue
        lo, hi = derive_training_target(_riegel_a, _riegel_b, dist_m)
        current_s_per_km = efforts[0]["s"] / (dist_m / 1000)
        current_str = fmt_pace_bare(efforts[0]["s"], dist_m) + "/km"
        speed_rows_html.append(render_speed_row(
            label, fmt_target_range(lo, hi), current_str, current_s_per_km, lo, hi,
        ))

    # --- Race goal cards (from optional config; generic predictions otherwise) ---
    race_cards_html = [
        render_race_card(r, _predict_race_time(r, t10k_ga, _riegel_a, _riegel_b))
        for r in cfg.races
    ]
    race_section = ""
    if race_cards_html:
        race_section = (
            '<div class="section">\n'
            '  <p class="section-title">Race goals</p>\n'
            f"{''.join(race_cards_html)}\n"
            '</div>\n'
        )

    # --- Riegel predictions table ---
    # Standard distances plus any configured race distance not already covered. Each row's
    # note is derived from a matching configured race (target + gap), else left blank.
    pred_rows_html = []
    if t10k_ga:
        _STD = [("5K", 5_000.0), ("10K", 10_000.0),
                ("Half marathon", 21_097.5), ("Full marathon", 42_195.0)]
        extras = [(r.name, r.distance_m) for r in cfg.races
                  if not any(abs(r.distance_m - d) / d <= 0.02 for _, d in _STD)]
        for label, dist_m in sorted(_STD + extras, key=lambda x: x[1]):
            ps = riegel(t10k_ga, 10_000, dist_m)
            race = _race_for_distance(cfg.races, dist_m)
            if race and race.target_time_s:
                gap = ps - race.target_time_s
                note = f"target {fmt_time(race.target_time_s)} · gap {'+' if gap >= 0 else '-'}{fmt_time(abs(gap))}"
                note_class = "warn" if gap > 0 else "ok"
            elif abs(dist_m - 10_000.0) < 200:
                note, note_class = "anchor effort", ""
            else:
                note, note_class = "—", ""
            pred_rows_html.append(render_pred_row(
                label, fmt_time(ps), fmt_pace(ps, dist_m), note, note_class))

    # --- Weekly mileage sparkline (with range toggle) ---
    mileage_html = render_mileage(weekly_runs(rows))

    # --- Assemble HTML ---
    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
{SHARED_CSS}
{GOAL_CSS}
  </style>
</head>
<body>
<h1>Goal gap dashboard</h1>
<p class="subtitle">{anchor_note}</p>

{race_section}
<div class="section">
  <p class="section-title">Training targets vs current bests</p>
  <p class="subtitle">{training_anchor_note}</p>
  <table class="speed-table">
    <thead>
      <tr>
        <th>Distance</th>
        <th>Target range</th>
        <th>Current best</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
{''.join(speed_rows_html)}
    </tbody>
  </table>
</div>

<div class="section">
  <p class="section-title">Riegel race predictions</p>
  <table class="pred-table">
    <tbody>
{''.join(pred_rows_html)}
    </tbody>
  </table>
</div>

<div class="section">
  <p class="section-title">Weekly mileage</p>
  {mileage_html}
</div>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Body fragments — return inner HTML only, for embedding in the hub
# ---------------------------------------------------------------------------

def body_best_efforts(rows: list[dict], updated: str) -> str:
    html = generate_best_efforts(rows, updated)
    s = html.index('<body>') + len('<body>')
    e = html.index('</body>')
    return html[s:e].strip()


def body_goal_dashboard(rows: list[dict], updated: str) -> str:
    html = generate_goal_dashboard(rows, updated)
    s = html.index('<body>') + len('<body>')
    e = html.index('</body>')
    return html[s:e].strip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    here = Path(__file__).resolve().parent.parent

    # Find the most recent *_strava.csv in csv_data/
    csv_dir = here / "csv_data"
    candidates = sorted(csv_dir.glob("*_strava.csv"))
    if not candidates:
        sys.exit(f"Error: no *_strava.csv found in {csv_dir}")
    csv_path = candidates[-1]
    print(f"Using: {csv_path.name}")

    rows = load_rows(csv_path)
    if not rows:
        sys.exit("Error: CSV is empty")

    # Derive update date from CSV filename (YYYY-MM-DD_strava.csv) or today
    try:
        date_str = csv_path.stem.split("_")[0]
        updated = datetime.strptime(date_str, "%Y-%m-%d").strftime("%#d %b %Y")
    except (ValueError, IndexError):
        updated = datetime.today().strftime("%#-d-%b-%Y")

    dashboards_dir = here / "dashboards"
    dashboards_dir.mkdir(exist_ok=True)

    best_path = dashboards_dir / "top_runs_by_distance.html"
    best_path.write_text(generate_best_efforts(rows, updated), encoding="utf-8")
    print(f"Written: {best_path}")

    goal_path = dashboards_dir / "goal_dashboard.html"
    goal_path.write_text(generate_goal_dashboard(rows, updated), encoding="utf-8")
    print(f"Written: {goal_path}")


if __name__ == "__main__":
    main()