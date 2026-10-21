"""Enumerated types for the layered schemas."""

import enum


class SourceKind(enum.StrEnum):
    API = "api"
    FEED = "feed"
    ATS_PLATFORM = "ats_platform"
    JSONLD = "jsonld"
    HTML = "html"
    OAI = "oai"


class LegalBasis(enum.StrEnum):
    """ADR 0012. A source without one is refused by the scheduler."""

    OFFICIAL_API = "official_api"
    OPEN_LICENCE = "open_licence"
    PRIMARY_INSTITUTION = "primary_institution"
    CONSENT = "consent"


class RobotsStatus(enum.StrEnum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class TargetRole(enum.StrEnum):
    LISTING = "listing"
    DETAIL = "detail"
    FEED = "feed"


class CircuitState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class FetchOutcome(enum.StrEnum):
    HIT = "hit"
    NOT_MODIFIED = "not_modified"
    ERROR = "error"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"


class OpportunityKind(enum.StrEnum):
    JOB = "job"
    FUNDING_CALL = "funding_call"
    FELLOWSHIP = "fellowship"
    SCHOLARSHIP = "scholarship"


class PostingIdentityKind(enum.StrEnum):
    """Within-source identity only (ADR 0008)."""

    NATIVE_REF = "native_ref"
    DETAIL_URL = "detail_url"


class PostingState(enum.StrEnum):
    """Lifecycle (plan §4.3). `pending` = discovered, not yet extracted."""

    PENDING = "pending"
    ACTIVE = "active"
    STALE = "stale"
    CLOSED = "closed"
    REMOVED = "removed"


class ClosureReason(enum.StrEnum):
    DELISTED = "delisted"
    STATED_CLOSED = "stated_closed"
    DEADLINE_PASSED = "deadline_passed"
    OPERATOR = "operator"


class AssertionMethod(enum.StrEnum):
    """How a value was read. Determines which evidence pointer is required."""

    OFFICIAL_API = "official_api"  # pointer into a JSON/XML response
    STRUCTURED_MARKUP = "structured_markup"  # pointer into JSON-LD / microdata
    TEMPLATE = "template"  # span in the page text
    LLM = "llm"  # verbatim quote from the page text


class OrgStatus(enum.StrEnum):
    """ROR record status."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    WITHDRAWN = "withdrawn"


class OrgRelationType(enum.StrEnum):
    """ROR relationship types."""

    PARENT = "parent"
    CHILD = "child"
    RELATED = "related"
    SUCCESSOR = "successor"
    PREDECESSOR = "predecessor"


class AliasMethod(enum.StrEnum):
    """How a raw organization string was mapped to ROR (ADR 0009)."""

    ROR_API = "ror_api"
    RULE = "rule"
    HUMAN = "human"


class DisciplineScheme(enum.StrEnum):
    OECD_FOS_2007 = "oecd_fos_2007"
    ARXIV = "arxiv"


class DatePrecision(enum.StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class LinkMethod(enum.StrEnum):
    AUTO = "auto"
    HUMAN = "human"


class CareerStage(enum.StrEnum):
    """European Framework for Research Careers."""

    R1 = "r1"  # first stage, up to the PhD
    R2 = "r2"  # recognised: PhD holder, not yet independent
    R3 = "r3"  # established: independent researcher
    R4 = "r4"  # leading researcher


class RequiredDegree(enum.StrEnum):
    NONE = "none"
    BACHELOR = "bachelor"
    MASTER = "master"
    PHD = "phd"


class ContractType(enum.StrEnum):
    PERMANENT = "permanent"
    FIXED_TERM = "fixed_term"
    TENURE_TRACK = "tenure_track"


class SalaryPeriod(enum.StrEnum):
    YEAR = "year"
    MONTH = "month"
    HOUR = "hour"


class PersonKeyKind(enum.StrEnum):
    """What was hashed into a person pseudonym (ADR 0011)."""

    ORCID = "orcid"
    SOURCE_KEY = "source_key"


class TaskState(enum.StrEnum):
    """ADR 0030. `failed` waits for its retry at run_after; `dead` has run out of attempts."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"
