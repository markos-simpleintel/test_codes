#!/usr/bin/env python3
"""Score what the system heard against what the test actually said.

    python check_transcripts.py transcripts.csv
    python check_transcripts.py transcripts.csv --run results/run5-40calls

Every WAV the harness plays has a known, fixed utterance, so accuracy is
measurable rather than a matter of reading logs and forming an impression.
Export the transcripts as CSV with a `text` column, plus whatever identifies
the turn (`session`/`call_id` and `turn`, or just rows in order), and this
reports word error rate per turn and flags hallucinations.

Hallucinations have a signature worth naming separately from ordinary errors:
a recogniser fed silence or noise does not return nothing, it returns fluent
text that was never said. Those are scored as total misses and listed, because
an averaged error rate hides them.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

# What each recording actually says, read from input_audios/manifest.json - the
# same file place_call.py labels a script from, so a new recording is described
# once rather than here as well. The dict below is the fallback for a checkout
# with no manifest.
FALLBACK_EXPECTED = {
    "name2.wav":      "my name is marcos test",
    "birthday2.wav":  "january 1 1993",
    "yes.wav":        "yes",
    "no.wav":         "no",
    "height.wav":     None,      # fill in once confirmed
    "weight.wav":     None,
}


def load_expected(audio_dir=None):
    try:
        import callscript
        directory = audio_dir or (Path(__file__).resolve().parent / "input_audios")
        manifest = callscript.load_manifest(directory)
    except Exception:
        manifest = {}
    return manifest or dict(FALLBACK_EXPECTED)


EXPECTED = load_expected()

# Text a recogniser emits when handed silence rather than speech. These are the
# well-known Whisper filler phrases; anything matching is near-certainly not
# something the test said.
HALLUCINATION_MARKERS = [
    re.compile(r"thank(s| you) for watching", re.I),
    re.compile(r"subscribe|like and share|see you (next|in the next)", re.I),
    re.compile(r"^\W*(you|bye|thank you|thanks)\W*$", re.I),
    re.compile(r"amara\.org|transcri(bed|ption) by", re.I),
    re.compile(r"(\b\w+\b)(\s+\1){3,}", re.I),      # same word 4+ times
]


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def wer(expected, heard):
    """Word error rate by Levenshtein distance over words."""
    e, h = normalize(expected).split(), normalize(heard).split()
    if not e:
        return None
    prev = list(range(len(h) + 1))
    for i, ew in enumerate(e, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ew != hw)))
        prev = cur
    return prev[-1] / len(e)


def looks_hallucinated(expected, heard):
    if not heard or not heard.strip():
        return False, ""
    for pattern in HALLUCINATION_MARKERS:
        if pattern.search(heard):
            return True, f"matches a known silence artefact: {pattern.pattern[:40]}"
    # Fluent text sharing almost no words with what was said is the other shape
    # a hallucination takes - invented content rather than a misheard word.
    e, h = set(normalize(expected).split()), normalize(heard).split()
    if expected and len(h) >= 4 and e and not (e & set(h)):
        return True, "no words in common with what was said"
    return False, ""


def wav_for(action):
    if not action:
        return None
    return Path(str(action).replace("\\", "/")).name


def load_expected_from_run(run_stem):
    """Map (call_id, turn) -> the wav that was played, from a run's turns.csv."""
    path = Path(f"{run_stem}.turns.csv")
    if not path.exists():
        path = Path(f"{run_stem}-rebuilt.turns.csv")
    if not path.exists():
        return {}
    played = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("action_type") != "wav":
                continue
            played[(row.get("call_id"), row.get("turn"))] = wav_for(row.get("action"))
    return played


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcripts", help="CSV with a text column (and ideally call_id/turn)")
    ap.add_argument("--run", help="run stem, e.g. results/run5-40calls, to match turns to WAVs")
    ap.add_argument("--text-column", default=None)
    ap.add_argument("--wer-threshold", type=float, default=0.34,
                    help="above this a turn counts as misheard (default 0.34)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.transcripts, encoding="utf-8")))
    if not rows:
        sys.exit("no rows in the transcript CSV")

    cols = rows[0].keys()
    text_col = args.text_column or next(
        (c for c in ("text", "transcript", "utterance", "message", "user_text") if c in cols), None)
    if not text_col:
        sys.exit(f"could not find a text column in: {list(cols)}  (use --text-column)")

    wav_col = next((c for c in ("wav", "audio", "file", "action") if c in cols), None)
    played = load_expected_from_run(args.run) if args.run else {}

    results, unknown = [], 0
    for r in rows:
        heard = r.get(text_col, "")
        wav = wav_for(r.get(wav_col)) if wav_col else None
        if not wav and played:
            wav = played.get((r.get("call_id"), r.get("turn")))
        expected = EXPECTED.get(wav) if wav else None
        if expected is None:
            unknown += 1
            continue
        halluc, why = looks_hallucinated(expected, heard)
        results.append({
            "wav": wav, "expected": expected, "heard": heard,
            "wer": wer(expected, heard), "hallucinated": halluc, "why": why,
            "call_id": r.get("call_id", ""), "turn": r.get("turn", ""),
        })

    if not results:
        sys.exit("nothing could be matched to a known recording - pass --run, or add a "
                 "wav/action column, or add it to input_audios/manifest.json")

    print(f"\n{'=' * 70}\n  TRANSCRIPTION ACCURACY  ({len(results)} turns scored)\n{'=' * 70}")

    by_wav = {}
    for r in results:
        by_wav.setdefault(r["wav"], []).append(r)

    print(f"\n  {'recording':<18}{'n':<6}{'exact':<9}{'misheard':<11}{'halluc':<9}mean WER")
    for wav, rs in sorted(by_wav.items()):
        exact = sum(1 for r in rs if r["wer"] == 0)
        bad = sum(1 for r in rs if (r["wer"] or 0) > args.wer_threshold)
        hall = sum(1 for r in rs if r["hallucinated"])
        mean = sum(r["wer"] or 0 for r in rs) / len(rs)
        print(f"  {wav:<18}{len(rs):<6}{exact:<9}{bad:<11}{hall:<9}{mean:.0%}")

    hallucinated = [r for r in results if r["hallucinated"]]
    if hallucinated:
        print(f"\n  HALLUCINATIONS ({len(hallucinated)} of {len(results)} — "
              f"{len(hallucinated)/len(results):.0%})")
        print("  Text that was never spoken. Usually means the recogniser was handed")
        print("  silence or noise rather than speech.\n")
        for r in hallucinated[:15]:
            where = f"call {r['call_id']} turn {r['turn']}" if r["call_id"] else r["wav"]
            print(f"    [{where}] {r['why']}")
            print(f"      said:  {r['expected']}")
            print(f"      heard: {r['heard'][:100]}")

    worst = sorted((r for r in results if not r["hallucinated"] and r["wer"]),
                   key=lambda r: -r["wer"])[:8]
    if worst:
        print("\n  WORST NON-HALLUCINATED TURNS")
        for r in worst:
            print(f"    WER {r['wer']:.0%}  said: {r['expected']!r}  heard: {r['heard'][:70]!r}")

    overall = sum(r["wer"] or 0 for r in results) / len(results)
    clean = sum(1 for r in results if r["wer"] == 0)
    print(f"\n  overall mean WER      {overall:.1%}")
    print(f"  exact matches         {clean}/{len(results)} ({clean/len(results):.0%})")
    if unknown:
        print(f"  unscored rows         {unknown} (no expected text - "
              f"add them to input_audios/manifest.json)")
    print()


if __name__ == "__main__":
    main()
