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
    removeVideo: (name) => `Remove ${name}`,
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
    ankiSync: "Sync Anki after mining",
    ankiSyncRunning: "Anki sync: syncing…",
    ankiSyncLast: (when) => `Anki sync: last ${when}`,
    ankiSyncFailed: (why) => `Anki sync failed: ${why}`,
    ankiSyncFixed: (file) => `${file} sets this switch.`,
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
    position: "Position",
    positionOf: (at, total) => `${at} of ${total}`,
    live: "LIVE",
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
  // the start (under 10 s: start over) or near the end (within 30 s: it was finished, start over).
  function resumeTime({ saved, duration, urlT }) {
    if (urlT !== null && urlT !== undefined && Number.isFinite(urlT)) return Math.max(0, urlT);
    if (!saved || !Number.isFinite(saved.t)) return 0;
    const d = Number(saved.duration) || Number(duration) || 0;
    if (saved.t < 10) return 0;
    if (d && saved.t >= d - 30) return 0;
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

  // How far in, 0..1, for the progress bar under a thumbnail; a finished video reads as 1.
  function watchedFraction(entry) {
    const d = Number(entry && entry.duration) || 0;
    const t = Number(entry && entry.t) || 0;
    if (!d) return 0;
    if (t >= d - 30) return 1;
    return Math.max(0, Math.min(1, t / d));
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

  // The line under Settings > Anki's sync switch: when the last sync went through, or why it
  // did not; "" before the first one and with the switch off. `clock(ms)` writes the time.
  function ankiSyncText(info, clock = (at) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) {
    const S = STRINGS;
    if (!info || !info.auto_sync) return "";
    if (info.running) return S.ankiSyncRunning;
    const last = info.last;
    if (!last) return "";
    return last.ok ? S.ankiSyncLast(clock(last.at)) : S.ankiSyncFailed(last.error);
  }

  function hasKanji(text) {
    return /[㐀-鿿豈-﫿々]/.test(String(text || ""));
  }

  const api = {
    parseVideoInput, parseStart, activeCueIndex, formatTime, coveredUntil, statusText, tokenOffsets,
    morae, pitchPattern, splitFurigana, hasKanji, VIDEO_ID_RE, watchStatus, freqLabel,
    STRINGS, resumeTime, saveAllowed, dismissAction, GHOST_CLICK_MS, watchedFraction, formatClock, spokenTime,
    SCHEDULES, backoffDelay, isTransient, withRetry, retryLeft,
    evenUnitTimes, isJapanese, segmentWords, tokensUseful, steadyTime, focusIdentity, focusMotion,
    moraCount, unitWeight, estimateUnitTimes, unitTimesFromWords, cueUnits, unitTimes, spokenCount,
    ankiSyncText,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortalLib = api;
})(typeof window !== "undefined" ? window : globalThis);
