# App-to-host mapping and migrations

## Single source of truth

App definitions live in `apps/<app>/app.yml` (one file per app). Host assignment lives in `host_vars/<host>.yml` via `assigned_apps`. All deployment, Caddy config, and backup/restore logic derive from that mapping. There is no horizontal scaling; one app -> one host.

Key fields per app:
- `domains`: list of FQDNs served by the app.
- `type`: `container` or `static`.
- `pod.ingress_network` / `pod.internal_network`: ingress and internal network names.
- `containers`: list of containers; mark one container with `entrypoint: true` for default proxying.
- `containers[].healthcheck`: optional per-container ingress health config used by Caddy for upstream health and retry behavior.
- `containers[].runtime_healthcheck`: optional per-container Podman healthcheck; keep it image-specific and only set it when the image has a valid runtime probe.
- `static_paths`: optional static handlers for mixed apps. Files should be placed in `/srv/data/static/<app>/public`.

`healthcheck` and `runtime_healthcheck` serve different layers:
- `healthcheck` is ingress-side only. It tells Caddy what to probe on the app network and does not define container runtime state.
- `runtime_healthcheck` is optional runtime behavior inside Podman. Use it only when the image supports a trustworthy probe command or localhost endpoint.

Examples:

```yaml
containers:
  - name: vikunja
    entrypoint: true
    healthcheck:
      path: /health
    runtime_healthcheck:
      command: ["/app/vikunja/vikunja", "doctor"]

  - name: headscale
    entrypoint: true
    healthcheck:
      path: /health
    runtime_healthcheck:
      command: ["headscale", "health"]
```

In practice, `runtime_healthcheck` should stay explicit and image-specific; many images do not expose a native exec-form probe, so `healthcheck` remains the primary ingress safeguard.

## Migration flow

Use two playbooks plus a `host_vars` edit to move an app while keeping the mapping authoritative.

1. **Backup and stop on current host**
   - `ansible-playbook 01_backup_and_stop.yml -e target_app=<app>`
   - Stops the app containers on the host currently assigned in `host_vars/<host>.yml`.
   - Runs the Restic container via the wrapper to back up the app root tagged with `restic_backup_tag`.

2. **Update mapping and deploy on new host**
   - Move the app name between `assigned_apps` lists in the relevant `host_vars/<host>.yml` files.
   - `ansible-playbook 02_deploy_and_restore.yml -e target_app=<app>` runs on the newly assigned host, restores data if snapshots exist (otherwise continues with an empty directory), redeploys Quadlets, and starts services.

Because all logic keys off `assigned_apps`, running `deploy.yml` after a migration will only deploy the app to its new host while leaving the old host untouched.
