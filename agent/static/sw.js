// Birdy service worker: maakt het bord installeerbaar. Netwerk eerst; de schil (pagina,
// css, js, logo's) blijft als terugval in de cache zodat de app ook opent zonder verbinding.
// De API wordt nooit gecachet.
const CACHE = 'birdy-schil-v1';
const SCHIL = ['/', '/logo.png', '/logo-bird.png', '/icon-192.png', '/icon-512.png', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SCHIL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then((r) => {
      if (r.ok && (url.pathname === '/' || url.pathname.startsWith('/dashboard.') || SCHIL.includes(url.pathname))) {
        const kopie = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, kopie));
      }
      return r;
    }).catch(() => caches.match(e.request).then((r) => r || (url.pathname === '/' ? caches.match('/') : undefined)))
  );
});
