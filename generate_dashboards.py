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
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


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


def riegel(t1: float, d1: float, d2: float) -> float:
    return t1 * (d2 / d1) ** 1.06


# ---------------------------------------------------------------------------
# Data loading
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
    """Returns list of (week_label, km) for Apr 2026 onwards, sorted."""
    weekly: dict[tuple, float] = defaultdict(float)
    for r in rows:
        try:
            dt = datetime.strptime(r.get("Activity Date", ""), "%b %d, %Y, %I:%M:%S %p")
        except ValueError:
            continue
        if dt.year < 2026 or dt.month < 4:
            continue
        dist_km = float(r.get("Distance") or 0) / 1000
        weekly[dt.isocalendar()[:2]] += dist_km
    return [(f"W{wk}", km) for (yr, wk), km in sorted(weekly.items())]


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
.gap-labels .current{color:#eee;}
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
.bars{display:flex;align-items:flex-end;gap:4px;height:48px;}
.bar-col{display:flex;flex-direction:column;align-items:center;flex:1;}
.bar-rect{width:100%;border-radius:2px 2px 0 0;background:#3a5a3a;min-height:2px;}
.bar-rect.latest{background:#5cb85c;}
.bar-wk{font-size:8px;color:#444;margin-top:3px;text-align:center;}
.bar-km{font-size:8px;color:#666;margin-bottom:2px;text-align:center;}
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


def generate_goal_dashboard(rows: list[dict], updated: str) -> str:
    # --- Riegel predictions from GA best 10k ---
    best10k_efforts = top3_for_band(rows, "best_10k_s", 10_000, False)
    if not best10k_efforts:
        anchor_note = "no 10k effort found"
        preds = {}
    else:
        b = best10k_efforts[0]
        t10k_ga = ga_time(b["s"], b["gain"], b["dist_km"])
        preds = {
            "5k":   riegel(t10k_ga, 10_000, 5_000),
            "10k":  riegel(t10k_ga, 10_000, 10_000),
            "14k":  riegel(t10k_ga, 10_000, 14_000),
            "half": riegel(t10k_ga, 10_000, 21_097.5),
            "full": riegel(t10k_ga, 10_000, 42_195),
        }
        anchor_note = (
            f"Riegel predictions anchored on GA 10k {fmt_time(t10k_ga)} "
            f"({fmt_pace(t10k_ga, 10_000)}) · {b['activity']} {b['date']} · "
            f"Minetti-adjusted · updated {updated}"
        )

    # --- 14k race card ---
    pred_14k_s   = preds.get("14k", 0)
    pred_14k_gap = pred_14k_s - 4200  # target 1:10:00 = 4200s
    gap_14k_str  = ("+" if pred_14k_gap >= 0 else "") + fmt_time(abs(pred_14k_gap))
    gap_14k_pace_s = pred_14k_gap / 14  # seconds per km gap
    gap_14k_pace_str = f"{int(abs(gap_14k_pace_s))}s/km {'over' if pred_14k_gap > 0 else 'under'}"
    # bar: pace range 6:30 (390s) to 4:30 (270s) = 120s window
    # current pred pace in s/km
    pred_14k_pace_s = pred_14k_s / 14
    bar_14k_pct = max(0, min(100, round((390 - pred_14k_pace_s) / 120 * 100)))
    target_14k_pct = round((390 - 300) / 120 * 100)  # 5:00/km = 300s

    # --- Half marathon card ---
    pred_half_s     = preds.get("half", 0)
    pred_half_gap   = 7200 - pred_half_s   # vs 2:00:00, positive = under
    gap_half_str    = ("-" if pred_half_gap >= 0 else "+") + fmt_time(abs(pred_half_gap))
    pred_half_pace_s = pred_half_s / 21.0975
    # bar: 2:20 (140min) to 1:30 (90min) = 50min = 3000s window; wave entry at 1:40 (100min)
    bar_half_pct  = max(0, min(100, round((8400 - pred_half_s) / 3000 * 100)))
    target_half_pct = round((8400 - 6000) / 3000 * 100)  # 1:40:00

    # --- Speed session targets ---
    # Current best paces (s/km) derived from best efforts
    best_400m  = top3_for_band(rows, "best_400m_s",  400,      True)
    best_800m  = top3_for_band(rows, "best_1/2mi_s", 804.672,  True)
    best_1km   = top3_for_band(rows, "best_1km_s",   1_000,    False)
    best_1mi   = top3_for_band(rows, "best_1mi_s",   1_609.344, False)
    best_5k    = top3_for_band(rows, "best_5k_s",    5_000,    False)

    def best_pace_s(efforts, dist_m):
        if not efforts:
            return None
        return efforts[0]["s"] / (dist_m / 1000)

    speed_rows = [
        # (label, target_range_str, dist_m, best_efforts, target_lo_s/km, target_hi_s/km)
        ("400m reps",      "4:47–4:59/km", 400,       best_400m, 287, 299),
        ("800m reps",      "4:56–5:11/km", 804.672,   best_800m, 296, 311),
        ("1km reps",       "5:08–5:20/km", 1_000,     best_1km,  308, 320),
        ("1mi reps",       "5:17–5:24/km", 1_609.344, best_1mi,  317, 324),
        ("Tempo 20–40min", "5:31–5:49/km", 5_000,     best_5k,   331, 349),
    ]

    speed_rows_html = []
    for label, target_range, dist_m, efforts, lo, hi in speed_rows:
        p = best_pace_s(efforts, dist_m)
        if p is None:
            continue
        current_str = fmt_pace_bare(efforts[0]["s"], dist_m) + "/km"
        speed_rows_html.append(render_speed_row(label, target_range, current_str, p, lo, hi))

    # --- Riegel predictions table ---
    pred_rows_html = []
    if preds:
        pred_rows_html.append(render_pred_row(
            "5K",
            fmt_time(preds["5k"]),
            fmt_pace(preds["5k"], 5_000),
            "well under target", "ok",
        ))
        pred_rows_html.append(render_pred_row(
            "10K",
            fmt_time(preds["10k"]),
            fmt_pace(preds["10k"], 10_000),
            "anchor effort", "",
        ))
        pred_rows_html.append(render_pred_row(
            "14K race",
            fmt_time(preds["14k"]),
            fmt_pace(preds["14k"], 14_000),
            f"target 1:10:00 · gap {gap_14k_str}", "warn",
        ))
        pred_rows_html.append(render_pred_row(
            "Half marathon",
            fmt_time(preds["half"]),
            fmt_pace(preds["half"], 21_097.5),
            f"inside 100–120 min wave ✓", "ok",
        ))
        pred_rows_html.append(render_pred_row(
            "Full marathon",
            fmt_time(preds["full"]),
            fmt_pace(preds["full"], 42_195),
            "—", "",
        ))

    # --- Weekly mileage sparkline ---
    weeks = weekly_mileage(rows)
    spark_cols = render_spark(weeks)

    # --- Assemble HTML ---
    half_status_class = "green" if pred_half_gap >= 0 else "amber"
    half_gap_display  = gap_half_str

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

<div class="section">
  <p class="section-title">Race goals</p>

  <div class="race-card">
    <div class="race-header">
      <span class="race-name">14km race</span>
      <span class="race-date">Coming up soon</span>
    </div>
    <div class="race-body">
      <div>
        <p class="stat-label">Target</p>
        <p class="stat-value">1:10:00</p>
        <p class="stat-sub">5:00/km</p>
      </div>
      <div>
        <p class="stat-label">Predicted (Riegel)</p>
        <p class="stat-value amber">{fmt_time(pred_14k_s)}</p>
        <p class="stat-sub">{fmt_pace(pred_14k_s, 14_000)}</p>
      </div>
      <div>
        <p class="stat-label">Gap</p>
        <p class="stat-value amber">{gap_14k_str}</p>
        <p class="stat-sub">{gap_14k_pace_str}</p>
      </div>
    </div>
    <div>
      <div class="gap-labels">
        <span>slow</span>
        <span class="current">you: {fmt_pace(pred_14k_s, 14_000)}</span>
        <span>target: 5:00/km</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill close" style="width:{bar_14k_pct}%;"></div>
        <div class="bar-target" style="left:{target_14k_pct}%;"></div>
      </div>
      <p class="gap-note">Grade adjustment accounts for much of this gap on a flat race course.</p>
    </div>
  </div>

  <div class="race-card">
    <div class="race-header">
      <span class="race-name">Half marathon — Sydney Olympic Park</span>
      <span class="race-date">Sep 2026 · flat course · target wave 100–120 min</span>
    </div>
    <div class="race-body">
      <div>
        <p class="stat-label">Target wave</p>
        <p class="stat-value">1:40–2:00</p>
        <p class="stat-sub">4:44–5:41/km</p>
      </div>
      <div>
        <p class="stat-label">Predicted (Riegel)</p>
        <p class="stat-value {half_status_class}">{fmt_time(pred_half_s)}</p>
        <p class="stat-sub">{fmt_pace(pred_half_s, 21_097.5)}</p>
      </div>
      <div>
        <p class="stat-label">vs 2:00 ceiling</p>
        <p class="stat-value {half_status_class}">{half_gap_display}</p>
        <p class="stat-sub">{'inside wave ✓' if pred_half_gap >= 0 else 'over ceiling'}</p>
      </div>
    </div>
    <div>
      <div class="gap-labels">
        <span>2:20</span>
        <span class="current">you: {fmt_time(pred_half_s)}</span>
        <span>wave open: 1:40</span>
      </div>
      <div class="bar-track">
        <div class="bar-fill on-track" style="width:{bar_half_pct}%;"></div>
        <div class="bar-target" style="left:{target_half_pct}%;"></div>
      </div>
      <p class="gap-note">16 weeks to build toward sub-1:50 stretch goal.</p>
    </div>
  </div>
</div>

<div class="section">
  <p class="section-title">Speed session targets vs current bests</p>
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
  <p class="section-title">Weekly mileage — Apr 2026 onwards</p>
  <div class="spark-wrap">
    <p class="spark-label">km per week (ISO weeks) · green = most recent</p>
    <div class="bars">
{spark_cols}
    </div>
  </div>
</div>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    here = Path(__file__).parent

    # Find the most recent *_strava.csv in the same folder as this script
    candidates = sorted(here.glob("*_strava.csv"))
    if not candidates:
        sys.exit(f"Error: no *_strava.csv found in {here}")
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

    best_path = here / "top_runs_by_distance.html"
    best_path.write_text(generate_best_efforts(rows, updated), encoding="utf-8")
    print(f"Written: {best_path}")

    goal_path = here / "goal_dashboard.html"
    goal_path.write_text(generate_goal_dashboard(rows, updated), encoding="utf-8")
    print(f"Written: {goal_path}")


if __name__ == "__main__":
    main()