# Installing, updating and removing Shisu-ko

The [README](../README.md#install) has the short version. This page has the rest: what setup
does, the **Start server** button, updates, removing Shisu-ko, Nix and Docker, other ways to
install the extension, and where Shisu-ko keeps its files.

- [Requirements](#requirements)
- [What setup does](#what-setup-does)
- [The Start server button](#the-start-server-button)
- [Updating](#updating)
- [Removing Shisu-ko](#removing-shisu-ko)
- [Nix](#nix)
- [Docker](#docker)
- [Installing the extension another way](#installing-the-extension-another-way)
- [Where Shisu-ko keeps its files](#where-shisu-ko-keeps-its-files)

## Requirements

- Firefox 140 or newer, or Chrome 120 or newer.
- For the native server: Python 3.10 or newer (on Windows `setup.cmd` takes the `py` launcher
  that the python.org installer registers, or a `python` that really runs), plus Node.js 20+
  or Deno (yt-dlp needs a JavaScript runtime for YouTube). On Nix the flake provides all of this.
- For the Docker server: Docker, and for the GPU variant the NVIDIA Container Toolkit (Docker
  Desktop on Windows has it built in); without an NVIDIA GPU, the CPU variant (see
  [Docker](#docker)). The image already contains Deno.
- A GPU for large-v3: an NVIDIA card with about 4 GB of free VRAM (with less the server switches
  to int8 weights by itself). Any Apple Silicon Mac decodes on its own GPU through MLX, and gets
  large-v3-turbo instead: on a Mac the GPU's memory is the browser's memory, and large-v3 does not
  fit beside the video you are watching. Without either GPU, pick the small model at setup and run
  on the CPU.
- Or, experimental and for the native server on Windows and Linux, an AMD graphics card:
  Radeon RX 7000 or RX 9000, Radeon PRO W7000/W9000 or AI PRO R9700, or the graphics of a Ryzen
  AI 300 (890M, 880M) or Ryzen AI Max (8060S, 8050S, 8040S); on Windows with Adrenalin 26.2.2 or
  newer, on Linux also an RX 6800/6900, with ROCm 7.2 installed. Setup offers it when it finds
  one and no NVIDIA GPU beside it; see [AMD graphics cards](amd.md).
- Optional: [Yomitan](https://yomitan.wiki/) for lookups, [Anki](https://apps.ankiweb.net/)
  with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on for mining.

## What setup does

Setup creates an isolated Python environment in `~/.shisu-ko/venv` and installs faster-whisper,
yt-dlp, the CUDA runtime libraries and, on Apple Silicon, mlx-whisper. Everything it installs
goes into `~/.shisu-ko`; outside it, setup only registers the launcher behind the popup's
**Start server** button with your browsers (see [The Start server button](#the-start-server-button)).
It then asks which model the server should use. `1` is large-v3 (best quality, about 3 GB, wants
a GPU with 4 GB or more free), or large-v3-turbo on an Apple Silicon Mac (about 1.6 GB, the one
that keeps up beside a browser). Without a suitable graphics card take `2`, Whisper small (about
500 MB, fine on a CPU, less accurate). `3` is kitsune-0.6b and `4` kitsune-0.1b (Japanese only;
setup first installs PyTorch for them, see
[Kitsune-Transcribe models](server.md#kitsune-transcribe-models)). It downloads the choice into
`~/.shisu-ko/models` with a progress bar and remembers it in `~/.shisu-ko/config.json`. Where Firefox is installed it also asks whether the server should send
Firefox's YouTube cookies with every download: YouTube refuses some downloads ("Sign in to confirm
you're not a bot") until they carry a signed-in browser's cookies. Say yes if you are signed in to
YouTube in Firefox; the answer goes to the same file (see [YouTube sign-in](server.md#youtube-sign-in)).
Last, where it finds an AMD graphics card and no NVIDIA GPU beside it, it offers the experimental
AMD engine and asks before it downloads anything; for an AMD card the engine cannot use, it says
why and asks nothing (see [AMD graphics cards](amd.md)). That step never makes the setup fail. When it says that everything is ready, close its window and start
`run.cmd` / `run.sh`. The choice is kept even when the download fails or is stopped with Ctrl+C:
the server then downloads the chosen model itself, without the progress bar, when a video first
needs it. The server is ready when it prints `Listening on http://127.0.0.1:8790`. Keep the window open while you watch; it
restarts itself if it ever crashes.

## The Start server button

From then on the toolbar popup can start it for you: while the server is offline, the status
line in the popup's header shows a **Start server** button. The first click asks the browser for
permission, Firefox to "exchange messages with programs other than Firefox", Chrome to
"communicate with cooperating native applications"; allow it, and the button launches
`server\run.cmd` in a window of its own (Windows) or `server/run.sh` in the background with its
output in `~/.shisu-ko/server.log` (Linux/macOS), then waits for the server to answer.
Out of the box the button passes no options: the server starts with its defaults (the model
chosen at setup, else large-v3, or large-v3-turbo on an
Apple GPU; the GPU when there is one; the YouTube cookies of the browser chosen at setup, if
any, see [YouTube sign-in](server.md#youtube-sign-in)), exactly as a bare `run.cmd` / `run.sh` start
would. Options you want on every start go in the popup's **Start server options** field, for
example `--device cpu` or `--lyrics off --beam-size 1`: the button passes them to `run.cmd` /
`run.sh` the next time it starts the server (one already running keeps its own), and the line
under the field says what it will pass. Only options from a fixed list are taken: the **?**
beside the field lists them, each with the values it takes, its default and what it does (more in
[Server options](server.md#server-options)); the line names any word it refuses, and so does the
status line when you click. The popup's model field switches the model from the first video on;
the few options the list leaves out (`--port`, `--js-runtime`, `--cookies`, ...) still need a
start by hand. In Chrome the
button is there for the [Chrome Web Store](https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm)
install only (see [Install the extension](../README.md#2-install-the-extension)): the launcher's manifest
has to name the extension's id, and an unpacked build (`dist/chrome`, the release zip) gets an
id of its own from the folder it was loaded from, so there the server is started by hand.
Docker and Nix users start the server as before.

Setup registers that launcher with Firefox and Chrome (on Linux with Chromium too), and so does
every `run.cmd` / `run.sh` start. An existing install therefore gets the button after one or two
starts by hand: the start that updates Shisu-ko to a version with the button still runs the old
launcher, so it is the start after the update that registers. An install that already has the
button gets Chrome's registration with the update itself on Linux and macOS, where `run.sh`
registers after updating (the popup's **Update** included), and with the next `run.cmd` start on
Windows, where `run.cmd` registers before it updates. Running `setup.cmd` / `setup.sh` once is
the sure way, and `run.cmd --check` says for which browsers the launcher is registered.

## Updating

Every start first looks for a newer Shisu-ko: a git clone is fast-forwarded to the branch it
tracks, a folder downloaded as a zip is replaced with the newest release, changed Python
requirements are installed, and a changed extension is pointed out (reload it in Firefox or
install the new `.xpi`; a Chrome Web Store install is updated by the store). A git clone is
never updated over local changes; a folder from the zip gets every file of the new release
written over it. Being offline just starts the current version. `run.cmd --no-update` (or
`SHISUKO_NO_UPDATE=1`) skips the check.

**Updates.** A server that keeps running would never see a new release, so it looks for one
itself: once a day, soon after it starts and then every 24 hours, it asks GitHub for the newest
release (an anonymous request; a restart within the day asks nothing, and a failed look is tried
again after 15 minutes, then an hour, then six). `--update-check off` or `SHISUKO_NO_UPDATE_CHECK=1`
turns that off. The extension reads the answer from the server. When the server you are running is
older, the toolbar icon gets a badge and the popup shows a banner with an **Update** button
(**Not now** hides it until Firefox restarts). A system notification says so too, once per
browser session, from the browser's start, and only when the server is already running then and
can update itself; clicking it updates the server. The line at the bottom of the popup's *Anki, clips
and server* drawer says what the server found and when. Update, or a click on the notification,
makes the server exit and its
launcher take over: `run.cmd` / `run.sh` run `update.py` (a git clone is fast-forwarded, a zip
install is replaced with the newest release) and start the server again, while the popup's
status line reads "Updating server" until the new version answers, which can take up to two
minutes (a server started with `--model-idle-minutes 0`, or on the AMD engine, also loads its
model again before it answers). A server that `run.cmd` / `run.sh` did not
start (Docker, Nix, `python server.py` by hand, or a `run.sh` from before 0.9.0 that has not been
restarted since it updated itself; `run.cmd` picks its new loop up by itself) or that was started
with `--no-update` cannot update itself; the banner then says the server was not started by
`run.cmd` / `run.sh`, whichever of those the cause is (the server only reports that it cannot),
and asks for a restart by hand. A restart of `run.cmd` / `run.sh` updates as before, unless it
was started with `--no-update`. In Docker, update the Shisu-ko folder (`git pull`, or the new
release's source zip unpacked over it), then run `docker\up.cmd --build` or
`docker compose up -d --build`. With Nix, `nix run --refresh github:Multysquid/shisu-ko` runs
the newest version (in a checkout, `git pull` first). Docker and Nix servers still check, so
they can tell you that a newer image or extension is out. The extension itself is never installed
by the add-on: Firefox updates it from the listing on addons.mozilla.org, and the banner links to
the release page when only the extension is behind, once the release carries the signed `.xpi`
(until then it says the signed `.xpi` is on its way). Chrome updates an install from the Chrome
Web Store by itself: every release goes to the store by itself, and Chrome gets it once it has
passed the store's review, which can be days after the GitHub release; until then the banner says
so and offers no release page, since an unpacked build from there would be a second copy of the
extension beside the store's (an unpacked build of your own keeps the link). While the server is
offline the popup goes by the last check a server reported.

The popup's update banner is described under [Settings](guide.md#settings).

## Removing Shisu-ko

Setup and every `run.cmd` / `run.sh` start register the launcher behind the popup's
**Start server** button with your browsers. To take that registration away again, for example
before deleting the checkout or if no browser should be able to start anything, run
`%USERPROFILE%\.shisu-ko\venv\Scripts\python server\native_host.py --unregister` in the
Shisu-ko folder in the Windows Command Prompt (in PowerShell, start it with
`~\.shisu-ko\venv\Scripts\python`; on Linux/macOS,
`~/.shisu-ko/venv/bin/python server/native_host.py --unregister`; any Python 3 works, the host
is standard library only). On Windows it removes
`shisuko.json` and `shisuko-chrome.json` from `~/.shisu-ko/native-messaging` and the registry
keys `HKCU\Software\Mozilla\NativeMessagingHosts\shisuko` and
`HKCU\Software\Google\Chrome\NativeMessagingHosts\shisuko`; on Linux `shisuko.json` from
`~/.mozilla/native-messaging-hosts`, `~/.config/google-chrome/NativeMessagingHosts` and
`~/.config/chromium/NativeMessagingHosts` (`$CHROME_CONFIG_HOME`, else `$XDG_CONFIG_HOME`, in
place of `~/.config` when it is set, as Chrome itself looks); on macOS `shisuko.json` from
`~/Library/Application Support/Mozilla/NativeMessagingHosts` and
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts`. Delete those by hand if the
checkout is already gone. `--status` shows the current state.

With Docker, run `docker\down.cmd` (or `docker compose down`) before you delete the folder:
otherwise the container comes back with every Docker start and holds port 8790. Then remove its
image, `shisu-ko-server`, and the `DATA_DIR` folder.

Then delete the Shisu-ko folder and `~/.shisu-ko` (or the folder `SHISUKO_HOME` names), and remove
the extension from your browser.

## Nix

**Nix / NixOS:** `nix run github:Multysquid/shisu-ko` (or `nix run .` in a checkout) starts the
server with CUDA support; `nix run github:Multysquid/shisu-ko#check` (`nix run .#check` in a
checkout) prints diagnostics; `nix develop` in a checkout opens a shell with Python, web-ext,
Node and Deno for development. The flake builds for x86-64 Linux only.

The flake takes CTranslate2 with CUDA from the `cache.nixos-cuda.org` binary cache. Add the cache
and its key to your Nix settings first, or expect a long build. Put these two lines into
`/etc/nix/nix.conf`, then restart the Nix daemon if your install has one
(`sudo systemctl restart nix-daemon`):

```
extra-substituters = https://cache.nixos-cuda.org
extra-trusted-public-keys = cache.nixos-cuda.org:74DUi4Ye579gUqzH4ziL9IyiJBlDpMRn9MBN8oNan9M=
```

On NixOS, put the same two values into `nix.settings.substituters` and
`nix.settings.trusted-public-keys`.

To keep the server running in the background:
`systemd-run --user --unit=shisu-ko nix run /path/to/shisu-ko`. Options go after `--`
(`nix run github:Multysquid/shisu-ko -- --device cpu`, or `nix run . -- --device cpu` in a
checkout). There is no setup to ask about YouTube's sign-in, so if YouTube asks for one, see
[YouTube sign-in](server.md#youtube-sign-in).

## Docker

**Docker:** copy `.env.example` to `.env`, set `DATA_DIR` to where models and caches should
live, then run `docker\up.cmd` (Windows) or `docker compose up -d`.

```
docker\up.cmd      build (first time) and start in the background; restarts after crashes
docker\logs.cmd    follow the server log
docker\down.cmd    stop
```

On Linux/macOS use `docker compose up -d`, `docker compose logs -f`, `docker compose down`.
Settings live in `.env` (copy `.env.example`): `DATA_DIR` is the host folder for models and
caches and `WHISPER_MODEL` the default model; the popup can switch the container to another
model, which is downloaded into `DATA_DIR`. Point `DATA_DIR` at the native setup's `~/.shisu-ko`
to share the downloaded models.

Without an NVIDIA GPU (on a Mac, for example), use the CPU variant, `compose.cpu.yaml`: set
`WHISPER_MODEL=small` in `.env` (large-v3, which `.env.example` names, is far too slow on a CPU),
then run `docker compose -f compose.cpu.yaml up -d`. `docker\up.cmd` starts only the GPU
variant.

The image is `python:3.12-slim` plus the pip-installed CUDA libraries, PyTorch's CUDA build for
the Kitsune models (about 3 GB of it) and Deno, about 5 GB in all.
`docker compose build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cpu` builds a
smaller image with PyTorch's CPU build. The GPU driver comes from the host through the NVIDIA
Container Toolkit. Two ways to get that on Windows:

- **Docker Desktop** with the WSL 2 backend (GPU support is built in).
- **Docker Engine inside WSL 2**, without Docker Desktop: run once
  `wsl -d Ubuntu -u root -- bash -c "tr -d '\r' < /mnt/c/<path to this folder>/docker/install-docker-wsl.sh | bash -s -- <your WSL user>"`.
  The `docker\*.cmd` wrappers detect which of the two is installed. Do not run both.
  WSL stops a distro a few seconds after its last session closes, which would take Docker and the
  server down with it, so `up.cmd` opens a minimized "Shisu-ko WSL keep-alive" window; leave it
  open while you watch, and close it (or run `down.cmd`) when you are done.

The extension does not change between native and Docker; both listen on `127.0.0.1:8790`.

The server's mining, its Anki sync and the word colours' deck are off in the container: 127.0.0.1
there is the container itself, not the computer running Anki. To turn them on, add `--anki-url` with an address the
container reaches Anki at to the `command:` line, for example
`http://host.docker.internal:8765` (Docker Desktop; on Linux add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service), and set AnkiConnect's
`webBindAddress` to an address that answers there (`0.0.0.0` answers every network; keep it on a
machine you trust, since AnkiConnect has no password unless you give it an `apiKey`, which the
server does not send).

## Installing the extension another way

### Firefox

Temporary install (until Firefox restarts):

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…** and choose `addon/manifest.json`.
3. Firefox asks for access to youtube.com the first time you open the popup; click **Allow on
   YouTube** (or right-click the toolbar icon > Always Allow on www.youtube.com).

Permanent install: every [GitHub release](https://github.com/Multysquid/shisu-ko/releases/latest)
carries `shisu_ko-<version>.xpi`, signed by addons.mozilla.org for self-distribution, usually
within minutes of the release; open it in Firefox to install. When addons.mozilla.org holds a
version for a human review, the release carries `shisu-ko-<version>-firefox-unsigned.xpi` until
the signed file replaces it: regular Firefox refuses an unsigned add-on, Developer Edition,
Nightly and ESR take it with `xpinstall.signatures.required` set to `false`. Or install
[Shisu-ko on addons.mozilla.org](https://addons.mozilla.org/firefox/addon/shisu-ko/), which gets
selected releases after AMO's review (that can take days), so it may be a release or two behind
GitHub. Both are the same add-on, and Firefox updates either from the listing: a GitHub install
moves on to the first listed version newer than its own (a release published there is listed as
its number plus `.1`, the same code). Regular Firefox only keeps signed
add-ons; Firefox Developer Edition, Nightly and ESR can instead load the unsigned zip with
`xpinstall.signatures.required` set to `false` in `about:config`. The popup says when a newer
release is out (see [Updates](#updating)). Since 0.9.0 the extension needs
"Display notifications to you", for that news at Firefox start; opening a newer `.xpi` over an
older version from before 0.9.0 lists it in the install prompt.

### Chrome

On Chrome, install [Shisu-ko from the Chrome Web Store](https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm).
Every release goes to the store by itself, and Chrome updates the install by itself once the store
has reviewed it, so it can trail the GitHub release by days; the popup then says the store will
update it instead of pointing you at the release page.

Chrome development uses the same source. Run `npm ci` and `npm run build:chrome`, then open
`chrome://extensions`, enable Developer mode, and choose **Load unpacked** on `dist/chrome`.
After edits, run `npm run watch`; reload the extension on that page and reload the YouTube tab.
The Firefox source remains directly loadable from `addon/manifest.json`. `npm run build` writes
both unpacked trees and `dist/shisu-ko-<version>-{firefox,chrome}.zip`. Instead of the store
install, you can also download `shisu-ko-<version>-chrome.zip` from the [Chrome release](https://github.com/Multysquid/shisu-ko/releases/latest),
unzip it, and choose **Load unpacked** on the extracted folder. This ZIP is unsigned and is not a
Chrome Web Store install; it has no automatic updates, and it gets an id of its own, so beside a
store install it is a second copy of the extension (keep one of the two). Its popup keeps the
release page link for a newer version. Keep the extracted folder and reload the extension from
`chrome://extensions` after updates. Chrome shortcuts are under
`chrome://extensions/shortcuts`.

## Where Shisu-ko keeps its files

Data lives in `~/.shisu-ko` (override with `SHISUKO_HOME`): `venv/`, `models/`, `cache/`,
`config.json` (the model and the browser for YouTube's sign-in chosen at setup, and
`"engine": "rocm"` once the AMD engine's test has passed), the AMD engine's `rocm/` with its
crash guard `rocm-starts` and, on Windows, `next-model` (the model a switch restarts the server
into), the instance lock `server-8790.lock` (one per port,
held while a server runs), `server.log` (the output of a server the popup started,
Linux/macOS), `update-check.json` (the server's daily release check), `anki-deck.json` (the deck
of the last card the server mined into, for the word colours), the phone portal's
`portal-state.json` (positions, the card setup, the paired devices' token hashes), `portal.json`
(an optional card setup) and `portal-thumbs/`, and, on Windows, the launcher's host manifests
`native-messaging/shisuko.json` (Firefox) and `native-messaging/shisuko-chrome.json` (Chrome).
