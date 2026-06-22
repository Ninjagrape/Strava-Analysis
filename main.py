#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# (label, script, forward_args) — only the compile step accepts CLI flags
# like --profile/--rebuild/--workers; the hub reads STRAVA_PROFILE from env.
STEPS = [
    ("Compile Strava export",         HERE / "strava_compile.py", True),
    ("Generate hub (all dashboards)", HERE / "generate_hub.py",   False),
]

def run_step(label: str, script: Path, forward_args: bool):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    cmd = [sys.executable, str(script)]
    if forward_args:
        cmd += sys.argv[1:]
    start = time.perf_counter()
    result = subprocess.run(cmd, check=False)
    print(f"  [time] {label}: {time.perf_counter() - start:.1f}s")
    if result.returncode != 0:
        sys.exit(f"\nFailed at: {label} (exit code {result.returncode})")

if __name__ == "__main__":
    if "--profile" in sys.argv[1:]:
        os.environ["STRAVA_PROFILE"] = "1"
    overall = time.perf_counter()
    for label, script, forward_args in STEPS:
        run_step(label, script, forward_args)
    print(f"\nAll done in {time.perf_counter() - overall:.1f}s. "
          "Open dashboards/TrainingHub.html to start.")
