"""Grade-adjustment helpers shared by generate_analytics.py and generate_dashboards.py.

Minetti's energy-cost-of-running polynomial and the grade-adjusted-time conversion
built on it. Kept in one place so the two consumers cannot drift.
"""


def minetti_cost(g: float) -> float:
    return 155.4 * g ** 5 - 30.4 * g ** 4 - 43.3 * g ** 3 + 46.3 * g ** 2 + 19.5 * g + 3.6


COST_FLAT = minetti_cost(0)


def ga_time(raw_s: float, elev_gain_m: float, dist_km: float) -> float:
    """Grade-adjusted time using Minetti formula, half one-way ascent ratio."""
    if not dist_km:
        return raw_s
    grade = (elev_gain_m / 2) / (dist_km * 1000)
    return raw_s * (COST_FLAT / minetti_cost(grade))
