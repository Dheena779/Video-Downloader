# Fix log

## Torrent (torrent_downloader.py)
* **Root cause:** `--dht-entry-point` was given three comma-separated hosts. aria2c only accepts ONE `HOST:PORT`,
  so it exited immediately with `errorCode=28`. The app ignored the exit code and then showed "Torrent download complete!".
* Progress now comes from aria2's RPC (the old regex could not match the `UL:` field in BitTorrent progress lines).
* Magnet links: metadata phase -> real download hand-over is handled; failures/exits now show the real aria2 error.
* Bencode parser rewritten (info-hash taken from the real top-level `info` slice, binary-safe, UTF-8 names).
* Corrupt `.torrent` files give a clear error. aria2c console window hidden on Windows.

## Browser extension
* **New:** segment cache + instant download (see README).
* **Popup buttons never worked:** MV3 blocks inline `onclick`. Rewritten with event listeners (also fixes HTML injection via titles/URLs).
* `.ts`/`.m4s` segments were listed as "MP4 Direct Video" (video/mp2t matched the generic video check first). Fixed.
* Detection by substring (`.ts` matched `.tsx`, etc.) replaced with path-based matching; range requests use real file size.
* Guessed `index.m3u8` was added without checking it exists; now verified before being listed.
* Master + variant playlists no longer show the same video twice.
* Detected streams were lost whenever Chrome stopped the idle service worker; state now persists.

## App / UI
* **UI script had a duplicate `let currentIgProfile`** -> SyntaxError, the whole UI script failed to load. Removed.
* Play / Folder buttons broke on Windows paths (backslashes inside inline handlers). Now real event handlers.
* `esc()` did not escape `"`; titles with quotes broke the cards.
* Extension bridge is threaded (one stalled connection no longer blocks the extension).
* `resolve_full_stream_url` rewrote ordinary URLs containing "chunk1"/"seg2" to an invented `index.m3u8`; now only real segment files are touched and only to a verified playlist.
* Output name no longer contains `[NAp]` when height is unknown; HLS percent uses fragment counts.
* Instagram: failed lookup saved an HTML page as `.mp4`; cancel did nothing. Both fixed.

## Reload
Extension: chrome://extensions -> reload OmniStream (it asks for new permissions: downloads, offscreen, declarativeNetRequest).
