#!/usr/bin/env python3
"""Render one host's desired app artifacts.

The compiler owns naming and rendering.  Ansible should only copy these files,
validate them with host tools, promote them, and operate systemd/podman.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .app_model.compiler import ModelError, compile_plan
else:
    from app_model.compiler import ModelError, compile_plan


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ModelError("input: must be a JSON object")
    return value


def _duration(value: Any) -> str:
    return str(value)


def _systemd_env_value(key: str, value: Any) -> str:
    text = f"{key}={value}"
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _exec_value(command: Any) -> str:
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(str(part)) for part in command)


def _render_network(network: dict[str, Any]) -> str:
    lines = ["[Network]", f"NetworkName={network['physical_name']}"]
    if network.get("internal"):
        lines.append("Internal=true")
    return "\n".join(lines) + "\n"


def _render_container(
    app: dict[str, Any],
    container: dict[str, Any],
    *,
    app_required_services: list[str],
    data_mount: str,
    app_memory_default: str,
    app_pids_limit_default: int,
    app_restart_policy: str,
    app_restart_sec: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    app_base = f"{data_mount}/apps/{app['name']}"
    volumes = []
    for volume in container.get("volumes", []):
        volumes.append(volume | {"host_path": f"{app_base}/volumes/{volume['name']}"})
    mounts = []
    for mount in container.get("mounts", []):
        mounts.append(mount | {"host_path": f"{app_base}/mounts/{mount['relative_path']}"})
    secrets = []
    for secret in container.get("secrets", []):
        secret_type = secret.get("type", "mount")
        target = secret.get("target", secret["name"] if secret_type == "env" else f"/run/secrets/{secret['name']}")
        secrets.append(secret | {"type": secret_type, "target": target})

    network_quadlets = [network["quadlet_name"] for network in app.get("networks", []) if network["name"] in container["networks"]]
    published_ports = []
    for port in container.get("published_ports", []):
        published_ports.append(
            port
            | {
                "host_port": int(port["host_port"]),
                "container_port": int(port["container_port"]),
                "protocol": port.get("protocol", "tcp"),
            }
        )

    dependency_services = container.get("dependency_services", [])
    lines = [
        "[Unit]",
        f"Description=Managed rootless container {app['name']}/{container['name']}",
    ]
    if published_ports:
        lines += [f"Wants={container['ingress_service_name']}", f"Before={container['ingress_service_name']}"]
    if app_required_services:
        joined = " ".join(app_required_services)
        lines += [f"Wants={joined}", f"After={joined}"]
    if dependency_services:
        joined = " ".join(dependency_services)
        lines += [f"Requires={joined}", f"After={joined}"]

    lines += ["", "[Container]", f"ContainerName={container['physical_name']}", f"Image={container['image']}"]
    if "user" in container:
        lines.append(f"User={container['user']}")
    if "command" in container:
        lines.append(f"Exec={_exec_value(container['command'])}")
    if "runtime_healthcheck" in container:
        check = container["runtime_healthcheck"]
        lines.append(f"HealthCmd={check['command'] if isinstance(check['command'], str) else json.dumps(check['command'])}")
        for source, key in (("interval", "HealthInterval"), ("timeout", "HealthTimeout"), ("retries", "HealthRetries"), ("start_period", "HealthStartPeriod"), ("on_failure", "HealthOnFailure")):
            if source in check:
                lines.append(f"{key}={check[source]}")
    for network_quadlet in network_quadlets:
        lines.append(f"Network={network_quadlet}")
    for volume in volumes:
        lines.append(f"Volume={volume['host_path']}:{volume['container_path']}:rw")
    for mount in mounts:
        lines.append(f"Volume={mount['host_path']}:{mount['container_path']}:ro,Z")
    for secret in secrets:
        lines.append(f"Secret={secret['physical_name']},type={secret['type']},target={secret['target']}")
    for key in sorted(container.get("env", {})):
        lines.append(f"Environment={_systemd_env_value(key, container['env'][key])}")
    lines += ["DropCapability=ALL"]
    for capability in container.get("capabilities", []):
        lines.append(f"AddCapability={capability}")
    lines += [
        "NoNewPrivileges=true",
        "ReadOnly=true",
        f"Tmpfs=/tmp:size={container.get('tmpfs_tmp_size', '64m')}",
        f"Tmpfs=/run:size={container.get('tmpfs_run_size', '32m')}",
        "",
        "[Service]",
        "ExecCondition=/usr/local/libexec/ansible-storage-ready",
        f"Restart={container.get('restart_policy', app_restart_policy)}",
        f"RestartSec={container.get('restart_sec', app_restart_sec)}",
        f"MemoryMax={container.get('memory_max', app_memory_default)}",
        f"TasksMax={container.get('pids_limit', app_pids_limit_default)}",
        f"CPUWeight={container.get('cpu_weight', container.get('cpu_shares', 1024))}",
    ]
    if container.get("cpu_quota"):
        lines.append(f"CPUQuota={container['cpu_quota']}")
    lines += ["", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n", volumes, mounts, secrets


def _render_ingress_anchor(container: dict[str, Any]) -> str:
    lines = [
        "[Unit]",
        f"Description=Namespace anchor for source-preserving ingress to {container['physical_name']}",
        f"Requires={container['service_name']}",
        f"After={container['service_name']}",
        f"BindsTo={container['service_name']}",
        f"PartOf={container['service_name']}",
        f"Wants={container['pasta_service_name']}",
        f"Before={container['pasta_service_name']}",
        "",
        "[Container]",
        f"ContainerName={container['ingress_physical_name']}",
        "Image=docker.io/library/alpine:3.22",
        f"Network=container:{container['physical_name']}",
        "User=0:0",
        "Exec=sleep infinity",
        "DropCapability=ALL",
        "NoNewPrivileges=true",
        "ReadOnly=true",
        "",
        "[Service]",
        "Restart=on-failure",
        "RestartSec=2s",
        "MemoryMax=32M",
        "TasksMax=32",
    ]
    return "\n".join(lines) + "\n"


def _render_pasta_service(container: dict[str, Any]) -> str:
    mappings: dict[str, list[str]] = {"tcp": [], "udp": []}
    for port in container.get("published_ports", []):
        protocol = port.get("protocol", "tcp")
        mappings[protocol].append(f"{int(port['host_port'])}:{int(port['container_port'])}")
    command = [
        "/usr/local/libexec/pasta-for-container",
        container["ingress_physical_name"],
        "--foreground",
        "--config-net",
        "--ns-ifname",
        "pasta0",
    ]
    if mappings["tcp"]:
        command += ["--tcp-ports", ",".join(sorted(mappings["tcp"]))]
    if mappings["udp"]:
        command += ["--udp-ports", ",".join(sorted(mappings["udp"]))]
    lines = [
        "[Unit]",
        f"Description=Source-preserving pasta ingress for {container['physical_name']}",
        f"Requires={container['ingress_service_name']}",
        f"After={container['ingress_service_name']}",
        f"BindsTo={container['ingress_service_name']}",
        f"PartOf={container['ingress_service_name']}",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={_exec_value(command)}",
        "Restart=on-failure",
        "RestartSec=2s",
        "MemoryMax=64M",
        "TasksMax=64",
    ]
    return "\n".join(lines) + "\n"


def _proxy_block(container: dict[str, Any], target_port: int) -> str:
    if "healthcheck" not in container:
        return f"        reverse_proxy {container['physical_name']}:{target_port}"
    check = container["healthcheck"]
    lines = [f"        reverse_proxy {container['physical_name']}:{target_port} {{"]
    mapping = [
        ("retry_duration", "lb_try_duration"),
        ("retry_interval", "lb_try_interval"),
        ("path", "health_uri"),
        ("interval", "health_interval"),
        ("timeout", "health_timeout"),
        ("passes", "health_passes"),
        ("fails", "health_fails"),
        ("status", "health_status"),
        ("fail_duration", "fail_duration"),
        ("max_fails", "max_fails"),
    ]
    for source, directive in mapping:
        if source in check:
            lines.append(f"            {directive} {check[source]}")
    if "unhealthy_status" in check:
        value = check["unhealthy_status"]
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        lines.append(f"            unhealthy_status {value}")
    lines.append("        }")
    return "\n".join(lines)


def _render_caddy_site(app: dict[str, Any], *, caddy_static_mount_path: str, caddy_tls_default: Any) -> str:
    tls_mode = app.get("ingress", {}).get("tls", caddy_tls_default)
    labels = [f"http://{domain}" if tls_mode is False else domain for domain in app["domains"]]
    lines = [f"{', '.join(labels)} {{"]
    if app.get("type", "container") == "static":
        lines.append(f"    root * {caddy_static_mount_path}/{app['name']}/public")
    ingress = app.get("ingress", {})
    directives = ingress.get("caddy_directives", ingress.get("extra_caddy"))
    if directives:
        lines.append(directives.rstrip("\n"))
    if tls_mode == "internal":
        lines.append("    tls internal")

    for static in app.get("static_paths", []):
        if "root" in static:
            if static.get("root_base", "app_public") == "app_static":
                root = f"{caddy_static_mount_path}/{app['name']}/{static['root']}"
            else:
                root = f"{caddy_static_mount_path}/{app['name']}/public/{static['root']}"
        else:
            root = f"{caddy_static_mount_path}/{app['name']}/public"
        handle = "handle_path" if static.get("strip_prefix", True) else "handle"
        lines += [f"    {handle} {static['path']} {{", f"        root * {root}", "        file_server", "    }"]

    if app.get("type", "container") == "static":
        if not ingress.get("disable_default_file_server", False):
            lines.append("    file_server")
    else:
        routed = [container for container in app.get("containers", []) if container.get("proxy_paths")]
        entry = next((container for container in app.get("containers", []) if container.get("entrypoint")), None)
        for container in routed:
            target_port = container.get("service_port", container.get("ports", [8080])[0] if container.get("ports") else 8080)
            for route in container["proxy_paths"]:
                handle = "handle_path" if route.get("strip_prefix", True) else "handle"
                lines += [f"    {handle} {route['path']} {{", _proxy_block(container, target_port), "    }"]
        if entry is not None:
            target_port = entry.get("service_port", entry.get("ports", [8080])[0] if entry.get("ports") else 8080)
            lines += ["    handle {", _proxy_block(entry, target_port), "    }"]
        else:
            lines.append('    respond "No entrypoint container configured for this domain." 502')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_draining_caddy_site(app: dict[str, Any]) -> str:
    address = app["drain_target_address"]
    blocks = []
    for domain in app["domains"]:
        blocks.append(
            f"{domain} {{\n"
            f"    reverse_proxy {address}:443 {{\n"
            f"        header_up Host {domain}\n"
            "        transport http {\n"
            "            tls\n"
            f"            tls_server_name {domain}\n"
            + ("            versions 1.1\n" if app.get("ingress", {}).get("handoff_upstream_http1", False) else "")
            + "        }\n"
            "    }\n"
            "}\n"
        )
    return "".join(blocks)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render(plan: dict[str, Any], root: Path, request: dict[str, Any]) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    (root / "quadlets" / "caddy.container.d").mkdir(parents=True)
    (root / "units").mkdir(parents=True)
    (root / "caddy" / "sites-enabled").mkdir(parents=True)

    data_mount = request["data_mount"]
    caddy_static_mount_path = request.get("caddy_static_mount_path", "/srv/static")
    defaults = request.get("defaults", {})
    app_memory_default = defaults.get("memory_max", "512M")
    app_pids_limit_default = defaults.get("pids_limit", 512)
    app_restart_policy = defaults.get("restart_policy", "on-failure")
    app_restart_sec = defaults.get("restart_sec", "5s")
    caddy_tls_override = request.get("caddy_tls_override", "__UNSET__")
    caddy_tls_default = request.get("caddy_tls_default", True) if caddy_tls_override == "__UNSET__" else caddy_tls_override

    volume_dirs: list[dict[str, Any]] = []
    static_dirs: set[str] = set()
    mounts: list[dict[str, Any]] = []
    secrets: list[dict[str, Any]] = []

    host_apps = plan["host_apps"]
    host_by_name = {app["name"]: app for app in host_apps}
    ordered_apps = [host_by_name[name] for name in plan["deploy_app_service_order"]]
    running_apps = [app for app in ordered_apps if app["desired_state"] == "running"]
    inactive_apps = [app for app in reversed(ordered_apps) if app["desired_state"] in ("inactive", "draining")]
    for app in plan["deploy_apps"]:
        static_app_root = f"{data_mount}/static/{app['name']}"
        if app["type"] == "static" or app.get("static_paths"):
            static_dirs.add(static_app_root)
        if app["type"] == "static":
            static_dirs.add(f"{static_app_root}/public")
        for static in app.get("static_paths", []):
            static_root = static.get("root")
            if static_root:
                base = static_app_root if static.get("root_base", "app_public") == "app_static" else f"{static_app_root}/public"
                static_dirs.add(f"{base}/{static_root}")
            else:
                static_dirs.add(f"{static_app_root}/public")

        required_services = []
        for dependency_name in app.get("requires_apps", []):
            for dependency_container in host_by_name[dependency_name]["containers"]:
                required_services.append(dependency_container["service_name"])
        for network in app.get("networks", []):
            _write(root / "quadlets" / network["quadlet_name"], _render_network(network))
        caddy_networks = [network for network in app.get("networks", []) if network.get("caddy")]
        if caddy_networks:
            _write(
                root / "quadlets" / "caddy.container.d" / f"50-network-{app['name']}.conf",
                ("[Container]\n" + "".join(f"Network={network['quadlet_name']}\n" for network in caddy_networks))
                if app["desired_state"] == "running" else "# Inactive application: no Caddy network attachments.\n",
            )
        for container in app.get("containers", []):
            content, container_volumes, container_mounts, container_secrets = _render_container(
                app,
                container,
                app_required_services=sorted(set(required_services)),
                data_mount=data_mount,
                app_memory_default=app_memory_default,
                app_pids_limit_default=app_pids_limit_default,
                app_restart_policy=app_restart_policy,
                app_restart_sec=app_restart_sec,
            )
            _write(root / "quadlets" / container["quadlet_name"], content)
            if container.get("published_ports"):
                _write(root / "quadlets" / container["ingress_quadlet_name"], _render_ingress_anchor(container))
                _write(root / "units" / container["pasta_unit_name"], _render_pasta_service(container))
            for volume in container_volumes:
                volume_dirs.append({"path": volume["host_path"], "mode": volume.get("directory_mode", "0750"), "initial_uid": volume.get("initial_uid", 0), "initial_gid": volume.get("initial_gid", 0)})
            for mount in container_mounts:
                mounts.append(
                    {
                        "src": mount["src_absolute"],
                        "dest": mount["host_path"],
                        "source_kind": mount["source_kind"],
                        "directories": ([""] + [path.relative_to(mount["src_absolute"]).as_posix()
                                         for path in sorted(Path(mount["src_absolute"]).rglob("*")) if path.is_dir()])
                                       if mount["source_kind"] == "directory" else [],
                        "files": [path.relative_to(mount["src_absolute"]).as_posix()
                                  for path in sorted(Path(mount["src_absolute"]).rglob("*")) if path.is_file()]
                                 if mount["source_kind"] == "directory" else [],
                        "directory_mode": mount["directory_mode"],
                        "file_mode": mount["file_mode"],
                    }
                )
            for secret in container_secrets:
                secrets.append({"app": app["name"], "logical_name": secret["name"], "physical_name": secret["physical_name"], "type": secret["type"], "target": secret["target"]})
        if app.get("domains"):
            if app["desired_state"] == "running":
                site = _render_caddy_site(app, caddy_static_mount_path=caddy_static_mount_path, caddy_tls_default=caddy_tls_default)
            elif app["desired_state"] == "draining":
                site = _render_draining_caddy_site(app)
            else:
                site = "# Inactive application: no public routes.\n"
            _write(root / "caddy" / "sites-enabled" / f"{app['name']}.caddy", site)

    apply_plan = {
        "version": plan["version"],
        "naming_scheme": plan["naming_scheme"],
        "scope": plan["scope"],
        "assigned_apps": plan["host_assigned_app_names"],
        "assigned_apps_digest": plan["host_assigned_apps_digest"],
        "inventory_topology_digest": plan["inventory_topology_digest"],
        "deploy_app_order": plan["deploy_app_service_order"],
        "services": [service for app in plan["deploy_apps"] for container in app.get("containers", [])
                     for service in ([container["service_name"]] + ([container["ingress_service_name"], container["pasta_service_name"]] if container.get("published_ports") else []))],
        "running_services": [service for app in running_apps for container in app["containers"]
                             for service in ([container["service_name"]] + ([container["ingress_service_name"], container["pasta_service_name"]] if container.get("published_ports") else []))],
        "inactive_services": [service for app in inactive_apps for container in reversed(app["containers"])
                              for service in (([container["pasta_service_name"], container["ingress_service_name"]] if container.get("published_ports") else []) + [container["service_name"]])],
        "running_containers": [name for app in running_apps for container in app["containers"]
                               for name in ([container["physical_name"]] + ([container["ingress_physical_name"]] if container.get("published_ports") else []))],
        "inactive_containers": [name for app in inactive_apps for container in app["containers"]
                                for name in ([container["physical_name"]] + ([container["ingress_physical_name"]] if container.get("published_ports") else []))],
        "deploy_images": sorted(({container["image"] for app in plan["deploy_apps"] for container in app["containers"]}
                                | ({"docker.io/library/alpine:3.22"} if any(container.get("published_ports") for app in plan["deploy_apps"] for container in app["containers"]) else set()))),
        "inactive_images": sorted(({container["image"] for app in inactive_apps for container in app["containers"]}
                                  | ({"docker.io/library/alpine:3.22"} if any(container.get("published_ports") for app in inactive_apps for container in app["containers"]) else set()))),
        "inactive_caddy_networks": sorted({network["physical_name"] for app in inactive_apps for network in app["networks"] if network["caddy"]}),
        "network_services": [network["service_name"] for app in plan["deploy_apps"] for network in app.get("networks", [])],
        "containers": [name for app in plan["deploy_apps"] for container in app.get("containers", [])
                       for name in ([container["physical_name"]] + ([container["ingress_physical_name"]] if container.get("published_ports") else []))],
        "networks": [network["physical_name"] for app in plan["deploy_apps"] for network in app.get("networks", [])],
        "caddy_networks": plan["deploy_caddy_networks"] if plan["scope"] == "targeted" else plan["host_caddy_networks"],
        "host_caddy_networks": plan["host_caddy_networks"],
        "quadlets": sorted(str(path.relative_to(root / "quadlets")) for path in (root / "quadlets").rglob("*") if path.is_file()),
        "units": sorted(path.name for path in (root / "units").glob("*.service")),
        "caddy_sites": sorted(str(path.relative_to(root / "caddy" / "sites-enabled")) for path in (root / "caddy" / "sites-enabled").glob("*.caddy")),
        "artifact_directories": [""] + sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()),
        "artifact_files": sorted(["apply-plan.json"] + [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]),
        "volume_dirs": sorted({json.dumps(item, sort_keys=True) for item in volume_dirs}),
        "static_dirs": sorted(static_dirs),
        "mounts": mounts,
        "secrets": sorted({json.dumps(item, sort_keys=True) for item in secrets}),
        "published_ports": plan["host_published_port_allocations"],
        "all_host_services": plan["host_service_resource_names"],
        "all_host_units": plan["host_unit_resource_names"],
        "all_host_containers": plan["host_container_resource_names"],
        "all_host_networks": plan["host_network_resource_names"],
    }
    apply_plan["volume_dirs"] = [json.loads(item) for item in apply_plan["volume_dirs"]]
    apply_plan["secrets"] = [json.loads(item) for item in apply_plan["secrets"]]
    _write(root / "apply-plan.json", json.dumps(apply_plan, sort_keys=True, indent=2) + "\n")
    return apply_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Render desired host app artifacts")
    parser.add_argument("--apps-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        request = _read_request()
        assigned_apps = request.get("assigned_apps")
        if assigned_apps is None:
            assigned_apps = sorted(path.parent.name for path in args.apps_root.glob("*/app.yml"))
        plan = compile_plan(
            args.apps_root,
            assigned_apps=assigned_apps,
            target_app=request.get("target_app", ""),
            ssh_port=request.get("ssh_port", 22),
            caddy_platform_network=request.get("caddy_platform_network", "caddy-egress-net"),
            defaults=request.get("defaults"),
            app_desired_states=request.get("app_desired_states"),
            draining_apps=request.get("draining_apps"),
            inventory_hosts=request.get("inventory_hosts"),
            current_host=request.get("current_host", ""),
            selected_hosts=request.get("selected_hosts"),
        )
        apply_plan = render(plan, args.artifact_root, request)
    except (OSError, json.JSONDecodeError, ModelError) as error:
        print(f"render error: {error}", file=sys.stderr)
        return 2
    json.dump(apply_plan, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
