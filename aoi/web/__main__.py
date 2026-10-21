"""Entry point: ``python -m aoi.web.app``."""

import uvicorn

from aoi.config import get_settings
from aoi.web.app import create_app

settings = get_settings()
app = create_app()
uvicorn.run(app, host=settings.web_host, port=settings.web_port)
