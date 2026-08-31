import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# SIP and RTP
LOCAL_SIP_PORT = int(os.getenv("LOCAL_SIP_PORT", "5062"))
REMOTE_SIP_PORT = int(os.getenv("REMOTE_SIP_PORT", "5060"))
MEDIA_RTP_PORT = int(os.getenv("MEDIA_RTP_PORT", "4000"))
MEDIA_RTP_PORT_RANGE = int(os.getenv("MEDIA_RTP_PORT_RANGE", "400"))

ASTERISK_HOST = os.getenv("ASTERISK_HOST", "10.29.32.138")
CALLER_USER = os.getenv("CALLER_USER", "1001")
CALLER_PASS = os.getenv("CALLER_PASS", "")
CALLER_DISPLAY = os.getenv("CALLER_DISPLAY", "Rahul")
DEST_NUMBER = os.getenv("DEST_NUMBER", "19073750302")
DEST_URI = f"sip:{DEST_NUMBER}@{ASTERISK_HOST}:{REMOTE_SIP_PORT}"

USE_TCP = env_bool("USE_TCP", False)
FORCE_BIND_IP = os.getenv("FORCE_BIND_IP") or None
FORCE_PUBLIC_IP = os.getenv("FORCE_PUBLIC_IP") or None

# Runtime and logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PJSIP_LOG_LEVEL = int(os.getenv("PJSIP_LOG_LEVEL", "1"))
PJSIP_CONSOLE_LOG_LEVEL = int(os.getenv("PJSIP_CONSOLE_LOG_LEVEL", "0"))
PJSIP_FORCE_CONSOLE_LOG = env_bool("PJSIP_FORCE_CONSOLE_LOG", False)
PY_GIL_SWITCH_INTERVAL = float(os.getenv("PY_GIL_SWITCH_INTERVAL", "0.001"))

NUM_CALLS = int(os.getenv("NUM_CALLS", "1"))
CALL_START_GAP_MS = int(os.getenv("CALL_START_GAP_MS", "250"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "1800"))
MAX_CALLS_HEADROOM = int(os.getenv("MAX_CALLS_HEADROOM", "4"))
MIN_RUNTIME_MAX_CALLS = int(os.getenv("MIN_RUNTIME_MAX_CALLS", "8"))

# Media
MEDIA_THREAD_COUNT = int(os.getenv("MEDIA_THREAD_COUNT", "4"))
TX_CLOCK_RATE = int(os.getenv("TX_CLOCK_RATE", "8000"))
TX_CHANNEL_COUNT = 1
TX_SAMPLE_WIDTH_BYTES = 2
TX_FRAME_PTIME_MS = 20
TX_FRAME_BYTES = (
    TX_CLOCK_RATE
    * TX_CHANNEL_COUNT
    * TX_SAMPLE_WIDTH_BYTES
    * TX_FRAME_PTIME_MS
    // 1000
)
MEDIA_SETUP_ATTEMPTS = int(os.getenv("MEDIA_SETUP_ATTEMPTS", "5"))
MEDIA_SETUP_RETRY_MS = int(os.getenv("MEDIA_SETUP_RETRY_MS", "100"))
TAP_FRAME_DECIMATION = max(1, int(os.getenv("TAP_FRAME_DECIMATION", "5")))

# Turn detection
SILENCE_AFTER_VOICE_MS = int(os.getenv("SILENCE_AFTER_VOICE_MS", "1500"))
POLL_MS = int(os.getenv("POLL_MS", "100"))
POST_REDIRECT_TOTAL_SILENCE_MS = int(
    os.getenv("POST_REDIRECT_TOTAL_SILENCE_MS", "2")
)
VOICE_ENERGY_THRESHOLD = float(os.getenv("VOICE_ENERGY_THRESHOLD", "180.0"))
INITIAL_WAIT_TIMEOUT_SECS = int(os.getenv("INITIAL_WAIT_TIMEOUT_SECS", "60"))
NEXT_TURN_WAIT_TIMEOUT_SECS = int(os.getenv("NEXT_TURN_WAIT_TIMEOUT_SECS", "60"))

# DTMF
DTMF_METHOD = os.getenv("DTMF_METHOD", "rfc2833").lower()
DTMF_DURATION_MS = int(os.getenv("DTMF_DURATION_MS", "200"))
DTMF_SETTLE_MS_PER_DIGIT = int(os.getenv("DTMF_SETTLE_MS_PER_DIGIT", "300"))
DTMF_SETTLE_EXTRA_MS = int(os.getenv("DTMF_SETTLE_EXTRA_MS", "500"))
DTMF_HERD_SPACING_MS = int(os.getenv("DTMF_HERD_SPACING_MS", "50"))
DTMF_GATE_MAX_WAIT_MS = int(os.getenv("DTMF_GATE_MAX_WAIT_MS", "1000"))
DTMF_MIN_PROMPT_VOICE_MS = int(os.getenv("DTMF_MIN_PROMPT_VOICE_MS", "1500"))
DTMF_SILENCE_AFTER_VOICE_MS = int(
    os.getenv("DTMF_SILENCE_AFTER_VOICE_MS", "1200")
)

# AMI ready-event correlation
USE_AMI_READY_EVENTS = env_bool("USE_AMI_READY_EVENTS", True)
AMI_HOST = os.getenv("AMI_HOST", ASTERISK_HOST)
AMI_PORT = int(os.getenv("AMI_PORT", "5038"))
AMI_USER = os.getenv("AMI_USER", "admin")
AMI_SECRET = os.getenv("AMI_SECRET", "")
AMI_READY_EVENT_NAME = os.getenv("AMI_READY_EVENT_NAME", "TestReadyForInput")
AMI_EVENT_CALLER = os.getenv("AMI_EVENT_CALLER", "")
AMI_TRACE_EVENTS = env_bool("AMI_TRACE_EVENTS", False)
AMI_TRACE_ALL_EVENTS = env_bool("AMI_TRACE_ALL_EVENTS", False)
AMI_DIAGNOSTIC_INTERVAL_SECS = float(
    os.getenv("AMI_DIAGNOSTIC_INTERVAL_SECS", "5")
)
AMI_LOGIN_ACTION_ID = "codex-ami-login"
TEST_CALL_ID_HEADER = os.getenv("TEST_CALL_ID_HEADER", "X-Test-Call-Id")
TEST_CALL_ID_FIELDS = ("TestCallId", "Testcallid", "TestcallId", "CallTag", "Value")

# These settings are used by the existing AmiReadyEvents implementation.
AMI_USE_AGI_STREAM_EVENTS = env_bool("AMI_USE_AGI_STREAM_EVENTS", True)
AMI_DETECT_TRANSFER = env_bool("AMI_DETECT_TRANSFER", True)
AMI_TRANSFER_CONTEXT_PREFIXES = os.getenv(
    "AMI_TRANSFER_CONTEXT_PREFIXES", "transfer-"
)
AMI_TRANSFER_DIAL_TARGETS = os.getenv("AMI_TRANSFER_DIAL_TARGETS", "@CUCM_Trunk")
HANGUP_ON_AMI_TRANSFER = env_bool("HANGUP_ON_AMI_TRANSFER", True)
AMI_TRANSFER_CONTEXT_PREFIX_LIST = tuple(
    part.strip()
    for part in AMI_TRANSFER_CONTEXT_PREFIXES.split(",")
    if part.strip()
)
AMI_TRANSFER_DIAL_TARGET_LIST = tuple(
    part.strip()
    for part in AMI_TRANSFER_DIAL_TARGETS.split(",")
    if part.strip()
)

# Test actions and files
INPUT_AUDIO_DIR = Path(os.getenv("INPUT_AUDIO_DIR", "input_audios"))
CALLS_OUTPUT_DIR = os.getenv("CALLS_OUTPUT_DIR", "call_recordings")

ACTIONS = [
    ("wav", "name2.wav"),
    ("wav", "birthday2.wav"),
    ("dtmf", "5408249373#"),
    ("wav", "yes.wav"),
    ("wav", "yes.wav"),
    ("wav", "yes.wav"),
    ("wav", "height.wav"),
    ("wav", "weight.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
    ("wav", "no.wav"),
]


def validate_config() -> None:
    if NUM_CALLS < 1:
        raise ValueError("NUM_CALLS must be at least 1")
    if DTMF_METHOD not in {"rfc2833", "sip_info"}:
        raise ValueError("DTMF_METHOD must be 'rfc2833' or 'sip_info'")
    if TX_CLOCK_RATE <= 0 or TX_FRAME_BYTES <= 0:
        raise ValueError("TX_CLOCK_RATE must produce a non-zero 20 ms frame")
    if MEDIA_SETUP_ATTEMPTS < 1:
        raise ValueError("MEDIA_SETUP_ATTEMPTS must be at least 1")
    if DTMF_DURATION_MS < 40:
        raise ValueError("DTMF_DURATION_MS must be at least 40 ms")
