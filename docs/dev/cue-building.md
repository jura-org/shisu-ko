# Cue building: how the server schedules and builds cues

How `server/server.py` plans windows, decodes them and turns Whisper output into cues. Read it before changing anything between `plan_window()` and `normalise_gaps()`. `docs/subtitle-quality.md` holds the rationale and the measurements.

## How the server schedules work

`plan_window()` in `server/server.py` decides what to transcribe next: if the playhead is not
inside a covered range, a short `--first-window` (20 s) starts at the playhead; otherwise the next
`--window` (30 s) continues from the end of the covered range, up to `--lookahead` seconds ahead.
30 s is faster-whisper's own chunk, and that is why the window is no longer 40: with
`condition_on_previous_text` false the library drops the initial prompt after the first chunk of a
call (checked in 1.2.1), so a 40 s window decoded its last ten seconds unprompted.
A segment touching the end of a window is dropped and the covered range ends where that segment
began, so the next window re-transcribes it whole. These functions are pure; test them by importing the
module (register it in `sys.modules` before `exec_module` because of `from __future__ import annotations`).

Cue building (`docs/subtitle-quality.md` is the rationale and the measurements): the server runs
Silero VAD itself on each window (`detect_speech()`, with `VAD_PARAMS`: min speech 250 ms, min
silence 300 ms) and passes the same options to faster-whisper.

Whisper is asked for punctuation rather than left to guess at it: `--initial-prompt` defaults to
`DEFAULT_PROMPTS[args.language]` (resolved in `parse_args()`; only `ja` has an entry, any other
language gets nothing, and an explicit `--initial-prompt ""` turns it off), a short punctuated
sentence in the style the subtitles should read. Each window is decoded with nothing in front of
it, so without the prompt the decoder has no reason to write a 。 at all: measured over 15 minutes
of 5csq1MlSspA, marks land on 88% of the hand-labelled sentence ends with the prompt and 53%
without, and not one line of the prompt reached the transcript. A lyrics window is decoded with
`initial_prompt=None`: its gates were measured on unprompted decodes and the blocklist holds no
sentence of the prompt, so a noisy window the language head lets through could echo the prompt
itself into the cache with nothing to catch it. `dump_words.py` resolves the default and withholds
it the same way, or dump plus replay would no longer be an A/B on the server's own decode.

The prompt has one cost: it sometimes makes Whisper jump its first timestamp past real speech, and
what it jumped is never decoded. Measured with `dump_words.py`, prompted against unprompted on the
server's own windows: 10 to 23 s lost at the start of a window, and one first window decoded as a
single line, 言い返す!, its 返 stretched over thirteen seconds of speech it never heard, while the unprompted decodes start on time; about one skip per
12 minutes of speech. So `process()` checks every prompted talk window (`retry_prompt_skips()`):
`skipped_speech()` finds the runs of detected speech holding at least `PROMPT_SKIP_MIN_S` (3 s) that
no covering segment comes within `PROMPT_SKIP_SLACK` (0.5 s) of. Covering (`kept_spans()`) means the
gates keep it (`gate_segment()`, the one judge `build_window_cues()` also asks) and no word of it
spans `PROMPT_SKIP_MIN_S` of detected speech (`stretched_over_speech()`): that
line passes every gate, and only its word shows the jump; a last word stretched over the silence after
an utterance spans no speech and still covers. Only then is the window decoded once more with
`initial_prompt=None`, everything else the same. `splice_segments()` takes from that decode the
covering segments whose midpoint lies in a skipped stretch and that overlap no covering prompted
segment by more than `PROMPT_SPLICE_OVERLAP` (0.2 s), drops a stretched prompted segment an added
one overlaps, and `build_window_cues()` gets the combined list in time order. One retry at most; a
retry that adds nothing leaves the prompted result as it was. The log line gains
` [N s skipped with the prompt, K segments from a decode without it]`. Never on a lyrics window or
with an empty prompt. `dump_words.py` calls the same function, writes the spliced list and marks
the record `"prompt_retry": true`, so `replay_cues.py` builds on what the server built on.

`repair_lead_words()` runs first, before the gates. faster-whisper anchors a segment's first word to
the segment's own start, and segment starts run flush with the previous segment's end, so one or two
characters end up stranded in the previous utterance, seconds ahead of the sentence they open
(measured: the gap after word[0] has p90 0.72 s and a worst case of 9.2 s, while every later position
has p90 0.00 s). It only ever slides such a head **forward**, only while it is short, and only out of
an interval it does not already share with the rest of the segment: moving one backwards drops it on
the previous utterance, where `cue_overlaps()` then deletes a whole good cue. It runs before
`hallucination_reason()` because an unrepaired head makes the segment's span cover a silence it never
contained, and the VAD and anomaly gates then delete real speech — 29 lines in 17 minutes of the
sample, against 8 once repaired.

`punctuate_words()` runs next, over every kept segment of a talk window before any cues are built,
because the pause behind a segment's last word, and the word that follows it, are in the next kept
segment, which is handed over whole (`next_word`) so that every test below applies across a segment
boundary as it does inside one. Never on a lyrics window: its `speech` is the padded word runs
`lyrics_spans()` made, so every breath inside a sung line reads as a pause, and these thresholds
were measured on talk. The cue
builder has no sentence signal of its own — every cut in `group_words()` and every seam in
`seam_for()` defers to Whisper's punctuation — and Whisper writes it inconsistently: at 18:06 of
one video the live run decoded そうなんですよねおじいちゃん先生とゲームの話したりするの with no
mark, one 29-character cue, while a second decode of the same audio wrote そうなんですよね。 and
the same builder gave three. Neither half of the evidence stands alone (a speaker pauses inside a
word; よね runs on mid-sentence), so the rule is the pair, the one Akita et al. (2006) reach F 0.85
with on spontaneous Japanese: a word whose text so far ends in a sentence-final shape and is
followed by a pause takes a `。`, or a `？` for か, かな, っけ, でしょ and でしょう. `SENTENCE_STRONG`
(sentence-final particles and the polite and copula endings) needs `limits.sentence_pause` (0.30 s);
`SENTENCE_WEAK` (the plain forms た, ない, る, い, which end a casual sentence as often as they run
on into the next clause) needs `sentence_pause_weak` (0.60 s). The pause is the longer of the
detector's silence inside [the word's own start, the next word's start] and Whisper's raw gap to
the next word, because each hides what the other shows: Whisper anchors a segment's last word to
the end of the audio it decoded, so the silence usually lies inside that word's span where the gap
reads zero, while Silero refuses a silence under 300 ms and pads what it keeps by 200 ms on each
side, so the measured 0.34 s after よね sat in the middle of one interval. Nothing is written over
a mark already there, before a word opening on a character that can never open one, before a word
that **is** a particle (`SENTENCE_PARTICLES`, the whole word and not its first character: です before
か is left alone and the か judged instead, while はい, やっぱり, もう and ところで all open a sentence
and all start with a particle kana), or after a word that nothing follows, since the pause is the
evidence and a window's last segment has none. The particle test is skipped when the next word ends
in a clause mark, or the word after it opens with one: Whisper writes the sentence-initial connective
with its own comma, `で、` or `で` then `、`, and that comma says the で opens a sentence rather than
closing a phrase. Three tables refuse a shape on the words in front of it, each from a measured false
mark: `SENTENCE_NOT_ENDINGS` for a word that merely ends in a shape kana (何か, そんな, また — a shape
is a suffix test, so without it 何か行きたい became 何か？行きたい), `FILLER_KA` for なんか, とか,
というか … where the speaker is choosing the next word rather than asking (but not the nominaliser
ことか, `SHAPE_EXCEPT`), and `SENTENCE_INTERJECTIONAL` after a connective (けど, から, し, て, で,
のに, ので), where the speaker is holding the floor (めっちゃ偏見だけどさ ‖ はいはい). They cost no
labelled sentence end on the measured range and take mark precision from 89.5% to 97.1%.
`--sentence-ends off` (`limits.sentence_ends`) turns the rule off, and `replay_cues.py` and
`retranscribe.py` take the same switch, so a before and after run on identical Whisper output.
In `merge_segments()` a previous cue whose last row holds at least `MIN_PIECE_CHARS` characters and
ends in a `SENTENCE_END` mark no longer counts as `short`: a finished sentence is not a stub, so it
does not buy the `cross_reach` budget a half-line is given. Nor is `forced` allowed to override such
a mark (`not ends_sentence(prev_row) and breaks_word(...)`): a next cue opening on ー or a small kana
makes `breaks_word()` true, and its flat join put two sentences on one row (そうですね。ーっと言います),
when nothing is split inside a word after the speaker has ended one. `carry_trailing_mark()` in
`build_cues()` is the other half of keeping a mark: the word the rule marks is a segment's last, and
that is the word Whisper stretches over the silence after the utterance, so `trim_words()` reads its
midpoint as noise and drops it. The timings go, which is what P1.1 is for; the mark moves onto the
word that now ends the cue.

Segments go through gates before becoming cues: no words, VAD overlap under 0.5, faster-whisper's own
word-anomaly score, repetition loops, and a gated phrase blocklist. The talk path scores the anomaly
without the short-word term (`is_segment_anomaly(words, short_term=False)`): Whisper's Japanese words
are sub-tokens, usually one kana, so they fall under the 133 ms it penalises whatever the speaker did,
and the term measured the tokenizer rather than the audio. Over two 15-minute dumps the first-8-words
score reached the threshold for 37 of 279 segments and 34 of 202 with the term and for 0 and 2 without
it, and every segment the gate actually deleted was real speech - a 10 s block holding
一応、担任の先生とかいるの? … そうなんですよね on the live run, a 9.6 s block of 31 words, three shorter
lines - with no hallucination among them. `lyrics_reason()` keeps the term: its thresholds were measured
with it, and nothing has re-measured them.

`build_cues(words, speech, limits)` then trims words outside speech, splits at sentence ends, long
pauses and `--max-cue-chars`/`--max-cue-seconds`, snaps starts to speech onsets, adds a lead-out into
following silence, merges fragments below `--min-cue-seconds`, and closes gaps under 0.5 s.

Every cut asks `may_break(head, tail)` first, because a pause is not a word boundary: Japanese
speakers pause inside words, VAD confirms the silence, and the splitter used to obey (言 | ってた,
広 | い, 動 | いた). The rules are kinsoku shori plus one piece of script evidence: nothing opens a
line with a small kana, っ, ー, 々 or a closing mark; neither side may be shorter than
`MIN_PIECE_CHARS`; hiragana after a **kanji** is okurigana; nothing opens on a particle. Katakana is
deliberately outside the okurigana rule — katakana words are self-delimiting, so the hiragana after
カメラ does open a word. A speaker's own 。！？ or 、 outranks every guess. `split_for_break()`
applies the same rules when a buffer runs past the hard limits, including to the seam against the
word that follows it, plus one more (`may_split()`, `joins_compound()`): no cut between two kanji or
two katakana (能|力, 学|校, カメ|ラ). Only there, since a length limit is no evidence of a boundary,
while a pause the detector confirmed is, even between two kanji (天気 … 電車). `may_break()` is
asked about a prospective line, not one Whisper word: Whisper's words are sub-tokens, often one character, so `group_words()` gathers just enough of what
follows (`tail_text()`) to reach `MIN_PIECE_CHARS` before asking.

`breaks_word()` is the narrow half of `may_break()`, and the two must stay apart. `may_break()` also
says no on taste — a short piece, a line opening on a particle — which is right for the splitter,
where refusing a cut costs nothing. `merge_segments()` reads its predicate as "a word is broken here"
and overrules its own length budget to close it, so it must ask `breaks_word()`, which answers only
on evidence. Wiring the merge to `may_break()` produced a 41-character, 8.1-second cue on the first
live run, forced together only because と is a particle.

`merge_segments()` is the last step of `build_window_cues()`, and must run **before**
`normalise_gaps()`: closing every gap to `min_gap` first would hide the pause the seam is judged on.
`build_cues()` runs once per Whisper segment, so `merge_adjacent()` only ever saw one segment's cues,
and in a two-person conversation 94% of neighbouring cues come from different segments — which left
the anti-flicker rule dead code and the median cue seven characters long. The cross-segment merge
closes a broken word whatever the budget says (inside `cross_chars` and `cross_ceiling`), lets a cue
shorter than `reach_chars` reach `cross_reach` for a partner, and joins the halves through
`seam_for()`: `""` inside one sentence, `"\n"` where a viewer would see a new line. Three readers act
on that newline — `.shisuko-sub` is `white-space: pre-wrap` so the overlay renders the second row,
Yomitan ends its sentence there, and `match.js`'s `TERMINATORS` splits on it. A seam the gap alone
put there gives way to a plain join when it would leave a row under `MIN_PIECE_CHARS` or a third row
(`rows_fit()`), rather than the merge being refused; refusing leaves the stub alone on screen.

A sentence mark is the one seam that never gives way (`ends_sentence()`), in `merge_adjacent()` as
well as `merge_segments()`: what the speaker finished and what follows it never share a row, and a
merge that cannot give them a row each is refused instead of flattened. A short row is no reason
to flatten one: `rows_fit(text, limits, at_mark=True)` only refuses a third row, because うん。 is a
whole turn and not a stub we left by breaking badly, which is what `MIN_PIECE_CHARS` judges elsewhere. Measured on 15 minutes of
5csq1MlSspA decoded with `--initial-prompt`, 22% of all cues held a mark mid-row and read as two
people on one line (`マジで?それいいね。`, `出そう、出そう、出そう。だって、集合に行くの誰?`), which
also put the other speaker's clause on every card mined from one. Hard boundary in both merges took
the share of hand-labelled sentence ends the viewer actually sees from 66% to 90% on that decode, and
from 71% to 82% on the unprompted one. `merge_adjacent()` seams only at a mark — a gap-based seam
there would refuse the stub merges the anti-flicker rule exists for — so pieces without a mark join
flat exactly as before. The cost is accepted: a short finished sentence (`まじ?`, `うん。`) now stands
as its own cue for its `min_seconds` instead of riding on a neighbour's row.

Every cue still carries `seg`, the id of the Whisper segment it came from, and a merge re-stamps
every cue carrying a swallowed segment's id. Mining does not read it: a segment is a run of speech,
not a sentence, and rejoining its cues put clauses on a card that were never on screen (see "What a
mined card gets"). `seg` stays for `cue_stats.py`, which measures a change per segment, and a stale
id would mis-group it. Cue caches are format 6; older caches are ignored, which is the only way a
geometry change reaches a video someone has already watched. `server/tools/cue_stats.py` and
`retranscribe.py` measure a cache before and after a change, and `dump_words.py` + `replay_cues.py`
compare two cue builders on identical Whisper output; keep them working (`retranscribe.py` wraps
`build_window_cues()`, so its wrapper takes every argument, `lyrics` included; `dump_words.py`
decides every window as `process()` does, `wants_lyrics()` and then `sung_in_target()` on an idle
`App` like `retranscribe.py`'s, decodes a sung one without the detector and writes `"lyrics": true`
into its record, its own `--lyrics off` sending every window through the detector; `replay_cues.py`
passes a record's `lyrics` through, building on `lyrics_spans()` and the lyrics gates).

Sung lyrics (P0.3 of the doc): singing over music is no speech to Silero, so `process()` decides
`wants_lyrics()` after the detector and the language watch: with `--lyrics auto` (default; `off`
is the switch), less than `LYRICS_MAX_SPEECH_S` (1 s) of detected speech in the window and an
`rms()` of at least `LYRICS_MIN_RMS` (0.02), and then, that holding, a target-language verdict on
the window from `Transcriber.sung_in_target()` (the language head over the window's own samples,
`speech_samples(audio, [[start, end]], start)`, the window's first `LANGUAGE_DETECT_SECONDS` (30 s)
and, when those are refused and the window is longer, its last 30 s, so a song starting after an
instrumental intro in a window's last seconds is not refused on the intro alone: two encoder
passes at most; `args.language` at `LANGUAGE_MIN_PROB` or more on either; asked whatever
`--language-patience` says, since it guards a decode, not the pause; a head that raises
sends the window down the lyrics path, since a detector failure must never silence a video, one
that hears another language or is unsure leaves it with the detector, which decodes nothing of a
window Silero heard nothing in: rain, a crowd, an English song under a montage stay blank), the
window is transcribed with `vad_filter=False` and no `vad_parameters`, everything else as
usual. `build_window_cues(..., lyrics=True)` then gates each
segment with `lyrics_reason(seg, words)` instead of `hallucination_reason()`: "empty"; "unsure" for
`no_speech_prob > LYRICS_MAX_NO_SPEECH` (0.9), `avg_logprob < LYRICS_MIN_LOGPROB` (-0.8) or a mean
word probability under `LYRICS_MIN_WORD_PROB` (0.35); "anomaly" (`is_segment_anomaly()`, always);
"repetition"; "blocklist" (any phrase, unconditionally, since no VAD evidence can rescue it). The
thresholds are measured, not guessed, and the doc holds the numbers: a rap verse and an 18-voice
chorus score `no_speech_prob` 0.59 and 0.80 with every line right, so that gate only refuses what
the decoder is all but sure of. `lyrics_spans(segs, offset, limits)`, the padded word runs of the
segments that pass, stands in for the speech intervals, in the detector's own shape: a segment's
words are cut into runs at every gap of at least `pause_split`, every run is padded by
`speech_pad_ms` on both sides, as Silero pads what it hears, so the cue builder sees the breaths
(a whole, unpadded segment span hid them: the lead-out ate a breath, `merge_segments()` glued two
lines into one row, and a pause inside a segment never split), and a stranded first word
(`stranded_head()`: the anchoring artifact of `repair_lead_words()`, a head of at most
`LEAD_REPAIR_CHARS` characters followed by a gap of `LEAD_REPAIR_GAP`) starts no run, its run
beginning a pad before the next word, so the repair slides the head onto that onset as it does on
the talk path. `process()` passes them as `speech` and stores them in `Session.speech`, so the
sync's speech list and the cache carry the sung lines (nothing else reads them today; `/clip`
slices the audio by the times the client sends); the log line gains ` [lyrics]`. A song shares a
window with the line announcing it (a 歌枠's MC line): `wants_lyrics()` judges the window whole,
the second of speech sends it the detector's way, and nothing of the singing is decoded, so
`process()` leaves the `unsung_stretches()` of a talk window (loud, `rms()` at least
`LYRICS_MIN_RMS`, unheard, `unheard_stretches()` of the window at `LYRICS_MIN_STRETCH_S`, 4 s,
under both floors) out of `covered` when the window holds `LYRICS_MAX_SPEECH_S` or more of
detected speech, and only then (a window the head refused, or `--lyrics off`, is covered whole,
or it would be planned for ever); the planner brings each back as a window of its own, where the
rule sees next to no speech and judges it alone, and the log line gains
` [N s heard nothing in, planned again]`. `App.cache_record(s)` is the record `save_cache()`
writes (`"lyrics"` beside the cues, the covered ranges and the speech), and `retranscribe.py`'s
`write_results()` writes the same. The language watch is untouched: without speech intervals it
casts no vote, and the head's verdict in `sung_in_target()` never reaches `language_vote()`, so
a foreign song never pauses a video. Live streams take the same path (a 歌枠 gets its lyrics).
Tests: `server/tests/test_lyrics.py` (a fake model and a patched `detect_speech`, like `test_language.py`).

Language watch. YouTube goes first, the audio only where YouTube says nothing.

`declared_language(info)` reads `info["language"]`, the default audio track the uploader declared,
as a bare lowercase code (`en-US` -> `en`), else None. It was right on all four videos tested (`ja`,
`ja`, `ja`, `en`), and it is the only field in the metadata worth reading: `automatic_captions`
lists some 157 languages for every video, because YouTube offers to machine-translate its own
transcript into all of them.

A video YouTube names as something other than `--language`, with the patience above 0, is refused
in `Fetcher.download_once()` before `process_ie_result`, so before one byte is downloaded.
`Fetcher.fetch()` catches `ForeignLanguage` and writes the state the audio detector would have
produced — `heard`, `language_paused`, `status: ready`, `duration` from `duration_hint` — so the
overlay says what it always said and the extension needs no change. Verified on a running server: a
TEDx talk refused one second after the sync, nothing written to the audio cache. It used to cost
the whole download, the whole decode and 60 seconds of forced Japanese out of English speech before
the patience ran out. A video whose audio is already cached never calls `download()` and so never
meets this test; the detector below still covers it.

A video YouTube names as `--language` never reaches the audio detector at all:
`Transcriber.process()` asks `watch_language()` only while `s.declared_language != args.language`.
That pass cost about 1.0 s a window, and turbo does not shrink it, because turbo shrinks the
decoder and detection is an encoder pass. The cost of trusting the declaration is that an English
stretch inside a video YouTube calls Japanese is now transcribed as Japanese for its whole length.

The audio detector stays, unchanged, as the fallback for a video YouTube names nothing for: when
`--language-patience` is above 0 (default 60) every window's speech-only
samples (`speech_samples()`, capped at 30 s) go through `model.detect_language()` before
transcription. `language_vote()` is the pure state machine over `Session.foreign_seconds`,
`heard` and `language_paused`: a foreign vote below the patience still transcribes, so one
misdetection never costs a subtitle; at the patience the session pauses, and every planned window
is then only listened to, until the target language is heard again.

What a paused session listens to goes into `Session.probed`, never into `covered`, and `probed`
is never written to the cache. That is the invariant that keeps a wrong pause cheap: `covered`
always means "Whisper has seen this", so a video paused by mistake can never cache as finished
and go permanently blank. `planned_ranges()` is the union the planners walk; hearing the target
language again empties `probed` and offers that audio back. `lookahead_for()` keeps the probes
within `LANGUAGE_PROBE_AHEAD` (90 s) of the playhead rather than `--lookahead`. A detector that
raises is never what silences a video: the window is transcribed unjudged. `--language-patience 0`
skips detection entirely, skips the metadata refusal with it and refuses to restore a pause from
the cache, so it really is the
cure the README offers. `/sync` and `/sessions` report `heard` and `language_paused`; the cache
stores them under `language_state`, discarded when `--language` changes. `retranscribe.py` sets
the patience to 0. `server/tests/test_language.py` covers the rules with a scripted model, and
`server/tests/test_declared_language.py` the metadata refusal.

Before the whole track is decoded (seconds for a long video), `Fetcher.make_preview()` decodes a
minute around the playhead into `Session.preview` (`(offset, samples)`) and marks the session
ready; `plan_window()` then only plans inside the preview and `audio_slice()` serves it. When the
playhead is within the first minute, a yt-dlp progress hook already runs that preview on the growing
`.part` file once enough bytes are in, so the first cues arrive while the download continues. The
full decode replaces it with `Session.audio` and clears the preview.

## Runtime data and the cue cache

The runtime-data invariant in full:

- Runtime data lives in `~/.shisu-ko` (`SHISUKO_HOME` overrides it): `venv/`, `models/`, `cache/`,
  `config.json` (`{"model": ..., "cookies_from_browser": ...}`; the browser is written by
  `--save-cookies-from-browser` / `--setup-cookies` and read by `resolve_default_cookies()`, see
  `docs/dev/server-runtime.md`; the model by `server.py --download-model NAME` at setup through
  `write_config()` (a merge; a None value drops its key): before the download for a size from
  faster-whisper's table, so that the choice outlives a failed or interrupted download and the
  first start fetches that model rather than the built-in default, after it for a repo id, which
  may be a typo or a PyTorch checkpoint; read by `parse_args()` (`resolve_default_model()`:
  `--model`, else the config's model, else `DEFAULT_MODEL` large-v3; `read_config()` never
  raises and ignores anything but a JSON object); Docker and Nix pass `--model` and never read
  it), plus what the Start
  button brought: `server-<port>.lock` (`hold_instance_lock()` /
  `try_lock()`, held from before the model load until the server exits), `server.log` (the POSIX
  `launch()` appends the launched server's output there) and, on Windows only, the host
  manifests `native-messaging/shisuko.json` (Firefox) and `native-messaging/shisuko-chrome.json`
  (Chrome) (`manifest_path()`; Linux and macOS keep them under the browsers' own directories). Cue caches are only reused when model (compared canonically) and language
  match. The loaded model's cues are `cache/<video_id>.cues.json`; when another model takes the
  file over, `save_cache()` first archives the old cues as `cache/<video_id>.<slug>.cues.json`
  (slug: the canonical model name with everything outside `[A-Za-z0-9._-]` replaced by `_`), and
  `load_cache()` brings them back from there after a switch back. `CACHE_FORMAT` is 6, bumped for
  the prompt-skip retry (0.13.0's caches hold windows whose skipped speech was covered blank); 5
  was 0.13.0's kanji/katakana seam rule; 4 was 0.12.0's cue geometry (sentence marks, the row boundary at
  one, the anomaly gate); every
  record of an older format is dropped whole by the check in `load_cache()`, the title kept, and
  the video transcribed again from the start. That check is the only way a geometry change reaches
  a video someone has already watched, so it runs before anything is read out of the record and
  there is no migration behind it. A record carries `"lyrics"`, the rule its covered ranges were
  made under (`--lyrics` as the server ran, see "How the server schedules work"), and from format
  4 on it always does. `--lyrics off` marks a sung stretch covered with nothing in it, so a record
  written with the switch off, read by a server running `--lyrics auto`, gets its blank stretches
  back (`unheard_stretches()`: the parts of `covered` that no speech interval and no cue touches,
  under two floors: 1.5 s, `plan_window()`'s own floor, at either end of a covered range, where a
  video's music sits, and `LYRICS_MIN_STRETCH_S` (4 s) between two heard things, `load_cache()`
  passing `inner_seconds=LYRICS_MIN_STRETCH_S`, since every pause of a talk is a hole of a second
  or two and a window per pause would fetch, walk and rewrite the record dozens of times over;
  taken out of `covered` with `subtract_intervals()`; the cues and the rest stay, and the session,
  no longer covered to its end, is fetched and planned again over them). That is not a migration
  and does not date: it is what makes the switch reversible.

## The `--lyrics` switch

Music videos: `--lyrics auto` (default) transcribes a window the speech detector hears next to
nothing in (under `LYRICS_MAX_SPEECH_S`, 1 s) without the detector when its audio is not silent
and the language head hears the target language in it; `--lyrics off` transcribes such windows
with the detector as before. `retranscribe.py` takes the same switch.
