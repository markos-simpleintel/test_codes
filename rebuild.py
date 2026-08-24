#!/usr/bin/env python3
"""Rebuild a run's report from its streamed event trace.

    python rebuild.py results/run4-40calls.events.ndjson

The trace is written as events happen, so it survives a kill. Everything
derived from it - concurrency, per-turn response percentiles, call outcomes -
can be recovered afterwards. CPU samples are only written at the end of a run,
so those are gone; the timings, which are the expensive part, are not.
"""

import json
import sys
from pathlib import Path

from metrics import RunMetrics
import evaluate


class _NoSamples:
    samples = []
    available = False
    error = None


def load(path):
    rec = RunMetrics()
    bad = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec.events.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1          # a kill can truncate the final line mid-write
    if rec.events:
        rec.t0 = min(e["t"] for e in rec.events)
    return rec, bad


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python rebuild.py <run>.events.ndjson")

    path = Path(sys.argv[1])
    if not path.exists():
        sys.exit(f"not found: {path}")

    rec, bad = load(path)
    if not rec.events:
        sys.exit(f"{path} has no usable events")
    if bad:
        print(f"*** skipped {bad} truncated line(s) at the end of the trace",
              file=sys.stderr)

    label = path.name.replace(".events.ndjson", "")
    wall = max(e["t"] for e in rec.events) - rec.t0
    calls = rec.calls()
    requested = max((c["call_id"] for c in calls), default=0)

    report = evaluate.build_report(label, requested, rec, _NoSamples(),
                                   _NoSamples(), 0, wall)

    stem = path.with_suffix("").with_suffix("")      # strip .events.ndjson
    stem = stem.parent / f"{label}-rebuilt"

    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    with open(f"{stem}.turns.csv", "w", encoding="utf-8") as f:
        f.write("call_id,turn,action_type,action,response_ms,remote_speech_ms,"
                "turn_total_ms,detected_by\n")
        for t in report["turns"]:
            f.write(f"{t['call_id']},{t.get('turn','')},{t.get('action_type','')},"
                    f"\"{t.get('action','')}\",{t.get('response_ms','')},"
                    f"{t.get('remote_speech_ms','')},{t.get('turn_total_ms','')},"
                    f"{t.get('detected_by','')}\n")

    text = evaluate.render(report)
    print(text)
    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write(text)

    print(f"  rebuilt from {len(rec.events)} events")
    print(f"  wrote {stem}.{{txt,json,turns.csv}}")
    print("  (CPU data is not in the trace - it is only written when a run "
          "finishes normally)\n")


if __name__ == "__main__":
    main()
