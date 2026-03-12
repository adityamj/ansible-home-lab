# Quadlets in this setup

Quadlets let systemd own the lifecycle of Podman containers using simple `.container` unit files in `~/.config/containers/systemd/` for the `podman_user`. In this repository:

- Every long-running container (apps and per-host Caddy) is defined as a Quadlet.
- Quadlets are rendered from Jinja templates and dropped into the podman user's systemd directory.
- systemd user units are enabled/started with `systemctl --user` (invoked via `runuser -l {{ podman_user }}`).
- `AutoUpdate=registry` is set on each Quadlet so `podman-auto-update.timer` can refresh images periodically.
- Networks, volumes, and environment variables are declared in the Quadlet `[Container]` section. Each app gets its own ingress network (`<app>-ingress-net`) and optional internal network (`<app>-internal-net`); Caddy joins only ingress networks on its host so it can proxy without exposing apps to each other.

### File locations

- Quadlet files: `/home/{{ podman_user }}/.config/containers/systemd/*.container`
- The rendered files come from `roles/apps/templates/quadlet/app.container.j2` and the inline Caddy Quadlet in `roles/caddy_host/tasks/main.yml`.
- SSH hardening is applied separately via `/etc/ssh/sshd_config.d/99-ansible-hardening.conf` to enforce key-only access and sane auth limits.

### Workflow

1. Ansible renders Quadlets for the app containers (and a dedicated one for Caddy).
2. The systemd user daemon is reloaded.
3. Units are enabled and started, giving systemd responsibility for restart and ordering.
4. `podman-auto-update.timer` runs for the podman user to pull updated images and restart services automatically.

### Restic exception

Restic is intentionally **not** run as a Quadlet. Backups/restores are ad-hoc and executed via the Restic container using the wrapper script (`podman run --rm ... restic ...`) so jobs stay on-demand rather than persistent services.
