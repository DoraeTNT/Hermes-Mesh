#!/usr/bin/env python3
"""Build the HermesLauncher executable and its runtime module bundle."""

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET_DIR = os.path.abspath(os.path.join(ROOT, "..", "dist"))
MODULES_DIR = os.path.join(TARGET_DIR, "modules")
BUILD_DIR = os.path.join(ROOT, "build")
TCL_DIR = Path(sys.base_prefix) / "tcl" / "tcl8.6"
TK_DIR = Path(sys.base_prefix) / "tcl" / "tk8.6"

RUNTIME_MODULES = [
    "_enc_config.py",
    "crypto_loader.py",
    "runtime.py",
    "unified.py",
    "agent.py",
    "streamer.py",
]


def main():
    """Create a fresh distributable bundle. This function is intentionally explicit."""
    os.chdir(ROOT)
    print(f"ROOT       : {ROOT}")
    print(f"TARGET_DIR : {TARGET_DIR}")

    if os.path.isdir(TARGET_DIR):
        shutil.rmtree(TARGET_DIR)
    os.makedirs(MODULES_DIR)

    if not os.path.exists("_enc_config.py"):
        raise FileNotFoundError("_enc_config.py not found; download client/_enc_config.py first")
    if not (TCL_DIR / "init.tcl").is_file() or not (TK_DIR / "tk.tcl").is_file():
        raise FileNotFoundError("Python Tcl/Tk runtime files are missing; reinstall Python with tkinter support")

    print("\n[STEP 1/2] Copying modules...")
    for filename in RUNTIME_MODULES:
        if os.path.exists(filename):
            shutil.copy2(filename, os.path.join(MODULES_DIR, filename))
            print(f"  {filename}")
        else:
            print(f"  [MISS] {filename}")

    print("\n[STEP 2/2] Building exe...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "launcher.py",
            "--name=HermesLauncher",
            "--noconsole",
            "--onefile",
            "--clean",
            "--noconfirm",
            f"--workpath={BUILD_DIR}",
            "--hidden-import=PIL",
            "--hidden-import=PIL.Image",
            "--hidden-import=tkinter",
            "--hidden-import=tkinter.ttk",
            "--hidden-import=tkinter.scrolledtext",
            "--hidden-import=tkinter.filedialog",
            "--hidden-import=tkinter.messagebox",
            f"--add-data={TCL_DIR}{os.pathsep}_tcl_data",
            f"--add-data={TK_DIR}{os.pathsep}_tk_data",
        ],
        check=True,
    )

    exe_found = next(
        (
            candidate
            for candidate in glob.glob(os.path.join(ROOT, "dist", "**", "HermesLauncher.exe"), recursive=True)
            if os.path.isfile(candidate)
        ),
        None,
    )
    if not exe_found:
        raise FileNotFoundError("HermesLauncher.exe was not produced by PyInstaller")

    destination = os.path.join(TARGET_DIR, "HermesLauncher.exe")
    shutil.move(exe_found, destination)

    pyi_dist = os.path.join(ROOT, "dist")
    if os.path.isdir(pyi_dist):
        shutil.rmtree(pyi_dist)
    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    for spec in glob.glob(os.path.join(ROOT, "*.spec")):
        os.remove(spec)

    print(f"\n[OK] HermesLauncher.exe ({os.path.getsize(destination):,} bytes)")
    print(f"  {TARGET_DIR}")
    print(f"  ├─ HermesLauncher.exe")
    print(f"  └─ modules/ ({len(os.listdir(MODULES_DIR))} files)")


if __name__ == "__main__":
    main()
