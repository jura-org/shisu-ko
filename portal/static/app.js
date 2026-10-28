// SPDX-License-Identifier: Apache-2.0
"use strict";

(() => {
  const L = window.PortalLib;
  const $ = (id) => document.getElementById(id);

  const SETTINGS_KEY = "portal.settings";
  const RECENT_KEY = "portal.recent";
  // wordColours is off by default, like the extension's cardStatus.
  const DEFAULTS = { pauseOnLookup: true, lookupResume: "tap", autoPause: false, speed: 1, listening: false, furigana: false, sweep: true, wordColours: false };
  const SPEEDS = [0.75, 1, 1.25, 1.5];
  const SYNC_PLAYING_MS = 1000;
  const SYNC_PAUSED_MS = 5000;
  const BROWSE_IDLE_MS = 3000;
  const TOKENIZE_BATCH = 10;
  const FROM_HOME = "portal.fromHome"; // sessionStorage: this video was opened from the home page
  const LINE_HOLD_S = 2.5; // a line stays up this long after it ends, then the space is empty
  const GLIDE_MS = 400; // the list's transition (app.css .lyrics-track)
  const TOAST_MS = 2000;
  // /api/watched: the first report 10 s after play starts (YouTube's first scheduled flush), then
  // when the server's answer says (`next`, YouTube's own schedule), and a final one at every pause,
  // end, hide and leave.
  const WATCH_FIRST_MS = 10000;
  const WATCH_UNANSWERED_MS = 40000; // the next report when no answer names one
  const END_HOME_MS = 1500; // a finished video goes home after this beat, so its end is seen
  // YT.PlayerState values, usable before the API has loaded.
  const YT_ENDED = 0;
  const YT_PLAYING = 1;
  const YT_PAUSED = 2;
  const YT_BUFFERING = 3;
  const YT_CUED = 5;

  const settings = { ...DEFAULTS, ...readJson(SETTINGS_KEY, {}) };
  const S = L.STRINGS;
  // With ?debug in the address: read-only numbers for checking the sweep from a console (which
  // timing source each swept cue used, the clock it runs on). Nothing is exposed otherwise.
  const debug = { sources: { words: 0, estimate: 0 }, lastSource: null };
  if (new URLSearchParams(location.search).has("debug")) {
    Object.defineProperty(debug, "t", { get: () => state.t });
    window.portalDebug = debug;
  }
  if (!SPEEDS.includes(settings.speed)) settings.speed = 1;
  if (settings.lookupResume !== "line") settings.lookupResume = "tap"; // "tap" or "line": lib.js lookupResume()

  const state = {
    videoId: null,
    startAt: 0,
    player: null,
    playerReady: false,
    ytState: -1,
    everPlayed: false,
    pauseCause: null, // why we paused: "user" | "auto" | "sheet"; decides the scrim over the video
    playing: false,
    t: 0,
    cues: [],
    cueById: new Map(),
    cueIndex: new Map(),
    since: 0,
    session: null,
    syncData: null,
    offline: false,
    syncError: null,
    syncInFlight: false,
    lastSyncAt: 0,
    tokens: new Map(),
    tokenWanted: new Set(),
    tokenizing: false,
    activeIdx: -1,
    focusKey: null,
    focusPos: null,
    trackBase: 0,
    browse: { active: false, offset: 0, timer: null },
    sweep: { cueId: null, spans: [], times: [], count: -1 },
    clock: { raw: null, rawAt: 0, t: 0 },
    gestureAt: 0,
    backUntil: 0,
    mediaDuration: null,
    urlT: null,
    lang: null,
    info: null,
    previewT: null,
    startKnown: false,
    focusId: null,
    reanchor: false,
    glideUntil: 0,
    tornDown: false,
    endTimer: null,
    lastSave: null, // the last position posted: { videoId, t, at, request }
    endGen: 0, // bumped by every cancel: an endHome() past its await checks it still holds
    endWaiting: false, // endHome() waits on the end's save: still the beat
    endHeld: false, // the current touch or key cancelled the beat, so it must not also play
    sheetClosedAt: 0,
    resumeTarget: null,
    resumeLanded: true,
    cardSetup: null,
    syncFailures: 0,
    syncRetryAt: 0,
    syncReceivedAt: 0,
    tokenFailed: new Set(), // cues Yomitan could not tokenise: tappable per character meanwhile
    wheelTimer: null,
    autoPausedFor: null,
    rowById: new Map(),
    renderedCount: 0,
    title: null,
    lastSavedAt: 0,
    watch: L.newWatchLog(), // the stretches our own player played, for /api/watched
    watchDueAt: Infinity, // the next scheduled /api/watched report; Infinity until a play starts the schedule
    watchGapMs: WATCH_FIRST_MS, // playing time from a resume to that report: the server's last `next`
    lastShieldTap: null,
    shieldTimer: null,
    controlsHidden: false, // a side tap hid the controls (landscape); renderBack()
    stepBase: -1, // a double tap's run steps from the line it began on
    stepIdx: null, // and the line it last put the playhead on
    inDeck: new Map(), // term -> boolean, from /api/canadd
    // The word colours: the deck's rows as the server sent them (L.applyDeck), and the words.js
    // index built from them; wordsKey is its entries as JSON, so an unchanged deck draws nothing.
    deck: { token: null, status: null, rows: new Map() },
    wordIndex: null,
    wordsKey: "",
    sheet: { open: false, pausedIt: false, cue: null, span: null, entries: [], pick: 0, def: null, seq: 0 },
    // The landscape mode: on, and whether the browser's fullscreen holds the page for it.
    landscape: { on: false, fullscreen: false },
  };

  function readJson(key, fallback) {
    try {
      const value = JSON.parse(localStorage.getItem(key));
      return value ?? fallback;
    } catch {
      return fallback;
    }
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  }

  // A request that hangs (a dead connection after the phone slept, or after a back-forward
  // restore) must not leave the page waiting for ever: every fetch has a deadline.
  function timedFetch(path, init = {}, ms = 10000) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), ms);
    return fetch(path, { ...init, signal: abort.signal }).finally(() => clearTimeout(timer));
  }

  async function api(path, body) {
    // A mine can take a while (the sentence audio waits for the server): a longer deadline.
    const res = await timedFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, path === "/api/mine" ? 90000 : 20000);
    return answerOf(res);
  }

  async function getJSON(path) {
    return answerOf(await timedFetch(path));
  }

  async function answerOf(res) {
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    // This device was forgotten (lan mode): the page itself now asks to pair again.
    if (res.status === 401 && data && data.pair) {
      toast(S.notPaired);
      setTimeout(() => location.reload(), 1500);
    }
    return { status: res.status, data };
  }

  function isJa() {
    return L.isJapanese(state.lang);
  }

  // ------------------------------------------------------------ routing and home

  // The view comes from the address alone and is drawn before any request: whatever the network
  // does, the page is never blank.
  function route() {
    applyStrings();
    window.addEventListener("pageshow", (ev) => onShow(ev.persisted));
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") onShow(false); });
    const params = new URLSearchParams(location.search);
    const hash = new URLSearchParams(location.hash.slice(1));
    const id = params.get("v") || hash.get("v");
    if (id && L.VIDEO_ID_RE.test(id)) {
      state.videoId = id;
      const urlT = params.get("t") ?? hash.get("t");
      state.urlT = urlT === null ? null : L.parseStart(urlT);
      startWatch();
    } else {
      showHome();
    }
  }

  function openVideo(text) {
    const parsed = L.parseVideoInput(text);
    if (!parsed) {
      $("open-error").textContent = S.notALink;
      $("open-error").hidden = false;
      return false;
    }
    sessionStorage.setItem(FROM_HOME, "1");
    location.href = `?v=${encodeURIComponent(parsed.id)}${parsed.t ? `&t=${Math.floor(parsed.t)}` : ""}`;
    return true;
  }

  function showHome() {
    $("home").hidden = false;
    $("watch").hidden = true;
    const input = $("open-input");
    $("open-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      openVideo(input.value);
    });
    // Pasting a link is the whole task, so a paste opens it at once.
    input.addEventListener("paste", () => setTimeout(() => {
      if (L.parseVideoInput(input.value)) openVideo(input.value);
    }, 0));
    input.addEventListener("input", () => { $("open-error").hidden = true; });
    // The clipboard API exists only in a secure context; over plain HTTP the field's own paste remains.
    if (navigator.clipboard && navigator.clipboard.readText) {
      const paste = $("paste-btn");
      paste.hidden = false;
      paste.addEventListener("click", async () => {
        try {
          input.value = (await navigator.clipboard.readText()).trim();
          openVideo(input.value);
        } catch {
          input.focus();
        }
      });
    }
    $("edit-btn").textContent = S.edit;
    $("edit-btn").addEventListener("click", () => setEditing(!$("home").classList.contains("editing")));
    bindSwipe();
    bindAnkiSetup();
    renderRecent(recentList()); // the local copy at once; the server's list replaces it
    // Opening a video from here: its back button may return here with history.back().
    $("recent").addEventListener("click", (ev) => { if (ev.target.closest("a")) sessionStorage.setItem(FROM_HOME, "1"); });
    renderSetupHints();
    loadInfo().then(renderAnkiRow);
    migrateLocal().then(loadRecent);
  }

  // Every time the page is shown (a load, a back-forward restore, a return from another app):
  // the home view is drawn and its list fetched again; a watch page whose video was released on the
  // way out starts over.
  function onShow(persisted) {
    if (!state.videoId) {
      sessionStorage.removeItem(FROM_HOME);
      $("home").hidden = false;
      $("watch").hidden = true;
      renderRecent(recentList());
      loadRecent();
      renderSetupHints();
    } else if (persisted && state.tornDown) {
      location.reload();
    }
  }

  // The device's old local list, moved into the shared server list once.
  async function migrateLocal() {
    if (localStorage.getItem("portal.migrated")) return;
    const items = recentList();
    try {
      const res = await api("/api/positions/import", { items });
      if (res.status === 200) localStorage.setItem("portal.migrated", "1");
    } catch {
      // offline: try again next time
    }
  }

  async function loadRecent() {
    try {
      const res = await fetch("/api/positions");
      const data = await res.json();
      if (!data.ok) return;
      // Kept locally as the offline fallback.
      localStorage.setItem(RECENT_KEY, JSON.stringify(data.videos.slice(0, 30).map((v) => ({ id: v.id, title: v.title || "", t: v.t, duration: v.duration, at: v.updated }))));
      renderRecent(data.videos);
    } catch {
      // offline: the local list stays
    }
  }

  function recentList() {
    const list = readJson(RECENT_KEY, []);
    return Array.isArray(list) ? list.filter((r) => r && L.VIDEO_ID_RE.test(r.id)) : [];
  }

  function remember() {
    const list = recentList().filter((r) => r.id !== state.videoId);
    list.unshift({ id: state.videoId, title: state.title || "", t: Math.floor(state.t), duration: playerDuration(), at: Date.now() });
    localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 30)));
  }

  // Thumbnails come through the portal (cached on the PC); i.ytimg.com is the fallback chain.
  function thumbImage(id) {
    const img = document.createElement("img");
    img.alt = "";
    const chain = [`/api/thumb/${id}`, `https://i.ytimg.com/vi/${id}/mqdefault.jpg`, `https://i.ytimg.com/vi/${id}/hqdefault.jpg`, `https://i.ytimg.com/vi/${id}/default.jpg`];
    let at = 0;
    img.addEventListener("error", () => { if (++at < chain.length) img.src = chain[at]; });
    img.src = chain[0];
    return img;
  }

  // Each row: the actions behind it (actionsFor()), and in front the row itself, which a swipe to
  // the left slides off them. Edit mode's red minus opens the same actions.
  function renderRecent(list) {
    const ul = $("recent");
    swipe.open = null;
    swipe.drag = null;
    ul.replaceChildren();
    for (const r of list) ul.append(recentRow(r));
    listChanged();
  }

  function recentRow(r) {
    const name = r.title || r.id;
    const li = document.createElement("li");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.inert = true;
    const buttons = actionsFor(r).map((spec) => {
      const button = document.createElement("button");
      fillAction(button, spec, name);
      button.addEventListener("click", () => button.spec.run(li, r));
      return button;
    });
    actions.append(...buttons);
    swipe.rows.set(li, { r, buttons, width: buttons.length * ACTION_W });

    const front = document.createElement("div");
    front.className = "row-front";
    const a = document.createElement("a");
    a.href = `?v=${encodeURIComponent(r.id)}`;
    a.draggable = false; // a mouse drag swipes; it never drags the link away
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    thumb.append(thumbImage(r.id));
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = name;
    a.append(thumb, title);
    const remove = document.createElement("button");
    remove.className = "remove";
    remove.setAttribute("aria-label", S.rowActions(name));
    remove.addEventListener("click", () => {
      if (swipe.open === li) closeRow();
      else {
        openRow(li);
        buttons[0].focus({ preventScroll: true });
      }
    });
    front.append(remove, a);
    li.append(actions, front);
    drawBar(li, r);
    return li;
  }

  // The watch bar under the thumbnail, drawn again whenever the position changes.
  function drawBar(li, r) {
    const thumb = li.querySelector(".thumb");
    const old = thumb.querySelector(".progress");
    if (old) old.remove();
    const fraction = L.watchedFraction(r);
    if (fraction <= 0) return;
    const bar = document.createElement("div");
    bar.className = "progress";
    const fill = document.createElement("span");
    fill.style.width = `${(fraction * 100).toFixed(1)}%`;
    bar.append(fill);
    thumb.append(bar);
  }

  // Line icons in the page's own SVG style (app.css svg), from path data.
  const ICONS = {
    trash: ["M4 7h16", "M9.5 7V4.5h5V7", "M6.5 7l1 12.5h9l1-12.5", "M10 11v5", "M14 11v5"],
    eyeCheck: ["M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z", "M9 12.2l2.1 2.1 3.9-3.9"],
    eyeOff: ["M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z", "M4 4l16 16"],
  };

  function icon(name) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    for (const d of ICONS[name]) {
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }

  // The swipe's actions, left to right; Delete is always last (a full swipe runs it). Mark as
  // watched needs the duration, which a row moved over from an old device's list may lack.
  function actionsFor(r) {
    const del = { className: "delete", icon: "trash", label: S.delete, aria: S.deleteVideo, run: deleteRow };
    if (L.watchedToggle(r) === null) return [del];
    return [markAction(r), del];
  }

  function markAction(r) {
    return L.watchedToggle(r) === 0
      ? { className: "mark", icon: "eyeOff", label: S.markUnwatched, aria: S.markUnwatchedVideo, run: markWatched }
      : { className: "mark", icon: "eyeCheck", label: S.markWatched, aria: S.markWatchedVideo, run: markWatched };
  }

  function fillAction(button, spec, name) {
    button.spec = spec;
    button.className = spec.className;
    const face = document.createElement("span");
    face.className = "act-face";
    const label = document.createElement("span");
    label.textContent = spec.label;
    face.append(icon(spec.icon), label);
    const old = button.firstChild;
    if (old) {
      face.style.cssText = old.style.cssText;
      label.style.opacity = old.lastChild.style.opacity;
    }
    button.replaceChildren(face);
    button.setAttribute("aria-label", spec.aria(name));
  }

  function setEditing(editing) {
    $("home").classList.toggle("editing", editing);
    $("edit-btn").textContent = editing ? S.done : S.edit;
    closeRow();
  }

  // Called directly, never through a synthetic click: the swipe's click guard would eat one.
  function listChanged() {
    const empty = !$("recent").children.length;
    $("recent-empty").hidden = !empty;
    $("edit-btn").hidden = empty;
    if (empty && $("home").classList.contains("editing")) setEditing(false);
  }

  function forget(id) {
    localStorage.setItem(RECENT_KEY, JSON.stringify(recentList().filter((x) => x.id !== id)));
  }

  async function deleteRow(li, r) {
    if (swipe.open === li) swipe.open = null;
    collapse(li);
    forget(r.id);
    try {
      await api("/api/position/delete", { video_id: r.id });
    } catch {
      // gone locally; the server copy goes next time
    }
  }

  // Mark as watched sets the position to the end, here and on the PC, so the bar reads full and the
  // next open starts at 0 (resumeTime() starts a finished() video over); on a finished video it marks it
  // unwatched: 0, the bar gone. The button then offers the other.
  async function markWatched(li, r) {
    const t = L.watchedToggle(r);
    if (t === null) return;
    closeRow();
    r.t = t;
    drawBar(li, r);
    fillAction(swipe.rows.get(li).buttons[0], markAction(r), r.title || r.id);
    localStorage.setItem(RECENT_KEY, JSON.stringify(recentList().map((x) => (x.id === r.id ? { ...x, t } : x))));
    try {
      // keep_order: a mark is not a watch, so the row keeps its place here and on the PC.
      await api("/api/position", { video_id: r.id, t, duration: r.duration, keep_order: true });
    } catch {
      // offline: the PC keeps the old position until the video is played again
    }
  }

  // ------------------------------------------------------------ swiping a home row

  // How it stays smooth: during a drag only transforms change (the row, and each button sliding in
  // from the right edge), written once per frame; nothing is measured but the row's width, read at
  // the touch. Released, the row springs to where swipeSettle() sends it (the finger's speed
  // included) on a CSS transition; li.live holds will-change while a row moves.
  const ACTION_W = 80; // one action button's share of an open row
  const SETTLE_MS = 300; // app.css .recent .row-front transition
  const EXPAND_MS = 200; // app.css .recent li .row-actions button.expanding
  const COLLAPSE_MS = 300; // app.css .recent li.collapsing
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  // open: the row whose actions show. drag: the gesture under way. eat: the click this gesture would
  // make is swallowed (it closed a row, or it was a swipe).
  const swipe = { open: null, openScrollY: 0, drag: null, eat: false, gesture: 0, rows: new WeakMap() };

  // shift px left of closed (negative: the rubber band to the right); rowW the row's width.
  function setShift(li, shift, rowW) {
    const row = swipe.rows.get(li);
    li.querySelector(".row-front").style.transform = shift ? `translateX(${-shift}px)` : "";
    // Past the delete point Delete takes the whole width, as in iOS Mail, with a tick where the
    // device has one (Android; iOS gives the web none).
    const full = shift >= rowW * L.SWIPE.DELETE;
    const last = row.buttons[row.buttons.length - 1];
    if (full !== li.classList.contains("full")) {
      li.classList.toggle("full", full);
      last.classList.add("expanding");
      clearTimeout(last.expandTimer);
      last.expandTimer = setTimeout(() => last.classList.remove("expanding"), EXPAND_MS);
      if (full && navigator.vibrate) navigator.vibrate(8);
    }
    const offsets = L.actionOffsets(shift, row.buttons.length, full);
    row.buttons.forEach((b, i) => {
      b.style.transform = `translateX(${-offsets[i]}px)`;
      // Its icon and label stay centred in the part of it that shows, until that is a full slot.
      const shown = offsets[i] - (offsets[i + 1] || 0);
      b.firstChild.style.transform = shown < ACTION_W ? `translateX(${(shown - ACTION_W) / 2}px)` : "";
      // The label fades in over the second half of the slot: clipped words read worse than none.
      b.firstChild.lastChild.style.opacity = Math.max(0, Math.min(1, (shown - ACTION_W / 2) / (ACTION_W / 2)));
    });
  }

  function settle(li, shift) {
    li.classList.add("live");
    clearTimeout(li.settleTimer);
    setShift(li, shift, li.offsetWidth);
    if (!shift) li.settleTimer = setTimeout(() => li.classList.remove("live"), SETTLE_MS);
  }

  function openRow(li) {
    if (swipe.open && swipe.open !== li) closeRow();
    swipe.open = li;
    swipe.openScrollY = window.scrollY;
    li.querySelector(".row-actions").inert = false;
    settle(li, swipe.rows.get(li).width);
  }

  function closeRow() {
    const li = swipe.open;
    if (!li) return;
    swipe.open = null;
    li.querySelector(".row-actions").inert = true;
    settle(li, 0);
  }

  // The row slides off to the left, then its space closes.
  function collapse(li) {
    if (reducedMotion.matches) {
      li.remove();
      listChanged();
      return;
    }
    settle(li, li.offsetWidth);
    li.style.height = `${li.offsetHeight}px`;
    void li.offsetHeight; // the height above is the transition's start
    li.classList.add("collapsing");
    li.style.height = "0px";
    setTimeout(() => {
      li.remove();
      listChanged();
    }, COLLAPSE_MS);
  }

  // Pointer events, so touch and a mouse drag alike. The rows are touch-action: pan-y: the browser
  // scrolls vertically itself, and a gesture becomes a swipe only once its first SLOP px went
  // sideways.
  function bindSwipe() {
    const ul = $("recent");
    document.addEventListener("pointerdown", (ev) => {
      swipe.eat = false;
      swipe.gesture++;
      const open = swipe.open;
      // With a row open, a touch anywhere but on its actions or on the row itself only closes it.
      if (open && !open.contains(ev.target)) {
        closeRow();
        swipe.eat = true;
      }
    }, true);
    document.addEventListener("click", (ev) => {
      if (!swipe.eat) return;
      swipe.eat = false;
      ev.preventDefault();
      ev.stopPropagation();
    }, true);
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeRow(); });
    // A scroll closes the open row; a few px of jitter (a rubber band settling) does not.
    window.addEventListener("scroll", () => {
      if (swipe.open && Math.abs(window.scrollY - swipe.openScrollY) > L.SWIPE.SLOP) closeRow();
    }, { passive: true });

    ul.addEventListener("pointerdown", (ev) => {
      if (!ev.isPrimary || ev.button > 0 || swipe.eat) return;
      const li = ev.target.closest("li");
      if (!li || ev.target.closest(".row-actions, .remove")) return;
      const row = swipe.rows.get(li);
      // live (will-change) now, so the layers exist by the time the first SLOP px are past.
      li.classList.add("live");
      clearTimeout(li.settleTimer);
      swipe.drag = { li, row, rowW: li.offsetWidth, id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, axis: null,
        start: li === swipe.open ? row.width : 0, shift: 0, frame: 0, samples: [{ t: ev.timeStamp, x: ev.clientX }] };
    });
    ul.addEventListener("pointermove", (ev) => {
      const d = swipe.drag;
      if (!d || ev.pointerId !== d.id) return;
      const dx = ev.clientX - d.x0;
      d.samples.push({ t: ev.timeStamp, x: ev.clientX });
      if (d.samples.length > 12) d.samples.shift();
      if (!d.axis) {
        d.axis = L.swipeAxis(dx, ev.clientY - d.y0);
        if (!d.axis) return;
        if (d.axis === "y") {
          swipe.drag = null;
          if (d.li !== swipe.open) settle(d.li, 0); // drops live
          closeRow();
          return;
        }
        d.li.setPointerCapture(d.id);
        d.li.classList.add("dragging");
        if (swipe.open && swipe.open !== d.li) closeRow();
        // Measured from here, so the row starts moving under the finger, not SLOP px behind it.
        d.x0 = ev.clientX;
      }
      d.shift = L.swipeShift({ start: d.start, dx: ev.clientX - d.x0, row: d.rowW, actions: d.row.width });
      if (!d.frame) {
        d.frame = requestAnimationFrame(() => {
          d.frame = 0;
          setShift(d.li, d.shift, d.rowW);
        });
      }
    });
    const release = (ev) => {
      const d = swipe.drag;
      if (!d || ev.pointerId !== d.id) return;
      swipe.drag = null;
      cancelAnimationFrame(d.frame);
      if (!d.axis) {
        // A tap on the open row closes it and opens nothing.
        if (d.li === swipe.open) {
          closeRow();
          swipe.eat = true;
        } else settle(d.li, 0);
        return;
      }
      swipe.eat = true; // a swipe is never also a tap on the link
      d.samples.push({ t: ev.timeStamp, x: ev.clientX });
      d.li.classList.remove("dragging");
      const verdict = ev.type === "pointercancel" ? (d.start ? "open" : "closed")
        : L.swipeSettle({ shift: d.shift, actions: d.row.width, row: d.rowW, wasOpen: d.start > 0, velocity: L.swipeVelocity(d.samples) });
      if (verdict === "delete") deleteRow(d.li, d.row.r);
      else if (verdict === "open") openRow(d.li);
      else if (swipe.open === d.li) closeRow();
      else settle(d.li, 0);
    };
    ul.addEventListener("pointerup", release);
    ul.addEventListener("pointercancel", release);
    // The click that follows a gesture comes right after its pointerup; one that never comes
    // (a touch that moved) must not eat a later click, a keyboard's among them.
    for (const name of ["pointerup", "pointercancel"]) {
      document.addEventListener(name, () => {
        const gesture = swipe.gesture;
        if (swipe.eat) setTimeout(() => { if (swipe.gesture === gesture) swipe.eat = false; }, 600);
      });
    }
  }

  // ------------------------------------------------------------ positions

  function playerDuration() {
    const d = state.player && state.player.getDuration ? state.player.getDuration() : 0;
    return Number.isFinite(d) && d > 0 ? d : state.mediaDuration || null;
  }

  // Saved on the PC (shared by every device) and locally. keepalive lets it outlive a page that is
  // being hidden or left. Answers the request, or null when there was nothing to save.
  function savePosition() {
    if (!state.videoId || !state.playerReady || !state.everPlayed) return null; // never overwrite with an unplayed 0
    // A torn-down player reads 0: going home saved first, and the pagehide after it must not undo that.
    if (state.tornDown) return null;
    const t = state.player.getCurrentTime();
    if (!Number.isFinite(t)) return null;
    if (!L.saveAllowed(t, state.resumeTarget, state.resumeLanded)) return null; // the resume has not landed yet
    state.t = t;
    remember();
    // The same position again (the end's pause, ended and the way home): the post already sent,
    // which endHome() can still wait on.
    const now = Date.now();
    if (L.repeatsSave(state.lastSave, state.videoId, t, now)) return state.lastSave.request;
    const sent = { videoId: state.videoId, t, at: now, request: null };
    sent.request = fetch("/api/position", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_id: state.videoId, t, duration: playerDuration(), title: state.title || null }),
      keepalive: true,
    }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    }).catch(() => {
      if (state.lastSave === sent) state.lastSave = null; // not received: the next save posts it again
    });
    state.lastSave = sent;
    return sent.request;
  }

  // The portal's --watch-history: what our own player played goes to /api/watched, which reports
  // it to the viewer's YouTube history. YouTube's iframe player reports its own watches: nothing
  // from it. keepalive, as for the position.
  // The last touch or key on the page: YouTube's pings say how long ago it was (lact).
  let lastActivity = Date.now();
  for (const name of ["pointerdown", "keydown"]) {
    document.addEventListener(name, () => { lastActivity = Date.now(); }, { capture: true, passive: true });
  }

  // final: the viewing ends here (a pause, the end, the page hidden or left): YouTube gets this
  // report's ping at once, marked final. A later play goes on in the same session.
  function reportWatch(final = false) {
    if (final) {
      // Still playing (the page hidden, the player swapped): the schedule goes on; else a play restarts it.
      const video = state.player && state.player.video;
      state.watchDueAt = video && !video.paused && !video.ended ? Date.now() + state.watchGapMs : Infinity;
    }
    if (!state.info) return; // not known yet: the stretches wait
    if (state.info.watch_history !== "on") {
      state.watch = L.newWatchLog();
      return;
    }
    if (!state.player || state.player.kind !== "native" || state.tornDown) return;
    const video = state.player.video;
    const duration = playerDuration();
    if (!duration) return;
    const playing = !video.paused && !video.ended;
    const ranges = L.watchDrain(state.watch, video.currentTime, playing);
    if (!ranges.length) return;
    const sentAt = Date.now();
    if (!final) state.watchDueAt = sentAt + WATCH_UNANSWERED_MS;
    fetch("/api/watched", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_id: state.videoId, ranges, cmt: Math.min(video.currentTime, duration), duration,
        state: playing ? "playing" : "paused", final, lact: Date.now() - lastActivity, volume: Math.round(video.volume * 100),
        muted: video.muted, client: L.clientInfo(navigator.userAgent) }),
      keepalive: true,
    }).then((r) => r.json()).then((answer) => {
      const gap = L.watchNextMs(answer);
      if (gap === null) return;
      state.watchGapMs = gap;
      if (!final && state.watchDueAt !== Infinity) state.watchDueAt = sentAt + state.watchGapMs;
    }).catch(() => {});
  }

  function goHome() {
    savePosition(); // after endHome()'s save, the same position: nothing posted again (repeatsSave())
    reportWatch(true);
    teardownWatch();
    // Opened from the home page: going back pops that entry (and swipe-back keeps working). Opened
    // any other way (a link, the home-screen app's start, a reload), there is no such entry: load home.
    const ref = document.referrer ? new URL(document.referrer) : null;
    const fromHome = sessionStorage.getItem(FROM_HOME) === "1" || (ref && ref.origin === location.origin && !ref.searchParams.get("v"));
    if (fromHome && history.length > 1) {
      history.back();
      setTimeout(() => location.assign("./"), 1500); // back did nothing: load home after all
    } else {
      location.assign("./");
    }
  }

  // A video that played to its end goes home after a beat, its position the end, so its bar there
  // is full. Anything the viewer does in the beat keeps them here (cancelEndHome()), and so does a
  // sheet already open. Not for a live stream, and not for an "ended" short of the end (an HLS
  // stream may end a segment early): only the last END_SLOP_S counts (atEnd()). A scrub to the very
  // end is an end like any other.
  function scheduleEndHome() {
    cancelEndHome();
    if (!state.playerReady || (state.syncData && state.syncData.live)) return;
    if (!L.atEnd(state.player.getCurrentTime(), playerDuration())) return;
    if (state.sheet.open || $("settings").classList.contains("open") || document.visibilityState === "hidden") return;
    state.endTimer = setTimeout(endHome, END_HOME_MS);
  }

  // A touch or key in the beat keeps the viewer here, and that is all it does: the tap on the
  // video that stops the way home must not also restart the ended video from 0 and save that over
  // the end. play() swallows it; the viewer's next tap plays. Decided anew by every touch and key.
  function holdEnd() {
    state.endHeld = state.endTimer !== null || state.endWaiting;
    cancelEndHome();
  }

  function cancelEndHome() {
    clearTimeout(state.endTimer);
    state.endTimer = null;
    state.endGen++;
  }

  async function endHome() {
    state.endTimer = null;
    const gen = state.endGen;
    // The home list reads the positions as it loads: let the end's position land first (bounded,
    // so a slow computer only delays the way home). goHome() also flushes the watch report.
    const saved = savePosition();
    state.endWaiting = true;
    if (saved) await Promise.race([saved, new Promise((resolve) => setTimeout(resolve, 1000))]);
    state.endWaiting = false;
    // A touch, key, seek or play during that wait cancelled the beat: stay.
    if (gen !== state.endGen || state.tornDown || document.visibilityState === "hidden") return;
    goHome();
  }

  // Leaving the video: stop it and let go of its stream, so a page kept in the back-forward cache
  // neither plays nor downloads.
  function teardownWatch() {
    exitLandscape(); // leaving by any way (home, the end of the video, a link) restores the page
    clearTimeout(state.shieldTimer); // a lone tap's pending pause or controls toggle
    state.lastShieldTap = null;
    state.controlsHidden = false;
    if (!state.player || state.tornDown) return;
    state.tornDown = true;
    try {
      state.player.pauseVideo();
      if (state.player.kind === "native") state.player.destroy();
    } catch {
      // a player that is already gone
    }
  }

  // ------------------------------------------------------------ player

  async function startWatch() {
    $("home").hidden = true;
    $("watch").hidden = false;
    loadInfo().then(renderAnkiRow); // the language matters only once cues arrive, after the start below
    $("shield").style.setProperty("--poster", `url("/api/thumb/${state.videoId}")`);
    renderPlayState();
    applySettings();
    bindWatch();
    bindScrubber();
    bindSheet();
    bindSettings();
    setInterval(tick, 250);
    requestAnimationFrame(frame);
    // Any touch, click or key in the beat after the end keeps the viewer here.
    for (const name of ["pointerdown", "keydown"]) document.addEventListener(name, holdEnd, true);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        cancelEndHome();
        savePosition();
        reportWatch(true);
      }
    });
    window.addEventListener("pagehide", () => {
      savePosition();
      reportWatch(true);
      teardownWatch();
    });

    // Where to start first (a local file on the PC: milliseconds), then the stream, which the PC
    // resolves and pre-fetches at exactly that point.
    const saved = await fetch(`/api/position?video_id=${state.videoId}`).then((r) => r.json()).then((d) => d.position)
      .catch(() => recentList().find((r) => r.id === state.videoId) || null);
    state.startAt = L.resumeTime({ saved, duration: null, urlT: state.urlT });
    const media = await fetch(`/api/media?video_id=${state.videoId}&t=${Math.floor(state.startAt)}`).then((r) => r.json())
      .catch(() => ({ kind: "youtube", reason: "media endpoint unreachable" }));
    state.mediaDuration = media.duration || null;
    // The near-end rule needs the duration, which only the stream knew.
    state.startAt = L.resumeTime({ saved, duration: media.duration, urlT: state.urlT });
    // Only now does the server hear of the video: a /sync before this would name time 0, and the
    // server would transcribe the start of a video being resumed in the middle.
    state.t = state.clock.t = state.startAt;
    state.resumeTarget = state.startAt > 1 ? state.startAt : null;
    state.resumeLanded = state.resumeTarget === null;
    state.startKnown = true;
    sync();
    if (media.kind === "hls" && document.createElement("video").canPlayType("application/vnd.apple.mpegurl")) {
      state.player = createNativePlayer(media.url);
      state.playerReady = true;
    } else {
      useYouTube(media.reason || "this browser plays no HLS");
    }
  }

  // The same handful of methods as YouTube's player (playVideo, pauseVideo, seekTo,
  // getCurrentTime, getDuration, get/setPlaybackRate, getPlayerState) over our own <video>, so
  // sync, the sweep, auto-pause, mining and gestures never ask which one is playing.
  function createNativePlayer(src) {
    const video = document.createElement("video");
    video.id = "player";
    for (const name of ["playsinline", "webkit-playsinline", "disablepictureinpicture", "disableremoteplayback"]) video.setAttribute(name, "");
    video.setAttribute("x-webkit-airplay", "deny");
    video.playsInline = true;
    video.disablePictureInPicture = true;
    if ("disableRemotePlayback" in video) video.disableRemotePlayback = true;
    video.preload = "auto";
    video.poster = `/api/thumb/${state.videoId}`;
    let pendingSeek = state.startAt > 0 ? state.startAt : null;
    let lastState = -1;
    // The time of the frame on screen, from requestVideoFrameCallback where there is one.
    let shown = null;
    const onFrame = (now, meta) => {
      if (!video.seeking) shown = { mediaTime: meta.mediaTime, at: now }; // a frame from before the seek
      video.requestVideoFrameCallback(onFrame);
    };
    // A seek is checked once it should have landed: one the browser dropped (the playhead still
    // far from it, nothing seeking) is made again, once.
    let seekCheck = null;
    const checkSeek = (t, retried) => {
      clearTimeout(seekCheck);
      seekCheck = setTimeout(() => {
        if (video.seeking || Math.abs(video.currentTime - t) <= 1) return;
        console.log(`seek: at ${video.currentTime.toFixed(1)} s instead of ${t.toFixed(1)} s${retried ? "" : ", seeking again"}`);
        if (retried) return;
        shown = null;
        video.currentTime = t;
        checkSeek(t, true);
      }, 1000);
    };
    if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(onFrame);
    const emit = (s) => {
      lastState = s;
      onPlayerState(s);
    };
    video.addEventListener("loadedmetadata", () => {
      if (pendingSeek !== null) video.currentTime = pendingSeek;
      pendingSeek = null;
    });
    // iOS's native HLS may drop a seek made before the first play (and the playlist's EXT-X-START
    // may be ignored too): once playing, put the video where the resume meant it to be, once.
    let firstPlaying = true;
    video.addEventListener("playing", () => {
      if (firstPlaying) {
        firstPlaying = false;
        const target = state.resumeLanded ? null : state.resumeTarget;
        if (target !== null && Math.abs(video.currentTime - target) > 1.5) {
          console.log(`resume: at ${video.currentTime.toFixed(1)} s instead of ${target.toFixed(1)} s, seeking`);
          shown = null;
          video.currentTime = target;
        } else {
          state.resumeLanded = true;
        }
      }
      // A start or a resume (not the end of a stall): the schedule goes on from the server's last answer.
      if (state.watchDueAt === Infinity) state.watchDueAt = Date.now() + state.watchGapMs;
      emit(YT_PLAYING);
    });
    // The played stretches, once the resume has landed (before it the element may still say 0).
    video.addEventListener("timeupdate", () => {
      if (state.resumeLanded && !video.paused && !video.seeking) L.watchAt(state.watch, video.currentTime);
    });
    video.addEventListener("seeking", () => L.watchClose(state.watch));
    video.addEventListener("ratechange", () => L.watchClose(state.watch));
    for (const name of ["pause", "ended"]) {
      video.addEventListener(name, () => {
        L.watchClose(state.watch);
        reportWatch(true);
      });
    }
    video.addEventListener("seeked", () => {
      if (!state.resumeLanded && state.resumeTarget !== null && Math.abs(video.currentTime - state.resumeTarget) <= 1.5) state.resumeLanded = true;
    });
    video.addEventListener("pause", () => emit(YT_PAUSED));
    video.addEventListener("waiting", () => { if (!video.paused) emit(YT_BUFFERING); });
    video.addEventListener("ended", () => emit(YT_ENDED));
    video.addEventListener("seeked", () => { shown = null; });
    video.addEventListener("error", () => useYouTube(`stream error ${video.error ? video.error.code : "?"}`));
    video.src = src;
    $("player").replaceWith(video);
    return {
      kind: "native",
      video,
      playVideo() {
        const p = video.play();
        if (p && p.catch) p.catch((err) => console.log("video.play() refused:", err.name));
      },
      pauseVideo() { video.pause(); },
      seekTo(t) {
        shown = null;
        if (video.readyState >= 1) {
          video.currentTime = t;
          checkSeek(t, false);
        } else {
          pendingSeek = t;
        }
      },
      getCurrentTime() {
        if (pendingSeek !== null) return pendingSeek;
        // Before the resume lands the element may still say 0: the page shows where it will be.
        if (!state.resumeLanded && state.resumeTarget !== null && video.currentTime < 0.5) return state.resumeTarget;
        if (!shown || video.paused) return video.currentTime;
        const frame = shown.mediaTime + ((performance.now() - shown.at) / 1000) * video.playbackRate;
        return L.framePlayhead(frame, video.currentTime, { seeking: video.seeking });
      },
      getDuration() { return Number.isFinite(video.duration) ? video.duration : state.mediaDuration || 0; },
      setPlaybackRate(r) { video.playbackRate = r; },
      getPlaybackRate() { return video.playbackRate; },
      getPlayerState() {
        if (video.ended) return YT_ENDED;
        if (video.paused) return lastState === -1 ? -1 : YT_PAUSED;
        return video.readyState < 3 ? YT_BUFFERING : YT_PLAYING;
      },
      destroy() {
        video.removeAttribute("src");
        video.load();
      },
    };
  }

  // YouTube's own player: live streams, a stream we cannot resolve, a browser without HLS.
  function useYouTube(reason) {
    if (state.player && state.player.kind === "youtube") return;
    console.log("using YouTube's player:", reason);
    reportWatch(true); // what our own player played until now
    if (state.player) {
      state.startAt = state.everPlayed ? state.player.getCurrentTime() : state.startAt;
      state.player.destroy();
      const div = document.createElement("div");
      div.id = "player";
      $("player").replaceWith(div);
    }
    state.player = null;
    state.playerReady = false;
    state.everPlayed = false;
    state.ytState = -1;
    $("player-note").hidden = false;
    renderPlayState();
    window.onYouTubeIframeAPIReady = createYouTubePlayer;
    if (window.YT && window.YT.Player) createYouTubePlayer();
    else {
      const script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      document.head.append(script);
    }
  }

  function createYouTubePlayer() {
    const yt = new YT.Player("player", {
      videoId: state.videoId,
      // Our toolbar replaces YouTube's controls; nothing of YouTube's UI should ever show.
      playerVars: {
        controls: 0,
        rel: 0,
        iv_load_policy: 3,
        disablekb: 1,
        fs: 0,
        playsinline: 1,
        modestbranding: 1,
        cc_load_policy: 0,
        start: Math.floor(state.startAt || 0),
        origin: location.origin,
      },
      events: {
        onReady: () => {
          state.playerReady = true;
          yt.setPlaybackRate(settings.speed);
          if (state.startAt) yt.seekTo(state.startAt, true); // start= takes whole seconds only
        },
        onStateChange: (ev) => onPlayerState(ev.data),
      },
    });
    yt.kind = "youtube";
    yt.destroy = yt.destroy.bind(yt);
    state.player = yt;
  }

  function onPlayerState(s) {
    // A new pause or end shows the controls again, even after a tap hid them (landscape).
    if (s !== state.ytState && (s === YT_PAUSED || s === YT_ENDED)) state.controlsHidden = false;
    state.ytState = s;
    state.playing = s === YT_PLAYING || s === YT_BUFFERING;
    if (s === YT_PLAYING && state.player.kind === "youtube" && !state.resumeLanded && state.resumeTarget !== null) {
      // YouTube's start= takes whole seconds and may be ignored: check once it plays.
      if (Math.abs(state.player.getCurrentTime() - state.resumeTarget) > 1.5) state.player.seekTo(state.resumeTarget, true);
      state.resumeLanded = true;
    }
    if (s === YT_PLAYING) {
      state.pauseCause = null;
      state.everPlayed = true;
      state.player.setPlaybackRate(settings.speed);
    }
    if (s === YT_PAUSED || s === YT_ENDED) savePosition();
    if (s === YT_ENDED) scheduleEndHome();
    else cancelEndHome();
    renderPlayState();
    updateVeil();
    sync();
  }

  // Must run synchronously inside the tap handler that wants playback: iOS allows an unmuted
  // play only within a user gesture's call stack, so no await, timer or animation callback may
  // come between the tap and this call.
  function play() {
    if (!state.playerReady) return;
    if (state.endHeld) {
      state.endHeld = false; // this tap only kept the viewer here (holdEnd())
      return;
    }
    state.player.playVideo();
    setTimeout(() => {
      const ytState = state.player.getPlayerState();
      console.log("play() -> player state after 500 ms:", ytState);
      // The glyph follows the player, never what was asked: re-read it in case an event was missed.
      state.ytState = ytState;
      state.playing = ytState === YT_PLAYING || ytState === YT_BUFFERING;
      if (!state.playing) state.pauseCause = "user"; // refused or stalled: our scrim with a play glyph
      renderPlayState();
    }, 500);
  }

  function pause(cause) {
    if (!state.playerReady) return;
    state.pauseCause = cause;
    state.player.pauseVideo();
    renderPlayState();
  }

  function seek(t, andPlay) {
    if (!state.playerReady) return;
    state.resumeLanded = true; // the viewer chose a place: no resume correction, no saver guard
    cancelEndHome();
    state.autoPausedFor = null;
    state.player.seekTo(Math.max(0, t), true);
    state.t = Math.max(0, t);
    state.clock.t = state.t; // steadyTime then takes the new, earlier time as it is
    state.clock.raw = null;
    state.sweep.count = -1;
    // A playing video plays on from its new place; play() again right after the seek would only
    // be one more request racing it.
    if (andPlay && !state.playing) play();
    sync();
    savePosition();
  }

  function renderPlayState() {
    const button = $("btn-play");
    button.classList.toggle("playing", state.playing);
    button.setAttribute("aria-label", state.playing ? S.pause : S.play);
    const shield = $("shield");
    let look = "";
    if (!state.everPlayed) {
      // Before the first play: our poster. With YouTube's player it lets the tap through, since iOS
      // may only start that playback from a tap on YouTube's own frame; our own <video> starts from
      // the shield's tap (video.play() inside the gesture).
      look = state.player && state.player.kind === "youtube" ? "ended poster passthrough" : "ended poster";
    } else if (state.ytState === YT_ENDED || state.ytState === YT_CUED || state.ytState === -1) {
      look = "ended";
    } else if (state.ytState === YT_PAUSED) {
      look = state.pauseCause === "auto" || state.pauseCause === "sheet" ? "dim" : "paused";
    }
    shield.className = `shield ${look}`.trim();
    renderBack();
  }

  // The way home over the video: with the paused scrim, and for 3 s after any tap on the video.
  function renderBack() {
    const show = !state.controlsHidden && (state.ytState === YT_PAUSED || state.ytState === YT_ENDED || Date.now() < state.backUntil);
    document.querySelector(".player-box").classList.toggle("show-back", show);
  }

  // ------------------------------------------------------------ the landscape mode

  // The video as large as the screen allows, the line being said as a strip along its bottom.
  // Where a page may go fullscreen (Android, iPad, desktop) the whole page does, so the subtitles
  // and the dictionary sheet come along, and the screen is locked sideways where that is allowed.
  // An iPhone lets only a bare <video> go fullscreen, into Apple's player with nothing of ours over
  // it; there the mode is the page's own layout, turned a quarter (app.css html.turned) while the
  // phone is held upright, so the viewer just turns the phone, rotation lock or not.
  function canFullscreen() {
    const root = document.documentElement;
    return !!((document.fullscreenEnabled && root.requestFullscreen) || (document.webkitFullscreenEnabled && root.webkitRequestFullscreen));
  }

  function fullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  // Runs inside the tap: a fullscreen request needs the gesture.
  function enterLandscape() {
    if (state.landscape.on) return;
    state.landscape.on = true;
    document.documentElement.classList.add("switching", "landscape");
    if (canFullscreen()) {
      const root = document.documentElement;
      try {
        const asked = root.requestFullscreen ? root.requestFullscreen({ navigationUI: "hide" }) : root.webkitRequestFullscreen();
        if (asked && asked.catch) asked.catch((err) => console.log("fullscreen refused:", err.name));
      } catch (err) {
        console.log("fullscreen refused:", err.name); // the page-level layout stays
      }
    }
    modeChanged();
  }

  function exitLandscape() {
    if (!state.landscape.on) return;
    state.landscape.on = false;
    state.landscape.fullscreen = false;
    state.controlsHidden = false; // a side tap's hide belongs to the mode: portrait's back button shows again
    document.documentElement.classList.add("switching");
    document.documentElement.classList.remove("landscape", "turned");
    if (screen.orientation && screen.orientation.unlock) {
      try {
        screen.orientation.unlock();
      } catch {
        // never locked
      }
    }
    if (fullscreenElement()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      try {
        const left = exit.call(document);
        if (left && left.catch) left.catch(() => {});
      } catch {
        // already leaving
      }
    }
    modeChanged();
  }

  function onFullscreenChange() {
    if (fullscreenElement()) {
      if (!state.landscape.on) return;
      state.landscape.fullscreen = true;
      // Allowed only in fullscreen, and not everywhere then (a desktop, a tablet that refuses).
      if (screen.orientation && screen.orientation.lock) screen.orientation.lock("landscape").catch(() => {});
    } else if (state.landscape.fullscreen) {
      exitLandscape(); // the browser's own way out: the back gesture, Escape
    }
  }

  function modeChanged() {
    const b = $("btn-full");
    b.classList.toggle("on", state.landscape.on);
    b.setAttribute("aria-label", state.landscape.on ? S.exitFullscreen : S.fullscreen);
    exitBrowse(); // a browse offset is in the old layout's pixels
    viewChanged();
    renderBack();
  }

  // After the mode or the viewport changed: turned or not, the list re-centred without motion (the
  // same cue stays the focus, L.focusMotion), the scrubber and an open sheet measured again.
  function viewChanged() {
    const root = document.documentElement;
    // The closed sheets and the list jump to their new places instead of gliding across the
    // screen: the new layout is committed with transitions off ("switching", also set by
    // enterLandscape() and exitLandscape()), then they come back.
    const turned = state.landscape.on && L.shouldTurn(window.innerWidth, window.innerHeight);
    if (turned !== root.classList.contains("turned")) root.classList.add("switching");
    root.classList.toggle("turned", turned);
    // The turned body is the viewport the other way round, in the viewport's own px: dvh/dvw can
    // disagree with the layout viewport the fixed body fills (a browser bar, a stale value), and
    // then the whole view slid sideways, its top cut off and closed sheets peeking past its bottom.
    root.style.setProperty("--turn-w", turned ? `${window.innerHeight}px` : "");
    root.style.setProperty("--turn-h", turned ? `${window.innerWidth}px` : "");
    state.focusKey = null;
    if (state.sheet.open) capSheet(state.sheet.cue);
    measureScrubber();
    void document.body.offsetHeight;
    root.classList.remove("switching");
  }

  // Pointer events and getBoundingClientRect() speak in screen coordinates; gestures and layout
  // maths speak in the view's own frame, which the turned mode rotates (lib.js viewPoint()).
  function viewTurn() {
    return document.documentElement.classList.contains("turned") ? { height: document.body.clientHeight } : null;
  }

  function viewPoint(x, y) {
    return L.viewPoint(x, y, viewTurn());
  }

  function viewDelta(dx, dy) {
    return L.viewDelta(dx, dy, viewTurn());
  }

  function viewRect(el) {
    return L.viewRect(el.getBoundingClientRect(), viewTurn());
  }

  function viewHeight() {
    return state.landscape.on ? document.body.clientHeight : window.innerHeight;
  }

  // ------------------------------------------------------------ /sync

  async function sync() {
    if (state.syncInFlight || !state.videoId || !state.startKnown) return;
    state.syncInFlight = true;
    state.lastSyncAt = Date.now();
    let res;
    try {
      const deck = settings.wordColours ? { deck: { since: state.deck.token } } : {};
      res = await api("/api/sync", { video_id: state.videoId, t: state.t, since: state.since, paused: !state.playing, ...deck });
    } catch {
      res = null;
    } finally {
      state.syncInFlight = false;
    }
    if (!res || res.status !== 200 || !res.data || !res.data.ok) {
      // Network down, the portal or the server restarting, a 5xx: back off 1, 2, 4 … 15 s and keep
      // trying. The heartbeat never stops; the next success resets it.
      state.syncFailures += 1;
      state.syncRetryAt = Date.now() + L.backoffDelay(state.syncFailures - 1, L.SCHEDULES.sync);
      state.offline = true;
      state.syncError = (res && res.data && res.data.error) || (res ? `HTTP ${res.status}` : "network");
      renderServerStatus();
      return;
    }
    state.syncFailures = 0;
    state.offline = false;
    state.syncError = null;
    state.syncReceivedAt = Date.now();
    const data = res.data;
    if (typeof data.session === "string" && data.session !== state.session) {
      const restarted = state.session !== null;
      state.session = data.session;
      if (restarted) {
        // A fresh server session numbers its cues from 0 again: start the transcript over.
        resetCues();
        state.syncData = data;
        sync();
        return;
      }
    }
    state.syncData = data;
    if (data.title && data.title !== state.title) {
      state.title = data.title;
      document.title = data.title;
      remember();
    }
    if (data.deck) takeDeck(data.deck);
    if (Array.isArray(data.cues) && data.cues.length) mergeCues(data.cues);
    if (typeof data.next === "number") state.since = data.next;
    renderServerStatus();
  }

  // ------------------------------------------------------------ word colours

  // The server's deck rows in, the index rebuilt when the entries changed, every row drawn again.
  function takeDeck(answer) {
    state.deck = L.applyDeck(state.deck, answer);
    const entries = state.deck.status === "ok" ? L.deckEntries(state.deck.rows.values()) : [];
    const key = JSON.stringify(entries);
    if (key === state.wordsKey) return;
    state.wordsKey = key;
    state.wordIndex = entries.length ? SHISUKO_WORDS.buildIndex(entries, []) : null;
    redrawRows();
  }

  // One status per character of a cue, or null without colours.
  function cueMarks(cue) {
    if (!settings.wordColours || !state.wordIndex) return null;
    return L.statusAt(SHISUKO_WORDS.markWords(cue.text, state.wordIndex));
  }

  function redrawRows() {
    for (const cue of state.cues) {
      const row = state.rowById.get(cue.id);
      if (row) fillCue(row, cue);
    }
    state.focusKey = null;
  }

  function renderServerStatus() {
    const { text } = L.statusText({ offline: state.offline, error: state.syncError, data: state.syncData, t: state.t, cueCount: state.cues.length });
    if ($("set-status").textContent !== text) $("set-status").textContent = text;
  }

  function resetCues() {
    state.cues = [];
    state.cueById.clear();
    state.cueIndex.clear();
    state.tokens.clear();
    state.tokenWanted.clear();
    state.since = 0;
    state.activeIdx = -1;
    state.rowById.clear();
    state.renderedCount = 0;
    state.focusKey = null;
    state.focusPos = null;
    state.sweep.cueId = null;
    $("lyrics-track").replaceChildren();
  }

  function mergeCues(incoming) {
    let added = false;
    let inOrder = true;
    let lastStart = state.cues.length ? state.cues[state.cues.length - 1].start : -Infinity;
    for (const raw of incoming) {
      if (!raw || typeof raw.text !== "string") continue;
      const cue = { id: Number(raw.id), start: Number(raw.start), end: Number(raw.end), text: raw.text.trim() };
      if (!cue.text || !Number.isFinite(cue.id) || state.cueById.has(cue.id)) continue;
      if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.end < cue.start) continue;
      // Real word times, should the server ever send them, replace the estimate (lib.js unitTimes).
      if (Array.isArray(raw.words)) cue.words = raw.words.map((w) => ({ start: Number(w.start), end: Number(w.end), text: String(w.text || "") }));
      if (cue.start <= lastStart) inOrder = false;
      lastStart = cue.start;
      state.cueById.set(cue.id, cue);
      state.cues.push(cue);
      state.tokenWanted.add(cue.id);
      added = true;
    }
    if (!added) return;
    if (!inOrder) state.cues.sort((a, b) => a.start - b.start || a.end - b.end);
    state.cueIndex.clear();
    state.cues.forEach((c, i) => state.cueIndex.set(c.id, i));
    state.activeIdx = L.activeCueIndex(state.cues, state.t);
    if (inOrder) {
      renderRows();
    } else {
      // Earlier lines arrived (the server fills in windows before the playhead): slot their rows in
      // among the existing ones, which keep their elements and styles, so the line being said does
      // not start over; the frame loop then re-anchors the list without motion.
      placeRows();
      state.focusPos = null; // indices shifted: restyle every row (unchanged rows do not animate)
    }
    state.focusKey = null; // re-place and restyle on the next frame
    pumpTokens();
  }

  // ------------------------------------------------------------ tokens

  // Tokenise the cues nearest the playhead first, a batch at a time, one request in flight.
  async function pumpTokens() {
    if (state.tokenizing || !state.tokenWanted.size) return;
    state.tokenizing = true;
    try {
      while (state.tokenWanted.size) {
        const near = Math.max(0, state.activeIdx);
        const batch = [...state.tokenWanted]
          .map((id) => state.cueById.get(id))
          .filter(Boolean)
          .sort((a, b) => Math.abs(state.cueIndex.get(a.id) - near) - Math.abs(state.cueIndex.get(b.id) - near))
          .slice(0, TOKENIZE_BATCH);
        if (!batch.length) {
          state.tokenWanted.clear();
          break;
        }
        const res = await L.withRetry(() => api("/api/tokenize", { texts: batch.map((c) => c.text.slice(0, 300)) }), L.SCHEDULES.lookup);
        if (res.status !== 200 || !res.data || !res.data.ok) {
          // Yomitan unreachable (Firefox closed?) after the retries: these lines become tappable per
          // character (a tap scans forward from there, as Yomitan does); tokenising resumes later.
          for (const cue of batch) {
            if (state.tokenFailed.has(cue.id)) continue;
            state.tokenFailed.add(cue.id);
            // Without Yomitan, a language with spaces between words still splits into words.
            if (!isJa()) state.tokens.set(cue.id, L.tokenOffsets(L.segmentWords(cue.text, state.lang)));
            const row = state.rowById.get(cue.id);
            if (row) fillCue(row, cue);
          }
          setTimeout(pumpTokens, 10000);
          break;
        }
        let refocus = false;
        batch.forEach((cue, i) => {
          state.tokenWanted.delete(cue.id);
          let tokens = res.data.tokens[i];
          if (!Array.isArray(tokens)) return;
          // Yomitan answers a language it has no dictionaries for with no words: split it ourselves.
          if (!L.tokensUseful(tokens)) tokens = L.segmentWords(cue.text.slice(0, 300), state.lang);
          const withOffsets = L.tokenOffsets(tokens);
          // Offsets index the cue text, so a reply that does not add up to it is not used.
          if (withOffsets.map((t) => t.text).join("") !== cue.text.slice(0, 300)) return;
          state.tokens.set(cue.id, withOffsets);
          state.tokenFailed.delete(cue.id);
          const row = state.rowById.get(cue.id);
          if (row) {
            fillCue(row, cue);
            refocus = true;
          }
        });
        // Tokens change how a row wraps, so rows above the line being said may change height:
        // re-anchor without motion (one layout read per batch).
        if (refocus) state.reanchor = true;
      }
    } finally {
      state.tokenizing = false;
    }
  }

  // ------------------------------------------------------------ the subtitle list

  function newRow(cue) {
    const row = document.createElement("div");
    row.className = "cue future";
    row.dataset.cue = String(cue.id);
    fillCue(row, cue);
    state.rowById.set(cue.id, row);
    return row;
  }

  // Every cue's row in sorted order, creating only the missing ones and moving nothing that is
  // already in place.
  function placeRows() {
    const track = $("lyrics-track");
    let prev = null;
    for (const cue of state.cues) {
      const row = state.rowById.get(cue.id) || newRow(cue);
      const inPlace = prev ? prev.nextElementSibling === row : track.firstElementChild === row;
      if (!inPlace) {
        if (prev) prev.after(row);
        else track.prepend(row);
      }
      prev = row;
    }
    state.renderedCount = state.cues.length;
  }

  function renderRows() {
    const track = $("lyrics-track");
    for (let i = state.renderedCount; i < state.cues.length; i++) track.append(newRow(state.cues[i]));
    state.renderedCount = state.cues.length;
  }

  // A cue's text as one span per unit (a Yomitan token, or a character until tokens arrive), so the
  // sweep can light units one by one. Units with a headword are tappable words. Every piece goes in
  // as a text node; no markup from the server is parsed here.
  function fillCue(row, cue) {
    row.replaceChildren();
    const ruby = settings.furigana && isJa();
    row.classList.toggle("furigana", ruby);
    const scan = !state.tokens.has(cue.id) && state.tokenFailed.has(cue.id);
    const marks = cueMarks(cue);
    for (const unit of L.cueUnits(cue.text, state.tokens.get(cue.id))) {
      if (scan && L.hasKanji(unit.text) || scan && /[ぁ-ゖァ-ヺ]/.test(unit.text)) unit.term = "?"; // a scan start
      // A break opportunity before every unit but punctuation, which stays with the word before it.
      if (row.childNodes.length && !/^[、。，．,.!?！？…‥」』）)]/.test(unit.text)) row.append(document.createElement("wbr"));
      const span = document.createElement("span");
      span.className = unit.term ? "tok word" : "tok";
      if (unit.term === "?") span.dataset.scan = "1";
      span.dataset.start = String(unit.start);
      span.dataset.end = String(unit.end);
      const pieces = marks ? L.markedSegs(unit, marks) : unit.segs.map(([text, reading]) => ({ text, reading, status: null }));
      for (const { text, reading, status } of pieces) {
        let node = text;
        if (ruby && reading && L.hasKanji(text)) {
          node = document.createElement("ruby");
          node.append(text);
          const rt = document.createElement("rt");
          rt.textContent = reading;
          node.append(rt);
        }
        if (status) {
          const mark = document.createElement("span");
          mark.className = "wc";
          mark.dataset.status = status;
          mark.append(node);
          node = mark;
        }
        span.append(node);
      }
      row.append(span);
    }
    if (state.sweep.cueId === cue.id) state.sweep.cueId = null; // its spans are new: set up again
  }

  // Where the list is centred, as a cue index: the cue being said, the looked-up cue while the sheet
  // is open, or half a step after the last cue when the status takes the centre slot.
  function currentFocus() {
    if (state.sheet.open) return { pos: state.cueIndex.get(state.sheet.cue.id) ?? -1, status: null };
    if (state.previewT !== null) return { pos: L.activeCueIndex(state.cues, state.previewT), status: null };
    const idx = state.activeIdx;
    const cue = state.cues[idx];
    const live = cue && state.t <= cue.end + LINE_HOLD_S;
    const status = live ? null : L.watchStatus({ offline: state.offline, error: state.syncError, data: state.syncData, t: state.t, receivedAt: state.syncReceivedAt, now: Date.now() });
    return { pos: status ? idx + 0.5 : idx, status };
  }

  // Restyle the cues around the focus and glide the list so the focus sits at the centre point.
  // Runs only when the focus changes (or the sheet resizes): one layout read, then transforms.
  function applyFocus(focus, animate) {
    const old = state.focusPos;
    state.focusPos = focus.pos;
    const lo = Math.max(0, Math.floor(Math.min(old ?? focus.pos, focus.pos)) - 3);
    const hi = Math.min(state.cues.length - 1, Math.ceil(Math.max(old ?? focus.pos, focus.pos)) + 3);
    const touched = old === null ? [0, state.cues.length - 1] : [lo, hi];
    for (let i = touched[0]; i <= touched[1]; i++) styleRow(i, focus.pos);

    const status = $("lyrics-status");
    status.hidden = !focus.status;
    if (focus.status) {
      status.replaceChildren();
      if (focus.status.busy) {
        const spin = document.createElement("span");
        spin.className = "spinner";
        status.append(spin);
      }
      status.append(focus.status.text);
    }
    placeTrack(animate);
    updateVeil();
  }

  function styleRow(i, pos) {
    const cue = state.cues[i];
    const row = cue && state.rowById.get(cue.id);
    if (!row) return;
    const dist = Math.ceil(Math.abs(i - pos) - 1e-6);
    if (dist <= 2) row.dataset.d = String(dist);
    else delete row.dataset.d;
    row.classList.toggle("past", i < pos);
    row.classList.toggle("active", i === pos);
    row.classList.toggle("future", i > pos);
    row.classList.toggle("nosweep", i === pos && !settings.sweep);
  }

  // The centre point: the middle of the list, or of the room left above the dictionary sheet.
  function focusY() {
    const box = $("lyrics");
    // The landscape mode: the strip's bottom, where the line's box rests (placeTrack()).
    if (state.landscape.on) return box.clientHeight;
    // The scrubber covers the list's faded bottom edge: centre in what is left above it.
    if (!state.sheet.open) return (box.clientHeight - $("scrubber").offsetHeight) / 2;
    const sheetTop = viewHeight() - $("sheet").offsetHeight;
    const room = sheetTop - viewRect(box).top;
    return Math.max(40, Math.min(box.clientHeight / 2, room / 2));
  }

  function placeTrack(animate = true) {
    const pos = state.focusPos;
    const centre = focusY();
    let anchor = 0;
    const whole = Number.isInteger(pos);
    const row = pos >= 0 ? state.rowById.get(state.cues[Math.floor(pos)]?.id) : null;
    // The landscape mode anchors the line's bottom, so a second row grows upward, as on a TV.
    if (row && whole) anchor = row.offsetTop + (state.landscape.on ? row.offsetHeight : row.offsetHeight / 2);
    else if (row) anchor = row.offsetTop + row.offsetHeight + 40; // the status slot after the last cue
    else anchor = -40; // nothing said yet: the first cue waits just below the centre
    state.trackBase = centre - anchor;
    $("lyrics-status").style.top = `${centre}px`;
    setTrack(animate);
  }

  // `animate` false re-anchors in the same frame: no transition, so content that changed above the
  // line being said (cues filled in, tokens re-measured) never shows as the list moving.
  function setTrack(animate = true) {
    const track = $("lyrics-track");
    if (state.browse.active) {
      // Browsing stops at the ends of the list.
      const min = focusY() - track.offsetHeight;
      // Bottom-anchored (landscape), the first line's box stops at the anchor, not its top.
      const max = focusY() - (state.landscape.on && track.firstElementChild ? track.firstElementChild.offsetHeight : 0);
      state.browse.offset = Math.min(max - state.trackBase, Math.max(min - state.trackBase, state.browse.offset));
    }
    const value = `translateY(${Math.round(state.trackBase + state.browse.offset)}px)`;
    if (track.style.transform === value) return;
    // A re-anchor during a glide retargets the glide instead of cutting it short.
    if (animate || performance.now() < state.glideUntil) {
      if (animate) state.glideUntil = performance.now() + GLIDE_MS;
      track.style.transform = value;
      return;
    }
    track.style.transition = "none";
    track.style.transform = value;
    void track.offsetHeight; // commit the jump before the transition comes back
    track.style.transition = "";
  }

  // The word sweep inside the cue being said: units turn white as their (estimated) time comes.
  // Touches the DOM only when the count of spoken units changes.
  function sweep(t) {
    const pos = state.focusPos;
    if (!settings.sweep || state.sheet.open || !Number.isInteger(pos) || pos < 0) return;
    const cue = state.cues[pos];
    const sw = state.sweep;
    if (sw.cueId !== cue.id) {
      const row = state.rowById.get(cue.id);
      if (!row) return;
      sw.cueId = cue.id;
      sw.spans = [...row.querySelectorAll(".tok")];
      sw.times = L.unitTimes(cue, L.cueUnits(cue.text, state.tokens.get(cue.id)), state.lang);
      sw.count = -1;
      debug.sources[cue.words && cue.words.length ? "words" : "estimate"] += 1;
      debug.lastSource = cue.words && cue.words.length ? "words" : "estimate";
    }
    const n = L.spokenCount(sw.times, t);
    if (n === sw.count) return;
    const from = Math.max(0, Math.min(n, sw.count < 0 ? 0 : sw.count) - 1);
    const to = sw.count < 0 ? sw.spans.length : Math.min(sw.spans.length, Math.max(n, sw.count) + 1);
    for (let i = from; i < to; i++) {
      sw.spans[i].classList.toggle("spoken", i < n);
      sw.spans[i].classList.toggle("now", i === n - 1);
    }
    sw.count = n;
  }

  // Listening mode: the cue being said and the ones after it stay hidden while the video plays.
  function updateVeil() {
    $("lyrics").classList.toggle("veiled", settings.listening && state.playing && !state.sheet.open);
    $("lyrics").classList.toggle("sheet-open", state.sheet.open);
  }

  // ---- browsing the list by hand

  function enterBrowse() {
    if (state.browse.active) return;
    state.browse.active = true;
    state.browse.offset = 0;
    $("lyrics").classList.add("browsing");
    $("back-to-now").hidden = false;
  }

  function exitBrowse() {
    clearTimeout(state.browse.timer);
    if (!state.browse.active) return;
    state.browse.active = false;
    state.browse.offset = 0;
    $("lyrics").classList.remove("browsing", "dragging");
    $("back-to-now").hidden = true;
    setTrack();
  }

  function browseIdle() {
    clearTimeout(state.browse.timer);
    state.browse.timer = setTimeout(exitBrowse, BROWSE_IDLE_MS);
  }

  // ------------------------------------------------------------ the clock and the frame loop

  // YouTube's API reports the time in steps of a few hundred milliseconds; between reports the clock
  // runs on at the playback rate, so the sweep moves smoothly. A seek shows up as a jump and resets it.
  function smoothTime(now) {
    const c = state.clock;
    if (!state.playerReady || !state.player) return state.startAt || 0;
    const raw = state.player.getCurrentTime();
    if (!Number.isFinite(raw)) return c.t;
    if (state.player.kind === "native") return (c.t = L.steadyTime(c.t, raw, state.playing)); // exact, kept monotonic
    if (raw !== c.raw) {
      c.raw = raw;
      c.rawAt = now;
    }
    let t = raw;
    if (state.playing) {
      t = raw + Math.min(1, ((now - c.rawAt) / 1000) * (state.player.getPlaybackRate() || 1));
      if (t < c.t && c.t - t < 0.3) t = c.t; // no small steps backwards between reports
    }
    c.t = t;
    return t;
  }

  function frame(now) {
    state.t = smoothTime(now);
    const idx = L.activeCueIndex(state.cues, state.t);
    const cue = idx >= 0 ? state.cues[idx] : null;
    // Auto-pause: once at the end of each line, never again for the same line.
    if (settings.autoPause && state.playing && cue && !state.sheet.open &&
        state.t >= cue.end - 0.08 && state.t < cue.end + 0.6 && state.autoPausedFor !== cue.id) {
      state.autoPausedFor = cue.id;
      pause("auto");
    }
    state.activeIdx = idx;
    const focus = currentFocus();
    const key = `${focus.pos}|${focus.status ? focus.status.text : ""}|${state.sheet.open}`;
    if (key !== state.focusKey) {
      state.focusKey = key;
      const id = L.focusIdentity(state.cues, focus.pos);
      // Only a different cue is movement; the same cue at another index is content shifting.
      const animate = L.focusMotion(state.focusId, id) === "glide";
      state.focusId = id;
      applyFocus(focus, animate);
      state.reanchor = false;
    } else if (state.reanchor) {
      state.reanchor = false;
      placeTrack(false); // rows above changed height (tokens arrived): keep the line where it is
    }
    sweep(state.t);
    renderScrubber(state.t);
    requestAnimationFrame(frame);
  }

  // Server sync, the resume point: no need to run every frame.
  function tick() {
    renderBuffered();
    if (state.syncFailures) {
      if (Date.now() >= state.syncRetryAt) sync();
    } else {
      // An errored session keeps being heartbeated too: the server retries on a later sync.
      const due = state.playing ? SYNC_PLAYING_MS : SYNC_PAUSED_MS;
      if (Date.now() - state.lastSyncAt >= due) sync();
    }
    if (state.playing && Date.now() - state.lastSavedAt > 5000) {
      state.lastSavedAt = Date.now();
      savePosition();
    }
    if (state.playing && Date.now() >= state.watchDueAt) reportWatch();
  }

  // ------------------------------------------------------------ line navigation

  // The line being said, from the player's own playhead rather than the frame clock.
  function lineNow() {
    return L.activeCueIndex(state.cues, state.playerReady ? state.player.getCurrentTime() : state.t);
  }

  // Back always means the line before, wherever the playhead sits in this one (as in the
  // extension). Nothing happens at either end.
  function stepLine(delta) {
    const { idx, moved } = L.stepTarget(state.cues.length, lineNow(), delta);
    if (moved) seek(state.cues[idx].start, true);
  }

  // The controls (back, exit, scrubber) for 3 s, as after any tap on the video.
  function showControls() {
    state.controlsHidden = false;
    state.backUntil = Date.now() + 3000;
    setTimeout(renderBack, 3050);
    renderBack();
  }

  // Hidden until the next tap, pause or end shows them (a side tap in the landscape mode).
  function hideControls() {
    state.controlsHidden = true;
    state.backUntil = 0;
    renderBack();
  }

  // The double tap's badge on the tapped side: how many lines the run has stepped.
  function showStepBadge(zone, steps, landscape) {
    const badge = $("step-badge");
    // Exactly the side zone the taps landed in (L.tapZone()).
    badge.style.width = `${L.sideShare(landscape ? L.TAP_MIDDLE.landscape : L.TAP_MIDDLE.portrait) * 100}%`;
    badge.textContent = zone === "left" ? S.linesBack(steps) : S.linesForward(steps);
    badge.dataset.side = zone;
    badge.classList.remove("show");
    void badge.offsetWidth; // restart the fade for every tap of the run
    badge.classList.add("show");
  }

  function replayLine() {
    const cue = state.cues[state.activeIdx];
    if (cue) seek(cue.start, true);
  }

  function togglePlay() {
    if (state.playing) {
      pause("user");
    } else {
      state.autoPausedFor = state.cues[state.activeIdx] ? state.cues[state.activeIdx].id : null;
      play();
    }
  }

  // ------------------------------------------------------------ watch view input

  function bindWatch() {
    $("btn-prev").addEventListener("click", () => stepLine(-1));
    $("btn-next").addEventListener("click", () => stepLine(1));
    $("btn-replay").addEventListener("click", replayLine);
    $("btn-play").addEventListener("click", togglePlay);
    $("btn-settings").addEventListener("click", openSettings);
    $("back-to-now").addEventListener("click", exitBrowse);
    $("btn-home").addEventListener("click", (ev) => {
      ev.stopPropagation();
      goHome();
    });
    $("btn-full").addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (state.landscape.on) exitLandscape();
      else enterLandscape();
      showControls(); // the controls stay up a moment in the new layout
    });
    for (const name of ["fullscreenchange", "webkitfullscreenchange"]) document.addEventListener(name, onFullscreenChange);
    window.addEventListener("orientationchange", viewChanged);

    // The shield over the video, as in YouTube's app (lib.js shieldTap()): the middle plays or
    // pauses; a double tap on a side steps a line, each further tap one more. A lone side tap
    // pauses in portrait, and shows or hides the controls in the landscape mode.
    $("shield").addEventListener("click", (ev) => {
      const landscape = state.landscape.on;
      const zone = L.tapZone(viewPoint(ev.clientX, ev.clientY).x, viewRect(ev.currentTarget), landscape ? L.TAP_MIDDLE.landscape : L.TAP_MIDDLE.portrait);
      const now = Date.now();
      const tap = L.shieldTap(state.lastShieldTap, { at: now, zone, playing: state.playing }, { landscape });
      state.lastShieldTap = tap.next;
      clearTimeout(state.shieldTimer);
      if (tap.action === "step") {
        if (tap.steps === 1) {
          state.stepBase = lineNow(); // the run counts from here
          state.stepIdx = null;
        }
        const { idx, moved } = L.stepTarget(state.cues.length, state.stepBase, tap.delta);
        if (!moved) return; // no line that way: nothing seeks, no badge claims a step
        if (idx !== state.stepIdx) seek(state.cues[idx].start, true); // past the end, taps repeat nothing
        state.stepIdx = idx;
        showStepBadge(zone, moved, landscape);
        return;
      }
      if (tap.action === "controls-later") {
        state.shieldTimer = setTimeout(() => {
          state.lastShieldTap = null;
          if ($("btn-home").closest(".show-back")) hideControls();
          else showControls();
        }, L.DOUBLE_TAP_MS);
        return;
      }
      if (tap.action === "pause-later") {
        // Pausing needs no gesture: wait out the window for a second tap.
        state.shieldTimer = setTimeout(() => {
          state.lastShieldTap = null;
          if (state.playing) pause("user");
          showControls();
        }, L.DOUBLE_TAP_MS);
        return;
      }
      showControls();
      togglePlay(); // inside the tap: iOS plays only from a gesture's own call stack
    });

    const lyrics = $("lyrics");
    // A word in any cue opens the sheet; a tap elsewhere on a cue that is not the focus seeks there.
    lyrics.addEventListener("click", (ev) => {
      if (Date.now() - state.gestureAt < 400) return; // the end of a drag or swipe, not a tap
      const row = ev.target.closest(".cue");
      if (!row) return;
      const cue = state.cueById.get(Number(row.dataset.cue));
      if (!cue) return;
      const word = ev.target.closest(".word");
      if (word) {
        // Japanese looks up from the character under the finger, as Yomitan's scan does, so a
        // token the parser started early (a particle glued on) cannot shift the word.
        const scan = isJa() || !!word.dataset.scan;
        const from = scan ? tappedIndex(word, ev.target, ev.clientX, ev.clientY) : Number(word.dataset.start);
        openSheet(cue, [from, Number(word.dataset.end)], scan);
      } else if (!row.classList.contains("active")) {
        exitBrowse();
        seek(cue.start, state.playing || state.pauseCause !== "user");
      }
    });

    // A vertical drag browses the list; a horizontal swipe steps a line.
    let touch = null;
    lyrics.addEventListener("touchstart", (ev) => {
      if (state.sheet.open || Date.now() - state.sheetClosedAt < L.GHOST_CLICK_MS) {
        touch = null; // the sheet's own dismissal, not a swipe or a browse
        return;
      }
      const p = ev.touches[0];
      touch = { x: p.clientX, y: p.clientY, axis: null, offset: state.browse.offset };
    }, { passive: true });
    lyrics.addEventListener("touchmove", (ev) => {
      if (!touch) return;
      const p = ev.touches[0];
      const { dx, dy } = viewDelta(p.clientX - touch.x, p.clientY - touch.y);
      if (!touch.axis && Math.max(Math.abs(dx), Math.abs(dy)) > 8) touch.axis = Math.abs(dy) > Math.abs(dx) ? "y" : "x";
      if (touch.axis === "y" && !state.sheet.open) {
        if (!state.browse.active) {
          enterBrowse();
          touch.offset = 0;
        }
        lyrics.classList.add("dragging");
        state.browse.offset = touch.offset + dy;
        setTrack();
        clearTimeout(state.browse.timer);
      }
      if (touch.axis) ev.preventDefault();
    }, { passive: false });
    lyrics.addEventListener("touchend", (ev) => {
      if (!touch) return;
      const p = ev.changedTouches[0];
      const { dx } = viewDelta(p.clientX - touch.x, p.clientY - touch.y);
      const { axis } = touch;
      touch = null;
      lyrics.classList.remove("dragging");
      if (axis) state.gestureAt = Date.now();
      if (axis === "y" && state.browse.active) browseIdle();
      if (axis === "x" && Math.abs(dx) > 60 && !state.sheet.open) stepLine(dx < 0 ? 1 : -1);
    }, { passive: true });
    lyrics.addEventListener("wheel", (ev) => {
      if (state.sheet.open) return;
      ev.preventDefault();
      enterBrowse();
      lyrics.classList.add("dragging");
      state.browse.offset -= ev.deltaY;
      setTrack();
      clearTimeout(state.wheelTimer);
      state.wheelTimer = setTimeout(() => lyrics.classList.remove("dragging"), 120);
      browseIdle();
    }, { passive: false });

    window.addEventListener("resize", viewChanged);
    // iOS reports a browser bar coming or going here, not always as a window resize.
    if (window.visualViewport) window.visualViewport.addEventListener("resize", viewChanged);
  }

  // ------------------------------------------------------------ the word sheet

  // Content-sized, but always leaving room above it for the tapped cue.
  function capSheet(cue) {
    if (state.landscape.on) {
      $("sheet").style.maxHeight = ""; // a side panel, the view's full height
      return;
    }
    const row = state.rowById.get(cue.id);
    const top = viewRect($("lyrics")).top;
    const room = viewHeight() - top - (row ? row.offsetHeight : 80) - 40;
    $("sheet").style.maxHeight = `${Math.max(240, room)}px`;
  }

  // A token's text nodes with the cue offset each starts at. Furigana (<rt>) is not cue text.
  function* tokenText(tok) {
    let at = Number(tok.dataset.start);
    const walk = document.createTreeWalker(tok, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      const node = walk.currentNode;
      if (node.parentElement.closest("rt")) continue;
      yield { node, at };
      at += node.data.length;
    }
  }

  // The cue offset of the character under a tap on a word: the glyph whose box holds the point,
  // or the nearest one; a tap on furigana picks among its ruby base. Glyph boxes, not
  // caretPositionFromPoint(): a caret is the nearest boundary, so the right half of a glyph would
  // answer the next one.
  function tappedIndex(tok, target, x, y) {
    const rt = target.closest("rt");
    const scope = rt && tok.contains(rt) ? rt.parentElement : tok;
    const offsets = [];
    const boxes = [];
    const range = document.createRange();
    for (const { node, at } of tokenText(tok)) {
      if (!scope.contains(node)) continue;
      let i = 0;
      for (const ch of node.data) {
        range.setStart(node, i);
        range.setEnd(node, i + ch.length);
        offsets.push(at + i);
        boxes.push(range.getBoundingClientRect());
        i += ch.length;
      }
    }
    const pick = L.nearestBox(boxes, x, y);
    return pick < 0 ? Number(tok.dataset.start) : offsets[pick];
  }

  // Lights the span's characters exactly (a scan from inside a token covers part of it), or,
  // without the CSS Highlight API, every unit the span touches.
  function markPicked(cue, span) {
    document.querySelectorAll("#lyrics .picked").forEach((el) => el.classList.remove("picked"));
    const exact = typeof Highlight === "function" && window.CSS && CSS.highlights;
    if (exact) CSS.highlights.delete("picked");
    const row = cue && state.rowById.get(cue.id);
    if (!row) return;
    const ranges = [];
    for (const tok of row.querySelectorAll(".tok")) {
      if (Number(tok.dataset.start) >= span[1] || Number(tok.dataset.end) <= span[0]) continue;
      if (!exact) {
        tok.classList.add("picked");
        continue;
      }
      for (const { node, at } of tokenText(tok)) {
        const from = Math.max(span[0], at) - at;
        const to = Math.min(span[1], at + node.data.length) - at;
        if (from >= to) continue;
        const range = document.createRange();
        range.setStart(node, from);
        range.setEnd(node, to);
        ranges.push(range);
      }
    }
    if (exact && ranges.length) CSS.highlights.set("picked", new Highlight(...ranges));
  }

  async function openSheet(cue, span, scan = false) {
    const s = state.sheet;
    const seq = ++s.seq;
    if (!s.open) {
      s.openedAt = state.player ? state.player.getCurrentTime() : state.t; // the playhead at the tap
      s.pausedIt = settings.pauseOnLookup && state.playing;
      if (s.pausedIt) pause("sheet");
    }
    s.open = true;
    s.cue = cue;
    s.tapped = span; // each entry's span starts here (entrySpan())
    s.scan = scan;
    s.span = span;
    s.entries = [];
    s.pick = 0;
    s.def = null;
    exitBrowse();
    markPicked(cue, span);
    capSheet(cue);
    state.focusKey = null; // the list centres the tapped cue in the room above the sheet
    updateVeil();
    const sheet = $("sheet");
    sheet.style.transform = "";
    sheet.classList.add("open");
    sheet.setAttribute("aria-hidden", "false");
    // A scan looks up the text from the tapped character on, like Yomitan's; the picked entry's
    // match then becomes the span (the longest, until another is picked).
    const surface = scan ? cue.text.slice(span[0], span[0] + 12) : cue.text.slice(span[0], span[1]);
    $("sheet-headword").textContent = cue.text.slice(span[0], span[1]);
    $("sheet-reading").replaceChildren();
    $("sheet-freq").textContent = "";
    $("sheet-audio").hidden = true;
    $("sheet-candidates").replaceChildren();
    $("sheet-scroll").scrollTop = 0;
    setMineButton(null, true);
    setSheetMessage(S.lookingUp);

    const res = await L.withRetry(() => api("/api/lookup", { text: surface }), L.SCHEDULES.lookup);
    if (seq !== s.seq) return; // another word was tapped meanwhile
    if (res.status !== 200 || !res.data || !res.data.ok) {
      setSheetMessage(res.status === 503 || !res.data ? S.yomitanDown : res.data.error || S.yomitanDown,
        res.status === 503 || !res.data ? { href: "https://github.com/yomidevs/yomitan-api", text: S.yomitanDocs } : null);
      return;
    }
    s.entries = res.data.entries || [];
    if (!s.entries.length) {
      setSheetMessage(S.noEntry);
      return;
    }
    renderCandidates();
    showEntry(0);
  }

  function setSheetMessage(text, link = null) {
    const p = document.createElement("p");
    p.className = "sheet-state";
    p.textContent = text;
    if (link) {
      const a = document.createElement("a");
      a.href = link.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = link.text;
      p.append(document.createElement("br"), a);
    }
    $("sheet-body").replaceChildren(p);
  }

  function setMineButton(inDeck, disabled) {
    const b = $("sheet-mine");
    b.disabled = !!disabled;
    b.classList.toggle("again", inDeck === true);
    b.textContent = inDeck === true ? S.inDeckAgain : S.mine;
  }

  // Asked when an entry is shown, so the button says the truth before it is tapped.
  async function checkInDeck(term) {
    if (state.inDeck.has(term)) return;
    // Anki unreachable after the retries: the button stays "Mine" and the mine reports the problem.
    const res = await L.withRetry(() => api("/api/canadd", { term }), L.SCHEDULES.lookup);
    if (res.status === 200 && res.data && res.data.ok) state.inDeck.set(term, !!res.data.in_deck);
  }

  // Every match is a choice: the longest first (the default), then the shorter ones past a divider.
  function renderCandidates() {
    const box = $("sheet-candidates");
    box.replaceChildren();
    const entries = state.sheet.entries;
    if (entries.length < 2) return;
    L.candidateTabs(entries).forEach((tab, i) => {
      const b = document.createElement("button");
      b.className = tab.shorter ? "tab shorter" : "tab";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", "sheet-body");
      b.append(tab.term);
      if (tab.reading) {
        const r = document.createElement("span");
        r.className = "tab-reading";
        r.textContent = tab.reading;
        b.append(r);
      }
      b.addEventListener("click", () => showEntry(i));
      box.append(b);
    });
  }

  // The row scrolls sideways only: scrollIntoView() would scroll the sheet too.
  function revealTab(tab) {
    const box = tab.parentElement;
    const r = tab.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (r.left < b.left) box.scrollLeft -= b.left - r.left;
    else if (r.right > b.right) box.scrollLeft += r.right - b.right;
  }

  function showEntry(i) {
    const s = state.sheet;
    s.pick = i;
    s.def = null;
    const entry = s.entries[i];
    [...$("sheet-candidates").children].forEach((b, j) => {
      b.classList.toggle("on", j === i);
      b.setAttribute("aria-selected", String(j === i));
      if (j === i) revealTab(b);
    });
    // The line lights what this entry matched, and a mine clozes that.
    s.span = L.entrySpan({ span: s.tapped, matched: entry.matched, longest: s.entries[0].matched,
                           scan: s.scan, length: s.cue.text.length });
    markPicked(s.cue, s.span);
    $("sheet-headword").textContent = entry.term;

    // One reading line; the pitch accent is drawn on it as an overline with a drop mark.
    const reading = $("sheet-reading");
    reading.replaceChildren();
    const kana = entry.reading || entry.term;
    const pos = entry.pitches[0];
    reading.hidden = entry.reading === entry.term && pos === undefined;
    if (!reading.hidden) {
      if (pos === undefined) {
        reading.textContent = kana;
      } else {
        reading.setAttribute("aria-label", `${kana}, pitch accent ${pos}`);
        for (const m of L.pitchPattern(kana, pos)) {
          const span = document.createElement("span");
          span.className = `mora${m.high ? " high" : ""}${m.drop ? " drop" : ""}`;
          span.textContent = m.mora;
          reading.append(span);
        }
      }
    }
    $("sheet-audio").hidden = false;
    $("sheet-freq").textContent = (entry.freqs[0] && L.freqLabel(entry.freqs[0].value)) || "";

    const body = $("sheet-body");
    body.replaceChildren();
    // The dictionary that supplies the implied definition leads, so the first gloss on screen is
    // the one a card gets when nothing is picked.
    const dicts = [...entry.dicts].sort((a, b) => (b.name === entry.glossFrom) - (a.name === entry.glossFrom));
    const [first, ...rest] = dicts;
    if (first) for (const item of first.items) body.append(glossNode(item));
    if (rest.length) {
      const more = document.createElement("details");
      more.className = "more";
      const summary = document.createElement("summary");
      summary.textContent = S.moreDictionaries;
      more.append(summary);
      for (const dict of rest) {
        const name = document.createElement("div");
        name.className = "dict-name";
        name.textContent = dict.name;
        more.append(name);
        for (const item of dict.items) more.append(glossNode(item));
      }
      body.append(more);
    }
    $("sheet-scroll").scrollTop = 0;
    setMineButton(state.inDeck.get(entry.term), false);
    checkInDeck(entry.term).then(() => {
      if (state.sheet.open && state.sheet.entries[state.sheet.pick] === entry) setMineButton(state.inDeck.get(entry.term), false);
    });
  }

  function glossNode(item) {
    const gloss = document.createElement("div");
    gloss.className = "gloss";
    // The one HTML insertion on the page: the server builds it from Yomitan's structured content
    // with an allowlist of tags, every text escaped and no attributes but colspan/rowspan and an
    // `sc-<marker>` class (portal.py sc_to_html), so nothing in it can run or load.
    gloss.innerHTML = item;
    // Part-of-speech badges shrink to one muted line above the glosses.
    const labels = [...new Set([...gloss.querySelectorAll(".sc-label")].map((el) => el.textContent.trim()).filter(Boolean))];
    if (labels.length) {
      const pos = document.createElement("div");
      pos.className = "pos";
      pos.textContent = labels.join(" · ");
      gloss.prepend(pos);
    }
    // The glosses one can choose as the card's definition: a dictionary's marked glossary items, else the
    // innermost list items, else the whole definition.
    let picks = [...gloss.querySelectorAll(".sc-glossary > li")];
    if (!picks.length) picks = [...gloss.querySelectorAll("li")].filter((li) => !li.querySelector("li"));
    for (const el of picks.length ? picks : [gloss]) el.classList.add("pick");
    return gloss;
  }

  function closeSheet(resume = true) {
    const s = state.sheet;
    if (!s.open) return;
    s.open = false;
    s.seq++;
    const sheet = $("sheet");
    sheet.classList.remove("open");
    sheet.style.transform = "";
    sheet.setAttribute("aria-hidden", "true");
    markPicked(null);
    state.focusKey = null;
    state.sheetClosedAt = Date.now(); // the ghost-click guard counts from here
    if (s.pausedIt) {
      s.pausedIt = false;
      // Back to just before the tap, or the line's start (Settings): never on from where it paused.
      // Only when playback resumes: a sheet closed for something else (the Anki setup) leaves the
      // playhead where it paused, and a seek to where the playhead already is would cost a saved
      // position and a /sync for nothing.
      const { t, cue } = L.lookupResume(state.cues, s.openedAt, s.cue && s.cue.id, settings.lookupResume);
      if (resume && Number.isFinite(t) && Math.abs(t - state.player.getCurrentTime()) > 0.05) seek(t, false);
      // The line looked up may have ended while the sheet was open; auto-pause must not fire for it again.
      state.autoPausedFor = cue ? cue.id : null;
      if (resume) play(); // still inside the gesture that closed the sheet
    }
    updateVeil();
  }

  // Everything a mine needs is copied at tap time, so later playback cannot change it.
  function mineFromSheet() {
    if (state.info && !state.info.card) {
      // No deck or note type yet: the setup screen first; the word stays where it was.
      toast(S.setupFirst);
      openAnkiSetup();
      return;
    }
    const s = state.sheet;
    const entry = s.entries[s.pick];
    if (!entry || !s.cue) return;
    const job = {
      video_id: state.videoId,
      cue: { start: s.cue.start, end: s.cue.end, text: s.cue.text },
      span: [...s.span],
      term: entry.term,
      reading: entry.reading,
      definition: s.def || entry.gloss || "",
      force: state.inDeck.get(entry.term) === true,
      extra_tags: readJson("portal.extraTags", []),
      // One id per mine: the server answers a retry of a job it already finished with that result,
      // so a lost answer never becomes a second note.
      job_id: newJobId(),
    };
    closeSheet(); // resumes playback synchronously, before the request below
    runMine(job);
  }

  function newJobId() {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes); // crypto.randomUUID needs a secure context; plain HTTP has none
    return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // Transient failures (network, 5xx: audio not ready, Anki or Yomitan timing out) retry by
  // themselves, 4 attempts over about 30 s; only then does the pill offer Retry.
  async function runMine(job) {
    const res = await L.withRetry(() => api("/api/mine", job), L.SCHEDULES.mine, {
      onRetry: (n, delay, r) => console.log(`mine ${job.term}: attempt ${n + 1} in ${delay} ms after`, r.status, r.data && r.data.error),
    });
    const data = res.data;
    if (data && data.ok) {
      state.inDeck.set(job.term, true);
      if (data.warnings && data.warnings.length) console.log("mined with warnings:", data.warnings);
      toast(S.added(data.word || job.term));
    } else if (data && data.duplicate) {
      state.inDeck.set(job.term, true);
      toast(S.alreadyInDeck(job.term), () => runMine({ ...job, force: true }));
    } else if (res.status === 428) {
      // No card set up yet: the setup screen, then Mine again.
      toast(S.setupFirst);
      openAnkiSetup();
    } else {
      console.log("mine failed:", data && data.error);
      const ankiDown = res.status === 503 && /AnkiConnect/.test((data && data.error) || "");
      toast(ankiDown ? S.ankiDown(job.term) : S.couldNotAdd(job.term), () => runMine(job));
    }
  }

  function bindSheet() {
    $("sheet-mine").addEventListener("click", mineFromSheet);
    $("sheet-audio").addEventListener("click", async () => {
      const entry = state.sheet.entries[state.sheet.pick];
      if (!entry) return;
      const audio = new Audio(`/api/audio?term=${encodeURIComponent(entry.term)}&reading=${encodeURIComponent(entry.reading || "")}`);
      try {
        await audio.play();
      } catch {
        toast(S.noAudio(entry.term));
      }
    });
    $("sheet-body").addEventListener("click", (ev) => {
      const pick = ev.target.closest(".pick");
      if (!pick || ev.target.closest("summary")) return;
      const was = pick.classList.contains("chosen");
      $("sheet-body").querySelectorAll(".pick.chosen").forEach((el) => el.classList.remove("chosen"));
      state.sheet.def = was ? null : pickText(pick);
      if (!was) pick.classList.add("chosen");
    });

    // A tap outside closes the sheet (and resumes, inside this tap) and does nothing else; a tap on
    // another word of the same sentence looks that word up instead. Just after a close, a stray
    // click (iOS's late synthetic one) is swallowed wherever it lands (lib.js dismissAction()).
    document.addEventListener("click", (ev) => {
      const word = ev.target.closest(".word");
      const row = word && word.closest("#lyrics .cue");
      const action = L.dismissAction({
        sheetOpen: state.sheet.open,
        inSheet: !!ev.target.closest("#sheet, #toasts, #settings, #anki-sheet"),
        wordCue: row ? row.dataset.cue : null,
        sheetCue: state.sheet.cue ? state.sheet.cue.id : null,
        sinceCloseMs: Date.now() - state.sheetClosedAt,
      });
      if (action === "pass") return;
      ev.preventDefault();
      ev.stopPropagation();
      if (action === "dismiss") closeSheet();
    }, true);

    makeDraggable($("sheet"), $("sheet-scroll"), () => closeSheet());
    window.addEventListener("resize", () => { if (state.sheet.open) capSheet(state.sheet.cue); });
    // The sheet grows when its entry arrives: centre the tapped cue in the room that is left.
    new ResizeObserver(() => { if (state.sheet.open) placeTrack(); }).observe($("sheet"));
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        closeAnkiSetup();
        closeSheet();
        closeSettings();
      }
    });
  }

  function pickText(el) {
    const copy = el.cloneNode(true);
    copy.querySelectorAll(".pos, rt, .sc-label").forEach((n) => n.remove());
    return copy.textContent.replace(/\s+/g, " ").trim().slice(0, 500);
  }

  // Drag a sheet down to dismiss, from its content once that is scrolled to the top. No handle is
  // drawn: the drag is a shortcut, and a tap outside or the close button is the visible way out.
  // The word sheet in the landscape mode is a side panel and is dragged right instead.
  // The dismissal runs in touchend, so a resume inside it is still a gesture.
  function makeDraggable(sheet, scroller, onDismiss) {
    let drag = null;
    sheet.addEventListener("touchstart", (ev) => {
      if (!sheet.classList.contains("open") || ev.target.closest("button, input, summary")) return;
      const side = state.landscape.on && sheet.classList.contains("word-sheet");
      drag = { x: ev.touches[0].clientX, y: ev.touches[0].clientY, dy: 0, active: false, side };
    }, { passive: true });
    sheet.addEventListener("touchmove", (ev) => {
      if (!drag) return;
      const d = viewDelta(ev.touches[0].clientX - drag.x, ev.touches[0].clientY - drag.y);
      // Along the way out (down, or right for the side panel), and across it.
      const [dy, across] = drag.side ? [d.dx, d.dy] : [d.dy, d.dx];
      if (!drag.active) {
        if (dy > 8 && (drag.side ? Math.abs(across) < dy : scroller.scrollTop <= 0)) {
          drag.active = true;
          sheet.classList.add("dragging");
        } else if (Math.max(Math.abs(dy), drag.side ? Math.abs(across) : 0) > 8) {
          drag = null;
          return;
        } else {
          return;
        }
      }
      drag.dy = Math.max(0, dy);
      sheet.style.transform = drag.side ? `translateX(${drag.dy}px)` : `translateY(${drag.dy}px)`;
      ev.preventDefault();
    }, { passive: false });
    sheet.addEventListener("touchend", () => {
      if (!drag) return;
      const { active, dy } = drag;
      drag = null;
      if (!active) return;
      sheet.classList.remove("dragging");
      sheet.style.transform = "";
      if (dy > 80) onDismiss();
    });
  }

  // ------------------------------------------------------------ settings sheet

  function applySettings() {
    $("set-autopause").checked = settings.autoPause;
    $("set-lookup-pause").checked = settings.pauseOnLookup;
    $(`set-resume-${settings.lookupResume}`).checked = true;
    $("set-furigana").checked = settings.furigana;
    $("set-listening").checked = settings.listening;
    $("set-sweep").checked = settings.sweep;
    $("set-colours").checked = settings.wordColours;
    for (const b of $("set-speed").children) b.classList.toggle("on", Number(b.dataset.speed) === settings.speed);
    // Furigana and the word colours change every cue's markup; redraw the list once when one flips.
    const drawn = `${settings.furigana} ${settings.wordColours}`;
    if (state.drawnWith !== undefined && state.drawnWith !== drawn) redrawRows();
    state.drawnWith = drawn;
    state.focusKey = null;
    updateVeil();
  }

  function bindSettings() {
    const seg = $("set-speed");
    for (const speed of SPEEDS) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.speed = String(speed);
      b.setAttribute("role", "radio");
      b.textContent = String(speed);
      b.addEventListener("click", () => {
        settings.speed = speed;
        if (state.playerReady) state.player.setPlaybackRate(speed);
        saveSettings();
        applySettings();
      });
      seg.append(b);
    }
    const bindSwitch = (id, key) => $(id).addEventListener("change", (ev) => {
      settings[key] = ev.target.checked;
      saveSettings();
      applySettings();
    });
    bindSwitch("set-autopause", "autoPause");
    bindSwitch("set-lookup-pause", "pauseOnLookup");
    for (const id of ["set-resume-tap", "set-resume-line"]) $(id).addEventListener("change", (ev) => {
      settings.lookupResume = ev.target.value;
      saveSettings();
      applySettings();
    });
    bindSwitch("set-furigana", "furigana");
    bindSwitch("set-listening", "listening");
    bindSwitch("set-sweep", "sweep");
    bindSwitch("set-colours", "wordColours");
    $("settings-backdrop").addEventListener("click", closeSettings);
    $("settings").querySelector(".sheet-close").addEventListener("click", closeSettings);
    $("set-anki").addEventListener("click", openAnkiSetup);
    bindAnkiSetup();
    makeDraggable($("settings"), $("settings").querySelector(".sheet-scroll"), closeSettings);
    applySettings();
  }

  function openSettings() {
    renderDevices();
    renderAnkiRow();
    renderAnkiSync();
    renderWatchHistory();
    closeSheet();
    renderServerStatus();
    $("settings").classList.add("open");
    $("settings").setAttribute("aria-hidden", "false");
    $("settings-backdrop").classList.add("open");
  }

  function closeSettings() {
    $("settings").classList.remove("open");
    $("settings").style.transform = "";
    $("settings").setAttribute("aria-hidden", "true");
    $("settings-backdrop").classList.remove("open");
  }

  // ------------------------------------------------------------ the scrubber

  const scrub = { dragging: false, target: 0, width: 0, left: 0, f: -1, shownSec: -1, duration: -1 };

  function scrubDuration() {
    return playerDuration() || (state.syncData && state.syncData.duration) || 0;
  }

  function measureScrubber() {
    const rect = viewRect($("scrub-track"));
    scrub.width = rect.width;
    scrub.left = rect.left;
    scrub.f = -1; // place the thumb again at the new width
  }

  // Called every frame: transforms only, and the labels only when the shown second changes.
  function renderScrubber(t) {
    const live = !!(state.syncData && state.syncData.live);
    $("scrubber").classList.toggle("live", live);
    $("scrub-live").hidden = !live;
    if (live) return;
    const d = scrubDuration();
    if (!d) return;
    const at = scrub.dragging ? scrub.target : Math.min(t, d);
    const f = Math.max(0, Math.min(1, at / d));
    if (Math.abs(f - scrub.f) > 0.0002) {
      scrub.f = f;
      $("scrub-fill").style.transform = `scaleX(${f})`;
      $("scrub-thumb").style.transform = `translateX(${(f * scrub.width).toFixed(1)}px)`;
    }
    const sec = Math.floor(at);
    if (sec === scrub.shownSec && d === scrub.duration) return;
    scrub.shownSec = sec;
    scrub.duration = d;
    $("scrub-elapsed").textContent = L.formatClock(at, d);
    $("scrub-remaining").textContent = `-${L.formatClock(Math.max(0, d - at), d)}`;
    const track = $("scrub-track");
    track.setAttribute("aria-valuemax", String(Math.floor(d)));
    track.setAttribute("aria-valuenow", String(sec));
    track.setAttribute("aria-valuetext", S.positionOf(L.spokenTime(at), L.spokenTime(d)));
  }

  // How much is loaded, as a lighter fill: cheap, so a few times a second from tick().
  function renderBuffered() {
    const d = scrubDuration();
    const p = state.player;
    if (!d || !p) return;
    let end = 0;
    if (p.kind === "native") {
      const b = p.video.buffered;
      for (let i = 0; i < b.length; i++) if (b.start(i) <= state.t + 0.5) end = Math.max(end, b.end(i));
    } else if (p.getVideoLoadedFraction) {
      end = p.getVideoLoadedFraction() * d;
    }
    $("scrub-buffer").style.transform = `scaleX(${Math.max(0, Math.min(1, end / d)).toFixed(4)})`;
  }

  function scrubTo(ev) {
    const d = scrubDuration();
    if (!d || !scrub.width) return;
    const x = viewPoint(ev.clientX, ev.clientY).x;
    scrub.target = Math.max(0, Math.min(d, ((x - scrub.left) / scrub.width) * d));
    state.previewT = scrub.target; // the subtitle list shows the cue at the target meanwhile
  }

  function bindScrubber() {
    const track = $("scrub-track");
    measureScrubber();
    new ResizeObserver(measureScrubber).observe(track);
    // Pointer capture keeps the whole drag on the track: no page scroll, no lyrics swipe.
    track.addEventListener("pointerdown", (ev) => {
      if (!scrubDuration()) return;
      ev.preventDefault();
      track.setPointerCapture(ev.pointerId);
      measureScrubber();
      scrub.dragging = true;
      $("scrubber").classList.add("dragging");
      scrubTo(ev);
    });
    track.addEventListener("pointermove", (ev) => { if (scrub.dragging) scrubTo(ev); });
    const end = (commit) => {
      if (!scrub.dragging) return;
      scrub.dragging = false;
      $("scrubber").classList.remove("dragging");
      state.previewT = null;
      if (commit) seek(scrub.target, false); // one seek, one saved position, on release
      scrub.shownSec = -1;
      if (state.landscape.on) showControls(); // the bar stays a moment after the drag
    };
    track.addEventListener("pointerup", () => end(true));
    track.addEventListener("pointercancel", () => end(false));
    track.addEventListener("keydown", (ev) => {
      const d = scrubDuration();
      if (!d) return;
      const step = { ArrowLeft: -5, ArrowDown: -5, ArrowRight: 5, ArrowUp: 5 }[ev.key];
      const to = step !== undefined ? state.t + step : ev.key === "Home" ? 0 : ev.key === "End" ? d - 1 : null;
      if (to === null) return;
      ev.preventDefault();
      seek(Math.max(0, Math.min(d, to)), false);
    });
  }

  // ------------------------------------------------------------ the server's view: language, setup

  async function loadInfo() {
    try {
      const res = await getJSON("/api/info");
      if (res.status === 200 && res.data) {
        state.info = res.data;
        state.lang = res.data.language || null;
      }
    } catch {
      // the portal itself is unreachable: the page says so where it matters
    }
    // Japanese fonts and furigana only for Japanese subtitles.
    for (const id of ["lyrics", "sheet"]) $(id).lang = state.lang || "ja";
    $("set-furigana").closest(".row-item").hidden = !isJa();
  }

  function applyStrings() {
    for (const el of document.querySelectorAll("[data-s]")) {
      if (typeof S[el.dataset.s] === "string") el.textContent = S[el.dataset.s];
    }
    for (const el of document.querySelectorAll("[data-s-label]")) el.setAttribute("aria-label", S[el.dataset.sLabel]);
  }

  // What still stands between this setup and a first card, from /api/health.
  async function renderSetupHints() {
    let health = null;
    try {
      const res = await getJSON("/api/health");
      health = res.status === 200 ? res.data : null;
    } catch {
      health = null;
    }
    const list = $("setup-list");
    list.replaceChildren();
    const add = (text, action) => {
      const li = document.createElement("li");
      // A URL in a hint becomes a link; the rest stays text.
      const m = /(https?:\/\/\S+)$/.exec(text);
      li.append(m ? text.slice(0, m.index) : text);
      if (m) {
        const a = document.createElement("a");
        a.href = m[1];
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = m[1].replace(/^https?:\/\//, "");
        li.append(a);
      }
      if (action) li.append(action);
      list.append(li);
    };
    if (!health) {
      add(S.reconnecting);
    } else {
      for (const name of ["shisuko", "yomitan", "anki", "ytdlp", "pyav"]) {
        const svc = health.services[name];
        if (svc && !svc.ok && svc.hint) add(svc.hint);
      }
      if (!health.card) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "paste";
        button.textContent = S.setupAnkiButton;
        button.addEventListener("click", openAnkiSetup);
        add(`${S.setupAnki}. `, button);
      }
    }
    $("setup-hints").hidden = !list.children.length;
  }

  // ---- Settings > Anki: deck, note type, and what each field holds

  let ankiBound = false;
  function bindAnkiSetup() {
    if (ankiBound) return;
    ankiBound = true;
    $("anki-backdrop").addEventListener("click", closeAnkiSetup);
    $("anki-sheet").querySelector(".sheet-close").addEventListener("click", closeAnkiSetup);
    makeDraggable($("anki-sheet"), $("anki-sheet").querySelector(".sheet-scroll"), closeAnkiSetup);
    $("anki-model").addEventListener("change", () => renderFieldRows($("anki-model").value, null));
    $("anki-save").addEventListener("click", saveAnkiSetup);
  }

  function ankiMessage(text) {
    $("anki-message").textContent = text || "";
    $("anki-message").hidden = !text;
  }

  function fillSelect(select, values, chosen) {
    select.replaceChildren(...values.map((v) => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = v;
      o.selected = v === chosen;
      return o;
    }));
  }

  async function openAnkiSetup() {
    bindAnkiSetup();
    closeSettings();
    if (state.sheet.open) closeSheet(false);
    $("anki-sheet").classList.add("open");
    $("anki-sheet").setAttribute("aria-hidden", "false");
    $("anki-backdrop").classList.add("open");
    ankiMessage(S.lookingUp);
    $("anki-save").disabled = true;
    $("anki-fields").replaceChildren();
    const [cardRes, optRes] = await Promise.all([getJSON("/api/card"), getJSON("/api/anki/options")]).catch(() => [null, null]);
    if (!optRes || optRes.status !== 200 || !optRes.data || !cardRes || !cardRes.data) {
      ankiMessage(S.ankiLoadFailed);
      return;
    }
    const info = cardRes.data;
    const card = info.card || info.saved || {};
    state.cardSetup = info;
    if (!optRes.data.decks.length) {
      ankiMessage(S.ankiNoDecks);
      return;
    }
    const preferDeck = card.deck && optRes.data.decks.includes(card.deck) ? card.deck : optRes.data.decks.find((d) => d !== "Default") || optRes.data.decks[0];
    fillSelect($("anki-deck"), optRes.data.decks, preferDeck);
    const preferModel = card.note_model && optRes.data.models.includes(card.note_model) ? card.note_model : optRes.data.models[0];
    fillSelect($("anki-model"), optRes.data.models, preferModel);
    const locked = info.overrides || [];
    $("anki-deck").disabled = locked.includes("deck");
    $("anki-model").disabled = locked.includes("note_model");
    ankiMessage(locked.length ? S.ankiOverride(locked.join(", "), info.override_file) : "");
    await renderFieldRows(preferModel, card.note_model === preferModel ? card.fields : null);
  }

  // One select per field: empty, the portal's own sources, Yomitan's markers, or a custom template.
  async function renderFieldRows(model, existing) {
    const box = $("anki-fields");
    box.replaceChildren();
    $("anki-save").disabled = true;
    const res = await getJSON(`/api/anki/fields?model=${encodeURIComponent(model)}`).catch(() => null);
    if (!res || res.status !== 200 || !res.data) {
      ankiMessage(S.ankiLoadFailed);
      return;
    }
    const info = state.cardSetup || {};
    const locked = (info.overrides || []).includes("fields");
    for (const name of res.data.fields) {
      const value = existing && name in existing ? existing[name] : res.data.guess[name] || "";
      const label = document.createElement("label");
      label.className = "row-item";
      const fieldName = document.createElement("span");
      fieldName.className = "field-name";
      fieldName.textContent = name;
      label.append(fieldName);
      const select = document.createElement("select");
      select.className = "select";
      select.dataset.field = name;
      select.disabled = locked;
      const option = (template, text) => {
        const o = document.createElement("option");
        o.value = template;
        o.textContent = text;
        return o;
      };
      select.append(option("", S.fieldEmpty));
      const portalGroup = document.createElement("optgroup");
      portalGroup.label = S.groupPortal;
      for (const src of Object.values(info.sources || {})) portalGroup.append(option(src.template, src.label));
      const dictGroup = document.createElement("optgroup");
      dictGroup.label = S.groupYomitan;
      for (const marker of info.markers || []) dictGroup.append(option(`{${marker}}`, marker));
      select.append(portalGroup, dictGroup);
      if (value && ![...select.options].some((o) => o.value === value)) select.append(option(value, value));
      select.append(option("\u0000custom", S.fieldCustom));
      select.value = value;
      select.addEventListener("change", () => {
        if (select.value !== "\u0000custom") return;
        const typed = window.prompt(name, value) || "";
        if (typed && ![...select.options].some((o) => o.value === typed)) select.insertBefore(option(typed, typed), select.lastChild);
        select.value = typed;
      });
      label.append(select);
      box.append(label);
    }
    $("anki-save").disabled = false;
  }

  async function saveAnkiSetup() {
    const fields = {};
    for (const select of $("anki-fields").querySelectorAll("select")) fields[select.dataset.field] = select.value === "\u0000custom" ? "" : select.value;
    $("anki-save").disabled = true;
    const res = await api("/api/card", { deck: $("anki-deck").value, note_model: $("anki-model").value, fields }).catch(() => null);
    $("anki-save").disabled = false;
    if (!res || res.status !== 200 || !res.data || !res.data.ok) {
      ankiMessage((res && res.data && res.data.error) || S.ankiLoadFailed);
      return;
    }
    const card = res.data.card;
    state.info = { ...(state.info || {}), card: card ? { deck: card.deck, note_model: card.note_model } : null };
    state.inDeck.clear();
    toast(S.ankiSaved);
    closeAnkiSetup();
    renderAnkiRow();
    if (!$("home").hidden) renderSetupHints();
  }

  function closeAnkiSetup() {
    $("anki-sheet").classList.remove("open");
    $("anki-sheet").style.transform = "";
    $("anki-sheet").setAttribute("aria-hidden", "true");
    $("anki-backdrop").classList.remove("open");
  }

  function renderAnkiRow() {
    const card = state.info && state.info.card;
    $("set-anki-text").textContent = card ? `${card.deck} · ${card.note_model}` : S.setupAnki;
  }

  // ---- Settings > Anki: the server's Anki sync after mining, read-only (its --anki-sync sets it)

  async function renderAnkiSync() {
    let sync = null;
    try {
      const res = await getJSON("/api/health");
      const server = res.status === 200 && res.data.services ? res.data.services.shisuko : null;
      sync = server && server.ok ? server.anki_sync : null;
    } catch {
      sync = null;
    }
    const text = L.ankiSyncText(sync);
    $("set-anki-sync-status").textContent = text;
    $("set-anki-sync-status").hidden = !text;
  }

  // ---- Settings > Server: the portal's --watch-history

  function renderWatchHistory() {
    const text = L.watchHistoryText(state.info && state.info.watch_history);
    $("set-watch-history").textContent = text;
    $("set-watch-history").hidden = !text;
  }

  // ---- Settings > Paired devices (lan mode, or a device paired in tailscale mode)

  async function renderDevices() {
    const res = await getJSON("/api/devices").catch(() => null);
    const data = res && res.status === 200 ? res.data : null;
    const box = $("devices");
    box.replaceChildren();
    $("devices-group").hidden = !data || (data.mode === "local" || (data.mode === "tailscale" && !data.devices.length));
    if (!data) return;
    if (!data.devices.length) {
      const empty = document.createElement("div");
      empty.className = "row-item status-text";
      empty.textContent = S.devicesNone;
      box.append(empty);
    }
    for (const d of data.devices) {
      const row = document.createElement("div");
      row.className = "row-item";
      const text = document.createElement("span");
      text.textContent = `${d.name}${d.current ? ` (${S.deviceThis})` : ""}`;
      const seen = document.createElement("span");
      seen.className = "status-text";
      seen.textContent = S.deviceSeen(new Date(d.seen).toLocaleDateString());
      const forget = document.createElement("button");
      forget.type = "button";
      forget.className = "paste";
      forget.textContent = S.deviceForget;
      forget.setAttribute("aria-label", `${S.deviceForget} ${d.name}`);
      forget.addEventListener("click", async () => {
        await api("/api/devices/forget", { id: d.id }).catch(() => null);
        if (d.current) location.reload();
        else renderDevices();
      });
      const left = document.createElement("div");
      left.className = "device-text";
      left.append(text, seen);
      row.append(left, forget);
      box.append(row);
    }
  }

  // ------------------------------------------------------------ toasts

  // A success pill leaves by itself after TOAST_MS; an error pill with Retry stays until it is
  // tapped (Retry) or dismissed (a tap on its text).
  function toast(text, onRetry) {
    const pill = document.createElement("div");
    pill.className = `toast${onRetry ? " error" : ""}`;
    pill.setAttribute("role", onRetry ? "alert" : "status");
    const label = document.createElement("span");
    label.className = "toast-text";
    label.textContent = text;
    pill.append(label);
    const remove = () => {
      pill.classList.add("leaving");
      setTimeout(() => pill.remove(), 250);
    };
    if (onRetry) {
      const retry = document.createElement("button");
      retry.className = "retry";
      retry.textContent = S.retry;
      retry.addEventListener("click", () => {
        remove();
        onRetry();
      });
      pill.append(retry);
      label.addEventListener("click", remove);
    } else {
      setTimeout(remove, TOAST_MS);
    }
    $("toasts").append(pill);
  }

  route();
})();
