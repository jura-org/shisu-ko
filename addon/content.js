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
 *  - sentence mining is the server's: it attaches a line's audio and picture to the card Yomitan
 *    made from it. This script sends the popup's Anki settings with every /sync, the viewer's own
 *    mine (Alt+Shift+M, a line's button) with the next one, and toasts what the server reports
 */

(() => {
  if (window.__shisukoLoaded) return;
  window.__shisukoLoaded = true;
  // A reloaded or updated extension leaves this instance orphaned. Chrome keeps an orphan running
  // with a dead runtime, and it tears itself down (see runtimeAlive()); Firefox kills it without
  // running another line, so its overlay stays behind for the fresh instance to remove
  // (ensureOverlay()).
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
  const RESUME_DELAY_MS = 350;
  const TOAST_MS = 3500;
  const TOAST_MAX_CHARS = 240;
  // The status line is one line over the video; a server error can carry a traceback or a URL.
  const STATUS_ERROR_MAX_CHARS = 160;
  const STATUS_NAME_MAX_CHARS = 100;
  const MINE_RECENT_WINDOW_S = 6;
  // A mine the server has not answered by then is given up: it waits up to a minute for audio
  // still being fetched, and a server older than 0.22.0 never answers one at all.
  const MINE_TIMEOUT_MS = 90000;
  // The deck index behind the word colours is asked for this often. The background answers from
  // the rows the server sent with the last /sync, so most asks come back "unchanged" for the price
  // of a message, and a card just made shows within seconds; a failing ask is logged this often.
  const WORD_INDEX_REFRESH_MS = 2000;
  const WORD_INDEX_LOG_MS = 60000;
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
  // Where the subtitle may sit (the subDock setting). video-bottom is the placement from before
  // the setting; the others are dockLayout()'s.
  const SUB_DOCKS = ["video-bottom", "video-top", "window-bottom", "video-left", "video-right"];
  // Gap between an outside box and the window edge (window-bottom) or the video edge (the sides).
  const DOCK_GAP_PX = 16;
  const SIDE_GAP_PX = 12;
  // The inside docks, in % of the player, as content.css places them: the top dock's gap, the
  // side columns' top and bottom (clear of the controls) and their gap from the edge.
  const DOCK_TOP_PCT = 6;
  const SIDE_BOTTOM_PCT = 12;
  const SIDE_EDGE_PCT = 2;
  // An outside side needs room for two columns of text plus the box's padding, in em; narrower,
  // the column goes inside the video at that edge. Two line-heights (1.35) and 0.25em per side.
  const SIDE_MIN_EM = 2 * 1.35 + 0.5;
  // The transcript panel's width and gap, as content.css has them: min(34%, 380px) + 16px.
  const PANEL_PCT = 34;
  const PANEL_MAX_PX = 380;
  const PANEL_GAP_PX = 16;
  // A pointer that moved less than this between press and release on the grip clicked it.
  const DRAG_THRESHOLD_PX = 4;
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
    // The viewport-fixed layer on document.body that holds the subtitle when it docks outside the
    // player (which clips), and the drag's targets.
    outside: null,
    layoutFrame: 0, // the pending requestAnimationFrame of scheduleLayout()
    drag: null, // the grip's press, then its drag (onGripDown)
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
    resumeTimer: null,
    lastPointer: { x: 0, y: 0 },
    // Whether the pointer is over the player now, that is whether lastPointer still says where it
    // is: Alt+Shift+K reads the word there (knownTarget).
    pointerInPlayer: false,
    syncInFlight: false,
    // The playhead on the video's own clock as of the last request outside an ad, or where the
    // video will start before the first: what is sent while an ad runs on its clock (see sync).
    contentPlayhead: 0,
    // The viewer's own mine until the server answers it: { cue_id, nonce, session, at, generation },
    // sent with every /sync (see mineCue), or null.
    mining: null,
    // The cursor into the server's mining log (/sync's `mined_next`); null asks where it stands.
    minedSince: null,
    ankiError: null, // the last `anki_error` the server gave, toasted once
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
    endDrag(null);
    detach();
    clearResumeTimer();
    // An orphaned instance must stop swallowing arrow keys; the fresh one owns them now.
    window.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("resize", onViewportChange);
    window.removeEventListener("scroll", onViewportChange, true);
    if (state.root) state.root.remove();
    state.root = null;
    if (state.outside) state.outside.remove();
    state.outside = null;
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

  // Whether the pointer, gone to `el`, is still on the page rather than in a dictionary popup:
  // anything of the player's (isYouTubeElement), our own layer outside it, and YouTube's page
  // around the player. Yomitan's popup is an iframe, or a host element of its own directly under
  // the body (or inside the fullscreen player, where only the player's own count).
  function isPageElement(el) {
    if (!el || el.tagName === "IFRAME") return false;
    if (isYouTubeElement(el)) return true;
    if (!state.player || state.player.contains(el)) return false;
    if (el === document.documentElement || el === document.body) return true;
    if (state.outside && state.outside.contains(el)) return true;
    return typeof el.closest === "function" && !!el.closest("ytd-app");
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
      if (state.outside) applyStyleSettings(state.outside, s);
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
    placeSubtitle();
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
    mineBtn.title = "Mine this sentence into the newest Anki card: audio + picture (Alt+Shift+M)";
    mineBtn.addEventListener("click", onMineClick);
    subBox.appendChild(mineBtn);
    // The grip moves the box to another dock; the text itself stays free for selection and Yomitan.
    const grip = document.createElement("button");
    grip.type = "button";
    grip.className = "shisuko-drag";
    grip.textContent = "⠿";
    grip.title = "Drag to move the subtitles";
    grip.addEventListener("pointerdown", onGripDown);
    // YouTube's player toggles playback on a click and starts a selection on a press: neither may
    // reach it from the grip.
    for (const type of ["mousedown", "click", "dblclick"]) grip.addEventListener(type, swallowTrusted);
    subBox.appendChild(grip);
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
    subBox.addEventListener("mousemove", onSubtitleMove);

    player.appendChild(root);
    Object.assign(state, { root, statusEl, toastEl, subWrap, subBox, subText, mineBtn, grip, transcriptEl, transcriptList: list });
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
  // card, seek or pause. The keyboard commands come through the browser, not the page.
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
    endDrag(null);
    if (state.root) state.root.remove();
    if (state.outside) state.outside.remove();
    state.outside = null;
    // A dead instance's overlay, frozen on its last cue: Firefox unloads an updated extension's
    // content script without running its shutdown(), and its DOM stays in the page, the layer
    // outside the player on the body included.
    for (const stale of document.querySelectorAll(".shisuko-root")) stale.remove();
    for (const stale of document.querySelectorAll(".shisuko-outside")) stale.remove();
    buildOverlay(player);
  }

  // ------------------------------------------------------------ subtitle dock

  function subDock(s) {
    return oneOf(s.subDock, SUB_DOCKS, DEFAULT_SETTINGS.subDock);
  }

  // Where a dock puts the subtitle. Pure: the dock, the player's and the viewport's boxes in
  // viewport px ({ left, top, width, height }; the viewport needs only the size) and
  // { confined, fontPx, bottomPct, transcript } in; { dock, outside, vertical, side, rect } out.
  // `confined` means nothing outside the player shows (fullscreen, or no video page): there a
  // window-bottom renders as video-bottom (the `dock` answered) and a side goes inside the video.
  // `transcript` is the open panel's side, or null. An outside rect is where the subwrap goes (with
  // `bottom` when it hangs from the window's bottom edge); an inside one approximates the box
  // content.css places, for the drag's targets.
  function dockLayout(dock, player, viewport, opts) {
    const d = oneOf(dock, SUB_DOCKS, "video-bottom");
    const o = opts || {};
    const font = o.fontPx > 0 ? o.fontPx : 20;
    const row = font * 1.9; // a line of text with the box's padding
    const panel = Math.min((player.width * PANEL_PCT) / 100, PANEL_MAX_PX) + PANEL_GAP_PX;
    const panelLeft = o.transcript === "left" ? panel : 0;
    const panelRight = o.transcript === "right" ? panel : 0;
    const right = player.left + player.width;
    if (d === "window-bottom" && !o.confined) {
      const width = Math.max(0, Math.min(player.width, viewport.width - 2 * DOCK_GAP_PX));
      const centred = player.left + (player.width - width) / 2;
      const left = Math.min(Math.max(centred, DOCK_GAP_PX), viewport.width - DOCK_GAP_PX - width);
      const top = viewport.height - DOCK_GAP_PX - row;
      return { dock: d, outside: true, vertical: false, side: null, rect: { left, top, width, height: row, bottom: DOCK_GAP_PX } };
    }
    if (d === "video-left" || d === "video-right") {
      const side = d === "video-left" ? "left" : "right";
      const room = side === "left" ? player.left : viewport.width - right;
      if (!o.confined && room >= SIDE_MIN_EM * font + 2 * SIDE_GAP_PX) {
        const left = side === "left" ? SIDE_GAP_PX : right + SIDE_GAP_PX;
        const rect = { left, top: player.top + SIDE_GAP_PX, width: room - 2 * SIDE_GAP_PX, height: Math.max(0, player.height - 2 * SIDE_GAP_PX) };
        return { dock: d, outside: true, vertical: true, side, rect };
      }
      // Inside, at that edge; past the transcript panel when it is open on that side.
      const edge = (player.width * SIDE_EDGE_PCT) / 100 + (side === "left" ? panelLeft : panelRight);
      const top = player.top + (player.height * DOCK_TOP_PCT) / 100;
      const height = (player.height * (100 - DOCK_TOP_PCT - SIDE_BOTTOM_PCT)) / 100;
      const left = side === "left" ? player.left + edge : right - edge - row;
      return { dock: d, outside: false, vertical: true, side, rect: { left, top, width: row, height } };
    }
    const top = d === "video-top";
    const bandLeft = player.left + panelLeft;
    const bandWidth = Math.max(0, player.width - panelLeft - panelRight);
    const bottomPct = clampNumber(o.bottomPct, SUB_POSITION_MIN, SUB_POSITION_MAX, DEFAULT_SETTINGS.subPosition);
    const rect = {
      left: bandLeft + bandWidth * 0.04, // the subwrap's 4% padding
      top: top ? player.top + (player.height * DOCK_TOP_PCT) / 100 : player.top + player.height * (1 - bottomPct / 100) - row,
      width: bandWidth * 0.92,
      height: row,
    };
    return { dock: top ? d : "video-bottom", outside: false, vertical: false, side: null, rect };
  }

  // The docks a drag may end on right now, each with its place. Pure. Confined, window-bottom is
  // not offered: it would render where video-bottom does.
  function dockTargets(player, viewport, opts) {
    const docks = opts && opts.confined ? SUB_DOCKS.filter((d) => d !== "window-bottom") : SUB_DOCKS;
    return docks.map((dock) => ({ dock, rect: dockLayout(dock, player, viewport, opts).rect }));
  }

  // The dock whose rect lies nearest the point (0 inside it); the first one on a tie. Pure.
  function nearestDock(point, targets) {
    let best = null;
    let bestDistance = Infinity;
    for (const { dock, rect } of targets) {
      const dx = Math.max(rect.left - point.x, 0, point.x - (rect.left + rect.width));
      const dy = Math.max(rect.top - point.y, 0, point.y - (rect.top + rect.height));
      const distance = Math.hypot(dx, dy);
      if (distance < bestDistance) {
        best = dock;
        bestDistance = distance;
      }
    }
    return best;
  }

  function playerBox() {
    const r = state.player.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  }

  function viewportBox() {
    const el = document.documentElement;
    return { width: el.clientWidth || window.innerWidth, height: el.clientHeight || window.innerHeight };
  }

  // Nothing outside the player can show: in fullscreen, on a page with no video (the miniplayer
  // over YouTube's home), or with the player itself out of view.
  function layoutOptions(player) {
    const s = state.settings;
    const confined = !!document.fullscreenElement || !getVideoIdFromUrl(location.href) || !(player.width > 0 && player.height > 0);
    return {
      confined,
      fontPx: parseFloat(state.subBox.style.fontSize) || 0,
      bottomPct: s.subPosition,
      transcript: s.showTranscript ? oneOf(s.transcriptSide, TRANSCRIPT_SIDES, DEFAULT_SETTINGS.transcriptSide) : null,
    };
  }

  function subtitleOutside() {
    return !!(state.outside && state.subWrap && state.subWrap.parentNode === state.outside);
  }

  // The layer on the body for what lives outside the player. It carries the root's custom
  // properties, so the subtitle looks the same in it.
  function outsideLayer() {
    if (state.outside && state.outside.isConnected) return state.outside;
    const layer = document.createElement("div");
    layer.className = "shisuko-outside shisuko-hidden";
    applyStyleSettings(layer, state.settings);
    document.body.appendChild(layer);
    state.outside = layer;
    return layer;
  }

  const BOX_PROPS = ["left", "top", "right", "bottom", "width", "height"];

  // Inline px placement of the subwrap, or none (null): content.css places it then.
  function setBox(el, rect) {
    for (const prop of BOX_PROPS) el.style[prop] = "";
    if (!rect) return;
    for (const prop of BOX_PROPS) if (rect[prop] !== undefined) el.style[prop] = `${rect[prop]}px`;
    if (rect.bottom !== undefined) el.style.top = "auto";
    else el.style.bottom = "auto";
    el.style.right = "auto";
    if (rect.bottom !== undefined) el.style.height = "";
  }

  function setDockClasses(wrap, layout) {
    wrap.classList.toggle("shisuko-dock-top", !!layout && layout.dock === "video-top");
    wrap.classList.toggle("shisuko-vertical", !!layout && layout.vertical);
    wrap.classList.toggle("shisuko-side-left", !!layout && layout.side === "left");
    wrap.classList.toggle("shisuko-side-right", !!layout && layout.side === "right");
  }

  // Puts the subtitle on its dock: classes for the docks inside the player (content.css places
  // them), the outside layer and px for the rest. Moving the element keeps its listeners and its
  // text nodes, so Yomitan holds the same text wherever it is. Off, it goes back inside the root,
  // which is hidden.
  function placeSubtitle() {
    const wrap = state.subWrap;
    if (!wrap || !state.root || (state.drag && state.drag.moving)) return;
    const s = state.settings;
    const dock = subDock(s);
    let layout = null;
    if (dock !== "video-bottom" && state.player && state.player.isConnected) {
      const player = playerBox();
      layout = dockLayout(dock, player, viewportBox(), layoutOptions(player));
    }
    const outside = !!(s.enabled && layout && layout.outside);
    const host = outside ? outsideLayer() : state.root;
    if (wrap.parentNode !== host) host.appendChild(wrap);
    wrap.classList.remove("shisuko-dragging");
    setDockClasses(wrap, layout);
    setBox(wrap, outside ? layout.rect : null);
    if (state.outside) state.outside.classList.toggle("shisuko-hidden", !outside);
  }

  // Layout reads the player's box, so it waits for the next frame and runs once per frame.
  function scheduleLayout() {
    if (state.layoutFrame || !state.subWrap) return;
    state.layoutFrame = requestAnimationFrame(() => {
      state.layoutFrame = 0;
      placeSubtitle();
    });
  }

  function onViewportChange() {
    if (subDock(state.settings) !== "video-bottom") scheduleLayout();
  }

  function swallowTrusted(ev) {
    if (!ev.isTrusted) return;
    ev.preventDefault();
    ev.stopPropagation();
  }

  // The grip: a press, and once the pointer has moved DRAG_THRESHOLD_PX, a drag; the release
  // snaps the box to the nearest dock and saves it. The moves are followed on the window, not
  // through pointer capture: the drag moves the box (grip included) to another parent, which
  // would release a capture.
  function onGripDown(ev) {
    if (!ev.isTrusted || ev.button !== 0 || ev.isPrimary === false || !state.player) return;
    ev.preventDefault();
    ev.stopPropagation();
    endDrag(null);
    state.drag = { x: ev.clientX, y: ev.clientY, moving: false, targets: [], marks: [], near: null };
    window.addEventListener("pointermove", onDragMove, true);
    window.addEventListener("pointerup", onDragUp, true);
    window.addEventListener("pointercancel", onDragCancel, true);
    window.addEventListener("keydown", onDragKey, true);
  }

  function onDragMove(ev) {
    const drag = state.drag;
    if (!ev.isTrusted || !drag || ev.isPrimary === false) return;
    if (!drag.moving) {
      if (Math.hypot(ev.clientX - drag.x, ev.clientY - drag.y) < DRAG_THRESHOLD_PX) return;
      startDragging();
    }
    ev.preventDefault();
    ev.stopPropagation();
    moveDrag(ev.clientX, ev.clientY);
  }

  function startDragging() {
    const drag = state.drag;
    const player = playerBox();
    const opts = layoutOptions(player);
    drag.moving = true;
    drag.targets = dockTargets(player, viewportBox(), opts);
    // Fullscreen shows nothing outside the player, so the drag happens inside it there.
    drag.host = opts.confined ? state.root : outsideLayer();
    drag.host.classList.remove("shisuko-hidden");
    const origin = drag.host.getBoundingClientRect();
    drag.origin = { x: origin.left, y: origin.top };
    drag.marks = drag.targets.map(({ rect }) => {
      const mark = document.createElement("div");
      mark.className = "shisuko-dock-target";
      setBox(mark, { left: rect.left - drag.origin.x, top: rect.top - drag.origin.y, width: rect.width, height: rect.height });
      drag.host.appendChild(mark);
      return mark;
    });
    const wrap = state.subWrap;
    if (wrap.parentNode !== drag.host) drag.host.appendChild(wrap);
    setDockClasses(wrap, null);
    wrap.classList.add("shisuko-dragging");
    setBox(wrap, { left: 0, top: 0 });
    // Where the grip sits in the box, so the box hangs from the pointer by it.
    const box = wrap.getBoundingClientRect();
    const grip = state.grip.getBoundingClientRect();
    drag.grab = { x: grip.left + grip.width / 2 - box.left, y: grip.top + grip.height / 2 - box.top };
    document.documentElement.classList.add("shisuko-dragging");
  }

  function moveDrag(x, y) {
    const drag = state.drag;
    state.lastPointer.x = x;
    state.lastPointer.y = y;
    setBox(state.subWrap, { left: x - drag.grab.x - drag.origin.x, top: y - drag.grab.y - drag.origin.y });
    drag.near = nearestDock({ x, y }, drag.targets);
    drag.marks.forEach((mark, i) => mark.classList.toggle("shisuko-dock-near", drag.targets[i].dock === drag.near));
  }

  function onDragUp(ev) {
    const drag = state.drag;
    if (!ev.isTrusted || !drag || ev.isPrimary === false) return;
    if (drag.moving) {
      ev.preventDefault();
      ev.stopPropagation();
      // The click that follows lands on whatever lies under the pointer now, the player maybe.
      window.addEventListener("click", swallowTrusted, true);
      setTimeout(() => window.removeEventListener("click", swallowTrusted, true), 0);
    }
    endDrag(drag.moving ? nearestDock({ x: ev.clientX, y: ev.clientY }, drag.targets) : null);
  }

  function onDragCancel(ev) {
    if (ev.isTrusted) endDrag(null);
  }

  function onDragKey(ev) {
    if (!ev.isTrusted || ev.key !== "Escape" || !state.drag) return;
    ev.preventDefault();
    ev.stopPropagation();
    endDrag(null);
  }

  // Ends a press or a drag: on `dock`, saved, or (null) back where it was.
  function endDrag(dock) {
    const drag = state.drag;
    if (!drag) return;
    state.drag = null;
    window.removeEventListener("pointermove", onDragMove, true);
    window.removeEventListener("pointerup", onDragUp, true);
    window.removeEventListener("pointercancel", onDragCancel, true);
    window.removeEventListener("keydown", onDragKey, true);
    if (!drag.moving) return;
    for (const mark of drag.marks) mark.remove();
    document.documentElement.classList.remove("shisuko-dragging");
    if (dock && dock !== subDock(state.settings)) {
      // At once here; the storage echo brings the same value to every other tab.
      state.settings = Object.assign({}, state.settings, { subDock: dock });
      saveSettings({ subDock: dock }).then((res) => {
        if (res && res.ok === false) showToast("Not saved: " + (res.error || "unknown error"), "error", 6000);
      });
    }
    placeSubtitle();
    // The box has left the pointer behind, and no mouseleave said so (onSubtitleLeave sits out
    // the drag): a hover pause is decided now, the way leaving the box decides it.
    if (state.hoverPaused) scheduleResume();
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
    // A dock beside the video follows it wherever the page moves it, and goes back inside the
    // player on a page with no video (onViewportChange covers resizes and scrolls).
    if (subDock(state.settings) !== "video-bottom") scheduleLayout();
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
      state.resizeObserver = new ResizeObserver(() => {
        updateFontSize();
        scheduleLayout();
      });
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
        clearResumeTimer();
        sync(); // playing again: back to the one second cadence at once, not at the next tick
      };
      video.addEventListener("timeupdate", onTime);
      video.addEventListener("seeking", onSeek);
      video.addEventListener("play", onPlay);
      state.videoListeners = { onTime, onSeek, onPlay };
    }
  }

  function detach() {
    if (state.video && state.videoListeners) {
      const { onTime, onSeek, onPlay } = state.videoListeners;
      state.video.removeEventListener("timeupdate", onTime);
      state.video.removeEventListener("seeking", onSeek);
      state.video.removeEventListener("play", onPlay);
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
    state.mining = null; // its cue is the last video's
    state.minedSince = null; // another video is another session, with a log of its own
    state.languagePaused = false;
    state.heard = null;
    state.modelLoading = null;
    state.modelError = null;
    state.hoverPaused = false;
    state.awaitingPlayerMove = false;
    state.lastSyncAt = 0;
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
  // A mine the viewer asked for goes out, and is answered, on the next tick.
  // Pure: { mining, paused, t, status, covered, duration, lastSyncAt, standby } and a clock in, a decision out.
  function shouldSync(st, now) {
    if (!st.paused || st.mining) return true;
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
    pollWordIndex();
    const decision = {
      mining: !!state.mining,
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
    const mine = pendingMine();
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
          anki: ankiForSync(s),
          mined_since: state.minedSince,
          ...(mine ? { mine } : {}),
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
    // be unable to tell waiting for its turn from a dead server.
    if (result && result.ok && result.data && result.data.status === "standby") {
      state.standby = true;
      updateStatus();
      return;
    }
    if (!result || !result.ok) {
      // Not standby, so the election let this request through: the background answers standby
      // only with ok: true. A flag left by the last refused tick would rank above the verdict
      // below in statusText().
      state.standby = false;
      // The language verdict came from the last answer that was one; this one says nothing about
      // the session, so it goes, as on a restart, or statusText() would show the pause in place of
      // the error below. The next real answer brings it back.
      state.languagePaused = false;
      state.heard = null;
      // Only an answer the server itself gave carries `data` (null for an empty body). Without it
      // nothing of ours answered: the server unreachable, something else on its port (not JSON),
      // or the background gone (sendMessage's own {ok, error} during a reload or a worker restart),
      // and every other message would fail the same way.
      if (!result || result.offline || !("data" in result)) {
        if (state.mining) {
          state.mining = null; // nobody to mine it: said now, not at the timeout
          showToast("Mining failed: " + (errorText(result && result.error) || "Server unreachable"), "error", 6000);
        }
        state.offline = true;
        state.serverStatus = "offline";
        state.serverError = errorText(result && result.error) || "Server unreachable";
      } else {
        // The server is up and refused this request (a 400 for an odd id, a 500 from a broken
        // cache): its own words, in the error style. It is not offline.
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
    // Before the session check: the log belongs to the video, not to one session token.
    takeMined(data);
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
      // here reads it, because a segment is not a sentence: a mined card gets exactly its cue.
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
    mine.title = "Mine this sentence into the newest Anki card: audio + picture";
    line.appendChild(time);
    line.appendChild(text);
    line.appendChild(mine);
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
      if (cue) mineCue(cue);
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
  // which the server sends with the holder tab's /sync and the background keeps for every tab;
  // the standby tab keeps its lines on screen, and a card reviewed must recolour them there too.
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

  // The popup's mining settings as /sync carries them (`anki`): the server attaches with them, and
  // validates them, since nothing the browser sends is trusted there. Pure: the settings in, the
  // object out.
  function ankiForSync(s) {
    return {
      auto: !!s.autoMine,
      picture: String(s.ankiImageField || "").trim(),
      audio: String(s.ankiAudioField || "").trim(),
      sentence: String(s.ankiSentenceField || "").trim(),
      word: String(s.ankiWordField || "").trim(),
      pad_ms: Math.max(0, Math.min(2000, Math.round(Number(s.clipPaddingMs) || 0))),
      format: s.clipFormat === "wav" ? "wav" : "mp3",
    };
  }

  // A mine's name in the server's log: unique enough within one session, which is all it needs.
  function newNonce() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  // One canvas for every frame read back. A DRM-protected video taints it for good, so a failed
  // read lets it go and the next one starts afresh.
  let frameCanvas = null;

  // The video's current frame as a JPEG data: URL (1280 px wide at most), or null when there is
  // no frame or the video cannot be read back (DRM).
  function captureFrame(video) {
    const w = video && video.videoWidth;
    const h = video && video.videoHeight;
    if (!w || !h) return null;
    try {
      const scale = Math.min(1, 1280 / w);
      const canvas = frameCanvas || (frameCanvas = document.createElement("canvas"));
      canvas.width = Math.round(w * scale);
      canvas.height = Math.round(h * scale);
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      return canvas.toDataURL("image/jpeg", 0.85);
    } catch (err) {
      frameCanvas = null;
      return null;
    }
  }

  // The server's answers that mean there is no Anki to mine into: AnkiConnect did not answer, or
  // the server mines nothing. The line then goes to Downloads (saveMine in background.js).
  const NO_ANKI = new Set(["unreachable", "off"]);

  // The viewer's own mine: this cue into the newest Anki card. The server does the work; the next
  // /sync carries the request, and every /sync after it until the answer's `mined` reports it
  // (takeMined), so a lost answer costs a resend, never a second card. The frame is taken now,
  // while the video still shows the line, for the case the answer says there is no Anki.
  function mineCue(cue) {
    if (!cue || !state.videoId || !state.settings.enabled) return;
    if (state.mining) {
      showToast("Still mining the last line…", "info");
      return;
    }
    state.mining = { cue_id: cue.id, nonce: newNonce(), session: state.serverSession, at: Date.now(), generation: state.cueGeneration,
      cue: { start: cue.start, end: cue.end }, videoId: state.videoId, frame: captureFrame(state.video) };
    showToast("Mining…", "info", 15000);
    resumeAfterMining(state.video);
    sync();
  }

  // What the next /sync asks for, or nothing: a mine whose cues were dropped meanwhile (a new
  // session) is given up, since its cue id now names another line.
  function pendingMine() {
    const m = state.mining;
    if (!m) return null;
    if (m.generation !== state.cueGeneration) {
      state.mining = null;
      showToast("Nothing mined: the subtitles changed", "warn");
      return null;
    }
    if (Date.now() - m.at > MINE_TIMEOUT_MS) {
      state.mining = null;
      showToast("Mining failed: no answer from the server (a server older than 0.22.0 cannot mine)", "error", 6000);
      return null;
    }
    return { cue_id: m.cue_id, nonce: m.nonce, session: m.session };
  }

  // The server's mining log since the last answer: the outcome of the viewer's own mine, which
  // ends it, and each note the server attached to by itself. The word colours need no word here:
  // the server reads the new card's deck and sends its row with the next /sync.
  function takeMined(data) {
    if (typeof data.mined_next === "number") state.minedSince = data.mined_next;
    const refused = typeof data.anki_error === "string" ? data.anki_error : null;
    if (refused && refused !== state.ankiError) showToast("The server refused the Anki settings: " + refused, "error", 6000);
    state.ankiError = refused;
    for (const event of Array.isArray(data.mined) ? data.mined : []) {
      if (!event || typeof event !== "object") continue;
      const own = !!state.mining && event.nonce === state.mining.nonce;
      if (own && !event.ok && NO_ANKI.has(event.status)) {
        // An answer that lands after the switch went off saves nothing: off is off.
        if (state.settings.enabled) saveMine(state.mining);
        state.mining = null;
        continue;
      }
      if (own) state.mining = null;
      // An automatic attach that went wrong is the server log's business, not a toast's.
      if (own || (event.ok && !event.nonce)) {
        const text = String(event.message || (event.ok ? "Mined" : "unknown error"));
        if (event.ok) showToast(text, "ok");
        else showToast(own && event.status === "error" ? "Mining failed: " + text : text, "warn", 6000);
      }
    }
  }

  async function saveMine(m) {
    const res = await sendMessage({ type: "saveMine", videoId: m.videoId, cue: m.cue, frame: m.frame });
    if (res && res.ok) showToast(String(res.message || "Saved to Downloads"), "ok", 6000);
    else showToast(String((res && res.error) || "Saving failed"), "error", 6000);
  }

  function resumeAfterMining(video) {
    // Keep watching while the server cuts the audio clip and the frame. Only a pause
    // caused by hovering the subtitle is undone here; a pause the viewer chose stays.
    if (!state.hoverPaused || !video || !video.paused) return;
    state.hoverPaused = false;
    state.awaitingPlayerMove = false;
    clearResumeTimer();
    video.play().catch(() => {});
  }

  function mineCurrent() {
    const cue = currentCueForMining();
    if (!cue) {
      showToast("No subtitle at this position to mine", "error");
      return;
    }
    mineCue(cue);
  }

  // ------------------------------------------------------------ pause while hovering

  function onSubtitleEnter(ev) {
    if (!ev.isTrusted) return; // see onMineClick
    clearResumeTimer();
    state.awaitingPlayerMove = false;
    if (!state.settings.pauseOnHover || !state.video) return;
    if (!state.video.paused && !state.video.ended) {
      state.video.pause();
      state.hoverPaused = true;
    }
  }

  function onSubtitleLeave(ev) {
    if (!ev.isTrusted) return;
    // The box follows the grip; endDrag() decides about the pause once it lands.
    if (state.drag && state.drag.moving) return;
    if (subtitleOutside()) {
      // No player listener follows the pointer out here: where it left is where it is.
      state.lastPointer.x = ev.clientX;
      state.lastPointer.y = ev.clientY;
      state.pointerInPlayer = false;
    }
    if (!state.hoverPaused) return;
    const related = ev.relatedTarget;
    // Leaving towards a dictionary popup (an iframe, or anything that is not part of the
    // player, or of the page around a box outside it) keeps the video paused until the pointer
    // comes back over the video.
    if (!stillOnPage(related)) {
      state.awaitingPlayerMove = true;
      return;
    }
    scheduleResume();
  }

  // The pointer over a box outside the player, where onPlayerMouseMove never hears it:
  // Alt+Shift+K reads the word under it (knownTarget).
  function onSubtitleMove(ev) {
    if (!ev.isTrusted || !subtitleOutside()) return;
    state.lastPointer.x = ev.clientX;
    state.lastPointer.y = ev.clientY;
    state.pointerInPlayer = true;
  }

  // Whether the pointer, gone to `el` from the subtitle, is still on the page rather than in a
  // dictionary popup. Inside the player only the player counts; a box outside it is left for
  // the page around it as well (isPageElement).
  function stillOnPage(el) {
    if (subtitleOutside()) return isPageElement(el);
    return !!el && el.tagName !== "IFRAME" && isYouTubeElement(el);
  }

  function onPlayerMouseMove(ev) {
    // Mutated, not replaced: this runs on every mouse move across the player.
    state.lastPointer.x = ev.clientX;
    state.lastPointer.y = ev.clientY;
    state.pointerInPlayer = true;
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
      if (under && !stillOnPage(under)) {
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
    window.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("yt-navigate-finish", () => {
      // YouTube swaps the player on navigation: the cached element must be looked up again.
      state.rediscover = true;
      setTimeout(discover, 50);
    });
    // Fullscreen shows nothing outside the player: the outside docks come inside, and back after.
    document.addEventListener("fullscreenchange", () => {
      scheduleLayout();
      setTimeout(() => {
        updateFontSize();
        scheduleLayout();
      }, 100);
    });
    window.addEventListener("resize", onViewportChange, { passive: true });
    // Captured: a scroll inside the page (the Shorts reel) moves the player too, and does not bubble.
    window.addEventListener("scroll", onViewportChange, { capture: true, passive: true });
  });
})();
