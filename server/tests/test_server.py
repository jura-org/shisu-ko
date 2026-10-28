"""Unit tests for the pure planning / caching / formatting helpers in server/server.py.

The cue builder and the hallucination gates have their own file, test_cues.py.

These do not touch the network, GPU or a real Whisper model: only the functions that are
plain data in, data out, as called out in the README's Development section.
"""
from __future__ import annotations

import errno
import os
from types import SimpleNamespace

import pytest

from _serverlib import load_server

server = load_server()


def make_args(**overrides):
    base = dict(first_window=20.0, window=40.0, lookahead=900.0)
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- fmt_time

@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0:00"),
        (5, "0:05"),
        (65, "1:05"),
        (599, "9:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (-5, "0:00"),  # clamped to zero
    ],
)
def test_fmt_time(seconds, expected):
    assert server.fmt_time(seconds) == expected


# --------------------------------------------------------------------------- merge_intervals

def test_merge_intervals_sorts_and_merges_overlaps():
    # [0,5] and [4,12] overlap into [0,12]; [10,20] then overlaps that too, so everything
    # collapses into a single interval.
    out = server.merge_intervals([[10, 20], [0, 5], [4, 12]])
    assert out == [[0, 20]]


def test_merge_intervals_merges_adjacent_within_gap():
    out = server.merge_intervals([[0, 5], [5.03, 10]], gap=0.05)
    assert out == [[0, 10]]


def test_merge_intervals_keeps_far_apart_intervals_separate():
    out = server.merge_intervals([[0, 5], [6, 10]], gap=0.05)
    assert out == [[0, 5], [6, 10]]


def test_merge_intervals_drops_empty_or_inverted_intervals():
    out = server.merge_intervals([[5, 5], [10, 2]])
    assert out == []


def test_merge_intervals_empty_input():
    assert server.merge_intervals([]) == []


# --------------------------------------------------------------------------- find_covering / next_start_after

def test_find_covering_returns_matching_interval():
    intervals = [(0.0, 5.0), (10.0, 20.0)]
    assert server.find_covering(intervals, 12.0) == (10.0, 20.0)


def test_find_covering_respects_tolerance():
    intervals = [(0.0, 5.0)]
    assert server.find_covering(intervals, 5.2, tol=0.25) == (0.0, 5.0)
    assert server.find_covering(intervals, 5.3, tol=0.25) is None


def test_find_covering_no_match_returns_none():
    assert server.find_covering([(0.0, 5.0)], 7.0) is None


def test_next_start_after_picks_smallest_start_greater_than_t():
    intervals = [(0.0, 5.0), (20.0, 25.0), (10.0, 15.0)]
    assert server.next_start_after(intervals, 5.0) == 10.0


def test_next_start_after_returns_none_when_nothing_later():
    assert server.next_start_after([(0.0, 5.0)], 5.0) is None


# --------------------------------------------------------------------------- friendly_error

@pytest.mark.parametrize(
    "message, expected_snippet",
    [
        ("ERROR: Sign in to confirm you're not a bot", "sign-in"),
        ("This video is Private video", "private"),
        ("This video is members-only, join this channel", "Members-only"),
        ("No javascript runtime could be found", "Node.js or Deno"),
        ("ERROR: Video unavailable", "unavailable"),
    ],
)
def test_friendly_error_recognizes_known_causes(message, expected_snippet):
    result = server.friendly_error(RuntimeError(message))
    assert expected_snippet.lower() in result.lower()


def test_friendly_error_falls_back_to_last_line_truncated():
    long_line = "x" * 300
    exc = RuntimeError(f"first line\n{long_line}")
    result = server.friendly_error(exc)
    assert result == long_line[:200]
    assert len(result) == 200


def test_friendly_error_uses_class_name_when_message_empty():
    class Boom(Exception):
        pass

    assert server.friendly_error(Boom()) == "Boom"


# --------------------------------------------------------------------------- JUNK_RE / VIDEO_ID_RE

@pytest.mark.parametrize("text", ["", "   ", "...", "―", "_"])
def test_junk_re_matches_punctuation_only_text(text):
    assert server.JUNK_RE.match(text)


@pytest.mark.parametrize("text", ["hello", "あ", "a.", "42"])
def test_junk_re_rejects_text_with_content(text):
    assert not server.JUNK_RE.match(text)


@pytest.mark.parametrize("video_id", ["dQw4w9WgXcQ", "abcdef", "A_B-C123456789"])
def test_video_id_re_accepts_youtube_ids(video_id):
    assert server.VIDEO_ID_RE.match(video_id)


@pytest.mark.parametrize("video_id", ["", "short", "has space", "semi;colon", "x" * 21])
def test_video_id_re_rejects_invalid_ids(video_id):
    assert not server.VIDEO_ID_RE.match(video_id)


# --------------------------------------------------------------------------- Session.fully_covered

def test_session_fully_covered_true_when_one_interval_spans_the_video():
    s = server.Session(video_id="abcdefabcdef", url="u", duration=100.0, covered=[[0.0, 100.0]])
    assert s.fully_covered()


def test_session_fully_covered_false_with_gaps_or_multiple_intervals():
    s = server.Session(video_id="abcdefabcdef", url="u", duration=100.0, covered=[[0.0, 50.0], [50.0, 100.0]])
    assert not s.fully_covered()


def test_session_fully_covered_false_with_no_duration():
    s = server.Session(video_id="abcdefabcdef", url="u", duration=0.0, covered=[[0.0, 0.0]])
    assert not s.fully_covered()


# --------------------------------------------------------------------------- plan_window

def test_plan_window_none_when_session_not_ready():
    s = server.Session(video_id="abcdefabcdef", url="u", status="pending")
    assert server.plan_window(s, make_args()) is None


def test_plan_window_starts_a_fresh_window_from_the_playhead():
    s = server.Session(video_id="abcdefabcdef", url="u", status="ready", audio=object(), duration=120.0, want_t=10.0)
    window = server.plan_window(s, make_args(first_window=20.0))
    assert window == (9.5, 29.5)


def test_plan_window_continues_after_a_covered_region():
    s = server.Session(
        video_id="abcdefabcdef", url="u", status="ready", audio=object(),
        duration=200.0, want_t=5.0, covered=[[0.0, 30.0]],
    )
    window = server.plan_window(s, make_args(window=40.0, lookahead=0))
    assert window == (30.0, 70.0)


def test_plan_window_none_when_fully_covered_to_the_end():
    s = server.Session(
        video_id="abcdefabcdef", url="u", status="ready", audio=object(),
        duration=100.0, want_t=0.0, covered=[[0.0, 100.0]],
    )
    assert server.plan_window(s, make_args()) is None


def test_plan_window_stops_at_lookahead_limit():
    s = server.Session(
        video_id="abcdefabcdef", url="u", status="ready", audio=object(),
        duration=1000.0, want_t=0.0, covered=[[0.0, 30.0]],
    )
    assert server.plan_window(s, make_args(lookahead=10.0)) is None


def test_default_lookahead_is_left_to_the_backend():
    # No flag: parse_args() leaves it open, and the loaded model's backend decides.
    assert server.parse_args([]).lookahead is None
    assert server.default_lookahead_for("cuda") == 180.0
    assert server.default_lookahead_for("mlx") == 900.0
    assert server.default_lookahead_for("cpu") == 900.0


def lookahead_app(monkeypatch, tmp_path, device, argv=()):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = server.parse_args(["--model", "large-v3", *argv])
    return server.App(args, model=None, device=device, compute_type="float16")


@pytest.mark.parametrize("device, expected", [("cuda", 180.0), ("mlx", 900.0), ("cpu", 900.0)])
def test_app_resolves_the_default_lookahead_for_its_backend(monkeypatch, tmp_path, device, expected):
    assert lookahead_app(monkeypatch, tmp_path, device).args.lookahead == expected


@pytest.mark.parametrize("given", ["300", "0"])
def test_an_explicit_lookahead_wins_on_every_backend(monkeypatch, tmp_path, given):
    for device in ("cuda", "mlx", "cpu"):
        app = lookahead_app(monkeypatch, tmp_path, device, ["--lookahead", given])
        assert app.args.lookahead == float(given), device
        app.device = "cpu" if device == "cuda" else "cuda"  # a switch or a CPU fallback moves nothing
        app.settle_lookahead()
        assert app.args.lookahead == float(given), device


def test_the_default_lookahead_follows_a_backend_change(monkeypatch, tmp_path):
    # A CUDA load that fell back to the CPU refills near real time: the slow default applies.
    app = lookahead_app(monkeypatch, tmp_path, "cuda")
    app.device = "cpu"
    app.settle_lookahead()
    assert app.args.lookahead == 900.0
    app.device = "cuda"
    app.settle_lookahead()
    assert app.args.lookahead == 180.0


def test_default_lookahead_on_cuda_stops_planning_three_minutes_ahead(monkeypatch, tmp_path):
    args = lookahead_app(monkeypatch, tmp_path, "cuda").args

    def session(covered_end):
        return server.Session(
            video_id="abcdefabcdef", url="u", status="ready", audio=object(),
            duration=1500.0, want_t=100.0, covered=[[0.0, covered_end]],
        )

    # Below the limit the planner keeps filling: the next window starts at the covered end.
    assert server.plan_window(session(279.0), args) == (279.0, 309.0)
    # At exactly the limit it plans one more window; past it, nothing.
    assert server.plan_window(session(280.0), args) == (280.0, 310.0)
    assert server.plan_window(session(280.5), args) is None
    assert server.plan_window(session(310.0), args) is None


def test_plan_window_ignores_lookahead_when_disabled():
    s = server.Session(
        video_id="abcdefabcdef", url="u", status="ready", audio=object(),
        duration=1000.0, want_t=0.0, covered=[[0.0, 30.0]],
    )
    assert server.plan_window(s, make_args(lookahead=0, window=40.0)) == (30.0, 70.0)


def test_plan_window_is_clipped_by_the_next_covered_region():
    s = server.Session(
        video_id="abcdefabcdef", url="u", status="ready", audio=object(),
        duration=200.0, want_t=0.0, covered=[[0.0, 10.0], [15.0, 20.0]],
    )
    window = server.plan_window(s, make_args(window=40.0, lookahead=0))
    assert window == (10.0, 15.0)


def test_plan_window_merges_tiny_remaining_gap_instead_of_returning_it():
    s = server.Session(
        video_id="abcdefabcdef", url="u", status="ready", audio=object(),
        duration=200.0, want_t=0.0, covered=[[0.0, 10.0], [10.8, 20.0]],
    )
    result = server.plan_window(s, make_args(window=40.0, lookahead=0))
    assert result is None
    assert s.covered == [[0.0, 20.0]]


# --------------------------------------------------------------------------- App cache round trip

def test_app_cache_round_trips_cues_for_matching_model_and_drops_for_different_model(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)

    args = make_args(model="large-v3", language="ja", idle_minutes=30)
    app = server.App(args, model=None, device="cpu", compute_type="int8")

    original = server.Session(video_id="abcdefabcdef", url="u")
    original.title = "A great video"
    original.cues = [{"start": 0.0, "end": 1.0, "text": "hello"}]
    original.covered = [[0.0, 1.0]]
    original.duration = 10.0
    app.save_cache(original)

    reloaded = server.Session(video_id="abcdefabcdef", url="u")
    app.load_cache(reloaded)
    assert reloaded.title == "A great video"
    assert [c["text"] for c in reloaded.cues] == ["hello"]
    assert reloaded.covered == [[0.0, 1.0]]

    other_model_args = make_args(model="a-different-model", language="ja", idle_minutes=30)
    other_app = server.App(other_model_args, model=None, device="cpu", compute_type="int8")
    reloaded_other = server.Session(video_id="abcdefabcdef", url="u")
    other_app.load_cache(reloaded_other)
    assert reloaded_other.title == "A great video"
    assert reloaded_other.cues == []


def test_app_cache_stores_the_format_version_segment_ids_and_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = make_args(model="large-v3", language="ja", idle_minutes=30)
    app = server.App(args, model=None, device="cpu", compute_type="int8")

    original = server.Session(video_id="abcdefabcdef", url="u")
    original.cues = [{"start": 0.0, "end": 1.0, "text": "hello", "seg": 0},
                     {"start": 1.2, "end": 2.0, "text": "world", "seg": 1}]
    original.covered = [[0.0, 2.0]]
    original.speech = [[0.0, 2.0]]
    original.duration = 10.0
    app.save_cache(original)

    import json

    data = json.loads((tmp_path / "abcdefabcdef.cues.json").read_text(encoding="utf-8"))
    assert data["format"] == server.CACHE_FORMAT
    assert data["speech"] == [[0.0, 2.0]]

    reloaded = server.Session(video_id="abcdefabcdef", url="u")
    app.load_cache(reloaded)
    assert [c["seg"] for c in reloaded.cues] == [0, 1]
    assert reloaded.seg_next == 2  # the next window continues the segment numbering
    assert reloaded.speech == [[0.0, 2.0]]


def test_app_cache_ignores_a_cache_without_the_current_format(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    args = make_args(model="large-v3", language="ja", idle_minutes=30)
    app = server.App(args, model=None, device="cpu", compute_type="int8")

    import json

    # A version 1 cache: right model, no format key, so its cues carry no segment ids.
    (tmp_path / "abcdefabcdef.cues.json").write_text(json.dumps({
        "video_id": "abcdefabcdef", "title": "Old", "duration": 10.0,
        "model": "large-v3", "language": "ja",
        "cues": [{"start": 0.0, "end": 1.0, "text": "hello"}], "covered": [[0.0, 10.0]],
    }), encoding="utf-8")

    reloaded = server.Session(video_id="abcdefabcdef", url="u")
    app.load_cache(reloaded)
    assert reloaded.title == "Old"  # the title survives, as it does for a model change
    assert reloaded.cues == []
    assert reloaded.covered == []


# --------------------------------------------------------------------------- instance lock

def test_instance_lock_keeps_a_second_server_off_the_port_before_the_model_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "APP_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "INSTANCE_LOCK", None)
    path = server.instance_lock_path(8790)
    assert path == tmp_path / "data" / "server-8790.lock"
    assert server.hold_instance_lock(8790) is True
    try:
        assert server.INSTANCE_LOCK is not None and not server.INSTANCE_LOCK.closed
        assert server.try_lock(path) is None, "held: a second server stops here, exit code 2 in main()"
        other = server.try_lock(server.instance_lock_path(8791))
        assert other is not None, "another port is another server, e.g. a test instance"
        other.close()
    finally:
        server.INSTANCE_LOCK.close()
    again = server.try_lock(path)
    assert again is not None, "released with the file, however the process ends"
    again.close()


def test_instance_lock_never_stops_the_server_when_the_file_is_unusable(tmp_path, monkeypatch, caplog):
    (tmp_path / "data").write_text("")  # a file where the data directory should be
    monkeypatch.setattr(server, "APP_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "INSTANCE_LOCK", None)
    with caplog.at_level("WARNING", logger="shisu-ko"):
        assert server.hold_instance_lock(8790) is True
    assert server.INSTANCE_LOCK is None
    assert "instance lock" in caplog.text


def lock_call_raises(monkeypatch, code: int) -> None:
    """Make the platform's non-blocking lock call fail with `code`, the file itself opening fine."""
    def raise_(*args):
        raise OSError(code, os.strerror(code))
    if os.name == "nt":
        monkeypatch.setattr(server.msvcrt, "locking", raise_)
    else:
        monkeypatch.setattr(server.fcntl, "flock", raise_)


def test_instance_lock_reads_only_a_held_lock_as_held(tmp_path, monkeypatch, caplog):
    """ENOLCK (an NFS home without a lock manager), EOPNOTSUPP, EINVAL: the file cannot be locked at all.

    That is an unusable lock, not another server; reading it as held would exit 2 on every start.
    """
    monkeypatch.setattr(server, "APP_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "INSTANCE_LOCK", None)
    lock_call_raises(monkeypatch, errno.ENOLCK)
    with pytest.raises(OSError):
        server.try_lock(server.instance_lock_path(8790))
    with caplog.at_level("WARNING", logger="shisu-ko"):
        assert server.hold_instance_lock(8790) is True
    assert server.INSTANCE_LOCK is None
    assert "instance lock" in caplog.text
    lock_call_raises(monkeypatch, errno.EACCES if os.name == "nt" else errno.EWOULDBLOCK)
    assert server.try_lock(server.instance_lock_path(8790)) is None, "what a held lock raises"
    assert server.hold_instance_lock(8790) is False


# --------------------------------------------------------------------------- Hugging Face defaults

def test_hugging_face_notices_are_off_before_the_library_loads():
    """The xet backend, the "set a HF_TOKEN" nag (an X-HF-Warning header the Hub sends, logged
    twice) and the Windows symlink warning are switched off through the environment the module
    sets on import, so the library, imported later by load_model(), starts with them."""
    import os

    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    assert os.environ["HF_HUB_VERBOSITY"] == "error"
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1"
