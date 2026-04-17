# Caddy Reverse Proxy — Access & Hardening Guide

> **Last updated**: 2026-04-17 (C8 audit fix)

## Current Configuration

| Setting | Value |
|---------|-------|
| Bind address | `127.0.0.1:80` (IPv4 loopback **only**) |
| TLS | None (no domain — localhost-only Mode A) |
| Auth | HTTP Basic Auth (bcrypt hash, cost factor 14) |
| Caddyfile | `/etc/caddy/Caddyfile` |
| Backup | `/etc/caddy/Caddyfile.bak.2026-04-17` |

Caddy v2 was previously binding to `:::80` (IPv6 wildcard, which dual-stacks IPv4).
The `bind 127.0.0.1` directive forces an IPv4-loopback-only socket, verified via:

```
ss -tlnp | grep ':80'
# LISTEN 0  4096  127.0.0.1:80  0.0.0.0:*  users:(("caddy",...))
```

External access returns `Connection refused` (verified 2026-04-17).

---

## Credentials

Stored in `~/.atlas-secrets.json` under key `caddy_basic_auth`:

```json
{
  "caddy_basic_auth": {
    "username": "atlas",
    "password": "<see secrets file>",
    "note": "Added 2026-04-17 by C8 Caddy hardening audit."
  }
}
```

Retrieve at runtime:
```bash
python3 -c "import json; s=json.load(open('/root/.atlas-secrets.json')); print(s['caddy_basic_auth']['password'])"
```

---

## Accessing the Dashboard Remotely

Since Caddy only accepts connections from `127.0.0.1`, use an **SSH tunnel**:

```bash
# Open tunnel (run locally)
ssh -L 8080:127.0.0.1:80 <user>@<host>

# Then browse to (or curl from your local machine)
http://localhost:8080/
```

With credentials:
```bash
curl -u atlas:<password> http://localhost:8080/api/health
```

---

## Caddy Routing

| Path | Destination |
|------|-------------|
| `/api/*` | `localhost:8000` (Atlas job server / API) — prefix stripped |
| `/tui/*` | `/var/www/tui` (static file server) — prefix stripped |
| Everything else | `404 Not found` |

---

## Rollback

If the Caddyfile is ever broken, roll back to the known-good backup:

```bash
cp /etc/caddy/Caddyfile.bak.2026-04-17 /etc/caddy/Caddyfile
systemctl reload caddy
systemctl status caddy --no-pager | head -10
```

---

## Upgrading to Full TLS (Mode B — future)

If a real domain is pointed at this server:

1. Replace `127.0.0.1:80` block with `https://<domain>` (Caddy auto-ACME)
2. Keep a `127.0.0.1:80` block as a fallback/health path
3. Remove the `bind 127.0.0.1` directive (TLS block needs 0.0.0.0)
4. Keep `basicauth` — HTTPS does not eliminate the need for application-level auth

---

## Security Posture After C8

| Check | Before | After |
|-------|--------|-------|
| Bind interface | `:::80` (IPv6 wildcard / all interfaces) | `127.0.0.1:80` (loopback only) |
| Auth | None | HTTP Basic Auth (bcrypt-14) |
| TLS | None | None (no domain available) |
| External access | Open (200 OK) | Connection refused |
| Unauthenticated local | 200 OK | 401 Unauthorized |
| Security headers | ✅ (unchanged) | ✅ |
