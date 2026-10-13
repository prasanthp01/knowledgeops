#!/bin/sh
set -eu
test "$POSTGRES_DB" = knowledgeops_test
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
  --set=owner_password="$OWNER_PASSWORD" --set=app_password="$APP_PASSWORD" <<'SQL'
CREATE ROLE knowledgeops_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'owner_password';
CREATE ROLE knowledgeops_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER DATABASE knowledgeops_test OWNER TO knowledgeops_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO knowledgeops_owner;
SQL
