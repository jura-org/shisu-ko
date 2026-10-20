# Mining: one tab at a time, the server's attach, matching

How the add-on elects the tab that talks to the server (`addon/background.js`), and how the
server gives a card its clip and picture (`Miner`, `FrameGrabber` and the matcher in
`server/server.py`). Since 0.22.0 the extension never writes to Anki.

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
back keeps its subtitles (the word colours' `cardStatus` ask keeps going: the index is the
deck's, served from the background's one cache, and never reaches the server, see "How word
colours work" in `word-colours.md`). A viewer's own mine waits in a standby tab until the tab
gets the right back, which a watched tab does on its next ask. Every other answer clears the flag, a
refusal by the server (`{ok: false, error, data}`, a 4xx/5xx) or unreachable included: the
background answers standby only with `ok: true`, so any other answer means the election let the
request through, and a flag left by the last refused tick would hide the server's error behind
"running in another tab". Such a refusal also
drops the language verdict (`languagePaused`, `heard`), as a restarted session does: the answer
says nothing about the session, `statusText()` ranks the pause above the error, and the next
real answer brings the verdict back. An ad does not hand the right on: `/sync` keeps going out
through it (see "Gotchas" in AGENTS.md), so the watched tab keeps asking and keeps the right for the ad's
length. `statusText()` in `content.js` (pure, tested) shows standby and the language pause even
with `showStatus` off, since they are the only answer to "why is nothing appearing?"; neither is
styled as an error. `statusBadge` off shows nothing at all, errors, standby and pause included:
the viewer's own choice, from the popup's switch or Alt+Shift+H (the `toggle-status` command,
`toggleStatusBadge()`, which saves the setting and toasts which way it went; the popup's header
still says how the server is). In the `ready` case it shows `No speech found in this video` only
when the covered range the playhead sits in runs from the start of the video
(`view.coveredFrom <= 0.5`: the first window opens half a second before the playhead, and a video
resumed near its end gets one window from there on and nothing before it, the server never
planning backwards) to its end
(`ahead >= view.duration - 1`, the done reading) with `view.duration > 0` and
`view.cueCount === 0` (a silent clip, an instrumental), never for `view.live`, whose cues keep
coming; `updateStatus()` passes `cueCount: state.cues.length`, `coveredFrom` (the range's start,
null outside every range) and `live: state.live || state.serverLive` in the view, the latter
read from the `/sync` answer's `live` key (the player's own API says live only in Firefox, and
not through an ad; an older server sends no key and the flag stays as it was). It is a progress
message like "Transcribing…", not an error, and obeys `showStatus`;
`addon/tests/content.test.js` covers it beside the other `statusText()` cases.

## How automatic mining works

The server mines. `AnkiWatcher` (see "How the Anki sync after mining works" in
`server-runtime.md`) looks at `findNotes("added:2")` and hands every note id it has not seen
before to `Miner.notes_added()` (its `on_new` hook), which queues them for the miner's one
worker thread (`run_forever()`). The baseline the watcher takes at start is never handed over.

How often the watcher looks is the miner's call (`Miner.poll_interval()`, the watcher's
`cadence` hook), by what the viewer is doing:

- `ANKI_POLL_ENGAGED` (1 s) while a client mining has paused, for at most
  `ANKI_ENGAGED_SECONDS` (120 s) from the pause. Yomitan's card is made on a paused video: the
  hover pause. A longer pause is reading, not mining.
- `ANKI_POLL_MINING` (3 s) while one plays. "Mining" is `candidates()`: a session synced in the
  last `ATTACH_SYNC_WINDOW` with auto on and cues to match.
- `ANKI_POLL` (15 s) otherwise.

The pause comes from `/sync`'s `paused`, a literal `true` (anything else is playing, never a
400); `App.sync()` keeps it in `Session.paused` and the time the pause began in
`Session.paused_since`, which a heartbeat does not move. A paused tab syncs only every 5 s, so
`shouldSync()` in `content.js` also sends when `paused` differs from what the last `/sync`
carried (`state.sentPaused`): one request per pause or resume. The watcher asks the cadence at
every step and wakes at least every `ANKI_POLL_ENGAGED`, so a pause counts within a second, not
after the slow look already planned. One `findNotes("added:2")` took 25 ms (median of 50; 28 ms
at most) on a collection of about 14,000 notes, most of it AnkiConnect's own turnaround.

`Miner.attach_new(note_id)` decides, and these rules keep it from touching the wrong card:

- Once. The id goes into `handled` before anything else; a note is judged once, whatever the
  verdict, and never again (the set keeps the last `MINE_HANDLED_KEEP`).
- Fresh. A note id is its creation time in milliseconds. A note made more than
  `ATTACH_NOTE_MAX_AGE` (180 s) before it was found is not the viewer's lookup.
- Watched. Only sessions (`candidates()`) whose client sent `anki` settings with `auto` on and
  synced within `ATTACH_SYNC_WINDOW` (60 s) count. No `/sync` means no candidate: the `enabled`
  switch off, a closed tab, a tab left on another video. The portal sends no `anki`, so its
  sessions are never candidates (and its cards are complete anyway).
- One lookup at a time. More than `ATTACH_MAX_BATCH` (5) new notes in one look are an import or
  a sync from another device: `notes_added()` queues none of them.
- A sentence. A card whose sentence field (the setting, else `Sentence`) is empty names no line;
  the word alone is in every video, so such a card is left alone.
- A match. `best_cue()` against each candidate's cues; the best of all wins (below). No match,
  nothing written; the debug log names the videos it tried.
- Something to do. `targets()` is the picture and audio fields that exist and are empty (the
  picture only with `--mine-picture on`); `sentence_update()` is the sentence grown to the cue
  (below). A card with neither is left alone.

`Miner.attach()` then takes the line's media made ahead (`Premine.take()`, below), or makes what
is missing on demand: it cuts the clip (`Miner.clip()`: `App.clip()` over `clip_span()`, the
client's `pad_ms`, in its `format`, waiting up to `ATTACH_CLIP_WAIT` for audio still being
fetched, since the cues can be cached and the audio not), takes the picture (`Miner.picture()`,
below), stores both with
`storeMediaFile` under `shisuko_<video>_<start ms>.mp3` / `.jpg`, and writes the field
names Anki reported back (it may lowercase a name or add a suffix). Right before
`updateNoteFields` it reads the note again and drops every field that no longer holds what it
held at the first read: Yomitan, the viewer or another tool may have changed one while the clip
and the frame were made, and that change is never overwritten. A card that gets the audio and no
picture says so in its message ("no picture"), and one whose sentence grew says "sentence
extended to the whole line". A manual mine that fails inside AnkiConnect is answered as an
`error` against its nonce, never left to the tab's timeout; a picture that fails, at the resolve
or mid-decode, costs the picture alone.

Every field the settings name is looked up through `field_key(fields, name)` (words.js's
`fieldKey`, ported): the name itself, trimmed, else the one field whose name differs from it in
case alone. Note types disagree on case (Lapis and JPMN write `Picture` and `SentenceAudio`,
Eminent `picture` and `sentenceAudio`), and Anki answers a name exactly. Two fields differing in
case alone leave an inexact name unmatched rather than guessed. A card with neither field gets
`missing_fields_error()`: the names looked for, where to change them, then the card's own field
names, the media-looking ones (`MEDIA_FIELD_NAME`) first, the first `MISSING_FIELDS_SHOWN` (8).

Whatever the server writes into an HTML field goes through `escape_html()`: the stored file
names inside `<img src="...">` and `[sound:...]`, the sentence a manual mine puts into an empty
sentence field, and every text part `grow_sentence()` puts around its own `<b>`. The newline `seam_for()` puts between two merged utterances becomes
`<br>`, since HTML would otherwise collapse it into a space.

### Media made ahead (`Premine`)

A card used to wait for its media after Yomitan made it. Measured over three mines, 6.5 to 8.5 s
went from the note's creation to its media: up to 3 s for the next look, then the clip (80 ms), a
stream resolve (1.7 s, once per video) and a frame decode (2.7 to 5.0 s, below), then four
AnkiConnect calls (about 150 ms). The viewer mines the line on screen, and every `/sync` says where
the screen is, so the server makes that line's media while it plays.

`Miner.observe(s)` runs inside every `/sync` of a session whose client mines with auto on, while
AnkiConnect answers (`Miner.anki_up`, the watcher's last look), and does no I/O: `Premine.observe()`
names the line at the playhead (`cue_at()`: the cue spanning `want_t` that started last; none in
a gap). Each video wants one line at a time, the newest named: a viewer scrubbing past ten lines
gets the one the playhead stopped on made, not a queue of ten. The one worker thread
(`anki-premine`, `run_once()`) makes the line wanted of the video synced last first:
`App.clip()` over `clip_span()` and `FrameGrabber.grab()` at `frame_time()`, a live stream's
line its clip alone. A part that fails (audio still coming, DRM,
YouTube's refusal) is kept as None, and the attach makes that part on demand with its own waits
and messages. `take()` hands the attach the line's media; a line wanted or being made is waited
for up to `PREMINE_WAIT` (5 s), quicker than making it twice.

Media is keyed by the session token, the cue's id, start and end, and the clip's `pad_ms` and
`format`, so a restarted session, a revised cue or another padding never takes another line's
media. The bounds: `PREMINE_LINES` (10) lines made per video, the newest kept, and one wanted;
`PREMINE_SESSIONS` (3) videos, the one synced longest ago dropped first; a video not synced for
`PREMINE_IDLE` (60 s), or synced with auto off, loses all of it. A line is about 30-50 KB of
MP3 and 55-160 KB of JPEG, so the whole store stays a few MB.

The cost: only lines a viewer played while mining are made, one frame decode each. Measured on
one video with Whisper idle: 0.29 CPU-seconds per line (0.2 to 0.5 s of wall time), about 8% of
one core during playback, against 1% for the same playback with auto off; about 0.8 MB read
from YouTube per frame. With the media ready an attach took 130-160 ms, and note creation to
media attached took 0.9 to 1.2 s, most of it the wait for the next look. Tests:
`server/tests/test_premine.py`; the cadence is in `test_mining.py`, on a fake clock.

### What the client sends, and what it hears (`/sync`)

The extension's popup settings travel inside every `/sync` body (`ankiForSync()` in
`content.js`): `anki: {auto, picture, audio, sentence, word, pad_ms, format}`, from `autoMine`,
`ankiImageField`, `ankiAudioField`, `ankiSentenceField`, `ankiWordField`, `clipPaddingMs` and
`clipFormat`. The server takes them as untrusted (`anki_settings()`): a field name is at most 64
characters with no control character and none of `"{}:<>`, `pad_ms` is 0-2000, `format` is
`mp3` or `wav`, `auto` a boolean; unknown keys are ignored. Valid settings are kept on the
session (`Session.anki`), the latest wins; invalid ones are not stored and the answer carries
`anki_error`, which the tab toasts once. The subtitles go on either way.

The viewer's own mine (Alt+Shift+M, the pickaxe on the subtitle or a transcript line) is
`mine: {cue_id, nonce, session}` in the same body (`mine_request()` validates it; a malformed one
is a 400). No endpoint was added for it. `content.js` keeps the request in `state.mining` and
sends it with every `/sync` until the log answers its nonce; the server queues a nonce once
(`App.queue_mine()`, `Session.mine_nonces`), so a lost answer costs a resend, never a second
write. `Miner.mine_now()` takes the cue by id, refuses one whose `session` token is no longer the
session's (the cue ids restarted), and writes it into the newest note of `findNotes("added:1")`,
filling empty fields and growing the sentence the same way, plus an empty sentence field the
settings name. It marks the
note handled, so the automatic attach leaves it alone. A tab gives a mine up when its cues are
dropped, when the server is unreachable, and after `MINE_TIMEOUT_MS` (90 s; a server older than
0.22.0 never answers).

What the server did goes into the session's log (`Miner.record()`: `Session.mined`, the last
`MINE_LOG_KEEP` entries of the last `MINE_LOG_SECONDS`, each `{seq, nonce, ok, status, message,
note}`; `status` is `attached`, `no_note`, `no_cue`, `no_fields`, `filled`, `off`, `unreachable` (no
AnkiConnect answered the mine's `findNotes`) or `error`; on `off` and `unreachable` the
extension saves the line to Downloads instead, see "The no-Anki fallback"). A
body carrying `anki` or `mined_since` is answered `mined` (the entries from the cursor on) and
`mined_next`. A cursor of `null` asks only where the log stands, so a fresh page replays nothing;
one past `mined_next` is from before a server restart and reads the whole log. The answer is read
before a mine is queued, so an outcome recorded at once (`off`: no miner) is never skipped by a
fresh cursor. A body without either field gets exactly the answer it got before 0.22.0.

`takeMined()` in `content.js` reads the log: the entry with its nonce ends the viewer's mine and
is toasted; an automatic attach that worked is toasted, one that failed is the server log's
business. The word colours need nothing from the tab: `Miner.outcome()` hands each note it
attached to to `DeckIndexes.note_mined()`, which learns the note's deck (see `word-colours.md`).

### What a mined card gets

The cue that was on screen, and nothing more. The clip runs from that cue's `start` to its `end`,
padded by `pad_ms` (`Miner.clip()`), and the picture is taken at `frame_time(start, end)`, a
little into the line. A mine by hand names the cue by id; an automatic attach takes the cue the
matcher picked. Cues are never rejoined by `seg`.

They used to be, on the premise that a Whisper segment is a sentence. It is not, and there is no
second signal to fall back on. Whisper punctuates a fluent narrator barely at all (7 marks in
3095 characters on one measured video), so every split inside such a segment came from
`--max-cue-chars` and the rejoin undid all of them: a card mined off a 3 s line got 9 s of audio
and a clause that was never displayed. The VAD is a worse witness, not a better one: Silero cuts
at 300 ms of silence, a breath, and that narrator ran 14.26 s across three cues between breaths.

Yomitan's sentence grows to the cue, never past it (`grow_sentence()`, the port of 0.21.1's
`extendSentenceField()`; `test_mining.py` holds its old cases). Yomitan cuts the sentence it puts
on a card out of the line on screen at 。！？, so a cue holding two sentences gives the card one
of them. When the card's sentence, normalised as the matcher does, is a strict piece of the cue,
the field gets the cue's text, escaped, with Yomitan's `<b>` around the same word (plain when the
word cannot be found in the raw text). Anything else, the whole cue already or text the viewer
wrote, is left alone. It is the one write into a filled field, and it is safe for the same
reason: only text that the cue itself contains is ever replaced, and only by that cue.

A merged cue can hold two utterances either side of the newline `seam_for()` put between them,
and Yomitan ends its sentence at that newline, so the grow stops at the row the fragment came
from. `match_normalize()` strips whitespace, so the seam is invisible to the comparison and
`grow_sentence()` splits on it itself; without that it put the other speaker's line on the card,
which is the one thing the seam exists to prevent.

### Matching a card to its subtitle (`best_cue()` in `server.py`)

A port of the extension's `match.js` from 0.21.1, with its thresholds; `server/tests/test_mining.py`
holds every case `addon/tests/match.test.js` held.

- `match_normalize(text)` removes ruby readings first (`_RUBY`: an `<rt>` or `<rp>` up to the
  next end tag; Yomitan's `{sentence-furigana}` writes `<ruby>食<rt>た</rt></ruby>べる`, which
  stripping the tags alone would leave as 食たべる), then strips HTML tags, decodes `&nbsp; &amp;
  &lt; &gt; &quot; &#39;`, removes bracket furigana (` 食[た]べる`), all whitespace (JavaScript's
  `\s`, U+FEFF included) and punctuation. The word colours read a card's word by the same ruby
  rule (`words.js`); `test_mining.py` holds `words.test.js`'s ruby shapes.
- `match_similarity(card, spoken)` takes the card's text first, and the order matters. It is 1
  when the spoken text contains the card's sentence whole, however short (`_contains()`: the
  server merges a short cue into its neighbour, 嘘でしょ。本当にそんなことがあったの, and Yomitan
  stops at the 。), or when the card's sentence contains the spoken text and that text is a real
  share of it (`_comparable()`: at least 6 characters, or half of the longer one); otherwise the
  Dice coefficient of their character-bigram sets, 0 unless comparable. `MIN_SIMILARITY` is 0.6.
  Dice, not the overlap coefficient: dividing by the smaller set alone passes two sentences that
  merely end the same way (別の字幕です against これはテスト字幕です scores 0.6).
- `best_cue(cues, note, rank, t)` returns `(cue, score, share)`: cues below the threshold are out,
  a cue containing the note's word gets `WORD_BONUS` (+0.2), and ties break on `_share()`, then on
  `rank(cue)`, then on distance from `t`. `_share()` is 1 for a cue that runs past the sentence at a
  point where Yomitan cuts (`_cut_from()`: a whole piece of the cue's text between 。！？!?．… or a
  newline, or between quotes, 「」『』 and the straight ones; Yomitan never cuts at 、), else the
  shorter text's length over the longer one's, so a whole line beats a fragment of itself and a
  line that is the sentence beats one holding it mid-clause. `match_cue()` is its cue alone.
- The attach passes no `rank` and the session's playhead (`want_t`) as `t`. The extension used to
  rank the sentences it had pre-mined and the transcript line under the pointer first; the server
  has neither signal, and `_share()` already separates the cases the ranking was for.
- Across sessions, `attach_new()` keeps the candidate with the highest `(score, share)` and, on a
  tie, the one synced last. Each candidate is matched with its own settings, since the sentence
  and word field names are per client.
- It stays O(n): containment is tried on every cue first and bigrams only if nothing contained.

### The picture (`FrameGrabber`)

The frame comes from YouTube's stream, not from the browser. `FrameGrabber.resolve_stream()` asks
yt-dlp, with `Fetcher.ytdlp_options()` (the cookies and JavaScript runtime the audio fetch uses) and
`FRAME_FORMAT` (H.264 up to 720p, else the best up to 720p), for one stream URL; `frame_jpeg()`
opens it with PyAV, seeks to the time and encodes the first frame at or after it with PyAV's own
MJPEG encoder (`encode_jpeg()`, the portal's code). It opens the stream with FFmpeg's `request_size`
(`FRAME_REQUEST_SIZE`, 256 KB): FFmpeg otherwise asks for an open-ended range (`bytes=N-`), and
googlevideo trickles those at about 300 KB/s, which made one frame 2.7 to 5.0 s of waiting; bounded
ranges arrive at full speed, 0.2 to 0.6 s a frame. 1 MB requests were as fast and read 2 MB a frame.
FFmpeg before 8 ignores the option and is slow again. A URL is kept per video for `FRAME_STREAM_TTL`
(4 h). A verdict (`FrameUnavailable`: DRM via `has_drm`, a live stream, no stream up to 720p) is
kept as long, so it costs one resolve, not one per card; any other failure (YouTube's bot check, the
network) is kept for `FRAME_RETRY` (10 min) and then tried again. A kept URL that no longer opens
and is older than `FRAME_FRESH` (120 s) has expired: one fresh resolve, and that answer stands. A
live stream gets no picture: its past frames are not there to seek to. `--mine-picture off` builds
the miner without a grabber, and nothing is resolved.

The extension's pre-mined frames and clips (the background's `premined`) are gone with it; the
server makes the same media ahead itself (`Premine`, above).

### The no-Anki fallback

With no Anki to mine into, the viewer's mine (Alt+Shift+M or a line's pickaxe) saves the line
to `Downloads/shisu-ko-mining` instead, as 0.21 did. `mineCue()` in `content.js` draws the
playing video onto a shared canvas when the viewer mines (`captureFrame()`: 1280 px wide at most,
JPEG 0.85; null for a DRM-tainted canvas or a video with no frame yet) and keeps it in
`state.mining`. When the server's answer for that nonce is `unreachable` (no AnkiConnect
answered its `findNotes`) or `off` (the server mines nothing), `takeMined()` sends `saveMine`
`{videoId, cue: {start, end}, frame}` to the background, and the toast says what was saved.
`saveMine()` in `background.js` refuses a video id outside `^[A-Za-z0-9_-]{6,20}$` or a cue that
is not a forward span, asks the server's `/clip` for the cue plus `clipPaddingMs` on each side in
`clipFormat` (`fetchClip()`, 20 s), and saves `shisuko_<video>_<start ms>.jpg` and `.mp3` /
`.wav` through `downloadBytes()`: a Blob and an object URL in Firefox (which refuses `data:` URLs
there), revoked after two minutes, a `data:` URL in Chrome's service worker, which has no
`URL.createObjectURL` (Chromium saved a 20 MB one whole; the longest clip, 60 s of WAV, is
7.7 MB as base64). Each file is saved on its own; what it saves it saves, and what is missing it names, with the reason.
An answer that lands after `enabled` went off ends the mine and saves nothing. This is the only use of
`downloads`, which stays a permission because the published set holds it.

### Switches

`--mine auto|off` (`resolve_mining()`): off never writes to Anki. Inside the Docker image the
default is off unless `--anki-url` names an Anki, as for the sync: 127.0.0.1 there is the
container. `start_anki_watcher(args, app)` starts the miner's thread (`anki-mine`) beside the
watcher's (`anki-sync`); `--anki-sync off` with mining on runs the watcher for the miner alone
(`AnkiWatcher.sync_on` False, and `/health`'s `anki_sync` says `off`), and so does the word
colours' deck thread (`anki-deck`, `resolve_word_colours()`) with both off. A mine asked of a
server whose mining is off is answered `off`.

## The Anki sync after mining

The add-on does none. The server watches Anki and asks it to sync after new notes, whoever made
them (`AnkiWatcher` in `server/server.py`; `server-runtime.md`, "How the Anki sync after mining
works"). 0.20.0 and 0.21.0 ran a scheduler here, kept alive across a stopped event page by the
`alarms` permission; that permission held 0.21.0 at the Chrome Web Store's review, and the
add-on is a thin client of the server. `addon/tests/settings.test.js` holds that no file in
`addon/` names the action `sync`, `alarms` or the old `ankiAutoSync` setting, which a viewer's
storage may still hold and nothing reads, that none names `updateNoteFields`,
`storeMediaFile`, `addNote`, `mineTarget`, `mineFallbackDownload` or the old matcher, and that
`browser.downloads` is `saveMine()`'s alone. The browser smoke test runs with that stale setting stored, mines through its fixture
server's `/sync`, and sees no write and no `sync` reach its fixture AnkiConnect; with its
fixture answering a mine `unreachable`, it waits for the fallback's toast and two complete
downloads (a JPEG and the fixture's WAV from `/clip`); the attach
itself is tested in `server/tests/test_mining.py`.
