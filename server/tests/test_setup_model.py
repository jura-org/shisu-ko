"""Setup asks for the Whisper model: `server.py --download-model NAME` fetches it with progress bars
and writes ~/.shisu-ko/config.json, which parse_args() reads as the --model default from then on.

faster_whisper and huggingface_hub are fakes in sys.modules (a snapshot_download() that records
its keyword arguments and writes model.bin into the directory it returns), so nothing here
downloads or loads a model, and run_check() sees stand-ins for ctranslate2, yt-dlp and the
registry as well, so it never initialises a GPU driver. The launchers are checked as text, like
run.cmd / run.sh in test_update_endpoint.py.
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


class DisabledTqdm:
    """Stands for faster_whisper.utils.disabled_tqdm, the class the setup download must not pass on."""


def fake_hub(monkeypatch, tmp_path, fail=None, bare=False, table=None, block=None):
    """faster_whisper (its size table and disabled tqdm) and huggingface_hub with a recording snapshot_download().

    The download comes back with a directory under tmp_path holding model.bin (unless `bare`),
    or raises `fail`; with `block`, a pair of Events, it sets the first and waits for the second
    first, a download that is still streaming. Returns the list of (repo_id, kwargs) it was
    called with.
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
            (directory / "model.bin").write_bytes(b"\0")
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


def test_the_initial_prompt_defaults_to_the_language_one(tmp_path):
    # Whisper decodes each window with nothing in front of it, so the prompt is what asks it to
    # punctuate. Only Japanese has one today; another language gets none, and so does "".
    assert server.parse_args([]).initial_prompt == server.DEFAULT_PROMPTS["ja"]
    assert server.parse_args(["--language", "en"]).initial_prompt == ""
    assert server.parse_args(["--initial-prompt", ""]).initial_prompt == ""
    assert server.parse_args(["--initial-prompt", "あ。"]).initial_prompt == "あ。"


def test_the_window_default_is_one_whisper_chunk(tmp_path):
    # faster-whisper drops the initial prompt after the first 30 s chunk of a call, so a longer
    # window would decode its tail unprompted.
    assert server.parse_args([]).window == 30.0
    assert server.parse_args([]).first_window == 20.0


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


# --- run_check() --------------------------------------------------------------------------------

def no_registry(*args):
    raise OSError("no such key")


def test_run_check_names_the_default_model(monkeypatch, tmp_path, capsys):
    # run_check() imports ctranslate2 (which would initialise the CUDA driver where the library is
    # installed: the venv, nix run .#tests), faster_whisper and yt_dlp, and loads native_host.py to
    # look the launcher up in the registry and in the browsers' folders (under the home, and on
    # Linux under CHROME_CONFIG_HOME or XDG_CONFIG_HOME): every one of them is a stand-in here,
    # and the output shows that they were what the check saw.
    fake_hub(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(__version__="0", get_cuda_device_count=lambda: 0))
    version = SimpleNamespace(__version__="0")
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(version=version))
    monkeypatch.setitem(sys.modules, "yt_dlp.version", version)
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(HKEY_CURRENT_USER=0, REG_SZ=1, OpenKey=no_registry))
    for name in ("SHISUKO_HOME", "USERPROFILE", "HOME", "CHROME_CONFIG_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.delenv("SHISUKO_CONTAINER", raising=False)
    server.run_check()
    out = capsys.readouterr().out
    assert "CTranslate2 0: 0 CUDA device(s)" in out and "faster-whisper 0" in out and "yt-dlp 0" in out
    assert "Start button launcher: not registered" in out
    assert "Default model: large-v3 (built-in default)" in out
    launcher = "run.cmd" if os.name == "nt" else "run.sh"
    if not sys.prefix.startswith("/nix/store/"):  # nix run .#tests names its own command, below
        assert f"YouTube sign-in: none (if YouTube asks for one: {launcher} --save-cookies-from-browser firefox)" in out
    # `nix run .#check` is this check with the Nix store's Python, which has no venv for run.sh to start.
    monkeypatch.setattr(sys, "prefix", "/nix/store/0000-python3-3.12-env")
    server.run_check()
    assert "YouTube sign-in: none (if YouTube asks for one: nix run . -- --save-cookies-from-browser firefox)" in capsys.readouterr().out
    monkeypatch.setenv("SHISUKO_CONTAINER", "1")
    server.run_check()
    assert "YouTube sign-in: none (if YouTube asks for one: a cookies.txt in the data folder and --cookies /data/cookies.txt)" in capsys.readouterr().out
    monkeypatch.delenv("SHISUKO_CONTAINER")
    server.write_config({"model": "small", "cookies_from_browser": "firefox"})
    server.run_check()
    out = capsys.readouterr().out
    assert "Default model: small (chosen at setup)" in out
    assert "YouTube sign-in: firefox's cookies go with every download (chosen at setup)" in out


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
    choice = index_of(lines, lambda l: l == 'choice /c 1234 /n /m "Type 1, 2, 3 or 4: "', "choice")
    pick = index_of(lines, lambda l: l == ('if errorlevel 5 (set "MODEL=large-v3") else if errorlevel 4 (set "MODEL=kitsune-0.1b") '
                                           'else if errorlevel 3 (set "MODEL=kitsune-0.6b") else if errorlevel 2 (set "MODEL=small") '
                                           'else (set "MODEL=large-v3")'), "the pick")
    kitsune = index_of(lines, lambda l: l == '  "%VENV%\\Scripts\\python.exe" "%~dp0kitsune_setup.py"', "kitsune_setup.py")
    download = index_of(lines, lambda l: l == '"%VENV%\\Scripts\\python.exe" "%~dp0server.py" --download-model %MODEL%', "download")
    done = index_of(lines, lambda l: l == "echo Close this window and start run.cmd.", "the last line")
    cookies = index_of(lines, lambda l: l == '"%VENV%\\Scripts\\python.exe" "%~dp0server.py" --setup-cookies', "--setup-cookies")
    amd = index_of(lines, lambda l: "amd_setup.py" in l and not l.startswith("REM"), "amd_setup.py")
    assert check < choice < pick < kitsune < cookies < download < amd < done
    # choice's errorlevel is the key's number, or 255 when it cannot read one (stdin closed or
    # empty), and "if errorlevel N" means N or more: 5 is tested first, so that 255 takes large-v3
    # like setup.sh's EOF fallback, then 4, 3 and 2; the pick is the first thing after choice that
    # looks at errorlevel (a set inside an if-block resets it to 0).
    # PyTorch is installed only for a Kitsune model, before its download, and a failure ends setup.
    assert lines[kitsune - 2] == 'if not "%MODEL:kitsune-=%"=="%MODEL%" ('
    assert lines[kitsune + 1:kitsune + 8] == [
        "  if errorlevel 1 (",
        "    echo PyTorch could not be installed for the Kitsune model. Check the connection and run",
        "    echo setup.cmd again, or pick a Whisper model.",
        "    pause",
        "    exit /b 1",
        "  )",
        ")",
    ]
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
    # The AMD engine's offer runs once the model is there, with the venv's Python and nothing on
    # its command line, on a line of its own after the download's failure block has closed: only
    # comments and blank echoes lie between them, so no if-block holds it. Nothing after it reads
    # its errorlevel or leaves early, so neither a no nor a failure there can end the setup short
    # of "Setup is complete".
    assert lines[amd] == '"%VENV%\\Scripts\\python.exe" "%~dp0amd_setup.py"'
    closed = failed + 5
    assert lines[closed] == ")" and closed < amd
    assert all(l.startswith("REM ") or l == "echo." for l in lines[closed + 1:amd])
    assert lines[amd + 1:done - 1] == ["echo."]
    after = [l.lower() for l in lines[amd + 1:]]
    assert not any("errorlevel" in l or "exit" in l or "goto" in l for l in after)


def test_setup_sh_asks_then_downloads_then_says_it_is_done():
    text = sh_text()
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    check = text.index('"${HERE}/server.py" --check')
    loop = text.index("while :; do")
    read = text.index('read -r -p "Type 1, 2, 3 or 4: " pick || pick=1')
    large = text.index("1) MODEL=large-v3; break;;")
    small = text.index("2) MODEL=small; break;;")
    kit6 = text.index("3) MODEL=kitsune-0.6b; break;;")
    kit1 = text.index("4) MODEL=kitsune-0.1b; break;;")
    done_loop = text.index("done", kit1)
    # PyTorch only for a Kitsune model, before its download; a failure ends setup.
    kitsune = text.index('  kitsune-*)\n    echo\n    if ! "${VENV}/bin/python" "${HERE}/kitsune_setup.py"; then\n'
                         '      echo "PyTorch could not be installed for the Kitsune model. Check the connection and run"\n'
                         '      echo "setup.sh again, or pick a Whisper model."\n      exit 1\n    fi;;\nesac\n')
    # Its own line, and never the end of setup under set -e: the model download still follows.
    cookies = text.index('\n"${VENV}/bin/python" "${HERE}/server.py" --setup-cookies || true\n')
    download = text.index('if ! "${VENV}/bin/python" "${HERE}/server.py" --download-model "$MODEL"; then')
    failed = text.index('echo "The model could not be downloaded. Check the connection and run setup.sh again,"\n'
                        '  echo "or start ./run.sh: the server then downloads $MODEL itself, without a progress bar."')
    exit_line = text.index("exit 1", failed)
    closed = text.index("\nfi\n", exit_line)
    # The AMD engine's offer: its own line, after the download's if-block has closed, and `|| true`
    # so that under set -e neither a failure nor a crash in it ends the setup before its last lines.
    amd_call = '\n"${VENV}/bin/python" "${HERE}/amd_setup.py" || true\n'
    amd = text.index(amd_call)
    calls = [l for l in text.splitlines() if "amd_setup.py" in l and not l.startswith("#")]
    assert calls == [amd_call.strip("\n")], "amd_setup.py is called once, with the venv's Python and no arguments"
    complete = text.index('echo "Setup is complete: the $MODEL model is downloaded and everything is ready."')
    last = text.index('echo "Close this window and start ./run.sh."')
    assert check < loop < read < large < small < kit6 < kit1 < done_loop < kitsune < cookies < download < failed \
        < exit_line < closed < amd < complete < last
    between = text[closed + len("\nfi\n"):amd].splitlines()
    assert all(l.startswith("#") or l == "echo" for l in between), "nothing but comments and a blank line before it"
    assert text[amd + len(amd_call):complete] == "echo\n"
    assert text.rstrip("\n").endswith('echo "Close this window and start ./run.sh."')
    assert "downloaded on the first start" not in text
    assert "1  large-v3" in text and "2  small" in text
    assert "also downloads it on its first start" not in text and not any("also downloads it" in l for l in cmd_lines())


def test_the_amd_helper_both_setups_call_is_there():
    # Neither setup looks at the call's verdict, so a missing file would only print Python's
    # "can't open file" and go on to "Setup is complete": its absence shows here instead.
    assert (SERVER_DIR / "amd_setup.py").is_file()
    assert '"%~dp0amd_setup.py"' in "\n".join(cmd_lines()) and '"${HERE}/amd_setup.py"' in sh_text()


def test_setup_scripts_offer_the_same_four_models():
    cmd = "\n".join(cmd_lines())
    sh = sh_text()
    for text in (cmd, sh):
        assert "1  large-v3      Whisper: best quality, about 3 GB, wants a GPU with 4 GB or more free" in text
        assert "2  small         Whisper: about 500 MB, fine on a CPU, less accurate" in text
        assert "3  kitsune-0.6b  Kitsune-Transcribe: Japanese only, about 1.2 GB, plus PyTorch (about 3 GB)" in text
        assert "4  kitsune-0.1b  Kitsune-Transcribe: Japanese only, about 200 MB, plus PyTorch, fine on a CPU" in text
        assert "Which model should the server use? (the popup can switch later)" in text
    assert server.MODEL_SIZES["large-v3"] == "about 3 GB" and server.MODEL_SIZES["small"] == "about 500 MB"
    assert server.kitsune_size("kitsune-0.6b") == "about 1.2 GB" and server.kitsune_size("kitsune-0.1b") == "about 210 MB"
    assert (SERVER_DIR / "kitsune_setup.py").is_file() and (SERVER_DIR / "requirements-kitsune.txt").is_file()


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
