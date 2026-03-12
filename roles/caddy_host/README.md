# Caddy Host Role

This role deploys Caddy as a rootless Podman Quadlet container for ingress routing to apps.

## Config Structure

- **Main Caddyfile**: `/srv/data/system/ingress/caddy/config/Caddyfile` - Global settings, imports snippets and sites.
- **Snippets**: `/srv/data/system/ingress/caddy/config/snippets/` - Shared directives (e.g., security headers).
- **Sites**: `/srv/data/system/ingress/caddy/config/sites-enabled/` - Per-app site configs (auto-generated).

## Customization

- Add shared snippets in `snippets/*.caddy`.
- Per-app custom directives via `ingress.caddy_directives` in `apps/<app>/app.yml`.

## Validation

Config is validated via container before reload to prevent downtime.

## Troubleshooting

- Check logs: `podman logs caddy`
- Validate manually: `podman run --rm -v /srv/data/system/ingress/caddy/config:/etc/caddy:ro caddy:2-alpine caddy validate`
