#!/usr/bin/env bash
# Starts the Shisu-ko transcription server (Linux/macOS) and restarts it if it stops
# unexpectedly. Extra arguments are passed through, e.g.
# bash server/run.sh --cookies-from-browser firefox, or bash server/run.sh --no-update to
# start without looking for a newer version first.
set -uo pipefail
VENV="${HOME}/.shisu-ko/venv"
HERE="$(cd "$(dirname "$0")" && pwd)"
# A copy of this file on its own (a zip browsed as a folder, a stray copy) has no siblings.
[ -f "${HERE}/server.py" ] || { echo "This script is running on its own, without the rest of Shisu-ko: extract the whole zip first, then start server/run.sh from the extracted folder."; exit 1; }
[ -x "${VENV}/bin/python" ] || { echo "Run bash server/setup.sh first"; exit 1; }

main() {
  # Looks for a newer version (git fast-forward, or the newest release for a downloaded
  # folder) and installs it; it never stops the server from starting.
  "${VENV}/bin/python" "${HERE}/update.py" "$@"
  # Registers the native-messaging host behind the extension's "Start server" button
  # (cheap, idempotent), so a checkout that never re-ran setup.sh gets the button too. After
  # the update, which may rewrite the wrapper: registering also restores its mode bit.
  "${VENV}/bin/python" "${HERE}/native_host.py" --register
  # Tells server.py that this loop is around: its POST /update (the extension's Update button)
  # answers yes only then, and exits with code 4 so that the loop runs the update. A run.sh
  # from before this line that updated itself still runs its old loop (parsed before the
  # update, without the code-4 branch) until it is restarted by hand; the server it starts
  # sees no variable, refuses the button and says why.
  export SHISUKO_LAUNCHER=1
  while true; do
    "${VENV}/bin/python" "${HERE}/server.py" "$@"
    code=$?
    [ "$code" -eq 0 ] && exit 0
    [ "$code" -eq 2 ] && exit 2
    # Exit code 4: update, give the wrapper its mode bits back, start again. The update may
    # replace this file, but bash parsed the whole function before any of it ran.
    [ "$code" -eq 4 ] && { "${VENV}/bin/python" "${HERE}/update.py" "$@"; "${VENV}/bin/python" "${HERE}/native_host.py" --register; continue; }
    echo "The server stopped unexpectedly (exit code $code). Restarting in 5 seconds... press Ctrl+C to quit."
    sleep 5
  done
}

# The update may replace this file while it runs, and bash reads scripts as it goes: the whole
# loop lives in a function that was parsed before the update, and nothing follows this line.
main "$@"; exit
