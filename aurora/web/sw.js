// summary: R396 Aurora Web service worker: the page shell and the last sessions/transcripts stay readable offline; live and mutating requests always go to the network.
'use strict';
const CACHE = 'aurora-web-v1';
const SHELL = ['/', '/v1', '/paper', '/manifest.webmanifest', '/favicon.png', '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png'];
// GETs that may be answered from the last good copy when the network is gone. Everything else (the stream,
// state, every POST) is never cached: a stale "busy" or a replayed action would be wrong, not just old.
const READ_ONLY = [/^\/api\/sessions$/, /^\/api\/sessions\/[A-Za-z0-9_-]{1,64}$/, /^\/api\/notes$/];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function offlineCopy(res) {   // the page tells a cached answer from a live one by this header
  const h = new Headers(res.headers); h.set('X-Aurora-Offline', '1');
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
}

// Network first, so a deploy or a new message is seen at once when online; the cache only fills gaps.
async function networkFirst(req, key) {
  const c = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.ok) await c.put(key, res.clone());
    return res;
  } catch (err) {
    const hit = await c.match(key);
    if (hit) return offlineCopy(hit);
    throw err;
  }
}

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  const p = url.pathname;
  if (p === '/app.js') return e.respondWith(networkFirst(req, '/app.js'));   // one entry, whatever ?v= the page asked for
  if (SHELL.includes(p) || p === '/v1/' || p === '/paper/' || p === '/index.html')
    return e.respondWith(networkFirst(req, p.replace(/\/$/, '') || '/'));
  if (READ_ONLY.some(r => r.test(p))) return e.respondWith(networkFirst(req, p + url.search));   // R417: a search is not the full list
});
