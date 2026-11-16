# How Shisu-ko works

```
Browser extension (addon/)                       Local server (server/), http://127.0.0.1:8790
+----------------------------------+             +------------------------------------------------+
| content script on youtube.com    |  POST /sync | 1. yt-dlp downloads the audio track once       |
|  - video id, playhead, model 1x/s| ----------> |    (a live stream: follows its audio segments) |
|  - Anki field names, a mine,     | <---------- | 2. PyAV decodes it to 16 kHz mono              |
|    the deck token (word colours) |   new cues, | 3. Silero VAD + faster-whisper transcribe      |
|  - renders cues as DOM text      |   mined log,|    windows at the playhead, then ahead of it   |
|  - hover pauses, Yomitan scans   |   the deck's| 4. Miner: a new card's line audio + a frame    |
+----------------------------------+   rows      | 5. the deck index: one deck's words and states |
                                                 +------------------------------------------------+
      Anki  <-- the server, through AnkiConnect (http://127.0.0.1:8765); the extension never calls it:
                findNotes every 15 s (3 s while mining, 1 s paused), storeMediaFile + updateNoteFields
                on a new card's empty fields (--mine), sync a minute after new notes (--anki-sync), and
                findNotes + notesInfo on the word colours' deck every 30 s while a tab shows them
```

Why a local server instead of running the model in the browser: Whisper large-v3 has 1.5
billion parameters and needs a GPU, which a browser extension cannot use well. The extension
therefore only sends the video id, the current playhead and the wanted model once per second,
and the server does the work with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(CTranslate2). On Apple Silicon it runs the same weights through
[MLX](https://github.com/ml-explore/mlx) instead, because CTranslate2 has no Metal backend and
would leave Whisper on the CPU.

**Scheduling.** When you open a video the server fetches the audio track with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), decodes a minute around the playhead while the
download is still running, and transcribes a short 20-second window there so the first subtitles
appear quickly. It then continues in 30-second windows up to 3 minutes ahead of you on an NVIDIA
GPU, and up to 15 minutes on an Apple GPU or the CPU, which refill more slowly and use a pause to
get ahead. A sentence cut at a window edge is dropped and re-transcribed at the start of the next window, so lines are
never chopped. Seeking to an untranscribed part starts a new short window there. Only the tab you
are looking at is served: the extension elects one, and the others are answered without the server
being asked at all.

Where the first subtitle's seconds go, measured on an M1 Pro with large-v3-turbo and nothing else
running: yt-dlp reads the video's metadata in 6.1 s cold and 2.1 s warm (it solves YouTube's
JavaScript challenge, and the first video after a server start pays the cold price), the first
bytes and the preview decode take about 1 s, voice activity detection 0.06 s, and the 20-second
first window 3.2 s. Eleven seconds cold, seven warm.

**Language.** Whisper is told which language to expect (`--language`, default Japanese), and told
that, it will gladly turn an English talk into Japanese subtitles. Two things stop it, and YouTube
goes first. YouTube states every video's default audio track. When that is not the subtitle
language, the server refuses the video before it downloads a byte of it, and the badge says so a
second after you open it. When it is the subtitle language, the server believes it and never
listens, which saves about a second per window. Only for a video YouTube states nothing about does
the server ask Whisper what each window's speech actually was; once `--language-patience` seconds
of speech (60 by default) have gone by without the subtitle language being heard, it stops
transcribing that video. It keeps listening to every window it would have transcribed, at a tenth
of the cost, and starts again the moment the language returns; a video that opens with an English
introduction loses nothing. One misjudged window never costs a subtitle, because until the patience
runs out every window is transcribed anyway. Nothing a paused video has merely listened to is
recorded as transcribed, so a pause that was wrong costs a second listen and never a blank video.
`--language-patience 0` switches off both the check on YouTube's word and the listening, and
transcribes everything.

**Cues.** The server runs Silero voice activity detection on each window and drops what Whisper
makes up over silence and music: segments without words, segments that barely overlap detected
speech, repetition loops and a short blocklist of known hallucinated phrases. Whisper's word
timestamps then split the rest into subtitle-sized cues at sentence ends, long pauses and a
character and duration limit; starts snap to the onset of speech, ends get a short lead-out into
the following silence, fragments are merged and gaps under half a second are closed. Characters
that belong to no text are dropped, cached cues included: Whisper now and then spells a private-use
code point out of raw bytes, which Firefox showed as a small box with a hex code in it. Singing is
not speech to the voice-activity detector, so a window in which it hears next to nothing (under a
second of speech) but the audio is not silent (a song, a 歌枠) is transcribed without it once
Whisper hears Japanese in it, under stricter gates on Whisper's own confidence: music videos get
their lyrics, and an instrumental may show a wrong line now and then (`--lyrics off` sends such
windows through the detector as before, so a song stays blank). A loud stretch beside speech that
the detector missed (a song after its announcement, a shriek) gets the same check, and then the
whole window is transcribed without the detector. When a Japanese video carries subtitles its
creator uploaded (never YouTube's automatic captions, and never a track that reads as their
output), each line Whisper heard the same way shows the creator's text and line breaks, timed from
Whisper's words; where the two disagree, or the track has no line, Whisper's own cue stays, and a
line over music or speech Whisper missed keeps the track's times (`--human-subtitles off` turns
this off). The reasoning and the measurements behind these rules are in
[docs/subtitle-quality.md](subtitle-quality.md). Cues are saved per video and per model in
`~/.shisu-ko/cache`, so a video you have watched before shows subtitles immediately: the
current model's cues are `<video_id>.cues.json`, and when you switch models another model's
cues are kept beside it and come back the moment you switch back.

**Updates.** The extension makes no request beyond your own machine. The server makes one for
updates: `GET https://api.github.com/repos/Multysquid/shisu-ko/releases/latest`, at most once a
day while the checks succeed (soon after a start whose last check is over a day old, then every
24 hours; after a failure, again in 15 minutes, an hour, then every six hours). It carries no
account, token, cookie or identifier, only a `User-Agent` naming Shisu-ko and its version, and
what any request carries (your IP address). GitHub's answer (the release's version, page, `.xpi`
address and date, and when it was checked) is kept in `~/.shisu-ko/update-check.json` and
reported to the extension in `/health`'s `latest`; the extension keeps the last one in its
storage for while the server is offline. `--update-check off` or `SHISUKO_NO_UPDATE_CHECK=1`
stops the request. The **Update** button then sends `POST /update` to the local server, which
exits with code 4 so that `run.cmd` / `run.sh` run `update.py` and start it again; the server
never downloads anything itself, and the extension never installs itself. The browser updates it
with requests of its own: Firefox from addons.mozilla.org, and Chrome, for a Chrome Web Store
install, from the store.
