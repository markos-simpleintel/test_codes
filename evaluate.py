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
            LIVE_CALLS.append(self)
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


def project_ceiling(cpu_samples, calls_achieved, ncpu, conc=None):
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

    # A group whose CPU barely rises between the idle baseline and peak load is
    # not paying a per-call cost - it is constant background work. Dividing its
    # peak by the call count would invent a per-call figure and project a
    # nonsense ceiling from it.
    #
    # The comparison is against genuinely idle samples (the second of sampling
    # before any call is placed, plus the drain after they end) rather than a
    # split of the loaded period - concurrency usually sits on a plateau, and a
    # percentile split of a plateau finds no contrast at all.
    flat = set()
    if conc:
        by_rel = {round(p["rel"], 1): p["calls"] for p in conc}
        peak_calls = max(p["calls"] for p in conc)
        idle, busy = {}, {}
        for smp in cpu_samples:
            n = by_rel.get(round(smp["rel"], 1), 0)
            bucket = idle if n == 0 else (busy if n >= peak_calls * 0.8 else None)
            if bucket is None:
                continue
            for g, v in smp["groups"].items():
                bucket.setdefault(g, []).append(v)

        for g in set(idle) & set(busy):
            if len(idle[g]) < 2 or len(busy[g]) < 2:
                continue
            base = statistics.fmean(idle[g])
            load = statistics.fmean(busy[g])
            if load < max(base * 1.5, base + 5):
                flat.add(g)

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
            "per_call_pct": None if g in flat else round(per_call, 2),
            "ceiling_pct": cap,
            "projected_calls": None if g in flat else int(cap / per_call),
            "headroom_pct": round(cap - p, 1),
            "scales_across_cores": g not in single_threaded,
            "constant_load": g in flat,
        })
    # Groups with no projection sort last; they impose a fixed tax rather than a
    # limit that arrives at some call count.
    return sorted(out, key=lambda r: (r["projected_calls"] is None,
                                      r["projected_calls"] or 0))


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
        "projection": project_ceiling(cpu.samples, peak_ours or len(connected), ncpu, conc),
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
            per_call = "constant" if p.get("constant_load") else str(p["per_call_pct"]) + "%"
            calls = "-" if p["projected_calls"] is None else str(p["projected_calls"])
            w(f"    {p['group']:<16}{str(p['peak_pct'])+'%':<10}"
              f"{per_call:<11}{str(p['ceiling_pct'])+'%':<10}"
              f"{calls:<9}{'yes' if p['scales_across_cores'] else 'NO - single core'}")

        constant = [p for p in r["projection"] if p.get("constant_load")]
        if constant:
            w("\n  'constant' means CPU did not rise with call count - a fixed tax on"
              " the box,")
            w("  not a per-call cost, so no call-count ceiling can be projected from it:")
            for p in constant:
                w(f"    {p['group']}: ~{p['peak_pct']}% of a core regardless of load")

        scaling = [p for p in r["projection"] if p["projected_calls"] is not None]
        if scaling:
            first = scaling[0]
            w(f"\n  First to run out: {first['group']} at roughly {first['projected_calls']} calls.")
            if not first["scales_across_cores"]:
                w("  It is single-threaded, so more cores will not move that number.")
            if r["calls"]["peak_concurrent_measured"] < 2:
                w("  Projected from a single call, so treat it as a direction, not a number -"
                  " re-run at higher concurrency to fit a real slope.")
    w("")
    return "\n".join(L)


LIVE_CALLS = []


def _release_calls():
    """Tell every call to stop before teardown reaches its thread joins.

    A wait thread exits only when its call's _stop_evt is set. Teardown joins
    two threads per call with a 2s timeout each, so calls that never received a
    DISCONNECTED callback used to cost two seconds apiece - minutes, at forty
    calls.
    """
    for call in LIVE_CALLS:
        try:
            call._stop_evt.set()
        except Exception:                                   # noqa: BLE001
            pass


def _supervise(driver, done, rec, expected_calls, stall_timeout, teardown_grace):
    """Wait for the run to be over, and refuse to wait forever for pjsua2.

    runner.main() runs on a worker thread so the report is not hostage to its
    teardown. Endpoint destruction deadlocks reliably at this call count, and
    killing the process is what has been costing us the numbers. The
    measurement is complete once the last call ends; what the SIP client does
    with its own mutexes afterwards is not part of the result.

    Returns why the run ended.
    """
    def ended():
        return sum(1 for e in list(rec.events) if e.get("kind") == "call_end")

    while True:
        if done.wait(2.0):
            return "runner returned"

        events = list(rec.events)
        last = events[-1]["t"] if events else None

        if expected_calls and ended() >= expected_calls:
            _release_calls()
            if done.wait(teardown_grace):
                return "runner returned"
            print(f"\n*** all {expected_calls} calls ended; pjsua2 teardown has not "
                  f"returned after {teardown_grace}s - reporting without it",
                  file=sys.stderr)
            return "teardown hung"

        if last is not None and time.time() - last > stall_timeout:
            idle = int(time.time() - last)
            _release_calls()
            print(f"\n*** no call activity for {idle}s - ending the run "
                  f"(raise --stall-timeout if calls are legitimately this quiet)",
                  file=sys.stderr)
            if done.wait(teardown_grace):
                return "runner returned after stall"
            return "stalled, teardown hung"

def _check_sip_port_free(wait_s=0):
    """A previous run still holding the SIP port fails deep inside pjsua2 as
    'bind() error: Address already in use', which says nothing about the cause.
    Check first and name it.

    A level that exits hard can hold the port for a moment after the process is
    gone, so a ladder waits rather than dropping the next rung.
    """
    import socket
    import config
    deadline = time.time() + wait_s
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("", config.LOCAL_SIP_PORT))
            return None
        except OSError:
            if time.time() >= deadline:
                return (f"SIP port {config.LOCAL_SIP_PORT} is in use - a previous run is "
                        f"probably still alive."
                        f"\n    ss -lunp | grep {config.LOCAL_SIP_PORT}"
                        f"\n    pkill -9 -f evaluate.py; pkill -9 -f runner.py")
            time.sleep(1.0)
        finally:
            probe.close()


def run_one(n, args, outdir):
    from metrics import RunMetrics
    import metrics as metrics_mod

    stem = outdir / f"{args.label}-{n}calls"
    # Stream to disk from the first event so an ugly exit costs at most the
    # last event rather than the entire run's timings.
    metrics_mod.RECORDER = RunMetrics(stream_path=f"{stem}.events.ndjson")
    rec = _install(n, args.gap)

    from monitors import CpuSampler, ChannelSampler
    # Streamed alongside the event trace, so a killed run keeps its resource
    # numbers too - which are the point of a concurrency test, not a footnote.
    cpu = CpuSampler(interval=args.cpu_interval, stream_path=f"{stem}.cpu.csv")
    chan = ChannelSampler(interval=args.channel_interval,
                          stream_path=f"{stem}.channels.csv")
    cpu.start()
    chan.start()

    import signal
    import threading

    def _on_sigint(_sig, _frm):
        # runner.main() catches KeyboardInterrupt and tears down cleanly; this
        # just makes sure its thread joins do not have to wait anyone out.
        _release_calls()
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass

    time.sleep(1.0)

    # runner.py's own entry point wraps main() in the file logger; calling
    # main() directly skips it and loses the per-call detail, which is where
    # the reason a call ended actually shows up.
    os.environ["RUNNER_LOG_FILE"] = str(outdir / f"{args.label}-{n}calls.runner.log")
    from run_logging import setup_run_logging
    import runner

    done = threading.Event()

    def _drive():
        try:
            with setup_run_logging():
                runner.main()
        except BaseException as e:                          # noqa: BLE001
            print(f"*** runner ended: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            done.set()

    t0 = time.time()
    driver = threading.Thread(target=_drive, name="runner", daemon=True)
    driver.start()

    try:
        end_reason = _supervise(driver, done, rec, n, args.stall_timeout,
                                args.teardown_grace)
    except KeyboardInterrupt:
        _release_calls()
        end_reason = "interrupted"

    wall = time.time() - t0
    LIVE_CALLS.clear()
    cpu.stop()
    chan.stop()
    cpu.close_stream()
    chan.close_stream()
    time.sleep(args.cpu_interval * 2)
    print(f"*** run ended: {end_reason}   ({wall:.0f}s)", file=sys.stderr)

    if cpu.error:
        print(f"*** CPU sampler error: {cpu.error}", file=sys.stderr)
    if not chan.available:
        print("*** asterisk CLI unavailable - channel counts skipped", file=sys.stderr)

    report = build_report(f"{args.label}-{n}", n, rec, cpu, chan,
                          os.cpu_count() or 1, wall)

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
    print(f"  wrote {stem}.{{txt,json,turns.csv,cpu.csv,events.ndjson,runner.log}}\n")
    if driver.is_alive():
        # Everything above is on disk. A stuck pjsua2 teardown is a client-side
        # hang with no bearing on the calls, and waiting it out is what has been
        # forcing runs to be killed before they could report.
        print("*** pjsua2 has not finished shutting down - exiting hard. The results "
              "above are complete; this is\n*** the SIP client's own teardown, not a "
              "call failure.", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
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
    ap.add_argument("--stall-timeout", type=int, default=120,
                    help="end the run after this many seconds with no call activity "
                         "(default 120; the per-turn wait can legitimately reach 60)")
    ap.add_argument("--teardown-grace", type=int, default=25,
                    help="seconds to let pjsua2 shut down after the last call "
                         "ends before writing the report and exiting anyway")
    ap.add_argument("--settle", type=int, default=30,
                    help="seconds between levels in a ladder")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    if os.name != "posix":
        sys.exit("This reads /proc and calls the asterisk CLI - run it on the PBX host.")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    busy = _check_sip_port_free(wait_s=20)
    if busy:
        sys.exit(f"\nERROR: {busy}\n")

    levels = [int(x) for x in str(args.calls).split(",") if x.strip()]
    if len(levels) > 1:
        print(f"*** ladder: {levels}   settle {args.settle}s between levels")
        print("*** each level runs in its own process so pjsua2 starts clean")
        here = Path(__file__).resolve()
        for i, n in enumerate(levels):
            os.system(f"{sys.executable} {here} --calls {n} --label {args.label} "
                      f"--out {args.out} --cpu-interval {args.cpu_interval} "
                      f"--channel-interval {args.channel_interval} "
                      f"--stall-timeout {args.stall_timeout} "
                      f"--teardown-grace {args.teardown_grace}"
                      + (f" --gap {args.gap}" if args.gap else ""))
            if i < len(levels) - 1:
                time.sleep(args.settle)
        print("\n*** ladder complete. Compare with:")
        print(f"    grep -H 'peak concurrent\\|lowest idle\\|First to run out' {args.out}/*.txt")
        return

    run_one(levels[0], args, outdir)


if __name__ == "__main__":
    main()
