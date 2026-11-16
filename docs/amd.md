# AMD graphics cards (experimental)

The native server can run Whisper on an AMD graphics card, through CTranslate2's build for AMD's
ROCm. This is experimental: it is not yet tested on AMD hardware by the maintainer. Reports from
AMD users, whether it works or not, are welcome as a
[GitHub issue](https://github.com/Multysquid/shisu-ko/issues), with the card, the system and
what `amd_setup.py --status` and `run.cmd --check` print.

**Which cards.** On Windows: Radeon RX 7000 and RX 9000, Radeon PRO W7000 and W9000, Radeon AI
PRO R9700, and the graphics of Ryzen AI 300 (Radeon 890M, 880M) and Ryzen AI Max (Radeon 8060S,
8050S, 8040S), with AMD Software: Adrenalin Edition 26.2.2 or newer. AMD's Windows runtime has
no kernels for the RX 6000 series or anything older, nor for other processor graphics such as
the Radeon 780M or 860M, so on Windows those get no AMD engine. A card that Windows lists as
"Microsoft Basic Display Adapter" has no AMD driver yet; install Adrenalin first. On Linux: the
same cards and the RX 6800 and 6900 series (gfx1030), on a ROCm 7.2.x that you install yourself
following [AMD's guide for 7.2](https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.4/),
with your user in the `render` and `video` groups (`sudo usermod -aG render,video "$USER"`, then
log out and in again); until then setup says what is missing and asks nothing. Follow the guide
for 7.2, not AMD's latest one: that installs ROCm 10, and the engine needs ROCm 7.2's libraries.
On Linux setup goes by the GPU target that the amdgpu driver reports (`gfx1100` and so on), not
by the card's name. An RX 6600 or 6700 (gfx1032, gfx1031) is not one of the targets the build is
compiled for, but is reported to run when told to pass for a gfx1030; that is at your own risk:
put `export HSA_OVERRIDE_GFX_VERSION=10.3.0` in your login shell's profile (`~/.bash_profile`,
`~/.zprofile` or `~/.profile`), log out and in again, and run `amd_setup.py --ignore-old-graphics`. There is no
AMD engine in the Docker image, with Nix or on macOS; the first two keep their own engine even
when they share `~/.shisu-ko` with a native setup that has one.

**What setup does.** After the model download, `setup.cmd` / `setup.sh` run
`server/amd_setup.py`, which looks for an AMD graphics card. Where it finds one the engine runs
on, it names the card, says that the engine is experimental, how much it downloads and, on
Windows, how much room it takes, and asks `Download the AMD engine? [y/N]`; anything but `y` or
`yes` is a no, and so is an unattended setup. A card it does not know gets the question with a
warning; a card the engine cannot run (an older Radeon, an RX 6000 on Windows, ...) gets one line
saying why, and no question; `amd_setup.py --ignore-old-graphics` offers the engine for it anyway,
at your own risk (it is switched on only if its test passes, which on such a card it likely does
not). Next to an NVIDIA GPU the server stays on the NVIDIA GPU and the engine is not offered;
`amd_setup.py --yes` installs it anyway, and the server then uses the AMD card instead (an
unsupported card there needs `--yes --ignore-old-graphics`). Whatever happens in this step, the
setup finishes.

On Windows a yes downloads about 1.27 GB: CTranslate2 4.8.2's ROCm build from its GitHub release
and AMD's ROCm 7.2.1 runtime from `repo.radeon.com`. Installed, the engine takes about 3.90 GB,
and about 5.31 GB must be free on the disk of `~/.shisu-ko` while it installs (where the system's
temporary folder is on another disk, pip needs about 3.90 GB there as well); setup checks the
free space before the first download and again before it installs. On Linux the download is
about 284 MB, since the runtime is the system's own ROCm. Each file is checked against the size
and SHA-256 pinned in `amd_setup.py` and installed with pip into a folder of its own,
`~/.shisu-ko/rocm`; the downloads wait in `~/.shisu-ko/cache/rocm-download` meanwhile and are
deleted afterwards. The venv's own CTranslate2 is never touched, so the NVIDIA and CPU engines
stay exactly as they are.

Then setup tests the engine: `server.py --probe-gpu` loads the model chosen at setup on the AMD
GPU, in a process of its own, and transcribes two seconds of silence with it. That can take some
minutes; the test gives up after 15. Only a test that passes switches the server over
(`"engine": "rocm"` in `~/.shisu-ko/config.json`), and it ends with "The server will use the AMD
GPU from its next start." A card or driver that is not up to it can kill the test's process,
never the setup's. From then on every start logs
`GPU engine: AMD ROCm (CTranslate2 4.8.2 from ...)` and loads the model "on the AMD GPU (ROCm)".

**Checking, testing again, removing.** `run.cmd --check` (`bash server/run.sh --check`) prints
`GPU engine: AMD ROCm (CTranslate2 4.8.2 from ...)` when the server uses the engine, and
`GPU engine: AMD ROCm installed but not used:` with the reason when it does not; without the
engine there is no such line. To CTranslate2 the AMD card is a CUDA device, so the line above it
counts it as one, and `/health` reports the device as `cuda`. `amd_setup.py` runs with the
venv's Python, in the Shisu-ko folder:
`%USERPROFILE%\.shisu-ko\venv\Scripts\python server\amd_setup.py --status` in the Windows
Command Prompt (in PowerShell, start it with `~\.shisu-ko\venv\Scripts\python`; on Linux,
`~/.shisu-ko/venv/bin/python server/amd_setup.py --status`). Its messages give the whole command
for the next step:

```
amd_setup.py            look for an AMD card, ask, download, install and test
amd_setup.py --yes      the same without the question, also next to an NVIDIA GPU; exits with 1
                        unless the engine ends up working
amd_setup.py --ignore-old-graphics
                        also offer the engine for a card the support lists rule out (older
                        Radeons, RX 6000 on Windows, ...); still asks, combines with --yes;
                        --ignore_old_graphics works too
amd_setup.py --probe    test the installed engine again, without a download; exits with 1 unless
                        the test passes
amd_setup.py --status   what is found, installed and switched on; downloads and changes nothing
amd_setup.py --remove   switch the engine off, delete ~/.shisu-ko/rocm and any leftover downloads
```

Without `--yes` or `--probe` it always exits with 0, so that the setup scripts never fail on it.

**When it fails.** A test that fails or crashes says why (the reason `server.py` gave, or "the
test crashed" with the exit code) and leaves the engine switched off: the server stays on the
NVIDIA GPU or the CPU, as before. The installed engine stays too, so that `amd_setup.py --probe`
can test it again without a download, after a driver update for instance. A download that breaks
off is tried once more, a file that does not match its pin is deleted and stops the install, and
finished downloads are kept for the next try. A setup that no longer finds an AMD GPU switches
an installed engine off.

At a server start the engine can fail where Python cannot catch it: a card or driver that is not
up to it kills the process, and `run.cmd` / `run.sh` would start it into the same crash again.
The server therefore counts its starts on the AMD engine until one has loaded its model on the
GPU and warmed it up; after two that did not, it leaves the engine off and starts on the default
engine. A start whose engine does not load, or whose ROCm runtime sees no AMD GPU (a card removed
or replaced, a driver it cannot use), leaves it off at once: the log says why, and the server
exits with code 3, which the launcher answers with a start on the default engine (a server
started by hand just stops, and its next start is on the default engine). On Linux the engine
also stays off while ROCm 7.2's libraries are missing from `$ROCM_PATH/lib` (`/opt/rocm/lib` by
default). In each case `run.cmd --check` gives the reason, and `amd_setup.py --probe` tests the
engine again and switches it back on when it passes.

To go back to the default engine for good, run `amd_setup.py --remove`: it switches the engine
off in `config.json` and deletes `~/.shisu-ko/rocm` and any downloads left over, and the next
start runs on the NVIDIA GPU or the CPU as before. `SHISUKO_ENGINE=default` in the server's
environment leaves the engine off for the starts that have it, without removing anything.

**Known limitations.**

- The server loads the model at its start and keeps it: `--model-idle-minutes` does not apply
  to the AMD engine.
- It is not yet tested on AMD hardware by the maintainer. The card lists follow the GPU targets
  CTranslate2's ROCm build is compiled for and the kernels in AMD's Windows runtime.
- The engine uses the first AMD GPU the ROCm runtime lists (HIP device 0). On a machine with
  both an AMD processor's graphics and an AMD card, that can be the wrong one, and the test fails
  or runs on the processor's graphics. `HIP_VISIBLE_DEVICES` set to the card's number (`1`, say)
  hides the others: set it where every start sees it, the Start button's too (a user environment
  variable on Windows, the login shell's profile on Linux), then run `amd_setup.py --probe`.
- On Windows, switching the model in the popup restarts the server: freeing a model can hang
  AMD's runtime there, so the server hands the new name to its next start and exits with code
  3, and `run.cmd` (which the popup's **Start server** button runs too) starts it again five
  seconds later with that model. The subtitles stop for the restart and the load. A server
  started as a plain `python server.py` has nothing to restart it and refuses the switch;
  choose the model with `--model` there.
- On Windows the server cannot read the AMD card's free memory, so it loads float16 weights and
  never switches to int8 by itself; with little free VRAM, start it with
  `--compute-type int8_float16` or pick a smaller model. On Linux it reads the free memory from
  the amdgpu driver.
- The engine is built for the venv's Python version: a venv rebuilt on another one runs on the
  default engine until `amd_setup.py` has installed the matching build. A Shisu-ko update that
  pins a newer engine leaves the installed one working, and `amd_setup.py` offers the update
  (`Update the AMD engine? [y/N]`).
- On Linux a variable that every start needs (`ROCM_PATH` when ROCm is not in `/opt/rocm`,
  `HSA_OVERRIDE_GFX_VERSION`, `HIP_VISIBLE_DEVICES`) belongs in the login shell's profile: the
  popup's **Start server** button starts the server without a terminal's exports.
