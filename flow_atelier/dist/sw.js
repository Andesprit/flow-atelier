// Only the built-in offline document enters Cache Storage. Network responses
// (including redirects to a sign-in page) are never written there.
const cacheName = "flow-atelier-offline";
const offlinePage = "/offline.html";
const shell = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Flow Atelier · Offline</title>
<style>body{background:#1a1611;color:#eee8df;font:1.05rem system-ui;margin:12vh auto;padding:1.5rem;max-width:32rem}h1{font:2rem Georgia,serif}a{color:#e6aa59}</style>
</head><body><main><h1>Flow Atelier is offline</h1><p>Connect to your Atelier server to launch a conduit or view a run.</p>
<a href="">Try again</a></main></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(cacheName).then((cache) => cache.put(offlinePage,
    new Response(shell, { headers: { "Content-Type": "text/html; charset=utf-8" } }))));
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== "GET" || request.mode !== "navigate" ||
      url.origin !== self.location.origin || url.search || request.headers.has("Authorization")) return;
  if (!/^\/(?:dashboard|launch|designer|kanban|runs\/[\w.-]+)?$/.test(url.pathname)) return;
  event.respondWith(fetch(request).catch(async () => (await caches.open(cacheName)).match(offlinePage)));
});

