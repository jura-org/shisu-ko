0.7.0 was the last version on this listing; 0.8.0 to 0.14.0 came out on GitHub only (https://github.com/Multysquid/shisu-ko/releases has every release's full notes). What changed since 0.7.0:

- 0.22.0: the server, not the extension, gives a new Yomitan card its line's audio and a picture of the video, and takes Alt+Shift+M; no downloads permission, no Downloads fallback.
- 0.21.1: the server syncs Anki after mining (--anki-sync off stops it); no alarms permission.
- 0.21.0: a Japanese video with subtitles its creator uploaded shows their text, timed by Whisper.
- 0.19.1, 0.19.2: subtitles for speech the voice detector misses (shouting, a quiet mix) and songs running past a window; a sentence's first words stay with it; no boxes with a hex code.
- 0.19.0: a phone portal (server/portal.cmd, .sh): subtitles, lookups, mining on a phone.
- 0.18.0: drag the subtitle by its grip to the top of the video, the window's bottom, or beside the video in vertical text.
- 0.17.3: a video YouTube refuses for a moment (HTTP 403) is fetched again within seconds.
- 0.17.0: an Apple Silicon Mac transcribes on its own GPU, on large-v3-turbo; a video in another language is left alone before it downloads.
- 0.16.0: the server runs Kitsune-Transcribe models next to Whisper: small Japanese-only ones, picked at setup or in the popup; setup installs PyTorch for them.
- 0.15.0: Whisper can run on an AMD graphics card (experimental): a Radeon RX 7000/9000 or Ryzen AI GPU (RX 6800/6900 on Linux too; 0.17.1: others at own risk, --ignore-old-graphics). Setup remembers which browser's YouTube cookies to send, for "Sign in to confirm you're not a bot".
- 0.14.6: mining finds the fields in any case, and a card without them names its own. "Particles count as known" also covers the copula and everyday grammar words.
- 0.14.5: word colours show only what your Anki deck says. Particles (は, です), names and Latin text are coloured without a card only by switches, off by default, in a new "More word colour options" drawer.
- 0.14.0: your own list of known words (Alt+Shift+K adds the word under the pointer); particles, katakana words, names and Latin text can be coloured; a card for a noun also colours its する forms; the pitch overbar reads Yomitan's pitch graph; 0.14.3: Alt+Shift+H hides the status badge; 0.14.4: speech Whisper skipped is filled in.
- 0.10 to 0.13: word colours by each word's Anki card state and a pitch accent overbar, both off by default; setup asks which Whisper model to download; only the tab you are watching is transcribed; subtitles pause while the speech is not Japanese; sung lyrics get subtitles; lines are whole sentences, never cut inside a word; a mined card gets the line on screen; Left steps back a line; the popup offers to update the server, and tells you when a newer release is out.
- Also: live streams; a master switch (Alt+Shift+S); the Whisper model picked in the popup; any installed font for the subtitles; Chrome support.
