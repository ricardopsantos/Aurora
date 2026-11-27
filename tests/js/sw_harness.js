// R471: runs aurora/web/sw.js in Node with a fake Cache API and fetch, then a scenario; prints the cache keys as JSON.
'use strict';
const fs = require('fs'), vm = require('vm');
const ORIGIN = 'https://aurora.test';
const store = new Map();                 // cache name -> Map(path -> body); a Map keeps insertion order like the Cache API
const cacheOf = name => {
  if (!store.has(name)) store.set(name, new Map());
  const m = store.get(name), key = k => new URL(typeof k === 'string' ? k : k.url, ORIGIN).pathname + new URL(typeof k === 'string' ? k : k.url, ORIGIN).search;
  return {
    put: async (k, res) => { m.set(key(k), res.body); },
    delete: async k => m.delete(key(k)),
    match: async k => (m.has(key(k)) ? { body: m.get(key(k)), headers: {}, status: 200 } : undefined),
    keys: async () => [...m.keys()].map(p => ({ url: ORIGIN + p })),
    addAll: async () => {},
  };
};
let server = { status: 200 };
const listeners = {};
const ctx = {
  self: { addEventListener: (t, f) => { listeners[t] = f; }, skipWaiting: () => {}, clients: { claim: async () => {} } },
  caches: { open: async n => cacheOf(n), keys: async () => [...store.keys()], delete: async n => store.delete(n) },
  location: { origin: ORIGIN },
  fetch: async req => ({ ok: server.status < 300, status: server.status, body: 'b:' + req.url, clone() { return this; }, headers: {} }),
  Headers: class { constructor() {} set() {} }, Response: class { constructor(b) { this.body = b; } },
  URL, console,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const fire = async (method, path, body) => {
  let p = null;
  const req = { method, url: ORIGIN + path, clone: () => ({ json: async () => body || {} }) };
  listeners.fetch({ request: req, respondWith: x => { p = x; } });
  if (p) await p;
};
(async () => {
  const steps = JSON.parse(process.argv[3]);
  for (const s of steps) { server.status = s.status || 200; await fire(s.method || 'GET', s.path, s.body); }
  const c = store.get([...store.keys()][0]) || new Map();
  process.stdout.write(JSON.stringify([...c.keys()]));
})().catch(e => { console.error(e); process.exit(1); });
