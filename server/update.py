#!/usr/bin/env python3
"""Brings this Shisu-ko checkout up to date before the server starts.

run.cmd and run.sh run this first. A git clone is fast-forwarded to the branch it tracks
(normally origin/main); a folder downloaded as a zip from GitHub is replaced file by file
with the newest release. When server/requirements.txt changed, the new requirements are
installed into the environment that runs this script (the venv under ~/.shisu-ko).

Nothing here ever stops the server from starting: no network, local commits, local changes
that would be overwritten, or any other failure just leave the current version in place and
print why. Skip the check with `--no-update` (run.cmd --no-update) or SHISUKO_NO_UPDATE=1.

Stdlib only, on purpose: it runs before the requirements are (re)installed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

REPO = "Multysquid/shisu-ko"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
LATEST_API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ROOT = Path(__file__).resolve().parent.parent  # the checkout: server/, addon/, ...
NETWORK_TIMEOUT = 20.0  # seconds per request
FETCH_TIMEOUT = 60.0    # git fetch may have to negotiate a bit longer
MAX_ZIP_BYTES = 64 * 1024 * 1024  # a release zip is a few MB; anything bigger is not ours
KITSUNE_REQUIREMENTS = "requirements-kitsune.txt"
REQUIREMENT_FILES = ("requirements.txt", KITSUNE_REQUIREMENTS)  # under server/, each installed when it changed


def say(message: str) -> None:
    print(f"[update] {message}", flush=True)


def parse_version(text: str) -> tuple:
    """Version tuple of a tag or version string: "v0.4.1" gives (0, 4, 1); junk sorts lowest."""
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def read_version(text: str) -> Optional[str]:
    """The VERSION constant of server.py, or None when the file does not carry one."""
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def manifest_version(text: str) -> Optional[str]:
    try:
        version = json.loads(text).get("version")
    except (ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def read_text(path: Path) -> str:
    """A file's text, "" when it is not there."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def looks_like_checkout(root: Path) -> bool:
    return (root / "server" / "server.py").is_file() and (root / "addon" / "manifest.json").is_file()


def install_requirements(root: Path, name: str = "requirements.txt") -> None:
    """Install server/<name> into the interpreter running this script.

    requirements-kitsune.txt (the Kitsune models' transformers) only where kitsune_setup.py has
    installed PyTorch: a Whisper-only venv never asked for it.
    """
    requirements = root / "server" / name
    if name == KITSUNE_REQUIREMENTS and importlib.util.find_spec("torch") is None:
        return
    if sys.prefix == sys.base_prefix:  # not a venv: never touch a system Python
        say(f"the Python requirements changed; install them with: python -m pip install -r {requirements}")
        return
    say("the Python requirements changed; installing them ...")
    code = subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(requirements)])
    if code != 0:
        say("installing the requirements failed; run setup.cmd / setup.sh again if the server does not start")


def report_extension_change(old_manifest: Optional[str], new_manifest: Optional[str]) -> None:
    old, new = manifest_version(old_manifest or ""), manifest_version(new_manifest or "")
    if old and new and old != new:
        say(f"the extension changed too ({old} -> {new}): reload it in Firefox (about:debugging) "
            f"or install the new .xpi from {RELEASES_URL}")


# --- git clone ---------------------------------------------------------------------------------

def git(root: Path, *args: str, timeout: float = NETWORK_TIMEOUT) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout, env=env, check=True)
    return result.stdout.strip()


def git_file(root: Path, revision: str, path: str) -> str:
    """Contents of a tracked file at a revision, "" when it did not exist there."""
    try:
        return git(root, "show", f"{revision}:{path}")
    except subprocess.CalledProcessError:
        return ""


def git_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or exc.stdout or "").strip() or f"git exited with {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timed out"
    return str(exc)


def update_git(root: Path) -> bool:
    """Fast-forward the checkout to its upstream branch. True when files changed."""
    try:
        upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        say("the current branch has no upstream to update from; leaving it alone")
        return False
    remote = upstream.split("/", 1)[0]
    try:
        git(root, "fetch", "--quiet", remote, timeout=FETCH_TIMEOUT)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        say(f"could not reach {remote} to look for a newer version ({git_error(exc)}); starting the current one")
        return False
    old, new = git(root, "rev-parse", "HEAD"), git(root, "rev-parse", upstream)
    if old == new:
        say(f"Shisu-ko is up to date ({old[:7]})")
        return False
    try:
        git(root, "merge-base", "--is-ancestor", old, new)
    except subprocess.CalledProcessError:
        say(f"this checkout has commits that are not on {upstream}; not updating it automatically")
        return False
    old_requirements = {name: git_file(root, old, f"server/{name}") for name in REQUIREMENT_FILES}
    old_manifest = git_file(root, old, "addon/manifest.json")
    try:
        git(root, "merge", "--ff-only", "--quiet", upstream)
    except subprocess.CalledProcessError as exc:
        say(f"could not update: {git_error(exc)}")
        say("commit, stash or discard the local changes and start again; running the current version")
        return False
    say(f"updated Shisu-ko {old[:7]} -> {new[:7]}:")
    for line in git(root, "log", "--oneline", "--no-decorate", f"{old}..{new}").splitlines()[:15]:
        say(f"  {line}")
    for name in REQUIREMENT_FILES:
        if git_file(root, new, f"server/{name}") != old_requirements[name]:
            install_requirements(root, name)
    report_extension_change(old_manifest, git_file(root, new, "addon/manifest.json"))
    return True


# --- zip download ------------------------------------------------------------------------------

def http_get(url: str, limit: int = MAX_ZIP_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "shisu-ko-update",
        "Accept": "application/vnd.github+json, application/octet-stream, */*",
    })
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is larger than {limit // (1024 * 1024)} MiB")
    return data


def latest_release() -> tuple[str, str]:
    """(tag, zip URL) of the newest GitHub release."""
    info = json.loads(http_get(LATEST_API_URL, limit=1024 * 1024).decode("utf-8"))
    tag = info.get("tag_name")
    if not isinstance(tag, str) or not parse_version(tag):
        raise ValueError(f"unexpected release tag {tag!r}")
    return tag, f"https://github.com/{REPO}/archive/refs/tags/{tag}.zip"


def unpack_over(archive: bytes, root: Path) -> int:
    """Write every file of the release zip over the checkout. Returns the file count.

    Each file is written next to its target and moved into place, so a running run.cmd/run.sh
    is replaced whole, never half-written. Files the release no longer ships are left alone.
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefixes = {n[: -len("server/server.py")] for n in names if n.endswith("server/server.py")}
        if len(prefixes) != 1:
            raise ValueError("the archive does not contain a single Shisu-ko checkout")
        prefix = prefixes.pop()
        written = 0
        for name in names:
            if not name.startswith(prefix):
                continue
            parts = name[len(prefix):].split("/")
            if not parts or any(part in ("", ".", "..") for part in parts):
                continue
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(target.name + ".shisuko-new")
            try:
                with zf.open(name) as src, open(staged, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(staged, target)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
            written += 1
    return written


def update_zip(root: Path) -> bool:
    """Replace a zip-downloaded folder with the newest release. True when files changed."""
    local = read_version((root / "server" / "server.py").read_text(encoding="utf-8")) or "0"
    try:
        tag, zip_url = latest_release()
    except Exception as exc:  # noqa: BLE001
        say(f"could not look up the newest release ({exc}); starting the current version {local}")
        return False
    if parse_version(tag) <= parse_version(local):
        say(f"Shisu-ko {local} is the newest release")
        return False
    say(f"Shisu-ko {tag.lstrip('v')} is available (this folder has {local}); downloading it ...")
    try:
        archive = http_get(zip_url)
    except Exception as exc:  # noqa: BLE001
        say(f"the download failed ({exc}); starting the current version")
        return False
    manifest = root / "addon" / "manifest.json"
    old_requirements = {name: read_text(root / "server" / name) for name in REQUIREMENT_FILES}
    old_manifest = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
    try:
        count = unpack_over(archive, root)
    except Exception as exc:  # noqa: BLE001
        say(f"unpacking the release failed ({exc}); this folder may now mix two versions. "
            f"Download {zip_url} and unpack it over this folder to repair it")
        return False
    say(f"updated Shisu-ko {local} -> {tag.lstrip('v')} ({count} files)")
    for name in REQUIREMENT_FILES:
        if read_text(root / "server" / name) != old_requirements[name]:
            install_requirements(root, name)
    report_extension_change(old_manifest, manifest.read_text(encoding="utf-8"))
    return True


# --- entry point -------------------------------------------------------------------------------

def skipped(argv: list[str], environ=os.environ) -> bool:
    return "--no-update" in argv or environ.get("SHISUKO_NO_UPDATE", "").strip() not in ("", "0")


def update(root: Path = ROOT) -> bool:
    """Look for a newer version and install it. True when files changed; never raises."""
    try:
        if not looks_like_checkout(root):
            say(f"{root} is not a Shisu-ko checkout; skipping the update check")
            return False
        if (root / ".git").exists():
            if shutil.which("git") is None:
                say("this is a git checkout but git is not on the PATH; skipping the update check")
                return False
            return update_git(root)
        return update_zip(root)
    except Exception as exc:  # noqa: BLE001
        say(f"the update check failed ({exc}); starting the current version")
        return False


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):  # commit messages may not fit the console's code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    if not skipped(argv):
        update()
    return 0


if __name__ == "__main__":
    sys.exit(main())
