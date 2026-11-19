/* Spatial Lab · Service Worker
 * 目标：公网隧道下第二次访问近乎秒开（静态资源全部走本地缓存）
 * 策略：
 *   /             HTML → 网络优先，失败回退缓存（保证版本最新）
 *   /static/*     静态资源 → 缓存优先 + 后台更新（URL 带 ?v= 版本号，天然失效）
 *   /thumb/*      缩略图 → 网络优先，缓存兜底
 *   /api/*        一律不缓存
 *   /files/*      模型文件不缓存（体积大，按需）
 */
const CACHE = 'spatiallab-v1';
const PRECACHE = [
  '/static/style.css',
  '/static/app.js',
  '/static/vendor/three.min.js',
  '/static/vendor/GLTFLoader.js',
  '/static/vendor/OrbitControls.js',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((c) => Promise.allSettled(PRECACHE.map((u) => c.add(u))))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const p = url.pathname;

  // API 不碰
  if (p.startsWith('/api/')) return;

  // HTML：网络优先
  if (p === '/' || p.endsWith('.html')) {
    e.respondWith((async () => {
      try {
        const r = await fetch(req);
        if (r.ok) {
          const c = await caches.open(CACHE);
          c.put(req, r.clone());
        }
        return r;
      } catch (err) {
        const hit = await caches.match(req);
        if (hit) return hit;
        throw err;
      }
    })());
    return;
  }

  // 静态资源：缓存优先 + 后台更新
  if (p.startsWith('/static/')) {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(req);
      const net = fetch(req).then((r) => {
        if (r.ok) cache.put(req, r.clone());
        return r;
      }).catch(() => null);
      if (hit) { net; return hit; }
      const r = await net;
      if (r) return r;
      throw new Error('offline');
    })());
    return;
  }

  // 缩略图：网络优先 + 缓存兜底
  if (p.startsWith('/thumb/')) {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      try {
        const r = await fetch(req);
        if (r.ok) cache.put(req, r.clone());
        return r;
      } catch (err) {
        const hit = await cache.match(req);
        if (hit) return hit;
        throw err;
      }
    })());
  }
});
