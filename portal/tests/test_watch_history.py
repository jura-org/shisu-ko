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
CAPTURED = {**TRACKING, "client": CLIENT}
RESPONSES = [
    {"streamingData": {}},  # a client's response without tracking
    {"playbackTracking": {"videostatsPlaybackUrl": {"baseUrl": PLAYBACK},
                          "videostatsWatchtimeUrl": {"baseUrl": WATCHTIME}},
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
        self.extractor.mark_watched(VIDEO, RESPONSES)
        if "cookiesfrombrowser" not in self.options and not FakeYoutubeDL.anonymous_hls:
            return {"duration": 600, "title": "t", "formats": []}
        return {"duration": 600, "title": "t", "formats": [{"protocol": "m3u8_native", "manifest_url": "https://example.invalid/m"}]}


@pytest.fixture
def fake_ytdlp(monkeypatch):
    FakeYoutubeDL.anonymous_hls = True
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


def test_resolve_without_the_switch_captures_nothing(home, fake_ytdlp):
    with_cookies(home)
    FakeYoutubeDL.anonymous_hls = False  # resolved signed in, yet the switch is off
    video = portal.HlsVideo(VIDEO)
    portal.HlsProxy().resolve(video)
    assert video.tracking is None and len(FakeYoutubeDL.made) == 2
    assert all(ydl.extractor.own_ran for ydl in FakeYoutubeDL.made)  # yt-dlp's own, untouched


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
])
def test_bad_reports_are_refused(body):
    with pytest.raises(ValueError):
        portal.parse_watch_report(body)


def test_a_good_report():
    got = portal.parse_watch_report({"video_id": VIDEO, "ranges": [[0, 604.5]], "duration": 600, "cmt": 604, "state": "paused",
                                     "url": "https://evil.example/"})
    assert got == {"video_id": VIDEO, "ranges": [(0.0, 604.5)], "cmt": 604.0, "duration": 600.0, "state": "paused"}


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
    assert post(server.port, GOOD) == (200, {"ok": True, "reported": True})
    assert server.services.watch.sessions[VIDEO].pending == [(0.0, 30.0)]
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
    assert set(got) == {"video_id", "ranges", "cmt", "duration", "state"}
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
    assert post(server.port, GOOD) == (200, {"ok": True, "reported": True})
    assert server.services.watch.sessions[VIDEO].page == {"fmt": "134", "afmt": "140"}  # the variant playing now
