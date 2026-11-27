# Using Shisu-ko

The [README](../README.md#using-shisu-ko) covers the basics. This page explains each part in
full: watching, the shortcuts, reading with Yomitan, sentence mining, the word colours, live
streams, every setting in the popup and the limitations.

- [Watching](#watching)
- [Shortcuts](#shortcuts)
- [Reading with Yomitan](#reading-with-yomitan)
- [Sentence mining](#sentence-mining)
- [Word colours](#word-colours)
- [Live streams](#live-streams)
- [Settings](#settings)
- [Limitations](#limitations)

## Watching

Open any YouTube video. The badge in the top-left corner of the player goes from "Fetching
audio…" through "Decoding audio…" to "Transcribing…" (with "Loading model X…" while the server
loads its model, which it does when a video needs it), and the first subtitles appear after a few
seconds. From then on the server stays ahead of you. The toolbar popup holds every setting, and
the switch in its header turns the whole extension off and on again.

Only the video you are watching is transcribed. Other YouTube tabs say "subtitles are running in
another tab" and take over the moment you click into them, so two open videos never compete for
the GPU. A video YouTube itself declares to be in another language is never downloaded at all: the
badge says "the speech is not in the subtitle language" a second after you open it. One that
declares nothing, and then speaks another language, stops after about a minute of it and starts
again by itself when the subtitle language comes back. A Kitsune model has no language detector,
so with one only YouTube's own label counts, and a video YouTube names nothing for is transcribed
as Japanese whatever it speaks.

## Shortcuts

| Shortcut | Action |
|---|---|
| Alt+Shift+S | Turn Shisu-ko on or off (the switch in the popup header) |
| Alt+Shift+L | Toggle the transcript panel |
| Alt+Shift+M | Mine the current sentence into the newest Anki card (audio + picture) |
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
and its timestamps seek the video. A subtitle docked outside the video (the window's bottom, or
beside it) pauses the same way, and leaving it for the page around it resumes the video as well.

## Sentence mining

The server mines, not the extension. A card gets two things for its line: an MP3 clip of that
line's audio, cut from the original track with a little padding on both sides, and a picture of
the video a moment into the line. The line on screen is the whole of it: the clip covers exactly
the seconds of that one subtitle, never the lines around it.

**Anki.** Normally you never trigger mining at all:

1. Hover the subtitle; the video pauses.
2. Scan the word with Yomitan and click its **+** (Yomitan fills word, reading, sentence and
   glossary from its own template).
3. Within about a second the server notices the new card, uploads `shisuko_<video>_<time>.mp3` and
   `.jpg` to Anki's media folder and writes `[sound:...]` and `<img src=...>` into the fields. A
   toast on the player confirms it. Playback is left alone so you can keep reading the popup.

While a tab with auto-attach on is syncing, the server looks at Anki's newest notes every second
while its video is paused (for up to two minutes) and every 3 s while it plays; every 15 s
otherwise. While a line plays, the server already cuts its audio and takes its picture, so the
card only has to receive them. The card is matched to the line it is about: the server compares
the card's sentence and word against the transcript of the video you are watching, so a card
made a few lines later, or while the video kept playing, still gets the right audio and picture.

The server only touches a card made in the last three minutes, while a video was syncing, whose
sentence matches one of that video's lines. It fills only empty fields, with one exception: when
Yomitan cut the card's sentence short at a 。 or ！ inside the line, the server writes the whole
line in its place, keeping Yomitan's bold word. It reads the card again right before writing,
leaves a field you changed meanwhile alone, and judges each card once. Several cards appearing at
once (an import, a sync from another device) are left alone, and so is a card without a sentence.
A card that matches no line is left alone too (the server log says why only with
`--log-level DEBUG`). Turn the watching off with **Auto-attach to new Yomitan cards** in the
popup; the shortcut and the pickaxe keep working either way and attach to the newest card added
today.

To mine by hand instead:

- hover the subtitle and click the pickaxe that appears at its right edge,
- press Alt+Shift+M while watching,
- click the pickaxe on any line in the transcript panel.

The request rides the next `/sync` to the server, which answers with what it did: the fields it
filled, or why it filled none (no card today, the card already has its audio and picture, the note
type lacks the fields). Field names default to `Picture` and `SentenceAudio`, as used by common
Japanese mining note types, and match whatever case your note type writes them in (`picture` and
`sentenceAudio` in Eminent); change them in the popup when yours are named differently. The popup
sends them to the server with every `/sync`. An optional sentence field is filled with the
subtitle text by a mine by hand, and only when it is empty. A sentence Yomitan cut short grows to
the whole line, by hand or automatically, but only when it is a piece of that line: text of your
own is never touched. An optional word field names the field holding the expression, used to tell two similar
lines apart and, by the [word colours](#word-colours), to read each card's word; left empty, the
note's first field is read.

**The picture.** The server takes it from YouTube's own stream, not from the browser: it resolves
an H.264 stream of 720p at most through yt-dlp, with the same cookies as the audio download, once
per video, and keeps it 4 hours; PyAV decodes the frame. A DRM-protected video, or one yt-dlp
cannot resolve, gets the audio alone; a resolve that failed for any reason but DRM is tried again
after 10 minutes. Before 0.22.0 the browser drew the frame, which failed on
DRM videos as well; now a video the browser could draw but yt-dlp cannot resolve gets no picture
either. `--mine-picture off` attaches the audio alone, and `--mine off` stops the server writing
to Anki at all (see [Server options](server.md#server-options)). A live stream's card gets its audio and no
picture.

Without Anki there is nothing to mine into, so a mine you ask for (Alt+Shift+M or a line's
pickaxe) saves the line instead: when the server says Anki is not running, or mines nothing
(`--mine off`, or the Docker image without `--anki-url`), the line's audio (from the server, with
the popup's padding and format) and a screenshot of the video taken as you mined go to
`Downloads/shisu-ko-mining`, and the toast says so. A DRM-protected video gives no screenshot,
only the audio.

**Sync with AnkiWeb.** The server watches Anki and asks it to sync (AnkiConnect's `sync`) a
minute after a new card appears, so the card is on your phone without pressing Sync. It does not
matter what made the card: Yomitan with the extension, the phone portal, or Anki itself. Every 15 s
the server asks AnkiConnect for the notes added today and yesterday; cards that were there when it
started never count. Cards made in that minute go with the sync; while you keep mining, the next
sync follows 30 s after the one before began, and once 30 s pass without a new card it stops. Anki
must be signed in to AnkiWeb. A failed sync is tried again after 1, 2 and then every 5 minutes
while cards are waiting; "not signed in" and a full sync Anki wants you to confirm wait for your
next card instead. With Anki closed the server looks once a minute, logs one line, and syncs
when Anki is back. Nothing ever interrupts you; the server log says what happened, and the
portal's Settings > Anki shows the last sync. `--anki-sync off` turns it off (see
[Server options](server.md#server-options)). The extension itself never syncs and has no switch for it.

The Shisu-ko server uses port 8790 precisely so that AnkiConnect can keep its default 8765.

## Word colours

Two optional colourings, both off by default, both in the popup's **Word colours** section. They
need Anki running with AnkiConnect, as mining does, and the Shisu-ko server 0.23.0 or newer: the
server reads your deck (through its `--anki-url`, like the mining) and sends each card's word,
state and pitch accent with the subtitles, every card the first time and the changed ones after
that. The extension never talks to AnkiConnect, so there is no permission dialog to click. Nothing
leaves your computer, and nothing is written to Anki for the colours. A card reviewed in Anki shows
its new colour within about half a minute, a card just made within seconds.

**Colour words by their Anki card** colours each word of a subtitle line, and of the transcript
panel, by the state of its card: green for a card you have learned (in review), yellow for one you
are still learning, orange for a suspended card, red for a new one. A word with no card keeps the
text colour, unless it is a word on your list of known words, or a particle, katakana word or name
you chose to colour (below). The cards come from one deck. Left on *Automatic*, that is the deck the
last card the server put media into went to: nothing is looked up until a card is mined, and the
first mine then names the deck (the hint under the **Deck** select says which, or "no card mined
yet"). The server remembers it across restarts in `~/.shisu-ko/anki-deck.json`. After the
update from 0.22.0 or older, the extension hands the server the deck it remembered, once. Choose a
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

![Popup, dark theme](images/popup-dark.png)

The switch in the header is the master switch. Off means nothing happens on YouTube pages: no
overlay, no requests to the server, no Anki watching, no word colours and no key handling, until
it is switched on again (Alt+Shift+S flips it too). The status line beside the switch says whether the server
answers, and with which model and device ("loads when a video needs it" while the server keeps the
model out of memory, see [`--model-idle-minutes`](server.md#server-options)); while it does not answer, a **Start server** button
on that line launches it (Firefox and the Chrome Web Store install, see
[Start the server](install.md#the-start-server-button)) with the options in **Start server
options**, a drawer of its own: empty, the server starts with its defaults; `--lyrics off
--beam-size 1` or `--device cpu` there go to every start the button makes, and the line under
the field says what it will pass, or which word the launcher would refuse. The **?** beside the
field opens the list of every option it takes, with the values each allows, its default and what
it does (more in [Server options](server.md#server-options)). The model field below takes effect once the server
is up. When another server answers on the Start button's address, one that is not this
computer's own (an old copy, or a Docker container), the status line says **Other server** and
who it is instead of "Server online": stop that one, and the button is back. The same page opens
as the add-on's preferences under Add-ons and themes > Shisu-ko.

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
line at the bottom of the *Anki, clips and server* drawer shows the server's daily check
("Newest release: 0.9.0, the server checked 3 min ago", or why its check failed).

| Setting | Meaning |
|---|---|
| Pause video while hovering a line | Needed for comfortable Yomitan lookups |
| Left/Right jump between subtitles | Arrow keys move between cues instead of seeking five seconds |
| Transcript panel | List of all cues so far, with jump and mine buttons |
| Auto-attach to new Yomitan cards | Watches AnkiConnect and fills the new card by itself; off means Alt+Shift+M or the pickaxe |
| Colour words by their Anki card | Colours each word of a line by the state of its card in the deck below: green learned, yellow learning, orange suspended, red new; green for your known words and, as the three switches under More word colour options say, green for particles and katakana words and blue for names and Latin text; other words keep the text colour. Needs Anki with AnkiConnect, which the server reads, see [Word colours](#word-colours) |
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

![Overlay with custom style](images/overlay-styled.png)

| Style setting | Meaning |
|---|---|
| Place | Where the subtitle sits: bottom or top of the video, bottom of the window, or left or right of the video in vertical text, like a book. Dragging the grip (⠿) at the box's edge does the same: the docks light up, and the release snaps to the nearest. A side too narrow for two columns (theater mode, a small window) puts the column inside the video at that edge. Fullscreen shows only the video, so there the window's bottom becomes the video's and the sides go inside; leaving fullscreen puts them back |
| Height above the bottom | For the bottom of the video: 2-40% of the player height; it still drops when YouTube's controls fade out |
| Font | Gothic (the default stack), Gothic bold, Rounded or Mincho, for the subtitle and the transcript |
| Font family | Any font installed on this computer, by name. It goes in front of the preset's stack, so the preset stays the fallback and still decides the weight. The popup previews the result and says whether the name resolved; Firefox cannot list installed fonts, hence the suggestions instead of a menu |
| Text colour | Colour of the subtitle text |
| Background | Opacity of the black box behind the text, 0-100% |
| Outline the text | Black outline instead of the box; readable over bright video with the background turned down |
| Transcript panel side | Docks the panel right or left; the subtitle moves out of its way |
| Reset style | Restores the eight settings above and nothing else |

The **Transcription model** drawer picks the model the server runs. The field takes a
Kitsune-Transcribe model (`kitsune-0.6b`, `kitsune-0.3b-int8`, ...; see
[Kitsune-Transcribe models](server.md#kitsune-transcribe-models)), a
faster-whisper size (`large-v3`, `large-v3-turbo`, `distil-large-v3`, `medium`, `small`, ...)
or the Hugging Face repo id `owner/name` of a CTranslate2 model
(`kotoba-tech/kotoba-whisper-v2.0-faster`); empty means the server's own default (`--model`, else
the model chosen at setup in `~/.shisu-ko/config.json`, else large-v3, or large-v3-turbo on an
Apple GPU), which the placeholder shows. The sizes mean the same on an Apple GPU, where the server
fetches the MLX build of those weights; a repo id must then be an MLX one
(`mlx-community/whisper-large-v3-mlx`).
Models already downloaded are offered as suggestions. The change applies while
a video plays: a model that is not on disk yet is downloaded from Hugging Face first, while the
current model keeps subtitling, and once the swap is done the video's transcript starts over with
the new model. Meanwhile the badge on the video says "Loading model X…"; a name the server cannot
use shows "Shisu-ko: model X: …" with the reason, even with progress messages off, and the previous
model keeps running. The popup mirrors this under the field: the status badge says "Loading model"
during a switch, and the hint under the field carries the server's verdict on the name you typed.
With the default `--model-idle-minutes` (not on the AMD engine) a swap that fails brings nothing
back: the server stays up with the previous model's subtitles and loads that model again when a
video needs it.
With the experimental AMD engine on Windows the swap is a restart of the server (see
[AMD graphics cards](amd.md)).

The last drawer, **Anki, clips and server**, holds the image, audio, sentence, word and pitch
accent field names (the last one
empty means the first field named pitch or accent that holds a readable value), the audio
padding around the sentence, the clip format (MP3 or WAV), the Shisu-ko server URL (default
`http://127.0.0.1:8790`) and the result of the server's last update check.

## Limitations

- Subtitles are hidden while YouTube plays an ad.
- YouTube changes its player regularly. Downloads can then fail until a new yt-dlp is out, usually
  within days, and you have installed it (see [Troubleshooting](troubleshooting.md)). Shisu-ko's
  own updates do not upgrade yt-dlp.
- Whisper sometimes invents a line over music or silence. Loud noise and songs in other languages
  stay blank.
- Live streams work in Firefox only, and not on streams without DVR. On low-latency streams the
  subtitles can trail the sound by a few seconds.
- One video is transcribed at a time.
