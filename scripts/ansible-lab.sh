#!/usr/bin/env bash
set -euo pipefail

### =========================
### Configuration
### =========================

VM_NAME="ansible-lab"
BASE_DIR="/tank/data/vms/${VM_NAME}"

BASE_IMG="${BASE_DIR}/debian-14-genericcloud-amd64-daily.qcow2"
DISK_IMG="${BASE_DIR}/${VM_NAME}.qcow2"

USER_DATA="${BASE_DIR}/user-data"
META_DATA="${BASE_DIR}/meta-data"

OS_VARIANT="debian13"
NET_NAME="default"
MAC_ADDR="52:54:00:ab:cd:ef"

MEMORY=4096
VCPUS=2
DISK_SIZE=20G

DEBUG_PASSWORD=""

### =========================
### Helpers
### =========================

vm_exists() {
  virsh -c qemu:///system dominfo "${VM_NAME}" >/dev/null 2>&1
}

vm_running() {
  virsh -c qemu:///system domstate "${VM_NAME}" 2>/dev/null | grep -q running
}

disk_exists() {
  [[ -f "${DISK_IMG}" ]]
}

ensure_dirs() {
  mkdir -p "${BASE_DIR}"
}

ensure_network() {
  virsh -c qemu:///system net-info "${NET_NAME}" >/dev/null 2>&1 ||
    {
      echo "Libvirt network '${NET_NAME}' not found"
      exit 1
    }

  if virsh -c qemu:///system net-info "${NET_NAME}" | grep -q "Active:.*no"; then
    virsh -c qemu:///system net-start "${NET_NAME}" >/dev/null
  fi
}

fetch_base_image() {
  if [[ ! -f "${BASE_IMG}" ]]; then
    echo "Downloading Debian Sid (unstable) cloud image..."
    curl -L -o "${BASE_IMG}" \
      https://cloud.debian.org/images/cloud/forky/daily/latest/debian-14-genericcloud-amd64-daily.qcow2
  fi
}

detect_pubkey() {
  local key=""
  key=$(ssh-add -L 2>/dev/null | head -n1 || true)

  if [[ -z "${key}" ]]; then
    for f in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
      [[ -f "${f}" ]] && key=$(<"${f}") && break
    done
  fi

  [[ -z "${key}" ]] && {
    echo "No SSH public key found" >&2
    exit 1
  }

  echo "${key}"
}

### =========================
### Cloud-init (DATA ONLY)
### =========================

write_cloud_init() {
  local pubkey="$1"
  local password="$2"

  # user-data
  cat >"${USER_DATA}" <<EOF
#cloud-config
users:
  - default
  - name: podman
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ${pubkey}
    lock_passwd: false

ssh_pwauth: true
disable_root: false

chpasswd:
  expire: false
  list:
    - podman:${password:-podman}
    - root:${password:-root}

console: ttyS0
EOF

  # meta-data
  cat >"${META_DATA}" <<EOF
instance-id: ${VM_NAME}
local-hostname: ${VM_NAME}
EOF
}

### =========================
### Lifecycle Verbs
### =========================

create() {
  if vm_exists; then
    echo "VM already exists" >&2
    exit 1
  fi

  ensure_dirs
  ensure_network
  fetch_base_image

  local pubkey
  pubkey=$(detect_pubkey)

  write_cloud_init "${pubkey}" "${DEBUG_PASSWORD}"

  qemu-img create -f qcow2 -b "${BASE_IMG}" -F qcow2 "${DISK_IMG}" "${DISK_SIZE}"

  virt-install \
    --connect qemu:///system \
    --name "${VM_NAME}" \
    --memory "${MEMORY}" \
    --vcpus "${VCPUS}" \
    --os-variant "${OS_VARIANT}" \
    --boot uefi \
    --import \
    --disk path="${DISK_IMG}",format=qcow2 \
    --network network="${NET_NAME}",model=virtio,mac="${MAC_ADDR}" \
    --graphics none \
    --noautoconsole \
    --cloud-init "user-data=${USER_DATA},meta-data=${META_DATA}"
}

up() {
  if ! vm_exists; then
    echo "VM does not exist; run create first" >&2
    exit 1
  fi

  if ! disk_exists; then
    echo "Disk missing; cannot start VM" >&2
    exit 1
  fi

  if vm_running; then
    echo "VM already running"
    return
  fi

  virsh -c qemu:///system start "${VM_NAME}"
}

down() {
  if vm_running; then
    virsh -c qemu:///system destroy "${VM_NAME}"
  fi
}

delete() {
  down

  if vm_exists; then
    virsh -c qemu:///system undefine "${VM_NAME}" --nvram
  fi

  rm -f "${DISK_IMG}" "${USER_DATA}" "${META_DATA}"
}

reset() {
  down
  delete
  create
  up
}

### =========================
### CLI
### =========================

ACTION="${1:-}"
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
  --password)
    DEBUG_PASSWORD="$2"
    shift 2
    ;;
  *)
    echo "Unknown option: $1" >&2
    exit 1
    ;;
  esac
done

case "${ACTION}" in
create) create ;;
up) up ;;
down) down ;;
delete) delete ;;
reset) reset ;;
*)
  echo "Usage: $0 {create|up|down|delete|reset} [--password <pw>]" >&2
  exit 1
  ;;
esac
