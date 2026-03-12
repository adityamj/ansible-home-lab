# Rootless Podman + Quadlet Ansible Repo

This repository deploys stateful applications to Debian hosts using rootless Podman, systemd user Quadlets, per-host Caddy ingress, and containerized Restic for backups/migrations. Caddy and Restic are only run as containers—no host packages are installed beyond Podman and basic tooling.

## Quick start

```bash
ansible-galaxy collection install -r collections/requirements.yml
ansible-vault encrypt group_vars/all/vault.yml
```

1. Populate `inventories/production/hosts.yml` with your hosts.
2. Map apps per host in `host_vars/<host>.yml` via `assigned_apps`.
3. Adjust `group_vars/all/main.yml` for usernames, paths, and images.
4. Define apps in `apps/<app>/app.yml`.
5. Set `apps_root` to the controller path that contains app folders (for example `/path/to/control-repo/apps`). If omitted, it defaults to `apps` for backward compatibility.
6. Store secrets and overrides in `group_vars/all/vault.yml` and encrypt it.
7. Bootstrap hosts and deploy: `ansible-playbook bootstrap.yml -e "apps_root=/path/to/control-repo/apps"`.
8. Deploy apps: `ansible-playbook deploy.yml -e "apps_root=/path/to/control-repo/apps"`.

## What gets installed

- Podman (from native Debian repos, non-root only).
- Rootless Quadlet-managed containers for all apps and Caddy.
- `podman-auto-update.timer` enabled for the podman user so Quadlet containers refresh from their registries.
- Per-app ingress networks (`<app>-ingress-net`) and optional internal networks (`<app>-internal-net`) to isolate apps; Caddy joins only ingress networks for apps on its host.
- All persistent data lives under `/srv/data`.
- Restic runs ad-hoc via `podman run` using `roles/app_runtime/files/restic_wrapper.sh`; no host Restic binary is required.
- SSH hardening via `/etc/ssh/sshd_config.d/99-ansible-hardening.conf`: key-based auth only, limited auth attempts, optional `AllowUsers`. Default keeps root key login allowed (`PermitRootLogin prohibit-password`) so Ansible can still connect—adjust as needed.

## App model

- App definitions live one per file under `<apps_root>/<app>/app.yml`; the folder name is the app name.
- Host assignment is controlled in `host_vars/<host>.yml` via `assigned_apps`.
- Each app may have multiple containers; set `entrypoint: true` on the container Caddy should proxy by default (catch-all).
- Volumes are defined by name plus `container_path`; the host path is derived automatically as `<app_data_base_path>/<app>/<volume name>` unless overridden.
- Each app gets an ingress network (`<app>-ingress-net`) and optional internal network (`<app>-internal-net`) via `pod.ingress_network`/`pod.internal_network`.
- Static-only apps use `type: static`; static files live under `/srv/data/static/<app>/public`.
- Mixed apps can define `static_paths` to serve filesystem paths from `/srv/data/static/<app>/public` alongside proxies.
- Secret env overrides come from `vault_app_env_overrides` in the vaulted vars file.
- Optional per-container `mounts` can stage files or directories under the app’s data path and mount them read-only into the container. Each mount needs `relative_path` (under the app base), `container_path`, and either `content` or `src`. Multiple mounts are supported.
- Default mount sources resolve to `<apps_root>/<app>/files/<relative_path>`; set `src` to override.
- Optional per-container `proxy_paths` can add path-based reverse proxy routes on the app's domains. This is useful when an app has multiple containers that should share the same domain (e.g., `/api/*` to an API container, and everything else to the web container).
  - `proxy_paths` is a list of objects with:
     - `path`: a Caddy path matcher like `/api/*`
     - `strip_prefix`: optional, defaults to `true` (uses `handle_path` so `/api/foo` becomes `/foo` upstream). Set `false` to preserve the prefix.
  - `service_port`: optional container port Caddy should connect to without publishing it on the host; defaults to `8080`.
- Optional per-container `published_ports` can expose a direct host port only when there is no viable alternative through Caddy or the app ingress network (for example, Git SSH).
  - `published_ports` is a list of objects with:
    - `host_port`: required host port to bind
    - `container_port`: required container port to expose
    - `protocol`: optional, defaults to `tcp`
    - `host_ip`: optional bind address, defaults to all interfaces
  - Prefer unprivileged ports and keep the exposure app-specific and justified.

## Playbooks

- `bootstrap.yml`: Prepare hosts and deploy Caddy/apps.
- `deploy.yml`: Deploy Caddy and applications.
- `deploy_apps_only.yml`: redeploy only the app Quadlets (skip host prep and Caddy). Pass `-e target_app=<app>` to limit to a single app.
- `manage_services.yml`: restart/stop/start app services on their assigned host (`-e target_app=blog -e target_state=restart`).
- `01_backup_and_stop.yml`: stop an app on its current host and back up its data via the Restic container (`-e target_app=blog`).
- `02_deploy_and_restore.yml`: deploy an app to its mapped host and restore data if a Restic snapshot exists (`-e target_app=blog`). If no backup exists (new app or first deploy), it proceeds with an empty directory.

App deployment roles honor the optional `target_app` variable to constrain deployments to a single app on its mapped host.

## Testing on Testbench

To deploy to a testbench host instead of production:

- Add the testbench host to `inventories/production/hosts.yml`.
- Update `host_vars/<host>.yml` to point `assigned_apps` at the testbench host.

## Notes

- Target OS must be Debian; the roles fail early otherwise.
- No host-level Caddy or Restic packages are installed—only Podman and dependencies.
- Keep `apps/<app>/app.yml` and `host_vars/<host>.yml` under version control as the authoritative mapping for deployments and migrations.
