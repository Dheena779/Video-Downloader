"""
OmniStream PRO - Torrent Downloader Module
Supports .torrent file upload and magnet links.
Uses bundled aria2c (or system aria2c) with full DHT, PEX, LPD, and public trackers.
Real-time progress reporting: metadata discovery, download percentage, speed, seeds, peers, ETA.
"""

import os
import re
import json
import time
import struct
import hashlib
import threading
import urllib.parse
from typing import Callable, Optional, Dict, Any, List

# ---------------------------------------------------------------------------
# Tracker list for turbo DHT / peer discovery
# ---------------------------------------------------------------------------
PUBLIC_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.tracker.cl:1337/announce',
    'udp://tracker.openbittorrent.com:6969/announce',
    'udp://tracker.torrent.eu.org:451/announce',
    'udp://open.demonii.com:1337/announce',
    'udp://tracker.coppersurfer.tk:6969/announce',
    'udp://explodie.org:6969/announce',
    'udp://9.rarbg.to:2920/announce',
    'udp://p4p.arenabg.com:1337/announce',
    'http://tracker.openbittorrent.com:80/announce',
]

def get_all_active_trackers() -> List[str]:
    """Loads bundled 75+ worldwide trackers and merges with default trackers."""
    trackers = list(PUBLIC_TRACKERS)
    trackers_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'trackers.txt')
    if os.path.isfile(trackers_file):
        try:
            with open(trackers_file, 'r', encoding='utf-8') as f:
                for line in f:
                    t = line.strip()
                    if t and not t.startswith('#') and t not in trackers:
                        trackers.append(t)
        except Exception:
            pass
    return trackers

def clean_torrent_title(name: str) -> str:
    """Extracts clean human-readable movie/video title from raw release filename."""
    s = re.sub(r'^(?:www\.)?[a-zA-Z0-9.-]+\s*-\s*', '', name, flags=re.I)
    s = re.sub(r'(?:BR-Rip|HDRip|WEBRip|DVDRip|BluRay|HDTV|x264|x265|HEVC|AAC|DDP?|5\.1|1080p|720p|480p|2160p|4K|ESub|HQ|PRO|Proper).*', '', s, flags=re.I)
    s = re.sub(r'\b(?:English|Tamil|Hindi|Telugu|Malayalam|Kannada|Dual\s*Audio|Multi\s*Audio)\b.*', '', s, flags=re.I)
    s = re.sub(r'\[.*?\]', '', s)
    s = re.sub(r'\(.*?\)', lambda m: m.group(0) if re.search(r'\d{4}', m.group(0)) else '', s)
    cleaned = s.strip(' -_.')
    return cleaned if cleaned else name

# ---------------------------------------------------------------------------
# Torrent metadata parser (pure Python, bencode decoder)
# ---------------------------------------------------------------------------
def _bdecode(data: bytes, pos: int = 0):
    """
    Recursive bencode decoder working on raw bytes.
    Byte strings are returned as `bytes` (decode only where text is expected);
    dict keys are returned as `str`.  Returns (value, next_pos).
    """
    if pos >= len(data):
        raise ValueError('Unexpected end of bencode data')
    ch = data[pos:pos + 1]

    if ch == b'i':
        end = data.index(b'e', pos + 1)
        return int(data[pos + 1:end]), end + 1

    if ch == b'l':
        pos += 1
        lst = []
        while data[pos:pos + 1] != b'e':
            val, pos = _bdecode(data, pos)
            lst.append(val)
        return lst, pos + 1

    if ch == b'd':
        pos += 1
        d = {}
        while data[pos:pos + 1] != b'e':
            key, pos = _bdecode(data, pos)
            val, pos = _bdecode(data, pos)
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='replace')
            d[key] = val
        return d, pos + 1

    if ch.isdigit():
        colon = data.index(b':', pos)
        length = int(data[pos:colon])
        start = colon + 1
        if start + length > len(data):
            raise ValueError('Bencode string runs past end of data')
        return data[start:start + length], start + length

    raise ValueError(f'Unknown bencode token: {data[pos:pos + 4]!r}')


def _txt(v) -> str:
    """Decode a bencoded byte string (or pass through str) to text."""
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='replace')
    return str(v) if v is not None else ''


def _extract_info_bytes(raw: bytes) -> bytes:
    """
    Return the exact raw bytes of the top-level 'info' dict.
    Walks the outer dictionary instead of searching for the text '4:info'
    (which could also appear inside a comment or tracker string).
    """
    if raw[:1] != b'd':
        raise ValueError('Not a bencoded dictionary')
    pos = 1
    while raw[pos:pos + 1] != b'e':
        key, pos = _bdecode(raw, pos)
        val_start = pos
        _, pos = _bdecode(raw, pos)
        if key == b'info':
            return raw[val_start:pos]
    raise ValueError("No 'info' dictionary in torrent")


def parse_torrent_file(filepath: str) -> Dict[str, Any]:
    """Parse a .torrent file and extract clean metadata."""
    with open(filepath, 'rb') as f:
        raw = f.read()

    data, _ = _bdecode(raw)
    if not isinstance(data, dict) or not isinstance(data.get('info'), dict):
        raise ValueError('This file is not a valid .torrent (no info section).')
    info = data['info']

    name = _txt(info.get('name.utf-8') or info.get('name'))

    info_bytes = _extract_info_bytes(raw)
    info_hash = hashlib.sha1(info_bytes).hexdigest()

    # File listing
    files = []
    total_size = 0
    if 'files' in info:
        for f in info['files']:
            parts = f.get('path.utf-8') or f.get('path') or []
            path = '/'.join(_txt(p) for p in parts)
            sz = int(f.get('length', 0))
            total_size += sz
            files.append({'path': path, 'size': sz})
    else:
        sz = int(info.get('length', 0))
        total_size = sz
        files = [{'path': name, 'size': sz}]

    # Trackers
    trackers = []
    ann = _txt(data.get('announce'))
    if ann:
        trackers.append(ann)
    for tier in data.get('announce-list', []) or []:
        for t in (tier if isinstance(tier, list) else [tier]):
            t = _txt(t)
            if t and t not in trackers:
                trackers.append(t)

    pieces = info.get('pieces', b'')
    return {
        'name': name or os.path.basename(filepath),
        'info_hash': info_hash,
        'total_size': total_size,
        'files': files,
        'trackers': trackers,
        'piece_length': info.get('piece length', 0),
        'num_pieces': len(pieces) // 20 if isinstance(pieces, bytes) else 0,
        'magnet': f'magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}',
    }


def parse_magnet(magnet_link: str) -> Dict[str, Any]:
    """Parse a magnet link and extract metadata."""
    parsed = urllib.parse.urlparse(magnet_link)
    params = urllib.parse.parse_qs(parsed.query)

    xt = params.get('xt', [''])[0]
    info_hash = ''
    if 'urn:btih:' in xt:
        info_hash = xt.split('urn:btih:')[1].split('&')[0].lower()

    name = urllib.parse.unquote(params.get('dn', ['Magnet Torrent'])[0])
    trackers = [urllib.parse.unquote(t) for t in params.get('tr', [])]

    for t in PUBLIC_TRACKERS:
        if t not in trackers:
            trackers.append(t)

    return {
        'name': name,
        'info_hash': info_hash,
        'total_size': 0,
        'files': [],
        'trackers': trackers,
        'piece_length': 0,
        'num_pieces': 0,
        'magnet': magnet_link,
    }


# ---------------------------------------------------------------------------
# aria2c detection (prioritizes bundled <app folder>\bin\aria2c.exe)
# ---------------------------------------------------------------------------
def _find_aria2c() -> Optional[str]:
    import shutil
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, 'bin', 'aria2c.exe'),
        os.path.join(base_dir, 'bin', 'aria2c'),
        r'E:\Video Downloader\bin\aria2c.exe',
        r'E:\aria2\aria2c.exe',
        r'C:\Program Files\aria2\aria2c.exe',
        r'C:\aria2\aria2c.exe',
        r'C:\Users\Public\aria2c.exe',
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which('aria2c')


# ---------------------------------------------------------------------------
# aria2c runner.
#
# Progress is read through aria2's JSON-RPC interface instead of scraping the
# console text.  Scraping was fragile (the BitTorrent summary line contains a
# "UL:" field the old regex did not allow, so progress never matched) and it
# could not tell "failed to start" from "finished".
# ---------------------------------------------------------------------------
def _fmt_size(n: float) -> str:
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.1f} PB'


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return '—'
    if seconds >= 86400:
        return f'{seconds // 86400}d {(seconds % 86400) // 3600}h'
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}h {m:02d}m'
    if m:
        return f'{m}m {s:02d}s'
    return f'{s}s'


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Aria2Rpc:
    def __init__(self, port: int, secret: str):
        self.url = f'http://127.0.0.1:{port}/jsonrpc'
        self.secret = secret
        self._n = 0

    def call(self, method: str, *params):
        import urllib.request
        self._n += 1
        payload = json.dumps({
            'jsonrpc': '2.0',
            'id': str(self._n),
            'method': method,
            'params': [f'token:{self.secret}', *params],
        }).encode('utf-8')
        req = urllib.request.Request(
            self.url, data=payload, headers={'Content-Type': 'application/json'}
        )
        # Local loopback call - never go through a system proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        if 'error' in body:
            raise RuntimeError(body['error'].get('message', 'aria2 RPC error'))
        return body['result']


_SESSION_KEYS = [
    'gid', 'status', 'totalLength', 'completedLength', 'downloadSpeed',
    'numSeeders', 'connections', 'errorCode', 'errorMessage', 'followedBy',
    'files', 'bittorrent', 'dir',
]


def build_aria2_command(aria2c: str, output_dir: str, port: int, secret: str) -> List[str]:
    """Build the aria2c command line.  Every option here is validated against aria2 1.37."""
    trackers = get_all_active_trackers()
    return [
        aria2c,
        '--no-conf=true',
        '--dir=' + output_dir,
        # --- RPC (progress channel) ---
        '--enable-rpc=true',
        '--rpc-listen-all=false',
        f'--rpc-listen-port={port}',
        f'--rpc-secret={secret}',
        '--quiet=true',
        # --- peer discovery ---
        '--enable-dht=true',
        '--enable-dht6=true',
        '--enable-peer-exchange=true',
        '--bt-enable-lpd=true',
        '--bt-tracker=' + ','.join(trackers),
        '--bt-tracker-connect-timeout=10',
        '--bt-tracker-timeout=10',
        '--bt-max-peers=200',
        '--bt-request-peer-speed-limit=10M',
        '--listen-port=50000-50100',
        '--dht-listen-port=50000-50100',
        # --- encryption: accept plain and encrypted peers ---
        '--bt-min-crypto-level=plain',
        '--bt-require-crypto=false',
        # --- behaviour ---
        '--seed-time=0',                  # stop as soon as the download is complete
        '--file-allocation=none',
        '--auto-file-renaming=false',
        '--allow-overwrite=true',
        '--continue=true',
        '--bt-save-metadata=true',
        '--bt-load-saved-metadata=true',
        '--bt-prioritize-piece=head=2M,tail=2M',
        '--follow-torrent=true',
        '--max-connection-per-server=16',
        '--split=16',
        '--peer-agent=uTorrent/355(45798)',
        '--peer-id-prefix=-UT3550-',
    ]


def _popen_flags() -> int:
    # Hide the black console window on Windows.
    return getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0)


def _download_aria2c(
    torrent_source: str,
    output_dir: str,
    task_id: str,
    progress_callback: Callable,
    cancel_event: threading.Event,
) -> str:
    import subprocess
    import secrets
    from collections import deque

    aria2c = _find_aria2c()
    if not aria2c:
        raise FileNotFoundError(
            'aria2c engine not found. Put aria2c.exe in the "bin" folder next to app.py.'
        )

    os.makedirs(output_dir, exist_ok=True)

    port = _free_port()
    secret = secrets.token_hex(16)
    rpc = _Aria2Rpc(port, secret)
    cmd = build_aria2_command(aria2c, output_dir, port, secret)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        creationflags=_popen_flags(),
    )

    # Drain aria2c's output so its pipe can never fill up, and keep the tail
    # so a start-up failure can be reported to the user.
    log_tail: deque = deque(maxlen=12)

    def _drain():
        try:
            for ln in proc.stdout:
                ln = ln.strip()
                if ln:
                    log_tail.append(ln)
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()

    def _stop():
        try:
            if proc.poll() is None:
                try:
                    rpc.call('aria2.forceShutdown')
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.terminate()
        except Exception:
            pass

    def _fail_text() -> str:
        tail = ' | '.join(list(log_tail)[-4:])
        return tail or 'aria2c stopped unexpectedly.'

    try:
        progress_callback({
            'task_id': task_id, 'status': 'downloading', 'percent': 0,
            'seeds': 0, 'peers': 0,
            'speed_str': 'Starting torrent engine…', 'eta_str': '…',
            'downloaded_str': 'Starting', 'total_str': 'Waiting for metadata',
        })

        # 1. Wait for the RPC server to come up (fails fast if aria2c rejects an option)
        deadline = time.time() + 20
        while True:
            if cancel_event.is_set():
                return ''
            if proc.poll() is not None:
                time.sleep(0.2)
                raise RuntimeError('aria2c failed to start: ' + _fail_text())
            try:
                rpc.call('aria2.getVersion')
                break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError('aria2c did not start in time: ' + _fail_text())
                time.sleep(0.25)

        # 2. Add the torrent / magnet
        if torrent_source.startswith('magnet:'):
            gid = rpc.call('aria2.addUri', [torrent_source])
        else:
            import base64
            with open(torrent_source, 'rb') as fh:
                b64 = base64.b64encode(fh.read()).decode('ascii')
            gid = rpc.call('aria2.addTorrent', b64)

        is_magnet = torrent_source.startswith('magnet:')
        saved_path = ''

        # 3. Poll progress
        while True:
            if cancel_event.is_set():
                progress_callback({
                    'task_id': task_id, 'status': 'cancelled',
                    'message': 'Download cancelled by user.',
                })
                return ''

            if proc.poll() is not None:
                raise RuntimeError('aria2c exited unexpectedly: ' + _fail_text())

            try:
                st = rpc.call('aria2.tellStatus', gid, _SESSION_KEYS)
            except Exception as e:
                if proc.poll() is not None:
                    raise RuntimeError('aria2c exited unexpectedly: ' + _fail_text())
                time.sleep(0.5)
                continue

            status = st.get('status')

            # A magnet first downloads its metadata, then aria2 hands the job
            # to a new gid ("followedBy").  Switch to it.
            followed = st.get('followedBy') or []
            if status == 'complete' and followed:
                gid = followed[0]
                is_magnet = False
                continue

            total = int(st.get('totalLength') or 0)
            done = int(st.get('completedLength') or 0)
            speed = int(st.get('downloadSpeed') or 0)
            seeds = int(st.get('numSeeders') or 0)
            peers = int(st.get('connections') or 0)

            if status == 'error':
                msg = st.get('errorMessage') or f"aria2 error code {st.get('errorCode')}"
                raise RuntimeError(msg)

            if status == 'removed':
                raise RuntimeError('Torrent was removed before it finished.')

            if status == 'complete':
                bt_name = ((st.get('bittorrent') or {}).get('info') or {}).get('name', '')
                files = st.get('files') or []
                base = st.get('dir') or output_dir
                if bt_name:
                    saved_path = os.path.join(base, bt_name)
                elif files and files[0].get('path'):
                    saved_path = files[0]['path']
                else:
                    saved_path = base
                return saved_path

            # waiting / paused / active
            if total == 0:
                progress_callback({
                    'task_id': task_id, 'status': 'downloading', 'percent': 0,
                    'seeds': seeds, 'peers': peers,
                    'speed_str': f'{peers} peers connected',
                    'eta_str': 'Fetching metadata…',
                    'downloaded_str': f'{peers} peers, {seeds} seeds',
                    'total_str': 'Connecting to swarm' if is_magnet else 'Waiting for peers',
                })
            else:
                pct = round(done * 100 / total, 1)
                remaining = total - done
                eta = _fmt_eta(remaining / speed) if speed > 0 else '—'
                progress_callback({
                    'task_id': task_id, 'status': 'downloading', 'percent': pct,
                    'seeds': seeds, 'peers': peers,
                    'speed_str': f'{_fmt_size(speed)}/s ({peers} peers, {seeds} seeds)',
                    'eta_str': eta,
                    'downloaded_str': _fmt_size(done),
                    'total_str': _fmt_size(total),
                })

            time.sleep(1)
    finally:
        _stop()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_active_torrent_tasks: Dict[str, threading.Event] = {}

def get_torrent_info(source: str) -> Dict[str, Any]:
    """Parse a .torrent file path or magnet: link and return metadata."""
    source = (source or '').strip()
    if not source:
        return {'success': False, 'error': 'No torrent source provided.'}

    try:
        if source.lower().endswith('.torrent') and os.path.isfile(source):
            meta = parse_torrent_file(source)
        elif source.startswith('magnet:'):
            meta = parse_magnet(source)
        else:
            return {'success': False, 'error': 'Please provide a valid .torrent file path or magnet: link.'}

        def fmt_size(b):
            if not b or b <= 0:
                return 'Unknown'
            for u in ('B', 'KB', 'MB', 'GB', 'TB'):
                if b < 1024:
                    return f'{b:.1f} {u}'
                b /= 1024
            return f'{b:.1f} PB'

        meta['clean_title'] = clean_torrent_title(meta.get('name', ''))
        meta['total_size_str'] = fmt_size(meta['total_size']) if meta['total_size'] else 'Unknown'
        for f in meta['files']:
            f['size_str'] = fmt_size(f['size']) if f.get('size') else ''

        meta['aria2c_available'] = _find_aria2c() is not None
        meta['aria2c_path'] = _find_aria2c() or ''

        return {'success': True, 'data': meta}
    except Exception as e:
        return {'success': False, 'error': f'This is not a valid torrent file or magnet link ({e}).'}


def start_torrent_download(
    source: str,
    output_dir: str,
    task_id: str,
    progress_callback: Callable,
) -> Dict[str, Any]:
    cancel_event = threading.Event()
    _active_torrent_tasks[task_id] = cancel_event

    def run():
        try:
            aria2c = _find_aria2c()
            if not aria2c:
                progress_callback({
                    'task_id': task_id,
                    'status': 'error',
                    'message': 'aria2c is missing. Please put aria2c.exe in the "bin" folder next to app.py.'
                })
                return

            if not source.startswith('magnet:'):
                if not os.path.isfile(source):
                    raise FileNotFoundError(f'Torrent file not found: {source}')
                parse_torrent_file(source)   # raises a clear error for corrupt files

            saved_path = _download_aria2c(source, output_dir, task_id, progress_callback, cancel_event)

            if not cancel_event.is_set():
                fn = os.path.basename(saved_path) if saved_path else 'Downloaded Torrent'
                size_str = 'Complete'
                try:
                    if saved_path and os.path.isfile(saved_path):
                        size_str = _fmt_size(os.path.getsize(saved_path))
                    elif saved_path and os.path.isdir(saved_path):
                        size_str = _fmt_size(sum(
                            os.path.getsize(os.path.join(dp, f))
                            for dp, _, fs in os.walk(saved_path) for f in fs))
                except Exception:
                    pass
                progress_callback({
                    'task_id': task_id,
                    'status': 'completed',
                    'percent': 100,
                    'filename': fn,
                    'filepath': saved_path or output_dir,
                    'filesize': size_str,
                    'message': 'Torrent download complete!'
                })
        except Exception as e:
            progress_callback({
                'task_id': task_id,
                'status': 'error',
                'message': str(e),
            })
        finally:
            _active_torrent_tasks.pop(task_id, None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return {'success': True, 'task_id': task_id}


def cancel_torrent_download(task_id: str):
    ev = _active_torrent_tasks.get(task_id)
    if ev:
        ev.set()
