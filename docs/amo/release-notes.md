0.7.0 was the last version on this listing; 0.8.0 to 0.14.0 came out on GitHub only (https://github.com/Multysquid/shisu-ko/releases has every release's full notes). What changed since 0.7.0:

- 0.27.0: the phone portal keeps your watch history: a finished video leaves Up next for Watched. A resumed video's subtitles no longer freeze on iPhone.
- 0.26.1: on an NVIDIA GPU the server transcribes 3 minutes ahead, not 15: the same subtitles for less GPU work.
- 0.26.0: the phone portal gets a landscape mode with iOS-style subtitles, double tap to skip lines, YouTube's red watch bar, swipe actions, lookups from the tapped character, and your YouTube history (--watch-history on).
- 0.25.0: a Yomitan card gets its line's audio and picture in about a second.
- 0.24.0: the server, not the extension, looks for a newer release, once a day. Without Anki, a mine saves the line's audio and picture to Downloads again.
- 0.21.1 to 0.23.0: the server syncs Anki after mining, gives a Yomitan card its line's audio and picture (Alt+Shift+M too) and reads your deck for the word colours; no alarms permission.
- 0.21.0: a Japanese video with its creator's subtitles shows their text, timed by Whisper.
- 0.19.1, 0.19.2: subtitles for speech the voice detector misses and songs running past a window; no boxes with a hex code.
- 0.19.0: a phone portal: subtitles, lookups, mining on a phone.
- 0.18.0: drag the subtitle above or below the video, or beside it in vertical text.
- 0.17.0: an Apple Silicon Mac transcribes on its own GPU; a video in another language is skipped before it downloads.
- 0.16.0: Kitsune-Transcribe models beside Whisper, small and Japanese-only; setup installs PyTorch for them.
- 0.15.0: Whisper on an AMD graphics card (experimental; 0.17.1: --ignore-old-graphics for older ones). Setup remembers which browser's YouTube cookies to send.
- 0.14.6: mining finds the fields in any case, and a card without them names its own. "Particles count as known" also covers the copula and everyday grammar words.
- 0.14.5: word colours show only what your Anki deck says. Particles (は, です), names and Latin text are coloured without a card only by switches, off by default.
- 0.14.0: your own list of known words (Alt+Shift+K); particles, katakana words, names and Latin text can be coloured; a card for a noun also colours its する forms; the pitch overbar reads Yomitan's pitch graph; 0.14.3: Alt+Shift+H hides the status badge; 0.14.4: speech Whisper skipped is filled in.
- 0.10 to 0.13: word colours by each word's Anki card state and a pitch accent overbar, both off by default; only the tab you are watching is transcribed; sung lyrics get subtitles; lines are whole sentences, never cut inside a word; a mined card gets the line on screen; Left steps back a line; the popup offers server updates.
- Also: live streams; a master switch (Alt+Shift+S); the Whisper model picked in the popup; any installed font for the subtitles; Chrome support.
