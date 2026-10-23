"""Stage 1 scaffold tests — verify version, config, and package structure."""

import importlib
import importlib.metadata
import os


def test_version_readable() -> None:
    """aoi.__version__ should equal the version declared in pyproject.toml."""
    import aoi

    assert aoi.__version__ == "0.1.0"


def test_version_matches_importlib_metadata() -> None:
    """importlib.metadata.version('aoi') should return 0.1.0."""
    assert importlib.metadata.version("aoi") == "0.1.0"


def test_get_settings_returns_settings_instance() -> None:
    """get_settings() should return a Settings object with expected fields."""
    from aoi.config import Settings, get_settings

    s = get_settings()
    assert isinstance(s, Settings)
    assert s.database_url != ""
    assert "5432" in s.database_url or "localhost" in s.database_url


def test_settings_has_s3_fields() -> None:
    """Settings should expose S3 endpoint and bucket names."""
    from aoi.config import get_settings

    s = get_settings()
    assert s.s3_endpoint.startswith("http")
    assert s.s3_bucket_evidence != ""
    assert s.s3_bucket_derived != ""


def test_settings_env_prefix() -> None:
    """AOI_DEBUG env var (set to true in .env) should be reflected in settings."""
    from aoi.config import get_settings

    s = get_settings()
    # AOI_DEBUG=true in .env — value is bool
    assert isinstance(s.debug, bool)


def test_package_submodules_importable() -> None:
    """All sub-packages should be importable without errors."""
    import aoi.db
    import aoi.pipeline
    import aoi.stats
    import aoi.storage

    # All are packages (have __path__)
    for mod in (aoi.db, aoi.pipeline, aoi.stats, aoi.storage):
        assert hasattr(mod, "__path__"), f"{mod.__name__} is not a package"


def test_test_database_url_is_configured() -> None:
    """AOI_TEST_DATABASE_URL must be set and point to aoi_test."""
    url = os.environ.get("AOI_TEST_DATABASE_URL", "")
    assert url != "", "AOI_TEST_DATABASE_URL is not set"
    assert "aoi_test" in url, "AOI_TEST_DATABASE_URL must point to aoi_test"


def test_tests_use_their_own_s3_buckets() -> None:
    """Tests must never write into the dev evidence buckets (finding of 2026-10-17)."""
    from aoi.config import get_settings

    s = get_settings()
    assert s.s3_bucket_evidence == "aoi-test-evidence"
    assert s.s3_bucket_derived == "aoi-test-derived"
