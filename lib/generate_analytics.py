#!/usr/bin/env python3
"""
generate_analytics.py
Builds a third dashboard, analytics_dashboard.html, from a strava_compile.py
enriched CSV. Everything here is derived from PACE / DISTANCE / ELEVATION only,
because the source device records no heart rate and no power. These are the
analyses that paid Strava / Runalyze / SmashRun offer that your current two
dashboards do not yet cover, restricted to what your data can actually support.

Sections:
  1. Fitness / Fatigue / Form (CTL / ATL / TSB) from a pace-based load score
  2. Acute:Chronic Workload Ratio (ACWR) with injury-risk sweet-spot band
  3. Training monotony and strain (Foster)
  4. Critical-speed model (running analog of Strava's power curve)
  5. Pace-zone time-in-zone distribution (derived from critical speed)
  6. VO2max / VDOT estimate trend (Daniels, from GA best efforts)
  7. Cadence trend
  8. Training-log calendar heatmap

Run after generate_dashboards.py:
    python generate_analytics.py
Writes analytics_dashboard.html next to the script.
"""

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# Enriched-CSV stream fields (fit_distance_stream) can exceed the default
# 128 KB per-field limit at higher sampling density.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
from pathlib import Path

from grade import minetti_cost, COST_FLAT, ga_time
from generate_dashboards import fit_riegel


def num(row: dict, key: str):
    try:
        v = row.get(key, "")
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Activity dates are localized from each run's first GPS coordinate; see
# localtime.parse_date. Re-exported here so existing callers keep importing it
# from generate_analytics.
from localtime import parse_date  # noqa: E402


def fmt_time(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(round(s % 60))
    if sec == 60:
        m, sec = m + 1, 0
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_pace_from_s_per_km(s_per_km: float) -> str:
    m, sec = int(s_per_km // 60), int(round(s_per_km % 60))
    if sec == 60:
        m, sec = m + 1, 0
    return f"{m}:{sec:02d}"


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 1. Pace-based training load -> CTL / ATL / TSB
# ---------------------------------------------------------------------------
# Strava's Fitness & Freshness uses a heart-rate or power load. With neither
# available, we build an equivalent load from grade-adjusted effort: a session's
# load is its grade-adjusted distance scaled by intensity (how fast it was run
# relative to the athlete's recent best pace). This is the same principle
# Runalyze falls back to ("TRIMP-less" load) when HR is missing.

def session_loads(rows: list[dict]) -> list[tuple[datetime, float]]:
    """Return [(date, load)] sorted by date. Load is a unitless stress score."""
    # Establish a reference pace: the fastest 5k-equivalent GA pace seen, as a
    # rough threshold pace. Fall back to overall median pace if no efforts.
    ref_paces = []
    for r in rows:
        dist_km = num(r, "Distance")
        moving = num(r, "Moving Time")
        if dist_km and moving and dist_km > 0:
            gain = num(r, "Elevation Gain") or 0
            ga_s = ga_time(moving, gain, dist_km / 1000)
            ref_paces.append(ga_s / (dist_km / 1000))  # s per km, dist in m
    if not ref_paces:
        return []
    ref_paces.sort()
    # threshold pace ~ 15th percentile (fast end), guards against outliers
    threshold_pace = ref_paces[max(0, int(len(ref_paces) * 0.15))]

    loads = []
    for r in rows:
        dt = parse_date(r)
        dist_m = num(r, "Distance")
        moving = num(r, "Moving Time")
        if not dt or not dist_m or not moving or dist_m <= 0:
            continue
        dist_km = dist_m / 1000
        gain = num(r, "Elevation Gain") or 0
        ga_s = ga_time(moving, gain, dist_km)
        pace = ga_s / dist_km  # s/km GA
        # intensity factor: faster than threshold -> >1, capped for sanity
        intensity = threshold_pace / pace if pace > 0 else 1.0
        intensity = max(0.5, min(1.5, intensity))
        # load grows with distance and the square of intensity (TSS-like)
        load = dist_km * 10 * intensity ** 2
        loads.append((dt.date(), load))
    loads.sort(key=lambda x: x[0])
    return loads


def daily_series(loads: list[tuple]) -> list[tuple]:
    """Fill every calendar day (zero on rest days) so EWMA decay is correct."""
    if not loads:
        return []
    by_day = defaultdict(float)
    for d, l in loads:
        by_day[d] += l
    start, end = min(by_day), max(by_day)
    out = []
    cur = start
    while cur <= end:
        out.append((cur, by_day.get(cur, 0.0)))
        cur += timedelta(days=1)
    return out


def ctl_atl_tsb(daily: list[tuple]) -> list[dict]:
    """Exponentially weighted: CTL 42-day, ATL 7-day, TSB = prev CTL - prev ATL."""
    ctl = atl = 0.0
    k_ctl = 1 - math.exp(-1 / 42)
    k_atl = 1 - math.exp(-1 / 7)
    out = []
    for d, load in daily:
        prev_ctl, prev_atl = ctl, atl
        ctl = prev_ctl + k_ctl * (load - prev_ctl)
        atl = prev_atl + k_atl * (load - prev_atl)
        out.append({"date": d, "load": load, "ctl": ctl, "atl": atl,
                    "tsb": prev_ctl - prev_atl})
    return out


def acwr_series(daily: list[tuple]) -> list[dict]:
    """Acute:chronic workload ratio. Acute = 7-day sum, chronic = 28-day avg week."""
    loads = [l for _, l in daily]
    dates = [d for d, _ in daily]
    out = []
    for i in range(len(daily)):
        acute = sum(loads[max(0, i - 6):i + 1])
        chronic_window = loads[max(0, i - 27):i + 1]
        chronic = sum(chronic_window) / len(chronic_window) * 7 if chronic_window else 0
        ratio = acute / chronic if chronic > 0 else 0
        out.append({"date": dates[i], "acwr": ratio})
    return out


def monotony_strain(daily: list[tuple]) -> list[dict]:
    """Foster monotony = weekly mean / weekly SD; strain = weekly load * monotony."""
    out = []
    loads = [l for _, l in daily]
    dates = [d for d, _ in daily]
    for i in range(len(daily)):
        window = loads[max(0, i - 6):i + 1]
        if len(window) < 2:
            out.append({"date": dates[i], "monotony": 0, "strain": 0})
            continue
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        sd = math.sqrt(var)
        monotony = mean / sd if sd > 0 else 0
        weekly_load = sum(window)
        out.append({"date": dates[i], "monotony": monotony,
                    "strain": weekly_load * monotony})
    return out


# Strava caps bulk-export downloads to one packet per week, so between packets the
# user takes rest days with no fresh data. Let them preview up to a week of rest.
MAX_AS_OF_DAYS = 7


def _tsb_class(tsb: float) -> str:
    return "green" if tsb > 5 else ("amber" if tsb > -10 else "red")


def _tsb_note(tsb: float) -> str:
    return ("fresh / tapered" if tsb > 5 else
            "neutral, building" if tsb > -10 else "fatigued, watch recovery")


def _acwr_class(acwr: float) -> str:
    return "green" if 0.8 <= acwr <= 1.3 else ("amber" if acwr < 0.8 else "red")


def _acwr_note(acwr: float) -> str:
    return ("in the sweet spot" if 0.8 <= acwr <= 1.3 else
            "detraining risk (too low)" if acwr < 0.8 else "spike, elevated injury risk")


def _mono_class(mono: float) -> str:
    return "green" if mono < 1.5 else ("amber" if mono < 2.0 else "red")


def _strain_baseline(mono: list[dict]) -> tuple[float, float]:
    """Personalised safe-strain ceiling: mean + 1 SD (and + 2 SD) of trailing weekly
    strain, excluding the current week so a live spike doesn't raise its own bar.
    Returns (safe_strain, strain_high); (0.0, 0.0) when there isn't enough history."""
    strain_hist = [x["strain"] for x in mono[:-7] if x["strain"] > 0][-90:]
    if len(strain_hist) < 8:
        return 0.0, 0.0
    s_mean = sum(strain_hist) / len(strain_hist)
    s_sd = math.sqrt(sum((x - s_mean) ** 2 for x in strain_hist) / len(strain_hist))
    return s_mean + s_sd, s_mean + 2 * s_sd


def _strain_class(strain: float, safe_strain: float, strain_high: float) -> str:
    if safe_strain <= 0:
        return ""
    return ("green" if strain < safe_strain
            else "amber" if strain < strain_high else "red")


def _strain_sub(safe_strain: float) -> str:
    return (f"safe &lt; {safe_strain:.0f} (your baseline)" if safe_strain > 0
            else "weekly load × monotony")


def extend_daily(daily: list[tuple], k: int) -> list[tuple]:
    """Append `k` zero-load rest days after the last day of `daily`.

    Moving the as-of date forward past the last run means `k` calendar days with no
    training, over which the load-derived metrics decay. Returns a new list; the
    input is not mutated. `k <= 0` returns a copy unchanged.
    """
    if not daily or k <= 0:
        return list(daily)
    last_date = daily[-1][0]
    return list(daily) + [(last_date + timedelta(days=i), 0.0)
                          for i in range(1, k + 1)]


def as_of_blocks(rows: list[dict]) -> tuple[str, list[dict]]:
    """Precompute the injury-risk tab's display values for as-of offsets 0..MAX_AS_OF_DAYS.

    Offset k = k rest days after the last run. Block 0 reproduces exactly the values
    baked into the static HTML; the browser swaps in block k when the user moves the
    date picker forward. Every numeric field is a pre-formatted string matching its
    template's format spec, so the client does no number formatting (guarantees the
    offset-0 block is byte-identical to the static render and avoids rounding drift).

    The strain baseline (safe/high ceiling) is fixed from the real data, so only the
    current-week strain value moves as rest days are added.

    Returns (last_run_date_iso, [block0, block1, ...]); ("", []) with no data.
    """
    base_daily = daily_series(session_loads(rows))
    if not base_daily:
        return "", []
    last_date_iso = base_daily[-1][0].isoformat()
    safe_strain, strain_high = _strain_baseline(monotony_strain(base_daily))
    blocks = []
    for k in range(MAX_AS_OF_DAYS + 1):
        daily_k = extend_daily(base_daily, k)
        fit = ctl_atl_tsb(daily_k)[-1]
        aw = acwr_series(daily_k)[-1]["acwr"]
        mo = monotony_strain(daily_k)[-1]
        tsb, strain = fit["tsb"], mo["strain"]
        blocks.append({
            "ctl": f"{fit['ctl']:.0f}",
            "atl": f"{fit['atl']:.0f}",
            "tsb": f"{tsb:+.0f}",
            "tsbClass": _tsb_class(tsb),
            "tsbNote": _tsb_note(tsb),
            "acwr": f"{aw:.2f}",
            "acwrClass": _acwr_class(aw),
            "acwrNote": _acwr_note(aw),
            "mono": f"{mo['monotony']:.2f}",
            "monoClass": _mono_class(mo["monotony"]),
            "strain": f"{strain:.0f}",
            "strainClass": _strain_class(strain, safe_strain, strain_high),
            "strainSub": _strain_sub(safe_strain),
        })
    return last_date_iso, blocks


def _asof_chart_variants(base_daily: list[tuple]) -> tuple[list[str], list[str], list[str]]:
    """Pre-render the three injury-tab charts for as-of offsets 0..MAX_AS_OF_DAYS.

    Each list holds one full `_ranged` group per offset, rendered on the daily series
    extended by k zero-load rest days. Offset 0 reproduces the static charts exactly
    (same renderers), so swapping block 0 back in is a no-op visually.
    """
    fit_list: list[str] = []
    acwr_list: list[str] = []
    strain_list: list[str] = []
    for k in range(MAX_AS_OF_DAYS + 1):
        daily_k = extend_daily(base_daily, k)
        fit_k = ctl_atl_tsb(daily_k)
        acwr_k = acwr_series(daily_k)
        mono_k = monotony_strain(daily_k)
        fit_list.append(_ranged(DAILY_RANGES, "3M", lambda win, s=fit_k: _line_chart(
            _last_days(s, win),
            [("ctl", "#5cb85c", "Fitness (CTL)"),
             ("atl", "#e0a020", "Fatigue (ATL)"),
             ("tsb", "#5b8fd9", "Form (TSB)")],
            yfmt=lambda v: f"{round(v / 5) * 5:g}",
        )))
        acwr_list.append(_ranged(DAILY_RANGES, "3M", lambda win, s=acwr_k: _bar_chart(
            [x["date"].strftime("%#d/%#m") for x in _last_days(s, win)],
            [round(x["acwr"], 2) for x in _last_days(s, win)], band=(0.8, 1.3),
            yfmt=lambda v: f"{v:.1f}",
        )))
        strain_list.append(_ranged(DAILY_RANGES, "3M", lambda win, s=mono_k: _bar_chart(
            [x["date"].strftime("%#d/%#m") for x in _last_days(s, win)],
            [round(x["strain"]) for x in _last_days(s, win)], color="#5b8fd9",
            yfmt=lambda v: f"{v:.0f}",
        )))
    return fit_list, acwr_list, strain_list


# ---------------------------------------------------------------------------
# 4 & 5. Critical-speed model and pace zones (running power-curve analog)
# ---------------------------------------------------------------------------
# Best-effort columns give time at fixed distances. CS model: distance = CS*t + D'
# Linear regression of distance (m) on time (s) -> slope CS (m/s), intercept D' (m).

CS_DISTANCES = [
    ("best_400m_s", 400.0),
    ("best_1/2mi_s", 804.672),
    ("best_1km_s", 1_000.0),
    ("best_1mi_s", 1_609.344),
    ("best_2mi_s", 3_218.69),
    ("best_5k_s", 5_000.0),
    ("best_10k_s", 10_000.0),
    ("best_15k_s", 15_000.0),
    ("best_half_s", 21_097.5),
]


def best_effort_points(rows: list[dict]) -> list[tuple]:
    """Best (fastest) GA time per distance across all activities -> (dist_m, time_s)."""
    best = {}
    for col, dist_m in CS_DISTANCES:
        for r in rows:
            s = num(r, col)
            if not s or s <= 0:
                continue
            dist_km = (num(r, "Distance") or 0) / 1000
            gain = num(r, "Elevation Gain") or 0
            ga_s = ga_time(s, gain, dist_km)
            if dist_m not in best or ga_s < best[dist_m]:
                best[dist_m] = ga_s
    return sorted(best.items())


def fit_critical_speed(points: list[tuple]) -> tuple:
    """Linear: dist = CS * time + Dprime. Returns (CS_mps, Dprime_m, r2)."""
    # use longer-distance points (>= 1200m) where CS model is valid
    pts = [(t, d) for d, t in points if d >= 1200]
    if len(pts) < 2:
        return None, None, None
    xs = [t for t, _ in pts]
    ys = [d for _, d in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    ssxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ssxx = sum((x - mx) ** 2 for x in xs)
    if ssxx == 0:
        return None, None, None
    cs = ssxy / ssxx
    dprime = my - cs * mx
    # r^2
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (cs * x + dprime)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return cs, dprime, r2


def pace_zones_from_cs(cs_mps: float) -> list[dict]:
    """5 running pace zones expressed as pace ranges around critical speed."""
    if not cs_mps or cs_mps <= 0:
        return []
    cs_pace = 1000 / cs_mps  # s/km at critical speed (~threshold)
    # zone boundaries as % of threshold pace (slower = higher s/km)
    zones = [
        ("Z1 easy",        cs_pace * 1.30, cs_pace * 1.155),
        ("Z2 endurance",   cs_pace * 1.155, cs_pace * 1.08),
        ("Z3 tempo",       cs_pace * 1.08, cs_pace * 1.02),
        ("Z4 threshold",   cs_pace * 1.02, cs_pace * 0.97),
        ("Z5 VO2/speed",   cs_pace * 0.97, cs_pace * 0.85),
    ]
    return [{"name": n, "lo_s": lo, "hi_s": hi} for n, lo, hi in zones]


def time_in_pace_zones(rows: list[dict], zones: list[dict]) -> list[float]:
    """Approximate seconds in each zone using each run's average GA pace."""
    if not zones:
        return []
    secs = [0.0] * len(zones)
    for r in rows:
        dist_km = (num(r, "Distance") or 0) / 1000
        moving = num(r, "Moving Time")
        if not dist_km or not moving or dist_km <= 0:
            continue
        gain = num(r, "Elevation Gain") or 0
        ga_s = ga_time(moving, gain, dist_km)
        pace = ga_s / dist_km  # s/km
        for i, z in enumerate(zones):
            # zone lo is the slow bound (larger s/km), hi is fast bound
            if z["hi_s"] <= pace <= z["lo_s"]:
                secs[i] += moving
                break
        else:
            if pace > zones[0]["lo_s"]:
                secs[0] += moving
            elif pace < zones[-1]["hi_s"]:
                secs[-1] += moving
    return secs


# ---------------------------------------------------------------------------
# 6. VDOT / VO2max estimate trend (Daniels)
# ---------------------------------------------------------------------------

def daniels_vo2max(dist_m: float, time_s: float) -> float:
    """Jack Daniels VDOT estimate from a single time-trial effort."""
    t_min = time_s / 60
    velocity = dist_m / t_min  # m/min
    pct_max = 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + \
        0.2989558 * math.exp(-0.1932605 * t_min)
    vo2 = -4.60 + 0.182258 * velocity + 0.000104 * velocity ** 2
    return vo2 / pct_max


def vdot_trend(rows: list[dict]) -> list[tuple]:
    """Per-month best VDOT from any 1mi-10k GA effort. Returns [(label, vdot)]."""
    monthly = defaultdict(float)
    cols = [("best_1mi_s", 1_609.344), ("best_2mi_s", 3_218.69),
            ("best_5k_s", 5_000.0), ("best_10k_s", 10_000.0)]
    for r in rows:
        dt = parse_date(r)
        if not dt:
            continue
        dist_km = (num(r, "Distance") or 0) / 1000
        gain = num(r, "Elevation Gain") or 0
        best_v = 0.0
        for col, dist_m in cols:
            s = num(r, col)
            if not s or s <= 0:
                continue
            ga_s = ga_time(s, gain, dist_km)
            v = daniels_vo2max(dist_m, ga_s)
            best_v = max(best_v, v)
        if best_v > 0:
            key = (dt.year, dt.month)
            monthly[key] = max(monthly[key], best_v)
    out = []
    for (y, m), v in sorted(monthly.items()):
        out.append((datetime(y, m, 1).strftime("%b"), round(v, 1)))
    return out


# ---------------------------------------------------------------------------
# 7. Cadence trend
# ---------------------------------------------------------------------------

def cadence_trend(rows: list[dict]) -> list[tuple]:
    monthly = defaultdict(list)
    for r in rows:
        dt = parse_date(r)
        cad = num(r, "fit_avg_cadence")
        if dt and cad:
            monthly[(dt.year, dt.month)].append(cad)
    out = []
    for (y, m), vals in sorted(monthly.items()):
        out.append((datetime(y, m, 1).strftime("%b"), round(sum(vals) / len(vals), 1)))
    return out


def period_totals(rows: list[dict], by: str) -> list[dict]:
    """Per-period totals: [{date, dist (km), elev (m), time (h), runs, breakdown}],
    gap-filled.

    by='week' buckets by ISO-week Monday, by='month' by first-of-month. Empty
    periods between the first and last activity are filled with zeros so the
    line drops to the axis on rest weeks, like Strava's weekly graph.

    `breakdown` is the per-run metric list (oldest run first) so the +Runs
    overlay bar can be split into one notch per run, each sized by the run's
    share of the period's distance / elevation / time.
    """
    def _new():
        return {"dist": 0.0, "elev": 0.0, "time": 0.0, "runs": 0, "runs_bd": []}
    bucket = defaultdict(_new)
    for r in rows:
        dt = parse_date(r)
        dist_m = num(r, "Distance")
        if not dt or not dist_m or dist_m <= 0:
            continue
        d = dt.date()
        key = d - timedelta(days=d.weekday()) if by == "week" else d.replace(day=1)
        b = bucket[key]
        km   = dist_m / 1000
        elev = num(r, "Elevation Gain") or 0
        hrs  = (num(r, "Moving Time") or 0) / 3600
        b["dist"] += km
        b["elev"] += elev
        b["time"] += hrs
        b["runs"] += 1
        b["runs_bd"].append((dt, km, elev, hrs))
    if not bucket:
        return []
    keys = sorted(bucket)
    out, cur, end = [], keys[0], keys[-1]
    while cur <= end:
        b = bucket.get(cur)
        if b is None:
            breakdown, runs = [], 0
            dist = elev = time = 0.0
        else:
            runs = b["runs"]
            dist, elev, time = b["dist"], b["elev"], b["time"]
            breakdown = [
                {"dist": round(km, 3), "elev": round(ev), "time": round(hr, 3)}
                for _dt, km, ev, hr in sorted(b["runs_bd"], key=lambda x: x[0])
            ]
        out.append({"date": cur.isoformat(),
                    "dist": round(dist, 1), "elev": round(elev),
                    "time": round(time, 2), "runs": runs,
                    "breakdown": breakdown})
        cur = (cur + timedelta(days=7) if by == "week"
               else (cur.replace(day=28) + timedelta(days=4)).replace(day=1))
    return out


# ---------------------------------------------------------------------------
# 8. Calendar heatmap
# ---------------------------------------------------------------------------

def calendar_data(loads: list[tuple]) -> dict:
    by_day = defaultdict(float)
    for d, l in loads:
        by_day[d] += l
    return dict(by_day)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SHARED_CSS = """
*{box-sizing:border-box;}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:1rem;background:#1a1a1a;color:#eee;}
h1{font-size:15px;font-weight:600;margin:0 0 .25rem;color:#999;}
.subtitle{font-size:11px;color:#555;margin:0 0 1.25rem;}
.section{margin:0 0 1.5rem;}
.section-title{font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.05em;margin:0 0 .5rem;padding-bottom:4px;border-bottom:1px solid #2a2a2a;}
.note{font-size:10px;color:#555;margin:6px 0 0;font-style:italic;}
.card{background:#222;border:1px solid #3a3a3a;border-radius:8px;padding:12px 14px;margin-bottom:.75rem;}
.stat-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:6px;}
.stat{flex:1;min-width:90px;}
.stat-label{font-size:10px;color:#555;margin:0 0 2px;}
.stat-value{font-size:18px;font-weight:700;margin:0;color:#eee;}
.stat-value.green{color:#5cb85c;}
.stat-value.amber{color:#e0a020;}
.stat-value.red{color:#d9534f;}
.stat-sub{font-size:10px;color:#666;margin:2px 0 0;}
svg{display:block;width:100%;height:auto;}
.rng-tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px;}
.rng-btn{background:#222;border:1px solid #3a3a3a;color:#888;font-size:10px;padding:3px 9px;border-radius:6px;cursor:pointer;}
.rng-btn:hover{color:#ccc;}
.rng-btn.active{color:#eee;border-color:#5cb85c;background:#1b2a1b;}
.sub-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 1.25rem;}
.sub-tab{background:#222;border:1px solid #3a3a3a;color:#888;font-size:12px;font-weight:600;padding:6px 14px;border-radius:8px;cursor:pointer;}
.sub-tab:hover{color:#ccc;}
.sub-tab.active{color:#eee;border-color:#5cb85c;background:#1b2a1b;}
.asof-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:#1b2330;border:1px solid #2f3b4d;border-radius:8px;padding:8px 12px;margin:0 0 1rem;}
.asof-title{font-size:12px;font-weight:600;color:#ccd;}
.asof-label{font-size:11px;color:#9ab;display:inline-flex;align-items:center;gap:6px;}
.asof-label input{background:#222;border:1px solid #3a3a3a;color:#eee;font-size:11px;padding:3px 6px;border-radius:6px;color-scheme:dark;}
.asof-tag{font-size:10px;color:#8ab4d8;font-weight:600;}
.asof-reset{background:#222;border:1px solid #3a3a3a;color:#888;font-size:10px;padding:3px 9px;border-radius:6px;cursor:pointer;}
.asof-reset:hover{color:#ccc;}
.asof-step{background:#222;border:1px solid #3a3a3a;color:#bcd;font-size:12px;line-height:1;padding:3px 9px;border-radius:6px;cursor:pointer;font-weight:600;}
.asof-step:hover:not(:disabled){color:#fff;border-color:#4a5a70;}
.asof-step:disabled{opacity:.35;cursor:default;}
.asof-help{flex-basis:100%;font-size:10px;color:#667;margin:2px 0 0;font-style:italic;}
.ptot-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px;}
.ptot-head .rng-tabs{margin-bottom:0;}
.ptot-total{font-size:12px;font-weight:600;color:#ccc;margin:0;}
.ptot-chart{position:relative;}
.ptot-tip{position:absolute;transform:translate(-50%,-115%);background:#111;border:1px solid #3a3a3a;border-radius:6px;padding:4px 8px;font-size:11px;color:#eee;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .08s;z-index:5;}
.ptot-tip-d{font-size:9px;color:#888;margin-bottom:1px;}
.legend{font-size:10px;color:#666;margin-top:6px;display:flex;gap:12px;flex-wrap:wrap;}
.legend span{display:inline-flex;align-items:center;gap:4px;}
.swatch{width:10px;height:10px;border-radius:2px;display:inline-block;}
.cmp-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
.cmp-axis{font-size:10px;color:#888;display:inline-flex;align-items:center;gap:5px;}
.cmp-sel{background:#222;border:1px solid #3a3a3a;color:#ddd;font-size:11px;padding:3px 6px;border-radius:6px;cursor:pointer;}
.cmp-sel:hover{border-color:#4a5a70;}
.zone-bar{display:flex;height:26px;border-radius:4px;overflow:hidden;margin:8px 0 4px;}
.zone-seg{display:flex;align-items:center;justify-content:center;font-size:9px;color:#000;font-weight:600;}
table{width:100%;border-collapse:collapse;}
th{font-size:10px;font-weight:600;color:#555;text-align:left;padding:4px 6px;border-bottom:1px solid #2a2a2a;}
th:not(:first-child){text-align:right;}
td{font-size:12px;padding:6px;border-bottom:1px solid #222;}
td:not(:first-child){text-align:right;font-variant-numeric:tabular-nums;}
tr:last-child td{border-bottom:none;}
"""


def _line_chart(series: list[dict], keys: list[tuple], w=720, h=160, pad=28, yfmt=None):
    """series: list of dicts with 'date' + value keys. keys: [(key,color,label)]."""
    if not series:
        return "<p class='note'>No data.</p>"
    n = len(series)
    pad_l = 40  # wider left gutter to hold the y-axis scale
    all_vals = [s[k] for s in series for k, _, _ in keys]
    vmin, vmax = min(all_vals), max(all_vals)
    if vmax == vmin:
        vmax = vmin + 1
    span = vmax - vmin
    yfmt = yfmt or (lambda v: f"{v:g}")

    def x(i):
        return pad_l + i / max(1, n - 1) * (w - pad_l - pad)

    def y(v):
        return h - pad - (v - vmin) / span * (h - 2 * pad)

    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">']
    # y-axis scale: 3 gridlines with value labels on the left
    for g in range(3):
        gv = vmin + g / 2 * span
        gy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w-pad}" y2="{gy:.1f}" stroke="#2a2a2a" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{gy+3:.1f}" font-size="8" fill="#666" text-anchor="end">{yfmt(gv)}</text>')
    # zero line if range crosses zero
    if vmin < 0 < vmax:
        zy = y(0)
        parts.append(f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{w-pad}" y2="{zy:.1f}" stroke="#444" stroke-width="1" stroke-dasharray="3 3"/>')
    for key, color, _ in keys:
        pts = " ".join(f"{x(i):.1f},{y(s[key]):.1f}" for i, s in enumerate(series))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
    parts.append("</svg>")
    legend = " ".join(
        f'<span><span class="swatch" style="background:{c}"></span>{lab}</span>'
        for _, c, lab in keys
    )
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def _bar_chart(labels, values, color="#5cb85c", w=720, h=140, pad=28, fmt=None, band=None, yfmt=None):
    if not values:
        return "<p class='note'>No data.</p>"
    vmax = max(values) or 1
    vmin = min(0, min(values))
    span = (vmax - vmin) or 1
    n = len(values)
    pad_l = 40  # wider left gutter to hold the y-axis scale
    bw = (w - pad_l - pad) / n * 0.7
    gap = (w - pad_l - pad) / n
    yfmt = yfmt or (lambda v: f"{v:g}")

    def y(v):
        return h - pad - (v - vmin) / span * (h - 2 * pad)

    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">']
    if band:  # shaded sweet-spot band (lo,hi) in value units
        lo, hi = band
        y_hi, y_lo = y(hi), y(lo)
        parts.append(f'<rect x="{pad_l}" y="{y_hi:.1f}" width="{w-pad_l-pad}" height="{abs(y_lo-y_hi):.1f}" fill="#5cb85c" opacity="0.10"/>')
    # y-axis scale: 3 gridlines with value labels on the left
    for g in range(3):
        gv = vmin + g / 2 * span
        gy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w-pad}" y2="{gy:.1f}" stroke="#2a2a2a" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{gy+3:.1f}" font-size="8" fill="#666" text-anchor="end">{yfmt(gv)}</text>')
    for i, v in enumerate(values):
        bx = pad_l + i * gap + (gap - bw) / 2
        by = y(v)
        bh = abs(y(0) - by)
        c = color
        if band:
            lo, hi = band
            c = "#5cb85c" if lo <= v <= hi else ("#e0a020" if v < lo else "#d9534f")
        parts.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="{c}"/>')
        if fmt:
            parts.append(f'<text x="{bx+bw/2:.1f}" y="{by-3:.1f}" font-size="8" fill="#888" text-anchor="middle">{fmt(v)}</text>')
    # x labels (sparse)
    step = max(1, n // 12)
    for i in range(0, n, step):
        lx = pad_l + i * gap + gap / 2
        parts.append(f'<text x="{lx:.1f}" y="{h-8:.1f}" font-size="8" fill="#555" text-anchor="middle">{labels[i]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _calendar_heatmap(cal: dict, w=720):
    if not cal:
        return "<p class='note'>No data.</p>"
    days = sorted(cal)
    start, end = days[0], days[-1]
    # align start to Monday
    start -= timedelta(days=start.weekday())
    weeks = (end - start).days // 7 + 1
    cell = min(13, (w - 30) / weeks)
    vmax = max(cal.values()) or 1
    h = 30 + 7 * (cell + 2)
    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">']
    dow = ["Mon", "", "Wed", "", "Fri", "", "Sun"]
    for r, lab in enumerate(dow):
        if lab:
            parts.append(f'<text x="2" y="{24+r*(cell+2)+cell*0.8:.1f}" font-size="8" fill="#555">{lab}</text>')
    cur = start
    wk = 0
    month_marks = {}
    while cur <= end:
        col = (cur - start).days // 7
        row = cur.weekday()
        load = cal.get(cur, 0)
        if cur.day <= 7:
            month_marks[col] = cur.strftime("%b")
        if load <= 0:
            fill = "#262626"
        else:
            t = min(1.0, load / vmax)
            # green ramp
            g = int(60 + t * 140)
            fill = f"rgb({int(40+t*40)},{g},{int(40+t*40)})"
        x = 26 + col * (cell + 2)
        y = 22 + row * (cell + 2)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="2" fill="{fill}"/>')
        cur += timedelta(days=1)
    for col, lab in month_marks.items():
        x = 26 + col * (cell + 2)
        parts.append(f'<text x="{x:.1f}" y="14" font-size="8" fill="#666">{lab}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Per-chart time-range controls (recency window)
# ---------------------------------------------------------------------------
# Each time-series chart is pre-rendered for several windows; the buttons just
# show/hide the matching SVG (setRange in JS). The underlying metrics are still
# computed over the full history — only the *view* is cropped.

DAILY_RANGES   = [("1M", 30), ("3M", 90), ("6M", 180), ("1Y", 365), ("All", None)]
MONTHLY_RANGES = [("3M", 3), ("6M", 6), ("1Y", 12), ("All", None)]
CAL_RANGES     = [("3M", 90), ("6M", 180), ("1Y", 365), ("All", None)]


def _last_days(series: list[dict], days, date_key="date") -> list[dict]:
    """Trailing slice of a date-keyed series to the last `days` (None = all)."""
    if days is None or not series:
        return series
    start = series[-1][date_key] - timedelta(days=days)
    return [s for s in series if s[date_key] >= start]


def _ranged(ranges: list[tuple], default_label: str, render_fn) -> str:
    """Wrap render_fn(window) outputs in a button-toggled range group."""
    btns, divs = [], []
    for label, win in ranges:
        active = label == default_label
        btns.append(
            f'<button class="rng-btn{" active" if active else ""}" '
            f'data-range="{label}" onclick="setRange(this)">{label}</button>')
        style = "" if active else ' style="display:none"'
        divs.append(f'<div class="rng-chart" data-range="{label}"{style}>{render_fn(win)}</div>')
    return (f'<div class="rng-group"><div class="rng-tabs">{"".join(btns)}</div>'
            f'{"".join(divs)}</div>')


def _best_5k_threshold_mps(rows: list[dict]) -> float | None:
    """All-time best 5K -> threshold m/s (single raw point). Fallback anchor only;
    prefer robust_threshold_mps. Mirrors the original generate_hub._compute_threshold."""
    best = None
    for r in rows:
        s = num(r, "best_5k_s")
        if s and s > 0 and (best is None or s < best):
            best = s
    return round(5000.0 / best, 4) if best else None


# Distances used for the current-fitness curve fit (metres, from CS_DISTANCES). These
# are exactly the six distances the card displays, so every shown pace is interpolated
# within the fit range rather than extrapolated (fitting the endurance set alone
# projected a 1 km faster than the athlete's actual best). The truly anaerobic 400 m /
# 804 m sprints and the redundant 2 mi point are excluded so they don't distort the fit.
_CURVE_FIT_DISTS = frozenset({1_000.0, 1_609.344, 5_000.0, 10_000.0, 15_000.0, 21_097.5})


def fit_current_curve(rows: list[dict]) -> tuple | None:
    """Personal power-law T = a * D^b over grade-adjusted best efforts at several
    endurance distances. Multi-point and grade-adjusted, so no single hot or downhill
    run dominates the estimate. Returns (a, b, n_points) or None if under 2 points."""
    pts = [(d, t) for d, t in best_effort_points(rows) if d in _CURVE_FIT_DISTS]
    if len(pts) < 2:
        return None
    a, b = fit_riegel(pts)
    return a, b, len(pts)


def robust_threshold_mps(rows: list[dict]) -> float | None:
    """Threshold speed (m/s) as the fitted curve's 5K-equivalent, not a single raw 5K.
    Falls back to the raw best 5K when too few endurance efforts exist to fit a curve."""
    fit = fit_current_curve(rows)
    if fit:
        a, b, _ = fit
        return round(5000.0 / (a * 5000.0 ** b), 4)
    return _best_5k_threshold_mps(rows)


# Run-type dot/line colours, mirrored verbatim from generate_hub.RUN_TYPE_COLOR so the
# progression chart matches the Runs-tab badges. misc is excluded from the chart entirely.
RUN_TYPE_COLOR = {
    "recovery": "#7aa7d6", "easy": "#5cb85c", "long": "#3fb6a8", "tempo": "#e0a020",
    "threshold": "#e8772e", "intervals": "#9b7ede", "race": "#d9534f",
}
# Selector order: hardest/most-specific classifications first.
_PACE_PROG_ORDER = ["tempo", "threshold", "race", "intervals", "long", "easy", "recovery"]
_PACE_PROG_MIN_RUNS = 3   # a type needs this many runs to earn a selector button
_PACE_PROG_ROLL = 5       # rolling-average window (runs), min 2 present to draw the trend


def _rolling_trend(paces: list[float]) -> list[float | None]:
    """Trailing mean of the last _PACE_PROG_ROLL paces at each index; None until >=2 exist."""
    out = []
    for i in range(len(paces)):
        window = paces[max(0, i - (_PACE_PROG_ROLL - 1)):i + 1]
        out.append(sum(window) / len(window) if len(window) >= 2 else None)
    return out


def _pace_prog_chart(dated_paces: list[tuple], color: str, w=720, h=170, pad=28) -> str:
    """Scatter of GA pace over time + rolling trend. x is proportional to real date;
    y is s/km inverted so faster (lower s/km) sits at the top (improvement climbs)."""
    if not dated_paces:
        return "<p class='note'>No data.</p>"
    pad_l = 44  # wider gutter for m:ss/km labels
    paces = [p for _, p in dated_paces]
    vmin, vmax = min(paces), max(paces)
    if vmax == vmin:
        vmax = vmin + 1
    span = vmax - vmin
    d0, d1 = dated_paces[0][0], dated_paces[-1][0]
    dspan = (d1 - d0).days or 1

    def x(d):
        return pad_l + (d - d0).days / dspan * (w - pad_l - pad)

    def y(v):  # inverted: fastest pace (vmin) at the top
        return pad + (v - vmin) / span * (h - 2 * pad)

    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">']
    for g in range(3):
        gv = vmin + g / 2 * span
        gy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w-pad}" y2="{gy:.1f}" stroke="#2a2a2a" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{gy+3:.1f}" font-size="8" fill="#666" text-anchor="end">{fmt_pace_from_s_per_km(gv)}</text>')
    # trend polyline first, so the dots read on top of it
    trend = [(dated_paces[i][0], t) for i, t in enumerate(_rolling_trend(paces)) if t is not None]
    if len(trend) >= 2:
        pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in trend)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
    for d, p in dated_paces:
        parts.append(f'<circle cx="{x(d):.1f}" cy="{y(p):.1f}" r="3" fill="{color}" opacity="0.7"/>')
    # sparse x-axis date labels (~6, muted, like _bar_chart). Evenly spaced along the
    # date axis rather than per data point, so clustered runs can't overprint each other.
    n_labels = min(6, max(2, len(dated_paces)))
    for k in range(n_labels):
        d = d0 + timedelta(days=round(k / (n_labels - 1) * dspan))
        parts.append(f'<text x="{x(d):.1f}" y="{h-8:.1f}" font-size="8" fill="#555" text-anchor="middle">{d.day}/{d.month}</text>')
    parts.append("</svg>")
    return "".join(parts)


def pace_progression_section(runs: list[dict] | None) -> str:
    """Per-run GA pace over time for a selectable run classification, with a rolling-average
    trend line. Returns "" when no runs are supplied (standalone build) or no type qualifies,
    so the section simply drops out like paces_card does when the curve can't be fit."""
    if not runs:
        return ""
    by_type: dict[str, list[tuple]] = defaultdict(list)
    for r in runs:
        rt = r.get("run_type")
        if not rt or rt == "misc":
            continue
        dist_km = r.get("dist_km") or 0
        moving_s = r.get("moving_s") or 0
        gain = r.get("gain") or 0
        if moving_s <= 0 or dist_km <= 0:
            continue
        try:
            d = datetime.strptime(r["date_iso"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError):
            continue
        pace = ga_time(moving_s, gain, dist_km) / dist_km  # s/km GA
        by_type[rt].append((d, pace))

    eligible = {t: sorted(v) for t, v in by_type.items() if len(v) >= _PACE_PROG_MIN_RUNS}
    if not eligible:
        return ""
    ordered = [t for t in _PACE_PROG_ORDER if t in eligible]
    default = "tempo" if "tempo" in eligible else max(eligible, key=lambda t: len(eligible[t]))

    def _render(type_key):
        data = eligible[type_key]
        color = RUN_TYPE_COLOR.get(type_key, "#888")
        trend = [t for t in _rolling_trend([p for _, p in data]) if t is not None]
        cur = f"{fmt_pace_from_s_per_km(trend[-1])}/km" if trend else "—"
        stats = (
            f'<div class="stat-row">'
            f'<div class="stat"><p class="stat-label">Current {type_key} pace</p>'
            f'<p class="stat-value" style="color:{color}">{cur}</p>'
            f'<p class="stat-sub">rolling {_PACE_PROG_ROLL}-run average (GA)</p></div>'
            f'<div class="stat"><p class="stat-label">Runs</p>'
            f'<p class="stat-value">{len(data)}</p>'
            f'<p class="stat-sub">classified {type_key}</p></div>'
            f'</div>')
        return stats + _pace_prog_chart(data, color)

    chart = _ranged([(t.capitalize(), t) for t in ordered], default.capitalize(), _render)
    return f"""
<div class="section">
  <p class="section-title">Pace progression</p>
  <div class="card">
    {chart}
    <p class="note">Dots are individual runs at grade-adjusted pace; the line is the rolling {_PACE_PROG_ROLL}-run average. Classifications are era-relative: each run was classified against the fitness curve at the time, so absolute paces for the same label drift as fitness changes.</p>
  </div>
</div>"""


def generate(rows: list[dict], updated: str, runs: list[dict] | None = None) -> str:
    loads = session_loads(rows)
    daily = daily_series(loads)
    fitness = ctl_atl_tsb(daily)
    acwr = acwr_series(daily)
    mono = monotony_strain(daily)

    # current values
    cur_ctl = fitness[-1]["ctl"] if fitness else 0
    cur_atl = fitness[-1]["atl"] if fitness else 0
    cur_tsb = fitness[-1]["tsb"] if fitness else 0
    cur_acwr = acwr[-1]["acwr"] if acwr else 0
    cur_mono = mono[-1]["monotony"] if mono else 0
    cur_strain = mono[-1]["strain"] if mono else 0

    # Personalized safe-strain ceiling: this athlete's own recent normal, mean + 1 SD of
    # trailing weekly strain, excluding the current week so a live spike doesn't raise its
    # own bar. Strain has no universal cutoff like monotony's 2.0 because it scales with load.
    safe_strain, strain_high = _strain_baseline(mono)
    strain_class = _strain_class(cur_strain, safe_strain, strain_high)
    strain_sub = _strain_sub(safe_strain)

    tsb_class = _tsb_class(cur_tsb)
    tsb_note = _tsb_note(cur_tsb)
    acwr_class = _acwr_class(cur_acwr)
    acwr_note = _acwr_note(cur_acwr)
    mono_class = _mono_class(cur_mono)

    # As-of date preview: rest-day offsets 0..MAX_AS_OF_DAYS for the injury cards.
    asof_anchor, asof_injury_blocks = as_of_blocks(rows)
    asof_max = ((daily[-1][0] + timedelta(days=MAX_AS_OF_DAYS)).isoformat()
                if daily else asof_anchor)
    asof_injury_json = json.dumps(asof_injury_blocks, ensure_ascii=False)
    _fit_variants, _acwr_variants, _strain_variants = _asof_chart_variants(daily)
    asof_fit_charts_json = json.dumps(_fit_variants, ensure_ascii=False)
    asof_acwr_charts_json = json.dumps(_acwr_variants, ensure_ascii=False)
    asof_strain_charts_json = json.dumps(_strain_variants, ensure_ascii=False)

    # critical speed
    pts = best_effort_points(rows)
    cs, dprime, r2 = fit_critical_speed(pts)

    # vdot
    vdot = vdot_trend(rows)
    cur_vdot = vdot[-1][1] if vdot else None

    # cadence
    cad = cadence_trend(rows)

    # ---- build HTML ----
    fitness_chart = _ranged(DAILY_RANGES, "3M", lambda win: _line_chart(
        _last_days(fitness, win),
        [("ctl", "#5cb85c", "Fitness (CTL)"),
         ("atl", "#e0a020", "Fatigue (ATL)"),
         ("tsb", "#5b8fd9", "Form (TSB)")],
        yfmt=lambda v: f"{round(v / 5) * 5:g}",
    ))

    def _acwr_render(win):
        s = _last_days(acwr, win)
        return _bar_chart([x["date"].strftime("%#d/%#m") for x in s],
                          [round(x["acwr"], 2) for x in s], band=(0.8, 1.3),
                          yfmt=lambda v: f"{v:.1f}")
    acwr_chart = _ranged(DAILY_RANGES, "3M", _acwr_render)

    def _strain_render(win):
        s = _last_days(mono, win)
        return _bar_chart([x["date"].strftime("%#d/%#m") for x in s],
                          [round(x["strain"]) for x in s], color="#5b8fd9",
                          yfmt=lambda v: f"{v:.0f}")
    strain_chart = _ranged(DAILY_RANGES, "3M", _strain_render)

    # critical speed table
    cs_rows = ""
    if cs:
        for d, t in pts:
            model_t = (d - dprime) / cs if cs else 0
            cs_rows += (f"<tr><td>{d/1000:.2f} km</td>"
                        f"<td>{fmt_time(t)}</td>"
                        f"<td>{fmt_pace_from_s_per_km(t/(d/1000))}/km</td>"
                        f"<td>{fmt_time(model_t) if model_t>0 else '-'}</td></tr>")

    def _monthly_render(data, color):
        def render(n):
            sub = data if n is None else data[-n:]
            if not sub:
                return "<p class='note'>No data.</p>"
            return _bar_chart([l for l, _ in sub], [v for _, v in sub],
                              color=color, fmt=lambda v: f"{v:.0f}")
        return render
    vdot_chart = _ranged(MONTHLY_RANGES, "6M", _monthly_render(vdot, "#5cb85c")) if vdot else ""
    cad_chart = _ranged(MONTHLY_RANGES, "6M", _monthly_render(cad, "#5b8fd9")) if cad else ""

    cs_pace_str = fmt_pace_from_s_per_km(1000 / cs) if cs else "-"

    # current paces, from the multi-point grade-adjusted fitness curve (fit_current_curve),
    # the same robust anchor the Runs tab now classifies tempo/threshold with.
    curve = fit_current_curve(rows)
    thr_mps = robust_threshold_mps(rows)
    pace_prog_section = pace_progression_section(runs)
    paces_card = ""   # empty => card is hidden when the curve can't be fit
    if curve and thr_mps:
        a, b, n_fit = curve
        p = 1000.0 / thr_mps                      # s/km at threshold speed (5K-equiv off the curve)
        # Z3/Z4 bands mirror generate_hub bounds [0.87, 0.93, 1.03] (speed fractions).
        # Slower pace = larger s/km, so divide by the slow-end fraction for the slow bound.
        thr_lo = fmt_pace_from_s_per_km(p / 1.03)  # threshold (Z4) fast bound
        thr_hi = fmt_pace_from_s_per_km(p / 0.93)  # ...slow bound
        tmp_lo = fmt_pace_from_s_per_km(p / 0.93)  # tempo (Z3) fast bound
        tmp_hi = fmt_pace_from_s_per_km(p / 0.87)  # ...slow bound
        # equivalent-effort paces read straight off the fitted curve T = a * D^b.
        dist_defs = [("1 km", 1000.0), ("1 mile", 1609.344), ("5 km", 5000.0),
                     ("10 km", 10000.0), ("15 km", 15000.0), ("Half", 21_097.5)]
        pace_rows = ""
        for label, d in dist_defs:
            t = a * d ** b
            pace_rows += (f"<tr><td>{label}</td><td>{fmt_time(t)}</td>"
                          f"<td>{fmt_pace_from_s_per_km(t / (d / 1000.0))}/km</td></tr>")
        paces_card = f"""
<div class="section">
  <p class="section-title">Current paces</p>
  <div class="card">
    <div class="stat-row">
      <div class="stat"><p class="stat-label">Threshold pace</p><p class="stat-value green">{thr_lo}-{thr_hi}/km</p><p class="stat-sub">Z4 (0.93-1.03 &times; threshold speed)</p></div>
      <div class="stat"><p class="stat-label">Tempo pace</p><p class="stat-value amber">{tmp_lo}-{tmp_hi}/km</p><p class="stat-sub">Z3 (0.87-0.93 &times; threshold speed)</p></div>
    </div>
    <table>
      <thead><tr><th>Distance</th><th>Equivalent time</th><th>Pace</th></tr></thead>
      <tbody>{pace_rows}</tbody>
    </table>
    <p class="note">From a personal power-law curve (T = a&middot;D<sup>b</sup>, b={b:.2f}) fitted over your grade-adjusted best efforts at {n_fit} distances from 1 km to the half, so no single downhill or one-off run skews it. This is the same robust anchor the Runs tab uses to classify tempo and threshold efforts. Tempo and threshold are the classifier's Z3/Z4 pace bands; the distance rows are equivalent honest-effort paces given current fitness, not training targets.</p>
  </div>
</div>"""

    return f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
{SHARED_CSS}
</style>
</head>
<body>
<h1>Training analytics</h1>
<p class="subtitle">Derived from pace, distance and elevation only (this device records no HR or power) · grade-adjusted · updated {updated}</p>

<div class="sub-tabs">
  <button class="sub-tab active" data-sub="injury" onclick="setSubTab(this)">Injury risk</button>
  <button class="sub-tab" data-sub="paces" onclick="setSubTab(this)">Paces</button>
  <button class="sub-tab" data-sub="trends" onclick="setSubTab(this)">Trends</button>
  <button class="sub-tab" data-sub="compare" onclick="setSubTab(this);cmpDraw()">Compare</button>
</div>

<div class="sub-panel" data-sub="injury">
<div class="asof-bar">
  <span class="asof-title">Preview after rest days</span>
  <label class="asof-label">as of <input type="date" id="asof-injury" min="{asof_anchor}" max="{asof_max}" value="{asof_anchor}"></label>
  <button type="button" class="asof-step" id="asof-injury-dec" title="one fewer rest day">&minus;1</button>
  <button type="button" class="asof-step" id="asof-injury-inc" title="one more rest day">+1</button>
  <span class="asof-tag" id="asof-injury-tag">latest data</span>
  <button type="button" class="asof-reset" id="asof-injury-reset" hidden>reset</button>
  <p class="asof-help">Injury-risk figures and their charts below decay over rest days up to the next weekly Strava packet.</p>
</div>
<div class="section">
  <p class="section-title">Fitness, fatigue and form</p>
  <div class="card">
    <div class="stat-row">
      <div class="stat"><p class="stat-label">Fitness (CTL)</p><p class="stat-value green" id="inj-ctl">{cur_ctl:.0f}</p><p class="stat-sub">42-day load</p></div>
      <div class="stat"><p class="stat-label">Fatigue (ATL)</p><p class="stat-value amber" id="inj-atl">{cur_atl:.0f}</p><p class="stat-sub">7-day load</p></div>
      <div class="stat"><p class="stat-label">Form (TSB)</p><p class="stat-value {tsb_class}" id="inj-tsb">{cur_tsb:+.0f}</p><p class="stat-sub" id="inj-tsb-sub">{tsb_note}</p></div>
    </div>
    <div id="chart-fitness">{fitness_chart}</div>
  </div>
  <p class="note">Load is a pace-based stress score (grade-adjusted distance weighted by intensity vs threshold pace), the heart-rate-free equivalent of Strava's Fitness &amp; Freshness. Form = prior fitness minus prior fatigue: positive means rested, negative means carrying fatigue.</p>
</div>

<div class="section">
  <p class="section-title">Acute:chronic workload ratio</p>
  <div class="card">
    <div class="stat-row">
      <div class="stat"><p class="stat-label">Current ACWR</p><p class="stat-value {acwr_class}" id="inj-acwr">{cur_acwr:.2f}</p><p class="stat-sub" id="inj-acwr-sub">{acwr_note}</p></div>
    </div>
    <div id="chart-acwr">{acwr_chart}</div>
    <p class="note">Shaded band is the 0.8-1.3 sweet spot. Green bars are in range, amber below (detraining), red above (injury-risk spike).</p>
  </div>
</div>

<div class="section">
  <p class="section-title">Training strain &amp; monotony (Foster)</p>
  <div class="card">
    <div class="stat-row">
      <div class="stat"><p class="stat-label">Monotony</p><p class="stat-value {mono_class}" id="inj-mono">{cur_mono:.2f}</p><p class="stat-sub">lower is better; &gt;2 is risky</p></div>
      <div class="stat"><p class="stat-label">Strain (latest week)</p><p class="stat-value {strain_class}" id="inj-strain">{cur_strain:.0f}</p><p class="stat-sub">{strain_sub}</p></div>
    </div>
    <div id="chart-strain">{strain_chart}</div>
    <p class="note">The chart plots <strong>strain</strong> (weekly load × monotony), not monotony — so its scale runs into the hundreds. High strain with high monotony (same load every day, no easy/hard variation) is the classic overtraining signature.</p>
  </div>
</div>
</div>

<div class="sub-panel" data-sub="paces" style="display:none">
{pace_prog_section}
{paces_card}
<div class="section">
  <p class="section-title">Critical speed model</p>
  <div class="card">
    <div class="stat-row">
      <div class="stat"><p class="stat-label">Critical speed</p><p class="stat-value green">{cs_pace_str}/km</p><p class="stat-sub">{cs:.2f} m/s</p></div>
      <div class="stat"><p class="stat-label">D' (anaerobic)</p><p class="stat-value">{dprime:.0f} m</p><p class="stat-sub">finite work above CS</p></div>
      <div class="stat"><p class="stat-label">Model fit r²</p><p class="stat-value">{r2:.3f}</p><p class="stat-sub">{len(pts)} efforts</p></div>
    </div>
    <table>
      <thead><tr><th>Distance</th><th>Best (GA)</th><th>Pace</th><th>Model</th></tr></thead>
      <tbody>{cs_rows}</tbody>
    </table>
    <p class="note">The running analog of Strava's power curve: distance = CS × time + D'. Critical speed is your sustainable threshold; D' is the fixed distance you can cover above it before fatigue. Model column shows the predicted time at each distance.</p>
  </div>
</div>
</div>

<div class="sub-panel" data-sub="trends" style="display:none">
<div class="section">
  <p class="section-title">VO₂max estimate (VDOT) by month</p>
  <div class="card">
    {vdot_chart}
    <p class="note">Daniels VDOT from your best grade-adjusted efforts each month. Current estimate: <strong>{cur_vdot if cur_vdot else "n/a"}</strong>. An aerobic-fitness proxy, useful as a trend rather than an absolute figure.</p>
  </div>
</div>

<div class="section">
  <p class="section-title">Average cadence by month</p>
  <div class="card">
    {cad_chart}
    <p class="note">Steps per minute (both feet). Rising cadence at the same pace usually signals improving running economy.</p>
  </div>
</div>
</div>

<div class="sub-panel" data-sub="compare" style="display:none">
<div class="section">
  <p class="section-title">Compare stats</p>
  <div class="card">
    <div class="cmp-controls">
      <div class="rng-tabs" id="cmp-mode">
        <button class="rng-btn active" data-mode="scatter" onclick="cmpSet('mode','scatter')">Scatter</button>
        <button class="rng-btn" data-mode="time" onclick="cmpSet('mode','time')">Over time</button>
        <button class="rng-btn" data-mode="dist" onclick="cmpSet('mode','dist')">Distribution</button>
      </div>
      <label class="cmp-axis" id="cmp-x-wrap">x <select class="cmp-sel" id="cmp-x" onchange="cmpSet('x',this.value)"></select></label>
      <label class="cmp-axis" id="cmp-y-wrap">y <select class="cmp-sel" id="cmp-y" onchange="cmpSet('y',this.value)"></select></label>
    </div>
    <div id="cmp-chart" class="ptot-chart"></div>
    <div class="legend" id="cmp-legend"></div>
    <p class="note">Pick any two metrics to plot against each other, watch one metric over time, or see its distribution. Points are coloured by run type. Runs missing the selected metric are skipped (heart rate is empty until an HR monitor is used). Pace is min/km, so lower sits toward the bottom.</p>
  </div>
</div>
</div>

<script>
function setSubTab(btn){{
  var bar = btn.closest('.sub-tabs');
  if(!bar) return;
  var sub = btn.getAttribute('data-sub');
  var btns = bar.querySelectorAll('.sub-tab');
  for(var i=0;i<btns.length;i++){{ btns[i].classList.toggle('active', btns[i]===btn); }}
  var panels = bar.parentNode.querySelectorAll('.sub-panel');
  for(var j=0;j<panels.length;j++){{
    panels[j].style.display = (panels[j].getAttribute('data-sub')===sub) ? '' : 'none';
  }}
}}
function setRange(btn){{
  var g = btn.closest('.rng-group');
  if(!g) return;
  var range = btn.getAttribute('data-range');
  var btns = g.querySelectorAll('.rng-btn');
  for(var i=0;i<btns.length;i++){{ btns[i].classList.toggle('active', btns[i]===btn); }}
  var charts = g.querySelectorAll('.rng-chart');
  for(var j=0;j<charts.length;j++){{
    charts[j].style.display = (charts[j].getAttribute('data-range')===range) ? '' : 'none';
  }}
}}

// ── As-of date preview (injury tab) ──────────────────────────────────────────
// The date picker moves the reference date forward past rest days. Each offset's
// figures AND charts are precomputed in Python (as_of_blocks / _asof_chart_variants);
// JS just swaps the block and the chart HTML in (preserving the active range).
var AS_OF_INJURY = {asof_injury_json};
var AS_OF_ANCHOR = "{asof_anchor}";
var CHART_FITNESS = {asof_fit_charts_json};
var CHART_ACWR = {asof_acwr_charts_json};
var CHART_STRAIN = {asof_strain_charts_json};
(function(){{
  var input = document.getElementById('asof-injury');
  if(!input || !AS_OF_INJURY.length) return;
  var tag = document.getElementById('asof-injury-tag');
  var reset = document.getElementById('asof-injury-reset');
  var dec = document.getElementById('asof-injury-dec');
  var inc = document.getElementById('asof-injury-inc');
  var anchor = new Date(AS_OF_ANCHOR + 'T00:00:00');
  function iso(dt){{ return dt.getFullYear()+'-'+('0'+(dt.getMonth()+1)).slice(-2)+'-'+('0'+dt.getDate()).slice(-2); }}
  function currentK(){{ var k=Math.round((new Date(input.value+'T00:00:00')-anchor)/86400000); return isNaN(k)?0:k; }}
  function stepBy(delta){{
    var k=currentK()+delta;
    if(k<0) k=0;
    if(k>AS_OF_INJURY.length-1) k=AS_OF_INJURY.length-1;
    input.value=iso(new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate()+k));
    apply();
  }}
  function setText(id, txt){{ var el=document.getElementById(id); if(el) el.textContent=txt; }}
  function setStat(id, txt, cls){{ var el=document.getElementById(id); if(el){{ el.textContent=txt; el.className='stat-value '+cls; }} }}
  function swapChart(id, html){{
    var c = document.getElementById(id);
    if(!c) return;
    var active = c.querySelector('.rng-btn.active');
    var range = active ? active.getAttribute('data-range') : null;
    c.innerHTML = html;
    if(range){{
      var btns = c.querySelectorAll('.rng-btn');
      for(var i=0;i<btns.length;i++){{ btns[i].classList.toggle('active', btns[i].getAttribute('data-range')===range); }}
      var charts = c.querySelectorAll('.rng-chart');
      for(var j=0;j<charts.length;j++){{ charts[j].style.display = (charts[j].getAttribute('data-range')===range) ? '' : 'none'; }}
    }}
  }}
  function apply(){{
    var d = new Date(input.value + 'T00:00:00');
    var k = Math.round((d - anchor)/86400000);
    if(isNaN(k) || k<0) k=0;
    if(k>AS_OF_INJURY.length-1) k=AS_OF_INJURY.length-1;
    var b = AS_OF_INJURY[k];
    setText('inj-ctl', b.ctl);
    setText('inj-atl', b.atl);
    setStat('inj-tsb', b.tsb, b.tsbClass);
    setText('inj-tsb-sub', b.tsbNote);
    setStat('inj-acwr', b.acwr, b.acwrClass);
    setText('inj-acwr-sub', b.acwrNote);
    setStat('inj-mono', b.mono, b.monoClass);
    setStat('inj-strain', b.strain, b.strainClass);
    if(typeof CHART_FITNESS!=='undefined'){{ swapChart('chart-fitness', CHART_FITNESS[k]); swapChart('chart-acwr', CHART_ACWR[k]); swapChart('chart-strain', CHART_STRAIN[k]); }}
    tag.textContent = k===0 ? 'latest data' : (k===1 ? '1 rest day ahead' : k+' rest days ahead');
    reset.hidden = (k===0);
    if(dec) dec.disabled = (k<=0);
    if(inc) inc.disabled = (k>=AS_OF_INJURY.length-1);
  }}
  input.addEventListener('change', apply);
  input.addEventListener('input', apply);
  reset.addEventListener('click', function(){{ input.value = AS_OF_ANCHOR; apply(); }});
  if(dec) dec.addEventListener('click', function(){{ stepBy(-1); }});
  if(inc) inc.addEventListener('click', function(){{ stepBy(1); }});
  apply();
}})();
</script>

<script>
// ── Compare subtab (Football-Manager-style stat toggle) ──────────────────────
// Client-side interactive chart over the global RUNS array (defined later in the
// hub document, so init is deferred to DOMContentLoaded). Absent when this module
// is rendered standalone; cmpDraw() no-ops if RUNS is undefined.
var CMP_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
var CMP_STATE = {{mode:'scatter', x:'dist_km', y:'pace'}};
var CMP_METRICS = [
  {{key:'dist_km', label:'Distance', unit:'km', fmt:function(v){{ return (Math.round(v*100)/100)+' km'; }}, get:function(r){{ return r.dist_km>0?r.dist_km:null; }}}},
  {{key:'pace', label:'Pace', unit:'min/km', fmt:function(v){{ var s=v*60, m=Math.floor(s/60), ss=Math.round(s-m*60); if(ss===60){{m++;ss=0;}} return m+':'+(ss<10?'0':'')+ss+'/km'; }}, get:function(r){{ return (r.dist_km>0&&r.moving_s>0)?(r.moving_s/r.dist_km/60):null; }}}},
  {{key:'elev', label:'Elev gain', unit:'m', fmt:function(v){{ return Math.round(v)+' m'; }}, get:function(r){{ return (r.gain!=null)?r.gain:null; }}}},
  {{key:'elev_per_km', label:'Elev / km', unit:'m/km', fmt:function(v){{ return (Math.round(v*10)/10)+' m/km'; }}, get:function(r){{ return (r.dist_km>0&&r.gain!=null)?(r.gain/r.dist_km):null; }}}},
  {{key:'time', label:'Moving time', unit:'min', fmt:function(v){{ var m=Math.round(v), h=Math.floor(m/60), mm=m-h*60; return h>0?(h+'h '+(mm<10?'0':'')+mm+'m'):(mm+'m'); }}, get:function(r){{ return r.moving_s>0?(r.moving_s/60):null; }}}},
  {{key:'cadence', label:'Cadence', unit:'spm', fmt:function(v){{ return Math.round(v)+' spm'; }}, get:function(r){{ return r.cadence!=null?r.cadence:null; }}}},
  {{key:'calories', label:'Calories', unit:'kcal', fmt:function(v){{ return Math.round(v)+' kcal'; }}, get:function(r){{ return r.calories!=null?r.calories:null; }}}},
  {{key:'avg_hr', label:'Heart rate', unit:'bpm', fmt:function(v){{ return Math.round(v)+' bpm'; }}, get:function(r){{ return r.avg_hr!=null?r.avg_hr:null; }}}}
];
function cmpMetric(k){{ for(var i=0;i<CMP_METRICS.length;i++){{ if(CMP_METRICS[i].key===k) return CMP_METRICS[i]; }} return CMP_METRICS[0]; }}
function cmpEsc(s){{ return String(s==null?'':s).replace(/[&<>"]/g, function(c){{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
function cmpTick(m,v){{
  if(m.key==='pace'){{ var s=v*60, mm=Math.floor(s/60), ss=Math.round(s-mm*60); if(ss===60){{mm++;ss=0;}} return mm+':'+(ss<10?'0':'')+ss; }}
  if(Math.abs(v)>=100) return ''+Math.round(v);
  if(Math.abs(v)>=10) return ''+(Math.round(v*10)/10);
  return ''+(Math.round(v*100)/100);
}}
function cmpDateLabel(ms){{ var d=new Date(ms); return CMP_MON[d.getMonth()]+" '"+String(d.getFullYear()).slice(2); }}

function cmpInit(){{
  var xs=document.getElementById('cmp-x'), ys=document.getElementById('cmp-y');
  if(!xs||!ys) return;
  var opts=CMP_METRICS.map(function(m){{ return '<option value="'+m.key+'">'+m.label+'</option>'; }}).join('');
  xs.innerHTML=opts; ys.innerHTML=opts; xs.value=CMP_STATE.x; ys.value=CMP_STATE.y;
  cmpSyncControls(); cmpDraw();
}}
function cmpSyncControls(){{
  var mode=CMP_STATE.mode;
  var xw=document.getElementById('cmp-x-wrap'), yw=document.getElementById('cmp-y-wrap');
  if(xw) xw.style.display=(mode==='scatter')?'':'none';
  if(yw){{ yw.style.display=''; yw.firstChild.nodeValue=(mode==='scatter')?'y ':'metric '; }}
}}
function cmpSet(kind,val){{
  CMP_STATE[kind]=val;
  if(kind==='mode'){{
    var btns=document.querySelectorAll('#cmp-mode .rng-btn');
    for(var i=0;i<btns.length;i++){{ btns[i].classList.toggle('active', btns[i].getAttribute('data-mode')===val); }}
    cmpSyncControls();
  }}
  cmpDraw();
}}
function cmpLegend(pts){{
  var el=document.getElementById('cmp-legend'); if(!el) return;
  if(typeof RUN_TYPES==='undefined'){{ el.innerHTML=''; return; }}
  var present={{}};
  for(var i=0;i<pts.length;i++){{ present[pts[i].type]=1; }}
  var items=[];
  for(var i=0;i<RUN_TYPES.length;i++){{ var t=RUN_TYPES[i]; if(present[t.key]){{ items.push('<span><span class="swatch" style="background:'+(RUN_TYPE_COLOR[t.key]||'#888')+'"></span>'+t.label+'</span>'); }} }}
  el.innerHTML=items.join('');
}}
function cmpDraw(){{
  var holder=document.getElementById('cmp-chart');
  if(!holder||typeof RUNS==='undefined') return;
  if(CMP_STATE.mode==='dist') cmpDrawDist(holder); else cmpDrawXY(holder);
}}
function cmpDrawXY(holder){{
  var mode=CMP_STATE.mode, my=cmpMetric(CMP_STATE.y), mx=(mode==='time')?null:cmpMetric(CMP_STATE.x);
  var pts=[];
  for(var i=0;i<RUNS.length;i++){{
    var r=RUNS[i], yv=my.get(r); if(yv==null) continue;
    var xv;
    if(mode==='time'){{ xv=Date.parse(r.date_iso); if(isNaN(xv)) continue; }}
    else {{ xv=mx.get(r); if(xv==null) continue; }}
    pts.push({{x:xv, y:yv, type:r.run_type||'misc', run:r}});
  }}
  if(pts.length<2){{ holder.innerHTML='<p class="note">Not enough data for this metric.</p>'; cmpLegend([]); return; }}
  var W=720,H=240,padL=52,padR=14,padT=16,padB=30;
  var xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;
  for(var i=0;i<pts.length;i++){{ var p=pts[i]; if(p.x<xmin)xmin=p.x; if(p.x>xmax)xmax=p.x; if(p.y<ymin)ymin=p.y; if(p.y>ymax)ymax=p.y; }}
  var xr=(xmax-xmin)||1, yr=(ymax-ymin)||1;
  ymin-=yr*0.06; ymax+=yr*0.06; yr=(ymax-ymin)||1;
  if(mode!=='time'){{ xmin-=xr*0.04; xmax+=xr*0.04; xr=(xmax-xmin)||1; }}
  function px(v){{ return padL+(v-xmin)/xr*(W-padL-padR); }}
  function py(v){{ return H-padB-(v-ymin)/yr*(H-padT-padB); }}
  var svg=['<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">'];
  for(var g=0;g<=3;g++){{
    var gv=ymin+g/3*yr, gy=py(gv);
    svg.push('<line x1="'+padL+'" y1="'+gy.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+gy.toFixed(1)+'" stroke="#2a2a2a" stroke-width="1"/>');
    svg.push('<text x="'+(padL-6)+'" y="'+(gy+3).toFixed(1)+'" font-size="8" fill="#666" text-anchor="end">'+cmpTick(my,gv)+'</text>');
  }}
  for(var g=0;g<=4;g++){{
    var gv=xmin+g/4*xr, gx=px(gv);
    var lab=(mode==='time')?cmpDateLabel(gv):cmpTick(mx,gv);
    svg.push('<text x="'+gx.toFixed(1)+'" y="'+(H-10)+'" font-size="8" fill="#555" text-anchor="middle">'+lab+'</text>');
  }}
  svg.push('<text x="'+padL+'" y="10" font-size="8" fill="#888">'+my.label+' ('+my.unit+')</text>');
  svg.push('<text x="'+((padL+W-padR)/2).toFixed(1)+'" y="'+(H-1)+'" font-size="8" fill="#777" text-anchor="middle">'+((mode==='time')?'Date':(mx.label+' ('+mx.unit+')'))+'</text>');
  for(var i=0;i<pts.length;i++){{
    var p=pts[i], c=(RUN_TYPE_COLOR[p.type]||'#888');
    svg.push('<circle cx="'+px(p.x).toFixed(1)+'" cy="'+py(p.y).toFixed(1)+'" r="3.5" fill="'+c+'" fill-opacity="0.82" stroke="#111" stroke-width="0.5"/>');
  }}
  svg.push('</svg>');
  holder.innerHTML=svg.join('')+'<div class="ptot-tip" id="cmp-tip"></div>';
  holder._cmp={{pts:pts, W:W, H:H, padL:padL, padR:padR, padT:padT, padB:padB, xmin:xmin, xr:xr, ymin:ymin, yr:yr, mx:mx, my:my, mode:mode}};
  holder.onmousemove=function(e){{ cmpHoverXY(e,holder); }};
  holder.onmouseleave=function(){{ var t=document.getElementById('cmp-tip'); if(t) t.style.opacity=0; }};
  cmpLegend(pts);
}}
function cmpHoverXY(e,holder){{
  var st=holder._cmp; if(!st) return;
  var svg=holder.querySelector('svg'); if(!svg) return;
  var sr=svg.getBoundingClientRect();
  var mx=(e.clientX-sr.left)/sr.width*st.W, my=(e.clientY-sr.top)/sr.height*st.H;
  function px(v){{ return st.padL+(v-st.xmin)/st.xr*(st.W-st.padL-st.padR); }}
  function py(v){{ return st.H-st.padB-(v-st.ymin)/st.yr*(st.H-st.padT-st.padB); }}
  var best=-1, bd=Infinity;
  for(var i=0;i<st.pts.length;i++){{ var dx=px(st.pts[i].x)-mx, dy=py(st.pts[i].y)-my, d=dx*dx+dy*dy; if(d<bd){{bd=d;best=i;}} }}
  var tip=document.getElementById('cmp-tip'); if(!tip||best<0) return;
  if(bd>1000){{ tip.style.opacity=0; return; }}
  var p=st.pts[best];
  var head='<div class="ptot-tip-d">'+cmpEsc(p.run.name)+' · '+p.run.date_long+'</div>';
  var body=(st.mode==='time')
    ? '<div><b>'+st.my.label+': '+st.my.fmt(p.y)+'</b></div>'
    : '<div>'+st.mx.label+': '+st.mx.fmt(p.x)+'</div><div><b>'+st.my.label+': '+st.my.fmt(p.y)+'</b></div>';
  tip.innerHTML=head+body;
  var hr=holder.getBoundingClientRect(), scaleX=sr.width/st.W, scaleY=sr.height/st.H;
  tip.style.left=((sr.left-hr.left)+px(p.x)*scaleX)+'px';
  tip.style.top=((sr.top-hr.top)+py(p.y)*scaleY)+'px';
  tip.style.opacity=1;
}}
function cmpDrawDist(holder){{
  var m=cmpMetric(CMP_STATE.y), vals=[];
  for(var i=0;i<RUNS.length;i++){{ var v=m.get(RUNS[i]); if(v!=null) vals.push({{v:v, type:RUNS[i].run_type||'misc'}}); }}
  if(vals.length<2){{ holder.innerHTML='<p class="note">Not enough data for this metric.</p>'; cmpLegend([]); return; }}
  var vmin=Infinity, vmax=-Infinity;
  for(var i=0;i<vals.length;i++){{ if(vals[i].v<vmin)vmin=vals[i].v; if(vals[i].v>vmax)vmax=vals[i].v; }}
  if(vmax<=vmin) vmax=vmin+1;
  var nb=Math.min(12, Math.max(5, Math.round(Math.sqrt(vals.length)))), bw=(vmax-vmin)/nb;
  var bins=[]; for(var b=0;b<nb;b++) bins.push({{total:0, types:{{}}}});
  for(var i=0;i<vals.length;i++){{ var bi=Math.floor((vals[i].v-vmin)/bw); if(bi>=nb)bi=nb-1; if(bi<0)bi=0; bins[bi].total++; bins[bi].types[vals[i].type]=(bins[bi].types[vals[i].type]||0)+1; }}
  var cmax=0; for(var b=0;b<nb;b++){{ if(bins[b].total>cmax)cmax=bins[b].total; }} if(cmax<=0)cmax=1;
  var W=720,H=240,padL=32,padR=14,padT=16,padB=30, gw=(W-padL-padR)/nb;
  function py(c){{ return H-padB-(c/cmax)*(H-padT-padB); }}
  var svg=['<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">'];
  for(var g=0;g<=3;g++){{ var gc=cmax*g/3, gy=py(gc); svg.push('<line x1="'+padL+'" y1="'+gy.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+gy.toFixed(1)+'" stroke="#2a2a2a" stroke-width="1"/><text x="'+(padL-6)+'" y="'+(gy+3).toFixed(1)+'" font-size="8" fill="#666" text-anchor="end">'+Math.round(gc)+'</text>'); }}
  var step=Math.max(1, Math.round(nb/6));
  for(var b=0;b<nb;b++){{
    var bx=padL+b*gw+gw*0.12, w=gw*0.76, yb=H-padB;
    for(var ti=0;ti<RUN_TYPES.length;ti++){{
      var tk=RUN_TYPES[ti].key, cnt=bins[b].types[tk]||0; if(!cnt) continue;
      var sh=(cnt/cmax)*(H-padT-padB), sy=yb-sh;
      svg.push('<rect x="'+bx.toFixed(1)+'" y="'+sy.toFixed(1)+'" width="'+w.toFixed(1)+'" height="'+sh.toFixed(1)+'" fill="'+(RUN_TYPE_COLOR[tk]||'#888')+'" fill-opacity="0.85"/>');
      yb=sy;
    }}
    if(b%step===0) svg.push('<text x="'+(padL+b*gw+gw/2).toFixed(1)+'" y="'+(H-10)+'" font-size="8" fill="#555" text-anchor="middle">'+cmpTick(m, vmin+b*bw)+'</text>');
  }}
  svg.push('<text x="'+padL+'" y="10" font-size="8" fill="#888">runs</text>');
  svg.push('<text x="'+((padL+W-padR)/2).toFixed(1)+'" y="'+(H-1)+'" font-size="8" fill="#777" text-anchor="middle">'+m.label+' ('+m.unit+')</text>');
  svg.push('</svg>');
  holder.innerHTML=svg.join('')+'<div class="ptot-tip" id="cmp-tip"></div>';
  holder._cmpd={{bins:bins, nb:nb, vmin:vmin, bw:bw, W:W, padL:padL, padR:padR, gw:gw, m:m}};
  holder.onmousemove=function(e){{ cmpHoverDist(e,holder); }};
  holder.onmouseleave=function(){{ var t=document.getElementById('cmp-tip'); if(t) t.style.opacity=0; }};
  cmpLegend(vals);
}}
function cmpHoverDist(e,holder){{
  var st=holder._cmpd; if(!st) return;
  var svg=holder.querySelector('svg'); if(!svg) return;
  var sr=svg.getBoundingClientRect();
  var mx=(e.clientX-sr.left)/sr.width*st.W;
  var b=Math.floor((mx-st.padL)/st.gw);
  var tip=document.getElementById('cmp-tip'); if(!tip) return;
  if(b<0||b>=st.nb){{ tip.style.opacity=0; return; }}
  var lo=st.vmin+b*st.bw, hi=lo+st.bw, cnt=st.bins[b].total;
  tip.innerHTML='<div class="ptot-tip-d">'+st.m.fmt(lo)+' – '+st.m.fmt(hi)+'</div><div><b>'+cnt+' run'+(cnt===1?'':'s')+'</b></div>';
  var hr=holder.getBoundingClientRect(), scaleX=sr.width/st.W, cx=st.padL+(b+0.5)*st.gw;
  tip.style.left=((sr.left-hr.left)+cx*scaleX)+'px';
  tip.style.top=((sr.top-hr.top)+22)+'px';
  tip.style.opacity=1;
}}
if(document.readyState==='loading'){{ document.addEventListener('DOMContentLoaded', cmpInit); }} else {{ cmpInit(); }}
</script>
</body>
</html>
"""


def overview_sections(rows: list[dict], updated: str, heatmap_html: str = "") -> str:
    """HTML + JS for the three sections relocated to the hub's Overview tab:
    distance/elevation/time totals, time in pace zones, and the training-log heatmap.

    heatmap_html, if given, is the hub's all-time heatmap section; it is placed
    between the distance/elevation/time chart and the pace-zone section.

    The training-log range buttons call setRange(), which is defined in the
    analytics panel script; the hub always emits both panels, so it is in scope.
    The distance/elevation/time chart is fully self-contained (own ptot* JS).
    """
    loads = session_loads(rows)
    cal = calendar_data(loads)

    def _cal_render(win):
        if win is None or not cal:
            c = cal
        else:
            start = max(cal) - timedelta(days=win)
            c = {d: v for d, v in cal.items() if d >= start}
        return _calendar_heatmap(c)
    cal_chart = _ranged(CAL_RANGES, "6M", _cal_render)

    # pace-zone distribution, anchored to critical speed
    pts = best_effort_points(rows)
    cs, _dprime, _r2 = fit_critical_speed(pts)
    zones = pace_zones_from_cs(cs) if cs else []
    zsecs = time_in_pace_zones(rows, zones) if zones else []
    ztotal = sum(zsecs) or 1
    zone_colors = ["#3a7a3a", "#5cb85c", "#e0a020", "#e07020", "#d9534f"]
    zone_segs = zone_legend = ""
    if zones:
        for i, z in enumerate(zones):
            pct = zsecs[i] / ztotal * 100
            zone_segs += (f'<div class="zone-seg" style="width:{pct:.1f}%;background:{zone_colors[i]}">'
                          f'{pct:.0f}%</div>')
            zone_legend += (f'<span><span class="swatch" style="background:{zone_colors[i]}"></span>'
                            f'{z["name"]} ({fmt_pace_from_s_per_km(z["hi_s"])}-{fmt_pace_from_s_per_km(z["lo_s"])}/km)</span>')

    # per-period totals (distance / elevation / time), weekly + monthly, for the
    # interactive Strava-style toggle chart rendered in JS
    totals_json = json.dumps({"week": period_totals(rows, "week"),
                              "month": period_totals(rows, "month")})

    return f"""\
<div class="ov-section">
  <p class="ov-section-title">Distance, elevation &amp; time</p>
  <div class="card">
    <div class="ptot-head">
      <div class="rng-tabs" id="ptot-metric">
        <button class="rng-btn active" data-metric="dist" onclick="ptotSet('metric','dist',this)">Distance</button>
        <button class="rng-btn" data-metric="elev" onclick="ptotSet('metric','elev',this)">Elev gain</button>
        <button class="rng-btn" data-metric="time" onclick="ptotSet('metric','time',this)">Time</button>
      </div>
      <p class="ptot-total" id="ptot-total"></p>
    </div>
    <div id="ptot-chart" class="ptot-chart"></div>
    <div class="ptot-head">
      <div class="rng-tabs" id="ptot-win">
        <button class="rng-btn" data-win="90" onclick="ptotSet('win',90,this)">3M</button>
        <button class="rng-btn active" data-win="180" onclick="ptotSet('win',180,this)">6M</button>
        <button class="rng-btn" data-win="365" onclick="ptotSet('win',365,this)">1Y</button>
        <button class="rng-btn" data-win="0" onclick="ptotSet('win',0,this)">All</button>
      </div>
      <div class="rng-tabs" id="ptot-gran">
        <button class="rng-btn active" data-gran="week" onclick="ptotSet('gran','week',this)">Weekly</button>
        <button class="rng-btn" data-gran="month" onclick="ptotSet('gran','month',this)">Monthly</button>
      </div>
      <div class="rng-tabs" id="ptot-overlay">
        <button class="rng-btn" onclick="ptotToggleOverlay(this)">+ Runs</button>
      </div>
    </div>
    <p class="note">Distance, elevation gain or moving time per week (or month). Hover a point to read its exact value. Window and granularity are independent toggles. Toggle <strong>+ Runs</strong> to overlay number of runs per period as bars.</p>
  </div>
</div>
{heatmap_html}
<div class="ov-section">
  <p class="ov-section-title">Time in pace zones</p>
  <div class="card">
    <div class="zone-bar">{zone_segs}</div>
    <div class="legend">{zone_legend}</div>
    <p class="note">Approximate distribution of moving time by zone, using each run's average grade-adjusted pace. Zones are anchored to your critical speed (Z4 = threshold). A polarised distribution (lots of Z1-Z2, some Z4-Z5, little Z3) is the typical endurance-training target.</p>
  </div>
</div>

<div class="ov-section">
  <p class="ov-section-title">Training log</p>
  <div class="card">
    {cal_chart}
    <p class="note">Daily training load. Darker green = higher load. Gaps are rest days.</p>
  </div>
</div>

<script>
var PTOT_DATA = {totals_json};
</script>
<script>
// ── Per-period totals chart (Strava-style, interactive) ──────────────────────
var PTOT_STATE = {{metric:'dist', win:180, gran:'week', overlay:false}};
var PTOT_RUNS_COLOR = '#6b6b6b';
var PTOT_META = {{
  dist: {{unit:'km', color:'#5cb85c'}},
  elev: {{unit:'m',  color:'#8a6db5'}},
  time: {{unit:'h',  color:'#5b8fd9'}}
}};
var PTOT_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function ptotFmt(metric, v){{
  if(metric==='time'){{
    var h=Math.floor(v), m=Math.round((v-h)*60);
    if(m===60){{h++;m=0;}}
    return h+'h '+(m<10?'0':'')+m+'m';
  }}
  if(metric==='dist') return (Math.round(v*10)/10)+' km';
  return Math.round(v)+' m';
}}

function ptotShortDate(iso, gran){{
  var d=new Date(iso+'T00:00:00');
  if(gran==='month') return PTOT_MON[d.getMonth()];
  return d.getDate()+' '+PTOT_MON[d.getMonth()];
}}

function ptotRangeLabel(iso, gran){{
  var d=new Date(iso+'T00:00:00');
  if(gran==='month') return PTOT_MON[d.getMonth()]+' '+d.getFullYear();
  var e=new Date(d.getTime()+6*86400000);
  return d.getDate()+' '+PTOT_MON[d.getMonth()]+' – '+e.getDate()+' '+PTOT_MON[e.getMonth()];
}}

function ptotWindow(){{
  var data=(PTOT_DATA[PTOT_STATE.gran]||[]).slice();
  var win=PTOT_STATE.win;
  if(win>0 && data.length){{
    var last=new Date(data[data.length-1].date+'T00:00:00');
    var cut=last.getTime()-win*86400000;
    data=data.filter(function(p){{ return new Date(p.date+'T00:00:00').getTime()>=cut; }});
  }}
  return data;
}}

function ptotSetTotal(txt){{ var el=document.getElementById('ptot-total'); if(el) el.textContent=txt; }}

function drawPtot(){{
  var holder=document.getElementById('ptot-chart');
  if(!holder) return;
  var metric=PTOT_STATE.metric, meta=PTOT_META[metric];
  var data=ptotWindow();
  if(data.length<2){{ holder.innerHTML='<p class="note">Not enough data for this window.</p>'; ptotSetTotal(''); return; }}
  var overlay=PTOT_STATE.overlay;
  var W=720,H=180,padL=44,padR=overlay?30:12,padT=14,padB=24;
  var vals=data.map(function(p){{ return p[metric]; }});
  var vmin=0, vmax=Math.max.apply(null, vals); if(vmax<=0) vmax=1;
  var runs=data.map(function(p){{ return p.runs||0; }});
  var rmax=Math.max.apply(null, runs); if(rmax<=0) rmax=1;
  var n=data.length;
  function px(i){{ return padL+(n<=1?0:i/(n-1))*(W-padL-padR); }}
  function py(v){{ return H-padB-(v-vmin)/(vmax-vmin)*(H-padT-padB); }}
  function ry(c){{ return H-padB-(c/rmax)*(H-padT-padB); }}
  var grid='';
  for(var g=0; g<=2; g++){{
    var gv=vmin+g/2*(vmax-vmin), gy=py(gv);
    grid+='<line x1="'+padL+'" y1="'+gy.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+gy.toFixed(1)+'" stroke="#2a2a2a" stroke-width="1"/>'
        +'<text x="'+(padL-6)+'" y="'+(gy+3).toFixed(1)+'" font-size="8" fill="#666" text-anchor="end">'+Math.round(gv)+'</text>';
    if(overlay){{
      grid+='<text x="'+(W-padR+5)+'" y="'+(gy+3).toFixed(1)+'" font-size="8" fill="'+PTOT_RUNS_COLOR+'" text-anchor="start">'+Math.round(rmax*g/2)+'</text>';
    }}
  }}
  // run-count bars (drawn first so the metric line sits on top). Bar height
  // still encodes run count; the bar is split into one notch per run, each
  // sized by that run's share of the period total for the active metric
  // (oldest run at the bottom). Flat periods for the metric fall back to equal
  // notches so every run still shows.
  var bars='';
  if(overlay){{
    var slot=(W-padL-padR)/Math.max(1,n), bw=Math.min(slot*0.6, 14);
    for(var bi=0;bi<n;bi++){{
      var c=runs[bi];
      if(!c) continue;
      var bx=px(bi)-bw/2, byTop=ry(c), bh=(H-padB)-byTop;
      var bd=(data[bi].breakdown)||[];
      bars+='<rect x="'+bx.toFixed(1)+'" y="'+byTop.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+bh.toFixed(1)+'" rx="1" fill="'+PTOT_RUNS_COLOR+'" opacity="0.18"/>';
      if(bd.length){{
        var mtot=0; for(var k=0;k<bd.length;k++) mtot+=(bd[k][metric]||0);
        var yb=H-padB;  // build upward from the axis
        for(var k=0;k<bd.length;k++){{
          var share=(mtot>0)?((bd[k][metric]||0)/mtot):(1/bd.length);
          var sh=bh*share, sy=yb-sh;
          bars+='<rect x="'+bx.toFixed(1)+'" y="'+sy.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+Math.max(0,sh).toFixed(1)+'" fill="'+PTOT_RUNS_COLOR+'" opacity="'+((k%2)?0.6:0.42)+'" stroke="#1a1a1a" stroke-width="0.6"/>';
          yb=sy;
        }}
      }}
    }}
  }}
  var line=data.map(function(p,i){{ return (i?'L':'M')+px(i).toFixed(1)+','+py(p[metric]).toFixed(1); }}).join(' ');
  var area='M'+px(0).toFixed(1)+','+(H-padB)+data.map(function(p,i){{ return 'L'+px(i).toFixed(1)+','+py(p[metric]).toFixed(1); }}).join('')+'L'+px(n-1).toFixed(1)+','+(H-padB)+'Z';
  var xlab='', step=Math.max(1, Math.round(n/6));
  for(var i=0;i<n;i+=step){{
    xlab+='<text x="'+px(i).toFixed(1)+'" y="'+(H-8)+'" font-size="8" fill="#555" text-anchor="middle">'+ptotShortDate(data[i].date, PTOT_STATE.gran)+'</text>';
  }}
  var rlab=overlay?'<text x="'+(W-padR+5)+'" y="'+(padT-4)+'" font-size="8" fill="'+PTOT_RUNS_COLOR+'" text-anchor="start">runs</text>':'';
  var svg='<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">'
    +'<defs><linearGradient id="ptot-grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+meta.color+'" stop-opacity="0.30"/><stop offset="1" stop-color="'+meta.color+'" stop-opacity="0.02"/></linearGradient></defs>'
    +grid+bars+'<path d="'+area+'" fill="url(#ptot-grad)"/><path d="'+line+'" fill="none" stroke="'+meta.color+'" stroke-width="2"/>'+xlab+rlab
    +'<g id="ptot-cursor" style="opacity:0;pointer-events:none"><line y1="'+padT+'" y2="'+(H-padB)+'" stroke="#888" stroke-width="1" stroke-dasharray="3,3"/><circle r="4" fill="'+meta.color+'" stroke="#fff" stroke-width="1.5"/></g>'
    +'</svg>';
  holder.innerHTML=svg+'<div class="ptot-tip" id="ptot-tip"></div>';
  var sum=vals.reduce(function(a,b){{ return a+b; }}, 0);
  ptotSetTotal('Total '+ptotFmt(metric, sum));
  holder._ptot={{data:data, metric:metric, n:n, W:W, H:H, padL:padL, padR:padR, padT:padT, padB:padB, vmin:vmin, vmax:vmax, sum:sum, overlay:overlay}};
  holder.onmousemove=function(e){{ ptotHover(e, holder); }};
  holder.onmouseleave=function(){{
    var c=document.getElementById('ptot-cursor'); if(c) c.style.opacity=0;
    var t=document.getElementById('ptot-tip'); if(t) t.style.opacity=0;
    ptotSetTotal('Total '+ptotFmt(metric, holder._ptot.sum));
  }};
}}

function ptotHover(e, holder){{
  var st=holder._ptot; if(!st) return;
  var svg=holder.querySelector('svg'); if(!svg) return;
  var sr=svg.getBoundingClientRect();
  var sx=(e.clientX-sr.left)/sr.width*st.W;
  function px(i){{ return st.padL+(st.n<=1?0:i/(st.n-1))*(st.W-st.padL-st.padR); }}
  function py(v){{ return st.H-st.padB-(v-st.vmin)/(st.vmax-st.vmin)*(st.H-st.padT-st.padB); }}
  var bi=0, bd=Infinity;
  for(var i=0;i<st.n;i++){{ var dx=Math.abs(px(i)-sx); if(dx<bd){{ bd=dx; bi=i; }} }}
  var p=st.data[bi], cx=px(bi), cy=py(p[st.metric]);
  var cur=document.getElementById('ptot-cursor');
  if(cur){{ cur.style.opacity=1; var ln=cur.querySelector('line'), dot=cur.querySelector('circle'); ln.setAttribute('x1',cx); ln.setAttribute('x2',cx); dot.setAttribute('cx',cx); dot.setAttribute('cy',cy); }}
  var tip=document.getElementById('ptot-tip');
  if(tip){{
    var runLine=st.overlay?'<div style="color:'+PTOT_RUNS_COLOR+'">'+(p.runs||0)+' run'+((p.runs===1)?'':'s')+'</div>':'';
    tip.innerHTML='<div class="ptot-tip-d">'+ptotRangeLabel(p.date, PTOT_STATE.gran)+'</div><div><b>'+ptotFmt(st.metric, p[st.metric])+'</b></div>'+runLine;
    var hr=holder.getBoundingClientRect(), scaleX=sr.width/st.W, scaleY=sr.height/st.H;
    tip.style.left=((sr.left-hr.left)+cx*scaleX)+'px';
    tip.style.top=((sr.top-hr.top)+cy*scaleY)+'px';
    tip.style.opacity=1;
  }}
  ptotSetTotal(ptotRangeLabel(p.date, PTOT_STATE.gran)+': '+ptotFmt(st.metric, p[st.metric]));
}}

function ptotSet(kind, val, btn){{
  PTOT_STATE[kind]=val;
  var bs=btn.parentNode.querySelectorAll('.rng-btn');
  for(var i=0;i<bs.length;i++) bs[i].classList.toggle('active', bs[i]===btn);
  drawPtot();
}}

function ptotToggleOverlay(btn){{
  PTOT_STATE.overlay=!PTOT_STATE.overlay;
  btn.classList.toggle('active', PTOT_STATE.overlay);
  drawPtot();
}}

drawPtot();
</script>
"""


# Exported for hub embedding (the dashboard-specific CSS without body-level rules)
ANALYTICS_PANEL_CSS = SHARED_CSS


def body_analytics(rows: list[dict], updated: str, runs: list[dict] | None = None) -> str:
    html = generate(rows, updated, runs)
    s = html.index('<body>') + len('<body>')
    e = html.index('</body>')
    return html[s:e].strip()


def main():
    here = Path(__file__).resolve().parent.parent
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
        updated = datetime.strptime(date_str, "%Y-%m-%d").strftime("%#d %b %Y")
    except (ValueError, IndexError):
        updated = datetime.today().strftime("%#-d-%b-%Y")
        
    dashboards_dir = here / "dashboards"
    dashboards_dir.mkdir(exist_ok=True)
    out = dashboards_dir / "analytics_dashboard.html"
    out.write_text(generate(rows, updated), encoding="utf-8")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
