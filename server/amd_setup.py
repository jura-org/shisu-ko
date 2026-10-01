#!/usr/bin/env python3
"""Offers the experimental AMD GPU engine when setup finds an AMD graphics card.

The server runs Whisper through CTranslate2, whose PyPI build speaks CUDA only. CTranslate2 also
publishes a build for AMD cards (ROCm/HIP), as zips on its GitHub release rather than on PyPI, and
on Windows that build needs AMD's own runtime wheels from repo.radeon.com. setup.cmd / setup.sh run
this file after the model download. Where it finds a card that build can run on, it says so, asks,
downloads the pinned files (each checked against its size and SHA-256 in PINS), installs them with
pip into a side folder, ~/.shisu-ko/rocm, and has server.py test the engine in a child process
(`server.py --probe-gpu`). Only a test that passes switches the server over (config.json
"engine": "rocm"), and a card or driver that is not up to it kills that child, not the server. The
venv's own CTranslate2 is never touched, so the NVIDIA and CPU paths stay exactly as they were.

Experimental: not yet tested on AMD hardware by the maintainer.

  amd_setup.py            look for a card, ask, download, install, test
  amd_setup.py --yes      the same without the question; exit 1 unless the engine ends up working
  amd_setup.py --ignore-old-graphics
                          also install on a card the lists below call unsupported (an older Radeon,
                          RX 6000 on Windows, ...); combines with --yes; --ignore_old_graphics too
  amd_setup.py --probe    test the installed engine again; exit 1 when the test fails
  amd_setup.py --status   what is detected, installed and switched on; nothing is downloaded
  amd_setup.py --remove   switch the engine off and delete the side folder

Without --yes or --probe it always exits 0, whatever happened, and says why it installed nothing:
the setup scripts must never fail on it.

Stdlib only, on purpose, and it never imports server.py: it must run even when the server's own
requirements are broken, so it runs server.py only as the child process that tests the engine.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import ntpath
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

# Every file the AMD engine is installed from, pinned in this one place: versions, URLs, sizes in
# bytes and SHA-256 digests. The CTranslate2 zips' digests are the ones GitHub lists for the release
# assets. AMD publishes no digests for its wheels, so the maintainer pinned those from a download.
# A digest that is not 64 hex characters is refused before anything is downloaded, so a pin that
# was never filled in can never pass a check.
PINS = {
    "ctranslate2": "4.8.2",
    "rocm": "7.2.1",  # AMD's Windows runtime wheels; on Linux the system's own ROCm 7.2.x is used
    "files": {
        "win_amd64": [
            {
                "url": "https://github.com/OpenNMT/CTranslate2/releases/download/v4.8.2/"
                       "rocm-python-wheels-Windows.zip",
                "size": 137_538_158,
                "sha256": "43da4baa5feaee49f77e176277a9647f99c493173c81a0bc60f491cac97532c2",
            },
            {
                "url": "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/"
                       "rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
                "size": 644_793_492,
                "sha256": "f68989d48df71cbfc3cb68bf705dc37c0f56e9666feddb59a1a0f5ff7539fe1c",
            },
            {
                "url": "https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/"
                       "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
                "size": 489_964_648,
                "sha256": "c7fe0b0731af8896093ff69e11496830d3cb6a4aed73e895c60b7cbdc200be92",
            },
        ],
        "linux_x86_64": [
            {
                "url": "https://github.com/OpenNMT/CTranslate2/releases/download/v4.8.2/"
                       "rocm-python-wheels-Linux.zip",
                "size": 284_315_912,
                "sha256": "b469e765f74ef85fb97bf4fe2347c8c6296dda00d7f5cf20658f23e95004a925",
            },
        ],
    },
    # The side folder once installed, in bytes: the pinned files (with the cp312 CTranslate2 wheel)
    # installed by pip_command() and the marker, measured. Linux is not measured yet; install() checks
    # the room for what the wheels unpack to before pip runs, on both.
    "installed_size": {
        "win_amd64": 3_902_512_758,
    },
}

WINDOWS, LINUX = "win_amd64", "linux_x86_64"  # the platform names of shisuko-rocm.json
SUPPORTED, UNSUPPORTED, UNKNOWN = "supported", "unsupported", "unknown"
# The flag that installs on a card the lists call unsupported (setup()'s `ignore_old`).
IGNORE_OLD_FLAG = "--ignore-old-graphics"
# Next to an NVIDIA GPU only --yes installs at all, so a card the lists rule out there needs both.
BESIDE_NVIDIA = f"--yes {IGNORE_OLD_FLAG}"
SCRIPT = Path(__file__).resolve()
SERVER_PY = SCRIPT.parent / "server.py"
ROCM_DIR_NAME = "rocm"                # APP_DIR / "rocm", the side folder server.py puts first on sys.path
MARKER_NAME = "shisuko-rocm.json"     # what is in the side folder, for server.py's Python tag check
GUARD_NAME = "rocm-starts"            # server.py's crash guard: starts with the AMD engine that never loaded
GUARD_LIMIT = 2                       # server.py's ROCM_GUARD_LIMIT: from this count on it leaves the engine off
CONFIG_NAME = "config.json"
# AMD's guide for ROCm 7.2, the line the pinned wheels are built on: its "latest" guide installs
# ROCm 10, and tells the reader to remove 7.2 first.
ROCM_INSTALL_URL = "https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.4/"
DEFAULT_ROCM = "/opt/rocm"  # where server.py looks for ROCm on Linux without ROCM_PATH
GROUPS_HINT = 'sudo usermod -aG render,video "$USER", then log out and in again'
# What the server runs on without the AMD engine, where this file has not looked for an NVIDIA GPU.
DEFAULT_ENGINE = "its default engine (an NVIDIA GPU where there is one, else the CPU)"
# The ROCm libraries CTranslate2's Linux wheel is linked against, under $ROCM_PATH/lib.
LINUX_LIBRARIES = ("libamdhip64.so.7", "libhipblas.so.3", "libhiprand.so.1")
CIM_QUERY = "Get-CimInstance Win32_VideoController | Select-Object Name, PNPDeviceID | ConvertTo-Json -Compress"
CIM_TIMEOUT = 20.0       # seconds; the query answers in about one
# The name Windows gives a card whose own driver is not installed; an AMD one keeps its VEN_1002.
BASIC_DISPLAY = "microsoft basic display adapter"
# An interpreter path that Command Prompt and PowerShell both run when it is typed without quotes.
BARE_WORD_RE = re.compile(r"[A-Za-z0-9_.:\\/+-]+")
# What PowerShell expands between double quotes; Command Prompt takes both as they are.
PS_EXPANDS = ("$", "`")
SOCKET_TIMEOUT = 60.0    # seconds without a byte before a download counts as failed
DOWNLOAD_RETRIES = 1     # one more try after a failed download; a wrong digest is never retried
PROBE_TIMEOUT = 900.0    # seconds for the child to load the model on the GPU and transcribe a little
# Seconds per wait for the child: on Windows Ctrl+C reaches this process only once a wait returns
# (server.py's wait_for_thread()), and one wait of PROBE_TIMEOUT would sit through it.
PROBE_WAIT_STEP = 1.0
CHUNK = 1024 * 1024
USER_AGENT = "shisu-ko-amd-setup"
AMD_VENDOR, NVIDIA_VENDOR = "0x1002", "0x10de"
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
# The GPU targets CTranslate2's ROCm build is compiled for (its prepare_build_environment_*_rocm.sh).
SUPPORTED_GFX = ("gfx1030", "gfx1100", "gfx1101", "gfx1102", "gfx1150", "gfx1151", "gfx1200", "gfx1201")
# RX 6600/6700: not a target, but reported to run as gfx1030 when told to.
OVERRIDE_GFX = ("gfx1031", "gfx1032")
# ctranslate2-4.8.2-cp312-cp312-win_amd64.whl; the free-threaded wheel is ...-cp314-cp314t-...
WHEEL_RE = re.compile(
    r"(?P<name>[A-Za-z0-9_]+)-(?P<version>[0-9][0-9A-Za-z.+]*)-(?P<python>[a-z0-9]+)-(?P<abi>[a-z0-9]+)"
    r"-(?P<platform>[A-Za-z0-9_.]+)\.whl"
)


def _patterns(*pairs: tuple[str, str]) -> tuple:
    return tuple((re.compile(pattern), family) for pattern, family in pairs)


# Windows has no GPU architecture to read without the ROCm runtime, so the adapter's name decides.
# AMD's Windows runtime wheels carry rocBLAS kernels for gfx1100/1101/1102/1150/1151/1200/1201 only,
# so RX 6000 (gfx1030), which the Linux build runs, is out here. Matched against
# normalise_adapter_name(); the supported list is tried first ("r9700" would pass for an old R9).
WINDOWS_SUPPORTED = _patterns(
    (r"\brx\s?7\d{3}(?!\d)", "Radeon RX 7000 (RDNA 3)"),
    (r"\brx\s?9\d{3}(?!\d)", "Radeon RX 9000 (RDNA 4)"),
    (r"\bpro\s?w7\d{3}(?!\d)", "Radeon PRO W7000 (RDNA 3)"),
    (r"\bpro\s?w9\d{3}(?!\d)", "Radeon PRO W9000 (RDNA 4)"),
    (r"\br9700(?!\d)", "Radeon AI PRO R9700 (RDNA 4)"),
    (r"\b(?:890|880)m\b", "Radeon 890M/880M (Ryzen AI 300, Strix Point)"),
    (r"\b(?:8060|8050|8040)s\b", "Radeon 8060S/8050S/8040S (Ryzen AI Max, Strix Halo)"),
)
WINDOWS_UNSUPPORTED = _patterns(
    (r"\brx\s?6\d{3}(?!\d)", "Radeon RX 6000 (RDNA 2)"),
    (r"\bpro\s?w6\d{3}(?!\d)", "Radeon PRO W6000 (RDNA 2)"),
    (r"\brx\s?5\d{3}(?!\d)", "Radeon RX 5000 (RDNA 1)"),
    (r"\bpro\s?w5\d{3}(?!\d)", "Radeon PRO W5000 (RDNA 1)"),
    (r"\bvega\b", "Radeon Vega"),
    (r"\b(?:780|760|740)m\b", "Radeon 780M/760M/740M (gfx1103)"),
    (r"\b(?:680|660|610)m\b", "Radeon 680M/660M/610M (RDNA 2)"),
    (r"\b(?:860|840|820)m\b", "Radeon 860M/840M/820M (Krackan Point, gfx1152)"),
    (r"\brx\s?[45]\d{2}(?!\d)|\br[579](?:\s|$|\d{3}\b)|\bhd\s?\d{4}\b|\bfirepro\b|\bpro\s?wx\b",
     "an older Radeon"),
)


class SetupError(Exception):
    """A step that failed; the message is the line that says why."""


class ChecksumError(SetupError):
    """A download that is not the pinned file. Never retried: the same bytes would come again."""


class DetectError(SetupError):
    """The graphics adapters could not be listed, which says nothing about whether an AMD GPU is there."""


@dataclass(frozen=True)
class Card:
    name: str
    gfx: Optional[str] = None  # Linux: the kfd architecture (gfx1100); Windows: None


def say(message: str) -> None:
    print(f"[amd] {message}", flush=True)


def app_dir(environ=os.environ) -> Path:
    """The data directory, as server.py's APP_DIR: SHISUKO_HOME, else ~/.shisu-ko."""
    return Path(environ.get("SHISUKO_HOME") or (Path.home() / ".shisu-ko"))


def rocm_dir(home: Path) -> Path:
    return home / ROCM_DIR_NAME


def download_dir(home: Path) -> Path:
    return home / "cache" / "rocm-download"


def ps_quote(text) -> str:
    """`text` as a PowerShell literal: in single quotes, each quote mark in it doubled (PowerShell
    takes the typographic single quotes for quotes too)."""
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", str(text)) + "'"


def rerun_line(python: str, script: Path = SCRIPT, flag: str = "", windows: Optional[bool] = None) -> str:
    """The line that runs `script` with `python`, for the messages that tell the user to.

    On Windows the line has to work wherever it is pasted: Command Prompt, or PowerShell, which
    Windows 11's Terminal opens by default and which refuses a line that starts with a quoted string.
    An interpreter path both take without quotes is given bare; one that needs them (a space in the
    user name) gets PowerShell's call operator, and the line says what Command Prompt leaves out.
    PowerShell expands $ and ` between double quotes, which Command Prompt needs, and Command Prompt
    takes no single quotes: a path with either gets one line for each.
    """
    windows = os.name == "nt" if windows is None else windows
    tail = f" {flag}" if flag else ""
    if not windows:
        return " ".join(shlex.quote(str(part)) for part in (python, script)) + tail
    bare = BARE_WORD_RE.fullmatch(python) is not None
    if not any(mark in str(part) for part in (python, script) for mark in PS_EXPANDS):
        if bare:
            return f'{python} "{script}"{tail}'
        return f'& "{python}" "{script}"{tail} (in PowerShell; in Command Prompt leave out the "& ")'
    run = python if bare else f"& {ps_quote(python)}"
    return f'{run} {ps_quote(script)}{tail} (in PowerShell; in Command Prompt: "{python}" "{script}"{tail})'


def command(flag: str = "") -> str:
    """How to run this file again, with this Python, for the lines that tell the user to."""
    return rerun_line(sys.executable, SCRIPT, flag)


# --- config.json, the marker, the crash guard -----------------------------------------------------

def read_config(path: Path) -> dict:
    """config.json as server.py's read_config() reads it: {} when missing, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        say(f"ignoring {path} ({exc})")
        return {}
    return data if isinstance(data, dict) else {}


def write_config(path: Path, patch: dict) -> None:
    """Merge `patch` into config.json exactly as server.py's write_config() does: a None value drops
    its key, and the file is written beside itself and moved into place in one step."""
    data = read_config(path)
    for key, value in patch.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def switch_off(home: Path) -> bool:
    """Drop config.json's "engine" key. True when it was there; a file without it is left unwritten."""
    path = home / CONFIG_NAME
    if "engine" not in read_config(path):
        return False
    write_config(path, {"engine": None})
    return True


def python_tag(version_info=sys.version_info, gil_disabled: Optional[bool] = None) -> str:
    """The running interpreter's wheel tag: cp312, or cp314t for a free-threaded build.

    server.py's python_tag() must give the same string: the side folder is used only when its
    marker names the Python that runs the server.
    """
    if gil_disabled is None:
        gil_disabled = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    return f"cp{version_info[0]}{version_info[1]}" + ("t" if gil_disabled else "")


def platform_key(platform: Optional[str] = None) -> Optional[str]:
    """win_amd64 or linux_x86_64 for the Python running this, None where no AMD build exists
    (macOS, ARM, a 32-bit Python)."""
    platform = sysconfig.get_platform() if platform is None else platform
    return {"win-amd64": WINDOWS, "linux-x86_64": LINUX}.get(platform)


def pinned_rocm(platform: Optional[str]) -> str:
    """The marker's "rocm": AMD's pinned runtime on Windows, the system's own ROCm on Linux."""
    return PINS["rocm"] if platform == WINDOWS else "system"


def marker(platform: str, tag: str, now: Optional[datetime] = None) -> dict:
    return {
        "ctranslate2": PINS["ctranslate2"],
        "rocm": pinned_rocm(platform),
        "python": tag,
        "platform": platform,
        "installed": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }


def read_marker(folder: Path) -> Optional[dict]:
    try:
        data = json.loads((folder / MARKER_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def marker_usable(info: Optional[dict], folder: Path, platform: Optional[str], tag: str) -> bool:
    """True when server.py uses the side folder once it is switched on (its rocm_engine_state()): the
    package is there and was installed for this Python and platform, whichever pins it came from."""
    return (bool(info) and platform is not None and (folder / "ctranslate2" / "__init__.py").is_file()
            and info.get("python") == tag and info.get("platform") == platform)


def marker_current(info: Optional[dict], folder: Path, platform: str, tag: str) -> bool:
    """True when the side folder holds this file's pinned engine, built for this Python."""
    if not marker_usable(info, folder, platform, tag):
        return False
    return info.get("ctranslate2") == PINS["ctranslate2"] and info.get("rocm") == pinned_rocm(platform)


def read_guard(home: Path) -> int:
    """server.py's count of AMD-engine starts that never loaded their model; missing or junk is 0."""
    try:
        count = int((home / GUARD_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return max(count, 0)


# --- detection -----------------------------------------------------------------------------------

def cim_adapters(json_text: str) -> list[tuple[str, str]]:
    """(Name, PNPDeviceID upper-cased) of each adapter in PowerShell's ConvertTo-Json output, which is
    an object for one adapter and a list for several; [] for anything else."""
    try:
        data = json.loads(json_text.strip().lstrip("\ufeff"))
    except (AttributeError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    adapters = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name, pnp = item.get("Name"), item.get("PNPDeviceID")
        adapters.append((name.strip() if isinstance(name, str) else "",
                         pnp.upper() if isinstance(pnp, str) else ""))
    return adapters


def amd_adapters_from_cim(json_text: str) -> list[str]:
    """The names of the AMD adapters (PCI vendor 1002) in the Win32_VideoController query's JSON."""
    return [name or "an AMD graphics adapter" for name, pnp in cim_adapters(json_text) if "VEN_1002" in pnp]


def nvidia_in_cim(json_text: str) -> bool:
    return any("VEN_10DE" in pnp for _, pnp in cim_adapters(json_text))


def powershell(environ=os.environ, isfile: Callable = os.path.isfile) -> str:
    """Windows PowerShell by its full path, not whatever "powershell" the folder this runs in or
    PATH holds first; the bare name where that file is missing."""
    path = ntpath.join(environ.get("SystemRoot") or "C:\\Windows", "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    return path if isfile(path) else "powershell"


def cim_json(run: Optional[Callable] = None) -> str:
    """The Win32_VideoController query's output ("" or a list without AMD for a machine without one).

    Raises DetectError when PowerShell cannot be started, takes too long, or fails without printing
    anything: that is not the same as finding no AMD GPU, and must not be reported as such.
    """
    run = run or subprocess.run
    try:
        result = run([powershell(), "-NoProfile", "-NonInteractive", "-Command", CIM_QUERY],
                     capture_output=True, timeout=CIM_TIMEOUT)
    except OSError as exc:
        raise DetectError(f"PowerShell could not be started ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise DetectError(f"PowerShell did not answer within {CIM_TIMEOUT:.0f} s") from exc
    except subprocess.SubprocessError as exc:
        raise DetectError(f"PowerShell failed ({exc})") from exc
    out = result.stdout or b""
    # The names are ASCII in practice; the console code page may mangle a (R) sign, never a match.
    text = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out
    if result.returncode != 0 and not text.strip():
        raise DetectError(f"the graphics-adapter query failed (exit {result.returncode})")
    return text


def gfx_name(version: int) -> str:
    """kfd's gfx_target_version as an LLVM target name: 110000 is gfx1100, 90010 gfx90a, 110501 gfx1151."""
    major, minor, stepping = version // 10000, version // 100 % 100, version % 100
    return f"gfx{major}{minor:x}{stepping:x}"


def gfx_targets(properties_texts: Iterable[str]) -> list[str]:
    """The GPU targets of the kfd topology nodes' properties files, in order, each once.

    A CPU node has gfx_target_version 0 and is left out, as is a node without the line.
    """
    found: list[str] = []
    for text in properties_texts:
        match = re.search(r"^gfx_target_version\s+(\d+)\s*$", text, re.MULTILINE)
        if not match or int(match.group(1)) <= 0:
            continue
        name = gfx_name(int(match.group(1)))
        if name not in found:
            found.append(name)
    return found


def drm_vendors(drm_root: Path) -> list[str]:
    """The PCI vendor ids ("0x1002") of the cards under /sys/class/drm."""
    vendors = []
    for card in sorted(drm_root.glob("card*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue  # card0-DP-1 and the like are connectors
        try:
            vendors.append((card / "device" / "vendor").read_text(encoding="ascii", errors="replace").strip().lower())
        except OSError:
            continue
    return vendors


def kfd_properties(nodes_root: Path) -> list[str]:
    texts = []
    for path in sorted(nodes_root.glob("*/properties"), key=lambda p: (len(p.parent.name), p.parent.name)):
        try:
            texts.append(path.read_text(encoding="ascii", errors="replace"))
        except OSError:
            continue
    return texts


def pci_display_vendors(pci_root: Path) -> set[str]:
    """The vendor ids ("0x10de") of the display controllers (PCI class 0x03xxxx) on the bus, whatever
    driver runs them. The class leaves out a card's other functions (HDMI audio, USB), which carry
    its vendor id too."""
    vendors = set()
    for device in pci_root.glob("*"):
        try:
            if (device / "class").read_text(encoding="ascii", errors="replace").strip().lower().startswith("0x03"):
                vendors.add((device / "vendor").read_text(encoding="ascii", errors="replace").strip().lower())
        except OSError:
            continue
    return vendors


def detect_linux(sys_root: Path = Path("/sys")) -> tuple[list[Card], bool]:
    """The AMD GPUs (named by their kfd target) and whether an NVIDIA GPU is there too.

    The NVIDIA GPU is looked for on the PCI bus, which also lists one without nvidia-drm (headless),
    and not by nvidia-smi, which stays installed after the card is gone.
    """
    vendors = drm_vendors(sys_root / "class" / "drm")
    targets = gfx_targets(kfd_properties(sys_root / "class" / "kfd" / "kfd" / "topology" / "nodes"))
    cards = [Card(f"an AMD GPU ({gfx})", gfx) for gfx in targets]
    if not cards and AMD_VENDOR in vendors:
        cards = [Card("an AMD GPU", None)]  # the driver is there, its compute interface (kfd) is not
    nvidia = NVIDIA_VENDOR in vendors or NVIDIA_VENDOR in pci_display_vendors(sys_root / "bus" / "pci" / "devices")
    return cards, nvidia


def detect(platform: str) -> tuple[list[Card], bool]:
    """The AMD GPUs of this machine and whether an NVIDIA GPU is there too. Raises DetectError."""
    if platform == WINDOWS:
        text = cim_json()
        return [Card(name) for name in amd_adapters_from_cim(text)], nvidia_in_cim(text)
    if platform == LINUX:
        return detect_linux()
    return [], False


def rocm_root(environ=os.environ) -> Path:
    return Path(environ.get("ROCM_PATH") or DEFAULT_ROCM)


def missing_libraries(environ=os.environ) -> list[str]:
    """server.py's missing_rocm_libraries(): the LINUX_LIBRARIES that are not under rocm_root()/lib.
    Without them server.py keeps its default engine, however the AMD one is switched."""
    lib = rocm_root(environ) / "lib"
    return [name for name in LINUX_LIBRARIES if not (lib / name).exists()]


def login_profile(environ=os.environ, home: Optional[Path] = None) -> str:
    """The file a login shell reads, where a variable every server start needs goes: the desktop
    session, and with it the Start button, gets its environment from one. zsh reads ~/.zprofile;
    bash reads the first of ~/.bash_profile, ~/.bash_login and ~/.profile that is there."""
    shell = Path(environ.get("SHELL") or "").name
    if shell == "zsh":
        return "~/.zprofile"
    if shell == "bash":
        home = Path.home() if home is None else home
        for name in (".bash_profile", ".bash_login"):
            if (home / name).exists():
                return f"~/{name}"
    return "~/.profile"


def linux_missing(environ=os.environ, kfd: Path = Path("/dev/kfd"), access: Callable = os.access) -> list[str]:
    """What the Linux engine still needs from the system, one line each; [] when nothing is missing."""
    missing = []
    if not kfd.exists():
        missing.append(f"{kfd}, the amdgpu driver's compute interface (AMD's ROCm install sets it up)")
    elif not access(kfd, os.R_OK | os.W_OK):
        missing.append(f"access to {kfd}: join the render and video groups ({GROUPS_HINT})")
    absent = missing_libraries(environ)
    if absent:
        missing.append(f"ROCm 7.2.x: {', '.join(absent)} not found under {rocm_root(environ) / 'lib'} (if it lives "
                       f"elsewhere, export ROCM_PATH in {login_profile(environ)}: every server start needs it, the "
                       "Start button's too)")
    return missing


def normalise_adapter_name(name: str) -> str:
    text = name.lower()
    for mark in ("(tm)", "(r)", "\u2122", "\u00ae"):  # also the trade mark and registered signs
        text = text.replace(mark, " ")
    return " ".join(text.split())


def driverless(card: Card) -> bool:
    """An AMD card (Windows) that runs on Microsoft's fallback driver: the ROCm runtime cannot see it."""
    return normalise_adapter_name(card.name) == BASIC_DISPLAY


def classify(platform: str, gfx: Optional[str], name: str) -> tuple[str, str]:
    """(SUPPORTED | UNSUPPORTED | UNKNOWN, why) for one card: by its gfx target on Linux, by its
    adapter name on Windows."""
    if platform == LINUX:
        if gfx is None:
            return UNKNOWN, "its GPU architecture could not be read from /sys/class/kfd"
        if gfx in SUPPORTED_GFX:
            return SUPPORTED, f"{gfx} is one of the GPU targets CTranslate2's ROCm build is compiled for"
        if gfx in OVERRIDE_GFX:
            return UNSUPPORTED, (f"{gfx} is not a target of CTranslate2's ROCm build "
                                 "(at your own risk: HSA_OVERRIDE_GFX_VERSION=10.3.0)")
        return UNSUPPORTED, (f"{gfx} is not one of the GPU targets CTranslate2's ROCm build is compiled for "
                             f"({', '.join(SUPPORTED_GFX)})")
    if platform == WINDOWS:
        text = normalise_adapter_name(name)
        for pattern, family in WINDOWS_SUPPORTED:
            if pattern.search(text):
                return SUPPORTED, family
        for pattern, family in WINDOWS_UNSUPPORTED:
            if pattern.search(text):
                return UNSUPPORTED, f"{family}: AMD's Windows ROCm runtime has no kernels for it"
        return UNKNOWN, "Shisu-ko does not know whether AMD's Windows ROCm runtime supports it"
    return UNSUPPORTED, "the AMD engine exists for Windows and Linux on x86-64 only"


def best_card(platform: str, cards: list[Card]) -> tuple[Card, str, str]:
    """The card to offer the engine for: the first supported one, else the first unknown, else the first."""
    rank = {SUPPORTED: 0, UNKNOWN: 1, UNSUPPORTED: 2}
    rated = [(card, *classify(platform, card.gfx, card.name)) for card in cards]
    return min(rated, key=lambda item: rank[item[1]])


# --- download and install ------------------------------------------------------------------------

def file_name(entry: dict) -> str:
    return entry["url"].rsplit("/", 1)[-1]


def pinned_digest(entry: dict) -> str:
    """The entry's SHA-256, lower-cased. A pin that is not 64 hex characters (a placeholder that was
    never filled in) raises before anything is fetched."""
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise SetupError(f"amd_setup.py pins no SHA-256 for {file_name(entry)} (it reads {digest!r}), "
                         "so it refuses to download it")
    return digest.lower()


def human_size(size: int) -> str:
    return f"{size / 1e9:.2f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"


def download_size(platform: str) -> int:
    return sum(entry["size"] for entry in PINS["files"][platform])


def installed_size(platform: str) -> Optional[int]:
    """What the side folder takes once installed, as PINS has it; None where it was not measured."""
    return PINS.get("installed_size", {}).get(platform)


def space_needed(home: Path, platform: str) -> int:
    """The free space the install takes on the disk of `home`: the pinned files not downloaded yet,
    the CTranslate2 wheel taken out of its zip (counted as the zip, which is larger) and the
    installed engine. One installed before takes nothing more: it is on the disk already and goes
    once the new one is in its place."""
    downloads = download_dir(home)
    need = installed_size(platform) or 0
    for entry in PINS["files"][platform]:
        try:
            there = (downloads / file_name(entry)).stat().st_size == entry["size"]
        except OSError:
            there = False
        need += 0 if there else entry["size"]
        if file_name(entry).endswith(".zip"):
            need += entry["size"]
    return need


def existing(path: Path) -> Optional[Path]:
    """`path`, or the nearest of its parents that is there."""
    for folder in (path, *path.parents):
        if folder.exists():
            return folder
    return None


def free_space(path: Path) -> Optional[int]:
    """The free bytes of the disk that holds `path` (or will); None when that cannot be read."""
    folder = existing(path)
    try:
        return None if folder is None else shutil.disk_usage(folder).free
    except OSError:
        return None


def disk_of(path: Path):
    """Which disk `path` is on (the device number), so that two folders on one disk share its room."""
    folder = existing(path)
    try:
        return os.stat(folder).st_dev if folder is not None else path
    except OSError:
        return folder


def short_of_space(needs: Iterable[tuple[Path, int]]) -> Optional[tuple[Path, int, int]]:
    """(folder, bytes needed, bytes free) for the first disk without room for what goes onto it, else None.

    Folders on one disk share its room, and there the largest need counts: pip unpacks into the
    temporary folder and moves what it unpacked, which on the same disk takes no second copy. A
    disk whose free space cannot be read counts as having room.
    """
    largest: dict = {}
    for folder, size in needs:
        key = disk_of(folder)
        if key not in largest or size > largest[key][1]:
            largest[key] = (folder, size)
    for folder, size in largest.values():
        free = free_space(folder)
        if free is not None and free < size:
            return folder, size, free
    return None


def space_shortfall(home: Path, platform: str) -> Optional[str]:
    """What stops the install before its first download for want of space, as a line; None when it fits."""
    short = short_of_space([(home, space_needed(home, platform)),
                            (Path(tempfile.gettempdir()), installed_size(platform) or 0)])
    if short is None:
        return None
    folder, need, free = short
    return (f"Not enough free space for the AMD engine: it needs about {human_size(need)} on the disk holding "
            f"{folder} while it installs, and {human_size(free)} are free. Free {human_size(need - free)} more, then "
            f"run {command()} again.")


def unpacked_size(wheels: Iterable[Path]) -> int:
    """What pip unpacks the wheels to, with 1 % for the bytecode and records it adds; a file that is
    not a readable wheel counts as its own size, and pip says what is wrong with it."""
    total = 0
    for wheel in wheels:
        try:
            with zipfile.ZipFile(wheel) as archive:
                total += sum(info.file_size for info in archive.infolist())
        except (OSError, zipfile.BadZipFile):
            total += wheel.stat().st_size if wheel.is_file() else 0
    return total + total // 100


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, part: Path, size: int, opener: Callable) -> str:
    """Stream `url` into `part`, saying how far it got every 5 %. Returns the SHA-256 of what came.

    More bytes than pinned is a ChecksumError at once; fewer (a connection that closed early) is a
    ConnectionError, which the caller retries.
    """
    name = part.name[: -len(".part")] if part.name.endswith(".part") else part.name
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    done, shown = 0, 0
    with opener(request, timeout=SOCKET_TIMEOUT) as response, open(part, "wb") as out:
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            done += len(chunk)
            if done > size:
                raise ChecksumError(f"{name} is larger than its pinned {size} bytes; it was deleted")
            digest.update(chunk)
            out.write(chunk)
            step = done * 20 // size if size else 20
            if step > shown:
                shown = step
                say(f"  {name}: {step * 5} % of {human_size(size)}")
    if done < size:
        raise ConnectionError(f"the download ended after {done} of {size} bytes")
    return digest.hexdigest()


def download_file(entry: dict, folder: Path, opener: Optional[Callable] = None) -> Path:
    """Download one pinned file into `folder` (through a .part file) and check its size and SHA-256.

    A file already there with the pinned digest is used as it is. A failed download is tried once
    more; a file that does not match its pin is deleted and fails the install, and so does a full
    disk, which a second try would only fill again.
    """
    opener = opener or urllib.request.urlopen
    name = file_name(entry)
    digest = pinned_digest(entry)
    size = entry["size"]
    target = folder / name
    if target.is_file() and target.stat().st_size == size and sha256_file(target) == digest:
        say(f"{name} is already downloaded")
        return target
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (name + ".part")
    say(f"downloading {name} ({human_size(size)}) ...")
    for attempt in range(DOWNLOAD_RETRIES + 1):
        try:
            got = fetch(entry["url"], part, size, opener)
            break
        except (OSError, http.client.HTTPException) as exc:
            part.unlink(missing_ok=True)
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:  # Windows' ERROR_DISK_FULL too
                raise SetupError(f"the disk holding {folder} is full; downloading {name} stopped") from exc
            if attempt >= DOWNLOAD_RETRIES:
                raise SetupError(f"downloading {name} failed ({exc})") from exc
            say(f"downloading {name} failed ({exc}); trying once more")
        except BaseException:
            # A ChecksumError (not an OSError, so never retried), Ctrl+C or anything else: no .part stays.
            part.unlink(missing_ok=True)
            raise
    if got != digest:
        part.unlink(missing_ok=True)
        raise ChecksumError(f"{name} does not match its pinned SHA-256 (it came as {got}); it was deleted")
    os.replace(part, target)
    return target


def pick_wheel(names: Iterable[str], tag: str, platform: str, version: Optional[str] = None) -> str:
    """The zip member holding the CTranslate2 wheel for this Python (tag) and platform.

    The zips hold one wheel per Python, in a folder: ...-cp312-cp312-win_amd64.whl on Windows,
    ...-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl on Linux. A free-threaded
    Python (cp314t) takes ...-cp314-cp314t-..., never the GIL build of the same version.
    """
    version = version or PINS["ctranslate2"]
    for member in names:
        match = WHEEL_RE.fullmatch(member.rsplit("/", 1)[-1])
        if not match or match["name"] != "ctranslate2" or match["version"] != version:
            continue
        if match["python"] != tag.rstrip("t") or match["abi"] != tag:
            continue
        platforms = match["platform"].split(".")
        if platform == WINDOWS and WINDOWS in platforms:
            return member
        if platform == LINUX and any(p.startswith("manylinux") and p.endswith("_x86_64") for p in platforms):
            return member
    raise SetupError(f"CTranslate2 {version}'s ROCm zip has no wheel for this Python ({tag}, {platform}); "
                     "the AMD engine needs one of the Python versions it was built for")


def extract_wheel(zip_path: Path, tag: str, platform: str, folder: Path) -> Path:
    """Copy this Python's CTranslate2 wheel out of the release zip into `folder`."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            member = pick_wheel(archive.namelist(), tag, platform)
            target = folder / member.rsplit("/", 1)[-1]  # a WHEEL_RE name: no separators, no ".."
            with archive.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
    except zipfile.BadZipFile as exc:
        raise SetupError(f"{zip_path.name} is not a readable zip ({exc})") from exc
    return target


def pip_command(python: str, target: Path, wheels: Iterable[Path]) -> list[str]:
    """pip installs the wheel files as they are: nothing else from anywhere (--no-deps, --no-index)."""
    return [python, "-m", "pip", "install", "--no-deps", "--no-index", "--disable-pip-version-check",
            "--target", str(target), *[str(wheel) for wheel in wheels]]


def replace_dir(new: Path, final: Path) -> None:
    """Put `new` in the place of `final`: the old folder moves aside first and is removed last, and
    moves back when the new one cannot take its place."""
    old = final.with_name(final.name + ".old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            raise SetupError(f"could not remove {old}; stop the server and run {command()} again")
    moved = False
    try:
        if final.exists():
            os.replace(final, old)
            moved = True
        os.replace(new, final)
    except OSError as exc:
        if moved and not final.exists():
            os.replace(old, final)
        raise SetupError(f"could not replace {final} ({exc}); stop the server and run {command()} again") from exc
    if moved:
        shutil.rmtree(old, ignore_errors=True)


def install(home: Path, platform: str, tag: str, opener: Optional[Callable] = None,
            run: Optional[Callable] = None) -> Path:
    """Download, check and install the pinned engine into the side folder. Raises SetupError."""
    run = run or subprocess.run
    entries = PINS["files"][platform]
    for entry in entries:
        pinned_digest(entry)  # every pin is checked before the first byte is fetched
    downloads = download_dir(home)
    wheels = []
    for entry in entries:
        path = download_file(entry, downloads, opener)
        wheels.append(extract_wheel(path, tag, platform, downloads) if path.suffix == ".zip" else path)
    final = rocm_dir(home)
    staged = final.with_name(final.name + ".new")
    if staged.exists():
        shutil.rmtree(staged)
    # pip unpacks into the temporary folder first; a disk that fills halfway leaves only its exit code.
    unpacked = unpacked_size(wheels)
    short = short_of_space([(final.parent, unpacked), (Path(tempfile.gettempdir()), unpacked)])
    if short is not None:
        folder, need, free = short
        raise SetupError(f"the AMD engine takes about {human_size(need)} unpacked, and the disk holding {folder} has "
                         f"{human_size(free)} free; free {human_size(need - free)} more")
    say(f"installing the AMD engine into {final} ...")
    code = run(pip_command(sys.executable, staged, wheels)).returncode
    if code != 0 or not (staged / "ctranslate2" / "__init__.py").is_file():
        shutil.rmtree(staged, ignore_errors=True)
        raise SetupError(f"pip could not install the AMD engine (exit code {code})")
    (staged / MARKER_NAME).write_text(json.dumps(marker(platform, tag), indent=2) + "\n", encoding="utf-8")
    # An engine that replaces another is untested until the probe passes, so the switch goes off first.
    switch_off(home)
    replace_dir(staged, final)
    shutil.rmtree(downloads, ignore_errors=True)
    say(f"installed CTranslate2 {PINS['ctranslate2']} for AMD GPUs into {final}")
    return final


# --- the test in a child process -----------------------------------------------------------------

def probe_env(home: Path, environ=os.environ) -> dict:
    env = dict(environ)
    env["SHISUKO_ENGINE"] = "rocm"
    env["SHISUKO_HOME"] = str(home)
    env["PYTHONUNBUFFERED"] = "1"  # its lines appear as they are printed, also through a pipe
    env.pop("SHISUKO_ROCM_REEXEC", None)  # server.py's own re-exec guard belongs to its own start
    return env


def describe_exit(code: int) -> str:
    """Why the probe child ended, from its exit code: a signal (Linux) or an NTSTATUS (Windows) is a crash."""
    if code < 0:
        try:
            name = f" {signal.Signals(-code).name}"
        except ValueError:
            name = ""
        return f"the test crashed (signal {-code}{name})"
    if code >= 0xC0000000:
        return f"the test crashed (exit code 0x{code:08X})"
    return f"the test failed (exit code {code})"


def probe_failed(home: Path, why: str, fallback: Optional[str] = None) -> bool:
    # A child that crashed or was killed could not switch the engine off itself.
    switch_off(home)
    say(f"{why}.")
    say(f"The server keeps using {fallback or DEFAULT_ENGINE}; run {command('--probe')} to test again, or "
        f"{command('--remove')} to delete the AMD engine ({rocm_dir(home)}) and free its space.")
    return False


def wait_in_steps(child, timeout: float, clock: Callable[[], float] = time.monotonic) -> int:
    """child.wait(timeout) in PROBE_WAIT_STEP slices, so that Ctrl+C reaches this process while the
    child runs: on Windows one long wait is a single WaitForSingleObject that sits through it."""
    deadline = clock() + timeout
    while True:
        try:
            return child.wait(timeout=max(0.0, min(PROBE_WAIT_STEP, deadline - clock())))
        except subprocess.TimeoutExpired:
            if clock() >= deadline:
                raise


def start_variables(platform: Optional[str], environ=os.environ) -> list[tuple[str, str, str]]:
    """The variables of this shell the test ran with that every server start needs too, each as
    (name, value, what a start without it does). The popup's Start button and a new terminal start
    the server without them, and nothing stores them for those."""
    needed = []
    override = environ.get("HSA_OVERRIDE_GFX_VERSION")
    if override and platform == LINUX:  # read by ROCr, Linux's runtime; AMD's Windows HIP runtime ignores it
        # No count: a start whose runtime sees no AMD GPU sets the crash guard to its limit at once.
        needed.append(("HSA_OVERRIDE_GFX_VERSION", override,
                       "the AMD engine cannot run the model on the GPU, and the server then leaves it off until "
                       f"{command('--probe')} passes"))
    rocm_path = environ.get("ROCM_PATH")
    if rocm_path and platform == LINUX and missing_libraries({}):
        # server.py looks in $ROCM_PATH/lib, else in DEFAULT_ROCM's, where the libraries are not.
        needed.append(("ROCM_PATH", rocm_path, "the server keeps its default engine"))
    return needed


def run_probe(home: Path, popen: Optional[Callable] = None, timeout: float = PROBE_TIMEOUT,
              fallback: Optional[str] = None, clock: Callable[[], float] = time.monotonic) -> bool:
    """Have server.py load the model on the AMD GPU in a child process. True when that worked.

    The child writes config.json's "engine" itself on success; everything else leaves it switched off,
    and the server on `fallback` ("the NVIDIA GPU", "the CPU"; None where detection did not run).
    A card or driver that is not up to it kills the child, never this process.
    """
    popen = popen or subprocess.Popen
    say(f"testing the AMD engine: server.py loads the model on the GPU (up to {int(timeout) // 60} minutes) ...")
    try:
        child = popen([sys.executable, str(SERVER_PY), "--probe-gpu"], env=probe_env(home))
    except OSError as exc:
        return probe_failed(home, f"server.py could not be started ({exc})", fallback)
    try:
        code = wait_in_steps(child, timeout, clock)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        return probe_failed(home, f"the test did not finish within {int(timeout) // 60} minutes and was stopped",
                            fallback)
    except KeyboardInterrupt:
        # The child takes the engine out of config.json before it loads the model, so a cancelled
        # test can leave it off: say so, and make it certain once the child is dead (a child still
        # running could write "rocm" after the switch).
        child.kill()
        try:
            ended = child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ended = None
        if ended == 0:  # it passed just before the Ctrl+C, and switched the engine on itself
            say("The test passed before it was cancelled: the server will use the AMD GPU from its next start.")
        else:
            switch_off(home)
            say(f"The test was cancelled, so the AMD engine stays switched off until {command('--probe')} passes; "
                f"the server uses {fallback or DEFAULT_ENGINE} from its next start.")
        raise
    except BaseException:
        child.kill()
        raise
    if code == 0:
        say("The server will use the AMD GPU from its next start.")
        for name, value, without in start_variables(platform_key()):
            say(f"The test ran with {name}={value}. Every server start needs it too, the Start button's included "
                f"(put the line 'export {name}={shlex.quote(value)}' in {login_profile()}, then log out and in "
                f"again); without it {without}.")
        return True
    return probe_failed(home, describe_exit(code), fallback)


# --- commands ------------------------------------------------------------------------------------

def ask(question: str, read: Optional[Callable] = None) -> bool:
    """A yes/no question; only y or yes is a yes, and so is nothing else: EOF (no terminal) is a no."""
    read = read or input
    try:
        answer = read(question)
    except (EOFError, KeyboardInterrupt):
        print(flush=True)
        return False
    return answer.strip().lower() in ("y", "yes")


def wrong_python(home: Path) -> Optional[Path]:
    """The venv's Python when this runs under another one, else None. The engine is built for one
    Python version and the probe runs server.py, which needs the venv's packages."""
    venv = home / "venv"
    if not venv.is_dir():
        return None  # Docker, Nix or a hand-made setup: the running Python is the server's
    try:
        if Path(sys.prefix).resolve() == venv.resolve():
            return None
    except OSError:
        return None
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def override_hint(card: Card, flags: str = IGNORE_OLD_FLAG) -> str:
    """For an RX 6600/6700 on Linux, how to try the engine anyway, as a sentence; "" for any other card.

    The variable has to be there at every server start, not only in the shell that runs the test:
    the popup's Start button and a new terminal start the server without it otherwise.
    """
    if card.gfx not in OVERRIDE_GFX:
        return ""
    return (f" To try it anyway, put the line 'export HSA_OVERRIDE_GFX_VERSION=10.3.0' in {login_profile()} (every "
            "server start needs it, the Start button's too), log out and in again, then run "
            f"{command(flags)}.")


def unsupported_hint(flags: str) -> str:
    """How to install the engine on an unsupported card anyway, as a sentence."""
    return f" To try it anyway (it will likely fail its test), run {command(flags)}."


def engine_label(platform: Optional[str], info: Optional[dict] = None) -> str:
    """"CTranslate2 4.8.2" (with AMD's runtime on Windows) as a marker names it, or as PINS does."""
    if info is None:
        info = {"ctranslate2": PINS["ctranslate2"], "rocm": pinned_rocm(platform)}
    label = f"CTranslate2 {info.get('ctranslate2', '?')}"
    return label + (f" with AMD's ROCm {info.get('rocm', '?')} runtime" if platform == WINDOWS else "")


def has_files(folder: Path) -> bool:
    try:
        return any(folder.iterdir())
    except OSError:
        return False


def engine_in_use(home: Path, platform: str, tag: str) -> bool:
    """Whether the server's next start runs on the AMD engine, as config.json, the marker, the crash
    guard and, on Linux, ROCm's libraries have it (SHISUKO_ENGINE and ROCM_PATH in the server's own
    environment can still say otherwise)."""
    folder = rocm_dir(home)
    return (marker_usable(read_marker(folder), folder, platform, tag) and read_guard(home) < GUARD_LIMIT
            and read_config(home / CONFIG_NAME).get("engine") == "rocm"
            and not (platform == LINUX and missing_libraries()))


def setup(home: Path, assume_yes: bool = False, ignore_old: bool = False) -> Optional[bool]:
    """The default command. True: the engine works; False: something failed; None: nothing to do,
    or the user said no. `assume_yes` (--yes) asks nothing and installs next to an NVIDIA GPU too;
    `ignore_old` (--ignore-old-graphics) installs on a card the support lists call unsupported,
    which nothing else does."""
    platform = platform_key()
    if platform is None:
        # macOS, ARM, 32-bit: there is no AMD build to offer, and nothing to say during setup.
        if assume_yes:
            say(f"The AMD engine exists for Windows and Linux on x86-64 only, not for {sysconfig.get_platform()}.")
        return None
    try:
        cards, nvidia = detect(platform)
    except DetectError as exc:
        say(f"Could not list the graphics adapters: {exc}. Nothing was changed; look for an AMD GPU again "
            f"later with {command()}.")
        return False if assume_yes else None
    if not cards:
        folder = rocm_dir(home)
        if read_config(home / CONFIG_NAME).get("engine") == "rocm" and switch_off(home):
            # Left on, the server would load the AMD engine, find no GPU for it and run on the CPU. A
            # working ROCm needs AMD's driver, which lists the card, and a --probe that passes turns it on again.
            say(f"No AMD GPU found, so the AMD engine in {folder} is switched off; the server uses "
                f"{'the NVIDIA GPU' if nvidia else 'the CPU'} from its next start. Once an AMD GPU is back, a test "
                f"that passes switches it on again ({command('--probe')}); delete it with {command('--remove')}.")
        elif folder.is_dir():
            say(f"No AMD GPU found; the AMD engine in {folder} is not needed ({command('--remove')} deletes it).")
        else:
            say("No AMD GPU found; the AMD engine is not needed.")
        return None
    card, verdict, reason = best_card(platform, cards)
    fallback = "the NVIDIA GPU" if nvidia else "the CPU"  # what the server runs on without the AMD engine
    tag, folder = python_tag(), rocm_dir(home)
    info = read_marker(folder) or {}
    usable = marker_usable(info, folder, platform, tag)    # server.py runs it once it is switched on
    current = marker_current(info, folder, platform, tag)  # ... and it is the one PINS names
    if usable and read_config(home / CONFIG_NAME).get("engine") == "rocm":
        # Switched on: the server runs on it, whatever card detection rates and whatever GPU sits next
        # to it, until the crash guard gives up on it. A start whose ROCm runtime sees no AMD GPU (on
        # Linux no /dev/kfd, no access to it or no ROCm libraries; on Windows no card with AMD's
        # driver) leaves it off at once (server.py's check_rocm_import()), so those come first.
        missing = linux_missing() if platform == LINUX else []
        if missing:
            say(f"The AMD engine in {folder} is switched on, but the server leaves it off and uses {fallback} "
                "until it has:")
            for line in missing:
                say(f"  - {line}")
            say(f"AMD's install guide: {ROCM_INSTALL_URL} (it also has you join the render and video "
                f"groups: {GROUPS_HINT}). Then run {command()} again.")
            return False if assume_yes else None
        if platform == WINDOWS and all(driverless(found) for found in cards):
            say(f"The AMD engine in {folder} is switched on, but the AMD graphics card has lost its driver (Windows "
                "shows it as Microsoft Basic Display Adapter), so the server's next start leaves the engine off and "
                f"uses {fallback}. Install AMD Software: Adrenalin Edition 26.2.2 or newer, then run {command()} "
                "again.")
            return False if assume_yes else None
        starts = read_guard(home)
        if starts >= GUARD_LIMIT:
            if not assume_yes:
                # No count: check_rocm_import() sets the guard to its limit after one start.
                say("The AMD engine is installed, but it did not get a model onto the AMD GPU at its last server "
                    "start (a crash, a CTranslate2 that did not load, or no AMD GPU in sight), so the server leaves "
                    f"it off and uses {fallback}. Test it again with {command('--probe')}, or delete it with "
                    f"{command('--remove')}.")
                return None
            # --yes tests it again below, or installs PINS' engine over an older one and tests that.
        elif current or (nvidia and not assume_yes):
            beside = (f", so the server uses the AMD GPU, not the NVIDIA GPU next to it ({command('--remove')} "
                      "goes back to the NVIDIA GPU)" if nvidia else "")
            say(f"The AMD engine is installed and switched on ({folder}){beside}.")
            if not current:
                say(f"It is {engine_label(platform, info)}; {command('--yes')} updates it to "
                    f"{engine_label(platform)}.")
            return True
    if platform == WINDOWS and driverless(card):
        # Before the NVIDIA GPU's line, whose --yes would only end here.
        say("Found an AMD graphics card without its driver (Windows shows it as Microsoft Basic Display "
            "Adapter). The AMD engine needs AMD Software: Adrenalin Edition 26.2.2 or newer; install it, then "
            f"run {command()} again." + (" Until then the server uses the NVIDIA GPU." if nvidia else ""))
        return False if assume_yes else None
    if nvidia and not assume_yes:
        if usable:  # installed and switched off (one that is on was handled above)
            say(f"Found {card.name} next to an NVIDIA GPU, which the server uses: the AMD engine in {folder} is "
                f"installed but switched off. A test that passes ({command('--probe')}) switches it on in place of "
                f"the NVIDIA GPU; delete it with {command('--remove')} to free its space.")
            if not current:
                say(f"It is {engine_label(platform, info)}; {command('--yes')} updates it to "
                    f"{engine_label(platform)} and tests it.")
            return None
        if verdict == UNSUPPORTED:
            say(f"Found {card.name} next to an NVIDIA GPU, which the server uses; the AMD engine does not "
                f"support it: {reason}.{override_hint(card, BESIDE_NVIDIA) or unsupported_hint(BESIDE_NVIDIA)}")
        else:
            say(f"Found {card.name} next to an NVIDIA GPU, which the server uses, so the AMD engine is not "
                f"offered (install it anyway with {command('--yes')}).")
        return None
    if verdict == UNSUPPORTED and not usable:  # one installed anyway before is handled as installed, below
        if not ignore_old:
            # --yes alone does not do this: an unattended run is no say-so for a card the lists rule out.
            flags = BESIDE_NVIDIA if nvidia else IGNORE_OLD_FLAG
            say(f"Found {card.name}, which the AMD engine does not support: {reason}. The server uses "
                f"{fallback}.{override_hint(card, flags) or unsupported_hint(flags)}")
            return False if assume_yes else None
        say(f"Found {card.name}, which the AMD engine does not support: {reason}. Installing it anyway "
            f"({IGNORE_OLD_FLAG}).")
    if platform == LINUX:
        missing = linux_missing()
        if missing:
            if verdict == SUPPORTED:
                say(f"Found {card.name}. The experimental AMD engine can run Whisper on it, but it still needs:")
            elif verdict == UNKNOWN:
                say(f"Found {card.name}, but {reason}, which the amdgpu kernel driver provides: the card may run "
                    "on the older radeon driver, or may not be one of the GPU targets CTranslate2's ROCm build "
                    f"is compiled for ({', '.join(SUPPORTED_GFX)}). The experimental AMD engine would need:")
            elif usable:
                say(f"Found {card.name}. The AMD engine installed in {folder} still needs:")
            else:  # --ignore-old-graphics for a card it does not support, which the line above said
                say("The experimental AMD engine would also need:")
            for line in missing:
                say(f"  - {line}")
            say(f"AMD's install guide: {ROCM_INSTALL_URL} (it also has you join the render and video "
                f"groups: {GROUPS_HINT}). Then run {command()} again.")
            return False if assume_yes else None
    other = wrong_python(home)
    if other is not None:
        say(f"The AMD engine must be installed with the server's own Python: run {rerun_line(str(other))}")
        return False if assume_yes else None
    if current:
        # Switched off (a failed or cancelled test, no AMD GPU found at the last setup, or a fresh install
        # not yet tested), or, under --yes, on with the crash guard given up.
        if not assume_yes:
            say(f"The AMD engine is installed but switched off; a test that passes switches it on "
                f"({command('--probe')}), or delete it with {command('--remove')}.")
            return None
        return run_probe(home, fallback=fallback)
    if usable:  # for this Python, from older pins: server.py runs it when it is on, and this replaces it
        say(f"The AMD engine in {folder} is {engine_label(platform, info)}; this setup can update it to "
            f"{engine_label(platform)}.")
    elif verdict == SUPPORTED:
        say(f"Found {card.name}. Shisu-ko can run Whisper on it with CTranslate2's AMD build (ROCm).")
    elif verdict == UNKNOWN:
        say(f"Found {card.name}. CTranslate2's AMD build (ROCm) may or may not run Whisper on it.")
    say("This is experimental, not yet tested on AMD hardware by the maintainer.")
    if verdict == UNKNOWN:
        say(f"Warning: {reason}; it may not work.")
    installed = installed_size(platform)
    room = (f"; installed in {folder} it takes about {human_size(installed)}, and about "
            f"{human_size(space_needed(home, platform))} must be free while it installs" if installed
            else f", installed in {folder}")
    if platform == WINDOWS:
        say(f"It needs AMD Software: Adrenalin Edition 26.2.2 or newer and a download of about "
            f"{human_size(download_size(platform))} (CTranslate2 {PINS['ctranslate2']} and AMD's ROCm "
            f"{PINS['rocm']} runtime){room}.")
    else:
        say(f"It needs a download of about {human_size(download_size(platform))} (CTranslate2 "
            f"{PINS['ctranslate2']} for ROCm){room}.")
    if not assume_yes and not ask("Update the AMD engine? [y/N] " if usable else "Download the AMD engine? [y/N] "):
        if not usable:
            say(f"Not installing the AMD engine; the server uses {fallback}. Install it later with {command()}.")
        elif read_config(home / CONFIG_NAME).get("engine") == "rocm":
            say("Keeping the installed AMD engine; the server keeps using it.")
        else:
            say(f"Keeping the installed AMD engine; it stays switched off (test it with {command('--probe')}).")
        return None
    short = space_shortfall(home, platform)
    if short is not None:  # before the first download, which would only fill the disk
        say(short)
        return False
    try:
        install(home, platform, tag)
    except (SetupError, OSError) as exc:  # an OSError: a full disk while unpacking, say
        say(str(exc) if isinstance(exc, SetupError) else f"installing the AMD engine failed ({exc})")
        downloads = download_dir(home)
        if has_files(downloads):
            if folder.exists():  # --remove would take the installed engine along
                say(f"The finished downloads stay in {downloads} for the next try; delete that folder to free "
                    f"the space ({command('--remove')} would delete the installed AMD engine too).")
            else:
                say(f"The finished downloads stay in {downloads} for the next try "
                    f"({command('--remove')} deletes them).")
        say(f"The server keeps using {'the installed AMD engine' if engine_in_use(home, platform, tag) else fallback}.")
        return False
    return run_probe(home, fallback=fallback)


def probe(home: Path) -> bool:
    """--probe: test the installed engine again."""
    folder = rocm_dir(home)
    if not (folder / "ctranslate2" / "__init__.py").is_file():
        say(f"The AMD engine is not installed in {folder}; install it with {command()}.")
        return False
    other = wrong_python(home)
    if other is not None:
        say(f"Test the AMD engine with the server's own Python: run {rerun_line(str(other), SCRIPT, '--probe')}")
        return False
    return run_probe(home)


def remove(home: Path) -> None:
    """--remove: switch the engine off first, so no start reaches for a folder that is half gone, then delete it."""
    if switch_off(home):
        say("switched the AMD engine off in config.json")
    folder = rocm_dir(home)
    leftovers = [folder, folder.with_name(folder.name + ".new"), folder.with_name(folder.name + ".old"),
                 download_dir(home)]
    found = False
    for path in leftovers:
        if not path.exists():
            continue
        found = True
        try:
            shutil.rmtree(path)
            say(f"deleted {path}")
        except OSError as exc:
            say(f"could not delete {path} ({exc}); stop the server and run {command('--remove')} again")
    (home / GUARD_NAME).unlink(missing_ok=True)
    if not found:
        say(f"The AMD engine is not installed ({folder}).")


def engine_wanted(config: dict, environ=os.environ) -> tuple[bool, str]:
    """Whether config.json (or SHISUKO_ENGINE, which overrides it) asks for the AMD engine, and whence."""
    override = environ.get("SHISUKO_ENGINE", "").strip().lower()
    if override in ("rocm", "default"):
        return override == "rocm", f"SHISUKO_ENGINE={override}"
    return config.get("engine") == "rocm", f"config.json engine: {config.get('engine', 'not set')}"


def show_status(home: Path) -> None:
    """--status: what is detected, installed and switched on. Nothing is downloaded or changed."""
    platform, tag = platform_key(), python_tag()
    say(f"Python {tag} ({sys.executable}) on {platform or sysconfig.get_platform()}")
    if platform is None:
        say("the AMD engine exists for Windows and Linux on x86-64 only")
    else:
        try:
            cards, nvidia = detect(platform)
        except DetectError as exc:
            say(f"could not list the graphics adapters: {exc}")
        else:
            if not cards:
                say("no AMD GPU found")
            for card in cards:
                verdict, reason = classify(platform, card.gfx, card.name)
                unready = " (the AMD driver is not installed)" if platform == WINDOWS and driverless(card) else ""
                say(f"{card.name}: {verdict} ({reason}){unready}")
            if nvidia:
                say(f"an NVIDIA GPU is present{' too' if cards else ''}; the server uses it unless the AMD engine "
                    "is switched on")
        if platform == LINUX:
            missing = linux_missing()
            for line in missing:
                say(f"missing: {line}")
            if not missing:
                say(f"ROCm libraries under {rocm_root() / 'lib'} and /dev/kfd: found")
    folder = rocm_dir(home)
    info = read_marker(folder) or {}
    installed = (folder / "ctranslate2" / "__init__.py").is_file()
    usable = marker_usable(info, folder, platform, tag)  # what server.py itself asks of the side folder
    current = platform is not None and marker_current(info, folder, platform, tag)
    if not installed:
        say(f"AMD engine: not installed ({folder})")
    else:
        say(f"AMD engine: installed in {folder}: CTranslate2 {info.get('ctranslate2', '?')}, ROCm "
            f"{info.get('rocm', '?')}, for {info.get('python', '?')} on {info.get('platform', '?')}, "
            f"installed {info.get('installed', '?')}")
        if info.get("python") != tag:
            say(f"  it was installed for {info.get('python', '?')} and this Python is {tag}: run {command()} again")
        elif not usable:
            say(f"  it was installed for {info.get('platform', '?')}, not for {platform or sysconfig.get_platform()}")
        elif not current:
            say(f"  this amd_setup.py installs {engine_label(platform)}: run {command()} again to update it")
    wanted, source = engine_wanted(read_config(home / CONFIG_NAME))
    say(f"switched {'on' if wanted else 'off'} ({source})")
    starts = read_guard(home)
    if starts >= GUARD_LIMIT:  # no count: check_rocm_import() sets the guard to its limit after one start
        say("the AMD engine did not get a model onto the AMD GPU at its last start (a crash, a CTranslate2 that did "
            f"not load, or no AMD GPU in sight), so the server leaves it off until {command('--probe')} passes")
    elif starts:
        say("one start with the AMD engine has not finished loading its model yet (or never did)")
    libraries = platform == LINUX and bool(missing_libraries())  # server.py leaves the engine off without them
    if wanted and usable and libraries:
        say(f"the server leaves it off while ROCm's libraries are missing from {rocm_root() / 'lib'} (above)")
    active = wanted and usable and starts < GUARD_LIMIT and not libraries
    say(f"the server's next start uses the AMD engine: {'yes' if active else 'no'} "
        "(server.py --check says what the server itself decides)")


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):  # adapter names and paths may not fit the console's code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(
        prog="amd_setup.py",
        description="Installs and tests the experimental AMD GPU engine (CTranslate2 for ROCm) "
                    "in the side folder ~/.shisu-ko/rocm.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--yes", action="store_true",
                       help="install without asking; exit 1 unless the engine ends up working")
    group.add_argument("--probe", action="store_true",
                       help="test the installed engine again; exit 1 when the test fails")
    group.add_argument("--status", action="store_true",
                       help="say what is detected, installed and switched on (no download)")
    group.add_argument("--remove", action="store_true",
                       help="switch the engine off and delete the side folder")
    parser.add_argument(IGNORE_OLD_FLAG, "--ignore_old_graphics", dest="ignore_old_graphics", action="store_true",
                        help="install even on a card the support lists call unsupported (an older Radeon, RX 6000 "
                             "on Windows, ...); the engine is still switched on only if its test passes. Combines "
                             "with --yes")
    args = parser.parse_args(argv)
    if args.ignore_old_graphics and (args.probe or args.status or args.remove):
        parser.error(f"{IGNORE_OLD_FLAG} goes with an install (no flag, or --yes), not with --probe, --status or --remove")
    strict = args.yes or args.probe
    home = app_dir()
    ok: Optional[bool] = None
    try:
        if args.status:
            show_status(home)
        elif args.remove:
            remove(home)
        elif args.probe:
            ok = probe(home)
        else:
            ok = setup(home, assume_yes=args.yes, ignore_old=args.ignore_old_graphics)
    except KeyboardInterrupt:
        say("cancelled")
        ok = False
    except Exception as exc:  # noqa: BLE001
        say(f"the AMD engine setup failed ({exc})")
        ok = False
    return 1 if strict and ok is not True else 0


if __name__ == "__main__":
    sys.exit(main())
