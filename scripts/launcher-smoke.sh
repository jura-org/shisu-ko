#!/usr/bin/env bash
# Runs the real server/run.sh against stub server.py / update.py / native_host.py and checks
# the loop the popup's Update button relies on: register, update, start the server with
# SHISUKO_LAUNCHER=1, and after an exit with code 4 update again (which replaces run.sh with a
# longer file while it runs), register again and start again, until the server exits 0.
# Then it runs the real server/setup.sh unattended against stubs, as `yes 1 | bash setup.sh`.
# Only stdlib Python, bash and a scratch HOME are needed; nothing outside the temp directory
# is touched. Usage: bash scripts/launcher-smoke.sh   (exit 0 = every step happened in order)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home"
mkdir -p "$HOME/.shisu-ko" "$WORK/checkout/server"
python3 -m venv "$HOME/.shisu-ko/venv"
cp "$HERE/server/run.sh" "$WORK/checkout/server/run.sh"
LOG="$WORK/steps.log"

# Each server run appends its launcher variable and exits with the next code from the queue.
cat > "$WORK/checkout/server/server.py" <<'EOF'
import os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
queue = here / "exit-codes.txt"
codes = queue.read_text().split()
code = int(codes.pop(0)) if codes else 0
queue.write_text(" ".join(codes))
Path(os.environ["SMOKE_LOG"]).open("a").write(f"server launcher={os.environ.get('SHISUKO_LAUNCHER', '')} args={' '.join(sys.argv[1:])} exit={code}\n")
sys.exit(code)
EOF
# The update replaces run.sh with a longer file (as a real update would), then records itself.
cat > "$WORK/checkout/server/update.py" <<'EOF'
import os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
run = here / "run.sh"
text = run.read_text()
if "# updated" not in text:
    run.write_text("# updated\n# the update adds lines at the top, so every old byte offset is wrong now\n" + text)
Path(os.environ["SMOKE_LOG"]).open("a").write(f"update args={' '.join(sys.argv[1:])}\n")
EOF
cat > "$WORK/checkout/server/native_host.py" <<'EOF'
import os, sys
from pathlib import Path
Path(os.environ["SMOKE_LOG"]).open("a").write(f"register args={' '.join(sys.argv[1:])}\n")
EOF
echo "4 0" > "$WORK/checkout/server/exit-codes.txt"

# set -e must not end the script on a non-zero exit here: the code is what is being checked.
if SMOKE_LOG="$LOG" bash "$WORK/checkout/server/run.sh" --lookahead 0; then code=0; else code=$?; fi
echo "run.sh exited with $code"
cat "$LOG"
expected="update args=--lookahead 0
register args=--register
server launcher=1 args=--lookahead 0 exit=4
update args=--lookahead 0
register args=--register
server launcher=1 args=--lookahead 0 exit=0"
if [ "$code" -ne 0 ]; then echo "FAIL: run.sh should end with the server's exit code 0"; exit 1; fi
if [ "$(cat "$LOG")" != "$expected" ]; then echo "FAIL: the launcher did not run the steps in this order:"; echo "$expected"; exit 1; fi
grep -q "^# updated" "$WORK/checkout/server/run.sh" || { echo "FAIL: update.py did not replace run.sh"; exit 1; }
echo "OK: run.sh registers, updates, passes SHISUKO_LAUNCHER=1, and runs the update again after exit code 4"

# setup.sh's comment offers `yes 1 | bash setup.sh` as the unattended pick of large-v3. It must
# still reach the model download and its last line where Firefox keeps a profile: the stub
# server.py asks the cookie question there as server.py does, again after anything but Y or N,
# three times at most (SETUP_COOKIES_TRIES).
SETUP="$WORK/setup/server"
SETUP_HOME="$WORK/setup-home"
SETUP_LOG="$WORK/setup-steps.log"
mkdir -p "$SETUP" "$SETUP_HOME/.shisu-ko/venv/bin"
cp "$HERE/server/setup.sh" "$SETUP/setup.sh"
cp "$WORK/checkout/server/native_host.py" "$SETUP/native_host.py"
# The venv's python: pip installs nothing, and the scripts it runs are the stubs.
cat > "$SETUP_HOME/.shisu-ko/venv/bin/python" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = "-m" ] && exit 0
exec python3 "$@"
EOF
chmod +x "$SETUP_HOME/.shisu-ko/venv/bin/python"
# Like server.py, the stub takes three answers that are neither Y nor N as none, so a setup.sh
# that passes `yes` on logs answers=3 and fails here where it should log answers=0.
cat > "$SETUP/server.py" <<'EOF'
import os, sys
from pathlib import Path
args = sys.argv[1:]
line = f"server args={' '.join(args)}"
if args == ["--setup-cookies"]:
    answers = 0
    while answers < 3:
        try:
            answer = input("Type Y or N: ").strip().lower()
        except EOFError:
            break
        answers += 1
        if answer in ("y", "yes", "n", "no"):
            break
    line += f" answers={answers}"
Path(os.environ["SMOKE_LOG"]).open("a").write(line + "\n")
EOF

# Without pipefail the status is setup.sh's, not that of `yes` ending on the closed pipe.
set +o pipefail
if yes 1 | HOME="$SETUP_HOME" SMOKE_LOG="$SETUP_LOG" bash "$SETUP/setup.sh" > "$WORK/setup.out" 2>&1; then code=0; else code=$?; fi
set -o pipefail
echo "setup.sh exited with $code"
cat "$SETUP_LOG"
expected="register args=--register --verbose
server args=--check
server args=--setup-cookies answers=0
server args=--download-model large-v3"
if [ "$code" -ne 0 ]; then echo "FAIL: setup.sh should end with 0"; cat "$WORK/setup.out"; exit 1; fi
if [ "$(cat "$SETUP_LOG")" != "$expected" ]; then echo "FAIL: setup.sh did not run these steps, the cookie question ending at an EOF:"; echo "$expected"; exit 1; fi
grep -q "Setup is complete: the large-v3 model is downloaded" "$WORK/setup.out" || { echo "FAIL: setup.sh did not say it is complete"; exit 1; }
echo "OK: setup.sh under \`yes 1\` picks large-v3, gives the cookie question an EOF and finishes"
