"""The Apple GPU backend: --device mlx decodes Whisper through MLX instead of CTranslate2.

CTranslate2 has no Metal backend, so on Apple Silicon faster-whisper runs on the CPU; MLX runs the
same weights on the GPU. MlxWhisperModel wears faster-whisper's WhisperModel interface, so nothing
else in the server knows which of the two is loaded, and that disguise is what these tests check.

Nothing here needs a Mac, a GPU, a model or the network: mlx.core, mlx_whisper, huggingface_hub and
faster_whisper are faked in sys.modules the way test_model_switch.py fakes faster_whisper, and
sys.platform is monkeypatched where the code asks for the operating system.
"""
from __future__ import annotations

import bisect
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
RATE = server.SAMPLE_RATE

# A slice of faster_whisper.utils._MODELS: an alias and its repo id are the same weights.
FAKE_MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "small": "Systran/faster-whisper-small",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
}
LARGE_MLX = "mlx-community/whisper-large-v3-mlx"


@pytest.fixture(autouse=True)
def fresh_alias_table(monkeypatch):
    # canonical_model_name() remembers faster-whisper's table once it could import it; every
    # test starts without one so a fake installed by an earlier test cannot leak into it.
    monkeypatch.setattr(server, "_MODEL_ALIASES", None)


@pytest.fixture(autouse=True)
def beam_search_off(monkeypatch):
    # enable_mlx_beam_search() remembers its answer for the life of the process, so without this
    # every test below would inherit whatever the first MlxWhisperModel built anywhere decided.
    # False is the fallback this file's fakes describe: the model wrapped in greedy mlx-whisper.
    # A test about the beam says so itself; test_mlx_beam.py covers the decoder that lifts it.
    monkeypatch.setattr(server, "_MLX_BEAM", False)


# --------------------------------------------------------------------------- the fake libraries

def fake_faster_whisper(monkeypatch, tmp_path):
    """faster_whisper with the alias table and a download_model() that records its names.

    load_model() imports WhisperModel before it looks at the device, and canonical_model_name()
    needs the table to know that large and large-v3 are one model. Returns the list of names the
    CTranslate2 download was asked for, which must stay empty on the MLX path.
    """
    downloads = []

    def download_model(name, cache_dir=None, **kwargs):
        downloads.append(name)
        directory = tmp_path / "ct2" / name.replace("/", "--")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "model.bin").write_bytes(b"\0")
        return str(directory)

    package = types.ModuleType("faster_whisper")
    package.utils = SimpleNamespace(_MODELS=dict(FAKE_MODELS))
    package.download_model = download_model
    package.WhisperModel = lambda *a, **k: pytest.fail("the MLX backend must not build a CTranslate2 model")
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    return downloads


class MxArray:
    """What mx.array() returns, so the fake mlx-whisper can tell one from a numpy array."""

    def __init__(self, samples):
        self.samples = np.asarray(samples)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, item):
        return MxArray(self.samples[item])


def fake_mlx(monkeypatch, metal=True):
    """mlx.core: the dtypes MlxWhisperModel picks and the Metal check mlx_available() makes."""
    core = types.ModuleType("mlx.core")
    core.__version__ = "0.22.0"
    core.float16, core.float32 = "float16", "float32"
    core.array = MxArray
    core.metal = SimpleNamespace(is_available=lambda: metal)
    package = types.ModuleType("mlx")
    package.core = core
    monkeypatch.setitem(sys.modules, "mlx", package)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return core


def fake_mlx_whisper(monkeypatch, result=None, probabilities=None, n_mels=128):
    """mlx_whisper with a recording transcribe(), a scripted detector and a fresh model cache.

    Returns a namespace: `calls` is one (audio, kwargs) pair per decode, `holder` is the module
    level cache MlxWhisperModel must clear when it is dropped, `mels` what detect_language built.
    """
    calls, mels = [], []

    def transcribe(audio, **kwargs):
        calls.append((np.asarray(audio), kwargs))
        return dict(result if result is not None else {"segments": [], "language": "ja"})

    class ModelHolder:
        model = None
        model_path = None

        @classmethod
        def get_model(cls, path, dtype):
            cls.model_path, cls.model = path, SimpleNamespace(dims=SimpleNamespace(n_mels=n_mels), dtype=dtype)
            return cls.model

    def log_mel_spectrogram(audio, n_mels=80):
        mels.append((len(audio), n_mels))
        return "mel"

    def detect_language(model, mel):
        return ["<|ja|>"], probabilities

    transcribe_module = types.ModuleType("mlx_whisper.transcribe")
    transcribe_module.ModelHolder = ModelHolder
    audio_module = types.ModuleType("mlx_whisper.audio")
    audio_module.N_SAMPLES = 30 * RATE
    def pad_or_trim(audio, length):
        # mlx-whisper pads with mx.pad, which refuses a numpy array; a window shorter than the
        # 30 s encoder frame (nearly all of them) goes through this path every time.
        assert isinstance(audio, MxArray), "detect_language must hand MLX an mx.array"
        return audio[:length]

    audio_module.pad_or_trim = pad_or_trim
    audio_module.log_mel_spectrogram = log_mel_spectrogram
    decoding_module = types.ModuleType("mlx_whisper.decoding")
    decoding_module.detect_language = detect_language
    package = types.ModuleType("mlx_whisper")
    package.transcribe = transcribe
    for name, module in (("mlx_whisper", package), ("mlx_whisper.transcribe", transcribe_module),
                         ("mlx_whisper.audio", audio_module), ("mlx_whisper.decoding", decoding_module)):
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(calls=calls, holder=ModelHolder, mels=mels)


def fake_hub(monkeypatch, tmp_path, files=("config.json", "weights.safetensors")):
    """huggingface_hub.snapshot_download() over a temp directory; returns what it was asked for."""
    seen = {}

    def snapshot_download(repo_id=None, cache_dir=None, allow_patterns=None, **kwargs):
        seen.update(repo_id=repo_id, cache_dir=cache_dir, allow_patterns=allow_patterns)
        directory = tmp_path / "snapshots" / repo_id.replace("/", "--")
        directory.mkdir(parents=True, exist_ok=True)
        for name in files:
            (directory / name).write_bytes(b"\0")
        return str(directory)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    return seen


def snapshot_dir(tmp_path, repo):
    return str(tmp_path / "snapshots" / repo.replace("/", "--"))


def fake_vad(monkeypatch, chunks):
    """faster_whisper.vad with a scripted Silero result; the arithmetic below is the library's own.

    MlxWhisperModel emulates vad_filter=True with exactly these three helpers, so the fake keeps
    their real behaviour (concatenate the speech, add the silence before a chunk back to a time)
    and records the options it was given.
    """
    seen = {}

    class VadOptions(SimpleNamespace):
        pass

    def get_speech_timestamps(audio, options, sampling_rate=None):
        seen.update(options=options, sampling_rate=sampling_rate, samples=len(audio))
        return [dict(c) for c in chunks]

    def collect_chunks(audio, chunks, sampling_rate=None):
        # faster-whisper merges every chunk into one array when max_duration is left at infinity.
        merged = np.concatenate([audio[c["start"]:c["end"]] for c in chunks])
        return [merged], [{}]

    class SpeechTimestampsMap:
        def __init__(self, chunks, sampling_rate, time_precision=2):
            self.sampling_rate, self.time_precision = sampling_rate, time_precision
            self.chunk_end_sample, self.total_silence_before = [], []
            previous_end = silent_samples = 0
            for chunk in chunks:
                silent_samples += chunk["start"] - previous_end
                previous_end = chunk["end"]
                self.chunk_end_sample.append(chunk["end"] - silent_samples)
                self.total_silence_before.append(silent_samples / sampling_rate)

        def get_chunk_index(self, time, is_end=False):
            sample = int(time * self.sampling_rate)
            if sample in self.chunk_end_sample and is_end:
                return self.chunk_end_sample.index(sample)
            return min(bisect.bisect(self.chunk_end_sample, sample), len(self.chunk_end_sample) - 1)

        def get_original_time(self, time, chunk_index=None, is_end=False):
            if chunk_index is None:
                chunk_index = self.get_chunk_index(time, is_end)
            return round(self.total_silence_before[chunk_index] + time, self.time_precision)

    module = types.ModuleType("faster_whisper.vad")
    module.VadOptions = VadOptions
    module.get_speech_timestamps = get_speech_timestamps
    module.collect_chunks = collect_chunks
    module.SpeechTimestampsMap = SpeechTimestampsMap
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", module)
    return seen


def make_model(monkeypatch, compute_type="float16", path="/models/whisper-large-v3-mlx"):
    fake_mlx(monkeypatch)
    return server.MlxWhisperModel(path, compute_type)


SEGMENT_RESULT = {
    "language": "ja",
    "segments": [{
        "start": 0.5, "end": 1.5, "text": "これはテストです",
        "words": [{"word": "これは", "start": 0.5, "end": 1.0, "probability": 0.9},
                  {"word": "テストです", "start": 1.0, "end": 1.5, "probability": 0.8}],
    }],
}


# --------------------------------------------------------------------------- picking the backend

def test_auto_prefers_cuda_then_the_apple_gpu_then_the_cpu(monkeypatch):
    monkeypatch.setattr(server, "cuda_available", lambda: True)
    monkeypatch.setattr(server, "mlx_available", lambda: True)
    assert server.resolve_device("auto") == "cuda"  # an NVIDIA GPU is the fastest of the three
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    assert server.resolve_device("auto") == "mlx"
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    assert server.resolve_device("auto") == "cpu"


@pytest.mark.parametrize("device", ["cpu", "cuda", "mlx"])
def test_a_device_the_operator_named_is_never_second_guessed(monkeypatch, device):
    # --device cpu on a Mac means the CPU; a backend that cannot run must fail loudly instead of
    # quietly running somewhere else, so only "auto" is ever resolved.
    monkeypatch.setattr(server, "cuda_available", lambda: True)
    monkeypatch.setattr(server, "mlx_available", lambda: True)
    assert server.resolve_device(device) == device


def test_mlx_is_available_only_on_a_mac_with_metal(monkeypatch):
    fake_mlx(monkeypatch)
    fake_mlx_whisper(monkeypatch)
    monkeypatch.setattr(server.sys, "platform", "darwin")
    assert server.mlx_available() is True
    for platform in ("linux", "win32"):
        monkeypatch.setattr(server.sys, "platform", platform)
        assert server.mlx_available() is False, "MLX is Apple's; elsewhere the import is not even tried"


def test_mlx_without_metal_or_without_the_libraries_is_not_available(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    fake_mlx_whisper(monkeypatch)
    fake_mlx(monkeypatch, metal=False)
    assert server.mlx_available() is False  # an Intel Mac carries the library but has no Metal device
    fake_mlx(monkeypatch)
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # the import fails: pip install mlx-whisper first
    assert server.mlx_available() is False
    fake_mlx_whisper(monkeypatch)
    monkeypatch.setitem(sys.modules, "mlx", None)
    assert server.mlx_available() is False


# --------------------------------------------------------------------------- names

def test_an_mlx_repo_is_the_same_weights_as_its_size(monkeypatch, tmp_path):
    # The cue cache is keyed on the canonical name, so a video transcribed with large-v3 on the CPU
    # and with the MLX build on the GPU must be one cache, not two.
    fake_faster_whisper(monkeypatch, tmp_path)
    assert server.canonical_model_name(LARGE_MLX) == "large-v3"
    assert server.canonical_model_name("mlx-community/whisper-tiny-mlx") == "tiny"
    assert server.canonical_model_name("Systran/faster-whisper-large-v3") == "large-v3"
    assert server.canonical_model_name("large") == "large-v3"
    assert server.canonical_model_name("mlx-community/whisper-nonesuch") == "mlx-community/whisper-nonesuch"


def test_every_mlx_repo_names_one_size():
    # The reverse table is built from the forward one, so a repo listed under two sizes would
    # silently lose one of them and canonical_model_name() would answer with the wrong weights.
    assert len(server.MLX_ALIASES) == len(server.MLX_REPOS)
    assert all(server.MLX_REPOS[alias] == repo for repo, alias in server.MLX_ALIASES.items())


def test_model_spellings_offers_the_mlx_repo_last(monkeypatch, tmp_path):
    # The popup matches the model field's text against this list, so a viewer on a Mac who typed
    # the mlx-community repo gets the verdict on a failed download too.
    fake_faster_whisper(monkeypatch, tmp_path)
    assert server.model_spellings("large-v3") == ["large-v3", "large", "Systran/faster-whisper-large-v3", LARGE_MLX]
    assert server.model_spellings("tiny") == ["tiny", "Systran/faster-whisper-tiny", "mlx-community/whisper-tiny-mlx"]
    # A model nobody converted has no MLX name. The MLX repo itself answers with the whole set:
    # it is looked up through its canonical name, so it knows the sizes it was converted from.
    assert server.model_spellings("kotoba-tech/kotoba-whisper-v2.0-faster") == ["kotoba-tech/kotoba-whisper-v2.0-faster"]
    assert server.model_spellings(LARGE_MLX) == ["large-v3", "large", "Systran/faster-whisper-large-v3", LARGE_MLX]


def test_mlx_repo_for_a_size_an_alias_or_a_repo_id(monkeypatch, tmp_path):
    fake_faster_whisper(monkeypatch, tmp_path)
    assert server.mlx_repo_for("large-v3") == LARGE_MLX
    assert server.mlx_repo_for("large") == LARGE_MLX  # through faster-whisper's alias table
    assert server.mlx_repo_for("Systran/faster-whisper-large-v3") == LARGE_MLX
    assert server.mlx_repo_for(LARGE_MLX) == LARGE_MLX
    # A repo the table does not know passes through: whether it holds MLX weights the download decides.
    assert server.mlx_repo_for("owner/whisper-something-mlx") == "owner/whisper-something-mlx"


def test_a_bare_name_without_an_mlx_build_is_refused(monkeypatch, tmp_path):
    # A size that is not in the table cannot be guessed into a repo id, and asking the hub for one
    # would only produce a 404 the viewer cannot read.
    fake_faster_whisper(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="no MLX build"):
        server.mlx_repo_for("whisper-jp")


# --------------------------------------------------------------------------- the download

def test_the_weights_are_fetched_into_the_servers_models_directory(monkeypatch, tmp_path):
    fake_faster_whisper(monkeypatch, tmp_path)
    seen = fake_hub(monkeypatch, tmp_path)
    assert server.download_mlx_model_files("large") == snapshot_dir(tmp_path, LARGE_MLX)
    assert seen["repo_id"] == LARGE_MLX
    assert seen["cache_dir"] == str(tmp_path / "models")  # one folder for both backends, under ~/.shisu-ko
    assert seen["allow_patterns"] == ["*.json", "*.safetensors", "*.npz"]  # no PyTorch checkpoint


def test_npz_weights_count_as_a_model_too(monkeypatch, tmp_path):
    # mlx_whisper.convert writes weights.npz; the community repos ship safetensors.
    fake_hub(monkeypatch, tmp_path, files=("config.json", "weights.npz"))
    assert server.download_mlx_model_files("owner/converted") == snapshot_dir(tmp_path, "owner/converted")


@pytest.mark.parametrize("files", [("config.json",), ("weights.safetensors",), ()])
def test_a_repo_that_is_not_an_mlx_model_is_refused_before_the_gpu_sees_it(monkeypatch, tmp_path, files):
    # The twin of the no-model.bin rule: the refusal happens on the download thread, where it costs
    # nothing but the download, instead of in the swap, which has already freed the working model.
    fake_hub(monkeypatch, tmp_path, files=files)
    with pytest.raises(ValueError, match="not an MLX Whisper model"):
        server.download_mlx_model_files("owner/pytorch-only")


def test_the_device_decides_which_files_are_downloaded(monkeypatch, tmp_path):
    ct2 = fake_faster_whisper(monkeypatch, tmp_path)
    hub = fake_hub(monkeypatch, tmp_path)
    assert server.download_model_files("large-v3", "mlx") == snapshot_dir(tmp_path, LARGE_MLX)
    assert ct2 == [] and hub["repo_id"] == LARGE_MLX
    hub.clear()
    for device in ((), ("cpu",), ("cuda",)):  # the default and both CTranslate2 backends
        assert server.download_model_files("large-v3", *device) == str(tmp_path / "ct2" / "large-v3")
    assert ct2 == ["large-v3"] * 3 and hub == {}


# --------------------------------------------------------------------------- transcribe

def test_faster_whispers_options_arrive_as_mlx_whispers(monkeypatch):
    mlx = fake_mlx_whisper(monkeypatch, result=SEGMENT_RESULT)
    model = make_model(monkeypatch)
    segments, info = model.transcribe(
        np.zeros(RATE, dtype=np.float32), language="ja", beam_size=5, word_timestamps=True,
        vad_filter=False, vad_parameters=server.vad_parameters(), log_prob_threshold=-0.8,
        temperature=[0.0, 0.2], condition_on_previous_text=False, no_speech_threshold=0.5,
        compression_ratio_threshold=2.0, initial_prompt="prompt", hallucination_silence_threshold=2.0)

    (audio, kwargs), = mlx.calls
    assert audio.dtype == np.float32 and len(audio) == RATE
    assert kwargs["logprob_threshold"] == -0.8  # mlx-whisper spells it without the underscore
    assert "beam_size" not in kwargs  # no decoder was installed: see the beam test below
    assert "vad_filter" not in kwargs and "vad_parameters" not in kwargs  # faster-whisper's alone
    assert kwargs["path_or_hf_repo"] == model.path and kwargs["fp16"] is True
    assert kwargs["temperature"] == (0.0, 0.2) and kwargs["language"] == "ja"
    assert (kwargs["word_timestamps"], kwargs["condition_on_previous_text"]) == (True, False)
    assert (kwargs["no_speech_threshold"], kwargs["compression_ratio_threshold"]) == (0.5, 2.0)
    assert (kwargs["initial_prompt"], kwargs["hallucination_silence_threshold"]) == ("prompt", 2.0)
    assert info.language == "ja"

    seg, = segments
    assert (seg.start, seg.end, seg.text) == (0.5, 1.5, "これはテストです")
    assert [(w.word, w.start, w.end, w.probability) for w in seg.words] == [
        ("これは", 0.5, 1.0, 0.9), ("テストです", 1.0, 1.5, 0.8)]
    assert all(isinstance(w, server.Word) for w in seg.words), "the cue builder reads real Word objects"


@pytest.mark.parametrize("installed, asked, crosses", [
    (True, 5, 5),
    (True, 1, None),      # a beam of one is greedy decoding: there is nothing to search
    (True, None, None),   # detect_language and the warm-up name no beam size at all
    (False, 5, None),     # nothing was installed, so the library would raise NotImplementedError
])
def test_the_beam_size_crosses_only_when_there_is_a_decoder_for_it(monkeypatch, installed, asked, crosses):
    # mlx-whisper ships a greedy decoder and refuses any beam_size outright; mlx_beam.py gives it
    # one, and self.beam is whether that took. Where it did not, the option must be dropped rather
    # than passed on, which is the fallback the rest of this file's fakes describe.
    monkeypatch.setattr(server, "_MLX_BEAM", installed)
    mlx = fake_mlx_whisper(monkeypatch)
    model = make_model(monkeypatch)
    assert model.beam is installed
    options = {"language": "ja"}
    if asked is not None:
        options["beam_size"] = asked
    model.transcribe(np.zeros(RATE, dtype=np.float32), **options)
    assert mlx.calls[0][1].get("beam_size") == crosses


def test_a_segment_without_word_timestamps_still_becomes_one(monkeypatch):
    # The warm-up and a --no-word-timestamps run ask for none, and mlx-whisper then leaves the key
    # out; a word whose start is missing is dropped rather than carried as None into a cue.
    result = {"segments": [{"start": 0.0, "end": 2.0, "text": "x"},
                           {"text": "y", "words": [{"word": "y", "start": None, "end": 1.0}]}]}
    fake_mlx_whisper(monkeypatch, result=result)
    model = make_model(monkeypatch)
    segments, info = model.transcribe(np.zeros(RATE, dtype=np.float32), language="ja")
    assert [(s.start, s.end, s.text, s.words) for s in segments] == [(0.0, 2.0, "x", []), (0.0, 0.0, "y", [])]
    assert info.language == "ja"  # mlx-whisper said nothing: the language asked for is the answer


def test_float32_turns_fp16_off(monkeypatch):
    mlx = fake_mlx_whisper(monkeypatch)
    model = make_model(monkeypatch, compute_type="float32")
    model.transcribe(np.zeros(RATE, dtype=np.float32), language="ja")
    assert mlx.calls[0][1]["fp16"] is False
    assert model.dtype == "float32" and make_model(monkeypatch).dtype == "float16"


# --------------------------------------------------------------------------- the VAD emulation

def test_silence_is_cut_out_and_the_timestamps_come_back(monkeypatch):
    # mlx-whisper has no vad_filter, so the model runs faster-whisper's own Silero pass, decodes
    # the speech alone and maps the times back. Without the map every cue would be early by the
    # silence before it, which on a music video is minutes.
    seen = fake_vad(monkeypatch, [{"start": 2 * RATE, "end": 4 * RATE}, {"start": 6 * RATE, "end": 7 * RATE}])
    mlx = fake_mlx_whisper(monkeypatch, result={"language": "ja", "segments": [
        {"start": 0.5, "end": 1.5, "text": "a", "words": [{"word": "a", "start": 0.5, "end": 1.5, "probability": 0.9}]},
        {"start": 2.2, "end": 2.8, "text": "b", "words": []},
    ]})
    model = make_model(monkeypatch)
    audio = np.arange(10 * RATE, dtype=np.float32)
    segments, info = model.transcribe(audio, language="ja", vad_filter=True,
                                      vad_parameters=server.vad_parameters(), word_timestamps=True)

    # The same Silero settings the server's own pass uses, so both see the same intervals.
    assert vars(seen["options"]) == dict(server.VAD_PARAMS, max_speech_duration_s=server.VAD_MAX_SPEECH_SECONDS)
    assert (seen["sampling_rate"], seen["samples"]) == (RATE, 10 * RATE)
    heard, _ = mlx.calls[0]
    assert len(heard) == 3 * RATE  # two seconds and one, the four seconds of silence gone
    # 0.5 s into the first chunk is 2.5 s in the video; 2.2 s is inside the second, after four
    # seconds of silence in all, so 6.2 s.
    assert [(s.start, s.end, s.text) for s in segments] == [(2.5, 3.5, "a"), (6.2, 6.8, "b")]
    assert [(w.word, w.start, w.end) for w in segments[0].words] == [("a", 2.5, 3.5)]
    assert info.language == "ja"


def test_a_window_without_speech_is_never_decoded(monkeypatch):
    # Silero found nothing, so there is nothing to hear; starting the GPU for it would cost a
    # window's worth of time and invite a hallucination over the silence.
    fake_vad(monkeypatch, [])
    mlx = fake_mlx_whisper(monkeypatch)
    model = make_model(monkeypatch)
    segments, info = model.transcribe(np.zeros(RATE, dtype=np.float32), language="ja", vad_filter=True)
    assert segments == [] and info.language == "ja" and mlx.calls == []


# --------------------------------------------------------------------------- detect_language

@pytest.mark.parametrize("wrap", [lambda probs: [probs], lambda probs: probs])
def test_detect_language_reports_the_most_probable_one(monkeypatch, wrap):
    # mlx-whisper answers a batch (a list of one dict); the language watch unpacks the same triple
    # faster-whisper returns, and reads the first two of it.
    probs = {"en": 0.2, "ja": 0.7, "ko": 0.1}
    mlx = fake_mlx_whisper(monkeypatch, probabilities=wrap(probs), n_mels=128)
    model = make_model(monkeypatch)
    language, probability, everything = model.detect_language(audio=np.zeros(5 * RATE, dtype=np.float32))
    assert (language, probability) == ("ja", 0.7) and everything == probs
    assert mlx.mels == [(5 * RATE, 128)], "the mel bands come from the loaded model, not from a guess"
    assert mlx.holder.model_path == model.path, "the decoder's cache is reused: no second copy of the weights"


def test_detection_and_transcription_share_one_loaded_model(monkeypatch):
    mlx = fake_mlx_whisper(monkeypatch, probabilities=[{"ja": 1.0}])
    model = make_model(monkeypatch, compute_type="float32")
    model.detect_language(audio=np.zeros(RATE, dtype=np.float32))
    assert mlx.holder.model.dtype == "float32"  # the dtype --compute-type asked for, not mlx's default


def test_dropping_the_model_frees_mlx_whispers_cached_weights(monkeypatch):
    # switch_model_if_wanted() drops its reference and collects before loading the next model, so
    # that two models never sit in GPU memory at once; mlx-whisper's module-level cache would
    # otherwise hold the old weights until a window decoded.
    mlx = fake_mlx_whisper(monkeypatch)
    model = make_model(monkeypatch, path="/models/a")
    mlx.holder.model, mlx.holder.model_path = "weights", "/models/a"
    model.__del__()
    assert (mlx.holder.model, mlx.holder.model_path) == (None, None)

    mlx.holder.model, mlx.holder.model_path = "weights", "/models/b"
    model.__del__()
    assert mlx.holder.model_path == "/models/b", "another model's weights are not thrown away"


# --------------------------------------------------------------------------- load_model

def mlx_args(**overrides):
    base = dict(model="large-v3", device="mlx", compute_type="auto", cpu_threads=0, language="ja")
    base.update(overrides)
    return SimpleNamespace(**base)


def test_load_model_builds_the_mlx_backend_and_warms_it_up(monkeypatch, tmp_path):
    fake_faster_whisper(monkeypatch, tmp_path)  # its WhisperModel fails the test if it is built
    hub = fake_hub(monkeypatch, tmp_path)
    fake_mlx(monkeypatch)
    mlx = fake_mlx_whisper(monkeypatch)
    monkeypatch.setattr(server, "_MLX_BEAM", True)  # even with the beam decoder installed:
    model, device, compute = server.load_model(mlx_args())
    assert isinstance(model, server.MlxWhisperModel) and (device, compute) == ("mlx", "float16")
    assert model.path == snapshot_dir(tmp_path, LARGE_MLX) and hub["repo_id"] == LARGE_MLX
    # The warm-up went through MLX: two seconds of silence and no VAD. It asks for a beam of one,
    # which is greedy decoding, so no beam_size crosses and two seconds of silence cost one pass.
    (audio, kwargs), = mlx.calls
    assert len(audio) == 2 * RATE and kwargs["language"] == "ja" and "beam_size" not in kwargs


def test_a_prepared_directory_is_loaded_without_asking_the_hub(monkeypatch, tmp_path):
    fake_faster_whisper(monkeypatch, tmp_path)
    hub = fake_hub(monkeypatch, tmp_path)
    fake_mlx(monkeypatch)
    fake_mlx_whisper(monkeypatch)
    prepared = str(tmp_path / "prepared")
    model, device, _ = server.load_model(mlx_args(), "small", path=prepared)
    assert model.path == prepared and device == "mlx" and hub == {}


def test_the_operators_compute_type_is_honoured(monkeypatch, tmp_path):
    fake_faster_whisper(monkeypatch, tmp_path)
    fake_hub(monkeypatch, tmp_path)
    fake_mlx(monkeypatch)
    mlx = fake_mlx_whisper(monkeypatch)
    model, _, compute = server.load_model(mlx_args(compute_type="float32"))
    assert compute == "float32" and model.fp16 is False and mlx.calls[0][1]["fp16"] is False


def test_device_auto_on_a_mac_loads_mlx(monkeypatch, tmp_path):
    fake_faster_whisper(monkeypatch, tmp_path)
    fake_hub(monkeypatch, tmp_path)
    fake_mlx(monkeypatch)
    fake_mlx_whisper(monkeypatch)
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: True)
    model, device, compute = server.load_model(mlx_args(device="auto"))
    assert isinstance(model, server.MlxWhisperModel) and (device, compute) == ("mlx", "float16")


# --------------------------------------------------------------------------- the switch

class InertTranscriber:
    """Stands in for the transcriber thread so the test is the only caller of the switch."""

    def __init__(self, app):
        self.app = app

    def start(self):
        pass


def make_app(monkeypatch, tmp_path, device="mlx"):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    args = SimpleNamespace(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3",
                           language="ja", idle_minutes=30, retry_after=30.0)
    app = server.App(args, model=object(), device=device, compute_type="float16")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


def test_a_switch_on_the_apple_gpu_prepares_mlx_weights(monkeypatch, tmp_path):
    # App.device is the backend the loaded model runs on, so the files prepare_model() fetches are
    # the ones the swap will need: CTranslate2's model.bin would be useless to MLX.
    ct2 = fake_faster_whisper(monkeypatch, tmp_path)
    hub = fake_hub(monkeypatch, tmp_path)
    loaded = []
    monkeypatch.setattr(server, "load_model",
                        lambda args, name=None, path=None: (loaded.append((name, path)), (object(), "mlx", "float16"))[1])
    app = make_app(monkeypatch, tmp_path)
    app.request_model("small")

    assert app.switch_model_if_wanted() is False  # the files first, on the prepare thread
    app.prepare_thread.join(5.0)
    assert hub["repo_id"] == "mlx-community/whisper-small-mlx" and ct2 == []
    assert app.model_prepared == ("small", snapshot_dir(tmp_path, "mlx-community/whisper-small-mlx"))

    assert app.switch_model_if_wanted() is True
    assert app.model_name == "small" and app.model_error is None
    assert loaded == [("small", snapshot_dir(tmp_path, "mlx-community/whisper-small-mlx"))]


def test_a_model_nobody_converted_is_reported_instead_of_crashing(monkeypatch, tmp_path):
    # On the Apple GPU a CTranslate2-only model cannot be loaded at all. The viewer must read why
    # in the popup, and the working model must stay: this costs nothing but the failed lookup.
    fake_faster_whisper(monkeypatch, tmp_path)
    fake_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("nothing to load"))
    app = make_app(monkeypatch, tmp_path)
    old_model = app.model
    app.request_model("whisper-jp")

    assert app.switch_model_if_wanted() is False
    app.prepare_thread.join(5.0)
    assert app.switch_model_if_wanted() is False
    assert app.model is old_model and app.model_name == "large-v3"
    assert app.model_error[0] == "whisper-jp" and "no MLX build" in app.model_error[1]
    assert app.wanted_model == "large-v3"  # nothing retries on its own
    assert app.sync("abcdefabcdef", "u", 0.0, 0, "whisper-jp")["model_error"].startswith("there is no MLX build")
