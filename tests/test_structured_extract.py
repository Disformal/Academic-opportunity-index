"""Tests for aoi/pipeline/structured_extract.py.

These are unit tests — no database, no network.  They run trafilatura and
extruct against in-memory HTML strings.
"""

import json

from aoi.pipeline.structured_extract import extract_json_ld_job, extract_main_text

# ---------------------------------------------------------------------------
# Fixtures: minimal HTML documents
# ---------------------------------------------------------------------------

_JOB_JSON_LD = json.dumps(
    {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Postdoctoral Researcher in High Energy Physics",
        "validThrough": "2026-12-31",
        "hiringOrganization": {"@type": "Organization", "name": "CERN"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressCountry": "CH"},
        },
        "description": "Join our team to work on LHC experiments.",
        "url": "https://jobs.cern.ch/job/hep-postdoc",
    }
)

_HTML_WITH_JOB_POSTING = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Postdoc at CERN</title>
  <script type="application/ld+json">
  {_JOB_JSON_LD}
  </script>
</head>
<body>
  <nav>Home | Jobs | About</nav>
  <header>Site Header</header>
  <main>
    <h1>Postdoctoral Researcher in High Energy Physics</h1>
    <p>Join our team to work on LHC experiments.</p>
    <p>The position is based at CERN in Switzerland.</p>
    <p>Deadline: 31 December 2026.</p>
  </main>
  <footer>© 2026 CERN. Cookie preferences. Privacy policy.</footer>
</body>
</html>
"""

_HTML_WITHOUT_JOB_POSTING = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Company Blog</title>
  <script type="application/ld+json">
  {"@context": "https://schema.org", "@type": "BlogPosting", "headline": "News"}
  </script>
</head>
<body>
  <main><p>This is not a job posting.</p></main>
</body>
</html>
"""

_HTML_WITHOUT_JSON_LD = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Job</title></head>
<body>
  <nav>Site navigation</nav>
  <main>
    <h1>Research Associate</h1>
    <p>We are looking for a talented researcher to join our team.</p>
    <p>The candidate must have a PhD in physics.</p>
  </main>
  <footer>Cookie preferences | Privacy | Contact</footer>
</body>
</html>
"""

_HTML_NOISY = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Job</title></head>
<body>
  <nav>EURAXESS home | Jobs | Search | My Profile | Log in</nav>
  <div class="cookie-banner">Cookie preferences | Accept | Reject</div>
  <main>
    <article>
      <h1>Researcher in Machine Learning</h1>
      <p>We are seeking a researcher with expertise in deep learning.</p>
      <p>The role involves designing and implementing neural network models.</p>
      <p>Deadline for applications: 15 November 2026.</p>
    </article>
  </main>
  <footer>© European Commission | Cookie preferences | Accessibility</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# extract_json_ld_job tests
# ---------------------------------------------------------------------------


def test_extract_json_ld_job_returns_dict_for_job_posting() -> None:
    """extract_json_ld_job returns a dict when HTML contains schema.org/JobPosting."""
    result = extract_json_ld_job(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert result is not None


def test_extract_json_ld_job_maps_title() -> None:
    result = extract_json_ld_job(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert result is not None
    assert result.get("title") == "Postdoctoral Researcher in High Energy Physics"


def test_extract_json_ld_job_maps_deadline() -> None:
    result = extract_json_ld_job(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert result is not None
    assert result.get("deadline") == "2026-12-31"


def test_extract_json_ld_job_maps_organization() -> None:
    result = extract_json_ld_job(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert result is not None
    assert result.get("organization") == "CERN"


def test_extract_json_ld_job_maps_country() -> None:
    result = extract_json_ld_job(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert result is not None
    assert result.get("country") == "CH"


def test_extract_json_ld_job_maps_application_url() -> None:
    result = extract_json_ld_job(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert result is not None
    assert result.get("application_url") == "https://jobs.cern.ch/job/hep-postdoc"


def test_extract_json_ld_job_returns_none_for_non_job_posting() -> None:
    """extract_json_ld_job returns None when no JobPosting is in the JSON-LD."""
    result = extract_json_ld_job(_HTML_WITHOUT_JOB_POSTING, "https://example.com/blog/1")
    assert result is None


def test_extract_json_ld_job_returns_none_when_no_json_ld_present() -> None:
    """extract_json_ld_job returns None when HTML has no JSON-LD at all."""
    result = extract_json_ld_job(_HTML_WITHOUT_JSON_LD, "https://example.com/job/1")
    assert result is None


def test_extract_json_ld_job_omits_missing_optional_fields() -> None:
    """extract_json_ld_job does not include keys for absent optional fields."""
    minimal = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Research Assistant",
        }
    )
    html = (
        f'<html><head><script type="application/ld+json">{minimal}</script>'
        f"</head><body></body></html>"
    )
    result = extract_json_ld_job(html, "https://example.com/job/min")
    assert result is not None
    assert result.get("title") == "Research Assistant"
    # Optional fields not present in the JSON-LD should not appear in the result
    assert "deadline" not in result
    assert "organization" not in result
    assert "country" not in result


# ---------------------------------------------------------------------------
# extract_main_text tests
# ---------------------------------------------------------------------------


def test_extract_main_text_returns_non_empty_string() -> None:
    """extract_main_text returns a non-empty string for valid HTML."""
    text = extract_main_text(_HTML_WITH_JOB_POSTING, "https://jobs.cern.ch/job/1")
    assert isinstance(text, str)
    assert len(text) > 0


def test_extract_main_text_contains_article_content() -> None:
    """extract_main_text includes the main article text."""
    text = extract_main_text(_HTML_NOISY, "https://euraxess.ec.europa.eu/jobs/1")
    assert (
        "deep learning" in text.lower()
        or "neural network" in text.lower()
        or "machine learning" in text.lower()
    )


def test_extract_main_text_excludes_cookie_banner_text() -> None:
    """extract_main_text does not include 'Cookie preferences' from banners/footers."""
    text = extract_main_text(_HTML_NOISY, "https://euraxess.ec.europa.eu/jobs/1")
    # trafilatura should strip the cookie banner and footer
    assert "Cookie preferences" not in text


def test_extract_main_text_falls_back_to_body_text_for_minimal_html() -> None:
    """extract_main_text falls back to body text if trafilatura returns None."""
    minimal = "<html><body><p>Just a simple page.</p></body></html>"
    text = extract_main_text(minimal, "https://example.com/simple")
    assert "simple" in text.lower()


def test_extract_main_text_returns_string_on_empty_html() -> None:
    """extract_main_text returns a string (possibly empty) even for empty/broken HTML."""
    text = extract_main_text("", "https://example.com/empty")
    assert isinstance(text, str)
