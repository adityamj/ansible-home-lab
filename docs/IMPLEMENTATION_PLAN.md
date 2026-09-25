# Platform Implementation Reference

Status: implemented baseline

## Supported Workflows

- `bootstrap.yml` prepares or repairs a host. It does not deploy Caddy or apps.
- `deploy.yml` without `app` performs authoritative full reconciliation.
- `deploy.yml -e app=<app>` reconciles one assigned app and its transitive
  `requires_apps` closure.

## Host Preparation

Bootstrap validates the Debian release, cgroups v2, user namespaces, and an
explicit storage mode before creating persistent children. `mount` requires a
real mountpoint, `zfs` creates a fixed `app-host` hierarchy in the configured
existing imported pool, and lab-only `plain` uses a protected ordinary
directory.

Every generated container service also runs a root-owned storage source guard
before startup, preventing later restarts from binding an unavailable mount or
the wrong ZFS hierarchy.
It installs the rootless Podman runtime without a distribution upgrade,
preserves existing subordinate ID allocations, enables linger, and starts the
user manager through systemd.

SSH and nftables candidates are syntax checked. The firewall replaces only its
own nftables table and does not flush unrelated rules. It permits the explicit
SSH port, Caddy HTTP/HTTPS and HTTP/3, and unique app-declared published ports.
Caddy and app-declared listeners use systemd-managed pasta companions attached
directly to their container network namespaces, preserving client addresses
without Podman's `rootlessport` connection proxy.
A dedicated systemd unit restores the managed table after nftables at boot;
the global nftables entry point remains otherwise untouched. UFW and fail2ban
are unsupported. Security updates are unattended; application images are not.

## Deployment Model

App definitions are normalized before remote app work. Domains and app names
are globally unique. Logical network aliases become app-owned physical network
names and Quadlet network units. Container routes must use a Caddy-facing
network, while backend-only networks remain isolated.

The compiler doubles hyphens inside logical name components and uses single
separators plus `ctr`, `net`, `sec`, and `vol` kind tags. Templates and tasks
consume compiler-emitted physical names rather than rebuilding them.

App dependency cycles and unassigned requirements are rejected before runtime
mutation. Quadlets may be rendered in any order; service actions follow the
topological app order, and cross-app unit relationships use soft `Wants=` plus
`After=` dependencies.

The base Caddy Quadlet has no generated app-network list. Each app owns a
`caddy.container.d/50-network-<app>.conf` source drop-in. Live network changes
use explicit checked Podman connect and disconnect commands, without restarting
Caddy.

Caddy configuration is copied to a candidate tree, changed there, validated,
and then promoted. App services start or restart before changed routes are
published. Reload uses the configured Unix admin socket. Automation-owned
control, Caddy configuration, and Quadlet trees reject symbolic links before
they are copied or promoted.
Targeted promotion is restricted to explicit selected-app files; the complete
candidate remains a validation context only.

## Storage and Secrets

Writable volume paths are derived beneath
`<data_mount>/apps/<app>/volumes`. Managed configuration sources live beneath
`apps/<app>/mounts` on the controller and their host destinations live beneath
`<data_mount>/apps/<app>/mounts`. Sources are constrained to `apps_root`,
synchronized convergently, and mounted read-only without recursive user-
namespace ownership changes. The separate `migrate_volume_layout.yml` playbook
stops affected app services and moves only legacy writable volumes.

Writable volumes follow a PVC-like contract. Deployment creates a missing
declared root and rejects a symlink or non-directory root, but never reconciles
metadata or contents below an existing root. Managed configuration follows a
ConfigMap-like contract and is enforced exactly from its controller source.
Writable bind mounts omit Podman's recursive `:U` ownership adjustment.
Managed configuration directories and files are respectively readable and
traversable, and readable, by any container UID so image-defined non-root users
can consume them without ownership-changing mount options.
For an explicit numeric container `UID:GID`, only a missing empty writable
volume root is initialized through `podman unshare`. A durable marker allows an
interrupted first initialization to resume without changing established roots.

Optional ZFS provisioning creates `<pool>/app-host/{apps,static,system}` before
runtime account creation. `apps` uses `recordsize=16K`; the remaining datasets
use the default `recordsize=128K`. Compression is disabled throughout the
managed hierarchy. Existing pools are consumed but disks and pools are never
created or formatted, and pools imported with an alternate root are rejected.
Deployment verifies the exact ZFS source and filesystem type for the root and
all three child datasets before Caddy writes persistent data, then invokes the
installed storage guard again before application reconciliation. Generated
services invoke the same guard before every start.

Static roots are derived beneath `<static_base_path>/<app>`. Deployment enforces
only the declared root-directory access contract; externally published
descendants remain unmanaged. Caddy receives the tree through a read-only bind
mount.

Podman secret names use `<encoded-app>-sec-<encoded-secret>`. A peppered
hash marker and Podman inspection both participate in reconciliation. Vaulted
values are never rendered into systemd `Environment=` lines.

## Managed State

One non-secret ownership record is stored per app. Full deployment uses these
records to remove stale services, containers, Quadlets, sites, drop-ins,
networks, and secrets. Persistent app data, static data, Caddy certificates,
and unmanaged resources are retained.

Targeted deployment updates the selected app and its dependency closure. It
does not perform host-wide cleanup or change resources outside that closure.
It requires the exact app assignment, naming version, host identity, and port
ledger committed in the host manifest by a successful full deployment. It
loads only selected app records and skips nftables reconciliation when their
published-port allocation is unchanged.

## Application Model Compiler

`tools/app_model` is the controller-side, typed boundary for application
declarations. Ansible invokes it for each inventory host and passes only
non-secret host inputs such as `assigned_apps`, `target_app`, `ssh_port`, and
the reserved Caddy network name. The compiler validates and normalizes the app
catalog, derives physical resource names, resolves dependency closures, checks
collisions, and returns a deterministic versioned JSON plan.

The compiler does not inspect hosts or choose runtime actions. Ansible combines
the desired plan with observed managed state and remains the sole owner of SSH,
filesystem, package, Podman, systemd, Caddy, and nftables mutations.

## Verification

Implementation requires:

- YAML parsing and Ansible syntax checks.
- Python compiler unit tests.
- `ansible-lint` at the production profile.
- Controller-side app-model and template rendering tests.
- Podman Quadlet generator dry-run before remote service actions.
- Caddy candidate validation before configuration promotion.

Host-level idempotency and reboot persistence must be verified on a prepared
Podman 5.x Debian host before a production rollout.

## Non-Goals

- Automatic disk selection or formatting.
- Static artifact publishing.
- DNS automation.
- Backup and restore implementation.
- Application migration.
- Generic service or container command interfaces.
