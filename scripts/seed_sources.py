"""Seed the sources table with INSPIRE-HEP and EURAXESS source records."""

import os
import sys

import structlog
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

load_dotenv()

log = structlog.get_logger()

SOURCES = [
    dict(
        name="inspire",
        domain="inspirehep.net",
        endpoint_url="https://inspirehep.net/api/jobs",
        source_type="api",
        first_party=False,
        trust_tier=2,
        countries=[],
        languages=["en"],
        acquisition_decision="permitted",
        requests_per_second=2.0,
        enabled=True,
    ),
    dict(
        name="euraxess",
        domain="euraxess.ec.europa.eu",
        endpoint_url="https://euraxess.ec.europa.eu/jobs/search",
        source_type="scraper",
        first_party=False,
        trust_tier=2,
        countries=[],
        languages=["en"],
        acquisition_decision="permitted",
        requests_per_second=1.0,
        enabled=True,
    ),
]


def seed(database_url: str) -> None:
    """Insert source rows if they do not already exist."""
    from aoi.db.models import Source

    engine = create_engine(database_url)
    with Session(engine) as session:
        for source_def in SOURCES:
            existing = session.query(Source).filter_by(name=source_def["name"]).first()
            if existing is not None:
                log.info(
                    "seed_sources.already_exists",
                    name=source_def["name"],
                    id=str(existing.id),
                )
                continue

            source = Source(**source_def)
            session.add(source)
            try:
                session.flush()
                log.info(
                    "seed_sources.inserted",
                    name=source_def["name"],
                    id=str(source.id),
                )
            except IntegrityError:
                session.rollback()
                log.warning("seed_sources.conflict", name=source_def["name"])

        session.commit()


if __name__ == "__main__":
    url = os.environ.get("AOI_DATABASE_URL", "")
    if not url:
        print("AOI_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    seed(url)
