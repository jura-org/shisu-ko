# SPDX-License-Identifier: Apache-2.0
"""The viewer's YouTube watch history (--watch-history): the capture at resolve, the page's
reports, the pings built from YouTube's own tracking URLs. Nothing here reaches the network."""

import http.client
import importlib.util
import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "portal.py"
spec = importlib.util.spec_from_file_location("portal", PATH)
portal = importlib.util.module_from_spec(spec)
sys.modules["portal"] = portal  # `from __future__ import annotations` needs the module registered
spec.loader.exec_module(portal)

VIDEO = "AbCdEfGhIjK"
PLAYBACK = "https://s.youtube.com/api/stats/playback?cl=1&docid=X&ei=E&el=detailpage&len=600&ns=yt&plid=P"
WATCHTIME = "https://s.youtube.com/api/stats/watchtime?cl=1&docid=X&ei=E&el=detailpage&len=600&ns=yt&plid=P"
TRACKING = {"playback": PLAYBACK, "watchtime": WATCHTIME}
CLIENT = {"c": "WEB", "cver": "2.20260101.00.00", "cr": "JP"}
CADENCE = {"schedule": (10.0, 20.0, 30.0), "interval": 40.0}
# What the capture keeps: the URLs moved to www.youtube.com, the client, the flush schedule.
CAPTURED = {**{k: v.replace("//s.", "//www.") for k, v in TRACKING.items()}, "client": CLIENT, "cadence": CADENCE}
RESPONSES = [
    {"streamingData": {}},  # a client's response without tracking
    {"playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": PLAYBACK},
                          "videostatsWatchtimeUrl": {"baseUrl": WATCHTIME},
                          "videostatsScheduledFlushWalltimeSeconds": [10, 20, 30],
                          "videostatsDefaultFlushIntervalSeconds": 40},
     "responseContext": {"serviceTrackingParams": [
         {"service": "GFEEDBACK", "params": [{"key": "ipcc", "value": "JP"}, {"key": "logged_in", "value": "1"}]},
         {"service": "CSI", "params": [{"key": "c", "value": "WEB"}, {"key": "cver", "value": "2.20260101.00.00"},
                                       {"key": "yt_li", "value": "1"}]},
         {"service": "ECATCHER", "params": [{"key": "client.name", "value": "WEB"}]}]}},
    {"playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": PLAYBACK + "&second=1"},
                          "videostatsWatchtimeUrl": {"baseUrl": WATCHTIME + "&second=1"}}},
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home folder of its own: the real ~/.shisu-ko is never read."""
    monkeypatch.setattr(portal, "SHISUKO_HOME", tmp_path)
    monkeypatch.delenv("SHISUKO_CONTAINER", raising=False)
    return tmp_path


def with_cookies(home, browser="firefox"):
    (home / "config.json").write_text(json.dumps({"cookies_from_browser": browser}), encoding="utf-8")


def query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def report(ranges, cmt=None, state="playing", duration=600.0):
    return portal.parse_watch_report({"video_id": VIDEO, "ranges": ranges, "duration": duration, "state": state,
                                      "cmt": ranges[-1][1] if cmt is None and ranges else cmt or 0})


# ---------------------------------------------------------------- the switch


def test_off_by_default_and_off_sends_nothing(home):
    with_cookies(home)
    assert re.search(r'"--watch-history", choices=\("on", "off"\), default="off"', PATH.read_text(encoding="utf-8"))
    assert portal.watch_history_state("off") == ("off", None)
    services = portal.Services(SimpleNamespace(mode="local", config=home / "portal.json"))
    assert services.watch.mode == "off" and not services.hls.capture_tracking
    reporter = portal.WatchReporter("off", send=lambda url: pytest.fail("sent while off"))
    assert reporter.report(report([[0, 30]]), TRACKING) is False
    assert reporter.due(time.monotonic() + 100) == [] and reporter.sessions == {}


def test_no_cookies_or_the_container_leave_it_inactive_with_a_reason(home, monkeypatch):
    state, note = portal.watch_history_state("on")
    assert state == "no-cookies" and "--save-cookies-from-browser" in note and "Firefox" in note
    with_cookies(home, "none")
    assert portal.watch_history_state("on")[0] == "no-cookies"
    with_cookies(home)
    assert portal.watch_history_state("on")[0] == "on"
    monkeypatch.setenv("SHISUKO_CONTAINER", "1")
    state, note = portal.watch_history_state("on")
    assert state == "no-cookies" and "container" in note
    reporter = portal.WatchReporter(state, send=lambda url: pytest.fail("sent without cookies"))
    assert reporter.report(report([[0, 30]]), TRACKING) is False


def test_info_tells_the_page(home, monkeypatch):
    with_cookies(home)
    monkeypatch.setattr(portal.Services, "language", lambda self: "ja")
    on = portal.Services(SimpleNamespace(mode="local", config=home / "portal.json", watch_history="on"))
    on.state = portal.PortalState(home / "state.json")
    assert on.info()["watch_history"] == "on" and on.hls.capture_tracking
    off = portal.Services(SimpleNamespace(mode="local", config=home / "portal.json"))
    off.state = portal.PortalState(home / "state.json")
    assert off.info()["watch_history"] == "off"


# ---------------------------------------------------------------- the capture at resolve


class FakeExtractor:
    def mark_watched(self, video_id, player_responses):
        # yt-dlp's own: without its mark_watched option it sends nothing; this one notes it ran.
        self.own_ran = True


ORIGINAL_MARK_WATCHED = FakeExtractor.mark_watched


class FakeYoutubeDL:
    made = []

    def __init__(self, options):
        assert "mark_watched" not in options
        self.options = options
        self.extractor = FakeExtractor()
        FakeYoutubeDL.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_info_extractor(self, key):
        assert key == "Youtube"
        return self.extractor

    anonymous_hls = True
    responses = RESPONSES  # the player responses yt-dlp hands mark_watched
    gate = None  # a threading.Event a signed-in extraction waits for
    signed_in_error = None

    def extract_info(self, url, download, **kwargs):
        self.kwargs = kwargs
        signed_in = "cookiesfrombrowser" in self.options
        if signed_in and FakeYoutubeDL.gate is not None:
            assert FakeYoutubeDL.gate.wait(5)
        if signed_in and FakeYoutubeDL.signed_in_error:
            raise FakeYoutubeDL.signed_in_error
        # Where yt-dlp's YouTube extractor calls self.mark_watched(video_id, player_responses).
        self.extractor.mark_watched(VIDEO, FakeYoutubeDL.responses)
        if "cookiesfrombrowser" not in self.options and not FakeYoutubeDL.anonymous_hls:
            return {"duration": 600, "title": "t", "formats": []}
        return {"duration": 600, "title": "t", "formats": [{"protocol": "m3u8_native", "manifest_url": "https://example.invalid/m"}]}


@pytest.fixture
def fake_ytdlp(monkeypatch):
    FakeYoutubeDL.anonymous_hls = True
    FakeYoutubeDL.responses = RESPONSES
    FakeYoutubeDL.made = []
    FakeYoutubeDL.gate = None
    FakeYoutubeDL.signed_in_error = None
    monkeypatch.setattr(portal, "import_ytdlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(portal, "http_open", lambda url, headers=None, timeout=20.0: SimpleNamespace(read=lambda: b"#EXTM3U"))
    monkeypatch.setattr(portal, "rewrite_master", lambda text, prefix: ("#EXTM3U", {"v1": "u"}, {"v1": 720}))


def test_resolve_keeps_the_first_player_responses_tracking_urls_and_sends_nothing(home, fake_ytdlp):
    with_cookies(home)
    FakeYoutubeDL.anonymous_hls = False  # signed in: the tracking URLs are the account's
    video = portal.HlsVideo(VIDEO)
    portal.HlsProxy(capture_tracking=True).resolve(video)
    assert video.tracking == CAPTURED
    # The override lives on that YoutubeDL's own extractor, never on the class.
    assert "mark_watched" in vars(FakeYoutubeDL.made[-1].extractor)
    assert FakeExtractor.mark_watched is ORIGINAL_MARK_WATCHED


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_an_anonymous_resolve_looks_the_tracking_up_signed_in_without_waiting_for_it(home, fake_ytdlp):
    with_cookies(home)
    FakeYoutubeDL.gate = threading.Event()
    found = []
    hls = portal.HlsProxy(capture_tracking=True, on_tracking=lambda vid, tracking: found.append((vid, tracking)))
    video = portal.HlsVideo(VIDEO)
    hls.resolve(video)  # returns while the signed-in look-up still waits
    assert video.master and video.tracking is None and video.tracking_busy
    wait_for(lambda: len(FakeYoutubeDL.made) == 2)
    anonymous, signed_in = FakeYoutubeDL.made
    assert "cookiesfrombrowser" not in anonymous.options and "mark_watched" not in vars(anonymous.extractor)
    assert signed_in.options["cookiesfrombrowser"] == ("firefox",)
    hls.start_tracking(video)  # one look-up at a time
    assert len(FakeYoutubeDL.made) == 2
    FakeYoutubeDL.gate.set()
    wait_for(lambda: not video.tracking_busy)
    assert video.tracking == CAPTURED and found == [(VIDEO, CAPTURED)]
    assert signed_in.kwargs == {"process": False}
    hls.resolve(video)  # a re-resolve (a 403) keeps them and looks nothing up again
    assert len(FakeYoutubeDL.made) == 3


def test_a_signed_in_resolve_without_tracking_is_not_extracted_again(home, fake_ytdlp, caplog):
    with_cookies(home)
    FakeYoutubeDL.anonymous_hls = False  # the resolve itself goes signed in
    FakeYoutubeDL.responses = RESPONSES[:1]  # and its answer carries no tracking URLs
    hls = portal.HlsProxy(capture_tracking=True)
    video = portal.HlsVideo(VIDEO)
    with caplog.at_level("WARNING", logger="portal"):
        for _ in range(2):
            hls.resolve(video)
    # Two passes per resolve, anonymous then signed in; no third, identical signed-in look-up.
    assert len(FakeYoutubeDL.made) == 4 and not video.tracking_busy and video.tracking is None
    assert len([r for r in caplog.records if "carried no tracking URLs" in r.getMessage()]) == 1


def test_a_failed_look_up_leaves_no_tracking_and_is_logged_once(home, fake_ytdlp, caplog):
    with_cookies(home)
    FakeYoutubeDL.signed_in_error = RuntimeError("bot check")
    hls = portal.HlsProxy(capture_tracking=True)
    video = portal.HlsVideo(VIDEO)
    with caplog.at_level("WARNING", logger="portal"):
        for _ in range(2):
            hls.resolve(video)
            wait_for(lambda: not video.tracking_busy)
    assert video.tracking is None
    assert len([r for r in caplog.records if "signed-in look-up failed" in r.getMessage()]) == 1


def test_a_failed_look_up_is_tried_again_on_reports_with_backoff_and_its_stretches_go(home, fake_ytdlp, caplog):
    with_cookies(home)
    now = [0.0]
    services = portal.Services(SimpleNamespace(mode="local", config=home / "portal.json", watch_history="on"))
    services.watch.background = False
    services.hls.clock = lambda: now[0]
    FakeYoutubeDL.signed_in_error = RuntimeError("bot check")

    def look_ups():
        return sum("cookiesfrombrowser" in y.options for y in FakeYoutubeDL.made)

    def watched(at, ranges):
        now[0] = at
        held = services.watched(report(ranges))
        wait_for(lambda: not video.tracking_busy)
        return held

    with caplog.at_level("WARNING", logger="portal"):
        video = services.hls.get(VIDEO)  # resolves anonymously; the look-up fails on its thread
        wait_for(lambda: not video.tracking_busy)
        assert video.tracking is None and video.tracking_retry_at == 30.0 and look_ups() == 1
        # Within the backoff: no look-up, and the stretches are held rather than lost.
        assert watched(10.0, [[0, 12]]) is True and look_ups() == 1
        assert watched(31.0, [[12, 20]]) is True and look_ups() == 2  # due: tried again, failed again
        assert video.tracking_retry_at == 31.0 + 120.0
        assert watched(100.0, [[20, 25]]) is True and look_ups() == 2
        assert services.watch.due(time.monotonic()) == []  # nothing goes without the URLs
        FakeYoutubeDL.signed_in_error = None  # YouTube answers now
        assert watched(151.0, [[25, 30]]) is True and look_ups() == 3
    assert video.tracking == CAPTURED and video.tracking_retry_at is None
    playback, watchtime = services.watch.due(time.monotonic())
    assert query(playback[1])["cmt"] == "0.000"
    assert (query(watchtime[1])["st"], query(watchtime[1])["et"]) == ("0.000", "30.000")
    assert len([r for r in caplog.records if "signed-in look-up failed" in r.getMessage()]) == 1
    # Found: no more look-ups.
    watched(2000.0, [[30, 40]])
    assert look_ups() == 3


def test_the_backoff_grows_to_ten_minutes(home, fake_ytdlp):
    with_cookies(home)
    now = [0.0]
    hls = portal.HlsProxy(capture_tracking=True, clock=lambda: now[0])
    video = portal.HlsVideo(VIDEO)
    FakeYoutubeDL.signed_in_error = RuntimeError("bot check")
    waits = []
    for _ in range(5):
        video.tracking_busy = True
        hls.fetch_tracking(video)
        waits.append(video.tracking_retry_at - now[0])
        now[0] = video.tracking_retry_at
    assert waits == [30.0, 120.0, 600.0, 600.0, 600.0] and not video.tracking_busy


def test_reports_wait_for_the_tracking_urls_and_go_when_they_come():
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    assert reporter.report(report([[0, 12]]), None, waiting=True)
    assert reporter.report(report([[12, 20]]), None, waiting=True)
    assert reporter.due(0.0) == [] and reporter.wait_time(0.0) is None
    reporter.set_tracking(VIDEO, TRACKING)
    playback, watchtime = reporter.due(0.0)
    assert query(playback[1])["cmt"] == "0.000" and query(watchtime[1])["et"] == "20.000"
    assert reporter.report(report([[20, 30]]), None) is True  # the session keeps its URLs
    assert reporter.report({**report([[0, 5]]), "video_id": "ZyXwVuTsRqP"}, None) is False


class RacingVideo(portal.HlsVideo):
    """A video whose first read of `tracking` lets the signed-in look-up finish (`finish()`), so
    the look-up lands exactly between Services.watched()'s reads of tracking and tracking_busy."""

    finish = None

    @property
    def tracking(self):
        value = self.__dict__.get("tracking")  # read before the look-up lands
        finish, RacingVideo.finish = RacingVideo.finish, None
        if finish:
            finish()
        return value

    @tracking.setter
    def tracking(self, value):
        self.__dict__["tracking"] = value


def test_a_report_racing_the_look_up_keeps_its_stretches(home, fake_ytdlp):
    with_cookies(home)
    services = portal.Services(SimpleNamespace(mode="local", config=home / "portal.json", watch_history="on"))
    services.watch.background = False
    video = RacingVideo(VIDEO)
    video.tracking_busy = True  # start_tracking()'s claim
    services.hls.videos[VIDEO] = video
    FakeYoutubeDL.gate = threading.Event()
    done = threading.Event()
    look_up = threading.Thread(target=lambda: (services.hls.fetch_tracking(video), done.set()), daemon=True)
    look_up.start()

    def finish():
        # The look-up's answer arrives now. It hands its URLs over under the lock the report holds,
        # so it may not finish until the report is in; without that lock it finishes here.
        FakeYoutubeDL.gate.set()
        done.wait(0.5)

    RacingVideo.finish = finish
    assert services.watched(report([[0, 12]])) is True
    assert done.wait(5)
    assert video.tracking == CAPTURED and not video.tracking_busy
    playback, watchtime = services.watch.due(time.monotonic())
    assert query(playback[1])["cmt"] == "0.000" and query(watchtime[1])["st"] == "0.000"


def test_resolve_without_the_switch_captures_nothing(home, fake_ytdlp):
    with_cookies(home)
    FakeYoutubeDL.anonymous_hls = False  # resolved signed in, yet the switch is off
    video = portal.HlsVideo(VIDEO)
    portal.HlsProxy().resolve(video)
    assert video.tracking is None and len(FakeYoutubeDL.made) == 2
    assert all(ydl.extractor.own_ran for ydl in FakeYoutubeDL.made)  # yt-dlp's own, untouched


def test_tracking_urls_go_to_www_youtube_com_only_from_s_youtube_com():
    assert portal.stats_host(PLAYBACK) == PLAYBACK.replace("https://s.youtube.com/", "https://www.youtube.com/")
    for other in ("https://www.youtube.com/api/stats/playback?x=1", "https://m.youtube.com/api/stats/playback",
                  "https://s.youtube.com.evil.example/api/stats/playback", "https://xs.youtube.com/api/stats/playback"):
        assert portal.stats_host(other) == other
    captured = portal.tracking_urls(RESPONSES)
    assert all(urllib.parse.urlsplit(captured[k]).hostname == "www.youtube.com" for k in ("playback", "watchtime"))
    assert query(captured["watchtime"]) == query(WATCHTIME)  # only the host moves


def test_flush_cadence_reads_the_response_or_keeps_the_web_players():
    assert portal.flush_cadence({"videostatsScheduledFlushWalltimeSeconds": [5, 15, 30.5],
                                 "videostatsDefaultFlushIntervalSeconds": 60}) == {"schedule": (5.0, 15.0, 30.5),
                                                                                  "interval": 60.0}
    assert portal.flush_cadence({}) == CADENCE
    for schedule in ([], [10, 5], [10, 11], [0, 10], [10, 20, 9999], ["10"], [True], list(range(10, 230, 20)), "10,20"):
        assert portal.flush_cadence({"videostatsScheduledFlushWalltimeSeconds": schedule})["schedule"] == CADENCE["schedule"]
    for interval in (0, 1, -40, 9999, "40", None, float("nan")):
        assert portal.flush_cadence({"videostatsDefaultFlushIntervalSeconds": interval})["interval"] == 40.0


def test_tracking_urls_takes_youtube_https_urls_only():
    assert portal.tracking_urls(RESPONSES) == CAPTURED
    assert portal.tracking_urls([]) is None and portal.tracking_urls(None) is None
    bad = {"playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": "https://evil.example/api/stats/playback"},
                                "videostatsWatchtimeUrl": {"baseUrl": WATCHTIME}}}
    assert portal.tracking_urls([bad]) is None
    http = {"playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": PLAYBACK.replace("https", "http")},
                                 "videostatsWatchtimeUrl": {"baseUrl": WATCHTIME}}}
    assert portal.tracking_urls([http]) is None
    assert portal.tracking_urls([{"playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": PLAYBACK}}}]) is None


# ---------------------------------------------------------------- the pings


def test_first_report_sends_the_playback_ping_then_watchtime_with_lists():
    clock = [1000.0]
    reporter = portal.WatchReporter("on", send=None, clock=lambda: clock[0])
    reporter.background = False
    assert reporter.report(report([[12, 40.5], [100, 130.25]], cmt=130.25), TRACKING)
    clock[0] += 3
    pings = reporter.due(clock[0])
    assert [urllib.parse.urlsplit(u).path for _, u in pings] == ["/api/stats/playback", "/api/stats/watchtime"]
    playback, watchtime = (query(u) for _, u in pings)
    assert playback["cpn"] == watchtime["cpn"]
    assert len(playback["cpn"]) == 16 and set(playback["cpn"]) <= set(portal.CPN_ALPHABET)
    assert playback["ver"] == "2" and playback["el"] == "detailpage" and playback["cmt"] == "12.000"
    assert "st" not in playback and "et" not in playback
    assert watchtime["st"] == "12.000,100.000" and watchtime["et"] == "40.500,130.250"
    assert watchtime["cmt"] == "130.250" and watchtime["state"] == "playing" and watchtime["rt"] == "3.000"
    assert watchtime["len"] == "600"  # YouTube's own len stays
    assert watchtime["docid"] == "X" and watchtime["plid"] == "P"  # the base URL's own parameters stay
    assert ",".join(["12.000", "100.000"]) in pings[1][1]  # commas as YouTube's player sends them
    # Later reports: watch time only, same cpn.
    reporter.report(report([[130.25, 160]], state="paused"), TRACKING)
    clock[0] += 30
    (only,) = reporter.due(clock[0])
    assert urllib.parse.urlsplit(only[1]).path == "/api/stats/watchtime"
    assert query(only[1])["cpn"] == playback["cpn"] and query(only[1])["state"] == "paused"


def test_len_comes_from_the_report_when_youtube_gave_none():
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    bare = {k: v.replace("&len=600", "") for k, v in TRACKING.items()}
    reporter.report(report([[0, 20]], duration=321.5), bare)
    assert query(reporter.due(0.0)[1][1])["len"] == "321.500"


def test_one_watchtime_ping_per_ten_seconds_merges_the_stretches():
    clock = [0.0]
    reporter = portal.WatchReporter("on", send=None, clock=lambda: clock[0])
    reporter.background = False
    reporter.report(report([[0, 10]]), TRACKING)
    assert len(reporter.due(0.0)) == 2
    clock[0] = 2.0
    reporter.report(report([[10, 12]]), TRACKING)
    clock[0] = 4.0
    reporter.report(report([[12.2, 14], [50, 55]]), TRACKING)
    assert reporter.due(4.0) == [] and reporter.wait_time(4.0) == pytest.approx(6.0)
    (ping,) = reporter.due(10.0)
    assert query(ping[1])["st"] == "10.000,50.000" and query(ping[1])["et"] == "14.000,55.000"
    assert reporter.due(30.0) == [] and reporter.wait_time(30.0) is None  # nothing new: nothing sent


def test_the_schedule_sets_the_gap_and_the_next_report():
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    assert reporter.next_report(VIDEO) is None
    reporter.report(report([[0, 10]]), CAPTURED)
    session = reporter.sessions[VIDEO]
    assert [session.flush_at(n) for n in range(1, 6)] == [10, 20, 30, 70, 110]
    assert session.gap() == portal.WATCH_PING_GAP and reporter.next_report(VIDEO) == 10.0
    # A schedule with closer steps than WATCH_PING_GAP: the server never holds it back.
    session.tracking = {**CAPTURED, "cadence": {"schedule": (4.0, 8.0), "interval": 30.0}}
    assert session.gap() == 4.0
    nexts = []
    for flushes in (0, 1, 2, 3):
        session.flushes = flushes
        nexts.append(session.next_report())
    assert nexts == [4.0, 4.0, 30.0, 30.0]
    # A capture from before the schedule was kept: the web player's.
    session.tracking = TRACKING
    assert session.cadence() == {"schedule": portal.WATCH_FLUSH_SCHEDULE, "interval": portal.WATCH_FLUSH_INTERVAL}


def test_a_final_report_goes_within_two_seconds_with_final_1():
    clock = [0.0]
    reporter = portal.WatchReporter("on", send=None, clock=lambda: clock[0])
    reporter.background = False
    reporter.report(report([[0, 10]]), CAPTURED)
    first = reporter.due(0.0)
    assert len(first) == 2 and "final" not in query(first[1][1])
    # A pause 1 s later: not before WATCH_FINAL_GAP, then at once, not after the 10 s gap.
    clock[0] = 1.0
    reporter.report(report([[10, 11]], state="paused") | {"final": True}, CAPTURED)
    assert reporter.due(1.0) == [] and reporter.wait_time(1.0) == pytest.approx(1.0)
    (ping,) = reporter.due(2.0)
    got = query(ping[1])
    assert got["final"] == "1" and got["state"] == "paused" and got["st"] == "10.000" and got["et"] == "11.000"
    assert portal.ping_summary(ping[1]).endswith("state=paused final=1")
    # Another final at once (the page hidden right after): held to the 2 s limit.
    clock[0] = 2.5
    reporter.report(report([[11, 11.5]], state="paused") | {"final": True}, CAPTURED)
    assert reporter.due(2.5) == [] and reporter.wait_time(2.5) == pytest.approx(1.5)
    assert query(reporter.due(4.0)[0][1])["final"] == "1"
    # A later play goes on in the same session, scheduled again, without final.
    clock[0] = 20.0
    reporter.report(report([[11.5, 21.5]]), CAPTURED)
    (later,) = reporter.due(20.0)
    assert "final" not in query(later[1]) and query(later[1])["cpn"] == got["cpn"]
    clock[0] = 25.0
    reporter.report(report([[21.5, 26.5]]), CAPTURED)
    assert reporter.due(25.0) == []  # a scheduled report keeps the 10 s gap


def test_a_pause_reports_paused_on_the_schedule_without_final():
    # A pause is no end of the viewing: its ping keeps the 10 s gap and carries no final=1.
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    reporter.report(report([[0, 10]]), CAPTURED)
    reporter.due(0.0)
    reporter.report(report([[10, 11]], state="paused"), CAPTURED)
    assert reporter.due(5.0) == []
    (ping,) = reporter.due(10.0)
    got = query(ping[1])
    assert got["state"] == "paused" and "final" not in got and got["st"] == "10.000"


def test_a_final_report_then_more_play_before_its_ping_drops_final():
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    reporter.report(report([[0, 10]]), CAPTURED)
    reporter.due(0.0)
    reporter.report(report([[10, 11]], state="paused") | {"final": True}, CAPTURED)
    reporter.report(report([[11, 15]]), CAPTURED)  # the last report decides
    (ping,) = reporter.due(10.0)
    assert "final" not in query(ping[1]) and query(ping[1])["st"] == "10.000"


def test_pings_carry_the_authuser_header_through_yt_dlp(monkeypatch):
    opened = []

    class Response:
        status = 204

        def read(self):
            return b""

        def close(self):
            pass

    class FakeYDL:
        def __init__(self, opts):
            pass

        def urlopen(self, request):
            opened.append(request)
            return Response()

    class Request:
        def __init__(self, url, headers=None):
            self.url, self.headers = url, headers

    fake = SimpleNamespace(YoutubeDL=FakeYDL, networking=SimpleNamespace(Request=Request))
    monkeypatch.setattr(portal, "import_ytdlp", lambda: fake)
    monkeypatch.setattr(portal, "ydl_options", lambda *a, **k: {})
    reporter = portal.WatchReporter("on")
    assert reporter.youtube_get(CAPTURED["watchtime"]) == 204
    (request,) = opened
    assert request.url == CAPTURED["watchtime"] and request.headers == {"X-Goog-AuthUser": "0"}


def test_yt_dlps_own_request_carries_the_header():
    networking = pytest.importorskip("yt_dlp.networking")
    request = networking.Request(CAPTURED["watchtime"], headers=portal.WATCH_HEADERS)
    assert request.headers["x-goog-authuser"] == "0" and request.url == CAPTURED["watchtime"]


def test_a_report_with_nothing_played_sends_nothing():
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    reporter.report(report([[5, 5]], cmt=5), TRACKING)
    assert reporter.due(0.0) == []


def test_a_video_without_tracking_urls_is_skipped():
    reporter = portal.WatchReporter("on", send=lambda url: pytest.fail("sent"))
    assert reporter.report(report([[0, 30]]), None) is False
    assert reporter.sessions == {}


def test_idle_sessions_go_and_a_new_one_gets_a_new_cpn():
    clock = [0.0]
    reporter = portal.WatchReporter("on", send=None, clock=lambda: clock[0])
    reporter.background = False
    reporter.report(report([[0, 30]]), TRACKING)
    first = query(reporter.due(0.0)[0][1])["cpn"]
    clock[0] = portal.WATCH_IDLE_DROP + 60
    reporter.report(report([[30, 60]]), TRACKING)
    pings = reporter.due(clock[0])
    assert len(pings) == 2 and query(pings[0][1])["cpn"] != first  # a new session announces itself again


def test_the_thread_sends_and_a_failure_is_logged_once(caplog):
    sent, done = [], threading.Event()

    def send(url):
        sent.append(url)
        if len(sent) >= 2:
            done.set()
        raise OSError("refused")

    reporter = portal.WatchReporter("on", send=send)
    reporter.background = True
    with caplog.at_level("WARNING", logger="portal"):
        reporter.report(report([[0, 30]]), TRACKING)
        assert done.wait(5)
    assert len(sent) == 2
    assert len([r for r in caplog.records if "watch history" in r.getMessage()]) == 1


def test_merge_ranges_keeps_the_order_played():
    assert portal.merge_ranges([(0, 10), (10.3, 20), (5, 8), (8, 8), (100, 90)]) == [(0, 20), (5, 8)]


# ---------------------------------------------------------------- what the page may send


@pytest.mark.parametrize("body", [
    {"video_id": "bad id!", "ranges": [], "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": 0, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": float("nan"), "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": True, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": 600, "cmt": float("inf"), "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": 600, "cmt": 606, "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": 600, "cmt": 0, "state": "buffering"},
    {"video_id": VIDEO, "ranges": [[0, 1]] * 21, "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [[10, 5]], "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [[-1, 5]], "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [[0, 606]], "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [[0, "5"]], "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [[0, 1, 2]], "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": "0-5", "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": None, "duration": 600, "cmt": 0, "state": "playing"},
    {"video_id": VIDEO, "ranges": [], "duration": 600, "cmt": 0, "state": "paused", "final": "yes"},
    {"video_id": VIDEO, "ranges": [], "duration": 600, "cmt": 0, "state": "paused", "final": 1},
])
def test_bad_reports_are_refused(body):
    with pytest.raises(ValueError):
        portal.parse_watch_report(body)


def test_a_good_report():
    got = portal.parse_watch_report({"video_id": VIDEO, "ranges": [[0, 604.5]], "duration": 600, "cmt": 604, "state": "paused",
                                     "url": "https://evil.example/"})
    assert got == {"video_id": VIDEO, "ranges": [(0.0, 604.5)], "cmt": 604.0, "duration": 600.0, "state": "paused",
                   "final": False}


# ---------------------------------------------------------------- /api/watched over HTTP


@pytest.fixture
def server(home, monkeypatch):
    with_cookies(home)
    services = portal.Services(SimpleNamespace(mode="lan", config=home / "portal.json", watch_history="on"))
    services.state = portal.PortalState(home / "state.json")
    sent = []
    services.watch.send = sent.append
    services.watch.background = False
    video = portal.HlsVideo(VIDEO)
    video.tracking = TRACKING
    services.hls.videos[VIDEO] = video
    monkeypatch.setattr(portal.Handler, "services", services)
    monkeypatch.setattr(portal.Handler, "allowed_hosts", {"127.0.0.1", "localhost"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), portal.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=httpd.server_address[1], services=services)
    httpd.shutdown()
    httpd.server_close()


def post(port, body, headers=None, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode()
    conn.request("POST", "/api/watched", data, {"Host": host, "Content-Type": "application/json", **(headers or {})})
    res = conn.getresponse()
    answer = (res.status, json.loads(res.read() or b"{}"))
    conn.close()
    return answer


GOOD = {"video_id": VIDEO, "ranges": [[0, 30]], "duration": 600, "cmt": 30, "state": "playing"}


def test_api_watched_holds_a_good_report(server):
    # The answer names when the page reports next: YouTube's schedule, 10, 20, 30 s, then every 40 s.
    assert post(server.port, GOOD) == (200, {"ok": True, "reported": True, "next": 10.0})
    assert server.services.watch.sessions[VIDEO].pending == [(0.0, 30.0)]
    assert [post(server.port, GOOD)[1]["next"] for _ in range(3)] == [10.0, 40.0, 40.0]
    # A final report does not move the schedule.
    assert post(server.port, {**GOOD, "state": "paused", "final": True})[1]["next"] == 40.0
    # A video that never streamed through the portal: accepted, nothing held.
    assert post(server.port, {**GOOD, "video_id": "ZyXwVuTsRqP"}) == (200, {"ok": True, "reported": False})


def test_api_watched_refuses_bad_bodies_hosts_origins_and_types(server):
    assert post(server.port, {**GOOD, "ranges": [[0, 9999]]})[0] == 400
    assert post(server.port, GOOD, host="evil.example")[0] == 403
    assert post(server.port, GOOD, {"Origin": "https://evil.example"})[0] == 403
    assert post(server.port, GOOD, {"Content-Type": "text/plain"})[0] == 415
    assert server.services.watch.sessions == {}


def test_api_watched_needs_a_paired_device_off_loopback(server, monkeypatch):
    monkeypatch.setattr(portal, "ip_allowed", lambda addr, owners: False)  # as if from another device
    status, answer = post(server.port, GOOD)
    assert status == 401 and answer.get("pair")
    assert server.services.watch.sessions == {}


def test_ping_summary_names_the_ping_and_its_times_only():
    url = ("https://s.youtube.com/api/stats/watchtime?ns=yt&docid=x&cpn=abc&ei=e&st=0.000,30.000"
           "&et=10.000,45.000&cmt=45.000&len=228&state=paused")
    assert portal.ping_summary(url) == "watchtime st=0.000,30.000 et=10.000,45.000 cmt=45.000 len=228 state=paused"


# ---------------------------------------------------------------- the client context, as YouTube's web player sends it


PHONE = {"browser": "Safari", "browser_version": "17.5", "os": "iPhone", "os_version": "17_5"}


def test_response_client_takes_only_what_the_response_holds():
    assert portal.response_client(RESPONSES[1]) == CLIENT
    assert portal.response_client({}) == {}
    odd = {"responseContext": {"serviceTrackingParams": [
        {"service": "CSI", "params": [{"key": "c", "value": "WEB&x=1"}, {"key": "cver", "value": 2}]},
        {"service": "OTHER", "params": [{"key": "ipcc", "value": "JP"}]}, "junk"]}}
    assert portal.response_client(odd) == {}  # unsafe, not a string, another service's key


def full_report(**extra):
    body = {"video_id": VIDEO, "ranges": [[0, 10], [50, 60]], "duration": 600, "cmt": 60, "state": "playing",
            "lact": 1234, "volume": 80, "muted": False, "client": PHONE, **extra}
    return portal.parse_watch_report(body)


def test_pings_carry_the_web_players_client_context():
    clock = [100.0]
    reporter = portal.WatchReporter("on", send=None, clock=lambda: clock[0])
    reporter.background = False
    got = full_report()
    got.update(fmt="136", afmt="140")
    reporter.report(got, CAPTURED)
    clock[0] = 112.6
    (_, playback), (_, watchtime) = reporter.due(clock[0])
    context = {"c": "WEB", "cver": "2.20260101.00.00", "cr": "JP", "cplayer": "UNIPLAYER", "cbr": "Safari",
               "cbrver": "17.5", "cos": "iPhone", "cosver": "17_5", "fs": "0", "rt": "12.600", "fmt": "136",
               "afmt": "140", "lact": "1234"}
    for url in (playback, watchtime):
        q = query(url)
        assert {k: q.get(k) for k in context} == context
        assert "euri=" in url  # present and empty
    q = query(watchtime)
    assert q["st"] == "0.000,50.000" and q["et"] == "10.000,60.000"
    assert (q["volume"], q["muted"], q["vis"]) == ("80,80", "0,0", "0,0")  # one per stretch
    assert (q["rti"], q["rtn"], q["idpj"], q["ldpj"]) == ("12", "22", "-1", "-1")
    assert q["ver"] == "2" and q["el"] == "detailpage" and q["state"] == "playing"


def test_what_nobody_said_is_left_out():
    reporter = portal.WatchReporter("on", send=None, clock=lambda: 0.0)
    reporter.background = False
    reporter.report(report([[0, 20]]), TRACKING)  # an old capture without the client, a bare page
    (_, playback), (_, watchtime) = reporter.due(0.0)
    for url in (playback, watchtime):
        q = query(url)
        for absent in ("c", "cver", "cr", "cbr", "cbrver", "cos", "cosver", "lact", "fmt", "afmt", "hl"):
            assert absent not in q, absent
        assert q["cplayer"] == "UNIPLAYER" and q["fs"] == "0"
    assert "volume" not in query(watchtime) and "muted" not in query(watchtime)


@pytest.mark.parametrize("extra", [
    {"lact": -1}, {"lact": float("nan")}, {"lact": "5"}, {"volume": 101}, {"volume": True}, {"muted": "no"},
    {"muted": 0}, {"client": "Safari"}, {"client": {"agent": "x"}}, {"client": {"os": "iPhone;rm"}},
    {"client": {"browser": "x" * 33}}, {"client": {"browser": ""}}, {"client": {"os": 17}},
])
def test_bad_page_context_is_refused(extra):
    with pytest.raises(ValueError):
        full_report(**extra)


def test_the_page_context_is_optional():
    got = portal.parse_watch_report({"video_id": VIDEO, "ranges": [], "duration": 600, "cmt": 0, "state": "paused"})
    assert set(got) == {"video_id", "ranges", "cmt", "duration", "state", "final"} and got["final"] is False
    assert full_report(client={"os": "X11"})["client"] == {"cos": "X11"}


def test_fmt_and_afmt_are_the_variants_the_player_takes_segments_from(server, monkeypatch):
    video = server.services.hls.videos[VIDEO]
    video.master = "#EXTM3U"
    video.upstream = {"v136": "https://example.invalid/itag/136/x", "v134": "https://example.invalid/itag/134/x",
                      "a140": "https://example.invalid/itag/140/x"}
    server.services.hls.by_token[video.token] = video
    monkeypatch.setattr(server.services.hls, "segment", lambda video, key, index: b"segment")
    for path in ("v136/0.ts", "a140/0.aac", "v134/1.ts"):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", f"/api/hls/{video.token}/{path}", headers={"Host": "127.0.0.1"})
        assert conn.getresponse().status == 200
        conn.close()
    assert post(server.port, GOOD)[1]["reported"] is True
    assert server.services.watch.sessions[VIDEO].page == {"fmt": "134", "afmt": "140"}  # the variant playing now
