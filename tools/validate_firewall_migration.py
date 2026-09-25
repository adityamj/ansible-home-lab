#!/usr/bin/env python3
"""Validate the two supported managed policies; emit unchanged, validated ports.

Controller-only parsing. Ansible owns backup, rendering and all host mutation.
Comments/blank lines are immaterial, but every executable statement must match
our historical/current template. No arbitrary nft expressions are imported.
"""
import json
import re
import sys


def validate(policy: str, ssh_port: int) -> dict:
    lines = [line.strip() for line in policy.splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    old = ['add table inet ansible_host_filter', 'delete table inet ansible_host_filter']
    if lines[:2] == old:
        lines = lines[2:]
    elif lines[:1] == ['flush ruleset']:
        lines = lines[1:]
    else:
        raise ValueError('Unsupported firewall ownership framing')
    # Older deployments attached a generation hash to the input chain. Accept
    # exactly that metadata in its original position, including on interrupted
    # migrations whose ownership framing has already been converted. The new
    # renderer omits it; it has no effect on packet filtering.
    if len(lines) > 2 and re.fullmatch(r'comment "ansible-generation=[0-9a-f]{64}"', lines[2]):
        lines = lines[:2] + lines[3:]
    prefix = [
        'table inet ansible_host_filter {',
        'chain input {',
        'type filter hook input priority 0; policy drop;',
        'iif "lo" accept',
        'ct state established,related accept',
        'ct state invalid drop',
        'ip protocol icmp accept',
        'ip6 nexthdr icmpv6 accept',
    ]
    if len(lines) != len(prefix) + 4 or lines[:len(prefix)] != prefix or lines[-2:] != ['}', '}']:
        raise ValueError('Unsupported firewall rules/order; manual review required')
    ports = {}
    for protocol, line in zip(('tcp', 'udp'), lines[len(prefix):-2]):
        match = re.fullmatch(rf'{protocol} dport \{{ ([0-9]+(?:, [0-9]+)*) \}} ct state new accept', line)
        if not match:
            raise ValueError(f'Unsupported {protocol} allowance')
        values = [int(value) for value in match[1].split(', ')]
        if any(not 1 <= port <= 65535 for port in values):
            raise ValueError('Port outside 1..65535')
        ports[protocol] = sorted(set(values))
    if not 1 <= ssh_port <= 65535 or ssh_port not in ports['tcp']:
        raise ValueError('Preserved policy must allow the configured SSH port')
    # The shared renderer always includes these platform ports. Reject a policy
    # lacking them rather than silently expanding its allowances on migration.
    if not {80, 443}.issubset(ports['tcp']) or 443 not in ports['udp']:
        raise ValueError('Preserved policy must contain standard ingress allowances')
    return {'ports': [{'protocol': protocol, 'host_port': port}
                      for protocol in ('tcp', 'udp') for port in ports[protocol]]}


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        print(json.dumps(validate(request['policy'], int(request['ssh_port']))))
    except (ValueError, KeyError, TypeError) as error:
        sys.exit(str(error))
