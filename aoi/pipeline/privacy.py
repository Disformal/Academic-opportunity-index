"""Contact data suppression for AOI — INSPIRE prohibits bulk email collection."""

import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

ROLE_PREFIXES = (
    "jobs",
    "job",
    "hr",
    "recruitment",
    "recruiting",
    "careers",
    "career",
    "admissions",
    "phd",
    "postdoc",
    "vacancies",
    "apply",
    "info",
    "contact",
    "personnel",
    "search",
    "committee",
)


def is_role_address(addr: str) -> bool:
    """True if the email appears to be a role/functional address, not personal."""
    local = addr.split("@", 1)[0].lower()
    return any(
        local == p or local.startswith(p + "-") or local.startswith(p + ".") for p in ROLE_PREFIXES
    )


def scrub(text: str | None) -> str | None:
    """Remove every email address that is not clearly a role account."""
    if not text:
        return text
    return EMAIL.sub(
        lambda m: m.group(0) if is_role_address(m.group(0)) else "[contact removed]",
        text,
    )
