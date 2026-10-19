"""The update check: the server asks GitHub for the newest release once a day and reports it in /health.

No test reaches GitHub: fetch_latest_release() takes a stand-in for its opener, and UpdateChecker
a stand-in for the whole fetch. The version rules are background.js's, ported with its cases.
"""
from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from _serverlib import load_server
from test_update import update
from test_update_endpoint import make_app

server = load_server()

DAY = 24 * 3600.0
RELEASE = {
    "url": "https://api.github.com/repos/Multysquid/shisu-ko/releases/1",
    "html_url": "https://github.com/Multysquid/shisu-ko/releases/tag/v0.9.0",
    "tag_name": "v0.9.0",
    "name": "Shisu-ko 0.9.0",
    "published_at": "2026-10-01T12:00:00Z",
    "assets": [
        {"name": "shisu-ko-0.9.0-chrome.zip", "browser_download_url": "https://github.com/Multysquid/shisu-ko/releases/download/v0.9.0/shisu-ko-0.9.0-chrome.zip"},
        {"name": "shisu_ko-0.9.0.xpi", "browser_download_url": "https://github.com/Multysquid/shisu-ko/releases/download/v0.9.0/shisu_ko-0.9.0.xpi"},
    ],
}
LATEST = {"version": "0.9.0", "url": RELEASE["html_url"], "xpi": RELEASE["assets"][1]["browser_download_url"],
          "published": "2026-10-01T12:00:00Z"}


# ---- versions: background.js's parseVersion and compareVersions, case for case

@pytest.mark.parametrize("text, expected", [
    ("0.9.0", (0, 9, 0)), ("v0.9.0", (0, 9, 0)), ("V1.2.3", (1, 2, 3)), ("0.10.0", (0, 10, 0)), ("0.9", (0, 9, 0)),
    ("2", (2, 0, 0)), ("", (0, 0, 0)), (None, (0, 0, 0)), ("garbage", (0, 0, 0)), ("0.9.0-rc1", (0, 9, 0)),
    ("a.b.c", (0, 0, 0)), (" 1.2.3 ", (1, 2, 3)), ("1.2.3.4", (1, 2, 3)), ("-1.2.3", (0, 2, 3)),
])
def test_parse_version_matches_the_extension(text, expected):
    assert server.parse_version(text) == expected


@pytest.mark.parametrize("a, b, expected", [
    ("0.9.0", "0.8.0", 1), ("0.8.0", "0.9.0", -1), ("0.9.0", "v0.9.0", 0), ("0.10.0", "0.9.0", 1),
    ("1.0.0", "0.99.99", 1), ("0.9", "0.9.0", 0), ("0.9.1", "0.9", 1), ("garbage", "0.0.1", -1), ("", "", 0),
    ("0.9.0.1", "0.9.0", 0),
])
def test_compare_versions_matches_the_extension(a, b, expected):
    assert server.compare_versions(a, b) == expected


# ---- GitHub's answer

def test_release_from_api_reads_the_version_the_page_and_the_signed_xpi():
    assert server.release_from_api(RELEASE) == LATEST
    assert server.release_from_api({"tag_name": "0.9.1", "html_url": RELEASE["html_url"]}) == {
        "version": "0.9.1", "url": RELEASE["html_url"], "xpi": None, "published": None}
    unsigned = {"name": "shisu-ko-0.9.0-firefox-unsigned.xpi", "browser_download_url": "https://x/u.xpi"}
    signed = {"name": "shisu_ko-0.9.0.xpi", "browser_download_url": "https://x/s.xpi"}
    assert server.release_from_api({"tag_name": "v0.9.0", "assets": [unsigned]})["xpi"] is None
    assert server.release_from_api({"tag_name": "v0.9.0", "assets": [unsigned, signed]})["xpi"] == "https://x/s.xpi"
    for nothing in ({"html_url": RELEASE["html_url"]}, {"tag_name": "  "}, None, "v0.9.0", []):
        assert server.release_from_api(nothing) is None
    assert server.release_from_api({"tag_name": "v0.9.0", "html_url": "javascript:alert(1)"})["url"] is None
    assert server.release_from_api({"tag_name": "v0.9.0", "assets": [{"name": "a.xpi", "browser_download_url": "http://x/a.xpi"}]})["xpi"] is None


class Opener:
    """urllib's open() for fetch_latest_release(): records the request, answers or raises."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return io.BytesIO(self.answer)


def http_error(code):
    return urllib.error.HTTPError(server.LATEST_RELEASE_URL, code, "x", {}, io.BytesIO(b""))


def test_the_request_is_anonymous_a_get_of_releases_latest_with_two_headers():
    opener = Opener(json.dumps(RELEASE).encode())
    assert server.fetch_latest_release(opener) == {"latest": LATEST}
    (request, timeout), = opener.requests
    assert request.full_url == "https://api.github.com/repos/Multysquid/shisu-ko/releases/latest"
    assert request.get_method() == "GET" and request.data is None
    # urllib stores header names capitalised; nothing beyond these two goes out from here.
    assert dict(request.header_items()) == {"Accept": "application/vnd.github+json", "User-agent": f"Shisu-ko/{server.VERSION}"}
    lowered = {name.lower() for name, _ in request.header_items()}
    assert not lowered & {"authorization", "cookie"}
    assert timeout == server.UPDATE_CHECK_TIMEOUT
    # The same address update.py downloads from.
    assert server.LATEST_RELEASE_URL == update.LATEST_API_URL


def test_the_default_opener_carries_no_cookie_jar():
    import urllib.request
    opener = urllib.request.build_opener()
    assert not any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in opener.handlers)


@pytest.mark.parametrize("answer, expected", [
    (http_error(404), {"latest": None}),
    (http_error(403), {"error": "GitHub's rate limit is reached"}),
    (http_error(429), {"error": "GitHub's rate limit is reached"}),
    (http_error(500), {"error": "GitHub answered HTTP 500"}),
    (urllib.error.URLError("no route"), {"error": "could not reach GitHub"}),
    (TimeoutError("timed out"), {"error": "could not reach GitHub"}),
    (b"<html>not json</html>", {"error": "GitHub returned a non-JSON response"}),
    (json.dumps({"message": "Moved Permanently"}).encode(), {"error": "GitHub's answer names no release"}),
    (b" " * (server.UPDATE_CHECK_MAX_BYTES + 1), {"error": "GitHub's answer is too large"}),
])
def test_a_failed_fetch_is_an_error_never_a_raise(answer, expected):
    assert server.fetch_latest_release(Opener(answer)) == expected


# ---- the day's check

class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Fetch:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def checker(tmp_path, fetch, clock):
    return server.UpdateChecker(path=tmp_path / "update-check.json", fetch=fetch, clock=clock)


def test_one_check_a_day_and_a_restart_within_it_asks_nothing(tmp_path):
    clock, fetch = Clock(), Fetch({"latest": LATEST})
    first = checker(tmp_path, fetch, clock)
    assert first.status() is None, "nothing to report before the first attempt"
    first.step()
    assert fetch.calls == 1
    assert first.status() == {**LATEST, "checked": int(clock.now * 1000), "error": None}
    # The rest of the day: neither this process nor a restarted one asks again.
    clock.now += DAY - 60
    first.step()
    restarted = checker(tmp_path, fetch, clock)
    assert restarted.status() == first.status()
    restarted.step()
    assert fetch.calls == 1
    # A day after: the next check.
    clock.now += 61
    restarted.step()
    assert fetch.calls == 2


def test_a_failure_keeps_the_release_seen_and_backs_off(tmp_path):
    clock = Clock()
    fetch = Fetch({"latest": LATEST}, {"error": "could not reach GitHub"})
    c = checker(tmp_path, fetch, clock)
    c.check()
    clock.now += DAY
    c.step()
    assert fetch.calls == 2
    status = c.status()
    assert status["version"] == "0.9.0" and status["error"] == "could not reach GitHub"
    assert status["checked"] == int(clock.now * 1000)
    # The waits grow with the failures in a row, and stay put at the last one.
    for wait in (*server.UPDATE_CHECK_BACKOFF, server.UPDATE_CHECK_BACKOFF[-1]):
        calls = fetch.calls
        clock.now += wait - 1
        c.step()
        assert fetch.calls == calls, wait
        clock.now += 1
        c.step()
        assert fetch.calls == calls + 1, wait
    # The record on disk keeps the release too, so a restart reports it, error and all.
    assert checker(tmp_path, fetch, clock).status() == c.status()
    # One that goes through ends the backoff: the next is a day away again.
    fetch.answers = [{"latest": {**LATEST, "version": "0.9.1"}}]
    clock.now += server.UPDATE_CHECK_BACKOFF[-1]
    c.step()
    assert c.status()["version"] == "0.9.1" and c.status()["error"] is None
    assert c.due() == clock.now + DAY


def test_no_release_published_is_an_answer_not_an_error(tmp_path):
    c = checker(tmp_path, Fetch({"latest": None}), Clock())
    c.check()
    assert c.status() == {"version": None, "url": None, "xpi": None, "published": None,
                          "checked": c.record["checked"], "error": None}
    assert c.record["failures"] == 0


def test_an_unreadable_record_or_a_clock_gone_back_means_a_check_is_due(tmp_path):
    clock = Clock()
    path = tmp_path / "update-check.json"
    for text in ("{not json", json.dumps([1]), json.dumps({"checked": "yesterday"})):
        path.write_text(text, encoding="utf-8")
        c = checker(tmp_path, Fetch({"latest": LATEST}), clock)
        assert c.status() is None and c.due() == 0.0
    path.write_text(json.dumps({"checked": int((clock.now + DAY) * 1000), "error": None, "failures": 0,
                                "latest": LATEST}), encoding="utf-8")
    assert checker(tmp_path, Fetch({"latest": LATEST}), clock).due() == 0.0


def test_the_thread_wakes_at_least_hourly():
    c = server.UpdateChecker.__new__(server.UpdateChecker)
    c.clock, c.due, c.check = Clock(), lambda: Clock()() + DAY, None
    assert c.step() == server.UPDATE_CHECK_WAKE


@pytest.mark.parametrize("flag, environ, on", [
    ("on", {}, True), ("off", {}, False), ("on", {"SHISUKO_NO_UPDATE_CHECK": "1"}, False),
    ("on", {"SHISUKO_NO_UPDATE_CHECK": "0"}, True), ("on", {"SHISUKO_NO_UPDATE_CHECK": " "}, True),
    # The Docker image keeps it: a check only reports.
    ("on", {"SHISUKO_CONTAINER": "1"}, True),
])
def test_the_off_switch(flag, environ, on):
    assert server.resolve_update_check(SimpleNamespace(update_check=flag), environ) is on


def test_the_flag_parses_and_defaults_on():
    assert server.parse_args(["--update-check", "off"]).update_check == "off"
    assert server.parse_args([]).update_check == "on"


def test_off_starts_no_thread_and_asks_nothing(monkeypatch):
    monkeypatch.delenv("SHISUKO_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "UpdateChecker", lambda: pytest.fail("no checker when off"))
    assert server.start_update_check(SimpleNamespace(update_check="off")) is None


# ---- /health

def test_health_reports_latest_or_null(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    assert app.health()["latest"] is None, "off, or a tool's App: null"
    app.update_checker = checker(tmp_path, Fetch({"latest": LATEST}), Clock())
    assert app.health()["latest"] is None, "on, before the first attempt: null"
    app.update_checker.check()
    latest = app.health()["latest"]
    assert set(latest) == {"version", "url", "xpi", "published", "checked", "error"}
    assert latest["version"] == "0.9.0" and isinstance(latest["checked"], int)
    json.dumps(app.health())  # the answer goes out as JSON
