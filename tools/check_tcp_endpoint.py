#!/usr/bin/env python3
"""Fail unless a TCP endpoint accepts a connection."""

from __future__ import annotations

import argparse
import socket


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("port", type=int)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    with socket.create_connection((args.address, args.port), timeout=args.timeout):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
