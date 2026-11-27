// summary: R396 Aurora Web service worker: the page shell and the last sessions/transcripts stay readable offline (R471: the 20 most recently read; deleted chats dropped); live and mutating requests always go to the network.
'use strict';
const CACHE = 'aurora-web-v1';
const SHELL = ['/', '/v1', '/paper', '/manifest.webmanifest', '/favicon.png', '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png'];
// GETs that may be answered from the last good copy when the network is gone. Everything else (the stream,
// state, every POST) is never cached: a stale "busy" or a replayed action would be wrong, not just old.
const TRANSCRIPT = /^\/api\/sessions\/[A-Za-z0-9_-]{1,64}$/;
const READ_ONLY = [/^\/api\/sessions$/, TRANSCRIPT, /^\/api\/notes$/];
// R471: at most this many transcripts are kept (each up to 300k characters): the most recently read ones.
// They used to stay forever, deleted chats included, filling the phone's storage with every chat ever opened.
const MAX_TRANSCRIPTS = 20;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => caches.open(CACHE)).then(trimTranscripts).catch(() => {})
    .then(() => self.clients.claim()));
});

function offlineCopy(res) {   // the page tells a cached answer from a live one by this header
  const h = new Headers(res.headers); h.set('X-Aurora-Offline', '1');
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
}

const isTranscript = k => TRANSCRIPT.test(new URL(k.url || k, location.origin).pathname);

// R471: drop the oldest transcripts over MAX_TRANSCRIPTS (keys come back in the order they were stored)
async function trimTranscripts(c) {
  const ts = (await c.keys()).filter(isTranscript);
  for (const k of ts.slice(0, Math.max(0, ts.length - MAX_TRANSCRIPTS))) await c.delete(k);
}

// Network first, so a deploy or a new message is seen at once when online; the cache only fills gaps.
async function networkFirst(req, key) {
  const c = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.ok) {
      if (isTranscript(key)) { await c.delete(key); await c.put(key, res.clone()); await trimTranscripts(c); }  // R471: re-stored last = newest
      else await c.put(key, res.clone());
    } else if (res.status === 404) await c.delete(key);    // R471: gone on the server, gone here
    return res;
  } catch (err) {
    const hit = await c.match(key);
    if (hit) return offlineCopy(hit);
    throw err;
  }
}

// R471: deleting a chat (or a folder of chats) on the page also deletes the offline copies of it
async function forgetDeleted(req) {
  const body = await req.clone().json().catch(() => ({}));
  const res = await fetch(req);
  if (res.ok) {
    try {
      const c = await caches.open(CACHE);
      const p = new URL(req.url).pathname;
      if (p === '/api/chat/delete' && typeof body.id === 'string') await c.delete('/api/sessions/' + body.id);
      else for (const k of (await c.keys()).filter(isTranscript)) await c.delete(k);   // a folder: which chats is not known here
      for (const k of await c.keys()) if (new URL(k.url).pathname === '/api/sessions') await c.delete(k);
    } catch (e) {}
  }
  return res;
}

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.method === 'POST' && (url.pathname === '/api/chat/delete' || url.pathname === '/api/folder/delete'))
    return e.respondWith(forgetDeleted(req));
  if (req.method !== 'GET') return;
  const p = url.pathname;
  if (p === '/app.js') return e.respondWith(networkFirst(req, '/app.js'));   // one entry, whatever ?v= the page asked for
  if (SHELL.includes(p) || p === '/v1/' || p === '/paper/' || p === '/index.html')
    return e.respondWith(networkFirst(req, p.replace(/\/$/, '') || '/'));
  if (READ_ONLY.some(r => r.test(p))) return e.respondWith(networkFirst(req, p + url.search));   // R417: a search is not the full list
});
