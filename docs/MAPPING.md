# Assignment and Reconciliation

App definitions live under `apps/<app>/app.yml`. Host assignment lives in
`host_vars/<host>.yml` as `assigned_apps`; non-authoritative old-host forwarding
lives in `draining_apps`. Every compile receives all `app_hosts` declarations and
rejects duplicate ownership before filtering output to hosts selected by
`--limit`.

## Full Deployment

Running `deploy.yml` without `app` treats the host assignment as authoritative.
It reconciles every assigned app, Caddy site, app-owned Caddy network drop-in,
Quadlet, container, network, and managed secret.

The role stores its non-secret ownership manifest at
`/var/lib/ansible-app-host/host-state.json`. Full deployment compares tracked
resources with the compiled desired state and removes stale managed runtime
resources. It does not delete app data, static content, Caddy certificate storage,
or unmanaged Podman resources.

Before creating resources, deployment adds their names to `host-state.json`,
leaving successful assignment/port metadata unchanged. An interrupted run thus
leaves ownership discoverable without a pending transaction. After successful
full deployment, the ledger is pruned to desired resources and successful host
metadata is updated. Targeted runs retain unrelated resource ownership.

The desired apply plan is ephemeral; only resource names and successful host
metadata are persisted. `force_refresh` is entirely the caller's choice. Full
deployment reconciles published-port firewall rules from the entire host plan,
including removal of obsolete allowances.

## Targeted Deployment

Running `deploy.yml -e app=<app>` requires the app to be assigned to the limited
host. It reconciles the selected app, its transitive `requires_apps` closure,
and the minimum Caddy state those apps own. It does not perform unrelated
cleanup or mutate another app's units, sites, drop-ins, secrets, or networks.
It reads the host manifest and compiles the assigned catalog to validate host-wide
constraints. A missing or incompatible manifest, a changed assigned-app set,
changed published ports, or removal/renaming of tracked runtime resources requires
a full run. Targeted runs retain newly introduced resource ownership in the host
manifest so a subsequent full run can garbage-collect it.

An app may use `requires_apps` to declare a co-located dependency. Full
deployment verifies assignment, rejects dependency cycles, and starts required
app services before dependent app services. Targeted deployment reconciles the
same dependency closure. Quadlet rendering remains order-independent.

## Activation State

`desired_state` defaults to `running` in the catalog. Host inventory may override
assigned apps through `app_desired_states: {app-name: inactive}`. Unknown states
or override keys, and running apps requiring inactive dependencies, are rejected.

Inactive apps remain assigned and retain all resource ownership and port
reservations. Deployment installs native persistent systemd masks before starting
the user manager, stops app services before data preparation, and prepares their
images/storage/secrets/Quadlets/networks without starting application containers.
Caddy sites/drop-ins become comment-only placeholders and live app-network
attachments are disconnected. Running state unmasks and starts app services.

A draining app is not assigned to the source host. `draining_apps` must name a
different target host which is the app's sole global owner, plus its fixed target
address. Deployment keeps source services persistently masked, removes local
published-port allowances and Caddy network attachment, and renders only the
verified HTTPS handoff proxy. This permits ordinary reconciliation while old DNS
and pinned clients continue using the source endpoint.

No separate activation ledger is used; `force_refresh` cannot override inactivity.

Targeted state transitions reconcile only the selected dependency closure, with
full-host dependency validation. Existing interruption/refresh rules still apply
to already-promoted configuration and Caddy routing. Systemd masks, unlike ordinary
disable operations, block dependency and boot activation.

## Domains and Networks

Every domain must be globally unique. Multiple domains may belong to one app.

Network declarations use logical aliases. Physical names double logical
hyphens and use `<encoded-app>-net-<encoded-alias>`, preventing tuple ambiguity
and cross-app collisions. Only
aliases marked `caddy: true` on running apps are attached to Caddy. An `internal: true` network
cannot be Caddy-facing.

## Migration

`migrate_volume_layout.yml` performs only the one-time, in-place transition of
legacy app volume directories into each app's `volumes/` namespace. It does not
transfer data between hosts. Routine deployment assumes this migration is
complete and checks only the namespaced layout. Assignment changes alone do not
move data to another host.
