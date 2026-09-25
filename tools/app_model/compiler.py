"""Compile application declarations into a deterministic host deployment plan."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver


PLAN_VERSION = 3
NAMING_SCHEME = "dash-escape-kind-v1"
MAX_PHYSICAL_NAME_LENGTH = 63
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_.])?$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CAPABILITY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
MODE_RE = re.compile(r"^0[0-7]{3}$")
SIZE_RE = re.compile(r"^[0-9]+[kKmMgG]?$")
MEMORY_RE = re.compile(r"^[0-9]+[kKmMgGtT]?$")
DURATION_RE = re.compile(r"^[0-9]+(?:ms|s|min|h)$")
CPU_QUOTA_RE = re.compile(r"^(?:|[0-9]+%)$")
CONTAINER_USER_RE = re.compile(r"^[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?$")
NUMERIC_CONTAINER_USER_RE = re.compile(r"^([0-9]+):([0-9]+)$")
SAFE_RELATIVE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
SAFE_ABSOLUTE_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
STATUS_RE = re.compile(r"^[1-5](?:[0-9]{2}|xx)$")
CONTAINER_REFERENCE_RE = re.compile(
    r"\{\{\s*container:([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/"
    r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\s*\}\}"
)
MAX_APP_FILE_SIZE = 1024 * 1024

APP_FIELDS = {"type", "domains", "ingress", "requires_apps", "networks", "containers", "static_paths", "desired_state"}
NETWORK_FIELDS = {"name", "caddy", "internal"}
CONTAINER_FIELDS = {
    "name", "image", "networks", "service_port", "ports", "entrypoint", "proxy_paths",
    "depends_on", "user", "command", "env", "capabilities", "volumes", "mounts", "secrets",
    "published_ports", "healthcheck", "runtime_healthcheck", "tmpfs_tmp_size", "tmpfs_run_size",
    "memory_max", "pids_limit", "cpu_weight", "cpu_shares", "cpu_quota", "restart_policy",
    "restart_sec",
}
INGRESS_FIELDS = {
    "caddy_directives", "extra_caddy", "tls", "disable_default_file_server",
    "handoff_upstream_http1",
}
STATIC_PATH_FIELDS = {"path", "root", "root_base", "strip_prefix"}
PROXY_PATH_FIELDS = {"path", "strip_prefix"}
VOLUME_FIELDS = {"name", "container_path", "directory_mode", "host_path"}
MOUNT_FIELDS = {
    "relative_path", "container_path", "src", "directory_mode", "file_mode", "mode", "host_path",
}
SECRET_FIELDS = {"name", "type", "target"}
PUBLISHED_PORT_FIELDS = {"host_port", "container_port", "protocol", "reason", "host_ip"}
HEALTHCHECK_FIELDS = {
    "path", "interval", "timeout", "passes", "fails", "status", "retry_duration", "retry_interval",
    "fail_duration", "max_fails", "unhealthy_status",
}
RUNTIME_HEALTHCHECK_FIELDS = {"command", "interval", "timeout", "retries", "start_period", "on_failure"}
DEFAULT_FIELDS = {
    "memory_max", "pids_limit", "restart_policy", "restart_sec", "healthcheck", "runtime_healthcheck"
}


class ModelError(ValueError):
    """A precise, operator-facing application model error."""


def _encode_component(value: str) -> str:
    return value.replace("-", "--")


def _physical_name(app_name: str, kind: str, local_name: str, path: str) -> str:
    name = f"{_encode_component(app_name)}-{kind}-{_encode_component(local_name)}"
    if len(name.encode("utf-8")) > MAX_PHYSICAL_NAME_LENGTH:
        raise _error(path, f"derived physical name {name!r} exceeds {MAX_PHYSICAL_NAME_LENGTH} bytes")
    return name


class ModelLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects aliases and duplicate mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise ComposerError(None, None, "YAML aliases are not supported", event.start_mark)
        return super().compose_node(parent, index)


def _construct_unique_mapping(loader: ModelLoader, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


ModelLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True)
class AppModel:
    name: str
    source_path: Path
    data: dict[str, Any]

    @property
    def containers(self) -> list[dict[str, Any]]:
        return self.data["containers"]

    @property
    def networks(self) -> list[dict[str, Any]]:
        return self.data["networks"]

    @property
    def requires_apps(self) -> list[str]:
        return self.data["requires_apps"]

    def as_dict(self) -> dict[str, Any]:
        return self.data


def _error(path: str, message: str) -> ModelError:
    return ModelError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be a mapping")
    return value


def _known_fields(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(repr(key) for key in value if key not in allowed)
    if unknown:
        raise _error(path, f"contains unsupported fields {unknown}")


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(path, "must be a list")
    return value


def _string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise _error(path, "must be a string")
    if nonempty and not value:
        raise _error(path, "must not be empty")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(path, "must be a boolean")
    return value


def _integer(value: Any, path: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, "must be an integer")
    if value < minimum or maximum is not None and value > maximum:
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise _error(path, f"must be in the range {bounds}")
    return value


def _named_mapping_list(value: Any, path: str, pattern: re.Pattern[str] = NAME_RE) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(_list(value, path)):
        item_path = f"{path}[{index}]"
        entry = _mapping(item, item_path)
        name = _string(entry.get("name"), f"{item_path}.name")
        if not pattern.fullmatch(name):
            raise _error(f"{item_path}.name", f"has invalid value {name!r}")
        if name in names:
            raise _error(path, f"contains duplicate name {name!r}")
        names.add(name)
        result.append(entry)
    return result


def _string_list(value: Any, path: str, pattern: re.Pattern[str] | None = None) -> list[str]:
    result: list[str] = []
    for index, item in enumerate(_list(value, path)):
        item = _string(item, f"{path}[{index}]")
        if pattern is not None and not pattern.fullmatch(item):
            raise _error(f"{path}[{index}]", f"has invalid value {item!r}")
        result.append(item)
    if len(result) != len(set(result)):
        raise _error(path, "must not contain duplicates")
    return result


def _safe_relative(value: Any, path: str) -> str:
    value = _string(value, path)
    if value.startswith("/") or not SAFE_RELATIVE_RE.fullmatch(value):
        raise _error(path, "must be a safe relative path")
    parts = value.split("/")
    if "." in parts or ".." in parts or "" in parts[:-1]:
        raise _error(path, "must not contain empty, '.' or '..' components")
    return value


def _safe_absolute(value: Any, path: str) -> str:
    value = _string(value, path)
    if not SAFE_ABSOLUTE_RE.fullmatch(value) or "." in value.split("/") or ".." in value.split("/"):
        raise _error(path, "must be a safe absolute path")
    return value


def _mode(value: Any, path: str) -> str:
    value = str(value)
    if not MODE_RE.fullmatch(value):
        raise _error(path, "must be a four-digit octal mode")
    return value


def _domain(value: Any, path: str) -> str:
    value = _string(value, path).lower()
    if len(value) > 253:
        raise _error(path, "must not exceed 253 characters")
    labels = value.split(".")
    if any(not label or len(label) > 63 for label in labels):
        raise _error(path, "contains an empty or overlong DNS label")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        raise _error(path, f"has invalid domain value {value!r}")
    return value


def _validate_no_line_breaks(value: Any, path: str) -> None:
    if "\n" in str(value) or "\r" in str(value):
        raise _error(path, "must not contain line breaks")


def _caddy_path(value: Any, path: str) -> str:
    value = _string(value, path)
    if not value.startswith("/") or any(character.isspace() or ord(character) < 32 for character in value):
        raise _error(path, "must be an absolute Caddy path matcher without whitespace")
    return value


def _validate_ingress(app: dict[str, Any], path: str) -> None:
    ingress = app.get("ingress", {})
    ingress = _mapping(ingress, f"{path}.ingress")
    _known_fields(ingress, INGRESS_FIELDS, f"{path}.ingress")
    for key in ("caddy_directives", "extra_caddy"):
        if key in ingress:
            _string(ingress[key], f"{path}.ingress.{key}", nonempty=False)
    if "tls" in ingress and ingress["tls"] not in (True, False, "internal"):
        raise _error(f"{path}.ingress.tls", "must be true, false, or 'internal'")
    if "disable_default_file_server" in ingress:
        _boolean(ingress["disable_default_file_server"], f"{path}.ingress.disable_default_file_server")
    if "handoff_upstream_http1" in ingress:
        _boolean(ingress["handoff_upstream_http1"], f"{path}.ingress.handoff_upstream_http1")
    app["ingress"] = ingress


def _resolve_ingress_container_references(app: AppModel, apps: dict[str, AppModel]) -> None:
    allowed_apps = set(_dependency_closure(app.name, apps))
    for key in ("caddy_directives", "extra_caddy"):
        directives = app.data["ingress"].get(key)
        if directives is None:
            continue

        def replace_reference(match: re.Match[str]) -> str:
            referenced_app_name, container_name = match.groups()
            path = f"apps.{app.name}.ingress.{key}"
            if referenced_app_name not in allowed_apps:
                raise _error(path, f"references undeclared dependency {referenced_app_name!r}")
            referenced_app = apps[referenced_app_name]
            containers = {container["name"]: container for container in referenced_app.containers}
            if container_name not in containers:
                raise _error(path, f"references unknown container {referenced_app_name}/{container_name}")
            container = containers[container_name]
            caddy_networks = {
                network["name"] for network in referenced_app.networks if network["caddy"]
            }
            if not set(container["networks"]) & caddy_networks:
                raise _error(
                    path,
                    f"references unreachable container {referenced_app_name}/{container_name}",
                )
            return container["physical_name"]

        resolved = CONTAINER_REFERENCE_RE.sub(replace_reference, directives)
        if "{{" in resolved or "}}" in resolved:
            raise _error(
                f"apps.{app.name}.ingress.{key}",
                "contains an invalid compiler reference",
            )
        app.data["ingress"][key] = resolved


def _validate_static_paths(app: dict[str, Any], path: str) -> None:
    static_paths = _list(app.get("static_paths", []), f"{path}.static_paths")
    for index, raw in enumerate(static_paths):
        item_path = f"{path}.static_paths[{index}]"
        item = _mapping(raw, item_path)
        _known_fields(item, STATIC_PATH_FIELDS, item_path)
        _caddy_path(item.get("path"), f"{item_path}.path")
        if "root" in item:
            _safe_relative(item["root"], f"{item_path}.root")
        if item.get("root_base", "app_public") not in ("app_public", "app_static"):
            raise _error(f"{item_path}.root_base", "must be 'app_public' or 'app_static'")
        if "strip_prefix" in item:
            _boolean(item["strip_prefix"], f"{item_path}.strip_prefix")
    app["static_paths"] = static_paths


def _validate_volume(raw: Any, path: str) -> None:
    volume = _mapping(raw, path)
    _known_fields(volume, VOLUME_FIELDS, path)
    name = _string(volume.get("name"), f"{path}.name")
    if not VOLUME_NAME_RE.fullmatch(name) or name == "volumes":
        raise _error(f"{path}.name", "must be a safe logical name other than 'volumes'")
    _safe_absolute(volume.get("container_path"), f"{path}.container_path")
    if "host_path" in volume:
        raise _error(f"{path}.host_path", "is derived and cannot be configured")
    _mode(volume.get("directory_mode", "0750"), f"{path}.directory_mode")


def _validate_mount(raw: Any, path: str) -> None:
    mount = _mapping(raw, path)
    _known_fields(mount, MOUNT_FIELDS, path)
    _safe_absolute(mount.get("container_path"), f"{path}.container_path")
    _safe_relative(mount.get("relative_path"), f"{path}.relative_path")
    if "host_path" in mount:
        raise _error(f"{path}.host_path", "is derived and cannot be configured")
    if "src" in mount:
        _safe_relative(mount["src"], f"{path}.src")
    directory_mode = _mode(mount.get("directory_mode", "0755"), f"{path}.directory_mode")
    file_mode = _mode(mount.get("file_mode", mount.get("mode", "0644")), f"{path}.file_mode")
    if int(directory_mode[-3], 8) & 0o7 != 0o7 or int(directory_mode[-1], 8) & 0o5 != 0o5:
        raise _error(
            f"{path}.directory_mode",
            "must allow owner read/write/traversal and other-container-UID read/traversal",
        )
    if int(file_mode[-3], 8) & 0o6 != 0o6 or int(file_mode[-1], 8) & 0o4 != 0o4:
        raise _error(f"{path}.file_mode", "must allow owner write and other-container-UID read")


def _validate_secret(raw: Any, path: str) -> None:
    secret = _mapping(raw, path)
    _known_fields(secret, SECRET_FIELDS, path)
    name = _string(secret.get("name"), f"{path}.name")
    if not VOLUME_NAME_RE.fullmatch(name):
        raise _error(f"{path}.name", "must be a safe logical name")
    secret_type = secret.get("type", "mount")
    if secret_type not in ("mount", "env"):
        raise _error(f"{path}.type", "must be 'mount' or 'env'")
    target = secret.get("target", name if secret_type == "env" else f"/run/secrets/{name}")
    if secret_type == "env":
        if not isinstance(target, str) or not ENV_NAME_RE.fullmatch(target):
            raise _error(f"{path}.target", "must be a valid environment variable name")
    else:
        _safe_absolute(target, f"{path}.target")


def _validate_published_port(raw: Any, path: str) -> None:
    port = _mapping(raw, path)
    _known_fields(port, PUBLISHED_PORT_FIELDS, path)
    _integer(port.get("host_port"), f"{path}.host_port", minimum=1, maximum=65535)
    _integer(port.get("container_port"), f"{path}.container_port", minimum=1, maximum=65535)
    if port.get("protocol", "tcp") not in ("tcp", "udp"):
        raise _error(f"{path}.protocol", "must be 'tcp' or 'udp'")
    reason = _string(port.get("reason"), f"{path}.reason")
    if not reason.strip():
        raise _error(f"{path}.reason", "must not be blank")
    if "host_ip" in port:
        raise _error(f"{path}.host_ip", "is not supported")


def _validate_proxy_healthcheck(check: dict[str, Any], path: str) -> None:
    _known_fields(check, HEALTHCHECK_FIELDS, path)
    if "path" in check:
        _caddy_path(check["path"], f"{path}.path")
    for key in ("interval", "timeout", "retry_duration", "retry_interval", "fail_duration"):
        if key in check and not DURATION_RE.fullmatch(str(check[key])):
            raise _error(f"{path}.{key}", "has an invalid duration")
    for key in ("passes", "fails", "max_fails"):
        if key in check:
            _integer(check[key], f"{path}.{key}", minimum=1)
    if "status" in check:
        _health_status(check["status"], f"{path}.status")
    if "unhealthy_status" in check:
        statuses = check["unhealthy_status"]
        statuses = statuses if isinstance(statuses, list) else [statuses]
        if not statuses:
            raise _error(f"{path}.unhealthy_status", "must not be empty")
        for index, status in enumerate(statuses):
            _health_status(status, f"{path}.unhealthy_status[{index}]")


def _validate_runtime_healthcheck(check: dict[str, Any], path: str, *, require_command: bool) -> None:
    _known_fields(check, RUNTIME_HEALTHCHECK_FIELDS, path)
    command = check.get("command")
    if require_command:
        if not isinstance(command, (str, list)) or not command:
            raise _error(f"{path}.command", "must be a nonempty string or list")
        if isinstance(command, str) and not command.strip():
            raise _error(f"{path}.command", "must not be blank")
        for index, argument in enumerate([command] if isinstance(command, str) else command):
            _string(argument, f"{path}.command[{index}]", nonempty=False)
            _validate_no_line_breaks(argument, f"{path}.command[{index}]")
    for key, default in (("interval", "30s"), ("timeout", "10s"), ("start_period", "20s")):
        if not DURATION_RE.fullmatch(str(check.get(key, default))):
            raise _error(f"{path}.{key}", "has an invalid duration")
    _integer(check.get("retries", 3), f"{path}.retries", minimum=1)
    if check.get("on_failure", "kill") not in ("none", "kill", "restart", "stop"):
        raise _error(f"{path}.on_failure", "has an unsupported action")


def _validate_healthchecks(container: dict[str, Any], path: str, defaults: dict[str, Any]) -> None:
    if "healthcheck" in container:
        declaration = _mapping(container["healthcheck"], f"{path}.healthcheck")
        _validate_proxy_healthcheck(defaults["healthcheck"] | declaration, f"{path}.healthcheck")
    if "runtime_healthcheck" in container:
        declaration = _mapping(container["runtime_healthcheck"], f"{path}.runtime_healthcheck")
        _validate_runtime_healthcheck(
            defaults["runtime_healthcheck"] | declaration,
            f"{path}.runtime_healthcheck",
            require_command=True,
        )


def _health_status(value: Any, path: str) -> None:
    if isinstance(value, bool):
        raise _error(path, "must be an HTTP status or status class")
    if isinstance(value, int):
        _integer(value, path, minimum=100, maximum=599)
        return
    if not isinstance(value, str) or not STATUS_RE.fullmatch(value):
        raise _error(path, "must be an HTTP status or status class")


def _normalize_container(
    raw: Any,
    app_name: str,
    index: int,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    path = f"apps.{app_name}.containers[{index}]"
    container = copy.deepcopy(_mapping(raw, path))
    _known_fields(container, CONTAINER_FIELDS, path)
    name = _string(container.get("name"), f"{path}.name")
    if not NAME_RE.fullmatch(name):
        raise _error(f"{path}.name", f"has invalid value {name!r}")
    physical_name = _physical_name(app_name, "ctr", name, f"{path}.name")
    container.update(
        physical_name=physical_name,
        service_name=f"{physical_name}.service",
        quadlet_name=f"{physical_name}.container",
    )
    image = _string(container.get("image"), f"{path}.image")
    if any(character.isspace() for character in image):
        raise _error(f"{path}.image", "must not contain whitespace")

    networks = container.get("networks")
    if networks is None or networks == []:
        networks = ["ingress"]
    container["networks"] = _string_list(networks, f"{path}.networks", NAME_RE)
    if "entrypoint" in container:
        _boolean(container["entrypoint"], f"{path}.entrypoint")

    proxy_paths = _list(container.get("proxy_paths", []), f"{path}.proxy_paths")
    for proxy_index, raw_proxy in enumerate(proxy_paths):
        proxy_path = f"{path}.proxy_paths[{proxy_index}]"
        proxy = _mapping(raw_proxy, proxy_path)
        _known_fields(proxy, PROXY_PATH_FIELDS, proxy_path)
        _caddy_path(proxy.get("path"), f"{proxy_path}.path")
        if "strip_prefix" in proxy:
            _boolean(proxy["strip_prefix"], f"{proxy_path}.strip_prefix")

    container["depends_on"] = _string_list(container.get("depends_on", []), f"{path}.depends_on", NAME_RE)
    env = _mapping(container.get("env", {}), f"{path}.env")
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_NAME_RE.fullmatch(key):
            raise _error(f"{path}.env", f"contains invalid key {key!r}")
        if not isinstance(value, str):
            raise _error(f"{path}.env.{key}", "must be a string")
        _validate_no_line_breaks(value, f"{path}.env.{key}")

    if "user" in container and not CONTAINER_USER_RE.fullmatch(_string(container["user"], f"{path}.user")):
        raise _error(f"{path}.user", "has invalid user syntax")
    if "command" in container:
        command = container["command"]
        if not isinstance(command, (str, list)) or not command:
            raise _error(f"{path}.command", "must be a nonempty string or list")
        if isinstance(command, str) and not command.strip():
            raise _error(f"{path}.command", "must not be blank")
        arguments = [command] if isinstance(command, str) else command
        for argument_index, argument in enumerate(arguments):
            _string(argument, f"{path}.command[{argument_index}]", nonempty=False)
            _validate_no_line_breaks(argument, f"{path}.command[{argument_index}]")

    capabilities = _string_list(container.get("capabilities", []), f"{path}.capabilities")
    for capability in capabilities:
        if not CAPABILITY_RE.fullmatch(capability):
            raise _error(f"{path}.capabilities", f"contains invalid capability {capability!r}")

    volumes = _list(container.get("volumes", []), f"{path}.volumes")
    volume_uid = 0
    volume_gid = 0
    if volumes and "user" in container:
        numeric_user = NUMERIC_CONTAINER_USER_RE.fullmatch(container["user"])
        if not numeric_user:
            raise _error(f"{path}.user", "must be numeric UID:GID when writable volumes are declared")
        volume_uid, volume_gid = (int(value) for value in numeric_user.groups())
    for volume_index, volume in enumerate(volumes):
        _validate_volume(volume, f"{path}.volumes[{volume_index}]")
        physical_name = _physical_name(app_name, "vol", volume["name"], f"{path}.volumes[{volume_index}]")
        volume.update(
            physical_name=physical_name,
            initial_uid=volume_uid,
            initial_gid=volume_gid,
        )
    mounts = _list(container.get("mounts", []), f"{path}.mounts")
    for mount_index, mount in enumerate(mounts):
        _validate_mount(mount, f"{path}.mounts[{mount_index}]")
        if "src" in mount and not mount["src"].startswith(f"{app_name}/mounts/"):
            raise _error(
                f"{path}.mounts[{mount_index}].src",
                f"must remain below {app_name!r}/mounts",
            )
    secrets = _list(container.get("secrets", []), f"{path}.secrets")
    for secret_index, secret in enumerate(secrets):
        _validate_secret(secret, f"{path}.secrets[{secret_index}]")
        physical_name = _physical_name(app_name, "sec", secret["name"], f"{path}.secrets[{secret_index}]")
        secret["physical_name"] = physical_name
        secret["hash_marker_name"] = f"{physical_name}.sha256"
    secret_names = [secret["name"] for secret in secrets]
    if len(secret_names) != len(set(secret_names)):
        raise _error(f"{path}.secrets", "must not contain duplicate names")
    published_ports = _list(container.get("published_ports", []), f"{path}.published_ports")
    for port_index, port in enumerate(published_ports):
        _validate_published_port(port, f"{path}.published_ports[{port_index}]")
    if published_ports:
        ingress_physical_name = _physical_name(app_name, "ing", name, f"{path}.published_ports")
        container.update(
            ingress_physical_name=ingress_physical_name,
            ingress_quadlet_name=f"{ingress_physical_name}.container",
            ingress_service_name=f"{ingress_physical_name}.service",
            pasta_unit_name=f"{container['physical_name']}-pasta.service",
            pasta_service_name=f"{container['physical_name']}-pasta.service",
        )

    if "service_port" in container:
        _integer(container["service_port"], f"{path}.service_port", minimum=1, maximum=65535)
    if "ports" in container:
        for port_index, port in enumerate(_list(container["ports"], f"{path}.ports")):
            _integer(port, f"{path}.ports[{port_index}]", minimum=1, maximum=65535)

    for key, default, pattern in (
        ("tmpfs_tmp_size", "64m", SIZE_RE),
        ("tmpfs_run_size", "32m", SIZE_RE),
        ("memory_max", defaults["memory_max"], MEMORY_RE),
        ("restart_sec", defaults["restart_sec"], DURATION_RE),
        ("cpu_quota", "", CPU_QUOTA_RE),
    ):
        if not pattern.fullmatch(str(container.get(key, default))):
            raise _error(f"{path}.{key}", "has an invalid value")
    _integer(container.get("pids_limit", defaults["pids_limit"]), f"{path}.pids_limit", minimum=1)
    cpu_weight = container.get("cpu_weight", container.get("cpu_shares", 1024))
    _integer(cpu_weight, f"{path}.cpu_weight", minimum=1, maximum=10000)
    if container.get("restart_policy", defaults["restart_policy"]) not in ("no", "on-success", "on-failure", "always"):
        raise _error(f"{path}.restart_policy", "has an unsupported policy")
    _validate_healthchecks(container, path, defaults)
    return container


def _check_container_dependencies(app: AppModel) -> None:
    names = {container["name"] for container in app.containers}
    graph: dict[str, set[str]] = {}
    for container in app.containers:
        name = container["name"]
        dependencies = set(container["depends_on"])
        unknown = sorted(dependencies - names)
        if unknown:
            raise _error(f"apps.{app.name}.containers.{name}.depends_on", f"references unknown containers {unknown}")
        if name in dependencies:
            raise _error(f"apps.{app.name}.containers.{name}.depends_on", "cannot reference itself")
        graph[name] = dependencies
    _topological_order(graph, f"apps.{app.name}.containers")


def _check_managed_mount_destinations(app_name: str, containers: list[dict[str, Any]]) -> None:
    destinations: list[tuple[str, str]] = []
    for container in containers:
        for mount in container.get("mounts", []):
            destination = mount["relative_path"].rstrip("/")
            owner = f"{container['name']}/{mount['relative_path']}"
            for existing, existing_owner in destinations:
                if (
                    destination == existing
                    or destination.startswith(existing + "/")
                    or existing.startswith(destination + "/")
                ):
                    raise _error(
                        f"apps.{app_name}.containers.{container['name']}.mounts",
                        f"managed destination {destination!r} overlaps {existing_owner}",
                    )
            destinations.append((destination, owner))


def _managed_source_digest(source: Path, directory_mode: str, file_mode: str, path: str) -> tuple[str, str]:
    if not source.exists() or source.is_symlink():
        raise _error(path, "must be an existing regular file or directory without symbolic links")
    entries: list[dict[str, str]] = []
    candidates = [source] if source.is_file() else [source, *sorted(source.rglob("*"))]
    for candidate in candidates:
        relative = "." if candidate == source else candidate.relative_to(source).as_posix()
        candidate_stat = candidate.lstat()
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise _error(path, f"contains unsupported symbolic link {relative!r}")
        if stat.S_ISDIR(candidate_stat.st_mode):
            entries.append({"kind": "directory", "path": relative, "mode": directory_mode})
            continue
        if not stat.S_ISREG(candidate_stat.st_mode) or candidate_stat.st_nlink != 1:
            raise _error(path, f"contains unsupported or hard-linked entry {relative!r}")
        entries.append(
            {
                "kind": "file",
                "path": relative,
                "mode": file_mode,
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            }
        )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ("file" if source.is_file() else "directory", hashlib.sha256(payload).hexdigest())


def _normalize_app(name: str, source_path: Path, raw: Any, defaults: dict[str, Any]) -> AppModel:
    path = f"apps.{name}"
    if not NAME_RE.fullmatch(name):
        raise _error(path, "directory name must be a lowercase logical name")
    app = copy.deepcopy(_mapping(raw, path))
    _known_fields(app, APP_FIELDS, path)

    app_type = app.get("type", "container")
    if app_type not in ("container", "static"):
        raise _error(f"{path}.type", "must be 'container' or 'static'")
    domains = [_domain(value, f"{path}.domains[{index}]") for index, value in enumerate(_list(app.get("domains", []), f"{path}.domains"))]
    if len(domains) != len(set(domains)):
        raise _error(f"{path}.domains", "must not contain duplicates")
    requires_apps = _string_list(app.get("requires_apps", []), f"{path}.requires_apps", NAME_RE)
    raw_containers = _list(app.get("containers", []), f"{path}.containers")
    containers = [
        _normalize_container(raw_container, name, index, defaults)
        for index, raw_container in enumerate(raw_containers)
    ]
    container_names = [container["name"] for container in containers]
    if len(container_names) != len(set(container_names)):
        raise _error(f"{path}.containers", "must not contain duplicate names")
    volume_contracts: dict[str, tuple[int, int, str]] = {}
    for container in containers:
        for volume in container.get("volumes", []):
            contract = (
                volume["initial_uid"],
                volume["initial_gid"],
                str(volume.get("directory_mode", "0750")),
            )
            if volume["name"] in volume_contracts and volume_contracts[volume["name"]] != contract:
                raise _error(
                    f"{path}.volumes.{volume['name']}",
                    "has conflicting UID:GID or creation mode across consuming containers",
                )
            volume_contracts[volume["name"]] = contract
    _check_managed_mount_destinations(name, containers)
    managed_root = (source_path / "mounts").resolve()
    apps_root = source_path.parent.resolve()
    for container in containers:
        mount_digests: list[dict[str, str]] = []
        for mount_index, mount in enumerate(container.get("mounts", [])):
            source_name = mount.get("src", f"{name}/mounts/{mount['relative_path']}")
            source_name = source_name.replace("{{ container.name }}", container["name"])
            source = apps_root.joinpath(source_name)
            source_cursor = apps_root
            for component in Path(source_name).parts:
                source_cursor /= component
                if source_cursor.is_symlink():
                    raise _error(
                        f"{path}.containers.{container['name']}.mounts[{mount_index}].src",
                        "must not traverse symbolic links",
                    )
            resolved_source = source.resolve()
            if not resolved_source.is_relative_to(managed_root):
                raise _error(
                    f"{path}.containers.{container['name']}.mounts[{mount_index}].src",
                    f"must remain below {name!r}/mounts",
                )
            directory_mode = str(mount.get("directory_mode", "0755"))
            file_mode = str(mount.get("file_mode", mount.get("mode", "0644")))
            source_kind, source_digest = _managed_source_digest(
                source,
                directory_mode,
                file_mode,
                f"{path}.containers.{container['name']}.mounts[{mount_index}].src",
            )
            mount.update(
                src_absolute=str(resolved_source),
                source_kind=source_kind,
                source_digest=source_digest,
                directory_mode=directory_mode,
                file_mode=file_mode,
            )
            mount_digests.append(
                {
                    "container_path": mount["container_path"],
                    "relative_path": mount["relative_path"],
                    "source_digest": source_digest,
                }
            )
        container["managed_config_digest"] = hashlib.sha256(
            json.dumps(mount_digests, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    raw_networks = app.get("networks")
    if raw_networks is None or raw_networks == []:
        raw_networks = [{"name": "ingress", "caddy": True}] if containers else []
    networks: list[dict[str, Any]] = []
    for index, raw_network in enumerate(_named_mapping_list(raw_networks, f"{path}.networks")):
        network_path = f"{path}.networks[{index}]"
        network = copy.deepcopy(raw_network)
        _known_fields(network, NETWORK_FIELDS, network_path)
        caddy = _boolean(network.get("caddy", False), f"{network_path}.caddy")
        internal = _boolean(network.get("internal", False), f"{network_path}.internal")
        if caddy and internal:
            raise _error(network_path, "cannot be both Caddy-facing and internal")
        physical_name = _physical_name(name, "net", network["name"], f"{network_path}.name")
        network.update(
            physical_name=physical_name,
            quadlet_name=f"{physical_name}.network",
            service_name=f"{physical_name}-network.service",
            caddy=caddy,
            internal=internal,
        )
        networks.append(network)

    network_names = {network["name"] for network in networks}
    caddy_networks = {network["name"] for network in networks if network["caddy"]}
    for container in containers:
        unknown = sorted(set(container["networks"]) - network_names)
        if unknown:
            raise _error(f"{path}.containers.{container['name']}.networks", f"references unknown networks {unknown}")
        routed = container.get("entrypoint", False) or bool(container.get("proxy_paths", []))
        if routed and not set(container["networks"]) & caddy_networks:
            raise _error(f"{path}.containers.{container['name']}", "is routed but has no Caddy-facing network")
    for network_name in sorted(caddy_networks):
        if not any(network_name in container["networks"] for container in containers):
            raise _error(f"{path}.networks.{network_name}", "is Caddy-facing but has no container members")
    if sum(container.get("entrypoint", False) for container in containers) > 1:
        raise _error(f"{path}.containers", "must not contain more than one entrypoint")
    if app_type == "static" and (containers or networks):
        raise _error(path, "static applications cannot declare containers or networks")

    resources = {
        "services": ([container["service_name"] for container in containers]
                     + [container["ingress_service_name"] for container in containers if container.get("published_ports")]
                     + [container["pasta_service_name"] for container in containers if container.get("published_ports")]),
        "containers": ([container["physical_name"] for container in containers]
                       + [container["ingress_physical_name"] for container in containers if container.get("published_ports")]),
        "quadlets": ([container["quadlet_name"] for container in containers]
                     + [container["ingress_quadlet_name"] for container in containers if container.get("published_ports")]),
        "units": [container["pasta_unit_name"] for container in containers if container.get("published_ports")],
        "networks": [network["physical_name"] for network in networks],
        "network_quadlets": [network["quadlet_name"] for network in networks],
        "network_services": [network["service_name"] for network in networks],
        "caddy_networks": [network["physical_name"] for network in networks if network["caddy"]],
        "secrets": sorted({secret["physical_name"] for container in containers for secret in container.get("secrets", [])}),
    }
    desired_state = _string(app.get("desired_state", "running"), f"{path}.desired_state")
    if desired_state not in ("running", "inactive"):
        raise _error(f"{path}.desired_state", "must be running or inactive")
    app.update(
        desired_state=desired_state,
        name=name,
        source_path=str(source_path),
        type=app_type,
        domains=domains,
        requires_apps=requires_apps,
        networks=networks,
        containers=containers,
        resources=resources,
    )
    _validate_ingress(app, path)
    _validate_static_paths(app, path)
    model = AppModel(name=name, source_path=source_path, data=app)
    _check_container_dependencies(model)
    service_names = {container["name"]: container["service_name"] for container in containers}
    for container in containers:
        container["dependency_services"] = [service_names[dependency] for dependency in container["depends_on"]]
    try:
        json.dumps(app)
    except (TypeError, ValueError) as error:
        raise _error(path, f"contains a non-JSON value: {error}") from error
    return model


def _topological_order(graph: dict[str, set[str]], path: str) -> list[str]:
    order: list[str] = []
    remaining = set(graph)
    while remaining:
        ready = sorted(name for name in remaining if graph[name] <= set(order))
        if not ready:
            raise _error(path, f"contains a dependency cycle involving {sorted(remaining)}")
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def _dependency_closure(name: str, apps: dict[str, AppModel]) -> list[str]:
    closure = [name]
    for current in closure:
        for dependency in apps[current].requires_apps:
            if dependency not in closure:
                closure.append(dependency)
    return closure


def _ensure_unique(values: list[str], path: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise _error(path, f"contains duplicate derived resource {value!r}")
        seen.add(value)


def _resource_names(apps: list[AppModel]) -> dict[str, list[str]]:
    containers = [container["physical_name"] for app in apps for container in app.containers]
    containers += [container["ingress_physical_name"] for app in apps for container in app.containers if container.get("published_ports")]
    networks = [network["physical_name"] for app in apps for network in app.networks]
    services = [container["service_name"] for app in apps for container in app.containers]
    services += [container["ingress_service_name"] for app in apps for container in app.containers if container.get("published_ports")]
    services += [container["pasta_service_name"] for app in apps for container in app.containers if container.get("published_ports")]
    services += [network["service_name"] for app in apps for network in app.networks]
    quadlets = [container["quadlet_name"] for app in apps for container in app.containers]
    quadlets += [container["ingress_quadlet_name"] for app in apps for container in app.containers if container.get("published_ports")]
    quadlets += [network["quadlet_name"] for app in apps for network in app.networks]
    units = [container["pasta_unit_name"] for app in apps for container in app.containers if container.get("published_ports")]
    secrets = sorted({secret["physical_name"] for app in apps for container in app.containers for secret in container.get("secrets", [])})
    return {
        "containers": containers,
        "networks": networks,
        "services": services,
        "quadlets": quadlets,
        "units": units,
        "secrets": secrets,
    }


def _published_ports(apps: list[AppModel]) -> list[dict[str, Any]]:
    allocations: list[dict[str, Any]] = []
    for app in apps:
        for container in app.containers:
            for port in container.get("published_ports", []):
                allocations.append(
                    {
                        "app": app.name,
                        "container": container["name"],
                        "owner": f"{app.name}/{container['name']}",
                        "host_port": port["host_port"],
                        "container_port": port["container_port"],
                        "protocol": port.get("protocol", "tcp"),
                        "reason": port["reason"],
                        "host_ip_defined": False,
                    }
                )
    return allocations


def _load_apps(apps_root: Path, defaults: dict[str, Any]) -> list[AppModel]:
    unresolved_root = apps_root.expanduser()
    if unresolved_root.is_symlink():
        raise _error("apps_root", "must not be a symbolic link")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise _error("apps_root", f"{root} is not a directory")
    apps: list[AppModel] = []
    for app_file in sorted(root.glob("*/app.yml")):
        if app_file.parent.is_symlink() or app_file.is_symlink() or not app_file.is_file():
            raise _error(str(app_file), "must be a regular file below a regular app directory")
        if app_file.stat().st_size > MAX_APP_FILE_SIZE:
            raise _error(str(app_file), f"must not exceed {MAX_APP_FILE_SIZE} bytes")
        try:
            raw = yaml.load(app_file.read_text(encoding="utf-8"), Loader=ModelLoader)
        except (OSError, yaml.YAMLError) as error:
            raise _error(str(app_file), f"cannot be loaded: {error}") from error
        apps.append(_normalize_app(app_file.parent.name, app_file.parent.resolve(), raw, defaults))
    return apps


def _normalize_defaults(raw: dict[str, Any] | None) -> dict[str, Any]:
    defaults = {
        "memory_max": "512M",
        "pids_limit": 512,
        "restart_policy": "on-failure",
        "restart_sec": "5s",
        "healthcheck": {},
        "runtime_healthcheck": {
            "interval": "30s",
            "timeout": "10s",
            "retries": 3,
            "start_period": "20s",
            "on_failure": "kill",
        },
    }
    if raw is not None:
        raw = _mapping(raw, "defaults")
        _known_fields(raw, DEFAULT_FIELDS, "defaults")
        defaults.update(copy.deepcopy(raw))
    if not MEMORY_RE.fullmatch(str(defaults["memory_max"])):
        raise _error("defaults.memory_max", "has an invalid value")
    _integer(defaults["pids_limit"], "defaults.pids_limit", minimum=1)
    if defaults["restart_policy"] not in ("no", "on-success", "on-failure", "always"):
        raise _error("defaults.restart_policy", "has an unsupported policy")
    if not DURATION_RE.fullmatch(str(defaults["restart_sec"])):
        raise _error("defaults.restart_sec", "has an invalid duration")
    defaults["healthcheck"] = copy.deepcopy(_mapping(defaults["healthcheck"], "defaults.healthcheck"))
    defaults["runtime_healthcheck"] = copy.deepcopy(
        _mapping(defaults["runtime_healthcheck"], "defaults.runtime_healthcheck")
    )
    _validate_proxy_healthcheck(defaults["healthcheck"], "defaults.healthcheck")
    _validate_runtime_healthcheck(
        defaults["runtime_healthcheck"],
        "defaults.runtime_healthcheck",
        require_command=False,
    )
    return defaults


def _validate_inventory_topology(
    inventory_hosts: dict[str, Any] | None,
    apps: dict[str, AppModel],
) -> tuple[dict[str, dict[str, Any]], str]:
    topology = _mapping(inventory_hosts if inventory_hosts is not None else {}, "inventory_hosts")
    normalized: dict[str, dict[str, Any]] = {}
    owners: dict[str, list[str]] = {}
    drains: dict[str, list[str]] = {}
    outgoing_by_app: dict[str, list[tuple[str, str]]] = {}
    incoming_by_app: dict[str, list[tuple[str, str]]] = {}
    for raw_host, raw_declaration in topology.items():
        host = _string(raw_host, "inventory_hosts host")
        declaration = _mapping(raw_declaration, f"inventory_hosts.{host}")
        unknown_fields = sorted(set(declaration) - {
            "assigned_apps", "draining_apps", "outgoing_migrations", "incoming_migrations"
        })
        if unknown_fields:
            raise _error(f"inventory_hosts.{host}", f"contains unsupported fields {unknown_fields}")
        assigned = sorted(_string_list(declaration.get("assigned_apps", []), f"inventory_hosts.{host}.assigned_apps", NAME_RE))
        draining_raw = _mapping(declaration.get("draining_apps", {}), f"inventory_hosts.{host}.draining_apps")
        draining: dict[str, dict[str, str]] = {}
        for app_name, raw_drain in draining_raw.items():
            app_name = _string(app_name, f"inventory_hosts.{host}.draining_apps app")
            if not NAME_RE.fullmatch(app_name):
                raise _error(f"inventory_hosts.{host}.draining_apps", f"has invalid app {app_name!r}")
            drain = _mapping(raw_drain, f"inventory_hosts.{host}.draining_apps.{app_name}")
            unknown = sorted(set(drain) - {"target_host", "target_address"})
            if unknown:
                raise _error(f"inventory_hosts.{host}.draining_apps.{app_name}", f"contains unsupported fields {unknown}")
            target_host = _string(drain.get("target_host"), f"inventory_hosts.{host}.draining_apps.{app_name}.target_host")
            target_address = _string(drain.get("target_address"), f"inventory_hosts.{host}.draining_apps.{app_name}.target_address")
            draining[app_name] = {"target_host": target_host, "target_address": target_address}
            drains.setdefault(app_name, []).append(host)
        outgoing_raw = _mapping(declaration.get("outgoing_migrations", {}), f"inventory_hosts.{host}.outgoing_migrations")
        incoming_raw = _mapping(declaration.get("incoming_migrations", {}), f"inventory_hosts.{host}.incoming_migrations")
        outgoing: dict[str, dict[str, str]] = {}
        incoming: dict[str, dict[str, str]] = {}
        for app_name, raw_intent in outgoing_raw.items():
            app_name = _string(app_name, f"inventory_hosts.{host}.outgoing_migrations app")
            intent = _mapping(raw_intent, f"inventory_hosts.{host}.outgoing_migrations.{app_name}")
            unknown = sorted(set(intent) - {"target_host"})
            if unknown:
                raise _error(f"inventory_hosts.{host}.outgoing_migrations.{app_name}", f"contains unsupported fields {unknown}")
            target_host = _string(intent.get("target_host"), f"inventory_hosts.{host}.outgoing_migrations.{app_name}.target_host")
            outgoing[app_name] = {"target_host": target_host}
            outgoing_by_app.setdefault(app_name, []).append((host, target_host))
        for app_name, raw_intent in incoming_raw.items():
            app_name = _string(app_name, f"inventory_hosts.{host}.incoming_migrations app")
            intent = _mapping(raw_intent, f"inventory_hosts.{host}.incoming_migrations.{app_name}")
            unknown = sorted(set(intent) - {"source_host"})
            if unknown:
                raise _error(f"inventory_hosts.{host}.incoming_migrations.{app_name}", f"contains unsupported fields {unknown}")
            source_host = _string(intent.get("source_host"), f"inventory_hosts.{host}.incoming_migrations.{app_name}.source_host")
            incoming[app_name] = {"source_host": source_host}
            incoming_by_app.setdefault(app_name, []).append((host, source_host))
        unknown_apps = sorted((set(assigned) | set(draining) | set(outgoing) | set(incoming)) - set(apps))
        if unknown_apps:
            raise _error(f"inventory_hosts.{host}", f"references unknown applications {unknown_apps}")
        overlap = sorted(set(assigned) & set(draining))
        if overlap:
            raise _error(f"inventory_hosts.{host}", f"apps cannot be both assigned and draining {overlap}")
        for app_name in assigned:
            owners.setdefault(app_name, []).append(host)
        normalized[host] = {
            "assigned_apps": assigned,
            "draining_apps": draining,
            "outgoing_migrations": outgoing,
            "incoming_migrations": incoming,
        }

    duplicate_owners = {name: hosts for name, hosts in owners.items() if len(hosts) > 1}
    if duplicate_owners:
        raise _error("inventory_hosts.assigned_apps", f"applications have multiple owners {duplicate_owners}")
    duplicate_drains = {name: hosts for name, hosts in drains.items() if len(hosts) > 1}
    if duplicate_drains:
        raise _error("inventory_hosts.draining_apps", f"applications drain from multiple hosts {duplicate_drains}")
    intent_apps = sorted(set(outgoing_by_app) | set(incoming_by_app))
    for app_name in intent_apps:
        outgoing_entries = outgoing_by_app.get(app_name, [])
        incoming_entries = incoming_by_app.get(app_name, [])
        if len(outgoing_entries) != 1 or len(incoming_entries) != 1:
            raise _error("inventory_hosts.migrations", f"{app_name!r} requires exactly one paired outgoing and incoming declaration")
        source_host, outgoing_target = outgoing_entries[0]
        target_host, incoming_source = incoming_entries[0]
        if source_host == target_host or outgoing_target != target_host or incoming_source != source_host:
            raise _error("inventory_hosts.migrations", f"{app_name!r} has mismatched source/target declarations")
        if source_host not in normalized or target_host not in normalized:
            raise _error("inventory_hosts.migrations", f"{app_name!r} references a host outside app_hosts")
        if owners.get(app_name, []) != [source_host]:
            raise _error("inventory_hosts.migrations", f"{app_name!r} must remain solely assigned to its declared source before handoff")
        if app_name in drains:
            raise _error("inventory_hosts.migrations", f"{app_name!r} cannot be draining while migration intent is active")

    for source_host, declaration in normalized.items():
        for app_name, drain in declaration["draining_apps"].items():
            target_host = drain["target_host"]
            if target_host == source_host or target_host not in normalized:
                raise _error(f"inventory_hosts.{source_host}.draining_apps.{app_name}.target_host", "must name a different inventory host")
            if owners.get(app_name, []) != [target_host]:
                raise _error(
                    f"inventory_hosts.{source_host}.draining_apps.{app_name}",
                    f"requires exactly one assigned owner on {target_host!r}",
                )
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return normalized, digest


def compile_plan(
    apps_root: str | Path,
    *,
    assigned_apps: list[str],
    target_app: str = "",
    ssh_port: int,
    caddy_platform_network: str = "caddy-egress-net",
    defaults: dict[str, Any] | None = None,
    app_desired_states: dict[str, str] | None = None,
    draining_apps: dict[str, Any] | None = None,
    inventory_hosts: dict[str, Any] | None = None,
    current_host: str = "",
    selected_hosts: list[str] | None = None,
) -> dict[str, Any]:
    """Compile all declarations and select the desired state for one host."""
    unresolved_root = Path(apps_root).expanduser()
    compiler_defaults = _normalize_defaults(defaults)
    apps_list = _load_apps(unresolved_root, compiler_defaults)
    root = unresolved_root.resolve()
    apps = {app.name: app for app in apps_list}
    if len(apps) != len(apps_list):
        raise _error("apps", "contains duplicate application names")

    all_domains = [domain for app in apps_list for domain in app.data["domains"]]
    _ensure_unique(all_domains, "apps.domains")
    all_resources = _resource_names(apps_list)
    for resource_type, names in all_resources.items():
        _ensure_unique(names, f"apps.resources.{resource_type}")

    caddy_platform_network = _string(caddy_platform_network, "caddy_platform_network")
    if caddy_platform_network != "caddy-egress-net":
        raise _error("caddy_platform_network", "must be the fixed name 'caddy-egress-net'")
    caddy_platform_quadlet = f"{caddy_platform_network[:-4]}.network"
    caddy_platform_service = f"{caddy_platform_network[:-4]}-network.service"
    if caddy_platform_network in all_resources["networks"]:
        raise _error("apps.resources.networks", "collides with the Caddy platform network")
    if caddy_platform_quadlet in all_resources["quadlets"] or caddy_platform_service in all_resources["services"]:
        raise _error("apps.resources", "collides with a Caddy platform unit")

    for app in apps_list:
        unknown = sorted(set(app.requires_apps) - set(apps))
        if unknown:
            raise _error(f"apps.{app.name}.requires_apps", f"references unknown applications {unknown}")
        if app.name in app.requires_apps:
            raise _error(f"apps.{app.name}.requires_apps", "cannot reference itself")
    _topological_order({app.name: set(app.requires_apps) for app in apps_list}, "apps.requires_apps")
    for app in apps_list:
        _resolve_ingress_container_references(app, apps)

    normalized_topology, inventory_topology_digest = _validate_inventory_topology(inventory_hosts, apps)
    current_host = _string(current_host, "current_host", nonempty=False).strip()
    if normalized_topology and current_host not in normalized_topology:
        raise _error("current_host", f"{current_host!r} is not present in inventory_hosts")
    selected_hosts = _string_list(selected_hosts if selected_hosts is not None else [], "selected_hosts")
    unknown_selected = sorted(set(selected_hosts) - set(normalized_topology)) if normalized_topology else []
    if unknown_selected:
        raise _error("selected_hosts", f"references unknown inventory hosts {unknown_selected}")

    assigned_apps = sorted(_string_list(assigned_apps, "assigned_apps", NAME_RE))
    unknown_assigned = sorted(set(assigned_apps) - set(apps))
    if unknown_assigned:
        raise _error("assigned_apps", f"references unknown applications {unknown_assigned}")
    drains = _mapping(draining_apps if draining_apps is not None else {}, "draining_apps")
    normalized_drains: dict[str, dict[str, str]] = {}
    for name, raw_drain in drains.items():
        if name not in apps:
            raise _error("draining_apps", f"references unknown application {name!r}")
        if name in assigned_apps:
            raise _error("draining_apps", f"{name!r} cannot also be assigned to this host")
        drain = _mapping(raw_drain, f"draining_apps.{name}")
        unknown = sorted(set(drain) - {"target_host", "target_address"})
        if unknown:
            raise _error(f"draining_apps.{name}", f"contains unsupported fields {unknown}")
        normalized_drains[name] = {
            "target_host": _string(drain.get("target_host"), f"draining_apps.{name}.target_host"),
            "target_address": _string(drain.get("target_address"), f"draining_apps.{name}.target_address"),
        }
    if normalized_topology and normalized_drains != normalized_topology[current_host]["draining_apps"]:
        raise _error("draining_apps", "does not match the globally validated inventory declaration")

    overrides = _mapping(app_desired_states if app_desired_states is not None else {}, "app_desired_states")
    for name, state in overrides.items():
        if name not in assigned_apps:
            raise _error("app_desired_states", f"{name!r} is not assigned to this host")
        if not isinstance(state, str) or state not in ("running", "inactive"):
            raise _error(f"app_desired_states.{name}", "must be running or inactive")
        apps[name].data["desired_state"] = state
    for name, drain in normalized_drains.items():
        apps[name].data["desired_state"] = "draining"
        apps[name].data["drain_target_host"] = drain["target_host"]
        apps[name].data["drain_target_address"] = drain["target_address"]
    host_app_names = sorted(set(assigned_apps) | set(normalized_drains))
    host_apps = [app for app in apps_list if app.name in host_app_names]
    for app in host_apps:
        if app.name in normalized_drains:
            continue
        missing = sorted(set(app.requires_apps) - set(assigned_apps))
        if missing:
            raise _error(f"assigned_apps.{app.name}", f"is missing required applications {missing}")

    for app in host_apps:
        if app.data["desired_state"] == "running":
            inactive = [name for name in app.requires_apps if apps[name].data["desired_state"] == "inactive"]
            if inactive:
                raise _error(f"apps.{app.name}.requires_apps", f"running application requires inactive applications {inactive}")

    host_graph = {
        app.name: (set() if app.name in normalized_drains else set(app.requires_apps))
        for app in host_apps
    }
    host_order = _topological_order(host_graph, "assigned_apps")
    closures = {
        app.name: ([app.name] if app.name in normalized_drains else _dependency_closure(app.name, apps))
        for app in host_apps
    }
    target_app = _string(target_app, "target_app", nonempty=False).strip()
    deployment_is_targeted = bool(target_app)
    if deployment_is_targeted and target_app not in host_app_names:
        raise _error("target_app", f"{target_app!r} is not assigned or draining on this host")

    selected_names = set(closures[target_app] if deployment_is_targeted else [app.name for app in host_apps])
    deploy_apps = [app for app in host_apps if app.name in selected_names]
    deploy_order = [name for name in host_order if name in selected_names]
    host_resources = _resource_names(host_apps)
    declared_allocations = _published_ports(host_apps)
    allocations = _published_ports([app for app in host_apps if app.data["desired_state"] == "running"])
    ssh_port = _integer(ssh_port, "ssh_port", minimum=1, maximum=65535)
    binding_owners: dict[tuple[str, int], str] = {
        ("tcp", ssh_port): "platform/ssh",
        ("tcp", 80): "platform/http",
        ("tcp", 443): "platform/https",
        ("udp", 443): "platform/http3",
    }
    if len(binding_owners) != 4:
        raise _error("ssh_port", "collides with a Caddy platform port")
    for allocation in declared_allocations:
        key = (allocation["protocol"], allocation["host_port"])
        if key in binding_owners:
            raise _error(
                "published_ports",
                f"{allocation['owner']} conflicts with {binding_owners[key]} on {key[0]}/{key[1]}",
            )
        binding_owners[key] = allocation["owner"]

    host_caddy_networks = sorted(
        {network["physical_name"] for app in host_apps if app.data["desired_state"] == "running" for network in app.networks if network["caddy"]}
    )
    deploy_caddy_networks = sorted(
        {network["physical_name"] for app in deploy_apps if app.data["desired_state"] == "running" for network in app.networks if network["caddy"]}
    )
    assigned_apps_digest = hashlib.sha256(
        json.dumps(assigned_apps, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "version": PLAN_VERSION,
        "naming_scheme": NAMING_SCHEME,
        "scope": "targeted" if deployment_is_targeted else "full",
        "requested_app": target_app,
        "app_sources_root": str(root),
        "apps_list": [app.as_dict() for app in apps_list],
        "host_assigned_app_names": assigned_apps,
        "host_draining_apps": normalized_drains,
        "host_assigned_apps_digest": assigned_apps_digest,
        "inventory_topology_digest": inventory_topology_digest,
        "host_apps": [app.as_dict() for app in host_apps],
        "deployment_is_targeted": deployment_is_targeted,
        "host_app_dependency_order": host_order,
        "host_app_dependency_closures": closures,
        "host_container_resource_names": host_resources["containers"],
        "host_network_resource_names": host_resources["networks"],
        "host_service_resource_names": host_resources["services"],
        "host_quadlet_resource_names": host_resources["quadlets"],
        "host_unit_resource_names": host_resources["units"],
        "host_secret_resource_names": host_resources["secrets"],
        "host_published_port_allocations": allocations,
        "deploy_apps": [app.as_dict() for app in deploy_apps],
        "deploy_app_service_order": deploy_order,
        "host_caddy_networks": host_caddy_networks,
        "deploy_caddy_networks": deploy_caddy_networks,
    }
