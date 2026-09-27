#!/usr/bin/env python3
"""Installs what the Kitsune-Transcribe models run on: PyTorch, transformers and safetensors.

The server runs Whisper through CTranslate2 and needs none of this. Kitsune's students are not
Whisper models, and the server runs them on PyTorch (server/kitsune_engine.py). setup.cmd /
setup.sh run this file when a Kitsune model is picked, before the model downloads. It installs
PyTorch's CUDA build where nvidia-smi lists an NVIDIA GPU, its CPU build elsewhere (about 3 GB
against about 200 MB), PyPI's own build on macOS, and then server/requirements-kitsune.txt, all
into the interpreter running it (the venv's). A later run with PyTorch already importable
installs nothing unless --force says so.

  kitsune_setup.py            install when missing; exit 0 once torch and transformers import
  kitsune_setup.py --cpu      the CPU build even where there is an NVIDIA GPU
  kitsune_setup.py --force    install again (a CPU build replaced by the CUDA one, say)
  kitsune_setup.py --status   what is installed and whether PyTorch sees a GPU; nothing is installed

Exit 1 when the packages do not import afterwards, which ends setup: the model picked would not
start. Stdlib only, on purpose, and it never imports server.py, like amd_setup.py.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE / "requirements-kitsune.txt"
# PyTorch's own wheel indexes. CUDA 12.8 is the oldest build with Blackwell (RTX 50) kernels, and
# it runs on every NVIDIA driver from 570 on.
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
IMPORT_CHECK = "import torch, transformers, safetensors; print(torch.__version__, transformers.__version__)"


def say(message: str) -> None:
    print(f"[kitsune] {message}", flush=True)


def nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe is None and os.name == "nt":
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(candidate) if candidate.exists() else None
    return exe


def nvidia_gpus() -> list[str]:
    """The GPUs nvidia-smi lists ("GPU 0: NVIDIA GeForce ..."), or none when it is missing or fails."""
    exe = nvidia_smi()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-L"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip().startswith("GPU ")]


def torch_command(python: str, cuda: bool, platform: str = sys.platform) -> list[str]:
    """pip's command line for PyTorch: the CUDA or CPU index, or plain PyPI on macOS (no CUDA there)."""
    cmd = [python, "-m", "pip", "install", "--upgrade", "torch"]
    if platform == "darwin":
        return cmd
    return cmd + ["--index-url", TORCH_CUDA_INDEX if cuda else TORCH_CPU_INDEX]


def installed(python: str = sys.executable) -> str | None:
    """'torch-version transformers-version' when the packages import in `python`, else None."""
    try:
        done = subprocess.run([python, "-c", IMPORT_CHECK], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def status(python: str = sys.executable) -> int:
    versions = installed(python)
    gpus = nvidia_gpus()
    say(f"NVIDIA GPU: {gpus[0] if gpus else 'none found'}")
    if not versions:
        say("PyTorch / transformers: not installed (Kitsune models cannot run; Whisper models do)")
        return 0
    say(f"PyTorch / transformers: {versions}")
    try:
        cuda = subprocess.run([python, "-c", "import torch; print(torch.cuda.is_available())"],
                              capture_output=True, text=True, timeout=600).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        cuda = "?"
    say(f"PyTorch sees a CUDA GPU: {cuda}")
    return 0


def torch_version(python: str = sys.executable) -> str | None:
    """PyTorch's version when it imports in `python`, else None: transformers alone missing is no
    reason to download PyTorch's 3 GB again."""
    try:
        done = subprocess.run([python, "-c", "import torch; print(torch.__version__)"],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def install(python: str = sys.executable, cpu: bool = False, force: bool = False) -> int:
    before = installed(python) or torch_version(python)
    if before and not force:
        say(f"PyTorch is installed already ({before})")
    else:
        gpus = [] if cpu else nvidia_gpus()
        if gpus:
            say(f"found {gpus[0]}: installing PyTorch's CUDA build (about 3 GB) ...")
        else:
            say("installing PyTorch's CPU build (" + ("--cpu" if cpu else "no NVIDIA GPU found") + ") ...")
        if subprocess.call(torch_command(python, bool(gpus))) != 0:
            say("installing PyTorch failed")
            return 1
    say("installing transformers and safetensors ...")
    if subprocess.call([python, "-m", "pip", "install", "-r", str(REQUIREMENTS)]) != 0:
        say(f"installing {REQUIREMENTS.name} failed")
        return 1
    after = installed(python)
    if not after:
        say("PyTorch or transformers still does not import; Kitsune models cannot run here")
        return 1
    say(f"ready: PyTorch / transformers {after}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Install PyTorch and transformers for Kitsune-Transcribe models")
    p.add_argument("--cpu", action="store_true", help="install PyTorch's CPU build even where there is an NVIDIA GPU")
    p.add_argument("--force", action="store_true", help="install again even when PyTorch imports already")
    p.add_argument("--status", action="store_true", help="say what is installed; install nothing")
    args = p.parse_args(argv)
    if args.status:
        return status()
    return install(cpu=args.cpu, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
