"""Kitsune-Transcribe models in the Shisu-ko server: the engine behind the kitsune-* model names.

Kitsune-Transcribe (https://github.com/Multysquid/Kitsune-Transcribe) distils Cohere Transcribe
into small Japanese-only students of two families:
- "aed": CohereAsrForConditionalGeneration, a FastConformer encoder with a Transformer decoder
  (the Transcribe students);
- "ctc": ParakeetForCTC, a FastConformer encoder with a CTC head (the Parakeet students).
Neither is a Whisper model, so CTranslate2 cannot run them: they run on PyTorch and
transformers. server.py imports this file (by path, see server.kitsune_engine()) only when such
a model is loaded, and this file imports torch only inside the functions that need it, so the
server and the pure helpers here stay importable, and testable, without it.

A package is a folder transformers can read (config.json, the processor and tokenizer files)
holding either
- model.safetensors with plain bf16 or fp16 weights (a training run's export), or
- a quantised variant as `python -m kitsune.quant export` writes it: model.safetensors with the
  packed weights of every quantised layer (<layer>.qweight, .qscale, .qblock_scale,
  .qtensor_scale, .bias) and every other tensor as it was, plus quantization.json (schema 1),
  the recipe that names the layers.

Quantised weights are unpacked once, at load, into the compute dtype: bf16 on a GPU that has
it, else fp16, and fp32 on the CPU. So every format runs on every machine with exactly the
numbers its weights hold. The activations stay 16-bit (the W8A8 / W4A4 formats' activation
quantisation is not emulated), the GPU memory is the 16-bit model's and the speed plain
PyTorch's: what a smaller format saves here is the download.

Word timings: faster-whisper hands the server words with start and end times, which the cue
builder needs, and Kitsune's students emit none. A CTC student's come from its greedy path (one
encoder frame is 80 ms). A Transcribe student's come from its decoder's cross-attention,
aligned to the encoder frames by dynamic time warping, as Whisper's own word timestamps are.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

log = logging.getLogger("shisuko")

SAMPLE_RATE = 16000
FRAME_S = 0.08  # one encoder frame of both families: 10 ms mel hop, 8x subsampling
ARCHITECTURES = {"CohereAsrForConditionalGeneration": "aed", "ParakeetForCTC": "ctc"}
QUANT_FILE = "quantization.json"
QUANT_SCHEMA = 1
WEIGHTS_FILE = "model.safetensors"
# quantization.json's "weights": how the quantised layers' weights are stored.
WEIGHT_FORMATS = ("fp16", "int8", "fp8", "nvfp4", "mxfp4")
PACKED_SUFFIXES = ("qweight", "qscale", "qblock_scale", "qtensor_scale")

# The Transcribe students' decoder prompt (Japanese, punctuation, no ITN: the teacher pass's
# decoder_prompt_ids, kitsune.trainset.PROMPT) and its special tokens (kitsune.student).
AED_PROMPT = (13764, 7, 4, 16, 98, 98, 5, 9, 11, 13)
AED_EOS, AED_PAD = 3, 2
CTC_BLANK = 3072  # ParakeetForCTC's blank, which is also its pad token

MAX_CHUNK_S = 28.0   # the Cohere feature extractor splits audio above 30 s; the teacher pass never saw more
CHUNK_GAP_S = 1.5    # speech after a longer pause than this starts a chunk of its own
CHUNK_PAD_S = 0.2    # audio kept on each side of a chunk's speech, so a first or last syllable is whole
SEGMENT_GAP_S = 1.0  # a pause this long between two words ends a segment
CTC_TAIL_S = 0.32    # a CTC word's end: its last frame plus up to this much, never past the next word
MEDIAN_WIDTH = 7     # frames of the median filter over the cross-attention (Whisper's value)
WORD_BASE_S = 0.5    # an aligned word lasts at most this ...
WORD_CHAR_S = 0.35   # ... plus this per character that is no punctuation (kanji run ~0.3 s, kana less)
SENTENCE_END = set("。！？!?…")
PUNCTUATION = SENTENCE_END | set("、，,.:：;；「」『』（）()[]〜~・\"'")  # not ー: a long vowel is spoken

E2M1_VALUES = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0], dtype=np.float32)


def _e4m3_table() -> np.ndarray:
    """float8_e4m3fn by byte: 1 sign, 4 exponent (bias 7), 3 mantissa bits; S.1111.111 is NaN, no infinity."""
    table = np.zeros(256, dtype=np.float32)
    for b in range(256):
        sign = -1.0 if b & 0x80 else 1.0
        exp, man = (b >> 3) & 0xF, b & 0x7
        if exp == 0xF and man == 0x7:
            table[b] = np.nan
        elif exp == 0:
            table[b] = sign * (man / 8.0) * 2.0 ** -6
        else:
            table[b] = sign * (1.0 + man / 8.0) * 2.0 ** (exp - 7)
    return table


E4M3_VALUES = _e4m3_table()


# --------------------------------------------------------------------------- packages

class PackageError(ValueError):
    """A folder that is not a Kitsune package this engine can load."""


@dataclass
class Package:
    path: str
    family: str          # "aed" or "ctc"
    architecture: str    # the transformers class
    weights: str         # "bf16" (a plain export, whatever its file dtype), or one of WEIGHT_FORMATS
    fmt: str             # quantization.json's "format" (int8-w8a16, ...), or "bf16"
    recipe: Optional[dict] = None


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise PackageError(f"{path} does not hold a JSON object")
    return data


def architecture_of(path: str) -> Optional[str]:
    """The Kitsune architecture config.json names, or None (no config, another model, unreadable)."""
    try:
        archs = read_json(os.path.join(path, "config.json")).get("architectures") or []
    except (OSError, ValueError):
        return None
    return next((a for a in archs if a in ARCHITECTURES), None)


def read_package(path: str) -> Package:
    """What `path` holds, checked before torch is imported: a missing file fails here, in one line."""
    arch = architecture_of(path)
    if arch is None:
        raise PackageError(f"{path} is not a Kitsune-Transcribe model: its config.json names neither "
                           + " nor ".join(ARCHITECTURES))
    if not os.path.isfile(os.path.join(path, WEIGHTS_FILE)):
        raise PackageError(f"{path} has no {WEIGHTS_FILE}")
    recipe_path = os.path.join(path, QUANT_FILE)
    if not os.path.isfile(recipe_path):
        return Package(path, ARCHITECTURES[arch], arch, "bf16", "bf16")
    recipe = read_json(recipe_path)
    if recipe.get("schema") != QUANT_SCHEMA:
        raise PackageError(f"{recipe_path}: schema {recipe.get('schema')!r}, this server reads schema {QUANT_SCHEMA}")
    weights = recipe.get("weights")
    if weights not in WEIGHT_FORMATS:
        raise PackageError(f"{recipe_path}: unknown weight format {weights!r}")
    if not isinstance(recipe.get("layers", {}), dict):
        raise PackageError(f"{recipe_path}: 'layers' is not an object")
    return Package(path, ARCHITECTURES[arch], arch, weights, str(recipe.get("format") or weights), recipe)


# --------------------------------------------------------------------------- dequantisation (numpy)

def unpack_nibbles(packed: np.ndarray) -> np.ndarray:
    """(N, K/2) uint8 -> (N, K) codes: element 2i in the low nibble, 2i+1 in the high one."""
    packed = np.asarray(packed, dtype=np.uint8)
    out = np.empty((packed.shape[0], packed.shape[1] * 2), dtype=np.uint8)
    out[:, 0::2] = packed & 0x0F
    out[:, 1::2] = packed >> 4
    return out


def e2m1_decode(codes: np.ndarray) -> np.ndarray:
    """FP4 E2M1 codes -> float32: bits 0-2 index {0, .5, 1, 1.5, 2, 3, 4, 6}, bit 3 is the sign."""
    codes = np.asarray(codes, dtype=np.uint8)
    values = E2M1_VALUES[codes & 0x7]
    return np.where(codes & 0x8, -values, values).astype(np.float32)


def dequantize(weights: str, shape: Sequence[int], qweight: np.ndarray, qscale: Optional[np.ndarray] = None,
               qblock_scale: Optional[np.ndarray] = None, qtensor_scale=None) -> np.ndarray:
    """A quantised layer's (N, K) weight in float32, from its packed tensors as kitsune.quant stores them.

    fp8 weights and nvfp4 block scales come as their raw bytes (uint8): numpy has no float8.
    int8 and fp8 scale each output row (qscale), nvfp4 each block of 16 along K (an E4M3 scale)
    times the tensor's FP32 scale, mxfp4 each block of 32 by a power of two (E8M0, bias 127).
    """
    n, k = (int(x) for x in shape)
    if weights == "int8":
        w = np.asarray(qweight, dtype=np.int8).astype(np.float32) * np.asarray(qscale, np.float32).reshape(n, 1)
    elif weights == "fp8":
        w = E4M3_VALUES[np.asarray(qweight, dtype=np.uint8)] * np.asarray(qscale, np.float32).reshape(n, 1)
    elif weights == "nvfp4":
        values = e2m1_decode(unpack_nibbles(qweight))
        block = np.repeat(E4M3_VALUES[np.asarray(qblock_scale, dtype=np.uint8)], 16, axis=1)
        w = values * block * np.float32(np.asarray(qtensor_scale, dtype=np.float32).reshape(()))
    elif weights == "mxfp4":
        exps = np.asarray(qblock_scale, dtype=np.uint8).astype(np.int32) - 127
        block = np.repeat(np.ldexp(np.ones(exps.shape, dtype=np.float32), exps), 32, axis=1)
        w = e2m1_decode(unpack_nibbles(qweight)) * block
    else:
        raise PackageError(f"cannot unpack weight format {weights!r}")
    if w.shape != (n, k):
        raise PackageError(f"unpacked weight has shape {w.shape}, the recipe says {(n, k)}")
    return w.astype(np.float32, copy=False)


# --------------------------------------------------------------------------- timings (numpy)

@dataclass
class CtcSpan:
    token: int
    first: int      # first frame of the token's run
    last: int       # last frame of the run
    probability: float


def ctc_spans(log_probs: np.ndarray, blank: int = CTC_BLANK) -> list:
    """The greedy CTC path as runs: argmax per frame, a run per repeated token, blanks dropped.

    The ids are kitsune.ctc_student.greedy_ctc_ids's; each run keeps its frames and the mean
    probability of its argmax.
    """
    lp = np.asarray(log_probs, dtype=np.float32)
    if lp.ndim != 2 or not len(lp):
        return []
    arg = lp.argmax(axis=1)
    best = np.exp(lp[np.arange(len(arg)), arg])
    spans: list = []
    t = 0
    while t < len(arg):
        tok = int(arg[t])
        u = t
        while u + 1 < len(arg) and int(arg[u + 1]) == tok:
            u += 1
        if tok != blank:
            spans.append(CtcSpan(tok, t, u, float(best[t:u + 1].mean())))
        t = u + 1
    return spans


def dtw_path(cost: np.ndarray) -> tuple:
    """Whisper's DTW: the monotonic path from (0, 0) to (N-1, M-1) through `cost` (N tokens x M frames)."""
    n, m = cost.shape
    acc = np.full((n + 1, m + 1), np.inf, dtype=np.float64)
    trace = np.full((n + 1, m + 1), -1, dtype=np.int8)
    acc[0, 0] = 0.0
    for j in range(1, m + 1):
        for i in range(1, n + 1):
            c0, c1, c2 = acc[i - 1, j - 1], acc[i - 1, j], acc[i, j - 1]
            if c0 <= c1 and c0 <= c2:
                c, t = c0, 0
            elif c1 <= c2:
                c, t = c1, 1
            else:
                c, t = c2, 2
            acc[i, j] = cost[i - 1, j - 1] + c
            trace[i, j] = t
    i, j = n, m
    trace[0, :] = 2
    trace[:, 0] = 1
    rows, cols = [], []
    while i > 0 or j > 0:
        rows.append(i - 1)
        cols.append(j - 1)
        t = trace[i, j]
        if t == 0:
            i, j = i - 1, j - 1
        elif t == 1:
            i -= 1
        else:
            j -= 1
    return np.array(rows[::-1]), np.array(cols[::-1])


def median_filter(x: np.ndarray, width: int) -> np.ndarray:
    """A median over `width` frames along the last axis, reflect-padded as Whisper's."""
    if width <= 1 or x.shape[-1] < 2:
        return x
    pad = width // 2
    if x.shape[-1] <= pad:
        return x
    padded = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(pad, pad)], mode="reflect")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width, axis=-1)
    return np.median(windows, axis=-1)


def attention_boundaries(weights: np.ndarray, n_tokens: int, width: int = MEDIAN_WIDTH) -> np.ndarray:
    """Token boundaries, in frames, from cross-attention (H heads x R rows x M frames).

    Row j is the attention of the step that predicted token j; R is n_tokens + 1 when the row of
    the end token is there, which lets the last token end where the end token takes over. Each
    head is normalised over the tokens, median filtered along the frames and the heads averaged,
    then DTW finds the path (as Whisper's find_alignment). Returns n_tokens + 1 frame indices:
    token j spans [b[j], b[j + 1]).
    """
    w = np.asarray(weights, dtype=np.float32)
    rows, frames = w.shape[1], w.shape[2]
    if n_tokens <= 0 or frames <= 0:
        return np.zeros(max(n_tokens, 0) + 1, dtype=np.int64)
    std = w.std(axis=1, keepdims=True)
    w = (w - w.mean(axis=1, keepdims=True)) / np.where(std > 0, std, 1.0)
    matrix = median_filter(w, width).mean(axis=0)
    text_idx, time_idx = dtw_path(-matrix.astype(np.float64))
    jumps = np.pad(np.diff(text_idx), (1, 0), constant_values=1).astype(bool)
    starts = time_idx[jumps]  # the first frame of each row
    bounds = np.empty(n_tokens + 1, dtype=np.int64)
    bounds[:n_tokens] = starts[:n_tokens]
    bounds[n_tokens] = starts[n_tokens] if rows > n_tokens else frames
    return np.maximum.accumulate(bounds)


def group_tokens(ids: Sequence[int], decode: Callable[[list], str]) -> list:
    """(text, [token indices]) per word: each token its own word, except that a character split
    over byte-fallback tokens stays in one; the texts are the decoded prefix's increments, so
    spaces and joins come out as the tokenizer writes them."""
    words: list = []
    done_text, start = "", 0
    for i in range(len(ids)):
        text = decode(list(ids[:i + 1]))
        if text.endswith("�") and i + 1 < len(ids):
            continue  # a character still incomplete: the next token finishes it
        piece = text[len(done_text):] if text.startswith(done_text) else text
        if piece:
            words.append((piece, list(range(start, i + 1))))
        done_text, start = text, i + 1
    return words


# --------------------------------------------------------------------------- the faster-whisper shape

@dataclass
class Word:
    word: str
    start: float
    end: float
    probability: float


@dataclass
class Segment:
    id: int
    seek: int
    start: float
    end: float
    text: str
    tokens: list
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float
    words: list
    temperature: float = 0.0


@dataclass
class Info:
    language: str = "ja"
    language_probability: float = 1.0
    duration: float = 0.0
    duration_after_vad: float = 0.0
    all_language_probs: Optional[list] = None
    transcription_options: Optional[dict] = None
    vad_options: Optional[dict] = None


def plan_chunks(speech: Sequence, duration: float, max_s: float = MAX_CHUNK_S, gap_s: float = CHUNK_GAP_S,
                pad_s: float = CHUNK_PAD_S) -> list:
    """The spans (seconds) decoded one by one: speech intervals joined across pauses up to
    `gap_s` while the span stays within `max_s`, a longer interval cut into equal parts, each
    span padded by `pad_s` without running into its neighbours or out of the audio."""
    spans: list = []
    for a, b in sorted((float(a), float(b)) for a, b in speech):
        a, b = max(0.0, a), min(duration, b)
        if b <= a:
            continue
        if spans and a - spans[-1][1] <= gap_s and b - spans[-1][0] <= max_s:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    cut: list = []
    for a, b in spans:
        parts = max(1, math.ceil((b - a) / max_s))
        step = (b - a) / parts
        cut += [[a + i * step, a + (i + 1) * step] for i in range(parts)]
    out = []
    for i, (a, b) in enumerate(cut):
        lo = 0.0 if i == 0 else (cut[i - 1][1] + a) / 2
        hi = duration if i == len(cut) - 1 else (b + cut[i + 1][0]) / 2
        out.append((max(lo, a - pad_s), min(hi, b + pad_s)))
    return out


def split_segments(words: Sequence, gap_s: float = SEGMENT_GAP_S) -> list:
    """Words into segments: a sentence mark ends one, and so does a pause of `gap_s` or more."""
    out, cur = [], []
    for w in words:
        if cur and w.start - cur[-1].end >= gap_s:
            out.append(cur)
            cur = []
        cur.append(w)
        text = w.word.strip()
        if text and text[-1] in SENTENCE_END:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def make_segments(words: Sequence, first_id: int = 0) -> list:
    """faster-whisper-shaped segments of timed words."""
    segments = []
    for i, group in enumerate(split_segments(words)):
        text = "".join(w.word for w in group)
        probs = [max(w.probability, 1e-6) for w in group]
        segments.append(Segment(
            id=first_id + i, seek=0, start=group[0].start, end=group[-1].end, text=text, tokens=[],
            avg_logprob=float(np.mean(np.log(probs))), compression_ratio=1.0, no_speech_prob=0.0,
            words=list(group)))
    return segments


def ctc_words(spans: Sequence, decode: Callable[[list], str], end_frame: int) -> list:
    """Timed words (seconds from the chunk's start) of a CTC path's runs."""
    ids = [s.token for s in spans]
    words = []
    groups = group_tokens(ids, decode)
    for n, (text, idx) in enumerate(groups):
        first, last = spans[idx[0]], spans[idx[-1]]
        nxt = spans[groups[n + 1][1][0]].first if n + 1 < len(groups) else end_frame
        end = min(nxt, last.last + 1 + int(round(CTC_TAIL_S / FRAME_S)))
        words.append(Word(text, first.first * FRAME_S, max(end, last.last + 1) * FRAME_S,
                          float(np.mean([spans[i].probability for i in idx]))))
    return words


def longest_word_s(text: str) -> float:
    """The longest a word of `text` may last: WORD_BASE_S plus WORD_CHAR_S per character that is no punctuation."""
    return WORD_BASE_S + WORD_CHAR_S * sum(1 for ch in text.strip() if ch not in PUNCTUATION)


def cap_durations(words: Sequence) -> list:
    """Words no longer than longest_word_s(): the alignment gives a word every frame up to the
    next one, so a word before a pause, or a first word after unwritten sound, spans it all.
    A first word, or one after a sentence mark or a comma, keeps its end (the pause lay before
    it); any other keeps its start (the pause follows it)."""
    out = []
    for i, w in enumerate(words):
        cap = longest_word_s(w.word)
        start, end = w.start, w.end
        if end - start > cap:
            prev = words[i - 1].word.strip() if i else ""
            if i == 0 or (prev and prev[-1] in PUNCTUATION):
                start = end - cap
            else:
                end = start + cap
        out.append(Word(w.word, start, end, w.probability))
    return out


def aed_words(ids: Sequence[int], probs: Sequence[float], bounds: np.ndarray, decode: Callable[[list], str]) -> list:
    """Timed words (seconds from the chunk's start) of a decoded token sequence and its token boundaries."""
    words = []
    for text, idx in group_tokens(ids, decode):
        start, end = int(bounds[idx[0]]), int(bounds[idx[-1] + 1])
        words.append(Word(text, start * FRAME_S, max(end, start + 1) * FRAME_S,
                          float(np.mean([probs[i] for i in idx]))))
    return cap_durations(words)


# --------------------------------------------------------------------------- torch side

def pick_device(device: str) -> str:
    import torch

    if device in ("auto", "cuda") and torch.cuda.is_available():
        return "cuda"
    if device == "cuda":
        log.warning("PyTorch sees no CUDA device (a CPU-only build, or no NVIDIA GPU); the Kitsune model runs on the CPU")
    return "cpu"


def pick_dtype(compute: str, device: str, weights: str):
    """The compute dtype: --compute-type when it names one, else bf16 on a GPU that has it (the
    dtype the students were trained and evaluated in; fp16 for an fp16 variant), fp32 on the CPU."""
    import torch

    named = {"float16": torch.float16, "fp16": torch.float16, "bfloat16": torch.bfloat16, "bf16": torch.bfloat16,
             "float32": torch.float32, "fp32": torch.float32}
    if compute in named:
        dtype = named[compute]
        if device == "cpu" and dtype != torch.float32:
            log.warning("--compute-type %s on the CPU: Kitsune models run in float32 there", compute)
            return torch.float32
        return dtype
    if compute not in ("auto", "", None):
        log.warning("--compute-type %s means nothing to a Kitsune model (its precision is in its name, e.g. "
                    "kitsune-0.6b-int8); using the automatic choice", compute)
    if device == "cpu":
        return torch.float32
    if weights != "fp16" and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float16


def packed_array(tensor) -> np.ndarray:
    """A packed tensor from safetensors as numpy: float8 as its raw bytes, the rest as it is."""
    import torch

    if tensor.dtype in (torch.float8_e4m3fn, getattr(torch, "float8_e8m0fnu", None)):
        return tensor.contiguous().view(torch.uint8).numpy()
    if tensor.dtype in (torch.bfloat16, torch.float16):
        return tensor.float().numpy()
    return tensor.numpy()


def variant_state_dict(pkg: Package) -> dict:
    """The HF state dict of a quantised variant, its packed layers unpacked to float32 weights."""
    import torch
    from safetensors import safe_open

    layers = pkg.recipe.get("layers") or {}
    out = {}
    with safe_open(os.path.join(pkg.path, WEIGHTS_FILE), framework="pt") as f:
        keys = set(f.keys())
        packed = set()
        for name, info in layers.items():
            names = {s: f"{name}.{s}" for s in PACKED_SUFFIXES}
            if names["qweight"] not in keys:
                raise PackageError(f"{pkg.path}: the recipe names {name}, the file has no {names['qweight']}")
            try:
                parts = {s: packed_array(f.get_tensor(k)) for s, k in names.items() if k in keys}
                shape = info.get("shape") or []
                w = dequantize(pkg.weights, shape, parts["qweight"], parts.get("qscale"), parts.get("qblock_scale"),
                               parts.get("qtensor_scale"))
                t = torch.from_numpy(w)
                hf = name
                if info.get("kind") == "pointwise_conv1d":
                    hf = name[: -len(".linear")] if name.endswith(".linear") else name
                    t = t.reshape([int(x) for x in (info.get("orig_shape") or list(shape) + [1])])
            except PackageError:
                raise
            except (ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError) as exc:
                # A recipe that does not fit its tensors is the package's fault: said as such, it is
                # reported at once instead of being taken for a GPU failure and loaded again on the CPU.
                raise PackageError(f"{pkg.path}: layer {name} does not unpack ({exc})") from exc
            out[f"{hf}.weight"] = t
            packed |= set(names.values())
            if f"{name}.bias" in keys:
                out[f"{hf}.bias"] = f.get_tensor(f"{name}.bias")
                packed.add(f"{name}.bias")
        for key in keys - packed:
            out[key] = f.get_tensor(key)
    return out


def build_model(pkg: Package):
    """The transformers model of `pkg` on the CPU in float32, with sdpa attention, in eval mode."""
    import torch
    import transformers

    cls = getattr(transformers, pkg.architecture)
    if pkg.recipe is None or pkg.weights == "fp16":
        # A plain export, or the fp16 variant, whose keys and shapes are HF's own.
        model = cls.from_pretrained(pkg.path, dtype=torch.float32, attn_implementation="sdpa")
    else:
        config = transformers.AutoConfig.from_pretrained(pkg.path)
        try:
            from transformers.initialization import no_init_weights
        except ImportError:  # an older layout: initialising 0.6B weights only to overwrite them is slow, not wrong
            from contextlib import nullcontext as no_init_weights
        with no_init_weights():
            model = cls._from_config(config, attn_implementation="sdpa", dtype=torch.float32)
        state = variant_state_dict(pkg)
        missing, unexpected = model.load_state_dict(state, strict=False)
        tied = set((pkg.recipe.get("tied") or {}).keys()) | set(getattr(model, "_tied_weights_keys", None) or {})
        missing = [k for k in missing if k not in tied]
        if missing or unexpected:
            raise PackageError(f"{pkg.path}: the weights do not fit {pkg.architecture} "
                               f"(missing {missing[:3]}, unexpected {list(unexpected)[:3]})")
        model.tie_weights()
    for sub in (model.config, getattr(model.config, "encoder_config", None)):
        if sub is not None and getattr(sub, "_attn_implementation", "sdpa") != "sdpa":
            sub._attn_implementation = "sdpa"
    return model.eval()


def cast_for_inference(model, dtype) -> None:
    """Linear, conv and embedding weights to `dtype`; norms and BatchNorm stay float32 and autocast
    does the rest (kitsune.quant's fp16 recipe). A tied head keeps its tie: one Parameter, cast once."""
    import torch
    from torch import nn

    if dtype == torch.float32:
        return
    for module in model.modules():
        if isinstance(module, (nn.Linear, nn.Conv1d, nn.Conv2d, nn.Embedding)):
            for p in module.parameters(recurse=False):
                p.data = p.data.to(dtype)


class fp32_head:
    """An AED model's proj_out computed in fp32 outside autocast while decoding, as kitsune.evaluate's
    greedy_eval does (_FP32Head): the greedy argmax and the word probabilities over unrounded
    logits. The tied weight is read, not copied."""

    def __init__(self, model):
        self.model = model
        self.inner = None

    def __enter__(self):
        import torch

        inner = self.inner = self.model.proj_out

        class Head(torch.nn.Module):
            def forward(self, h):
                with torch.autocast(device_type=h.device.type, enabled=False):
                    b = inner.bias.float() if inner.bias is not None else None
                    return torch.nn.functional.linear(h.float(), inner.weight.float(), b)

        self.model.proj_out = Head()
        return self.model

    def __exit__(self, *exc):
        self.model.proj_out = self.inner
        return False


class RepetitionStop:
    """Stop once the last `window` generated tokens repeat with a period up to `max_period`: a
    decoder loop (kitsune.generation.RepetitionStop, the teacher pass's own guard)."""

    def __init__(self, prompt_len: int, window: int = 24, max_period: int = 12):
        self.prompt_len, self.window, self.max_period = prompt_len, window, max_period

    def __call__(self, input_ids, scores, **kwargs):
        import torch

        gen = input_ids[:, self.prompt_len:]
        done = torch.zeros(gen.shape[0], dtype=torch.bool, device=gen.device)
        if gen.shape[1] < self.window:
            return done
        last = gen[:, -self.window:]
        for p in range(1, self.max_period + 1):
            done |= (last[:, p:] == last[:, :-p]).all(dim=1)
        return done


class KitsuneModel:
    """A Kitsune student behind faster-whisper's WhisperModel interface, as far as server.py uses it."""

    detects_language = False  # Japanese only, and no language head: the server's language watch is off
    sings = False             # no lyrics path: its gates read Whisper's own confidence figures
    takes_prompt = False      # the decoder prompt is fixed (AED_PROMPT); --initial-prompt is Whisper's

    def __init__(self, pkg: Package, model, processor, device: str, dtype):
        self.pkg = pkg
        self.model = model
        self.processor = processor
        self.feature_extractor = processor.feature_extractor
        self.tokenizer = processor.tokenizer
        self.device = device
        self.dtype = dtype
        self.family = pkg.family
        self.blank = int(getattr(model.config, "pad_token_id", None) or CTC_BLANK) if pkg.family == "ctc" else None

    @property
    def compute_label(self) -> str:
        dtype = str(self.dtype).replace("torch.", "")
        return dtype if self.pkg.weights == "bf16" else f"{self.pkg.fmt} weights, {dtype}"

    def decode_text(self, ids: list) -> str:
        if self.family == "ctc":
            # The label pass's detokenisation: collapsed ids, no grouping of repeats (kitsune.ctc_student.decode_ids).
            return self.tokenizer.decode(ids, skip_special_tokens=True, group_tokens=False)
        return self.tokenizer.decode(ids, skip_special_tokens=True)

    def detect_language(self, audio=None, **kwargs):
        raise NotImplementedError("Kitsune-Transcribe models transcribe Japanese only and have no language head")

    def autocast(self):
        import torch
        from contextlib import nullcontext

        if self.dtype == torch.float32:
            return nullcontext()
        return torch.autocast(device_type=self.device, dtype=self.dtype)

    def features(self, samples: np.ndarray) -> tuple:
        fe = self.feature_extractor(samples, sampling_rate=SAMPLE_RATE, return_tensors="pt", return_attention_mask=True)
        feats = fe["input_features"].to(self.device)
        mask = fe.get("attention_mask")
        mask = mask.to(self.device) if mask is not None else None
        return feats, mask

    def transcribe(self, audio, language: Optional[str] = None, vad_filter: bool = True,
                   vad_parameters: Optional[dict] = None, **_whisper_options):
        """(segments, info) like WhisperModel.transcribe(): the audio's speech, cut into chunks of
        at most MAX_CHUNK_S by the same Silero detector, each decoded greedily, as the students
        were evaluated. Whisper's decoding options (beam size, temperatures, prompt, ...) have no
        counterpart here and are ignored."""
        if language not in (None, "", "ja"):
            raise ValueError(f"Kitsune-Transcribe models transcribe Japanese only, not '{language}'")
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        duration = len(audio) / SAMPLE_RATE
        if vad_filter:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            options = dict(vad_parameters or {})
            options.setdefault("max_speech_duration_s", MAX_CHUNK_S)
            chunks = get_speech_timestamps(audio, VadOptions(**options), sampling_rate=SAMPLE_RATE)
            speech = [(c["start"] / SAMPLE_RATE, c["end"] / SAMPLE_RATE) for c in chunks]
        else:
            speech = [(0.0, duration)] if duration > 0 else []
        segments: list = []
        for a, b in plan_chunks(speech, duration):
            samples = audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)]
            if len(samples) < SAMPLE_RATE // 10:
                continue
            words = [Word(w.word, round(a + w.start, 3), round(a + min(w.end, b - a), 3), w.probability)
                     for w in self.decode_chunk(samples)]
            segments += make_segments(words, first_id=len(segments))
        info = Info(duration=duration, duration_after_vad=sum(b - a for a, b in speech))
        return segments, info

    def decode_chunk(self, samples: np.ndarray) -> list:
        import torch

        with torch.inference_mode(), self.autocast():
            if self.family == "ctc":
                return self.decode_ctc(samples)
            return self.decode_aed(samples)

    def decode_ctc(self, samples: np.ndarray) -> list:
        import torch

        feats, mask = self.features(samples)
        if mask is None:
            mask = torch.ones(feats.shape[:2], dtype=torch.long, device=self.device)
        enc = self.model.encoder(input_features=feats, attention_mask=mask.long(), output_attention_mask=True)
        h = enc.last_hidden_state
        # The CTC head in fp32 outside autocast, log-softmax as z - logsumexp(z) (kitsune.ctc_student.ctc_log_probs).
        with torch.autocast(device_type=self.device, enabled=False):
            head = self.model.ctc_head
            z = torch.nn.functional.conv1d(h.float().transpose(1, 2), head.weight.float(),
                                           head.bias.float() if head.bias is not None else None).transpose(1, 2)
            lp = z - torch.logsumexp(z, dim=-1, keepdim=True)
        frames = int(enc.attention_mask.sum()) if getattr(enc, "attention_mask", None) is not None else lp.shape[1]
        spans = ctc_spans(lp[0, :frames].cpu().numpy(), self.blank)
        return ctc_words(spans, self.decode_text, frames)

    def decode_aed(self, samples: np.ndarray) -> list:
        with fp32_head(self.model):
            return self._decode_aed(samples)

    def _decode_aed(self, samples: np.ndarray) -> list:
        import torch
        from transformers import StoppingCriteriaList

        feats, mask = self.features(samples)
        model = self.model
        enc = model.model.encoder(feats, attention_mask=mask)
        prompt = list(AED_PROMPT)
        p = len(prompt)
        limit = int(getattr(model.config, "max_position_embeddings", 1024) or 1024)
        max_new = max(1, min(int(16 + 10 * len(samples) / SAMPLE_RATE), limit - p - 1))
        prompt_ids = torch.tensor([prompt], dtype=torch.long, device=self.device)
        seq = model.generate(encoder_outputs=enc, attention_mask=mask, decoder_input_ids=prompt_ids,
                             max_new_tokens=max_new, do_sample=False, num_beams=1, eos_token_id=AED_EOS,
                             pad_token_id=AED_PAD, stopping_criteria=StoppingCriteriaList([RepetitionStop(p)]))
        seq = seq.sequences if hasattr(seq, "sequences") else seq
        gen = seq[0, p:].tolist()
        stops = [i for i, t in enumerate(gen) if t in (AED_EOS, AED_PAD)]
        ended = bool(stops) and gen[stops[0]] == AED_EOS
        ids = gen[:stops[0]] if stops else gen
        ids = [t for t in ids if t not in self.tokenizer.all_special_ids]
        if not ids:
            return []
        weights, logits = self.cross_attention(enc, prompt + ids)
        lp = torch.log_softmax(logits.float(), dim=-1)
        probs = [float(lp[p - 1 + j, t].exp()) for j, t in enumerate(ids)]
        frames = int(enc.attention_mask.sum()) if getattr(enc, "attention_mask", None) is not None else weights.shape[-1]
        rows = len(ids) + 1 if ended else len(ids)
        w = weights[:, p - 1:p - 1 + rows, :frames]
        bounds = attention_boundaries(w, len(ids))
        return aed_words(ids, probs, bounds, self.decode_text)

    def cross_attention(self, enc, ids: list) -> tuple:
        """(weights (layers x heads, rows, frames) as numpy, logits (rows, vocab)) of one teacher-forced
        pass over `ids`. The attention is recomputed from the q/k projections the pass runs, so it
        does not depend on the attention backend (sdpa returns no weights). The upper half of the
        decoder layers counts, as Whisper's default alignment heads do."""
        import torch

        model = self.model
        layers = list(model.model.decoder.layers)
        chosen = layers[len(layers) // 2:]
        captured: list = []
        hooks = []
        for layer in chosen:
            att = layer.encoder_attn
            store: dict = {}
            captured.append((att, store))
            hooks.append(att.q_proj.register_forward_hook(lambda m, i, o, s=store: s.__setitem__("q", o)))
            hooks.append(att.k_proj.register_forward_hook(lambda m, i, o, s=store: s.__setitem__("k", o)))
        try:
            dec = torch.tensor([ids], dtype=torch.long, device=self.device)
            out = model(encoder_outputs=enc, decoder_input_ids=dec, use_cache=False)
        finally:
            for h in hooks:
                h.remove()
        maps = []
        for att, store in captured:
            q, k = store["q"].float(), store["k"].float()
            hd = att.head_dim
            q = q.view(q.shape[0], q.shape[1], -1, hd).transpose(1, 2)
            k = k.view(k.shape[0], k.shape[1], -1, hd).transpose(1, 2)
            if k.shape[1] != q.shape[1]:
                k = k.repeat_interleave(q.shape[1] // k.shape[1], dim=1)
            scores = (q @ k.transpose(-1, -2)) * att.scaling
            valid = getattr(enc, "attention_mask", None)
            if valid is not None:
                # The decoder masks the encoder's padded frames (create_bidirectional_mask); so must this.
                scores = scores.masked_fill(~valid.bool()[:, None, None, :scores.shape[-1]], float("-inf"))
            maps.append(scores.softmax(dim=-1)[0])
        weights = torch.cat(maps, dim=0).cpu().numpy()
        return weights, out.logits[0]


def load(path: str, device: str = "auto", compute: str = "auto", cpu_threads: int = 0) -> KitsuneModel:
    """Load the Kitsune package in `path` for inference on `device` ("auto", "cuda", "cpu")."""
    pkg = read_package(path)
    import torch
    from transformers import AutoProcessor
    from transformers.utils import logging as hf_logging

    hf_logging.disable_progress_bar()  # "Loading weights" bars, a screenful in the server's log

    if cpu_threads:
        torch.set_num_threads(int(cpu_threads))
    device = pick_device(device)
    dtype = pick_dtype(compute, device, pkg.weights)
    model = build_model(pkg)
    cast_for_inference(model, dtype)
    model = model.to(device)
    processor = AutoProcessor.from_pretrained(pkg.path)
    return KitsuneModel(pkg, model, processor, device, dtype)
