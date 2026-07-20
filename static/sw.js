const CACHE_NAME = 'royal-v1'
const STATIC_ASSETS = ['/', '/owner', '/catalogue', '/static/customer.html', '/static/owner.html']

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS)))
})

self.addEventListener('fetch', e => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  )
})