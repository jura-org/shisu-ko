#!/usr/bin/env bash
# Native-messaging host for the extension's "Start server" button. Firefox and Chrome (on
# Linux Chromium too) run this file (registered by native_host.py --register), each with
# arguments of its own, and talk to it over stdin/stdout, so nothing here may print: the
# venv's Python runs the host, or the system Python before setup ran.
PY="${HOME}/.shisu-ko/venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" "$(dirname "$0")/native_host.py" "$@"
