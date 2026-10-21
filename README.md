# Academic Opportunity Index

A search index for academic opportunities (jobs, funding calls, fellowships and scholarships).

## Stack

Python 3.13 · PostgreSQL 16 · SeaweedFS (S3-compatible evidence store, WARC) ·
FastAPI + Jinja2 · [uv](https://docs.astral.sh/uv/) · structlog · Smokescreen egress proxy.

## Layout

| Path | Contents |
|---|---|
| `aoi/adapters/` | One adapter per source or job-board platform |
| `aoi/pipeline/` | Fetching (SSRF-checked), robots.txt, evidence storage |
| `aoi/tasks/` | Task queue, scheduler, worker and task handlers |
| `aoi/db/` | SQLAlchemy models (`core`, `ops` schemas) |
| `aoi/web/` | Web app and dashboard |
| `alembic/` | Database migrations |
| `ontology/` | Disciplines, closure policy and other controlled vocabularies |
| `grafana/` | Dashboards and alert rules |

## Development

```bash
cp .env.example .env
docker compose up -d                      # postgres, seaweedfs, smokescreen, grafana
uv sync
uv run alembic upgrade head
uv run pytest tests/ -q -m "not live"     # needs AOI_TEST_DATABASE_URL
./scripts/ci.sh                           # lint, format, types, migration round-trip, tests
uv run aoi --help                         # command-line interface
uv run python -m aoi.web                  # web app
```

Tests run against a PostgreSQL database; there is no SQLite or mocked database.

## License

[EUPL-1.2](LICENSE)
