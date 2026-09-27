"use strict";

/*
 * Shisu-ko content script for youtube.com.
 *
 * Responsibilities:
 *  - find the player and <video>, follow YouTube's single-page navigation
 *  - every second, tell the local Whisper server which video and playhead we are at
 *    and collect the timestamped cues it has produced so far
 *  - render the active cue as ordinary DOM text over the player so dictionary
 *    extensions such as Yomitan can scan it; pause the video while the text is hovered
 *  - optional transcript panel listing every cue (also plain text)
 *  - sentence mining: grab the current video frame and ask the background script to
 *    fetch the matching audio clip and file both into Anki or the Downloads folder, either on
 *    demand or by itself as soon as Yomitan has added a card
 */

(() => {
  if (window.__shisukoLoaded) return;
  window.__shisukoLoaded = true;
  // A reloaded or updated extension leaves this instance orphaned with a dead runtime; it must
  // tear itself down so the fresh instance can take over the player (see runtimeAlive()).
  const timers = [];

  const DEFAULT_SETTINGS = SHISUKO_DEFAULT_SETTINGS; // from settings.js

  const SYNC_INTERVAL_MS = 1000;
  const RENDER_INTERVAL_MS = 100;
  const DISCOVER_INTERVAL_MS = 750;
  // Heartbeat while a paused video needs nothing: slow enough to be free, far below the server's
  // 30 s client timeout (it stops transcribing for a session nobody has synced since then).
  const SYNC_IDLE_INTERVAL_MS = 5000;
  // The server's own --lookahead default: it transcribes no further ahead than this, so once the
  // covered range reaches it there is nothing left to ask for until the playhead moves.
  const SYNC_LOOKAHEAD_S = 900;
  // A video paused this long with nobody reading the subtitle is not being mined from.
  const PAUSE_POLL_IDLE_MS = 120000;
  // Past that, a hover pause is still polled, at this cadence: the dictionary popup is an iframe
  // whose pointer the page never sees, so a viewer reading a long entry looks parked, and the
  // background drops the card they then make after a gap over its ANKI_BASELINE_MAX_AGE_MS (10 s).
  const PAUSE_POLL_SLOW_MS = 5000;
  const RESUME_DELAY_MS = 350;
  const TOAST_MS = 3500;
  const TOAST_MAX_CHARS = 240;
  // The status line is one line over the video; a server error can carry a traceback or a URL.
  const STATUS_ERROR_MAX_CHARS = 160;
  const STATUS_NAME_MAX_CHARS = 100;
  const MINE_RECENT_WINDOW_S = 6;
  const HOVER_CAPTURE_DELAY_MS = 400;
  // Long enough that skimming through a video prepares nothing, short enough that a sentence the
  // viewer actually reads is ready before the lookup. Reading a frame back stalls the main thread,
  // so it must not happen on the tick that puts the line on screen.
  const PREMINE_CAPTURE_DELAY_MS = 400;
  const ANKI_POLL_LOG_MS = 60000;
  // The deck index behind the word colours is asked for this often (the background keeps it as
  // long, so most asks come back "unchanged"); a failing ask is logged this often.
  const WORD_INDEX_REFRESH_MS = 30000;
  const WORD_INDEX_LOG_MS = 60000;
  // A card just mined must show its colour now, not after the interval. The background expires
  // its index once Anki has told it the card's deck, two answers after the mine, so the next ask
  // waits this long for that: an ask in between would be answered with the old index.
  const WORD_INDEX_MINE_DELAY_MS = 1500;
  // The settings the deck index depends on: a change to any of them starts it over.
  const WORD_SETTINGS = ["cardStatus", "pitchAccent", "cardStatusDeck", "ankiPitchField", "ankiWordField"];
  // A refreshed index that differs from the last in more words than this is walked line by line
  // like a new one: matching a line again costs about as much as looking for that many words in it.
  const WORD_INDEX_PROBE_MAX = 64;
  // One kanji, for the word under the pointer (segmentAt): ICU cuts a verb after its kanji.
  const KANJI_RE = /^\p{Script=Han}$/u;
  // The honorific prefixes ICU cuts off the word they front (お|風呂, ご|家族), as words.js has
  // them: pointed at, the word is meant, not the prefix (knownTarget).
  const HONORIFICS = new Set(["お", "ご"]);
  // A card is most likely to appear while a subtitle is hovered.
  const HOVER_POLL_INTERVAL_MS = 300;
  // A blank shorter than this reads as a flicker rather than a pause, so the text is held instead.
  const MIN_BLANK_S = 0.3;
  const CUE_LEAD_IN_S = 0.15;
  // A playhead less than this far past a covered end is the server catching up, not a seek into
  // an untranscribed stretch: the server's own --window, the 40 s of speech it takes at a time. A
  // live stream's covered end trails the edge by the LIVE_MIN_WINDOW it waits for there, the tail
  // segment the next window takes over and the transcription itself, while the viewer sits 10-40 s
  // behind that edge; a plain video's, whenever a window takes longer to transcribe than to play.
  // The last line before that end is then the line just heard, and Left replays it.
  const COVERED_LAG_S = 40;
  // Elements whose own arrow key handling wins: text entry, YouTube's search box, the comments,
  // and the player's keyboard controls once focused (the volume slider, the settings menu and its
  // items, the radios and lists of its dialogs). The progress bar is a slider too, but its arrows
  // are the five second seek this feature replaces.
  const KEY_SKIP_SELECTOR =
    "input, textarea, select, [contenteditable], #search, ytd-comments, [role=\"slider\"]:not(.ytp-progress-bar)," +
    " [role=\"radio\"], [role=\"listbox\"], [role=\"option\"], [role=\"menu\"], [role=\"menuitem\"]," +
    " [role=\"menuitemcheckbox\"], [role=\"menuitemradio\"], tp-yt-paper-dialog";

  // ---- subtitle style ----
  const GOTHIC_STACK = '"Noto Sans JP", "Noto Sans CJK JP", "Yu Gothic UI", "Yu Gothic", "Meiryo", "Hiragino Sans", sans-serif';
  const SUB_FONTS = {
    default: GOTHIC_STACK,
    "gothic-bold": GOTHIC_STACK,
    rounded: '"M PLUS Rounded 1c", "Hiragino Maru Gothic ProN", "Hiragino Maru Gothic Pro", "Yu Gothic UI", "Yu Gothic", sans-serif',
    mincho: '"Noto Serif JP", "Noto Serif CJK JP", "Hiragino Mincho ProN", "Hiragino Mincho Pro", "Yu Mincho", "YuMincho", serif',
  };
  // A font family name as CSS may see it: letters and digits (Japanese names such as 游ゴシック
  // included), spaces, dots, hyphens and underscores. Anything else (quotes, semicolons, braces,
  // backslashes, url(...)) never reaches the stylesheet. popup.js keeps a copy for its preview.
  const FONT_FAMILY_RE = /^[\p{L}\p{N}][\p{L}\p{N} _.\-]{0,99}$/u;
  const TRANSCRIPT_SIDES = ["right", "left"];
  const SUB_POSITION_MIN = 2;
  const SUB_POSITION_MAX = 40;
  // How far the box drops once YouTube's controls fade out: 11% - 4% at the default position.
  const AUTOHIDE_DROP = 7;
  const HOVER_ALPHA_STEP = 0.18; // 0.72 -> 0.90, the hover shade the overlay always had
  const PLAIN_SHADOW = "0 0 3px rgba(0, 0, 0, 0.9)";
  // Four 1px offsets carve the letter out of the video; the blur softens the corners they leave.
  const OUTLINE_SHADOW = "1px 1px 0 #000, -1px 1px 0 #000, 1px -1px 0 #000, -1px -1px 0 #000, 0 0 2px #000";

  const state = {
    settings: Object.assign({}, DEFAULT_SETTINGS),
    videoId: null,
    video: null,
    player: null,
    root: null,
    statusEl: null,
    toastEl: null,
    toastTimer: null,
    subWrap: null,
    subBox: null,
    subText: null,
    mineBtn: null,
    transcriptEl: null,
    transcriptList: null,
    cues: [],
    cueById: new Map(), // id -> cue, so no hot path scans the cue array
    // Counts the times the cues and `since` started over; a /sync answer from before the last
    // one was asked with a cursor that means nothing now, however the video id compares.
    cueGeneration: 0,
    since: 0,
    covered: [],
    duration: 0,
    serverStatus: "idle",
    serverError: null,
    offline: false,
    serverSession: null,
    // The background refused this tab's /sync: another tab holds the server. Nothing in such an
    // answer came from the server, so nothing about the server is known while this is true.
    standby: false,
    languagePaused: false, // the server stopped transcribing: the speech is not the subtitle language
    heard: null, // the language code the server hears instead, when it names one
    modelLoading: null, // name of the model the server is loading right now; cues wait for it
    modelError: null, // why the model this client asked for cannot be used
    activeCueId: null,
    activeLineEl: null,
    transcriptDirty: true,
    transcriptRebuild: true, // every line anew (a new panel, dropped cues); else the new cues sort in
    transcriptAppendFrom: null, // index of the first unrendered cue when only appends are pending
    lineById: new Map(), // cue id -> transcript line element, maintained on every render
    // Transcript line -> its text span, and any element -> what renderText() last drew in it (see
    // drawKey): a refresh leaves a line alone when it would look the same, so a card reviewed in
    // Anki does not replace the nodes of every other line (nor what Yomitan holds on them).
    lineTexts: new WeakMap(),
    drawnKeys: new WeakMap(),
    // Cue -> the runs of its text under the index and colours they were found with (lookOf), and
    // the text's word boundaries, which no index changes: a panel rebuilt for a new session, and
    // a line a refresh finds untouched, cost no segmentation and no matching.
    cueLooks: new WeakMap(),
    transcriptHovered: false,
    hoverPaused: false,
    awaitingPlayerMove: false,
    // When the pointer was last over the subtitle or the player: a hover pause whose viewer has
    // not been seen for as long as a plain pause is given is not a lookup any more (ankiPollAllowed).
    hoverSeenAt: 0,
    resumeTimer: null,
    lastPointer: { x: 0, y: 0 },
    // Whether the pointer is over the player now, that is whether lastPointer still says where it
    // is: Alt+Shift+K reads the word there (knownTarget).
    pointerInPlayer: false,
    syncInFlight: false,
    // The playhead on the video's own clock as of the last request outside an ad, or where the
    // video will start before the first: what is sent while an ad runs on its clock (see sync).
    contentPlayhead: 0,
    mining: false,
    // What the background holds ready for this tab, newest sentence first: [{ key, cueIds, image,
    // audio }]. Replaced by every premine reply; the payloads themselves never come back here.
    premined: [],
    // Premine and reset messages go out one at a time, each once the one before was answered (see
    // premineTurn); the count is what is queued or out, so a reset knows there is something to drop.
    premineQueue: Promise.resolve(),
    premineInFlight: 0,
    premineTimer: null,
    hoverCaptureTimer: null,
    // The sentence and position of the last hover frame sent: a second entry on the same paused
    // frame reads nothing back (captureHoverFrame).
    hoverShot: null,
    transcriptHoverTimer: null, // the transcript line under the pointer, about to be ranked first
    ankiPollInFlight: false,
    lastAnkiPollAt: 0, // when the last Anki poll went out: the slow cadence of a long hover pause
    lastAnkiPollLog: 0,
    // The deck's words as SHISUKO_WORDS.buildIndex() holds them, for the word colours: `at` is the
    // background's timestamp of the entries it was built from (the `since` of the next ask), `key`
    // a fingerprint of those entries, so an index refetched unchanged does not redraw the transcript.
    // The viewer's own known words (the knownWords setting) go into the index as learned, so
    // `entries`, the deck's words as the background handed them over, are kept: a change to that
    // list builds the index again from them without asking for the deck.
    wordIndex: null,
    wordEntries: null,
    // Moves on with every index put in wordIndex (a new one or none): a cue's look is dated by
    // this number, not by the index it was found under, so a cue drawn while the transcript was
    // hidden (whose look no refresh visits) does not keep every index since in memory.
    wordIndexSerial: 0,
    wordIndexAt: 0,
    wordIndexKey: "",
    wordIndexAskedAt: 0,
    wordIndexInFlight: false,
    // Counts the times the index was started over: an ask from before a deck or field change
    // is answered for the old deck, and that answer is thrown away.
    wordIndexGeneration: 0,
    // The index the text on screen was last refreshed with (refreshWordMarks), and its serial:
    // the next refresh only looks at the lines holding a word the two indexes disagree on.
    wordIndexDrawn: null,
    wordIndexDrawnSerial: 0,
    lastWordIndexLog: 0,
    resizeObserver: null,
    videoListeners: null,
    lastSeekSync: 0,
    lastSyncAt: 0,
    pausedSince: 0, // when the video was last paused, 0 while it plays
    lastHref: null,
    rediscover: true, // re-query the player and video on the next discover() tick
    // A live stream's cues sit on the stream's media clock, which the player exposes as
    // getProgressState().current; video.currentTime restarts from an arbitrary point on every load.
    live: false,
    liveOffset: 0, // media clock minus video.currentTime, refreshed on every sync
    // What the server calls the session (`live` in every /sync answer). The player's API above
    // answers only in Firefox and is not read through an ad; this one is for the status line
    // alone and never moves the clock.
    serverLive: false,
  };

  // ------------------------------------------------------------ helpers

  function runtimeAlive() {
    try {
      return !!(browser.runtime && browser.runtime.id);
    } catch (err) {
      return false;
    }
  }

  function shutdown() {
    for (const t of timers) clearInterval(t);
    timers.length = 0;
    detach();
    clearHoverCapture();
    clearTranscriptHover();
    clearPremineTimer();
    clearResumeTimer();
    // An orphaned instance must stop swallowing arrow keys; the fresh one owns them now.
    window.removeEventListener("keydown", onKeyDown, true);
    if (state.root) state.root.remove();
    state.root = null;
    document.documentElement.classList.remove("shisuko-hide-native");
    try {
      delete window.__shisukoLoaded;
    } catch (err) {
      window.__shisukoLoaded = false;
    }
  }

  function sendMessage(msg) {
    try {
      return browser.runtime.sendMessage(msg).catch((err) => ({ ok: false, error: String(err) }));
    } catch (err) {
      return Promise.resolve({ ok: false, error: String(err) });
    }
  }

  function getVideoIdFromUrl(href) {
    try {
      const url = new URL(href);
      const v = url.searchParams.get("v");
      if (v && /^[A-Za-z0-9_-]{6,}$/.test(v)) return v;
      const m = url.pathname.match(/^\/(?:shorts|live|embed)\/([A-Za-z0-9_-]{6,})/);
      if (m) return m[1];
    } catch (err) {
      /* ignore malformed URLs */
    }
    return null;
  }

  // Whether an address is a Short's, where the player is #shorts-player, not #movie_player. Pure.
  function isShortsUrl(href) {
    try {
      return new URL(href).pathname.startsWith("/shorts/");
    } catch (err) {
      return false;
    }
  }

  // The seconds of a link's t= parameter (t=90, t=90s, t=1m30s, t=1h2m3s): where the video will
  // start once it plays. Pure; 0 for none or one that is not a time.
  function startTimeFromUrl(href) {
    try {
      const m = (new URL(href).searchParams.get("t") || "").match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$/);
      if (!m || !(m[1] || m[2] || m[3])) return 0;
      return (Number(m[1]) || 0) * 3600 + (Number(m[2]) || 0) * 60 + (Number(m[3]) || 0);
    } catch (err) {
      return 0;
    }
  }

  // The server's own words about a session, or null: anything that is not a string is not shown.
  function errorText(value) {
    return typeof value === "string" && value ? value : null;
  }

  // Text cut to `max` characters, the last one an ellipsis when something was cut. Pure.
  function truncate(text, max) {
    const s = String(text);
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }

  function formatTime(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(sec).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  }

  function isAdPlaying() {
    const p = state.player;
    return !!(p && (p.classList.contains("ad-showing") || p.classList.contains("ad-interrupting")));
  }

  function isYouTubeElement(el) {
    // True for the player itself and for anything that belongs to YouTube's player DOM (or our
    // overlay). Walks up only as far as the player, so a dictionary popup that Yomitan appends
    // inside the fullscreen player element is not mistaken for part of the player.
    const player = state.player;
    if (!el || !player) return false;
    if (el === player) return true;
    let node = el;
    while (node && node !== player) {
      if (node.nodeType === 1) {
        if (node.tagName === "VIDEO") return true;
        const cls = typeof node.className === "string" ? node.className : "";
        if (/(^|\s)(ytp-|html5-|shisuko-)/.test(cls)) return true;
      }
      node = node.parentNode;
    }
    return false;
  }

  function cueById(id) {
    if (id === null || id === undefined) return null;
    return state.cueById.get(Number(id)) || null;
  }

  // ------------------------------------------------------------ playhead clock

  // The stream clock of a live player, or null for an ordinary video. Firefox lets a content
  // script call the page's player API through wrappedJSObject; only numbers are taken from it.
  function liveClock(player) {
    try {
      const api = player && player.wrappedJSObject;
      if (!api || typeof api.getVideoData !== "function" || typeof api.getProgressState !== "function") return null;
      const data = api.getVideoData();
      if (!data || !data.isLive) return null;
      const current = Number(api.getProgressState().current);
      return Number.isFinite(current) ? current : null;
    } catch (err) {
      return null;
    }
  }

  function updateLiveClock() {
    const current = state.video ? liveClock(state.player) : null;
    state.live = current !== null;
    state.liveOffset = state.live ? current - (Number(state.video.currentTime) || 0) : 0;
  }

  // Where the viewer is, on the clock the cues use. Pure given state.
  function playhead() {
    const t = state.video ? Number(state.video.currentTime) || 0 : 0;
    return state.live ? t + state.liveOffset : t;
  }

  function seekPlayhead(t) {
    state.video.currentTime = Math.max(0, t - (state.live ? state.liveOffset : 0));
  }

  // ------------------------------------------------------------ settings

  async function loadSettings() {
    const result = await sendMessage({ type: "getSettings" });
    if (result && typeof result === "object" && result.ok !== false) {
      state.settings = Object.assign({}, DEFAULT_SETTINGS, result);
    }
    applySettings();
  }

  function saveSettings(patch) {
    return sendMessage({ type: "saveSettings", settings: patch });
  }

  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.settings) return;
    const next = Object.assign({}, DEFAULT_SETTINGS, changes.settings.newValue || {});
    // A verdict belongs to the name it was given for. Keeping it under a corrected name would show
    // "model large-v3: not a model name" until the next round trip, up to five seconds while paused,
    // so the old verdict goes now and the server is asked about the new name at once.
    const modelChanged = modelForSync(next) !== modelForSync(state.settings);
    if (modelChanged) {
      state.modelError = null;
      state.modelLoading = null;
    }
    // The index belongs to a deck and the fields a word and its pitch are read from; with any of
    // them changed, or a colour switched, the lines go back to plain text now and the new deck is
    // asked for at once (never while off, and never from a tab without a video).
    const wordsChanged = WORD_SETTINGS.some((key) => next[key] !== state.settings[key]);
    if (wordsChanged) dropWordIndex();
    // The viewer's own list and the katakana, particle and name switches change what the matcher makes
    // of a line, not the deck: a new list goes into an index built again from the entries in hand
    // (the background is not asked), a switch is an option of the matcher and needs no new index,
    // and the lines are drawn again in place, the looks found under the settings of before being
    // out of date (lookOf compares them).
    const knownChanged = next.knownWords !== state.settings.knownWords;
    const katakanaChanged = !!next.katakanaKnown !== !!state.settings.katakanaKnown;
    const particlesChanged = !!next.particlesKnown !== !!state.settings.particlesKnown;
    const namesChanged = !!next.properNames !== !!state.settings.properNames;
    // The master switch decides a line's look as well (wordColoursOn). Off, nothing more is done:
    // the lines keep their colours under the hidden root (setSubtitle(null) clears the screen,
    // and rewriting every coloured line of a transcript nobody sees, in every tab showing one,
    // would be work for nothing). That holds for the known list and the switches as well:
    // their refresh waits for the switch, whose refresh compares against the index last drawn and
    // the switches each look was found under, and so draws again just the lines they changed.
    // On again, the refresh finds the lines drawn under the index it still holds and touches none
    // of them; only what changed while off (the deck, a colour: wordsChanged dropped the index and
    // made the lines plain; the list, a switch) gets a new draw.
    const enabledChanged = next.enabled !== state.settings.enabled;
    state.settings = next;
    // No setting rebuilds the transcript (see applySettings): a style setting (a slider being
    // dragged in the popup writes several times a second) leaves thousands of lines as they are,
    // and refreshWordMarks() changes the look of a line where it is, from the runs of the last
    // draw, so neither switch costs a rebuild or a match.
    if (knownChanged && !wordsChanged) rebuildWordIndex();
    applySettings();
    if (modelChanged) sync();
    const matcherChanged = knownChanged || katakanaChanged || particlesChanged || namesChanged;
    if (wordsChanged || (matcherChanged && next.enabled) || (enabledChanged && next.enabled)) refreshWordMarks();
    if (wordsChanged && wordColoursOn()) pollWordIndex();
  });

  // Settings come from storage, so every value is treated as untrusted input before it reaches CSS.
  function clampNumber(value, min, max, fallback) {
    const n = Number(value);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  }

  function oneOf(value, allowed, fallback) {
    return allowed.includes(value) ? value : fallback;
  }

  function hexColor(value, fallback) {
    return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value) ? value : fallback;
  }

  function fontFamilyName(value) {
    const name = typeof value === "string" ? value.trim() : "";
    return FONT_FAMILY_RE.test(name) ? name : "";
  }

  // The font-family value for the subtitle and the transcript: the preset's stack, with the
  // viewer's own installed font in front of it when the name is clean. The preset still decides
  // the weight. Pure: the two settings in, a CSS value out.
  function fontStack(subFont, subFontFamily) {
    const preset = SUB_FONTS[oneOf(subFont, Object.keys(SUB_FONTS), DEFAULT_SETTINGS.subFont)];
    const family = fontFamilyName(subFontFamily);
    return family ? `"${family}", ${preset}` : preset;
  }

  // The model name the server is asked for: the setting, trimmed; anything that is not a string
  // is the server's default. Pure. The server validates the name itself; this only cleans it.
  function modelForSync(settings) {
    const model = settings && settings.model;
    return typeof model === "string" ? model.trim() : "";
  }

  // Turn the style settings into custom properties that content.css reads. Setting them on
  // the root keeps the stylesheet the single place that decides where each value lands.
  function applyStyleSettings(root, s) {
    const bottom = clampNumber(s.subPosition, SUB_POSITION_MIN, SUB_POSITION_MAX, DEFAULT_SETTINGS.subPosition);
    const font = oneOf(s.subFont, Object.keys(SUB_FONTS), DEFAULT_SETTINGS.subFont);
    const alpha = clampNumber(s.subBackgroundOpacity, 0, 100, DEFAULT_SETTINGS.subBackgroundOpacity) / 100;
    const style = root.style;
    style.setProperty("--shisuko-sub-bottom", `${bottom}%`);
    // The controls have faded out, so the box drops by the same amount it does at the default.
    style.setProperty("--shisuko-sub-bottom-autohide", `${Math.max(SUB_POSITION_MIN, bottom - AUTOHIDE_DROP)}%`);
    style.setProperty("--shisuko-sub-font", fontStack(font, s.subFontFamily));
    style.setProperty("--shisuko-sub-weight", font === "gothic-bold" ? "700" : "400");
    style.setProperty("--shisuko-sub-color", hexColor(s.subTextColor, DEFAULT_SETTINGS.subTextColor));
    style.setProperty("--shisuko-sub-bg", `rgba(0, 0, 0, ${alpha})`);
    style.setProperty("--shisuko-sub-bg-hover", `rgba(0, 0, 0, ${Math.min(1, alpha + HOVER_ALPHA_STEP)})`);
    style.setProperty("--shisuko-sub-shadow", s.subOutline ? OUTLINE_SHADOW : PLAIN_SHADOW);
    root.classList.toggle("shisuko-transcript-left", oneOf(s.transcriptSide, TRANSCRIPT_SIDES, DEFAULT_SETTINGS.transcriptSide) === "left");
  }

  function applySettings() {
    const s = state.settings;
    document.documentElement.classList.toggle("shisuko-hide-native", !!s.enabled && !!s.hideNativeCaptions);
    if (state.root) {
      const shown = !!s.showTranscript && state.transcriptEl.classList.contains("shisuko-hidden");
      state.root.classList.toggle("shisuko-hidden", !s.enabled);
      state.root.classList.toggle("shisuko-has-transcript", !!s.showTranscript);
      state.transcriptEl.classList.toggle("shisuko-hidden", !s.showTranscript);
      applyStyleSettings(state.root, s);
      // Before the panel is read: highlightTranscript() measures the active line, and the font
      // size the panel is measured at must be the new one.
      updateFontSize();
      if (s.showTranscript) {
        // No line reads a setting (font and colours are custom properties on the root, the size a
        // style on the panel; what a word's card says is drawn in place, see refreshWordMarks), so
        // a settings change never rebuilds the panel: the popup saves on every slider tick, and a
        // rebuild is thousands of nodes in every tab showing it. What arrived while the panel was
        // hidden is still pending, and this renders it.
        renderTranscript();
        // highlightTranscript() and refreshWordMarks() sit out while the panel is hidden: a panel
        // just shown catches up on the line that became active meanwhile and on the deck index
        // that changed meanwhile (the rebuild used to do both on the side).
        if (shown) {
          refreshWordMarks();
          highlightTranscript(cueById(state.activeCueId));
        }
      }
    }
    if (!s.enabled) setSubtitle(null);
    updateStatus();
  }

  // ------------------------------------------------------------ overlay DOM

  function buildOverlay(player) {
    const root = document.createElement("div");
    root.className = "shisuko-root";

    const statusEl = document.createElement("div");
    statusEl.className = "shisuko-status shisuko-hidden";
    root.appendChild(statusEl);

    const toastEl = document.createElement("div");
    toastEl.className = "shisuko-toast shisuko-hidden";
    root.appendChild(toastEl);

    const subWrap = document.createElement("div");
    subWrap.className = "shisuko-subwrap";
    const subBox = document.createElement("div");
    subBox.className = "shisuko-sub shisuko-hidden";
    subBox.setAttribute("lang", "ja");
    // Yomitan's sentence scan (without "layout-aware scan") walks straight across element
    // boundaries and would prepend YouTube's time display to the sentence. Invisible full stops
    // on both sides of the text end the scan at the box edge.
    const subText = document.createElement("span");
    subText.className = "shisuko-subtext";
    subBox.appendChild(makeSentinel("\u3002\n"));
    subBox.appendChild(subText);
    subBox.appendChild(makeSentinel("\n\u3002"));
    const mineBtn = document.createElement("button");
    mineBtn.type = "button";
    mineBtn.className = "shisuko-mine";
    mineBtn.textContent = "⛏";
    mineBtn.title = "Mine this sentence: screenshot + audio (Alt+Shift+M)";
    mineBtn.addEventListener("click", onMineClick);
    subBox.appendChild(mineBtn);
    subWrap.appendChild(subBox);
    root.appendChild(subWrap);

    const transcriptEl = document.createElement("div");
    transcriptEl.className = "shisuko-transcript shisuko-hidden";
    const header = document.createElement("div");
    header.className = "shisuko-transcript-header";
    const title = document.createElement("span");
    title.textContent = "Transcript";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "shisuko-transcript-close";
    closeBtn.textContent = "×";
    closeBtn.title = "Hide transcript (Alt+Shift+L)";
    closeBtn.addEventListener("click", (ev) => {
      if (ev.isTrusted) saveSettings({ showTranscript: false });
    });
    header.appendChild(title);
    header.appendChild(closeBtn);
    const list = document.createElement("div");
    list.className = "shisuko-transcript-list";
    list.setAttribute("lang", "ja");
    list.addEventListener("click", onTranscriptClick);
    // The panel does not scroll itself away under the reader's pointer (see highlightTranscript).
    list.addEventListener("mouseenter", () => { state.transcriptHovered = true; });
    list.addEventListener("mouseleave", () => { state.transcriptHovered = false; });
    transcriptEl.appendChild(header);
    transcriptEl.appendChild(list);
    root.appendChild(transcriptEl);

    subBox.addEventListener("mouseenter", onSubtitleEnter);
    subBox.addEventListener("mouseleave", onSubtitleLeave);

    player.appendChild(root);
    Object.assign(state, { root, statusEl, toastEl, subWrap, subBox, subText, mineBtn, transcriptEl, transcriptList: list });
    state.activeCueId = null;
    state.activeLineEl = null;
    state.transcriptDirty = true;
    state.transcriptRebuild = true;
    state.transcriptAppendFrom = null;
    state.transcriptHovered = false;
    state.lineById.clear();
    applySettings();
  }

  // The overlay lives in the page's DOM, where any script on youtube.com can dispatch a click on
  // the button or a mouseenter on the subtitle; only what the viewer did (isTrusted) may write a
  // card, save a file, seek or pause. The keyboard commands come through the browser, not the page.
  function onMineClick(ev) {
    if (!ev.isTrusted) return;
    ev.preventDefault();
    ev.stopPropagation();
    mineCurrent();
  }

  function makeSentinel(text) {
    // A newline ends Yomitan's sentence in its default mode; the full stop covers the mode
    // where newlines are ignored. The span is clipped to nothing by content.css.
    const el = document.createElement("span");
    el.className = "shisuko-sentinel";
    el.setAttribute("aria-hidden", "true");
    el.textContent = text;
    return el;
  }

  function ensureOverlay(player) {
    if (state.root && state.root.isConnected && state.root.parentElement === player) return;
    if (state.root) state.root.remove();
    buildOverlay(player);
  }

  // ------------------------------------------------------------ discovery

  // The player the page shows. YouTube's SPA keeps the watch page in the DOM when it leaves it
  // (ytd-watch-flexy gets `hidden`, #movie_player stays inside with its video), so on a Short
  // reached from a watch page #movie_player still exists and must not win: the Short would be
  // fetched and transcribed on the hidden video's clock, for cues in a player nobody can see. A
  // fresh load on /shorts/ has no #movie_player and finds #shorts-player through its class;
  // asking for it by id there makes both routes agree.
  function findPlayer(href) {
    const shorts = isShortsUrl(href) ? document.querySelector("#shorts-player") : null;
    return shorts || document.querySelector("#movie_player") || document.querySelector(".html5-video-player");
  }

  function discover() {
    if (!runtimeAlive()) {
      shutdown();
      return;
    }
    let player = state.player;
    let video = state.video;
    const href = location.href;
    // The elements we hold are good until YouTube replaces them, and a replaced element is
    // disconnected; searching the whole document every tick only repeats an answer we already have.
    if (state.rediscover || !player || !player.isConnected || !video || !video.isConnected) {
      state.rediscover = false;
      player = findPlayer(href);
      video = player
        ? player.querySelector("video.html5-main-video") || player.querySelector("video")
        : document.querySelector("video.html5-main-video");
    }
    if (player !== state.player || video !== state.video) attach(player, video);
    else if (player && (!state.root || !state.root.isConnected)) ensureOverlay(player);
    if (href !== state.lastHref) {
      state.lastHref = href;
      const id = getVideoIdFromUrl(href);
      if (id !== state.videoId) onVideoChanged(id);
    }
  }

  function attach(player, video) {
    detach();
    state.player = player;
    state.video = video;
    if (player) {
      ensureOverlay(player);
      state.resizeObserver = new ResizeObserver(() => updateFontSize());
      state.resizeObserver.observe(player);
      player.addEventListener("mousemove", onPlayerMouseMove);
      player.addEventListener("mouseleave", onPlayerMouseLeave);
      updateFontSize();
    }
    if (video) {
      const onTime = () => render();
      const onSeek = () => {
        const now = Date.now();
        if (now - state.lastSeekSync > 250) {
          state.lastSeekSync = now;
          sync();
        }
      };
      const onPlay = () => {
        state.hoverPaused = false;
        state.awaitingPlayerMove = false;
        state.pausedSince = 0;
        clearResumeTimer();
        sync(); // playing again: back to the one second cadence at once, not at the next tick
      };
      const onPause = () => {
        if (!state.pausedSince) state.pausedSince = Date.now();
      };
      video.addEventListener("timeupdate", onTime);
      video.addEventListener("seeking", onSeek);
      video.addEventListener("play", onPlay);
      video.addEventListener("pause", onPause);
      state.videoListeners = { onTime, onSeek, onPlay, onPause };
      if (video.paused) state.pausedSince = Date.now();
    }
  }

  function detach() {
    if (state.video && state.videoListeners) {
      const { onTime, onSeek, onPlay, onPause } = state.videoListeners;
      state.video.removeEventListener("timeupdate", onTime);
      state.video.removeEventListener("seeking", onSeek);
      state.video.removeEventListener("play", onPlay);
      state.video.removeEventListener("pause", onPause);
    }
    state.videoListeners = null;
    if (state.player) {
      state.player.removeEventListener("mousemove", onPlayerMouseMove);
      state.player.removeEventListener("mouseleave", onPlayerMouseLeave);
    }
    if (state.resizeObserver) {
      state.resizeObserver.disconnect();
      state.resizeObserver = null;
    }
  }

  // Forget every cue and everything keyed by a cue id: the transcript lines, the active line, the
  // hover frame, the covered ranges and the `since` cursor. Cue ids start at 0 again in every
  // fresh session (another video, a server restart, a model switch), so a stale entry in cueById
  // would make mergeCues() drop the new cue with the same id and cueById() answer with old text.
  function dropCues() {
    state.cues = [];
    state.cueById.clear();
    state.cueGeneration++;
    state.since = 0;
    state.covered = [];
    state.transcriptDirty = true;
    state.transcriptRebuild = true;
    state.transcriptAppendFrom = null;
    state.lineById.clear();
    clearHoverCapture();
    clearTranscriptHover();
    state.hoverShot = null;
    // The cue ids the held sentences are keyed by mean nothing once the cues are gone.
    resetPremine();
    setSubtitle(null);
    // setSubtitle() already did this where there is an overlay; without one nothing else would.
    state.activeCueId = null;
    state.activeLineEl = null;
  }

  function onVideoChanged(id) {
    state.videoId = id;
    dropCues();
    state.duration = 0;
    state.serverStatus = id ? "connecting" : "idle";
    state.serverError = null;
    state.offline = false;
    state.serverSession = null;
    state.standby = false;
    state.languagePaused = false;
    state.heard = null;
    state.modelLoading = null;
    state.modelError = null;
    state.hoverPaused = false;
    state.awaitingPlayerMove = false;
    state.lastSyncAt = 0;
    state.pausedSince = state.video && state.video.paused ? Date.now() : 0;
    state.live = false;
    state.liveOffset = 0;
    state.serverLive = false;
    state.contentPlayhead = id ? startTimeFromUrl(location.href) : 0;
    clearResumeTimer();
    renderTranscript();
    updateStatus();
    if (id) sync();
  }

  // ------------------------------------------------------------ server sync

  // The covered range the playhead sits in, or null when this position is not covered. Pure: the
  // covered list and a time in, one of its ranges out.
  function coveredRange(covered, t) {
    if (!Array.isArray(covered)) return null;
    for (const range of covered) {
      if (!Array.isArray(range) || range.length < 2) continue;
      if (t >= range[0] - 0.5 && t <= range[1] + 0.01) return range;
    }
    return null;
  }

  // End of the covered range the playhead sits in, or null when this position is not covered.
  // Pure: the covered list and a time in, a time out.
  function coveredEnd(covered, t) {
    const range = coveredRange(covered, t);
    return range ? range[1] : null;
  }

  // Should the tick actually talk to the server? While the video plays, always: the playhead moves
  // and cues are wanted. While it is paused, only while the server still has work around the
  // playhead, plus a slow heartbeat so a restart, an error or a late cue is still noticed — and so
  // the server does not drop the session for want of a client.
  // Pure: { paused, t, status, covered, duration, lastSyncAt, standby } and a clock in, a decision out.
  function shouldSync(st, now) {
    if (!st.paused) return true;
    if (now - (st.lastSyncAt || 0) >= SYNC_IDLE_INTERVAL_MS) return true;
    // Standing by, paused: the heartbeat above is the whole cadence. Its status was left wherever
    // the last real answer put it, so without this the rule below would make the tab ask every
    // tick for a server it is not allowed to reach. A playing tab already returned true: that is
    // what makes taking the right back immediate the moment the viewer looks at it.
    if (st.standby) return false;
    if (st.status !== "ready") return true; // still fetching, decoding, erroring: keep watching
    const duration = Number(st.duration) || 0;
    const target = duration > 0 ? Math.min(duration, st.t + SYNC_LOOKAHEAD_S) : Infinity;
    const ahead = coveredEnd(st.covered, st.t);
    return ahead === null || ahead < target - 0.5;
  }

  function syncTick() {
    const video = state.video;
    if (!video) return;
    pollForNewCard();
    pollWordIndex();
    const decision = {
      paused: !!video.paused,
      t: playhead(),
      status: state.serverStatus,
      covered: state.covered,
      duration: state.duration,
      lastSyncAt: state.lastSyncAt,
      standby: state.standby,
    };
    if (shouldSync(decision, Date.now())) sync();
  }

  async function sync() {
    const s = state.settings;
    if (!s.enabled || !state.videoId || !state.video || state.syncInFlight) return;
    const videoId = state.videoId;
    const generation = state.cueGeneration;
    // An ad runs on a clock of its own, so the position sent while one shows is the video's: where
    // it was at the last request outside an ad, or where it will start before the first (the
    // link's t=, else 0). The request itself still goes out: the server hears of a new video only
    // through /sync, so a pre-roll ad used to hold up the audio fetch and the first window by its
    // whole length, and a long mid-roll let the session time out.
    const ad = isAdPlaying();
    if (!ad) {
      updateLiveClock();
      state.contentPlayhead = playhead();
    }
    state.syncInFlight = true;
    state.lastSyncAt = Date.now();
    let result;
    try {
      result = await sendMessage({
        type: "api",
        path: "/sync",
        body: {
          video_id: videoId,
          url: location.href,
          t: state.contentPlayhead,
          paused: !!state.video.paused,
          since: state.since,
          model: modelForSync(s),
        },
      });
    } finally {
      state.syncInFlight = false;
    }
    // Another video now, or this one again after a detour (A -> B -> A while the request hung):
    // the cues started over meanwhile, and taking this answer's `next` as the cursor would leave
    // everything before it unfetched for good. The tick asks again with the fresh cursor.
    if (videoId !== state.videoId || generation !== state.cueGeneration) return;
    // The background refused this tab: another tab holds the server, and this answer never left
    // the browser. So nothing is written down here -- not the cues, not `since`, not the covered
    // ranges, and least of all "the server is reachable". A standby tab that recorded that would
    // be unable to tell waiting for its turn from a dead server, and would go on fetching clips
    // and polling Anki against a server it never contacted.
    if (result && result.ok && result.data && result.data.status === "standby") {
      state.standby = true;
      updateStatus();
      return;
    }
    if (!result || !result.ok) {
      // Not standby, so the election let this request through: the background answers standby
      // only with ok: true. A flag left by the last refused tick would rank above the verdict
      // below in statusText() and keep the pre-mining and the Anki watch closed.
      state.standby = false;
      // The language verdict came from the last answer that was one; this one says nothing about
      // the session, so it goes, as on a restart, or statusText() would show the pause in place of
      // the error below. The next real answer brings it back.
      state.languagePaused = false;
      state.heard = null;
      // Only an answer the server itself gave carries `data` (null for an empty body). Without it
      // nothing of ours answered: the server unreachable, something else on its port (not JSON),
      // or the background gone (sendMessage's own {ok, error} during a reload or a worker restart),
      // and every other message would fail the same way, so the pre-mining and the Anki watch stop.
      if (!result || result.offline || !("data" in result)) {
        state.offline = true;
        state.serverStatus = "offline";
        state.serverError = errorText(result && result.error) || "Server unreachable";
      } else {
        // The server is up and refused this request (a 400 for an odd id, a 500 from a broken
        // cache): its own words, in the error style. It is not offline, so the pre-mining and the
        // Anki watch go on; /clip is another handler.
        state.offline = false;
        state.serverStatus = "error";
        state.serverError = errorText(result.error) || "error";
      }
      updateStatus();
      return;
    }
    state.standby = false;
    state.offline = false;
    const data = result.data || {};
    // Read before the session check: a model switch ends in a fresh session, and the status must
    // already say what is being loaded while the old one's cues are dropped. An older server sends
    // neither key, which is the same as nothing loading and nothing wrong. Both are judged for the
    // name this request carried: when the settings changed it while the request was out, the
    // storage listener dropped that name's verdict and could not ask about the new one (this
    // request was in flight), so this answer's verdict stays out and the new name goes right after.
    const modelChanged = modelForSync(s) !== modelForSync(state.settings);
    if (!modelChanged) {
      state.modelLoading = typeof data.model_loading === "string" && data.model_loading ? data.model_loading : null;
      state.modelError = typeof data.model_error === "string" && data.model_error ? data.model_error : null;
    }
    if (typeof data.session === "string" && data.session !== state.serverSession) {
      const restarted = state.serverSession !== null;
      state.serverSession = data.session;
      if (restarted) {
        // The server started a fresh session for this video (restart, model change): its cue ids
        // begin at 0 again, so drop what we have (the held sentences too) and fetch the new
        // transcript from the start. The cues in this answer were asked for with the old session's
        // `since`, so they are not taken; the status is the new session's and can be shown right away.
        dropCues();
        state.serverStatus = data.status || "unknown";
        state.serverError = errorText(data.error);
        state.languagePaused = false;
        state.heard = null;
        renderTranscript();
        updateStatus();
        if (modelChanged) sync();
        return;
      }
    }
    state.serverStatus = data.status || "unknown";
    state.serverError = errorText(data.error);
    // The server gave up on this video's language and stopped transcribing. An older server sends
    // neither key, which reads as "still listening".
    state.languagePaused = data.language_paused === true;
    state.heard = typeof data.heard === "string" ? data.heard : null;
    if (typeof data.live === "boolean") state.serverLive = data.live; // an older server sends no key: unchanged
    if (typeof data.duration === "number") state.duration = data.duration;
    if (Array.isArray(data.covered)) state.covered = data.covered;
    if (Array.isArray(data.cues) && data.cues.length) mergeCues(data.cues);
    if (typeof data.next === "number") state.since = data.next;
    updateStatus();
    render();
    if (modelChanged) sync(); // after this answer is applied, so the new request carries the right `since`
  }

  function mergeCues(incoming) {
    const seen = state.cueById; // already holds every cue: no need to rebuild an id set per response
    const before = state.cues.length;
    let lastStart = before ? state.cues[before - 1].start : -Infinity;
    let inOrder = true;
    let added = false;
    for (const raw of incoming) {
      if (!raw || typeof raw.text !== "string") continue;
      // The server stamps every cue with the `seg` of the Whisper segment it came from; nothing
      // here reads it, because a segment is not a sentence (see sentenceForCue).
      const cue = {
        id: Number(raw.id),
        start: Number(raw.start) || 0,
        end: Number(raw.end) || 0,
        text: raw.text.trim(),
      };
      // JSON.parse turns 1e999 into Infinity: a cue that never ends, or a seek target the video
      // element throws on. Nothing a server has a reason to send, so it is dropped like a bad id.
      if (!cue.text || !Number.isFinite(cue.id) || seen.has(cue.id)) continue;
      if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.end < cue.start) continue;
      seen.set(cue.id, cue);
      if (cue.start <= lastStart) inOrder = false;
      lastStart = cue.start;
      state.cues.push(cue);
      added = true;
    }
    if (added) {
      // Cues normally arrive in order, and then the array is already sorted: sorting thousands of
      // them again every second would be the one expensive thing on this path.
      if (!inOrder) state.cues.sort((a, b) => a.start - b.start || a.end - b.end);
      // New cues that all lie after the last rendered one keep the rendered prefix intact (the
      // sort is stable), so the panel can append them; cues sorted in behind it are given their
      // place among the rendered lines instead (see renderTranscript).
      if (!state.transcriptRebuild) {
        if (inOrder) state.transcriptAppendFrom = state.transcriptAppendFrom === null ? before : Math.min(state.transcriptAppendFrom, before);
        else state.transcriptAppendFrom = null;
      }
      state.transcriptDirty = true;
      renderTranscript();
    }
  }

  // ------------------------------------------------------------ rendering

  function findActiveCue(t) {
    const cues = state.cues;
    if (!cues.length) return null;
    let lo = 0;
    let hi = cues.length - 1;
    let idx = -1;
    const target = t + 0.05;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].start <= target) {
        idx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    if (idx < 0) return null;
    for (let i = idx; i >= 0 && i > idx - 4; i--) {
      const c = cues[i];
      if (t >= c.start - 0.05 && t <= c.end + 0.05) return c;
    }
    // Past every candidate's end: keep the newest one up for a moment. A cue that arrived late and
    // sorted in behind the playhead only shortens that moment, because `next` is always the cue
    // following `last` in the current, freshly sorted array.
    const last = cues[idx];
    const next = cues[idx + 1];
    if (next && t >= next.start) return null;
    const linger = Math.max(0, Number(state.settings.lingerSeconds) || 0);
    const lingerEnd = last.end + linger;
    // The blank the viewer would see runs from the end of the linger to the moment the next line
    // shows, 0.05 s before its start (above); that is what is judged against MIN_BLANK_S, not the
    // gap between the cues, or the gaps just over the 0.5 s the server leaves open, the commonest
    // kind, would still blink for a render tick or two.
    const until = next && next.start - 0.05 - lingerEnd < MIN_BLANK_S ? next.start : lingerEnd;
    return t <= until ? last : null;
  }

  // The covered range whose end lies nearest behind `t`, when that end is less than COVERED_LAG_S
  // ago; else null. Pure: the covered list and a time in, one of its ranges out.
  function rangeBehind(covered, t) {
    if (!Array.isArray(covered)) return null;
    let best = null;
    for (const range of covered) {
      if (!Array.isArray(range) || range.length < 2) continue;
      if (range[1] < t && t - range[1] <= COVERED_LAG_S && (!best || range[1] > best[1])) best = range;
    }
    return best;
  }

  // Where Left/Right should land. Pure: `cues` sorted by start, `t` the playhead, `direction`
  // -1 or +1, `covered` the server's ranges (left out, every position counts as transcribed).
  // Returns the time to seek to, or null when YouTube's own five second seek is the right thing:
  // no line lies that way, there is no cue at all (the server offline, the first window still
  // out), or the target sits outside the covered range around the playhead, because the last
  // known line before an untranscribed stretch is not the previous line, and Left stepping back
  // minutes to it is not what the key means. The playhead's range is the one it sits in, or the
  // one that ended less than COVERED_LAG_S before it (rangeBehind): a live stream's playhead runs
  // past the covered end most of the time, and so does a plain video's while a window is still
  // being transcribed, and the line just heard is still the previous line there.
  //
  // Left steps one line back and lands on its start: inside a line, the line before it; in the
  // gap after a line, that line itself, which is the last thing heard and the one the viewer
  // wants again. Before the first line it lands on the start of the video. Right takes the start
  // of the next line. Left used to replay the current line once the viewer was a second into it,
  // asbplayer's rule, but a subtitle line runs three to six seconds, so past that first second --
  // nearly always -- Left restarted the line already playing and read as a dead key.
  function jumpTarget(cues, t, direction, covered) {
    const list = cues || [];
    if (!list.length) return null;
    const target = t + 0.05;
    let idx = -1; // the last cue that has already started
    for (let lo = 0, hi = list.length - 1; lo <= hi; ) {
      const mid = (lo + hi) >> 1;
      if (list[mid].start <= target) {
        idx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    let j; // the line to land on
    if (direction > 0) {
      j = idx + 1;
    } else {
      const cur = list[idx];
      // Inside a line, step past it to the one before; in the gap after a line, that line is
      // itself the step back, since nothing has been heard since.
      j = cur && t <= cur.end ? idx - 1 : idx;
    }
    let to;
    if (direction < 0 && j < 0) to = 0; // nothing before the first line but the start of the video
    else if (j < 0 || j >= list.length) to = null;
    else to = leadIn(list[j].start, j > 0 ? list[j - 1].end : null);
    if (to === null || covered === undefined) return to;
    const range = coveredRange(covered, t) || rangeBehind(covered, t);
    return range && coveredRange(covered, to) === range ? to : null;
  }

  // A seek lands a shade before the line so its first syllable survives, but the lead-in may only
  // eat silence. `normalise_gaps` on the server closes every gap under 0.5 s to 0.1 s, shorter
  // than the lead-in itself, so reaching back through one lands inside the previous line and
  // flashes its tail before the line that was asked for. Where there is no room, land on the line.
  function leadIn(start, prevEnd) {
    const want = start - CUE_LEAD_IN_S;
    return Math.max(0, prevEnd !== null && prevEnd !== undefined && want < prevEnd ? start : want);
  }

  function onKeyDown(ev) {
    if (!state.settings.enabled || !state.settings.arrowKeysJumpCues) return;
    if (ev.ctrlKey || ev.altKey || ev.metaKey || ev.shiftKey) return;
    const direction = ev.key === "ArrowLeft" ? -1 : ev.key === "ArrowRight" ? 1 : 0;
    if (!direction) return;
    const video = state.video;
    // No watch page (YouTube keeps the player around off one): nothing to jump between.
    if (!video || !state.videoId) return;
    // Typing in the search box or a comment: the arrows belong to the caret.
    const el = ev.target;
    if (el && typeof el.closest === "function" && el.closest(KEY_SKIP_SELECTOR)) return;
    const to = jumpTarget(state.cues, playhead(), direction, state.covered);
    if (to === null) return; // no line that way: leave YouTube's five second seek alone
    // YouTube listens on the player while the event bubbles, so the capture phase is not enough
    // on its own; killing the rest of the dispatch here is what keeps the 5 s seek from firing.
    ev.preventDefault();
    ev.stopImmediatePropagation();
    const playing = !video.paused;
    // A jump is the viewer taking over, exactly as onPlay treats a manual resume.
    state.hoverPaused = false;
    state.awaitingPlayerMove = false;
    clearResumeTimer();
    seekPlayhead(to);
    if (playing) video.play().catch(() => {});
  }

  function render() {
    const video = state.video;
    if (!video || !state.subText || !state.settings.enabled) return;
    if (isAdPlaying()) {
      setSubtitle(null);
      return;
    }
    if (state.hoverPaused) return;
    setSubtitle(findActiveCue(playhead()));
  }

  function setSubtitle(cue) {
    if (!state.subBox) return;
    const id = cue ? cue.id : null;
    if (id === state.activeCueId) return;
    state.activeCueId = id;
    if (!cue) {
      state.subBox.classList.add("shisuko-hidden");
      state.subText.textContent = "";
    } else {
      renderText(state.subText, cue);
      state.subBox.classList.remove("shisuko-hidden");
      schedulePremine(cue);
    }
    highlightTranscript(cue);
  }

  function updateFontSize() {
    if (!state.player || !state.subBox) return;
    const h = state.player.clientHeight || 0;
    const scale = Number(state.settings.fontScale) || 1;
    const px = Math.min(96, Math.max(13, h * 0.047 * scale));
    state.subBox.style.fontSize = `${px.toFixed(1)}px`;
    if (state.transcriptEl) {
      const tpx = Math.min(22, Math.max(12, h * 0.022 * scale));
      state.transcriptEl.style.fontSize = `${tpx.toFixed(1)}px`;
    }
  }

  // What the status line should say, and whether it is an error. Pure: a plain view in,
  // { text, isError } out.
  //
  // Precedence. "Offline" and a refused model are verdicts about the server this tab last reached,
  // and they outrank a local standby: a tab that cannot see the server has worse news than a tab
  // waiting for its turn. The language pause comes next, above the status switch and not inside
  // `case "ready"`: it is a property of the video, not a stage of the work, and nesting it would
  // let it vanish the moment the server reported it beside any other status. Both of the new lines
  // are shown even with progress messages off, because they are the only answer to "why is nothing
  // appearing?" -- but neither is an error, so `always`, not `isError`, is what carries them past
  // the setting.
  function statusText(view) {
    let text = null;
    let isError = false;
    let always = false;
    if (view.enabled && view.videoId) {
      if (view.offline || view.status === "offline") {
        text = "Shisu-ko server offline. Start it with server/run.cmd or docker/up.cmd";
        isError = true;
      } else if (view.modelError) {
        // The model this viewer asked for is unusable: an error like the server's own, so it shows
        // even with progress messages off. The fix is in the popup, so the name goes in the text;
        // both are capped like a toast, since neither is ours.
        const name = truncate(view.model, STATUS_NAME_MAX_CHARS);
        text = `Shisu-ko: model${name ? " " + name : ""}: ${truncate(view.modelError, STATUS_ERROR_MAX_CHARS)}`;
        isError = true;
      } else if (view.standby) {
        text = "Shisu-ko: subtitles are running in another tab";
        always = true;
      } else if (view.languagePaused) {
        const heard = typeof view.heard === "string" && view.heard ? ` (hearing ${truncate(view.heard, STATUS_NAME_MAX_CHARS)})` : "";
        text = "Shisu-ko paused: the speech is not in the subtitle language" + heard;
        always = true;
      } else if (view.modelLoading && view.status !== "error") {
        // Transcription waits for the load, whatever the session's status says meanwhile. A session
        // that failed is the exception: no load makes an audio fetch succeed, and its error must
        // not sit behind minutes of "Loading model" (or, with progress messages off, behind nothing).
        text = `Loading model ${view.modelLoading}… (a first use downloads it)`;
      } else {
        switch (view.status) {
          case "connecting":
            text = "Connecting to the Shisu-ko server…";
            break;
          case "pending":
          case "downloading":
            text = "Fetching audio…";
            break;
          case "decoding":
            text = "Decoding audio…";
            break;
          case "error":
            // The server's words, capped like the model error: a 500 carries the raw exception.
            text = "Shisu-ko: " + truncate(view.error || "error", STATUS_ERROR_MAX_CHARS);
            isError = true;
            break;
          case "ready": {
            const t = view.t;
            const ahead = view.ahead;
            if (ahead === null) text = "Transcribing…";
            else if (ahead - t < 8 && ahead < view.duration - 1) text = `Transcribing… (ready to ${formatTime(ahead)})`;
            // Covered to the end (the "done" reading, a second of slack for the decoder's own idea
            // of the length) with not one cue: a silent clip, an instrumental. The line used to fall
            // silent here too, which looks like a failure. Not an error, and a progress message like
            // "Transcribing…", so it obeys the setting. A live stream's cues keep coming: never there.
            // And `ahead` is the end of the range the playhead sits in, which need not begin at the
            // start: a video resumed near its end (the saved position, a t= link) gets one window
            // from the playhead on and nothing before it, the server never planning backwards, so
            // the verdict about the whole video needs that range to run from the start as well
            // (the first window opens half a second before the playhead).
            else if (
              ahead >= view.duration - 1 && view.duration > 0 && view.cueCount === 0 && !view.live &&
              view.coveredFrom !== null && view.coveredFrom <= 0.5
            ) {
              text = "No speech found in this video";
            }
            break;
          }
          default:
            break;
        }
      }
    }
    if (!view.showStatus && !isError && !always) text = null;
    // The badge switched off (Alt+Shift+H, or the popup's switch) shows nothing at all, errors and
    // the standby and pause lines included: the viewer asked for the red "server offline" to go
    // too. The popup's header still says how the server is.
    if (view.statusBadge === false) text = null;
    return { text, isError };
  }

  function updateStatus() {
    const el = state.statusEl;
    if (!el) return;
    const s = state.settings;
    // An ad runs on its own clock; the video's position is what the server works around.
    const t = isAdPlaying() ? state.contentPlayhead : playhead();
    const range = coveredRange(state.covered, t);
    const { text, isError } = statusText({
      enabled: s.enabled,
      showStatus: s.showStatus,
      statusBadge: s.statusBadge,
      model: modelForSync(s),
      videoId: state.videoId,
      status: state.serverStatus,
      error: state.serverError,
      offline: state.offline,
      standby: state.standby,
      languagePaused: state.languagePaused,
      heard: state.heard,
      modelLoading: state.modelLoading,
      modelError: state.modelError,
      duration: state.duration,
      t,
      ahead: range ? range[1] : null,
      coveredFrom: range ? range[0] : null,
      cueCount: state.cues.length,
      // The player's own API says live only in Firefox, and not through an ad; the server's flag
      // says it everywhere, so a stream's waiting screen never reads as a video without speech.
      live: state.live || state.serverLive,
    });
    // Only touch the DOM when something changed: every mutation wakes other extensions'
    // observers (Bitwarden re-walks the whole page after each one).
    const hidden = el.classList.contains("shisuko-hidden");
    if (!text) {
      if (!hidden) el.classList.add("shisuko-hidden");
      return;
    }
    if (el.textContent !== text) el.textContent = text;
    if (el.classList.contains("shisuko-status-error") !== isError) el.classList.toggle("shisuko-status-error", isError);
    if (hidden) el.classList.remove("shisuko-hidden");
  }

  function showToast(text, kind, ms) {
    const el = state.toastEl;
    if (!el) return;
    // An error can carry a whole URL or payload; a toast that fills the player helps nobody.
    el.textContent = truncate(text, TOAST_MAX_CHARS);
    let cls = "shisuko-toast";
    if (kind === "error") cls += " shisuko-toast-error";
    else if (kind === "warn") cls += " shisuko-toast-warn";
    else if (kind === "ok") cls += " shisuko-toast-ok";
    el.className = cls;
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => el.classList.add("shisuko-hidden"), ms || TOAST_MS);
  }

  // ------------------------------------------------------------ transcript panel

  function transcriptLine(cue) {
    const line = document.createElement("div");
    line.className = "shisuko-line";
    line.dataset.start = String(cue.start);
    line.dataset.id = String(cue.id);
    const time = document.createElement("span");
    time.className = "shisuko-time";
    time.textContent = formatTime(cue.start);
    time.title = "Jump here";
    const text = document.createElement("span");
    text.className = "shisuko-linetext";
    renderText(text, cue);
    state.lineTexts.set(line, text);
    const mine = document.createElement("button");
    mine.type = "button";
    mine.className = "shisuko-line-mine";
    mine.textContent = "⛏";
    mine.title = "Mine this sentence: screenshot + audio";
    line.appendChild(time);
    line.appendChild(text);
    line.appendChild(mine);
    line.addEventListener("mouseenter", onTranscriptLineEnter);
    line.addEventListener("mouseleave", onTranscriptLineLeave);
    state.lineById.set(cue.id, line);
    return line;
  }

  function renderTranscript() {
    const list = state.transcriptList;
    if (!list || !state.settings.showTranscript || !state.transcriptDirty) return;
    state.transcriptDirty = false;
    const from = state.transcriptAppendFrom;
    state.transcriptAppendFrom = null;
    const rebuild = state.transcriptRebuild;
    state.transcriptRebuild = false;
    // The map counts the lines already in the panel, so nothing walks the DOM to find out.
    const rendered = state.lineById.size;
    if (!rebuild && rendered && from !== null && from === rendered && from <= state.cues.length) {
      // Only cues after the rendered ones arrived: append them instead of rebuilding thousands of nodes.
      const frag = document.createDocumentFragment();
      for (let i = from; i < state.cues.length; i++) frag.appendChild(transcriptLine(state.cues[i]));
      list.appendChild(frag);
    } else if (!rebuild && rendered) {
      // Cues sorted in among the rendered ones: the server transcribes forward from a seek back
      // behind the known lines, so every window it finishes there arrives like this for minutes.
      // Each new cue gets its line before the line of the next cue that has one; walking from the
      // end, that is the last line put in. A map lookup per cue, against a rebuild of every line.
      let left = state.cues.length - rendered; // lines still to make
      let next = null; // insertBefore(null) appends
      for (let i = state.cues.length - 1; i >= 0 && left > 0; i--) {
        const cue = state.cues[i];
        const line = state.lineById.get(cue.id);
        if (line) {
          next = line;
          continue;
        }
        next = list.insertBefore(transcriptLine(cue), next);
        left--;
      }
    } else {
      state.lineById.clear();
      const frag = document.createDocumentFragment();
      for (const cue of state.cues) frag.appendChild(transcriptLine(cue));
      if (!state.cues.length) {
        const empty = document.createElement("div");
        empty.className = "shisuko-empty";
        empty.textContent = "No transcript yet.";
        frag.appendChild(empty);
      }
      list.replaceChildren(frag);
      state.activeLineEl = null;
    }
    if (state.activeCueId !== null) highlightTranscript(cueById(state.activeCueId));
  }

  function highlightTranscript(cue) {
    const list = state.transcriptList;
    if (!list || !state.settings.showTranscript) return;
    if (state.activeLineEl) state.activeLineEl.classList.remove("shisuko-active");
    state.activeLineEl = null;
    if (!cue) return;
    const line = state.lineById.get(cue.id);
    if (!line) return;
    line.classList.add("shisuko-active");
    state.activeLineEl = line;
    // matches(":hover") would flush style on every cue change; the two listeners on the list keep
    // the same answer for free. offsetTop below forces layout, so it stays behind this guard.
    if (!state.transcriptHovered) {
      list.scrollTop = line.offsetTop - list.clientHeight / 2 + line.offsetHeight / 2;
    }
  }

  function onTranscriptClick(ev) {
    if (!ev.isTrusted) return; // see onMineClick
    const mineButton = ev.target.closest(".shisuko-line-mine");
    if (mineButton) {
      ev.preventDefault();
      const line = mineButton.closest(".shisuko-line");
      const cue = cueById(line && line.dataset.id);
      if (cue) mineCue(cue, { seekForFrame: cue.id !== state.activeCueId });
      return;
    }
    const time = ev.target.closest(".shisuko-time");
    if (!time || !state.video) return;
    const line = time.closest(".shisuko-line");
    const start = Number(line && line.dataset.start);
    if (!Number.isFinite(start)) return;
    seekPlayhead(start - 0.2);
    state.video.play().catch(() => {});
  }

  // ------------------------------------------------------------ word colours

  // Either colour needs the deck index; neither may cost anything while the add-on is off.
  function wordColoursOn() {
    const s = state.settings;
    return !!s.enabled && (!!s.cardStatus || !!s.pitchAccent);
  }

  function dropWordIndex() {
    state.wordIndex = null;
    state.wordEntries = null;
    state.wordIndexSerial++;
    state.wordIndexAt = 0;
    state.wordIndexKey = "";
    state.wordIndexAskedAt = 0;
    state.wordIndexGeneration++;
  }

  // How a cue's text is to be drawn now: `runs`, a string for plain text and an object for a word
  // whose card has something to show under the settings of now (null while the whole text is
  // plain), and `key`, its drawKey(). Found once per cue, index, pair of colours and pair of
  // switches (katakana, particles, names) and kept in cueLooks (the text's word boundaries with it,
  // whatever the index), so that the panel rebuilt for a style change, or a line the refresh
  // finds untouched, asks the matcher nothing. The look names the index it was found under by its
  // serial, never by holding it: the index of a 10k-word deck weighs a megabyte and a hidden
  // transcript's cues are never visited again. The viewer's known words are in the index
  // (buildIndex takes them), so the serial dates a look for them too; the katakana, particle and
  // name switches reach the matcher as options of their own.
  function lookOf(cue) {
    const s = state.settings;
    const index = state.wordIndex;
    if (!wordColoursOn() || !index) return { runs: null, key: cue.text };
    const cardStatus = !!s.cardStatus;
    const pitchAccent = !!s.pitchAccent;
    const katakana = !!s.katakanaKnown;
    const particles = !!s.particlesKnown;
    const names = !!s.properNames;
    const serial = state.wordIndexSerial;
    const known = state.cueLooks.get(cue);
    if (known && known.serial === serial && known.cardStatus === cardStatus && known.pitchAccent === pitchAccent && sameSwitches(known, s)) return known;
    const starts = known ? known.starts : SHISUKO_WORDS.wordStarts(cue.text);
    const runs = [];
    let plain = "";
    for (const run of SHISUKO_WORDS.markWords(cue.text, index, starts, { katakana, particles, names })) {
      const status = cardStatus && run.status ? run.status : null;
      const pitch = pitchAccent && run.pitch ? run.pitch : null;
      // A card with nothing to show here is text like any other, joined with its neighbours.
      if (!status && !pitch) {
        plain += run.text;
        continue;
      }
      if (plain) runs.push(plain);
      plain = "";
      runs.push({ text: run.text, status, pitch });
    }
    if (plain) runs.push(plain);
    const look = { serial, cardStatus, pitchAccent, katakana, particles, names, starts, runs, key: drawKey(cue.text, runs) };
    state.cueLooks.set(cue, look);
    return look;
  }

  // Whether a look was found with the matcher's switches as the settings have them now: katakana
  // words and particles counted as known, names drawn blue, or not.
  function sameSwitches(look, s) {
    return look.katakana === !!s.katakanaKnown && look.particles === !!s.particlesKnown && look.names === !!s.properNames;
  }

  // One string per look of a line: the text and where each mark sits in it. Two draws with the
  // same key put the same nodes in.
  function drawKey(text, runs) {
    let key = text;
    if (!runs) return key;
    let at = 0;
    for (const run of runs) {
      if (typeof run === "string") {
        at += run.length;
        continue;
      }
      key += `\n${at} ${run.text.length} ${run.status || ""} ${run.pitch || ""}`;
      at += run.text.length;
    }
    return key;
  }

  function drawRuns(el, text, runs, key) {
    state.drawnKeys.set(el, key);
    if (!runs) {
      el.textContent = text;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const run of runs) {
      if (typeof run === "string") {
        frag.appendChild(document.createTextNode(run));
        continue;
      }
      const span = document.createElement("span");
      span.className = "shisuko-word";
      if (run.status) span.dataset.status = run.status;
      if (run.pitch) span.dataset.pitch = run.pitch;
      span.textContent = run.text;
      frag.appendChild(span);
    }
    el.replaceChildren(frag);
  }

  // The one place a cue's text goes into an element, on screen and in the transcript. The text
  // stays DOM text nodes, which is what Yomitan scans; a word with a card sits in an inline span
  // that carries what the card says, and content.css colours it. Nothing else is ever put in.
  function renderText(el, cue) {
    const look = lookOf(cue);
    drawRuns(el, cue.text, look.runs, look.key);
  }

  // Draw the text again only where the index or the settings changed its look. Only for an
  // element renderText() last wrote: what anything else put in is not on record.
  function refreshText(el, cue) {
    const look = lookOf(cue);
    if (state.drawnKeys.get(el) === look.key) return;
    drawRuns(el, cue.text, look.runs, look.key);
  }

  // The strings a line must hold for its runs to differ under `next` from those under `prev`: a
  // word one index has a card for and the other not, or says something else about, or the stem
  // it is found by (a conjugated form starts with the stem and so does the word itself, so the
  // stem alone is looked for). Empty when the two say the same about every word; null when they
  // differ in more words than WORD_INDEX_PROBE_MAX, where matching the lines again is no dearer.
  function indexProbes(prev, next) {
    const changed = new Set();
    for (const [word, entry] of next.exact) {
      const was = prev.exact.get(word);
      if (!was || was.status !== entry.status || was.pitch !== entry.pitch) changed.add(word);
    }
    for (const word of prev.exact.keys()) if (!next.exact.has(word)) changed.add(word);
    if (changed.size > WORD_INDEX_PROBE_MAX) return null;
    if (!changed.size) return [];
    const probes = new Set();
    const stemmed = new Set();
    for (const stems of [prev.stems, next.stems]) {
      for (const [stem, list] of stems) {
        for (const { entry } of list) {
          if (!changed.has(entry.word)) continue;
          probes.add(stem);
          stemmed.add(entry.word);
        }
      }
    }
    for (const word of changed) if (!stemmed.has(word)) probes.add(word);
    return [...probes];
  }

  // Whether a transcript line looks under the index of now as it already does: what it shows are
  // the runs found under the index of serial `prevSerial` with the colours and switches of now,
  // and none of `probes` is in its text, so the index of now finds the same runs. Those are then
  // on record for it too.
  function sameLook(el, cue, prevSerial, probes) {
    const look = state.cueLooks.get(cue);
    const s = state.settings;
    if (!look || look.serial !== prevSerial || look.cardStatus !== !!s.cardStatus || look.pitchAccent !== !!s.pitchAccent) return false;
    if (!sameSwitches(look, s)) return false;
    if (state.drawnKeys.get(el) !== look.key) return false;
    for (const probe of probes) if (cue.text.includes(probe)) return false;
    look.serial = state.wordIndexSerial;
    return true;
  }

  // Draw the line on screen and the transcript again with the index as it is now. The transcript
  // is walked line by line and only the lines that would look different are touched, so nothing
  // scrolls and the thousands of others keep their nodes; and against the index of the last
  // refresh, a line holding none of the words the two differ on is not even matched again, so a
  // card reviewed in Anki costs a look at each line's text. Lines still pending (none, as a rule:
  // a shown panel renders on arrival) go in first, drawn under the index of now.
  function refreshWordMarks() {
    const index = state.wordIndex;
    const prev = state.wordIndexDrawn;
    const prevSerial = state.wordIndexDrawnSerial;
    const probes = prev && index && wordColoursOn() ? indexProbes(prev, index) : null;
    state.wordIndexDrawn = index;
    state.wordIndexDrawnSerial = state.wordIndexSerial;
    if (state.activeCueId !== null && state.subText) {
      const cue = cueById(state.activeCueId);
      if (cue && !(probes && sameLook(state.subText, cue, prevSerial, probes))) refreshText(state.subText, cue);
    }
    if (!state.settings.showTranscript || !state.transcriptList) return;
    if (state.transcriptDirty) renderTranscript();
    for (const cue of state.cues) {
      const text = state.lineTexts.get(state.lineById.get(cue.id));
      if (!text) continue;
      if (probes && sameLook(text, cue, prevSerial, probes)) continue;
      refreshText(text, cue);
    }
  }

  // Ask the background for the deck's words. It runs from syncTick(), so a switched-off add-on
  // and a hidden tab never ask, and neither does a tab without a video (a settings change asks
  // from every tab, the home page and the player kept off a watch page included): the background
  // answers from its cache, and "unchanged" when it still holds what this tab was last given.
  // A tab standing by for another (state.standby) asks like any other: the index is the deck's,
  // read from Anki through the background's one cache, and the election is about the server,
  // which this never reaches; the standby tab keeps its lines on screen, and a card reviewed
  // must recolour them there too, for the price of one cache lookup every half minute.
  async function pollWordIndex() {
    if (!wordColoursOn() || !state.video || !state.videoId || state.wordIndexInFlight) return;
    if (document.visibilityState !== "visible") return;
    const now = Date.now();
    if (now - state.wordIndexAskedAt < WORD_INDEX_REFRESH_MS) return;
    state.wordIndexAskedAt = now;
    state.wordIndexInFlight = true;
    const generation = state.wordIndexGeneration;
    let res;
    try {
      res = await sendMessage({ type: "cardStatus", since: state.wordIndexAt });
    } finally {
      state.wordIndexInFlight = false;
    }
    // Started over while the ask was out: this answer is about the deck or the fields of before,
    // and the next tick asks again (the stamp was reset with the index).
    if (!res || generation !== state.wordIndexGeneration) return;
    if (!res.ok) {
      // Turned off since the ask: nothing may stay coloured. Anki closed or a deck gone is
      // ordinary and gets a debug line, never a toast.
      if (res.reason === "off") {
        dropWordIndex();
        refreshWordMarks();
      } else {
        logWordIndexError(res.error || res.reason);
        // No deck to colour by (nothing mined and none chosen, Anki closed since the page
        // loaded): the viewer's known words, the particles and the katakana words need none, so
        // they are drawn from an index of the list alone until a deck answer replaces it (the
        // stamp and the key stay unset, so the first one does).
        if (!state.wordIndex) {
          rebuildWordIndex();
          refreshWordMarks();
        }
      }
      return;
    }
    if (res.unchanged || !Array.isArray(res.entries)) return;
    const at = Number(res.at) || 0;
    if (at === state.wordIndexAt) return;
    state.wordIndexAt = at;
    // The background refetches the deck every half minute and stamps it anew, yet the words rarely
    // change; a transcript of thousands of lines is only rebuilt when they did.
    const key = JSON.stringify(res.entries);
    if (key === state.wordIndexKey && state.wordIndex) return;
    state.wordEntries = res.entries;
    state.wordIndex = SHISUKO_WORDS.buildIndex(res.entries, knownList(state.settings));
    state.wordIndexSerial++;
    state.wordIndexKey = key;
    refreshWordMarks();
  }

  function logWordIndexError(error) {
    const now = Date.now();
    if (now - state.lastWordIndexLog < WORD_INDEX_LOG_MS) return;
    state.lastWordIndexLog = now;
    console.debug("Shisu-ko: word colours:", error || "unknown error");
  }

  // ------------------------------------------------------------ known words

  // The viewer's own known words, as the setting holds them: one per line, trimmed, blank lines
  // out, each once, in the order written. Pure.
  function knownList(settings) {
    const raw = settings && typeof settings.knownWords === "string" ? settings.knownWords : "";
    const seen = new Set();
    for (const line of raw.split("\n")) {
      const word = line.trim();
      if (word) seen.add(word);
    }
    return [...seen];
  }

  // The index again from the deck's last entries, for a known list that changed: the deck is
  // the same, so the background is not asked, and the new serial dates every look; the refresh
  // then matches only the lines holding a word the two indexes disagree on. Without a deck
  // answer in hand the index holds the list alone (see pollWordIndex), which the first answer
  // with entries replaces.
  function rebuildWordIndex() {
    state.wordIndex = SHISUKO_WORDS.buildIndex(state.wordEntries || [], knownList(state.settings));
    state.wordIndexSerial++;
  }

  // The caret a point on the page falls at: the node and the offset in it, from Firefox's
  // caretPositionFromPoint or Chrome's caretRangeFromPoint; null without either, or off the page.
  function caretAt(x, y) {
    try {
      if (typeof document.caretPositionFromPoint === "function") {
        const pos = document.caretPositionFromPoint(x, y);
        return pos && pos.offsetNode ? { node: pos.offsetNode, offset: Number(pos.offset) || 0 } : null;
      }
      if (typeof document.caretRangeFromPoint === "function") {
        const range = document.caretRangeFromPoint(x, y);
        return range && range.startContainer ? { node: range.startContainer, offset: Number(range.startOffset) || 0 } : null;
      }
    } catch (err) {
      // A point outside the viewport throws in some browsers: no word there either.
    }
    return null;
  }

  function hasClass(el, name) {
    const cls = typeof el.className === "string" ? el.className : "";
    return cls.split(/\s+/).includes(name);
  }

  // Whether `node` is `root` or inside it; walks up itself, so any node will do.
  function within(root, node) {
    if (!root) return false;
    for (let n = node; n; n = n.parentNode) if (n === root) return true;
    return false;
  }

  // What renderText() drew around `node`: the element it drew in (the line on screen, or a
  // transcript line's text), the cue it drew there, and the word span holding the node when one
  // does. Null for a node elsewhere: a sentinel, a time stamp, YouTube's own text.
  function drawnAround(node) {
    let span = null;
    for (let n = node; n; n = n.parentNode) {
      if (n.nodeType !== 1) continue;
      if (hasClass(n, "shisuko-word")) span = n;
      if (n === state.subText) return { el: n, cue: cueById(state.activeCueId), span };
      if (hasClass(n, "shisuko-linetext")) {
        const line = n.parentNode;
        return { el: n, cue: line && line.dataset ? cueById(line.dataset.id) : null, span };
      }
    }
    return null;
  }

  // The offset in the drawn text of a caret at `offset` in `node`, one of the element's own
  // nodes: a text node, a word span (the offset then counts its children), the span's text node,
  // or the element itself (the caret between two of its children). Null for any other node.
  function offsetIn(el, node, offset) {
    const children = el.childNodes;
    let at = 0;
    if (node === el) {
      for (let i = 0; i < offset && i < children.length; i++) at += children[i].textContent.length;
      return at;
    }
    for (let i = 0; i < children.length; i++) {
      const child = children[i];
      if (child === node) return at + (child.nodeType === 1 ? (offset > 0 ? child.textContent.length : 0) : offset);
      if (child.nodeType === 1 && child.childNodes[0] === node) return at + offset;
      at += child.textContent.length;
    }
    return null;
  }

  // The piece of `text` a viewer pointing at position `pos` means, by the word boundaries in
  // `starts`: the segment holding the position and, for a single kanji, the hiragana segments
  // after it up to the first particle (ICU cuts 食べて into 食|べ|て, and 私はこれが into
  // 私|は|これ|が, where the kanji is a word of its own). Not a dictionary form, but near enough
  // for a list the popup lets the viewer edit; entryWordFor() looks for the deck's own word first.
  function segmentAt(text, starts, pos) {
    if (!text) return null;
    const at = Math.max(0, Math.min(pos, text.length - 1));
    const bounds = [...new Set([0, text.length, ...starts])].filter((i) => i >= 0 && i <= text.length).sort((a, b) => a - b);
    let k = 0;
    while (k + 1 < bounds.length - 1 && bounds[k + 1] <= at) k++;
    const start = bounds[k];
    let end = bounds[k + 1];
    if (end - start === 1 && KANJI_RE.test(text[start])) {
      const particles = particleStarts(text, starts);
      for (let j = k + 1; j + 1 < bounds.length; j++) {
        const piece = text.slice(bounds[j], bounds[j + 1]);
        if (!piece || particles.has(bounds[j]) || ![...piece].every((ch) => SHISUKO_WORDS.isHiragana(ch))) break;
        end = bounds[j + 1];
      }
    }
    return { start, end, text: text.slice(start, end) };
  }

  // Where the matcher finds a particle in `text`, counting the particles as known with no deck:
  // the okurigana ICU cut off a kanji (食|べ|て, 走|っ|た) ends there, while a noun's particle
  // (私|は, 猫|が, 本|を, 前|から) is no okurigana at all. The matcher's own guard keeps the
  // inflection after the okurigana (the て of 食べて) and the pieces of a verb ICU cut up in a
  // particle's shape out of it, so this asks it rather than the list of particles.
  function particleStarts(text, starts) {
    const found = new Set();
    let at = 0;
    for (const run of SHISUKO_WORDS.markWords(text, SHISUKO_WORDS.buildIndex([], []), starts, { particles: true })) {
      if (run.status) found.add(at);
      at += run.text.length;
    }
    return found;
  }

  // The deck's or the list's own word for `piece`, a word span's text or an ICU segment: the
  // piece itself when the index holds it; else the word whose stem the piece begins with and
  // continues in hiragana (食べる for 食べた, 来る for 来てます, 勉強する for 勉強して; not 食う for
  // 食事, whose 事 continues no verb), or an exact word the piece begins with the same way (勉強
  // for 勉強している), the longer span winning and the exact word a tie. So the list gets the form
  // the deck knows, whichever form the line holds. Null when the index has nothing there.
  function entryWordFor(piece) {
    const index = state.wordIndex;
    if (!index || !index.size) return null;
    if (index.exact.has(piece)) return piece;
    let found = null;
    let span = 0;
    for (let len = Math.min(index.maxStemLen, piece.length - 1); len >= 1; len--) {
      const list = index.stems.get(piece.slice(0, len));
      if (!list || !list.length || !SHISUKO_WORDS.isHiragana(piece[len])) continue;
      found = list[0].entry.word;
      span = len + 1;
      break;
    }
    for (let len = Math.min(index.maxLen, piece.length - 1); len >= Math.max(1, span); len--) {
      const entry = index.exact.get(piece.slice(0, len));
      if (entry && SHISUKO_WORDS.isHiragana(piece[len])) return entry.word;
    }
    return found;
  }

  // A word for the known list: one line, trimmed, at most the index's word length, and with a
  // letter or digit in it (a full stop or a space is no word). Null otherwise.
  function knownWordFrom(text) {
    const word = String(text || "").trim();
    if (!word || word.includes("\n") || word.length > SHISUKO_WORDS.MAX_WORD_LEN) return null;
    return /[\p{L}\p{N}]/u.test(word) ? word : null;
  }

  // The word Alt+Shift+K is about: the text selected inside the subtitle box or the transcript,
  // when there is one (Yomitan selects the text it scanned while its popup is up); else the run
  // under the pointer where it was last seen over the player: a word span's whole text, or the
  // ICU segment of plain text there, and for an honorific prefix the word it fronts. Either is
  // replaced by the deck's own word when the index knows the form (entryWordFor). Null for
  // nothing usable, and for a pointer that has left the player (onPlayerMouseLeave): the line
  // that was under it has moved on since, or the transcript has scrolled, and what is at that
  // place now is no word the viewer pointed at. A hover pause keeps it: the video waits for the
  // pointer, gone to a dictionary popup, and the line stays where it was.
  function knownTarget() {
    const selected = selectedText();
    if (selected) return entryWordFor(selected) || selected;
    if (!state.pointerInPlayer && !state.hoverPaused) return null;
    const { x, y } = state.lastPointer;
    const caret = caretAt(x, y);
    if (!caret) return null;
    const drawn = drawnAround(caret.node);
    if (!drawn || !drawn.cue) return null;
    const text = drawn.cue.text;
    const gap = offsetIn(drawn.el, caret.node, caret.offset);
    if (gap === null) return null;
    const on = characterUnder(drawn.el, gap, x, y);
    const span = on ? on.span : drawn.span;
    let piece = null;
    if (span) {
      const word = HONORIFICS.has(span.textContent) ? wordAfter(drawn.el, span) : null;
      piece = (word || span).textContent;
    } else {
      const look = state.cueLooks.get(drawn.cue);
      const starts = look ? look.starts : SHISUKO_WORDS.wordStarts(text);
      let segment = segmentAt(text, starts, on ? on.index : gap);
      if (segment && HONORIFICS.has(segment.text) && segment.end < text.length) segment = segmentAt(text, starts, segment.end);
      piece = segment ? segment.text : null;
    }
    const word = knownWordFrom(piece);
    return word ? entryWordFor(word) || word : null;
  }

  // The character the pointer at (x, y) is on, for a caret at `gap` in what renderText() drew in
  // `el`: its index and the word span holding it (null for plain text). The caret APIs answer the
  // gap between two characters nearest the point, which over the right half of a character is
  // the gap after it, so the character before the gap is the one when its box holds the point,
  // else the one after (Yomitan checks the character's box for the same reason). Null when
  // neither box holds it (the pointer between two rows, past the text's end) or there are no
  // boxes to ask: the gap is then read as the character after it.
  function characterUnder(el, gap, x, y) {
    if (typeof document.createRange !== "function") return null;
    try {
      for (const index of [gap - 1, gap]) {
        const at = drawnCharacter(el, index);
        if (!at) continue;
        const range = document.createRange();
        range.setStart(at.node, at.offset);
        range.setEnd(at.node, at.offset + 1);
        const box = range.getBoundingClientRect();
        if (box && x >= box.left && x <= box.right && y >= box.top && y <= box.bottom) return { index, span: at.span };
      }
    } catch (err) {
      // A node gone from the page since the caret was read: no box, and the gap as it came.
    }
    return null;
  }

  // The text node holding the character at `index` of what renderText() drew in `el`, the
  // character's offset in it, and the word span around it (null for plain text). Null outside
  // the text.
  function drawnCharacter(el, index) {
    if (index < 0) return null;
    const children = el.childNodes;
    let at = 0;
    for (let i = 0; i < children.length; i++) {
      const child = children[i];
      const length = child.textContent.length;
      if (index < at + length) {
        if (child.nodeType === 3) return { node: child, offset: index - at, span: null };
        const inner = child.childNodes[0];
        return inner && inner.nodeType === 3 ? { node: inner, offset: index - at, span: child } : null;
      }
      at += length;
    }
    return null;
  }

  // The word span right after `span` in `el`, for an honorific prefix drawn in a span of its own
  // before the word it fronts ([お][風呂]); null when plain text or nothing follows.
  function wordAfter(el, span) {
    const children = el.childNodes;
    for (let i = 0; i + 1 < children.length; i++) {
      if (children[i] !== span) continue;
      const next = children[i + 1];
      return next.nodeType === 1 && hasClass(next, "shisuko-word") ? next : null;
    }
    return null;
  }

  function selectedText() {
    try {
      const sel = typeof document.getSelection === "function" ? document.getSelection() : null;
      if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
      const node = sel.getRangeAt(0).commonAncestorContainer;
      if (!within(state.subBox, node) && !within(state.transcriptList, node)) return null;
      return knownWordFrom(sel.toString());
    } catch (err) {
      return null;
    }
  }

  // Alt+Shift+K: the word under the pointer (or selected) goes on the known list, or comes off
  // it again; the list is saved and every tab's lines follow through the storage listener.
  function markKnown() {
    const word = knownTarget();
    if (!word) {
      showToast("No word under the pointer", "warn");
      return;
    }
    const list = knownList(state.settings);
    const at = list.indexOf(word);
    // A particle never goes into the index (buildIndex drops it, as it drops a card for one): on
    // the list it would colour nothing, and the toast would claim what no line shows. One that is
    // on the list already (typed in the popup) still comes off it.
    if (at < 0 && !SHISUKO_WORDS.buildIndex([], [word]).size) {
      showToast(`${word} is a particle: the "Particles count as known" switch decides its colour`, "warn");
      return;
    }
    if (at >= 0) list.splice(at, 1);
    else list.push(word);
    saveSettings({ knownWords: list.join("\n") }).then((res) => {
      if (res && res.ok === false) showToast("Known words not saved: " + (res.error || "unknown error"), "error", 6000);
    });
    showToast(at >= 0 ? `${word} is no longer marked as known` : `${word} marked as known`, "ok");
  }

  // ------------------------------------------------------------ sentence mining

  function currentCueForMining() {
    if (state.activeCueId !== null) {
      const active = cueById(state.activeCueId);
      if (active) return active;
    }
    const t = playhead();
    let best = null;
    for (const c of state.cues) {
      if (c.start <= t + 0.5 && c.end >= t - MINE_RECENT_WINDOW_S && (!best || c.start > best.start)) best = c;
    }
    return best;
  }

  // The spoken sentence a mined card gets: the cue that was on screen, and nothing more.
  //
  // The server marks every cue it split out of one Whisper segment with a shared `seg`, and this
  // used to rejoin them, on the premise that a segment is a sentence. It is not. A narrator who
  // reads without pausing gets almost no punctuation out of Whisper, so `group_words` splits on
  // the cue length limit alone, and rejoining those pieces hands the card clauses the viewer
  // never saw and seconds they never heard. The VAD is no better a witness: Silero cuts at 300 ms
  // of silence, which is a breath, and such a narrator runs ten seconds and three cues between
  // breaths -- deferring to it makes the card longer still. Neither signal marks a sentence.
  //
  // So the line the viewer read and heard is the one thing certainly true of the card, and the
  // clip bounds follow from it. Pure: a cue in, a sentence out, in the shape the callers and the
  // background's clipParams() expect.
  function sentenceForCue(cue) {
    if (!cue) return null;
    return { start: cue.start, end: cue.end, text: cue.text, cueIds: [cue.id] };
  }

  // The sentence after this one, so its audio can be fetched before it is spoken. Pure: the cue
  // list and a sentence in, the sentence starting at the next cue after it out, or null at the end.
  function nextSentence(cues, sentence) {
    const list = cues || [];
    const ids = sentence && Array.isArray(sentence.cueIds) ? sentence.cueIds : [];
    if (!ids.length) return null;
    let last = -1;
    for (let i = 0; i < list.length; i++) {
      if (list[i] && ids.indexOf(list[i].id) >= 0 && i > last) last = i;
    }
    if (last < 0) return null;
    const next = list[last + 1];
    return next ? sentenceForCue(next) : null;
  }

  // One canvas for every frame read back: a backing store the size of the frame per line would be
  // megabytes of garbage a minute. A DRM-protected video taints it for good (every later read
  // would fail, on every later video), so a failed read lets it go and the next one starts afresh.
  let frameCanvas = null;

  // The video's frame drawn onto the shared canvas, sized to it (1280 px wide at most), or null
  // when the video has no frame yet. Setting the size clears what the last read left.
  function drawFrame(video) {
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) return null;
    const scale = Math.min(1, 1280 / w);
    const canvas = frameCanvas || (frameCanvas = document.createElement("canvas"));
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  function captureFrame(video) {
    if (!video) return null;
    try {
      const canvas = drawFrame(video);
      return canvas ? canvas.toDataURL("image/jpeg", 0.85) : null;
    } catch (err) {
      frameCanvas = null;
      return null; // DRM-protected streams taint the canvas
    }
  }

  // ---- pre-mining: prepare every sentence that plays, before any card exists ----

  // Where a cue sits in what the background holds ready, Infinity when it holds nothing for it.
  // The newest sentence ranks first, so a card that could belong to two identical lines is given
  // the one the viewer just read. Pure: the held list and a cue in, an order out.
  function rankOfCue(held, cue) {
    const list = held || [];
    for (let i = 0; i < list.length; i++) {
      const ids = list[i] && list[i].cueIds;
      if (Array.isArray(ids) && cue && ids.indexOf(cue.id) >= 0) return i;
    }
    return Infinity;
  }

  function heldHas(key, what) {
    return state.premined.some((entry) => entry.key === key && entry[what]);
  }

  // Preparing material costs a frame read and a clip request, so it is spent only where a card can
  // follow: the add-on on, a video open, the server reachable, the tab in front, no ad running.
  function premineAllowed() {
    return !!(
      state.settings.enabled &&
      state.videoId &&
      state.video &&
      !state.offline &&
      !state.standby && // another tab has the server: a clip request would only be refused
      document.visibilityState === "visible" &&
      !isAdPlaying()
    );
  }

  // Premine and reset messages go out one at a time, each sent once the one before was answered.
  // The background files a sentence only after an await of its own (its settings read), so a reset
  // fired while a premine was out could be handled first, and the old video's sentence would stay
  // in the store, listed in every later inventory under a key the new video's first lines share.
  // `send` runs when the turn comes and may return null to send nothing after all.
  function premineTurn(send) {
    const turn = state.premineQueue.then(send);
    state.premineQueue = turn.then(() => undefined, () => undefined);
    return turn;
  }

  async function sendPremine(sentence, extra) {
    const generation = state.cueGeneration;
    const msg = Object.assign(
      {
        type: "premine",
        videoId: state.videoId,
        key: sentence.cueIds[0],
        cueIds: sentence.cueIds,
        sentence: { start: sentence.start, end: sentence.end, text: sentence.text },
      },
      extra || {}
    );
    state.premineInFlight++;
    let res = null;
    try {
      // A sentence whose cues were dropped while it waited for its turn is not sent at all.
      res = await premineTurn(() => (generation === state.cueGeneration ? sendMessage(msg) : null));
    } finally {
      state.premineInFlight--;
    }
    // The inventory answered is the tab's as of this message. Another video, or this one's cues
    // started over, while it was out: those keys are the new cues' ids (they restart at 0), and
    // taking them would make the new video's first lines count as prepared with the old one's
    // material. The reset queued behind this message drops them from the store.
    if (res && res.ok && Array.isArray(res.held) && generation === state.cueGeneration) state.premined = res.held;
    return res;
  }

  function clearPremineTimer() {
    if (state.premineTimer) {
      clearTimeout(state.premineTimer);
      state.premineTimer = null;
    }
  }

  function resetPremine() {
    clearPremineTimer();
    // Nothing held and nothing out means nothing to drop, and with the master switch off nothing
    // is ever held: a switched-off add-on sends no messages at all.
    if (state.premined.length || state.premineInFlight) premineTurn(() => sendMessage({ type: "premineReset" }));
    state.premined = [];
  }

  // A new line is on screen. Wait out the delay before touching the GPU, then prepare this
  // sentence and ask for the next one's audio, so a lookup on either is already paid for.
  function schedulePremine(cue) {
    clearPremineTimer();
    if (!cue || !premineAllowed()) return;
    const sentence = sentenceForCue(cue);
    if (!sentence) return;
    const key = sentence.cueIds[0];
    state.premineTimer = setTimeout(() => {
      state.premineTimer = null;
      premineNow(key);
    }, PREMINE_CAPTURE_DELAY_MS);
  }

  // Can a card be mined by itself: automatic mining on, and into Anki (to the Downloads folder
  // nothing is ever mined without the viewer asking). Pure given the settings.
  function autoAnkiMining() {
    const s = state.settings;
    return !!s.autoMine && s.mineTarget === "anki";
  }

  async function premineNow(key) {
    if (!premineAllowed()) return;
    const active = cueById(state.activeCueId);
    const sentence = active ? sentenceForCue(active) : null;
    if (!sentence || sentence.cueIds[0] !== key) return; // the line moved on while we waited
    // A held frame is read by automatic mining alone: a mine the viewer asks for captures the frame
    // on screen. Otherwise the readback and the few hundred kilobytes shipped per line would be
    // dead weight, so only the clip is prepared, which a manual mine does reuse.
    const frame = autoAnkiMining();
    // Another video, or this one's cues started over, while a frame was read back or a message
    // was out: the sentence and the frame are the old cues', and the new ones share their ids.
    const generation = state.cueGeneration;
    if (frame ? !heldHas(key, "image") : !heldHas(key, "audio")) {
      const imageDataUrl = frame ? await captureFrameAsync(state.video) : null;
      if (generation !== state.cueGeneration || !premineAllowed()) return;
      await sendPremine(sentence, frame ? { imageDataUrl } : {});
      if (generation !== state.cueGeneration) return;
    }
    const next = nextSentence(state.cues, sentence);
    if (next && !heldHas(next.cueIds[0], "audio")) await sendPremine(next, { ahead: true });
  }

  function captureHoverFrame() {
    // The frame to attach is the one on screen when the viewer hovered the line, but reading a
    // video frame back from the GPU stalls the main thread, so wait until Yomitan's scan has run
    // and encode off the main thread. It replaces the frame taken when the line appeared: this is
    // the one the viewer was looking at, and hovering pins the sentence against eviction.
    clearHoverCapture();
    const id = state.activeCueId;
    if (id === null || !premineAllowed() || !autoAnkiMining()) return; // the frame is for automatic mining
    state.hoverCaptureTimer = setTimeout(() => {
      state.hoverCaptureTimer = null;
      if (state.activeCueId !== id || !premineAllowed()) return;
      const sentence = sentenceForCue(cueById(id));
      if (!sentence) return;
      const video = state.video;
      const key = sentence.cueIds[0];
      const t = video.currentTime;
      // The pointer crosses the box edge for every word looked up in the popup, and the first
      // hover paused the video: every crossing would read the same frame back and ship it again,
      // when the background already holds this sentence's frame from this very position.
      const shot = state.hoverShot;
      if (shot && shot.key === key && shot.t === t && video.paused && heldHas(key, "image")) return;
      const generation = state.cueGeneration; // the same id on another video is another line
      captureFrameAsync(video).then((imageDataUrl) => {
        if (!imageDataUrl || state.activeCueId !== id || generation !== state.cueGeneration) return;
        state.hoverShot = { key, t };
        sendPremine(sentence, { imageDataUrl, hover: true });
      });
    }, HOVER_CAPTURE_DELAY_MS);
  }

  function clearHoverCapture() {
    if (state.hoverCaptureTimer) {
      clearTimeout(state.hoverCaptureTimer);
      state.hoverCaptureTimer = null;
    }
  }

  // A line read in the transcript panel is looked up there, so its sentence is ranked first, the
  // way a hover on the subtitle ranks the line on screen: a card cut from it whose sentence is
  // also a piece of the playing line is separated from that line by rank alone (matchCue), and
  // without this it would go to the playing line. No frame goes with it: the panel is not the
  // video, and the background keeps the frame it already holds for the sentence when the message
  // brings none. Sent once the pointer has rested on the line as long as the subtitle hover waits:
  // scrolling the panel sweeps the pointer across lines, each of which would cost a clip request.
  function onTranscriptLineEnter(ev) {
    if (!ev.isTrusted) return; // see onMineClick
    clearTranscriptHover();
    if (!premineAllowed()) return; // with the master switch off nothing is sent
    const line = ev.currentTarget;
    const cue = cueById(line && line.dataset.id);
    if (!cue) return;
    const generation = state.cueGeneration; // the same id on another video is another line
    state.transcriptHoverTimer = setTimeout(() => {
      state.transcriptHoverTimer = null;
      if (generation !== state.cueGeneration || !premineAllowed()) return;
      const sentence = sentenceForCue(cue);
      if (sentence) sendPremine(sentence, { hover: true });
    }, HOVER_CAPTURE_DELAY_MS);
  }

  // Leaving the line sends nothing; a line the pointer only crossed is not ranked either.
  function onTranscriptLineLeave(ev) {
    if (!ev.isTrusted) return;
    clearTranscriptHover();
  }

  function clearTranscriptHover() {
    if (state.transcriptHoverTimer) {
      clearTimeout(state.transcriptHoverTimer);
      state.transcriptHoverTimer = null;
    }
  }

  function captureFrameAsync(video) {
    return new Promise((resolve) => {
      try {
        const canvas = drawFrame(video);
        if (!canvas) return resolve(null);
        // toBlob copies the bitmap before it returns, so the canvas is free for the next read.
        canvas.toBlob((blob) => {
          if (!blob) return resolve(null);
          const reader = new FileReader();
          reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : null);
          reader.onerror = () => resolve(null);
          reader.readAsDataURL(blob);
        }, "image/jpeg", 0.85);
      } catch (err) {
        frameCanvas = null;
        resolve(null); // DRM-protected streams taint the canvas
      }
    });
  }

  function seekTo(video, t) {
    // Setting currentTime to the position the media is already at fires no "seeked" event, so
    // the wait below would only end at the fallback timeout. Nothing to do in that case.
    if (Math.abs((Number(video.currentTime) || 0) - t) < 0.05) return Promise.resolve();
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        video.removeEventListener("seeked", finish);
        setTimeout(resolve, 80); // let the new frame paint before we grab it
      };
      video.addEventListener("seeked", finish);
      video.currentTime = t;
      setTimeout(finish, 1500);
    });
  }

  async function mineCue(cue, options) {
    if (!cue || state.mining || !state.video || !state.videoId) return;
    const opts = options || {};
    state.mining = true;
    showToast(opts.auto ? "Attaching to the new card…" : "Mining…", "info", 15000);
    try {
      const video = state.video;
      const videoId = state.videoId;
      const generation = state.cueGeneration;
      const sentence = sentenceForCue(cue);
      const key = sentence ? sentence.cueIds[0] : null;
      // The viewer asking for this line gets the frame on screen now. An automatic mine takes the
      // pre-mined frame when there is one: it is the frame that was up while the line was read,
      // and by now the video has moved on. Only a line still on screen, or one worth seeking
      // back to, is captured afresh.
      const capture = !opts.auto || (!heldHas(key, "image") && (cue.id === state.activeCueId || !!opts.seekForFrame));
      // The viewer clicked on to another video while a seek was pending (up to 1.5 s when the
      // media was swapped under it): the cue and its times are the old video's, and the card would
      // get a clip cut from the new one at them. YouTube keeps the element across videos, so the
      // position to restore is the old video's too, and the seek is left where the new one landed.
      const moved = () => generation !== state.cueGeneration;
      let restore = null;
      if (capture && opts.seekForFrame) {
        restore = { t: video.currentTime, paused: video.paused };
        video.pause();
        await seekTo(video, Math.min(cue.end, cue.start + 0.4) - (state.live ? state.liveOffset : 0));
        if (moved()) {
          showToast("Nothing mined: the video changed", "warn");
          return;
        }
      }
      const imageDataUrl = capture ? captureFrame(video) : null;
      if (restore) {
        await seekTo(video, restore.t);
        if (moved()) {
          showToast("Nothing mined: the video changed", "warn");
          return;
        }
        if (!restore.paused) video.play().catch(() => {});
      }
      // Automatic mining leaves playback alone: the viewer is probably still in Yomitan's popup.
      if (!opts.auto) resumeAfterMining(video);
      const result = await sendMessage({
        type: "mine",
        videoId,
        key,
        cue: { start: cue.start, end: cue.end, text: cue.text },
        sentence: sentence ? { start: sentence.start, end: sentence.end, text: sentence.text } : null,
        imageDataUrl,
        noteId: opts.noteId,
        auto: !!opts.auto,
      });
      if (result && result.ok) {
        showToast(result.message || "Mined", result.warning ? "warn" : "ok");
        // The card the word colours are about was just made (or, with no deck chosen, the first
        // one, which tells the background the deck): the next ask goes out soon, not at the interval.
        state.wordIndexAskedAt = Date.now() - WORD_INDEX_REFRESH_MS + WORD_INDEX_MINE_DELAY_MS;
      } else if (result && result.mismatch) showToast(result.error, "warn", 6000);
      else showToast("Mining failed: " + ((result && result.error) || "unknown error"), "error", 6000);
    } catch (err) {
      showToast("Mining failed: " + String((err && err.message) || err), "error", 6000);
    } finally {
      state.mining = false;
    }
  }

  function resumeAfterMining(video) {
    // The frame is captured; keep watching while the server cuts the audio clip. Only a pause
    // caused by hovering the subtitle is undone here; a pause the viewer chose stays.
    if (!state.hoverPaused || !video || !video.paused) return;
    state.hoverPaused = false;
    state.awaitingPlayerMove = false;
    clearResumeTimer();
    video.play().catch(() => {});
  }

  // Is anyone plausibly mining right now? Polling costs a message and an AnkiConnect request per
  // second, so it is spent only where a card can appear: a visible tab, playing, or paused with the
  // viewer at the subtitle. A video left paused in a visible tab for two minutes is not being read.
  function ankiPollAllowed() {
    const s = state.settings;
    // To the Downloads folder nothing is mined by itself: the background would answer every
    // message with no card, and the message is a round trip across processes. A standby tab has
    // no server to mine against: another tab holds it.
    if (!s.enabled || !autoAnkiMining() || state.offline || state.standby) return false;
    // Nothing transcribed yet means nothing a new card could be given.
    if (!state.videoId || !state.cues.length) return false;
    if (document.visibilityState !== "visible" || isAdPlaying()) return false;
    const now = Date.now();
    // A hover pause is a lookup in progress, the pointer on the subtitle or gone from the player
    // towards the popup, and it ends only when the pointer crosses the player again. Given the
    // same two minutes at the hover rate, from the pointer's last appearance rather than from the
    // pause, and a slow heartbeat after them: a viewer who parked the pointer on the sidebar and
    // left would otherwise be polled for, at the hover rate, for hours, while one still reading
    // the popup, whose pointer the page cannot see, must not lose the card they make to a gap the
    // background no longer trusts its baseline across (PAUSE_POLL_SLOW_MS).
    if (state.hoverPaused || state.awaitingPlayerMove) {
      if (now - state.hoverSeenAt < PAUSE_POLL_IDLE_MS) return true;
      return now - state.lastAnkiPollAt >= PAUSE_POLL_SLOW_MS;
    }
    return !state.pausedSince || now - state.pausedSince < PAUSE_POLL_IDLE_MS;
  }

  // Ask the background whether Yomitan just created a card.
  async function pollForNewCard() {
    if (state.mining || state.ankiPollInFlight) return;
    if (!ankiPollAllowed()) return;
    state.ankiPollInFlight = true;
    state.lastAnkiPollAt = Date.now();
    let res;
    try {
      res = await sendMessage({ type: "ankiPoll" });
    } finally {
      state.ankiPollInFlight = false;
    }
    if (!res) return;
    // Anki being closed or not having granted access is normal; it must not raise toasts.
    if (!res.ok) logAnkiPollError(res.error);
    else if (res.newNoteId) autoMine(res.newNoteId, res.note, !!res.replayed);
  }

  function logAnkiPollError(error) {
    const now = Date.now();
    if (now - state.lastAnkiPollLog < ANKI_POLL_LOG_MS) return;
    state.lastAnkiPollLog = now;
    console.debug("Shisu-ko: Anki watch:", error || "unknown error");
  }

  // A card is matched to the line it is about, not assumed to be about the line playing now: by
  // the time Yomitan has written the note the video has moved on, and with pause-on-hover off it
  // has moved on by several lines. Only a card with neither sentence nor word to go on falls back
  // to the playhead. Sentences already prepared rank first, so two identical lines resolve to the
  // one the viewer just read. A `replayed` note is one the background found for another tab's
  // poll and hands to every tab (its ledger): it may well be about a line of another video, so
  // a line of this one matching its sentence is the only reason to act on it, and no match is
  // no news to this tab: no toast, and never the line at this tab's playhead.
  function autoMine(noteId, note, replayed) {
    const written = note ? SHISUKO_MATCH.normalize(note.sentence) : "";
    const word = note ? SHISUKO_MATCH.normalize(note.word) : "";
    let cue;
    if (written || word) {
      cue = SHISUKO_MATCH.matchCue(state.cues, note, { rank: (c) => rankOfCue(state.premined, c), t: playhead() });
      if (!cue) {
        if (!replayed) showToast("New card's sentence matches no subtitle; nothing attached", "warn");
        return;
      }
    } else if (replayed) {
      return; // the ledger only carries cards with a sentence; nothing to match is nothing to do
    } else {
      cue = currentCueForMining();
      if (!cue) {
        showToast("New card detected but no subtitle to attach", "warn");
        return;
      }
    }
    const seekForFrame = cue.id !== state.activeCueId && !!(state.video && state.video.paused);
    mineCue(cue, { seekForFrame, noteId, auto: true });
  }

  function mineCurrent() {
    const cue = currentCueForMining();
    if (!cue) {
      showToast("No subtitle at this position to mine", "error");
      return;
    }
    mineCue(cue, { seekForFrame: false });
  }

  // ------------------------------------------------------------ pause while hovering

  function onSubtitleEnter(ev) {
    if (!ev.isTrusted) return; // see onMineClick
    clearResumeTimer();
    state.awaitingPlayerMove = false;
    state.hoverSeenAt = Date.now();
    captureHoverFrame();
    if (!state.settings.pauseOnHover || !state.video) return;
    if (!state.video.paused && !state.video.ended) {
      state.video.pause();
      state.hoverPaused = true;
    }
  }

  function onSubtitleLeave(ev) {
    if (!ev.isTrusted) return;
    clearHoverCapture();
    state.hoverSeenAt = Date.now();
    if (!state.hoverPaused) return;
    const related = ev.relatedTarget;
    // Leaving towards a dictionary popup (an iframe, or anything that is not part of the
    // player) keeps the video paused until the pointer comes back over the video.
    if (!related || related.tagName === "IFRAME" || !isYouTubeElement(related)) {
      state.awaitingPlayerMove = true;
      return;
    }
    scheduleResume();
  }

  function onPlayerMouseMove(ev) {
    // Mutated, not replaced: this runs on every mouse move across the player.
    state.lastPointer.x = ev.clientX;
    state.lastPointer.y = ev.clientY;
    state.pointerInPlayer = true;
    state.hoverSeenAt = Date.now();
    if (!state.hoverPaused || !state.awaitingPlayerMove) return;
    if (state.subBox && state.subBox.contains(ev.target)) return;
    if (!isYouTubeElement(ev.target)) return;
    state.awaitingPlayerMove = false;
    scheduleResume();
  }

  // The pointer has left the player (the transcript panel is part of it): its last position
  // stays for the resume (scheduleResume), but no longer says what the viewer points at.
  function onPlayerMouseLeave() {
    state.pointerInPlayer = false;
  }

  function scheduleResume() {
    clearResumeTimer();
    state.resumeTimer = setTimeout(() => {
      state.resumeTimer = null;
      if (!state.hoverPaused || !state.video) return;
      if (state.subBox && state.subBox.matches(":hover")) return;
      const under = document.elementFromPoint(state.lastPointer.x, state.lastPointer.y);
      if (under && (under.tagName === "IFRAME" || !isYouTubeElement(under))) {
        state.awaitingPlayerMove = true;
        return;
      }
      state.hoverPaused = false;
      state.video.play().catch(() => {});
    }, RESUME_DELAY_MS);
  }

  function clearResumeTimer() {
    if (state.resumeTimer) {
      clearTimeout(state.resumeTimer);
      state.resumeTimer = null;
    }
  }

  // ------------------------------------------------------------ keyboard commands

  browser.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.type !== "command") return undefined;
    // Off means nothing happens on a YouTube page; only the switch itself keeps working. The cues
    // outlive the switch (applySettings only hides the overlay), so a mine would still find one and
    // write a card, and the transcript toggle would flip a setting nobody can see.
    if (!state.settings.enabled && msg.name !== "toggle-subtitles") return undefined;
    if (msg.name === "toggle-subtitles") saveSettings({ enabled: !state.settings.enabled });
    else if (msg.name === "toggle-transcript") saveSettings({ showTranscript: !state.settings.showTranscript });
    else if (msg.name === "mine-current") mineCurrent();
    else if (msg.name === "mark-known") markKnown();
    else if (msg.name === "toggle-status") toggleStatusBadge();
    return undefined;
  });

  // Alt+Shift+H: the status badge in the player's top left off, and on again. A setting, so that
  // the popup shows it and every tab follows through the storage listener; the toast says which
  // way it went, since the badge itself is what goes away.
  function toggleStatusBadge() {
    const show = state.settings.statusBadge === false;
    saveSettings({ statusBadge: show }).then((res) => {
      if (res && res.ok === false) showToast("Not saved: " + (res.error || "unknown error"), "error", 6000);
    });
    showToast(show ? "Status badge shown" : "Status badge hidden: the same shortcut or the popup shows it again", "info");
  }

  // ------------------------------------------------------------ start

  loadSettings().then(() => {
    discover();
    timers.push(setInterval(discover, DISCOVER_INTERVAL_MS));
    timers.push(setInterval(syncTick, SYNC_INTERVAL_MS));
    timers.push(setInterval(render, RENDER_INTERVAL_MS));
    timers.push(setInterval(() => {
      if (state.hoverPaused || state.awaitingPlayerMove) pollForNewCard();
    }, HOVER_POLL_INTERVAL_MS));
    window.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("yt-navigate-finish", () => {
      // YouTube swaps the player on navigation: the cached element must be looked up again.
      state.rediscover = true;
      setTimeout(discover, 50);
    });
    document.addEventListener("fullscreenchange", () => setTimeout(updateFontSize, 100));
  });
})();
