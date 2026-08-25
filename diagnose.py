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


def is_voice_turn(row):
    """DTMF turns are answered with Read() in the dialplan, not EAGI, so no
    recording is ever made for them. Absence of audio there is correct, not a
    fault, and counting it as silence made an entire turn index look 100% dead."""
    return (row.get("vad") or "").lower() != "dtmf"


def is_silent(row):
    """Did this turn carry any speech at all?

    Size alone does not answer it. The recordings that matter here are 80KB and
    five seconds long, and every sample in them is zero - a big file containing
    nothing. Checking bytes reported none of them.
    """
    if not is_voice_turn(row):
        return False
    if row.get("audio_rms") is not None:
        return row["audio_rms"] == 0 or row["audio_rms"] < QUIET_RMS
    if row.get("audio_silence_frac") is not None:
        return row["audio_silence_frac"] > 0.95
    if row.get("in_bytes") is None:
        return False              # no measurement, not a measurement of nothing
    return row["in_bytes"] < SILENT_BYTES


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

    raw = raw[:len(raw) - (len(raw) % 2)]
    n_samples = len(raw) // 2
    if not n_samples:
        return {"duration_s": 0.0, "rms": 0, "peak": 0, "silence_frac": 1.0}

    # audioop does these in C. The pure-Python version walked every sample three
    # times and sliced an array per 32ms frame, which on six hundred recordings
    # is tens of millions of interpreted operations.
    try:
        import audioop
        peak = audioop.max(raw, 2)
        rms = audioop.rms(raw, 2)
        frame_bytes = max(2, (rate * 32 // 1000) * 2)
        quiet = frames = 0
        for i in range(0, len(raw) - frame_bytes + 1, frame_bytes):
            frames += 1
            if audioop.max(raw[i:i + frame_bytes], 2) < 200:
                quiet += 1
    except ImportError:                       # audioop is gone in 3.13
        import array
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
        peak = max(abs(s) for s in samples)
        rms = int((sum(s * s for s in samples) / len(samples)) ** 0.5)
        frame = max(1, rate * 32 // 1000)
        quiet = frames = 0
        for i in range(0, len(samples) - frame, frame):
            frames += 1
            if max(abs(s) for s in samples[i:i + frame]) < 200:
                quiet += 1

    return {
        "duration_s": round(n_samples / rate, 2),
        "rms": rms,
        "peak": peak,
        "silence_frac": round(quiet / frames, 2) if frames else None,
    }


_RECORDING_INDEX = {}


def _index_recordings(sounds_dir):
    """Map every input recording under the sounds tree by filename, once.

    The sounds directory gains a folder per call and is never pruned, so it
    holds every session from every run ever made. Walking it once and keeping
    the names costs one pass; the recursive glob this replaces walked the whole
    tree again for every turn that missed its direct path.
    """
    key = str(sounds_dir)
    if key in _RECORDING_INDEX:
        return _RECORDING_INDEX[key]
    index = {}
    for root, _dirs, files in os.walk(sounds_dir):
        for name in files:
            if name.endswith("_input_raw.wav"):
                index.setdefault(name, os.path.join(root, name))
    _RECORDING_INDEX[key] = index
    return index


def find_recording(sounds_dir, session_prefix, interaction):
    """The raw input WAV the PBX handed to Jane for this turn."""
    if not session_prefix or interaction is None:
        return None
    name = f"{session_prefix}_{interaction}_input_raw.wav"
    direct = Path(sounds_dir) / session_prefix / name
    if direct.exists():
        return direct
    hit = _index_recordings(sounds_dir).get(name)
    return Path(hit) if hit else None


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
            "idle_at_turn": t.get("idle_at_turn"),
            # Which recording we played, so what arrived can be compared with
            # what was sent. A transcript that stops mid-word is either a file
            # that was always short or audio cut off on the way - and those need
            # opposite fixes.
            "action": t.get("action"),
            "action_type": t.get("action_type"),
        }
    return per_session


def idle_by_wallclock(run):
    """CPU headroom at any wall-clock instant during the run.

    Tagging turns through the harness's own records could only reach turns the
    harness knew about - which are precisely the turns that were not failing.
    The PBX logs every turn with a timestamp, so going through wall-clock time
    reaches the silent ones too, and those are the ones the question is about.
    """
    t0 = run.get("t0_epoch")
    timeline = run.get("cpu_timeline") or []
    cores = run.get("cores") or 1
    if not t0 or not timeline:
        return None
    points = sorted((t0 + s["rel"], s["idle_pct"] / cores) for s in timeline
                    if s.get("idle_pct") is not None)
    if not points:
        return None
    times = [p[0] for p in points]

    def at(epoch):
        import bisect
        i = bisect.bisect_left(times, epoch)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(points):
                d = abs(points[j][0] - epoch)
                # The PBX logs to the second, so a second of slack is the floor.
                if d <= 1.5 and (best is None or d < best[0]):
                    best = (d, points[j][1])
        return round(best[1], 1) if best else None
    return at


def source_durations(input_dir):
    """How long each recording we play actually is."""
    out = {}
    base = Path(input_dir)
    if not base.is_dir():
        return out
    for wav in base.glob("*.wav"):
        try:
            with wave.open(str(wav), "rb") as w:
                rate = w.getframerate()
                out[wav.name] = round(w.getnframes() / rate, 2) if rate else None
        except (OSError, wave.Error):
            continue
    return out


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

    # A sliding window over the records in time order. Comparing every record
    # against every other is fine for one run's worth and hopeless against
    # ai_curl.log, which is append-only and holds every run ever made.
    from collections import Counter
    timed = sorted((s, i) for i, s in enumerate(stamps) if s is not None)
    live = Counter()
    lo = hi = 0
    for s, i in timed:
        while hi < len(timed) and timed[hi][0] <= s + window:
            live[records[timed[hi][1]]["session"]] += 1
            hi += 1
        while lo < len(timed) and timed[lo][0] < s - window:
            sess = records[timed[lo][1]]["session"]
            live[sess] -= 1
            if live[sess] <= 0:
                del live[sess]
            lo += 1
        records[i]["_est_concurrent"] = len(live)
    for r, s in zip(records, stamps):
        if s is None:
            r["_est_concurrent"] = None
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
    ap.add_argument("--input-audios", default="input_audios",
                    help="where the WAVs we play live, to compare what we sent "
                         "against what the PBX captured")
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

    sources = source_durations(args.input_audios)
    idle_at_epoch = idle_by_wallclock(run) if run else None
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
            # Prefer the harness's own tag, but fall back to wall clock so
            # turns it never saw are still classified - they are the point.
            "idle_at_turn": (info.get("idle_at_turn")
                             if info.get("idle_at_turn") is not None
                             else (idle_at_epoch(r["_epoch"])
                                   if idle_at_epoch and r.get("_epoch") else None)),
            "sent_wav": Path(str(info.get("action") or "").replace(chr(92), "/")).name or None,
            "sent_secs": sources.get(
                Path(str(info.get("action") or "").replace(chr(92), "/")).name),
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
            if not is_voice_turn(r):
                continue          # DTMF turns make no recording by design
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
        w("\n  DTMF turns are excluded throughout: the dialplan answers those with")
        w("  Read() rather than EAGI, so no recording is made and none should be.")
        w("\n  'no speech' counts turns where the PBX captured almost nothing. A")
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

    sent = [r for r in rows if r.get("sent_secs") and r.get("audio_duration_s")]
    if sent:
        w("\nWHAT WE SENT vs WHAT THE PBX CAPTURED")
        w("  The PBX records our audio plus the silence timer it waits out after,")
        w("  so a capture should be LONGER than the file we played. Shorter means")
        w("  the audio was cut off on the way, and the transcript stops mid-word.")
        by_wav = {}
        for r in sent:
            by_wav.setdefault(r["sent_wav"], []).append(r)
        w(f"\n    {'we played':<20}{'its length':<13}{'captured (median)':<20}"
          f"{'turns':<8}{'cut short':<11}")
        for name, rs in sorted(by_wav.items()):
            src = rs[0]["sent_secs"]
            caps = [x["audio_duration_s"] for x in rs]
            short = sum(1 for c in caps if c < src - 0.15)
            w(f"    {name:<20}{str(src) + 's':<13}"
              + f"{statistics.median(caps):.2f}s".ljust(20)
              + f"{len(rs):<8}"
              + f"{short}/{len(rs)} ({short / len(rs):.0%})".ljust(11))
        worst = [(n, rs) for n, rs in by_wav.items()
                 if sum(1 for x in rs if x["audio_duration_s"] < x["sent_secs"] - 0.15)
                 > len(rs) * 0.5]
        if worst:
            w("")
            for name, rs in worst:
                src = rs[0]["sent_secs"]
                med = statistics.median([x["audio_duration_s"] for x in rs])
                w(f"     {name} is {src}s long but arrives as {med:.2f}s on most turns.")
            w("     Audio is being lost in transit, not misheard. Every call losing the")
            w("     same file at the same point is a fixed fault, not contention - check")
            w("     that file plays end to end on one call before reading anything else.")

    # Which of the PBX's turns the harness was present for. A turn the harness
    # never saw gets no reply, so the VAD records silence and times out - and
    # that is invisible in any measurement of the turns it did see.
    seen_any = any(r.get("detected_by") for r in rows)
    if seen_any:
        by_sess = {}
        for r in rows:
            if is_voice_turn(r) and r["inter"] is not None:
                by_sess.setdefault(r["prefix"], []).append(r)
        pattern_rows = []
        for prefix, rs in by_sess.items():
            rs.sort(key=lambda x: x["inter"])
            marks, missed, silent_after_miss = [], 0, 0
            for x in rs:
                if is_silent(x):
                    marks.append("S")
                elif x.get("detected_by"):
                    marks.append(".")
                else:
                    marks.append("?")
                if not x.get("detected_by") and not is_silent(x):
                    missed += 1
            answered = sum(1 for m in marks if m == ".")
            pattern_rows.append((prefix, "".join(marks), len(rs), answered,
                                 sum(1 for m in marks if m == "S")))
        pattern_rows.sort(key=lambda t: -t[4])
        if any(t[4] for t in pattern_rows):
            w("\nWHICH OF THE PBX'S TURNS THE HARNESS WAS PRESENT FOR")
            w("  One character per turn the PBX ran, in order.")
            w("    .  the harness answered it")
            w("    S  silence recorded - the harness never replied to this turn")
            w("    ?  audio present but the harness has no record of the turn\n")
            w(f"    {'session':<22}{'pattern':<26}{'PBX':<6}{'ours':<7}{'silent':<7}")
            for prefix, pat, total, answered, silent in pattern_rows[:16]:
                sess = prefix.split("_")[-1]
                w(f"    {sess:<22}{pat[:25]:<26}{total:<6}{answered:<7}{silent:<7}")
            tot_pbx = sum(t[2] for t in pattern_rows)
            tot_ours = sum(t[3] for t in pattern_rows)
            w(f"\n     Across every call the PBX ran {tot_pbx} turns and the harness answered")
            w(f"     {tot_ours}. The {tot_pbx - tot_ours} it did not answer are where the silence comes from:")
            w("     the PBX prompted, nothing replied, the VAD waited out its timeout and")
            w("     sent what it had. Runs of S at the end are a conversation that stopped;")
            w("     an S in the middle of dots is a single turn the harness slept through.")

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
             if r["concurrency"] and is_voice_turn(r)]
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

        # The decisive split. The harness answers a turn as soon as the PBX
        # tells it the prompt finished; when that AMI cue is missed it falls
        # back to listening for RTP silence, which is slower. Meanwhile the
        # harness transmits an actual silence file between turns - so a slow
        # answer means the PBX records that silence and times out. If the
        # fallback turns are the silent ones, that is the mechanism.
        by_detect = {}
        for r in rows:
            if r.get("detected_by") and is_voice_turn(r):
                by_detect.setdefault(r["detected_by"], []).append(is_silent(r))
        if len(by_detect) > 1:
            w("\n  by how the harness decided the PBX had finished talking:")
            w(f"    {'detected by':<16}{'turns':<9}{'silent':<12}")
            for k in sorted(by_detect):
                v = by_detect[k]
                w(f"    {k:<16}{len(v):<9}{sum(v)}/{len(v)} ({sum(v) / len(v):.0%})")
            amis = by_detect.get("ami", [])
            sils = by_detect.get("silence", [])
            if amis and sils:
                a = sum(amis) / len(amis)
                s = sum(sils) / len(sils)
                if s > a * 1.8:
                    w("\n     Turns that fell back to RTP silence detection are far more")
                    w("     likely to have recorded nothing. That path answers later, and")
                    w("     the harness streams a silence file while it waits - so the PBX")
                    w("     records that silence and hits its no-speech timeout. Fix the")
                    w("     missed AMI cue and these turns should stop going quiet.")
                elif a and s and abs(s - a) / max(a, s) < 0.3:
                    w("\n     Both paths go silent at about the same rate, so the missed AMI")
                    w("     cue is not what causes it. Look at the media path itself.")

        # The question this whole exercise keeps circling: is the silence caused
        # by the box running out of CPU? vad_bargein has to process 32ms of audio
        # inside 32ms. If silent turns cluster where the box had nothing left,
        # starvation is the mechanism. If they are spread evenly across busy and
        # idle moments, CPU is not what does it.
        voice = [r for r in rows if is_voice_turn(r)]
        headroom = [r for r in voice if r.get("idle_at_turn") is not None]
        silent_total = sum(1 for r in voice if is_silent(r))
        silent_tagged = sum(1 for r in headroom if is_silent(r))
        # Coverage first. This table once read "CPU is not the cause" while
        # covering none of the failing turns, because headroom came from the
        # harness's records and a turn it never saw has no record to tag. A
        # verdict drawn from the successes only is worse than no verdict.
        if voice and len(headroom) < len(voice) * 0.9:
            w(f"\n  *** CPU headroom is known for only {len(headroom)} of {len(voice)} turns,")
            w(f"  *** and for {silent_tagged} of the {silent_total} silent ones. Those are the turns")
            w("  *** the question is about, so any verdict below is drawn from the wrong")
            w("  *** sample. This happens when the run predates t0_epoch; regenerate it:")
            w("  ***    python rebuild.py results/<run>.events.ndjson")
            w("  ***    cp results/<run>-rebuilt.json results/<run>.json")
        if len(headroom) >= 10 and silent_tagged >= 3:
            bands = [(0, 5, "none left (under 5%)"), (5, 20, "tight (5-20%)"),
                     (20, 50, "some (20-50%)"), (50, 101, "plenty (over 50%)")]
            w("\n  by how much of the box was free at that moment:")
            w(f"    {'CPU free':<24}{'turns':<9}{'silent':<14}")
            seen = []
            for lo, hi, name in bands:
                v = [r for r in headroom if lo <= r["idle_at_turn"] < hi]
                if not v:
                    continue
                sil = sum(1 for r in v if is_silent(r))
                seen.append((name, sil / len(v), len(v)))
                w(f"    {name:<24}{len(v):<9}"
                  + f"{sil}/{len(v)} ({sil / len(v):.0%})".ljust(14))
            if len(seen) >= 2:
                rates = [x[1] for x in seen]
                # Starvation would show a gradient: the less CPU was free, the
                # more silence. Comparing only the first band against the last
                # called a non-monotonic shape a trend, so the whole run of
                # bands has to agree now.
                falling = all(a >= b - 0.02 for a, b in zip(rates, rates[1:]))
                spread = max(rates) - min(rates)
                if falling and spread > 0.15:
                    w("\n     Silence falls steadily as CPU frees up, across every band.")
                    w("     vad_bargein has to keep up with real time, and starved of CPU it")
                    w("     stops doing so - that is the link between load and hallucinated")
                    w("     transcripts, and it means freeing CPU fixes accuracy as well as")
                    w("     capacity.")
                elif spread <= 0.1:
                    w("\n     Silence happens at about the same rate whether the box was busy")
                    w("     or idle. CPU is not what causes it - freeing CPU will raise how")
                    w("     many calls you carry but will not fix the transcripts.")
                else:
                    worst = max(seen, key=lambda x: x[1])
                    best = min(seen, key=lambda x: x[1])
                    w(f"\n     No clean gradient. The worst band is '{worst[0]}' at {worst[1]:.0%}")
                    w(f"     and the best is '{best[0]}' at {best[1]:.0%}, but the bands in between do")
                    w("     not line up in order, which is not what starvation looks like -")
                    w("     that would get steadily worse as headroom shrinks.")
                    w("     Treat this as suggestive at most. CPU headroom also tracks how many")
                    w("     calls are running and how far into them you are, so a difference")
                    w("     between the busiest and quietest moments may be either of those.")

        by_turn = {}
        for r in rows:
            if r["turn"] is not None:
                by_turn.setdefault(r["turn"], []).append(r)
        if by_turn:
            w("\n  by turn number (does a conversation go deaf partway through?):")
            w(f"    {'turn':<7}{'turns':<8}{'silent':<14}{'calls up then':<15}")
            trend = []
            for t in sorted(by_turn)[:16]:
                rs = [x for x in by_turn[t] if is_voice_turn(x)]
                if not rs:
                    w(f"    {t:<7}{'-':<8}{'(DTMF turn - no recording is made)':<14}")
                    continue
                sil = sum(1 for x in rs if is_silent(x))
                cs = [x["concurrency"] for x in rs if x["concurrency"]]
                conc = round(statistics.median(cs)) if cs else None
                trend.append((t, sil / len(rs), conc))
                w(f"    {t:<7}{len(rs):<8}"
                  + f"{sil}/{len(rs)} ({sil / len(rs):.0%})".ljust(14)
                  + f"{conc if conc is not None else '-':<15}")
            w("     'calls up then' is how many calls were still running at that turn.")

            # Turn index and concurrency move in opposite directions here: calls
            # start together and drain, so late turns run at LOW concurrency. If
            # those are the silent ones, load is not what causes this.
            if len(trend) >= 4:
                early, late = trend[:len(trend) // 2], trend[len(trend) // 2:]
                e_sil = statistics.fmean([x[1] for x in early])
                l_sil = statistics.fmean([x[1] for x in late])
                e_c = [x[2] for x in early if x[2]]
                l_c = [x[2] for x in late if x[2]]
                e_avg = statistics.fmean(e_c) if e_c else None
                l_avg = statistics.fmean(l_c) if l_c else None
                # Only a genuinely lower concurrency late in the run rules load
                # out. Equal averages were being reported as "LOWER (17 vs 17)".
                if (e_avg and l_avg and l_sil > e_sil * 1.5
                        and l_avg < e_avg * 0.95):
                    w(f"\n     Late turns are far more silent ({l_sil:.0%} vs {e_sil:.0%}) while running at")
                    w(f"     LOWER concurrency ({l_avg:.0f} calls vs {e_avg:.0f}). Load cannot explain that.")
                    w("     Something accumulates over the life of a call - the longer it runs,")
                    w("     the less audio gets through - so any correlation with concurrency")
                    w("     above is turn number in disguise.")
                elif e_avg and l_avg and l_sil > e_sil * 1.5:
                    w(f"\n     Late turns are far more silent ({l_sil:.0%} vs {e_sil:.0%}) at much the same")
                    w(f"     concurrency ({l_avg:.0f} calls vs {e_avg:.0f}). Whatever causes it tracks how")
                    w("     far into a call you are, not how many calls are running.")
    else:
        w("  (not enough matched turns to say)")
    w("")


if __name__ == "__main__":
    main()
