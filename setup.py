#!/usr/bin/env python3
"""VidDrop setup: creates .venv, installs requirements.txt into it, adds a desktop shortcut.

Usage:  python setup.py [--no-shortcut]
The shortcut opens VidDrop in its own app window (no browser tabs).
Re-run any time to update yt-dlp and the other dependencies.
"""
import os
import platform
import shlex
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
ASSETS = ROOT / "assets"
APP = ROOT / "app.py"


def find_chromium():
    names = ["microsoft-edge", "google-chrome", "chromium", "chromium-browser", "brave-browser", "chrome", "msedge"]
    paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    return next((shutil.which(n) for n in names if shutil.which(n)), None) or next((p for p in paths if os.path.exists(p)), None)


def step(msg):
    print(f"\n==> {msg}")


def die(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


# ---------- icon (pure Python, no dependencies) ----------
def _seg_dist(px, py, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def render_png(size=256):
    s = size / 32.0
    segs = [((16, 7), (16, 19)), ((11, 14), (16, 19)), ((16, 19), (21, 14)), ((9, 24), (23, 24))]
    bg, fg = (29, 78, 216), (255, 255, 255)
    raw = bytearray()
    for j in range(size):
        raw.append(0)
        y = (j + 0.5) / s
        for i in range(size):
            x = (i + 0.5) / s
            qx, qy = abs(x - 16) - 8, abs(y - 16) - 8  # rounded square, radius 8
            d = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - 8
            a = min(1.0, max(0.0, 0.5 - d * s))
            if a <= 0:
                raw += b"\0\0\0\0"
                continue
            ds = min(_seg_dist(x, y, p, q) for p, q in segs) - 1.3  # stroke width 2.6
            f = min(1.0, max(0.0, 0.5 - ds * s))
            raw += bytes(int(bg[k] + (fg[k] - bg[k]) * f) for k in range(3)) + bytes([int(255 * a)])

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def make_icons():
    ASSETS.mkdir(exist_ok=True)
    png, ico = ASSETS / "viddrop.png", ASSETS / "viddrop.ico"
    if not (png.exists() and ico.exists()):
        data = render_png(256)
        png.write_bytes(data)
        # ICO container holding one 256x256 PNG image
        ico.write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22) + data)
    return png, ico


# ---------- shortcuts ----------
def shortcut_windows(ico, windowed):
    ps = r"""
$ws = New-Object -ComObject WScript.Shell
$link = Join-Path ([Environment]::GetFolderPath('Desktop')) 'VidDrop.lnk'
$s = $ws.CreateShortcut($link)
$s.TargetPath = $env:VD_PY
$s.Arguments = '"' + $env:VD_APP + '" ' + $env:VD_FLAG
$s.WorkingDirectory = $env:VD_ROOT
$s.IconLocation = $env:VD_ICON
$s.Description = 'Start VidDrop'
$s.Save()
Write-Output $link
"""
    pyw = VENV / "Scripts/pythonw.exe"  # no console window
    env = dict(os.environ, VD_PY=str(pyw if windowed and pyw.exists() else VENV_PY), VD_APP=str(APP),
               VD_ROOT=str(ROOT), VD_ICON=str(ico), VD_FLAG="--app" if windowed else "--open")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "PowerShell failed")
    return r.stdout.strip()


def desktop_dir():
    if not IS_WIN and not IS_MAC and shutil.which("xdg-user-dir"):
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    return Path.home() / "Desktop"


def shortcut_mac(windowed):
    desk = desktop_dir()
    desk.mkdir(parents=True, exist_ok=True)
    f = desk / "VidDrop.command"
    f.write_text(f"#!/bin/bash\ncd {shlex.quote(str(ROOT))} && exec {shlex.quote(str(VENV_PY))} app.py {'--app' if windowed else '--open'}\n")
    f.chmod(0o755)
    return str(f)


def shortcut_linux(png, windowed):
    q = lambda p: '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'
    entry = ("[Desktop Entry]\nType=Application\nName=VidDrop\nComment=Start VidDrop\n"
             f"Exec={q(VENV_PY)} {q(APP)} {'--app' if windowed else '--open'}\nPath={ROOT}\nIcon={png}\n"
             f"Terminal={'false' if windowed else 'true'}\n"
             "Categories=Network;AudioVideo;\n")
    made = []
    for d in (desktop_dir(), Path.home() / ".local/share/applications"):
        d.mkdir(parents=True, exist_ok=True)
        f = d / "viddrop.desktop"
        f.write_text(entry)
        f.chmod(0o755)
        if shutil.which("gio"):  # lets GNOME launch it without the "untrusted" prompt
            subprocess.run(["gio", "set", str(f), "metadata::trusted", "true"], capture_output=True)
        made.append(str(f))
    return " and ".join(made)


# ---------- main ----------
def main():
    if sys.version_info < (3, 9):
        die(f"Python 3.9 or newer is required (you have {platform.python_version()}).")

    step("Creating virtual environment (.venv)")
    if VENV_PY.exists():
        print("Already exists - reusing it.")
    else:
        try:
            subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
        except subprocess.CalledProcessError:
            shutil.rmtree(VENV, ignore_errors=True)
            hint = " On Debian/Ubuntu run: sudo apt install python3-venv" if not IS_WIN and not IS_MAC else ""
            die("Could not create the virtual environment." + hint)

    step("Installing requirements.txt into .venv")
    pip = [str(VENV_PY), "-m", "pip", "--disable-pip-version-check"]
    subprocess.call(pip + ["install", "--quiet", "--upgrade", "pip"])  # best effort
    if subprocess.call(pip + ["install", "--upgrade", "-r", str(ROOT / "requirements.txt")]) != 0:
        die("Installing requirements failed. Check your internet connection and try again.")

    has_webview = subprocess.call([str(VENV_PY), "-c", "import webview"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

    step("Checking tools")
    if shutil.which("ffmpeg"):
        print("ffmpeg: found")
    else:
        cmd = "winget install ffmpeg" if IS_WIN else "brew install ffmpeg" if IS_MAC else "sudo apt install ffmpeg"
        print(f"ffmpeg: NOT FOUND - needed for merging and MP3/M4A. Install it with:  {cmd}")
    if not any(shutil.which(x) for x in ("deno", "node", "bun")):
        print("JavaScript runtime: none found - if YouTube downloads fail, install Deno or Node.js "
              "(yt-dlp uses one to read YouTube).")

    if "--no-shortcut" not in sys.argv:
        step("Creating desktop shortcut")
        try:
            png, ico = make_icons()
            windowed = has_webview or bool(find_chromium())
            print("Window mode: " + ("native app window" if has_webview else "Edge/Chrome app window" if windowed
                                     else "not available - will open in your browser"))
            where = (shortcut_windows(ico, windowed) if IS_WIN else shortcut_mac(windowed) if IS_MAC
                     else shortcut_linux(png, windowed))
            print(f"Created: {where}")
        except Exception as e:  # never fail the whole setup over a shortcut
            print(f"Could not create the shortcut ({e}).")

    print("\nDone. Start VidDrop with the desktop shortcut, or run:")
    print(f"    {VENV_PY} app.py --app")


if __name__ == "__main__":
    main()
