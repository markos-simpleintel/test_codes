"""Load a call script from JSON and turn it into the action list a call runs.

A scenario is a JSON file in scenarios/. One step per turn, one verb per step:

    {"say": "name2.wav"}      play a recording from the audio dir
    {"press": "5408249373"}   send those digits as DTMF, one at a time
    {"wait": 6}               stay silent for six seconds
    {"hangup": true}          hang up

A turn is what the harness does after the PBX finishes speaking, so the steps
line up one-to-one with Jane's prompts. Values may contain {placeholders},
filled from the scenario's own "vars" and then overridden by --var, so --dtmf
and --caller-id reach a script without editing it.

Audio is checked before the call rather than during it: a wrong-format wav
fails as "could not play" three turns in, by which point the call is wasted.
"""

import json
import wave
from pathlib import Path

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
MANIFEST_NAME = "manifest.json"

# What every wav must be for the PBX's ulaw path. setup.md T5 is the same rule.
WAV_RATE = 8000
WAV_CHANNELS = 1
WAV_SAMPLE_WIDTH = 2

VERBS = ("say", "press", "wait", "hangup")


class ScriptError(Exception):
    """A scenario that cannot be run, phrased for whoever wrote it."""


class Step:
    def __init__(self, index, action_type, value, label):
        self.index = index
        self.action_type = action_type
        self.value = value
        self.label = label

    def as_action(self):
        return (self.action_type, self.value)

    def __str__(self):
        return f"{self.index:>2}. {self.label}"


class CallScript:
    def __init__(self, name, path, steps, variables, caller_id, notes):
        self.name = name
        self.path = path
        self.steps = steps
        self.variables = variables
        self.caller_id = caller_id
        self.notes = notes

    def actions(self):
        return [step.as_action() for step in self.steps]

    def describe(self):
        lines = [f"scenario: {self.name}  ({self.path})"]
        if self.notes:
            lines.append(f"  notes: {self.notes}")
        if self.variables:
            joined = " ".join(f"{k}={v}" for k, v in sorted(self.variables.items()) if v)
            lines.append(f"  vars:  {joined}")
        lines.extend(f"  {step}" for step in self.steps)
        return "\n".join(lines)


def available_scenarios(scenario_dir=SCENARIO_DIR):
    """Scenario names, template excluded - it names files nobody has recorded."""
    directory = Path(scenario_dir)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json") if not p.stem.startswith("_"))


def resolve_scenario_path(scenario, scenario_dir=SCENARIO_DIR):
    """A name from scenarios/, or a path to a file anywhere."""
    candidate = Path(scenario)
    if candidate.suffix == ".json" and candidate.exists():
        return candidate

    named = Path(scenario_dir) / f"{Path(scenario).stem}.json"
    if named.exists():
        return named

    known = available_scenarios(scenario_dir)
    raise ScriptError(
        f"no scenario '{scenario}'. Available: {', '.join(known) or '<none>'}. "
        f"Pass a name from {scenario_dir}, or a path to a .json file."
    )


def load_manifest(audio_dir):
    """Filename to spoken text. Missing or unreadable is not an error - the
    manifest only makes logs and transcript scoring readable."""
    path = Path(audio_dir) / MANIFEST_NAME
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def relative_name(path, audio_dir):
    """How a file is written in a scenario: "yes.wav", "catherine/name.wav"."""
    try:
        return Path(path).relative_to(Path(audio_dir)).as_posix()
    except ValueError:
        return Path(path).name


def describe_wav(path, audio_dir, manifest):
    """What a recording says. A subfolder file can be listed either by its path
    or by its bare name, so shared answers need one entry, not one per folder."""
    rel = relative_name(path, audio_dir)
    if rel in manifest:
        return manifest[rel]
    return manifest.get(Path(path).name)


def check_wav(path):
    """None if the file is playable, otherwise why it is not."""
    try:
        with wave.open(str(path), "rb") as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.getnframes()
    except (OSError, wave.Error) as e:
        return f"not a readable WAV ({e})"

    wrong = []
    if rate != WAV_RATE:
        wrong.append(f"{rate} Hz (need {WAV_RATE})")
    if channels != WAV_CHANNELS:
        wrong.append(f"{channels} channels (need {WAV_CHANNELS})")
    if width != WAV_SAMPLE_WIDTH:
        wrong.append(f"{width * 8}-bit (need {WAV_SAMPLE_WIDTH * 8}-bit PCM)")
    if frames == 0:
        wrong.append("no audio frames")
    if wrong:
        return ", ".join(wrong) + ". Convert with: sox in.wav -r 8000 -c 1 -b 16 out.wav"
    return None


def _substitute(value, variables, where):
    try:
        return value.format(**variables)
    except KeyError as e:
        known = ", ".join(sorted(variables)) or "<none>"
        raise ScriptError(
            f"{where}: no value for {{{e.args[0]}}}. Defined: {known}. "
            f"Set it in the scenario's vars, or pass --var {e.args[0]}=..."
        ) from None
    except (IndexError, ValueError) as e:
        raise ScriptError(f"{where}: bad placeholder in {value!r} ({e})") from None


def _step_verb(raw, where):
    used = [v for v in VERBS if v in raw]
    if not used:
        raise ScriptError(f"{where}: no verb. Use one of: {', '.join(VERBS)}.")
    if len(used) > 1:
        raise ScriptError(
            f"{where}: {' and '.join(used)} in one step. One verb per step - "
            f"split it into two steps, they run in order."
        )
    return used[0]


def _resolve_say(raw, variables, audio_dir, manifest, check_audio, where, index):
    name = _substitute(str(raw["say"]), variables, where)
    path = Path(name)
    # Any relative path resolves under the audio dir, subfolders included, so one
    # patient's recordings can live in their own folder next to the shared
    # yes/no answers: {"say": "catherine-williams/name.wav"}.
    if not path.is_absolute():
        path = Path(audio_dir) / path
    if not path.exists():
        raise ScriptError(
            f"{where}: no audio file {path}. Put it in {audio_dir}, or pass --audio-dir."
        )
    if check_audio:
        problem = check_wav(path)
        if problem:
            raise ScriptError(f"{where}: {name} is {problem}")
    said = describe_wav(path, audio_dir, manifest)
    shown = relative_name(path, audio_dir)
    label = f"say   {shown}" + (f'  "{said}"' if said else "")
    return Step(index, "wav", str(path), label)


def _resolve_press(raw, variables, where, index):
    digits = _substitute(str(raw["press"]), variables, where).strip()
    if not digits:
        raise ScriptError(f"{where}: press is empty")
    bad = sorted({c for c in digits if c not in "0123456789*#"})
    if bad:
        raise ScriptError(
            f"{where}: {''.join(bad)} is not a keypad digit. "
            f"Only 0-9, * and # can be dialled."
        )
    return Step(index, "dtmf", digits, f"press {digits}")


def _resolve_wait(raw, where, index):
    try:
        seconds = float(raw["wait"])
    except (TypeError, ValueError):
        raise ScriptError(
            f"{where}: wait wants a number of seconds, got {raw['wait']!r}"
        ) from None
    if seconds <= 0:
        raise ScriptError(f"{where}: wait must be more than 0 seconds")
    return Step(index, "wait", seconds, f"wait  {seconds:g}s of silence")


def _build_step(index, raw, verb, variables, audio_dir, manifest, check_audio, where):
    if verb == "say":
        return _resolve_say(raw, variables, audio_dir, manifest, check_audio, where, index)
    if verb == "press":
        return _resolve_press(raw, variables, where, index)
    if verb == "wait":
        return _resolve_wait(raw, where, index)
    if not raw["hangup"]:
        raise ScriptError(f"{where}: hangup set to false does nothing. Remove the step.")
    return Step(index, "hangup", "", "hangup")


def load(scenario, audio_dir, overrides=None, caller_id=None,
         check_audio=True, scenario_dir=SCENARIO_DIR):
    """Read a scenario and resolve it into a runnable script.

    overrides beat the scenario's own vars, so one file serves every caller and
    every order number the command line hands it.
    """
    path = resolve_scenario_path(scenario, scenario_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ScriptError(f"could not read {path}: {e}") from None
    except ValueError as e:
        raise ScriptError(f"{path} is not valid JSON: {e}") from None

    if not isinstance(data, dict):
        raise ScriptError(f"{path}: expected one scenario object, got {type(data).__name__}")

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScriptError(f"{path}: needs a non-empty steps list")

    effective_caller = caller_id or data.get("caller_id") or ""

    variables = {"caller_id": effective_caller}
    variables.update({str(k): str(v) for k, v in (data.get("vars") or {}).items()})
    variables.update({str(k): str(v) for k, v in (overrides or {}).items()})
    variables.setdefault("phone", "")

    manifest = load_manifest(audio_dir)
    steps = []
    for i, raw in enumerate(steps_raw, 1):
        where = f"{path.name} step {i}"
        if not isinstance(raw, dict):
            raise ScriptError(f"{where}: expected an object such as " '{"say": "yes.wav"}')
        verb = _step_verb(raw, where)
        step = _build_step(i, raw, verb, variables, audio_dir, manifest, check_audio, where)
        if step.action_type == "hangup" and i != len(steps_raw):
            raise ScriptError(f"{where}: hangup is not the last step - the rest would never run")
        steps.append(step)

    return CallScript(
        name=data.get("name") or path.stem,
        path=path,
        steps=steps,
        variables=variables,
        caller_id=effective_caller,
        notes=data.get("notes", ""),
    )
