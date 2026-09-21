"""Setup asks for the Whisper model: `server.py --download-model NAME` fetches it with progress bars
and writes ~/.shisu-ko/config.json, which parse_args() reads as the --model default from then on.

faster_whisper and huggingface_hub are fakes in sys.modules (a snapshot_download() that records
its keyword arguments and writes model.bin into the directory it returns), so nothing here
downloads or loads a model, and run_check() sees stand-ins for ctranslate2, yt-dlp and the
registry as well, so it never initialises a GPU driver. The backend is faked too: the download
follows resolve_device("auto"), so cuda_available() and mlx_available() are pinned rather than
asked, and the machine the tests run on cannot change what they assert. The launchers are checked
as text, like run.cmd / run.sh in test_update_endpoint.py.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()
SERVER_DIR = Path(__file__).resolve().parent.parent

# A slice of faster_whisper.utils._MODELS: the two sizes setup offers.
FAKE_MODELS = {"small": "Systran/faster-whisper-small", "large-v3": "Systran/faster-whisper-large-v3"}
MODEL_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    """Everything server.py reads or writes at setup lives under tmp_path, and the alias table starts empty."""
    monkeypatch.setattr(server, "APP_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(server, "_MODEL_ALIASES", None)
    return tmp_path


@pytest.fixture(autouse=True)
def on_the_cpu(monkeypatch):
    """The backend is pinned, not read off the machine: everything below describes the CTranslate2 path.

    run_download_model() asks resolve_device("auto") which conversion this computer would load,
    so on an Apple Silicon Mac with mlx-whisper installed the same command fetches MLX weights
    from mlx-community instead (the tests at the end of the --download-model section turn that
    machine on again). Without this the file would pass or fail by where it runs.
    """
    monkeypatch.setattr(server, "cuda_available", lambda: False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)


def on_the_apple_gpu(monkeypatch):
    """Undo the fixture for one test: a Mac whose resolve_device("auto") answers "mlx".

    Faking the machine is enough; mlx.core and mlx_whisper never have to exist, because the setup
    download goes to the Hub and loads nothing (test_mlx.py fakes the libraries themselves).
    """
    monkeypatch.setattr(server, "mlx_available", lambda: True)
    assert server.resolve_device("auto") == "mlx"


class DisabledTqdm:
    """Stands for faster_whisper.utils.disabled_tqdm, the class the setup download must not pass on."""


def fake_hub(monkeypatch, tmp_path, fail=None, bare=False, table=None, block=None, files=("model.bin",)):
    """faster_whisper (its size table and disabled tqdm) and huggingface_hub with a recording snapshot_download().

    The download comes back with a directory under tmp_path holding `files` (model.bin, or the
    MLX weights an mlx-community repo ships), none of them with `bare`, or raises `fail`; with
    `block`, a pair of Events, it sets the first and waits for the second first, a download that
    is still streaming. Returns the list of (repo_id, kwargs) it was called with.
    """
    calls = []

    def snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        if block is not None:
            started, release = block
            started.set()
            release.wait()
        if fail is not None:
            raise fail
        directory = tmp_path / "snapshots" / repo_id.replace("/", "--")
        directory.mkdir(parents=True, exist_ok=True)
        if not bare:
            for name in files:
                (directory / name).write_bytes(b"\0")
        return str(directory)

    utils = SimpleNamespace(_MODELS=dict(FAKE_MODELS if table is None else table), disabled_tqdm=DisabledTqdm)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(utils=utils, disabled_tqdm=DisabledTqdm, __version__="0"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    return calls


class Untouchable:
    """A sys.modules entry the test fails on at the first attribute access: the library must not be imported."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        pytest.fail(f"{self.name} was imported (asked for {attr})")


def run_main(monkeypatch, *argv):
    """main() the way setup calls it; returns the SystemExit code. The lock and the model load are forbidden."""
    monkeypatch.setattr(sys, "argv", ["server.py", *argv])
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: pytest.fail("the instance lock was taken"))
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    with pytest.raises(SystemExit) as info:
        server.main()
    return info.value.code


def config(tmp_path):
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


# --- config.json --------------------------------------------------------------------------------

def test_read_config_without_a_file_is_empty(tmp_path, caplog):
    assert server.read_config() == {}
    assert caplog.records == []


def test_read_config_ignores_a_corrupt_file_with_a_warning(tmp_path, caplog):
    (tmp_path / "config.json").write_text('{"model": "small"', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        assert server.read_config() == {}
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "config.json" in caplog.records[0].getMessage()


def test_read_config_ignores_anything_but_an_object(tmp_path, caplog):
    (tmp_path / "config.json").write_text('["small"]', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        assert server.read_config() == {}
    assert "not a JSON object" in caplog.text


def test_write_config_merges_and_leaves_only_the_file_behind(tmp_path):
    server.write_config({"model": "large-v3", "other": 1})
    server.write_config({"model": "small"})
    assert config(tmp_path) == {"model": "small", "other": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"], "the temporary file was replaced, not left"
    assert (tmp_path / "config.json").read_bytes().endswith(b"}\n")


def test_write_config_replaces_a_corrupt_file(tmp_path):
    (tmp_path / "config.json").write_text("not json", encoding="utf-8")
    server.write_config({"model": "small"})
    assert config(tmp_path) == {"model": "small"}


def test_write_config_drops_a_key_for_none(tmp_path):
    server.write_config({"model": "small", "other": 1})
    server.write_config({"model": None, "missing": None})
    assert config(tmp_path) == {"other": 1}
    assert server.configured_model() is None


# --- parse_args(): the --model default -----------------------------------------------------------

def test_the_flag_wins_over_the_config(tmp_path):
    server.write_config({"model": "small"})
    assert server.parse_args(["--model", "x"]).model == "x"


def test_the_config_wins_over_the_built_in_default(tmp_path):
    server.write_config({"model": "small"})
    assert server.parse_args([]).model == "small"


def test_without_a_config_the_default_is_large_v3(tmp_path):
    assert server.DEFAULT_MODEL == "large-v3"
    assert server.parse_args([]).model == "large-v3"
    assert not (tmp_path / "config.json").exists(), "reading never creates the file"


@pytest.mark.parametrize("value", [7, "", None, ["small"], {"name": "small"}, True])
def test_a_config_model_that_is_not_a_name_is_ignored(tmp_path, value):
    (tmp_path / "config.json").write_text(json.dumps({"model": value}), encoding="utf-8")
    assert server.parse_args([]).model == "large-v3"


def test_the_download_flag_takes_a_name_and_defaults_to_none():
    assert server.parse_args(["--download-model", "small"]).download_model == "small"
    assert server.parse_args([]).download_model is None


# --- --download-model ---------------------------------------------------------------------------

def test_download_fetches_with_the_bars_on_and_writes_the_canonical_name(monkeypatch, tmp_path, capsys):
    calls = fake_hub(monkeypatch, tmp_path)
    assert run_main(monkeypatch, "--download-model", "Systran/faster-whisper-small") == 0
    (repo_id, kwargs), = calls
    assert repo_id == "Systran/faster-whisper-small"
    assert kwargs["cache_dir"] == str(tmp_path / "models")
    assert kwargs["allow_patterns"] == MODEL_FILES, "the five files faster-whisper's download_model() fetches"
    assert "tqdm_class" not in kwargs, "huggingface_hub's own bars, not faster-whisper's disabled ones"
    assert config(tmp_path) == {"model": "small"}, "the repo id is stored under its alias"
    out = capsys.readouterr().out
    assert f"Downloading small (about 500 MB) into {tmp_path / 'models'} ..." in out
    assert f"Model small is ready in {tmp_path / 'snapshots' / 'Systran--faster-whisper-small'}." in out
    assert (tmp_path / "models").is_dir(), "created before the download, like every start does"


def test_download_by_size_alias_resolves_the_repo_id(monkeypatch, tmp_path, capsys):
    calls = fake_hub(monkeypatch, tmp_path)
    assert run_main(monkeypatch, "--download-model", "large-v3") == 0
    assert [repo for repo, _ in calls] == ["Systran/faster-whisper-large-v3"]
    assert config(tmp_path) == {"model": "large-v3"}
    assert "Downloading large-v3 (about 3 GB) into" in capsys.readouterr().out


def test_download_keeps_the_other_config_keys(monkeypatch, tmp_path):
    server.write_config({"other": "kept"})
    fake_hub(monkeypatch, tmp_path)
    assert run_main(monkeypatch, "--download-model", "small") == 0
    assert config(tmp_path) == {"model": "small", "other": "kept"}


@pytest.mark.parametrize("name, size", [
    ("large-v3", "about 3 GB"), ("large-v3-turbo", "about 1.6 GB"), ("medium", "about 1.5 GB"),
    ("distil-large-v3", "about 1.5 GB"), ("small", "about 500 MB"), ("base", "about 150 MB"),
    ("tiny", "about 75 MB"), ("kotoba-tech/kotoba-whisper-v2.0-faster", "size unknown"),
])
def test_download_announces_the_size(monkeypatch, tmp_path, capsys, name, size):
    table = {n: f"Systran/faster-whisper-{n}" for n in ("large-v3", "large-v3-turbo", "medium", "distil-large-v3",
                                                        "small", "base", "tiny")}
    fake_hub(monkeypatch, tmp_path, table=table)
    assert run_main(monkeypatch, "--download-model", name) == 0
    assert f"Downloading {name} ({size}) into" in capsys.readouterr().out


def test_unknown_size_lists_the_sizes_and_writes_nothing(monkeypatch, tmp_path, capsys):
    calls = fake_hub(monkeypatch, tmp_path)
    assert run_main(monkeypatch, "--download-model", "medium") == 2
    assert calls == []
    out = capsys.readouterr().out
    assert "unknown model size 'medium'" in out and "small, large-v3" in out
    assert not (tmp_path / "config.json").exists()


def test_a_repo_without_model_bin_is_refused(monkeypatch, tmp_path, capsys):
    fake_hub(monkeypatch, tmp_path, bare=True)
    assert run_main(monkeypatch, "--download-model", "openai/whisper-small") == 2
    out = capsys.readouterr().out
    assert "Could not download openai/whisper-small: openai/whisper-small is not a CTranslate2/faster-whisper model (no model.bin)" in out
    assert not (tmp_path / "config.json").exists()


def test_a_size_whose_repo_holds_no_model_bin_is_taken_back(monkeypatch, tmp_path, capsys):
    # A size from the table is written before the download (the next tests say why); files that
    # turn out not to be a model give the previous choice back, or leave none.
    fake_hub(monkeypatch, tmp_path, bare=True)
    assert run_main(monkeypatch, "--download-model", "small") == 2
    assert "no model.bin" in capsys.readouterr().out
    assert "model" not in server.read_config() and server.parse_args([]).model == "large-v3"
    server.write_config({"model": "large-v3", "other": 1})
    assert run_main(monkeypatch, "--download-model", "small") == 2
    assert config(tmp_path) == {"model": "large-v3", "other": 1}


CONNECTION_ERROR = ConnectionError("HTTPSConnectionPool(host='huggingface.co'): Max retries exceeded")


def test_a_failed_download_prints_the_friendly_line(monkeypatch, tmp_path, capsys):
    fake_hub(monkeypatch, tmp_path, fail=CONNECTION_ERROR)
    assert run_main(monkeypatch, "--download-model", "small") == 2
    out, err = capsys.readouterr()
    assert "Could not download small: could not reach Hugging Face to download 'small'" in out
    assert "Traceback" not in err


def test_a_failed_download_keeps_the_choice_for_the_first_start(monkeypatch, tmp_path):
    # Setup says the server downloads the model on its first start when the download failed: true
    # only when the choice survives the failure. A size from faster-whisper's table is one
    # WhisperModel() fetches by itself, so it is written before the download.
    fake_hub(monkeypatch, tmp_path, fail=CONNECTION_ERROR)
    assert run_main(monkeypatch, "--download-model", "small") == 2
    assert config(tmp_path) == {"model": "small"}
    assert server.parse_args([]).model == "small", "the next run.cmd / run.sh start loads small, not large-v3"


def test_a_failed_repo_id_is_not_kept(monkeypatch, tmp_path, capsys):
    # A typo in owner/name, or a repo that is no model, must not make every later start exit 2.
    fake_hub(monkeypatch, tmp_path, fail=CONNECTION_ERROR)
    assert run_main(monkeypatch, "--download-model", "owner/name") == 2
    assert "could not reach Hugging Face to download 'owner/name'" in capsys.readouterr().out
    assert not (tmp_path / "config.json").exists()
    server.write_config({"model": "small"})
    assert run_main(monkeypatch, "--download-model", "owner/name") == 2
    assert config(tmp_path) == {"model": "small"}, "the earlier choice stands"


def interrupted_download(monkeypatch, tmp_path):
    """A download that is still streaming when Ctrl+C arrives in the main thread's wait.

    The fake snapshot_download() blocks until the test releases it, and the wait is replaced by
    one that raises KeyboardInterrupt once the download thread is inside it; os._exit() raises
    SystemExit with its code instead of ending pytest. Returns a function that releases the
    download (it then fails, touching nothing on disk) and joins its thread.
    """
    started, release = threading.Event(), threading.Event()
    threads = []
    fake_hub(monkeypatch, tmp_path, fail=CONNECTION_ERROR, block=(started, release))

    def wait_for_thread(thread):
        threads.append(thread)
        assert started.wait(5), "the download did not start"
        assert thread.is_alive() and thread.daemon
        raise KeyboardInterrupt

    def _exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(server, "wait_for_thread", wait_for_thread)
    monkeypatch.setattr(os, "_exit", _exit)

    def finish():
        release.set()
        for thread in threads:
            thread.join(5)

    return finish


def test_ctrl_c_ends_the_download_at_once_and_keeps_the_choice(monkeypatch, tmp_path, capsys):
    # The download has not returned when the interrupt arrives, and the handler must not wait for
    # it (a real snapshot_download() would go on streaming model.bin for minutes): the process
    # ends from inside it with the launchers' do-not-retry code, the choice already on disk.
    finish = interrupted_download(monkeypatch, tmp_path)
    try:
        assert run_main(monkeypatch, "--download-model", "small") == 2
    finally:
        finish()
    out, err = capsys.readouterr()
    assert "Download interrupted; run setup again to finish it" in out and "Traceback" not in err
    assert config(tmp_path) == {"model": "small"}
    assert server.parse_args([]).model == "small"


def test_ctrl_c_on_a_repo_id_keeps_nothing(monkeypatch, tmp_path):
    finish = interrupted_download(monkeypatch, tmp_path)
    try:
        assert run_main(monkeypatch, "--download-model", "owner/name") == 2
    finally:
        finish()
    assert not (tmp_path / "config.json").exists()


def test_wait_for_thread_joins_in_short_steps_and_lets_an_interrupt_through():
    # Windows delivers Ctrl+C only when a wait returns, so the join is timed; the loop itself
    # must not swallow the KeyboardInterrupt a join then raises.
    done = threading.Thread(target=lambda: None)
    done.start()
    server.wait_for_thread(done)  # returns once the thread is gone
    assert not done.is_alive()
    timeouts = []

    class Recording(threading.Thread):
        def join(self, timeout=None):
            timeouts.append(timeout)
            if len(timeouts) == 3:
                raise KeyboardInterrupt
            super().join(0.01)  # what the caller asked for is recorded; the test need not wait it out

    hold = threading.Event()
    stuck = Recording(target=hold.wait)
    stuck.start()
    with pytest.raises(KeyboardInterrupt):
        server.wait_for_thread(stuck)
    assert timeouts == [0.5, 0.5, 0.5]
    hold.set()
    stuck.join()


def test_the_failure_code_is_one_the_launchers_end_on():
    # run.cmd / run.sh restart the server on every code but 0, 2 and 4 (test_update_endpoint.py
    # pins their loops); a failed --download-model through them must end, not retry every five
    # seconds. setup.cmd's "if errorlevel 1" and setup.sh's "if !" catch 2 as well.
    run_cmd = (SERVER_DIR / "run.cmd").read_bytes().decode("utf-8").split("\r\n")
    assert 'if "%CODE%"=="2" goto end' in run_cmd
    assert '[ "$code" -eq 2 ] && exit 2' in (SERVER_DIR / "run.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["../x", "a b", "/etc/passwd", "C:\\models"])
def test_an_invalid_name_is_refused_before_anything_is_imported(monkeypatch, tmp_path, capsys, name):
    monkeypatch.setitem(sys.modules, "faster_whisper", Untouchable("faster_whisper"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", Untouchable("huggingface_hub"))
    assert run_main(monkeypatch, "--download-model", name) == 2
    out = capsys.readouterr().out
    assert f"'{name}' is {server.MODEL_NAME_HINT}" in out
    assert not (tmp_path / "config.json").exists()


def test_a_python_without_the_libraries_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import raises ImportError
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    assert run_main(monkeypatch, "--download-model", "small") == 2
    assert "faster-whisper is not installed" in capsys.readouterr().out


def test_download_mode_runs_before_the_lock_and_never_loads(monkeypatch, tmp_path):
    # run_main() fails the test from hold_instance_lock() and load_model(); the server must also
    # not listen: ThreadingHTTPServer is replaced by something that fails too.
    fake_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "ThreadingHTTPServer", lambda *a, **k: pytest.fail("the server listened"))
    assert run_main(monkeypatch, "--download-model", "small") == 0


def test_the_switch_and_the_setup_download_refuse_a_bare_repo_alike(tmp_path):
    with pytest.raises(ValueError, match="no model.bin"):
        server.require_model_bin(str(tmp_path), "x")
    (tmp_path / "model.bin").write_bytes(b"\0")
    server.require_model_bin(str(tmp_path), "x")


# --- --download-model on an Apple GPU -------------------------------------------------------------
# The machine that will run the model decides which conversion setup fetches, because the first
# start loads that one: on a Mac where resolve_device("auto") answers "mlx" the CTranslate2 files
# would be dead weight and the viewer would wait for three gigabytes twice.

LARGE_MLX = "mlx-community/whisper-large-v3-mlx"
MLX_WEIGHTS = ("config.json", "weights.safetensors")
MLX_FILES = ["*.json", "*.safetensors", "*.npz"]


def test_download_on_the_apple_gpu_fetches_the_mlx_conversion(monkeypatch, tmp_path, capsys):
    on_the_apple_gpu(monkeypatch)
    calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert run_main(monkeypatch, "--download-model", "large-v3") == 0
    (repo_id, kwargs), = calls
    assert repo_id == LARGE_MLX, "mlx-community's conversion, not Systran's CTranslate2 build"
    assert kwargs["allow_patterns"] == MLX_FILES, "the MLX weights; a model.bin would never be loaded"
    assert kwargs["cache_dir"] == str(tmp_path / "models"), "one models folder for both backends"
    assert config(tmp_path) == {"model": "large-v3"}
    out = capsys.readouterr().out
    assert f"Model large-v3 is ready in {tmp_path / 'snapshots' / 'mlx-community--whisper-large-v3-mlx'}." in out


def test_an_mlx_repo_id_is_stored_under_its_size(monkeypatch, tmp_path):
    # The two names are the same weights, so the config, /health and the cue cache all say large-v3;
    # a viewer who typed the mlx-community repo at setup must not split the cache in two.
    on_the_apple_gpu(monkeypatch)
    calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert run_main(monkeypatch, "--download-model", LARGE_MLX) == 0
    assert [repo for repo, _ in calls] == [LARGE_MLX]
    assert config(tmp_path) == {"model": "large-v3"}


@pytest.mark.parametrize("files", [("config.json",), ("weights.safetensors",), ("model.bin",)])
def test_an_mlx_download_without_weights_is_refused_and_the_choice_taken_back(monkeypatch, tmp_path, capsys, files):
    # require_mlx_weights() judges the directory here, not require_model_bin(): a repo that holds
    # only the CTranslate2 model.bin is no more loadable on the GPU than an empty one, and the size
    # written before the download is given back so the next start does not exit 2 on it forever.
    on_the_apple_gpu(monkeypatch)
    fake_hub(monkeypatch, tmp_path, files=files)
    assert run_main(monkeypatch, "--download-model", "small") == 2
    out = capsys.readouterr().out
    assert "Could not download small: small is not an MLX Whisper model (no config.json beside weights.safetensors)" in out
    assert "model" not in server.read_config() and server.parse_args([]).model == "large-v3"


def test_a_name_without_an_mlx_build_says_so_instead_of_downloading(monkeypatch, tmp_path, capsys):
    # mlx_repo_for() raises for a bare name nobody converted; run_download_model() must turn that
    # into setup's own exit 2 with the reason, not let it out as a traceback the viewer reads as a crash.
    on_the_apple_gpu(monkeypatch)
    calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert run_main(monkeypatch, "--download-model", "whisper-jp") == 2
    assert calls == [], "the hub is never asked for a repo id that cannot be built"
    assert "there is no MLX build of 'whisper-jp'" in capsys.readouterr().out
    assert not (tmp_path / "config.json").exists()


def test_the_named_device_decides_the_download_on_a_mac(monkeypatch, tmp_path, capsys):
    # --device cpu on a Mac means the CPU, here as everywhere else: the operator who runs the
    # server on CTranslate2 must get its files at setup, or the first start downloads the whole
    # model a second time. The machine is only consulted for --device auto.
    on_the_apple_gpu(monkeypatch)
    calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert run_main(monkeypatch, "--device", "cpu", "--download-model", "large-v3") == 2
    (repo_id, kwargs), = calls
    assert repo_id == "Systran/faster-whisper-large-v3" and kwargs["allow_patterns"] == MODEL_FILES
    assert "large-v3 is not a CTranslate2/faster-whisper model (no model.bin)" in capsys.readouterr().out
    again = fake_hub(monkeypatch, tmp_path)  # the same repo with model.bin: those files are enough
    assert run_main(monkeypatch, "--device", "cpu", "--download-model", "large-v3") == 0
    assert [repo for repo, _ in again] == ["Systran/faster-whisper-large-v3"]
    assert config(tmp_path) == {"model": "large-v3"}


def test_the_named_device_decides_the_download_without_a_mac_too(monkeypatch, tmp_path, capsys):
    # The other direction, so that the rule is the device's and not the machine's: --device mlx is
    # refused by load_model() where MLX cannot run, and a download that quietly fetched something
    # else would hide that behind a missing-weights error hours later.
    calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert server.resolve_device("auto") == "cpu", "the fixture's machine has neither GPU"
    assert run_main(monkeypatch, "--device", "mlx", "--download-model", "large-v3") == 0
    (repo_id, kwargs), = calls
    assert repo_id == LARGE_MLX and kwargs["allow_patterns"] == MLX_FILES
    assert config(tmp_path) == {"model": "large-v3"}
    assert f"Model large-v3 is ready in {tmp_path / 'snapshots' / 'mlx-community--whisper-large-v3-mlx'}." \
        in capsys.readouterr().out


def test_the_device_defaults_to_auto_so_a_bare_call_asks_the_machine(monkeypatch, tmp_path, capsys):
    # Every test above that names no device relies on this default; the tools and any later caller
    # get the machine's own backend without having to say so.
    on_the_apple_gpu(monkeypatch)
    mlx_calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert server.run_download_model("large-v3") == 0
    monkeypatch.setattr(server, "mlx_available", lambda: False)
    ct2_calls = fake_hub(monkeypatch, tmp_path)
    assert server.run_download_model("large-v3") == 0
    assert [repo for repo, _ in mlx_calls] == [LARGE_MLX]
    assert [repo for repo, _ in ct2_calls] == ["Systran/faster-whisper-large-v3"]
    capsys.readouterr()


def test_main_hands_the_download_the_device_it_was_started_with(monkeypatch, tmp_path):
    # The argument is only useful if main() passes it on; a refactor that drops it would leave
    # every test above green except this one, since the fixture's machine and "auto" agree.
    asked = []
    monkeypatch.setattr(server, "run_download_model", lambda *a, **k: (asked.append((a, k)), 0)[1])
    assert run_main(monkeypatch, "--device", "mlx", "--download-model", "small") == 0
    assert run_main(monkeypatch, "--download-model", "small") == 0
    assert asked == [(("small", "mlx"), {}), (("small", "auto"), {})]


def test_without_an_apple_gpu_the_same_call_takes_the_ctranslate2_files(monkeypatch, tmp_path, capsys):
    # The twin of the first test, same command and same files in the repo, on the machine the
    # autouse fixture describes: faster-whisper's repo id, its five files, and model.bin required.
    calls = fake_hub(monkeypatch, tmp_path, files=MLX_WEIGHTS)
    assert run_main(monkeypatch, "--download-model", "large-v3") == 2
    (repo_id, kwargs), = calls
    assert repo_id == "Systran/faster-whisper-large-v3" and kwargs["allow_patterns"] == MODEL_FILES
    assert "Could not download large-v3: large-v3 is not a CTranslate2/faster-whisper model (no model.bin)" \
        in capsys.readouterr().out


# --- run_check() --------------------------------------------------------------------------------

def no_registry(*args):
    raise OSError("no such key")


def test_run_check_names_the_default_model(monkeypatch, tmp_path, capsys):
    # run_check() imports ctranslate2 (which would initialise the CUDA driver where the library is
    # installed: the venv, nix run .#tests), faster_whisper and yt_dlp, and loads native_host.py to
    # look the launcher up in the registry and under ~/.shisu-ko: every one of them is a stand-in
    # here, and the output shows that they were what the check saw.
    fake_hub(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(__version__="0", get_cuda_device_count=lambda: 0))
    version = SimpleNamespace(__version__="0")
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(version=version))
    monkeypatch.setitem(sys.modules, "yt_dlp.version", version)
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(HKEY_CURRENT_USER=0, REG_SZ=1, OpenKey=no_registry))
    for name in ("SHISUKO_HOME", "USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    server.run_check()
    out = capsys.readouterr().out
    assert "CTranslate2 0: 0 CUDA device(s)" in out and "faster-whisper 0" in out and "yt-dlp 0" in out
    assert "Start button launcher: not registered" in out
    assert "Default model: large-v3 (built-in default)" in out
    server.write_config({"model": "small"})
    server.run_check()
    assert "Default model: small (chosen at setup)" in capsys.readouterr().out


# --- the launchers ------------------------------------------------------------------------------

def cmd_lines():
    raw = (SERVER_DIR / "setup.cmd").read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b""), "setup.cmd must stay CRLF"
    return raw.decode("utf-8").split("\r\n")


def sh_text():
    raw = (SERVER_DIR / "setup.sh").read_bytes()
    assert b"\r" not in raw, "setup.sh must stay LF"
    return raw.decode("utf-8")


def index_of(lines, predicate, what):
    hits = [i for i, line in enumerate(lines) if predicate(line)]
    assert len(hits) == 1, f"{what}: expected exactly one line, found {hits}"
    return hits[0]


def test_setup_cmd_asks_then_downloads_then_says_it_is_done():
    lines = cmd_lines()
    check = index_of(lines, lambda l: l.endswith('"%~dp0server.py" --check'), "--check")
    choice = index_of(lines, lambda l: l == 'choice /c 12 /n /m "Type 1 or 2: "', "choice")
    pick = index_of(lines, lambda l: l == 'if errorlevel 3 (set "MODEL=large-v3") else if errorlevel 2 (set "MODEL=small") else (set "MODEL=large-v3")', "the pick")
    download = index_of(lines, lambda l: l == '"%VENV%\\Scripts\\python.exe" "%~dp0server.py" --download-model %MODEL%', "download")
    done = index_of(lines, lambda l: l == "echo Close this window and start run.cmd.", "the last line")
    assert check < choice < pick < download < done
    # choice's errorlevel is the key's number, or 255 when it cannot read one (stdin closed or
    # empty), and "if errorlevel N" means N or more: 3 is tested first, so that 255 takes large-v3
    # like setup.sh's EOF fallback, then 2; the pick is the first thing after choice that looks at
    # errorlevel (a set inside an if-block resets it to 0).
    assert pick == choice + 1
    errorlevel_tests = [i for i, l in enumerate(lines) if l.startswith("if errorlevel") and i > choice]
    assert errorlevel_tests[0] == pick
    failed = download + 1  # the download's verdict, read right after it
    assert lines[failed] == "if errorlevel 1 (" and lines[failed + 1:failed + 5] == [
        "  echo The model could not be downloaded. Check the connection and run setup.cmd again,",
        "  echo or start run.cmd: the server then downloads %MODEL% itself, without a progress bar.",
        "  pause",
        "  exit /b 1",
    ]
    assert lines[done - 1] == "echo Setup is complete: the %MODEL% model is downloaded and everything is ready."
    assert [l for l in lines[done + 1:] if l] == ["pause"]
    assert not any("downloaded on the first start" in l for l in lines)
    assert any("1  large-v3" in l for l in lines) and any("2  small" in l for l in lines)


def test_setup_sh_asks_then_downloads_then_says_it_is_done():
    text = sh_text()
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    check = text.index('"${HERE}/server.py" --check')
    loop = text.index("while :; do")
    read = text.index('read -r -p "Type 1 or 2: " pick || pick=1')
    large = text.index("1) MODEL=large-v3; break;;")
    small = text.index("2) MODEL=small; break;;")
    done_loop = text.index("done", small)
    download = text.index('if ! "${VENV}/bin/python" "${HERE}/server.py" --download-model "$MODEL"; then')
    failed = text.index('echo "The model could not be downloaded. Check the connection and run setup.sh again,"\n'
                        '  echo "or start ./run.sh: the server then downloads $MODEL itself, without a progress bar."')
    exit_line = text.index("exit 1", failed)
    complete = text.index('echo "Setup is complete: the $MODEL model is downloaded and everything is ready."')
    last = text.index('echo "Close this window and start ./run.sh."')
    assert check < loop < read < large < small < done_loop < download < failed < exit_line < complete < last
    assert text.rstrip("\n").endswith('echo "Close this window and start ./run.sh."')
    assert "downloaded on the first start" not in text
    assert "1  large-v3" in text and "2  small" in text
    assert "also downloads it on its first start" not in text and not any("also downloads it" in l for l in cmd_lines())


def test_setup_scripts_offer_the_same_two_models():
    cmd = "\n".join(cmd_lines())
    sh = sh_text()
    for text in (cmd, sh):
        assert "large-v3  best quality, about 3 GB, wants a GPU with 4 GB or more free" in text
        assert "small     about 500 MB, fine on a CPU, less accurate" in text
        assert "Which Whisper model should the server use? (the popup can switch later)" in text
    assert server.MODEL_SIZES["large-v3"] == "about 3 GB" and server.MODEL_SIZES["small"] == "about 500 MB"


# --- server/tools/retranscribe.py: its --model default is the server's ---------------------------

def load_retranscribe():
    """The tool as a module. Its load_server() finds shisuko_server in sys.modules, so it drives
    this test's server module and sees the patched CONFIG_PATH; the HF_HUB_OFFLINE=1 its import
    sets for the process is put back, so the other tests keep the environment they had."""
    name = "shisuko_retranscribe"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    offline = os.environ.get("HF_HUB_OFFLINE")
    spec = importlib.util.spec_from_file_location(name, SERVER_DIR / "tools" / "retranscribe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = offline
    return module


def test_retranscribe_runs_the_model_chosen_at_setup(tmp_path):
    """Without --model the tool measures the model the server runs: config.json's, not a hard-coded large-v3."""
    tool = load_retranscribe()
    assert tool.server is server, "the tool must drive the same server module"
    server.write_config({"model": "small"})
    assert tool.parse_args(["abc123def45", "--out", str(tmp_path / "out")]).model == "small"


def test_retranscribe_without_a_config_takes_large_v3_and_the_flag_wins(tmp_path):
    tool = load_retranscribe()
    assert tool.parse_args(["abc123def45", "--out", str(tmp_path / "out")]).model == server.DEFAULT_MODEL == "large-v3"
    server.write_config({"model": "small"})
    assert tool.parse_args(["abc123def45", "--out", str(tmp_path / "out"), "--model", "x"]).model == "x"


def test_retranscribe_help_names_the_setup_default(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "200")
    tool = load_retranscribe()
    with pytest.raises(SystemExit):
        tool.parse_args(["--help"])
    assert "the model chosen at setup (config.json), else large-v3" in capsys.readouterr().out
