"""Resource sampling during a load run.

CPU comes from /proc rather than by parsing `top`. Two reasons: `ps` reports
average CPU since process start, which is useless for a short test against a
long-running daemon, and `top`'s column layout shifts between versions. Reading
jiffie counters and differencing them gives the same number top would show,
computed here, per process group.
"""

import os
import re
import subprocess
import threading
import time


def _total_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    return sum(int(x) for x in parts[1:8])


def _proc_jiffies(pid):
    # /proc/pid/stat: comm (field 2) can contain spaces and parens, so index
    # from the last ')' rather than splitting the whole line.
    with open(f"/proc/{pid}/stat") as f:
        data = f.read()
    fields = data[data.rindex(")") + 2:].split()
    return int(fields[11]) + int(fields[12])          # utime + stime


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


# Which processes matter, and what to call them in the report. First match wins,
# so the specific patterns must precede the general ones.
DEFAULT_GROUPS = [
    ("vad_bargein",  re.compile(r"vad_bargein\.py")),
    ("pbx_receiver", re.compile(r"pbx_receiver\.py")),
    ("test_harness", re.compile(r"runner\.py|evaluate\.py")),
    ("asterisk",     re.compile(r"/usr/sbin/asterisk|\basterisk\b -")),
    ("jane_app",     re.compile(r"janev|scheduling|uvicorn|manage\.py")),
    ("orbitty",      re.compile(r"RoutingAI/orbitty")),
]

# The channel sampler shells out to `asterisk -rx` every couple of seconds, and
# that matches the asterisk pattern above - so the measuring instrument was
# being counted as part of the thing measured, inflating both asterisk's CPU
# and its process count. Anything matching here belongs to no group.
IGNORE = re.compile(r"asterisk\s+-rx")


class CpuSampler(threading.Thread):
    """Samples per-process CPU into groups. Percentages are per-core, matching
    top: 100 means one core fully used."""

    def __init__(self, interval=0.5, groups=None, stream_path=None, t0=None):
        super().__init__(daemon=True)
        self.interval = interval
        self.groups = groups or DEFAULT_GROUPS
        self.samples = []                 # [{rel, idle_pct, groups:{name: pct}, procs: n}]
        self.ncpu = os.cpu_count() or 1
        self._stop = threading.Event()
        self.t0 = t0 or time.time()
        self.error = None
        # Samples go to disk as they are taken. Held only in memory they are
        # lost whenever a run has to be killed - and a run that had to be killed
        # is exactly the one whose resource numbers you still want.
        self._names = [n for n, _ in self.groups]
        # Every distinct pid ever seen in a group. EAGI starts a fresh Python
        # process per turn, so a group's cost can be process churn rather than
        # work the processes do - a distinction the percentages hide, and the
        # one that decides whether tuning the code or reusing the process is
        # what would help.
        self.pids_seen = {n: set() for n in self._names}
        self._stream = None
        if stream_path:
            try:
                self._stream = open(stream_path, "w", encoding="utf-8", buffering=1)
                cols = (["rel_s", "ncpu", "busy_pct", "idle_pct"]
                        + self._names
                        + ["n_" + n for n in self._names]
                        + ["spawned_" + n for n in self._names])
                self._stream.write(",".join(cols) + "\n")
            except OSError:
                self._stream = None

    @property
    def spawn_counts(self):
        """How many processes each group started over the whole run."""
        return {n: len(v) for n, v in self.pids_seen.items() if v}

    def _emit(self, sample):
        if self._stream is None:
            return
        try:
            row = ([sample["rel"], self.ncpu, sample["busy_pct"], sample["idle_pct"]]
                   + [sample["groups"].get(n, 0) for n in self._names]
                   + [sample["counts"].get(n, 0) for n in self._names]
                   + [sample["spawned"].get(n, 0) for n in self._names])
            self._stream.write(",".join(str(x) for x in row) + "\n")
        except (OSError, ValueError):
            self._stream = None

    def close_stream(self):
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except (OSError, ValueError):
                pass
            self._stream = None

    def stop(self):
        self._stop.set()

    def _classify(self, cmd):
        if IGNORE.search(cmd):
            return None
        for name, pattern in self.groups:
            if pattern.search(cmd):
                return name
        return None

    def _snapshot(self):
        out = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                cmd = _cmdline(pid)
                if not cmd:
                    continue
                group = self._classify(cmd)
                if group is None:
                    continue
                out[pid] = (group, _proc_jiffies(pid))
            except (OSError, ValueError, IndexError):
                continue          # process exited mid-read; normal at this rate
        return out

    def run(self):
        try:
            prev_total = _total_jiffies()
            prev = self._snapshot()
            while not self._stop.wait(self.interval):
                total = _total_jiffies()
                cur = self._snapshot()
                dt = total - prev_total
                if dt <= 0:
                    prev_total, prev = total, cur
                    continue

                by_group = {}
                counts = {}
                for pid, (group, _jiff) in cur.items():
                    self.pids_seen.setdefault(group, set()).add(pid)
                for pid, (group, jiff) in cur.items():
                    if pid not in prev:
                        continue          # first sighting; no delta yet
                    pgroup, pjiff = prev[pid]
                    if pgroup != group:
                        continue
                    pct = 100.0 * (jiff - pjiff) * self.ncpu / dt
                    if pct < 0:
                        continue
                    by_group[group] = by_group.get(group, 0.0) + pct
                    counts[group] = counts.get(group, 0) + 1

                busy = sum(by_group.values())
                sample = {
                    "rel": round(time.time() - self.t0, 2),
                    "groups": {k: round(v, 1) for k, v in by_group.items()},
                    "counts": counts,
                    "busy_pct": round(busy, 1),
                    "idle_pct": round(max(0.0, 100.0 * self.ncpu - busy), 1),
                    "spawned": {g: len(v) for g, v in self.pids_seen.items()},
                }
                self.samples.append(sample)
                self._emit(sample)
                prev_total, prev = total, cur
        except Exception as e:                      # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"


class ChannelSampler(threading.Thread):
    """Asterisk's own count of active channels - the independent check on how
    many calls were really up, rather than how many we asked for."""

    def __init__(self, interval=2.0, stream_path=None, t0=None):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []                 # [{rel, channels}]
        self._stop = threading.Event()
        self.t0 = t0 or time.time()
        self.available = True
        self._stream = None
        if stream_path:
            try:
                self._stream = open(stream_path, "w", encoding="utf-8", buffering=1)
                self._stream.write("rel_s,channels\n")
            except OSError:
                self._stream = None

    def close_stream(self):
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except (OSError, ValueError):
                pass
            self._stream = None

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                out = subprocess.run(
                    ["asterisk", "-rx", "core show channels count"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                m = re.search(r"(\d+)\s+active channel", out)
                if m:
                    sample = {
                        "rel": round(time.time() - self.t0, 2),
                        "channels": int(m.group(1)),
                    }
                    self.samples.append(sample)
                    if self._stream is not None:
                        try:
                            self._stream.write(f"{sample['rel']},{sample['channels']}\n")
                        except (OSError, ValueError):
                            self._stream = None
            except (OSError, subprocess.SubprocessError):
                self.available = False
                return
