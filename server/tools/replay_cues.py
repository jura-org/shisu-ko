#!/usr/bin/env python3
"""Replay the cue builder over a dump_words.py file and report the quality metrics.

The other half of the A/B rig of docs/subtitle-quality.md (e). No GPU, no network, no model: the
Whisper output is fixed, so any difference in the numbers below is the cue builder's doing and
nothing else. Check out the old code, run this, check out the new code, run it again.

    python server/tools/dump_words.py DhcrgdOzgic --out /tmp/words   # once, on a GPU
    python server/tools/replay_cues.py /tmp/words/*.words.json       # as often as you like

Metrics, in the order they print:

    n                   cues built
    dur p50 / <1.0s     how long a line stays up; sub-second lines are the flicker
    ch p50 / <=4 / <=6  how much is on the line; a four-character cue cannot be mined
    kinsoku             lines opening on a character that can never open a word; exact, target 0
    okuri               lines opening on hiragana after a kanji; a heuristic, so read the names
    stubs               breaks leaving under MIN_PIECE_CHARS on a side, where a merge could reach
    over-silence        cues whose span is less than half speech; text where there is no voice
    speech-with-text    share of spoken seconds that carry a subtitle
    swaps/min           how often the text changes; the eye re-acquires the box every time

`may_break()` itself is deliberately not the metric. It is a veto on cuts the builder chooses to
make, where refusing costs nothing, so it says no to boundaries that are perfectly good once a real
pause separates them (可愛い感じの子だった気がする || なんか、靴舐めますって言ってた fails it only
because な is a particle). Scoring finished cues with it invents defects.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load_server():
    name = "shisuko_server"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent.parent / "server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server = load_server()


class FakeWord:
    """Quacks like faster_whisper's Word, but mutable, the way absolute_words() expects."""

    def __init__(self, d, offset):
        self.word, self.probability = d["w"], d["p"]
        self.start, self.end = d["s"] - offset, d["e"] - offset


class FakeSegment:
    def __init__(self, d, offset):
        self.text, self.words = d["text"], [FakeWord(w, offset) for w in d["words"]]
        self.start, self.end = d["start"] - offset, d["end"] - offset
        self.avg_logprob, self.no_speech_prob = d["avg_logprob"], d["no_speech_prob"]
        self.compression_ratio = d["compression_ratio"]


def build(records, limits):
    """Drive build_window_cues() window by window, deduping exactly as Transcriber.process does.

    A record dump_words.py marked `lyrics` was decoded without the detector: as process() does,
    its segments go through the lyrics gates, and the spans of those that pass stand in for the
    speech intervals, in the cues and in the speech list the metrics are measured against.
    """
    cues, seg_id, speech, drops = [], 0, [], {}
    for record in records:
        offset, window_end = record["window"]
        segments = [FakeSegment(d, offset) for d in record["segments"]]
        lyrics = bool(record.get("lyrics", False))
        heard = server.lyrics_spans(segments, offset, limits) if lyrics else record["speech"]
        fresh, seg_id = server.build_window_cues(
            segments, offset, heard, limits, seg_id, drops, window_end, lyrics)
        recent = cues[-80:]
        for cue in fresh:
            if any(server.cue_overlaps(cue, r) for r in recent):
                continue
            cue["id"] = len(cues)
            cues.append(cue)
            recent.append(cue)
        speech += [[max(a, offset), min(b, window_end)] for a, b in heard if min(b, window_end) > max(a, offset)]
    return cues, server.merge_intervals(speech), drops


# The metric owns these, and does not import them from server.py: a yardstick made out of the
# thing being measured cannot compare a new cue builder against an old one that has no such names.
NO_LINE_START = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮーヵヶ々〜~,.、。!?！？)）]】」』")
SENTENCE_END = set("。！？!?…")
CLAUSE_BREAK = set("、,，")
MIN_PIECE = 4


def is_kanji(ch: str) -> bool:
    return "一" <= ch <= "鿿" or ch == "々"


def is_hiragana(ch: str) -> bool:
    return "ぁ" <= ch <= "ゟ"


def quantile(values, q: float):
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


def breaks(cues):
    """Every place the viewer sees one line end and the next begin: between cues, and at a seam."""
    out = [(a["text"], b["text"], b["start"] - a["end"]) for a, b in zip(cues, cues[1:])]
    for cue in cues:
        lines = cue["text"].split("\n")
        out += [(h, t, 0.0) for h, t in zip(lines, lines[1:])]
    return out


def report(cues, speech, label: str) -> dict:
    if not cues:
        print(f"{label:<20} no cues")
        return {}
    n = len(cues)
    durations = sorted(c["end"] - c["start"] for c in cues)
    chars = sorted(len(c["text"]) for c in cues)
    pairs = [(h.strip(), t.strip(), gap) for h, t, gap in breaks(cues) if h.strip() and t.strip()]
    # Exact: these characters cannot open a Japanese word, so the cut was inside one.
    kinsoku = [(h, t) for h, t, _ in pairs if t[0] in NO_LINE_START]
    # A heuristic: hiragana after a kanji is usually okurigana (動|いた) but sometimes two words
    # (対象 | あどう?). Read the names it prints rather than the count alone.
    okuri = [(h, t) for h, t, _ in pairs
             if h[-1] not in SENTENCE_END | CLAUSE_BREAK
             and is_hiragana(t[0]) and is_kanji(h[-1])]
    # A short line beside a long silence is a real interjection, not a fragment; only a stub a
    # merge could plausibly have reached counts against the builder.
    stubs = [(h, t) for h, t, gap in pairs
             if gap < 1.5 and h[-1] not in SENTENCE_END and min(len(h), len(t)) < MIN_PIECE]
    silent = [c for c in cues if server.speech_ratio(c["start"], c["end"], speech) < 0.5]
    spans = server.merge_intervals([[c["start"], c["end"]] for c in cues])
    spoken = sum(b - a for a, b in speech)
    with_text = sum(server.interval_overlap(a, b, spans) for a, b in speech)
    minutes = (cues[-1]["end"] - cues[0]["start"]) / 60 or 1e-9
    print(f"{label:<20} n={n:<5} dur p50 {quantile(durations, 0.5):.2f} "
          f"<1.0s {100 * sum(d < 1.0 for d in durations) / n:4.1f}% | "
          f"ch p50 {quantile(chars, 0.5):>2} <=4 {100 * sum(c <= 4 for c in chars) / n:4.1f}% "
          f"<=6 {100 * sum(c <= 6 for c in chars) / n:4.1f}% | "
          f"kinsoku {len(kinsoku):>3} okuri {len(okuri):>3} stubs {len(stubs):>3} | "
          f"over-silence {100 * len(silent) / n:4.1f}% | "
          f"speech-with-text {100 * with_text / max(spoken, 1e-9):4.1f}% | "
          f"swaps/min {n / minutes:.1f}")
    return {"kinsoku": kinsoku, "okuri": okuri, "stubs": stubs, "over_silence": silent}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="<video_id>.words.json from dump_words.py")
    p.add_argument("--max-cue-chars", type=int, default=30)
    p.add_argument("--max-cue-seconds", type=float, default=7.0)
    p.add_argument("--min-cue-seconds", type=float, default=0.8)
    p.add_argument("--sentence-ends", default="auto", choices=["auto", "off"],
                   help="auto: punctuate_words() writes the sentence marks Whisper left out; off: "
                        "cut and merge on Whisper's punctuation alone (the before of the A/B)")
    p.add_argument("--show", action="store_true", help="print every cue, and the defects by name")
    args = p.parse_args(argv)
    limits = server.cue_limits(args)

    for name in args.files:
        path = Path(name)
        records = json.loads(path.read_text(encoding="utf-8"))
        cues, speech, drops = build(records, limits)
        print(f"### {path.stem.replace('.words', '')}")
        found = report(cues, speech, "cues")
        gated = ", ".join(f"{k}:{v}" for k, v in sorted(drops.items()) if not k.startswith("_"))
        print(f"   segments dropped by the gates: {gated or 'none'}")
        if args.show:
            for cue in cues:
                lines = cue["text"].split("\n")
                print(f"   {cue['start']:8.2f}-{cue['end']:7.2f} seg{cue['seg']:>4}  {lines[0]}")
                for line in lines[1:]:
                    print(f"   {'':>30}{line}")
            for kind in ("kinsoku", "okuri", "stubs"):
                for head, tail in found.get(kind, []):
                    print(f"   [{kind}] {head} || {tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
