"""VidDrop - minimal YouTube downloader (Flask + yt-dlp + ffmpeg)."""
import copy
import os
import re
import shutil
import atexit
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file
from yt_dlp.postprocessor import PostProcessor

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # very old yt-dlp
    class DownloadCancelled(Exception):
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

MAX_DURATION = int(os.environ.get("MAX_DURATION", 3 * 60 * 60))  # seconds
MAX_JOBS = int(os.environ.get("MAX_JOBS", 3))                    # parallel downloads
FRAGMENTS = int(os.environ.get("FRAGMENTS", 8))                  # parallel fragments per download
TTL = 600          # seconds a finished file stays available
INFO_TTL = 300     # seconds video details are cached (stream URLs stay valid for hours)
INFO_MAX = 64      # max cached videos
SWEEP_EVERY = 15   # seconds between cleanup passes

WATCH = "https://www.youtube.com/watch?v="
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_FORMATS = {"mp4", "webm", "mkv"}
AUDIO_FORMATS = {"mp3", "m4a", "opus", "wav"}
AUDIO_BITRATES = {"128", "192", "256", "320"}
FINAL_STATES = {"ready", "error", "cancelled"}
PUBLIC = ("state", "stage", "percent", "done", "total", "speed", "eta", "name", "size", "error")

_slots = threading.BoundedSemaphore(MAX_JOBS)
_lock = threading.Lock()
_last_sweep = 0.0
JOBS = {}   # job_id -> job dict (see new_job)
_INFO = {}  # video_id -> (timestamp, raw yt-dlp info)


class UserError(Exception):
    """An error whose message is safe to show to the user."""


# ---------- helpers ----------
def valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in ALLOWED_HOSTS


def video_id(url: str):
    """Return the 11-char video id of a single-video YouTube link, else None."""
    p = urlparse(url)
    if (p.hostname or "").lower() == "youtu.be":
        cand = p.path.lstrip("/").split("/")[0]
    else:
        q = parse_qs(p.query).get("v")
        if q:
            cand = q[0]
        else:
            m = re.match(r"^/(?:shorts|live|embed|v)/([^/?#]+)", p.path)
            cand = m.group(1) if m else ""
    return cand if re.fullmatch(r"[A-Za-z0-9_-]{11}", cand) else None


def check_link(url: str):
    """Return (video_id, error_message)."""
    if not valid_url(url):
        return None, "Enter a valid YouTube link."
    vid = video_id(url)
    if not vid:
        return None, "That link doesn't point to a single video."
    return vid, None


def clean_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    return msg.strip().splitlines()[-1][:200] if msg.strip() else "Something went wrong."


def pick_thumb(d: dict):
    if d.get("thumbnail"):
        return d["thumbnail"]
    thumbs = [t for t in d.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return None
    best = max(thumbs, key=lambda t: (t.get("preference") if t.get("preference") is not None else -1,
                                      t.get("width") or -1, t.get("height") or -1))
    return best["url"]


def _clone(d):
    try:
        return copy.deepcopy(d)
    except Exception:
        return d


def get_info(vid: str, ydl=None):
    """Raw (unprocessed) yt-dlp info for a video.

    Cached, so /api/info and the download that follows it hit YouTube only once
    (this removes several seconds from every download).
    """
    now = time.time()
    with _lock:
        hit = _INFO.get(vid)
    if hit and now - hit[0] < INFO_TTL:
        return _clone(hit[1])
    own = ydl is None
    if own:
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                                "skip_download": True, "socket_timeout": 20})
    try:
        d = ydl.extract_info(WATCH + vid, download=False, process=False)
    finally:
        if own:
            ydl.close()
    with _lock:
        if len(_INFO) >= INFO_MAX:
            del _INFO[min(_INFO, key=lambda k: _INFO[k][0])]
        _INFO[vid] = (now, d)
    return _clone(d)


def sweep():
    global _last_sweep
    now = time.time()
    if now - _last_sweep < SWEEP_EVERY:
        return
    _last_sweep = now
    dead = []
    with _lock:
        for jid, j in list(JOBS.items()):
            if j["state"] in FINAL_STATES and now - j["t"] > TTL:
                dead.append(JOBS.pop(jid))
        for k, (t, _) in list(_INFO.items()):
            if now - t > INFO_TTL:
                del _INFO[k]
    for j in dead:  # disk work outside the lock
        shutil.rmtree(j["dir"], ignore_errors=True)


@app.before_request
def _before():
    sweep()


@app.after_request
def _no_cache_api(resp):
    if request.path.startswith("/api/") and not request.path.startswith("/api/file/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- job plumbing ----------
def new_job(tmp: str) -> dict:
    job = {
        "id": uuid.uuid4().hex, "dir": tmp, "path": None,
        "state": "starting", "stage": "Starting", "percent": None,
        "done": 0, "total": 0, "speed": None, "eta": None,
        "name": None, "size": None, "error": None,
        "cancel": False, "expected": 0, "streams": {}, "_tick": 0.0, "t": time.time(),
    }
    with _lock:
        JOBS[job["id"]] = job
    return job


def _set(job: dict, **kw):
    with _lock:
        job.update(kw)
        job["t"] = time.time()


class _Plan(PostProcessor):
    """Runs just before download starts to learn the total size of the chosen streams."""

    def __init__(self, job):
        super().__init__(None)
        self.job = job

    def run(self, info):
        fmts = info.get("requested_formats") or [info]
        total = sum(int(f.get("filesize") or f.get("filesize_approx") or 0) for f in fmts)
        with _lock:
            self.job["expected"] = total
        return [], info


def _on_progress(job: dict, kind: str, d: dict):
    if job["cancel"]:
        raise DownloadCancelled()
    st = d.get("status")
    if st not in ("downloading", "finished"):
        return
    now = time.time()
    if st == "downloading" and now - job["_tick"] < 0.2:
        return
    done = d.get("downloaded_bytes") or 0
    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
    if st == "finished":
        total = total or done
        done = total
    vcodec = (d.get("info_dict") or {}).get("vcodec")
    stage = "Downloading video" if kind == "video" and vcodec not in (None, "none") else "Downloading audio"
    with _lock:
        job["_tick"] = now
        job["streams"][d.get("filename")] = (done, max(total, done))
        got = sum(s[0] for s in job["streams"].values())
        seen = sum(s[1] for s in job["streams"].values())
        expected = max(job["expected"], seen)
        pct = job["percent"] or 0.0
        if expected:
            pct = max(pct, min(99.0, got * 100.0 / expected))  # never moves backwards
        speed = d.get("speed") if st == "downloading" else job["speed"]
        job.update(
            state="downloading", stage=stage, percent=round(pct, 1) if expected else None,
            done=got, total=expected, speed=speed,
            eta=(expected - got) / speed if expected and speed else None, t=now,
        )


def _on_pp(job: dict, fmt: str, d: dict):
    if d.get("status") != "started":
        return
    labels = {
        "Merger": "Merging video and audio",
        "ExtractAudio": f"Converting to {fmt.upper()}" if fmt in ("mp3", "wav") else f"Packaging as {fmt.upper()}",
        "VideoRemuxer": "Finishing up",
        "VideoConvertor": "Finishing up",
    }
    label = labels.get(d.get("postprocessor"))
    if label:
        _set(job, state="processing", stage=label, percent=100.0, speed=None, eta=None)


def _ydl_opts(job: dict, kind: str, fmt: str, quality: str) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "outtmpl": os.path.join(job["dir"], "%(id)s.%(ext)s"),
        "concurrent_fragment_downloads": FRAGMENTS,
        "retries": 5, "fragment_retries": 5, "socket_timeout": 20,
        "progress_hooks": [lambda d: _on_progress(job, kind, d)],
        "postprocessor_hooks": [lambda d: _on_pp(job, fmt, d)],
    }
    if kind == "video":
        h = "" if quality == "best" else f"[height<={quality}]"
        if fmt == "mp4":
            sel = f"bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}"
        elif fmt == "webm":
            sel = f"bv*{h}[ext=webm]+ba[ext=webm]/bv*{h}+ba/b{h}"
        else:
            sel = f"bv*{h}+ba/b{h}"
        opts.update(format=sel, merge_output_format=fmt)
    else:
        # M4A / Opus: pick a stream already in that codec so ffmpeg only repackages (no re-encode).
        if fmt == "m4a":
            sel = "bestaudio[ext=m4a]/bestaudio/best"
        elif fmt == "opus":
            sel = "bestaudio[acodec=opus]/bestaudio/best"
        else:
            sel = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt != "wav":
            pp["preferredquality"] = quality
        opts.update(format=sel, postprocessors=[pp])
    return opts


def _run(job: dict, vid: str, kind: str, fmt: str, quality: str):
    tmp = job["dir"]
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(job, kind, fmt, quality)) as ydl:
            ydl.add_post_processor(_Plan(job), when="before_dl")
            _set(job, stage="Reading video details")
            meta = get_info(vid, ydl)
            if meta.get("is_live"):
                raise UserError("Live streams are not supported.")
            if (meta.get("duration") or 0) > MAX_DURATION:
                raise UserError(f"Video is longer than {MAX_DURATION // 3600} hours.")
            if job["cancel"]:
                raise DownloadCancelled()
            _set(job, state="downloading", stage="Starting download")
            ydl.process_ie_result(meta, download=True)

        if job["cancel"]:  # cancelled while ffmpeg was running
            raise DownloadCancelled()
        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise UserError("Download failed.")
        match = [f for f in files if f.endswith("." + fmt)]
        fname = match[0] if match else max(files, key=lambda f: os.path.getsize(os.path.join(tmp, f)))
        path = os.path.join(tmp, fname)
        title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", meta.get("title") or "download").strip()[:120] or "download"
        _set(job, state="ready", stage="Ready", percent=100.0, speed=None, eta=None, path=path,
             name=f"{title}{os.path.splitext(fname)[1]}", size=os.path.getsize(path))
    except DownloadCancelled:
        shutil.rmtree(tmp, ignore_errors=True)
        _set(job, state="cancelled", stage="Cancelled", speed=None, eta=None)
    except UserError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        _set(job, state="error", stage="Failed", error=str(e))
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        _set(job, state="error", stage="Failed", error=clean_error(e))
    except Exception:
        app.logger.exception("download job failed")
        shutil.rmtree(tmp, ignore_errors=True)
        _set(job, state="error", stage="Failed", error="Unexpected error. Is ffmpeg installed?")
    finally:
        _slots.release()


# ---------- routes ----------
@app.get("/")
def index():
    return send_file(os.path.join(BASE, "index.html"))


@app.post("/api/info")
def info():
    url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
    vid, err = check_link(url)
    if err:
        return jsonify(error=err), 400
    try:
        d = get_info(vid)
    except yt_dlp.utils.DownloadError as e:
        return jsonify(error=clean_error(e)), 422
    if d.get("is_live"):
        return jsonify(error="Live streams are not supported."), 422
    heights = sorted(
        {f["height"] for f in d.get("formats") or []
         if f.get("vcodec") not in (None, "none") and f.get("height") and f.get("url") and not f.get("has_drm")},
        reverse=True,
    )
    return jsonify(
        title=d.get("title"),
        uploader=d.get("uploader"),
        duration=d.get("duration"),
        thumbnail=pick_thumb(d),
        heights=heights,
    )


@app.post("/api/prepare")
def prepare():
    """Validate, then start the download in the background and return a job id."""
    body = request.get_json(silent=True) or {}
    url = str(body.get("url", "")).strip()
    kind = body.get("type", "video")
    fmt = str(body.get("format", "mp4")).lower()
    quality = str(body.get("quality", "best"))

    vid, err = check_link(url)
    if err:
        return jsonify(error=err), 400
    if kind == "video":
        if fmt not in VIDEO_FORMATS or not (quality == "best" or quality.isdigit()):
            return jsonify(error="Invalid options."), 400
    elif kind == "audio":
        if fmt not in AUDIO_FORMATS or quality not in AUDIO_BITRATES:
            return jsonify(error="Invalid options."), 400
    else:
        return jsonify(error="Invalid options."), 400

    if not _slots.acquire(blocking=False):
        return jsonify(error="Server is busy. Try again in a moment."), 503

    job = new_job(tempfile.mkdtemp(prefix="viddrop_"))
    try:
        threading.Thread(target=_run, args=(job, vid, kind, fmt, quality), daemon=True).start()
    except Exception:
        _slots.release()
        shutil.rmtree(job["dir"], ignore_errors=True)
        with _lock:
            JOBS.pop(job["id"], None)
        raise
    return jsonify(id=job["id"])


@app.get("/api/status/<jid>")
def status(jid):
    with _lock:
        j = JOBS.get(jid)
        out = {k: j[k] for k in PUBLIC} if j else None
    if out is None:
        return jsonify(state="expired", error="This download has expired. Please start again."), 404
    return jsonify(out)


@app.post("/api/cancel/<jid>")
def cancel(jid):
    with _lock:
        j = JOBS.get(jid)
        if j and j["state"] not in FINAL_STATES:
            j["cancel"] = True
    return jsonify(ok=True)


@app.get("/api/file/<jid>")
def file(jid):
    with _lock:
        j = JOBS.get(jid)
        if j and j["state"] == "ready":
            j["t"] = time.time()  # keep it alive while it's being fetched
            path, name = j["path"], j["name"]
        else:
            path = None
    if not path:
        return "This link has expired. Please start the download again.", 404
    return send_file(path, as_attachment=True, download_name=name, conditional=True, max_age=0)


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _cleanup():
    for j in list(JOBS.values()):
        shutil.rmtree(j["dir"], ignore_errors=True)


atexit.register(_cleanup)


def _find_chromium():
    """Edge/Chrome/Chromium/Brave, used for a chromeless app window."""
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
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return next((p for p in paths if os.path.exists(p)), None)


def run_desktop(host: str, port: int):
    """Run VidDrop like a desktop app: server in the background, UI in its own window."""
    url = f"http://127.0.0.1:{port}"
    if not _port_open(port):
        threading.Thread(target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
                         daemon=True).start()
        for _ in range(100):
            if _port_open(port):
                break
            time.sleep(0.1)
        else:
            sys.exit("VidDrop server did not start.")
    try:  # 1) native window (Windows: WebView2, macOS: WebKit)
        import webview
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True  # lets the window save files
        except Exception:
            pass
        webview.create_window("VidDrop", url, width=1180, height=860, min_size=(480, 600))
        webview.start()
        return
    except Exception:
        pass
    exe = _find_chromium()  # 2) Edge/Chrome in app mode: own window, no tabs or address bar
    if exe:
        profile = os.path.join(BASE, ".app-profile")  # separate profile so this call waits until the window closes
        subprocess.run([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                        "--no-default-browser-check", "--window-size=1180,860"])
        return
    webbrowser.open(url)  # 3) last resort: normal browser tab
    threading.Event().wait()


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found - merging and MP3 conversion will fail.")
    if "--app" in sys.argv:  # desktop-app window (used by the shortcut)
        run_desktop(host, port)
        sys.exit(0)
    if "--open" in sys.argv:  # open in the normal browser
        url = f"http://127.0.0.1:{port}"
        if _port_open(port):  # already running: just open the page
            webbrowser.open(url)
            sys.exit(0)
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    print(f"VidDrop is running on port {port}. Close this window or press Ctrl+C to stop.")
    app.run(host=host, port=port, threaded=True)
