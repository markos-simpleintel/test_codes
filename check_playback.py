#!/usr/bin/env python3
"""Did the harness actually send audio on every turn?

    python check_playback.py results/knee-40calls.runner.log
    python check_playback.py "results/knee-*.runner.log" --config-site ~/pjproject/pjlib/include/pj/config_site.h

The PBX recorded turns containing nothing but zeros, exactly
NO_SPEECH_TIMEOUT_MS long. Two readings fit that: the PBX failed to capture
what the caller said, or the caller never said anything. This checks the
second, because the caller is us.

Every turn should log a playback starting and the same playback finishing. A
start with no finish is a turn where this harness transmitted silence while the
PBX sat listening - and the recording of that silence is what reached Jane.

pjsua2 allocates players from a fixed table sized at compile time
(PJSUA_MAX_PLAYERS, 32 by default). Exhausting it makes createPlayer fail with
PJ_ETOOMANY, and call_session.py catches that, restarts the keepalive silence,
and returns without advancing the turn - so the call goes quiet and stays quiet.
"""

import argparse
import glob
import re
import sys
from collections import defaultdict
from pathlib import Path

CALL = r"\[call-(?P<call>\d+)\]\s*"
PATTERNS = {
    "start":        re.compile(CALL + r"starting playback: (?P<path>\S+)"),
    "finish":       re.compile(CALL + r"local WAV finished: (?P<path>\S+)"),
    "play_failed":  re.compile(CALL + r"playback start failed for (?P<path>\S+): (?P<err>.*)"),
    "ka_start_bad": re.compile(CALL + r"failed to start RTP keepalive silence: (?P<err>.*)"),
    "ka_stop_bad":  re.compile(CALL + r"failed to stop RTP keepalive silence: (?P<err>.*)"),
    "ka_on":        re.compile(CALL + r"RTP keepalive silence started"),
    "dtmf":         re.compile(CALL + r"sending DTMF"),
}

# pjsua2 shouting about a full fixed-size table, in either form it takes.
TOO_MANY = re.compile(r"PJ_ETOOMANY|status=70010|Too many objects", re.I)
PJ_ERROR = re.compile(r"status=(\d{4,6})")

LIMITS = ("PJSUA_MAX_PLAYERS", "PJSUA_MAX_CONF_PORTS", "PJSUA_MAX_CALLS",
          "PJ_IOQUEUE_MAX_HANDLES", "PJSUA_MAX_RECORDERS")


def scan(paths):
    calls = defaultdict(lambda: defaultdict(int))
    errors = defaultdict(int)
    too_many = 0
    first_fail_turn = {}

    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"*** cannot read {path}: {e}", file=sys.stderr)
            continue
        for line in text.splitlines():
            if TOO_MANY.search(line):
                too_many += 1
            for name, pat in PATTERNS.items():
                m = pat.search(line)
                if not m:
                    continue
                cid = int(m.group("call"))
                calls[cid][name] += 1
                if name in ("play_failed", "ka_start_bad"):
                    err = (m.groupdict().get("err") or "").strip() or "<blank>"
                    errors[err[:70]] += 1
                    first_fail_turn.setdefault(cid, calls[cid]["start"])
                break
    return calls, errors, too_many, first_fail_turn


def read_limits(path):
    if not path:
        for guess in ("/usr/local/include/pj/config_site.h",
                      str(Path.home() / "pjproject/pjlib/include/pj/config_site.h")):
            if Path(guess).exists():
                path = guess
                break
    if not path or not Path(path).exists():
        return None, {}
    found = {}
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*#\s*define\s+(\w+)\s+(\d+)", line)
            if m and m.group(1) in LIMITS:
                found[m.group(1)] = int(m.group(2))
    except OSError:
        return path, {}
    return path, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+", help="runner.log file(s) or a glob")
    ap.add_argument("--config-site", default=None,
                    help="path to config_site.h, to read the compile-time table sizes")
    args = ap.parse_args()

    paths = []
    for pat in args.logs:
        paths.extend(sorted(glob.glob(pat)) or [pat])

    calls, errors, too_many, first_fail = scan(paths)
    if not calls:
        sys.exit("no per-call playback lines found - is that a runner.log?")

    starts = sum(c["start"] for c in calls.values())
    finishes = sum(c["finish"] for c in calls.values())
    failed = sum(c["play_failed"] for c in calls.values())
    ka_bad = sum(c["ka_start_bad"] + c["ka_stop_bad"] for c in calls.values())
    stuck = starts - finishes - failed

    print("\n" + "=" * 76)
    print(f"  DID WE ACTUALLY SPEAK?   {len(calls)} calls   {len(paths)} log file(s)")
    print("=" * 76)

    print("\nPLAYBACK ATTEMPTS")
    print(f"  playbacks started        {starts}")
    print(f"  playbacks finished       {finishes}")
    print(f"  failed to start          {failed}")
    print(f"  started, never finished  {stuck}"
          + ("   <-- these turns sent silence" if stuck > 0 else ""))
    print(f"  keepalive errors         {ka_bad}")
    if starts:
        silent = failed + max(0, stuck)
        print(f"\n  {silent} of {starts} turns ({silent / starts:.0%}) transmitted nothing.")

    if errors:
        print("\nWHY PLAYBACK FAILED")
        for err, n in sorted(errors.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {n:>5}x  {err}")
        if too_many:
            print(f"\n  {too_many} line(s) mention a full fixed-size table (PJ_ETOOMANY).")
            print("  pjsua2 allocates players from a table sized at compile time. Once it")
            print("  is full, createPlayer fails and the call cannot speak again.")

    worst = sorted(((cid, c["start"] - c["finish"] - c["play_failed"], c)
                    for cid, c in calls.items()),
                   key=lambda t: -(t[1] + t[2]["play_failed"]))
    bad = [(cid, s, c) for cid, s, c in worst if s > 0 or c["play_failed"]]
    if bad:
        print(f"\nCALLS THAT WENT QUIET  ({len(bad)} of {len(calls)})")
        print(f"    {'call':<7}{'started':<10}{'finished':<11}{'failed':<9}"
              f"{'silent':<9}{'first bad turn':<15}")
        for cid, s, c in bad[:20]:
            print(f"    {cid:<7}{c['start']:<10}{c['finish']:<11}{c['play_failed']:<9}"
                  f"{s + c['play_failed']:<9}{first_fail.get(cid, '-'):<15}")
        if len(bad) > 20:
            print(f"    ... and {len(bad) - 20} more")
        print("\n  'first bad turn' is how many playbacks the call had managed before its")
        print("  first failure. A low number across many calls means the limit is being")
        print("  hit early; a spread means players are leaking as the run goes on.")

    path, limits = read_limits(args.config_site)
    print("\nCOMPILE-TIME LIMITS")
    if limits:
        print(f"  from {path}")
        for k in LIMITS:
            if k in limits:
                print(f"    {k:<26}{limits[k]}")
        players = limits.get("PJSUA_MAX_PLAYERS")
        if players is not None and len(calls) * 2 > players:
            print(f"\n  {len(calls)} calls need up to {len(calls) * 2} players at once - one for")
            print(f"  the prompt and one for the keepalive silence - against a limit of")
            print(f"  {players}. That is not enough, and it is a compile-time constant:")
            print("  raising it means editing config_site.h and rebuilding pjproject.")
        elif players is None:
            print("\n  PJSUA_MAX_PLAYERS is not set here, so it is pjsua2's default of 32.")
            print(f"  {len(calls)} concurrent calls can need up to {len(calls) * 2}.")
    else:
        print("  config_site.h not found - pass --config-site to read the real values.")
        print("  Defaults are PJSUA_MAX_PLAYERS=32 and PJSUA_MAX_CONF_PORTS=254, and 32")
        print(f"  is below what {len(calls)} concurrent calls need.")

    print("\nVERDICT")
    silent_rate = (failed + max(0, stuck)) / starts if starts else 0
    if silent_rate >= 0.10:
        print(f"  This harness failed to transmit on {silent_rate:.0%} of turns. That is")
        print("  enough to be the silence the PBX recorded, so the hallucinated")
        print("  transcripts start here rather than in the recogniser. Fix this before")
        print("  reading anything about capacity from the affected runs.")
    elif failed or stuck > 0:
        # A few failures do not explain a lot of silence, and saying "fix this"
        # on any non-zero count sends you after the wrong thing. Note that a
        # playback in flight when a call hangs up never reports finishing, so a
        # small unfinished count is ordinary teardown rather than a fault.
        print(f"  Only {failed + max(0, stuck)} of {starts} turns ({silent_rate:.1%}) failed to")
        print("  transmit, and a playback still running when its call hangs up never")
        print("  reports finishing - so most of that is ordinary teardown.")
        print("\n  This is NOT enough to explain a large number of silent recordings. If")
        print("  diagnose.py found many more silent turns than the count above, the")
        print("  harness did speak and the audio went missing after it left here -")
        print("  compare the two numbers before chasing this further.")
    else:
        print("  Every playback that started also finished, so the harness spoke on")
        print("  every turn. The silence the PBX recorded came from somewhere between")
        print("  our RTP and the VAD's input - look at the SIP/media path, not here.")
    print()


if __name__ == "__main__":
    main()
