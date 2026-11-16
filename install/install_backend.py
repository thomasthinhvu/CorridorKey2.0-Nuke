#!/usr/bin/env python3
"""Install CorridorKey 2.0 (the backend) and the CorridorKey Nuke plugin.

The backend gets its own private conda environment, so it never depends on the Python (or ffmpeg)
already on the machine:

  1. Downloads the official CorridorKey 2.0 source (pinned to the version this plugin was tested with).
  2. Installs Miniforge (conda-forge's free conda) privately inside the backend folder, and creates a
     Python 3.12 environment with ffmpeg in it.
  3. Installs CUDA PyTorch and CorridorKey's requirements into that environment.
  4. Downloads the CorridorKey weights and the three foundation models from their original publishers
     (about 5 GB) using CorridorKey's own scripts/download_weights.py.
  5. Copies the Nuke plugin into your .nuke folder, points it at the environment and adds one line to
     .nuke/init.py.

Nothing from CorridorKey is redistributed by this repository - everything is fetched from the official sources.
Normally you run install_windows.bat / install_linux.sh, which bootstrap Miniforge and then run this file.

  install_windows.bat                                    # backend in ~/CorridorKey2.0, plugin in ~/.nuke
  install_windows.bat --backend D:/CorridorKey2.0
  python install_backend.py --skip-backend --backend D:/CorridorKey2.0   # plugin only, existing backend
Re-running is safe: finished steps are skipped.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

UPSTREAM_REF = "485f5e9ab759ead6138cc990776184b59d6ea971"   # CorridorKey 2.0 commit tested with this plugin
UPSTREAM_ZIP = "https://github.com/nikopueringer/CorridorKey2.0/archive/{ref}.zip"
ENV_PYTHON_VERSION = "3.12"     # CorridorKey 2.0 needs >= 3.11 (scipy >= 1.16) and numpy < 2 (no 3.13 wheels)
TORCH = ["torch==2.9.1", "torchvision==0.24.1"]
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
MINIFORGE = "https://github.com/conda-forge/miniforge/releases/latest/download/" + (
    "Miniforge3-Windows-x86_64.exe" if os.name == "nt" else "Miniforge3-Linux-x86_64.sh")
PLUGIN_SRC = Path(__file__).resolve().parents[1] / "CorridorKey"
INIT_MARK = "# CorridorKey 2.0 for Nuke"
WIN = os.name == "nt"


def step(msg):
    print(f"\n=== {msg}", flush=True)


def run(cmd, **kw):
    print("  >", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def download(url, dst: Path):
    print(f"  downloading {url}", flush=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct // 10 != last:
                    last = pct // 10
                    print(f"    {pct}%  ({done >> 20} / {total >> 20} MB)", flush=True)
    tmp.replace(dst)


# --- conda locations -----------------------------------------------------------------------------

def conda_root(backend: Path) -> Path:
    """Private Miniforge location. conda can't be installed into a path with spaces, so in that case it goes to
    the root of the same drive (Windows) or the home folder (Linux). Must match install_windows.ps1 / install_linux.sh."""
    if " " not in str(backend):
        return backend / "_conda"
    if WIN:
        return Path(backend.drive + "\\") / "CorridorKey_conda"
    return Path.home() / ".corridorkey_conda"


def conda_exe(backend: Path) -> Path:
    return conda_root(backend) / ("Scripts/conda.exe" if WIN else "bin/conda")


def env_dir(backend: Path) -> Path:
    return conda_root(backend) / "envs" / "corridorkey"


def env_python(backend: Path) -> Path:
    return env_dir(backend) / ("python.exe" if WIN else "bin/python")


def env_ffmpeg(backend: Path) -> Path:
    return env_dir(backend) / ("Library/bin/ffmpeg.exe" if WIN else "bin/ffmpeg")


def env_vars(backend: Path) -> dict:
    """Environment for running the env's python without 'conda activate' (DLL folders on PATH)."""
    e = env_dir(backend)
    extra = [e, e / "Library/mingw-w64/bin", e / "Library/usr/bin", e / "Library/bin", e / "Scripts"] if WIN \
        else [e / "bin"]
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        env.pop(var, None)
    env["PATH"] = os.pathsep.join(str(p) for p in extra) + os.pathsep + env.get("PATH", "")
    env["PYTHONNOUSERSITE"] = "1"
    return env


def python_version(py: Path, env: dict) -> str:
    try:
        out = subprocess.run([str(py), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, env=env, timeout=60)
        return out.stdout.strip()
    except Exception:
        return ""


# --- steps ---------------------------------------------------------------------------------------

MIN_DRIVER_MAJOR = 570   # the cu128 PyTorch wheels need a driver that supports CUDA 12.8 (approximate)


def check_gpu():
    """Warn (never block) about the GPU model and driver version that CorridorKey needs."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
    except Exception as exc:
        print(f"  WARNING: could not query the GPU with nvidia-smi: {exc}")
        return
    if not out:
        print("  WARNING: nvidia-smi reported no GPU.")
        return
    name, _, driver = out[0].partition(",")
    name, driver = name.strip(), driver.strip()
    print(f"  GPU: {name} | driver {driver}")
    if "RTX" not in name.upper():
        print("  WARNING: CorridorKey is tested on NVIDIA RTX 30-series or newer; this GPU may be too old.")
    try:
        if int(driver.split(".")[0]) < MIN_DRIVER_MAJOR:
            print(f"  WARNING: driver {driver} is older than {MIN_DRIVER_MAJOR}; update the NVIDIA driver "
                  "before running CorridorKey.")
    except ValueError:
        pass


def checks(backend: Path):
    step("Checking the machine")
    if not (WIN or sys.platform.startswith("linux")):
        sys.exit("CorridorKey 2.0 needs an NVIDIA CUDA GPU: only Windows and Linux are supported.")
    if not shutil.which("nvidia-smi"):
        print("  WARNING: nvidia-smi not found. CorridorKey needs an NVIDIA RTX GPU (30-series or newer) "
              "with a current driver.")
    else:
        check_gpu()
    probe = backend if backend.exists() else backend.anchor or backend.parent
    try:
        free_gb = shutil.disk_usage(probe).free / 1e9
        print(f"  free space on {Path(probe).anchor or probe}: {free_gb:.0f} GB")
        if free_gb < 15:
            print("  WARNING: about 15 GB is needed (Python env + PyTorch ~6 GB, models ~5 GB, downloads).")
    except Exception:
        pass
    if " " in str(backend):
        print(f"  note: the folder path contains spaces, which conda can't use - its Python environment goes to "
              f"{conda_root(backend)} instead (the code and models stay in {backend}).")


def install_source(backend: Path, ref: str):
    step(f"CorridorKey 2.0 source -> {backend}")
    if (backend / "infer.py").is_file():
        print("  already present")
        return
    backend.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "corridorkey.zip"
        download(UPSTREAM_ZIP.format(ref=ref), zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(td)
        top = next(p for p in Path(td).iterdir() if p.is_dir())
        for item in top.iterdir():
            target = backend / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
    if not (backend / "infer.py").is_file():
        sys.exit("Could not unpack the CorridorKey 2.0 source.")
    print("  done")


def install_miniforge(backend: Path):
    step(f"Miniforge (private conda) -> {conda_root(backend)}")
    if conda_exe(backend).is_file():
        print("  already present")
        return
    with tempfile.TemporaryDirectory() as td:
        installer = Path(td) / MINIFORGE.rsplit("/", 1)[1]
        download(MINIFORGE, installer)
        root = conda_root(backend)
        print("  installing (takes a minute)...", flush=True)
        if WIN:
            # NSIS: /D= must be last and unquoted, so pass a raw command line
            cmd = (f'"{installer}" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /NoRegistry=1 '
                   f'/NoShortcuts=1 /S /D={root}')
            subprocess.run(cmd, check=True)
        else:
            run(["bash", installer, "-b", "-p", root])
    if not conda_exe(backend).is_file():
        sys.exit(f"Miniforge did not install into {conda_root(backend)}.")
    print("  done")


def create_env(backend: Path):
    step(f"Python {ENV_PYTHON_VERSION} + ffmpeg environment -> {env_dir(backend)}")
    py, env = env_python(backend), env_vars(backend)
    have = python_version(py, env) if py.is_file() else ""
    if have == ENV_PYTHON_VERSION and env_ffmpeg(backend).is_file():
        print(f"  already present (Python {have})")
        return
    if env_dir(backend).exists():
        print(f"  replacing the existing environment (Python {have or 'unknown'})")
        shutil.rmtree(env_dir(backend), ignore_errors=True)
    run([conda_exe(backend), "create", "-y", "-p", env_dir(backend), "--override-channels", "-c", "conda-forge",
         f"python={ENV_PYTHON_VERSION}", "ffmpeg", "pip"])
    have = python_version(py, env)
    if have != ENV_PYTHON_VERSION:
        sys.exit(f"The environment's Python is '{have}', expected {ENV_PYTHON_VERSION}.")
    print(f"  Python {have} OK")


def install_packages(backend: Path):
    py, env = env_python(backend), env_vars(backend)
    step("PyTorch (CUDA 12.8) and requirements - large download, this can take a while")
    run([py, "-m", "pip", "install", "--no-cache-dir", *TORCH, "--index-url", TORCH_INDEX], env=env)
    run([py, "-m", "pip", "install", "--no-cache-dir", "-r", backend / "requirements.txt"], env=env)


def download_weights(backend: Path):
    py = env_python(backend)
    step("Model weights (about 5 GB) from the official sources")
    env = dict(env_vars(backend), HF_HOME=str(backend / "_cache" / "hf"), HF_HUB_DISABLE_XET="1")
    run([py, backend / "scripts" / "download_weights.py"], cwd=backend, env=env)


def gpu_check(backend: Path):
    step("GPU check")
    run([env_python(backend), "-c",
         "import torch; ok = torch.cuda.is_available(); "
         "print('  torch', torch.__version__, '| CUDA:', ok, '|', torch.cuda.get_device_name(0) if ok else 'no GPU')"],
        env=env_vars(backend))


def install_plugin(backend: Path, nuke_dir: Path):
    step(f"Nuke plugin -> {nuke_dir / 'CorridorKey'}")
    dst = nuke_dir / "CorridorKey"
    dst.mkdir(parents=True, exist_ok=True)
    for f in PLUGIN_SRC.glob("*.py"):
        shutil.copy2(f, dst / f.name)
        print(f"  copied {f.name}")
    cfg_path = dst / "corridorkey_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    cfg["backend"] = backend.resolve().as_posix()
    if env_python(backend).is_file():
        cfg["python"] = env_python(backend).resolve().as_posix()
        cfg["ffmpeg"] = env_ffmpeg(backend).resolve().as_posix()
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    print(f"  backend: {cfg['backend']}")
    if "python" in cfg:
        print(f"  python : {cfg['python']}")

    init_py = nuke_dir / "init.py"
    text = init_py.read_text(encoding="utf-8", errors="replace") if init_py.is_file() else ""
    # absolute path: relative pluginAddPath entries are resolved against other plugin folders by some tools
    line = f"nuke.pluginAddPath(r'{dst.resolve().as_posix()}')  {INIT_MARK}"
    if INIT_MARK in text:
        print("  init.py already loads CorridorKey")
    else:
        if init_py.is_file():
            backup = init_py.with_name(f"init.py.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}")
            shutil.copy2(init_py, backup)
            print(f"  backed up init.py -> {backup.name}")
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        header = "" if "import nuke" in text else "import nuke\n"
        with open(init_py, "a", encoding="utf-8") as f:
            f.write(f"{prefix}\n{header}{line}\n")
        print("  added CorridorKey to init.py")


def main():
    ap = argparse.ArgumentParser(description="Install CorridorKey 2.0 and the CorridorKey Nuke plugin.")
    ap.add_argument("--backend", default=str(Path.home() / "CorridorKey2.0"),
                    help="where CorridorKey 2.0 is (or will be) installed - needs ~15 GB (default ~/CorridorKey2.0)")
    ap.add_argument("--nuke-dir", default=str(Path.home() / ".nuke"), help="your .nuke folder (default ~/.nuke)")
    ap.add_argument("--skip-backend", action="store_true", help="only install the Nuke plugin")
    ap.add_argument("--skip-plugin", action="store_true", help="only install the backend")
    ap.add_argument("--ref", default=UPSTREAM_REF, help="CorridorKey 2.0 git commit to use")
    args = ap.parse_args()

    backend = Path(args.backend).expanduser().resolve()
    checks(backend)
    if not args.skip_backend:
        install_source(backend, args.ref)
        install_miniforge(backend)
        create_env(backend)
        install_packages(backend)
        download_weights(backend)
        gpu_check(backend)
        if (backend / "venv").is_dir():
            print(f"\n  note: {backend / 'venv'} is from an older install and is no longer used - you can delete it.")
    elif not (backend / "infer.py").is_file():
        sys.exit(f"--skip-backend given but no CorridorKey 2.0 found at {backend}")
    if not args.skip_plugin:
        install_plugin(backend, Path(args.nuke_dir).expanduser())
    step("Done. Restart Nuke: Nodes toolbar > CorridorKey > CorridorKey 2.0")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(f"\nA step failed (exit {exc.returncode}). Fix the error above and run the installer again - "
                 "finished steps are skipped.")
    except Exception as exc:  # network errors etc.
        sys.exit(f"\nThe installer stopped: {exc}\nRun it again - finished steps are skipped.")
