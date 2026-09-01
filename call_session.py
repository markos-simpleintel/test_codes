import threading
import time
import uuid

import pjsua2 as pj

from audio_assets import frame_rms, resolve_input_audio
from config import (
    CALLER_USER,
    DTMF_DURATION_MS,
    DTMF_GATE_MAX_WAIT_MS,
    DTMF_HERD_SPACING_MS,
    DTMF_METHOD,
    DTMF_MIN_PROMPT_VOICE_MS,
    DTMF_SETTLE_EXTRA_MS,
    DTMF_SETTLE_MS_PER_DIGIT,
    DTMF_SILENCE_AFTER_VOICE_MS,
    HANGUP_ON_AMI_TRANSFER,
    INITIAL_WAIT_TIMEOUT_SECS,
    MEDIA_SETUP_ATTEMPTS,
    MEDIA_SETUP_RETRY_MS,
    NEXT_TURN_WAIT_TIMEOUT_SECS,
    POLL_MS,
    POST_REDIRECT_TOTAL_SILENCE_MS,
    SILENCE_AFTER_VOICE_MS,
    TAP_FRAME_DECIMATION,
    TEST_CALL_ID_HEADER,
    TX_CHANNEL_COUNT,
    TX_CLOCK_RATE,
    TX_FRAME_BYTES,
    TX_FRAME_PTIME_MS,
    TX_SAMPLE_WIDTH_BYTES,
    VOICE_ENERGY_THRESHOLD,
)
from pjsip_helpers import is_active_audio_media
from run_logging import log_message


MEDIA_GRAPH_LOCK = threading.RLock()
_DTMF_GATE_LOCK = threading.Lock()
_dtmf_last_start_ts = 0.0


class MyAccount(pj.Account):
    def __init__(self):
        super().__init__()


class TxAudioPort(pj.AudioMediaPort):
    """Permanent per-call source that emits cached audio or silence."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._lock = threading.Lock()
        self._frames = None
        self._frame_index = 0
        self._playback_name = None
        self._silence_frame = pj.ByteVector(b"\x00" * TX_FRAME_BYTES)

    def create(self):
        media_format = pj.MediaFormatAudio()
        try:
            media_format.type = pj.PJMEDIA_TYPE_AUDIO
        except Exception:
            pass
        media_format.clockRate = TX_CLOCK_RATE
        media_format.channelCount = TX_CHANNEL_COUNT
        media_format.bitsPerSample = TX_SAMPLE_WIDTH_BYTES * 8
        media_format.frameTimeUsec = TX_FRAME_PTIME_MS * 1000
        self.createPort(f"tx-audio-{self.owner.call_id:02d}", media_format)

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
    """Observe inbound audio for per-call voice and silence detection."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._frame_count = 0

    def create(self):
        media_format = pj.MediaFormatAudio()
        try:
            media_format.type = pj.PJMEDIA_TYPE_AUDIO
        except Exception:
            pass
        media_format.clockRate = TX_CLOCK_RATE
        media_format.channelCount = TX_CHANNEL_COUNT
        media_format.bitsPerSample = TX_SAMPLE_WIDTH_BYTES * 8
        media_format.frameTimeUsec = TX_FRAME_PTIME_MS * 1000
        self.createPort(f"remote-tap-{self.owner.call_id:02d}", media_format)

    def onFrameReceived(self, frame):
        self._frame_count += 1
        if self._frame_count % TAP_FRAME_DECIMATION:
            return
        try:
            if frame.buf is None:
                return
            try:
                pcm = bytes(frame.buf)
            except Exception:
                try:
                    pcm = bytes(bytearray(frame.buf))
                except Exception:
                    return
            if not pcm:
                return

            energy = frame_rms(pcm)
            owner = self.owner
            owner.last_frame_energy = energy
            if energy >= owner.voice_energy_threshold:
                owner.remote_seen_voice = True
                owner.last_voice_ts = time.time()
                owner.voice_tick_count += 1
        except Exception as exc:
            self.owner.log(f"remote tap frame error: {exc}", "ERROR")


class MyCall(pj.Call):
    def __init__(
        self,
        ep,
        acc,
        call_id,
        dst_uri,
        actions,
        mixed_recording,
        audio_assets,
        ami_ready_events=None,
    ):
        super().__init__(acc)
        self.ep = ep
        self.call_id = call_id
        self.test_call_id = f"tc-{call_id:02d}-{uuid.uuid4().hex[:8]}"
        self.dst_uri = dst_uri
        self.actions = list(actions)
        self.mixed_recording = mixed_recording
        self.audio_assets = audio_assets
        self.ami_ready_events = ami_ready_events

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
        self._transfer_thread = None
        self._driver_started = False
        self._waiting_for_remote = False
        self._transfer_detected = False
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

    def log(self, message, level="INFO"):
        log_message(f"[call-{self.call_id:02d}] {message}", level)

    def start_transfer_monitor(self):
        if self.ami_ready_events is None or not self.ami_ready_events.is_running():
            return
        with self._lock:
            if self.disconnected or self._transfer_thread is not None:
                return
            self.current_channel_ami_sequence = (
                self.ami_ready_events.current_channel_sequence()
            )
            self.current_transfer_ami_sequence = (
                self.ami_ready_events.current_transfer_sequence()
            )

        self._transfer_thread = threading.Thread(
            target=self._wait_for_ami_transfer,
            name=f"transfer-{self.call_id}",
            daemon=True,
        )
        self._transfer_thread.start()

    def _wait_for_ami_transfer(self):
        try:
            self.ep.libRegisterThread(f"transfer-{self.call_id}")
        except pj.Error as exc:
            self.log(f"libRegisterThread warning (transfer): {exc}", "ERROR")

        self._bind_ami_channel()
        with self._lock:
            sequence = self.current_transfer_ami_sequence
            linkedid = self.ami_linkedid

        # Without a Linkedid, a global transfer event cannot safely be assigned
        # to one call during a concurrent run.
        if not linkedid:
            return

        event = self.ami_ready_events.wait_for_transfer_after(
            sequence,
            None,
            self._stop_evt,
            linkedid=linkedid,
        )
        if event and not self.disconnected:
            self.log(
                f"AMI transfer detected: "
                f"{self.ami_ready_events.summarize_event(event)}"
            )
            self._on_transfer_detected()

    def _bind_ami_channel(self):
        with self._lock:
            if self.ami_linkedid:
                return
            sequence = self.current_channel_ami_sequence

        event = self.ami_ready_events.wait_for_channel_after(
            sequence,
            timeout_secs=10,
            stop_evt=self._stop_evt,
            expected_caller=CALLER_USER,
        )
        if not event:
            self.log("AMI channel bind timed out; using audio silence")
            return

        with self._lock:
            self.ami_channel = event.get("Channel", "")
            self.ami_uniqueid = event.get("Uniqueid", "")
            self.ami_linkedid = event.get("Linkedid", "") or self.ami_uniqueid
        self.log(
            f"AMI channel bound: channel={self.ami_channel} "
            f"linkedid={self.ami_linkedid}"
        )

    def _on_transfer_detected(self):
        with self._lock:
            if self.disconnected or self._transfer_detected:
                return
            self._transfer_detected = True
            self._waiting_for_remote = False
        if self.tx_audio is not None:
            self.tx_audio.cancel_playback()
        self._ami_ready_evt.set()
        self._playback_done_evt.set()
        self.log("stopped local actions after PBX transfer was detected")
        if HANGUP_ON_AMI_TRANSFER:
            self.log("hanging up before live-agent bridge")
            self.safe_hangup()

    def start(self):
        call_param = pj.CallOpParam(True)
        call_param.opt.audioCount = 1
        call_param.opt.videoCount = 0
        call_param.opt.textCount = 0

        try:
            header = pj.SipHeader()
            header.hName = TEST_CALL_ID_HEADER
            header.hValue = self.test_call_id
            call_param.txOption.headers.append(header)
        except Exception as exc:
            self.log(f"could not attach {TEST_CALL_ID_HEADER}: {exc}", "ERROR")

        self.start_transfer_monitor()
        self.makeCall(self.dst_uri, call_param)

    def safe_hangup(self):
        if self.disconnected:
            return
        try:
            if self.getId() < 0:
                return
        except Exception:
            return
        try:
            self.hangup(pj.CallOpParam())
        except Exception as exc:
            self.log(f"hangup warning: {exc}", "ERROR")

    def onCallState(self, prm):
        call_info = self.getInfo()
        self.log(
            f"call state: {call_info.stateText} | "
            f"lastStatusCode={call_info.lastStatusCode} | "
            f"lastReason={call_info.lastReason}"
        )

        if call_info.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.connected = True

        if call_info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.disconnected = True
            self._stop_evt.set()
            self._ami_ready_evt.set()
            self._playback_done_evt.set()
            if self.tx_audio is not None:
                self.tx_audio.cancel_playback()
            with self._lock:
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False

    def onCallMediaState(self, prm):
        call_info = self.getInfo()
        for index, media_description in enumerate(call_info.media):
            if not is_active_audio_media(media_description):
                continue
            try:
                audio_media = self.getAudioMedia(index)
            except pj.Error as exc:
                self.log(f"getAudioMedia failed: {exc}", "ERROR")
                continue

            with self._lock:
                self.call_audio = audio_media
                self.media_ready = True
            self.log("media is ready")
            self.start_driver()
            break

    def onCallTransferRequest(self, prm):
        self.log(f"TRANSFER via REFER to {prm.dstUri}; declining and hanging up")
        prm.statusCode = 603
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallRedirected(self, prm):
        target = getattr(prm, "targetUri", "<unknown>")
        self.log(f"TRANSFER via redirect to {target}; hanging up")
        prm.opt = getattr(pj, "PJSIP_REDIRECT_STOP", 2)
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallReplaced(self, prm):
        self.log("TRANSFER via call replacement; hanging up")
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def _hangup_after_transfer(self):
        time.sleep(0.3)
        self.safe_hangup()

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
            except pj.Error as exc:
                last_error = exc
                self.log(
                    f"media connect attempt {attempt}/{MEDIA_SETUP_ATTEMPTS} "
                    f"failed ({label}): {exc}",
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

        try:
            tx_audio = TxAudioPort(self)
            with MEDIA_GRAPH_LOCK:
                tx_audio.create()
            self.tx_audio = tx_audio
        except Exception as exc:
            self.log(f"outbound media port creation failed: {exc}", "ERROR")
            return False

        if not self._connect_with_retry(tx_audio, call_audio, "tx-audio -> call"):
            return False

        try:
            remote_tap = RemoteTap(self)
            with MEDIA_GRAPH_LOCK:
                remote_tap.create()
            self.remote_tap = remote_tap
            tap_connected = self._connect_with_retry(
                call_audio, remote_tap, "call -> remote-tap"
            )
        except Exception as exc:
            self.log(f"remote tap setup failed: {exc}", "ERROR")
            tap_connected = False

        if not tap_connected:
            self.log("remote tap is required for turn detection", "ERROR")
            return False

        if self.mixed_recording:
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
            except Exception as exc:
                self.log(
                    f"mixed recorder setup failed (call continues): {exc}",
                    "ERROR",
                )

        self.media_graph_ready = True
        self.log("permanent media graph is ready")
        return True

    def is_waiting_for_remote(self):
        with self._lock:
            return self._waiting_for_remote and not self.disconnected

    def on_ami_ready_event(self, event: dict, how: str = "?"):
        if self._stop_evt.is_set() or self.disconnected:
            return
        detail = (
            event.get("Interaction")
            or event.get("Mode")
            or event.get("Application")
            or event.get("Command")
            or event.get("Channel")
            or "unknown"
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
            return False

        if merge_bridge_gap:
            self.log(
                f"remote turn finished after post-redirect silence ({label}) "
                f"silent_for_ms={silent_for_ms:.0f}"
            )
        else:
            self.log(
                f"remote turn finished ({label}) voiced_ms={voiced_ms} "
                f"silent_for_ms={silent_for_ms:.0f}"
            )
        return True

    def _wait_for_turn(
        self,
        timeout_secs,
        label,
        require_prompt_start,
        merge_bridge_gap,
        min_voice_ms=0,
        silence_ms=None,
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
            if self.ami_ready_events is not None:
                self.current_wait_ami_sequence = (
                    self.ami_ready_events.current_sequence()
                )

        self._ami_ready_evt.clear()
        self.remote_seen_voice = False
        self.last_voice_ts = 0.0
        self.last_frame_energy = 0.0
        self.voice_tick_count = 0

        self.log(
            f"waiting for turn ({label}) "
            f"require_prompt_start={require_prompt_start} "
            f"merge_bridge_gap={merge_bridge_gap} "
            f"min_voice_ms={min_voice_ms} silence_ms={effective_silence_ms}"
        )
        started_at = time.time()

        # Give the existing AMI monitor a short chance to bind this call to
        # its Asterisk Linkedid. If it cannot, silence detection remains safe.
        if self.ami_ready_events is not None and self.ami_ready_events.is_running():
            bind_deadline = time.time() + 2.0
            while not self._stop_evt.is_set() and time.time() < bind_deadline:
                with self._lock:
                    if self.ami_linkedid or self._transfer_thread is None:
                        break
                time.sleep(0.05)

        try:
            while not self._stop_evt.is_set():
                if self._transfer_detected:
                    return "aborted"

                ami_matched = False
                ami_is_running = (
                    self.ami_ready_events is not None
                    and self.ami_ready_events.is_running()
                )
                with self._lock:
                    linkedid = self.ami_linkedid
                    ami_sequence = self.current_wait_ami_sequence

                if ami_is_running and linkedid:
                    ami_matched = self.ami_ready_events.wait_for_event_after(
                        ami_sequence,
                        POLL_MS / 1000.0,
                        self._stop_evt,
                        linkedid=linkedid,
                    )
                else:
                    self._ami_ready_evt.wait(POLL_MS / 1000.0)
                    self._ami_ready_evt.clear()

                if self._stop_evt.is_set() or self._transfer_detected:
                    return "aborted"
                if ami_matched:
                    return "ami"

                if self._remote_turn_ready_by_silence(label):
                    return "silence"
                if timeout_secs and time.time() - started_at >= timeout_secs:
                    self.log(f"turn wait timeout ({label})", "ERROR")
                    return "timeout"
            return "aborted"
        finally:
            with self._lock:
                self._waiting_for_remote = False
                self.current_wait_requires_prompt_start = False
                self.current_wait_merge_bridge_gap = False
                self.current_wait_min_voice_ms = 0
                self.current_wait_silence_ms = SILENCE_AFTER_VOICE_MS

    def _play_wav(self, filename: str) -> bool:
        path = resolve_input_audio(filename)
        if self.tx_audio is None or not self.media_graph_ready:
            self.log(f"cannot play {filename}: outbound media is not ready", "ERROR")
            return False

        self._playback_done_evt.clear()
        try:
            self.log(f"starting playback: {path}")
            self.tx_audio.start_playback(filename)
        except Exception as exc:
            self.log(f"playback start failed for {filename}: {exc}", "ERROR")
            return False

        while not self._stop_evt.is_set():
            if self._playback_done_evt.wait(0.2):
                break

        if self._stop_evt.is_set() or self.disconnected:
            self.tx_audio.cancel_playback()
            self.log(f"local WAV aborted: {path}", "ERROR")
            return False

        self.log(f"local WAV finished: {path}")
        return True

    def _acquire_dtmf_start_slot(self) -> bool:
        global _dtmf_last_start_ts
        if DTMF_HERD_SPACING_MS <= 0:
            return True
        entered_at = time.time()
        while not self._stop_evt.is_set():
            capped = False
            with _DTMF_GATE_LOCK:
                now = time.time()
                wait_seconds = (
                    _dtmf_last_start_ts + DTMF_HERD_SPACING_MS / 1000.0 - now
                )
                if wait_seconds <= 0:
                    _dtmf_last_start_ts = now
                    return True
                if (now - entered_at) * 1000.0 >= DTMF_GATE_MAX_WAIT_MS:
                    _dtmf_last_start_ts = now
                    capped = True
            if capped:
                self.log("DTMF gate cap reached; sending now")
                return True
            if self._stop_evt.wait(min(wait_seconds, 0.2)):
                return False
        return False

    def _send_dtmf(self, digits: str) -> bool:
        if not self.media_graph_ready or self.tx_audio is None:
            self.log("DTMF not sent: RTP transmitter is unavailable", "ERROR")
            return False
        if not self._acquire_dtmf_start_slot():
            return False

        self.log(
            f"sending DTMF ({DTMF_METHOD}): {digits} "
            f"duration_ms={DTMF_DURATION_MS}"
        )
        try:
            if hasattr(pj, "CallSendDtmfParam"):
                dtmf_param = pj.CallSendDtmfParam()
                dtmf_param.method = (
                    pj.PJSUA_DTMF_METHOD_SIP_INFO
                    if DTMF_METHOD == "sip_info"
                    else pj.PJSUA_DTMF_METHOD_RFC2833
                )
                dtmf_param.duration = DTMF_DURATION_MS
                dtmf_param.digits = digits
                self.sendDtmf(dtmf_param)
            else:
                if DTMF_METHOD == "sip_info":
                    self.log("SIP INFO is unavailable in this PJSUA2 build", "ERROR")
                    return False
                self.dialDtmf(digits)
        except pj.Error as exc:
            self.log(f"DTMF send failed: {exc}", "ERROR")
            return False
        except Exception as exc:
            self.log(f"DTMF send unexpected error: {exc}", "ERROR")
            return False

        settle_ms = len(digits) * DTMF_SETTLE_MS_PER_DIGIT + DTMF_SETTLE_EXTRA_MS
        deadline = time.time() + settle_ms / 1000.0
        while not self._stop_evt.is_set() and time.time() < deadline:
            time.sleep(0.05)
        self.log("DTMF transmission window elapsed (not a delivery ACK)")
        return not self._stop_evt.is_set()

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
        try:
            self.ep.libRegisterThread(f"driver-{self.call_id}")
        except pj.Error as exc:
            self.log(f"libRegisterThread warning: {exc}", "ERROR")

        try:
            if not self._setup_media_graph():
                self.log("aborting call because media setup failed", "ERROR")
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

            for index, (action_type, action_value) in enumerate(self.actions):
                if self._stop_evt.is_set() or self._transfer_detected:
                    return

                if action_type == "wav":
                    action_ok = self._play_wav(action_value)
                elif action_type == "dtmf":
                    action_ok = self._send_dtmf(action_value)
                else:
                    self.log(f"unknown action type: {action_type}", "ERROR")
                    action_ok = False

                if self._transfer_detected:
                    return

                if not action_ok:
                    self.log(
                        f"aborting at action {index + 1}: {action_type} {action_value}",
                        "ERROR",
                    )
                    self.safe_hangup()
                    return
                if self._stop_evt.is_set():
                    return
                if index == len(self.actions) - 1:
                    self.log("action sequence complete")
                    return

                next_is_dtmf = self.actions[index + 1][0] == "dtmf"
                reason = self._wait_for_turn(
                    timeout_secs=NEXT_TURN_WAIT_TIMEOUT_SECS,
                    label=f"after-action-{index + 1}",
                    require_prompt_start=(action_type == "dtmf"),
                    merge_bridge_gap=(index == 0),
                    min_voice_ms=(DTMF_MIN_PROMPT_VOICE_MS if next_is_dtmf else 0),
                    silence_ms=(
                        DTMF_SILENCE_AFTER_VOICE_MS if next_is_dtmf else None
                    ),
                )
                if reason == "aborted":
                    return
                self.log(f"ready source={reason} (after-action-{index + 1})")
        except Exception as exc:
            self.log(f"driver loop error: {exc}", "ERROR")
