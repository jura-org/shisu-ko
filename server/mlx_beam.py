#!/usr/bin/env python3
"""Beam search for mlx-whisper, whose decoding.py ships only a greedy decoder.

Beam search is a search over the same logits, not a model feature, and mlx-whisper already has
everything it needs: `DecodingTask.n_group` sizes the batch, `MaximumLikelihoodRanker` does the
length-penalised final choice, and `Inference.rearrange_kv_cache()` reorders the cache when the
beams are shuffled, which is the one genuinely awkward part. Only the decoder itself is missing,
behind a `NotImplementedError`. This is a port of openai-whisper's `BeamSearchDecoder` (MIT) onto
MLX, plus the patch that puts it in place.

It matters for Japanese in particular: greedy decoding commits to a homophone before the words
that would disambiguate it arrive, which is how `敬老` becomes `経老` and `犯行` becomes `判行`.

server.py loads this file by path, the way it loads native_host.py, and only on the MLX backend.
"""
from __future__ import annotations

import dataclasses
from typing import List

import mlx.core as mx
import numpy as np
from mlx.utils import tree_map


class BeamSearchDecoder:
    """mlx-whisper's TokenDecoder contract (reset / update / finalize) over a beam.

    `update()` returns `(tokens, completed, sum_logprobs)` rather than mutating its arguments,
    which is where this differs from the PyTorch original: MLX arrays are values.
    """

    def __init__(self, beam_size: int, eot: int, inference, patience=None):
        self.beam_size = beam_size
        self.eot = eot
        self.inference = inference
        self.patience = patience or 1.0
        self.max_candidates = round(beam_size * self.patience)
        self.finished_sequences: list = []
        if self.max_candidates <= 0:
            raise ValueError(f"invalid beam size {beam_size} or patience {patience}")

    def reset(self) -> None:
        self.finished_sequences = []

    def update(self, tokens: mx.array, logits: mx.array, sum_logprobs: mx.array):
        if tokens.shape[0] % self.beam_size != 0:
            raise ValueError(f"{tokens.shape}[0] is not a multiple of the beam size {self.beam_size}")
        n_audio = tokens.shape[0] // self.beam_size
        if not self.finished_sequences:
            self.finished_sequences = [{} for _ in range(n_audio)]

        logprobs = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
        # Only the best beam_size + 1 continuations of a beam can survive the cut below, so only
        # those come back from the GPU. The whole row is the vocabulary, some 51 866 floats per
        # beam per token, and copying it would cost more than the search it feeds.
        width = self.beam_size + 1
        candidates = mx.argpartition(-logprobs, width, axis=-1)[:, :width]
        candidate_logprobs = mx.take_along_axis(logprobs, candidates, axis=-1)
        mx.eval(candidates, candidate_logprobs)
        candidates = np.array(candidates)
        candidate_logprobs = np.array(candidate_logprobs)
        prefixes = tokens.tolist()
        totals = np.array(sum_logprobs)

        next_tokens: List[List[int]] = []
        next_totals: List[float] = []
        source_indices: List[int] = []
        finished_now: List[dict] = []
        for i in range(n_audio):
            scores, sources, finished = {}, {}, {}
            for j in range(self.beam_size):
                row = i * self.beam_size + j
                prefix = tuple(prefixes[row])
                for token, logprob in zip(candidates[row], candidate_logprobs[row]):
                    sequence = prefix + (int(token),)
                    scores[sequence] = float(totals[row]) + float(logprob)
                    sources[sequence] = row
            saved = 0
            for sequence in sorted(scores, key=scores.get, reverse=True):
                if sequence[-1] == self.eot:
                    finished[sequence] = scores[sequence]
                    continue
                next_tokens.append(list(sequence))
                next_totals.append(scores[sequence])
                source_indices.append(sources[sequence])
                saved += 1
                if saved == self.beam_size:
                    break
            finished_now.append(finished)

        tokens = mx.array(next_tokens, dtype=tokens.dtype)
        sum_logprobs = mx.array(next_totals, dtype=sum_logprobs.dtype)
        # The cache rows must follow the beams they belong to, or the next step continues one
        # beam's text from another's attention.
        self.inference.rearrange_kv_cache(source_indices)

        for previously, newly in zip(self.finished_sequences, finished_now):
            for sequence in sorted(newly, key=newly.get, reverse=True):
                if len(previously) >= self.max_candidates:
                    break
                previously[sequence] = newly[sequence]

        completed = all(len(group) >= self.max_candidates for group in self.finished_sequences)
        # An MLX array, not a bool: _main_loop hands this to mx.async_eval beside the tensors.
        return tokens, mx.array(completed), sum_logprobs

    def finalize(self, preceding_tokens: mx.array, sum_logprobs: mx.array):
        """The best beam_size candidates per audio, as one array run() can slice and trim."""
        totals = np.array(sum_logprobs)  # (n_audio, n_group)
        preceding = preceding_tokens.tolist()
        sequences: List[List[List[int]]] = []
        scores: List[List[float]] = []
        for i, finished in enumerate(self.finished_sequences or [{}] * len(preceding)):
            pool = dict(finished)
            # Not enough beams reached the end of text: the best unfinished ones stand in, ended
            # by hand, so that every group offers the ranker the same number of candidates.
            for j in np.argsort(totals[i])[::-1]:
                if len(pool) >= self.beam_size:
                    break
                pool[tuple(preceding[i][j]) + (self.eot,)] = float(totals[i][j])
            best = sorted(pool, key=pool.get, reverse=True)[: self.beam_size]
            sequences.append([list(s) for s in best])
            scores.append([pool[s] for s in best])
        # run() slices these as one array and only then trims each row at its first EOT, so a
        # short candidate is padded with EOT: the trim cuts it back to its own length.
        longest = max(len(s) for group in sequences for s in group)
        padded = [[s + [self.eot] * (longest - len(s)) for s in group] for group in sequences]
        return mx.array(padded), mx.array(scores)


def rearrange_self_attention_only(inference, source_indices) -> None:
    """Reorder the beams' self-attention cache and leave the cross-attention cache alone.

    The library's own rearrange_kv_cache() maps over both halves of every layer's
    `(self_kv, cross_kv)`. The cross-attention keys and values are computed from the audio, not
    from the tokens, so every beam of one audio holds the same 1500 frames of them: permuting
    those rows changes nothing and, on large-v3, copies about 1.2 GB per token decoded. A beam
    never takes a candidate from another audio's group, so dropping that copy is exact.
    """
    if list(source_indices) == list(range(len(source_indices))):
        return
    inference.kv_cache = [
        (tree_map(lambda x: x[source_indices], self_kv), cross_kv)
        for self_kv, cross_kv in inference.kv_cache
    ]


_PATCHED = False


def enable_beam_search() -> None:
    """Let mlx_whisper.decode() accept beam_size, by giving DecodingTask the decoder it lacks.

    The constructor is wrapped rather than rewritten: it is run with the beam stripped out, and
    the two things beam_size decides, `n_group` and `decoder`, are set afterwards. Carrying the
    group size through as `best_of` instead would not do, since _verify_options() refuses
    best_of at temperature 0, which is the only temperature beam search runs at.
    """
    global _PATCHED
    if _PATCHED:
        return
    from mlx_whisper import decoding

    if getattr(decoding, "BeamSearchDecoder", None) is not None:
        _PATCHED = True  # the library grew its own; its decoder knows its own internals better
        return

    original = decoding.DecodingTask.__init__

    def __init__(self, model, options):  # noqa: N807
        beam = options.beam_size
        # The original refuses any beam_size at all, a beam of one included, so the option is
        # always cleared away rather than passed on; one beam is greedy decoding.
        stripped = dataclasses.replace(options, beam_size=None, patience=None)
        if not beam or beam <= 1:
            original(self, model, stripped)
            self.options = options
            return
        original(self, model, stripped)
        self.options = options  # what DecodingResult reports back
        self.n_group = beam
        self.decoder = BeamSearchDecoder(beam, self.tokenizer.eot, self.inference, options.patience)

    decoding.DecodingTask.__init__ = __init__
    decoding.Inference.rearrange_kv_cache = rearrange_self_attention_only
    decoding.BeamSearchDecoder = BeamSearchDecoder
    _PATCHED = True
