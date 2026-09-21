#!/usr/bin/env python3
"""
Local transcription server for Shisu-ko, the Firefox extension that overlays live Whisper
subtitles on YouTube for Japanese learners.

The extension posts {video_id, url, t, since, model} to /sync about once a second while a
YouTube video plays. For each new video the server downloads the audio track with
yt-dlp, decodes it to 16 kHz mono, and a single worker thread transcribes it with
faster-whisper, starting at the current playhead and continuing ahead of it in
windows. Cues are returned incrementally, cached on disk, and the extension renders
them as ordinary DOM text so dictionary tools such as Yomitan can scan them.

A live stream has no file to download: the server follows its DASH audio segments
instead, keeping the last minutes decoded in memory, and transcribes just behind the
live edge. Times are the stream's own media clock, which the extension reads from the
player, so cues line up whatever latency the viewer is watching at.

Endpoints
  GET  /health   -> {ok, version, model, default_model, model_loading, model_error, models, device, compute_type,
                     language, launcher}
  POST /sync     -> {ok, session, status, error, duration, title, live, covered, speech, cues, next, busy,
                     model, model_loading, model_error, heard, language_paused}
  GET  /clip?video_id=..&start=..&end=..&format=mp3|wav -> audio clip of a sentence (mining)
  GET  /sessions -> the videos the server holds, for diagnostics
  POST /update   -> {ok, restarting, version}: the server exits with EXIT_UPDATE so that run.cmd / run.sh
                     run update.py and start it again; 409 {ok, error} when nothing would (Docker, Nix,
                     a hand start, --no-update, SHISUKO_NO_UPDATE). Extension and local non-browser
                     clients only: a loopback page may not restart the server (update_origin_allowed())

Everything lives under ~/.shisu-ko (override with the SHISUKO_HOME environment variable):
the Python environment, downloaded models, cached audio and cue files, and config.json with
the model chosen at setup (server.py --download-model NAME), the default of --model.
"""
from __future__ import annotations

import argparse
import errno
import gc
import glob
import io
import json
import logging
import math
import os
import re
import shutil
import site
import subprocess
import sys
import threading
import time
import uuid
import wave
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

try:
    import msvcrt  # Windows: byte-range locks for the instance lock
except ImportError:  # pragma: no cover - not Windows
    msvcrt = None  # type: ignore[assignment]
try:
    import fcntl  # POSIX: flock for the same
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

VERSION = "0.11.4"
# Exit codes run.cmd / run.sh act on: 0 stops the loop, 2 is a startup error that must not be retried
# (sys.exit; a failed --download-model ends on it too), 3 asks for a plain restart (os._exit: a broken
# GPU context, no model left) and
# EXIT_UPDATE asks the launcher to run update.py first and then start the server again (POST /update).
EXIT_UPDATE = 4
SAMPLE_RATE = 16000
APP_DIR = Path(os.environ.get("SHISUKO_HOME") or (Path.home() / ".shisu-ko"))
CACHE_DIR = APP_DIR / "cache"
MODELS_DIR = APP_DIR / "models"
CONFIG_PATH = APP_DIR / "config.json"  # {"model": ...}, written by --download-model at setup; read_config()
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
# A faster-whisper size or a Hugging Face repo id. WhisperModel() also opens local directories, so
# anything else (paths, "..") is refused before it can point the server at an arbitrary folder.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$")
MODEL_NAME_HINT = ("not a model name: use a faster-whisper size (large-v3, large-v3-turbo, small, ...) "
                   "or a Hugging Face repo id like owner/name")
DEFAULT_MODEL = "large-v3"  # --model when neither the flag nor config.json names one
CACHE_FORMAT = 3  # bumped when cue fields change; older caches are ignored and transcribed again
SPEECH_SYNC_BACK = 30.0   # seconds of speech intervals sent behind the playhead
SPEECH_SYNC_AHEAD = 120.0  # ... and ahead of it
LANGUAGE_MIN_SPEECH = 4.0        # a window with less speech than this gets no language vote:
LANGUAGE_MIN_PROB = 0.7          # a short clip is padded to 30 s, where the language head guesses
                                 # worst, so a confident vote needs real speech and real confidence
LANGUAGE_DETECT_SECONDS = 30.0   # Whisper judges one encoder window; more speech than that is wasted
LANGUAGE_PROBE_AHEAD = 90.0      # a paused video is only probed this far ahead of the playhead, not --lookahead
AUDIO_SUFFIXES = {".webm", ".m4a", ".opus", ".mp4", ".mp3", ".ogg", ".oga", ".wav", ".mka", ".aac"}
PARTIAL_SUFFIXES = {".part", ".ytdl"}  # yt-dlp's in-progress download and its fragment state

log = logging.getLogger("shisu-ko")


def add_nvidia_dll_dirs() -> list:
    """Make pip-installed CUDA libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12) visible on Windows."""
    found = []
    if os.name != "nt":
        return found
    roots = set(site.getsitepackages())
    try:
        roots.add(site.getusersitepackages())
    except Exception:
        pass
    for root in roots:
        for directory in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(directory)
            except (OSError, AttributeError):
                continue
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
            found.append(directory)
    return found


# The Hugging Face "xet" transfer backend has stalled on some Windows machines; plain HTTPS
# downloads are slower to start but reliable. Set HF_HUB_DISABLE_XET=0 to opt back in.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# Two notices the model download prints for every viewer and neither of which asks for
# anything a viewer should do: the Hub sends "You are sending unauthenticated requests ...
# set a HF_TOKEN" as a warning header, which huggingface_hub logs through its own handler
# and through ours (twice on screen), and on Windows without Developer Mode it warns that
# its cache cannot use symlinks (it works, it just keeps a copy). Errors still come
# through: a failed download is reported by friendly_model_error() with its own words.
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

NVIDIA_DIRS = add_nvidia_dll_dirs()

import numpy as np  # noqa: E402  (after the DLL setup on purpose)


# --------------------------------------------------------------------------- helpers

def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def merge_intervals(intervals, gap: float = 0.05) -> list:
    merged: list = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a)):
        if merged and a <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def find_covering(intervals, t: float, tol: float = 0.25):
    for a, b in intervals:
        if a - tol <= t <= b + tol:
            return (a, b)
    return None


def next_start_after(intervals, t: float):
    best = None
    for a, _ in intervals:
        if a > t and (best is None or a < best):
            best = a
    return best


def subtract_intervals(intervals, holes) -> list:
    """`intervals` with every part inside one of `holes` cut out; both merged, the result merged."""
    out: list = []
    holes = merge_intervals(holes)
    for a, b in merge_intervals(intervals):
        pos = a
        for x, y in holes:
            if y <= pos or x >= b:
                continue
            if x > pos:
                out.append([pos, x])
            pos = max(pos, y)
        if b > pos:
            out.append([pos, b])
    return out


def unheard_stretches(covered, speech, cues, min_seconds: float = 1.5, inner_seconds: Optional[float] = None) -> list:
    """The parts of `covered` that no speech interval and no cue touches: at least `min_seconds`
    long at the start or end of a covered range, at least `inner_seconds` (the same when None)
    between two heard things.

    A cache from before the lyrics rule (P0.3) marked a sung window covered with nothing in it:
    Silero heard no speech there, so nothing reached the decoder. load_cache() gives such
    stretches back to the planner; below plan_window()'s own floor a gap would never be planned.
    The two floors: the ends of a covered range are where a video's music sits (an intro, an outro,
    the whole of a Short), while between two lines a short hole is a pause of the talk, which the
    lead-out and the detector's padding already shrank by a second or so.
    """
    heard = merge_intervals(list(speech) + [[c["start"], c["end"]] for c in cues])
    covered = merge_intervals(covered)
    edges = {a for a, _ in covered} | {b for _, b in covered}
    inner = min_seconds if inner_seconds is None else inner_seconds
    return [[a, b] for a, b in subtract_intervals(covered, heard)
            if b - a >= (min_seconds if a in edges or b in edges else inner)]


SENTENCE_END = set("。！？!?…")
CLAUSE_BREAK = set("、,，")
JUNK_RE = re.compile(r"^[\s\W_]*$")


# --------------------------------------------------------------------------- speech intervals (VAD)

# Silero settings, see docs/subtitle-quality.md (P0.1). The library's own defaults leave
# min_speech_duration_ms at 0, so single 32 ms transients (drums, synth stabs) reach the decoder.
VAD_PARAMS = {
    "threshold": 0.5,
    "neg_threshold": 0.35,
    "min_speech_duration_ms": 250,
    "min_silence_duration_ms": 300,
    "speech_pad_ms": 200,
}
# faster-whisper forces max_speech_duration_s to the 30 s encoder window when vad_parameters is a
# dict, so our own pass must use the same value to produce exactly the same intervals.
VAD_MAX_SPEECH_SECONDS = 30.0


def vad_parameters() -> dict:
    """A fresh dict for transcribe(); the library pops keys out of the one it is given."""
    return dict(VAD_PARAMS)


def detect_speech(audio, offset: float = 0.0) -> list:
    """Silero speech intervals of one window, in absolute seconds."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(**VAD_PARAMS, max_speech_duration_s=VAD_MAX_SPEECH_SECONDS)
    chunks = get_speech_timestamps(audio, options, sampling_rate=SAMPLE_RATE)
    return [[offset + c["start"] / SAMPLE_RATE, offset + c["end"] / SAMPLE_RATE] for c in chunks]


def interval_overlap(start: float, end: float, intervals) -> float:
    """Seconds of [start, end) covered by a sorted, merged interval list."""
    total = 0.0
    for a, b in intervals:
        if b <= start:
            continue
        if a >= end:
            break
        total += min(end, b) - max(start, a)
    return total


def speech_ratio(start: float, end: float, intervals) -> float:
    """Share of [start, end) that is speech; 0.0 for an empty range."""
    if end <= start:
        return 0.0
    return interval_overlap(start, end, intervals) / (end - start)


def distance_to_speech(t: float, intervals) -> float:
    """Seconds from t to the nearest speech interval; 0.0 when no intervals are known."""
    if not intervals:
        return 0.0
    best = float("inf")
    for a, b in intervals:
        if a <= t <= b:
            return 0.0
        best = min(best, a - t if t < a else t - b)
    return best


def next_silence(t: float, intervals) -> tuple:
    """(start, end) of the first silence at or after t; end is inf when no speech follows."""
    if not intervals:
        return (t, float("inf"))
    start = t
    for a, b in intervals:
        if a <= t < b:
            start = b
            break
    nxt = next_start_after(intervals, start)
    return (start, nxt if nxt is not None else float("inf"))


def silence_around(start: float, end: float, intervals) -> tuple:
    """Silence before and after an utterance, measured from its own speech interval outwards.

    An unknown neighbour (nothing in this window before or after) counts as no silence, so the
    isolation test stays conservative at window edges.
    """
    if not intervals:
        return (0.0, 0.0)
    left, right = start, end
    for a, b in intervals:
        if a <= start <= b:
            left = min(left, a)
        if a <= end <= b:
            right = max(right, b)
    prev_end = max((b for _, b in intervals if b <= left), default=None)
    nxt = next_start_after(intervals, right)
    return (left - prev_end if prev_end is not None else 0.0,
            nxt - right if nxt is not None else 0.0)


def nearest_onset(intervals, t: float, reach: float):
    """Start of the speech interval t belongs to, but only when t sits near that start.

    A cue in the middle of a long speech interval must not be dragged back to its onset, so
    anything further than `reach` into an interval is left alone.
    """
    for a, b in intervals:
        if a - reach <= t <= min(b, a + reach):
            return a
    return None


# --------------------------------------------------------------------------- word timing repair

# faster-whisper anchors a segment's first word to the segment's own start, and segment starts run
# flush with the previous segment's end. The result is one or two characters stranded in the
# previous utterance, seconds before the rest of the sentence they belong to. Measured over 448
# multi-word segments of a 17-minute video: the gap after word[0] has p90 0.72 s and a worst case
# of 9.2 s, while every later position has p90 0.00 s; 23% of segments put word[0] in a different
# speech interval from word[1]. Left alone that one artifact splits words (コ|ラボ配信を), floats a
# single character over silence, makes the VAD and anomaly gates below delete whole real
# utterances, and feeds trim_words a leading mora to eat.
LEAD_REPAIR_GAP = 0.30     # a gap this big straight after the first words is the artifact, not a pause
LEAD_REPAIR_CHARS = 6      # only a short head can be a mis-anchored fragment
LEAD_REPAIR_WORDS = 3


def repair_lead_words(words, speech) -> list:
    """Slide a segment's stranded first words onto the front of the utterance they belong to.

    Rewrites the Word objects in place and returns the same list; every caller hands it a fresh
    list from absolute_words(), and one that did not would find its word timings rewritten.

    Conservative on purpose: the head only moves forward, only when it is short, and only out of
    a speech interval it does not already share with the rest of the segment. Moving one backwards
    would drop it on the previous utterance, where cue_overlaps() then deletes a whole good cue.
    """
    if len(words) < 2 or not speech:
        return words
    for k in range(min(LEAD_REPAIR_WORDS, len(words) - 1)):
        head, nxt = words[:k + 1], words[k + 1]
        if nxt.start - head[-1].end < LEAD_REPAIR_GAP:
            break
        if len(word_text(head)) > LEAD_REPAIR_CHARS:
            break
        interval = find_covering(speech, nxt.start, tol=0.02)
        if interval is None or interval[0] >= nxt.start:
            continue
        # Two ways to say "this head is not stranded". The second catches every case on sorted,
        # non-overlapping intervals, which is all detect_speech() and merge_intervals() produce;
        # the first is kept because it is the one that states the rule the regression turns on.
        if interval[0] <= head[0].start or find_covering(speech, head[-1].end, tol=0.02) == interval:
            break
        onset = interval[0]
        span = max(head[-1].end - head[0].start, 1e-3)
        scale = min(1.0, (nxt.start - onset) / span)
        base = head[0].start
        for w in head:
            w.start = onset + (w.start - base) * scale
            w.end = onset + (w.end - base) * scale
        head[-1].end = min(head[-1].end, nxt.start)
        break
    return words


# --------------------------------------------------------------------------- hallucination gates

@dataclass
class Word:
    word: str
    start: float
    end: float
    probability: float = 1.0


# Whisper's stock sign-offs. Real videos say these too, so they only count against a segment that
# also fails the VAD or isolation test below (a lyrics window has no such evidence to offer, so
# lyrics_reason drops them outright).
BLOCKLIST_PHRASES = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "ご清聴ありがとうございました",
    "ご覧いただきありがとうございます",
    "チャンネル登録",
    "おやすみなさい",
    "字幕by",
    "字幕 by",
    "字幕提供",
    "字幕視聴",
)
BLOCKLIST_MAX_OVERLAP = 0.8   # above this the cue sits on real speech and is kept
BLOCKLIST_ISOLATION = 3.0     # seconds of silence on both sides that make a sign-off suspicious
VAD_GATE_OVERLAP = 0.5
VAD_GATE_CHARS = 8            # a long, confident segment survives a low overlap
VAD_GATE_PROB = 0.5
ANOMALY_MAX_OVERLAP = 0.8
COMPRESSION_LIMIT = 2.2
REPEAT_MIN_RUN = 6     # a unit repeated this many times back to back is a loop whatever it says
REPEAT_MIN_REPS = 3    # three repeats only count as a loop when they fill a line
REPEAT_MIN_CHARS = 16
# Sung lyrics (P0.3 of the doc). Silero hears no speech in singing over music (an anime opening:
# 0 s at the 0.5 threshold, 2.7 s at 0.2), so nothing of it reached Whisper, which transcribes the
# same audio cleanly on its own. A window with next to no detected speech whose audio is not
# silence, in which the language head then hears the target language (Transcriber.sung_in_target;
# rain, a crowd, an English song under a montage stay with the detector, which decodes nothing of
# them), is therefore transcribed without the detector, and with no intervals to weigh a segment
# against, the decoder's own confidence gates it instead (lyrics_reason).
LYRICS_MAX_SPEECH_S = 1.0    # more detected speech than this and the window is ordinary talk
LYRICS_MIN_RMS = 0.02        # about -34 dBFS: sung windows measure 0.11-0.47, a timelapse's background
                             # music 0.015, room tone far below
LYRICS_MAX_NO_SPEECH = 0.9   # the decoder's "not speech" probability for its 30 s decode. No judge of
                             # singing: a rap verse scored 0.59 and an 18-voice chorus 0.80, every line
                             # right, while a sign-off made up over background music scored 0.47; so
                             # only what the decoder is all but sure of is refused on it
LYRICS_MIN_LOGPROB = -0.8    # sung windows scored -0.14 to -0.58; made-up lines -0.49 to -0.91
LYRICS_MIN_WORD_PROB = 0.35  # mean word probability: genuine lines from 0.48, a garbled それられ 0.30
LYRICS_MIN_STRETCH_S = 4.0   # a talk window's loud stretch the detector heard nothing in is planned again
                             # as a window of its own from this length: about one sung line, and each
                             # such window costs a Silero pass and an encoder pass for the head


def absolute_words(seg, offset: float) -> list:
    """The segment's words shifted onto the video's timeline."""
    return [
        Word(w.word, offset + float(w.start), offset + float(w.end), float(getattr(w, "probability", 1.0) or 0.0))
        for w in (getattr(seg, "words", None) or [])
    ]


PUNCTUATION = set("\"'“¿([{-。！？、，,.!?:：;；)]}、…～~ー'\"")


def word_anomaly_score(word: Word) -> float:
    """Port of faster_whisper.transcribe.word_anomaly_score (1.2.1, MIT): long, short or improbable words."""
    score = 0.0
    duration = word.end - word.start
    if word.probability < 0.15:
        score += 1.0
    if duration < 0.133:
        score += (0.133 - duration) * 15
    if duration > 2.0:
        score += duration - 2.0
    return score


def is_segment_anomaly(words) -> bool:
    """Port of faster_whisper.transcribe.is_segment_anomaly (1.2.1, MIT)."""
    words = [w for w in words if w.word.strip() and w.word.strip() not in PUNCTUATION][:8]
    if not words:
        return False
    score = sum(word_anomaly_score(w) for w in words)
    return score >= 3 or score + 0.01 >= len(words)


def compression_ratio(text: str) -> float:
    data = text.encode("utf-8")
    return len(data) / len(zlib.compress(data)) if data else 0.0


def has_repetition(text: str) -> bool:
    """True when a substring repeats back to back often enough to be a decoder loop.

    Measured, not guessed: the doc's "3 repeats of 2 characters" also deletes ordinary Japanese
    backchannels (そうそうそう, 違う違う違う, あるあるある - 15 genuine lines in a 76-minute sample),
    so a loop must either repeat REPEAT_MIN_RUN times or fill REPEAT_MIN_CHARS characters.
    """
    t = "".join(text.split())
    n = len(t)
    for size in range(1, n // 2 + 1):
        i = 0
        while i <= n - 2 * size:
            unit = t[i:i + size]
            reps, j = 1, i + size
            while t[j:j + size] == unit:
                reps += 1
                j += size
            if reps >= REPEAT_MIN_RUN or (reps >= REPEAT_MIN_REPS and reps * size >= REPEAT_MIN_CHARS):
                return True
            i = j if reps > 1 else i + 1
    return False


def hallucination_reason(seg, words, speech):
    """Name of the gate that rejects this segment, or None when it passes. See P0.2 of the doc."""
    text = (getattr(seg, "text", "") or "").strip()
    if not words or not text or JUNK_RE.match(text):
        return "empty"

    start, end = words[0].start, words[-1].end
    overlap = speech_ratio(start, end, speech)
    mean_prob = sum(w.probability for w in words) / len(words)
    if overlap < VAD_GATE_OVERLAP and not (len(text) >= VAD_GATE_CHARS and mean_prob >= VAD_GATE_PROB):
        return "vad"
    if overlap < ANOMALY_MAX_OVERLAP and is_segment_anomaly(words):
        return "anomaly"
    # seg.compression_ratio is faster-whisper's value for the whole 30 s decode, shared by every
    # segment it produced, so one loop would take its innocent neighbours with it. Use this text.
    if has_repetition(text) or compression_ratio(text) > COMPRESSION_LIMIT:
        return "repetition"
    if any(p in text for p in BLOCKLIST_PHRASES):
        before, after = silence_around(start, end, speech)
        if overlap < BLOCKLIST_MAX_OVERLAP or (before >= BLOCKLIST_ISOLATION and after >= BLOCKLIST_ISOLATION):
            return "blocklist"
    return None


def lyrics_reason(seg, words):
    """Name of the gate that rejects a segment of a lyrics window, or None when it passes (P0.3).

    The detector heard nothing here, so there is no VAD overlap to excuse a segment with: the
    decoder's own confidence stands in ("unsure"), and every other gate applies on its own.
    """
    text = (getattr(seg, "text", "") or "").strip()
    if not words or not text or JUNK_RE.match(text):
        return "empty"
    mean_prob = sum(w.probability for w in words) / len(words)
    no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
    logprob = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
    if no_speech > LYRICS_MAX_NO_SPEECH or logprob < LYRICS_MIN_LOGPROB or mean_prob < LYRICS_MIN_WORD_PROB:
        return "unsure"
    if is_segment_anomaly(words):
        return "anomaly"
    if has_repetition(text) or compression_ratio(text) > COMPRESSION_LIMIT:
        return "repetition"
    if any(p in text for p in BLOCKLIST_PHRASES):
        return "blocklist"
    return None


# --------------------------------------------------------------------------- cue building

@dataclass
class CueLimits:
    """Cue geometry from docs/subtitle-quality.md (P1); seconds unless the name says chars."""
    max_chars: int = 30          # 26 = 13 x 2 lines (Netflix JP); 30 keeps mined sentences whole
    max_seconds: float = 7.0     # Netflix's maximum; a merged cue is often near it now
    min_seconds: float = 0.8     # Netflix's general minimum
    hard_min_seconds: float = 0.5  # the Japanese floor: never go below this
    clause_ratio: float = 0.6    # a 、 breaks the line once the buffer is this full
    lead_in: float = 0.08
    lead_out: float = 0.70       # the beat of padding after the audio; measured in the doc's (e)
    min_gap: float = 0.10
    dead_zone: float = 0.50      # gaps between min_gap and this read as a glitch, so they are closed
    pause_split: float = 0.45
    vad_silence: float = 0.35    # a pause only splits when this much of it is real silence
    merge_gap: float = 0.35      # adjacent cues closer than this merge
    merge_reach: float = 1.0     # a too-short cue may merge with a neighbour this far away
    trim_slack: float = 0.15     # edge words whose midpoint is this far outside speech are dropped
    snap_reach: float = 0.60
    lead_out_silence: float = 0.30
    # Merging across Whisper segments (merge_segments).
    seam_gap: float = 0.25       # a pause at least this long reads as a new line, not a continuation
    cross_reach: float = 1.5     # how far a cue too short to read, or a broken word, may reach
    cross_ceiling: float = 8.5   # even a broken word may not build a cue longer than this
    cross_chars: int = 34        # nor a wider one than this
    reach_chars: int = 8         # a cue this short is worth reaching cross_reach for a partner
    max_lines: int = 2           # the professional ceiling, and what the overlay has room for


def cue_limits(args) -> CueLimits:
    return CueLimits(
        max_chars=int(getattr(args, "max_cue_chars", 30)),
        max_seconds=float(getattr(args, "max_cue_seconds", 7.0)),
        min_seconds=float(getattr(args, "min_cue_seconds", 0.8)),
    )


def word_text(words) -> str:
    return "".join(w.word for w in words).strip()


def trim_words(words, speech, slack: float) -> list:
    """Drop edge words whose midpoint lies outside every speech interval (P1.1).

    Whisper stretches the first and last word of a segment; those are exactly the two timestamps
    the cue in and out times come from.
    """
    if not speech:
        return list(words)
    lo, hi = 0, len(words)
    while lo < hi and distance_to_speech((words[lo].start + words[lo].end) / 2, speech) > slack:
        lo += 1
    while hi > lo and distance_to_speech((words[hi - 1].start + words[hi - 1].end) / 2, speech) > slack:
        hi -= 1
    return list(words[lo:hi])


# Kana that only ever continue the sound before them, and so may never open a line (kinsoku shori),
# together with the closing marks that belong to the line they end.
NO_LINE_START = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮーヵヶ々〜~,.、。!?！？)）]】」』")
# Hiragana that attach to what came before: particles and auxiliaries. A line may not open with one.
PARTICLE_START = set("がをにへはもやかねよぞなのでとんだしてたる")
MIN_PIECE_CHARS = 4         # nothing shorter is a line of its own, or either side of a break
OKURIGANA_TAIL_CHARS = 8    # a longer tail after a kanji is a new word, not that kanji's okurigana


def is_kanji(ch: str) -> bool:
    return "一" <= ch <= "鿿" or ch == "々"


def is_hiragana(ch: str) -> bool:
    return "ぁ" <= ch <= "ゟ"


def breaks_word(head_text: str, tail_text: str) -> bool:
    """True when a cut between these two texts lands inside a word, on evidence and not on taste.

    Japanese writes no spaces, so the cheap evidence of a word boundary is the script change. Two
    signs are unambiguous: the tail opens with a character that can never open a word, or with
    hiragana directly after a kanji, which is okurigana. That pair is what keeps 言|ってた, 広|い,
    動|いた and 思|っております off the screen. Katakana is left out deliberately - katakana words are
    self-delimiting, so the hiragana after カメラ or ポンポン does open a new word.

    Kept apart from may_break() because only this one may overrule a length budget. Refusing a cut
    the splitter was about to make costs nothing, so may_break() can also say no on taste; a merge
    that overrules its own limits on taste builds a 41-character line.
    """
    head, tail = (head_text or "").strip(), (tail_text or "").strip()
    if not head or not tail:
        return False
    if tail[0] in NO_LINE_START:
        return True
    if head[-1] in SENTENCE_END | CLAUSE_BREAK:
        return False
    # Okurigana is a few kana on the end of a stem, so only a short tail is evidence of one. A long
    # one after a kanji is a new word - 全然 || こういうピンクとかでもいけちゃいそう - and forcing
    # that merge past the length budget builds a line that wraps to three rows.
    return is_hiragana(tail[0]) and is_kanji(head[-1]) and len(tail) <= OKURIGANA_TAIL_CHARS


def may_break(head_text: str, tail_text: str) -> bool:
    """False when a cut between these two texts would land inside a word, or read as a stub.

    The splitter's rules, and it can afford to be fussier than breaks_word(): refusing a cut only
    leaves two words together, so this also says no to a piece too short to read, to a line opening
    on a particle, and to any hiragana after a kanji, without breaks_word()'s tail-length test. The
    marks a speaker's own punctuation puts at a real boundary outrank every guess below them.
    """
    head, tail = (head_text or "").strip(), (tail_text or "").strip()
    if not head or not tail:
        return True
    if tail[0] in NO_LINE_START:
        return False
    if head[-1] in SENTENCE_END:
        return True
    if len(head) < MIN_PIECE_CHARS or len(tail) < MIN_PIECE_CHARS:
        return False
    if head[-1] in CLAUSE_BREAK:
        return True
    if is_hiragana(tail[0]) and is_kanji(head[-1]):
        return False
    return tail[0] not in PARTICLE_START


def split_at_clause(buf, limits: CueLimits) -> tuple:
    """Back a hard break off to the last clause boundary inside the final 40% of the buffer (P1.2)."""
    total = len(word_text(buf))
    best = None
    for j in range(len(buf) - 1):
        text = word_text(buf[:j + 1])
        if text and len(text) >= total * limits.clause_ratio and text[-1] in (SENTENCE_END | CLAUSE_BREAK):
            best = j + 1
    return (buf[:best], buf[best:]) if best else (buf, [])


def split_for_break(buf, limits: CueLimits, next_word: str) -> tuple:
    """Where to cut a buffer that has run past the limits: the last clause boundary if it is a
    legal break, else the last position that is one. Emitting the whole buffer is itself a break,
    against the word that follows it, so that seam is checked too."""
    head, tail = split_at_clause(buf, limits)
    if tail:
        if may_break(word_text(head), word_text(tail)):
            return head, tail
    elif may_break(word_text(buf), next_word):
        return buf, []
    for j in range(len(buf) - 1, 0, -1):
        if may_break(word_text(buf[:j]), word_text(buf[j:])):
            return buf[:j], buf[j:]
    return buf, []


def group_words(words, speech, limits: CueLimits) -> list:
    """Cut a word list into cue-sized groups: sentence end, VAD pause, clause, then hard limits.

    Every cut asks may_break() first. Japanese speakers pause inside words - one second between 言
    and ってた in the sample - and VAD confirms the silence, so a pause alone is not a boundary.
    """
    groups: list = []
    buf: list = []

    def tail_text(start: int) -> str:
        tail: list = []
        tail_chars = 0
        for j in range(start, len(words)):
            candidate = words[j]
            tail.append(candidate)
            tail_chars += len((candidate.word or "").strip())
            if tail_chars >= MIN_PIECE_CHARS:
                break
        return word_text(tail)

    for i, w in enumerate(words):
        # may_break() needs enough of the prospective line to avoid treating a readable boundary
        # as a stub. Whisper's words are not display units, so the next word alone can be too short;
        # gather only the small prefix needed for that test rather than rebuilding the full suffix.
        next_text = tail_text(i)
        following_text = tail_text(i + 1)
        if buf:
            gap = w.start - words[i - 1].end
            silent = gap - interval_overlap(words[i - 1].end, w.start, speech)
            if gap >= limits.pause_split and silent >= limits.vad_silence \
                    and may_break(word_text(buf), next_text):
                groups.append(buf)
                buf = []
        buf.append(w)
        text = word_text(buf)
        if not text:
            continue
        if text[-1] in SENTENCE_END and may_break(text, following_text):
            groups.append(buf)
            buf = []
        elif text[-1] in CLAUSE_BREAK and len(text) >= limits.max_chars * limits.clause_ratio \
                and may_break(text, following_text):
            groups.append(buf)
            buf = []
        elif len(text) >= limits.max_chars or buf[-1].end - buf[0].start >= limits.max_seconds:
            head, buf = split_for_break(buf, limits, following_text)
            groups.append(head)
    if buf:
        groups.append(buf)
    return [g for g in groups if word_text(g) and not JUNK_RE.match(word_text(g))]


def merge_adjacent(cues, limits: CueLimits, max_gap: float, only_short: bool) -> list:
    """Fold neighbouring cues together while they stay inside the char and duration limits."""
    out: list = []
    for cue in cues:
        if out:
            prev = out[-1]
            gap = cue["start"] - prev["end"]
            short = (prev["end"] - prev["start"] < limits.min_seconds
                     or cue["end"] - cue["start"] < limits.min_seconds)
            if (gap <= max_gap and (short or not only_short)
                    and len(prev["text"]) + len(cue["text"]) <= limits.max_chars
                    and cue["end"] - prev["start"] <= limits.max_seconds):
                prev["end"] = cue["end"]
                prev["text"] = prev["text"] + cue["text"]
                continue
        out.append(dict(cue))
    return out


def seam_for(prev_text: str, gap: float, limits: CueLimits) -> str:
    """What joins two merged cues: "" inside one sentence, "\\n" where a viewer would see a new line.

    The newline is not decoration. `.shisuko-sub` is `white-space: pre-wrap`, so the overlay renders
    a second line; Yomitan ends its sentence at a newline, so a lookup in the first half yields the
    first half; and match.js's TERMINATORS splits on it, so cutFrom() scores the mined sentence
    exactly instead of falling back to coverage.
    """
    if prev_text and prev_text[-1] in SENTENCE_END:
        return "\n"
    return "\n" if gap >= limits.seam_gap else ""


def merge_segments(cues, limits: CueLimits) -> list:
    """Merge neighbouring cues across Whisper segment boundaries.

    build_cues() runs once per segment, so merge_adjacent() only ever sees one segment's cues -
    and in a two-person conversation 94% of neighbouring cues come from different segments, which
    left the anti-flicker rule dead code and the median cue seven characters long. Three jobs the
    within-segment merge never had: a seam Whisper cut inside a word (思 || っております) closes
    whatever the budget says, since the break would otherwise survive into the overlay; a cue too
    short to read reaches `cross_reach` instead of `merge_gap` for a partner; and the halves are
    joined by seam_for().
    """
    out: list = []
    for cue in cues:
        if out:
            prev = out[-1]
            gap = cue["start"] - prev["end"]
            forced = breaks_word(prev["text"].split("\n")[-1], cue["text"].split("\n")[0])
            short = min(len(prev["text"]), len(cue["text"])) <= limits.reach_chars
            seam = "" if forced else seam_for(prev["text"], gap, limits)
            text = prev["text"] + seam + cue["text"]
            # The line break is the first thing to give up. A row nobody can read, or a third row,
            # is worse than no break at all, and refusing the merge over one leaves the stub alone
            # on screen - which is how 言ってた ended up a four-character cue of its own.
            if seam == "\n":
                rows = text.split("\n")
                if len(rows) > limits.max_lines or min(len(x) for x in rows) < MIN_PIECE_CHARS:
                    text = prev["text"] + cue["text"]
            lines = text.split("\n")
            fits = (len(text) - text.count("\n") <= limits.max_chars
                    and len(lines) <= limits.max_lines
                    and max(len(x) for x in lines) <= limits.max_chars
                    and cue["end"] - prev["start"] <= limits.max_seconds)
            budget = limits.cross_reach if short else limits.merge_gap
            if ((gap <= budget and fits)
                    or (forced and gap <= limits.cross_reach
                        and cue["end"] - prev["start"] <= limits.cross_ceiling
                        and len(text) - text.count("\n") <= limits.cross_chars)):
                prev["end"] = cue["end"]
                prev["text"] = text
                prev.setdefault("_merged", [prev["seg"]]).extend(
                    cue.get("_merged", [cue["seg"]]))
                continue
        out.append(dict(cue))
    # `seg` ties a cue to the sentence mining rejoins (sentenceForCue in content.js). A merged cue
    # swallowed other segments' cues, so every cue still carrying one of those ids has to follow it
    # here, or a leftover fragment would rejoin into half a sentence.
    rename: dict = {}
    for cue in out:
        for old in cue.pop("_merged", [])[1:]:
            if old != cue["seg"]:
                rename.setdefault(old, cue["seg"])
    for cue in out:
        seen = set()
        while cue["seg"] in rename and cue["seg"] not in seen:
            seen.add(cue["seg"])
            cue["seg"] = rename[cue["seg"]]
    return out


def normalise_gaps(cues, limits: CueLimits) -> list:
    """Close gaps that are long enough to see but too short to read as deliberate (P1.7)."""
    for prev, nxt in zip(cues, cues[1:]):
        gap = nxt["start"] - prev["end"]
        if gap < 0:
            # Overlapping cues beat a 50 ms flash, so an overlap only closes when the earlier cue
            # stays readable afterwards.
            trimmed = nxt["start"] - limits.min_gap
            if trimmed - prev["start"] >= limits.hard_min_seconds:
                prev["end"] = trimmed
        elif limits.min_gap < gap < limits.dead_zone:
            prev["end"] = nxt["start"] - limits.min_gap
    return cues


def build_cues(words, speech, limits: CueLimits) -> list:
    """One Whisper segment's words -> display-ready cues [{start, end, text}] (P1 rules 1-7)."""
    words = trim_words(words, speech, limits.trim_slack)
    cues = []
    for group in group_words(words, speech, limits):
        start = group[0].start
        onset = nearest_onset(speech, start, limits.snap_reach)
        if onset is not None:  # P1.3: cue in at the speech onset, not at Whisper's first word
            start = min(max(start, onset - limits.lead_in), onset + 0.30)
        cues.append({"start": start, "end": max(group[-1].end, start + 0.01), "text": word_text(group)})

    for i, cue in enumerate(cues):
        ceiling = cues[i + 1]["start"] - limits.min_gap if i + 1 < len(cues) else float("inf")
        sil_start, sil_end = next_silence(cue["end"], speech)
        silence = sil_end - sil_start
        if silence >= limits.lead_out_silence:  # P1.4: cue out a beat after the audio
            cue["end"] += min(limits.lead_out, silence - 0.1)
        if cue["end"] - cue["start"] < limits.min_seconds:  # P1.5: grow into the trailing silence
            cue["end"] = min(max(cue["end"], cue["start"] + limits.min_seconds), sil_end - limits.min_gap)
        cue["end"] = min(cue["end"], max(ceiling, cue["start"] + limits.hard_min_seconds))

    cues = merge_adjacent(cues, limits, limits.merge_reach, only_short=True)   # P1.5 merge
    cues = merge_adjacent(cues, limits, limits.merge_gap, only_short=False)    # P1.6 anti-flicker
    for cue in cues:
        if cue["end"] - cue["start"] < limits.hard_min_seconds:
            cue["end"] = cue["start"] + limits.hard_min_seconds
    return normalise_gaps(cues, limits)


def cue_overlaps(a, b, share: float = 0.5) -> bool:
    """True when two cues share more than `share` of the shorter one (P1.8 dedup)."""
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    return shorter > 0 and inter > share * shorter


def stranded_head(words) -> int:
    """How many of a segment's first words are the anchoring artifact, judged on timing alone.

    repair_lead_words() decides the same head with the detector's intervals as evidence. A lyrics
    window has none, so this is its first two tests only: a head of at most LEAD_REPAIR_CHARS
    characters followed by a gap of at least LEAD_REPAIR_GAP. The loop there breaks on a gap under
    the threshold at the first word, so it is one word or none.
    """
    if len(words) < 2:
        return 0
    head, nxt = words[0], words[1]
    if nxt.start - head.end >= LEAD_REPAIR_GAP and len(word_text([head])) <= LEAD_REPAIR_CHARS:
        return 1
    return 0


def lyrics_spans(segs, offset: float, limits: Optional[CueLimits] = None) -> list:
    """Speech intervals for a lyrics window, made of the segments it keeps: absolute seconds, merged.

    A sung window has no detector intervals to build cues on, so the segments that pass
    lyrics_reason stand in for them, in the detector's own shape: a segment's words are cut into
    runs at every gap of at least `pause_split`, and every run is padded by speech_pad_ms on both
    sides, as Silero pads what it hears. A whole, unpadded segment span hid every breath from the
    cue builder: the lead-out ate a breath under 0.8 s down to 0.1 s, merge_segments() then glued
    the two lines into one row, and a pause inside a segment (two lines in one, the usual shape
    without the VAD filter) never split, since none of it counted as silence. A stranded first
    word (the anchoring artifact of repair_lead_words(), stranded_head()) starts no run: a span
    over it merged with the previous line's, the repair found the head in its interval already and
    moved nothing, and the line came on screen seconds early. Its run starts a pad before the next
    word, so the repair slides the head onto that onset. process() stores the spans as the window's
    speech as well, so the sync's speech list and the cache carry the sung lines (/clip never
    reads Session.speech: it slices the audio by the times the client sends).
    """
    pause_split = (limits or CueLimits()).pause_split
    pad = VAD_PARAMS["speech_pad_ms"] / 1000.0
    spans = []
    for seg in segs:
        words = absolute_words(seg, offset)
        if lyrics_reason(seg, words) is not None:
            continue
        words = words[stranded_head(words):]
        run = [words[0]]
        for prev, w in zip(words, words[1:]):
            if w.start - prev.end >= pause_split:
                spans.append([max(0.0, run[0].start - pad), run[-1].end + pad])
                run = []
            run.append(w)
        spans.append([max(0.0, run[0].start - pad), run[-1].end + pad])
    return merge_intervals(spans)


def build_window_cues(segs, offset: float, speech, limits: CueLimits, seg_id: int, drops=None,
                      window_end: Optional[float] = None, lyrics: bool = False) -> tuple:
    """Gate hallucinated segments, build their cues and stamp each with its segment id.

    `seg` ties every cue back to the Whisper segment it came from, which is a run of speech and
    not a sentence: mining reads the cue alone (see sentenceForCue in content.js). Kept for the
    cache tools, which measure a change per segment. Returns (cues, next segment id). A lyrics
    window was transcribed without the detector: its segments go through lyrics_reason instead,
    and `speech` is what lyrics_spans() made of them (their word runs, padded like the detector's
    intervals and starting past a stranded head, so that the lead repair below slides it onto its
    line as it does on the talk path).
    """
    out: list = []
    for seg in segs:
        # Before the gates, not after: an unrepaired first word makes the segment's span cover a
        # silence it never contained, and the VAD gate then deletes real speech (20 utterances in
        # 17 minutes of the sample, キズナアイでーす and はじめまして! among them) while the anomaly
        # gate scores its six-second "word" straight past the threshold.
        words = repair_lead_words(absolute_words(seg, offset), speech)
        reason = lyrics_reason(seg, words) if lyrics else hallucination_reason(seg, words, speech)
        if reason:
            if drops is not None:
                drops[reason] = drops.get(reason, 0) + 1
                drops.setdefault("_text", []).append((reason, (getattr(seg, "text", "") or "").strip()))
            continue
        cues = build_cues(words, speech, limits)
        if not cues:
            if drops is not None:
                drops["trimmed"] = drops.get("trimmed", 0) + 1
            continue
        for cue in cues:
            cue["seg"] = seg_id
        out += cues
        seg_id += 1
    if window_end is not None:
        # This window owns [offset, window_end); the next one re-transcribes from there, so a
        # lead-out reaching past it would overlap a cue that does not exist yet.
        out = [c for c in out if c["start"] < window_end - 0.05]
        for cue in out:
            cue["end"] = min(cue["end"], max(window_end, cue["start"] + limits.hard_min_seconds))
    # Merge before normalise_gaps, never after: closing every gap to min_gap first would hide the
    # pause the seam is judged on and make every neighbour look adjacent.
    out = merge_segments(sorted(out, key=lambda c: c["start"]), limits)
    for cue in out:
        cue["start"] = round(cue["start"], 2)
        cue["end"] = round(max(cue["end"], cue["start"] + 0.05), 2)
    return normalise_gaps(out, limits), seg_id


# --------------------------------------------------------------------------- sessions

@dataclass
class Session:
    video_id: str
    url: str
    # Identifies this in-memory session; changes when the server starts over for a video, so
    # clients can tell that cue ids restarted from zero and drop what they had.
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"  # pending | downloading | decoding | ready | error | evicted
    error: Optional[str] = None
    title: str = ""
    duration: float = 0.0
    duration_hint: float = 0.0  # length reported by yt-dlp, used before the container is opened
    audio: Optional[np.ndarray] = None
    # (offset_seconds, samples): a short decode around the playhead that lets transcription start
    # before the whole track has been decoded. Dropped once `audio` holds everything.
    preview: Optional[tuple] = None
    # A live stream keeps its audio here instead: the decoded segments around the playhead, on the
    # stream's media clock (the same one the player reports), without a beginning or an end.
    live: bool = False
    live_audio: Optional["LiveAudio"] = None
    cues: list = field(default_factory=list)
    covered: list = field(default_factory=list)
    speech: list = field(default_factory=list)  # merged Silero intervals, absolute seconds
    # Language watch (see language_vote): speech heard in another language since the target
    # language was last heard, which language that was, and whether cues are paused because of it.
    foreign_seconds: float = 0.0
    heard: Optional[str] = None
    language_paused: bool = False
    # Windows a paused session has only listened to. Kept apart from `covered` and never written
    # to the cache: when the language comes back they are forgotten, so a wrong pause costs a
    # second listen instead of leaving the video permanently blank.
    probed: list = field(default_factory=list)
    seg_next: int = 0  # next Whisper-segment id; cues of one segment share it (see build_window_cues)
    want_t: float = 0.0
    last_sync: float = field(default_factory=time.time)
    busy: Optional[list] = None
    fetching: bool = False
    error_at: float = 0.0  # time.time() of the last failed fetch; drives the automatic retry
    lock: threading.RLock = field(default_factory=threading.RLock)

    def cache_path(self) -> Path:
        return CACHE_DIR / f"{self.video_id}.cues.json"

    def model_cache_path(self, model) -> Path:
        """Where the cues of `model` are kept while another model owns cache_path() (see save_cache)."""
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(model))
        return CACHE_DIR / f"{self.video_id}.{slug}.cues.json"

    def fully_covered(self) -> bool:
        return (
            self.duration > 0
            and len(self.covered) == 1
            and self.covered[0][0] <= 0.3
            and self.covered[0][1] >= self.duration - 0.3
        )


class LiveAudio:
    """Decoded 16 kHz audio of a live stream: one chunk per fetched segment, on the stream's clock.

    Chunks are kept sorted by start; segments arrive in order from the follower but a seek can add
    earlier ones. Everything older than the playhead's neighbourhood is trimmed away by the caller.
    """

    def __init__(self):
        self.chunks: list = []  # [start_seconds, float32 samples], sorted by start

    def add(self, start: float, samples: np.ndarray) -> None:
        if len(samples) == 0 or any(abs(c[0] - start) < 0.01 for c in self.chunks):
            return
        self.chunks.append([float(start), samples])
        self.chunks.sort(key=lambda c: c[0])

    def available(self) -> list:
        return merge_intervals([[c[0], c[0] + len(c[1]) / SAMPLE_RATE] for c in self.chunks])

    def end(self) -> float:
        return max((c[0] + len(c[1]) / SAMPLE_RATE for c in self.chunks), default=0.0)

    def trim(self, before: float) -> None:
        self.chunks = [c for c in self.chunks if c[0] + len(c[1]) / SAMPLE_RATE >= before]

    def slice(self, start: float, end: float) -> Optional[np.ndarray]:
        """Samples for [start, end), or None unless the whole range has been fetched."""
        have = find_covering(self.available(), start, tol=0.0)
        if have is None or end <= start or end > have[1] + 0.01:
            return None
        n = int(round((end - start) * SAMPLE_RATE))
        out = np.zeros(n, dtype=np.float32)
        for c_start, samples in self.chunks:
            offset = int(round((c_start - start) * SAMPLE_RATE))
            a, b = max(0, offset), min(n, offset + len(samples))
            if b > a:
                out[a:b] = samples[a - offset: b - offset]
        return out


def audio_slice(s: Session, start: float, end: float) -> Optional[np.ndarray]:
    """Samples for [start, end): from the full decode, or from the preview while that is all there is."""
    if s.live_audio is not None:
        return s.live_audio.slice(start, end)
    if s.audio is not None:
        return s.audio[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
    if s.preview is None:
        return None
    offset, samples = s.preview
    a = int(round((start - offset) * SAMPLE_RATE))
    b = int(round((end - offset) * SAMPLE_RATE))
    if a < 0 or b > len(samples) or b <= a:
        return None
    return samples[a:b]


LIVE_MIN_WINDOW = 8.0  # seconds of new audio at the live edge before it is worth a Whisper call


def plan_live_window(s: Session, args) -> Optional[tuple]:
    """plan_window for a live stream: the same rules, inside the audio fetched so far.

    The live edge is not the end of the video: a short window there is left to grow instead of
    being transcribed as a sliver or marked covered.
    """
    if s.status != "ready" or s.live_audio is None:
        return None
    avail = s.live_audio.available()
    if not avail:
        return None
    t = max(0.0, s.want_t)
    ranges = planned_ranges(s)
    cov = find_covering(ranges, t)
    if cov is None:
        start = max(0.0, t - 0.5)
        size = args.first_window
    else:
        start = cov[1]
        lookahead = lookahead_for(s, args)
        if lookahead > 0 and start - t > lookahead:
            return None
        size = args.window
    have = find_covering(avail, start, tol=0.0)
    if have is None:
        return None  # the follower has not fetched this part (yet)
    end = min(start + size, have[1])
    nxt = next_start_after(ranges, start + 0.01)
    if nxt is not None:
        end = min(end, nxt)
    at_edge = end >= have[1] - 0.01
    if end - start < 1.5:
        if not at_edge:
            if s.language_paused:
                s.probed = merge_intervals(s.probed + [[start, end]])
            else:
                s.covered = merge_intervals(s.covered + [[start, end]])
        return None
    if at_edge and end - start < LIVE_MIN_WINDOW:
        return None
    return (start, end)


def planned_ranges(s: Session) -> list:
    """Where the planner must not send another window: what has been transcribed, plus what a
    paused session has already listened to."""
    return merge_intervals(s.covered + s.probed) if s.probed else s.covered


def lookahead_for(s: Session, args) -> float:
    """How far ahead of the playhead to plan: --lookahead, or a short way while the language watch
    has paused the video, where every window is a probe and the GPU should otherwise sit idle."""
    if not s.language_paused:
        return args.lookahead
    return min(args.lookahead, LANGUAGE_PROBE_AHEAD) if args.lookahead > 0 else LANGUAGE_PROBE_AHEAD


def plan_window(s: Session, args) -> Optional[tuple]:
    """Pick the next [start, end) window to transcribe for a session, or None if idle."""
    if s.live_audio is not None:
        return plan_live_window(s, args)
    if s.status != "ready" or (s.audio is None and s.preview is None) or s.duration <= 0:
        return None
    t = min(max(0.0, s.want_t), s.duration)
    ranges = planned_ranges(s)
    cov = find_covering(ranges, t)
    if cov is None:
        start = max(0.0, t - 0.5)
        size = args.first_window
    else:
        start = cov[1]
        if start >= s.duration - 0.05:
            return None
        lookahead = lookahead_for(s, args)
        if lookahead > 0 and start - t > lookahead:
            return None
        size = args.window
    end = min(start + size, s.duration)
    nxt = next_start_after(ranges, start + 0.01)
    if nxt is not None:
        end = min(end, nxt)
    if s.audio is None:
        # Only the preview exists: stay inside it, and leave anything outside for the full decode.
        offset, samples = s.preview
        preview_end = offset + len(samples) / SAMPLE_RATE
        if start < offset or start >= preview_end:
            return None
        end = min(end, preview_end)
        return (start, end) if end - start >= 1.5 else None
    if end - start < 1.5:
        if s.language_paused:
            s.probed = merge_intervals(s.probed + [[start, end]])
        else:
            s.covered = merge_intervals(s.covered + [[start, end]])
        return None
    return (start, end)


# --------------------------------------------------------------------------- audio fetching

class YtdlpLogger:
    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


def friendly_error(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube asks for a sign-in. Restart the server with --cookies-from-browser firefox (or --cookies /data/cookies.txt in Docker)"
    if "private video" in low:
        return "This video is private"
    if "members-only" in low or "join this channel" in low:
        return "Members-only video. Restart the server with --cookies-from-browser firefox"
    if "javascript runtime" in low:
        return "yt-dlp needs Node.js or Deno installed to download from YouTube"
    if "video unavailable" in low:
        return "Video unavailable"
    if "live stream" in low:
        return msg
    last = msg.strip().splitlines()[-1] if msg.strip() else msg
    return last[:200]


def find_cached_audio(video_id: str) -> Optional[Path]:
    for p in CACHE_DIR.glob(f"{video_id}.*"):
        if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def discard_partial_downloads(video_id: str) -> int:
    """Remove yt-dlp's leftover .part/.ytdl files so the next download starts from byte 0."""
    removed = 0
    for p in CACHE_DIR.glob(f"{video_id}.*"):
        if p.suffix.lower() in PARTIAL_SUFFIXES and p.is_file():
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                log.debug("[%s] could not remove %s: %s", video_id, p.name, exc)
    return removed


def is_range_error(exc: BaseException) -> bool:
    """True for yt-dlp's "HTTP Error 416: Requested range not satisfiable".

    It means yt-dlp resumed a stale .part file past the end of what YouTube serves now (a
    different format, or a part that already held the whole file); resuming can never succeed.
    """
    text = str(exc)
    return "416" in text and "range" in text.lower()


def probe_duration(path: Path) -> Optional[float]:
    """Length of an audio file from its container header, available long before it is decoded."""
    import av

    try:
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            if stream.duration:
                return float(stream.duration * stream.time_base)
            if container.duration:
                return float(container.duration) / av.time_base
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read the duration of %s: %s", path, exc)
    return None


# The preview can start before the download finishes: a prefix of a WebM/Opus (or m4a with a
# leading moov atom) file decodes up to where the data stops.
EARLY_PREVIEW_MAX_START = 60.0        # past the first minute the prefix does not hold what the viewer needs
EARLY_PREVIEW_MIN_BYTES = 256 * 1024  # enough for the container header and the first clusters
EARLY_PREVIEW_MARGIN = 5.0            # seconds of slack on top of the preview range
DEFAULT_AUDIO_BITRATE = 160.0         # kbit/s assumed when yt-dlp reports none


def stream_bytes_per_second(hook_data: dict, fallback_abr: float) -> float:
    """Download bytes per second of audio, from the average bitrate yt-dlp reports."""
    abr = (hook_data.get("info_dict") or {}).get("abr")
    if not isinstance(abr, (int, float)) or abr <= 0:
        abr = fallback_abr if fallback_abr > 0 else DEFAULT_AUDIO_BITRATE
    return float(abr) * 1000.0 / 8.0


class Fetcher:
    def __init__(self, args):
        self.args = args

    def js_runtimes(self) -> dict:
        spec = (self.args.js_runtime or "auto").strip()
        if spec and spec != "auto":
            name, _, path = spec.partition(":")
            return {name.strip().lower(): ({"path": path.strip()} if path.strip() else {})}
        runtimes = {}
        for name in ("deno", "node", "bun"):
            if shutil.which(name):
                runtimes[name] = {}
        return runtimes or {"deno": {}}

    def ytdlp_options(self, video_id: str, progress_hook=None, resume: bool = True) -> dict:
        opts = {
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(CACHE_DIR / f"{video_id}.%(ext)s"),
            "quiet": True,
            "noprogress": True,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "logger": YtdlpLogger(),
            "js_runtimes": self.js_runtimes(),
            "continuedl": resume,  # False truncates a leftover .part instead of resuming it
        }
        if self.args.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.args.cookies_from_browser,)
        if self.args.cookies:
            opts["cookiefile"] = self.args.cookies
        if self.args.allow_remote_ejs:
            opts["remote_components"] = ["ejs:github"]
        if progress_hook is not None:
            opts["progress_hooks"] = [progress_hook]
        return opts

    def fetch(self, s: Session) -> None:
        try:
            with s.lock:
                s.status = "downloading"
                s.error = None
            path = find_cached_audio(s.video_id)
            if path is None:
                log.info("[%s] downloading audio", s.video_id)
                path = self.download(s)
                if path is None:
                    self.follow_live(s)  # returns when the stream ends or nobody watches any more
                    return
            else:
                log.info("[%s] using cached audio %s", s.video_id, path.name)
            with s.lock:
                if s.live:
                    # The stream ended and came back as a video: its clock starts over, so the cues
                    # made on the live clock are dropped, and the new token tells the client to follow.
                    s.live = False
                    s.live_audio = None
                    s.cues, s.covered, s.speech, s.seg_next = [], [], [], 0
                    s.probed, s.foreign_seconds, s.heard, s.language_paused = [], 0.0, None, False
                    s.token = uuid.uuid4().hex[:12]
                have_preview = s.preview is not None  # the download hook already published one
                if not have_preview:
                    s.status = "decoding"
            # Decoding a 40-minute track takes seconds; a few windows around the playhead take
            # milliseconds, so the first subtitles appear before the full decode finishes.
            if not have_preview:
                self.make_preview(s, path)
            from faster_whisper.audio import decode_audio

            audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
            audio = np.ascontiguousarray(audio, dtype=np.float32)
            with s.lock:
                s.audio = audio
                s.duration = float(len(audio)) / SAMPLE_RATE
                s.preview = None
                s.status = "ready"
            log.info("[%s] audio ready, %s long%s", s.video_id, fmt_time(s.duration), f": {s.title}" if s.title else "")
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] fetching audio failed: %s", s.video_id, exc)
            with s.lock:
                s.status = "error"
                s.error = friendly_error(exc)
                s.error_at = time.time()
                s.preview = None  # a preview from a partial download must not outlive the failure
        finally:
            with s.lock:
                s.fetching = False

    def preview_range(self, s: Session, duration: float) -> tuple:
        """The [start, end) seconds a preview should cover for the current playhead."""
        with s.lock:
            start = max(0.0, s.want_t - 1.0)
        return start, min(float(duration), start + self.args.first_window + self.args.window + 2.0)

    def make_preview(self, s: Session, path: Path) -> bool:
        """Decode a couple of windows around the playhead so the transcriber can start right away.

        `path` may be a partially downloaded file: decoding simply stops where the data does.
        """
        try:
            duration = probe_duration(path) or s.duration_hint
            if not duration or duration <= 0:
                return False  # unknown length: plan_window cannot work, so skip the preview entirely
            start, end = self.preview_range(s, duration)
            if end - start < 1.5:
                return False
            samples = _decode_range(path, start, end, rate=SAMPLE_RATE)
            if len(samples) < SAMPLE_RATE * 1.5:
                return False
            preview = np.ascontiguousarray(samples, dtype=np.float32) / 32768.0
            with s.lock:
                # The full decode may have landed while this ran, and a failed download must stay failed.
                if s.audio is not None or s.status == "error":
                    return False
                s.preview = (start, preview)
                s.duration = float(duration)
                s.status = "ready"
            log.info("[%s] preview %s-%s ready while the full audio decodes",
                     s.video_id, fmt_time(start), fmt_time(start + len(preview) / SAMPLE_RATE))
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] preview decode skipped: %s", s.video_id, exc)
            return False

    def progress_hook(self, s: Session, state: dict):
        """Watch the download and start the preview as soon as the file holds enough audio.

        Runs on yt-dlp's download thread, so it stays cheap, never raises and never blocks.
        """
        def hook(d):
            try:
                if state["fired"] or d.get("status") != "downloading":
                    return
                downloaded = d.get("downloaded_bytes") or 0
                tmp = d.get("tmpfilename") or d.get("filename")
                if not tmp or downloaded < EARLY_PREVIEW_MIN_BYTES:
                    return
                with s.lock:
                    want, duration = s.want_t, s.duration_hint
                if duration <= 0 or want > EARLY_PREVIEW_MAX_START:
                    return  # after a seek deep into the video the prefix holds the wrong audio
                _, end = self.preview_range(s, duration)
                if downloaded < (end + EARLY_PREVIEW_MARGIN) * stream_bytes_per_second(d, state["abr"]):
                    return
                state["fired"] = True
                threading.Thread(target=self.early_preview, args=(s, Path(tmp)), daemon=True,
                                 name=f"preview-{s.video_id}").start()
            except Exception as exc:  # noqa: BLE001
                log.debug("[%s] download progress hook failed: %s", s.video_id, exc)

        return hook

    def early_preview(self, s: Session, path: Path) -> None:
        """Preview decoded from the growing .part file, while yt-dlp is still downloading."""
        with s.lock:
            if s.preview is not None or s.audio is not None:
                return
        if self.make_preview(s, path):
            log.info("[%s] transcribing from the partial download", s.video_id)
        else:
            log.debug("[%s] the partial download did not decode yet; waiting for the full file", s.video_id)

    def download(self, s: Session, resume: bool = True) -> Optional[Path]:
        """Download the audio track; None for a live stream, which has no track to download.

        A leftover .part file is resumed first. When YouTube refuses the range (416), the file
        is stale and is thrown away, and the download runs once more from the start.
        """
        try:
            return self.download_once(s, resume)
        except Exception as exc:  # noqa: BLE001
            if not resume or not is_range_error(exc):
                raise
        log.info("[%s] the leftover partial download cannot be resumed; starting over", s.video_id)
        discard_partial_downloads(s.video_id)
        return self.download_once(s, resume=False)

    def download_once(self, s: Session, resume: bool) -> Optional[Path]:
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={s.video_id}"
        state = {"fired": False, "abr": 0.0}  # shared with the progress hook below
        with yt_dlp.YoutubeDL(self.ytdlp_options(s.video_id, self.progress_hook(s, state), resume)) as ydl:
            info = ydl.extract_info(url, download=False)
            hint, abr = info.get("duration"), info.get("abr")
            state["abr"] = float(abr) if isinstance(abr, (int, float)) else 0.0
            with s.lock:
                s.title = info.get("title") or ""
                s.duration_hint = float(hint) if isinstance(hint, (int, float)) else 0.0
            if info.get("is_live"):
                return None
            try:
                ydl.process_ie_result(info, download=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("[%s] process_ie_result failed (%s); retrying with a plain download", s.video_id, exc)
                if find_cached_audio(s.video_id) is None:
                    ydl.download([url])
        path = find_cached_audio(s.video_id)
        if path is None:
            raise RuntimeError("yt-dlp finished but no audio file was produced")
        return path

    def follow_live(self, s: Session) -> None:
        log.info("[%s] live stream: following the audio segments%s", s.video_id, f": {s.title}" if s.title else "")
        source = DashLiveSource(self, s.video_id)
        source.refresh()
        LiveFollower(s, source, self.args).run()


# --------------------------------------------------------------------------- live streams

# YouTube serves a live stream as numbered DASH segments (…&sq=N), each a self-contained fMP4 whose
# timestamps are the stream's media clock: the clock the player's getProgressState().current runs
# on, so no conversion is needed between what the extension reports and what is transcribed.
LIVE_KEEP_BEHIND = 900.0        # seconds of audio kept behind the playhead, for seeking back and clips
LIVE_START_BEHIND = 8           # segments behind the live head to start at while the playhead is unknown
LIVE_HEAD_POLL = 1.0            # seconds between head checks once the follower has caught up
LIVE_MAX_ERRORS = 12            # consecutive failed segment fetches before the session errors out
LIVE_PREFERRED_ITAGS = ("140", "141", "139", "251", "250", "249")


class LiveEnded(Exception):
    """The stream is over: yt-dlp no longer reports it as live."""


def decode_segment(data: bytes) -> tuple:
    """(start_seconds, float32 samples at SAMPLE_RATE) of one self-contained DASH segment."""
    import av

    chunks = []
    start = None
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            if start is None:
                start = float(frame.pts * stream.time_base)
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray()[0])
        for rf in resampler.resample(None):
            chunks.append(rf.to_ndarray()[0])
    if start is None or not chunks:
        raise RuntimeError("segment holds no audio")
    return start, np.concatenate(chunks).astype(np.float32) / 32768.0


class DashLiveSource:
    """The segment URLs of a live stream's audio, refreshed through yt-dlp when they expire."""

    def __init__(self, fetcher: "Fetcher", video_id: str):
        self.fetcher = fetcher
        self.video_id = video_id
        self.base_url = ""
        self.seg_seconds = 5.0
        self.last_head: Optional[int] = None
        self.refreshed_at = 0.0

    def refresh(self) -> None:
        import yt_dlp

        opts = dict(self.fetcher.ytdlp_options(self.video_id), live_from_start=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={self.video_id}", download=False)
        if not info.get("is_live"):
            raise LiveEnded("The live stream has ended")
        fmts = [f for f in info.get("formats") or [] if f.get("vcodec") == "none" and f.get("url") and f.get("is_from_start")]
        fmts.sort(key=lambda f: (LIVE_PREFERRED_ITAGS.index(str(f.get("format_id"))) if str(f.get("format_id")) in LIVE_PREFERRED_ITAGS else 99))
        if not fmts:
            raise RuntimeError("This live stream offers no audio segments (DVR may be disabled)")
        chosen = fmts[0]
        self.base_url = chosen["url"]
        seg = chosen.get("target_duration")
        self.seg_seconds = float(seg) if isinstance(seg, (int, float)) and seg > 0 else 5.0
        self.refreshed_at = time.time()
        log.info("[%s] live audio format %s, %.0f s segments", self.video_id, chosen.get("format_id"), self.seg_seconds)

    def _request(self, url: str, method: str = "GET") -> tuple:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                head = res.headers.get("X-Head-Seqnum")
                if head is not None:
                    self.last_head = int(head)
                return res.read() if method == "GET" else b"", res.status
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and time.time() - self.refreshed_at > 60:
                self.refresh()  # the URL expired; the caller retries with the new one
            raise

    def head(self) -> int:
        self._request(self.base_url, method="HEAD")
        if self.last_head is None:
            raise RuntimeError("no X-Head-Seqnum header on the live stream")
        return self.last_head

    def segment(self, seq: int) -> tuple:
        data, _ = self._request(f"{self.base_url}&sq={seq}")
        return decode_segment(data)


class LiveFollower:
    """Keeps a live session's audio buffer filled around the playhead.

    Runs on the fetch thread until the stream ends, the viewer leaves for --idle-minutes, or too
    many fetches fail in a row. `source` provides head() and segment(seq); tests pass a fake.
    """

    def __init__(self, s: Session, source, args, sleep=time.sleep, clock=time.time):
        self.s = s
        self.source = source
        self.args = args
        self.sleep = sleep
        self.clock = clock

    def run(self) -> None:
        s = self.s
        buf = LiveAudio()
        with s.lock:
            s.live = True
            s.live_audio = buf
            s.audio = None
            s.preview = None
            s.status = "downloading"
        cursor: Optional[int] = None
        head: Optional[int] = None
        head_at = 0.0
        errors = 0
        idle_limit = float(getattr(self.args, "idle_minutes", 30)) * 60.0
        client_timeout = float(getattr(self.args, "client_timeout", 30.0))
        lookahead = float(getattr(self.args, "lookahead", 0.0))
        try:
            while True:
                with s.lock:
                    want = s.want_t
                    idle = self.clock() - s.last_sync
                    avail = buf.available()
                if idle > idle_limit:
                    with s.lock:
                        s.status = "evicted"
                        s.live_audio = None
                    log.info("[%s] released the live audio after %d idle minutes", s.video_id, int(idle_limit // 60))
                    return
                if client_timeout > 0 and idle > client_timeout:
                    self.sleep(1.0)  # nobody is watching: leave the segments where they are
                    continue
                dur = self.source.seg_seconds
                if head is None or (cursor is not None and cursor > head and self.clock() - head_at >= LIVE_HEAD_POLL):
                    head = self.source.head()
                    head_at = self.clock()
                cursor = self.place_cursor(cursor, want, avail, head, dur)
                if cursor > head:
                    self.sleep(0.5)
                    continue
                if lookahead > 0 and cursor * dur > want + lookahead and want > 0:
                    self.sleep(1.0)
                    continue
                try:
                    start, samples = self.source.segment(cursor)
                except LiveEnded:
                    raise
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    log.warning("[%s] live segment %d failed (%s)", s.video_id, cursor, exc)
                    if errors >= LIVE_MAX_ERRORS:
                        self.source.refresh()  # raises LiveEnded once the stream is over
                        raise RuntimeError("The live stream's audio could not be fetched") from exc
                    self.sleep(min(5.0, 1.0 * errors))
                    continue
                errors = 0
                head = max(head, self.source.last_head or head)
                with s.lock:
                    buf.add(start, samples)
                    buf.trim(max(want, start) - LIVE_KEEP_BEHIND)
                    s.duration = buf.end()
                    if s.status == "downloading":
                        s.status = "ready"
                        log.info("[%s] live audio from %s, %d s behind the head", s.video_id, fmt_time(start), int((head - cursor) * dur))
                cursor += 1
        except LiveEnded as exc:
            with s.lock:
                s.status = "error"
                s.error = str(exc)
                s.error_at = self.clock()
                s.live_audio = None
            log.info("[%s] %s", s.video_id, exc)

    @staticmethod
    def place_cursor(cursor: Optional[int], want: float, avail: list, head: int, dur: float) -> int:
        """The next segment to fetch: the current run, or a fresh start near the playhead after a seek.

        Segment N holds roughly [N*dur, (N+1)*dur) of the clock, but the exact offset differs from
        stream to stream, so a fresh start begins two segments early to be sure to reach the playhead.
        """
        if want <= 0:
            return cursor if cursor is not None else max(0, head - LIVE_START_BEHIND)
        if cursor is not None:
            near = find_covering(avail, want, tol=2 * dur) is not None
            heading_there = (cursor - 2) * dur <= want <= (cursor + 2) * dur
            if near or heading_there:
                return cursor
        return max(0, int(want // dur) - 2)


# --------------------------------------------------------------------------- audio clips (sentence mining)

CLIP_RATE = 48000
MAX_CLIP_SECONDS = 60.0


class ClipNotReady(Exception):
    """Raised when the audio for a video has not been fetched yet."""


def _wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(samples, dtype=np.int16).tobytes())
    return buf.getvalue()


def _mp3_bytes(samples: np.ndarray, rate: int, bit_rate: int = 96000) -> bytes:
    import av

    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mp3") as out:
        stream = out.add_stream("libmp3lame", rate=rate, layout="mono", format="s16p")
        stream.bit_rate = bit_rate
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples, dtype=np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        frame.pts = 0
        resampler = av.AudioResampler(format="s16p", layout="mono", rate=rate)
        for f in list(resampler.resample(frame)) + list(resampler.resample(None)):
            for packet in stream.encode(f):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


def _decode_range(src: Path, start: float, end: float, rate: int = CLIP_RATE) -> np.ndarray:
    """Decode [start, end) seconds of an audio file as int16 mono at `rate` (PyAV, no ffmpeg binary needed)."""
    import av

    chunks = []
    with av.open(str(src)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        try:
            container.seek(int(max(0.0, start - 2.0) / float(stream.time_base)), stream=stream, backward=True, any_frame=False)
        except Exception:  # noqa: BLE001
            pass
        state = {"t0": None, "pos": 0}

        def take(rf):
            arr = rf.to_ndarray()
            n = arr.shape[-1]
            t_chunk = state["t0"] + state["pos"] / rate
            i0 = int(max(0, round((start - t_chunk) * rate)))
            i1 = int(min(n, round((end - t_chunk) * rate)))
            if i1 > i0:
                chunks.append(arr[0, i0:i1])
            state["pos"] += n

        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            if state["t0"] is None:
                state["t0"] = float(frame.pts * stream.time_base)
            for rf in resampler.resample(frame):
                take(rf)
            if state["t0"] + state["pos"] / rate >= end + 0.1:
                break
        if state["t0"] is not None:
            for rf in resampler.resample(None):
                take(rf)
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks)


def make_clip(video_id: str, start: float, end: float, fmt: str, fallback_audio: Optional[np.ndarray]):
    """Return (bytes, mime, extension) for the [start, end) audio clip of a video."""
    samples = None
    rate = CLIP_RATE
    src = find_cached_audio(video_id)
    if src is not None:
        try:
            samples = _decode_range(src, start, end, rate)
            if len(samples) < rate * 0.2:
                samples = None
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] clip decode from the source file failed (%s); using the 16 kHz copy", video_id, exc)
    if samples is None and fallback_audio is not None:
        a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
        return encode_clip(fallback_audio[a:b], fmt)
    if samples is None or len(samples) == 0:
        raise RuntimeError("no audio available for this range")
    return _encode_clip(samples, rate, fmt)


def encode_clip(samples: np.ndarray, fmt: str):
    """Clip bytes from float32 samples at SAMPLE_RATE (the decoded copy, or a live stream's buffer)."""
    if len(samples) == 0:
        raise RuntimeError("no audio available for this range")
    return _encode_clip(np.clip(samples * 32767.0, -32768, 32767).astype(np.int16), SAMPLE_RATE, fmt)


def _encode_clip(samples: np.ndarray, rate: int, fmt: str):
    if fmt == "mp3":
        try:
            return _mp3_bytes(samples, rate), "audio/mpeg", "mp3"
        except Exception as exc:  # noqa: BLE001
            log.warning("mp3 encoding unavailable (%s); returning wav", exc)
    return _wav_bytes(samples, rate), "audio/wav", "wav"


# --------------------------------------------------------------------------- transcription

def speech_samples(audio, speech, start: float, limit: float = LANGUAGE_DETECT_SECONDS):
    """The speech parts of one window, concatenated, at most `limit` seconds of them."""
    cap = int(limit * SAMPLE_RATE)
    parts, total = [], 0
    for a, b in speech:
        i = max(0, int((a - start) * SAMPLE_RATE))
        j = min(len(audio), int((b - start) * SAMPLE_RATE), i + cap - total)
        if j <= i:
            continue
        parts.append(audio[i:j])
        total += j - i
        if total >= cap:
            break
    return np.concatenate(parts) if parts else audio[:0]


def speech_seconds(speech, start: float, end: float) -> float:
    """Seconds of detected speech inside [start, end)."""
    return sum(max(0.0, min(b, end) - max(a, start)) for a, b in speech)


def rms(audio) -> float:
    """Root mean square of a window's samples (1.0 is full scale); 0.0 for an empty one."""
    if len(audio) == 0:
        return 0.0
    return math.sqrt(float(np.mean(np.square(audio, dtype=np.float64))))


def wants_lyrics(args, audio, speech, start: float, end: float) -> bool:
    """Whether a window is transcribed without the detector: --lyrics auto, next to no speech heard, not silence."""
    if getattr(args, "lyrics", "auto") != "auto":
        return False
    return speech_seconds(speech, start, end) < LYRICS_MAX_SPEECH_S and rms(audio) >= LYRICS_MIN_RMS


def unsung_stretches(audio, start: float, end: float, speech, cues,
                     min_seconds: float = LYRICS_MIN_STRETCH_S) -> list:
    """The parts of a talk window, at least `min_seconds` long, that the detector heard nothing in
    and that are not silence: what a song shares a window with the line announcing it.

    wants_lyrics() judges a window whole, so a 歌枠's MC line (a second of detected speech) sends
    the window the detector's way, faster-whisper decodes only that line, and the thirty seconds of
    singing after it are covered with nothing in them. process() leaves these stretches out of the
    covered range instead; the planner brings each back as a window of its own, where the rule sees
    next to no speech and judges it alone. `audio` is the window's samples from `start`.
    """
    out = []
    for a, b in unheard_stretches([[start, end]], speech, cues, min_seconds):
        samples = audio[max(0, int((a - start) * SAMPLE_RATE)):max(0, int((b - start) * SAMPLE_RATE))]
        if rms(samples) >= LYRICS_MIN_RMS:
            out.append([a, b])
    return out


def language_vote(s: Session, heard: Optional[str], seconds: float, target: str, patience: float) -> bool:
    """Fold one window's language detection into the session; True if the window is worth transcribing.

    Below the patience a foreign vote is still transcribed. The language is forced anyway, so one
    misdetection (singing, noise, a line full of loanwords) must not cost a subtitle; only speech
    that stays foreign for `patience` seconds does. A paused session probes every planned window
    and resumes the moment the target language is heard again.
    """
    if patience <= 0:
        return True
    if heard is None:  # too little speech, or too unsure to count
        return not s.language_paused
    if heard == target:
        s.foreign_seconds = 0.0
        s.heard = None
        s.language_paused = False
        s.probed = []  # what was only listened to is offered to the planner again
        return True
    # Capped: past the patience the number decides nothing, and it is written to the cache.
    s.foreign_seconds = min(s.foreign_seconds + seconds, patience)
    s.heard = heard
    if s.foreign_seconds >= patience:
        s.language_paused = True
    return not s.language_paused


class Transcriber(threading.Thread):
    def __init__(self, app: "App"):
        super().__init__(daemon=True, name="transcriber")
        self.app = app

    def run(self) -> None:
        while True:
            try:
                if self.app.switch_model_if_wanted():
                    continue
                picked = self.app.pick_work()
                if picked is None:
                    time.sleep(0.15)
                    continue
                self.process(*picked)
            except Exception:  # noqa: BLE001
                log.exception("transcriber loop error")
                time.sleep(1.0)

    def detect_language(self, s: Session, audio, speech, start: float, end: float):
        """Judge this window: (code or None, probability, seconds of speech, whether the detector worked).

        The seconds are all the speech in the window; only the samples fed to the detector are
        capped, so the patience stays a count of seconds actually spoken.
        """
        seconds = speech_seconds(speech, start, end)
        samples = speech_samples(audio, speech, start)
        if len(samples) / SAMPLE_RATE < LANGUAGE_MIN_SPEECH:
            return None, 0.0, seconds, True  # no evidence either way, but nothing broke
        try:
            language, probability, _ = self.app.model.detect_language(audio=samples)
            probability = float(probability)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] language detection failed (%s); the window is transcribed unjudged", s.video_id, exc)
            return None, 0.0, seconds, False
        if not math.isfinite(probability) or probability < LANGUAGE_MIN_PROB:
            return None, 0.0, seconds, True
        return str(language), probability, seconds, True

    def watch_language(self, s: Session, audio, speech, start: float, end: float) -> bool:
        """Detect, vote, log. False means this window must not be transcribed."""
        args = self.app.args
        vote, probability, seconds, worked = self.detect_language(s, audio, speech, start, end)
        if not worked:
            # Nothing was heard, so nothing is decided. Staying paused on a detector that cannot
            # judge would blank the video for as long as it stays broken.
            return True
        with s.lock:
            was_paused = s.language_paused
            wanted = language_vote(s, vote, seconds, args.language, args.language_patience)
            paused, heard, foreign = s.language_paused, s.heard, s.foreign_seconds
        if vote is not None and vote != args.language:
            log.info("[%s] %s-%s: heard %s (%.2f), %.0f s of foreign speech so far",
                     s.video_id, fmt_time(start), fmt_time(end), vote, probability, foreign)
        if paused and not was_paused:
            log.info("[%s] no %s heard for %.0f s of speech (heard %s); subtitles paused until it returns",
                     s.video_id, args.language, foreign, heard)
        elif was_paused and not paused:
            log.info("[%s] %s is back; subtitles resume", s.video_id, args.language)
        if paused != was_paused:
            self.app.save_cache(s)  # the verdict changed; a probe on its own changes nothing to keep
        return wanted

    def sung_in_target(self, s: Session, audio, start: float, end: float) -> bool:
        """Whether a loud window the detector heard no speech in is sung in the target language.

        wants_lyrics() judges loudness alone: without this, every loud windowful of rain, crowd or
        engine noise, and an English song under a montage, would be decoded in full (the VAD path
        decodes nothing when Silero heard nothing) and its inventions left to the gates. So the
        language head judges the window's own samples first, LANGUAGE_DETECT_SECONDS of them at a
        time: the window's first thirty seconds and, when those are refused and the window is
        longer, its last thirty, since a song that starts in a window's last ten seconds, after an
        instrumental intro, would otherwise be refused on the intro alone and those seconds covered
        blank for good. The verdict never reaches language_vote(): a foreign song never pauses. A
        detector that raises must not silence a video: the window is decoded as sung, unjudged.
        """
        args = self.app.args
        slices = [speech_samples(audio, [[start, end]], start)]
        if len(audio) > int(LANGUAGE_DETECT_SECONDS * SAMPLE_RATE):
            slices.append(audio[-int(LANGUAGE_DETECT_SECONDS * SAMPLE_RATE):])
        language, probability = None, 0.0
        for samples in slices:
            try:
                language, probability, _ = self.app.model.detect_language(audio=samples)
                probability = float(probability)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] language detection of a sung window failed (%s); transcribing it as lyrics", s.video_id, exc)
                return True
            if str(language) == args.language and math.isfinite(probability) and probability >= LANGUAGE_MIN_PROB:
                return True
        log.info("[%s] %s-%s: no speech heard, and it does not sound like %s (%s %.2f); left blank",
                 s.video_id, fmt_time(start), fmt_time(end), args.language, language, probability)
        return False

    def process(self, s: Session, start: float, end: float) -> None:
        args = self.app.args
        with s.lock:
            audio = audio_slice(s, start, end)
            if audio is None:
                return
            s.busy = [round(start, 2), round(end, 2)]
            # More audio always follows the live edge, so a segment cut there is re-transcribed too.
            boundary_free = (s.live or end < s.duration - 0.05) and find_covering(s.covered, end + 0.01, tol=0.0) is None

        t0 = time.time()
        try:
            # Our own Silero pass over the same audio and the same options the decoder gets, so the
            # cue builder can snap to, extend into and judge cues against the intervals it heard.
            speech = detect_speech(audio, offset=start)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] VAD failed (%s); treating the whole window as speech", s.video_id, exc)
            speech = [[start, end]]

        # Detection costs an encoder pass, so --language-patience 0 must not reach it at all.
        wanted = True
        if float(getattr(args, "language_patience", 0.0) or 0.0) > 0:
            try:
                wanted = self.watch_language(s, audio, speech, start, end)
            except Exception:  # noqa: BLE001
                # Between setting s.busy and the transcribe call nothing may raise: the window
                # would be replanned for ever with busy stuck on it.
                log.exception("[%s] language watch failed; transcribing the window", s.video_id)
        if not wanted:
            with s.lock:
                # Heard, not written. This goes to `probed`, not `covered`: the planner moves on,
                # but nothing claims Whisper has seen this audio, and nothing reaches the cache.
                s.probed = merge_intervals(s.probed + [[start, end]])
                s.busy = None
                heard = s.heard
            log.info("[%s] %s-%s: skipped (heard %s, paused)", s.video_id, fmt_time(start), fmt_time(end), heard)
            return

        # Singing over music is no speech to Silero, so a window it heard nothing in, whose audio
        # is not silence, is decoded without the detector and gated on Whisper's own confidence
        # (lyrics_reason), once the language head has heard the target language in it: loud
        # noise and a foreign song stay with the detector, which decodes nothing of them. The
        # language watch above cast no vote on such a window, and the head's verdict here is no
        # vote either: a foreign song never pauses.
        lyrics = wants_lyrics(args, audio, speech, start, end) and self.sung_in_target(s, audio, start, end)
        options = dict(
            language=args.language,
            task="transcribe",
            beam_size=args.beam_size,
            word_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=args.initial_prompt or None,
            temperature=[0.0, 0.2, 0.4, 0.6],
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            hallucination_silence_threshold=2.0,
        )
        if lyrics:
            options["vad_filter"] = False
        else:
            options.update(vad_filter=True, vad_parameters=vad_parameters())
        try:
            segments, _info = self.app.model.transcribe(audio, **options)
            segs = list(segments)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] transcription of %s-%s failed: %s", s.video_id, fmt_time(start), fmt_time(end), exc)
            if "cuda" in str(exc).lower() or "cudnn" in str(exc).lower() or "cublas" in str(exc).lower():
                log.error("The GPU context looks broken (driver reset or out of memory). Exiting so the launcher can restart the server.")
                os._exit(3)
            with s.lock:
                s.busy = None
                s.covered = merge_intervals(s.covered + [[start, end]])
            return

        # A segment touching the end of the window is probably cut mid-sentence; drop it and let
        # the next window start where it began.
        new_end = end
        if boundary_free and len(segs) > 1 and float(segs[-1].end) > (end - start) - 1.0:
            dropped = segs.pop()
            new_end = min(end, max(start + 1.0, start + float(dropped.start)))

        limits = cue_limits(args)
        if lyrics:
            # No intervals to build on: the lines that pass the gates are the window's speech.
            speech = lyrics_spans(segs, start, limits)
        drops: dict = {}
        with s.lock:
            seg_id = s.seg_next
        fresh, seg_id = build_window_cues(segs, start, speech, limits, seg_id, drops, new_end, lyrics)

        # A song shares a window with the line announcing it: the detector heard that line, so the
        # window went its way and nothing of the singing was decoded. Its loud unheard stretches are
        # not covered; the planner brings each back as a window of its own, where wants_lyrics()
        # sees next to no speech. Only a window that the speech heard in it kept from the lyrics
        # path: one the head refused (or --lyrics off) is covered whole, or it would be planned
        # for ever.
        unsung: list = []
        if not lyrics and getattr(args, "lyrics", "auto") == "auto" \
                and speech_seconds(speech, start, end) >= LYRICS_MAX_SPEECH_S:
            unsung = unsung_stretches(audio, start, new_end, speech, fresh)

        added = 0
        with s.lock:
            recent = s.cues[-80:]
            for cue in fresh:
                if any(cue_overlaps(cue, r) for r in recent):
                    continue
                cue["id"] = len(s.cues)
                s.cues.append(cue)
                recent.append(cue)
                added += 1
            s.seg_next = seg_id
            s.covered = merge_intervals(s.covered + subtract_intervals([[start, new_end]], unsung))
            s.speech = merge_intervals(
                s.speech + [[max(a, start), min(b, new_end)] for a, b in speech if min(b, new_end) > max(a, start)])
            s.busy = None
        elapsed = time.time() - t0
        gated = ", ".join(f"{k}:{v}" for k, v in sorted(drops.items()) if not k.startswith("_"))
        log.info(
            "[%s] %s-%s: %d cues in %.1fs (%.0fx realtime)%s%s%s",
            s.video_id, fmt_time(start), fmt_time(new_end), added, elapsed,
            (new_end - start) / max(elapsed, 1e-3), " [lyrics]" if lyrics else "",
            f" [dropped {gated}]" if gated else "",
            f" [{sum(b - a for a, b in unsung):.0f} s heard nothing in, planned again]" if unsung else "",
        )
        self.app.save_cache(s)


# --------------------------------------------------------------------------- application

class App:
    def __init__(self, args, model, device: str, compute_type: str):
        self.args = args
        self.model = model
        self.device = device
        self.compute_type = compute_type
        # Which model is loaded, which one the client last asked for, and how the last switch went.
        # Only the transcriber thread replaces self.model (switch_model_if_wanted, between windows),
        # so the HTTP threads never touch a model that is being freed or loaded. Names are kept in
        # their canonical form (canonical_model_name) so that an alias and its repo id compare equal.
        self.default_model = canonical_model_name(getattr(args, "model", None))
        self.model_name = self.default_model
        self.wanted_model = self.model_name
        self.model_loading: Optional[str] = None  # the name being prepared or swapped in
        self.model_preparing: Optional[str] = None  # a download runs for this name on prepare_thread
        self.model_prepared: Optional[tuple] = None  # (name, directory) ready for the transcriber to swap
        self.prepare_thread: Optional[threading.Thread] = None
        self.model_error: Optional[tuple] = None  # (name, message) of the last failed prepare or load
        self.model_failed_at = 0.0
        self.exit_code: Optional[int] = None  # set by request_update(): main() exits with it after serve_forever()
        self.sessions: dict = {}
        self.lock = threading.Lock()
        self.fetcher = Fetcher(args)
        self.last_evict = time.time()
        self.transcriber = Transcriber(self)
        self.transcriber.start()

    def health(self) -> dict:
        with self.lock:
            error = self.model_error
            state = {
                "model": self.model_name,
                "default_model": self.default_model,
                "model_loading": self.model_loading,
                "model_error": ({"model": error[0], "error": error[1], "names": model_spellings(error[0])}
                                if error else None),
                "models": downloaded_models(),
                "device": self.device,
                "compute_type": self.compute_type,
            }
        return {"ok": True, "version": VERSION, **state, "language": self.args.language,
                "launcher": self.update_blocker() is None}

    def update_blocker(self) -> Optional[str]:
        """Why POST /update would achieve nothing, or None when run.cmd / run.sh would act on the exit.

        Only the launchers run update.py after an exit with EXIT_UPDATE; they say so through
        SHISUKO_LAUNCHER. Under Docker, Nix or a plain `python server.py` the exit would just end
        the server, and with --no-update the launcher restarts it without updating. A launcher
        from before the variable that updated itself but was never restarted has no code-4 branch
        either (run.sh parsed its loop before the update), so its server is rightly refused too.
        """
        if os.environ.get("SHISUKO_LAUNCHER") != "1":
            return ("the server was not started by run.cmd / run.sh, or by an older launcher that has not been "
                    "restarted since it was updated, so nothing would update it; restart it by hand")
        if getattr(self.args, "no_update", False):
            return "the server was started with --no-update; restart it without the flag to update"
        if os.environ.get("SHISUKO_NO_UPDATE", "").strip() not in ("", "0"):
            # The same rule as update.skipped(): the launcher runs update.py under this very
            # environment, so the exit would only restart the server, without a word from update.py.
            return "the server was started with SHISUKO_NO_UPDATE set; restart it without the variable to update"
        return None

    def request_update(self) -> tuple:
        """(ok, error) for POST /update: mark the process to end with EXIT_UPDATE, or say why not.

        The caller stops the HTTP server afterwards; main() then exits with exit_code and the
        launcher runs update.py. The server never spawns update.py itself: the update may replace
        server.py and the launcher, and only the launcher's loop knows how to survive that.
        """
        error = self.update_blocker()
        if error is not None:
            return False, error
        with self.lock:
            self.exit_code = EXIT_UPDATE
        return True, None

    def request_model(self, name) -> None:
        """Remember the model the client wants; the transcriber switches to it between windows."""
        name = (name or "").strip()
        if not name:
            # An empty setting means the operator's --model. It is not validated: it may be a
            # local directory, which valid_model_name() refuses because a browser could name one.
            name = self.default_model
        elif not valid_model_name(name):
            return  # not stored: an invalid name is reported per request by model_state()
        else:
            name = canonical_model_name(name)
        with self.lock:
            if name is None or self.in_cooldown(name):
                return  # the client re-sends the setting every second; a failed name waits for --retry-after
            self.wanted_model = name

    def model_state(self, requested) -> dict:
        """The /sync fields about the model, judged for the name this request asked for."""
        requested = (requested or "").strip()
        with self.lock:
            error = None
            if not requested:
                requested = self.default_model  # the operator's choice, valid by definition
            elif not valid_model_name(requested):
                error = MODEL_NAME_HINT
            else:
                requested = canonical_model_name(requested)
            if error is None and self.model_error is not None and self.model_error[0] == requested:
                error = self.model_error[1]
            return {"model": self.model_name, "model_loading": self.model_loading, "model_error": error}

    def in_cooldown(self, name) -> bool:
        """True while `name` failed less than --retry-after seconds ago. Call with self.lock held."""
        return (self.model_error is not None and self.model_error[0] == name
                and time.time() - self.model_failed_at < getattr(self.args, "retry_after", 30.0))

    def switch_model_if_wanted(self) -> bool:
        """One step towards the wanted model, called by the transcriber between windows.

        The files come first, on a side thread (prepare_model), while the loaded model keeps
        transcribing: a typo, a repo that does not exist or an offline hub then costs nothing but
        a failed download, never the model in use. Once the files are in place the swap happens
        here, on the transcriber thread so no window runs meanwhile, and the old model is released
        before the new one loads: on a GPU whose memory is mostly held by other programs the two
        rarely fit side by side. If the previous model cannot come back after a failed swap, the
        server exits with code 3 so the launcher restarts it with the default.
        """
        with self.lock:
            wanted, previous = self.wanted_model, self.model_name
            if wanted == previous or wanted is None:
                # The client changed its mind: nothing is loading, and files a finished download
                # left behind are dropped (a running one is dropped by the tick after it ends).
                self.model_prepared, self.model_loading = None, None
                return False
            if self.model_preparing is not None:
                # A download is still running; keep transcribing with the old model. The wanted
                # name is reported as loading whether it is that download or the one that follows
                # it: a change of mind cannot cancel a download, so the new name waits behind it.
                self.model_loading = wanted
                return False
            prepared = self.model_prepared
            if prepared is None or prepared[0] != wanted:
                self.model_prepared = None  # files of a name nobody wants any more
                if self.in_cooldown(wanted):
                    return False
                if self.model_error is not None and self.model_error[0] == wanted:
                    self.model_error = None  # a fresh attempt: the old verdict would be reported beside it
                self.model_preparing = self.model_loading = wanted
                self.prepare_thread = threading.Thread(target=self.prepare_model, args=(wanted,),
                                                       daemon=True, name="prepare-model")
                self.prepare_thread.start()
                return False
            self.model_prepared = None
            self.model_loading = wanted
        log.info("Switching from model '%s' to '%s'", previous, wanted)
        self.model = None
        gc.collect()  # CTranslate2 gives the GPU memory back once the last reference is gone
        try:
            loaded = load_model(self.args, wanted, path=prepared[1])
        except Exception as exc:  # noqa: BLE001
            log.error("Could not load the model '%s': %s", wanted, exc)
            with self.lock:
                self.model_error = (wanted, friendly_model_error(exc, wanted))
                self.model_failed_at = time.time()
                if self.wanted_model == wanted:
                    self.wanted_model = previous  # nothing retries on its own: the client has to ask again
                self.model_loading = previous
            self.reload_model(previous)
            return False
        with self.lock:
            self.model, self.device, self.compute_type = loaded
            self.model_name = wanted
            self.model_loading = None
            self.model_error = None
        self.restart_sessions()
        return True

    def prepare_model(self, name: str) -> None:
        """Download (or locate) the files of `name` and hand them to the transcriber; runs on its own thread."""
        try:
            if name == self.default_model and os.path.isdir(name):
                path = name  # the operator's --model is a folder (see request_model): nothing to download
            else:
                # self.device is the backend the loaded model runs on, so the files are the ones
                # the swap will need: CTranslate2 for cuda and cpu, MLX weights for the Apple GPU.
                path = download_model_files(name, self.device)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not prepare the model '%s': %s", name, exc)
            with self.lock:
                self.model_error = (name, friendly_model_error(exc, name))
                self.model_failed_at = time.time()
                self.model_preparing = None
                self.model_loading = None
                if self.wanted_model == name:
                    self.wanted_model = self.model_name  # see switch_model_if_wanted: no retry without a request
            return
        with self.lock:
            self.model_preparing = None
            self.model_prepared = (name, path)

    def reload_model(self, name) -> None:
        """Bring the previous model back after a failed switch; without any model the server is useless."""
        try:
            loaded = load_model(self.args, name)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not load the previous model '%s' either (%s). The server has no model left; "
                      "exiting so the launcher can restart it.", name, exc)
            os._exit(3)
        with self.lock:
            self.model, self.device, self.compute_type = loaded
            self.model_loading = None

    def restart_sessions(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            self.restart_session(s)

    def restart_session(self, s: Session) -> None:
        """Drop the cues of the model that is gone; the new token tells the client to start over."""
        with s.lock:
            s.cues, s.covered, s.speech, s.seg_next = [], [], [], 0
            s.busy = None
            s.token = uuid.uuid4().hex[:12]
            no_audio = s.audio is None and s.preview is None and s.live_audio is None
            if no_audio and not s.fetching and s.status == "ready":
                # The old model's cache had marked the video covered, so its audio was never
                # fetched; pending makes get_session fetch it again (the same trick as in clip()).
                s.status = "pending"
            self.load_cache(s)

    def sessions_summary(self) -> dict:
        with self.lock:
            sessions = list(self.sessions.values())
        out = []
        for s in sessions:
            with s.lock:
                out.append({
                    "video_id": s.video_id, "status": s.status, "title": s.title, "duration": s.duration,
                    "cues": len(s.cues), "covered": s.covered, "want_t": s.want_t, "busy": s.busy,
                    "preview": s.preview is not None, "live": s.live,
                    "live_audio": s.live_audio.available() if s.live_audio is not None else None,
                    "idle_seconds": round(time.time() - s.last_sync, 1),
                    "heard": s.heard, "language_paused": s.language_paused,
                })
        return {"ok": True, "sessions": out}

    def get_session(self, video_id: str, url: str) -> Session:
        with self.lock:
            s = self.sessions.get(video_id)
            if s is None:
                s = Session(video_id=video_id, url=url)
                self.load_cache(s)
                self.sessions[video_id] = s
        with s.lock:
            retry_due = s.status == "error" and time.time() - s.error_at >= self.args.retry_after
            need_fetch = (s.status in ("pending", "evicted") or retry_due) and not s.fetching
            if need_fetch:
                if retry_due:
                    log.info("[%s] retrying the audio fetch after an earlier failure", video_id)
                s.fetching = True
        if need_fetch:
            threading.Thread(target=self.fetcher.fetch, args=(s,), daemon=True, name=f"fetch-{video_id}").start()
        return s

    def sync(self, video_id: str, url: str, t: float, since: int, model=None) -> dict:
        self.request_model(model)
        s = self.get_session(video_id, url)
        with s.lock:
            s.want_t = max(0.0, float(t))
            s.last_sync = time.time()
            since = max(0, min(int(since), len(s.cues)))
            resp = {
                "ok": True,
                "session": s.token,
                "status": s.status,
                "error": s.error,
                "duration": s.duration,
                "title": s.title,
                "live": s.live,
                "covered": [[round(a, 2), round(b, 2)] for a, b in s.covered],
                # Only the intervals around the playhead: the whole list would be resent every second.
                "speech": [[round(a, 2), round(b, 2)] for a, b in s.speech
                           if b >= s.want_t - SPEECH_SYNC_BACK and a <= s.want_t + SPEECH_SYNC_AHEAD],
                "cues": s.cues[since:],
                "next": len(s.cues),
                "busy": s.busy,
                # The last foreign language heard, and whether it has silenced this video.
                "heard": s.heard,
                "language_paused": s.language_paused,
            }
        resp.update(self.model_state(model))
        self.maybe_evict()
        return resp

    def clip(self, video_id: str, start: float, end: float, fmt: str):
        """Audio clip for sentence mining. Raises ClipNotReady while the audio is still being fetched."""
        if end <= start:
            raise ValueError("end must be after start")
        if end - start > MAX_CLIP_SECONDS:
            raise ValueError(f"clip longer than {MAX_CLIP_SECONDS:.0f} seconds")
        with self.lock:
            s = self.sessions.get(video_id)
        fallback = None
        if s is not None:
            with s.lock:
                if s.live_audio is not None:
                    # The buffer holds a few minutes around the playhead; a clip outside it is gone for good.
                    samples = s.live_audio.slice(max(0.0, start), end)
                    if samples is None:
                        raise ValueError("this part of the live stream is no longer buffered")
                    return encode_clip(samples, fmt)
                if s.live:
                    raise ClipNotReady()
                fallback = s.audio
                if s.duration:
                    end = min(end, s.duration)
        if find_cached_audio(video_id) is None and fallback is None:
            if s is not None:
                with s.lock:
                    # Cached cues can outlive the audio file: the session then looks ready but holds
                    # no audio, so mark it pending to make get_session fetch the audio again.
                    if s.status == "ready" and s.audio is None and not s.fetching:
                        s.status = "pending"
            self.get_session(video_id, f"https://www.youtube.com/watch?v={video_id}")
            raise ClipNotReady()
        return make_clip(video_id, max(0.0, start), end, fmt, fallback)

    def pick_work(self):
        now = time.time()
        timeout = getattr(self.args, "client_timeout", 30.0)
        with self.lock:
            sessions = sorted(self.sessions.values(), key=lambda x: x.last_sync, reverse=True)
        for s in sessions:
            if timeout > 0 and now - s.last_sync > timeout:
                continue  # nobody has synced this video recently (tab closed): do not transcribe ahead for it
            with s.lock:
                window = plan_window(s, self.args)
                if window:
                    return (s, window[0], window[1])
        return None

    def maybe_evict(self) -> None:
        now = time.time()
        if now - self.last_evict < 60:
            return
        self.last_evict = now
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            with s.lock:
                if now - s.last_sync <= self.args.idle_minutes * 60:
                    continue
                if s.status == "ready" and s.audio is not None:
                    s.audio = None
                    s.preview = None
                    s.status = "evicted"
                    log.info("[%s] released audio after %d idle minutes", s.video_id, self.args.idle_minutes)
                elif s.live_audio is not None and not s.fetching:
                    s.live_audio = None  # a follower that stopped on an error leaves its buffer behind
                    s.status = "evicted"

    def read_cache(self, s: Session, path: Path) -> Optional[dict]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] ignoring unreadable cue cache %s: %s", s.video_id, path.name, exc)
            return None
        return data if isinstance(data, dict) else None

    def cache_model_matches(self, data: dict) -> bool:
        return canonical_model_name(data.get("model")) == self.model_name

    def load_cache(self, s: Session) -> None:
        data = self.read_cache(s, s.cache_path())
        title = (data or {}).get("title") or ""
        if data is None or not self.cache_model_matches(data):
            # Another model owns the primary file: save_cache() moved ours aside when it took over.
            archived = self.read_cache(s, s.model_cache_path(self.model_name))
            if archived is not None and self.cache_model_matches(archived):
                data = archived
                title = title or (archived.get("title") or "")
        if data is None:
            return
        s.title = title
        if not self.cache_model_matches(data) or data.get("language") != self.args.language:
            return  # cues from another model are not reused, the title is
        if data.get("format") != CACHE_FORMAT:
            return  # older caches have no segment ids, so they are transcribed again
        s.cues = [c for c in data.get("cues", []) if isinstance(c, dict)]
        for i, c in enumerate(s.cues):
            c["id"] = i
        s.seg_next = max((int(c.get("seg", -1)) for c in s.cues), default=-1) + 1
        s.speech = merge_intervals(data.get("speech", []))
        s.covered = merge_intervals(data.get("covered", []))
        s.duration = float(data.get("duration") or 0.0)
        watch = data.get("language_state")
        # Not restored with detection off: nothing could ever clear it again, and --language-patience 0
        # is what the README offers a viewer whose video was paused by mistake.
        if isinstance(watch, dict) and float(getattr(self.args, "language_patience", 0.0) or 0.0) > 0:
            try:
                s.foreign_seconds = float(watch.get("foreign_seconds") or 0.0)
            except (TypeError, ValueError):
                s.foreign_seconds = 0.0
            heard = watch.get("heard")
            s.heard = heard if isinstance(heard, str) and heard else None
            s.language_paused = bool(watch.get("paused"))
        # A cache made without the lyrics rule (a 0.11.2 server, which wrote format 3 without the
        # key, or --lyrics off; an older format never gets here, the check above drops it whole)
        # marked a sung stretch covered without a word in it: Silero heard nothing there, so
        # nothing reached the decoder. Offer those stretches to the planner again, cues and the
        # rest kept, so a music video watched before the rule is not blank for ever: one at either
        # end of a covered range from 1.5 s (an intro, an outro, the whole of a Short), one between
        # two lines from LYRICS_MIN_STRETCH_S, as process() plans them for a fresh window, since
        # every pause of a talk is a hole of a second or two and a window per pause would fetch,
        # walk and rewrite the record dozens of times over; wants_lyrics() judges each window anew
        # (a silent one costs a Silero pass), and the record is written with the key by the first
        # window walked.
        if data.get("lyrics") != "auto" and getattr(self.args, "lyrics", "auto") == "auto":
            unheard = unheard_stretches(s.covered, s.speech, s.cues, inner_seconds=LYRICS_MIN_STRETCH_S)
            if unheard:
                s.covered = subtract_intervals(s.covered, unheard)
                log.info("[%s] %.0f s were covered before the lyrics rule with nothing heard; transcribing them again",
                         s.video_id, sum(b - a for a, b in unheard))
        if s.fully_covered():
            s.status = "ready"  # nothing left to transcribe, no need to fetch the audio again
        log.info("[%s] loaded %d cached cues", s.video_id, len(s.cues))

    def cache_record(self, s: Session) -> dict:
        """What save_cache() writes for a session (retranscribe.py writes the same shape elsewhere)."""
        with s.lock:
            return {
                "video_id": s.video_id, "title": s.title, "duration": s.duration,
                "format": CACHE_FORMAT,
                "model": self.model_name, "language": self.args.language,
                "cues": list(s.cues), "covered": [list(iv) for iv in s.covered],
                "speech": [[round(a, 2), round(b, 2)] for a, b in s.speech],
                # The rule the covered ranges were made under: without the lyrics rule a sung
                # window was covered with nothing in it, and load_cache() offers it again.
                "lyrics": getattr(self.args, "lyrics", "auto"),
                # Reopening a foreign video finds it paused instead of hallucinating all over again.
                "language_state": {"foreign_seconds": round(s.foreign_seconds, 2),
                                   "heard": s.heard, "paused": s.language_paused},
            }

    def save_cache(self, s: Session) -> None:
        with s.lock:
            if s.live:
                return  # the stream's clock is not the clock of the video it becomes afterwards
            data = self.cache_record(s)
        tmp = s.cache_path().with_suffix(".tmp")
        try:
            self.archive_other_model_cache(s)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, s.cache_path())
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] could not write cue cache: %s", s.video_id, exc)

    def archive_other_model_cache(self, s: Session) -> None:
        """Move another model's cues out of the primary file before it is overwritten.

        Two hours of large-v3 must survive a one-minute experiment with small: the primary
        <id>.cues.json always holds the loaded model's cues (the tools and docs refer to it), the
        other model's go to model_cache_path() and come back through load_cache() after the
        next switch.
        """
        primary = s.cache_path()
        old = self.read_cache(s, primary)
        if old is None or self.cache_model_matches(old):
            return
        model = old.get("model")
        if isinstance(model, str) and model:
            os.replace(primary, s.model_cache_path(canonical_model_name(model)))


# --------------------------------------------------------------------------- HTTP

EXTENSION_ORIGIN_PREFIXES = ("moz-extension://", "chrome-extension://", "safari-web-extension://")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def origin_allowed(origin: Optional[str]) -> bool:
    """Browser origins that may use the server: the extension itself and pages served on this machine.

    Requests without an Origin header (curl, the Docker health check, other local tools) are not
    browser cross-origin requests and are handled separately by the caller.
    """
    if not origin or origin == "null":
        return False
    if origin.startswith(EXTENSION_ORIGIN_PREFIXES):
        return True
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and (parts.hostname or "") in LOOPBACK_HOSTS


def update_origin_allowed(origin: Optional[str]) -> bool:
    """Who may POST /update: the extension itself and non-browser clients (no Origin header).

    Narrower than origin_allowed() on purpose. A page on a loopback host may drive downloads and
    transcription like the extension does, but ending the server and making the launcher run
    git and pip is a capability nothing but the popup has a use for; a local dev server or
    notebook carrying a third-party script must not get it.
    """
    return origin is None or origin.startswith(EXTENSION_ORIGIN_PREFIXES)


SHUTDOWN_DELAY = 0.5  # seconds between the answer to POST /update and the end of serve_forever()


def stop_server_later(httpd, delay: float = SHUTDOWN_DELAY) -> None:
    """Stop `httpd` from a helper thread once the answer under way has left the socket.

    shutdown() blocks until serve_forever() has returned, so the handler thread that answered
    POST /update cannot call it; a short wait keeps the client from seeing the connection drop
    before its 200 arrives. main() takes over after serve_forever().
    """
    time.sleep(delay)
    httpd.shutdown()


class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    server_version = f"ShisuKo/{VERSION}"

    def log_message(self, fmt, *args):  # quieter than the default
        log.debug("http: " + fmt, *args)

    def _origin_ok(self) -> bool:
        """True for non-browser clients (no Origin header) and for allowed browser origins."""
        origin = self.headers.get("Origin")
        return origin is None or origin_allowed(origin)

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if not origin or not origin_allowed(origin):
            return  # no CORS headers: the browser refuses to hand the response to the page
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _reject_origin(self) -> None:
        log.warning("rejected request from origin %s", self.headers.get("Origin"))
        self._json(403, {"ok": False, "error": "origin not allowed"})

    def _json(self, code: int, payload, close: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            # Also sets self.close_connection, so the handler stops after this request.
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._reject_origin()
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._reject_origin()
            return
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, self.app.health())
        elif path == "/sessions":
            self._json(200, self.app.sessions_summary())
        elif path == "/clip":
            self._clip(urlsplit(self.path).query)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _clip(self, query: str) -> None:
        qs = parse_qs(query)
        video_id = qs.get("video_id", [""])[0]
        if not VIDEO_ID_RE.match(video_id):
            self._json(400, {"ok": False, "error": "invalid video_id"})
            return
        try:
            start = float(qs.get("start", ["0"])[0])
            end = float(qs.get("end", ["0"])[0])
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid start/end"})
            return
        fmt = "wav" if qs.get("format", ["mp3"])[0].lower() == "wav" else "mp3"
        try:
            data, mime, ext = self.app.clip(video_id, start, end, fmt)
        except ClipNotReady:
            self._json(503, {"ok": False, "error": "audio not ready yet, retry in a moment"})
            return
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("clip failed")
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{video_id}_{int(start * 1000)}.{ext}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not self._origin_ok() or (path == "/update" and not update_origin_allowed(self.headers.get("Origin"))):
            self._reject_origin()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                # The body is left unread, so the connection must not be reused: on a keep-alive
                # connection the leftover bytes would be parsed as the next request.
                self._json(413, {"ok": False, "error": "request too large"}, close=True)
                return
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except Exception:  # noqa: BLE001
            self._json(400, {"ok": False, "error": "invalid JSON body"})
            return
        if path == "/update":
            self._update()
            return
        if path != "/sync":
            self._json(404, {"ok": False, "error": "not found"})
            return
        video_id = str(body.get("video_id") or "")
        if not VIDEO_ID_RE.match(video_id):
            self._json(400, {"ok": False, "error": "invalid video_id"})
            return
        try:
            t = float(body.get("t") or 0.0)
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "invalid t/since"})
            return
        model = body.get("model", "")  # absent in older extensions: the server's own default
        if not isinstance(model, str):
            self._json(400, {"ok": False, "error": "invalid model"})
            return
        try:
            self._json(200, self.app.sync(video_id, str(body.get("url") or ""), t, since, model))
        except Exception as exc:  # noqa: BLE001
            log.exception("sync failed")
            self._json(500, {"ok": False, "error": str(exc)})

    def _update(self) -> None:
        """POST /update: end the process with EXIT_UPDATE so the launcher updates and restarts it."""
        ok, error = self.app.request_update()
        if not ok:
            self._json(409, {"ok": False, "error": error})
            return
        log.info("Update requested: exiting with code %d so the launcher runs update.py and starts the server again", EXIT_UPDATE)
        self._json(200, {"ok": True, "restarting": True, "version": VERSION})
        # self.server is the ThreadingHTTPServer serving this request (BaseRequestHandler sets it).
        threading.Thread(target=stop_server_later, args=(self.server,), daemon=True, name="stop-server").start()


# --------------------------------------------------------------------------- startup

def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def gpu_memory_mb():
    """(free, total) GPU memory in MiB via nvidia-smi, or None if unavailable."""
    exe = shutil.which("nvidia-smi")
    if exe is None and os.name == "nt":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        exe = candidate if os.path.exists(candidate) else None
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        free, total = (int(float(x.strip())) for x in out[0].split(","))
        return free, total
    except Exception:  # noqa: BLE001
        return None


LOW_VRAM_MB = 4500      # below this, int8 weights are used automatically
CRITICAL_VRAM_MB = 2500  # below this, warn that the driver may reset under load


def valid_model_name(name) -> bool:
    return isinstance(name, str) and MODEL_NAME_RE.fullmatch(name) is not None and ".." not in name


# --------------------------------------------------------------------------- the Apple GPU (MLX)

# CTranslate2 has no Metal backend, so on Apple Silicon faster-whisper decodes on the CPU: large-v3
# manages about twice real time there and keeps every core busy while the video plays. MLX runs the
# same Whisper weights on the GPU instead, roughly three times faster and out of the CPU's way.
# mlx-community publishes one converted repo per size, and this table is the only place they appear.
MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "large-v1": "mlx-community/whisper-large-v1-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
    "distil-medium.en": "mlx-community/distil-whisper-medium.en",
}
# An MLX repo holds the same weights as its size, so canonical_model_name() reports and caches both
# under the size: the cues of a machine's CPU run are still the cues of its GPU run.
MLX_ALIASES = {repo: alias for alias, repo in MLX_REPOS.items()}


def mlx_available() -> bool:
    """True when Whisper can decode on this machine's GPU: Apple Silicon with mlx-whisper installed."""
    if sys.platform != "darwin":
        return False
    try:
        import mlx.core as mx
        import mlx_whisper  # noqa: F401

        return bool(mx.metal.is_available())
    except Exception:  # noqa: BLE001
        return False


def resolve_device(device: str) -> str:
    """Turn --device auto into the backend that will actually run: an NVIDIA GPU, else Apple's, else the CPU."""
    if device != "auto":
        return device
    if cuda_available():
        return "cuda"
    if mlx_available():
        return "mlx"
    return "cpu"


def mlx_repo_for(name: str) -> str:
    """The MLX repo holding `name`'s weights; a repo id passes through, a size must be in the table."""
    repo = MLX_REPOS.get(canonical_model_name(name))
    if repo is not None:
        return repo
    if "/" in name:
        return name  # the viewer named a repo; whether it holds MLX weights the download decides
    raise ValueError(f"there is no MLX build of '{name}'; use a size such as large-v3, or an MLX repo id "
                     "like mlx-community/whisper-large-v3-mlx")


# The files of an MLX model, what download_mlx_model_files() and --download-model ask the Hub for.
MLX_FILE_PATTERNS = ("*.json", "*.safetensors", "*.npz")


def require_mlx_weights(path: str, name: str) -> None:
    """Refuse a repo that came back without MLX weights; the twin of require_model_bin()."""
    weights = any(os.path.isfile(os.path.join(path, w)) for w in ("weights.safetensors", "weights.npz"))
    if not os.path.isfile(os.path.join(path, "config.json")) or not weights:
        raise ValueError(f"{name} is not an MLX Whisper model (no config.json beside weights.safetensors); "
                         "use an mlx-community/whisper-* repo, or convert one with mlx_whisper.convert")


def download_mlx_model_files(name: str) -> str:
    """Fetch the MLX weights of `name` into MODELS_DIR (or find them there) and return the directory.

    The twin of download_model_files(): the same folder, the same "nothing here touches the GPU"
    rule that lets a download run beside the working model, and the same refusal of a repo that is
    not a converted model.
    """
    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=mlx_repo_for(name), cache_dir=str(MODELS_DIR),
                             allow_patterns=list(MLX_FILE_PATTERNS))
    require_mlx_weights(path, name)
    return path


@dataclass
class MlxSegment:
    """What the gates read off a segment: its span, its text, its words and the decoder's confidence.

    `no_speech_prob` and `avg_logprob` are what lyrics_reason() judges a window Silero heard
    nothing in by; mlx-whisper reports both, so a sung window is gated the same either way.
    """
    start: float
    end: float
    text: str
    words: list
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


@dataclass
class MlxInfo:
    """The second half of faster-whisper's transcribe() return value; only `language` is ever read."""
    language: Optional[str]
    language_probability: float = 1.0


class MlxWhisperModel:
    """faster-whisper's WhisperModel over mlx-whisper, so nothing else in the server knows the difference.

    Two options do not survive the crossing. MLX has no beam search, so --beam-size is dropped and
    each temperature is sampled once. And mlx-whisper has no vad_filter of its own: given one, this
    runs the Silero pass faster-whisper would run, hands the decoder the speech with the silence cut
    out and maps the timestamps back, which is what faster-whisper does with the same options.
    """

    def __init__(self, path: str, compute_type: str = "float16"):
        import mlx.core as mx

        self.path = path
        self.fp16 = compute_type != "float32"
        self.dtype = mx.float16 if self.fp16 else mx.float32

    def _model(self):
        """The decoder's own cached model, so detect_language() never loads a second copy of the weights."""
        from mlx_whisper.transcribe import ModelHolder

        return ModelHolder.get_model(self.path, self.dtype)

    def __del__(self):
        # switch_model_if_wanted() drops its reference and collects before loading the next model;
        # mlx-whisper's module-level cache would otherwise hold these weights until a window decodes.
        try:
            from mlx_whisper.transcribe import ModelHolder

            if getattr(ModelHolder, "model_path", None) == self.path:
                ModelHolder.model = None
                ModelHolder.model_path = None
        except Exception:  # noqa: BLE001
            pass

    def transcribe(self, audio, **options):
        """(segments, info), the shape Transcriber.process() and load_model()'s warm-up expect."""
        import mlx_whisper

        language = options.get("language")
        restore = None
        if options.get("vad_filter"):
            from faster_whisper.vad import SpeechTimestampsMap, VadOptions, collect_chunks, get_speech_timestamps

            chunks = get_speech_timestamps(
                audio, VadOptions(**VAD_PARAMS, max_speech_duration_s=VAD_MAX_SPEECH_SECONDS),
                sampling_rate=SAMPLE_RATE)
            if not chunks:
                return [], MlxInfo(language)
            audio = collect_chunks(audio, chunks, sampling_rate=SAMPLE_RATE)[0][0]
            restore = SpeechTimestampsMap(chunks, SAMPLE_RATE)
        result = mlx_whisper.transcribe(
            np.asarray(audio, dtype=np.float32),
            path_or_hf_repo=self.path,
            language=language,
            task=options.get("task", "transcribe"),
            word_timestamps=bool(options.get("word_timestamps")),
            condition_on_previous_text=bool(options.get("condition_on_previous_text", True)),
            initial_prompt=options.get("initial_prompt"),
            temperature=tuple(options.get("temperature") or (0.0,)),
            no_speech_threshold=options.get("no_speech_threshold", 0.6),
            logprob_threshold=options.get("log_prob_threshold", -1.0),
            compression_ratio_threshold=options.get("compression_ratio_threshold", 2.4),
            hallucination_silence_threshold=options.get("hallucination_silence_threshold"),
            fp16=self.fp16,
        )
        segments = []
        for seg in result.get("segments") or []:
            start, end = float(seg.get("start") or 0.0), float(seg.get("end") or 0.0)
            words = [Word(w.get("word") or "", float(w["start"]), float(w["end"]),
                          float(w.get("probability") or 0.0))
                     for w in (seg.get("words") or []) if w.get("start") is not None]
            if restore is not None:
                start, end = restore.get_original_time(start), restore.get_original_time(end, is_end=True)
                words = [Word(w.word, restore.get_original_time(w.start),
                              restore.get_original_time(w.end, is_end=True), w.probability) for w in words]
            segments.append(MlxSegment(start, end, seg.get("text") or "", words,
                                       float(seg.get("no_speech_prob") or 0.0),
                                       float(seg.get("avg_logprob") or 0.0)))
        return segments, MlxInfo(result.get("language") or language)

    def detect_language(self, audio=None, **_options):
        """(language, probability, every probability), the shape Transcriber.detect_language() unpacks."""
        import mlx.core as mx
        from mlx_whisper.audio import N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.decoding import detect_language as mlx_detect_language

        model = self._model()
        # An MLX array, not a numpy one: pad_or_trim() pads with mx.pad, which refuses numpy, and
        # anything shorter than the 30 s encoder window (most windows) is padded.
        mel = log_mel_spectrogram(pad_or_trim(mx.array(np.asarray(audio, dtype=np.float32)), N_SAMPLES),
                                  n_mels=model.dims.n_mels)
        _tokens, probabilities = mlx_detect_language(model, mel)
        probs = probabilities[0] if isinstance(probabilities, (list, tuple)) else probabilities
        language = max(probs, key=probs.get)
        return language, float(probs[language]), probs


_MODEL_ALIASES: Optional[tuple] = None  # (alias -> repo id, repo id -> first alias), built on first use


def model_alias_tables() -> tuple:
    """faster-whisper's size table both ways; empty when the library (or its private table) is missing."""
    global _MODEL_ALIASES
    if _MODEL_ALIASES is None:
        try:
            from faster_whisper import utils

            forward = dict(getattr(utils, "_MODELS", None) or {})
        except Exception:  # noqa: BLE001
            return {}, {}  # not remembered: the tables can only appear once the library is importable
        reverse: dict = {}
        for alias, repo in forward.items():
            reverse.setdefault(repo, alias)  # the first alias wins: large-v3, not large
        _MODEL_ALIASES = (forward, reverse)
    return _MODEL_ALIASES


def canonical_model_name(name):
    """One name per set of weights: large-v3 for Systran/faster-whisper-large-v3, large and itself.

    faster-whisper's size aliases and their repo ids load the same files, so the server compares,
    reports and caches under the first alias of the repo. An MLX repo is the same weights converted
    for the Apple GPU, so it answers to the size too. Anything else passes through.
    """
    if not isinstance(name, str):
        return name
    name = MLX_ALIASES.get(name, name)
    forward, reverse = model_alias_tables()
    return reverse.get(forward.get(name, name), name)


def model_spellings(name) -> list:
    """Every name that loads the same weights as `name`: its aliases in faster-whisper's table and the repo id.

    /health reports a failed model under its canonical name and lists these beside it, so the
    popup can match whatever spelling the viewer typed without a table of its own. A name the
    table does not know is its own only spelling.
    """
    forward, _ = model_alias_tables()
    # Through the canonical name, so that an MLX repo answers with the sizes too, not only itself.
    canonical = canonical_model_name(name)
    repo = forward.get(canonical, canonical)
    spellings = [alias for alias, target in forward.items() if target == repo] + [repo]
    mlx = MLX_REPOS.get(canonical)
    return spellings + [mlx] if mlx and mlx not in spellings else spellings


def downloaded_models() -> list:
    """The models in MODELS_DIR (the hub stores owner/name as models--owner--name), under their canonical names."""
    if not MODELS_DIR.is_dir():
        return []
    names = set()
    for p in MODELS_DIR.glob("models--*"):
        parts = p.name[len("models--"):].split("--")
        if p.is_dir() and len(parts) == 2 and all(parts):
            names.add(canonical_model_name("/".join(parts)))
    return sorted(names)


def require_model_bin(path: str, name: str) -> None:
    """Refuse a repo that came back without model.bin: not a converted model (a PyTorch checkpoint, say)."""
    if not os.path.isfile(os.path.join(path, "model.bin")):
        raise ValueError(f"{name} is not a CTranslate2/faster-whisper model (no model.bin); convert it with "
                         "ct2-transformers-converter or pick a *-ct2 / faster-whisper repo")


def download_model_files(name: str, device: str = "cpu") -> str:
    """Fetch the files of `name` into MODELS_DIR (or find them there) and return the directory.

    faster-whisper resolves its size aliases through its own table (ValueError for an unknown
    size), treats owner/name as a Hugging Face repo id and downloads only the model files. Nothing
    here touches the GPU, so it runs beside the working model. A repo that is not a converted
    model (a PyTorch checkpoint, say) comes back without model.bin and is refused before
    WhisperModel() can choke on it. On the Apple GPU the files are MLX weights instead, so `device`
    decides which of the two downloads runs.
    """
    if device == "mlx":
        return download_mlx_model_files(name)
    from faster_whisper import download_model

    path = download_model(name, cache_dir=str(MODELS_DIR))
    require_model_bin(path, name)
    return path


def friendly_model_error(exc: BaseException, name: str) -> str:
    """A short reason for the popup; huggingface_hub's exceptions run to several lines with request ids."""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "invalid model size" in low:
        return f"unknown model size '{name}'; use a size such as large-v3 or a Hugging Face repo id owner/name"
    if "no model.bin" in low:
        return msg
    if "404" in msg or "not found" in low:
        return f"'{name}' was not found on Hugging Face"
    if any(word in low for word in ("connection", "timed out", "timeout", "unreachable", "offline")):
        return f"could not reach Hugging Face to download '{name}'"
    lines = [line.strip() for line in msg.splitlines() if line.strip()]
    return (lines[-1] if lines else msg)[:200]


# What --download-model says before the progress bars, by canonical name (the bars carry the exact
# figures): the size of the converted weights on disk, which is also what the download moves.
MODEL_SIZES = {
    "large-v3": "about 3 GB", "large": "about 3 GB", "large-v2": "about 3 GB", "large-v1": "about 3 GB",
    "large-v3-turbo": "about 1.6 GB", "medium": "about 1.5 GB", "distil-large-v3": "about 1.5 GB",
    "small": "about 500 MB", "base": "about 150 MB", "tiny": "about 75 MB",
}
# The files of a converted model, the list faster_whisper.download_model() gives snapshot_download().
MODEL_FILE_PATTERNS = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*")


def wait_for_thread(thread: threading.Thread) -> None:
    """Join `thread` in half-second steps, so that Ctrl+C reaches the caller while it runs.

    Python raises KeyboardInterrupt in the main thread alone, and on Windows only once a wait
    returns: a plain join() there sits through the signal until the thread ends by itself.
    """
    while thread.is_alive():
        thread.join(0.5)


def run_download_model(name: str, device: str = "auto") -> int:
    """--download-model NAME, what setup runs: fetch the model with progress bars and make it the default.

    The same files download_model_files() fetches for a switch, but through huggingface_hub's
    snapshot_download() directly, so that its own tqdm bars stay on (faster-whisper's
    download_model() passes a disabled tqdm class) and the viewer watches model.bin arrive; on a
    stdout that is no terminal there are simply no bars. Nothing here loads a model or touches
    the GPU. Returns main()'s exit code: 0 with config.json naming the model for later starts,
    else 2 with one line saying why not, the code run.cmd / run.sh end on rather than restart
    (`run.cmd --download-model x` would otherwise try again every five seconds); Ctrl+C ends the
    process from here with the same code.
    """
    if not valid_model_name(name):
        print(f"'{name}' is {MODEL_NAME_HINT}")
        return 2
    try:
        from faster_whisper import utils as fw_utils
        from huggingface_hub import snapshot_download
    except Exception as exc:  # noqa: BLE001
        print(f"faster-whisper is not installed for {sys.executable} ({exc}); run setup first")
        return 2
    name = canonical_model_name(name)
    sizes = dict(getattr(fw_utils, "_MODELS", None) or {})
    # The backend that will run this model decides which conversion of it setup fetches: the
    # CTranslate2 files, or the MLX ones for an Apple GPU. `device` is the launcher's own
    # --device, so a download and the start that follows it agree; fetching the other format
    # here would cost the viewer the whole wait twice.
    mlx = resolve_device(device) == "mlx"
    patterns, require = (MLX_FILE_PATTERNS, require_mlx_weights) if mlx else (MODEL_FILE_PATTERNS, require_model_bin)
    try:
        repo_id = mlx_repo_for(name) if mlx else (name if "/" in name else sizes.get(name))
    except ValueError as exc:
        print(str(exc))
        return 2
    if repo_id is None:
        print(f"unknown model size '{name}': faster-whisper knows {', '.join(sizes) or 'no sizes at all'}; "
              "a Hugging Face repo id is written owner/name")
        return 2
    # A size from faster-whisper's own table names a converted model that WhisperModel() fetches
    # by itself, so the choice is kept before the download: a start after a failed or interrupted
    # one then downloads this model, not the built-in default, as setup promises. A repo id is
    # kept only once its files were seen, since a typo or a PyTorch checkpoint would make every
    # later start exit 2; a size whose repo turns out that way is taken back again.
    alias = name in sizes
    previous = configured_model()
    if alias:
        write_config({"model": name})
    print(f"Downloading {name} ({MODEL_SIZES.get(name, 'size unknown')}) into {MODELS_DIR} ...", flush=True)
    # The download runs on a thread of its own, waited for in short steps, so that Ctrl+C is
    # honoured at once: snapshot_download() fetches the files through a thread pool that joins its
    # workers on the way out, and an interrupt raised inside it would only surface once the file
    # being streamed (model.bin, minutes of it) is complete.
    outcome: list = []  # the directory, or what snapshot_download() raised

    def fetch() -> None:
        try:
            outcome.append(snapshot_download(repo_id, cache_dir=str(MODELS_DIR), allow_patterns=list(patterns)))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    worker = threading.Thread(target=fetch, name="download-model", daemon=True)
    worker.start()
    try:
        wait_for_thread(worker)
    except KeyboardInterrupt:
        print("\nDownload interrupted; run setup again to finish it")
        # The pool is still streaming a file, and a normal exit would wait for it (the pool's
        # workers are joined at interpreter shutdown): the process ends from here instead. The
        # partial file stays behind as .incomplete, which the next download resumes.
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        os._exit(2)
    result = outcome[0]
    try:
        if isinstance(result, BaseException):
            raise result
        require(result, name)
    except Exception as exc:  # noqa: BLE001
        if alias and not isinstance(result, BaseException):
            write_config({"model": previous})  # the files came, but they are no model
        print(f"Could not download {name}: {friendly_model_error(exc, name)}")
        return 2
    if not alias:
        write_config({"model": name})
    print(f"Model {name} is ready in {result}.")
    return 0


def load_model(args, name: Optional[str] = None, path: Optional[str] = None):
    """Load `name` (default: --model), picking device and precision for the GPU memory free right now.

    `path` is the directory download_model_files() prepared for `name`; without it WhisperModel
    resolves the name itself, which is fine for the operator's --model (a size, a repo or a folder).
    """
    from faster_whisper import WhisperModel

    name = name or args.model
    device = resolve_device(args.device)
    compute = args.compute_type
    if device == "mlx":
        # MLX knows these two alone; reporting a CTranslate2 precision the GPU never ran would
        # make /health lie about what is loaded.
        if compute not in ("float16", "float32"):
            if compute != "auto":
                log.warning("MLX runs float16 or float32 only; --compute-type %s is ignored.", compute)
            compute = "float16"
        log.info("Loading Whisper model '%s' on the Apple GPU (MLX, %s); models are stored in %s",
                 name, compute, MODELS_DIR)
        model = MlxWhisperModel(path or download_mlx_model_files(name), compute)
        warm_up(model, args)
        return model, device, compute
    if device == "cuda":
        mem = gpu_memory_mb()
        if mem:
            free, total = mem
            log.info("GPU memory: %d MiB free of %d MiB", free, total)
            if compute == "auto":
                compute = "float16" if free >= LOW_VRAM_MB else "int8_float16"
            if free < LOW_VRAM_MB:
                log.warning("Only %d MiB of GPU memory is free, so other applications are holding most of it. "
                            "Using %s weights. Close GPU-heavy apps (games, wallpaper engines, VR software) for best speed and stability.",
                            free, compute)
            if free < CRITICAL_VRAM_MB:
                log.warning("Very little GPU memory is free (%d MiB). Transcription may be slow and the display driver may reset "
                            "under load. Consider closing other GPU apps, or run with --device cpu --model small.", free)
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    log.info("Loading Whisper model '%s' on %s (%s); models are stored in %s", name, device, compute, MODELS_DIR)
    kwargs = {"device": device, "compute_type": compute, "download_root": str(MODELS_DIR)}
    if args.cpu_threads:
        kwargs["cpu_threads"] = args.cpu_threads
    try:
        model = WhisperModel(path or name, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if device != "cuda":
            raise
        log.warning("CUDA initialisation failed (%s). Falling back to CPU int8, which is slow for large models.", exc)
        device, compute = "cpu", "int8"
        kwargs.update(device=device, compute_type=compute)
        model = WhisperModel(path or name, **kwargs)
    warm_up(model, args)
    return model, device, compute


def warm_up(model, args) -> None:
    """Decode two seconds of silence, so the first real window does not pay for the lazy load."""
    try:
        t0 = time.time()
        segs, _ = model.transcribe(np.zeros(SAMPLE_RATE * 2, dtype=np.float32), language=args.language,
                                   beam_size=1, vad_filter=False)
        list(segs)
        log.info("Model ready (warm-up took %.1fs)", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        log.warning("Warm-up transcription failed: %s", exc)


INSTANCE_LOCK = None  # the open, locked file of hold_instance_lock(); lives as long as the process


def instance_lock_path(port: int) -> Path:
    """The file this server holds locked from before its model load until it exits.

    native_host.py (the popup's Start button) tries the same lock: while it is held and /health
    does not answer yet, a server is loading, and the button must not start a second one.
    """
    return APP_DIR / f"server-{port}.lock"


# What the non-blocking lock call raises while another process holds the lock: EWOULDBLOCK /
# EAGAIN from flock(), EACCES (EDEADLOCK after retries) from msvcrt.locking(). Anything else
# (ENOLCK on NFS without a lock manager, EOPNOTSUPP, ENOSYS, EINVAL) means the file cannot be
# locked at all, and must not read as "held": that would stop every start on such a mount.
LOCK_HELD_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLOCK", -1)})


def try_lock(path: Path):
    """Lock `path` for this process, or None when another process holds it; native_host.py has the twin.

    The open file keeps the lock; closing it, or the process ending however it ends, releases it.
    An errno outside LOCK_HELD_ERRNOS (the file cannot be locked at all) raises, like a file
    that cannot be opened, and the callers carry on without the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in LOCK_HELD_ERRNOS:
            return None
        raise
    return handle


def hold_instance_lock(port: int) -> bool:
    """Take the port's instance lock for the life of this process; False when another server holds it.

    Taken before the model load, which is when a second server is most likely to be started
    (the Start button while /health is still silent, a second double-click on run.cmd): it
    stops here instead of loading the model a second time and failing on the port afterwards.
    Without a usable lock file the server starts as it always did.
    """
    global INSTANCE_LOCK
    path = instance_lock_path(port)
    try:
        INSTANCE_LOCK = try_lock(path)
    except OSError as exc:
        log.warning("Cannot use the instance lock %s (%s)", path, exc)
        return True
    return INSTANCE_LOCK is not None


def read_config() -> dict:
    """What setup wrote to config.json ({"model": ...}); {} without a file, or when it holds no JSON object.

    Never raises: a file that cannot be read or parsed costs a warning and the built-in defaults.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Ignoring %s (%s)", CONFIG_PATH, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("Ignoring %s: not a JSON object", CONFIG_PATH)
        return {}
    return data


def write_config(patch: dict) -> None:
    """Merge `patch` into config.json (a None value drops its key), replaced in one step so that a crash never leaves it half written."""
    data = read_config()
    for key, value in patch.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def configured_model():
    """The model chosen at setup (config.json's "model"), or None; anything but a non-empty string is ignored."""
    chosen = read_config().get("model")
    return chosen if isinstance(chosen, str) and chosen else None


def resolve_default_model(args):
    """Fill in --model when the flag was not given: the model chosen at setup, else DEFAULT_MODEL.

    Docker and Nix pass --model (WHISPER_MODEL) and never read config.json. Like the flag, the
    configured name is not validated: it may be a folder, which is the operator's to name.
    """
    if not args.model:
        args.model = configured_model() or DEFAULT_MODEL
    return args


def run_check() -> None:
    print(f"Python {sys.version.split()[0]} at {sys.executable}")
    print(f"Data directory: {APP_DIR}")
    print(f"NVIDIA library directories registered: {len(NVIDIA_DIRS)}")
    try:
        import ctranslate2

        n = ctranslate2.get_cuda_device_count()
        hint = "" if n else ("  -> the Apple GPU runs it instead" if mlx_available()
                             else "  -> CPU fallback; consider --model small")
        print(f"CTranslate2 {ctranslate2.__version__}: {n} CUDA device(s)" + hint)
    except Exception as exc:  # noqa: BLE001
        print(f"CTranslate2 import failed: {exc}")
    if sys.platform == "darwin":
        try:
            import mlx.core as mx
            import mlx_whisper  # noqa: F401

            print(f"MLX {mx.__version__}: Metal {'available' if mx.metal.is_available() else 'unavailable'}")
        except Exception as exc:  # noqa: BLE001
            print(f"MLX not usable ({exc}); Whisper would run on the CPU. Install it with "
                  "pip install mlx-whisper")
    print(f"Backend for --device auto: {resolve_device('auto')}")
    try:
        import faster_whisper

        print(f"faster-whisper {faster_whisper.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"faster-whisper import failed: {exc}")
    try:
        import yt_dlp.version

        print(f"yt-dlp {yt_dlp.version.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"yt-dlp import failed: {exc}")
    runtimes = {name: shutil.which(name) for name in ("deno", "node", "bun")}
    for name, path in runtimes.items():
        print(f"JS runtime {name}: {path or 'not found'}")
    if not any(runtimes.values()):
        print("WARNING: yt-dlp needs Node.js or Deno to download from YouTube.")
    models = sorted(p.name for p in MODELS_DIR.glob("models--*")) if MODELS_DIR.is_dir() else []
    print("Downloaded models: " + (", ".join(models) if models else "none yet (setup or the first start downloads one)"))
    chosen = configured_model()
    print(f"Default model: {chosen or DEFAULT_MODEL} " + ("(chosen at setup)" if chosen else "(built-in default)"))
    # The native-messaging host behind the popup's "Start server" button lives next to this file;
    # loaded by path so a missing or broken native_host.py only costs this one line.
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("shisuko_native_host", Path(__file__).with_name("native_host.py"))
        native_host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native_host)
        print(f"Start button launcher: {native_host.status_text()}")
    except Exception as exc:  # noqa: BLE001
        print(f"Start button launcher: could not check ({exc})")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Shisu-ko: local Whisper transcription server for the Firefox extension")
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep it local)")
    p.add_argument("--port", type=int, default=8790, help="default 8790 (8765 is left free for AnkiConnect)")
    p.add_argument("--model", default=None, help="faster-whisper model size or CTranslate2 repo, e.g. large-v3, large-v3-turbo, kotoba-tech/kotoba-whisper-v2.0-faster (default: the model chosen at setup (config.json), else large-v3)")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "mlx", "cpu"],
                   help="auto picks an NVIDIA GPU, else the Apple GPU through MLX, else the CPU")
    p.add_argument("--compute-type", default="auto", help="float16, int8_float16, int8, ... (auto = float16 on GPU, int8 on CPU; mlx knows float16 and float32)")
    p.add_argument("--language", default="ja")
    p.add_argument("--language-patience", type=float, default=60.0,
                   help="seconds of speech in another language before subtitles stop for that video (0 = never detect, always transcribe)")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--lyrics", default="auto", choices=["auto", "off"],
                   help="auto: a window in which the speech detector hears next to nothing (under a second of speech) "
                        "but the audio is not silent, sung lyrics or speech over music, is transcribed without the "
                        "detector when Whisper hears the target language in it, under stricter gates; off: such "
                        "windows go through the detector as before, blank when it heard nothing")
    p.add_argument("--initial-prompt", default="", help="optional text prompt given to Whisper for every window")
    p.add_argument("--window", type=float, default=40.0, help="seconds of audio transcribed per step (shorter reacts faster to seeking, longer is slightly more efficient)")
    p.add_argument("--first-window", type=float, default=20.0, help="shorter first step after a seek so subtitles appear quickly")
    p.add_argument("--lookahead", type=float, default=900.0, help="stop transcribing this many seconds ahead of the playhead (0 = whole video)")
    p.add_argument("--max-cue-chars", type=int, default=30, help="26 is the Netflix Japanese limit (13 x 2 lines); 30 keeps more mined sentences whole")
    p.add_argument("--max-cue-seconds", type=float, default=7.0)
    p.add_argument("--min-cue-seconds", type=float, default=0.8, help="cues shorter than this are extended or merged")
    p.add_argument("--idle-minutes", type=int, default=30, help="release decoded audio of videos not synced for this long")
    p.add_argument("--retry-after", type=float, default=30.0, help="seconds before a failed audio fetch is retried automatically, and the least time between two attempts to load a model that failed to download or load")
    p.add_argument("--client-timeout", type=float, default=30.0, help="stop transcribing ahead for a video whose tab has not synced for this many seconds (0 = never stop)")
    p.add_argument("--cpu-threads", type=int, default=0)
    p.add_argument("--cookies-from-browser", default="", help="e.g. firefox, for age-restricted or members-only videos")
    p.add_argument("--cookies", default="", help="path to a Netscape-format cookies.txt for yt-dlp (use this inside Docker, e.g. /data/cookies.txt)")
    p.add_argument("--js-runtime", default="auto", help="JS runtime for yt-dlp: auto, node, deno, bun, or name:path")
    p.add_argument("--allow-remote-ejs", action="store_true", help="let yt-dlp fetch updated challenge-solver scripts from GitHub")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--check", action="store_true", help="print environment diagnostics and exit")
    p.add_argument("--download-model", metavar="NAME", help="download NAME now, showing progress, and make it the default model for later starts; used by setup")
    p.add_argument("--no-update", action="store_true", help="start without looking for a newer version first (run.cmd / run.sh skip server/update.py) and refuse the popup's Update button (POST /update answers 409), since the launcher would restart the server without updating")
    return resolve_default_model(p.parse_args(argv))


def main() -> None:
    args = parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    for noisy in ("httpx", "httpcore", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # huggingface_hub reads HF_HUB_VERBOSITY (set above) when it configures its logger on
    # import, later than this; the same level here covers the records logged before that.
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if args.check:
        run_check()
        return
    if getattr(args, "download_model", None) is not None:
        sys.exit(run_download_model(args.download_model, args.device))

    if not hold_instance_lock(args.port):
        log.error("Another server is already starting or running on port %d (it holds %s). Stop it first.",
                  args.port, instance_lock_path(args.port))
        sys.exit(2)
    try:
        # No local name for the model: the switch frees it through App alone (see switch_model_if_wanted).
        app = App(args, *load_model(args))
    except Exception as exc:  # noqa: BLE001
        log.error("Could not load the model '%s': %s", args.model, exc)
        sys.exit(2)
    Handler.app = app
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        log.error("Cannot listen on %s:%d (%s). Is another server already running?", args.host, args.port, exc)
        sys.exit(2)
    server.daemon_threads = True
    log.info("Listening on http://%s:%d  (Ctrl+C to stop)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        server.server_close()
    if app.exit_code is not None:
        # POST /update: the launcher reads EXIT_UPDATE as "run update.py, then start again". Every
        # worker is a daemon thread, so the interpreter does not wait for a window to finish.
        sys.exit(app.exit_code)


if __name__ == "__main__":
    main()
