"""Identity resolution for AOI opportunities.

Determines whether a newly-extracted opportunity is an existing entity
or a new one. Resolution follows a strict hierarchy:

    native_ref > application_url > detail_url > fingerprint

The first available identifier wins. Two items from different sources that
share the same application_url are recognised as the same entity.
"""

import hashlib
import re
from dataclasses import dataclass

import structlog

from aoi.db.enums import IdentityKind

log = structlog.get_logger()

_WHITESPACE = re.compile(r"\s+")

# Prefixes stripped before fuzzy matching. Order matters: longer patterns first.
_TITLE_NOISE = re.compile(
    r"^(?:postdoctoral\s+researcher\s+in\s+"
    r"|postdoctoral\s+research\s+(?:position|fellow)\s+in\s+"
    r"|postdoc(?:toral)?\s*(?:position|:\s*)\s*"
    r"|phd\s+(?:position|student|candidate)\s+in\s+"
    r"|research\s+(?:associate|fellow|assistant)\s+in\s+"
    r")",
    re.IGNORECASE,
)

# Trailing parenthetical noise like (m/f/d), (Zurich), etc.
_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


@dataclass(frozen=True)
class IdentityInputs:
    """Source-agnostic fields needed for identity resolution."""

    native_ref: str | None
    application_url: str | None
    detail_url: str | None
    title: str
    organization: str | None
    country: str | None
    deadline: str | None


def normalize_title(title: str) -> str:
    """Normalize a title for fuzzy comparison.

    Strips common academic-job prefixes, trailing parenthetical noise,
    lowercases, and collapses whitespace. If stripping reduces the title
    to empty, returns the lowercased and whitespace-collapsed original.
    """
    lowered = _WHITESPACE.sub(" ", title.strip()).lower()
    stripped = _TITLE_NOISE.sub("", lowered).strip()
    stripped = _TRAILING_PAREN.sub("", stripped).strip()
    return stripped if stripped else lowered


def _normalize(value: str | None) -> str:
    """Lowercase, strip, and collapse whitespace."""
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", value.strip()).lower()


def compute_fingerprint(
    *,
    title: str,
    org: str | None,
    country: str | None,
    deadline: str | None,
) -> str:
    """Compute a SHA-256 fingerprint from opportunity fields.

    The fingerprint is the hex digest of ``title|org|country|deadline``
    after lowercasing, stripping, and collapsing whitespace.  None fields
    are represented as the literal string ``<none>`` so that None and
    empty-string produce distinct fingerprints.
    """

    def _part(v: str | None) -> str:
        if v is None:
            return "<none>"
        return _normalize(v)

    canonical = "|".join([_part(title), _part(org), _part(country), _part(deadline)])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve(inputs: IdentityInputs) -> tuple[IdentityKind, str]:
    """Resolve the identity of an opportunity from its available fields.

    Returns a ``(kind, key)`` pair suitable for the ``Opportunity`` table's
    ``identity_kind`` and ``identity_key`` columns.

    Raises ``ValueError`` if all identifiers are absent (including title
    being empty, which prevents fingerprint generation).
    """
    if inputs.native_ref:
        log.debug("identity.resolved", kind="native_ref", key=inputs.native_ref)
        return IdentityKind.NATIVE_REF, inputs.native_ref

    if inputs.application_url:
        log.debug("identity.resolved", kind="application_url", key=inputs.application_url)
        return IdentityKind.APPLICATION_URL, inputs.application_url

    if inputs.detail_url:
        log.debug("identity.resolved", kind="detail_url", key=inputs.detail_url)
        return IdentityKind.DETAIL_URL, inputs.detail_url

    # Fallback: fingerprint
    title = _normalize(inputs.title)
    if not title:
        raise ValueError("Cannot resolve identity: no native_ref, URL, or title available")

    fp = compute_fingerprint(
        title=inputs.title,
        org=inputs.organization,
        country=inputs.country,
        deadline=inputs.deadline,
    )
    log.debug("identity.resolved", kind="fingerprint", key=fp)
    return IdentityKind.FINGERPRINT, fp
