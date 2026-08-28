# Placing one test call

`place_call.py` dials the PBX once, plays a script, and hangs up. What it says
lives in `scenarios/*.json`, not in code, so changing the conversation, the
recordings, the caller ID or the digits keyed in is a flag or a line of JSON.

Setup — WSL/Ubuntu, `pjsua2`, `.env` — is unchanged and lives in
[setup.md](setup.md). This file is only about running a call once that is done.

```bash
cd ~/test_codes
python3 place_call.py                       # the default script, pbx-ladder
python3 place_call.py --list                # what scripts exist
python3 place_call.py -s identify-only
```

## The numbers, and which one Jane actually uses

`caller_id` is **the caller's phone number**, not anything internal to Asterisk.
Jane looks a patient up on DOB plus a phone number, and that phone number is
`caller_id` unless the caller keys a different one in
(`match_patient.py`, `handle_match_guest_state`):

```python
caller_digits = digits_only(session_data.get("caller_id", ""))
phone_for_lookup = session_data.get("alt_phone_digits") or caller_digits
patients = get_patient_records(dob_parsed.isoformat(), phone_for_lookup)
```

`session_id` is unrelated: the PBX mints `${EPOCH}${RAND(100,999)}` per call, and
it is the key into Jane's in-process `session_store` - which conversation a turn
belongs to - plus the name of the recording directory. Nothing sets it but the
PBX, and nothing about identity depends on it.

| Flag | What it is | Where it lands |
|---|---|---|
| `--dest` | what we dial | the number that reaches Jane |
| `--caller-id` | the number we call FROM | Jane's **first** lookup attempt |
| `--dtmf` | digits keyed in mid-call | becomes `alt_phone_digits`, which **overrides** `--caller-id` for the lookup |
| `--host` | the Asterisk box | where the INVITE goes |
| `--caller-user` / `--caller-pass` | the SIP account | digest auth only, separate from `--caller-id` |

```bash
python3 place_call.py --caller-id 9075550100 --dtmf 5408249373
```

Defaults come from `.env`, so the common case is no flags at all. Every flag
overrides `.env` for that one call and changes nothing on disk.

### Why the default caller ID is a number that matches nothing

`--caller-id` defaults to `CALLER_USER` (the SIP account, `1001`), which matches
no patient. That is deliberate and is what the concurrency harness did: the
lookup fails, Jane falls through to `CAPTURE_ALT_PHONE_DTMF`, and the script
keys the real order number in - which is the ladder the shipped scenarios walk.

So: **setting `--caller-id` to a number with a real order changes the
conversation.** Jane matches immediately and never asks for the keypad, so a
script with a `press` step will answer the wrong question from there on. Set it
to a matching number only when that is the path you mean to test, and write a
script without the `press` step for it.

### Caller ID and the SIP account are separate

Auth uses `--caller-user` / `CALLER_PASS`; `--caller-id` only changes the From
header, so one account can present any number. `--pai` also sends
`P-Asserted-Identity`, which some endpoint configs prefer. The dialplan logs
which it took, so one call settles it:

```bash
grep 'CALLER=' /var/log/asterisk/full | tail -5      # CALLER=... raw_cid=...
```

## Two things a test call must never do

### Reach a live GSR agent

The dialplan transfers with `Dial(PJSIP/368@CUCM_Trunk)` - not a SIP REFER - so
there is no request to decline. The channel is bridged and a real person picks up
a robot. The **only** thing that catches it is the AMI event listener, which
notices the transfer dialplan and hangs up first.

Every part of that is off or empty by default, so `place_call.py` refuses to dial
until it is armed:

```
*** live-agent transfer would NOT be blocked. Missing from .env:
      USE_AMI_READY_EVENTS=1   (starts the AMI listener at all)
      AMI_USER=<user>          (from the PBX manager.conf)
      AMI_SECRET=<secret>
```

Fill those in and the preflight prints instead:

```
  guard: live-agent transfer is detected over AMI and hung up on
```

A failed AMI connection is fatal too - it used to print a warning and place the
call anyway. `--allow-transfer` is the deliberate override, for when reaching a
live agent is the thing being tested; it says so loudly before dialling.

### Book a real appointment

Jane has no test mode. Accepting an offered slot is a real write
(`schedule_visit.py`), and so is completing a cancellation (`cancel_visit.py`).
The audio harness cannot read Jane's replies as text, so unlike the text-path
load runner it **cannot** detect a slot offer and abort. The script is the only
guard, which means:

- End the script with `hangup` before the SCHEDULING state can offer anything.
  `catherine-williams.json` does exactly this - its last answer is the location
  preference, then it hangs up.
- Never put a `say yes.wav` where a slot offer could land. A desynced script is
  how an accidental booking happens.
- Finishing screening does submit for real (`screening.py`). That is accepted -
  the text load runs do it too - but it is a write, so know that it happened.

## Writing a script

One JSON file per script in `scenarios/`. One step per turn — the harness plays
or presses the step, then waits for the PBX to finish its next reply — so the
steps line up one-to-one with Jane's prompts. Exactly one verb per step:

```json
{
  "name": "my-script",
  "notes": "what this proves, and how it is expected to end",
  "vars": { "phone": "5408249373" },
  "steps": [
    { "say": "name2.wav" },
    { "press": "{phone}" },
    { "wait": 6 },
    { "hangup": true }
  ]
}
```

| Verb | Does |
|---|---|
| `say` | plays a wav from the audio dir |
| `press` | sends those digits as DTMF, one at a time |
| `wait` | stays silent for that many seconds — how to test the re-ask timer |
| `hangup` | ends the call. Must be the last step |

Any `{placeholder}` is filled from `vars`, then overridden from the command
line. `{phone}` and `{caller_id}` always exist, so `--dtmf` and `--caller-id`
reach a script without editing it; anything else you invent takes `--var`:

```bash
python3 place_call.py -s my-script --var exam=mri --dtmf 9073750302
```

Copy `scenarios/_template.json` to start. A scenario can also be a path, so a
one-off script does not need to live in `scenarios/`:

```bash
python3 place_call.py -s /tmp/experiment.json
```

Check a script resolves before spending a call on it — `--show` prints the
whole thing and dials nothing:

```bash
python3 place_call.py -s pbx-ladder --dtmf 9073750302 --show
```

## Adding a recording

Drop the wav in `input_audios/` and add one line to
`input_audios/manifest.json` saying what it says:

```json
"insurance_aetna.wav": "aetna"
```

The manifest is what labels a step in the run log, and what
`check_transcripts.py` scores the recogniser against — so a new recording is
described once, in one place.

Every wav must be **8000 Hz, mono, 16-bit PCM**. Check the whole folder at any
time — this is the first thing to run after dropping new files in:

```bash
python3 place_call.py --check-audio
```

It lists every file, flags the ones that cannot be played and why, and names the
ones with no manifest line. Convert anything it rejects:

```bash
sox in.wav -r 8000 -c 1 -b 16 input_audios/out.wav
```

The same check runs automatically for the files a script uses, before dialling,
so a bad wav costs a second rather than failing as "could not play" three turns
into a call.

## After the call

The PBX records the call itself, so there is nothing to collect locally. On the
Asterisk box:

```
/usr/local/share/asterisk/sounds/call_sessions/<caller-id>/<caller-id>_<session>_full_conversation_unfiltered.wav
```

The harness prints that directory when it starts. The run's own log goes to
`logs/` (set `RUNNER_LOG_FILE` to put it elsewhere).

## Every flag

```
-s, --scenario     script name from scenarios/, or a path to a .json file
    --caller-id    number the PBX should see as the caller
    --dtmf         digits for {phone} - the order number keyed in
    --dest         number to dial
    --host         Asterisk box to call
    --caller-user  SIP account to authenticate as
    --caller-pass  SIP password (prefer .env - this lands in shell history)
    --caller-display   SIP display name
    --audio-dir    where the wavs live (default: INPUT_AUDIO_DIR, or input_audios/)
    --var K=V      fill any other {placeholder}. Repeatable
    --max-seconds  hang up after this long regardless
    --pai          also send P-Asserted-Identity
    --no-audio-check   skip the 8 kHz / mono / 16-bit check
    --allow-transfer   dial even though a live-agent transfer could not be stopped
    --check-audio  check every wav in the audio dir and exit
    --list         list scripts and exit
    --show         resolve the script, print it, dial nothing
```

## How this relates to the load test

`evaluate.py` and `runner.py` are unchanged and still drive concurrent calls
from `ACTIONS` in `config.py`. `place_call.py` is the single-call path: it
reuses the same call engine and teardown, and only supplies its own script.

`scenarios/pbx-ladder.json` is turn-for-turn the same conversation as
`pbx-parity` in `loadtest/scenarios/jane/default.json`, which drives Jane's text
API directly. Same script over audio and over text — when the two disagree, the
difference is the telephony path.
