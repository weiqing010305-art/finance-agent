#!/bin/sh
set -eu

action="${1:-}"
path="${2:-}"
inventory="${3:-}"
bucket="${4:-}"
root_access="$(tr -d '\r\n' < /run/secrets/minio_root_access_key)"
root_secret="$(tr -d '\r\n' < /run/secrets/minio_root_secret_key)"
mc alias set local http://minio:9000 "$root_access" "$root_secret" >/dev/null

case "$action" in
  collect)
    [ -f "$inventory" ] || { echo "object inventory missing" >&2; exit 2; }
    mkdir -p "$path"
    while IFS="$(printf '\t')" read -r key expected_size expected_sha; do
      [ -n "$key" ] || continue
      case "$key" in /*|*../*|../*|*..|*[!A-Za-z0-9._/-]*) echo "unsafe object key" >&2; exit 2 ;; esac
      case "$expected_size:$expected_sha" in *[!0-9:abcdef]*) echo "invalid object inventory" >&2; exit 2 ;; esac
      target="$path/$key"
      mkdir -p "$(dirname "$target")"
      mc cp --quiet "local/finscope-private/$key" "$target"
      actual_size="$(wc -c < "$target" | tr -d ' ')"
      checksum_line="$(sha256sum "$target")"
      actual_sha="${checksum_line%% *}"
      [ "$actual_size" = "$expected_size" ] && [ "$actual_sha" = "$expected_sha" ] || {
        echo "object identity mismatch: $key" >&2; exit 1;
      }
    done < "$inventory"
    ;;
  drill)
    case "$bucket" in finscope-restore-[a-f0-9]*) ;; *) echo "invalid restore bucket" >&2; exit 2 ;; esac
    mc mb "local/$bucket"
    trap 'mc rb --force "local/$bucket" >/dev/null 2>&1 || true' EXIT
    mc mirror --overwrite "$path" "local/$bucket"
    while IFS="$(printf '\t')" read -r key expected_size expected_sha; do
      [ -n "$key" ] || continue
      verify_path="/tmp/finscope-restore-verify"
      rm -f "$verify_path"
      mc cp --quiet "local/$bucket/$key" "$verify_path"
      checksum_line="$(sha256sum "$verify_path")"
      actual_sha="${checksum_line%% *}"
      actual_size="$(wc -c < "$verify_path" | tr -d ' ')"
      [ "$actual_size" = "$expected_size" ] && [ "$actual_sha" = "$expected_sha" ] || {
        echo "restored object identity mismatch: $key" >&2; exit 1;
      }
    done < "$inventory"
    ;;
  *)
    echo "usage: minio_backup.sh collect PATH INVENTORY | drill PATH INVENTORY BUCKET" >&2
    exit 2
    ;;
esac
