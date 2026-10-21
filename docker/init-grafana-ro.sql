-- Read-only PostgreSQL role for Grafana.
-- Grafana must never be able to modify data — this is a security boundary.
-- Mounted as a docker-entrypoint-initdb.d script so it runs on first container start.

CREATE USER aoi_grafana_ro WITH PASSWORD 'grafana_readonly';
GRANT CONNECT ON DATABASE aoi TO aoi_grafana_ro;
GRANT USAGE ON SCHEMA public TO aoi_grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aoi_grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO aoi_grafana_ro;
