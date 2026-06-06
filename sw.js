// Minimal service worker — only here so KalshiBot is installable as a standalone
// app (its own window + icon). Network-only: it caches nothing, so the bot always
// shows live data.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => self.clients.claim());
self.addEventListener("fetch", (e) => {});
