#!/usr/bin/env python3
"""Rebuild a run's report from its streamed event trace.

    python rebuild.py results/run4-40calls.events.ndjson

The trace is written as events happen, so it survives a kill. Everything
derived from it - concurrency, per-turn response percentiles, call outcomes -
can be recovered afterwards, including CPU and channel counts, which the
samplers stream to CSV alongside it.

Rebuilt rungs fold back into a ladder summary with:

    python evaluate.py --label ladder --summarize results/ladder-*calls.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

from metrics import RunMetrics
import evaluate
import monitors


class _Samples:
    """Stands in for a live sampler, filled from the CSV it streamed."""

    def __init__(self, samples=None, available=True, spawn_counts=None):
        self.samples = samples or []
        self.available = available
        self.spawn_counts = spawn_counts or {}
        # The CSV carries untracked CPU as a total but not the per-process
        # breakdown behind it, so a rebuilt run reports the figure without the
        # names. Only a live run can list those.
        self.other_top = []
        self.capacity_pct = None
        self.error = None


def load_cpu_csv(path):
    """CPU samples the run streamed.

    Column names come from the header rather than from a fixed layout, so this
    keeps working when the group list changes, and tolerates traces written
    before the per-group process counts existed.
    """
    if not path.exists():
        return [], 0, {}
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return [], 0, {}

    header = lines[0].split(",")
    if "idle_pct" not in header:
        return [], 0, {}
    idx = {name: i for i, name in enumerate(header)}
    # Anything that is not a known fixed column and not a count column is a
    # process group. Matching by name rather than by position means a trace
    # written before steal and untracked existed still reads correctly.
    fixed = set(monitors.FIXED_COLUMNS)
    pct_names = [n for n in header
                 if n not in fixed and not n.startswith(("n_", "spawned_"))]

    def num(parts, name, default=0.0):
        i = idx.get(name)
        if i is None or i >= len(parts):
            return default
        try:
            return float(parts[i])
        except ValueError:
            return default

    rows, ncpu, spawned = [], 0, {}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) != len(header):
            continue                     # truncated final line from a kill
        try:
            ncpu = int(parts[idx["ncpu"]]) if "ncpu" in idx else ncpu
            groups, counts = {}, {}
            for n in pct_names:
                v = num(parts, n)
                if v > 0:
                    groups[n] = v
                if "n_" + n in idx:
                    counts[n] = int(num(parts, "n_" + n))
                if "spawned_" + n in idx:
                    # Monotonic, so the largest value seen is the run total.
                    spawned[n] = max(spawned.get(n, 0), int(num(parts, "spawned_" + n)))
            rows.append({
                "rel": num(parts, "rel_s"),
                "busy_pct": num(parts, "busy_pct"),
                "idle_pct": num(parts, "idle_pct"),
                "steal_pct": num(parts, "steal_pct"),
                "untracked_pct": num(parts, "untracked_pct"),
                "groups": groups,
                "counts": counts,
            })
        except (ValueError, IndexError, KeyError):
            continue
    return rows, ncpu, {k: v for k, v in spawned.items() if v}


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
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", help="<run>.events.ndjson")
    ap.add_argument("--slo", type=float, default=10000,
                    help="wait in ms above which a turn counts as too slow")
    ap.add_argument("--silence-timer", type=float, default=2.0,
                    help="seconds of configured PBX silence timer to report separately")
    args = ap.parse_args()

    path = Path(args.trace)
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
    cpu_rows, ncpu, spawned = load_cpu_csv(path.parent / f"{label}.cpu.csv")
    chan_rows = load_channels_csv(path.parent / f"{label}.channels.csv")
    if not cpu_rows:
        print("*** no cpu.csv beside the trace - CPU sections will be empty",
              file=sys.stderr)

    report = evaluate.build_report(
        label, requested, rec,
        _Samples(cpu_rows, spawn_counts=spawned),
        _Samples(chan_rows, bool(chan_rows)),
        ncpu or (os.cpu_count() or 1), wall,
        silence_timer_ms=args.silence_timer * 1000.0,
        slo_ms=args.slo,
    )

    stem = path.parent / f"{label}-rebuilt"

    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    with open(f"{stem}.turns.csv", "w", encoding="utf-8") as f:
        f.write("call_id,turn,action_type,action,response_ms,remote_speech_ms,"
                "turn_total_ms,detected_by,calls_up,inflight,tx_packets,tx_seconds\n")
        for t in report["turns"]:
            f.write(f"{t['call_id']},{t.get('turn','')},{t.get('action_type','')},"
                    f"\"{t.get('action','')}\",{t.get('response_ms','')},"
                    f"{t.get('remote_speech_ms','')},{t.get('turn_total_ms','')},"
                    f"{t.get('detected_by','')},{t.get('calls_up','')},"
                    f"{t.get('inflight','')},{t.get('tx_packets','')},"
                    f"{t.get('tx_seconds','')}\n")

    evaluate.write_calls_csv(f"{stem}.calls.csv", report)

    text = evaluate.render(report)
    # A trace that stops early cannot distinguish a call the system abandoned
    # from one that was still going when the recording stopped. Both look
    # identical here, so the verdict is stated for what it is.
    unfinished = sum(1 for c in calls if c["connected"] and not c["ended"])
    if unfinished:
        text += (f"\n  NOTE: {unfinished} call(s) have no end event - the trace stops "
                 f"while they were still\n  running. They are counted as failures above, "
                 f"but a trace that was cut short looks\n  exactly the same as a call the "
                 f"system stopped answering. Treat those verdicts as\n  'unknown', and "
                 f"read the timings, concurrency and CPU, which are unaffected.\n")
    print(text)
    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write(text)

    print(f"  rebuilt from {len(rec.events)} events"
          + (f", {len(cpu_rows)} cpu samples" if cpu_rows else "")
          + (f", {len(chan_rows)} channel samples" if chan_rows else ""))
    print(f"  wrote {stem}.{{txt,json,turns.csv}}\n")
    print(f"  to fold this rung into a ladder summary, copy it over the original:")
    print(f"    cp {stem}.json {path.parent / (label + '.json')}\n")


if __name__ == "__main__":
    main()
