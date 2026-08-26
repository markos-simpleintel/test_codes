#!/usr/bin/env python3
"""Chart a run produced by evaluate.py.

    python plot_run.py results/run1-40calls.json
    python plot_run.py results/*.json --out charts/

Two figures per run:

  <stem>.resources.png   CPU by process group over time, stacked, against the
                         box's real capacity - plus concurrent calls below, on
                         a shared clock so you can line up saturation with load.
  <stem>.latency.png     Response time per turn. This is the one that shows a
                         single slow turn hiding inside a healthy average.

Needs matplotlib. If the PBX host does not have it, copy the .json files
somewhere that does - nothing here touches the run itself.
"""

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# Validated categorical slots: these four clear the CVD, normal-vision and 3:1
# contrast checks against a light surface. Anything past them folds into "other"
# rather than inventing a fifth hue.
COLORS = {
    "untracked":    "#9aa0a6",
    "vad_bargein":  "#eb6834",
    "asterisk":     "#2a78d6",
    "pbx_receiver": "#4a3aa7",
    "test_harness": "#008300",
    "jane_app":     "#0e7c86",
    "orbitty":      "#a15c00",
    "other":        "#8a8a85",
}
# Biggest consumers nearest the axis, so the bands that matter are the ones
# whose height is easiest to read.
STACK_ORDER = ["untracked", "vad_bargein", "asterisk", "pbx_receiver",
               "test_harness", "jane_app", "orbitty"]

INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#8a8a85"
SURFACE, GRID = "#fcfcfb", "#e6e5e1"


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)


def secs(v, _pos=None):
    return f"{v:g}s"


def plot_resources(run, out):
    cpu = run.get("cpu_timeline") or []
    conc = run.get("concurrency_timeline") or []
    chan = run.get("channel_timeline") or []
    if not cpu:
        return None

    cores = run.get("cores", 1)
    capacity = 100 * cores
    t = [s["rel"] for s in cpu]

    # Every group plus the CPU none of them account for, so the stack sums to
    # what the kernel says the box was actually doing.
    series = {k: [] for k in STACK_ORDER}
    for s in cpu:
        g = dict(s["groups"])
        known = sum(g.get(n, 0) for n in STACK_ORDER if n != "untracked")
        for k in STACK_ORDER:
            if k == "untracked":
                series[k].append(max(0.0, s.get("untracked_pct",
                                               s.get("busy_pct", known) - known)))
            else:
                series[k].append(g.get(k, 0.0))

    used = [k for k in STACK_ORDER if any(v > 0.5 for v in series[k])]

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(10, 7), sharex=True,
        gridspec_kw={"height_ratios": [1.6, 1]})
    fig.patch.set_facecolor(SURFACE)

    ax.stackplot(t, *[series[k] for k in used],
                 labels=used, colors=[COLORS[k] for k in used], alpha=0.9)
    ax.axhline(capacity, color=INK_3, lw=1.2, ls=(0, (5, 4)))
    ax.annotate(f"{cores} cores = {capacity}%", (t[0], capacity),
                xytext=(4, 5), textcoords="offset points",
                color=INK_3, fontsize=9, va="bottom")
    ax.set_ylabel("CPU  (100% = one core)", color=INK_2, fontsize=9.5)
    ax.set_ylim(0, max(capacity * 1.15, max(s.get("busy_pct", 0) for s in cpu) * 1.1))
    ax.set_title(f"{run.get('label', 'run')} — {run['calls']['connected']} calls connected",
                 color=INK, fontsize=13, loc="left", pad=14)
    style(ax)
    ax.legend(frameon=False, loc="upper left", fontsize=9.5,
              labelcolor=INK_2, ncol=len(used))

    if conc:
        ax2.plot([p["rel"] for p in conc], [p["calls"] for p in conc],
                 color=COLORS["asterisk"], lw=2, label="calls in flight (measured)")
    if chan:
        ax2.plot([p["rel"] for p in chan], [p["channels"] for p in chan],
                 color=INK_3, lw=1.5, ls=(0, (4, 3)), label="asterisk channels")
    ax2.set_ylabel("concurrent calls", color=INK_2, fontsize=9.5)
    ax2.set_xlabel("time into run", color=INK_2, fontsize=9.5)
    ax2.xaxis.set_major_formatter(FuncFormatter(secs))
    ax2.set_ylim(bottom=0)
    style(ax2)
    if conc or chan:
        ax2.legend(frameon=False, loc="upper left", fontsize=9.5, labelcolor=INK_2)

    fig.tight_layout()
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return out


def cpu_breakdown(run):
    """Every consumer, in percent and in core-seconds, for one run.

    Percentages say how hard something worked; core-seconds say how much of the
    machine it actually took. A group that spikes briefly and one that idles at
    half a core all run look nothing alike in the first and are directly
    comparable in the second - which is the number that decides what is worth
    fixing.
    """
    cpu = run.get("cpu_timeline") or []
    if len(cpu) < 2:
        return
    cores = run.get("cores", 1)
    capacity = 100.0 * cores
    span = cpu[-1]["rel"] - cpu[0]["rel"]
    step = span / max(1, len(cpu) - 1)

    names = sorted({g for s in cpu for g in s["groups"]})
    rows = []
    for g in names:
        v = [s["groups"].get(g, 0.0) for s in cpu]
        rows.append((g, v))
    untracked = []
    for s in cpu:
        known = sum(s["groups"].values())
        untracked.append(max(0.0, s.get("untracked_pct",
                                        s.get("busy_pct", known) - known)))
    rows.append(("(untracked)", untracked))

    busy = [s.get("busy_pct", sum(s["groups"].values())) for s in cpu]
    total_core_s = sum(busy) * step / 100.0

    print(f"\n{'=' * 78}")
    print(f"  CPU BREAKDOWN — {run.get('label', 'run')}   "
          f"{run['calls']['connected']} calls   {cores} cores   {span:.0f}s")
    print("=" * 78)
    print(f"\n  {'consumer':<16}{'mean':>9}{'median':>9}{'peak':>9}"
          f"{'core-sec':>11}{'share':>9}")
    rows.sort(key=lambda r: -sum(r[1]))
    for g, v in rows:
        mean = sum(v) / len(v)
        core_s = sum(v) * step / 100.0
        share = core_s / total_core_s if total_core_s else 0
        print(f"  {g:<16}{mean:>8.1f}%{median(v):>8.1f}%{max(v):>8.1f}%"
              f"{core_s:>10.1f}s{share:>8.0%}")
    idle = [s.get("idle_pct", capacity - b) for s, b in zip(cpu, busy)]
    print(f"  {'':<16}{'':>9}{'':>9}{'':>9}{'':>11}")
    print(f"  {'TOTAL BUSY':<16}{sum(busy) / len(busy):>8.1f}%"
          f"{median(busy):>8.1f}%{max(busy):>8.1f}%{total_core_s:>10.1f}s{'100%':>9}")
    print(f"  {'idle':<16}{sum(idle) / len(idle):>8.1f}%{median(idle):>8.1f}%"
          f"{max(idle):>8.1f}%{sum(idle) * step / 100.0:>10.1f}s")
    print(f"\n  100% = one core. This box holds {capacity:.0f}%. "
          f"{len(cpu)} samples, {step:.2f}s apart.")

    started = (run.get("cpu") or {}).get("processes_started") or {}
    if started:
        print(f"\n  {'consumer':<16}{'processes':>11}{'core-sec each':>16}")
        for g, v in rows:
            n = started.get(g)
            if not n:
                continue
            core_s = sum(v) * step / 100.0
            print(f"  {g:<16}{n:>11}{core_s / n:>15.3f}s")
        print("     A group that starts one process per turn pays its startup cost")
        print("     every time. Core-seconds each is what one of those costs.")

    other = (run.get("cpu") or {}).get("other_processes") or []
    if other:
        print(f"\n  inside (untracked) — the busiest processes in no group:")
        print(f"  {'process':<34}{'mean':>9}{'peak':>9}")
        for o in other[:8]:
            print(f"  {o['name']:<34}{o['mean_pct']:>8.1f}%{o['peak_pct']:>8.1f}%")
    print()


def cpu_peaks(run, threshold=0.85, top=None):
    """What the box was doing at the moments it nearly ran out of CPU.

    Averages and core-seconds answer "how much of the machine did this take
    over the run", which is the wrong question for a box that falls over in
    bursts. A group with a 46% median and a 345% peak is invisible in the mean
    and is the entire problem at the moment things break.

    So this looks only at the samples where the box was near capacity, and
    reports what was running then - against what that same thing normally does,
    because a consumer that is already high and stays high is not what caused
    the spike.
    """
    cpu = run.get("cpu_timeline") or []
    if len(cpu) < 4:
        return
    cores = run.get("cores", 1)
    capacity = 100.0 * cores
    span = cpu[-1]["rel"] - cpu[0]["rel"]
    step = span / max(1, len(cpu) - 1)

    def busy_of(s):
        return s.get("busy_pct", sum(s["groups"].values()))

    def parts(s):
        """Every consumer's share of one sample, untracked included."""
        g = dict(s["groups"])
        known = sum(g.values())
        g["(untracked)"] = max(0.0, s.get("untracked_pct",
                                          busy_of(s) - known))
        return g

    busy = [busy_of(s) for s in cpu]
    limit = capacity * threshold
    hot = [i for i, b in enumerate(busy) if b >= limit]
    if top:
        hot = sorted(range(len(busy)), key=lambda i: -busy[i])[:top]
    if not hot:
        print(f"\n  No sample reached {threshold:.0%} of capacity "
              f"({limit:.0f}%). Peak was {max(busy):.0f}%.")
        return

    # Contiguous runs above the line: a spike that lasts four seconds is a
    # different problem from forty separate half-second ones.
    events, run_start, prev = [], None, None
    for i in sorted(hot):
        if prev is None or i != prev + 1:
            if run_start is not None:
                events.append((run_start, prev))
            run_start = i
        prev = i
    if run_start is not None:
        events.append((run_start, prev))

    names = sorted({k for s in cpu for k in parts(s)})
    at_peak = {n: statistics.fmean([parts(cpu[i]).get(n, 0.0) for i in hot])
               for n in names}
    typical = {n: median([parts(s).get(n, 0.0) for s in cpu]) for n in names}
    peak_total = sum(at_peak.values())

    print(f"\n{'=' * 78}")
    print(f"  CPU AT THE PEAKS — {run.get('label', 'run')}   "
          f"{run['calls']['connected']} calls   {cores} cores")
    print("=" * 78)
    print(f"\n  Samples at or above {threshold:.0%} of the box ({limit:.0f}%): "
          f"{len(hot)} of {len(cpu)}  ({len(hot) / len(cpu):.0%} of the run)")
    print(f"  Spike events: {len(events)}   "
          f"median {statistics.median([(b - a + 1) * step for a, b in events]):.1f}s   "
          f"longest {max((b - a + 1) * step for a, b in events):.1f}s")
    print(f"  Highest single sample: {max(busy):.0f}% of {capacity:.0f}%")

    print(f"\n  {'consumer':<16}{'at peak':>10}{'typical':>10}{'rise':>10}"
          f"{'share of peak':>16}")
    for n in sorted(names, key=lambda k: -at_peak[k]):
        if at_peak[n] < 0.5 and typical[n] < 0.5:
            continue
        rise = at_peak[n] - typical[n]
        share = at_peak[n] / peak_total if peak_total else 0
        print(f"  {n:<16}{at_peak[n]:>9.1f}%{typical[n]:>9.1f}%"
              f"{rise:>+9.1f}%{share:>15.0%}")
    print(f"  {'-' * 62}")
    print(f"  {'TOTAL':<16}{peak_total:>9.1f}%{sum(typical.values()):>9.1f}%"
          f"{peak_total - sum(typical.values()):>+9.1f}%{'100%':>16}")

    print(f"\n  'rise' is what each consumer adds ON TOP of its normal level when")
    print(f"  the box is at its busiest. That is the column that names the cause:")
    print(f"  something already high that stays high did not create the spike.")

    worst = max(names, key=lambda n: at_peak[n] - typical[n])
    print(f"\n  Largest rise: {worst}  "
          f"{typical[worst]:.0f}% -> {at_peak[worst]:.0f}%  "
          f"(+{at_peak[worst] - typical[worst]:.0f}% of a core)")

    forked = (run.get("cpu") or {}).get("forked_mean_by_group") or {}
    if forked:
        print(f"\n  Of that, CPU from short-lived children, by who spawned them:")
        for g, v in sorted(forked.items(), key=lambda kv: -kv[1]):
            if v > 0.5:
                print(f"    {g:<16}{v:>8.1f}%  mean across the run")

    other = (run.get("cpu") or {}).get("other_processes") or []
    if other:
        print(f"\n  Named processes inside (untracked):")
        print(f"  {'process':<34}{'mean':>9}{'peak':>9}")
        for o in other[:8]:
            print(f"  {o['name']:<34}{o['mean_pct']:>8.1f}%{o['peak_pct']:>8.1f}%")
        named = sum(o["mean_pct"] for o in other)
        unnamed = typical.get("(untracked)", 0) - named
        if unnamed > 1:
            print(f"\n    Those named account for {named:.1f}% of the untracked band.")
            print(f"    The rest lives too briefly to be caught alive - see the")
            print(f"    forked figures above for where it came from.")
    print()

def median(v):
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def plot_latency(run, out):
    by_turn = (run.get("latency_ms") or {}).get("by_turn") or {}
    if not by_turn:
        return None

    turns = sorted(by_turn, key=lambda k: int(k))
    p50 = [by_turn[k]["p50"] for k in turns]
    p95 = [by_turn[k]["p95"] for k in turns]
    n = [by_turn[k]["count"] for k in turns]
    x = range(len(turns))

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor(SURFACE)

    # The bar is the typical caller's wait; the marker is the unlucky one in
    # twenty. Drawing both stops a bad tail hiding behind a healthy median.
    ax.bar(x, p50, color=COLORS["asterisk"], width=0.62, label="median")
    ax.scatter(x, p95, color=COLORS["vad_bargein"], s=42, zorder=4,
               label="95th percentile")
    for i, (a, b) in enumerate(zip(p50, p95)):
        ax.plot([i, i], [a, b], color=COLORS["vad_bargein"], lw=1.5, zorder=3)

    budget = 5000
    ax.axhline(budget, color=INK_3, lw=1, ls=(0, (5, 4)))
    ax.annotate("5s — a caller notices this much silence", (-0.4, budget),
                xytext=(0, 5), textcoords="offset points",
                color=INK_3, fontsize=9, va="bottom")

    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{k}\nn={c}" for k, c in zip(turns, n)])
    ax.set_ylabel("response time", color=INK_2, fontsize=9.5)
    ax.set_xlabel("turn in the conversation", color=INK_2, fontsize=9.5)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v/1000:g}s"))
    ax.set_title(f"{run.get('label', 'run')} — wait after the caller stops speaking",
                 color=INK, fontsize=13, loc="left", pad=14)
    style(ax)
    ax.legend(frameon=False, loc="upper left", fontsize=9.5, labelcolor=INK_2)

    fig.tight_layout()
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return out


def plot_ladder(runs, out):
    """Response time and peak CPU against concurrency, when several levels were
    run. One point per level."""
    pts = []
    for r in runs:
        conc = r["calls"]["peak_concurrent_measured"] or r["calls"]["connected"]
        resp = (r.get("latency_ms") or {}).get("response") or {}
        if not conc or not resp.get("count"):
            continue
        pts.append((conc, resp["p50"], resp["p95"],
                    max((s.get("busy_pct", 0) for s in r.get("cpu_timeline") or []), default=0),
                    r.get("cores", 1)))
    if len(pts) < 2:
        return None
    pts.sort()

    x = [p[0] for p in pts]
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True,
                                  gridspec_kw={"height_ratios": [1.3, 1]})
    fig.patch.set_facecolor(SURFACE)

    ax.plot(x, [p[2] for p in pts], color=COLORS["vad_bargein"], lw=2,
            marker="o", ms=5, label="95th percentile")
    ax.plot(x, [p[1] for p in pts], color=COLORS["asterisk"], lw=2,
            marker="o", ms=5, label="median")
    slo = next((r.get("slo_ms") for r in runs if r.get("slo_ms")), None)
    if slo:
        ax.axhline(slo, color=INK_3, lw=1, ls=(0, (5, 4)))
        # Right-aligned: the legend sits top-left, and the two collide there.
        ax.annotate(f"too slow above {slo / 1000:g}s", xy=(1, slo),
                    xycoords=("axes fraction", "data"), xytext=(-4, 5),
                    textcoords="offset points", color=INK_3, fontsize=9,
                    ha="right", va="bottom")
    ax.set_ylabel("response time", color=INK_2, fontsize=9.5)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v/1000:g}s"))
    ax.set_title("Response time and CPU vs concurrent calls", color=INK,
                 fontsize=13, loc="left", pad=14)
    style(ax)
    ax.legend(frameon=False, loc="upper left", fontsize=9.5, labelcolor=INK_2)

    capacity = 100 * pts[0][4]
    ax2.plot(x, [p[3] for p in pts], color=COLORS["pbx_receiver"], lw=2,
             marker="s", ms=5, label="total CPU in use")
    ax2.axhline(capacity, color=INK_3, lw=1.2, ls=(0, (5, 4)))
    ax2.annotate(f"box capacity {capacity}%", (x[0], capacity), xytext=(4, 5),
                 textcoords="offset points", color=INK_3, fontsize=9, va="bottom")
    ax2.set_ylabel("CPU", color=INK_2, fontsize=9.5)
    ax2.set_xlabel("concurrent calls", color=INK_2, fontsize=9.5)
    ax2.set_xticks(x)
    style(ax2)
    ax2.legend(frameon=False, loc="upper left", fontsize=9.5, labelcolor=INK_2)

    fig.tight_layout()
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return out


def plot_degradation(runs, out):
    """Every answered turn as one point: what it waited, against the load that
    applied to it.

    A rung of a ladder is one average at one call count. This is every
    observation at every load the run passed through, which is where the shape
    of the degradation actually shows - whether it is a straight line, a knee,
    or a cliff.
    """
    pts = []
    for r in runs:
        for t in r.get("turns") or []:
            if t.get("response_ms") is not None and t.get("inflight") is not None:
                pts.append((t["inflight"], t["response_ms"]))
    if len(pts) < 10:
        return None

    fig, ax = plt.subplots(figsize=(9, 5.2))
    fig.patch.set_facecolor(SURFACE)
    ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=14, alpha=0.35,
               color=COLORS["asterisk"], edgecolors="none", label="one answered turn")

    # Median wait in each load bucket - the trend, without the scatter's noise.
    buckets = {}
    for x, y in pts:
        buckets.setdefault(x, []).append(y)
    xs = sorted(b for b, v in buckets.items() if len(v) >= 3)
    if xs:
        meds = [sorted(buckets[b])[len(buckets[b]) // 2] for b in xs]
        ax.plot(xs, meds, color=COLORS["vad_bargein"], lw=2.2, marker="o", ms=4,
                label="median at that load")

    slo = next((r.get("slo_ms") for r in runs if r.get("slo_ms")), None)
    if slo:
        ax.axhline(slo, color=INK_3, lw=1.2, ls=(0, (5, 4)))
        # Right-aligned: the legend sits top-left, and the two collide there.
        ax.annotate(f"too slow above {slo / 1000:g}s", xy=(1, slo),
                    xycoords=("axes fraction", "data"), xytext=(-4, 5),
                    textcoords="offset points", color=INK_3, fontsize=9,
                    ha="right", va="bottom")

    ax.set_xlabel("requests in flight at that moment", color=INK_2, fontsize=9.5)
    ax.set_ylabel("caller wait", color=INK_2, fontsize=9.5)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v/1000:g}s"))
    ax.set_title("What each caller waited, against the load at that moment",
                 color=INK, fontsize=13, loc="left", pad=14)
    style(ax)
    ax.legend(frameon=False, loc="upper left", fontsize=9.5, labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(out, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="+", help="run .json file(s) from evaluate.py")
    ap.add_argument("--out", default=None, help="output directory (default: beside the json)")
    ap.add_argument("--peak-threshold", type=float, default=0.85,
                    help="a sample counts as a peak at this fraction of the box "
                         "(default 0.85, so 340%% of 400%%)")
    ap.add_argument("--no-breakdown", action="store_true",
                    help="skip the printed CPU table, just write the charts")
    args = ap.parse_args()

    paths = []
    for pat in args.json:
        paths.extend(sorted(glob.glob(pat)) or [pat])

    runs = []
    written = []
    for p in paths:
        try:
            run = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:                              # noqa: BLE001
            print(f"skip {p}: {e}", file=sys.stderr)
            continue
        if "cpu_timeline" not in run:
            print(f"skip {p}: not an evaluate.py run", file=sys.stderr)
            continue
        runs.append(run)
        if not args.no_breakdown:
            cpu_breakdown(run)
            cpu_peaks(run, threshold=args.peak_threshold)

        outdir = Path(args.out) if args.out else Path(p).parent
        outdir.mkdir(parents=True, exist_ok=True)
        stem = outdir / Path(p).stem

        for fn, suffix in ((plot_resources, "resources"), (plot_latency, "latency")):
            got = fn(run, f"{stem}.{suffix}.png")
            if got:
                written.append(got)
            else:
                print(f"  (no {suffix} data in {Path(p).name})", file=sys.stderr)

    if runs:
        outdir = Path(args.out) if args.out else Path(paths[0]).parent
        got = plot_degradation(runs, outdir / "degradation.png")
        if got:
            written.append(got)
    if len(runs) > 1:
        outdir = Path(args.out) if args.out else Path(paths[0]).parent
        got = plot_ladder(runs, outdir / "ladder.png")
        if got:
            written.append(got)

    for w in written:
        print(f"wrote {w}")
    if not written:
        sys.exit("nothing written")


if __name__ == "__main__":
    main()
