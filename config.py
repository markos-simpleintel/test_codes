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

# --- caller ID, separate from the SIP account -------------------------------
#
# The dialplan reads CALLERID(num) and uses it as the patient's phone number
# (extensions_custom.conf: CALLER=${FILTER(0-9,${CALLERID(num)})}), so the
# number the PBX thinks is calling decides which patient is looked up. It used
# to be CALLER_USER, which is also the digest auth username - meaning testing a
# different caller needed a different PBX account.
#
# CALLER_ID_NUMBER is the number the call asserts, sent in P-Asserted-Identity.
# It does NOT go in the From header: Asterisk identifies the inbound endpoint by
# the From user, so a caller ID there matches no endpoint and the call lands on
# PJSIP/anonymous, never reaching the AI context. Whether the PAI is honoured is
# the endpoint's trust_id_inbound setting:
#     asterisk -rx "pjsip show endpoint <CALLER_USER>" | grep trust_id
# SEND_PAI forces the header even when the number equals the account. Either way
# the dialplan logs what it settled on: NoOp CALLER=... raw_cid=...
CALLER_ID_NUMBER = os.getenv("CALLER_ID_NUMBER", "") or CALLER_USER
SEND_PAI = os.getenv("SEND_PAI", "").strip().lower() in ("1", "true", "yes", "on")

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


# setup.md documents these as .env settings, so read them from there. They were
# hardcoded, which meant NUM_CALLS=40 in .env silently placed one call.
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


# --- per-call identities -----------------------------------------------------
#
# Every call playing the same name, date of birth and number means N concurrent
# calls are one patient. Against a scheduler that is not a load test - it is N
# operations contending for a single record, which can look like slowness that
# real callers would never produce.
#
# The DTMF field is dialled digits, so it has to stay a valid 10-digit number or
# the PBX normalizes it away. 555-01xx is the range reserved as permanently
# fictional, so these are well-formed, never route anywhere real, and are a
# single grep to find afterwards.
#
# Off by default: the dialled number has to match an existing order, so made-up
# numbers fail patient lookup and the call is transferred out three turns in,
# before reaching the scheduling turns worth measuring. Turn this on once test
# orders exist for the 907555-01xx block.

VARY_IDENTITIES = env_bool("VARY_IDENTITIES", False)
TEST_NPA_NXX = os.getenv("TEST_NPA_NXX", "907555")     # area code + exchange
TEST_LINE_START = int(os.getenv("TEST_LINE_START", "100"))
FIXED_DTMF = os.getenv("FIXED_DTMF", "5408249373")


def test_phone_for(call_id):
    """A valid 10-digit number in the reserved fictional block."""
    line = (TEST_LINE_START + call_id - 1) % 10000
    return f"{TEST_NPA_NXX}{line:04d}"


def _wav_pool(pattern, fallback):
    """Every matching file, so dropping more recordings in varies the identity
    further with no code change. Falls back to the original single file."""
    try:
        found = sorted(p for p in INPUT_AUDIO_DIR.glob(pattern) if p.is_file())
    except OSError:
        found = []
    return [str(p) for p in found] or [str(INPUT_AUDIO_DIR / fallback)]


NAME_WAVS = _wav_pool("name*.wav", "name2.wav")
BIRTHDAY_WAVS = _wav_pool("birthday*.wav", "birthday2.wav")


def identity_for(call_id):
    if not VARY_IDENTITIES:
        return {
            "name_wav": str(INPUT_AUDIO_DIR / "name2.wav"),
            "birthday_wav": str(INPUT_AUDIO_DIR / "birthday2.wav"),
            "dtmf": FIXED_DTMF,
        }
    i = call_id - 1
    return {
        "name_wav": NAME_WAVS[i % len(NAME_WAVS)],
        "birthday_wav": BIRTHDAY_WAVS[i % len(BIRTHDAY_WAVS)],
        "dtmf": test_phone_for(call_id),
    }


def actions_for(call_id):
    """ACTIONS with this call's identity substituted in."""
    ident = identity_for(call_id)
    out = []
    for action_type, value in ACTIONS:
        if action_type == "dtmf":
            out.append((action_type, ident["dtmf"]))
        elif value.endswith("name2.wav"):
            out.append((action_type, ident["name_wav"]))
        elif value.endswith("birthday2.wav"):
            out.append((action_type, ident["birthday_wav"]))
        else:
            out.append((action_type, value))
    return out


def describe_identity(call_id):
    ident = identity_for(call_id)
    return (f"phone={ident['dtmf']} "
            f"name={Path(ident['name_wav']).name} "
            f"dob={Path(ident['birthday_wav']).name}")


USE_AMI_READY_EVENTS = env_bool("USE_AMI_READY_EVENTS", False)
AMI_HOST = os.getenv("AMI_HOST", ASTERISK_HOST)
AMI_PORT = int(os.getenv("AMI_PORT", "5038"))
AMI_USER = os.getenv("AMI_USER", "")
AMI_SECRET = os.getenv("AMI_SECRET", "")
AMI_READY_EVENT_NAME = os.getenv("AMI_READY_EVENT_NAME", "TestReadyForInput")
AMI_EVENT_CALLER = os.getenv("AMI_EVENT_CALLER", "")
AMI_TRACE_EVENTS = env_bool("AMI_TRACE_EVENTS", False)
AMI_USE_AGI_STREAM_EVENTS = env_bool("AMI_USE_AGI_STREAM_EVENTS", True)
AMI_DETECT_TRANSFER = env_bool("AMI_DETECT_TRANSFER", True)
AMI_TRANSFER_CONTEXT_PREFIXES = os.getenv("AMI_TRANSFER_CONTEXT_PREFIXES", "transfer-")
AMI_TRANSFER_DIAL_TARGETS = os.getenv("AMI_TRANSFER_DIAL_TARGETS", "@CUCM_Trunk")
HANGUP_ON_AMI_TRANSFER = env_bool("HANGUP_ON_AMI_TRANSFER", True)

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
