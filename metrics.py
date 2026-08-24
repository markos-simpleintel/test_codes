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
    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self.t0 = time.time()

    def record(self, kind, call_id=None, **fields):
        with self._lock:
            self.events.append({
                "t": time.time(),
                "rel": round(time.time() - self.t0, 3),
                "kind": kind,
                "call_id": call_id,
                **fields,
            })

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
                    }

                elif k == "action_end" and open_turn is not None:
                    open_turn["action_end"] = e["t"]

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

        return out

    @staticmethod
    def _finish_turn(t):
        def gap(a, b):
            if t.get(a) is None or t.get(b) is None:
                return None
            return round((t[b] - t[a]) * 1000, 1)

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
                "turns": 0,
            })
            k = e["kind"]
            if k == "call_start" and c["started"] is None:
                c["started"] = e["t"]
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
        spans = []
        for c in calls:
            start = c["media_ready"] or c["connected"]
            end = c["ended"]
            if start and end:
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

    def write_ndjson(self, path):
        with self._lock:
            events = sorted(self.events, key=lambda e: e["t"])
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")


# Module-level recorder so instrumented call objects can reach it without
# threading a reference through pjsua2's constructors.
RECORDER = RunMetrics()
