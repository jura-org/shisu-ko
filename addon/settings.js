"use strict";

/*
 * Shared settings schema for Shisu-ko. This file is loaded before background.js, content.js and
 * popup.js (see manifest.json and popup.html), so all three see the same defaults.
 * To add a setting: add it here and add an input with the same id to popup.html.
 */

const SHISUKO_DEFAULT_SETTINGS = Object.freeze({
  // subtitles
  enabled: true,
  serverUrl: "http://127.0.0.1:8790",
  fontScale: 1.0,
  pauseOnHover: true,
  lingerSeconds: 0.3,
  showTranscript: false,
  hideNativeCaptions: false,
  showStatus: true,
  // Left/Right jump to the previous/next subtitle instead of YouTube's 5 s seek.
  arrowKeysJumpCues: true,
  // subtitle style and position. The defaults reproduce the look before these settings existed.
  subPosition: 11, // % of the player height between the bottom edge and the subtitle box
  subFont: "default", // "default" | "rounded" | "mincho" | "gothic-bold"
  // A font family installed on this computer, put in front of the preset's stack; the preset
  // stays the fallback and still decides the weight. Empty means the preset alone.
  subFontFamily: "",
  subTextColor: "#ffffff",
  subBackgroundOpacity: 72, // % alpha of the black box behind the text
  subOutline: false, // draw a black outline instead of relying on the box
  transcriptSide: "right", // "right" | "left"
  // transcription
  // The Whisper model the server should use: a faster-whisper size (large-v3, large-v3-turbo,
  // distil-large-v3, medium, small, base, tiny, ...) or the Hugging Face repo id "owner/name" of a
  // CTranslate2 model (kotoba-tech/kotoba-whisper-v2.0-faster). Empty means the server's --model.
  // The sizes name the same weights on an Apple GPU, where the server loads their MLX build
  // instead; a repo id must then be an MLX one (mlx-community/whisper-large-v3-mlx).
  model: "",
  // sentence mining
  mineTarget: "anki", // "anki" (newest card via AnkiConnect) or "download"
  // Attach screenshot + audio automatically when a new Anki note appears while a video is open.
  autoMine: true,
  mineFallbackDownload: true,
  ankiUrl: "http://127.0.0.1:8765",
  ankiImageField: "Picture",
  ankiAudioField: "SentenceAudio",
  ankiSentenceField: "",
  // Word/expression field, used to match a new card to its subtitle; empty means the note's
  // first field.
  ankiWordField: "",
  clipPaddingMs: 200,
  clipFormat: "mp3",
  // The note field holding Yomitan's pitch accent ({pitch-accent-categories}, {pitch-accent-positions}
  // or {pitch-accents}); empty means the first field whose name contains "pitch" or "accent".
  ankiPitchField: "",
  // word colours (both off by default: they need Anki running and a deck to look at)
  // Colour each word of a line by the state of its Anki card: green learned, yellow learning,
  // orange suspended, red new; a word with no card keeps the text colour.
  cardStatus: false,
  // The deck whose cards are looked at; empty means the deck the last mined card went to.
  cardStatusDeck: "",
  // An overbar in the colour of the word's pitch accent pattern (blue heiban, red atamadaka,
  // orange nakadaka, green odaka), read from the card's pitch accent field of the same deck.
  pitchAccent: false,
});
