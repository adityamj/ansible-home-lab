#!/usr/bin/env python3
"""Append an explicit HTTP-only ACME relay beside a generated HTTPS site."""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys

MARKER = "# BEGIN managed migration HTTP-01 relay v3"
LEGACY_MARKER = "# BEGIN managed migration HTTP-01 relay"
END_MARKER = "# END managed migration HTTP-01 relay"
DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def _remove_legacy_relay(lines: list[str]) -> list[str]:
    start = next((index for index, line in enumerate(lines) if LEGACY_MARKER in line), None)
    if start is None:
        return lines
    legacy_v2 = "relay v2" in lines[start]
    end = next((index for index in range(start, len(lines)) if END_MARKER in lines[index]), None)
    if end is None:
        raise ValueError("incomplete legacy migration relay")
    del lines[start : end + 1]

    if not legacy_v2:
        return lines

    # v2 placed the original application body inside a fallback handle. Remove
    # that wrapper without interpreting any application-owned directives.
    if start >= len(lines) or lines[start].strip() != "handle {":
        raise ValueError("legacy v2 fallback handle is missing")
    del lines[start]
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if len(nonempty) < 2 or lines[nonempty[-1]].strip() != "}" or lines[nonempty[-2]].strip() != "}":
        raise ValueError("legacy v2 fallback closing brace is missing")
    del lines[nonempty[-2]]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--domains", required=True)
    args = parser.parse_args()

    target = str(ipaddress.ip_address(args.target))
    domains = [item.strip() for item in args.domains.split(",") if item.strip()]
    if not domains or len(domains) != len(set(domains)) or any(not DOMAIN_RE.fullmatch(item) for item in domains):
        parser.error("--domains must be a unique comma-separated DNS-name list")

    source = sys.stdin.read()
    if MARKER in source:
        sys.stdout.write(source)
        return 0

    try:
        lines = _remove_legacy_relay(source.splitlines(keepends=True))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    source = "".join(lines)
    if source.count("{") != source.count("}"):
        print("site is not balanced after legacy relay removal", file=sys.stderr)
        return 2
    if source and not source.endswith("\n"):
        source += "\n"

    labels = ", ".join(f"http://{domain}" for domain in domains)
    relay = (
        f"\n{MARKER}\n"
        f"{labels} {{\n"
        "    handle /.well-known/acme-challenge/* {\n"
        f"        reverse_proxy http://{target}:80\n"
        "    }\n"
        "    handle {\n"
        "        redir https://{host}{uri} 308\n"
        "    }\n"
        "}\n"
        f"{END_MARKER} v3\n"
    )
    sys.stdout.write(source + relay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
