/* sw.js — cache do shell do Energia Doméstica (PWA).
 *
 * Estratégia (regra de ouro: dado fresco vence cache):
 *   - não-GET (start/stop-charge, config, eventos)  → NUNCA interceptado
 *   - /api/*                                        → sempre rede
 *   - CDNs do shell (chart.js, leaflet)             → cache-first
 *   - tiles de mapa (OSM)                           → fora do SW
 *   - navegação (/)                                 → network-first; cache só se a rede falhar
 *     (assim o deploy atualiza o shell sem bump manual de versão)
 *   - resto same-origin (ícones, manifest)          → cache-first
 *
 * Registrado só em contexto seguro (guarda isSecureContext no index.html);
 * sobre HTTP LAN fica inert — ativa no dia em que houver HTTPS (ex.: tailscale serve).
 */

const CACHE = 'energia-shell-v1';

const SHELL = [
  './',
  './manifest.json',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
  'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.min.js',
  'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.min.css',
];

self.addEventListener('install', (e) => {
  // cache.add() individual (NÃO addAll, que é atômico): um CDN fora do ar
  // não pode falhar o install — o que faltar entra no runtime depois.
  e.waitUntil(caches.open(CACHE).then((c) => Promise.allSettled(SHELL.map((u) => c.add(u)))));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return; // controle (POST): passa sempre

  const url = new URL(req.url);
  if (url.pathname.startsWith('/api/')) return; // dados: sempre rede

  if (url.origin !== location.origin) {
    if (!url.hostname.includes('jsdelivr')) return; // tiles de mapa etc: fora
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return r;
      }))
    );
    return;
  }

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return r;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match('./')))
    );
    return;
  }

  e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
});
