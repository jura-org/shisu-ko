import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { deflateRawSync } from "node:zlib";
import {
  ADDON_ID, LISTING_SUFFIX, NO_FILE_EXIT, PENDING_EXIT, buildDifferences, downloadTarget, folderEntries, jwt, listingManifest,
  listingVersion, run, stateOf, versionUrl, zipEntries,
} from "../amo-xpi.mjs";

const MANIFEST = readFileSync(new URL("../../addon/manifest.json", import.meta.url), "utf8");

const ENV = { WEB_EXT_API_KEY: "user:1:2", WEB_EXT_API_SECRET: "s3cret" };
const FILE_URL = "https://addons.mozilla.org/firefox/downloads/file/1/shisu_ko-0.14.0.xpi";

function amo(states, { body = "signed bytes" } = {}) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, auth: init.headers.Authorization });
    if (url === FILE_URL) return new Response(body);
    const state = states.length > 1 ? states.shift() : states[0];
    if (state === 404) return new Response("{}", { status: 404 });
    if (typeof state === "number") return new Response("nope", { status: state });
    return Response.json({ file: { status: state, url: FILE_URL } });
  };
  return { fetch, calls };
}

function harness(states, extra = {}) {
  const out = [], err = [];
  let clock = 0;
  const { fetch, calls } = amo(states, extra);
  const deps = {
    env: ENV, fetch, now: () => clock, sleep: async (ms) => { clock += ms; },
    log: (line) => out.push(line), warn: (line) => err.push(line),
  };
  return { deps, calls, out, err };
}

test("the token is an HS256 JWT signed with the secret and short-lived", () => {
  const token = jwt("user:1:2", "s3cret", 1_000_000);
  const [head, body, sig] = token.split(".");
  assert.equal(sig, createHmac("sha256", "s3cret").update(`${head}.${body}`).digest("base64url"));
  assert.deepEqual(JSON.parse(Buffer.from(head, "base64url")), { alg: "HS256", typ: "JWT" });
  const claims = JSON.parse(Buffer.from(body, "base64url"));
  assert.equal(claims.iss, "user:1:2");
  assert.equal(claims.iat, 1000);
  assert.equal(claims.exp - claims.iat, 60);
  assert.notEqual(JSON.parse(Buffer.from(jwt("user:1:2", "s3cret").split(".")[1], "base64url")).jti, claims.jti);
});

test("the version URL takes a version number or a tag and nothing else", () => {
  const base = `https://addons.mozilla.org/api/v5/addons/addon/${encodeURIComponent(ADDON_ID)}/versions/`;
  assert.equal(versionUrl("0.14.0"), `${base}0.14.0/`);
  assert.equal(versionUrl("v0.14.0"), `${base}0.14.0/`);
  for (const bad of ["", "latest", "0.14.0/../x", "1", "0.14.0-beta"]) assert.throws(() => versionUrl(bad));
});

test("states", () => {
  assert.equal(stateOf(null), "missing");
  assert.equal(stateOf({ file: { status: "unreviewed" } }), "pending");
  assert.equal(stateOf({}), "pending");
  assert.equal(stateOf({ file: { status: "public" } }), "public");
  assert.equal(stateOf({ file: { status: "disabled" } }), "disabled");
});

test("the signed file is only downloaded from addons.mozilla.org", () => {
  assert.deepEqual(downloadTarget(FILE_URL), { url: FILE_URL, name: "shisu_ko-0.14.0.xpi" });
  assert.throws(() => downloadTarget("http://addons.mozilla.org/x/a.xpi"));
  assert.throws(() => downloadTarget("https://addons.mozilla.org.evil.example/a.xpi"));
  assert.throws(() => downloadTarget("https://addons.mozilla.org/firefox/downloads/file/1/..%2Fa.xpi"));
  assert.throws(() => downloadTarget("https://addons.mozilla.org/firefox/downloads/file/1/"));
});

test("status prints the state", async () => {
  for (const [state, printed] of [[404, "missing"], ["unreviewed", "pending"], ["public", "public"]]) {
    const h = harness([state]);
    assert.equal(await run(["status", "0.14.0"], h.deps), 0);
    assert.deepEqual(h.out, [printed]);
    assert.match(h.calls[0].auth, /^JWT [\w-]+\.[\w-]+\.[\w-]+$/);
  }
});

test("fetch downloads an approved version, after waiting for the review", async () => {
  const dir = mkdtempSync(join(tmpdir(), "amo-xpi-"));
  try {
    const h = harness(["unreviewed", "unreviewed", "public"]);
    assert.equal(await run(["fetch", "0.14.0", "--out", dir, "--wait", "600"], h.deps), 0);
    const path = join(dir, "shisu_ko-0.14.0.xpi");
    assert.deepEqual(h.out, [path]);
    assert.equal(readFileSync(path, "utf8"), "signed bytes");
    assert.equal(h.calls.at(-1).url, FILE_URL);
    assert.ok(h.calls.at(-1).auth.startsWith("JWT "));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("fetch gives up with the pending code when the wait runs out", async () => {
  const h = harness(["unreviewed"]);
  assert.equal(await run(["fetch", "0.14.0", "--wait", "90"], h.deps), PENDING_EXIT);
  assert.equal(h.calls.length, 4); // at 0, 30, 60 and 90 seconds
  const once = harness(["unreviewed"]);
  assert.equal(await run(["fetch", "0.14.0"], once.deps), PENDING_EXIT);
  assert.equal(once.calls.length, 1);
});

test("fetch fails for a missing or rejected version, with its own code, and on AMO errors", async () => {
  // A version AMO has no file for is a state the workflows name (the upload failed, or AMO refused
  // it); an error answer is not, and keeps the plain failure.
  for (const [state, code] of [[404, NO_FILE_EXIT], ["disabled", NO_FILE_EXIT], [500, 1]]) {
    const h = harness([state]);
    assert.equal(await run(["fetch", "0.14.0", "--wait", "60"], h.deps), code);
    assert.equal(h.out.length, 0);
  }
  assert.equal(NO_FILE_EXIT, 4);
  assert.notEqual(NO_FILE_EXIT, PENDING_EXIT);
});

test("bad arguments and a missing key are usage errors", async () => {
  const h = harness(["public"]);
  assert.equal(await run(["fetch", "latest"], h.deps), 2);
  assert.equal(await run(["upload", "0.14.0"], h.deps), 2);
  assert.equal(await run(["fetch", "0.14.0", "--wait", "-1"], h.deps), 2);
  assert.equal(await run(["status", "0.14.0"], { ...h.deps, env: {} }), 2);
  assert.equal(h.calls.length, 0);
});

test("the listed build of a release is its number plus .1", () => {
  assert.equal(LISTING_SUFFIX, ".1");
  assert.equal(listingVersion("0.15.0"), "0.15.0.1");
  assert.equal(listingVersion("1.0.12"), "1.0.12.1");
  // A number that already has a fourth part is a listed build: stamping it again would give a
  // fifth, which Firefox refuses; a tag, a short number or a suffix is no release version.
  for (const bad of ["0.15.0.1", "v0.15.0", "0.15", "", "0.15.0-beta", "0.15.0 "]) {
    assert.throws(() => listingVersion(bad), /not a release version/);
  }
});

test("the listed manifest differs from the release's in the version alone", () => {
  const { version: released } = JSON.parse(MANIFEST);
  const { text, version } = listingManifest(MANIFEST);
  assert.equal(version, `${released}.1`);
  assert.deepEqual(JSON.parse(text), { ...JSON.parse(MANIFEST), version });
  const before = MANIFEST.split("\n"), after = text.split("\n");
  assert.equal(after.length, before.length);
  const changed = before.flatMap((line, i) => (line === after[i] ? [] : [[line, after[i]]]));
  assert.deepEqual(changed, [[`  "version": "${released}",`, `  "version": "${version}",`]]);
});

test("the listed manifest refuses a file it cannot change in one place", () => {
  const nested = '{"version": "0.15.0", "x": {"version": "0.15.0"}}';
  assert.throws(() => listingManifest(nested), /expected one "version" entry/);
  assert.throws(() => listingManifest('{"version": "0.15.0.1"}'), /not a release version/);
  assert.throws(() => listingManifest('{"name": "x"}'), /not a release version/);
  assert.throws(() => listingManifest("not json"));
  // Another key ending in "version" is left alone.
  const other = '{\n  "strict_min_version": "140.0.0",\n  "version": "0.15.0"\n}\n';
  assert.equal(listingManifest(other).text, '{\n  "strict_min_version": "140.0.0",\n  "version": "0.15.0.1"\n}\n');
});

test("listing rewrites the manifest of a build folder and needs no API key", async () => {
  const dir = mkdtempSync(join(tmpdir(), "amo-listing-"));
  const manifest = join(dir, "manifest.json");
  try {
    writeFileSync(manifest, '{\n  "name": "x",\n  "version": "0.15.0"\n}\n');
    const h = harness(["public"]);
    assert.equal(await run(["listing", dir], { ...h.deps, env: {} }), 0);
    assert.deepEqual(h.out, ["0.15.0.1"]);
    assert.equal(readFileSync(manifest, "utf8"), '{\n  "name": "x",\n  "version": "0.15.0.1"\n}\n');
    // A second run would stamp a listed build again: refused, and the file stays as it is.
    assert.equal(await run(["listing", dir], h.deps), 1);
    assert.match(h.err.at(-1), /not a release version/);
    assert.equal(readFileSync(manifest, "utf8"), '{\n  "name": "x",\n  "version": "0.15.0.1"\n}\n');
    assert.equal(await run(["listing", join(dir, "nowhere")], h.deps), 1);
    assert.equal(await run(["listing"], h.deps), 2);
    assert.equal(await run(["listing", dir, "--wait", "1"], h.deps), 2);
    assert.equal(h.calls.length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// publish-addon.cmd runs the script on Windows, where a checkout is often reached through a
// junction: the module's URL is the real path, argv[1] the one it was started by. A script that
// took itself for an import there would stamp nothing and exit 0.
test("the script runs when started directly and through a junction or symlink", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "amo-main-"));
  try {
    const scripts = fileURLToPath(new URL("..", import.meta.url));
    const link = join(dir, "scripts");
    try {
      symlinkSync(scripts, link, "junction");
    } catch (err) {
      return t.skip(`no junction or symlink here: ${err.code}`);
    }
    for (const [name, script] of [["direct", join(scripts, "amo-xpi.mjs")], ["linked", join(link, "amo-xpi.mjs")]]) {
      const build = join(dir, name);
      mkdirSync(build);
      writeFileSync(join(build, "manifest.json"), '{"version": "0.15.0"}');
      const ran = spawnSync(process.execPath, [script, "listing", build], { encoding: "utf8" });
      assert.equal(ran.status, 0, `${name}: ${ran.stderr}`);
      assert.equal(ran.stdout.trim(), "0.15.0.1", name);
      assert.equal(readFileSync(join(build, "manifest.json"), "utf8"), '{"version": "0.15.0.1"}', name);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// A zip as web-ext and AMO write one, stored or deflated; a name ending in "/" is a folder.
function zip(files, { deflate = false } = {}) {
  const locals = [], central = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const data = Buffer.from(text);
    const body = deflate ? deflateRawSync(data) : data;
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(deflate ? 8 : 0, 8);
    local.writeUInt32LE(body.length, 18); local.writeUInt32LE(data.length, 22); local.writeUInt16LE(nameBytes.length, 26);
    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0); entry.writeUInt16LE(deflate ? 8 : 0, 10);
    entry.writeUInt32LE(body.length, 20); entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28); entry.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, body);
    central.push(entry, nameBytes);
    offset += 30 + nameBytes.length + body.length;
  }
  const dir = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(Object.keys(files).length, 8); end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(dir.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, dir, end]);
}

const BUILD = { "manifest.json": '{\n  "version": "0.15.0"\n}\n', "content.js": "run();\n", "icons/icon.svg": "<svg/>" };
const SIGNED = {
  "META-INF/cose.manifest": "x", "META-INF/mozilla.rsa": "y",
  "manifest.json": '{"version":"0.15.0"}', "content.js": "run();\n", "icons/": "", "icons/icon.svg": "<svg/>",
};
const text = (files) => Object.fromEntries([...files].map(([name, bytes]) => [name, bytes.toString("utf8")]));

test("zipEntries reads stored and deflated entries alike and leaves folders out", () => {
  for (const deflate of [false, true]) {
    assert.deepEqual(text(zipEntries(zip(SIGNED, { deflate }))), Object.fromEntries(Object.entries(SIGNED).filter(([n]) => !n.endsWith("/"))));
  }
  assert.throws(() => zipEntries(Buffer.from("not a zip")), /not a zip file/);
  const broken = zip(BUILD);
  broken.writeUInt32LE(0, broken.length - 6); // the directory's offset now points at a local header
  assert.throws(() => zipEntries(broken), /broken zip directory/);
});

test("buildDifferences: AMO's signature aside, the signed file is the build or it is not", () => {
  const build = new Map(Object.entries(BUILD).map(([n, t]) => [n, Buffer.from(t)]));
  // META-INF is AMO's; manifest.json is compared as JSON, so its layout does not matter.
  assert.deepEqual(buildDifferences(zipEntries(zip(SIGNED, { deflate: true })), build), []);
  const changed = { ...SIGNED, "content.js": "run(1);\n", "manifest.json": '{"version":"0.15.1"}' };
  assert.deepEqual(buildDifferences(zipEntries(zip(changed)), build), ["manifest.json: differs", "content.js: differs"]);
  const { "icons/icon.svg": gone, ...fewer } = SIGNED;
  assert.ok(gone);
  assert.deepEqual(buildDifferences(zipEntries(zip({ ...fewer, "extra.js": "" })), build), ["icons/icon.svg: not in the signed file", "extra.js: not in the build"]);
  assert.deepEqual(buildDifferences(zipEntries(zip({ ...SIGNED, "manifest.json": "{broken" })), build), ["manifest.json: differs"]);
});

test("same-build compares a signed file with a build folder or zip", async () => {
  const dir = mkdtempSync(join(tmpdir(), "amo-same-"));
  try {
    const folder = join(dir, "firefox");
    mkdirSync(join(folder, "icons"), { recursive: true });
    for (const [name, content] of Object.entries(BUILD)) writeFileSync(join(folder, name), content);
    writeFileSync(join(folder, ".amo-upload-uuid"), "{}"); // web-ext sign leaves it there; not packed
    writeFileSync(join(dir, "signed.xpi"), zip(SIGNED, { deflate: true }));
    writeFileSync(join(dir, "other.xpi"), zip({ ...SIGNED, "content.js": "other();\n" }, { deflate: true }));
    writeFileSync(join(dir, "release.zip"), zip(BUILD));
    assert.deepEqual([...(await folderEntries(folder)).keys()].sort(), ["content.js", "icons/icon.svg", "manifest.json"]);
    const h = harness(["public"]);
    // No API key needed, and AMO is never asked.
    assert.equal(await run(["same-build", join(dir, "signed.xpi"), folder], { ...h.deps, env: {} }), 0);
    assert.equal(await run(["same-build", join(dir, "signed.xpi"), join(dir, "release.zip")], h.deps), 0);
    assert.equal(await run(["same-build", join(dir, "other.xpi"), folder], h.deps), 1);
    assert.ok(h.err.includes("  content.js: differs"), h.err.join("\n"));
    assert.equal(await run(["same-build", join(dir, "missing.xpi"), folder], h.deps), 1);
    assert.equal(await run(["same-build", join(dir, "signed.xpi")], h.deps), 2);
    assert.equal(await run(["same-build", join(dir, "signed.xpi"), folder, "x"], h.deps), 2);
    assert.equal(h.calls.length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
