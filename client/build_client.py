#!/usr/bin/env python3
"""
HermesLauncher — Build Script
Packages launcher.py into exe, copies runtime modules to dist/modules/
"""
import PyInstaller.__main__
import os, sys, shutil, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

TARGET_DIR = os.path.abspath(os.path.join(ROOT, "..", "dist"))
MODULES_DIR = os.path.join(TARGET_DIR, "modules")
BUILD_DIR = os.path.join(ROOT, "build")

print(f"ROOT       : {ROOT}")
print(f"TARGET_DIR : {TARGET_DIR}")

if os.path.isdir(TARGET_DIR):
    shutil.rmtree(TARGET_DIR)
os.makedirs(MODULES_DIR)

# ── 1. Encrypted config check ──
if not os.path.exists("_enc_config.py"):
    print("[ERROR] _enc_config.py not found!")
    print("        Download it from the server: client/_enc_config.py")
    sys.exit(1)

# ── 2. Copy runtime modules ──
print("\n[STEP 1/2] Copying modules...")
RUNTIME_MODULES = [
    "_enc_config.py",
    "crypto_loader.py",
    "runtime.py",
    "unified.py",
    "agent.py",
    "streamer.py",
]
for f in RUNTIME_MODULES:
    if os.path.exists(f):
        shutil.copy2(f, os.path.join(MODULES_DIR, f))
        print(f"  {f}")
    else:
        print(f"  [MISS] {f}")

# ── 3. PyInstaller ──
print(f"\n[STEP 2/2] Building exe...")
args = [
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
]
PyInstaller.__main__.run(args)

# ── 4. Find and move exe ──
exe_found = None
for pattern in [
    os.path.join(ROOT, "dist", "HermesLauncher.exe"),
    os.path.join(ROOT, "dist", "HermesLauncher", "HermesLauncher.exe"),
]:
    for f in glob.glob(pattern):
        if os.path.exists(f):
            exe_found = f
            break

if not exe_found:
    for root_dir, dirs, files in os.walk(ROOT):
        for f in files:
            if f.lower() == "hermeslauncher.exe":
                exe_found = os.path.join(root_dir, f)
                break

if exe_found:
    dest = os.path.join(TARGET_DIR, "HermesLauncher.exe")
    shutil.move(exe_found, dest)
    print(f"\n[OK] HermesLauncher.exe ({os.path.getsize(dest):,} bytes)")
else:
    print(f"\n[ERROR] HermesLauncher.exe not found!")
    sys.exit(1)

# ── 5. Cleanup ──
pyi_dist = os.path.join(ROOT, "dist")
if os.path.isdir(pyi_dist):
    shutil.rmtree(pyi_dist)
if os.path.isdir(BUILD_DIR):
    shutil.rmtree(BUILD_DIR)
for spec in glob.glob(os.path.join(ROOT, "*.spec")):
    os.remove(spec)

print(f"  {TARGET_DIR}")
print(f"  ├── HermesLauncher.exe")
print(f"  └── modules/ ({len(os.listdir(MODULES_DIR))} files)")
print(f"\nDone!")
