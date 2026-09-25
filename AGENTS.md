# Contributor Architecture Rules

This repository provides reusable, rootless application hosting automation for
Debian systems.

## Invariants

- Use Ansible for all host and runtime changes.
- Use rootless Podman and systemd Quadlets for every long-running container.
- Run one rootless Caddy ingress container per application host.
- Keep all persistent data beneath `data_mount`.
- Require an explicit `storage_mode`: `mount` requires a real mountpoint, `zfs`
  requires the exact managed dataset hierarchy, and `plain` is for labs/tests.
- Give every app only compiler-named app-owned networks using escaped logical
  components and explicit resource-kind tags.
- Allow Caddy to join only aliases explicitly marked `caddy: true`.
- Never join Caddy to an internal network.
- Never restart Caddy during routine deployment; validate and reload it.
- Never publish app ports unless the protocol cannot use Caddy and the app
  declares the exception explicitly.
- Treat each published protocol/host-port pair as a unique host resource and
  derive its nftables allowance from the app declaration.
- Own the complete host nftables ruleset exclusively. Replace it atomically,
  detect whole-ruleset drift, and reject independent iptables-legacy policy.
  Other host firewall writers and foreign NAT/VPN tables are unsupported.
- Never delete persistent app or static data during normal reconciliation.
- Treat writable app volumes as PVC-like storage: create a missing declared
  root, but never reconcile ownership, mode, or contents of an existing volume.
- Treat managed read-only mounts as ConfigMap-like content owned and enforced by
  this automation.
- Never use Docker, Compose, Kubernetes, privileged containers, or rootful
  Podman.

## Supported Interfaces

`bootstrap.yml` prepares a host and its Caddy/firewall platform baseline.
`update_platform.yml` explicitly reapplies that shared baseline implementation.
`deploy.yml` supports authoritative full app reconciliation and targeted
`-e app=<app>` reconciliation. It checks platform prerequisites rather than
reconciling Caddy's base container or firewall service/package wiring. Legacy
compatibility operations belong exclusively in migration-specific files.

Full deployment owns host-wide cleanup of resources recorded in managed app
state. Targeted deployment owns only the selected app and must leave unrelated
resources untouched.

`force_refresh` defaults to false: unchanged configuration is trusted. When true,
restart all selected app services, gracefully reload Caddy and user systemd, and
reapply firewall policy on full runs. The resource ownership ledger must not
implicitly force refresh. Do not reintroduce pending deployment transactions.
Keep GC ownership and volume-initialization safeguards independent
of this caller preference.

Physical app resource names encode `-` as `--` inside each logical component
and join components with single `-` separators and a resource-kind tag. A
targeted deployment requires the assigned-app set and naming version committed
by the last successful full deployment; assignment changes require a full run.

App definitions live at `apps/<app>/app.yml`. The directory name is the app
name. Use canonical `domains`, logical `networks`, and logical volume names.
Do not add configurable app roots, volume host paths, mount host paths, or
physical network names.

The controller-side compiler in `tools/app_model` owns app schema validation,
normalization, dependency resolution, derived resource names, and declaration
collision checks. Ansible invokes it per host and remains the sole remote-state
observer and actuator. Compiled plans must be deterministic, versioned, and
free of secret values.

Static content lives under `<static_base_path>/<app>` and is populated outside
this automation. Creating required empty directories is allowed; copying or
deleting static content is not. Declared static roots must remain writable by
root and `podman` and readable by Caddy; descendant permissions are the
publisher's responsibility.

Secrets come from encrypted variables and become app-namespaced Podman secrets.
Do not place secret values in environment lines, generated Quadlets, task names,
or logs.

## Quadlet Defaults

Containers drop all capabilities, enable no-new-privileges, use a read-only root
filesystem, receive bounded tmpfs mounts, and have memory and PID limits. Add a
capability only when a service requires it. Hard CPU quota is opt-in.

Do not use registry auto-update. Image changes must be explicit deployment
changes.

App networks are `.network` Quadlets. Caddy network persistence uses app-owned
drop-ins under `caddy.container.d/`; live attachment uses checked Podman network
commands without Caddy restart.

## Ansible Style

- Use fully qualified collection names.
- Prefer idempotent modules and explicit `changed_when` for commands.
- Run rootless Podman and user-systemd operations as `podman_user` with the
  shared runtime environment.
- Use the account's passwd home directory instead of assuming `/home/<user>`.
- Validate before promotion for SSH, nftables, Caddy, and Quadlets.
- Keep reusable code, comments, examples, tests, and documentation free of
  deployment-specific hostnames, inventory names, and workload names.

## Verification

Run playbook syntax checks, `ansible-lint`, and compile the consuming catalog
with `tools/compile_app_model.py --apps-root <apps-root> --check`.
The old test suites were removed during the rewrite; replacement coverage is
tracked in `TODO.md`. Host-affecting changes require idempotency, reboot
persistence, network isolation, and graceful Caddy reload checks on a disposable
supported host. Do not claim runtime verification based on syntax checks alone.
