#!/bin/sh
set -eu
# Values travel through psql variables and SQL identifier/literal quoting.
# Never enable shell tracing: this script handles credentials.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=migration_password="$MIGRATION_DB_PASSWORD" --set=app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE codepilot_migrator LOGIN PASSWORD :'migration_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE codepilot LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO codepilot_migrator;
GRANT USAGE ON SCHEMA public TO codepilot;
ALTER DEFAULT PRIVILEGES FOR ROLE codepilot_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO codepilot;
ALTER DEFAULT PRIVILEGES FOR ROLE codepilot_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO codepilot;
SQL
