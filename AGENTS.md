# AGENTS.md

This file is the **single source of truth** for AI coding agents (Cursor, Aider, Claude Code, GitHub Copilot agents, etc.) working on this repository.

It defines the **final architectural spec**, conventions, safety boundaries, and concrete examples so agents can contribute accurately without hallucinating incompatible patterns.

**Project mission**  
Build and maintain a **frugal enterprise-grade**, multi-host, fully rootless service management platform using:

- **Ansible** for orchestration, declarative configuration, lifecycle, and migration
- **Podman** (rootless only) + **Quadlets** (systemd-native units) for all containers
- Single **Caddy** instance per host (rootless Quadlet container) as reverse proxy / ingress
- Per-app declarative YAML configs (`apps/*/app.yml`) acting as simplified compose → quadlet translator
- Unified persistent storage under `/srv/data` (EBS-like mount, survives host failure)

No Docker, no docker-compose, no privileged containers, no Kubernetes.  
Treat as long-term production infrastructure: idempotent, auditable, recoverable, minimal blast radius, 10+ year durability.

**Core architectural invariants (do NOT break without explicit discussion):**

- One FQDN → one app (clean DNS, certificates, routing)
- Per-app isolated ingress network: `<app>-ingress-net` — Caddy dynamically joins **only** this network for the app
- Optional per-app internal network: `<app>-internal-net` — backend-only (DB, cache, workers); Caddy **never** joins
- Path-based routing within an app: multiple containers share the ingress network and FQDN, differentiated by Caddy `handle` blocks
- Static sites: served directly by Caddy (`file_server`) under the same FQDN — no container needed
- All persistent data (app files, volumes, Caddy certs/config) under `/srv/data`
- Caddy: graceful reload only — never restart
- Operations: start/stop/restart/destroy/migrate with confirmation on destructive actions

## Repository Structure – Key Paths

- `inventories/production/hosts.yml`          ← [homelab_servers], [caddy_hosts]
- `host_vars/<host>.yml`                      ← assigned_apps: [], caddy_tuning: {}
- `apps/<app-name>/app.yml`                   ← heart of the system
- `apps/<app-name>/files/`                    ← configs copied → bind-mounted
- `apps/<app-name>/secrets/`                  ← gitignored
- `playbooks/`
  - `system_prepare.yml`                      ← Podman, /srv/data mount, Caddy setup
  - `app_manage.yml`                          ← start/stop/restart/destroy
  - `app_migrate.yml`                         ← rsync-based migration
  - `caddy_manage.yml`                        ← Caddy reload & global config
- `roles/`
  - `podman_system/`                          ← subuid, lingering, /srv/data mount
  - `caddy_system_service/`                   ← rootless Caddy Quadlet + hardening
  - `podman_app_deploy/`                      ← app.yml → Quadlets + networks + Caddy attach

## Core Rules & Conventions

### 1. Rootless & Security First
- Rootless Podman everywhere — no exceptions.
- Quadlet hardening for **all** containers (including Caddy):
  ```
  CapDrop=ALL
  CapAdd=NET_BIND_SERVICE
  NoNewPrivileges=true
  ReadOnly=true
  Tmpfs=/tmp:size=64m
  Tmpfs=/run:size=32m
  Memory=512M              # adjust per service
  CPUWeight=1024          # cgroups v2 weight
  CPUQuota=10%            # hard limit (optional)
  ```
- Use `:Z,U` on bind mounts for auto-chown to container runtime UID.
- Pre-copy ownership fix (prevents flip-flop):
  ```yaml
  - name: Pre-set ownership in user namespace
    become_user: "{{ podman_user }}"
    command: >-
      podman unshare chown -R $(id -u):$(id -g)
      {{ app_root }}/files
      {{ app_root }}/public
    changed_when: false
  ```
- Use `synchronize` (rsync) for config dirs instead of `copy`/`template` when possible — preserves ownership/timestamps.

### 2. Networking & Isolation
- No shared internal network — **per-app isolation only**.
- Every app: `<app>-ingress-net` (Caddy + frontend/UI containers)
- Optional: `<app>-internal-net` (backends only)
- Dynamic Caddy attach:
  ```yaml
  - name: Attach Caddy to app ingress network
    command: podman network connect {{ pod.ingress_network }} caddy
    register: attach
    changed_when: "'already connected' not in attach.stderr"
    notify: Reload Caddy
  ```
- Disconnect on destroy/stop/migration source.

### 3. App Definition – Examples

**Multi-container app example** (wiki with frontend + admin + db)

```yaml
app_name: wiki
host: blr1.adityaj.in
app_root: "{{ data_mount }}/apps/wiki"

type: container

pod:
  name: wiki
  ingress_network: wiki-ingress-net
  internal_network: wiki-internal-net

ingress:
  main_domain: wiki.adityaj.in
  tls: true

containers:
  - name: frontend
    image: lscr.io/linuxserver/bookstack:latest
    networks: ["{{ pod.ingress_network }}"]
    volumes:
      - type: volume
        name: bookstack-data
        destination: /var/www/bookstack/storage
    depends_on: [db]

  - name: admin
    image: mycompany/admin-tool:latest
    networks: ["{{ pod.ingress_network }}"]
    proxy_paths: ["/admin", "/admin/*"]

  - name: db
    image: mariadb:10.11
    networks: ["{{ pod.internal_network }}"]
    volumes:
      - type: volume
        name: db-data
        destination: /var/lib/mysql
```

**Static site example** (Hugo blog)

```yaml
app_name: blog
host: blr1.adityaj.in
app_root: "{{ data_mount }}/apps/blog"

type: static

ingress:
  main_domain: blog.adityaj.in
  tls: true
  caddy_directives: |
    encode zstd gzip
    header Cache-Control "public, max-age=86400"
```

**Mixed app example** (container + static assets)

```yaml
app_name: myapp
host: blr1.adityaj.in
app_root: "{{ data_mount }}/apps/myapp"

type: container

pod:
  name: myapp
  ingress_network: myapp-ingress-net

ingress:
  main_domain: myapp.adityaj.in
  tls: true

# Static paths served from /srv/data/static/myapp/public
static_paths:
  - path: "/static/*"
    root: "assets"  # serves from /srv/data/static/myapp/public/assets
    strip_prefix: true
  - path: "/docs"
    root: "docs"    # serves from /srv/data/static/myapp/public/docs
    strip_prefix: false
  - path: "/images/*"
    strip_prefix: true  # uses default root: /srv/data/static/myapp/public

containers:
  - name: api
    image: mycompany/api:latest
    proxy_paths: ["/*"]
```

**Static-only with subpath** (if static files are in a subdirectory)

```yaml
app_name: docs
host: blr1.adityaj.in

type: static

ingress:
  main_domain: docs.adityaj.in
  tls: true
  caddy_directives: |
    # Override root to serve from a subdirectory
    root * /srv/data/static/docs/public/docs
    file_server
```

### 4. Caddy as System Service
- Quadlet mounts:
  ```
  Volume=/srv/data/system/ingress/caddy/config:/etc/caddy:rw,Z,U
  Volume=/srv/data/system/ingress/caddy/data:/data:rw,Z,U
  Volume=/srv/data/static:/srv/data/static:ro,U,Z
  ```
- Static files location: `/srv/data/static/{app}/public` (read-only mount)
- Snippet example (generated):
  ```caddy
  wiki.adityaj.in {
    tls internal

    handle / {
      reverse_proxy wiki-frontend:80
    }

    handle /admin/* {
      reverse_proxy wiki-admin:8080
    }

    encode zstd gzip
    header Strict-Transport-Security "max-age=31536000;"
  }
  ```
- Static site snippet:
  ```caddy
  blog.adityaj.in {
    root * /srv/data/apps/blog/public
    file_server
    encode zstd gzip
  }
  ```
- Always reload: `caddy reload --config /etc/caddy/Caddyfile`

### 5. Lifecycle Guidelines
- `app_manage.yml`: host-filtered via `--limit`, confirmation on destroy
- `app_migrate.yml`: stop → rsync `/srv/data/apps/<app>` & `/srv/data/system/ingress/caddy/config` (if needed) → cleanup source → start target
- Destroy: disconnect Caddy → remove nets → rm quadlets/volumes/snippet → rm data dir (confirm by typing app_name)

### 6. Code Style & Ansible Patterns
- FQCN always
- `loop` + `loop_control` preferred
- `become_user: "{{ podman_user }}"` for user tasks
- Idempotency: `creates`, `changed_when: false`, check-before-act
- Commit: `<type>(<app>): description` e.g. `fix(blog): preserve ownership on static sync`

### 7. Boundaries – Never Do These
- No shared networks
- Avoid port publishing to host unless there is no viable alternative through Caddy or per-app network routing. Any exception must be explicit in `app.yml`, app-scoped, justified by protocol limitations (for example Git SSH), and should use an unprivileged port unless a stronger reason is documented.
- No rootful Podman
- No Caddy restart — reload only
- No data outside `/srv/data`
- No manual edits outside Ansible

### 8. Quick Commands
```bash
ansible-playbook app_manage.yml -e "app=wiki operation=start" --limit blr1.adityaj.in
ansible-playbook app_migrate.yml -e "app=blog source_host=blr1 target_host=blr2"
journalctl --user -u caddy -f
podman network ls | grep ingress
```

**Improvement note**:  
These guidelines and examples are the current agreed baseline.  
During development, agents may propose refinements (e.g. better error handling in migration, additional hardening, metrics integration) — but **only** after explaining impact on isolation, rootless safety, migration, and reload behavior.

Thank you for contributing to a clean, durable, production-grade platform! Remember always put on a Principal Architect hat on, and assume a single person startup - must be secure, must need minimal maintenance, must be easy to recall in times of crisis. Consistency in pattern is important.
