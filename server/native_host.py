#!/usr/bin/env python3
"""Native-messaging host behind the popup's "Start server" button.

A WebExtension cannot start a program. Firefox can hand a message to a native-messaging host
registered on the machine, so the extension sends {"cmd": "start"} to the host named "shisuko"
and this file starts the launcher next to it, server/run.cmd or server/run.sh. The message
never carries a path, a program or arguments: the only thing the host can run is that
launcher, and the only other thing it answers is whether the server is up.

Protocol (Firefox's: 4-byte little-endian length, then UTF-8 JSON, one request per message,
answered in order until stdin closes):
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
and the extension id); they are ignored. Registration (`native_host.py --register`) writes the
host manifest Firefox looks for, and on Windows the registry value pointing at it. setup.cmd /
setup.sh and run.cmd / run.sh run it, so the button works once the server was set up or
started by hand. `--unregister` takes both away again, `--status` says which it is; all quiet
unless `--verbose`.

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
    import winreg  # Windows only; the registry is where Firefox looks for hosts there
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
DESCRIPTION = "Starts the Shisu-ko transcription server"
ROOT = Path(__file__).resolve().parent.parent  # the checkout: server/, addon/, ...
PORT = 8790
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
HEALTH_TIMEOUT = 1.5  # seconds; a server that is up answers at once
LOCK_NAME = f"server-{PORT}.lock"  # held by server.py from before its model load until it exits
MAX_MESSAGE_BYTES = 1024 * 1024  # a request is a few bytes; anything bigger is not ours
REGISTRY_KEY = rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}"
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


def manifest(wrapper: Path) -> dict:
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": str(wrapper),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }


def manifest_path(home: Optional[Path] = None, environ=os.environ, platform: str = sys.platform) -> Path:
    """Where Firefox looks for the host manifest on this platform (Windows: where the registry points)."""
    if platform == "win32":
        return data_dir(home, environ) / "native-messaging" / f"{HOST_NAME}.json"
    home = home or user_home(environ)
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts" / f"{HOST_NAME}.json"
    return home / ".mozilla" / "native-messaging-hosts" / f"{HOST_NAME}.json"


def registry_value() -> Optional[str]:
    """The manifest path the registry names, or None when the host is not registered there."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            value, kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    return value if kind == winreg.REG_SZ and isinstance(value, str) else None


def register(root: Path = ROOT, home: Optional[Path] = None, environ=os.environ) -> Path:
    """Write the host manifest (and the registry value on Windows). Returns the manifest path."""
    wrapper = wrapper_path(root)
    if not WINDOWS and wrapper.is_file():
        # Firefox executes the wrapper itself, so it needs its mode bit, which a zip install
        # (update.py writes every file without one) or a copy through a mode-blind tool drops.
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
    path = manifest_path(home, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest(wrapper), indent=2) + "\n", encoding="utf-8")
    if WINDOWS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))
    return path


def unregister(home: Optional[Path] = None, environ=os.environ) -> bool:
    """Remove the manifest and the registry value. True when there was something to remove."""
    path = manifest_path(home, environ)
    removed = False
    if path.is_file():
        path.unlink()
        removed = True
    if WINDOWS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY)
            removed = True
        except OSError:
            pass
    return removed


def registered(home: Optional[Path] = None, environ=os.environ) -> Optional[Path]:
    """The manifest path when Firefox would find the host from here, else None."""
    path = manifest_path(home, environ)
    if WINDOWS:
        value = registry_value()
        if not value or not Path(value).is_file():
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
    path = registered(home, environ)
    if path is None:
        return "not registered (run setup or start the server once)"
    text = f"registered at {path}"
    try:
        target = json.loads(path.read_text(encoding="utf-8")).get("path")
    except (OSError, ValueError, AttributeError):
        target = None
    if not same_file(target, str(wrapper_path(root))):
        text += f" (points at {target}; run native_host.py --register for this checkout)"
    return text


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
        try:
            path = register()
        except Exception as exc:  # noqa: BLE001
            say(f"could not register the Start button launcher ({one_line(exc)})")
            return 1
        if verbose:
            print(f"registered the Start button launcher at {path}")
        return 0
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
