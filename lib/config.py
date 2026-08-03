#!/usr/bin/env python3
"""
config.py
Optional, user-supplied personalization loaded from cache/config.json.

Everything here is optional. With no config.json (or a malformed one) the pipeline
falls back to fully data-driven behaviour: the goal panel shows generic race
predictions only, and route segments are auto-detected from GPS tracks. Copy
config.example.json to cache/config.json to declare your own races or to pin/rename a
loop the segment detector misses.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "cache" / "config.json"


@dataclass(frozen=True)
class Race:
    name: str
    distance_m: float
    date: str = ""              # display-only, e.g. "2026-09"
    target_time_s: int | None = None
    target_wave_s: tuple[int, int] | None = None
    notes: str = ""


@dataclass(frozen=True)
class SegmentAnchor:
    name: str
    center: tuple[float, float]   # (lat, lon)
    len_m: float


@dataclass(frozen=True)
class DuplicateOverrides:
    """Manual corrections to automatic duplicate detection, keyed by Activity ID."""
    not_duplicates: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    force_duplicate: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (loser, winner)
    force_primary: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Config:
    races: tuple[Race, ...] = field(default_factory=tuple)
    segment_anchors: tuple[SegmentAnchor, ...] = field(default_factory=tuple)
    duplicates: DuplicateOverrides = field(default_factory=DuplicateOverrides)


def _parse_race(d: dict) -> Race | None:
    try:
        wave = d.get("target_wave_s")
        return Race(
            name=str(d["name"]),
            distance_m=float(d["distance_m"]),
            date=str(d.get("date", "")),
            target_time_s=int(d["target_time_s"]) if d.get("target_time_s") is not None else None,
            target_wave_s=(int(wave[0]), int(wave[1])) if wave else None,
            notes=str(d.get("notes", "")),
        )
    except (KeyError, TypeError, ValueError, IndexError) as e:
        print(f"[config] skipping malformed race entry ({e})")
        return None


def _parse_anchor(d: dict) -> SegmentAnchor | None:
    try:
        cen = d["center"]
        return SegmentAnchor(
            name=str(d["name"]),
            center=(float(cen[0]), float(cen[1])),
            len_m=float(d["len_m"]),
        )
    except (KeyError, TypeError, ValueError, IndexError) as e:
        print(f"[config] skipping malformed segment_anchor entry ({e})")
        return None


def _parse_duplicates(d: dict) -> DuplicateOverrides:
    """Every field is independently salvageable: one malformed entry is dropped
    rather than costing the user the whole block."""
    pairs = []
    for p in d.get("not_duplicates", []):
        try:
            a, b = str(p[0]).strip(), str(p[1]).strip()
            if a and b and a != b:
                pairs.append((a, b))
        except (KeyError, TypeError, ValueError, IndexError) as e:
            print(f"[config] skipping malformed not_duplicates entry ({e})")

    forced = []
    raw_forced = d.get("force_duplicate", {})
    if isinstance(raw_forced, dict):
        for loser, winner in raw_forced.items():
            try:
                lo, wi = str(loser).strip(), str(winner).strip()
                if lo and wi and lo != wi:
                    forced.append((lo, wi))
            except (TypeError, ValueError) as e:
                print(f"[config] skipping malformed force_duplicate entry ({e})")
    elif raw_forced:
        print("[config] force_duplicate must be an object of loser -> winner, ignoring it")

    primary = tuple(str(a).strip() for a in d.get("force_primary", []) if str(a).strip())
    return DuplicateOverrides(tuple(pairs), tuple(forced), primary)


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load config.json if present. Never raises: a missing or malformed file
    yields an empty Config so the pipeline degrades to data-driven defaults."""
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] {path.name} is unreadable, ignoring it ({e})")
        return Config()
    if not isinstance(raw, dict):
        print(f"[config] {path.name} is not a JSON object, ignoring it")
        return Config()

    races = tuple(r for r in (_parse_race(d) for d in raw.get("races", []) if isinstance(d, dict)) if r)
    anchors = tuple(a for a in (_parse_anchor(d) for d in raw.get("segment_anchors", []) if isinstance(d, dict)) if a)
    dup_raw = raw.get("duplicates", {})
    dups = _parse_duplicates(dup_raw) if isinstance(dup_raw, dict) else DuplicateOverrides()
    return Config(races=races, segment_anchors=anchors, duplicates=dups)
