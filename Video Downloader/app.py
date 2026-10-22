import os
import sys
import json
import threading
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import webview
from downloader import StreamDownloader, get_ffmpeg_path
from instagram_fetcher import (
    fetch_instagram_profile,
    start_instagram_download,
    cancel_instagram_download,
)
from torrent_downloader import get_torrent_info, start_torrent_download, cancel_torrent_download

downloader = StreamDownloader()
main_window = None

DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser('~'), 'Downloads', 'OmniStream')
os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)
EXTENSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extension')


# ---------------------------------------------------------------------------
# Local Bridge HTTP Server (for Chrome / Brave / Edge extension)
# ---------------------------------------------------------------------------
class ExtensionBridgeHandler(BaseHTTPRequestHandler):
    def _send_cors(self, code=200, content_type='application/json'):
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Content-Type', content_type)
        self.end_headers()

    def do_OPTIONS(self):
        self._send_cors(204)

    def do_GET(self):
        if self.path == '/status':
            self._send_cors(200)
            self.wfile.write(json.dumps({'status': 'online', 'app': 'OmniStream PRO', 'version': '2.0'}).encode('utf-8'))
        else:
            self._send_cors(404)
            self.wfile.write(b'{"error": "Not found"}')

    def do_POST(self):
        if self.path in ('/add-stream', '/intercept', '/add'):
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                data = json.loads(body)

                def notify():
                    try:
                        if main_window:
                            payload = json.dumps(data).replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
                            main_window.evaluate_js(f"window.onExtensionStreamCaptured({payload});")
                    except Exception:
                        pass

                threading.Thread(target=notify, daemon=True).start()
                self._send_cors(200)
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self._send_cors(500)
                self.wfile.write(json.dumps({'success': False, 'error': str(e)}).encode('utf-8'))
        else:
            self._send_cors(404)
            self.wfile.write(b'{"error": "Not found"}')

    def log_message(self, format, *args):
        pass


def start_extension_bridge(port=6890):
    try:
        # Threaded: a slow/idle browser connection must not block the extension's other requests.
        ThreadingHTTPServer.daemon_threads = True
        server = ThreadingHTTPServer(('127.0.0.1', port), ExtensionBridgeHandler)
        server.serve_forever()
    except Exception as e:
        print(f"Extension bridge notice: {e}")


# ---------------------------------------------------------------------------
# Python-to-JavaScript Api Bridge
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._is_maximized = False

    def get_system_info(self):
        from torrent_downloader import _find_aria2c
        return {
            'default_download_dir': DEFAULT_DOWNLOAD_DIR,
            'ffmpeg_path': get_ffmpeg_path(),
            'platform': sys.platform,
            'extension_dir': EXTENSION_DIR,
            'aria2c_available': _find_aria2c() is not None,
        }

    # Universal Stream Extraction
    def fetch_info(self, url, cookie_mode='none', cookie_file='', referer=''):
        try:
            url = (url or '').strip()
            if not url:
                return {'success': False, 'error': 'Please enter a valid video stream URL.'}
            info = downloader.extract_info(
                url=url,
                cookie_mode=cookie_mode,
                cookie_file=cookie_file,
                referer=referer
            )
            return {'success': True, 'data': info}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # Dedicated Instagram Profile & Media Extractor
    def fetch_instagram_profile(self, username, cookie_mode='none', cookie_file=''):
        try:
            res = fetch_instagram_profile(
                username=username,
                cookie_mode=cookie_mode,
                cookie_file=cookie_file
            )
            return res
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def download_instagram_item(self, media_url, media_type, page_url, output_dir, filename_hint, task_id):
        target_dir = (output_dir or '').strip() or DEFAULT_DOWNLOAD_DIR
        os.makedirs(target_dir, exist_ok=True)

        def on_progress(data):
            if main_window:
                payload = json.dumps(data)
                main_window.evaluate_js(f"window.onDownloadProgress({payload});")

        return start_instagram_download(
            media_url=media_url,
            media_type=media_type,
            page_url=page_url,
            output_dir=target_dir,
            filename_hint=filename_hint,
            task_id=task_id,
            progress_callback=on_progress,
        )

    def cancel_instagram_download(self, task_id):
        cancel_instagram_download(task_id)
        return {'success': True}

    # -----------------------------------------------------------------------
    # TORRENT API
    # -----------------------------------------------------------------------
    def parse_torrent(self, source):
        """Parse a .torrent file or magnet link and return metadata."""
        try:
            return get_torrent_info(source)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def choose_torrent_file(self):
        """Open file dialog to pick a .torrent file."""
        if not main_window:
            return ''
        files = main_window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=('Torrent files (*.torrent)', 'All files (*.*)')
        )
        if files and len(files) > 0:
            return files[0]
        return ''

    def upload_torrent_file(self, filename, base64_content):
        """Accepts base64 encoded torrent file content from drag-and-drop or file upload."""
        try:
            import base64
            import time
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
            os.makedirs(cache_dir, exist_ok=True)
            safe_name = os.path.basename(filename) or f"torrent_{int(time.time())}.torrent"
            if not safe_name.lower().endswith('.torrent'):
                safe_name += '.torrent'
            target_path = os.path.join(cache_dir, safe_name)

            if ',' in base64_content[:100] and base64_content.lstrip().startswith('data:'):
                base64_content = base64_content.split(',', 1)[1]
            raw_bytes = base64.b64decode(base64_content)
            with open(target_path, 'wb') as f:
                f.write(raw_bytes)

            info = get_torrent_info(target_path)
            if info.get('success'):
                return {
                    'success': True,
                    'filepath': target_path,
                    'data': info.get('data')
                }
            return info
        except Exception as e:
            return {'success': False, 'error': f"Failed to process torrent: {str(e)}"}

    def start_torrent_download(self, source, output_dir, task_id):
        target_dir = (output_dir or '').strip() or DEFAULT_DOWNLOAD_DIR
        os.makedirs(target_dir, exist_ok=True)

        def on_progress(data):
            if main_window:
                payload = json.dumps(data)
                main_window.evaluate_js(f"window.onDownloadProgress({payload});")

        return start_torrent_download(source, target_dir, task_id, on_progress)

    def cancel_torrent_download(self, task_id):
        cancel_torrent_download(task_id)
        return {'success': True}

    # -----------------------------------------------------------------------
    # Stream Download
    # -----------------------------------------------------------------------
    def start_download(self, url, format_id, strict_mp4, output_dir,
                       cookie_mode, cookie_file, referer, turbo_threads, task_id):
        target_dir = output_dir.strip() if output_dir and output_dir.strip() else DEFAULT_DOWNLOAD_DIR

        def run_dl():
            def on_progress(data):
                if main_window:
                    payload = json.dumps(data)
                    main_window.evaluate_js(f"window.onDownloadProgress({payload});")

            downloader.download_stream(
                url=url,
                format_id=format_id,
                output_dir=target_dir,
                strict_mp4=strict_mp4,
                task_id=task_id,
                cookie_mode=cookie_mode,
                cookie_file=cookie_file,
                referer=referer,
                turbo_threads=turbo_threads,
                progress_callback=on_progress
            )

        t = threading.Thread(target=run_dl, daemon=True)
        t.start()
        return {'success': True, 'task_id': task_id}

    def cancel_download(self, task_id):
        if task_id and str(task_id).startswith('tor_'):
            cancel_torrent_download(task_id)
        elif task_id and str(task_id).startswith('ig_'):
            cancel_instagram_download(task_id)
        else:
            downloader.cancel_download(task_id)
        return {'success': True}

    def choose_folder(self):
        if not main_window:
            return ''
        dirs = main_window.create_file_dialog(webview.FOLDER_DIALOG)
        if dirs and len(dirs) > 0:
            return dirs[0]
        return ''

    def choose_cookie_file(self):
        if not main_window:
            return ''
        files = main_window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=('Text files (*.txt)', 'All files (*.*)')
        )
        if files and len(files) > 0:
            return files[0]
        return ''

    def open_folder(self, path):
        try:
            if not path or not os.path.exists(path):
                path = DEFAULT_DOWNLOAD_DIR
            if os.path.isfile(path):
                subprocess.run(['explorer', '/select,', os.path.normpath(path)], check=False)
            else:
                os.startfile(os.path.normpath(path))
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def open_extension_folder(self):
        try:
            if os.path.exists(EXTENSION_DIR):
                os.startfile(os.path.normpath(EXTENSION_DIR))
                return {'success': True}
            return {'success': False, 'error': 'Extension directory not found'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def open_file(self, filepath):
        try:
            if filepath and os.path.exists(filepath):
                os.startfile(os.path.normpath(filepath))
                return {'success': True}
            return {'success': False, 'error': 'File does not exist'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # Frameless window controls
    def window_minimize(self):
        if main_window:
            main_window.minimize()

    def window_toggle_maximize(self):
        if not main_window:
            return
        try:
            if hasattr(main_window, 'state') and main_window.state:
                st = str(main_window.state).lower()
                if 'max' in st or 'full' in st:
                    main_window.restore()
                    self._is_maximized = False
                    return
            if self._is_maximized:
                main_window.restore()
                self._is_maximized = False
            else:
                main_window.maximize()
                self._is_maximized = True
        except Exception:
            if self._is_maximized:
                main_window.restore()
                self._is_maximized = False
            else:
                main_window.maximize()
                self._is_maximized = True

    def window_close(self):
        if main_window:
            main_window.destroy()


def main():
    global main_window
    api = Api()
    ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')
    index_html = os.path.join(ui_dir, 'index.html')

    bridge_thread = threading.Thread(target=start_extension_bridge, daemon=True)
    bridge_thread.start()

    main_window = webview.create_window(
        title='OmniStream PRO - Universal Video Downloader',
        url=index_html,
        width=1280,
        height=880,
        min_size=(980, 680),
        frameless=True,
        easy_drag=False,
        background_color='#0B0F17',
        js_api=api
    )

    webview.start(debug=False)


if __name__ == '__main__':
    main()
