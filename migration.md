# Move an application to a new host

**Status: operator-serialized migration playbook.** Regular deployment supports
`desired_state: running | inactive` and per-host `app_desired_states` overrides.
`automation/migrate_app_host.yml` implements preparation, transfer, cutover,
handoff recovery and post-DNS cleanup for one dependency-free HTTP application
with named volumes and compiler-declared static trees. It resolves immutable digests from the running source,
pulls those digests on the target, then restores the unchanged catalog tags so
source and target Quadlets remain byte-identical without image drift. It
uses paired source/target inventory intent plus host-local barriers. There is no
inter-process lock, so one operator must still serialize migrations, but ordinary
application deployment refuses to run on either participating host while intent
is active. This workflow is separate from the existing on-host naming/storage
migrations.

## Sequence

| Step | Source host | Target host |
| --- | --- | --- |
| 1. Initial rsync | Continues serving and writing | Receives warm data copy; application activation blocked |
| 2. Stage runtime | Continues serving | Images, secrets and Quadlets prepared; no application containers started or enabled |
| 3. Stop source | Maintenance response; all application writers stopped and persistently fenced | Remains fenced |
| 4. Final rsync | Data frozen; source remains authoritative | Receives and verifies final consistent copy |
| 5. Start target | Remains fenced | Activation permitted; services start and are verified |
| 6. Proxy old traffic | Caddy forwards to target Caddy | Sole application writer and serving host |

DNS is changed manually after step 6. Cleanup is a later, explicit operation;
source persistent data is retained. Applications using non-WebSocket HTTP/1.1
Upgrade protocols may opt into `ingress.handoff_upstream_http1: true`; ordinary
apps retain Caddy's default upstream protocol negotiation.

The repository uses rootless Podman containers and systemd Quadlets, not a
shared Podman pod abstraction. “Setup pods” here means preparing the selected
application's existing runtime model, without introducing pods.

## Scope and prerequisites

- Migrate one app, or an explicitly reviewed group of inseparable dependencies.
  Do not silently start its dependency closure on the target. Review shared
  volumes, external databases, workers, timers, webhooks and scheduled jobs.
- Support HTTP/HTTPS applications behind Caddy, including WebSockets. For a
  static app or an app with `static_paths`, the complete
  `{{ static_base_path }}/<app>/` tree is warm-copied and checksum-mirrored during
  final cutover through a separate app-scoped write-only receiver. Any external
  static-data writer must be stopped by the reviewed frozen callback.
- Declared direct TCP/UDP ports require `migration_direct_port_policy=outage_until_dns`;
  the ordinary target deployment reconciles their compiler-owned nftables
  allowances and migration verifies live Podman mappings. The controller also
  verifies TCP reachability; UDP has no generic end-to-end response test. Direct
  ports are not forwarded by the old host and remain unavailable there until DNS
  changes.
- Prepare the target platform separately: rootless user, subordinate IDs,
  verified storage, Podman, user systemd, Caddy and authoritative nftables.
  Do not bootstrap or upgrade either platform during cutover.
- Use the same reviewed catalog revision on both sides. Resolve each running
  source image to its unique repository digest, pull that digest on the target,
  and point the unchanged catalog tag at it locally before staging. Fail if the
  source image lacks a matching digest or changes across retries. Do not combine
  a host move with an application/database upgrade.
- Verify free space/inodes, compatible architecture, filesystem metadata
  support, rsync versions, SSH identity and host-to-host reachability.
- Source and target rootless UID/GID, subordinate UID/GID allocations and
  effective container user-namespace mappings must match for numeric-ID copy.
  Reject dynamic or incompatible mappings rather than recursively chowning data.
- Discover every source writer and its activation source, including stopped
  containers, user/admin Quadlets, services, timers and external supervisors.
  Reject unexplained writers or shared writable paths before copying data.
- Target paths must be new or belong to this same reviewed migration. Never
  overwrite an unrelated existing app or a previously active target database.
- Take a recoverable backup and retain out-of-band access to both hosts.

## Coordination and inventory

Entry point: `automation/migrate_app_host.yml`, with explicit `migration_app`,
`source_host`, `target_host`, and `migration_phase` inputs. Both exact inventory
hosts must be reachable; do not use `--limit`. The playbook requires
`migration_operator_serialized=true`, explicit reviewed preflight/frozen/healthy
command argv lists and fixed source/target `ansible_host` IPv4 addresses. The
playbook provisions its own expiring restricted rsync receiver.

Implemented phases:

- `prepare`: prerequisites, step 1, step 2 and certificate readiness.
- `cutover`: steps 3–6, with explicit operator confirmation.
- `cleanup`: after inventory/DNS transition and drain verification; requires
  `--confirm-endpoint-retirement`. The cleanup play validates the source drain,
  then the wrapper atomically removes its `draining_apps` entry after success.
  Run a final full deployment on both hosts to reconcile target-only ownership.
- `all` (default): prepare and cut over in one invocation; requires both
  `migration_confirm_cutover=true` and `migration_approve_target_prune=true`.
- `handoff`: routing-only recovery after activation; never recopies data or
  restarts the source.

Use the wrapper for new migrations. It validates and writes both paired host-var
intent files via atomic replacement before starting a fresh Ansible process, ensuring inventory sees
the guard from its first task:

```bash
automation/migrate_app_host.py \
  --app example \
  --source source.example.net \
  --target target.example.net \
  --phase all \
  --confirm-cutover \
  --approve-target-prune
```

After DNS TTL expiry, source-log drain verification, and explicit approval, run
cleanup for each migrated application:

```bash
automation/migrate_app_host.py \
  --app example \
  --source source.example.net \
  --target target.example.net \
  --phase cleanup \
  --confirm-endpoint-retirement
```

The wrapper leaves intent in place after failure. After a successful `all`,
`cutover`, or `handoff`, it immediately removes both intent entries, moves sole
assignment to the target, and adds source draining with the target inventory
address. Each host-var replacement is atomic; interruption between the two files
fails closed under global topology validation. Direct `ansible-playbook`
invocation remains available for guarded retries, but does not edit inventory.

The reviewed callbacks are required because backup validation, external writers,
database shutdown/consistency and application-level health cannot be inferred
safely from the generic catalog. For non-wildcard names, the workflow injects an
HTTP-01-only relay into the still-live source route, forces target Caddy away from
TLS-ALPN issuance, and verifies every hostname directly against the target before
downtime. Wildcard names remain unsupported by this path.

Before starting, keep `assigned_apps` solely on the source and declare paired
intent in host vars:

```yaml
# source
outgoing_migrations:
  example:
    target_host: target.example.net

# target
incoming_migrations:
  example:
    source_host: source.example.net
```

The global compiler validates the pair and sole source ownership even under
`--limit`. Ordinary application deployment is suspended on both hosts while the
intent exists; only `migrate_app_host.yml` authorizes migration-owned
reconciliation. This prevents a full deployment from removing a target fence or
unmasking a stopped source writer.

After successful handoff, atomically remove the paired intent, move assignment to
the target, and add the source `draining_apps` declaration. The old-host proxy
and migration record may remain for weeks. Subsequent migrations compile active
records as a runtime ownership overlay, so prior resources remain represented.
Never assign an app to both hosts.

Persist a small root-owned migration record on both hosts containing:

- Migration ID, app, source/target identities, catalog/image digests.
- Validated data-path pairs and affected resource names.
- Source/target activation fences and saved source routing configuration.
- Final-copy completion and a conservative `target_activation_attempted` flag.
- Handoff verification and operator inventory/DNS acknowledgements.

This is a migration-specific safety record, not a routine deployment transaction
or persisted `force_refresh` acknowledgement. Every phase reobserves live storage,
containers, systemd and routing. A marker alone never proves that services stopped
or a copy/reload succeeded. Conflicting or partially written records fail closed.

## 1. Initial rsync

First establish a persistent target activation fence and verify no target
application container is running. This is a safety prerequisite to the first
copy, even though runtime staging happens in step 2.

Build a semantic data manifest from the app model, resolving each host's paths
independently. For example:

```yaml
migration_paths:
  - kind: volume
    name: database
    source: /srv/data/apps/example/volumes/database
    target: /mnt/data/apps/example/volumes/database
```

Copy writable volume contents and explicitly identified mutable static content.
Regenerate declarative mounts/configuration and secrets from the catalog/Vault.
Do not blindly copy the whole app directory: that could overwrite target-specific
configuration. Explicitly classify any application-managed configuration as data.
Do not copy Podman graphroot/runroot, systemd runtime files, host-state ledgers,
Caddy certificate storage or the host's entire `data_mount`.

For every manifest entry:

1. Canonicalize roots and parents on both hosts; reject symlink roots, overlaps
   with unrelated apps, unsafe nesting, and paths outside approved storage.
2. Verify actual mounts/datasets immediately before writing, not only at preflight.
   Reject undeclared nested mounts; do not silently skip or traverse them.
3. Copy directory contents using trailing-slash semantics and root-capable,
   narrowly scoped rsync over SSH with verified host keys.
4. Preserve numeric ownership, modes, timestamps, hard links and supported
   ACLs/xattrs (`-aHAX --numeric-ids`). Review SELinux labels separately for the
   target host; never blindly reuse source container-private labels. Do not
   apply recursive ownership/mode normalization to copied data.
5. Use no deletion in the initial pass. Record transfer results and failures.
   A live-copy vanished-file warning may be reviewed/retried; do not suppress
   arbitrary rsync errors.

A live database file copy is only a transfer optimization, not a usable backup
or a consistent database. The target must not open it before the final pass.
External databases need their own consistency/migration procedure.

Use short-lived, restricted transfer credentials: pinned SSH host identity,
no shell/forwarding, and an audited forced-command rsync receiver restricted to
the exact destination roots. Source-address restrictions may be added only when
the target has a stable, verified view of the source egress address; do not infer
that address from the source inventory endpoint. Validate that it supports the required
metadata operations without providing arbitrary root command execution. Remove
credentials in `always` blocks and provide expiry/manual revocation for host or
controller failure, when `always` cannot run. Never disable SSH host verification.

## 2. Stage runtime without starting or enabling it

Reuse ordinary deployment with a host-specific `app_desired_states` override
setting the app and reviewed dependencies to `inactive`.

- Render and validate the target application using the shared compiler/renderer.
- Pull pinned images and reconstruct Podman secrets.
- Prepare declarative configuration without overwriting copied writable data.
- Install normal administrator-owned Quadlets and prepare/start network resources.
- Persistently mask and stop application services before preparing mounted data;
  do not start application containers. No pre-created stopped container is required.
- Replace application Caddy sites/network references with comment-only managed
  placeholders and detach any old live Caddy app-network connections.
- Verify dependencies, timers, restart policies, lingering user managers and host
  reboots cannot activate the target application.

Generated Quadlet services do not support ordinary `systemctl disable` semantics.
Systemd masks enforce inactivity; there is no separate list of staged units or
withheld activation files. Avoid masking unrelated apps or shared Caddy/platform
units. A normal inactive deployment may finalize resource ownership; it does not
claim that an application has started or that migration data is authoritative.

The migration orchestrator must still refuse data-copy/staging replay after
`target_activation_attempted`, even though ordinary inactive deployment can stop
a previously running app. Explicitly inactive dependencies are needed: targeted
deployment otherwise reconciles the dependency closure's existing desired states.

Caddy itself remains running. Preparation installs the normal target route plus
an opaque, file-backed migration health endpoint while application services stay
masked. The health file is absent until the verified final transfer completes.
This migration-owned overlay must not be overwritten by ordinary app deployment.
Validate and gracefully reload Caddy, not restart it.

### TLS readiness before downtime

Step 6 uses verified HTTPS between the two Caddy instances. The target must have
a valid certificate before the source application is stopped.

For non-wildcard names, preparation installs an explicit HTTP-01 challenge relay
inside the active source route to target port 80, without changing other source
traffic. Target Caddy disables TLS-ALPN issuance while DNS still points to the
source. Wildcard names are rejected by this path. The subsequent direct TLS
probe is the acceptance test: if Caddy's local challenge handling prevents the
relay on the deployed version, preparation fails while the source remains live.

Verify the certificate by connecting directly to the target address with each
real hostname as HTTP Host and TLS SNI. Do not copy Caddy storage or disable TLS
verification. If this check fails, leave the source running and stop preparation.

## 3. Enter the bounded handoff and stop the current service

1. Confirm the prepared catalog/images/data manifest have not changed and perform
   another checksum copy while the source still serves traffic.
2. Atomically reload the already-validated source route as a fixed-address proxy
   to target Caddy. Its active health check uses the opaque migration endpoint;
   bounded retries hold eligible requests while the final-copy gate is absent.
3. Suspend external producers and all app-specific workers/timers as applicable.
4. Persistently mask and gracefully stop only the selected source services. A
   shutdown timeout or forced kill requires database recovery review; it is not
   proof of a clean final-copy point.
5. Verify no source or target process/container can write any migrated path.
   Recheck both hosts' storage and fences before the final copy.

There is no maintenance-response cutover. Shared Caddy remains running. Existing
long-lived WebSocket/stream sessions still require explicit draining, and client
timeouts shorter than the handoff cannot be made lossless.

Do not stop or restart shared Caddy. No source data is deleted.

## 4. Final rsync

With all writers stopped and both applications fenced:

- Run the same validated path manifest with `-aHAX --numeric-ids --checksum`.
  Checksums matter: an initial live copy may contain inconsistent blocks even
  when size and timestamps match at the final pass.
- Mirror deletions only inside the explicitly approved target data roots using
  `--delete-delay`. Preview deletions and require explicit authorization to prune
  this migration's target copy. Never delete source data, undeclared target data,
  or declarative target configuration. If pruning is necessary but not approved,
  stop; do not claim an exact copy while leaving stale database files.
- Require successful rsync exit status, then perform an itemized checksum dry run
  with the same mirror/metadata options and verify no unexplained differences.
- Confirm numeric ownership and representative metadata, and run application-
  specific offline consistency checks where supported.
- As the final transfer action, atomically create the opaque health file in the
  migration-owned target Caddy directory. Never create it before every checksum
  dry-run succeeds. Keep the source fenced.
- Record final-copy completion only after verification. Keep the source fenced.

If interrupted here, keep both applications stopped and repeat the final pass
only after verifying that target activation has never been attempted.

## 5. Start services on the new host

1. Reverify source fencing, final-copy validity and target storage.
2. Persist `target_activation_attempted=true` on both hosts **before** removing
   any target fence or installing activation sources. If either write fails,
   do not start the target; ambiguous state requires manual review.
3. Do not rerun the shared deployment pipeline. All Quadlets, secrets, Caddy
   routes, network definitions, images and firewall rules were reconciled during
   preparation.
4. Start the prestaged networks, connect target Caddy to them, then unmask and
   start the target services. Boot activation comes from Quadlet install metadata,
   not `systemctl enable` on generated services. Static apps naturally have no
   application services in this same path.
6. Verify containers are running, configured healthchecks pass, and HTTP/TLS
   requests through target Caddy reach the correct application. Include a
   reviewed application-level data check; “systemd active” is not enough.
7. Confirm background workers and integrations are operating only on the target.

**Once target startup is attempted, treat its data as potentially newer.** Never
rerun source-to-target rsync or restart the source automatically after this point,
including if startup/health verification fails. Even an unhealthy worker may
already have committed writes.

## 6. Verify the already-active old-host proxy

The source proxy was activated before source shutdown and held eligible requests
behind the final-copy health gate. After target verification, verify that same
whole-site proxy to the target's explicit address. Example only:

```caddyfile
app.example.com {
    reverse_proxy TARGET_IPV4:443 {
        header_up Host app.example.com
        transport http {
            tls
            tls_server_name app.example.com
        }
    }
}
```

- Generate one reviewed block per hostname. The upstream is a fixed target
  address, not the public app hostname, which may still resolve to the source
  and cause a proxy loop.
- Preserve TLS verification and use the real hostname for SNI/Host. Do not fall
  back to a local source upstream or load-balance between old and new writers.
- Validate source Caddy configuration before promotion and gracefully reload.
  Preserve a backup for routing recovery, but do not restore a stopped local
  application as an automatic fallback.
- Verify old-address and new-address requests, TLS, redirects, WebSockets and
  representative application operations. Old IPv4 and IPv6 endpoints must both
  forward correctly; the bridge may use a fixed IPv4 target address.
- If original client IPs matter, explicitly configure and test target Caddy's
  trusted-proxy handling for the source's exact egress `/32` or `/128`. Source
  Caddy must sanitize untrusted forwarding headers. Verify the application sees
  the intended address; do not trust arbitrary public or shared-NAT clients.
- The handoff site must survive reboot and ordinary Caddy reload. Keep source
  application activation fenced and migration guards in place until final
  inventory and routing ownership are reconciled.

The target is now the only writer, even while public DNS still points to the old
host. Update inventory assignment and all relevant A/AAAA records manually.
Account for source certificate renewal if the forwarding period will be long.

## Recovery rules

| Interruption point | Safe action |
| --- | --- |
| Initial copy or staging | Source stays active; reverify target fencing and retry |
| Source stopped, target never activated | Keep the source proxy/fences in place and retry final copy; operator may abandon after proving target never wrote |
| Target activation attempted | Keep source fenced; investigate/recover target; never overwrite target from stale source |
| Target healthy, source proxy failed | Keep target running and source fenced; repair only routing, not data transfer |
| DNS/cleanup incomplete | Retain handoff and source fence; resume cleanup after verification |

A rollback after target activation is a new reverse migration: stop/fence target,
identify its authoritative writes, synchronize back under review, and only then
reactivate source. No automatic rollback or automatic deletion of persistent data.

## DNS drain and cleanup

1. Require app assignment to the target only and verify target readiness.
2. Verify authoritative/public A and AAAA results through multiple resolvers.
3. Wait at least the old TTL plus a safety margin and review source access logs;
   TTL expiry alone does not prove every client stopped using the old address.
4. Explicitly approve retirement of the old endpoint. Retain the proxy longer
   where pinned clients or long-lived connections require it.
5. Remove source app runtime containers/Quadlets and Caddy app network drop-ins;
   reload user systemd before removing runtime networks without force.
6. Remove source handoff sites only after approved drain, and remove temporary
   ACME/client-IP trust configuration when no longer needed. Validate/reload Caddy.
7. Reconcile each host's resource ownership ledger with the final inventory,
   retaining unrelated apps and recording target deployment success only after
   activation verification. Never copy host-state wholesale between hosts.
8. Remove migration credentials and guards after reconciliation. Keep source
   masks until every source activation path is proved retired; retaining masks
   as a documented safeguard is acceptable.
9. Keep source writable data and backups. Their deletion is outside this workflow.

## Remaining implementation work

1. Replace operator serialization with host locks and enforced checks in ordinary
   deployment/platform entry points before permitting concurrent operators.
2. Extend the reviewed model beyond dependency-free apps and named volumes;
   shared dependencies and mutable static paths remain rejected.
3. Add a controller-level finalizer when the harness can guarantee execution
   after failure. Transfer credentials are removed after successful runs, are
   independently expiry-limited after interruption, and can be revoked explicitly
   with `automation/revoke_migration_transfer.yml`. HTTP-01 issuance is automated
   for non-wildcard names and checked by a direct TLS probe.
4. Add source-only trusted-proxy rendering where preserving original client IPs
   is required; the current handoff intentionally does not declare forwarded
   client addresses trusted.
5. Add focused regression tests and disposable-host integration tests, including
   reboot and injected failure checks, before relying on the workflow in production.

## Acceptance checks

- Different source/target data mounts; matching numeric user mappings; ACL/xattr
  preservation; populated volumes receive no blanket chown/chmod.
- Missing/wrong mounts, symlink roots, unexpected target data, unknown writers,
  incompatible namespaces and unapproved direct TCP/UDP ports fail before cutover.
- No target start during initial copy/staging, including dependency activation,
  interrupted attempts and reboot; no source writer returns after fencing/reboot.
- Final checksum pass detects same-size/same-timestamp modifications and removes
  approved stale target files without touching unrelated data.
- Failure injected before/after every fence, transfer, activation and Caddy reload;
  reruns after target activation cannot replay source-to-target copy.
- Certificates validate before DNS change; old/new IPv4/IPv6 routes, WebSockets
  and forwarded client identities work without restarting shared Caddy.
- Source proxy and target network membership survive reboot.
- Unrelated apps stay running; nftables remains the only firewall authority;
  migration guards prevent ordinary deployment from undoing the cutover.
- Cleanup retains source data and does not force-remove an in-use network.
