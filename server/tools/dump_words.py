#!/usr/bin/env python3
"""Transcribe windows of a cached video and write the raw Whisper segments and word timings.

Half of the A/B rig docs/subtitle-quality.md (e) describes. This half costs a GPU and runs once;
replay_cues.py then compares any number of cue builders against the JSON it leaves behind, so two
versions are judged on identical model output instead of on two beam searches.

Every window is decided as Transcriber.process() decides it: one the server would take the lyrics
path on (wants_lyrics(), then sung_in_target(): next to no speech heard, not silence, and the
target language heard in it) is decoded without the detector, and its record carries
"lyrics": true, which replay_cues.py reads. --lyrics off dumps every window through the detector.

    python server/tools/dump_words.py DhcrgdOzgic --from 80 --to 1120 --out /tmp/words
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


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
    p.add_argument("video_id")
    p.add_argument("--out", required=True, help="directory for <video_id>.words.json")
    p.add_argument("--cache", default=str(server.CACHE_DIR), help="directory holding <video_id>.<ext>")
    p.add_argument("--from", dest="start", type=float, default=0.0)
    p.add_argument("--to", dest="end", type=float, default=0.0, help="0 means the whole track")
    p.add_argument("--window", type=float, default=40.0)
    p.add_argument("--model", default="large-v3")
    p.add_argument("--device", default="auto")
    p.add_argument("--compute-type", default="auto")
    p.add_argument("--cpu-threads", type=int, default=0)  # load_model() reads it, as retranscribe.py does
    p.add_argument("--language", default="ja")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--lyrics", default="auto", choices=["auto", "off"],
                   help="auto: a window in which the speech detector hears next to nothing but the audio is "
                        "not silent is decoded without the detector when Whisper hears the target language in "
                        "it, and its record is marked \"lyrics\": true; off: every window goes through the detector")
    return p.parse_args(argv)


def audio_path(cache: Path, video_id: str):
    for p in sorted(cache.glob(f"{video_id}.*")):
        if p.suffix.lower() in server.AUDIO_SUFFIXES and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def worker_args(args) -> SimpleNamespace:
    """The server options Transcriber.sung_in_target() and wants_lyrics() read, the rest idle:
    retranscribe.py builds the same App, whose transcriber thread has no session to work on."""
    return SimpleNamespace(model=args.model, language=args.language, lyrics=args.lyrics,
                           language_patience=0.0, lookahead=0.0, client_timeout=0.0,
                           idle_minutes=10 ** 6, retry_after=10 ** 6,
                           beam_size=args.beam_size, initial_prompt="")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    src = audio_path(Path(args.cache), args.video_id)
    if src is None:
        print(f"no cached audio for {args.video_id} in {args.cache}", file=sys.stderr)
        return 1

    import numpy as np
    from faster_whisper.audio import decode_audio

    audio = np.ascontiguousarray(decode_audio(str(src), sampling_rate=server.SAMPLE_RATE), dtype=np.float32)
    total = len(audio) / server.SAMPLE_RATE
    end = min(args.end or total, total)
    print(f"[{args.video_id}] {total:.0f}s decoded; dumping {args.start:.0f}-{end:.0f}s", flush=True)

    # Through load_model(), so the dump comes from the backend the server would have used here:
    # CTranslate2, or MLX on an Apple GPU, where a WhisperModel() of our own would decode on the
    # CPU instead and the words would not be the ones the server writes.
    model, device, compute = server.load_model(args)
    lyrics_args = worker_args(args)
    # sung_in_target() lives on the transcriber and asks the App's model; the App's own worker
    # thread idles, since no session is ever registered with it.
    worker = server.Transcriber(server.App(lyrics_args, model, device, compute))
    stub = server.Session(video_id=args.video_id, url="")
    out = []
    start = args.start
    while start < end:
        stop = min(start + args.window, end)
        chunk = audio[int(start * server.SAMPLE_RATE): int(stop * server.SAMPLE_RATE)]
        speech = server.detect_speech(chunk, offset=start)
        # The same decision and the same call Transcriber.process() makes, so the dump is what the
        # server would have seen: a window it would take the lyrics path on is decoded without the
        # detector, and the record says so.
        lyrics = (server.wants_lyrics(lyrics_args, chunk, speech, start, stop)
                  and worker.sung_in_target(stub, chunk, start, stop))
        vad = {"vad_filter": False} if lyrics else {"vad_filter": True, "vad_parameters": server.vad_parameters()}
        segments, _info = model.transcribe(
            chunk, language=args.language, task="transcribe", beam_size=args.beam_size,
            word_timestamps=True, condition_on_previous_text=False, initial_prompt=None,
            temperature=[0.0, 0.2, 0.4, 0.6], no_speech_threshold=0.6, log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4, hallucination_silence_threshold=2.0, **vad,
        )
        record = {"window": [start, stop],
                  "speech": [[round(a, 2), round(b, 2)] for a, b in speech],
                  "lyrics": lyrics,
                  "segments": []}
        for seg in segments:
            record["segments"].append({
                "start": round(start + seg.start, 2), "end": round(start + seg.end, 2),
                "text": seg.text, "avg_logprob": round(seg.avg_logprob, 3),
                "no_speech_prob": round(seg.no_speech_prob, 3),
                "compression_ratio": round(seg.compression_ratio, 2),
                "words": [{"w": w.word, "s": round(start + w.start, 2), "e": round(start + w.end, 2),
                           "p": round(w.probability, 2)} for w in (seg.words or [])],
            })
        out.append(record)
        print(f"  {server.fmt_time(start)}-{server.fmt_time(stop)}: {len(record['segments'])} segments"
              f"{' [lyrics]' if lyrics else ''}", flush=True)
        start = stop

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{args.video_id}.words.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
