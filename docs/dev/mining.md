# Mining: one tab at a time, automatic mining, matching, pre-mining

How the add-on elects the tab that talks to the server and how a card gets its sentence, frame and clip (`addon/background.js`, `addon/content.js`, `addon/match.js`).

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
and never reaches the server, see "How word colours work" in `word-colours.md`). Every other answer clears the flag, a
refusal by the server (`{ok: false, error, data}`, a 4xx/5xx) or unreachable included: the
background answers standby only with `ok: true`, so any other answer means the election let the
request through, and a flag left by the last refused tick would hide the server's error behind
"running in another tab" and keep the pre-mining and the Anki watch closed. Such a refusal also
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

Every field the settings name (image, audio, sentence, word, pitch) is looked up through
`SHISUKO_WORDS.fieldKey(fields, name)`: the name itself, trimmed, else the one field whose name
differs from it in case alone, and written back under the note type's own spelling. Note types
disagree on case (Lapis and JPMN write `Picture` and `SentenceAudio`, Eminent `picture` and
`sentenceAudio`), Anki answers a name exactly, and a setting in the other case found nothing:
every mine into an Eminent card failed with "The new card has none of the fields Picture,
SentenceAudio". Two fields differing in case alone leave an inexact name unmatched rather than
guessed, and only the note's own names count (`constructor` is no field; `update` has no
prototype, so a field named `__proto__` is written like any other). A card with neither the image
nor the audio field is refused before any media is stored, even when its sentence field could be
extended (one of the two missing is a partial mine, as before), with `missingFieldsError()`: the
names looked for, where to change them ("Check the field names and change them in the settings if
they differ: popup > Anki, clips and server") and then the card's own field names, those that
look like a picture or audio field (`MEDIA_FIELD_NAME`) first, the rest in the note type's order,
the first `MISSING_FIELDS_SHOWN` (8). The list comes last because a toast is cut at 240
characters, and the Downloads fallback says "Saved to Downloads instead." before the Anki error
for the same reason. A note AnkiConnect no longer has (`notesInfo` answers `{}`) is "no longer in
Anki", not a settings problem.

Right after a successful `updateNoteFields`, `addToAnki()` calls `rememberDeck(url, noteId)`
without awaiting it (see "How word colours work" in `word-colours.md`): the mine's answer never waits for it, and its
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
