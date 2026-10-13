#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Shisu-ko mining portal: a phone-sized page that plays a YouTube video with Shisu-ko's live
subtitles, looks words up through the Yomitan API and mines them into Anki.

It is a separate process that composes services already running on this machine: the Shisu-ko
server (cues, sentence audio), the Yomitan API (tokenising, dictionary entries, card fields, word
audio), AnkiConnect, yt-dlp (the video, as an HLS stream it proxies) and PyAV (the card's picture).
Who may use it depends on the access mode: this machine only (local), paired devices on the LAN
(lan), or the owner's own Tailscale devices (tailscale).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.client
import http.cookies
import io
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- the card
#
# Which deck and note type a mined word goes to, and what each field holds, is set up in the page
# (Settings > Anki) and kept in portal-state.json. ~/.shisu-ko/portal.json, when present, overrides
# any of those keys (a hand-written setup that the page cannot change).

CARD_KEYS = ("deck", "note_model", "tags", "duplicate_scope", "fields", "first_field", "sentence_field",
             "default_gloss_dictionary")
CARD_DEFAULTS = {"tags": ["shisu-ko-portal"], "duplicate_scope": "deck", "default_gloss_dictionary": ""}

# The portal's own markers, filled by the portal itself rather than by Yomitan.
PORTAL_MARKERS = {"popup-selection-text", "cloze-prefix", "cloze-body", "cloze-suffix",
                  "portal-sentence", "portal-picture", "portal-sentence-audio"}
SENTENCE_CLOZE = "{cloze-prefix}<b>{cloze-body}</b>{cloze-suffix}"
# What the setup screen offers for a field, besides Yomitan's markers and "empty".
PORTAL_SOURCES = {
    "sentence-cloze": {"label": "Sentence, word in bold", "template": SENTENCE_CLOZE},
    "sentence": {"label": "Sentence", "template": "{portal-sentence}"},
    "picture": {"label": "Picture of the video", "template": "{portal-picture}"},
    "sentence-audio": {"label": "Sentence audio", "template": "{portal-sentence-audio}"},
    "definition": {"label": "Selected definition", "template": "{popup-selection-text}"},
}
# Yomitan's own markers (the ones its Anki settings offer for terms) shown in the setup screen.
YOMITAN_MARKERS = [
    "expression", "reading", "furigana", "furigana-plain", "glossary", "glossary-brief",
    "glossary-no-dictionary", "glossary-first", "glossary-first-brief", "glossary-first-no-dictionary",
    "audio", "pitch-accents", "pitch-accent-graphs", "pitch-accent-positions", "pitch-accent-categories",
    "frequencies", "frequency-harmonic-rank", "frequency-average-rank", "part-of-speech", "conjugation",
    "phonetic-transcriptions", "tags", "dictionary", "url",
]

# Field-name guesses for a note type the page has not seen: (pattern, template), first match wins.
# Names are compared lower-cased with spaces, dashes and underscores removed. The extension's own
# defaults, "Picture" and "SentenceAudio", come first, so both tools fill the same fields.
FIELD_GUESSES = [
    (r"sentenceaudio|sentaudio|audiosentence|sentencesound|例文音声|文音声", "{portal-sentence-audio}"),
    (r"picture|image|screenshot|photo|snapshot|bild|imagen|画像|写真|スクショ", "{portal-picture}"),
    (r"sentencefurigana|sentencereading", ""),
    (r"sentence|context|example|satz|frase|phrase|例文|文", SENTENCE_CLOZE),
    (r"wordaudio|termaudio|vocabaudio|^audio$|^sound$|pronunciation|発音|音声", "{audio}"),
    (r"furigana|ふりがな|振り仮名", "{furigana}"),
    (r"reading|kana|yomi|pronunciationtext|読み|よみ", "{reading}"),
    (r"pitchaccentgraph|pitchgraph|graph", "{pitch-accent-graphs}"),
    (r"pitchaccentposition|pitchposition|pitchnumber", "{pitch-accent-positions}"),
    (r"pitch|accent|アクセント", "{pitch-accents}"),
    (r"freq|frequency|rank|頻度", "{frequency-harmonic-rank}"),
    (r"selected|chosen|picked|mydefinition", "{popup-selection-text}"),
    (r"^back$|def$|definition|glossary|meaning|bedeutung|significado|signification|意味|定義|translation|english", "{glossary}"),
    (r"^(word|expression|term|vocab|vocabulary|target|front|kanji|headword|lemma|wort|palabra|mot|単語|語)$", "{expression}"),
    (r"word|expression|term|vocab", "{expression}"),
]


def guess_fields(names: list[str]) -> dict[str, str]:
    """A first mapping for a note type's fields; whatever matches nothing stays empty. The first
    field, which Anki's duplicate check compares, gets the word when nothing else claimed it."""
    out = {}
    for name in names:
        key = re.sub(r"[\s_\-]+", "", name).lower()
        out[name] = next((tpl for pattern, tpl in FIELD_GUESSES if re.search(pattern, key)), "")
    if names and not out[names[0]] and "{expression}" not in out.values():
        out[names[0]] = "{expression}"
    return out


def validate_card(data, partial: bool = False) -> dict:
    """A card setup, checked. Unknown keys are refused, so a typo in portal.json is an error at
    start instead of a silently ignored setting. `partial` allows missing keys (an override)."""
    if not isinstance(data, dict):
        raise ValueError("the card setup must be a JSON object")
    out = {}
    for key, value in data.items():
        if key not in CARD_KEYS:
            raise ValueError(f"unknown key {key!r} (known: {', '.join(CARD_KEYS)})")
        if key == "tags":
            ok = isinstance(value, list) and all(isinstance(t, str) and t and " " not in t for t in value)
        elif key == "fields":
            ok = isinstance(value, dict) and value and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        elif key in ("default_gloss_dictionary", "sentence_field"):
            ok = isinstance(value, str)  # may be empty: no default dictionary, no sentence field
        else:
            ok = isinstance(value, str) and value.strip() != ""
        if not ok:
            raise ValueError(f"bad value for {key!r}")
        out[key] = value
    if partial:
        return out
    for key in ("deck", "note_model", "fields"):
        if key not in out:
            raise ValueError(f"{key!r} is missing")
    card = {**CARD_DEFAULTS, **out}
    fields = card["fields"]
    card.setdefault("first_field", next(iter(fields)))
    card.setdefault("sentence_field", next((k for k, v in fields.items() if "cloze-body" in v or "portal-sentence}" in v), ""))
    if card["first_field"] not in fields:
        raise ValueError(f"first_field {card['first_field']!r} is not one of the fields")
    if card["sentence_field"] and card["sentence_field"] not in fields:
        raise ValueError(f"sentence_field {card['sentence_field']!r} is not one of the fields")
    return card


def read_card_file(path: Path):
    """~/.shisu-ko/portal.json as a (partial) card override, or None when there is no file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return validate_card(json.loads(text), partial=True)


# ---------------------------------------------------------------- services

SHISUKO_URL = ("127.0.0.1", 8790)
YOMITAN_URL = ("127.0.0.1", 19633)
ANKI_URL = ("127.0.0.1", 8765)
STATIC_DIR = Path(__file__).resolve().parent / "static"
SHISUKO_HOME = Path(os.environ.get("SHISUKO_HOME") or Path.home() / ".shisu-ko")
CONFIG_PATH = SHISUKO_HOME / "portal.json"
YOMITAN_API_DOCS = "https://github.com/yomidevs/yomitan-api"
ANKICONNECT_DOCS = "https://ankiweb.net/shared/info/2055492159"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
STATIC_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,60}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
EXTRA_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_BODY = 64 * 1024
MAX_CLIP_SECONDS = 30.0
TOKENIZE_BATCH = 12
LOOKUP_ENTRIES = 8
STREAM_URL_TTL = 2 * 3600
OWNER_REFRESH_SECONDS = 60
OWNER_MISS_REFRESH_SECONDS = 10
PAIRING_TTL = 30 * 60
PAIR_PER_IP = 10
PAIR_GLOBAL = 60
DEVICE_COOKIE = "portal_device"
DEVICE_COOKIE_AGE = 400 * 24 * 3600  # the longest a browser keeps a cookie

log = logging.getLogger("portal")


class UpstreamError(Exception):
    """A local service failed or refused; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- pure helpers


def valid_video_id(value) -> bool:
    return isinstance(value, str) and bool(VIDEO_ID_RE.match(value))


def normalise_ip(addr: str) -> str:
    """'::ffff:1.2.3.4' -> '1.2.3.4'; anything unparsable comes back unchanged."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return addr
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ip)


def owner_ips(status: dict) -> set[str]:
    """The tailnet addresses of every node owned by the same user as this node.

    `status` is `tailscale status --json`. A node of another user (or a status without a Self)
    contributes nothing.
    """
    me = (status or {}).get("Self") or {}
    owner = me.get("UserID")
    if owner is None:
        return set()
    ips = set()
    for node in [me, *((status.get("Peer") or {}).values())]:
        if node.get("UserID") == owner:
            ips.update(normalise_ip(ip) for ip in node.get("TailscaleIPs") or [])
    return ips


def ip_allowed(addr: str, owners: set[str]) -> bool:
    ip = normalise_ip(addr)
    if ip in ("127.0.0.1", "::1"):
        return True
    return ip in owners


def host_allowed(host_header, allowed: set[str]) -> bool:
    """Host header check against DNS rebinding: the name before the port must be one we serve."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host in allowed


def build_cloze(text: str, start: int, end: int) -> tuple[str, str, str]:
    """The sentence split around the tapped span, each part HTML-escaped for an Anki field."""
    if not isinstance(text, str) or not text:
        raise ValueError("empty sentence")
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)):
        raise ValueError("span outside the sentence")
    return html.escape(text[:start], quote=False), html.escape(text[start:end], quote=False), html.escape(text[end:], quote=False)


def render_template(template: str, values: dict) -> str:
    return re.sub(r"\{([a-z0-9-]+)\}", lambda m: values.get(m.group(1), ""), template)


def template_markers(fields: dict) -> set[str]:
    found = set()
    for template in fields.values():
        found.update(re.findall(r"\{([a-z0-9-]+)\}", template))
    return found


# Structured-content tags kept as they are; any other tag becomes a <span>. No attribute survives
# except colspan/rowspan as small integers and one class, `sc-<name>`, from the dictionary's
# data-content marker (lowercase letters and hyphens only), so no href, src, style or on* gets through.
SC_TAGS = {"ul", "ol", "li", "span", "div", "i", "b", "br", "ruby", "rt", "rp", "table", "thead",
           "tbody", "tfoot", "tr", "td", "th", "details", "summary"}
SC_MAX_DEPTH = 40
SC_MARKER_RE = re.compile(r"^[a-z][a-z-]{0,30}$")


def sc_to_html(node, depth: int = 0) -> str:
    """Yomitan structured content -> safe HTML: allowlisted tags, escaped text, no attributes."""
    if depth > SC_MAX_DEPTH or node is None:
        return ""
    if isinstance(node, str):
        return html.escape(node, quote=False).replace("\n", "<br>")
    if isinstance(node, list):
        return "".join(sc_to_html(child, depth + 1) for child in node)
    if not isinstance(node, dict):
        return ""
    tag = node.get("tag")
    if tag == "br":
        return "<br>"
    if tag == "img":
        # Dictionary images (part-of-speech badges and the like) carry their meaning in a title.
        label = node.get("title") or node.get("alt") or ""
        return f'<span class="sc-label">{html.escape(str(label), quote=False)}</span>' if label else ""
    if tag not in SC_TAGS:
        tag = "span"
    attrs = ""
    marker = (node.get("data") or {}).get("content") if isinstance(node.get("data"), dict) else None
    if isinstance(marker, str) and SC_MARKER_RE.match(marker):
        attrs += f' class="sc-{marker}"'
    if tag in ("td", "th"):
        for key, name in (("colSpan", "colspan"), ("rowSpan", "rowspan")):
            value = node.get(key)
            if isinstance(value, int) and 1 <= value <= 20:
                attrs += f' {name}="{value}"'
    return f"<{tag}{attrs}>{sc_to_html(node.get('content'), depth + 1)}</{tag}>"


def glossary_html(entry) -> str:
    """One Yomitan definition entry (string, text, structured content or image) as safe HTML."""
    if isinstance(entry, str):
        return html.escape(entry, quote=False).replace("\n", "<br>")
    if isinstance(entry, dict):
        kind = entry.get("type")
        if kind == "structured-content":
            return sc_to_html(entry.get("content"))
        if kind == "text":
            return html.escape(str(entry.get("text", "")), quote=False).replace("\n", "<br>")
    return ""


def sc_text(node, depth: int = 0) -> str:
    if depth > SC_MAX_DEPTH or node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(sc_text(child, depth + 1) for child in node)
    if isinstance(node, dict) and node.get("tag") not in ("img", "rt", "rp"):
        return sc_text(node.get("content"), depth + 1)
    return ""


def first_gloss(entry) -> str:
    """The first innermost list item of a definition as plain text, else its first line."""
    def walk(node, depth=0):
        if depth > SC_MAX_DEPTH:
            return None
        if isinstance(node, list):
            for child in node:
                found = walk(child, depth + 1)
                if found:
                    return found
        elif isinstance(node, dict):
            inner = walk(node.get("content"), depth + 1)
            if inner:
                return inner
            if node.get("tag") == "li":
                return sc_text(node.get("content")).strip() or None
        return None

    if isinstance(entry, str):
        return next((line.strip() for line in entry.splitlines() if line.strip()), "")
    if isinstance(entry, dict):
        if entry.get("type") == "text":
            return first_gloss(str(entry.get("text", "")))
        if entry.get("type") == "structured-content":
            return walk(entry.get("content")) or first_gloss(sc_text(entry.get("content")))
    return ""


def compact_tokens(content) -> list[dict]:
    """A Yomitan scanning-parser line -> [{text, segs: [[text, reading]], term}].

    `term` is the first headword the scan found (None for punctuation and unknown text); the token
    texts concatenate back to the input line.
    """
    tokens = []
    for token in content or []:
        segs = [[str(s.get("text", "")), str(s.get("reading", "") or "")] for s in token]
        term = None
        for s in token:
            for group in s.get("headwords") or []:
                for head in group:
                    term = head.get("term")
                    break
                if term:
                    break
            if term:
                break
        tokens.append({"text": "".join(s[0] for s in segs), "segs": segs, "term": term})
    return tokens


def match_length(entry: dict) -> int:
    """How much of the looked-up text this entry matched (its longest source)."""
    return max((len(src.get("originalText") or "") for head in entry.get("headwords") or []
                for src in head.get("sources") or []), default=0)


def compact_entries(data: dict, limit: int = LOOKUP_ENTRIES, default_dictionary: str = "") -> list[dict]:
    """Yomitan termEntries -> what the word sheet shows, one item per headword.

    Only the entries matching as much of the text as the best one count as candidates: tapping
    天気 offers 天気, not every reading of 天 that also matched its first character.
    """
    out = []
    seen = set()
    entries = (data or {}).get("dictionaryEntries", [])
    longest = max((match_length(e) for e in entries), default=0)
    for entry in entries:
        if match_length(entry) < longest:
            continue
        headwords = entry.get("headwords") or []
        if not headwords:
            continue
        head = headwords[0]
        key = (head.get("term"), head.get("reading"))
        if key in seen:
            continue
        seen.add(key)
        dicts = []
        by_name = {}
        default = ""
        default_from = None
        for definition in entry.get("definitions") or []:
            name = definition.get("dictionaryAlias") or definition.get("dictionary") or "?"
            if name not in by_name:
                by_name[name] = {"name": name, "items": []}
                dicts.append(by_name[name])
            for item in definition.get("entries") or []:
                rendered = glossary_html(item)
                if rendered:
                    by_name[name]["items"].append(rendered)
                if default_dictionary and name == default_dictionary and not default:
                    default = first_gloss(item)
                    default_from = name if default else None
        if not default:
            for definition in entry.get("definitions") or []:
                for item in definition.get("entries") or []:
                    default = first_gloss(item)
                    if default:
                        default_from = definition.get("dictionaryAlias") or definition.get("dictionary")
                        break
                if default:
                    break
        pitches = []
        for group in entry.get("pronunciations") or []:
            if group.get("headwordIndex", 0) != 0:
                continue
            for p in group.get("pronunciations") or []:
                pos = p.get("positions")
                if p.get("type") == "pitch-accent" and isinstance(pos, int) and pos not in pitches:
                    pitches.append(pos)
        freqs = []
        named = set()
        for f in entry.get("frequencies") or []:
            name = f.get("dictionaryAlias") or f.get("dictionary")
            if f.get("headwordIndex", 0) != 0 or name in named:
                continue
            named.add(name)
            freqs.append({"name": name, "value": str(f.get("displayValue") or f.get("frequency"))})
        out.append({
            "term": head.get("term"),
            "reading": head.get("reading"),
            "pitches": pitches,
            "freqs": freqs,
            "dicts": [d for d in dicts if d["items"]],
            "gloss": default[:300],
            "glossFrom": default_from,
            "matched": longest,
        })
        if len(out) >= limit:
            break
    return out


def frame_time(start: float, end: float) -> float:
    """Where in a cue the picture is taken: a little in, so the frame belongs to the line."""
    return start + min(0.75, max(0.0, end - start) / 2)


# ---------------------------------------------------------------- upstream calls


def request(addr, method: str, path: str, body: bytes | None = None, headers=None, timeout: float = 15.0):
    """One HTTP request to a local service; returns (status, bytes). Raises UpstreamError when down."""
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, http.client.HTTPException) as exc:
        name = {SHISUKO_URL: "the Shisu-ko server", YOMITAN_URL: "the Yomitan API", ANKI_URL: "AnkiConnect"}.get(tuple(addr), "a service")
        raise UpstreamError(f"{name} is not answering at {addr[0]}:{addr[1]} ({exc.__class__.__name__})", 503) from exc
    finally:
        conn.close()


def yomitan(action: str, payload: dict, timeout: float = 20.0):
    status, data = request(YOMITAN_URL, "POST", "/" + action, json.dumps(payload).encode(),
                           {"Content-Type": "application/json"}, timeout)
    if status != 200:
        raise UpstreamError(f"Yomitan {action} answered {status} (is Firefox with Yomitan open?)")
    return json.loads(data)


def anki(action: str, timeout: float = 15.0, **params):
    # AnkiConnect wants no Content-Type header, so the request is built without one.
    status, data = request(ANKI_URL, "POST", "/", json.dumps({"action": action, "version": 6, "params": params}).encode(),
                           {}, timeout)
    if status != 200:
        raise UpstreamError(f"AnkiConnect answered {status}")
    reply = json.loads(data)
    if reply.get("error"):
        # Anki answered and refused: a verdict on the request (a duplicate, a bad field), never
        # worth repeating, so a 4xx. Only "could not ask" (unreachable, timeout) is a 5xx.
        error = str(reply["error"])
        raise UpstreamError(f"Anki: {error}", 409 if "duplicate" in error.lower() else 422)
    return reply.get("result")


# ---------------------------------------------------------------- retries


def is_transient(exc: Exception) -> bool:
    """Worth trying again: a service unreachable, timed out or failing (5xx), not a refusal (4xx)."""
    return isinstance(exc, UpstreamError) and exc.status >= 500


def backoff_delay(attempt: int, base: float, factor: float, max_delay: float, rand=random.random) -> float:
    """Seconds before retry number `attempt` (0 = the first retry), with ±20% jitter."""
    return min(max_delay, base * factor ** attempt) * (0.8 + 0.4 * rand())


def retry(fn, tries: int = 2, base: float = 1.0, factor: float = 2.0, max_delay: float = 8.0,
          sleep=time.sleep, rand=random.random):
    """Call fn(); on a transient failure wait (1, 2, 4 … s by default) and call it again, `tries`
    more times at most. A permanent failure, or the last transient one, is raised."""
    for attempt in range(tries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= tries or not is_transient(exc):
                raise
            delay = backoff_delay(attempt, base, factor, max_delay, rand)
            log.info("retrying in %.1f s after: %s", delay, exc)
            sleep(delay)
    raise AssertionError("unreachable")


def anki_retry(action: str, **params):
    """An AnkiConnect call that is safe to repeat (reads, storeMediaFile), retried on timeouts."""
    return retry(lambda: anki(action, **params))


def search_quote(text: str) -> str:
    """Text for an Anki search term in double quotes: \\ and " escaped, wildcards * and _ literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("_", "\\_")


def find_same_note(note: dict, first_field: str, sentence_field: str, call=anki):
    """The id of a note already holding this note's first field and sentence in its deck, or None."""
    fields = note["fields"]
    query = f'"deck:{search_quote(note["deckName"])}" "{search_quote(first_field)}:{search_quote(fields[first_field])}"'
    ids = call("findNotes", query=query) or []
    for info in (call("notesInfo", notes=ids[-50:]) if ids else []):
        have = {k: v.get("value") for k, v in (info.get("fields") or {}).items()}
        if have.get(first_field) == fields[first_field] and (not sentence_field or have.get(sentence_field) == fields.get(sentence_field)):
            return info.get("noteId")
    return None


def add_note_once(note: dict, first_field: str, sentence_field: str, call=anki, tries: int = 3,
                  sleep=time.sleep, rand=random.random):
    """addNote that never adds the same note twice.

    A timeout or a dropped connection can hide an add that went through, so before every retry
    Anki is asked whether a note with this exact first field and sentence now exists; if so its id is the
    answer and nothing is added.
    """
    for attempt in range(tries + 1):
        try:
            if attempt:
                existing = find_same_note(note, first_field, sentence_field, call)
                if existing:
                    log.info("addNote had gone through before the failure: note %s", existing)
                    return existing
            return call("addNote", note=note)
        except Exception as exc:
            if attempt >= tries or not is_transient(exc):
                raise
            delay = backoff_delay(attempt, 1.0, 2.0, 8.0, rand)
            log.info("addNote: retrying in %.1f s after: %s", delay, exc)
            sleep(delay)
    raise AssertionError("unreachable")


class Services:
    """The state the portal keeps between requests: caches and the owner allowlist."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.owners: set[str] = set()
        self.owners_at = 0.0
        self.model = None
        self.model_at = 0.0
        self.streams: dict[str, tuple[str, float]] = {}
        self.stream_locks: dict[str, threading.Lock] = {}
        self.audio_cache: dict[tuple, dict] = {}
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mine")
        self.hls = HlsProxy()
        self.state = PortalState()
        self.positions = self.state
        self.card_file = None  # portal.json's keys, read at start
        self.pairing = None
        self.public_url = None
        self.language_code = None
        self.language_at = 0.0
        self.pair_hits: dict[str, list[float]] = {}
        self.pair_total: list[float] = []
        self.jobs: dict[str, tuple[float, dict]] = {}
        self.job_locks: dict[str, threading.Lock] = {}

    # ---- who may use the portal
    #
    # Loopback always. In lan mode, a device holding a token from pairing. In tailscale mode, the
    # tailnet devices of this machine's own Tailscale user (a paired device too, should it have one).

    def authorize(self, addr: str, token) -> str:
        """"ok", "unpaired" (lan mode, no valid token: may pair) or "forbidden"."""
        if ip_allowed(addr, set()):
            return "ok"
        if self.args.mode in ("lan", "tailscale") and self.state.device_for(token):
            return "ok"
        if self.args.mode == "tailscale" and self.allowed(addr):
            return "ok"
        return "unpaired" if self.args.mode == "lan" else "forbidden"

    def new_pairing(self) -> None:
        """A fresh one-time pairing secret: a long one for the URL and QR code, an 8-digit one to
        type in (an iPhone home-screen app has its own cookies, apart from Safari's)."""
        with self.lock:
            self.pairing = {"long": secrets.token_urlsafe(24), "short": f"{secrets.randbelow(10**8):08d}",
                            "until": time.time() + PAIRING_TTL}
        self.print_pairing()

    def print_pairing(self) -> None:
        if not self.public_url:
            return
        url = f"{self.public_url}/pair?code={self.pairing['long']}"
        try:
            from qr import qr_text  # portal/qr.py, next to this file

            code = qr_text(url)
        except Exception as exc:  # noqa: BLE001 - the QR code is a convenience; the URL is enough
            log.info("no QR code: %s", exc)
            code = ""
        print(f"\nPair a phone or tablet: open this link on it (one use, {PAIRING_TTL // 60} minutes)\n\n  {url}\n", flush=True)
        if code:
            print(code, flush=True)
        print(f"or type this code on the portal's page there: {self.pairing['short'][:4]} {self.pairing['short'][4:]}\n", flush=True)

    def pair_limited(self, addr: str) -> bool:
        """At most PAIR_PER_IP tries a minute from one address and PAIR_GLOBAL from all of them."""
        now = time.time()
        with self.lock:
            hits = [t for t in self.pair_hits.get(addr, []) if now - t < 60]
            total = [t for t in self.pair_total if now - t < 60]
            if len(hits) >= PAIR_PER_IP or len(total) >= PAIR_GLOBAL:
                self.pair_hits[addr], self.pair_total = hits, total
                return True
            self.pair_hits[addr] = hits + [now]
            self.pair_total = total + [now]
            return False

    def pair(self, code: str, user_agent: str):
        """A device token for a valid pairing code (either form), or None. A code works once."""
        with self.lock:
            pairing = self.pairing
            if not pairing or time.time() > pairing["until"]:
                expired = True
                ok = False
            else:
                expired = False
                given = re.sub(r"\s+", "", str(code or ""))
                ok = secrets.compare_digest(given, pairing["long"]) or secrets.compare_digest(given, pairing["short"])
                if ok:
                    self.pairing = None
        if expired and self.args.mode == "lan":
            self.new_pairing()  # the old one ran out: the terminal shows a new one
            return None
        if not ok:
            return None
        token = self.state.add_device(device_name(user_agent))
        log.info("paired a new device: %s", device_name(user_agent))
        self.new_pairing()  # the next device gets a code of its own
        return token

    # ---- the Tailscale owner allowlist

    def refresh_owners(self) -> None:
        try:
            out = subprocess.run([self.args.tailscale_bin, "status", "--json"], capture_output=True, timeout=10, check=True).stdout
            owners = owner_ips(json.loads(out))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("tailscale status failed: %s", exc)
            owners = None
        with self.lock:
            if owners is not None:
                self.owners = owners
            self.owners_at = time.time()

    def allowed(self, addr: str) -> bool:
        now = time.time()
        with self.lock:
            owners, age = self.owners, now - self.owners_at
        if age > OWNER_REFRESH_SECONDS or (not ip_allowed(addr, owners) and age > OWNER_MISS_REFRESH_SECONDS):
            self.refresh_owners()
            with self.lock:
                owners = self.owners
        return ip_allowed(addr, owners)

    # ---- Shisu-ko

    def loaded_model(self):
        """The model the server holds now. /sync names it so this client never asks for a switch."""
        with self.lock:
            if self.model and time.time() - self.model_at < 30:
                return self.model
        status, data = request(SHISUKO_URL, "GET", "/health", timeout=5)
        model = json.loads(data).get("model") if status == 200 else None
        with self.lock:
            self.model, self.model_at = model, time.time()
        return model

    def sync(self, body: dict) -> dict:
        video_id = body.get("video_id")
        if not valid_video_id(video_id):
            raise UpstreamError("bad video id", 400)
        payload = {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "t": max(0.0, float(body.get("t") or 0)),
            "since": max(0, int(body.get("since") or 0)),
            "paused": bool(body.get("paused")),
            "model": self.loaded_model(),
            # Whisper's word timings per cue, for the word sweep; a server without them ignores it.
            "words": True,
        }
        status, data = request(SHISUKO_URL, "POST", "/sync", json.dumps(payload).encode(),
                               {"Content-Type": "application/json"}, timeout=10)
        reply = json.loads(data) if data else {}
        if status != 200:
            raise UpstreamError(reply.get("error") or f"Shisu-ko answered {status}", 502)
        reply.pop("speech", None)
        return reply

    def clip(self, video_id: str, start: float, end: float, wait: float = 0.0) -> bytes:
        """Sentence audio as MP3; waits up to `wait` seconds while the server still fetches audio."""
        query = urllib.parse.urlencode({"video_id": video_id, "start": f"{start:.3f}", "end": f"{end:.3f}", "format": "mp3"})
        deadline = time.time() + wait
        while True:
            status, data = request(SHISUKO_URL, "GET", "/clip?" + query, timeout=60)
            if status == 200:
                return data
            if status != 503 or time.time() >= deadline:
                detail = ""
                try:
                    detail = json.loads(data).get("error") or ""
                except ValueError:
                    pass
                raise UpstreamError(f"sentence audio not available ({status}{': ' + detail if detail else ''})",
                                    503 if status == 503 else 502)
            time.sleep(2)

    # ---- Yomitan

    def tokenize(self, texts: list[str]) -> list[list[dict]]:
        result = yomitan("tokenize", {"text": texts, "scanLength": 20})
        by_index = {}
        for item in result:
            if item.get("id") == "scan":
                by_index.setdefault(item.get("index", 0), compact_tokens(item.get("content")))
        return [by_index.get(i, [{"text": t, "segs": [[t, ""]], "term": None}]) for i, t in enumerate(texts)]

    def lookup(self, text: str) -> list[dict]:
        card = self.card()
        return compact_entries(yomitan("termEntries", {"term": text}),
                               default_dictionary=(card or {}).get("default_gloss_dictionary", ""))

    def anki_fields(self, term: str, reading: str, fields: dict) -> dict:
        """Yomitan's rendered card fields and media for the entry with this headword and reading."""
        markers = sorted((template_markers(fields) - PORTAL_MARKERS) | {"expression", "reading"})
        entries = yomitan("termEntries", {"term": term}).get("dictionaryEntries", [])
        index = 0
        for i, entry in enumerate(entries[:20]):
            head = (entry.get("headwords") or [{}])[0]
            if head.get("term") == term and (not reading or head.get("reading") == reading):
                index = i
                break
        result = yomitan("ankiFields", {"text": term, "type": "term", "markers": markers,
                                        "maxEntries": index + 1, "includeMedia": True}, timeout=45)
        fields = result.get("fields") or []
        if not fields:
            raise UpstreamError(f"Yomitan found no entry for {term}", 404)
        chosen = fields[-1]
        for f in fields:
            if f.get("expression") == term and (not reading or f.get("reading") == reading):
                chosen = f
                break
        audio = [m for m in result.get("audioMedia") or []
                 if m.get("term") == chosen.get("expression") and m.get("reading") == chosen.get("reading")]
        return {"fields": chosen, "audio": audio[:1], "dictionaryMedia": result.get("dictionaryMedia") or []}

    def word_audio(self, term: str, reading: str):
        key = (term, reading)
        with self.lock:
            if key in self.audio_cache:
                return self.audio_cache[key]
        result = yomitan("ankiFields", {"text": term, "type": "term", "markers": ["audio", "expression", "reading"],
                                        "maxEntries": 4, "includeMedia": True}, timeout=30)
        media = None
        for m in result.get("audioMedia") or []:
            if m.get("term") == term and (not reading or m.get("reading") == reading) and m.get("content"):
                media = m
                break
        with self.lock:
            if len(self.audio_cache) > 200:
                self.audio_cache.clear()
            self.audio_cache[key] = media
        return media

    # ---- video frame

    def stream_url(self, video_id: str, fresh: bool = False) -> str:
        with self.lock:
            lock = self.stream_locks.setdefault(video_id, threading.Lock())
        with lock:
            with self.lock:
                cached = self.streams.get(video_id)
            if cached and not fresh and time.time() - cached[1] < STREAM_URL_TTL:
                return cached[0]
            yt_dlp = import_ytdlp()
            opts = ydl_options("bestvideo[height<=720][vcodec^=avc1]/bestvideo[height<=720]/best[height<=720]/best")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            url = info.get("url") or next((f.get("url") for f in info.get("requested_formats") or [] if f.get("vcodec") != "none"), None)
            if not url:
                raise UpstreamError("no video stream found", 502)
            with self.lock:
                self.streams[video_id] = (url, time.time())
            return url

    def media(self, video_id: str, t: float = 0.0) -> dict:
        """How the page plays this video: our HLS stream, or YouTube's player when we cannot."""
        try:
            video = self.hls.get(video_id)
            threading.Thread(target=self.hls.warm, args=(video, t), daemon=True, name=f"warm-{video_id}").start()
        except UpstreamError as exc:
            log.info("[%s] YouTube's player instead: %s", video_id, exc)
            return {"ok": True, "kind": "youtube", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises its own types; any failure means fallback
            log.warning("[%s] HLS resolve failed: %s", video_id, exc)
            return {"ok": True, "kind": "youtube", "reason": "the stream could not be resolved"}
        start = f"?start={t:.1f}" if t > 0 else ""  # a resume: the playlists name where to start
        return {"ok": True, "kind": "hls", "url": f"{self.hls.prefix(video)}master.m3u8{start}",
                "duration": video.duration, "title": video.title, "height": max(video.heights.values())}

    def frame(self, video_id: str, t: float) -> bytes:
        # The segment the phone played, when the video streams through this PC: fast, and the
        # very frame on screen. Else a frame from a DASH stream URL.
        try:
            return self.hls.frame(video_id, t)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the DASH path
            log.info("[%s] HLS frame unavailable (%s); using a DASH stream", video_id, exc)
        last_error = None
        for attempt in range(2):
            url = self.stream_url(video_id, fresh=attempt > 0)
            try:
                return frame_jpeg(url, t, relative=False)
            except UpstreamError as exc:
                last_error = exc
        raise last_error

    # ---- mining

    def card(self):
        """The card setup in force: the page's saved one with portal.json's keys on top; None
        until there is a deck, a note type and fields (the page then asks for them)."""
        merged = {**(self.state.card() or {}), **(self.card_file or {})}
        if not merged:
            return None
        try:
            return validate_card(merged)
        except ValueError as exc:
            log.warning("the card setup is incomplete (%s): the page will ask for it", exc)
            return None

    def need_card(self) -> dict:
        card = self.card()
        if card is None:
            raise UpstreamError("Set up your Anki card first: Settings > Anki", 428)
        return card

    def in_deck(self, term: str) -> bool:
        """Whether a note with this headword already exists (Anki's own duplicate rule)."""
        card = self.need_card()
        probe = {"deckName": card["deck"], "modelName": card["note_model"], "fields": {card["first_field"]: term},
                 "options": {"allowDuplicate": False, "duplicateScope": card["duplicate_scope"]}}
        verdict = (anki("canAddNotesWithErrorDetail", timeout=5, notes=[probe]) or [{}])[0]
        return not verdict.get("canAdd") and "duplicate" in str(verdict.get("error", "")).lower()

    def mine(self, body: dict) -> dict:
        """One mine per job id: a client retrying a job (its answer lost on the way) gets the first
        result back, and a retry arriving while the first attempt runs waits for it."""
        job = body.get("job_id")
        if not (isinstance(job, str) and JOB_ID_RE.match(job)):
            return self.mine_once(body)
        with self.lock:
            lock = self.job_locks.setdefault(job, threading.Lock())
            for old in [k for k, (at, _) in self.jobs.items() if time.time() - at > 3600]:
                self.jobs.pop(old, None)
                self.job_locks.pop(old, None)
        with lock:
            with self.lock:
                done = self.jobs.get(job)
            if done:
                log.info("job %s already done: note %s", job, done[1].get("note"))
                return done[1]
            result = self.mine_once(body)
            if result.get("ok"):
                with self.lock:
                    self.jobs[job] = (time.time(), result)
            return result

    def mine_once(self, body: dict) -> dict:
        video_id = body.get("video_id")
        if not valid_video_id(video_id):
            raise UpstreamError("bad video id", 400)
        cue = body.get("cue") or {}
        text = cue.get("text")
        try:
            start, end = float(cue.get("start")), float(cue.get("end"))
        except (TypeError, ValueError):
            raise UpstreamError("cue needs start and end", 400) from None
        if not (0 <= start < end) or end - start > MAX_CLIP_SECONDS:
            raise UpstreamError("cue timing out of range", 400)
        span = body.get("span") or []
        try:
            prefix, cloze, suffix = build_cloze(text, int(span[0]), int(span[1]))
        except (ValueError, TypeError, IndexError) as exc:
            raise UpstreamError(f"bad span: {exc}", 400) from None
        term = body.get("term")
        reading = body.get("reading") or ""
        if not isinstance(term, str) or not term or len(term) > 40:
            raise UpstreamError("no word", 400)
        definition = str(body.get("definition") or "")[:500]
        card = self.need_card()
        tags = list(card["tags"])
        for tag in body.get("extra_tags") or []:
            if isinstance(tag, str) and EXTRA_TAG_RE.match(tag) and tag not in tags and len(tags) < len(card["tags"]) + 3:
                tags.append(tag)

        if not body.get("force") and retry(lambda: self.in_deck(term)):
            return {"ok": False, "duplicate": True, "error": f"{term} is already in {card['deck']}"}

        # The picture and the sentence audio come from the cue's own timestamps, fetched while
        # Yomitan renders the fields; only when a field uses them.
        wanted = template_markers(card["fields"])
        frame_job = self.pool.submit(self.frame, video_id, frame_time(start, end)) if "portal-picture" in wanted else None
        clip_job = self.pool.submit(self.clip, video_id, start, end, 45.0) if "portal-sentence-audio" in wanted else None
        found = retry(lambda: self.anki_fields(term, reading, card["fields"]))
        values = dict(found["fields"])
        warnings = []
        values.update({"popup-selection-text": html.escape(definition, quote=False),
                       "cloze-prefix": prefix, "cloze-body": cloze, "cloze-suffix": suffix,
                       "portal-sentence": html.escape(text, quote=False)})
        base = f"shisuko_{video_id}_{round(start * 1000)}"
        if frame_job:
            try:
                name = anki_retry("storeMediaFile", filename=f"{base}.jpg", data=base64.b64encode(frame_job.result()).decode())
                values["portal-picture"] = f'<img src="{html.escape(name or base + ".jpg")}">'
            except Exception as exc:  # noqa: BLE001 - a missing picture must not cost the card
                warnings.append(f"no picture: {exc}")
        if clip_job:
            try:
                name = anki_retry("storeMediaFile", filename=f"{base}.mp3", data=base64.b64encode(clip_job.result()).decode())
                values["portal-sentence-audio"] = f"[sound:{name or base + '.mp3'}]"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"no sentence audio: {exc}")
        for media in found["audio"]:
            if media.get("ankiFilename") and media.get("content"):
                anki_retry("storeMediaFile", filename=media["ankiFilename"], data=media["content"])
        if "audio" in wanted and not values.get("audio"):
            warnings.append("no word audio")
        fields = {name: render_template(template, values) for name, template in card["fields"].items()}
        if not fields.get(card["first_field"]):
            fields[card["first_field"]] = html.escape(term, quote=False)  # Anki refuses a note whose first field is empty
        for media in found["dictionaryMedia"]:
            filename = media.get("ankiFilename")
            if filename and media.get("content") and any(filename in v for v in fields.values()):
                anki_retry("storeMediaFile", filename=filename, data=media["content"])
        note = {"deckName": card["deck"], "modelName": card["note_model"], "fields": fields, "tags": tags,
                "options": {"allowDuplicate": bool(body.get("force")), "duplicateScope": card["duplicate_scope"]}}
        note_id = add_note_once(note, card["first_field"], card["sentence_field"])
        log.info("mined %s (note %s)%s", term, note_id, f" warnings: {warnings}" if warnings else "")
        return {"ok": True, "note": note_id, "word": term, "warnings": warnings}

    def health(self) -> dict:
        """Every service the portal needs, each with what to do when it is missing."""
        import importlib.util

        services = {}

        def check(name, call, hint):
            try:
                services[name] = {"ok": True, **call()}
            except Exception as exc:  # noqa: BLE001 - health reports, it does not fail
                services[name] = {"ok": False, "error": str(exc), "hint": hint}

        check("shisuko", lambda: {"version": json.loads(request(SHISUKO_URL, "GET", "/health", timeout=3)[1]).get("version")},
              "Start the Shisu-ko server (server/run.cmd on Windows, server/run.sh elsewhere).")
        check("yomitan", lambda: {"version": yomitan("yomitanVersion", {}, timeout=3).get("version")},
              f"Open the browser that has Yomitan, and install the Yomitan API: {YOMITAN_API_DOCS}")
        check("anki", lambda: {"version": anki("version", timeout=3)},
              f"Start Anki, with the AnkiConnect add-on installed: {ANKICONNECT_DOCS}")
        for module, name, hint in (
                ("yt_dlp", "ytdlp", "yt-dlp is missing: videos play in YouTube's own player. Run the portal with the "
                                    "Shisu-ko server's Python (~/.shisu-ko/venv)."),
                ("av", "pyav", "PyAV is missing: cards get no picture. Run the portal with the Shisu-ko server's Python.")):
            services[name] = {"ok": importlib.util.find_spec(module) is not None}
            if not services[name]["ok"]:
                services[name]["hint"] = hint
        return {"ok": True, "services": services, "card": self.card() is not None,
                "media_bytes_in": self.hls.bytes_in, "media_bytes_out": self.hls.bytes_out}

    def language(self):
        """The server's subtitle language (its /health), cached a minute; None when unknown."""
        with self.lock:
            if time.time() - self.language_at < 60:
                return self.language_code
        try:
            language = json.loads(request(SHISUKO_URL, "GET", "/health", timeout=3)[1]).get("language")
        except (UpstreamError, ValueError):
            language = None
        with self.lock:
            self.language_code, self.language_at = language, time.time()
        return language

    def info(self) -> dict:
        card = self.card()
        return {"ok": True, "mode": self.args.mode, "language": self.language(),
                "card": {"deck": card["deck"], "note_model": card["note_model"]} if card else None}

    def card_info(self) -> dict:
        return {"ok": True, "card": self.card(), "saved": self.state.card(),
                "overrides": sorted(self.card_file or {}), "override_file": str(self.args.config),
                "sources": PORTAL_SOURCES, "markers": YOMITAN_MARKERS, "defaults": CARD_DEFAULTS}

    def save_card(self, body: dict) -> dict:
        """The setup made in the page. The note type's own field order decides the first field."""
        wanted = {k: body[k] for k in ("deck", "note_model", "fields", "tags", "default_gloss_dictionary") if k in body}
        card = validate_card(wanted)
        names = anki("modelFieldNames", timeout=8, modelName=card["note_model"])
        unknown = [f for f in card["fields"] if f not in names]
        if unknown:
            raise UpstreamError(f"{card['note_model']} has no field {unknown[0]!r}", 400)
        card["fields"] = {name: card["fields"].get(name, "") for name in names}
        card["first_field"] = names[0]
        card = validate_card({k: v for k, v in card.items() if k != "sentence_field"})
        self.state.set_card(card)
        log.info("card set up: deck %s, note type %s", card["deck"], card["note_model"])
        return self.card_info()

def cookies_browser():
    """The browser whose YouTube cookies the Shisu-ko server sends, from its config.json."""
    try:
        chosen = json.loads((SHISUKO_HOME / "config.json").read_text(encoding="utf-8")).get("cookies_from_browser")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(chosen, str) and chosen.strip() and chosen.strip().lower() != "none":
        return chosen.strip().lower()
    return None


# ---------------------------------------------------------------- pictures, with PyAV


def encode_jpeg(frame) -> bytes:
    """One decoded video frame as a JPEG, with PyAV's own MJPEG encoder (no Pillow, no ffmpeg)."""
    import av

    codec = av.CodecContext.create("mjpeg", "w")
    codec.width, codec.height = frame.width, frame.height
    codec.pix_fmt = "yuvj420p"
    codec.time_base = frame.time_base or 1
    codec.options = {"qscale": "3"}
    image = frame.reformat(format="yuvj420p")
    data = b"".join(bytes(p) for p in codec.encode(image)) + b"".join(bytes(p) for p in codec.encode(None))
    if not data.startswith(b"\xff\xd8"):
        raise UpstreamError("the frame did not encode as a JPEG", 502)
    return data


def frame_jpeg(source, t: float, relative: bool) -> bytes:
    """A JPEG of the first frame at time t of `source` (the bytes of a segment, or a URL).

    `relative`: t counts from the source's first frame (an HLS segment, whose timestamps continue
    the whole stream's); else t is the stream's own time, reached with a seek (a whole video).
    """
    try:
        import av
    except ImportError:
        raise UpstreamError("PyAV is not installed in this Python, so no picture", 503) from None
    try:
        container = av.open(io.BytesIO(source) if isinstance(source, bytes) else source, timeout=30)
    except av.error.FFmpegError as exc:
        raise UpstreamError(f"the video could not be opened ({exc})", 502) from None
    with container:
        stream = container.streams.video[0]
        base = stream.time_base
        if not relative and t > 1:
            container.seek(int(t / base), stream=stream)
        first = last = None
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            at = float(frame.pts * base)
            first = at if first is None else first
            last = frame
            if at >= (first + t if relative else t) - 0.001:
                return encode_jpeg(frame)
        if last is not None:
            return encode_jpeg(last)
    raise UpstreamError("no video frame at that time", 502)


# ---------------------------------------------------------------- video: an HLS proxy
#
# The phone plays YouTube's own HLS stream in a plain <video>, through this PC: googlevideo URLs
# only work from the address that resolved them. The master playlist is reduced to H.264 variants
# up to HLS_MAX_HEIGHT with an AAC-LC audio rendition, and every URI in it, and in the media
# playlists, becomes /api/hls/<token>/... The token names a video the portal resolved itself; a
# client can never make it fetch a URL of its own choosing.

HLS_MAX_HEIGHT = 720
HLS_TOKEN_TTL = 8 * 3600  # googlevideo URLs live about 6 h; a 403 before that re-resolves
HLS_IDLE_DROP = 2 * 3600
HLS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
HLS_KEY_RE = re.compile(r"^[av][0-9]{1,4}$")
SEGMENT_CACHE_BYTES = 64 * 1024 * 1024
ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def hls_attrs(line: str) -> dict:
    """The attribute list of an #EXT-X-... tag: {'BANDWIDTH': '804860', 'CODECS': 'avc1...,mp4a...'}."""
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k: v[1:-1] if v.startswith('"') else v for k, v in ATTR_RE.findall(body)}


def itag_of(url: str) -> str | None:
    m = re.search(r"/itag/(\d+)/", url)
    return m.group(1) if m else None


def rewrite_master(text: str, prefix: str, max_height: int = HLS_MAX_HEIGHT):
    """YouTube's master playlist reduced to what every phone plays, URIs pointing at `prefix`.

    Keeps H.264 (avc1) variants up to `max_height`; when any of them pairs with AAC-LC
    (mp4a.40.2) audio, only those, and only the audio renditions they use. Returns the playlist,
    {key: upstream url} for each playlist named in it, and {key: height} for the video ones.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            attrs = hls_attrs(line)
            height = int((attrs.get("RESOLUTION", "0x0").split("x") + ["0"])[1] or 0)
            codecs = attrs.get("CODECS", "")
            if codecs.startswith("avc1") and 0 < height <= max_height:
                variants.append((line, lines[i + 1], attrs, height))
    if any("mp4a.40.2" in v[2].get("CODECS", "") for v in variants):
        variants = [v for v in variants if "mp4a.40.2" in v[2].get("CODECS", "")]
    groups = {v[2].get("AUDIO") for v in variants if v[2].get("AUDIO")}
    upstream, heights = {}, {}
    out = ["#EXTM3U"]
    for line in lines:
        if line.startswith("#EXT-X-INDEPENDENT-SEGMENTS") or line.startswith("#EXT-X-VERSION"):
            out.append(line)
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = hls_attrs(line)
            if attrs.get("GROUP-ID") not in groups or "URI" not in attrs:
                continue
            key = f"a{itag_of(attrs['URI']) or len(upstream)}"
            upstream[key] = attrs["URI"]
            out.append(line.replace(f'URI="{attrs["URI"]}"', f'URI="{prefix}{key}.m3u8"'))
    # A player starts with the first variant listed: the best one, since 720p H.264 is under
    # 1 Mbit/s and a slow link steps down from there by itself.
    variants.sort(key=lambda v: -v[3])
    for line, uri, _attrs, height in variants:
        key = f"v{itag_of(uri) or len(upstream)}"
        if key in upstream:
            continue  # the same video playlist listed once per audio group
        upstream[key] = uri
        heights[key] = height
        out += [line, f"{prefix}{key}.m3u8"]
    return "\n".join(out) + "\n", upstream, heights


def rewrite_media(text: str, prefix: str, ext: str = "ts"):
    """A media playlist with each segment URI replaced by `prefix<n>.<ext>`. Returns the playlist,
    the upstream segment URLs and their durations, in order.

    `ext` names the segment format: "ts" for MPEG-TS video, "aac" for YouTube's audio renditions,
    which are packed audio (ADTS AAC with ID3 timestamps), not TS; a player picks the demuxer by it.
    """
    out, urls, durations = [], [], []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line[8:].split(",", 1)[0])
            except ValueError:
                pending = 0.0
            out.append(line)
        elif line.startswith("#"):
            if line.startswith("#EXT-X-MAP") or line.startswith("#EXT-X-KEY"):
                continue  # TS segments carry neither; never forward a URI we do not rewrite
            out.append(line)
        else:
            out.append(f"{prefix}{len(urls)}.{ext}")
            urls.append(line)
            durations.append(pending or 0.0)
            pending = None
    return "\n".join(out) + "\n", urls, durations


def byte_range(header, size: int):
    """A single "bytes=a-b" Range as (a, b) inclusive; None without one, False when unsatisfiable."""
    if not header:
        return None
    m = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        a = int(m.group(1))
        b = min(size - 1, int(m.group(2))) if m.group(2) else size - 1
    else:
        a, b = max(0, size - int(m.group(2))), size - 1
    return (a, b) if a <= b and a < size else False


def with_start(playlist: str, t) -> str:
    """A playlist that starts playback at t seconds: EXT-X-START, which iOS honours for a VOD
    stream even where it drops a seek made before the first play. In a master playlist the URIs of
    the media playlists carry the same start, so they say it too."""
    try:
        t = float(t)
    except (TypeError, ValueError):
        return playlist
    if not 0 < t < 1e6:
        return playlist
    out = []
    for line in playlist.split("\n"):
        if line.startswith("#EXT-X-START"):
            continue
        if line.startswith("/api/hls/") and line.endswith(".m3u8"):
            line += f"?start={t:.1f}"
        elif line.startswith("#EXT-X-MEDIA:") and '.m3u8"' in line:
            line = line.replace('.m3u8"', f'.m3u8?start={t:.1f}"')
        out.append(line)
        if line == "#EXTM3U":
            out.append(f"#EXT-X-START:TIME-OFFSET={t:.1f},PRECISE=YES")
    return "\n".join(out)


def segment_at(durations: list[float], t: float) -> tuple[int, float]:
    """Index of the segment holding time t, and t's offset inside it."""
    start = 0.0
    for i, d in enumerate(durations):
        if t < start + d or i == len(durations) - 1:
            return i, max(0.0, t - start)
        start += d
    return 0, 0.0


def import_ytdlp():
    try:
        import yt_dlp
    except ImportError:
        raise UpstreamError("yt-dlp is not installed in this Python: run the portal with the Shisu-ko "
                            "server's Python (~/.shisu-ko/venv)", 503) from None
    return yt_dlp


def ydl_options(fmt: str | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "js_runtimes": {name: {} for name in ("deno", "node") if shutil.which(name)} or {"deno": {}},
    }
    if fmt:
        opts["format"] = fmt
    browser = cookies_browser()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def http_open(url: str, headers=None, timeout: float = 20.0):
    """GET an upstream URL; a 403/410 (expired or IP-bound URL) and a 404 are permanent, the
    rest (5xx, network) transient."""
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(f"upstream answered {exc.code}", 410 if exc.code in (403, 410) else exc.code) from None
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamError(f"upstream unreachable ({exc.__class__.__name__})", 503) from None


class HlsVideo:
    def __init__(self, video_id: str):
        self.video_id = video_id
        self.token = secrets.token_urlsafe(18)
        self.lock = threading.Lock()
        self.created = time.time()
        self.used = time.time()
        self.master = None
        self.upstream: dict[str, str] = {}
        self.heights: dict[str, int] = {}
        self.media: dict[str, tuple[str, list[str], list[float]]] = {}
        self.duration = None
        self.title = None


class HlsProxy:
    def __init__(self):
        self.lock = threading.Lock()
        self.videos: dict[str, HlsVideo] = {}
        self.by_token: dict[str, HlsVideo] = {}
        self.bytes_in = 0  # pulled from googlevideo
        self.bytes_out = 0  # sent to players
        self.cache: OrderedDict[tuple, bytes] = OrderedDict()
        self.cache_size = 0

    def prefix(self, video: HlsVideo) -> str:
        return f"/api/hls/{video.token}/"

    def resolve(self, video: HlsVideo) -> None:
        """yt-dlp -> the HLS master URL -> our master. Call with video.lock held."""
        yt_dlp = import_ytdlp()
        with yt_dlp.YoutubeDL(ydl_options()) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video.video_id}", download=False)
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            raise UpstreamError("live stream: YouTube's player plays these", 422)
        master_url = next((f.get("manifest_url") for f in info.get("formats") or []
                           if str(f.get("protocol", "")).startswith("m3u8") and f.get("manifest_url")), None)
        if not master_url:
            raise UpstreamError("no HLS stream for this video", 422)
        text = retry(lambda: http_open(master_url).read().decode("utf-8", "replace"), tries=2, base=0.5)
        master, upstream, heights = rewrite_master(text, self.prefix(video))
        if not heights:
            raise UpstreamError("no H.264 stream for this video", 422)
        video.master, video.upstream, video.heights = master, upstream, heights
        video.media = {}
        video.duration = info.get("duration")
        video.title = info.get("title")
        video.created = time.time()

    def get(self, video_id: str) -> HlsVideo:
        """The resolved video, resolving it (or again, after HLS_TOKEN_TTL) as needed."""
        with self.lock:
            for vid, v in list(self.videos.items()):
                if time.time() - v.used > HLS_IDLE_DROP:
                    self.videos.pop(vid, None)
                    self.by_token.pop(v.token, None)
            video = self.videos.get(video_id)
            if video is None or time.time() - video.created > HLS_TOKEN_TTL:
                if video is not None:
                    self.by_token.pop(video.token, None)
                video = HlsVideo(video_id)
                self.videos[video_id] = video
                self.by_token[video.token] = video
        with video.lock:
            if video.master is None:
                self.resolve(video)
        video.used = time.time()
        return video

    def token_video(self, token: str) -> HlsVideo:
        with self.lock:
            video = self.by_token.get(token)
        if video is None or video.master is None:
            raise UpstreamError("unknown or expired stream", 404)
        video.used = time.time()
        return video

    def refresh(self, video: HlsVideo) -> None:
        """New upstream URLs (after a 403) under the same token, so the player never notices."""
        with video.lock:
            log.info("[%s] stream URLs refused: resolving again", video.video_id)
            self.resolve(video)

    def media_playlist(self, video: HlsVideo, key: str):
        with video.lock:
            cached = video.media.get(key)
        if cached:
            return cached
        if key not in video.upstream:
            raise UpstreamError("no such playlist", 404)
        for attempt in range(2):
            try:
                text = retry(lambda: http_open(video.upstream[key]).read().decode("utf-8", "replace"), tries=2, base=0.5)
                break
            except UpstreamError as exc:
                if exc.status != 410 or attempt:
                    raise
                self.refresh(video)
        result = rewrite_media(text, f"{self.prefix(video)}{key}/", "aac" if key.startswith("a") else "ts")
        with video.lock:
            video.media[key] = result
        return result

    def open_segment(self, video: HlsVideo, key: str, index: int, range_header: str | None):
        """The upstream response for one segment, ready to stream; a 403 re-resolves once."""
        headers = {"Range": range_header} if range_header else {}
        for attempt in range(2):
            urls = self.media_playlist(video, key)[1]
            if not 0 <= index < len(urls):
                raise UpstreamError("no such segment", 404)
            try:
                return retry(lambda: http_open(urls[index], headers), tries=3, base=0.5)
            except UpstreamError as exc:
                if exc.status != 410 or attempt:
                    raise
                self.refresh(video)
        raise AssertionError("unreachable")

    def segment(self, video: HlsVideo, key: str, index: int) -> bytes:
        """One segment's bytes; the last few are kept, so a re-request or a Range costs nothing."""
        cache_key = (video.token, key, index)
        with self.lock:
            data = self.cache.get(cache_key)
            if data is not None:
                self.cache.move_to_end(cache_key)
                return data
        upstream = self.open_segment(video, key, index, None)
        try:
            data = upstream.read()
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamError(f"segment download broke off ({exc.__class__.__name__})", 503) from None
        finally:
            upstream.close()
        with self.lock:
            self.bytes_in += len(data)
            self.cache[cache_key] = data
            self.cache_size += len(data)
            while self.cache_size > SEGMENT_CACHE_BYTES and self.cache:
                _, old = self.cache.popitem(last=False)
                self.cache_size -= len(old)
        return data

    def warm(self, video: HlsVideo, t: float) -> None:
        """Fetch what the first play needs (playlists, the segments at t) into the cache, so the
        tap that starts the video finds them on this PC. The best video variant is where iOS
        starts, the lowest where Chromium does; audio is shared."""
        heights = video.heights
        keys = {max(heights, key=heights.get), min(heights, key=heights.get)} | {k for k in video.upstream if k.startswith("a")}
        for key in keys:
            try:
                durations = self.media_playlist(video, key)[2]
                index = segment_at(durations, t)[0]
                for i in (index, index + 1):
                    if i < len(durations):
                        self.segment(video, key, i)
            except Exception as exc:  # noqa: BLE001 - warming is a bonus; playback fetches on its own
                log.info("[%s] warming %s failed: %s", video.video_id, key, exc)

    def frame(self, video_id: str, t: float) -> bytes:
        """A JPEG of time t from the highest variant, cut from the very segment the player shows."""
        with self.lock:
            video = self.videos.get(video_id)
        if video is None or video.master is None:
            raise UpstreamError("stream not resolved", 404)
        key = max(video.heights, key=video.heights.get)
        durations = self.media_playlist(video, key)[2]
        index, offset = segment_at(durations, t)
        return frame_jpeg(self.segment(video, key, index), offset, relative=True)


# ---------------------------------------------------------------- watch positions and thumbnails

STATE_PATH = SHISUKO_HOME / "portal-state.json"
THUMB_DIR = SHISUKO_HOME / "portal-thumbs"
THUMB_MAX_FILES = 500
POSITIONS_MAX = 200


def atomic_write_json(path: Path, data) -> None:
    """Write via a temporary file in the same folder and a rename: a reader (or a crash) sees the
    old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class PortalState:
    """Everything the portal keeps on this machine, in one JSON file shared by every device:
    where each video was left, the card setup made in the page, and the paired devices."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def load(self) -> dict:
        if self.data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data = data if isinstance(data, dict) else {}
            if not isinstance(data.get("positions"), dict):
                data["positions"] = {}
            if not isinstance(data.get("devices"), list):
                data["devices"] = []
            self.data = data
        return self.data

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    # ---- watch positions: {t, duration, title, updated} per video

    def get(self, video_id: str):
        with self.lock:
            return self.load()["positions"].get(video_id)

    def all(self) -> list[dict]:
        with self.lock:
            items = [{"id": k, **v} for k, v in self.load()["positions"].items()]
        return sorted(items, key=lambda x: x.get("updated", 0), reverse=True)

    def put(self, video_id: str, t: float, duration=None, title=None, updated=None) -> dict:
        with self.lock:
            positions = self.load()["positions"]
            entry = dict(positions.get(video_id) or {})
            entry["t"] = round(max(0.0, float(t)), 1)
            if isinstance(duration, (int, float)) and duration > 0:
                entry["duration"] = round(float(duration), 1)
            if isinstance(title, str) and title.strip():
                entry["title"] = title.strip()[:200]
            entry["updated"] = int(updated or time.time() * 1000)
            positions[video_id] = entry
            # Bounded: the least recently watched go first.
            for old in sorted(positions, key=lambda k: positions[k].get("updated", 0))[:-POSITIONS_MAX]:
                positions.pop(old, None)
            self.save()
            return entry

    def delete(self, video_id: str) -> None:
        with self.lock:
            if self.load()["positions"].pop(video_id, None) is not None:
                self.save()

    # ---- the card setup made in the page

    def card(self):
        with self.lock:
            card = self.load().get("card")
        return dict(card) if isinstance(card, dict) else None

    def set_card(self, card: dict) -> None:
        with self.lock:
            self.load()["card"] = card
            self.save()

    # ---- paired devices (lan mode): only a hash of each device's token is kept

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def add_device(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time() * 1000)
        with self.lock:
            self.load()["devices"].append({"id": secrets.token_hex(4), "name": name[:80], "hash": self.token_hash(token),
                                           "created": now, "seen": now})
            self.save()
        return token

    def device_for(self, token) -> dict | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        wanted = self.token_hash(token)
        with self.lock:
            for device in self.load()["devices"]:
                if secrets.compare_digest(device.get("hash", ""), wanted):
                    now = int(time.time() * 1000)
                    if now - device.get("seen", 0) > 60_000:  # one write a minute at most
                        device["seen"] = now
                        self.save()
                    return {k: v for k, v in device.items() if k != "hash"}
        return None

    def devices(self) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in d.items() if k != "hash"} for d in self.load()["devices"]]

    def forget(self, device_id: str | None = None) -> int:
        """Revoke one device, or every device when no id is given; returns how many went."""
        with self.lock:
            devices = self.load()["devices"]
            keep = [d for d in devices if device_id is not None and d.get("id") != device_id]
            gone = len(devices) - len(keep)
            if gone:
                self.load()["devices"] = keep
                self.save()
            return gone


PositionStore = PortalState  # the name the tests and older code use


def device_name(user_agent: str) -> str:
    """A recognisable name for a paired device, from its User-Agent: "iPhone · Safari"."""
    ua = user_agent or ""
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Mac OS X", "Mac"), ("Windows", "Windows"), ("CrOS", "ChromeOS"),
                                          ("Linux", "Linux")) if key in ua), "Device")
    browser = next((name for key, name in (("EdgA", "Edge"), ("Edg/", "Edge"), ("Firefox", "Firefox"), ("FxiOS", "Firefox"),
                                           ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Safari", "Safari")) if key in ua), "browser")
    return f"{system} · {browser}"


def thumbnail(video_id: str) -> bytes:
    """The video's thumbnail, fetched once from i.ytimg.com and kept on this PC."""
    path = THUMB_DIR / f"{video_id}.jpg"
    try:
        return path.read_bytes()
    except OSError:
        pass
    data = None
    for name in ("mqdefault.jpg", "hqdefault.jpg", "default.jpg"):
        try:
            data = retry(lambda: http_open(f"https://i.ytimg.com/vi/{video_id}/{name}", timeout=10).read(), tries=1, base=0.3)
            if data.startswith(b"\xff\xd8"):
                break
        except UpstreamError:
            data = None
    if not data:
        raise UpstreamError("no thumbnail", 404)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    files = sorted(THUMB_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    for old in files[:-THUMB_MAX_FILES]:
        old.unlink(missing_ok=True)
    return data


# ---------------------------------------------------------------- HTTP

OPEN_PATHS = {"/app.css", "/pair.js", "/icon.svg", "/icon-180.png", "/manifest.webmanifest"}
APPLE_ICON_RE = re.compile(r"^/apple-touch-icon(-\d{2,3}x\d{2,3})?(-precomposed)?\.png$")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".webmanifest": "application/manifest+json", ".json": "application/json"}
CSP = ("default-src 'self'; script-src 'self' https://www.youtube.com https://s.ytimg.com; "
       "frame-src https://www.youtube.com https://www.youtube-nocookie.com; img-src 'self' data: https://i.ytimg.com; "
       "media-src 'self' blob:; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "ShisukoPortal/1"
    protocol_version = "HTTP/1.1"  # keep-alive: a video is hundreds of segment requests
    services: Services = None  # set in main()
    allowed_hosts: set[str] = set()

    def log_message(self, fmt, *args):  # the access log is written in finish_request()
        pass

    def answer(self, status: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the client went away (a seek, a closed page)
        self.status_sent = status

    def json(self, status: int, payload) -> None:
        self.answer(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def device_token(self):
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        morsel = jar.get(DEVICE_COOKIE)
        return morsel.value if morsel else None

    def gate(self) -> bool:
        addr = self.client_address[0]
        if not host_allowed(self.headers.get("Host"), self.allowed_hosts):
            log.warning("refused Host %r from %s", self.headers.get("Host"), addr)
            self.json(403, {"ok": False, "error": "unknown host"})
            return False
        verdict = self.services.authorize(addr, self.device_token())
        if verdict == "ok":
            return True
        log.warning("refused %s %s from %s (%s)", self.command, self.path, addr, verdict)
        if verdict == "unpaired":
            if urllib.parse.urlsplit(self.path).path in ("/", "/index.html"):
                # The page itself: the one place to type the pairing code.
                self.answer(401, (STATIC_DIR / "pair.html").read_bytes(), "text/html; charset=utf-8",
                            {"Content-Security-Policy": CSP})
            else:
                self.json(401, {"ok": False, "error": "this device is not paired", "pair": True})
        else:
            self.json(403, {"ok": False, "error": "forbidden"})
        return False

    def finish_pairing(self, token) -> None:
        cookie = f"{DEVICE_COOKIE}={token}; Path=/; Max-Age={DEVICE_COOKIE_AGE}; HttpOnly; SameSite=Strict"
        self.answer(303, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})

    def pair_get(self, query: dict):
        """GET /pair?code=...: the link printed (and shown as a QR code) by the portal."""
        addr = self.client_address[0]
        if not host_allowed(self.headers.get("Host"), self.allowed_hosts):
            return self.json(403, {"ok": False, "error": "unknown host"})
        if self.services.pair_limited(addr):
            return self.json(429, {"ok": False, "error": "too many tries; wait a minute"})
        token = self.services.pair((query.get("code") or [""])[0], self.headers.get("User-Agent", ""))
        if not token:
            return self.answer(403, (STATIC_DIR / "pair.html").read_bytes(), "text/html; charset=utf-8",
                               {"Content-Security-Policy": CSP})
        return self.finish_pairing(token)

    def handle_one_request(self):
        self.started = time.time()
        self.status_sent = None
        super().handle_one_request()
        path = getattr(self, "path", "")
        if self.status_sent and not (path.startswith("/api/sync") and self.status_sent == 200):
            log.info("%s %s %s %s %.0fms", self.client_address[0], getattr(self, "command", "?"),
                     path.split("?", 1)[0], self.status_sent, (time.time() - self.started) * 1000)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        if path == "/pair":
            return self.pair_get(query)
        if path in OPEN_PATHS or APPLE_ICON_RE.match(path):
            # What the pairing page itself needs; nothing in these says anything about the user.
            if not host_allowed(self.headers.get("Host"), self.allowed_hosts):
                return self.json(403, {"ok": False, "error": "unknown host"})
            return self.static(path)
        if not self.gate():
            return
        try:
            if path == "/api/health":
                return self.json(200, self.services.health())
            if path == "/api/info":
                return self.json(200, self.services.info())
            if path == "/api/card":
                return self.json(200, self.services.card_info())
            if path == "/api/anki/options":
                return self.json(200, {"ok": True, "decks": sorted(anki("deckNames", timeout=8)),
                                       "models": sorted(anki("modelNames", timeout=8))})
            if path == "/api/anki/fields":
                model = (query.get("model") or [""])[0]
                if not model or len(model) > 200:
                    return self.json(400, {"ok": False, "error": "model please"})
                names = anki("modelFieldNames", timeout=8, modelName=model)
                return self.json(200, {"ok": True, "fields": names, "guess": guess_fields(names)})
            if path == "/api/devices":
                current = self.services.state.device_for(self.device_token())
                return self.json(200, {"ok": True, "mode": self.services.args.mode, "devices": [
                    {**d, "current": bool(current and current["id"] == d["id"])} for d in self.services.state.devices()]})
            if path.startswith("/api/hls/"):
                return self.hls(path, query)
            if path == "/api/media":
                video_id = (query.get("video_id") or [""])[0]
                if not valid_video_id(video_id):
                    return self.json(400, {"ok": False, "error": "bad video id"})
                try:
                    t = max(0.0, float((query.get("t") or ["0"])[0]))
                except ValueError:
                    t = 0.0
                return self.json(200, self.services.media(video_id, t))
            if path.startswith("/api/thumb/"):
                video_id = path.rsplit("/", 1)[1].removesuffix(".jpg")
                if not valid_video_id(video_id):
                    return self.json(400, {"ok": False, "error": "bad video id"})
                return self.answer(200, thumbnail(video_id), "image/jpeg", {"Cache-Control": "max-age=604800"})
            if path == "/api/position":
                video_id = (query.get("video_id") or [""])[0]
                if not valid_video_id(video_id):
                    return self.json(400, {"ok": False, "error": "bad video id"})
                return self.json(200, {"ok": True, "position": self.services.positions.get(video_id)})
            if path == "/api/positions":
                return self.json(200, {"ok": True, "videos": self.services.positions.all()})
            if path == "/api/clip":
                video_id = (query.get("video_id") or [""])[0]
                start, end = float(query["start"][0]), float(query["end"][0])
                if not valid_video_id(video_id) or not (0 <= start < end) or end - start > MAX_CLIP_SECONDS:
                    return self.json(400, {"ok": False, "error": "bad clip request"})
                return self.answer(200, self.services.clip(video_id, start, end), "audio/mpeg")
            if path == "/api/audio":
                term = (query.get("term") or [""])[0]
                reading = (query.get("reading") or [""])[0]
                if not term or len(term) > 40 or len(reading) > 60:
                    return self.json(400, {"ok": False, "error": "bad word"})
                media = self.services.word_audio(term, reading)
                if not media:
                    return self.json(404, {"ok": False, "error": "no audio for this word"})
                return self.answer(200, base64.b64decode(media["content"]), media.get("mediaType") or "audio/mpeg")
            return self.static(path)
        except UpstreamError as exc:
            return self.json(exc.status, {"ok": False, "error": str(exc)})
        except (KeyError, ValueError) as exc:
            return self.json(400, {"ok": False, "error": f"bad request: {exc}"})

    def hls(self, path: str, query: dict):
        """/api/hls/<token>/master.m3u8, /<key>.m3u8 and /<key>/<n>.ts: our playlists and the
        segments behind them, streamed from googlevideo as they arrive."""
        parts = path.split("/")[3:]
        if len(parts) < 2 or not HLS_TOKEN_RE.match(parts[0]):
            return self.json(404, {"ok": False, "error": "not found"})
        hls = self.services.hls
        video = hls.token_video(parts[0])
        playlist = {"Content-Type": "application/vnd.apple.mpegurl"}
        start = (query.get("start") or [None])[0]  # not part of any cache key: only this response
        if len(parts) == 2 and parts[1] == "master.m3u8":
            return self.answer(200, with_start(video.master, start).encode(), "application/vnd.apple.mpegurl")
        if len(parts) == 2 and parts[1].endswith(".m3u8") and HLS_KEY_RE.match(parts[1][:-5]):
            text = hls.media_playlist(video, parts[1][:-5])[0]
            return self.answer(200, with_start(text, start).encode(), playlist["Content-Type"])
        segment = re.fullmatch(r"([0-9]{1,5})\.(ts|aac)", parts[2]) if len(parts) == 3 else None
        if segment and HLS_KEY_RE.match(parts[1]) and (segment.group(2) == "aac") == parts[1].startswith("a"):
            ctype = "audio/aac" if segment.group(2) == "aac" else "video/mp2t"
            data = hls.segment(video, parts[1], int(segment.group(1)))
            span = byte_range(self.headers.get("Range"), len(data))
            if span is None:
                hls.bytes_out += len(data)
                return self.answer(200, data, ctype, {"Cache-Control": "max-age=3600", "Accept-Ranges": "bytes"})
            if span is False:
                return self.answer(416, b"", ctype, {"Content-Range": f"bytes */{len(data)}"})
            a, b = span
            hls.bytes_out += b - a + 1
            return self.answer(206, data[a:b + 1], ctype,
                               {"Cache-Control": "max-age=3600", "Accept-Ranges": "bytes", "Content-Range": f"bytes {a}-{b}/{len(data)}"})
        return self.json(404, {"ok": False, "error": "not found"})

    def static(self, path: str):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        # iOS asks for these by name, whatever the page's <link> says.
        if APPLE_ICON_RE.match(path):
            name = "icon-180.png"
        if not STATIC_NAME_RE.match(name) or ".." in name:
            return self.json(404, {"ok": False, "error": "not found"})
        file = STATIC_DIR / name
        if not file.is_file():
            return self.json(404, {"ok": False, "error": "not found"})
        extra = {"Content-Security-Policy": CSP} if name.endswith(".html") else {}
        return self.answer(200, file.read_bytes(), CONTENT_TYPES.get(file.suffix, "application/octet-stream"), extra)

    def do_POST(self):  # noqa: N802
        # Every answer before the body is read ends the connection: an unread body would otherwise
        # be parsed as the next request on this keep-alive connection.
        self.close_connection = True
        pairing = urllib.parse.urlsplit(self.path).path == "/api/pair"
        if pairing:
            # The typed pairing code: no device token yet, so only the Host check and the rate limit.
            if not host_allowed(self.headers.get("Host"), self.allowed_hosts):
                return self.json(403, {"ok": False, "error": "unknown host"})
            if self.services.pair_limited(self.client_address[0]):
                return self.json(429, {"ok": False, "error": "too many tries; wait a minute"})
        elif not self.gate():
            return
        # JSON only: a foreign page cannot send this type without a CORS preflight we never grant.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.json(415, {"ok": False, "error": "send application/json"})
        origin = self.headers.get("Origin")
        if origin and not host_allowed(urllib.parse.urlsplit(origin).netloc, self.allowed_hosts):
            return self.json(403, {"ok": False, "error": "foreign origin"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self.json(413, {"ok": False, "error": "body too large"})
        raw = self.rfile.read(length)
        self.close_connection = False
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            return self.json(400, {"ok": False, "error": f"bad JSON: {exc}"})
        path = urllib.parse.urlsplit(self.path).path
        try:
            if pairing:
                token = self.services.pair(body.get("code"), self.headers.get("User-Agent", ""))
                if not token:
                    return self.json(403, {"ok": False, "error": "wrong or used code"})
                cookie = f"{DEVICE_COOKIE}={token}; Path=/; Max-Age={DEVICE_COOKIE_AGE}; HttpOnly; SameSite=Strict"
                return self.answer(200, b'{"ok": true}', "application/json", {"Set-Cookie": cookie})
            if path == "/api/card":
                return self.json(200, self.services.save_card(body))
            if path == "/api/devices/forget":
                device_id = body.get("id")
                if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{8}", device_id):
                    return self.json(400, {"ok": False, "error": "device id please"})
                return self.json(200, {"ok": True, "forgotten": self.services.state.forget(device_id)})
            if path == "/api/sync":
                return self.json(200, self.services.sync(body))
            if path == "/api/tokenize":
                texts = body.get("texts")
                if (not isinstance(texts, list) or not texts or len(texts) > TOKENIZE_BATCH
                        or not all(isinstance(t, str) and 0 < len(t) <= 300 for t in texts)):
                    return self.json(400, {"ok": False, "error": f"texts: 1-{TOKENIZE_BATCH} strings of 1-300 characters"})
                return self.json(200, {"ok": True, "tokens": self.services.tokenize(texts)})
            if path == "/api/lookup":
                text = body.get("text")
                if not isinstance(text, str) or not 0 < len(text) <= 40:
                    return self.json(400, {"ok": False, "error": "text: 1-40 characters"})
                return self.json(200, {"ok": True, "entries": self.services.lookup(text)})
            if path == "/api/position":
                video_id = body.get("video_id")
                t = body.get("t")
                if not valid_video_id(video_id) or not isinstance(t, (int, float)) or not 0 <= t < 1e6:
                    return self.json(400, {"ok": False, "error": "video_id and t please"})
                entry = self.services.positions.put(video_id, t, body.get("duration"), body.get("title"))
                return self.json(200, {"ok": True, "position": entry})
            if path == "/api/position/delete":
                video_id = body.get("video_id")
                if not valid_video_id(video_id):
                    return self.json(400, {"ok": False, "error": "bad video id"})
                self.services.positions.delete(video_id)
                return self.json(200, {"ok": True})
            if path == "/api/positions/import":
                # The one-time move of a device's old local list: entries the server lacks only.
                added = 0
                for item in (body.get("items") or [])[:50]:
                    if not isinstance(item, dict) or not valid_video_id(item.get("id")) or self.services.positions.get(item["id"]):
                        continue
                    t = item.get("t") if isinstance(item.get("t"), (int, float)) and 0 <= item["t"] < 1e6 else 0
                    at = item.get("at") if isinstance(item.get("at"), (int, float)) else None
                    self.services.positions.put(item["id"], t, None, item.get("title"), at)
                    added += 1
                return self.json(200, {"ok": True, "added": added})
            if path == "/api/canadd":
                term = body.get("term")
                if not isinstance(term, str) or not 0 < len(term) <= 40:
                    return self.json(400, {"ok": False, "error": "term: 1-40 characters"})
                return self.json(200, {"ok": True, "in_deck": self.services.in_deck(term)})
            if path == "/api/mine":
                result = self.services.mine(body)
                return self.json(200 if result.get("ok") else 409, result)
            return self.json(404, {"ok": False, "error": "not found"})
        except UpstreamError as exc:
            return self.json(exc.status, {"ok": False, "error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            log.exception("bad request on %s", path)
            return self.json(400, {"ok": False, "error": f"bad request: {exc}"})


def serve(address: str, port: int) -> None:
    """Bind one address, retrying while it does not exist yet (tailscale up after the portal)."""
    while True:
        try:
            httpd = ThreadingHTTPServer((address, port), Handler)
        except OSError as exc:
            log.warning("cannot bind %s:%d (%s); retrying in 15 s", address, port, exc)
            time.sleep(15)
            continue
        httpd.daemon_threads = True
        log.info("listening on http://%s:%d/", address if ":" not in address else f"[{address}]", port)
        httpd.serve_forever()


def tailscale_status(binary: str) -> dict:
    try:
        out = subprocess.run([binary, "status", "--json"], capture_output=True, timeout=10, check=True).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def tailscale_ipv4(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "ip", "-4"], capture_output=True, timeout=10, check=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return next((line.strip() for line in out.splitlines() if line.strip()), None)


def host_names(binds: list[str], extra: list[str], status: dict) -> set[str]:
    """Every name the page may be asked for by (the Host check): the bind addresses, loopback, this
    machine's host name, and its Tailscale name and addresses."""
    me = (status or {}).get("Self") or {}
    host = socket.gethostname()
    names = {*binds, "localhost", "127.0.0.1", "::1", host, f"{host}.local", *extra}
    if me.get("HostName"):
        names.add(me["HostName"])
    if me.get("DNSName"):
        dns = me["DNSName"].rstrip(".")
        names |= {dns, dns.split(".", 1)[0]}
    names |= {normalise_ip(ip) for ip in me.get("TailscaleIPs") or []}
    return {n.lower() for n in names if n}


def lan_ipv4() -> str | None:
    """This machine's address on its local network: the source of a route to a public address.
    Nothing is sent (a UDP socket only picks its route on connect)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1: never answered, never needs to be
            return sock.getsockname()[0]
    except OSError:
        return None


TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def check_binds(mode: str, binds: list[str]) -> str | None:
    """Why these addresses do not suit the mode, or None. Without device tokens (local and
    tailscale modes) the portal must not be reachable from the LAN, let alone everywhere."""
    for addr in binds:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return f"{addr!r} is not an IP address"
        if ip.is_unspecified and mode != "lan":
            return f"{addr} listens on every network: only --mode lan, where every device must be paired, may do that"
        if mode == "local" and not ip.is_loopback:
            return f"{addr} is not this machine only: use --mode lan (pairing) or --mode tailscale"
        if mode == "tailscale" and not (ip.is_loopback or any(ip in net for net in TAILSCALE_NETS)):
            return f"{addr} is not a Tailscale address: use --mode lan to serve the local network"
    return None


def service_address(value: str, default: tuple[str, int]) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    try:
        return (host or default[0], int(port))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r}: host:port please") from None


def main(argv=None) -> int:
    global SHISUKO_URL, YOMITAN_URL, ANKI_URL
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("local", "lan", "tailscale"), default=os.environ.get("PORTAL_MODE") or "local",
                        help="who may use the portal: this machine only (default); devices on the local network "
                             "once paired; or your own Tailscale devices")
    parser.add_argument("--tailscale", action="store_true", help="the same as --mode tailscale")
    parser.add_argument("--host", "--bind", dest="hosts", action="append", default=[],
                        help="address to listen on (repeatable). Default: 127.0.0.1, plus in lan mode this "
                             "machine's LAN address and in tailscale mode its Tailscale address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORTAL_PORT") or 8791))
    parser.add_argument("--host-name", action="append", default=[],
                        help="extra Host name the page may be reached by (the listening addresses, localhost and "
                             "this machine's host and Tailscale names are always allowed)")
    parser.add_argument("--forget-devices", action="store_true", help="revoke every paired device, then exit")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help=f"card settings that override the page's (default {CONFIG_PATH}, optional)")
    parser.add_argument("--server", default="127.0.0.1:8790", help="the Shisu-ko server (host:port)")
    parser.add_argument("--yomitan", default="127.0.0.1:19633", help="the Yomitan API (host:port)")
    parser.add_argument("--anki", default="127.0.0.1:8765", help="AnkiConnect (host:port)")
    parser.add_argument("--tailscale-bin", default=shutil.which("tailscale") or "tailscale")
    args = parser.parse_args(argv)
    if args.tailscale:
        args.mode = "tailscale"
    # A console that cannot show the pairing QR code's block characters must not stop the portal.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    SHISUKO_URL = service_address(args.server, SHISUKO_URL)
    YOMITAN_URL = service_address(args.yomitan, YOMITAN_URL)
    ANKI_URL = service_address(args.anki, ANKI_URL)

    services = Services(args)
    if args.forget_devices:
        print(f"Forgot {services.state.forget()} paired device(s).")
        return 0

    binds = list(args.hosts) or [h.strip() for h in os.environ.get("PORTAL_HOST", "").split(",") if h.strip()]
    if not binds:
        binds = ["127.0.0.1"]
        if args.mode == "lan":
            ip = lan_ipv4()
            if not ip:
                log.error("--mode lan: no local network address found; name one with --host")
                return 2
            binds.append(ip)
        elif args.mode == "tailscale":
            ip = tailscale_ipv4(args.tailscale_bin)
            if not ip:
                log.error("--mode tailscale: `%s ip -4` gave no address (is Tailscale running?)", args.tailscale_bin)
                return 2
            binds.append(ip)
    problem = check_binds(args.mode, binds)
    if problem:
        log.error("%s", problem)
        return 2
    try:
        services.card_file = read_card_file(args.config)
    except (OSError, ValueError) as exc:
        log.error("%s: %s", args.config, exc)
        return 2
    card = services.card()
    log.info("card: %s", f"deck {card['deck']}, note type {card['note_model']}" if card else "not set up yet (the page asks)")
    if services.card_file:
        log.info("%s overrides: %s", args.config, ", ".join(sorted(services.card_file)))

    status = tailscale_status(args.tailscale_bin) if args.mode == "tailscale" else {}
    if args.mode == "tailscale":
        services.refresh_owners()
        log.info("owner devices: %s", ", ".join(sorted(services.owners)) or "none (is Tailscale running?)")
    Handler.services = services
    Handler.allowed_hosts = host_names(binds, args.host_name, status)
    public = next((b for b in binds if not ipaddress.ip_address(b).is_loopback and not ipaddress.ip_address(b).is_unspecified),
                  lan_ipv4() if any(ipaddress.ip_address(b).is_unspecified for b in binds) else None)
    if public:
        services.public_url = f"http://{public if ':' not in public else f'[{public}]'}:{args.port}"
        Handler.allowed_hosts.add(public)
    threads = [threading.Thread(target=serve, args=(b, args.port), daemon=True, name=f"serve-{b}") for b in binds]
    for t in threads:
        t.start()
    log.info("mode %s: open http://127.0.0.1:%d/ here%s", args.mode, args.port,
             f", or {services.public_url}/ on your devices" if public else "")
    if args.mode == "lan":
        services.new_pairing()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
