#!/bin/bash
set -euo pipefail

usage() {
    echo "usage: ${0##*/} SOURCE_HOST TARGET_HOST --confirm-endpoint-retirement" >&2
    exit 64
}

[[ $# -eq 3 ]] || usage
source_host=$1
target_host=$2
[[ $3 == --confirm-endpoint-retirement ]] || usage
[[ $source_host =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || usage
[[ $target_host =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || usage
[[ $source_host != "$target_host" ]] || usage

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_root"
source_vars="inventories/production/host_vars/${source_host}.yml"
target_vars="inventories/production/host_vars/${target_host}.yml"
[[ -f $source_vars && -f $target_vars ]] || {
    echo "host variable files for source and target must exist" >&2
    exit 1
}

mapfile -t migrated_apps < <(
    awk '
        $0 == "draining_apps:" { draining = 1; next }
        draining && /^[^[:space:]#]/ { exit }
        draining && /^  [a-z0-9][a-z0-9-]*:$/ {
            app = $0
            sub(/^  /, "", app)
            sub(/:$/, "", app)
            print app
        }
    ' "$source_vars"
)

((${#migrated_apps[@]} > 0)) || {
    echo "no draining migrated applications found on $source_host" >&2
    exit 1
}

cleanup_vault_file=
if [[ -z ${ANSIBLE_VAULT_PASSWORD_FILE:-} ]]; then
    umask 077
    cleanup_vault_file=$(mktemp)
    trap 'rm -f "$cleanup_vault_file"' EXIT
    read -r -s -p "Ansible Vault password: " vault_password
    echo
    printf '%s\n' "$vault_password" >"$cleanup_vault_file"
    unset vault_password
    export ANSIBLE_VAULT_PASSWORD_FILE=$cleanup_vault_file
fi
export ANSIBLE_ASK_VAULT_PASS=false

echo "Finalizing migrations from $source_host to $target_host: ${migrated_apps[*]}"
for app in "${migrated_apps[@]}"; do
    echo "==> Cleaning up $app"
    automation/migrate_app_host.py \
        --app "$app" \
        --source "$source_host" \
        --target "$target_host" \
        --phase cleanup \
        --confirm-endpoint-retirement
done

echo "==> Running final full deployment on $source_host and $target_host"
ansible-playbook automation/deploy.yml --limit "$source_host,$target_host"
