#!/usr/bin/env node
// Takes a release to the Chrome Web Store: asks the store what it holds against a version, and
// uploads a release's Chrome zip and submits it for review. The listing workflow
// (.github/workflows/cws-listing.yml) runs it for every release and on a schedule. The store
// reviews one submission at a time and takes a version only above the one before, so the script
// says what is in the way before anything is uploaded, and uploads only when nothing is. It speaks
// the store's API v2 alone: v1.1 stops on 2026-10-15. Node only, no packages.
//
//   node scripts/cws.mjs status <version>
//     prints one word for what the store holds against <version> (major.minor.patch, the newest
//     release), and the details on stderr ("published 0.5.0 (PUBLISHED); submitted 0.14.6
//     (PENDING_REVIEW)"):
//       submit      nothing in the way: upload <version> and submit it
//       published   the store publishes <version> or a newer version
//       in-review   <version> waits for its review
//       staged      <version> is approved and staged (only a Dashboard submission stages):
//                   publish it there
//       waiting     an older version waits for its review or is staged; <version> follows once
//                   the store has published it or its review is cancelled (rejected, it becomes
//                   rejected-older)
//       rejected    the store rejected <version>
//       rejected-older
//                   the store rejected an older version and nothing else is in the way: submit
//                   uploads <version> as it does for submit, but <version> may repeat what was
//                   rejected, so someone reads the review first
//       cancelled   the review of <version> was cancelled (in the Dashboard): it is not
//                   submitted again
//       newer       the store has a newer version in its submission than <version>
//       taken-down  the store has taken the item down
//   node scripts/cws.mjs submit <zip> <version> [--cancel-review] [--wait <seconds>]
//     uploads <zip>, submits it for review and prints the submission's state (PENDING_REVIEW). It
//     refuses a zip whose manifest.json does not hold <version>, and goes on only when status says
//     submit or rejected-older, or waiting with --cancel-review, which cancels the older version's
//     review first; the store must then say submit. While the store is still reading the upload
//     it is asked again every 10 seconds for up to --wait seconds (default 300), the last time at
//     the end of the wait; an upload that failed, is still being read then, or holds another
//     version is never submitted. The store's warnings go to stderr, and after the submission the
//     store must hold <version>.
//
// Exit codes: 0 done; 1 the store, the network or the zip failed (the message on stderr); 2 a
// usage error or missing or invalid credentials, found before anything is sent. stdout carries
// only the word or the state, for the workflow to read.
//
// Both commands need CWS_SERVICE_ACCOUNT_JSON, the JSON key of a Google Cloud service account
// that the Developer Dashboard knows, and CWS_PUBLISHER_ID, the Dashboard's publisher id
// (docs/cws/README.md). The key, the token signed with it and the access token Google trades for
// that are never printed, not even in an error.
import { createPrivateKey, createSign } from "node:crypto";
import { realpathSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { zipEntries } from "./amo-xpi.mjs";

export const ITEM_ID = "ecenifonpkaiccmmknpbllbebbfigjnm";
export const API = "https://chromewebstore.googleapis.com/v2/";
export const UPLOAD_API = "https://chromewebstore.googleapis.com/upload/v2/";
export const TOKEN_URL = "https://oauth2.googleapis.com/token";
export const SCOPE = "https://www.googleapis.com/auth/chromewebstore";
export const POLL_MS = 10_000;
const WAIT_S = 300;
const REQUEST_MS = 120_000;
const TOKEN_S = 3600;
const ERROR_CHARS = 300;
const VERSION_RE = /^\d+(\.\d+){0,3}$/;
const RELEASE_VERSION_RE = /^\d+\.\d+\.\d+$/;
const PUBLISHER_RE = /^[A-Za-z0-9._-]{1,128}$/;
const STATE_RE = /^[A-Z_]{1,64}$/;
const METHODS = new Set(["fetchStatus", "publish", "cancelSubmission"]);
const GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer";

// Text from the store or the zip goes into a single line of the log. GitHub Actions reads a
// workflow command from "::" at the start of a line and from the older "##[" anywhere in one, on
// stderr as on stdout: a newline could start the first, so newlines go, and "##[" is broken up.
// "# #[" cannot form "##[" again, next to another "#" or cut short.
function oneLine(text, max) {
  const line = String(text).replace(/[\s\p{Cc}]+/gu, " ").replace(/##\[/g, "# #[").trim();
  return line.length > max ? line.slice(0, max) : line;
}

// A state the store names, or the fallback when it names none that looks like one.
function stateName(state, fallback) {
  return typeof state === "string" && STATE_RE.test(state) ? state : fallback;
}

// Chrome's version numbers are one to four dot-separated integers, compared part by part as
// numbers, so 0.14.10 is newer than 0.14.9 and 0.5.0 older than 0.14.6; a missing part counts
// as 0.
export function compareVersions(a, b) {
  const parts = (version) => {
    if (typeof version !== "string" || !VERSION_RE.test(version)) throw new Error(`not a version number: ${oneLine(version, 40)}`);
    return version.split(".").map(Number);
  };
  const left = parts(a), right = parts(b);
  for (let i = 0; i < 4; i++) {
    const difference = (left[i] ?? 0) - (right[i] ?? 0);
    if (difference) return difference > 0 ? 1 : -1;
  }
  return 0;
}

function sameVersion(held, version) {
  return typeof held === "string" && VERSION_RE.test(held) && compareVersions(held, version) === 0;
}

// The service account's JSON key, as the Google Cloud console downloads it, checked before
// anything is sent. Every message says what is wrong and quotes nothing of the text: a
// JSON.parse error quotes the text it stopped at, and that text may be the private key.
export function serviceAccount(text) {
  if (typeof text !== "string" || !text.trim()) throw new Error("CWS_SERVICE_ACCOUNT_JSON is not set");
  let key;
  try {
    key = JSON.parse(text.replace(/^\uFEFF/, ""));
  } catch {
    throw new Error("CWS_SERVICE_ACCOUNT_JSON is not JSON");
  }
  if (!key || typeof key !== "object" || key.type !== "service_account") {
    throw new Error('CWS_SERVICE_ACCOUNT_JSON is not the JSON key of a service account (its "type" is not "service_account")');
  }
  if (typeof key.client_email !== "string" || !/^[^\s@]+@[^\s@]+$/.test(key.client_email)) {
    throw new Error("CWS_SERVICE_ACCOUNT_JSON has no client_email");
  }
  let privateKey = null;
  try {
    if (typeof key.private_key === "string") privateKey = createPrivateKey(key.private_key);
  } catch {
    privateKey = null;
  }
  if (privateKey?.asymmetricKeyType !== "rsa") throw new Error("CWS_SERVICE_ACCOUNT_JSON has no RSA private_key");
  const keyId = typeof key.private_key_id === "string" && key.private_key_id ? key.private_key_id : undefined;
  return { clientEmail: key.client_email, privateKey, keyId };
}

// The token the service account trades for an access token (Google's JWT bearer grant). It lives
// an hour, the longest Google takes, and its audience is the fixed token URL: a key file's own
// token_uri is never asked, so a changed key cannot send the signed token anywhere else.
export function assertion(account, now = Date.now()) {
  const part = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const iat = Math.floor(now / 1000);
  const header = account.keyId ? { alg: "RS256", typ: "JWT", kid: account.keyId } : { alg: "RS256", typ: "JWT" };
  const claims = { iss: account.clientEmail, scope: SCOPE, aud: TOKEN_URL, iat, exp: iat + TOKEN_S };
  const unsigned = `${part(header)}.${part(claims)}`;
  return `${unsigned}.${createSign("RSA-SHA256").update(unsigned).sign(account.privateKey, "base64url")}`;
}

// The publisher id comes from a repository variable. It must be one path segment: a lone "." or
// ".." passes the character check and would be resolved away by the URL parser.
function itemPath(publisherId) {
  if (typeof publisherId !== "string" || !PUBLISHER_RE.test(publisherId) || /^\.+$/.test(publisherId)) {
    throw new Error("not a publisher id (1 to 128 letters, digits, dots, underscores or hyphens)");
  }
  return `publishers/${encodeURIComponent(publisherId)}/items/${ITEM_ID}`;
}

export function itemUrl(publisherId, method) {
  if (!METHODS.has(method)) throw new Error(`not a Chrome Web Store method: ${method}`);
  return `${API}${itemPath(publisherId)}:${method}`;
}

export function uploadUrl(publisherId) {
  return `${UPLOAD_API}${itemPath(publisherId)}:upload`;
}

// The version a revision carries: the highest of its distribution channels' (a staged rollout
// has more than one), or null when it names none.
export function revisionVersion(revision) {
  const channels = Array.isArray(revision?.distributionChannels) ? revision.distributionChannels : [];
  let highest = null;
  for (const channel of channels) {
    const version = channel?.crxVersion;
    if (typeof version !== "string" || !VERSION_RE.test(version)) continue;
    if (highest === null || compareVersions(version, highest) > 0) highest = version;
  }
  return highest;
}

const SAME_VERSION = new Map([
  ["PENDING_REVIEW", "in-review"], ["STAGED", "staged"], ["REJECTED", "rejected"], ["CANCELLED", "cancelled"],
  ["PUBLISHED", "published"], ["PUBLISHED_TO_TESTERS", "published"],
]);
const BUSY = new Set(["PENDING_REVIEW", "STAGED"]);

// What the store holds against a version, as the one word the usage above explains, from a
// fetchStatus answer. Only "submit" and "rejected-older" lead to an upload. A state this script
// does not know counts, for the version itself, as the store being busy with it, never as a
// reason to upload again; for an older version it counts as finished. A submission that names no
// version is taken for an older one.
export function decide(status, version) {
  if (status?.takenDown === true) return "taken-down";
  const published = revisionVersion(status?.publishedItemRevisionStatus);
  if (published !== null && compareVersions(published, version) >= 0) return "published";
  const submitted = status?.submittedItemRevisionStatus;
  if (!submitted) return "submit";
  const held = revisionVersion(submitted);
  const order = held === null ? -1 : compareVersions(held, version);
  if (order === 0) return SAME_VERSION.get(submitted.state) ?? "in-review";
  if (order > 0) return "newer";
  if (BUSY.has(submitted.state)) return "waiting";
  // A rejected older version is nothing the store waits on, but a release made while it was in
  // review may repeat what was rejected, and no run ever asks about the older version again: it
  // has its own word, so that the workflow can leave the submission to someone who has read the
  // review rather than send the same problem back for a second one.
  return submitted.state === "REJECTED" ? "rejected-older" : "submit";
}

// The details line under the word: what is published and what is submitted, each with its state.
export function describe(status) {
  const revision = (label, value) => {
    if (!value) return `nothing ${label}`;
    return `${label} ${revisionVersion(value) ?? "no version"} (${stateName(value.state, "no state")})`;
  };
  const parts = [
    revision("published", status?.publishedItemRevisionStatus),
    revision("submitted", status?.submittedItemRevisionStatus),
  ];
  if (status?.lastAsyncUploadState) parts.push(`last upload ${stateName(status.lastAsyncUploadState, "in an unknown state")}`);
  if (status?.takenDown === true) parts.push("taken down");
  if (status?.warned === true) parts.push("warned");
  return parts.join("; ");
}

// One line for a failed request: the HTTP code, then Google's own words in either of its shapes
// (the store's {error: {status, message}}, the token endpoint's {error, error_description}), or
// else the body itself, cut to ERROR_CHARS, since a proxy's error page can be long.
export function apiError(response, text) {
  let code = "", message = "";
  try {
    const body = JSON.parse(text);
    const error = body?.error;
    if (error && typeof error === "object") {
      if (typeof error.status === "string") code = error.status;
      if (typeof error.message === "string") message = error.message;
    } else if (typeof error === "string") {
      code = error;
      if (typeof body.error_description === "string") message = body.error_description;
    }
  } catch {
    // Not JSON: the body is all there is to show.
  }
  if (!code && !message) message = String(text ?? "");
  const head = oneLine(`HTTP ${response.status} ${oneLine(code, 64)}`, 80);
  const tail = oneLine(message, ERROR_CHARS);
  return tail ? `${head}: ${tail}` : head;
}

// The zip must be the release it claims to be before the store sees it: the store reads the
// version from the manifest, and once a version is submitted no later upload may go below it.
async function releaseZip(path, version) {
  const bytes = await readFile(path);
  // The parser's errors quote an entry's name, and that name is the zip's own text: it goes into
  // one line like the store's.
  let entries;
  try {
    entries = zipEntries(bytes);
  } catch (err) {
    throw new Error(`${path}: ${oneLine(err?.message ?? err, ERROR_CHARS)}`);
  }
  const manifest = entries.get("manifest.json");
  if (!manifest) throw new Error(`${path} holds no manifest.json`);
  let held;
  try {
    held = JSON.parse(manifest.toString("utf8").replace(/^\uFEFF/, "")).version;
  } catch {
    throw new Error(`${path}: its manifest.json is not JSON`);
  }
  if (held !== version) throw new Error(`${path} holds version ${oneLine(held ?? "none", 40)}, not ${version}: not uploading it`);
  return bytes;
}

const USAGE = "usage: cws.mjs status <version> | submit <zip> <version> [--cancel-review] [--wait <seconds>]";

const REFUSALS = new Map([
  ["published", "the store publishes it or a newer version already"],
  ["in-review", "it waits for its review"],
  ["staged", "it is approved and staged: publish it in the Developer Dashboard"],
  ["waiting", "an older version waits for its review or is staged (--cancel-review cancels that review)"],
  ["rejected", "the store rejected it"],
  ["cancelled", "its review was cancelled in the Developer Dashboard"],
  ["newer", "the store's submission holds a newer version"],
  ["taken-down", "the store has taken the item down"],
]);

function parseArgs(argv) {
  const [command, ...rest] = argv;
  if (command !== "status" && command !== "submit") throw new Error(USAGE);
  const opts = { command, cancelReview: false, wait: WAIT_S };
  const positional = [];
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (command === "submit" && arg === "--cancel-review") opts.cancelReview = true;
    else if (command === "submit" && arg === "--wait") {
      const value = rest[++i];
      if (!/^\d+$/.test(value ?? "")) throw new Error(`--wait takes a whole number of seconds: ${value ?? "nothing"}`);
      opts.wait = Number(value);
    } else if (arg.startsWith("-")) throw new Error(`unknown argument: ${arg}`);
    else positional.push(arg);
  }
  if (command === "status" && positional.length === 1) [opts.version] = positional;
  else if (command === "submit" && positional.length === 2 && positional[0]) [opts.zip, opts.version] = positional;
  else throw new Error(USAGE);
  if (!RELEASE_VERSION_RE.test(opts.version)) throw new Error(`not a release version (major.minor.patch): ${opts.version}`);
  return opts;
}

export async function run(argv, {
  env = process.env,
  fetch = globalThis.fetch,
  sleep = (ms) => new Promise((done) => setTimeout(done, ms)),
  now = Date.now,
  log = console.log,
  warn = console.error,
} = {}) {
  // Every line goes out through scrub, and that scrub is the one that counts. No message the
  // script writes quotes the signed token or the access token, but two kinds of text it passes on
  // can: an error body is the server's text, and a server that echoes a request echoes them; and
  // fetch's own rejection quotes the request's headers ('Headers.append: "Bearer ..." is an
  // invalid header value.'), which request() passes on as it is. request() also scrubs a body
  // before apiError cuts it, so no prefix of either survives the cut.
  const secrets = [];
  const scrub = (text) => secrets.reduce((line, secret) => line.split(secret).join("[redacted]"), String(text));
  const out = (line) => log(scrub(line));
  const say = (line) => warn(scrub(line));

  let opts, account, publisher;
  try {
    opts = parseArgs(argv);
    account = serviceAccount(env.CWS_SERVICE_ACCOUNT_JSON);
    publisher = env.CWS_PUBLISHER_ID;
    if (!publisher) throw new Error("CWS_PUBLISHER_ID is not set");
    try {
      itemUrl(publisher, "fetchStatus");
    } catch (err) {
      throw new Error(`CWS_PUBLISHER_ID is ${err.message}`);
    }
  } catch (err) {
    say(err.message);
    return 2;
  }

  const request = async (label, url, init, { json = true } = {}) => {
    // The body is read inside the try: the timeout also ends a body that stalls after the
    // headers, and a connection can break in the middle of one; either is this request's failure.
    let response, text;
    try {
      response = await fetch(url, { ...init, signal: AbortSignal.timeout(REQUEST_MS) });
      text = await response.text();
    } catch (err) {
      const why = err?.name === "TimeoutError"
        ? `no answer within ${REQUEST_MS / 1000} s`
        : `${err?.message}${err?.cause?.code ? ` (${err.cause.code})` : ""}`;
      throw new Error(`${label} failed: ${why}`);
    }
    if (!response.ok) throw new Error(`${label} failed: ${apiError(response, scrub(text))}`);
    if (!json) return {};
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      throw new Error(`${label}: the answer is not JSON`);
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error(`${label}: the answer is not a JSON object`);
    return body;
  };

  try {
    // Before anything is sent: a wrong zip needs no token.
    const zip = opts.command === "submit" ? await releaseZip(opts.zip, opts.version) : null;

    // One token for the whole run: it lives an hour, and a run takes minutes.
    const signed = assertion(account, now());
    secrets.push(signed);
    const granted = await request("the token request", TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: GRANT, assertion: signed }).toString(),
    });
    const token = granted.access_token;
    if (typeof token !== "string" || !token) throw new Error("the token request failed: Google's answer holds no access_token");
    secrets.push(token);
    const auth = { Authorization: `Bearer ${token}` };

    const fetchStatus = () => request("fetchStatus", itemUrl(publisher, "fetchStatus"), { method: "GET", headers: auth });
    const ask = async () => {
      const status = await fetchStatus();
      say(describe(status));
      return decide(status, opts.version);
    };

    if (opts.command === "status") {
      out(await ask());
      return 0;
    }

    let word = await ask();
    if (word === "waiting" && opts.cancelReview) {
      say(`cancelling the review that waits, so that ${opts.version} can be submitted`);
      await request("cancelSubmission", itemUrl(publisher, "cancelSubmission"), { method: "POST", headers: auth }, { json: false });
      word = await ask();
      if (word !== "submit") throw new Error(`not submitting ${opts.version}: the store still says ${word} after the cancel`);
    }
    // The workflow asks for a submission past an older rejection only as a person's decision: in a
    // run by hand, or in the first run of the newest release's own release workflow. The schedule
    // and a re-run of a release workflow only warn.
    if (word === "rejected-older") say(`the store rejected the older version in its submission; submitting ${opts.version} in its place`);
    else if (word !== "submit") throw new Error(`not submitting ${opts.version}: ${word}, ${REFUSALS.get(word) ?? "the store is busy with it"}`);

    say(`uploading ${opts.zip} (${zip.length} bytes)`);
    const uploaded = await request("the upload", uploadUrl(publisher), {
      method: "POST",
      headers: { ...auth, "X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-File-Name": "extension.zip" },
      body: zip,
    });
    if (uploaded.uploadState === "SUCCEEDED") {
      if (!sameVersion(uploaded.crxVersion, opts.version)) {
        const read = oneLine(uploaded.crxVersion ?? "none", 40);
        throw new Error(`the store read version ${read} from the upload, not ${opts.version}: not submitting it`);
      }
    } else if (uploaded.uploadState === "IN_PROGRESS") {
      // The store took the zip and reads it in the background; fetchStatus says how that ends. It
      // does not name the version: the manifest was checked before the upload, and the
      // submission's version is checked after the publish.
      say(`the store is still reading the upload; asking every ${POLL_MS / 1000} s for up to ${opts.wait} s`);
      const deadline = now() + opts.wait * 1000;
      for (;;) {
        await sleep(Math.min(POLL_MS, Math.max(0, deadline - now())));
        const state = (await fetchStatus()).lastAsyncUploadState;
        if (state === "SUCCEEDED") break;
        if (state === "FAILED" || state === "NOT_FOUND") throw new Error(`the upload failed: ${state}`);
        if (now() >= deadline) {
          throw new Error(`the upload is still ${stateName(state, "in an unknown state")} after ${opts.wait} s: not submitting it`);
        }
      }
    } else {
      throw new Error(`the upload failed: ${stateName(uploaded.uploadState, "the store names no upload state")}`);
    }

    const submitted = await request("publish", itemUrl(publisher, "publish"), {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ publishType: "DEFAULT_PUBLISH" }),
    });
    const warnings = Array.isArray(submitted.warningInfo?.warnings) ? submitted.warningInfo.warnings : [];
    for (const warning of warnings) {
      say(`the store warns: ${oneLine(warning?.reason ?? "", 80)}: ${oneLine(warning?.description ?? "", ERROR_CHARS)}`);
    }

    // The publish answers for whatever the draft held; the store's own status says it is this
    // version that went in.
    const after = await fetchStatus();
    say(describe(after));
    const held = [after.submittedItemRevisionStatus, after.publishedItemRevisionStatus].map(revisionVersion);
    if (!held.some((version) => sameVersion(version, opts.version))) {
      throw new Error(`the store's submission does not hold ${opts.version} after the publish: ${describe(after)}`);
    }
    out(stateName(submitted.state, stateName(after.submittedItemRevisionStatus?.state, "ITEM_STATE_UNSPECIFIED")));
    return 0;
  } catch (err) {
    say(err?.message ?? String(err));
    return 1;
  }
}

// Run as a script, not imported. The module's URL has its symlinks and junctions resolved while
// argv[1] keeps the path it was started by, so the two are compared as real paths: otherwise a
// checkout reached through a junction would run nothing and exit 0.
function isMain(argv1) {
  if (!argv1) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(argv1)).href;
  } catch {
    return false;
  }
}

if (isMain(process.argv[1])) {
  process.exitCode = await run(process.argv.slice(2));
}
