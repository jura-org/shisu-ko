0.7.0 was the last version on this listing; 0.8.0 to 0.14.0 came out on GitHub only (https://github.com/Multysquid/shisu-ko/releases has every release's full notes). What changed since 0.7.0:

- 0.14.6: mining finds the fields in any case (Eminent's picture, sentenceAudio), and a card without them names its own. With "Particles count as known", the copula is found where Firefox's splitter hid it (大変だったね) and everyday grammar words (ある, する, この, こと …) count as known; a card for one decides its colour.
- 0.14.5: word colours show only what your Anki deck says. Particles (は, です) and names and Latin text (OK) are no longer coloured without a card; both are switches, off by default, in a new "More word colour options" drawer with the katakana switch and your known-words list.
- 0.14.4: speech Whisper skipped at the start of a stretch of audio (up to 20 seconds with no subtitle, even though someone was talking) is transcribed again and filled in. Videos you watched before are transcribed again on your next visit.
- 0.14.3: Alt+Shift+H hides the status badge on the video, the red "server offline" too (a popup switch as well). Every GitHub release carries an .xpi, the unsigned build while addons.mozilla.org reviews it; this listing gets selected releases, each as its number plus .1.
- 0.14.1: submitted to this listing with everything below (a newer listed version takes its place if its review had not finished).
- 0.14.0: Word colours: particles count as known and show green (a popup switch, on by default); names and Latin text are blue; your own list of known words, and Alt+Shift+K puts the word under the pointer on it; katakana words can count as known; a card for a noun also colours its する forms; the pitch overbar reads Yomitan's pitch graph.
- 0.13.0: a word mined in kanji is coloured where the subtitle writes it in kana (更に in さらに); a long line is never cut inside a word.
- 0.12.0: lines that are sentences, with their punctuation, never two sentences on one row; real lines are no longer thrown away as noise.
- 0.11: sung lyrics get subtitles; "No speech found in this video" when nothing was heard; a mined card gets the line that was on screen; lines merge across Whisper's segments and break between words; Left steps back a whole line.
- 0.11.0: word colours by the state of each word's Anki card (green, yellow, orange, red) and a pitch accent overbar, both off by default; setup asks which Whisper model to download.
- 0.10: only the tab you are watching is transcribed; subtitles pause when the speech is not Japanese.
- 0.9.0: the popup offers to update the server (the new notifications permission is for that); 0.8.0: a Start server button (optional nativeMessaging permission).
- Also: live streams; a master switch (Alt+Shift+S); the Whisper model picked in the popup; any installed font for the subtitles; a new card matched to its subtitle by sentence and word, with the screenshot and audio prepared while the line plays; Chrome support.
