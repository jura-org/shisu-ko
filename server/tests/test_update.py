"""server/update.py: the update step run.cmd / run.sh perform before the server starts.

The git scenarios drive the real git against a bare repository in a temp dir; the zip
scenarios feed a locally built archive in place of the GitHub download. Nothing touches
the network or the real checkout.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_UPDATE_PATH = Path(__file__).resolve().parent.parent / "update.py"


def load_update():
    name = "shisuko_update"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _UPDATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


update = load_update()

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# --- pure helpers ------------------------------------------------------------------------------

def test_parse_version_orders_tags_numerically():
    assert update.parse_version("v0.4.1") == (0, 4, 1)
    assert update.parse_version("0.10.0") > update.parse_version("v0.9.9")
    assert update.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert update.parse_version("latest") == ()
    assert update.parse_version("latest") < update.parse_version("0.0.1")


def test_read_version_finds_the_constant_of_server_py():
    assert update.read_version('import x\nVERSION = "0.4.0"\nOTHER_VERSION = "9"\n') == "0.4.0"
    assert update.read_version("# no version here\n") is None
    assert update.read_version((Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8"))


def test_manifest_version_tolerates_bad_json():
    assert update.manifest_version(json.dumps({"version": "0.4.0"})) == "0.4.0"
    assert update.manifest_version("{not json") is None
    assert update.manifest_version(json.dumps({"version": 4})) is None


def test_skipped_by_flag_or_environment():
    assert not update.skipped(["--model", "small"], environ={})
    assert update.skipped(["--model", "small", "--no-update"], environ={})
    assert update.skipped([], environ={"SHISUKO_NO_UPDATE": "1"})
    assert not update.skipped([], environ={"SHISUKO_NO_UPDATE": "0"})
    assert not update.skipped([], environ={"SHISUKO_NO_UPDATE": ""})


def test_main_skips_the_check_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(update, "update", lambda: calls.append(1))
    assert update.main(["--no-update"]) == 0
    assert calls == []
    assert update.main(["--model", "small"]) == 0
    assert calls == [1]


def test_update_ignores_folders_that_are_not_a_checkout(tmp_path, capsys):
    assert update.update(tmp_path) is False
    assert "not a Shisu-ko checkout" in capsys.readouterr().out


# --- a fake checkout ---------------------------------------------------------------------------

def write_checkout(root: Path, version: str = "0.4.0", requirements: str = "yt-dlp>=1\n", extra: str = "") -> None:
    (root / "server").mkdir(parents=True, exist_ok=True)
    (root / "addon").mkdir(parents=True, exist_ok=True)
    (root / "server" / "server.py").write_text(f'VERSION = "{version}"\n{extra}', encoding="utf-8")
    (root / "server" / "requirements.txt").write_text(requirements, encoding="utf-8")
    (root / "server" / "run.cmd").write_text(f"@echo off\r\nREM {version}\r\n", encoding="utf-8", newline="")
    (root / "addon" / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")


def install_calls(monkeypatch):
    calls = []
    # requirements.txt as the root alone, any other file as (root, name)
    monkeypatch.setattr(update, "install_requirements",
                        lambda root, name="requirements.txt": calls.append(root if name == "requirements.txt" else (root, name)))
    return calls


# --- git checkout ------------------------------------------------------------------------------

def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """A bare "origin", a clone that plays the user's checkout and one that plays upstream."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n", encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], check=True)
    author = tmp_path / "author"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(author)], check=True, capture_output=True)
    write_checkout(author)
    run_git(author, "add", "-A")
    run_git(author, "commit", "--quiet", "-m", "0.4.0")
    run_git(author, "push", "--quiet", "-u", "origin", "main")
    user = tmp_path / "user"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(user)], check=True, capture_output=True)
    return origin, author, user


def publish(author: Path, message: str, **files) -> str:
    write_checkout(author, **files)
    run_git(author, "add", "-A")
    run_git(author, "commit", "--quiet", "-m", message)
    run_git(author, "push", "--quiet")
    return run_git(author, "rev-parse", "HEAD")


@needs_git
def test_git_up_to_date_changes_nothing(repos, monkeypatch, capsys):
    _, _, user = repos
    calls = install_calls(monkeypatch)
    head = run_git(user, "rev-parse", "HEAD")
    assert update.update(user) is False
    assert run_git(user, "rev-parse", "HEAD") == head
    assert calls == []
    assert "up to date" in capsys.readouterr().out


@needs_git
def test_git_fast_forwards_and_reports_the_commits(repos, monkeypatch, capsys):
    _, author, user = repos
    calls = install_calls(monkeypatch)
    new = publish(author, "Fix the thing", version="0.4.1")
    assert update.update(user) is True
    assert run_git(user, "rev-parse", "HEAD") == new
    assert (user / "server" / "run.cmd").read_bytes() == b"@echo off\r\nREM 0.4.1\r\n"
    out = capsys.readouterr().out
    assert "updated Shisu-ko" in out and "Fix the thing" in out
    assert "the extension changed too (0.4.0 -> 0.4.1)" in out
    assert calls == [], "the requirements did not change"


@needs_git
def test_git_installs_requirements_only_when_they_changed(repos, monkeypatch, capsys):
    _, author, user = repos
    calls = install_calls(monkeypatch)
    publish(author, "Need newer yt-dlp", requirements="yt-dlp>=2\n")
    assert update.update(user) is True
    assert calls == [user]
    assert "extension changed" not in capsys.readouterr().out


@needs_git
def test_git_keeps_local_changes_that_would_be_overwritten(repos, monkeypatch, capsys):
    _, author, user = repos
    calls = install_calls(monkeypatch)
    head = run_git(user, "rev-parse", "HEAD")
    (user / "server" / "server.py").write_text('VERSION = "0.4.0"\n# my experiment\n', encoding="utf-8")
    publish(author, "Conflicting change", version="0.4.1")
    assert update.update(user) is False
    assert run_git(user, "rev-parse", "HEAD") == head
    assert "# my experiment" in (user / "server" / "server.py").read_text(encoding="utf-8")
    assert calls == []
    assert "could not update" in capsys.readouterr().out


@needs_git
def test_git_fast_forwards_around_unrelated_local_changes(repos, monkeypatch):
    _, author, user = repos
    install_calls(monkeypatch)
    (user / "notes.txt").write_text("untracked\n", encoding="utf-8")
    (user / "addon" / "manifest.json").write_text(json.dumps({"version": "0.4.0", "mine": True}), encoding="utf-8")
    new = publish(author, "Server only", extra="# more server code\n")
    assert update.update(user) is True
    assert run_git(user, "rev-parse", "HEAD") == new
    assert (user / "notes.txt").exists()
    assert json.loads((user / "addon" / "manifest.json").read_text(encoding="utf-8"))["mine"] is True


@needs_git
def test_git_leaves_a_diverged_checkout_alone(repos, monkeypatch, capsys):
    _, author, user = repos
    install_calls(monkeypatch)
    (user / "local.txt").write_text("x", encoding="utf-8")
    run_git(user, "add", "-A")
    run_git(user, "commit", "--quiet", "-m", "Local work")
    head = run_git(user, "rev-parse", "HEAD")
    publish(author, "Upstream work", version="0.4.1")
    assert update.update(user) is False
    assert run_git(user, "rev-parse", "HEAD") == head
    assert "commits that are not on origin/main" in capsys.readouterr().out


@needs_git
def test_git_without_an_upstream_branch_does_nothing(repos, monkeypatch, capsys):
    _, author, user = repos
    install_calls(monkeypatch)
    run_git(user, "checkout", "--quiet", "--detach")
    publish(author, "Upstream work", version="0.4.1")
    assert update.update(user) is False
    assert "no upstream" in capsys.readouterr().out


@needs_git
def test_git_unreachable_remote_starts_the_current_version(repos, monkeypatch, capsys):
    origin, _, user = repos
    install_calls(monkeypatch)
    run_git(user, "remote", "set-url", "origin", str(origin.with_name("gone.git")))
    head = run_git(user, "rev-parse", "HEAD")
    assert update.update(user) is False
    assert run_git(user, "rev-parse", "HEAD") == head
    assert "could not reach origin" in capsys.readouterr().out


# --- zip download ------------------------------------------------------------------------------

def release_zip(version: str, prefix: str | None = None, requirements: str = "yt-dlp>=1\n", extra_entries=()) -> bytes:
    prefix = f"shisu-ko-{version}/" if prefix is None else prefix
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(prefix, "")
        zf.writestr(prefix + "server/", "")
        zf.writestr(prefix + "server/server.py", f'VERSION = "{version}"\n')
        zf.writestr(prefix + "server/requirements.txt", requirements)
        zf.writestr(prefix + "server/run.cmd", f"@echo off\r\nREM {version}\r\n")
        zf.writestr(prefix + "addon/manifest.json", json.dumps({"version": version}))
        zf.writestr(prefix + "docs/new-file.md", "new\n")
        for name, data in extra_entries:
            zf.writestr(name, data)
    return buffer.getvalue()


def fake_github(monkeypatch, tag: str, archive: bytes):
    downloads = []
    monkeypatch.setattr(update, "latest_release", lambda: (tag, f"https://example.invalid/{tag}.zip"))
    monkeypatch.setattr(update, "http_get", lambda url, limit=0: downloads.append(url) or archive)
    return downloads


def test_zip_replaces_the_files_of_a_downloaded_folder(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path)
    (tmp_path / ".env").write_text("DATA_DIR=/data\n", encoding="utf-8")
    (tmp_path / "server" / "old-tool.py").write_text("keep me\n", encoding="utf-8")
    calls = install_calls(monkeypatch)
    downloads = fake_github(monkeypatch, "v0.5.0", release_zip("0.5.0"))
    assert update.update(tmp_path) is True
    assert downloads == ["https://example.invalid/v0.5.0.zip"]
    assert update.read_version((tmp_path / "server" / "server.py").read_text(encoding="utf-8")) == "0.5.0"
    assert (tmp_path / "server" / "run.cmd").read_bytes() == b"@echo off\r\nREM 0.5.0\r\n"
    assert (tmp_path / "docs" / "new-file.md").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "DATA_DIR=/data\n"
    assert (tmp_path / "server" / "old-tool.py").read_text(encoding="utf-8") == "keep me\n"
    assert not list(tmp_path.rglob("*.shisuko-new"))
    out = capsys.readouterr().out
    assert "updated Shisu-ko 0.4.0 -> 0.5.0" in out
    assert "the extension changed too (0.4.0 -> 0.5.0)" in out
    assert calls == []


def test_zip_installs_requirements_when_they_changed(tmp_path, monkeypatch):
    write_checkout(tmp_path)
    calls = install_calls(monkeypatch)
    fake_github(monkeypatch, "v0.4.1", release_zip("0.4.1", requirements="yt-dlp>=2\n"))
    assert update.update(tmp_path) is True
    assert calls == [tmp_path]


def test_zip_installs_the_kitsune_requirements_when_they_changed(tmp_path, monkeypatch):
    write_checkout(tmp_path)
    (tmp_path / "server" / "requirements-kitsune.txt").write_text("transformers>=5.13\n", encoding="utf-8")
    calls = install_calls(monkeypatch)
    entries = [("shisu-ko-0.4.1/server/requirements-kitsune.txt", "transformers>=5.20\n")]
    fake_github(monkeypatch, "v0.4.1", release_zip("0.4.1", extra_entries=entries))
    assert update.update(tmp_path) is True
    assert calls == [(tmp_path, "requirements-kitsune.txt")]


def test_the_kitsune_requirements_are_installed_only_where_pytorch_is(tmp_path, monkeypatch, capsys):
    (tmp_path / "server").mkdir()
    pip = []
    monkeypatch.setattr(update.subprocess, "call", lambda cmd: pip.append(cmd) or 0)
    monkeypatch.setattr(update.sys, "prefix", "/venv")  # a venv, where installing is allowed
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: None)
    update.install_requirements(tmp_path, "requirements-kitsune.txt")
    assert pip == []
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: object())
    update.install_requirements(tmp_path, "requirements-kitsune.txt")
    assert pip and pip[0][-1].endswith("requirements-kitsune.txt")


def test_zip_does_not_download_an_older_or_equal_release(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path, version="0.4.0")
    install_calls(monkeypatch)
    downloads = fake_github(monkeypatch, "v0.4.0", release_zip("0.4.0"))
    assert update.update(tmp_path) is False
    assert downloads == []
    assert "newest release" in capsys.readouterr().out
    write_checkout(tmp_path, version="0.9.0")
    assert update.update(tmp_path) is False
    assert downloads == []


def test_zip_skips_unsafe_entries_and_foreign_prefixes(tmp_path, monkeypatch):
    write_checkout(tmp_path)
    install_calls(monkeypatch)
    archive = release_zip("0.5.0", extra_entries=[
        ("shisu-ko-0.5.0/../escaped.txt", "bad"),
        ("shisu-ko-0.5.0/server/../../escaped2.txt", "bad"),
        ("other-project/server/x.py", "bad"),
    ])
    fake_github(monkeypatch, "v0.5.0", archive)
    assert update.update(tmp_path) is True
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert not (tmp_path.parent / "escaped2.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "other-project").exists()
    assert not (tmp_path / "server" / "x.py").exists()


def test_zip_rejects_an_archive_without_a_checkout(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path)
    install_calls(monkeypatch)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("something/else.txt", "x")
    fake_github(monkeypatch, "v0.5.0", buffer.getvalue())
    assert update.update(tmp_path) is False
    assert update.read_version((tmp_path / "server" / "server.py").read_text(encoding="utf-8")) == "0.4.0"
    assert "unpacking the release failed" in capsys.readouterr().out


def test_zip_lookup_failure_starts_the_current_version(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path)
    install_calls(monkeypatch)

    def offline():
        raise OSError("name resolution failed")

    monkeypatch.setattr(update, "latest_release", offline)
    assert update.update(tmp_path) is False
    assert "could not look up the newest release" in capsys.readouterr().out


def test_latest_release_parses_the_github_answer(monkeypatch):
    monkeypatch.setattr(update, "http_get", lambda url, limit=0: json.dumps({"tag_name": "v0.4.0"}).encode())
    assert update.latest_release() == ("v0.4.0", "https://github.com/Multysquid/shisu-ko/archive/refs/tags/v0.4.0.zip")
    monkeypatch.setattr(update, "http_get", lambda url, limit=0: json.dumps({"message": "Not Found"}).encode())
    with pytest.raises(ValueError):
        update.latest_release()


def test_install_requirements_never_touches_a_system_python(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path)
    monkeypatch.setattr(update.sys, "prefix", "/usr")
    monkeypatch.setattr(update.sys, "base_prefix", "/usr")
    monkeypatch.setattr(update.subprocess, "call", lambda *a, **k: pytest.fail("pip must not run"))
    update.install_requirements(tmp_path)
    assert "pip install -r" in capsys.readouterr().out
