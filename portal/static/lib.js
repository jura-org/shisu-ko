// SPDX-License-Identifier: Apache-2.0
"use strict";
// Pure helpers for the portal page. No DOM, no network: node --test loads this file directly.

(function (root) {
  const VIDEO_ID_RE = /^[A-Za-z0-9_-]{6,20}$/;
  const YOUTUBE_HOSTS = /(^|\.)(youtube\.com|youtube-nocookie\.com|youtu\.be)$/i;

  // "1h2m3s", "90", "90s" -> seconds; anything else -> 0.
  function parseStart(value) {
    if (!value) return 0;
    if (/^\d+(\.\d+)?$/.test(value)) return Number(value);
    const m = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/.exec(value);
    if (!m || !m[0]) return 0;
    return (Number(m[1]) || 0) * 3600 + (Number(m[2]) || 0) * 60 + (Number(m[3]) || 0);
  }

  // A YouTube link or a bare id -> {id, t}, or null. Takes watch, youtu.be, shorts, live and embed links.
  function parseVideoInput(input) {
    const text = String(input || "").trim();
    if (!text) return null;
    if (VIDEO_ID_RE.test(text)) return { id: text, t: 0 };
    let url;
    try {
      url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : "https://" + text);
    } catch {
      return null;
    }
    if (!YOUTUBE_HOSTS.test(url.hostname)) return null;
    const t = parseStart(url.searchParams.get("t") || url.searchParams.get("start"));
    let id = null;
    if (/(^|\.)youtu\.be$/i.test(url.hostname)) {
      id = url.pathname.split("/")[1];
    } else if (url.searchParams.get("v")) {
      id = url.searchParams.get("v");
    } else {
      const m = /^\/(?:shorts|live|embed|v)\/([^/?#]+)/.exec(url.pathname);
      if (m) id = m[1];
    }
    return id && VIDEO_ID_RE.test(id) ? { id, t } : null;
  }

  // Index of the cue to show at time t: the one playing, else the last one that started (it stays
  // up through the gap after it). -1 before the first cue.
  function activeCueIndex(cues, t) {
    let lo = 0;
    let hi = cues.length - 1;
    let idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].start <= t + 0.05) {
        idx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return idx;
  }

  // Where playback picks up when the dictionary sheet closes. `at` is the playhead at the tap, which
  // comes a second or more after the words that made the viewer stop: resuming there loses them.
  // The line is the tapped word's when it is the line being said or the one before it (still on
  // screen), else the line being said. "line" resumes at its start; anything else LOOKUP_LEAD_S
  // before the tap, never before the line's start and never after the tap. Before the first line
  // (an intro, or a tap on the line still to come) there is no line to anchor to: both modes
  // resume LOOKUP_LEAD_S before the tap, so a long intro is never rewound to 0:00.
  const LOOKUP_LEAD_S = 1;
  function lookupResume(cues, at, tappedId, mode) {
    const active = activeCueIndex(cues, at);
    const tapped = cues.findIndex((c) => c.id === tappedId);
    const cue = cues[tapped >= 0 && (tapped === active || tapped === active - 1) ? tapped : active] || null;
    const lead = at - LOOKUP_LEAD_S;
    const t = !cue ? lead : mode === "line" ? cue.start : Math.max(cue.start, lead);
    return { t: Math.max(0, Math.min(at, t)), cue };
  }

  function formatTime(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  }

  // A clock for the scrubber: h:mm:ss only when the video is an hour or longer, so both labels keep
  // one width for the whole video.
  function formatClock(seconds, duration) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const pad = (n) => String(n).padStart(2, "0");
    if ((Number(duration) || 0) >= 3600) return `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
    return `${Math.floor(s / 60)}:${pad(s % 60)}`;
  }

  // The same in words, for a screen reader: "12 minutes 5 seconds".
  function spokenTime(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const parts = [[Math.floor(s / 3600), "hour"], [Math.floor((s % 3600) / 60), "minute"], [s % 60, "second"]]
      .filter(([n], i, all) => n > 0 || (i === 2 && all.every(([m]) => m === 0)))
      .map(([n, unit]) => `${n} ${unit}${n === 1 ? "" : "s"}`);
    return parts.join(" ");
  }

  // The end of the covered range holding t, or null.
  function coveredUntil(covered, t) {
    for (const [a, b] of covered || []) if (t >= a - 0.5 && t <= b) return b;
    return null;
  }

  // The server's state in plain words, from the last /sync answer.
  // Every string the page shows, in one place, so a translation is one table. Functions take the
  // values they need. English only for now.
  const STRINGS = {
    reconnecting: "Reconnecting…",
    // The double tap's badge on the video: how many lines it stepped, the arrows pointing the way.
    linesBack: (n) => `« ${n} ${n === 1 ? "line" : "lines"}`,
    linesForward: (n) => `${n} ${n === 1 ? "line" : "lines"} »`,
    reconnectingDetail: (e) => `Reconnecting to the subtitle server…${e ? ` (${e})` : ""}`,
    reconnectingError: (e) => `Reconnecting… (${e})`,
    connecting: "Connecting…",
    modelProblem: (e) => `Model problem: ${e}`,
    languagePausedDetail: (heard) => `Paused: the speech is not in the subtitle language${heard ? ` (hearing ${heard})` : ""}`,
    languagePaused: "Subtitles paused: the speech is not in the subtitle language",
    loadingModel: (m) => `Loading model ${m}…`,
    fetchingAudio: "Fetching audio…",
    decodingAudio: "Decoding audio…",
    serverError: (when, e) => `Server error (${when}): ${e || "unknown"}`,
    gaveUp: "gave up",
    retryingIn: (s) => `retrying in ${s} s`,
    retryingLower: "retrying",
    transcribingFromHere: "Transcribing from here…",
    readyToEnd: "Subtitles ready to the end",
    noSpeech: "No speech found",
    transcribingTo: (t) => `Transcribing… (ready to ${t})`,
    readyTo: (t, busy) => `Subtitles ready to ${t}${busy ? ", transcribing ahead" : ""}`,
    serverState: (st) => `Server: ${st}`,
    waitingForServer: "Waiting for the server…",
    unavailable: "Subtitles unavailable",
    retrying: "Retrying…",
    retryingCountdown: (s) => `Retrying in ${s} s…`,
    transcribing: "Transcribing…",
    // the page
    notALink: "That isn't a YouTube link or video ID.",
    rowActions: (name) => `Actions for ${name}`, // the edit minus: Mark as watched (with a duration) and Delete
    deleteVideo: (name) => `Delete ${name}`,
    markWatchedVideo: (name) => `Mark as watched: ${name}`,
    markUnwatchedVideo: (name) => `Mark as unwatched: ${name}`,
    delete: "Delete",
    markWatched: "Watched",
    markUnwatched: "Unwatched",
    edit: "Edit",
    done: "Done",
    close: "Close",
    back: "Back",
    settings: "Settings",
    // Settings > Subtitle style (the landscape mode's caption box)
    subtitleStyle: "Subtitle style",
    captionBackground: "Background",
    captionSize: "Text size",
    captionPosition: "Position",
    captionNone: "None",
    captionSmall: "Small",
    captionMedium: "Medium",
    captionLarge: "Large",
    captionLower: "Lower",
    captionDefault: "Default",
    captionHigher: "Higher",
    captionSample: "今日は雨が降っています",
    captionStyleNote: "For the subtitle over the video in the landscape mode.",
    play: "Play",
    pause: "Pause",
    lookingUp: "Looking up…",
    noEntry: "No dictionary entry.",
    yomitanDown: "The dictionary is not answering. Open the browser that has Yomitan, with the Yomitan API installed.",
    yomitanDocs: "How to install the Yomitan API",
    mine: "Mine",
    inDeckAgain: "In deck · Add again",
    moreDictionaries: "More dictionaries",
    added: (w) => `Added ${w}`,
    couldNotAdd: (w) => `Couldn't add ${w}`,
    alreadyInDeck: (w) => `Couldn't add ${w}: already in deck`,
    ankiDown: (w) => `Couldn't add ${w}: Anki is not running`,
    setupFirst: "Set up your Anki card first",
    noAudio: (w) => `No audio for ${w}`,
    retry: "Retry",
    emptyRecent: "Videos you watch show up here, with how far you got.",
    setupTitle: "Before you start",
    setupAnki: "Choose the deck and note type for your cards",
    setupAnkiButton: "Set up Anki",
    ankiSaved: "Anki setup saved",
    ankiLoadFailed: "Anki is not answering. Start Anki with AnkiConnect, then open this again.",
    ankiNoDecks: "Anki has no decks yet.",
    ankiOverride: (keys, file) => `${file} sets ${keys}; those choices are fixed here.`,
    ankiSyncOff: "Anki sync: off on the server",
    ankiSyncWaiting: "Anki sync: on, after the next new card",
    ankiSyncRunning: "Anki sync: syncing…",
    ankiSyncLast: (when) => `Anki sync: last ${when}`,
    ankiSyncFailed: (why) => `Anki sync failed: ${why}`,
    fieldEmpty: "Empty",
    fieldCustom: "Custom template…",
    groupPortal: "From the video",
    groupYomitan: "From the dictionary",
    devicesNone: "No devices paired yet.",
    deviceForget: "Forget",
    deviceThis: "this device",
    deviceSeen: (when) => `last seen ${when}`,
    notPaired: "This device is no longer paired.",
    usingYouTube: "Using YouTube's player",
    watchHistoryOn: "YouTube history: on, what you watch here counts on your YouTube account",
    watchHistoryOff: "YouTube history: off (start the portal with --watch-history on)",
    watchHistoryNoCookies: "YouTube history: needs the server's YouTube cookies (--save-cookies-from-browser)",
    position: "Position",
    positionOf: (at, total) => `${at} of ${total}`,
    live: "LIVE",
    resumeAfterLookup: "Resume after a lookup",
    resumeBeforeTap: "Just before where I tapped",
    resumeLineStart: "Start of the line",
    fullscreen: "Full screen",
    exitFullscreen: "Exit full screen",
  };

  function statusText(view) {
    const S = STRINGS;
    if (view.offline) return { text: S.reconnectingDetail(view.error), error: true };
    if (view.error) return { text: S.reconnectingError(view.error), error: true };
    const d = view.data;
    if (!d) return { text: S.connecting, error: false };
    if (d.model_error) return { text: S.modelProblem(d.model_error), error: true };
    if (d.language_paused) return { text: S.languagePausedDetail(d.heard), error: false };
    if (d.model_loading && d.status !== "error") return { text: S.loadingModel(d.model_loading), error: false };
    switch (d.status) {
      case "pending":
      case "downloading":
        return { text: S.fetchingAudio, error: false };
      case "decoding":
        return { text: S.decodingAudio, error: false };
      case "error": {
        const when = d.retrying === false ? S.gaveUp : Number.isFinite(d.retry_in) ? S.retryingIn(Math.round(d.retry_in)) : S.retryingLower;
        return { text: S.serverError(when, d.error), error: true };
      }
      case "ready": {
        const ahead = coveredUntil(d.covered, view.t);
        const duration = d.duration || 0;
        if (ahead === null) return { text: S.transcribingFromHere, error: false };
        if (duration && ahead >= duration - 1) return { text: view.cueCount ? S.readyToEnd : S.noSpeech, error: false };
        if (ahead - view.t < 8) return { text: S.transcribingTo(formatTime(ahead)), error: false };
        return { text: S.readyTo(formatTime(ahead), d.busy), error: false };
      }
      default:
        return { text: d.status ? S.serverState(d.status) : S.waitingForServer, error: false };
    }
  }

  // What the watch view says about the server: only what changes what the viewer does next.
  // {text, busy} or null; null means healthy, and nothing is shown.
  function watchStatus(view) {
    const S = STRINGS;
    // The heartbeat keeps going in every one of these states; the words only say what happens next.
    if (view.offline || view.error) return { text: S.reconnecting, busy: true };
    const d = view.data;
    if (!d) return { text: S.connecting, busy: true };
    if (d.language_paused) return { text: S.languagePaused, busy: false };
    if (d.status === "error") {
      // Only a server that says it gave up makes the error final. One that says nothing (before
      // server-side backoff) retries the fetch when a later heartbeat arrives, so it is retrying too.
      if (d.retrying === false) return { text: S.unavailable, busy: false };
      const left = retryLeft(view);
      return { text: left === null ? S.retrying : S.retryingCountdown(left), busy: true };
    }
    if (d.model_loading || ["pending", "downloading", "decoding", "connecting"].includes(d.status)) {
      return { text: S.transcribing, busy: true };
    }
    if (d.status === "ready") {
      const duration = d.duration || 0;
      if (coveredUntil(d.covered, view.t) === null && !(duration && view.t >= duration - 1)) {
        return { text: S.transcribing, busy: true };
      }
    }
    return null;
  }

  // ---- word timing inside a cue

  const SMALL_MORA = "ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ";
  const PAUSE_SHORT = /^[、，,・]$/;
  const PAUSE_LONG = /^[。．.！!？?…‥]$/;
  const MAX_WORD_SKIP = 3; // characters a word may lie past the last one matched (punctuation)

  // Morae in a kana string: a small ゃ/ゅ/ょ (and small vowels) join the mora before it; っ and ー
  // count, as they take a beat of their own.
  function moraCount(kana) {
    let n = 0;
    for (const ch of String(kana || "")) {
      if (/[ぁ-ゖァ-ヺー]/.test(ch) && !SMALL_MORA.includes(ch)) n += 1;
    }
    return n;
  }

  // Speaking weight of one character with no reading at hand.
  function charWeight(ch) {
    if (PAUSE_SHORT.test(ch)) return 1.5;
    if (PAUSE_LONG.test(ch)) return 3;
    if (/[ぁ-ゖァ-ヺー]/.test(ch)) return SMALL_MORA.includes(ch) ? 0 : 1;
    if (/[㐀-鿿豈-﫿々]/.test(ch)) return 1.5; // a kanji is one to two morae
    if (/[0-9０-９]/.test(ch)) return 1;
    if (/[A-Za-zＡ-Ｚａ-ｚ]/.test(ch)) return 0.4;
    if (/\s/.test(ch)) return 0.3;
    return 0; // brackets, quotes and other marks take no time
  }

  // Weight of a unit: a token {segs: [[text, reading]]} or a single character {segs: [[ch, ""]]}.
  function unitWeight(unit) {
    let w = 0;
    for (const [text, reading] of unit.segs) {
      if (reading) w += moraCount(reading);
      else for (const ch of text) w += charWeight(ch);
    }
    return w;
  }

  // Estimated [start, end] per unit, spread over the cue by speaking weight at the cue's own pace.
  function estimateUnitTimes(units, start, end) {
    const weights = units.map(unitWeight);
    // A cue's closing punctuation is said in the gap after the cue, not inside it.
    for (let i = weights.length - 1; i >= 0 && !units[i].term && /^[\s、。，．,.!?！？…‥」』）)]*$/.test(units[i].text); i--) weights[i] = 0;
    const total = weights.reduce((a, b) => a + b, 0);
    const span = Math.max(0, end - start);
    let acc = 0;
    return units.map((_, i) => {
      const a = total > 0 ? start + (acc / total) * span : start + (i / units.length) * span;
      acc += weights[i];
      const b = total > 0 ? start + (acc / total) * span : start + ((i + 1) / units.length) * span;
      return [a, b];
    });
  }

  // Real word times (a cue's `words: [{start, end, text}]`, should the server ever send them) mapped
  // onto the units by character position. Characters no word covers take the time before them.
  function unitTimesFromWords(units, text, words, start, end) {
    const at = new Array(text.length).fill(null);
    let pos = 0;
    for (const w of words) {
      const piece = String(w.text || "").trim();
      if (!piece || !Number.isFinite(w.start) || !Number.isFinite(w.end)) continue;
      const idx = text.indexOf(piece, pos);
      // A match far ahead is a stray word (from the next or previous cue, or a repeated phrase):
      // taking it would skip every word in between.
      if (idx < 0 || idx - pos > MAX_WORD_SKIP) continue;
      for (let k = 0; k < piece.length; k++) at[idx + k] = w.start + (k / piece.length) * (w.end - w.start);
      pos = idx + piece.length;
    }
    let last = start;
    for (let k = 0; k < at.length; k++) {
      if (at[k] === null) at[k] = last;
      else last = at[k];
    }
    const starts = units.map((u) => (u.start < at.length ? at[u.start] : end));
    return starts.map((a, i) => [a, i + 1 < starts.length ? Math.max(a, starts[i + 1]) : end]);
  }

  // Units of a cue, each with its character offset: tokens when Yomitan has tokenised the line,
  // else one unit per character.
  function cueUnits(text, tokens) {
    if (tokens && tokens.length) return tokens.map((t) => ({ ...t, segs: t.segs || [[t.text, ""]] }));
    return Array.from(text).reduce((out, ch) => {
      const startAt = out.length ? out[out.length - 1].end : 0;
      out.push({ text: ch, segs: [[ch, ""]], start: startAt, end: startAt + ch.length, term: null });
      return out;
    }, []);
  }

  // Japanese gets the mora estimate; any other language an even spread by letters and digits
  // (punctuation and spaces take no time), which needs no knowledge of the language.
  function evenUnitTimes(units, start, end) {
    const weights = units.map((u) => (u.text.match(/[\p{L}\p{N}]/gu) || []).length);
    const total = weights.reduce((a, b) => a + b, 0);
    const span = Math.max(0, end - start);
    let acc = 0;
    return units.map((_, i) => {
      const a = total > 0 ? start + (acc / total) * span : start + (i / units.length) * span;
      acc += weights[i];
      return [a, total > 0 ? start + (acc / total) * span : start + ((i + 1) / units.length) * span];
    });
  }

  function unitTimes(cue, units, lang = "ja") {
    if (Array.isArray(cue.words) && cue.words.length) return unitTimesFromWords(units, cue.text, cue.words, cue.start, cue.end);
    return isJapanese(lang) ? estimateUnitTimes(units, cue.start, cue.end) : evenUnitTimes(units, cue.start, cue.end);
  }

  // Japanese-only features (furigana, the mora estimate, Japanese fonts) follow the server's language.
  function isJapanese(lang) {
    return !lang || /^ja\b/i.test(lang);
  }

  // Words of a line for a language Yomitan cannot tokenise: Intl.Segmenter where there is one, else
  // runs of letters and digits. Same shape as the server's tokens; words are tappable.
  function segmentWords(text, lang) {
    const out = [];
    const push = (piece, isWord) => out.push({ text: piece, segs: [[piece, ""]], term: isWord ? piece : null });
    if (typeof Intl !== "undefined" && Intl.Segmenter) {
      for (const seg of new Intl.Segmenter(lang || undefined, { granularity: "word" }).segment(text)) push(seg.segment, seg.isWordLike);
    } else {
      for (const m of text.matchAll(/[\p{L}\p{N}'’-]+|[^\p{L}\p{N}'’-]+/gu)) push(m[0], /[\p{L}\p{N}]/u.test(m[0]));
    }
    return out;
  }

  // Yomitan answers languages it does not know with tokens but no headwords: nothing to tap.
  function tokensUseful(tokens) {
    return Array.isArray(tokens) && tokens.some((t) => t.term);
  }

  // The playback clock never steps back while playing: a new frame report can land a few
  // milliseconds behind the time already extrapolated, and near a cue boundary that would flip the
  // active cue back and forth. A real jump back (a seek) is larger than this and goes through.
  function steadyTime(prev, raw, playing) {
    return playing && Number.isFinite(prev) && raw < prev && prev - raw < 0.3 ? prev : raw;
  }

  // What the subtitle list is centred on, by identity: a cue id (or the status slot after one), not
  // an index, which an insertion above shifts without anything having been played.
  function focusIdentity(cues, pos) {
    if (pos === null || pos === undefined) return null;
    const cue = cues[Math.floor(pos)];
    const base = cue ? String(cue.id) : "start";
    return Number.isInteger(pos) ? base : `${base}+`;
  }

  // Glide only when playback (or the viewer) moved the focus to another cue; content that changed
  // above or below re-anchors at once, so the line being said stays where it is on screen.
  function focusMotion(prevIdentity, nextIdentity) {
    return prevIdentity !== nextIdentity ? "glide" : "instant";
  }

  // How many units have started by time t (the sweep position), by binary search.
  function spokenCount(times, t) {
    let lo = 0;
    let hi = times.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid][0] <= t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  // ---- resuming

  // Where to start a video: a ?t= in the link wins; else the saved position minus 2 s, except near
  // the start (under 10 s: start over) or once finished() (it was watched: start over).
  function resumeTime({ saved, duration, urlT }) {
    if (urlT !== null && urlT !== undefined && Number.isFinite(urlT)) return Math.max(0, urlT);
    if (!saved || !Number.isFinite(saved.t)) return 0;
    const d = Number(saved.duration) || Number(duration) || 0;
    if (saved.t < 10) return 0;
    if (finished(saved.t, d)) return 0;
    return Math.max(0, saved.t - 2);
  }

  // What a click does while the dictionary sheet is open, or just after it closed. The gesture
  // that closes the sheet does nothing else, and neither does a late synthetic click (iOS sends one
  // after the touch) landing on whatever slid under the finger as the sheet went away.
  //   inSheet: the click is inside the sheet (or a toast); wordCue: the cue id of a word tapped in
  //   the subtitle list, or null; sheetCue: the cue the sheet was opened from.
  function dismissAction({ sheetOpen, inSheet, wordCue, sheetCue, sinceCloseMs }) {
    if (inSheet) return "pass";
    if (sheetOpen) {
      // Another word of the same sentence: look that one up instead (the sentence stays put).
      if (wordCue !== null && wordCue !== undefined && String(wordCue) === String(sheetCue)) return "pass";
      return "dismiss";
    }
    return sinceCloseMs < GHOST_CLICK_MS ? "swallow" : "pass";
  }
  const GHOST_CLICK_MS = 400;

  // Whether the position saver may write t. Until a resume has landed (or the viewer seeks), a time
  // far from the resume target is the player still at 0 before its seek took: saving it would
  // overwrite the saved spot with 0 for good.
  function saveAllowed(t, resumeTarget, landed) {
    if (landed || resumeTarget === null || resumeTarget === undefined) return true;
    return Math.abs(t - resumeTarget) <= 2;
  }

  // Where the native player seeks on its first "playing", or null for nowhere. iOS's native HLS
  // never completes a seek made before the first play: `seeking` stays true and currentTime stays
  // pinned at the target while playback runs from elsewhere. So every seek wanted before then waits
  // for that event: the viewer's latest (pending) wins over the resume target, which counts only
  // until it has landed. The playlist's EXT-X-START has usually put the video there already, so a
  // time within RESUME_SLACK_S of the wanted one is left alone.
  const RESUME_SLACK_S = 1.5;
  function firstPlaySeek({ current, pending, target, landed }) {
    const wanted = pending !== null && pending !== undefined ? pending
      : !landed && target !== null && target !== undefined ? target : null;
    if (wanted === null || Math.abs(current - wanted) <= RESUME_SLACK_S) return null;
    return wanted;
  }

  // Whether a position save would post what the last one posted a moment ago: the same video, the
  // same time within SAVE_SAME_S, sent less than SAVE_REPEAT_MS before. A video's end fires pause,
  // then ended, then the way home saves: one post is enough. last is null after a failed post, so a
  // save the server never received is never skipped; after the window a save goes out again, in
  // case another device moved the position meanwhile.
  const SAVE_SAME_S = 0.05;
  const SAVE_REPEAT_MS = 5000;
  function repeatsSave(last, videoId, t, now) {
    return !!last && last.videoId === videoId && Math.abs(last.t - t) <= SAVE_SAME_S && now - last.at < SAVE_REPEAT_MS;
  }

  // The one rule for a watched video, read by the home bar (full), Mark as watched / unwatched and
  // the resume (start over): the last tenth of it, never less than END_SLOP_S and never more than
  // FINISHED_S. So a long video is finished in its last 30 s (the credits, an outro), a 2-minute
  // one in its last 12 s, and a short clip in its last 2 s, not a third of it.
  const FINISHED_S = 30;
  const FINISHED_SHARE = 0.1;
  // A playhead this close to the end is at the end: players stop a frame or a segment short.
  const END_SLOP_S = 2;

  function finished(t, duration) {
    if (!(Number.isFinite(duration) && duration > 0 && Number.isFinite(t))) return false;
    return t >= duration - Math.min(FINISHED_S, Math.max(END_SLOP_S, duration * FINISHED_SHARE));
  }

  // Whether an "ended" is the real end, for the way home: an HLS stream may stop a segment early,
  // and that must not count. Narrower than finished(), on purpose: it asks where the player
  // stopped, not whether the viewer has seen the video.
  function atEnd(t, duration) {
    return Number.isFinite(duration) && duration > 0 && Number.isFinite(t) && t >= duration - END_SLOP_S;
  }

  // How far in, 0..1, for the bar under a thumbnail: the position over the duration, a finished
  // video 1. No duration, no bar.
  function watchedFraction(entry) {
    const d = Number(entry && entry.duration) || 0;
    const t = Number(entry && entry.t) || 0;
    if (!(d > 0) || !(t > 0)) return 0;
    if (finished(t, d)) return 1;
    return Math.min(1, t / d);
  }

  // Mark as watched / unwatched: the position it sets. A finished video goes back to 0, any other
  // to its end, so the bar reads full; null without a duration (no end to set).
  function watchedToggle(entry) {
    const d = Number(entry && entry.duration) || 0;
    if (!(d > 0)) return null;
    return finished(Number(entry.t) || 0, d) ? 0 : d;
  }

  // ---- swiping a home row (iOS Mail's swipe to the left)

  // SLOP: px of movement before the gesture picks an axis; a vertical one is the page's scroll.
  // OPEN: the share of the actions' width a slow release must pass to open them (from open, the
  // share it must close by to close them). DELETE: the share of the row's width past which a release
  // deletes. FLICK: px/ms; a release this fast goes the way it moved, however short. DAMP: how much of
  // the finger's travel past the actions the row follows. BAND: px; the rubber band to the right of
  // closed tends to it. VELOCITY_MS: the stretch of movement the release speed is read from.
  const SWIPE = { SLOP: 10, OPEN: 1 / 3, DELETE: 0.6, FLICK: 0.4, DAMP: 0.8, BAND: 60, VELOCITY_MS: 100 };

  // null until the finger has moved SLOP px, then "x" or "y".
  function swipeAxis(dx, dy) {
    if (Math.hypot(dx, dy) < SWIPE.SLOP) return null;
    return Math.abs(dx) > Math.abs(dy) ? "x" : "y";
  }

  // Where a released row goes: "open", "closed" or "delete". shift is how far left it sits (px),
  // actions the buttons' width, row the row's width, wasOpen how the swipe began, velocity the
  // release speed (swipeVelocity(): px/ms, leftward positive). A flick back to the right closes
  // first of all, as in iOS Mail: a finger pulling back is taking the delete back.
  function swipeSettle({ shift, actions, row, wasOpen, velocity = 0 }) {
    if (velocity <= -SWIPE.FLICK) return "closed";
    if (shift >= row * SWIPE.DELETE) return "delete";
    if (velocity >= SWIPE.FLICK) return "open";
    const needed = wasOpen ? actions * (1 - SWIPE.OPEN) : actions * SWIPE.OPEN;
    return shift >= needed ? "open" : "closed";
  }

  // How far left a row sits while dragged (px; negative is right of closed). It follows the finger
  // 1:1 up to the actions' width, DAMP of it past them up to the row's width, and to the right of
  // closed it stretches like iOS's rubber band, (1 - 1 / (x * 0.55 / BAND + 1)) * BAND.
  function swipeShift({ start, dx, row, actions }) {
    const raw = start - dx;
    if (raw < 0) return -(1 - 1 / ((-raw * 0.55) / SWIPE.BAND + 1)) * SWIPE.BAND;
    if (raw <= actions) return raw;
    return Math.min(row, actions + (raw - actions) * SWIPE.DAMP);
  }

  // The release speed, px/ms leftward, from pointer samples [{t, x}] (oldest first, the release
  // last): across those of the last VELOCITY_MS, so a finger that stopped before lifting reads 0.
  function swipeVelocity(samples) {
    if (samples.length < 2) return 0;
    const last = samples[samples.length - 1];
    const first = samples.find((p) => last.t - p.t <= SWIPE.VELOCITY_MS);
    const dt = last.t - first.t;
    return dt > 0 ? (first.x - last.x) / dt : 0;
  }

  // Where each of n action buttons sits behind a row shifted by shift px, as iOS reveals them: each
  // takes an equal share of the uncovered width, so all grow with the drag. Each answer is the
  // button's left edge, in px left of the row's right edge. full: the last (Delete) takes it all.
  function actionOffsets(shift, n, full) {
    const s = Math.max(0, shift);
    return Array.from({ length: n }, (_, i) => (full && i === n - 1 ? s : (s * (n - i)) / n));
  }

  // ---- retries

  // Backoff schedules, in milliseconds before retry number `attempt` (0 = the first retry).
  const SCHEDULES = {
    sync: { base: 1000, factor: 2, max: 15000, tries: Infinity }, // 1, 2, 4, 8, 15, 15 … s
    lookup: { base: 300, factor: 3, max: 2700, tries: 3 }, // 300 ms, 900 ms, 2.7 s
    mine: { base: 3000, factor: 2.5, max: 20000, tries: 3 }, // 3, 7.5, 18.75 s: 4 attempts in ~30 s
  };

  // Exponential backoff with ±20% jitter, so many clients (or tabs) never retry in step.
  function backoffDelay(attempt, schedule, rand = Math.random) {
    const raw = Math.min(schedule.max, schedule.base * schedule.factor ** attempt);
    return Math.round(raw * (0.8 + 0.4 * rand()));
  }

  // Worth trying again: the network failed (no status), or a server or the service behind it did
  // (5xx, including the portal's 502 for a Yomitan or AnkiConnect timeout and 503 for audio not
  // ready yet). A 4xx is a verdict on the request itself, and a duplicate is a decision.
  function isTransient(status, data) {
    if (data && data.duplicate) return false;
    if (!status) return true;
    return status >= 500;
  }

  // Run `attempt()` (resolving to {status, data}) until it succeeds, fails for good, or runs out of
  // tries; waits by `schedule` in between. `onRetry(n, delay, result)` hears about each wait.
  async function withRetry(attempt, schedule, { sleep = (ms) => new Promise((r) => setTimeout(r, ms)), rand, onRetry } = {}) {
    for (let n = 0; ; n++) {
      let result;
      try {
        result = await attempt();
      } catch (err) {
        result = { status: 0, data: null, error: err };
      }
      const ok = result.status >= 200 && result.status < 300 && (!result.data || result.data.ok !== false);
      if (ok || !isTransient(result.status, result.data) || n >= schedule.tries) return result;
      const delay = backoffDelay(n, schedule, rand);
      if (onRetry) onRetry(n + 1, delay, result);
      await sleep(delay);
    }
  }

  // Whole seconds until the server's announced retry, counted from when the answer came; null when
  // it named no time.
  function retryLeft(view) {
    const d = view.data;
    if (!d || !Number.isFinite(d.retry_in) || !Number.isFinite(view.receivedAt) || !Number.isFinite(view.now)) return null;
    return Math.max(0, Math.ceil(d.retry_in - (view.now - view.receivedAt) / 1000));
  }

  // "2214㋕" -> "#2214"; null when there is no number.
  function freqLabel(value) {
    const m = /\d[\d,]*/.exec(String(value ?? ""));
    return m ? `#${m[0]}` : null;
  }

  // Each token with its character offsets in the line it came from.
  function tokenOffsets(tokens) {
    let at = 0;
    return tokens.map((tok) => {
      const start = at;
      at += tok.text.length;
      return { ...tok, start, end: at };
    });
  }

  const SMALL_KANA = "ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ";

  // Kana -> morae: a small kana belongs to the mora before it.
  function morae(reading) {
    const out = [];
    for (const ch of String(reading || "")) {
      if (out.length && SMALL_KANA.includes(ch)) out[out.length - 1] += ch;
      else out.push(ch);
    }
    return out;
  }

  // Tokyo pitch accent: [{mora, high, drop}] for a downstep position (0 = heiban).
  function pitchPattern(reading, position) {
    const ms = morae(reading);
    return ms.map((mora, i) => {
      const n = i + 1;
      let high;
      if (position === 0) high = n > 1;
      else if (position === 1) high = n === 1;
      else high = n > 1 && n <= position;
      return { mora, high: ms.length === 1 && position === 0 ? false : high, drop: position > 0 && n === position };
    });
  }

  function toHiragana(text) {
    return String(text).replace(/[ァ-ヶ]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0x60));
  }

  // Ruby parts for a headword: the kana it shares with its reading at either end carry no ruby.
  // 食べる/たべる -> [["食","た"],["べる",""]].
  function splitFurigana(term, reading) {
    const t = Array.from(String(term || ""));
    const r = Array.from(String(reading || ""));
    if (!r.length || toHiragana(t.join("")) === toHiragana(r.join(""))) return [[t.join(""), ""]];
    let head = 0;
    while (head < t.length - 1 && head < r.length - 1 && toHiragana(t[head]) === toHiragana(r[head])) head++;
    let tail = 0;
    while (
      tail < t.length - head - 1 && tail < r.length - head - 1 &&
      toHiragana(t[t.length - 1 - tail]) === toHiragana(r[r.length - 1 - tail])
    ) tail++;
    const parts = [];
    if (head) parts.push([t.slice(0, head).join(""), ""]);
    parts.push([t.slice(head, t.length - tail).join(""), r.slice(head, r.length - tail).join("")]);
    if (tail) parts.push([t.slice(t.length - tail).join(""), ""]);
    return parts;
  }

  // Settings > Anki's line: the server's Anki sync (its /health anki_sync, {state, last, error}).
  // "" for a server without one. `clock(ms)` writes the time.
  function ankiSyncText(sync, clock = (at) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) {
    const S = STRINGS;
    if (!sync || typeof sync.state !== "string") return "";
    if (sync.state === "off") return S.ankiSyncOff;
    if (sync.state === "syncing") return S.ankiSyncRunning;
    if (sync.error) return S.ankiSyncFailed(sync.error);
    if (typeof sync.last === "number") return S.ankiSyncLast(clock(sync.last));
    return S.ankiSyncWaiting;
  }

  // Settings' line on the portal's --watch-history (/api/info watch_history); "" from a portal without it.
  function watchHistoryText(mode) {
    const S = STRINGS;
    return { on: S.watchHistoryOn, off: S.watchHistoryOff, "no-cookies": S.watchHistoryNoCookies }[mode] || "";
  }

  // ------------------------------------------------------------ the YouTube watch history
  //
  // The stretches of media time our own player played, [start, end] each, which the page reports
  // to /api/watched when the portal runs with --watch-history on. A seek, pause, rate change or
  // the end closes a stretch (watchClose()); so does a jump the events missed.

  const WATCH_JUMP_S = 3; // further than playback moves between two timeupdates: a seek

  function newWatchLog() {
    return { open: null, closed: [] };
  }

  // While playing: the open stretch runs on to t, or a new one starts at t.
  function watchAt(log, t) {
    if (!Number.isFinite(t) || t < 0) return;
    const open = log.open;
    if (open && t >= open[1] - 0.25 && t - open[1] <= WATCH_JUMP_S) {
      open[1] = Math.max(open[1], t);
      return;
    }
    watchClose(log);
    log.open = [t, t];
  }

  function watchClose(log) {
    if (log.open && log.open[1] > log.open[0]) log.closed.push(log.open);
    log.open = null;
  }

  // One report's stretches, at most `max` (the rest wait for the next), rounded to the
  // millisecond. The open stretch closes at t; while playing, the next one starts there.
  function watchDrain(log, t, playing, max = 20) {
    if (log.open) {
      watchAt(log, t);
      const end = log.open[1];
      watchClose(log);
      if (playing) log.open = [end, end];
    }
    const ms = (x) => Math.round(x * 1000) / 1000;
    return log.closed.splice(0, max).map(([a, b]) => [ms(a), ms(b)]);
  }

  // /api/watched's answer: milliseconds of playing until the next scheduled report (YouTube's own
  // flush schedule, `next` in seconds), or null when it names none.
  function watchNextMs(answer) {
    const next = answer && answer.next;
    return typeof next === "number" && Number.isFinite(next) && next > 0 && next <= 3600 ? next * 1000 : null;
  }

  // The browser and system a User-Agent names, as YouTube's pings name them (cbr, cbrver, cos,
  // cosver): {browser, browser_version, os, os_version}, only the parts it names plainly. The
  // version keeps the User-Agent's own spelling (iOS "17_5").
  function clientInfo(ua) {
    const text = String(ua || "");
    const out = {};
    const os = [
      [/\b(iPhone|iPad|iPod)\b.*?\bOS (\d+(?:_\d+)*)/, (m) => [m[1], m[2]]],
      [/\bAndroid (\d+(?:\.\d+)*)/, (m) => ["Android", m[1]]],
      [/\bWindows NT (\d+\.\d+)/, (m) => ["Windows", m[1]]],
      [/\bMac OS X (\d+(?:[_.]\d+)*)/, (m) => ["Macintosh", m[1]]],
      [/\bCrOS\b/, () => ["CrOS", ""]],
      [/\b(X11|Linux)\b/, () => ["X11", ""]],
    ];
    for (const [re, take] of os) {
      const m = re.exec(text);
      if (!m) continue;
      [out.os, out.os_version] = take(m);
      break;
    }
    const browsers = [
      [/\bEdg(?:e|A|iOS)?\/(\d+(?:\.\d+)*)/, "Edge"],
      [/\b(?:Firefox|FxiOS)\/(\d+(?:\.\d+)*)/, "Firefox"],
      [/\b(?:CriOS|Chrome)\/(\d+(?:\.\d+)*)/, "Chrome"],
      [/\bVersion\/(\d+(?:\.\d+)*).*\bSafari\//, "Safari"],
    ];
    for (const [re, name] of browsers) {
      const m = re.exec(text);
      if (!m) continue;
      out.browser = name;
      out.browser_version = m[1];
      break;
    }
    for (const key of Object.keys(out)) if (!/^[A-Za-z0-9._ -]{1,32}$/.test(out[key])) delete out[key];
    return out;
  }

  // ------------------------------------------------------------ word colours
  //
  // The server reads the Anki deck and sends its rows with /sync (`deck`), [id, word, reading,
  // status, pitch] each: every row the first time, the changed ones after that. The page keeps
  // them as the extension's background does and turns them into the entries words.js's
  // buildIndex() takes; words.js marks the line, as on YouTube.

  // The rows held after the server's `deck` answer: {token, status, rows (Map id -> row)}.
  function applyDeck(held, answer) {
    if (!answer || typeof answer !== "object" || typeof answer.token !== "string") return held;
    const status = typeof answer.status === "string" ? answer.status : "error";
    const rows = status !== "ok" || answer.full ? new Map() : new Map(held.rows);
    if (status === "ok") {
      for (const row of Array.isArray(answer.notes) ? answer.notes : []) {
        if (Array.isArray(row) && row.length === 5 && Number.isSafeInteger(row[0]) && typeof row[1] === "string" && row[1]) rows.set(row[0], row);
      }
      for (const id of Array.isArray(answer.removed) ? answer.removed : []) rows.delete(id);
    }
    return { token: answer.token, status, rows };
  }

  // buildIndex()'s entries, by note id: the word, then the kana reading of a word usually written
  // in kana (background.js's deckEntries()).
  function deckEntries(rows) {
    const entries = [];
    for (const [, word, reading, status, pitch] of [...rows].sort((a, b) => a[0] - b[0])) {
      entries.push([word, status, pitch]);
      if (reading) entries.push([reading, status, pitch]);
    }
    return entries;
  }

  // markWords()' runs as one status per UTF-16 index of the text, null where a run has none.
  function statusAt(runs) {
    const out = [];
    for (const run of runs || []) for (let i = 0; i < run.text.length; i++) out.push(run.status || null);
    return out;
  }

  // A unit's [text, reading] pieces cut where the status changes, from offset `start` of the
  // cue: [{text, reading, status}]. A piece with a reading (ruby) stays whole, in the status of
  // its first character.
  function markedSegs(unit, marks) {
    const out = [];
    let at = unit.start;
    for (const [text, reading] of unit.segs) {
      if (reading) {
        out.push({ text, reading, status: marks[at] || null });
      } else {
        let from = 0;
        for (let i = 1; i <= text.length; i++) {
          if (i === text.length || (marks[at + i] || null) !== (marks[at + from] || null)) {
            out.push({ text: text.slice(from, i), reading: "", status: marks[at + from] || null });
            from = i;
          }
        }
      }
      at += text.length;
    }
    return out;
  }

  // The box a tap at (x, y) falls in, or the nearest one: its index, or -1 for none. Boxes are
  // {left, top, right, bottom}, one per character of a tapped word.
  function nearestBox(boxes, x, y) {
    let best = -1;
    let bestDistance = Infinity;
    boxes.forEach((box, i) => {
      const dx = Math.max(box.left - x, 0, x - box.right);
      const dy = Math.max(box.top - y, 0, y - box.bottom);
      const distance = dx * dx + dy * dy;
      if (distance < bestDistance) {
        best = i;
        bestDistance = distance;
      }
    });
    return best;
  }

  // ---- the dictionary sheet's choices

  // What a scan looks up: up to `n` UTF-16 units of the line from `start`, never ending inside a
  // character beyond the BMP (𠮟 is two units), whose lone half Yomitan cannot match.
  function scanText(text, start, n) {
    let end = Math.min(text.length, start + n);
    if (end > start && end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1])) end--;
    return text.slice(start, end);
  }

  // The line's span an entry stands for. A lookup starts at the tapped span's start, and each
  // entry says how many characters of it matched. A scan (Japanese, from the tapped character on)
  // takes every entry's own length; a token looked up whole keeps the token for its longest
  // matches and narrows only for a shorter one.
  //   span: the tapped [start, end]; length: the line's length; longest: entries[0].matched.
  // Offsets and `matched` are UTF-16 code units, JavaScript's own (the server counts them so too).
  function entrySpan({ span, matched, longest, scan, length }) {
    if (!(matched > 0) || (!scan && matched >= longest)) return [span[0], span[1]];
    return [span[0], Math.min(length, span[0] + matched)];
  }

  // The sheet's tabs: [{term, reading, shorter}]. The reading shows only where the term alone
  // would be ambiguous (the same term twice); `shorter` marks the first of the entries that
  // matched less of the line than the default one, where the row draws a divider.
  function candidateTabs(entries) {
    const count = new Map();
    for (const e of entries) count.set(e.term, (count.get(e.term) || 0) + 1);
    const first = entries.findIndex((e) => e.matched < entries[0].matched);
    return entries.map((e, i) => ({
      term: e.term,
      reading: count.get(e.term) > 1 && e.reading && e.reading !== e.term ? e.reading : null,
      shorter: i === first,
    }));
  }

  // The landscape mode on a phone held upright turns the page a quarter clockwise (app.css
  // html.turned): the view's top lies along the screen's right edge, and `turn.height` is the view's
  // own height, which is the screen's width. Pointer events and getBoundingClientRect() speak in
  // screen coordinates; these put them in the view's frame. `turn` is null when nothing is turned.
  function viewPoint(x, y, turn) {
    return turn ? { x: y, y: turn.height - x } : { x, y };
  }

  function viewDelta(dx, dy, turn) {
    return turn ? { dx: dy, dy: -dx } : { dx, dy };
  }

  function viewRect(rect, turn) {
    if (!turn) return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height };
    return { left: rect.top, top: turn.height - rect.right, right: rect.bottom, bottom: turn.height - rect.left, width: rect.height, height: rect.width };
  }

  // A viewport taller than wide shows the landscape mode turned: the viewer turns the phone, even
  // with its rotation lock on, which is the only way an iPhone's page goes sideways.
  function shouldTurn(width, height) {
    return height > width;
  }

  const DOUBLE_TAP_MS = 300;

  // A tap on the video, as in YouTube's app. `zone` is where it fell (tapZone()), `last` what the
  // previous tap left (null at first). On a side, a second tap within `windowMs` steps a line that
  // way, and each further tap in time steps one more (`steps` counts them from the run's first
  // step). What a lone tap does depends on the layout:
  // - Portrait: the middle third plays or pauses at once. A lone side tap plays at once while
  //   paused, since iOS lets play() run only inside the tap itself; a pause needs no gesture, so
  //   while playing it pauses only once the window passes with no second tap ("pause-later"), and
  //   a double tap never stops the video first.
  // - Landscape: the middle fifth plays or pauses at once; a lone tap anywhere else shows or hides
  //   the controls once the window has passed ("controls-later") and never pauses.
  function shieldTap(last, tap, { landscape = false, windowMs = DOUBLE_TAP_MS } = {}) {
    const side = tap.zone === "left" || tap.zone === "right";
    if (side && last && last.zone === tap.zone && tap.at - last.at < windowMs) {
      const steps = last.steps + 1;
      return { action: "step", delta: tap.zone === "left" ? -steps : steps, steps, next: { at: tap.at, zone: tap.zone, steps } };
    }
    if (!side) return { action: "toggle", next: null };
    const action = landscape ? "controls-later" : tap.playing ? "pause-later" : "play";
    return { action, next: { at: tap.at, zone: tap.zone, steps: 0 } };
  }

  // The middle zone's share of the video's width: a third in portrait, a fifth in landscape. Each
  // side zone is what is left of it, halved; the double tap's badge covers exactly that.
  const TAP_MIDDLE = { portrait: 1 / 3, landscape: 0.2 };
  function sideShare(middle) {
    return (1 - middle) / 2;
  }

  // Where a tap fell across a box, both in the view's frame: "middle" within the central
  // `middle` share of its width, else its side.
  function tapZone(x, box, middle = TAP_MIDDLE.portrait) {
    const f = (x - box.left) / box.width;
    return Math.abs(f - 0.5) <= middle / 2 ? "middle" : f < 0.5 ? "left" : "right";
  }

  // Where a step lands (back, forward, a swipe, a double tap's run): `delta` lines from `base`,
  // the line being said when the run began (activeCueIndex(), so mid-line back means the line
  // before, as in the extension; -1 before the first line), clamped to the list. `moved` is how
  // many lines that really is; a badge counts it. Back on the first line has no line before it,
  // so it replays that line from its start (`replay`, nothing moved, no badge). Forward on the
  // last line, or back before the first, finds nothing: idx -1, and nothing seeks.
  function stepTarget(count, base, delta) {
    if (!count || !delta) return { idx: -1, moved: 0 };
    const idx = Math.min(count - 1, Math.max(0, base + delta));
    const moved = (idx - base) * Math.sign(delta);
    if (moved > 0) return { idx, moved };
    if (delta < 0 && base === 0) return { idx: 0, moved: 0, replay: true };
    return { idx: -1, moved: 0 };
  }

  // The playhead from a frame report, when it is trustworthy. requestVideoFrameCallback's
  // mediaTime is exact to the frame on screen, but right after a seek a browser can still report
  // frames from before it (Safari does), so it only refines the element's own currentTime, the
  // authority: never while seeking, never more than `slack` seconds away from it.
  function framePlayhead(frame, current, { seeking = false, slack = 0.5 } = {}) {
    if (seeking || !Number.isFinite(frame) || Math.abs(frame - current) > slack) return current;
    return frame;
  }

  // Settings > Subtitle style: the landscape caption box's background, text size and resting
  // height, each one of a few fixed choices; `fallback` is the default and today's look. Anything
  // else read back from storage becomes the default.
  const CAPTION_CHOICES = {
    captionBg: { values: [0, 0.3, 0.45, 0.6, 0.8], fallback: 0.6 },
    captionSize: { values: ["small", "medium", "large"], fallback: "medium" },
    captionPosition: { values: ["lower", "default", "higher"], fallback: "default" },
  };
  const CAPTION_SCALE = { small: 0.85, medium: 1, large: 1.2 }; // times the size clamp in app.css
  const CAPTION_LIFT = { lower: -8, default: 0, higher: 24 }; // px off the resting height, inside the safe area

  function captionStyle(saved) {
    const out = {};
    for (const [key, { values, fallback }] of Object.entries(CAPTION_CHOICES)) {
      const v = saved ? saved[key] : undefined;
      out[key] = values.includes(v) ? v : fallback;
    }
    return out;
  }

  // The custom properties app.css reads, as plain numbers clamped to sane ranges whatever comes in:
  // nothing from storage reaches the stylesheet unchecked. Below a 45% box the text gets a shadow
  // of its own (--cap-shadow), as the iPhone's own outline styles do, or white text on a bright
  // picture is lost.
  function captionVars(saved) {
    const s = captionStyle(saved);
    const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, Number.isFinite(x) ? x : lo));
    const bg = clamp(s.captionBg, 0, 1);
    return {
      "--cap-bg": String(bg),
      "--cap-scale": String(clamp(CAPTION_SCALE[s.captionSize], 0.5, 2)),
      "--cap-lift": `${clamp(CAPTION_LIFT[s.captionPosition], -16, 48)}px`,
      "--cap-shadow": bg < 0.45 ? "0.9" : "0",
    };
  }

  function hasKanji(text) {
    return /[㐀-鿿豈-﫿々]/.test(String(text || ""));
  }

  const api = {
    parseVideoInput, parseStart, activeCueIndex, formatTime, coveredUntil, statusText, tokenOffsets,
    morae, pitchPattern, splitFurigana, hasKanji, VIDEO_ID_RE, watchStatus, freqLabel,
    STRINGS, resumeTime, saveAllowed, firstPlaySeek, RESUME_SLACK_S, repeatsSave, SAVE_REPEAT_MS, dismissAction, GHOST_CLICK_MS, watchedFraction, finished, FINISHED_S, atEnd, END_SLOP_S,
    watchedToggle, SWIPE, swipeAxis, swipeSettle, swipeShift, swipeVelocity, actionOffsets, formatClock, spokenTime,
    SCHEDULES, backoffDelay, isTransient, withRetry, retryLeft,
    evenUnitTimes, isJapanese, segmentWords, tokensUseful, steadyTime, focusIdentity, focusMotion,
    moraCount, unitWeight, estimateUnitTimes, unitTimesFromWords, cueUnits, unitTimes, spokenCount,
    ankiSyncText, applyDeck, deckEntries, statusAt, markedSegs, nearestBox, entrySpan, scanText, candidateTabs, lookupResume, LOOKUP_LEAD_S,
    watchHistoryText, newWatchLog, watchAt, watchClose, watchDrain, watchNextMs, clientInfo, viewPoint, viewDelta, viewRect, shouldTurn,
    shieldTap, tapZone, TAP_MIDDLE, sideShare, stepTarget, framePlayhead, DOUBLE_TAP_MS,
    CAPTION_CHOICES, captionStyle, captionVars,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortalLib = api;
})(typeof window !== "undefined" ? window : globalThis);
