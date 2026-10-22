// OmniStream Extension - Popup Controller
// NOTE: Manifest V3 forbids inline event handlers (onclick="...") in extension
// pages, so every handler is attached with addEventListener.
const LOCAL_BRIDGE_URL = 'http://127.0.0.1:6890';

let activeTab = null;
const cards = new Map(); // streamId -> {root, els}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (res) => {
      if (chrome.runtime.lastError) resolve({ success: false, error: chrome.runtime.lastError.message });
      else resolve(res || {});
    });
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  checkBridgeStatus();
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs || !tabs[0]) return;
  activeTab = tabs[0];

  const auto = document.getElementById('autoCache');
  auto.addEventListener('change', () => send({ action: 'set_settings', settings: { autoCache: auto.checked } }));

  await refresh();
  setInterval(refresh, 700);
});

async function checkBridgeStatus() {
  const badge = document.getElementById('statusBadge');
  try {
    const res = await fetch(`${LOCAL_BRIDGE_URL}/status`, { method: 'GET' });
    if (!res.ok) throw new Error();
    badge.textContent = '● Bridge Online';
    badge.className = 'status';
  } catch (e) {
    badge.textContent = '○ App Offline';
    badge.className = 'status offline';
    badge.title = 'OmniStream desktop application is not open.';
  }
}

async function refresh() {
  const res = await send({ action: 'get_streams', tabId: activeTab.id });
  const streams = res.streams || [];
  if (res.settings) document.getElementById('autoCache').checked = !!res.settings.autoCache;
  render(streams);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function render(streams) {
  const container = document.getElementById('streamList');
  if (!streams.length) return;           // keep the "nothing detected" message
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();

  const ids = new Set(streams.map((s) => s.id));
  for (const [id, c] of cards) if (!ids.has(id)) { c.root.remove(); cards.delete(id); }

  streams.forEach((stream) => {
    let c = cards.get(stream.id);
    if (!c) { c = buildCard(stream); cards.set(stream.id, c); container.appendChild(c.root); }
    updateCard(c, stream);
  });
}

function buildCard(stream) {
  const root = el('div', 'stream-card');
  const meta = el('div', 'stream-meta');
  const badge = el('span', 'badge', stream.label || 'Video Stream');
  const size = el('span', 'size', stream.size || '');
  meta.append(badge, size);

  const url = el('div', 'url-text', stream.url);
  url.title = stream.url;

  const quality = el('select', 'q');
  quality.style.display = 'none';

  const progress = el('div', 'progress');
  const bar = el('div');
  progress.appendChild(bar);
  progress.style.display = 'none';

  const status = el('div', 'jstatus');
  const sLeft = el('span');
  const sRight = el('span');
  status.append(sLeft, sRight);
  status.style.display = 'none';

  const row = el('div', 'btn-row');
  const dl = el('button', 'btn', 'Download');
  const app = el('button', 'btn secondary', 'Send to app');
  const copy = el('button', 'btn-copy', 'Copy');
  row.append(dl, app, copy);

  root.append(meta, url, quality, progress, status, row);

  const c = { root, els: { badge, size, quality, progress, bar, status, sLeft, sRight, dl, app, copy }, stream };

  dl.addEventListener('click', () => onDownload(c));
  app.addEventListener('click', () => sendToApp(c.stream));
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(c.stream.url); copy.textContent = 'Copied'; }
    catch (e) { copy.textContent = 'Failed'; }
    setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
  });
  return c;
}

function fmtMB(b) { return (b / 1048576).toFixed(1) + ' MB'; }

function updateCard(c, stream) {
  c.stream = stream;
  const { els } = c;
  const job = stream.job;
  const hls = stream.hls;
  els.size.textContent = stream.size || '';

  // Variant picker (master playlists) before caching starts
  if (stream.kind === 'hls' && hls && hls.isMaster && hls.variants.length > 1 && !job) {
    if (els.quality.options.length !== hls.variants.length) {
      els.quality.innerHTML = '';
      hls.variants.forEach((v, i) => {
        const o = document.createElement('option');
        o.value = v.url;
        o.textContent = v.label + (i === 0 ? ' (best)' : '');
        els.quality.appendChild(o);
      });
    }
    els.quality.style.display = '';
  } else {
    els.quality.style.display = 'none';
  }

  els.progress.style.display = 'none';
  els.status.style.display = 'none';
  els.dl.disabled = false;
  els.app.style.display = '';

  if (stream.kind === 'dash') {
    els.dl.style.display = 'none';
    els.sLeft.textContent = 'DASH streams are downloaded by the desktop app.';
    els.status.style.display = '';
    return;
  }
  els.dl.style.display = '';

  if (stream.kind === 'direct') {
    els.dl.textContent = 'Download';
    return;
  }

  // HLS
  if (hls && hls.error) {
    els.status.style.display = '';
    els.sLeft.textContent = 'Cannot inspect playlist: ' + hls.error;
    els.sLeft.className = 'err';
    els.dl.style.display = 'none';
    return;
  }
  if (hls && hls.live) {
    els.status.style.display = '';
    els.sLeft.textContent = 'Live stream - record it with the desktop app.';
    els.dl.style.display = 'none';
    return;
  }
  if (hls && hls.unsupported) {
    els.status.style.display = '';
    els.sLeft.textContent = hls.unsupported;
    els.sLeft.className = 'err';
    els.dl.style.display = 'none';
    return;
  }

  if (!job) {
    els.dl.textContent = 'Cache & download';
    return;
  }

  els.progress.style.display = '';
  els.status.style.display = '';
  els.sLeft.className = '';
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  els.bar.style.width = pct + '%';
  els.bar.className = '';

  const q = job.quality ? job.quality + ' · ' : '';
  switch (job.status) {
    case 'queued':
    case 'fetching':
      els.sLeft.textContent = `${q}Caching ${job.done}/${job.total || '?'} segments (${pct}%)`;
      els.sRight.textContent = fmtMB(job.bytes);
      els.dl.textContent = job.autoSave ? 'Will save when ready…' : 'Save when ready';
      els.dl.disabled = !!job.autoSave;
      break;
    case 'ready':
      els.bar.className = 'done';
      els.sLeft.textContent = `${q}Ready - ${job.total} segments cached`;
      els.sRight.textContent = fmtMB(job.bytes);
      els.dl.textContent = 'Download now';
      break;
    case 'saving':
      els.bar.className = 'done';
      els.sLeft.textContent = 'Saving file…';
      els.sRight.textContent = fmtMB(job.bytes);
      els.dl.textContent = 'Saving…';
      els.dl.disabled = true;
      break;
    case 'saved':
      els.bar.className = 'done';
      els.sLeft.textContent = `Saved to Downloads/OmniStream/${job.filename}.${job.ext}`;
      els.sRight.textContent = '';
      els.dl.textContent = 'Cache again';
      break;
    case 'error':
      els.bar.className = 'err';
      els.sLeft.textContent = job.error || 'Error';
      els.sLeft.className = 'err';
      els.sRight.textContent = '';
      els.dl.textContent = 'Retry';
      break;
    default:
      els.sLeft.textContent = job.status;
  }
  if (job.warning && job.status !== 'error') {
    els.sRight.textContent = '';
    els.sLeft.className = 'warn';
    els.sLeft.textContent = job.warning;
  }
}

async function onDownload(c) {
  const stream = c.stream;
  const job = stream.job;
  const title = stream.pageTitle || activeTab.title || 'video';

  if (stream.kind === 'direct') {
    const r = await send({ action: 'direct_download', url: stream.url, title });
    if (!r.success) alert('Download failed: ' + (r.error || 'unknown error'));
    else window.close();
    return;
  }

  // HLS
  if (!job || job.status === 'saved' || job.status === 'error') {
    if (job) await send({ action: 'cancel', jobId: job.id });
    const variantUrl = c.els.quality.style.display !== 'none' ? c.els.quality.value : '';
    const r = await send({ action: 'cache_start', tabId: activeTab.id, streamId: stream.id, variantUrl });
    if (!r.success) { alert('Could not start: ' + (r.error || '')); return; }
    // Tapping Download means "give me the file": save automatically when ready.
    if (!job || job.status !== 'saved') await send({ action: 'save', jobId: r.job.id });
    refresh();
    return;
  }
  const r = await send({ action: 'save', jobId: job.id });
  if (!r.success) alert('Save failed: ' + (r.error || ''));
  refresh();
}

async function sendToApp(stream) {
  try {
    const res = await fetch(`${LOCAL_BRIDGE_URL}/add-stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: stream.url,
        pageUrl: stream.pageUrl || (activeTab && activeTab.url) || '',
        pageTitle: stream.pageTitle || (activeTab && activeTab.title) || '',
        streamType: stream.type,
      }),
    });
    if (res.ok) window.close();
    else alert('Could not deliver stream to OmniStream desktop app. Ensure OmniStream is running.');
  } catch (e) {
    alert('OmniStream desktop app is not running. Please open OmniStream first.');
  }
}
