#!/usr/bin/env python3
"""One command: place N concurrent calls, measure everything, report.

    python evaluate.py --calls 40
    python evaluate.py --calls 20,40,60 --label ceiling-hunt

Reports concurrency actually achieved, a full CPU breakdown by process group,
per-turn response-time percentiles, and a projection of which process runs out
of headroom first and at roughly what call count.

Instrumentation is done by subclassing the existing call classes and swapping
them in before runner imports them - no edits to call_session.py or runner.py,
so this cannot change how the test itself behaves.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path


# --- must happen before runner/call_session bind their imports --------------

def _install(num_calls, gap_ms):
    import config
    config.NUM_CALLS = num_calls
    if gap_ms is not None:
        config.CALL_START_GAP_MS = gap_ms

    import call_session
    from metrics import RECORDER

    base_call = call_session.MyCall
    base_tap = call_session.RemoteTap

    class InstrumentedTap(base_tap):
        def onFrameReceived(self, frame):
            had_voice = self.owner.remote_seen_voice
            super().onFrameReceived(frame)
            # The moment the far end starts talking ends the caller's wait.
            if not had_voice and self.owner.remote_seen_voice:
                RECORDER.record("remote_first_voice",
                                call_id=self.owner.call_id,
                                turn=self.owner.action_idx)

    class InstrumentedCall(base_call):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            # Both turn-end paths can run for one turn - the AMI branch calls
            # finish_wait_and_start_next, and the RTP branch returns through
            # wait_for_remote_turn_end - so a turn is only closed once.
            self._closed_turns = set()

        def _close_turn(self, turn, source, label):
            if turn in self._closed_turns:
                return
            self._closed_turns.add(turn)
            RECORDER.record("remote_turn_end", call_id=self.call_id,
                            turn=turn, source=source, label=label)

        def start(self):
            RECORDER.record("call_start", call_id=self.call_id)
            return super().start()

        def onCallState(self, prm):
            try:
                state = self.getInfo().stateText
            except Exception:                       # noqa: BLE001
                state = "?"
            super().onCallState(prm)
            if state == "CONFIRMED":
                RECORDER.record("call_connected", call_id=self.call_id)
            elif state == "DISCONNECTED":
                RECORDER.record("call_end", call_id=self.call_id, reason=state)

        def onCallMediaState(self, prm):
            super().onCallMediaState(prm)
            if self.media_ready:
                RECORDER.record("call_media_ready", call_id=self.call_id)

        def start_next_action(self):
            idx = self.action_idx
            if idx < len(self.actions):
                a_type, a_val = self.actions[idx]
                RECORDER.record("action_start", call_id=self.call_id, turn=idx,
                                action_type=a_type, action=str(a_val)[-40:])
            return super().start_next_action()

        def on_action_complete(self, expected_idx=None):
            # A duplicate onEof2 is discarded by the base guard, so only record
            # the completions that actually advanced the script.
            before = self.action_idx
            result = super().on_action_complete(expected_idx=expected_idx)
            if self.action_idx != before:
                RECORDER.record("action_end", call_id=self.call_id, turn=before)
            return result

        def finish_wait_and_start_next(self, source, label):
            self._close_turn(self.action_idx, source, label)
            return super().finish_wait_and_start_next(source, label)

        def wait_for_remote_turn_end(self, timeout_secs, label):
            # The RTP-silence path calls start_next_action() directly instead of
            # going through finish_wait_and_start_next, so close the turn here.
            before = self.action_idx
            result = super().wait_for_remote_turn_end(timeout_secs, label)
            if self.action_idx != before:
                self._close_turn(before, "silence", label)
            return result

    call_session.MyCall = InstrumentedCall
    call_session.RemoteTap = InstrumentedTap
    return RECORDER


# --- reporting ---------------------------------------------------------------

def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    if q <= 0:
        return s[0]
    if q >= 1:
        return s[-1]
    return s[min(int(q * len(s) + 0.9999), len(s)) - 1]


def summarize(values):
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "p50": round(pct(values, 0.50), 1),
        "p90": round(pct(values, 0.90), 1),
        "p95": round(pct(values, 0.95), 1),
        "p99": round(pct(values, 0.99), 1),
        "max": round(max(values), 1),
        "mean": round(statistics.fmean(values), 1),
    }


def project_ceiling(cpu_samples, calls_achieved, ncpu):
    """Which group runs out of room first, and roughly when.

    A multi-threaded process can use every core, so its limit is the box. A
    single-threaded one is capped at 100% however many cores there are - and
    that is the one that bites first, silently, because the machine still looks
    like it has capacity left.
    """
    if not cpu_samples or not calls_achieved:
        return []

    peak = {}
    for s in cpu_samples:
        for g, v in s["groups"].items():
            if v > peak.get(g, 0):
                peak[g] = v

    single_threaded = {"pbx_receiver", "test_harness"}
    out = []
    for g, p in sorted(peak.items(), key=lambda kv: -kv[1]):
        per_call = p / calls_achieved if calls_achieved else 0
        if per_call <= 0:
            continue
        cap = 100.0 if g in single_threaded else 100.0 * ncpu
        out.append({
            "group": g,
            "peak_pct": round(p, 1),
            "per_call_pct": round(per_call, 2),
            "ceiling_pct": cap,
            "projected_calls": int(cap / per_call),
            "headroom_pct": round(cap - p, 1),
            "scales_across_cores": g not in single_threaded,
        })
    return sorted(out, key=lambda r: r["projected_calls"])


def build_report(label, requested, rec, cpu, chan, ncpu, wall_s):
    turns = rec.turns()
    calls = rec.calls()
    conc = rec.concurrency_timeline()

    connected = [c for c in calls if c["connected"]]
    peak_ours = max((p["calls"] for p in conc), default=0)
    peak_ast = max((s["channels"] for s in chan.samples), default=None)

    resp = [t["response_ms"] for t in turns if t.get("response_ms") is not None]
    total = [t["turn_total_ms"] for t in turns if t.get("turn_total_ms") is not None]

    by_turn = {}
    for t in turns:
        if t.get("response_ms") is None:
            continue
        by_turn.setdefault(t["turn"], []).append(t["response_ms"])

    cpu_peak, cpu_mean = {}, {}
    for s in cpu.samples:
        for g, v in s["groups"].items():
            cpu_peak[g] = max(cpu_peak.get(g, 0), v)
            cpu_mean.setdefault(g, []).append(v)
    cpu_mean = {g: round(statistics.fmean(v), 1) for g, v in cpu_mean.items()}

    idles = [s["idle_pct"] for s in cpu.samples]
    saturated = [s for s in cpu.samples if s["idle_pct"] <= 1.0]

    return {
        "label": label,
        "requested_calls": requested,
        "wall_seconds": round(wall_s, 1),
        "cores": ncpu,
        "calls": {
            "connected": len(connected),
            "peak_concurrent_measured": peak_ours,
            "peak_channels_asterisk": peak_ast,
            "completed_turns": len(turns),
        },
        "latency_ms": {
            "response": summarize(resp),
            "turn_total": summarize(total),
            "by_turn": {str(k): summarize(v) for k, v in sorted(by_turn.items())},
        },
        "cpu": {
            "peak_by_group": {k: round(v, 1) for k, v in sorted(cpu_peak.items(), key=lambda kv: -kv[1])},
            "mean_by_group": cpu_mean,
            "min_idle_pct": round(min(idles), 1) if idles else None,
            "saturated_samples": len(saturated),
            "total_samples": len(cpu.samples),
            "box_capacity_pct": 100 * ncpu,
        },
        "projection": project_ceiling(cpu.samples, peak_ours or len(connected), ncpu),
        "concurrency_timeline": conc,
        "channel_timeline": chan.samples,
        "cpu_timeline": cpu.samples,
        "turns": turns,
        "call_records": calls,
    }


def render(r):
    L = []
    w = L.append
    c, lat, cpu = r["calls"], r["latency_ms"], r["cpu"]

    w(f"\n{'=' * 74}")
    w(f"  {r['label']}   requested {r['requested_calls']} calls   "
      f"{r['wall_seconds']}s   {r['cores']} cores")
    w("=" * 74)

    w("\nCONCURRENCY")
    w(f"  connected                {c['connected']} / {r['requested_calls']}")
    w(f"  peak concurrent (ours)   {c['peak_concurrent_measured']}")
    w(f"  peak channels (asterisk) {c['peak_channels_asterisk'] if c['peak_channels_asterisk'] is not None else 'n/a'}")
    w(f"  completed turns          {c['completed_turns']}")

    w("\nRESPONSE TIME  (our audio ends -> far end starts speaking)")
    s = lat["response"]
    if s["count"]:
        w(f"  samples {s['count']}   p50 {s['p50']}ms   p90 {s['p90']}ms   "
          f"p95 {s['p95']}ms   p99 {s['p99']}ms   max {s['max']}ms")
    else:
        w("  no samples - the far end never produced audio we could detect")

    if lat["by_turn"]:
        w("\n  per turn:")
        w(f"    {'turn':<6}{'n':<6}{'p50':<10}{'p95':<10}{'max':<10}")
        for k, v in lat["by_turn"].items():
            w(f"    {k:<6}{v['count']:<6}{str(v['p50'])+'ms':<10}"
              f"{str(v['p95'])+'ms':<10}{str(v['max'])+'ms':<10}")

    w(f"\nCPU  (100% = one core; this box tops out at {cpu['box_capacity_pct']}%)")
    w(f"    {'group':<16}{'peak':<12}{'mean':<12}")
    for g, v in cpu["peak_by_group"].items():
        w(f"    {g:<16}{str(v)+'%':<12}{str(cpu['mean_by_group'].get(g, 0))+'%':<12}")
    w(f"  lowest idle              {cpu['min_idle_pct']}%")
    w(f"  saturated samples        {cpu['saturated_samples']} of {cpu['total_samples']}"
      f"  ({'box ran out of CPU' if cpu['saturated_samples'] else 'headroom remained'})")

    if r["projection"]:
        w("\nWHERE IT RUNS OUT")
        w(f"    {'group':<16}{'peak':<10}{'per call':<11}{'ceiling':<10}{'~calls':<9}scales?")
        for p in r["projection"]:
            w(f"    {p['group']:<16}{str(p['peak_pct'])+'%':<10}"
              f"{str(p['per_call_pct'])+'%':<11}{str(p['ceiling_pct'])+'%':<10}"
              f"{p['projected_calls']:<9}{'yes' if p['scales_across_cores'] else 'NO - single core'}")
        first = r["projection"][0]
        w(f"\n  First to run out: {first['group']} at roughly {first['projected_calls']} calls.")
        if not first["scales_across_cores"]:
            w("  It is single-threaded, so more cores will not move that number.")
    w("")
    return "\n".join(L)


def run_one(n, args, outdir):
    from metrics import RunMetrics
    import metrics as metrics_mod

    metrics_mod.RECORDER = RunMetrics()
    rec = _install(n, args.gap)

    from monitors import CpuSampler, ChannelSampler
    cpu = CpuSampler(interval=args.cpu_interval)
    chan = ChannelSampler(interval=args.channel_interval)
    cpu.start()
    chan.start()
    time.sleep(1.0)

    import runner
    t0 = time.time()
    try:
        runner.main()
    finally:
        wall = time.time() - t0
        cpu.stop()
        chan.stop()
        time.sleep(args.cpu_interval * 2)

    if cpu.error:
        print(f"*** CPU sampler error: {cpu.error}", file=sys.stderr)
    if not chan.available:
        print("*** asterisk CLI unavailable - channel counts skipped", file=sys.stderr)

    report = build_report(f"{args.label}-{n}", n, rec, cpu, chan,
                          os.cpu_count() or 1, wall)

    stem = outdir / f"{args.label}-{n}calls"
    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    rec.write_ndjson(f"{stem}.events.ndjson")

    with open(f"{stem}.turns.csv", "w", encoding="utf-8") as f:
        f.write("call_id,turn,action_type,action,response_ms,remote_speech_ms,turn_total_ms,detected_by\n")
        for t in report["turns"]:
            f.write(f"{t['call_id']},{t.get('turn','')},{t.get('action_type','')},"
                    f"\"{t.get('action','')}\",{t.get('response_ms','')},"
                    f"{t.get('remote_speech_ms','')},{t.get('turn_total_ms','')},"
                    f"{t.get('detected_by','')}\n")

    with open(f"{stem}.cpu.csv", "w", encoding="utf-8") as f:
        groups = sorted({g for s in cpu.samples for g in s["groups"]})
        f.write("rel_s,idle_pct," + ",".join(groups) + "\n")
        for s in cpu.samples:
            f.write(f"{s['rel']},{s['idle_pct']}," +
                    ",".join(str(s["groups"].get(g, 0)) for g in groups) + "\n")

    text = render(report)
    print(text)
    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  wrote {stem}.{{txt,json,turns.csv,cpu.csv,events.ndjson}}\n")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", default="40",
                    help="call count, or comma-separated ladder e.g. 20,40,60")
    ap.add_argument("--label", default="run")
    ap.add_argument("--gap", type=int, default=None,
                    help="ms between call starts (default: leave config alone)")
    ap.add_argument("--cpu-interval", type=float, default=0.5)
    ap.add_argument("--channel-interval", type=float, default=2.0)
    ap.add_argument("--settle", type=int, default=30,
                    help="seconds between levels in a ladder")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    if os.name != "posix":
        sys.exit("This reads /proc and calls the asterisk CLI - run it on the PBX host.")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    levels = [int(x) for x in str(args.calls).split(",") if x.strip()]
    if len(levels) > 1:
        print(f"*** ladder: {levels}   settle {args.settle}s between levels")
        print("*** each level runs in its own process so pjsua2 starts clean")
        here = Path(__file__).resolve()
        for i, n in enumerate(levels):
            os.system(f"{sys.executable} {here} --calls {n} --label {args.label} "
                      f"--out {args.out} --cpu-interval {args.cpu_interval} "
                      f"--channel-interval {args.channel_interval}"
                      + (f" --gap {args.gap}" if args.gap else ""))
            if i < len(levels) - 1:
                time.sleep(args.settle)
        print("\n*** ladder complete. Compare with:")
        print(f"    grep -H 'peak concurrent\\|lowest idle\\|First to run out' {args.out}/*.txt")
        return

    run_one(levels[0], args, outdir)


if __name__ == "__main__":
    main()
