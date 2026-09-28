#!/usr/bin/env python3
"""Native-messaging host behind the popup's "Start server" button.

A WebExtension cannot start a program. Firefox and Chrome can hand a message to a
native-messaging host registered on the machine, so the extension sends {"cmd": "start"} to the
host named "shisuko" and this file starts the launcher next to it, server/run.cmd or
server/run.sh. The message never carries a path, a program or arguments: the only thing the host
can run is that launcher, and the only other thing it answers is whether the server is up.

Protocol (the browsers' own: 4-byte little-endian length, then UTF-8 JSON, one request per
message, answered in order until stdin closes):
  {"cmd": "status"} -> {"ok": true, "running": bool, "version": "...", "root": "..."}
  {"cmd": "start"}  -> {"ok": true, "already": true}
                    |  {"ok": true, "already": true, "starting": true}   (launched, still loading)
                    |  {"ok": true, "started": true, "log": path or null}
                    |  {"ok": false, "error": "..."}
  anything else     -> {"ok": false, "error": "unknown command"}

"Running" is a /health answer. A server that is loading its model does not answer yet, so
server.py holds a lock file in the data directory from before the load until it exits, and a
second "start" in that time is told "already" instead of launching a second server.

The browser passes arguments of its own when it starts the host (Firefox: the manifest path
and the extension id; Chrome: the extension's origin); they are ignored. Registration
(`native_host.py --register`) writes one host manifest per browser where that browser looks for
it, under the user's own profile (Firefox; Chrome; Chromium on Linux), and on Windows the
registry values pointing at them. Firefox's names the add-on's id; Chrome's names the origin of
the Chrome Web Store install, the only one whose id is fixed (an unpacked build's id comes from
its folder's path). setup.cmd / setup.sh and run.cmd / run.sh run it, so the button works once
the server was set up or started by hand. `--unregister` takes all of it away again, `--status`
says which it is; all quiet unless `--verbose`.

Stdlib only, on purpose: the wrapper falls back to the system Python when the venv is missing.
"""
from __future__ import annotations

import errno
import json
import os
import re
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import winreg  # Windows only; the registry is where the browsers look for hosts there
except ImportError:  # pragma: no cover - not Windows
    winreg = None  # type: ignore[assignment]
try:
    import msvcrt  # Windows: byte-range locks for the instance lock
except ImportError:  # pragma: no cover - not Windows
    msvcrt = None  # type: ignore[assignment]
try:
    import fcntl  # POSIX: flock for the same
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

HOST_NAME = "shisuko"
EXTENSION_ID = "shisu-ko@multysquid.github.io"
# The Chrome Web Store install's id (popup.js has a copy, CHROME_STORE_ID). Chrome lets only the
# origins a host manifest names reach the host, and ids differ per install everywhere else.
CHROME_EXTENSION_ID = "ecenifonpkaiccmmknpbllbebbfigjnm"
CHROME_ORIGIN = f"chrome-extension://{CHROME_EXTENSION_ID}/"
FIREFOX, CHROME, CHROMIUM = "Firefox", "Chrome", "Chromium"
DESCRIPTION = "Starts the Shisu-ko transcription server"
ROOT = Path(__file__).resolve().parent.parent  # the checkout: server/, addon/, ...
PORT = 8790
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
HEALTH_TIMEOUT = 1.5  # seconds; a server that is up answers at once
LOCK_NAME = f"server-{PORT}.lock"  # held by server.py from before its model load until it exits
MAX_MESSAGE_BYTES = 1024 * 1024  # a request is a few bytes; anything bigger is not ours
REGISTRY_KEY = rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}"
CHROME_REGISTRY_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
REGISTRY_KEYS = {FIREFOX: REGISTRY_KEY, CHROME: CHROME_REGISTRY_KEY}  # under HKEY_CURRENT_USER
WINDOWS = sys.platform == "win32"


def server_version(root: Path = ROOT) -> str:
    """server.py's VERSION, read with a regex: the host never imports server.py (numpy, faster-whisper)."""
    try:
        text = (root / "server" / "server.py").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


VERSION = server_version()  # one version per checkout, the one the manifest and server.py carry


def say(message: str) -> None:
    """Diagnostics go to stderr: stdout carries only protocol messages while serving."""
    print(f"[native-host] {message}", file=sys.stderr, flush=True)


def one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:200]


# --- framing -----------------------------------------------------------------------------------

class MessageTooLarge(ValueError):
    """The length prefix announces more than MAX_MESSAGE_BYTES; the stream cannot be trusted."""


EOF = object()  # end of stream; not None, which a frame can legitimately carry (JSON null)


def read_message(stream) -> Any:
    """The next JSON message on a binary stream, or EOF when it ends (also mid-message)."""
    header = stream.read(4)
    if len(header) < 4:
        return EOF
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        raise MessageTooLarge(f"a message of {length} bytes exceeds {MAX_MESSAGE_BYTES}")
    data = stream.read(length)
    if len(data) < length:
        return EOF
    return json.loads(data.decode("utf-8"))


def write_message(stream, obj: Any) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


# --- commands ----------------------------------------------------------------------------------

def server_running(url: str = HEALTH_URL, timeout: float = HEALTH_TIMEOUT) -> bool:
    """True when the transcription server answers /health; any failure counts as not running."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def user_home(environ=os.environ) -> Path:
    """The user's home directory the way Path.home() finds it, but overridable for tests."""
    value = environ.get("USERPROFILE" if WINDOWS else "HOME")
    return Path(value) if value else Path.home()


def data_dir(home: Optional[Path] = None, environ=os.environ) -> Path:
    """~/.shisu-ko, or SHISUKO_HOME, like server.py's APP_DIR."""
    override = environ.get("SHISUKO_HOME")
    if override:
        return Path(override)
    return (home or user_home(environ)) / ".shisu-ko"


# What the non-blocking lock call raises while another process holds the lock: EWOULDBLOCK /
# EAGAIN from flock(), EACCES (EDEADLOCK after retries) from msvcrt.locking(). Anything else
# (ENOLCK on NFS without a lock manager, EOPNOTSUPP, ENOSYS, EINVAL) means the file cannot be
# locked at all, and must not read as "held": the button could then never start anything.
LOCK_HELD_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLOCK", -1)})


def try_lock(path: Path):
    """Lock `path` for this process, or None when another process holds it; server.py has the twin.

    The open file keeps the lock; closing it, or the process ending however it ends, releases it.
    An errno outside LOCK_HELD_ERRNOS (the file cannot be locked at all) raises, like a file
    that cannot be opened, and server_starting() then answers False as it does for that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if WINDOWS:
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


def server_starting(environ=os.environ) -> bool:
    """True while a server holds the instance lock but /health is still silent: it is loading.

    server.py takes the lock before its model load and keeps it until it exits, so between a
    launch and the first /health answer (10-40 s on a GPU, minutes for a first download) this
    tells a starting server from none. A lock that cannot be tried (an unwritable data
    directory, a filesystem without locks) counts as not starting, which is what the answer
    used to be.
    """
    try:
        handle = try_lock(data_dir(environ=environ) / LOCK_NAME)
    except OSError:
        return False
    if handle is None:
        return True
    handle.close()
    return False


def launch(root: Path = ROOT, environ=os.environ) -> dict:
    """Start the checkout's own launcher, detached so it outlives this host. Raises on failure."""
    server_dir = root / "server"
    if WINDOWS:
        run_cmd = server_dir / "run.cmd"
        if not run_cmd.is_file():
            raise FileNotFoundError(f"{run_cmd} is missing")
        # `start` opens a console window that shows the server log and keeps it after the host
        # is gone. The launcher is named relative to the working directory, never by its full
        # path: cmd.exe splits a path holding `&`, `^` or `(` even when it is quoted, and `start`
        # would then report success with nothing started. Firefox keeps its native hosts in a
        # job object; a child that stays in the job dies with the host, so break away when the
        # job allows it (it does), and fall back to a plain detached start otherwise.
        argv = ["cmd.exe", "/c", "start", "Shisu-ko server", ".\\run.cmd"]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        streams = dict(cwd=str(server_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                             close_fds=True, **streams)
        except PermissionError:
            subprocess.Popen(argv, creationflags=flags, close_fds=True, **streams)
        return {"ok": True, "started": True, "log": None}
    run_sh = server_dir / "run.sh"
    if not run_sh.is_file():
        raise FileNotFoundError(f"{run_sh} is missing")
    log_path = data_dir(environ=environ) / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Through bash, not by executing the file: a zip install (update.py writes every file
    # without its mode bits) has run.sh without the executable bit, and it needs bash anyway.
    # A new session: Firefox ends the host after the answer, and the server must not go with it.
    with open(log_path, "ab") as log:
        subprocess.Popen(["bash", str(run_sh)], cwd=str(server_dir), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    return {"ok": True, "started": True, "log": str(log_path)}


def handle(request: Any, running: Callable[[], bool] = server_running,
           launch: Callable[[Path], dict] = launch, root: Path = ROOT,
           starting: Callable[[], bool] = server_starting) -> dict:
    """Answer one request. Only a bare {"cmd": ...} object is a request; nothing else is read."""
    if not isinstance(request, dict) or set(request) != {"cmd"} or not isinstance(request["cmd"], str):
        return {"ok": False, "error": "unknown command"}
    cmd = request["cmd"]
    if cmd == "status":
        return {"ok": True, "running": bool(running()), "version": VERSION, "root": str(root)}
    if cmd == "start":
        if running():
            return {"ok": True, "already": True}
        if starting():
            # Launched but not listening yet: a second launcher would load the model a second
            # time and then fail on the port. Already running, as far as the popup is concerned,
            # so it keeps waiting for /health instead of offering the button again.
            return {"ok": True, "already": True, "starting": True}
        try:
            return launch(root)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": one_line(exc)}
    return {"ok": False, "error": "unknown command"}


def serve(stdin, stdout) -> int:
    """Answer requests until stdin closes. Binary streams, one answer per message."""
    while True:
        try:
            request = read_message(stdin)
        except MessageTooLarge as exc:
            write_message(stdout, {"ok": False, "error": one_line(exc)})
            return 1  # the rest of the stream is unreadable
        except (ValueError, RecursionError):  # not JSON, not UTF-8, nested past the parser: the frame was consumed, carry on
            write_message(stdout, {"ok": False, "error": "unknown command"})
            continue
        if request is EOF:
            return 0
        # Looked up here, not in handle()'s defaults, so a test can stand in for all three.
        write_message(stdout, handle(request, running=server_running, launch=launch, starting=server_starting))


# --- registration ------------------------------------------------------------------------------

def wrapper_path(root: Path = ROOT, platform: str = sys.platform) -> Path:
    return root / "server" / ("native-host.cmd" if platform == "win32" else "native-host.sh")


class RegistrationError(OSError):
    """Some browsers could not be registered; `done` maps the ones that were to their manifests."""

    def __init__(self, failures: list[str], done: dict[str, Path]):
        super().__init__("; ".join(failures))
        self.done = done


def browsers(platform: str = sys.platform) -> tuple[str, ...]:
    """The browsers the host is registered for: Chromium only on Linux, where it has a folder of
    its own and is common; on Windows and macOS Firefox and Google Chrome."""
    return (FIREFOX, CHROME) if platform in ("win32", "darwin") else (FIREFOX, CHROME, CHROMIUM)


def manifest(wrapper: Path, browser: str = FIREFOX) -> dict:
    """The host manifest for `browser`. Firefox's names the add-on's id; Chrome's (Chromium's too)
    takes origins instead, without wildcards: the store install's, the one id that is fixed."""
    allowed = {"allowed_extensions": [EXTENSION_ID]} if browser == FIREFOX else {"allowed_origins": [CHROME_ORIGIN]}
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": str(wrapper),
        "type": "stdio",
        **allowed,
    }


def config_home(home: Path, environ=os.environ) -> Path:
    """The folder Chrome and Chromium keep their profiles in on Linux, host manifests included,
    found as Chrome finds it (chrome_paths_linux.cc): $CHROME_CONFIG_HOME, else $XDG_CONFIG_HOME,
    else ~/.config. A variable set but empty counts as unset."""
    value = environ.get("CHROME_CONFIG_HOME") or environ.get("XDG_CONFIG_HOME")
    return Path(value) if value else home / ".config"


def manifest_path(home: Optional[Path] = None, environ=os.environ, platform: str = sys.platform,
                  browser: str = FIREFOX) -> Path:
    """Where `browser` looks for the host manifest on this platform (Windows: where the registry points)."""
    name = f"{HOST_NAME}.json"
    if platform == "win32":
        folder = data_dir(home, environ) / "native-messaging"
        return folder / (name if browser == FIREFOX else f"{HOST_NAME}-{browser.lower()}.json")
    home = home or user_home(environ)
    if platform == "darwin":
        vendor = {FIREFOX: ("Mozilla",), CHROME: ("Google", "Chrome"), CHROMIUM: ("Chromium",)}[browser]
        return home.joinpath("Library", "Application Support", *vendor, "NativeMessagingHosts", name)
    if browser == FIREFOX:
        return home / ".mozilla" / "native-messaging-hosts" / name
    profile = "google-chrome" if browser == CHROME else "chromium"
    return config_home(home, environ) / profile / "NativeMessagingHosts" / name


def registry_key(browser: str) -> Optional[str]:
    """The HKCU key whose default value names `browser`'s manifest, or None off Windows."""
    return REGISTRY_KEYS.get(browser) if WINDOWS else None


def registry_value(key: str = REGISTRY_KEY) -> Optional[str]:
    """The manifest path the registry names under `key`, or None when the host is not registered there."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, kind = winreg.QueryValueEx(handle, "")
    except OSError:
        return None
    return value if kind == winreg.REG_SZ and isinstance(value, str) else None


def register(root: Path = ROOT, home: Optional[Path] = None, environ=os.environ) -> dict[str, Path]:
    """Write every browser's host manifest (and its registry value on Windows).

    Returns browser -> manifest path. A browser whose folder cannot be written does not keep the
    others from the button: each is tried, then the failures raise RegistrationError together.
    """
    wrapper = wrapper_path(root)
    if not WINDOWS and wrapper.is_file():
        # The browser executes the wrapper itself, so it needs its mode bit, which a zip install
        # (update.py writes every file without one) or a copy through a mode-blind tool drops.
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
    done: dict[str, Path] = {}
    failures: list[str] = []
    for browser in browsers():
        path = manifest_path(home, environ, browser=browser)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest(wrapper, browser), indent=2) + "\n", encoding="utf-8")
            key = registry_key(browser)
            if key:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
                    winreg.SetValueEx(handle, "", 0, winreg.REG_SZ, str(path))
        except OSError as exc:
            failures.append(f"{browser}: {one_line(exc)}")
            continue
        done[browser] = path
    if failures:
        raise RegistrationError(failures, done)
    return done


def unregister(home: Optional[Path] = None, environ=os.environ) -> bool:
    """Remove every browser's manifest and registry value. True when there was something to remove."""
    removed = False
    failures: list[str] = []
    for browser in browsers():
        path = manifest_path(home, environ, browser=browser)
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError as exc:
            failures.append(f"{browser}: {one_line(exc)}")
        key = registry_key(browser)
        if key:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                removed = True
            except OSError:
                pass
    if failures:
        raise OSError("; ".join(failures))
    return removed


def registered(home: Optional[Path] = None, environ=os.environ, browser: str = FIREFOX) -> Optional[Path]:
    """The manifest path when `browser` would find the host from here, else None."""
    path = manifest_path(home, environ, browser=browser)
    key = registry_key(browser)
    if key:
        value = registry_value(key)
        if not value:
            return None
        path = Path(value)
    return path if path.is_file() else None


def same_file(a, b) -> bool:
    """Whether two path strings name the same file.

    macOS and Windows keep the case of a name but ignore it when looking one up, so the very same
    wrapper can be registered under a spelling that does not compare equal to this checkout's.
    """
    if not a or not b:
        return False  # nothing is the same file as nothing, however the caller spells it
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def status_text(root: Path = ROOT, home: Optional[Path] = None, environ=os.environ) -> str:
    """One line, per browser: where the host is registered and whether for this checkout."""
    found: dict[str, Optional[Path]] = {}
    unchecked: dict[str, str] = {}
    for browser in browsers():
        try:
            found[browser] = registered(home, environ, browser)
        except OSError as exc:
            # A folder this user cannot enter (a profile left root's by a browser once started
            # through sudo): is_file() raises EACCES there, and the others must still be reported.
            found[browser], unchecked[browser] = None, one_line(exc)
    if not any(found.values()) and not unchecked:
        return "not registered (run setup or start the server once)"
    wrapper = wrapper_path(root)
    parts = []
    for browser, path in found.items():
        if browser in unchecked:
            parts.append(f"{browser} could not be checked ({unchecked[browser]})")
            continue
        if path is None:
            parts.append(f"{browser} not registered (run setup or start the server once)")
            continue
        text = f"{browser} registered at {path}"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            target = data.get("path")
        except (OSError, ValueError, AttributeError):
            data = target = None
        # same_file(), not a string compare: macOS and Windows look a path up without its case, so
        # the very same wrapper can be registered under a spelling this checkout does not spell back.
        if not same_file(target, str(wrapper)):
            text += f" (points at {target}; run native_host.py --register for this checkout)"
        elif data != {**manifest(wrapper, browser), "path": target}:
            # Every key but the path, which same_file() has already judged: comparing that as text
            # a second time would call this very wrapper out of date under its other spelling.
            text += " (out of date; run native_host.py --register)"
        parts.append(text)
    return "; ".join(parts)


# --- entry point -------------------------------------------------------------------------------

USAGE = "usage: native_host.py [--register | --unregister | --status] [--verbose]"
ACTIONS = ("--status", "--register", "--unregister")


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbose = "--verbose" in argv
    actions = [a for a in argv if a in ACTIONS]
    if len(actions) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    if not actions:
        # A browser starts the host with arguments of its own (Firefox: the manifest path and
        # the extension id; Chrome: its origin and, on Windows, --parent-window=<handle>). None
        # of them is used or checked: without an action flag the host serves, whatever else is
        # on the line. A terminal is never a browser, though: a mistyped flag gets the usage
        # instead of a host waiting on the keyboard.
        try:
            interactive = sys.stdin.isatty()
        except (AttributeError, ValueError):
            interactive = False
        if interactive:
            print(USAGE, file=sys.stderr)
            return 2
        if WINDOWS:  # the pipes must carry bytes untouched; Python sets this already, but be sure
            for stream in (sys.stdin, sys.stdout):
                try:
                    msvcrt.setmode(stream.fileno(), os.O_BINARY)
                except (OSError, ValueError):  # a captured or closed stream has no descriptor
                    pass
        return serve(sys.stdin.buffer, sys.stdout.buffer)
    (action,) = actions
    if action == "--status":
        print(f"Start button launcher: {status_text()}")
        return 0
    if action == "--register":
        failed = False
        try:
            done = register()
        except Exception as exc:  # noqa: BLE001
            # A RegistrationError still names the browsers that were registered.
            done, failed = getattr(exc, "done", {}), True
            say(f"could not register the Start button launcher ({one_line(exc)})")
        if verbose:
            for browser, path in done.items():
                print(f"registered the Start button launcher for {browser} at {path}")
        return 1 if failed else 0
    try:
        removed = unregister()
    except Exception as exc:  # noqa: BLE001
        say(f"could not unregister the Start button launcher ({one_line(exc)})")
        return 1
    if verbose:
        print("removed the Start button launcher" if removed else "the Start button launcher was not registered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
