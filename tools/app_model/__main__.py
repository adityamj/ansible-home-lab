"""Command-line interface for the application model compiler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .compiler import ModelError, compile_plan


def _input() -> dict[str, Any]:
    payload = sys.stdin.read()
    if not payload.strip():
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ModelError(f"input: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ModelError("input: must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile application declarations for one host")
    parser.add_argument("--apps-root", required=True, type=Path)
    parser.add_argument("--assigned-app", action="append", dest="assigned_apps")
    parser.add_argument("--target-app", default="")
    parser.add_argument("--ssh-port", default=22, type=int)
    parser.add_argument("--caddy-platform-network", default="caddy-egress-net")
    parser.add_argument("--check", action="store_true", help="validate without printing the plan")
    args = parser.parse_args(argv)
    try:
        request = _input()
        assigned_apps = request.get("assigned_apps", args.assigned_apps)
        if assigned_apps is None:
            assigned_apps = sorted(path.parent.name for path in args.apps_root.glob("*/app.yml"))
        plan = compile_plan(
            args.apps_root,
            assigned_apps=assigned_apps,
            target_app=request.get("target_app", args.target_app),
            ssh_port=request.get("ssh_port", args.ssh_port),
            caddy_platform_network=request.get("caddy_platform_network", args.caddy_platform_network),
            defaults=request.get("defaults"),
            app_desired_states=request.get("app_desired_states"),
            draining_apps=request.get("draining_apps"),
            inventory_hosts=request.get("inventory_hosts"),
            current_host=request.get("current_host", ""),
            selected_hosts=request.get("selected_hosts"),
        )
    except ModelError as error:
        print(f"app model error: {error}", file=sys.stderr)
        return 2
    if not args.check:
        json.dump(plan, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
