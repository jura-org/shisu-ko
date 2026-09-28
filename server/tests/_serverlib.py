"""Loads server/server.py as an importable module.

server.py is a script, not a package, and uses `from __future__ import annotations`
(postponed evaluation), so it must be registered in sys.modules under its own name
before exec, the same way the "Development" section of the README describes.

server.py fixes its data folder at import time (APP_DIR, and every path under it: the cache,
config.json, next-model, rocm-starts, the instance locks): SHISUKO_HOME, else the real
~/.shisu-ko. Before the import, isolate_home() points SHISUKO_HOME at a fresh temporary folder
for the whole process unless the caller set one, so nothing loaded here can write to the real
folder; conftest.py calls it before any test module is collected, for the child processes the
tests start as well.

The import runs with SHISUKO_ENGINE=default: server.py decides at import time whether the AMD
engine is on (rocm_engine()), and the tests must not depend on what the machine they run on has
installed. test_rocm.py sets ROCM_ACTIVE itself where it needs it.
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

_MODULE_NAME = "shisuko_server"
_SERVER_PATH = Path(__file__).resolve().parent.parent / "server.py"
_temporary_homes: list[str] = []


def isolate_home() -> Path:
    """SHISUKO_HOME for this process: the caller's, else a fresh temporary folder set in os.environ.

    An empty SHISUKO_HOME counts as unset, as it does in server.py (`or` falls back to the real
    folder). The temporary folder is removed when the interpreter exits (remove_temporary_homes()).
    """
    current = os.environ.get("SHISUKO_HOME")
    if current:
        return Path(current)
    folder = tempfile.mkdtemp(prefix="shisuko-tests-")
    os.environ["SHISUKO_HOME"] = folder
    _temporary_homes.append(folder)
    return Path(folder)


@atexit.register
def remove_temporary_homes() -> None:
    """Deletes the folders isolate_home() made; conftest.py calls it itself before its os._exit() on CI."""
    while _temporary_homes:
        shutil.rmtree(_temporary_homes.pop(), ignore_errors=True)


def load_server():
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None:
        return cached
    isolate_home()
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    engine = os.environ.get("SHISUKO_ENGINE")
    os.environ["SHISUKO_ENGINE"] = "default"
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    finally:
        if engine is None:
            os.environ.pop("SHISUKO_ENGINE", None)
        else:
            os.environ["SHISUKO_ENGINE"] = engine
    return module
