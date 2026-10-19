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
                     engine, language, launcher}
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
the model chosen at setup (server.py --download-model NAME), the default of --model, and the
browser whose YouTube cookies every download sends (server.py --save-cookies-from-browser NAME),
the default of --cookies-from-browser.

The AMD engine (experimental) is CTranslate2's ROCm build, which server/amd_setup.py installs into
~/.shisu-ko/rocm, apart from the venv, and turns on in config.json ("engine": "rocm") once a test
run (server.py --probe-gpu) got a model through on the GPU. rocm_engine() puts that folder in front
of the venv's own CTranslate2 before anything imports it; to faster-whisper it is still "cuda".
"""
from __future__ import annotations

import argparse
import bisect
import copy
import difflib
import errno
import gc
import glob
import hashlib
import html
import io
import json
import logging
import math
import os
import random
import re
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
import threading
import time
import unicodedata
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

VERSION = "0.20.0"
# Exit codes run.cmd / run.sh act on: 0 stops the loop, 2 is a startup error that must not be retried
# (finish(); a failed --download-model ends on it too), 3 asks for a plain restart (hard_exit(): a
# broken GPU context, no model left, a model switch with the AMD engine on Windows, an AMD engine
# whose CTranslate2 does not load) and
# EXIT_UPDATE asks the launcher to run update.py first and then start the server again (POST /update).
EXIT_UPDATE = 4
SAMPLE_RATE = 16000
APP_DIR = Path(os.environ.get("SHISUKO_HOME") or (Path.home() / ".shisu-ko"))
CACHE_DIR = APP_DIR / "cache"
MODELS_DIR = APP_DIR / "models"
# {"model": ..., "cookies_from_browser": ..., "engine": "rocm"}, written at setup; read_config()
CONFIG_PATH = APP_DIR / "config.json"
# The AMD engine: CTranslate2's ROCm build in a folder of its own (amd_setup.py installs it with pip
# --target), never in the venv, whose PyPI build stays the NVIDIA/CPU engine. ROCM_MANIFEST in it
# says what was installed and for which Python ({"ctranslate2", "rocm", "python", "platform", ...}).
ROCM_DIR = APP_DIR / "rocm"
ROCM_MANIFEST = "shisuko-rocm.json"
# The platforms CTranslate2's ROCm wheels exist for: sysconfig's name, and the wheel's (and
# ROCM_MANIFEST's "platform"). amd_setup.platform_key() maps them the same way.
ROCM_PLATFORMS = {"win-amd64": "win_amd64", "linux-x86_64": "linux_x86_64"}
# What the Linux wheel loads from the system's ROCm 7.2, in $ROCM_PATH/lib (amd_setup.LINUX_LIBRARIES).
ROCM_LINUX_LIBRARIES = ("libamdhip64.so.7", "libhipblas.so.3", "libhiprand.so.1")
# The crash guard: the number of server starts on the AMD engine since the last one that got a model
# loaded and warmed up on the GPU. A card or driver the ROCm build cannot use can kill the process
# where no except clause sees it, and the launcher would restart it into the same crash for ever.
ROCM_STARTS_PATH = APP_DIR / "rocm-starts"
ROCM_GUARD_LIMIT = 2  # starts that did not get through before the AMD engine is left off
# The model a switch with the AMD engine on Windows restarts the server into (App.restart_for_model()).
NEXT_MODEL_PATH = APP_DIR / "next-model"
# Where Linux shows the AMD card's memory (amdgpu) and the GPUs' gfx targets (the ROCm kernel driver).
DRM_ROOT = Path("/sys/class/drm")
KFD_NODES_ROOT = Path("/sys/class/kfd/kfd/topology/nodes")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
# A faster-whisper size or a Hugging Face repo id. WhisperModel() also opens local directories, so
# anything else (paths, "..") is refused before it can point the server at an arbitrary folder.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$")
MODEL_NAME_HINT = ("not a model name: use a Kitsune model (kitsune-0.6b, kitsune-0.3b-int8, ...), a faster-whisper "
                   "size (large-v3, large-v3-turbo, small, ...) or a Hugging Face repo id like owner/name")
DEFAULT_MODEL = "large-v3"  # --model when neither the flag nor config.json names one
# ... except on the Apple GPU, where large-v3's 2.9 GB of weights share unified memory with the
# browser the viewer is watching in. Once they no longer fit, every decoder step pages them:
# measured on an M1 Pro, one window took 13 s with nothing else running and 81 s with Chrome
# playing a single video, which is below real time and no use to anyone. large-v3-turbo has four
# decoder layers instead of thirty-two and 1.5 GB of weights, so it fits and stays at 7.9x real
# time with the browser running, for one misheard word in the twenty-two lines of that window.
MLX_DEFAULT_MODEL = "large-v3-turbo"
# Kitsune-Transcribe's Japanese students (kitsune_engine.py), by name. Each repo holds the training
# run's bf16 export at its root (the bare name) and each precision in a folder of its own, as
# `python -m kitsune.quant export` writes it: kitsune-0.6b-int8 loads <repo>/int8-w8a16.
KITSUNE_REPOS = {
    "kitsune-0.6b": "Multy123/kitsune-transcribe-0.6b",
    "kitsune-0.3b": "Multy123/kitsune-transcribe-0.3b",
    "kitsune-0.1b": "Multy123/kitsune-transcribe-0.1b",
}
# A precision's short name -> its folder. The W8A8 / W4A4 files are byte-identical to the W8A16 /
# W4A16 ones (the formats differ in the activations, which the server keeps 16-bit), so both
# spellings load one folder and are one model to the server.
PROBE_WHISPER_MODEL = "tiny"  # what --probe-gpu tests the AMD engine with when config.json names a Kitsune model
KITSUNE_FORMATS = {"fp16": "fp16", "int8": "int8-w8a16", "fp8": "fp8-w8a8", "nvfp4": "nvfp4-w4a16", "mxfp4": "mxfp4-w4a4"}
KITSUNE_FORMAT_ALIASES = {"bf16": "", "int8-w8a16": "int8", "int8-w8a8": "int8", "fp8-w8a8": "fp8",
                          "nvfp4-w4a16": "nvfp4", "nvfp4-w4a4": "nvfp4", "mxfp4-w4a4": "mxfp4"}
# The files of a Kitsune package that the server fetches (config, processor, tokenizer, weights, recipe, card).
KITSUNE_FILES = ("config.json", "generation_config.json", "processor_config.json", "preprocessor_config.json",
                 "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "model.safetensors",
                 "quantization.json", "student_meta.json", "README.md", "MODEL_CARD.md")
KITSUNE_INSTALL_HINT = ("Kitsune models run on PyTorch, which is not installed for this server: run server/setup.cmd "
                        "(setup.sh) again and pick a Kitsune model, or install it with server/kitsune_setup.py")
# The browsers yt-dlp reads cookies from (yt_dlp.cookies.SUPPORTED_BROWSERS), the names
# --save-cookies-from-browser takes and config.json's "cookies_from_browser" may hold.
COOKIE_BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale")
# The cookies YouTube sets for a signed-in account; any of them in a browser's store means the
# sign-in that YouTube's "confirm you're not a bot" wall asks for is there to send.
YOUTUBE_SIGN_IN_COOKIES = frozenset({"LOGIN_INFO", "SAPISID", "__Secure-3PSID"})
CACHE_FORMAT = 6  # bumped when cue geometry or fields change; older caches are ignored and transcribed again
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
# transformers (Kitsune models only) logs advice meant for training code at warning level.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

NVIDIA_DIRS = add_nvidia_dll_dirs()


def read_json_object(path: Path) -> dict:
    """The JSON object in `path`, or {} for a missing, unreadable or corrupt file; never raises.

    For what rocm_engine() reads at import time, before logging has a handler: config.json is read
    again by read_config() moments later, which says why it ignores a file.
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001  (a deeply nested file raises RecursionError, not ValueError)
        return {}
    return data if isinstance(data, dict) else {}


def python_tag(version_info=None, gil_disabled=None) -> str:
    """This interpreter's CPython wheel tag (cp312; cp314t for a free-threaded build).

    CTranslate2's wheels are built per tag, and the one in ROCM_DIR loads only into the Python it
    was installed for: a venv rebuilt on a newer Python must fall back to its own CTranslate2.
    """
    major, minor = tuple(version_info or sys.version_info)[:2]
    if gil_disabled is None:
        gil_disabled = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    return f"cp{major}{minor}" + ("t" if gil_disabled else "")


def platform_key(name: Optional[str] = None) -> Optional[str]:
    """This Python's platform as the ROCm wheels name it (win_amd64, linux_x86_64), from sysconfig's
    name; None where no ROCm build exists (macOS, ARM, a 32-bit Python)."""
    return ROCM_PLATFORMS.get(sysconfig.get_platform() if name is None else name)


def in_container(environ=os.environ) -> bool:
    """True inside Shisu-ko's Docker image (SHISUKO_CONTAINER, set by the Dockerfile).

    Not any container: /.dockerenv and /run/.containerenv are in toolbox and distrobox too, which
    share the home folder and its Firefox profile, so a setup run there reads and saves a browser
    that its starts must then send.
    """
    return bool(environ.get("SHISUKO_CONTAINER"))


def read_rocm_starts(path: Path) -> int:
    """The crash guard's count (ROCM_STARTS_PATH); 0 for a missing, unreadable or garbled file."""
    try:
        return max(int(path.read_text(encoding="utf-8").strip()), 0)
    except Exception:  # noqa: BLE001
        return 0


def rocm_guard_allows(count: int) -> bool:
    """Whether the AMD engine may be tried again after `count` starts that did not get through."""
    return count < ROCM_GUARD_LIMIT


def rocm_engine_state(config: dict, env, rocm_dir: Path, tag: str, guard_count: int,
                      platform: Optional[str], prefix: str = "") -> tuple:
    """(active, reason): whether this process runs on the AMD engine, and why or why not.

    Never in Shisu-ko's Docker image (in_container()), whatever else says so: it shares the data
    folder with the native setup (DATA_DIR), side folder and crash guard included, but has no ROCm
    runtime, and a start that failed there would count against the native server's guard. Nor
    under a Python from the Nix store (`prefix`, sys.base_prefix: `nix run .` and its dev shell
    share ~/.shisu-ko too): the side folder's manylinux build needs the system's libraries, which
    Nix's loader does not search.
    SHISUKO_ENGINE=rocm|default decides over config.json (--probe-gpu runs with rocm before
    config.json says anything); config.json's "engine" is written only by a probe that passed.
    The engine also needs the CTranslate2 package in `rocm_dir`, installed for this Python (`tag`)
    on this platform (`platform`, platform_key()), and a crash guard that has not given up on it.
    The reason is shown by --check and the log.
    """
    if in_container(env):
        return False, ("Shisu-ko's Docker image keeps its own engine; the side folder in the data folder it shares "
                       "with the native setup is that setup's")
    if prefix.startswith("/nix/store/"):
        return False, ("Nix's Python keeps its own engine; the side folder in the data folder it shares with the "
                       "native setup is that setup's (it needs the system's libraries, which Nix's loader does not search)")
    choice = str(env.get("SHISUKO_ENGINE") or "").strip().lower()
    if choice == "default":
        return False, "SHISUKO_ENGINE=default asks for the default engine"
    if choice == "rocm":
        source = "SHISUKO_ENGINE=rocm"
    elif config.get("engine") == "rocm":
        source = "config.json"
    else:
        return False, ("config.json does not turn it on, which only a passed test does "
                       "(server/amd_setup.py --probe tests the AMD GPU again)")
    if not (rocm_dir / "ctranslate2" / "__init__.py").is_file():
        return False, f"{rocm_dir} holds no CTranslate2 (server/amd_setup.py installs it)"
    manifest = read_json_object(rocm_dir / ROCM_MANIFEST)
    built_for = manifest.get("python")
    if built_for != tag:
        return False, (f"it was installed for {built_for or 'an unknown Python'} and this Python is {tag} "
                       "(server/amd_setup.py installs the matching one)")
    built_on = manifest.get("platform")
    if platform is None or built_on != platform:
        return False, (f"it was installed for {built_on or 'an unknown platform'} and this is "
                       f"{platform or 'a platform without an AMD build'} (server/amd_setup.py installs the matching one)")
    if not rocm_guard_allows(guard_count):
        # No count in the words: check_rocm_import() sets the guard to its limit after one start.
        return False, ("the AMD engine did not get a model onto the AMD GPU at its last start (a crash, a CTranslate2 "
                       "that did not load, or no AMD GPU in sight), so the server leaves it off until "
                       "server/amd_setup.py --probe tests it again")
    return True, f"AMD ROCm, CTranslate2 from {rocm_dir} (turned on by {source})"


def rocm_root(env) -> str:
    """Where the system's ROCm lives on Linux: $ROCM_PATH, default /opt/rocm (no trailing slash)."""
    return (env.get("ROCM_PATH") or "/opt/rocm").rstrip("/")


def rocm_library_path(env) -> Optional[str]:
    """The LD_LIBRARY_PATH the ROCm build needs on Linux, or None when $ROCM_PATH/lib is on it already.

    The Linux wheel links against the system's ROCm (libamdhip64.so.7, libhipblas.so.3 under
    $ROCM_PATH/lib, default /opt/rocm; OpenMP under lib/llvm/lib). AMD's post-install step puts
    them in ld.so.conf, but not every install did it, and the dynamic loader reads LD_LIBRARY_PATH
    only when a process starts: rocm_engine() starts the server once more with this value.
    """
    root = rocm_root(env)
    lib = root + "/lib"
    current = env.get("LD_LIBRARY_PATH") or ""
    if lib in (entry.rstrip("/") for entry in current.split(":")):
        return None
    return ":".join([lib, root + "/lib/llvm/lib"] + ([current] if current else []))


def missing_rocm_libraries(env, exists=os.path.exists) -> list:
    """The ROCM_LINUX_LIBRARIES that are not in $ROCM_PATH/lib (rocm_root()); [] when all are there.

    amd_setup.py installs the Linux engine only where they are, but ROCm can be removed or
    upgraded past 7.2 later, and a ROCM_PATH exported in the terminal that ran the setup is not
    in the Start button's environment: every start would then fail on the import, with exit
    code 2, which the launcher never retries. Without them the server keeps the default engine,
    and takes the AMD one again by itself once they are back.
    """
    lib = rocm_root(env) + "/lib"
    return [name for name in ROCM_LINUX_LIBRARIES if not exists(lib + "/" + name)]


def rocm_engine(argv, environ, script: bool, platform: str = sys.platform) -> tuple:
    """Put the AMD engine in front of the venv's CTranslate2 when it is turned on; (active, reason).

    Runs at import time, before anything imports ctranslate2 or faster_whisper, and never raises.
    With the engine off (no side folder, no "engine": "rocm") nothing changes at all. On, ROCM_DIR
    goes first on sys.path and CTranslate2 gets the cub_caching allocator: its default on Linux
    loses text or aborts on AMD cards (CTranslate2 #2090, #2021), and it must be set before the
    first allocation. A program that only imports this file (the tools in server/tools) ends
    through the interpreter's own exit, where main()'s finish() cannot reach: on Windows it keeps
    the default engine even with SHISUKO_ENGINE=rocm, since once it has loaded a model, freeing it
    and that exit hang every time with the ROCm runtime (CTranslate2 #2038, #2085, #2101). On Linux
    SHISUKO_ENGINE=rocm turns the AMD engine on for it, provided $ROCM_PATH/lib is on its
    LD_LIBRARY_PATH already, since it is not started again. On Linux the ROCm libraries have to
    be on LD_LIBRARY_PATH from the start of the process, so the server starts itself once more
    with them (SHISUKO_ROCM_REEXEC=1 makes it once); without them in $ROCM_PATH/lib it keeps the
    default engine.
    --probe-gpu ignores the crash guard: it is how a guarded engine gets tested again.
    """
    try:
        probe = "--probe-gpu" in argv[1:]
        guard = 0 if probe else read_rocm_starts(ROCM_STARTS_PATH)
        active, reason = rocm_engine_state(read_json_object(CONFIG_PATH), environ, ROCM_DIR, python_tag(), guard,
                                           platform_key(), sys.base_prefix)
    except Exception as exc:  # noqa: BLE001  (a side folder this user may not read: is_file() raises on EACCES)
        return False, f"{ROCM_DIR} could not be checked ({exc})"
    if not active:
        return False, reason
    if not script and platform.startswith("win"):
        return False, ("a program that only imports server.py keeps the default engine on Windows: once it has "
                       "loaded a model, its exit through the interpreter hangs with the ROCm runtime "
                       "(CTranslate2 #2085, #2101)")
    if not script and str(environ.get("SHISUKO_ENGINE") or "").strip().lower() != "rocm":
        return False, ("a program that only imports server.py keeps the default engine "
                       "(SHISUKO_ENGINE=rocm turns the AMD engine on for it too, on Linux)")
    if platform.startswith("linux"):
        missing = missing_rocm_libraries(environ)
        if missing:
            return False, (f"{', '.join(missing)} of ROCm 7.2 not found under {rocm_root(environ)}/lib (set ROCM_PATH "
                           "where it lives, or server/amd_setup.py --remove takes the AMD engine out)")
    if platform.startswith("linux") and environ.get("SHISUKO_ROCM_REEXEC") != "1":
        library_path = rocm_library_path(environ)
        if library_path is not None:
            if not script:
                return False, (f"$ROCM_PATH/lib is not on LD_LIBRARY_PATH, which server.py sets itself only when it "
                               f"runs as the server (export LD_LIBRARY_PATH={library_path} first)")
            saved = {name: environ.get(name) for name in ("LD_LIBRARY_PATH", "SHISUKO_ROCM_REEXEC")}
            environ["LD_LIBRARY_PATH"] = library_path
            environ["SHISUKO_ROCM_REEXEC"] = "1"
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()  # execv() drops what this process has not written yet
                except Exception:  # noqa: BLE001  (None under pythonw)
                    pass
            try:
                os.execv(sys.executable, [sys.executable] + list(argv))
            except Exception as exc:  # noqa: BLE001
                for name, value in saved.items():
                    if value is None:
                        environ.pop(name, None)
                    else:
                        environ[name] = value
                return False, f"the server could not start itself again with the ROCm libraries on LD_LIBRARY_PATH ({exc})"
    sys.path.insert(0, str(ROCM_DIR))
    environ.setdefault("CT2_CUDA_ALLOCATOR", "cub_caching")
    return True, reason


# The AMD engine, when setup installed it and its test passed. Logged by main(), shown by --check.
ROCM_ACTIVE, ROCM_REASON = rocm_engine(sys.argv, os.environ, script=__name__ == "__main__")

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


# Whisper punctuates spontaneous Japanese only when its context suggests punctuation, and it decodes
# each window with nothing in front of it, so the cheapest way to ask is a short prompt written the
# way the subtitles should read. Measured over 15 minutes of one talk video: marks at 88% of the
# hand-labelled sentence ends against 53% without it, and not one line of the prompt reached the
# transcript. A language with no entry here gets no prompt; --initial-prompt "" turns it off.
DEFAULT_PROMPTS = {"ja": "はい、そうですね。今日はよろしくお願いします。それで、どう思いますか？"}

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


# --------------------------------------------------------------------------- sentence ends

# Sentence-final particles and the polite and copula endings. Sorted longest first, so よね is
# matched before ね, でしょう before でしょ and ました before the weak た below.
SENTENCE_STRONG = tuple(sorted((
    "よね", "ね", "よ", "な", "なあ", "わ", "ぞ", "ぜ", "さ", "か", "かな", "っけ",
    "でしょ", "でしょう", "じゃん", "もん", "です", "ます", "ました", "ません", "でした",
    "ましょう", "ください", "なさい", "んだ", "のだ", "んです", "だ",
), key=len, reverse=True))
# Plain forms. They end a casual sentence, but they also run on into the next clause
# (食べた + ので), so only a longer pause makes one an end.
SENTENCE_WEAK = tuple(sorted(("た", "ない", "る", "い"), key=len, reverse=True))
SENTENCE_QUESTION = frozenset({"か", "かな", "っけ", "でしょ", "でしょう"})
# A mark is never written over one of these, ...
SENTENCE_CLOSERS = SENTENCE_END | CLAUSE_BREAK | set("」』）)]】》〉”’\"'")
CLOSER_CHARS = "".join(sorted(SENTENCE_CLOSERS))   # the same set as a str, for rstrip()
# ... nor before a word that is one of these, which can only continue the sentence: です in ですか
# is left alone and the か judged instead. The test is on the whole word, not its first character:
# はい, やっぱり, もう, ところで and でも all open a sentence and all start with a particle kana.
# Whisper writes the sentence-initial connective with its own comma (で、 / でも、), and that comma
# is the evidence that this で opens a sentence rather than closing a phrase.
SENTENCE_PARTICLES = frozenset((
    "は", "が", "を", "に", "で", "と", "も", "の", "へ", "や", "か", "ね", "よ", "な",
    "から", "まで", "より", "とか", "など", "って", "には", "では", "とは", "のは", "のに",
    "ので", "のか",
))
# The か of these is a filler, not a question: the speaker is still choosing the next word
# (悩んで、なんか || , 行きたい人多そうなんか || さ). Three of the four false marks measured.
FILLER_KA = ("なんか", "とか", "なんとか", "というか", "っていうか", "なんつーか")
# こと + か is the nominaliser and a real question (老害教師少なめってことか？), and it ends in とか.
SHAPE_EXCEPT = ("ことか",)
# Whole words that end in a shape kana without being sentence-final: 何か is not a question, そんな
# is not a な, また is not a た. A shape is a suffix test, so without this table 何か行きたい becomes
# 何か？行きたい.
SENTENCE_NOT_ENDINGS = (
    "何か", "誰か", "いつか", "どこか", "確か", "なにか", "だれか", "どっか",
    "そんな", "こんな", "どんな", "あんな", "また", "まだ", "ただ",
)
# A sentence-final particle straight after a clause connective is interjectional - the speaker is
# holding the floor, not ending the sentence (めっちゃ偏見だけどさ || はいはい). The cost is a real
# だからね。 now and then; the gain is every けどさ, からさ, してね that used to cut a sentence in two.
SENTENCE_CONNECTIVES = ("けど", "から", "し", "て", "で", "のに", "ので")
SENTENCE_INTERJECTIONAL = frozenset({"さ", "ね", "よ", "な"})


def longest_silence(start: float, end: float, intervals) -> float:
    """The longest stretch of [start, end) that a sorted, merged interval list leaves uncovered."""
    if end <= start:
        return 0.0
    longest, cursor = 0.0, start
    for a, b in intervals:
        if b <= start:
            continue
        if a >= end:
            break
        longest = max(longest, min(a, end) - cursor)
        cursor = max(cursor, min(b, end))
    return max(longest, end - cursor)


def sentence_shape(text: str) -> tuple:
    """(shape, strong) for the sentence-final expression this text ends in, else (None, False)."""
    for shape in SENTENCE_STRONG:
        if text.endswith(shape):
            return shape, True
    for shape in SENTENCE_WEAK:
        if text.endswith(shape):
            return shape, False
    return None, False


def punctuate_words(words, speech, next_word, limits: CueLimits) -> list:
    """Write the sentence mark Whisper left out, where a sentence-final shape meets a pause.

    Rewrites the Word objects in place and returns the same list, as repair_lead_words does; every
    caller hands it a fresh list from absolute_words(), and one that did not would find its words
    rewritten.

    The cue builder has no sentence signal of its own: every cut in group_words() and every seam in
    merge_segments() defers to Whisper's punctuation, and Whisper writes it inconsistently. The same
    audio decoded twice gave そうなんですよねおじいちゃん先生とゲームの話したりするの?, which the
    builder made one 29-character line of, and そうなんですよね。, which it made three readable ones
    of. Neither half of the evidence stands alone - a Japanese speaker pauses inside a word, and よね
    runs on mid-sentence - but a sentence-final expression followed by a pause is the signal Kyoto's
    spontaneous-Japanese work (Akita et al. 2006) reaches F 0.85 with, and that pair is the rule here.

    `next_word` is the next segment's first Word, or None; inside the list the next word answers for
    it. It is the whole Word and not just its start because every test below asks what follows as
    well as when: a segment opening on っと or on a particle continues the one before it, and a mark
    between them would be as wrong across a segment boundary as inside one. A word with nothing
    after it gets no mark at all: the pause is the evidence, and without something following there
    is none to measure.

    The pause is the longer of two measures, because each hides what the other shows. The detector's
    intervals are read from the word's own start, not its end: Whisper anchors a segment's last word
    to the end of the audio it decoded, so the silence that follows the utterance usually lies inside
    that word's span, where the gap to the next word is zero. And the intervals miss a short pause
    entirely - Silero refuses a silence under 300 ms and pads what it keeps by 200 ms on each side,
    so the measured 0.34 s after よね at 18:06 sat in the middle of one interval - where Whisper's own
    word timings still show it, flush as they otherwise are (two gaps over 0.25 s in 50 s of talk).
    """
    if not limits.sentence_ends:
        return words
    for i, w in enumerate(words):
        # The next two words, the second only to see a comma Whisper split off as a token of its
        # own (で then 、). Past the end of the segment the next segment's first word stands in.
        ahead = words[i + 1:i + 3]
        if len(ahead) < 2 and next_word is not None:
            ahead = ahead + [next_word]
        if not ahead:
            break
        nxt, after_next = ahead[0], (ahead[1] if len(ahead) > 1 else None)
        if not (w.word or "").strip():
            continue
        text = word_text(words[:i + 1])
        if not text or text[-1] in SENTENCE_CLOSERS:
            continue
        following = (nxt.word or "").strip()
        # で、 and でも、 open a sentence; the comma is sometimes its own token, so look past it.
        opens_clause = (following[-1:] in CLAUSE_BREAK
                        or (after_next is not None and (after_next.word or "").strip()[:1] in CLAUSE_BREAK))
        if following[:1] in NO_LINE_START:
            continue
        if following.rstrip(CLOSER_CHARS) in SENTENCE_PARTICLES and not opens_clause:
            continue
        shape, strong = sentence_shape(text)
        if shape is None or (text.endswith(SENTENCE_NOT_ENDINGS) and not text.endswith(SHAPE_EXCEPT)):
            continue
        if shape == "か" and text.endswith(FILLER_KA) and not text.endswith(SHAPE_EXCEPT):
            continue
        if shape in SENTENCE_INTERJECTIONAL and text[:-len(shape)].endswith(SENTENCE_CONNECTIVES):
            continue
        gap = nxt.start - w.end
        pause = max(gap, longest_silence(w.start, nxt.start, speech)) if speech else gap
        if pause < (limits.sentence_pause if strong else limits.sentence_pause_weak):
            continue
        w.word = (w.word or "") + ("？" if shape in SENTENCE_QUESTION else "。")
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
LYRICS_QUIET_RMS = 0.004     # the lowest floor a quietly mixed video's own speech may pull it down to
LYRICS_MAX_NO_SPEECH = 0.9   # the decoder's "not speech" probability for its 30 s decode. No judge of
                             # singing: a rap verse scored 0.59 and an 18-voice chorus 0.80, every line
                             # right, while a sign-off made up over background music scored 0.47; so
                             # only what the decoder is all but sure of is refused on it
LYRICS_MIN_LOGPROB = -0.8    # sung windows scored -0.14 to -0.58; made-up lines -0.49 to -0.91
LYRICS_MIN_WORD_PROB = 0.35  # mean word probability: genuine lines from 0.48, a garbled それられ 0.30
LYRICS_MIN_STRETCH_S = 4.0   # a talk window's loud stretch the detector heard nothing in is put to the
                             # head from this length: about one sung line, and each costs an encoder pass


# What no transcript holds. Whisper's tokenizer has no token of its own for most of Unicode and
# spells such a character with tokens for pieces of its UTF-8 bytes (a full-width ！ is 171 120 223,
# a token a byte), so the decoder can also spell one no font has: in s1UVis0tPiE large-v3 wrote
# 171 253 255 (EF 9F AD), U+F7ED of the private use area, three times, each where a sentence mark belongs
# (励ましたい\uf7ed励まされている\uf7ed). Those bytes are valid UTF-8, so faster-whisper's word
# splitter, which only waits out an unfinished character (U+FFFD), passed it on as a word, and
# Firefox drew a box reading F7 ED. Dropped: the private use area (Co); code points with no
# character (Cn: unassigned, and the noncharacters); lone surrogates (Cs, which save_cache() could
# not even encode); controls (Cc, drawn as boxes too, and a "\n" is a row break, which only the cue
# builder writes); the line and paragraph separators (Zl, Zp), row breaks as well; the format
# controls (Cf: a zero-width space, a byte order mark, a bidi override, the joiner inside an emoji
# sequence), invisible, yet in the text Yomitan scans and on the card, and an override reverses
# what follows it; and U+FFFD, bytes that never made a character (a decode cut off inside one).
MODEL_TEXT_DROP = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"})


def clean_model_text(text: str, keep_newline: bool = False) -> str:
    """`text` less every character of a MODEL_TEXT_DROP category and U+FFFD. `keep_newline` keeps
    "\\n", the row break the cue builder puts between a cue's two rows, which a cached cue holds."""
    return "".join(ch for ch in text
                   if (keep_newline and ch == "\n")
                   or (ch != "\ufffd" and unicodedata.category(ch) not in MODEL_TEXT_DROP))


def cleaned_words(seg) -> list:
    """[(word, its text cleaned), ...]: the segment's own word objects, in order, each that keeps
    any text once clean_model_text() has been over it. absolute_words() puts these on the clock,
    so an index into its list is an index into this one."""
    out = []
    for w in getattr(seg, "words", None) or []:
        text = clean_model_text(w.word or "")
        if text:
            out.append((w, text))
    return out


def absolute_words(seg, offset: float) -> list:
    """The segment's words shifted onto the video's timeline, their text cleaned (cleaned_words()).

    A word left with no text goes, so the time it spanned reaches the cue builder as the gap it
    was, and punctuate_words() reads it as a pause: each U+F7ED of s1UVis0tPiE stood where a
    sentence mark belongs."""
    return [Word(text, offset + float(w.start), offset + float(w.end), float(getattr(w, "probability", 1.0) or 0.0))
            for w, text in cleaned_words(seg)]


def segment_text(seg) -> str:
    """The segment's own text as the gates judge it: cleaned as its words are, and stripped."""
    return clean_model_text(getattr(seg, "text", "") or "").strip()


PUNCTUATION = set("\"'“¿([{-。！？、，,.!?:：;；)]}、…～~ー'\"")


def word_anomaly_score(word: Word) -> float:
    """faster_whisper.transcribe.word_anomaly_score (1.2.1, MIT) less its short-word term: improbable
    or stretched words.

    Upstream also adds (0.133 - duration) * 15 for a word under 133 ms. In Japanese that measures
    the tokenizer, not the audio: Whisper's Japanese words are sub-tokens, usually one kana, under
    133 ms by construction. Over two 15-minute talk dumps the first-8-words score reached the
    threshold for 37 of 279 segments and 34 of 202 with the term, 0 and 2 without; every segment the
    talk gate deleted on it was real speech (5 of 5 in 15 minutes, 29 lines in 17 before the lead
    repair). Over the lyrics and rescued windows of the quiet-video measurement set it fired 21
    times: 13 real lines (キビトの変身!, a sung 一撃一撃一撃, 頑張ってね、ゴーゴンちゃん), 2 garbled
    fragments, and 6 inventions the blocklist, the repetition test or the stretched-word score
    refuse anyway.
    """
    score = 0.0
    duration = word.end - word.start
    if word.probability < 0.15:
        score += 1.0
    if duration > 2.0:
        score += duration - 2.0
    return score


def is_segment_anomaly(words) -> bool:
    """faster_whisper.transcribe.is_segment_anomaly (1.2.1, MIT), on word_anomaly_score() above:
    the sum over the first 8 non-punctuation words reaches 3, or the word count."""
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
    text = segment_text(seg)
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
    text = segment_text(seg)
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
    # Sentence ends (punctuate_words): how long a pause has to be behind a sentence-final shape.
    sentence_pause: float = 0.30       # behind よね, です, か: the shape carries most of the evidence
    sentence_pause_weak: float = 0.60  # behind た, ない, る, い, which run on as often as they end
    sentence_ends: bool = True         # --sentence-ends off


def cue_limits(args) -> CueLimits:
    return CueLimits(
        max_chars=int(getattr(args, "max_cue_chars", 30)),
        max_seconds=float(getattr(args, "max_cue_seconds", 7.0)),
        min_seconds=float(getattr(args, "min_cue_seconds", 0.8)),
        sentence_ends=getattr(args, "sentence_ends", "auto") != "off",
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


def is_katakana(ch: str) -> bool:
    return "ァ" <= ch <= "ヺ"


def breaks_word(head_text: str, tail_text: str) -> bool:
    """True when a cut between these two texts lands inside a word, on evidence and not on taste.

    Japanese writes no spaces, so the cheap evidence of a word boundary is the script change. Two
    signs are unambiguous: the tail opens with a character that can never open a word, or with
    hiragana directly after a kanji, which is okurigana. That pair is what keeps 言|ってた, 広|い,
    動|いた and 思|っております off the screen. Katakana is left out deliberately - katakana words are
    self-delimiting, so the hiragana after カメラ or ポンポン does open a new word.

    Kept apart from may_break() because only this one may overrule a length budget. Refusing a cut
    the splitter was about to make costs nothing, so may_break() can also say no on taste; a merge
    that overrules its own limits on taste builds a 41-character line. That is why a cut between two
    kanji or two katakana is only refused by may_split(): it is likely a compound, not proof (今日|学校).
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


def joins_compound(head_text: str, tail_text: str) -> bool:
    """True when the cut falls between two kanji or two katakana: likely inside a compound (能|力)."""
    head, tail = (head_text or "").strip(), (tail_text or "").strip()
    if not head or not tail:
        return False
    return (is_kanji(head[-1]) and is_kanji(tail[0])) or (is_katakana(head[-1]) and is_katakana(tail[0]))


def may_split(head_text: str, tail_text: str) -> bool:
    """may_break() for a cut the length limits force. A pause the detector confirmed is evidence of
    a boundary even between two kanji (天気 … 電車); a length limit is none, so it avoids compounds."""
    return may_break(head_text, tail_text) and not joins_compound(head_text, tail_text)


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
        if may_split(word_text(head), word_text(tail)):
            return head, tail
    elif may_split(word_text(buf), next_word):
        return buf, []
    for j in range(len(buf) - 1, 0, -1):
        if may_split(word_text(buf[:j]), word_text(buf[j:])):
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


def ends_sentence(text: str) -> bool:
    """True when this text's last row ends in a sentence mark, and so closes what it says."""
    row = text.split("\n")[-1].rstrip()
    return bool(row) and row[-1] in SENTENCE_END


def finishes(cue: dict, nxt: Optional[dict], limits: CueLimits) -> bool:
    """True when `cue` ends what it says: a sentence mark, a pause of `pause_split` before the next
    cue (word to word, since a cue's end carries its lead-out), or nothing after it. A Whisper
    segment's end is no evidence: segments are the decoder's cut, and run flush into each other."""
    if nxt is None or ends_sentence(cue["text"]):
        return True
    end = cue["words"][-1]["end"] if cue.get("words") else cue["end"]
    start = nxt["words"][0]["start"] if nxt.get("words") else nxt["start"]
    return start - end >= limits.pause_split


def rows_fit(text: str, limits: CueLimits, at_mark: bool = False) -> bool:
    """Whether a seamed text is a cue a viewer can read. Never a third row, whatever put the seam there.

    The two seams differ on a short row. A seam the gap guessed is our break, so a row under
    MIN_PIECE_CHARS is a break we chose badly and the join goes flat instead. A seam at the speaker's
    own mark is theirs: うん。 is a whole turn, not a stub, and it reads as one on a row of its own.
    """
    rows = text.split("\n")
    if len(rows) > limits.max_lines:
        return False
    return at_mark or min(len(row) for row in rows) >= MIN_PIECE_CHARS


# --------------------------------------------------------------------------- word timings on cues
#
# A cue keeps the words it was built from as `words`: [{"start", "end", "text"}] on the video's
# clock. Their texts joined are the cue's text exactly, seam newlines included, so a client can
# light up the word being spoken without re-deriving anything. Optional on every cue: a cache
# record written before the field existed lacks it, and /sync sends it only to a client that asks.

def cue_words(group) -> list:
    """A group's words as cue word dicts whose texts join to word_text(group).

    word_text() strips the joined text, so the whitespace it drops is cut off the edge words, and a
    word left with no text at all is dropped rather than shipped empty.
    """
    texts = [w.word or "" for w in group]
    lo, hi = 0, len(texts)
    while lo < hi and not texts[lo].strip():
        lo += 1
    while hi > lo and not texts[hi - 1].strip():
        hi -= 1
    if lo == hi:
        return []
    texts[lo] = texts[lo].lstrip()
    texts[hi - 1] = texts[hi - 1].rstrip()
    return [{"start": w.start, "end": w.end, "text": t} for w, t in zip(group[lo:hi], texts[lo:hi])]


def join_words(prev: dict, seam: str, cue: dict) -> None:
    """Append `cue`'s words to `prev`'s after a merge joined their texts with `seam`.

    The seam goes on the end of the previous row's last word. A side without words (a cue from an
    old cache record) leaves the merged cue without any: a partial list would be a wrong one.
    """
    if not prev.get("words") or "words" not in cue:
        prev.pop("words", None)
        return
    # New lists and dicts: `prev` is a shallow copy, and its words are still the input cue's.
    head = [dict(w) for w in prev["words"]]
    head[-1]["text"] += seam
    prev["words"] = head + [dict(w) for w in cue["words"]]


def settle_words(cues) -> list:
    """Round every cue's word times as the cue times are, clamp them into the cue and keep them in
    order; drop the words of a cue whose text they no longer spell. Run after normalise_gaps()."""
    for cue in cues:
        ws = cue.get("words")
        if ws is None:
            continue
        if not ws or "".join(w["text"] for w in ws) != cue["text"]:
            del cue["words"]
            continue
        floor, hi = cue["start"], cue["end"]
        settled = []
        for w in ws:
            start = min(max(round(w["start"], 2), floor), hi)
            end = min(max(round(w["end"], 2), start), hi)
            settled.append({"start": start, "end": end, "text": w["text"]})
            floor = end
        cue["words"] = settled
    return cues


OPT_IN_CUE_FIELDS = ("words", "src")  # what /sync sends only to a body with "words": true


def without_words(cue: dict) -> dict:
    """The cue as /sync sends it to a client that did not ask for word timings: no `words`, and no
    `src` ("human" on a line from the creator's subtitles), so the answer is what it always was."""
    return {k: v for k, v in cue.items() if k not in OPT_IN_CUE_FIELDS} if any(
        k in cue for k in OPT_IN_CUE_FIELDS) else cue


def clean_cached_cue(cue: dict) -> Optional[dict]:
    """A cache record's cue cleaned as fresh words are (clean_model_text(), keeping the cue's own row
    break): the cue itself when there is nothing to clean, a cleaned copy, or None when nothing of
    it is left.

    A record written before the cleaning holds what the model wrote, and the cache is forever;
    bumping CACHE_FORMAT would throw away every cached transcript for three characters of one video.
    The text is stripped as word_text() strips a fresh one and its words cut to match by cue_words();
    words that no longer spell the text go, as settle_words() drops them.
    """
    text = cue.get("text")
    if not isinstance(text, str) or clean_model_text(text, keep_newline=True) == text:
        return cue
    out = dict(cue, text=clean_model_text(text, keep_newline=True).strip())
    if not out["text"]:
        return None
    if isinstance(cue.get("words"), list):
        texts = [(w, clean_model_text(w["text"], keep_newline=True)) for w in cue["words"]]
        out["words"] = cue_words([Word(t, w["start"], w["end"]) for w, t in texts if t])
        if not out["words"] or "".join(w["text"] for w in out["words"]) != out["text"]:
            del out["words"]
    return out


def merge_adjacent(cues, limits: CueLimits, max_gap: float, only_short: bool) -> list:
    """Fold neighbouring cues together while they stay inside the char and duration limits.

    A sentence mark is a hard row boundary: what the speaker finished and what comes after it never
    share a row, and when the second row would be too short to read, the merge is refused rather
    than flattened. Two people on one row (マジで?それいいね。) is worse than a two-character cue of
    its own, and Yomitan and match.js both cut a sentence at the newline. Pieces without a mark
    merge flat as they always did: that rule is the anti-flicker one, and it is not about sentences.
    """
    out: list = []
    for i, cue in enumerate(cues):
        if out:
            prev = out[-1]
            gap = cue["start"] - prev["end"]
            short = (prev["end"] - prev["start"] < limits.min_seconds
                     or cue["end"] - cue["start"] < limits.min_seconds)
            seam = "\n" if ends_sentence(prev["text"]) else ""
            text = prev["text"] + seam + cue["text"]
            if (gap <= max_gap and (short or not only_short)
                    and len(text) - text.count("\n") <= limits.max_chars
                    and cue["end"] - prev["start"] <= limits.max_seconds
                    and (seam != "\n" or (rows_fit(text, limits, at_mark=True)
                                          and finishes(cue, cues[i + 1] if i + 1 < len(cues) else None, limits)))):
                prev["end"] = cue["end"]
                prev["text"] = text
                join_words(prev, seam, cue)
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
    if ends_sentence(prev_text):
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
    for i, cue in enumerate(cues):
        if out:
            prev = out[-1]
            gap = cue["start"] - prev["end"]
            prev_row = prev["text"].split("\n")[-1]
            at_mark = ends_sentence(prev_row)
            # After a mark only a piece that itself ends what it says joins as a row. An unfinished
            # one opens the next sentence (ほんとは || 葉っぱを相手に…): riding on the row before, it
            # would leave its continuation to start a cue without it, on screen and on the card.
            if at_mark and not finishes(cue, cues[i + 1] if i + 1 < len(cues) else None, limits):
                out.append(dict(cue))
                continue
            # Nothing is split inside a word after a sentence mark: the speaker ended there. Without
            # this, a next cue opening on ー or a small kana made breaks_word() true and its flat
            # join put two sentences on one row (そうですね。ーっと言います).
            forced = not at_mark and breaks_word(prev_row, cue["text"].split("\n")[0])
            # A finished sentence is not a stub. そうなんですよね。 is eight characters and reads on
            # its own, so it must not buy the cross_reach budget a half-line is given.
            finished = at_mark and len(prev_row) >= MIN_PIECE_CHARS
            short = (len(cue["text"]) <= limits.reach_chars
                     or (not finished and len(prev["text"]) <= limits.reach_chars))
            seam = "" if forced else seam_for(prev["text"], gap, limits)
            text = prev["text"] + seam + cue["text"]
            if seam == "\n" and not rows_fit(text, limits, at_mark):
                # A seam the speaker's own mark put there is not negotiable: flattening it would
                # put two sentences, often two people, on one row. Refuse the merge instead - only
                # a third row can fail the check at a mark, and a third row has nowhere to go.
                if at_mark:
                    out.append(dict(cue))
                    continue
                # Elsewhere the line break is the first thing to give up. A row nobody can read, or
                # a third row, is worse than no break at all, and refusing the merge over one leaves
                # the stub alone on screen - which is how 言ってた ended up a four-character cue.
                seam = ""
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
                join_words(prev, seam, cue)
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


def carry_trailing_mark(words, kept) -> list:
    """Move a sentence mark off the words the trim dropped onto the one that now ends the cue.

    The word punctuate_words() marks is a segment's last, and that is the word Whisper stretches
    over the silence after the utterance - so its midpoint often lies outside speech and trim_words()
    drops it. The timings have to go, which is what P1.1 exists for; the mark does not, since it
    belongs to the sentence and not to that word. Trimming is left judging midpoints rather than
    keeping the word by its start: the P1.1 measurements are about cue in and out times, and a
    stretched last word kept for its mark would push every cue out by seconds.
    """
    if not kept or len(kept) == len(words):
        return kept
    end = next(i for i, w in enumerate(words) if w is kept[-1])
    mark = ""
    for w in words[end + 1:]:
        text = (w.word or "").rstrip()
        if text and text[-1] in SENTENCE_END:
            mark = text[-1]
    last = (kept[-1].word or "").rstrip()
    if mark and not (last and last[-1] in SENTENCE_END):
        kept[-1].word = (kept[-1].word or "") + mark
    return kept


def extend_end(cue: dict, ceiling: float, speech, limits: CueLimits) -> None:
    """Cue out a beat after the audio (P1.4) and grow a short cue into the silence after it (P1.5),
    never past `ceiling`, the next cue's start less the gap."""
    sil_start, sil_end = next_silence(cue["end"], speech)
    silence = sil_end - sil_start
    if silence >= limits.lead_out_silence:
        cue["end"] += min(limits.lead_out, silence - 0.1)
    if cue["end"] - cue["start"] < limits.min_seconds:
        cue["end"] = min(max(cue["end"], cue["start"] + limits.min_seconds), sil_end - limits.min_gap)
    cue["end"] = min(cue["end"], max(ceiling, cue["start"] + limits.hard_min_seconds))


def build_cues(words, speech, limits: CueLimits) -> list:
    """One Whisper segment's words -> display-ready cues [{start, end, text, words}] (P1 rules 1-7).

    `words` are not yet rounded or clamped into the cue: build_window_cues() settles them last."""
    words = carry_trailing_mark(words, trim_words(words, speech, limits.trim_slack))
    cues = []
    for group in group_words(words, speech, limits):
        start = group[0].start
        onset = nearest_onset(speech, start, limits.snap_reach)
        if onset is not None:  # P1.3: cue in at the speech onset, not at Whisper's first word
            start = min(max(start, onset - limits.lead_in), onset + 0.30)
        cues.append({"start": start, "end": max(group[-1].end, start + 0.01), "text": word_text(group),
                     "words": cue_words(group)})

    for i, cue in enumerate(cues):
        extend_end(cue, cues[i + 1]["start"] - limits.min_gap if i + 1 < len(cues) else float("inf"), speech, limits)

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


def gate_segment(seg, offset: float, speech, lyrics: bool = False) -> tuple:
    """(the segment's words on the video's clock, the gate that rejects it or None).

    The one place a segment is judged, for build_window_cues() and for the prompt-skip check.
    """
    # Before the gates, not after: an unrepaired first word makes the segment's span cover a
    # silence it never contained, and the VAD gate then deletes real speech (20 utterances in
    # 17 minutes of the sample, キズナアイでーす and はじめまして! among them) while the anomaly
    # gate scores its six-second "word" straight past the threshold.
    words = repair_lead_words(absolute_words(seg, offset), speech)
    reason = lyrics_reason(seg, words) if lyrics else hallucination_reason(seg, words, speech)
    return words, reason


def build_window_cues(segs, offset: float, speech, limits: CueLimits, seg_id: int, drops=None,
                      window_end: Optional[float] = None, lyrics: bool = False) -> tuple:
    """Gate hallucinated segments, build their cues and stamp each with its segment id.

    `seg` ties every cue back to the Whisper segment it came from, which is a run of speech and
    not a sentence: mining reads the cue alone (see sentenceForCue in content.js). Kept for the
    cache tools, which measure a change per segment. Every kept segment is punctuated before any
    cues are built (punctuate_words), since the pause behind its last word is the gap to the next
    segment's first. Returns (cues, next segment id). A lyrics
    window was transcribed without the detector: its segments go through lyrics_reason instead,
    and `speech` is what lyrics_spans() made of them (their word runs, padded like the detector's
    intervals and starting past a stranded head, so that the lead repair below slides it onto its
    line as it does on the talk path).
    """
    out: list = []
    kept: list = []
    for seg in segs:
        words, reason = gate_segment(seg, offset, speech, lyrics)
        if reason:
            if drops is not None:
                drops[reason] = drops.get(reason, 0) + 1
                drops.setdefault("_text", []).append((reason, (getattr(seg, "text", "") or "").strip()))
            continue
        kept.append((seg, words))
    # The pause behind a segment's last word, and what follows it, are in the next kept segment, so
    # every segment is repaired before any is punctuated. Not on a lyrics window: its speech is the
    # padded word runs lyrics_spans() made, so every breath inside a sung line reads as a pause, and
    # the thresholds here were measured on talk.
    for i, (_, words) in enumerate(kept):
        following = kept[i + 1][1] if i + 1 < len(kept) else []
        if not lyrics:
            punctuate_words(words, speech, following[0] if following else None, limits)
    for seg, words in kept:
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
    return settle_words(normalise_gaps(out, limits)), seg_id


# The prompt sometimes makes Whisper open a window with a timestamp seconds past its first speech,
# and whatever it jumped is never decoded: 6 to 23 s lost about once per 12 minutes of speech. An
# unprompted decode of the same audio starts on time, so a skip is filled from one.
PROMPT_SKIP_MIN_S = 3.0      # seconds of detected speech no kept segment reaches before a retry
PROMPT_SKIP_SLACK = 0.5      # how near a kept segment must come to count as having heard a stretch
PROMPT_ALIGN_MIN_CHARS = 2   # a shorter run of characters both decodes share is coincidence, not the same words


def stretched_over_speech(words, speech, min_s: float = PROMPT_SKIP_MIN_S) -> bool:
    """True when one word spans `min_s` seconds of detected speech: no word is that long, so the
    decoder jumped. A skip can hide inside a segment the gates keep, and this is how it shows:
    a prompted first window decoded as one line, 言い返す!, whose 返 runs over
    thirteen seconds of speech it never decoded. A last word stretched over the silence after an
    utterance, which Whisper writes often, spans no speech and is not this."""
    return any(speech_seconds(speech, w.start, w.end) >= min_s for w in words)


def kept_lines(segs, offset: float, speech) -> list:
    """[(segment, its words on the video's clock), ...] of every talk segment that hears what it
    spans: the gates keep it, as build_window_cues() judges, and no word of it is stretched over
    speech."""
    lines = []
    for seg in segs:
        words, reason = gate_segment(seg, offset, speech)
        if reason is None and not stretched_over_speech(words, speech):
            lines.append((seg, words))
    return lines


def kept_spans(segs, offset: float, speech) -> list:
    """[start, end] on the video's clock of every line kept_lines() keeps."""
    return [[words[0].start, words[-1].end] for _, words in kept_lines(segs, offset, speech)]


def skipped_speech(kept, speech, min_s: float = PROMPT_SKIP_MIN_S, slack: float = PROMPT_SKIP_SLACK) -> list:
    """[[start, end, seconds], ...]: the stretches of `speech` no span of `kept` comes within `slack` of.

    Consecutive unreached pieces with no kept span between them are one stretch, since a skip runs
    across the pauses of what it skipped; a stretch holding under `min_s` seconds of speech is
    left alone (a breath, a laugh, a line the gates rightly dropped).
    """
    reach = merge_intervals([[a - slack, b + slack] for a, b in kept])
    groups: list = []
    for a, b in subtract_intervals(speech, reach):
        if groups and not any(x < a and y > groups[-1][1] for x, y in reach):
            groups[-1][1] = b
            groups[-1][2] += b - a
        else:
            groups.append([a, b, b - a])
    return [g for g in groups if g[2] >= min_s]


def unheard_runs(lines, other) -> list:
    """[(segment, its words, [word index, ...]), ...]: the runs of words in `other`'s lines that have no
    counterpart in `lines`' text. Both are kept_lines() of two decodes of the same audio.

    The decodes disagree on word timings by more than any overlap test can allow (one put というこ|と
    a few hundredths of a second before the other's ということで、 began, and a head
    repair_lead_words() moved sits seconds from where the other decode put it), but they agree on
    the words. So the transcripts are aligned as text, spaces and punctuation aside, and a word is
    heard when any of its characters falls in a stretch of PROMPT_ALIGN_MIN_CHARS or more that both
    share. A word with no characters of its own (a lone mark) goes with the run it sits in.
    """
    def chars(lines_):
        return [(ch, li, wi) for li, (_, words) in enumerate(lines_) for wi, w in enumerate(words)
                for ch in w.word or "" if ch not in PUNCTUATION and not ch.isspace()]

    mine, theirs = chars(lines), chars(other)
    matcher = difflib.SequenceMatcher(None, [c for c, _, _ in mine], [c for c, _, _ in theirs], autojunk=False)
    heard = {(li, wi) for i, j, n in matcher.get_matching_blocks() if n >= PROMPT_ALIGN_MIN_CHARS
             for _, li, wi in theirs[j:j + n]}
    spelled = {(li, wi) for _, li, wi in theirs}
    runs = []
    for li, (seg, words) in enumerate(other):
        run: list = []
        for wi in range(len(words) + 1):
            if wi < len(words) and (li, wi) not in heard:
                run.append(wi)
            else:
                if any((li, w) in spelled for w in run):
                    runs.append((seg, words, run))
                run = []
    return runs


def splice_segments(segs, retry, offset: float, speech, stretches) -> tuple:
    """(`segs` with the pieces of `retry` that fill a skipped stretch, in time order; how many were added).

    A piece is a run of the unprompted decode's words that the prompted one never said
    (unheard_runs()) and whose midpoint lies inside one of `stretches`: a copy of its segment holding
    only those words, its text their join. A segment of `segs` stretched over speech that an added
    piece overlaps goes: it would put its one-character cue across the real lines.
    """
    added = []
    for seg, words, run in unheard_runs(kept_lines(segs, offset, speech), kept_lines(retry, offset, speech)):
        if not any(x <= (words[run[0]].start + words[run[-1]].end) / 2 <= y for x, y, _ in stretches):
            continue
        piece = copy.copy(seg)
        # `run` counts the words absolute_words() kept, not seg.words: a word left empty is not among them.
        own = [w for w, _ in cleaned_words(seg)]
        piece.words = [own[i] for i in run]
        piece.text = "".join(w.word or "" for w in piece.words)
        piece.start, piece.end = float(piece.words[0].start), float(piece.words[-1].end)
        added.append(piece)
    if not added:
        return list(segs), 0
    filled = [[offset + float(seg.start), offset + float(seg.end)] for seg in added]
    broken = [seg for seg in segs
              if stretched_over_speech(gate_segment(seg, offset, speech)[0], speech)
              and any(min(b, offset + float(seg.end)) > max(a, offset + float(seg.start)) for a, b in filled)]
    kept_segs = [seg for seg in segs if not any(seg is x for x in broken)]
    return sorted(kept_segs + added, key=lambda seg: float(seg.start)), len(added)


def retry_prompt_skips(model, audio, options: dict, segs, offset: float, speech) -> tuple:
    """(segments, seconds skipped, segments added) of a talk window decoded with `options`.

    When the prompted decode left PROMPT_SKIP_MIN_S or more of detected speech unreached, the
    window is decoded once more without the prompt, everything else the same, and the segments
    that fill the skip are spliced in. Seconds skipped is 0.0 when no retry ran. Transcriber.process()
    and dump_words.py both call this, so the A/B rig decodes what the server does.
    """
    if not options.get("initial_prompt"):
        return list(segs), 0.0, 0
    stretches = skipped_speech(kept_spans(segs, offset, speech), speech)
    if not stretches:
        return list(segs), 0.0, 0
    retry, _info = model.transcribe(audio, **dict(options, initial_prompt=None))
    spliced, added = splice_segments(segs, list(retry), offset, speech, stretches)
    return spliced, sum(g[2] for g in stretches), added


# --------------------------------------------------------------------------- human subtitles

# A track the video's creator uploaded (yt-dlp's `subtitles`) is better text than Whisper's: the
# names, the kanji and the sentence ends a person chose. YouTube's own recogniser
# (`automatic_captions`, "ja-orig" among them) never counts, punctuated or not: it makes errors
# Whisper does not (homophones) and cuts its lines at a fixed length, mid-word. A person's timing
# is loose by a few hundred milliseconds, so a cue takes its text and its line from the track and
# its times from the Whisper words that text aligns with. docs/dev/cue-building.md, "Human
# subtitles", has the rules and the measurements.
HUMAN_SUBS_LANGUAGES = frozenset({"ja"})       # measured on Japanese, and only there
HUMAN_SUBS_FORMATS = ("json3", "srv3", "vtt")  # json3 and srv3 time in milliseconds; vtt is the fallback
HUMAN_SUBS_MAX_BYTES = 4 * 1024 * 1024
HUMAN_SUBS_WORD_TIMED = 0.5   # a track with more of its lines timed word by word than this is ASR output
HUMAN_SUBS_UNMARKED = 0.95    # ... and so is one whose rows carry no punctuation and no space at all
HUMAN_SUBS_MIN_ROWS = 20      # a short track is too little to judge the second rule on
HUMAN_MIN_SIMILARITY = 0.6    # a line aligning worse than this with what Whisper heard keeps Whisper's text
HUMAN_TIME_SLACK = 1.5        # seconds around a line inside which a Whisper character may align with it
HUMAN_DISPLACED_S = 15.0      # how far a line on its own times looks for Whisper having said it elsewhere
HUMAN_CUT_LEAD = 0.3          # seconds of silence a window cut ahead of a line's speech leaves before it
HUMAN_SLOWEST_CHARS_S = 4.0   # a line holding more text than its span could take at this rate reaches on past it
HUMAN_MIN_LEFTOVER = MIN_PIECE_CHARS  # Whisper's words left beside an aligned line: fewer characters are its fringe

SUBTITLE_SOUND_WORDS = ("笑", "拍手", "音楽", "bgm", "歓声", "効果音", "ため息", "咳", "泣", "se", "music",
                        "applause", "laugh", "鳴き声", "チャイム", "物音", "沈黙", "無音", "雑音", "ノイズ", "♪")
SUBTITLE_TURN_RE = re.compile(r"^(?:>>|＞＞|≫|»|[-－]\s)\s*")
SUBTITLE_SQUARE_RE = re.compile(r"[\[［][^\]］\n]{0,30}[\]］]")
SUBTITLE_MUSIC_RE = re.compile(r"[♪♫♬♩\U0001F3B5\U0001F3B6]")
SUBTITLE_PAREN_RE = re.compile(r"[（(]([^（）()\n]{1,30})[）)]")
SUBTITLE_LABEL_RE = re.compile(r"^【[^】\n]{1,20}】")
SUBTITLE_NAME_RE = re.compile(r"^[^\s：:、。，,！？!?「」『』（）()0-9０-９]{1,10}[：:](?=\s*\S)")
SUBTITLE_SPACE_RE = re.compile(r"[ \t　]+")
SUBTITLE_MARKS = set("。、，,．.！？!?…「」『』“”\"・")
VTT_TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
VTT_INLINE_TIME_RE = re.compile(r"<(?:\d+:)?\d{1,2}:\d{2}[.,]\d{3}>")
MARKUP_RE = re.compile(r"<[^>\n]*>")


def human_subtitles_on(args) -> bool:
    """--human-subtitles auto and a language the rules were measured on. A namespace without the
    switch (the tools', the tests') reads as off, so nothing there reaches the network."""
    return getattr(args, "human_subtitles", "off") == "auto" and getattr(args, "language", "") in HUMAN_SUBS_LANGUAGES


def asr_format(fmt: dict) -> bool:
    """A track YouTube itself labels as speech recognition, wherever yt-dlp filed it."""
    query = parse_qs(urlsplit(str(fmt.get("url") or "")).query)
    name = str(fmt.get("name") or "").lower()
    return "asr" in query.get("kind", []) or "auto-generated" in name or "自動生成" in name


def pick_human_track(subtitles, language: str) -> Optional[tuple]:
    """(key, format dict) of the creator's track for `language`, or None.

    `ja` before `ja-JP` and any other regional tag; never `ja-orig`, which is YouTube's own
    recognition of the original audio. Only ever handed info["subtitles"], never
    info["automatic_captions"].
    """
    if not isinstance(subtitles, dict):
        return None
    keys = sorted((k for k in subtitles if isinstance(k, str)
                   and (k.lower() == language or k.lower().startswith(language + "-"))
                   and not k.lower().endswith("-orig")), key=lambda k: (k.lower() != language, k))
    for key in keys:
        formats = [f for f in subtitles.get(key) or [] if isinstance(f, dict) and isinstance(f.get("url"), str)]
        if any(asr_format(f) for f in formats):
            continue
        for ext in HUMAN_SUBS_FORMATS:
            for fmt in formats:
                if fmt.get("ext") == ext:
                    return key, fmt
    return None


def parse_json3(raw: str) -> tuple:
    """YouTube's json3 -> ([(start, end, text)], how many lines are timed word by word)."""
    data = json.loads(raw)
    lines, timed = [], 0
    for ev in (data.get("events") if isinstance(data, dict) else None) or []:
        if not isinstance(ev, dict):
            continue
        segs = [g for g in ev.get("segs") or [] if isinstance(g, dict)]
        text = "".join(str(g.get("utf8") or "") for g in segs)
        if not text.strip():
            continue
        start = float(ev.get("tStartMs") or 0) / 1000.0
        if any("tOffsetMs" in g for g in segs[1:]) or any("acAsrConf" in g for g in segs):
            timed += 1
        lines.append((start, start + float(ev.get("dDurationMs") or 0) / 1000.0, text))
    return lines, timed


def parse_srv3(raw: str) -> tuple:
    """YouTube's srv3 (timed text XML) -> ([(start, end, text)], lines timed word by word)."""
    import xml.etree.ElementTree as ET

    lines, timed = [], 0
    for p in ET.fromstring(raw).iter("p"):
        parts, words = [p.text or ""], 0
        for child in p:
            parts.append("\n" if child.tag == "br" else "".join(child.itertext()))
            words += child.tag == "s" and (child.get("t") is not None or child.get("ac") is not None)
            parts.append(child.tail or "")
        text = "".join(parts)
        if not text.strip():
            continue
        start = int(p.get("t") or 0) / 1000.0
        timed += words > 1
        lines.append((start, start + int(p.get("d") or 0) / 1000.0, text))
    return lines, timed


def vtt_seconds(text: str) -> Optional[float]:
    m = VTT_TIME_RE.match(text.strip())
    if m is None:
        return None
    hours, minutes, seconds, millis = m.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_vtt(raw: str) -> tuple:
    """WebVTT -> ([(start, end, text)], cues timed word by word, YouTube's <00:00:01.234> tags)."""
    lines, timed = [], 0
    for block in re.split(r"\n[ \t]*\n", raw.replace("\r\n", "\n").replace("\r", "\n")):
        rows = block.split("\n")
        for i, row in enumerate(rows):
            if "-->" not in row:
                continue
            head, _, tail = row.partition("-->")
            start, end = vtt_seconds(head), vtt_seconds(tail)
            body = "\n".join(rows[i + 1:]).strip("\n")
            if start is not None and end is not None and body.strip():
                timed += bool(VTT_INLINE_TIME_RE.search(body))
                lines.append((start, end, html.unescape(MARKUP_RE.sub("", body))))
            break
    return lines, timed


def is_sound_tag(inner: str) -> bool:
    """（拍手）, [音楽], (laughs): a bracketed sound, not speech. A Latin tag counts as a whole word."""
    low = inner.lower()
    return len(inner) <= 12 and any(re.search(rf"\b{w}", low) if w.isascii() else w in low
                                    for w in SUBTITLE_SOUND_WORDS)


def is_ruby(head: str, inner: str) -> bool:
    """（よみ） straight after kanji, all hiragana and no longer than such a reading runs: furigana."""
    run = 0
    for ch in reversed(head):
        if not is_kanji(ch):
            break
        run += 1
    return run > 0 and all(is_hiragana(c) for c in inner) and len(inner) <= 4 * run


def clean_subtitle_row(row: str) -> str:
    """One row of a person's subtitle without what is not speech: a speaker label (（田中）, 田中：,
    【田中】, a >> or - turn mark), a sound tag ([笑], （拍手）, ♪), furigana in brackets after its
    kanji; whitespace runs squeezed to one."""
    row = SUBTITLE_TURN_RE.sub("", row.strip())
    row = SUBTITLE_MUSIC_RE.sub("", SUBTITLE_SQUARE_RE.sub("", row)).strip()
    row = SUBTITLE_LABEL_RE.sub("", row).strip()
    out, pos = [], 0
    for m in SUBTITLE_PAREN_RE.finditer(row):
        inner, head = m.group(1).strip(), row[:m.start()]
        # At the row's start it names the speaker, or is a stage note when nothing follows it.
        drop = is_sound_tag(inner) or not head.strip() or is_ruby(head, inner)
        out.append(row[pos:m.start()] if drop else row[pos:m.end()])
        pos = m.end()
    row = ("".join(out) + row[pos:]).strip()
    row = SUBTITLE_NAME_RE.sub("", row).strip()
    return SUBTITLE_SPACE_RE.sub(lambda m: "　" if "　" in m.group(0) else " ", row)


def clean_subtitle_text(text: str) -> str:
    """A track line as a cue's text: model-text cleaning (controls, private use, format characters),
    each row through clean_subtitle_row(), empty rows gone. The track's own line breaks stay row
    breaks: they are where the person who made it put them, often a turn between two speakers."""
    rows = (clean_subtitle_row(r) for r in clean_model_text(text, keep_newline=True).split("\n"))
    return "\n".join(r for r in rows if r)


def unmarked_track(lines) -> bool:
    """Rows without one punctuation mark or space anywhere: YouTube's recogniser's text uploaded as
    a track, cut at a fixed length mid-word. A person marks a sentence somehow, with 。 or a space."""
    rows = [r for _, _, t in lines for r in t.split("\n") if r.strip()]
    if len(rows) < HUMAN_SUBS_MIN_ROWS:
        return False
    bare = sum(1 for r in rows if not (set(r) & SUBTITLE_MARKS) and " " not in r and "　" not in r)
    return bare >= HUMAN_SUBS_UNMARKED * len(rows)


def human_subtitle_lines(raw: bytes, ext: str) -> Optional[list]:
    """A downloaded track as [[start, end, text]], cleaned and in time order, or None when it is
    empty or reads as speech recognition (lines timed word by word, or no row marked)."""
    parser = {"json3": parse_json3, "srv3": parse_srv3, "vtt": parse_vtt}.get(ext)
    if parser is None:
        return None
    lines, timed = parser(raw.decode("utf-8-sig", errors="replace"))
    if not lines or timed > HUMAN_SUBS_WORD_TIMED * len(lines) or unmarked_track(lines):
        return None
    out: list = []
    for start, end, text in sorted(lines, key=lambda line: (line[0], line[1])):
        text = clean_subtitle_text(text)
        if not text or end <= start:
            continue
        if out and out[-1][2] == text and start <= out[-1][1] + 0.1:
            out[-1][1] = round(max(out[-1][1], end), 2)  # one line the track split in two
            continue
        out.append([round(start, 2), round(end, 2), text])
    return out or None


def human_lines_from_record(value) -> tuple:
    """(track, lines) out of a cache record's "human_subs", or (None, []) when it was never asked.

    The track is a signature string, "" when the video was asked and has none to use."""
    if not isinstance(value, dict):
        return None, []
    track = value.get("track")
    lines = []
    for line in value.get("lines") or []:
        if (isinstance(line, list) and len(line) == 3 and all(isinstance(x, (int, float)) for x in line[:2])
                and isinstance(line[2], str) and line[2] and line[1] > line[0]):
            lines.append([float(line[0]), float(line[1]), line[2]])
    return (track if isinstance(track, str) and track else ""), (lines if track else [])


# --------------------------------------------------------------------------- aligning a track to Whisper

def fold_chars(text: str) -> tuple:
    """(folded characters, the index in `text` each came from): NFKC, lower case, katakana as
    hiragana, and nothing but letters and digits, ー and 〜 included, since a person and Whisper
    disagree on those far more often than on the words."""
    chars, src = [], []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if "ァ" <= c <= "ヶ":
                c = chr(ord(c) - 0x60)
            if unicodedata.category(c)[0] in "LN" and c not in "ー〜～":
                chars.append(c)
                src.append(i)
    return chars, src


def whisper_chars(cues) -> list:
    """Every folded character of the cues' words: (char, start, end, cue index, word index), its
    time a share of its word's. A cue without words counts as one word over its whole span."""
    out = []
    for ci, cue in enumerate(cues):
        words = cue.get("words") or [{"start": cue["start"], "end": cue["end"], "text": cue["text"]}]
        for wi, w in enumerate(words):
            chars, _ = fold_chars(w["text"])
            step = max(0.0, w["end"] - w["start"]) / max(len(chars), 1)
            for k, c in enumerate(chars):
                out.append((c, w["start"] + k * step, w["start"] + (k + 1) * step, ci, wi))
    return out


def align_line(line, region) -> tuple:
    """Edit-distance alignment of one line's folded characters to a stretch of Whisper's, the
    stretch's ends free (semi-global). Returns (pairs, matches, first, last): pairs[k] is the
    region index line character k sits on, or -1 for one Whisper never wrote; first and last bound
    the Whisper characters the line spans."""
    n, m = len(line), len(region)
    prev = [0] * (m + 1)  # a line may start anywhere in the stretch
    back = []
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ops = bytearray(m + 1)
        ops[0] = 1
        ch = line[i - 1]
        for j in range(1, m + 1):
            best, op = prev[j - 1] + (ch != region[j - 1]), 0
            if prev[j] + 1 < best:
                best, op = prev[j] + 1, 1
            if cur[j - 1] + 1 < best:
                best, op = cur[j - 1] + 1, 2
            cur[j] = best
            ops[j] = op
        back.append(ops)
        prev = cur
    j = min(range(m + 1), key=lambda k: (prev[k], k))  # ... and end anywhere
    last = j
    pairs, matches, i = [-1] * n, 0, n
    while i > 0:
        op = back[i - 1][j] if j > 0 else 1
        if op == 0:
            pairs[i - 1] = j - 1
            matches += line[i - 1] == region[j - 1]
            i, j = i - 1, j - 1
        elif op == 1:
            i -= 1
        else:
            j -= 1
    return pairs, matches, j, last


def line_similarity(line, region, pairs, matches: int, spanned: int) -> float:
    """How well a line agrees with the Whisper text it aligned with, 0 to 1: twice the matching
    characters over both lengths (difflib's ratio), with half credit for a kanji written in kana on
    the other side or the reverse (優しい and やさしい). That half credit alone stays at 0.5 at most,
    under HUMAN_MIN_SIMILARITY: a line has to share real characters to pass."""
    spelled = sum(1 for k, j in enumerate(pairs) if j >= 0 and line[k] != region[j]
                  and (is_kanji(line[k]) and is_hiragana(region[j]) or is_hiragana(line[k]) and is_kanji(region[j])))
    return (2 * matches + spelled) / max(len(line) + spanned, 1)


def speech_end_before(speech, t0: float, t1: float) -> float:
    """t1 pulled back to the end of the speech it follows when it lies in silence after t0: the
    last word Whisper writes is stretched over the pause after the utterance."""
    for a, b in reversed(speech):
        if a < t1:
            return b if t0 < b < t1 else t1
    return t1


def char_class(ch: str) -> str:
    if is_kanji(ch):
        return "k"
    if is_hiragana(ch):
        return "h"
    if is_katakana(ch):
        return "K"
    return "a" if ch.isalnum() else ""


def human_words(text: str, times: list) -> list:
    """The line's text as tokens, runs of one script with what follows them attached, each timed
    from its characters' times. Joined, they are the text exactly."""
    tokens: list = []
    for i, ch in enumerate(text):
        cls = char_class(ch)
        if tokens and (not cls or cls == tokens[-1][3] or ch in NO_LINE_START):
            tokens[-1][0] += ch
            tokens[-1][2] = times[i][1]
        else:
            tokens.append([ch, times[i][0], times[i][1], cls or "p"])
    return [{"start": round(a, 3), "end": round(b, 3), "text": t} for t, a, b, _ in tokens]


def aligned_cue(line, src, pairs, chars, speech, limits: CueLimits) -> dict:
    """A cue holding a line's text, timed from the Whisper characters it aligned with."""
    _, _, text = line
    known: dict = {}
    for k, j in enumerate(pairs):
        if j >= 0:
            a, b = chars[j][1], chars[j][2]
            prev = known.get(src[k])
            known[src[k]] = (min(a, prev[0]), max(b, prev[1])) if prev else (a, b)
    t0 = min(a for a, _ in known.values())
    t1 = max(b for _, b in known.values())
    # Characters Whisper never wrote share the gap between the neighbours it did write.
    times, i = [], 0
    while i < len(text):
        if i in known:
            times.append(known[i])
            i += 1
            continue
        k = i
        while k < len(text) and k not in known:
            k += 1
        lo = times[-1][1] if times else t0
        hi = known[k][0] if k < len(text) else (times[-1][1] if times else t1)
        hi = max(hi, lo)
        step = (hi - lo) / (k - i)
        times += [(lo + (n - i) * step, lo + (n - i + 1) * step) for n in range(i, k)]
        i = k
    start = t0
    onset = nearest_onset(speech, t0, limits.snap_reach)
    if onset is not None:  # cue in at the speech onset, as build_cues() does
        start = min(max(t0, onset - limits.lead_in), onset + 0.30)
    end = max(speech_end_before(speech, t0, t1), start + 0.01)
    return {"start": start, "end": end, "text": text, "words": human_words(text, times), "src": "human"}


def leftover_cue(cue: dict, run: list, first: bool, last: bool) -> Optional[dict]:
    """The words of a Whisper cue no aligned line claimed, as a cue of their own, or None when they
    are only the fringe of the line beside them."""
    words = [Word(w["text"], w["start"], w["end"]) for w in run]
    text = word_text(words)
    if len(fold_chars(text)[0]) < HUMAN_MIN_LEFTOVER:
        return None
    out = {"start": cue["start"] if first else run[0]["start"], "end": cue["end"] if last else run[-1]["end"],
           "text": text, "words": cue_words(words)}
    if "seg" in cue:
        out["seg"] = cue["seg"]
    return out


def echoes(cue: dict, line, slack: float = HUMAN_TIME_SLACK) -> bool:
    """Whether a Whisper cue says what a line says, within `slack` of the line's time: the edge of a
    line whose speech runs across the window's end, which the neighbouring window shows, or the
    speech of a line the person timed seconds away from it."""
    start, _, text = line
    if not (start - slack <= cue["end"] and cue["start"] <= line_reach(line) + slack):
        return False
    heard, region = fold_chars(cue["text"])[0], fold_chars(text)[0]
    if not heard or not region:
        return False
    pairs, matches, first, last = align_line(heard, region)
    return line_similarity(heard, region, pairs, matches, last - first) >= HUMAN_MIN_SIMILARITY


def split_long_line(line, limits: CueLimits) -> list:
    """A line too long for the screen (over max_lines rows of max_chars) in pieces at its own
    sentence marks, each as long as fits, its share of the line's reach by characters. Text and
    marks stay the person's; only where one cue ends and the next begins is chosen here."""
    start, end, text = line
    if len(text) <= limits.max_chars * limits.max_lines:
        return [line]
    sentences = [m.group(0) for m in re.finditer(r"[^。！？!?]+[。！？!?]*", text)]
    pieces: list = []
    for sentence in sentences:
        if pieces and len(pieces[-1].lstrip()) + len(sentence) <= limits.max_chars * limits.max_lines:
            pieces[-1] += sentence
        else:
            pieces.append(sentence)
    pieces = [p.strip() for p in pieces if p.strip()]
    if len(pieces) < 2:
        return [line]
    # Shared over line_reach(), not the line's own span: a line this long is the one whose times
    # were too short for it (twenty seconds of speech written over three).
    out, at, total = [], start, sum(len(p) for p in pieces)
    reach = line_reach(line)
    for piece in pieces:
        span = (reach - start) * len(piece) / total
        out.append([round(at, 2), round(at + span, 2), piece])
        at += span
    return out


def assist_cues(cues, lines, speech, limits: CueLimits, seg_id: int, window_end: Optional[float] = None,
                stats: Optional[dict] = None, neighbours=()) -> tuple:
    """Whisper's cues with a person's subtitle lines put in where they agree with what Whisper heard.

    Each line, in time order, is aligned with the Whisper characters within HUMAN_TIME_SLACK of it
    and after the last line placed (align_line()). Agreeing at HUMAN_MIN_SIMILARITY or better, it
    becomes a cue with its own text, timed from those characters and snapped to the speech as a
    Whisper cue is, its words the line's tokens; the Whisper words it claimed go, and what a
    Whisper cue has left beside it stays as a cue of its own. A line over audio Whisper wrote
    nothing for (music, a stretch the detector missed, a segment the gates refused) keeps the
    track's own times and has no words. A line that disagrees is dropped and Whisper's text kept.
    Every line cue carries "src": "human" and a segment id of its own. Returns (cues, next id);
    `neighbours` are the lines either side that other windows own: a Whisper cue that echoes()
    one is the edge of that line's speech, shown there, and goes ("echoes" in `stats`).
    `stats` counts "aligned", "track" and "kept_whisper" lines, and lists each aligned line's
    similarity, text and the Whisper text it spans under "_similarity" (for replay_cues.py).
    """
    stats = stats if stats is not None else {}
    lines = [piece for line in lines for piece in split_long_line(line, limits)]
    chars = whisper_chars(cues)
    mids, top = [], float("-inf")
    for c in chars:
        top = max(top, (c[1] + c[2]) / 2)
        mids.append(top)
    claimed = [False] * len(chars)
    placed, aligned, floor = [], [], 0
    for line in lines:
        start, end, text = line
        folded, src = fold_chars(text)
        if not folded:
            continue
        # A person's times can be too short for the text (three questions in three seconds): the
        # stretch searched reaches as far as the text could take to say.
        reach = line_reach(line)
        lo = max(floor, bisect.bisect_left(mids, start - HUMAN_TIME_SLACK))
        hi = bisect.bisect_right(mids, reach + HUMAN_TIME_SLACK)
        heard = range(bisect.bisect_left(mids, start), bisect.bisect_right(mids, end))
        if not any(start <= (chars[j][1] + chars[j][2]) / 2 <= end for j in heard):
            placed.append({"start": start, "end": end, "text": text, "src": "human", "_line": line})
            continue
        if lo >= hi:  # what Whisper heard there an earlier line has claimed
            stats["kept_whisper"] = stats.get("kept_whisper", 0) + 1
            continue
        pairs, matches, first, last = align_line(folded, [c[0] for c in chars[lo:hi]])
        similarity = line_similarity(folded, [c[0] for c in chars[lo:hi]], pairs, matches, last - first)
        stats.setdefault("_similarity", []).append(
            (round(similarity, 3), text, "".join(c[0] for c in chars[lo + first:lo + last])))
        if similarity < HUMAN_MIN_SIMILARITY:
            stats["kept_whisper"] = stats.get("kept_whisper", 0) + 1
            continue
        for j in range(lo + first, lo + last):
            claimed[j] = True
        floor = lo + last
        placed.append(aligned_cue(line, src, [lo + j if j >= 0 else -1 for j in pairs], chars, speech, limits))
        aligned.append((placed[-1], start, end))
        stats["aligned"] = stats.get("aligned", 0) + 1
    # What Whisper heard inside an aligned line's own span that no line claimed is what its maker
    # chose to cover with that line: a sentence said twice and written once, a filler. The line's
    # cue stretches over it instead of Whisper's text standing beside it in another spelling.
    for cue, start, end in aligned:
        inside = [j for j in range(bisect.bisect_left(mids, start), bisect.bisect_right(mids, end))
                  if not claimed[j] and start <= (chars[j][1] + chars[j][2]) / 2 <= end]
        if not inside:
            continue
        for j in inside:
            claimed[j] = True
        first, last = chars[inside[0]][1], chars[inside[-1]][2]
        if first < cue["start"]:
            onset = nearest_onset(speech, first, limits.snap_reach)
            cue["start"] = min(max(first, onset - limits.lead_in), onset + 0.30) if onset is not None else first
        cue["end"] = max(cue["end"], speech_end_before(speech, first, last))

    # A Whisper word goes with the line that claimed most of its characters.
    taken: dict = {}
    for (_, _, _, ci, wi), done in zip(chars, claimed):
        votes = taken.setdefault((ci, wi), [0, 0])
        votes[done] += 1
    kept = []
    for ci, cue in enumerate(cues):
        words = cue.get("words") or [{"start": cue["start"], "end": cue["end"], "text": cue["text"]}]
        gone, state = [], False
        for wi in range(len(words)):
            votes = taken.get((ci, wi))
            state = votes[1] > votes[0] if votes else state  # punctuation goes with the word before it
            gone.append(state)
        if not any(gone):
            kept.append(cue)
            continue
        if all(gone) or "words" not in cue:
            continue
        i = 0
        while i < len(words):
            k = i
            while k < len(words) and gone[k] == gone[i]:
                k += 1
            if not gone[i]:
                piece = leftover_cue(cue, words[i:k], i == 0, k == len(words))
                if piece is not None:
                    kept.append(piece)
            i = k

    # A line on its own times goes where Whisper wrote this same text a few seconds off (the
    # person's timing slipped) or wrote anything over it: Whisper heard that speech, so it was not
    # music or a stretch the detector missed.
    kept_lines = []
    for h in placed:
        line = h.pop("_line", None)
        if line is not None:
            if any(cue_overlaps(c, h) or echoes(c, line, HUMAN_DISPLACED_S) for c in kept):
                stats["kept_whisper"] = stats.get("kept_whisper", 0) + 1
                continue
            stats["track"] = stats.get("track", 0) + 1
        kept_lines.append(h)
    placed = kept_lines

    # Where a line's cue and Whisper's still meet, the line wins: a Whisper cue mostly under one
    # goes, one at its edge gives way up to it.
    out = []
    for cue in kept:
        for h in placed:
            if cue is None or cue["end"] <= h["start"] or cue["start"] >= h["end"]:
                continue
            if cue_overlaps(cue, h):
                cue = None
            elif cue["start"] < h["start"]:
                cue = dict(cue, end=h["start"] - limits.min_gap)
            else:
                cue = dict(cue, start=h["end"] + limits.min_gap)
            if cue is not None and cue["end"] - cue["start"] < limits.hard_min_seconds:
                cue = None
        if cue is not None and any(echoes(cue, line) for line in neighbours):
            stats["echoes"] = stats.get("echoes", 0) + 1
            cue = None
        if cue is not None:
            out.append(cue)
    for h in placed:
        h["seg"] = seg_id
        seg_id += 1
    out = sorted(out + placed, key=lambda c: c["start"])
    for i, cue in enumerate(out):
        if "words" in cue and cue.get("src") == "human":
            extend_end(cue, out[i + 1]["start"] - limits.min_gap if i + 1 < len(out) else float("inf"), speech, limits)
    if window_end is not None:
        out = [c for c in out if c["start"] < window_end - 0.05]
        for cue in out:
            cue["end"] = min(cue["end"], max(window_end, cue["start"] + limits.hard_min_seconds))
    for cue in out:
        cue["start"] = round(cue["start"], 2)
        cue["end"] = round(max(cue["end"], cue["start"] + 0.05), 2)
    return settle_words(normalise_gaps(out, limits)), seg_id


def line_reach(line) -> float:
    """Where a line's speech may run to: its own end, or as far as its text could take to say."""
    start, end, text = line
    return max(end, start + len(fold_chars(text)[0]) / HUMAN_SLOWEST_CHARS_S)


def human_window_end(lines, speech, start: float, end: float, window: float) -> float:
    """Where a window ends so that no line of the track runs across its end (from HUMAN_TIME_SLACK
    before its start, where its speech may begin, to line_reach()): the latest onset of detected
    speech in the window's last two thirds that no line spans (from HUMAN_TIME_SLACK past its start
    to HUMAN_TIME_SLACK short of line_reach(), a person's times being that loose). The
    next window starts there, after a silence, and aligns the line whole, as a segment cut at a
    window's end is transcribed again. An onset and not the line's own start: a line often starts
    after the speech it holds, and a cut at the line left its first words in this window, a Whisper
    cue beside the line. With no such onset the end stays, and lines_in_window() gives the line
    that straddles it to the window holding most of it."""
    if not any(a - HUMAN_TIME_SLACK < end < line_reach((a, b, t)) for a, b, t in lines):
        return end  # no line's speech, which may start that much before the line, runs across the end
    spans = [(a + HUMAN_TIME_SLACK, line_reach((a, b, t)) - HUMAN_TIME_SLACK) for a, b, t in lines]
    for i in range(len(speech) - 1, 0, -1):
        onset = speech[i][0]
        if start + window / 3 <= onset < end and not any(a < onset < b for a, b in spans):
            # Inside the silence before it: a cue snapped a lead-in ahead of the onset would
            # otherwise still open before this window's end, and stay in this window.
            return onset - min(HUMAN_CUT_LEAD, (onset - speech[i - 1][1]) / 2)
    return end


def neighbour_lines(lines, start: float, end: float) -> list:
    """The last line before the window and the first after it, by lines_in_window()'s rule."""
    before = [line for line in lines if (line[0] + line_reach(line)) / 2 < start]
    after = [line for line in lines if (line[0] + line_reach(line)) / 2 >= end]
    return before[-1:] + after[:1]


def lines_in_window(lines, start: float, end: float) -> list:
    """The track lines a window owns: those whose middle, to line_reach(), lies inside it."""
    return [line for line in lines if start <= (line[0] + line_reach(line)) / 2 < end]


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
    # What YouTube states the default audio track is (declared_language()), or None when it states
    # nothing. Equal to --language means the audio detector never has to run for this video.
    declared_language: Optional[str] = None
    # Windows a paused session has only listened to. Kept apart from `covered` and never written
    # to the cache: when the language comes back they are forgotten, so a wrong pause costs a
    # second listen instead of leaving the video permanently blank.
    probed: list = field(default_factory=list)
    seg_next: int = 0  # next Whisper-segment id; cues of one segment share it (see build_window_cues)
    want_t: float = 0.0
    last_sync: float = field(default_factory=time.time)
    busy: Optional[list] = None
    fetching: bool = False
    error_at: float = 0.0  # time.time() of the last failed fetch
    # The automatic retry of a failed fetch (Fetcher.record_failure): failures since the last
    # success, when the next attempt is due (0: none scheduled, --retry-after after error_at
    # applies, as for a live follower's error), and whether the failure is one no retry can cure.
    fail_count: int = 0
    retry_at: float = 0.0
    permanent: bool = False
    # What Transcriber.choose_path() knows of the video beyond one window, kept in memory, never
    # cached: the loudest speech a talk window measured (a whole-song window's loudness floor, 0.0
    # until one has), and the audio the detector heard nothing in but the head heard the target
    # language in, which a stretch carrying on from it past a window's end inherits.
    talk_rms: float = 0.0
    sung: list = field(default_factory=list)
    # The creator's own subtitle track (human_subtitle_lines()): its signature, "" when the video
    # was asked and has none to use, None while it has not been asked; its cleaned lines; and
    # whether this session has asked, so a failed request is not repeated on every sync.
    human_track: Optional[str] = None
    human_lines: list = field(default_factory=list)
    human_asked: bool = False
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
    def __init__(self):
        self.warned = set()

    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg, only_once=False):
        # yt-dlp's cookie readers (youtube_cookies()) call this directly, with only_once for what
        # would repeat for every cookie they cannot decrypt; a download wraps it and passes msg alone.
        if only_once:
            if msg in self.warned:
                return
            self.warned.add(msg)
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


COOKIES_FILE_NOTE = "the cookies file (--cookies)"
# The Docker image never takes a browser from config.json (resolve_default_cookies()): an exported file is its way.
DOCKER_COOKIES_FILE = "--cookies /data/cookies.txt"


def save_cookies_command(prefix=None, windows=None) -> str:
    """The command that makes Firefox's YouTube cookies the default of every start on this install.

    run.cmd / run.sh start setup's venv, which a Nix install has not (run.sh refuses there): its
    Python is the Nix store's (sys.prefix; a venv's is the venv's own folder, wherever its Python
    came from), and `nix run . --` hands the option to server.py. Each text names only its own
    install's command, which also keeps the viewer's line within the overlay's 160 characters.
    """
    prefix = sys.prefix if prefix is None else prefix
    windows = os.name == "nt" if windows is None else windows
    if prefix.startswith("/nix/store/"):
        launcher = "nix run . --"
    else:
        launcher = "run.cmd" if windows else "run.sh"
    return f"{launcher} --save-cookies-from-browser firefox"


def friendly_error(exc: Exception, cookies: str = "") -> str:
    """One line for the viewer, at most 160 characters (the overlay's STATUS_ERROR_MAX_CHARS cuts the
    rest). `cookies` names what the download sent (cookies_note()), "" for none: a sign-in wall then
    asks for cookies the way this install takes them (save_cookies_command(), or Docker's file), and
    for a signed-in browser (or a fresh cookies file) once they are sent."""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        if cookies == COOKIES_FILE_NOTE:
            # Signing in anywhere leaves an exported file as it was; every download reads it anew.
            return (f"YouTube asks for a sign-in although the server sends {cookies}: export a fresh one "
                    "while signed in to YouTube, then play the video again")
        if cookies:
            return f"YouTube asks for a sign-in although the server sends {cookies}: sign in to YouTube there, then play the video again"
        if in_container():
            return ("YouTube asks for a sign-in. Export a cookies.txt from a browser signed in to YouTube into the data folder "
                    f"and add {DOCKER_COOKIES_FILE}")
        return f"YouTube asks for a sign-in. Run {save_cookies_command()} once and start the server again"
    if "private video" in low:
        return "This video is private"
    if "members-only" in low or "join this channel" in low:
        if cookies:
            return f"Members-only video, and the account behind {cookies} is not a member"
        if in_container():
            return f"Members-only video. Export a cookies.txt from a member's browser into the data folder and add {DOCKER_COOKIES_FILE}"
        return f"Members-only video. Run {save_cookies_command()} once with a member signed in there and start the server again"
    if "javascript runtime" in low:
        return "yt-dlp needs Node.js or Deno installed to download from YouTube"
    if "video unavailable" in low:
        return "Video unavailable"
    if "live stream" in low:
        return msg
    last = msg.strip().splitlines()[-1] if msg.strip() else msg
    return last[:200]


# How a failed fetch is retried (fetch_error_kind(), retry_delay()). The phrases are the ones
# friendly_error() turns into words for the viewer, plus yt-dlp's own for a gone video.
FETCH_BACKOFF_BASE = 2.0          # seconds before the first retry; doubles per failure up to --retry-after
FETCH_BACKOFF_RATE_LIMITED = 8.0  # the same after YouTube said "too many requests": back off harder
FETCH_BACKOFF_JITTER = 0.2        # each wait is cut by up to this share, so clients never retry in step
RATE_LIMIT_PHRASES = ("http error 429", "too many requests", "try again later", "rate-limit", "rate limit")
# Nothing changes these by waiting. A sign-in wall is among them only while no cookies are sent:
# with cookies, signing in to YouTube in that browser cures it, and every download reads them anew.
PERMANENT_PHRASES = (
    # yt-dlp passes YouTube's own reason through: "Video unavailable" and "This video is unavailable"
    # (an id that never existed) both occur.
    "private video", "video is private", "members-only", "join this channel", "video unavailable",
    "video is unavailable", "video is not available", "has been removed",
    "no longer available", "account associated with this video has been terminated",
    "this video does not exist", "not available in your country", "not made this video available",
    "incomplete youtube id", "unsupported url", "is not a valid url", "javascript runtime",
    "live event will begin", "premieres in",
)
SIGN_IN_PHRASES = ("sign in to confirm", "not a bot", "age-restricted", "inappropriate for some users")


def fetch_error_kind(exc: BaseException, cookies: str = "") -> str:
    """"permanent", "rate_limited" or "transient" for a failed audio fetch.

    Transient is the default: HTTP 403 (an expired or refused format URL, the incident this rule
    came from), 5xx, timeouts, connection resets, "unable to download video data", fragment errors,
    and anything unrecognised, since the server retried every failure before and an unknown message
    is likelier a hiccup than a verdict. Rate limiting is tested first: YouTube's "This content
    isn't available, try again later" also reads "Video unavailable". `cookies` is cookies_note().
    """
    low = (str(exc) or exc.__class__.__name__).lower()
    if any(p in low for p in RATE_LIMIT_PHRASES):
        return "rate_limited"
    if any(p in low for p in SIGN_IN_PHRASES):
        return "transient" if cookies else "permanent"
    if any(p in low for p in PERMANENT_PHRASES):
        return "permanent"
    return "transient"


def retry_delay(failures: int, kind: str, cap: float, jitter: float) -> float:
    """Seconds before retry number `failures` (1 for the first): 2, 4, 8, 16 ... (8, 16 ... when rate
    limited) capped at `cap` (--retry-after), each cut by up to FETCH_BACKOFF_JITTER; `jitter` is
    a draw from [0, 1)."""
    base = FETCH_BACKOFF_RATE_LIMITED if kind == "rate_limited" else FETCH_BACKOFF_BASE
    delay = min(cap, base * 2 ** min(max(failures, 1) - 1, 16))
    return max(0.0, delay * (1.0 - FETCH_BACKOFF_JITTER * jitter))


def ytdlp_retry_sleep(n: int) -> float:
    """yt-dlp's wait before its own retry `n` of a request or fragment: 0.5, 1, 2, 4 seconds."""
    return min(0.5 * 2 ** max(n, 0), 4.0)


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


class ForeignLanguage(Exception):
    """YouTube says this video is not the subtitle language, so nothing of it is fetched."""

    def __init__(self, language: str):
        super().__init__(language)
        self.language = language


def declared_language(info) -> Optional[str]:
    """The language YouTube states for a video's default audio track, as a bare code, or None.

    yt-dlp passes the uploader's declaration through as `language`; it is a plain two-letter code
    or a tagged one (en-US). Only the primary subtag is compared, since the target is a Whisper
    language code. Nothing else in the metadata is worth asking: `automatic_captions` lists some
    157 languages for every video, because YouTube offers to machine-translate its own transcript
    into all of them, so the keys say nothing about what was spoken.
    """
    value = info.get("language") if hasattr(info, "get") else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().replace("_", "-").split("-")[0].lower() or None


def store_human_track(s: Session, track: str, lines: list) -> None:
    """What a Fetcher without an App does with the creator's track: keep it on the session."""
    with s.lock:
        s.human_track, s.human_lines = track, lines


class Fetcher:
    def __init__(self, args, clock=time.time, sleep=time.sleep, rng=random.random):
        self.args = args
        self.clock = clock
        self.sleep = sleep
        self.rng = rng
        # Called with (session, track, lines) once the creator's subtitles are known; App puts its
        # own in, which also rebuilds cues made before the track arrived.
        self.on_human_subtitles = None

    def cookies_note(self) -> str:
        """What a download sends YouTube for a sign-in, in words for friendly_error(); "" for nothing."""
        if self.args.cookies_from_browser:
            return f"{self.args.cookies_from_browser}'s YouTube cookies"
        if self.args.cookies:
            return COOKIES_FILE_NOTE
        return ""

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
            # yt-dlp's own retries cover 5xx, timeouts and resets within one request; a 403 or 429 it
            # does not retry, which download_once() and Fetcher.fetch() take care of.
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "retry_sleep_functions": {"http": ytdlp_retry_sleep, "fragment": ytdlp_retry_sleep,
                                      "extractor": ytdlp_retry_sleep},
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
        """Fetch the audio, retrying a transient failure with exponential backoff while it is watched.

        The retry is the server's own, not the next /sync's: this thread keeps `fetching` set while
        it waits, so no sync starts a second one. It stops waiting once no client has synced the
        video for --client-timeout (watched()); the next /sync after `retry_at` then starts over.
        """
        try:
            while not self.fetch_once(s):
                with s.lock:
                    delay = None if s.permanent or not s.retry_at else s.retry_at - self.clock()
                if delay is None or not self.watched(s):
                    return
                self.sleep(max(0.0, delay))
                with s.lock:
                    if s.status != "error":
                        return
                if not self.watched(s):
                    return
                log.info("[%s] retrying the audio fetch (attempt %d)", s.video_id, s.fail_count + 1)
        finally:
            with s.lock:
                s.fetching = False

    def watched(self, s: Session) -> bool:
        """Whether a client synced this video recently enough to be worth a retry on its behalf."""
        window = float(getattr(self.args, "client_timeout", 30.0) or 0.0)
        if window <= 0:
            window = float(getattr(self.args, "idle_minutes", 30) or 30) * 60.0
        with s.lock:
            return self.clock() - s.last_sync <= window

    def succeeded(self, s: Session) -> None:
        """Forget earlier failures. Call with s.lock held."""
        s.fail_count, s.retry_at, s.permanent = 0, 0.0, False

    def record_failure(self, s: Session, exc: BaseException) -> None:
        """Set the session's error and schedule the next attempt, or none for a permanent error."""
        cookies = self.cookies_note()
        kind = fetch_error_kind(exc, cookies)
        cap = float(getattr(self.args, "retry_after", 30.0))
        with s.lock:
            s.status = "error"
            s.error = friendly_error(exc, cookies)
            s.error_at = self.clock()
            s.preview = None  # a preview from a partial download must not outlive the failure
            s.fail_count += 1
            s.permanent = kind == "permanent"
            delay = 0.0 if s.permanent else retry_delay(s.fail_count, kind, cap, self.rng())
            s.retry_at = 0.0 if s.permanent else s.error_at + delay
        if s.permanent:
            log.error("[%s] fetching audio failed, not retried: %s", s.video_id, exc)
        else:
            log.error("[%s] fetching audio failed (%s, failure %d), retrying in %.1f s: %s",
                      s.video_id, kind.replace("_", " "), s.fail_count, delay, exc)

    def fetch_once(self, s: Session) -> bool:
        """One attempt. True when it is over (audio ready, language refused, live stream followed),
        False when it failed and record_failure() has said what happens next."""
        try:
            with s.lock:
                s.status = "downloading"
                s.error = None
            path = find_cached_audio(s.video_id)
            if path is None:
                log.info("[%s] downloading audio", s.video_id)
                try:
                    path = self.download(s)
                except ForeignLanguage as foreign:
                    # The same two fields the audio detector sets, so the overlay says the same
                    # thing; ready with no cues, because there is nothing more to wait for.
                    with s.lock:
                        s.heard = foreign.language
                        s.language_paused = True
                        s.duration = s.duration_hint
                        s.status = "ready"
                        self.succeeded(s)
                    log.info("[%s] YouTube calls this video %s, not %s; nothing is transcribed%s",
                             s.video_id, foreign.language, self.args.language,
                             f": {s.title}" if s.title else "")
                    return True
                if path is None:
                    with s.lock:
                        self.succeeded(s)
                    # The follower handles its own errors and leaves an error for get_session().
                    self.follow_live(s)  # returns when the stream ends or nobody watches any more
                    return True
            else:
                log.info("[%s] using cached audio %s", s.video_id, path.name)
                self.start_human_check(s)
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
                self.succeeded(s)
            log.info("[%s] audio ready, %s long%s", s.video_id, fmt_time(s.duration), f": {s.title}" if s.title else "")
            return True
        except Exception as exc:  # noqa: BLE001
            self.record_failure(s, exc)
            return False

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
            language = declared_language(info)
            with s.lock:
                s.title = info.get("title") or ""
                s.duration_hint = float(hint) if isinstance(hint, (int, float)) else 0.0
                s.declared_language = language
            # Before a single byte: a video YouTube itself calls another language would otherwise
            # cost the whole download and decode, and sixty seconds of forced Japanese out of
            # English speech, before the audio detector's patience ran out.
            if language is not None and language != self.args.language and self.args.language_patience > 0:
                raise ForeignLanguage(language)
            if info.get("is_live"):
                return None
            # A copy: process_ie_result() goes on filling `info` on this thread.
            self.start_human_check(s, {k: copy.deepcopy(info.get(k)) for k in ("id", "subtitles", "language")})
            try:
                ydl.process_ie_result(info, download=True)
            except Exception as exc:  # noqa: BLE001
                # A 403 on the media request is most often a format URL that went stale or a player
                # client YouTube refused this once: download() extracts the info anew, so the second
                # try gets fresh URLs. Waiting cures neither a permanent error nor a rate limit, and
                # a second request straight away would only feed the latter.
                if find_cached_audio(s.video_id) is None:
                    if fetch_error_kind(exc, self.cookies_note()) != "transient":
                        raise
                    log.info("[%s] the download failed (%s); trying once more with fresh format URLs",
                             s.video_id, exc)
                    ydl.download([url])
        path = find_cached_audio(s.video_id)
        if path is None:
            raise RuntimeError("yt-dlp finished but no audio file was produced")
        return path

    def start_human_check(self, s: Session, info=None) -> None:
        """Ask for the creator's subtitles on a thread of their own, once per session: the audio
        never waits for them, and a video that has none, or a request that fails, costs nothing
        but the request. `info` is the metadata download_once() already holds; without it the
        thread asks YouTube for it, one metadata request."""
        if not human_subtitles_on(self.args):
            return
        with s.lock:
            if s.live or s.human_track is not None or s.human_asked:
                return
            s.human_asked = True
        threading.Thread(target=self.check_human_subtitles, args=(s, info), daemon=True,
                         name=f"subs-{s.video_id}").start()

    def check_human_subtitles(self, s: Session, info=None) -> None:
        try:
            import yt_dlp

            with yt_dlp.YoutubeDL(self.ytdlp_options(s.video_id)) as ydl:
                if info is None:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={s.video_id}", download=False,
                                            process=False)
                track, lines = self.read_human_track(ydl, info)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] the creator's subtitles could not be read (%s); Whisper alone", s.video_id, exc)
            return
        if track:
            log.info("[%s] the creator's subtitles (%s): %d lines", s.video_id, track.split(":")[0], len(lines))
        (self.on_human_subtitles or store_human_track)(s, track, lines)

    def read_human_track(self, ydl, info) -> tuple:
        """(signature, lines) of the creator's track for --language, ("", []) for none to use.

        A video YouTube names another language gets none: a Japanese track on it is a
        translation, not what is said. A live stream has no tracks while it is live.
        """
        if info.get("is_live") or declared_language(info) not in (None, self.args.language):
            return "", []
        picked = pick_human_track(info.get("subtitles"), self.args.language)
        if picked is None:
            return "", []
        key, fmt = picked
        resp = ydl.urlopen(fmt["url"])
        try:
            raw = resp.read(HUMAN_SUBS_MAX_BYTES + 1)
        finally:
            resp.close()
        if len(raw) > HUMAN_SUBS_MAX_BYTES:
            raise ValueError(f"the track is over {HUMAN_SUBS_MAX_BYTES} bytes")
        lines = human_subtitle_lines(raw, fmt["ext"])
        if lines is None:
            log.info("[%s] the uploaded %s track reads as speech recognition; not used", info.get("id") or "?", key)
            return "", []
        return f"{key}.{fmt['ext']}:{hashlib.sha1(raw).hexdigest()[:12]}", lines

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


def loudness_floor(level: float) -> float:
    """What is not silence next to speech measuring `level` rms: half of it, held between
    LYRICS_QUIET_RMS and LYRICS_MIN_RMS. An ordinary video's talk measures 0.04 and up, so its
    floor is LYRICS_MIN_RMS exactly; a quietly mixed one's measures 0.016. No level known (0.0)
    is LYRICS_MIN_RMS too."""
    return max(LYRICS_QUIET_RMS, min(LYRICS_MIN_RMS, level / 2)) if level > 0 else LYRICS_MIN_RMS


def wants_lyrics(args, audio, speech, start: float, end: float, floor: float = LYRICS_MIN_RMS) -> bool:
    """Whether a window is transcribed without the detector: --lyrics auto, next to no speech heard,
    louder than `floor` (loudness_floor() of the video's talk)."""
    if getattr(args, "lyrics", "auto") != "auto":
        return False
    return speech_seconds(speech, start, end) < LYRICS_MAX_SPEECH_S and rms(audio) >= floor


def transcribe_options(args, model, lyrics: bool, detector: bool = True) -> dict:
    """The options of a window's model.transcribe() call: Transcriber.process() and dump_words.py
    both take them from here, so the A/B rig decodes what the server does.

    A Kitsune model reads the VAD options alone and gets no prompt (its decoder prompt is fixed,
    `takes_prompt`), so retry_prompt_skips() never decodes a window twice for it. `detector` False
    is a talk window holding speech the detector missed (Transcriber.choose_path()): decoded whole,
    prompt and all.
    """
    options = dict(
        language=args.language,
        task="transcribe",
        beam_size=args.beam_size,
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=(args.initial_prompt or None) if getattr(model, "takes_prompt", True) else None,
        temperature=[0.0, 0.2, 0.4, 0.6],
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
        hallucination_silence_threshold=2.0,
    )
    if lyrics:
        # No prompt on this path. The lyrics gates were measured on unprompted decodes, and the
        # blocklist holds no sentence of the prompt, so a noisy window the language head lets
        # through could echo the prompt itself into the cache with nothing to catch it.
        options.update(vad_filter=False, initial_prompt=None)
    elif detector:
        options.update(vad_filter=True, vad_parameters=vad_parameters())
    else:
        options.update(vad_filter=False)
    return options


def unsung_stretches(audio, start: float, end: float, speech,
                     min_seconds: float = LYRICS_MIN_STRETCH_S, level: Optional[float] = None) -> list:
    """The parts of a talk window, at least `min_seconds` long, that the detector heard nothing in
    and that are loud next to the speech it did hear: a song beside the line announcing it, or a
    shriek Silero takes for noise. `audio` is the window's samples from `start`.

    Loud is loudness_floor() of the window's own speech (`level`, measured here when not given):
    an ordinary video keeps the absolute floor, a quietly mixed one, whose talk measures 0.016 and
    whose missed lines 0.019, gets half its talk. Only a window holding LYRICS_MAX_SPEECH_S of
    speech gets here, so its own is enough to measure.
    """
    if level is None:
        level = rms(speech_samples(audio, speech, start, end - start))
    floor = loudness_floor(level)
    out = []
    for a, b in unheard_stretches([[start, end]], speech, [], min_seconds):
        if rms(audio[int((a - start) * SAMPLE_RATE):int((b - start) * SAMPLE_RATE)]) >= floor:
            out.append([a, b])
    return out


# A record's "unheard_rule": its covered ranges were made by a server that puts a talk window's loud
# unheard stretches to the head (unsung_stretches()). App.old_record_sweep() rechecks one without it.
UNHEARD_RULE = 2
RECHECK_CONTEXT_S = 4.0  # talk on either side of a rechecked stretch: what its window would hold


def unheard_ranges(covered, speech, cues) -> list:
    """[(start, end, speech)]: every unheard stretch of a record of LYRICS_MIN_STRETCH_S or more,
    widened by RECHECK_CONTEXT_S into the talk around it and clipped to its covered range, with the
    speech heard inside, so Transcriber.choose_path() judges it as it would a window holding it.
    Alone, a quiet stretch would be a window with no speech, held to the absolute LYRICS_MIN_RMS
    until some talk was measured, which a quietly mixed video's missed line (0.019) does not reach."""
    covered, out = merge_intervals(covered), []
    for a, b in unheard_stretches(covered, speech, cues, LYRICS_MIN_STRETCH_S):
        x, y = next((x, y) for x, y in covered if x <= a and b <= y)
        lo, hi = max(x, a - RECHECK_CONTEXT_S), min(y, b + RECHECK_CONTEXT_S)
        out.append((lo, hi, [[max(p, lo), min(q, hi)] for p, q in speech if min(q, hi) > max(p, lo)]))
    return out


def vouched_segments(segs, offset: float, speech, stretches, drops=None) -> list:
    """`segs` less every segment lying mostly inside `stretches` that lyrics_reason() refuses.

    A rescued stretch counts as speech, which would excuse anything the decoder wrote there, a
    sign-off over the music included; the detector heard nothing in it, so the decoder's own
    confidence judges it, as on the lyrics path. dump_words.py records what this keeps. A refusal
    is counted in `drops` as build_window_cues() counts its own, so the log names the gate.
    """
    out = []
    for seg in segs:
        words, reason = gate_segment(seg, offset, speech, lyrics=True)
        inside = bool(words) and (2 * interval_overlap(words[0].start, words[-1].end, stretches)
                                  > words[-1].end - words[0].start)
        if not (reason and inside):
            out.append(seg)
        elif drops is not None:
            drops[reason] = drops.get(reason, 0) + 1
            drops.setdefault("_text", []).append((reason, (getattr(seg, "text", "") or "").strip()))
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


# What a failed decode says when the GPU itself is gone (a driver reset, its memory exhausted): CUDA's
# libraries, and ROCm's for the AMD engine, whose CTranslate2 build mostly still says "CUDA failed".
# "hip error" must start a word, or "relationship error" would count.
GPU_BROKEN_RE = re.compile(r"cuda|cudnn|cublas|hipblas|rocblas|hiprand|\bhip ?error|hsa_status|memory access fault",
                           re.IGNORECASE)


def gpu_context_broken(message) -> bool:
    """Whether a failed transcription means a broken GPU context, which only a restart of the process mends."""
    return GPU_BROKEN_RE.search(str(message)) is not None


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
                    if next(self.app.sweep, False) is False:  # idle: one step of the sweep, else rest
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

    def choose_path(self, s: Session, audio, speech, start: float, end: float) -> tuple:
        """(lyrics, rescued) for a window: whether it takes the lyrics path, and the loud stretches
        the detector missed in a talk window that the head hears the target language in, almost
        always none. process() and dump_words.py both decide here.

        Singing over music is no speech to Silero, so a window it heard next to nothing in, whose
        audio is not silence, is decoded without the detector and gated on Whisper's own confidence
        (lyrics_reason), once the head has heard the target language in it: loud noise and a
        foreign song stay with the detector, which decodes nothing of them. A talk window can hide
        the same thing beside its speech (a song after the line announcing it, a shriek): its
        unsung_stretches() go to the head together, and if it hears the target language the window
        is decoded whole with them counted as speech, so a line the detector caught late reaches
        the decoder in one piece. Neither verdict is a vote: a foreign song never pauses. A model
        whose gates this path has no figures for (Kitsune) never takes either.

        A window is a cut the planner made, not a boundary in the audio, so both judgements read
        what the session already knows of the video rather than the window alone. A whole-song
        window has no speech to measure its loudness against: its floor comes from the loudest
        talk heard so far (`s.talk_rms`). And the verdict belongs to the audio, not the window: a
        stretch starting inside audio the head already vouched for (`s.sung`), a song carrying on
        past the window before, needs no verdict. Asked again on its tail alone, the head hears too
        little to be sure (a song's 12 s tail scored 0.62, under LANGUAGE_MIN_PROB, and two lines
        were lost). The other stretches are judged as before, all of them together.
        """
        args = self.app.args
        if getattr(args, "lyrics", "auto") != "auto" or not getattr(self.app.model, "sings", True):
            return False, []
        if speech_seconds(speech, start, end) < LYRICS_MAX_SPEECH_S:
            lyrics = (wants_lyrics(args, audio, speech, start, end, loudness_floor(s.talk_rms))
                      and self.sung_in_target(s, audio, start, end))
            if lyrics:
                s.sung = merge_intervals(s.sung + [[start, end]])
            return lyrics, []
        level = rms(speech_samples(audio, speech, start, end - start))
        s.talk_rms = max(s.talk_rms, level)
        stretches = unsung_stretches(audio, start, end, speech, level=level)
        rescued = [st for st in stretches if find_covering(s.sung, st[0])]
        if rescued:
            log.info("[%s] %s-%s: carries on the target language heard before it; not asked again",
                     s.video_id, fmt_time(rescued[0][0]), fmt_time(rescued[-1][1]))
        if len(rescued) < len(stretches) and self.sung_in_target(
                s, speech_samples(audio, stretches, start, end - start), stretches[0][0], stretches[-1][1]):
            rescued = stretches
        s.sung = merge_intervals(s.sung + rescued)
        return False, rescued

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

        # Detection costs an encoder pass of its own, about a second a window on large-v3 and the
        # same on turbo, whose smaller decoder does not shrink the encoder. Three things make it
        # pointless: --language-patience 0, a model without a language head (Kitsune is Japanese
        # only), and a video YouTube has already named, where declared_language() agreed with
        # --language before the audio was fetched and a video that disagreed was never fetched at
        # all. So it runs only where it is still the only evidence there is.
        wanted = True
        if (float(getattr(args, "language_patience", 0.0) or 0.0) > 0
                and getattr(self.app.model, "detects_language", True)):
            if s.declared_language != args.language:
                try:
                    wanted = self.watch_language(s, audio, speech, start, end)
                except Exception:  # noqa: BLE001
                    # Between setting s.busy and the transcribe call nothing may raise: the window
                    # would be replanned for ever with busy stuck on it.
                    log.exception("[%s] language watch failed; transcribing the window", s.video_id)
            elif s.language_paused:
                # YouTube names this video the target language, so a pause the detector left
                # behind has nothing behind it any more: a cache written before the metadata was
                # read, or before this version. language_vote() is the only other thing that
                # lifts a pause and it no longer runs here, so without this the video stays
                # blank for ever.
                with s.lock:
                    s.language_paused, s.heard, s.foreign_seconds, s.probed = False, None, 0.0, []
                log.info("[%s] YouTube calls this video %s; lifting the pause left in its cache",
                         s.video_id, s.declared_language)
                self.app.save_cache(s)
        if not wanted:
            with s.lock:
                # Heard, not written. This goes to `probed`, not `covered`: the planner moves on,
                # but nothing claims Whisper has seen this audio, and nothing reaches the cache.
                s.probed = merge_intervals(s.probed + [[start, end]])
                s.busy = None
                heard = s.heard
            log.info("[%s] %s-%s: skipped (heard %s, paused)", s.video_id, fmt_time(start), fmt_time(end), heard)
            return

        lyrics, rescued = self.choose_path(s, audio, speech, start, end)
        options = transcribe_options(args, self.app.model, lyrics, detector=not rescued)
        speech = merge_intervals(speech + rescued) if rescued else speech
        drops: dict = {}
        try:
            segments, _info = self.app.model.transcribe(audio, **options)
            segs = list(segments)
            skipped, spliced = 0.0, 0
            if not lyrics:
                segs, skipped, spliced = retry_prompt_skips(self.app.model, audio, options, segs, start, speech)
            if rescued:
                segs = vouched_segments(segs, start, speech, rescued, drops)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] transcription of %s-%s failed: %s", s.video_id, fmt_time(start), fmt_time(end), exc)
            if gpu_context_broken(exc):
                log.error("The GPU context looks broken (driver reset or out of memory). Exiting so the launcher can restart the server.")
                hard_exit(3)
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

        if boundary_free and self.app.human_lines(s):
            # No line of the creator's track straddles the window's end either: it is aligned whole
            # in the next window, which starts on it.
            new_end = human_window_end(self.app.human_lines(s), speech, start, new_end,
                                       float(getattr(args, "window", 30.0)))

        limits = cue_limits(args)
        if lyrics:
            # No intervals to build on: the lines that pass the gates are the window's speech.
            speech = lyrics_spans(segs, start, limits)
        with s.lock:
            seg_id = s.seg_next
        fresh, seg_id = build_window_cues(segs, start, speech, limits, seg_id, drops, new_end, lyrics)
        # A vouched stretch running to the window's end that the decode wrote nothing in goes back
        # to the planner as a cut segment does: the next window starts there, carries the verdict
        # and hears it from its start, with what follows. One inside the window stays covered: it
        # had a whole window of context, and a window of a few seconds is where Whisper invents.
        inside = [[a, min(b, new_end)] for a, b in rescued if a < new_end]
        tail = [a for a, b in unheard_stretches(inside, [], fresh, LYRICS_MIN_STRETCH_S) if b >= new_end - 0.05]
        given_back = new_end - tail[0] if boundary_free and tail and tail[0] > start else 0.0
        new_end -= given_back
        # Read again: the track may have come in while the window was decoded.
        track = self.app.human_lines(s)
        owned = lines_in_window(track, start, new_end)
        human: dict = {}
        if track:
            fresh, seg_id = assist_cues(fresh, owned, speech, limits, seg_id, new_end, human,
                                        neighbour_lines(track, start, new_end))

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
            s.covered = merge_intervals(s.covered + [[start, new_end]])
            s.speech = merge_intervals(
                s.speech + [[max(a, start), min(b, new_end)] for a, b in speech if min(b, new_end) > max(a, start)])
            s.busy = None
        elapsed = time.time() - t0
        gated = ", ".join(f"{k}:{v}" for k, v in sorted(drops.items()) if not k.startswith("_"))
        log.info(
            "[%s] %s-%s: %d cues in %.1fs (%.0fx realtime)%s%s%s%s%s%s",
            s.video_id, fmt_time(start), fmt_time(new_end), added, elapsed,
            (new_end - start) / max(elapsed, 1e-3), " [lyrics]" if lyrics else "",
            f" [dropped {gated}]" if gated else "",
            f" [{sum(b - a for a, b in rescued):.0f} s the detector missed, decoded whole]" if rescued else "",
            f" [its last {given_back:.0f} s left to the next window]" if given_back else "",
            f" [{skipped:.0f} s skipped with the prompt, {spliced} segments from a decode without it]"
            if skipped else "",
            f" [creator's subtitles: {human.get('aligned', 0)} lines aligned, {human.get('track', 0)} on their own"
            f" times, {human.get('kept_whisper', 0)} left to Whisper"
            f"{', %d echoes of the next window dropped' % human['echoes'] if human.get('echoes') else ''}]"
            if owned or human.get("echoes") else "",
        )
        self.app.save_cache(s)


# --------------------------------------------------------------------------- application

class App:
    def __init__(self, args, model, device: str, compute_type: str, model_name: Optional[str] = None,
                 sweep: bool = False):
        self.args = args
        self.model = model
        self.device = device
        self.compute_type = compute_type
        # Which model is loaded, which one the client last asked for, and how the last switch went.
        # Only the transcriber thread replaces self.model (switch_model_if_wanted, between windows),
        # so the HTTP threads never touch a model that is being freed or loaded. Names are kept in
        # their canonical form (canonical_model_name) so that an alias and its repo id compare equal.
        # `model_name` is a model other than --model that this start loaded: the one a switch
        # restarted the server into (restart_for_model()).
        self.default_model = canonical_model_name(getattr(args, "model", None))
        self.model_name = model_name or self.default_model
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
        self.fetcher.on_human_subtitles = self.apply_human_track
        self.last_evict = time.time()
        self.sweep = self.old_record_sweep() if sweep else iter(())  # the server's own; tools and tests sweep nothing
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
                "engine": "kitsune" if getattr(self.model, "family", None) else "whisper",
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
        server exits with code 3 so the launcher restarts it with the default. With the AMD engine
        on Windows the swap is a restart instead (restart_for_model()), refused before its download
        when nothing would start the server again (restart_blocker()).
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
                blocked = self.restart_blocker()
                if blocked is not None:
                    # Refused before the download: its files would be fetched for a switch that cannot happen.
                    log.error("Staying with the model '%s' instead of '%s': %s", previous, wanted, blocked)
                    self.model_error = (wanted, blocked)
                    self.model_failed_at = time.time()
                    self.wanted_model = previous  # nothing retries on its own: the client has to ask again
                    self.model_loading = None
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
        if rocm_on_windows():
            self.restart_for_model(wanted, previous)
            return False  # the restart could not be asked for; the old model keeps working
        log.info("Switching from model '%s' to '%s'", previous, wanted)
        self.model = None
        gc.collect()  # CTranslate2 gives the GPU memory back once the last reference is gone
        release_torch_memory()  # PyTorch (a Kitsune model) keeps it cached until asked
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

    def restart_blocker(self) -> Optional[str]:
        """Why a model switch cannot happen here, or None when it can.

        With the AMD engine on Windows a switch ends the process for run.cmd to start again
        (restart_for_model()), and only run.cmd's loop does that, which it says through
        SHISUKO_LAUNCHER: under a plain `python server.py` the exit would just end the server.
        --no-update does not matter, unlike for update_blocker(): run.cmd restarts after code 3
        all the same. Everywhere else the switch stays in the process and nothing blocks it.
        """
        if not rocm_on_windows() or os.environ.get("SHISUKO_LAUNCHER") == "1":
            return None
        return ("with the AMD engine on Windows a model switch restarts the server, which only run.cmd (or the "
                "popup's Start button) does; start the server that way, or choose the model with --model")

    def restart_for_model(self, name: str, previous) -> None:
        """Switch to `name` by restarting the server into it: the switch of the AMD engine on Windows.

        Freeing a model can deadlock the ROCm runtime there (CTranslate2 #2038), so the old one is
        never released in this process: `name`, whose files prepare_model() has already fetched,
        goes to NEXT_MODEL_PATH, and the process ends with code 3, which run.cmd answers with a
        start five seconds later; start_app() loads the model named there. Returns only when the
        file cannot be written, with the failure reported as a failed switch is.
        """
        try:
            NEXT_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            NEXT_MODEL_PATH.write_text(name + "\n", encoding="utf-8")
        except OSError as exc:
            log.error("Could not write %s (%s); staying with the model '%s'", NEXT_MODEL_PATH, exc, previous)
            with self.lock:
                self.model_error = (name, f"the server could not restart into '{name}' ({exc})")
                self.model_failed_at = time.time()
                if self.wanted_model == name:
                    self.wanted_model = previous
                self.model_loading = None
            return
        log.info("Switching from model '%s' to '%s': with the AMD engine on Windows a switch restarts the server "
                 "(freeing a model can hang there)", previous, name)
        hard_exit(3)

    def prepare_model(self, name: str) -> None:
        """Download (or locate) the files of `name` and hand them to the transcriber; runs on its own thread."""
        try:
            if operator_folder(self.args, name):
                path = operator_folder(self.args, name)  # the operator's --model is a folder (see request_model): nothing to download
            elif is_kitsune_model_named(name) and kitsune_language_error(self.args):
                raise ValueError(kitsune_language_error(self.args))  # before gigabytes of a model that cannot load
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
            hard_exit(3)
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

    def human_lines(self, s: Session) -> list:
        """The creator's lines the next window builds with: none with --human-subtitles off, on a
        language the rules were not measured on, or live."""
        if not human_subtitles_on(self.args):
            return []
        with s.lock:
            return [] if s.live else s.human_lines

    def apply_human_track(self, s: Session, track: str, lines: list) -> None:
        """Keep the creator's track on the session and put it into the cues already made.

        Those are the cache's, or the first windows', decoded before the track arrived. Each line
        that lies wholly in covered audio goes through assist_cues() over them, as a window's lines
        do; what lies beyond is left to the windows still to come. Built off the lock and swapped in
        only if no window added a cue meanwhile, and under a new session token, which tells a
        client to drop the cue ids it holds and take the list afresh.
        """
        with s.lock:
            s.human_track, s.human_lines = track, lines
        for _ in range(5):
            with s.lock:
                cues, token = [dict(c) for c in s.cues], s.token
                covered, speech, seg_next = [list(iv) for iv in s.covered], list(s.speech), s.seg_next
            if not lines or not cues or any(c.get("src") for c in cues):
                break
            inside = [line for line in lines
                      if any(a <= line[0] and line[1] <= b + 0.05 for a, b in covered)]
            stats: dict = {}
            new, seg_next = assist_cues(cues, inside, speech, cue_limits(self.args), seg_next, None, stats)
            with s.lock:
                if s.token != token or len(s.cues) != len(cues):
                    continue  # a window landed meanwhile: build again over what it added
                for i, cue in enumerate(new):
                    cue["id"] = i
                s.cues, s.seg_next, s.token = new, seg_next, uuid.uuid4().hex[:12]
            log.info("[%s] %d cues made before the creator's subtitles arrived rebuilt: %d lines aligned, "
                     "%d on their own times, %d left to Whisper", s.video_id, len(cues), stats.get("aligned", 0),
                     stats.get("track", 0), stats.get("kept_whisper", 0))
            break
        self.save_cache(s)

    def get_session(self, video_id: str, url: str) -> Session:
        with self.lock:
            s = self.sessions.get(video_id)
            if s is None:
                s = Session(video_id=video_id, url=url)
                self.load_cache(s)
                self.sessions[video_id] = s
        with s.lock:
            retry_due = s.status == "error" and self.retry_due(s, time.time())
            need_fetch = (s.status in ("pending", "evicted") or retry_due) and not s.fetching
            if need_fetch:
                if retry_due:
                    log.info("[%s] retrying the audio fetch after an earlier failure", video_id)
                s.fetching = True
        if need_fetch:
            threading.Thread(target=self.fetcher.fetch, args=(s,), daemon=True, name=f"fetch-{video_id}").start()
        elif s.status == "ready" and not s.fetching and human_subtitles_on(self.args):
            # A video the cache covers to its end is never fetched, so nothing else would ask
            # whether it has the creator's subtitles; a fetch asks on its own.
            self.fetcher.start_human_check(s)
        return s

    def retry_due(self, s: Session, now: float) -> bool:
        """Whether a sync of this errored session should start a fetch. Call with s.lock held.

        A permanent error is asked again only of a viewer who comes back after --idle-minutes (the
        video may have been made public meanwhile); s.last_sync is still the previous sync here.
        """
        if s.permanent:
            return now - s.last_sync > float(getattr(self.args, "idle_minutes", 30)) * 60.0
        if s.retry_at:
            return now >= s.retry_at
        return now - s.error_at >= self.args.retry_after

    def retry_state(self, s: Session, now: float) -> dict:
        """`retrying` and `retry_in` (seconds, None when nothing is scheduled) for an errored
        session's /sync answer. Call with s.lock held."""
        if s.permanent:
            return {"retrying": False, "retry_in": None}
        due = s.retry_at or (s.error_at + self.args.retry_after)
        return {"retrying": True, "retry_in": round(max(0.0, due - now), 1)}

    def sync(self, video_id: str, url: str, t: float, since: int, model=None, words: bool = False) -> dict:
        """The /sync answer. `words` asks for each cue's word timings; without it no cue carries
        any, so a client that never asks sees exactly what it saw before the field existed."""
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
                "cues": s.cues[since:] if words else [without_words(c) for c in s.cues[since:]],
                "next": len(s.cues),
                "busy": s.busy,
                # The last foreign language heard, and whether it has silenced this video.
                "heard": s.heard,
                "language_paused": s.language_paused,
            }
            if s.status == "error":
                # Added only to an error, so every other answer is what it always was.
                resp.update(self.retry_state(s, time.time()))
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
                covered = s.covered
                window = plan_window(s, self.args)
                if window:
                    return (s, window[0], window[1])
                tidied = s.covered is not covered  # plan_window() assigns a new list only when it covers
            if tidied:
                # plan_window() covered a gap too short for a window. Unsaved, the record would stay
                # short of fully covered, and every load would decode the audio again to find it.
                self.save_cache(s)
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
        # Cleaned here on every load (clean_cached_cue()) and never written back from here: a later save
        # of the session writes the cleaned cues, and a record covered to its end may never get one.
        stored = [c for c in data.get("cues", []) if isinstance(c, dict)]
        cleaned = [clean_cached_cue(c) for c in stored]
        s.cues = [c for c in cleaned if c is not None]
        dirty = sum(1 for a, b in zip(stored, cleaned) if a is not b)
        if dirty:
            log.info("[%s] %d cached cues held characters no transcript holds; cleaned", s.video_id, dirty)
        for i, c in enumerate(s.cues):
            c["id"] = i
        s.seg_next = max((int(c.get("seg", -1)) for c in s.cues), default=-1) + 1
        s.speech = merge_intervals(data.get("speech", []))
        s.covered = merge_intervals(data.get("covered", []))
        s.human_track, s.human_lines = human_lines_from_record(data.get("human_subs"))
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
        # A record written with --lyrics off marked a sung stretch covered with nothing in it:
        # Silero heard nothing there, so nothing reached the decoder. Under --lyrics auto those
        # stretches go back to the planner, cues and the rest kept, so a video watched with the
        # switch off is not blank for ever once it is taken off: one at either end of a covered
        # range from 1.5 s (an intro, an outro, the whole of a Short), one between two lines from
        # LYRICS_MIN_STRETCH_S, as process() plans them for a fresh window, since every pause of a
        # talk is a hole of a second or two and a window per pause would fetch, walk and rewrite
        # the record dozens of times over; wants_lyrics() judges each window anew (a silent one
        # costs a Silero pass), and the first window walked rewrites the record under the rule.
        if data.get("lyrics") == "off" and getattr(self.args, "lyrics", "auto") == "auto":
            unheard = unheard_stretches(s.covered, s.speech, s.cues, inner_seconds=LYRICS_MIN_STRETCH_S)
            if unheard:
                s.covered = subtract_intervals(s.covered, unheard)
                log.info("[%s] %.0f s were covered with --lyrics off and nothing heard; transcribing them again",
                         s.video_id, sum(b - a for a, b in unheard))
        if s.fully_covered():
            s.status = "ready"  # nothing left to transcribe, no need to fetch the audio again
        log.info("[%s] loaded %d cached cues", s.video_id, len(s.cues))

    def old_record_sweep(self):
        """Recheck the cue records written before UNHEARD_RULE, one range per idle step of the
        transcriber thread, the only thread that touches the model, so a sync waits for one step at
        most. A record whose unheard_ranges() Transcriber.choose_path(), the decision process()
        makes, would decode in is deleted: its next open transcribes the video again from the audio
        on disk. Any other is rewritten with the marker, one with no such range without reading audio.
        Left as they are: records of another model or language, written with --lyrics off
        (load_cache() gives those back) or paused, all of them under --lyrics off, one with ranges but
        no audio on disk (a recheck never downloads), and one whose video a session holds by then."""
        for path in sorted(CACHE_DIR.glob("*.cues.json")):
            video_id = path.name[:-len(".cues.json")]
            try:
                data, audio = json.loads(path.read_text(encoding="utf-8")), find_cached_audio(video_id)
                if (data.get("unheard_rule") == UNHEARD_RULE or not self.cache_model_matches(data)
                        or data.get("language") != self.args.language or "off" in (getattr(self.args, "lyrics", "auto"), data.get("lyrics"))
                        or data["language_state"].get("paused")):
                    continue
                ranges, rescue = unheard_ranges(data["covered"], data["speech"], data["cues"]), False
                probe = Session(video_id, "")  # what choose_path() learns of the video stays out of live sessions
                for lo, hi, heard in ranges if audio is not None else ():
                    samples = _decode_range(audio, lo, hi, rate=SAMPLE_RATE).astype(np.float32) / 32768.0
                    rescue = self.transcriber.choose_path(probe, samples, heard, lo, hi) != (False, [])
                    yield
                    if rescue:
                        break
                with self.lock:  # a session loads its record under this lock: one that did keeps it
                    if video_id in self.sessions or (ranges and audio is None):
                        continue
                    if rescue:
                        path.unlink()
                    else:
                        marked = json.dumps({**data, "unheard_rule": UNHEARD_RULE}, ensure_ascii=False)
                        path.with_suffix(".tmp").write_text(marked, encoding="utf-8")
                        os.replace(path.with_suffix(".tmp"), path)
                log.info("[%s] old cue record %s", video_id, "removed: the new rule hears more" if rescue else "marked")
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not recheck the old cue record (%s); left as it is", video_id, exc)
            yield

    def cache_record(self, s: Session) -> dict:
        """What save_cache() writes for a session (retranscribe.py writes the same shape elsewhere)."""
        with s.lock:
            record = {
                "video_id": s.video_id, "title": s.title, "duration": s.duration,
                "format": CACHE_FORMAT,
                "model": self.model_name, "language": self.args.language,
                "cues": list(s.cues), "covered": [list(iv) for iv in s.covered],
                "speech": [[round(a, 2), round(b, 2)] for a, b in s.speech],
                # The rule the covered ranges were made under: without the lyrics rule a sung
                # window was covered with nothing in it, and load_cache() offers it again.
                "lyrics": getattr(self.args, "lyrics", "auto"),
                "unheard_rule": UNHEARD_RULE,
                # Reopening a foreign video finds it paused instead of hallucinating all over again.
                "language_state": {"foreign_seconds": round(s.foreign_seconds, 2),
                                   "heard": s.heard, "paused": s.language_paused},
            }
            if s.human_track is not None:
                # Asked: the lines are kept, so a later window needs no second request, and a record
                # asked once is not asked again. Absent means never asked.
                record["human_subs"] = ({"track": s.human_track, "lines": s.human_lines} if s.human_track
                                        else {"track": None})
            return record

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
        # Opt-in: only a literal true adds the word timings, so every other body answers as before.
        words = body.get("words") is True
        try:
            self._json(200, self.app.sync(video_id, str(body.get("url") or ""), t, since, model, words))
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

def rocm_on_windows() -> bool:
    """Whether this process runs the AMD engine on Windows, where the ROCm runtime can hang its end."""
    return ROCM_ACTIVE and os.name == "nt"


# Every model load_model() built where rocm_on_windows(): freeing one can deadlock the ROCm runtime
# there (CTranslate2 #2038, #2101), so none is let go, not by a function that returns (--probe-gpu)
# nor by the traceback of a failed warm-up. Such a process ends through TerminateProcess anyway,
# and its model switch is a restart (App.restart_for_model()), so nothing is held longer than before.
KEPT_MODELS: list = []


def end_on_ctrl_c(on_interrupt):
    """Where rocm_on_windows(), make Ctrl+C and Ctrl+Break call on_interrupt(), which ends the process,
    instead of raising KeyboardInterrupt; returns the handler to put back on both afterwards (None
    where nothing changed).

    A model load is one long call into CTranslate2, and a Ctrl+C during it raises KeyboardInterrupt
    the moment the call returns, before faster-whisper has stored the new model: the unwinding frees
    it, which can hang the ROCm runtime on Windows (#2038), before any except clause could end the
    process. A handler of Python's own runs at that same point, but nothing unwinds. Python leaves
    Ctrl+Break (SIGBREAK) to the console, whose default handler ends the process through
    ExitProcess, with the DLL detach that terminate_process() exists to skip; so it gets the same handler.
    """
    if not rocm_on_windows():
        return None

    def handler(signum, frame):
        on_interrupt()

    previous = signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGBREAK, handler)
    return signal.default_int_handler if previous is None else previous


def flush_output() -> None:
    """Write out what the log handlers, stdout and stderr still hold, before an exit that skips Python's clean-up."""
    handlers = list(logging.getLogger().handlers)
    for logger in list(logging.Logger.manager.loggerDict.values()):
        handlers.extend(getattr(logger, "handlers", []))  # placeholders have none
    for stream in handlers + [sys.stdout, sys.stderr]:
        try:
            stream.flush()
        except Exception:  # noqa: BLE001  (a closed handler, no stdout under pythonw)
            pass


def terminate_process(code: int) -> None:
    """End this process at once with `code` through TerminateProcess, once the log and stdio are flushed.

    CTranslate2's ROCm build on Windows can hang the way out of a process: freeing a model, the
    interpreter's exit and even os._exit(), inside the runtime's DLL detach (CTranslate2 #2038,
    #2085, #2101, the last even on the CPU). TerminateProcess runs no DLL detach at all; it is
    what helped there. The process is gone when the call returns, unless it failed.
    """
    import ctypes

    flush_output()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)


def hard_exit(code: int) -> None:
    """os._exit(code), the exit that waits for no thread; TerminateProcess where rocm_on_windows()."""
    if rocm_on_windows():
        terminate_process(code)
    os._exit(code)


def finish(code: Optional[int] = None) -> None:
    """How main() ends: sys.exit(code), or a plain return for None (its normal end).

    Where rocm_on_windows() the interpreter's own exit can hang, so both end in TerminateProcess
    instead, with 0 for the normal end.
    """
    if rocm_on_windows():
        terminate_process(code or 0)
    if code is not None:
        sys.exit(code)


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def amd_vram_mb(root: Path):
    """(free, total) MiB of the AMD card with the most memory under `root` (/sys/class/drm), or None.

    amdgpu shows each card's memory in bytes (device/mem_info_vram_total and _used) next to its PCI
    vendor (0x1002). The largest card is taken, since beside a dedicated card an AMD processor's
    own graphics shows up too, with a small carve-out of system memory that is not where a model runs.
    """
    try:
        cards = sorted((p for p in root.iterdir() if re.fullmatch(r"card\d+", p.name)), key=lambda p: int(p.name[4:]))
    except OSError:
        return None
    best = None
    for card in cards:
        device = card / "device"
        try:
            if (device / "vendor").read_text(encoding="ascii").strip().lower() != "0x1002":
                continue
            total = int((device / "mem_info_vram_total").read_text(encoding="ascii").strip())
            used = int((device / "mem_info_vram_used").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if total > 0 and (best is None or total > best[1]):
            best = (max(total - used, 0), total)
    return None if best is None else (best[0] // (1024 * 1024), best[1] // (1024 * 1024))


def gpu_memory_mb():
    """(free, total) GPU memory in MiB via nvidia-smi, or None if unavailable.

    With the AMD engine the model runs on an AMD card whatever else the machine holds: its memory
    comes from amdgpu's sysfs files on Linux, and on Windows there is nothing to ask (float16, as
    on an NVIDIA machine without nvidia-smi).
    """
    if ROCM_ACTIVE:
        return amd_vram_mb(DRM_ROOT) if sys.platform.startswith("linux") else None
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
    """Turn --device auto into the backend that will actually run: Apple's GPU, else an NVIDIA one, else the CPU.

    Apple's is asked first, and the order costs nothing: mlx_available() is false off darwin, and no
    Apple Silicon machine has a CUDA device, so the two can never both answer yes. It buys two
    things. A Mac never imports ctranslate2 to be told what it already knows, and
    "mlx_available()" becomes a faithful answer to "will the device be mlx", which
    default_model_for() needs at parse time, where the CUDA probe must not run (see there).
    """
    if device != "auto":
        return device
    if mlx_available():
        return "mlx"
    return "cuda" if cuda_available() else "cpu"


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


_MLX_BEAM: Optional[bool] = None  # whether mlx-whisper took the beam search; asked once


def enable_mlx_beam_search() -> bool:
    """Give mlx-whisper the beam search it lacks, and say whether it took.

    mlx_beam.py is loaded by path, the way run_check() loads native_host.py, so that server.py
    stays the one file and a machine without MLX never reads it. A failure here is not fatal:
    the backend falls back to greedy decoding, which is what it did before the decoder existed.
    """
    global _MLX_BEAM
    if _MLX_BEAM is None:
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "shisuko_mlx_beam", Path(__file__).with_name("mlx_beam.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.enable_beam_search()
            _MLX_BEAM = True
        except Exception as exc:  # noqa: BLE001
            log.warning("MLX beam search is unavailable (%s); decoding greedily, which is faster "
                        "but mishears a homophone now and then. Use --beam-size 1 to silence this.", exc)
            _MLX_BEAM = False
    return _MLX_BEAM


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

    One option does not survive the crossing on its own. mlx-whisper has no vad_filter: given one,
    this runs the Silero pass faster-whisper would run, hands the decoder the speech with the
    silence cut out and maps the timestamps back, which is what faster-whisper does with the same
    options. `--beam-size` it does serve, through the decoder mlx_beam.py adds to the library.
    """

    def __init__(self, path: str, compute_type: str = "float16"):
        import mlx.core as mx

        self.path = path
        self.fp16 = compute_type != "float32"
        self.dtype = mx.float16 if self.fp16 else mx.float32
        self.beam = enable_mlx_beam_search()

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
        # mlx-whisper drops beam_size above temperature 0 by itself, exactly as faster-whisper
        # does, so this asks for the beam only where it is used: the first, greedy-or-beam pass.
        size = int(options.get("beam_size") or 1)
        beam = {"beam_size": size} if self.beam and size > 1 else {}
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
            **beam,
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


def kitsune_name(name) -> Optional[tuple]:
    """(base, precision) of a Kitsune model name, else None: kitsune-0.6b is ("kitsune-0.6b", ""), the
    bf16 export; kitsune-0.6b-int8-w8a8 is ("kitsune-0.6b", "int8"). A repo id of KITSUNE_REPOS is its
    base name. An unknown precision gives ("kitsune-0.6b", None), which download_model_files() refuses."""
    if not isinstance(name, str):
        return None
    for base, repo in KITSUNE_REPOS.items():
        if name in (base, repo):
            return base, ""
        if name.startswith(base + "-"):
            suffix = name[len(base) + 1:]
            if suffix in KITSUNE_FORMATS:
                return base, suffix
            return base, KITSUNE_FORMAT_ALIASES.get(suffix)
    return None


def canonical_model_name(name):
    """One name per set of weights: large-v3 for Systran/faster-whisper-large-v3, large and itself.

    faster-whisper's size aliases and their repo ids load the same files, so the server compares,
    reports and caches under the first alias of the repo. A Kitsune name comes out as its base
    plus its precision's short name (kitsune-0.6b-int8 for -int8-w8a8, kitsune-0.6b for its repo
    id or -bf16). An MLX repo is the same weights converted for the Apple GPU, so it answers to
    the size too. Anything it does not know passes through.
    """
    if not isinstance(name, str):
        return name
    kit = kitsune_name(name)
    if kit is not None and kit[1] is not None:
        return kit[0] + (f"-{kit[1]}" if kit[1] else "")
    name = MLX_ALIASES.get(name, name)
    forward, reverse = model_alias_tables()
    return reverse.get(forward.get(name, name), name)


def model_spellings(name) -> list:
    """Every name that loads the same weights as `name`: its aliases in faster-whisper's table and the repo id.

    /health reports a failed model under its canonical name and lists these beside it, so the
    popup can match whatever spelling the viewer typed without a table of its own. A name the
    table does not know is its own only spelling.
    """
    kit = kitsune_name(name)
    if kit is not None and kit[1] is not None:
        base, fmt = kit
        if not fmt:
            return [base, f"{base}-bf16", KITSUNE_REPOS[base]]
        return [f"{base}-{fmt}"] + [f"{base}-{alias}" for alias, short in KITSUNE_FORMAT_ALIASES.items() if short == fmt]
    forward, _ = model_alias_tables()
    # Through the canonical name, so that an MLX repo answers with the sizes too, not only itself.
    canonical = canonical_model_name(name)
    repo = forward.get(canonical, canonical)
    spellings = [alias for alias, target in forward.items() if target == repo] + [repo]
    mlx = MLX_REPOS.get(canonical)
    return spellings + [mlx] if mlx and mlx not in spellings else spellings


def kitsune_downloaded(repo_dir: Path, base: str) -> set:
    """The Kitsune names whose files are in the hub folder of `base`'s repo: the bare name for a
    root package, base-<short> for each precision folder."""
    names = set()
    folders = {folder: short for short, folder in KITSUNE_FORMATS.items()}
    for snap in (repo_dir / "snapshots").glob("*"):
        if (snap / "config.json").is_file() and (snap / "model.safetensors").exists():
            names.add(base)
        for sub in snap.glob("*"):
            short = folders.get(sub.name)
            if short and (sub / "config.json").is_file() and (sub / "model.safetensors").exists():
                names.add(f"{base}-{short}")
    return names


def downloaded_models() -> list:
    """The models in MODELS_DIR (the hub stores owner/name as models--owner--name), under their canonical names."""
    if not MODELS_DIR.is_dir():
        return []
    names = set()
    for p in MODELS_DIR.glob("models--*"):
        parts = p.name[len("models--"):].split("--")
        if p.is_dir() and len(parts) == 2 and all(parts):
            kit = kitsune_name("/".join(parts))
            if kit is not None:
                names |= kitsune_downloaded(p, kit[0])
            else:
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
    WhisperModel() can choke on it.

    A Kitsune name fetches its package from its repo instead (kitsune_download_plan()), once PyTorch
    is there to run it: without it the download would be gigabytes for a model that cannot load.
    On the Apple GPU the files are MLX weights, so `device` decides which of the two downloads runs.
    """
    if kitsune_name(name) is not None:
        missing = kitsune_runtime_missing()
        if missing:
            raise ValueError(missing)
        from huggingface_hub import snapshot_download

        repo, folder, patterns = kitsune_download_plan(name)
        root = snapshot_download(repo, cache_dir=str(MODELS_DIR), allow_patterns=patterns)
        path = os.path.join(root, folder) if folder else root
        require_kitsune_files(path, name, folder)
        return path
    if device == "mlx":
        return download_mlx_model_files(name)
    from faster_whisper import download_model

    path = download_model(name, cache_dir=str(MODELS_DIR))
    require_model_bin(path, name)
    return path


def kitsune_download_plan(name: str) -> tuple:
    """(repo id, folder or "" for the root, the files to fetch) of a Kitsune name; ValueError for an unknown precision."""
    base, fmt = kitsune_name(name)
    if fmt is None:
        raise ValueError(f"unknown precision in '{name}': use {base} (bf16) or "
                         + ", ".join(f"{base}-{short}" for short in KITSUNE_FORMATS))
    folder = KITSUNE_FORMATS[fmt] if fmt else ""
    return KITSUNE_REPOS[base], folder, [f"{folder}/{f}" if folder else f for f in KITSUNE_FILES]


def require_kitsune_files(path: str, name: str, folder: str = "") -> None:
    """Refuse a download that brought no package: the repo has no such precision (or no bf16 export at its root)."""
    for needed in ("config.json", "model.safetensors"):
        if not os.path.isfile(os.path.join(path, needed)):
            # The popup puts "Shisu-ko: model <name>: " in front of this, so the name is not repeated.
            where = f"no {folder} folder" if folder else "no model at its root"
            raise ValueError(f"its repo has {where} ({needed} is missing); it may not be published yet")


def kitsune_runtime_missing() -> Optional[str]:
    """KITSUNE_INSTALL_HINT while PyTorch, transformers or safetensors cannot be imported, else None."""
    import importlib.util

    for module in ("torch", "transformers", "safetensors"):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return KITSUNE_INSTALL_HINT
    return None


_KITSUNE_ENGINE = None


def kitsune_engine():
    """kitsune_engine.py from next to this file, loaded by path (server.py runs as a script, and under
    another module name in the tests). It imports numpy alone; torch only once a model loads."""
    global _KITSUNE_ENGINE
    if _KITSUNE_ENGINE is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("shisuko_kitsune_engine", Path(__file__).with_name("kitsune_engine.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # before exec: its dataclasses look their module up there
        spec.loader.exec_module(module)
        _KITSUNE_ENGINE = module
    return _KITSUNE_ENGINE


def is_kitsune_model(name, path: Optional[str] = None) -> bool:
    """Whether `name` (or the folder `path` holds it in) is a Kitsune model: a KITSUNE_REPOS name, or a folder
    whose config.json names one of the students' architectures. For the operator's own names alone
    (--model, dump_words.py's): a client's name must never be probed as a directory, so load_model()
    asks is_kitsune_folder() only of a prepared path or the operator's --model."""
    return is_kitsune_model_named(name) or is_kitsune_folder(path or name)


def is_kitsune_model_named(name) -> bool:
    return kitsune_name(name) is not None


def is_kitsune_folder(folder) -> bool:
    return isinstance(folder, str) and os.path.isdir(folder) and kitsune_engine().architecture_of(folder) is not None


def operator_folder(args, name) -> Optional[str]:
    """The operator's --model folder when `name` is that model (as given, or its canonical form, which
    the App keeps: a folder named like kitsune-0.6b-int8-w8a8 is kitsune-0.6b-int8 there), else None.
    A client's name is never looked up on disk: only the folder the operator named is returned."""
    model = getattr(args, "model", None)
    if not isinstance(model, str) or name not in (model, canonical_model_name(model)):
        return None
    return model if os.path.isdir(model) else None


def kitsune_language_error(args) -> Optional[str]:
    """Why a Kitsune model cannot serve this server's --language, or None for Japanese."""
    language = getattr(args, "language", "ja")
    if language == "ja":
        return None
    return (f"Kitsune-Transcribe models transcribe Japanese only; --language is '{language}'. "
            "Pick a Whisper model for other languages")


def release_torch_memory() -> None:
    """Give a freed Kitsune model's GPU memory back to the driver; PyTorch keeps it cached otherwise."""
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def friendly_model_error(exc: BaseException, name: str) -> str:
    """A short reason for the popup; huggingface_hub's exceptions run to several lines with request ids."""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "invalid model size" in low:
        return f"unknown model size '{name}'; use a size such as large-v3 or a Hugging Face repo id owner/name"
    # The server's own one-line reasons, which say what to do already.
    if msg == KITSUNE_INSTALL_HINT or any(s in low for s in ("no model.bin", "unknown precision", "japanese only",
                                                             "may not be published yet")):
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
# A Kitsune precision's download next to its bf16 export: 1 byte a weight for int8
# and fp8, half a byte and its scales for the 4-bit formats; the kept tensors stay 16-bit.
KITSUNE_SIZE_SHARE = {"": 1.0, "fp16": 1.0, "int8": 0.55, "fp8": 0.55, "nvfp4": 0.33, "mxfp4": 0.32}


def kitsune_size(name: str) -> str:
    base, fmt = kitsune_name(name)
    mb = {"kitsune-0.6b": 1230, "kitsune-0.3b": 610, "kitsune-0.1b": 210}.get(base)
    if mb is None or fmt not in KITSUNE_SIZE_SHARE:
        return "size unknown"
    mb *= KITSUNE_SIZE_SHARE[fmt]
    return f"about {mb / 1000:.1f} GB" if mb >= 1000 else f"about {int(round(mb, -1))} MB"


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
        from huggingface_hub import snapshot_download  # faster-whisper's dependency, installed with it
    except Exception as exc:  # noqa: BLE001
        print(f"faster-whisper is not installed for {sys.executable} ({exc}); run setup first")
        return 2
    name = canonical_model_name(name)
    folder = ""
    if kitsune_name(name) is not None:
        # A Kitsune name is known by its table, like a faster-whisper size, and its files are its
        # package's (kitsune_download_plan()). PyTorch is not needed to download it: setup
        # installs it first all the same (kitsune_setup.py), and a start without it says so.
        try:
            repo_id, folder, patterns = kitsune_download_plan(name)
        except ValueError as exc:
            print(exc)
            return 2
        alias, size, require = True, kitsune_size(name), None
    else:
        try:
            from faster_whisper import utils as fw_utils
        except Exception as exc:  # noqa: BLE001
            print(f"faster-whisper is not installed for {sys.executable} ({exc}); run setup first")
            return 2
        sizes = dict(getattr(fw_utils, "_MODELS", None) or {})
        # The backend that will run this model decides which conversion of it setup fetches: the
        # CTranslate2 files, or the MLX ones for an Apple GPU. `device` is the launcher's own
        # --device, so a download and the start that follows it agree; fetching the other format
        # here would cost the viewer the whole wait twice.
        mlx = resolve_device(device) == "mlx"
        patterns, require = ((MLX_FILE_PATTERNS, require_mlx_weights) if mlx
                             else (MODEL_FILE_PATTERNS, require_model_bin))
        try:
            repo_id = mlx_repo_for(name) if mlx else (name if "/" in name else sizes.get(name))
        except ValueError as exc:
            print(str(exc))
            return 2
        if repo_id is None:
            print(f"unknown model size '{name}': faster-whisper knows {', '.join(sizes) or 'no sizes at all'}; "
                  "a Hugging Face repo id is written owner/name; Kitsune models are " + ", ".join(KITSUNE_REPOS))
            return 2
        alias, size = name in sizes, MODEL_SIZES.get(name, "size unknown")
    # A size from faster-whisper's own table names a converted model that WhisperModel() fetches
    # by itself, so the choice is kept before the download: a start after a failed or interrupted
    # one then downloads this model, not the built-in default, as setup promises. A repo id is
    # kept only once its files were seen, since a typo or a PyTorch checkpoint would make every
    # later start exit 2; a size whose repo turns out that way is taken back again. A Kitsune
    # name is kept early too, like a size.
    previous = configured_model()
    if alias:
        write_config({"model": name})
    print(f"Downloading {name} ({size}) into {MODELS_DIR} ...", flush=True)
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
        hard_exit(2)
    result = outcome[0]
    try:
        if isinstance(result, BaseException):
            raise result
        if folder or kitsune_name(name) is not None:
            result = os.path.join(result, folder) if folder else result
            require_kitsune_files(result, name, folder)
        else:
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


def load_model(args, name: Optional[str] = None, path: Optional[str] = None, strict: bool = False):
    """Load `name` (default: --model), picking device and precision for the GPU memory free right now.

    `path` is the directory download_model_files() prepared for `name`; without it WhisperModel
    resolves the name itself, which is fine for the operator's --model (a size, a repo or a folder).
    `strict` (--probe-gpu) means no CPU fallback, and a failed warm-up raises instead of warning.
    With the AMD engine, a model loaded and warmed up on the GPU resets the crash guard.
    A Kitsune model loads through load_kitsune_model() instead.
    """
    name = name or args.model
    # A folder is looked at only when the server prepared it or the operator named it (--model): a
    # client's name reaches here without a path from reload_model(), and must not be probed on disk.
    local = path or operator_folder(args, name)
    if is_kitsune_folder(local) or is_kitsune_model_named(name):
        return load_kitsune_model(args, name, path, strict)
    from faster_whisper import WhisperModel

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
    where = "the AMD GPU (ROCm)" if ROCM_ACTIVE and device == "cuda" else device
    log.info("Loading Whisper model '%s' on %s (%s); models are stored in %s", name, where, compute, MODELS_DIR)
    kwargs = {"device": device, "compute_type": compute, "download_root": str(MODELS_DIR)}
    if args.cpu_threads:
        kwargs["cpu_threads"] = args.cpu_threads
    try:
        model = WhisperModel(path or name, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if device != "cuda" or strict:
            raise
        log.warning("CUDA initialisation failed (%s). Falling back to CPU int8, which is slow for large models.", exc)
        device, compute = "cpu", "int8"
        kwargs.update(device=device, compute_type=compute)
        model = WhisperModel(path or name, **kwargs)
    warm_up(model, args, device=device, strict=strict)
    return model, device, compute


def warm_up(model, args, device: Optional[str] = None, strict: bool = False) -> None:
    """Decode two seconds of silence, so the first real window does not pay for the lazy load.

    With the AMD engine on Windows the model is held before the decode, and a decode that got
    through resets the crash guard; the MLX path names no device, so neither runs there.
    """
    if rocm_on_windows():
        KEPT_MODELS.append(model)  # before the warm-up: a strict one that raises must not free it either
    try:
        t0 = time.time()
        segs, _ = model.transcribe(np.zeros(SAMPLE_RATE * 2, dtype=np.float32), language=args.language,
                                   beam_size=1, vad_filter=False)
        list(segs)
        log.info("Model ready (warm-up took %.1fs)", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        log.warning("Warm-up transcription failed: %s", exc)
    else:
        if ROCM_ACTIVE and device == "cuda":
            clear_rocm_starts()  # the AMD engine got through: the crash guard starts counting from zero


def load_kitsune_model(args, name: str, path: Optional[str] = None, strict: bool = False):
    """(model, device, compute label) of a Kitsune model, loaded by kitsune_engine.py on PyTorch.

    The files come from download_model_files() (cached after the first time) unless `path` holds
    them or the operator's --model is the folder itself. Only Japanese: another --language is
    refused before anything loads. The GPU is PyTorch's CUDA; with the AMD engine on, it sees no
    GPU and the model runs on the CPU.
    """
    wrong_language = kitsune_language_error(args)
    if wrong_language:
        raise ValueError(wrong_language)
    missing = kitsune_runtime_missing()
    if missing:
        raise ValueError(missing)
    engine = kitsune_engine()
    if path is None:
        path = operator_folder(args, name) or download_model_files(name)
    if args.device != "cpu":
        mem = gpu_memory_mb()
        if mem:
            log.info("GPU memory: %d MiB free of %d MiB", *mem)
            if mem[0] < CRITICAL_VRAM_MB:
                log.warning("Very little GPU memory is free (%d MiB); close other GPU apps if the model does not load "
                            "or runs slowly, or run with --device cpu", mem[0])
    log.info("Loading Kitsune model '%s' (PyTorch); models are stored in %s", name, MODELS_DIR)
    try:
        model = engine.load(path, device=args.device, compute=args.compute_type, cpu_threads=args.cpu_threads)
    except ImportError as exc:
        # transformers without the architecture (too old), or a torch whose import fails. Not an
        # ImportError to the caller: start_app() reads that as CTranslate2's own, the AMD engine's.
        raise ValueError(f"PyTorch / transformers cannot run this model ({exc}); run server/kitsune_setup.py "
                         "--force with the venv's Python") from exc
    except Exception as exc:  # noqa: BLE001
        if args.device == "cpu" or strict or isinstance(exc, engine.PackageError):
            raise
        log.warning("Loading on the GPU failed (%s). Falling back to the CPU, which is slow for large models.", exc)
        release_torch_memory()
        model = engine.load(path, device="cpu", compute="auto", cpu_threads=args.cpu_threads)
    try:
        t0 = time.time()
        model.transcribe(np.zeros(SAMPLE_RATE * 2, dtype=np.float32), language="ja", vad_filter=False)
        log.info("Model ready on %s (%s; warm-up took %.1fs)", model.device, model.compute_label, time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        log.warning("Warm-up transcription failed: %s", exc)
    if ROCM_ACTIVE:
        # main() counted this start for the AMD engine's crash guard, and PyTorch never touches the
        # ROCm runtime: a start that got here did not crash it, so the count goes back to zero as
        # after a Whisper model's warm-up on the GPU. Left counted, two Kitsune starts would turn
        # the engine off as if it had crashed.
        clear_rocm_starts()
    return model, model.device, model.compute_label


def count_rocm_start() -> bool:
    """Count one more start on the AMD engine before its model loads (the crash guard, ROCM_STARTS_PATH).

    load_model() takes the count back once a model is warmed up on the GPU, start_app() when the
    load ends on a Python exception and main() when it ends on a Ctrl+C; a process that dies on
    the way leaves it standing, and after ROCM_GUARD_LIMIT of them rocm_engine() keeps the engine
    off. Only a start that can reach the GPU counts (not --device cpu). Never stops a start: a
    file that cannot be written costs the guard, not the server. True when this start was counted.
    """
    try:
        ROCM_STARTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROCM_STARTS_PATH.write_text(f"{read_rocm_starts(ROCM_STARTS_PATH) + 1}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write %s (%s); the AMD engine's crash guard does not count this start", ROCM_STARTS_PATH, exc)
        return False
    return True


def uncount_rocm_start() -> None:
    """Take back this start's count (count_rocm_start()): it ended on a Python exception or a Ctrl+C, not a crash.

    One step down, not to zero: a crash counted by an earlier start stays counted.
    """
    count = read_rocm_starts(ROCM_STARTS_PATH)
    try:
        if count <= 1:
            ROCM_STARTS_PATH.unlink(missing_ok=True)
        else:
            ROCM_STARTS_PATH.write_text(f"{count - 1}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write %s (%s)", ROCM_STARTS_PATH, exc)


def clear_rocm_starts() -> None:
    """Forget the starts the crash guard counted: the AMD engine loaded a model, or its probe passed."""
    try:
        ROCM_STARTS_PATH.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove %s (%s)", ROCM_STARTS_PATH, exc)


def check_rocm_import(device: str) -> None:
    """End the process with code 3 and the AMD engine off when it cannot run this start here.

    rocm_engine() sees files, not whether they load: a system ROCm removed or upgraded past 7.2, a
    DLL missing from the side folder, or on Linux a ROCM_PATH that the terminal of the setup had
    and the Start button's environment has not, fail the import that load_model() would meet, and
    the start would end on 2, which the launcher never retries, without a word about the engine.
    Nor do they say whether the ROCm runtime sees an AMD GPU: a card removed or replaced (by an
    NVIDIA one, say), a driver it cannot use, or on Linux a user outside the render group, and
    --device auto would run the model on the processor, with an NVIDIA GPU beside it left idle.
    So a start that is to use the GPU (not --device cpu) asks for its devices first.
    Either way the crash guard goes to its limit and the process ends with code 3 (leave_rocm()).
    """
    try:
        import ctranslate2

        count_devices = ctranslate2.get_cuda_device_count  # the compiled part: without it the package imports empty
    except Exception as exc:  # noqa: BLE001  (a DLL or shared object that does not load can raise OSError)
        hint = " (is ROCM_PATH set for the Start button too?)" if sys.platform.startswith("linux") else ""
        leave_rocm(f"The AMD engine's CTranslate2 in {ROCM_DIR} does not load ({exc})", hint)
        return
    if device == "cpu":
        return  # the ROCm runtime is never started for it
    try:
        devices = count_devices()
    except Exception:  # noqa: BLE001
        devices = 0
    if devices < 1:
        leave_rocm("The AMD engine sees no AMD GPU (a card removed or replaced, or a driver it cannot use: on Windows "
                   "Adrenalin 26.2.2 or newer; on Linux /dev/kfd and this user in the render and video groups)")


def leave_rocm(problem: str, hint: str = "") -> None:
    """End a start the AMD engine cannot run: the crash guard at its limit, then code 3.

    Under run.cmd / run.sh (SHISUKO_LAUNCHER) the launcher starts the server again from its own
    environment, and rocm_engine() keeps the engine off there until server/amd_setup.py --probe
    has tested it again; a hand start just ends, and its next start is on the default engine. A
    guard that cannot be written would restart into the same failure for ever, so then the start
    ends on 2 after all.
    """
    if os.environ.get("SHISUKO_LAUNCHER") == "1":
        then = "exiting so that the launcher starts the server again on the default engine"
    else:
        then = "the server stops, and its next start uses the default engine"
    log.error("%s; %s. server/amd_setup.py --probe tests it again, --remove takes it out%s", problem, then, hint)
    try:
        ROCM_STARTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROCM_STARTS_PATH.write_text(f"{ROCM_GUARD_LIMIT}\n", encoding="utf-8")
    except OSError as error:
        log.error("Could not write %s (%s), so a restart would meet the same failure", ROCM_STARTS_PATH, error)
        finish(2)
    hard_exit(3)


def take_next_model(path: Path) -> Optional[str]:
    """The model a switch restarted the server into (restart_for_model()), canonical; None without one.

    The file goes first, whatever it holds, so that a model that kills the process while it loads
    is not asked for again by every restart after it; a name that cannot be removed with it is
    not used either. Only a name valid_model_name() accepts counts: the file is outside the server.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        text = ""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s (%s); starting with the default model", path, exc)
        return None
    name = text.strip()
    return canonical_model_name(name) if valid_model_name(name) else None


def start_app(args) -> "App":
    """Load this start's model and build the App around it; ends the process with code 2 when none loads.

    With the AMD engine on Windows a model switch is a restart (App.restart_for_model()), and the
    model it named is this start's. Its files come from download_model_files(), as in a switch, so
    that a name from the client never reaches WhisperModel() as a folder. When it does not load,
    the server starts with --model instead and reports the failure, as a failed switch does.
    """
    wanted = take_next_model(NEXT_MODEL_PATH) if rocm_on_windows() else None
    failed = None
    if wanted is not None and wanted != canonical_model_name(args.model):
        log.info("Starting with the model '%s' that the switch asked for", wanted)
        try:
            # No local name for the model: the switch frees it through App alone (see switch_model_if_wanted).
            return App(args, *load_model(args, wanted, path=download_model_files(wanted)), model_name=wanted, sweep=True)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not load the model '%s' (%s); starting with '%s'", wanted, exc, args.model)
            failed = (wanted, friendly_model_error(exc, wanted))
    try:
        app = App(args, *load_model(args), sweep=True)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not load the model '%s': %s", args.model, exc)
        if ROCM_ACTIVE and args.device != "cpu" and not isinstance(exc, ImportError):
            # A Python exception after load_model()'s CPU fallback (or on the CPU, where no GPU was
            # seen): the model's fault (a typo, offline, a missing file), not a crash of the engine,
            # which no except clause would see. Code 2 is never retried, so no crash loop follows.
            uncount_rocm_start()
        finish(2)
    if failed is not None:
        with app.lock:
            app.model_error, app.model_failed_at = failed, time.time()
    return app


def amd_gpu_label() -> str:
    """What --probe-gpu calls the AMD GPU: its gfx target on Linux (gfx1100), else CTranslate2's "device 0"."""
    targets = kfd_gfx_targets(KFD_NODES_ROOT) if sys.platform.startswith("linux") else []
    return targets[0] if targets else "device 0"


def gfx_target(version: int) -> str:
    """The gfx name of a KFD gfx_target_version: major*10000 + minor*100 + stepping, the last two in hex."""
    return f"gfx{version // 10000}{version // 100 % 100:x}{version % 100:x}"


def kfd_gfx_targets(nodes_root: Path) -> list:
    """The gfx targets of the GPUs under `nodes_root` (/sys/class/kfd/kfd/topology/nodes), in node order.

    Each node's `properties` has a line "gfx_target_version N"; N is 0 for the processor's node.
    """
    try:
        nodes = sorted((p for p in nodes_root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return []
    targets = []
    for node in nodes:
        try:
            text = (node / "properties").read_text(encoding="ascii")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            key, _, value = line.strip().partition(" ")
            if key == "gfx_target_version" and value.strip().isdigit() and int(value):
                targets.append(gfx_target(int(value)))
    return targets


def run_probe_gpu(args) -> int:
    """--probe-gpu, what amd_setup.py runs with SHISUKO_ENGINE=rocm: does the AMD engine work here?

    A card or driver the ROCm build cannot use does not always raise: it can abort the process (a
    C++ terminate, "Memory access fault by GPU") where load_model()'s CPU fallback never sees it,
    so the test runs in a process of its own, and what reaches config.json is its verdict. The
    engine is taken out of config.json first, so that a probe that dies on the way leaves the
    server on the default engine; only --model loaded on the GPU without the fallback and warmed
    up turns it on, and resets the crash guard. Returns the exit code, 0 or 1 (main() ends on it
    through hard_exit(), since the interpreter's exit can hang with this engine on Windows). The
    model is not freed when this returns: on Windows load_model() keeps it (KEPT_MODELS) until
    hard_exit(), as freeing it can hang there too.
    """
    if not ROCM_ACTIVE:
        print(f"The AMD engine is not active: {ROCM_REASON}", flush=True)
        return 1

    def failed(reason: str) -> int:
        print(f"The AMD GPU engine does not work here: {reason}", flush=True)
        return 1

    try:
        if "engine" in read_config():
            write_config({"engine": None})
    except OSError as exc:
        return failed(f"could not write {CONFIG_PATH} ({exc})")
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        return failed(f"CTranslate2 from {ROCM_DIR} could not be loaded ({exc})")
    where = os.path.dirname(os.path.abspath(getattr(ctranslate2, "__file__", None) or "?"))
    print(f"CTranslate2 {getattr(ctranslate2, '__version__', '?')} from {where}: {count} device(s)", flush=True)
    if os.path.normcase(where) != os.path.normcase(os.path.abspath(ROCM_DIR / "ctranslate2")):
        return failed(f"that is not the CTranslate2 in {ROCM_DIR}")
    if count < 1:
        return failed("no AMD GPU visible to the ROCm engine")
    args.device = "cuda"
    if is_kitsune_model(args.model):
        # The test is of CTranslate2's ROCm build, which runs Whisper alone; a Kitsune model (setup's
        # pick, in config.json) runs on PyTorch and would load on the CPU here. Whisper's smallest
        # model (about 75 MB) stands in, and config.json keeps the Kitsune choice.
        print(f"The model chosen at setup, {args.model}, runs on PyTorch, not on this engine; "
              f"testing with {PROBE_WHISPER_MODEL} instead", flush=True)
        args.model = PROBE_WHISPER_MODEL
    try:
        _model, device, compute = load_model(args, strict=True)
    except Exception as exc:  # noqa: BLE001
        return failed(f"the model '{args.model}' did not load and run on it ({exc})")
    if device != "cuda":
        return failed(f"the model '{args.model}' loaded on {device}, not on the GPU")
    try:
        write_config({"engine": "rocm"})
    except OSError as exc:
        return failed(f"could not write {CONFIG_PATH} ({exc})")
    clear_rocm_starts()
    print(f"AMD GPU engine works: {amd_gpu_label()} ({compute})", flush=True)
    return 0


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


def default_model_for(device: str = "auto") -> str:
    """The built-in --model for the backend that will run it; see MLX_DEFAULT_MODEL for why it differs.

    It asks mlx_available() rather than resolve_device(), and the two cannot disagree: resolve_device()
    answers mlx for exactly the machines that function accepts. The reason not to call it is that
    parse_args() calls this, and resolve_device() would reach cuda_available() off darwin, importing
    ctranslate2 before the AMD engine's crash guard has counted the start (count_rocm_start(), then
    check_rocm_import()) — the one import that guard exists to survive. cuda and cpu answer with the
    same model anyway, so the probe would buy nothing.
    """
    apple = device == "mlx" or (device == "auto" and mlx_available())
    return MLX_DEFAULT_MODEL if apple else DEFAULT_MODEL


def resolve_default_model(args):
    """Fill in --model when the flag was not given: the model chosen at setup, else this machine's default.

    A choice made at setup wins over the backend's default, on the Apple GPU too: the viewer who
    asked for large-v3 gets large-v3, and the popup says how it is doing.
    Docker and Nix pass --model (WHISPER_MODEL) and never read config.json. Like the flag, the
    configured name is not validated: it may be a folder, which is the operator's to name.
    """
    if not args.model:
        args.model = configured_model() or default_model_for(getattr(args, "device", "auto"))
    return args


def configured_cookies_browser():
    """The browser chosen for YouTube's sign-in (config.json's "cookies_from_browser"), or None.

    Only a name from COOKIE_BROWSERS counts: anything else is ignored with a warning, since yt-dlp
    would fail every download on it.
    """
    chosen = read_config().get("cookies_from_browser")
    if chosen is None or chosen == "":
        return None
    if isinstance(chosen, str) and chosen.strip().lower() in COOKIE_BROWSERS:
        return chosen.strip().lower()
    log.warning("Ignoring cookies_from_browser %r in %s: not one of %s", chosen, CONFIG_PATH, ", ".join(COOKIE_BROWSERS))
    return None


def resolve_default_cookies(args, environ=os.environ):
    """Fill in --cookies-from-browser from config.json when neither it nor --cookies was given.

    YouTube answers some addresses with "Sign in to confirm you're not a bot" until a download
    carries a signed-in browser's cookies, and the popup's Start button starts run.cmd / run.sh
    without options, so the browser chosen at setup (or with --save-cookies-from-browser) is the
    default of every start. "none" turns it off for one start. The Docker image never takes it
    from the config: it shares the data folder with the native setup (DATA_DIR in .env) but has no
    browser profile to read, and would fail every download.
    """
    flag = (args.cookies_from_browser or "").strip()
    if flag.lower() == "none":
        args.cookies_from_browser = ""
    elif flag:
        args.cookies_from_browser = flag
    elif not args.cookies and not in_container(environ):
        args.cookies_from_browser = configured_cookies_browser() or ""
    return args


def youtube_cookies(browser: str) -> tuple[int, bool]:
    """How many youtube.com cookies `browser` holds, and whether a sign-in is among them.

    Reads the browser's cookie store the way a download does (yt-dlp), counts names and never
    looks at a value; raises when the store cannot be read.
    """
    from yt_dlp.cookies import extract_cookies_from_browser

    jar = extract_cookies_from_browser(browser, logger=YtdlpLogger())
    names = set()
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            names.add(cookie.name)
    return len(names), bool(names & YOUTUBE_SIGN_IN_COOKIES)


def write_cookies_config(name) -> bool:
    """write_config() of "cookies_from_browser" for run_save_cookies(); False, with the reason
    printed, when config.json cannot be written.

    An error must not end main() on its own: exit code 1 makes run.cmd / run.sh run the command
    again every 5 seconds, reading the browser's store each time.
    """
    try:
        write_config({"cookies_from_browser": name})
    except OSError as exc:
        print(f"Could not write {CONFIG_PATH} ({exc}); it stays as it was.")
        return False
    return True


def run_save_cookies(name: str) -> int:
    """--save-cookies-from-browser NAME: make NAME's YouTube cookies the default of every start.

    NAME's store is read first (youtube_cookies()): a browser whose cookies cannot be read, or that
    holds no youtube.com cookie at all (never on YouTube, or a store it keeps from other programs,
    as Chrome and Edge do on Windows), is refused and nothing is written. "none" forgets the
    choice. Returns main()'s exit code, 0 or 2 (the code run.cmd / run.sh end on).
    """
    name = (name or "").strip().lower()
    if name == "none":
        if not write_cookies_config(None):
            return 2
        print("The server no longer sends a browser's YouTube cookies (applies from its next start).")
        return 0
    if name not in COOKIE_BROWSERS:
        print(f"'{name}' is not a browser yt-dlp reads cookies from: use one of {', '.join(COOKIE_BROWSERS)}, or none")
        return 2
    try:
        count, signed_in = youtube_cookies(name)
    except ImportError as exc:
        print(f"yt-dlp is missing ({exc}); run setup first. Nothing was saved.")
        return 2
    except Exception as exc:  # noqa: BLE001
        reason = " ".join(str(exc).split())[:200] or exc.__class__.__name__
        print(f"Could not read {name}'s cookies ({reason}). Nothing was saved.")
        return 2
    if not count:
        print(f"{name} holds no YouTube cookies (YouTube never opened there, or {name} keeps its cookies "
              "from other programs, as Chrome and Edge do on Windows). Nothing was saved.")
        return 2
    if not write_cookies_config(name):
        return 2
    if signed_in:
        print(f"From its next start the server sends {name}'s YouTube cookies (signed in) with every download.")
    else:
        print(f"From its next start the server sends {name}'s YouTube cookies with every download. "
              f"{name} is not signed in to YouTube, though: sign in there for YouTube's sign-in wall.")
    return 0


def firefox_profile_found() -> bool:
    """Whether yt-dlp would find a Firefox cookie store here; True when that cannot be told.

    Asks yt-dlp's own search (its private helpers, the folders it really reads, snap and flatpak
    included), so setup offers Firefox exactly where a download could use it; a yt-dlp without
    them only costs a question that --save-cookies-from-browser then answers.
    """
    try:
        from yt_dlp import cookies

        return any(True for _ in cookies._firefox_cookie_dbs(cookies._firefox_browser_dirs()))
    except Exception:  # noqa: BLE001
        return True


SETUP_COOKIES_TRIES = 3  # answers to setup's question before it counts as none


def run_setup_cookies(ask=input) -> int:
    """--setup-cookies, what setup runs: offer Firefox's YouTube sign-in for every download.

    Asked only where Firefox keeps a profile (Chrome and Edge lock their cookies away on Windows).
    Y saves Firefox (run_save_cookies(), which reads the store first), N forgets an earlier
    Firefox choice (another browser, saved with --save-cookies-from-browser, stays), and no answer
    at all (stdin closed, or SETUP_COOKIES_TRIES answers that are neither: an unattended setup)
    leaves config.json as it is. Always 0: setup goes on to the model download whatever happens here.
    """
    if not firefox_profile_found():
        print("Firefox was not found. If YouTube asks for a sign-in later, run.cmd / run.sh "
              "--save-cookies-from-browser <browser> lets the server use one.")
        return 0
    print("YouTube sometimes refuses downloads (\"Sign in to confirm you're not a bot\") until they carry")
    print("a signed-in browser's cookies. Should the server send Firefox's YouTube cookies with every")
    print("download? (run.cmd / run.sh --save-cookies-from-browser none undoes it)")
    # Never asked forever: a stdin that never closes and never says Y or N (an endless pipe into
    # setup.cmd; setup.sh gives a piped stdin's question an EOF itself) counts as no answer.
    for _ in range(SETUP_COOKIES_TRIES):
        try:
            answer = ask("Type Y or N: ").strip().lower()
        except EOFError:
            print()
            return 0
        if answer in ("y", "yes"):
            run_save_cookies("firefox")
            return 0
        if answer in ("n", "no"):
            # The question named Firefox: a no is no answer about any other browser.
            if configured_cookies_browser() == "firefox":
                run_save_cookies("none")
            return 0
    print("No Y or N: config.json stays as it is (run.cmd / run.sh --save-cookies-from-browser firefox saves it later).")
    return 0


def rocm_engine_line() -> Optional[str]:
    """What --check and a server start say about the AMD engine; None where it is not installed.

    In use, it names the CTranslate2 build in ROCM_DIR; installed but off, it says why (ROCM_REASON:
    no passed test, another Python, the crash guard). A machine without the side folder, every
    NVIDIA and CPU one, hears nothing about it.
    """
    if ROCM_ACTIVE:
        version = read_json_object(ROCM_DIR / ROCM_MANIFEST).get("ctranslate2") or "?"
        return f"GPU engine: AMD ROCm (CTranslate2 {version} from {ROCM_DIR})"
    try:
        installed = ROCM_DIR.is_dir()
    except OSError:
        installed = True  # there, but not for this user to read: ROCM_REASON says what the check met
    return f"GPU engine: AMD ROCm installed but not used: {ROCM_REASON}" if installed else None


def kitsune_runtime_line() -> str:
    """What --check says about the Kitsune models' runtime: PyTorch and transformers, and whether PyTorch sees a GPU."""
    if kitsune_runtime_missing():
        return "Kitsune models: PyTorch not installed (server/kitsune_setup.py installs it; Whisper models work without)"
    try:
        import torch
        import transformers

        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
        return (f"Kitsune models: PyTorch {torch.__version__}, transformers {transformers.__version__}, "
                + (f"GPU {gpu}" if gpu else "no CUDA GPU (CPU only)"))
    except Exception as exc:  # noqa: BLE001
        return f"Kitsune models: PyTorch import failed: {exc}"


def run_check(device: str = "auto") -> None:
    print(f"Python {sys.version.split()[0]} at {sys.executable}")
    print(f"Data directory: {APP_DIR}")
    print(f"NVIDIA library directories registered: {len(NVIDIA_DIRS)}")
    n = None
    try:
        import ctranslate2

        n = ctranslate2.get_cuda_device_count()
        # With the AMD engine a start that sees no GPU hands over to the default engine
        # (check_rocm_import()), said below; on a Mac the Apple GPU runs it instead.
        if n or ROCM_ACTIVE:
            fallback = ""
        elif mlx_available():
            fallback = "  -> the Apple GPU runs it instead"
        else:
            fallback = "  -> CPU fallback; consider --model small"
        print(f"CTranslate2 {ctranslate2.__version__}: {n} CUDA device(s){fallback}")
    except Exception as exc:  # noqa: BLE001
        print(f"CTranslate2 import failed: {exc}")
    engine = rocm_engine_line()
    if engine:
        print(engine)
    if ROCM_ACTIVE and n == 0:
        print("The AMD engine sees no AMD GPU, so a start leaves it for the default engine "
              "(server/amd_setup.py --probe says why)")
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
    print(kitsune_runtime_line())
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
    print(f"Default model: {chosen or default_model_for(device)} " + ("(chosen at setup)" if chosen else "(built-in default)"))
    browser = configured_cookies_browser()
    if browser:
        print(f"YouTube sign-in: {browser}'s cookies go with every download (chosen at setup)"
              + ("; not inside this container" if in_container() else ""))
    elif in_container():
        print(f"YouTube sign-in: none (if YouTube asks for one: a cookies.txt in the data folder and {DOCKER_COOKIES_FILE})")
    else:
        print(f"YouTube sign-in: none (if YouTube asks for one: {save_cookies_command()})")
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


def port_number(text: str) -> int:
    """--port's type: a TCP port, 1-65535. Refused while the arguments are read, before the instance lock
    and the model load: the listening socket raises OverflowError, not OSError, for one out of range."""
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"{text} is not a port (1-65535)")
    return value


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Shisu-ko: local transcription server for the Firefox extension")
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep it local)")
    p.add_argument("--port", type=port_number, default=8790, help="default 8790 (8765 is left free for AnkiConnect)")
    p.add_argument("--model", default=None, help="a Kitsune-Transcribe model (Japanese only, needs PyTorch), e.g. kitsune-0.6b, kitsune-0.3b-int8, kitsune-0.1b-nvfp4; "
                                                 "a faster-whisper model size or CTranslate2 repo, e.g. large-v3, large-v3-turbo, kotoba-tech/kotoba-whisper-v2.0-faster; "
                                                 "or a local folder holding either (default: the model chosen at setup (config.json), else large-v3)")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "mlx", "cpu"],
                   help="auto picks an NVIDIA GPU, else the Apple GPU through MLX, else the CPU")
    p.add_argument("--compute-type", default="auto", help="Whisper: float16, int8_float16, int8, ... (auto = float16 on GPU, int8 on CPU); "
                                                          "MLX: float16 or float32; Kitsune: bfloat16, float16 or float32 (auto = bfloat16 on a GPU that has it, else float16, "
                                                          "and float16 for an -fp16 model; float32 on CPU); "
                                                          "a Kitsune model's weight precision is in its name")
    p.add_argument("--language", default="ja")
    p.add_argument("--language-patience", type=float, default=60.0,
                   help="seconds of speech in another language before subtitles stop for that video (0 = never detect, always transcribe)")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--lyrics", default="auto", choices=["auto", "off"],
                   help="auto: a window in which the speech detector hears next to nothing (under a second of speech) "
                        "but the audio is not silent, sung lyrics or speech over music, is transcribed without the "
                        "detector when Whisper hears the target language in it, under stricter gates; off: such "
                        "windows go through the detector as before, blank when it heard nothing")
    p.add_argument("--sentence-ends", default="auto", choices=["auto", "off"],
                   help="auto: write a sentence mark where Whisper left one out, when a word ending in a "
                        "sentence-final expression (よね, です, か, a plain form) is followed by a pause; "
                        "off: cut and merge lines on Whisper's own punctuation alone")
    p.add_argument("--initial-prompt", default=None,
                   help="text prompt given to Whisper for every window (default: a short punctuated sentence "
                        "in --language, see DEFAULT_PROMPTS; pass an empty string for none)")
    p.add_argument("--window", type=float, default=30.0, help="seconds of audio transcribed per step (shorter reacts faster to seeking; 30 is faster-whisper's own chunk, and the initial prompt reaches only the first chunk of a window)")
    p.add_argument("--first-window", type=float, default=20.0, help="shorter first step after a seek so subtitles appear quickly")
    p.add_argument("--lookahead", type=float, default=900.0, help="stop transcribing this many seconds ahead of the playhead (0 = whole video)")
    p.add_argument("--max-cue-chars", type=int, default=30, help="26 is the Netflix Japanese limit (13 x 2 lines); 30 keeps more mined sentences whole")
    p.add_argument("--max-cue-seconds", type=float, default=7.0)
    p.add_argument("--min-cue-seconds", type=float, default=0.8, help="cues shorter than this are extended or merged")
    p.add_argument("--idle-minutes", type=int, default=30, help="release decoded audio of videos not synced for this long")
    p.add_argument("--retry-after", type=float, default=30.0, help="the longest wait between two attempts at a failed audio fetch (the waits start at 2 s and double; a permanent error is not retried), and the least time between two attempts to load a model that failed to download or load")
    p.add_argument("--client-timeout", type=float, default=30.0, help="stop transcribing ahead for a video whose tab has not synced for this many seconds (0 = never stop)")
    p.add_argument("--cpu-threads", type=int, default=0)
    p.add_argument("--cookies-from-browser", default="",
                   help="e.g. firefox: send that browser's YouTube cookies with every download, for YouTube's sign-in wall, "
                        "age-restricted or members-only videos (default: the browser chosen at setup, config.json; none for no browser)")
    p.add_argument("--save-cookies-from-browser", metavar="NAME",
                   help="make NAME (firefox, chrome, ...) the browser whose YouTube cookies every later start sends, the popup's "
                        "Start button included, after checking that its cookies can be read; none forgets it")
    p.add_argument("--setup-cookies", action="store_true", help="ask whether to send Firefox's YouTube cookies; used by setup")
    p.add_argument("--cookies", default="", help="path to a Netscape-format cookies.txt for yt-dlp (use this inside Docker, e.g. /data/cookies.txt)")
    p.add_argument("--js-runtime", default="auto", help="JS runtime for yt-dlp: auto, node, deno, bun, or name:path")
    p.add_argument("--allow-remote-ejs", action="store_true", help="let yt-dlp fetch updated challenge-solver scripts from GitHub")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--check", action="store_true", help="print environment diagnostics and exit")
    p.add_argument("--default-model", action="store_true",
                   help="print this machine's built-in default model and exit, ignoring config.json; setup asks this rather than keeping a copy of the rule")
    p.add_argument("--download-model", metavar="NAME", help="download NAME now, showing progress, and make it the default model for later starts; used by setup")
    # amd_setup.py's test of the AMD engine (run_probe_gpu()), with SHISUKO_ENGINE=rocm; not for the viewer.
    p.add_argument("--probe-gpu", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-update", action="store_true", help="start without looking for a newer version first (run.cmd / run.sh skip server/update.py) and refuse the popup's Update button (POST /update answers 409), since the launcher would restart the server without updating")
    args = p.parse_args(argv)
    if args.initial_prompt is None:
        args.initial_prompt = DEFAULT_PROMPTS.get(args.language, "")
    return resolve_default_cookies(resolve_default_model(args))


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

    # Every way out of main() goes through finish() (hard_exit() for --probe-gpu), and so does a
    # Ctrl+C or Ctrl+Break while the model loads with the AMD engine on Windows: the interpreter's
    # own exit can hang there, and they end the process instead. entry_point() catches whatever
    # still gets out.
    if getattr(args, "default_model", False):
        # The built-in default, not what a start would load: setup.sh reads this to name the
        # choice it is about to offer, and config.json is what that choice will write.
        print(default_model_for(args.device))
        finish()
        return
    if args.check:
        run_check(args.device)
        finish()
        return
    if getattr(args, "download_model", None) is not None:
        finish(run_download_model(args.download_model, args.device))
    if getattr(args, "probe_gpu", False):
        def cancelled() -> None:
            # amd_setup.py's wait for this process cannot be interrupted on Windows, and the
            # interpreter's exit could hang there: the test ends through hard_exit() all the same.
            # run_probe_gpu() took the engine out of config.json before the load.
            print("\nThe AMD GPU engine test was cancelled", flush=True)
            flush_output()
            hard_exit(1)

        end_on_ctrl_c(cancelled)  # no restore: the process ends right after the test
        try:
            code = run_probe_gpu(args)
        except KeyboardInterrupt:
            cancelled()  # inside the except clause, while its traceback still holds what it holds
        flush_output()  # os._exit() leaves unwritten what the buffers still hold
        hard_exit(code)
    if getattr(args, "save_cookies_from_browser", None) is not None:
        finish(run_save_cookies(args.save_cookies_from_browser))
    if getattr(args, "setup_cookies", False):
        finish(run_setup_cookies())
    if getattr(args, "cookies_from_browser", ""):
        log.info("Downloads send %s's YouTube cookies", args.cookies_from_browser)
    engine = rocm_engine_line()
    if engine:
        log.info("%s", engine)

    if not hold_instance_lock(args.port):
        log.error("Another server is already starting or running on port %d (it holds %s). Stop it first.",
                  args.port, instance_lock_path(args.port))
        finish(2)
    counted = False

    def interrupted() -> None:
        # A Ctrl+C while the model loads with the AMD engine on Windows (end_on_ctrl_c(), or the
        # except clause below): the viewer's, not a crash of the engine, so the guard takes it back.
        if counted:
            uncount_rocm_start()
        log.info("Shutting down")
        finish(0)  # 0, as a Ctrl+C while serving ends; the interpreter's exit can hang with the AMD engine on Windows

    previous = end_on_ctrl_c(interrupted)
    try:
        if ROCM_ACTIVE:
            if args.device != "cpu":
                counted = count_rocm_start()  # the crash guard; load_model() takes it back once the GPU got through
            check_rocm_import(args.device)  # ends the process with code 3, the engine off, when it cannot run here
        app = start_app(args)
    except KeyboardInterrupt:
        if not rocm_on_windows():
            if counted:
                uncount_rocm_start()
            raise  # everywhere else the interpreter's own exit, as before
        interrupted()
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)  # serving, a Ctrl+C raises again: server_close(), then finish()
            signal.signal(signal.SIGBREAK, previous)  # and so does a Ctrl+Break, never the console's ExitProcess
    Handler.app = app
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except (OSError, OverflowError, ValueError, TypeError) as exc:
        # OverflowError: a port out of range; ValueError / TypeError: a host that cannot be encoded.
        log.error("Cannot listen on %s:%d (%s). Is another server already running?", args.host, args.port, exc)
        finish(2)
    server.daemon_threads = True
    log.info("Listening on http://%s:%d  (Ctrl+C to stop)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        server.server_close()
    # POST /update sets exit_code: the launcher reads EXIT_UPDATE as "run update.py, then start
    # again". Every worker is a daemon thread, so the interpreter does not wait for a window to
    # finish. Without one, main() simply returns (finish(None)).
    finish(app.exit_code)


def entry_point() -> None:
    """main() as `python server.py` runs it: with the AMD engine on Windows nothing leaves it for the interpreter.

    main() ends through finish() and hard_exit(), but an exception from serve_forever(), or a
    Ctrl+C between start_app() and serve_forever(), would still end through the interpreter's own
    exit, which frees the model and hangs with the ROCm runtime on Windows (CTranslate2 #2038,
    #2085): run.cmd would wait for ever instead of starting the server again. Everywhere else it
    is the interpreter's exit, as before.
    """
    try:
        main()
    except SystemExit:
        raise  # argparse, before anything loads, or a finish() whose TerminateProcess failed
    except KeyboardInterrupt:
        if not rocm_on_windows():
            raise
        log.info("Shutting down")
        finish(0)
    except BaseException:  # noqa: BLE001
        if not rocm_on_windows():
            raise  # the traceback and exit code 1, as before
        log.exception("The server stopped on an unexpected error")
        finish(1)  # the interpreter's code for it: run.cmd says "stopped unexpectedly" and starts it again


if __name__ == "__main__":
    entry_point()
