#!/usr/bin/env python3
"""Inject an opaque, file-backed migration readiness endpoint into Caddy sites."""

from __future__ import annotations

import argparse
import re
import sys

DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
TOKEN_RE = re.compile(r"^[a-f0-9]{32,128}$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domains", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    domains = [item.strip() for item in args.domains.split(",") if item.strip()]
    if not domains or len(domains) != len(set(domains)) or any(not DOMAIN_RE.fullmatch(item) for item in domains):
        parser.error("--domains must be a unique comma-separated DNS-name list")
    if not TOKEN_RE.fullmatch(args.token):
        parser.error("--token must contain 32-128 lowercase hexadecimal characters")

    source = sys.stdin.read()
    marker = f"# Migration readiness gate {args.token}"
    if marker in source:
        sys.stdout.write(source)
        return 0
    if source.count("{") != source.count("}"):
        print("site is not balanced", file=sys.stderr)
        return 2

    labels = {domain for domain in domains}
    output: list[str] = []
    injected: set[str] = set()
    for line in source.splitlines(keepends=True):
        output.append(line)
        stripped = line.strip()
        if not stripped.endswith("{"):
            continue
        site_labels = {item.strip() for item in stripped[:-1].split(",")}
        matched = site_labels & labels
        if not matched:
            continue
        indent = line[: len(line) - len(line.lstrip())] + "    "
        output.extend(
            [
                f"{indent}{marker}\n",
                f"{indent}handle /.__migration_ready_{args.token} {{\n",
                f"{indent}    root * /etc/caddy/migration-gates\n",
                f"{indent}    rewrite * /{args.token}\n",
                f"{indent}    file_server\n",
                f"{indent}}}\n",
            ]
        )
        injected.update(matched)

    if injected != labels:
        missing = ", ".join(sorted(labels - injected))
        print(f"site does not contain expected domain blocks: {missing}", file=sys.stderr)
        return 2
    sys.stdout.write("".join(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
