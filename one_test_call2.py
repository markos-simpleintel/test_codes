
import gc
import os
import math
import socket
import sys
import threading
import time
import uuid
import wave
from pathlib import Path

from dotenv import load_dotenv
import pjsua2 as pj

load_dotenv()

# The pjmedia conference clock is a single C thread, but every TxAudioPort /
# RemoteTap callback it makes must acquire the Python GIL first. CPython's
# default thread switch interval is 5 ms; under contention that is up to a
# 5 ms stall PER CALLBACK on the thread that also paces RFC2833 DTMF events.
# At 50 calls x 100 callbacks/sec that guarantees the 20 ms media tick slips,
# which is exactly what mangles the telephone-event trains. Shrink the
# handoff window so the media thread never waits long for the GIL.
sys.setswitchinterval(float(os.getenv("PY_GIL_SWITCH_INTERVAL", "0.001")))


# =========================
# FRAME ENERGY HELPER
# =========================
# The old implementation did struct.unpack + a pure-Python sum(s*s) on every
# 20 ms frame of every call. At 20 concurrent calls that is ~1000 trips into
# the interpreter per second on the single pjmedia worker thread, which jitters
# the media clock and mangles RFC2833 event timing. audioop.rms is a C loop.
try:
    import audioop as _audioop

    def frame_rms(pcm: bytes) -> float:
        return float(_audioop.rms(pcm, 2))

except ImportError:  # audioop removed in Python 3.13
    _audioop = None
    import array

    def frame_rms(pcm: bytes) -> float:
        usable = len(pcm) // 2 * 2
        if usable <= 0:
            return 0.0
        samples = array.array("h")
        samples.frombytes(pcm[:usable])
        # Decimate: 1-in-4 samples is plenty for a voice/no-voice decision.
        subset = samples[::4]
        if not subset:
            return 0.0
        return math.sqrt(sum(s * s for s in subset) / len(subset))


LOG_LEVELS = {
    "QUIET": 0,
    "ERROR": 1,
    "INFO": 2,
}


def get_log_level() -> int:
    raw_level = os.getenv("LOG_LEVEL", "INFO").upper()
    return LOG_LEVELS.get(raw_level, LOG_LEVELS["INFO"])


ACTIVE_LOG_LEVEL = get_log_level()
_print_lock = threading.Lock()


def log_message(message: str, level: str = "INFO"):
    if LOG_LEVELS[level] <= ACTIVE_LOG_LEVEL:
        with _print_lock:
            print(message, flush=True)


def log_info(message: str):
    log_message(message, "INFO")


def log_error(message: str):
    log_message(message, "ERROR")


# =========================
# CONFIG
# =========================
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

USE_TCP = False
FORCE_BIND_IP = None
FORCE_PUBLIC_IP = None

PJSIP_LOG_LEVEL = int(os.getenv("PJSIP_LOG_LEVEL", "1"))
PJSIP_CONSOLE_LOG_LEVEL = int(os.getenv("PJSIP_CONSOLE_LOG_LEVEL", "0"))

# Number of pjmedia worker threads. Default in PJSUA is 1, which is the single
# biggest scaling limit for a 20-call conference bridge.
MEDIA_THREAD_COUNT = int(os.getenv("MEDIA_THREAD_COUNT", "4"))

# All outbound media is converted once to this format and then supplied by one
# permanent AudioMediaPort per call. Keeping the port connected for the whole
# call avoids conference-port create/connect/disconnect churn between turns.
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
MEDIA_GRAPH_LOCK = threading.RLock()

# --- DTMF -----------------------------------------------------------------
# "rfc2833" -> in-band RTP telephone-event, driven by the media clock.
#              Reliability at high concurrency depends entirely on the media
#              clock not slipping -- see DTMF NOTE at the bottom of this file.
# "sip_info" -> SIP INFO, signalling path, immune to media-thread jitter.
#              THE load-proof option for a test harness. Requires
#              dtmf_mode=info (chan_pjsip: info or auto_info) on the
#              Asterisk endpoint.
DTMF_METHOD = os.getenv("DTMF_METHOD", "rfc2833").lower()
DTMF_DURATION_MS = int(os.getenv("DTMF_DURATION_MS", "200"))
# How long to let the digit queue drain before we start listening for the
# next prompt. pjmedia clocks the digits itself; this is just slack.
DTMF_SETTLE_MS_PER_DIGIT = int(os.getenv("DTMF_SETTLE_MS_PER_DIGIT", "300"))
DTMF_SETTLE_EXTRA_MS = int(os.getenv("DTMF_SETTLE_EXTRA_MS", "500"))

# Minimum spacing between DTMF train starts across ALL calls. Turn detection
# re-synchronizes the herd -- every call hears the same prompt end within the
# same second -- so without a gate 20+ RFC2833 event trains start on the same
# few media ticks, spiking the clock thread exactly when digit pacing matters
# most.
DTMF_HERD_SPACING_MS = int(os.getenv("DTMF_HERD_SPACING_MS", "50"))
# Hard cap on how long one call may sit in the herd gate. The dialplan's
# Read() allows only ~5 s after the prompt to START dialing, and silence
# detection has already consumed DTMF_SILENCE_AFTER_VOICE_MS of that budget.
# A call queued behind dozens of others must never be delayed past the
# window; once the cap is hit it sends immediately even if trains bunch up.
# The 14:31 run showed ~half the calls missing the window at 100 ms / 2000 ms
# -- with the media clock fixes in place, smoothing matters far less than
# landing inside the window, so keep both small.
DTMF_GATE_MAX_WAIT_MS = int(os.getenv("DTMF_GATE_MAX_WAIT_MS", "1000"))
_DTMF_GATE_LOCK = threading.Lock()
_dtmf_last_start_ts = 0.0

# Voice-energy sampling interval for the remote tap, in frames. The tap fires
# 50x/sec per call ON THE MEDIA CLOCK THREAD; copying and RMS-ing every frame
# at 50 calls is 2500 buffer copies + GIL round-trips per second on the thread
# that also paces RFC2833 DTMF. Sampling every 5th frame (100 ms) is more than
# enough resolution for a >=1500 ms silence threshold and cuts that load 5x.
TAP_FRAME_DECIMATION = max(1, int(os.getenv("TAP_FRAME_DECIMATION", "5")))

# Minimum cumulative voiced time (ms) the remote side must produce during the
# wait BEFORE a DTMF action. The dialplan plays short "one moment" fillers
# while its AI backend thinks; 2 s of silence after a filler must not trigger
# digit entry, because the dialplan's DTMF window (x-enable-dtmf turn) only
# opens with the real number prompt. Under load the AI wait gaps grow, which
# is exactly when silence-triggered DTMF starts landing in a closed window.
# 0 disables the check. Voiced time is counted from sampled tap frames.
DTMF_MIN_PROMPT_VOICE_MS = int(os.getenv("DTMF_MIN_PROMPT_VOICE_MS", "1500"))

# Silence threshold for the wait BEFORE a DTMF action only. The dialplan's
# Read() allows ~5 s after the prompt for the FIRST digit, and digits sent
# DURING the prompt are also accepted (Read is barge-in): sending early is
# harmless, sending late is fatal. So react faster here than the generic
# SILENCE_AFTER_VOICE_MS used for voice turns. Budget at 50 calls:
#   silence detect (~this value) + herd gate (<= DTMF_GATE_MAX_WAIT_MS)
# must stay well under Read's first-digit timeout.
DTMF_SILENCE_AFTER_VOICE_MS = int(os.getenv("DTMF_SILENCE_AFTER_VOICE_MS", "1200"))

# --- AMI ------------------------------------------------------------------
USE_AMI_READY_EVENTS = os.getenv("USE_AMI_READY_EVENTS", "1") == "1"
AMI_HOST = os.getenv("AMI_HOST", ASTERISK_HOST)
AMI_PORT = int(os.getenv("AMI_PORT", "5038"))
AMI_USER = os.getenv("AMI_USER", "admin")
AMI_SECRET = os.getenv("AMI_SECRET", "")
AMI_READY_EVENT_NAME = os.getenv("AMI_READY_EVENT_NAME", "TestReadyForInput")
AMI_TRACE_EVENTS = os.getenv("AMI_TRACE_EVENTS", "0") == "1"
AMI_TRACE_ALL_EVENTS = os.getenv("AMI_TRACE_ALL_EVENTS", "0") == "1"
AMI_DIAGNOSTIC_INTERVAL_SECS = float(os.getenv("AMI_DIAGNOSTIC_INTERVAL_SECS", "5"))
AMI_LOGIN_ACTION_ID = "codex-ami-login"

# Correlation key. We stamp every INVITE with this header; the dialplan must
# echo it back so AMI events can be routed to the right MyCall. Without this,
# a ready event from one channel wakes an arbitrary call and every call in the
# run desynchronizes. See CORRELATION NOTE at the bottom of this file.
TEST_CALL_ID_HEADER = os.getenv("TEST_CALL_ID_HEADER", "X-Test-Call-Id")
# AMI event fields we will look at when hunting for the id, in priority order.
TEST_CALL_ID_FIELDS = ("TestCallId", "Testcallid", "TestcallId", "CallTag", "Value")

INPUT_AUDIO_DIR = Path(os.getenv("INPUT_AUDIO_DIR", "input_audios"))

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

CALLS_OUTPUT_DIR = os.getenv("CALLS_OUTPUT_DIR", "call_recordings")

# Safe default for a script named one_test_call2.py. Load runs must opt in by
# setting NUM_CALLS explicitly.
NUM_CALLS = int(os.getenv("NUM_CALLS", "1"))
CALL_START_GAP_MS = int(os.getenv("CALL_START_GAP_MS", "250"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "1800"))

MAX_CALLS_HEADROOM = 4
MIN_RUNTIME_MAX_CALLS = 8

SILENCE_AFTER_VOICE_MS = int(os.getenv("SILENCE_AFTER_VOICE_MS", "1500"))
POLL_MS = int(os.getenv("POLL_MS", "100"))
POST_REDIRECT_TOTAL_SILENCE_MS = int(os.getenv("POST_REDIRECT_TOTAL_SILENCE_MS", "2"))
VOICE_ENERGY_THRESHOLD = float(os.getenv("VOICE_ENERGY_THRESHOLD", "180.0"))

INITIAL_WAIT_TIMEOUT_SECS = int(os.getenv("INITIAL_WAIT_TIMEOUT_SECS", "60"))
NEXT_TURN_WAIT_TIMEOUT_SECS = int(os.getenv("NEXT_TURN_WAIT_TIMEOUT_SECS", "60"))


def safe_set(obj, attr, value):
    if not hasattr(obj, attr):
        return False
    try:
        setattr(obj, attr, value)
        return True
    except Exception as e:
        log_error(f"*** could not set {obj.__class__.__name__}.{attr}: {e}")
        return False


def detect_local_ip_for_remote(remote_host: str, remote_port: int) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, remote_port))
        return s.getsockname()[0]
    finally:
        s.close()


def get_bind_ip() -> str:
    if FORCE_BIND_IP:
        return FORCE_BIND_IP
    return detect_local_ip_for_remote(ASTERISK_HOST, REMOTE_SIP_PORT)


def configure_codecs(ep: pj.Endpoint):
    try:
        codec_infos = ep.codecEnum2()
    except Exception as e:
        log_error(f"*** codec enumeration failed: {e}")
        return

    # telephone-event is NOT a codec in pjmedia's codec list, so this filter
    # does not disable RFC2833 -- but verify in the SDP that Asterisk actually
    # negotiated telephone-event/8000 before blaming the media thread.
    keep_prefixes = ("PCMU/8000",)

    for c in codec_infos:
        codec_id = c.codecId
        prio = 255 if any(codec_id.startswith(p) for p in keep_prefixes) else 0
        try:
            ep.codecSetPriority(codec_id, prio)
        except Exception as e:
            log_error(f"*** failed to set codec priority for {codec_id}: {e}")

    log_info("*** codec priority update done")


def make_transport(ep: pj.Endpoint, bind_ip: str) -> int:
    tp_cfg = pj.TransportConfig()
    tp_cfg.port = LOCAL_SIP_PORT
    safe_set(tp_cfg, "boundAddress", bind_ip)

    if FORCE_PUBLIC_IP:
        safe_set(tp_cfg, "publicAddress", FORCE_PUBLIC_IP)
        log_info(f"*** forcing public SIP address: {FORCE_PUBLIC_IP}")

    if USE_TCP:
        tp_type = pj.PJSIP_TRANSPORT_TCP
        log_info("*** using TCP transport")
    else:
        tp_type = pj.PJSIP_TRANSPORT_UDP
        log_info("*** using UDP transport")

    return ep.transportCreate(tp_type, tp_cfg)


def configure_endpoint(ep_cfg: pj.EpConfig):
    ep_cfg.logConfig.level = PJSIP_LOG_LEVEL

    # PJSIP console logging is a synchronous fwrite under a global log mutex.
    # At level 4 every SIP message is dumped in full, and media threads that
    # log ("Resetting jitter buffer", "codec parsed 0 frames") block behind
    # those writes -- through `tee` in run_one_test_call.sh that is pipe I/O
    # inside the media path. Clamp it for load runs; set
    # PJSIP_FORCE_CONSOLE_LOG=1 to keep full traces anyway.
    console_level = PJSIP_CONSOLE_LOG_LEVEL
    if (
        NUM_CALLS > 10
        and console_level > 2
        and os.getenv("PJSIP_FORCE_CONSOLE_LOG", "0") != "1"
    ):
        log_error(
            f"*** clamping PJSIP console log level {console_level} -> 2 for "
            f"{NUM_CALLS} concurrent calls (synchronous SIP tracing jitters "
            "the media clock; set PJSIP_FORCE_CONSOLE_LOG=1 to override)"
        )
        console_level = 2
    ep_cfg.logConfig.consoleLevel = console_level

    safe_set(ep_cfg.uaConfig, "userAgent", "")
    safe_set(ep_cfg.uaConfig, "natTypeInSdp", 0)
    safe_set(ep_cfg.uaConfig, "enableUpnp", False)

    requested_max_calls = max(NUM_CALLS + MAX_CALLS_HEADROOM, MIN_RUNTIME_MAX_CALLS)
    if not safe_set(ep_cfg.uaConfig, "maxCalls", requested_max_calls):
        log_error("*** warning: could not set uaConfig.maxCalls")
    else:
        log_info(f"*** uaConfig.maxCalls = {requested_max_calls}")

    ep_cfg.medConfig.clockRate = 8000
    ep_cfg.medConfig.channelCount = 1
    ep_cfg.medConfig.sndClockRate = 8000
    safe_set(ep_cfg.medConfig, "audioFramePtime", TX_FRAME_PTIME_MS)
    ep_cfg.medConfig.quality = 4
    ep_cfg.medConfig.noVad = True
    ep_cfg.medConfig.sndAutoCloseTime = -1

    # Each call uses roughly four ports: call audio, permanent TX, remote tap,
    # and recorder. Leave explicit headroom for PJSUA's own bridge ports.
    requested_media_ports = max(
        int(getattr(ep_cfg.medConfig, "maxMediaPorts", 0) or 0),
        NUM_CALLS * 4 + 64,
    )
    if safe_set(ep_cfg.medConfig, "maxMediaPorts", requested_media_ports):
        log_info(f"*** medConfig.maxMediaPorts = {requested_media_ports}")

    if safe_set(ep_cfg.medConfig, "threadCnt", MEDIA_THREAD_COUNT):
        log_info(f"*** medConfig.threadCnt = {MEDIA_THREAD_COUNT}")


def build_account_config(bind_ip: str, transport_id: int) -> pj.AccountConfig:
    acfg = pj.AccountConfig()

    acfg.idUri = f'"{CALLER_DISPLAY}" <sip:{CALLER_USER}@{ASTERISK_HOST}>'
    acfg.regConfig.registerOnAdd = False

    safe_set(acfg.sipConfig, "transportId", transport_id)

    acfg.sipConfig.authCreds.append(
        pj.AuthCredInfo("digest", "*", CALLER_USER, 0, CALLER_PASS)
    )

    safe_set(acfg.sipConfig, "authInitialEmpty", False)
    safe_set(acfg.sipConfig, "useSharedAuth", False)

    safe_set(acfg.sipConfig, "contactForced", f"sip:{CALLER_USER}@{bind_ip}:{LOCAL_SIP_PORT}")
    safe_set(acfg.sipConfig, "contactParams", "")
    safe_set(acfg.sipConfig, "contactUriParams", "")

    safe_set(acfg.callConfig, "prackUse", pj.PJSUA_100REL_NOT_USED)
    safe_set(acfg.callConfig, "timerUse", pj.PJSUA_SIP_TIMER_INACTIVE)

    safe_set(acfg.natConfig, "contactRewriteUse", 0)
    safe_set(acfg.natConfig, "viaRewriteUse", 0)
    safe_set(acfg.natConfig, "sdpNatRewriteUse", 0)
    safe_set(acfg.natConfig, "sipOutboundUse", 0)
    safe_set(acfg.natConfig, "contactUseSrcPort", 0)
    safe_set(acfg.natConfig, "udpKaIntervalSec", 0)

    safe_set(acfg.natConfig, "iceEnabled", False)
    safe_set(acfg.natConfig, "turnEnabled", False)
    safe_set(acfg.natConfig, "iceNoRtcp", True)
    safe_set(acfg.natConfig, "iceAlwaysUpdate", False)

    safe_set(acfg.mediaConfig.transportConfig, "port", MEDIA_RTP_PORT)
    safe_set(acfg.mediaConfig.transportConfig, "portRange", MEDIA_RTP_PORT_RANGE)
    safe_set(acfg.mediaConfig.transportConfig, "boundAddress", bind_ip)

    safe_set(acfg.mediaConfig, "lockCodecEnabled", False)
    safe_set(acfg.mediaConfig, "streamKaEnabled", False)
    safe_set(acfg.mediaConfig, "rtcpXrEnabled", False)
    safe_set(acfg.mediaConfig, "rtcpMuxEnabled", False)

    try:
        safe_set(acfg.mediaConfig.rtcpFbConfig, "dontUseAvpf", True)
    except Exception:
        pass

    return acfg


def is_active_audio_media(media_desc) -> bool:
    if media_desc.type != pj.PJMEDIA_TYPE_AUDIO:
        return False

    status = getattr(media_desc, "status", None)
    active_const = getattr(pj, "PJSUA_CALL_MEDIA_ACTIVE", None)

    if status is not None and active_const is not None and status != active_const:
        return False

    return True


def build_recording_path(kind: str, call_id: int) -> str:
    out_dir = Path(CALLS_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{kind}_{call_id:02d}.wav")


def resolve_input_audio(filename: str) -> str:
    path = Path(filename)
    if path.is_absolute():
        return str(path)
    return str(INPUT_AUDIO_DIR / path)


def load_wav_frames(filename: str):
    """Load one WAV and convert it to fixed 20 ms PCM frames."""
    path = Path(resolve_input_audio(filename))
    if not path.is_file():
        raise FileNotFoundError(f"required input audio is missing: {path}")

    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        source_rate = wav_file.getframerate()
        compression = wav_file.getcomptype()
        pcm = wav_file.readframes(wav_file.getnframes())

    if compression != "NONE":
        raise ValueError(f"{path}: compressed WAV is unsupported ({compression})")
    if channels != TX_CHANNEL_COUNT or sample_width != TX_SAMPLE_WIDTH_BYTES:
        raise ValueError(
            f"{path}: expected mono 16-bit PCM, got "
            f"channels={channels}, sample_width={sample_width}"
        )
    if not pcm:
        raise ValueError(f"{path}: WAV contains no audio frames")

    if source_rate != TX_CLOCK_RATE:
        if _audioop is None:
            raise ValueError(
                f"{path}: sample rate is {source_rate} Hz, but {TX_CLOCK_RATE} Hz is "
                "required and Python audioop is unavailable; convert the file first"
            )
        pcm, _ = _audioop.ratecv(
            pcm,
            TX_SAMPLE_WIDTH_BYTES,
            TX_CHANNEL_COUNT,
            source_rate,
            TX_CLOCK_RATE,
            None,
        )

    frames = []
    for offset in range(0, len(pcm), TX_FRAME_BYTES):
        chunk = pcm[offset:offset + TX_FRAME_BYTES]
        if len(chunk) < TX_FRAME_BYTES:
            chunk += b"\x00" * (TX_FRAME_BYTES - len(chunk))
        frames.append(pj.ByteVector(chunk))

    duration_ms = len(pcm) * 1000 / (
        TX_CLOCK_RATE * TX_CHANNEL_COUNT * TX_SAMPLE_WIDTH_BYTES
    )
    log_info(
        f"*** loaded {filename}: {source_rate} Hz -> {TX_CLOCK_RATE} Hz, "
        f"duration={duration_ms:.0f} ms, frames={len(frames)}"
    )
    return tuple(frames)


def load_audio_assets(actions):
    """Fail before making calls if an action WAV is missing or malformed."""
    assets = {}
    for action_type, action_value in actions:
        if action_type == "wav" and action_value not in assets:
            assets[action_value] = load_wav_frames(action_value)
    return assets


def validate_config():
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


def parse_ami_message(raw_message: str) -> dict:
    event = {}
    for line in raw_message.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        event[key] = value
    return event


class AmiReadyListener:
    """
    Routes Asterisk ready-for-input events to the MyCall they belong to.

    Correlation order:
      1. explicit test-call-id echoed by the dialplan  (reliable)
      2. a Channel we have already mapped to a call     (learned from #1)
      3. a single waiting call                          (safe only at N=1)
    If none match, the event is dropped with a warning. It is never handed to
    an arbitrary call -- that is what desynchronizes a concurrent run.
    """

    def __init__(self):
        self.calls = []
        self._calls_by_test_id = {}
        self._calls_by_channel = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        self._sock = None
        self._message_count = 0
        self._event_count = 0
        self._response_count = 0
        self._uncorrelated_count = 0
        self._event_counts = {}
        self._traced_event_names = set()
        self._last_rx_at = None
        self._last_diag_at = time.time()
        self._authenticated = None

    def add_call(self, call):
        with self._lock:
            self.calls.append(call)
            self._calls_by_test_id[call.test_call_id] = call

    def start(self):
        if not USE_AMI_READY_EVENTS:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _send_action(self, action: str):
        if self._sock is not None:
            self._sock.sendall(action.encode("utf-8"))

    def _run(self):
        try:
            with socket.create_connection((AMI_HOST, AMI_PORT), timeout=10) as sock:
                self._sock = sock
                sock.settimeout(1.0)
                self._send_action(
                    "Action: Login\r\n"
                    f"ActionID: {AMI_LOGIN_ACTION_ID}\r\n"
                    f"Username: {AMI_USER}\r\n"
                    f"Secret: {AMI_SECRET}\r\n"
                    "Events: on\r\n\r\n"
                )
                log_info(
                    f"*** AMI listener connected to {AMI_HOST}:{AMI_PORT}; "
                    f"login sent as user '{AMI_USER}'"
                )

                buffer = ""
                while not self._stop_evt.is_set():
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        self._maybe_log_diagnostics()
                        continue
                    if not data:
                        break

                    self._last_rx_at = time.time()
                    buffer += data.decode("utf-8", errors="replace")
                    while "\r\n\r\n" in buffer:
                        raw_message, buffer = buffer.split("\r\n\r\n", 1)
                        self._handle_message(parse_ami_message(raw_message))
                    self._maybe_log_diagnostics()

        except Exception as e:
            if not self._stop_evt.is_set():
                log_error(f"*** AMI listener error: {e}")
        finally:
            self._sock = None

    # --- correlation ------------------------------------------------------

    def _extract_test_id(self, event: dict):
        for field in TEST_CALL_ID_FIELDS:
            value = event.get(field)
            if value and value in self._calls_by_test_id:
                return value
        # Some dialplans stuff it into AppData / a Varset value.
        for field in ("AppData", "Value", "Command"):
            blob = event.get(field, "")
            for test_id in self._calls_by_test_id:
                if test_id and test_id in blob:
                    return test_id
        return None

    def _resolve_call(self, event: dict):
        channel = event.get("Channel")

        with self._lock:
            test_id = self._extract_test_id(event)
            if test_id:
                call = self._calls_by_test_id.get(test_id)
                if call is not None and channel:
                    # Learn the channel so later events without the header
                    # still route correctly.
                    self._calls_by_channel[channel] = call
                if call is not None:
                    return call, "test-id"

            if channel and channel in self._calls_by_channel:
                return self._calls_by_channel[channel], "channel"

            waiting = [c for c in self.calls if c.is_waiting_for_remote()]
            if len(waiting) == 1 and len(self.calls) == 1:
                return waiting[0], "single-call-fallback"

        return None, "uncorrelated"

    def _handle_message(self, event: dict):
        self._record_message(event)
        self._record_auth_state(event)
        self._learn_channel_mapping(event)
        self._trace_event(event)

        if not self._is_ready_event(event):
            return

        call, how = self._resolve_call(event)
        if call is None:
            self._uncorrelated_count += 1
            if self._uncorrelated_count <= 5:
                log_error(
                    "*** AMI ready event could not be correlated to a call "
                    f"(Channel={event.get('Channel')}). Falling back to silence "
                    "detection for this turn. See CORRELATION NOTE in this file."
                )
            return

        call.on_ami_ready_event(event, how)

    def _learn_channel_mapping(self, event: dict):
        """Pick up Channel <-> test-id pairs from any event that carries both."""
        channel = event.get("Channel")
        if not channel:
            return
        with self._lock:
            if channel in self._calls_by_channel:
                return
            test_id = self._extract_test_id(event)
            if test_id:
                call = self._calls_by_test_id.get(test_id)
                if call is not None:
                    self._calls_by_channel[channel] = call
                    log_info(f"*** AMI mapped channel {channel} -> {call.test_call_id}")

    # --- bookkeeping ------------------------------------------------------

    def _record_message(self, event: dict):
        self._message_count += 1
        event_name = event.get("Event")
        if event_name:
            self._event_count += 1
            key = event_name.lower()
            self._event_counts[key] = self._event_counts.get(key, 0) + 1
            return
        if "Response" in event:
            self._response_count += 1

    def _record_auth_state(self, event: dict):
        if "Response" not in event:
            return

        response = event.get("Response", "")
        message = event.get("Message", "")
        action_id = event.get("ActionID", "")
        message_lower = message.lower()

        if action_id and action_id != AMI_LOGIN_ACTION_ID:
            return

        if response == "Success" and self._authenticated is None:
            self._authenticated = True
            log_info(f"*** AMI authentication accepted ({action_id}); waiting for events")
            return

        if response == "Error" and "auth" in message_lower:
            if self._authenticated is not False:
                log_error(
                    f"*** AMI authentication failed ({action_id}); AMI events are unavailable. "
                    "This run will advance from remote-audio silence fallback only."
                )
            self._authenticated = False

    def _maybe_log_diagnostics(self):
        if not AMI_TRACE_EVENTS or AMI_DIAGNOSTIC_INTERVAL_SECS <= 0:
            return

        now = time.time()
        if now - self._last_diag_at < AMI_DIAGNOSTIC_INTERVAL_SECS:
            return

        self._last_diag_at = now
        age = "never" if self._last_rx_at is None else f"{now - self._last_rx_at:.1f}s ago"
        auth = (
            "success" if self._authenticated is True
            else "failed" if self._authenticated is False
            else "pending"
        )
        counts = (
            ", ".join(f"{n}={c}" for n, c in sorted(self._event_counts.items()))
            if self._event_counts else "none"
        )
        log_info(
            "*** AMI diagnostic: "
            f"messages={self._message_count} responses={self._response_count} "
            f"events={self._event_count} uncorrelated={self._uncorrelated_count} "
            f"auth={auth} last_rx={age} event_counts=[{counts}]"
        )

    def _trace_event(self, event: dict):
        if not AMI_TRACE_EVENTS:
            return

        if "Response" in event:
            response = event.get("Response", "")
            message = event.get("Message", "")
            action_id = event.get("ActionID", "")
            log_info(f"*** AMI response: {response} {message} {action_id}".strip())
            return

        event_name = event.get("Event", "")
        event_key = event_name.lower()
        trace_detail_events = {
            "newexten", "agiexecstart", "agiexecend", "varset",
            "mixmonitorstart", "mixmonitorstop", "monitorstart", "monitorstop",
            "newchannel", "newstate", "hangup", "userevent",
        }

        if event_name and event_key not in self._traced_event_names:
            self._traced_event_names.add(event_key)
            keys = ",".join(sorted(event.keys()))
            log_info(f"*** AMI first event: {event_name} keys=[{keys}]")

        if not AMI_TRACE_ALL_EVENTS and event_key not in trace_detail_events:
            return

        detail = (
            event.get("Application") or event.get("Command")
            or event.get("Variable") or event.get("ChannelStateDesc") or ""
        )
        value = event.get("AppData") or event.get("Value") or ""
        channel = event.get("Channel", "")
        log_info(f"*** AMI trace: {event_name} {detail} {value} {channel}")

    def _is_ready_event(self, event: dict) -> bool:
        if self._authenticated is False:
            return False

        event_name = event.get("Event", "").lower()

        if event_name == "userevent":
            return event.get("UserEvent") == AMI_READY_EVENT_NAME

        if event_name == "newexten":
            application = event.get("Application", "").lower()
            app_data = event.get("AppData", "")
            return application == "record" and "_input_raw.wav" in app_data

        if event_name in {"mixmonitorstart", "monitorstart"}:
            file_name = event.get("File", "") or event.get("Filename", "")
            return "_input_raw" in file_name

        if event_name in {"agiexecstart", "agiexecend"}:
            command = event.get("Command", "")
            return command.upper().startswith("STREAM FILE ")

        return False


class MyAccount(pj.Account):
    def __init__(self):
        super().__init__()


class TxAudioPort(pj.AudioMediaPort):
    """
    Permanent outbound source for one call.

    It emits silence when idle and cached WAV frames during an action. The
    connection to the call never changes, so the RTP clock keeps running for
    RFC4733 DTMF and concurrent turns cannot race conference-port teardown.
    """

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._lock = threading.Lock()
        self._frames = None
        self._frame_index = 0
        self._playback_name = None
        self._silence_frame = pj.ByteVector(b"\x00" * TX_FRAME_BYTES)

    def create(self):
        fmt = pj.MediaFormatAudio()
        try:
            fmt.type = pj.PJMEDIA_TYPE_AUDIO
        except Exception:
            pass
        fmt.clockRate = TX_CLOCK_RATE
        fmt.channelCount = TX_CHANNEL_COUNT
        fmt.bitsPerSample = TX_SAMPLE_WIDTH_BYTES * 8
        fmt.frameTimeUsec = TX_FRAME_PTIME_MS * 1000
        self.createPort(f"tx-audio-{self.owner.call_id:02d}", fmt)

    def start_playback(self, filename: str):
        frames = self.owner.audio_assets.get(filename)
        if not frames:
            raise ValueError(f"no preloaded frames for {filename}")

        with self._lock:
            if self._frames is not None:
                raise RuntimeError(
                    f"cannot start {filename}; {self._playback_name} is still playing"
                )
            self._frames = frames
            self._frame_index = 0
            self._playback_name = filename

    def cancel_playback(self):
        with self._lock:
            self._frames = None
            self._frame_index = 0
            self._playback_name = None

    def onFrameRequested(self, frame):
        # Media clock callback: only select an already-built ByteVector.
        finished = False
        with self._lock:
            if self._frames is None:
                output = self._silence_frame
            else:
                output = self._frames[self._frame_index]
                self._frame_index += 1
                if self._frame_index >= len(self._frames):
                    self._frames = None
                    self._frame_index = 0
                    self._playback_name = None
                    finished = True

        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        frame.buf = output
        if finished:
            self.owner.on_playback_eof()


class RemoteTap(pj.AudioMediaPort):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._frame_count = 0

    def create(self):
        fmt = pj.MediaFormatAudio()
        try:
            fmt.type = pj.PJMEDIA_TYPE_AUDIO
        except Exception:
            pass
        fmt.clockRate = 8000
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = 20000
        self.createPort("remote-tap", fmt)

    def onFrameReceived(self, frame):
        # Runs on the pjmedia clock thread, ~50x/sec per call. Every
        # microsecond spent here delays every other call's frame, including
        # RFC2833 event pacing, so most frames are dropped unexamined. A
        # 100 ms sampling grid is ample for a multi-second silence threshold.
        self._frame_count += 1
        if self._frame_count % TAP_FRAME_DECIMATION:
            return
        try:
            buf = frame.buf
            if buf is None:
                return
            try:
                pcm = bytes(buf)
            except Exception:
                try:
                    pcm = bytes(bytearray(buf))
                except Exception:
                    return
            if not pcm:
                return

            energy = frame_rms(pcm)
            owner = self.owner

            # Lock-free fast path: only take the lock when there is voice.
            owner.last_frame_energy = energy
            if energy >= owner.voice_energy_threshold:
                owner.remote_seen_voice = True
                owner.last_voice_ts = time.time()
                # Each voiced sampled frame represents one decimation window.
                owner.voice_tick_count += 1
        except Exception as e:
            self.owner.log(f"remote tap frame error: {e}")


class MyCall(pj.Call):
    def __init__(self, ep, acc, call_id, dst_uri, actions, mixed_recording, audio_assets):
        super().__init__(acc)
        self.ep = ep
        self.call_id = call_id
        self.test_call_id = f"tc-{call_id:02d}-{uuid.uuid4().hex[:8]}"
        self.dst_uri = dst_uri
        self.actions = list(actions)
        self.mixed_recording = mixed_recording
        self.audio_assets = audio_assets

        self.call_audio = None
        self.mixed_recorder = None
        self.tx_audio = None
        self.remote_tap = None

        self.media_ready = False
        self.media_graph_ready = False
        self.disconnected = False
        self.connected = False

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._driver_thread = None
        self._driver_started = False
        self._waiting_for_remote = False
        self._ami_ready_evt = threading.Event()
        self._playback_done_evt = threading.Event()

        self.voice_energy_threshold = VOICE_ENERGY_THRESHOLD
        self.remote_seen_voice = False
        self.last_voice_ts = 0.0
        self.last_frame_energy = 0.0
        self.voice_tick_count = 0

        self.current_wait_requires_prompt_start = False
        self.current_wait_merge_bridge_gap = False
        self.current_wait_min_voice_ms = 0
        self.current_wait_silence_ms = SILENCE_AFTER_VOICE_MS

    # --- lifecycle --------------------------------------------------------

    def release_pjsua2_ownership(self):
        try:
            self.thisown = False
        except Exception:
            pass

    def log(self, message, level="INFO"):
        log_message(f"[call-{self.call_id:02d}] {message}", level)

    def start(self):
        prm = pj.CallOpParam(True)
        prm.opt.audioCount = 1
        prm.opt.videoCount = 0
        prm.opt.textCount = 0

        # Stamp the INVITE so AMI events can be correlated back to this call.
        try:
            hdr = pj.SipHeader()
            hdr.hName = TEST_CALL_ID_HEADER
            hdr.hValue = self.test_call_id
            prm.txOption.headers.append(hdr)
        except Exception as e:
            self.log(f"could not attach {TEST_CALL_ID_HEADER}: {e}", "ERROR")

        self.makeCall(self.dst_uri, prm)

    def safe_hangup(self):
        if self.disconnected:
            return
        try:
            if self.getId() < 0:
                return  # makeCall() never succeeded -- no valid PJSIP slot
        except Exception:
            return
        try:
            self.hangup(pj.CallOpParam())
        except Exception as e:
            self.log(f"hangup warning: {e}")

    def onCallState(self, prm):
        ci = self.getInfo()
        self.log(
            f"call state: {ci.stateText} | "
            f"lastStatusCode={ci.lastStatusCode} | "
            f"lastReason={ci.lastReason}"
        )

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected = True

        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.disconnected = True
            self._stop_evt.set()
            self._ami_ready_evt.set()
            self._playback_done_evt.set()
            tx_audio = self.tx_audio
            if tx_audio is not None:
                tx_audio.cancel_playback()
            with self._lock:
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False

    def onCallMediaState(self, prm):
        ci = self.getInfo()

        for i, m in enumerate(ci.media):
            if not is_active_audio_media(m):
                continue

            try:
                audio_media = self.getAudioMedia(i)
            except pj.Error as e:
                self.log(f"getAudioMedia failed: {e}")
                continue

            with self._lock:
                self.call_audio = audio_media
                self.media_ready = True

            self.log("media is ready")
            self.start_driver()
            break

    def onCallTransferRequest(self, prm):
        self.log(f"*** TRANSFER via REFER to: {prm.dstUri} -- declining and hanging up")
        prm.statusCode = 603
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallRedirected(self, prm):
        target = getattr(prm, "targetUri", "<unknown>")
        self.log(f"*** TRANSFER via 3xx redirect to: {target} -- hanging up")
        prm.opt = getattr(pj, "PJSIP_REDIRECT_STOP", 2)
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallReplaced(self, prm):
        self.log("*** TRANSFER via call replace -- hanging up")
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def _hangup_after_transfer(self):
        time.sleep(0.3)
        self.safe_hangup()

    # --- permanent media graph -------------------------------------------

    def _connect_with_retry(self, source, sink, label) -> bool:
        last_error = None
        for attempt in range(1, MEDIA_SETUP_ATTEMPTS + 1):
            if self._stop_evt.is_set() or self.disconnected:
                return False
            try:
                with MEDIA_GRAPH_LOCK:
                    source.startTransmit(sink)
                self.log(f"media connected: {label}")
                return True
            except pj.Error as e:
                last_error = e
                self.log(
                    f"media connect attempt {attempt}/{MEDIA_SETUP_ATTEMPTS} "
                    f"failed ({label}): {e}",
                    "ERROR",
                )
                if attempt < MEDIA_SETUP_ATTEMPTS:
                    time.sleep(MEDIA_SETUP_RETRY_MS / 1000.0)

        self.log(f"media connection failed permanently ({label}): {last_error}", "ERROR")
        return False

    def _setup_media_graph(self) -> bool:
        with self._lock:
            call_audio = self.call_audio
        if call_audio is None or self.disconnected:
            self.log("cannot set up media graph: call audio is unavailable", "ERROR")
            return False

        # The transmit source is critical. It stays connected and supplies
        # either a WAV frame or silence on every media tick.
        try:
            tx_audio = TxAudioPort(self)
            with MEDIA_GRAPH_LOCK:
                tx_audio.create()
            self.tx_audio = tx_audio
        except Exception as e:
            self.log(f"outbound media port creation failed: {e}", "ERROR")
            return False

        if not self._connect_with_retry(tx_audio, call_audio, "tx-audio -> call"):
            return False

        # RemoteTap is required when silence detection is the turn signal.
        try:
            remote_tap = RemoteTap(self)
            with MEDIA_GRAPH_LOCK:
                remote_tap.create()
            self.remote_tap = remote_tap
            tap_connected = self._connect_with_retry(
                call_audio, remote_tap, "call -> remote-tap"
            )
        except Exception as e:
            self.log(f"remote tap setup failed: {e}", "ERROR")
            tap_connected = False

        if not tap_connected:
            self.log(
                "remote tap is required for per-call turn detection and AMI fallback",
                "ERROR",
            )
            return False

        # Recording is diagnostic and must never prevent audio transmission.
        try:
            recorder = pj.AudioMediaRecorder()
            with MEDIA_GRAPH_LOCK:
                recorder.createRecorder(self.mixed_recording)
            self.mixed_recorder = recorder
            remote_recording = self._connect_with_retry(
                call_audio, recorder, "call -> mixed-recorder"
            )
            local_recording = self._connect_with_retry(
                tx_audio, recorder, "tx-audio -> mixed-recorder"
            )
            if remote_recording or local_recording:
                self.log(f"mixed recording started: {self.mixed_recording}")
        except Exception as e:
            self.log(f"mixed recorder setup failed (call continues): {e}", "ERROR")

        self.media_graph_ready = True
        self.log("permanent media graph is ready")
        return True

    # --- turn detection ---------------------------------------------------

    def is_waiting_for_remote(self):
        with self._lock:
            return self._waiting_for_remote and not self.disconnected

    def on_ami_ready_event(self, event: dict, how: str = "?"):
        if self._stop_evt.is_set() or self.disconnected:
            return
        detail = (
            event.get("Interaction") or event.get("Mode")
            or event.get("Application") or event.get("Command")
            or event.get("Channel") or "unknown"
        )
        self.log(f"AMI ready-for-input event ({how}): {detail}")
        self._ami_ready_evt.set()

    def on_playback_eof(self):
        self._playback_done_evt.set()

    def _remote_turn_ready_by_silence(self, label) -> bool:
        now = time.time()

        with self._lock:
            require_prompt_start = self.current_wait_requires_prompt_start
            merge_bridge_gap = self.current_wait_merge_bridge_gap
            min_voice_ms = self.current_wait_min_voice_ms
            silence_ms = self.current_wait_silence_ms

        seen_voice = self.remote_seen_voice
        last_voice_ts = self.last_voice_ts

        if require_prompt_start and not seen_voice:
            return False

        if require_prompt_start and seen_voice:
            with self._lock:
                self.current_wait_requires_prompt_start = False

        if not seen_voice or last_voice_ts <= 0:
            return False

        silent_for_ms = (now - last_voice_ts) * 1000.0
        if silent_for_ms < silence_ms:
            return False

        if merge_bridge_gap and silent_for_ms < POST_REDIRECT_TOTAL_SILENCE_MS:
            return False

        voiced_ms = self.voice_tick_count * TX_FRAME_PTIME_MS * TAP_FRAME_DECIMATION
        if min_voice_ms and voiced_ms < min_voice_ms:
            # Silence after a short filler ("one moment...") -- the real
            # digit-entry prompt has not played yet, so keep waiting.
            return False

        if merge_bridge_gap:
            self.log(
                f"remote turn finished after post-redirect silence ({label}) "
                f"silent_for_ms={silent_for_ms:.0f}"
            )
        else:
            self.log(f"remote turn finished ({label}) voiced_ms={voiced_ms}")
        return True

    def _wait_for_turn(
        self, timeout_secs, label, require_prompt_start, merge_bridge_gap,
        min_voice_ms=0, silence_ms=None,
    ) -> str:
        effective_silence_ms = (
            silence_ms if silence_ms is not None else SILENCE_AFTER_VOICE_MS
        )
        with self._lock:
            if self.disconnected:
                return "aborted"
            self._waiting_for_remote = True
            self.current_wait_requires_prompt_start = require_prompt_start
            self.current_wait_merge_bridge_gap = merge_bridge_gap
            self.current_wait_min_voice_ms = min_voice_ms
            self.current_wait_silence_ms = effective_silence_ms

        self._ami_ready_evt.clear()
        self.remote_seen_voice = False
        self.last_voice_ts = 0.0
        self.last_frame_energy = 0.0
        self.voice_tick_count = 0

        self.log(
            f"waiting for turn ({label}) "
            f"require_prompt_start={require_prompt_start} merge_bridge_gap={merge_bridge_gap} "
            f"min_voice_ms={min_voice_ms} silence_ms={effective_silence_ms}"
        )
        started_at = time.time()

        try:
            while not self._stop_evt.is_set():
                fired = self._ami_ready_evt.wait(POLL_MS / 1000.0)
                if self._stop_evt.is_set():
                    return "aborted"
                if fired:
                    if USE_AMI_READY_EVENTS:
                        return "ami"
                    self._ami_ready_evt.clear()

                if self._remote_turn_ready_by_silence(label):
                    return "silence"

                if timeout_secs and (time.time() - started_at) >= timeout_secs:
                    self.log(f"turn wait timeout ({label})")
                    return "timeout"
            return "aborted"
        finally:
            with self._lock:
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False
                self.current_wait_min_voice_ms = 0
                self.current_wait_silence_ms = SILENCE_AFTER_VOICE_MS

    # --- actions ----------------------------------------------------------

    def _play_wav(self, filename: str) -> bool:
        path = resolve_input_audio(filename)
        tx_audio = self.tx_audio
        if tx_audio is None or not self.media_graph_ready:
            self.log(f"cannot play {filename}: outbound media is not ready", "ERROR")
            return False

        self._playback_done_evt.clear()
        try:
            self.log(f"starting playback: {path}")
            tx_audio.start_playback(filename)
        except Exception as e:
            self.log(f"playback start failed for {filename}: {e}")
            return False

        while not self._stop_evt.is_set():
            if self._playback_done_evt.wait(0.2):
                break

        if self._stop_evt.is_set() or self.disconnected:
            tx_audio.cancel_playback()
            self.log(f"local WAV aborted: {path}", "ERROR")
            return False

        self.log(f"local WAV finished: {path}")
        return True

    def _acquire_dtmf_start_slot(self) -> bool:
        """
        Space DTMF train starts across calls. Turn detection re-synchronizes
        every call to the same IVR prompt, so without this gate dozens of
        RFC2833 event trains begin on the same few media ticks -- the worst
        possible moment to load the clock thread that paces them.

        The wait is CAPPED: the dialplan collects digits with Read(), which
        gives ~5 s after the prompt to start dialing. Smoothing the herd is
        never worth missing that window.
        """
        global _dtmf_last_start_ts
        if DTMF_HERD_SPACING_MS <= 0:
            return True
        entered = time.time()
        while not self._stop_evt.is_set():
            capped = False
            with _DTMF_GATE_LOCK:
                now = time.time()
                wait_s = _dtmf_last_start_ts + DTMF_HERD_SPACING_MS / 1000.0 - now
                if wait_s <= 0:
                    _dtmf_last_start_ts = now
                    return True
                if (now - entered) * 1000.0 >= DTMF_GATE_MAX_WAIT_MS:
                    _dtmf_last_start_ts = now
                    capped = True
            if capped:
                self.log(
                    "DTMF gate wait cap reached; sending now to stay inside "
                    "the IVR's Read() input window"
                )
                return True
            if self._stop_evt.wait(min(wait_s, 0.2)):
                return False
        return False

    def _send_dtmf(self, digits: str) -> bool:
        """
        Three things matter here:

        1. The RTP keepalive stays running. RFC2833 events are emitted from
           pjmedia_stream's put_frame(); if nothing transmits into the call's
           conference port, the digit queue drains erratically or not at all.
        2. The whole string goes in one dialDtmf() call. pjmedia clocks the
           digit durations and inter-digit gaps off RTP timestamps. Driving
           that from a Python loop with time.sleep() is what produced doubled
           and dropped digits once the GIL got busy at high concurrency.
        3. The media clock must not slip while the train is on the wire. That
           is handled globally (GIL switch interval, tap decimation, console
           log clamp) plus the herd gate below. See DTMF NOTE at end of file.
        """
        if not self.media_graph_ready or self.tx_audio is None:
            self.log("DTMF not sent: permanent RTP transmitter is unavailable", "ERROR")
            return False

        if not self._acquire_dtmf_start_slot():
            return False

        self.log(
            f"sending DTMF ({DTMF_METHOD}): {digits} "
            f"duration_ms={DTMF_DURATION_MS}"
        )

        try:
            if hasattr(pj, "CallSendDtmfParam"):
                prm = pj.CallSendDtmfParam()
                prm.method = (
                    pj.PJSUA_DTMF_METHOD_SIP_INFO
                    if DTMF_METHOD == "sip_info"
                    else pj.PJSUA_DTMF_METHOD_RFC2833
                )
                prm.duration = DTMF_DURATION_MS
                prm.digits = digits
                self.sendDtmf(prm)
            else:
                if DTMF_METHOD == "sip_info":
                    self.log("SIP INFO is unavailable in this PJSUA2 build", "ERROR")
                    return False
                self.dialDtmf(digits)
        except pj.Error as e:
            self.log(f"DTMF send failed: {e}", "ERROR")
            return False
        except Exception as e:
            self.log(f"DTMF send unexpected error: {e}", "ERROR")
            return False

        settle_ms = len(digits) * DTMF_SETTLE_MS_PER_DIGIT + DTMF_SETTLE_EXTRA_MS
        deadline = time.time() + settle_ms / 1000.0
        while not self._stop_evt.is_set() and time.time() < deadline:
            time.sleep(0.05)

        self.log("DTMF transmission window elapsed (queueing is not a delivery ACK)")
        return not self._stop_evt.is_set()

    # --- driver -----------------------------------------------------------

    def start_driver(self):
        with self._lock:
            if self._driver_started or self.disconnected:
                return
            self._driver_started = True

        self._driver_thread = threading.Thread(
            target=self._driver_loop,
            name=f"driver-{self.call_id}",
            daemon=True,
        )
        self._driver_thread.start()

    def _driver_loop(self):
        """
        One thread per call, registered with PJLIB exactly once. The old design
        spawned a fresh thread per action and called libRegisterThread each
        time; those descriptors are never released, so a 20-call x 19-action
        run leaked 380+ of them.
        """
        try:
            self.ep.libRegisterThread(f"driver-{self.call_id}")
        except pj.Error as e:
            self.log(f"libRegisterThread warning: {e}", "ERROR")

        try:
            if not self._setup_media_graph():
                self.log("aborting call because media setup did not complete", "ERROR")
                self.safe_hangup()
                return

            reason = self._wait_for_turn(
                timeout_secs=INITIAL_WAIT_TIMEOUT_SECS,
                label="initial-remote-turn",
                require_prompt_start=True,
                merge_bridge_gap=False,
            )
            if reason == "aborted":
                return
            self.log(f"ready source={reason} (initial-remote-turn)")

            for idx, (action_type, action_value) in enumerate(self.actions):
                if self._stop_evt.is_set():
                    return

                if action_type == "wav":
                    action_ok = self._play_wav(action_value)
                elif action_type == "dtmf":
                    action_ok = self._send_dtmf(action_value)
                else:
                    self.log(f"unknown action type: {action_type}", "ERROR")
                    action_ok = False

                if not action_ok:
                    self.log(
                        f"aborting action sequence at action {idx + 1}: "
                        f"{action_type} {action_value}",
                        "ERROR",
                    )
                    self.safe_hangup()
                    return

                if self._stop_evt.is_set():
                    return

                if idx == len(self.actions) - 1:
                    self.log("action sequence complete")
                    return

                next_is_dtmf = self.actions[idx + 1][0] == "dtmf"
                reason = self._wait_for_turn(
                    timeout_secs=NEXT_TURN_WAIT_TIMEOUT_SECS,
                    label=f"after-action-{idx + 1}",
                    # After DTMF the IVR always speaks a fresh prompt, so
                    # require voice onset before we accept silence as an end.
                    require_prompt_start=(action_type == "dtmf"),
                    # First turn can span a bridge/redirect gap.
                    merge_bridge_gap=(idx == 0),
                    # Before DTMF, silence after a short filler must not open
                    # digit entry -- wait for the real prompt to have played.
                    min_voice_ms=(DTMF_MIN_PROMPT_VOICE_MS if next_is_dtmf else 0),
                    # Before DTMF, react fast: Read() accepts digits during
                    # the prompt, so early is harmless and late misses the
                    # first-digit timeout.
                    silence_ms=(
                        DTMF_SILENCE_AFTER_VOICE_MS if next_is_dtmf else None
                    ),
                )
                if reason == "aborted":
                    return
                self.log(f"ready source={reason} (after-action-{idx + 1})")

        except Exception as e:
            self.log(f"driver loop error: {e}", "ERROR")


def main():
    ep = pj.Endpoint()
    acc = None
    calls = []
    audio_assets = None
    ami_listener = AmiReadyListener() if USE_AMI_READY_EVENTS else None

    try:
        validate_config()
        audio_assets = load_audio_assets(ACTIONS)

        if ami_listener is not None:
            ami_listener.start()

        bind_ip = get_bind_ip()
        log_info(f"*** chosen local bind IP: {bind_ip}")

        ep_cfg = pj.EpConfig()
        configure_endpoint(ep_cfg)

        ep.libCreate()
        ep.libInit(ep_cfg)
        ep.audDevManager().setNullDev()

        transport_id = make_transport(ep, bind_ip)
        ep.libStart()
        log_info("*** PJSUA2 STARTED ***")

        configure_codecs(ep)

        acfg = build_account_config(bind_ip, transport_id)
        acc = MyAccount()
        acc.create(acfg)

        log_info("*** account created without registration")
        log_info(f"*** starting {NUM_CALLS} direct INVITE call(s), DTMF method={DTMF_METHOD}")

        for call_id in range(1, NUM_CALLS + 1):
            call = MyCall(
                ep=ep,
                acc=acc,
                call_id=call_id,
                dst_uri=DEST_URI,
                actions=ACTIONS,
                mixed_recording=build_recording_path("mixed", call_id),
                audio_assets=audio_assets,
            )
            calls.append(call)
            if ami_listener is not None:
                ami_listener.add_call(call)
            call.log(f"starting direct INVITE to {DEST_URI} [{call.test_call_id}]")
            call.start()

            if call_id < NUM_CALLS and CALL_START_GAP_MS > 0:
                time.sleep(CALL_START_GAP_MS / 1000.0)

        started = time.time()
        while time.time() - started < MAX_CALL_SECONDS:
            if all(call.disconnected for call in calls):
                break
            time.sleep(0.1)

        remaining_calls = [call for call in calls if not call.disconnected]
        if remaining_calls:
            log_error(f"*** max call time reached, hanging up {len(remaining_calls)} call(s)")
            for call in remaining_calls:
                call.safe_hangup()

            wait_start = time.time()
            while time.time() - wait_start < 3:
                if all(call.disconnected for call in calls):
                    break
                time.sleep(0.1)

    except pj.Error as e:
        log_error(f"*** PJSUA2 error: {e}")
    except KeyboardInterrupt:
        log_error("*** interrupted, shutting down")
    except Exception as e:
        log_error(f"*** general error: {e}")
    finally:
        if ami_listener is not None:
            ami_listener.stop()

        for call in calls:
            call._stop_evt.set()
            call._ami_ready_evt.set()
            call._playback_done_evt.set()

        for call in calls:
            if not call.disconnected:
                call.safe_hangup()

        wait_start = time.time()
        while time.time() - wait_start < 5:
            if all(c.disconnected for c in calls):
                break
            time.sleep(0.1)

        # Join driver threads so their bound-method references to MyCall are
        # released before calls.clear(). A live thread keeps MyCall alive past
        # libDestroy(), which trips the C assertion
        # "call_id >= 0 && call_id < max_calls" in pjsua_call_set_user_data.
        for call in calls:
            t = call._driver_thread
            if t is not None and t.is_alive():
                t.join(timeout=3.0)

        for call in calls:
            call.release_pjsua2_ownership()

        calls.clear()
        acc = None
        gc.collect()

        try:
            ep.libDestroy()
        except Exception as e:
            log_error(f"*** libDestroy warning: {e}")


# =============================================================================
# CORRELATION NOTE
# =============================================================================
# Every INVITE now carries:
#
#     X-Test-Call-Id: tc-07-a3f19b2c
#
# For AMI events to route to the right call, the dialplan must echo it back.
# Minimal example:
#
#     exten => _X.,1,Set(TESTID=${SIP_HEADER(X-Test-Call-Id)})
#      same  =>    n,UserEvent(TestReadyForInput,TestCallId: ${TESTID},Channel: ${CHANNEL})
#
# (chan_pjsip: use ${PJSIP_HEADER(read,X-Test-Call-Id)} instead of SIP_HEADER.)
#
# Once one event carries both TestCallId and Channel, the listener learns the
# Channel -> call mapping and subsequent events route on Channel alone.
#
# If you cannot touch the dialplan, set USE_AMI_READY_EVENTS=0 and run on
# silence detection only -- that is per-call by construction and correct at any
# concurrency, just less precise about where a prompt ends.
# =============================================================================
#
# =============================================================================
# DTMF NOTE -- why digits vanished at high concurrency, and the fix hierarchy
# =============================================================================
# dialDtmf()/sendDtmf() only QUEUE digits. The RFC2833 event packets are
# emitted by pjmedia_stream's put_frame(), driven by the conference-bridge
# clock: one C thread ticking every 20 ms across every port of every call.
# Each Python media port (TxAudioPort, RemoteTap) forces that thread through
# a GIL acquisition per frame; at 50 calls that is ~5000 GIL round-trips/sec
# on the one thread whose punctuality decides whether Asterisk sees clean
# telephone-event trains. Evidence it was slipping: Asterisk's teardown stats
# reported 8-15 ms TX jitter on a 20 ms-ptime stream and 400-600 ms RTCP RTT
# on a LAN. When the tick slips mid-train, Asterisk's inter-digit timeout
# merges/drops digits; the IVR waits for a complete number that never
# arrives, then hangs up. Settle times, '#' suffixes, and call-start gaps
# cannot fix this because they never touch the media clock.
#
# Mitigations in this file (keep RFC2833 usable to ~50 calls/process):
#   1. sys.setswitchinterval(0.001)     -- caps GIL handoff stalls at ~1 ms
#   2. TAP_FRAME_DECIMATION=5           -- 5x less Python work on the clock
#   3. console log clamp at NUM_CALLS>10 -- no synchronous SIP tracing in
#      the media path (PJSIP_FORCE_CONSOLE_LOG=1 to override)
#   4. DTMF_HERD_SPACING_MS=100         -- event trains start staggered,
#      capped at DTMF_GATE_MAX_WAIT_MS so no call misses the dialplan's
#      Read() input window (~5 s after the prompt ends)
#
# If digits still drop, escalate in this order:
#   a. DTMF_METHOD=sip_info. SIP INFO rides the signalling path and is
#      immune to media jitter BY CONSTRUCTION -- the only truly load-proof
#      option. Asterisk endpoint needs dtmf_mode=info (chan_pjsip: info or
#      auto_info; FreePBX: extension -> Advanced -> DTMF Signaling).
#   b. Shard the run: several processes of <=20 calls each (separate SIP
#      ports / RTP ranges). The GIL is per-process; this is the real
#      scaling axis for a Python pjsua2 load generator.
#   c. On WSL2, enable mirrored networking (.wslconfig: [wsl2]
#      networkingMode=mirrored, Windows 11 22H2+). The default NAT (your
#      log shows received=192.168.220.159 vs bound 172.25.35.136) adds
#      per-flow translation jitter to all 50 RTP streams.
# =============================================================================


if __name__ == "__main__":
    main()
