#!/usr/bin/env python3
"""Why do conversations get shorter as concurrency rises?

    python diagnose.py --run results/ladder-40calls.json
    python diagnose.py --curl-log /var/log/asterisk/ai_curl.log

A shorter conversation is not the same thing as a failed call. Every call can
reach a clean end and still have got nowhere, because the PBX transfers out
when it cannot make sense of the caller. So the question is not "did calls
die" - it is "did the system stop understanding them, and why".

This ties four things together, per turn:

  how many calls were up      from the run's own event trace
  how much audio was captured from the PBX's in_bytes log line
  what that audio sounded like from the recording the PBX sent to Jane
  where the conversation ended from the last turn each session reached

If captured audio shrinks as concurrency rises, the recogniser is being handed
silence, and hallucinated transcripts follow from that rather than from the
model being unreliable. If the audio is fine and the conversations still stop,
the problem is downstream and this will say so.
"""

import argparse
import glob
import json
import os
import re
import statistics
import sys
import wave
from pathlib import Path

# 2026-08-25 14:23:11 caller=1001 session=17561234567 inter=3 barge=0 vad=speech in_bytes=41324 ...
CURL_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<rest>.*)$")

SILENT_BYTES = 2000          # under ~0.12s of 8kHz 16-bit audio: nothing was said
QUIET_RMS = 120              # below the VAD's own MIN_AMPLITUDE neighbourhood

# vad_bargein gives up after NO_SPEECH_TIMEOUT_MS with no speech at all and
# writes out what it buffered - which is that many seconds of digital silence.
# A recording of exactly this length, containing nothing, is that timeout rather
# than a quiet caller.
NO_SPEECH_TIMEOUT_S = 5.0


def is_silent(row):
    """Did this turn carry any speech at all?

    Size alone does not answer it. The recordings that matter here are 80KB and
    five seconds long, and every sample in them is zero - a big file containing
    nothing. Checking bytes reported none of them.
    """
    if row.get("audio_rms") is not None:
        return row["audio_rms"] == 0 or row["audio_rms"] < QUIET_RMS
    if row.get("audio_silence_frac") is not None:
        return row["audio_silence_frac"] > 0.95
    return (row.get("in_bytes") or 0) < SILENT_BYTES


def parse_curl_log(path, since=None, until=None):
    """Per-turn records the dialplan already writes before each dispatch."""
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"*** cannot read {path}: {e}", file=sys.stderr)
        return out
    for line in lines:
        m = CURL_LINE.match(line.strip())
        if not m:
            continue
        rec = {"ts": m.group("ts")}
        for part in m.group("rest").split():
            if "=" in part:
                k, v = part.split("=", 1)
                rec[k] = v
        if "session" not in rec:
            continue
        for k in ("inter", "in_bytes", "barge"):
            try:
                rec[k] = int(rec[k])
            except (KeyError, ValueError, TypeError):
                rec[k] = None
        out.append(rec)
    if since or until:
        out = [r for r in out
               if (not since or r["ts"] >= since) and (not until or r["ts"] <= until)]
    return out


def audio_stats(path):
    """Duration, level and how much of the recording is effectively silence."""
    try:
        with wave.open(str(path), "rb") as w:
            n, width, rate = w.getnframes(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(n)
    except (OSError, wave.Error):
        return None
    if width != 2 or not raw:
        return {"duration_s": round(n / rate, 2) if rate else 0, "rms": None,
                "peak": None, "silence_frac": None}

    import array
    samples = array.array("h")
    samples.frombytes(raw[:len(raw) - (len(raw) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return {"duration_s": 0.0, "rms": 0, "peak": 0, "silence_frac": 1.0}

    peak = max(abs(s) for s in samples)
    total = sum(s * s for s in samples)
    rms = int((total / len(samples)) ** 0.5)

    # Silence measured in 32ms frames, the same unit the VAD works in.
    frame = max(1, rate * 32 // 1000)
    quiet = frames = 0
    for i in range(0, len(samples) - frame, frame):
        frames += 1
        if max(abs(s) for s in samples[i:i + frame]) < 200:
            quiet += 1
    return {
        "duration_s": round(len(samples) / rate, 2),
        "rms": rms,
        "peak": peak,
        "silence_frac": round(quiet / frames, 2) if frames else None,
    }


def find_recording(sounds_dir, session_prefix, interaction):
    """The raw input WAV the PBX handed to Jane for this turn."""
    if not session_prefix or interaction is None:
        return None
    name = f"{session_prefix}_{interaction}_input_raw.wav"
    direct = Path(sounds_dir) / session_prefix / name
    if direct.exists():
        return direct
    hits = glob.glob(str(Path(sounds_dir) / "**" / name), recursive=True)
    return Path(hits[0]) if hits else None


def concurrency_from_run(run):
    """session -> {turn index -> in-flight at that turn}, from the run's trace."""
    by_call = {}
    for c in run.get("call_records") or []:
        key = c.get("session_prefix") or c.get("session")
        if key:
            by_call[c["call_id"]] = key
    per_session = {}
    for t in run.get("turns") or []:
        key = by_call.get(t["call_id"])
        if key is None:
            continue
        per_session.setdefault(key, {})[t.get("turn")] = {
            "inflight": t.get("inflight"),
            "calls_up": t.get("calls_up"),
            "response_ms": t.get("response_ms"),
            "detected_by": t.get("detected_by"),
        }
    return per_session


def concurrency_from_log(records, window=30.0):
    """Fallback when the run trace has no session ids: how many other sessions
    were active within a window of each turn."""
    import time as _t
    stamps = []
    for r in records:
        try:
            stamps.append(_t.mktime(_t.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            stamps.append(None)
    for r, s in zip(records, stamps):
        r["_epoch"] = s
    for r, s in zip(records, stamps):
        if s is None:
            r["_est_concurrent"] = None
            continue
        r["_est_concurrent"] = len({
            o["session"] for o, os_ in zip(records, stamps)
            if os_ is not None and abs(os_ - s) <= window})
    return records


def bucket(value, edges=(1, 5, 10, 20, 30, 40)):
    if value is None:
        return None
    for i, e in enumerate(edges):
        if value <= e:
            return e
    return edges[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="a run .json from evaluate.py, for exact concurrency")
    ap.add_argument("--curl-log", default="/var/log/asterisk/ai_curl.log")
    ap.add_argument("--sounds", default="/usr/local/share/asterisk/sounds",
                    help="where the PBX writes session directories")
    ap.add_argument("--interaction-offset", type=int, default=1,
                    help="dialplan INTERACTION for the harness's turn 0 (default 1)")
    ap.add_argument("--no-audio", action="store_true",
                    help="skip reading the WAVs (much faster; bytes only)")
    ap.add_argument("--out", default=None, help="write a per-turn CSV here")
    args = ap.parse_args()

    run = None
    if args.run:
        try:
            run = json.loads(Path(args.run).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.exit(f"cannot read {args.run}: {e}")

    records = parse_curl_log(args.curl_log)
    if not records:
        sys.exit(f"no usable lines in {args.curl_log}")

    per_session = concurrency_from_run(run) if run else {}
    if not per_session:
        print("*** no session ids in the run trace - estimating concurrency from the\n"
              "*** log's own timestamps instead. For exact figures the AMI user needs\n"
              "*** the 'dialplan' read class so SESSION VarSet events come through.",
              file=sys.stderr)
    # Always available as a fallback. The log spans every rung of a ladder, so
    # turns belonging to other rungs have no entry in this run's trace - and
    # dropping them from the summary while still listing them below made the two
    # halves of the report contradict each other.
    records = concurrency_from_log(records)

    # Sessions the run actually placed, so an unrelated call in the log is not
    # mixed into the numbers.
    wanted = set(per_session) or None

    rows = []
    for r in records:
        prefix = f"{r.get('caller','')}_{r.get('session','')}"
        key = prefix if (wanted and prefix in wanted) else r.get("session")
        if wanted and prefix not in wanted and r.get("session") not in wanted:
            continue
        turn = (r["inter"] - args.interaction_offset) if r["inter"] is not None else None
        info = (per_session.get(key) or {}).get(turn, {})
        conc = info.get("inflight") or info.get("calls_up") or r.get("_est_concurrent")

        stats = None
        if not args.no_audio:
            wav = find_recording(args.sounds, prefix, r["inter"])
            if wav:
                stats = audio_stats(wav)

        rows.append({
            "session": r.get("session"),
            "prefix": prefix,
            "inter": r["inter"],
            "turn": turn,
            "concurrency": conc,
            "in_bytes": r.get("in_bytes"),
            "vad": r.get("vad"),
            "barge": r.get("barge"),
            "response_ms": info.get("response_ms"),
            "detected_by": info.get("detected_by"),
            **{f"audio_{k}": v for k, v in (stats or {}).items()},
        })

    if not rows:
        sys.exit("no turns matched - check --run, --curl-log and --interaction-offset")

    report(rows, args)

    if args.out:
        cols = sorted({k for r in rows for k in r})
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(",".join(cols) + "\n")
            for r in rows:
                f.write(",".join("" if r.get(c) is None else str(r.get(c))
                                 for c in cols) + "\n")
        print(f"  wrote {args.out}\n")


def report(rows, args):
    w = print
    w("\n" + "=" * 76)
    w(f"  AUDIO AND CONCURRENCY   {len(rows)} turns"
      f"   {len({r['prefix'] for r in rows})} sessions")
    w("=" * 76)

    have_conc = [r for r in rows if r["concurrency"]]
    have_bytes = [r for r in rows if r["in_bytes"] is not None]

    w("\nHOW MUCH AUDIO THE PBX CAPTURED, BY CONCURRENCY")
    w("  in_bytes is what the dialplan measured before sending the turn to Jane.")
    w("  8kHz 16-bit mono, so 16000 bytes is one second of speech.\n")
    if have_conc and have_bytes:
        buckets = {}
        for r in have_conc:
            if r["in_bytes"] is None:
                continue
            buckets.setdefault(bucket(r["concurrency"]), []).append(r)
        w(f"    {'calls':<9}{'turns':<8}{'median bytes':<15}{'median secs':<14}"
          f"{'no speech':<15}{'vad timeouts':<12}")
        for b in sorted(x for x in buckets if x is not None):
            rs = buckets[b]
            by = [x["in_bytes"] for x in rs if x["in_bytes"] is not None]
            secs = [x.get("audio_duration_s") for x in rs
                    if x.get("audio_duration_s") is not None]
            sil = sum(1 for x in rs if is_silent(x))
            timeouts = sum(1 for x in rs
                           if is_silent(x) and x.get("audio_duration_s") is not None
                           and abs(x["audio_duration_s"] - NO_SPEECH_TIMEOUT_S) < 0.3)
            med_by = statistics.median(by) if by else "-"
            med_s = round(statistics.median(secs), 2) if secs else "-"
            sil_text = "{} ({:.0%})".format(sil, sil / len(rs))
            w(f"    <={b:<7}{len(rs):<8}{med_by:<15}{med_s:<14}"
              f"{sil_text:<15}{timeouts:<12}")
        w("\n  'silent turns' are turns where the PBX captured almost nothing. A")
        w("  recogniser handed one of those does not return nothing - it returns")
        w("  fluent text that was never said. That is where hallucinations come from,")
        w("  and if this column climbs with concurrency, that is the mechanism.")
    else:
        w("    (no concurrency figures available - pass --run with a trace that has")
        w("     session ids, or check the log timestamps)")

    w("\nWHERE EACH CONVERSATION STOPPED")
    last = {}
    for r in rows:
        p = r["prefix"]
        if r["inter"] is not None and r["inter"] >= last.get(p, (0, None))[0]:
            last[p] = (r["inter"], r["concurrency"])
    if last:
        by_conc = {}
        for p, (inter, conc) in last.items():
            by_conc.setdefault(bucket(conc), []).append(inter)
        w(f"    {'calls':<9}{'sessions':<11}{'median last turn':<18}{'shortest':<10}")
        for b in sorted((x for x in by_conc if x is not None)):
            v = by_conc[b]
            w(f"    <={b:<7}{len(v):<11}{statistics.median(v):<18}{min(v):<10}")
        w("\n  A conversation that stops early has usually been transferred out, not")
        w("  dropped - which is why every call still reports as completed.")

    w("\nTURNS THAT WOULD MAKE A RECOGNISER HALLUCINATE")
    bad = [r for r in rows if is_silent(r)]
    dead = [r for r in bad if r.get("audio_rms") == 0]
    timeouts = [r for r in bad if r.get("audio_duration_s") is not None
                and abs(r["audio_duration_s"] - NO_SPEECH_TIMEOUT_S) < 0.3]
    if dead:
        w(f"  {len(dead)} of them contain nothing at all - every sample is zero, not")
        w("  quiet. That is not a caller who did not speak; it is no audio arriving.")
    if timeouts:
        w(f"  {len(timeouts)} are almost exactly {NO_SPEECH_TIMEOUT_S:.0f}s long, which is")
        w("  vad_bargein's NO_SPEECH_TIMEOUT_MS. The VAD waited the full timeout, heard")
        w("  nothing, gave up, and the dialplan sent the silence on to Jane regardless.")
    w("")
    if bad:
        w(f"  {len(bad)} of {len(rows)} turns "
          f"({len(bad) / len(rows):.0%}) sent little or no real audio:\n")
        w(f"    {'session':<16}{'turn':<7}{'bytes':<9}{'secs':<8}{'rms':<7}"
          f"{'silence':<10}{'calls':<7}{'vad':<9}")
        for r in sorted(bad, key=lambda x: (x["concurrency"] or 0))[:20]:
            frac = r.get("audio_silence_frac")
            sil_text = "-" if frac is None else "{:.0%}".format(frac)
            w(f"    {str(r['session'])[:15]:<16}{str(r['inter']):<7}"
              f"{str(r['in_bytes']):<9}"
              f"{str(r.get('audio_duration_s', '-')):<8}"
              f"{str(r.get('audio_rms', '-')):<7}"
              f"{sil_text:<10}"
              f"{str(r['concurrency'] or '-'):<7}{str(r.get('vad'))[:8]:<9}")
        if len(bad) > 20:
            w(f"    ... and {len(bad) - 20} more"
              + (f" - all of them in {args.out}" if args.out else ""))
    else:
        w("  None. Every turn carried real audio, so hallucinated transcripts are")
        w("  not being caused by silence reaching the recogniser - look downstream.")

    w("\nDOES THIS GET WORSE WITH LOAD?")
    # Bytes were the wrong signal: a turn that captured nothing still produces a
    # large file, so file size stays flat while the content goes empty. What
    # matters is the share of turns carrying no speech.
    pairs = [(r["concurrency"], 1.0 if is_silent(r) else 0.0) for r in rows
             if r["concurrency"]]
    if len(pairs) >= 3:
        xs = [p[0] for p in pairs]
        mx, my = statistics.fmean(xs), statistics.fmean([p[1] for p in pairs])
        sxx = sum((x - mx) ** 2 for x in xs)
        # A slope fitted across a two-call spread extrapolates wildly from
        # nothing. Needs a real range of concurrency behind it to mean anything.
        if max(xs) - min(xs) < 5:
            w(f"  Only {min(xs)}-{max(xs)} concurrent calls in this data - too narrow a")
            w(f"  range to fit a trend against. {my:.0%} of turns were silent overall.")
            w("  Point --run at rungs spanning a wider spread, or pass a log covering")
            w("  more than one rung of a ladder.")
        elif sxx > 0:
            slope = sum((x - mx) * (y - my) for x, y in pairs) / sxx
            w(f"  {slope * 100:+.1f} percentage points of silent turns per additional"
              f" concurrent call")
            w(f"  ({len(pairs)} turns, {min(xs)}-{max(xs)} calls, "
              f"{my:.0%} silent overall)")
            if slope > 0.005:
                w("\n  Silent turns climb with concurrency. Whatever stops audio reaching")
                w("  the VAD happens more often the busier the box is, so the hallucinations")
                w("  and the shortened conversations share one cause worth fixing.")
            elif slope < -0.005:
                w("\n  Silent turns fall as load rises, which usually means they are not")
                w("  caused by load at all - look for something that affects every run.")
            else:
                w("\n  Flat. The silent turns are not load-driven: they happen at the same")
                w("  rate whether the box is busy or idle. That points at the exchange")
                w("  between the caller side and the VAD, not at capacity.")

        by_turn = {}
        for r in rows:
            if r["turn"] is not None:
                by_turn.setdefault(r["turn"], []).append(is_silent(r))
        if by_turn:
            w("\n  by turn number (does a conversation go deaf partway through?):")
            w(f"    {'turn':<7}{'turns':<8}{'silent':<10}")
            for t in sorted(by_turn)[:14]:
                v = by_turn[t]
                w(f"    {t:<7}{len(v):<8}{sum(v)}/{len(v)} ({sum(v) / len(v):.0%})")
            w("     Silence that starts partway in and stays is a call that lost sync,")
            w("     not a system that is uniformly broken.")
    else:
        w("  (not enough matched turns to say)")
    w("")


if __name__ == "__main__":
    main()
