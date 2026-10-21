"""Tests for contact data suppression (privacy.py)."""

from aoi.pipeline.privacy import is_role_address, scrub


def test_scrub_removes_personal_email() -> None:
    result = scrub("Contact a.researcher@uni.example for details.")
    assert "[contact removed]" in result
    assert "a.researcher@uni.example" not in result


def test_scrub_keeps_role_address_jobs() -> None:
    result = scrub("Apply at jobs@uni.example")
    assert "jobs@uni.example" in result
    assert "[contact removed]" not in result


def test_scrub_keeps_role_address_phd_admissions() -> None:
    result = scrub("Contact phd-admissions@university.edu for details.")
    assert "phd-admissions@university.edu" in result
    assert "[contact removed]" not in result


def test_scrub_keeps_role_address_hr() -> None:
    result = scrub("Send your CV to hr@cern.ch")
    assert "hr@cern.ch" in result
    assert "[contact removed]" not in result


def test_scrub_keeps_role_address_recruitment() -> None:
    result = scrub("Email recruitment@institute.org")
    assert "recruitment@institute.org" in result
    assert "[contact removed]" not in result


def test_scrub_keeps_role_address_careers() -> None:
    result = scrub("See careers@company.com for openings")
    assert "careers@company.com" in result
    assert "[contact removed]" not in result


def test_scrub_keeps_role_address_info() -> None:
    result = scrub("Contact info@lab.net")
    assert "info@lab.net" in result
    assert "[contact removed]" not in result


def test_scrub_removes_multiple_personal_emails() -> None:
    text = "Contact john.doe@example.com or jane.smith@research.org"
    result = scrub(text)
    assert result.count("[contact removed]") == 2
    assert "john.doe@example.com" not in result
    assert "jane.smith@research.org" not in result


def test_scrub_mixed_text_preserves_roles_removes_personal() -> None:
    text = "Send CV to jobs@lab.edu or email j.walker@lab.edu"
    result = scrub(text)
    assert "jobs@lab.edu" in result
    assert "j.walker@lab.edu" not in result
    assert "[contact removed]" in result


def test_scrub_none_returns_none() -> None:
    assert scrub(None) is None


def test_scrub_empty_string_returns_empty_string() -> None:
    assert scrub("") == ""


def test_scrub_no_emails_returns_unchanged() -> None:
    text = "No contact information here."
    assert scrub(text) == text


def test_is_role_address_exact_match() -> None:
    assert is_role_address("jobs@anywhere.edu") is True
    assert is_role_address("hr@anywhere.edu") is True
    assert is_role_address("careers@anywhere.edu") is True
    assert is_role_address("admissions@anywhere.edu") is True
    assert is_role_address("postdoc@anywhere.edu") is True
    assert is_role_address("vacancies@anywhere.edu") is True
    assert is_role_address("apply@anywhere.edu") is True
    assert is_role_address("info@anywhere.edu") is True
    assert is_role_address("contact@anywhere.edu") is True
    assert is_role_address("personnel@anywhere.edu") is True
    assert is_role_address("search@anywhere.edu") is True
    assert is_role_address("committee@anywhere.edu") is True
    assert is_role_address("recruiting@anywhere.edu") is True


def test_is_role_address_with_dash_prefix() -> None:
    assert is_role_address("jobs-2024@anywhere.edu") is True
    assert is_role_address("phd-applications@anywhere.edu") is True


def test_is_role_address_with_dot_prefix() -> None:
    assert is_role_address("jobs.physics@anywhere.edu") is True
    assert is_role_address("careers.science@anywhere.edu") is True


def test_is_role_address_personal_returns_false() -> None:
    assert is_role_address("john.doe@university.edu") is False
    assert is_role_address("a.researcher@institution.ac.uk") is False
    assert is_role_address("firstname.lastname@cern.ch") is False
