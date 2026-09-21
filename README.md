# Shisu-ko

[![Tests](https://github.com/Multysquid/shisu-ko/actions/workflows/tests.yml/badge.svg)](https://github.com/Multysquid/shisu-ko/actions/workflows/tests.yml)

Live Japanese subtitles for YouTube in Firefox and Chrome: transcribed on your own machine by Whisper,
readable with [Yomitan](https://yomitan.wiki/), and mined into Anki without pressing a key.

![Shisu-ko on a YouTube video: hovering a subtitle pauses the video, Yomitan looks up シルバーウィーク, adding the card makes Shisu-ko attach the frame and the sentence audio, and Anki shows the finished card](docs/demo.gif)

Hover a line and the video waits. Look the word up, add the card, and Shisu-ko fills it with the
frame you were reading and the audio of the sentence. The same recording
[with sound](docs/demo.mp4).

YouTube's Japanese captions are often missing, wrong, or burned into the picture where no
dictionary can reach them. Shisu-ko runs OpenAI's Whisper large-v3 on your GPU, keeps
transcribing a little ahead of where you are watching, and draws the result over the player as
ordinary page text. Everything runs locally: the only network traffic is yt-dlp fetching the
audio from YouTube, the one-time model download, and a look at GitHub for a newer release,
by `run.cmd` / `run.sh` before each start and by the extension once a day.

## What it does

- **Live subtitles**, starting a few seconds after a video opens. The server transcribes ahead
  of the playhead and caches every cue, so seeking back or rewatching is instant.
- **Live streams too** (Firefox). The server follows the stream's audio a little ahead of where
  you are watching, so a stream gets the same subtitles, transcript and mining as a video.
- **Dictionary-friendly text.** Subtitles are real DOM text, so Yomitan or any popup dictionary
  scans them. Hovering pauses the video, the dictionary popup keeps it paused, and moving back
  over the video resumes it.
- **Transcript panel** with every line so far. A timestamp jumps there, a pickaxe mines it.
- **Sentence mining by itself.** The moment Yomitan adds a card, a screenshot and an MP3 clip of
  the line you were reading go into it. The pickaxe on a subtitle or a transcript line, or Alt+Shift+M,
  does the same on demand, into the newest card or into your Downloads folder.
- **Word colours** (optional). With Anki running, every word of a line that has a card in your
  deck is coloured by the card's state, green to red, and can carry an overbar in the colour of
  its pitch accent, read from the card. The deck follows your mining, and a verb is found in its
  conjugations.
- **Your hardware, your model.** Setup asks whether you want Whisper large-v3 or small and
  downloads it; it runs on an NVIDIA GPU, on an Apple Silicon GPU through MLX, or on the CPU,
  whichever the server finds. The popup switches to any other model without restarting the
  server: a faster-whisper size or a Hugging Face repo id of a CTranslate2 model, such as
  `kotoba-tech/kotoba-whisper-v2.0-faster` (Japanese-specialised, about 6x faster) or a small
  CPU model. `--model` only sets the default.
- **Native, Nix or Docker.** A one-time setup script on Windows, Linux and macOS, a Nix flake,
  or a container with GPU support. All of them share the same model folder. When the native
  server is not running, a button in the popup starts it (Firefox).

## Requirements

- Firefox 140 or newer, or Chrome 120 or newer.
- For the native server: Python 3.10 or newer (on Windows `setup.cmd` takes the `py` launcher
  that the python.org installer registers, or a `python` that really runs), plus Node.js 20+
  or Deno (yt-dlp needs a JavaScript runtime for YouTube). On Nix the flake provides all of this.
- For the Docker server: Docker with the NVIDIA Container Toolkit (Docker Desktop on Windows
  has it built in). The image already contains Deno.
- A GPU for large-v3: an NVIDIA card with about 4 GB of free VRAM (with less the server switches
  to int8 weights by itself), or any Apple Silicon Mac, where the server decodes on the Mac's own
  GPU through MLX. Without either, pick the small model at setup and run on the CPU.
- Optional: [Yomitan](https://yomitan.wiki/) for lookups, [Anki](https://apps.ankiweb.net/)
  with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on for mining.

## Quick start

### 1. Start the server

Get the code: `git clone https://github.com/Multysquid/shisu-ko` (updates itself on every
start), or download the zip from the [latest release](https://github.com/Multysquid/shisu-ko/releases/latest)
and **extract it** (right-click, *Extract All...*): the scripts refuse to run from inside the
zip, where Windows would start them without the rest of the files.

**Windows:** double-click `server\setup.cmd` once, then `server\run.cmd`.
**Linux/macOS:** `bash server/setup.sh` once, then `server/run.sh`.

Setup creates an isolated Python environment in `~/.shisu-ko/venv` and installs faster-whisper,
yt-dlp, the CUDA runtime libraries and, on Apple Silicon, mlx-whisper; nothing else on the system
is touched. It then asks which Whisper model the server should use, `1` for large-v3 (best
quality, about 3 GB, wants a GPU with 4 GB or more free) or `2` for small (about 500 MB, fine on
a CPU, less accurate), downloads it into `~/.shisu-ko/models` with a progress bar and remembers
the choice in `~/.shisu-ko/config.json`. When it says that everything is ready, close its window
and start `run.cmd` / `run.sh`. The choice is kept even when the download fails or is stopped with
Ctrl+C: the first start then downloads the chosen model itself, without the progress bar. The
server is ready when it prints `Listening on http://127.0.0.1:8790`. Keep the window open while
you watch; it restarts itself if it ever crashes.

**On Apple Silicon** the transcription runs on the Mac's own GPU, with no option to set:
CTranslate2 has no Metal backend, so the server decodes through MLX instead and `--device auto`
picks it as soon as `setup.sh` has installed mlx-whisper. Model names do not change — `large-v3`
in the popup is the same weights either way, setup downloads the MLX build of whichever model you
pick, and both backends share one cue cache. On an M1 Pro, over three minutes of Japanese news
audio, large-v3 transcribed at 7.3x realtime on the GPU against 2.3x on the CPU, and left the
cores to the video. What you give up is beam search: MLX has none, so decoding is greedy and gets
a word wrong now and then where the CPU would not — three lines in those three minutes.
`--device cpu` buys the beam search back at a third of the speed.

From then on the toolbar popup can start it for you: while the server is offline, the status
line in the popup's header shows a **Start server** button. The first click asks Firefox for
permission to "exchange messages with programs other than Firefox"; allow it, and the button
launches `server\run.cmd` in a window of its own (Windows) or `server/run.sh` in the background
with its output in `~/.shisu-ko/server.log` (Linux/macOS), then waits for the server to answer.
The button passes no options: the server starts with its defaults (the model chosen at setup,
else large-v3; the GPU when there is one; no cookies), exactly as a bare `run.cmd` / `run.sh`
start would. The popup's model field switches the model once that default one is up; anything
else you usually append to `run.cmd` / `run.sh` (`--device cpu`, `--cookies-from-browser`,
`--js-runtime`, ...) needs a start by hand, see [Server options](#server-options). Firefox only
for now: Chrome wants the installed extension's id in the launcher's manifest. Docker and Nix
users start the server as before.

Setup registers that launcher with Firefox, and so does every `run.cmd` / `run.sh` start. An
existing install therefore gets the button after one or two starts by hand: the start that
updates Shisu-ko to a version with the button still runs the old launcher, so it is the start
after the update that registers; running `setup.cmd` / `setup.sh` once is the sure way, and
`run.cmd --check` says whether the launcher is registered. To take the registration away again,
for example before deleting the checkout or if Firefox should not be able to start anything, run
`~/.shisu-ko/venv/Scripts/python server/native_host.py --unregister` (`venv/bin/python` on
Linux/macOS; any Python 3 works, the host is standard library only). It removes
`~/.shisu-ko/native-messaging/shisuko.json` and the `HKCU\Software\Mozilla\NativeMessagingHosts\shisuko`
registry key on Windows, `~/.mozilla/native-messaging-hosts/shisuko.json` on Linux and
`~/Library/Application Support/Mozilla/NativeMessagingHosts/shisuko.json` on macOS; delete
those by hand if the checkout is already gone. `--status` shows the current state.

Every start first looks for a newer Shisu-ko: a git clone is fast-forwarded to the branch it
tracks, a folder downloaded as a zip is replaced with the newest release, changed Python
requirements are installed, and a changed extension is pointed out (reload it in Firefox or
install the new `.xpi`). Local changes are never overwritten, and being offline just starts
the current version. `run.cmd --no-update` (or `SHISUKO_NO_UPDATE=1`) skips the check.

**Updates.** A server that keeps running would never see a new release, so the add-on looks for
one itself: it asks GitHub for the newest release once a day, when Firefox starts or the popup
opens and the last check is older than that or failed (offline, the next opening tries again),
and whenever you click **Check for updates** in the popup's *Anki, clips and server* drawer.
When the server you are running is older, the toolbar icon gets a badge and the popup shows a
banner with an **Update** button (**Not now** hides it until Firefox restarts). A system
notification says so too, once per browser session, but only from the check at Firefox start,
and only when the server is already running at that moment and can update itself; a check from
the popup sets the badge and the banner and never notifies, so with the usual order (Firefox
first, the server later) there is no notification. Update, or a click on the notification,
makes the server exit and its launcher take over: `run.cmd` / `run.sh` run `update.py` (a git
clone is fast-forwarded, a zip install is replaced with the newest release) and start the server
again, while the popup's status line reads "Updating server" until the new version answers,
which can take up to two minutes because the restart loads the model again. A server that
`run.cmd` / `run.sh` did not start (Docker, Nix, `python server.py` by hand, or a `run.sh` from
before 0.9.0 that has not been restarted since it updated itself; `run.cmd` picks its new loop
up by itself) or that was started with `--no-update` cannot update itself; the banner then says
the server was not started by `run.cmd` / `run.sh`, whichever of those the cause is (the server
only reports that it cannot), and asks for a restart by hand, which
updates as before. The extension itself is never installed by the add-on: once the listing on
addons.mozilla.org is live Firefox updates it from there, and until then the release page has
the signed `.xpi`, which the banner links to when only the extension is behind. Being offline
costs one failed check, shown under **Check for updates**; a failed check never notifies.

**Nix / NixOS:** `nix run github:Multysquid/shisu-ko` (or `nix run .` in a checkout) starts the
server with CUDA support; `nix run .#check` prints diagnostics; `nix develop` opens a shell with
Python, web-ext, Node and Deno for development. The flake takes CTranslate2 with CUDA from the
`cache.nixos-cuda.org` binary cache, so add it to your substituters or expect a long build. To
keep the server running in the background: `systemd-run --user --unit=shisu-ko nix run /path/to/shisu-ko`.

**Docker:** copy `.env.example` to `.env`, set `DATA_DIR` to where models and caches should
live, then run `docker\up.cmd` (Windows) or `docker compose up -d`. See [Docker](#docker) below.

### 2. Install the extension

Temporary install (until Firefox restarts):

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…** and choose `addon/manifest.json`.
3. Firefox asks for access to youtube.com the first time you open the popup; click **Allow on
   YouTube** (or right-click the toolbar icon > Always Allow on www.youtube.com).

Permanent install: download the signed `shisu_ko-<version>.xpi` from the
[latest release](https://github.com/Multysquid/shisu-ko/releases/latest) and open it in Firefox.
Regular Firefox only keeps signed add-ons; the release workflow signs each tagged version
through addons.mozilla.org (unlisted channel, nobody else sees it), and `sign-addon.cmd` does
the same for a local build with a free
[addons.mozilla.org API key](https://addons.mozilla.org/developers/addon/api/key/). Firefox
Developer Edition, Nightly and ESR can instead load the unsigned zip with
`xpinstall.signatures.required` set to `false` in `about:config`. The popup says when a newer
release is out (see [Updates](#1-start-the-server)); the `.xpi` is installed by hand until the
listing on addons.mozilla.org is live. Since 0.9.0 the extension needs one more permission,
"Display notifications to you": opening the new `.xpi` over an older version lists it in the
install prompt, and an automatic update (from the listing, once it is live) is held back by
Firefox until you approve it, from the notice on the application menu (≡) or under Add-ons
and themes.

Chrome development uses the same source. Run `npm ci` and `npm run build:chrome`, then open
`chrome://extensions`, enable Developer mode, and choose **Load unpacked** on `dist/chrome`.
After edits, run `npm run watch`; reload the extension on that page and reload the YouTube tab.
The Firefox source remains directly loadable from `addon/manifest.json`. `npm run build` writes
both unpacked trees and `dist/shisu-ko-<version>-{firefox,chrome}.zip`. For a Chrome release,
download `shisu-ko-<version>-chrome.zip` from the [Chrome release](https://github.com/Multysquid/shisu-ko/releases/latest),
unzip it, and choose **Load unpacked** on the extracted folder. This ZIP is unsigned and is not a
Chrome Web Store install; it has no automatic updates. Keep the extracted folder and reload the
extension from `chrome://extensions` after updates. Chrome shortcuts are under
`chrome://extensions/shortcuts`.

### 3. Watch

Open any YouTube video. The badge in the top-left corner of the player goes from "Fetching
audio…" through "Decoding audio…" to "Transcribing…", and the first subtitles appear after a few
seconds. From then on the server stays ahead of you. The toolbar popup holds every setting, and
the switch in its header turns the whole extension off and on again.

Only the video you are watching is transcribed. Other YouTube tabs say "subtitles are running in
another tab" and take over the moment you click into them, so two open videos never compete for
the GPU. A video whose speech is not in the subtitle language stops after about a minute of it
("the speech is not in the subtitle language") and starts again by itself when the language
comes back.

| Shortcut | Action |
|---|---|
| Alt+Shift+S | Turn Shisu-ko on or off (the switch in the popup header) |
| Alt+Shift+L | Toggle the transcript panel |
| Alt+Shift+M | Mine the current sentence (screenshot + audio) |
| ← / → | Jump to the start of the previous / next subtitle. In a gap between lines, Left goes back to the line that just ended. Where nothing is transcribed yet, and before the first subtitle arrives, the keys keep YouTube's five second seek. Can be turned off in the popup |

Shortcuts can be changed in Firefox under Add-ons and themes > Manage Extension Shortcuts, or in
Chrome at `chrome://extensions/shortcuts`.

## Reading with Yomitan

Hover a subtitle and the video pauses while the pointer is on the text. Scan words with Yomitan
as usual: when the pointer moves into Yomitan's popup the video stays paused, and it resumes
about a third of a second after the pointer comes back over the video. Space or the play button
resume it too. The transcript panel is plain text as well, so earlier lines can be looked up,
and its timestamps seek the video.

## Sentence mining

Mining captures two things for the line you are looking at: a screenshot of the video frame and an
MP3 clip of that line's audio, cut from the original track with a little padding on both sides.
The line on screen is the whole of it — the card's sentence and its clip always describe the same
seconds you just read and heard. Where Yomitan copied only part of the line, because it stopped at
a 。 inside it, the sentence field is grown back to the line, keeping the bold around the word you
looked up.

Both are prepared while you watch. Each line that plays has its clip, and with auto-attach on its
frame, made ready in the background, so making a card attaches them at once, and still attaches
them after the line has gone from the screen. Nothing is written to disk; a handful of recent
sentences are held in memory and dropped when you leave the page.

**Anki (default).** Normally you never trigger mining at all:

1. Hover the subtitle; the video pauses.
2. Scan the word with Yomitan and click its **+** (Yomitan fills word, reading, sentence and
   glossary from its own template).
3. Within a second Shisu-ko notices the new card and uploads `shisuko_<video>_<time>.jpg` and
   `.mp3` to Anki's media folder, writing `<img src=...>` and `[sound:...]` into the fields. A
   toast on the player confirms it. The screenshot is the frame that was on screen when you
   hovered the line, and playback is left alone so you can keep reading the popup.

The card is matched to the line it is about. Shisu-ko compares the card's sentence and word
against the transcript rather than assuming you looked up whatever is on screen now, so a card
made a few lines later, or while the video kept playing, still gets the right frame and the right
audio. A card that matches no subtitle is left alone and says so.

Shisu-ko only touches a card that appeared while you were watching, one card at a time, and only
when the card's sentence matches the subtitle, so an import, a sync or a card made elsewhere is
never overwritten. When it cannot reach Anki it stays quiet and writes nothing. Turn the watching
off with **Auto-attach to new Yomitan cards** in the popup; the shortcut and the pickaxe keep
working either way and attach to the newest card added today.

To mine by hand instead:

- hover the subtitle and click the pickaxe that appears at its right edge,
- press Alt+Shift+M while watching,
- click the pickaxe on any line in the transcript panel (the video briefly jumps to that line to
  grab the matching frame, then jumps back).

The first time, Anki shows a dialog asking whether to allow the extension; click **Yes**. Field
names default to `Picture` and `SentenceAudio`, as used by common Japanese mining note types;
change them in the popup to match yours. An optional sentence field is filled with the subtitle
text only when it is empty, so it never overwrites what Yomitan wrote. An optional word field
names the field holding the expression, used to tell two similar lines apart and, by the
[word colours](#word-colours), to read each card's word; left empty, the note's first field is
read. If Anki is not running,
mining by hand saves the files to Downloads instead (can be turned off).

**Downloads.** With **Send screenshot and audio to** set to Downloads, the files land in
`Downloads/shisu-ko-mining/`, ready to drag into any card.

DRM-protected videos block screenshots; the audio clip still works. The Shisu-ko server uses
port 8790 precisely so that AnkiConnect can keep its default 8765.

## Word colours

Two optional colourings, both off by default, both in the popup's **Word colours** section. They
need Anki running with AnkiConnect, the same as mining, and share its permission dialog: click
**Yes** once. Nothing leaves your computer; the extension only reads your deck.

**Colour words by their Anki card** colours each word of a subtitle line, and of the transcript
panel, by the state of its card: green for a card you have learned (in review), yellow for one you
are still learning, orange for a suspended card, red for a new one. A word with no card keeps the
text colour. The cards come from one deck. Left on *Automatic*, that is the deck your last mined
card went to: nothing is looked up until you have mined a card, and the first mine then names the
deck (the hint under the **Deck** select says which, or "no card mined yet"). Choose a deck in
the select to look at that one instead; its subdecks count. Words are taken from the note's word
field (the popup's **Word field**, else the note's first field), and a verb or adjective is found
in its usual conjugations and in its noun form: a card for 食べる colours 食べました,
食べたことがある and 食べ in 食べに行く, 書く colours 書かない and 書いて, 美しい colours
美しかった, 勉強する colours 勉強している and the bare 勉強, 終わる colours 終わり, and a word
written in kana is found in its forms too (かける colours かけて, しまう colours しまった,
おいしい colours おいしかった). The particles after a coloured word and the honorific お or ご
before it take its colour (お風呂の, 中で, 学生です), and so does the quotative って or と between
a coloured word and one found after it (話しかけていただくっていう with いう in the deck), so a
line reads in whole pieces. A word is not coloured inside a compound (食べ物 for 食べる, 日本語
for 日本, 走者 for 走る), and a card for a particle, the copula or an auxiliary (は, のは, から,
でも, だ, です, ます, ない, たい, ん …) never colours anything by itself, since it would paint every
line the same way. Two cards for one word show the one with the least progress; a suspended card
only counts when there is no other.

**Overbar by pitch accent** draws a bar over each word that has a card, in the colour of its pitch
accent pattern: blue heiban, red atamadaka, orange nakadaka, green odaka (the colours Migaku and
Yomitan use). The pattern is read from the card's pitch accent field in whichever form Yomitan
wrote it: a category name (`{pitch-accent-categories}`), a position such as `[2]`
(`{pitch-accent-positions}`) or the drawn graph (`{pitch-accents}`). A position needs the word's
mora count to tell odaka from nakadaka; it comes from the graph, else from the card's reading
field (a field named reading or furigana, not the sentence's), else from the word itself when it
is kana, and without any of them the word counts as nakadaka. The field is found by itself: the
first one whose name contains "pitch" or "accent" and holds a readable value, unless you name one
under **Pitch accent field** in the *Anki, clips and server* drawer. Verbs and adjectives, which
Yomitan files under kifuku, get no bar rather than a wrong one. Both colourings can be on at once:
the text colour is the card's state, the bar its pitch.

The deck is looked at again every 30 seconds while a video is open, and only the lines whose
colours changed are redrawn, so a card you review in Anki changes colour within a minute and a
card you have just mined shows red within seconds. The words stay ordinary page text, so Yomitan
scans across the colours as before. When Anki is closed the colours stay as they were last read,
or off when nothing was read yet; a deck that no longer exists colours nothing; either way the
hint under the deck select says what stands in the way, and nothing is ever toasted on the video.

## Live streams

A live stream has no audio track to download, so the server fetches the stream's audio segments
one by one instead, starting just behind where you are watching and then keeping pace with the
live edge, and transcribes each stretch of new audio as it arrives. YouTube's player normally
plays 10-40 seconds behind the live edge, which is the head start the transcription needs; on a
low-latency stream the subtitles can trail the sound by a few seconds. The extension reads the
stream's own clock from the player, so the cues stay aligned whatever your latency is and after
seeking back into the stream. That clock is read from the player through Firefox's
`wrappedJSObject`, which Chrome does not have, so live streams are Firefox only; videos work
in both browsers.

The last 15 minutes of audio stay in memory for seeking back and for mining. Live cues are not
cached, because the recording YouTube publishes afterwards runs on a different clock; when a
stream ends and comes back as a video, Shisu-ko starts over on the video's clock. Streams with
DVR disabled cannot be followed, since the server needs the numbered audio segments.

## Settings

![Popup, dark theme](docs/images/popup-dark.png)

The switch in the header is the master switch. Off means nothing happens on YouTube pages: no
overlay, no requests to the server, no Anki watching, no word colours and no key handling, until
it is switched on again (Alt+Shift+S flips it too). The status line beside the switch says whether the server
answers, and with which model and device; while it does not answer, a **Start server** button
on that line launches it with the server's default options (Firefox, see
[Start the server](#1-start-the-server)); the model field below takes effect once it is up. The
same page opens as the add-on's preferences under Add-ons and themes > Shisu-ko.

A banner under the header appears when a newer release than the running server (or than this
extension) is out: "Shisu-ko 0.9.0 is available — the server runs 0.8.0." with **Update**, which
restarts the server on the new version through its launcher, and **Not now**, which hides the
banner for this browser session; the toolbar badge stays until the server is current. A server
that cannot update itself (Docker, Nix, a start by hand, `--no-update`) gets a note instead of
the button: the banner says the server was not started by `run.cmd` / `run.sh` in every one of
those cases, since the server only reports whether it can update, not why not (a `--no-update`
server's own reason sits in its 409 answer, which the popup never asks for without the
button). While the server is offline there is no banner at all, since its next start
updates it anyway. When only the extension is behind, the banner links to the release page. The
**Check for updates** link in the *Anki, clips and server* drawer asks GitHub now, whatever the
age of the daily check, and the line under it keeps the result ("Newest release: 0.9.0, checked
3 min ago", or why the check failed).

| Setting | Meaning |
|---|---|
| Pause video while hovering a line | Needed for comfortable Yomitan lookups |
| Left/Right jump between subtitles | Arrow keys move between cues instead of seeking five seconds |
| Transcript panel | List of all cues so far, with jump and mine buttons |
| Auto-attach to new Yomitan cards | Watches AnkiConnect and fills the new card by itself; off means Alt+Shift+M or the pickaxe |
| Colour words by their Anki card | Colours each word of a line by the state of its card in the deck below: green learned, yellow learning, orange suspended, red new; other words keep the text colour. Needs Anki with AnkiConnect, see [Word colours](#word-colours) |
| Deck | The deck whose cards are looked at. Automatic means the deck your last mined card went to; nothing is looked up before a card was mined or a deck chosen. The hint under it names the deck, or says what stands in the way |
| Overbar by pitch accent | Draws a bar over each word that has a card, in the colour of its pitch accent pattern: blue heiban, red atamadaka, orange nakadaka, green odaka, read from the card's pitch accent field |
| Font size, keep line after speech | Presentation; the linger time keeps short lines readable |
| Hide YouTube's own captions | Avoids two subtitle layers |
| Show progress messages on the video | The status badge; errors are always shown |

Subtitle style lives in its own drawer. The screenshot shows mincho, a raised position, a lighter
box, an outline, and the transcript docked left:

![Overlay with custom style](docs/images/overlay-styled.png)

| Style setting | Meaning |
|---|---|
| Height above the bottom | Where the subtitle box sits, 2-40% of the player height; it still drops when YouTube's controls fade out |
| Font | Gothic (the default stack), Gothic bold, Rounded or Mincho, for the subtitle and the transcript |
| Font family | Any font installed on this computer, by name. It goes in front of the preset's stack, so the preset stays the fallback and still decides the weight. The popup previews the result and says whether the name resolved; Firefox cannot list installed fonts, hence the suggestions instead of a menu |
| Text colour | Colour of the subtitle text |
| Background | Opacity of the black box behind the text, 0-100% |
| Outline the text | Black outline instead of the box; readable over bright video with the background turned down |
| Transcript panel side | Docks the panel right or left; the subtitle moves out of its way |
| Reset style | Restores the seven settings above and nothing else |

The **Transcription model** drawer picks the Whisper model the server runs. The field takes a
faster-whisper size (`large-v3`, `large-v3-turbo`, `distil-large-v3`, `medium`, `small`, ...)
or the Hugging Face repo id `owner/name` of a CTranslate2 model
(`kotoba-tech/kotoba-whisper-v2.0-faster`); empty means the server's own default (`--model`, else
the model chosen at setup in `~/.shisu-ko/config.json`, else large-v3), which the placeholder
shows. The sizes mean the same on an Apple GPU, where the server fetches the MLX build of those
weights; a repo id must then be an MLX one (`mlx-community/whisper-large-v3-mlx`).
Models already downloaded are offered as suggestions. The change applies while
a video plays: a model that is not on disk yet is downloaded from Hugging Face first, while the
current model keeps subtitling, and once the swap is done the video's transcript starts over with
the new model. Meanwhile the badge on the video says "Loading model X…"; a name the server cannot
use shows "Shisu-ko: model X: …" with the reason, even with progress messages off, and the previous
model keeps running. The popup mirrors this under the field: the status badge says "Loading model"
during a switch, and the hint under the field carries the server's verdict on the name you typed.

The last drawer, **Anki, clips and server**, holds where mined material goes (Anki's newest
card or the Downloads folder, with an optional Downloads fallback when Anki is unreachable), the
AnkiConnect URL, the image, audio, sentence, word and pitch accent field names (the last one
empty means the first field named pitch or accent that holds a readable value), the audio
padding around the sentence, the clip format (MP3 or WAV), the Shisu-ko server URL (default
`http://127.0.0.1:8790`) and the **Check for updates** link with the result of the last check.

## How it works

```
Firefox (addon/)                                 Local server (server/), http://127.0.0.1:8790
+----------------------------------+             +------------------------------------------------+
| content script on youtube.com    |  POST /sync | 1. yt-dlp downloads the audio track once       |
|  - video id, playhead, model 1x/s| ----------> |    (a live stream: follows its audio segments) |
|  - renders cues as DOM text      | <---------- | 2. PyAV decodes it to 16 kHz mono              |
|  - hover pauses, Yomitan scans   |   new cues  | 3. Silero VAD + faster-whisper transcribe      |
|  - screenshot via <canvas>       |  GET /clip  |    windows at the playhead, then ahead of it   |
+----------------------------------+ ----------> | 4. /clip cuts sentence audio from the source   |
        |                                        +------------------------------------------------+
        | AnkiConnect (http://127.0.0.1:8765): storeMediaFile + updateNoteFields on the newest card;
        | findNotes + notesInfo on one deck for the word colours
        v
      Anki
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
appear quickly. It then continues in 40-second windows up to 15 minutes ahead of you. A sentence
cut at a window edge is dropped and re-transcribed at the start of the next window, so lines are
never chopped. Seeking to an untranscribed part starts a new short window there. Only the tab you
are looking at is served: the extension elects one, and the others are answered without the server
being asked at all.

**Language.** Whisper is told which language to expect (`--language`, default Japanese), and told
that, it will gladly turn an English talk into Japanese subtitles. So the server also asks it what
each window's speech actually was, and once `--language-patience` seconds of speech (60 by default)
have gone by without the subtitle language being heard, it stops transcribing that video. It keeps
listening to every window it would have transcribed, at a tenth of the cost, and starts again the
moment the language returns; a video that opens with an English introduction loses nothing. One
misjudged window never costs a subtitle, because until the patience runs out every window is
transcribed anyway. Nothing a paused video has merely listened to is recorded as transcribed, so
a pause that was wrong costs a second listen and never a blank video.

**Cues.** The server runs Silero voice activity detection on each window and drops what Whisper
makes up over silence and music: segments without words, segments that barely overlap detected
speech, repetition loops and a short blocklist of known hallucinated phrases. Whisper's word
timestamps then split the rest into subtitle-sized cues at sentence ends, long pauses and a
character and duration limit; starts snap to the onset of speech, ends get a short lead-out into
the following silence, fragments are merged and gaps under half a second are closed. Singing is
not speech to the voice-activity detector, so a window in which it hears next to nothing (under a
second of speech) but the audio is not silent (a song, a 歌枠) is transcribed without it once
Whisper hears Japanese in it, under stricter gates on Whisper's own confidence: music videos get
their lyrics, and an instrumental may show a wrong line now and then (`--lyrics off` sends such
windows through the detector as before, so a song stays blank). The reasoning and the measurements behind these rules are in
[docs/subtitle-quality.md](docs/subtitle-quality.md). Cues are saved per video and per model in
`~/.shisu-ko/cache`, so a video you have watched before shows subtitles immediately: the
current model's cues are `<video_id>.cues.json`, and when you switch models another model's
cues are kept beside it and come back the moment you switch back.

**Updates.** The one request the extension makes beyond your own machine is
`GET https://api.github.com/repos/Multysquid/shisu-ko/releases/latest`: when Firefox starts (or
the extension is installed or updated) or the popup opens and the last check is over a day old
or failed, so at most once a day by itself while the checks succeed (a failure is tried again
at the next of those occasions), and on every click of **Check for updates**. It carries no account, token,
cookie or identifier, only what any visit to GitHub carries (your IP address and the browser's
user agent); GitHub's answer (the release's version, page and `.xpi` address, and when it was
checked) is kept in the extension's storage. The **Update** button then sends `POST /update` to
the local server, which exits with code 4 so that `run.cmd` / `run.sh` run `update.py` and start
it again; the server never downloads anything itself, and the extension never installs itself.

## Server options

Append options to `run.cmd` / `run.sh`, or put them in the `command:` line of `compose.yaml`.
The popup's **Start server** button runs `run.cmd` / `run.sh` without any of them, so it always
starts the defaults below (the model can still be switched from the popup afterwards); a server
that needs `--device cpu`, cookies or another option is started by hand. Without `--model` the
server runs the model chosen at setup (`~/.shisu-ko/config.json`), else large-v3.

| Option | Effect |
|---|---|
| `--model kotoba-tech/kotoba-whisper-v2.0-faster` | Default model (here the Japanese-specialised distilled one, about 6x faster and lighter on memory than large-v3). The popup overrides the default with any faster-whisper size or Hugging Face repo id, without a restart |
| `--model large-v3-turbo` | OpenAI's faster large model as the default |
| `--model small --device cpu` | CPU-only operation |
| `--download-model small` | Download the model now, with a progress bar, and make it the default of later starts (what setup runs after its environment check); exits instead of starting the server, with code 2 on a failure or Ctrl+C, which `run.cmd` / `run.sh` do not restart on |
| `--device mlx` | Decode on an Apple Silicon GPU through MLX, about three times the CPU speed but without beam search. `auto` already picks it when there is no NVIDIA GPU and mlx-whisper is installed; `--device cpu` is the way back to beam search |
| `--compute-type int8_float16` | Halves GPU memory use; chosen by itself when less than 4.5 GB is free. MLX knows `float16` (the default there) and `float32` only |
| `--cookies-from-browser firefox` | Age-restricted or members-only videos, or when YouTube asks for a sign-in |
| `--cookies /path/cookies.txt` | Same, with an exported cookies file (use this inside Docker) |
| `--lookahead 0` | Transcribe to the end of the video instead of stopping 15 minutes ahead |
| `--window 60` | Longer windows are slightly more efficient, shorter ones react faster to seeking (default 40) |
| `--max-cue-chars 26` | Characters per cue before it is split (default 30; 26 is the Netflix Japanese limit) |
| `--max-cue-seconds 7` / `--min-cue-seconds 0.8` | Longest and shortest cue (defaults 7, Netflix's own maximum, and 0.8); shorter ones are extended or merged |
| `--initial-prompt "こんにちは。今日は、いい天気ですね。"` | Nudges Whisper towards punctuated output |
| `--language-patience 60` | Seconds of speech in another language before a video's subtitles stop (0 = never listen for it, transcribe everything) |
| `--lyrics off` | Transcribe a window in which the speech detector hears under a second of speech with the detector as before (blank when it heard nothing). The default `auto` transcribes such a window without the detector when its audio is not silent (sung lyrics, speech over music) and Whisper hears the target language in it, under stricter gates |
| `--idle-minutes 30` | Release the decoded audio of a video nobody has synced for this long |
| `--retry-after 30` | Seconds before a failed audio fetch is retried, and the wait before a model name that failed to download or load is tried again |
| `--js-runtime deno` | JavaScript runtime for yt-dlp: auto, node, deno, bun, or name:path |
| `--allow-remote-ejs` | Lets yt-dlp fetch updated YouTube challenge-solver scripts from GitHub |
| `--check` | Print environment diagnostics (CUDA, MLX and the backend `--device auto` would pick, yt-dlp's JavaScript runtime, downloaded models, whether the popup's Start button has its launcher registered) and exit |
| `--no-update` | Start without looking for a newer version of Shisu-ko first (`run.cmd` / `run.sh`). The popup's **Update** button is refused too, since the launcher would restart the server without updating |

`run.cmd` / `run.sh` set `SHISUKO_LAUNCHER=1` for the server they start. Only with it does
`POST /update` (the popup's **Update** button) answer yes: the server then exits with code 4,
which the launchers read as "run `update.py`, then start again" (0 stops the loop, 2 is a
startup error, anything else restarts after 5 s). Under Docker, Nix or a `python server.py` by
hand the variable is missing, `/health` reports `launcher: false` and `/update` answers 409;
those servers are updated the way they were started.

Endpoints, for anyone building on the server: `GET /health` (`model`, `default_model`,
`model_loading`, `model_error` as `{model, error, names}` or null, where `names` lists every
spelling of the failed model (aliases and repo id), `models` with the models downloaded so far,
device, compute type, language), `POST /sync` (`{video_id, url, t, paused, since, model}`
returns new cues and covered ranges plus `model` (the loaded one), `model_loading` (the name being
prepared or swapped in) and `model_error`, the last judged for the name this request asked for
and null for any other; a model switch answers with a new session token, so the client starts
over; for a live stream `t` is the stream's media clock, `getProgressState().current` in
YouTube's player, and the reply carries `live: true`), `GET /clip?video_id=&start=&end=&format=mp3|wav`,
`GET /sessions` for debugging, and `POST /update` (body ignored), which answers
`{ok: true, restarting: true, version}` and then exits with code 4 for the launcher, or 409
`{ok: false, error}` with the reason when nothing would update it (no launcher, `--no-update`,
`SHISUKO_NO_UPDATE`); `/health` carries `version` and `launcher`, true when `/update` would
work. The server only listens on 127.0.0.1 and answers browser requests
only from the extension itself or from pages served on this machine, so an arbitrary website
cannot drive downloads and transcription; `/update` is narrower still and takes browser
requests from the extension alone, never from a page, so nothing served on this machine can
restart the server. A model name is validated (a size alias or
`owner/name`, never a path) and resolved through faster-whisper's own download before anything
is loaded, so a request can never point the server at a local folder.

## Docker

```
docker\up.cmd      build (first time) and start in the background; restarts after crashes
docker\logs.cmd    follow the server log
docker\down.cmd    stop
```

On Linux/macOS use `docker compose up -d`, `docker compose logs -f`, `docker compose down`.
Settings live in `.env` (copy `.env.example`): `DATA_DIR` is the host folder for models and
caches and `WHISPER_MODEL` the default model; the popup can switch the container to another
model, which is downloaded into `DATA_DIR`. Point `DATA_DIR` at the native setup's `~/.shisu-ko`
to share the downloaded models. `compose.cpu.yaml` is a CPU-only variant.

The image is `python:3.12-slim` plus the pip-installed CUDA libraries and Deno, about 2 GB. The
GPU driver comes from the host through the NVIDIA Container Toolkit. Two ways to get that on
Windows:

- **Docker Desktop** with the WSL 2 backend (GPU support is built in).
- **Docker Engine inside WSL 2**, without Docker Desktop: run once
  `wsl -d Ubuntu -u root -- bash -c "tr -d '\r' < /mnt/c/<path to this folder>/docker/install-docker-wsl.sh | bash -s -- <your WSL user>"`.
  The `docker\*.cmd` wrappers detect which of the two is installed. Do not run both.
  WSL stops a distro a few seconds after its last session closes, which would take Docker and the
  server down with it, so `up.cmd` opens a minimized "Shisu-ko WSL keep-alive" window; leave it
  open while you watch, and close it (or run `down.cmd`) when you are done.

The extension does not change between native and Docker; both listen on `127.0.0.1:8790`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `run.cmd` or `setup.cmd` says it is running on its own, or Python cannot open `...\Temp\...\shisu-ko-main.zip\...\server\server.py` | The zip was opened in Explorer and the script double-clicked inside it, so Windows extracted only that one file into a temporary folder. Extract the whole zip (right-click, *Extract All...*) and start `server\run.cmd` from the extracted folder. |
| `setup.cmd` says "Python was not found; run without arguments to install from the Microsoft Store" | Windows answers `python` with a shortcut to the Store when no Python is on the PATH, and the setup used to trust it. Since 0.10.2 the setup runs the candidates instead (`py -3`, `python`, `python3`) and takes the first Python 3.10+ that works; with an older `setup.cmd`, install Python from python.org with "Add python.exe to PATH" ticked, or turn `python.exe` off under Settings > Apps > Advanced app settings > App execution aliases. |
| No subtitles until the toolbar icon is clicked | Firefox has not granted access to youtube.com yet. Open the popup and click **Allow on YouTube**. |
| Nothing happens on YouTube at all | Check the switch in the popup header; Alt+Shift+S may have turned Shisu-ko off. |
| Badge says "subtitles are running in another tab" | One video is transcribed at a time. Click into this tab, or close the other one. |
| Badge says "the speech is not in the subtitle language" | The server heard a minute of another language and stopped; it starts again when the subtitle language returns. For a video that really does mix languages, start the server with `--language-patience 0`. |
| Badge says "No speech found in this video" | The whole video, from its start, was transcribed and nothing was heard: a silent clip, an instrumental, a song Whisper does not hear as Japanese, or, with `--lyrics off`, any song. |
| A music video shows no subtitles | Since 0.11.3 a window the speech detector hears next to nothing in (under a second of speech) is transcribed without it when its audio is not silent and Whisper hears Japanese in it, so sung lyrics appear. A video watched before that gets its lines on the next visit: the server transcribes it again where nothing was heard (a result saved by 0.11.2) or from the start (older results); nothing needs deleting from `~/.shisu-ko/cache`. A song Whisper does not hear as Japanese stays blank, as does loud non-speech (rain, a crowd, an engine). A server started with `--lyrics off` transcribes such windows with the detector as before, so a sung one stays blank. |
| Badge says "Shisu-ko server offline" | Start `server\run.cmd` or `docker\up.cmd`, or click **Start server** in the popup. Check the server URL in the popup. |
| **Start server** says "launcher not registered" | Run `server\setup.cmd` (Windows) or `bash server/setup.sh` once, or start the server by hand once: `run.cmd` / `run.sh` register the launcher with Firefox on every start. `run.cmd --check` prints "Start button launcher: registered at …" once it is. |
| **Start server** says "Allow Shisu-ko to talk to its launcher …" | Firefox's permission prompt was declined. Click the button again and allow "Exchange messages with programs other than Firefox"; the permission is also under Add-ons and themes > Shisu-ko > Permissions and data. |
| **Start server** says "No answer from the server after 90 s" | The launch did not lead to a listening server, or the server is still downloading or loading its model: the button starts the defaults, so a first start downloads the default model unless setup already did (large-v3 is 3 GB), and a CPU load takes minutes. Look at the server window (Windows) or `~/.shisu-ko/server.log` (Linux/macOS) before clicking again; clicking again while it loads is harmless, the launcher reports it as already starting. If the server is up but the popup still says offline, the server URL under Anki, clips and server points elsewhere. |
| The banner or **Update** says the server cannot update itself | The server was not started by `run.cmd` / `run.sh` (Docker, Nix, `python server.py` by hand: it has no launcher to run `update.py` after the exit), was started with `--no-update` or `SHISUKO_NO_UPDATE`, or is a 0.8.0 server, which predates the button. The banner names the first of those causes whatever the actual one, because the server only reports that it cannot. Update it the way it was started: `docker compose build`, `nix run` with the new revision, or a plain restart of `run.cmd` / `run.sh`, which updates before every start. A `run.sh` that updated itself from before 0.9.0 keeps running its old loop, so its first server is refused too; restart `run.sh` once by hand (`run.cmd` reads its new loop as soon as it has updated and needs no restart). |
| "The server restarted but still runs X; look at its window: update.py said why" | The launcher ran `update.py` but it could not update: local changes git would overwrite, a diverged branch, a detached HEAD, no network, or a release zip that could not be downloaded. Its message is in the server window (Windows) or `~/.shisu-ko/server.log` (Linux/macOS); fix that and click **Update** again, or update by hand (`git pull`, or unpack the release). |
| **Update** ends with "No answer from the server 120 s after the update" | The server exited for the update but nothing answered within two minutes: the new version is still loading its model (a CPU load takes minutes, and a new default model is downloaded first), or it did not start (the new version crashed, or `update.py` could not reinstall the requirements). Look at the server window (Windows) or `~/.shisu-ko/server.log` (Linux/macOS). A server that is still loading answers by itself in a while and the popup's status line follows; otherwise fix what the log says and click **Start server**, which is back on the status line. |
| No update notification although GitHub has a newer release | The notification comes from one place only: the check when Firefox starts (or the extension is installed or updated), and only when the server is already running at that moment, started by `run.cmd` / `run.sh` and able to update itself. With the usual order, Firefox first and the server started later by hand or with **Start server**, there is none: the popup's own check (on opening, when the last one is over a day old or failed, and on **Check for updates** in the Anki, clips and server drawer) sets the toolbar badge and the banner but never notifies, and a profile that has never opened the popup checks nothing on its own. The line under **Check for updates** says why a check failed: offline, or GitHub's limit of sixty unauthenticated requests an hour per address (shared with everything else on your connection that asks GitHub's API). The notification is shown once per browser session; **Not now** hides the banner until Firefox restarts, and the toolbar badge stays either way. |
| Firefox keeps the old extension after an update, or its menu button shows a notice that Shisu-ko requires new permissions | 0.9.0 adds the `notifications` permission, and Firefox holds an update that adds a permission until you approve it: open the notice on the application menu (≡) or Add-ons and themes and allow "Display notifications to you". A `.xpi` opened by hand asks in its install dialog instead. |
| Server says "Another server is already starting or running on port 8790 (it holds ~/.shisu-ko/server-8790.lock). Stop it first." and stops (exit code 2) | A second server was started while one is loading or running: `run.cmd` double-clicked twice, or a start by hand while a server the popup launched is still in its update check (the **Start server** button itself looks at the lock first and reports a loading server as already starting). Close the window and let the first one finish; it holds the lock until it exits, and the file needs no cleaning up. If no server is running and the message persists, a stale `server.py` process still holds it; end that process (Task Manager, `pkill -f server.py`). |
| First start sits at "Loading Whisper model" for a long time | A model that setup did not download (large-v3 is 3 GB) is fetched at your connection speed, without a progress bar in the server window; `setup.cmd` / `setup.sh` and `server.py --download-model NAME` show one. Hugging Face's xet transfer mode is disabled because it stalled on Windows; set `HF_HUB_DISABLE_XET=0` to try it. |
| "Loading model X…" stays on the video for a long time | A model picked in the popup is downloaded first, at your connection speed (large-v3 is 3 GB, small about 500 MB); the current model keeps subtitling meanwhile, and the transcript starts over once the new one is in. The popup's status line follows along. |
| "Shisu-ko: model X: unknown model size" or "… was not found on Hugging Face" | The name in the popup's Transcription model field is not a faster-whisper size or an existing `owner/name` repo. Fix the name there; the previous model keeps running meanwhile. |
| "Shisu-ko: model X: … not a CTranslate2/faster-whisper model" | The repo holds a PyTorch checkpoint, not converted weights. Convert it with `ct2-transformers-converter`, or pick a `*-ct2` or `faster-whisper` repo of the same model. |
| Server log says it has no model left and exits with code 3 | A switch failed and the previous model could not be reloaded either (usually GPU memory). The launcher restarts the server on its `--model`; fix or clear the name in the popup. |
| Popup says "X was not found on this computer; the preset is used" or that a font name is letters, digits, spaces, dots, hyphens and underscores | Install the font, or type its family name exactly as the operating system lists it. Quotes, commas and other punctuation are refused; in both cases the preset font applies until the name resolves. |
| "yt-dlp needs Node.js or Deno" | Install [Node.js](https://nodejs.org/) 20+ or [Deno](https://deno.com/), then restart the server. |
| "YouTube asks for a sign-in" | Restart with `--cookies-from-browser firefox` (native) or `--cookies /data/cookies.txt` (Docker). |
| Downloads fail after a YouTube update | Update yt-dlp: `~/.shisu-ko/venv/Scripts/python -m pip install -U yt-dlp` (Windows) or the `bin/python` equivalent; or start with `--allow-remote-ejs`. |
| "This live stream offers no audio segments (DVR may be disabled)" | The streamer turned DVR off. Nothing can be done until the stream is published as a video. |
| "The live stream has ended" | Reload the page once YouTube shows the recording; the server starts over on the video's clock. |
| Server says "Only N MiB of GPU memory is free" or restarts by itself | Other programs (games, Wallpaper Engine, VR software) hold most of the VRAM. The server switches to int8 weights; with under about 2.5 GB free the display driver can reset under load (Windows logs LiveKernelEvent 141). Close GPU-heavy apps or type `kotoba-tech/kotoba-whisper-v2.0-faster` into the popup's model field. Cached cues survive restarts. |
| CPU fallback, transcription far too slow | `run.cmd --check` should list one CUDA device; update the NVIDIA driver or type `small` into the popup's model field. On a Mac, `run.sh --check` should end with `Backend for --device auto: mlx`; if it says MLX is not usable, run `bash server/setup.sh` again (mlx-whisper needs Apple Silicon, not an Intel Mac). |
| A word is wrong that the same model got right on another machine | MLX has no beam search, so on an Apple GPU Whisper decodes greedily and slips on a word now and then. Start with `--device cpu` for beam search at about a third of the speed. |
| Mining says "AnkiConnect denied access" | Click **Yes** in the dialog Anki shows, then mine again. |
| Mining says the card has none of the fields | Set the image/audio field names in the popup to the fields of your note type. |
| No screenshot, only audio | The video is DRM-protected; the browser refuses to read its frames. |

`run.cmd --check` prints diagnostics, including whether the Start button's launcher is registered;
`GET http://127.0.0.1:8790/sessions` lists active sessions.

## Limitations

- On a live stream the subtitles can only be as early as the transcription of the audio behind
  the live edge; with YouTube's low-latency setting they may trail the sound by a few seconds.
  Streams with DVR disabled cannot be followed, and live streams work in Firefox only.
- Subtitles are hidden while YouTube plays ads.
- YouTube changes its player regularly; yt-dlp usually needs an update within days.
- Whisper occasionally hallucinates on music or silence; the voice-activity gates remove most of
  it but not all. Songs are transcribed without the speech gate (see `--lyrics`), so an
  instrumental passage may show a wrong line now and then; loud non-speech (rain, a crowd, a
  song in another language) stays blank rather than being guessed at.

Planned: a distilled, smaller Japanese model that could eventually run in the browser itself.

## Development

```
addon/                Firefox extension (Manifest V3, plain JS, no build step)
  content.js          overlay, sync loop, hover-pause, transcript, live clock, mining trigger
  background.js       server proxy, settings, AnkiConnect watching, Downloads handling, server start,
                      release check (GitHub), badge and notification, the server's update
  settings.js         the one place settings and their defaults are declared
  match.js, words.js  shared by background.js and content.js: matching a card to its subtitle;
                      the word colours (reading a note's word and pitch, finding words in a line)
  popup.*             settings UI with the server status, the Start and Update buttons and the update
                      banner, also the preferences page
  tests/              Node tests for background.js, popup.js, match.js, words.js and the pure helpers
                      of content.js
server/
  server.py           HTTP server: yt-dlp + faster-whisper (MLX on Apple Silicon) + live
                      follower + clip cutting
  setup.cmd/.sh       setup, downloads the model     run.cmd/.sh   start (with auto-restart)
  update.py           self-update run by run.cmd/.sh, first and after the server exits with code 4
                      (POST /update): git fast-forward or newest release
  native_host.py      native-messaging host behind the popup's Start server button (stdlib only);
                      native-host.cmd/.sh wrap it for Firefox; --register writes the host manifest
  tests/              pytest suite                   tools/        cue statistics, re-transcription
docker/               Windows wrappers for docker compose and the WSL engine installer
docs/                 subtitle-quality.md, screenshots, the demo recording, amo/ (store listing)
Dockerfile, compose.yaml, compose.cpu.yaml, .env.example, flake.nix
sign-addon.cmd        signs the extension through addons.mozilla.org for self-distribution
publish-addon.cmd     submits a version to the public listing on addons.mozilla.org
AGENTS.md             architecture notes, invariants and gotchas for contributors and coding agents
```

Checks:

- Extension: `for file in addon/*.js; do node --check "$file"; done`, `npx web-ext lint --source-dir addon`,
  `npx web-ext build --source-dir addon --artifacts-dir dist --ignore-files "tests/**"` (the
  tests folder is not shipped).
- Browser packages: `npm ci`, `npm test`, `npm run build`, `npm run watch`, and `npm run test:browser`.
  `nix build .#addon` and `nix build .#addon-chrome` build the two release packages. Refresh committed PNG
  icons after changing `addon/icons/icon.svg` with `npm run refresh-icons` (ImageMagick required).
- Server: `python -W error -c "import ast; ast.parse(open('server/server.py').read())"`,
  `server/run.cmd --check`. The planning, cue-building and live-follower code is pure and easy
  to unit test by importing `server.py` as a module (register it in `sys.modules` first because
  of the postponed annotations).
- Nix: `nix develop` gives the Python environment, `web-ext`, Node and Deno; `nix run .#tests`
  runs both test suites; `nix build .#addon` produces the extension zip.
- Data lives in `~/.shisu-ko` (override with `SHISUKO_HOME`): `venv/`, `models/`, `cache/`,
  `config.json` (the model chosen at setup), the instance lock `server-8790.lock` (one per port,
  held while a server runs), `server.log` (the output of a server the popup started,
  Linux/macOS) and, on Windows, the launcher's host manifest `native-messaging/shisuko.json`.

Tests cover the pure logic on both sides, need no GPU, network or Firefox, and run in CI on
every push and pull request via [`.github/workflows/tests.yml`](.github/workflows/tests.yml):

```
pip install -r server/requirements-test.txt
python -m pytest server/tests
node --test addon/tests/*.test.js
```

The browser smoke test uses a local fixture page and does not open a real video. The shared
development commands are `npm ci`, `npm test`, `npm run build`, `npm run watch`, and
`npm run test:browser`. Before the first browser test, run `npx playwright install chromium`
(`npx playwright install --with-deps chromium` on Linux). Set `CHROMIUM_PATH` to use an existing
Chromium executable instead. The browser test also runs an isolated AnkiConnect fixture; it does
not change your Anki collection.

The server suite covers window planning, interval merging, cue building and the hallucination
gates, the preview decode, the live-stream buffer and follower (driven by a fake source and
clock), fetch retries, the origin policy, session tokens and the on-disk cue cache.
`test_model_switch.py` drives a model switch with a fake faster-whisper: name validation and
aliases, the download beside the working model, the swap and session restart, every failure path
and cooldown, the per-model cache files and what `/health` and `/sync` report. `test_mlx.py` does
the same for the Apple GPU with MLX faked in as well: which backend `--device auto` picks, the
model names shared with faster-whisper, the download of the converted weights, the voice-activity
pass the wrapper runs in mlx-whisper's stead, and a model switch on that backend — no Mac needed.
`test_native_host.py` drives the native host behind the Start button: the message framing, the
two commands and every malformed request, the launch on each platform with a recorded `Popen`,
the instance lock shared with `server.py`, registration into a temporary home with a fake
registry, the host over a real pipe, and the wrapper and launcher scripts.
`test_update_endpoint.py` drives `POST /update` over a real socket: the launcher variable and
the two no-update switches, the 409s, the answer followed by the exit with code 4 through
`main()`, the origin rule that admits the extension and no page, and the launchers' `:update`
label, variable and code-4 lines. The extension
suite runs `background.js` and the helpers of `content.js` in a Node `vm` sandbox that stands in
for the WebExtension APIs: settings storage, the server, AnkiConnect and Downloads proxying,
sentence mining, the live clock and the master switch. `content.test.js` also covers the model
name sent with every sync, the restart on a new session token, the model status and error
messages and the font stack; `popup-copies.test.js` keeps the popup's copies of the font and
model-name rules equal to those in `content.js` and `server.py`. `background.test.js` covers
the `startServer` message: the native host's answers, the timeout, the mapping of the browser's
errors to hints, and the launch record that outlives the event page; `popup.test.js` runs the
popup's start flow against a fake document, from the button to the 90 s deadline and its hints.
The update check is covered on both sides too: `background.test.js` feeds a fake GitHub answer
to `checkForUpdate`, the version helpers and `decideUpdate`, the badge and the one notification
per session, the `/update` request with its record, and the `/health` polls that end it;
`popup.test.js` walks the banner through every verdict, Update to "Updated to 0.9.0", the old
version coming back, the 120 s deadline, Not now and Check for updates. The word colours are
covered in `words.test.js` (reading a note's fields and pitch accent, the card states, the
conjugation matcher with its boundaries and the words it must not colour), `background.test.js`
(the deck's five searches, the index and its time to live, the deck of the last mined card, Anki
away or refusing, a hostile note), `content.test.js` (the spans a line is drawn with, the poll, a
deck change, the refresh that redraws only the changed lines) and `popup.test.js` (the deck list,
its hints and when Anki is asked).

## Acknowledgements

Built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and
[CTranslate2](https://github.com/OpenNMT/CTranslate2), [MLX](https://github.com/ml-explore/mlx)
with [mlx-whisper](https://pypi.org/project/mlx-whisper/) on Apple Silicon,
[yt-dlp](https://github.com/yt-dlp/yt-dlp),
[PyAV](https://github.com/PyAV-Org/PyAV), OpenAI's [Whisper](https://github.com/openai/whisper)
and [Kotoba-Whisper](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0). The mining flow
follows the conventions of [Yomitan](https://yomitan.wiki/), [AnkiConnect](https://foosoft.net/projects/anki-connect/)
and [asbplayer](https://github.com/killergerbah/asbplayer).

## License

MIT, see [LICENSE](LICENSE).
