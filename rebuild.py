#!/usr/bin/env python3
"""Rebuild a run's report from its streamed event trace.

    python rebuild.py results/run4-40calls.events.ndjson

The trace is written as events happen, so it survives a kill. Everything
derived from it - concurrency, per-turn response percentiles, call outcomes -
can be recovered afterwards - including CPU and channel counts, which the
samplers stream to CSV alongside it.
"""

import json
import os
import sys
from pathlib import Path

from metrics import RunMetrics
import evaluate


class _Samples:
    """Stands in for a live sampler, filled from the CSV it streamed."""

    def __init__(self, samples=None, available=True):
        self.samples = samples or []
        self.available = available
        self.error = None


def load_cpu_csv(path):
    """CPU samples the run streamed. Group columns are read from the header, so
    this keeps working if the group list changes."""
    if not path.exists():
        return [], 0
    rows, ncpu = [], 0
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return [], 0
    header = lines[0].split(",")
    try:
        gi = header.index("idle_pct") + 1
    except ValueError:
        return [], 0
    names = header[gi:]
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) != len(header):
            continue                     # truncated final line from a kill
        try:
            ncpu = int(parts[1])
            rows.append({
                "rel": float(parts[0]),
                "busy_pct": float(parts[2]),
                "idle_pct": float(parts[3]),
                "groups": {n: float(v) for n, v in zip(names, parts[gi:]) if float(v) > 0},
                "counts": {},
            })
        except ValueError:
            continue
    return rows, ncpu


def load_channels_csv(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) != 2:
            continue
        try:
            rows.append({"rel": float(parts[0]), "channels": int(parts[1])})
        except ValueError:
            continue
    return rows


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

    # The samplers stream to CSV beside the trace, so a killed run keeps its
    # resource numbers as well as its timings.
    cpu_rows, ncpu = load_cpu_csv(path.parent / f"{label}.cpu.csv")
    chan_rows = load_channels_csv(path.parent / f"{label}.channels.csv")
    if not cpu_rows:
        print("*** no cpu.csv beside the trace - CPU sections will be empty",
              file=sys.stderr)

    report = evaluate.build_report(label, requested, rec,
                                   _Samples(cpu_rows),
                                   _Samples(chan_rows, bool(chan_rows)),
                                   ncpu or (os.cpu_count() or 1), wall)

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

    print(f"  rebuilt from {len(rec.events)} events"
          + (f", {len(cpu_rows)} cpu samples" if cpu_rows else "")
          + (f", {len(chan_rows)} channel samples" if chan_rows else ""))
    print(f"  wrote {stem}.{{txt,json,turns.csv}}\n")


if __name__ == "__main__":
    main()
