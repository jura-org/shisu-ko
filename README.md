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
  downloads it. The popup switches to any other model without restarting the server (on Windows
  the experimental [AMD engine](#amd-graphics-cards-experimental) restarts it): a faster-whisper
  size or a Hugging Face repo id of a CTranslate2 model, such as
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
- An NVIDIA GPU with about 4 GB of free VRAM for large-v3. With less free memory the server
  switches to int8 weights by itself; without a GPU pick the small model at setup and run on
  the CPU.
- Or, experimental and for the native server on Windows and Linux, an AMD graphics card:
  Radeon RX 7000 or RX 9000, Radeon PRO W7000/W9000 or AI PRO R9700, or the graphics of a Ryzen
  AI 300 (890M, 880M) or Ryzen AI Max (8060S, 8050S, 8040S); on Windows with Adrenalin 26.2.2 or
  newer, on Linux also an RX 6800/6900, with ROCm 7.2 installed. Setup offers it when it finds
  one; see [AMD graphics cards](#amd-graphics-cards-experimental).
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
yt-dlp and the CUDA runtime libraries; nothing else on the system is touched. It then asks which
Whisper model the server should use, `1` for large-v3 (best quality, about 3 GB, wants a GPU with
4 GB or more free) or `2` for small (about 500 MB, fine on a CPU, less accurate), downloads it
into `~/.shisu-ko/models` with a progress bar and remembers the choice in
`~/.shisu-ko/config.json`. Where Firefox is installed it also asks whether the server should send
Firefox's YouTube cookies with every download: YouTube refuses some downloads ("Sign in to confirm
you're not a bot") until they carry a signed-in browser's cookies. Say yes if you are signed in to
YouTube in Firefox; the answer goes to the same file (see [YouTube sign-in](#youtube-sign-in)).
Last, where it finds an AMD graphics card, it offers the experimental AMD engine and asks before
it downloads anything (see [AMD graphics cards](#amd-graphics-cards-experimental)); that step
never makes the setup fail. When it says that everything is ready, close its window and start
`run.cmd` / `run.sh`. The choice is kept even when the download fails or is stopped with Ctrl+C:
the first start then downloads the chosen model itself, without the progress bar. The server is
ready when it prints `Listening on http://127.0.0.1:8790`. Keep the window open while you watch; it
restarts itself if it ever crashes.

From then on the toolbar popup can start it for you: while the server is offline, the status
line in the popup's header shows a **Start server** button. The first click asks the browser for
permission, Firefox to "exchange messages with programs other than Firefox", Chrome to
"communicate with cooperating native applications"; allow it, and the button launches
`server\run.cmd` in a window of its own (Windows) or `server/run.sh` in the background with its
output in `~/.shisu-ko/server.log` (Linux/macOS), then waits for the server to answer.
The button passes no options: the server starts with its defaults (the model chosen at setup,
else large-v3; the GPU when there is one; the YouTube cookies of the browser chosen at setup, if
any, see [YouTube sign-in](#youtube-sign-in)), exactly as a bare `run.cmd` / `run.sh` start
would. The popup's model field switches the model once that default one is up; anything else you
usually append to `run.cmd` / `run.sh` (`--device cpu`, `--js-runtime`, ...) needs a start by
hand, see [Server options](#server-options). In Chrome the
button is there for the [Chrome Web Store](https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm)
install only (see [Install the extension](#2-install-the-extension)): the launcher's manifest
has to name the extension's id, and an unpacked build (`dist/chrome`, the release zip) gets an
id of its own from the folder it was loaded from, so there the server is started by hand.
Docker and Nix users start the server as before.

Setup registers that launcher with Firefox and Chrome (on Linux with Chromium too), and so does
every `run.cmd` / `run.sh` start. An existing install therefore gets the button after one or two
starts by hand: the start that updates Shisu-ko to a version with the button still runs the old
launcher, so it is the start after the update that registers. An install that already has the
button gets Chrome's registration with the update itself on Linux and macOS, where `run.sh`
registers after updating (the popup's **Update** included), and with the next `run.cmd` start on
Windows, where `run.cmd` registers before it updates. Running `setup.cmd` / `setup.sh` once is
the sure way, and `run.cmd --check` says for which browsers the launcher is registered. To take
the registration away again, for example before deleting the checkout or if no browser should
be able to start anything, run
`~/.shisu-ko/venv/Scripts/python server/native_host.py --unregister` (`venv/bin/python` on
Linux/macOS; any Python 3 works, the host is standard library only). On Windows it removes
`shisuko.json` and `shisuko-chrome.json` from `~/.shisu-ko/native-messaging` and the registry
keys `HKCU\Software\Mozilla\NativeMessagingHosts\shisuko` and
`HKCU\Software\Google\Chrome\NativeMessagingHosts\shisuko`; on Linux `shisuko.json` from
`~/.mozilla/native-messaging-hosts`, `~/.config/google-chrome/NativeMessagingHosts` and
`~/.config/chromium/NativeMessagingHosts` (`$CHROME_CONFIG_HOME`, else `$XDG_CONFIG_HOME`, in
place of `~/.config` when it is set, as Chrome itself looks); on macOS `shisuko.json` from
`~/Library/Application Support/Mozilla/NativeMessagingHosts` and
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts`. Delete those by hand if the
checkout is already gone. `--status` shows the current state.

Every start first looks for a newer Shisu-ko: a git clone is fast-forwarded to the branch it
tracks, a folder downloaded as a zip is replaced with the newest release, changed Python
requirements are installed, and a changed extension is pointed out (reload it in Firefox or
install the new `.xpi`; a Chrome Web Store install is updated by the store). Local changes are
never overwritten, and being offline just starts the current version. `run.cmd --no-update` (or
`SHISUKO_NO_UPDATE=1`) skips the check.

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
updates as before. The extension itself is never installed by the add-on: Firefox updates it from
the listing on addons.mozilla.org, and the banner links to the release page when only the
extension is behind, once the release carries the signed `.xpi` (until then it says the signed
`.xpi` is on its way). Chrome updates an install from the Chrome Web Store by itself: every
release goes to the store by itself, and Chrome gets it once it has passed the store's review,
which can be days after the GitHub release; until then the banner says so and offers no release
page, since an unpacked build from there would be a second copy of the extension beside the
store's (an unpacked build of your own keeps the link). Being offline costs one failed check,
shown under **Check for updates**; a failed check never notifies.

**Nix / NixOS:** `nix run github:Multysquid/shisu-ko` (or `nix run .` in a checkout) starts the
server with CUDA support; `nix run .#check` prints diagnostics; `nix develop` opens a shell with
Python, web-ext, Node and Deno for development. The flake takes CTranslate2 with CUDA from the
`cache.nixos-cuda.org` binary cache, so add it to your substituters or expect a long build. To
keep the server running in the background: `systemd-run --user --unit=shisu-ko nix run /path/to/shisu-ko`.
Options go after `--` (`nix run . -- --device cpu`). There is no setup to ask about YouTube's
sign-in, so if YouTube asks for one, see [YouTube sign-in](#youtube-sign-in).

**Docker:** copy `.env.example` to `.env`, set `DATA_DIR` to where models and caches should
live, then run `docker\up.cmd` (Windows) or `docker compose up -d`. See [Docker](#docker) below.

### 2. Install the extension

Temporary install (until Firefox restarts):

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…** and choose `addon/manifest.json`.
3. Firefox asks for access to youtube.com the first time you open the popup; click **Allow on
   YouTube** (or right-click the toolbar icon > Always Allow on www.youtube.com).

Permanent install: every [GitHub release](https://github.com/Multysquid/shisu-ko/releases/latest)
carries `shisu_ko-<version>.xpi`, signed by addons.mozilla.org for self-distribution, usually
within minutes of the release; open it in Firefox to install. When addons.mozilla.org holds a
version for a human review, the release carries `shisu-ko-<version>-firefox-unsigned.xpi` until
the signed file replaces it: regular Firefox refuses an unsigned add-on, Developer Edition,
Nightly and ESR take it with `xpinstall.signatures.required` set to `false`. Or install
[Shisu-ko on addons.mozilla.org](https://addons.mozilla.org/firefox/addon/shisu-ko/), which gets
selected releases after AMO's review (that can take days), so it may be a release or two behind
GitHub. Both are the same add-on, and Firefox updates either from the listing: a GitHub install
moves on to the first listed version newer than its own (a release published there is listed as
its number plus `.1`, the same code). Regular Firefox only keeps signed
add-ons; Firefox Developer Edition, Nightly and ESR can instead load the unsigned zip with
`xpinstall.signatures.required` set to `false` in `about:config`. The popup says when a newer
release is out (see [Updates](#1-start-the-server)). Since 0.9.0 the extension needs one more permission,
"Display notifications to you": opening the new `.xpi` over an older version lists it in the
install prompt, and an automatic update (from the listing, for a release published there) is held back by
Firefox until you approve it, from the notice on the application menu (≡) or under Add-ons
and themes.

On Chrome, install [Shisu-ko from the Chrome Web Store](https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm).
Every release goes to the store by itself, and Chrome updates the install by itself once the store
has reviewed it, so it can trail the GitHub release by days; the popup then says the store will
update it instead of pointing you at the release page.

Chrome development uses the same source. Run `npm ci` and `npm run build:chrome`, then open
`chrome://extensions`, enable Developer mode, and choose **Load unpacked** on `dist/chrome`.
After edits, run `npm run watch`; reload the extension on that page and reload the YouTube tab.
The Firefox source remains directly loadable from `addon/manifest.json`. `npm run build` writes
both unpacked trees and `dist/shisu-ko-<version>-{firefox,chrome}.zip`. Instead of the store
install, you can also download `shisu-ko-<version>-chrome.zip` from the [Chrome release](https://github.com/Multysquid/shisu-ko/releases/latest),
unzip it, and choose **Load unpacked** on the extracted folder. This ZIP is unsigned and is not a
Chrome Web Store install; it has no automatic updates, and it gets an id of its own, so beside a
store install it is a second copy of the extension (keep one of the two). Its popup keeps the
release page link for a newer version. Keep the extracted folder and reload the extension from
`chrome://extensions` after updates. Chrome shortcuts are under
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
| Alt+Shift+K | Mark the word under the pointer (or the word selected in a line) as known, or take it off the list again; see [Word colours](#word-colours) |
| Alt+Shift+H | Hide the status badge in the player's top left, the red "server offline" included, or show it again (the popup's **Status badge on the video, errors too** switch) |
| ← / → | Jump to the start of the previous / next subtitle. In a gap between lines, Left goes back to the line that just ended. Where nothing is transcribed yet, and before the first subtitle arrives, the keys keep YouTube's five second seek. Can be turned off in the popup |

Shortcuts can be changed in Firefox under Add-ons and themes > Manage Extension Shortcuts, or in
Chrome at `chrome://extensions/shortcuts`. Chrome gives an extension four default shortcuts, so
Alt+Shift+H has none there until you set one on that page.

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
names default to `Picture` and `SentenceAudio`, as used by common Japanese mining note types, and
match whatever case your note type writes them in (`picture` and `sentenceAudio` in Eminent);
change them in the popup when yours are named differently. An optional sentence field is filled with the subtitle
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
text colour, unless it is a word on your list of known words, or a particle, katakana word or name
you chose to colour (below). The cards come from one deck. Left on *Automatic*, that is the deck your
last mined card went to: nothing is looked up until you have mined a card, and the first mine then
names the deck (the hint under the **Deck** select says which, or "no card mined yet"). Choose a
deck in the select to look at that one instead; its subdecks count. Words are taken from the note's
word field (the popup's **Word field**, else the note's first field), and a verb or adjective is
found in its usual conjugations and in its noun form: a card for 食べる colours 食べました,
食べたことがある and 食べ in 食べに行く, 書く colours 書かない and 書いて, 美しい colours
美しかった, 勉強する colours 勉強している and the bare 勉強, 終わる colours 終わり, and a word
written in kana is found in its forms too (かける colours かけて, しまう colours しまった, おいしい
colours おいしかった). A card for a noun written with kanji or katakana also colours the する forms
after it (お願い colours お願いします, スタート colours スタートしました); one written in kana, such
as びっくり, does not, nor does a time word, an adverb, a counter or a single kanji (今日します,
全然しない, 何かしたい, 一回した, 顔する keep the する apart), and a card for the verb itself keeps
its own forms (with 話 and 話す in the deck, 話して is 話す's). A card whose dictionary entry marks
the word as usually written in kana (Jitendex's "kana" tag, JMdict's `uk`) colours its reading as
well: a card for 更に colours さらに.
A particle does not take the colour of the word before it (領域まで is 領域 in the card's colour and
まで apart: まで has no card), save one: with particles not counted as known (below), the quotative
って or と between a word coloured by its card and いう found after it (話しかけていただくっていう),
which ICU holds in one piece, takes the colour of the word before it. The honorific お or ご before
a word takes its colour, being part of the word (お風呂). A word is not coloured inside a compound
(食べ物 for 食べる, 日本語 for 日本, 走者 for 走る), and a card for a particle, the copula or an
auxiliary (は, のは, から, でも, だ, です, ます, ない, たい, ん …) never colours anything by itself,
since it would paint every line by that card's state. Two cards for one word show the one with the
least progress; a suspended card only counts when there is no other.

The colours below need no card, so they sit apart, under **More word colour options** at the end of
the Word colours section (closed until you open it), and the three switches there are off by
default: a colour should say what your own deck says. They come with the card colours: they show
only while **Colour words by their Anki card** is on, and **Overbar by pitch accent** on its own
colours none of them. They need no deck either: with no card mined and no deck chosen, or with Anki
closed, your known words and the particles, katakana words and names you switched on are coloured
all the same, and the card colours join them once a deck has been read.

**Particles count as known** (off by default) colours green every particle the browser's word
splitter sets apart as a word of its own, whatever stands before it, and the combinations of
particles, the copula and the auxiliaries along with them (は, には, から, まで, です, ですね, という
…; と alone when いう has a card of its own), as grammar you know rather than words with a card,
and with them the words a learner knows as grammar: the verbs that carry it (ある, いる, おる, みる,
する, くる, なる, いく, しまう, もらう … in their common forms), そう, よう, みたい, らしい, この, それ,
ここ, どう and the other こそあど words, こと, もの, わけ, はず, and まだ, もう, また, よく, ちょっと and
the like; a card for one of them still decides its colour. Turned off, they keep the text colour. A particle the splitter joins to a verb's ending keeps
the text colour too (the よ of できますよ). Most verbs the deck lacks are not taken apart for it:
the splitter cuts a kana one it does not know into pieces that look like particles (やって, なった,
よかった, してます, もらって), and the kana ending of a kanji one into more of them (飲んだ,
書かない, 呼ばれた), while the endings after its stem are its own (ございます,
見えてきました), so those keep the text colour whole. Measured on real subtitles, about one green
particle in thirty is still such a piece, among them a negative cut into particle shapes of two
kana (わからない and 分からない show わ, から and ない green); the other way round, the Kansai
copula や before った or って (日本初やった) looks like やる and stays uncoloured.

**Names and Latin text in blue** (off by default) colours blue, no card needed: Latin letters (jr, YouTube, iPhone, Ｗｉ－Ｆｉ, and
Tシャツ as one word; www laughter is no name), place names (the prefectures, their capitals and
big cities, Tokyo's wards, the districts and sights a travel video names, the countries and cities
abroad: 東京, 丸の内, 北海道, アメリカ), and a place, or any word of two kanji or katakana or more
that has no card, with a suffix such as 駅, 区, 寺 or 通り after it (東京駅, 渋谷区, 品川駅,
金閣寺), which turns an ordinary word without a card before such a suffix blue as well (予定通り,
時間通り); a word with a card keeps its colour and the suffix its own (地元駅 with cards for 地元
and 駅). A card for the same word wins (東京 alone, with 東京 in the deck, is the card's colour),
a longer name wins over a shorter card (東京駅 over 東京, 丸の内 over 丸). Switched off, a name
keeps the text colour and is still read whole, so no card is found inside it (a card for 駅 does not
colour the 駅 of 東京駅); a katakana name such as アメリカ is still green with **Katakana words count
as known** on.

A word on your list of known words is green whatever its card says (its pitch overbar stays), and a
known word with no card is found in its conjugations like a deck word. The list is the **Known
words** field under More word colour options, one word per line. **Alt+Shift+K** adds the word under
the pointer in a subtitle or the transcript, or the word you selected there (a word Yomitan has
selected counts too, so the shortcut works with its popup open), and takes it off the list again
when it is on it already. The word is the one under the pointer's tip, down to the character: a
coloured word whole, else the word the browser's word splitter makes of the text there, a single
kanji with the kana that follow it up to the next particle (食べて, but 私 in 私はこれが), お or ご
with the word it fronts (風呂 for お風呂), and a verb or adjective in the form your deck holds it in
(食べる for 食べた) when the deck has it. A particle is not added, since the **Particles count as
known** switch decides its colour, but one you typed into the list comes off it. With the pointer
moved off the player the shortcut marks nothing and says "No word under the pointer", unless the
video is paused by a hover and waiting for you. With **Katakana words count as known**, every
katakana word of two characters or more that no card, known word or name covers is green too.

**Overbar by pitch accent** draws a bar over each word that has a card, in the colour of its pitch
accent pattern: blue heiban, red atamadaka, orange nakadaka, green odaka (the colours Migaku and
Yomitan use). The pattern is read from the card's pitch accent field in whichever form Yomitan wrote
it: a category name (`{pitch-accent-categories}`), a position such as `[2]`
(`{pitch-accent-positions}`), the drawn marks (`{pitch-accents}`) or the drawn graph
(`{pitch-accent-graphs}`). A position needs the word's mora count to tell odaka from nakadaka; it
comes from the drawing, else from the card's reading field (a field named reading or furigana, not
the sentence's), else from the word itself when it is kana, and without any of them the word counts
as nakadaka. A field holding the bare reading and nothing else says nothing about the pitch (old
templates wrote it there whatever the pattern). The field is found by itself: the first one whose
name contains "pitch" or "accent" and holds a readable value, unless you name one under **Pitch
accent field** in the *Anki, clips and server* drawer; when none reads, a reading field Yomitan drew
the pitch into is read instead. Verbs and adjectives, which Yomitan files under kifuku, get no bar
rather than a wrong one. Both colourings can be on at once: the text colour is the card's state, the
bar its pitch.

The deck is looked at again every 30 seconds while a video is open, and only the lines whose
colours changed are redrawn, so a card you review in Anki changes colour within a minute and a
card you have just mined shows red within seconds. The words stay ordinary page text, so Yomitan
scans across the colours as before. When Anki is closed the colours stay as they were last read,
or, when nothing was read yet, only the colours that need no card show; a deck that no longer
exists colours no card; either way the hint under the deck select says what stands in the way,
and nothing is ever toasted on the video.

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
on that line launches it with the server's default options (Firefox and the Chrome Web Store
install, see [Start the server](#1-start-the-server)); the model field below takes effect once
it is up. The same page opens as the add-on's preferences under Add-ons and themes > Shisu-ko.

A banner under the header appears when a newer release than the running server (or than this
extension) is out: "Shisu-ko 0.9.0 is available — the server runs 0.8.0." with **Update**, which
restarts the server on the new version through its launcher, and **Not now**, which hides the
banner for this browser session; the toolbar badge stays until the server is current. A server
that cannot update itself (Docker, Nix, a start by hand, `--no-update`) gets a note instead of
the button: the banner says the server was not started by `run.cmd` / `run.sh` in every one of
those cases, since the server only reports whether it can update, not why not (a `--no-update`
server's own reason sits in its 409 answer, which the popup never asks for without the
button). While the server is offline the banner says nothing about the server, since its next
start updates it anyway. When the extension is behind and the server is current or offline, the
banner links to the release page (in Firefox once the release carries the signed `.xpi`, until
then it says the signed `.xpi` is on its way; for an unpacked Chrome build at once), or, for a
Chrome Web Store install, says that the store updates it once it has reviewed that version. The
**Check for updates** link in the *Anki, clips and server* drawer asks GitHub now, whatever the
age of the daily check, and the line under it keeps the result ("Newest release: 0.9.0, checked
3 min ago", or why the check failed).

| Setting | Meaning |
|---|---|
| Pause video while hovering a line | Needed for comfortable Yomitan lookups |
| Left/Right jump between subtitles | Arrow keys move between cues instead of seeking five seconds |
| Transcript panel | List of all cues so far, with jump and mine buttons |
| Auto-attach to new Yomitan cards | Watches AnkiConnect and fills the new card by itself; off means Alt+Shift+M or the pickaxe |
| Colour words by their Anki card | Colours each word of a line by the state of its card in the deck below: green learned, yellow learning, orange suspended, red new; green for your known words and, as the three switches under More word colour options say, green for particles and katakana words and blue for names and Latin text; other words keep the text colour. Needs Anki with AnkiConnect, see [Word colours](#word-colours) |
| Deck | The deck whose cards are looked at. Automatic means the deck your last mined card went to; nothing is looked up before a card was mined or a deck chosen. The hint under it names the deck, or says what stands in the way |
| Particles, katakana, names, known words | Under More word colour options, closed by default |
| Particles count as known | Off by default. On: particles and their combinations with the copula and the auxiliaries (は, には, です, という …) and the grammar words (ある, いる, する, この, こと, まだ …) are green, as grammar you know; off, they keep the text colour |
| Katakana words count as known | Off by default. Katakana words of two characters or more that no card, known word or name covers are green |
| Names and Latin text in blue | Off by default. Place names, a word with a place suffix (駅, 区 …) and Latin text (OK, iPhone) are blue; off, they keep the text colour |
| Known words | Your own list, one word per line: green whatever the card says, and found in their conjugations without a card. Alt+Shift+K adds the word under the pointer, or takes it off again |
| Overbar by pitch accent | Draws a bar over each word that has a card, in the colour of its pitch accent pattern: blue heiban, red atamadaka, orange nakadaka, green odaka, read from the card's pitch accent field |
| Font size, keep line after speech | Presentation; the linger time keeps short lines readable |
| Hide YouTube's own captions | Avoids two subtitle layers |
| Status badge on the video | The badge in the player's top left, errors included; off (or Alt+Shift+H) it shows nothing at all |
| Show progress messages on the video | With the badge on: the progress messages; errors are always shown |

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
shows. Models already downloaded are offered as suggestions. The change applies while
a video plays: a model that is not on disk yet is downloaded from Hugging Face first, while the
current model keeps subtitling, and once the swap is done the video's transcript starts over with
the new model. Meanwhile the badge on the video says "Loading model X…"; a name the server cannot
use shows "Shisu-ko: model X: …" with the reason, even with progress messages off, and the previous
model keeps running. The popup mirrors this under the field: the status badge says "Loading model"
during a switch, and the hint under the field carries the server's verdict on the name you typed.
With the experimental AMD engine on Windows the swap is a restart of the server (see
[AMD graphics cards](#amd-graphics-cards-experimental)).

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
and the server does the heavy lifting with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2).

**Scheduling.** When you open a video the server fetches the audio track with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), decodes a minute around the playhead while the
download is still running, and transcribes a short 20-second window there so the first subtitles
appear quickly. It then continues in 30-second windows up to 15 minutes ahead of you. A sentence
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
`GET https://api.github.com/repos/Multysquid/shisu-ko/releases/latest`: when the browser starts
(or the extension is installed or updated) or the popup opens and the last check is over a day old
or failed, so at most once a day by itself while the checks succeed (a failure is tried again
at the next of those occasions), and on every click of **Check for updates**. It carries no account, token,
cookie or identifier, only what any visit to GitHub carries (your IP address and the browser's
user agent); GitHub's answer (the release's version, page and `.xpi` address, and when it was
checked) is kept in the extension's storage. The **Update** button then sends `POST /update` to
the local server, which exits with code 4 so that `run.cmd` / `run.sh` run `update.py` and start
it again; the server never downloads anything itself, and the extension never installs itself.
The browser updates it with requests of its own: Firefox from addons.mozilla.org, and Chrome, for
a Chrome Web Store install, from the store.

## Server options

Append options to `run.cmd` / `run.sh` (with Nix, after `--`: `nix run . -- --device cpu`), or
put them in the `command:` line of `compose.yaml`. The popup's **Start server** button runs
`run.cmd` / `run.sh` without any of them, so it always starts the defaults below (the model can
still be switched from the popup afterwards); a server that needs `--device cpu` or another
option is started by hand. Without `--model` the server runs the model chosen at setup
(`~/.shisu-ko/config.json`), else large-v3; without `--cookies-from-browser` or `--cookies` it
sends the YouTube cookies of the browser chosen there (see [YouTube sign-in](#youtube-sign-in)).

| Option | Effect |
|---|---|
| `--model kotoba-tech/kotoba-whisper-v2.0-faster` | Default model (here the Japanese-specialised distilled one, about 6x faster and lighter on memory than large-v3). The popup overrides the default with any faster-whisper size or Hugging Face repo id, without a restart (with the AMD engine on Windows, through one) |
| `--model large-v3-turbo` | OpenAI's faster large model as the default |
| `--model small --device cpu` | CPU-only operation |
| `--download-model small` | Download the model now, with a progress bar, and make it the default of later starts (what setup runs after its environment check); exits instead of starting the server, with code 2 on a failure or Ctrl+C, which `run.cmd` / `run.sh` do not restart on |
| `--compute-type int8_float16` | Halves GPU memory use; chosen by itself when less than 4.5 GB is free |
| `--cookies-from-browser firefox` | Send that browser's YouTube cookies for this start: when YouTube asks for a sign-in, and for age-restricted or members-only videos. `none` sends no browser's, whatever setup chose |
| `--save-cookies-from-browser firefox` | Make that browser's YouTube cookies the default of every later start, the popup's **Start server** button included, after checking that they can be read; `none` forgets it. Exits instead of starting the server (see [YouTube sign-in](#youtube-sign-in)) |
| `--cookies /path/cookies.txt` | Same as `--cookies-from-browser`, with an exported cookies file (use this inside Docker) |
| `--lookahead 0` | Transcribe to the end of the video instead of stopping 15 minutes ahead |
| `--window 60` | Longer windows are slightly more efficient, shorter ones react faster to seeking (default 30, faster-whisper's own chunk: a longer window decodes its tail without the initial prompt) |
| `--max-cue-chars 26` | Characters per cue before it is split (default 30; 26 is the Netflix Japanese limit) |
| `--max-cue-seconds 7` / `--min-cue-seconds 0.8` | Longest and shortest cue (defaults 7, Netflix's own maximum, and 0.8); shorter ones are extended or merged |
| `--initial-prompt ""` | Turn off the prompt that asks Whisper for punctuation. Japanese gets one by default (`はい、そうですね。今日はよろしくお願いします。それで、どう思いますか？`), because a window is decoded with nothing in front of it and an unprompted decode writes a sentence mark at about half of the sentence ends; any other text replaces it, and a language other than Japanese has none. The prompt now and then makes Whisper skip the first seconds of speech in a window; such a window is decoded once more without it and the skipped lines filled in |
| `--language-patience 60` | Seconds of speech in another language before a video's subtitles stop (0 = never listen for it, transcribe everything) |
| `--lyrics off` | Transcribe a window in which the speech detector hears under a second of speech with the detector as before (blank when it heard nothing). The default `auto` transcribes such a window without the detector when its audio is not silent (sung lyrics, speech over music) and Whisper hears the target language in it, under stricter gates |
| `--sentence-ends off` | Cut and merge lines on Whisper's own punctuation alone. The default `auto` writes the sentence mark Whisper left out where a word ending in a sentence-final expression (よね, です, ます, か, or a plain form) is followed by a pause, so a run-on line breaks where the speaker ended the sentence and a mined card gets that sentence and no more |
| `--idle-minutes 30` | Release the decoded audio of a video nobody has synced for this long |
| `--retry-after 30` | Seconds before a failed audio fetch is retried, and the wait before a model name that failed to download or load is tried again |
| `--js-runtime deno` | JavaScript runtime for yt-dlp: auto, node, deno, bun, or name:path |
| `--allow-remote-ejs` | Lets yt-dlp fetch updated YouTube challenge-solver scripts from GitHub |
| `--check` | Print environment diagnostics (CUDA, the AMD engine where it is installed, yt-dlp's JavaScript runtime, downloaded models, whether the popup's Start button has its launcher registered) and exit |
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

### YouTube sign-in

YouTube answers some addresses with "Sign in to confirm you're not a bot" and refuses to let
yt-dlp download anything until the request carries a signed-in browser's cookies. The server can
send them: setup asks once, where Firefox is installed, and
`server\run.cmd --save-cookies-from-browser firefox` (`server/run.sh ...` on Linux/macOS;
`nix run . -- --save-cookies-from-browser firefox` with Nix, which has no setup) sets it at any
time. That reads Firefox's cookie store once to check that it holds YouTube cookies (only their
names are looked at), writes `"cookies_from_browser": "firefox"` to `~/.shisu-ko/config.json` and
exits; from the next start on, every download sends Firefox's YouTube cookies, whether the server
was started by hand or with the popup's **Start server** button. `--save-cookies-from-browser
none` forgets it, and `--cookies-from-browser none` leaves it out for one start.
`run.cmd --check` (`nix run .#check`) says which browser is used.

Firefox is the browser to use on Windows: Chrome and Edge keep their cookies from other programs
there, so yt-dlp finds none and the command refuses them. The cookies are those of whatever
account is signed in to YouTube in that browser. yt-dlp does not keep Firefox's contexts apart,
though: the YouTube cookies of every Multi-Account Container, and those that YouTube players
embedded in other sites keep, go into one set, so an account signed in in a container can take
the place of the default one or mix with it. yt-dlp reads the browser's whole cookie store, every
site's cookies, for each download, through a copy of the store's file that it writes into the
system's temp folder and deletes after reading, and sends a site only its own: YouTube's to
YouTube, and GitHub's to GitHub when `--allow-remote-ejs` fetches the solver script there.
yt-dlp's authors warn that an account used this way may be rate-limited or flagged, so a
secondary account is the careful choice. The Docker image never takes the browser from
`config.json` (it has none to read): Docker users export a `cookies.txt` into the data folder and
add `--cookies /data/cookies.txt` to the `command:` line. Toolbox and distrobox share your home
folder and its Firefox, so there the saved browser is used as on the host.

## AMD graphics cards (experimental)

The native server can run Whisper on an AMD graphics card, through CTranslate2's build for AMD's
ROCm. This is experimental: it is not yet tested on AMD hardware by the maintainer. Reports from
AMD users, whether it works or not, are welcome as a
[GitHub issue](https://github.com/Multysquid/shisu-ko/issues), with the card, the system and
what `amd_setup.py --status` and `run.cmd --check` print.

**Which cards.** On Windows: Radeon RX 7000 and RX 9000, Radeon PRO W7000 and W9000, Radeon AI
PRO R9700, and the graphics of Ryzen AI 300 (Radeon 890M, 880M) and Ryzen AI Max (Radeon 8060S,
8050S, 8040S), with AMD Software: Adrenalin Edition 26.2.2 or newer. AMD's Windows runtime has
no kernels for the RX 6000 series or anything older, nor for other processor graphics such as
the Radeon 780M or 860M, so on Windows those get no AMD engine. A card that Windows lists as
"Microsoft Basic Display Adapter" has no AMD driver yet; install Adrenalin first. On Linux: the
same cards and the RX 6800 and 6900 series (gfx1030), on a ROCm 7.2.x that you install yourself
following [AMD's guide for 7.2](https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.4/),
with your user in the `render` and `video` groups (`sudo usermod -aG render,video "$USER"`, then
log out and in again); until then setup says what is missing and asks nothing. Follow the guide
for 7.2, not AMD's latest one: that installs ROCm 10, and the engine needs ROCm 7.2's libraries.
On Linux setup goes by the GPU target that the amdgpu driver reports (`gfx1100` and so on), not
by the card's name. An RX 6600 or 6700 (gfx1032, gfx1031) is not one of the targets the build is
compiled for, but is reported to run when told to pass for a gfx1030; that is at your own risk:
put `export HSA_OVERRIDE_GFX_VERSION=10.3.0` in your login shell's profile (`~/.bash_profile`,
`~/.zprofile` or `~/.profile`), log out and in again, and run `amd_setup.py --yes`. There is no
AMD engine in the Docker image, with Nix or on macOS; the first two keep their own engine even
when they share `~/.shisu-ko` with a native setup that has one.

**What setup does.** After the model download, `setup.cmd` / `setup.sh` run
`server/amd_setup.py`, which looks for an AMD graphics card. Where it finds one the engine runs
on, it names the card, says that the engine is experimental, how much it downloads and, on
Windows, how much room it takes, and asks `Download the AMD engine? [y/N]`; anything but `y` or
`yes` is a no, and so is an unattended setup. A card it does not know gets the question with a
warning; a card the engine cannot run gets one line saying why, and no question. Next to an
NVIDIA GPU the server stays on the NVIDIA GPU and the engine is not offered; `amd_setup.py --yes`
installs it anyway, and the server then uses the AMD card instead. Whatever happens in this step,
the setup finishes.

On Windows a yes downloads about 1.27 GB: CTranslate2 4.8.2's ROCm build from its GitHub release
and AMD's ROCm 7.2.1 runtime from `repo.radeon.com`. Installed, the engine takes about 3.90 GB,
and about 5.31 GB must be free on the disk of `~/.shisu-ko` while it installs (where the system's
temporary folder is on another disk, pip needs about 3.90 GB there as well); setup checks the
free space before the first download and again before it installs. On Linux the download is
about 284 MB, since the runtime is the system's own ROCm. Each file is checked against the size
and SHA-256 pinned in `amd_setup.py` and installed with pip into a folder of its own,
`~/.shisu-ko/rocm`; the downloads wait in `~/.shisu-ko/cache/rocm-download` meanwhile and are
deleted afterwards. The venv's own CTranslate2 is never touched, so the NVIDIA and CPU engines
stay exactly as they are.

Then setup tests the engine: `server.py --probe-gpu` loads the model chosen at setup on the AMD
GPU, in a process of its own, and transcribes two seconds of silence with it. That can take some
minutes; the test gives up after 15. Only a test that passes switches the server over
(`"engine": "rocm"` in `~/.shisu-ko/config.json`), and it ends with "The server will use the AMD
GPU from its next start." A card or driver that is not up to it can kill the test's process,
never the setup's. From then on every start logs
`GPU engine: AMD ROCm (CTranslate2 4.8.2 from ...)` and loads the model "on the AMD GPU (ROCm)".

**Checking, testing again, removing.** `run.cmd --check` (`run.sh --check`) prints
`GPU engine: AMD ROCm (CTranslate2 4.8.2 from ...)` when the server uses the engine, and
`GPU engine: AMD ROCm installed but not used:` with the reason when it does not; without the
engine there is no such line. To CTranslate2 the AMD card is a CUDA device, so the line above it
counts it as one, and `/health` reports the device as `cuda`. `amd_setup.py` runs with the
venv's Python, `~/.shisu-ko/venv/Scripts/python server/amd_setup.py --status` on Windows
(`venv/bin/python` on Linux), and its messages give the whole command for the next step:

```
amd_setup.py            look for an AMD card, ask, download, install and test
amd_setup.py --yes      the same without the question, also next to an NVIDIA GPU or for a card
                        it does not support; exits with 1 unless the engine ends up working
amd_setup.py --probe    test the installed engine again, without a download; exits with 1 unless
                        the test passes
amd_setup.py --status   what is found, installed and switched on; downloads and changes nothing
amd_setup.py --remove   switch the engine off, delete ~/.shisu-ko/rocm and any leftover downloads
```

Without `--yes` or `--probe` it always exits with 0, so that the setup scripts never fail on it.

**When it fails.** A test that fails or crashes says why (the reason `server.py` gave, or "the
test crashed" with the exit code) and leaves the engine switched off: the server stays on the
NVIDIA GPU or the CPU, as before. The installed engine stays too, so that `amd_setup.py --probe`
can test it again without a download, after a driver update for instance. A download that breaks
off is tried once more, a file that does not match its pin is deleted and stops the install, and
finished downloads are kept for the next try. A setup that no longer finds an AMD GPU switches
an installed engine off.

At a server start the engine can fail where Python cannot catch it: a card or driver that is not
up to it kills the process, and `run.cmd` / `run.sh` would start it into the same crash again.
The server therefore counts its starts on the AMD engine until one has loaded its model on the
GPU and warmed it up; after two that did not, it leaves the engine off and starts on the default
engine. A start whose engine does not load, or whose ROCm runtime sees no AMD GPU (a card removed
or replaced, a driver it cannot use), leaves it off at once: the log says why, and the server
exits with code 3, which the launcher answers with a start on the default engine (a server
started by hand just stops, and its next start is on the default engine). On Linux the engine
also stays off while ROCm 7.2's libraries are missing from `$ROCM_PATH/lib` (`/opt/rocm/lib` by
default). In each case `run.cmd --check` gives the reason, and `amd_setup.py --probe` tests the
engine again and switches it back on when it passes.

To go back to the default engine for good, run `amd_setup.py --remove`: it switches the engine
off in `config.json` and deletes `~/.shisu-ko/rocm` and any downloads left over, and the next
start runs on the NVIDIA GPU or the CPU as before. `SHISUKO_ENGINE=default` in the server's
environment leaves the engine off for the starts that have it, without removing anything.

**Known limitations.**

- It is not yet tested on AMD hardware by the maintainer. The card lists follow the GPU targets
  CTranslate2's ROCm build is compiled for and the kernels in AMD's Windows runtime.
- The engine uses the first AMD GPU the ROCm runtime lists (HIP device 0). On a machine with
  both an AMD processor's graphics and an AMD card, that can be the wrong one, and the test fails
  or runs on the processor's graphics. `HIP_VISIBLE_DEVICES` set to the card's number (`1`, say)
  hides the others: set it where every start sees it, the Start button's too (a user environment
  variable on Windows, the login shell's profile on Linux), then run `amd_setup.py --probe`.
- On Windows, switching the model in the popup restarts the server: freeing a model can hang
  AMD's runtime there, so the server hands the new name to its next start and exits with code
  3, and `run.cmd` (which the popup's **Start server** button runs too) starts it again five
  seconds later with that model. The subtitles stop for the restart and the load. A server
  started as a plain `python server.py` has nothing to restart it and refuses the switch;
  choose the model with `--model` there.
- On Windows the server cannot read the AMD card's free memory, so it loads float16 weights and
  never switches to int8 by itself; with little free VRAM, start it with
  `--compute-type int8_float16` or pick a smaller model. On Linux it reads the free memory from
  the amdgpu driver.
- The engine is built for the venv's Python version: a venv rebuilt on another one runs on the
  default engine until `amd_setup.py` has installed the matching build. A Shisu-ko update that
  pins a newer engine leaves the installed one working, and `amd_setup.py` offers the update
  (`Update the AMD engine? [y/N]`).
- On Linux a variable that every start needs (`ROCM_PATH` when ROCm is not in `/opt/rocm`,
  `HSA_OVERRIDE_GFX_VERSION`, `HIP_VISIBLE_DEVICES`) belongs in the login shell's profile: the
  popup's **Start server** button starts the server without a terminal's exports.

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
| No subtitles and no badge in the player's top left, not even "server offline" | Alt+Shift+H (or the popup's **Status badge on the video, errors too** switch) has hidden the badge; press it again. The popup's header still says whether the server is online. |
| Badge says "subtitles are running in another tab" | One video is transcribed at a time. Click into this tab, or close the other one. |
| Badge says "the speech is not in the subtitle language" | The server heard a minute of another language and stopped; it starts again when the subtitle language returns. For a video that really does mix languages, start the server with `--language-patience 0`. |
| Badge says "No speech found in this video" | The whole video, from its start, was transcribed and nothing was heard: a silent clip, an instrumental, a song Whisper does not hear as Japanese, or, with `--lyrics off`, any song. |
| A music video shows no subtitles | Since 0.11.3 a window the speech detector hears next to nothing in (under a second of speech) is transcribed without it when its audio is not silent and Whisper hears Japanese in it, so sung lyrics appear. A video watched before 0.12.0 gets its lines on the next visit: that release changed the shape of the cue cache, so every older result is ignored and the video is transcribed again from the start; nothing needs deleting from `~/.shisu-ko/cache`. A song Whisper does not hear as Japanese stays blank, as does loud non-speech (rain, a crowd, an engine). A server started with `--lyrics off` transcribes such windows with the detector as before, so a sung one stays blank. |
| Badge says "Shisu-ko server offline" | Start `server\run.cmd` or `docker\up.cmd`, or click **Start server** in the popup. Check the server URL in the popup. |
| **Start server** says "launcher not registered" | Run `server\setup.cmd` (Windows) or `bash server/setup.sh` once, or start the server by hand once: `run.cmd` / `run.sh` register the launcher with Firefox and Chrome on every start. `run.cmd --check` prints "Start button launcher: Firefox registered at …; Chrome registered at …" once it is. The launcher is registered only for Firefox, Chrome (on Linux and macOS its stable channel; Beta, Dev and Canary keep folders of their own) and, on Linux, Chromium: in another browser with the store install (Brave, Edge, Chromium outside Linux) start the server by hand. |
| **Start server** says "Allow Shisu-ko to talk to its launcher …" | The browser's permission prompt was declined. Click the button again and allow "Exchange messages with programs other than Firefox" (Firefox; the permission is also under Add-ons and themes > Shisu-ko > Permissions and data) or "Communicate with cooperating native applications" (Chrome). |
| No **Start server** button in Chrome | The button is there for the [Chrome Web Store](https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm) install only: an unpacked build (`dist/chrome`, the release zip) has an id of its own, which the launcher is not registered for. Start `server\run.cmd` / `server/run.sh` by hand, or install from the store. |
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
| "YouTube asks for a sign-in" | Run `server\run.cmd --save-cookies-from-browser firefox` once (`server/run.sh` on Linux/macOS, `nix run . -- --save-cookies-from-browser firefox` with Nix), then start the server again, by hand or with **Start server** (see [YouTube sign-in](#youtube-sign-in)). In Docker, export a `cookies.txt` into the data folder and add `--cookies /data/cookies.txt`. |
| "YouTube asks for a sign-in although the server sends firefox's YouTube cookies" | Firefox is not signed in to YouTube (or the sign-in has expired): sign in to YouTube in Firefox and play the video again. |
| Downloads fail after a YouTube update | Update yt-dlp: `~/.shisu-ko/venv/Scripts/python -m pip install -U yt-dlp` (Windows) or the `bin/python` equivalent; or start with `--allow-remote-ejs`. |
| "This live stream offers no audio segments (DVR may be disabled)" | The streamer turned DVR off. Nothing can be done until the stream is published as a video. |
| "The live stream has ended" | Reload the page once YouTube shows the recording; the server starts over on the video's clock. |
| Server says "Only N MiB of GPU memory is free" or restarts by itself | Other programs (games, Wallpaper Engine, VR software) hold most of the VRAM. The server switches to int8 weights; with under about 2.5 GB free the display driver can reset under load (Windows logs LiveKernelEvent 141). Close GPU-heavy apps or type `kotoba-tech/kotoba-whisper-v2.0-faster` into the popup's model field. Cached cues survive restarts. |
| CPU fallback, transcription far too slow | `run.cmd --check` should list one CUDA device (with the AMD engine, the AMD card counts as one); update the NVIDIA driver or type `small` into the popup's model field. |
| `run.cmd --check` says "GPU engine: AMD ROCm installed but not used" | The rest of the line says why: no test of the engine has passed yet, it was installed for another Python, it did not get a model onto the AMD GPU at its last start, or (Linux) ROCm 7.2's libraries are missing. `amd_setup.py --probe` tests it again and switches it on when it passes; `amd_setup.py --remove` takes it out. See [AMD graphics cards](#amd-graphics-cards-experimental). |
| Mining says "AnkiConnect denied access" | Click **Yes** in the dialog Anki shows, then mine again. |
| Mining says the card has no field "Picture" or "SentenceAudio" | Your note type names them differently; the message lists the card's own fields. Enter the right names under Anki, clips and server > Image field / Audio field in the popup (upper and lower case do not matter). |
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
  server.py           HTTP server: yt-dlp + faster-whisper + live follower + clip cutting
  setup.cmd/.sh       setup, downloads the model     run.cmd/.sh   start (with auto-restart)
  amd_setup.py        the experimental AMD engine, run by setup: looks for a card, installs
                      CTranslate2 for ROCm into ~/.shisu-ko/rocm and tests it (stdlib only)
  update.py           self-update run by run.cmd/.sh, first and after the server exits with code 4
                      (POST /update): git fast-forward or newest release
  native_host.py      native-messaging host behind the popup's Start server button (stdlib only);
                      native-host.cmd/.sh wrap it for Firefox and Chrome; --register writes a
                      host manifest for each browser
  tests/              pytest suite                   tools/        cue statistics, re-transcription
docker/               Windows wrappers for docker compose and the WSL engine installer
docs/                 subtitle-quality.md, screenshots, the demo recording, amo/ (the listing on
                      addons.mozilla.org), cws/ (the Chrome Web Store: how releases get there,
                      the key's one-time setup), dev/ (developer docs: the design of each subsystem)
scripts/cws.mjs       asks the Chrome Web Store what it holds, uploads a release's Chrome zip and
                      submits it for review
.github/workflows/    cws-listing.yml takes every release to the Chrome Web Store; cws-schedule.yml
                      runs it every three hours for a release held back behind an older review
Dockerfile, compose.yaml, compose.cpu.yaml, .env.example, flake.nix
sign-addon.cmd        signs a local build through addons.mozilla.org (unlisted; manual fallback)
publish-addon.cmd     submits a release to the public listing by hand (fallback for amo-listing.yml)
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
  `config.json` (the model and the browser for YouTube's sign-in chosen at setup, and
  `"engine": "rocm"` once the AMD engine's test has passed), the AMD engine's `rocm/` with its
  crash guard `rocm-starts` and, on Windows, `next-model` (the model a switch restarts the server
  into), the instance lock `server-8790.lock` (one per port,
  held while a server runs), `server.log` (the output of a server the popup started,
  Linux/macOS) and, on Windows, the launcher's host manifests `native-messaging/shisuko.json`
  (Firefox) and `native-messaging/shisuko-chrome.json` (Chrome).

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
and cooldown, the per-model cache files and what `/health` and `/sync` report.
`test_native_host.py` drives the native host behind the Start button: the message framing, the
two commands and every malformed request, the launch on each platform with a recorded `Popen`,
the instance lock shared with `server.py`, registration for every browser into a temporary home
with a fake registry, the host over a real pipe, and the wrapper and launcher scripts.
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
[CTranslate2](https://github.com/OpenNMT/CTranslate2), [yt-dlp](https://github.com/yt-dlp/yt-dlp),
[PyAV](https://github.com/PyAV-Org/PyAV), OpenAI's [Whisper](https://github.com/openai/whisper)
and [Kotoba-Whisper](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0). The mining flow
follows the conventions of [Yomitan](https://yomitan.wiki/), [AnkiConnect](https://foosoft.net/projects/anki-connect/)
and [asbplayer](https://github.com/killergerbah/asbplayer).

## License

MIT, see [LICENSE](LICENSE).
