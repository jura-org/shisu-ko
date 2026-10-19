# SPDX-License-Identifier: Apache-2.0
"""The Anki sync after mining: the scheduler on a fake clock, the switch, and the /api/mine hook."""

import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "portal.py"
if "portal" in sys.modules:
    portal = sys.modules["portal"]
else:
    spec = importlib.util.spec_from_file_location("portal", PATH)
    portal = importlib.util.module_from_spec(spec)
    sys.modules["portal"] = portal  # `from __future__ import annotations` needs the module registered
    spec.loader.exec_module(portal)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def simulate(cards, duration=2.0, until=3600.0, fail=lambda n: None):
    """Cards at the given times; a sync takes `duration` seconds, during which the cards that fall
    inside it arrive; fail(n) is what sync number n raises (None: it goes through). Returns the
    start times and the scheduler."""
    clock = Clock()
    queue = sorted(cards)
    starts = []

    def sync():
        n = len(starts)
        starts.append(clock.t)
        end = clock.t + duration
        while queue and queue[0] < end:
            clock.t = queue.pop(0)
            sched.card_added()
        clock.t = end
        error = fail(n)
        if error:
            raise error

    sched = portal.AnkiSync(sync, clock=clock, wall=lambda: 1000.0)
    while True:
        card = queue[0] if queue else float("inf")
        due = sched.due if sched.due is not None else float("inf")
        t = min(card, due)
        if t == float("inf") or t > until:
            break
        clock.t = max(clock.t, t)
        if t == card:
            queue.pop(0)
            sched.card_added()
        else:
            assert sched.tick()
    return starts, sched


NOT_RUNNING = portal.UpstreamError("AnkiConnect is not answering at 127.0.0.1:8765 (ConnectionRefusedError)", 503)
NO_LOGIN = portal.UpstreamError("Anki: sync: auth not configured", 422)


def test_the_delays_are_named():
    assert (portal.SYNC_FIRST_DELAY, portal.SYNC_REPEAT, portal.SYNC_BACKOFF) == (60.0, 30.0, (60.0, 120.0, 300.0))


def test_one_card_one_sync_a_minute_later():
    starts, sched = simulate([0])
    assert starts == [60]
    assert sched.due is None and sched.last == {"at": 1000000, "ok": True}


def test_cards_in_the_first_minute_ride_along():
    assert simulate([0, 20, 59])[0] == [60]


def test_a_card_every_10_s_for_3_minutes_syncs_every_30_s_then_stops():
    starts, sched = simulate([i * 10 for i in range(18)])  # 0 .. 170 s
    assert starts == [60, 90, 120, 150, 180]
    assert sched.due is None and sched.cards == 0


def test_a_card_during_a_sync_gets_a_follow_up():
    assert simulate([0, 62], duration=5)[0] == [60, 90]


def test_a_card_long_after_the_last_sync_waits_its_minute():
    assert simulate([0, 100])[0] == [60, 160]


def test_a_slow_sync_is_followed_at_once_and_never_overlapped():
    assert simulate([0, 61, 70], duration=45)[0] == [60, 105]


def test_no_two_syncs_at_once_across_threads():
    gate = threading.Event()
    entered = threading.Event()
    calls = []
    clock = Clock()

    def sync():
        calls.append(clock.t)
        entered.set()
        gate.wait(5)

    sched = portal.AnkiSync(sync, clock=clock)
    sched.card_added()
    clock.t = 60
    first = threading.Thread(target=sched.tick)
    first.start()
    assert entered.wait(5)
    assert sched.tick() is False  # the one under way is the only one
    sched.card_added()  # made during the sync: counts for the next
    gate.set()
    first.join(5)
    assert calls == [60]
    assert sched.due == 90 and sched.cards == 1


def test_failures_back_off_1_2_5_5_minutes_while_cards_are_owed():
    starts, sched = simulate([0], duration=0, until=1200, fail=lambda n: NOT_RUNNING)
    assert starts == [60, 120, 240, 540, 840, 1140]
    assert sched.cards == 1
    assert sched.last["ok"] is False and sched.last["error"] == "Anki is not running"


def test_a_retry_that_goes_through_resets_the_backoff():
    starts, sched = simulate([0, 1000], duration=0, fail=lambda n: NOT_RUNNING if n < 2 else None)
    assert starts == [60, 120, 240, 1060]
    assert sched.failures == 0 and sched.cards == 0


def test_not_signed_in_stops_until_the_next_card():
    starts, sched = simulate([0], duration=0, fail=lambda n: NO_LOGIN)
    assert starts == [60]
    assert sched.halted and sched.due is None
    assert sched.last["error"] == "not signed in to AnkiWeb"
    starts, _ = simulate([0, 500], duration=0, fail=lambda n: NO_LOGIN if n == 0 else None)
    assert starts == [60, 560]


def test_failures_sort_into_retry_and_stop():
    assert portal.sync_failure(NOT_RUNNING) == ("Anki is not running", False)
    timeout = portal.UpstreamError("AnkiConnect is not answering at 127.0.0.1:8765 (TimeoutError)", 503)
    assert portal.sync_failure(timeout) == ("Anki did not answer in time", False)
    assert portal.sync_failure(NO_LOGIN) == ("not signed in to AnkiWeb", True)
    full = portal.UpstreamError("Anki: Sync status 2 not one of [0, 1] - see SyncCollectionResponse", 422)
    assert portal.sync_failure(full)[1] is True
    assert portal.sync_failure(portal.UpstreamError("Anki: collection is not available", 422)) == ("collection is not available", False)


def test_the_switch_off_plans_nothing_and_cancels_what_was_planned():
    on = {"value": False}
    calls = []
    clock = Clock()
    sched = portal.AnkiSync(lambda: calls.append(clock.t), enabled=lambda: on["value"], clock=clock)
    sched.card_added()
    assert sched.due is None and sched.cards == 0
    on["value"] = True
    sched.card_added()
    assert sched.due == 60
    sched.stop()
    clock.t = 60
    assert sched.tick() is False and calls == []
    sched.card_added()
    on["value"] = False  # switched off while planned: the tick plans nothing more
    clock.t = 120
    assert sched.tick() is False and calls == [] and sched.due is None


# ---------------------------------------------------------------- the switch and the mine hook


def make_services(tmp_path, config=None):
    path = tmp_path / "portal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if config is not None:
        path.write_text(json.dumps(config), encoding="utf-8")
    args = SimpleNamespace(mode="local", tailscale_bin="/nonexistent/tailscale", config=path)
    services = portal.Services(args)
    services.state = portal.PortalState(tmp_path / "state.json")
    services.card_file, services.sync_override = portal.read_config_file(path)
    return services


def test_the_switch_is_on_by_default_saved_with_the_portal_state(tmp_path):
    services = make_services(tmp_path)
    assert services.sync_info()["auto_sync"] is True and services.sync_info()["locked"] is False
    assert services.save_sync({"auto_sync": False})["auto_sync"] is False
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["auto_sync"] is False
    assert make_services(tmp_path).auto_sync() is False  # read back by a restarted portal
    with pytest.raises(portal.UpstreamError):
        services.save_sync({"auto_sync": "yes"})


def test_portal_json_auto_sync_overrides_the_switch(tmp_path):
    services = make_services(tmp_path, {"auto_sync": False, "deck": "Fixture deck"})
    assert services.card_file == {"deck": "Fixture deck"}
    info = services.sync_info()
    assert info["auto_sync"] is False and info["locked"] is True
    with pytest.raises(portal.UpstreamError) as refused:
        services.save_sync({"auto_sync": True})
    assert refused.value.status == 409
    with pytest.raises(ValueError):
        portal.read_config_file(_write(tmp_path / "bad.json", {"auto_sync": "on"}))


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def fake_anki(calls):
    def anki(action, timeout=15.0, **params):
        calls.append(action)
        if action == "canAddNotesWithErrorDetail":
            return [{"canAdd": True}]
        if action == "addNote":
            return 4242
        if action == "sync":
            return None
        raise AssertionError(f"unexpected AnkiConnect action {action}")
    return anki


MINE = {"video_id": "fixture01", "cue": {"text": "これはテストの文です", "start": 1.0, "end": 3.0},
        "span": [3, 6], "term": "テスト", "reading": "てすと"}


def mining_services(tmp_path, monkeypatch, calls, config=None):
    services = make_services(tmp_path, config)
    services.state.set_card({"deck": "Fixture deck", "note_model": "Fixture note",
                             "fields": {"Word": "{expression}", "Sentence": portal.SENTENCE_CLOZE}})
    anki = fake_anki(calls)
    monkeypatch.setattr(portal, "anki", anki)
    # add_note_once() took the real anki() as its default when it was defined.
    monkeypatch.setattr(portal, "add_note_once", lambda note, first, sentence: anki("addNote", note=note))
    monkeypatch.setattr(services, "anki_fields", lambda term, reading, fields: {
        "fields": {"expression": term, "reading": reading}, "audio": [], "dictionaryMedia": []})
    clock = Clock()
    services.anki_sync.clock = clock
    return services, clock


def test_a_mined_note_is_synced_a_minute_later(tmp_path, monkeypatch):
    calls = []
    services, clock = mining_services(tmp_path, monkeypatch, calls)
    assert services.mine(MINE)["ok"] is True
    assert services.anki_sync.due == 60 and "sync" not in calls
    clock.t = 59
    assert services.anki_sync.tick() is False
    clock.t = 60
    assert services.anki_sync.tick() is True
    assert calls.count("sync") == 1
    assert services.sync_info()["last"]["ok"] is True
    # A repeat of the same job (its answer lost on the way) adds no note, and so plans no sync.
    assert services.mine({**MINE, "job_id": "job-00000001"})["ok"] is True
    assert services.mine({**MINE, "job_id": "job-00000001"})["ok"] is True
    assert calls.count("addNote") == 2 and services.anki_sync.cards == 1


def test_a_mine_with_the_switch_off_plans_no_sync(tmp_path, monkeypatch):
    calls = []
    services, clock = mining_services(tmp_path, monkeypatch, calls)
    services.save_sync({"auto_sync": False})
    assert services.mine(MINE)["ok"] is True
    assert services.anki_sync.due is None
    calls.clear()
    fixed, _ = mining_services(tmp_path / "fixed", monkeypatch, calls, config={"auto_sync": False})
    assert fixed.mine(MINE)["ok"] is True
    assert fixed.anki_sync.due is None
