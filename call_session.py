import math
import os
import struct
import threading
import time

import pjsua2 as pj

from config import (
    CALLER_USER,
    HANGUP_ON_AMI_TRANSFER,
    INITIAL_WAIT_TIMEOUT_SECS,
    NEXT_TURN_WAIT_TIMEOUT_SECS,
    POLL_MS,
    POST_REDIRECT_TOTAL_SILENCE_MS,
    SILENCE_AFTER_VOICE_MS,
    VOICE_ENERGY_THRESHOLD,
)
from pjsip_helpers import describe_media, describe_pj_error, is_active_audio_media


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
        # Players whose audio has finished but which are still connected to the
        # conference bridge. pjsua2 warns against destroying a player inside its
        # own EOF callback, so they are disconnected on the next action instead -
        # a different thread, after the callback has returned.
        self._retired_players = []

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
        else:
            self.log("transfer detected; leaving call connected because HANGUP_ON_AMI_TRANSFER=0")

    # Set RELEASE_RETIRED_PLAYERS=0 to restore the original behaviour of simply
    # dropping the reference. Kept as a switch so this change can be measured
    # against the code it replaced without a rebuild or a checkout.
    RELEASE_RETIRED = os.getenv("RELEASE_RETIRED_PLAYERS", "1").strip() != "0"

    def _release_retired_players(self):
        """Let finished players be destroyed, off the callback that finished them.

        The original code cleared self.player inside on_action_complete, which
        runs inside the player's own onEof2. That drops the last reference there
        and then, whenever the destructor runs, pjsua2 tears the player down from
        within its own EOF callback - which it warns against.

        Holding the player in a list and clearing that list on the next action
        moves the destruction onto an ordinary thread instead. Nothing here calls
        stopTransmit: the destructor already removes the conference port, and
        removing it twice aborts the process on an assertion in
        pjmedia_conf_remove_port.
        """
        with self._lock:
            retired = self._retired_players
            self._retired_players = []
        if not self.RELEASE_RETIRED:
            return                      # dropped, exactly as before this change
        # Simply going out of scope here is the point: the reference dies on this
        # thread rather than inside onEof2. The owner back-reference is left
        # alone, since MyCall.player was already cleared and clearing it would
        # break onEof2 if it fired once more.
        del retired[:]

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
        self._release_retired_players()

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
        try:
            self.start_next_action()
        except Exception as e:                              # noqa: BLE001
            # This runs on a daemon wait thread. An exception here used to kill
            # that thread without a word, leaving the call connected but never
            # speaking again - which looks like the far end going quiet rather
            # than like a bug in here. Say so loudly instead.
            import traceback
            self.log(f"*** FAILED TO START NEXT ACTION: {type(e).__name__}: {e}")
            for line in traceback.format_exc().splitlines():
                self.log(f"    {line}")

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
        INTER_DIGIT_MS = 500  # ms between digits â€” increase if still doubling

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

        # Free the previous turn's bridge slot before claiming another one.
        self._release_retired_players()
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
            # Was `self.player = None`, which dropped the reference while the
            # player was still connected to the bridge. Every other cleanup path
            # in this file calls stopTransmit; this one, the one that runs on
            # every single turn, did not.
            if self.player is not None:
                self._retired_players.append(self.player)
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
        self.log(f"*** TRANSFER via REFER to: {prm.dstUri} â€” declining and hanging up")
        prm.statusCode = 603  # Decline
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallRedirected(self, prm):
        # Fires when Asterisk sends a 3xx redirect
        target = getattr(prm, "targetUri", "<unknown>")
        self.log(f"*** TRANSFER via 3xx redirect to: {target} â€” hanging up")
        prm.opt = getattr(pj, "PJSIP_REDIRECT_STOP", 2)  # stop following redirects
        threading.Thread(target=self._hangup_after_transfer, daemon=True).start()

    def onCallReplaced(self, prm):
        # Fires when our call is replaced (attended transfer)
        self.log("*** TRANSFER via call replace â€” hanging up")
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
                return  # makeCall() never succeeded â€” no valid PJSIP slot
        except Exception:
            return
        try:
            prm = pj.CallOpParam()
            self.hangup(prm)
        except Exception as e:
            self.log(f"hangup warning: {e}")



