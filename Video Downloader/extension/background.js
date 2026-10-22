// OmniStream Extension - Background Service Worker (Manifest V3, ES module)
//
//  * Sniffs video requests (HLS / DASH / direct files) per tab.
//  * For HLS it can PRE-CACHE every segment inside the extension (IndexedDB),
//    so pressing "Download" later only has to stitch the stored pieces together.
//  * Streams are still forwarded to the OmniStream desktop app on 127.0.0.1:6890.

import {
  isPlaylistText, parseMaster, parseMedia, resolveUrl,
  hexToBytes, ivFromSequence, sanitizeFilename,
} from './hls.js';

const LOCAL_BRIDGE_URL = 'http://127.0.0.1:6890';
const SEGMENT_CONCURRENCY = 6;
const SEGMENT_RETRIES = 4;
const CACHE_MAX_AGE_MS = 6 * 60 * 60 * 1000;

// ---------------------------------------------------------------------------
// State (kept in memory AND mirrored to chrome.storage.session, because an MV3
// service worker is shut down after ~30s idle and would otherwise forget
// everything it had detected).
// ---------------------------------------------------------------------------
const tabStreams = new Map();   // tabId -> [stream]
const jobs = new Map();         // jobId -> job
const jobRuntime = new Map();   // jobId -> { abort: AbortController, keys: Map }
const ignoredVariantUrls = new Map(); // tabId -> Set(url)
const pendingAdds = new Set();  // `${tabId}|${manifestKey}` currently being analysed
let settings = { autoCache: true };
let ruleCounter = 1000;

let persistTimer = null;
function persistSoon() {
  if (persistTimer) return;
  persistTimer = setTimeout(async () => {
    persistTimer = null;
    try {
      await chrome.storage.session.set({
        os_tabStreams: Object.fromEntries([...tabStreams].map(([k, v]) => [k, v])),
        os_jobs: Object.fromEntries(jobs),
        os_ignored: Object.fromEntries([...ignoredVariantUrls].map(([k, v]) => [k, [...v]])),
      });
    } catch (e) { /* session storage unavailable - memory only */ }
  }, 300);
}

const ready = (async () => {
  try {
    const local = await chrome.storage.local.get('os_settings');
    if (local.os_settings) settings = { ...settings, ...local.os_settings };
    const s = await chrome.storage.session.get(['os_tabStreams', 'os_jobs', 'os_ignored']);
    for (const [k, v] of Object.entries(s.os_tabStreams || {})) tabStreams.set(Number(k), v);
    for (const [k, v] of Object.entries(s.os_ignored || {})) ignoredVariantUrls.set(Number(k), new Set(v));
    for (const [k, v] of Object.entries(s.os_jobs || {})) {
      jobs.set(k, v);
      // A job that was mid-flight when the worker died is resumed (segments already
      // stored in IndexedDB are skipped).
      if (v.status === 'fetching') {
        v.status = 'queued';
        setTimeout(() => runJob(v.id).catch(() => {}), 500);
      }
    }
  } catch (e) { /* ignore */ }
  purgeOldJobs().catch(() => {});
})();

// ---------------------------------------------------------------------------
// IndexedDB segment store
// ---------------------------------------------------------------------------
let dbPromise = null;
function openDb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open('omnistream-cache', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('segs');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  return dbPromise;
}
const segKey = (jobId, idx) => `${jobId}/${String(idx).padStart(7, '0')}`;

async function idbPut(key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('segs', 'readwrite');
    tx.objectStore('segs').put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('IndexedDB write aborted (disk full?)'));
  });
}
async function idbKeys(jobId) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const range = IDBKeyRange.bound(`${jobId}/`, `${jobId}/￿`);
    const req = db.transaction('segs').objectStore('segs').getAllKeys(range);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbDeleteJob(jobId) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('segs', 'readwrite');
    tx.objectStore('segs').delete(IDBKeyRange.bound(`${jobId}/`, `${jobId}/￿`));
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ---------------------------------------------------------------------------
// Stream classification
// ---------------------------------------------------------------------------
function pathOf(url) {
  try { return new URL(url).pathname.toLowerCase(); } catch (e) { return (url || '').toLowerCase().split('?')[0]; }
}

function classify(url, contentType, contentLength, status) {
  const u = (url || '').toLowerCase();
  const p = pathOf(url);
  const ct = (contentType || '').toLowerCase();

  if (/analytics|doubleclick|telemetry|google-analytics|googletagmanager/.test(u)) return null;
  if (status && status >= 400) return null;

  // HLS / DASH manifests - highest priority
  if (/\.m3u8$/.test(p) || /mpegurl/.test(ct)) {
    return { type: 'HLS Stream (.m3u8)', label: 'HLS Playlist', priority: 1, kind: 'hls' };
  }
  if (/\.mpd$/.test(p) || /dash\+xml/.test(ct)) {
    return { type: 'DASH Stream (.mpd)', label: 'DASH Manifest', priority: 1, kind: 'dash' };
  }

  // Individual segments must be recognised BEFORE the generic video/* check,
  // because segments are served as video/mp2t or video/mp4 as well.
  if (/\.(ts|m4s|aac|cmfv|cmfa)$/.test(p) || /video\/mp2t|audio\/mp2t/.test(ct)) {
    return { type: 'Segment', label: 'HLS Segment', priority: 9, kind: 'segment' };
  }

  // Instagram CDN video
  if (u.includes('cdninstagram.com') && (/\.mp4$/.test(p) || ct.includes('video'))) {
    return { type: 'Instagram Video', label: 'Instagram Reel/Video', priority: 1, kind: 'direct' };
  }

  // Direct video files
  if (/\.(mp4|webm|mkv|mov|m4v)$/.test(p) || ct.startsWith('video/')) {
    if (contentLength && contentLength < 50000) return null; // tracker / teaser
    const m = p.match(/\.(mp4|webm|mkv|mov|m4v)$/);
    const ext = m ? m[1].toUpperCase() : 'MP4';
    return { type: `${ext} Video`, label: `${ext} Direct Video`, priority: 2, kind: 'direct' };
  }
  return null;
}

function manifestKey(url) {
  try { const u = new URL(url); return u.origin + u.pathname; } catch (e) { return url; }
}

// ---------------------------------------------------------------------------
// Tab lifecycle
// ---------------------------------------------------------------------------
async function dropTab(tabId) {
  await ready;
  const list = tabStreams.get(tabId) || [];
  for (const s of list) if (s.jobId) removeJob(s.jobId, { keepIfSaving: true }).catch(() => {});
  tabStreams.delete(tabId);
  ignoredVariantUrls.delete(tabId);
  persistSoon();
}

chrome.tabs.onRemoved.addListener((tabId) => { dropTab(tabId); });
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'loading' && changeInfo.url) {
    dropTab(tabId);
    chrome.action.setBadgeText({ text: '', tabId }).catch(() => {});
  }
});

function updateBadge(tabId) {
  const list = tabStreams.get(tabId) || [];
  chrome.action.setBadgeText({ text: list.length ? String(list.length) : '', tabId }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: '#06B6D4', tabId }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Request sniffing
// ---------------------------------------------------------------------------
function postToBridge(payload) {
  fetch(`${LOCAL_BRIDGE_URL}/add-stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).catch(() => {});
}

chrome.webRequest.onHeadersReceived.addListener(
  (details) => { handleResponse(details).catch(() => {}); },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

async function handleResponse(details) {
  if (details.tabId < 0) return; // our own fetches and background noise
  await ready;

  let contentType = '';
  let contentLength = 0;
  for (const h of details.responseHeaders || []) {
    const n = (h.name || '').toLowerCase();
    if (n === 'content-type') contentType = h.value || '';
    else if (n === 'content-range') {
      const m = /\/(\d+)$/.exec(h.value || '');
      if (m) contentLength = parseInt(m[1], 10);       // total size, not chunk size
    } else if (n === 'content-length' && !contentLength) contentLength = parseInt(h.value || '0', 10);
  }

  const info = classify(details.url, contentType, contentLength, details.statusCode);
  if (!info) return;

  if (!tabStreams.has(details.tabId)) tabStreams.set(details.tabId, []);
  const list = tabStreams.get(details.tabId);

  if (info.kind === 'segment') {
    // A segment tells us a stream exists but we only expose real manifests.
    // Try the common playlist names next to the segment, and only keep a
    // candidate that really is a playlist.
    if (!list.some((s) => s.kind === 'hls' || s.kind === 'dash')) {
      await tryReconstructManifest(details.tabId, details.url);
    }
    return;
  }

  if (ignoredVariantUrls.get(details.tabId)?.has(details.url)) return;
  const key = manifestKey(details.url);
  const pendKey = `${details.tabId}|${key}`;
  if (pendingAdds.has(pendKey)) return;
  if (list.some((s) => s.url === details.url || (s.kind !== 'direct' && manifestKey(s.url) === key))) return;

  pendingAdds.add(pendKey);
  try {
    await addStream(details.tabId, {
      url: details.url,
      type: info.type,
      label: info.label,
      priority: info.priority,
      kind: info.kind,
      size: contentLength > 0 ? (contentLength / (1024 * 1024)).toFixed(2) + ' MB' : 'Full Stream',
    });
  } finally {
    pendingAdds.delete(pendKey);
  }
}

async function tryReconstructManifest(tabId, segmentUrl) {
  let base;
  try {
    const u = new URL(segmentUrl);
    base = u.origin + u.pathname.replace(/\/[^/]*$/, '/');
  } catch (e) { return; }
  const search = (() => { try { return new URL(segmentUrl).search; } catch (e) { return ''; } })();
  for (const name of ['index.m3u8', 'master.m3u8', 'playlist.m3u8']) {
    const cand = base + name + search;
    try {
      const r = await fetch(cand, { credentials: 'include' });
      if (!r.ok) continue;
      const t = await r.text();
      if (isPlaylistText(t)) {
        const list = tabStreams.get(tabId) || [];
        if (!list.some((s) => manifestKey(s.url) === manifestKey(cand))) {
          await addStream(tabId, {
            url: cand, type: 'HLS Stream (.m3u8)', label: 'HLS Playlist',
            priority: 1, kind: 'hls', size: 'Full Stream',
          });
        }
        return;
      }
    } catch (e) { /* try next */ }
  }
}

async function addStream(tabId, partial) {
  let tab = null;
  try { tab = await chrome.tabs.get(tabId); } catch (e) { /* tab gone */ }
  const stream = {
    id: 'stream_' + Date.now() + '_' + Math.floor(Math.random() * 1000),
    timestamp: Date.now(),
    pageTitle: (tab && tab.title) || 'Web Stream',
    pageUrl: (tab && tab.url) || '',
    jobId: null,
    ...partial,
  };

  // Inspect HLS playlists: master playlists hide their variant playlists
  // (otherwise the same video shows up several times), and we learn whether
  // the stream is VOD, live or encrypted.
  if (stream.kind === 'hls') {
    try {
      const a = await withHeaders(stream.pageUrl, [stream.url], () => analyzeManifest(stream.url));
      stream.hls = a;
      if (a.isMaster) {
        const ign = ignoredVariantUrls.get(tabId) || new Set();
        a.variants.forEach((v) => ign.add(v.url));
        ignoredVariantUrls.set(tabId, ign);
        const cur = tabStreams.get(tabId) || [];
        tabStreams.set(tabId, cur.filter((s) => !ign.has(s.url)));
      }
    } catch (e) {
      stream.hls = { error: String(e.message || e) };
    }
  }

  // The master playlist may have been analysed while this one was in flight.
  // No await between this check and the push, so the two can't interleave.
  if (ignoredVariantUrls.get(tabId)?.has(stream.url)) return;

  const list = tabStreams.get(tabId) || [];
  if (!tabStreams.has(tabId)) tabStreams.set(tabId, list);
  list.push(stream);
  list.sort((a, b) => a.priority - b.priority);
  updateBadge(tabId);
  persistSoon();

  postToBridge({
    url: stream.url, pageUrl: stream.pageUrl, pageTitle: stream.pageTitle,
    streamType: stream.type, streamLabel: stream.label,
  });

  if (settings.autoCache && stream.kind === 'hls' && stream.hls && !stream.hls.error && stream.hls.canCache) {
    startCache(tabId, stream.id).catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Manifest analysis
// ---------------------------------------------------------------------------
async function fetchText(url, signal) {
  const r = await fetch(url, { credentials: 'include', cache: 'no-store', signal });
  if (!r.ok) throw new Error(`HTTP ${r.status} while loading playlist`);
  return r.text();
}

async function analyzeManifest(url) {
  const text = await fetchText(url);
  if (!isPlaylistText(text)) throw new Error('Not a valid HLS playlist');
  if (/#EXT-X-STREAM-INF/.test(text)) {
    const m = parseMaster(text, url);
    return {
      isMaster: true,
      variants: m.variants.map((v) => ({ url: v.url, label: v.label, bandwidth: v.bandwidth, height: v.height })),
      hasSeparateAudio: m.variants.some((v) => v.audioGroup && m.audioGroups[v.audioGroup]),
      canCache: m.variants.length > 0,
    };
  }
  const media = parseMedia(text, url);
  return {
    isMaster: false,
    variants: [],
    segments: media.segments.length,
    duration: media.duration,
    live: !media.endList,
    canCache: media.segments.length > 0 && !media.unsupported && media.endList,
    unsupported: media.unsupported,
  };
}

// ---------------------------------------------------------------------------
// Header rules: make the extension's own fetches look like the page made them
// (Referer / Origin), otherwise many CDNs answer 403.
// ---------------------------------------------------------------------------
async function addHeaderRule(pageUrl, hosts) {
  if (!pageUrl || !hosts.length || !chrome.declarativeNetRequest) return 0;
  let origin = '';
  try { origin = new URL(pageUrl).origin; } catch (e) { return 0; }
  const id = ++ruleCounter;
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      addRules: [{
        id,
        priority: 1,
        action: {
          type: 'modifyHeaders',
          requestHeaders: [
            { header: 'Referer', operation: 'set', value: pageUrl },
            { header: 'Origin', operation: 'set', value: origin },
          ],
        },
        condition: {
          requestDomains: hosts,
          tabIds: [chrome.tabs.TAB_ID_NONE],
          resourceTypes: ['xmlhttprequest', 'other'],
        },
      }],
    });
    return id;
  } catch (e) { return 0; }
}
function hostOf(url) { try { return new URL(url).hostname; } catch (e) { return ''; } }

// Run fn while Referer/Origin are forced for requests the extension makes to these URLs' hosts.
async function withHeaders(pageUrl, urls, fn) {
  const hosts = [...new Set(urls.map(hostOf).filter(Boolean))];
  const id = await addHeaderRule(pageUrl, hosts);
  try { return await fn(); } finally { await removeHeaderRule(id); }
}

async function removeHeaderRule(id) {
  if (!id) return;
  try { await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [id] }); } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Cache jobs
// ---------------------------------------------------------------------------
function publicJob(j) {
  if (!j) return null;
  return {
    id: j.id, status: j.status, total: j.total, done: j.done, bytes: j.bytes,
    error: j.error || '', filename: j.filename, autoSave: !!j.autoSave,
    quality: j.quality || '', ext: j.ext || 'ts', warning: j.warning || '',
    startedAt: j.startedAt,
  };
}

async function startCache(tabId, streamId, variantUrl) {
  await ready;
  const list = tabStreams.get(tabId) || [];
  const stream = list.find((s) => s.id === streamId);
  if (!stream) throw new Error('Stream not found');
  if (stream.jobId && jobs.has(stream.jobId)) {
    const ex = jobs.get(stream.jobId);
    if (!['error', 'cancelled'].includes(ex.status)) return publicJob(ex);
    await removeJob(ex.id);
  }
  const id = 'job_' + Date.now() + '_' + Math.floor(Math.random() * 1000);
  const job = {
    id, tabId, streamId,
    manifestUrl: stream.url,
    variantUrl: variantUrl || '',
    pageUrl: stream.pageUrl,
    filename: sanitizeFilename(stream.pageTitle),
    status: 'queued', total: 0, done: 0, bytes: 0, error: '',
    autoSave: false, ext: 'ts', quality: '', startedAt: Date.now(),
  };
  jobs.set(id, job);
  stream.jobId = id;
  persistSoon();
  runJob(id).catch(() => {});
  return publicJob(job);
}

let keepAliveTimer = null;
function keepAlive(on) {
  if (on && !keepAliveTimer) {
    keepAliveTimer = setInterval(() => { chrome.runtime.getPlatformInfo(() => {}); }, 20000);
  } else if (!on && keepAliveTimer) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
}
function anyJobRunning() {
  for (const j of jobs.values()) if (['queued', 'fetching', 'saving'].includes(j.status)) return true;
  return false;
}

async function importKey(rt, keyUrl, signal) {
  if (rt.keys.has(keyUrl)) return rt.keys.get(keyUrl);
  const p = (async () => {
    const r = await fetch(keyUrl, { credentials: 'include', signal });
    if (!r.ok) throw new Error(`Could not fetch decryption key (HTTP ${r.status})`);
    const raw = await r.arrayBuffer();
    return crypto.subtle.importKey('raw', raw, { name: 'AES-CBC' }, false, ['decrypt']);
  })();
  rt.keys.set(keyUrl, p);
  return p;
}

async function fetchBytes(url, range, signal) {
  let lastErr;
  for (let attempt = 0; attempt < SEGMENT_RETRIES; attempt++) {
    try {
      const headers = range ? { Range: range } : undefined;
      const r = await fetch(url, { credentials: 'include', cache: 'no-store', headers, signal });
      if (!r.ok && r.status !== 206) {
        const err = new Error(`HTTP ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return await r.arrayBuffer();
    } catch (e) {
      if (signal && signal.aborted) throw e;
      lastErr = e;
      if (e.status === 403 || e.status === 401 || e.status === 404) {
        if (attempt >= 1) break; // auth / missing: no point hammering
      }
      await new Promise((res) => setTimeout(res, 400 * (attempt + 1) ** 2));
    }
  }
  throw lastErr || new Error('Download failed');
}

async function runJob(jobId) {
  const job = jobs.get(jobId);
  if (!job) return;
  const rt = { abort: new AbortController(), keys: new Map() };
  jobRuntime.set(jobId, rt);
  const { signal } = rt.abort;
  keepAlive(true);

  const fail = (msg) => {
    job.status = 'error';
    job.error = msg;
    persistSoon();
  };

  try {
    job.status = 'fetching';
    persistSoon();
    // Referer/Origin for the playlist requests (segment hosts are added below).
    rt.ruleId = await addHeaderRule(job.pageUrl, [hostOf(job.variantUrl || job.manifestUrl)].filter(Boolean));

    // 1. Resolve the media playlist (pick the best variant of a master playlist).
    let mediaUrl = job.variantUrl || job.manifestUrl;
    let text = await fetchText(mediaUrl, signal);
    if (!isPlaylistText(text)) throw new Error('Not a valid HLS playlist');
    if (/#EXT-X-STREAM-INF/.test(text)) {
      const m = parseMaster(text, mediaUrl);
      if (!m.variants.length) throw new Error('Playlist has no playable variants');
      const v = m.variants[0];
      mediaUrl = v.url;
      job.quality = v.label;
      if (hostOf(mediaUrl) !== hostOf(job.manifestUrl)) {
        await removeHeaderRule(rt.ruleId);
        rt.ruleId = await addHeaderRule(job.pageUrl, [hostOf(mediaUrl)].filter(Boolean));
      }
      if (v.audioGroup && m.audioGroups[v.audioGroup]) {
        job.warning = 'This stream has a separate audio track; the saved file may be silent. Use "Send to app" for full audio.';
      }
      text = await fetchText(mediaUrl, signal);
    } else if (job.variantUrl) {
      job.quality = 'selected';
    }

    const media = parseMedia(text, mediaUrl);
    if (media.unsupported) return fail(media.unsupported);
    if (!media.segments.length) return fail('Playlist contains no segments.');
    if (!media.endList) return fail('This is a live stream - it can only be recorded with the desktop app.');

    job.ext = media.isFmp4 ? 'mp4' : 'ts';
    const items = [];
    if (media.map) items.push({ index: 0, url: media.map.url, range: '', key: null, seq: 0, init: true });
    media.segments.forEach((s) => items.push({ ...s, index: items.length }));
    job.total = items.length;

    // 2. Referer/Origin for the CDN.
    const hosts = [...new Set(items.map((i) => { try { return new URL(i.url).hostname; } catch (e) { return ''; } })
      .concat(media.segments.filter((s) => s.key && s.key.uri).map((s) => { try { return new URL(s.key.uri).hostname; } catch (e) { return ''; } }))
      .filter(Boolean))];
    await removeHeaderRule(rt.ruleId);
    rt.ruleId = await addHeaderRule(job.pageUrl, hosts);

    // 3. Resume: skip segments that are already stored.
    const have = new Set((await idbKeys(jobId)).map((k) => parseInt(k.split('/')[1], 10)));
    job.done = have.size;
    const queue = items.filter((i) => !have.has(i.index));
    let failure = null;

    const worker = async () => {
      while (queue.length && !failure && !signal.aborted) {
        const it = queue.shift();
        try {
          let buf = await fetchBytes(it.url, it.range, signal);
          if (it.key && it.key.method === 'AES-128') {
            const key = await importKey(rt, it.key.uri, signal);
            const iv = it.key.iv ? hexToBytes(it.key.iv) : ivFromSequence(it.seq);
            buf = await crypto.subtle.decrypt({ name: 'AES-CBC', iv }, key, buf);
          }
          await idbPut(segKey(jobId, it.index), new Blob([buf]));
          job.done += 1;
          job.bytes += buf.byteLength;
          if (job.done % 5 === 0) persistSoon();
        } catch (e) {
          if (signal.aborted) return;
          failure = e;
          rt.abort.abort();
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(SEGMENT_CONCURRENCY, queue.length || 1) }, worker));

    if (job.status === 'cancelled') return;
    if (failure) {
      const st = failure.status;
      return fail(
        st === 403 || st === 401
          ? `The server refused the segments (HTTP ${st}). Use "Send to app" instead.`
          : `Segment download failed: ${failure.message || failure}`
      );
    }

    job.status = 'ready';
    persistSoon();

    if (job.autoSave) await saveJob(jobId);
  } catch (e) {
    if (job.status !== 'cancelled') fail(String((e && e.message) || e));
  } finally {
    await removeHeaderRule(rt.ruleId);
    jobRuntime.delete(jobId);
    if (!anyJobRunning()) keepAlive(false);
  }
}

// ---------------------------------------------------------------------------
// Assemble + save through an offscreen document (service workers cannot create
// blob: URLs).
// ---------------------------------------------------------------------------
async function ensureOffscreen() {
  const has = chrome.runtime.getContexts
    ? (await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] })).length > 0
    : false;
  if (has) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['BLOBS'],
    justification: 'Assemble cached video segments into one downloadable file.',
  });
}

const pendingDownloads = new Map(); // downloadId -> {jobId, blobUrl}

async function saveJob(jobId) {
  const job = jobs.get(jobId);
  if (!job) throw new Error('Job not found');
  if (job.status === 'fetching' || job.status === 'queued') {
    job.autoSave = true;     // save automatically as soon as the cache is complete
    persistSoon();
    return { pending: true };
  }
  if (job.status !== 'ready') throw new Error('Nothing to save yet');

  job.status = 'saving';
  persistSoon();
  try {
    await ensureOffscreen();
    const res = await chrome.runtime.sendMessage({
      target: 'offscreen', action: 'assemble', jobId, total: job.total, ext: job.ext,
    });
    if (!res || !res.ok) throw new Error((res && res.error) || 'Could not assemble file');
    const filename = `OmniStream/${job.filename}.${job.ext}`;
    const downloadId = await chrome.downloads.download({ url: res.blobUrl, filename, conflictAction: 'uniquify' });
    pendingDownloads.set(downloadId, { jobId, blobUrl: res.blobUrl });
    return { started: true };
  } catch (e) {
    job.status = 'ready';
    job.error = String((e && e.message) || e);
    persistSoon();
    throw e;
  }
}

chrome.downloads.onChanged.addListener(async (delta) => {
  const p = pendingDownloads.get(delta.id);
  if (!p || !delta.state) return;
  if (delta.state.current === 'complete' || delta.state.current === 'interrupted') {
    pendingDownloads.delete(delta.id);
    const job = jobs.get(p.jobId);
    try { await chrome.runtime.sendMessage({ target: 'offscreen', action: 'revoke', blobUrl: p.blobUrl }); } catch (e) { /* ignore */ }
    if (job) {
      if (delta.state.current === 'complete') {
        job.status = 'saved';
        await idbDeleteJob(job.id).catch(() => {});
      } else {
        job.status = 'ready';
        job.error = 'The browser download was interrupted. Press Download to retry.';
      }
      persistSoon();
    }
    try {
      const left = (await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] })).length;
      if (left && ![...jobs.values()].some((j) => j.status === 'saving')) await chrome.offscreen.closeDocument();
    } catch (e) { /* ignore */ }
  }
});

async function removeJob(jobId, opts = {}) {
  const job = jobs.get(jobId);
  if (!job) return;
  if (opts.keepIfSaving && job.status === 'saving') return;
  job.status = 'cancelled';
  const rt = jobRuntime.get(jobId);
  if (rt) rt.abort.abort();
  jobs.delete(jobId);
  await idbDeleteJob(jobId).catch(() => {});
  persistSoon();
}

async function purgeOldJobs() {
  const now = Date.now();
  for (const j of [...jobs.values()]) {
    if (now - j.startedAt > CACHE_MAX_AGE_MS && j.status !== 'fetching') await removeJob(j.id);
  }
  // Orphaned segment data (job metadata lost) is cleaned too.
  try {
    const db = await openDb();
    const keys = await new Promise((res, rej) => {
      const r = db.transaction('segs').objectStore('segs').getAllKeys();
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
    const orphan = new Set(keys.map((k) => k.split('/')[0]).filter((id) => !jobs.has(id)));
    for (const id of orphan) await idbDeleteJob(id);
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Messages from the popup
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.target === 'offscreen') return false;   // not for us
  (async () => {
    await ready;
    switch (request.action) {
      case 'get_streams': {
        const streams = (tabStreams.get(request.tabId) || []).map((s) => ({
          ...s, job: publicJob(jobs.get(s.jobId)),
        }));
        return { streams, settings };
      }
      case 'clear_streams': {
        await dropTab(request.tabId);
        chrome.action.setBadgeText({ text: '', tabId: request.tabId }).catch(() => {});
        return { success: true };
      }
      case 'cache_start': {
        const job = await startCache(request.tabId, request.streamId, request.variantUrl);
        return { success: true, job };
      }
      case 'save': {
        const r = await saveJob(request.jobId);
        return { success: true, ...r };
      }
      case 'cancel': {
        await removeJob(request.jobId);
        for (const list of tabStreams.values()) for (const s of list) if (s.jobId === request.jobId) s.jobId = null;
        persistSoon();
        return { success: true };
      }
      case 'direct_download': {
        const name = sanitizeFilename(request.title, 'video');
        let ext = 'mp4';
        const m = pathOf(request.url).match(/\.(mp4|webm|mkv|mov|m4v)$/);
        if (m) ext = m[1];
        const id = await chrome.downloads.download({ url: request.url, filename: `OmniStream/${name}.${ext}`, conflictAction: 'uniquify' });
        return { success: true, id };
      }
      case 'set_settings': {
        settings = { ...settings, ...request.settings };
        await chrome.storage.local.set({ os_settings: settings });
        return { success: true, settings };
      }
      default:
        return { success: false, error: 'Unknown action' };
    }
  })().then(sendResponse).catch((e) => sendResponse({ success: false, error: String((e && e.message) || e) }));
  return true; // async response
});
