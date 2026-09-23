#!/usr/bin/env python3
"""Re-run the current cue pipeline over cached audio, window by window, and write new cue files.

Loads the model once and drives server.py's own plan_window/Transcriber.process, so the output
is what the live server would produce for the same video. Writes <video_id>.new.cues.json and
<video_id>.speech.json into the output directory, and prints how many segments each gate dropped.

    python server/tools/retranscribe.py 2q3XN2rYEGE --out /tmp/new

--model follows the server's own default: the model chosen at setup (~/.shisu-ko/config.json),
else this machine's built-in default, so a measurement without the flag runs the model the
server runs (large-v3-turbo on an Apple GPU, large-v3 elsewhere).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # the models are already in ~/.shisu-ko/models


def load_server():
    """Same dance as server/tests/_serverlib.py: server.py is a script, not a package."""
    name = "shisuko_server"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent.parent / "server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server = load_server()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video_ids", nargs="+")
    p.add_argument("--out", required=True, help="directory for the new cue and speech files")
    p.add_argument("--cache", default=str(server.CACHE_DIR), help="directory holding <video_id>.<ext> audio")
    p.add_argument("--model", default=None, help="Whisper model (default: the model chosen at setup (config.json), else this machine's built-in default: large-v3-turbo on an Apple GPU, large-v3 elsewhere)")
    p.add_argument("--device", default="auto")
    p.add_argument("--compute-type", default="auto")
    p.add_argument("--language", default="ja")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--lyrics", default="auto", choices=["auto", "off"],
                   help="auto: a window in which the speech detector hears next to nothing (under a second of speech) "
                        "but the audio is not silent, sung lyrics or speech over music, is transcribed without the "
                        "detector when Whisper hears the target language in it, under stricter gates; off: such "
                        "windows go through the detector as before, blank when it heard nothing")
    p.add_argument("--initial-prompt", default="")
    p.add_argument("--window", type=float, default=40.0)
    p.add_argument("--first-window", type=float, default=20.0)
    p.add_argument("--max-cue-chars", type=int, default=30)
    p.add_argument("--max-cue-seconds", type=float, default=7.0)
    p.add_argument("--min-cue-seconds", type=float, default=0.8)
    p.add_argument("--limit-seconds", type=float, default=0.0, help="only transcribe the first N seconds")
    p.add_argument("--cpu-threads", type=int, default=0)
    args = p.parse_args(argv)
    args.lookahead = 0.0          # no playhead here: cover the whole track
    args.language_patience = 0.0  # transcribe every window, whatever language it is in
    args.client_timeout = 0.0
    args.idle_minutes = 10 ** 6
    args.retry_after = 10 ** 6
    return server.resolve_default_model(args)


def audio_path(cache: Path, video_id: str):
    for p in sorted(cache.glob(f"{video_id}.*")):
        if p.suffix.lower() in server.AUDIO_SUFFIXES and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def count_drops(totals: dict, examples: list):
    """Wrap build_window_cues so every window's gate counts land in one place."""
    original = server.build_window_cues

    def wrapper(segs, offset, speech, limits, seg_id, drops=None, window_end=None, lyrics=False):
        seen: dict = {}
        result = original(segs, offset, speech, limits, seg_id, seen, window_end, lyrics)
        for key, value in seen.items():
            if key == "_text":
                examples.extend(value)
            else:
                totals[key] = totals.get(key, 0) + value
        return result

    server.build_window_cues = wrapper
    return original


def run_video(app, worker, args, video_id: str, src: Path, out_dir: Path) -> dict:
    from faster_whisper.audio import decode_audio

    import numpy as np

    t0 = time.time()
    audio = np.ascontiguousarray(decode_audio(str(src), sampling_rate=server.SAMPLE_RATE), dtype=np.float32)
    if args.limit_seconds > 0:
        audio = audio[: int(args.limit_seconds * server.SAMPLE_RATE)]
    s = server.Session(video_id=video_id, url="")
    s.audio = audio
    s.duration = len(audio) / server.SAMPLE_RATE
    s.status = "ready"
    print(f"[{video_id}] {s.duration:.0f}s of audio decoded in {time.time() - t0:.1f}s", flush=True)

    t0 = time.time()
    while True:
        window = server.plan_window(s, args)  # want_t stays 0, so this walks the covered edge forward
        if window is None:
            break
        worker.process(s, window[0], window[1])
    took = time.time() - t0
    print(f"[{video_id}] {len(s.cues)} cues in {took:.0f}s ({s.duration / max(took, 1e-3):.0f}x realtime)", flush=True)
    write_results(app, s, out_dir)
    return {"cues": len(s.cues), "seconds": s.duration}


def write_results(app, s, out_dir: Path) -> None:
    """<video_id>.new.cues.json, in the shape save_cache() writes (the "lyrics" key included, so
    the file is a record made under the rule to load_cache() if it is ever put in the cache
    directory, not one to migrate), and <video_id>.speech.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{s.video_id}.new.cues.json").write_text(
        json.dumps(app.cache_record(s), ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{s.video_id}.speech.json").write_text(
        json.dumps([[round(a, 2), round(b, 2)] for a, b in s.speech]), encoding="utf-8")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    out_dir = Path(args.out).expanduser()
    cache = Path(args.cache).expanduser()
    server.CACHE_DIR = out_dir  # save_cache must not touch the real cue cache
    out_dir.mkdir(parents=True, exist_ok=True)

    model, device, compute = server.load_model(args)
    app = server.App(args, model, device, compute)  # its worker thread idles: no session is registered
    worker = server.Transcriber(app)

    totals: dict = {}
    examples: list = []
    count_drops(totals, examples)

    for video_id in args.video_ids:
        src = audio_path(cache, video_id)
        if src is None:
            print(f"[{video_id}] no cached audio found in {cache}", file=sys.stderr)
            continue
        run_video(app, worker, args, video_id, src, out_dir)

    print("\ndropped segments by gate:")
    for key, value in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {key:<12} {value}")
    (out_dir / "drops.json").write_text(json.dumps({"counts": totals, "examples": examples}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
