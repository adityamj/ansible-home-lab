# Rootless Application Hosting with Ansible

This repository prepares Debian application hosts and deploys isolated services
with rootless Podman, systemd Quadlets, and one rootless Caddy ingress container
per host.

The reusable automation is intentionally separate from deployment-specific
inventory, vaulted values, and application definitions. A control repository
can vendor this project as `automation/` and provide those inputs alongside it.

## Requirements

- A Debian 12 or 13 host with cgroups v2 and rootless user namespaces.
- Podman 5.0 or newer with the Quadlet system generator.
- An explicit `storage_mode` of `mount`, `zfs`, or `plain`.
- SSH access with privilege escalation for host preparation.
- Ansible collections from `collections/requirements.yml`.

`mount` requires an existing filesystem mounted at `data_mount`. `zfs` requires
an existing imported pool and creates only the fixed managed dataset hierarchy.
`plain` creates an ordinary directory and is intended only for disposable labs
and tests. The automation never selects or formats a block device.

## Commands

Install collections:

```bash
ansible-galaxy collection install -r automation/collections/requirements.yml
```

Prepare or repair one host:

```bash
ansible-playbook automation/bootstrap.yml --limit app-host-01.example.com
```

Reconcile every app assigned to one host:

```bash
ansible-playbook automation/deploy.yml --limit app-host-01.example.com
```

Reconcile one assigned app without changing unrelated apps:

```bash
ansible-playbook automation/deploy.yml \
  --limit app-host-01.example.com \
  -e app=sample-app
```

`bootstrap.yml` prepares the platform, including the Caddy baseline and firewall
service wiring. `update_platform.yml` explicitly reapplies that same platform
configuration on existing hosts while preserving managed app firewall allowances.
`deploy.yml` manages app resources, Caddy app routes/network attachments, and live
firewall rules; it does not reinstall packages or reconcile the Caddy baseline.

```bash
ansible-playbook automation/update_platform.yml --limit <host>
```

When upgrading an existing `PublishPort=` installation, run `update_platform.yml`
first to move Caddy from `rootlessport` to pasta, then run one full `deploy.yml`
to migrate app-declared ports and commit their companion-unit ownership. The
platform update performs the required one-time controlled Caddy restart.

Targeted deployment assumes bootstrap and an initial full deployment have
already completed. It also requires the current `assigned_apps` set to match the
host manifest committed by that full deployment. Additions, removals, and app
directory renames require another full deployment first.

Initial bootstrap creates the firewall with SSH and Caddy ports. A later
bootstrap preserves an existing authoritative managed policy and its app ports;
full deployment owns the complete platform-plus-application allowance.

**Ansible exclusively owns the host nftables ruleset.** Each replacement submits
`flush ruleset` and the complete desired policy in one atomic nft transaction.
All other host nftables tables, including iptables-nft, VPN, NAT, bridge, or
forwarding rules, are removed. Rootless containers' private network namespaces
are not affected. There is no intermediate empty-ruleset window.

Drift detection checks the entire live ruleset, and deployment verifies that
only the managed table remains. Independent iptables-legacy policies are rejected
because nftables cannot remove that separate backend. Platform reconciliation
owns `/etc/nftables.conf` and removes UFW, fail2ban, firewalld, and the
netfilter/iptables-persistent packages. Do not run other host firewall writers.

For existing hosts with the former table-only policy, run the one-time migration
**before** platform updates or routine deployment:

```bash
ansible-playbook -i <inventory> migrate_firewall_layout.yml --limit <host>
```

It recognizes the old managed policy, preserves its currently deployed port
allowances (not today's app catalog), validates the replacement, applies the
complete ruleset, and installs the authoritative boot configuration. It does not
restart app containers. Independent iptables-legacy policy or unrecognized input
requires manual review rather than guessed conversion. Recognition checks the
entire supported rule sequence, not comment markers or port substrings: only the
old add/delete framing or the new flush framing plus the exact managed input
chain and numeric TCP/UDP allowance lists are accepted. Extra rules, tables,
includes, or reordered statements are rejected before backup/promotion. SSH and
standard ingress allowances must already be present; migration does not expand
policy to compensate for missing allowances.

Removal of unsupported firewall-manager packages always attempts authoritative
policy reapplication, even if package removal fails. The removal failure remains
fatal and must be corrected before retrying. This cannot protect against loss of
the host or Ansible connection during removal; retain out-of-band access.

Original policy, boot entry point, and live ruleset are backed up under
`/var/lib/ansible-app-host/firewall-migration-backup/` with root-only permissions.
Backups are not overwritten on retries. Review foreign rules first: they are
backed up but removed, not imported into the new policy. Rerun the migration if
interrupted; it accepts already-converted policy. There is no automatic rollback.
Use out-of-band access for the first migration on a production host.

### Lightweight deployment transfers

Rendered artifacts and declarative configuration directories use Ansible `copy`
over its existing SSH connection, not a separate rsync SSH process. Exact file
manifests drive stale-file cleanup inside those managed directories; writable
volumes are never mirrored by deployment. Empty directories and configured modes
are preserved. Configuration changes, including deletions, still trigger the
normal application refresh logic. Caddy's temporary validation workspace is
separate from the artifact tree.

This avoids rsync's additional security-token authentication for deployment;
reconnecting Ansible itself may still require authentication. Bulk cross-host
data migration remains a separate rsync-based workflow.

## Cross-host application migration (planned)

See [the host migration plan](migration.md) for initial rsync, inactive target
staging, source shutdown, final rsync, target activation, and old-host proxying.
The cross-host workflow is not implemented yet. Ordinary deployment supports
inactive applications, but does not provide migration's transfer coordination
or data-authority safeguards.

## Inventory

Assign apps in host variables:

```yaml
assigned_apps:
  - sample-app

ssh_port: 22
storage_mode: mount
```

`ssh_port` is required and must match the port Ansible uses for bootstrap.

To have bootstrap create the application datasets in an existing imported ZFS
pool, set its pool name in host variables:

```yaml
storage_mode: zfs
use_zpool: tank
```

This creates `tank/app-host` at `data_mount`, plus `apps`, `static`, and
`system` child datasets at their corresponding paths. The `apps` dataset uses a
`16K` record size; the other datasets use the ZFS `128K` default. Compression
is explicitly disabled for this hierarchy. Dataset creation happens before
the rootless Podman account is created.

Bootstrap refuses to cover an unrelated mount or a non-empty unmounted
directory. The pool must already exist and be imported, and the `zfs` and
`zpool` commands must already be available. Pools imported with `altroot` are
rejected because their effective mount paths differ from `data_mount`. Deploy
verifies the exact root and child dataset mount sources before writing data.
Changing modes never removes existing datasets or persistent directories.

`assigned_apps` is authoritative during a full deployment. Removing an app
from the list removes only automation-managed runtime resources. Persistent app
and static data are retained.

Root-owned reconciliation records and secret hash markers are stored beneath
`/var/lib/ansible-app-host`; persistent application and ingress data remain
beneath `data_mount`.

Keep secrets in encrypted variables:

```yaml
vault_secret_hash_pepper: "replace-with-a-random-secret"
vault_podman_secrets:
  sample-app:
    database_password: "replace-with-the-secret-value"
```

## App Model

### Application activation state

Apps default to `desired_state: running`. Set this top-level field in `app.yml`
to `inactive` to prepare the app without running its application services.
Alternatively, override it per host (useful for a migration destination):

```yaml
assigned_apps:
  - sample-app
app_desired_states:
  sample-app: inactive
```

Override keys must be assigned apps; values must be `running` or `inactive`.
A running app cannot require an inactive app. Targeted deployment still includes
its dependency closure: explicitly choose dependency states rather than assuming
that making one app inactive also makes its dependencies inactive.

Inactive deployment prepares images, secrets, storage, configuration and normal
administrator-owned Quadlets. Application services are persistently masked and
stopped **before** mounted configuration/data preparation. Networks are created
and started, but application containers are not started; a stopped container
object need not exist. Systemd masks prevent boot/dependency/manual systemd
activation until the desired state changes. Direct manual Podman starts and
external writers are unsupported; unexpected running inactive containers fail
deployment rather than being force-killed.

Caddy site/drop-in files become comment-only managed placeholders. Their prior
routes and network references are removed even in targeted deployment, without
losing file ownership or touching unrelated sites. Live Caddy app-network
attachments are disconnected after its graceful reload. Inactive static apps
also have no public routes. Network/storage resources remain present, and
published-port reservations/firewall allowances are unchanged (no application
listener is started).

Switching back to `running` unmasks and starts selected services and restores
Caddy routing. `force_refresh` never overrides inactivity. Ordinary
`systemctl enable/disable` is not used for generated Quadlet services. No new
persistent activation ledger is maintained: inventory/catalog express intent,
and systemd holds the masks. The usual ownership ledger still tracks resources.

After interruption, the existing `force_refresh: true` recovery rule still
applies to already-promoted configuration/Caddy routing; it never starts an
inactive app. Inactivity alone is not a database consistency or migration
rollback guarantee.

Each app lives at `<apps_root>/<app>/app.yml`. The directory name is the app
name. Names and domains must be globally unique. Every deployment invokes the
controller-side Python compiler in `tools/app_model/`; it validates and
normalizes the complete catalog, resolves host assignments and dependency
closures, and emits a versioned JSON plan for Ansible. Ansible remains
responsible for observing and mutating remote host state.

Minimal container app:

```yaml
type: container

domains:
  - app.example.com

ingress:
  caddy_directives: |
    encode zstd gzip

containers:
  - name: web
    image: registry.example.com/team/web:1.2.3
    service_port: 8080
    entrypoint: true
    volumes:
      - name: data
        container_path: /var/lib/app
    env:
      APP_MODE: production
    secrets:
      - name: database_password
        type: env
        target: DATABASE_PASSWORD
```

Host volume paths are always derived as
`<data_mount>/apps/<app>/volumes/<volume-name>`. Managed read-only mount sources
live in the controller tree beneath `apps/<app>/mounts/` and are copied to
`<data_mount>/apps/<app>/mounts/<relative-path>` on the remote host. App
definitions cannot provide arbitrary host paths.

Raw ingress directives can address a container in the current app or a declared
dependency with `{{ container:<app>/<container> }}`. The compiler validates the
reference and replaces it with the versioned physical container name; app files
must not embed compiler-generated names directly.

Writable volumes are reconciled like PVCs: deployment creates a missing volume
root and requires an existing root to be a real directory, but it never changes
the ownership, mode, or contents of an existing volume. Managed read-only
mounts are reconciled like ConfigMaps and remain authoritative copies of their
controller sources. Writable mounts intentionally omit Podman's recursive `:U`
ownership adjustment. A container with writable volumes may declare a numeric
`user: "UID:GID"`; a missing empty root is initialized once with that ownership
through `podman unshare`. Existing roots are never changed automatically.

Existing hosts must run the one-time volume migration before deploying this
layout. The migration stops affected app services, moves writable volumes, and
leaves services stopped for the subsequent deployment. Managed mount files are
not migrated because deployment copies them again from the controller. Routine
deployment assumes the namespaced layout and does not inspect legacy paths:

```bash
ansible-playbook -i <inventory> migrate_volume_layout.yml
ansible-playbook -i <inventory> migrate_volume_layout.yml \
  --limit app-host-01.example.com -e app=sample-app
```

### Account and storage observations

Account identity is loaded explicitly after provisioning or at an execution-phase
entry point. Manager reconciliation reuses that identity and performs one
idempotent start operation; it does not look the account up again. There is no
persistent "host already validated" cache.

Live storage is checked before runtime activation and again before app data
writes. These are separate safety boundaries, not reusable cached observations.
ZFS verification reads one flat mount-table snapshot per boundary and checks all
four required datasets against it. Plain mode requires an existing root not to
be a mountpoint (util-linux exit 32); inspection errors are not accepted as proof.

Quadlets live under `/etc/containers/systemd/users/<uid>`, but Podman images and
container layers use its **effective graphroot**, discovered by `podman info`
during platform reconciliation. That path is validated without moving data or
changing ownership. It need not be the default home-directory store. Podman info
may initialize an empty store, so it runs only after subordinate IDs are ready.
Missing mappings combined with explicit storage configuration require manual
review before allocation; automation must not guess where existing storage lives.

### Deployment implementation and scope

The apps role has one steady-state executor, statically imported in this order:
`preflight.yml`, `prepare.yml`, `stage.yml`, `validate.yml`, `apply.yml`, and
`finalize.yml`. Migration-specific entry points reuse it after their own shutdown
and compatibility work. Obsolete writers and their activation sources are retired
before replacement Quadlets are promoted.

A **full deployment** reconciles the complete desired published-port allowance:
new declarations open ports and deleted declarations remove those allowances.
Published ports are owned by per-container pasta services instead of Podman's
source-rewriting `rootlessport`; the services attach directly to each container
network namespace and preserve remote addresses. The complete host nftables
ruleset is replaced; established connections remain allowed by the
connection-tracking rule. Firewall service wiring and package policy remain
platform operations.

A **targeted deployment** must keep the committed host port allocation unchanged.
Port changes and removal/renaming of tracked containers or networks require a
full deployment and are rejected before app mutation. This avoids both stale
firewall rules and old writers surviving a targeted replacement.

### Caller-controlled refresh

`force_refresh` is a boolean, defaulting to `false`:

```bash
# Trust unchanged configuration; refresh only when changes are detected.
ansible-playbook -i <inventory> deploy.yml

# Refresh every selected app service even when files are unchanged.
ansible-playbook -i <inventory> deploy.yml -e '{"force_refresh": true}'

# Limit app restarts to the selected app and its dependency closure.
ansible-playbook -i <inventory> deploy.yml -e app=example \
  -e '{"force_refresh": true}'
```

Forced refresh restarts selected app containers, reloads user systemd and Caddy,
and reapplies the authoritative firewall on full deployments. It does not restart
Caddy, network units, or unrelated host services. Targeted deployment still skips
firewall reconciliation and requires unchanged port allocations. Caddy is shared,
so its graceful reload applies the complete active routing configuration.

Without force, stopped desired services are still started and detected changes
are applied normally, including detected firewall drift. Unchanged files are
trusted: after an interruption between promotion and runtime refresh, rerun with
`force_refresh: true` if you need to guarantee that processes load those files.
The ownership ledger does not override the caller's refresh choice.

Hosts that ran the former pending-transaction executor may have a
`pending-apply.json`. If present, run `migrate_deployment_state.yml` once before
using this executor. It merges tracked resource names into `host-state.json`
before removing the obsolete record; normal deployment never reads it.

### Measuring routine deployment

Task count is not a performance guarantee: loop iterations, retries, and remote
connection latency dominate many deployments. Profile an unchanged deployment,
a single-app change, and interrupted-run recovery on a disposable host:

```bash
ANSIBLE_CALLBACKS_ENABLED=ansible.posix.profile_tasks \
  ansible-playbook -i <inventory> deploy.yml --limit <disposable-host>
```

Record elapsed time, changed/skipped counts, and loop sizes. Routine deployment
intentionally retains storage validation, resource ownership tracking, live
firewall verification, and application readiness checks. Do not remove those
checks merely to meet a numerical task budget.

### One-time container-name migration

For existing hosts whose volume layout is already deployed, run
`migrate_container_names.yml` once before routine deployment:

```bash
# From the automation checkout; the consuming inventory supplies apps_root.
ansible-playbook -i <inventory> migrate_container_names.yml --limit <host>
ansible-playbook -i <inventory> deploy.yml --limit <host>
```

The migration compiles the catalog and verifies canonical existing volume roots
and running ingress. Before shutdown it inventories all rootless containers and
applicable Quadlet sources. Unknown names, naming overrides, alternate persistent
user units, and unsupported source locations are rejected—not guessed or deleted.
Keep the catalog's logical app/container names identical to the deployed legacy
catalog during this migration. Combine logical renames with a separately reviewed
migration, not this physical-name transition.

The current administrator-owned Caddy baseline is required: its effective
`SourcePath` must be `/etc/containers/systemd/users/<uid>/caddy.container`, its base
network must be `caddy-egress.network`. Before cutover, each app's network drop-in
may contain its exact legacy `Network=<app>-ingress.network` reference or its
desired stable references. After deployment, the migration rereads drop-ins and
requires all desired stable references before removing legacy network sources or
runtime networks. Missing drop-ins, cross-app references, and service overrides
are rejected. Legacy user-owned `caddy.container` and service overrides are
rejected before app shutdown. If present, back up and retire those activation
sources outside Quadlet/systemd search paths, then reconcile the current platform
with `update_platform.yml` (after firewall migration). Review that platform change
separately; the container-name migration does not silently replace ingress.

It persistently masks legacy services **before** installing
any stable-name Quadlets, then stops the old writers and invokes the regular
deployment executor. Masks remain in place so an interrupted migration or reboot
cannot reactivate the old writers. After deployment succeeds it removes the
unused legacy default ingress networks without force. Nonstandard legacy
network names require manual review. Do not rerun the volume migration.

Legacy app Quadlets must be root-owned (`root:root`) under
`/etc/containers/systemd/users/<uid>/`, just like stable-name Quadlets. Migration
changes physical names, not source location or ownership. User-directory Quadlet
sources are rejected, even if root-owned, so a shadowed copy cannot reactivate
after reboot. Legacy container sources are retired after stopping the writer;
default legacy network sources are retired after successful deployment. Their
filenames are `<app>-ingress.network`, while their `NetworkName` values are
`<app>-ingress-net`; this exact mapping is validated before cutover. Caddy's
`caddy-egress.network` / `caddy-egress-net` is platform-owned and is not retired.
Static apps are excluded from legacy network candidates. Cleanup inventories
live network names and Caddy attachments, disconnects only attached legacy
networks, and removes only existing legacy networks without force. Already
absent/detached resources are skipped on reruns; other Podman failures remain fatal.
Persistent `/dev/null` service masks remain in the user's systemd directory;
these are activation safeguards, not Quadlet sources.

Accepted reruns may contain catalog-matched legacy and stable containers/sources
and `/dev/null` masks from an earlier attempt. Completed hosts may rerun with no
legacy containers. Unknown resources fail before cutover in all these states.

Host-side artifact validation happens in the shared deployment executor, after
legacy shutdown. A validation or application-start failure therefore leaves
applications stopped until corrected and rerun; there is no automatic rollback
to legacy writers. Existing writable volume ownership and contents are preserved.

Migration is host-wide (`--limit` selects hosts; `-e app` is rejected) because
apps can share ingress dependencies. Routine `deploy.yml` contains no legacy
container/network cleanup; its garbage collection uses the saved host state.
Routine deployment records resource names in `host-state.json` before app
changes. After successful full reconciliation it prunes ownership to the desired
resources. The same file stores the last successful assignment and port metadata;
it is not a transaction log or a configuration-refresh acknowledgement.
To refresh already-promoted files after interruption, rerun with
`-e '{"force_refresh": true}'`. New volume initialization has a separate durable intent marker
and can resume only while the root remains empty. Existing populated roots are
never initialized again.

If interrupted, rerun the migration on the same host with `force_refresh: true`.
It does not automatically
restart legacy writers after replacements have started. Validate this procedure
on a representative rootless Podman host before production use.

### Networks

A container app with no `networks` declaration receives one logical `ingress`
network that Caddy may join. Every `-` inside a logical component is encoded as
`--`; single separators and resource-kind tags make physical names collision-proof:

```text
<encoded-app>-net-<encoded-network-alias>
```

For example, `foo-bar`/`ingress` becomes `foo--bar-net-ingress`. Containers use
`<encoded-app>-ctr-<encoded-container>` and secrets use
`<encoded-app>-sec-<encoded-secret>`.

Multiple tiers can be declared explicitly:

```yaml
networks:
  - name: ingress
    caddy: true
  - name: backend
  - name: database
    internal: true

containers:
  - name: web
    image: registry.example.com/team/web:1.2.3
    networks: [ingress, backend]
    service_port: 8080
    entrypoint: true

  - name: api
    image: registry.example.com/team/api:1.2.3
    networks: [backend, database]

  - name: db
    image: docker.io/library/postgres:17.2
    networks: [database]
```

Caddy receives one app-owned Quadlet drop-in per app and joins only networks
marked `caddy: true`. A live network connect applies additions without a Caddy
restart. Drop-ins make the same membership persistent across host reboot or
natural container recreation.

### Routing

Set one `entrypoint: true` container for the default route. Additional
containers can declare path routes:

```yaml
proxy_paths:
  - path: /api/*
    strip_prefix: true
```

`service_port` defaults to `8080`. App ports are not published on the host.
Use `published_ports` only for protocols Caddy cannot proxy. Declared host
ports are exposed by a source-preserving pasta companion service through a
platform-owned namespace anchor and added to the managed firewall automatically.
The anchor permits non-root application processes without introducing a TCP
proxy or losing the client address. Deployment rejects duplicate allocations or
collisions with SSH and Caddy.

### Static Content

Static apps are served directly by Caddy:

```yaml
type: static

domains:
  - docs.example.com

ingress:
  caddy_directives: |
    encode zstd gzip
```

Content belongs under `<static_base_path>/<app>/public`. Deployment reconciles
only declared root directories as `root:podman` mode `2775`; it never copies,
validates, overwrites, or deletes their descendants. Publishers should use
group-writable, world-readable output, normally directories mode `2775`, files
mode `0664`, and `umask 0002`. `static_paths` can add relative roots beneath the
same app directory. Caddy mounts the complete static tree read-only.

### Managed Configuration

Read-only configuration can be synchronized from the controller:

```yaml
mounts:
  - relative_path: config/
    src: sample-app/mounts/config/
    container_path: /etc/sample-app/
    directory_mode: "0755"
    file_mode: "0644"
```

Directory synchronization is convergent and removes remote files deleted from
the declared source. Controller sources and host destinations are constrained
to their configured roots. Managed configuration cannot contain secrets and
must be readable by any container UID. Podman's ownership-changing `:U` mount
option is intentionally not used.

### Hardening

Generated containers drop all capabilities, use no-new-privileges and a
read-only root filesystem, and receive bounded tmpfs, memory, PID, and CPU
controls. Add capabilities explicitly only when required by the image.

Registry auto-update is disabled. Image changes are applied through reviewed
full or targeted deployments.

## Validation

Run the controller-side checks without contacting a host:

```bash
python3 tools/compile_app_model.py --apps-root <apps-root> --check
ansible-playbook --syntax-check -i <inventory> bootstrap.yml
ansible-playbook --syntax-check -i <inventory> deploy.yml
ansible-playbook --syntax-check -i <inventory> migrate_volume_layout.yml
ansible-lint bootstrap.yml deploy.yml update_platform.yml migrate_*.yml roles
```

The previous automated test suites were intentionally removed during the rewrite.
There is currently no automated regression suite. Syntax checks and catalog
compilation do not establish runtime correctness. Track replacement coverage and
disposable-host verification in `TODO.md`.

## Safety Boundaries

- Rootless Podman only.
- No shared app networks.
- No data outside `data_mount`.
- No static descendant content mutation.
- No Caddy restart during routine deployment.
- No destructive storage cleanup during reconciliation.
- Existing writable-volume metadata and contents are not reconciled.
- No automatic disk formatting, DNS management, backup, or migration workflow.
- App definitions are host-administrator-trusted input; raw Caddy directives
  are not a tenant sandbox.
