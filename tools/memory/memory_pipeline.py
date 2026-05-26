#!/usr/bin/env python3
"""
memory_pipeline.py — P2 manual orchestration entry point.

Runs reflect → synthesize end-to-end.

Usage:
  python memory_pipeline.py              # dry-run (no files written)
  python memory_pipeline.py --apply      # write reflections + active_lessons.md
  python memory_pipeline.py --reflect-only   # only reflect
  python memory_pipeline.py --synthesize-only  # only synthesize
"""

import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent


def run_reflect(apply: bool) -> bool:
    cmd = [sys.executable, str(_SCRIPTS_DIR / "reflect.py")]
    if apply:
        cmd.append("--apply")
    print(f"[reflect] {'APPLY' if apply else 'DRY-RUN'}")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def run_synthesize(apply: bool) -> bool:
    cmd = [sys.executable, str(_SCRIPTS_DIR / "synthesize.py")]
    if apply:
        cmd.append("--apply")
    print(f"[synthesize] {'APPLY' if apply else 'DRY-RUN'}")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="P2 memory pipeline (reflect → synthesize)")
    parser.add_argument("--apply", action="store_true", help="Actually write files")
    parser.add_argument("--reflect-only", action="store_true", help="Only run reflect")
    parser.add_argument("--synthesize-only", action="store_true", help="Only run synthesize")
    args = parser.parse_args()

    apply = args.apply
    reflect_only = args.reflect_only
    synthesize_only = args.synthesize_only

    if synthesize_only:
        ok = run_synthesize(apply)
    elif reflect_only:
        ok = run_reflect(apply)
    else:
        ok = run_reflect(apply)
        if ok:
            print()
            ok = run_synthesize(apply)

    if not ok:
        print("\nPipeline: some steps failed.")
    elif not apply:
        print(f"\nPipeline dry-run complete.  Run with --apply to write files.")
    else:
        print(f"\nPipeline complete: reflections written + active_lessons.md updated.")


if __name__ == "__main__":
    main()
