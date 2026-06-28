import gc
import os
import time
import math
import struct
import socket
import threading
from pathlib import Path
from dotenv import load_dotenv
import pjsua2 as pj

load_dotenv()

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

NUM_CALLS = 1
CALL_START_GAP_MS = 200
MAX_CALL_SECONDS = 1800

# Added:
# Give PJSUA a higher call capacity than NUM_CALLS so it does not stop at 4.
# Stay comfortably below the usual compile-time default limit of 32.
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


def env_csv(name, default=""):
    value = os.getenv(name, default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


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


def safe_set(obj, attr, value):
    if not hasattr(obj, attr):
        return False
    try:
        setattr(obj, attr, value)
        return True
    except Exception as e:
        print(f"*** could not set {obj.__class__.__name__}.{attr}: {e}")
        return False


def describe_pj_error(err):
    fields = [f"repr={err!r}"]
    for attr in ("status", "reason", "title", "srcFile", "srcLine"):
        try:
            value = getattr(err, attr)
        except Exception:
            continue
        if value not in (None, ""):
            fields.append(f"{attr}={value}")
    return " ".join(fields)


def describe_media(label, media):
    if media is None:
        return f"{label}: <None>"

    fields = [f"{label}: class={media.__class__.__name__}"]

    try:
        fields.append(f"port_id={media.getPortId()}")
    except Exception as e:
        fields.append(f"port_id=<error {e!r}>")

    try:
        info = media.getPortInfo()
        for attr in ("portId", "name", "clockRate", "channelCount", "samplesPerFrame"):
            try:
                value = getattr(info, attr)
            except Exception:
                continue
            if value not in (None, ""):
                fields.append(f"{attr}={value}")
    except Exception as e:
        fields.append(f"port_info=<error {e!r}>")

    return " ".join(fields)


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


def print_transport_info(ep: pj.Endpoint, transport_id: int):
    try:
        info = ep.transportGetInfo(transport_id)
        print(f"*** transport type: {info.typeName}")
        print(f"*** transport localAddress: {format_socket_addr(info.localAddress)}")
        print(f"*** transport localName: {format_socket_addr(info.localName)}")
        print(f"*** transport info: {info.info}")
    except Exception as e:
        print(f"*** could not fetch transport info: {e}")


def format_socket_addr(value):
    host = getattr(value, "host", None)
    port = getattr(value, "port", None)
    if host is not None and port is not None:
        return f"{host}:{port}"
    return str(value)


def configure_codecs(ep: pj.Endpoint):
    try:
        codec_infos = ep.codecEnum2()
    except Exception as e:
        print(f"*** codec enumeration failed: {e}")
        return

    keep_prefixes = ("PCMU/8000",)

    print("*** codec list before priority update:")
    for c in codec_infos:
        print(f"    {c.codecId}")

    for c in codec_infos:
        codec_id = c.codecId
        prio = 0
        if any(codec_id.startswith(prefix) for prefix in keep_prefixes):
            prio = 255

        try:
            ep.codecSetPriority(codec_id, prio)
        except Exception as e:
            print(f"*** failed to set codec priority for {codec_id}: {e}")

    print("*** codec priority update done")


def make_transport(ep: pj.Endpoint, bind_ip: str) -> int:
    tp_cfg = pj.TransportConfig()
    tp_cfg.port = LOCAL_SIP_PORT
    safe_set(tp_cfg, "boundAddress", bind_ip)

    if FORCE_PUBLIC_IP:
        safe_set(tp_cfg, "publicAddress", FORCE_PUBLIC_IP)
        print(f"*** forcing public SIP address: {FORCE_PUBLIC_IP}")
    else:
        print("*** no public SIP address override")

    if USE_TCP:
        tp_type = pj.PJSIP_TRANSPORT_TCP
        print("*** using TCP transport")
    else:
        tp_type = pj.PJSIP_TRANSPORT_UDP
        print("*** using UDP transport")

    return ep.transportCreate(tp_type, tp_cfg)


def configure_endpoint(ep_cfg: pj.EpConfig):
    ep_cfg.logConfig.level = 2
    ep_cfg.logConfig.consoleLevel = 2

    safe_set(ep_cfg.uaConfig, "userAgent", "")
    safe_set(ep_cfg.uaConfig, "natTypeInSdp", 0)
    safe_set(ep_cfg.uaConfig, "enableUpnp", False)

    # Added:
    # Default PJSUA maxCalls is 4 unless overridden.
    requested_max_calls = max(NUM_CALLS + MAX_CALLS_HEADROOM, MIN_RUNTIME_MAX_CALLS)
    if safe_set(ep_cfg.uaConfig, "maxCalls", requested_max_calls):
        print(f"*** uaConfig.maxCalls set to {requested_max_calls}")
    else:
        print("*** warning: could not set uaConfig.maxCalls")

    ep_cfg.medConfig.clockRate = 8000
    ep_cfg.medConfig.channelCount = 1
    ep_cfg.medConfig.sndClockRate = 8000
    ep_cfg.medConfig.quality = 4
    ep_cfg.medConfig.noVad = True
    ep_cfg.medConfig.sndAutoCloseTime = -1


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


class AmiReadyEvents:
    def __init__(self, host, port, username, secret, ready_event_name, caller_filter="", trace=False):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.ready_event_name = ready_event_name
        self.caller_filter = caller_filter.strip()
        self.trace = trace

        self._sock = None
        self._thread = None
        self._stop_evt = threading.Event()
        self._cond = threading.Condition()
        self._login_cond = threading.Condition()
        self._sequence = 0
        self._last_event = None
        self._ready_events = []
        self._transfer_sequence = 0
        self._last_transfer_event = None
        self._transfer_events = []
        self._channel_sequence = 0
        self._channel_events = []
        self._claimed_channel_keys = set()
        self._login_response = None
        self._running = False

    def start(self):
        if not self.username or not self.secret:
            raise ValueError("AMI_USER and AMI_SECRET are required when USE_AMI_READY_EVENTS=1")

        self._sock = socket.create_connection((self.host, self.port), timeout=5)
        self._sock.settimeout(1.0)

        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        self._send_action(
            {
                "Action": "Login",
                "Username": self.username,
                "Secret": self.secret,
                "Events": "on",
            }
        )

        response = self._wait_for_login_response(timeout_secs=5)
        if response is None:
            raise RuntimeError("AMI login timed out before any Response message")

        status = response.get("Response", "")
        message = response.get("Message", "")
        if status.lower() != "success":
            raise RuntimeError(f"AMI login failed: Response={status} Message={message}")

        with self._cond:
            self._running = True
            self._cond.notify_all()

        print(f"*** AMI login accepted: {message or status}")

    def stop(self):
        self._stop_evt.set()
        try:
            if self._sock is not None:
                self._send_action({"Action": "Logoff"})
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def is_running(self):
        with self._cond:
            return self._running

    def current_sequence(self):
        with self._cond:
            return self._sequence

    def current_transfer_sequence(self):
        with self._cond:
            return self._transfer_sequence

    def current_channel_sequence(self):
        with self._cond:
            return self._channel_sequence

    def wait_for_event_after(self, sequence, timeout_secs, stop_evt, linkedid=""):
        deadline = time.time() + timeout_secs if timeout_secs else None
        with self._cond:
            while not stop_evt.is_set():
                for event_sequence, event in self._ready_events:
                    if event_sequence <= sequence:
                        continue
                    if linkedid and event.get("Linkedid") != linkedid:
                        continue
                    return True

                if not self._running:
                    return False

                wait_secs = 0.25
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return False
                    wait_secs = min(wait_secs, remaining)

                self._cond.wait(wait_secs)
        return False

    def wait_for_transfer_after(self, sequence, timeout_secs, stop_evt, linkedid=""):
        deadline = time.time() + timeout_secs if timeout_secs else None
        with self._cond:
            while not stop_evt.is_set():
                for event_sequence, event in self._transfer_events:
                    if event_sequence <= sequence:
                        continue
                    if linkedid and event.get("Linkedid") != linkedid:
                        continue
                    return dict(event)

                if not self._running:
                    return None

                wait_secs = 0.25
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None
                    wait_secs = min(wait_secs, remaining)

                self._cond.wait(wait_secs)
        return None

    def wait_for_channel_after(self, sequence, timeout_secs, stop_evt, expected_caller=""):
        deadline = time.time() + timeout_secs if timeout_secs else None
        with self._cond:
            while not stop_evt.is_set():
                for event_sequence, event in self._channel_events:
                    if event_sequence <= sequence:
                        continue
                    if not self._matches_expected_caller(event, expected_caller):
                        continue

                    channel_key = self._channel_key(event)
                    if not channel_key or channel_key in self._claimed_channel_keys:
                        continue

                    self._claimed_channel_keys.add(channel_key)
                    return dict(event)

                if not self._running:
                    return None

                wait_secs = 0.25
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None
                    wait_secs = min(wait_secs, remaining)

                self._cond.wait(wait_secs)
        return None

    def _wait_for_login_response(self, timeout_secs):
        deadline = time.time() + timeout_secs
        with self._login_cond:
            while self._login_response is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._login_cond.wait(remaining)
            return dict(self._login_response)

    def last_event_summary(self):
        with self._cond:
            if not self._last_event:
                return "<none>"
            return self._summarize_event(self._last_event)

    def last_transfer_event_summary(self):
        with self._cond:
            if not self._last_transfer_event:
                return "<none>"
            return self._summarize_event(self._last_transfer_event)

    def summarize_event(self, msg):
        return self._summarize_event(msg)

    def _send_action(self, fields):
        data = "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n"
        self._sock.sendall(data.encode("utf-8"))

    def _reader_loop(self):
        buf = b""
        try:
            while not self._stop_evt.is_set():
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue

                if not chunk:
                    break

                buf += chunk
                while b"\r\n\r\n" in buf:
                    raw_msg, buf = buf.split(b"\r\n\r\n", 1)
                    msg = self._parse_message(raw_msg)
                    if not msg:
                        continue
                    self._handle_message(msg)
        except Exception as e:
            print(f"*** AMI listener stopped: {e}")
        finally:
            with self._cond:
                self._running = False
                self._cond.notify_all()

    def _parse_message(self, raw_msg):
        text = raw_msg.decode("utf-8", errors="replace")
        msg = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            msg[key.strip()] = value.strip()
        return msg

    def _handle_message(self, msg):
        response = msg.get("Response")
        if response:
            if self.trace:
                print(f"*** AMI response: {self._summarize_response(msg)}")
            with self._login_cond:
                if self._login_response is None:
                    self._login_response = dict(msg)
                    self._login_cond.notify_all()
            return

        event = msg.get("Event")
        if not event:
            return

        if self.trace:
            print(f"*** AMI event: {self._summarize_event(msg)}")

        ready_matched = self._matches_ready_event(msg)
        transfer_matched = self._matches_transfer_event(msg)
        channel_matched = self._matches_channel_event(msg)

        if not ready_matched and not transfer_matched and not channel_matched:
            return

        summary = self._summarize_event(msg)

        with self._cond:
            if channel_matched:
                self._channel_sequence += 1
                self._channel_events.append((self._channel_sequence, dict(msg)))
            if ready_matched:
                self._sequence += 1
                self._last_event = dict(msg)
                self._ready_events.append((self._sequence, dict(msg)))
            if transfer_matched:
                self._transfer_sequence += 1
                self._last_transfer_event = dict(msg)
                self._transfer_events.append((self._transfer_sequence, dict(msg)))
            self._cond.notify_all()

        if ready_matched:
            print(f"*** AMI ready event matched: {summary}")
        if transfer_matched:
            print(f"*** AMI transfer event matched: {summary}")

    def _matches_channel_event(self, msg):
        event_name = msg.get("Event", "")
        return event_name.lower() == "newchannel"

    def _channel_key(self, msg):
        return msg.get("Uniqueid") or msg.get("Channel") or msg.get("Linkedid")

    def _matches_expected_caller(self, msg, expected_caller):
        if not expected_caller:
            return True

        fields = (
            "Caller",
            "CallerIDNum",
            "CallerIDName",
            "Channel",
            "Exten",
        )
        return any(expected_caller in msg.get(field, "") for field in fields)

    def _matches_ready_event(self, msg):
        event_name = msg.get("Event", "")
        event_name_lower = event_name.lower()

        if event_name_lower == "userevent" and msg.get("UserEvent") == self.ready_event_name:
            return self._matches_caller_filter(msg)

        if event_name == self.ready_event_name:
            return self._matches_caller_filter(msg)

        if AMI_USE_AGI_STREAM_EVENTS and event_name_lower == "agiexecend":
            command = msg.get("Command", "")
            if command.upper().startswith("STREAM FILE "):
                return self._matches_caller_filter(msg)

        return False

    def _matches_transfer_event(self, msg):
        if not AMI_DETECT_TRANSFER:
            return False

        event_name = msg.get("Event", "")
        event_name_lower = event_name.lower()

        if event_name_lower == "newexten":
            context = msg.get("Context", "")
            application = msg.get("Application", "")
            app_data = msg.get("AppData", "")

            if any(context.startswith(prefix) for prefix in AMI_TRANSFER_CONTEXT_PREFIX_LIST):
                return self._matches_caller_filter(msg)

            if application.lower() == "dial":
                if any(target in app_data for target in AMI_TRANSFER_DIAL_TARGET_LIST):
                    return self._matches_caller_filter(msg)

        if event_name_lower == "dialbegin":
            dial_string = " ".join(
                msg.get(field, "")
                for field in ("DialString", "DestChannel", "Destination", "Channel")
            )
            if any(target in dial_string for target in AMI_TRANSFER_DIAL_TARGET_LIST):
                return self._matches_caller_filter(msg)

        return False

    def _matches_caller_filter(self, msg):
        if not self.caller_filter:
            return True

        fields = (
            "Caller",
            "CallerIDNum",
            "CallerIDName",
            "ConnectedLineNum",
            "ConnectedLineName",
            "Channel",
            "DestChannel",
            "Exten",
            "Uniqueid",
            "Linkedid",
        )
        return any(self.caller_filter in msg.get(field, "") for field in fields)

    def _summarize_event(self, msg):
        fields = []
        for key in (
            "Event",
            "UserEvent",
            "Command",
            "Application",
            "AppData",
            "Context",
            "DialString",
            "DestChannel",
            "CallerIDNum",
            "CallerIDName",
            "Channel",
            "Exten",
            "Uniqueid",
            "Linkedid",
        ):
            value = msg.get(key)
            if value:
                fields.append(f"{key}={value}")
        return " ".join(fields) if fields else repr(msg)

    def _summarize_response(self, msg):
        fields = []
        for key in ("Response", "Message", "ActionID"):
            value = msg.get(key)
            if value:
                fields.append(f"{key}={value}")
        return " ".join(fields) if fields else repr(msg)


class MyAccount(pj.Account):
    def __init__(self):
        super().__init__()


class FilePlayer(pj.AudioMediaPlayer):
    def __init__(self, owner, wav_path, action_idx):
        super().__init__()
        self.owner = owner
        self.wav_path = wav_path
        self.action_idx = action_idx

    def start_into(self, call_audio):
        self.owner.log(f"starting playback: {self.wav_path}")
        self.createPlayer(self.wav_path, pj.PJMEDIA_FILE_NO_LOOP)
        self.owner.log(describe_media("wav_player_before_startTransmit", self))
        self.owner.log(describe_media("call_audio_before_startTransmit", call_audio))
        self.startTransmit(call_audio)

    def onEof2(self):
        self.owner.log(f"local WAV finished: {self.wav_path}")
        self.owner.on_action_complete(expected_idx=self.action_idx)


class RemoteTap(pj.AudioMediaPort):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

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

            sample_count = len(pcm) // 2
            if sample_count <= 0:
                return

            samples = struct.unpack("<" + ("h" * sample_count), pcm[: sample_count * 2])
            energy = math.sqrt(sum(s * s for s in samples) / sample_count)

            now = time.time()
            with self.owner._lock:
                self.owner.last_frame_energy = energy
                if energy >= self.owner.voice_energy_threshold:
                    self.owner.remote_seen_voice = True
                    self.owner.last_voice_ts = now

        except Exception as e:
            self.owner.log(f"remote tap frame error: {e}")


class MyCall(pj.Call):
    def __init__(self, ep, acc, call_id, dst_uri, actions, silence_wav, ami_ready_events=None):
        super().__init__(acc)
        self.ep = ep
        self.call_id = call_id
        self.dst_uri = dst_uri
        self.actions = list(actions)
        self.silence_wav = silence_wav
        self.ami_ready_events = ami_ready_events

        self.call_audio = None
        self.player = None
        self.keepalive_player = None
        self.remote_tap = None

        self.action_idx = 0
        self.last_action_type = None
        self.media_ready = False
        self.disconnected = False
        self.connected = False

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._wait_thread = None
        self._transfer_thread = None
        self._waiting_for_remote = False
        self._transfer_detected = False

        self.voice_energy_threshold = VOICE_ENERGY_THRESHOLD
        self.remote_seen_voice = False
        self.last_voice_ts = 0.0
        self.last_frame_energy = 0.0

        self.current_wait_requires_prompt_start = False
        self.current_wait_merge_bridge_gap = False
        self.current_wait_ami_sequence = 0
        self.current_transfer_ami_sequence = 0
        self.current_channel_ami_sequence = 0
        self.ami_channel = ""
        self.ami_uniqueid = ""
        self.ami_linkedid = ""

    def release_pjsua2_ownership(self):
        try:
            self.thisown = False
        except Exception:
            pass

    def log(self, message):
        print(f"[call-{self.call_id:02d}] {message}")

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
            self.stop_rtp_keepalive()
            with self._lock:
                self.call_audio = None
                self.player = None
                self.remote_tap = None
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False
            self.release_pjsua2_ownership()

    def start_transfer_monitor(self):
        if self.ami_ready_events is None or not self.ami_ready_events.is_running():
            return

        with self._lock:
            if self.disconnected or self._transfer_thread is not None:
                return
            self.current_channel_ami_sequence = self.ami_ready_events.current_channel_sequence()
            self.current_transfer_ami_sequence = self.ami_ready_events.current_transfer_sequence()

        t = threading.Thread(target=self.wait_for_ami_transfer, daemon=True)
        self._transfer_thread = t
        t.start()

    def wait_for_ami_transfer(self):
        try:
            self.ep.libRegisterThread(f"transfer-{self.call_id}")
            self.log("registered transfer monitor with PJLIB")
        except pj.Error as e:
            self.log(f"libRegisterThread warning (transfer): {e}")

        self.bind_ami_channel()

        with self._lock:
            sequence = self.current_transfer_ami_sequence
            linkedid = self.ami_linkedid

        if linkedid:
            self.log(f"waiting for AMI transfer event after sequence={sequence} linkedid={linkedid}")
        else:
            self.log(
                f"AMI channel was not bound; transfer monitor will not use global "
                f"events for multi-call safety"
            )
            return

        event = self.ami_ready_events.wait_for_transfer_after(
            sequence,
            None,
            self._stop_evt,
            linkedid=linkedid,
        )

        if event and not self.disconnected:
            self.log(f"AMI transfer detected: {self.ami_ready_events.summarize_event(event)}")
            self.on_transfer_detected()

    def bind_ami_channel(self):
        with self._lock:
            if self.ami_linkedid:
                return
            sequence = self.current_channel_ami_sequence

        self.log(f"waiting for AMI Newchannel after sequence={sequence}")
        event = self.ami_ready_events.wait_for_channel_after(
            sequence,
            timeout_secs=10,
            stop_evt=self._stop_evt,
            expected_caller=CALLER_USER,
        )

        if not event:
            self.log("AMI channel bind timed out; transfer detection disabled for this call")
            return

        with self._lock:
            self.ami_channel = event.get("Channel", "")
            self.ami_uniqueid = event.get("Uniqueid", "")
            self.ami_linkedid = event.get("Linkedid", "") or self.ami_uniqueid

        self.log(
            "AMI channel bound: "
            f"channel={self.ami_channel} uniqueid={self.ami_uniqueid} linkedid={self.ami_linkedid}"
        )

    def on_transfer_detected(self):
        with self._lock:
            if self.disconnected or self._transfer_detected:
                return
            self._transfer_detected = True
            self._waiting_for_remote = False
            self.current_wait_requires_prompt_start = False
            self.current_wait_merge_bridge_gap = False

        self.stop_current_audio()
        self.log("stopped local actions after PBX transfer path was detected")

        if HANGUP_ON_AMI_TRANSFER:
            self.log("hanging up before live-agent bridge")
            self.safe_hangup()

    def stop_current_audio(self):
        with self._lock:
            player = self.player
            keepalive_player = self.keepalive_player
            call_audio = self.call_audio
            self.player = None
            self.keepalive_player = None

        for label, media in (("local playback", player), ("RTP keepalive", keepalive_player)):
            if media and call_audio:
                try:
                    media.stopTransmit(call_audio)
                    self.log(f"{label} stopped")
                except pj.Error as e:
                    self.log(f"{label} stop warning: {e}")

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

            try:
                if self.remote_tap is None:
                    self.remote_tap = RemoteTap(self)
                    self.remote_tap.create()
                    self.call_audio.startTransmit(self.remote_tap)
                    self.log("remote tap started")
            except pj.Error as e:
                self.log(f"remote tap setup failed: {e}")

            self.start_rtp_keepalive()
            self.start_wait_thread(
                timeout_secs=INITIAL_WAIT_TIMEOUT_SECS,
                label="initial-remote-turn",
                require_prompt_start=True,
                merge_bridge_gap=False,
            )
            break

    def start_rtp_keepalive(self):
        with self._lock:
            if self.disconnected or not self.call_audio or self.keepalive_player is not None:
                return

            call_audio = self.call_audio
            self.keepalive_player = pj.AudioMediaPlayer()
            player = self.keepalive_player

        try:
            player.createPlayer(self.silence_wav)
            player.startTransmit(call_audio)
            self.log(f"RTP keepalive silence started: {self.silence_wav}")
        except pj.Error as e:
            self.log(f"failed to start RTP keepalive silence: {e}")
            with self._lock:
                self.keepalive_player = None

    def stop_rtp_keepalive(self):
        with self._lock:
            player = self.keepalive_player
            call_audio = self.call_audio
            self.keepalive_player = None
            disconnected = self.disconnected

        if disconnected:
            return

        if player and call_audio:
            try:
                player.stopTransmit(call_audio)
                self.log("RTP keepalive silence stopped")
            except pj.Error as e:
                self.log(f"failed to stop RTP keepalive silence: {e}")

    def start_wait_thread(self, timeout_secs, label, require_prompt_start=False, merge_bridge_gap=False):
        with self._lock:
            if self.disconnected or not self.call_audio:
                return
            if self._waiting_for_remote:
                return
            self._waiting_for_remote = True
            self.remote_seen_voice = False
            self.last_voice_ts = 0.0
            self.last_frame_energy = 0.0
            self.current_wait_requires_prompt_start = require_prompt_start
            self.current_wait_merge_bridge_gap = merge_bridge_gap
            if self.ami_ready_events is not None:
                self.current_wait_ami_sequence = self.ami_ready_events.current_sequence()

        t = threading.Thread(
            target=self.wait_for_remote_turn_end,
            args=(timeout_secs, label),
            daemon=True,
        )
        self._wait_thread = t
        t.start()

    def wait_for_remote_turn_end(self, timeout_secs, label):
        try:
            self.ep.libRegisterThread(f"wait-{self.call_id}-{label}")
            self.log(f"registered wait thread with PJLIB: {label}")
        except pj.Error as e:
            self.log(f"libRegisterThread warning ({label}): {e}")

        if self.ami_ready_events is not None and self.ami_ready_events.is_running():
            self.wait_for_ami_ready_event(timeout_secs, label)
            return

        if self.ami_ready_events is not None:
            self.log(f"AMI listener is not running; falling back to RTP silence detection ({label})")

        last_log_at = 0.0
        started_at = time.time()

        try:
            while not self._stop_evt.is_set():
                now = time.time()

                if timeout_secs and (now - started_at) >= timeout_secs:
                    self.log(f"wait timeout reached ({label})")
                    with self._lock:
                        self._waiting_for_remote = False
                        self.current_wait_requires_prompt_start = False
                        self.current_wait_merge_bridge_gap = False
                    self.start_next_action()
                    return

                with self._lock:
                    seen_voice = self.remote_seen_voice
                    last_voice_ts = self.last_voice_ts
                    energy = self.last_frame_energy
                    require_prompt_start = self.current_wait_requires_prompt_start
                    merge_bridge_gap = self.current_wait_merge_bridge_gap

                if now - last_log_at >= 1.0:
                    self.log(
                        f"frame energy ({label}): {energy:.1f} | "
                        f"seen_voice={seen_voice} | "
                        f"require_prompt_start={require_prompt_start} | "
                        f"merge_bridge_gap={merge_bridge_gap}"
                    )
                    last_log_at = now

                if require_prompt_start and not seen_voice:
                    time.sleep(POLL_MS / 1000.0)
                    continue

                if require_prompt_start and seen_voice:
                    with self._lock:
                        self.current_wait_requires_prompt_start = False

                if seen_voice and last_voice_ts > 0:
                    silent_for_ms = (now - last_voice_ts) * 1000.0

                    if silent_for_ms >= SILENCE_AFTER_VOICE_MS:
                        if merge_bridge_gap:
                            if silent_for_ms >= POST_REDIRECT_TOTAL_SILENCE_MS:
                                self.log(
                                    f"remote turn seems finished after total post-redirect silence "
                                    f"({label}) silent_for_ms={silent_for_ms:.0f}"
                                )
                                with self._lock:
                                    self._waiting_for_remote = False
                                    self.current_wait_requires_prompt_start = False
                                    self.current_wait_merge_bridge_gap = False
                                self.start_next_action()
                                return
                            else:
                                self.log(
                                    f"possible remote turn end ({label}); "
                                    f"still waiting for resumed audio, "
                                    f"silent_for_ms={silent_for_ms:.0f}/"
                                    f"{POST_REDIRECT_TOTAL_SILENCE_MS}"
                                )
                        else:
                            self.log(f"remote turn seems finished ({label})")
                            with self._lock:
                                self._waiting_for_remote = False
                                self.current_wait_requires_prompt_start = False
                                self.current_wait_merge_bridge_gap = False
                            self.start_next_action()
                            return

                time.sleep(POLL_MS / 1000.0)
        finally:
            with self._lock:
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False

    def finish_wait_and_start_next(self, source, label):
        with self._lock:
            if self.disconnected or self._stop_evt.is_set() or self._transfer_detected:
                return

            self._waiting_for_remote = False
            self.current_wait_requires_prompt_start = False
            self.current_wait_merge_bridge_gap = False

        self.log(f"ready source={source} ({label}); starting next action")
        self.start_next_action()

    def remote_turn_ready_by_silence(self, label):
        now = time.time()

        with self._lock:
            seen_voice = self.remote_seen_voice
            last_voice_ts = self.last_voice_ts
            require_prompt_start = self.current_wait_requires_prompt_start
            merge_bridge_gap = self.current_wait_merge_bridge_gap

        if require_prompt_start and not seen_voice:
            return False

        if require_prompt_start and seen_voice:
            with self._lock:
                self.current_wait_requires_prompt_start = False

        if not seen_voice or last_voice_ts <= 0:
            return False

        silent_for_ms = (now - last_voice_ts) * 1000.0
        if silent_for_ms < SILENCE_AFTER_VOICE_MS:
            return False

        if merge_bridge_gap and silent_for_ms < POST_REDIRECT_TOTAL_SILENCE_MS:
            self.log(
                f"possible remote turn end ({label}); still waiting for resumed audio, "
                f"silent_for_ms={silent_for_ms:.0f}/{POST_REDIRECT_TOTAL_SILENCE_MS}"
            )
            return False

        if merge_bridge_gap:
            self.log(
                f"remote turn seems finished after total post-redirect silence "
                f"({label}) silent_for_ms={silent_for_ms:.0f}"
            )
        else:
            self.log(f"remote turn seems finished ({label})")

        return True

    def wait_for_ami_ready_event(self, timeout_secs, label):
        bind_wait_until = time.time() + 2.0
        while not self._stop_evt.is_set():
            with self._lock:
                linkedid = self.ami_linkedid
                transfer_thread = self._transfer_thread
            if linkedid or transfer_thread is None or time.time() >= bind_wait_until:
                break
            time.sleep(0.05)

        with self._lock:
            sequence = self.current_wait_ami_sequence
            linkedid = self.ami_linkedid

        if linkedid:
            self.log(
                f"waiting for AMI ready event or RTP silence ({label}) "
                f"after sequence={sequence} linkedid={linkedid}"
            )
        else:
            self.log(
                f"waiting for AMI ready event or RTP silence ({label}) "
                f"after sequence={sequence}"
            )
        started_at = time.time()
        last_log_at = 0.0

        try:
            while not self._stop_evt.is_set():
                if timeout_secs and (time.time() - started_at) >= timeout_secs:
                    self.log(f"ready wait timeout reached ({label})")
                    self.finish_wait_and_start_next(source="timeout", label=label)
                    return

                matched = self.ami_ready_events.wait_for_event_after(
                    sequence,
                    POLL_MS / 1000.0,
                    self._stop_evt,
                    linkedid=linkedid,
                )
                if matched:
                    self.log(
                        f"AMI ready event received ({label}): "
                        f"{self.ami_ready_events.last_event_summary()}"
                    )
                    self.finish_wait_and_start_next(source="ami", label=label)
                    return

                if self.remote_turn_ready_by_silence(label):
                    self.finish_wait_and_start_next(source="silence", label=label)
                    return

                if not self.ami_ready_events.is_running():
                    time.sleep(POLL_MS / 1000.0)

                now = time.time()
                if now - last_log_at >= 1.0:
                    with self._lock:
                        energy = self.last_frame_energy
                        seen_voice = self.remote_seen_voice
                        require_prompt_start = self.current_wait_requires_prompt_start
                    self.log(
                        f"ready wait ({label}): energy={energy:.1f} "
                        f"seen_voice={seen_voice} "
                        f"require_prompt_start={require_prompt_start}"
                    )
                    last_log_at = now
        finally:
            with self._lock:
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False

    def _send_dtmf_digits(self, digits, expected_idx):
        INTER_DIGIT_MS = 500  # ms between digits — increase if still doubling

        try:
            self.ep.libRegisterThread(f"dtmf-{self.call_id}")
        except pj.Error as e:
            self.log(f"libRegisterThread warning (dtmf): {e}")

        self.log(f"sending DTMF digit-by-digit: {digits}")
        for digit in digits:
            if self.disconnected:
                break
            try:
                self.dialDtmf(digit)
                self.log(f"DTMF digit sent: {digit}")
            except pj.Error as e:
                self.log(f"DTMF digit {digit} failed: {e}")
            except Exception as e:
                self.log(f"DTMF digit {digit} unexpected error: {e}")
            time.sleep(INTER_DIGIT_MS / 1000.0)
        self.log("DTMF sequence complete")
        self.on_action_complete(expected_idx=expected_idx)

    def start_next_action(self):
        with self._lock:
            if self.disconnected or self._transfer_detected or not self.call_audio:
                return
            if self.action_idx >= len(self.actions):
                self.log("all actions finished")
                return
            action_type, action_value = self.actions[self.action_idx]
            expected_idx = self.action_idx
            self.last_action_type = action_type
            call_audio = self.call_audio

        self.stop_rtp_keepalive()

        if action_type == "wav":
            with self._lock:
                self.player = FilePlayer(self, action_value, expected_idx)
                player = self.player

            try:
                player.start_into(call_audio)
            except pj.Error as e:
                self.log(f"playback start failed for {action_value}: {describe_pj_error(e)}")
                self.log(describe_media("failed_wav_player", player))
                self.log(describe_media("failed_call_audio", call_audio))
                with self._lock:
                    if self.player is player:
                        self.player = None
                self.start_rtp_keepalive()
            return

        if action_type == "dtmf":
            threading.Thread(
                target=self._send_dtmf_digits,
                args=(action_value, expected_idx),
                daemon=True,
            ).start()
            return

        self.log(f"unknown action type: {action_type}")
        self.on_action_complete(expected_idx=expected_idx)

    def on_action_complete(self, expected_idx=None):
        with self._lock:
            # Guard: if another thread (e.g. a second onEof2 from file looping)
            # already advanced action_idx past what we expect, skip this call.
            if expected_idx is not None and self.action_idx != expected_idx:
                return
            if self._transfer_detected:
                return
            self.action_idx += 1
            self.player = None

            if self.disconnected:
                return

            next_needed = self.action_idx < len(self.actions)
            require_new_prompt = (self.last_action_type == "dtmf")

        if next_needed:
            self.log("waiting for next remote response before next action")
            self.start_rtp_keepalive()
            self.start_wait_thread(
                timeout_secs=NEXT_TURN_WAIT_TIMEOUT_SECS,
                label=f"after-action-{self.action_idx}",
                require_prompt_start=require_new_prompt,
                merge_bridge_gap=(self.action_idx == 1),
            )
        else:
            self.log("action sequence complete")
            self.start_rtp_keepalive()

    def onCallTransferRequest(self, prm):
        # Fires when Asterisk sends a SIP REFER to us
        self.log(f"*** TRANSFER via REFER to: {prm.dstUri} — declining and hanging up")
        prm.statusCode = 603  # Decline
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallRedirected(self, prm):
        # Fires when Asterisk sends a 3xx redirect
        target = getattr(prm, "targetUri", "<unknown>")
        self.log(f"*** TRANSFER via 3xx redirect to: {target} — hanging up")
        prm.opt = getattr(pj, "PJSIP_REDIRECT_STOP", 2)  # stop following redirects
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallReplaced(self, prm):
        # Fires when our call is replaced (attended transfer)
        self.log("*** TRANSFER via call replace — hanging up")
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallTsxState(self, prm):
        # Log every SIP transaction so we can see what Asterisk sends during transfer
        try:
            e = prm.e
            method = getattr(e.body.tsxState, "method", "")
            status = getattr(e.body.tsxState, "statusCode", "")
            self.log(f"SIP tsx: method={method} status={status}")
        except Exception:
            pass

    def _hangup_after_transfer(self):
        time.sleep(0.3)
        self.safe_hangup()

    def start(self):
        prm = pj.CallOpParam(True)
        prm.opt.audioCount = 1
        prm.opt.videoCount = 0
        prm.opt.textCount = 0
        self.start_transfer_monitor()
        self.makeCall(self.dst_uri, prm)

    def safe_hangup(self):
        if self.disconnected:
            return
        try:
            if self.getId() < 0:
                return  # makeCall() never succeeded — no valid PJSIP slot
        except Exception:
            return
        try:
            prm = pj.CallOpParam()
            self.hangup(prm)
        except Exception as e:
            self.log(f"hangup warning: {e}")


def main():
    ep = pj.Endpoint()
    acc = None
    calls = []
    ami_ready_events = None

    try:
        if USE_AMI_READY_EVENTS:
            print(
                "*** AMI config from .env: "
                f"host={AMI_HOST} port={AMI_PORT} user={AMI_USER or '<empty>'} "
                f"secret={masked_secret(AMI_SECRET)} "
                f"ready_event={AMI_READY_EVENT_NAME} "
                f"caller_filter={AMI_EVENT_CALLER or '<none>'} "
                f"trace={AMI_TRACE_EVENTS} "
                f"agi_stream_events={AMI_USE_AGI_STREAM_EVENTS} "
                f"detect_transfer={AMI_DETECT_TRANSFER} "
                f"transfer_context_prefixes={AMI_TRANSFER_CONTEXT_PREFIX_LIST} "
                f"transfer_dial_targets={AMI_TRANSFER_DIAL_TARGET_LIST} "
                f"hangup_on_transfer={HANGUP_ON_AMI_TRANSFER}"
            )
            try:
                ami_ready_events = AmiReadyEvents(
                    host=AMI_HOST,
                    port=AMI_PORT,
                    username=AMI_USER,
                    secret=AMI_SECRET,
                    ready_event_name=AMI_READY_EVENT_NAME,
                    caller_filter=AMI_EVENT_CALLER,
                    trace=AMI_TRACE_EVENTS,
                )
                ami_ready_events.start()
                print(
                    f"*** AMI ready-event listener started: {AMI_HOST}:{AMI_PORT} "
                    f"custom_event={AMI_READY_EVENT_NAME} "
                    f"agi_stream_events={AMI_USE_AGI_STREAM_EVENTS} "
                    f"detect_transfer={AMI_DETECT_TRANSFER}"
                )
            except Exception as e:
                ami_ready_events = None
                print(f"*** AMI listener unavailable; using RTP silence detection: {e}")

        bind_ip = get_bind_ip()
        print(f"*** chosen local bind IP: {bind_ip}")

        ep_cfg = pj.EpConfig()
        configure_endpoint(ep_cfg)

        ep.libCreate()
        ep.libInit(ep_cfg)
        ep.audDevManager().setNullDev()

        transport_id = make_transport(ep, bind_ip)
        ep.libStart()
        print("*** PJSUA2 STARTED ***")

        print_transport_info(ep, transport_id)
        configure_codecs(ep)

        acfg = build_account_config(bind_ip, transport_id)

        acc = MyAccount()
        acc.create(acfg)

        print("*** account created without registration")
        print(f"*** starting {NUM_CALLS} direct INVITE call(s)")

        for call_id in range(1, NUM_CALLS + 1):
            call = MyCall(
                ep=ep,
                acc=acc,
                call_id=call_id,
                dst_uri=DEST_URI,
                actions=ACTIONS,
                silence_wav=SILENCE_PAD_WAV,
                ami_ready_events=ami_ready_events,
            )
            calls.append(call)
            call.log(f"starting direct INVITE call to {DEST_URI}")
            call.start()

            if call_id < NUM_CALLS and CALL_START_GAP_MS > 0:
                time.sleep(CALL_START_GAP_MS / 1000.0)

        started = time.time()
        while time.time() - started < MAX_CALL_SECONDS:
            active_calls = [call for call in calls if not call.disconnected]
            if not active_calls:
                break
            time.sleep(0.1)

        remaining_calls = [call for call in calls if not call.disconnected]
        if remaining_calls:
            print(f"*** max call time reached, hanging up {len(remaining_calls)} remaining call(s)")
            for call in remaining_calls:
                call.safe_hangup()

            wait_start = time.time()
            while time.time() - wait_start < 3:
                if all(call.disconnected for call in calls):
                    break
                time.sleep(0.1)

    except pj.Error as e:
        print(f"*** PJSUA2 error: {e}")
    except KeyboardInterrupt:
        print("*** interrupted by user; hanging up active calls")
    except Exception as e:
        print(f"*** general error: {e}")
    finally:
        if ami_ready_events is not None:
            ami_ready_events.stop()

        for call in calls:
            if not call.disconnected:
                call.safe_hangup()

        wait_start = time.time()
        while time.time() - wait_start < 5:
            if all(c.disconnected for c in calls):
                break
            time.sleep(0.1)

        # Join wait threads so their bound-method references to MyCall are released
        # before calls.clear() drops the list reference. Without this, a running
        # thread keeps MyCall alive past libDestroy(), causing the C-level assertion
        # "call_id >= 0 && call_id < max_calls" when the destructor fires too late.
        for call in calls:
            t = call._wait_thread
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

            t = call._transfer_thread
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

        for call in calls:
            call.release_pjsua2_ownership()

        calls.clear()
        acc = None
        gc.collect()  # break any remaining reference cycles so pj.Call destructors fire now

        try:
            ep.libDestroy()
        except Exception as e:
            print(f"*** libDestroy warning: {e}")


if __name__ == "__main__":
    main()
