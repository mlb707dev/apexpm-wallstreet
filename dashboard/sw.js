// Service worker intentionally does nothing: the API must never be cached and the page must always be fresh.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
