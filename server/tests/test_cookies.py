"""YouTube's sign-in: the browser whose cookies every download sends, remembered in config.json.

`server.py --save-cookies-from-browser NAME` (and setup's `--setup-cookies`) write
~/.shisu-ko/config.json's "cookies_from_browser"; parse_args() reads it as the default of
--cookies-from-browser, so a bare run.cmd / run.sh start, the popup's Start button included, sends
that browser's YouTube cookies. yt_dlp.cookies is a fake in sys.modules: nothing here reads a real
browser's cookie store.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()
ROOT = Path(__file__).resolve().parent.parent.parent
REAL_IN_CONTAINER = server.in_container  # the fixture below stands in for it in every test


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    """config.json lives under tmp_path, and no test runs as if inside a container unless it says so."""
    monkeypatch.setattr(server, "APP_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(server, "in_container", lambda environ=None: False)
    return tmp_path


def config(tmp_path):
    path = tmp_path / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def cookie(name, domain, value="SECRET-VALUE"):
    return SimpleNamespace(name=name, domain=domain, value=value)


SIGNED_IN = [cookie("LOGIN_INFO", ".youtube.com"), cookie("PREF", ".youtube.com"), cookie("SID", ".google.com")]
SIGNED_OUT = [cookie("VISITOR_INFO1_LIVE", ".youtube.com"), cookie("YSC", "www.youtube.com")]


def fake_cookies(monkeypatch, jar=(), fail=None, found=True, warn=None):
    """yt_dlp.cookies with an extract_cookies_from_browser() that records the browser and returns `jar`.

    `warn` is logged twice the way yt-dlp's Chromium decryptors and Safari parser log a cookie they
    cannot read: logger.warning(msg, only_once=True), straight to the logger they were given.
    """
    calls = []

    def extract_cookies_from_browser(browser, profile=None, logger=None, **kwargs):
        calls.append(browser)
        if fail is not None:
            raise fail
        if warn is not None:
            for _ in range(2):
                logger.warning(warn, only_once=True)
        return list(jar)

    module = SimpleNamespace(
        extract_cookies_from_browser=extract_cookies_from_browser,
        _firefox_browser_dirs=lambda: iter(["/profiles"]),
        _firefox_cookie_dbs=lambda roots: iter(["/profiles/x/cookies.sqlite"] if found else []),
    )
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(cookies=module))
    monkeypatch.setitem(sys.modules, "yt_dlp.cookies", module)
    return calls


# --- the default of a start ----------------------------------------------------------------------

def test_a_bare_start_sends_the_browser_chosen_at_setup(tmp_path):
    server.write_config({"cookies_from_browser": "firefox"})
    args = server.parse_args([])
    assert args.cookies_from_browser == "firefox"
    assert server.Fetcher(args).ytdlp_options("abcdefghijk")["cookiesfrombrowser"] == ("firefox",)


def test_without_a_choice_nothing_is_sent(tmp_path):
    args = server.parse_args([])
    assert args.cookies_from_browser == ""
    assert "cookiesfrombrowser" not in server.Fetcher(args).ytdlp_options("abcdefghijk")


def test_the_flag_wins_over_the_config(tmp_path):
    server.write_config({"cookies_from_browser": "firefox"})
    assert server.parse_args(["--cookies-from-browser", "chrome"]).cookies_from_browser == "chrome"


def test_none_sends_no_browser_for_one_start(tmp_path):
    server.write_config({"cookies_from_browser": "firefox"})
    for spelling in ("none", "NONE", " none "):
        args = server.parse_args(["--cookies-from-browser", spelling])
        assert args.cookies_from_browser == ""
        assert "cookiesfrombrowser" not in server.Fetcher(args).ytdlp_options("abcdefghijk")


def test_a_cookies_file_is_not_joined_by_the_configured_browser(tmp_path):
    server.write_config({"cookies_from_browser": "firefox"})
    args = server.parse_args(["--cookies", "/data/cookies.txt"])
    assert args.cookies_from_browser == ""
    opts = server.Fetcher(args).ytdlp_options("abcdefghijk")
    assert opts["cookiefile"] == "/data/cookies.txt" and "cookiesfrombrowser" not in opts


def test_the_docker_image_never_takes_the_browser_from_the_config(monkeypatch, tmp_path):
    """Docker shares DATA_DIR with the native setup, and has no Firefox profile to read."""
    server.write_config({"cookies_from_browser": "firefox"})
    monkeypatch.setattr(server, "in_container", lambda environ=None: True)
    assert server.parse_args([]).cookies_from_browser == ""
    assert server.parse_args(["--cookies-from-browser", "firefox"]).cookies_from_browser == "firefox", "the flag still counts"


def container_markers(monkeypatch):
    """/.dockerenv and /run/.containerenv exist, as in any toolbox or distrobox; the real in_container()."""
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda path: path in ("/.dockerenv", "/run/.containerenv") or real_exists(path))
    monkeypatch.setattr(server, "in_container", REAL_IN_CONTAINER)


def test_in_container_follows_the_dockerfile_variable(monkeypatch):
    container_markers(monkeypatch)
    assert server.in_container({"SHISUKO_CONTAINER": "1"}) is True
    assert server.in_container({}) is False, "a container's marker files are not the Docker image"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "    SHISUKO_CONTAINER=1 \\\n" in dockerfile


def test_toolbox_and_distrobox_send_the_browser_their_setup_saved(monkeypatch, tmp_path):
    """They share the home folder and its Firefox profile: setup reads and saves it there, so a start must send it."""
    container_markers(monkeypatch)
    monkeypatch.delenv("SHISUKO_CONTAINER", raising=False)
    server.write_config({"cookies_from_browser": "firefox"})
    assert server.parse_args([]).cookies_from_browser == "firefox"
    monkeypatch.setenv("SHISUKO_CONTAINER", "1")
    assert server.parse_args([]).cookies_from_browser == "", "the Docker image still never does"


@pytest.mark.parametrize("value", [7, ["firefox"], {"name": "firefox"}, True, "netscape", "firefox:default"])
def test_a_config_browser_yt_dlp_cannot_read_is_ignored_with_a_warning(tmp_path, caplog, value):
    server.write_config({"cookies_from_browser": value})
    with caplog.at_level(logging.WARNING):
        assert server.parse_args([]).cookies_from_browser == ""
    assert "Ignoring cookies_from_browser" in caplog.text


def test_an_empty_config_browser_is_unset_without_a_warning(tmp_path, caplog):
    server.write_config({"cookies_from_browser": ""})
    with caplog.at_level(logging.WARNING):
        assert server.configured_cookies_browser() is None
    assert caplog.text == ""


def test_a_config_browser_is_read_in_any_case(tmp_path):
    server.write_config({"cookies_from_browser": " Firefox "})
    assert server.configured_cookies_browser() == "firefox"


def test_the_browsers_are_the_ones_yt_dlp_reads():
    pytest.importorskip("yt_dlp.cookies")
    import yt_dlp.cookies

    assert set(server.COOKIE_BROWSERS) == set(yt_dlp.cookies.SUPPORTED_BROWSERS)


# --- --save-cookies-from-browser -----------------------------------------------------------------

def test_saving_a_signed_in_browser(monkeypatch, tmp_path, capsys):
    server.write_config({"model": "small"})
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    assert server.run_save_cookies("Firefox") == 0
    assert calls == ["firefox"]
    assert config(tmp_path) == {"model": "small", "cookies_from_browser": "firefox"}
    out = capsys.readouterr().out
    assert "(signed in)" in out and "SECRET-VALUE" not in out


def test_saving_a_browser_that_is_not_signed_in_saves_and_says_so(monkeypatch, tmp_path, capsys):
    fake_cookies(monkeypatch, SIGNED_OUT)
    assert server.run_save_cookies("firefox") == 0
    assert config(tmp_path) == {"cookies_from_browser": "firefox"}
    assert "not signed in to YouTube" in capsys.readouterr().out


def test_a_browser_without_youtube_cookies_is_refused(monkeypatch, tmp_path, capsys):
    """Chrome and Edge on Windows keep their cookies from other programs: yt-dlp reads none there."""
    fake_cookies(monkeypatch, [cookie("SID", ".google.com"), cookie("x", "notyoutube.com"), cookie("y", "youtube.com.evil")])
    assert server.run_save_cookies("chrome") == 2
    assert config(tmp_path) is None
    assert "holds no YouTube cookies" in capsys.readouterr().out


def test_a_store_that_cannot_be_read_is_refused_with_the_reason(monkeypatch, tmp_path, capsys):
    server.write_config({"cookies_from_browser": "firefox"})
    fake_cookies(monkeypatch, fail=OSError("could not find firefox cookies database\nin /x"))
    assert server.run_save_cookies("edge") == 2
    assert config(tmp_path) == {"cookies_from_browser": "firefox"}, "the earlier choice stands"
    assert "Could not read edge's cookies (could not find firefox cookies database in /x)" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["netscape", "", "firefox:default", "../x"])
def test_a_name_yt_dlp_does_not_know_is_refused_before_anything_is_read(monkeypatch, tmp_path, capsys, name):
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    assert server.run_save_cookies(name) == 2
    assert calls == [] and config(tmp_path) is None
    assert "firefox" in capsys.readouterr().out, "the message lists the names that work"


def test_none_forgets_the_browser_and_keeps_the_rest(monkeypatch, tmp_path, capsys):
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    server.write_config({"model": "small", "cookies_from_browser": "firefox"})
    assert server.run_save_cookies("none") == 0
    assert config(tmp_path) == {"model": "small"}
    assert calls == [], "forgetting reads nothing"


def test_a_cookie_yt_dlp_cannot_decrypt_is_a_warning_not_a_refusal(monkeypatch, tmp_path, capsys, caplog):
    """A Chromium store mixing cookies with and without a key: yt-dlp skips the one it cannot decrypt."""
    fake_cookies(monkeypatch, SIGNED_IN, warn="cannot decrypt v11 cookies: no key found")
    with caplog.at_level(logging.WARNING):
        assert server.run_save_cookies("chrome") == 0
    assert config(tmp_path) == {"cookies_from_browser": "chrome"}
    assert "(signed in)" in capsys.readouterr().out
    assert caplog.text.count("cannot decrypt v11 cookies: no key found") == 1, "only_once is honoured"


def unwritable_config(tmp_path):
    """config.json that os.replace() cannot overwrite (a folder in its place), like a read-only file on Windows."""
    (tmp_path / "config.json").mkdir()


def test_a_config_that_cannot_be_written_ends_on_2_not_a_traceback(monkeypatch, tmp_path, capsys):
    """Exit code 1 would make run.cmd / run.sh run the command again every 5 seconds."""
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    unwritable_config(tmp_path)
    assert server.run_save_cookies("firefox") == 2
    assert calls == ["firefox"]
    assert "Could not write" in capsys.readouterr().out
    assert server.run_save_cookies("none") == 2
    assert run_main(monkeypatch, "--save-cookies-from-browser", "firefox") == 2
    assert server.run_setup_cookies(answers("y")) == 0, "setup's model download still follows"
    assert (tmp_path / "config.json").is_dir()


def run_main(monkeypatch, *argv):
    """main() the way run.cmd passes --save-cookies-from-browser; the lock and the model load are forbidden."""
    monkeypatch.setattr(sys, "argv", ["server.py", *argv])
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: pytest.fail("the instance lock was taken"))
    monkeypatch.setattr(server, "load_model", lambda *a, **k: pytest.fail("a model was loaded"))
    with pytest.raises(SystemExit) as info:
        server.main()
    return info.value.code


def test_main_saves_before_the_lock_and_ends_on_a_code_the_launchers_stop_at(monkeypatch, tmp_path):
    fake_cookies(monkeypatch, SIGNED_IN)
    assert run_main(monkeypatch, "--save-cookies-from-browser", "firefox") == 0
    assert config(tmp_path) == {"cookies_from_browser": "firefox"}
    assert run_main(monkeypatch, "--save-cookies-from-browser", "netscape") == 2  # run.cmd / run.sh end on 2, never loop


def test_main_setup_cookies_runs_before_the_lock(monkeypatch, tmp_path):
    fake_cookies(monkeypatch, SIGNED_IN, found=False)
    assert run_main(monkeypatch, "--setup-cookies") == 0


# --- --setup-cookies -----------------------------------------------------------------------------

def answers(*replies):
    """An input() that gives `replies` in turn, then the EOF of a closed stdin; records the prompts."""
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if len(asked) > len(replies):
            raise EOFError
        return replies[len(asked) - 1]

    ask.asked = asked
    return ask


def test_setup_asks_nothing_without_a_firefox_profile(monkeypatch, tmp_path, capsys):
    calls = fake_cookies(monkeypatch, SIGNED_IN, found=False)
    ask = answers("y")
    assert server.run_setup_cookies(ask) == 0
    assert ask.asked == [] and calls == [] and config(tmp_path) is None
    assert "Firefox was not found" in capsys.readouterr().out


def test_setup_yes_saves_firefox_after_reading_it(monkeypatch, tmp_path, capsys):
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    ask = answers("what", "Y")
    assert server.run_setup_cookies(ask) == 0
    assert ask.asked == ["Type Y or N: ", "Type Y or N: "], "anything but y or n asks again"
    assert calls == ["firefox"] and config(tmp_path) == {"cookies_from_browser": "firefox"}
    assert "Sign in to confirm you're not a bot" in capsys.readouterr().out


def test_setup_reads_no_cookie_before_the_yes(monkeypatch, tmp_path):
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    assert server.run_setup_cookies(answers("n")) == 0
    assert calls == [] and config(tmp_path) is None, "a no writes no file"


def test_setup_no_forgets_an_earlier_yes(monkeypatch, tmp_path):
    fake_cookies(monkeypatch, SIGNED_IN)
    server.write_config({"model": "small", "cookies_from_browser": "firefox"})
    assert server.run_setup_cookies(answers("no")) == 0
    assert config(tmp_path) == {"model": "small"}


def test_setup_no_keeps_a_browser_other_than_firefox(monkeypatch, tmp_path):
    """The question names Firefox: a chrome saved with --save-cookies-from-browser is not what a no answers."""
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    server.write_config({"model": "small", "cookies_from_browser": "chrome"})
    assert server.run_setup_cookies(answers("n")) == 0
    assert config(tmp_path) == {"model": "small", "cookies_from_browser": "chrome"} and calls == []


def test_an_endless_stdin_without_y_or_n_ends_the_question(monkeypatch, tmp_path, capsys):
    """`yes 1 | bash setup.sh`, setup.sh's unattended model pick, sends "1" here too and never closes stdin."""
    calls = fake_cookies(monkeypatch, SIGNED_IN)
    server.write_config({"cookies_from_browser": "firefox"})
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if len(asked) > 100:
            pytest.fail("setup asked forever")
        return "1"

    assert server.run_setup_cookies(ask) == 0
    assert len(asked) == server.SETUP_COOKIES_TRIES
    assert calls == [] and config(tmp_path) == {"cookies_from_browser": "firefox"}, "no answer leaves the config"
    assert "No Y or N" in capsys.readouterr().out


def test_an_unattended_setup_leaves_the_config_as_it_is(monkeypatch, tmp_path):
    fake_cookies(monkeypatch, SIGNED_IN)
    server.write_config({"cookies_from_browser": "firefox"})
    assert server.run_setup_cookies(answers()) == 0
    assert config(tmp_path) == {"cookies_from_browser": "firefox"}


def test_setup_goes_on_when_firefox_cannot_be_read(monkeypatch, tmp_path, capsys):
    fake_cookies(monkeypatch, fail=OSError("database is locked"))
    assert server.run_setup_cookies(answers("y")) == 0, "setup's model download still follows"
    assert config(tmp_path) is None
    assert "database is locked" in capsys.readouterr().out


def test_setup_asks_when_yt_dlp_cannot_tell_where_firefox_is(monkeypatch, tmp_path):
    fake_cookies(monkeypatch, SIGNED_IN)
    del sys.modules["yt_dlp.cookies"]._firefox_cookie_dbs
    ask = answers("y")
    assert server.run_setup_cookies(ask) == 0
    assert ask.asked == ["Type Y or N: "] and config(tmp_path) == {"cookies_from_browser": "firefox"}


# --- what the viewer reads -----------------------------------------------------------------------

WALL = RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser")
MEMBERS = RuntimeError("This video is members-only, join this channel")


def test_the_sign_in_wall_without_cookies_names_the_one_command_that_remembers_them():
    text = server.friendly_error(WALL)
    assert f"Run {server.save_cookies_command()} once and start the server again" in text
    assert "--cookies /data/cookies.txt" not in text, "the Docker way is the container's alone"


def test_the_command_is_the_one_this_install_starts_with():
    # setup's venv, even one made from the Nix store's Python (NixOS): sys.prefix is the venv's folder.
    assert server.save_cookies_command("/home/u/.shisu-ko/venv", windows=False) == "run.sh --save-cookies-from-browser firefox"
    assert server.save_cookies_command(r"C:\Users\u\.shisu-ko\venv", windows=True) == "run.cmd --save-cookies-from-browser firefox"
    assert server.save_cookies_command("/nix/store/0000-python3-3.12-env", windows=False) == \
        "nix run . -- --save-cookies-from-browser firefox"


def test_a_nix_install_is_told_the_nix_command(monkeypatch):
    """Nix has no setup and no venv: run.sh refuses there ("Run ./setup.sh first")."""
    monkeypatch.setattr(sys, "prefix", "/nix/store/0000-python3-3.12-env")
    for exc in (WALL, MEMBERS):
        text = server.friendly_error(exc)
        assert "nix run . -- --save-cookies-from-browser firefox" in text
        assert "run.sh" not in text and "run.cmd" not in text


def test_the_docker_image_is_told_the_cookies_file(monkeypatch):
    """The image never takes a browser from config.json: --save-cookies-from-browser would change nothing there."""
    monkeypatch.setattr(server, "in_container", lambda environ=None: True)
    for exc in (WALL, MEMBERS):
        text = server.friendly_error(exc)
        assert text.endswith("into the data folder and add --cookies /data/cookies.txt")
        assert "--save-cookies-from-browser" not in text


def status_error_max_chars():
    """The overlay's cap on a session error (content.js), which cuts everything after it."""
    content = (ROOT / "addon" / "content.js").read_text(encoding="utf-8")
    return int(re.search(r"const STATUS_ERROR_MAX_CHARS = (\d+);", content).group(1))


@pytest.mark.parametrize("prefix, windows", [("/venv", True), ("/venv", False), ("/nix/store/0000-python3-3.12-env", False)])
@pytest.mark.parametrize("container", [False, True])
def test_every_sign_in_text_fits_the_overlay(monkeypatch, prefix, windows, container):
    command = server.save_cookies_command
    monkeypatch.setattr(server, "save_cookies_command", lambda: command(prefix, windows))
    monkeypatch.setattr(server, "in_container", lambda environ=None: container)
    notes = ["", server.COOKIES_FILE_NOTE] + [f"{name}'s YouTube cookies" for name in server.COOKIE_BROWSERS]
    limit = status_error_max_chars()
    for exc in (WALL, MEMBERS):
        for note in notes:
            text = server.friendly_error(exc, note)
            assert len(text) <= limit, f"the overlay cuts {text[limit - 1:]!r} off"


def test_the_sign_in_wall_with_cookies_asks_for_a_sign_in_in_that_browser():
    args = SimpleNamespace(cookies_from_browser="firefox", cookies="")
    text = server.friendly_error(WALL, server.Fetcher(args).cookies_note())
    assert text.startswith("YouTube asks for a sign-in although the server sends firefox's YouTube cookies")
    assert "--save-cookies-from-browser" not in text
    note = server.Fetcher(SimpleNamespace(cookies_from_browser="", cookies="/data/c.txt")).cookies_note()
    assert "cookies file" in server.friendly_error(WALL, note)
    assert server.Fetcher(SimpleNamespace(cookies_from_browser="", cookies="")).cookies_note() == ""


def test_the_sign_in_wall_with_a_cookies_file_asks_for_a_fresh_file():
    """Signing in anywhere leaves an exported cookies.txt as it was (Docker's way, --cookies /data/cookies.txt)."""
    note = server.Fetcher(SimpleNamespace(cookies_from_browser="", cookies="/data/cookies.txt")).cookies_note()
    text = server.friendly_error(WALL, note)
    assert text.startswith("YouTube asks for a sign-in although the server sends the cookies file (--cookies)")
    assert "export a fresh one while signed in to YouTube, then play the video again" in text
    assert "sign in to YouTube there" not in text


def test_members_only_with_and_without_cookies():
    assert f"Run {server.save_cookies_command()} once with a member signed in there" in server.friendly_error(MEMBERS)
    assert "is not a member" in server.friendly_error(MEMBERS, "firefox's YouTube cookies")


def test_a_failed_fetch_says_what_it_sent(monkeypatch, tmp_path):
    """Fetcher.fetch() hands its cookies to friendly_error(): the badge must not ask for what was sent."""
    args = server.parse_args(["--cookies-from-browser", "firefox"])
    fetcher = server.Fetcher(args)
    monkeypatch.setattr(server, "find_cached_audio", lambda video_id: None)

    def download(session):
        raise WALL

    monkeypatch.setattr(fetcher, "download", download)
    session = server.Session(video_id="abcdefghijk", url="u", fetching=True)
    fetcher.fetch(session)
    assert session.status == "error"
    assert session.error.startswith("YouTube asks for a sign-in although the server sends firefox's YouTube cookies")
