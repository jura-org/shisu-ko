# The phone portal

`portal/` serves a phone-sized web page from the computer that runs the server: a YouTube video
in the page's own player, Shisu-ko's cues as a scrolling list with a word-by-word highlight,
dictionary lookups through the Yomitan API, and one-tap mining into Anki. It is a separate process
with no state of its own beyond `~/.shisu-ko/portal-state.json`; everything else it composes from
services that already run on the computer.

```
phone browser ──HTTP──► portal/portal.py (127.0.0.1 / LAN / Tailscale, port 8791)
                          ├─► Shisu-ko server  127.0.0.1:8790  /sync (cues, words), /clip (sentence audio), /health
                          ├─► Yomitan API      127.0.0.1:19633 /tokenize, /termEntries, /ankiFields (fields, word audio)
                          ├─► AnkiConnect      127.0.0.1:8765  deckNames, modelNames, modelFieldNames, canAddNotes, storeMediaFile, addNote, sync
                          ├─► yt-dlp (in-process)              YouTube's HLS stream, proxied
                          └─► PyAV (in-process)                the card's picture, cut from an HLS segment
```

Files: `portal/portal.py` (the server; stdlib plus yt-dlp and PyAV, imported lazily), `portal/qr.py`
(a small QR encoder for the pairing code), `portal/static/` (the page: `index.html`, `app.js`,
`app.css`; `lib.js` holds its pure helpers and every UI string; `pair.html`, `pair.js` the pairing
page), `portal/tests/`. Launchers: `server/portal.cmd`, `server/portal.sh`, `nix run .#portal`. They
run the server's own Python environment (`~/.shisu-ko/venv`), which has yt-dlp and PyAV.

## Who may use it

Three access modes, chosen at start (`--mode`, or `PORTAL_MODE`):

| Mode | Listens on | Who gets an answer |
|---|---|---|
| `local` (default) | 127.0.0.1 | this computer only |
| `lan` | 127.0.0.1 and the LAN address (`--host` to choose; 0.0.0.0 allowed) | loopback, and devices holding a paired token |
| `tailscale` | 127.0.0.1 and `tailscale ip -4` | loopback, the tailnet devices of this computer's own Tailscale user (`tailscale status --json`), and paired devices |

`check_binds()` refuses an address that does not fit the mode: a LAN or wildcard address in local
mode, a non-Tailscale address in tailscale mode. Only lan mode, where every device must pair, may
listen on 0.0.0.0.

**Pairing (lan mode).** At start, and again after each pairing or when a code expires, the portal
prints a one-time link with a 192-bit secret, the same link as a QR code (`qr.py`), and an
8-digit code. A code works once and for 30 minutes. `GET /pair?code=` (the link) and
`POST /api/pair` (the typed code) exchange it for a device token: 256 random bits in an `HttpOnly`,
`SameSite=Strict` cookie for 400 days. Only its SHA-256 is stored (`devices` in
`portal-state.json`, with a name from the User-Agent and when it was last seen). Both pairing paths
are rate-limited: 10 tries a minute per address, 60 in all. The typed code exists because an
iPhone home-screen app keeps cookies apart from Safari, so a device paired in Safari is not paired
in the installed app. Settings lists the devices and forgets one (`POST /api/devices/forget`);
`--forget-devices` forgets every device.

<img src="../images/portal-pairing.png" width="220" alt="The pairing page: type the 8-digit code the portal shows on the computer">

**On every request**, in this order: the `Host` header must name this computer (a bind address,
localhost, its host name or `<host>.local`, its Tailscale name or addresses, or a `--host-name`);
this stops DNS rebinding. Then `authorize()`: loopback, a valid device token, or (tailscale mode)
an owner address. A refused page in lan mode is the pairing page (401); a refused API call is a 401
or 403 in JSON. Only `/pair`, `/api/pair` and the files the pairing page needs (`app.css`,
`pair.js`, the icons, the manifest) are open without a token, and those still need the Host check.
POSTs must be `application/json` and carry no foreign `Origin`, so a page on another site that the
phone visits cannot drive the portal (CSRF): it cannot send that type without a CORS preflight,
which the portal never grants, and the device cookie is `SameSite=Strict` besides.

**Threat model.** The portal assumes the computer and its user are trusted, and so are the services
on loopback. It defends against other devices on the same network (lan mode: no token, no answer),
against other people on a shared tailnet (tailscale mode: owner check), and against web pages the
phone visits (Host, Origin, JSON-only, SameSite). Traffic is plain HTTP: on a LAN anyone who can
read the network can read the pages and steal a device token; Tailscale encrypts its traffic. The
portal never proxies a URL a client names: video URLs are resolved on the computer by yt-dlp, and
the client only ever sees opaque tokens (below). Video IDs are checked against
`^[A-Za-z0-9_-]{6,20}$`; request bodies are capped at 64 KB; every upstream call has a timeout.

## The card

Which deck and note type a word goes to, and what each field holds, is set up in the page
(Settings > Anki), stored as `card` in `portal-state.json`. `~/.shisu-ko/portal.json`, when
present, overrides any of its keys (`deck`, `note_model`, `tags`, `duplicate_scope`, `fields`,
`first_field`, `sentence_field`, `default_gloss_dictionary`, and `auto_sync`, below); the page then
shows those choices as fixed. `validate_card()` checks both; an unknown key or a bad value in the file stops the portal at
start with the reason. There are no built-in defaults: until a deck, a note type and its fields are
set, the home screen says so and the first Mine opens the setup.

A field's value is a template: Yomitan markers (`{expression}`, `{reading}`, `{glossary}`,
`{audio}`, ... rendered through the Yomitan API's `/ankiFields` with Yomitan's current profile),
and the portal's own:

| Marker | Value |
|---|---|
| `{cloze-prefix}<b>{cloze-body}</b>{cloze-suffix}` | the cue's text, the tapped word in bold (all HTML-escaped) |
| `{portal-sentence}` | the cue's text |
| `{portal-picture}` | `<img>` of the frame at the cue (start + min(0.75 s, half the cue)) |
| `{portal-sentence-audio}` | `[sound:]` of the server's `/clip` for the cue's start..end |
| `{popup-selection-text}` | the definition picked in the sheet, else the first gloss of `default_gloss_dictionary` (or of the first dictionary) |

`guess_fields()` proposes a mapping from the field names, case-insensitively and in a few
languages: Picture/Image/Screenshot get the picture, SentenceAudio the sentence audio (the
extension's own defaults, `ankiImageField: "Picture"` and `ankiAudioField: "SentenceAudio"` in
`addon/settings.js`, so both tools fill the same fields), Sentence the bold sentence, Audio the word
audio, Reading, Furigana, Pitch, Frequency, Definition/Meaning/Glossary, and the word for
Word/Expression/Front or, failing all, the first field (Anki's duplicate check compares it). The
picture and the sentence audio are fetched only when a field uses them.

Media files are named like the extension's: `shisuko_<video>_<start ms>.jpg` / `.mp3`.

## The video: an HLS proxy

The page plays the video in its own `<video playsinline>` (no YouTube UI at all). yt-dlp resolves
YouTube's HLS master playlist on the computer; googlevideo URLs only work from the address that
resolved them, so the portal proxies every byte:

- `rewrite_master()` keeps H.264 variants up to 720p, best first (where iOS starts), with the AAC-LC
  audio rendition, and rewrites their URIs to `/api/hls/<token>/<key>.m3u8`; `rewrite_media()`
  rewrites each segment to `/api/hls/<token>/<key>/<n>.ts` (video, MPEG-TS) or `.aac` (audio:
  YouTube's audio renditions are packed ADTS AAC with ID3 timestamps, not TS, and a player picks its
  demuxer by the name). No upstream URL ever reaches the page.
- The token is random and names a video the portal resolved itself; it expires after 8 hours, or 2
  idle. A 403 from googlevideo re-resolves the stream under the same token, so the player never
  notices; network errors and 5xx retry with backoff.
- Segments are fetched when a player asks for them, so a paused or closed player costs nothing; the
  last 64 MB stay in memory, for re-requests and for `Range`. HTTP/1.1 keep-alive carries the
  hundreds of segment requests.
- `/api/media?video_id=&t=` resolves the stream (about 2 s) and pre-fetches the playlists and the
  segments at the start time, so a tap plays in about 0.2 s.
- The card's picture is cut with PyAV (`frame_jpeg()`, `encode_jpeg()`: decode the segment the
  phone played, encode with PyAV's own MJPEG encoder; no ffmpeg binary, no Pillow), falling back to a
  DASH stream URL when the video was not streamed through the portal.
- Live streams, a stream that cannot be resolved, yt-dlp missing, a browser without native HLS
  (desktop Firefox) or a playback error fall back to YouTube's iframe player; Settings then says
  "Using YouTube's player". That player still draws a title row and a play/pause glyph whatever
  `controls=` says; the page renders it at three times the box size, scaled down, inside a taller
  clipped frame, so most of it falls outside the picture.

Every resume (Mine, closing the sheet, the toolbar, a tap on the video) calls `play()`
synchronously inside the tap, for iOS's rule that sound may only start inside a user gesture.

## Cues, timing and language

The page heartbeats the server's `/sync` (every second while playing, every 5 s paused), always
naming the model the server already holds, so it never asks for a model switch. The proxy sends
`"words": true`, the server's opt-in for Whisper's word timings per cue (`words: [{start, end,
text}]`, and `src`, which the page ignores); a server without it sends none. A line from the
creator's subtitles carries the line's own tokens there, timed from Whisper, or no words at all when
it kept the track's times.

The subtitle list highlights each word as it is spoken (`lib.js`): with `words`, a token takes the
time of the word holding its first character, interpolated inside a word that a token starts in the
middle of; otherwise an estimate, which for Japanese spreads the cue over its tokens by mora count
(from the Yomitan reading) with pauses for 、 and 。, and for any other language evenly by letters.
The clock is the presented frame's time (`requestVideoFrameCallback`) of our own player, or
YouTube's reported time run on at the playback rate for the iframe.

The server's language (`/health`, through `/api/info`) decides the Japanese-only parts: furigana,
the mora estimate and Japanese fonts. Lines are tokenised by Yomitan (`/tokenize`); a language it
has no dictionaries for comes back without headwords, and the page splits the line itself with
`Intl.Segmenter` (or runs of letters). When Yomitan is down, Japanese lines become tappable per
character and a tap looks up the text from there on, as Yomitan's own scan does.

The scrubber above the toolbar shows elapsed and remaining time (h:mm:ss only for a video of an
hour or more) and what is loaded. It updates from the frame clock with `transform: scaleX()` on its
fill and the thumb's `translateX()`, and rewrites its labels only when the second changes. A tap
seeks; a drag (pointer capture, so neither the page nor the subtitle list's swipes see it) previews
the cue at the target in the list and seeks, once, on release, which saves the position once. It is
a `role="slider"` with the time in words as `aria-valuetext`; the arrow keys step 5 s. A live
stream shows "LIVE" instead. The list centres its line in the space above it.

<img src="../images/portal-scrubber-drag.png" width="220" alt="Dragging the scrubber: the thumb grows and the list previews the line at the target">

## Retries

- The heartbeat never stops. A failed `/sync` backs off 1, 2, 4, 8 ... 15 s with jitter and reads
  "Reconnecting…". An errored session reads "Retrying…", with the server's opt-in `retry_in`
  countdown, and is "Subtitles unavailable" only when the server says `retrying: false`.
- Lookups, tokenising and the in-deck check retry 3 times (300 ms, 900 ms, 2.7 s).
- A mine retries by itself 4 times over about 30 s. Each mine carries a job ID, and the portal
  answers a repeat of a finished job with its first result, so a lost answer never makes a second
  note; an `addNote` retried after an ambiguous failure first looks for a note with the same first
  field and sentence (`add_note_once()`). Duplicates and other 4xx answers are never retried.
- Helpers: `SCHEDULES`, `backoffDelay()`, `isTransient()`, `withRetry()` in `lib.js`; `retry()`,
  `backoff_delay()` in `portal.py`.

## The Anki sync after mining

A note the portal adds reaches AnkiWeb, and so Anki on the phone, without anyone pressing Sync.
After a successful `/api/mine` (`addNote`, its media stored first), `mine_once()` calls
`AnkiSync.card_added()`; a job the page repeats is answered from `jobs` and adds nothing. The
scheduler's thread (`run_forever()`, started by `main()`) asks AnkiConnect for `sync` when it is
due, which runs Anki's own sync with AnkiWeb, media included. The rule is the extension's
(`docs/dev/mining.md`, "The Anki sync after mining"):

- the first note after a quiet spell syncs `SYNC_FIRST_DELAY` (60 s) later, and notes mined in
  that minute ride along;
- a note mined after a sync began goes with the next one, `SYNC_REPEAT` (30 s) after that start,
  or at once when that has passed, so steady mining syncs every 30 s;
- once a sync's 30 s pass with no new note, the round is over;
- one sync at a time (`tick()` holds `running` under the lock); a note mined during one goes with
  the next.

A failed sync (`sync_failure()`) keeps its notes owed and is tried again after `SYNC_BACKOFF` (1, 2,
then 5 minutes); "sync: auth not configured" (Anki not signed in to AnkiWeb) and "Sync status ...
not one of" (a full sync to confirm in Anki) wait for the next note. Every sync and failure is one
line in the log ("anki sync: 1 new note(s), syncing", "anki sync: done in 0.4 s", "anki sync
failed: Anki is not running; trying again in 60 s"); the page shows one line under the switch.

The switch is Settings > Anki > **Sync Anki after mining**, on by default and stored as `auto_sync`
in `portal-state.json` (`GET` / `POST /api/anki/sync`, `sync_info()`, `save_sync()`); off clears
what is planned. `portal.json`'s `auto_sync` (true or false) fixes it: `read_config_file()` takes
it apart from the card keys, the page shows the switch disabled, and a `POST` is refused with 409.
The extension runs its own scheduler: when both mine at once each may sync, and Anki runs
AnkiConnect's requests one after the other.

## Positions and the home screen

Where each video was left is kept in `portal-state.json` (`positions`), shared by every device:
saved every 5 s while playing, on pause, seek, hide and leaving. Reopening resumes 2 s before it;
under 10 s, or within 30 s of the end, starts from 0; a `?t=` in the link wins (`resumeTime()`).
The home screen lists them with thumbnails (`/api/thumb/<id>`, cached on the computer) and a
progress bar, and shows what is still missing (`/api/health`: the server, Yomitan API, AnkiConnect,
yt-dlp, PyAV, and the card setup), each with what to do.

`portal-state.json` is written atomically (a temporary file and a rename).

## Tests

```
python -m pytest portal/tests          # pytest; PyAV and numpy for the picture tests (skipped without)
node --test portal/tests/lib.test.js
```

CI runs both on Linux and Windows (`.github/workflows/tests.yml`, job `portal`). The page itself
has no automated browser test; the proof of a change is the page in a 390x844 touch browser.
