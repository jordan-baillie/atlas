# Caddy basicauth fix — 2026-04-17

**File:** `/etc/caddy/Caddyfile`

**What changed:** Wrapped all handlers in a `route {}` block so `basicauth *` runs as middleware before any path matcher (fixes Caddy v2 `handle_path` subroute bypass). Also changed site address from `127.0.0.1:80` to `:80` with `bind 127.0.0.1` so that requests with `Host: localhost` are matched (previously `127.0.0.1:80` only matched `Host: 127.0.0.1`, silently bypassing auth for `localhost`-addressed curls).

**Backup before change:** `/etc/caddy/Caddyfile.bak.pre-route-fix`
**Previous backup:** `/etc/caddy/Caddyfile.bak.2026-04-17` (untouched)

**Verified:** `curl -sI http://localhost/api/` → 401 + `Www-Authenticate: Basic`; `curl -u atlas:<pass> http://localhost/api/health` → 404 from uvicorn (backend proxied, no 401).
