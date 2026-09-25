# Caddy Host Role

This role establishes one rootless Caddy Quadlet per application host.

It creates the persistent configuration, certificate, runtime configuration,
log, and static-content directories beneath `data_mount`. The Caddy
configuration mount is read-only inside the container; only certificate and
runtime storage remain writable.

The base Caddy Quadlet does not enumerate app networks. The apps role owns
per-app source drop-ins under `caddy.container.d/` and live network attachment.
A companion `caddy-pasta.service` owns TCP 80/443 and UDP 443 directly in
Caddy's network namespace, preserving client addresses without `rootlessport`.

Global and app configuration is staged, validated with the configured Caddy
image, and promoted before a graceful reload through the Unix admin socket.
Routine deployment never restarts Caddy. Changing the platform Quadlet may
perform a controlled restart, including the one-time migration away from
`PublishPort=`.
