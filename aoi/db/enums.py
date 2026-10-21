"""PostgreSQL enumerated types for the AOI database."""

import enum


class PublicationState(enum.StrEnum):
    UNVERIFIED = "unverified"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    REJECTED = "rejected"


class OpportunityStatus(enum.StrEnum):
    UPCOMING = "upcoming"
    OPEN = "open"
    ROLLING = "rolling"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class ClosureReason(enum.StrEnum):
    REMOVED_404 = "removed_404"
    DELISTED = "delisted"
    STATED_CLOSED = "stated_closed"
    DEADLINE_PASSED = "deadline_passed"
    SUPERSEDED = "superseded"
    OPERATOR = "operator"


class IdentityKind(enum.StrEnum):
    NATIVE_REF = "native_ref"
    APPLICATION_URL = "application_url"
    DETAIL_URL = "detail_url"
    FINGERPRINT = "fingerprint"


class PageRole(enum.StrEnum):
    LISTING = "listing"
    DETAIL = "detail"
    UNKNOWN = "unknown"


class Derivation(enum.StrEnum):
    OFFICIAL_API = "official_api"
    STRUCTURED_MARKUP = "structured_markup"
    STRUCTURED_DATA = "structured_data"
    TEMPLATE = "template"
    PARSER = "parser"
    HUMAN = "human"
    INHERITED = "inherited"


class FetchOutcome(enum.StrEnum):
    HIT = "hit"
    NOT_MODIFIED = "not_modified"
    ERROR = "error"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
