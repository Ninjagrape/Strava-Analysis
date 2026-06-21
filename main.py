#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

STEPS = [
    ("Compile Strava export",    HERE / "strava_compile.py"),
    ("Generate hub (all dashboards)", HERE / "generate_hub.py"),
]

def run_step(label: str, script: Path):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = subprocess.run([sys.executable, str(script)], check=False)
    if result.returncode != 0:
        sys.exit(f"\nFailed at: {label} (exit code {result.returncode})")

if __name__ == "__main__":
    for label, script in STEPS:
        run_step(label, script)
    print("\nAll done. Open dashboards/TrainingHub.html to start.")
