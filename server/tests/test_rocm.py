"""The AMD engine (experimental): CTranslate2's ROCm build in ~/.shisu-ko/rocm, beside the venv.

server.py decides at import time whether it runs on it (rocm_engine()); here each piece of that
decision is fed tmp paths and dict environments, and what acts on it (the crash guard, the model
switch that restarts on Windows, the exits that end in TerminateProcess there, --probe-gpu and
--check's line) runs with ROCM_ACTIVE set by the test. Nothing touches the real ~/.shisu-ko,
loads a model, installs a Ctrl+C handler or reaches the real TerminateProcess: ctranslate2,
faster_whisper, ctypes and signal are stand-ins, and the fixture turns any call of the real
terminate_process() into a test failure.
The NVIDIA and CPU paths are checked to stay as they were wherever the engine is off.
"""
from __future__ import annotations

import ast
import gc
import importlib.util
import json
import logging
import os
import signal
import sys
import sysconfig
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from _serverlib import load_server

server = load_server()
SERVER_DIR = Path(__file__).resolve().parent.parent
REAL_TERMINATE = server.terminate_process
REAL_PLATFORM_KEY = server.platform_key
REAL_MISSING_LIBRARIES = server.missing_rocm_libraries
REAL_TRANSCRIBER = server.Transcriber
TAG = server.python_tag()
WINDOWS, LINUX = "win_amd64", "linux_x86_64"
MIB = 1024 * 1024
# A slice of faster_whisper.utils._MODELS, for canonical_model_name().
FAKE_MODELS = {"small": "Systran/faster-whisper-small", "large-v3": "Systran/faster-whisper-large-v3"}
# What ROCM_REASON says once the crash guard has given up on the engine.
GUARD_WORDS = "did not get a model onto the AMD GPU at its last start"


@pytest.fixture(autouse=True)
def not_an_apple_machine(monkeypatch):
    """Every machine in this file is an NVIDIA, AMD or CPU one, never a Mac.

    resolve_device("auto") asks mlx_available() first, so on an Apple Silicon machine with
    mlx-whisper installed --device auto answers "mlx", no WhisperModel is built and the engine
    these tests describe never runs. Pinning it keeps the file from passing or failing by where
    it runs.
    """
    monkeypatch.setattr(server, "mlx_available", lambda: False)


def forbidden_terminate(code):
    pytest.fail(f"the real TerminateProcess was reached (code {code})")


class Signals:
    """server.py's `signal`: signal.signal() records the handler instead of installing it in pytest's process.

    `installed` lists what SIGINT (Ctrl+C) was given, `breaks` what SIGBREAK (Ctrl+Break) was.
    Linux has no SIGBREAK; server.py asks for it only on Windows, and here it has Windows' number.
    """

    SIGINT = signal.SIGINT
    SIGBREAK = getattr(signal, "SIGBREAK", 21)
    default_int_handler = signal.default_int_handler

    def __init__(self):
        # As Python starts: its own handler raises KeyboardInterrupt on Ctrl+C, and Ctrl+Break is
        # left to the console, whose default handler ends the process through ExitProcess.
        self.handlers = {self.SIGINT: signal.default_int_handler, self.SIGBREAK: signal.SIG_DFL}
        self.installed = []
        self.breaks = []

    @property
    def handler(self):
        return self.handlers[self.SIGINT]

    def signal(self, signum, handler):
        assert signum in self.handlers, signum
        previous, self.handlers[signum] = self.handlers[signum], handler
        (self.installed if signum == self.SIGINT else self.breaks).append(handler)
        return previous

    def press(self, signum):
        """What the key does on the main thread: Python's own handler raises, another one is called."""
        handler = self.handlers[signum]
        if handler is signal.default_int_handler:
            raise KeyboardInterrupt
        if handler is signal.SIG_DFL:
            pytest.fail("the console's default handler ends the process through ExitProcess, with its DLL detach")
        handler(signum, None)

    def ctrl_c(self):
        self.press(self.SIGINT)

    def ctrl_break(self):
        self.press(self.SIGBREAK)


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    """Every path server.py reads or writes lives under tmp_path; the engine starts off, as the import left it.

    Whatever runs the tests, server.py sees a Windows x64 Python (platform_key()) that is not
    Nix's, outside the Docker image and outside run.cmd, and on Linux a ROCm whose libraries are
    all there; the tests that mean otherwise say so. A Ctrl+C handler main() installs goes to a
    Signals stand-in, never to pytest's own process, and the models kept for the AMD engine on
    Windows (KEPT_MODELS) start empty for each test.
    """
    monkeypatch.setenv("SHISUKO_HOME", str(tmp_path))  # for amd_setup.py and anything else that reads it at run time
    monkeypatch.setattr(server, "APP_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(server, "ROCM_DIR", tmp_path / "rocm")
    monkeypatch.setattr(server, "ROCM_STARTS_PATH", tmp_path / "rocm-starts")
    monkeypatch.setattr(server, "NEXT_MODEL_PATH", tmp_path / "next-model")
    monkeypatch.setattr(server, "DRM_ROOT", tmp_path / "drm")
    monkeypatch.setattr(server, "KFD_NODES_ROOT", tmp_path / "kfd")
    monkeypatch.setattr(server, "ROCM_ACTIVE", False)
    monkeypatch.setattr(server, "ROCM_REASON", "config.json does not turn it on")
    monkeypatch.setattr(server, "_MODEL_ALIASES", None)
    monkeypatch.setattr(server, "terminate_process", forbidden_terminate)
    monkeypatch.setattr(server, "platform_key", lambda name=None: WINDOWS)
    monkeypatch.setattr(server, "missing_rocm_libraries", lambda env: [])
    monkeypatch.setattr(server, "signal", Signals())
    monkeypatch.setattr(server, "KEPT_MODELS", [])
    for name in ("SHISUKO_CONTAINER", "SHISUKO_LAUNCHER", "SHISUKO_NO_UPDATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))  # rocm_engine() may put the side folder in front
    monkeypatch.setattr(sys, "base_prefix", "/usr")  # `nix run .#tests` too: the Nix check has tests of its own
    return tmp_path


class Exited(BaseException):
    """What the stand-ins of os._exit(), hard_exit() and os.execv() raise instead of ending pytest."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Crashed(BaseException):
    """A process dying where no `except Exception` sees it (a C++ terminate, a memory access fault)."""


class ModuleAs:
    """A module as server.py sees it on another platform: the given attributes, the rest the real module's."""

    def __init__(self, module, **overrides):
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, attr):
        return getattr(self._module, attr)


def os_as(name, calls=None):
    """server.py's `os` with os.name `name` and an os._exit() that records ("_exit", code) and raises Exited."""
    calls = [] if calls is None else calls

    def _exit(code):
        calls.append(("_exit", code))
        raise Exited(code)

    return ModuleAs(os, name=name, _exit=_exit)


class InertTranscriber:
    """App.__init__ starts the transcriber thread; here the test is the only caller of the switch."""

    def __init__(self, app):
        self.app = app

    def start(self):
        pass


def install_rocm(tmp_path, python=TAG, manifest=True, platform=WINDOWS):
    """What amd_setup.py leaves in ROCM_DIR: the ctranslate2 package and shisuko-rocm.json."""
    rocm = tmp_path / "rocm"
    (rocm / "ctranslate2").mkdir(parents=True, exist_ok=True)
    (rocm / "ctranslate2" / "__init__.py").write_text("", encoding="utf-8")
    if manifest:
        (rocm / "shisuko-rocm.json").write_text(json.dumps({
            "ctranslate2": "4.8.2", "rocm": "7.2.1" if platform == WINDOWS else "system", "python": python,
            "platform": platform, "installed": "2026-09-26T12:00:00+00:00"}), encoding="utf-8")
    return rocm


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def config(tmp_path):
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


def alias_table(monkeypatch, **extra):
    """faster_whisper with its size table, for canonical_model_name(), plus `extra` attributes."""
    utils = SimpleNamespace(_MODELS=dict(FAKE_MODELS))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(utils=utils, __version__="0", **extra))


def fake_whisper(monkeypatch, fail_on=(), fail_warmup=False, error=None, built=None):
    """faster_whisper.WhisperModel that records (name, kwargs), fails for a device in `fail_on` (with `error`,
    else a HIP error), and warms up (or fails to, with a HIP error or the exception `fail_warmup` is) at once.
    Each model it builds goes into `built` as a weakref. gpu_memory_mb() finds nothing, as on Windows.
    Returns the records."""
    made = []

    class WhisperModel:
        def __init__(self, name, **kwargs):
            made.append((name, kwargs))
            if kwargs["device"] in fail_on:
                raise error or RuntimeError("hipErrorNoBinaryForGpu: Unable to find code object for all current devices")
            if built is not None:
                built.append(weakref.ref(self))

        def transcribe(self, audio, **kwargs):
            if isinstance(fail_warmup, BaseException):
                raise fail_warmup
            if fail_warmup:
                raise RuntimeError("HIP error: an illegal memory access was encountered")
            return iter([]), None

    alias_table(monkeypatch, WhisperModel=WhisperModel)
    monkeypatch.setattr(server, "gpu_memory_mb", lambda: None)
    return made


def load_args(**overrides):
    base = dict(model="small", device="cuda", compute_type="auto", cpu_threads=0, language="ja")
    base.update(overrides)
    return SimpleNamespace(**base)


def app_args(**overrides):
    base = dict(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3", language="ja",
                idle_minutes=30, retry_after=30.0, device="auto")
    base.update(overrides)
    return SimpleNamespace(**base)


def engine_imports(monkeypatch, devices=1):
    """ctranslate2 as the AMD engine's build imports: with its compiled part (get_cuda_device_count)."""
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(__version__="4.8.2",
                                                                    get_cuda_device_count=lambda: devices))


def run_main(monkeypatch, *argv):
    """main() with `argv`; returns the code it ended on (SystemExit, or Exited from a stand-in exit), else None."""
    monkeypatch.setattr(sys, "argv", ["server.py", *argv])
    try:
        server.main()
    except (SystemExit, Exited) as exc:
        return exc.code
    return None


# --- python_tag(), read_json_object(), the crash guard's file -------------------------------------

def test_python_tag_names_the_interpreter_the_wheel_was_built_for():
    assert server.python_tag((3, 12, 6, "final", 0), False) == "cp312"
    assert server.python_tag((3, 9), False) == "cp39"
    assert server.python_tag((3, 14), True) == "cp314t", "a free-threaded build loads only the t wheel"
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    assert TAG == f"cp{sys.version_info[0]}{sys.version_info[1]}" + ("t" if free_threaded else "")


def test_read_json_object_never_raises(tmp_path):
    path = tmp_path / "x.json"
    assert server.read_json_object(path) == {}, "missing"
    path.write_text('{"engine": "rocm"', encoding="utf-8")
    assert server.read_json_object(path) == {}, "corrupt"
    path.write_text('["rocm"]', encoding="utf-8")
    assert server.read_json_object(path) == {}, "not an object"
    path.write_text("[" * 100000, encoding="utf-8")
    assert server.read_json_object(path) == {}, "nested deeper than the parser's recursion"
    path.write_bytes(b"\xef\xbb\xbf" + b'{"engine": "rocm"}')
    assert server.read_json_object(path) == {"engine": "rocm"}, "a BOM from Notepad or PowerShell"
    path.write_bytes(b"\xff\xfe\x00")
    assert server.read_json_object(path) == {}, "not UTF-8"
    assert server.read_json_object(tmp_path) == {}, "a folder"


@pytest.mark.parametrize("text, count", [(None, 0), ("", 0), ("garbage", 0), ("-3", 0), ("2\n", 2), (" 1 ", 1)])
def test_read_rocm_starts_counts_missing_or_garbled_as_zero(tmp_path, text, count):
    path = tmp_path / "rocm-starts"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    assert server.read_rocm_starts(path) == count


def test_the_guard_allows_two_tries_and_no_third():
    assert server.ROCM_GUARD_LIMIT == 2
    assert [server.rocm_guard_allows(n) for n in range(5)] == [True, True, False, False, False]


def load_amd_setup(monkeypatch):
    """server/amd_setup.py, registered in sys.modules while it runs (its dataclasses look themselves up there)."""
    name = "shisuko_amd_setup_for_test_rocm"
    spec = importlib.util.spec_from_file_location(name, SERVER_DIR / "amd_setup.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_platform_key_names_the_platforms_the_rocm_wheels_exist_for():
    assert REAL_PLATFORM_KEY("win-amd64") == WINDOWS
    assert REAL_PLATFORM_KEY("linux-x86_64") == LINUX
    for other in ("win32", "win-arm64", "linux-aarch64", "linux-i686", "macosx-14.0-arm64"):
        assert REAL_PLATFORM_KEY(other) is None, other
    assert REAL_PLATFORM_KEY() == REAL_PLATFORM_KEY(sysconfig.get_platform()), "this Python's, by default"


def test_the_platforms_and_the_linux_libraries_are_amd_setup_s(monkeypatch):
    amd_setup = load_amd_setup(monkeypatch)
    for name in ("win-amd64", "linux-x86_64", "win32", "linux-aarch64", "macosx-14.0-arm64"):
        assert REAL_PLATFORM_KEY(name) == amd_setup.platform_key(name), name
    assert server.ROCM_LINUX_LIBRARIES == amd_setup.LINUX_LIBRARIES


# --- rocm_engine_state(): when the engine is on ---------------------------------------------------

def state(tmp_path, config=None, env=None, tag=TAG, guard=0, platform=WINDOWS, prefix="/usr"):
    return server.rocm_engine_state(config or {}, env or {}, tmp_path / "rocm", tag, guard, platform, prefix)


@pytest.mark.parametrize("value", [None, "", "cuda", "default", "ROCM", True, ["rocm"]])
def test_without_rocm_in_config_or_env_the_engine_is_off(tmp_path, value):
    install_rocm(tmp_path)
    active, reason = state(tmp_path, config={} if value is None else {"engine": value})
    assert active is False
    assert "amd_setup.py --probe" in reason, "the reason says how to turn it on"


def test_the_environment_decides_over_the_config(tmp_path):
    install_rocm(tmp_path)
    active, reason = state(tmp_path, config={"engine": "rocm"}, env={"SHISUKO_ENGINE": "default"})
    assert (active, reason) == (False, "SHISUKO_ENGINE=default asks for the default engine")
    active, reason = state(tmp_path, env={"SHISUKO_ENGINE": " ROCm "})
    assert active is True and "SHISUKO_ENGINE=rocm" in reason, "the probe's way in, before config.json says anything"
    active, reason = state(tmp_path, config={"engine": "rocm"}, env={"SHISUKO_ENGINE": ""})
    assert active is True and "config.json" in reason and str(tmp_path / "rocm") in reason


def test_the_engine_needs_the_package_in_the_side_folder(tmp_path):
    active, reason = state(tmp_path, config={"engine": "rocm"})
    assert active is False and "holds no CTranslate2" in reason
    (tmp_path / "rocm" / "ctranslate2").mkdir(parents=True)
    write_json(tmp_path / "rocm" / "shisuko-rocm.json", {"python": TAG})
    assert state(tmp_path, config={"engine": "rocm"})[0] is False, "a folder without __init__.py is no package"


def test_the_engine_needs_a_wheel_for_this_python(tmp_path):
    install_rocm(tmp_path, python="cp311")
    active, reason = state(tmp_path, config={"engine": "rocm"}, tag="cp312")
    assert active is False and "cp311" in reason and "cp312" in reason
    assert state(tmp_path, config={"engine": "rocm"}, tag="cp311")[0] is True
    assert state(tmp_path, config={"engine": "rocm"}, tag="cp311t")[0] is False, "the free-threaded build is another ABI"


def test_a_side_folder_without_its_manifest_is_not_used(tmp_path):
    install_rocm(tmp_path, manifest=False)
    active, reason = state(tmp_path, config={"engine": "rocm"})
    assert active is False and "an unknown Python" in reason


def test_the_engine_needs_a_build_for_this_platform(tmp_path):
    # A data folder shared between Windows and a Linux container, or between two systems: the
    # Python tag alone (cp312 on both) would take the other one's build.
    install_rocm(tmp_path, platform=LINUX)
    active, reason = state(tmp_path, config={"engine": "rocm"}, platform=WINDOWS)
    assert active is False and LINUX in reason and WINDOWS in reason and "amd_setup.py" in reason
    assert state(tmp_path, config={"engine": "rocm"}, platform=LINUX)[0] is True
    install_rocm(tmp_path, platform=WINDOWS)
    assert state(tmp_path, config={"engine": "rocm"}, platform=LINUX)[0] is False
    assert state(tmp_path, config={"engine": "rocm"}, platform=WINDOWS)[0] is True
    active, reason = state(tmp_path, config={"engine": "rocm"}, platform=None)
    assert active is False and "a platform without an AMD build" in reason, "macOS, ARM: no build at all"


def test_a_manifest_that_names_no_platform_is_not_used(tmp_path):
    install_rocm(tmp_path)
    write_json(tmp_path / "rocm" / "shisuko-rocm.json", {"ctranslate2": "4.8.2", "python": TAG})
    active, reason = state(tmp_path, config={"engine": "rocm"})
    assert active is False and "an unknown platform" in reason
    active, reason = state(tmp_path, config={"engine": "rocm"}, platform=None)
    assert active is False and "a platform without an AMD build" in reason, "None must not match a missing platform"


@pytest.mark.parametrize("env", [{"SHISUKO_CONTAINER": "1"}, {"SHISUKO_CONTAINER": "1", "SHISUKO_ENGINE": "rocm"}])
def test_the_docker_image_never_takes_the_native_setup_s_engine(tmp_path, env):
    # DATA_DIR shares ~/.shisu-ko, side folder, config.json and crash guard included, with the image.
    install_rocm(tmp_path)
    active, reason = state(tmp_path, config={"engine": "rocm"}, env=env)
    assert active is False and "Docker image" in reason
    assert state(tmp_path, config={"engine": "rocm"}, env={**env, "SHISUKO_CONTAINER": ""})[0] is True


@pytest.mark.parametrize("env", [{}, {"SHISUKO_ENGINE": "rocm"}])
def test_nix_s_python_never_takes_the_native_setup_s_engine(tmp_path, env):
    # `nix run .` shares ~/.shisu-ko with a native setup.sh; the side folder's manylinux build needs
    # the system's libraries, which Nix's loader does not search, and a failed start there would
    # use up the native server's crash guard.
    install_rocm(tmp_path, platform=LINUX)
    active, reason = state(tmp_path, config={"engine": "rocm"}, env=env, platform=LINUX,
                           prefix="/nix/store/abc-python3-3.13.0")
    assert active is False and "Nix" in reason
    assert state(tmp_path, config={"engine": "rocm"}, env=env, platform=LINUX, prefix="/usr")[0] is True
    assert server.rocm_engine_state({"engine": "rocm"}, env, tmp_path / "rocm", TAG, 0, LINUX)[0] is True, \
        "without a prefix (amd_setup.py's agreement test) nothing changes"


def test_the_crash_guard_turns_the_engine_off_after_two_starts(tmp_path):
    install_rocm(tmp_path)
    assert state(tmp_path, config={"engine": "rocm"}, guard=1)[0] is True
    active, reason = state(tmp_path, config={"engine": "rocm"}, guard=2)
    assert active is False
    assert GUARD_WORDS in reason and "amd_setup.py --probe" in reason
    assert state(tmp_path, config={"engine": "rocm"}, guard=3)[1] == reason, \
        "no count in it: check_rocm_import() sets the guard to its limit after a single start"


# --- rocm_library_path(): Linux's LD_LIBRARY_PATH -------------------------------------------------

def test_the_library_path_puts_rocm_first_and_keeps_the_rest():
    assert server.rocm_library_path({}) == "/opt/rocm/lib:/opt/rocm/lib/llvm/lib"
    assert server.rocm_library_path({"LD_LIBRARY_PATH": ""}) == "/opt/rocm/lib:/opt/rocm/lib/llvm/lib"
    assert (server.rocm_library_path({"ROCM_PATH": "/opt/rocm-7.2.1/", "LD_LIBRARY_PATH": "/usr/local/lib"})
            == "/opt/rocm-7.2.1/lib:/opt/rocm-7.2.1/lib/llvm/lib:/usr/local/lib")


def test_the_library_path_is_left_alone_when_rocm_is_on_it():
    assert server.rocm_library_path({"LD_LIBRARY_PATH": "/usr/local/lib:/opt/rocm/lib"}) is None
    assert server.rocm_library_path({"LD_LIBRARY_PATH": "/opt/rocm/lib/"}) is None
    assert server.rocm_library_path({"ROCM_PATH": "/opt/rocm-7.2.1", "LD_LIBRARY_PATH": "/opt/rocm-7.2.1/lib"}) is None
    # Another folder is not this one: lib64, or the lib of another ROCm.
    assert server.rocm_library_path({"LD_LIBRARY_PATH": "/opt/rocm/lib64"}) is not None
    assert server.rocm_library_path({"ROCM_PATH": "/opt/rocm-7.2.1", "LD_LIBRARY_PATH": "/opt/rocm/lib"}).startswith("/opt/rocm-7.2.1/lib:")


# --- rocm_engine(): what the import does -----------------------------------------------------------

def no_execv(monkeypatch):
    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("the server started itself again"))


def turned_on(tmp_path, platform=WINDOWS):
    install_rocm(tmp_path, platform=platform)
    write_json(tmp_path / "config.json", {"engine": "rocm"})


def turned_on_linux(monkeypatch, tmp_path):
    """turned_on() for a Linux x64 Python: its build in the side folder, and platform_key() says linux_x86_64."""
    monkeypatch.setattr(server, "platform_key", lambda name=None: LINUX)
    turned_on(tmp_path, platform=LINUX)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_with_the_engine_off_the_import_changes_nothing(monkeypatch, tmp_path, platform):
    no_execv(monkeypatch)
    before = list(sys.path)
    environ = {"PATH": "/usr/bin"}
    active, _ = server.rocm_engine(["server.py"], environ, script=True, platform=platform)
    assert active is False
    assert sys.path == before and environ == {"PATH": "/usr/bin"}
    assert list(tmp_path.iterdir()) == [], "nothing is written"
    install_rocm(tmp_path)  # installed, but no passed test turned it on
    assert server.rocm_engine(["server.py"], environ, script=True, platform=platform)[0] is False
    assert sys.path == before and environ == {"PATH": "/usr/bin"}


def test_turned_on_the_side_folder_goes_first_with_the_cub_allocator(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on(tmp_path)
    environ = {}
    active, reason = server.rocm_engine(["server.py"], environ, script=True, platform="win32")
    assert active is True and "config.json" in reason
    assert sys.path[0] == str(tmp_path / "rocm")
    assert environ == {"CT2_CUDA_ALLOCATOR": "cub_caching"}
    environ = {"CT2_CUDA_ALLOCATOR": "cuda_malloc_async"}
    assert server.rocm_engine(["server.py"], environ, script=True, platform="win32")[0] is True
    assert environ == {"CT2_CUDA_ALLOCATOR": "cuda_malloc_async"}, "an operator's own choice stands"


def test_the_crash_guard_holds_the_server_but_not_the_probe(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on(tmp_path)
    (tmp_path / "rocm-starts").write_text("2\n", encoding="utf-8")
    before = list(sys.path)
    active, reason = server.rocm_engine(["server.py"], {}, script=True, platform="win32")
    assert active is False and GUARD_WORDS in reason and sys.path == before
    assert server.rocm_engine(["server.py", "--probe-gpu"], {}, script=True, platform="win32")[0] is True
    assert server.rocm_engine(["server.py", "--check"], {}, script=True, platform="win32")[0] is False


def test_a_folder_that_cannot_be_read_leaves_the_engine_off(monkeypatch, tmp_path):
    def unreadable(*args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(server, "rocm_engine_state", unreadable)
    active, reason = server.rocm_engine(["server.py"], {}, script=True, platform="linux")
    assert active is False and "could not be checked" in reason


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_in_the_docker_image_the_import_changes_nothing(monkeypatch, tmp_path, platform):
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)  # the image's own platform and Python, too: a Linux host's folder
    before = list(sys.path)
    environ = {"SHISUKO_CONTAINER": "1", "SHISUKO_ENGINE": "rocm", "LD_LIBRARY_PATH": "/usr/local/lib"}
    active, reason = server.rocm_engine(["server.py"], environ, script=True, platform=platform)
    assert active is False and "Docker image" in reason
    assert sys.path == before
    assert environ == {"SHISUKO_CONTAINER": "1", "SHISUKO_ENGINE": "rocm", "LD_LIBRARY_PATH": "/usr/local/lib"}, \
        "no restart, no allocator"


def test_under_nix_s_python_the_import_changes_nothing(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "base_prefix", "/nix/store/abc-python3-3.13.0")  # a venv built from it too
    before = list(sys.path)
    environ = {"LD_LIBRARY_PATH": "/usr/local/lib"}
    active, reason = server.rocm_engine(["server.py"], environ, script=True, platform="linux")
    assert active is False and "Nix" in reason
    assert environ == {"LD_LIBRARY_PATH": "/usr/local/lib"} and sys.path == before, "no restart, no allocator"
    assert not (tmp_path / "rocm-starts").exists()


@pytest.mark.parametrize("platform, words", [("win32", "Windows"), ("linux", "SHISUKO_ENGINE=rocm")])
def test_a_program_that_only_imports_server_py_keeps_the_default_engine(monkeypatch, tmp_path, platform, words):
    # server/tools/retranscribe.py and the like: they end through the interpreter's own exit, which
    # hangs with the ROCm runtime on Windows, and load_model() would clear the server's crash guard.
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    before = list(sys.path)
    environ = {}
    active, reason = server.rocm_engine(["retranscribe.py"], environ, script=False, platform=platform)
    assert active is False and words in reason
    assert environ == {} and sys.path == before


def test_a_program_that_asks_for_the_amd_engine_never_gets_it_on_windows(monkeypatch, tmp_path):
    # Once it has loaded a model, freeing it and the interpreter's exit hang every time with the
    # ROCm runtime there, and a program that only imports server.py never reaches main()'s finish().
    no_execv(monkeypatch)
    turned_on(tmp_path)
    before = list(sys.path)
    environ = {"SHISUKO_ENGINE": "rocm"}
    active, reason = server.rocm_engine(["retranscribe.py"], environ, script=False, platform="win32")
    assert active is False and "Windows" in reason
    assert environ == {"SHISUKO_ENGINE": "rocm"} and sys.path == before, "no allocator, no side folder"


def test_a_program_that_asks_for_the_amd_engine_gets_it_on_linux(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    environ = {"SHISUKO_ENGINE": "rocm", "LD_LIBRARY_PATH": "/opt/rocm/lib:/opt/rocm/lib/llvm/lib"}
    assert server.rocm_engine(["retranscribe.py"], environ, script=False, platform="linux")[0] is True
    assert sys.path[0] == str(tmp_path / "rocm") and environ["CT2_CUDA_ALLOCATOR"] == "cub_caching"


def test_missing_rocm_libraries_looks_in_rocm_path_s_lib():
    present = {"/opt/rocm/lib/libamdhip64.so.7", "/opt/rocm/lib/libhipblas.so.3"}
    assert REAL_MISSING_LIBRARIES({}, exists=present.__contains__) == ["libhiprand.so.1"]
    assert REAL_MISSING_LIBRARIES({"ROCM_PATH": "/opt/rocm-7.2.1"}, exists=present.__contains__) == \
        list(server.ROCM_LINUX_LIBRARIES)
    assert REAL_MISSING_LIBRARIES({"ROCM_PATH": "/opt/rocm-7.2.1/"},
                                  exists=lambda path: path.startswith("/opt/rocm-7.2.1/lib/lib")) == []


def test_linux_keeps_the_default_engine_while_the_rocm_libraries_are_missing(monkeypatch, tmp_path):
    # ROCm removed or upgraded past 7.2, or a ROCM_PATH only the setup's terminal had: the import
    # would fail every start on 2, which the launcher never retries. The engine comes back by
    # itself with the libraries, without a probe and without the crash guard counting.
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "missing_rocm_libraries", REAL_MISSING_LIBRARIES)
    root = tmp_path / "rocm-7.2.1"
    environ = {"ROCM_PATH": str(root), "LD_LIBRARY_PATH": "/usr/local/lib"}
    before = list(sys.path)
    active, reason = server.rocm_engine(["server.py"], environ, script=True, platform="linux")
    assert active is False
    assert "libamdhip64.so.7, libhipblas.so.3, libhiprand.so.1" in reason
    assert f"{root}/lib" in reason and "ROCM_PATH" in reason and "amd_setup.py --remove" in reason
    assert environ == {"ROCM_PATH": str(root), "LD_LIBRARY_PATH": "/usr/local/lib"} and sys.path == before
    assert not (tmp_path / "rocm-starts").exists()
    (root / "lib").mkdir(parents=True)
    for name in server.ROCM_LINUX_LIBRARIES:
        (root / "lib" / name).write_text("", encoding="utf-8")
    environ["SHISUKO_ROCM_REEXEC"] = "1"  # as the start again with them on LD_LIBRARY_PATH
    assert server.rocm_engine(["server.py"], environ, script=True, platform="linux")[0] is True


def test_windows_never_looks_for_the_linux_libraries(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on(tmp_path)
    monkeypatch.setattr(server, "missing_rocm_libraries", lambda env: pytest.fail("the Linux libraries were looked for"))
    assert server.rocm_engine(["server.py"], {}, script=True, platform="win32")[0] is True


def test_linux_starts_the_server_once_more_with_the_rocm_libraries(monkeypatch, tmp_path):
    turned_on_linux(monkeypatch, tmp_path)
    calls = []

    def execv(path, argv):
        calls.append((path, list(argv), dict(environ)))
        raise Exited(0)  # the real one never returns

    monkeypatch.setattr(os, "execv", execv)
    environ = {"ROCM_PATH": "/opt/rocm-7.2.1", "LD_LIBRARY_PATH": "/usr/local/lib"}
    before = list(sys.path)
    with pytest.raises(Exited):
        server.rocm_engine(["server.py", "--port", "8791"], environ, script=True, platform="linux")
    (path, argv, env), = calls
    assert (path, argv) == (sys.executable, [sys.executable, "server.py", "--port", "8791"])
    assert env["LD_LIBRARY_PATH"] == "/opt/rocm-7.2.1/lib:/opt/rocm-7.2.1/lib/llvm/lib:/usr/local/lib"
    assert env["SHISUKO_ROCM_REEXEC"] == "1"
    assert sys.path == before
    # The new process: the guard variable says it is the second start, and nothing starts a third.
    active, _ = server.rocm_engine(["server.py", "--port", "8791"], env, script=True, platform="linux")
    assert active is True and len(calls) == 1
    assert sys.path[0] == str(tmp_path / "rocm") and env["CT2_CUDA_ALLOCATOR"] == "cub_caching"


def test_the_second_start_never_starts_a_third_even_without_the_library_path(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    environ = {"SHISUKO_ROCM_REEXEC": "1"}  # a wrapper dropped LD_LIBRARY_PATH across the exec
    active, _ = server.rocm_engine(["server.py"], environ, script=True, platform="linux")
    assert active is True
    assert sys.path[0] == str(tmp_path / "rocm") and environ["CT2_CUDA_ALLOCATOR"] == "cub_caching"


def test_linux_needs_no_second_start_when_the_libraries_are_on_the_path(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    environ = {"LD_LIBRARY_PATH": "/opt/rocm/lib:/opt/rocm/lib/llvm/lib"}
    assert server.rocm_engine(["server.py"], environ, script=True, platform="linux")[0] is True
    assert "SHISUKO_ROCM_REEXEC" not in environ


def test_linux_never_restarts_a_program_that_only_imports_server_py(monkeypatch, tmp_path):
    no_execv(monkeypatch)
    turned_on_linux(monkeypatch, tmp_path)
    environ = {"SHISUKO_ENGINE": "rocm"}  # even one that asks for the AMD engine
    before = list(sys.path)
    active, reason = server.rocm_engine(["cue_stats.py"], environ, script=False, platform="linux")
    assert active is False and "LD_LIBRARY_PATH" in reason
    assert environ == {"SHISUKO_ENGINE": "rocm"} and sys.path == before


def test_linux_keeps_the_default_engine_when_it_cannot_start_itself_again(monkeypatch, tmp_path):
    turned_on_linux(monkeypatch, tmp_path)

    def execv(path, argv):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(os, "execv", execv)
    environ = {"LD_LIBRARY_PATH": "/usr/local/lib"}
    before = list(sys.path)
    active, reason = server.rocm_engine(["server.py"], environ, script=True, platform="linux")
    assert active is False and "could not start itself again" in reason
    assert environ == {"LD_LIBRARY_PATH": "/usr/local/lib"}, "the environment is put back"
    assert sys.path == before


# --- the crash guard at work ------------------------------------------------------------------------

def test_the_guard_counts_each_start_and_forgets_them_together(tmp_path):
    starts = tmp_path / "rocm-starts"
    server.count_rocm_start()
    server.count_rocm_start()
    assert starts.read_text(encoding="utf-8") == "2\n"
    assert server.rocm_guard_allows(server.read_rocm_starts(starts)) is False
    server.clear_rocm_starts()
    assert not starts.exists()
    server.clear_rocm_starts()  # nothing to forget is no error
    starts.write_text("garbage", encoding="utf-8")
    server.count_rocm_start()
    assert starts.read_text(encoding="utf-8") == "1\n"


def test_a_guard_file_that_cannot_be_written_costs_a_warning_not_the_start(monkeypatch, tmp_path, caplog):
    (tmp_path / "blocked").write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "ROCM_STARTS_PATH", tmp_path / "blocked" / "rocm-starts")
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        server.count_rocm_start()
    assert "crash guard" in caplog.text


def test_a_model_warmed_up_on_the_amd_gpu_clears_the_guard(monkeypatch, tmp_path, caplog):
    made = fake_whisper(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    (tmp_path / "rocm-starts").write_text("1\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert server.load_model(load_args())[1:] == ("cuda", "float16")
    assert not (tmp_path / "rocm-starts").exists()
    assert "Loading Whisper model 'small' on the AMD GPU (ROCm) (float16)" in caplog.text
    assert [kwargs["device"] for _, kwargs in made] == ["cuda"], "still cuda to CTranslate2 and faster-whisper"


def test_a_failed_warm_up_or_the_cpu_fallback_leaves_the_guard_counting(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")
    fake_whisper(monkeypatch, fail_warmup=True)
    assert server.load_model(load_args())[1] == "cuda"
    assert starts.read_text(encoding="utf-8") == "1\n"
    made = fake_whisper(monkeypatch, fail_on=("cuda",))
    assert server.load_model(load_args())[1:] == ("cpu", "int8")
    assert [kwargs["device"] for _, kwargs in made] == ["cuda", "cpu"]
    assert starts.read_text(encoding="utf-8") == "1\n", "a model on the processor is no pass for the GPU"


def test_the_default_engine_leaves_the_guard_alone_and_logs_as_before(monkeypatch, tmp_path, caplog):
    fake_whisper(monkeypatch)
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        server.load_model(load_args())
    assert starts.read_text(encoding="utf-8") == "1\n"
    assert "Loading Whisper model 'small' on cuda (float16)" in caplog.text and "ROCm" not in caplog.text


def test_strict_loading_has_no_cpu_fallback_and_no_warm_up_forgiveness(monkeypatch, tmp_path):
    made = fake_whisper(monkeypatch, fail_on=("cuda",))
    with pytest.raises(RuntimeError, match="hipErrorNoBinaryForGpu"):
        server.load_model(load_args(), strict=True)
    assert [kwargs["device"] for _, kwargs in made] == ["cuda"]
    fake_whisper(monkeypatch, fail_warmup=True)
    with pytest.raises(RuntimeError, match="illegal memory access"):
        server.load_model(load_args(), strict=True)


def test_a_server_start_on_the_amd_engine_is_counted_before_its_model_loads(monkeypatch, tmp_path, caplog):
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    order = []
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: order.append("lock") or True)

    def load_model(args, *rest, **kwargs):
        order.append(("load", server.read_rocm_starts(tmp_path / "rocm-starts")))
        raise Crashed("Memory access fault by GPU node-1")  # the process dies; no except clause sees it

    monkeypatch.setattr(server, "load_model", load_model)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        with pytest.raises(Crashed):
            run_main(monkeypatch)
        with pytest.raises(Crashed):
            run_main(monkeypatch)
    assert order == ["lock", ("load", 1), "lock", ("load", 2)]
    assert f"GPU engine: AMD ROCm (CTranslate2 4.8.2 from {tmp_path / 'rocm'})" in caplog.text
    active, reason = server.rocm_engine(["server.py"], {}, script=True, platform="win32")
    assert active is False and GUARD_WORDS in reason, "the third start leaves it off"


class DiesOnLoad:
    """ctranslate2 whose compiled part kills the process as it loads (no except clause sees it)."""

    def __getattr__(self, attr):
        raise Crashed("Memory access fault while the HIP runtime loads")


def test_a_start_that_dies_in_the_engine_import_is_counted(monkeypatch, tmp_path):
    # The count comes before the import check: a process that dies inside the ROCm import would
    # otherwise never be counted, and the launcher would restart it into the same crash for ever.
    turned_on(tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", DiesOnLoad())
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    with pytest.raises(Crashed):
        run_main(monkeypatch)
    assert (tmp_path / "rocm-starts").read_text(encoding="utf-8") == "1\n"


def test_a_cpu_start_whose_model_fails_keeps_an_earlier_count(monkeypatch, tmp_path):
    # --device cpu counted nothing, so it has nothing to take back: a crash an earlier GPU start
    # left counted stays counted.
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    fake_whisper(monkeypatch, fail_on=("cpu",), error=ValueError("Invalid model size 'smal'"))
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")
    assert run_main(monkeypatch, "--device", "cpu", "--model", "smal") == 2
    assert starts.read_text(encoding="utf-8") == "1\n"


def raising_count():
    raise RuntimeError("hipErrorNoDevice: no ROCm-capable device is detected")


@pytest.mark.parametrize("devices", [0, raising_count])
@pytest.mark.parametrize("argv", [[], ["--device", "cuda"]])
def test_an_engine_that_sees_no_amd_gpu_hands_over_to_the_default_engine(monkeypatch, tmp_path, caplog, argv, devices):
    # The card removed or replaced by an NVIDIA one, a driver the ROCm runtime cannot use, a Linux
    # user outside the render group: --device auto would run the model on the processor through the
    # ROCm build, with an NVIDIA GPU beside it idle, and two such starts would read as two crashes.
    turned_on(tmp_path)
    count = devices if callable(devices) else (lambda: devices)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(__version__="4.8.2", get_cuda_device_count=count))
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch, *argv) == 3, "the launcher starts the server again, on the default engine"
    assert (tmp_path / "rocm-starts").read_text(encoding="utf-8") == "2\n"
    assert "sees no AMD GPU" in caplog.text and "amd_setup.py --probe" in caplog.text
    active, reason = server.rocm_engine(["server.py"], {}, script=True, platform="win32")
    assert active is False and GUARD_WORDS in reason and "no AMD GPU" in reason


def test_a_start_on_the_cpu_never_asks_for_the_amd_gpu(monkeypatch, tmp_path):
    turned_on(tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(
        __version__="4.8.2", get_cuda_device_count=lambda: pytest.fail("the ROCm runtime was started")))
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    started = []
    monkeypatch.setattr(server, "start_app", lambda args: started.append(args.device) or SimpleNamespace(exit_code=None))
    monkeypatch.setattr(server, "ThreadingHTTPServer", InstantServer)
    monkeypatch.setattr(server.Handler, "app", None)
    assert run_main(monkeypatch, "--device", "cpu") is None
    assert started == ["cpu"] and not (tmp_path / "rocm-starts").exists()


@pytest.mark.parametrize("launcher, words", [
    (None, "the server stops, and its next start uses the default engine"),
    ("1", "exiting so that the launcher starts the server again on the default engine")])
def test_the_hand_over_says_what_comes_next(monkeypatch, tmp_path, caplog, launcher, words):
    # A hand start (`python server.py`) is not started again by anything: it must not say it is.
    turned_on(tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    if launcher is not None:
        monkeypatch.setenv("SHISUKO_LAUNCHER", launcher)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch) == 3
    assert words in caplog.text and "starting again" not in caplog.text


def test_a_start_that_finds_the_port_taken_counts_nothing(monkeypatch, tmp_path, caplog):
    # A second run.cmd, or the Start button while a server runs: turned away before any model loads.
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: False)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch) == 2
        assert run_main(monkeypatch) == 2
    assert "Another server is already starting or running" in caplog.text
    assert not (tmp_path / "rocm-starts").exists(), "a start the lock turned away never counts toward the crash guard"


def test_a_start_on_the_cpu_counts_nothing(monkeypatch, tmp_path):
    # --device cpu, which the low-memory warning suggests, never reaches the GPU: and load_model()
    # clears the count only for a model on the GPU, so each such start would count for good.
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    counts = []

    def load_model(args, *rest, **kwargs):
        counts.append(server.read_rocm_starts(tmp_path / "rocm-starts"))
        raise Crashed()

    monkeypatch.setattr(server, "load_model", load_model)
    for _ in range(2):
        with pytest.raises(Crashed):
            run_main(monkeypatch, "--device", "cpu", "--model", "small")
    assert counts == [0, 0] and not (tmp_path / "rocm-starts").exists()


def test_a_model_that_loads_nowhere_takes_its_count_back(monkeypatch, tmp_path, caplog):
    # A typo in --model, or a model not downloaded while offline: the GPU and then the CPU raise,
    # and the start ends on 2, which is never retried. That is the model's fault, not the engine's.
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    made = fake_whisper(monkeypatch, fail_on=("cuda", "cpu"), error=ValueError("Invalid model size 'large-v3-trubo'"))
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")  # a crash before stays counted
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch, "--model", "large-v3-trubo") == 2
        assert run_main(monkeypatch, "--model", "large-v3-trubo") == 2
    assert [kwargs["device"] for _, kwargs in made] == ["cuda", "cpu", "cuda", "cpu"]
    assert "Invalid model size" in caplog.text
    assert starts.read_text(encoding="utf-8") == "1\n"
    starts.unlink()
    assert run_main(monkeypatch, "--model", "large-v3-trubo") == 2
    assert not starts.exists(), "taken back to nothing"


def test_a_load_that_ends_on_an_import_error_still_counts(monkeypatch, tmp_path):
    # "DLL load failed" from the side folder's build is the engine's failure, not the model's.
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)

    def load_model(args, *rest, **kwargs):
        raise ImportError("DLL load failed while importing _ext: The specified module could not be found.")

    monkeypatch.setattr(server, "load_model", load_model)
    assert run_main(monkeypatch) == 2
    assert (tmp_path / "rocm-starts").read_text(encoding="utf-8") == "1\n"


@pytest.mark.parametrize("platform, module", [("win32", None), ("linux", None), ("win32", SimpleNamespace())])
def test_an_engine_that_does_not_import_is_turned_off_and_the_server_restarts(monkeypatch, tmp_path, caplog,
                                                                             platform, module):
    # None in sys.modules: the import raises ImportError, as "libamdhip64.so.7: cannot open shared
    # object file" does. The empty module: a package whose compiled part is missing imports empty.
    turned_on(tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", module)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform=platform))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch) == 3, "the launcher starts the server again, never 2"
    assert (tmp_path / "rocm-starts").read_text(encoding="utf-8") == "2\n"
    assert "does not load" in caplog.text and "amd_setup.py --probe" in caplog.text
    assert ("ROCM_PATH" in caplog.text) is (platform == "linux")
    active, reason = server.rocm_engine(["server.py"], {}, script=True, platform="win32")
    assert active is False and GUARD_WORDS in reason, "the restart runs on the default engine"


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_a_cpu_start_whose_engine_does_not_import_hands_over(monkeypatch, tmp_path, caplog, platform):
    # --device cpu (the low-VRAM warning suggests it) counts nothing, so the guard write must set the
    # limit itself; and a load_model() ImportError would end the start on 2, which run.cmd never retries.
    turned_on(tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform=platform))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)

    def load_model(args, *rest, **kwargs):
        raise ImportError("DLL load failed while importing _ext: The specified module could not be found.")

    monkeypatch.setattr(server, "load_model", load_model)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch, "--device", "cpu") == 3
    assert "does not load" in caplog.text
    assert (tmp_path / "rocm-starts").read_text(encoding="utf-8") == "2\n"
    active, reason = server.rocm_engine(["server.py"], {}, script=True, platform="win32")
    assert active is False and GUARD_WORDS in reason, "the restart runs on the default engine"


def test_an_engine_that_does_not_import_and_a_guard_that_cannot_be_written_end_on_2(monkeypatch, tmp_path):
    # A restart would meet the same failure for ever: stop, as a failed load does.
    turned_on(tmp_path)
    (tmp_path / "blocked").write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "ROCM_STARTS_PATH", tmp_path / "blocked" / "rocm-starts")
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    assert run_main(monkeypatch) == 2


def failing_load(args, *rest, **kwargs):
    raise RuntimeError("CUDA failed with error out of memory")


def test_a_start_on_the_default_engine_counts_nothing(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", failing_load)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache", "models"], "no rocm-starts, no next-model"
    assert "GPU engine" not in caplog.text, "no side folder: not a word about the AMD engine"


def test_the_one_shot_commands_never_count_a_start(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: pytest.fail("the instance lock was taken"))
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    monkeypatch.setattr(server, "run_check", lambda *a: None)
    monkeypatch.setattr(server, "run_download_model", lambda name, *a: 0)
    monkeypatch.setattr(server, "run_save_cookies", lambda name: 0)
    monkeypatch.setattr(server, "run_setup_cookies", lambda: 0)
    assert run_main(monkeypatch, "--check") is None
    assert run_main(monkeypatch, "--download-model", "small") == 0
    assert run_main(monkeypatch, "--save-cookies-from-browser", "firefox") == 0
    assert run_main(monkeypatch, "--setup-cookies") == 0
    assert not (tmp_path / "rocm-starts").exists()


# --- gpu_context_broken() -------------------------------------------------------------------------

def old_gpu_context_broken(message) -> bool:
    """The test that stood at the transcriber's except clause before the AMD engine."""
    text = str(message).lower()
    return "cuda" in text or "cudnn" in text or "cublas" in text


OLD_BROKEN = [
    "CUDA failed with error out of memory",
    "cuDNN failed with status CUDNN_STATUS_EXECUTION_FAILED",
    "cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED",
    "CUDA error: an illegal memory access was encountered",
    "RuntimeError: cudaErrorLaunchFailure",
    "Library cublas64_12.dll is not found or cannot be loaded",
    "Could not load library cudnn_ops_infer64_8.dll",
    "xxCUDAxx",
]
HIP_BROKEN = [
    "hipBLAS failed with status HIPBLAS_STATUS_EXECUTION_FAILED",
    "rocBLAS error: Could not initialize Tensile host: No devices found",
    "hiprand failed",
    "HIP error: invalid device function",
    "hipErrorOutOfMemory",
    "hip error",
    "HSA_STATUS_ERROR_MEMORY_APERTURE_VIOLATION",
    "Memory access fault by GPU node-1 (Agent handle: 0x5555) on address 0x7f00. Reason: Page not present",
]
NOT_BROKEN = ["relationship error", "chipError", "could not decode the audio", "", "timed out", "hipster error"]


@pytest.mark.parametrize("message", OLD_BROKEN)
def test_every_message_that_broke_the_context_before_still_does(message):
    assert old_gpu_context_broken(message) is True
    assert server.gpu_context_broken(message) is True
    assert server.gpu_context_broken(RuntimeError(message)) is True


@pytest.mark.parametrize("message", HIP_BROKEN)
def test_the_amd_runtime_s_words_break_it_too(message):
    assert server.gpu_context_broken(message) is True


@pytest.mark.parametrize("message", NOT_BROKEN)
def test_other_failures_do_not_restart_the_server(message):
    assert old_gpu_context_broken(message) is False
    assert server.gpu_context_broken(message) is False


def test_the_new_test_is_the_old_one_widened():
    samples = OLD_BROKEN + HIP_BROKEN + NOT_BROKEN + ["Cuda", "CUDNN", "cuBlas", "nothing", "cu da"]
    for message in samples:
        if old_gpu_context_broken(message):
            assert server.gpu_context_broken(message), message


# --- amd_vram_mb(), gpu_memory_mb(), the gfx targets ------------------------------------------------

def drm_card(root: Path, name: str, vendor: str, total=None, used=None) -> None:
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n", encoding="ascii")
    if total is not None:
        (device / "mem_info_vram_total").write_text(f"{total}\n", encoding="ascii")
    if used is not None:
        (device / "mem_info_vram_used").write_text(f"{used}\n", encoding="ascii")


def test_amd_vram_takes_the_amd_card_with_the_most_memory(tmp_path):
    root = tmp_path / "drm"
    drm_card(root, "card0", "0x10de", 8192 * MIB, 1024 * MIB)  # an NVIDIA card: not the AMD engine's
    drm_card(root, "card1", "0x1002", 512 * MIB, 400 * MIB)  # the processor's graphics
    drm_card(root, "card2", "0x1002", 16368 * MIB, 2000 * MIB)
    drm_card(root, "card3", "0x1002", "garbage", 0)
    drm_card(root, "card4", "0X1002")  # no memory files
    (root / "card2-DP-1").mkdir()  # a connector, not a card
    (root / "renderD128").mkdir()
    (root / "version").write_text("drm 1.1.0\n", encoding="ascii")
    assert server.amd_vram_mb(root) == (14368, 16368)


def test_amd_vram_is_none_without_an_amd_card(tmp_path):
    assert server.amd_vram_mb(tmp_path / "missing") is None
    root = tmp_path / "drm"
    drm_card(root, "card0", "0x10de", 8192 * MIB, 1024 * MIB)
    drm_card(root, "card1", "0x1002")
    assert server.amd_vram_mb(root) is None
    drm_card(root, "card2", "0x1002", 4096 * MIB, 5000 * MIB)
    assert server.amd_vram_mb(root) == (0, 4096), "more used than there is reads as nothing free"


def test_gpu_memory_with_the_amd_engine_asks_amdgpu_not_nvidia_smi(monkeypatch, tmp_path):
    drm_card(tmp_path / "drm", "card0", "0x1002", 8192 * MIB, 1024 * MIB)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server.shutil, "which", lambda name: pytest.fail(f"{name} was looked for"))
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform="linux"))
    assert server.gpu_memory_mb() == (7168, 8192)
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform="win32"))
    assert server.gpu_memory_mb() is None, "Windows: float16, as without nvidia-smi"


def test_gpu_memory_on_the_default_engine_never_reads_amdgpu(monkeypatch, tmp_path):
    drm_card(tmp_path / "drm", "card0", "0x1002", 8192 * MIB, 1024 * MIB)
    monkeypatch.setattr(server, "amd_vram_mb", lambda root: pytest.fail("amdgpu was read"))
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    monkeypatch.setattr(server, "os", os_as("posix"))
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform="linux"))
    assert server.gpu_memory_mb() is None


@pytest.mark.parametrize("version, name", [(110000, "gfx1100"), (100300, "gfx1030"), (110501, "gfx1151"),
                                           (120001, "gfx1201"), (90010, "gfx90a"), (110502, "gfx1152")])
def test_gfx_target_reads_kfd_s_version(version, name):
    assert server.gfx_target(version) == name


def kfd_node(root: Path, name: str, version=None) -> None:
    (root / name).mkdir(parents=True)
    if version is not None:
        (root / name / "properties").write_text(f"cpu_cores_count 0\ngfx_target_version {version}\nvendor_id 4098\n",
                                               encoding="ascii")


def test_kfd_gfx_targets_lists_the_gpus_in_node_order(tmp_path):
    nodes = tmp_path / "kfd"
    kfd_node(nodes, "0", 0)  # the processor
    kfd_node(nodes, "1", 110000)
    kfd_node(nodes, "2", 90010)
    kfd_node(nodes, "10", 120001)
    kfd_node(nodes, "3")  # no properties file
    kfd_node(nodes, "x", 100300)  # not a node
    assert server.kfd_gfx_targets(nodes) == ["gfx1100", "gfx90a", "gfx1201"]
    assert server.kfd_gfx_targets(tmp_path / "missing") == []


def test_the_probe_names_the_gpu_by_its_gfx_target_on_linux(monkeypatch, tmp_path):
    kfd_node(tmp_path / "kfd", "0", 0)
    kfd_node(tmp_path / "kfd", "1", 110000)
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform="linux"))
    assert server.amd_gpu_label() == "gfx1100"
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform="win32"))
    assert server.amd_gpu_label() == "device 0"
    monkeypatch.setattr(server, "KFD_NODES_ROOT", tmp_path / "missing")
    monkeypatch.setattr(server, "sys", ModuleAs(sys, platform="linux"))
    assert server.amd_gpu_label() == "device 0"


# --- hard_exit() and finish(): TerminateProcess for the AMD engine on Windows ----------------------

def fake_ctypes(monkeypatch, calls):
    """ctypes with a kernel32 whose GetCurrentProcess() and TerminateProcess() record their calls and return."""

    class Function:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, *args))
            return self.result

    kernel32 = SimpleNamespace(GetCurrentProcess=Function("GetCurrentProcess", 4242),
                               TerminateProcess=Function("TerminateProcess", 0))
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(windll=SimpleNamespace(kernel32=kernel32),
                                                               c_void_p="c_void_p", c_uint="c_uint"))
    monkeypatch.setattr(server, "terminate_process", REAL_TERMINATE)
    return kernel32


@pytest.mark.parametrize("name, active, terminates", [
    ("nt", True, True), ("nt", False, False), ("posix", True, False), ("posix", False, False)])
def test_hard_exit_terminates_only_for_the_amd_engine_on_windows(monkeypatch, name, active, terminates):
    calls = []
    kernel32 = fake_ctypes(monkeypatch, calls)
    monkeypatch.setattr(server, "ROCM_ACTIVE", active)
    monkeypatch.setattr(server, "os", os_as(name, calls))
    with pytest.raises(Exited):
        server.hard_exit(3)
    ended = [("GetCurrentProcess",), ("TerminateProcess", 4242, 3)] if terminates else []
    assert calls == ended + [("_exit", 3)], "os._exit() as before, and after a TerminateProcess that failed"
    if terminates:
        assert kernel32.GetCurrentProcess.restype == "c_void_p"
        assert kernel32.TerminateProcess.argtypes == ("c_void_p", "c_uint")


@pytest.mark.parametrize("name, active", [("nt", False), ("posix", True), ("posix", False)])
def test_finish_is_sys_exit_everywhere_else(monkeypatch, name, active):
    calls = []
    fake_ctypes(monkeypatch, calls)
    monkeypatch.setattr(server, "ROCM_ACTIVE", active)
    monkeypatch.setattr(server, "os", os_as(name, calls))
    assert server.finish() is None, "the normal end of main() returns"
    assert server.finish(None) is None
    for code in (0, 2, 4):
        with pytest.raises(SystemExit) as info:
            server.finish(code)
        assert info.value.code == code
    assert calls == []


def test_finish_terminates_for_the_amd_engine_on_windows(monkeypatch):
    calls = []
    fake_ctypes(monkeypatch, calls)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("nt", calls))
    server.finish()  # the fake TerminateProcess returns, as the real one does only when it failed
    with pytest.raises(SystemExit) as info:
        server.finish(4)
    assert info.value.code == 4
    assert calls == [("GetCurrentProcess",), ("TerminateProcess", 4242, 0),
                     ("GetCurrentProcess",), ("TerminateProcess", 4242, 4)]


def test_the_log_and_stdio_are_flushed_before_the_process_is_terminated(monkeypatch):
    calls = []
    fake_ctypes(monkeypatch, calls)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("nt", calls))

    class Recording(logging.Handler):
        def emit(self, record):
            pass

        def flush(self):
            calls.append(("flush", "log"))

    class Stream:
        def __init__(self, name):
            self.name = name

        def flush(self):
            calls.append(("flush", self.name))

    handler = Recording()
    logging.getLogger("shisu-ko").addHandler(handler)
    monkeypatch.setattr(sys, "stdout", Stream("stdout"))
    monkeypatch.setattr(sys, "stderr", None)  # pythonw has none: no error
    try:
        with pytest.raises(Exited):
            server.hard_exit(3)
    finally:
        logging.getLogger("shisu-ko").removeHandler(handler)
    assert calls.index(("flush", "log")) < calls.index(("flush", "stdout")) < calls.index(("TerminateProcess", 4242, 3))


@pytest.mark.parametrize("end", ["hard_exit", "finish"])
def test_the_kept_models_live_until_terminate_process(monkeypatch, end):
    # Freeing a model hangs the ROCm runtime on Windows (#2038): the models load_model() kept must
    # still be held when TerminateProcess ends the process, whatever the exit path does first.
    windows_end(monkeypatch)
    kernel32 = sys.modules["ctypes"].windll.kernel32

    class Model:
        pass

    model = Model()
    ref = weakref.ref(model)
    server.KEPT_MODELS.append(model)
    del model
    alive = []
    terminate = kernel32.TerminateProcess

    def watching(*args):
        gc.collect()
        alive.append(ref() is not None)
        return terminate(*args)

    kernel32.TerminateProcess = watching
    with pytest.raises((Exited, SystemExit)):  # os_as's _exit raises Exited; finish(3) raises SystemExit after the fake returns
        getattr(server, end)(3)
    assert alive == [True], "the kept model was freed before TerminateProcess"


# Every exit of server.py goes through hard_exit() or finish(), so that with the AMD engine on
# Windows none of them reaches the interpreter's exit that can hang there (CTranslate2 #2085,
# #2101). The maintainer has no AMD card to see such a hang: these tests are the only guard.

def windows_end(monkeypatch):
    """The AMD engine on Windows, with a kernel32 that records; returns the list of calls."""
    calls = []
    fake_ctypes(monkeypatch, calls)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("nt", calls))
    return calls


def terminated(code):
    return [("GetCurrentProcess",), ("TerminateProcess", 4242, code)]


def exit_sites(tree) -> list:
    """(enclosing function, what) for every call that ends the process and every `raise SystemExit`."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call):
                called = child.func
                if isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name):
                    name = f"{called.value.id}.{called.attr}"
                    if name in ("os._exit", "sys.exit", "os.abort", "os.kill"):
                        found.append((function, name))
                elif isinstance(called, ast.Name) and called.id in ("exit", "quit"):
                    found.append((function, called.id))
            if isinstance(child, ast.Raise) and child.exc is not None:
                raised = child.exc.func if isinstance(child.exc, ast.Call) else child.exc
                if isinstance(raised, ast.Name) and raised.id == "SystemExit":
                    found.append((function, "raise SystemExit"))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def test_only_hard_exit_and_finish_end_the_process():
    tree = ast.parse((SERVER_DIR / "server.py").read_text(encoding="utf-8"))
    assert exit_sites(tree) == [("hard_exit", "os._exit"), ("finish", "sys.exit")]


def test_the_script_runs_main_through_the_entry_point():
    tree = ast.parse((SERVER_DIR / "server.py").read_text(encoding="utf-8"))
    guard, = [node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"]
    assert [ast.unparse(statement) for statement in guard.body] == ["entry_point()"]


def test_the_import_asks_rocm_engine_whether_it_runs_as_the_script():
    # The tools in server/tools import server.py under another name; on Windows they must keep the
    # default engine (rocm_engine()'s script=False), or their interpreter exit hangs with the ROCm build.
    # The tests import it with SHISUKO_ENGINE=default (_serverlib.py), so only the source can say.
    tree = ast.parse((SERVER_DIR / "server.py").read_text(encoding="utf-8"))
    assign, = [node for node in tree.body if isinstance(node, ast.Assign)
               and ast.unparse(node.targets[0]) == "(ROCM_ACTIVE, ROCM_REASON)"]
    assert ast.unparse(assign.value) == "rocm_engine(sys.argv, os.environ, script=__name__ == '__main__')"


@pytest.mark.parametrize("argv, code, result", [
    (["--check"], 0, None), (["--download-model", "small"], 2, 2),
    (["--save-cookies-from-browser", "firefox"], 0, 0), (["--setup-cookies"], 0, 0)])
def test_the_one_shot_commands_end_through_terminate_process_on_windows(monkeypatch, argv, code, result):
    calls = windows_end(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: pytest.fail("the instance lock was taken"))
    monkeypatch.setattr(server, "run_check", lambda *a: None)
    monkeypatch.setattr(server, "run_download_model", lambda name, *a: 2)
    monkeypatch.setattr(server, "run_save_cookies", lambda name: 0)
    monkeypatch.setattr(server, "run_setup_cookies", lambda: 0)
    assert run_main(monkeypatch, *argv) == result  # the fake TerminateProcess returns, as only a failed one does
    assert calls == terminated(code)


class InstantServer:
    """ThreadingHTTPServer that serves nothing: serve_forever() returns at once."""

    def __init__(self, address, handler):
        self.daemon_threads = False

    def serve_forever(self):
        pass

    def server_close(self):
        pass


@pytest.mark.parametrize("exit_code, code, result", [(None, 0, None), (server.EXIT_UPDATE, 4, 4)])
def test_the_server_s_end_goes_through_terminate_process_on_windows(monkeypatch, exit_code, code, result):
    # POST /update's exit 4 must reach run.cmd, or update.py never runs.
    calls = windows_end(monkeypatch)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "start_app", lambda args: SimpleNamespace(exit_code=exit_code))
    monkeypatch.setattr(server, "ThreadingHTTPServer", InstantServer)
    monkeypatch.setattr(server.Handler, "app", None)
    assert run_main(monkeypatch) == result
    assert calls == terminated(code)
    handler, restored = server.signal.installed
    assert handler is not signal.default_int_handler, "Ctrl+C while the model loads ends the process (end_on_ctrl_c())"
    assert restored is signal.default_int_handler, "serving, it raises again: server_close(), then finish()"
    assert server.signal.breaks == [handler, restored], "Ctrl+Break does what Ctrl+C does, never the console's ExitProcess"


def test_a_start_that_cannot_run_ends_through_terminate_process_on_windows(monkeypatch):
    calls = windows_end(monkeypatch)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: False)
    assert run_main(monkeypatch) == 2
    assert calls == terminated(2), "the port's lock is taken"
    calls.clear()

    def taken(address, handler):
        raise OSError(10048, "Only one usage of each socket address is normally permitted")

    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "start_app", lambda args: SimpleNamespace(exit_code=None))
    monkeypatch.setattr(server, "ThreadingHTTPServer", taken)
    monkeypatch.setattr(server.Handler, "app", None)
    assert run_main(monkeypatch) == 2
    assert calls == terminated(2), "the port cannot be listened on"


@pytest.mark.parametrize("error", [
    OverflowError("bind(): port must be 0-65535."),  # a port out of range that got past parse_args()
    TypeError("encoding of hostname failed"),  # a --host IDNA cannot encode
    UnicodeError("encoding with 'idna' codec failed (UnicodeError: label too long)")])
def test_a_listen_that_fails_otherwise_ends_through_terminate_process_on_windows(monkeypatch, caplog, error):
    # Not an OSError: it left main() for the interpreter's own exit, which frees the model and hangs
    # with the AMD engine on Windows, and run.cmd waited for ever.
    calls = windows_end(monkeypatch)
    engine_imports(monkeypatch)

    def refused(address, handler):
        raise error

    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "start_app", lambda args: SimpleNamespace(exit_code=None))
    monkeypatch.setattr(server, "ThreadingHTTPServer", refused)
    monkeypatch.setattr(server.Handler, "app", None)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch) == 2
    assert calls == terminated(2)
    assert "Cannot listen on" in caplog.text


@pytest.mark.parametrize("port", ["70000", "65536", "0", "-1", "http"])
def test_a_port_out_of_range_is_refused_before_anything_loads(monkeypatch, capsys, port):
    calls = windows_end(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: pytest.fail("the instance lock was taken"))
    monkeypatch.setattr(server, "start_app", lambda args: pytest.fail("a model was loaded"))
    assert run_main(monkeypatch, "--port", port) == 2, "argparse's code, which run.cmd does not retry"
    assert calls == [], "nothing is loaded yet: the interpreter's own exit is safe"
    assert "--port" in capsys.readouterr().err


def test_every_port_in_range_is_taken():
    for port in (1, 8790, 8791, 65535):
        assert server.parse_args(["--port", str(port)]).port == port
    assert server.parse_args([]).port == 8790


class FailingServer(InstantServer):
    def serve_forever(self):
        raise RuntimeError("select() failed")


def test_what_still_leaves_main_ends_through_terminate_process_on_windows(monkeypatch, caplog):
    # An exception from serve_forever() is no KeyboardInterrupt: main() let it go, and the
    # interpreter's own exit would free the model and hang there.
    calls = windows_end(monkeypatch)
    engine_imports(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "start_app", lambda args: SimpleNamespace(exit_code=None))
    monkeypatch.setattr(server, "ThreadingHTTPServer", FailingServer)
    monkeypatch.setattr(server.Handler, "app", None)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        with pytest.raises(SystemExit) as info:  # the fake TerminateProcess returns, as only a failed one does
            server.entry_point()
    assert info.value.code == 1, "run.cmd says the server stopped unexpectedly and starts it again"
    assert calls == terminated(1)
    assert "The server stopped on an unexpected error" in caplog.text and "select() failed" in caplog.text


def test_a_ctrl_c_that_still_leaves_main_ends_through_terminate_process_on_windows(monkeypatch, caplog):
    # Between start_app() and serve_forever(), where main()'s own handlers are not.
    calls = windows_end(monkeypatch)
    monkeypatch.setattr(server, "main", interrupted_load)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        try:
            with pytest.raises(SystemExit) as info:
                server.entry_point()
        except KeyboardInterrupt:
            pytest.fail("the Ctrl+C left for the interpreter's own exit")
    assert info.value.code == 0 and calls == terminated(0)
    assert "Shutting down" in caplog.text


def test_the_entry_point_passes_a_system_exit_on(monkeypatch):
    # argparse's, before anything is loaded; or finish()'s, once TerminateProcess has failed.
    calls = windows_end(monkeypatch)

    def main():
        raise SystemExit(2)

    monkeypatch.setattr(server, "main", main)
    with pytest.raises(SystemExit) as info:
        server.entry_point()
    assert info.value.code == 2 and calls == []


@pytest.mark.parametrize("name, active", [("nt", False), ("posix", True), ("posix", False)])
@pytest.mark.parametrize("error", [RuntimeError("select() failed"), KeyboardInterrupt()])
def test_elsewhere_what_leaves_main_is_the_interpreter_s(monkeypatch, name, active, error):
    calls = []
    fake_ctypes(monkeypatch, calls)
    monkeypatch.setattr(server, "ROCM_ACTIVE", active)
    monkeypatch.setattr(server, "os", os_as(name, calls))

    def main():
        raise error

    monkeypatch.setattr(server, "main", main)
    with pytest.raises(type(error)):
        server.entry_point()
    assert calls == []


def test_a_broken_gpu_context_ends_through_terminate_process_on_windows(monkeypatch):
    calls = windows_end(monkeypatch)
    alias_table(monkeypatch)
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    monkeypatch.setattr(server, "detect_speech", lambda audio, offset=0.0: [[offset, offset + 20.0]])

    class BrokenModel:
        def transcribe(self, audio, **options):
            raise RuntimeError("hipErrorLaunchFailure: unspecified launch failure")

    args = app_args(beam_size=1, initial_prompt="", language_patience=0.0, lyrics="off")
    app = server.App(args, model=BrokenModel(), device="cuda", compute_type="float16")
    s = server.Session(video_id="abcdefghijk", url="u", status="ready", duration=60.0,
                       audio=np.zeros(60 * server.SAMPLE_RATE, dtype=np.float32))
    with pytest.raises(Exited):
        REAL_TRANSCRIBER(app).process(s, 0.0, 20.0)
    assert calls == terminated(3) + [("_exit", 3)], "run.cmd restarts the server"


def test_no_model_left_ends_through_terminate_process_on_windows(monkeypatch):
    calls = windows_end(monkeypatch)
    alias_table(monkeypatch)
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    monkeypatch.setattr(server, "load_model", failing_load)
    app = server.App(app_args(), model=object(), device="cuda", compute_type="float16")
    with pytest.raises(Exited):
        app.reload_model("large-v3")
    assert calls == terminated(3) + [("_exit", 3)]


def interrupted_load(*args, **kwargs):
    raise KeyboardInterrupt


def test_ctrl_c_while_the_model_loads_ends_through_terminate_process_on_windows(monkeypatch, tmp_path, caplog):
    calls = windows_end(monkeypatch)
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", interrupted_load)
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")  # a crash counted by an earlier start
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        try:
            code = run_main(monkeypatch)
        except KeyboardInterrupt:
            pytest.fail("the Ctrl+C left main() for the interpreter's own exit")
    assert code == 0, "as a Ctrl+C while serving ends: run.cmd stops"
    assert calls == terminated(0)
    assert "Shutting down" in caplog.text
    assert starts.read_text(encoding="utf-8") == "1\n", "this start's count is taken back, the earlier crash's is not"


def test_ctrl_c_twice_while_the_model_loads_leaves_the_engine_on(monkeypatch, tmp_path):
    # A model download of minutes, stopped twice by the viewer: no crash of the engine at all.
    windows_end(monkeypatch)
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", interrupted_load)
    assert run_main(monkeypatch) == 0
    assert run_main(monkeypatch) == 0
    assert not (tmp_path / "rocm-starts").exists()
    assert server.rocm_engine(["server.py"], {}, script=True, platform="win32")[0] is True


def test_ctrl_c_during_a_cpu_start_leaves_an_earlier_crash_counted_on_windows(monkeypatch, tmp_path):
    # --device cpu never counts toward the crash guard (count_rocm_start()), so its Ctrl+C takes nothing back.
    calls = windows_end(monkeypatch)
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", interrupted_load)
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")  # a crash counted by an earlier GPU start
    assert run_main(monkeypatch, "--device", "cpu") == 0
    assert calls == terminated(0)
    assert starts.read_text(encoding="utf-8") == "1\n", "a --device cpu start was never counted, so nothing is taken back"


@pytest.mark.parametrize("before, after", [(None, None), ("1\n", "1\n")])
@pytest.mark.parametrize("name, active", [("nt", False), ("posix", True), ("posix", False)])
def test_ctrl_c_while_the_model_loads_is_the_interpreter_s_everywhere_else(monkeypatch, tmp_path, name, active,
                                                                          before, after):
    calls = []
    fake_ctypes(monkeypatch, calls)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", active)
    monkeypatch.setattr(server, "os", os_as(name, calls))
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", interrupted_load)
    starts = tmp_path / "rocm-starts"
    if before is not None:
        starts.write_text(before, encoding="utf-8")
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            run_main(monkeypatch)
    assert calls == []
    assert server.signal.installed == [], "Python's own Ctrl+C handler stays"
    assert server.signal.breaks == [], "and Ctrl+Break is not asked for: Linux has no SIGBREAK"
    assert (starts.read_text(encoding="utf-8") if starts.exists() else None) == after, \
        "a Ctrl+C is taken back from the crash guard, an earlier crash is not"


def test_ctrl_c_during_the_ctranslate2_load_ends_without_unwinding_on_windows(monkeypatch, tmp_path):
    # The load is one long call into CTranslate2; a Ctrl+C in it raised KeyboardInterrupt the moment
    # it returned, and the unwinding freed the new model before any except clause ran: that free
    # hangs the ROCm runtime on Windows (#2038). The handler of end_on_ctrl_c() runs at that point.
    calls = windows_end(monkeypatch)
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")

    def start_app(args):
        loading = object()  # the new model, on the loading frame's stack
        try:
            server.signal.ctrl_c()
        except KeyboardInterrupt:
            pytest.fail("the Ctrl+C raised KeyboardInterrupt, whose unwinding frees the model being loaded")
        pytest.fail(f"the load went on after the Ctrl+C with {loading}")

    monkeypatch.setattr(server, "start_app", start_app)
    assert run_main(monkeypatch) == 0
    assert calls == terminated(0)
    assert starts.read_text(encoding="utf-8") == "1\n", "this start's count is taken back"
    assert server.signal.handler is signal.default_int_handler, "put back when main() leaves the load"


def test_ctrl_c_during_the_probe_s_load_ends_without_unwinding_on_windows(monkeypatch, tmp_path, capsys):
    fake_engine(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "os", os_as("nt"))

    def run_probe_gpu(args):
        try:
            server.signal.ctrl_c()
        except KeyboardInterrupt:
            pytest.fail("the Ctrl+C raised KeyboardInterrupt, whose unwinding frees the model being loaded")
        pytest.fail("the test went on after the Ctrl+C")

    monkeypatch.setattr(server, "run_probe_gpu", run_probe_gpu)
    assert probe(monkeypatch) == [1]
    assert "The AMD GPU engine test was cancelled" in capsys.readouterr().out


# Python installs no handler for Ctrl+Break (SIGBREAK): the console's default one ends the process
# through ExitProcess, whose DLL detach is what TerminateProcess skips (os._exit() with the model
# held took about ten seconds on the maintainer's PC, and the issues cite hangs). With the AMD
# engine on Windows it does what Ctrl+C does, in each of the three places.

def test_ctrl_break_while_the_model_loads_ends_as_ctrl_c_does_on_windows(monkeypatch, tmp_path, caplog):
    calls = windows_end(monkeypatch)
    turned_on(tmp_path)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    starts = tmp_path / "rocm-starts"
    starts.write_text("1\n", encoding="utf-8")

    def start_app(args):
        try:
            server.signal.ctrl_break()
        except KeyboardInterrupt:
            pytest.fail("the Ctrl+Break raised KeyboardInterrupt, whose unwinding frees the model being loaded")
        pytest.fail("the load went on after the Ctrl+Break")

    monkeypatch.setattr(server, "start_app", start_app)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert run_main(monkeypatch) == 0
    assert calls == terminated(0) and "Shutting down" in caplog.text
    assert starts.read_text(encoding="utf-8") == "1\n", "the viewer's, not a crash: this start's count is taken back"
    assert server.signal.handlers[server.signal.SIGBREAK] is signal.default_int_handler, "put back as Ctrl+C's"


class BreakingServer(InstantServer):
    """A server that is serving when the viewer presses Ctrl+Break."""

    closed = False

    def serve_forever(self):
        server.signal.ctrl_break()

    def server_close(self):
        BreakingServer.closed = True


@pytest.mark.parametrize("exit_code, code", [(None, 0), (server.EXIT_UPDATE, 4)])
def test_ctrl_break_while_serving_ends_as_ctrl_c_does_on_windows(monkeypatch, caplog, exit_code, code):
    calls = windows_end(monkeypatch)
    engine_imports(monkeypatch)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "start_app", lambda args: SimpleNamespace(exit_code=exit_code))
    monkeypatch.setattr(server, "ThreadingHTTPServer", BreakingServer)
    monkeypatch.setattr(BreakingServer, "closed", False)
    monkeypatch.setattr(server.Handler, "app", None)
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        run_main(monkeypatch)
    assert BreakingServer.closed and "Shutting down" in caplog.text
    assert calls == terminated(code), "through TerminateProcess, with POST /update's code kept"


def test_ctrl_break_during_the_probe_ends_through_hard_exit_on_windows(monkeypatch, tmp_path, capsys):
    fake_engine(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "os", os_as("nt"))

    def run_probe_gpu(args):
        try:
            server.signal.ctrl_break()
        except KeyboardInterrupt:
            pytest.fail("the Ctrl+Break raised KeyboardInterrupt, whose unwinding frees the model being loaded")
        pytest.fail("the test went on after the Ctrl+Break")

    monkeypatch.setattr(server, "run_probe_gpu", run_probe_gpu)
    assert probe(monkeypatch) == [1]
    assert "The AMD GPU engine test was cancelled" in capsys.readouterr().out


# --- the model switch with the AMD engine on Windows: a restart -------------------------------------

def windows_app(monkeypatch, tmp_path, name="nt", active=True):
    """An App on the AMD engine as Windows sees it, started by run.cmd; hard_exit() records its code and raises Exited."""
    alias_table(monkeypatch)
    monkeypatch.setattr(server, "ROCM_ACTIVE", active)
    monkeypatch.setattr(server, "os", os_as(name))
    monkeypatch.setenv("SHISUKO_LAUNCHER", "1")
    exits = []

    def hard_exit(code):
        exits.append(code)
        raise Exited(code)

    monkeypatch.setattr(server, "hard_exit", hard_exit)
    monkeypatch.setattr(server, "download_model_files", lambda n, *a: str(tmp_path / "files" / n))
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    loads = []

    def load_model(args, name=None, path=None):
        loads.append((name or args.model, path))
        return object(), "cuda", "float16"

    monkeypatch.setattr(server, "load_model", load_model)
    app = server.App(app_args(), model=object(), device="cuda", compute_type="float16")
    return app, exits, loads


def prepare(app, name):
    """The switch's first tick: the files of `name` are fetched on the side thread."""
    app.request_model(name)
    assert app.switch_model_if_wanted() is False
    app.prepare_thread.join(5)
    assert app.model_prepared is not None and app.model_prepared[0] == server.canonical_model_name(name)


def test_a_switch_on_windows_with_the_amd_engine_restarts_into_the_model(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    old = app.model
    prepare(app, "Systran/faster-whisper-small")
    with pytest.raises(Exited):
        app.switch_model_if_wanted()
    assert exits == [3], "run.cmd starts the server again five seconds later"
    assert (tmp_path / "next-model").read_text(encoding="utf-8") == "small\n", "the canonical name"
    assert loads == [] and app.model is old, "the old model is never freed in this process"


def test_without_run_cmd_a_switch_with_the_amd_engine_on_windows_is_refused_before_its_download(monkeypatch,
                                                                                                 tmp_path, caplog):
    # A plain `python server\server.py`: the exit with code 3 would end the server for good.
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    monkeypatch.delenv("SHISUKO_LAUNCHER")
    monkeypatch.setattr(server, "download_model_files", lambda name, *a: pytest.fail("the files were fetched"))
    old = app.model
    app.request_model("small")
    with caplog.at_level(logging.INFO, logger="shisu-ko"):
        assert app.switch_model_if_wanted() is False
    assert app.prepare_thread is None, "no download for a switch that cannot happen"
    assert exits == [] and loads == [] and app.model is old
    assert not (tmp_path / "next-model").exists()
    assert (app.model_name, app.wanted_model, app.model_loading) == ("large-v3", "large-v3", None)
    assert app.model_error[0] == "small" and "run.cmd" in app.model_error[1]
    assert app.model_state("small")["model_error"] == app.model_error[1], "the popup hears why"
    assert "Staying with the model 'large-v3' instead of 'small'" in caplog.text
    app.request_model("small")  # the client sends its setting every second
    assert app.wanted_model == "large-v3", "asked again only after --retry-after"


def test_only_run_cmd_s_loop_decides_whether_a_switch_can_restart(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    app.args.no_update = True
    monkeypatch.setenv("SHISUKO_NO_UPDATE", "1")
    assert app.restart_blocker() is None, "run.cmd restarts after code 3 with --no-update too"
    monkeypatch.delenv("SHISUKO_LAUNCHER")
    assert "run.cmd" in app.restart_blocker()
    monkeypatch.setattr(server, "ROCM_ACTIVE", False)
    assert app.restart_blocker() is None, "the default engine switches in the process"
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "os", os_as("posix"))
    assert app.restart_blocker() is None, "so does the AMD engine on Linux"


@pytest.mark.parametrize("name, active", [("nt", False), ("posix", True)])
def test_everywhere_else_the_switch_stays_in_the_process(monkeypatch, tmp_path, name, active):
    app, exits, loads = windows_app(monkeypatch, tmp_path, name=name, active=active)
    monkeypatch.delenv("SHISUKO_LAUNCHER")  # a plain `python server.py` switches as before
    prepare(app, "small")
    assert app.switch_model_if_wanted() is True
    assert loads == [("small", str(tmp_path / "files" / "small"))]
    assert app.model_name == "small" and exits == []
    assert not (tmp_path / "next-model").exists()


def test_a_restart_that_cannot_be_asked_for_keeps_the_old_model(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    (tmp_path / "blocked").write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "NEXT_MODEL_PATH", tmp_path / "blocked" / "next-model")
    old = app.model
    prepare(app, "small")
    assert app.switch_model_if_wanted() is False
    assert exits == [] and loads == [] and app.model is old
    assert (app.model_name, app.wanted_model, app.model_loading) == ("large-v3", "large-v3", None)
    assert app.model_error[0] == "small" and "could not restart" in app.model_error[1]


def test_take_next_model_reads_the_name_once(monkeypatch, tmp_path):
    alias_table(monkeypatch)
    path = tmp_path / "next-model"
    assert server.take_next_model(path) is None
    assert not path.exists()
    path.write_text("Systran/faster-whisper-small\n", encoding="utf-8")
    assert server.take_next_model(path) == "small"
    assert not path.exists(), "gone before the model loads: a model that kills the start is not asked for again"
    assert server.take_next_model(path) is None


@pytest.mark.parametrize("text", ["", "\n", "../models/x", "C:\\models\\x", "/opt/models/x", "a b", "x" * 300, "\ufeff"])
def test_take_next_model_drops_a_name_the_server_would_refuse(monkeypatch, tmp_path, text):
    alias_table(monkeypatch)
    path = tmp_path / "next-model"
    path.write_text(text, encoding="utf-8")
    assert server.take_next_model(path) is None
    assert not path.exists()


def test_take_next_model_drops_a_file_that_is_not_utf8(monkeypatch, tmp_path):
    # A damaged file: UnicodeDecodeError out of start_app() would end each start on 1, and run.cmd
    # would restart into the same file every five seconds.
    alias_table(monkeypatch)
    path = tmp_path / "next-model"
    path.write_bytes(b"\xff\xfesmall\n")
    assert server.take_next_model(path) is None
    assert not path.exists(), "a damaged file goes too, or every restart meets it again"


def test_take_next_model_does_not_use_a_name_it_cannot_remove(monkeypatch, tmp_path, caplog):
    alias_table(monkeypatch)
    path = tmp_path / "next-model"
    path.write_text("small\n", encoding="utf-8")

    def unlink(self, missing_ok=False):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(path), "unlink", unlink)
    with caplog.at_level(logging.WARNING, logger="shisu-ko"):
        assert server.take_next_model(path) is None
    assert "starting with the default model" in caplog.text


def test_the_restart_starts_with_the_model_the_switch_named(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    (tmp_path / "next-model").write_text("small\n", encoding="utf-8")
    started = server.start_app(app_args())
    assert loads == [("small", str(tmp_path / "files" / "small"))], "through download_model_files(), never a raw string"
    assert (started.model_name, started.default_model, started.wanted_model) == ("small", "large-v3", "small")
    assert started.health()["model"] == "small" and started.health()["device"] == "cuda"
    assert not (tmp_path / "next-model").exists()


def test_the_restart_into_the_default_model_is_a_plain_start(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    (tmp_path / "next-model").write_text("large-v3\n", encoding="utf-8")
    started = server.start_app(app_args())
    assert loads == [("large-v3", None)] and started.model_name == "large-v3"
    assert not (tmp_path / "next-model").exists()


def test_a_model_the_restart_cannot_load_gives_way_to_the_default(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)

    def load_model(args, name=None, path=None):
        loads.append(name or args.model)
        if name == "small":
            raise RuntimeError("hipErrorOutOfMemory")
        return object(), "cuda", "float16"

    monkeypatch.setattr(server, "load_model", load_model)
    (tmp_path / "next-model").write_text("small\n", encoding="utf-8")
    started = server.start_app(app_args())
    assert loads == ["small", "large-v3"] and started.model_name == "large-v3"
    assert started.model_error[0] == "small" and started.in_cooldown("small")


def test_without_a_model_the_start_ends_on_2(monkeypatch, tmp_path):
    app, exits, loads = windows_app(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "load_model", failing_load)
    ends = []

    def finish(code=None):
        ends.append(code)
        raise Exited(code)

    monkeypatch.setattr(server, "finish", finish)
    with pytest.raises(Exited):
        server.start_app(app_args())
    assert ends == [2]


@pytest.mark.parametrize("name, active", [("nt", False), ("posix", True)])
def test_elsewhere_a_start_never_reads_the_next_model(monkeypatch, tmp_path, name, active):
    app, exits, loads = windows_app(monkeypatch, tmp_path, name=name, active=active)
    (tmp_path / "next-model").write_text("small\n", encoding="utf-8")
    started = server.start_app(app_args())
    assert loads == [("large-v3", None)] and started.model_name == "large-v3"
    assert (tmp_path / "next-model").read_text(encoding="utf-8") == "small\n", "left as it was"


# --- --probe-gpu --------------------------------------------------------------------------------------

def fake_engine(monkeypatch, tmp_path, devices=1, fail_on=(), fail_warmup=False, elsewhere=False, built=None):
    """The ROCm build as --probe-gpu meets it: ctranslate2 from ROCM_DIR (or another folder) with `devices`."""
    rocm = install_rocm(tmp_path)
    package = (tmp_path / "venv-site" if elsewhere else rocm) / "ctranslate2"
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(
        __version__="4.8.2", __file__=str(package / "__init__.py"), get_cuda_device_count=lambda: devices))
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "ROCM_REASON", "AMD ROCm, CTranslate2 from rocm (turned on by SHISUKO_ENGINE=rocm)")
    return fake_whisper(monkeypatch, fail_on=fail_on, fail_warmup=fail_warmup, built=built)


def probe(monkeypatch):
    """main() as amd_setup.py runs it: server.py --probe-gpu. Returns the codes hard_exit() was given."""
    exits = []

    def hard_exit(code):
        exits.append(code)
        raise Exited(code)

    monkeypatch.setattr(server, "hard_exit", hard_exit)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: pytest.fail("the instance lock was taken"))
    assert run_main(monkeypatch, "--probe-gpu") == exits[-1]
    return exits


class Untouchable:
    """A sys.modules entry the test fails on at the first attribute access: the library must not be used."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        pytest.fail(f"{self.name} was used (asked for {attr})")


def test_the_probe_flag_is_hidden_and_the_device_choices_stay(capsys):
    assert server.parse_args(["--probe-gpu"]).probe_gpu is True
    assert server.parse_args([]).probe_gpu is False
    with pytest.raises(SystemExit):
        server.parse_args(["--help"])
    assert "--probe-gpu" not in capsys.readouterr().out
    for device in ("auto", "cuda", "cpu"):
        assert server.parse_args(["--device", device]).device == device
    for device in ("rocm", "hip"):
        with pytest.raises(SystemExit):
            server.parse_args(["--device", device])


def test_the_probe_refuses_without_the_engine(monkeypatch, tmp_path, capsys):
    write_json(tmp_path / "config.json", {"model": "small", "engine": "rocm"})
    monkeypatch.setattr(server, "ROCM_REASON", "it was installed for cp311 and this Python is cp312")
    monkeypatch.setitem(sys.modules, "ctranslate2", Untouchable("ctranslate2"))
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    assert probe(monkeypatch) == [1]
    assert "The AMD engine is not active: it was installed for cp311 and this Python is cp312" in capsys.readouterr().out
    assert config(tmp_path) == {"model": "small", "engine": "rocm"}, "a refusal is no verdict"


def test_a_passed_probe_turns_the_engine_on_and_clears_the_guard(monkeypatch, tmp_path, capsys):
    made = fake_engine(monkeypatch, tmp_path)
    write_json(tmp_path / "config.json", {"model": "small"})
    (tmp_path / "rocm-starts").write_text("2\n", encoding="utf-8")
    assert probe(monkeypatch) == [0]
    assert config(tmp_path) == {"model": "small", "engine": "rocm"}
    assert not (tmp_path / "rocm-starts").exists()
    assert made == [("small", {"device": "cuda", "compute_type": "float16", "download_root": str(tmp_path / "models")})]
    out = capsys.readouterr().out
    assert f"CTranslate2 4.8.2 from {tmp_path / 'rocm' / 'ctranslate2'}: 1 device(s)" in out
    assert "AMD GPU engine works: device 0 (float16)" in out


@pytest.mark.parametrize("case, words", [
    (dict(fail_on=("cuda",)), "hipErrorNoBinaryForGpu"),
    (dict(fail_warmup=True), "illegal memory access"),
    (dict(devices=0), "no AMD GPU visible to the ROCm engine"),
    (dict(elsewhere=True), "that is not the CTranslate2 in"),
])
def test_a_failed_probe_turns_the_engine_off(monkeypatch, tmp_path, capsys, case, words):
    made = fake_engine(monkeypatch, tmp_path, **case)
    write_json(tmp_path / "config.json", {"model": "small", "engine": "rocm"})
    (tmp_path / "rocm-starts").write_text("1\n", encoding="utf-8")
    assert probe(monkeypatch) == [1]
    assert config(tmp_path) == {"model": "small"}
    assert (tmp_path / "rocm-starts").read_text(encoding="utf-8") == "1\n"
    out = capsys.readouterr().out
    assert "The AMD GPU engine does not work here" in out and words in out
    assert all(kwargs["device"] == "cuda" for _, kwargs in made), "never the CPU fallback"
    assert len(made) <= 1


def test_a_probe_whose_ctranslate2_does_not_load_fails(monkeypatch, tmp_path, capsys):
    fake_engine(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "ctranslate2", None)  # import raises ImportError
    assert probe(monkeypatch) == [1]
    assert "could not be loaded" in capsys.readouterr().out
    assert not (tmp_path / "config.json").exists() or "engine" not in config(tmp_path)


def test_a_cancelled_probe_still_ends_through_hard_exit(monkeypatch, tmp_path, capsys):
    # Ctrl+C in setup's window while the model loads: amd_setup.py's wait for this process cannot
    # be interrupted on Windows, and the interpreter's own exit could hang there with the ROCm
    # runtime loaded, which would freeze the setup until its fifteen-minute timeout.
    fake_engine(monkeypatch, tmp_path)
    write_json(tmp_path / "config.json", {"model": "small", "engine": "rocm"})
    monkeypatch.setattr(server, "load_model", interrupted_load)
    try:
        exits = probe(monkeypatch)
    except KeyboardInterrupt:
        pytest.fail("the Ctrl+C left the probe for the interpreter's own exit")
    assert exits == [1]
    assert "The AMD GPU engine test was cancelled" in capsys.readouterr().out
    assert config(tmp_path) == {"model": "small"}, "a cancelled test turns nothing on"


@pytest.mark.parametrize("fail_warmup, code", [(False, 0), (True, 1)])
def test_the_probe_never_frees_its_model_with_the_amd_engine_on_windows(monkeypatch, tmp_path, fail_warmup, code):
    # Freeing a model deadlocks the ROCm runtime on Windows (#2038): a passed test that freed its
    # model on the way out hung until amd_setup.py stopped it after fifteen minutes and turned the
    # engine off again; a failed warm-up freed it with the traceback that held it.
    built = []
    fake_engine(monkeypatch, tmp_path, fail_warmup=fail_warmup, built=built)
    monkeypatch.setattr(server, "rocm_on_windows", lambda: True)
    write_json(tmp_path / "config.json", {"model": "small"})
    assert server.run_probe_gpu(load_args(device="auto")) == code
    gc.collect()
    assert len(built) == 1 and built[0]() is not None, "the model is still there for hard_exit()"
    assert server.KEPT_MODELS == [built[0]()]


def test_a_ctrl_c_in_the_probe_s_warm_up_keeps_the_model_until_the_end_on_windows(monkeypatch, tmp_path, capsys):
    built = []
    fake_engine(monkeypatch, tmp_path, fail_warmup=KeyboardInterrupt(), built=built)
    monkeypatch.setattr(server, "rocm_on_windows", lambda: True)
    write_json(tmp_path / "config.json", {"model": "small"})
    ends = []

    def hard_exit(code):
        gc.collect()
        ends.append((code, [ref() is not None for ref in built]))
        raise Exited(code)

    monkeypatch.setattr(server, "hard_exit", hard_exit)
    assert run_main(monkeypatch, "--probe-gpu") == 1
    assert ends == [(1, [True])], "not freed before the process ends"
    assert "The AMD GPU engine test was cancelled" in capsys.readouterr().out


def test_everywhere_else_a_model_is_freed_as_before(monkeypatch, tmp_path):
    # NVIDIA, the CPU and Linux free the old model on a switch (App.switch_model_if_wanted()).
    built = []
    fake_engine(monkeypatch, tmp_path, built=built)
    monkeypatch.setattr(server, "rocm_on_windows", lambda: False)
    assert server.run_probe_gpu(load_args(device="auto")) == 0
    gc.collect()
    assert len(built) == 1 and built[0]() is None
    assert server.KEPT_MODELS == []


# --- --check's line -------------------------------------------------------------------------------

def test_the_engine_line_says_nothing_without_the_side_folder(tmp_path):
    assert server.rocm_engine_line() is None


def test_the_engine_line_says_why_an_installed_engine_is_not_used(monkeypatch, tmp_path):
    install_rocm(tmp_path)
    reason = server.rocm_engine_state({"engine": "rocm"}, {}, tmp_path / "rocm", TAG, 2, WINDOWS)[1]
    monkeypatch.setattr(server, "ROCM_REASON", reason)
    assert server.rocm_engine_line() == f"GPU engine: AMD ROCm installed but not used: {reason}"
    assert GUARD_WORDS in reason


def test_the_engine_line_names_the_build_in_use(monkeypatch, tmp_path):
    install_rocm(tmp_path)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    assert server.rocm_engine_line() == f"GPU engine: AMD ROCm (CTranslate2 4.8.2 from {tmp_path / 'rocm'})"


def no_registry(*args):
    raise OSError("no such key")


def check_stand_ins(monkeypatch, tmp_path, devices=1):
    """The stand-ins of test_setup_model.py's run_check() test: nothing here initialises a driver
    or reads the registry and the browsers' folders of the machine running the tests."""
    alias_table(monkeypatch)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(__version__="4.8.2",
                                                                    get_cuda_device_count=lambda: devices))
    version = SimpleNamespace(__version__="0")
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(version=version))
    monkeypatch.setitem(sys.modules, "yt_dlp.version", version)
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(HKEY_CURRENT_USER=0, REG_SZ=1, OpenKey=no_registry))
    for name in ("SHISUKO_HOME", "USERPROFILE", "HOME", "CHROME_CONFIG_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.delenv("SHISUKO_CONTAINER", raising=False)
    monkeypatch.setattr(server, "mlx_available", lambda: False)


def test_run_check_says_when_the_amd_engine_sees_no_gpu(monkeypatch, tmp_path, capsys):
    check_stand_ins(monkeypatch, tmp_path, devices=0)
    server.run_check()
    without = capsys.readouterr().out.splitlines()
    assert "CTranslate2 4.8.2: 0 CUDA device(s)  -> CPU fallback; consider --model small" in without
    assert not any("AMD" in line for line in without), "the default engine: as before"
    install_rocm(tmp_path)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    server.run_check()
    lines = capsys.readouterr().out.splitlines()
    hint, = [line for line in lines if "sees no AMD GPU" in line]
    assert "amd_setup.py --probe" in hint
    assert lines.index(hint) == lines.index(f"GPU engine: AMD ROCm (CTranslate2 4.8.2 from {tmp_path / 'rocm'})") + 1
    assert "CTranslate2 4.8.2: 0 CUDA device(s)" in lines
    assert not any("CPU fallback" in line for line in lines), "a start leaves the AMD engine, it does not fall back to the CPU"


def test_run_check_adds_the_engine_line_and_changes_no_other(monkeypatch, tmp_path, capsys):
    check_stand_ins(monkeypatch, tmp_path)
    server.run_check()
    without = capsys.readouterr().out.splitlines()
    assert not any(line.startswith("GPU engine") for line in without)
    install_rocm(tmp_path)
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    server.run_check()
    lines = capsys.readouterr().out.splitlines()
    engine = f"GPU engine: AMD ROCm (CTranslate2 4.8.2 from {tmp_path / 'rocm'})"
    assert engine in lines
    assert [line for line in lines if line != engine] == without, "every other line as it was"
    assert lines.index(engine) == lines.index("CTranslate2 4.8.2: 1 CUDA device(s)") + 1


# --- conftest.py's check of the real data folder ---------------------------------------------------

def real_folder_check():
    """conftest.py, as pytest loaded it (under the name "conftest") before any test module."""
    import conftest

    return conftest


def later(path: Path) -> None:
    """Move `path`'s mtime a second on: a file or folder written anew, whatever the clock's resolution."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))


def test_the_real_folder_check_leaves_out_what_the_viewer_s_own_server_writes(tmp_path):
    # The Start button on Linux/macOS appends the server's output to server.log (native_host.launch()),
    # and a server started meanwhile takes its lock: a clean run must not fail on either.
    check = real_folder_check()
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "server.log").write_bytes(b"")
    before = check.snapshot(tmp_path)
    with open(tmp_path / "server.log", "ab") as log:
        log.write(b"12:00:00 INFO    Listening on http://127.0.0.1:8790\n")
    later(tmp_path / "server.log")
    (tmp_path / "server-8790.lock").write_bytes(b"")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "abcdefghijk.webm").write_bytes(b"")  # the live server's cache: not looked into
    assert check.home_changes(before, check.snapshot(tmp_path)) == ["new: cache"]
    later(tmp_path / "config.json")
    (tmp_path / "rocm-starts").write_text("1\n", encoding="utf-8")
    assert sorted(check.home_changes(before, check.snapshot(tmp_path))) == [
        "modified: config.json", "new: cache", "new: rocm-starts"], "what a test could write is still seen"


def test_the_real_folder_check_looks_into_the_amd_engine_s_folders(tmp_path):
    # amd_setup.py's replace_dir() swaps rocm/ for a new one of the same names, and a download
    # leaves a .part file in cache/rocm-download: neither changes a name at the top level.
    check = real_folder_check()
    package = tmp_path / "rocm" / "ctranslate2"
    package.mkdir(parents=True)
    (tmp_path / "rocm" / "shisuko-rocm.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cache" / "rocm-download").mkdir(parents=True)
    (tmp_path / "models" / "models--Systran--faster-whisper-small").mkdir(parents=True)
    before = check.snapshot(tmp_path)
    (tmp_path / "models" / "models--Systran--faster-whisper-large-v3").mkdir()  # the live server's download
    assert check.home_changes(before, check.snapshot(tmp_path)) == []

    (tmp_path / "rocm").rename(tmp_path / "rocm.old")
    package.mkdir(parents=True)
    later(package)
    (tmp_path / "cache" / "rocm-download" / "rocm_sdk_core.whl.part").write_bytes(b"")
    assert sorted(check.home_changes(before, check.snapshot(tmp_path))) == [
        "created: rocm.old, holding: ctranslate2, shisuko-rocm.json", "gone: rocm/shisuko-rocm.json",
        "modified: rocm/ctranslate2", "new: cache/rocm-download/rocm_sdk_core.whl.part", "new: rocm.old"]

    package.rmdir()
    (tmp_path / "rocm").rmdir()  # --remove, or a swap that failed half-way
    changes = check.home_changes(before, check.snapshot(tmp_path))
    assert "gone: rocm" in changes and "removed: rocm" in changes


def test_the_real_folder_check_of_a_folder_that_was_not_there(tmp_path):
    check = real_folder_check()
    home = tmp_path / ".shisu-ko"
    before = check.snapshot(home)
    assert check.home_changes(before, check.snapshot(home)) == []
    (home / "rocm").mkdir(parents=True)
    assert check.home_changes(before, check.snapshot(home)) == ["the folder was created, holding: rocm", "created: rocm"]
    after = check.snapshot(home)
    (home / "rocm").rmdir()
    home.rmdir()
    assert check.home_changes(after, check.snapshot(home)) == ["the folder was removed", "removed: rocm"]


# --- Kitsune models (PyTorch) next to the AMD engine ---------------------------------------------

def test_a_probe_with_a_kitsune_model_chosen_tests_whisper_tiny_and_keeps_the_choice(monkeypatch, tmp_path, capsys):
    # Setup runs amd_setup.py after a Kitsune download, and --probe-gpu takes config.json's model:
    # a Kitsune model runs on PyTorch, never on this engine, so Whisper's smallest stands in.
    made = fake_engine(monkeypatch, tmp_path)
    write_json(tmp_path / "config.json", {"model": "kitsune-0.6b"})
    assert probe(monkeypatch) == [0]
    assert config(tmp_path) == {"model": "kitsune-0.6b", "engine": "rocm"}
    assert [name for name, _kwargs in made] == [server.PROBE_WHISPER_MODEL]
    assert "runs on PyTorch, not on this engine; testing with tiny instead" in capsys.readouterr().out


def test_a_kitsune_start_with_the_engine_on_clears_the_crash_guard(monkeypatch, tmp_path):
    # main() counts every GPU start with the engine on and only a load that got through takes it
    # back; a Kitsune model (PyTorch) cannot crash the ROCm runtime, so its start clears it too.
    monkeypatch.setattr(server, "ROCM_ACTIVE", True)
    monkeypatch.setattr(server, "ROCM_STARTS_PATH", tmp_path / "rocm-starts")
    (tmp_path / "rocm-starts").write_text("1\n", encoding="utf-8")
    model = SimpleNamespace(device="cpu", compute_label="float32", transcribe=lambda *a, **k: ([], None))
    monkeypatch.setattr(server, "kitsune_runtime_missing", lambda: None)
    monkeypatch.setattr(server, "kitsune_engine", lambda: SimpleNamespace(load=lambda *a, **k: model, PackageError=ValueError))
    monkeypatch.setattr(server, "gpu_memory_mb", lambda: None)
    args = SimpleNamespace(language="ja", device="auto", compute_type="auto", cpu_threads=0)
    assert server.load_kitsune_model(args, "kitsune-0.1b", path=str(tmp_path))[0] is model
    assert not (tmp_path / "rocm-starts").exists()
