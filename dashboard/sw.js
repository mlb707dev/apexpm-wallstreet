// Minimal service worker: makes the dashboard installable. Network-first; no caching of API data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return;           // never cache live data
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
