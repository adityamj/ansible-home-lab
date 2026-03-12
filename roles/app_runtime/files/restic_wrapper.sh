#!/usr/bin/env bash
set -euo pipefail

CMD="${1:-}"
shift || true

if [[ -z "${CMD}" ]]; then
  echo "Usage: $0 <backup|restore> <path>" >&2
  exit 1
fi

TARGET_PATH="${1:-}"
if [[ -z "${TARGET_PATH}" ]]; then
  echo "Target path is required" >&2
  exit 1
fi

RESTIC_IMAGE="${RESTIC_IMAGE:-docker.io/restic/restic:latest}"
ENV_FILE="${RESTIC_ENV_FILE:-$HOME/.restic-env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Restic env file not found: $ENV_FILE" >&2
  exit 1
fi

case "$CMD" in
  backup)
    podman run --rm \
      --env-file "$ENV_FILE" \
      -v "$TARGET_PATH:/data:Z" \
      "$RESTIC_IMAGE" \
      backup /data
    ;;
  restore)
    podman run --rm \
      --env-file "$ENV_FILE" \
      -v "$TARGET_PATH:/data:Z" \
      "$RESTIC_IMAGE" \
      restore latest --target /data
    ;;
  *)
    echo "Unknown command: $CMD (expected backup|restore)" >&2
    exit 1
    ;;
esac
