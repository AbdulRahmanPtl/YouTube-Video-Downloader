# VidDrop

A free, self-hosted YouTube video and audio downloader that runs on your own computer. Paste a link, pick a format and quality, and save the file. It opens as a desktop app, with no browser tabs and no accounts.

Built with Flask, yt-dlp and ffmpeg.

> **Use responsibly.** Only download content you own, that is in the public domain or licensed for reuse, or that you have permission to save. See [Legal](#legal).

## Features

- **Video downloads:** MP4, WebM and MKV, in any resolution the video offers, or the best available.
- **Audio extraction:** MP3, M4A, Opus and WAV. M4A and Opus are repackaged without re-encoding, so they are fast and lossless.
- **Live progress:** real percentage, downloaded size, speed and time remaining, plus clear "Merging" and "Converting" stages.
- **Cancel anytime** while a download is running.
- **Desktop-app experience:** the shortcut opens VidDrop in its own window (no browser UI, no console) and closing the window stops it.
- **Dark and light mode:** smooth animated switch, remembers your choice and follows your system setting by default.
- **One-step setup:** a single script creates a virtual environment, installs everything and adds a desktop shortcut.
- **Fast backend:** video details are fetched once and reused, and fragmented streams download in parallel.
- **Private:** no accounts, no tracking. Temporary files are deleted automatically after 10 minutes.
- **Responsive UI:** also usable from a phone on the same Wi-Fi (see [Use it on your phone](#use-it-on-your-phone-same-wi-fi)).

## Requirements

| Requirement | Notes |
|---|---|
| **Python 3.9 or newer** | Windows: install from [python.org](https://www.python.org/downloads/) and tick **Add Python to PATH**. |
| **ffmpeg** | Needed to merge video and audio and to convert MP3/WAV. |
| **Edge, Chrome or Chromium** | Only for the app window on Linux. Windows and macOS use a built-in native window. |
| *(Recommended)* **Node.js or Deno** | yt-dlp may need a JavaScript runtime to read YouTube. Install one if downloads fail. |

Install ffmpeg:

- **Windows:** `winget install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`

Restart your terminal afterwards so the new command is found.

## Setup

1. **Get the code**

       git clone https://github.com/Abdul-7411/VidDrop.git
       cd VidDrop

   Or download the ZIP from GitHub and extract it.

2. **Run the setup script**

   - **Windows:** double-click `setup.bat`
   - **macOS / Linux:**

         python3 setup.py

   The script creates a `.venv` virtual environment, installs the dependencies into it, checks for ffmpeg, and puts a **VidDrop** shortcut on your Desktop. It tells you which window mode you got.

   Use `python setup.py --no-shortcut` to skip the shortcut.

3. **Start VidDrop**

   Double-click the **VidDrop** shortcut on your Desktop. It opens in its own window. Close the window to stop it.

   - **macOS:** the first time, right-click the shortcut and choose **Open**. It briefly opens Terminal.
   - **Linux:** if the shortcut doesn't launch, right-click it and choose **Allow Launching**.

## How to use

1. Paste a YouTube link, or click **Paste**, then click **Get video**.
2. Choose **Video** or **Audio**, then the format and quality.
3. Click **Download** and watch the progress. Your file is saved when it finishes.

Only single videos are supported. Playlists, live streams and videos longer than 3 hours are not.

## Updating

YouTube changes often and old yt-dlp versions break. To update everything, re-run the setup:

    python3 setup.py --no-shortcut

(Windows: double-click `setup.bat` again.) Do this first whenever downloads start failing.

## Run manually

    # App window
    .venv/bin/python app.py --app            # Windows: .venv\Scripts\python.exe app.py --app

    # In your normal browser
    .venv/bin/python app.py --open

    # Server only, then open http://127.0.0.1:5000
    .venv/bin/python app.py

## Use it on your phone (same Wi-Fi)

    HOST=0.0.0.0 .venv/bin/python app.py

On Windows PowerShell: `$env:HOST="0.0.0.0"; .venv\Scripts\python.exe app.py`

Then open `http://<your-computer-ip>:5000` on your phone. Only do this on a network you trust, because there is no login.

## Configuration

Set these as environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `HOST` | `127.0.0.1` | Address to listen on. |
| `PORT` | `5000` | Port to listen on. |
| `MAX_DURATION` | `10800` | Maximum video length in seconds. |
| `MAX_JOBS` | `3` | Downloads that can run at the same time. |
| `FRAGMENTS` | `8` | Parallel fragment downloads per job. |

## Troubleshooting

| Problem | Fix |
|---|---|
| "Unexpected error. Is ffmpeg installed?" | Install ffmpeg (see [Requirements](#requirements)) and restart the app. |
| Downloads fail or say sign-in or bot check is required | Update with `setup.py`, and install Node.js or Deno. YouTube often blocks cloud and datacenter IPs, so run VidDrop on your home connection. |
| "Server is busy" | The limit of parallel downloads is reached. Wait a moment or raise `MAX_JOBS`. |
| Port 5000 is already in use | Set a different `PORT`. |
| No app window on Linux | Install Chromium or Chrome. Otherwise it falls back to your browser. |
| Setup says it could not create the virtual environment | Debian/Ubuntu: `sudo apt install python3-venv`. |
| A resolution is missing | Only resolutions the uploader published are listed. |

## Deploy on a server (optional)

    gunicorn -w 1 --threads 16 -t 120 -b 0.0.0.0:8000 app:app

Keep `-w 1`, because jobs and ready files are tracked in memory. Put it behind HTTPS and some access control if it is reachable from the internet. Be aware that public downloaders carry legal risk.

## API

| Endpoint | Description |
|---|---|
| `POST /api/info` `{url}` | Title, thumbnail, duration and available resolutions. |
| `POST /api/prepare` `{url, type, format, quality}` | Starts a download and returns `{id}`. |
| `GET /api/status/<id>` | `state` (`starting`, `downloading`, `processing`, `ready`, `error`, `cancelled`), `percent`, `done`, `total`, `speed`, `eta`. |
| `POST /api/cancel/<id>` | Stops a running download. |
| `GET /api/file/<id>` | Downloads the finished file. |

## Project structure

    app.py            Flask backend, download jobs, desktop-window launcher
    index.html        Single-page UI (light/dark theme, progress panel)
    setup.py          Creates .venv, installs requirements, adds the shortcut
    setup.bat         Double-click launcher for setup.py on Windows
    requirements.txt  Python dependencies

## Legal

VidDrop is a tool. You are responsible for how you use it. Downloading content you don't have the right to save may violate YouTube's Terms of Service and copyright law in your country. VidDrop is not affiliated with or endorsed by YouTube or Google. It is provided as is, without warranty.

## License

MIT. See `LICENSE`.
