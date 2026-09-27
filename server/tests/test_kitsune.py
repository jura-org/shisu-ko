"""Kitsune-Transcribe models: the engine's pure parts (kitsune_engine.py) and the server's names, downloads and rules.

Nothing here needs PyTorch, transformers or a model: the packed formats are built by hand from
the recipes kitsune.quant writes (the WP5 contract), the timings from synthetic CTC paths and
attention maps, and the server's side runs with fakes in place of the hub and the model.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
engine = server.kitsune_engine()
RATE = server.SAMPLE_RATE


# --------------------------------------------------------------------------- packed formats

def test_the_e4m3_table_decodes_the_bytes_float8_e4m3fn_defines():
    t = engine.E4M3_VALUES
    assert t[0x00] == 0.0 and t[0x80] == 0.0
    assert t[0x38] == 1.0          # exponent 7 (the bias), no mantissa
    assert t[0xB8] == -1.0         # the sign bit
    assert t[0x7E] == 448.0        # the largest finite value
    assert np.isnan(t[0x7F]) and np.isnan(t[0xFF])  # S.1111.111 is NaN; there is no infinity
    assert t[0x01] == 2.0 ** -9    # the smallest subnormal: 1/8 x 2^-6
    assert t[0x3C] == 1.5


def test_nibbles_unpack_low_first_and_e2m1_codes_carry_their_sign_in_bit_3():
    packed = np.array([[0x21, 0xF8]], dtype=np.uint8)
    assert engine.unpack_nibbles(packed).tolist() == [[1, 2, 8, 15]]
    assert engine.e2m1_decode(np.array([0, 1, 2, 3, 4, 5, 6, 7], np.uint8)).tolist() == [0, .5, 1, 1.5, 2, 3, 4, 6]
    assert engine.e2m1_decode(np.array([9, 15, 8], np.uint8)).tolist() == [-.5, -6, 0]


def test_int8_rows_scale_by_their_own_factor():
    q = np.array([[127, -128, 0, 1], [10, 20, 30, 40]], dtype=np.int8)
    w = engine.dequantize("int8", (2, 4), q, qscale=np.array([0.5, 0.25], np.float32))
    assert w.dtype == np.float32
    assert w.tolist() == [[63.5, -64.0, 0.0, 0.5], [2.5, 5.0, 7.5, 10.0]]


def test_fp8_weights_come_as_bytes_and_scale_per_row():
    q = np.array([[0x38, 0xB8], [0x7E, 0x00]], dtype=np.uint8)
    w = engine.dequantize("fp8", (2, 2), q, qscale=np.array([2.0, 0.5], np.float32))
    assert w.tolist() == [[2.0, -2.0], [224.0, 0.0]]


def test_nvfp4_is_the_code_times_its_block_scale_times_the_tensor_scale():
    codes = np.zeros((1, 32), dtype=np.uint8)
    codes[0, 0], codes[0, 1], codes[0, 16], codes[0, 17] = 7, 9, 2, 12  # 6, -0.5 | 1, -2
    packed = (codes[:, 0::2] | (codes[:, 1::2] << 4)).astype(np.uint8)
    blocks = np.array([[0x38, 0x40]], dtype=np.uint8)  # E4M3 1.0 and 2.0
    w = engine.dequantize("nvfp4", (1, 32), packed, qblock_scale=blocks, qtensor_scale=np.float32(0.5))
    assert w[0, :2].tolist() == [3.0, -0.25]
    assert w[0, 16:18].tolist() == [1.0, -2.0]
    assert not w[0, 2:16].any() and not w[0, 18:].any()


def test_mxfp4_blocks_scale_by_a_power_of_two_with_bias_127():
    codes = np.full((1, 64), 2, dtype=np.uint8)  # every value 1.0
    packed = (codes[:, 0::2] | (codes[:, 1::2] << 4)).astype(np.uint8)
    w = engine.dequantize("mxfp4", (1, 64), packed, qblock_scale=np.array([[127 + 3, 127 - 2]], np.uint8))
    assert set(w[0, :32].tolist()) == {8.0} and set(w[0, 32:].tolist()) == {0.25}


def reference_int8(w):
    """kitsune.quant's INT8 recipe (per output row, symmetric, 127.5), as the contract states it."""
    s = np.maximum(np.abs(w).max(axis=1), np.finfo(np.float32).eps) / 127.5
    return np.clip(np.round(w / s[:, None]), -128, 127).astype(np.int8), s.astype(np.float32)


def test_a_recipe_packed_int8_weight_comes_back_within_half_a_step():
    rng = np.random.default_rng(0)
    w = rng.normal(size=(8, 64)).astype(np.float32)
    q, s = reference_int8(w)
    back = engine.dequantize("int8", w.shape, q, qscale=s)
    assert np.all(np.abs(back - w) <= s[:, None] / 2 + 1e-6)


def test_a_wrong_shape_or_format_is_refused():
    with pytest.raises(engine.PackageError):
        engine.dequantize("int8", (2, 2), np.zeros((2, 3), np.int8), qscale=np.ones(2, np.float32))
    with pytest.raises(engine.PackageError):
        engine.dequantize("int4", (1, 1), np.zeros((1, 1), np.int8))


# --------------------------------------------------------------------------- packages

def write_package(tmp_path, arch="ParakeetForCTC", recipe=None, weights=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({"architectures": [arch]}), encoding="utf-8")
    if weights:
        (tmp_path / "model.safetensors").write_bytes(b"")
    if recipe is not None:
        (tmp_path / "quantization.json").write_text(json.dumps(recipe), encoding="utf-8")
    return str(tmp_path)


def test_a_plain_export_is_bf16_of_its_family(tmp_path):
    pkg = engine.read_package(write_package(tmp_path / "ctc"))
    assert (pkg.family, pkg.weights, pkg.fmt, pkg.recipe) == ("ctc", "bf16", "bf16", None)
    pkg = engine.read_package(write_package(tmp_path / "aed", arch="CohereAsrForConditionalGeneration"))
    assert pkg.family == "aed"


def test_a_variant_takes_its_format_from_its_recipe(tmp_path):
    pkg = engine.read_package(write_package(tmp_path, recipe={"schema": 1, "format": "nvfp4-w4a4",
                                                              "weights": "nvfp4", "layers": {}}))
    assert (pkg.weights, pkg.fmt) == ("nvfp4", "nvfp4-w4a4")


@pytest.mark.parametrize("arch, recipe, weights, words", [
    ("WhisperForConditionalGeneration", None, True, "not a Kitsune-Transcribe model"),
    ("ParakeetForCTC", None, False, "no model.safetensors"),
    ("ParakeetForCTC", {"schema": 2, "weights": "int8"}, True, "schema 2"),
    ("ParakeetForCTC", {"schema": 1, "weights": "int4"}, True, "unknown weight format"),
])
def test_what_is_no_package_says_why(tmp_path, arch, recipe, weights, words):
    with pytest.raises(engine.PackageError, match=words):
        engine.read_package(write_package(tmp_path, arch=arch, recipe=recipe, weights=weights))


# --------------------------------------------------------------------------- CTC timings

def ctc_log_probs(path, vocab=5, blank=4):
    lp = np.full((len(path), vocab), np.log(0.01), dtype=np.float32)
    for t, tok in enumerate(path):
        lp[t, tok] = np.log(0.9)
    return lp


def test_the_greedy_path_collapses_repeats_and_drops_blanks():
    b = 4
    spans = engine.ctc_spans(ctc_log_probs([b, 1, 1, b, 1, 2, b, b, 3]), blank=b)
    assert [(s.token, s.first, s.last) for s in spans] == [(1, 1, 2), (1, 4, 4), (2, 5, 5), (3, 8, 8)]
    assert all(abs(s.probability - 0.9) < 1e-5 for s in spans)
    assert engine.ctc_spans(np.zeros((0, 5), np.float32), blank=b) == []


def test_a_ctc_word_ends_a_little_after_its_last_frame_but_never_past_the_next_word():
    spans = [engine.CtcSpan(1, 0, 1, 0.9), engine.CtcSpan(2, 3, 3, 0.8), engine.CtcSpan(3, 20, 20, 0.7)]
    words = engine.ctc_words(spans, lambda ids: "".join("abc"[i - 1] for i in ids), end_frame=40)
    assert [w.word for w in words] == ["a", "b", "c"]
    frame = engine.FRAME_S
    assert words[0].start == 0.0 and words[0].end == pytest.approx(3 * frame)          # cut at the next word
    assert words[1].end == pytest.approx((3 + 1 + 4) * frame)                          # its last frame + CTC_TAIL_S
    assert words[2].end == pytest.approx((20 + 1 + 4) * frame)
    assert words[1].probability == pytest.approx(0.8)


def test_a_character_split_over_byte_tokens_stays_one_word():
    # ids 1-3 are the three UTF-8 bytes of 語, 4 is "は"
    table = {1: b"\xe8", 2: b"\xaa", 3: b"\x9e", 4: "は".encode()}
    decode = lambda ids: b"".join(table[i] for i in ids).decode("utf-8", errors="replace")  # noqa: E731
    assert engine.group_tokens([1, 2, 3, 4], decode) == [("語", [0, 1, 2]), ("は", [3])]


def test_words_keep_the_spaces_the_tokenizer_writes():
    pieces = {1: "▁hello", 2: "▁wor", 3: "ld"}
    decode = lambda ids: "".join(pieces[i] for i in ids).replace("▁", " ").lstrip()  # noqa: E731
    assert [w for w, _ in engine.group_tokens([1, 2, 3], decode)] == ["hello", " wor", "ld"]


# --------------------------------------------------------------------------- attention alignment

def test_dtw_walks_a_diagonal():
    cost = np.ones((3, 6))
    for i in range(3):
        cost[i, 2 * i:2 * i + 2] = 0.0
    rows, cols = engine.dtw_path(cost)
    assert rows[0] == 0 and cols[0] == 0 and rows[-1] == 2 and cols[-1] == 5
    assert all(np.diff(rows) >= 0) and all(np.diff(cols) >= 0)
    assert all(cost[r, c] == 0.0 for r, c in zip(rows, cols))


def block_attention(bounds, frames, heads=2):
    """Attention where row j looks at frames [bounds[j], bounds[j+1])."""
    w = np.full((heads, len(bounds) - 1, frames), 0.01, dtype=np.float32)
    for j in range(len(bounds) - 1):
        w[:, j, bounds[j]:bounds[j + 1]] = 1.0
    return w / w.sum(axis=-1, keepdims=True)


def test_the_boundaries_follow_where_each_token_looked():
    # Three tokens and the end token's row: 0-10, 10-20, 20-35, then the end token over 35-40.
    w = block_attention([0, 10, 20, 35, 40], 40)
    bounds = engine.attention_boundaries(w, 3)
    assert len(bounds) == 4
    assert abs(bounds[1] - 10) <= 1 and abs(bounds[2] - 20) <= 1 and abs(bounds[3] - 35) <= 1
    assert bounds[0] == 0


def test_without_the_end_row_the_last_token_runs_to_the_end_of_the_audio():
    bounds = engine.attention_boundaries(block_attention([0, 10, 20], 30), 2)
    assert bounds[-1] == 30 and list(bounds) == sorted(bounds)


def test_an_aligned_word_is_capped_and_a_first_word_keeps_its_end():
    words = [engine.Word("シ", 0.4, 13.04, 0.9), engine.Word("ュ", 13.04, 13.12, 0.9),
             engine.Word("、", 13.36, 16.0, 0.9), engine.Word("合", 16.0, 16.3, 0.9), engine.Word("って", 16.3, 19.0, 0.9)]
    out = engine.cap_durations(words)
    assert out[0].end == 13.04 and out[0].start == pytest.approx(13.04 - 0.85)  # the silence lay before it
    assert out[1].start == 13.04 and out[1].end == 13.12                        # short words stay as they are
    assert out[2].start == 13.36 and out[2].end == pytest.approx(13.86)         # punctuation: WORD_BASE_S
    assert out[4].start == 16.3 and out[4].end == pytest.approx(16.3 + 1.2)     # the pause follows it


def test_aed_words_group_tokens_and_average_their_probabilities():
    decode = lambda ids: "".join({1: "今日", 2: "は"}[i] for i in ids)  # noqa: E731
    words = engine.aed_words([1, 2], [0.9, 0.5], np.array([2, 5, 8]), decode)
    assert [(w.word, w.start, w.end) for w in words] == [("今日", 2 * 0.08, 5 * 0.08), ("は", 5 * 0.08, 8 * 0.08)]
    assert words[1].probability == 0.5


# --------------------------------------------------------------------------- chunks and segments

def test_speech_joins_across_short_pauses_and_splits_at_long_ones():
    chunks = engine.plan_chunks([(1.0, 3.0), (3.5, 6.0), (10.0, 12.0)], duration=30.0)
    assert chunks == [(0.8, 6.2), (9.8, 12.2)]


def test_a_chunk_never_outgrows_the_limit_and_padding_stops_at_the_neighbours_and_the_audio():
    chunks = engine.plan_chunks([(0.05, 20.0), (20.5, 40.0)], duration=40.0, max_s=28.0)
    assert chunks[0][0] == 0.0 and chunks[-1][1] == 40.0
    assert all(b - a <= 28.0 + 2 * engine.CHUNK_PAD_S for a, b in chunks)
    long = engine.plan_chunks([(0.0, 60.0)], duration=60.0, max_s=28.0)
    assert len(long) == 3 and all(b <= c for (_, b), (c, _) in zip(long, long[1:]))
    assert engine.plan_chunks([], duration=10.0) == []


def test_segments_end_at_a_sentence_mark_or_a_pause():
    W = engine.Word
    words = [W("はい", 0.0, 0.3, 0.9), W("。", 0.3, 0.4, 0.9), W("それで", 0.5, 0.9, 0.8),
             W("ね", 0.9, 1.0, 0.8), W("次", 2.5, 2.8, 0.7)]
    segs = engine.make_segments(words, first_id=4)
    assert [s.text for s in segs] == ["はい。", "それでね", "次"]
    assert [s.id for s in segs] == [4, 5, 6]
    assert segs[1].start == 0.5 and segs[1].end == 1.0
    assert segs[2].avg_logprob == pytest.approx(np.log(0.7))
    assert segs[0].no_speech_prob == 0.0 and segs[0].words[0].word == "はい"


def test_the_model_refuses_another_language_and_has_no_language_head():
    model = engine.KitsuneModel.__new__(engine.KitsuneModel)
    with pytest.raises(ValueError, match="Japanese only"):
        engine.KitsuneModel.transcribe(model, np.zeros(RATE, np.float32), language="en")
    with pytest.raises(NotImplementedError):
        model.detect_language(audio=np.zeros(RATE, np.float32))
    assert (engine.KitsuneModel.detects_language, engine.KitsuneModel.sings, engine.KitsuneModel.takes_prompt) \
        == (False, False, False)


def test_transcribe_decodes_each_chunk_and_puts_the_words_on_the_windows_timeline(monkeypatch):
    model = engine.KitsuneModel.__new__(engine.KitsuneModel)
    seen = []

    def decode_chunk(samples):
        seen.append(len(samples) / RATE)
        return [engine.Word("はい", 0.1, 0.4, 0.9), engine.Word("。", 0.4, 0.5, 0.9)]

    model.decode_chunk = decode_chunk
    segs, info = engine.KitsuneModel.transcribe(model, np.zeros(10 * RATE, np.float32), vad_filter=False)
    assert seen == [pytest.approx(10.0)]
    assert [s.text for s in segs] == ["はい。"] and segs[0].start == pytest.approx(0.1)
    assert info.language == "ja" and info.duration == 10.0


# --------------------------------------------------------------------------- the server's names

def test_kitsune_names_parse_to_a_base_and_a_precision():
    assert server.kitsune_name("kitsune-0.6b") == ("kitsune-0.6b", "")
    assert server.kitsune_name(server.KITSUNE_REPOS["kitsune-0.3b"]) == ("kitsune-0.3b", "")
    assert server.kitsune_name("kitsune-0.6b-int8") == ("kitsune-0.6b", "int8")
    assert server.kitsune_name("kitsune-0.1b-nvfp4-w4a4") == ("kitsune-0.1b", "nvfp4")
    assert server.kitsune_name("kitsune-0.6b-int4") == ("kitsune-0.6b", None)
    assert server.kitsune_name("large-v3") is None and server.kitsune_name(None) is None


def test_every_spelling_of_one_set_of_weights_is_one_model():
    for name in ("kitsune-0.6b-int8", "kitsune-0.6b-int8-w8a16", "kitsune-0.6b-int8-w8a8"):
        assert server.canonical_model_name(name) == "kitsune-0.6b-int8"
    assert server.canonical_model_name("kitsune-0.6b-bf16") == "kitsune-0.6b"
    assert server.canonical_model_name(server.KITSUNE_REPOS["kitsune-0.6b"]) == "kitsune-0.6b"
    assert server.canonical_model_name("kitsune-0.6b-int4") == "kitsune-0.6b-int4"  # passes through, refused later
    assert set(server.model_spellings("kitsune-0.6b-int8")) == {
        "kitsune-0.6b-int8", "kitsune-0.6b-int8-w8a16", "kitsune-0.6b-int8-w8a8"}
    assert server.KITSUNE_REPOS["kitsune-0.6b"] in server.model_spellings("kitsune-0.6b")


def test_every_kitsune_name_is_a_valid_model_name_for_the_client():
    for base in server.KITSUNE_REPOS:
        assert server.valid_model_name(base)
        for short in list(server.KITSUNE_FORMATS) + list(server.KITSUNE_FORMAT_ALIASES):
            assert server.valid_model_name(f"{base}-{short}")


def test_a_precision_downloads_its_own_folder_only():
    repo, folder, patterns = server.kitsune_download_plan("kitsune-0.3b-nvfp4-w4a4")
    assert repo == server.KITSUNE_REPOS["kitsune-0.3b"] and folder == "nvfp4-w4a16"
    assert "nvfp4-w4a16/model.safetensors" in patterns and "nvfp4-w4a16/quantization.json" in patterns
    assert all(p.startswith("nvfp4-w4a16/") for p in patterns)
    _repo, folder, patterns = server.kitsune_download_plan("kitsune-0.3b")
    assert folder == "" and "model.safetensors" in patterns and all("/" not in p for p in patterns)
    with pytest.raises(ValueError, match="unknown precision"):
        server.kitsune_download_plan("kitsune-0.3b-int4")


def fake_hub(monkeypatch, tmp_path, make=("", "int8-w8a16")):
    """snapshot_download that lays out the files it was asked for (of the folders in `make`)."""
    calls = []

    def snapshot_download(repo_id, cache_dir=None, allow_patterns=None, **kwargs):
        calls.append((repo_id, list(allow_patterns)))
        root = tmp_path / ("models--" + repo_id.replace("/", "--")) / "snapshots" / "abc"
        for pattern in allow_patterns:
            folder = pattern.rsplit("/", 1)[0] if "/" in pattern else ""
            if folder in make and pattern.endswith(("config.json", "model.safetensors")):
                (root / pattern).parent.mkdir(parents=True, exist_ok=True)
                (root / pattern).write_text("{}" if pattern.endswith(".json") else "", encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return str(root)

    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(server, "kitsune_runtime_missing", lambda: None)
    return calls


def test_a_kitsune_download_returns_its_precisions_folder(monkeypatch, tmp_path):
    calls = fake_hub(monkeypatch, tmp_path)
    path = server.download_model_files("kitsune-0.6b-int8")
    assert path.replace("\\", "/").endswith("snapshots/abc/int8-w8a16")
    assert calls[0][0] == server.KITSUNE_REPOS["kitsune-0.6b"]
    assert server.download_model_files("kitsune-0.6b").replace("\\", "/").endswith("snapshots/abc")
    assert server.downloaded_models() == ["kitsune-0.6b", "kitsune-0.6b-int8"]


def test_a_precision_the_repo_does_not_have_is_refused_with_a_word_on_why(monkeypatch, tmp_path):
    fake_hub(monkeypatch, tmp_path, make=("",))
    with pytest.raises(ValueError, match="may not be published yet") as err:
        server.download_model_files("kitsune-0.6b-fp8")
    assert "fp8-w8a8" in server.friendly_model_error(err.value, "kitsune-0.6b-fp8")


def test_without_pytorch_nothing_is_downloaded(monkeypatch, tmp_path):
    calls = fake_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "kitsune_runtime_missing", lambda: server.KITSUNE_INSTALL_HINT)
    with pytest.raises(ValueError) as err:
        server.download_model_files("kitsune-0.6b")
    assert calls == []
    assert server.friendly_model_error(err.value, "kitsune-0.6b") == server.KITSUNE_INSTALL_HINT


def test_download_model_keeps_a_kitsune_choice_and_fetches_its_folder(monkeypatch, tmp_path, capsys):
    calls = fake_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    assert server.run_download_model("kitsune-0.1b-int8-w8a8") == 0
    assert server.read_config()["model"] == "kitsune-0.1b-int8"
    assert all(p.startswith("int8-w8a16/") for p in calls[0][1])
    out = capsys.readouterr().out
    assert "Downloading kitsune-0.1b-int8 (about 120 MB)" in out and "is ready" in out


def test_download_model_refuses_an_unknown_precision(monkeypatch, tmp_path, capsys):
    fake_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    assert server.run_download_model("kitsune-0.1b-int4") == 2
    assert "unknown precision" in capsys.readouterr().out
    assert "model" not in server.read_config()


def test_a_local_folder_is_a_kitsune_model_by_its_config(tmp_path):
    folder = write_package(tmp_path / "student", arch="CohereAsrForConditionalGeneration")
    assert server.is_kitsune_model(folder)
    assert server.is_kitsune_model("kitsune-0.6b")
    assert not server.is_kitsune_model("large-v3")
    whisper = tmp_path / "ct2"
    whisper.mkdir()
    (whisper / "model.bin").write_bytes(b"")
    assert not server.is_kitsune_model(str(whisper))


def test_a_kitsune_model_is_refused_for_another_language(monkeypatch):
    args = SimpleNamespace(language="en", device="auto", compute_type="auto", cpu_threads=0)
    with pytest.raises(ValueError, match="Japanese only") as err:
        server.load_kitsune_model(args, "kitsune-0.6b")
    assert "Japanese only" in server.friendly_model_error(err.value, "kitsune-0.6b")


def test_load_model_hands_a_kitsune_name_to_the_kitsune_loader(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "load_kitsune_model", lambda args, name, path, strict: seen.append((name, path)) or "ok")
    args = SimpleNamespace(model="kitsune-0.3b-int8", language="ja")
    assert server.load_model(args) == "ok"
    assert server.load_model(args, "kitsune-0.6b", path="/x") == "ok"
    assert seen == [("kitsune-0.3b-int8", None), ("kitsune-0.6b", "/x")]


# --------------------------------------------------------------------------- the transcriber's rules

class FakeKitsune:
    """What the transcriber reads of a Kitsune model: no language head, no lyrics path, no prompt."""

    detects_language = False
    sings = False
    takes_prompt = False
    family = "ctc"

    def __init__(self):
        self.calls = []

    def detect_language(self, audio=None, **kwargs):
        raise AssertionError("the language watch must not ask a Kitsune model")

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        words = [server.Word("これは", 1.0, 2.0, 0.9), server.Word("テストです", 2.0, 3.0, 0.9)]
        return iter([SimpleNamespace(text="これはテストです", start=1.0, end=3.0, words=words, compression_ratio=1.0)]), None


def make_worker(monkeypatch, tmp_path, model, speech=None, **overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "detect_speech", speech or (lambda audio, offset=0.0: [[offset + 0.5, offset + len(audio) / RATE - 0.5]]))
    base = dict(first_window=20.0, window=20.0, lookahead=900.0, model="kitsune-0.6b", language="ja",
                language_patience=30.0, beam_size=5, initial_prompt="はい。", idle_minutes=30, client_timeout=30.0,
                lyrics="auto")
    base.update(overrides)
    app = server.App(SimpleNamespace(**base), model=model, device="cpu", compute_type="bfloat16")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return server.Transcriber(app)


def test_a_kitsune_window_skips_the_language_watch_and_gets_no_prompt(monkeypatch, tmp_path):
    model = FakeKitsune()
    worker = make_worker(monkeypatch, tmp_path, model)
    s = server.Session(video_id="abcdefabcdef", url="u", status="ready",
                       audio=np.zeros(60 * RATE, dtype=np.float32), duration=60.0)
    worker.process(s, 0.0, 20.0)
    assert len(model.calls) == 1  # no second, unprompted decode either (retry_prompt_skips)
    assert model.calls[0]["initial_prompt"] is None and model.calls[0]["vad_filter"] is True
    assert s.cues and s.covered == [[0.0, 20.0]]
    assert worker.app.health()["engine"] == "kitsune"


def test_a_loud_window_without_speech_is_no_lyrics_window_for_kitsune(monkeypatch, tmp_path):
    model = FakeKitsune()
    worker = make_worker(monkeypatch, tmp_path, model, speech=lambda audio, offset=0.0: [])
    rng = np.random.default_rng(1)
    s = server.Session(video_id="abcdefabcdef", url="u", status="ready",
                       audio=(0.3 * rng.standard_normal(60 * RATE)).astype(np.float32), duration=60.0)
    worker.process(s, 0.0, 20.0)
    assert model.calls[0]["vad_filter"] is True  # the detector's path, never the lyrics path
    assert s.covered == [[0.0, 20.0]]


def test_whisper_keeps_its_prompt_and_its_lyrics_path():
    args = SimpleNamespace(language="ja", beam_size=5, initial_prompt="はい。")
    whisper = SimpleNamespace()
    assert server.transcribe_options(args, whisper, False)["initial_prompt"] == "はい。"
    assert server.transcribe_options(args, whisper, True)["vad_filter"] is False
    assert server.transcribe_options(args, FakeKitsune(), False)["initial_prompt"] is None


# --------------------------------------------------------------------------- kitsune_setup.py

def load_kitsune_setup():
    import importlib.util
    import sys
    from pathlib import Path

    name = "shisuko_kitsune_setup"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(server.__file__).with_name("kitsune_setup.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def test_pytorch_comes_from_its_cuda_index_with_an_nvidia_gpu_and_from_the_cpu_index_without():
    ks = load_kitsune_setup()
    assert ks.torch_command("py", True, "win32")[-2:] == ["--index-url", ks.TORCH_CUDA_INDEX]
    assert ks.torch_command("py", False, "linux")[-2:] == ["--index-url", ks.TORCH_CPU_INDEX]
    assert ks.torch_command("py", True, "darwin") == ["py", "-m", "pip", "install", "--upgrade", "torch"]


def fake_setup(monkeypatch, gpus, imports, torch=None):
    """kitsune_setup with nvidia-smi's list, pip and the import check scripted; `imports` answers
    installed() in turn."""
    ks = load_kitsune_setup()
    answers = list(imports)
    calls = []
    monkeypatch.setattr(ks, "nvidia_gpus", lambda: list(gpus))
    monkeypatch.setattr(ks, "installed", lambda python=None: answers.pop(0) if answers else None)
    monkeypatch.setattr(ks, "torch_version", lambda python=None: torch)
    monkeypatch.setattr(ks.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    return ks, calls


def test_setup_installs_the_cuda_build_then_the_requirements(monkeypatch):
    ks, calls = fake_setup(monkeypatch, ["GPU 0: NVIDIA GeForce RTX 4070"], [None, "2.14.0 5.13.1"])
    assert ks.main([]) == 0
    assert ks.TORCH_CUDA_INDEX in calls[0] or ks.sys.platform == "darwin"
    assert calls[1][-2:] == ["-r", str(ks.REQUIREMENTS)]


def test_setup_leaves_an_installed_pytorch_alone_unless_forced(monkeypatch):
    ks, calls = fake_setup(monkeypatch, [], ["2.14.0 5.13.1", "2.14.0 5.13.1"])
    assert ks.main([]) == 0
    assert len(calls) == 1 and "-r" in calls[0]  # the requirements only
    ks, calls = fake_setup(monkeypatch, ["GPU 0: x"], ["2.14.0 5.13.1", "2.14.0 5.13.1"])
    assert ks.main(["--force", "--cpu"]) == 0
    assert "torch" in calls[0] and ks.TORCH_CUDA_INDEX not in calls[0]


def test_setup_fails_when_pytorch_does_not_import_afterwards(monkeypatch):
    ks, _calls = fake_setup(monkeypatch, [], [None, None])
    assert ks.main([]) == 1


def test_setup_does_not_download_pytorch_again_when_only_transformers_is_missing(monkeypatch):
    ks, calls = fake_setup(monkeypatch, ["GPU 0: x"], [None, "2.14.0 5.13.1"], torch="2.14.0")
    assert ks.main([]) == 0
    assert len(calls) == 1 and "-r" in calls[0]


# --------------------------------------------------------------------------- review round's regressions

def test_a_kitsune_name_is_not_downloaded_for_a_server_of_another_language(monkeypatch, tmp_path):
    calls = fake_hub(monkeypatch, tmp_path)
    app = SimpleNamespace(args=SimpleNamespace(language="en"), default_model="large-v3", lock=__import__("threading").Lock(),
                          model_error=None, model_failed_at=0.0, model_preparing="kitsune-0.6b",
                          model_loading="kitsune-0.6b", wanted_model="kitsune-0.6b", model_name="large-v3",
                          model_prepared=None)
    server.App.prepare_model(app, "kitsune-0.6b")
    assert calls == []
    assert app.model_error[0] == "kitsune-0.6b" and "Japanese only" in app.model_error[1]


def test_a_clients_name_is_never_looked_at_as_a_folder(monkeypatch, tmp_path):
    # reload_model() hands load_model() a name without a path; a folder by that name in the working
    # directory must not be loaded as a package unless the operator named it with --model.
    folder = write_package(tmp_path / "owner" / "name", arch="ParakeetForCTC")
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(server, "load_kitsune_model", lambda *a, **k: seen.append(a[1]) or "kitsune")
    import types
    fake_fw = types.ModuleType("faster_whisper")
    fake_fw.WhisperModel = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("whisper path"))
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", fake_fw)
    monkeypatch.setattr(server, "gpu_memory_mb", lambda: None)
    args = SimpleNamespace(model="large-v3", device="cpu", compute_type="auto", cpu_threads=0, language="ja")
    with pytest.raises(RuntimeError, match="whisper path"):
        server.load_model(args, "owner/name")
    assert seen == []
    assert server.load_model(SimpleNamespace(model=folder, language="ja"), folder) == "kitsune"


def test_a_recipe_that_does_not_fit_its_tensors_is_a_package_error(tmp_path):
    torch = pytest.importorskip("torch")
    safetensors_torch = pytest.importorskip("safetensors.torch")
    path = write_package(tmp_path, recipe={"schema": 1, "format": "int8-w8a16", "weights": "int8",
                                           "layers": {"encoder.x": {"shape": [2, 3, 4]}}})
    safetensors_torch.save_file({"encoder.x.qweight": torch.zeros((2, 3), dtype=torch.int8),
                                 "encoder.x.qscale": torch.ones(2)}, str(tmp_path / "model.safetensors"))
    with pytest.raises(engine.PackageError, match="does not unpack"):
        engine.variant_state_dict(engine.read_package(path))


def test_an_operator_folder_named_like_a_kitsune_model_stays_the_folder_under_its_canonical_name(monkeypatch, tmp_path):
    # The App keeps --model canonical (kitsune-0.6b-int8 for a folder named ...-int8-w8a8), and a
    # failed switch reloads it by that name: it must still be the folder, never the hub's model.
    monkeypatch.chdir(tmp_path)
    write_package(tmp_path / "kitsune-0.6b-int8-w8a8", arch="ParakeetForCTC")
    args = SimpleNamespace(model="kitsune-0.6b-int8-w8a8", language="ja")
    assert server.operator_folder(args, "kitsune-0.6b-int8") == "kitsune-0.6b-int8-w8a8"
    assert server.operator_folder(args, "kitsune-0.6b") is None
    assert server.operator_folder(SimpleNamespace(model="kitsune-0.6b-int8-w8a8"), "kitsune-0.6b-int8-w8a8") \
        == "kitsune-0.6b-int8-w8a8"
    monkeypatch.setattr(server, "kitsune_runtime_missing", lambda: None)
    monkeypatch.setattr(server, "download_model_files", lambda name: pytest.fail("the hub was asked for the operator's folder"))
    loaded = []
    model = SimpleNamespace(device="cpu", compute_label="float32", transcribe=lambda *a, **k: ([], None))
    monkeypatch.setattr(server, "kitsune_engine", lambda: SimpleNamespace(
        load=lambda path, **k: loaded.append(path) or model, PackageError=ValueError, architecture_of=lambda p: "ParakeetForCTC"))
    full = SimpleNamespace(model="kitsune-0.6b-int8-w8a8", language="ja", device="cpu", compute_type="auto", cpu_threads=0)
    server.load_model(full, "kitsune-0.6b-int8")
    assert loaded == ["kitsune-0.6b-int8-w8a8"]


def test_an_import_error_inside_the_engine_is_no_ctranslate2_import_error(monkeypatch, tmp_path):
    # start_app() spares the AMD crash guard's count for an ImportError (CTranslate2's); a
    # transformers too old for the architecture must not pass for one.
    monkeypatch.setattr(server, "kitsune_runtime_missing", lambda: None)
    monkeypatch.setattr(server, "gpu_memory_mb", lambda: None)

    def load(*a, **k):
        raise ImportError("cannot import name 'ParakeetForCTC' from 'transformers'")

    monkeypatch.setattr(server, "kitsune_engine", lambda: SimpleNamespace(load=load, PackageError=KeyError))
    args = SimpleNamespace(model="x", language="ja", device="auto", compute_type="auto", cpu_threads=0)
    with pytest.raises(ValueError, match="kitsune_setup.py --force"):
        server.load_kitsune_model(args, "kitsune-0.1b", path=str(tmp_path))
