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
began, so the next window re-transcribes it whole. A gap under 1.5 s gets no window: `plan_window()`
covers it as it is, and `pick_work()` saves the session then, or the record would stay short of fully
covered and every load would decode the audio again to find it (39 of 193 records here held only such
gaps). These functions are pure; test them by importing the
module (register it in `sys.modules` before `exec_module` because of `from __future__ import annotations`).

Cue building (`docs/subtitle-quality.md` is the rationale and the measurements): the server runs
Silero VAD itself on each window (`detect_speech()`, with `VAD_PARAMS`: min speech 250 ms, min
silence 300 ms) and passes the same options to faster-whisper.

Whisper is asked for punctuation rather than left to guess at it: `--initial-prompt` defaults to
`DEFAULT_PROMPTS[args.language]` (resolved in `parse_args()`; only `ja` has an entry, any other
language gets nothing, and an explicit `--initial-prompt ""` turns it off), a short punctuated
sentence in the style the subtitles should read. Each window is decoded with nothing in front of
it, so without the prompt the decoder has no reason to write a 。 at all: measured over 15 minutes
of video A (a long-form Japanese podcast episode), marks land on 88% of the hand-labelled sentence ends with the prompt and 53%
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
`initial_prompt=None`, everything else the same. `splice_segments()` then asks what the prompted
decode never said, as text: two decodes of one audio disagree on word timings by more than any
overlap test allows (one put というこ|と a few hundredths of a second before the other's
ということで、 began; a head `repair_lead_words()` moved sits seconds from where the other decode
put it), but they agree on the words. `unheard_runs()` aligns the two transcripts' covering lines
character by character (`difflib.SequenceMatcher`, spaces and punctuation aside); a word is heard
when any of its characters falls in a shared stretch of `PROMPT_ALIGN_MIN_CHARS` (2) or more, since
one shared kana is coincidence. Each run of unheard words whose midpoint lies in a skipped stretch
becomes a copy of its segment holding only those words, so a cue's words still join to its text.
The old rule refused any segment overlapping a kept one by 0.2 s, and with it the line the segment
opened with; on identical Whisper output the alignment brought back 早速作業していく…,
氷のもとを探せば…, そのポイントをひたすら探る練習をするのが必要になります and キーの上にはトラックパッド…
(where the prompted decode had written ご視聴ありがとうございました over the speech), and the seam echo
the tolerance let through (ああすごい。 || すごい、…) went. It drops a stretched prompted segment an
added piece overlaps, and `build_window_cues()` gets the combined list in time order. Its blind
spot is the window's edge: the prompted text it aligns against is this window's alone, so an
unprompted run opening on the previous window's last words is new to it. One retry at most; a
retry that adds nothing leaves the prompted result as it was. The log line gains
` [N s skipped with the prompt, K segments from a decode without it]`. Never on a lyrics window or
with an empty prompt. `dump_words.py` calls the same function, writes the spliced list and marks
the record `"prompt_retry": true`, so `replay_cues.py` builds on what the server built on.

Model text. Every word is cleaned as it comes onto the video's clock, before anything judges it.
Whisper's tokenizer has no token of its own for most of Unicode and spells such a character with
tokens for pieces of its UTF-8 bytes (a full-width ！ is 171 120 223, a token a byte), so the
decoder can also spell one no font has: at 3:27
and 3:39 of s1UVis0tPiE large-v3 wrote 171 253 255 (EF 9F AD), U+F7ED of the private use area,
three times, each where a sentence mark belongs (励ましたい U+F7ED 励まされている U+F7ED, 裏切り
U+F7ED いや、). Those bytes are valid UTF-8, so faster-whisper's word splitter, which only waits out
an unfinished character (U+FFFD), passed it on as a word, and the overlay showed Firefox's box
reading F7 ED; no version had filtered characters. `clean_model_text()` drops every character of a
`MODEL_TEXT_DROP` category, and U+FFFD: the private use areas (Co); code points with no character
(Cn: unassigned, by the running Python's Unicode tables, and the noncharacters); lone surrogates
(Cs, which `save_cache()` could not even encode); controls (Cc, boxes as well); the line and
paragraph separators (Zl, Zp); and the format controls (Cf). Cf was a choice: a zero-width space, a
byte order mark, a bidi override and the joiner of an emoji sequence draw no box, but they sit in
the text Yomitan scans and on the card, and an override reverses what follows it on screen. The
price is an emoji sequence shown as its parts, and no cue in the cache holds one. A `"\n"` is a row
break, which only the cue builder writes, at the seams of its merges (`merge_adjacent()`,
`merge_segments()`), so model text keeps none and a cached cue keeps its own (`keep_newline`). `absolute_words()` cleans every word a segment brings (through
`cleaned_words()`), whichever backend wrote it, and drops a word left with nothing, so the time it
spanned reaches the cue builder as the gap it was: on the 3:27 line `punctuate_words()` then
writes the mark where the U+F7ED stood (励ましたい。, on made-up timings in `test_model_text.py`).
The gates judge the segment's text cleaned the same way (`segment_text()`), so a sign-off with such
a character inside still meets the blocklist, and `splice_segments()` takes a piece's words from
`cleaned_words()`, the list `unheard_runs()` counts, not from `seg.words`. Cached cues are cleaned
when they load (see "Runtime data and the cue cache"). This machine's 317 cue records (23,714 cues)
held four such characters: those three, and a U+FFFD in a record of an old format, where a looping
decode ended inside a character (の地域の地域…の地 U+FFFD).

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
word-anomaly score, repetition loops, and a gated phrase blocklist. The anomaly score is upstream's
without its short-word term (`word_anomaly_score()`), on both paths: Whisper's Japanese words
are sub-tokens, usually one kana, so they fall under the 133 ms it penalises whatever the speaker did,
and the term measured the tokenizer rather than the audio. Over two 15-minute dumps the first-8-words
score reached the threshold for 37 of 279 segments and 34 of 202 with the term and for 0 and 2 without
it, and every segment the gate actually deleted was real speech - a 10 s block holding
一応、担任の先生とかいるの? … そうなんですよね on the live run, a 9.6 s block of 31 words, three shorter
lines - with no hallucination among them. The lyrics gate kept the term until it was measured too: over
the lyrics and rescued windows of a seven-video set (two decodes each) it fired 21 times, on 13 real
lines (a shout in a rescued stretch, キビトの変身!, whose stretch edge Silero's framing moved onto it in
some runs; a sung 一撃一撃一撃), 2 garbled fragments, and 6 inventions that the blocklist, the repetition
test or the stretched-word score refuse anyway.

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
Yomitan ends its sentence there, and the mining matcher's `_TERMINATORS` splits on it. A seam the gap alone
put there gives way to a plain join when it would leave a row under `MIN_PIECE_CHARS` or a third row
(`rows_fit()`), rather than the merge being refused; refusing leaves the stub alone on screen.

A sentence mark is the one seam that never gives way (`ends_sentence()`), in `merge_adjacent()` as
well as `merge_segments()`: what the speaker finished and what follows it never share a row, and a
merge that cannot give them a row each is refused instead of flattened. A short row is no reason
to flatten one: `rows_fit(text, limits, at_mark=True)` only refuses a third row, because うん。 is a
whole turn and not a stub we left by breaking badly, which is what `MIN_PIECE_CHARS` judges elsewhere. Measured on 15 minutes of
video A decoded with `--initial-prompt`, 22% of all cues held a mark mid-row and read as two
people on one line (`マジで?それいいね。`, `出そう、出そう、出そう。だって、集合に行くの誰?`), which
also put the other speaker's clause on every card mined from one. Hard boundary in both merges took
the share of hand-labelled sentence ends the viewer actually sees from 66% to 90% on that decode, and
from 71% to 82% on the unprompted one. `merge_adjacent()` seams only at a mark — a gap-based seam
there would refuse the stub merges the anti-flicker rule exists for — so pieces without a mark join
flat exactly as before. The cost is accepted: a short finished sentence (`まじ?`, `うん。`) now stands
as its own cue for its `min_seconds` instead of riding on a neighbour's row.

After a mark, only a piece that itself ends what it says joins as a row (`finishes()`): a mark of
its own, a pause of `pause_split` before the next cue, or nothing after it. An unfinished piece
opens the next sentence; riding on the row before, it left its continuation to start a cue without
it (`楽しいよ。 / オーナー跳びで遊んでみたくなったら、` then `うちで行ってね。`, and the card mined from the
second cue lost its head). Refused, it starts the next cue and the usual rules join it to what
follows. A Whisper segment's end is deliberately no evidence: segments are the decoder's cut and
run flush into each other (a real decode cut ほんとは into a segment of its own, then 葉っぱを… at the
same instant). The accepted cost is an unmarked finished line run into the next with no pause, which
now joins it flat (`住みたいかった待ってギャルと…`): two sentences on one row lose no text, a head
missing from the mined cue does.

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

Word timings. A cue built from timed words keeps them as `words`: `[{"start", "end", "text"}]`,
seconds on the video's clock. `build_cues()` takes them from the same group it takes the text from
(`cue_words()`), after `repair_lead_words()`, `punctuate_words()` and `carry_trailing_mark()` have
had their say, so the texts joined are the cue's text exactly. A merge concatenates the lists
(`join_words()`) and puts a seam's newline on the end of the first row's last word.
`settle_words()` runs last in `build_window_cues()`, after `normalise_gaps()`: it rounds the times
as the cue times are rounded, clamps them into the cue and keeps each start at or after the
previous word's end. A cue whose words no longer spell its text, or that merged with a cue holding
none, loses the field rather than carry a wrong one; `words` is absent, never empty. Every backend
gives word times today (faster-whisper, MLX, Kitsune), since a segment without words is gated as
`empty`. `/sync` sends `words` only to a body with `"words": true` (`without_words()` strips it
otherwise), so the extension's answer is byte for byte what it was. The field is optional in the
cache, so `CACHE_FORMAT` stays 6: a bump would throw away every watched video to add data nothing
displays, while a record written before the field existed loads unchanged and simply has no
`words` until its video is transcribed again.

Sung lyrics (P0.3 of the doc): singing over music is no speech to Silero, so `process()` decides
`wants_lyrics()` after the detector and the language watch: with `--lyrics auto` (default; `off`
is the switch), less than `LYRICS_MAX_SPEECH_S` (1 s) of detected speech in the window and an
`rms()` of at least `loudness_floor(Session.talk_rms)` (`LYRICS_MIN_RMS`, 0.02, on an ordinary
video; see below), and then, that holding, a target-language verdict on
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
word probability under `LYRICS_MIN_WORD_PROB` (0.35); "anomaly" (`is_segment_anomaly()`, always, improbable or stretched words);
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
slices the audio by the times the client sends); the log line gains ` [lyrics]`. A talk window can hide the same thing beside its speech: a song after the
line announcing it (a 歌枠's MC line), or a shriek Silero takes for noise. `wants_lyrics()` judges
the window whole, so the detector's decode used to cut both out, and a line whose onset the
detector caught late reached Whisper with its head clipped off. `Transcriber.choose_path()`
decides both paths before the decode (`process()` and `dump_words.py` call it): a window holding
`LYRICS_MAX_SPEECH_S` or more of detected speech has its `unsung_stretches()` (unheard,
`unheard_stretches()` of the window at `LYRICS_MIN_STRETCH_S`, 4 s, under both floors; loud, `rms()`
at least half the window's own detected-speech level, held between `LYRICS_QUIET_RMS`, 0.004, and
`LYRICS_MIN_RMS`) put to `sung_in_target()` together, their samples concatenated. Only a verdict for
the target language changes anything: the window is then decoded whole (`transcribe_options(...,
detector=False)`: `vad_filter=False`, the prompt kept), the stretches join `speech` for the
prompt-skip retry, the gates, the cue builder and `Session.speech`, and `vouched_segments()` drops a
segment lying mostly inside a stretch that `lyrics_reason()` refuses, since a stretch counted as
speech would excuse a sign-off made up over the music; its refusals count in the log's `[dropped …]`. A part of a stretch, 4 s or more, that runs to
the window's end and holds no cue goes back to the planner as a cut segment does: `new_end` moves to
its start, and the next window hears it from its own start, verdict carried (below). A part inside
the window stays covered. It used to be planned again alone, and a 4 s window padded to Whisper's
30 s is where it invents: a picture book's song tail came back as "おやすみなさい。" or "ご視聴あり
がとうございました。" in three runs of three, over audio the reference decode holds nothing in, while
none of nine such windows over two videos gave back a line the whole window had missed. A refused or silent stretch costs nothing beyond the head's pass, and the
window is decoded and covered as before; a window without such a stretch decodes exactly as before.
The floor is relative because a quietly mixed video's talk measures 0.016 and its missed lines 0.019,
under the old absolute 0.02, while ordinary talk measures 0.04 and up and keeps the old floor
(`loudness_floor()`); a talk window's own speech is its measure. Measured over the first ten
minutes of 40 cached videos (451 windows): 51 windows hold a stretch at the old floor, 55 at this
one, and the head accepts 18. A whole-song window has no speech
of its own, so its floor is `loudness_floor()` of `Session.talk_rms`, the loudest speech level any
talk window of the session measured (kept in memory, never cached): 0.02 until one has, and 0.02
for good once any talk reached 0.04, so one whispered window in a loud video lowers nothing. A CPU
scan of the cache's 284 videos (fixed 30 s windows) finds one window this opens, in a video whose
talk measures 0.007. A song also runs on past a window's end: a stretch starting inside audio the
head already vouched for (`Session.sung`, in memory: the stretches it accepted and the lyrics windows
it passed) needs no verdict. Asked alone, the head hears too little of a fragment to be sure: a
song's 12 s tail scored 0.62 and 0.66 and two lines were lost. Detected speech is not enough to
carry: a window the segment rule cut ends inside speech, and a stretch after it may be one the head
has just refused. Any other stretch of the window sends all of them to the head together, as before,
and the gates still judge every line; a lyrics window is always asked, since it has thirty seconds
to judge. A carried stretch is logged as `carries on the target language heard before it`.
A whole-window decode can write the narration after a sung stretch in kana (わたしはあかずきんです for
私は赤ずきんです): the music in the same encoder window shifts the decoder, since the same audio with the
song muted comes out in kanji, and neither the prompt nor `condition_on_previous_text` changes it.
Over the ten rescued windows of three videos the narration's kanji share is unchanged (0.234 on
main, 0.233), so it stays: decoding the narration through the detector instead (and filling the
stretches from a whole decode) brings back what this path fixes, since faster-whisper's VAD glues
the lines either side of a removed stretch into one (キビタの変身に向けて飛ばす技なんだけど).
The log line gains ` [N s the detector missed, decoded whole]` and
` [its last N s left to the next window]`. `App.cache_record(s)` is the record `save_cache()`
writes (`"lyrics"` and `"unheard_rule"` beside the cues, the covered ranges and the speech), and `retranscribe.py`'s
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

## Human subtitles

A track the video's creator uploaded is better text than Whisper's: the names, the kanji and the
sentence ends a person chose. Its timing is a person's too, loose by a few hundred milliseconds and
now and then by seconds. So a cue takes its text and its line from the track and its times from the
Whisper words that text aligns with. `--human-subtitles auto` (default) turns it on for `--language
ja`, the only language it was measured on (`human_subtitles_on()`); `off` builds as before and asks
YouTube for nothing more. A live stream never uses a track.

What counts as human. Only yt-dlp's `info["subtitles"]`, the creator's uploads; never
`automatic_captions`, `ja-orig` among them, punctuated or not: YouTube's recogniser writes
homophones Whisper does not and cuts its lines at a fixed length, mid-word. `pick_human_track()`
takes `ja` before `ja-JP` (any `ja-` tag but `-orig`), json3 before srv3 before vtt, and skips a
format YouTube labels as recognition (`kind=asr`, "auto-generated"). A creator can also upload the
recogniser's output as their own track, and `human_subtitle_lines()` refuses that too: more than
half its lines timed word by word (json3 `tOffsetMs`/`acAsrConf`, srv3 `<s t=…>`, vtt
`<00:00:01.234>`), or no punctuation mark and no space in 95% of 20 rows or more
(`unmarked_track()`). The second rule is the one that fired on a real upload: a lesson channel's
track with no mark anywhere and two rows of 8 seconds cut mid-word (行き / ましょう), which a person
never writes. A video YouTube names as another language gets no track (`read_human_track()`): a Japanese
track on it is a translation. The track is no evidence of the audio's language, for the same
reason, and `declared_language()` stays the only metadata the language watch reads.

The request. `Fetcher.start_human_check()` asks once per session, on a thread of its own, so the
audio never waits for it: from `download_once()` with the metadata it already holds (a copy, since
`process_ie_result()` goes on filling it), from `fetch_once()` for audio on disk with one
`extract_info(process=False)` of its own, and from `App.get_session()` for a record the cache covers
to its end, which no fetch would reach. It goes through a `YoutubeDL` built from `ytdlp_options()`,
so it sends the download's cookies. A failed request logs a warning and leaves the video to Whisper;
it is not repeated for that session (`Session.human_asked`). Cleaning (`clean_subtitle_text()`): the
model-text cleaning, then per row a speaker label (（田中）, 田中：, 【田中】, a >> or - turn mark), a
sound tag ([笑], （拍手）, ♪), furigana in brackets after its kanji (`is_ruby()`: hiragana, at most four
per kanji), and whitespace runs squeezed to one. The track's own row breaks stay `"\n"` rows: they are
where its maker put them, often a turn between two speakers, and the overlay, Yomitan and the mining matcher
read them as the cue builder's own.

Alignment, per window (`assist_cues()`, after `build_window_cues()` in `process()`). Every character
of the window's Whisper cues is folded (`fold_chars()`: NFKC, lower case, katakana as hiragana,
letters and digits only, ー and 〜 dropped) and timed as its share of its word. Each line the window
owns is aligned with the Whisper characters from `HUMAN_TIME_SLACK` (1.5 s) before it to
`line_reach()` plus that slack, after what earlier lines claimed: an edit distance with free ends
on the Whisper side (`align_line()`). `line_reach()` is the line's own end, or as far as its text
takes at `HUMAN_SLOWEST_CHARS_S` (4 characters a second), since one real track wrote three questions
over three seconds of an eight-second stretch. The score (`line_similarity()`) is difflib's ratio,
twice the matches over both lengths, with half credit for a kanji written in kana on the other side
(優しい / やさしい): alone that credit stays at 0.5, under the bar. At `HUMAN_MIN_SIMILARITY` (0.6) or
better the line becomes a cue holding its own text, timed from the Whisper characters it sits on,
snapped to the speech onset as `build_cues()` snaps, its end pulled out of the silence Whisper
stretches a last word over (`speech_end_before()`) and given the lead-out (`extend_end()`, the step
`build_cues()` now calls too). Its `words` are the line's own tokens, runs of one script with marks
attached (`human_words()`), each timed from its characters; a character Whisper never wrote shares
the gap between the neighbours it did. Their texts join to the cue's, as every cue's must. What
Whisper heard inside an aligned line's own span that no line claimed (a sentence said twice and
written once, a filler) goes with the line, whose cue stretches over it, rather than standing beside
it in Whisper's spelling. Whisper's words a line claimed go; the rest of a Whisper cue stays as a
cue of its own (`leftover_cue()`) unless under `HUMAN_MIN_LEFTOVER` (`MIN_PIECE_CHARS`) characters,
the line's fringe. A Whisper cue mostly under a line's goes, one at its edge gives way to it.

Where it falls back. A line scoring under 0.6 keeps Whisper's text for that stretch: on the
measured tracks those are English read under `--language ja` (Whisper writes katakana or a
translation), a line half of which the person left out, or a typo of theirs. Speech the track has no
line for keeps Whisper's cue. A line over audio Whisper wrote nothing for (music, a stretch the
detector missed, a window the prompt-skip retry could not fill) keeps the track's own times and no
`words`, unless Whisper wrote anything over it, or wrote this same line within `HUMAN_DISPLACED_S`
(15 s) of it (`echoes()`): then the person's timing slipped, which one track did by ten seconds,
and Whisper's cue stays. A line too long for the screen (over `max_lines` rows of `max_chars`) is cut at its own
sentence marks first (`split_long_line()`), its time shared by characters. The gates still judge
every Whisper segment; a line's text is never gated, since a person wrote it.

Windows. A line is owned by the window holding its middle (`lines_in_window()`, to `line_reach()`).
`human_window_end()` ends a window, as the segment rule does, where no line's speech runs across the
end: at the latest onset of detected speech in the window's last two thirds that no line spans,
less up to `HUMAN_CUT_LEAD` (0.3 s) of the silence before it, so a cue snapped a lead-in ahead of
the onset does not open inside this window. An onset and not the line's start: lines start after
their speech often enough that a cut at the line left the head of its speech here as a Whisper cue
beside the next window's line (a cue うん。 just before the next window's うん。\nそれで…; 14 of 35
window ends of a 14-minute run before the cut moved into the silence and echoes went). With no such onset the end stays; a Whisper cue that echoes() the line either
side the neighbouring windows own is dropped (`neighbour_lines()`). The log line gains
` [creator's subtitles: N lines aligned, K on their own times, M left to Whisper]`.

A track that arrives late. The first window usually decodes before the track lands, and a cached
record was built without one. `App.apply_human_track()` runs `assist_cues()` over the cues already
made, with the lines that lie wholly in covered audio, builds off the lock and swaps the result in
only if no window added a cue meanwhile, under a new session token: the client drops the ids it
holds and takes the list afresh. A list already carrying a line is never rebuilt.

The cache. `CACHE_FORMAT` stays 6. A record gains `"human_subs"`: `{"track": "<key>.<ext>:<sha1
prefix>", "lines": [[start, end, text], ...]}` when a track was used, `{"track": null}` when the
video was asked and has none, nothing when it was never asked. The lines are kept so a partly
covered video needs no second request, and a record asked once is not asked again. A line's cue
carries `"src": "human"`; Whisper's carry none. `/sync` strips `src` with `words`
(`OPT_IN_CUE_FIELDS`, `without_words()`): a client that does not ask for word timings sees exactly
what it saw before, byte for byte, on any video. A bump would have thrown away every watched video
to add a field nothing displays; instead an old record loads unchanged, is asked once, and with a
track found is rebuilt in place as above. `covered` still means "Whisper has seen this": a line
on its own times extends nothing. The rebuild is permanent like everything in the cache; deleting
the record, or `retranscribe.py`, gives Whisper's cues back, and `--human-subtitles off` only stops
new windows from using a track.

Measuring. `replay_cues.py --human TRACK` builds a `dump_words.py` file twice, Whisper alone and
with the track, and scores both against it (CER, sentence ends marked and seen, the share of cues
from each source, word sanity, the start shift); `--human-scope whole` puts the lines in as
`apply_human_track()` does, `--human-raw` skips the recognition test. Handed a cue record instead
(`retranscribe.py --human TRACK` drives the real `process()`, window cuts included), it scores the
record as it is. `dump_words.py` is untouched: a dump has no track, and `process()` without one
decides every window as before. Numbers: `docs/subtitle-quality.md`, (f). Tests:
`server/tests/test_human_subtitles.py`.

## Runtime data and the cue cache

The runtime-data invariant in full:

- Runtime data lives in `~/.shisu-ko` (`SHISUKO_HOME` overrides it): `venv/`, `models/`, `cache/`,
  `config.json` (`{"model": ..., "cookies_from_browser": ..., "engine": ...}`; the browser is
  written by `--save-cookies-from-browser` / `--setup-cookies` and read by
  `resolve_default_cookies()`, see `docs/dev/server-runtime.md`; the model by
  `server.py --download-model NAME` at setup through
  `write_config()` (a merge; a None value drops its key): before the download for a size from
  faster-whisper's table, so that the choice outlives a failed or interrupted download and the
  first start fetches that model rather than the built-in default, after it for a repo id, which
  may be a typo or a PyTorch checkpoint; read by `parse_args()` (`resolve_default_model()`:
  `--model`, else the config's model, else `DEFAULT_MODEL` large-v3; `read_config()` never
  raises and ignores anything but a JSON object); Docker and Nix pass `--model` and never read
  it), plus what the Start
  button brought: `server-<port>.lock` (`hold_instance_lock()` /
  `try_lock()`, held from before the model load until the server exits), `server.log` (the POSIX
  `launch()` appends the launched server's output there), `update-check.json` (`UpdateChecker`:
  the day's release check, `{checked, error, failures, latest}`; `docs/dev/updates-and-release.md`)
  and, on Windows only, the host
  manifests `native-messaging/shisuko.json` (Firefox) and `native-messaging/shisuko-chrome.json`
  (Chrome) (`manifest_path()`; Linux and macOS keep them under the browsers' own directories);
  and the experimental AMD engine's data: `config.json`'s `"engine"` (`"rocm"`, written only by a
  `server.py --probe-gpu` that passed, dropped by a probe and by `amd_setup.py` whenever it
  switches the engine off; `SHISUKO_ENGINE` decides over it), its side folder `rocm/` with the
  marker `shisuko-rocm.json`, `rocm-starts`, `next-model` and, while `amd_setup.py` installs,
  `rocm.new`, `rocm.old` and `cache/rocm-download` (`docs/dev/server-runtime.md`, "Where it
  lives"). Cue caches are only reused when model (compared canonically) and language
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
- A cue's text is cleaned when it loads. A record written before `clean_model_text()` holds what
  the model wrote (s1UVis0tPiE's carries U+F7ED three times, in two cues), so `load_cache()` puts
  every cue through `clean_cached_cue()`: the same cleaning with the row break kept, the text
  stripped as `word_text()` strips a fresh one, its `words` cleaned and cut to match by
  `cue_words()`, or dropped when they no longer spell the text, as `settle_words()` drops them. A
  cue left with nothing goes and the ids are renumbered as always; a cue with nothing to clean is
  the record's own dict, untouched. Nothing is written back from the load: the file changes when
  something saves the session (`save_cache()` writes the cleaned `s.cues`), and a record covered
  to its end may never be saved again, so its file keeps what the model wrote (which the cache
  tools then read) and every load cleans it again, for microseconds and a log line. Not a
  migration either, and no `CACHE_FORMAT` bump: a bump drops every record whole, which would
  throw away every cached transcript for three characters of one video.
- `"unheard_rule": 2` (`UNHEARD_RULE`) says the covered ranges were made by a server that puts a talk
  window's loud unheard stretches to the head (`unsung_stretches()`). Records without it are
  rechecked by `App.old_record_sweep()`, a generator the server's own `App` (`start_app()`, `sweep=True`;
  never the tools' or the tests') steps once each time the transcriber thread finds no work, so the
  model stays on that thread and a new sync waits for one step at most. `load_cache()` and the
  session path are untouched. Each record of the loaded model, `--language` and `CACHE_FORMAT`, not
  written with `--lyrics off` (that branch gives its stretches back already) or paused, gets its
  `unheard_ranges()`: every unheard stretch of `LYRICS_MIN_STRETCH_S` or more, widened by
  `RECHECK_CONTEXT_S` (4 s) into the talk around it with the speech heard there, since alone a quiet
  stretch would be a window with no speech, held to the absolute `LYRICS_MIN_RMS` a quietly mixed
  video's missed line does not reach. With none it is rewritten with the marker without reading
  audio. With some and no audio on disk it is left as it is (a recheck never downloads). Otherwise
  each range is read from disk (`_decode_range()`), one per step, and put to
  `Transcriber.choose_path()`, the decision `process()` makes, on a probe `Session` of the sweep's
  own (its `talk_rms` and `sung` never reach a live one), up to the first it would decode: then
  the record is deleted and the next open transcribes the video again from the disk audio; else it
  is rewritten with the marker (tmp file and `os.replace()`). A record whose video has a session by
  then is left to it: sessions load records under `App.lock`, which the write holds. Measured over
  this machine's 181 large-v3 Japanese records: 76 marked without audio, 99 read, 33 deleted; 243
  ranges, 184 head passes, 58 s of idle time once; a step takes 0.23 s at the median and 1.25 s at
  worst (a 234 s range of a 2.7-hour video); reading every record to skip it takes 0.17 s. A restart
  sweeps nothing: every record it would look at is marked.

## The `--lyrics` switch

Music videos: `--lyrics auto` (default) transcribes a window the speech detector hears next to
nothing in (under `LYRICS_MAX_SPEECH_S`, 1 s) without the detector when its audio is not silent
and the language head hears the target language in it; `--lyrics off` transcribes such windows
with the detector as before. `retranscribe.py` takes the same switch.
