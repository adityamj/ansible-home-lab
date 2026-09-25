#!/usr/bin/env python3
"""Declare paired inventory intent, then start a fresh migration playbook process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
TOP_LEVEL_RE = re.compile(r"^(?!\s|#)([A-Za-z_][A-Za-z0-9_]*):(?:\s.*)?$")


def block_span(text: str, key: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == f"{key}:"), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not TOP_LEVEL_RE.match(lines[end].rstrip("\n")):
        end += 1
    return start, end


def assigned_apps(text: str) -> set[str]:
    lines = text.splitlines(keepends=True)
    span = block_span(text, "assigned_apps")
    if span is None:
        return set()
    start, end = span
    result: set[str] = set()
    for line in lines[start + 1:end]:
        match = re.fullmatch(r"  - ([a-z0-9][a-z0-9-]*)\s*\n?", line)
        if match:
            result.add(match.group(1))
    return result


def mapping_apps(text: str, key: str) -> set[str]:
    lines = text.splitlines(keepends=True)
    span = block_span(text, key)
    if span is None:
        return set()
    start, end = span
    return {
        match.group(1)
        for line in lines[start + 1:end]
        if (match := re.fullmatch(r"  ([a-z0-9][a-z0-9-]*):\s*\n?", line))
    }


def add_mapping_entry(text: str, key: str, app: str, values: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    span = block_span(text, key)
    entry = f"  {app}:\n" + "".join(f"    {name}: {value}\n" for name, value in values.items())
    if span is None:
        block = f"\n{key}:\n{entry}"
        insert = next((i for i, line in enumerate(lines) if line.rstrip() == "caddy_tuning:"), len(lines))
        lines.insert(insert, block)
        return "".join(lines)

    start, end = span
    existing = mapping_apps(text, key)
    if app in existing:
        block_text = "".join(lines[start:end])
        expected_lines = f"  {app}:\n" + "".join(
            f"    {name}: {value}\n" for name, value in values.items()
        )
        if expected_lines.rstrip() not in block_text:
            raise ValueError(f"{key}.{app} already exists with different content")
        return text
    lines.insert(end, entry)
    return "".join(lines)


def add_intent(text: str, key: str, app: str, peer_key: str, peer: str) -> str:
    return add_mapping_entry(text, key, app, {peer_key: peer})


def remove_mapping_entry(text: str, key: str, app: str) -> str:
    lines = text.splitlines(keepends=True)
    span = block_span(text, key)
    if span is None:
        return text
    start, end = span
    app_start = next((i for i in range(start + 1, end) if lines[i].rstrip() == f"  {app}:"), None)
    if app_start is None:
        return text
    app_end = app_start + 1
    while app_end < end and not re.match(r"^  [a-z0-9][a-z0-9-]*:\s*$", lines[app_end].rstrip("\n")):
        app_end += 1
    del lines[app_start:app_end]
    remaining = "".join(lines[start + 1:end - (app_end - app_start)])
    if not re.search(r"^  [a-z0-9][a-z0-9-]*:\s*$", remaining, re.MULTILINE):
        del lines[start:start + 1]
        if start < len(lines) and not lines[start].strip():
            del lines[start]
    return "".join(lines)


def remove_assignment(text: str, app: str) -> str:
    return re.sub(rf"^  - {re.escape(app)}\s*\n", "", text, count=1, flags=re.MULTILINE)


def add_assignment(text: str, app: str) -> str:
    if app in assigned_apps(text):
        return text
    lines = text.splitlines(keepends=True)
    span = block_span(text, "assigned_apps")
    if span is None:
        raise ValueError("assigned_apps is missing")
    lines.insert(span[1], f"  - {app}\n")
    return "".join(lines)


def inventory_address(inventory_path: Path, host: str) -> str:
    text = inventory_path.read_text(encoding="utf-8")
    match = re.search(
        rf"^        {re.escape(host)}:\s*\n(?:^          .*\n)*?^          ansible_host:\s*([0-9.]+)\s*$",
        text,
        re.MULTILINE,
    )
    if not match:
        raise ValueError(f"cannot find IPv4 ansible_host for {host!r} in {inventory_path}")
    return match.group(1)


def atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--phase", choices=("prepare", "all", "cutover", "handoff", "cleanup"), default="all")
    parser.add_argument("--confirm-cutover", action="store_true")
    parser.add_argument("--approve-target-prune", action="store_true")
    parser.add_argument("--confirm-endpoint-retirement", action="store_true")
    parser.add_argument("--direct-port-policy", choices=("reject", "outage_until_dns"), default="reject")
    parser.add_argument("--target-address", help="target IPv4; defaults to production inventory ansible_host")
    parser.add_argument("--host-vars-dir", type=Path)
    parser.add_argument("--ansible-playbook", default="ansible-playbook")
    args = parser.parse_args()

    if not NAME_RE.fullmatch(args.app):
        parser.error("--app must be a safe application name")
    if args.source == args.target or not HOST_RE.fullmatch(args.source) or not HOST_RE.fullmatch(args.target):
        parser.error("--source and --target must be different inventory host names")
    if args.phase in ("all", "cutover") and not (args.confirm_cutover and args.approve_target_prune):
        parser.error("all/cutover requires --confirm-cutover and --approve-target-prune")
    if args.phase == "cleanup" and not args.confirm_endpoint_retirement:
        parser.error("cleanup requires --confirm-endpoint-retirement")

    repo_root = Path(__file__).resolve().parent.parent
    host_vars_dir = args.host_vars_dir or repo_root / "inventories/production/host_vars"
    source_path = host_vars_dir / f"{args.source}.yml"
    target_path = host_vars_dir / f"{args.target}.yml"
    if not source_path.is_file() or not target_path.is_file():
        parser.error("source and target host-var files must exist")

    if args.phase in ("prepare", "all", "cutover"):
        source_text = source_path.read_text(encoding="utf-8")
        target_text = target_path.read_text(encoding="utf-8")
        if args.app not in assigned_apps(source_text) or args.app in assigned_apps(target_text):
            parser.error("before handoff the app must be assigned solely to the source")
        if args.app in mapping_apps(source_text, "draining_apps"):
            parser.error("the source app cannot already be draining")
        source_new = add_intent(source_text, "outgoing_migrations", args.app, "target_host", args.target)
        target_new = add_intent(target_text, "incoming_migrations", args.app, "source_host", args.source)
        # Prepare both complete files before either replacement. A process failure
        # between replacements is fail-closed: global compilation rejects the
        # unpaired declaration, and rerunning this command repairs it idempotently.
        atomic_write(source_path, source_new)
        atomic_write(target_path, target_new)

    if args.phase == "cleanup":
        source_text = source_path.read_text(encoding="utf-8")
        target_text = target_path.read_text(encoding="utf-8")
        if args.app not in assigned_apps(target_text) or args.app in assigned_apps(source_text):
            parser.error("cleanup requires sole target assignment")
        if args.app in mapping_apps(source_text, "outgoing_migrations") or args.app in mapping_apps(target_text, "incoming_migrations"):
            parser.error("cleanup requires completed handoff inventory without migration intent")
        # The cleanup play validates the handoff contract against draining_apps.
        # Repair inventory left by the short-lived pre-play removal bug before
        # starting Ansible; final ownership is committed only after success.
        if args.app not in mapping_apps(source_text, "draining_apps"):
            target_address = args.target_address or inventory_address(
                repo_root / "inventories/production/hosts.yml", args.target
            )
            source_text = add_mapping_entry(
                source_text,
                "draining_apps",
                args.app,
                {"target_host": args.target, "target_address": target_address},
            )
            atomic_write(source_path, source_text)

    extra_vars = {
        "migration_app": args.app,
        "source_host": args.source,
        "target_host": args.target,
        "migration_phase": args.phase,
        "migration_operator_serialized": True,
        "migration_confirm_cutover": args.confirm_cutover,
        "migration_approve_target_prune": args.approve_target_prune,
        "migration_confirm_endpoint_retirement": args.confirm_endpoint_retirement,
        "migration_direct_port_policy": args.direct_port_policy,
    }
    command = [args.ansible_playbook, "automation/migrate_app_host.yml", "-e", json.dumps(extra_vars, separators=(",", ":"))]
    completed = subprocess.run(command, cwd=repo_root)
    if completed.returncode:
        message = (
            "Inventory remains in cleanup-ready final ownership for a safe retry."
            if args.phase == "cleanup"
            else "Migration intent remains in host vars for a safe retry."
        )
        print(message, file=sys.stderr)
        return completed.returncode

    if args.phase == "cleanup":
        source_text = source_path.read_text(encoding="utf-8")
        atomic_write(source_path, remove_mapping_entry(source_text, "draining_apps", args.app))
        print("Committed final target-only ownership; run a full deployment on both hosts.")

    if args.phase in ("all", "cutover", "handoff"):
        # Successful handoff establishes the target as sole authority. Commit
        # that topology immediately so a later ordinary deployment cannot
        # interpret the source as a writer.
        source_text = source_path.read_text(encoding="utf-8")
        target_text = target_path.read_text(encoding="utf-8")
        target_address = args.target_address or inventory_address(
            repo_root / "inventories/production/hosts.yml", args.target
        )
        source_new = remove_assignment(source_text, args.app)
        source_new = remove_mapping_entry(source_new, "outgoing_migrations", args.app)
        source_new = add_mapping_entry(
            source_new,
            "draining_apps",
            args.app,
            {"target_host": args.target, "target_address": target_address},
        )
        target_new = add_assignment(target_text, args.app)
        target_new = remove_mapping_entry(target_new, "incoming_migrations", args.app)
        atomic_write(source_path, source_new)
        atomic_write(target_path, target_new)
        print("Committed target ownership and source draining inventory after successful handoff.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
