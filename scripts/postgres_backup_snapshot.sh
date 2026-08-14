#!/bin/sh
set -eu

dump_path="${1:-}"
inventory_path="${2:-}"
schema_path="${3:-}"
case "$dump_path:$inventory_path:$schema_path" in
  /tmp/*:/tmp/*:/tmp/*) ;;
  *) echo "backup outputs must be under /tmp" >&2; exit 2 ;;
esac

snapshot_file="/tmp/finscope-snapshot-$$"
rm -f "$dump_path" "$inventory_path" "$schema_path" "$snapshot_file"

psql -X -q -A -t -v ON_ERROR_STOP=1 -U finscope_admin -d finscope >"$snapshot_file" <<'SQL' &
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT pg_export_snapshot();
SELECT pg_sleep(300);
ROLLBACK;
SQL
holder_pid=$!
cleanup() {
  kill "$holder_pid" >/dev/null 2>&1 || true
  wait "$holder_pid" >/dev/null 2>&1 || true
  rm -f "$snapshot_file"
}
trap cleanup EXIT

attempt=0
while [ ! -s "$snapshot_file" ]; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 100 ] || { echo "snapshot export timed out" >&2; exit 1; }
  sleep 0.1
done
snapshot_id="$(head -n 1 "$snapshot_file" | tr -d '\r\n')"
[ -n "$snapshot_id" ] || { echo "snapshot id is empty" >&2; exit 1; }

pg_dump -U finscope_admin -d finscope -Fc --snapshot="$snapshot_id" -f "$dump_path"
psql -X -q -v ON_ERROR_STOP=1 -U finscope_admin -d finscope <<SQL >"$inventory_path"
BEGIN ISOLATION LEVEL REPEATABLE READ;
SET TRANSACTION SNAPSHOT '$snapshot_id';
COPY (
  SELECT object_key, verified_size, sha256
  FROM objects
  WHERE status = 'ready'
  ORDER BY object_key
) TO STDOUT WITH (FORMAT text, DELIMITER E'\t');
COMMIT;
SQL
psql -X -q -A -t -v ON_ERROR_STOP=1 -U finscope_admin -d finscope <<SQL >"$schema_path"
BEGIN ISOLATION LEVEL REPEATABLE READ;
SET TRANSACTION SNAPSHOT '$snapshot_id';
SELECT version_num FROM alembic_version;
COMMIT;
SQL

[ -s "$dump_path" ] || { echo "pg_dump output is empty" >&2; exit 1; }
[ -s "$schema_path" ] || { echo "schema revision output is empty" >&2; exit 1; }
