"""
OmniStream PRO - Instagram Hub Backend
High-speed native extractor using curl_cffi Chrome impersonation to extract public profiles,
HD avatars, follower counts, full timeline media (Reels & Posts), Story Highlights — no login.
"""

import os
import re
import json
import html
import time
import threading
import urllib.parse
from typing import Dict, Any, List, Optional, Callable
from curl_cffi import requests as crequests

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-User': '?1',
    'Sec-Fetch-Dest': 'document',
    'Upgrade-Insecure-Requests': '1',
}


def clean_username(input_str: str) -> str:
    """Extracts raw username from @username, URL, or plain text."""
    s = (input_str or '').strip()
    if s.startswith('@'):
        s = s[1:]
    if 'instagram.com/' in s:
        parsed = urllib.parse.urlparse(s if '://' in s else 'https://' + s)
        parts = [p for p in parsed.path.strip('/').split('/') if p]
        if parts:
            if parts[0] in ('p', 'reel', 'tv', 'stories') and len(parts) > 1:
                return parts[1]
            return parts[0]
    s = s.split('?')[0].strip('/')
    return s


def _fmt_num(n) -> str:
    if not n:
        return '0'
    try:
        n = int(n)
        if n >= 1_000_000:
            return f'{n/1_000_000:.1f}M'
        if n >= 1_000:
            return f'{n/1_000:.1f}K'
        return f"{n:,}"
    except Exception:
        return str(n)


def _find_key_recursive(obj, target_key):
    """Recursively search for target_key in nested dicts/lists."""
    if isinstance(obj, dict):
        if target_key in obj:
            return obj[target_key]
        for v in obj.values():
            res = _find_key_recursive(v, target_key)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _find_key_recursive(item, target_key)
            if res is not None:
                return res
    return None


def _find_video_versions(obj, depth=0):
    """Recursively find the first video URL in video_versions list."""
    if depth > 40:
        return None
    if isinstance(obj, dict):
        vv = obj.get('video_versions')
        if isinstance(vv, list) and vv:
            u = vv[0].get('url', '')
            if u and 'http' in u:
                return u
        for v in obj.values():
            res = _find_video_versions(v, depth + 1)
            if res:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _find_video_versions(item, depth + 1)
            if res:
                return res
    return None


def _get_scripts(text: str) -> List[str]:
    return re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL)


# ---------------------------------------------------------------------------
# Direct media URL resolver (no yt-dlp, no login)
# ---------------------------------------------------------------------------
def resolve_instagram_media_url(page_url: str) -> Dict[str, Any]:
    """
    Given an Instagram post/reel page URL, fetch it natively with curl_cffi
    and extract the direct CDN media URL (mp4 for video, jpg for photo).
    Returns {'success': True, 'url': ..., 'ext': ..., 'filename': ...}
    """
    try:
        session = crequests.Session()
        r = session.get(page_url, impersonate='chrome124', headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return {'success': False, 'error': f'HTTP {r.status_code}'}

        scripts = _get_scripts(r.text)

        # 1. Try video_versions (works for reels, videos, story highlights)
        for sc in scripts:
            if 'video_versions' in sc:
                try:
                    d = json.loads(sc)
                    url = _find_video_versions(d)
                    if url:
                        code = re.search(r'/(?:p|reel|tv|highlights)/([A-Za-z0-9_-]+)', page_url)
                        fname = f"instagram_{code.group(1) if code else 'video'}.mp4"
                        return {'success': True, 'url': url, 'ext': 'mp4', 'filename': fname}
                except Exception:
                    pass

                # Fast regex fallback for deeply nested or stringified JSON
                m = re.search(r'"video_versions"\s*:\s*\[\s*\{[^}]*?"url"\s*:\s*"([^"]+)"', sc)
                if m:
                    raw_url = m.group(1).replace(r'\/', '/')
                    code = re.search(r'/(?:p|reel|tv|highlights)/([A-Za-z0-9_-]+)', page_url)
                    fname = f"instagram_{code.group(1) if code else 'video'}.mp4"
                    return {'success': True, 'url': raw_url, 'ext': 'mp4', 'filename': fname}

        # 2. Try image (photo posts — look for display_uri or image_versions2)
        for sc in scripts:
            if 'display_uri' in sc or 'image_versions2' in sc:
                try:
                    d = json.loads(sc)

                    def find_img(obj, depth=0):
                        if depth > 40:
                            return None
                        if isinstance(obj, dict):
                            iv2 = obj.get('image_versions2')
                            if isinstance(iv2, dict) and 'candidates' in iv2 and iv2['candidates']:
                                u = iv2['candidates'][0].get('url', '')
                                if u and 'http' in u:
                                    return u
                            du = obj.get('display_uri', '')
                            if du and 'http' in str(du):
                                return du
                            for v in obj.values():
                                res = find_img(v, depth + 1)
                                if res:
                                    return res
                        elif isinstance(obj, list):
                            for item in obj:
                                res = find_img(item, depth + 1)
                                if res:
                                    return res
                        return None

                    url = find_img(d)
                    if url:
                        code = re.search(r'/(?:p|reel|tv|highlights)/([A-Za-z0-9_-]+)', page_url)
                        fname = f"instagram_{code.group(1) if code else 'photo'}.jpg"
                        return {'success': True, 'url': url, 'ext': 'jpg', 'filename': fname}
                except Exception:
                    pass

                # Fast regex fallback for display_uri
                m = re.search(r'"display_uri"\s*:\s*"([^"]+)"', sc)
                if m:
                    raw_url = m.group(1).replace(r'\/', '/')
                    code = re.search(r'/(?:p|reel|tv|highlights)/([A-Za-z0-9_-]+)', page_url)
                    fname = f"instagram_{code.group(1) if code else 'photo'}.jpg"
                    return {'success': True, 'url': raw_url, 'ext': 'jpg', 'filename': fname}

        return {'success': False, 'error': 'Could not find media URL in page. Post may require login.'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Direct media downloader (handles both photos and videos)
# ---------------------------------------------------------------------------
def download_instagram_media(
    media_url: str,
    media_type: str,       # 'photo', 'video', 'reel', 'highlight'
    page_url: str,
    output_dir: str,
    filename_hint: str,
    task_id: str,
    progress_callback: Callable,
) -> None:
    """
    Download a single Instagram media item:
    - Photo: direct CDN JPG URL → save as .jpg
    - Video/Reel: resolve direct mp4 from page_url → save as .mp4
    Calls progress_callback with standardized progress dicts.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)

        progress_callback({'task_id': task_id, 'status': 'downloading',
                           'percent': 5, 'speed_str': 'Connecting…',
                           'eta_str': '—', 'downloaded_str': '0 B', 'total_str': '—'})

        direct_url = media_url
        ext = 'jpg' if media_type == 'photo' else 'mp4'

        # For videos/reels/highlights: resolve the actual CDN video URL from page
        if media_type in ('video', 'reel', 'highlight') and page_url:
            progress_callback({'task_id': task_id, 'status': 'downloading',
                               'percent': 10, 'speed_str': 'Resolving video URL…',
                               'eta_str': '—', 'downloaded_str': '—', 'total_str': '—'})
            resolved = resolve_instagram_media_url(page_url)
            if resolved.get('success'):
                direct_url = resolved['url']
                ext = resolved['ext']
            elif not re.search(r'\.(?:mp4|jpg|jpeg|webp|png)(?:\?|$)', media_url or '', re.I):
                # media_url is just the post page - downloading it would save an HTML
                # file with an .mp4 name.  Fail with the real reason instead.
                raise RuntimeError(
                    resolved.get('error') or 'Could not find the video on this post. '
                    'It may be private or require login (set Cookie Auth and retry).'
                )
            else:
                ext = 'mp4'

        # Build a safe filename
        safe_hint = re.sub(r'[^\w\-.]', '_', filename_hint or 'instagram_media')
        if not safe_hint.endswith(f'.{ext}'):
            safe_hint = safe_hint[:60] + f'.{ext}'
        out_path = os.path.join(output_dir, safe_hint)

        # Stream-download the file
        progress_callback({'task_id': task_id, 'status': 'downloading',
                           'percent': 20, 'speed_str': 'Downloading…',
                           'eta_str': '—', 'downloaded_str': '0 B', 'total_str': '—'})

        session = crequests.Session()
        resp = session.get(
            direct_url,
            impersonate='chrome124',
            headers={
                'Referer': 'https://www.instagram.com/',
                'Accept': '*/*',
            },
            timeout=60,
            stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'HTTP {resp.status_code} downloading media')
        if 'text/html' in (resp.headers.get('content-type') or '').lower():
            raise RuntimeError('Instagram returned a web page instead of the media file (login required?).')

        total_bytes = int(resp.headers.get('content-length', 0))
        downloaded = 0
        start = time.time()

        with open(out_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if _ig_cancel_flags.get(task_id):
                    f.close()
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                    progress_callback({'task_id': task_id, 'status': 'cancelled',
                                       'message': 'Download cancelled.'})
                    return
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.time() - start, 0.001)
                    speed = downloaded / elapsed
                    pct = int(downloaded * 100 / total_bytes) if total_bytes else 50

                    def _fmt(b):
                        for u in ('B', 'KB', 'MB', 'GB'):
                            if b < 1024:
                                return f'{b:.1f} {u}'
                            b /= 1024
                        return f'{b:.1f} GB'

                    progress_callback({
                        'task_id': task_id,
                        'status': 'downloading',
                        'percent': max(pct, 10),
                        'speed_str': f'{_fmt(speed)}/s',
                        'eta_str': f'{int((total_bytes - downloaded) / speed)}s' if speed and total_bytes else '—',
                        'downloaded_str': _fmt(downloaded),
                        'total_str': _fmt(total_bytes) if total_bytes else '—',
                    })

        filesize_str = f'{downloaded / 1024 / 1024:.1f} MB' if downloaded > 0 else ''
        progress_callback({
            'task_id': task_id,
            'status': 'completed',
            'filename': os.path.basename(out_path),
            'filepath': out_path,
            'filesize': filesize_str,
            'message': 'Instagram media downloaded!'
        })

    except Exception as e:
        progress_callback({
            'task_id': task_id,
            'status': 'error',
            'message': str(e),
        })


# ---------------------------------------------------------------------------
# Native Public Instagram Extractor (No Login Required)
# ---------------------------------------------------------------------------
def _fetch_native_instagram(uname: str) -> Dict[str, Any] | None:
    try:
        url = f"https://www.instagram.com/{uname}/"
        s = crequests.Session()
        r = s.get(url, impersonate='chrome124', headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None

        text = r.text
        scripts = _get_scripts(text)

        user_info = {}
        media_items = []
        highlights = []

        for sc in scripts:
            # 1. Profile metadata
            if 'ProfileRootContentQuery' in sc and 'xig_user_by_username' in sc:
                try:
                    d = json.loads(sc)
                    u = _find_key_recursive(d, 'xig_user_by_username')
                    if u and isinstance(u, dict):
                        user_info = u
                except Exception:
                    pass

            # 2. Timeline media (Posts & Reels)
            if 'ProfilePostsTabContentQuery' in sc and 'polaris_ordered_timeline_connection' in sc:
                try:
                    d = json.loads(sc)
                    tl = _find_key_recursive(d, 'polaris_ordered_timeline_connection')
                    if tl and isinstance(tl, dict) and 'edges' in tl:
                        for edge in tl['edges']:
                            node = edge.get('node', {})
                            code = node.get('code')
                            if not code:
                                continue

                            is_vid = (node.get('media_type') == 2) or (node.get('product_type') == 'clips')
                            is_reel = (node.get('product_type') == 'clips')
                            cap_obj = node.get('caption') or {}
                            cap_text = cap_obj.get('text', '') if isinstance(cap_obj, dict) else ''
                            thumb = node.get('display_uri') or ''
                            if is_reel:
                                page_url = f"https://www.instagram.com/reel/{code}/"
                                mtype = 'reel'
                                type_label = 'REEL'
                            elif is_vid:
                                page_url = f"https://www.instagram.com/p/{code}/"
                                mtype = 'video'
                                type_label = 'VIDEO'
                            else:
                                page_url = f"https://www.instagram.com/p/{code}/"
                                mtype = 'photo'
                                type_label = 'PHOTO'

                            media_items.append({
                                'id': code,
                                'shortcode': code,
                                'type': mtype,
                                'type_label': type_label,
                                'thumbnail': thumb,
                                # For photos: download_url = direct CDN jpg (thumb IS the full image)
                                # For videos/reels: download_url = page_url (we resolve the mp4 at download time)
                                'download_url': thumb if mtype == 'photo' else page_url,
                                'page_url': page_url,
                                'caption': cap_text[:140] + ('…' if len(cap_text) > 140 else ''),
                                'likes': 'Public',
                                'comments': 'Public',
                                'date': '',
                                'ext': 'mp4' if is_vid or is_reel else 'jpg',
                            })
                except Exception:
                    pass

            # 3. Story Highlights (lox_highlights_connection)
            if 'lox_highlights_connection' in sc:
                try:
                    d = json.loads(sc)
                    lox = _find_key_recursive(d, 'lox_highlights_connection')
                    if lox and isinstance(lox, dict) and 'edges' in lox:
                        for edge in lox['edges']:
                            node = edge.get('node', {})
                            hid = node.get('id', '')
                            title = node.get('title', 'Highlight')
                            cover = node.get('cover_media_cropped_thumbnail_url', '')
                            if hid:
                                highlights.append({
                                    'id': hid,
                                    'title': title,
                                    'cover': cover,
                                    'page_url': f'https://www.instagram.com/stories/highlights/{hid}/',
                                    'type': 'highlight',
                                    'type_label': 'HIGHLIGHT',
                                    'ext': 'mp4',
                                })
                except Exception:
                    pass

        # If user_info wasn't in Relay, parse from OG meta tags
        title_m = re.search(r'<meta property="og:title" content="([^"]+)"', text)
        desc_m  = re.search(r'<meta property="og:description" content="([^"]+)"', text)
        pic_m   = re.search(r'<meta property="og:image" content="([^"]+)"', text)

        raw_title = html.unescape(title_m.group(1)) if title_m else uname
        raw_desc  = html.unescape(desc_m.group(1)) if desc_m else ""
        raw_pic   = html.unescape(pic_m.group(1)) if pic_m else ""

        full_name = user_info.get('full_name') or (raw_title.split('(')[0].strip() if '(' in raw_title else raw_title)
        bio = user_info.get('biography') or raw_desc
        avatar = user_info.get('profile_pic_url') or raw_pic

        followers = _fmt_num(user_info.get('follower_count'))
        following = _fmt_num(user_info.get('following_count'))

        if followers == '0' and 'Followers' in raw_desc:
            try:
                parts = raw_desc.split(' - ')[0].split(',')
                for p in parts:
                    if 'Followers' in p:
                        followers = p.replace('Followers', '').strip()
                    elif 'Following' in p:
                        following = p.replace('Following', '').strip()
            except Exception:
                pass

        if not avatar and not media_items and not user_info:
            return None

        return {
            'success': True,
            'data': {
                'username': user_info.get('username') or uname,
                'full_name': full_name,
                'biography': bio,
                'profile_pic_url': avatar,
                'followers': followers if followers != '0' else 'Public',
                'following': following if following != '0' else 'Public',
                'posts_count': str(len(media_items)) if media_items else (str(user_info.get('all_media_count')) if user_info.get('all_media_count') else 'View'),
                'is_verified': bool(user_info.get('is_verified', False)),
                'is_private': bool(user_info.get('is_private', False)),
                'media': media_items,
                'highlights': highlights,
                'method': 'native_ssr'
            }
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_instagram_profile(
    username: str,
    cookie_mode: str = 'none',
    cookie_file: str = ''
) -> Dict[str, Any]:
    s_raw = (username or '').strip()
    if '/reel/' in s_raw or '/p/' in s_raw or '/tv/' in s_raw:
        resolved = resolve_instagram_media_url(s_raw)
        if resolved.get('success'):
            is_reel = '/reel/' in s_raw
            ext = resolved.get('ext', 'mp4')
            mtype = 'reel' if is_reel else ('video' if ext == 'mp4' else 'photo')
            code = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)', s_raw)
            shortcode = code.group(1) if code else 'item'
            return {
                'success': True,
                'data': {
                    'username': f'{mtype}_{shortcode}',
                    'full_name': f'Instagram {mtype.capitalize()}',
                    'biography': s_raw,
                    'profile_pic_url': resolved.get('url') if ext in ('jpg', 'png') else '',
                    'followers': '—',
                    'following': '—',
                    'posts_count': '1',
                    'is_verified': False,
                    'media': [{
                        'id': shortcode,
                        'shortcode': shortcode,
                        'type': mtype,
                        'type_label': mtype.upper(),
                        'thumbnail': resolved.get('url') if ext in ('jpg', 'png') else '',
                        'download_url': resolved.get('url'),
                        'page_url': s_raw,
                        'caption': f'Instagram {mtype.capitalize()} ({shortcode})',
                        'likes': 'Direct Link',
                        'comments': 'Public',
                        'date': 'Ready',
                        'ext': ext,
                    }],
                    'highlights': [],
                    'method': 'direct_post'
                }
            }

    uname = clean_username(username)
    if not uname:
        return {'success': False, 'error': 'Please enter a valid Instagram username or profile URL.'}

    # Primary: Native public extraction (Instant, no login required)
    result = _fetch_native_instagram(uname)
    if result and result.get('success'):
        return result

    # Secondary: Try instaloader with cookies if cookie_mode is enabled
    if cookie_mode in ('firefox', 'edge', 'chrome', 'brave', 'opera'):
        try:
            import instaloader
            L = instaloader.Instaloader(
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
            )
            try:
                import browser_cookie3
                cj = getattr(browser_cookie3, cookie_mode)(domain_name='instagram.com')
                L.context._session.cookies = cj
            except Exception:
                pass

            profile = instaloader.Profile.from_username(L.context, uname)
            media_items = []
            count = 0
            for post in profile.get_posts():
                is_vid = post.is_video
                is_reel = is_vid and post.typename == 'GraphVideo'
                mtype = 'reel' if is_reel else ('video' if is_vid else 'photo')
                page_url = f"https://www.instagram.com/p/{post.shortcode}/"
                media_items.append({
                    'id': post.shortcode,
                    'shortcode': post.shortcode,
                    'type': mtype,
                    'type_label': mtype.upper(),
                    'thumbnail': post.url,
                    'download_url': post.url if mtype == 'photo' else page_url,
                    'page_url': page_url,
                    'caption': (post.caption or '')[:140] + ('…' if len(post.caption or '') > 140 else ''),
                    'likes': _fmt_num(post.likes or 0),
                    'comments': _fmt_num(post.comments or 0),
                    'date': post.date_utc.strftime('%b %d, %Y') if post.date_utc else '',
                    'ext': 'mp4' if is_vid else 'jpg',
                })
                count += 1
                if count >= 18:
                    break

            return {
                'success': True,
                'data': {
                    'username': profile.username,
                    'full_name': profile.full_name or profile.username,
                    'biography': profile.biography or '',
                    'profile_pic_url': profile.profile_pic_url,
                    'followers': _fmt_num(profile.followers or 0),
                    'following': _fmt_num(profile.followees or 0),
                    'posts_count': str(len(media_items)),
                    'is_verified': profile.is_verified,
                    'media': media_items,
                    'highlights': [],
                    'method': 'instaloader'
                }
            }
        except Exception:
            pass

    return {
        'success': False,
        'error': f"Could not load Instagram profile for '@{uname}'. Please check that the account is public."
    }


# ---------------------------------------------------------------------------
# Active download task tracking
# ---------------------------------------------------------------------------
_ig_cancel_flags: Dict[str, bool] = {}


def start_instagram_download(
    media_url: str,
    media_type: str,
    page_url: str,
    output_dir: str,
    filename_hint: str,
    task_id: str,
    progress_callback: Callable,
) -> Dict[str, Any]:
    """Start a background Instagram media download thread."""
    _ig_cancel_flags[task_id] = False

    def _run():
        download_instagram_media(
            media_url=media_url,
            media_type=media_type,
            page_url=page_url,
            output_dir=output_dir,
            filename_hint=filename_hint,
            task_id=task_id,
            progress_callback=progress_callback,
        )
        _ig_cancel_flags.pop(task_id, None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {'success': True, 'task_id': task_id}


def cancel_instagram_download(task_id: str):
    _ig_cancel_flags[task_id] = True
