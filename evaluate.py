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
import bisect
import glob
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
    if len(s) == 1:
        return round(s[0], 1)
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 1)


def summarize(values):
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "p50": pct(values, 0.50),
        "p90": pct(values, 0.90),
        "p95": pct(values, 0.95),
        "p99": pct(values, 0.99),
        "mean": round(statistics.fmean(values), 1),
        "max": round(max(values), 1),
    }


def ms(v, width=0):
    """Formats a millisecond figure, or a dash when there is nothing to show."""
    text = "-" if v is None else f"{v:.0f}ms"
    return f"{text:<{width}}" if width else text


def fit_line(xs, ys):
    """Least-squares slope and intercept, with R2 so a bad fit is visible.

    Dividing a peak by a call count assumes the line passes through the origin,
    which is exactly what a process with a fixed startup cost does not do.
    Fitting both terms separates the fixed tax from the per-call cost instead of
    blending them into one misleading average.
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:                       # no spread in x - nothing to fit
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in pairs)
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return {
        "slope": round(slope, 4),
        "intercept": round(intercept, 2),
        "r2": round(r2, 3) if r2 is not None else None,
        "n": len(pairs),
        "x_min": min(xs),
        "x_max": max(xs),
    }


def series_at(timeline, key, tolerance=1.5):
    """Look a timeline value up by time, tolerating clock skew between sources.

    The CPU sampler and the event recorder start a moment apart, so their
    relative clocks do not line up exactly. Matching on a rounded key silently
    dropped most pairs; nearest-within-tolerance keeps them.
    """
    points = sorted((p["rel"], p[key]) for p in timeline)
    if not points:
        return lambda _rel: None
    rels = [p[0] for p in points]

    def at(rel):
        i = bisect.bisect_left(rels, rel)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(points):
                d = abs(points[j][0] - rel)
                if d <= tolerance and (best is None or d < best[0]):
                    best = (d, points[j][1])
        return best[1] if best else None
    return at


# A process that can only ever use one core is the one that bites first, and it
# bites silently: the box still shows idle capacity while that process is pinned.
SINGLE_THREADED = {"pbx_receiver", "test_harness"}


def project_ceiling(cpu_samples, ncpu, conc=None):
    """Which process group runs out of room first, and at how many calls.

    Concurrency is not constant during a run - calls start together and drain as
    they finish - so each CPU sample sits at a different call count. That spread
    is enough to fit CPU against concurrency and read the fixed and per-call
    parts off separately, rather than dividing a peak by a call count and hoping
    the process has no startup cost.
    """
    if not cpu_samples:
        return []

    calls_at = series_at(conc or [], "calls")
    peak, series = {}, {}
    for s in cpu_samples:
        n = calls_at(s["rel"])
        for g, v in s["groups"].items():
            peak[g] = max(peak.get(g, 0), v)
            if n is not None:
                series.setdefault(g, ([], []))
                series[g][0].append(n)
                series[g][1].append(v)

    out = []
    for g, p in sorted(peak.items(), key=lambda kv: -kv[1]):
        cap = 100.0 if g in SINGLE_THREADED else 100.0 * ncpu
        xs, ys = series.get(g, ([], []))
        fit = fit_line(xs, ys)
        projected, per_call, fixed, r2 = None, None, None, None
        if fit:
            per_call, fixed, r2 = fit["slope"], fit["intercept"], fit["r2"]
            # A slope indistinguishable from flat means this group is a fixed tax
            # on the box, not a per-call cost - no call count follows from it.
            if per_call > 0.05 and (r2 is None or r2 >= 0.3):
                projected = int((cap - fixed) / per_call)
                if projected < 1:
                    projected = None
        out.append({
            "group": g,
            "peak_pct": round(p, 1),
            "per_call_pct": round(per_call, 2) if per_call is not None else None,
            "fixed_pct": round(fixed, 1) if fixed is not None else None,
            "fit_r2": r2,
            "ceiling_pct": cap,
            "projected_calls": projected,
            "headroom_pct": round(cap - p, 1),
            "scales_across_cores": g not in SINGLE_THREADED,
        })
    return sorted(out, key=lambda r: (r["projected_calls"] is None,
                                      r["projected_calls"] or 0))


def build_report(label, requested, rec, cpu, chan, ncpu, wall_s,
                 silence_timer_ms=0.0, slo_ms=None):
    turns = rec.annotate()
    calls = rec.calls()
    outcomes = rec.outcomes(turns)
    conc = rec.concurrency_timeline()
    flight = rec.inflight_timeline(turns=turns)

    connected = [c for c in calls if c["connected"]]
    peak_ours = max((p["calls"] for p in conc), default=0)
    peak_ast = max((s["channels"] for s in chan.samples), default=None)
    peak_flight = max((p["inflight"] for p in flight), default=0)
    mean_flight = (round(statistics.fmean([p["inflight"] for p in flight]), 1)
                   if flight else 0)

    answered = [t for t in turns if t.get("response_ms") is not None]
    resp = [t["response_ms"] for t in answered]
    # The configured silence timer is a constant the PBX waits out on every turn
    # before it begins working at all. Leaving it in makes a doubling of the real
    # work look like a 60% rise, so it is reported apart from it.
    pipeline = [max(0.0, r - silence_timer_ms) for r in resp]
    total = [t["turn_total_ms"] for t in turns if t.get("turn_total_ms") is not None]

    by_turn = {}
    for t in answered:
        by_turn.setdefault(t["turn"], []).append(t["response_ms"])

    # Every answered turn is one (load, wait) observation. Pooled, these describe
    # the curve far more densely than one point per rung of a ladder.
    load_fit = fit_line([t.get("inflight") for t in answered], resp)
    calls_fit = fit_line([t.get("calls_up") for t in answered], resp)

    verdicts = {}
    for o in outcomes:
        verdicts[o["verdict"]] = verdicts.get(o["verdict"], 0) + 1
    failed = sum(v for k, v in verdicts.items() if k != "completed")

    detected = {}
    for t in turns:
        if t.get("detected_by"):
            detected[t["detected_by"]] = detected.get(t["detected_by"], 0) + 1

    cpu_peak, cpu_mean = {}, {}
    for s in cpu.samples:
        for g, v in s["groups"].items():
            cpu_peak[g] = max(cpu_peak.get(g, 0), v)
            cpu_mean.setdefault(g, []).append(v)
    cpu_mean = {g: round(statistics.fmean(v), 1) for g, v in cpu_mean.items()}

    # Idle is summed across cores, so on a 4-core box it runs to 400 and "16%
    # idle" means 4% of the machine is free, not 16%. Both are reported, because
    # the raw figure read as reassuring when it was nearly the opposite. The
    # saturation mark sits at 5% of the box rather than a quarter of one percent.
    idles = [s["idle_pct"] for s in cpu.samples]
    capacity = 100.0 * ncpu
    saturated = [s for s in cpu.samples if s["idle_pct"] <= 0.05 * capacity]

    return {
        "label": label,
        "requested_calls": requested,
        "wall_seconds": round(wall_s, 1),
        "cores": ncpu,
        "silence_timer_ms": silence_timer_ms,
        "slo_ms": slo_ms,
        "calls": {
            "connected": len(connected),
            "peak_concurrent_measured": peak_ours,
            "peak_channels_asterisk": peak_ast,
            "peak_inflight": peak_flight,
            "mean_inflight": mean_flight,
            "completed_turns": len(turns),
            "answered_turns": len(answered),
            # How far into the script each call actually got. A call that ends
            # after five turns of a fifteen-turn conversation is a failure the
            # per-call verdict cannot see: every turn it did get was answered,
            # so it looks completed. This is the number that shows it.
            "turns_per_call": summarize([o["turns_answered"] for o in outcomes
                                         if o["turns_answered"]]),
        },
        "outcomes": {
            "by_verdict": verdicts,
            "failed": failed,
            "detected_by": detected,
            "per_call": outcomes,
        },
        "latency_ms": {
            "response": summarize(resp),
            "pipeline": summarize(pipeline),
            "turn_total": summarize(total),
            "by_turn": {str(k): summarize(v) for k, v in sorted(by_turn.items())},
            "slo_breaches": sum(1 for r in resp if slo_ms and r > slo_ms),
            "vs_inflight": load_fit,
            "vs_calls_up": calls_fit,
        },
        "cpu": {
            "peak_by_group": {k: round(v, 1) for k, v
                              in sorted(cpu_peak.items(), key=lambda kv: -kv[1])},
            "mean_by_group": cpu_mean,
            "processes_started": dict(getattr(cpu, "spawn_counts", {}) or {}),
            "min_idle_pct": round(min(idles), 1) if idles else None,
            "min_idle_of_box_pct": round(min(idles) / ncpu, 1) if idles else None,
            "saturated_samples": len(saturated),
            "total_samples": len(cpu.samples),
            "box_capacity_pct": 100 * ncpu,
        },
        "projection": project_ceiling(cpu.samples, ncpu, conc),
        "concurrency_timeline": conc,
        "inflight_timeline": flight,
        "channel_timeline": chan.samples,
        "cpu_timeline": cpu.samples,
        "turns": turns,
        "call_records": calls,
    }


VERDICT_TEXT = {
    "never_connected": "the INVITE never reached a connected call",
    "no_media": "connected, but no audio path was ever established",
    "no_response_at_all": "connected and asked, never got a single answer back",
    "abandoned_mid_call": "answered for a while, then stopped answering",
}


def render(r):
    L = []
    w = L.append
    c, lat, cpu = r["calls"], r["latency_ms"], r["cpu"]
    out = r["outcomes"]

    w("\n" + "=" * 76)
    w(f"  {r['label']}   requested {r['requested_calls']} calls   "
      f"{r['wall_seconds']}s   {r['cores']} cores")
    w("=" * 76)

    w("\nDID IT WORK")
    w(f"  calls completed          {out['by_verdict'].get('completed', 0)} "
      f"/ {r['requested_calls']}")
    for v, n in sorted(out["by_verdict"].items(), key=lambda kv: -kv[1]):
        if v != "completed":
            w(f"  {v:<24} {n:<5} {VERDICT_TEXT.get(v, '')}")
    w(f"  turns answered           {c['answered_turns']} of {c['completed_turns']} asked")
    tpc = c.get("turns_per_call") or {}
    if tpc.get("count"):
        w(f"  turns per call           median {tpc['p50']:.0f}   "
          f"best {tpc['max']:.0f}   ({tpc['count']} calls)")
        w("     How far into the script each call got before it ended. Calls that")
        w("     end early still show every turn answered, so this is where a")
        w("     conversation breaking down shows up and the verdict above does not.")
    if r.get("slo_ms"):
        w(f"  waits over {r['slo_ms'] / 1000:.1f}s          "
          f"{lat['slo_breaches']} of {c['answered_turns']}")
    if out["detected_by"]:
        w(f"  turn end detected by     "
          + "  ".join(f"{k}={v}" for k, v in sorted(out["detected_by"].items())))
        w("     'ami' is the PBX telling us it is ready; 'silence' is our own fallback")
        w("     guess. Fallbacks rising with load means the signal itself is slipping.")

    w("\nHOW MUCH LOAD WAS ACTUALLY APPLIED")
    w(f"  calls up (peak)          {c['peak_concurrent_measured']}")
    w(f"  asterisk channels (peak) "
      f"{c['peak_channels_asterisk'] if c['peak_channels_asterisk'] is not None else 'n/a'}")
    w(f"  requests in flight       {c['peak_inflight']} peak   {c['mean_inflight']} mean")
    w("     A call sitting in silence costs a channel and a VAD process. A call")
    w("     waiting on an answer costs speech recognition, an LLM turn and speech")
    w("     synthesis. Only the second is load, so 'requests in flight' is the")
    w("     number that decides where this system tops out.")

    w("\nCALLER WAIT  (our audio stops -> the system starts speaking)")
    s = lat["response"]
    if s["count"]:
        w(f"  samples {s['count']}   p50 {ms(s['p50'])}   p90 {ms(s['p90'])}   "
          f"p95 {ms(s['p95'])}   p99 {ms(s['p99'])}   max {ms(s['max'])}")
        if r.get("silence_timer_ms"):
            pl = lat["pipeline"]
            w(f"\n  {r['silence_timer_ms']:.0f}ms of that is the configured PBX silence timer,")
            w("  which is the same at every load. The part that actually does work:")
            w(f"  p50 {ms(pl['p50'])}   p95 {ms(pl['p95'])}   max {ms(pl['max'])}")
    else:
        w("  no turns were answered")

    if lat["by_turn"]:
        w("\n  per turn:")
        w(f"    {'turn':<6}{'n':<6}{'p50':<11}{'p95':<11}{'max':<11}")
        rows = sorted(lat["by_turn"].items(), key=lambda kv: int(kv[0]))
        for k, v in rows:
            w(f"    {k:<6}{v['count']:<6}{ms(v['p50'], 11)}{ms(v['p95'], 11)}{ms(v['max'], 11)}")
        if len(rows) > 1 and rows[-1][1]["count"] < rows[0][1]["count"]:
            w("     Calls drop out as the run goes on, so later turns ran at lower")
            w("     concurrency. A p50 that falls down this column is load easing off,")
            w("     not the system speeding up.")

    fit = lat.get("vs_inflight")
    if fit:
        w("\nHOW WAIT SCALES WITH LOAD  (every answered turn, not just averages)")
        w(f"  wait = {fit['intercept']:.0f}ms + {fit['slope']:.0f}ms per request in flight"
          f"   (R2 {fit['r2']}, {fit['n']} turns, {fit['x_min']}-{fit['x_max']} in flight)")
        if fit["r2"] is not None and fit["r2"] < 0.3:
            w("  R2 is low, so load does not explain the spread here - something other")
            w("  than concurrency is driving these waits.")

    w(f"\nCPU  (100% = one core; this box tops out at {cpu['box_capacity_pct']}%)")
    w(f"    {'group':<16}{'peak':<11}{'mean':<11}{'processes started':<18}")
    for g, v in cpu["peak_by_group"].items():
        started = cpu["processes_started"].get(g)
        w(f"    {g:<16}{str(v) + '%':<11}{str(cpu['mean_by_group'].get(g, '?')) + '%':<11}"
          f"{(str(started) if started else '-'):<18}")
    w(f"  lowest idle              {cpu.get('min_idle_of_box_pct')}% of the box"
      f"   ({cpu['min_idle_pct']}% out of {cpu['box_capacity_pct']}%)")
    w(f"  near-saturated samples   {cpu['saturated_samples']} of {cpu['total_samples']}"
      f"   (under 5% of the box left free)")
    if any(n > 3 for n in cpu["processes_started"].values()):
        w("     'processes started' counts distinct processes over the whole run. A")
        w("     group that starts a fresh one per turn pays its startup cost over and")
        w("     over, which shows up as a high peak against a low mean.")

    if r["projection"]:
        w("\nWHERE IT RUNS OUT  (CPU fitted against measured concurrency)")
        w(f"    {'group':<16}{'peak':<10}{'fixed':<10}{'per call':<11}{'ceiling':<10}"
          f"{'~calls':<9}{'fit':<7}")
        for p in r["projection"]:
            per = f"{p['per_call_pct']}%" if p["per_call_pct"] is not None else "-"
            fixed = f"{p['fixed_pct']}%" if p["fixed_pct"] is not None else "-"
            w(f"    {p['group']:<16}{str(p['peak_pct']) + '%':<10}{fixed:<10}{per:<11}"
              f"{str(p['ceiling_pct']) + '%':<10}"
              f"{(str(p['projected_calls']) if p['projected_calls'] else 'flat'):<9}"
              f"{(str(p['fit_r2']) if p['fit_r2'] is not None else '-'):<7}")
        w("     'fixed' is what the group costs with no calls running; 'per call' is")
        w("     what each additional concurrent call adds on top. 'flat' means no")
        w("     per-call cost was measurable, so no call count follows from it.")
        first = next((p for p in r["projection"] if p["projected_calls"]), None)
        if first:
            w(f"\n  First to run out: {first['group']} at roughly "
              f"{first['projected_calls']} concurrent calls.")
            if not first["scales_across_cores"]:
                w("  It is single-threaded, so adding cores will not move that number.")
            if first["group"] == "test_harness":
                w("  That is this test rig, not the system under test. It says the")
                w("  measurement stops being trustworthy near that call count - run")
                w("  the harness from another box before reading anything above it.")
            if first["fit_r2"] is not None and first["fit_r2"] < 0.5:
                w(f"  The fit behind that number is weak (R2 {first['fit_r2']}), so treat")
                w("  it as a direction rather than a figure.")
    w("")
    return "\n".join(L)


# --- the ladder: what no single rung can tell you ----------------------------

def build_ladder(reports, slo_ms=None):
    rungs = []
    for r in sorted(reports, key=lambda x: x["requested_calls"]):
        c, lat = r["calls"], r["latency_ms"]
        rungs.append({
            "requested": r["requested_calls"],
            "connected": c["connected"],
            "peak_calls": c["peak_concurrent_measured"],
            "peak_inflight": c["peak_inflight"],
            "answered": c["answered_turns"],
            "failed": r["outcomes"]["failed"],
            "p50": lat["response"].get("p50"),
            "p95": lat["response"].get("p95"),
            "pipeline_p50": lat["pipeline"].get("p50"),
            "slo_breaches": lat["slo_breaches"],
            "turns_per_call": (c.get("turns_per_call") or {}).get("p50"),
            "min_idle": r["cpu"].get("min_idle_of_box_pct", r["cpu"]["min_idle_pct"]),
            "saturated": r["cpu"]["saturated_samples"],
            "cpu_samples": r["cpu"]["total_samples"],
            "top_group": next(iter(r["cpu"]["peak_by_group"]), None),
            "top_pct": next(iter(r["cpu"]["peak_by_group"].values()), None),
        })

    base = next((x for x in rungs if x["p50"]), None)
    for x in rungs:
        x["vs_base"] = (round(x["p50"] / base["p50"], 2)
                        if base and x["p50"] else None)

    first_failure = next((x["requested"] for x in rungs if x["failed"]), None)
    first_slo = next((x["requested"] for x in rungs
                      if slo_ms and x["p95"] and x["p95"] > slo_ms), None)
    # One sample touching zero is a spike, not a saturated box. Sustained means
    # at least three samples and one percent of the run.
    first_sat = next((x["requested"] for x in rungs
                      if x["saturated"] >= 3
                      and x["saturated"] >= 0.01 * (x["cpu_samples"] or 1)), None)

    # Conversations getting shorter is a failure the per-call verdict cannot
    # see - every turn a truncated call did get was answered. Measured against
    # the smallest rung, so no absolute idea of "a full conversation" is needed.
    base_tpc = next((x["turns_per_call"] for x in rungs if x["turns_per_call"]), None)
    first_collapse = next((x["requested"] for x in rungs
                           if base_tpc and x["turns_per_call"]
                           and x["turns_per_call"] < 0.6 * base_tpc), None)

    # Pooled across every rung: the densest available view of wait against load.
    xs, ys = [], []
    for r in reports:
        for t in r["turns"]:
            if t.get("response_ms") is not None and t.get("inflight") is not None:
                xs.append(t["inflight"])
                ys.append(t["response_ms"])
    pooled = fit_line(xs, ys)

    # Turn 0 happens while every call is still up, so it is the one measurement
    # taken at exactly the requested concurrency, uncontaminated by calls
    # draining out of the run underneath it.
    t0_xs, t0_ys = [], []
    for r in sorted(reports, key=lambda x: x["requested_calls"]):
        s = r["latency_ms"]["by_turn"].get("0")
        if s and s.get("p50"):
            t0_xs.append(r["requested_calls"])
            t0_ys.append(s["p50"])
    turn0 = fit_line(t0_xs, t0_ys)

    # Each turn asks the backend for something different - a name lookup is not
    # a yes/no - so pooling every turn together buries the load signal under the
    # difference between turns. Fitting one turn index at a time across the
    # rungs holds the work constant and lets concurrency show.
    per_turn = {}
    for r in reports:
        for k, s in r["latency_ms"]["by_turn"].items():
            if s.get("p50"):
                per_turn.setdefault(k, ([], []))
                per_turn[k][0].append(r["requested_calls"])
                per_turn[k][1].append(s["p50"])
    turn_fits = {}
    for k, (xs, ys) in per_turn.items():
        f = fit_line(xs, ys)
        if f:
            base = min(zip(xs, ys))[1]
            top = max(zip(xs, ys))[1]
            turn_fits[k] = {**f, "base_p50": base, "top_p50": top,
                            "growth": round(top / base, 2) if base else None}

    ceilings = {}
    for r in reports:
        for p in r["projection"]:
            if p["projected_calls"]:
                ceilings.setdefault(p["group"], []).append(p["projected_calls"])

    return {
        "rungs": rungs,
        "slo_ms": slo_ms,
        "first_failure_at": first_failure,
        "first_slo_breach_at": first_slo,
        "first_saturation_at": first_sat,
        "first_collapse_at": first_collapse,
        "base_turns_per_call": base_tpc,
        "pooled_fit": pooled,
        "turn0_fit": turn0,
        "turn_fits": turn_fits,
        "turn0_points": list(zip(t0_xs, t0_ys)),
        "ceiling_by_group": {g: int(statistics.fmean(v)) for g, v in ceilings.items()},
        "cores": reports[0]["cores"] if reports else None,
        "silence_timer_ms": reports[0].get("silence_timer_ms", 0) if reports else 0,
    }


def render_ladder(d):
    L = []
    w = L.append
    rungs = d["rungs"]
    if not rungs:
        return "no rungs completed"
    top = rungs[-1]["requested"]

    w("\n" + "=" * 88)
    w(f"  CAPACITY LADDER   {rungs[0]['requested']} -> {top} calls   {d['cores']} cores")
    w("=" * 88)

    w(f"\n  {'calls':<7}{'up':<6}{'inflt':<7}{'turns':<8}{'/call':<8}{'failed':<8}"
      f"{'p50':<10}{'p95':<10}{'vs base':<9}{'idle':<8}{'busiest':<20}")
    for x in rungs:
        busiest = (f"{x['top_group']} {x['top_pct']}%" if x["top_group"] else "-")
        tpc = f"{x['turns_per_call']:.0f}" if x["turns_per_call"] else "-"
        w(f"  {x['requested']:<7}{x['peak_calls']:<6}{x['peak_inflight']:<7}"
          f"{x['answered']:<8}{tpc:<8}{x['failed']:<8}{ms(x['p50'], 10)}{ms(x['p95'], 10)}"
          f"{(str(x['vs_base']) + 'x' if x['vs_base'] else '-'):<9}"
          f"{(str(x['min_idle']) + '%' if x['min_idle'] is not None else '-'):<8}{busiest:<20}")
    w("\n     up      = calls actually connected at the same time")
    w("     inflt   = requests in flight at once - the real load on the pipeline")
    w("     turns   = turns answered across the whole rung")
    w("     /call   = median turns each call got through before it ended. This")
    w("               falling is a conversation breaking down, and it will not")
    w("               show up as a failed call - every turn it got was answered.")
    w("     vs base = median wait as a multiple of the smallest rung")

    w("\nWHERE IT BREAKS")

    def line(label, at, detail=""):
        if at:
            w(f"  {label:<28} {at} calls   {detail}")
        else:
            w(f"  {label:<28} not reached by {top} calls   {detail}")

    line("first failed call", d["first_failure_at"])
    if d.get("base_turns_per_call"):
        line("conversations cut short", d["first_collapse_at"],
             f"(under 60% of the {d['base_turns_per_call']:.0f} turns/call "
             f"the smallest rung managed)")
    if d["slo_ms"]:
        line(f"p95 wait over {d['slo_ms'] / 1000:.1f}s", d["first_slo_breach_at"],
             "(threshold set by --slo)")
    idles = [x["min_idle"] for x in rungs if x["min_idle"] is not None]
    line("box out of CPU", d["first_saturation_at"],
         f"(lowest idle seen {min(idles)}%, sustained)" if idles else "")

    if d["ceiling_by_group"]:
        w("\n  Extrapolating the fitted CPU slopes:")
        for g, n in sorted(d["ceiling_by_group"].items(), key=lambda kv: kv[1]):
            w(f"    {g:<18} runs out at roughly {n} concurrent calls")

    w("\nHOW WAIT SCALES")
    t0 = d["turn0_fit"]
    if t0:
        w("  at the requested concurrency (turn 0, while every call is still up):")
        w(f"    wait = {t0['intercept']:.0f}ms + {t0['slope']:.0f}ms per call"
          f"   (R2 {t0['r2']}, {t0['n']} rungs)")
    p = d["pooled_fit"]
    if p:
        w("  across every answered turn at every rung:")
        w(f"    wait = {p['intercept']:.0f}ms + {p['slope']:.0f}ms per request in flight"
          f"   (R2 {p['r2']}, {p['n']} turns, {p['x_min']}-{p['x_max']} in flight)")
        if p["r2"] is not None and p["r2"] < 0.3:
            w("    R2 is low. Turns are not interchangeable - a name lookup is not a")
            w("    yes/no - so pooling them buries load under the difference between")
            w("    turns. The per-turn table below holds the work constant instead.")

    fits = d.get("turn_fits") or {}
    if fits:
        w("\n  per turn, across the rungs (same question each time, rising load):")
        w(f"    {'turn':<7}{'calls':<12}{'at low load':<15}{'at high load':<15}"
          f"{'growth':<10}{'per call':<11}{'fit':<7}")
        for k in sorted(fits, key=int):
            f = fits[k]
            # Not every turn survives to the top rung, so each row names the
            # call range it was actually measured over.
            span = "{}-{}".format(f["x_min"], f["x_max"])
            w(f"    {k:<7}{span:<12}"
              f"{ms(f['base_p50'], 15)}{ms(f['top_p50'], 15)}"
              f"{(str(f['growth']) + 'x' if f['growth'] else '-'):<10}"
              f"{str(round(f['slope'])) + 'ms':<11}"
              f"{(str(f['r2']) if f['r2'] is not None else '-'):<7}")
        worst = max(fits.items(), key=lambda kv: kv[1]["growth"] or 0)
        if worst[1]["growth"]:
            w(f"     Turn {worst[0]} degrades hardest, {worst[1]['growth']}x across its range."
              f" A turn that grows")
            w("     far faster than the rest points at one backend step rather than at")
            w("     the system as a whole.")

    first, last = rungs[0], rungs[-1]
    if d["silence_timer_ms"] and first["pipeline_p50"] and last["pipeline_p50"]:
        w(f"\n  {d['silence_timer_ms']:.0f}ms of every wait is the configured PBX silence")
        w("  timer, which does not move with load. Excluding it, the work itself went")
        w(f"  {ms(first['pipeline_p50'])} -> {ms(last['pipeline_p50'])}, a "
          f"{last['pipeline_p50'] / first['pipeline_p50']:.2f}x increase across the ladder")
        w(f"  (against {last['p50'] / first['p50']:.2f}x for the wait as a caller hears it).")
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
    # Same t0 as the recorder: the two clocks started a moment apart, and CPU
    # cannot be lined up against concurrency if their relative times disagree.
    cpu = CpuSampler(interval=args.cpu_interval, stream_path=f"{stem}.cpu.csv",
                     t0=rec.t0)
    chan = ChannelSampler(interval=args.channel_interval,
                          stream_path=f"{stem}.channels.csv", t0=rec.t0)
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
                          os.cpu_count() or 1, wall,
                          silence_timer_ms=args.silence_timer * 1000.0,
                          slo_ms=args.slo)

    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    rec.write_ndjson(f"{stem}.events.ndjson")

    with open(f"{stem}.turns.csv", "w", encoding="utf-8") as f:
        f.write("call_id,turn,action_type,action,response_ms,remote_speech_ms,"
                "turn_total_ms,detected_by,calls_up,inflight\n")
        for t in report["turns"]:
            f.write(f"{t['call_id']},{t.get('turn','')},{t.get('action_type','')},"
                    f"\"{t.get('action','')}\",{t.get('response_ms','')},"
                    f"{t.get('remote_speech_ms','')},{t.get('turn_total_ms','')},"
                    f"{t.get('detected_by','')},{t.get('calls_up','')},"
                    f"{t.get('inflight','')}\n")

    # The sampler already streamed a richer cpu.csv - core count, busy percent,
    # per-group process counts. Rewriting it here replaced that with a narrower
    # format rebuild.py cannot parse, so a run that finished lost the CPU data
    # that a run which had to be killed kept.

    text = render(report)
    print(text)
    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  wrote {stem}.{{txt,json,turns.csv,cpu.csv,channels.csv,events.ndjson,runner.log}}\n")
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


def summarize_ladder(paths, args, outdir):
    """Build the ladder report from rungs already on disk.

    Kept separate from running them so a rung that had to be rebuilt by hand can
    still be folded into the summary without repeating the whole ladder.
    """
    reports = []
    for path in paths:
        try:
            reports.append(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"*** skipping {path}: {e}", file=sys.stderr)
    if not reports:
        print("*** no rung reports to summarize", file=sys.stderr)
        return None

    d = build_ladder(reports, slo_ms=args.slo)
    text = render_ladder(d)
    print(text)

    stem = outdir / f"{args.label}-ladder"
    with open(f"{stem}.txt", "w", encoding="utf-8") as f:
        f.write(text)
    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    with open(f"{stem}.csv", "w", encoding="utf-8") as f:
        cols = ["requested", "connected", "peak_calls", "peak_inflight", "answered",
                "failed", "p50", "p95", "pipeline_p50", "slo_breaches", "vs_base",
                "min_idle", "saturated", "top_group", "top_pct"]
        f.write(",".join(cols) + "\n")
        for x in d["rungs"]:
            f.write(",".join("" if x.get(c) is None else str(x.get(c)) for c in cols) + "\n")
    print(f"  wrote {stem}.{{txt,json,csv}}\n")
    return d


def main():
    ap = argparse.ArgumentParser(
        description="Place N concurrent calls, measure everything, and say where "
                    "the system starts to degrade.")
    ap.add_argument("--calls", default="40",
                    help="call count, or a comma-separated ladder e.g. 5,10,20,40")
    ap.add_argument("--label", default="run")
    ap.add_argument("--gap", type=int, default=None,
                    help="ms between call starts (default: leave config alone)")
    ap.add_argument("--slo", type=float, default=10000,
                    help="the wait in ms above which a turn counts as too slow. "
                         "This is a judgement about your callers, not a measurement "
                         "- set it to whatever you would actually accept (default 10000)")
    ap.add_argument("--silence-timer", type=float, default=2.0,
                    help="seconds the PBX waits out on every turn before it starts "
                         "working (SILENCE_END_SEC in extensions_custom.conf). Reported "
                         "apart from the rest so a fixed cost is not mistaken for load "
                         "(default 2.0; use 0 to disable the split)")
    ap.add_argument("--cpu-interval", type=float, default=0.5)
    ap.add_argument("--channel-interval", type=float, default=2.0)
    ap.add_argument("--stall-timeout", type=int, default=120,
                    help="end the run after this many seconds with no call activity "
                         "(default 120; the per-turn wait can legitimately reach 60)")
    ap.add_argument("--teardown-grace", type=int, default=25,
                    help="seconds to let pjsua2 shut down after the last call "
                         "ends before writing the report and exiting anyway")
    ap.add_argument("--settle", type=int, default=30,
                    help="seconds between rungs of a ladder")
    ap.add_argument("--summarize", nargs="*", default=None, metavar="RUN.json",
                    help="skip running; build the ladder report from these rung "
                         "reports (e.g. --summarize results/ladder-*calls.json)")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.summarize is not None:
        paths = args.summarize or sorted(glob.glob(str(outdir / f"{args.label}-*calls.json")))
        summarize_ladder(paths, args, outdir)
        return

    if os.name != "posix":
        sys.exit("This reads /proc and calls the asterisk CLI - run it on the PBX host.")

    busy = _check_sip_port_free(wait_s=20)
    if busy:
        sys.exit(f"\nERROR: {busy}\n")

    levels = [int(x) for x in str(args.calls).split(",") if x.strip()]
    if len(levels) > 1:
        print(f"*** ladder: {levels}   settle {args.settle}s between rungs")
        print("*** each rung runs in its own process so pjsua2 starts clean")
        here = Path(__file__).resolve()
        passthrough = (f"--out {args.out} --cpu-interval {args.cpu_interval} "
                       f"--channel-interval {args.channel_interval} "
                       f"--stall-timeout {args.stall_timeout} "
                       f"--teardown-grace {args.teardown_grace} "
                       f"--slo {args.slo} --silence-timer {args.silence_timer}"
                       + (f" --gap {args.gap}" if args.gap else ""))
        for i, n in enumerate(levels):
            print(f"\n*** rung {i + 1} of {len(levels)}: {n} calls")
            os.system(f"{sys.executable} {here} --calls {n} --label {args.label} "
                      + passthrough)
            if i < len(levels) - 1:
                print(f"*** settling {args.settle}s before the next rung")
                time.sleep(args.settle)

        done = [outdir / f"{args.label}-{n}calls.json" for n in levels]
        missing = [p for p in done if not p.exists()]
        for p in missing:
            print(f"*** {p.name} is missing - that rung produced no report. Rebuild it "
                  f"with rebuild.py, then re-run with --summarize", file=sys.stderr)
        summarize_ladder([p for p in done if p.exists()], args, outdir)
        return

    run_one(levels[0], args, outdir)


if __name__ == "__main__":
    main()
