#!/usr/bin/env python3
"""Transcribe windows of a cached video and write the raw Whisper segments and word timings.

Half of the A/B rig docs/subtitle-quality.md (e) describes. This half costs a GPU and runs once;
replay_cues.py then compares any number of cue builders against the JSON it leaves behind, so two
versions are judged on identical model output instead of on two beam searches.

Every window is decided as Transcriber.process() decides it: one the server would take the lyrics
path on (wants_lyrics(), then sung_in_target(): next to no speech heard, not silence, and the
target language heard in it) is decoded without the detector, and its record carries
"lyrics": true, which replay_cues.py reads. --lyrics off dumps every window through the detector.
A talk window whose prompted decode skipped speech is decoded again without the prompt and the
two spliced (retry_prompt_skips()), as process() does; its record carries "prompt_retry": true.

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
    p.add_argument("--language", default="ja")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--initial-prompt", default=None, help="text prompt given to Whisper for every window, as the "
                   "server's --initial-prompt (default: DEFAULT_PROMPTS for --language, \"\" for none); it "
                   "reaches only the first 30 s of a window, and never a lyrics window, as in the server")
    p.add_argument("--lyrics", default="auto", choices=["auto", "off"],
                   help="auto: a window in which the speech detector hears next to nothing but the audio is "
                        "not silent is decoded without the detector when Whisper hears the target language in "
                        "it, and its record is marked \"lyrics\": true; off: every window goes through the detector")
    args = p.parse_args(argv)
    if args.initial_prompt is None:  # the same resolution parse_args() does, so a bare dump is
        args.initial_prompt = server.DEFAULT_PROMPTS.get(args.language, "")  # what process() decodes
    return args


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
                           beam_size=args.beam_size, initial_prompt=args.initial_prompt)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    src = audio_path(Path(args.cache), args.video_id)
    if src is None:
        print(f"no cached audio for {args.video_id} in {args.cache}", file=sys.stderr)
        return 1

    import numpy as np
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    audio = np.ascontiguousarray(decode_audio(str(src), sampling_rate=server.SAMPLE_RATE), dtype=np.float32)
    total = len(audio) / server.SAMPLE_RATE
    end = min(args.end or total, total)
    print(f"[{args.video_id}] {total:.0f}s decoded; dumping {args.start:.0f}-{end:.0f}s", flush=True)

    if server.is_kitsune_model(args.model):
        # A Kitsune model loads as the server loads it (kitsune_engine.py, on PyTorch).
        load_args = SimpleNamespace(model=args.model, language=args.language, device=args.device,
                                    compute_type=args.compute_type, cpu_threads=0)
        model = server.load_kitsune_model(load_args, args.model)[0]
    else:
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type,
                             download_root=str(server.MODELS_DIR))
    lyrics_args = worker_args(args)
    # sung_in_target() lives on the transcriber and asks the App's model; the App's own worker
    # thread idles, since no session is ever registered with it.
    worker = server.Transcriber(server.App(lyrics_args, model, args.device, args.compute_type))
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
        lyrics = (getattr(model, "sings", True) and server.wants_lyrics(lyrics_args, chunk, speech, start, stop)
                  and worker.sung_in_target(stub, chunk, start, stop))
        options = server.transcribe_options(lyrics_args, model, lyrics)
        segments, _info = model.transcribe(chunk, **options)
        segments, skipped, spliced = list(segments), 0.0, 0
        if not lyrics:
            # The prompt-skip retry process() runs: the record holds the spliced list, so
            # replay_cues.py builds on what the server would have built on.
            segments, skipped, spliced = server.retry_prompt_skips(model, chunk, options, segments, start, speech)
        record = {"window": [start, stop],
                  "speech": [[round(a, 2), round(b, 2)] for a, b in speech],
                  "lyrics": lyrics,
                  "prompt_retry": skipped > 0,
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
              f"{' [lyrics]' if lyrics else ''}"
              f"{f' [{skipped:.0f} s skipped with the prompt, {spliced} segments from a decode without it]' if skipped else ''}",
              flush=True)
        start = stop

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{args.video_id}.words.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
