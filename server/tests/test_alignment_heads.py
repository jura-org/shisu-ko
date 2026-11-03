"""A converted Whisper model's word-timing heads must fit its decoder (safe_alignment_heads()).

CTranslate2 files config.json's alignment_heads by decoder layer without a bounds check: kotoba-whisper's
conversions list large-v3's heads (layers 7 to 25) for a decoder of 2 layers, and the server died with an
access violation on the first window with word timings. The model.bin files here are written in
CTranslate2's own layout (python/ctranslate2/specs/model_spec.py), with tiny weights.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()

LARGE_V3_HEADS = [[7, 0], [10, 17], [12, 18], [13, 12], [16, 1], [17, 14], [19, 11], [21, 4], [24, 1], [25, 6]]
KOTOBA_FIXED = [[1, head] for head in range(20)]  # what Systran's faster-distil-whisper-large-v3 lists


def ct2_string(text: str) -> bytes:
    data = text.encode("utf-8") + b"\0"
    return struct.pack("<H", len(data)) + data


def write_model_bin(folder, decoder_layers=2, heads=20, encoder_layers=4, version=6):
    """model.bin with a few variables per layer, decoder/num_heads (int16) and one alias, as CTranslate2 writes it."""
    variables = {"decoder/embeddings/weight": struct.pack("<4e", 0, 0, 0, 0),
                 "decoder/num_heads": struct.pack("<h", heads),
                 "encoder/num_heads": struct.pack("<h", heads)}
    for i in range(decoder_layers):
        variables[f"decoder/layer_{i}/self_attention/linear_0/weight"] = struct.pack("<2e", 0, 0)
        variables[f"decoder/layer_{i}/attention/linear_1/bias"] = struct.pack("<2e", 0, 0)
    for i in range(encoder_layers):
        variables[f"encoder/layer_{i}/self_attention/linear_0/weight"] = struct.pack("<2e", 0, 0)
    out = struct.pack("<I", version)
    if version >= 2:
        out += ct2_string("WhisperSpec") + struct.pack("<I", 3)
    out += struct.pack("<I", len(variables))
    for name in sorted(variables):
        data = variables[name]
        scalar = name.endswith("/num_heads")
        out += ct2_string(name) + (struct.pack("<B", 0) if scalar else struct.pack("<BI", 1, len(data) // 2))
        if version >= 4:
            out += struct.pack("<BI", 2 if scalar else 4, len(data))  # int16 / float16, then the size in bytes
        else:
            out += struct.pack("<BI", 2, len(data) // 2)  # the item size, then the count
        out += data
    if version >= 3:
        out += struct.pack("<I", 1) + ct2_string("decoder/projection/weight") + ct2_string("decoder/embeddings/weight")
    (folder / "model.bin").write_bytes(out)


def converted(folder, alignment=LARGE_V3_HEADS, **shape):
    """A converted model's folder: model.bin of `shape`, and a config.json naming `alignment` (no key for None)."""
    folder.mkdir(parents=True, exist_ok=True)
    write_model_bin(folder, **shape)
    config = {"lang_ids": [50259, 50260], "suppress_ids": [1, 2], "suppress_ids_begin": [220, 50257]}
    if alignment is not None:
        config["alignment_heads"] = alignment
    (folder / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return folder


def config_of(folder) -> dict:
    return json.loads((folder / "config.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- ct2_decoder_shape

@pytest.mark.parametrize("version", [6, 5, 4, 3, 2])
def test_the_decoder_shape_comes_from_the_variable_table(tmp_path, version):
    write_model_bin(tmp_path, decoder_layers=2, heads=20, encoder_layers=32, version=version)
    assert server.ct2_decoder_shape(str(tmp_path)) == (2, 20), "the encoder's layers never count"


def test_the_decoder_shape_counts_layers_as_ctranslate2_builds_them(tmp_path):
    write_model_bin(tmp_path, decoder_layers=4, heads=6)
    assert server.ct2_decoder_shape(str(tmp_path)) == (4, 6)
    # CTranslate2 stops at the first layer missing; a stray later one is not part of the decoder.
    data = (tmp_path / "model.bin").read_bytes().replace(b"decoder/layer_2/", b"decoder/layer_9/")
    (tmp_path / "model.bin").write_bytes(data)
    assert server.ct2_decoder_shape(str(tmp_path)) == (2, 6)


def test_a_model_bin_the_reader_does_not_know_has_no_shape(tmp_path):
    assert server.ct2_decoder_shape(str(tmp_path)) is None, "no model.bin"
    write_model_bin(tmp_path)
    whole = (tmp_path / "model.bin").read_bytes()
    (tmp_path / "model.bin").write_bytes(whole[:len(whole) // 2])
    assert server.ct2_decoder_shape(str(tmp_path)) is None, "cut short"
    (tmp_path / "model.bin").write_bytes(struct.pack("<I", 7) + whole[4:])
    assert server.ct2_decoder_shape(str(tmp_path)) is None, "a binary version from after this reader"
    (tmp_path / "model.bin").write_bytes(b"")
    assert server.ct2_decoder_shape(str(tmp_path)) is None, "empty"
    (tmp_path / "model.bin").write_bytes(whole.replace(b"decoder/num_heads", b"decoder/num_xxxxx"))
    assert server.ct2_decoder_shape(str(tmp_path)) is None, "no head count"


# --------------------------------------------------------------------------- safe_alignment_heads

def test_kotoba_whispers_large_v3_heads_become_every_head_of_its_last_layer(tmp_path, caplog):
    folder = converted(tmp_path / "kotoba")
    before = config_of(folder)
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        server.safe_alignment_heads(str(folder), "kotoba-tech/kotoba-whisper-v2.0-faster")
    after = config_of(folder)
    assert after["alignment_heads"] == KOTOBA_FIXED
    assert {k: v for k, v in after.items() if k != "alignment_heads"} == \
        {k: v for k, v in before.items() if k != "alignment_heads"}, "nothing else in config.json changes"
    assert list(after) == list(before), "nor its order"
    assert "kotoba-tech/kotoba-whisper-v2.0-faster" in caplog.text and "2-layer, 20-head decoder" in caplog.text
    assert not (folder / "config.json.tmp").exists()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        server.safe_alignment_heads(str(folder), "kotoba-tech/kotoba-whisper-v2.0-faster")
    assert caplog.text == "", "the corrected file is left alone the next time"


def test_the_default_takes_the_second_half_of_a_deeper_decoder(tmp_path):
    folder = converted(tmp_path / "m", alignment=[[4, 0]], decoder_layers=4, heads=6)
    server.safe_alignment_heads(str(folder), "m")
    assert config_of(folder)["alignment_heads"] == [[layer, head] for layer in (2, 3) for head in range(6)]


@pytest.mark.parametrize("alignment", [
    [], [[1, 20]], [[-1, 0]], [[1, -1]], [[1]], [[1, 0, 0]], [[1, "0"]], [[1, 1.0]], [[True, 0]], [None], {"1": 0}, "1,0",
])
def test_any_list_ctranslate2_cannot_use_is_replaced(tmp_path, alignment):
    folder = converted(tmp_path / "m", alignment=alignment)
    server.safe_alignment_heads(str(folder), "m")
    assert config_of(folder)["alignment_heads"] == KOTOBA_FIXED


def test_a_config_without_heads_gets_them(tmp_path, caplog):
    folder = converted(tmp_path / "m", alignment=None)
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        server.safe_alignment_heads(str(folder), "m")
    assert config_of(folder)["alignment_heads"] == KOTOBA_FIXED
    assert "named no word-timing heads" in caplog.text


@pytest.mark.parametrize("alignment", [[[0, 0], [3, 5]], [[3, 0]], [[2, 2], [3, 0], [3, 5]]])
def test_heads_that_fit_are_left_byte_for_byte(tmp_path, alignment):
    folder = converted(tmp_path / "m", alignment=alignment, decoder_layers=4, heads=6)
    before = (folder / "config.json").read_bytes()
    server.safe_alignment_heads(str(folder), "m")
    assert (folder / "config.json").read_bytes() == before


def test_a_folder_the_reader_cannot_judge_is_left_alone(tmp_path):
    folder = converted(tmp_path / "m")
    (folder / "model.bin").write_bytes(b"\0\0")
    before = (folder / "config.json").read_bytes()
    server.safe_alignment_heads(str(folder), "m")
    assert (folder / "config.json").read_bytes() == before
    write_model_bin(folder)
    (folder / "config.json").unlink()
    server.safe_alignment_heads(str(folder), "m")  # no config.json: CTranslate2's own error, no crash
    assert not (folder / "config.json").exists()
    (folder / "config.json").write_text("{not json", encoding="utf-8")
    server.safe_alignment_heads(str(folder), "m")
    assert (folder / "config.json").read_text(encoding="utf-8") == "{not json"


def test_a_config_that_cannot_be_corrected_refuses_the_load(tmp_path, monkeypatch):
    folder = converted(tmp_path / "m")

    def replace(src, dst):
        raise PermissionError(13, "Access is denied", dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(ValueError, match="names no word-timing heads its 2-layer decoder has"):
        server.safe_alignment_heads(str(folder), "m")
    assert config_of(folder)["alignment_heads"] == LARGE_V3_HEADS


# --------------------------------------------------------------------------- load_model

def whisper_that_reads_the_heads(monkeypatch, tmp_path):
    """faster_whisper whose WhisperModel reads config.json's heads at construction, as CTranslate2 does; the
    hub folder of every name is tmp_path/hub/<name>, a kotoba-style conversion. Returns (folder, heads) records."""
    seen = []

    class WhisperModel:
        def __init__(self, folder, **kwargs):
            with open(os.path.join(folder, "config.json"), encoding="utf-8") as f:
                seen.append((folder, json.load(f)["alignment_heads"]))

        def transcribe(self, *a, **k):
            return iter(()), None

    def download_model(name, cache_dir=None, **kwargs):
        folder = tmp_path / "hub" / name.replace("/", "--")
        if not folder.exists():
            converted(folder)
        return str(folder)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel, download_model=download_model))
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    return seen


def cpu_args(model):
    return SimpleNamespace(model=model, device="cpu", compute_type="auto", cpu_threads=0, language="ja")


def test_load_model_corrects_the_heads_before_ctranslate2_reads_them(monkeypatch, tmp_path):
    seen = whisper_that_reads_the_heads(monkeypatch, tmp_path)
    server.load_model(cpu_args("kotoba-tech/kotoba-whisper-v2.0-faster"))  # a start: --model is a repo id
    prepared = converted(tmp_path / "prepared")
    server.load_model(cpu_args("large-v3"), "owner/other", path=str(prepared))  # a switch: download_model_files() made it
    folder = converted(tmp_path / "operator")
    server.load_model(cpu_args(str(folder)))  # the operator's own folder
    assert seen == [(str(tmp_path / "hub" / "kotoba-tech--kotoba-whisper-v2.0-faster"), KOTOBA_FIXED),
                    (str(prepared), KOTOBA_FIXED), (str(folder), KOTOBA_FIXED)]


def test_load_model_refuses_a_model_whose_heads_cannot_be_corrected(monkeypatch, tmp_path):
    seen = whisper_that_reads_the_heads(monkeypatch, tmp_path)

    def replace(src, dst):
        raise PermissionError(13, "Access is denied", dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(ValueError, match="could not be corrected"):
        server.load_model(cpu_args("kotoba-tech/kotoba-whisper-v2.0-faster"))
    assert seen == [], "CTranslate2 never got the model that would crash it"
