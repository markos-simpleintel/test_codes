import time
import threading
import pjsua2 as pj


# =========================
# CONFIG
# =========================
LOCAL_SIP_PORT = 5062

ASTERISK_HOST = "10.29.32.138"

CALLER_USER = "1001"
CALLER_PASS = "b0f1306769fe67fec2b2e0941e34d962"
CALLER_DISPLAY = "Rahul"

DEST_URI = "sip:19073750302@10.29.32.138"

# Use the IP address that Asterisk can really reach.
ADVERTISED_IP = "192.168.220.196"

# Keep UDP first because your SIPp worked with UDP.
USE_TCP = False

PLAYLIST = [
    "audio1.wav",
    "audio2.wav",
    "name.wav",
    "birthday.wav",
]

# Make this file longer than MAX_CALL_SECONDS.
# Example:
# ffmpeg -f lavfi -i anullsrc=r=8000:cl=mono -t 300 -c:a pcm_s16le silence_300s.wav
SILENCE_PAD_WAV = "silence_60s.wav"

REMOTE_RECORDING = "remote.wav"
MIXED_RECORDING = "mixed.wav"

MAX_CALL_SECONDS = 180

# Remote-turn detector
# A turn is considered finished after:
# 1) remote speech/noise is detected above threshold
# 2) then silence continues for this long
RX_THRESHOLD = 0
SILENCE_AFTER_VOICE_MS = 2000
POLL_MS = 100

# Fallback:
# If no remote voice is detected at all for this many seconds,
# force the next local playback so the call does not stall forever.
INITIAL_WAIT_TIMEOUT_SECS = 28
NEXT_TURN_WAIT_TIMEOUT_SECS = 15


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
        print(f"*** starting playback: {self.wav_path}")
        self.createPlayer(self.wav_path)
        self.startTransmit(call_audio)

        # Also send local playback into the mixed recorder
        if self.owner.mixed_recorder is not None:
            try:
                self.startTransmit(self.owner.mixed_recorder)
            except pj.Error as e:
                print(f"*** failed to feed mixed recorder from {self.wav_path}: {e}")

    def onEof2(self):
        print(f"*** local WAV finished: {self.wav_path}")
        self.owner.on_player_eof()


# =========================
# CALL
# =========================
class MyCall(pj.Call):
    def __init__(self, ep, acc, dst_uri, wavs, remote_recording, mixed_recording, silence_wav):
        super().__init__(acc)
        self.ep = ep
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

        self.play_idx = 0
        self.media_ready = False
        self.disconnected = False
        self.connected = False

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._wait_thread = None
        self._waiting_for_remote = False

    def onCallState(self, prm):
        ci = self.getInfo()
        print(
            f"*** call state: {ci.stateText} | "
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
                self._waiting_for_remote = False

    def onCallMediaState(self, prm):
        ci = self.getInfo()

        for i, m in enumerate(ci.media):
            if m.type != pj.PJMEDIA_TYPE_AUDIO:
                continue

            try:
                audio_media = self.getAudioMedia(i)
            except pj.Error as e:
                print(f"*** getAudioMedia failed: {e}")
                continue

            with self._lock:
                self.call_audio = audio_media
                self.media_ready = True

            print("*** media is ready")

            try:
                if self.recorder is None:
                    self.recorder = pj.AudioMediaRecorder()
                    self.recorder.createRecorder(self.remote_recording)
                    self.call_audio.startTransmit(self.recorder)
                    print(f"*** remote recording started: {self.remote_recording}")
            except pj.Error as e:
                print(f"*** recorder setup failed: {e}")

            try:
                if self.mixed_recorder is None:
                    self.mixed_recorder = pj.AudioMediaRecorder()
                    self.mixed_recorder.createRecorder(self.mixed_recording)
                    self.call_audio.startTransmit(self.mixed_recorder)
                    print(f"*** mixed recording started: {self.mixed_recording}")
            except pj.Error as e:
                print(f"*** mixed recorder setup failed: {e}")

            # Start outbound silence immediately so Asterisk receives RTP.
            self.start_rtp_keepalive()

            # Initial wait for remote greeting/IVR turn to finish
            self.start_wait_thread(
                timeout_secs=INITIAL_WAIT_TIMEOUT_SECS,
                label="initial-remote-turn",
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
            print(f"*** RTP keepalive silence started: {self.silence_wav}")
        except pj.Error as e:
            print(f"*** failed to start RTP keepalive silence: {e}")
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
                print("*** RTP keepalive silence stopped")
            except pj.Error as e:
                print(f"*** failed to stop RTP keepalive silence: {e}")

    def start_wait_thread(self, timeout_secs, label):
        with self._lock:
            if self.disconnected or not self.call_audio:
                return
            if self._waiting_for_remote:
                return
            self._waiting_for_remote = True

        t = threading.Thread(
            target=self.wait_for_remote_turn_end,
            args=(timeout_secs, label),
            daemon=True,
        )
        self._wait_thread = t
        t.start()

    def wait_for_remote_turn_end(self, timeout_secs, label):
        try:
            self.ep.libRegisterThread(f"wait-{label}")
            print(f"*** registered wait thread with PJLIB: {label}")
        except pj.Error as e:
            print(f"*** libRegisterThread warning ({label}): {e}")

        seen_voice = False
        silent_ms = 0
        started = time.time()
        last_rx_log_at = 0.0

        try:
            while not self._stop_evt.is_set():
                with self._lock:
                    call_audio = self.call_audio

                if not call_audio:
                    time.sleep(0.1)
                    continue

                try:
                    rx = call_audio.getRxLevel()
                except pj.Error as e:
                    print(f"*** getRxLevel failed ({label}): {e}")
                    time.sleep(0.1)
                    continue

                # Light debug logging so the console does not flood too hard
                now = time.time()
                if now - last_rx_log_at >= 1.0:
                    print(f"*** rx level ({label}): {rx}")
                    last_rx_log_at = now

                if rx > RX_THRESHOLD:
                    if not seen_voice:
                        print(f"*** remote audio detected ({label})")
                    seen_voice = True
                    silent_ms = 0
                else:
                    if seen_voice:
                        silent_ms += POLL_MS

                if seen_voice and silent_ms >= SILENCE_AFTER_VOICE_MS:
                    print(f"*** remote turn seems finished ({label})")
                    with self._lock:
                        self._waiting_for_remote = False
                    self.start_next_file()
                    return

                if (not seen_voice) and (time.time() - started >= timeout_secs):
                    print(f"*** no remote voice detected, forcing playback ({label})")
                    with self._lock:
                        self._waiting_for_remote = False
                    self.start_next_file()
                    return

                time.sleep(POLL_MS / 1000.0)
        finally:
            with self._lock:
                self._waiting_for_remote = False

    def start_next_file(self):
        with self._lock:
            if self.disconnected or not self.call_audio:
                return

        # Stop silence before sending a real prompt
        self.stop_rtp_keepalive()

        with self._lock:
            if self.play_idx >= len(self.wavs):
                print("*** all playback finished")
                self.start_rtp_keepalive()
                return

            wav = self.wavs[self.play_idx]
            self.player = FilePlayer(self, wav)
            player = self.player
            call_audio = self.call_audio

        try:
            player.start_into(call_audio)
        except pj.Error as e:
            print(f"*** playback start failed for {wav}: {e}")
            self.start_rtp_keepalive()

    def on_player_eof(self):
        with self._lock:
            self.play_idx += 1
            self.player = None

            if self.disconnected:
                return

            next_needed = self.play_idx < len(self.wavs)

        if next_needed:
            print("*** waiting for next remote response before next WAV")
            self.start_rtp_keepalive()
            self.start_wait_thread(
                timeout_secs=NEXT_TURN_WAIT_TIMEOUT_SECS,
                label=f"after-local-{self.play_idx}",
            )
        else:
            print("*** playlist complete")
            self.start_rtp_keepalive()

    def start(self):
        prm = pj.CallOpParam(True)

        # Make media offer simple
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
            print(f"*** hangup warning: {e}")


# =========================
# HELPERS
# =========================
def configure_codecs(ep: pj.Endpoint):
    """
    Reduce SDP size to be closer to SIPp.
    Keep only PCMU/8000 active.
    """
    try:
        codec_infos = ep.codecEnum2()
    except Exception as e:
        print(f"*** codec enumeration failed: {e}")
        return

    keep_prefixes = (
        "PCMU/8000",
    )

    print("*** codec list before priority update:")
    for c in codec_infos:
        print(f"    {c.codecId}")

    for c in codec_infos:
        codec_id = c.codecId
        prio = 0
        for prefix in keep_prefixes:
            if codec_id.startswith(prefix):
                prio = 255
                break

        try:
            ep.codecSetPriority(codec_id, prio)
        except Exception as e:
            print(f"*** failed to set codec priority for {codec_id}: {e}")

    print("*** codec priority update done")


def make_transport(ep: pj.Endpoint):
    tp_cfg = pj.TransportConfig()
    tp_cfg.port = LOCAL_SIP_PORT

    if ADVERTISED_IP:
        tp_cfg.publicAddress = ADVERTISED_IP
        print(f"*** advertising SIP transport address: {ADVERTISED_IP}")

    if USE_TCP:
        tp_type = pj.PJSIP_TRANSPORT_TCP
        print("*** using TCP transport")
    else:
        tp_type = pj.PJSIP_TRANSPORT_UDP
        print("*** using UDP transport")

    ep.transportCreate(tp_type, tp_cfg)


# =========================
# MAIN
# =========================
def main():
    ep = pj.Endpoint()
    acc = None
    call = None

    try:
        ep_cfg = pj.EpConfig()

        # Logging
        ep_cfg.logConfig.level = 5
        ep_cfg.logConfig.consoleLevel = 5

        # Media behavior
        ep_cfg.medConfig.clockRate = 8000
        ep_cfg.medConfig.channelCount = 1
        ep_cfg.medConfig.sndClockRate = 8000
        ep_cfg.medConfig.quality = 4
        ep_cfg.medConfig.noVad = True
        ep_cfg.medConfig.sndAutoCloseTime = -1

        ep.libCreate()
        ep.libInit(ep_cfg)

        # Null device gives timing to the conference bridge
        ep.audDevManager().setNullDev()

        make_transport(ep)
        ep.libStart()
        print("*** PJSUA2 STARTED ***")

        configure_codecs(ep)

        acfg = pj.AccountConfig()

        # SIPp-like identity
        acfg.idUri = f'"{CALLER_DISPLAY}" <sip:{CALLER_USER}@{ASTERISK_HOST}>'

        # Do not REGISTER
        acfg.regConfig.registerOnAdd = False

        # Credentials for 401 challenge on INVITE
        acfg.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", CALLER_USER, 0, CALLER_PASS)
        )

        acc = MyAccount()
        acc.create(acfg)

        print("*** account created without registration")
        print("*** starting direct INVITE call")

        call = MyCall(
            ep=ep,
            acc=acc,
            dst_uri=DEST_URI,
            wavs=PLAYLIST,
            remote_recording=REMOTE_RECORDING,
            mixed_recording=MIXED_RECORDING,
            silence_wav=SILENCE_PAD_WAV,
        )
        call.start()

        started = time.time()
        while time.time() - started < MAX_CALL_SECONDS:
            if call.disconnected:
                break
            time.sleep(0.1)

        if call and not call.disconnected:
            print("*** max call time reached, hanging up")
            call.safe_hangup()

            wait_start = time.time()
            while time.time() - wait_start < 3:
                if call.disconnected:
                    break
                time.sleep(0.1)

        call = None
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