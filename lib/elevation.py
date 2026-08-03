#!/usr/bin/env python3
"""
elevation.py
Fills in per-point elevation for runs whose recording device never captured any.

Some uploads carry a GPS track but no altitude at all: the Mi Fitness / Zepp band
writes only timestamp, distance, position, heart rate and cadence, and its session
totals read 0 ascent / 0 descent. Strava's own app still shows a profile for those
activities because it looks each coordinate up against a digital elevation model
server-side; the bulk export passes on only the run-level summary. We do the same
lookup here, so a route the device could not measure still gets the profile the
terrain implies.

Only runs with a GPS track and no altitude at all are touched. A run that recorded
its own altitude keeps it: a device's barometric or GPS reading follows the actual
path taken, including the footbridge a 90 m DEM cell knows nothing about.

Lookups are anchored, not per point. Stream points sit ~7 m apart, far finer than
any DEM, so we sample anchors every DEM_ANCHOR_M along the route and interpolate
between them over cumulative distance, exactly as strava_compile's `elev_at` does
for sparse recorded altitude. A 5 km run costs ~200 lookups, two batched requests,
and almost all of those are cache hits once a route has been seen once.

Nothing here is ever fatal. No network, a refusing API, a corrupt cache: the run
simply keeps no elevation and the build carries on, which is the same outcome as
before this module existed. Set STRAVA_DEM_OFF=1 to skip the network entirely.
"""

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_DIR   = Path(__file__).resolve().parent.parent / "cache"
DEM_CACHE   = CACHE_DIR / "elevation_dem_cache.json"

# Open-Meteo's elevation endpoint: Copernicus DEM GLO-90, worldwide, no API key
# for non-commercial use. Attribution: Copernicus / Open-Meteo,
# https://doi.org/10.5270/ESA-c5d3d65
DEM_URL       = "https://api.open-meteo.com/v1/elevation"
USER_AGENT    = "Strava-Analysis-Hub/1.0 (personal training dashboard)"

DEM_BATCH     = 100     # coordinates per request (the API's documented maximum)
DEM_TIMEOUT_S = 20.0
DEM_ANCHOR_M  = 50.0    # spacing of lookup anchors along a route: still two samples
                        # per 90 m DEM cell, at half the lookups of a 25 m spacing
DEM_KEY_DP    = 4       # cache-key precision, ~11 m: finer than the DEM's own cell,
                        # coarse enough that repeat runs down a street reuse entries
DEM_MIN_ANCHORS = 2     # below this a route cannot be interpolated at all
DEM_MIN_COVER = 0.80    # accept a profile only if this fraction of its anchors resolved,
                        # or a hole in the lookups becomes a straight line through a hill

# A route crossing a DEM's cell boundaries picks up a metre or two of step at each
# one, and summing positive deltas turns that jitter into elevation gain that was
# never run: unsmoothed, a flat foreshore route read 68 m of gain against Strava's
# 2 m. A centred mean over DEM_SMOOTH_W anchors (~350 m of route) is what brought
# the five affected runs closest to Strava's own figures: 405 -> 171 m against
# their 187 m, 189 -> 73 m against 95 m, 223 -> 58 m against 61 m (2026-08-02).
# Real hills are far longer than the window and survive it; net elevation, which
# sets segment grade, is unaffected by jitter either way.
DEM_SMOOTH_W  = 7

# Open-Meteo bills by coordinate, not by request: 600 coordinates in one burst is
# enough for a 429 even though that is only six calls. Pace the batches to stay
# under it, and treat a 429 as "wait", never as "give up" (verified 2026-08-02).
DEM_SLEEP_S   = 12.0    # between batches, i.e. ~500 coordinates per minute
DEM_RETRY_S   = 60.0    # after a 429 with no Retry-After header
DEM_RETRIES   = 3

_EARTH_R_M = 6371008.8


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(h)))


def _load_cache() -> dict:
    try:
        data = json.loads(DEM_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        DEM_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"Elevation: could not write {DEM_CACHE.name}: {e}")


def _key(lat: float, lon: float) -> str:
    """Cache key and the coordinate actually sent, so the two can never disagree."""
    return f"{round(lat, DEM_KEY_DP)},{round(lon, DEM_KEY_DP)}"


def _needs_elevation(stream: list) -> bool:
    """True for a run that has a usable GPS track but no altitude anywhere.

    A single stray altitude sample is enough to leave the run alone: partially
    recorded elevation is the device's own account of the route, and mixing a DEM
    into it would put a step at the join.
    """
    if not stream:
        return False
    if any(p.get("elev") is not None for p in stream):
        return False
    return sum(1 for p in stream if p.get("lat") is not None) >= DEM_MIN_ANCHORS


def _anchors(stream: list) -> list:
    """[(distance_km, cache_key)] every ~DEM_ANCHOR_M along the route.

    Both endpoints are always included so interpolation never has to extrapolate.
    """
    pts = [p for p in stream if p.get("lat") is not None and p.get("lon") is not None]
    if len(pts) < DEM_MIN_ANCHORS:
        return []
    out = [(pts[0]["d"], _key(pts[0]["lat"], pts[0]["lon"]))]
    last = pts[0]
    for p in pts[1:-1]:
        if _haversine_m(last["lat"], last["lon"], p["lat"], p["lon"]) >= DEM_ANCHOR_M:
            out.append((p["d"], _key(p["lat"], p["lon"])))
            last = p
    out.append((pts[-1]["d"], _key(pts[-1]["lat"], pts[-1]["lon"])))
    return out


def _request(batch: list):
    """Elevations for one batch of cache keys, or None if the API would not answer.

    A 429 is a pace complaint, not a refusal, so it is waited out and retried.
    """
    lats, lons = zip(*(k.split(",") for k in batch))
    params = urllib.parse.urlencode({"latitude": ",".join(lats),
                                     "longitude": ",".join(lons)})
    req = urllib.request.Request(f"{DEM_URL}?{params}",
                                 headers={"User-Agent": USER_AGENT})
    for attempt in range(DEM_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=DEM_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8")).get("elevation") or []
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == DEM_RETRIES - 1:
                print(f"Elevation: DEM lookup failed ({e}); runs without altitude keep none")
                return None
            wait = float(e.headers.get("Retry-After") or DEM_RETRY_S)
            print(f"Elevation: DEM rate limit, waiting {wait:.0f}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError, ValueError) as e:
            print(f"Elevation: DEM lookup failed ({e}); runs without altitude keep none")
            return None
    return None


def _fetch(keys: list, cache: dict) -> int:
    """Look up every key not already cached. Returns the number newly resolved.

    A key the API answers for is cached as a number; one it cannot answer is cached
    as None, so a hole in the DEM is asked about once rather than on every build.
    """
    missing = [k for k in keys if k not in cache]
    if not missing:
        return 0
    resolved = 0
    for i in range(0, len(missing), DEM_BATCH):
        batch = missing[i:i + DEM_BATCH]
        values = _request(batch)
        if values is None:
            return resolved
        if len(values) != len(batch):
            print(f"Elevation: DEM returned {len(values)} values for {len(batch)} "
                  f"coordinates; skipping the rest")
            return resolved
        for k, v in zip(batch, values):
            cache[k] = float(v) if isinstance(v, (int, float)) else None
            resolved += 1
        if i + DEM_BATCH < len(missing):
            time.sleep(DEM_SLEEP_S)
    return resolved


def _smooth(values: list) -> list:
    """Centred moving mean over DEM_SMOOTH_W samples, shrinking at the ends so the
    first and last anchors keep their own value's weight."""
    if DEM_SMOOTH_W <= 1 or len(values) < 3:
        return values
    half = DEM_SMOOTH_W // 2
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _apply(stream: list, anchors: list, cache: dict) -> bool:
    """Interpolate anchor elevations across every stream point. False if not possible."""
    known = [(d, cache[k]) for d, k in anchors if cache.get(k) is not None]
    if len(known) < DEM_MIN_ANCHORS or len(known) < DEM_MIN_COVER * len(anchors):
        return False
    known = list(zip((d for d, _ in known), _smooth([e for _, e in known])))
    dists = [d for d, _ in known]
    for p in stream:
        d = p.get("d")
        if d is None:
            continue
        if d <= dists[0]:
            p["elev"] = round(known[0][1], 1)
            continue
        if d >= dists[-1]:
            p["elev"] = round(known[-1][1], 1)
            continue
        lo, hi = 0, len(known) - 1
        while hi - lo > 1:                      # bisect on distance
            mid = (lo + hi) // 2
            if dists[mid] <= d:
                lo = mid
            else:
                hi = mid
        d0, e0 = known[lo]
        d1, e1 = known[hi]
        frac = (d - d0) / (d1 - d0) if d1 > d0 else 0.0
        p["elev"] = round(e0 + frac * (e1 - e0), 1)
    return True


def _refill_km_splits(run: dict) -> None:
    """Recompute per-km gain/loss now that the stream has elevation.

    strava_compile derived these at parse time, when there was no altitude to derive
    them from, so the splits carry only a time until they are redone here.
    """
    splits = run.get("km_splits") or []
    if not splits:
        return
    gain, loss = {}, {}
    prev = None
    for p in run.get("dist_stream") or []:
        e, d = p.get("elev"), p.get("d")
        if e is None or d is None:
            continue
        km = int(d) + 1                          # km 1 covers 0.0-1.0
        if prev is not None:
            delta = e - prev
            if delta > 0:
                gain[km] = gain.get(km, 0.0) + delta
            else:
                loss[km] = loss.get(km, 0.0) - delta
        prev = e
    for s in splits:
        km = s.get("km")
        s["gain_m"] = round(gain.get(km, 0.0))
        s["loss_m"] = round(loss.get(km, 0.0))


def backfill_elevation(runs: list) -> dict:
    """Give every GPS-carrying run without altitude a DEM-derived elevation profile.

    Tags each run's `elev_source`: "device" for a recorded profile, "dem" for one
    filled in here, None for a run that has no elevation either way (a treadmill
    recording, or a route the DEM could not answer for). Mutates runs in place and
    returns a summary for the caller to print.
    """
    todo = [r for r in runs if _needs_elevation(r.get("dist_stream") or [])]
    for r in runs:
        r["elev_source"] = ("device" if any(p.get("elev") is not None
                                            for p in (r.get("dist_stream") or []))
                            else None)
    summary = {"candidates": len(todo), "filled": 0, "fetched": 0, "cached": 0}
    if not todo:
        return summary

    cache = _load_cache()
    summary["cached"] = len(cache)
    per_run = {id(r): _anchors(r["dist_stream"]) for r in todo}
    wanted, seen = [], set()
    for anchors in per_run.values():
        for _, k in anchors:
            if k not in seen:
                seen.add(k)
                wanted.append(k)

    if os.environ.get("STRAVA_DEM_OFF"):
        print("Elevation: STRAVA_DEM_OFF set, using cached DEM values only")
    else:
        summary["fetched"] = _fetch(wanted, cache)
        if summary["fetched"]:
            _save_cache(cache)

    for r in todo:
        if _apply(r["dist_stream"], per_run[id(r)], cache):
            r["elev_source"] = "dem"
            _refill_km_splits(r)
            summary["filled"] += 1
    return summary
