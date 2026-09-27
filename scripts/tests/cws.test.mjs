import assert from "node:assert/strict";
import { createVerify, generateKeyPairSync } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, mock, test } from "node:test";
import { fileURLToPath } from "node:url";
import { inspect } from "node:util";
import {
  API, ITEM_ID, POLL_MS, SCOPE, TOKEN_URL, UPLOAD_API, apiError, assertion, compareVersions, decide, describe, itemUrl,
  revisionVersion, run, serviceAccount, uploadUrl,
} from "../cws.mjs";

// A real key pair, so the signature is checked the way Google checks it.
const { publicKey: PUBLIC_KEY, privateKey: PRIVATE_KEY } = generateKeyPairSync("rsa", {
  modulusLength: 2048,
  publicKeyEncoding: { type: "spki", format: "pem" },
  privateKeyEncoding: { type: "pkcs8", format: "pem" },
});
const KEY = {
  type: "service_account",
  project_id: "shisu-ko-test",
  private_key_id: "0123456789abcdef0123456789abcdef01234567",
  private_key: PRIVATE_KEY,
  client_email: "cws-publisher@shisu-ko-test.iam.gserviceaccount.com",
  client_id: "123456789",
  token_uri: "https://oauth2.googleapis.com/token",
};
const PUBLISHER = "f3a9c2e1-publisher_7.x";
const ENV = { CWS_SERVICE_ACCOUNT_JSON: JSON.stringify(KEY), CWS_PUBLISHER_ID: PUBLISHER };
const ACCESS_TOKEN = `ya29.FAKE-ACCESS-TOKEN-${"q7Zx".repeat(40)}`;
const START = 1_790_000_000_000;
const VERSION = "0.14.6";

const STORE = `https://chromewebstore.googleapis.com/v2/publishers/${PUBLISHER}/items/ecenifonpkaiccmmknpbllbebbfigjnm`;
const STATUS_URL = `${STORE}:fetchStatus`;
const PUBLISH_URL = `${STORE}:publish`;
const CANCEL_URL = `${STORE}:cancelSubmission`;
const UPLOAD_URL = `https://chromewebstore.googleapis.com/upload/v2/publishers/${PUBLISHER}/items/ecenifonpkaiccmmknpbllbebbfigjnm:upload`;

const revision = (state, ...versions) => ({
  state, distributionChannels: versions.map((crxVersion) => ({ deployPercentage: 100, crxVersion })),
});
const status = (fields = {}) => ({ name: `publishers/${PUBLISHER}/items/${ITEM_ID}`, itemId: ITEM_ID, publicKey: "MIIBIjANBg", ...fields });
const OLD = status({ publishedItemRevisionStatus: revision("PUBLISHED", "0.5.0") });
const SUBMITTED = status({
  publishedItemRevisionStatus: revision("PUBLISHED", "0.5.0"), submittedItemRevisionStatus: revision("PENDING_REVIEW", VERSION),
});
const WAITING = status({
  publishedItemRevisionStatus: revision("PUBLISHED", "0.5.0"), submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.14.5"),
});
const REJECTED_OLDER = status({
  publishedItemRevisionStatus: revision("PUBLISHED", "0.5.0"), submittedItemRevisionStatus: revision("REJECTED", "0.14.5"),
});
const READING = { ...OLD, lastAsyncUploadState: "IN_PROGRESS" };
const TOKEN = { access_token: ACCESS_TOKEN, expires_in: 3599, token_type: "Bearer" };
const UPLOADED = { name: `publishers/${PUBLISHER}/items/${ITEM_ID}`, itemId: ITEM_ID, crxVersion: VERSION, uploadState: "SUCCEEDED" };
const PUBLISHED = { name: `publishers/${PUBLISHER}/items/${ITEM_ID}`, itemId: ITEM_ID, state: "PENDING_REVIEW" };
const answer = (code, body) => () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status: code });

// Which endpoint a request went to, for the queues and for the order of calls.
function endpointOf(url) {
  if (url === "https://oauth2.googleapis.com/token") return "token";
  if (url.startsWith("https://chromewebstore.googleapis.com/upload/v2/")) return "upload";
  return url.split(":").at(-1);
}

// The store and Google's token endpoint behind an injected fetch: every request is recorded, with
// the fake clock's time since the start, and answered from its endpoint's queue, whose last answer
// repeats. An answer is a JSON body, or a function of the request that returns a Response.
function harness(routes, { env = ENV } = {}) {
  const out = [], err = [], calls = [];
  let clock = START;
  const queues = Object.fromEntries(Object.entries(routes).map(([name, answers]) => [name, [...answers]]));
  const fetch = async (url, init = {}) => {
    const call = {
      endpoint: endpointOf(url), url, method: init.method ?? "GET",
      headers: Object.fromEntries(new Headers(init.headers)), body: init.body, signal: init.signal, at: clock - START,
    };
    calls.push(call);
    // The longest run asks 34 times (a token, a status, the upload and 31 polls in five minutes).
    // A loop that never ends, such as one whose deadline the fake clock never passes, fails here
    // in milliseconds: the fake sleep and answers never yield to a timer, so no test timeout
    // could stop it before the heap runs out.
    if (calls.length > 200) throw new Error(`runaway loop: ${calls.length} requests`);
    const queue = queues[call.endpoint];
    if (!queue?.length) return new Response(`nothing queued for ${url}`, { status: 599 });
    const next = queue.length > 1 ? queue.shift() : queue[0];
    return typeof next === "function" ? next(call) : Response.json(next);
  };
  const deps = {
    env, fetch, now: () => clock, sleep: async (ms) => { clock += ms; },
    log: (line) => out.push(line), warn: (line) => err.push(line),
  };
  const endpoints = () => calls.map((call) => call.endpoint);
  const times = (endpoint) => calls.filter((call) => call.endpoint === endpoint).map((call) => call.at);
  return { deps, calls, out, err, endpoints, times };
}

// Whatever a run printed holds nothing of the key, of the token signed with it, or of the access
// token: the listing workflow's log is public.
function assertClean(h) {
  const signed = h.calls.filter((call) => call.endpoint === "token").map((call) => new URLSearchParams(call.body).get("assertion"));
  const keyLines = PRIVATE_KEY.split("\n").filter((line) => line.length >= 16 && !line.startsWith("-----"));
  for (const line of [...h.out, ...h.err]) {
    assert.equal(typeof line, "string");
    assert.ok(!line.includes(ACCESS_TOKEN.slice(0, 22)), `the access token in: ${line}`);
    assert.ok(!line.includes("PRIVATE KEY"), `the private key in: ${line}`);
    for (const keyLine of keyLines) assert.ok(!line.includes(keyLine), `the private key in: ${line}`);
    for (const jwt of signed) {
      assert.ok(!line.includes(jwt), `the signed token in: ${line}`);
      assert.ok(!line.includes(jwt.split(".")[2].slice(0, 20)), `the signed token's signature in: ${line}`);
    }
  }
}

// GitHub Actions reads a workflow command from "::" at the start of a line and from the older
// "##[" anywhere in one, on stderr as on stdout: no line a run prints may hold either.
function assertNoCommands(h) {
  for (const line of [...h.out, ...h.err]) {
    assert.ok(!/[\r\n]/.test(line), `a line break in: ${JSON.stringify(line)}`);
    assert.ok(!line.trimStart().startsWith("::"), `a workflow command in: ${line}`);
    assert.ok(!line.includes("##["), `a workflow command in: ${line}`);
  }
}

// Every request carries its own timeout of 120 seconds: without one, a connection that stalls
// holds the listing job, and the runs queued behind it, until GitHub's six-hour limit, and one in
// seconds for milliseconds would give up on every upload. AbortSignal.timeout is watched for the
// run, so each request's signal is known to come from a timeout, and from which.
async function go(h, argv) {
  const timeouts = new Map();
  const timeout = AbortSignal.timeout;
  const spy = mock.method(AbortSignal, "timeout", (ms) => {
    const signal = timeout.call(AbortSignal, ms);
    timeouts.set(signal, ms);
    return signal;
  });
  let code;
  try {
    code = await run(argv, h.deps);
  } finally {
    spy.mock.restore();
  }
  assertClean(h);
  assertNoCommands(h);
  for (const call of h.calls) assert.equal(timeouts.get(call.signal), 120_000, `no 120 s timeout on ${call.url}`);
  return code;
}

// A stored zip as scripts/build.mjs writes the release's Chrome package.
function zip(files) {
  const locals = [], central = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const data = Buffer.from(text);
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt32LE(data.length, 18); local.writeUInt32LE(data.length, 22); local.writeUInt16LE(nameBytes.length, 26);
    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt32LE(data.length, 20); entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28); entry.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, data);
    central.push(entry, nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const dir = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(Object.keys(files).length, 8); end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(dir.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, dir, end]);
}

const DIR = mkdtempSync(join(tmpdir(), "cws-"));
after(() => rmSync(DIR, { recursive: true, force: true }));

function zipFile(name, files) {
  const path = join(DIR, name);
  writeFileSync(path, zip(files));
  return path;
}
const manifest = (version) => `${JSON.stringify({ manifest_version: 3, name: "Shisu-ko", version }, null, 2)}\n`;
const ZIP = zipFile("release.zip", { "manifest.json": manifest(VERSION), "content.js": "run();\n", "icons/icon-128.png": "png" });

const decode = (part) => JSON.parse(Buffer.from(part, "base64url").toString("utf8"));

// API v1.1 stops on 2026-10-15: every request must go to v2, for the one item the listing is.
test("the constants name the store's v2 API, Google's token URL and the Shisu-ko item", () => {
  assert.equal(ITEM_ID, "ecenifonpkaiccmmknpbllbebbfigjnm");
  assert.equal(API, "https://chromewebstore.googleapis.com/v2/");
  assert.equal(UPLOAD_API, "https://chromewebstore.googleapis.com/upload/v2/");
  assert.equal(TOKEN_URL, "https://oauth2.googleapis.com/token");
  assert.equal(SCOPE, "https://www.googleapis.com/auth/chromewebstore");
  assert.equal(POLL_MS, 10_000);
});

// The store refuses a version not above the last, and a string comparison puts 0.5.0 above 0.14.6.
test("versions compare as numbers part by part, a missing part counting as 0", () => {
  assert.equal(compareVersions("0.14.10", "0.14.9"), 1);
  assert.equal(compareVersions("0.14.9", "0.14.10"), -1);
  assert.equal(compareVersions("0.5.0", "0.14.6"), -1);
  assert.equal(compareVersions("0.14.6", "0.14.6"), 0);
  assert.equal(compareVersions("1", "1.0.0.0"), 0);
  assert.equal(compareVersions("1.2.3.4", "1.2.3"), 1);
  assert.equal(compareVersions("1.2.3.4", "1.2.3.5"), -1);
  assert.equal(compareVersions("2", "1.99.99.99"), 1);
  for (const bad of ["", "1.2.3.4.5", "a.b", "1..2", "-1", " 1", "v1.0", "1.0.", undefined, null, 1]) {
    assert.throws(() => compareVersions(bad, "1.0"), /not a version number/);
    assert.throws(() => compareVersions("1.0", bad), /not a version number/);
  }
});

// A staged rollout lists a channel per share; the revision is the newest version it rolls out.
test("a revision's version is the highest of its channels', or null when it names none", () => {
  assert.equal(revisionVersion(undefined), null);
  assert.equal(revisionVersion({ state: "PENDING_REVIEW" }), null);
  assert.equal(revisionVersion({ distributionChannels: [] }), null);
  assert.equal(revisionVersion(revision("PUBLISHED", "0.14.6")), "0.14.6");
  assert.equal(revisionVersion(revision("PUBLISHED", "0.14.9", "0.14.10", "0.5.0")), "0.14.10");
  assert.equal(revisionVersion({ distributionChannels: [{ deployPercentage: 10 }, { crxVersion: "not a version" }, { crxVersion: "0.1.0" }] }), "0.1.0");
});

// Every word the workflow acts on comes from these rules, in this order: a wrong "submit" uploads
// over a review, a wrong anything else holds a release back for good.
test("decide gives each of the store's states its word", () => {
  const published = (...versions) => ({ publishedItemRevisionStatus: revision("PUBLISHED", ...versions) });
  const submitted = (state, ...versions) => ({ submittedItemRevisionStatus: revision(state, ...versions) });
  const rows = [
    ["taken down, whatever else it holds", status({ takenDown: true, ...published("0.14.7") }), "taken-down"],
    ["taken down with nothing else", status({ takenDown: true }), "taken-down"],
    ["not taken down", status({ takenDown: false, warned: false }), "submit"],
    ["nothing published, nothing submitted", status(), "submit"],
    ["an older version published, nothing submitted", status(published("0.5.0")), "submit"],
    ["the version published", status(published(VERSION)), "published"],
    ["a newer version published", status(published("0.14.7")), "published"],
    ["published to testers", status({ publishedItemRevisionStatus: revision("PUBLISHED_TO_TESTERS", VERSION) }), "published"],
    ["published beats a submission", status({ ...published(VERSION), ...submitted("REJECTED", "0.14.7") }), "published"],
    ["in review", status({ ...published("0.5.0"), ...submitted("PENDING_REVIEW", VERSION) }), "in-review"],
    ["staged", status(submitted("STAGED", VERSION)), "staged"],
    ["rejected", status(submitted("REJECTED", VERSION)), "rejected"],
    ["cancelled", status(submitted("CANCELLED", VERSION)), "cancelled"],
    ["submitted and published", status(submitted("PUBLISHED", VERSION)), "published"],
    ["submitted and published to testers", status(submitted("PUBLISHED_TO_TESTERS", VERSION)), "published"],
    ["the version in an unspecified state", status(submitted("ITEM_STATE_UNSPECIFIED", VERSION)), "in-review"],
    ["the version in a state the script does not know", status(submitted("SOMETHING_NEW", VERSION)), "in-review"],
    ["the version with no state", status({ submittedItemRevisionStatus: { distributionChannels: [{ crxVersion: VERSION }] } }), "in-review"],
    ["the version under a name no Map key may shadow", status(submitted("constructor", VERSION)), "in-review"],
    ["a newer version in review", status(submitted("PENDING_REVIEW", "0.14.7")), "newer"],
    ["a newer version rejected", status(submitted("REJECTED", "0.15.0")), "newer"],
    ["an older version in review", status({ ...published("0.5.0"), ...submitted("PENDING_REVIEW", "0.14.5") }), "waiting"],
    ["an older version staged", status(submitted("STAGED", "0.14.5")), "waiting"],
    // A release made while an older version was in review may repeat what was rejected: the
    // rejection has its own word, so that the schedule does not submit past it unread.
    ["an older version rejected", status(submitted("REJECTED", "0.14.5")), "rejected-older"],
    ["an older version rejected, an older one published", status({ ...published("0.5.0"), ...submitted("REJECTED", "0.14.5") }), "rejected-older"],
    ["an older version cancelled", status(submitted("CANCELLED", "0.14.5")), "submit"],
    ["an older version in an unspecified state", status(submitted("ITEM_STATE_UNSPECIFIED", "0.14.5")), "submit"],
    ["an older version in a state the script does not know", status(submitted("SOMETHING_NEW", "0.14.5")), "submit"],
    ["a submission in review that names no version", status(submitted("PENDING_REVIEW")), "waiting"],
    ["a rejected submission that names no version", status(submitted("REJECTED")), "rejected-older"],
    ["a cancelled submission that names no version", status(submitted("CANCELLED")), "submit"],
    ["channels: the highest is the version", status(submitted("PENDING_REVIEW", "0.14.5", VERSION)), "in-review"],
    ["channels: a newer one among them", status(submitted("PENDING_REVIEW", "0.14.7", "0.14.5")), "newer"],
    ["channels: the highest published counts", status({ publishedItemRevisionStatus: revision("PUBLISHED", "0.14.5", "0.14.6") }), "published"],
  ];
  for (const [what, given, word] of rows) assert.equal(decide(given, VERSION), word, what);
  // Versions are numbers: 0.14.10 is past 0.14.9, and 0.5.0 is not past 0.14.6.
  assert.equal(decide(status({ publishedItemRevisionStatus: revision("PUBLISHED", "0.14.10") }), "0.14.9"), "published");
  assert.equal(decide(status({ publishedItemRevisionStatus: revision("PUBLISHED", "0.14.9") }), "0.14.10"), "submit");
  assert.equal(decide(status({ submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.14.10") }), "0.14.9"), "newer");
  assert.equal(decide(status({ submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.14.9") }), "0.14.10"), "waiting");
  assert.equal(decide(status({ publishedItemRevisionStatus: revision("PUBLISHED", "0.5.0") }), "0.14.6"), "submit");
  assert.equal(decide(status({ submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.5.0") }), "0.14.6"), "waiting");
});

// The line under the word is what the workflow's notice points to; it must stay one line.
test("describe says what is published and submitted, in one line", () => {
  assert.equal(describe(SUBMITTED), "published 0.5.0 (PUBLISHED); submitted 0.14.6 (PENDING_REVIEW)");
  assert.equal(describe(status()), "nothing published; nothing submitted");
  assert.equal(
    describe(status({ lastAsyncUploadState: "FAILED", takenDown: true, warned: true })),
    "nothing published; nothing submitted; last upload FAILED; taken down; warned",
  );
  assert.equal(describe(status({ submittedItemRevisionStatus: { state: "PENDING_REVIEW" } })), "nothing published; submitted no version (PENDING_REVIEW)");
  const odd = describe(status({ submittedItemRevisionStatus: revision("X\n::error::Y", VERSION) }));
  assert.equal(odd, "nothing published; submitted 0.14.6 (no state)");
});

// A failed request is one line of the log, in Google's words, whatever shape its body has.
test("apiError gives the HTTP code, Google's status and message, and cuts the body", () => {
  const google = JSON.stringify({ error: { code: 403, message: "The caller does not have permission", status: "PERMISSION_DENIED", details: [] } });
  assert.equal(apiError({ status: 403 }, google), "HTTP 403 PERMISSION_DENIED: The caller does not have permission");
  const grant = JSON.stringify({ error: "invalid_grant", error_description: "Invalid JWT Signature." });
  assert.equal(apiError({ status: 400 }, grant), "HTTP 400 invalid_grant: Invalid JWT Signature.");
  assert.equal(apiError({ status: 500 }, "<html>\n<body>Server Error</body>\n</html>\n"), "HTTP 500: <html> <body>Server Error</body> </html>");
  assert.equal(apiError({ status: 503 }, ""), "HTTP 503");
  assert.equal(apiError({ status: 400 }, '{"foo":1}'), 'HTTP 400: {"foo":1}');
  assert.equal(apiError({ status: 502 }, "x".repeat(1000)), `HTTP 502: ${"x".repeat(300)}`);
  const long = JSON.stringify({ error: { status: "INVALID_ARGUMENT", message: `a\r\nb ${"y".repeat(1000)}` } });
  const line = apiError({ status: 400 }, long);
  assert.ok(line.startsWith("HTTP 400 INVALID_ARGUMENT: a b yyy"), line);
  assert.equal(line.length, "HTTP 400 INVALID_ARGUMENT: ".length + 300);
  assert.ok(!/[\r\n]/.test(line));
  // GitHub Actions reads "##[" anywhere in a line as a workflow command.
  const command = JSON.stringify({ error: { status: "##[add-mask]", message: "no ##[error]here" } });
  assert.equal(apiError({ status: 400 }, command), "HTTP 400 # #[add-mask]: no # #[error]here");
  assert.equal(apiError({ status: 502 }, "##[group]x"), "HTTP 502: # #[group]x");
});

// The key is read before anything is sent, and nothing of it is ever quoted: a JSON.parse error
// quotes the text it stopped at, which is the key.
test("serviceAccount reads a service account's key and quotes nothing of it when it cannot", () => {
  const account = serviceAccount(JSON.stringify(KEY));
  assert.equal(account.clientEmail, KEY.client_email);
  assert.equal(account.keyId, KEY.private_key_id);
  assert.equal(account.privateKey.asymmetricKeyType, "rsa");
  // Logged by mistake, the account shows no key.
  assert.ok(!JSON.stringify(account).includes("PRIVATE KEY"));
  assert.ok(!inspect(account, { depth: 5 }).includes("PRIVATE KEY"));
  const { private_key_id: unused, ...noId } = KEY;
  assert.ok(unused);
  assert.equal(serviceAccount(JSON.stringify(noId)).keyId, undefined);

  const { privateKey: ecKey } = generateKeyPairSync("ec", {
    namedCurve: "prime256v1",
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
  const bad = [
    [undefined, /is not set/], ["", /is not set/], ["  \n", /is not set/],
    [JSON.stringify(KEY).replace('"client_email"', 'client_email"'), /^CWS_SERVICE_ACCOUNT_JSON is not JSON$/],
    [JSON.stringify(KEY).slice(0, -30), /^CWS_SERVICE_ACCOUNT_JSON is not JSON$/],
    ["null", /not the JSON key of a service account/], ["[]", /not the JSON key of a service account/],
    [JSON.stringify({ ...KEY, type: "authorized_user" }), /not the JSON key of a service account/],
    [JSON.stringify({ ...KEY, client_email: undefined }), /no client_email/],
    [JSON.stringify({ ...KEY, client_email: "not an address" }), /no client_email/],
    [JSON.stringify({ ...KEY, private_key: undefined }), /no RSA private_key/],
    [JSON.stringify({ ...KEY, private_key: "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n" }), /no RSA private_key/],
    [JSON.stringify({ ...KEY, private_key: PUBLIC_KEY }), /no RSA private_key/],
    [JSON.stringify({ ...KEY, private_key: ecKey }), /no RSA private_key/],
    [JSON.stringify({ ...KEY, private_key: { key: PRIVATE_KEY } }), /no RSA private_key/],
  ];
  for (const [text, message] of bad) {
    let thrown;
    assert.throws(() => serviceAccount(text), (err) => { thrown = err; return true; });
    assert.match(thrown.message, message);
    assert.ok(!thrown.message.includes("PRIVATE KEY"), thrown.message);
    assert.ok(!thrown.message.includes("-----"), thrown.message);
  }
});

// Google checks the signature with the key's public half, the audience against its token URL and
// the lifetime against its hour; a wrong kid or scope is refused.
test("the assertion is an RS256 token for the store's scope, signed with the service account's key", () => {
  const signed = assertion(serviceAccount(JSON.stringify(KEY)), START + 999);
  const [head, body, signature] = signed.split(".");
  assert.ok(createVerify("RSA-SHA256").update(`${head}.${body}`).verify(PUBLIC_KEY, signature, "base64url"));
  assert.ok(!createVerify("RSA-SHA256").update(`${head}.${body}x`).verify(PUBLIC_KEY, signature, "base64url"));
  assert.deepEqual(decode(head), { alg: "RS256", typ: "JWT", kid: KEY.private_key_id });
  const claims = decode(body);
  assert.deepEqual(claims, {
    iss: KEY.client_email, scope: "https://www.googleapis.com/auth/chromewebstore", aud: "https://oauth2.googleapis.com/token",
    iat: START / 1000, exp: START / 1000 + 3600,
  });
  assert.equal(claims.exp - claims.iat, 3600);
  // A key file without private_key_id gives a token without kid.
  const { private_key_id: unused, ...noId } = KEY;
  assert.ok(unused);
  assert.deepEqual(decode(assertion(serviceAccount(JSON.stringify(noId)), START).split(".")[0]), { alg: "RS256", typ: "JWT" });
});

// The publisher id is a repository variable pasted by hand: it may only ever be one path segment
// of the store's URL.
test("the publisher id is checked and URL-encoded into the item's URLs", () => {
  assert.equal(itemUrl(PUBLISHER, "fetchStatus"), STATUS_URL);
  assert.equal(itemUrl(PUBLISHER, "publish"), PUBLISH_URL);
  assert.equal(itemUrl(PUBLISHER, "cancelSubmission"), CANCEL_URL);
  assert.equal(uploadUrl(PUBLISHER), UPLOAD_URL);
  for (const id of ["12345678901234567890", "a", "A.b_c-d", "x".repeat(128)]) {
    assert.equal(itemUrl(id, "fetchStatus"), `${API}publishers/${encodeURIComponent(id)}/items/${ITEM_ID}:fetchStatus`);
    assert.equal(uploadUrl(id), `${UPLOAD_API}publishers/${encodeURIComponent(id)}/items/${ITEM_ID}:upload`);
  }
  for (const id of ["", ".", "..", "...", "../x", "a/b", "a b", "a%2Fb", "a?b", "a#b", "a:b", "x".repeat(129), undefined, 12]) {
    assert.throws(() => itemUrl(id, "fetchStatus"), /not a publisher id/, String(id));
    assert.throws(() => uploadUrl(id), /not a publisher id/, String(id));
  }
  for (const method of ["upload", "delete", "fetchStatus/../x", ""]) assert.throws(() => itemUrl(PUBLISHER, method), /not a Chrome Web Store method/);
});

// The workflow reads stdout as the word and nothing else; the details are for the log.
test("status prints exactly one word on stdout and the details on stderr", async () => {
  const h = harness({ token: [TOKEN], fetchStatus: [SUBMITTED] });
  assert.equal(await go(h, ["status", VERSION]), 0);
  assert.deepEqual(h.out, ["in-review"]);
  assert.deepEqual(h.err, ["published 0.5.0 (PUBLISHED); submitted 0.14.6 (PENDING_REVIEW)"]);
  assert.deepEqual(h.endpoints(), ["token", "fetchStatus"]);
  assert.equal(h.calls[1].url, STATUS_URL);
  assert.equal(h.calls[1].method, "GET");
  assert.equal(h.calls[1].headers.authorization, `Bearer ${ACCESS_TOKEN}`);
  for (const [given, word] of [
    [OLD, "submit"], [WAITING, "waiting"], [REJECTED_OLDER, "rejected-older"], [status({ takenDown: true }), "taken-down"],
  ]) {
    const other = harness({ token: [TOKEN], fetchStatus: [given] });
    assert.equal(await go(other, ["status", VERSION]), 0);
    assert.deepEqual(other.out, [word]);
    assert.equal(other.err.length, 1);
  }
});

// The whole way from the key to a submission, request by request, as Google documents each.
test("submit trades the key for a token, uploads the zip, publishes it and checks the store holds it", async () => {
  const h = harness({ token: [TOKEN], fetchStatus: [OLD, SUBMITTED], upload: [UPLOADED], publish: [PUBLISHED] });
  assert.equal(await go(h, ["submit", ZIP, VERSION]), 0);
  assert.deepEqual(h.out, ["PENDING_REVIEW"]);
  assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload", "publish", "fetchStatus"]);
  const [token, first, upload, publish, last] = h.calls;

  assert.equal(token.url, "https://oauth2.googleapis.com/token");
  assert.equal(token.method, "POST");
  assert.equal(token.headers["content-type"], "application/x-www-form-urlencoded");
  assert.equal(token.headers.authorization, undefined);
  const form = new URLSearchParams(token.body);
  assert.deepEqual([...form.keys()], ["grant_type", "assertion"]);
  assert.equal(form.get("grant_type"), "urn:ietf:params:oauth:grant-type:jwt-bearer");
  const [head, body, signature] = form.get("assertion").split(".");
  assert.ok(createVerify("RSA-SHA256").update(`${head}.${body}`).verify(PUBLIC_KEY, signature, "base64url"));
  assert.equal(decode(body).iat, START / 1000);

  for (const call of [first, last]) {
    assert.equal(call.url, STATUS_URL);
    assert.equal(call.method, "GET");
  }
  assert.equal(upload.url, UPLOAD_URL);
  assert.equal(upload.method, "POST");
  assert.equal(upload.headers["x-goog-upload-protocol"], "raw");
  assert.equal(upload.headers["x-goog-upload-file-name"], "extension.zip");
  assert.equal(Buffer.compare(Buffer.from(upload.body), zip({ "manifest.json": manifest(VERSION), "content.js": "run();\n", "icons/icon-128.png": "png" })), 0);
  assert.equal(publish.url, PUBLISH_URL);
  assert.equal(publish.method, "POST");
  assert.equal(publish.headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(publish.body), { publishType: "DEFAULT_PUBLISH" });
  // One token for the run, on every call to the store.
  for (const call of h.calls.slice(1)) assert.equal(call.headers.authorization, `Bearer ${ACCESS_TOKEN}`);
});

// A large package is read in the background: the upload answers IN_PROGRESS, and fetchStatus
// says when the store is done, asked every ten seconds.
test("an upload the store is still reading is asked after every ten seconds until it succeeds", async () => {
  const done = { ...OLD, lastAsyncUploadState: "SUCCEEDED" };
  const h = harness({
    token: [TOKEN], fetchStatus: [OLD, READING, done, SUBMITTED],
    upload: [{ name: "x", itemId: ITEM_ID, uploadState: "IN_PROGRESS" }], publish: [PUBLISHED],
  });
  assert.equal(await go(h, ["submit", ZIP, VERSION]), 0);
  assert.deepEqual(h.out, ["PENDING_REVIEW"]);
  assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload", "fetchStatus", "fetchStatus", "publish", "fetchStatus"]);
  assert.deepEqual(h.times("fetchStatus"), [0, 10_000, 20_000, 20_000]);
  assert.deepEqual(h.times("publish"), [20_000]);
});

// A failed upload leaves the draft broken: nothing may be submitted from it.
test("an upload that fails or that the store lost while reading it is never published", async () => {
  for (const state of ["FAILED", "NOT_FOUND"]) {
    const h = harness({
      token: [TOKEN], fetchStatus: [OLD, READING, { ...OLD, lastAsyncUploadState: state }],
      upload: [{ uploadState: "IN_PROGRESS" }], publish: [PUBLISHED],
    });
    assert.equal(await go(h, ["submit", ZIP, VERSION]), 1, state);
    assert.deepEqual(h.out, []);
    assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload", "fetchStatus", "fetchStatus"]);
    assert.equal(h.err.at(-1), `the upload failed: ${state}`);
  }
  // The upload's own answer can fail it too.
  for (const state of ["FAILED", "NOT_FOUND", "UPLOAD_STATE_UNSPECIFIED", undefined]) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [{ uploadState: state }], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", ZIP, VERSION]), 1, String(state));
    assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload"]);
    assert.match(h.err.at(-1), /^the upload failed: /);
  }
});

// The wait is bounded, and its end is asked once more rather than given up a poll early.
test("an upload still being read at the end of the wait is asked once at the deadline and never published", async () => {
  const h = harness({ token: [TOKEN], fetchStatus: [OLD, READING], upload: [{ uploadState: "IN_PROGRESS" }], publish: [PUBLISHED] });
  assert.equal(await go(h, ["submit", ZIP, VERSION, "--wait", "25"]), 1);
  assert.deepEqual(h.times("fetchStatus"), [0, 10_000, 20_000, 25_000]);
  assert.deepEqual(h.times("publish"), []);
  assert.deepEqual(h.out, []);
  assert.equal(h.err.at(-1), "the upload is still IN_PROGRESS after 25 s: not submitting it");
  // The default wait is five minutes.
  const long = harness({ token: [TOKEN], fetchStatus: [OLD, READING], upload: [{ uploadState: "IN_PROGRESS" }] });
  assert.equal(await go(long, ["submit", ZIP, VERSION]), 1);
  const times = long.times("fetchStatus");
  assert.equal(times.length, 31);
  assert.equal(times.at(-1), 300_000);
  // No wait still asks once.
  const none = harness({ token: [TOKEN], fetchStatus: [OLD, READING], upload: [{ uploadState: "IN_PROGRESS" }] });
  assert.equal(await go(none, ["submit", ZIP, VERSION, "--wait", "0"]), 1);
  assert.deepEqual(none.times("fetchStatus"), [0, 0]);
});

// Only SUCCEEDED ends the wait with a submission. lastAsyncUploadState is optional, and a loop that
// asked again only while it read IN_PROGRESS would take a missing or unspecified state for done
// and submit whatever the draft holds.
test("an upload whose state the store does not name while reading it is never published", async () => {
  for (const [later, named] of [[OLD, "in an unknown state"], [{ ...OLD, lastAsyncUploadState: "UPLOAD_STATE_UNSPECIFIED" }, "UPLOAD_STATE_UNSPECIFIED"]]) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD, READING, later], upload: [{ uploadState: "IN_PROGRESS" }], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", ZIP, VERSION, "--wait", "20"]), 1, named);
    assert.deepEqual(h.times("publish"), [], named);
    assert.deepEqual(h.times("fetchStatus"), [0, 10_000, 20_000], named);
    assert.deepEqual(h.out, [], named);
    assert.equal(h.err.at(-1), `the upload is still ${named} after 20 s: not submitting it`);
  }
});

// The last answer, at the deadline itself, counts: a store done just in time is not given up on.
test("an upload that succeeds exactly at the deadline is published", async () => {
  const h = harness({
    token: [TOKEN], fetchStatus: [OLD, READING, READING, { ...OLD, lastAsyncUploadState: "SUCCEEDED" }, SUBMITTED],
    upload: [{ uploadState: "IN_PROGRESS" }], publish: [PUBLISHED],
  });
  assert.equal(await go(h, ["submit", ZIP, VERSION, "--wait", "25"]), 0);
  assert.deepEqual(h.out, ["PENDING_REVIEW"]);
  assert.deepEqual(h.times("fetchStatus"), [0, 10_000, 20_000, 25_000, 25_000]);
  assert.deepEqual(h.times("publish"), [25_000]);
});

// The store read another version than the release's: submitting it would spend a number that
// belongs to another release.
test("an upload whose version is not the release's is never published", async () => {
  for (const crxVersion of ["0.14.5", "0.14.7", undefined, "##[error]x"]) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [{ ...UPLOADED, crxVersion }], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", ZIP, VERSION]), 1);
    assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload"]);
    assert.match(h.err.at(-1), /not submitting it$/);
  }
});

// The zip is checked before the token is asked for: a wrong package costs no request.
test("a zip that does not hold the version is refused before any request", async () => {
  const cases = [
    [zipFile("older.zip", { "manifest.json": manifest("0.14.5") }), /holds version 0\.14\.5, not 0\.14\.6/],
    [zipFile("no-version.zip", { "manifest.json": "{}" }), /holds version none, not 0\.14\.6/],
    [zipFile("no-manifest.zip", { "content.js": "run();\n" }), /holds no manifest\.json/],
    [zipFile("broken-manifest.zip", { "manifest.json": "{version" }), /manifest\.json is not JSON/],
    [join(DIR, "missing.zip"), /ENOENT/],
    [(() => { const path = join(DIR, "not-a.zip"); writeFileSync(path, "not a zip"); return path; })(), /not a zip file/],
  ];
  for (const [path, message] of cases) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [UPLOADED], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", path, VERSION]), 1, path);
    assert.equal(h.calls.length, 0, path);
    assert.deepEqual(h.out, []);
    assert.match(h.err.at(-1), message);
  }
});

// The zip parser's errors quote an entry's name, which is the zip's own text: a newline in it
// would start a line GitHub Actions reads as a workflow command, and "##[" is one anywhere in a
// line. go() holds every line to that.
test("a zip whose entry name holds a newline or a workflow command is refused in one line", async () => {
  const name = "x\n::error::injected\r\n::warning::too ##[error]legacy ###[stop-commands]t";
  const patched = (file, change) => {
    const bytes = zip({ [name]: "data", "manifest.json": manifest(VERSION) });
    // The first entry of the central directory, whose offset the end record holds.
    change(bytes, bytes.readUInt32LE(bytes.length - 22 + 16));
    const path = join(DIR, file);
    writeFileSync(path, bytes);
    return path;
  };
  const shown = "x ::error::injected ::warning::too # #[error]legacy ## #[stop-commands]t";
  const cases = [
    [patched("method-12.zip", (bytes, entry) => bytes.writeUInt16LE(12, entry + 10)), `unsupported compression 12 for ${shown}`],
    [patched("broken-entry.zip", (bytes, entry) => bytes.writeUInt32LE(bytes.length, entry + 42)), `broken zip entry ${shown}`],
  ];
  for (const [path, message] of cases) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [UPLOADED], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", path, VERSION]), 1, path);
    assert.equal(h.calls.length, 0, path);
    assert.deepEqual(h.out, []);
    assert.equal(h.err.at(-1), `${path}: ${message}`);
  }
  // The manifest's version is the zip's text too, and a short one fits the line whole.
  const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [UPLOADED], publish: [PUBLISHED] });
  const path = zipFile("command-version.zip", { "manifest.json": manifest("##[error]x") });
  assert.equal(await go(h, ["submit", path, VERSION]), 1);
  assert.equal(h.calls.length, 0);
  assert.equal(h.err.at(-1), `${path} holds version # #[error]x, not 0.14.6: not uploading it`);
});

// Only "submit" uploads: anything else is the store busy with this version, past it, or a
// state that needs a person.
test("submit refuses, before the upload, whatever the store holds that is not submit", async () => {
  const cases = [
    ["published", status({ publishedItemRevisionStatus: revision("PUBLISHED", VERSION) })],
    ["in-review", SUBMITTED],
    ["staged", status({ submittedItemRevisionStatus: revision("STAGED", VERSION) })],
    ["rejected", status({ submittedItemRevisionStatus: revision("REJECTED", VERSION) })],
    ["cancelled", status({ submittedItemRevisionStatus: revision("CANCELLED", VERSION) })],
    ["newer", status({ submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.14.7") })],
    ["taken-down", status({ takenDown: true })],
    ["waiting", WAITING],
  ];
  for (const [word, given] of cases) {
    const h = harness({ token: [TOKEN], fetchStatus: [given], upload: [UPLOADED], publish: [PUBLISHED], cancelSubmission: [{}] });
    assert.equal(await go(h, ["submit", ZIP, VERSION]), 1, word);
    assert.deepEqual(h.endpoints(), ["token", "fetchStatus"], word);
    assert.deepEqual(h.out, [], word);
    assert.ok(h.err.at(-1).startsWith(`not submitting 0.14.6: ${word}, `), h.err.at(-1));
  }
  // A waiting review is cancelled only when asked.
  const h = harness({ token: [TOKEN], fetchStatus: [WAITING], cancelSubmission: [{}] });
  assert.equal(await go(h, ["submit", ZIP, VERSION]), 1);
  assert.match(h.err.at(-1), /--cancel-review cancels that review/);
});

// An older version rejected is nothing the store waits on: submit, which the workflow runs past a
// rejection only for a new release or by hand, uploads and says what it submits past.
test("submit past an older version the store rejected uploads, and says so", async () => {
  const h = harness({ token: [TOKEN], fetchStatus: [REJECTED_OLDER, SUBMITTED], upload: [UPLOADED], publish: [PUBLISHED], cancelSubmission: [{}] });
  assert.equal(await go(h, ["submit", ZIP, VERSION]), 0);
  assert.deepEqual(h.out, ["PENDING_REVIEW"]);
  assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload", "publish", "fetchStatus"]);
  assert.deepEqual(h.err.slice(0, 2), [
    "published 0.5.0 (PUBLISHED); submitted 0.14.5 (REJECTED)",
    "the store rejected the older version in its submission; submitting 0.14.6 in its place",
  ]);
});

// A run by hand with cancel_review withdraws the older review, and the release takes its place.
test("waiting with --cancel-review cancels the older review, asks again and then uploads", async () => {
  const cancelled = status({
    publishedItemRevisionStatus: revision("PUBLISHED", "0.5.0"), submittedItemRevisionStatus: revision("CANCELLED", "0.14.5"),
  });
  const h = harness({
    token: [TOKEN], fetchStatus: [WAITING, cancelled, SUBMITTED], cancelSubmission: [{}], upload: [UPLOADED], publish: [PUBLISHED],
  });
  assert.equal(await go(h, ["submit", ZIP, VERSION, "--cancel-review"]), 0);
  assert.deepEqual(h.out, ["PENDING_REVIEW"]);
  assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "cancelSubmission", "fetchStatus", "upload", "publish", "fetchStatus"]);
  const cancel = h.calls[2];
  assert.equal(cancel.url, CANCEL_URL);
  assert.equal(cancel.method, "POST");
  assert.equal(cancel.body, undefined);
  assert.equal(cancel.headers.authorization, `Bearer ${ACCESS_TOKEN}`);
  assert.equal(h.endpoints().filter((name) => name === "token").length, 1);
});

// The Dashboard allows six cancels a day: one is spent only on a review that is in the way.
test("--cancel-review cancels nothing when nothing waits", async () => {
  const h = harness({ token: [TOKEN], fetchStatus: [OLD, SUBMITTED], cancelSubmission: [{}], upload: [UPLOADED], publish: [PUBLISHED] });
  assert.equal(await go(h, ["submit", ZIP, VERSION, "--cancel-review"]), 0);
  assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "upload", "publish", "fetchStatus"]);
  const busy = harness({ token: [TOKEN], fetchStatus: [SUBMITTED], cancelSubmission: [{}] });
  assert.equal(await go(busy, ["submit", ZIP, VERSION, "--cancel-review"]), 1);
  assert.deepEqual(busy.endpoints(), ["token", "fetchStatus"]);
});

// A cancel the store did not act on leaves the older review in the way: uploading then would
// replace a draft nobody can submit.
test("after the cancel the store must say submit, or nothing is uploaded", async () => {
  for (const still of [WAITING, status({ submittedItemRevisionStatus: revision("STAGED", "0.14.5") }), SUBMITTED]) {
    const h = harness({ token: [TOKEN], fetchStatus: [WAITING, still], cancelSubmission: [{}], upload: [UPLOADED], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", ZIP, VERSION, "--cancel-review"]), 1);
    assert.deepEqual(h.endpoints(), ["token", "fetchStatus", "cancelSubmission", "fetchStatus"]);
    assert.match(h.err.at(-1), /^not submitting 0\.14\.6: the store still says [a-z-]+ after the cancel$/);
  }
  const refused = answer(400, { error: { status: "FAILED_PRECONDITION", message: "No active submission." } });
  const failed = harness({ token: [TOKEN], fetchStatus: [WAITING], cancelSubmission: [refused] });
  assert.equal(await go(failed, ["submit", ZIP, VERSION, "--cancel-review"]), 1);
  assert.deepEqual(failed.endpoints(), ["token", "fetchStatus", "cancelSubmission"]);
  assert.equal(failed.err.at(-1), "cancelSubmission failed: HTTP 400 FAILED_PRECONDITION: No active submission.");
});

// The store's warnings are for the log; stdout stays the state alone.
test("the publish's warnings go to stderr", async () => {
  const warned = {
    ...PUBLISHED,
    warningInfo: {
      warnings: [
        { reason: "PERMISSION_JUSTIFICATION", description: "Say why\nnativeMessaging is needed." }, { reason: "OTHER" },
        // "##[stop-commands]" anywhere in a line would switch off the workflow's own notice after it.
        { reason: "##[stop-commands]t", description: "x ##[error]from the store\n::warning::too" },
      ],
    },
  };
  const h = harness({ token: [TOKEN], fetchStatus: [OLD, SUBMITTED], upload: [UPLOADED], publish: [warned] });
  assert.equal(await go(h, ["submit", ZIP, VERSION]), 0);
  assert.deepEqual(h.out, ["PENDING_REVIEW"]);
  assert.ok(h.err.includes("the store warns: PERMISSION_JUSTIFICATION: Say why nativeMessaging is needed."), h.err.join("\n"));
  assert.ok(h.err.includes("the store warns: OTHER: "), h.err.join("\n"));
  assert.ok(h.err.includes("the store warns: # #[stop-commands]t: x # #[error]from the store ::warning::too"), h.err.join("\n"));
});

// The publish answers for whatever the draft held; only the store's status says it is this version.
test("a submission that does not hold the version afterwards fails the run", async () => {
  for (const afterwards of [OLD, WAITING, status({ submittedItemRevisionStatus: revision("PENDING_REVIEW", "0.14.7") })]) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD, afterwards], upload: [UPLOADED], publish: [PUBLISHED] });
    assert.equal(await go(h, ["submit", ZIP, VERSION]), 1);
    assert.deepEqual(h.out, []);
    assert.match(h.err.at(-1), /^the store's submission does not hold 0\.14\.6 after the publish: /);
  }
  // Published at once counts as held.
  const h = harness({
    token: [TOKEN], fetchStatus: [OLD, status({ publishedItemRevisionStatus: revision("PUBLISHED", VERSION) })],
    upload: [UPLOADED], publish: [{ ...PUBLISHED, state: "PUBLISHED" }],
  });
  assert.equal(await go(h, ["submit", ZIP, VERSION]), 0);
  assert.deepEqual(h.out, ["PUBLISHED"]);
});

// A wrong, deleted or disabled key, or a bad signature, is refused by Google's token endpoint
// (invalid_grant): one line, and no request goes to the store without a token.
test("a refused token fails the run with Google's words in one line", async () => {
  for (const argv of [["status", VERSION], ["submit", ZIP, VERSION]]) {
    const h = harness({ token: [answer(400, { error: "invalid_grant", error_description: "Invalid JWT Signature." })], fetchStatus: [OLD] });
    assert.equal(await go(h, argv), 1);
    assert.deepEqual(h.endpoints(), ["token"]);
    assert.deepEqual(h.out, []);
    assert.deepEqual(h.err, ["the token request failed: HTTP 400 invalid_grant: Invalid JWT Signature."]);
  }
  const empty = harness({ token: [{ token_type: "Bearer" }], fetchStatus: [OLD] });
  assert.equal(await go(empty, ["status", VERSION]), 1);
  assert.deepEqual(empty.endpoints(), ["token"]);
  assert.match(empty.err.at(-1), /holds no access_token/);
});

// The store's errors come as Google's JSON or, from a proxy, as a page; either is one line.
test("the store's errors fail the run with one line each", async () => {
  // A valid key whose service account the Developer Dashboard does not know still gets a token;
  // the store refuses it here, with 403 PERMISSION_DENIED.
  const denied =answer(403, { error: { code: 403, message: "The caller does not have permission", status: "PERMISSION_DENIED" } });
  const h = harness({ token: [TOKEN], fetchStatus: [denied] });
  assert.equal(await go(h, ["status", VERSION]), 1);
  assert.deepEqual(h.out, []);
  assert.deepEqual(h.err, ["fetchStatus failed: HTTP 403 PERMISSION_DENIED: The caller does not have permission"]);

  const page = answer(500, `<!DOCTYPE html>\n<html><body>${"Server Error ".repeat(100)}</body></html>`);
  const broken = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [page] });
  assert.equal(await go(broken, ["submit", ZIP, VERSION]), 1);
  const line = broken.err.at(-1);
  assert.ok(line.startsWith("the upload failed: HTTP 500: <!DOCTYPE html> <html><body>Server Error"), line);
  assert.equal(line.length, "the upload failed: HTTP 500: ".length + 300);

  const unpublishable = answer(400, { error: { status: "INVALID_ARGUMENT", message: "Item is not in a publishable state." } });
  const publish = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [UPLOADED], publish: [unpublishable] });
  assert.equal(await go(publish, ["submit", ZIP, VERSION]), 1);
  assert.equal(publish.err.at(-1), "publish failed: HTTP 400 INVALID_ARGUMENT: Item is not in a publishable state.");

  const notJson = harness({ token: [TOKEN], fetchStatus: [answer(200, "<html>ok</html>")] });
  assert.equal(await go(notJson, ["status", VERSION]), 1);
  assert.equal(notJson.err.at(-1), "fetchStatus: the answer is not JSON");

  const offline = harness({ token: [TOKEN] });
  offline.deps.fetch = async () => { throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ENOTFOUND" } }); };
  assert.equal(await run(["status", VERSION], offline.deps), 1);
  assert.deepEqual(offline.err, ["the token request failed: fetch failed (ENOTFOUND)"]);

  // A request's own timeout gives up on a connection that stalls, in words a person can read.
  const stalled = harness({ token: [TOKEN] });
  stalled.deps.fetch = async () => { throw new DOMException("The operation was aborted due to timeout", "TimeoutError"); };
  assert.equal(await run(["status", VERSION], stalled.deps), 1);
  assert.deepEqual(stalled.err, ["the token request failed: no answer within 120 s"]);

  // The same timeout ends a body that stalls after the headers, and a connection can break in the
  // middle of one: either is still the request's failure, named as such.
  const failing = (error, head = "") => () => new Response(new ReadableStream({
    start(controller) {
      if (head) controller.enqueue(new TextEncoder().encode(head));
      controller.error(error);
    },
  }));
  const timedOut = () => new DOMException("The operation was aborted due to timeout", "TimeoutError");
  const cases = [
    [{ token: [failing(timedOut())] }, "the token request failed: no answer within 120 s"],
    [{ token: [TOKEN], fetchStatus: [failing(timedOut(), '{"name":')] }, "fetchStatus failed: no answer within 120 s"],
    [
      { token: [TOKEN], fetchStatus: [failing(Object.assign(new TypeError("terminated"), { cause: { code: "UND_ERR_SOCKET" } }), '{"name":')] },
      "fetchStatus failed: terminated (UND_ERR_SOCKET)",
    ],
  ];
  for (const [routes, message] of cases) {
    const h = harness(routes);
    assert.equal(await go(h, ["status", VERSION]), 1, message);
    assert.deepEqual(h.out, []);
    assert.equal(h.err.at(-1), message);
  }
});

// The log is public, and an error body is the server's text: a server that echoes the request
// must not get the signed token or the access token into it, not even cut short.
test("no line ever holds the key, the signed token or the access token, even when a server echoes them", async () => {
  const echoGrant = (call) => new Response(JSON.stringify({
    error: "invalid_grant", error_description: `bad assertion ${new URLSearchParams(call.body).get("assertion")}`,
  }), { status: 400 });
  const h = harness({ token: [echoGrant] });
  assert.equal(await go(h, ["status", VERSION]), 1);
  assert.deepEqual(h.err, ["the token request failed: HTTP 400 invalid_grant: bad assertion [redacted]"]);

  // The token starts inside the first 300 characters and ends past them: cut first, a prefix
  // would survive.
  const echoToken = (call) => new Response(JSON.stringify({
    error: { status: "UNAUTHENTICATED", message: `${"x".repeat(250)} ${call.headers.authorization} end` },
  }), { status: 401 });
  const store = harness({ token: [TOKEN], fetchStatus: [echoToken] });
  assert.equal(await go(store, ["status", VERSION]), 1);
  assert.match(store.err.at(-1), /Bearer \[redacted\] end$/);

  const text = harness({ token: [TOKEN], fetchStatus: [(call) => new Response(`denied: ${call.headers.authorization}`, { status: 401 })] });
  assert.equal(await go(text, ["status", VERSION]), 1);
  assert.equal(text.err.at(-1), "fetchStatus failed: HTTP 401: denied: Bearer [redacted]");

  // A rejected fetch quotes the header in its message ("Headers.append: ... is an invalid header
  // value" for a token holding a control character), and request() passes that on: only the
  // line's own scrub keeps the token out.
  const rejected = harness({ token: [TOKEN] });
  const answerFetch = rejected.deps.fetch;
  rejected.deps.fetch = async (url, init = {}) => {
    if (endpointOf(url) === "fetchStatus") throw new TypeError(`Headers.append: "${init.headers.Authorization}" is an invalid header value.`);
    return answerFetch(url, init);
  };
  assert.equal(await go(rejected, ["status", VERSION]), 1);
  assert.equal(rejected.err.at(-1), 'fetchStatus failed: Headers.append: "Bearer [redacted]" is an invalid header value.');

  // Every copy goes, not only the first: three, since request() and the line each scrub once.
  const copies = harness({ token: [TOKEN], fetchStatus: [(call) => new Response([1, 2, 3].map(() => call.headers.authorization).join(" and "), { status: 401 })] });
  assert.equal(await go(copies, ["status", VERSION]), 1);
  assert.equal(copies.err.at(-1), "fetchStatus failed: HTTP 401: Bearer [redacted] and Bearer [redacted] and Bearer [redacted]");

  // And a whole run, every line of it.
  const full = harness({
    token: [TOKEN], fetchStatus: [WAITING, OLD, READING, { ...OLD, lastAsyncUploadState: "SUCCEEDED" }, SUBMITTED],
    cancelSubmission: [{}], upload: [{ uploadState: "IN_PROGRESS" }], publish: [PUBLISHED],
  });
  assert.equal(await go(full, ["submit", ZIP, VERSION, "--cancel-review"]), 0);
  assert.ok(full.err.length >= 5);
});

// Nothing is sent before the arguments and the credentials are known to be good.
test("usage errors and missing or invalid credentials exit 2 before any request", async () => {
  const usage = [
    [], ["status"], ["status", "latest"], ["status", "0.14"], ["status", "v0.14.6"], ["status", "0.14.6.1"], ["status", "0.14.6-beta"],
    ["status", VERSION, "extra"], ["status", VERSION, "--cancel-review"], ["status", VERSION, "--wait", "10"],
    ["submit"], ["submit", ZIP], ["submit", VERSION], ["submit", ZIP, "latest"], ["submit", ZIP, VERSION, "extra"],
    ["submit", ZIP, VERSION, "--wait"], ["submit", ZIP, VERSION, "--wait", "-1"], ["submit", ZIP, VERSION, "--wait", "soon"],
    ["submit", ZIP, VERSION, "--wait", "1.5"], ["submit", ZIP, VERSION, "--force"], ["submit", "", VERSION],
    ["upload", ZIP, VERSION], ["publish", VERSION], ["--wait", "10"],
  ];
  for (const argv of usage) {
    const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [UPLOADED], publish: [PUBLISHED] });
    assert.equal(await go(h, argv), 2, argv.join(" "));
    assert.equal(h.calls.length, 0, argv.join(" "));
    assert.deepEqual(h.out, []);
    assert.equal(h.err.length, 1);
  }
  const json = (fields) => JSON.stringify({ ...KEY, ...fields });
  const credentials = [
    [{}, /CWS_SERVICE_ACCOUNT_JSON is not set/],
    [{ CWS_PUBLISHER_ID: PUBLISHER }, /CWS_SERVICE_ACCOUNT_JSON is not set/],
    [{ ...ENV, CWS_SERVICE_ACCOUNT_JSON: "" }, /CWS_SERVICE_ACCOUNT_JSON is not set/],
    [{ ...ENV, CWS_SERVICE_ACCOUNT_JSON: ENV.CWS_SERVICE_ACCOUNT_JSON.replace('"client_email"', 'client_email"') }, /^CWS_SERVICE_ACCOUNT_JSON is not JSON$/],
    [{ ...ENV, CWS_SERVICE_ACCOUNT_JSON: json({ type: "authorized_user" }) }, /not the JSON key of a service account/],
    [{ ...ENV, CWS_SERVICE_ACCOUNT_JSON: json({ client_email: undefined }) }, /no client_email/],
    [{ ...ENV, CWS_SERVICE_ACCOUNT_JSON: json({ private_key: undefined }) }, /no RSA private_key/],
    [{ ...ENV, CWS_SERVICE_ACCOUNT_JSON: json({ private_key: PRIVATE_KEY.slice(0, 200) }) }, /no RSA private_key/],
    [{ CWS_SERVICE_ACCOUNT_JSON: ENV.CWS_SERVICE_ACCOUNT_JSON }, /^CWS_PUBLISHER_ID is not set$/],
    [{ ...ENV, CWS_PUBLISHER_ID: "" }, /^CWS_PUBLISHER_ID is not set$/],
    [{ ...ENV, CWS_PUBLISHER_ID: "../x" }, /^CWS_PUBLISHER_ID is not a publisher id/],
    [{ ...ENV, CWS_PUBLISHER_ID: ".." }, /^CWS_PUBLISHER_ID is not a publisher id/],
    [{ ...ENV, CWS_PUBLISHER_ID: "a/b" }, /^CWS_PUBLISHER_ID is not a publisher id/],
    [{ ...ENV, CWS_PUBLISHER_ID: "x".repeat(129) }, /^CWS_PUBLISHER_ID is not a publisher id/],
  ];
  for (const [env, message] of credentials) {
    for (const argv of [["status", VERSION], ["submit", ZIP, VERSION]]) {
      const h = harness({ token: [TOKEN], fetchStatus: [OLD], upload: [UPLOADED], publish: [PUBLISHED] }, { env });
      assert.equal(await go(h, argv), 2, `${argv[0]} ${message}`);
      assert.equal(h.calls.length, 0);
      assert.deepEqual(h.out, []);
      assert.equal(h.err.length, 1);
      assert.match(h.err[0], message);
    }
  }
});

// A key file names its own token_uri; the signed token goes only to Google's, whatever it says.
test("the token is always asked of Google's token URL, never of the key file's token_uri", async () => {
  const env = { ...ENV, CWS_SERVICE_ACCOUNT_JSON: JSON.stringify({ ...KEY, token_uri: "https://token.example.com/steal" }) };
  const h = harness({ token: [TOKEN], fetchStatus: [OLD] }, { env });
  assert.equal(await go(h, ["status", VERSION]), 0);
  assert.equal(h.calls[0].url, "https://oauth2.googleapis.com/token");
  assert.ok(h.calls.every((call) => !call.url.includes("example.com")));
  const claims = decode(new URLSearchParams(h.calls[0].body).get("assertion").split(".")[1]);
  assert.equal(claims.aud, "https://oauth2.googleapis.com/token");
});

// The workflow runs the file itself, and a run by hand on the owner's Windows machine often starts
// it from a checkout behind a junction: a script that took itself for an import would do nothing
// and exit 0.
test("the script runs when started directly or through a junction, and exits 2 with no arguments", (t) => {
  const scripts = fileURLToPath(new URL("..", import.meta.url));
  const env = { ...process.env };
  delete env.CWS_SERVICE_ACCOUNT_JSON;
  delete env.CWS_PUBLISHER_ID;
  const targets = [["direct", join(scripts, "cws.mjs")]];
  const link = join(DIR, "scripts");
  try {
    symlinkSync(scripts, link, "junction");
    targets.push(["linked", join(link, "cws.mjs")]);
  } catch (err) {
    t.diagnostic(`no junction or symlink here: ${err.code}`);
  }
  try {
    for (const [name, script] of targets) {
      const ran = spawnSync(process.execPath, [script], { encoding: "utf8", env });
      assert.equal(ran.status, 2, `${name}: ${ran.stderr}`);
      assert.equal(ran.stdout, "", name);
      assert.match(ran.stderr, /^usage: cws\.mjs status <version> \| submit <zip> <version>/, name);
    }
  } finally {
    // The link goes by itself, before the temporary folder is removed around it.
    if (targets.length > 1) unlinkSync(link);
  }
});
