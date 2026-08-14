#!/bin/sh
set -eu

app_password="$(tr -d '\r\n' < /run/secrets/postgres_app_password)"
worker_password="$(tr -d '\r\n' < /run/secrets/postgres_worker_password)"
if [ -z "$app_password" ] || [ -z "$worker_password" ]; then
  echo "postgres runtime password secret is empty" >&2
  exit 1
fi

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$app_password" --set=worker_password="$worker_password" <<'SQL'
SELECT format('CREATE ROLE finscope_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finscope_app') \gexec
SELECT format('CREATE ROLE finscope_worker LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', :'worker_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finscope_worker') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
