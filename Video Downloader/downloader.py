"""
OmniStream PRO - Universal Video Downloader Engine
Supports: YouTube, Vimeo, Reddit, Twitter/X, Dailymotion, Twitch,
          any .m3u8 / .mpd / .ts / .mp4 / .webm direct URL,
          any generic webpage with embedded video.

Fallback chain on extract failure:
  1) yt-dlp native (1800+ sites built-in)
  2) yt-dlp generic extractor on the page URL
  3) HTML regex-sniff for stream URLs embedded in the page
  4) Common video player API pattern scan (JWPlayer, VideoJS, Flowplayer...)
"""

import os
import re
import sys
import json
import shutil
import requests
from typing import Dict, Any, List, Optional, Callable
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Tool paths
# ---------------------------------------------------------------------------

def get_ffmpeg_path() -> str:
    # 1. Prioritize bundled bin inside project folder
    local_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg.exe')
    if os.path.exists(local_bin):
        return local_bin
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    found = shutil.which('ffmpeg')
    return found if found else 'ffmpeg'


def get_node_path() -> Optional[str]:
    local_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'node.exe')
    if os.path.exists(local_bin):
        return local_bin
    found = shutil.which('node')
    if found:
        return found
    default = r'C:\Program Files\nodejs\node.exe'
    return default if os.path.exists(default) else None


FFMPEG_PATH = get_ffmpeg_path()
NODE_PATH   = get_node_path()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_bytes(size: Optional[float]) -> str:
    if not size or size <= 0:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "Unknown"
    t = int(seconds)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _height_label(height: int) -> str:
    if height >= 4320: return f"8K UHD ({height}p)"
    if height >= 2160: return f"4K UHD ({height}p)"
    if height >= 1440: return f"2K QHD ({height}p)"
    if height >= 1080: return f"Full HD ({height}p)"
    if height >= 720:  return f"HD ({height}p)"
    if height >= 480:  return f"Standard ({height}p)"
    return f"Low ({height}p)"


def _is_video_format(f: dict) -> bool:
    """Return True if this yt-dlp format dict contains a video track."""
    vcodec    = f.get('vcodec') or ''
    video_ext = f.get('video_ext') or ''
    ext       = (f.get('ext') or '').lower()
    return bool(
        f.get('height')
        or f.get('resolution')
        or (video_ext and video_ext != 'none')
        or (vcodec and vcodec != 'none')
        or ext in ('mp4', 'mkv', 'webm', 'ts', 'avi', 'mov', 'flv', 'm4v', 'mpeg')
    )


def _skip_format(f: dict) -> bool:
    """Return True if this format should be excluded (storyboards, audio-only)."""
    ext    = (f.get('ext') or '').lower()
    vcodec = f.get('vcodec') or ''
    acodec = f.get('acodec') or ''
    # skip storyboard images
    if ext == 'mhtml':
        return True
    if 'image' in vcodec:
        return True
    # audio-only  (no video codec, or vcodec literally 'none')
    if vcodec == 'none' or (not vcodec and acodec and acodec != 'none'):
        return True
    return False


# ---------------------------------------------------------------------------
# Common HTTP headers (looks like a real browser)
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/126.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


# ---------------------------------------------------------------------------
# Build base yt-dlp options shared by extract and download
# ---------------------------------------------------------------------------

def _base_opts(cookie_mode: str = 'none',
               cookie_file: str = '',
               referer: str = '') -> dict:
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers['Referer'] = referer.strip()

    opts: dict = {
        'ffmpeg_location': FFMPEG_PATH,
        'http_headers': headers,
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'continue_dl': True,
        # YouTube EJS challenge solver (critical for YouTube)
        'remote_components': ['ejs:github'],
    }

    if NODE_PATH:
        opts['js_runtimes'] = {'node': {'path': NODE_PATH}}

    # Browser cookie auth
    if cookie_mode in ('firefox', 'edge', 'chrome', 'brave', 'opera'):
        opts['cookiesfrombrowser'] = (cookie_mode,)
    elif cookie_mode == 'file' and cookie_file and os.path.exists(cookie_file):
        opts['cookiefile'] = cookie_file

    return opts


# ---------------------------------------------------------------------------
# HTML / page stream sniffer
# ---------------------------------------------------------------------------

# Known video player JS patterns that embed stream URLs
_PLAYER_PATTERNS = [
    # JWPlayer
    r'file\s*:\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm)(?:\?[^"\']*)?)["\']',
    # VideoJS / Flowplayer / generic src
    r'src\s*:\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm|ts)(?:\?[^"\']*)?)["\']',
    # HTML5 data attributes
    r'data-(?:src|video|stream|file|hls|dash|url)\s*=\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm|ts)[^"\']*)["\']',
    # Generic URL with video extension anywhere in quotes
    r'["\']((https?:)?//[^"\'<>\s]+\.(?:m3u8|mpd|mp4|webm|ts|mkv)(?:\?[^"\'<>\s]*)?)["\']',
    # og:video / og:video:url meta
    r'og:video(?::url|:secure_url)?"[^>]+content="([^"]+)"',
    r'og:video(?::url|:secure_url)?\'[^>]+content=\'([^\']+)\'',
]

_IFRAME_PROVIDERS = [
    'youtube.com/embed', 'youtu.be/', 'player.vimeo.com', 'vimeo.com/',
    'dailymotion.com/embed', 'ok.ru/videoembed', 'streamtape.com',
    'dood.', 'vidcloud', 'streamlare', 'mixdrop', 'fembed',
    'twitch.tv/', 'facebook.com/video', 'rutube.ru',
]


def sniff_streams(html: str, page_url: str = '') -> List[str]:
    """
    Scan raw HTML for embedded stream / video URLs.
    Returns a deduplicated list, best candidates first.
    """
    found: dict = {}   # url -> priority (lower = better)

    def add(url: str, priority: int = 5):
        url = url.strip()
        if not url or len(url) < 10:
            return
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/') and page_url:
            parsed = urlparse(page_url)
            url = f"{parsed.scheme}://{parsed.netloc}{url}"
        if url not in found or priority < found[url]:
            found[url] = priority

    # 1. <source src="..."> and <video src="...">
    for tag in ('source', 'video'):
        for m in re.finditer(rf'<{tag}[^>]+src=["\']([^"\']+)["\']', html, re.I):
            src = m.group(1)
            if page_url and not src.startswith('http'):
                src = urljoin(page_url, src)
            add(src, 1)

    # 2. og:video meta tags
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)=["\']og:video(?::(?:url|secure_url))?["\'][^>]*content=["\']([^"\']+)["\']',
        html, re.I
    ):
        add(m.group(1), 1)

    # 3. All JS player patterns
    for pattern in _PLAYER_PATTERNS:
        for m in re.finditer(pattern, html, re.I):
            url = m.group(1)
            if page_url and not url.startswith('http'):
                url = urljoin(page_url, url)
            add(url, 2)

    # 4. Bare URL scan - any quote-wrapped URL with a video extension
    for m in re.finditer(
        r'["\']((https?:)?//[^\s"\'<>\\]+\.(?:m3u8|mpd|mp4|webm|ts|mkv)(?:\?[^\s"\'<>\\]*)?)["\']',
        html, re.I
    ):
        add(m.group(1), 3)

    # 5. Iframe embeds from known providers
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1)
        if any(p in src.lower() for p in _IFRAME_PROVIDERS):
            if src.startswith('//'):
                src = 'https:' + src
            add(src, 4)

    # Sort by priority then return
    return [url for url, _ in sorted(found.items(), key=lambda x: x[1])]


def fetch_page_streams(page_url: str, extra_headers: dict = None) -> List[str]:
    """Fetch a webpage and sniff it for stream URLs."""
    try:
        headers = dict(BROWSER_HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        resp = requests.get(page_url, headers=headers, timeout=15,
                            allow_redirects=True, verify=True)
        ct = resp.headers.get('content-type', '')
        if resp.status_code == 200 and ('html' in ct or 'text' in ct):
            return sniff_streams(resp.text, page_url)
    except Exception:
        pass
    return []


def resolve_full_stream_url(url: str, referer: str = '') -> str:
    """
    If the provided URL is an individual HLS segment (e.g. seg-8-v1-a1.ts?token=...),
    look for the playlist (.m3u8 / .mpd) next to it so the ENTIRE video is downloaded.

    Only URLs whose *file extension* is a segment type are treated as segments.
    (The old version also matched names like "chunk1.mp4" and, when nothing was
    found, still returned an invented index.m3u8 - which broke normal downloads.)
    If no playlist can be verified the original URL is returned unchanged.
    """
    parsed = urlparse(url)
    if not re.search(r'\.(?:ts|m4s|aac|cmfv|cmfa)$', parsed.path, re.I):
        return url

    dir_path = parsed.path.rsplit('/', 1)[0]
    query = parsed.query

    candidates = ['index.m3u8', 'master.m3u8', 'playlist.m3u8', 'prog_index.m3u8', 'video.m3u8', 'manifest.mpd']
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers['Referer'] = referer

    for c in candidates:
        cand_url = f"{parsed.scheme}://{parsed.netloc}{dir_path}/{c}{('?' + query) if query else ''}"
        try:
            r_get = requests.get(cand_url, headers=headers, timeout=6, stream=True)
            try:
                if r_get.status_code == 200:
                    first_bytes = next(r_get.iter_content(512), b'')
                    if b'#EXTM3U' in first_bytes or b'<MPD' in first_bytes:
                        return cand_url
            finally:
                r_get.close()
        except Exception:
            continue

    return url


# ---------------------------------------------------------------------------
# Main downloader class
# ---------------------------------------------------------------------------

class StreamDownloader:

    def __init__(self):
        self._cancel: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public: extract_info
    # ------------------------------------------------------------------
    def extract_info(
        self,
        url: str,
        cookie_mode: str = 'none',
        cookie_file: str = '',
        referer: str = '',
    ) -> Dict[str, Any]:
        import yt_dlp

        url = url.strip()
        # If user entered a plain search query (e.g. movie/song name or direct title from a torrent)
        if not re.match(r'^[a-zA-Z0-9+.-]+://', url) and not url.startswith(('ytsearch', 'gvsearch', 'scsearch')):
            url = f'ytsearch1:{url}'
        else:
            # Automatically upgrade individual chunk segments to full master stream playlists
            url = resolve_full_stream_url(url, referer)

        base = _base_opts(cookie_mode, cookie_file, referer)
        quiet_base = dict(base, quiet=True, no_warnings=True,
                          skip_download=True, extract_flat=False)

        info      = None
        last_err  = ''

        # ---------------------------------------------------------------
        # ATTEMPT 1: yt-dlp native extraction (handles 1800+ sites)
        # ---------------------------------------------------------------
        try:
            with yt_dlp.YoutubeDL(quiet_base) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = str(e)

        # ---------------------------------------------------------------
        # ATTEMPT 2: yt-dlp generic extractor with extra flags
        # (forces the generic extractor to dig into unknown pages)
        # ---------------------------------------------------------------
        if info is None:
            try:
                generic_opts = dict(quiet_base)
                generic_opts.update({
                    'force_generic_extractor': True,
                    'allow_unplayable_formats': True,
                })
                with yt_dlp.YoutubeDL(generic_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                last_err = str(e)

        # ---------------------------------------------------------------
        # ATTEMPT 3: Fetch the webpage, sniff for stream URLs, try each
        # ---------------------------------------------------------------
        if info is None:
            sniffed = fetch_page_streams(
                url,
                extra_headers={'Referer': referer} if referer else None
            )
            for candidate in sniffed:
                try:
                    with yt_dlp.YoutubeDL(quiet_base) as ydl:
                        info = ydl.extract_info(candidate, download=False)
                    if info:
                        url = candidate   # update URL to the working stream
                        break
                except Exception:
                    continue

        # ---------------------------------------------------------------
        # ATTEMPT 4: Try as a direct stream with yt-dlp's generic handler
        # using a looser set of options
        # ---------------------------------------------------------------
        if info is None:
            for extra_flag in [
                {'youtube_include_dash_manifest': True},
                {'no_check_extensions': True},
                {},
            ]:
                try:
                    opts4 = dict(quiet_base, **extra_flag)
                    opts4['generic_extractor_mode'] = 'both'
                    with yt_dlp.YoutubeDL(opts4) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if info:
                        break
                except Exception as e:
                    last_err = str(e)

        # ---------------------------------------------------------------
        # All attempts failed — give a clear, actionable error
        # ---------------------------------------------------------------
        if info is None:
            hint = ''
            if 'Sign in' in last_err or 'bot' in last_err.lower():
                hint = (
                    "\n\nFix: This site requires login. "
                    "Set Cookie Auth to your browser (Firefox/Edge/Chrome) and try again."
                )
            elif 'authentication' in last_err.lower() or '403' in last_err:
                hint = (
                    "\n\nFix: Page is restricted. "
                    "Try setting Cookie Auth to your browser, "
                    "or paste the direct stream URL (.m3u8 / .mp4) if you have it."
                )
            elif '404' in last_err:
                hint = "\n\nFix: URL not found. Check the link is correct and the page is public."
            raise RuntimeError(
                f"Could not find any downloadable video stream on this page.\n"
                f"Reason: {last_err[:300]}{hint}\n\n"
                f"Tips:\n"
                f"  - For sites requiring login, set Cookie Auth to your browser\n"
                f"  - Open DevTools (F12) > Network > find .m3u8 or .mp4 URLs and paste them directly\n"
                f"  - Use the Referer field if the site blocks direct stream access"
            )

        # Flatten playlist -> take first entry
        if 'entries' in info and info['entries']:
            first = info['entries'][0]
            if first:
                info = first

        return self._build_result(url, info)

    # ------------------------------------------------------------------
    # Build quality result dict from yt-dlp info
    # ------------------------------------------------------------------
    def _build_result(self, url: str, info: dict) -> Dict[str, Any]:
        title     = info.get('title') or 'Untitled'
        thumbnail = info.get('thumbnail') or ''
        duration  = info.get('duration')
        uploader  = (
            info.get('uploader') or info.get('channel')
            or info.get('extractor_key') or 'Web'
        )
        raw_fmts = info.get('formats') or []

        # Stream type label
        proto    = info.get('protocol') or ''
        src_ext  = (info.get('ext') or '').lower()
        u_lower  = url.lower()
        if 'm3u8' in u_lower or 'm3u8' in proto:
            stream_type = 'HLS (.m3u8)'
        elif 'mpd' in u_lower or 'dash' in proto:
            stream_type = 'DASH (.mpd)'
        elif '.ts' in u_lower:
            stream_type = 'Transport Stream (.ts)'
        elif src_ext:
            stream_type = f'{src_ext.upper()} Video'
        else:
            stream_type = 'Web Stream'

        qualities: List[Dict[str, Any]] = []

        # ---- No format list: single direct stream ----
        if not raw_fmts:
            fsize = info.get('filesize') or info.get('filesize_approx')
            qualities.append({
                'id': 'best',
                'label': 'Original Source Quality',
                'resolution': 'Source',
                'height': 0, 'fps': 0,
                'ext': src_ext or 'mp4',
                'vcodec': 'Original', 'acodec': 'Original',
                'filesize': format_bytes(fsize),
                'format_note': 'Direct Stream',
                'is_single': True,
            })
        else:
            # Sort best quality first
            sorted_fmts = sorted(
                raw_fmts,
                key=lambda f: (
                    f.get('height') or 0,
                    f.get('fps') or 0,
                    f.get('tbr') or f.get('vbr') or 0,
                ),
                reverse=True,
            )

            seen: set = set()
            for f in sorted_fmts:
                if not _is_video_format(f):
                    continue
                if _skip_format(f):
                    continue

                height = f.get('height') or 0
                fps    = f.get('fps') or 0
                ext    = f.get('ext') or 'mp4'
                note   = f.get('format_note') or ''
                fid    = str(f.get('format_id') or '')

                # Deduplicate by height+fps bucket
                dedup = f"{height}_{fps if fps > 30 else 30}"
                if dedup in seen:
                    continue
                seen.add(dedup)

                # Label
                if height:
                    label = _height_label(height)
                    if fps > 30:
                        label += f" {fps}fps"
                else:
                    label = f.get('resolution') or 'Original Quality'

                # Resolution string (always use height-based, e.g. "1080p")
                res_str = f"{height}p" if height else (f.get('resolution') or 'Source')

                # File size estimate
                fsize = f.get('filesize') or f.get('filesize_approx')
                if not fsize and duration:
                    bitrate = f.get('tbr') or f.get('vbr') or 0
                    if bitrate:
                        fsize = (bitrate * 1000 * duration) / 8

                vcodec = f.get('vcodec') or 'auto'
                acodec = f.get('acodec') or 'auto'
                vc = vcodec.split('.')[0] if '.' in vcodec else vcodec
                ac = acodec.split('.')[0] if '.' in acodec else acodec

                qualities.append({
                    'id': fid,
                    'label': label,
                    'resolution': res_str,
                    'height': height,
                    'fps': fps,
                    'ext': ext,
                    'vcodec': vc,
                    'acodec': ac,
                    'filesize': format_bytes(fsize),
                    'format_note': note or 'Original',
                    'is_single': False,
                })

            # Audio-only option
            qualities.append({
                'id': 'bestaudio',
                'label': 'Audio Only (Best Quality)',
                'resolution': 'Audio',
                'height': -1, 'fps': 0,
                'ext': 'm4a',
                'vcodec': 'none', 'acodec': 'best',
                'filesize': 'Audio',
                'format_note': 'Audio Track',
                'is_single': False,
            })

        # If only one video quality, mark it as single
        video_only = [q for q in qualities if q['id'] != 'bestaudio']
        if len(video_only) == 1:
            video_only[0]['is_single'] = True

        canonical_url = info.get('webpage_url') or info.get('original_url') or url
        return {
            'url': canonical_url,
            'title': title,
            'thumbnail': thumbnail,
            'duration': duration,
            'duration_str': format_duration(duration),
            'uploader': uploader,
            'qualities': qualities,
            'site': info.get('extractor_key') or 'Web',
            'stream_type': stream_type,
        }

    # ------------------------------------------------------------------
    # Public: download_stream
    # ------------------------------------------------------------------
    def download_stream(
        self,
        url: str,
        format_id: str,
        output_dir: str,
        strict_mp4: bool,
        task_id: str,
        cookie_mode: str,
        cookie_file: str,
        referer: str,
        turbo_threads: int,
        progress_callback: Callable[[Dict[str, Any]], None],
    ):
        import yt_dlp

        self._cancel[task_id] = False
        os.makedirs(output_dir, exist_ok=True)

        # Output template — use height-based label, not raw resolution string
        # %(resolution)s in yt-dlp is "WxH" which looks bad (e.g. "1920x1080")
        # We override with a custom hook approach below.
        tmpl = os.path.join(output_dir, '%(title).150B%(height& [{}p]|)s.%(ext)s')

        # Format selection
        if format_id == 'bestaudio':
            fmt = 'bestaudio/best'
        elif format_id == 'best':
            fmt = 'bestvideo+bestaudio/best'
        else:
            fmt = f'{format_id}+bestaudio/bestvideo+bestaudio/{format_id}/best'

        def hook(d):
            if self._cancel.get(task_id):
                raise Exception('Cancelled')

            st = d.get('status')
            if st == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                total_is_estimate = not d.get('total_bytes')
                done  = d.get('downloaded_bytes') or 0
                spd   = d.get('speed') or 0
                eta   = d.get('eta') or 0
                pct   = (done / total * 100) if total else 0
                fc, fi = d.get('fragment_count'), d.get('fragment_index')
                if fc and fi is not None and (not total or total_is_estimate):
                    pct = min(99.0, fi / fc * 100)

                progress_callback({
                    'task_id': task_id,
                    'status': 'downloading',
                    'percent': round(pct, 1),
                    'downloaded_str': format_bytes(done),
                    'total_str': format_bytes(total) if total else 'Calculating...',
                    'speed_str': f"{format_bytes(spd)}/s" if spd else 'Connecting...',
                    'eta_str': f"{int(eta)}s" if eta else '...',
                    'filename': os.path.basename(d.get('filename', '')),
                })

            elif st == 'finished':
                msg = 'Muxing audio+video into MP4...' if strict_mp4 else 'Finalizing...'
                progress_callback({
                    'task_id': task_id,
                    'status': 'processing',
                    'percent': 100.0,
                    'message': msg,
                })

        # Post-processors
        pp = []
        if format_id == 'bestaudio':
            pp.append({
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3' if strict_mp4 else 'm4a',
                'preferredquality': '320',
            })
        elif strict_mp4:
            pp.append({
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            })

        threads = max(1, min(int(turbo_threads or 8), 16))

        opts = _base_opts(cookie_mode, cookie_file, referer)
        opts.update({
            'format': fmt,
            'outtmpl': tmpl,
            'progress_hooks': [hook],
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'postprocessors': pp,
            'merge_output_format': (
                'mp4' if strict_mp4 and format_id != 'bestaudio' else None
            ),
            'concurrent_fragment_downloads': threads,
        })

        final = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    final = info.get('_filename') or ydl.prepare_filename(info)
                    if strict_mp4 and format_id != 'bestaudio':
                        base, _ = os.path.splitext(final)
                        mp4 = base + '.mp4'
                        if os.path.exists(mp4):
                            final = mp4

            if final and os.path.exists(final):
                progress_callback({
                    'task_id': task_id,
                    'status': 'completed',
                    'percent': 100.0,
                    'filepath': final,
                    'filename': os.path.basename(final),
                    'filesize': format_bytes(os.path.getsize(final)),
                    'message': 'Download complete! Original quality preserved.',
                })
            else:
                # File may have been remuxed — scan output_dir for newest file
                newest = _find_newest_file(output_dir)
                if newest:
                    progress_callback({
                        'task_id': task_id,
                        'status': 'completed',
                        'percent': 100.0,
                        'filepath': newest,
                        'filename': os.path.basename(newest),
                        'filesize': format_bytes(os.path.getsize(newest)),
                        'message': 'Saved to download folder.',
                    })
                else:
                    progress_callback({
                        'task_id': task_id,
                        'status': 'completed',
                        'percent': 100.0,
                        'filepath': output_dir,
                        'filename': '',
                        'message': 'Saved to download folder.',
                    })

        except Exception as e:
            if self._cancel.get(task_id):
                progress_callback({
                    'task_id': task_id,
                    'status': 'cancelled',
                    'message': 'Download cancelled.',
                })
            else:
                progress_callback({
                    'task_id': task_id,
                    'status': 'error',
                    'message': str(e),
                })

    def _forget(self, task_id: str):
        self._cancel.pop(task_id, None)

    def cancel_download(self, task_id: str):
        self._cancel[task_id] = True


# ---------------------------------------------------------------------------
# Utility: find the newest file in a directory (after download completes)
# ---------------------------------------------------------------------------

def _find_newest_file(directory: str) -> Optional[str]:
    try:
        files = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, f))
            and not f.endswith('.part')
            and not f.endswith('.ytdl')
        ]
        if not files:
            return None
        return max(files, key=os.path.getmtime)
    except Exception:
        return None
