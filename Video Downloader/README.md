# OmniStream PRO - Universal Desktop Video Downloader & Stream Sniffer

An advanced desktop streaming downloader and browser companion designed to inspect, extract, and download original video sources across websites, HLS (`.m3u8`), DASH (`.mpd`), transport chunks (`.ts`), and direct media (`.mp4`, `.webm`, `.mkv`).

---

## 🌟 Core Features

### 1. ⚡ Universal Stream Downloader
* **4-Layer Extraction Engine**:
  1. Native 1,800+ site extractors (YouTube, Vimeo, Reddit, Twitter/X, Dailymotion, Twitch...).
  2. Generic webpage stream inspection.
  3. Deep HTML regex stream sniffer (detects hidden `.m3u8`, `.mpd`, `.ts`, `.mp4` URLs in player scripts & HTML5 tags).
  4. Loose mode with adaptive stream decoding.
* **Original Master Quality**: Lists all detected stream resolutions (`8K`, `4K UHD`, `2K QHD`, `1080p Full HD`, `720p HD`, `480p`, `360p`, `240p`).
* **Strict MP4 Container Mode**: Repackages and muxes `.ts`, `.webm`, `.m3u8`, `.mpd` into `.mp4` using native **FFmpeg 7.1** with lossless stream copy (`-c copy`).
* **IDM-Style Turbo Concurrent Acceleration**: 8 to 16 parallel fragment connection threads.

### 2. 📸 Dedicated Instagram Hub
* Separate Instagram tab accessible directly from the app home navigation.
* Enter any username (`@username`) or paste a profile URL to view:
  * Full name, biography, verification status, followers count, following count, and post count.
  * 1-click **Download HD Profile Picture (Avatar)**.
  * Visual gallery of profile **Reels, Videos, and Photos** with view/preview and 1-click **Download MP4 / JPG** buttons.
* Direct Reel / Post link support: Paste any `instagram.com/reel/...` or `instagram.com/p/...` link for instant download.

### 3. 🧩 IDM-Style Browser Extension Companion (`extension/`)
* Runs in Chromium browsers: **Google Chrome**, **Microsoft Edge**, **Brave**, **Opera** (Manifest V3, Chrome 109+).
* Intercepts media streaming requests (`.m3u8`, `.mpd`, `.ts`, `.mp4`, `.webm`, Instagram CDN) while you browse ANY website.
* Sends captured streams in real-time to the OmniStream desktop app via local bridge (`http://127.0.0.1:6890`).
* **Segment cache (new):** for HLS streams the extension requests *every* segment itself, stores them in the browser (IndexedDB) and, when you press **Download**, stitches them into one file instantly. Handles master playlists (best quality picked), AES-128 encrypted streams, fMP4, byte-ranges, and sites that check the Referer.
  Output is `.ts` (or `.mp4` for fMP4 streams) in `Downloads/OmniStream/`. DRM / live / DASH streams are handed to the desktop app.

---

## 🚀 How to Install the Browser Extension (30 Seconds)

1. Open your browser's extensions management page:
   * **Chrome / Brave**: `chrome://extensions`
   * **Microsoft Edge**: `edge://extensions`
2. Enable the **"Developer mode"** toggle in the top-right corner.
3. Click **"Load unpacked"**.
4. Select the folder:
   ```
   E:\Video Downloader\extension
   ```
5. As you browse video sites or Instagram, the extension icon badge will count detected video streams and can send them to OmniStream with one click!

---

## 💻 How to Run the Desktop App

### Option 1: Double-Click
Double-click [`run.bat`](file:///E:/Video%20Downloader/run.bat) inside `E:\Video Downloader`.

### Option 2: PowerShell / Command Line
```powershell
cd "E:\Video Downloader"
python app.py
```
