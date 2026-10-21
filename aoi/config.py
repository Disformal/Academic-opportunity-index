"""AOI configuration via Pydantic Settings."""

import sys

from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET_KEY = "changeme-in-production-use-a-long-random-string"


class Settings(BaseSettings):
    """All AOI configuration, loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_prefix="AOI_", env_file=".env")

    # Database
    database_url: str
    test_database_url: str = ""

    # S3
    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket_evidence: str = "aoi-evidence"
    s3_bucket_derived: str = "aoi-derived"

    # Application
    secret_key: str
    debug: bool = True
    log_level: str = "INFO"

    def validate_production_secret(self) -> None:
        """Refuse to start in production mode with the default secret key."""
        if not self.debug and self.secret_key == _INSECURE_SECRET_KEY:
            sys.exit(
                "FATAL: AOI_SECRET_KEY is set to the default placeholder value. "
                "Generate a real key with: "
                'python -c "import secrets; print(secrets.token_urlsafe(64))" '
                "and set it in .env before running with AOI_DEBUG=false."
            )

    # Crawl settings
    rate_limit_rps: float = 1.0
    early_stop_consecutive: int = 10

    # Web server
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # HTTP client settings
    user_agent: str = (
        "AOI/0.1 (+https://github.com/Disformal/Academic-opportunity-index;"
        " academic-opportunity-index)"
    )
    http_timeout_seconds: float = 30.0
    max_body_bytes: int = 10_485_760  # 10 MB
    # Smokescreen egress proxy (ADR 0023); required when debug is false
    egress_proxy: str = ""


def get_settings() -> Settings:
    """Return a fresh Settings instance loaded from the environment."""
    return Settings()
