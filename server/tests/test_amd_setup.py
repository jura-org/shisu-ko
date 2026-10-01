"""server/amd_setup.py: finding an AMD card, and installing and testing the experimental AMD engine.

Nothing here reaches the network, PowerShell, pip, a GPU or the real ~/.shisu-ko. The downloads go
through a fake opener, pip and the test child through fake runners, detection is faked or reads a
sysfs tree built under tmp_path, and every data folder is a tmp_path passed as SHISUKO_HOME. An
autouse fixture fails any test that still reaches a real process or download.
"""
from __future__ import annotations

import ast
import builtins
import errno
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _serverlib import isolate_home

_AMD_PATH = Path(__file__).resolve().parent.parent / "amd_setup.py"
# The process's own SHISUKO_HOME: a temporary folder unless the caller set one (conftest.py sets it
# before this module is imported), never the real ~/.shisu-ko. amd_setup.py reads SHISUKO_HOME at
# each call (app_dir()), so inside a test the `home` fixture's tmp folder is the one it sees.
_ORIGINAL_HOME = str(isolate_home())


def load_amd_setup():
    name = "shisuko_amd_setup"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _AMD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


amd = load_amd_setup()
Card = amd.Card
WINDOWS, LINUX = amd.WINDOWS, amd.LINUX
REAL_FREE_SPACE, REAL_DEFAULT_ROCM = amd.free_space, amd.DEFAULT_ROCM


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    """SHISUKO_HOME for the test, and a trap for any real process or download that is not faked.

    Every disk has room unless a test says otherwise, and the default ROCm folder is an empty one,
    whatever the machine running the tests holds.
    """
    folder = tmp_path / "home"
    monkeypatch.setenv("SHISUKO_HOME", str(folder))
    for name in ("SHISUKO_ENGINE", "SHISUKO_ROCM_REEXEC", "ROCM_PATH", "HSA_OVERRIDE_GFX_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(amd, "free_space", lambda path: 1 << 50)
    monkeypatch.setattr(amd, "DEFAULT_ROCM", str(tmp_path / "no-rocm"))
    reached = []

    def refuse(*args, **kwargs):
        reached.append(args)
        raise OSError("tests never start a real process or download")

    monkeypatch.setattr(amd.subprocess, "run", refuse)
    monkeypatch.setattr(amd.subprocess, "Popen", refuse)
    monkeypatch.setattr(amd.urllib.request, "urlopen", refuse)
    yield folder
    assert reached == [], f"a test reached a real process or download: {reached!r}"


def server_module(monkeypatch):
    """server.py, loaded under the process's SHISUKO_HOME rather than this test's (its paths are set at import)."""
    monkeypatch.setenv("SHISUKO_HOME", _ORIGINAL_HOME)
    from _serverlib import load_server

    return load_server()


def say_lines(capsys) -> str:
    return capsys.readouterr().out


# --- the file itself and its pins ------------------------------------------------------------------

def test_amd_setup_is_stdlib_only_and_never_imports_server_py():
    tree = ast.parse(_AMD_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not {"server", "shisuko_server", "faster_whisper", "ctranslate2", "numpy"} & imported
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is not None:
        assert imported - {"__future__"} <= set(stdlib), imported - set(stdlib)


def test_every_pin_is_complete():
    for platform, entries in amd.PINS["files"].items():
        assert platform in (WINDOWS, LINUX)
        for entry in entries:
            assert entry["url"].startswith("https://")
            assert isinstance(entry["size"], int) and entry["size"] > 0
            assert amd.pinned_digest(entry) == entry["sha256"]


def test_pins_hold_the_published_and_the_maintainers_digests():
    windows = {amd.file_name(entry): entry for entry in amd.PINS["files"][WINDOWS]}
    linux = {amd.file_name(entry): entry for entry in amd.PINS["files"][LINUX]}
    assert set(windows) == {"rocm-python-wheels-Windows.zip", "rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
                            "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl"}
    assert set(linux) == {"rocm-python-wheels-Linux.zip"}
    assert windows["rocm-python-wheels-Windows.zip"]["size"] == 137_538_158
    assert windows["rocm-python-wheels-Windows.zip"]["sha256"] == \
        "43da4baa5feaee49f77e176277a9647f99c493173c81a0bc60f491cac97532c2"
    assert linux["rocm-python-wheels-Linux.zip"]["size"] == 284_315_912
    assert linux["rocm-python-wheels-Linux.zip"]["sha256"] == \
        "b469e765f74ef85fb97bf4fe2347c8c6296dda00d7f5cf20658f23e95004a925"
    core = windows["rocm_sdk_core-7.2.1-py3-none-win_amd64.whl"]
    libs = windows["rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl"]
    assert (core["size"], core["sha256"]) == (
        644_793_492, "f68989d48df71cbfc3cb68bf705dc37c0f56e9666feddb59a1a0f5ff7539fe1c")
    assert (libs["size"], libs["sha256"]) == (
        489_964_648, "c7fe0b0731af8896093ff69e11496830d3cb6a4aed73e895c60b7cbdc200be92")
    assert amd.PINS["ctranslate2"] == "4.8.2" and amd.PINS["rocm"] == "7.2.1"
    for entry in (*windows.values(), *linux.values()):
        assert amd.PINS["ctranslate2"] in entry["url"] or amd.PINS["rocm"] in entry["url"]


def test_the_download_sizes_the_question_announces():
    assert amd.human_size(amd.download_size(WINDOWS)) == "1.27 GB"
    assert amd.human_size(amd.download_size(LINUX)) == "284 MB"


def test_the_installed_size_the_question_announces(home):
    # The side folder pip made of the pinned files with the cp312 wheel, measured: 3.9 GB, three times the download.
    assert amd.installed_size(WINDOWS) == 3_902_512_758
    assert amd.human_size(amd.installed_size(WINDOWS)) == "3.90 GB"
    assert amd.installed_size(LINUX) is None  # not measured: install() checks what the wheel unpacks to
    # Downloads, the wheel out of the zip (counted as the zip) and the installed engine.
    assert amd.space_needed(home, WINDOWS) == 1_272_296_298 + 137_538_158 + 3_902_512_758
    assert amd.human_size(amd.space_needed(home, WINDOWS)) == "5.31 GB"
    assert amd.space_needed(home, LINUX) == 2 * 284_315_912


@pytest.mark.parametrize("digest", ["<PIN_CORE>", "", None, 12, "ab" * 31, "g" * 64, "a" * 65, "a" * 64 + "\n"])
def test_a_pin_that_is_not_64_hex_characters_is_refused(digest):
    with pytest.raises(amd.SetupError, match="refuses to download"):
        amd.pinned_digest({"url": "https://example.invalid/x.whl", "size": 1, "sha256": digest})


def test_a_pin_in_capitals_is_compared_in_lower_case():
    assert amd.pinned_digest({"url": "https://x/y", "size": 1, "sha256": "AB" * 32}) == "ab" * 32


# --- config.json, the marker, the crash guard ------------------------------------------------------

def test_app_dir_follows_shisuko_home():
    assert amd.app_dir({"SHISUKO_HOME": "/data/shisu"}) == Path("/data/shisu")
    assert amd.app_dir({}) == Path.home() / ".shisu-ko"
    assert amd.app_dir({"SHISUKO_HOME": ""}) == Path.home() / ".shisu-ko"
    assert amd.rocm_dir(Path("h")) == Path("h") / "rocm"
    assert amd.download_dir(Path("h")) == Path("h") / "cache" / "rocm-download"


def test_read_config_tolerates_what_server_py_tolerates(tmp_path, capsys):
    path = tmp_path / "config.json"
    assert amd.read_config(path) == {}
    path.write_text("{broken", encoding="utf-8")
    assert amd.read_config(path) == {}
    assert "ignoring" in say_lines(capsys)
    path.write_text("[1, 2]", encoding="utf-8")
    assert amd.read_config(path) == {}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"model": "small"}).encode("utf-8"))
    assert amd.read_config(path) == {"model": "small"}


def test_write_config_merges_drops_none_and_replaces_in_one_step(tmp_path):
    path = tmp_path / "sub" / "config.json"
    amd.write_config(path, {"model": "large-v3", "engine": "rocm", "cookies_from_browser": "firefox"})
    amd.write_config(path, {"engine": None, "model": "small", "absent": None})
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "small", "cookies_from_browser": "firefox"}
    assert path.read_bytes() == b'{\n  "cookies_from_browser": "firefox",\n  "model": "small"\n}\n'
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.json"]  # no .tmp left behind


def test_write_config_writes_what_server_py_writes(monkeypatch, tmp_path):
    server = server_module(monkeypatch)
    ours, theirs = tmp_path / "ours" / "config.json", tmp_path / "theirs" / "config.json"
    start = {"model": "large-v3", "engine": "rocm", "cookies_from_browser": "firefox"}
    for path in (ours, theirs):
        path.parent.mkdir()
        path.write_text(json.dumps(start), encoding="utf-8")
    monkeypatch.setattr(server, "CONFIG_PATH", theirs)
    for patch in ({"engine": None}, {"model": "small", "extra": "\u00c4"}, {"engine": "rocm"}):
        amd.write_config(ours, patch)
        server.write_config(patch)
        assert ours.read_bytes() == theirs.read_bytes()


def test_switch_off_writes_only_when_the_engine_is_on(home):
    path = home / "config.json"
    assert amd.switch_off(home) is False
    assert not path.exists()
    amd.write_config(path, {"model": "small"})
    before = path.read_bytes()
    assert amd.switch_off(home) is False
    assert path.read_bytes() == before
    amd.write_config(path, {"engine": "rocm"})
    assert amd.switch_off(home) is True
    assert amd.read_config(path) == {"model": "small"}


def test_python_tag():
    assert amd.python_tag((3, 12, 4), gil_disabled=False) == "cp312"
    assert amd.python_tag((3, 10, 0), gil_disabled=False) == "cp310"
    assert amd.python_tag((3, 14, 0), gil_disabled=True) == "cp314t"
    assert amd.python_tag().startswith(f"cp{sys.version_info[0]}{sys.version_info[1]}")


def test_python_tag_is_the_one_server_py_checks_the_marker_against(monkeypatch):
    server = server_module(monkeypatch)
    assert amd.python_tag() == server.python_tag()
    for version, free in (((3, 12), False), ((3, 13), False), ((3, 14), True)):
        assert amd.python_tag(version, gil_disabled=free) == server.python_tag(version, gil_disabled=free)


@pytest.mark.parametrize("platform, key", [
    ("win-amd64", WINDOWS), ("linux-x86_64", LINUX), ("macosx-11.0-arm64", None), ("macosx-10.9-x86_64", None),
    ("win-arm64", None), ("win32", None), ("linux-aarch64", None), ("linux-i686", None),
])
def test_platform_key(platform, key):
    assert amd.platform_key(platform) == key


def test_marker_says_what_server_py_reads():
    when = datetime(2026, 9, 26, 12, 0, 5, tzinfo=timezone.utc)
    assert amd.marker(LINUX, "cp312", when) == {
        "ctranslate2": "4.8.2", "rocm": "system", "python": "cp312", "platform": "linux_x86_64",
        "installed": "2026-09-26T12:00:05+00:00"}
    assert amd.marker(WINDOWS, "cp314t", when)["rocm"] == "7.2.1"
    assert amd.marker(WINDOWS, "cp314t", when)["platform"] == "win_amd64"
    datetime.fromisoformat(amd.marker(WINDOWS, "cp312")["installed"])


def make_installed(home: Path, platform: str = WINDOWS, tag: str | None = None, **changes) -> Path:
    folder = home / "rocm"
    (folder / "ctranslate2").mkdir(parents=True, exist_ok=True)
    (folder / "ctranslate2" / "__init__.py").write_text("", encoding="utf-8")
    info = dict(amd.marker(platform, tag or amd.python_tag()), **changes)
    (folder / "shisuko-rocm.json").write_text(json.dumps(info), encoding="utf-8")
    return folder


def test_marker_current_needs_the_package_and_matching_pins(home):
    folder = home / "rocm"
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp312") is False
    make_installed(home, WINDOWS, "cp312")
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp312") is True
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp313") is False
    assert amd.marker_current(amd.read_marker(folder), folder, LINUX, "cp312") is False
    make_installed(home, WINDOWS, "cp312", ctranslate2="4.7.0")
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp312") is False
    make_installed(home, WINDOWS, "cp312", rocm="7.1.0")
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp312") is False
    make_installed(home, LINUX, "cp312", rocm="7.2.1")
    assert amd.marker_current(amd.read_marker(folder), folder, LINUX, "cp312") is False
    make_installed(home, WINDOWS, "cp312")
    (folder / "ctranslate2" / "__init__.py").unlink()
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp312") is False


def test_marker_usable_asks_only_what_server_py_asks(home):
    folder = home / "rocm"
    assert amd.marker_usable(amd.read_marker(folder), folder, WINDOWS, "cp312") is False
    make_installed(home, WINDOWS, "cp312", ctranslate2="4.8.1", rocm="7.1.0")
    assert amd.marker_usable(amd.read_marker(folder), folder, WINDOWS, "cp312") is True  # older pins: still used
    assert amd.marker_current(amd.read_marker(folder), folder, WINDOWS, "cp312") is False
    assert amd.marker_usable(amd.read_marker(folder), folder, WINDOWS, "cp313") is False  # another Python: never
    assert amd.marker_usable(amd.read_marker(folder), folder, LINUX, "cp312") is False    # nor another platform
    assert amd.marker_usable(amd.read_marker(folder), folder, None, "cp312") is False
    (folder / "ctranslate2" / "__init__.py").unlink()
    assert amd.marker_usable(amd.read_marker(folder), folder, WINDOWS, "cp312") is False


def test_marker_usable_agrees_with_server_pys_rocm_engine_state(monkeypatch, home):
    server = server_module(monkeypatch)
    folder, tag = home / "rocm", amd.python_tag()
    # (None, {}) is a marker built for no platform ("platform": null), which both sides read as a
    # missing key: on a Python without an AMD build (platform None) it must not pass as a match.
    for built_on, changes in ((WINDOWS, {}), (WINDOWS, {"ctranslate2": "4.8.1"}), (WINDOWS, {"rocm": "7.1.0"}),
                              (LINUX, {}), (WINDOWS, {"python": "cp39"}), (None, {})):
        make_installed(home, built_on, **changes)
        for platform in (WINDOWS, LINUX, None):
            usable = amd.marker_usable(amd.read_marker(folder), folder, platform, tag)
            state = server.rocm_engine_state({"engine": "rocm"}, {}, folder, tag, 0, platform)
            assert usable is state[0], (built_on, changes, platform)
    make_installed(home, WINDOWS)
    info = json.loads((folder / "shisuko-rocm.json").read_text(encoding="utf-8"))
    del info["platform"]  # the key itself missing
    (folder / "shisuko-rocm.json").write_text(json.dumps(info), encoding="utf-8")
    for platform in (WINDOWS, LINUX, None):
        assert amd.marker_usable(amd.read_marker(folder), folder, platform, tag) is False
        assert server.rocm_engine_state({"engine": "rocm"}, {}, folder, tag, 0, platform)[0] is False
    (folder / "shisuko-rocm.json").unlink()
    assert amd.marker_usable(amd.read_marker(folder), folder, WINDOWS, tag) is False
    assert server.rocm_engine_state({"engine": "rocm"}, {}, folder, tag, 0, WINDOWS)[0] is False


def test_the_guard_limit_is_server_pys(monkeypatch):
    assert amd.GUARD_LIMIT == server_module(monkeypatch).ROCM_GUARD_LIMIT


def test_read_marker_tolerates_junk(home):
    folder = home / "rocm"
    assert amd.read_marker(folder) is None
    folder.mkdir(parents=True)
    (folder / "shisuko-rocm.json").write_text("{nope", encoding="utf-8")
    assert amd.read_marker(folder) is None
    (folder / "shisuko-rocm.json").write_text("[]", encoding="utf-8")
    assert amd.read_marker(folder) is None


@pytest.mark.parametrize("text, count", [(None, 0), ("2\n", 2), ("1", 1), ("junk", 0), ("-3", 0), ("", 0)])
def test_read_guard(home, text, count):
    if text is not None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "rocm-starts").write_text(text, encoding="utf-8")
    assert amd.read_guard(home) == count


# --- detection: Windows ----------------------------------------------------------------------------

RX7900 = {"Name": "AMD Radeon RX 7900 XTX",
          "PNPDeviceID": "PCI\\VEN_1002&DEV_744C&SUBSYS_0E3B1002&REV_C8\\6&2C7B8F5C&0&00000019"}
IGPU = {"Name": "AMD Radeon(TM) 780M",
        "PNPDeviceID": "PCI\\VEN_1002&DEV_15BF&SUBSYS_0B7B1028&REV_C4\\4&1D4E1F77&0&0041"}
RTX4070 = {"Name": "NVIDIA GeForce RTX 4070 Laptop GPU",
           "PNPDeviceID": "PCI\\VEN_10DE&DEV_2820&SUBSYS_0B7B1028&REV_A1\\4&2A1E3B2F&0&0008"}
BASIC = {"Name": "Microsoft Basic Display Adapter", "PNPDeviceID": "ROOT\\BASICDISPLAY\\0000"}
# An RX 7900 XTX without AMD's driver: Windows runs it on its fallback driver and names it so.
BASIC_AMD = {"Name": "Microsoft Basic Display Adapter",
             "PNPDeviceID": "PCI\\VEN_1002&DEV_744C&SUBSYS_0E3B1002&REV_C8\\6&2C7B8F5C&0&00000019"}


def cim(value) -> str:
    """What `ConvertTo-Json -Compress` prints: one object for one adapter, a list for several."""
    return json.dumps(value, separators=(",", ":"))


def test_amd_adapters_from_cim_reads_one_adapter_as_an_object():
    assert amd.amd_adapters_from_cim(cim(RX7900)) == ["AMD Radeon RX 7900 XTX"]
    assert amd.amd_adapters_from_cim("\ufeff" + cim(RX7900) + "\r\n") == ["AMD Radeon RX 7900 XTX"]


def test_amd_adapters_from_cim_reads_several_as_a_list():
    assert amd.amd_adapters_from_cim(cim([BASIC, IGPU, RX7900])) == ["AMD Radeon(TM) 780M", "AMD Radeon RX 7900 XTX"]
    lower = dict(RX7900, PNPDeviceID=RX7900["PNPDeviceID"].lower())
    assert amd.amd_adapters_from_cim(cim([lower])) == ["AMD Radeon RX 7900 XTX"]
    assert amd.amd_adapters_from_cim(cim([{"Name": None, "PNPDeviceID": RX7900["PNPDeviceID"]}])) == \
        ["an AMD graphics adapter"]


def test_amd_adapters_from_cim_next_to_nvidia():
    text = cim([RTX4070, IGPU])
    assert amd.amd_adapters_from_cim(text) == ["AMD Radeon(TM) 780M"]
    assert amd.nvidia_in_cim(text) is True
    assert amd.nvidia_in_cim(cim(RX7900)) is False


@pytest.mark.parametrize("text", ["", "   ", "not json", "null", "42", '"AMD"', "[1, 2]", '{"Name": 5}',
                                  cim([BASIC]), cim({"Name": "AMD Radeon", "PNPDeviceID": None}),
                                  "Get-CimInstance : Access denied", None])
def test_amd_adapters_from_cim_finds_nothing_in_garbage(text):
    assert amd.amd_adapters_from_cim(text) == []
    assert amd.nvidia_in_cim(text) is False


def test_cim_json_runs_the_query():
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=cim(RX7900).encode("utf-8"), returncode=0)

    assert amd.cim_json(run) == cim(RX7900)
    assert calls[0][0][0] == amd.powershell() and calls[0][0][1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "Win32_VideoController" in calls[0][0][-1]
    assert "PNPDeviceID" in calls[0][0][-1] and "ConvertTo-Json -Compress" in calls[0][0][-1]
    assert calls[0][1]["timeout"] == 20
    # A query that ran is an answer, also when it lists nothing or complains after printing the list.
    assert amd.cim_json(lambda cmd, **kw: SimpleNamespace(stdout=b"", returncode=0)) == ""
    assert amd.cim_json(lambda cmd, **kw: SimpleNamespace(stdout=cim(RX7900).encode(), returncode=1)) == cim(RX7900)


@pytest.mark.parametrize("error, says", [
    (FileNotFoundError(2, "The system cannot find the file specified"), "PowerShell could not be started"),
    (PermissionError(1260, "This program is blocked by group policy"), "PowerShell could not be started"),
    (subprocess.TimeoutExpired("powershell", 20), "PowerShell did not answer within 20 s"),
    (subprocess.SubprocessError("odd"), "PowerShell failed"),
])
def test_cim_json_reports_a_query_that_could_not_run(error, says):
    def run(cmd, **kwargs):
        raise error

    with pytest.raises(amd.DetectError, match=says):
        amd.cim_json(run)


@pytest.mark.parametrize("stdout", [b"", None, b" \r\n"])
def test_cim_json_reports_a_query_that_failed_without_an_answer(stdout):
    with pytest.raises(amd.DetectError, match=r"the graphics-adapter query failed \(exit 1\)"):
        amd.cim_json(lambda cmd, **kw: SimpleNamespace(stdout=stdout, returncode=1))


def test_powershell_is_run_by_its_full_path():
    looked = []
    assert amd.powershell({"SystemRoot": "D:\\WINDOWS"}, lambda path: looked.append(path) or True) == \
        "D:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    assert looked == ["D:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"]
    assert amd.powershell({}, lambda path: True) == "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    assert amd.powershell({"SystemRoot": "D:\\WINDOWS"}, lambda path: False) == "powershell"


def test_detect_on_windows_passes_a_failed_query_on(monkeypatch):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(amd.subprocess, "run", slow)
    with pytest.raises(amd.DetectError, match="did not answer"):
        amd.detect(WINDOWS)


def test_driverless_is_an_amd_card_on_microsofts_fallback_driver():
    assert amd.amd_adapters_from_cim(cim([BASIC, BASIC_AMD])) == ["Microsoft Basic Display Adapter"]
    assert amd.driverless(Card("Microsoft Basic Display Adapter")) is True
    assert amd.driverless(Card("Microsoft  Basic Display Adapter(R)")) is True
    assert amd.driverless(Card("AMD Radeon RX 7900 XTX")) is False


# --- detection: Linux ------------------------------------------------------------------------------

# /sys/class/kfd/kfd/topology/nodes/1/properties of a Radeon RX 7900 XTX (ROCm 6/7 kernel driver).
KFD_RX7900 = """cpu_cores_count 0
simd_count 192
mem_banks_count 1
caches_count 206
io_links_count 1
p2p_links_count 0
cpu_core_id_base 0
simd_id_base 2147487744
max_waves_per_simd 16
lds_size_in_kb 64
gds_size_in_kb 0
num_gws 64
wave_front_size 32
array_count 12
simd_arrays_per_engine 2
cu_per_simd_array 8
simd_per_cu 2
max_slots_scratch_cu 32
gfx_target_version 110000
vendor_id 4098
device_id 29772
location_id 768
domain 0
drm_render_minor 128
hive_id 0
num_sdma_engines 2
num_sdma_xgmi_engines 0
num_sdma_queues_per_engine 6
num_cp_queues 8
max_engine_clk_fcompute 2482
local_mem_size 0
fw_version 2150
capability 671588992
debug_prop 1495
sdma_fw_version 21
unique_id 11420932837461522459
num_xcc 1
max_engine_clk_ccompute 5083
"""

# Node 0, the processor: gfx_target_version 0.
KFD_CPU = """cpu_cores_count 16
simd_count 0
mem_banks_count 1
caches_count 0
io_links_count 1
p2p_links_count 0
cpu_core_id_base 0
simd_id_base 0
max_waves_per_simd 0
lds_size_in_kb 0
gds_size_in_kb 0
num_gws 0
wave_front_size 0
array_count 0
simd_arrays_per_engine 0
cu_per_simd_array 0
simd_per_cu 0
max_slots_scratch_cu 0
gfx_target_version 0
vendor_id 0
device_id 0
location_id 0
domain 0
drm_render_minor 0
hive_id 0
num_sdma_engines 0
num_sdma_xgmi_engines 0
num_sdma_queues_per_engine 0
num_cp_queues 0
max_engine_clk_ccompute 5083
"""


def kfd_node(version: int, device_id: int) -> str:
    """The RX 7900 XTX's properties as another card reports them: its target and PCI device id."""
    return (KFD_RX7900.replace("gfx_target_version 110000", f"gfx_target_version {version}")
            .replace("device_id 29772", f"device_id {device_id}"))


KFD_RX6800 = kfd_node(100300, 0x73BF)      # Radeon RX 6800 XT, gfx1030
KFD_STRIX_HALO = kfd_node(110501, 0x1586)  # Radeon 8060S, gfx1151
KFD_RX9070 = kfd_node(120001, 0x7550)      # Radeon RX 9070 XT, gfx1201
KFD_MI210 = kfd_node(90010, 0x740F)        # Instinct MI210, gfx90a


@pytest.mark.parametrize("version, name", [
    (110000, "gfx1100"), (110001, "gfx1101"), (110002, "gfx1102"), (100300, "gfx1030"), (100301, "gfx1031"),
    (110501, "gfx1151"), (110500, "gfx1150"), (120001, "gfx1201"), (120000, "gfx1200"), (90010, "gfx90a"),
    (90006, "gfx906"), (110003, "gfx1103"), (90402, "gfx942"),
])
def test_gfx_name(version, name):
    assert amd.gfx_name(version) == name


def test_gfx_name_agrees_with_server_py(monkeypatch):
    server = server_module(monkeypatch)
    for version in (110000, 100300, 110501, 120001, 90010, 90006, 100301):
        assert amd.gfx_name(version) == server.gfx_target(version)


def test_gfx_targets_reads_real_kfd_properties():
    texts = [KFD_CPU, KFD_RX7900, KFD_RX6800, KFD_STRIX_HALO, KFD_RX9070, KFD_MI210]
    assert amd.gfx_targets(texts) == ["gfx1100", "gfx1030", "gfx1151", "gfx1201", "gfx90a"]


def test_gfx_targets_leaves_out_the_cpu_node_and_repeats():
    assert amd.gfx_targets([KFD_CPU]) == []
    assert amd.gfx_targets([]) == []
    assert amd.gfx_targets([KFD_CPU, KFD_RX7900, KFD_RX7900]) == ["gfx1100"]
    assert amd.gfx_targets(["cpu_cores_count 8\nsimd_count 0\n", "", "gfx_target_version junk\n"]) == []


def build_sysfs(root: Path, vendors: dict, nodes: dict, pci: dict | None = None) -> Path:
    """A /sys with class/drm/<card>/device/vendor, class/kfd/kfd/topology/nodes/<n>/properties and
    bus/pci/devices/<address>/{vendor,class} (pci: address -> (vendor, class))."""
    for card, vendor in vendors.items():
        folder = root / "class" / "drm" / card
        folder.mkdir(parents=True)
        if vendor is not None:
            (folder / "device").mkdir()
            (folder / "device" / "vendor").write_text(vendor + "\n", encoding="ascii")
    for node, text in nodes.items():
        folder = root / "class" / "kfd" / "kfd" / "topology" / "nodes" / node
        folder.mkdir(parents=True)
        (folder / "properties").write_text(text, encoding="ascii")
    (root / "bus" / "pci" / "devices").mkdir(parents=True, exist_ok=True)
    for address, (vendor, device_class) in (pci or {}).items():
        folder = root / "bus" / "pci" / "devices" / address.replace(":", "-")  # Windows takes no colon in a name
        folder.mkdir()
        (folder / "vendor").write_text(vendor + "\n", encoding="ascii")
        (folder / "class").write_text(device_class + "\n", encoding="ascii")
    return root


def test_detect_linux_names_the_cards_by_their_kfd_target(tmp_path):
    root = build_sysfs(tmp_path / "sys", {"card0": "0x1002", "card0-DP-1": None, "card1": "0x1002"},
                       {"0": KFD_CPU, "1": KFD_RX7900, "10": KFD_MI210, "2": KFD_RX6800})
    cards, nvidia = amd.detect_linux(root)
    assert [card.gfx for card in cards] == ["gfx1100", "gfx1030", "gfx90a"]  # node order: 0, 1, 2, 10
    assert cards[0].name == "an AMD GPU (gfx1100)"
    assert nvidia is False


def test_detect_linux_sees_nvidia_next_to_amd(tmp_path):
    root = build_sysfs(tmp_path / "sys", {"card0": "0x10de", "card1": "0x1002"}, {"0": KFD_CPU, "1": KFD_RX7900})
    assert amd.detect_linux(root) == ([Card("an AMD GPU (gfx1100)", "gfx1100")], True)
    # A headless NVIDIA GPU (3D controller, no nvidia-drm) is on the PCI bus, not under /sys/class/drm.
    headless = build_sysfs(tmp_path / "sys2", {"card0": "0x1002"}, {"1": KFD_RX7900},
                           {"0000:01:00.0": ("0x10de", "0x030200"), "0000:03:00.0": ("0x1002", "0x030000")})
    assert amd.detect_linux(headless)[1] is True


def test_a_leftover_nvidia_smi_is_no_nvidia_gpu(monkeypatch, tmp_path):
    # The NVIDIA card was swapped for an AMD one and its driver packages stayed: only the bus decides.
    monkeypatch.setattr(amd.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    amd_only = build_sysfs(tmp_path / "sys", {"card0": "0x1002"}, {"1": KFD_RX7900},
                           {"0000:03:00.0": ("0x1002", "0x030000"), "0000:03:00.1": ("0x1002", "0x040300")})
    assert amd.detect_linux(amd_only) == ([Card("an AMD GPU (gfx1100)", "gfx1100")], False)


def test_an_nvidia_cards_audio_function_alone_is_no_nvidia_gpu(tmp_path):
    audio = build_sysfs(tmp_path / "sys", {"card0": "0x1002"}, {"1": KFD_RX7900},
                        {"0000:01:00.1": ("0x10de", "0x040300"), "0000:01:00.2": ("0x10de", "0x0c0330")})
    assert amd.detect_linux(audio)[1] is False
    assert amd.pci_display_vendors(tmp_path / "sys" / "bus" / "pci" / "devices") == set()
    assert amd.pci_display_vendors(tmp_path / "missing") == set()


def test_detect_linux_without_kfd_or_without_amd(tmp_path):
    driver_only = build_sysfs(tmp_path / "a", {"card0": "0x1002"}, {})
    assert amd.detect_linux(driver_only) == ([Card("an AMD GPU", None)], False)
    intel = build_sysfs(tmp_path / "b", {"card0": "0x8086"}, {"0": KFD_CPU})
    assert amd.detect_linux(intel) == ([], False)
    assert amd.detect_linux(tmp_path / "missing") == ([], False)


def test_linux_missing_names_what_to_install(tmp_path):
    rocm = tmp_path / "opt-rocm"
    kfd = tmp_path / "kfd"
    missing = amd.linux_missing({"ROCM_PATH": str(rocm)}, kfd=kfd, access=lambda path, mode: True)
    assert len(missing) == 2
    assert str(kfd) in missing[0]
    assert "libamdhip64.so.7" in missing[1] and str(rocm / "lib") in missing[1] and "ROCM_PATH" in missing[1]
    assert "every server start needs it" in missing[1] and "~/.profile" in missing[1]
    kfd.write_text("", encoding="ascii")
    (rocm / "lib").mkdir(parents=True)
    for name in amd.LINUX_LIBRARIES:
        (rocm / "lib" / name).write_text("", encoding="ascii")
    assert amd.linux_missing({"ROCM_PATH": str(rocm)}, kfd=kfd, access=lambda path, mode: True) == []
    groups = amd.linux_missing({"ROCM_PATH": str(rocm)}, kfd=kfd, access=lambda path, mode: False)
    assert len(groups) == 1 and "render" in groups[0] and "video" in groups[0]
    (rocm / "lib" / "libhipblas.so.3").unlink()
    partial = amd.linux_missing({"ROCM_PATH": str(rocm)}, kfd=kfd, access=lambda path, mode: True)
    assert len(partial) == 1 and "libhipblas.so.3" in partial[0] and "libamdhip64" not in partial[0]


def test_rocm_root_defaults_to_opt_rocm(monkeypatch):
    assert REAL_DEFAULT_ROCM == "/opt/rocm" == server_module(monkeypatch).rocm_root({})
    assert amd.rocm_root({}) == Path(amd.DEFAULT_ROCM)
    assert amd.rocm_root({"ROCM_PATH": "/usr/lib/rocm"}) == Path("/usr/lib/rocm")


def rocm_libraries(root: Path, names=amd.LINUX_LIBRARIES) -> Path:
    (root / "lib").mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / "lib" / name).write_text("", encoding="ascii")
    return root


def test_missing_libraries_looks_where_server_py_looks(monkeypatch, tmp_path):
    server = server_module(monkeypatch)
    for number, present in enumerate(((), ("libhipblas.so.3",), amd.LINUX_LIBRARIES)):
        root = rocm_libraries(tmp_path / f"rocm{number}", present)
        env = {"ROCM_PATH": str(root)}
        assert amd.missing_libraries(env) == server.missing_rocm_libraries(env)
        assert amd.missing_libraries(env) == [name for name in amd.LINUX_LIBRARIES if name not in present]
    assert amd.missing_libraries({"ROCM_PATH": str(tmp_path / "nowhere")}) == list(amd.LINUX_LIBRARIES)


def test_login_profile_is_the_file_a_login_shell_reads(tmp_path):
    assert amd.login_profile({"SHELL": "/usr/bin/zsh"}, tmp_path) == "~/.zprofile"
    assert amd.login_profile({"SHELL": "/bin/bash"}, tmp_path) == "~/.profile"
    (tmp_path / ".bash_login").write_text("", encoding="ascii")
    assert amd.login_profile({"SHELL": "/bin/bash"}, tmp_path) == "~/.bash_login"
    (tmp_path / ".bash_profile").write_text("", encoding="ascii")  # Fedora's and Arch's: bash then skips ~/.profile
    assert amd.login_profile({"SHELL": "/bin/bash"}, tmp_path) == "~/.bash_profile"
    for shell in ({"SHELL": "/usr/bin/fish"}, {"SHELL": "/bin/sh"}, {}):
        assert amd.login_profile(shell, tmp_path) == "~/.profile"


# --- which cards the engine supports ---------------------------------------------------------------

@pytest.mark.parametrize("gfx", ["gfx1030", "gfx1100", "gfx1101", "gfx1102", "gfx1150", "gfx1151", "gfx1200", "gfx1201"])
def test_classify_linux_supports_the_build_targets(gfx):
    verdict, reason = amd.classify(LINUX, gfx, "an AMD GPU")
    assert verdict == amd.SUPPORTED and gfx in reason


@pytest.mark.parametrize("gfx", ["gfx1031", "gfx1032"])
def test_classify_linux_rx_6600_and_6700_only_with_the_override(gfx):
    verdict, reason = amd.classify(LINUX, gfx, "an AMD GPU")
    assert verdict == amd.UNSUPPORTED
    assert "at your own risk: HSA_OVERRIDE_GFX_VERSION=10.3.0" in reason


@pytest.mark.parametrize("gfx", ["gfx1103", "gfx1010", "gfx906", "gfx90a", "gfx942", "gfx1152"])
def test_classify_linux_other_targets_are_unsupported(gfx):
    verdict, reason = amd.classify(LINUX, gfx, "an AMD GPU")
    assert verdict == amd.UNSUPPORTED and "gfx1100" in reason and "HSA_OVERRIDE" not in reason


def test_classify_linux_without_a_target_is_unknown():
    assert amd.classify(LINUX, None, "an AMD GPU")[0] == amd.UNKNOWN


def test_classify_linux_decides_by_target_not_by_name():
    # RX 6000 runs on Linux (gfx1030 is a build target) even though Windows has no kernels for it.
    assert amd.classify(LINUX, "gfx1030", "AMD Radeon RX 6800 XT")[0] == amd.SUPPORTED
    assert amd.classify(WINDOWS, None, "AMD Radeon RX 6800 XT")[0] == amd.UNSUPPORTED


@pytest.mark.parametrize("name", [
    "AMD Radeon RX 7900 XTX", "AMD Radeon RX 7900 GRE", "AMD Radeon RX 7800 XT", "AMD Radeon RX 7600",
    "AMD Radeon RX 7600S", "AMD Radeon RX 7700S", "AMD Radeon RX 9070 XT", "AMD Radeon RX 9060 XT",
    "AMD Radeon(TM) RX 9070", "AMD Radeon PRO W7900", "AMD Radeon Pro W7800 48GB", "AMD Radeon PRO W7600",
    "AMD Radeon PRO W9700", "AMD Radeon AI PRO R9700", "AMD Radeon(TM) 890M Graphics", "AMD Radeon 880M",
    "AMD Radeon(TM) 8060S Graphics", "AMD Radeon 8050S Graphics", "AMD Radeon 8040S",
])
def test_classify_windows_supported_families(name):
    verdict, reason = amd.classify(WINDOWS, None, name)
    assert verdict == amd.SUPPORTED, reason


@pytest.mark.parametrize("name, family", [
    ("AMD Radeon RX 6800 XT", "RX 6000"), ("AMD Radeon RX 6600", "RX 6000"), ("AMD Radeon RX 6700S", "RX 6000"),
    ("AMD Radeon RX 5700 XT", "RX 5000"), ("AMD Radeon RX 5500M", "RX 5000"), ("Radeon RX Vega 64", "Vega"),
    ("AMD Radeon(TM) Vega 8 Graphics", "Vega"), ("AMD Radeon 780M Graphics", "780M"), ("AMD Radeon 760M", "760M"),
    ("AMD Radeon 740M", "740M"), ("AMD Radeon 680M", "680M"), ("AMD Radeon(TM) 660M", "660M"),
    ("AMD Radeon PRO W6800", "W6000"), ("AMD Radeon RX 580 Series", "older"), ("AMD Radeon R9 200 Series", "older"),
    ("AMD Radeon R7", "older"), ("AMD Radeon HD 7970", "older"), ("AMD FirePro W9100", "older"),
    ("AMD Radeon Pro WX 7100", "older"),
])
def test_classify_windows_unsupported_families_say_why(name, family):
    verdict, reason = amd.classify(WINDOWS, None, name)
    assert verdict == amd.UNSUPPORTED, reason
    assert family in reason and "Windows ROCm runtime has no kernels" in reason


@pytest.mark.parametrize("name", ["AMD Radeon(TM) Graphics", "AMD Radeon Instinct MI100", "an AMD graphics adapter",
                                  "Microsoft Basic Display Adapter", ""])
def test_classify_windows_unknown_names(name):
    verdict, reason = amd.classify(WINDOWS, None, name)
    assert verdict == amd.UNKNOWN and "does not know" in reason


def test_classify_elsewhere_is_unsupported():
    assert amd.classify("macosx-arm64", None, "AMD Radeon RX 7900 XTX")[0] == amd.UNSUPPORTED


def test_normalise_adapter_name():
    assert amd.normalise_adapter_name("AMD Radeon(TM)  RX 7900 XTX\u00ae") == "amd radeon rx 7900 xtx"
    assert amd.normalise_adapter_name("AMD Radeon\u2122 8060S (R) Graphics") == "amd radeon 8060s graphics"


def test_best_card_prefers_supported_then_unknown():
    old, odd, good = Card("AMD Radeon 780M"), Card("AMD Radeon(TM) Graphics"), Card("AMD Radeon RX 7900 XTX")
    assert amd.best_card(WINDOWS, [old, odd, good])[:2] == (good, amd.SUPPORTED)
    assert amd.best_card(WINDOWS, [old, odd])[:2] == (odd, amd.UNKNOWN)
    assert amd.best_card(WINDOWS, [old])[:2] == (old, amd.UNSUPPORTED)
    igpu, dgpu = Card("an AMD GPU (gfx1036)", "gfx1036"), Card("an AMD GPU (gfx1100)", "gfx1100")
    assert amd.best_card(LINUX, [igpu, dgpu])[0] == dgpu


# --- the CTranslate2 wheel out of the release zip --------------------------------------------------

ZIP_TAGS = [("cp39", "cp39"), ("cp310", "cp310"), ("cp311", "cp311"), ("cp312", "cp312"), ("cp313", "cp313"),
            ("cp314", "cp314"), ("cp314", "cp314t")]
PLATFORM_TAG = {WINDOWS: "win_amd64", LINUX: "manylinux_2_27_x86_64.manylinux_2_28_x86_64"}


def wheel_name(python: str, abi: str, platform: str, version: str = "4.8.2") -> str:
    return f"ctranslate2-{version}-{python}-{abi}-{PLATFORM_TAG[platform]}.whl"


def zip_listing(platform: str, folder: str = "rocm-python-wheels/") -> list:
    names = [folder] if folder else []
    return names + [folder + wheel_name(python, abi, platform) for python, abi in ZIP_TAGS]


@pytest.mark.parametrize("platform", [WINDOWS, LINUX])
@pytest.mark.parametrize("tag, python, abi", [
    ("cp310", "cp310", "cp310"), ("cp312", "cp312", "cp312"), ("cp313", "cp313", "cp313"),
    ("cp314t", "cp314", "cp314t"), ("cp314", "cp314", "cp314"),
])
def test_pick_wheel_takes_this_pythons_wheel(platform, tag, python, abi):
    for folder in ("rocm-python-wheels/", ""):
        listing = list(reversed(zip_listing(platform, folder)))  # the order of the zip does not matter
        assert amd.pick_wheel(listing, tag, platform) == folder + wheel_name(python, abi, platform)


def test_pick_wheel_never_mixes_free_threaded_and_gil_builds():
    only_free = ["w/" + wheel_name("cp314", "cp314t", WINDOWS)]
    with pytest.raises(amd.SetupError, match="cp314"):
        amd.pick_wheel(only_free, "cp314", WINDOWS)
    only_gil = ["w/" + wheel_name("cp314", "cp314", LINUX)]
    with pytest.raises(amd.SetupError, match="cp314t"):
        amd.pick_wheel(only_gil, "cp314t", LINUX)


@pytest.mark.parametrize("platform", [WINDOWS, LINUX])
def test_pick_wheel_names_the_tag_it_has_no_wheel_for(platform):
    with pytest.raises(amd.SetupError, match=r"no wheel for this Python \(cp315, "):
        amd.pick_wheel(zip_listing(platform), "cp315", platform)


def test_pick_wheel_refuses_the_other_platform_version_or_package():
    with pytest.raises(amd.SetupError):
        amd.pick_wheel(zip_listing(WINDOWS), "cp312", LINUX)
    with pytest.raises(amd.SetupError):
        amd.pick_wheel(zip_listing(LINUX), "cp312", WINDOWS)
    with pytest.raises(amd.SetupError):
        amd.pick_wheel([wheel_name("cp312", "cp312", WINDOWS, version="4.8.1")], "cp312", WINDOWS)
    with pytest.raises(amd.SetupError):
        amd.pick_wheel(["other-4.8.2-cp312-cp312-win_amd64.whl", "README.txt", "../x.whl"], "cp312", WINDOWS)
    with pytest.raises(amd.SetupError):
        amd.pick_wheel(["ctranslate2-4.8.2-cp312-cp312-manylinux_2_28_aarch64.whl"], "cp312", LINUX)


def build_rocm_zip(path: Path, platform: str, folder: str = "rocm-python-wheels/") -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        for python, abi in ZIP_TAGS:
            archive.writestr(folder + wheel_name(python, abi, platform), f"wheel {python}-{abi}".encode())
    return path.read_bytes()


def test_extract_wheel_copies_only_this_pythons_wheel(tmp_path):
    build_rocm_zip(tmp_path / "engine.zip", LINUX)
    out = tmp_path / "out"
    out.mkdir()
    wheel = amd.extract_wheel(tmp_path / "engine.zip", "cp313", LINUX, out)
    assert wheel == out / wheel_name("cp313", "cp313", LINUX)
    assert wheel.read_bytes() == b"wheel cp313-cp313"
    assert [p.name for p in out.iterdir()] == [wheel.name]


def test_extract_wheel_refuses_a_broken_zip(tmp_path):
    (tmp_path / "engine.zip").write_bytes(b"PK not really")
    with pytest.raises(amd.SetupError, match="not a readable zip"):
        amd.extract_wheel(tmp_path / "engine.zip", "cp312", WINDOWS, tmp_path)


# --- downloads -------------------------------------------------------------------------------------

class FakeOpener:
    """Stands for urllib.request.urlopen: each URL answers with its queued bodies, or raises a queued error."""

    def __init__(self, answers=None):
        self.answers = {url: list(queue) for url, queue in (answers or {}).items()}
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({"url": request.full_url, "timeout": timeout, "agent": request.get_header("User-agent")})
        answer = self.answers[request.full_url].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return io.BytesIO(answer)


URL = "https://example.invalid/rocm/engine-1.0-py3-none-any.whl"


def pinned(body: bytes, url: str = URL) -> dict:
    return {"url": url, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def test_download_file_checks_and_keeps_a_good_file(tmp_path, capsys):
    body = b"x" * 1000
    opener = FakeOpener({URL: [body]})
    path = amd.download_file(pinned(body), tmp_path / "dl", opener)
    assert path == tmp_path / "dl" / "engine-1.0-py3-none-any.whl"
    assert path.read_bytes() == body
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # the .part is gone
    assert opener.calls == [{"url": URL, "timeout": 60.0, "agent": "shisu-ko-amd-setup"}]
    assert "100 %" in say_lines(capsys)


def test_download_file_reports_progress_every_5_percent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(amd, "CHUNK", 10)
    body = bytes(range(200))
    amd.download_file(pinned(body), tmp_path, FakeOpener({URL: [body]}))
    progress = [line for line in say_lines(capsys).splitlines() if " % of " in line]
    assert [int(line.split(": ")[1].split(" %")[0]) for line in progress] == list(range(5, 101, 5))


def test_download_file_deletes_a_file_with_the_wrong_digest(tmp_path):
    body = b"x" * 1000
    opener = FakeOpener({URL: [b"y" * 1000, body]})
    with pytest.raises(amd.ChecksumError, match="does not match its pinned SHA-256"):
        amd.download_file(pinned(body), tmp_path, opener)
    assert list(tmp_path.iterdir()) == []
    assert len(opener.calls) == 1  # never retried: the same bytes would come again


def test_download_file_deletes_a_file_larger_than_pinned(tmp_path):
    body = b"x" * 1000
    opener = FakeOpener({URL: [body + b"extra", body]})
    with pytest.raises(amd.ChecksumError, match="larger than its pinned 1000 bytes"):
        amd.download_file(pinned(body), tmp_path, opener)
    assert list(tmp_path.iterdir()) == []
    assert len(opener.calls) == 1


def test_download_file_retries_a_short_or_failed_download_once(tmp_path, capsys):
    body = b"x" * 1000
    opener = FakeOpener({URL: [body[:400], body]})
    assert amd.download_file(pinned(body), tmp_path, opener).read_bytes() == body
    assert len(opener.calls) == 2
    assert "trying once more" in say_lines(capsys)
    other = tmp_path / "other"
    opener = FakeOpener({URL: [ConnectionResetError("reset by peer"), body]})
    assert amd.download_file(pinned(body), other, opener).read_bytes() == body


def test_download_file_gives_up_after_the_retry_and_leaves_nothing(tmp_path):
    body = b"x" * 1000
    opener = FakeOpener({URL: [body[:10], TimeoutError("timed out"), body]})
    with pytest.raises(amd.SetupError, match="downloading engine-1.0-py3-none-any.whl failed"):
        amd.download_file(pinned(body), tmp_path, opener)
    assert list(tmp_path.iterdir()) == []
    assert len(opener.calls) == 2


def test_a_full_disk_is_not_retried_and_leaves_no_part(monkeypatch, tmp_path):
    body = b"x" * 1000
    opener = FakeOpener({URL: [body, body]})
    real_open = builtins.open

    class FullDisk:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.file.close()

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

    def full_open(path, mode="r", *args, **kwargs):
        file = real_open(path, mode, *args, **kwargs)
        return FullDisk(file) if str(path).endswith(".part") and "w" in mode else file

    monkeypatch.setattr(amd, "open", full_open, raising=False)
    folder = tmp_path / "dl"
    with pytest.raises(amd.SetupError, match="is full") as caught:
        amd.download_file(pinned(body), folder, opener)
    assert f"the disk holding {folder} is full" in str(caught.value)
    assert len(opener.calls) == 1  # a second try would only fill the disk again
    assert list(folder.iterdir()) == []


def test_a_placeholder_pin_refuses_before_any_request(tmp_path):
    opener = FakeOpener({URL: [b"anything"]})
    with pytest.raises(amd.SetupError, match="refuses to download"):
        amd.download_file({"url": URL, "size": 8, "sha256": "<PIN_CORE>"}, tmp_path / "dl", opener)
    assert opener.calls == []
    assert not (tmp_path / "dl").exists()


def test_install_checks_every_pin_before_the_first_download(monkeypatch, home):
    good = b"good" * 10
    second = "https://example.invalid/rocm/second.whl"
    monkeypatch.setitem(amd.PINS["files"], WINDOWS, [pinned(good), {"url": second, "size": 5, "sha256": "<PIN_LIBS>"}])
    opener = FakeOpener({URL: [good]})
    with pytest.raises(amd.SetupError, match="second.whl"):
        amd.install(home, WINDOWS, "cp312", opener=opener, run=FakePip())
    assert opener.calls == []
    assert not (home / "cache").exists() and not (home / "rocm").exists()


def test_download_file_keeps_a_file_already_downloaded(tmp_path, capsys):
    body = b"x" * 1000
    (tmp_path / "engine-1.0-py3-none-any.whl").write_bytes(body)
    opener = FakeOpener()
    assert amd.download_file(pinned(body), tmp_path, opener).read_bytes() == body
    assert opener.calls == []
    assert "already downloaded" in say_lines(capsys)
    (tmp_path / "engine-1.0-py3-none-any.whl").write_bytes(b"z" * 1000)  # same size, other bytes
    opener = FakeOpener({URL: [body]})
    assert amd.download_file(pinned(body), tmp_path, opener).read_bytes() == body
    assert len(opener.calls) == 1


def test_sha256_file(tmp_path):
    (tmp_path / "f").write_bytes(b"abc")
    assert amd.sha256_file(tmp_path / "f") == hashlib.sha256(b"abc").hexdigest()


def test_human_size():
    assert amd.human_size(1_272_296_298) == "1.27 GB"
    assert amd.human_size(284_315_912) == "284 MB"


# --- pip, the side folder --------------------------------------------------------------------------

def test_pip_command_installs_the_files_alone_into_the_target():
    wheels = [Path("dl") / "ctranslate2-4.8.2-cp312-cp312-win_amd64.whl", Path("dl") / "rocm_sdk_core.whl"]
    command = amd.pip_command("python.exe", Path("home") / "rocm.new", wheels)
    assert command[:4] == ["python.exe", "-m", "pip", "install"]
    assert "--no-deps" in command and "--no-index" in command
    target = command.index("--target")
    assert command[target + 1] == str(Path("home") / "rocm.new")
    assert command[target + 2:] == [str(wheel) for wheel in wheels]


class FakePip:
    """Stands for subprocess.run(pip install ...): records the command and fills --target like pip would."""

    def __init__(self, code: int = 0, package: bool = True):
        self.code, self.package = code, package
        self.commands = []
        self.wheels_there = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        position = command.index("--target")
        target = Path(command[position + 1])
        self.wheels_there.append([Path(wheel).is_file() for wheel in command[position + 2:]])
        if self.package:
            (target / "ctranslate2").mkdir(parents=True)
            (target / "ctranslate2" / "__init__.py").write_text("# the ROCm build\n", encoding="utf-8")
        return SimpleNamespace(returncode=self.code)


def test_replace_dir_puts_the_new_folder_in_place(tmp_path):
    final, new = tmp_path / "rocm", tmp_path / "rocm.new"
    new.mkdir()
    (new / "a").write_text("new", encoding="utf-8")
    amd.replace_dir(new, final)
    assert (final / "a").read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rocm"]


def test_replace_dir_replaces_an_older_folder_and_clears_a_stale_old_one(tmp_path):
    final, new, old = tmp_path / "rocm", tmp_path / "rocm.new", tmp_path / "rocm.old"
    for folder, text in ((final, "installed"), (new, "new"), (old, "stale")):
        folder.mkdir()
        (folder / "a").write_text(text, encoding="utf-8")
    (final / "only-in-the-old-one").write_text("", encoding="utf-8")
    amd.replace_dir(new, final)
    assert (final / "a").read_text(encoding="utf-8") == "new"
    assert not (final / "only-in-the-old-one").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rocm"]


def test_replace_dir_puts_the_old_folder_back_when_the_new_one_cannot_move(monkeypatch, tmp_path):
    final, new = tmp_path / "rocm", tmp_path / "rocm.new"
    for folder, text in ((final, "installed"), (new, "new")):
        folder.mkdir()
        (folder / "a").write_text(text, encoding="utf-8")
    real_replace = os.replace

    def replace(src, dst):
        if Path(src) == new:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(amd.os, "replace", replace)
    with pytest.raises(amd.SetupError, match="could not replace"):
        amd.replace_dir(new, final)
    assert (final / "a").read_text(encoding="utf-8") == "installed"
    assert (new / "a").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "rocm.old").exists()


def test_replace_dir_leaves_an_installed_folder_alone_when_it_cannot_move_aside(monkeypatch, tmp_path):
    final, new = tmp_path / "rocm", tmp_path / "rocm.new"
    for folder in (final, new):
        folder.mkdir()
    monkeypatch.setattr(amd.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError("DLL in use")))
    with pytest.raises(amd.SetupError, match="stop the server"):
        amd.replace_dir(new, final)
    assert final.is_dir() and new.is_dir()


def linux_pins(monkeypatch, tmp_path, folder: str = "rocm-python-wheels/") -> dict:
    body = build_rocm_zip(tmp_path / "Linux.zip", LINUX, folder)
    entry = pinned(body, "https://example.invalid/v4.8.2/rocm-python-wheels-Linux.zip")
    monkeypatch.setitem(amd.PINS["files"], LINUX, [entry])
    return {entry["url"]: [body]}


def windows_pins(monkeypatch, tmp_path) -> dict:
    body = build_rocm_zip(tmp_path / "Windows.zip", WINDOWS)
    core, libs = b"core wheel" * 7, b"libraries wheel" * 5
    entries = [pinned(body, "https://example.invalid/v4.8.2/rocm-python-wheels-Windows.zip"),
               pinned(core, "https://example.invalid/rocm-rel-7.2.1/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl"),
               pinned(libs, "https://example.invalid/rocm-rel-7.2.1/"
                            "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl")]
    monkeypatch.setitem(amd.PINS["files"], WINDOWS, entries)
    return {entry["url"]: [data] for entry, data in zip(entries, (body, core, libs))}


def test_install_on_linux(monkeypatch, tmp_path, home):
    opener = FakeOpener(linux_pins(monkeypatch, tmp_path))
    home.mkdir()
    amd.write_config(home / "config.json", {"engine": "rocm", "model": "small"})
    (home / "rocm").mkdir()
    (home / "rocm" / "left-from-before").write_text("", encoding="utf-8")
    pip = FakePip()
    assert amd.install(home, LINUX, "cp312", opener=opener, run=pip) == home / "rocm"
    command = pip.commands[0]
    assert command[0] == sys.executable
    assert command[command.index("--target") + 1] == str(home / "rocm.new")
    assert [Path(w).name for w in command[command.index("--target") + 2:]] == [wheel_name("cp312", "cp312", LINUX)]
    assert pip.wheels_there == [[True]]
    assert (home / "rocm" / "ctranslate2" / "__init__.py").is_file()
    assert not (home / "rocm" / "left-from-before").exists()
    info = amd.read_marker(home / "rocm")
    assert {k: info[k] for k in ("ctranslate2", "rocm", "python", "platform")} == {
        "ctranslate2": "4.8.2", "rocm": "system", "python": "cp312", "platform": "linux_x86_64"}
    assert amd.marker_current(info, home / "rocm", LINUX, "cp312")
    # An engine that replaces another is untested until the probe passes.
    assert amd.read_config(home / "config.json") == {"model": "small"}
    assert not (home / "cache" / "rocm-download").exists()
    assert sorted(p.name for p in home.iterdir()) == ["cache", "config.json", "rocm"]


def test_install_on_windows_takes_amds_runtime_wheels_too(monkeypatch, tmp_path, home):
    opener = FakeOpener(windows_pins(monkeypatch, tmp_path))
    pip = FakePip()
    amd.install(home, WINDOWS, "cp314t", opener=opener, run=pip)
    command = pip.commands[0]
    wheels = [Path(w).name for w in command[command.index("--target") + 2:]]
    assert wheels == [wheel_name("cp314", "cp314t", WINDOWS), "rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
                      "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl"]
    assert pip.wheels_there == [[True, True, True]]
    assert len(opener.calls) == 3
    info = amd.read_marker(home / "rocm")
    assert (info["rocm"], info["python"], info["platform"]) == ("7.2.1", "cp314t", "win_amd64")
    assert not (home / "cache" / "rocm-download").exists()


def test_install_keeps_the_old_engine_and_the_downloads_when_pip_fails(monkeypatch, tmp_path, home):
    opener = FakeOpener(linux_pins(monkeypatch, tmp_path))
    make_installed(home, LINUX, "cp312", ctranslate2="4.7.0")
    for pip in (FakePip(code=1), FakePip(code=0, package=False)):
        with pytest.raises(amd.SetupError, match="pip could not install"):
            amd.install(home, LINUX, "cp312", opener=opener, run=pip)
        assert amd.read_marker(home / "rocm")["ctranslate2"] == "4.7.0"
        assert not (home / "rocm.new").exists()
        assert (home / "cache" / "rocm-download" / "rocm-python-wheels-Linux.zip").is_file()
    assert len(opener.calls) == 1  # the second try reused the checked download


def test_install_fails_with_the_tag_when_the_zip_has_no_wheel_for_this_python(monkeypatch, tmp_path, home):
    opener = FakeOpener(linux_pins(monkeypatch, tmp_path))
    pip = FakePip()
    with pytest.raises(amd.SetupError, match="cp399"):
        amd.install(home, LINUX, "cp399", opener=opener, run=pip)
    assert pip.commands == []
    assert not (home / "rocm").exists()


def test_install_starts_from_an_empty_staging_folder(monkeypatch, tmp_path, home):
    opener = FakeOpener(linux_pins(monkeypatch, tmp_path))
    stale = home / "rocm.new" / "ctranslate2"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("# left by an interrupted install\n", encoding="utf-8")
    (stale / "stale.py").write_text("", encoding="utf-8")
    seen = []

    def pip(command, **kwargs):
        target = Path(command[command.index("--target") + 1])
        seen.append(target.exists() and any(target.iterdir()))
        return FakePip()(command, **kwargs)

    amd.install(home, LINUX, "cp312", opener=opener, run=pip)
    assert seen == [False]  # pip --target keeps an existing package folder, so staging must start empty
    assert not (home / "rocm" / "ctranslate2" / "stale.py").exists()
    assert (home / "rocm" / "ctranslate2" / "__init__.py").read_text(encoding="utf-8") == "# the ROCm build\n"


# --- room on the disk ------------------------------------------------------------------------------

def test_files_already_downloaded_lower_the_space_needed(monkeypatch, tmp_path, home):
    windows_pins(monkeypatch, tmp_path)
    entries = amd.PINS["files"][WINDOWS]
    before = amd.space_needed(home, WINDOWS)
    assert before == sum(entry["size"] for entry in entries) + entries[0]["size"] + amd.installed_size(WINDOWS)
    downloads = amd.download_dir(home)
    downloads.mkdir(parents=True)
    (downloads / amd.file_name(entries[1])).write_bytes(b"c" * entries[1]["size"])
    (downloads / amd.file_name(entries[2])).write_bytes(b"short")  # a size that is not the pin's counts as missing
    assert amd.space_needed(home, WINDOWS) == before - entries[1]["size"]
    (downloads / amd.file_name(entries[0])).write_bytes(b"z" * entries[0]["size"])
    assert amd.space_needed(home, WINDOWS) == before - entries[1]["size"] - entries[0]["size"]


def test_a_disk_without_room_stops_the_install_before_the_first_download(monkeypatch, flow, home, capsys):
    monkeypatch.setattr(amd, "free_space", lambda path: 1_000_000_000)
    assert amd.setup(home, assume_yes=True) is False
    out = say_lines(capsys)
    assert "Not enough free space for the AMD engine: it needs about 5.31 GB" in out
    assert "1.00 GB are free. Free 4.31 GB more" in out
    assert flow.installs == [] and flow.probes == [] and "finished downloads" not in out
    assert amd.main(["--yes"]) == 1
    assert amd.main([]) == 0  # the setup scripts never fail on it
    assert flow.questions == ["Download the AMD engine? [y/N] "] and flow.installs == []


def test_a_disk_without_room_is_found_before_any_request(monkeypatch, tmp_path, home, capsys):
    """Nothing but detection and the disk is faked: without the check, the download would start."""
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("AMD Radeon RX 7900 XTX")], False))
    monkeypatch.setattr(amd, "free_space", lambda path: 3 * 10**9)
    opener = FakeOpener()
    monkeypatch.setattr(amd.urllib.request, "urlopen", opener)
    assert amd.main(["--yes"]) == 1
    assert opener.calls == [] and not (home / "cache").exists()
    assert "2.31 GB more" in say_lines(capsys)


def test_the_temporary_folder_needs_room_for_the_installed_engine(monkeypatch, flow, home, tmp_path, capsys):
    temp = tmp_path / "other-disk"
    temp.mkdir()
    monkeypatch.setattr(amd.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(amd, "disk_of", lambda path: "D:" if temp in (path, *path.parents) else "C:")
    monkeypatch.setattr(amd, "free_space", lambda path: 1 << 30 if temp in (path, *path.parents) else 1 << 50)
    assert amd.setup(home, assume_yes=True) is False
    assert f"it needs about 3.90 GB on the disk holding {temp}" in say_lines(capsys)
    assert flow.installs == []


def test_folders_on_one_disk_share_its_room(monkeypatch, tmp_path):
    one, two = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(amd, "free_space", lambda path: 100)
    monkeypatch.setattr(amd, "disk_of", lambda path: "C:")
    assert amd.short_of_space([(one, 100), (two, 100)]) is None  # pip moves what it unpacked: no second copy
    assert amd.short_of_space([(one, 60), (two, 101)]) == (two, 101, 100)
    monkeypatch.setattr(amd, "disk_of", lambda path: path.name)
    monkeypatch.setattr(amd, "free_space", lambda path: {"a": 1000, "b": 50}[path.name])
    assert amd.short_of_space([(one, 100), (two, 60)]) == (two, 60, 50)
    monkeypatch.setattr(amd, "free_space", lambda path: None)  # unreadable: counts as room
    assert amd.short_of_space([(one, 1 << 60)]) is None


def test_free_space_reads_the_nearest_folder_that_is_there(tmp_path):
    free = REAL_FREE_SPACE(tmp_path / "not" / "there" / "yet")
    assert isinstance(free, int) and free > 0
    assert amd.existing(tmp_path / "not" / "there") == tmp_path
    assert amd.disk_of(tmp_path / "not" / "there") == amd.disk_of(tmp_path)


def test_unpacked_size_is_what_the_wheels_hold(tmp_path):
    wheel = tmp_path / "engine-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("engine/__init__.py", b"x" * 3000)
        archive.writestr("engine/lib.dll", b"\0" * 97_000)
    other = tmp_path / "not-a-wheel.whl"
    other.write_bytes(b"12345")
    assert amd.unpacked_size([wheel]) == 100_000 + 1000  # and 1 % for the bytecode and records pip adds
    assert amd.unpacked_size([wheel, other]) == (100_005 + 1000)


def test_install_checks_the_room_for_the_unpacked_engine_before_pip(monkeypatch, tmp_path, home):
    opener = FakeOpener(windows_pins(monkeypatch, tmp_path))
    monkeypatch.setattr(amd, "unpacked_size", lambda wheels: 4_000_000_000)
    monkeypatch.setattr(amd, "free_space", lambda path: 1_500_000_000)
    pip = FakePip()
    with pytest.raises(amd.SetupError, match=r"takes about 4.00 GB unpacked, and the disk holding .* has 1.50 GB "
                                             r"free; free 2.50 GB more"):
        amd.install(home, WINDOWS, "cp312", opener=opener, run=pip)
    assert pip.commands == [] and not (home / "rocm").exists() and not (home / "rocm.new").exists()
    assert (home / "cache" / "rocm-download" / "rocm-python-wheels-Windows.zip").is_file()  # for the next try


@pytest.mark.parametrize("short_temp", [True, False])
def test_install_checks_both_the_data_and_the_temporary_disk(monkeypatch, tmp_path, home, short_temp):
    # On Linux this is the only check of either disk: PINS has no installed size for it, so
    # space_shortfall() reserved nothing for the unpacked engine.
    temp = tmp_path / "other-disk"
    temp.mkdir()

    def on_temp(path):
        return temp in (path, *path.parents)

    monkeypatch.setattr(amd.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(amd, "disk_of", lambda path: "T:" if on_temp(path) else "H:")
    monkeypatch.setattr(amd, "unpacked_size", lambda wheels: 4_000_000_000)
    monkeypatch.setattr(amd, "free_space", lambda path: 1_500_000_000 if on_temp(path) == short_temp else 1 << 50)
    opener = FakeOpener(linux_pins(monkeypatch, tmp_path))
    pip = FakePip()
    with pytest.raises(amd.SetupError, match=r"unpacked") as info:
        amd.install(home, LINUX, "cp312", opener=opener, run=pip)
    assert pip.commands == []
    assert f"the disk holding {temp if short_temp else home} has 1.50 GB free" in str(info.value)


# --- the test in a child process -------------------------------------------------------------------

class FakeChild:
    def __init__(self, code: int = 0, hang: bool = False, interrupt_after: int | None = None):
        self.code, self.hang, self.interrupt_after = code, hang, interrupt_after
        self.waits, self.killed = [], False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.interrupt_after is not None and not self.killed:
            if len(self.waits) > self.interrupt_after:
                raise KeyboardInterrupt  # Ctrl+C, which surfaces once a wait returns
            raise subprocess.TimeoutExpired("server.py", timeout)
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("server.py", timeout)
        return self.code

    def clock(self) -> float:
        """A clock that moves on by each wait's timeout, so that a test never waits for real."""
        return float(sum(self.waits))

    def kill(self):
        self.killed = True


class FakePopen:
    def __init__(self, child: FakeChild | None = None, error: BaseException | None = None):
        self.child, self.error = child or FakeChild(), error
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.child


def test_run_probe_success(monkeypatch, home, capsys):
    monkeypatch.setenv("SHISUKO_ROCM_REEXEC", "1")
    monkeypatch.setenv("SHISUKO_ENGINE", "default")
    popen = FakePopen(FakeChild(0))
    assert amd.run_probe(home, popen=popen) is True
    args, kwargs = popen.calls[0]
    assert args == [sys.executable, str(amd.SERVER_PY), "--probe-gpu"]
    assert amd.SERVER_PY == _AMD_PATH.parent / "server.py" and amd.SERVER_PY.is_file()
    env = kwargs["env"]
    assert env["SHISUKO_ENGINE"] == "rocm" and env["SHISUKO_HOME"] == str(home)
    assert "SHISUKO_ROCM_REEXEC" not in env
    assert env.get("PATH") == os.environ.get("PATH")
    assert popen.child.waits == [1.0]  # in steps, so that Ctrl+C gets through on Windows
    assert "The server will use the AMD GPU from its next start." in say_lines(capsys)


def test_run_probe_failure_leaves_the_engine_switched_off(home, capsys):
    amd.write_config(home / "config.json", {"engine": "rocm", "model": "small"})
    assert amd.run_probe(home, popen=FakePopen(FakeChild(1)), fallback="the CPU") is False
    assert amd.read_config(home / "config.json") == {"model": "small"}
    out = say_lines(capsys)
    assert "the test failed (exit code 1)" in out
    assert (f"The server keeps using the CPU; run {amd.command('--probe')} to test again, or "
            f"{amd.command('--remove')} to delete the AMD engine ({home / 'rocm'}) and free its space.") in out
    assert (home / "config.json").is_file()


def test_run_probe_crash(home, capsys):
    assert amd.run_probe(home, popen=FakePopen(FakeChild(0xC0000005)), fallback="the NVIDIA GPU") is False
    out = say_lines(capsys)
    assert "crashed (exit code 0xC0000005)" in out and "keeps using the NVIDIA GPU" in out and "CPU" not in out


def test_run_probe_kills_a_child_that_takes_too_long(home, capsys):
    child = FakeChild(hang=True)
    assert amd.run_probe(home, popen=FakePopen(child), timeout=900, fallback="the NVIDIA GPU",
                         clock=child.clock) is False
    assert child.killed is True
    steps = child.waits[:-1]  # the last one waits for the killed child
    assert sum(steps) == 900 and max(steps) <= amd.PROBE_WAIT_STEP
    out = say_lines(capsys)
    assert "did not finish within 15 minutes" in out and "keeps using the NVIDIA GPU" in out


@pytest.mark.parametrize("fallback, uses", [("the NVIDIA GPU", "the NVIDIA GPU"), (None, amd.DEFAULT_ENGINE)])
def test_ctrl_c_while_the_test_runs_stops_the_child(home, capsys, fallback, uses):
    # On Windows one wait of 15 minutes sat through Ctrl+C until it ran out; the steps let it through.
    # The real child takes "engine" out of config.json before it loads the model; this one is
    # cancelled before it got there, so the file still turns the engine on.
    amd.write_config(home / "config.json", {"engine": "rocm", "model": "small"})
    child = FakeChild(code=1, interrupt_after=2)  # killed: not the 0 of a test that passed
    with pytest.raises(KeyboardInterrupt):
        amd.run_probe(home, popen=FakePopen(child), fallback=fallback, clock=child.clock)
    assert child.killed is True
    assert child.waits == [1.0, 1.0, 1.0, 30]  # the last one waits for the killed child before the switch
    assert amd.read_config(home / "config.json") == {"model": "small"}
    out = say_lines(capsys)
    assert (f"The test was cancelled, so the AMD engine stays switched off until {amd.command('--probe')} passes; "
            f"the server uses {uses} from its next start.") in out


def test_ctrl_c_after_the_test_passed_keeps_the_engine_on(home, capsys):
    # The child wrote "engine": "rocm" and ended with 0 just before the Ctrl+C reached this process.
    amd.write_config(home / "config.json", {"engine": "rocm", "model": "small"})
    before = (home / "config.json").read_bytes()
    child = FakeChild(code=0, interrupt_after=0)
    with pytest.raises(KeyboardInterrupt):
        amd.run_probe(home, popen=FakePopen(child), clock=child.clock)
    assert child.killed is True and (home / "config.json").read_bytes() == before
    out = say_lines(capsys)
    assert "The test passed before it was cancelled: the server will use the AMD GPU from its next start." in out
    assert "stays switched off" not in out


def test_ctrl_c_during_the_probe_command_says_the_engine_is_off(monkeypatch, home, capsys):
    make_installed(home, WINDOWS)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    child = FakeChild(code=1, interrupt_after=1)  # its waits return at once: no real time passes
    monkeypatch.setattr(amd.subprocess, "Popen", FakePopen(child))
    assert amd.main(["--probe"]) == 1
    out = say_lines(capsys)
    assert "stays switched off until" in out and out.rstrip().endswith("[amd] cancelled")
    assert "engine" not in amd.read_config(home / "config.json")


def test_wait_in_steps_gives_the_last_step_what_is_left():
    child = FakeChild(hang=True)
    with pytest.raises(subprocess.TimeoutExpired):
        amd.wait_in_steps(child, 2.5, child.clock)
    assert child.waits == [1.0, 1.0, 0.5]
    assert amd.wait_in_steps(FakeChild(3), 900) == 3


def test_run_probe_when_server_py_cannot_start(home, capsys):
    assert amd.run_probe(home, popen=FakePopen(error=FileNotFoundError("python")), fallback="the CPU") is False
    out = say_lines(capsys)
    assert "could not be started" in out and "keeps using the CPU" in out


def test_run_probe_without_detection_names_no_one_engine(home, capsys):
    # --probe looks for no GPU first, so it cannot say whether the server falls back to NVIDIA or the CPU.
    assert amd.run_probe(home, popen=FakePopen(FakeChild(1))) is False
    assert "keeps using its default engine (an NVIDIA GPU where there is one, else the CPU)" in say_lines(capsys)


def test_a_test_that_passed_with_the_override_says_every_start_needs_it(monkeypatch, home, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda name=None: LINUX)
    monkeypatch.setenv("HSA_OVERRIDE_GFX_VERSION", "10.3.0")
    popen = FakePopen(FakeChild(0))
    assert amd.run_probe(home, popen=popen) is True
    assert popen.calls[0][1]["env"]["HSA_OVERRIDE_GFX_VERSION"] == "10.3.0"  # the child had it from this shell
    out = say_lines(capsys)
    assert "ran with HSA_OVERRIDE_GFX_VERSION=10.3.0. Every server start needs it too" in out
    assert "Start button" in out and f"'export HSA_OVERRIDE_GFX_VERSION=10.3.0' in {amd.login_profile()}" in out
    assert ("without it the AMD engine cannot run the model on the GPU, and the server then leaves it off until "
            f"{amd.command('--probe')} passes") in out
    # No count: a start whose runtime sees no AMD GPU sets server.py's crash guard to its limit at once.
    assert "two starts" not in out and "after 2" not in out
    monkeypatch.delenv("HSA_OVERRIDE_GFX_VERSION")
    assert amd.run_probe(home, popen=FakePopen(FakeChild(0))) is True
    assert "HSA_OVERRIDE" not in say_lines(capsys)


def test_the_override_goes_into_the_file_the_login_shell_reads(monkeypatch, home, tmp_path, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda name=None: LINUX)
    monkeypatch.setenv("HSA_OVERRIDE_GFX_VERSION", "10.3.0")
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert amd.run_probe(home, popen=FakePopen(FakeChild(0))) is True
    assert "in ~/.zprofile, then log out and in again" in say_lines(capsys)


def test_the_override_is_no_advice_on_windows(monkeypatch, home, capsys):
    # ROCr, Linux's runtime, reads HSA_OVERRIDE_GFX_VERSION; AMD's Windows HIP runtime does not, and
    # Windows has no ~/.profile or export.
    monkeypatch.setattr(amd, "platform_key", lambda name=None: WINDOWS)
    monkeypatch.setenv("HSA_OVERRIDE_GFX_VERSION", "11.0.0")
    assert amd.run_probe(home, popen=FakePopen(FakeChild(0))) is True
    out = say_lines(capsys)
    assert "The server will use the AMD GPU from its next start." in out
    assert "HSA_OVERRIDE" not in out and "export" not in out
    assert amd.start_variables(WINDOWS, {"HSA_OVERRIDE_GFX_VERSION": "11.0.0"}) == []
    assert [name for name, _, _ in amd.start_variables(LINUX, {"HSA_OVERRIDE_GFX_VERSION": "11.0.0"})] == \
        ["HSA_OVERRIDE_GFX_VERSION"]


@pytest.mark.parametrize("platform, rocm_path, default_has_them, warned", [
    (LINUX, "set", False, True),     # found only through the shell's ROCM_PATH: the Start button has none
    (LINUX, None, False, False),
    (LINUX, "set", True, False),     # /opt/rocm has them too: every start finds them
    (WINDOWS, "set", False, False),  # AMD's runtime is in the side folder
])
def test_a_test_that_found_rocm_through_rocm_path_says_every_start_needs_it(monkeypatch, home, tmp_path, capsys,
                                                                            platform, rocm_path, default_has_them,
                                                                            warned):
    monkeypatch.setattr(amd, "platform_key", lambda name=None: platform)
    if rocm_path:
        monkeypatch.setenv("ROCM_PATH", str(rocm_libraries(tmp_path / "rocm-7.2.1")))
    if default_has_them:
        rocm_libraries(Path(amd.DEFAULT_ROCM))
    assert amd.run_probe(home, popen=FakePopen(FakeChild(0))) is True
    out = say_lines(capsys)
    assert ("The test ran with ROCM_PATH=" in out) is warned
    if warned:
        assert f"ROCM_PATH={tmp_path / 'rocm-7.2.1'}. Every server start needs it too, the Start button's" in out
        assert amd.login_profile() in out and "without it the server keeps its default engine" in out


def test_describe_exit():
    assert amd.describe_exit(1) == "the test failed (exit code 1)"
    assert amd.describe_exit(3) == "the test failed (exit code 3)"
    assert amd.describe_exit(-int(signal.SIGSEGV)) == f"the test crashed (signal {int(signal.SIGSEGV)} SIGSEGV)"
    assert amd.describe_exit(-200) == "the test crashed (signal 200)"
    assert amd.describe_exit(0xC0000409) == "the test crashed (exit code 0xC0000409)"


def test_engine_wanted():
    assert amd.engine_wanted({"engine": "rocm"}, {}) == (True, "config.json engine: rocm")
    assert amd.engine_wanted({}, {}) == (False, "config.json engine: not set")
    assert amd.engine_wanted({"engine": "rocm"}, {"SHISUKO_ENGINE": "default"}) == (False, "SHISUKO_ENGINE=default")
    assert amd.engine_wanted({}, {"SHISUKO_ENGINE": " ROCm "}) == (True, "SHISUKO_ENGINE=rocm")
    assert amd.engine_wanted({"engine": "rocm"}, {"SHISUKO_ENGINE": "junk"})[0] is True


# --- the question ----------------------------------------------------------------------------------

@pytest.mark.parametrize("answer, yes", [("y", True), ("Y", True), (" yes ", True), ("YES", True), ("", False),
                                         ("n", False), ("no", False), ("yeah", False), ("j", False)])
def test_ask_takes_only_y_or_yes(answer, yes):
    questions = []
    assert amd.ask("Download? [y/N] ", lambda q: questions.append(q) or answer) is yes
    assert questions == ["Download? [y/N] "]


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()])
def test_ask_treats_eof_and_ctrl_c_as_no(error):
    def read(question):
        raise error
    assert amd.ask("Download? [y/N] ", read) is False


def test_ask_reads_stdin_with_input(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    assert amd.ask("Download? [y/N] ") is True
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # setup.sh without a terminal: /dev/null
    assert amd.ask("Download? [y/N] ") is False


def test_eof_at_the_question_downloads_nothing(monkeypatch, home, capsys):
    # Nothing but detection is faked: a yes would reach the (trapped) download.
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("AMD Radeon RX 7900 XTX")], False))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "Download the AMD engine? [y/N]" in out
    assert "experimental, not yet tested on AMD hardware by the maintainer" in out
    assert "1.27 GB" in out
    assert "Not installing the AMD engine" in out
    assert not home.exists()


def test_a_yes_at_the_question_downloads(monkeypatch, home):
    """The counterpart of the EOF test: the same set-up with a yes does reach the download."""
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("AMD Radeon RX 7900 XTX")], False))
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    opened = []

    def opener(request, timeout=None):
        opened.append(request.full_url)
        raise ConnectionRefusedError("offline")

    monkeypatch.setattr(amd.urllib.request, "urlopen", opener)
    assert amd.main([]) == 0
    assert opened == [amd.PINS["files"][WINDOWS][0]["url"]] * 2  # the first file, tried twice
    assert not (home / "rocm").exists()


# --- the whole flow: exit codes --------------------------------------------------------------------

class Flow:
    """setup()'s surroundings: what detection finds, what the user answers, what install and the test do."""

    def __init__(self, monkeypatch):
        self.platform = WINDOWS
        self.cards = [Card("AMD Radeon RX 7900 XTX")]
        self.nvidia = False
        self.detect_error = None
        self.missing = []    # linux_missing()'s lines
        self.libraries = []  # missing_libraries(): what server.py leaves the engine off for
        self.answer = "y"  # or an exception input() raises
        self.install_error = None
        self.probe_result = True
        self.questions, self.installs, self.probes, self.fallbacks = [], [], [], []
        monkeypatch.setattr(amd, "platform_key", lambda platform=None: self.platform)
        monkeypatch.setattr(amd, "detect", self.detect)
        monkeypatch.setattr(amd, "linux_missing", lambda *args, **kwargs: list(self.missing))
        monkeypatch.setattr(amd, "missing_libraries", lambda *args, **kwargs: list(self.libraries))
        monkeypatch.setattr(amd, "install", self.install)
        monkeypatch.setattr(amd, "run_probe", self.probe)
        monkeypatch.setattr(builtins, "input", self.input)

    def detect(self, platform):
        if self.detect_error is not None:
            raise self.detect_error
        return list(self.cards), self.nvidia

    def input(self, question=""):
        self.questions.append(question)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def install(self, home, platform, tag, opener=None, run=None):
        self.installs.append((home, platform, tag))
        if self.install_error is not None:
            raise self.install_error
        return make_installed(home, platform, tag)

    def probe(self, home, popen=None, timeout=None, fallback=None):
        self.probes.append(home)
        self.fallbacks.append(fallback)
        if not self.probe_result:
            return amd.probe_failed(home, "the test failed (exit code 1)", fallback)  # as run_probe does
        return self.probe_result


@pytest.fixture
def flow(monkeypatch):
    return Flow(monkeypatch)


FLOW_CASES = {
    "macOS": dict(platform=None),
    "no AMD card": dict(cards=[]),
    "AMD next to NVIDIA": dict(cards=[Card("AMD Radeon 890M")], nvidia=True),
    "unsupported AMD next to NVIDIA": dict(cards=[Card("AMD Radeon(TM) 780M Graphics")], nvidia=True),
    "unsupported AMD next to NVIDIA on Linux": dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1103)", "gfx1103")],
                                                    nvidia=True),
    "unsupported on Windows": dict(cards=[Card("AMD Radeon RX 6800 XT")]),
    "unsupported on Linux": dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1103)", "gfx1103")]),
    "RX 6700 on Linux": dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1031)", "gfx1031")]),
    "Linux without ROCm": dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1100)", "gfx1100")], missing=["ROCm"]),
    "Linux, no GPU target, without ROCm": dict(platform=LINUX, cards=[Card("an AMD GPU")], missing=["/dev/kfd"]),
    "an AMD card without its driver": dict(cards=[Card("Microsoft Basic Display Adapter")]),
    "an AMD card without its driver next to NVIDIA": dict(cards=[Card("Microsoft Basic Display Adapter")],
                                                          nvidia=True),
    "detection breaks": dict(detect_error=amd.DetectError("PowerShell did not answer within 20 s")),
    "the answer is no": dict(answer="n"),
    "EOF at the question": dict(answer=EOFError()),
    "Ctrl+C at the question": dict(answer=KeyboardInterrupt()),
    "the download fails": dict(install_error=amd.SetupError("downloading x failed")),
    "a digest does not match": dict(install_error=amd.ChecksumError("x does not match its pinned SHA-256")),
    "something unexpected breaks": dict(install_error=RuntimeError("disk full")),
    "Ctrl+C during the install": dict(install_error=KeyboardInterrupt()),
    "the test fails": dict(probe_result=False),
    "an unknown card whose test passes": dict(cards=[Card("AMD Radeon(TM) Graphics")]),
    "the test passes": dict(),
    "Linux, the test passes": dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1201)", "gfx1201")]),
}


@pytest.mark.parametrize("case", FLOW_CASES)
def test_setup_without_yes_or_probe_always_exits_0(flow, case, capsys):
    for key, value in FLOW_CASES[case].items():
        setattr(flow, key, value)
    assert amd.main([]) == 0
    out = say_lines(capsys)
    if flow.platform is not None:
        assert out.strip(), "every path on Windows and Linux says what it did"
    if flow.questions:
        assert flow.questions == ["Download the AMD engine? [y/N] "]
    if case in ("the answer is no", "EOF at the question", "Ctrl+C at the question"):
        assert flow.installs == [] and flow.probes == []
    if case in ("macOS", "no AMD card", "AMD next to NVIDIA", "unsupported AMD next to NVIDIA",
                "unsupported AMD next to NVIDIA on Linux", "unsupported on Windows", "unsupported on Linux",
                "RX 6700 on Linux", "Linux without ROCm", "Linux, no GPU target, without ROCm",
                "an AMD card without its driver", "an AMD card without its driver next to NVIDIA",
                "detection breaks"):
        assert flow.questions == [] and flow.installs == []


def test_setup_says_nothing_on_macos(flow, capsys):
    flow.platform = None
    assert amd.main([]) == 0
    assert say_lines(capsys) == ""


def test_setup_passes_this_python_and_probes_after_the_install(flow, home):
    assert amd.main([]) == 0
    assert flow.installs == [(home, WINDOWS, amd.python_tag())]
    assert flow.probes == [home]
    assert flow.fallbacks == ["the CPU"]


def test_setup_tells_linux_users_what_to_install(flow, capsys):
    flow.platform, flow.cards = LINUX, [Card("an AMD GPU (gfx1100)", "gfx1100")]
    flow.missing = ["/dev/kfd, the amdgpu driver's compute interface"]
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.4/" in out
    assert "render" in out and "video" in out and "/dev/kfd" in out
    assert "can run Whisper on it, but it still needs" in out


def test_setup_promises_nothing_for_a_linux_card_whose_target_it_cannot_read(flow, home, capsys):
    flow.platform, flow.cards = LINUX, [Card("an AMD GPU", None)]
    flow.missing = ["/dev/kfd, the amdgpu driver's compute interface"]
    assert amd.setup(home) is None
    out = say_lines(capsys)
    assert "can run Whisper on it" not in out
    assert "could not be read" in out and "gfx1100" in out and "/dev/kfd" in out
    assert flow.questions == [] and flow.installs == []


def test_setup_unknown_card_is_asked_with_a_warning(flow, capsys):
    flow.cards, flow.answer = [Card("AMD Radeon(TM) Graphics")], "n"
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "Warning:" in out and "may or may not run Whisper on it" in out
    assert "can run Whisper on it" not in out  # no promise the warning then takes back
    assert flow.questions == ["Download the AMD engine? [y/N] "]


def test_setup_promises_nothing_for_a_linux_card_whose_target_it_cannot_read_with_rocm_there(flow, home, capsys):
    flow.platform, flow.cards, flow.answer = LINUX, [Card("an AMD GPU", None)], "n"
    flow.missing = []
    assert amd.setup(home) is None
    out = say_lines(capsys)
    assert "could not be read" in out and "can run Whisper on it" not in out
    assert flow.questions == ["Download the AMD engine? [y/N] "]


def test_setup_promises_a_supported_card_that_it_can_run_whisper(flow, home, capsys):
    flow.answer = "n"
    assert amd.setup(home) is None
    out = say_lines(capsys)
    assert "Found AMD Radeon RX 7900 XTX. Shisu-ko can run Whisper on it" in out and "Warning" not in out


YES_CASES = {
    "macOS": (dict(platform=None), 1, False),
    "no AMD card": (dict(cards=[]), 1, False),
    "Linux without ROCm": (dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1100)", "gfx1100")], missing=["x"]), 1,
                           False),
    "the download fails": (dict(install_error=amd.SetupError("downloading x failed")), 1, True),
    "something unexpected breaks": (dict(install_error=RuntimeError("disk full")), 1, True),
    "the test fails": (dict(probe_result=False), 1, True),
    "the test passes": (dict(), 0, True),
    "AMD next to NVIDIA, installed anyway": (dict(cards=[Card("AMD Radeon 890M")], nvidia=True), 0, True),
    # A card the lists rule out needs --ignore-old-graphics as well (IGNORE_OLD_CASES): an unattended
    # --yes alone is no say-so for it, and fails.
    "unsupported, not ignored": (dict(cards=[Card("AMD Radeon RX 6800 XT")]), 1, False),
    "unsupported next to NVIDIA, not ignored": (dict(cards=[Card("AMD Radeon(TM) 780M Graphics")], nvidia=True),
                                                1, False),
    "detection breaks": (dict(detect_error=amd.DetectError("PowerShell could not be started")), 1, False),
    "an AMD card without its driver": (dict(cards=[Card("Microsoft Basic Display Adapter")]), 1, False),
    "an AMD card without its driver next to NVIDIA": (dict(cards=[Card("Microsoft Basic Display Adapter")],
                                                           nvidia=True), 1, False),
}


@pytest.mark.parametrize("case", YES_CASES)
def test_yes_never_asks_and_fails_unless_the_engine_works(flow, case):
    changes, code, installs = YES_CASES[case]
    for key, value in changes.items():
        setattr(flow, key, value)
    assert amd.main(["--yes"]) == code
    assert flow.questions == []
    assert bool(flow.installs) is installs


IGNORE_OLD_CASES = {
    "unsupported on Windows": dict(cards=[Card("AMD Radeon RX 6800 XT")]),
    "unsupported on Linux": dict(platform=LINUX, cards=[Card("an AMD GPU (gfx1103)", "gfx1103")]),
    "unsupported next to NVIDIA": dict(cards=[Card("AMD Radeon(TM) 780M Graphics")], nvidia=True),
}


@pytest.mark.parametrize("case", IGNORE_OLD_CASES)
@pytest.mark.parametrize("spelling", ["--ignore-old-graphics", "--ignore_old_graphics"])
def test_yes_with_ignore_old_graphics_installs_on_an_unsupported_card(flow, capsys, case, spelling):
    for key, value in IGNORE_OLD_CASES[case].items():
        setattr(flow, key, value)
    assert amd.main(["--yes", spelling]) == 0
    assert flow.questions == [] and flow.installs
    assert "Installing it anyway (--ignore-old-graphics)" in say_lines(capsys)


def test_ignore_old_graphics_alone_still_asks_and_exits_0(flow, capsys):
    flow.cards = [Card("AMD Radeon RX 6800 XT")]
    flow.answer = "n"
    assert amd.main(["--ignore-old-graphics"]) == 0
    assert len(flow.questions) == 1 and flow.installs == []


def test_ignore_old_graphics_goes_with_an_install_only(flow, capsys):
    for other in ("--probe", "--status", "--remove"):
        with pytest.raises(SystemExit):
            amd.main([other, "--ignore-old-graphics"])


def test_an_unsupported_card_says_how_to_try_it_anyway(flow, capsys):
    flow.cards = [Card("AMD Radeon RX 6800 XT")]
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "does not support" in out and "--ignore-old-graphics" in out and flow.installs == []


def test_an_installed_engine_that_is_on_needs_nothing(flow, home, capsys):
    make_installed(home, WINDOWS)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    assert amd.main([]) == 0
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    assert "installed and switched on" in say_lines(capsys)


@pytest.mark.parametrize("nvidia, uses", [(True, "the NVIDIA GPU"), (False, "the CPU")])
@pytest.mark.parametrize("assume_yes", [False, True])
def test_an_engine_that_is_on_without_an_amd_gpu_is_switched_off(flow, home, capsys, nvidia, uses, assume_yes):
    # The AMD card was swapped for an NVIDIA one: left on, the server would run the AMD engine on the CPU.
    flow.cards, flow.nvidia = [], nvidia
    make_installed(home, WINDOWS)
    amd.write_config(home / "config.json", {"engine": "rocm", "model": "small"})
    assert amd.setup(home, assume_yes=assume_yes) is None
    out = say_lines(capsys)
    assert f"No AMD GPU found, so the AMD engine in {home / 'rocm'} is switched off; the server uses {uses}" in out
    assert amd.command("--probe") in out and amd.command("--remove") in out and "not needed" not in out
    assert amd.read_config(home / "config.json") == {"model": "small"}
    assert amd.engine_in_use(home, WINDOWS, amd.python_tag()) is False
    assert (home / "rocm").is_dir()  # a --probe that passes turns it on again, with no download
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])


def test_an_engine_that_is_off_without_an_amd_gpu_is_named_for_removal(flow, home, capsys):
    flow.cards = []
    make_installed(home, WINDOWS)
    amd.write_config(home / "config.json", {"model": "small"})
    before = (home / "config.json").read_bytes()
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert f"No AMD GPU found; the AMD engine in {home / 'rocm'} is not needed ({amd.command('--remove')}" in out
    assert (home / "config.json").read_bytes() == before


def test_no_amd_gpu_and_no_engine_says_so_and_writes_nothing(flow, home, capsys):
    flow.cards = []
    assert amd.main([]) == 0
    assert say_lines(capsys) == "[amd] No AMD GPU found; the AMD engine is not needed.\n"
    assert not home.exists()


def switched_on(home, starts=None, **changes):
    make_installed(home, WINDOWS, **changes)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    if starts is not None:
        (home / "rocm-starts").write_text(starts, encoding="utf-8")


def test_an_engine_that_is_on_next_to_nvidia_is_what_the_server_uses(flow, home, capsys):
    flow.cards, flow.nvidia = [Card("AMD Radeon RX 7900 XTX")], True
    switched_on(home)
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "installed and switched on" in out and "not the NVIDIA GPU next to it" in out
    assert "which the server uses" not in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])


@pytest.mark.parametrize("state", ["off", "another Python"])
def test_an_engine_the_server_does_not_use_leaves_it_on_the_nvidia_gpu(flow, home, capsys, state):
    flow.cards, flow.nvidia = [Card("AMD Radeon RX 7900 XTX")], True
    make_installed(home, WINDOWS, tag="cp39" if state == "another Python" else None)
    if state == "another Python":
        amd.write_config(home / "config.json", {"engine": "rocm"})
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "next to an NVIDIA GPU, which the server uses" in out and "switched on" not in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    if state == "off":
        # --yes installs nothing here: the engine is on the disk already, and only a test switches it on.
        assert f"the AMD engine in {home / 'rocm'} is installed but switched off" in out
        assert amd.command("--probe") in out and f"delete it with {amd.command('--remove')} to free its space" in out
        assert "install it anyway" not in out and "updates it" not in out


def test_an_older_engine_that_is_off_next_to_nvidia_is_updated_with_yes(flow, home, capsys):
    flow.cards, flow.nvidia = [Card("AMD Radeon RX 7900 XTX")], True
    make_installed(home, WINDOWS, ctranslate2="4.8.1")
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "is installed but switched off" in out and "install it anyway" not in out
    assert (f"It is CTranslate2 4.8.1 with AMD's ROCm 7.2.1 runtime; {amd.command('--yes')} updates it to "
            "CTranslate2 4.8.2 with AMD's ROCm 7.2.1 runtime and tests it.") in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    assert amd.main(["--yes"]) == 0
    assert flow.installs == [(home, WINDOWS, amd.python_tag())] and flow.probes == [home]


def test_an_engine_the_crash_guard_gave_up_on_is_not_called_switched_on(flow, home, capsys):
    switched_on(home, starts="2")  # what server.py's leave_rocm() writes after one start that saw no AMD GPU
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "switched on" not in out
    assert "did not get a model onto the AMD GPU at its last server start" in out and "uses the CPU" in out
    assert "last 2" not in out and "server starts" not in out  # check_rocm_import() sets it after one start
    assert "--probe" in out and "--remove" in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    flow.nvidia = True
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "uses the NVIDIA GPU" in out and "switched on" not in out and flow.probes == []


@pytest.mark.parametrize("result, code", [(True, 0), (False, 1)])
def test_yes_tests_an_engine_the_crash_guard_gave_up_on_again(flow, home, result, code):
    switched_on(home, starts="2")
    flow.probe_result = result
    assert amd.main(["--yes"]) == code
    assert flow.probes == [home] and flow.installs == [] and flow.questions == []


def test_one_unfinished_start_does_not_trip_the_guard(flow, home, capsys):
    switched_on(home, starts="1")
    assert amd.main([]) == 0
    assert "installed and switched on" in say_lines(capsys)
    assert amd.main(["--yes"]) == 0
    assert "installed and switched on" in say_lines(capsys)
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])


@pytest.mark.parametrize("nvidia, uses", [(False, "the CPU"), (True, "the NVIDIA GPU")])
def test_an_engine_that_is_on_without_rocms_libraries_is_not_called_switched_on(flow, home, capsys, nvidia, uses):
    # ROCm was removed or upgraded past 7.2: server.py keeps its default engine until the libraries are back.
    flow.platform, flow.cards, flow.nvidia = LINUX, [Card("an AMD GPU (gfx1100)", "gfx1100")], nvidia
    make_installed(home, LINUX)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    flow.libraries = ["libamdhip64.so.7"]
    flow.missing = ["ROCm 7.2.x: libamdhip64.so.7 not found under /opt/rocm/lib"]
    assert amd.main(["--yes"]) == 1
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "installed and switched on" not in out
    assert f"is switched on, but the server leaves it off and uses {uses} until it has:" in out
    assert "  - ROCm 7.2.x: libamdhip64.so.7 not found" in out and amd.ROCM_INSTALL_URL in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    assert amd.engine_in_use(home, LINUX, amd.python_tag()) is False
    flow.libraries = []
    assert amd.engine_in_use(home, LINUX, amd.python_tag()) is True


def test_linux_setup_and_status_look_for_rocms_libraries_where_server_py_does(monkeypatch, home, tmp_path, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: LINUX)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("an AMD GPU (gfx1100)", "gfx1100")], False))
    kfd, real_linux_missing = tmp_path / "kfd", amd.linux_missing
    kfd.write_text("", encoding="ascii")  # a /dev/kfd this user may use, whatever machine runs the tests
    monkeypatch.setattr(amd, "linux_missing", lambda environ=os.environ, **kwargs: real_linux_missing(
        environ, kfd=kfd, access=lambda path, mode: True))
    rocm = tmp_path / "rocm-7.2.1"
    rocm.mkdir()
    monkeypatch.setenv("ROCM_PATH", str(rocm))
    make_installed(home, LINUX)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    assert amd.setup(home) is None  # server.py's missing_rocm_libraries() finds none either (the test above)
    out = say_lines(capsys)
    assert "installed and switched on" not in out and "libamdhip64.so.7" in out and str(rocm / "lib") in out
    assert amd.main(["--yes"]) == 1
    assert amd.engine_in_use(home, LINUX, amd.python_tag()) is False
    out = status(capsys)
    assert "the server leaves it off while ROCm's libraries are missing" in out
    assert "next start uses the AMD engine: no" in out
    rocm_libraries(rocm)
    assert amd.setup(home) is True
    assert "installed and switched on" in say_lines(capsys)
    assert amd.engine_in_use(home, LINUX, amd.python_tag()) is True
    assert "next start uses the AMD engine: yes" in status(capsys)


@pytest.mark.parametrize("line", ["/dev/kfd, the amdgpu driver's compute interface (AMD's ROCm install sets it up)",
                                  "access to /dev/kfd: join the render and video groups"])
def test_an_engine_that_is_on_without_kfd_is_not_called_switched_on(flow, home, capsys, line):
    # The libraries are there, but the runtime sees no AMD GPU: server.py's check_rocm_import() leaves
    # the engine off at the next start.
    flow.platform, flow.cards = LINUX, [Card("an AMD GPU", None)]
    make_installed(home, LINUX)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    before = (home / "config.json").read_bytes()
    flow.missing = [line]
    assert amd.main(["--yes"]) == 1
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "installed and switched on" not in out
    assert f"is switched on, but the server leaves it off and uses the CPU until it has:\n[amd]   - {line}" in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    assert (home / "config.json").read_bytes() == before


@pytest.mark.parametrize("nvidia, uses", [(False, "the CPU"), (True, "the NVIDIA GPU")])
@pytest.mark.parametrize("assume_yes, result", [(False, None), (True, False)])
def test_an_engine_that_is_on_for_a_card_without_its_driver_is_not_called_switched_on(flow, home, capsys, nvidia,
                                                                                      uses, assume_yes, result):
    # A DDU clean or a failed driver update: Windows runs the card on its fallback driver, which the
    # ROCm runtime cannot see, so the server's next start leaves the engine off.
    flow.cards, flow.nvidia = [Card("Microsoft Basic Display Adapter")], nvidia
    switched_on(home)
    before = (home / "config.json").read_bytes()
    assert amd.setup(home, assume_yes=assume_yes) is result
    out = say_lines(capsys)
    assert "installed and switched on" not in out
    assert f"The AMD engine in {home / 'rocm'} is switched on, but the AMD graphics card has lost its driver" in out
    assert f"the server's next start leaves the engine off and uses {uses}" in out
    assert "Adrenalin Edition 26.2.2 or newer" in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    assert (home / "config.json").read_bytes() == before


def test_an_engine_that_is_on_for_a_card_with_its_driver_beside_one_without_is_switched_on(flow, home, capsys):
    flow.cards = [Card("Microsoft Basic Display Adapter"), Card("AMD Radeon RX 7900 XTX")]
    switched_on(home)
    assert amd.setup(home) is True
    assert "installed and switched on" in say_lines(capsys)


def test_an_engine_installed_anyway_is_not_called_unsupported(flow, home, capsys):
    # An RX 6800 XT on Windows is not supported, but --ignore-old-graphics installed it and its test passed.
    flow.cards = [Card("AMD Radeon RX 6800 XT")]
    switched_on(home)
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "installed and switched on" in out and "does not support" not in out and "CPU" not in out


@pytest.mark.parametrize("platform, card", [
    (WINDOWS, Card("AMD Radeon(TM) 780M Graphics")), (LINUX, Card("an AMD GPU (gfx1103)", "gfx1103")),
])
def test_an_unsupported_card_next_to_nvidia_needs_both_flags(flow, capsys, platform, card):
    flow.platform, flow.cards, flow.nvidia = platform, [card], True
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "does not support" in out and "next to an NVIDIA GPU, which the server uses" in out
    assert "--yes --ignore-old-graphics" in out and "CPU" not in out
    assert flow.questions == [] and flow.installs == []


def test_a_supported_card_next_to_nvidia_is_still_offered_with_yes(flow, capsys):
    flow.cards, flow.nvidia = [Card("AMD Radeon 890M")], True
    assert amd.main([]) == 0
    assert "install it anyway with" in say_lines(capsys) and flow.installs == []


def test_an_rx_6700_next_to_nvidia_gets_the_override_hint(flow, capsys):
    flow.platform, flow.cards, flow.nvidia = LINUX, [Card("an AMD GPU (gfx1031)", "gfx1031")], True
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "HSA_OVERRIDE_GFX_VERSION=10.3.0" in out and "--yes --ignore-old-graphics" in out
    assert f"in {amd.login_profile()} (every server start" in out


@pytest.mark.parametrize("change", [dict(install_error=amd.SetupError("downloading x failed")),
                                    dict(probe_result=False)])
def test_yes_next_to_nvidia_never_says_the_server_falls_back_to_the_cpu(flow, capsys, change):
    flow.cards, flow.nvidia = [Card("AMD Radeon(TM) 780M Graphics")], True
    for key, value in change.items():
        setattr(flow, key, value)
    assert amd.main(["--yes", "--ignore-old-graphics"]) == 1
    out = say_lines(capsys)
    assert "Installing it anyway (--ignore-old-graphics)" in out and "keeps using the NVIDIA GPU" in out
    assert "CPU" not in out and "can run Whisper on it" not in out
    assert flow.fallbacks == ([] if "install_error" in change else ["the NVIDIA GPU"])


def test_the_override_hint_says_every_server_start_needs_it(flow, capsys):
    flow.platform, flow.cards = LINUX, [Card("an AMD GPU (gfx1031)", "gfx1031")]
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "HSA_OVERRIDE_GFX_VERSION=10.3.0" in out and f"in {amd.login_profile()} (every server start" in out
    assert "every server start needs it" in out and "Start button" in out
    assert flow.installs == []


def test_the_override_hint_names_the_file_bash_reads_instead_of_profile(monkeypatch, flow, tmp_path, capsys):
    # Fedora's and Arch's bash has a ~/.bash_profile, and then never reads ~/.profile.
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(amd.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".bash_profile").write_text("", encoding="ascii")
    flow.platform, flow.cards = LINUX, [Card("an AMD GPU (gfx1031)", "gfx1031")]
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "'export HSA_OVERRIDE_GFX_VERSION=10.3.0' in ~/.bash_profile" in out and "~/.profile" not in out


def test_an_older_engine_for_this_python_is_offered_as_an_update(flow, home, capsys):
    switched_on(home, ctranslate2="4.8.1")
    flow.answer = "n"
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert flow.questions == ["Update the AMD engine? [y/N] "]
    assert "is CTranslate2 4.8.1 with AMD's ROCm 7.2.1 runtime" in out
    assert "update it to CTranslate2 4.8.2 with AMD's ROCm 7.2.1 runtime" in out
    assert "Keeping the installed AMD engine; the server keeps using it." in out
    assert "the server uses the CPU" not in out and "Download the AMD engine" not in out
    assert flow.installs == [] and amd.read_config(home / "config.json") == {"engine": "rocm"}
    flow.answer = "y"
    assert amd.main([]) == 0
    assert flow.installs == [(home, WINDOWS, amd.python_tag())] and flow.probes == [home]


def test_an_older_engine_that_is_off_stays_off_on_a_no(flow, home, capsys):
    make_installed(home, WINDOWS, ctranslate2="4.8.1")
    flow.answer = "n"
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "Keeping the installed AMD engine; it stays switched off" in out and "--probe" in out


def test_an_older_engine_next_to_nvidia_is_on_and_updated_only_with_yes(flow, home, capsys):
    flow.nvidia = True
    switched_on(home, ctranslate2="4.8.1")
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "installed and switched on" in out and "updates it to CTranslate2 4.8.2" in out and "--yes" in out
    assert (flow.questions, flow.installs) == ([], [])
    assert amd.main(["--yes"]) == 0
    assert flow.installs == [(home, WINDOWS, amd.python_tag())] and flow.questions == []


@pytest.mark.parametrize("assume_yes, result", [(False, None), (True, False)])
def test_setup_does_not_call_a_failed_query_no_amd_gpu(flow, home, capsys, assume_yes, result):
    flow.detect_error = amd.DetectError("PowerShell did not answer within 20 s")
    switched_on(home)
    before = (home / "config.json").read_bytes()
    assert amd.setup(home, assume_yes=assume_yes) is result
    assert (home / "config.json").read_bytes() == before  # "Nothing was changed"
    out = say_lines(capsys)
    assert "Could not list the graphics adapters: PowerShell did not answer within 20 s" in out
    assert "again later" in out and "No AMD GPU found" not in out


@pytest.mark.parametrize("start", ["current", "older pins", "another Python"])
@pytest.mark.parametrize("case", FLOW_CASES)
def test_setup_leaves_a_switched_on_engine_alone(flow, home, case, start):
    # Only two things switch an engine that is on off: no AMD GPU at all, and a test that failed.
    for key, value in FLOW_CASES[case].items():
        setattr(flow, key, value)
    switched_on(home, **({"ctranslate2": "4.8.1"} if start == "older pins" else
                         {"tag": "cp39"} if start == "another Python" else {}))
    before = (home / "config.json").read_bytes()
    amd.main([])
    if case == "no AMD card" or (case == "the test fails" and start != "current"):
        assert "engine" not in amd.read_config(home / "config.json")  # switched off by design
        return
    assert (home / "config.json").read_bytes() == before


@pytest.mark.parametrize("argv", [[], ["--yes"]])
def test_the_wrong_python_leaves_a_switched_on_engine_alone(flow, home, capsys, argv):
    (home / "venv").mkdir(parents=True)
    switched_on(home, ctranslate2="4.8.1")  # older pins: past the "switched on" line, on to the Python check
    before = (home / "config.json").read_bytes()
    assert amd.main(argv) == (1 if argv else 0)
    assert "server's own Python" in say_lines(capsys)
    assert (home / "config.json").read_bytes() == before and flow.installs == []


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("powershell", 20),
                                   PermissionError(1260, "This program is blocked by group policy")])
def test_a_powershell_that_fails_is_not_reported_as_no_amd_gpu(monkeypatch, home, capsys, error):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)

    def run(cmd, **kwargs):
        raise error

    monkeypatch.setattr(amd.subprocess, "run", run)
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "Could not list the graphics adapters" in out and "No AMD GPU found" not in out
    assert amd.main(["--yes"]) == 1
    assert amd.main(["--status"]) == 0
    out = say_lines(capsys)
    assert "could not list the graphics adapters" in out and "no AMD GPU found" not in out
    assert "AMD engine: not installed" in out


def windows_cim(monkeypatch, value) -> list:
    """Windows with this Win32_VideoController answer; returns what reached the question, install and test."""
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "cim_json", lambda run=None: cim(value))
    reached = []
    monkeypatch.setattr(amd, "ask", lambda *args, **kwargs: reached.append("ask") or False)
    monkeypatch.setattr(amd, "install", lambda *args, **kwargs: reached.append("install"))
    monkeypatch.setattr(amd, "run_probe", lambda *args, **kwargs: reached.append("probe") or True)
    return reached


def test_an_amd_card_without_its_driver_is_sent_to_adrenalin(monkeypatch, home, capsys):
    reached = windows_cim(monkeypatch, [BASIC_AMD])
    assert amd.setup(home) is None
    assert "Adrenalin Edition 26.2.2 or newer; install it, then run" in say_lines(capsys)
    assert amd.setup(home, assume_yes=True) is False
    assert reached == []


def test_a_card_with_its_driver_is_offered_next_to_one_without(monkeypatch, home, capsys):
    reached = windows_cim(monkeypatch, [BASIC_AMD, RX7900])
    assert amd.setup(home) is None
    assert reached == ["ask"]
    assert "Found AMD Radeon RX 7900 XTX" in say_lines(capsys)


def test_a_failed_download_says_where_the_finished_ones_stay(monkeypatch, tmp_path, home, capsys):
    answers = windows_pins(monkeypatch, tmp_path)
    second = list(answers)[1]
    answers[second] = [ConnectionResetError("reset by peer"), ConnectionResetError("reset by peer")]
    monkeypatch.setattr(amd.urllib.request, "urlopen", FakeOpener(answers))
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("AMD Radeon RX 7900 XTX")], False))
    assert amd.main(["--yes"]) == 1
    downloads = home / "cache" / "rocm-download"
    assert (downloads / "rocm-python-wheels-Windows.zip").is_file()
    out = say_lines(capsys)
    assert f"The finished downloads stay in {downloads} for the next try ({amd.command('--remove')}" in out
    assert "The server keeps using the CPU." in out


def test_a_full_disk_while_unpacking_is_reported_by_setup(monkeypatch, tmp_path, home, capsys):
    monkeypatch.setattr(amd.urllib.request, "urlopen", FakeOpener(windows_pins(monkeypatch, tmp_path)))
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("AMD Radeon RX 7900 XTX")], False))

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(amd, "extract_wheel", full)
    assert amd.main(["--yes"]) == 1
    out = say_lines(capsys)
    assert "installing the AMD engine failed ([Errno 28] No space left on device)" in out
    assert "The finished downloads stay in" in out and "the AMD engine setup failed" not in out


def test_a_failed_update_says_the_server_keeps_the_engine_it_has(flow, home, capsys):
    switched_on(home, ctranslate2="4.8.1")
    flow.install_error = amd.SetupError("downloading x failed")
    downloads = amd.download_dir(home)
    downloads.mkdir(parents=True)
    (downloads / "rocm-python-wheels-Windows.zip").write_bytes(b"done")
    assert amd.main(["--yes"]) == 1
    out = say_lines(capsys)
    assert "The server keeps using the installed AMD engine." in out
    # --remove would take the engine the line above says the server keeps using.
    assert f"The finished downloads stay in {downloads} for the next try; delete that folder" in out
    assert "would delete the installed AMD engine too" in out and "deletes them)." not in out


def test_a_failed_update_of_an_engine_the_guard_gave_up_on_names_the_fallback(flow, home, capsys):
    switched_on(home, starts="2", ctranslate2="4.8.1")
    flow.install_error = amd.SetupError("downloading x failed")
    assert amd.main(["--yes"]) == 1
    out = say_lines(capsys)
    assert "The server keeps using the CPU." in out
    assert "the installed AMD engine" not in out


def test_an_amd_card_without_its_driver_next_to_nvidia_is_sent_to_adrenalin_not_to_yes(flow, home, capsys):
    flow.cards, flow.nvidia = [Card("Microsoft Basic Display Adapter")], True
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "without its driver" in out and "Adrenalin Edition 26.2.2 or newer" in out
    assert "Until then the server uses the NVIDIA GPU." in out
    assert "--yes" not in out and "not offered" not in out
    assert flow.questions == [] and flow.installs == []
    assert amd.main(["--yes"]) == 1
    assert flow.installs == []


def test_an_installed_engine_that_is_off_is_tested_again_only_when_asked(flow, home, capsys):
    make_installed(home, WINDOWS)
    assert amd.main([]) == 0
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])
    assert "--probe" in say_lines(capsys)
    assert amd.main(["--yes"]) == 0
    assert flow.installs == [] and flow.probes == [home]


def test_an_engine_switched_off_for_want_of_an_amd_gpu_is_not_said_to_have_failed_a_test(flow, home, capsys):
    # An eGPU unplugged during one setup run, plugged in again for the next: no test failed.
    switched_on(home)
    flow.cards = []
    assert amd.main([]) == 0
    assert "engine" not in amd.read_config(home / "config.json")
    say_lines(capsys)
    flow.cards = [Card("AMD Radeon RX 7900 XTX")]
    assert amd.main([]) == 0
    out = say_lines(capsys)
    assert "The AMD engine is installed but switched off; a test that passes switches it on" in out
    assert "did not pass" not in out
    assert amd.command("--probe") in out and amd.command("--remove") in out
    assert (flow.questions, flow.installs, flow.probes) == ([], [], [])


def test_an_engine_for_another_python_is_offered_again(flow, home):
    make_installed(home, WINDOWS, tag="cp39")
    assert amd.main([]) == 0
    assert flow.questions and flow.installs == [(home, WINDOWS, amd.python_tag())]


def test_probe_needs_the_side_folder(flow, home, capsys):
    assert amd.main(["--probe"]) == 1
    assert flow.probes == []
    assert "not installed" in say_lines(capsys)


@pytest.mark.parametrize("result, code", [(True, 0), (False, 1)])
def test_probe_exits_with_the_tests_verdict(flow, home, result, code):
    make_installed(home, WINDOWS)
    flow.probe_result = result
    assert amd.main(["--probe"]) == code
    assert flow.probes == [home] and flow.installs == [] and flow.questions == []


def test_setup_refuses_another_python_than_the_venvs(flow, home, capsys):
    (home / "venv").mkdir(parents=True)
    assert amd.main([]) == 0
    assert amd.main(["--yes"]) == 1
    assert flow.installs == []
    assert "server's own Python" in say_lines(capsys)


def test_the_wrong_python_hints_use_the_same_line_as_every_other_hint(monkeypatch, flow, home, capsys):
    (home / "venv").mkdir(parents=True)
    make_installed(home, WINDOWS)
    lines = []

    def spy(python, script=amd.SCRIPT, flag="", windows=None):
        lines.append((python, str(script), flag))
        return f"<{python} {flag}>"

    monkeypatch.setattr(amd, "rerun_line", spy)
    venv_python = str(amd.wrong_python(home))
    assert amd.main(["--yes"]) == 1
    assert amd.main(["--probe"]) == 1
    assert (venv_python, str(amd.SCRIPT), "") in lines and (venv_python, str(amd.SCRIPT), "--probe") in lines
    out = say_lines(capsys)
    assert f"server's own Python: run <{venv_python} >" in out and f"run <{venv_python} --probe>" in out


def test_rerun_line_works_in_command_prompt_and_in_powershell():
    safe = "C:\\Users\\multy\\.shisu-ko\\venv\\Scripts\\python.exe"
    line = amd.rerun_line(safe, "C:\\Users\\multy\\fac+ x\\server\\amd_setup.py", "--probe", windows=True)
    assert line == safe + ' "C:\\Users\\multy\\fac+ x\\server\\amd_setup.py" --probe'  # PowerShell runs a bare path
    for needs_quotes in ("C:\\Users\\John Doe\\.shisu-ko\\venv\\Scripts\\python.exe",
                         "C:\\Users\\A&B\\python.exe", "C:\\Users\\x(1)\\python.exe"):
        line = amd.rerun_line(needs_quotes, "C:\\s\\amd_setup.py", "--yes", windows=True)
        assert line.startswith(f'& "{needs_quotes}" "C:\\s\\amd_setup.py" --yes ')  # PowerShell's call operator
        assert line.endswith('(in PowerShell; in Command Prompt leave out the "& ")')
    assert amd.rerun_line(safe, "C:\\s\\amd_setup.py", windows=True) == safe + ' "C:\\s\\amd_setup.py"'


def test_rerun_line_keeps_a_dollar_or_backtick_from_powershell():
    # Between double quotes PowerShell reads $h as a variable and ` as its escape; Command Prompt takes both
    # as they are, and no single quotes: a path holding one gets a line for each.
    line = amd.rerun_line("C:\\Users\\$x\\python.exe", "C:\\s\\amd_setup.py", "--yes", windows=True)
    assert line == ("& 'C:\\Users\\$x\\python.exe' 'C:\\s\\amd_setup.py' --yes (in PowerShell; in Command Prompt: "
                    '"C:\\Users\\$x\\python.exe" "C:\\s\\amd_setup.py" --yes)')
    safe = "C:\\Python312\\python.exe"
    line = amd.rerun_line(safe, "C:\\a$b\\amd_setup.py", windows=True)
    assert line == (f"{safe} 'C:\\a$b\\amd_setup.py' (in PowerShell; in Command Prompt: "
                    f'"{safe}" "C:\\a$b\\amd_setup.py")')
    for tick in ("C:\\Users\\a`b\\python.exe", "C:\\Users\\Jo$h\\python.exe"):
        assert amd.rerun_line(tick, "C:\\s\\amd_setup.py", windows=True).startswith(f"& '{tick}' ")
    assert amd.rerun_line(safe, "C:\\a`b\\amd_setup.py", windows=True).startswith(f"{safe} 'C:\\a`b\\amd_setup.py'")
    line = amd.rerun_line("C:\\Users\\O'B$\\python.exe", "C:\\s\\amd_setup.py", windows=True)
    assert line.startswith("& 'C:\\Users\\O''B$\\python.exe' ")  # a quote mark in it is doubled
    assert amd.ps_quote("a\u2019b") == "'a\u2019\u2019b'"


def test_rerun_line_quotes_for_a_posix_shell():
    line = amd.rerun_line("/home/u/.shisu-ko/venv/bin/python", "/opt/shisu-ko/server/amd_setup.py", "--probe",
                          windows=False)
    assert line == "/home/u/.shisu-ko/venv/bin/python /opt/shisu-ko/server/amd_setup.py --probe"
    assert amd.rerun_line("/home/jo$h/python", "/opt/a`b c/amd_setup.py", windows=False) == \
        "'/home/jo$h/python' '/opt/a`b c/amd_setup.py'"


def test_command_runs_this_file_with_this_python(monkeypatch):
    assert amd.SCRIPT == _AMD_PATH
    monkeypatch.setattr(sys, "executable", "C:\\Users\\John Doe\\python.exe")
    assert amd.command("--probe") == amd.rerun_line("C:\\Users\\John Doe\\python.exe", amd.SCRIPT, "--probe")
    assert amd.command() == amd.rerun_line("C:\\Users\\John Doe\\python.exe", amd.SCRIPT)


def test_wrong_python(monkeypatch, home):
    assert amd.wrong_python(home) is None  # no venv: Docker, Nix, a hand-made setup
    venv = home / "venv"
    venv.mkdir(parents=True)
    assert amd.wrong_python(home) == venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert amd.wrong_python(home) is None


# --- --status and --remove -------------------------------------------------------------------------

@pytest.fixture
def seen(monkeypatch):
    """--status on a Windows machine with an RX 7900 XTX, without asking PowerShell."""
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("AMD Radeon RX 7900 XTX")], False))


def status(capsys) -> str:
    assert amd.main(["--status"]) == 0
    return say_lines(capsys)


def test_status_with_nothing_installed_changes_nothing(seen, home, capsys):
    out = status(capsys)
    assert "AMD Radeon RX 7900 XTX: supported" in out
    assert "AMD engine: not installed" in out
    assert "switched off (config.json engine: not set)" in out
    assert "next start uses the AMD engine: no" in out
    assert not home.exists()


def test_status_of_an_engine_that_is_on(seen, home, capsys):
    make_installed(home, WINDOWS)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    before = sorted(p.name for p in home.iterdir())
    out = status(capsys)
    assert f"AMD engine: installed in {home / 'rocm'}: CTranslate2 4.8.2, ROCm 7.2.1, for {amd.python_tag()}" in out
    assert "switched on (config.json engine: rocm)" in out
    assert "next start uses the AMD engine: yes" in out
    assert sorted(p.name for p in home.iterdir()) == before


def test_status_without_an_amd_card_leaves_a_switched_on_engine_alone(monkeypatch, home, capsys):
    # This machine: an NVIDIA GPU and no AMD one. --status reports; only setup switches the engine off.
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "detect", lambda platform: ([], True))
    switched_on(home)
    before = (home / "config.json").read_bytes()
    out = status(capsys)
    assert "no AMD GPU found" in out and "switched on (config.json engine: rocm)" in out
    assert (home / "config.json").read_bytes() == before


@pytest.mark.parametrize("change, says", [
    ("guard", "did not get a model onto the AMD GPU at its last start (a crash, a CTranslate2 that did not load, "
              "or no AMD GPU in sight)"),
    ("default", "switched off (SHISUKO_ENGINE=default)"),
    ("python", "it was installed for cp39 and this Python is"),
])
def test_status_says_why_the_engine_is_not_used(monkeypatch, seen, home, capsys, change, says):
    make_installed(home, WINDOWS, tag="cp39" if change == "python" else None)
    amd.write_config(home / "config.json", {"engine": "rocm"})
    if change == "guard":
        (home / "rocm-starts").write_text("2", encoding="utf-8")
    if change == "default":
        monkeypatch.setenv("SHISUKO_ENGINE", "default")
    out = status(capsys)
    assert says in out
    assert "next start uses the AMD engine: no" in out
    assert "last 2" not in out  # server.py's leave_rocm() writes the limit after one start


def test_status_of_an_older_engine_the_server_still_uses(monkeypatch, seen, home, capsys):
    switched_on(home, ctranslate2="4.8.1")
    out = status(capsys)
    assert "next start uses the AMD engine: yes" in out
    assert "this amd_setup.py installs CTranslate2 4.8.2 with AMD's ROCm 7.2.1 runtime" in out
    assert "was installed for" not in out
    server = server_module(monkeypatch)
    tag = amd.python_tag()
    assert server.rocm_engine_state({"engine": "rocm"}, {}, home / "rocm", tag, 0, WINDOWS)[0] is True


def test_status_of_a_marker_without_a_platform_where_there_is_no_amd_build(monkeypatch, home, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: None)
    make_installed(home, None)  # "platform": null, read as a missing key, as server.py reads it
    amd.write_config(home / "config.json", {"engine": "rocm"})
    out = status(capsys)
    assert "switched on (config.json engine: rocm)" in out
    assert "next start uses the AMD engine: no" in out


def test_status_says_when_the_driver_is_missing(monkeypatch, home, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)
    monkeypatch.setattr(amd, "cim_json", lambda run=None: cim([BASIC_AMD]))
    out = status(capsys)
    assert "Microsoft Basic Display Adapter: unknown" in out and "(the AMD driver is not installed)" in out


def test_status_when_the_adapters_cannot_be_listed(monkeypatch, home, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)

    def broken(platform):
        raise amd.DetectError("PowerShell could not be started (blocked)")

    monkeypatch.setattr(amd, "detect", broken)
    out = status(capsys)
    assert "could not list the graphics adapters: PowerShell could not be started (blocked)" in out
    assert "no AMD GPU found" not in out and "AMD engine: not installed" in out


def test_status_on_linux_names_what_is_missing(monkeypatch, home, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: LINUX)
    monkeypatch.setattr(amd, "detect", lambda platform: ([Card("an AMD GPU (gfx1031)", "gfx1031")], True))
    monkeypatch.setattr(amd, "linux_missing", lambda *args, **kwargs: ["ROCm 7.2.x: libamdhip64.so.7 not found"])
    out = status(capsys)
    assert "(gfx1031): unsupported" in out and "HSA_OVERRIDE_GFX_VERSION=10.3.0" in out
    assert "an NVIDIA GPU is present too" in out
    assert "missing: ROCm 7.2.x" in out


def test_status_elsewhere_and_when_something_breaks(monkeypatch, home, capsys):
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: None)
    assert "Windows and Linux on x86-64 only" in status(capsys)
    monkeypatch.setattr(amd, "platform_key", lambda platform=None: WINDOWS)

    def broken(platform):
        raise RuntimeError("WMI is broken")

    monkeypatch.setattr(amd, "detect", broken)
    assert "WMI is broken" in status(capsys)


def test_remove_switches_off_and_deletes_everything_it_installed(home, capsys):
    make_installed(home, LINUX)
    for leftover in ("rocm.new", "rocm.old", "cache/rocm-download"):
        (home / leftover).mkdir(parents=True)
        (home / leftover / "file").write_text("", encoding="utf-8")
    (home / "cache" / "cues").mkdir()
    (home / "rocm-starts").write_text("1", encoding="utf-8")
    amd.write_config(home / "config.json", {"engine": "rocm", "model": "small", "cookies_from_browser": "firefox"})
    assert amd.main(["--remove"]) == 0
    assert amd.read_config(home / "config.json") == {"model": "small", "cookies_from_browser": "firefox"}
    assert sorted(p.name for p in home.iterdir()) == ["cache", "config.json"]
    assert [p.name for p in (home / "cache").iterdir()] == ["cues"]  # the cue cache is not ours
    out = say_lines(capsys)
    assert "switched the AMD engine off" in out and f"deleted {home / 'rocm'}" in out


def test_remove_with_nothing_installed(home, capsys):
    assert amd.main(["--remove"]) == 0
    assert "not installed" in say_lines(capsys)
    assert not home.exists()
    home.mkdir()
    amd.write_config(home / "config.json", {"model": "small"})
    before = (home / "config.json").read_bytes()
    assert amd.main(["--remove"]) == 0
    assert (home / "config.json").read_bytes() == before


def test_remove_that_cannot_delete_still_exits_0(monkeypatch, home, capsys):
    make_installed(home, WINDOWS)

    def rmtree(path, *args, **kwargs):
        raise PermissionError("a DLL is in use")

    monkeypatch.setattr(amd.shutil, "rmtree", rmtree)
    assert amd.main(["--remove"]) == 0
    assert "could not delete" in say_lines(capsys)


def test_the_commands_exclude_each_other():
    with pytest.raises(SystemExit):
        amd.main(["--yes", "--remove"])
