"""The premine: a watched line's clip and picture made before its card exists, and the attach that
takes them. The fixtures are test_mining.py's, all invented."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from _serverlib import load_server
from test_mining import LINES, NOW, OTHER, VIDEO, fresh_id, note_fields, rig  # noqa: F401 - rig is a fixture
from test_mining_sync import sync

server = load_server()


def premine_for(rig, clock):
    """The rig's miner with a premine on a clock the test moves."""
    rig.miner.premine = server.Premine(rig.app, rig.frames, clock=lambda: clock.t)
    return rig.miner.premine


def at(s, t):
    with s.lock:
        s.want_t = t


def test_cue_at_is_the_line_on_screen():
    cues = [{"id": 0, "start": 1.0, "end": 4.0}, {"id": 1, "start": 3.0, "end": 5.0}, {"id": 2, "start": 9.0, "end": 9.5}]
    assert server.cue_at(cues, 2.0)["id"] == 0
    assert server.cue_at(cues, 3.5)["id"] == 1  # overlapping: the one started last is on top
    assert server.cue_at(list(reversed(cues)), 3.5)["id"] == 1
    assert server.cue_at(cues, 5.0) is None and server.cue_at(cues, 7.0) is None and server.cue_at([], 1.0) is None


def test_the_line_at_the_playhead_is_made_ahead_and_the_attach_only_uploads(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    s = rig.session(t=14.0)
    rig.miner.observe(s)
    assert rig.clips == [] and rig.grabs == []  # observe() runs inside /sync: it makes nothing itself
    assert premine.run_once(wait=0)
    assert rig.clips == [(VIDEO, 12.8, 15.2, "mp3")] and rig.grabs == [(f"stream-{VIDEO}", 13.75)]
    rig.miner.observe(s)  # the same line again: made once
    assert not premine.run_once(wait=0)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これは<b>テスト</b>字幕です。"))
    event = rig.miner.attach_new(nid)
    assert event["status"] == "attached"
    assert len(rig.clips) == 1 and len(rig.grabs) == 1  # nothing cut or decoded at attach time
    assert rig.anki.actions == ["notesInfo", "storeMediaFile", "storeMediaFile", "notesInfo", "updateNoteFields"]
    name = f"shisuko_{VIDEO.lower()}"
    assert rig.anki.notes[nid]["Picture"]["value"] == f'<img src="{name}_13000.jpg">'
    assert rig.anki.notes[nid]["SentenceAudio"]["value"] == f"[sound:{name}_13000.mp3]"


def test_a_line_not_made_ahead_is_made_on_demand(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    s = rig.session(t=11.0)  # the viewer watched the first line only
    rig.miner.observe(s)
    premine.run_once(wait=0)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これは<b>テスト</b>字幕です。"))
    assert rig.miner.attach_new(nid)["status"] == "attached"
    assert rig.clips[-1] == (VIDEO, 12.8, 15.2, "mp3") and rig.grabs[-1] == (f"stream-{VIDEO}", 13.75)
    assert len(rig.clips) == 2 and len(rig.grabs) == 2


def test_a_part_that_failed_ahead_is_made_on_demand(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    real_clip, calls = rig.app.clip, []

    def clip(*args):
        calls.append(args)
        if len(calls) == 1:
            raise server.ClipNotReady()
        return real_clip(*args)

    rig.app.clip = clip
    s = rig.session(t=14.0)
    rig.miner.observe(s)
    premine.run_once(wait=0)
    nid = fresh_id()
    rig.anki.add(nid, note_fields(Sentence="これは<b>テスト</b>字幕です。"))
    assert rig.miner.attach_new(nid)["status"] == "attached"
    assert len(calls) == 2 and len(rig.grabs) == 1  # the clip cut again, the picture taken ready


def test_media_is_bound_to_the_session_the_cue_and_the_clip_settings(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    s = rig.session(t=14.0)
    rig.miner.observe(s)
    premine.run_once(wait=0)
    cue = dict(LINES[1])
    settings = s.anki
    assert premine.take(s, cue, settings, wait=0)["picture"]
    assert premine.take(s, dict(cue, end=15.5), settings, wait=0) == {"clip": None, "picture": None}  # revised cue
    assert premine.take(s, cue, dict(settings, pad_ms=500), wait=0)["clip"] is None  # another padding
    with s.lock:
        s.token = "restarted"  # the session started over: cue ids mean other lines now
    assert premine.take(s, cue, settings, wait=0) == {"clip": None, "picture": None}
    rig.miner.observe(s)
    assert premine.videos[VIDEO]["ready"] == {}


def test_memory_is_bounded_by_lines_and_videos(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    many = [{"id": i, "start": 2.0 * i, "end": 2.0 * i + 1.5, "text": f"台詞{i}"} for i in range(3 * server.PREMINE_LINES)]
    s = rig.session(cues=many)
    for cue in many:
        at(s, cue["start"] + 0.5)
        rig.miner.observe(s)
        premine.run_once(wait=0)
    ready = premine.videos[VIDEO]["ready"]
    assert len(ready) == server.PREMINE_LINES
    assert [key[1] for key in ready] == [c["id"] for c in many[-server.PREMINE_LINES:]]  # the last lines played
    # A viewer scrubbing faster than the lines are made: the line the playhead stopped on is the
    # one made, and none of those it passed.
    made = len(rig.grabs)
    for cue in many[:2 * server.PREMINE_LINES]:
        at(s, cue["start"] + 0.5)
        rig.miner.observe(s)
    assert premine.videos[VIDEO]["wanted"][0][1] == many[2 * server.PREMINE_LINES - 1]["id"]
    assert premine.run_once(wait=0) and not premine.run_once(wait=0)
    assert len(rig.grabs) == made + 1
    videos = [VIDEO, OTHER, "Vid3aaaaaaa", "Vid4bbbbbbb"]
    for vid in videos:
        clock.t += 1
        rig.miner.observe(rig.session(video_id=vid, t=14.0))
    assert list(premine.videos) == videos[-server.PREMINE_SESSIONS:]


def test_media_goes_when_its_video_is_no_longer_synced(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    s = rig.session(t=14.0)
    rig.miner.observe(s)
    premine.run_once(wait=0)
    clock.t += server.PREMINE_IDLE + 1  # the tab closed, or `enabled` went off: no /sync
    assert not premine.run_once(wait=0)
    assert premine.videos == {}
    assert premine.take(s, LINES[1], s.anki, wait=0) == {"clip": None, "picture": None}


def test_nothing_is_made_without_auto_or_between_lines(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    s = rig.session(t=12.7)  # between two lines
    rig.miner.observe(s)
    assert not premine.run_once(wait=0)
    at(s, 14.0)
    rig.miner.observe(s)
    with s.lock:
        s.anki = dict(s.anki, auto=False)
    rig.miner.observe(s)  # auto off: what was wanted and made goes
    assert premine.videos == {} and not premine.run_once(wait=0)
    with s.lock:
        s.anki = None  # a client that sends no settings (the portal)
    rig.miner.observe(s)
    assert premine.videos == {} and rig.clips == [] and rig.grabs == []


def test_nothing_is_made_while_anki_does_not_answer(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    up = [False]
    rig.miner.anki_up = lambda: up[0]
    s = rig.session(t=14.0)
    rig.miner.observe(s)
    assert premine.videos == {} and not premine.run_once(wait=0)
    up[0] = True
    rig.miner.observe(s)
    assert premine.run_once(wait=0)


def test_the_miner_asks_the_watcher_whether_anki_answers(monkeypatch, tmp_path):
    from test_update_endpoint import make_app

    monkeypatch.setattr(server.threading, "Thread", lambda *a, **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.delenv("SHISUKO_CONTAINER", raising=False)
    app = make_app(monkeypatch, tmp_path)
    args = SimpleNamespace(anki_sync="off", anki_url=None, mine=None, mine_picture="on")
    watcher = server.start_anki_watcher(args, app)
    assert app.miner.anki_up()  # not asked yet
    watcher.reachable = False
    assert not app.miner.anki_up()


def test_a_live_line_gets_its_clip_ahead_and_no_picture(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    s = rig.session(t=14.0)
    with s.lock:
        s.live = True
    rig.miner.observe(s)
    premine.run_once(wait=0)
    assert len(rig.clips) == 1 and rig.grabs == []


def test_an_attach_waits_for_the_line_being_made(rig):
    clock = SimpleNamespace(t=NOW)
    premine = premine_for(rig, clock)
    release = threading.Event()
    decode = rig.frames.decode

    def slow(url, t):
        release.wait(5)
        return decode(url, t)

    rig.frames.decode = slow
    s = rig.session(t=14.0)
    rig.miner.observe(s)
    worker = threading.Thread(target=premine.run_once, kwargs={"wait": 0})
    worker.start()
    while premine.busy is None:
        time.sleep(0.001)
    threading.Timer(0.05, release.set).start()
    media = premine.take(s, LINES[1], s.anki, wait=5)
    worker.join()
    assert media["picture"] and media["clip"] and len(rig.grabs) == 1


def test_sync_names_the_line_to_the_premine(rig):
    rig.session(t=14.0)
    sync(rig.app)  # t=14.0 with the extension's settings: auto is on by default
    key, _ = rig.miner.premine.videos[VIDEO]["wanted"]
    assert key[1] == 1
    rig.app.miner = None
    sync(rig.app)  # mining off: no miner, nothing named
