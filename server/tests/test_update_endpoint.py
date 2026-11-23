"""POST /update: the server exits with EXIT_UPDATE so that run.cmd / run.sh update and restart it.

The handler is driven over a real socket like test_http_oversized_body.py, against a real App
whose transcriber thread is stood down; the launcher is faked through SHISUKO_LAUNCHER in the
environment, `no_update` in the arguments and SHISUKO_NO_UPDATE, the variable update.py reads.
The launchers themselves are checked as text: their loops must read the exit code 4 and stay
safe against update.py replacing them.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from _serverlib import load_server
from test_update import update

server = load_server()
SERVER_DIR = Path(__file__).resolve().parent.parent
EXTENSION_ORIGIN = "moz-extension://0f3c2a9e-1b2c-4d5e-8f90-123456789abc"


class InertTranscriber:
    """App.__init__ starts the transcriber thread; nothing here needs it."""

    def __init__(self, app):
        self.app = app

    def start(self):
        pass


def make_app(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    base = dict(first_window=20.0, window=40.0, lookahead=900.0, model="large-v3", language="ja",
                idle_minutes=30, retry_after=30.0, no_update=False)
    base.update(overrides)
    app = server.App(SimpleNamespace(**base), model=object(), device="cpu", compute_type="int8")
    app.fetcher = SimpleNamespace(fetch=lambda s: None)
    return app


@pytest.fixture
def launcher(monkeypatch):
    """The environment run.cmd / run.sh give the server; tests unset it for the other starts."""
    monkeypatch.setenv("SHISUKO_LAUNCHER", "1")
    monkeypatch.delenv("SHISUKO_NO_UPDATE", raising=False)
    return monkeypatch


@pytest.fixture
def no_launcher(monkeypatch):
    monkeypatch.delenv("SHISUKO_LAUNCHER", raising=False)
    monkeypatch.delenv("SHISUKO_NO_UPDATE", raising=False)
    return monkeypatch


class Served:
    """A ThreadingHTTPServer on a free port, served on a thread the test can wait for."""

    def __init__(self, app):
        server.Handler.app = app
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.httpd.shutdown()
        self.httpd.server_close()
        server.Handler.app = None

    def request(self, method, path, body=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()


@pytest.fixture
def served(monkeypatch, tmp_path):
    made = []

    def start(**overrides):
        served_ = Served(make_app(monkeypatch, tmp_path, **overrides))
        made.append(served_)
        return served_

    try:
        yield start
    finally:
        for served_ in made:
            served_.close()


# --- the exit code and the App ------------------------------------------------------------------

def test_exit_update_is_its_own_code():
    assert server.EXIT_UPDATE == 4, "run.cmd / run.sh compare the exit code with the literal 4"
    assert server.EXIT_UPDATE not in (0, 2, 3), "0 stops, 2 must not be retried, 3 restarts without an update"


def test_request_update_needs_the_launcher_and_no_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("SHISUKO_LAUNCHER", raising=False)
    monkeypatch.delenv("SHISUKO_NO_UPDATE", raising=False)
    app = make_app(monkeypatch, tmp_path)
    ok, error = app.request_update()
    assert (ok, app.exit_code) == (False, None)
    assert "run.cmd / run.sh" in error

    monkeypatch.setenv("SHISUKO_LAUNCHER", "1")
    app = make_app(monkeypatch, tmp_path, no_update=True)
    ok, error = app.request_update()
    assert (ok, app.exit_code) == (False, None)
    assert "--no-update" in error

    app = make_app(monkeypatch, tmp_path)
    assert app.request_update() == (True, None)
    assert app.exit_code == server.EXIT_UPDATE


def test_request_update_reads_the_variable_strictly(monkeypatch, tmp_path):
    monkeypatch.delenv("SHISUKO_NO_UPDATE", raising=False)
    for value in ("", "0", "true", "yes"):
        monkeypatch.setenv("SHISUKO_LAUNCHER", value)
        assert make_app(monkeypatch, tmp_path).request_update()[0] is False, value


def test_request_update_honours_the_variable_update_py_reads(launcher, tmp_path):
    # The launcher runs update.py under the server's own environment, so SHISUKO_NO_UPDATE=1 would
    # make the exit a plain restart: a model reload for nothing and not a word from update.py.
    launcher.setenv("SHISUKO_NO_UPDATE", "1")
    app = make_app(launcher, tmp_path)
    ok, error = app.request_update()
    assert (ok, app.exit_code) == (False, None)
    assert "SHISUKO_NO_UPDATE" in error and "--no-update" not in error
    assert app.health()["launcher"] is False


@pytest.mark.parametrize("value", [None, "", "0", " 0 ", "1", " 1 ", "true", "no", "off"])
def test_request_update_reads_no_update_exactly_like_update_py(launcher, tmp_path, value):
    # One rule in two files: whatever update.skipped() makes of the value, the server must agree,
    # or /update promises an update that never runs (or refuses one that would).
    if value is None:
        launcher.delenv("SHISUKO_NO_UPDATE", raising=False)
        environ = {}
    else:
        launcher.setenv("SHISUKO_NO_UPDATE", value)
        environ = {"SHISUKO_NO_UPDATE": value}
    blocked = make_app(launcher, tmp_path).request_update()[0] is False
    assert blocked == update.skipped([], environ=environ), value


def test_health_reports_whether_update_would_work(monkeypatch, tmp_path):
    monkeypatch.delenv("SHISUKO_LAUNCHER", raising=False)
    monkeypatch.delenv("SHISUKO_NO_UPDATE", raising=False)
    health = make_app(monkeypatch, tmp_path).health()
    assert health["launcher"] is False and health["version"] == server.VERSION and health["ok"] is True
    monkeypatch.setenv("SHISUKO_LAUNCHER", "1")
    assert make_app(monkeypatch, tmp_path, no_update=True).health()["launcher"] is False
    assert make_app(monkeypatch, tmp_path).health()["launcher"] is True
    # Arguments without the flag at all (an App built by hand, older callers) count as updatable.
    assert make_app(monkeypatch, tmp_path).health()["launcher"] is True


# --- the endpoint -------------------------------------------------------------------------------

def test_update_is_refused_without_the_launcher(no_launcher, served):
    s = served()
    status, body = s.request("POST", "/update", "{}")
    assert status == 409
    assert body["ok"] is False and "run.cmd / run.sh" in body["error"]
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    assert s.request("GET", "/health", origin=None)[0] == 200, "the server keeps serving"
    assert s.thread.is_alive()


def test_update_is_refused_with_no_update(launcher, served):
    s = served(no_update=True)
    status, body = s.request("POST", "/update", "{}")
    assert status == 409
    assert body["ok"] is False and "--no-update" in body["error"]
    assert s.request("GET", "/health")[1]["launcher"] is False
    assert s.thread.is_alive()


def test_update_is_refused_with_the_no_update_variable(launcher, served):
    launcher.setenv("SHISUKO_NO_UPDATE", "1")
    s = served()
    status, body = s.request("POST", "/update", "{}", origin=EXTENSION_ORIGIN)
    assert status == 409
    assert body["ok"] is False and "SHISUKO_NO_UPDATE" in body["error"]
    assert s.request("GET", "/health")[1]["launcher"] is False
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    assert s.thread.is_alive()


def test_update_answers_then_stops_the_server(launcher, served):
    s = served()
    assert s.request("GET", "/health")[1]["launcher"] is True
    status, body = s.request("POST", "/update", "{}", origin=EXTENSION_ORIGIN)
    assert (status, body) == (200, {"ok": True, "restarting": True, "version": server.VERSION})
    assert s.httpd.RequestHandlerClass.app.exit_code == server.EXIT_UPDATE
    # The answer is out; serve_forever() returns a moment later and main() takes it from there.
    s.thread.join(5 + server.SHUTDOWN_DELAY)
    assert not s.thread.is_alive(), "serve_forever() did not return after POST /update"


def test_update_from_a_foreign_origin_is_refused(launcher, served):
    s = served()
    status, body = s.request("POST", "/update", "{}", origin="https://www.youtube.com")
    assert (status, body) == (403, {"ok": False, "error": "origin not allowed"})
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    time.sleep(server.SHUTDOWN_DELAY + 0.2)
    assert s.thread.is_alive(), "a refused request must not stop the server"


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:5500", "http://[::1]:8888", "null"])
def test_update_from_a_loopback_page_is_refused(launcher, served, origin):
    # A page on this machine may drive transcription like the extension, but not end the server
    # and make the launcher run git and pip: a local dev server with a third-party script is
    # not the popup. The other endpoints keep admitting it.
    s = served()
    status, body = s.request("POST", "/update", "{}", origin=origin)
    assert (status, body) == (403, {"ok": False, "error": "origin not allowed"}), origin
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    if origin != "null":
        assert s.request("GET", "/health", origin=origin)[0] == 200, "loopback pages still see the rest"
    time.sleep(server.SHUTDOWN_DELAY + 0.2)
    assert s.thread.is_alive(), "a refused request must not stop the server"


def test_update_from_a_loopback_page_is_refused_without_a_body(launcher, served):
    # fetch(url, {method: "POST", mode: "no-cors"}) from a loopback page: no preflight, no body,
    # no Content-Type, but always an Origin header, which is what the rule reads.
    s = served()
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    try:
        conn.request("POST", "/update", headers={"Origin": "http://localhost:8888"})
        resp = conn.getresponse()
        assert resp.status == 403
        resp.read()
    finally:
        conn.close()
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    assert s.thread.is_alive()


@pytest.mark.parametrize("origin", [
    None,  # curl, a script: no Origin header at all
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "safari-web-extension://ABCDEF12-3456-7890-ABCD-EF1234567890",
])
def test_update_admits_the_extension_and_non_browser_clients(launcher, served, origin):
    s = served()
    status, body = s.request("POST", "/update", "{}", origin=origin)
    assert (status, body["restarting"]) == (200, True), origin


def test_update_origin_rule_is_the_extension_or_no_origin():
    for origin in (None, "moz-extension://0f3c2a9e-1b2c-4d5e-8f90-123456789abc", "chrome-extension://a"):
        assert server.update_origin_allowed(origin), origin
    for origin in ("http://localhost:3000", "https://127.0.0.1", "http://[::1]:1", "null", "", "https://evil.example"):
        assert not server.update_origin_allowed(origin), origin
    # The loopback ones are real pages for the other endpoints: only this rule shuts them out.
    assert server.origin_allowed("http://localhost:3000") and not server.update_origin_allowed("http://localhost:3000")


def test_get_update_is_not_found(launcher, served):
    s = served()
    assert s.request("GET", "/update") == (404, {"ok": False, "error": "not found"})
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    assert s.thread.is_alive()


def test_update_parses_the_body_like_sync_and_ignores_it(launcher, served):
    s = served()
    for raw in ("not json", "[]", '"x"'):
        assert s.request("POST", "/update", raw) == (400, {"ok": False, "error": "invalid JSON body"}), raw
    assert s.httpd.RequestHandlerClass.app.exit_code is None
    # Any object (or no body at all) is fine: the request carries nothing the server needs.
    status, body = s.request("POST", "/update", json.dumps({"anything": 1}))
    assert status == 200 and body["restarting"] is True


def test_update_keeps_the_body_size_guard(launcher, served):
    s = served()
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    try:
        conn.putrequest("POST", "/update")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "70000")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 413
        assert json.loads(resp.read().decode("utf-8")) == {"ok": False, "error": "request too large"}
    finally:
        conn.close()
    assert s.httpd.RequestHandlerClass.app.exit_code is None


# --- main() -------------------------------------------------------------------------------------

@pytest.fixture
def main_ready(monkeypatch, tmp_path):
    """main() with the model load, the instance lock and the transcriber faked, on a thread.

    Returns (port, outcome) once the server listens; `outcome` holds "returned" or the exit code
    of the SystemExit main() raised, after the thread ends.
    """
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(server, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(server, "Transcriber", InertTranscriber)
    monkeypatch.setattr(server, "hold_instance_lock", lambda port: True)
    monkeypatch.setattr(server, "load_model", lambda args: (object(), "cpu", "int8"))
    monkeypatch.setattr(server, "parse_args", lambda argv=None: SimpleNamespace(
        host="127.0.0.1", port=0, model="large-v3", language="ja", log_level="INFO", check=False,
        first_window=20.0, window=40.0, lookahead=900.0, idle_minutes=30, retry_after=30.0, no_update=False))
    created = []

    class Recording(server.OwnPortHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(server, "OwnPortHTTPServer", Recording)
    outcome = {}

    def run():
        try:
            server.main()
        except SystemExit as exc:
            outcome["exit"] = exc.code
        else:
            outcome["exit"] = "returned"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not created and time.time() < deadline:
        time.sleep(0.02)
    assert created, "main() never created the HTTP server"
    try:
        yield created[0], outcome, thread
    finally:
        if thread.is_alive():
            created[0].shutdown()
            thread.join(5)
        server.Handler.app = None


def post_update(port, launcher_env, monkeypatch):
    if launcher_env is None:
        monkeypatch.delenv("SHISUKO_LAUNCHER", raising=False)
    else:
        monkeypatch.setenv("SHISUKO_LAUNCHER", launcher_env)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/update", body="{}", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def test_main_exits_with_the_update_code_after_the_answer(main_ready, monkeypatch):
    httpd, outcome, thread = main_ready
    status, body = post_update(httpd.server_address[1], "1", monkeypatch)
    assert status == 200 and body["restarting"] is True
    thread.join(5 + server.SHUTDOWN_DELAY)
    assert not thread.is_alive()
    assert outcome["exit"] == server.EXIT_UPDATE


def test_main_still_returns_quietly_when_nothing_asked_for_an_update(main_ready, monkeypatch):
    httpd, outcome, thread = main_ready
    assert post_update(httpd.server_address[1], None, monkeypatch)[0] == 409
    httpd.shutdown()  # what Ctrl+C amounts to
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["exit"] == "returned"


# --- the launchers ------------------------------------------------------------------------------

def cmd_lines():
    raw = (SERVER_DIR / "run.cmd").read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b""), "run.cmd must stay CRLF"
    return raw.decode("utf-8").split("\r\n")


def index_of(lines, predicate, what):
    hits = [i for i, line in enumerate(lines) if predicate(line)]
    assert len(hits) == 1, f"{what}: expected exactly one line, found {hits}"
    return hits[0]


def test_run_cmd_sets_the_launcher_variable_inside_the_loop():
    # After :loop and before the server call, so it runs on every pass, including the first one
    # of a run.cmd from before the variable: that launcher's own "goto loop" (parsed before
    # update.py replaced the file) lands here and never sees anything above the label. Above
    # :update would also serve "goto update", but not that old launcher.
    lines = cmd_lines()
    variable = index_of(lines, lambda l: l == 'set "SHISUKO_LAUNCHER=1"', "SHISUKO_LAUNCHER")
    loop = index_of(lines, lambda l: l == ":loop", ":loop")
    start = index_of(lines, lambda l: l == '"%VENV%\\Scripts\\python.exe" "%~dp0server.py" %*', "server call")
    assert loop < variable < start
    assert all(l.startswith("REM ") for l in lines[loop + 1:variable]), "only comments between :loop and the set"
    assert all(l.startswith("REM ") or l == "" for l in lines[variable + 1:start]), "the set is the last thing before the server"


def test_run_cmd_update_label_sits_right_above_the_one_line_update_call():
    lines = cmd_lines()
    label = index_of(lines, lambda l: l == ":update", ":update")
    update = lines[label + 1]
    assert update == '"%VENV%\\Scripts\\python.exe" "%~dp0update.py" %* & goto loop', update
    assert sum("update.py" in l and not l.startswith("REM") for l in lines) == 1, "one update call, shared with goto loop"
    loop = index_of(lines, lambda l: l == ":loop", ":loop")
    assert loop > label + 1 and all(l == "" for l in lines[label + 2:loop]), "nothing runs between the update and :loop"
    # The register call stays where it was: before the update line that may replace this file.
    register = index_of(lines, lambda l: "native_host.py" in l and not l.startswith("REM"), "register")
    assert register < label


def test_run_cmd_loop_reads_the_update_code_and_keeps_the_others():
    lines = cmd_lines()
    loop = index_of(lines, lambda l: l == ":loop", ":loop")
    end = index_of(lines, lambda l: l == ":end", ":end")
    body = [l for l in lines[loop + 1:end] if not l.startswith("REM ")]
    assert body[0] == 'set "SHISUKO_LAUNCHER=1"'
    assert body[1] == '"%VENV%\\Scripts\\python.exe" "%~dp0server.py" %*'
    assert body[2] == 'set "CODE=%ERRORLEVEL%"'
    assert body[3] == 'if "%CODE%"=="0" goto end'
    assert body[4] == 'if "%CODE%"=="2" goto end'
    assert body[5] == 'if "%CODE%"=="4" goto update', "the update branch comes before the restart message"
    assert any(l.startswith("echo The server stopped unexpectedly") for l in body[6:])
    assert "timeout /t 5 /nobreak >nul" in body[6:]
    assert [l for l in body if l.strip()][-1] == "goto loop"
    assert lines[end + 1] == "pause"


def test_run_cmd_explains_why_the_update_branch_is_safe():
    text = "\n".join(cmd_lines())
    comments = "\n".join(l for l in text.split("\n") if l.startswith("REM "))
    assert "old file" in comments and "new" in comments and "goto update" in comments


def sh_text():
    raw = (SERVER_DIR / "run.sh").read_bytes()
    assert b"\r" not in raw, "run.sh must stay LF"
    return raw.decode("utf-8")


def test_run_sh_exports_the_variable_inside_main_before_the_loop():
    text = sh_text()
    main_start = text.index("main() {")
    loop = text.index("while true; do")
    export = text.index("export SHISUKO_LAUNCHER=1")
    assert main_start < export < loop
    assert text.count("SHISUKO_LAUNCHER") == 1, "set once, inside main(), so nothing depends on the top of the file"


def test_run_sh_loop_reads_the_update_code_and_keeps_the_others():
    text = sh_text()
    loop = text[text.index("while true; do"):text.index("done")]
    lines = [l.strip() for l in loop.split("\n") if l.strip() and not l.strip().startswith("#")]
    assert lines[1] == '"${VENV}/bin/python" "${HERE}/server.py" "$@"'
    assert lines[2] == "code=$?"
    assert lines[3] == '[ "$code" -eq 0 ] && exit 0'
    assert lines[4] == '[ "$code" -eq 2 ] && exit 2'
    assert lines[5] == ('[ "$code" -eq 4 ] && { "${VENV}/bin/python" "${HERE}/update.py" "$@"; '
                        '"${VENV}/bin/python" "${HERE}/native_host.py" --register; continue; }')
    assert lines[6].startswith('echo "The server stopped unexpectedly')
    assert lines[7] == "sleep 5"
    assert text.count("update.py") == 2, "the start-up call and the code-4 call"


def test_run_sh_keeps_everything_in_main_and_ends_with_its_call():
    text = sh_text()
    assert text.rstrip("\n").endswith('main "$@"; exit')
    after_main = text[text.index("main() {"):]
    body_end = after_main.index("\n}\n")
    tail = after_main[body_end + 3:]
    assert all(l.startswith("#") or l == "" or l == 'main "$@"; exit' for l in tail.split("\n")), tail


def test_launcher_texts_mention_the_flag_they_forward():
    # --no-update reaches server.py through "$@" / %*, where it turns /update off.
    assert re.search(r"--no-update", sh_text()) and re.search(r"--no-update", "\n".join(cmd_lines()))
    assert server.parse_args(["--no-update"]).no_update is True
    assert server.parse_args([]).no_update is False
