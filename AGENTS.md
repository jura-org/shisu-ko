# AGENTS.md

Guidance for AI coding agents (and new contributors) working in this repository.
Read this before changing code; the README is the user-facing document.

## What this project is

Shisu-ko shows live Japanese subtitles on YouTube in Firefox and Chrome. A local Python server transcribes
the video's audio with Whisper (faster-whisper / CTranslate2, or MLX on the Apple GPU) a little
ahead of the playhead; the
extension renders the cues as real DOM text so Yomitan can scan them, and can mine a screenshot
plus sentence audio into the newest Anki card via AnkiConnect.

```
addon/        Firefox source extension, Manifest V3, plain JS; directly loadable without a build
              (match.js and words.js are shared by background.js and content.js; loaded before
              both, in that order)
server/       server.py (single file) + setup/run scripts + update.py; runtime data in ~/.shisu-ko
              native_host.py: the native-messaging host behind the popup's "Start server" button
              (stdlib only); native-host.cmd / native-host.sh wrap it, Firefox runs the wrapper
              mlx_beam.py: the beam search mlx-whisper lacks; server.py loads it by path on the
              Apple GPU and nowhere else
docker/       Windows wrappers for docker compose, WSL Docker Engine installer
Dockerfile, compose.yaml, compose.cpu.yaml, .env.example
flake.nix        Nix package/app/dev shell for the server and the extension build
sign-addon.cmd   signs the extension through addons.mozilla.org (needs the owner's API key)
publish-addon.cmd  submits a version to the public AMO listing; docs/amo/ holds the listing text and assets
```

## Invariants (do not break these)

- Subtitles must stay ordinary DOM text (`textContent`), never canvas, never `<track>` cues,
  never shadow DOM. Yomitan and other popup dictionaries depend on it. The one markup allowed
  inside a cue's text is the word colours' inline `<span class="shisuko-word">` holding a text
  node, with `data-status` and `data-pitch` and nothing else (`renderText()` in `content.js`
  is the one writer, on the subtitle and in the transcript alike); no other element, attribute
  or wrapper goes into a line.
- Never use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval` or `script.src` in the content
  script. youtube.com enforces Trusted Types; only `textContent`/`createElement` style DOM code works.
- Settings are untrusted input before they reach CSS: every style value goes through a sanitiser
  in `applyStyleSettings()` (`clampNumber`, `oneOf`, `hexColor`, `fontStack` with
  `FONT_FAMILY_RE`, which admits only letters, digits, spaces, dots, hyphens and underscores). A
  font family that fails the rule falls back to the preset; no quote, semicolon or `url(` reaches
  the stylesheet. `popup.js` keeps copies of `FONT_FAMILY_RE`, the preset stacks and
  `MODEL_NAME_RE`; `addon/tests/popup-copies.test.js` keeps them equal to the originals.
- Application code uses the `browser.*` promise API. Firefox provides it natively; the shared
  `browser-api.js` adapter supplies it on Chrome, where its internal bridge necessarily calls
  `chrome.*` callbacks. Application code must not call `chrome.*` directly.
- All overlay classes and flags use the `shisuko-` / `__shisuko` prefix.
- Settings defaults live once in `addon/settings.js` (`SHISUKO_DEFAULT_SETTINGS`), loaded before
  `background.js`, `content.js` and `popup.js`. To add a setting, add it there and add the popup
  input with the same id; `addon/tests/settings.test.js` enforces both.
- The server listens on `127.0.0.1:8790`. Port 8765 belongs to AnkiConnect; never use it.
- The server never exposes anything beyond `/health`, `/sync`, `/clip`, `/sessions` and
  `POST /update`; it binds to localhost, validates `video_id` against `^[A-Za-z0-9_-]{6,20}$`,
  and answers browser requests only from the extension's own origin or from pages on loopback
  hosts (`origin_allowed()`), so arbitrary websites cannot drive downloads and transcription.
  `/update` is narrower (`update_origin_allowed()`: the extension's origin or no `Origin` header
  at all, never a page, since ending the server and making the launcher run git and pip is a
  capability only the popup has a use for) and answers 409 `{ok: false, error}` unless the
  server was started by `run.cmd` / `run.sh` (`SHISUKO_LAUNCHER=1`) without `--no-update` and
  without `SHISUKO_NO_UPDATE` (`App.update_blocker()`); otherwise it answers
  `{ok: true, restarting: true, version}` and the process exits with `EXIT_UPDATE` (4). The
  server never spawns `update.py` and never downloads a release itself; the launcher does.
- The add-on never installs itself: no `update_url` in the manifest, no `.xpi` download or
  install (`addon/tests/settings.test.js` enforces the manifest); its updates come from
  addons.mozilla.org. Its one remote request is the anonymous `GET` of
  `https://api.github.com/repos/Multysquid/shisu-ko/releases/latest` in `fetchLatestRelease()`,
  never with a token, cookie or identifier, and there is no second remote endpoint;
  `notifications` stays a required permission (the start-up check notifies with no popup open
  to ask for a grant). The privacy policy, description and reviewer notes in `docs/amo/` state
  exactly this, so a change here changes them too.
- The native host (`server/native_host.py`, name `shisuko`) answers only `status` and `start`.
  It never takes a path, a program or an argument from a message: the only thing it can run is
  the checkout's own `server/run.cmd` / `server/run.sh` (root = the parent of the folder the
  host file lives in), and it registers only under the user's own profile
  (`HKCU\Software\Mozilla\NativeMessagingHosts`, `~/.mozilla/native-messaging-hosts`,
  `~/Library/Application Support/Mozilla/NativeMessagingHosts`), never system-wide.
  `nativeMessaging` stays in `optional_permissions`, requested by the popup's click handler
  before its first `await` and by nothing else; the button is not a setting.
- A model name from a client (`model` in `/sync`) must match `MODEL_NAME_RE`
  (`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?$`) and contain no `..`;
  anything else is answered with `MODEL_NAME_HINT` and never stored. A valid name is reduced to
  its canonical alias (`canonical_model_name()`: `large`, `Systran/faster-whisper-large-v3`,
  `mlx-community/whisper-large-v3-mlx` and `large-v3` are one model) and resolved through
  `download_model_files()` — faster-whisper's own download, or the MLX repo's on the Apple GPU —
  before it is loaded. A raw client string must never reach `WhisperModel()`, which also opens local
  directories; only the operator's `--model` may be a folder, and it skips the download.
- A transcription backend is `transcribe()` and `detect_language()`, and nothing else in the server
  may know which one is loaded. `--device` names it, `resolve_device()` is the single place `auto`
  becomes one of `cuda`, `mlx` and `cpu`, and `canonical_model_name()` gives one set of weights one
  name whatever backend holds them, so neither the cue cache nor the popup splits by backend. The
  built-in `--model` is the backend's, not the machine's alone: `default_model_for()` answers
  `MLX_DEFAULT_MODEL` (large-v3-turbo) on the Apple GPU and `DEFAULT_MODEL` (large-v3) elsewhere,
  and a model chosen at setup outranks both. See "How the Apple GPU works".
- A video's language is settled before its audio, not after. `declared_language(info)` reads
  YouTube's own `language` field; a video it names as something other than `--language` is refused
  in `Fetcher.download_once()` before a byte is downloaded, and one it names as `--language` never
  reaches the audio detector. The detector is the fallback for a video YouTube names nothing for,
  and nothing else changed about it. A refusal writes the same session state the detector writes
  and nothing to the cache. `--language-patience 0` must switch all of it off, the metadata refusal
  included. See "How the server schedules work".
- `enabled` in the settings is the master switch (the header toggle in the popup, Alt+Shift+S).
  Off must mean nothing happens on YouTube pages: no `/sync`, no overlay, no native-caption
  hiding, no arrow-key handling, no Anki polling, no mining (the cues outlive the switch, so
  Alt+Shift+M would still find one), no `cardStatus` asks for the word colours' deck index
  (`wordColoursOn()` in `content.js` includes `enabled`, and the poll runs from `syncTick()`),
  so no request reaches Anki from a YouTube tab. Only the toggle command itself keeps working:
  the command listener in `content.js` returns for every other command while `enabled` is false.
- The overlay lives in the page's DOM, where any script on youtube.com can dispatch events on
  it, so its handlers (`onMineClick`, `onTranscriptClick`, `onSubtitleEnter`, `onSubtitleLeave`,
  the transcript's close button) act only on trusted events (`ev.isTrusted`): a synthetic click
  must not write a card, save a file, seek or pause. The keyboard commands arrive through the
  browser, not the page. Text from the server is written into Anki's HTML fields only through
  `escapeHtml()` in `background.js`; the overlay shows it through `textContent`.
- Live streams run on the stream's media clock, `getProgressState().current` of YouTube's player
  (read through `wrappedJSObject`, Firefox only), never on `video.currentTime`, which restarts at
  an arbitrary point on every page load. Every place the content script reads or seeks the
  playhead goes through `playhead()` / `seekPlayhead()`.
- Runtime data lives in `~/.shisu-ko` (`SHISUKO_HOME` overrides it): `venv/`, `models/`, `cache/`,
  `config.json` (`{"model": ...}`, written by `server.py --download-model NAME` at setup through
  `write_config()` (a merge; a None value drops its key): before the download for a size from
  faster-whisper's table, so that the choice outlives a failed or interrupted download and the
  first start fetches that model rather than the built-in default, after it for a repo id, which
  may be a typo or a PyTorch checkpoint; read by `parse_args()` (`resolve_default_model()`:
  `--model`, else the config's model, else `default_model_for(args.device)`; `read_config()` never
  raises and ignores anything but a JSON object); Docker and Nix pass `--model` and never read
  it), plus what the Start
  button brought: `server-<port>.lock` (`hold_instance_lock()` /
  `try_lock()`, held from before the model load until the server exits), `server.log` (the POSIX
  `launch()` appends the launched server's output there) and, on Windows only, the host manifest
  `native-messaging/shisuko.json` (`manifest_path()`; Linux and macOS keep it under Mozilla's
  own directories). Cue caches are only reused when model (compared canonically) and language
  match. The loaded model's cues are `cache/<video_id>.cues.json`; when another model takes the
  file over, `save_cache()` first archives the old cues as `cache/<video_id>.<slug>.cues.json`
  (slug: the canonical model name with everything outside `[A-Za-z0-9._-]` replaced by `_`), and
  `load_cache()` brings them back from there after a switch back. A record carries `"lyrics"`,
  the rule its covered ranges were made under (`--lyrics` as the server ran, see "How the server
  schedules work"): a 0.11.2 server (format 3 without the key), or `--lyrics off`, marked a sung
  stretch covered with nothing in it, so `load_cache()` under `--lyrics auto` gives a record
  without `"lyrics": "auto"` its blank stretches back (`unheard_stretches()`: the parts of
  `covered` that no speech interval and no cue touches, under two floors: 1.5 s, `plan_window()`'s
  own floor, at either end of a covered range, where a video's music sits, and
  `LYRICS_MIN_STRETCH_S` (4 s) between two heard things, `load_cache()` passing
  `inner_seconds=LYRICS_MIN_STRETCH_S`, since every pause of a talk is a hole of a second or two
  and a window per pause would fetch, walk and rewrite the record dozens of times over; taken
  out of `covered` with `subtract_intervals()`; the cues and the rest stay, and the session, no
  longer covered to its end, is fetched and planned again over them). `CACHE_FORMAT` is 3
  (bumped by 0.11.2 for the cue geometry, see "How the server schedules work") and the format
  check in `load_cache()` runs before this migration, so it reaches only a format-3 record
  without the key: one written by 0.11.2, or by this version under `--lyrics off`. A format-2
  record, from 0.11.0 or 0.11.1, is dropped whole by the format check and the video is
  transcribed again from the start, which brings it under the lyrics rule as well.
- No absolute personal paths, no secrets and no `.env` in tracked files. `.env` is machine-specific
  and ignored; `.env.example` documents it.
- Line endings: LF everywhere, CRLF only for `*.cmd` (`.gitattributes` enforces this).

## How the server schedules work

`plan_window()` in `server/server.py` decides what to transcribe next: if the playhead is not
inside a covered range, a short `--first-window` (20 s) starts at the playhead; otherwise the next
`--window` (40 s) continues from the end of the covered range, up to `--lookahead` seconds ahead.
A segment touching the end of a window is dropped and the covered range ends where that segment
began, so the next window re-transcribes it whole. These functions are pure; test them by importing the
module (register it in `sys.modules` before `exec_module` because of `from __future__ import annotations`).

Cue building (`docs/subtitle-quality.md` is the rationale and the measurements): the server runs
Silero VAD itself on each window (`detect_speech()`, with `VAD_PARAMS`: min speech 250 ms, min
silence 300 ms) and passes the same options to faster-whisper.

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

Segments go through gates before becoming cues: no words, VAD overlap under 0.5, faster-whisper's own
word-anomaly score, repetition loops, and a gated phrase blocklist.

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
word that follows it. `may_break()` is asked about a prospective line, not one Whisper word:
Whisper's words are sub-tokens, often one character, so `group_words()` gathers just enough of what
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
Yomitan ends its sentence there, and `match.js`'s `TERMINATORS` splits on it. A seam that would leave
a row under `MIN_PIECE_CHARS`, or a third row, gives way to a plain join rather than the merge being
refused; refusing leaves the stub alone on screen.

Every cue still carries `seg`, the id of the Whisper segment it came from, and a merge re-stamps
every cue carrying a swallowed segment's id. Mining does not read it: a segment is a run of speech,
not a sentence, and rejoining its cues put clauses on a card that were never on screen (see "What a
mined card gets"). `seg` stays for `cue_stats.py`, which measures a change per segment, and a stale
id would mis-group it. Cue caches are format 3; older caches are ignored, which is the only way a
geometry change reaches a video someone has already watched. `server/tools/cue_stats.py` and
`retranscribe.py` measure a cache before and after a change, and `dump_words.py` + `replay_cues.py`
compare two cue builders on identical Whisper output; keep them working (`retranscribe.py` wraps
`build_window_cues()`, so its wrapper takes every argument, `lyrics` included; `dump_words.py`
decides every window as `process()` does, `wants_lyrics()` and then `sung_in_target()` on an idle
`App` like `retranscribe.py`'s, decodes a sung one without the detector and writes `"lyrics": true`
into its record, its own `--lyrics off` sending every window through the detector; `replay_cues.py`
passes a record's `lyrics` through, building on `lyrics_spans()` and the lyrics gates). Both tools
build their model with `load_model()`, never `WhisperModel()` directly, so the words they dump come
from the backend the server would have used on that machine.

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
`server/tests/test_lyrics.py` (a fake model recording what `transcribe()` was asked for and a
patched `detect_speech`, like `test_language.py`) covers `rms()` on silence and a tone, the
decision (`wants_lyrics()`: no speech and loud, quiet, speech heard, `--lyrics off`), every gate
of `lyrics_reason()` with scripted segments (a confident line passes; empty, unsure on each of
the three scores, a repetition loop, a sign-off phrase, an anomaly rejected), `lyrics_spans()`
(the padded runs, a segment cut at a pause, a stranded first word starting no run) and the cues
`build_window_cues()` builds on a lyrics window (snapped to the segments, the lead-out into the
gap, the `seg` ids; a breath between two sung lines keeping them apart, a pause inside a segment
splitting the lines, a stranded first word slid onto its line), the four paths through
`process()` with the recorded kwargs, a rejected line taking no segment id and no span, the log
text, a lyrics window casting no language vote, the song after the MC line, the one before it
and a bridge between two lines planned again rather than covered blank (`unsung_stretches()`;
a short or quiet stretch staying covered, a window the head refused covered whole), the head's
verdict (another language, an unsure head, the target language, a song starting in the last
seconds of a long window, a raising head, asked whatever the patience, not asked for talk,
silence or `--lyrics off`), a live stream, `subtract_intervals()`, `unheard_stretches()` (and
its inner floor), `load_cache()` on a record from before the rule (a music video offered again,
a talk video keeping its cues and giving back its blank stretch, a talk record giving back its
long holes and its ends but not its pauses, a format-2 record dropped by the format check and
not migrated, a record under the rule untouched, one written with `--lyrics off` offered again
under `auto`, `--lyrics off` loading an old record untouched), `save_cache()` writing the rule
and its own record reloading as covered, `parse_args(["--lyrics", "off"])`, `retranscribe.py`'s
own `--lyrics` and its record of the cache shape, `replay_cues.py` taking a lyrics window
through the lyrics gates, and `dump_words.py` deciding the lyrics path per window (with
`faster_whisper` faked in `sys.modules`) and marking the record.

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

A video YouTube names as `--language` never reaches the audio detector at all: `Transcriber.process()`
asks `watch_language()` only while `s.declared_language != args.language`. That pass cost about
1.0 s a window, and turbo does not shrink it, because turbo shrinks the decoder and detection is an
encoder pass. The cost of trusting the declaration is that an English stretch inside a video
YouTube calls Japanese is now transcribed as Japanese for its whole length.

The audio detector stays, unchanged, as the fallback for a video YouTube names nothing for: every
window's speech-only samples (`speech_samples()`, capped at 30 s) go through
`model.detect_language()` before transcription. `language_vote()` is the pure state machine over
`Session.foreign_seconds`, `heard` and `language_paused`: a foreign vote below the patience still
transcribes, so one misdetection never costs a subtitle; at the patience the session pauses, and
every planned window is then only listened to, until the target language is heard again.

What a paused session listens to goes into `Session.probed`, never into `covered`, and `probed`
is never written to the cache. That is the invariant that keeps a wrong pause cheap: `covered`
always means "Whisper has seen this", so a video paused by mistake can never cache as finished
and go permanently blank. `planned_ranges()` is the union the planners walk; hearing the target
language again empties `probed` and offers that audio back. `lookahead_for()` keeps the probes
within `LANGUAGE_PROBE_AHEAD` (90 s) of the playhead rather than `--lookahead`. A detector that
raises is never what silences a video: the window is transcribed unjudged. `--language-patience 0`
skips detection entirely, skips the metadata refusal with it and refuses to restore a pause from
the cache, so it really is the cure the README offers. `/sync` and `/sessions` report `heard` and `language_paused`; the cache
stores them under `language_state`, discarded when `--language` changes. `retranscribe.py` sets
the patience to 0. `server/tests/test_language.py` covers the rules with a scripted model.

Before the whole track is decoded (seconds for a long video), `Fetcher.make_preview()` decodes a
minute around the playhead into `Session.preview` (`(offset, samples)`) and marks the session
ready; `plan_window()` then only plans inside the preview and `audio_slice()` serves it. When the
playhead is within the first minute, a yt-dlp progress hook already runs that preview on the growing
`.part` file once enough bytes are in, so the first cues arrive while the download continues. The
full decode replaces it with `Session.audio` and clears the preview.

## How live streams work

yt-dlp reports `is_live`; `Fetcher.download()` then returns None and `Fetcher.follow_live()` runs
`LiveFollower` on the fetch thread instead of downloading. `DashLiveSource` asks yt-dlp (with
`live_from_start`) for the audio format's base URL and fetches `…&sq=N` segments: self-contained
fMP4 whose timestamps are the stream's media clock, verified to be the same clock as the player's
`getProgressState().current` (a DASH segment cross-correlates at 1.0 with the HLS audio at the
`PROGRAM-DATE-TIME` position, and the player's `ingestionTime` matches within ~0.2 s). The live
head comes from the `X-Head-Seqnum` response header; an expired URL (403) is refreshed once a
minute at most. The follower starts one segment before the playhead (`place_cursor()`), runs
forward to the head, waits for new segments, jumps after a seek, pauses while no client has
synced for `--client-timeout`, and exits after `--idle-minutes` (status `evicted`, refetched on
the next sync). Decoded audio lives in `Session.live_audio` (`LiveAudio`, 16 kHz chunks on the
stream clock, trimmed to `LIVE_KEEP_BEHIND` seconds behind the playhead); `audio_slice()`,
`plan_window()` (`plan_live_window()`: a window at the live edge waits until `LIVE_MIN_WINDOW`
seconds are there instead of being marked covered) and `/clip` read it. Live sessions are never
written to the cue cache; when the stream ends and comes back as a video, `Fetcher.fetch()` drops
the live cues and changes the session token so the client starts over on the video's clock.
`server/tests/test_live.py` drives the follower with a fake source and clock.

## How model switching works

The popup's `model` setting names the Whisper model the server should run; `--model` is only the
default. The content script sends it with every `/sync` (`modelForSync()`, trimmed, empty for the
default), and `App.request_model()` stores the wish: an empty name becomes the operator's
`--model` (that is `--model`, else the model chosen at setup in `config.json`, else this machine's
built-in default: large-v3-turbo on the Apple GPU, large-v3 elsewhere, resolved once in
`parse_args()`; not validated, it may be a folder), an invalid name is not
stored (`model_state()` answers that request with `MODEL_NAME_HINT`), a valid one is stored as
its canonical alias (`canonical_model_name()`, built lazily from `faster_whisper.utils._MODELS`:
alias -> repo id -> first alias, so `large` and `Systran/faster-whisper-large-v3` are
`large-v3`), and a name that failed less than `--retry-after` seconds ago is ignored
(`in_cooldown()`), since the client re-sends the setting every second.

`Transcriber.run()` calls `App.switch_model_if_wanted()` before every window, so the swap never
runs while a window is being transcribed. It is a small state machine over `wanted_model`,
`model_name`, `model_preparing`, `model_prepared`, `model_loading` and `model_error`, all under
`App.lock`:

1. Nothing wanted (`wanted == model_name`): drop leftover prepared files, clear `model_loading`.
2. Wanted but nothing in flight: start `prepare_model(wanted)` on a daemon thread
   (`prepare_thread`), set `model_preparing = model_loading = wanted`, keep transcribing with the
   old model. `prepare_model()` runs `download_model_files(wanted, self.device)`:
   `faster_whisper.download_model()` into `MODELS_DIR` plus a `model.bin` check, so a PyTorch
   checkpoint is refused before `WhisperModel()` sees it (the operator's `--model` folder skips the
   download; on the Apple GPU the MLX weights are fetched instead, see below). Nothing here
   touches the GPU, so a typo, a missing repo or an offline hub costs only a failed download:
   `model_error = (name, friendly_model_error())`, `model_failed_at`, `wanted_model` reset to the
   loaded model. There is no retry without a new request.
3. A download still running: keep transcribing, report the wanted name as loading (a change of
   mind cannot cancel a download; the new name waits behind it and stale files are dropped).
4. Files prepared for the wanted name: `self.model = None; gc.collect()` first, because on a GPU
   whose memory is mostly held by other programs two models rarely fit side by side, then
   `load_model(args, wanted, path=dir)`. Success: `model_name = wanted`, error cleared,
   `restart_sessions()`. Failure: `model_error`, `wanted_model = previous`, `reload_model(previous)`;
   if even that fails the server has no model left and calls `os._exit(3)` so the launcher
   restarts it on `--model`.

`restart_session()` clears cues, covered ranges, speech and `seg_next`, gives the session a new
token (the client drops everything on a token change, `dropCues()` in `content.js`), sets a
session without audio back to `pending` so `get_session()` fetches it again (the old model's
cache may have marked it covered without ever downloading), and calls `load_cache()` for the new
model. `/health` reports `model` (loaded, canonical), `default_model`, `model_loading`,
`model_error` as `{model, error, names}` (`names` from `model_spellings()`: every alias and the
repo id of the failed model, so the popup can match whatever spelling the viewer typed) and
`models` (`downloaded_models()`, the `models--owner--name` folders under canonical names).
`/sync` adds `model`, `model_loading` and `model_error`, the last judged for the name that
request carried and null for any other. `friendly_model_error()` turns huggingface_hub's
exceptions into one line: unknown size, not found on Hugging Face, could not reach Hugging Face,
no `model.bin`, else the last line of the message cut to 200 characters.

On the client, `content.js` keeps `state.modelLoading` / `state.modelError` from each answer,
shows `Loading model X… (a first use downloads it)` or `Shisu-ko: model X: <error>` (an error is
shown even with progress messages off, both parts capped by `truncate()`), and the storage
listener clears the verdict and syncs at once when the model setting changes. `popup.js` polls
`/health` every two seconds while open and in view (the same page is the options page, and a
hidden tab polls nothing until it is shown again): the badge says "Loading model" during a
switch, and
`modelErrorFor()` puts the server's verdict under the field only when the field's value (or the
default while empty) is one of the failed model's spellings.

Tests: `server/tests/test_model_switch.py` fakes `faster_whisper` in `sys.modules` (a
`download_model()` over a temp directory and a slice of `_MODELS`), stands the transcriber thread
down and calls `switch_model_if_wanted()` by hand (`tick()` joins the real prepare thread, a
blocking `Event` variant looks at the server mid-download); it covers names and aliases, the
prepare and swap, every failure path and the cooldown, the per-model cache files and both
endpoints; `server/tests/test_mlx.py` drives the same switch on the Apple GPU.
`addon/tests/content.test.js` covers the model name in `/sync`, the restart on a new
session token, the status texts and `fontStack()`; `addon/tests/popup-copies.test.js` keeps the
popup's copies of `FONT_FAMILY_RE`, the preset stacks and `MODEL_NAME_RE` equal to the originals
and the model hint in step with the `/health` shape. `server/tests/test_setup_model.py` covers
`config.json`, the `--model` default, `--download-model` (with `faster_whisper` and
`huggingface_hub` faked in `sys.modules`; the interrupt through a replaced `wait_for_thread()`
and `os._exit()`), `run_check()` against stand-ins for `ctranslate2`, `yt_dlp` and `winreg` (no
GPU driver and no registry in the suite) and the text of `setup.cmd` / `setup.sh`.

## How the Apple GPU works

CTranslate2 has no Metal backend, so on Apple Silicon faster-whisper decodes on the CPU. MLX runs
the same Whisper weights on the GPU and leaves the cores to the video that is playing. Hence
`--device auto` prefers it to the CPU. Measured on an M1 Pro over 180 s of Japanese news audio,
identical windows and identical cue pipeline, nothing else running: faster-whisper large-v3 on
`--device cpu` (int8, beam size 5) at 2.3x realtime, MLX large-v3 (float16) at 5.0x with the same
beam and 5.9x greedy.

Measure with a browser playing a video or the number describes nothing, because the GPU's memory is
the browser's memory. One 38 s window of Japanese conversation, M1 Pro, Chrome playing one YouTube
video:

| model | weights | idle | with Chrome playing |
|---|---|---|---|
| large-v3 | 2.9 GB | 13.0 s | 81.0 s (0.47x realtime) |
| large-v3-turbo | 1.5 GB | 4.8 s | 4.8 s (7.9x realtime) |

0.47x is below playback speed, so large-v3 on a Mac never catches the viewer up. The cause is
memory, not GPU contention: compressed memory rose from 1.9 GB to 14.8 GB between the two large-v3
runs, while Chrome works the GPU exactly as hard during the turbo runs, which cost 0.99x of their
idle time. Turbo has 4 decoder layers against large-v3's 32. It pays one word for them: over that
window both models write the same 22 lines, differing in `釣りあたり` where large-v3 heard
`次あたり`.

`MLX_DEFAULT_MODEL` (`large-v3-turbo`) therefore stands beside `DEFAULT_MODEL` (`large-v3`), and
`default_model_for(device="auto")` chooses between them through `resolve_device()`.
`resolve_default_model()` calls it, and a model chosen at setup still wins over the backend's
default: the viewer who asked for large-v3 gets large-v3, on the Apple GPU too. `run_check(device)`
reports whichever is built in, and `server.py --default-model` prints that name alone and exits,
ignoring `config.json`, so `setup.sh` names its first menu choice by asking rather than by keeping
a copy of the rule (`setup.cmd` is unchanged: there is no MLX on Windows). `retranscribe.py`'s
`--model` follows the same default.

`resolve_device()` is the single place `auto` is decided: `cuda` when there is an NVIDIA GPU, else
`mlx` when `mlx_available()` (darwin, `mlx.core`, `mlx_whisper`, `mx.metal.is_available()`), else
`cpu`. `--device mlx` names it outright. Everything downstream reads the resolved name, so a third
backend is a branch here plus a class, not a change in the transcriber.

A backend is two methods. `transcribe(audio, **options)` returns `(segments, info)`, each segment
carrying `start`, `end`, `text` and `words` of `word` / `start` / `end` / `probability`;
`detect_language(audio=...)` returns `(language, probability, every probability)`.
`MlxWhisperModel` presents exactly those over mlx-whisper (`MlxSegment`, `MlxInfo` and the file's
own `Word`), so the transcriber, the cue gates and the language watch never learn which backend
ran.

One option does not survive the crossing, and the wrapper absorbs it rather than the call sites.
mlx-whisper has no `vad_filter`, so given one the wrapper runs the Silero pass faster-whisper
would have run, hands the decoder the speech with the silence cut out (`collect_chunks`) and maps
every timestamp back (`SpeechTimestampsMap`) — the same `VAD_PARAMS`, so the gates in the cue
builder keep judging what they were written for. `--beam-size` is served, by the decoder
`mlx_beam.py` puts into the library: `MlxWhisperModel.transcribe()` passes the size on only when
the patch took and the caller asked for more than 1, and mlx-whisper, like faster-whisper, drops
it above temperature 0 by itself, so the beam runs on the first pass alone.

`canonical_model_name()` is what keeps one name per set of weights across backends. `MLX_REPOS`
maps a faster-whisper size to the mlx-community repo holding it converted and `MLX_ALIASES`
reverses that, so `large`, `large-v3`, `Systran/faster-whisper-large-v3` and
`mlx-community/whisper-large-v3-mlx` all canonicalise to `large-v3`. One name means one cue cache,
so a machine's CPU cues and its GPU cues are the same file, and one name in the popup whichever
backend is loaded. `model_spellings()` appends the MLX repo, so the popup still matches whatever
the viewer typed.

`download_model_files(name, device)` branches on the device: CTranslate2 files for `cuda` and
`cpu`, `download_mlx_model_files()` for `mlx` (`snapshot_download()` into the same `MODELS_DIR`,
refusing a repo without `config.json` beside `weights.safetensors` / `weights.npz`, the way a
missing `model.bin` refuses a PyTorch checkpoint). `App.prepare_model()` passes `self.device`, so
the state machine above is unchanged and runs on the Apple GPU as documented: nothing in the
download touches the GPU, so it still runs beside the working model, and a size nobody converted
fails as a failed download rather than a crash. `--compute-type` on MLX is `float16` (the default)
or `float32`; anything else is ignored with a warning instead of being reported as loaded, because
`/health` must not name a precision the GPU never ran.

`server/requirements.txt` installs `mlx-whisper` only under `sys_platform == "darwin" and
platform_machine == "arm64"`. faster-whisper stays required everywhere: the MLX path still uses its
Silero VAD, its audio decoding and its alias table. `run_check()` prints `MLX <version>: Metal
available` on macOS and `Backend for --device auto: <device>` on every platform.
`server/tests/test_mlx.py` fakes `mlx.core`, `mlx_whisper`, `huggingface_hub` and `faster_whisper`
in `sys.modules`, so the device choice, the names, the download, the option translation, the VAD
emulation, `detect_language()` and a switch on the Apple GPU are covered without a Mac.

### The beam search (`server/mlx_beam.py`)

mlx-whisper has everything a beam needs and no decoder to use it: `DecodingTask.n_group` sizes the
batch, `MaximumLikelihoodRanker` makes the length-penalised final choice,
`Inference.rearrange_kv_cache()` reorders the cache when the beams are shuffled, and the decoder
itself is a `NotImplementedError`. `mlx_beam.py` fills that hole with a port of openai-whisper's
`BeamSearchDecoder` (MIT) onto MLX, implementing the library's own `TokenDecoder` contract
(`reset` / `update` / `finalize`). It earns its keep in Japanese because greedy decoding commits to
a homophone before the words that would disambiguate it arrive: `敬老` comes out `経老`.

Two things differ from the PyTorch original. `update()` returns `sum_logprobs` rather than mutating
it, since an MLX array is a value. And it returns `completed` as an `mx.array`, because
`_main_loop` hands that straight to `mx.async_eval` beside the tensors. Only each beam's best
`beam_size + 1` continuations cross back from the GPU (`mx.argpartition`, then
`mx.take_along_axis`): no more than that can survive the cut below, and the whole row is the
51 866-wide vocabulary, which costs more to copy than the search it feeds.

`rearrange_self_attention_only()` replaces `Inference.rearrange_kv_cache`, and it is what makes the
beam affordable. The library's version reorders both halves of every layer's `(self_kv, cross_kv)`.
Cross-attention keys and values are computed from the audio, not from the tokens, so all five beams
of one audio hold identical copies of them: permuting those rows changes nothing and, on large-v3,
copies about 1.2 GB per decoded token. Skipping it is exact, because a candidate never comes from
another audio's beam group. Measured: that one change took beam search from 1.1x realtime to 4.1x.

`enable_beam_search()` wraps `DecodingTask.__init__` instead of rewriting it: it runs the original
with `beam_size` and `patience` stripped out, because the original raises on a beam size it cannot
serve, then puts `self.options` back (what `DecodingResult` reports), sets `self.n_group = beam` and
installs the decoder. Carrying the group size through as `best_of` would have been the smaller
patch and does not work: `_verify_options()` refuses `best_of` at temperature 0, the only
temperature beam search runs at. It is idempotent, and it stands aside when `decoding` already has
a `BeamSearchDecoder`, so a future mlx-whisper that grows its own keeps it — that one knows the
library's internals better than a patch does.

`enable_mlx_beam_search()` in `server.py` loads the file by path, the way `run_check()` loads
`native_host.py`, and caches whether it took, so `server.py` stays the one file and a machine
without MLX never reads it. A failure warns and answers False, and the wrapper decodes greedily,
which is what it did before the decoder existed: a patch that cannot be applied must cost accuracy,
never the backend.

The beam does not buy parity with the CPU. In those 180 s it fixes two of greedy decoding's three
slips (`経老` becomes `敬老`, the wrong figure `0.5%に上昇` becomes `0.2ポイント上昇`) and leaves
one (`線上降水帯` where the CPU writes `線状降水帯`).

What the beam costs depends on the audio, and one figure for it is a lie. It is about 15% on the
read news speech above and about 90% on dense conversation: 15.1 s against 8.0 s greedy over the
38 s window. The cost is per decoder step, and a conversational window holds roughly three times as
many segments as a news one. The temperature fallback is the obvious suspect and it is innocent:
pinning `temperature=[0.0]` left that window at 15.1 s against 17.0 s.

`server/tests/test_mlx_beam.py` needs neither a Mac nor MLX: the decoder's `update()` and
`finalize()` against scripted logits where greedy takes the locally better token and loses, the
cache reordering that follows the beams through the self-attention half and leaves the
cross-attention half untouched, the wrapped constructor with a beam and without one, its
idempotence and its retreat before a library that brought its own decoder, and `server.py` falling
back to greedy when the patch cannot be applied. Its `mlx.core` is numpy wearing MLX's names, which
is only safe as long as it does not drift from the library: every decoder test therefore runs a
second time against the installed `mlx.core` when there is one, and
`test_the_library_still_contracts_what_the_fakes_copy` pins the parts of `mlx_whisper.decoding` the
patch reaches into, so an upstream change that moves them fails here rather than on a Mac.

## One tab at a time

Only one YouTube tab's `/sync` reaches the server. `background.js` elects it in `electSyncTab()`
(pure, tested in `addon/tests/tabs.test.js`). Two clocks: `HOLD_TIMEOUT_MS` (12 s, two missed
idle heartbeats) and `FOCUS_STALE_MS` (7 s: more than one 5 s idle heartbeat, so a paused focused
tab does not flap between two of them, and well under the hold timeout, so a focused tab whose
switch was turned off or that left for the home page hands the right on in one heartbeat). The
rules, in order: the tab the viewer is watching wins if it is the one asking; else the asker takes
it when nobody holds it or the holder has been quiet for `HOLD_TIMEOUT_MS`; else a holder that is
the watched tab keeps it, playing or paused, as long as it synced within `FOCUS_STALE_MS`; else a
playing asker beats a holder that is paused or that has been quiet longer than `FOCUS_STALE_MS`,
watched or not; else the holder keeps it. The election never names a tab that did not ask — a
focused tab claims the right on its own next tick — because parking it on a tab that has stopped
asking blanks every other tab until the entry ages out.

Which tab is being watched comes from `tabs.onActivated` plus `windows.onFocusChanged`, and the
focus listener must ask `tabs.query({active: true, windowId})` itself: browsers fire
`onActivated` only when the selection inside a window changes, so a window whose tab was never
re-selected would otherwise stay unknown and its video starve in standby.

A refused tab gets `{status: "standby"}` from the background without a server call. The content
script treats that as "this tab is not the one talking to the server" and nothing more: it leaves
`offline`, the server status, the cues and `since` untouched, so a tab that stands by and comes
back keeps its subtitles, and it stops pre-mining and polling Anki for new cards, which is what
kept `/clip` reaching the server from a tab the election had refused (the word colours'
`cardStatus` ask keeps going: the index is the deck's, served from the background's one cache,
and never reaches the server, see "How word colours work"). Every other answer clears the flag, a
refusal by the server (`{ok: false, error, data}`, a 4xx/5xx) or unreachable included: the
background answers standby only with `ok: true`, so any other answer means the election let the
request through, and a flag left by the last refused tick would hide the server's error behind
"running in another tab" and keep the pre-mining and the Anki watch closed. Such a refusal also
drops the language verdict (`languagePaused`, `heard`), as a restarted session does: the answer
says nothing about the session, `statusText()` ranks the pause above the error, and the next
real answer brings the verdict back. An ad does not hand the right on: `/sync` keeps going out
through it (see "Gotchas"), so the watched tab keeps asking and keeps the right for the ad's
length. `statusText()` in `content.js` (pure, tested) shows standby and the language pause even
with `showStatus` off, since they are the only answer to "why is nothing appearing?"; neither is
styled as an error. In the `ready` case it shows `No speech found in this video` only when the
covered range the playhead sits in runs from the start of the video (`view.coveredFrom <= 0.5`:
the first window opens half a second before the playhead, and a video resumed near its end gets
one window from there on and nothing before it, the server never planning backwards) to its end
(`ahead >= view.duration - 1`, the done reading) with `view.duration > 0` and
`view.cueCount === 0` (a silent clip, an instrumental), never for `view.live`, whose cues keep
coming; `updateStatus()` passes `cueCount: state.cues.length`, `coveredFrom` (the range's start,
null outside every range) and `live: state.live || state.serverLive` in the view, the latter
read from the `/sync` answer's `live` key (the player's own API says live only in Firefox, and
not through an ad; an older server sends no key and the flag stays as it was). It is a progress
message like "Transcribing…", not an error, and obeys `showStatus`;
`addon/tests/content.test.js` covers it beside the other `statusText()` cases.

## How automatic mining works

`ankiPoll(tabId)` in `addon/background.js` watches AnkiConnect so the viewer never presses
anything: the content script asks once per sync tick (`ankiPollAllowed()`: `autoMine` on with
`mineTarget` `anki`, which is `autoAnkiMining()`; a visible tab, no ad, not already mining, not
standing by for another tab (`state.standby`, see "One tab at a time"), and not idle, meaning
two minutes after a plain pause (`PAUSE_POLL_IDLE_MS`) or, during a hover pause, two minutes
after the pointer was last seen over the subtitle or the player, `hoverSeenAt`, after which a
hover pause still polls at a slow heartbeat, `PAUSE_POLL_SLOW_MS` (5 s), since a viewer reading
the popup, whose pointer the page cannot see, must not lose the card they make to a gap the
background no longer trusts its baseline across) and the background answers
with the id of a note Yomitan has just created, plus what that note says (`notesInfo`: its
sentence field and its word field, `ankiWordField` or the field with `order` 0).
Four rules keep it from touching the wrong card.

- Baseline. Every poll remembers the highest `findNotes("added:1")` id. It reports nothing when
  that baseline cannot be trusted: first poll, previous poll failed, or more than 10 s since the
  previous successful one. Notes added while Anki was closed or no video was open stay untouched.
- One at a time. Two or more ids above the baseline mean an import or a sync, not a lookup, so
  the baseline moves and nothing is reported.
- Sentence guard. `addToAnki()` with an explicit note id scores the note's sentence field
  (`ankiSentenceField`, else `Sentence`) against the spoken sentence and against the cue text with
  `SHISUKO_MATCH.similarity`; below `MIN_SIMILARITY` on both it returns `{ mismatch: true }` and
  writes nothing. It is the same scoring the content script used to pick the cue, so the guard can
  no longer refuse what the matcher accepted.
- No downloads fallback. `mineCue` with `auto: true` never falls back to the Downloads folder: a
  failure the viewer did not ask for must not scatter files.

Right after a successful `updateNoteFields`, `addToAnki()` calls `rememberDeck(url, noteId)`
without awaiting it (see "How word colours work"): the mine's answer never waits for it, and its
failure is a `console.debug` line.

Polls are throttled to one request per 250 ms (several tabs poll the same background), and the
`requestPermission` handshake is retried at most once a minute after the viewer clicked No:
`ankiPermission(url)` shares one request in flight (`ankiWatch.permissionPending`), so every
caller arriving while Anki's dialog is up (the poll, a tab's `cardStatus` ask, the popup's
`ankiDecks`) awaits the same answer rather than queueing a dialog of its own, and it sets
`permissionAskAt` a minute ahead (`ANKI_PERMISSION_RECHECK_MS`) before the request, so an
answered request, granted or denied, is not repeated within the minute. A request Anki never
answered (closed, not installed) is no refusal: it puts `permissionAskAt` back to 0, rejects for
every waiting caller and is recorded in `ankiWatch.permissionFailed = {at, err}`; `ankiPoll()`,
a timer, rethrows that failure for `ANKI_PERMISSION_RETRY_MS` (5 s) instead of knocking again
(the poll answers `offline: true` meanwhile), so Anki not running is not a minute of "denied"
and a started one is noticed within seconds, while a tab's `cardStatus` ask and the popup's
`ankiDecks` (a viewer's own action) ask at once, and any answered request clears the failure
for the poll. Poll errors are logged with `console.debug`, never toasted. A `notesInfo` that
fails still reports the id, with `note: null`, and the content script falls back to the cue at
the playhead.

The baseline is shared between tabs, so a note found by one tab's poll goes on a ledger
(`ankiWatch.reports`, kept for `ANKI_REPORT_WINDOW_MS`, 60 s) and `replayReport()` answers it,
with `replayed: true`, to every other tab that polls within the window, once per tab, before the
throttle and without a request of its own; each tab matches the card's sentence against its own
lines (`matchCue`), and the mine that writes into the note takes it off the ledger
(`forgetReport()` marks the entry `written`; it stays until the window drops it). While a mine
for a note runs (`mineCue()` keeps its promise in the entry's `mine`), the note is handed to no
other tab, a second mine for it waits for the first and writes only when that one wrote nothing
(else it answers `{ok: true, warning: true}` and "attached in another tab"), and a mine that
wrote nothing (a mismatch, no clip) gives the note back: the finder is mid-mine for seconds
when the other tab's tick lands, and two writes would leave the card with the later tab's frame
and clip. Only a note with a sentence of at least `ANKI_REPORT_MIN_CHARS` (6) normalised
characters goes on the ledger: one with a word alone, one `notesInfo` could not read, or one
whose few-character sentence is in the lines of every video, goes to the tab that found it, as
before.

Every request on the mining path has a deadline, because the content script holds
`state.mining` until the `mine` message resolves: `fetchClip()` aborts each attempt after
`CLIP_TIMEOUT_MS` (30 s, body read included; "Whisper server timed out"), `addToAnki()` and
`storeMedia()` give AnkiConnect `ANKI_REQUEST_TIMEOUT_MS` (30 s) and its `requestPermission`
`ANKI_PERMISSION_TIMEOUT_MS` (60 s, the dialog), an abort reading "AnkiConnect did not answer in
time". The watcher's own `requestPermission` stays untimed: its poll resolves when the viewer
answers the dialog. Whatever is written into a note's HTML fields goes through `escapeHtml()`:
the sentence that fills an empty sentence field, and every text part `extendSentenceField()`
puts around its own `<b>`.

### What a mined card gets

The cue that was on screen, and nothing more. `sentenceForCue(cue)` in `content.js` is the one place
that decides it; it returns `{start, end, text, cueIds: [cue.id]}`, and `clipParams()` in
`background.js` takes the audio bounds from that, so the sentence field and the clip always describe
the same span the viewer read and heard.

It used to rejoin every cue sharing a `seg` within 1.5 s, on the premise that a Whisper segment is a
sentence. It is not, and there is no second signal to fall back on. Whisper punctuates a fluent
narrator barely at all (7 marks in 3095 characters on one measured video), so every split inside such
a segment came from `--max-cue-chars` and the rejoin undid all of them: a card mined off a 3 s line
got 9 s of audio and a clause that was never displayed. The VAD is a worse witness, not a better one
— Silero cuts at 300 ms of silence, a breath, and that narrator ran 14.26 s across three cues between
breaths, so deferring to it would have made the card longer again. `extendSentenceField()` still
grows Yomitan's fragment when Yomitan cut inside the cue, but never past the cue.

A merged cue can hold two utterances either side of the newline `seam_for()` put between them, and
Yomitan ends its sentence at that newline, so the grow stops at the row the fragment came from.
`normalize()` strips whitespace, so the seam is invisible to the comparison and
`extendSentenceField()` has to split on it itself; without that it put the other speaker's line on
the card, which is the one thing the seam exists to prevent. A cue without a seam is one row and
behaves as it always did. `escapeHtml()` writes the newline as `<br>`, since HTML would otherwise
collapse it into a space and the card would lose the boundary entirely.

### Matching a card to its subtitle (`addon/match.js`)

`SHISUKO_MATCH` is a plain script loaded after `settings.js` and, with `words.js` behind it,
before the two scripts that use it, in both `background.scripts` and `content_scripts[0].js`.

- `normalize(text)` removes ruby readings first (`RUBY`: an `<rt>` or `<rp>` up to the next end
  tag; Yomitan's `{sentence-furigana}` and `{furigana}` write `<ruby>食<rt>た</rt></ruby>べる`,
  which stripping the tags alone would leave as 食たべる), then strips HTML tags, decodes
  `&nbsp; &amp; &lt; &gt; &quot; &#39;`, removes bracket furigana (`{sentence-furigana-plain}`
  writes ` 食[た]べる`), all whitespace and punctuation.
- `similarity(card, spoken)` takes the card's text first, and the order matters. It is 1 when
  the spoken text contains the card's sentence whole, however short (`contains()`: the server
  merges a short cue into its neighbour, 嘘でしょ。本当にそんなことがあったの, and Yomitan stops at
  the 。, so the card reads 嘘でしょ, four characters only that line explains), or when the card's
  sentence contains the spoken text and that text is a real share of it (`comparable()`: at
  least 6 characters, or half of the longer one, so a three-character cue, ですね, inside a long
  card sentence is not a match); otherwise the Dice coefficient of their character-bigram sets,
  0 unless comparable; `MIN_SIMILARITY` is 0.6. Dice, not the overlap coefficient: dividing by
  the smaller set alone passes two sentences that merely end the same way (別の字幕です against
  これはテスト字幕です scores 0.6), and containment already covers a card whose sentence is a real
  fragment of the cue.
- `matchCue(cues, note, opts)` picks the cue a card belongs to: cues scoring below the threshold
  against the note's sentence are out, a cue containing the note's word gets +0.2, and ties break
  on `share()`, then on `opts.rank(cue)` (the content script ranks pre-mined sentences, the one
  read last first) and then on distance from `opts.t`, the playhead. `share()` is 1 for a cue
  that runs past the sentence at a point where Yomitan cuts (`cutFrom()`: the sentence is a whole
  piece of the cue's raw text between 。！？!?．… or a newline, or of such a piece between quotes,
  「」『』 and the straight ones; Yomitan never cuts at 、), else the shorter text's length over
  the longer one's (`coverage()`), so a whole line beats a fragment of itself and a line that is
  the sentence beats one holding it mid-clause. A card with no sentence matches on the word alone; a card
  with neither is nobody's, and `autoMine` falls back to `currentCueForMining()`.
- It stays O(n): containment is tried on every cue first and bigrams only if nothing contained.

### Pre-mined sentences

Nothing is captured when the card appears; it was captured while the line played. 400 ms after a
cue becomes active (`schedulePremine` in `content.js`), the content script reads the frame off the
video and sends `premine` to the background, and asks for the *next* sentence's clip as well
(`ahead: true`), so a lookup on either is already paid for. The frame is read only while
`autoAnkiMining()` holds (`autoMine` on and `mineTarget` `anki`): a mine the viewer asks for takes
the frame on screen, so otherwise the `premine` carries no image and only the clip is prepared.
Hovering a line sends the frame again with `hover: true`: that is the frame the viewer was
actually looking at, and it pins the sentence; the same sentence at the same paused position is
not read back again (`hoverShot`). Every frame is drawn onto one shared canvas (`drawFrame()`),
let go after a tainted read. Premine and `premineReset` messages leave one at a time
(`premineTurn()`), and an answer from before `state.cueGeneration` changed (`dropCues()` bumps
it: a new video, a new session token) is not taken as the held list, since the new cues' ids
start at 0 again and would count as prepared with the old video's material.

The store is `premined`, a `Map` in `background.js` keyed by tab, video and sentence
(`cueIds[0]`). Entries hold the base64 frame, the clip and the promise fetching it
(`fetchClip(..., attempts = 1)`: nobody is waiting, and a failure simply leaves `audio` null for
the next attempt). Caps: 5 sentences per tab, 10 in all, oldest-touched unpinned first; a pinned
entry goes only when nothing else is left. Oversize payloads (3 MB image, 5 MB audio) are dropped.
`premineReset`, `tabs.onRemoved` and a new video or server session clear a tab. The inventory
answered to the tab (`heldFor()`) is ordered by `seen`, stamped by every message without `ahead`,
then by `touched`: a sentence only prepared ahead of its turn ranks behind the one on screen.
Nothing is ever written to disk, and nothing survives a restart of the event page.

`mineCue` then uses it: an `imageDataUrl` sent with the message wins, else the held frame; the
held clip is used when its parameters still match the ones computed now (same start, end and
format to the millisecond), else the clip is fetched with the usual four tries and stored. Mining
does not remove an entry: two words from one line make two cards.

## How word colours work

Two opt-in colourings of the words of a line, both off by default: `cardStatus` colours a word by
the state of its Anki card (`data-status`: `learned` green, `learning` yellow, `suspended`
orange, `new` red; colours as custom properties on `.shisuko-root` in `content.css`), `pitchAccent`
draws an overline in the colour of its pitch accent pattern (`data-pitch`: `heiban` blue,
`atamadaka` red, `nakadaka` orange, `odaka` green). `cardStatusDeck` names the deck (empty is
automatic: the deck the last mined card went to; nothing mined and nothing chosen means no deck,
so a collection is never searched by guesswork), `ankiPitchField` the note field holding the
pitch (empty: found by name). `addon/words.js` (`SHISUKO_WORDS`, a plain frozen object like
`SHISUKO_MATCH`, loaded between `match.js` and the two scripts in both `background.scripts` and
`content_scripts[0].js`, and by `service-worker.js`) is shared: the background turns one deck's
notes into `[word, status, pitch]` entries, the content script builds an index from them and
marks the words of every line. Everything in words.js is pure, without DOM.

### words.js

- Fields. `plainText(html)` strips `<rt>`/`<rp>` with their content, turns `<br>` and the end of
  `p`/`div`/`li`/`tr` into line breaks, strips every other tag (`TAGS` = `/<[^<>]*>/`: a tag never
  runs across a `<`, so an unclosed `<` costs its length, not its square), decodes the six
  entities of `match.js` and collapses whitespace. `plainWord(html)` takes the first non-empty
  line, without bracket furigana (` 食[た]べる` -> `食べる`). `readingOf(html)` is the `<rt>` texts
  of a ruby, the brackets of the furigana or the field itself, and `""` unless what is left is
  kana (ー and ・ allowed). Bounds, since a field is third-party content: `stripMarkup()` and
  `parsePitch()` read at most `MAX_FIELD_HTML_LEN` (16,000) characters of the raw value,
  `plainWord()` at most `MAX_WORD_FIELD_LEN` (320) of a line (the index drops words over
  `MAX_WORD_LEN`, 40), `readingOf()` answers `""` for a raw field over `MAX_READING_FIELD_LEN`
  (1,000). `moraCount(kana)` counts every kana and ー, not the small ゃゅょぁぃぅぇぉヮゎ.
- `parsePitch(text, reading, word)` -> category or null, in this order: a category name in the
  plain text, the first by position (`heiban|平板|atamadaka|頭高|nakadaka|中高|odaka|尾高`,
  case-insensitive: Yomitan's `{pitch-accent-categories}`; its `kifuku` for verbs and adjectives
  is no category, so no overbar rather than a wrong one); else a position `n`: the `{pitch-accents}`
  markup (`drawnPitch()`: one `display:inline-block` span per mora holding a `border-color:` line
  span, the drop after the mora whose line has `border-right-width`; the first `<li>` of a list
  counts; a nasal mora's extra inline-block span is not a mora, the line spans are counted), else
  the first `[n]` of the plain text (`{pitch-accent-positions}`, a list's first), else a plain
  text of digits alone, else `ꜜ` in the plain text (n = the moras before it), else a raw value
  without any tag that is kana-only (n = 0); else null. Then 0 -> heiban, 1 -> atamadaka, else
  `n === m` -> odaka, otherwise nakadaka, with `m` the drawn mora count, else `moraCount()` of the
  pitch text without ꜜ when kana-only, else of `reading`, else of `word`, else unknown (nakadaka).
- `pitchOf(fields, settings)`: `fields` is `notesInfo`'s `{name: {value, order}}`. The candidates
  are the field `ankiPitchField` names (trimmed, when present), else every field whose name
  matches `/pitch|accent|アクセント/i` in `order`; the first whose value `parsePitch()` reads
  decides (a `{pitch-accent-graphs}` SVG before a position field does not hide it). The reading is
  the lowest-order field other than the candidate matching `/reading|furigana|読み|よみ/i` and not
  `/sentence|文/i` (`SentenceFurigana` holds the sentence's kana, whose mora count would make every
  odaka word nakadaka), through `readingOf()`; the word is `ankiWordField`, else order 0, through
  `plainWord()`. Null without a candidate or a readable value.
- `statusOf(sets, noteId)` over the sets of the five searches below: not in `unsuspended` ->
  `suspended` when in `suspended`, else null (not in the deck); in `new` -> `new`; in `learning`
  -> `learning`; in `review` -> `learned`; else `learning` (an Anki before 2.1.44, or a set a
  search left out). `mergeStatus(a, b)` for two notes of one word: the least progress wins
  (`new` < `learning` < `learned`), `suspended` only when both are, null the identity.
- `buildIndex(entries)`: words trimmed; empty, over `MAX_WORD_LEN` or in `PARTICLES` dropped
  (case, binding, adverbial, conjunctive and sentence-final particles, Yomitan's fusions such as
  のは, への, かも, and the copula and auxiliaries a learner mines: だ, です, ます, ない, たい, ん,
  じゃ, もん …: a card for one would paint every line); duplicates merged (`mergeStatus`, the
  first non-null pitch). Returns a frozen `{size, exact, stems, heads, maxLen, maxStemLen}`:
  `exact` Map word -> `{word, status, pitch, bounded}` (`bounded` for a word without kanji or
  katakana, which must end at a word boundary in every form, else ある is found in あるいは);
  `stems` Map stem -> `[{entry, kind}]` from `stemOf()`: a stem of at least one character for a
  word holding a kanji or katakana, of at least two for a kana-only word (かける -> かけ, しまう
  -> しま, おいしい -> おいし; いう, する, くる, みる, いい stay exact: a stem of one kana would be
  found in every line): `する` beyond the stem -> kind `suru` (こする, with two kana to spare, is
  a る verb); else a last `い` -> `i-adj`, `る` -> `ru` (ichidan or godan, unknown), one of
  うくぐすつぬぶむ -> that kana; anything else has no stem, and neither has a kana-only word
  whose stem is itself in `PARTICLES` (から of からい, なら of ならう, しか of しかる): it matches
  exactly only, since からかった would else be からい's かっ + た (the kanji twin 辛い keeps its
  stem). `heads` is the set of first characters, so a position whose character starts no word
  costs nothing.
- `wordStarts(text)`: the indices where a word may begin, `Intl.Segmenter("ja", {granularity:
  "word"})` segment starts (the instance cached) plus 0; every index without a segmenter or on
  any error; it never throws. ICU keeps a compound in one segment (日本語, あるいは, 見せかけ).
- `markWords(text, index, starts)` -> runs `[{text, status, pitch}]` covering the text in order,
  unmatched characters joined into one run with nulls. Left to right; only a position in `starts`
  (an iterable, `wordStarts(text)` by default) whose character is in `heads` is tried; after a
  match `i` jumps to its end (no overlaps). The boundaries it goes by are `boundsOf(text,
  starts)`: `starts` plus the index after a て or で at a start that ICU fused with the く of the
  auxiliary behind it (`TE_FORM`: 食|べ|てく|れ|た, 書|い|てく|れ|た, かけ|てく|れ|た; not
  読|んで|く|れ|た, which it cuts), where くれる begins and かけて ends; `starts` itself is never
  written (the content script keeps it per cue), the set is copied when there is something to
  add. `matchAt()` takes the longest span, the exact word on
  a tie, and a kana-only (`bounded`) exact word over a form of itself that adds particles alone
  (`particlesOnly()`: a walk over `particleLens()` from the word's end to the form's), so a kana
  noun ending in a verb's kana (いくつ, きょう, けっこう, ふつう, ほんとう, whose stem is in the
  tables) ends before its copula and carries no です in its pitch overbar (いくつ|です|か, the
  copula a particle of its own with the status), a kana verb's んだ / でしょう being coloured by
  the particle chain instead (わかる|ん|だ, おいしい|です), while a form that adds more than
  particles still wins (わかりました) and a kanji word keeps the form (食べるでしょう is one
  run): exact words longest first (`bounded` ones must end at a boundary or the end of the text,
  the others anywhere `endsWord()` admits: the end, a boundary, or not right before a kanji,
  katakana or ー, so 関 is not coloured in 関係, 飲み not in 飲み物, while 見た ends before 犬 and
  電話 before 番号); then every stem length from `maxStemLen` down, `continuationEnd()` giving the
  furthest end `endsWord()` admits, or, for a `bounded` entry, one at a boundary or where
  `boundedEnd()` admits: inside the segment ICU made of the form's ending and the particle
  after it (わか|っ|たよ, かけ|たよ, でき|ますよ, ちがい|ますよ: the segment holding the end must
  begin after the form's start and what remains of it must be a particle, which then takes the
  colour), while the exact bounded word, having no ending to be fused, still ends at a boundary
  only (ことば, あいだ, はなし and こんにちは with こと, あい, はな and こんにち in the deck stay
  plain, and so does はし in ICU's は|しか). ICU cuts a kana run it does not know into single
  kana (い|れ|ば, かけ|ら, し|まっ|た), so かけて in 話し|かけ|て|いただく, しまった and わかった are
  found and いれば is not いれる (ば is no first piece). Its rules:
  - The bare stem counts for `suru` (勉強 in 勉強が; a bounded `suru` entry keeps it at a boundary
    too, びっくり in びっくり|だ) and for `ru` when the stem ends in an i-row or e-row kana
    (`IE_ROW`) at a boundary (the ichidan 連用形 is the noun: 食べ in 食べに行く, 助け, 考え, 流れ),
    for `ru` never for a `bounded` entry (かけ in かけ|ら, いれ in い|れ|ば would be nouns at every
    cut); a stem ending in a kanji (走, 見) or the a-row (当た, 変わ: a godan verb, whose noun is
    its り piece, found through the tables) is no form, and nothing ends inside a compound ICU
    holds together (見せ in 見せかけ, 当た in 当たり前).
  - Otherwise a first piece from `FIRST_PIECES[kind]` must follow (`suru`: する し さ せ す すれ;
    `i-adj`: い く かっ けれ さ そう くて くない ければ; `ru`: る た て ない … られ させ よう れば ろ よ
    ず ん ら り れ っ なかっ なけれ, the ichidan stem being the 連用形; the godan rows わいうえおっ,
    かきくけこいっ, がぎぐげごい, さしすせそ, たちつてとっ, なにぬねのん, ばびぶべぼん, まみむめもん),
    so 走 in 走者 is not 走る. 行く's い is skipped (`text[pos - 1] === "行"`): its 音便 is っ alone,
    and 行い, 行います, 行いたい are 行う's; and っ is admitted after 行 alone, since every other く
    verb takes い (歩いた, 書いた), so its っ is another word's: あるって is ある and the quotative
    って, not あるく's, and はたらって, 書って are no forms.
  - `tailEnds()` then consumes up to `MAX_TAILS` (5) pieces of `TAIL_PIECES` (た て で だ ない …
    ます まし ませ ん たい … れる られる せる させる ば う よう ろ る い けれ ず ちゃ じゃ てる でる てい
    でい いる いた いて います いない ましょ でし でしょ です たら だら たり だり ても でも ながら なさい
    まい とく どく いか いき いく いけ いこ いっ いただく いただき いただけ いただい いただこ いただか っ
    ー), every split tried (泳いでいる is で + いる, not でい + る), each piece checked against the
    one before it by `firstRole()` (a godan first piece by its row `a`/`i`/`u`/`e`/`o` or `onbin`,
    a す verb's し as `shi`, the adjective's い as `adj`, a する verb's さ/せ/す as `suru:さ` etc.,
    a る verb's っ as `onbin`, any other piece by its text): `AFTER` lists what a single-kana tail
    and the いる/いく/いただく pieces may follow (だ after ん and not る: 食べるんだ, 食べる + だけ; た
    after 音便, まし, て and the ichidan-like stems, not after い, so た after いる's stem is the
    tail いた; う after the o-row, よ, ろ, ましょ …; よう only after し and the ichidan-like pieces,
    since after る, た, ない, the u-row or an adjective it is 様: 食べる + ように; ん after the forms
    it shortens and not ちゃ: 食べてちゃ + んと); `OPEN_TAILS` are pieces a span never ends right
    after (い, てい, でい, いか, いこ, いっ, いただい …, the a-row and o-row, ら, the 音便 kana,
    suru:さ/せ/す, かっ, なかっ, たかっ, けれ, なけれ, まし, でし: 聞こえる and 死の恐怖 have no run
    for 聞く / 死ぬ, 電話さえ is 電話 + さえ, 行い / 引っかかった have no run for 行く / 引く, 見たかっこいい
    is 見た + かっこいい, 食べるけれど is 食べる + けれど); `NEXT` lists what the 音便 kana may be
    followed by (the た/て pieces, ちゃ, じゃ, とく, どく, たら, たり, ても …: 行います has no run for
    行く); `NOT_BEFORE` keeps a piece from ending a span where the text after it makes it another
    word (ても/でも before ら: 食べて + もらう; たら/だら before しい しく しか しけ しさ: 食べた + らしい;
    たく before せ さ ら: くせに, たくさん, くらい; た before くさん; いき before な: いきなり; いく before
    ら: いくら; いた before だ: いただく, so 食べていただく is followed to its end and never cut inside).
    The furthest valid end wins; the span is at least stem + 1 except for the two bare stems.
  - Known gaps, listed rather than promised: 食べちゃった / 食べちゃって stop at 食べちゃ (っ is no
    tail after ちゃ / じゃ); 食べたがる and 勉強できる are not covered; 来い, 行こ！ (the volitional
    without う before punctuation), 行かねば / 行かぬ, 書いといて / 読んどいて (とく is a tail, とい is
    not, and 書い alone is no form) and 書きそう / 話しそう (そう is a first piece of the ichidan and
    adjective tables, not a tail) are not matched; 〜てもらう is 食べて + もらう (もらう is no tail,
    unlike いただく). Of the kana verbs: はいた (はいる, but also 吐いた and 履いた) and わかれ (わかる,
    but also 別れ) are homographs the matcher colours; からいよ (ICU から|いよ: the exact word at
    no boundary) and the くれた of 見てくれた (ICU's noun 見てくれ) stay plain; かけたらしい and
    かけたくさん (ICU かけ|たらしい, かけ|たくさん) stay plain, since らしい and くさん are no
    particles and `NOT_BEFORE` keeps た from ending the form before them.
  - It stays linear-ish: Maps keyed by the substring, never a loop over the deck per position.
- Three things take a colour without being a deck word, so that a line reads in whole pieces
  (ちょうどこのお風呂の中で with 風呂 and 中 in the deck is `[ちょうど][この][お風呂の][中で]`, not
  `お[風呂]の[中]で`; 視聴者の方に話しかけていただくっていうね is `[視聴者の][方に][話しかけて
  いただくって][いう][ね]`). Each is a run of its own with the word's `status` and `pitch` null;
  content.js draws every run, and adjacent runs of one status stay separate. A word with a
  pitch and no status has no colour to run on: what would take it is plain text.
  - An honorific prefix (`HONORIFICS`: お, ご) joins the word it fronts: at a start `i` whose
    character is one AND `starts.has(i + 1)` (ICU cut the prefix off: お|風呂, ご|家族, お|仕事; it
    keeps お茶, お前, お金, ご飯, お母さん whole, so those are never tried and 前 never colours
    お前), when no word matches at `i`, the match is tried at `i + 1`; on a hit the prefix's run
    comes before the word's.
  - The particles after a word take its colour (`particlesAt()`, handed `wordAt`): entries of
    `PARTICLES` in a row (`particleLens()`: `particleShapes()` less what `PARTICLE_NOT_BEFORE`
    refuses, longest first), each ending at a word boundary (には in 本|に|は, で in 中|で, です
    in 学生|です; not に in 猫|にんじん, not と in 食べる|という: the boundary is the whole test,
    and ICU's word list decides where one is) or, one kana long, right before the い of いる
    (`iruAt()`: a single い before a boundary and one of `IRU_ENDINGS`, た て ない なかっ なく ます
    まし ませ る れば よう たい, is a word to the particle before it, 猫|も|い|た, 猫|に|い|て, and a
    one-kana particle ICU fused with that い, 猫|がい|た, 猫|はい|て, 猫|とい|た, 猫|がい|れ|ば, is
    a shape although it ends at no boundary; not before an い that ends the text, 猫|がい, or that
    is followed by anything else, 猫|はい|、), the chain reaching furthest of those that a word
    follows (`wordFollows()`: the end of the text, anything but hiragana, a piece in a particle's
    shape, the い of いる, or a segment of two kana or more not ending in っ; or a deck word,
    `wordAt`, with or without an honorific or quotative in front, の in 私|の|お|風呂, which also
    ends the chain: かもしれない in the deck, in 猫|かも|し|れ|ない) or that end in one of
    `PARTICLES_ONLY` (を, へ: no word begins with them, so they are taken whatever follows,
    猫|を|み|た, 猫|を|た|べた, 猫|へ|い|っ|た; not は, since はいる, はしる, はなす and はじめる are
    common kana verbs), cut to `PARTICLE_CHAIN_MAX` (3): 本にはねよな takes には, ね and よ, and
    the walk stops there too, at `PARTICLE_MAX_LEN ** PARTICLE_CHAIN_MAX` nodes at most (a line
    of alternating particles, 猫のにのに…, used to cost 2 ** n). That
    rule answers ICU's habit of cutting a kana verb it does not know into single kana, the first
    of which is a particle as often as not (猫|が|で|た, 猫|に|も|ら|っ|た, 猫|は|よ|か|っ|た,
    食|べ|て|し|まっ|た): a particle before a single kana that is no particle is not taken
    (猫|が|す|わっ|た colours nothing past 猫), and `PARTICLE_NOT_BEFORE` refuses the particles a
    following kana makes such a verb of (なら before ない/なく/なかっ/なけれ/ん: ならない; で before
    て/た: 出る; な before に/れ/っ/る/で: 何, なる, 撫でる; ね before て/た: 寝る; よ before か/ん:
    よかった, 呼んだ; の before ん: 飲んだ; や before っ: やる; し before ま/れ/て/た: しまう, しれない,
    する; も before ら/て: もらう, もてる; か before っ/え: 買う, 帰る; と before っ: 取る), a refused
    piece counting as a word for the particle before it, so が is taken in 猫がでた and 猫がでてきた,
    に in 猫にもらった (never にも), は in 猫はねた and 猫はよかった, かも in 猫かもしれない, and で,
    も, ね, よ and し are not. 〜といて, the contracted ておいて, colours the と with the verb
    (勉強せんといて: ICU cuts 勉強|せん|とい|て, and と before the い of いる is a particle's shape;
    いて after it is いる to the matcher). A particle after an uncoloured word stays plain.
    Without a segmenter every segment is one kana, so a particle is then taken at the end of the
    text, before anything but hiragana, before another particle or before the い of いる only.
    Known gap: a verb cut into a particle and two kana or more takes the particle (猫|が|に|げた
    colours に, 猫|を|さ|が|した さ and が).
  - A quotative (`QUOTATIVES`: って, と, longest first) may front いう, and いう alone
    (`QUOTED_WORD`), inside one segment: ICU keeps っていう and という (彼|という|人) whole, so
    いう never begins a segment, while it cuts って off every other word (って|こと, って|もの)
    and keeps ところ, とおる, とまる and とくに whole because they are words of their own, so
    those stay plain with ころ, おる, まる or くに in the deck. At a start `i` holding one, when
    no word matches at `i`, the match is tried at `i + prefix.length` and taken only when it is
    いう and ends at a boundary or the text end, so いう is found in っていう / という and not in
    そういう or といった. The quotative takes the status of the run that ends at `i` (って after
    話しかけていただく, と after 猫), else it stays plain (彼と | いう).
- Tests: `addon/tests/words.test.js` covers every function above: the field readers and their
  bounds (a passage, a field of `<` never closed), `moraCount`, `parsePitch` in each form and its
  mora sources, `pitchOf` (the named field, the fallback, the sentence reading skipped, a graph
  field before a position field), `statusOf` and `mergeStatus`, `buildIndex` (trimming, the
  particles, the stems, the kana-only stems of two kana at least), `wordStarts` with Node's ICU,
  and the matcher rule by rule with explicit `starts` sets mirroring ICU and once more with the
  real `wordStarts`: the examples above, the tails, `AFTER`, `OPEN_TAILS`, `NEXT`, `NOT_BEFORE`,
  the compounds, the bare stems, the particles and auxiliaries, the two lines of the viewer
  exactly as wanted, the honorific prefix (お茶 / お前 with 茶 / 前 stay plain, お茶 with お茶,
  ご|家族, a status-less word's prefix), the particle chain (本には, 本からは, the fourth staying
  plain, a particle after a plain word, に in にほん as one segment, the cut-up verbs, the
  `PARTICLE_NOT_BEFORE` pieces, a deck word winning, no segmenter, the particle before a word
  with an honorific prefix, を and へ whatever follows them and が before the い of いる, a line
  of alternating particles staying quick), いう in という and っていう and not in そういう (and
  ところ, とおる, とまる, とくに staying plain), the kana-only stems with their guards (いれば,
  かけら, かけに行く, the one-kana stems staying exact), a kana verb's form ending inside the
  segment ICU made of its ending and a particle (and the exact bounded word not), くれる after
  the て ICU fused with its く (the caller's `starts` left as it was), a kana word whose stem is
  a particle getting no forms, the っ of a く verb admitted after 行 alone (あるって, はたらって
  and 書って staying plain, あるいて, 書いた and 行った found), a kana word found whole winning
  over a form of it that adds particles alone (いくつ|です|か, きょう|です, わかる|ん|だ, わかるまい
  and 食べるでしょう as one run), and a long line against a large deck.

### The background index (`addon/background.js`, "word colours" section)

- `deckSearch(name)` -> `"deck:NAME"` with `\`, `"`, `*`, `_` backslash-escaped inside the quotes
  (Anki's syntax; the deck and its subdecks). `deckScope(url, deck)`: the names `current` and
  `filtered` are keywords to Anki's search with no escape, so those two are searched as
  `did:<id,...>` (the deck's own id and its subdecks' from `deckNamesAndIds`), null when no such
  deck exists (an empty deck, like any other missing name: AnkiConnect answers `[]`; the popup's
  deck list is the guard).
- `fetchDeckIndex(url, deck, settings, signal)`: the five `findNotes` searches (`STATUS_QUERIES`,
  each `${scope} <clause>`): `suspended` `is:suspended`, `unsuspended` `-is:suspended`, `new`
  `is:new -is:suspended`, `learning` `is:learn -is:suspended`, `review` `is:review -is:learn
  -is:suspended` (since Anki 2.1.44 the three go by the card's type, which a suspended card keeps).
  The deck's ids are suspended ∪ unsuspended, sorted ascending: Anki's `findNotes` has no ORDER
  BY (a review that moves a due reorders its answer), and the same deck must give the same
  entries or every tab would take them in again. `notesInfo` (chunks of `NOTES_INFO_CHUNK`, 200,
  `CARD_STATUS_TIMEOUT_MS` 20 s per request) runs only for ids not in the note cache (`notes:
  Map<id, {word, pitch, mod}>`, kept across refreshes and mines, ids gone from the deck dropped)
  and for known notes edited since `checkedAt`: a sixth search `${scope} edited:<days>` (days =
  `max(2, ceil(elapsed / day) + 1)`; an Anki without it finds nothing) lists candidates and
  `notesModTime` re-reads only those whose `mod` moved (without the action, every candidate).
  Word = `SHISUKO_WORDS.plainWord(noteSummary(info, settings).word)` (`ankiWordField`, else
  order 0; over `MAX_WORD_LEN` stored as `""`), pitch = `SHISUKO_WORDS.pitchOf(info.fields,
  settings)`. Entries: one `[word, status, pitch]` per note with a word and a non-null
  `statusOf(sets, id)`, by ascending id. A `notesInfo` chunk that fails keeps what `known` said
  for its ids (a re-read note keeps its colour and old pitch) and is asked again on the next
  refresh; a `TypeError` or `AbortError` fails the ask as a whole. `dropped()` (the `signal`) is
  checked before every request and once more after the loop, so a fetch dropped during its last
  request answers nobody. Returns `{deck, wordField, pitchField, at, fetchedAt, checkedAt,
  entries, notes, changed}`: `at` is kept from the previous index when the entries came out the
  same (`sameEntries`), so a tab holding them hears "unchanged"; `fetchedAt` is the clock the
  TTL runs on; `changed` = a note read (`read > 0`) or dropped (`notes.size !== known.size`) or
  the record restored, and decides whether the session record is written.
- The cache: `cardIndex = {deck, wordField, pitchField, at, fetchedAt, checkedAt, entries,
  notes}` and the fetch under way `cardIndexInFlight = {deck, wordField, pitchField, promise,
  controller, expired}`, both named by the deck and the trimmed `ankiWordField` /
  `ankiPitchField` they were read with; `indexFor(index, deck, settings)` compares all three and
  is what the fresh and stale answers, the flight joining and `fetchDeckIndex`'s `previous` go
  by. `refreshCardIndex(url, deck, settings)` joins a flight for the same deck and fields and
  aborts one for anything else through its `AbortController` before starting anew (the walk ends
  at its next request with an error marked `dropped`; the waiting `cardStatus` re-runs once for
  what is set now). `dropCardIndex()` (the `browser.storage.onChanged` listener, when one of
  `CARD_INDEX_SETTINGS` = `cardStatusDeck`, `ankiPitchField`, `ankiWordField` changed; `ankiUrl`
  is not among them) forgets the index, moves `cardIndexGeneration` on, aborts the flight and
  clears the session record. `expireCardIndex()` sets `fetchedAt = 0` and marks a flight
  `expired` (its index lands expired), the notes staying. The notes also live in
  `storage.session` under `DECK_NOTES_KEY` (`deckNotes` = `{deck, wordField, pitchField, at,
  checkedAt, entries, notes: [[id, word, pitch, mod]]}`, `notesRecord()`), so a return to YouTube
  after the event page ended does not read the whole deck again: `restoreNotes(deck, settings)`
  ignores a record for another deck or other fields and hands back `at` and `entries` only with a
  positive `at` (0 is what a tab holding nothing sends), so the stamp survives a restart when the
  entries came out the same; the page that restored it writes it once more.
- The deck: `rememberDeck(url, noteId)`, from `addToAnki()` after every card it filled, not
  awaited: `findCards {query: "nid:<id>"}` -> `getDecks {cards}` -> the deck with most of them ->
  `storage.local` `DECK_SEEN_KEY` (`ankiDeckSeen` = `{deck, at, noteId}`) and `expireCardIndex()`,
  so the new card shows red at the next ask rather than after the TTL; errors are `console.debug`.
  `seenDeck()` reads the record; `resolveDeck(settings)` -> `{deck, automatic}`: the trimmed
  `cardStatusDeck`, else the seen deck (`automatic: true`), else `{deck: null, automatic: true}`.
- `cardStatus(msg, retried)`, the handler of `{type: "cardStatus", since?}`: captures
  `cardIndexGeneration`, then `getSettings()`; not `wordColoursOn(settings)` (`cardStatus ||
  pitchAccent`) -> `{ok: false, reason: "off"}`; no deck -> `{ok: false, reason: "noDeck",
  error: "No card mined yet; pick a deck in the popup"}`; `ankiPermission(url)` false -> `{ok:
  false, reason: "denied", error}` (the shared helper: one dialog at a time, see the mining
  section); the generation moved while the dialog was up -> once more for the settings of now;
  an index for this deck and fields with `fetchedAt` under `CARD_STATUS_TTL_MS` (30 s) is
  answered from memory, else `refreshCardIndex()`. The answer is `{ok: true, unchanged: true,
  at, deck}` when `msg.since === at`, else `{ok: true, deck, automatic, at, entries}`. On a
  failure the old index is answered with `stale: true` when it has entries, else `{ok: false,
  reason: "offline", error: "Anki is not running or AnkiConnect is not installed"}` for a
  `TypeError` or `reason: "error"` with the message; a `dropped` error re-runs once.
- `ankiDecks()`, the popup's `{type: "ankiDecks"}`: `{ok: true, decks: string[] (from
  `deckNames`, sorted), seen: string | null}` or `{ok: false, reason: "offline" | "denied" |
  "error", error, seen}`, through `ankiPermission()` as well. The message switch routes both.
- Tests: `addon/tests/background.test.js` (a fake AnkiConnect answering the searches by their
  `query`, `notesInfo` per chunk, `findCards` / `getDecks`, `deckNames`, `edited:` and
  `notesModTime`): `deckSearch`, `resolveDeck`, every `cardStatus` verdict, the five queries and
  each status including the buried-learning fallback, the pitch from a field named `PitchAccent`
  with `[2]` beside a `Reading` field, a merged duplicate, the fields the viewer named, the TTL
  and "unchanged", the stamp kept when the deck came out the same, the edited note re-read, the
  stale answer, the settings that drop the index and the ones that do not, the mine remembering
  its deck and expiring the index, a mine whose deck cannot be told, the dropped fetch (another
  deck asked for, the last request), a mine during a fetch, a field named while the dialog was
  up, the session record, the failing chunk, `current` / `filtered` by id, a sentence in the word
  field, the shared permission dialog and its failure, `ankiDecks`, the message switch, and a
  note whose fields are a hundred kilobytes of `<` (the ask finishes under a second).
  `_loadBackground.js` loads `words.js` between `match.js` and `background.js` and exposes
  `CARD_STATUS_TTL_MS`, `DECK_SEEN_KEY` and `DECK_NOTES_KEY`.

### The content side (`addon/content.js`, "word colours" section)

- `wordColoursOn()` = `enabled && (cardStatus || pitchAccent)`. State: `wordIndex` (a
  `buildIndex()` result), `wordIndexSerial` (moves on with every index put in `wordIndex`, a new
  one or none: what dates a cue's look), `wordIndexAt` (the background's `at`, the `since` of the
  next ask), `wordIndexKey` (`JSON.stringify(entries)`), `wordIndexAskedAt`, `wordIndexInFlight`,
  `wordIndexGeneration` (times the index was started over), `wordIndexDrawn` /
  `wordIndexDrawnSerial` (the index the last `refreshWordMarks()` ran with), and three WeakMaps:
  `lineTexts` (transcript line -> its `.shisuko-linetext` span), `drawnKeys` (element -> the
  `drawKey()` of what `renderText()` last drew in it), `cueLooks` (cue -> `{serial, cardStatus,
  pitchAccent, starts, runs, key}`). Constants `WORD_INDEX_REFRESH_MS` 30 s, `WORD_INDEX_LOG_MS`
  60 s, `WORD_INDEX_MINE_DELAY_MS` 1.5 s, `WORD_INDEX_PROBE_MAX` 64, `WORD_SETTINGS` =
  `cardStatus`, `pitchAccent`, `cardStatusDeck`, `ankiPitchField`, `ankiWordField` (if the
  background's `CARD_INDEX_SETTINGS` ever gains `ankiUrl`, add it here too).
- `renderText(el, cue)` is the one writer of a cue's text, used by `setSubtitle()` for
  `state.subText` and by `transcriptLine()` for the line's span: `lookOf(cue)` gives the runs,
  `drawRuns()` puts them in a `DocumentFragment` and `replaceChildren()`s it: a string run is a
  text node, a marked run `<span class="shisuko-word">` with `dataset.status` only when
  `cardStatus` is on and the run has a status, `dataset.pitch` only when `pitchAccent` is on and
  it has a pitch, and its text as a text node; a run with neither is joined into the plain text
  around it. Without `wordColoursOn()` or an index, `el.textContent = cue.text`. `lookOf()`
  answers from `cueLooks` when the entry's `serial` is `wordIndexSerial` under the same pair of
  colours, else runs `SHISUKO_WORDS.markWords(cue.text, index, starts)` with the entry's `starts`
  (`wordStarts(cue.text)` the first time, kept whatever the index) and records it under the serial
  of now; a look never holds an index, so a cue drawn while the transcript was hidden pins no old
  index. A rebuilt panel and the line on screen for a cue whose line is up ask the matcher and the
  segmenter nothing.
- `refreshWordMarks()`: with the last drawn index and the new one both held and the colours on,
  `indexProbes(prev, next)` lists the words the two disagree on (present in one only, or another
  status or pitch), each replaced by its stem where it has one (a prefix of the word and of every
  form), `[]` when they agree, null over `WORD_INDEX_PROBE_MAX`. The active cue's text and, when
  the transcript is shown, every line are left alone when `sameLook(el, cue, prevSerial, probes)`
  holds (the look carries `prevSerial` with the colours of now, `drawnKeys.get(el)` is its key,
  and the text includes no probe; the look's serial then moves on) and otherwise go through
  `refreshText()` (a redraw only when the `drawKey()` differs), so a card reviewed in Anki costs a
  look at each line's text and a match of the lines holding that word, and replaces neither the
  nodes of any other line nor what Yomitan holds on them. Lines still pending (none, as a rule: a
  shown panel renders on arrival) are put in first by `renderTranscript()`, drawn under the index
  of now; the panel is never rebuilt for a refresh.
- `pollWordIndex()` runs from `syncTick()` and asks only when `wordColoursOn()`, `state.video &&
  state.videoId` (the home page and a player left behind off a watch page never ask, a settings
  change included), the tab visible, nothing in flight and `WORD_INDEX_REFRESH_MS` past: `{type:
  "cardStatus", since: wordIndexAt}`. A tab standing by for another (`state.standby`, see "One
  tab at a time") asks like any other: the index is the deck's and the election is about the
  server, which this never reaches, and the standby tab's lines stay on screen and must be
  recoloured too. An answer from before `wordIndexGeneration` moved is thrown
  away. `unchanged` keeps everything; a new `at` with the same key moves the stamp only; new
  entries rebuild the index, move the serial and call `refreshWordMarks()`; `reason: "off"` drops
  the index and takes the colours off; any other failure is a `console.debug` line at most once
  per `WORD_INDEX_LOG_MS`, never a toast; `stale` answers count as fresh. After a successful mine
  `mineCue()` sets `wordIndexAskedAt` so the next ask goes out `WORD_INDEX_MINE_DELAY_MS` after
  it, once the background has expired its index.
- The storage listener: a change to any of `WORD_SETTINGS` runs `dropWordIndex()` (index null,
  serial and generation on, stamp, key and `askedAt` 0), then `refreshWordMarks()` (plain text
  again at once, in place) and, with the colours on, `pollWordIndex()`. `applySettings()` never
  rebuilds the transcript (only `state.transcriptRebuild`, set by `dropCues()` or a new panel,
  makes `renderTranscript()` build every line anew): a panel shown again renders what arrived
  while it was hidden (`renderTranscript()`) and then runs `refreshWordMarks()` and
  `highlightTranscript()`, so its lines catch up in place on the index and the active line; a
  style setting written (a slider dragged) leaves the lines and the line on screen as they are.
  `dropCues()` leaves the index alone (it belongs to the deck, not the video).
- Tests: `addon/tests/content.test.js`: `wordColoursOn`, `renderText` plain and with either or
  both attributes in spans holding text nodes, `setSubtitle` / `transcriptLine` drawing through
  it, the refresh redrawing only the changed lines, a word setting taking the colours off in
  place, a style write leaving the lines alone, one card reviewed matching only the lines holding
  the word or its stem (`countingWords()` wraps `markWords` / `wordStarts`; `_loadContent.js`
  declares words.js's export with `var` for that), a hidden transcript's cue pinning nothing, the
  poll's conditions and answers, the deck change, the overtaken answer, the mine re-ask, the
  master switch off sending nothing, a new server session keeping the index.

### The popup (`addon/popup.html` / `popup.js`)

The "Word colours" section holds `#cardStatus`, the `#cardStatusDeck` select (first option value
`""`), `#deck-hint`, `#pitchAccent` and the two legends (swatches in `popup.css`); `#ankiPitchField`
sits in the "Anki, clips and server" drawer after the word field. `renderDeckOptions(decks,
seen, current)` keeps the automatic entry first (`automaticDeckText(seen)`: `Automatic: <deck>`
once Anki has been asked, `Automatic: no card mined yet` for `seen` null, and the page's own
`Automatic: the deck of the last mined card` for `seen` undefined, before any ask or after a
message that failed outright), then one option per name, sorted, the stored value kept as an
option even when unlisted (a select drops a value without an option, and the setting would go
with it at the next save); `init()` calls it before the `setField` loop. `refreshDecks()` sends
`{type: "ankiDecks"}` (answers numbered by `decksAsked`, an overtaken one dropped; `decksOk` true
for a listed set, false for a failure, null while an ask is out) and paints `#deck-hint` only
while a checkbox is on: the error (warn) when not ok, `No deck named <name> in Anki` (error) for
an unlisted manual deck, nothing for a listed one, `The last mined card's deck <seen> is no
longer in Anki; mine a card, or choose a deck` (warn), `Looking at <seen>`, or `Automatic: no
card mined yet — mine one, or choose a deck` (warn). Anki is asked from `init()` only when
`cardStatus || pitchAccent` is stored on (the first ask brings up AnkiConnect's dialog, which a
viewer who never uses the feature must not meet), from `onChange` after the debounced save when
a checkbox was turned on or `ankiUrl` edited (`decksCheckPending = "feature"`, only when a
feature is on in the form the save wrote) or the deck select changed (`"deck"`, always), and,
while the newest answer failed and a feature is on, again every `DECKS_RETRY_MS` (30 s) from a
clock `init()` sets beside the health refresh (`retryDecks()`; a good answer is never re-asked by
the clock, and the clock, like the health poll, ticks only while the page is visible,
`document.hidden`), because the options page (`options_ui.page` = popup.html) lives for hours.
The save clears the hint when both features are off. A deck, a checkbox or `ankiUrl` written by
the other copy of the form (the options page and the toolbar popup) lands through
`onStorageChanged()`, which rebuilds the deck option from Anki's last list
(`renderDeckOptions(decksListed, decksSeen, value)`: a select shows nothing for a value without
an option) and asks Anki by the `onChange` rules less the save (`landed`: a deck, a feature that
landed checked, or the URL while a feature is on; a feature landing unchecked asks nothing; both
features off clears the hint). Tests:
`addon/tests/popup.test.js` (the fake document's `options`, `replaceChildren`,
`createElement("option")`): `renderDeckOptions`, every hint, the overtaken answer, `init()`
asking only with a feature on and keeping the stored deck, the retry clock and when it stays
quiet, the asks after a save, both features off clearing the hint, a new AnkiConnect URL, the
word colours edited in the other copy of the form landing in the select and the hint, and Reset
style taking a pending deck edit with it, the ask it carried following that one save.

## How the Start server button works

A WebExtension cannot spawn a process, so the popup's button goes through native messaging:
`background.js` sends `{cmd: "start"}` to the native host `shisuko`, and the host,
`server/native_host.py`, runs the checkout's own launcher. Firefox only: `register()` writes the
Mozilla host manifest, Chrome's would have to name the installed extension's id under its own
key, so `popup.js` hides the button unless `browser.runtime.getURL("")` is `moz-extension:`
(`START_AVAILABLE`). The host is stdlib only, so the wrapper can fall back to the system Python
before setup ran, and it never imports `server.py` (`VERSION` is read from it with a regex).

Protocol (Firefox's: 4-byte little-endian length, UTF-8 JSON, one request per message, answered
in order until stdin closes; `read_message()` / `write_message()`, `serve()`, `handle()`):
`{"cmd": "status"}` -> `{ok, running, version, root}`, `running` being a `/health` answer within
1.5 s (`server_running()`); `{"cmd": "start"}` -> `{ok: true, already: true}` for a running
server, `{ok: true, already: true, starting: true}` for one that holds the instance lock but
does not answer yet (its model is loading; `server_starting()`), else `launch()` and
`{ok: true, started: true, log}` (`log` null on Windows, the path of `~/.shisu-ko/server.log`
elsewhere) or `{ok: false, error}` in one line. Anything else, a request with extra keys
included, is `{ok: false, error: "unknown command"}`; a frame over 1 MiB is answered and ends
the host. The instance lock is `APP_DIR/server-<port>.lock`: `server.py` takes it in
`hold_instance_lock()` before `load_model()` and holds it until the process ends, a second
server exits 2. `try_lock()` exists in both files (`msvcrt.locking` / `fcntl.flock`); only
`LOCK_HELD_ERRNOS` mean "held", a filesystem that cannot lock at all counts as no lock.
`launch()` on Windows runs `cmd.exe /c start "Shisu-ko server" .\run.cmd` with `cwd=server/`
(cmd.exe splits a full path holding `&` or `(` even when quoted), `DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP` and first `CREATE_BREAKAWAY_FROM_JOB`, retrying without it on
`PermissionError`; on POSIX `bash run.sh` with `start_new_session=True` and stdout/stderr
appended to the log (bash, not the file itself: a zip install has no mode bits). The browser
starts the host with arguments of its own (Firefox: manifest path and extension id); `main()`
serves whenever no action flag is given and stdin is not a terminal.

Registration (`native_host.py --register | --unregister | --status [--verbose]`, exit 0 on
success, quiet unless `--verbose`): the manifest `{name: "shisuko", description, path:
<wrapper>, type: "stdio", allowed_extensions: ["shisu-ko@multysquid.github.io"]}` goes to
`%USERPROFILE%\.shisu-ko\native-messaging\shisuko.json` (`SHISUKO_HOME` respected) plus the
default value of `HKCU\Software\Mozilla\NativeMessagingHosts\shisuko` on Windows, to
`~/.mozilla/native-messaging-hosts/shisuko.json` on Linux and to
`~/Library/Application Support/Mozilla/NativeMessagingHosts/shisuko.json` on macOS. The
wrapper is `server/native-host.cmd` (CRLF) or `server/native-host.sh` (LF, mode 755;
`register()` restores the bit a zip install drops): the venv's Python, else the system one, on
`native_host.py`. `setup.cmd` / `setup.sh` register and then run `--check`, which reports it;
`run.cmd` / `run.sh` register on every start, so an install that never re-ran setup gets the
button after a manual start, with one exception: the start that updates an older checkout to
this version does not register, because the old launcher is what runs (`run.cmd`'s old
`update.py ... & goto loop` jumps to `:loop` in the new file, below the register line; `run.sh`'s
already-parsed old `main()` has no register call), so it is the start after the update, or
setup, that registers. In `run.cmd` the call sits on its own line before the
`update.py ... & goto loop` line; in `run.sh` after the update, which rewrites the wrapper.
`launch()` passes no arguments to the launcher: a server the button started runs on `server.py`'s
defaults (`--model` from `config.json`, else `default_model_for()`; `--device auto`), and the
popup's model setting only takes effect after that default model is loaded.
`run_check(device)` in `server.py` loads `native_host.py` by path and prints `status_text()`.

Extension side: the popup's flow is `startFlow.state`, `idle -> requesting -> starting ->
waiting -> idle` once `/health` answers, or `failed` with the reason on the detail line and the
button back; the permission request is issued in the click handler before its first `await`
(it needs the user gesture). A server-address edit (`checkServer(true)`) puts a spent flow
(`failed`, and the update flow's `done` / `stale` / `failed` / `lost`) back to `idle`, since
its hint was judged at the old address, and overtakes a `/health` request still under way
(`healthAsked`: the old address may hold it up for the full timeout, and its answer is
dropped). The background owns the launch (`startServer()`): one
`sendNativeMessage` at a time (`startInFlight`), raced against `NATIVE_TIMEOUT_MS` (15 s), the
answer recorded with a `deadline` of `START_WINDOW_MS` (90 s, past any healthy model load) in
memory and in `browser.storage.session` (Firefox ends an idle event page after 30 s), so a
reopened popup resumes at "waiting" (`startServerStatus`) instead of offering a second start
while the first still loads; a `/health` answer through `apiRequest()` forgets the record.
`nativeError()` maps the browser's exceptions: "No such native application" / "not found" /
"forbidden" -> `launcher not registered` with `LAUNCHER_HINT`; permission wording, or no
`sendNativeMessage` at all -> `permission missing`; timeout -> `the launcher did not answer`;
the host's own `{ok: false, error}` passes through. The popup keeps `already` and the host's
`starting` (as `loading`) for the hint at the deadline: `START_ELSEWHERE_HINT` when the launcher
saw a server answering that the popup cannot reach, else `startNotUpHint(log)`. Badge texts:
`Checking server`, `Server offline`, `Starting server`, `Updating server`, `Loading model`,
`Server online`. A start and an update exclude each other: `requestStart()` refuses while
`pendingUpdate()` holds a record (`UPDATE_RUNNING_HINT`), `startStatus()` carries that record
as `updating` so a reopened popup hides the button before its first paint, and the popup
disables Update while `START_BUSY` and hides Start while `UPDATE_BUSY`.

Tests: `server/tests/test_native_host.py` (framing, `handle()` for every shape with `launch()`
never called, `serve()`, `launch()` with a recorded `Popen` on both platforms, the lock against
`server.py`'s, registration into a temp home with a fake `winreg` on every platform, `same_file()`
against two spellings of one wrapper, `main()`, the host over a real pipe, the wrapper run the way
Firefox runs it, the launchers' register
lines); `server/tests/test_server.py` for `hold_instance_lock()`; `addon/tests/background.test.js`
(the message, the error mapping, the timeout, the record across an event-page restart);
`addon/tests/popup.test.js` (the flow against a fake document: resume, both deadline hints,
Chrome hiding the button, one host request per click); `addon/tests/browser-api.test.js` for
the Chrome bridge of `sendNativeMessage` and `storage.session`.

## How the update step works

`server/update.py` (stdlib only) runs first in `run.cmd` / `run.sh`, never in Docker or Nix. In a
git checkout it fetches the tracked upstream and fast-forwards (`merge --ff-only`); a diverged
branch, local changes git would overwrite, a detached HEAD or an unreachable remote leave the
tree alone with a message. In a folder without `.git` it compares `VERSION` with the newest
GitHub release tag (`v<VERSION>`, so keep bumping `VERSION`, the manifest and the tag together)
and unpacks the release zip over the folder, staging each file next to its target and
`os.replace()`-ing it, without deleting anything. Both paths reinstall `requirements.txt` into
the running interpreter when it changed (only inside a venv) and point out a changed
`addon/manifest.json` version. It always exits 0: the server must start even when the update
fails. `--no-update` or `SHISUKO_NO_UPDATE=1` skips it; `server.py` accepts `--no-update` too
(`args.no_update`) so the launchers can pass all arguments through, and reads it, like the
variable, as a reason to refuse `POST /update`.

The launchers also run the update on demand, for the popup's Update button: `run.cmd` /
`run.sh` set `SHISUKO_LAUNCHER=1` for the server they start, `POST /update` then marks
`App.exit_code = EXIT_UPDATE` (4), answers, and `stop_server_later()` calls `httpd.shutdown()`
from a helper thread after `SHUTDOWN_DELAY` (0.5 s, so the answer leaves the socket;
`shutdown()` blocks until `serve_forever()` returns, so the handler thread cannot call it),
and `main()` runs `server_close()` and then `sys.exit(app.exit_code)`; every worker is a daemon
thread, so nothing waits for a window. Exit code 4 means "run `update.py`, then start again":
`run.cmd` has `if "%CODE%"=="4" goto update` after the 0 and 2 branches, `run.sh`
`[ "$code" -eq 4 ] && { update.py "$@"; native_host.py --register; continue; }` (the register
call restores the wrapper's mode bits, which the zip update drops). Codes 0 and 2 keep their
meaning, every other code keeps the 5 s restart.

The update can replace the launcher that is running it. cmd.exe reads batch files incrementally,
so in `run.cmd` the update call and `goto loop` must stay on one line and the `:loop` label must
keep its name; `run.sh` keeps everything in `main()` and ends with `main "$@"; exit` for the same
reason. The code-4 path adds two rules. The `:update` label sits directly above that one-line
update call, so `goto update` lands on it: the lines between the server's exit and `goto update`
are read from the old file, which nothing changed since the jump to `:loop`, and the already
parsed `goto loop` looks its label up in the new file, so no line is ever read from a stale
offset. And `set "SHISUKO_LAUNCHER=1"` sits directly after `:loop`, not at the top: a launcher
from before the variable that has just updated itself arrives in the new file through its own,
already parsed `goto loop`, so only the lines after `:loop` run for it, and its server would
otherwise refuse the button. `run.sh` cannot help itself the same way (bash parsed the old
`main()`, which has no code-4 branch, before the update), so `export SHISUKO_LAUNCHER=1` sits
inside `main()` before the loop and the server's 409 text names "an older launcher that has not
been restarted since it was updated"; a restart by hand fixes it. `server/tests/test_update.py`
drives the real git against a bare repository in a temp directory and feeds a locally built zip
in place of the GitHub download; `server/tests/test_update_endpoint.py` covers the endpoint
over a real socket (the variable read strictly as `"1"`, both no-update switches, the latter
asserted equal to `update.skipped()` for nine values, the 409s, the answer followed by
`serve_forever()` returning and `main()` raising `SystemExit(4)`, the origin rule, `GET` 404,
the body guards) and the launcher texts (CRLF in `run.cmd`, `:update` right above the one-line
call, the variable inside the loop, the 0/2/4/restart order, `run.sh`'s export inside `main()`).

## How the update check works

The add-on side of updates lives in the "updates" section of `addon/background.js` and in
`popup.js`; the extension never installs itself (no `update_url`, no `.xpi` handling: its
updates come from addons.mozilla.org once the listing is live, and until then the release page
has the `.xpi`), it only tells the viewer and asks the server to update itself.

- Check. `fetchLatestRelease()` gets `GITHUB_LATEST_URL`
  (`https://api.github.com/repos/Multysquid/shisu-ko/releases/latest`, `Accept:
  application/vnd.github+json`, `UPDATE_CHECK_TIMEOUT_MS` 10 s). No host permission: GitHub's
  API answers cross-origin requests with `Access-Control-Allow-Origin: *`. `releaseFromApi()`
  reads `{version, tag, url, xpi}` (tag `v0.9.0` -> `0.9.0`; `html_url`; the
  `browser_download_url` of the `.xpi` asset or null; https only, since the popup opens `url`
  in a tab). `checkForUpdate({force})` answers from `storage.local.updateCheck`
  (`{checkedAt, latest, error}`) while it is fresh (`checkIsFresh()`: no error and under
  `UPDATE_CHECK_MAX_AGE_MS`, 24 h), else fetches; one check at a time (`checkInFlight`). A
  failure (offline, 403/429 rate limit, non-JSON, no `tag_name`) is stored as `error`, keeps the
  last `latest`, is logged with `console.debug` and never notifies; 404 means no release yet.
- When. `startupCheck()` on `runtime.onStartup` and `onInstalled`, but only once the profile
  has a stored check: a profile that never opened the popup makes no request on its own, which
  keeps `scripts/browser-smoke.mjs` (fresh Chromium profile, local fixtures) off GitHub; the
  smoke test seeds `updateCheck: {checkedAt: Date.now(), latest: null, error: null}` beside its
  settings. The popup's first `updateStatus` carries `check: true` (the day's check when the
  store is stale); "Check for updates" sends `checkForUpdate` with `force: true`.
- Decision. `parseVersion("v0.9.0") -> [0, 9, 0]` (non-numeric or missing parts are 0),
  `compareVersions(a, b) -> -1 | 0 | 1`, and `decideUpdate({latest, serverVersion,
  serverLauncher, extensionVersion, serverOnline})` -> `{server, extension}`. `server`:
  `current` (latest <= version), `newer` (latest > version and `launcher: true`), `cannot`
  (newer, `launcher: false`: Docker, Nix, a hand start, `--no-update`), `behind` (newer, no
  launcher flag: every 0.8.0 server, so no claim about run.cmd), `unknown` (no `latest`, or a
  server without a version such as the smoke fixture's `/health`), `offline` (no server).
  `extension`: `newer` when latest > `browser.runtime.getManifest().version`, else `current`.
  `serverInfo(health)` reads `version` and `launcher` from `/health`, null when missing.
  `popup.js` keeps copies of `parseVersion`, `compareVersions`, `UPDATE_SHUTDOWN_MS` and
  `UPDATE_WINDOW_MS`; `addon/tests/popup-copies.test.js` keeps them equal.
- Ask. `applyBadge()` sets the toolbar badge (`action.setBadgeText` "1", `BADGE_COLOR`
  `#5b6fb8`, never the alert red) for `newer`, `cannot`, `behind` or a newer extension and clears
  it otherwise; `notifyNewer()` creates one system notification per release and browser session
  (`notifiedVersion` in `storage.session`; id `shisuko-update`, "Shisu-ko <latest> is
  available" / "The server runs <server>. Click to update it now."), only for `newer`, only from
  the start-up check. `notifications.onClicked` runs `updateServer({watch: true})`; a request
  that fails there gets a notification of its own (`shisuko-update-failed`, whose click does
  nothing), except when the server already runs the release (`upToDate: true`: updated another
  way while the notification sat there, and the click's own clear was all there was to do);
  `updateStatus()` clears `shisuko-update` whenever the decision is `current`. Badge and
  notification helpers tolerate a missing API (the test sandbox, a content script). The
  popup's banner (`#update-banner`, `renderUpdate()`) names the release and the server's
  version with **Update** and **Not now** for `newer`, the reason and no Update button
  for `cannot` ("was not started by run.cmd / run.sh", the one text for every blocker, since
  `/health` carries only the flag and a `--no-update` server's 409 text is never fetched) and
  `behind` ("cannot be updated from
  here"), the release page link for an extension behind, and nothing for `offline` (the next
  start updates) or after "Not now" (`updateSnoozed` = latest in `storage.session`). The
  "Check for updates" link in the last drawer writes `#update-result` ("Newest release: 0.9.0,
  checked 3 min ago", "No release found, …", "Update check failed: <error> (last seen: X)").
- Update. Popup **Update** or the notification click -> `{type: "updateServer"}` ->
  `requestUpdate()`: `/health` first (so a spent record ends, see below), refuse without a POST
  (`{ok: false, upToDate: true, error}`) when the server already runs >= latest, else
  `POST /update` through `apiRequest()`. On
  `{ok: true, restarting: true}` the record `serverUpdate` `{requestedAt, from, to, deadline,
  down}` (`deadline` = `UPDATE_WINDOW_MS`, 120 s: the launcher's update plus a model load) goes
  to `storage.session` with a memory fallback (`sessionGet` / `sessionSet`), the notification is
  cleared and the popup gets `{ok, restarting, from, to, requestedAt, deadline}`; a 409 passes
  the server's text through as `{ok: false, error, refused: true}` (the banner shows it and
  drops the button), unreachable is `offline: true`. Every `/health` answer through
  `apiRequest()` passes `noteHealth()`: no answer marks the record `down`; a version >= `to` ends
  it, recomputes the badge and notifies "Shisu-ko updated to <version>" (`shisuko-updated`);
  the old version ends it silently once the server was seen down or `UPDATE_SHUTDOWN_MS` (10 s)
  has passed since the request (the old server closes its port within a second), because the
  old code back after a restart means `update.py` could not update. The notification path has
  no popup polling, so `watchUpdate()` polls `/health` every `UPDATE_POLL_MS` (3 s) until the
  record ends. The popup's `updateFlow` (`idle -> requesting -> updating -> done | stale |
  failed | lost`) mirrors it: badge "Updating server" with "Restarting with <latest>…", then
  "Updated to <version>", `stillOldHint()` ("The server restarted but still runs X; look at its
  window: update.py said why", banner stays) or, offline at the deadline, `UPDATE_LOST_HINT`
  with the Start button back. `refreshUpdate()` sends the popup's own `/health` answer with the
  question and re-asks only when the server's version, launcher flag or reachability changed or
  after an action; answers are numbered (`updateAsked`) so a slow first answer, held up by the
  check of GitHub, cannot overwrite the verdict for the server now on screen; an overtaken
  first answer makes the popup ask once more, since it carried the day's check, which the answer
  that overtook it was given without. It resolves to whether an answer was taken, and the banner
  is painted again only then. A reopened popup resumes the flow from `startServerStatus`
  (`updating`) before its first paint.
- Chrome. `browser-api.js` bridges `action.setBadgeText` / `setBadgeBackgroundColor`,
  `notifications.create` / `clear` (with `onClicked` passed through) and `tabs.create`; the
  endpoint is plain HTTP, so the flow is the same there.

Messages: `updateStatus {health?, check?}` -> `{latest, checkedAt, error, server: {version,
launcher} | null, decision, snoozed, updating, extensionVersion}`; `checkForUpdate {force?}` ->
`{checkedAt, latest, error}`; `updateServer` -> as above; `snoozeUpdate {version}` -> `{ok}`.

Tests: `addon/tests/background.test.js` (the version helpers and `decideUpdate`,
`releaseFromApi`, the store and its age, every failure of the check, the start-up check with
badge and one notification per session and its silence for `cannot` / `behind` / no server, a
browser without the APIs, `updateStatus`, `snoozeUpdate`, the `/update` request with its record
across an event-page restart, the `/health` polls that end it including the old version with
and without a poll that saw the server down, the notification click and its failure
notification, the refusal of a start during an update, the message switch);
`addon/tests/popup.test.js` (every banner verdict, the overtaken answer, Not now, Update to
"Updated to 0.9.0", the old version back, the deadline, a 409 in the banner, the resumed
update from both status messages, the Start/Update exclusion, Check for updates);
`addon/tests/browser-api.test.js` (the Chrome bridges); `addon/tests/settings.test.js`
(`notifications` required, no `update_url`); `addon/tests/popup-copies.test.js` (the copies).
`addon/tests/_loadBackground.js` stubs `action`, `notifications`, `runtime.onStartup` /
`onInstalled` / `getManifest` and exposes `startup()`, `clickNotification()` and `setNow()`.

## Commands

Nix (any Linux with flakes, NixOS): `nix run . -- [options]` starts the server with CUDA
(`flake.nix`; CTranslate2 comes prebuilt from `cache.nixos-cuda.org`, onnxruntime is the CPU build
because only the VAD uses it). `nix run .#check`, `nix run .#tests`, `nix build .#addon`,
`nix develop` for a shell with Python, web-ext, Node and Deno. `.#server-cpu` is the CUDA-free variant.
Native server (Windows): `server\setup.cmd` once (it asks for large-v3 or small and downloads it),
then `server\run.cmd [options]`.
Native server (Linux/macOS): `bash server/setup.sh`, then `server/run.sh`. Its first menu choice is
whatever `server.py --default-model` prints, so an Apple GPU is offered large-v3-turbo and every
other machine large-v3.
Diagnostics: `server\run.cmd --check` (also says which backend `--device auto` picks, whether the
Start button's launcher is registered and which model a bare start runs).
This machine's built-in default model: `server.py --default-model` prints it and exits, ignoring
`config.json`, which is the choice it is about to be compared with.
Music videos: `--lyrics auto` (default) transcribes a window the speech detector hears next to
nothing in (under `LYRICS_MAX_SPEECH_S`, 1 s) without the detector when its audio is not silent
and the language head hears the target language in it; `--lyrics off` transcribes such windows
with the detector as before. `retranscribe.py` takes the same switch.
Model download with a progress bar, what setup runs after the check: `server.py --download-model
NAME` (`run_download_model()`: validates the name like `/sync` does, resolves the alias,
`huggingface_hub.snapshot_download()` with faster-whisper's five file patterns and its own tqdm
bars, refuses a repo without `model.bin`, writes `config.json`; exit 0, else 2, the code the
launchers end on instead of restarting, so `run.cmd --download-model x` cannot loop; never loads
a model or takes the instance lock). The download runs on a daemon thread that the main thread
joins in half-second steps (`wait_for_thread()`): a Ctrl+C inside `snapshot_download()` would
only surface once its thread pool has finished streaming the current file (model.bin, minutes),
and Windows delivers the signal only between waits. The interrupt prints one line and ends the
process with `os._exit(2)`, since a normal exit would wait for that pool's worker at shutdown;
the partial blob stays as `.incomplete` and the next download resumes it. `setup.cmd`'s pick
line tests `errorlevel 3` before 2: `choice` answers 255 when it cannot read a key (stdin closed
or empty), and that takes large-v3 like `setup.sh`'s EOF fallback. Which conversion it fetches
follows `resolve_device(args.device)`, not the machine alone: on an Apple GPU it is the MLX build
of that name, since the CTranslate2 files would be the whole wait for weights that backend never
loads, and `--device cpu --download-model` still fetches the CTranslate2 ones, so the download and
the start that follows it agree.
Start-button launcher, with the venv's Python (`run.cmd` / `setup.cmd` and their `.sh` twins do
this themselves): `~/.shisu-ko/venv/Scripts/python server/native_host.py --register --verbose`
(`venv/bin/python` on Linux/macOS), `--status`, `--unregister`.

Docker: `docker\up.cmd`, `docker\logs.cmd`, `docker\down.cmd` (or `docker compose up -d` etc.).
`up.cmd` keeps a minimized "Shisu-ko WSL keep-alive" window open when Docker Engine runs inside
WSL, because WSL stops the distro (and Docker) seconds after the last WSL session ends.

Extension checks and browser packages:

```
for file in addon/*.js; do node --check "$file"; done
npx web-ext lint --source-dir addon
npx web-ext build --source-dir addon --artifacts-dir dist --overwrite-dest --ignore-files "tests/**"

npm ci
npm test
npm run build
npx playwright install --with-deps chromium
npm run test:browser
```

`addon/manifest.json` is the Firefox source and remains directly loadable from
`about:debugging`. `node scripts/build.mjs` derives Chrome from that source into `dist/chrome`;
it never maintains a second application copy. `npm run watch` rebuilds after edits; reload the
unpacked extension in `chrome://extensions` and reload the YouTube tab. The build writes
versioned Firefox and Chrome ZIPs and excludes `addon/tests`, dotfiles, and development metadata.
Chrome's `service-worker.js` loads `browser-api.js`, `settings.js`, `match.js`, `words.js` and
`background.js` in that order with classic `importScripts`, so settings globals retain the same
behavior as Firefox; `addon/tests/settings.test.js` and `scripts/tests/build.test.mjs` hold that
order.

Server check: `python -W error -c "import ast; ast.parse(open('server/server.py', encoding='utf-8').read())"`.

Automated tests (also run in CI via `.github/workflows/tests.yml`, no GPU/network/Firefox needed):

```
pip install -r server/requirements-test.txt && python -m pytest server/tests
node --test addon/tests/*.test.js
```

`server/tests/_serverlib.py` loads `server.py` the way this file already recommends above
(`sys.modules` registration before `exec_module`). `addon/tests/_loadBackground.js` runs
`background.js` in a Node `vm` sandbox with `browser`/`fetch`/`btoa` stubbed out — top-level
`function` declarations become sandbox properties, but `const`/`let` (`DEFAULT_SETTINGS`,
`REQUEST_TIMEOUT_MS`, `CARD_STATUS_TTL_MS`, `DECK_SEEN_KEY`, `DECK_NOTES_KEY`) need an extra
script run in the same context to expose them, since they live in the global lexical environment
rather than as globalThis properties; it loads `settings.js`, `match.js` and `words.js` first,
like the manifest. `addon/tests/_loadContent.js` does the same for `content.js` by rewriting
its IIFE to return its pure helpers (`shouldSync`, `mergeCues`, `findActiveCue`, `jumpTarget`,
`fontStack`, `modelForSync`, ...), the handlers that drive them (`sync`, `premineNow`,
`onKeyDown`, `onMineClick`, `discover`, ...), the word-colour functions (`renderText`,
`refreshWordMarks`, `pollWordIndex`, `wordColoursOn`, `syncTick`, `setSubtitle`,
`transcriptLine`, `mineCue`), the `browser.storage.onChanged` listener as `onSettingsChanged`
and the `runtime.onMessage` listener as `onCommand`; it throws if the file's shape changes. It
declares words.js's export with `var` instead of `const`, so a test can put a counting wrapper
in `sandbox.SHISUKO_WORDS` and see how often content.js asks the matcher. Its
`stubElement(tag)` keeps a child list (`appendChild`, `insertBefore`, `replaceChildren`, a
fragment that empties into its target), so a test can count the nodes a render makes and read
the transcript panel's order back, and a stub canvas yields no blob. `addon/tests/popup.test.js`
builds its fake document from `popup.html` (tags, types, range bounds, listeners a test can
fire) and plays the background with a `getSettings` / `saveSettings` pair that merges like
`background.js` and echoes the write to the storage listener.
When adding a new setting or a new pure helper, add a matching test rather than only exercising
it manually.

Load the extension for manual testing via `about:debugging#/runtime/this-firefox` > Load Temporary
Add-on > `addon/manifest.json`. The content script can also be exercised outside Firefox by
concatenating a small `browser.*` shim with `content.css` and `content.js` and running it on a page
that contains `#movie_player.html5-video-player > video` with `?v=<video id>` in the URL.

## Gotchas learned the hard way

- Windows command lines are limited to about 32 KB. Put long scripts in files instead of
  inline heredocs when running tools from a shell.
- Hugging Face's xet transfer backend stalled on Windows; the server sets `HF_HUB_DISABLE_XET=1`.
  It also sets `HF_HUB_VERBOSITY=error` and `HF_HUB_DISABLE_SYMLINKS_WARNING=1` before the
  library is imported: the Hub's "set a HF_TOKEN" nag arrives as an `X-HF-Warning` header that
  huggingface_hub logs through its own bare handler and ours (twice on screen), and Windows
  without Developer Mode gets a symlink `UserWarning` per model. Download failures still reach
  the viewer through `friendly_model_error()`.
- yt-dlp needs a JavaScript runtime (Deno preferred, Node 20+ works) for YouTube. The Docker image
  ships Deno; the native setup relies on what is installed.
- On Windows the CUDA libraries come from the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels;
  `add_nvidia_dll_dirs()` must run before `ctranslate2` is imported.
- GPU memory is often shared with games or wallpaper apps. `load_model()` reads free VRAM with
  `nvidia-smi` and picks `int8_float16` below 4.5 GB; a driver reset shows up as a process death
  without a traceback (Windows LiveKernelEvent 141). The launchers restart the server; exit code 2
  means a startup error that must not be retried. Exit code 3 asks for a restart: a broken GPU
  context, and also a failed model switch after which the previous model could not be reloaded,
  which would leave the server running without any model. Exit code 4 (`EXIT_UPDATE`) asks the
  launcher to run `update.py` before starting again; only `POST /update` produces it.
- Reordering a beam's KV cache must skip the cross-attention half. Those keys and values come from
  the audio, so every beam of one audio holds the same ones and permuting them changes nothing;
  on large-v3 the copy is about 1.2 GB per decoded token, more than the search it serves is worth
  (1.1x realtime with it, 4.1x without, both on an idle machine).
  `rearrange_self_attention_only()` in `mlx_beam.py`.
- A speed measurement taken on an idle Mac describes nothing. The Apple GPU's memory is the
  browser's memory, and the browser is where the video plays: large-v3 ran a 38 s window in 13 s
  alone and 81 s with Chrome playing one video, which is below playback speed. Every MLX figure in
  this file says whether a browser was running, and a new one must.
- YouTube's `automatic_captions` says nothing about a video's language: it lists some 157 of them
  for every video, because YouTube offers to machine-translate its own transcript into all of them.
  The uploader's declared default audio track, `info["language"]`, is the field that means
  something (`declared_language()`).
- `mlx_whisper.audio.pad_or_trim()` pads with `mx.pad`, which refuses a numpy array, so anything
  shorter than the 30 s encoder window has to be an `mx.array` first. Every language probe is
  shorter, and this broke the whole language watch in silence: a detector that raises is never what
  pauses a video, so the only sign was a line in the server log.
- macOS and Windows keep the case of a path but ignore it when looking one up, so comparing two
  path strings can call one file two (`same_file()` in `native_host.py`, used by `status_text()`:
  the very same registered wrapper used to be reported as another checkout's).
- AnkiConnect: send requests without a `Content-Type` header (a "simple" request needs no CORS
  preflight), call `requestPermission` first, find the newest card with `findNotes("added:1")`.
- `data_collection_permissions` in the manifest requires `strict_min_version` 140 or later.
- `notifications` is a required permission, not an optional one: the update notification is
  created from the start-up check, where no popup is open to ask for a grant. The GitHub check
  needs no host permission (`Access-Control-Allow-Origin: *`), but GitHub allows sixty
  unauthenticated API requests an hour per address, shared with everything else on the
  connection: hence one check a day, cached in `storage.local`, and the 403/429 text that asks
  to try again in an hour. Never add a token.
- Regular Firefox only keeps signed add-ons; unsigned builds are temporary installs only.
- Screenshots fail on DRM-protected videos (tainted canvas); the audio clip still works.
- The native server and the container both use port 8790; run one at a time.
- Firefox runs a `.cmd` native host through `cmd.exe /s /c "<host> <manifest path> <extension id>"`.
  stdout is the protocol, so the wrapper must not `echo`, `pause` or print anything (`@echo off`
  first), and the host has to accept those two arguments. Firefox keeps the host in a job
  object: a child that does not `CREATE_BREAKAWAY_FROM_JOB` dies with the host. And cmd.exe
  splits a quoted path holding `&`, `(` or `^`: name the launcher relative to `cwd`.
- `scripts/browser-smoke.mjs` waits for `#server-status` to read exactly `Server offline` and
  then `Server online`; keep those badge texts.
- `browser.downloads.download()` refuses `data:` URLs ("Access denied for URL data:...", thrown
  synchronously before any promise exists): an extension may not load a URL that inherits its
  principal. Build a `Blob`, pass `URL.createObjectURL()` from the background page, revoke it later.
- The toolbar popup's document dies with the click outside it that closes it, and a text field's
  `change` event fires on that very close: `flushSave()` in `popup.js` sends the save before any
  `await`, from `pagehide` and `visibilitychange` as well as the 150 ms debounce, and sends only
  the fields edited since the last save (`dirty`), because the form is not the only writer (the
  content script saves `enabled` and `showTranscript` for the commands) and a whole-form save
  would put back what another writer changed; `browser.storage.onChanged` brings such changes
  into the form. `saveSettings()` in `background.js` is a read-modify-write, so it runs one save
  at a time (`saveChain`): two in flight would drop a patch.
- YouTube's SPA keeps the watch page in the DOM when it leaves it: on a Short reached from a
  watch page `#movie_player` still exists, hidden, with its video, so `findPlayer()` asks for
  `#shorts-player` on `/shorts/` addresses first, or the Short would be fetched and transcribed
  on the hidden video's clock. An ad runs on a clock of its own: `/sync` still goes out during
  one (the server hears of a new video only through it, and a pre-roll ad used to hold up the
  fetch by its whole length), carrying `state.contentPlayhead`, the video's position as of the
  last request outside an ad, or the link's `t=` before the first.
- The player's focused controls (the volume slider, the settings menu, the radios and lists of
  its dialogs) take the arrow keys themselves; `KEY_SKIP_SELECTOR` names them by ARIA role,
  except the progress bar, whose arrows are the five second seek the subtitle jump replaces.
  `jumpTarget()` answers null, leaving YouTube's own seek alone, when there is no cue at all or
  the playhead and the target do not sit in the same covered range (`coveredRange()`): the last
  known line before an untranscribed stretch is not the previous line.
- Left steps one line back wherever the playhead sits in the current line: inside a line, the line
  before it; in the gap after one, that line again. It used to replay the current line past
  `CUE_REPLAY_S` (1 s) into it, asbplayer's rule, but a line runs three to six seconds, so that
  was nearly always and Left restarted what was already playing. And `leadIn()` takes the previous
  cue's end as a floor: `normalise_gaps` closes every gap under 0.5 s to 0.1 s, shorter than the
  0.15 s lead-in, so the old unclamped seek landed inside the previous line and flashed its last
  frames before sweeping back into the line the viewer had just left. The lead-in may eat silence
  and nothing else.

## Making changes

1. Keep `server.py` a single dependency-light file (stdlib + numpy + faster-whisper + yt-dlp + PyAV,
   plus mlx-whisper on Apple Silicon).
2. Bump `version` in `addon/manifest.json` and `VERSION` in `server/server.py` together.
3. Run the checks above, then test manually on a real YouTube video: subtitles appear, hover
   pauses, transcript panel works, Alt+Shift+M produces a toast and (with Anki running) fills the card.
4. Update README sections that describe changed behaviour; keep this file's invariants current.
5. Commit with a descriptive message; do not commit `.env`, `dist/` or anything from `~/.shisu-ko`.

## Release

- `npx web-ext build --source-dir addon --artifacts-dir dist --overwrite-dest --ignore-files "tests/**"` produces the zip (without the tests folder).
- `sign-addon.cmd` (repository owner only) produces a signed `.xpi` for regular Firefox.
- `publish-addon.cmd` (repository owner only) submits the build to the public listing on
  addons.mozilla.org: `docs/amo/make_metadata.py` turns `docs/amo/{summary.txt,description.md,
  release-notes.md,reviewer-notes.md}` into the metadata JSON that `web-ext sign --channel listed`
  sends. The privacy policy, icon and screenshots in `docs/amo/` are set in the Developer Hub;
  `docs/amo/README.md` is the checklist. AMO refuses a version number that was uploaded before in
  either channel, so bump before signing or publishing. Update `release-notes.md` per release.
- Attach the zip/xpi to a GitHub release; rebuild the Docker image with `docker compose build`.
