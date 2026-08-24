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
    "asterisk":     "#2a78d6",
    "vad_bargein":  "#eb6834",
    "pbx_receiver": "#4a3aa7",
    "test_harness": "#008300",
    "other":        "#8a8a85",
}
STACK_ORDER = ["asterisk", "vad_bargein", "pbx_receiver", "test_harness", "other"]

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

    # Collapse anything outside the named groups into "other" so the stack
    # always sums to the measured busy total.
    series = {k: [] for k in STACK_ORDER}
    for s in cpu:
        g = dict(s["groups"])
        for k in STACK_ORDER:
            if k == "other":
                known = sum(g.get(n, 0) for n in STACK_ORDER if n != "other")
                series[k].append(max(0.0, s.get("busy_pct", known) - known))
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
    ax.axhline(5000, color=INK_3, lw=1, ls=(0, (5, 4)))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="+", help="run .json file(s) from evaluate.py")
    ap.add_argument("--out", default=None, help="output directory (default: beside the json)")
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

        outdir = Path(args.out) if args.out else Path(p).parent
        outdir.mkdir(parents=True, exist_ok=True)
        stem = outdir / Path(p).stem

        for fn, suffix in ((plot_resources, "resources"), (plot_latency, "latency")):
            got = fn(run, f"{stem}.{suffix}.png")
            if got:
                written.append(got)
            else:
                print(f"  (no {suffix} data in {Path(p).name})", file=sys.stderr)

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
