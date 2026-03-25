import time
import math
import struct
import socket
import threading
from pathlib import Path
import pjsua2 as pj


# =========================
# CONFIG
# =========================
LOCAL_SIP_PORT = 5062
REMOTE_SIP_PORT = 5060
MEDIA_RTP_PORT = 4000
MEDIA_RTP_PORT_RANGE = 400

ASTERISK_HOST = "10.29.32.138"

CALLER_USER = "1001"
CALLER_PASS = "b0f1306769fe67fec2b2e0941e34d962"
CALLER_DISPLAY = "Rahul"

DEST_URI = f"sip:19073750302@{ASTERISK_HOST}:{REMOTE_SIP_PORT}"

# Keep UDP first so it stays close to your SIPp test.
USE_TCP = False

# Let the code auto-pick the local interface used to reach Asterisk.
FORCE_BIND_IP = None

# Usually leave this empty. Only set it if you explicitly want a different advertised address.
FORCE_PUBLIC_IP = None

PLAYLIST = [
    "first.wav",
    "second.wav",
    "name.wav",
    "birthday.wav",
]

# After the whole WAV playlist is done, wait for the next remote prompt
# to finish (voice detected, then silence detected), then send this DTMF.
FINAL_DTMF_DIGITS = "5408249373"

# Keep this as whatever file you already have.
# If your calls are long, you can switch to a longer silence file later.
SILENCE_PAD_WAV = "silence_60s.wav"

CALLS_OUTPUT_DIR = "call_recordings"

NUM_CALLS = 1
CALL_START_GAP_MS = 200
# Change only NUM_CALLS to 10, 20, etc.
MAX_CALL_SECONDS = 1800

# Old working silence behavior
SILENCE_AFTER_VOICE_MS = 1500
POLL_MS = 100

# After the first local answer, cap the total silence budget from the
# last remote voice frame so we speak before Asterisk Record() times out.
POST_REDIRECT_TOTAL_SILENCE_MS = 2200

# Frame-energy VAD threshold
VOICE_ENERGY_THRESHOLD = 300.0

# Fallback timeouts
INITIAL_WAIT_TIMEOUT_SECS = 60
NEXT_TURN_WAIT_TIMEOUT_SECS = 60


# =========================
# HELPERS
# =========================
def safe_set(obj, attr, value):
    if not hasattr(obj, attr):
        return False
    try:
        setattr(obj, attr, value)
        return True
    except Exception as e:
        print(f"*** could not set {obj.__class__.__name__}.{attr}: {e}")
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


def print_transport_info(ep: pj.Endpoint, transport_id: int):
    try:
        info = ep.transportGetInfo(transport_id)
        print(f"*** transport type: {info.typeName}")
        print(f"*** transport localAddress: {info.localAddress.host}:{info.localAddress.port}")
        print(f"*** transport localName: {info.localName.host}:{info.localName.port}")
        print(f"*** transport info: {info.info}")
    except Exception as e:
        print(f"*** could not fetch transport info: {e}")


def configure_codecs(ep: pj.Endpoint):
    """
    Keep only PCMU active so the SDP is as small/simple as possible.
    """
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

    # Bind to the exact interface we are actually using to reach Asterisk
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

    transport_id = ep.transportCreate(tp_type, tp_cfg)
    return transport_id


def configure_endpoint(ep_cfg: pj.EpConfig):
    # Logging
    ep_cfg.logConfig.level = 5
    ep_cfg.logConfig.consoleLevel = 5

    # Keep UA side minimal
    safe_set(ep_cfg.uaConfig, "userAgent", "")
    safe_set(ep_cfg.uaConfig, "natTypeInSdp", 0)
    safe_set(ep_cfg.uaConfig, "enableUpnp", False)

    # Media defaults
    ep_cfg.medConfig.clockRate = 8000
    ep_cfg.medConfig.channelCount = 1
    ep_cfg.medConfig.sndClockRate = 8000
    ep_cfg.medConfig.quality = 4
    ep_cfg.medConfig.noVad = True
    ep_cfg.medConfig.sndAutoCloseTime = -1


def build_account_config(bind_ip: str, transport_id: int) -> pj.AccountConfig:
    acfg = pj.AccountConfig()

    # SIPp-like From identity
    acfg.idUri = f'"{CALLER_DISPLAY}" <sip:{CALLER_USER}@{ASTERISK_HOST}>'

    # Do not REGISTER
    acfg.regConfig.registerOnAdd = False

    # Use only the chosen transport
    safe_set(acfg.sipConfig, "transportId", transport_id)

    # Credentials for 401 on INVITE
    acfg.sipConfig.authCreds.append(
        pj.AuthCredInfo("digest", "*", CALLER_USER, 0, CALLER_PASS)
    )

    # Keep auth flow like SIPp
    safe_set(acfg.sipConfig, "authInitialEmpty", False)
    safe_set(acfg.sipConfig, "useSharedAuth", False)

    # Force a simple Contact and avoid extra Contact decorations
    safe_set(acfg.sipConfig, "contactForced", f"sip:{CALLER_USER}@{bind_ip}:{LOCAL_SIP_PORT}")
    safe_set(acfg.sipConfig, "contactParams", "")
    safe_set(acfg.sipConfig, "contactUriParams", "")

    # Minimize feature headers
    safe_set(acfg.callConfig, "prackUse", pj.PJSUA_100REL_NOT_USED)
    safe_set(acfg.callConfig, "timerUse", pj.PJSUA_SIP_TIMER_INACTIVE)

    # Minimize NAT/account-side rewriting
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

    # Media transport bound to same interface as SIP
    safe_set(acfg.mediaConfig.transportConfig, "port", MEDIA_RTP_PORT)
    safe_set(acfg.mediaConfig.transportConfig, "portRange", MEDIA_RTP_PORT_RANGE)
    safe_set(acfg.mediaConfig.transportConfig, "boundAddress", bind_ip)

    # Keep SDP/media small
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




# =========================
# ACCOUNT
# =========================
class MyAccount(pj.Account):
    def __init__(self):
        super().__init__()


# =========================
# PLAYER
# =========================
class FilePlayer(pj.AudioMediaPlayer):
    def __init__(self, owner, wav_path):
        super().__init__()
        self.owner = owner
        self.wav_path = wav_path

    def start_into(self, call_audio):
        self.owner.log(f"starting playback: {self.wav_path}")
        self.createPlayer(self.wav_path)
        self.startTransmit(call_audio)

        if self.owner.mixed_recorder is not None:
            try:
                self.startTransmit(self.owner.mixed_recorder)
            except pj.Error as e:
                self.owner.log(f"failed to feed mixed recorder from {self.wav_path}: {e}")

    def onEof2(self):
        self.owner.log(f"local WAV finished: {self.wav_path}")
        self.owner.on_player_eof()


# =========================
# REMOTE AUDIO TAP
# =========================
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


# =========================
# CALL
# =========================
class MyCall(pj.Call):
    def __init__(self, ep, acc, call_id, dst_uri, wavs, remote_recording, mixed_recording, silence_wav):
        super().__init__(acc)
        self.ep = ep
        self.call_id = call_id
        self.dst_uri = dst_uri
        self.wavs = list(wavs)
        self.remote_recording = remote_recording
        self.mixed_recording = mixed_recording
        self.silence_wav = silence_wav

        self.call_audio = None
        self.recorder = None
        self.mixed_recorder = None
        self.player = None
        self.keepalive_player = None
        self.remote_tap = None

        self.play_idx = 0
        self.final_dtmf = FINAL_DTMF_DIGITS
        self.final_dtmf_sent = False

        self.media_ready = False
        self.disconnected = False
        self.connected = False

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._wait_thread = None
        self._waiting_for_remote = False

        # Real remote-audio VAD state
        self.voice_energy_threshold = VOICE_ENERGY_THRESHOLD
        self.remote_seen_voice = False
        self.last_voice_ts = 0.0
        self.last_frame_energy = 0.0

        # Wait-mode flags
        self.current_wait_requires_prompt_start = False
        self.current_wait_merge_bridge_gap = False

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
                self.recorder = None
                self.mixed_recorder = None
                self.remote_tap = None
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

            try:
                if self.recorder is None:
                    self.recorder = pj.AudioMediaRecorder()
                    self.recorder.createRecorder(self.remote_recording)
                    self.call_audio.startTransmit(self.recorder)
                    self.log(f"remote recording started: {self.remote_recording}")
            except pj.Error as e:
                self.log(f"recorder setup failed: {e}")

            try:
                if self.mixed_recorder is None:
                    self.mixed_recorder = pj.AudioMediaRecorder()
                    self.mixed_recorder.createRecorder(self.mixed_recording)
                    self.call_audio.startTransmit(self.mixed_recorder)
                    self.log(f"mixed recording started: {self.mixed_recording}")
            except pj.Error as e:
                self.log(f"mixed recorder setup failed: {e}")

            try:
                if self.remote_tap is None:
                    self.remote_tap = RemoteTap(self)
                    self.remote_tap.create()
                    self.call_audio.startTransmit(self.remote_tap)
                    self.log("remote tap started")
            except pj.Error as e:
                self.log(f"remote tap setup failed: {e}")

            # Start outbound silence immediately so Asterisk receives RTP.
            self.start_rtp_keepalive()

            # First turn only:
            # ignore silence until we've heard real remote audio once.
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

        started = time.time()
        last_log_at = 0.0

        try:
            while not self._stop_evt.is_set():
                now = time.time()

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

                # First turn only:
                # ignore silence until at least one real remote voice frame is heard.
                if require_prompt_start and not seen_voice:
                    if now - started >= timeout_secs:
                        self.log(f"first turn heard no remote voice, forcing playback ({label})")
                        with self._lock:
                            self._waiting_for_remote = False
                            self.current_wait_requires_prompt_start = False
                            self.current_wait_merge_bridge_gap = False
                        self.start_next_action()
                        return

                    time.sleep(POLL_MS / 1000.0)
                    continue

                # As soon as first remote voice is heard, switch back to old logic
                if require_prompt_start and seen_voice:
                    with self._lock:
                        self.current_wait_requires_prompt_start = False

                # Old working silence logic, with one narrow exception:
                # after the first local answer, treat redirect audio + server-2
                # prompt as one remote turn, but keep the total silence budget
                # below the Asterisk Record() timeout.
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

                if (not seen_voice) and (now - started >= timeout_secs):
                    self.log(f"no remote voice detected, forcing playback ({label})")
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

    def start_next_action(self):
        with self._lock:
            if self.disconnected or not self.call_audio:
                return

        # Stop silence before sending a real prompt or DTMF
        self.stop_rtp_keepalive()

        with self._lock:
            if self.play_idx < len(self.wavs):
                wav = self.wavs[self.play_idx]
                self.player = FilePlayer(self, wav)
                player = self.player
                call_audio = self.call_audio
            else:
                wav = None
                player = None
                call_audio = self.call_audio
                should_send_final_dtmf = bool(self.final_dtmf) and not self.final_dtmf_sent

        if wav is None:
            if should_send_final_dtmf:
                self.send_final_dtmf()
            else:
                self.log("all playback and final DTMF finished")
                self.start_rtp_keepalive()
            return

        try:
            player.start_into(call_audio)
        except pj.Error as e:
            self.log(f"playback start failed for {wav}: {e}")
            self.start_rtp_keepalive()

    def send_final_dtmf(self):
        with self._lock:
            if self.disconnected or not self.call_audio:
                return

            if self.final_dtmf_sent or not self.final_dtmf:
                self.start_rtp_keepalive()
                return

            digits = self.final_dtmf
            self.final_dtmf_sent = True

        try:
            self.log(f"sending final DTMF after remote prompt end: {digits}")
            if hasattr(self, "dialDtmf"):
                self.dialDtmf(digits)
            else:
                prm = pj.CallSendDtmfParam()
                prm.digits = digits
                self.sendDtmf(prm)
            self.log("final DTMF sent")
        except pj.Error as e:
            self.log(f"final DTMF send failed: {e}")
        except Exception as e:
            self.log(f"final DTMF send unexpected error: {e}")
        finally:
            self.start_rtp_keepalive()

    def on_player_eof(self):
        with self._lock:
            self.play_idx += 1
            self.player = None

            if self.disconnected:
                return

            next_needed = self.play_idx < len(self.wavs)
            should_wait_for_final_dtmf = bool(self.final_dtmf) and not self.final_dtmf_sent

        if next_needed:
            self.log("waiting for next remote response before next WAV")
            self.start_rtp_keepalive()
            self.start_wait_thread(
                timeout_secs=NEXT_TURN_WAIT_TIMEOUT_SECS,
                label=f"after-local-{self.play_idx}",
                require_prompt_start=False,
                merge_bridge_gap=(self.play_idx == 1),
            )
        elif should_wait_for_final_dtmf:
            self.log("playlist complete, waiting for final remote prompt end before DTMF")
            self.start_rtp_keepalive()
            self.start_wait_thread(
                timeout_secs=NEXT_TURN_WAIT_TIMEOUT_SECS,
                label="before-final-dtmf",
                require_prompt_start=False,
                merge_bridge_gap=False,
            )
        else:
            self.log("playlist complete")
            self.start_rtp_keepalive()

    def start(self):
        prm = pj.CallOpParam(True)

        prm.opt.audioCount = 1
        prm.opt.videoCount = 0
        prm.opt.textCount = 0

        self.makeCall(self.dst_uri, prm)

    def safe_hangup(self):
        if self.disconnected:
            return
        try:
            prm = pj.CallOpParam()
            self.hangup(prm)
        except Exception as e:
            self.log(f"hangup warning: {e}")


# =========================
# MAIN
# =========================
def main():
    ep = pj.Endpoint()
    acc = None
    calls = []

    try:
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
                wavs=PLAYLIST,
                remote_recording=build_recording_path("remote", call_id),
                mixed_recording=build_recording_path("mixed", call_id),
                silence_wav=SILENCE_PAD_WAV,
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

        calls.clear()
        acc = None
        time.sleep(1.0)

    except pj.Error as e:
        print(f"*** PJSUA2 error: {e}")
    except Exception as e:
        print(f"*** general error: {e}")
    finally:
        try:
            ep.libDestroy()
        except Exception as e:
            print(f"*** libDestroy warning: {e}")


if __name__ == "__main__":
    main()
