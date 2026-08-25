"""Per-turn timing for a load run.

Records what happened and when, so a run can be described in percentiles rather
than in prose. Nothing here knows about pjsua2 - callers hand it events and it
turns them into turn records at the end.

A "turn" is one exchange: we finish playing our audio, the far end starts
talking, the far end stops. The interesting number is the first gap - how long
the caller waits in silence after speaking - because that is the one a person
actually experiences.
"""

import json
import threading
import time


class RunMetrics:
    def __init__(self, stream_path=None):
        self._lock = threading.Lock()
        self.events = []
        self.t0 = time.time()
        # Events go to disk as they happen. Held only in memory, an ugly exit -
        # a kill during a slow teardown, say - loses the whole run's timings,
        # and the text log carries no timestamps to rebuild them from.
        self._stream = None
        if stream_path:
            try:
                self._stream = open(stream_path, "w", encoding="utf-8", buffering=1)
            except OSError:
                self._stream = None

    def record(self, kind, call_id=None, **fields):
        event = {
            "t": time.time(),
            "rel": round(time.time() - self.t0, 3),
            "kind": kind,
            "call_id": call_id,
            **fields,
        }
        with self._lock:
            self.events.append(event)
            if self._stream is not None:
                try:
                    self._stream.write(json.dumps(event) + "\n")
                except (OSError, ValueError):
                    self._stream = None

    def close_stream(self):
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.flush()
                    self._stream.close()
                except (OSError, ValueError):
                    pass
                self._stream = None

    # ---- derived views -------------------------------------------------

    def turns(self):
        """One record per turn, in call order.

        Built by walking each call's events in sequence: an action_end starts
        the clock, remote_first_voice stops the response-latency clock, and
        remote_turn_end closes the turn.
        """
        by_call = {}
        with self._lock:
            events = sorted(self.events, key=lambda e: e["t"])

        for e in events:
            cid = e.get("call_id")
            if cid is None:
                continue
            by_call.setdefault(cid, []).append(e)

        out = []
        for cid, evs in sorted(by_call.items()):
            open_turn = None
            for e in evs:
                k = e["kind"]

                if k == "action_start":
                    open_turn = {
                        "call_id": cid,
                        "turn": e.get("turn"),
                        "action_type": e.get("action_type"),
                        "action": e.get("action"),
                        "action_start": e["t"],
                        "action_end": None,
                        "first_voice": None,
                        "turn_end": None,
                        "detected_by": None,
                        # RTP counters, before and after our audio played.
                        "tx_pkt_start": e.get("tx_pkt"),
                        "tx_pkt_end": None,
                        "tx_loss_start": e.get("tx_loss"),
                        "tx_loss_end": None,
                    }

                elif k == "action_end" and open_turn is not None:
                    open_turn["action_end"] = e["t"]
                    open_turn["tx_pkt_end"] = e.get("tx_pkt")
                    open_turn["tx_loss_end"] = e.get("tx_loss")

                elif k == "remote_first_voice" and open_turn is not None:
                    if open_turn["first_voice"] is None:
                        open_turn["first_voice"] = e["t"]

                elif k == "remote_turn_end" and open_turn is not None:
                    open_turn["turn_end"] = e["t"]
                    open_turn["detected_by"] = e.get("source")
                    out.append(self._finish_turn(open_turn))
                    open_turn = None

            if open_turn is not None:
                out.append(self._finish_turn(open_turn))

        # How long WE took to start speaking after the PBX finished.
        #
        # Every other latency here measures the PBX answering us. This one is
        # the other direction, and it is the one that decides whether a
        # recording contains anything: vad_bargein starts listening the moment
        # its prompt ends and gives up after NO_SPEECH_TIMEOUT_MS. Take longer
        # than that to reply and it records silence, times out, and sends the
        # silence on - no matter that we were about to speak.
        by_call = {}
        for t in out:
            by_call.setdefault(t["call_id"], []).append(t)
        for turns_of_call in by_call.values():
            turns_of_call.sort(key=lambda t: t["action_start"] or 0)
            prev_end = None
            for t in turns_of_call:
                start = t.get("action_start")
                if prev_end is not None and start is not None and start >= prev_end:
                    t["reply_delay_ms"] = round((start - prev_end) * 1000, 1)
                else:
                    t["reply_delay_ms"] = None
                prev_end = t.get("turn_end") or prev_end
        return out

    @staticmethod
    def _finish_turn(t):
        def gap(a, b):
            if t.get(a) is None or t.get(b) is None:
                return None
            return round((t[b] - t[a]) * 1000, 1)

        # How much audio we actually put on the wire for this turn. RTP is one
        # packet per 20ms, so packets/50 is seconds transmitted - directly
        # comparable to the length of the file we meant to play and to what the
        # PBX recorded. Without it, "the recording was silent" cannot be told
        # apart from "we never sent anything".
        if t.get("tx_pkt_start") is not None and t.get("tx_pkt_end") is not None:
            sent = t["tx_pkt_end"] - t["tx_pkt_start"]
            t["tx_packets"] = sent if sent >= 0 else None
            t["tx_seconds"] = round(sent / 50.0, 2) if sent >= 0 else None
        else:
            t["tx_packets"] = None
            t["tx_seconds"] = None

        # How many of the packets we sent for this turn Asterisk told us, over
        # RTCP, that it never received. This is the one number that can see the
        # gap between our socket and the VAD's input.
        if t.get("tx_loss_start") is not None and t.get("tx_loss_end") is not None:
            lost = t["tx_loss_end"] - t["tx_loss_start"]
            t["tx_lost"] = lost if lost >= 0 else None
            sent = t.get("tx_packets") or 0
            t["tx_loss_pct"] = (round(100.0 * lost / (sent + lost), 1)
                                if (sent + lost) > 0 and lost >= 0 else None)
        else:
            t["tx_lost"] = None
            t["tx_loss_pct"] = None

        # How long the player actually ran, start to EOF.
        #
        # This is the only measure here that cannot be fooled by the keepalive.
        # RTP packet counts never fall to zero because a silence file streams
        # between turns, so "we transmitted" was true even when the recording
        # never played. A player that reports finishing in a fraction of its
        # file's length did not play it.
        t["playback_ms"] = gap("action_start", "action_end")

        # Response latency: our audio stopped, how long until theirs started.
        t["response_ms"] = gap("action_end", "first_voice")
        # How long the far end then spoke for.
        t["remote_speech_ms"] = gap("first_voice", "turn_end")
        # The whole turn, our audio included.
        t["turn_total_ms"] = gap("action_start", "turn_end")
        for k in ("action_start", "action_end", "first_voice", "turn_end"):
            if t.get(k) is not None:
                t[k] = round(t[k], 3)
        return t

    def calls(self):
        """One record per call: when it connected, ended, and why."""
        by_call = {}
        with self._lock:
            events = sorted(self.events, key=lambda e: e["t"])

        for e in events:
            cid = e.get("call_id")
            if cid is None:
                continue
            c = by_call.setdefault(cid, {
                "call_id": cid, "started": None, "connected": None,
                "media_ready": None, "ended": None, "end_reason": None,
                "turns": 0, "caller": None, "identity": None,
                "channel": None, "uniqueid": None, "linkedid": None,
                "session": None, "session_prefix": None,
            })
            k = e["kind"]
            if k == "call_session_id":
                c["session"] = e.get("session") or c["session"]
                c["session_prefix"] = e.get("session_prefix") or c["session_prefix"]
            if k == "call_identity":
                c["channel"] = e.get("channel") or c["channel"]
                c["uniqueid"] = e.get("uniqueid") or c["uniqueid"]
                c["linkedid"] = e.get("linkedid") or c["linkedid"]
            if k == "call_start" and c["started"] is None:
                c["started"] = e["t"]
                c["caller"] = e.get("caller")
                c["identity"] = e.get("identity")
            elif k == "call_connected" and c["connected"] is None:
                c["connected"] = e["t"]
            elif k == "call_media_ready" and c["media_ready"] is None:
                c["media_ready"] = e["t"]
            elif k == "call_end":
                c["ended"] = e["t"]
                c["end_reason"] = e.get("reason")
            elif k == "remote_turn_end":
                c["turns"] += 1

        for c in by_call.values():
            if c["connected"] and c["ended"]:
                c["duration_s"] = round(c["ended"] - c["connected"], 2)
            else:
                c["duration_s"] = None
        return [by_call[k] for k in sorted(by_call)]

    def concurrency_timeline(self, step=0.5):
        """How many calls were connected at each instant, measured rather than
        assumed. A call counts from media-ready (or connect) until it ends."""
        calls = self.calls()
        # A killed run has no call_end for calls that were still going, so
        # treat those as running until the last thing we saw. Requiring an end
        # event reported a peak of 1 on a 40-call run.
        last_seen = max((e["t"] for e in self.events), default=None)
        spans = []
        for c in calls:
            start = c["media_ready"] or c["connected"]
            end = c["ended"] or last_seen
            if start and end and end >= start:
                spans.append((start, end))
        if not spans:
            return []

        lo = min(s for s, _ in spans)
        hi = max(e for _, e in spans)
        out = []
        t = lo
        while t <= hi:
            n = sum(1 for s, e in spans if s <= t <= e)
            out.append({"rel": round(t - self.t0, 2), "calls": n})
            t += step
        return out

    # ---- load, as opposed to call count ---------------------------------
    #
    # "40 concurrent calls" is not 40 units of work. A call sitting in silence
    # while the caller thinks costs a channel and a VAD process; a call that has
    # just stopped speaking costs ASR, an LLM turn and TTS. Only the second kind
    # loads the pipeline, and it is the one that decides where the system tops
    # out - so it is measured separately rather than inferred from call count.

    def _call_spans(self):
        """(start, end) for every call that got as far as media."""
        last_seen = max((e["t"] for e in self.events), default=None)
        spans = []
        for c in self.calls():
            start = c["media_ready"] or c["connected"]
            end = c["ended"] or last_seen
            if start and end and end >= start:
                spans.append((start, end))
        return spans

    def inflight_spans(self, turns=None):
        """(start, end) for every window where a call was waiting on the system.

        Opens when our audio stops and closes when the far end starts speaking:
        exactly the interval during which the request is somewhere in the
        pipeline, and exactly what a caller experiences as silence.
        """
        last_seen = max((e["t"] for e in self.events), default=None)
        spans = []
        for t in (self.turns() if turns is None else turns):
            start = t.get("action_end")
            if start is None:
                continue
            end = t.get("first_voice") or t.get("turn_end") or last_seen
            if end and end >= start:
                spans.append((start, end))
        return spans

    @staticmethod
    def _count_at(spans, instant):
        return sum(1 for s, e in spans if s <= instant <= e)

    def inflight_timeline(self, step=0.5, turns=None):
        spans = self.inflight_spans(turns)
        if not spans:
            return []
        lo = min(s for s, _ in spans)
        hi = max(e for _, e in spans)
        out, t = [], lo
        while t <= hi:
            out.append({"rel": round(t - self.t0, 2),
                        "inflight": self._count_at(spans, t)})
            t += step
        return out

    def annotate(self, turns=None):
        """Tag each turn with the load in effect when it started waiting.

        This is what turns a run into a curve. Every turn becomes one
        (load, latency) observation, so response time can be plotted against
        the concurrency that actually applied to it rather than against the
        call count the run was launched with - which is only true for an
        instant at the very start.
        """
        turns = self.turns() if turns is None else turns
        call_spans = self._call_spans()
        flight_spans = self.inflight_spans(turns)
        for t in turns:
            at = t.get("action_end")
            if at is None:
                t["calls_up"] = None
                t["inflight"] = None
                continue
            t["calls_up"] = self._count_at(call_spans, at)
            # Counting the instant our audio stops would include this turn's own
            # request, which has not been submitted yet; a hair earlier does not.
            t["inflight"] = self._count_at(flight_spans, at - 0.001)
        return turns

    # ---- did it work -----------------------------------------------------

    def outcomes(self, turns=None):
        """One verdict per call, in categories that say what actually failed.

        A capacity test needs a definition of failure that is not "the number
        looked high". These are structural: either the call connected or it did
        not, either the far end answered a turn or it did not.
        """
        turns = self.annotate() if turns is None else turns
        by_call = {}
        for t in turns:
            by_call.setdefault(t["call_id"], []).append(t)

        out = []
        for c in self.calls():
            ts = by_call.get(c["call_id"], [])
            answered = [t for t in ts if t.get("response_ms") is not None]
            # A turn where our audio finished and nothing ever came back.
            unanswered = [t for t in ts
                          if t.get("action_end") is not None
                          and t.get("first_voice") is None]
            if not c["connected"]:
                verdict = "never_connected"
            elif not (c["media_ready"] or answered):
                verdict = "no_media"
            elif not answered:
                verdict = "no_response_at_all"
            elif unanswered:
                verdict = "abandoned_mid_call"
            else:
                verdict = "completed"
            out.append({
                "call_id": c["call_id"],
                "verdict": verdict,
                "turns_answered": len(answered),
                "turns_unanswered": len(unanswered),
                "duration_s": c["duration_s"],
                # Carried so a degraded call in the report can be found again in
                # Jane and in the PBX logs. Asterisk's uniqueid is <epoch>.<seq>,
                # and the PBX names its session directory <caller>_<epoch><rand>,
                # so the uniqueid locates the recordings for that exact call.
                "caller": c.get("caller"),
                "identity": c.get("identity"),
                "session": c.get("session"),
                "session_prefix": c.get("session_prefix"),
                "channel": c.get("channel"),
                "uniqueid": c.get("uniqueid"),
                "linkedid": c.get("linkedid"),
                "started": c.get("started"),
                "last_turn": max((t.get("turn") or 0 for t in ts), default=None),
            })
        return out

    def write_ndjson(self, path):
        """Rewrite the trace in time order. The streamed copy is in arrival
        order, which is close but not guaranteed across threads."""
        self.close_stream()
        with self._lock:
            events = sorted(self.events, key=lambda e: e["t"])
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")


# Module-level recorder so instrumented call objects can reach it without
# threading a reference through pjsua2's constructors.
RECORDER = RunMetrics()
