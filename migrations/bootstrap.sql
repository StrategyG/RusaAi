\set ON_ERROR_STOP on
\getenv app_password RUSA_APP_DB_PASSWORD
\i /migrations/001_tenant_records_rls.sql
ALTER ROLE rusa_app PASSWORD :'app_password';
