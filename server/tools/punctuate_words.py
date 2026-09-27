#!/usr/bin/env python3
"""Restore Japanese punctuation on dumped Whisper words with a neural model.

An A/B experiment against the rule-based sentence ends in ``server.py``. The cue
builder splits a line at a sentence end, and Whisper punctuates a fluent speaker
barely at all, so the splitter falls back to ``--max-cue-chars``. This tool asks
the ``punctuators`` PCS model (47 languages, ONNX) where the sentences end and
writes the marks back onto the very words ``dump_words.py`` captured, so
``replay_cues.py`` can build cues from the same Whisper output twice: once as the
decoder left it, once with the model's 。、？ in place.

Usage::

    python server/tools/punctuate_words.py IN.words.json --out OUT_DIR
                                           [--model pcs_47lang]

``--model`` takes an alias of ``PunctCapSegModelONNX`` (``pcs_47lang``) or a
Hugging Face repo id (``1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase``).

Per window the tool joins every segment's words into one string, strips the marks
Whisper already emitted, runs the model once with sentence-boundary detection on,
and walks the model's output against that string. Every non-mark character must
line up one to one; a window whose characters the model altered is left exactly
as it was, with a warning. A mark the model emits after character *k* is appended
to the word owning *k*, unless that word already ended in a mark, in which case
Whisper's own mark stands. Each window gains ``"punctuated": "<model>"``; nothing
else about the record changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Marks stripped before inference and counted after it.
MARKS = "。、？！?!,.，．・"
COUNTED = ("。", "？", "、")


def strip_marks(text: str) -> str:
    return "".join(ch for ch in text if ch not in MARKS)


def ends_in_mark(text: str) -> bool:
    return bool(text) and text[-1] in MARKS


def window_text(window: Dict[str, Any]) -> Tuple[str, List[Tuple[int, int]]]:
    """The window's words, marks removed, plus the (segment, word) owning each character."""
    chars: List[str] = []
    owners: List[Tuple[int, int]] = []
    for si, seg in enumerate(window.get("segments") or []):
        for wi, word in enumerate(seg.get("words") or []):
            for ch in word.get("w", ""):
                if ch in MARKS:
                    continue
                chars.append(ch)
                owners.append((si, wi))
    return "".join(chars), owners


def align_marks(
    source: str, predicted: str
) -> Optional[List[Tuple[int, str]]]:
    """Marks the model added, as ``(index in source of the character before it, mark)``.

    ``None`` when the model did not reproduce ``source`` character for character.
    Whitespace is ignored on both sides: the model joins its sentences with the
    spacing of the language it thinks it sees, which says nothing about the words.
    """
    added: List[Tuple[int, str]] = []
    i = 0  # next unconsumed index in source
    for ch in predicted:
        if ch.isspace():
            continue
        if ch in MARKS:
            if i > 0:
                added.append((i - 1, ch))
            continue
        while i < len(source) and source[i].isspace():
            i += 1
        if i >= len(source) or source[i].casefold() != ch.casefold():
            return None
        i += 1
    while i < len(source) and source[i].isspace():
        i += 1
    if i != len(source):
        return None
    return added


def apply_marks(window: Dict[str, Any], owners: List[Tuple[int, int]],
                added: List[Tuple[int, str]]) -> Dict[str, int]:
    """Append each mark to its word and rebuild the segment texts. Returns the counts."""
    counts: Dict[str, int] = {}
    segments = window["segments"]
    per_word: Dict[Tuple[int, int], str] = {}
    for index, mark in added:
        si, wi = owners[index]
        word = segments[si]["words"][wi]
        if ends_in_mark(word["w"]):
            continue  # Whisper's own mark stands
        per_word[(si, wi)] = per_word.get((si, wi), "") + mark
        counts[mark] = counts.get(mark, 0) + 1

    for (si, wi), marks in per_word.items():
        segments[si]["words"][wi]["w"] += marks
    for seg in segments:
        joined = "".join(w["w"] for w in seg.get("words") or [])
        # Only rebuild a text the words really spell out; anything else stays as it was.
        if strip_marks(joined) == strip_marks(seg.get("text", "")):
            seg["text"] = joined
    return counts


def punctuate(model, windows: List[Dict[str, Any]], name: str) -> None:
    for n, window in enumerate(windows):
        source, owners = window_text(window)
        if not source:
            print(f"window {n} {window.get('window')}: empty, skipped")
            continue
        before = "".join(seg.get("text", "") for seg in window["segments"])

        started = time.perf_counter()
        sentences = model.infer([source], apply_sbd=True)[0]
        took = time.perf_counter() - started
        predicted = "".join(sentences)

        added = align_marks(source, predicted)
        if added is None:
            print(
                f"window {n} {window.get('window')}: WARNING the model altered the "
                f"characters, left unchanged ({took:.2f} s)",
                file=sys.stderr,
            )
            continue

        counts = apply_marks(window, owners, added)
        window["punctuated"] = name
        tally = " ".join(f"{m} {counts.get(m, 0)}" for m in COUNTED)
        other = sum(v for k, v in counts.items() if k not in COUNTED)
        span = window.get("window")
        print(
            f"window {n} {span}: {len(source)} chars, {took:.2f} s, "
            f"added {tally}" + (f", other {other}" if other else "")
        )
        if n == 0:
            after = "".join(seg.get("text", "") for seg in window["segments"])
            print(f"  before: {before}")
            print(f"  after:  {after}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", help="a .words.json file from dump_words.py")
    parser.add_argument("--out", required=True, help="directory for the rewritten file")
    parser.add_argument("--model", default="pcs_47lang",
                        help="punctuators alias or a Hugging Face repo id")
    parser.add_argument("--providers", default="CPUExecutionProvider",
                        help="comma-separated ONNX Runtime providers")
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as fh:
        windows = json.load(fh)

    from punctuators.models import PunctCapSegModelONNX

    started = time.perf_counter()
    model = PunctCapSegModelONNX.from_pretrained(
        args.model, ort_providers=args.providers.split(",")
    )
    print(f"model {args.model} ready in {time.perf_counter() - started:.1f} s")

    punctuate(model, windows, args.model)

    os.makedirs(args.out, exist_ok=True)
    target = os.path.join(args.out, os.path.basename(args.input))
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(windows, fh, ensure_ascii=False)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
