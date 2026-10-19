#!/usr/bin/env python3
"""Replay the cue builder over a dump_words.py file and report the quality metrics.

The other half of the A/B rig of docs/subtitle-quality.md (e). No GPU, no network, no model: the
Whisper output is fixed, so any difference in the numbers below is the cue builder's doing and
nothing else. Check out the old code, run this, check out the new code, run it again.

    python server/tools/dump_words.py VIDEO_ID --out /tmp/words   # once, on a GPU
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

With --human FILE (a creator's subtitle track: .json3, .srv3 or .vtt, as YouTube serves it), the
same Whisper output is built twice, Whisper alone and with the track put in by assist_cues(), and
both are scored against the track:

    cer                 character error rate of the cues' text against the track's, folded as the
                        aligner folds it (NFKC, kana, no punctuation), counted per minute
    marked / seen       the track's sentence ends (。！？) the cues carry a mark at / end a cue or a
                        row at
    human / track / whisper   cues from an aligned line, from a line on its own times, from Whisper
    words bad           cues whose words do not spell the text or run backwards
    start shift         how far an aligned line's start moved from the track's own, p50 / p90

`--human-scope window` (default) puts each window's lines in as Transcriber.process() does, on the
dump's fixed windows (the server also moves a window's end off a line, which a dump cannot);
`whole` puts them all in at once over the built cues, as App.apply_human_track() does for cues made
before the track arrived. `--human-raw` skips the speech-recognition test, to see what such a
track would have done.

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


def build(records, limits, human=None, scope="window", stats=None):
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
        owned = server.lines_in_window(human, offset, window_end) if human and scope == "window" else []
        if human and scope == "window":
            fresh, seg_id = server.assist_cues(fresh, owned, heard, limits, seg_id, window_end, stats,
                                               server.neighbour_lines(human, offset, window_end))
        recent = cues[-80:]
        for cue in fresh:
            if any(server.cue_overlaps(cue, r) for r in recent):
                continue
            cue["id"] = len(cues)
            cues.append(cue)
            recent.append(cue)
        speech += [[max(a, offset), min(b, window_end)] for a, b in heard if min(b, window_end) > max(a, offset)]
    speech = server.merge_intervals(speech)
    if human and scope == "whole":
        lo, hi = records[0]["window"][0], records[-1]["window"][1]
        inside = [line for line in human if lo <= line[0] and line[1] <= hi]
        cues, seg_id = server.assist_cues(cues, inside, speech, limits, seg_id, None, stats)
        for i, cue in enumerate(cues):
            cue["id"] = i
    return cues, speech, drops


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


# --------------------------------------------------------------------------- against a human track

def folded_marks(texts) -> tuple:
    """Folded characters of the texts in order, and per character what follows it before the next
    one: a sentence mark ("." in the flags) and/or a cue or row boundary ("|")."""
    chars, flags = [], []
    for text in texts:
        for ch in text:
            if server.fold_chars(ch)[0]:
                for c in server.fold_chars(ch)[0]:
                    chars.append(c)
                    flags.append("")
            elif flags and ch in SENTENCE_END:
                flags[-1] += "."
            elif flags and ch == "\n":
                flags[-1] += "|"
        if flags:
            flags[-1] += "|"
    return chars, flags


def align(a, b) -> list:
    """Levenshtein alignment: for each index of a, the index of b it pairs with, or -1."""
    n, m = len(a), len(b)
    prev = list(range(m + 1))
    back = []
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ops = bytearray(m + 1)
        ops[0] = 1
        for j in range(1, m + 1):
            best, op = prev[j - 1] + (a[i - 1] != b[j - 1]), 0
            if prev[j] + 1 < best:
                best, op = prev[j] + 1, 1
            if cur[j - 1] + 1 < best:
                best, op = cur[j - 1] + 1, 2
            cur[j], ops[j] = best, op
        back.append(ops)
        prev = cur
    pairs, i, j = [-1] * n, n, m
    while i > 0:
        op = back[i - 1][j] if j > 0 else 1
        if op == 0:
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif op == 1:
            i -= 1
        else:
            j -= 1
    return pairs, prev[m]


def against_track(cues, lines, lo: float, hi: float) -> dict:
    """CER and sentence ends of the cues against the track, minute by minute over [lo, hi)."""
    errors = total = ends = marked = seen = 0
    t = lo
    while t < hi:
        ref = [line[2] for line in lines if t <= line[0] < min(t + 60, hi)]
        out = [c["text"] for c in cues if t <= c["start"] < min(t + 60, hi)]
        rc, rf = folded_marks(ref)
        oc, of = folded_marks(out)
        pairs, dist = align(rc, oc)
        errors, total = errors + dist, total + len(rc)
        for i, flag in enumerate(rf):
            if "." not in flag or i + 1 == len(rf):
                continue  # a sentence end, and not the minute's last, whose boundary is the chunk's
            ends += 1
            j = pairs[i]
            near = of[j] + (of[j + 1] if 0 <= j + 1 < len(of) and pairs[i + 1] != j + 1 else "") if j >= 0 else ""
            marked += "." in near
            seen += "|" in near
        t += 60
    return {"cer": errors / max(total, 1), "ends": ends, "marked": marked / max(ends, 1), "seen": seen / max(ends, 1)}


def human_report(cues, lines, lo, hi, label: str) -> None:
    score = against_track(cues, lines, lo, hi)
    src = [c for c in cues if c.get("src") == "human"]
    aligned = [c for c in src if "words" in c]
    bad = 0
    for c in cues:
        ws = c.get("words")
        if ws and ("".join(w["text"] for w in ws) != c["text"]
                   or any(w["end"] < w["start"] for w in ws)
                   or any(b["start"] < a["end"] for a, b in zip(ws, ws[1:]))):
            bad += 1
    by_text = {}
    for line in lines:
        by_text.setdefault(line[2], []).append(line[0])
    shifts = sorted(min(abs(c["start"] - t) for t in by_text[c["text"]]) for c in aligned if c["text"] in by_text)
    n = max(len(cues), 1)
    print(f"{label:<20} cer {100 * score['cer']:5.1f}% | sentence ends {score['ends']}: marked "
          f"{100 * score['marked']:5.1f}% seen {100 * score['seen']:5.1f}% | human {100 * len(aligned) / n:5.1f}% "
          f"track {100 * (len(src) - len(aligned)) / n:5.1f}% whisper {100 * (len(cues) - len(src)) / n:5.1f}% | "
          f"words bad {bad} | start shift p50 {quantile(shifts, 0.5):.2f} p90 {quantile(shifts, 0.9):.2f}")


def read_track(path: Path, raw_track: bool) -> list:
    data = path.read_bytes()
    ext = path.suffix.lstrip(".")
    if not raw_track:
        return server.human_subtitle_lines(data, ext) or []
    lines, _ = {"json3": server.parse_json3, "srv3": server.parse_srv3, "vtt": server.parse_vtt}[ext](
        data.decode("utf-8-sig"))
    return [[round(a, 2), round(b, 2), server.clean_subtitle_text(t)] for a, b, t in sorted(lines)
            if server.clean_subtitle_text(t) and b > a]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="<video_id>.words.json from dump_words.py, or a cue record "
                                            "(<video_id>.new.cues.json from retranscribe.py) to score as it is")
    p.add_argument("--max-cue-chars", type=int, default=30)
    p.add_argument("--max-cue-seconds", type=float, default=7.0)
    p.add_argument("--min-cue-seconds", type=float, default=0.8)
    p.add_argument("--sentence-ends", default="auto", choices=["auto", "off"],
                   help="auto: punctuate_words() writes the sentence marks Whisper left out; off: "
                        "cut and merge on Whisper's punctuation alone (the before of the A/B)")
    p.add_argument("--show", action="store_true", help="print every cue, and the defects by name")
    p.add_argument("--human", help="the creator's subtitle track (.json3, .srv3, .vtt): build with it as well, "
                                   "and score both builds against it (one dump file only)")
    p.add_argument("--human-scope", default="window", choices=["window", "whole"])
    p.add_argument("--human-raw", action="store_true", help="use the track even when it reads as speech recognition")
    p.add_argument("--score-to", type=float, default=0.0, help="score against the track up to this second only "
                                                                 "(0: all of it)")
    args = p.parse_args(argv)
    limits = server.cue_limits(args)
    if args.human and len(args.files) != 1:
        p.error("--human scores one dump file against one track")

    for name in args.files:
        path = Path(name)
        records = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(records, dict):
            # A cue record (the cache's, or retranscribe.py's): nothing to build, only to score.
            cues, speech = records.get("cues", []), records.get("speech", [])
            print(f"### {path.name.split('.')[0]}")
            report(cues, speech, "cues")
            if args.human:
                lines = read_track(Path(args.human), args.human_raw)
                hi = args.score_to or max((iv[1] for iv in records.get("covered", [])), default=0.0)
                human_report(cues, lines, 0.0, hi, "cues vs track")
            continue
        cues, speech, drops = build(records, limits)
        print(f"### {path.stem.replace('.words', '')}")
        found = report(cues, speech, "cues")
        if args.human:
            lines = read_track(Path(args.human), args.human_raw)
            if not lines:
                print("   the track reads as speech recognition, or is empty: nothing to put in (--human-raw)")
            else:
                lo, hi = records[0]["window"][0], args.score_to or records[-1]["window"][1]
                stats: dict = {}
                assisted, _, _ = build(records, limits, lines, args.human_scope, stats)
                report(assisted, speech, "with the track")
                human_report(cues, lines, lo, hi, "whisper vs track")
                human_report(assisted, lines, lo, hi, "assisted vs track")
                sims = sorted(stats.pop("_similarity", []))
                print(f"   track lines: {stats}")
                if sims:
                    print("   similarity p10 / p50 of lines Whisper heard: "
                          f"{quantile([x[0] for x in sims], 0.1):.2f} / {quantile([x[0] for x in sims], 0.5):.2f}; "
                          + ", ".join(f"<{t:.1f}: {sum(x[0] < t for x in sims)}" for t in (0.4, 0.5, 0.6, 0.7, 0.8)))
                if args.show:
                    for sim, text, heard in sims:
                        if sim < 0.8:
                            print(f"   [similarity {sim:.2f}] {text!r} || {heard}")
                if args.show:
                    cues = assisted
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
