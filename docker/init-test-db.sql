-- init-test-db.sql
-- Runs once when the PostgreSQL container is first created (via the
-- docker-entrypoint-initdb.d mechanism). Creates the test database
-- alongside the main 'aoi' database.
--
-- To re-run this (e.g. after wiping volumes):
--   docker compose down -v && docker compose up -d

CREATE DATABASE aoi_test OWNER aoi;

-- Grant full access to the aoi user on the test database
\connect aoi_test
GRANT ALL PRIVILEGES ON SCHEMA public TO aoi;
