// OmniStream - offscreen document.
// Service workers cannot create blob: URLs, so this page stitches the cached
// segments (stored in IndexedDB) into one Blob and hands back a blob: URL that
// chrome.downloads can save.

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('omnistream-cache', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('segs');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function assemble(jobId, total, ext) {
  const db = await openDb();
  const range = IDBKeyRange.bound(`${jobId}/`, `${jobId}/￿`);
  const parts = await new Promise((resolve, reject) => {
    const req = db.transaction('segs').objectStore('segs').getAll(range);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  db.close();
  if (!parts.length) throw new Error('No cached segments found');
  if (parts.length !== total) throw new Error(`Cache incomplete (${parts.length}/${total} segments)`);
  // Keys are zero-padded, so getAll() already returns them in playback order.
  const blob = new Blob(parts, { type: ext === 'mp4' ? 'video/mp4' : 'video/mp2t' });
  return URL.createObjectURL(blob);
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== 'offscreen') return false;
  if (msg.action === 'assemble') {
    assemble(msg.jobId, msg.total, msg.ext)
      .then((blobUrl) => sendResponse({ ok: true, blobUrl }))
      .catch((e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
    return true;
  }
  if (msg.action === 'revoke') {
    try { URL.revokeObjectURL(msg.blobUrl); } catch (e) { /* ignore */ }
    sendResponse({ ok: true });
  }
  return false;
});
