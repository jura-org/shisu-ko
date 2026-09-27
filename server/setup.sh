#!/usr/bin/env bash
# One-time setup for Linux/macOS: isolated Python environment under ~/.shisu-ko
set -euo pipefail
ROOT="${HOME}/.shisu-ko"
VENV="${ROOT}/venv"
HERE="$(cd "$(dirname "$0")" && pwd)"
# A copy of this file on its own (a zip browsed as a folder, a stray copy) has no siblings.
[ -f "${HERE}/server.py" ] || { echo "This script is running on its own, without the rest of Shisu-ko: extract the whole zip first, then start server/setup.sh from the extracted folder."; exit 1; }

command -v python3 >/dev/null 2>&1 || { echo "python3 (3.10+) is required"; exit 1; }
mkdir -p "${ROOT}/cache" "${ROOT}/models"
[ -x "${VENV}/bin/python" ] || python3 -m venv "${VENV}"
"${VENV}/bin/python" -m pip install --upgrade pip
"${VENV}/bin/python" -m pip install -r "${HERE}/requirements.txt"
if command -v nvidia-smi >/dev/null 2>&1; then
  "${VENV}/bin/python" -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
fi
# The native-messaging host behind the extension's "Start server" button, registered before
# the environment check reports whether it is; a failure here (it prints why) must not undo
# the setup that just succeeded.
"${VENV}/bin/python" "${HERE}/native_host.py" --register --verbose || true
"${VENV}/bin/python" "${HERE}/server.py" --check

# The model the server starts with, downloaded now so the first start is not the wait; the popup
# can switch to another one later. The check above said whether there is a CUDA device. The first
# choice is whatever a bare start would load here, asked of server.py rather than spelled out
# again: on the Apple GPU that is large-v3-turbo, which fits beside the browser, and elsewhere
# large-v3. An EOF on read (stdin closed or redirected from an empty file) takes it instead of
# asking forever; `yes 1 | bash setup.sh` picks it the same way.
BEST="$("${VENV}/bin/python" "${HERE}/server.py" --default-model 2>/dev/null || echo large-v3)"
case "$BEST" in
  large-v3-turbo) BEST_LINE="large-v3-turbo  about 1.6 GB, the one that keeps up on an Apple GPU beside a browser";;
  *)              BEST_LINE="large-v3        best quality, about 3 GB, wants a GPU with 4 GB or more free";;
esac
echo
echo "Which Whisper model should the server use? (the popup can switch later)"
echo "  1  ${BEST_LINE}"
echo "  2  small           about 500 MB, fine on a CPU, less accurate"
while :; do
  read -r -p "Type 1 or 2: " pick || pick=1
  case "$pick" in
    1) MODEL="$BEST"; break;;
    2) MODEL=small; break;;
  esac
done
# YouTube refuses some downloads ("Sign in to confirm you're not a bot") until they carry a
# signed-in browser's cookies. server.py asks, only where Firefox keeps a profile, and reads nothing
# before a yes; the answer goes to config.json, the default of every start, the popup's Start
# button included. An EOF leaves config.json as it is, and nothing here may end the setup. Only a
# terminal answers: a piped stdin held the model's answer, and `yes 1` would spend server.py's
# three tries on "1", so there the question gets an EOF instead.
echo
[ -t 0 ] || exec </dev/null
"${VENV}/bin/python" "${HERE}/server.py" --setup-cookies || true
echo
# Inside the if, set -e leaves the verdict to us: a failed download ends setup with a word on it.
if ! "${VENV}/bin/python" "${HERE}/server.py" --download-model "$MODEL"; then
  echo "The model could not be downloaded. Check the connection and run setup.sh again,"
  echo "or start ./run.sh: the server then downloads $MODEL itself, without a progress bar."
  exit 1
fi
echo
echo "Setup is complete: the $MODEL model is downloaded and everything is ready."
echo "Close this window and start ./run.sh."
