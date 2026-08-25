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


def _cpu_times():
    """The whole cpu line from /proc/stat.

    Idle used to be inferred as capacity minus the processes we happen to watch,
    which counts every untracked process on the box as idle. This box also runs
    Django, several Node services and Orbitty, so that inference read as
    headroom that was not there. Real idle comes from the kernel instead.
    """
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:9]] + [0] * 8
    user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
    return {
        "total": user + nice + system + idle + iowait + irq + softirq + steal,
        "idle": idle + iowait,
        # Time the hypervisor gave to someone else. On a shared VM this is CPU
        # the box thinks it has and does not.
        "steal": steal,
    }


def _proc_jiffies(pid):
    # /proc/pid/stat: comm (field 2) can contain spaces and parens, so index
    # from the last ')' rather than splitting the whole line.
    with open(f"/proc/{pid}/stat") as f:
        data = f.read()
    fields = data[data.rindex(")") + 2:].split()
    return int(fields[11]) + int(fields[12])          # utime + stime


def real_capacity_pct(ncpu):
    """What this box can actually use, in case a cgroup quota caps it below the
    core count. A ceiling projected against the wrong capacity is wrong."""
    for path in ("/sys/fs/cgroup/cpu.max",
                 "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
        try:
            with open(path) as f:
                text = f.read().split()
        except OSError:
            continue
        try:
            if path.endswith("cpu.max"):
                if text[0] == "max":
                    return 100.0 * ncpu
                return 100.0 * int(text[0]) / int(text[1])
            quota = int(text[0])
            if quota <= 0:
                return 100.0 * ncpu
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
                return 100.0 * quota / int(f.read().strip())
        except (ValueError, IndexError, OSError, ZeroDivisionError):
            continue
    return 100.0 * ncpu


def _short_name(cmd):
    """A readable label for a process we have no group for."""
    first = cmd.split(" ", 1)[0]
    name = os.path.basename(first) or first
    if name in ("python", "python3", "node", "sh", "bash", "perl"):
        for part in cmd.split(" ")[1:]:
            if not part.startswith("-"):
                return f"{name} {os.path.basename(part)}"[:40]
    return name[:40]


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
IGNORE = re.compile(r"asterisk\s+-rx|\brasterisk\b")

# Leading columns of the streamed CSV, ahead of the per-group ones. Named here
# so the reader can tell a fixed column from a group without counting positions.
FIXED_COLUMNS = ("rel_s", "ncpu", "busy_pct", "idle_pct", "steal_pct", "untracked_pct", "scan_ms")


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
        # What the processes in a group actually were. Asterisk is threaded, not
        # forked, so a group crediting it with hundreds of processes is counting
        # something else - and the only way to know what is to keep the command
        # lines rather than infer them from the pattern that matched.
        self.cmd_samples = {}
        # pid -> (group, short name). A command line cannot change, so reading it
        # once per process instead of once per sample is what keeps the scan
        # shorter than the interval it is sampled on.
        self._cmd_cache = {}
        # CPU spent by processes in none of the groups, kept by name so the
        # report can say what else was busy rather than calling it idle.
        self.other_totals = {}
        self.other_peak = {}
        self.capacity_pct = real_capacity_pct(self.ncpu)
        self._stream = None
        if stream_path:
            try:
                self._stream = open(stream_path, "w", encoding="utf-8", buffering=1)
                cols = (list(FIXED_COLUMNS)
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

    @property
    def other_top(self):
        """The busiest processes belonging to none of the groups, by mean CPU."""
        n = max(1, len(self.samples))
        return sorted((({"name": k, "mean_pct": round(v / n, 1),
                         "peak_pct": round(self.other_peak.get(k, 0.0), 1)})
                       for k, v in self.other_totals.items()),
                      key=lambda d: -d["mean_pct"])[:8]

    def _emit(self, sample):
        if self._stream is None:
            return
        try:
            row = ([sample["rel"], self.ncpu, sample["busy_pct"], sample["idle_pct"],
                    sample.get("steal_pct", 0), sample.get("untracked_pct", 0),
                    sample.get("scan_ms", 0)]
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
        """Every process, grouped or not.

        Processes outside the groups are kept too, so the CPU nothing accounts
        for can be named. A busy box whose named groups are quiet means we are
        watching the wrong processes, and that is worth finding out.
        """
        out = {}
        cache = self._cmd_cache
        live = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            live.add(pid)
            try:
                # A pid's command line never changes, so it is read once and
                # remembered. Reading it every half second for every process on
                # the box made the scan take 2 seconds at forty calls - longer
                # than the sampling interval, which is how a group came to report
                # more CPU than the machine has.
                known = cache.get(pid)
                if known is None:
                    cmd = _cmdline(pid)
                    if not cmd:
                        continue
                    group = self._classify(cmd)
                    known = cache[pid] = (group, _short_name(cmd))
                    if group is not None:
                        seen = self.cmd_samples.setdefault(group, {})
                        if known[1] not in seen and len(seen) < 12:
                            seen[known[1]] = cmd[:160]
                out[pid] = (known[0], _proc_jiffies(pid), known[1])
            except (OSError, ValueError, IndexError):
                continue          # process exited mid-read; normal at this rate
        if len(cache) > len(live) * 2 + 256:
            for dead in [p for p in cache if p not in live]:
                del cache[dead]           # pids are reused; do not grow forever
        return out

    def run(self):
        try:
            # /proc/stat is read AFTER the process scan, not before it.
            #
            # The scan reads a few hundred /proc/pid/stat files and takes longer
            # the busier the box is. With the total read first, the per-process
            # deltas covered a window that ran past the one they were divided by,
            # so percentages inflated exactly when the box was loaded - which is
            # how a single group came to report 580% of a 400% machine.
            prev = self._snapshot()
            prev_t = _cpu_times()
            while not self._stop.wait(self.interval):
                scan_started = time.time()
                cur = self._snapshot()
                scan_ms = round((time.time() - scan_started) * 1000, 1)
                cur_t = _cpu_times()
                dt = cur_t["total"] - prev_t["total"]
                if dt <= 0:
                    prev_t, prev = cur_t, cur
                    continue
                scale = 100.0 * self.ncpu / dt

                by_group = {}
                counts = {}
                other = {}
                for pid, (group, _jiff, _n) in cur.items():
                    if group is not None:
                        self.pids_seen.setdefault(group, set()).add(pid)
                for pid, (group, jiff, name) in cur.items():
                    if pid not in prev:
                        continue          # first sighting; no delta yet
                    pgroup, pjiff, _pn = prev[pid]
                    if pgroup != group:
                        continue
                    pct = (jiff - pjiff) * scale
                    if pct <= 0:
                        continue
                    if group is None:
                        other[name] = other.get(name, 0.0) + pct
                        continue
                    by_group[group] = by_group.get(group, 0.0) + pct
                    counts[group] = counts.get(group, 0) + 1

                for name, pct in other.items():
                    self.other_totals[name] = self.other_totals.get(name, 0.0) + pct
                    self.other_peak[name] = max(self.other_peak.get(name, 0.0), pct)

                # Real idle from the kernel, not capacity minus what we watch.
                idle = (cur_t["idle"] - prev_t["idle"]) * scale
                steal = (cur_t["steal"] - prev_t["steal"]) * scale
                system_busy = max(0.0, 100.0 * self.ncpu - idle)
                busy = sum(by_group.values())
                # CPU the box spent that none of our groups account for.
                untracked = max(0.0, system_busy - busy)
                sample = {
                    "rel": round(time.time() - self.t0, 2),
                    "groups": {k: round(v, 1) for k, v in by_group.items()},
                    "counts": counts,
                    "busy_pct": round(system_busy, 1),
                    "idle_pct": round(idle, 1),
                    "steal_pct": round(steal, 1),
                    "untracked_pct": round(untracked, 1),
                    # How long the scan itself took. If this approaches the
                    # sampling interval the numbers are being read too slowly to
                    # trust, and that should be visible rather than inferred.
                    "scan_ms": scan_ms,
                    "spawned": {g: len(v) for g, v in self.pids_seen.items()},
                }
                self.samples.append(sample)
                self._emit(sample)
                prev_t, prev = cur_t, cur
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
