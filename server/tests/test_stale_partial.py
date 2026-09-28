"""A stale .part file that YouTube answers with 416 must be discarded, not resumed forever."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from _serverlib import load_server

server = load_server()
VIDEO = "abcdefabcdef"
RANGE_ERROR = "ERROR: unable to download video data: HTTP Error 416: Requested range not satisfiable"


class FakeYoutubeDL:
    """Fails with 416 while asked to resume; writes the file when started from scratch."""

    attempts: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {"title": "t", "duration": 30.0, "abr": 128.0}

    def process_ie_result(self, info, download=True):
        self._download()

    def download(self, urls):
        self._download()

    def _download(self):
        FakeYoutubeDL.attempts.append(self.opts["continuedl"])
        if self.opts["continuedl"]:
            raise RuntimeError(RANGE_ERROR)
        (server.CACHE_DIR / f"{VIDEO}.webm").write_bytes(b"x" * 100)


def make_fetcher(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    FakeYoutubeDL.attempts = []
    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = FakeYoutubeDL
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)
    # language / language_patience: download_once() reads them to judge a video YouTube declares a
    # language for. This one declares none, so the resume rules below are what is being tested.
    args = SimpleNamespace(cookies_from_browser="", cookies="", allow_remote_ejs=False, js_runtime="auto",
                           first_window=20.0, window=40.0, language="ja", language_patience=60.0)
    return server.Fetcher(args)


def test_is_range_error():
    assert server.is_range_error(RuntimeError(RANGE_ERROR))
    assert not server.is_range_error(RuntimeError("ERROR: Video unavailable"))
    assert not server.is_range_error(RuntimeError("HTTP Error 403: Forbidden"))


def test_discard_partial_downloads_only_touches_yt_dlp_leftovers(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    for name in (f"{VIDEO}.webm.part", f"{VIDEO}.webm.ytdl", f"{VIDEO}.cues.json", "otherotherid.webm.part"):
        (tmp_path / name).write_bytes(b"x")
    assert server.discard_partial_downloads(VIDEO) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([f"{VIDEO}.cues.json", "otherotherid.webm.part"])


def test_416_on_resume_discards_the_part_and_downloads_from_scratch(monkeypatch, tmp_path):
    fetcher = make_fetcher(monkeypatch, tmp_path)
    (tmp_path / f"{VIDEO}.webm.part").write_bytes(b"stale")
    s = server.Session(video_id=VIDEO, url="u")
    path = fetcher.download(s)
    assert path == tmp_path / f"{VIDEO}.webm"
    assert not (tmp_path / f"{VIDEO}.webm.part").exists()
    # process_ie_result and the plain-download fallback both resumed and failed; the restart did not resume
    assert FakeYoutubeDL.attempts == [True, True, False]


def test_other_errors_are_not_retried_from_scratch(monkeypatch, tmp_path):
    fetcher = make_fetcher(monkeypatch, tmp_path)

    def fail(self):
        FakeYoutubeDL.attempts.append(self.opts["continuedl"])
        raise RuntimeError("ERROR: Video unavailable")

    monkeypatch.setattr(FakeYoutubeDL, "_download", fail)
    s = server.Session(video_id=VIDEO, url="u")
    try:
        fetcher.download(s)
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("expected the download to fail")
    assert FakeYoutubeDL.attempts == [True, True]
