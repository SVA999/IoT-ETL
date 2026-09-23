/* Service worker de NEON AIR.
 *
 * Dos estrategias distintas a proposito:
 *   - El "shell" (HTML, CSS, JS, iconos) va stale-while-revalidate: responde
 *     al instante desde el cache (la app abre sin red) y en paralelo baja la
 *     version nueva, que queda lista para la siguiente carga. Con cache-first
 *     puro, un despliegue nuevo se quedaba atrapado detras del cache viejo.
 *   - La API va network-first con respaldo en cache: siempre se prefiere el
 *     dato fresco de SIATA, pero si el telefono esta sin senal se muestra la
 *     ultima lectura conocida en vez de una pantalla en blanco.
 */
const VERSION = 'neon-air-v3';
const SHELL = `${VERSION}-shell`;
const RUNTIME = `${VERSION}-api`;

const SHELL_ASSETS = [
    '/',
    '/static/css/cyber.css',
    '/static/js/app.js',
    '/manifest.webmanifest',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/icons/favicon.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(SHELL)
            .then((cache) => cache.addAll(SHELL_ASSETS))
            .then(() => self.skipWaiting()),
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)),
            ))
            .then(() => self.clients.claim()),
    );
});

self.addEventListener('fetch', (event) => {
    const { request } = event;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;  // fuentes y terceros: sin tocar

    if (url.pathname.startsWith('/api/')) {
        event.respondWith(networkFirst(request));
        return;
    }
    event.respondWith(cacheFirst(request));
});

async function cacheFirst(request) {
    const cached = await caches.match(request);
    if (cached) return cached;
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(SHELL);
            cache.put(request, response.clone());
        }
        return response;
    } catch (error) {
        const fallback = await caches.match('/');
        if (fallback) return fallback;
        throw error;
    }
}

async function networkFirst(request) {
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(RUNTIME);
            cache.put(request, response.clone());
        }
        return response;
    } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;
        return new Response(
            JSON.stringify({ error: 'Sin conexion y sin copia en cache de este recurso.' }),
            { status: 503, headers: { 'Content-Type': 'application/json' } },
        );
    }
}
