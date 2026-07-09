#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# Orchestration-only flags consumed by main.py; never forwarded to the compile
# step (its argparse would reject them). They translate into a compile flag
# and/or an env var the hub reads.
#   --rebuild            re-parse every .fit (compile parse cache) — a compile flag
#   --rebuild-segments   force a segment-cache rebuild (STRAVA_SEG_REBUILD for the hub)
#   --rebuild-all        both of the above
MAIN_ONLY_FLAGS = {"--rebuild-segments", "--rebuild-all"}

USAGE = """\
Usage: python main.py [flags]

  --profile            print per-phase timings (sets STRAVA_PROFILE for the hub)
  --rebuild            re-parse every .fit file, ignore the prior-CSV parse cache
  --rebuild-segments   ignore segments_cache.json and re-detect segments
  --rebuild-all        --rebuild and --rebuild-segments together
  --workers N          parallel parse workers (forwarded to compile)

Any other flag is forwarded to strava_compile.py (see its --help).
Hub-only regen can force a segment rebuild with:
  STRAVA_SEG_REBUILD=1 python generate_hub.py
"""

def run_step(label: str, script: Path, extra_args: list):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    cmd = [sys.executable, str(script)] + extra_args
    start = time.perf_counter()
    result = subprocess.run(cmd, check=False)
    print(f"  [time] {label}: {time.perf_counter() - start:.1f}s")
    if result.returncode != 0:
        sys.exit(f"\nFailed at: {label} (exit code {result.returncode})")

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        sys.exit(0)

    if "--profile" in argv:
        os.environ["STRAVA_PROFILE"] = "1"
    if "--rebuild-segments" in argv or "--rebuild-all" in argv:
        os.environ["STRAVA_SEG_REBUILD"] = "1"

    # Compile only understands its own flags: drop the orchestration-only ones
    # and expand --rebuild-all into compile's --rebuild.
    compile_args = [a for a in argv if a not in MAIN_ONLY_FLAGS]
    if "--rebuild-all" in argv and "--rebuild" not in compile_args:
        compile_args.append("--rebuild")

    steps = [
        ("Compile Strava export",         HERE / "strava_compile.py", compile_args),
        ("Generate hub (all dashboards)", HERE / "generate_hub.py",   []),
    ]
    overall = time.perf_counter()
    for label, script, extra_args in steps:
        run_step(label, script, extra_args)
    print(f"\nAll done in {time.perf_counter() - overall:.1f}s. "
          "Open dashboards/TrainingHub.html to start.")
