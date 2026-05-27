#!/usr/bin/env python3
"""
memory_pipeline.py — P2 + P5A shadow pipeline entry point.

Runs reflect → synthesize → quality_gate end-to-end.
Default: shadow/dry-run (writes preview files, NOT active_lessons.md).

Usage:
  python memory_pipeline.py                    # shadow run (preview only)
  python memory_pipeline.py --apply            # write reflections + active_lessons.md
  python memory_pipeline.py --reflect-only     # only reflect
  python memory_pipeline.py --synthesize-only  # only synthesize
  python memory_pipeline.py --quality-gate-only # only quality gate
  python memory_pipeline.py --reason "user_correction:不对"   # log trigger reason
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_MEMORY_DIR = Path.home() / ".hermes" / "profiles" / "me" / "memory"
_REPORT_PATH = _MEMORY_DIR / "models" / "latest_pipeline_report.json"


def run_reflect(apply: bool, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(_SCRIPTS_DIR / "reflect.py")]
    if apply:
        cmd.append("--apply")
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=60)


def run_synthesize(apply: bool, shadow: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(_SCRIPTS_DIR / "synthesize.py")]
    if apply:
        cmd.append("--apply")
    elif shadow:
        cmd.append("--candidate")  # write to active_lessons_candidate.md
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=60)


def run_quality_gate(capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(_SCRIPTS_DIR / "candidate_quality_gate.py")]
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=60)


def _write_report(reason: str, apply: bool, reflect_ok: bool, synth_ok: bool,
                  gate_ok: bool, candidates_before: int,
                  candidates_kept: int, candidates_rejected: int,
                  reflect_out: str, synth_out: str, errors: list) -> None:
    """Write pipeline report to models/latest_pipeline_report.json."""
    try:
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger_reason": reason,
            "mode": "apply" if apply else "shadow",
            "reflect_ok": reflect_ok,
            "synthesize_ok": synth_ok,
            "quality_gate_ok": gate_ok,
            "quality_gate_candidates_before": candidates_before,
            "quality_gate_candidates_kept": candidates_kept,
            "quality_gate_candidates_rejected": candidates_rejected,
            "errors": errors,
            "wrote_active_lessons": apply and reflect_ok and synth_ok,
        }
        _REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass  # non-fatal


def main():
    parser = argparse.ArgumentParser(description="P2+P5A+P5Q memory pipeline (reflect → synthesize → quality_gate)")
    parser.add_argument("--apply", action="store_true",
                        help="Write to active_lessons.md (default: shadow/preview only)")
    parser.add_argument("--reflect-only", action="store_true", help="Only run reflect")
    parser.add_argument("--synthesize-only", action="store_true", help="Only run synthesize")
    parser.add_argument("--quality-gate-only", action="store_true", help="Only run quality gate")
    parser.add_argument("--reason", type=str, default="",
                        help="Trigger reason (logged to report)")
    args = parser.parse_args()

    apply = args.apply
    reflect_only = args.reflect_only
    synthesize_only = args.synthesize_only
    gate_only = args.quality_gate_only
    reason = args.reason or "manual"

    reflect_ok = True
    synth_ok = True
    gate_ok = True
    candidates_before = 0
    candidates_kept = 0
    candidates_rejected = 0
    errors = []

    if gate_only:
        result = run_quality_gate()
        gate_ok = (result.returncode == 0)
        if not gate_ok:
            errors.append("quality_gate failed")
            print(result.stderr[:500], file=sys.stderr)
        else:
            print(result.stdout[:1000])
    elif synthesize_only:
        result = run_synthesize(apply, shadow=not apply)
        synth_ok = (result.returncode == 0)
        if not synth_ok:
            errors.append("synthesize failed")
            print(result.stderr[:500], file=sys.stderr)
        else:
            print(result.stdout[:1000])
            result_gate = run_quality_gate()
            gate_ok = (result_gate.returncode == 0)
            if gate_ok:
                print(result_gate.stdout[:1000])
            else:
                errors.append("quality_gate failed")
                print(result_gate.stderr[:500], file=sys.stderr)
    elif reflect_only:
        result = run_reflect(apply)
        reflect_ok = (result.returncode == 0)
        if not reflect_ok:
            errors.append("reflect failed")
            print(result.stderr[:500], file=sys.stderr)
        else:
            print(result.stdout[:1000])
    else:
        # Full pipeline: reflect → synthesize → quality_gate
        result = run_reflect(apply)
        reflect_ok = (result.returncode == 0)
        if not reflect_ok:
            errors.append("reflect failed")
            print(result.stderr[:500], file=sys.stderr)
        else:
            print(result.stdout[:1000])
            result2 = run_synthesize(apply, shadow=not apply)
            synth_ok = (result2.returncode == 0)
            if not synth_ok:
                errors.append("synthesize failed")
                print(result2.stderr[:500], file=sys.stderr)
            else:
                print(result2.stdout[:1000])
                result_gate = run_quality_gate()
                gate_ok = (result_gate.returncode == 0)
                if gate_ok:
                    print(result_gate.stdout[:1000])
                    # Extract quality gate stats from output
                    import re
                    m = re.search(r"Total:\s*(\d+).*?Kept:\s*(\d+).*?Rejected:\s*(\d+)",
                                  result_gate.stdout)
                    if m:
                        candidates_before = int(m.group(1))
                        candidates_kept = int(m.group(2))
                        candidates_rejected = int(m.group(3))
                else:
                    errors.append("quality_gate failed")
                    print(result_gate.stderr[:500], file=sys.stderr)

    _write_report(reason, apply, reflect_ok, synth_ok, gate_ok,
                  candidates_before, candidates_kept, candidates_rejected,
                  result.stdout[:2000] if 'result' in dir() else "",
                  result2.stdout[:2000] if 'result2' in dir() else "",
                  errors)

    if errors:
        print(f"\nPipeline: errors={errors}", file=sys.stderr)
        sys.exit(1)
    elif not apply:
        print(f"\nPipeline shadow complete. Report: {_REPORT_PATH}")
    else:
        print(f"\nPipeline complete: reflections + active_lessons.md updated.")


if __name__ == "__main__":
    main()
