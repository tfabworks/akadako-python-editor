// Service worker: precache the app shell + Pyodide + CodeMirror + the akadako
// package so the ~13MB runtime downloads only once. Second load is instant and
// works offline. Lives at the site root so its scope covers /web/ and /akadako/.
//
// Bump CACHE when assets change so clients refresh (deploys should also run a
// CloudFront invalidation of /*).
const CACHE = "akadako-py-v1";

const PRECACHE = [
  "/web/", "/web/index.html",
  "/web/main.js", "/web/worker.js", "/web/pybridge.py", "/web/akadako-api.js",
  // CodeMirror
  "/web/vendor/codemirror/codemirror.min.css",
  "/web/vendor/codemirror/codemirror.min.js",
  "/web/vendor/codemirror/mode/python/python.min.js",
  "/web/vendor/codemirror/addon/hint/show-hint.min.css",
  "/web/vendor/codemirror/addon/hint/show-hint.min.js",
  "/web/vendor/codemirror/addon/edit/matchbrackets.min.js",
  "/web/vendor/codemirror/addon/edit/closebrackets.min.js",
  "/web/vendor/codemirror/theme/material-darker.min.css",
  // Pyodide
  "/web/vendor/pyodide/pyodide.js",
  "/web/vendor/pyodide/pyodide.asm.js",
  "/web/vendor/pyodide/pyodide.asm.wasm",
  "/web/vendor/pyodide/python_stdlib.zip",
  "/web/vendor/pyodide/pyodide-lock.json",
  // akadako package (loaded into the Pyodide FS by the worker)
  "/akadako/__init__.py", "/akadako/board.py", "/akadako/firmata.py",
  "/akadako/transport.py", "/akadako/constants.py", "/akadako/errors.py",
  "/akadako/share.py",
  "/akadako/sensors/__init__.py", "/akadako/sensors/adxl345.py",
  "/akadako/sensors/bme280.py", "/akadako/sensors/kxtj3.py",
  "/akadako/sensors/ltr303.py", "/akadako/sensors/vl53l0x.py",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // allSettled so one bad URL doesn't abort the whole precache
      .then((cache) => Promise.allSettled(PRECACHE.map((u) => cache.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Same-origin GET: stale-while-revalidate. Cross-origin (e.g. the share server)
// is left to the network untouched.
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  if (new URL(req.url).origin !== location.origin) return;
  e.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => { if (res && res.ok) cache.put(req, res.clone()); return res; })
        .catch(() => cached);
      return cached || network;
    })
  );
});
