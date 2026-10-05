// Push contains its own summary; displaying it never requires the private site.
// Do not cache pages or intercept requests: Nightfeed may contain private data.
self.addEventListener('push', event => {
  let payload;
  try { payload = event.data.json(); } catch (_) { payload = {}; }
  event.waitUntil(self.registration.showNotification(payload.title || 'Nightfeed', {
    body: payload.body || 'There are updates in Nightfeed.',
    icon: '/static/nightfeed-192.png', badge: '/static/nightfeed-192.png',
    tag: payload.tag || 'nightfeed-summary', renotify: false,
    data: {url: payload.url || '/notifications'}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/notifications', self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async windows => {
    const existing = windows.find(client => new URL(client.url).origin === target.origin);
    if (existing) { await existing.navigate(target.href); return existing.focus(); }
    return self.clients.openWindow(target.href);
  }));
});
