"""Beam search for mlx-whisper: the decoder in mlx_beam.py, its patch, and the server's use of it.

mlx_beam.py imports mlx.core and mlx.utils at module scope, so a test that loads it must supply
them. Everything it asks of MLX is numpy-shaped -- logsumexp, argpartition, take_along_axis,
indexing, tolist -- so the fake below is numpy wearing MLX's names, and the whole file runs on a
Linux CI box with no mlx installed. The risk of any fake is that it drifts from the library it
copies, so the `beam` fixture runs the decoder tests a second time against the real mlx.core when
one is installed, and test_the_library_still_contracts_what_the_fakes_copy pins the parts of
mlx_whisper.decoding the patch reaches into. Nothing here needs a Mac, a GPU, a model or the
network.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
BEAM_PATH = Path(server.__file__).with_name("mlx_beam.py")

# A toy vocabulary: 0 and 6 are start tokens, 5 is end of text, the rest are words.
VOCAB = 8
EOT = 5

MLX_INSTALLED = all(importlib.util.find_spec(name) for name in ("mlx", "mlx_whisper"))
needs_mlx = pytest.mark.skipif(not MLX_INSTALLED, reason="mlx-whisper is installed on Apple Silicon only")


# --------------------------------------------------------------------------- the fake mlx.core

class MxArray(np.ndarray):
    """What the fake mx.array() builds: a numpy array that is also its own type.

    A subclass rather than a wrapper, so every operation mlx_beam.py performs -- negation,
    slicing, argpartition, take_along_axis -- keeps working and keeps returning an mx.array, and
    so `isinstance(x, mx.array)` answers the same here as it does against the real mlx.core.
    """

    def __new__(cls, values, dtype=None):
        return np.asarray(values, dtype=dtype).view(cls)


def fake_logsumexp(a, axis=None, keepdims=False):
    # Shifted by the maximum the way MLX's is: mlx_beam.py subtracts this from raw logits, so an
    # overflow here would be an overflow the real backend does not have.
    largest = np.max(a, axis=axis, keepdims=True)
    total = largest + np.log(np.sum(np.exp(a - largest), axis=axis, keepdims=True))
    return total if keepdims else np.squeeze(total, axis=axis)


def fake_tree_map(fn, tree, is_leaf=None):
    """mlx.utils.tree_map over the shape a kv cache has: tuples of tuples of arrays."""
    if is_leaf is not None and is_leaf(tree):
        return fn(tree)
    if isinstance(tree, (list, tuple)):
        return type(tree)(fake_tree_map(fn, child, is_leaf=is_leaf) for child in tree)
    if isinstance(tree, dict):
        return {k: fake_tree_map(fn, child, is_leaf=is_leaf) for k, child in tree.items()}
    return fn(tree)


def fake_mlx(monkeypatch):
    core = types.ModuleType("mlx.core")
    core.array = MxArray
    core.float16, core.float32, core.int32 = np.float16, np.float32, np.int32
    core.logsumexp = fake_logsumexp
    core.argpartition = np.argpartition
    core.take_along_axis = np.take_along_axis
    core.eval = lambda *arrays: None  # MLX is lazy; numpy is not, so there is nothing to force
    utils = types.ModuleType("mlx.utils")
    utils.tree_map = fake_tree_map
    package = types.ModuleType("mlx")
    package.core, package.utils = core, utils
    for name, module in (("mlx", package), ("mlx.core", core), ("mlx.utils", utils)):
        monkeypatch.setitem(sys.modules, name, module)
    return core


def load_beam():
    """mlx_beam.py by path, the way enable_mlx_beam_search() loads it, against whatever mlx is in."""
    spec = importlib.util.spec_from_file_location("shisuko_mlx_beam_under_test", BEAM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["faked", pytest.param("real", marks=needs_mlx)])
def beam(request, monkeypatch):
    """mlx_beam.py over the fake MLX, and over the installed one where there is one.

    The second run is what keeps the fake honest: a decoder test that passes on numpy and fails on
    Metal would be a test of the fake, not of the search.
    """
    if request.param == "faked":
        fake_mlx(monkeypatch)
    return load_beam()


# --------------------------------------------------------------------------- the fake mlx_whisper

@dataclasses.dataclass(frozen=True)
class DecodingOptions:
    """The fields of mlx_whisper.decoding.DecodingOptions the patch reads or replaces."""
    task: str = "transcribe"
    language: Optional[str] = None
    temperature: float = 0.0
    best_of: Optional[int] = None
    beam_size: Optional[int] = None
    patience: Optional[float] = None
    length_penalty: Optional[float] = None


def fake_decoding(monkeypatch, beam_module=None):
    """mlx_whisper.decoding as the library writes it, down to the refusal the patch works around.

    DecodingTask.__init__ and _verify_options are copies of the library's, so that a test of the
    patch is a test against the real contract: beam_size of any value raises, patience without a
    beam_size raises, and best_of at temperature 0 raises, which is why the group size cannot
    simply be carried through as best_of.
    """

    class GreedyDecoder:
        def __init__(self, temperature, eot):
            self.temperature, self.eot = temperature, eot

    class Inference:
        def __init__(self, model):
            self.model, self.kv_cache = model, None

        def rearrange_kv_cache(self, source_indices):
            self.kv_cache = ("the library's own", source_indices)

    class DecodingTask:
        def __init__(self, model, options):
            self.model = model
            self.tokenizer = SimpleNamespace(eot=50257)
            self.options = self._verify_options(options)
            self.n_group = options.beam_size or options.best_of or 1
            self.inference = Inference(model)
            if options.beam_size is not None:
                raise NotImplementedError("Beam search decoder is not yet implemented")
            self.decoder = GreedyDecoder(options.temperature, self.tokenizer.eot)

        def _verify_options(self, options):
            if options.beam_size is not None and options.best_of is not None:
                raise ValueError("beam_size and best_of can't be given together")
            if options.temperature == 0 and options.best_of is not None:
                raise ValueError("best_of with greedy sampling (T=0) is not compatible")
            if options.patience is not None and options.beam_size is None:
                raise ValueError("patience requires beam_size to be given")
            return options

    decoding = types.ModuleType("mlx_whisper.decoding")
    decoding.DecodingOptions = DecodingOptions
    decoding.DecodingTask = DecodingTask
    decoding.GreedyDecoder = GreedyDecoder
    decoding.Inference = Inference
    package = types.ModuleType("mlx_whisper")
    package.decoding = decoding
    monkeypatch.setitem(sys.modules, "mlx_whisper", package)
    monkeypatch.setitem(sys.modules, "mlx_whisper.decoding", decoding)
    return decoding


@pytest.fixture
def decoding(monkeypatch):
    fake_mlx(monkeypatch)
    return fake_decoding(monkeypatch)


# --------------------------------------------------------------------------- a scripted model

class Script:
    """A language model written down: one probability distribution per prefix.

    A prefix nobody scripted ends at once, so a beam that wanders off the script dies cheaply and
    with a score too low to win; that is what lets a script name only the paths under test.
    """

    def __init__(self, table):
        self.table = {tuple(k): v for k, v in table.items()}

    def probs(self, prefix):
        distribution = self.table.get(tuple(int(t) for t in prefix), {EOT: 1.0})
        probs = np.full(VOCAB, 1e-12)
        for token, p in distribution.items():
            probs[token] = p
        return probs / probs.sum()

    def logits(self, prefix):
        return np.log(self.probs(prefix))

    def score(self, sequence):
        """The log probability the script gives a whole sequence: what the beam is searching for."""
        return sum(float(np.log(self.probs(sequence[:i])[sequence[i]])) for i in range(1, len(sequence)))

    def greedy(self, start, steps):
        """The most probable next token, every step, committed to for good: what MLX ships."""
        sequence = list(start)
        for _ in range(steps):
            sequence.append(int(np.argmax(self.probs(sequence))))
            if sequence[-1] == EOT:
                break
        return tuple(sequence)


class Recorder:
    """An Inference that only remembers which rows the decoder asked the cache to follow."""

    def __init__(self):
        self.moves = []

    def rearrange_kv_cache(self, source_indices):
        self.moves.append(list(source_indices))


def logits_for(beam, script, tokens):
    rows = np.stack([script.logits(prefix) for prefix in tokens.tolist()]).astype(np.float32)
    return beam.mx.array(rows)


def drive(beam, decoder, script, tokens, sum_logprobs, steps):
    """Run the decoder the way DecodingTask._main_loop does, one scripted step at a time."""
    for _ in range(steps):
        tokens, completed, sum_logprobs = decoder.update(
            tokens, logits_for(beam, script, tokens), sum_logprobs)
        if bool(completed):
            break
    return tokens, bool(completed), sum_logprobs


def grouped(beam, tokens, sum_logprobs, n_audio):
    """(n_audio, n_group, length) and (n_audio, n_group), the shapes run() hands finalize()."""
    tokens, totals = np.array(tokens), np.array(sum_logprobs)
    return (beam.mx.array(tokens.reshape(n_audio, -1, tokens.shape[-1])),
            beam.mx.array(totals.reshape(n_audio, -1)))


# A first token that is locally best (0.5 against 0.3) but leads nowhere, against one that is
# locally worse and leads to a near-certain continuation. This is the homophone the README is
# about: 敬老 scores worse than 経老 until the word after it arrives.
SEARCH = Script({
    (0,): {1: 0.50, 2: 0.30, 3: 0.15, EOT: 0.05},
    (0, 1): {3: 0.50, 4: 0.30, EOT: 0.20},
    (0, 2): {3: 0.90, 4: 0.06, EOT: 0.04},
    (0, 1, 3): {EOT: 0.90, 4: 0.07, 1: 0.03},
    (0, 2, 3): {EOT: 0.95, 4: 0.03, 1: 0.02},
})

# Every beam's best continuation is the end of text, so the finished pool fills from the top.
ENDING = Script({
    (0,): {1: 0.60, 2: 0.40},
    (0, 1): {EOT: 0.90, 3: 0.07, 4: 0.03},
    (0, 2): {EOT: 0.80, 3: 0.15, 4: 0.05},
    (0, 1, 3): {EOT: 0.90, 4: 0.06, 1: 0.04},
    (0, 2, 3): {EOT: 0.85, 4: 0.10, 1: 0.05},
})


# --------------------------------------------------------------------------- the search itself

def test_the_beam_finds_a_sequence_greedy_decoding_cannot(beam):
    # The point of the whole file. Greedy takes 1 because 0.5 beats 0.3 and can never take it
    # back; the beam keeps 2 alive for one more token and wins on the product.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens, completed, totals = drive(
        beam, decoder, SEARCH, beam.mx.array([[0], [0]]), beam.mx.array([0.0, 0.0]), steps=4)
    assert completed is True
    sequences, scores = decoder.finalize(*grouped(beam, tokens, totals, n_audio=1))

    best = tuple(sequences.tolist()[0][0])
    greedy = SEARCH.greedy((0,), steps=4)
    assert best == (0, 2, 3, EOT)
    assert greedy == (0, 1, 3, EOT), "greedy commits to the locally best first token"
    assert best != greedy
    assert SEARCH.score(best) > SEARCH.score(greedy)  # -1.36 against -1.49
    assert scores.tolist()[0][0] == pytest.approx(SEARCH.score(best), abs=1e-4)
    # The runner-up is greedy's answer: the beam did not stumble onto one sequence, it ranked both.
    assert tuple(sequences.tolist()[0][1]) == greedy


def test_the_cache_follows_the_beams_it_belongs_to(beam):
    # A kept candidate carries its predecessor's attention with it. Rearranging with the wrong
    # rows continues one beam's text from another beam's cache, which reads as fluent nonsense.
    recorder = Recorder()
    decoder = beam.BeamSearchDecoder(2, EOT, recorder)
    drive(beam, decoder, SEARCH, beam.mx.array([[0], [0]]), beam.mx.array([0.0, 0.0]), steps=4)
    # Step 1: both beams start from the same prefix, so either row's cache serves either candidate.
    # Step 2: the best candidate (0, 2, 3) continues the second beam and the runner-up the first,
    # so the two rows swap; step 3 swaps them back.
    assert recorder.moves == [[1, 1], [1, 0], [1, 0]]


def test_update_returns_the_running_totals_instead_of_mutating_them(beam):
    # MLX arrays are values: _main_loop keeps the previous step's sum_logprobs alive and hands the
    # returned one to mx.async_eval, so a decoder that wrote into its argument would corrupt both.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens, before = beam.mx.array([[0], [0]]), beam.mx.array([0.0, 0.0])
    _tokens, _completed, after = decoder.update(tokens, logits_for(beam, SEARCH, tokens), before)
    assert np.array(before).tolist() == [0.0, 0.0]
    assert after is not before
    assert np.array(after).tolist() != [0.0, 0.0]


def test_completed_is_an_array_and_not_a_bool(beam):
    # _main_loop passes it straight to mx.async_eval beside the tensors, which refuses a Python bool.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens = beam.mx.array([[0], [0]])
    _tokens, completed, _totals = decoder.update(
        tokens, logits_for(beam, ENDING, tokens), beam.mx.array([0.0, 0.0]))
    assert isinstance(completed, beam.mx.array)
    assert not isinstance(completed, bool)


def test_the_batch_keeps_one_row_per_beam_of_every_audio(beam):
    # The rows are the model's batch: losing one would shrink the next forward pass out of step
    # with the kv cache, and gaining one would read a row that has no cache behind it.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens, totals = beam.mx.array([[0], [0], [6], [6]]), beam.mx.array([0.0] * 4)
    for step in range(1, 4):
        tokens, _completed, totals = decoder.update(tokens, logits_for(beam, TWO_AUDIO, tokens), totals)
        assert tuple(tokens.shape) == (4, step + 1)
        assert tuple(totals.shape) == (4,)


def test_a_batch_that_is_not_whole_beams_is_refused(beam):
    # n_audio is read off the batch by division; a remainder would silently mix two audios' beams.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens = beam.mx.array([[0], [0], [0]])
    with pytest.raises(ValueError, match="not a multiple of the beam size 2"):
        decoder.update(tokens, logits_for(beam, SEARCH, tokens), beam.mx.array([0.0, 0.0, 0.0]))


# --------------------------------------------------------------------------- finishing

def test_a_candidate_that_ends_leaves_the_beam_and_is_kept(beam):
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens, completed, _totals = drive(
        beam, decoder, ENDING, beam.mx.array([[0], [0]]), beam.mx.array([0.0, 0.0]), steps=4)
    finished, = decoder.finished_sequences
    assert sorted(finished) == [(0, 1, EOT), (0, 2, EOT)]
    assert finished[(0, 1, EOT)] == pytest.approx(ENDING.score((0, 1, EOT)), abs=1e-4)
    assert completed is True, "both places in the pool are taken, so there is nothing left to find"
    # The batch is refilled with the best unfinished candidates: the search does not shrink.
    assert [row[-1] for row in tokens.tolist()] != [EOT, EOT]


def test_patience_decides_how_many_endings_are_collected(beam):
    recorder = Recorder()
    assert beam.BeamSearchDecoder(5, EOT, recorder).max_candidates == 5  # no patience: one each
    assert beam.BeamSearchDecoder(5, EOT, recorder, 2.0).max_candidates == 10
    assert beam.BeamSearchDecoder(3, EOT, recorder, 1.5).max_candidates == 4  # round(4.5), half to even
    with pytest.raises(ValueError, match="invalid beam size"):
        beam.BeamSearchDecoder(0, EOT, recorder)


def test_patience_keeps_the_search_running_past_the_first_endings(beam):
    # Two endings arrive in one step. With patience the search carries on and finds two more,
    # which is the whole trade: more decoding for a better chance at the best sequence.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder(), patience=2.0)
    tokens = beam.mx.array([[0], [0]])
    totals = beam.mx.array([0.0, 0.0])
    tokens, _completed, totals = decoder.update(tokens, logits_for(beam, ENDING, tokens), totals)
    tokens, completed, totals = decoder.update(tokens, logits_for(beam, ENDING, tokens), totals)
    assert len(decoder.finished_sequences[0]) == 2 and bool(completed) is False
    _tokens, completed, _totals = decoder.update(tokens, logits_for(beam, ENDING, tokens), totals)
    assert sorted(decoder.finished_sequences[0]) == [
        (0, 1, 3, EOT), (0, 1, EOT), (0, 2, 3, EOT), (0, 2, EOT)]
    assert bool(completed) is True


def test_only_the_best_endings_are_kept_once_the_pool_is_full(beam):
    # round(2 * 0.5) is one: the step below offers two endings and only the better is stored, so
    # the cap is a cap on what is remembered, not on what is looked at.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder(), patience=0.5)
    assert decoder.max_candidates == 1
    tokens, totals = beam.mx.array([[0], [0]]), beam.mx.array([0.0, 0.0])
    tokens, _completed, totals = decoder.update(tokens, logits_for(beam, ENDING, tokens), totals)
    _tokens, completed, _totals = decoder.update(tokens, logits_for(beam, ENDING, tokens), totals)
    assert list(decoder.finished_sequences[0]) == [(0, 1, EOT)]
    assert bool(completed) is True


# Two audios in one batch: the first ends early, the second keeps talking. Their prefixes differ
# (0 against 6) so a beam of one can never be confused with a beam of the other.
TWO_AUDIO = Script({
    (0,): {1: 0.60, 2: 0.40},
    (0, 1): {EOT: 0.90, 3: 0.07, 4: 0.03},
    (0, 2): {EOT: 0.80, 3: 0.15, 4: 0.05},
    (0, 1, 3): {4: 0.90, 1: 0.10},
    (0, 2, 3): {4: 0.90, 1: 0.10},
    (6,): {1: 0.60, 2: 0.40},
    (6, 1): {3: 0.90, 4: 0.07, 1: 0.03},
    (6, 2): {3: 0.80, 4: 0.15, 1: 0.05},
    (6, 1, 3): {EOT: 0.90, 4: 0.10},
    (6, 2, 3): {EOT: 0.80, 4: 0.20},
})


def test_the_search_ends_only_when_every_audio_has_finished(beam):
    # One batch decodes several windows at once. Stopping when the first audio is done would cut
    # the others off mid-sentence, so `completed` is an and, not an or.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    tokens, totals = beam.mx.array([[0], [0], [6], [6]]), beam.mx.array([0.0] * 4)
    tokens, _completed, totals = decoder.update(tokens, logits_for(beam, TWO_AUDIO, tokens), totals)
    tokens, completed, totals = decoder.update(tokens, logits_for(beam, TWO_AUDIO, tokens), totals)
    assert sorted(decoder.finished_sequences[0]) == [(0, 1, EOT), (0, 2, EOT)]
    assert decoder.finished_sequences[1] == {}
    assert bool(completed) is False, "the second audio is still speaking"

    _tokens, completed, _totals = decoder.update(tokens, logits_for(beam, TWO_AUDIO, tokens), totals)
    assert sorted(decoder.finished_sequences[1]) == [(6, 1, 3, EOT), (6, 2, 3, EOT)]
    assert bool(completed) is True


# --------------------------------------------------------------------------- finalize

def test_finalize_pads_short_candidates_with_end_of_text(beam):
    # run() slices the whole array at sample_begin and only then trims each row at its first EOT,
    # so EOT is the one padding that survives the round trip: the trim cuts a row back to itself.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    decoder.finished_sequences = [{(0, 1, EOT): -1.0, (0, 2, 3, EOT): -2.0}]
    sequences, scores = decoder.finalize(
        beam.mx.array([[[0, 1, 3], [0, 2, 4]]]), beam.mx.array([[-9.0, -9.5]]))
    assert sequences.tolist() == [[[0, 1, EOT, EOT], [0, 2, 3, EOT]]]
    assert scores.tolist() == [[-1.0, -2.0]]  # best first, so run()'s ranker sees them in order


def test_finalize_offers_exactly_one_candidate_per_beam(beam):
    # MaximumLikelihoodRanker argmaxes over a group; a group with more rows than its neighbours
    # would make the array ragged and the reshape in run() fail.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder(), patience=2.0)
    decoder.finished_sequences = [{(0, 1, EOT): -1.0, (0, 2, EOT): -0.5,
                                   (0, 3, EOT): -4.0, (0, 4, EOT): -3.0}]
    sequences, scores = decoder.finalize(
        beam.mx.array([[[0, 1, 3], [0, 2, 4]]]), beam.mx.array([[-9.0, -9.5]]))
    assert sequences.tolist() == [[[0, 2, EOT], [0, 1, EOT]]]
    assert scores.tolist() == [[-0.5, -1.0]]


def test_finalize_falls_back_to_the_beams_still_running(beam):
    # sample_len ran out before the beam did. An unfinished beam is still an answer, so it is
    # ended by hand rather than dropped, and it wins here because its score is the better one.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    decoder.finished_sequences = [{(0, 1, EOT): -1.0}]
    sequences, scores = decoder.finalize(
        beam.mx.array([[[0, 2, 3], [0, 2, 4]]]), beam.mx.array([[-0.5, -9.0]]))
    assert sequences.tolist() == [[[0, 2, 3, EOT], [0, 1, EOT, EOT]]]
    assert scores.tolist() == [[-0.5, -1.0]]


def test_finalize_after_a_reset_answers_from_the_beams_alone(beam):
    # decoder.reset() runs before every window; a window that ends before a single EOT appeared
    # must still produce candidates rather than raise on an empty pool.
    decoder = beam.BeamSearchDecoder(2, EOT, Recorder())
    decoder.reset()
    sequences, scores = decoder.finalize(
        beam.mx.array([[[0, 1], [0, 2]]]), beam.mx.array([[-2.0, -1.0]]))
    assert sequences.tolist() == [[[0, 2, EOT], [0, 1, EOT]]]
    assert scores.tolist() == [[-1.0, -2.0]]


# --------------------------------------------------------------------------- the kv cache

def cache_of(beam):
    """One layer of ((self_k, self_v), (cross_k, cross_v)), two beam rows each."""
    make = beam.mx.array
    return [((make([[1.0], [2.0]]), make([[3.0], [4.0]])),
             (make([[5.0], [6.0]]), make([[7.0], [8.0]])))]


def test_the_cross_attention_cache_is_never_copied(beam):
    # It is computed from the audio, not the tokens, so every beam of one audio holds the same
    # 1500 frames of it. Permuting those rows changes nothing and copies about 1.2 GB per token
    # on large-v3: dropping the copy is what took the measured speed from 1.1x to 4.1x realtime.
    inference = SimpleNamespace(kv_cache=cache_of(beam))
    ((_, _), (cross_k, cross_v)), = inference.kv_cache
    beam.rearrange_self_attention_only(inference, [1, 0])

    (self_k, self_v), (kept_k, kept_v) = inference.kv_cache[0]
    assert self_k.tolist() == [[2.0], [1.0]] and self_v.tolist() == [[4.0], [3.0]]
    assert kept_k is cross_k and kept_v is cross_v, "the same arrays, not equal ones"


def test_a_beam_order_that_did_not_change_costs_nothing(beam):
    # Most steps keep the beams in place. Rebuilding the list would copy every layer's keys and
    # values for nothing, once per decoded token.
    inference = SimpleNamespace(kv_cache=cache_of(beam))
    before = inference.kv_cache
    beam.rearrange_self_attention_only(inference, [0, 1])
    assert inference.kv_cache is before


# --------------------------------------------------------------------------- the patch

def test_a_decoding_task_asked_for_a_beam_gets_one(beam_module_faked, decoding):
    beam_module_faked.enable_beam_search()
    task = decoding.DecodingTask(object(), DecodingOptions(beam_size=5))
    assert task.n_group == 5, "the batch is sized by the group, so this is what makes the beam wide"
    assert isinstance(task.decoder, beam_module_faked.BeamSearchDecoder)
    assert task.decoder.beam_size == 5 and task.decoder.eot == task.tokenizer.eot
    assert task.decoder.inference is task.inference
    # DecodingResult reports the options back, so the beam the viewer asked for must survive the
    # stripping the original constructor needs.
    assert task.options.beam_size == 5


def test_patience_survives_the_stripped_constructor(beam_module_faked, decoding):
    # _verify_options refuses a patience without a beam_size, so both have to come out together
    # and both have to be put back.
    beam_module_faked.enable_beam_search()
    task = decoding.DecodingTask(object(), DecodingOptions(beam_size=3, patience=2.0))
    assert task.decoder.max_candidates == 6 and task.options.patience == 2.0


def test_the_group_size_cannot_travel_as_best_of(beam_module_faked, decoding):
    # The reason the constructor is wrapped at all: beam search runs at temperature 0, and that is
    # exactly where the library refuses best_of.
    with pytest.raises(ValueError, match="best_of with greedy sampling"):
        decoding.DecodingTask(object(), DecodingOptions(best_of=5, temperature=0.0))


def test_the_cache_rearrangement_is_replaced_wholesale(beam_module_faked, decoding):
    beam_module_faked.enable_beam_search()
    assert decoding.Inference.rearrange_kv_cache is beam_module_faked.rearrange_self_attention_only
    inference = decoding.Inference(object())
    inference.kv_cache = cache_of(beam_module_faked)
    inference.rearrange_kv_cache([1, 0])  # bound as a method: the first argument is the inference
    assert inference.kv_cache[0][0][0].tolist() == [[2.0], [1.0]]


def test_no_beam_leaves_the_greedy_decoder_alone(beam_module_faked, decoding):
    # Every temperature above the first is sampled, not searched; that path must not change.
    beam_module_faked.enable_beam_search()
    task = decoding.DecodingTask(object(), DecodingOptions())
    assert isinstance(task.decoder, decoding.GreedyDecoder) and task.n_group == 1


def test_a_beam_of_one_is_greedy_decoding(beam_module_faked, decoding):
    # The library refuses any beam_size at all, so the patch clears the option away instead of
    # handing it on: one beam is greedy decoding, not a crash. It still reports what it was asked.
    beam_module_faked.enable_beam_search()
    task = decoding.DecodingTask(object(), DecodingOptions(beam_size=1))
    assert isinstance(task.decoder, decoding.GreedyDecoder) and task.n_group == 1
    assert task.options.beam_size == 1


def test_patching_twice_patches_once(beam_module_faked, decoding, monkeypatch):
    # run() builds a DecodingTask per window; a wrapper that wrapped itself would nest one call
    # deeper every time the server asked.
    beam_module_faked.enable_beam_search()
    first = decoding.DecodingTask.__init__
    beam_module_faked.enable_beam_search()
    assert decoding.DecodingTask.__init__ is first
    # The flag is the guard, not the state of the module it patched: a second mlx_whisper in the
    # same process is a test fake or a reload, and either way this copy has done its work.
    assert beam_module_faked._PATCHED is True
    fresh = fake_decoding(monkeypatch)
    beam_module_faked.enable_beam_search()
    assert not hasattr(fresh, "BeamSearchDecoder")


def test_a_library_that_grew_its_own_beam_search_is_left_alone(beam_module_faked, decoding):
    # Its decoder knows its own internals better than a copy of openai-whisper's does.
    theirs = object()
    decoding.BeamSearchDecoder = theirs
    before = (decoding.DecodingTask.__init__, decoding.Inference.rearrange_kv_cache)
    beam_module_faked.enable_beam_search()
    assert (decoding.DecodingTask.__init__, decoding.Inference.rearrange_kv_cache) == before
    assert decoding.BeamSearchDecoder is theirs


@pytest.fixture
def beam_module_faked(monkeypatch):
    """mlx_beam.py over the fake MLX only: the patch is wiring, and wiring needs no arithmetic."""
    fake_mlx(monkeypatch)
    return load_beam()


# --------------------------------------------------------------------------- the server's side

def test_the_server_turns_the_beam_on_and_says_it_took(monkeypatch, decoding):
    monkeypatch.setattr(server, "_MLX_BEAM", None)
    assert server.enable_mlx_beam_search() is True
    assert decoding.BeamSearchDecoder.__name__ == "BeamSearchDecoder"
    assert decoding.DecodingTask(object(), DecodingOptions(beam_size=4)).n_group == 4


def test_a_beam_that_cannot_be_installed_is_a_warning_and_not_a_crash(monkeypatch, caplog):
    # Greedy decoding is what the backend did before this file existed: worse Japanese, still
    # subtitles. A failure here must never take the server down with it.
    fake_mlx(monkeypatch)
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # the import inside raises
    monkeypatch.setattr(server, "_MLX_BEAM", None)
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        assert server.enable_mlx_beam_search() is False
    assert "beam search is unavailable" in caplog.text


def test_the_answer_is_worked_out_once_and_remembered(monkeypatch, decoding):
    # MlxWhisperModel asks in every constructor and the server builds one per model switch;
    # re-reading and re-executing the file each time would re-run the patch as well.
    monkeypatch.setattr(server, "_MLX_BEAM", False)
    assert server.enable_mlx_beam_search() is False
    assert not hasattr(decoding, "BeamSearchDecoder"), "a remembered no does no work at all"

    monkeypatch.setattr(server, "_MLX_BEAM", True)
    monkeypatch.setitem(sys.modules, "mlx.core", None)  # a load now would fail, and none happens
    assert server.enable_mlx_beam_search() is True


# Whether that answer puts a beam_size into the mlx_whisper.transcribe call is test_mlx.py's
# test_the_beam_size_crosses_only_when_there_is_a_decoder_for_it, beside the other options
# MlxWhisperModel translates on its way across.


# --------------------------------------------------------------------------- against the real thing

@needs_mlx
def test_the_library_still_contracts_what_the_fakes_copy():
    """The fakes above are only worth as much as their likeness; this is the likeness, checked.

    Skipped where mlx-whisper cannot be installed, which is everywhere but Apple Silicon.
    """
    import inspect

    from mlx.utils import tree_map
    from mlx_whisper import decoding as real

    assert not hasattr(real, "BeamSearchDecoder"), "the library grew one: mlx_beam.py can retire"
    source = inspect.getsource(real.DecodingTask.__init__)
    assert "raise NotImplementedError" in source and "options.beam_size is not None" in source
    assert {"beam_size", "patience", "best_of"} <= {f.name for f in dataclasses.fields(real.DecodingOptions)}
    assert callable(real.Inference.rearrange_kv_cache) and hasattr(real, "GreedyDecoder")
    assert "best_of with greedy sampling" in inspect.getsource(real.DecodingTask._verify_options)
    assert "patience requires beam_size" in inspect.getsource(real.DecodingTask._verify_options)

    # The one mlx.utils call mlx_beam.py makes, over the shape a kv cache has.
    tree = ((1, 2), {"k": 3})
    assert tree_map(lambda x: x * 10, tree) == fake_tree_map(lambda x: x * 10, tree)
