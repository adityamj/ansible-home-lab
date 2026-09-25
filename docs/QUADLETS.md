# Quadlet Lifecycle

All long-running containers and app networks are rootless systemd Quadlets in
Podman's root-owned per-user source directory:

```text
/etc/containers/systemd/users/<uid>/
```

The source directory and its managed files remain administrator-owned. The
rootless user's systemd generator can read these sources but cannot persistently
alter its service definitions through a user-writable home-directory Quadlet
path.

Logical-name hyphens are doubled in physical names. An app container uses
`<encoded-app>-ctr-<encoded-container>.container`. An app network uses
`<encoded-app>-net-<encoded-alias>.network` and declares the same stem as its
physical Podman name.

Caddy has one base `caddy.container` without app networks. Each app that exposes
a Caddy-facing network owns one source drop-in:

```text
caddy.container.d/50-network-<app>.conf
```

The drop-in contains the compiler-derived encoded network Quadlet name. This persists membership
for the next natural Caddy creation. Deployment also connects a missing network
to the running Caddy container so no restart is required.

Generated app units use systemd `Requires=` and `After=` for declared
same-app `depends_on` relationships. A changed unit, managed config, or secret
causes one restart of the affected app service. Unchanged units are only
ensured started and enabled.

Cross-app `requires_apps` relationships are soft. Dependent app units use
`Wants=` and `After=` for required app services, and deployment starts apps in
topological order. A required app failure does not hard-stop a dependent unit.
Dependency order does not constrain Quadlet rendering.

Containers with declared `published_ports` receive a platform-owned ingress
anchor Quadlet and a root-owned regular user unit in `/etc/systemd/user`, named
`<container-service-stem>-pasta.service`. The anchor runs as root inside the
rootless user namespace and shares the application container's network
namespace, giving pasta an accessible PID without changing the application's
UID. The application, anchor, and pasta units use `BindsTo=` and `PartOf=` so a
recreated application namespace is never reused. No TCP proxy, `PublishPort=`,
or `rootlessport` is in the ingress path, so the original client address is
preserved.

Registry auto-update is intentionally disabled. Unit and image changes happen
through reviewed Ansible deployment.

Before promotion or service actions, deployment copies the active source tree
to a root-owned candidate directory beneath `/var/lib/ansible-app-host`, applies
the desired changes there, and runs the Podman system generator in user dry-run
mode. Invalid Quadlet source files stop the deployment without replacing the
active source tree.

Full deployment promotes the authoritative complete tree. Targeted deployment
uses the complete candidate only for validation, then copies or removes only
the selected dependency closure's explicit Quadlet and Caddy drop-in paths.
