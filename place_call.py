#!/usr/bin/env python3
"""Place one scripted test call to the PBX.

    python3 place_call.py                                  # the default scenario
    python3 place_call.py -s identify-only
    python3 place_call.py -s pbx-ladder --dtmf 9073750302
    python3 place_call.py --caller-id 9075550100
    python3 place_call.py --list                           # what scenarios exist
    python3 place_call.py -s pbx-ladder --show             # resolve it, place nothing

What the call says lives in scenarios/*.json, not in code. Everything a run
usually needs to change - which script, which recordings, the number keyed in,
the number the PBX thinks is calling - is a flag here or a line in that JSON.

Three numbers are easy to confuse, so, plainly:

  --dest       what we dial, the number that reaches Jane
  --caller-id  the number we appear to be calling FROM. Jane tries to match a
               patient on it first, so it is the caller's phone number - not
               anything internal to Asterisk
  --dtmf       the digits typed mid-call when Jane asks for the order number.
               This OVERRIDES caller-id for the patient lookup

The PBX records the call itself (MixMonitor), under
/usr/local/share/asterisk/sounds/call_sessions/<caller-id>/ on the Asterisk box.
Nothing is recorded locally.
"""

import argparse
import os
import sys
from pathlib import Path

import callscript

DEFAULT_SCENARIO = "pbx-ladder"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Place one scripted test call to the PBX.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    p.add_argument("-s", "--scenario", default=os.getenv("SCENARIO", DEFAULT_SCENARIO),
                   help=f"scenario name from scenarios/, or a path to a .json file "
                        f"(default: {DEFAULT_SCENARIO})")
    p.add_argument("--caller-id", default=None,
                   help="number the PBX should see as the caller. Defaults to CALLER_ID_NUMBER, "
                        "then CALLER_USER, from .env")
    p.add_argument("--dtmf", default=None,
                   help="digits for the script's {phone} placeholder - the order number keyed in "
                        "when Jane asks")
    p.add_argument("--dest", default=None,
                   help="number to dial. Defaults to DEST_NUMBER from .env")
    p.add_argument("--host", default=None,
                   help="Asterisk box to call. Defaults to ASTERISK_HOST from .env")
    p.add_argument("--caller-user", default=None,
                   help="SIP account to authenticate as. Defaults to CALLER_USER from .env")
    p.add_argument("--caller-pass", default=None,
                   help="SIP password. Prefer CALLER_PASS in .env - a password on the "
                        "command line lands in your shell history")
    p.add_argument("--caller-display", default=None,
                   help="SIP display name. Defaults to CALLER_DISPLAY from .env")
    p.add_argument("--audio-dir", default=None,
                   help="where the wav files live. Defaults to INPUT_AUDIO_DIR from .env, "
                        "or input_audios/")
    p.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                   help="fill any other {placeholder} in the script. Repeatable")
    p.add_argument("--max-seconds", type=int, default=None,
                   help="hang up after this long no matter what. Defaults to MAX_CALL_SECONDS")
    p.add_argument("--pai", action="store_true",
                   help="also send P-Asserted-Identity, for a PBX that takes caller ID from "
                        "there rather than from the From header")
    p.add_argument("--no-audio-check", action="store_true",
                   help="skip the 8 kHz / mono / 16-bit check on each wav")
    p.add_argument("--check-audio", action="store_true",
                   help="check every wav in the audio dir and exit, without calling")
    p.add_argument("--allow-transfer", action="store_true",
                   help="place the call even with no way to stop it bridging to a live "
                        "GSR agent. Only for deliberately testing the transfer path")
    p.add_argument("--list", action="store_true", help="list scenarios and exit")
    p.add_argument("--show", action="store_true",
                   help="print the resolved script and exit without calling")
    return p.parse_args(argv)


def parse_vars(pairs):
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--var wants NAME=VALUE, got {pair!r}")
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            raise SystemExit(f"--var wants a name before the =, got {pair!r}")
        out[name] = value
    return out


def list_scenarios():
    names = callscript.available_scenarios()
    if not names:
        print(f"no scenarios in {callscript.SCENARIO_DIR}")
        return 1
    print(f"scenarios in {callscript.SCENARIO_DIR}:")
    for name in names:
        path = callscript.SCENARIO_DIR / f"{name}.json"
        try:
            import json
            notes = (json.loads(path.read_text(encoding="utf-8")).get("notes") or "").strip()
        except Exception:
            notes = ""
        first_sentence = notes.split(". ")[0][:96] if notes else ""
        print(f"  {name:<16} {first_sentence}")
    return 0


def transfer_protection(config):
    """What is missing before a transfer to a live agent would be caught.

    The dialplan reaches GSR with Dial(PJSIP/368@CUCM_Trunk), not a REFER, so
    there is no SIP request to decline - the channel is simply bridged and a real
    person answers a robot. The AMI event listener is the only thing that sees it
    coming, and every part of that is off or empty by default.
    """
    missing = []
    if not config.USE_AMI_READY_EVENTS:
        missing.append("USE_AMI_READY_EVENTS=1   (starts the AMI listener at all)")
    if not config.AMI_DETECT_TRANSFER:
        missing.append("AMI_DETECT_TRANSFER=1    (watch for the transfer dialplan)")
    if not config.HANGUP_ON_AMI_TRANSFER:
        missing.append("HANGUP_ON_AMI_TRANSFER=1 (hang up instead of staying bridged)")
    if not config.AMI_USER:
        missing.append("AMI_USER=<user>          (from the PBX manager.conf)")
    if not config.AMI_SECRET:
        missing.append("AMI_SECRET=<secret>")
    return missing


def check_audio_dir(audio_dir):
    """Report every wav in the dir: playable, and what it says.

    Answers "are the files I just dropped in usable" without spending a call or
    writing a scenario that mentions them.
    """
    directory = Path(audio_dir)
    if not directory.is_dir():
        print(f"no audio dir {directory}")
        return 1

    # Recursive, so a per-patient subfolder is checked along with the shared
    # answers at the top level.
    wavs = sorted(directory.rglob("*.wav"))
    if not wavs:
        print(f"no .wav files in {directory}")
        return 1

    manifest = callscript.load_manifest(directory)
    print(f"{len(wavs)} wav file(s) under {directory}\n")

    bad = 0
    resampled = 0
    undescribed = []
    for wav in wavs:
        rel = callscript.relative_name(wav, directory)
        problem = callscript.check_wav(wav)
        if problem:
            bad += 1
            print(f"  BAD  {rel}")
            print(f"       {problem}")
            continue

        said = callscript.describe_wav(wav, directory, manifest)
        if said:
            note = f'"{said}"'
        elif said == "":
            note = "(silence)"
        else:
            undescribed.append(rel)
            note = "(not in manifest.json)"
        print(f"  ok   {rel:<40} {note}")

        fmt = callscript.check_wav_format(wav)
        if fmt:
            resampled += 1
            print(f"       note: {fmt}")

    print()
    if bad:
        print(f"{bad} file(s) cannot be played at all. Replace them, then re-run this.")
    if resampled:
        print(f"{resampled} file(s) are not 8 kHz mono 16-bit. They still play - pjsua2 "
              f"resamples them -\n    but converting removes a step between the "
              f"recording and what Jane hears.")
    if undescribed:
        print(f"{len(undescribed)} file(s) have no line in {directory / 'manifest.json'}. "
              f"They still play; adding one labels them in the log and lets "
              f"check_transcripts.py score them.")
    if not bad and not undescribed and not resampled:
        print("every file is playable, native format, and described.")
    return 1 if bad else 0


def main(argv=None):
    args = parse_args(argv)

    if args.list:
        return list_scenarios()

    if args.check_audio:
        import config as _config
        return check_audio_dir(Path(args.audio_dir) if args.audio_dir
                               else _config.INPUT_AUDIO_DIR)

    overrides = parse_vars(args.var)
    if args.dtmf:
        overrides["phone"] = args.dtmf

    # Set before importing config: it reads the environment once, at import, and
    # pjsip_helpers copies the values it needs at import too. Overriding the
    # module attributes afterwards would leave the SIP account built from .env.
    # A scenario may name the number it has to call from - Jane matches a patient
    # on it, so for some scripts it is part of the script, not a per-run choice.
    # --caller-id still wins, and .env fills in when neither names one.
    caller_id = args.caller_id or callscript.peek_caller_id(args.scenario)

    # CALLER_USER goes in before CALLER_ID_NUMBER is read, so an unset
    # --caller-id still defaults to whatever account we authenticate as.
    for value, name in (
        (args.host, "ASTERISK_HOST"),
        (args.caller_user, "CALLER_USER"),
        (args.caller_pass, "CALLER_PASS"),
        (args.caller_display, "CALLER_DISPLAY"),
        (caller_id, "CALLER_ID_NUMBER"),
        (args.dest, "DEST_NUMBER"),
    ):
        if value:
            os.environ[name] = value
    if args.pai:
        os.environ["SEND_PAI"] = "1"
    os.environ["NUM_CALLS"] = "1"

    import config

    audio_dir = Path(args.audio_dir) if args.audio_dir else config.INPUT_AUDIO_DIR

    try:
        script = callscript.load(
            args.scenario,
            audio_dir=audio_dir,
            overrides=overrides,
            caller_id=config.CALLER_ID_NUMBER,
            check_audio=not args.no_audio_check,
        )
    except callscript.ScriptError as e:
        print(f"*** {e}", file=sys.stderr)
        return 2

    print(script.describe())
    print(f"  dial:  {config.DEST_URI}")
    print(f"  as:    caller_id={config.CALLER_ID_NUMBER} "
          f"(SIP account {config.CALLER_USER}, display {config.CALLER_DISPLAY})"
          + ("  +P-Asserted-Identity"
             if config.SEND_PAI or config.CALLER_ID_NUMBER != config.CALLER_USER else ""))
    print(f"  audio: {audio_dir}")

    gaps = transfer_protection(config)
    if gaps:
        print("\n*** live-agent transfer would NOT be blocked. Missing from .env:")
        for line in gaps:
            print(f"      {line}")
        print("    The dialplan bridges to GSR with Dial(), not a REFER, so without the "
              "AMI\n    listener nothing sees the transfer and a real person answers "
              "this call.")
    else:
        print("  guard: live-agent transfer is detected over AMI and hung up on")

    if args.show:
        print("\n--show given, so nothing was dialled.")
        return 0

    if gaps and not args.allow_transfer:
        print("\n*** refusing to dial. Fill those in, or pass --allow-transfer if "
              "reaching a\n    live agent is the thing you mean to test.", file=sys.stderr)
        return 3
    if gaps:
        print("\n*** --allow-transfer given: this call MAY reach a live GSR agent.")

    if config.CALLER_ID_NUMBER != config.CALLER_USER:
        print(f"*** caller ID {config.CALLER_ID_NUMBER} travels in P-Asserted-Identity; the "
              f"call still\n    registers as account {config.CALLER_USER}, which is what "
              f"Asterisk matches the\n    endpoint on. The PBX honours the assertion only "
              f"with trust_id_inbound=yes.\n    Check afterwards: "
              f"grep 'CALLER=' /var/log/asterisk/full | tail -3")

    print(f"\n*** the PBX records this call under "
          f"call_sessions/{config.CALLER_ID_NUMBER}/ on the Asterisk box\n")

    from run_logging import setup_run_logging

    with setup_run_logging() as run_log:
        if run_log.path:
            print(f"*** runner log file: {run_log.path}")
        import runner
        runner.main(
            num_calls=1,
            actions_provider=lambda call_id: script.actions(),
            describe=lambda call_id: f"scenario={script.name} "
                                     f"caller_id={config.CALLER_ID_NUMBER} "
                                     f"steps={len(script.steps)}",
            max_call_seconds=args.max_seconds,
            require_ami=not args.allow_transfer,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
