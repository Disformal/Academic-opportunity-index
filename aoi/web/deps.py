"""FastAPI dependency injection and shared middleware."""

from collections.abc import Iterator

from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from aoi.db.session import get_session_factory

# Shared rate limiter — used by app.py and dashboard.py
limiter = Limiter(key_func=get_remote_address)


def get_db() -> Iterator[Session]:
    """Yield a SQLAlchemy session that commits on success, rolls back on error."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
