#!/usr/bin/env python3
"""
taper.py
Race readiness for the Goals tab: projects the pace-based load model forward from the
last recorded day to race morning, under two plans. "As you are" keeps the recent
average daily load all the way in; "with taper" holds it until the taper opens, then
cuts volume. The race card shows both, so the reader sees what the taper buys.

The projection reuses generate_analytics' own session_loads/daily_series/ctl_atl_tsb,
padding the real history with synthetic future days, so race-day form is on exactly
the curve the Analytics tab draws. Imported by the hub only: generate_analytics
imports generate_dashboards, so the dashboards module cannot import this one back.
"""

from datetime import date, datetime, timedelta

from config import Race
from generate_analytics import ctl_atl_tsb, daily_series, daily_totals, session_loads

# Bosquet et al. (2007) meta-analysis: the performance gain peaks with an 8-14 day
# taper that cuts volume by 41-60% while intensity and frequency are held. Shorter
# races need less: a 10K block carries little fatigue worth shedding.
TAPER_DAYS = ((10_000 * 1.02, 7), (21_097.5 * 1.02, 10), (float("inf"), 14))
TAPER_CUT = 0.5             # fraction of volume removed once the taper opens
RECENT_DAYS = 28            # the "as you are" daily load is the mean over this window
RACE_TSB_LO = 5.0           # race-morning form band; below it you start tired,
RACE_TSB_HI = 25.0          # above it the taper has started costing fitness
TSB_TIRED = -10.0           # matches generate_analytics._tsb_class's red line


def parse_race_date(raw: str) -> tuple[date, bool] | None:
    """(date, exact). A month-only date ("2027-05") resolves to the 1st, the earliest
    the race can be, so a taper planned from it opens early rather than late."""
    for fmt, exact in (("%Y-%m-%d", True), ("%Y-%m", False)):
        try:
            return datetime.strptime(raw.strip(), fmt).date(), exact
        except ValueError:
            continue
    return None


def taper_days(dist_m: float) -> int:
    return next(days for limit, days in TAPER_DAYS if dist_m <= limit)


def _project(daily: list[tuple], race_day: date, taper_start: date, load: float,
             cut: float) -> dict:
    """Race-morning CTL/ATL/TSB after padding `daily` out to the day before the race.
    ctl_atl_tsb reports a day's TSB from the previous day's state, so race morning is
    the CTL minus ATL left at the end of the eve."""
    future = []
    day = daily[-1][0] + timedelta(days=1)
    while day < race_day:
        future.append((day, load * (1 - cut) if day >= taper_start else load))
        day += timedelta(days=1)
    end = ctl_atl_tsb(list(daily) + future)[-1]
    return {"ctl": end["ctl"], "tsb": end["ctl"] - end["atl"]}


def race_plan(race: Race, daily: list[tuple], day_km: dict) -> dict | None:
    """Readiness numbers for one goal race, or None when there is nothing to project
    (no load history, or a race date that cannot be read)."""
    parsed = parse_race_date(race.date) if race.date else None
    if not parsed or not daily:
        return None
    race_day, exact = parsed
    last = daily[-1][0]
    days = (race_day - last).days
    if days <= 0:
        return {"phase": "passed", "race_day": race_day, "exact": exact}

    recent = [load for _, load in daily[-RECENT_DAYS:]]
    load = sum(recent) / len(recent)
    window_start = last - timedelta(days=RECENT_DAYS - 1)
    week_km = sum(s["dist"] for d, s in day_km.items() if window_start <= d <= last) \
        / (RECENT_DAYS / 7)

    t_days = taper_days(race.distance_m)
    taper_start = race_day - timedelta(days=t_days)
    hold = _project(daily, race_day, race_day, load, 0.0)
    taper = _project(daily, race_day, taper_start, load, TAPER_CUT)
    return {
        "phase": "taper" if days <= t_days else "build",
        "race_day": race_day, "exact": exact, "days": days, "as_of": last,
        "taper_days": t_days, "taper_start": taper_start,
        "week_km": week_km, "taper_week_km": week_km * (1 - TAPER_CUT),
        "tsb_hold": hold["tsb"], "tsb_taper": taper["tsb"], "ctl_race": taper["ctl"],
    }


def _form_class(tsb: float) -> str:
    if RACE_TSB_LO <= tsb <= RACE_TSB_HI:
        return "green"
    return "red" if tsb < TSB_TIRED else "amber"


def _form_verdict(tsb: float) -> str:
    if tsb < RACE_TSB_LO:
        return "still carrying fatigue"
    if tsb > RACE_TSB_HI:
        return "fresh, but shedding fitness"
    return "in the race-day band"


def _signed(v: float) -> str:
    """+N / -N, never "-0" for a value that rounds to zero."""
    return f"{round(v) or 0:+d}"


def _fmt_day(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def render_taper(plan: dict) -> str:
    """The readiness strip appended to a race card."""
    if plan["phase"] == "passed":
        return ('<p class="gap-note">This race date has passed. Set its activity_id in '
                'cache/config.json to file it as run and drop it from Goals.</p>')

    when = _fmt_day(plan["race_day"])
    when_sub = when if plan["exact"] else f"~{when} (month only, set YYYY-MM-DD for an exact countdown)"
    if plan["phase"] == "taper":
        taper_val = "Now"
        taper_sub = f"~{plan['taper_week_km']:.0f} km/wk, keep the quality sessions"
    else:
        taper_val = f"{plan['taper_start'].day} {plan['taper_start'].strftime('%b')}"
        taper_sub = f"{plan['taper_days']}-day taper to ~{plan['taper_week_km']:.0f} km/wk"
    tsb = plan["tsb_taper"]
    cls = _form_class(tsb)

    return f"""\
    <div class="taper">
      <p class="taper-title">Race readiness</p>
      <div class="race-body">
        <div>
          <p class="stat-label">Race in</p>
          <p class="stat-value">{plan['days']} days</p>
          <p class="stat-sub">{when_sub}</p>
        </div>
        <div>
          <p class="stat-label">Taper opens</p>
          <p class="stat-value">{taper_val}</p>
          <p class="stat-sub">{taper_sub}</p>
        </div>
        <div>
          <p class="stat-label">Race-day form (TSB)</p>
          <p class="stat-value {cls}">{_signed(tsb)}</p>
          <p class="stat-sub">{_form_verdict(tsb)} · no taper: {_signed(plan['tsb_hold'])}</p>
        </div>
      </div>
      <p class="gap-note">Projected from your last {RECENT_DAYS // 7} weeks (~{plan['week_km']:.0f} km/wk, as of {_fmt_day(plan['as_of'])}) held to the taper, then a {TAPER_CUT:.0%} volume cut with intensity unchanged. Aim for +{RACE_TSB_LO:.0f} to +{RACE_TSB_HI:.0f} on race morning. Fitness (CTL) on the day: {plan['ctl_race']:.0f}.</p>
    </div>"""


def race_taper_blocks(races: tuple[Race, ...], rows: list[dict]) -> dict[str, str]:
    """{race name: readiness HTML} for every goal race (no activity_id) that can be
    projected. Races that cannot are left out, and their cards render as before."""
    goal_races = [r for r in races if not r.activity_id]
    if not goal_races:
        return {}
    daily = daily_series(session_loads(rows))
    day_km = daily_totals(rows)
    out = {}
    for race in goal_races:
        plan = race_plan(race, daily, day_km)
        if plan:
            out[race.name] = render_taper(plan)
    return out
