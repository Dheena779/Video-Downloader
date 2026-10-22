// OmniStream - HLS playlist helpers (pure functions, no browser APIs).
// Used by the background worker and covered by unit tests.

export function resolveUrl(base, ref) {
  try {
    return new URL(ref, base).href;
  } catch (e) {
    return ref;
  }
}

// Parse "KEY=VALUE,KEY2="quoted,value"" attribute lists from a tag.
export function parseAttrs(str) {
  const out = {};
  const re = /([A-Z0-9-]+)=("([^"]*)"|[^,]*)/g;
  let m;
  while ((m = re.exec(str)) !== null) {
    out[m[1]] = m[3] !== undefined ? m[3] : m[2];
  }
  return out;
}

export function isPlaylistText(text) {
  return typeof text === 'string' && text.trimStart().startsWith('#EXTM3U');
}

// Master playlist -> list of variants (best first).
export function parseMaster(text, baseUrl) {
  const lines = text.split(/\r?\n/);
  const variants = [];
  let audioGroups = {};
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith('#EXT-X-MEDIA:')) {
      const a = parseAttrs(line.slice('#EXT-X-MEDIA:'.length));
      if (a.TYPE === 'AUDIO' && a.URI) {
        (audioGroups[a['GROUP-ID']] = audioGroups[a['GROUP-ID']] || []).push(resolveUrl(baseUrl, a.URI));
      }
    } else if (line.startsWith('#EXT-X-STREAM-INF:')) {
      const a = parseAttrs(line.slice('#EXT-X-STREAM-INF:'.length));
      let j = i + 1;
      while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith('#'))) j++;
      if (j >= lines.length) break;
      const url = resolveUrl(baseUrl, lines[j].trim());
      const bandwidth = parseInt(a['AVERAGE-BANDWIDTH'] || a.BANDWIDTH || '0', 10) || 0;
      let height = 0;
      if (a.RESOLUTION) {
        const r = a.RESOLUTION.split('x');
        height = parseInt(r[1], 10) || 0;
      }
      variants.push({
        url,
        bandwidth,
        height,
        resolution: a.RESOLUTION || '',
        codecs: a.CODECS || '',
        audioGroup: a.AUDIO || '',
        label: height ? `${height}p` : bandwidth ? `${Math.round(bandwidth / 1000)} kbps` : 'Stream',
      });
      i = j;
    }
  }
  variants.sort((x, y) => (y.height - x.height) || (y.bandwidth - x.bandwidth));
  // de-duplicate identical URLs
  const seen = new Set();
  const uniq = variants.filter((v) => (seen.has(v.url) ? false : (seen.add(v.url), true)));
  return { variants: uniq, audioGroups };
}

export function ivFromSequence(seq) {
  const iv = new Uint8Array(16);
  let n = BigInt(seq);
  for (let i = 15; i >= 0 && n > 0n; i--) {
    iv[i] = Number(n & 0xffn);
    n >>= 8n;
  }
  return iv;
}

export function hexToBytes(hex) {
  let h = hex.replace(/^0x/i, '');
  if (h.length % 2) h = '0' + h;
  const out = new Uint8Array(16);
  const bytes = new Uint8Array(h.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(h.substr(i * 2, 2), 16);
  // left-pad / keep last 16 bytes
  out.set(bytes.slice(-16), 16 - Math.min(16, bytes.length));
  return out;
}

// Media playlist -> segments (with key/map/byte-range info resolved per segment).
export function parseMedia(text, baseUrl) {
  const lines = text.split(/\r?\n/);
  let mediaSeq = 0;
  let endList = false;
  let key = null; // {method, uri, iv}
  let map = null; // {url, range}
  let pendingRange = null;
  let lastRangeEnd = 0;
  let isFmp4 = false;
  let playlistType = '';
  let targetDuration = 0;
  const segments = [];
  let duration = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;

    if (line.startsWith('#EXT-X-MEDIA-SEQUENCE:')) {
      mediaSeq = parseInt(line.split(':')[1], 10) || 0;
    } else if (line.startsWith('#EXT-X-TARGETDURATION:')) {
      targetDuration = parseInt(line.split(':')[1], 10) || 0;
    } else if (line.startsWith('#EXT-X-PLAYLIST-TYPE:')) {
      playlistType = line.split(':')[1].trim();
    } else if (line.startsWith('#EXT-X-ENDLIST')) {
      endList = true;
    } else if (line.startsWith('#EXT-X-KEY:')) {
      const a = parseAttrs(line.slice('#EXT-X-KEY:'.length));
      if (!a.METHOD || a.METHOD === 'NONE') key = null;
      else key = { method: a.METHOD, uri: a.URI ? resolveUrl(baseUrl, a.URI) : '', iv: a.IV || '' };
    } else if (line.startsWith('#EXT-X-MAP:')) {
      const a = parseAttrs(line.slice('#EXT-X-MAP:'.length));
      map = { url: resolveUrl(baseUrl, a.URI), range: a.BYTERANGE || '' };
      isFmp4 = true;
    } else if (line.startsWith('#EXT-X-BYTERANGE:')) {
      pendingRange = line.slice('#EXT-X-BYTERANGE:'.length).trim();
    } else if (line.startsWith('#EXTINF:')) {
      duration += parseFloat(line.slice('#EXTINF:'.length)) || 0;
      // find URI line
      let j = i + 1;
      while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith('#'))) {
        const l = lines[j].trim();
        if (l.startsWith('#EXT-X-BYTERANGE:')) pendingRange = l.slice('#EXT-X-BYTERANGE:'.length).trim();
        j++;
      }
      if (j >= lines.length) break;
      const segUrl = resolveUrl(baseUrl, lines[j].trim());
      let range = '';
      if (pendingRange) {
        const [lenStr, offStr] = pendingRange.split('@');
        const len = parseInt(lenStr, 10);
        const off = offStr !== undefined ? parseInt(offStr, 10) : lastRangeEnd;
        range = `bytes=${off}-${off + len - 1}`;
        lastRangeEnd = off + len;
        pendingRange = null;
      }
      const seq = mediaSeq + segments.length;
      segments.push({
        index: segments.length,
        seq,
        url: segUrl,
        range,
        key: key ? { ...key } : null,
      });
      if (/\.(m4s|mp4|cmfv|cmfa)(\?|#|$)/i.test(segUrl.split('?')[0] + '?')) isFmp4 = true;
      i = j;
    }
  }

  const unsupported = segments.some((s) => s.key && s.key.method !== 'AES-128')
    ? 'This stream uses SAMPLE-AES / DRM encryption and cannot be cached in the browser.'
    : '';

  return {
    segments,
    map,
    endList: endList || playlistType === 'VOD',
    isFmp4,
    duration,
    targetDuration,
    unsupported,
  };
}

export function sanitizeFilename(name, fallback = 'video') {
  let s = String(name || '')
    .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\.+/, '')
    .slice(0, 120)
    .trim();
  return s || fallback;
}
