import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

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
INPUT_AUDIO_DIR = Path(os.getenv("INPUT_AUDIO_DIR", "input_audios"))

USE_TCP = False
FORCE_BIND_IP = None
FORCE_PUBLIC_IP = None

ACTIONS = [
    ("wav", str(INPUT_AUDIO_DIR / "name2.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "birthday2.wav")),
    ("dtmf", "5408249373"),
    ("wav", str(INPUT_AUDIO_DIR / "yes.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "yes.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "yes.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "height.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "weight.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
    ("wav", str(INPUT_AUDIO_DIR / "no.wav")),
]

SILENCE_PAD_WAV = str(INPUT_AUDIO_DIR / "silence_60s.wav")

NUM_CALLS = int(os.getenv("NUM_CALLS", "1"))
CALL_START_GAP_MS = int(os.getenv("CALL_START_GAP_MS", "200"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "1800"))

MAX_CALLS_HEADROOM = 4
MIN_RUNTIME_MAX_CALLS = 8

SILENCE_AFTER_VOICE_MS = 1500
POLL_MS = 100
POST_REDIRECT_TOTAL_SILENCE_MS = 2000
VOICE_ENERGY_THRESHOLD = 180.0

INITIAL_WAIT_TIMEOUT_SECS = 60
NEXT_TURN_WAIT_TIMEOUT_SECS = 60


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_csv(name, default=""):
    value = os.getenv(name, default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


USE_AMI_READY_EVENTS = env_bool("USE_AMI_READY_EVENTS", False)
AMI_HOST = os.getenv("AMI_HOST", ASTERISK_HOST)
AMI_PORT = int(os.getenv("AMI_PORT", "5038"))
AMI_USER = os.getenv("AMI_USER", "")
AMI_SECRET = os.getenv("AMI_SECRET", "")
AMI_READY_EVENT_NAME = os.getenv("AMI_READY_EVENT_NAME", "TestReadyForInput")
AMI_EVENT_CALLER = os.getenv("AMI_EVENT_CALLER", "")
AMI_TRACE_EVENTS = env_bool("AMI_TRACE_EVENTS", False)
AMI_DETECT_TRANSFER = env_bool("AMI_DETECT_TRANSFER", True)
AMI_TRANSFER_CONTEXT_PREFIXES = os.getenv("AMI_TRANSFER_CONTEXT_PREFIXES", "transfer-")
AMI_TRANSFER_DIAL_TARGETS = os.getenv("AMI_TRANSFER_DIAL_TARGETS", "GSR,@CUCM_Trunk")
HANGUP_ON_AMI_TRANSFER = env_bool("HANGUP_ON_AMI_TRANSFER", True)
AMI_ORBITI_MATCH = env_csv("AMI_ORBITI_MATCH", "orbiti")
AMI_JANE_MATCH = env_csv("AMI_JANE_MATCH", "jane,5100")
AMI_TRANSFER_MATCH = env_csv("AMI_TRANSFER_MATCH", "transfer")

AMI_TRANSFER_CONTEXT_PREFIX_LIST = env_csv(
    "AMI_TRANSFER_CONTEXT_PREFIXES",
    AMI_TRANSFER_CONTEXT_PREFIXES,
)
AMI_TRANSFER_DIAL_TARGET_LIST = env_csv(
    "AMI_TRANSFER_DIAL_TARGETS",
    AMI_TRANSFER_DIAL_TARGETS,
)


def masked_secret(value):
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"
