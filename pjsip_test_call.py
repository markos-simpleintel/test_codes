"""PBX test caller and client-facing acceptance workflow.

Run the sample first, capture the SIP/AMI phone identities, obtain IA confirmation, and
only then run concurrent calls. Each execution produces timestamped logs
and machine-readable evidence in test_results/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "test_results"
ENV_FILE = ROOT / ".env"
REQUIRED_AUDIO = (
    "name2.wav", "birthday2.wav", "yes.wav", "height.wav", "weight.wav",
    "no.wav", "silence_60s.wav",
)
REQUIRED_ENV = ("ASTERISK_HOST", "CALLER_USER", "CALLER_PASS", "DEST_NUMBER")
REQUIRED_AMI_ENV = ("AMI_HOST", "AMI_USER", "AMI_SECRET")
CALL_RE = re.compile(r"\[call-(\d+)]")
IDENTITY_PREFIX = "[TEST CALL] "


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def preflight() -> list[str]:
    cfg = {**read_dotenv(ENV_FILE), **os.environ}
    errors: list[str] = []
    if not ENV_FILE.exists():
        errors.append("Missing .env (copy .env.example to .env and fill in real values).")
    for key in REQUIRED_ENV:
        if not cfg.get(key) or cfg[key] == "replace_me":
            errors.append(f"Missing required configuration: {key}")
    if not enabled(cfg.get("USE_AMI_READY_EVENTS")):
        errors.append("USE_AMI_READY_EVENTS must be 1 so GSR transfers can be detected.")
    for key in REQUIRED_AMI_ENV:
        if not cfg.get(key) or cfg[key] == "replace_me":
            errors.append(f"Missing transfer-monitor configuration: {key}")
    if not enabled(cfg.get("AMI_DETECT_TRANSFER")):
        errors.append("AMI_DETECT_TRANSFER must be 1.")
    if not enabled(cfg.get("HANGUP_ON_AMI_TRANSFER")):
        errors.append("HANGUP_ON_AMI_TRANSFER must be 1.")
    audio_dir = ROOT / cfg.get("INPUT_AUDIO_DIR", "input_audios")
    for name in REQUIRED_AUDIO:
        if not (audio_dir / name).is_file():
            errors.append(f"Missing audio: {audio_dir / name}")
    try:
        __import__("pjsua2")
    except Exception:
        errors.append("Python module pjsua2 is not installed in this environment.")
    return errors


def run_calls(count: int, run_name: str) -> dict:
    RESULTS_DIR.mkdir(exist_ok=True)
    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    log_path = RESULTS_DIR / f"{run_id}_{run_name}.log"
    csv_path = RESULTS_DIR / f"{run_id}_{run_name}_latency.csv"
    child_env = os.environ.copy()
    child_env["NUM_CALLS"] = str(count)

    events: list[dict] = []
    command = [sys.executable, "-m", "sip_load_tester.runner"]
    with log_path.open("w", encoding="utf-8", newline="") as log:
        process = subprocess.Popen(
            command, cwd=ROOT, env=child_env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        assert process.stdout is not None
        for raw in process.stdout:
            now = utc_now()
            line = raw.rstrip("\r\n")
            rendered = f"{stamp(now)} {line}"
            print(rendered, flush=True)
            log.write(rendered + "\n")
            match = CALL_RE.search(line)
            events.append({
                "timestamp_utc": stamp(now),
                "elapsed_ms": round((now - started).total_seconds() * 1000, 1),
                "call_id": int(match.group(1)) if match else "",
                "event": classify_event(line),
                "message": line,
            })
        return_code = process.wait()

    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=(
            "timestamp_utc", "elapsed_ms", "call_id", "event", "message"
        ))
        writer.writeheader()
        writer.writerows(events)

    text = "\n".join(event["message"] for event in events)
    calls_connected = len(set(
        event["call_id"] for event in events
        if event["call_id"] and event["event"] == "call_connected"
    ))
    calls_completed = len(set(
        event["call_id"] for event in events
        if event["call_id"] and event["event"] == "action_sequence_complete"
    ))
    transfer_detected = any(event["event"] == "transfer_detected" for event in events)
    ami_active = "AMI login accepted" in text
    identities = extract_identities(events)
    observed_stages = sorted(set(item.get("stage", "") for item in identities))
    call_metrics = summarize_call_metrics(events, count)
    error_events = [
        event for event in events
        if any(token in event["message"].lower() for token in (
            "websocket", "azure", "whisper", "api error", "failed", "exception"
        ))
    ]
    return {
        "run_id": run_id,
        "started_utc": stamp(started),
        "requested_calls": count,
        "calls_connected": calls_connected,
        "calls_completed": calls_completed,
        "process_exit_code": return_code,
        "ami_monitor_active": ami_active,
        "gsr_transfer_detected": transfer_detected,
        "observed_stages": observed_stages,
        "phone_identity_observations": identities,
        "call_metrics": call_metrics,
        "error_events": error_events,
        "passed": (
            return_code == 0
            and calls_connected == count
            and calls_completed == count
            and ami_active
            and not transfer_detected
        ),
        "log_file": str(log_path),
        "latency_csv": str(csv_path),
    }


def summarize_call_metrics(events: list[dict], count: int) -> list[dict]:
    metrics: list[dict] = []
    for call_id in range(1, count + 1):
        call_events = [event for event in events if event.get("call_id") == call_id]
        first_by_type: dict[str, float] = {}
        for event in call_events:
            first_by_type.setdefault(event["event"], event["elapsed_ms"])
        started = first_by_type.get("call_started")

        def since_start(event_name: str):
            value = first_by_type.get(event_name)
            if started is None or value is None:
                return None
            return round(value - started, 1)

        metrics.append({
            "call_id": call_id,
            "connected": "call_connected" in first_by_type,
            "completed": "action_sequence_complete" in first_by_type,
            "connect_latency_ms": since_start("call_connected"),
            "media_ready_latency_ms": since_start("media_ready"),
            "completion_latency_ms": since_start("action_sequence_complete"),
            "disconnect_latency_ms": since_start("call_disconnected"),
        })
    return metrics


def extract_identities(events: list[dict]) -> list[dict]:
    identities: list[dict] = []
    for event in events:
        message = event.get("message", "")
        if not message.startswith(IDENTITY_PREFIX):
            continue
        try:
            record = json.loads(message[len(IDENTITY_PREFIX):])
        except json.JSONDecodeError:
            continue
        record["captured_timestamp_utc"] = event["timestamp_utc"]
        record["elapsed_ms"] = event["elapsed_ms"]
        identities.append(record)
    return identities


def classify_event(line: str) -> str:
    lower = line.lower()
    if "starting direct invite" in lower:
        return "call_started"
    if "call state: confirmed" in lower:
        return "call_connected"
    if "media is ready" in lower:
        return "media_ready"
    if "starting playback:" in lower:
        return "playback_started"
    if "remote turn" in lower and "finished" in lower:
        return "remote_turn_finished"
    if "action sequence complete" in lower:
        return "action_sequence_complete"
    if "transfer detected" in lower or "transfer via" in lower:
        return "transfer_detected"
    if "call state: disconnected" in lower:
        return "call_disconnected"
    return "log"


def write_report(report: dict, name: str) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{report['run_id']}_{name}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def latest_passing_sample() -> tuple[Path, dict] | None:
    candidates = sorted(RESULTS_DIR.glob("*_single_report.json"), reverse=True) if RESULTS_DIR.exists() else []
    for path in candidates:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("passed") and report.get("phone_identity_observations"):
            return path, report
    return None


def command_preflight(_args: argparse.Namespace) -> int:
    errors = preflight()
    if errors:
        print("PRE-FLIGHT FAILED")
        for error in errors:
            print(f"- {error}")
        return 2
    print("PRE-FLIGHT PASSED: configuration, audio, PJSUA2, and AMI safeguards are ready.")
    return 0


def command_single(args: argparse.Namespace) -> int:
    errors = preflight()
    if errors:
        return command_preflight(args)
    report = run_calls(1, "single")
    report["ia_confirmation"] = "pending"
    path = write_report(report, "single_report")
    print(f"SINGLE TEST {'PASSED' if report['passed'] else 'FAILED'}; report: {path}")
    return 0 if report["passed"] else 1


def command_concurrent(args: argparse.Namespace) -> int:
    args.calls = 2
    return command_load(args, run_name="concurrent")


def command_load(args: argparse.Namespace, run_name: str = "load") -> int:
    errors = preflight()
    if errors:
        return command_preflight(args)
    sample = latest_passing_sample()
    if sample is None:
        print("BLOCKED: no passing single-test report with captured identity observations.")
        return 2
    if not args.ia_confirmed:
        print("BLOCKED: IA must confirm the recorded numbers. Re-run with --ia-confirmed after confirmation.")
        return 2
    sample_path, sample_report = sample
    count = args.calls
    if count < 2 or count > 100:
        print("BLOCKED: --calls must be between 2 and 100.")
        return 2
    report = run_calls(count, f"{run_name}_{count}_calls")
    report["ia_confirmation"] = "confirmed"
    report["single_test_report"] = str(sample_path)
    report["confirmed_identity_observations"] = sample_report["phone_identity_observations"]
    report["azure_reviewers"] = ["Nikita", "Brandon"]
    report["capacity_result"] = "stable" if report["passed"] else "unstable"
    path = write_report(report, f"{run_name}_{count}_calls_report")
    label = "CONCURRENT" if run_name == "concurrent" else "LOAD"
    print(f"{label} TEST {'PASSED' if report['passed'] else 'FAILED'}; report: {path}")
    print(
        f"Capacity level {count}: connected={report['calls_connected']} "
        f"completed={report['calls_completed']} result={report['capacity_result']}"
    )
    print(f"Azure latency evidence for Nikita and Brandon: {report['latency_csv']}")
    return 0 if report["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the approved client PBX test workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight", help="check dependencies without placing calls")
    pre.set_defaults(func=command_preflight)
    single = sub.add_parser("single", help="run one sample test and capture SIP/AMI phone identities")
    single.set_defaults(func=command_single)
    concurrent = sub.add_parser("concurrent", help="run exactly two concurrent tests")
    concurrent.add_argument("--ia-confirmed", action="store_true", help="confirm IA approved the recorded numbers")
    concurrent.set_defaults(func=command_concurrent)
    load = sub.add_parser("load", help="run an approved concurrent-call capacity level")
    load.add_argument("--calls", type=int, required=True, help="concurrent calls (2-100)")
    load.add_argument("--ia-confirmed", action="store_true", help="confirm IA approved load testing")
    load.set_defaults(func=command_load)
    return parser


def main() -> int:
    if len(sys.argv) == 1:
        # Preserve the command used by existing testers.
        from sip_load_tester.runner import main as run_direct_test

        run_direct_test()
        return 0
    args = build_parser().parse_args()
    return args.func(args)
if __name__ == "__main__":
    raise SystemExit(main())
