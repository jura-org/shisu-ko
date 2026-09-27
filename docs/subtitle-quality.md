# Subtitle timing quality: standards, gaps, and a plan

Scope: cue timing and cue segmentation for the live Japanese overlay. Not transcription accuracy.
Everything about faster-whisper below was read from the installed 1.2.1 source at
`result-python/lib/python3.14/site-packages/faster_whisper/` (`transcribe.py`, `vad.py`), not from memory.

## (a) The numeric standards

| Rule | Netflix general | Netflix Japanese | BBC | JP film-subtitling trade practice |
|---|---|---|---|---|
| Min duration | 5/6 s = 20 frames @24fps | 500 ms | ~0.3 s per word (1.2 s for 4 words) | 0.5 s |
| Max duration | 7 s | (defers to general) | not stated | 7 s (some houses 6.5 s) |
| Lines | 2 | 2 | 2 (3 exceptionally) | 2 |
| Chars per line | 42 (Latin) | 13 full-width horizontal, 11 vertical; SDH 16 | ~68% of frame width | 13–14 horizontal, 10–11 vertical |
| Reading speed | 17 cps adult / 15 cps children (English) | 4 cps subtitles, 7 cps SDH | 160–180 wpm ≈ 0.33–0.375 s/word | 4 chars/s ("1秒4文字") |
| Min gap between cues | 2 frames (83 ms @24fps), all frame rates | — | "minimum gap time should be a second and a half" | — |
| Forbidden gaps | 3–11 frames: close to 2 frames. Legal gaps are 2 frames, or ≥12 frames (0.5 s) | — | — | — |
| In-cue vs audio | first frame of audio, within 1–2 frames (42–83 ms) is acceptable | — | cue up as the speaker starts | — |
| Out-cue vs audio | extend ≥0.5 s past end of audio when no cue follows | — | — | — |
| Shot changes | zones at 7 / 8–11 / 12 frames around the cut | — | a reason to vary timing | — |

Sources: [Netflix General Requirements](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215758617-Timed-Text-Style-Guide-General-Requirements),
[Netflix Subtitle Timing Guidelines](https://partnerhelp.netflixstudios.com/hc/en-us/articles/360051554394-Timed-Text-Style-Guide-Subtitle-Timing-Guidelines),
[Netflix Japanese TTSG](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215767517-Japanese-Timed-Text-Style-Guide),
[Netflix Subtitle Templates](https://partnerhelp.netflixstudios.com/hc/en-us/articles/219375728-Timed-Text-Style-Guide-Subtitle-Templates),
BBC Subtitle Guidelines v1.2.3 via [Clevercast's reproduction](https://www.clevercast.com/bbc-subtitling-guidelines/),
JP practice via [Vook](https://vook.vc/n/1339) and [映像翻訳.com](https://www.eizou-honyaku.com/category/jimaku/014.html).

Contradictions and unverified points, stated plainly:

- Netflix's own pages disagree on minimum duration: General Requirements says 5/6 s (0.833 s), the
  Timing Guidelines page says "20 frames (or 4/5 sec)" (0.8 s), and the Japanese guide says 500 ms.
  Treat 0.8 s as the general floor and 0.5 s as the Japanese floor.
- BBC's "minimum gap 1.5 s" and Netflix's "minimum gap 2 frames" look opposed but are not. Both say the
  same thing: a gap is either invisible (~0.1 s) or clearly deliberate (Netflix ≥0.5 s, BBC ≥1.5 s).
  The forbidden zone is the middle — long enough to see the screen blank, short enough to read as a glitch.
- I could not reach the primary BBC source (`bbc.co.uk` refused the fetch, `bbc.github.io/subtitle-guidelines`
  returned 404, the `bbc/subtitle-guidelines` repo is not public via the API). BBC numbers here are second-hand.
- The 4 cps Japanese reading speed is a **translation condensation budget**, not a transcription target.
  Japanese spontaneous speech runs roughly 7–9 morae/s; a verbatim transcript is physically ~6–10 chars/s.
  No verbatim pipeline can hit 4 cps without deleting words. The reachable target is Netflix's SDH 7 cps,
  and even that will be exceeded on fast speech. Chasing the 4 cps number would be chasing the wrong standard.

What professional guidance says "good" feels like, stripped to mechanics: cue in at speech onset (never after
it, at most ~2 frames early); cue out a beat *after* the audio, not on the last phoneme; close small gaps so a
run of dialogue reads as one flow instead of a strobe; keep durations within a narrow band so the eye stops
re-acquiring the text box; never let text outlive its audio by more than the deliberate lead-out.

## (b) What Shisu-ko does now, gap by gap

| Standard | Shisu-ko today | Gap |
|---|---|---|
| Min duration 0.5–0.8 s | `max(b, a + 0.4)` in `Transcriber.process()` | floor is 0.4 s, half the general minimum |
| Max duration ≤7 s | `--max-cue-seconds 7.0` | meets it, but 7 s + `lingerSeconds` 3 = 10 s on screen |
| Chars per cue ≤26 (JP) | `--max-cue-chars 42` | 42 is the *Latin* number; ~1.6× over the Japanese limit |
| Min gap 2 frames, no gaps in 0.08–0.5 s | no gap rule at all | every sub-0.5 s silence becomes a visible blink |
| In-cue at onset ±83 ms | first word's `start` from cross-attention DTW | unmeasured; first-word timings are the least reliable ones Whisper produces |
| Out-cue ≥0.5 s past audio | last word's `end`, then up to 3 s of client-side linger | lead-out is not in the data; it is a fixed 3 s client hack |
| No cues over non-speech | `vad_filter=True` only | VAD decides what the model *hears*, never what a cue *is* |
| Merge fragments | only folds a trailing cue shorter than 4 chars | no general merge, so 1–2 word cues survive and flicker |
| Split at pauses | splits on `。！？…` and `、`, on char count, on 7 s | never splits on a silence, so a 3 s pause sits inside one cue |

Three structural facts about faster-whisper 1.2.1 that matter here and are not obvious from the call site:

1. `vad_filter=True` does not merely gate. `transcribe()` runs Silero, then `collect_chunks()`
   **concatenates the speech and discards the silence** before the encoder ever sees it, in chunks up to
   `chunk_length` (30 s). Timestamps come back through `SpeechTimestampsMap`, which maps each word by its
   midpoint. So cue times are already VAD-derived — but the pipeline never sees those intervals, so it cannot
   snap to them, extend into them, or use them to judge a cue.
2. Passing a `vad_parameters` dict causes `max_speech_duration_s` to be popped and forced to 30. Also, the
   library's own default for the `vad_filter` path is `min_silence_duration_ms=160`; Shisu-ko's 400 is more
   conservative than that, while `VadOptions`' bare default is 2000. Unstated parameters take
   `VadOptions` defaults: `threshold=0.5`, `neg_threshold=threshold-0.15=0.35`, **`min_speech_duration_ms=0`**.
   Zero is the important one: a single 32 ms window above 0.5 becomes a speech chunk, so drum hits and
   vocal-ish synth transients are fed to the model. That is the direct cause of complaint (1).
3. `hallucination_silence_threshold` (default `None`, unused here) measures silence gaps *on the collapsed
   timeline*. With `vad_filter=True` the long silences are already gone, so the feature is largely toothless
   in this configuration. Its detector, however, is reusable: `word_anomaly_score()` scores a word +1 for
   `probability < 0.15`, `+ (0.133 - d) * 15` when shorter than 133 ms, `+ (d - 2.0)` when longer than 2 s;
   a segment is anomalous when the sum over its first 8 non-punctuation words is ≥3, or ≥ the word count.
4. Word timestamps are already patched by heuristics: `add_word_timestamps()` caps the median word duration
   at 0.7 s, truncates words longer than 2× median at sentence marks, and overrides the first or last word's
   time with the segment-level time when it looks too long. The source comments call these "hack". Conclusion:
   **first and last word boundaries are the least trustworthy numbers in the output** — exactly the two the
   cue builder uses for in and out times.

Client side: `lingerSeconds` 3 keeps a cue up to 3 s past its end unless the next cue has started. Combined
with a stretched final-word `end`, a cue can outlive its audio by 4–5 s. `RENDER_INTERVAL_MS` is 200 and
`timeupdate` fires ~4 Hz, so cue transitions carry ±200 ms of jitter — a quarter of a minimum-length cue.

## (c) Proposal, in priority order

### P0.1 — Own the VAD intervals (enables everything else)

`from faster_whisper.vad import VadOptions, get_speech_timestamps` and run it on the same window before
`model.transcribe()`. `get_vad_model()` is `functools.lru_cache`d over a 1.2 MB ONNX (`silero_vad_v6.onnx`),
so this reuses the already-loaded model: one extra CPU forward pass per 40 s window, milliseconds.
Pass the identical options dict to `transcribe()` so both passes see the same intervals.

Options: `threshold=0.5`, `neg_threshold=0.35`, `min_speech_duration_ms=250`,
`min_silence_duration_ms=300`, `speech_pad_ms=200`.

- `min_speech_duration_ms` 0 → 250 kills music transients at the source. Expected effect: the large majority
  of non-speech cues disappear before decoding. Risk: drops genuine one-mora interjections (はい, ん) — the
  short backchannels a learner wants. 250 ms is chosen to sit just above them being lost; verify empirically.
- `min_silence_duration_ms` 400 → 300 gives finer boundaries to snap to. Do **not** go below ~250: Japanese
  sokuon (っ) and pre-plosive closures are 150–200 ms of genuine silence *inside* a word, and Silero will
  split there.
- Do not pass `clip_timestamps` instead of `vad_filter`. In that path faster-whisper skips `collect_chunks()`,
  so every short utterance becomes its own 30 s-padded encoder pass. A 40 s window with 12 utterances would
  cost ~12 encodes instead of ~2. Wrong trade for an ahead-of-playhead pipeline.
- Note: `faster_whisper.vad` is not in the package `__all__`. Pin the version and cover it with a test.

### P0.2 — Hallucination filters (complaint 1)

Apply per segment, before `split_segment()`:

1. Drop segments with no words, or whose text after stripping punctuation is empty (already partly done by `JUNK_RE`).
2. **VAD overlap gate.** Compute `overlap = |cue ∩ speech intervals| / cue duration`. Drop when `< 0.5`,
   unless the text is ≥8 chars *and* mean word probability ≥0.5. Effect: the single highest-value filter —
   it directly encodes "no text where there is no voice". Risk: near-zero; VAD already gated the audio, so
   a cue failing this test is one whose timestamps drifted off its own chunk.
3. **Port `word_anomaly_score` / `is_segment_anomaly`** with upstream's thresholds (above) and drop anomalous
   segments whose VAD overlap is < 0.8. ~15 lines, reuses numbers someone else tuned. Risk: false positives
   on very fast speech (short words score on the `d < 0.133` term); the overlap gate is the safety.

   **Measured, and the risk was the whole gate.** The talk path now scores with `short_term=False`.
   Whisper's Japanese words are sub-tokens, usually a single kana, so they are under 133 ms by
   construction: over 15 minutes of 5csq1MlSspA dumped twice (279 segments at a 40 s window, 202 at
   30 s with an initial prompt) the first-8-words score reaches the threshold for 37 and 34 segments
   with the term, and for 0 and 2 without it — and those two survive on their VAD overlap and are
   real speech. Of the segments the gate deleted, all five were real utterances and none a
   hallucination: `1人暮らしかなって…9時半からなんですよ` (9.6 s, 31 words),
   `それこそ言語交換しようとしてる人が多いから`, `人間と関わりたくないってなって`,
   `あ、まじで?はいはいはいはい。`, and, on the live run, the 10 s block holding
   `一応、担任の先生とかいるの?` … `そうなんですよね`. The `p < 0.15` and `d > 2.0` terms stay, and so
   does the whole score on the lyrics path (P0.3), whose thresholds were measured with it.
4. **Repetition.** Drop a cue when any substring of ≥2 chars repeats ≥3 times consecutively, or when
   `compression_ratio > 2.2` for the segment. Whisper loops look exactly like this.
5. **Phrase blocklist**, gated, never unconditional: ご視聴ありがとうございました / ご覧いただきありがとうございます /
   チャンネル登録 / おやすみなさい / 字幕 + credit strings. Drop only when VAD overlap < 0.8 **or** the cue is
   isolated (≥3 s of silence on both sides). Risk: these are real sentences in real videos; the gate is what
   keeps the filter from deleting a genuine sign-off.
6. Set `hallucination_silence_threshold=2.0` anyway — free, harmless, but expect little from it (see (b).3).
   `condition_on_previous_text=False` is already correct and should stay.

### P0.3 — Sung lyrics (music videos, 歌枠)

**Observation.** An anime opening (a 34 s Short, sung over music) came out with `cues: 0` and the whole video
covered: Silero heard no speech at all (`speech: []`; 0 s at the 0.5 threshold, 2.7 s at 0.2), so nothing
of it ever reached the decoder. Whisper without the VAD transcribes the same audio cleanly (four segments,
`avg_logprob` −0.18, compression ratio 1.28); with the VAD at 0.2 it produced one garbled fragment. P0.1
therefore has a blind spot: singing is not speech to the detector, and a music video stays blank.

**Rule** (`wants_lyrics()`, `--lyrics auto`, the default): a window in which the detector heard less than
`LYRICS_MAX_SPEECH_S` (1 s) of speech, whose samples have an RMS of at least `LYRICS_MIN_RMS` (0.02, about
−34 dBFS), and in which Whisper's language head then hears the target language (`Transcriber.sung_in_target()`:
the window's own samples, `LANGUAGE_DETECT_SECONDS` of them at a time, with probability at least
`LANGUAGE_MIN_PROB`, 0.7), is transcribed with `vad_filter=False`, everything else unchanged. Sung windows
measure 0.11–0.47, a timelapse's background music 0.015, room tone far less, so the RMS floor keeps quiet
windows on the old path, where they cost nothing; the language verdict keeps loud non-speech there too (rain,
a crowd, an engine, an English song under a montage: on the old path faster-whisper decodes nothing of a
window Silero heard nothing in, while a full no-VAD decode of every such window would hand its inventions to
the gates, and an English song came out as English lines in a Japanese track). The Short's two windows score
`ja` 0.966 and 0.974 on the CPU, so sung Japanese clears the watch's own threshold. The verdict costs one
encoder pass and is no vote: the language watch runs on the detector's intervals and casts none on such a
window, and `sung_in_target()` never touches `language_vote()`, so a foreign song never pauses a video; it is
asked whatever `--language-patience` says (it guards a decode, not the pause), and a head that raises leaves
the window to the lyrics path, since a detector failure must never silence a video. Live streams take the
same path. A 40 s window is longer than the head's 30 s, so a song that starts in a window's last ten
seconds, after an instrumental intro, would be refused on the intro alone and those seconds covered blank for
good: the head judges the window's first thirty seconds and, when those are refused, its last thirty, and
either verdict for the target language sends the window to the lyrics path (a window sung throughout costs
one pass, a refused one two).

**A song beside the MC line.** `wants_lyrics()` judges a window whole, and the 歌枠 case has both in one
window: the singer announces the song (a second or two of detected speech), then sings for the rest of it,
which the detector hears nothing of. The window is talk to the rule, faster-whisper decodes the announcement
alone, and the singing would be covered with nothing in it, up to `--window` seconds of it at every
transition, the same at the song's end. So `process()` leaves the loud stretches of a talk window that the
detector heard nothing in (`unsung_stretches()`: at least `LYRICS_MIN_STRETCH_S`, 4 s, about one sung line,
no speech interval and no cue touching them, RMS over the floor) out of the covered range, and the planner
brings each back as a window of its own, where the rule sees next to no speech and judges it alone. Only a
window that the speech heard in it kept from the lyrics path: one the head refused is covered whole, or it
would be planned for ever. Under the floor a stretch is a pause with music under it more often than a line,
and each such window costs a Silero pass and an encoder pass for the head; a silent stretch costs nothing to
cover and is covered.

**Gates** (`lyrics_reason()`, in place of P0.2's; there is no VAD overlap to excuse a segment with):

1. "empty" as in P0.2.
2. "unsure": `no_speech_prob > LYRICS_MAX_NO_SPEECH` (0.9), or `avg_logprob < LYRICS_MIN_LOGPROB` (−0.8), or
   the mean word probability under `LYRICS_MIN_WORD_PROB` (0.35). Measured on this machine's cached videos,
   large-v3 int8 on the CPU: sung decode windows score `no_speech_prob` 0.07–0.42 as a rule, but a rap verse
   scored 0.59 (fourteen lines, all right) and an eighteen-voice chorus 0.80 (千本桜, every line right), while
   the one line made up over real background music (a drawing video's BGM) scored 0.47 and a sign-off over a
   2.3 s instrumental outro 0.85. The decoder's own "not speech" probability is no judge of singing, so it only
   refuses what the decoder is all but sure of (Whisper's own `no_speech_threshold` is 0.6 and acts only
   together with a failed log-probability). `avg_logprob` separates better: sung windows −0.14 to −0.58, made-up
   lines −0.49 to −0.91. The word probability catches garbled pieces (それられ at 0.30; genuine lines from 0.48).
3. "anomaly": `is_segment_anomaly()` unconditionally, and with the short-word term P0.2's talk path has since
   dropped (these thresholds were measured with it, and nothing has re-measured them). Cost: a very fast
   sung line can fall to it (ないないない 止めらんないない…, three lines of one song in the sample).
4. "repetition" as in P0.2.
5. "blocklist": any phrase, unconditionally. This is what actually stops the instrumental case: over background
   music Whisper's favourite invention is ご視聴ありがとうございました, at scores the other gates accept.

**Cues.** The accepted segments stand in for the speech intervals (`lyrics_spans()`), in the detector's own
shape: a segment's words are cut into runs at every gap of at least `pause_split` (0.45 s), and every run is
padded by `speech_pad_ms` (0.2 s) on both sides, as Silero pads what it hears, so that the cue builder sees
the breaths. Whole, unpadded segment spans hid them: the lead-out ate a breath under 0.8 s down to 0.1 s,
`merge_segments()` then glued the two lines into one row (会いたくて震える夜空を見上げて, one sentence to Yomitan
and to `cutFrom()`), and a pause inside a segment, two lines in one, which is the usual shape without the VAD
filter, never split, so the second line was on screen seconds before it was sung. A stranded first word
(`stranded_head()`: the anchoring artifact `repair_lead_words()` slides, a head of at most
`LEAD_REPAIR_CHARS` characters followed by a gap of `LEAD_REPAIR_GAP`) starts no run: a span over it merged
with the previous line's, the repair found the head in its interval already and moved nothing, and the line
came on screen seconds early with the breath gone from the window's speech. Its run starts a pad before the
next word, so the repair slides the head onto that onset. With this the lyrics path builds exactly the talk
path's cues for the same words (`test_lyrics.py` checks it for breaths from 0.5 to 2 s, a pause inside a
segment and a stranded head), and the spans are stored as the window's speech, so the sync's speech list and
the cache carry the sung lines (`/clip` never reads `Session.speech`: it slices the audio by the times the
client sends).

**Result.** The Short gives its four lines (seven cues); the rap verse fifteen lines, the chorus six; a 40 s
window of background music gives nothing. **Risk:** an instrumental window may still yield a made-up line the
gates let through (a phrase off the blocklist with a plausible log-probability), so a music video may show a
wrong line now and then where it used to show nothing; and a sung window the head is unsure of (a rap verse
over loud music, a chorus, a window too short to judge well) stays blank. `--lyrics off` restores the old
behaviour: such windows stay blank.

**Caches from before the rule.** A record written without it (a 0.11.2 server, which wrote format 3 without
the key, or `--lyrics off`) marked a sung stretch covered with nothing in it, and `load_cache()` would have
read it as finished: the fix would never reach a video already watched. So `save_cache()` writes the rule the
record was made under (`"lyrics"`), and `load_cache()` under `--lyrics auto` gives a format-3 record without
`"lyrics": "auto"` its blank stretches back: `unheard_stretches()` lists the parts of `covered` that no speech
interval and no cue touches, from 1.5 s (`plan_window()`'s own floor, so nothing unplannable is created) at
either end of a covered range, where a video's music sits (an intro, an outro, the whole of a Short), and from
`LYRICS_MIN_STRETCH_S` between two lines, as `process()` plans them for a fresh window, since the lead-out and
the detector's padding turn every pause of a talk into a hole of a second or two and a window per pause would
fetch, walk and rewrite every talk record dozens of times over; `subtract_intervals()` takes them out of
`covered`, the cues and the rest stay, and a session no longer covered to the end is fetched again and planned
over those stretches, where `wants_lyrics()` judges each window anew (a silent one costs a Silero pass and is
covered again), and the first window walked writes the record with the key. A record from 0.11.0 or 0.11.1
(format 2) never reaches the migration: the format check drops it whole, title kept, and the video is
transcribed again, which also brings a music video its lines.

### P1 — Rewrite `split_segment()` as `build_cues(words, speech_intervals)` (complaint 2)

Constants: `MIN_DUR 0.8`, `HARD_MIN_DUR 0.5`, `MAX_DUR 6.0`, `MAX_CHARS 26` (13 × 2 lines, Netflix JP),
`SOFT_CHARS 20`, `LEAD_IN 0.08`, `LEAD_OUT 0.50`, `MIN_GAP 0.10`, `DEAD_ZONE 0.50`, `PAUSE_SPLIT 0.45`.

1. **Trim.** Drop leading/trailing words whose midpoint falls outside every speech interval by >150 ms.
   Kills the stretched first/last word that (b).4 warns about.
2. **Break candidates**, in priority order: sentence-end punctuation; an inter-word gap ≥ `PAUSE_SPLIT`
   that coincides with a VAD silence ≥0.35 s; `、` once the buffer is ≥60% of `MAX_CHARS`; hard break at
   `MAX_CHARS` or `MAX_DUR`, backing off to the last clause boundary inside the final 40% of the buffer.
   The pause rule is what stops a 3 s pause living inside one cue.
3. **Snap in-cue.** `start = clamp(first_word.start, onset - LEAD_IN, onset + 0.30)` where `onset` is the
   containing speech interval's start. Meets Netflix's "within 1–2 frames of first audio" as closely as
   Whisper's alignment allows.
4. **Lead-out.** `end = last_word.end`; if the following silence ≥0.4 s, extend by `min(LEAD_OUT, silence - 0.1)`.
   Never past `next_cue.start - MIN_GAP`. This moves the lead-out from the client into the data, where it belongs.
5. **Minimum duration.** If `end - start < MIN_DUR`, extend into trailing silence; if still short and the
   neighbour is within 1.0 s, merge when the result stays ≤ `MAX_CHARS` and ≤ `MAX_DUR`; otherwise accept,
   but never below `HARD_MIN_DUR`.
6. **Merge fragments.** Adjacent cues separated by < 0.35 s merge when combined chars ≤ `MAX_CHARS` and
   duration ≤ `MAX_DUR`. This is the main anti-flicker rule.
7. **Normalise gaps.** Any gap in (`MIN_GAP`, `DEAD_ZONE`) closes to `MIN_GAP` by extending the earlier cue.
   Netflix's chaining rule, translated to seconds.
8. **Dedup across windows** by interval overlap, not string equality: drop a fresh cue overlapping an
   existing one by >50% of the shorter. The current `|Δstart| < 0.3 and text equal` test misses re-transcribed
   boundary segments that came back slightly reworded.

Expected effect: cue duration collapses into roughly [0.8, 6.0] instead of [0.4, 7.0]; the sub-0.5 s blanks
vanish; in/out times stop depending on Whisper's worst two numbers. Risks: `MAX_CHARS` 42 → 26 raises cue
count ~1.5× — more text swaps, partly offset by rule 6. More importantly, **shorter cues cut sentences, and
the mined Anki sentence comes from the cue**. That is a genuine conflict between display and mining; either
mine cue ± neighbours, or keep a `sentence_id` on each cue so mining can rejoin them. Decide before shipping.

**Decided, and then reversed.** `seg` shipped as that `sentence_id` and mining rejoined by it. It was
wrong: `seg` is a Whisper segment, a run of speech, and on a narrator who reads without pausing Whisper
punctuates almost nothing — one measured video had 7 marks in 3095 characters — so every split inside a
segment was rule 7's length limit and the rejoin undid all of them. A card mined off a 3 s line came back
with 9 s of audio and a clause the viewer never saw. Deferring to the VAD instead is worse, not better:
Silero cuts at 300 ms of silence, which is a breath, and the same narrator ran 14.26 s across three cues
between breaths. Neither signal marks a sentence. Mining now takes the cue and nothing else — the line
the viewer read and heard is the only thing certainly true of the card.

### P2 — Overlay rules

- `lingerSeconds` 3 → **0.3**, and cap the popup input at 1.0. Once P1.4 puts the lead-out in `end`, linger's
  only remaining job is absorbing the ±200 ms render jitter. Keeping 3 s on top of a real lead-out is what
  makes text outlive its audio. Risk: none beyond taste; the setting stays user-adjustable.
- **Never blank for less than 0.3 s.** If the next cue starts within 0.3 s of the current one's end, hold the
  current text instead of clearing. Belt-and-braces for gaps P1.7 missed (cues arriving out of order).
- `RENDER_INTERVAL_MS` 200 → 100, or drive from `requestVideoFrameCallback`. Cheap; halves transition jitter.
- Clamp `findActiveCue`'s linger by the next cue's start *including* cues that arrive later — already the
  behaviour, but re-check after the dedup change in P1.8, which can insert a cue behind the playhead.

### P3 — Alternatives considered, and why not now

- **WhisperX-style forced alignment.** Japanese is supported: `whisperx/alignment.py` maps `ja` to
  `jonatasgrosman/wav2vec2-large-xlsr-53-japanese` and lists `ja` in `LANGUAGES_WITHOUT_SPACES`
  ([repo](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py),
  [paper](https://www.robots.ox.ac.uk/~vgg/publications/2023/Bain23/bain23.pdf)). Two problems: it is a
  character-level CTC model, not a phoneme model, so kanji outside its vocab, digits and Latin loanwords get
  no timing at all; and it adds a second GPU model plus a second pass to a pipeline that must stay ahead of
  the playhead. Verdict: not worth it until P0–P2 are measured. Revisit only if measured start error stays >0.25 s.
- **stable-ts.** Its `suppress_silence` / `use_word_position` / `regroup` do roughly what P1 does, but inside
  a different transcription wrapper ([repo](https://github.com/jianfch/stable-ts)). Adopting it means giving
  up the single-file, dependency-light `server.py` invariant. Borrow the ideas, not the dependency.
- **Source separation (Demucs/UVR) for music.** Correct fix for sung vocals, far too slow for live. No.

## (d) What to measure

The cue caches already on disk (`~/.shisu-ko/cache/<video_id>.cues.json`) are a free corpus — every metric
below is computable from them plus one Silero pass over the audio. Add optional per-cue diagnostic fields
(`avg_logprob`, `no_speech_prob`, `vad_overlap`) behind a flag so the filters can be tuned from data.

| Metric | How | Target after the change |
|---|---|---|
| Cue duration distribution | p5 / p50 / p95; % < 0.8 s; % > 6 s | <2% under 0.8 s; 0% over 6 s; p50 in 1.5–3 s |
| Gap distribution | % of gaps in (0.1 s, 0.5 s) | <2% (today: unbounded) |
| Blank islands | count of blanks < 0.3 s per minute | 0 |
| Chars per second | p50, p95, % over 12 | p95 ≤ 12; report, do not enforce |
| Non-speech cues | cues with VAD overlap < 0.5, per minute | ~0 |
| Hallucination clips | fixed set: 60 s silence, 60 s BGM, an OP theme, a talking head | 0 cues on the first three |
| Overlay churn | text swaps per minute; mean on-screen time per cue | churn down vs baseline; on-screen ≥1.5 s median |
| Sync error | one hand-timed 2-minute clip; mean and p90 of \|start error\|, \|end error\| | median \|start error\| < 0.15 s |

Method: keep the decoded audio and re-run only the cue builder, so old and new are compared on identical
Whisper output — otherwise beam-search nondeterminism swamps the effect. The pure functions
(`plan_window`, `split_segment`/`build_cues`) are already unit-testable per `AGENTS.md`; every rule in P1 has
a number, so every rule gets a test with a synthetic word list.

Honest caveat: none of the targets above are validated against this codebase yet. They are derived from the
standards in (a) and from the faster-whisper source, not from a measured baseline. Measure first; the
baseline numbers may move the targets.

## (e) What was measured, and what changed

Section (d) asked for numbers and admitted it had none. This section has them, against `main`.

Method, as (d) prescribed: `server/tools/dump_words.py` transcribes 40 s windows of cached audio
with the server's own `model.transcribe()` call and writes the raw segments and word timings to
JSON; `server/tools/replay_cues.py` replays `build_window_cues()` over that JSON. Old and new are
compared on **identical Whisper output**, so beam-search nondeterminism cannot swamp the effect.
Three videos, ~45 minutes, chosen as the worst, a middling and the healthiest cache on disk. A
window the server would take the lyrics path on (P0.3) is dumped as it decodes it, without the
detector, and its record carries `"lyrics": true`; `replay_cues.py` then builds its cues on
`lyrics_spans()` through the lyrics gates, as `process()` does, and counts those spans as the
window's speech, so a change to the lyrics cue path is measured with the same rig.

### Three causes

**1. faster-whisper anchors a segment's first word to the previous segment's end.** Over 448
multi-word segments:

| gap between adjacent words | p50 | p90 | max |
|---|---|---|---|
| after word[0] | 0.00 s | **0.72 s** | **9.20 s** |
| every later position | 0.00 s | **0.00 s** | 3.73 s |

Position 0 holds 76% of all gaps at or over the 0.45 s split threshold while being 17% of
positions, and 23% of segments put word[0] in a different speech interval from word[1]:

```
チ[146.19-146.67]  ラ[152.54-152.70]  ッ  と  動画 ...    5.87 s inside チラッと
よ[184.14-184.42]  い[193.62-193.82]  しょ                9.20 s inside よいしょ
```

One artifact, five symptoms: words split mid-word; a single character floating over silence seconds
before its own sentence; `trim_words()` eating the leading mora; and — the expensive pair — the VAD
gate deleting the segment because its span now covers a silence it never contained, and the anomaly
gate deleting it because a six-second "word" scores +4. Those two dropped **29 real lines in 17
minutes**, キズナアイでーす, はじめまして! and ちょっと待って among them. Repairing the timings
before the gates brings that to 8.

**2. Nothing forbade a break inside a word.** Japanese speakers pause mid-word, VAD confirms it, and
`group_words()` obeyed. The one non-obvious rule is that hiragana after a **kanji** is okurigana,
while hiragana after **katakana** is not — カメラ|こうやって is a real boundary, 動|いた is not.

**3. Merging never crossed a Whisper segment.** `build_cues()` runs per segment, so
`merge_adjacent()` only ever saw one segment's cues — and 94% of neighbouring cues come from
different segments. The anti-flicker rule was dead code, and this is the largest single effect below.

### Before and after

`DhcrgdOzgic`, 17 minutes of a fast two-person collab, the worst cache on disk:

| metric | `main` | this branch |
|---|---|---|
| cues | 456 | 208 |
| duration p50 | 1.14 s | 3.78 s |
| under 1.0 s | 37.9% | **0.5%** |
| characters p50 | 7 | 23 |
| cues ≤ 4 chars | 28.7% | **1.9%** |
| cues ≤ 6 chars | 47.4% | **4.3%** |
| `kinsoku` (exact mid-word breaks) | 4 | **0** |
| `okuri` (suspected mid-word breaks) | 13 | 4 |
| `stubs` (under 4 chars beside a reachable gap) | 93 | **0** |
| cue time over silence | 3.9% | 1.0% |
| spoken seconds carrying text | 79.9% | **90.5%** |
| text swaps per minute | 26.6 | **12.1** |
| real lines deleted by the gates | 29 | 8 |

| video | cues ≤ 4 chars | kinsoku / okuri | stubs | speech with text | swaps/min |
|---|---|---|---|---|---|
| `DhcrgdOzgic` | 28.7% → 1.9% | 4 → 0 / 13 → 4 | 93 → 0 | 79.9% → 90.5% | 26.6 → 12.1 |
| `5oiVvJB3x9Q` | 23.9% → 3.6% | 2 → 0 / 8 → 1 | 40 → 1 | 81.9% → 88.3% | 20.5 → 10.4 |
| `IdYM3nWCWVk` | 6.9% → 0.6% | 0 → 0 / 2 → 0 | 7 → 0 | 60.6% → 64.3% | 16.3 → 14.7 |

`kinsoku` is exact and reaches zero on all three. Every surviving `okuri` is the heuristic's own
false positive — `…らしいえ?対象 || あどう?と`, `…から全然 || こういうピンクとか…`,
`…こう、透明感 || むらさきってそう…`, `声裏返りましたよ今 || まずやってきたのは` all cut between
two complete words. That is why the tool prints the pairs and not only the count.

The third video was already healthy, and it is here to show nothing over-merged: it loses 17 cues
out of 174 and gains nothing it did not need.

```
BEFORE                                  AFTER
129.8  コ                               131.1  コラボ配信をしていきたいと思いまーということで
131.2  ラボ配信をしていきたいと思いまー  139.1  ところばちゃん呼んでみましょう私もねあの今日が初対面なので
146.2  チ                               143.9  ドキドキしております
152.5  ラッと動画見たことあるんだけど…   152.4  チラッと動画見たことあるんだけどすごいね
154.5  可愛い感じの子                   154.5  可愛い感じの子だった気がする
157.7  だった気がする                   160.3  なんか、靴舐めますって言ってた
160.3  なんか、靴舐めますって言
163.4  ってた
```

### The two constants

`--max-cue-seconds` 6.0 → 7.0, Netflix's own maximum. The 6.0 in (c) was chosen when the median cue
was 1.14 s and the cap almost never bound; a merged cue now sits near it, and the cap was what left
`言ってた` alone as a four-character cue — the merge that would have absorbed it came to 6.96 s.
That matters beyond the flicker, because the cue is also the mined sentence (see (c), "Decided, and
then reversed"), and these two cues came from different Whisper segments, so nothing downstream
could have put the line back together.

| `--max-cue-seconds` | `5oiVvJB3x9Q` stubs | ≤ 4 chars | swaps/min | duration p95 |
|---|---|---|---|---|
| 6.0 | 5 | 4.8% | 11.9 | 5.48 s |
| **7.0** | **1** | **3.5%** | **10.6** | **6.01 s** |
| 7.5 | 1 | 3.6% | 10.4 | 6.24 s |

`lead_out` 0.50 → 0.70 and `lead_out_silence` 0.40 → 0.30, the beat after the audio:

| lead_out / silence | cues under 1.0 s | screen filled | tail after audio p50 |
|---|---|---|---|
| 0.50 / 0.40 | 3.5% | 71.5% | 0.72 s |
| **0.70 / 0.30** | **0.9%** | **73.6%** | **0.76 s** |
| 0.90 / 0.25 | 1.3% | 74.8% | 0.76 s |

0.70 is the knee: past it the extra screen time is bought by leaving text over silence, which rises
from 0.9% to 1.3%.

### Four traps

Each cost a whole cue or a whole line, silently, and each has a regression test.

- A head repair that slides a word **backwards** lands it on the previous utterance, where
  `cue_overlaps()` deletes the newer cue entirely. The first prototype did this and quietly lost
  `すごい、なんか未知との遭遇みたいな`. The repair moves forward only.
- Refusing a merge because it would leave a row below `MIN_PIECE_CHARS`, or a third row, is worse
  than merging: the stub stays on screen alone. The line break is what gives way. Refusing on the
  row count is what left `言ってた` by itself; refusing on the short row took `cues ≤ 4 chars` back
  up from 5.5% to 11.2%.
- One predicate cannot do both jobs. `may_break()` also says no on taste, and the merge reads its
  answer as "a word is broken here" and overrules its length budget. The first live run therefore
  produced a 41-character, 8.1-second cue, forced together only because `と` is a particle.
  `breaks_word()` is the narrow predicate, and it alone may overrule `max_chars`.
- The offline replay showed none of the last two. Both appeared only when the real pipeline took a
  different beam. Replay is for comparing builders; it is not a substitute for running the server.
