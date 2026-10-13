#!/usr/bin/env bash
# Starts the phone portal (portal/portal.py) with the server's Python environment, which has
# yt-dlp and PyAV. Arguments are passed through, e.g.
#   ./portal.sh                  this computer only (http://127.0.0.1:8791/)
#   ./portal.sh --mode lan       phones and tablets on the local network, once paired
#   ./portal.sh --mode tailscale your own devices on your tailnet
set -uo pipefail
VENV="${HOME}/.shisu-ko/venv"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "${HERE}/../portal/portal.py" ] || { echo "The portal folder is missing: extract the whole zip first, then start server/portal.sh from the extracted folder."; exit 1; }
[ -x "${VENV}/bin/python" ] || { echo "Run ./setup.sh first"; exit 1; }
exec "${VENV}/bin/python" "${HERE}/../portal/portal.py" "$@"
