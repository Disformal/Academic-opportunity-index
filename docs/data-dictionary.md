# AOI data dictionary

Generated from the models in `aoi/db/schema/` by `uv run python -m aoi.db.dictionary`.
Do not edit by hand: a test fails when this file and the models differ.

Rows in evidence, assertions, postings, history, links, samples and labels are never deleted
(ADR 0005, 0027, 0028). Times are UTC. *Observed time* is when AOI saw something;
*valid time* is when it was true at the source.

## Schema `evidence`

L0: the fetch log. Raw responses live in WARC files (ADR 0006).

### `evidence.fetch`

Every outbound request, successful or not. Immutable.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `crawl_target_id` | UUID | no | → ops.crawl_target.id | The target that was fetched. |
| `url` | TEXT | no |  | URL requested. |
| `final_url` | TEXT | yes |  | URL after redirects. |
| `requested_at` | TIMESTAMP WITH TIME ZONE | no |  | When the request started (UTC). |
| `elapsed_ms` | INTEGER | yes |  | Total time to response. |
| `outcome` | enum: hit, not_modified, error, blocked, rate_limited | no |  | hit, not_modified, error, blocked (SSRF / robots) or rate_limited. |
| `http_status` | SMALLINT | yes |  | Final HTTP status; null when no response was received. |
| `resolved_ip` | INET | yes |  | Address validate_host resolved for the final URL's host (ADR 0023). |
| `body_sha256` | VARCHAR(64) | yes |  | Hex SHA-256 of the body. |
| `body_bytes` | INTEGER | yes |  | Body size in bytes. |
| `warc_record_id` | VARCHAR(100) | yes |  | WARC-Record-ID of the response or revisit record; null if no response came. |
| `warc_key` | TEXT | yes |  | S3 key of the WARC object, stored before this row (ADR 0027, 0030). |
| `error` | TEXT | yes |  | Error class and message, if any. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

Rules:

- `ck_fetch_warc_pair`: `(warc_key IS NULL) = (warc_record_id IS NULL)`

## Schema `core`

L1 observations and L2 entities: sources, assertions, postings, positions, organizations, disciplines, funders, awards (ADR 0007).

### `core.assertion`

One field value from one document (L1). Append-only, never deleted.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `posting_id` | UUID | no | → core.posting.id | The posting this value describes. |
| `fetch_id` | UUID | no | → evidence.fetch.id | The fetched document the value was read from. |
| `extraction_run_id` | UUID | no | → core.extraction_run.id | The extractor execution that produced it. |
| `field` | VARCHAR(100) | no |  | Field name, e.g. 'title', 'deadline'. |
| `value` | JSONB | no |  | Normalised value. |
| `raw` | TEXT | yes |  | Value as it appeared in the document. |
| `method` | enum: official_api, structured_markup, template, llm | no |  | How it was read; decides which evidence pointer is required. |
| `pointer` | TEXT | yes |  | JSON Pointer (RFC 6901) or XPath into the document; for API and markup. |
| `span_start` | INTEGER | yes |  | Start character offset in the page text; for template extraction. |
| `span_end` | INTEGER | yes |  | End offset (exclusive). |
| `quote` | TEXT | yes |  | Verbatim supporting text from the page; required for LLM extraction. |
| `confidence` | NUMERIC(4, 3) | no |  | Extractor confidence in [0, 1]. |
| `observed_at` | TIMESTAMP WITH TIME ZONE | no |  | Observed time of the document. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

Rules:

- `ck_assertion_confidence`: `confidence >= 0 AND confidence <= 1`
- `ck_assertion_evidence_pointer`: `method IN ('official_api', 'structured_markup') AND pointer IS NOT NULL OR method = 'template' AND span_start IS NOT NULL OR method = 'llm' AND quote IS NOT NULL`
- `ck_assertion_span`: `span_start IS NULL AND span_end IS NULL OR span_start IS NOT NULL AND span_end IS NOT NULL AND span_start >= 0 AND span_end > span_start`

### `core.award`

A funded project from an award database.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `source` | VARCHAR(50) | no |  | Award database, e.g. 'cordis', 'nih_reporter'. |
| `native_id` | TEXT | no |  | The award's ID in that database. |
| `funder_id` | UUID | yes | → core.funder.id | Who funded it. |
| `ror_id` | VARCHAR(9) | yes | → core.organization.ror_id | Host organization. |
| `title` | TEXT | yes |  | Project title. |
| `amount` | NUMERIC(16, 2) | yes |  | Awarded amount. |
| `currency` | VARCHAR(3) | yes |  | ISO 4217 code of amount. |
| `start_date` | DATE | yes |  | Project start. |
| `end_date` | DATE | yes |  | Project end. |
| `discipline_code` | VARCHAR(40) | yes | → core.discipline.code | Main discipline. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_award_amount_currency`: `amount IS NULL OR currency IS NOT NULL`
- `ck_award_dates`: `end_date >= start_date`

### `core.award_person`

People on an award, as pseudonyms only.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `award_id` | UUID | no | PK → core.award.id | Award. |
| `pid` | VARCHAR(64) | no | PK → core.person_pseudonym.pid | Pseudonymised person. |
| `role` | VARCHAR(50) | no | PK | Role as the award database states it, e.g. 'pi', 'co_pi'. |

### `core.call_details`

Fields specific to funding calls.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `position_id` | UUID | no | PK → core.position.id | The position. |
| `kind` | enum: job, funding_call, fellowship, scholarship | no | → core.position.kind | Always 'funding_call'; part of the foreign key that enforces it. |
| `funder_id` | UUID | yes | → core.funder.id | Who funds the call. |
| `call_identifier` | TEXT | yes |  | The funder's call code, e.g. 'ERC-2027-StG'. |
| `opens_at` | TIMESTAMP WITH TIME ZONE | yes |  | When applications open (valid time). |
| `funding_min` | NUMERIC(16, 2) | yes |  | Lower bound of funding per award, as stated. |
| `funding_max` | NUMERIC(16, 2) | yes |  | Upper bound of funding per award, as stated. |
| `funding_currency` | VARCHAR(3) | yes |  | ISO 4217 code of the stated funding. |
| `duration_months` | SMALLINT | yes |  | Length of the funded project. |

Rules:

- `ck_call_details_funding_currency`: `funding_min IS NULL AND funding_max IS NULL OR funding_currency IS NOT NULL`
- `ck_call_details_funding_order`: `funding_max >= funding_min`
- `ck_call_details_kind`: `kind = 'funding_call'`

### `core.discipline`

Taxonomy: OECD FOS 2007 plus arXiv physics categories.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `code` | VARCHAR(40) | no | PK | Scheme-prefixed code, e.g. 'fos:1.3', 'arxiv:hep-th'. |
| `scheme` | enum: oecd_fos_2007, arxiv | no |  | oecd_fos_2007 or arxiv. |
| `label` | TEXT | no |  | English label. |

### `core.discipline_parent`

Broader-narrower links. A discipline may have several parents: overlaps between fields are intended.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `child_code` | VARCHAR(40) | no | PK → core.discipline.code | The narrower discipline. |
| `parent_code` | VARCHAR(40) | no | PK → core.discipline.code | One of its broader disciplines, e.g. an arXiv category's FOS field. |

Rules:

- `ck_discipline_parent_not_self`: `child_code <> parent_code`

### `core.extraction_run`

One execution of one extractor version.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `fetch_id` | UUID | no | → evidence.fetch.id | The document extracted; one run per fetch per extractor version (ADR 0030). |
| `extractor` | VARCHAR(100) | no |  | Extractor name, e.g. 'inspire.api'. |
| `extractor_version` | VARCHAR(50) | no |  | Version of the extractor code; replay changes it. |
| `model_id` | VARCHAR(200) | yes |  | LLM model identifier; null for deterministic extractors. |
| `model_quantisation` | VARCHAR(50) | yes |  | LLM quantisation, e.g. 'Q4_K_M'. |
| `prompt_sha256` | VARCHAR(64) | yes |  | Hex SHA-256 of the prompt template; required iff model_id is set. |
| `started_at` | TIMESTAMP WITH TIME ZONE | no |  | Start of the run (UTC). |
| `finished_at` | TIMESTAMP WITH TIME ZONE | yes |  | End of the run; null while running or if it crashed. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

Rules:

- `ck_extraction_run_llm_fields`: `(model_id IS NULL) = (prompt_sha256 IS NULL)`

### `core.fellowship_details`

Fields specific to fellowships and scholarships.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `position_id` | UUID | no | PK → core.position.id | The position. |
| `kind` | enum: job, funding_call, fellowship, scholarship | no | → core.position.kind | fellowship or scholarship; part of the foreign key that enforces it. |
| `career_stage` | enum: r1, r2, r3, r4 | yes |  | R1-R4 (EU research careers). |
| `required_degree` | enum: none, bachelor, master, phd | yes |  | Minimum degree required to apply. |
| `max_years_since_phd` | SMALLINT | yes |  | Eligibility limit in years after the PhD. |
| `required_languages` | VARCHAR(3)[] | no |  | ISO 639 codes of languages required for the role. |
| `duration_months` | SMALLINT | yes |  | Length of the appointment or award. |
| `start_date` | DATE | yes |  | Stated start date. |
| `salary_min` | NUMERIC(12, 2) | yes |  | Lower bound of pay or stipend, as stated. |
| `salary_max` | NUMERIC(12, 2) | yes |  | Upper bound of pay or stipend, as stated. |
| `salary_currency` | VARCHAR(3) | yes |  | ISO 4217 code of the stated pay. |
| `salary_period` | enum: year, month, hour | yes |  | year, month or hour. |
| `salary_eur_annual` | NUMERIC(12, 2) | yes |  | Midpoint pay normalised to EUR per year (ECB reference rate). |
| `fx_date` | DATE | yes |  | Date of the ECB rate used for salary_eur_annual. |

Rules:

- `ck_fellowship_details_kind`: `kind IN ('fellowship', 'scholarship')`
- `ck_fellowship_details_salary_fx`: `salary_eur_annual IS NULL OR fx_date IS NOT NULL`
- `ck_fellowship_details_salary_order`: `salary_max >= salary_min`
- `ck_fellowship_details_salary_unit`: `salary_min IS NULL AND salary_max IS NULL OR salary_currency IS NOT NULL AND salary_period IS NOT NULL`

### `core.funder`

A funding body, keyed by ROR and/or Crossref Funder ID.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `name` | TEXT | no |  | Funder name. |
| `ror_id` | VARCHAR(9) | yes | → core.organization.ror_id | The funder's ROR record. |
| `crossref_funder_id` | VARCHAR(30) | yes |  | Crossref Funder Registry ID (e.g. '501100000781' for the ERC). |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_funder_has_key`: `ror_id IS NOT NULL OR crossref_funder_id IS NOT NULL`

### `core.grant_reference`

A grant number found in a posting, raw and normalised.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `assertion_id` | UUID | no | → core.assertion.id | The assertion the grant number was extracted in (provenance). |
| `raw` | TEXT | no |  | As written in the posting. |
| `normalised` | TEXT | yes |  | Canonical form per funder rules, e.g. '101042368'. |
| `funder_id` | UUID | yes | → core.funder.id | Funder the number belongs to, once known. |
| `award_id` | UUID | yes | → core.award.id | Matched award, once award data is ingested. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

### `core.job_details`

Fields specific to jobs.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `position_id` | UUID | no | PK → core.position.id | The position. |
| `kind` | enum: job, funding_call, fellowship, scholarship | no | → core.position.kind | Always 'job'; part of the foreign key that enforces it. |
| `contract_type` | enum: permanent, fixed_term, tenure_track | yes |  | permanent, fixed_term or tenure_track. |
| `fte` | NUMERIC(3, 2) | yes |  | Full-time equivalent in (0, 1]. |
| `career_stage` | enum: r1, r2, r3, r4 | yes |  | R1-R4 (EU research careers). |
| `required_degree` | enum: none, bachelor, master, phd | yes |  | Minimum degree required to apply. |
| `max_years_since_phd` | SMALLINT | yes |  | Eligibility limit in years after the PhD. |
| `required_languages` | VARCHAR(3)[] | no |  | ISO 639 codes of languages required for the role. |
| `duration_months` | SMALLINT | yes |  | Length of the appointment or award. |
| `start_date` | DATE | yes |  | Stated start date. |
| `salary_min` | NUMERIC(12, 2) | yes |  | Lower bound of pay or stipend, as stated. |
| `salary_max` | NUMERIC(12, 2) | yes |  | Upper bound of pay or stipend, as stated. |
| `salary_currency` | VARCHAR(3) | yes |  | ISO 4217 code of the stated pay. |
| `salary_period` | enum: year, month, hour | yes |  | year, month or hour. |
| `salary_eur_annual` | NUMERIC(12, 2) | yes |  | Midpoint pay normalised to EUR per year (ECB reference rate). |
| `fx_date` | DATE | yes |  | Date of the ECB rate used for salary_eur_annual. |

Rules:

- `ck_job_details_fte`: `fte > 0 AND fte <= 1`
- `ck_job_details_kind`: `kind = 'job'`
- `ck_job_details_salary_fx`: `salary_eur_annual IS NULL OR fx_date IS NOT NULL`
- `ck_job_details_salary_order`: `salary_max >= salary_min`
- `ck_job_details_salary_unit`: `salary_min IS NULL AND salary_max IS NULL OR salary_currency IS NOT NULL AND salary_period IS NOT NULL`

### `core.org_alias`

Raw organization string -> ROR. Superseded, never overwritten.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `raw` | TEXT | no |  | Organization string exactly as a source wrote it. |
| `ror_id` | VARCHAR(9) | yes | → core.organization.ror_id | Matched organization; null = checked, no confident match. |
| `method` | enum: ror_api, rule, human | no |  | ror_api (affiliation matching), rule or human. |
| `confidence` | NUMERIC(4, 3) | yes |  | Match score in [0, 1]; required unless method is human. |
| `decided_by` | VARCHAR(100) | no |  | Matcher name and version, or the human reviewer. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `superseded_at` | TIMESTAMP WITH TIME ZONE | yes |  | When a newer mapping replaced this one. |

Rules:

- `ck_org_alias_confidence`: `method = 'human' OR confidence IS NOT NULL`
- `ck_org_alias_range`: `confidence >= 0 AND confidence <= 1`

### `core.organization`

Mirror of the ROR registry, one row per ROR record.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `ror_id` | VARCHAR(9) | no | PK | Bare ROR ID, e.g. '01ggx4157' (no URL prefix). |
| `display_name` | TEXT | no |  | The ROR display name. |
| `names` | JSONB | no |  | All ROR names: [{value, types, lang}] (labels, aliases, acronyms). |
| `types` | VARCHAR(20)[] | no |  | ROR types, e.g. education, facility, funder, government. |
| `status` | enum: active, inactive, withdrawn | no |  | active, inactive or withdrawn. |
| `country_code` | VARCHAR(2) | yes |  | ISO 3166-1 alpha-2 of the first ROR location. |
| `city` | TEXT | yes |  | GeoNames name of the first location. |
| `lat` | DOUBLE PRECISION | yes |  | Latitude of the first location. |
| `lon` | DOUBLE PRECISION | yes |  | Longitude of the first location. |
| `established` | SMALLINT | yes |  | Year founded. |
| `fundref_ids` | VARCHAR(30)[] | no |  | Crossref Funder IDs listed in ROR; the join key to funder data. |
| `ror_release` | VARCHAR(30) | no |  | ROR dump release this row was loaded from. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_organization_ror_id`: `ror_id ~ '^0[a-z0-9]{6}[0-9]{2}$'`

### `core.organization_relation`

ROR relationships between organizations.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `ror_id` | VARCHAR(9) | no | PK → core.organization.ror_id | The organization the relation is on. |
| `related_ror_id` | VARCHAR(9) | no | PK → core.organization.ror_id | The related organization. |
| `type` | enum: parent, child, related, successor, predecessor | no | PK | parent, child, related, successor or predecessor (as seen from ror_id). |

### `core.person_pseudonym`

A countable person with no identifying data (ADR 0011).

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `pid` | VARCHAR(64) | no | PK | Hex HMAC-SHA256(secret, ORCID or source person key). Never the key itself. |
| `key_kind` | enum: orcid, source_key | no |  | What was hashed: orcid or source_key. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

Rules:

- `ck_person_pseudonym_pid`: `pid ~ '^[0-9a-f]{64}$'`

### `core.position`

The real-world opportunity behind one or more postings.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `kind` | enum: job, funding_call, fellowship, scholarship | no |  | job, funding_call, fellowship or scholarship. |
| `title` | TEXT | no |  | Resolved title. |
| `ror_id` | VARCHAR(9) | yes | → core.organization.ror_id | Resolved organization. |
| `country_code` | VARCHAR(2) | yes |  | ISO 3166-1 alpha-2 where the position is based. |
| `city` | TEXT | yes |  | City where the position is based. |
| `posted_at` | TIMESTAMP WITH TIME ZONE | yes |  | Source-stated publication date (valid time). |
| `deadline_at` | TIMESTAMP WITH TIME ZONE | yes |  | Application deadline (valid time); null = none stated. |
| `deadline_precision` | enum: day, month, year | yes |  | day, month or year; set iff deadline_at is set. |
| `application_url` | TEXT | yes |  | Where to apply. |
| `in_scope` | BOOLEAN | yes |  | Within the site's coverage (e.g. physics); null until classified. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_position_deadline_precision`: `(deadline_at IS NULL) = (deadline_precision IS NULL)`

### `core.position_discipline`

Disciplines a position belongs to.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `position_id` | UUID | no | PK → core.position.id | The position. |
| `discipline_code` | VARCHAR(40) | no | PK → core.discipline.code | One of its disciplines. |

### `core.position_history`

Resolved position fields over observed time.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `position_id` | UUID | no | → core.position.id | The position. |
| `observed_from` | TIMESTAMP WITH TIME ZONE | no |  | Observed time this version was first resolved. |
| `observed_to` | TIMESTAMP WITH TIME ZONE | yes |  | Observed time a newer version replaced it; null = current. |
| `snapshot` | JSONB | no |  | All resolved fields of this version. |

Rules:

- `ck_position_history_order`: `observed_to > observed_from`

### `core.position_link`

posting -> position. Superseded, never deleted (ADR 0008).

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `posting_id` | UUID | no | → core.posting.id | The posting being linked. |
| `position_id` | UUID | no | → core.position.id | The position it is linked to. |
| `method` | enum: auto, human | no |  | auto (linker) or human (review queue). |
| `probability` | NUMERIC(4, 3) | yes |  | Linker match probability in [0, 1]; required for auto links. |
| `signals` | JSONB | yes |  | Comparison values that produced the probability. |
| `decided_by` | VARCHAR(100) | no |  | Linker name and version, or the human reviewer. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `superseded_at` | TIMESTAMP WITH TIME ZONE | yes |  | When a newer decision replaced this link. |

Rules:

- `ck_position_link_probability`: `method = 'human' OR probability IS NOT NULL`
- `ck_position_link_probability_range`: `probability >= 0 AND probability <= 1`

### `core.posting`

One listing at one source (ADR 0008). Never deleted.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `source_id` | UUID | no | → core.source.id | Where the listing appears. |
| `identity_kind` | enum: native_ref, detail_url | no |  | native_ref (source's own ID) or detail_url (canonical URL). |
| `identity_key` | TEXT | no |  | The native ref or canonical detail URL. |
| `detail_url` | TEXT | yes |  | Canonical detail page URL, when one exists. |
| `kind` | enum: job, funding_call, fellowship, scholarship | yes |  | job, funding_call, fellowship or scholarship; null until classified. |
| `state` | enum: pending, active, stale, closed, removed | no |  | pending → active → stale → closed; or removed (404/410 twice). |
| `closure_reason` | enum: delisted, stated_closed, deadline_passed, operator | yes |  | Why the posting closed; set iff state is closed. |
| `first_seen_at` | TIMESTAMP WITH TIME ZONE | no |  | Observed time of first sighting. |
| `last_seen_at` | TIMESTAMP WITH TIME ZONE | no |  | Observed time of latest sighting. |
| `disappeared_at` | TIMESTAMP WITH TIME ZONE | yes |  | First observed absence from the source; proxy for time-to-fill. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_posting_closure_reason`: `(state = 'closed') = (closure_reason IS NOT NULL)`
- `ck_posting_seen_order`: `last_seen_at >= first_seen_at`

### `core.posting_history`

Resolved posting fields over observed time.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `posting_id` | UUID | no | → core.posting.id | The posting. |
| `observed_from` | TIMESTAMP WITH TIME ZONE | no |  | Observed time we first saw this version. |
| `observed_to` | TIMESTAMP WITH TIME ZONE | yes |  | Observed time a newer version replaced it; null = current. |
| `stated_posted_at` | TIMESTAMP WITH TIME ZONE | yes |  | Source-stated publication date (valid time). |
| `stated_deadline_at` | TIMESTAMP WITH TIME ZONE | yes |  | Source-stated deadline (valid time). |
| `snapshot` | JSONB | no |  | All resolved fields of this version. |

Rules:

- `ck_posting_history_order`: `observed_to > observed_from`

### `core.posting_sighting`

A fetch that showed a posting (ADR 0033). Append-only, never deleted.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `posting_id` | UUID | no | → core.posting.id | The posting shown. |
| `fetch_id` | UUID | no | → evidence.fetch.id | The fetch that showed it; for an unchanged fetch, its earlier content did. |
| `observed_at` | TIMESTAMP WITH TIME ZONE | no |  | Observed time: when the fetch was made. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

### `core.source`

A source of postings and its legal basis (ADR 0012).

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `name` | VARCHAR(200) | no |  | Stable short name, e.g. 'inspire'. |
| `kind` | enum: api, feed, ats_platform, jsonld, html, oai | no |  | api, feed, ats_platform, jsonld, html or oai. |
| `platform` | VARCHAR(50) | yes |  | Shared job-board platform (e.g. 'workday'); null if bespoke. |
| `base_url` | TEXT | no |  | Entry point for discovery. |
| `legal_basis` | enum: official_api, open_licence, primary_institution, consent | yes |  | Why we may collect from it; the scheduler refuses null. |
| `consent_ref` | TEXT | yes |  | Pointer to the consent record in docs/sources/, when basis is consent. |
| `tos_url` | TEXT | yes |  | Terms of service URL. |
| `robots_status` | enum: allowed, disallowed, not_applicable, unknown | no |  | Result of the last robots.txt assessment. |
| `scope_tags` | VARCHAR(50)[] | no |  | Free tags for coverage scope, e.g. 'physics', 'eu'. |
| `enabled` | BOOLEAN | no |  | Scheduler only crawls if true. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

## Schema `ops`

Crawl scheduling, ground-truth samples and labels, the erasure log.

### `ops.crawl_target`

A URL we revisit on an adaptive schedule.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `source_id` | UUID | no | → core.source.id | The source this URL belongs to. |
| `host_id` | UUID | no | → ops.host.id | Host for politeness accounting. |
| `url` | TEXT | no |  | Canonical URL. |
| `url_sha256` | VARCHAR(64) | no |  | Hex SHA-256 of `url`; the uniqueness key. |
| `role` | enum: listing, detail, feed | no |  | listing, detail or feed. |
| `etag` | VARCHAR(300) | yes |  | ETag from the last successful response, for conditional GET. |
| `last_modified` | VARCHAR(100) | yes |  | Last-Modified header from the last successful response, verbatim. |
| `content_sha256` | VARCHAR(64) | yes |  | Hex SHA-256 of the last body; extraction runs only when it changes. |
| `recrawl_interval` | INTERVAL | no |  | Adaptive: halves on change, doubles otherwise, within [1 h, 7 d]. |
| `next_due_at` | TIMESTAMP WITH TIME ZONE | no |  | When the scheduler should next enqueue a fetch. |
| `retired_at` | TIMESTAMP WITH TIME ZONE | yes |  | When it was retired: never fetched again, ignored by lifecycle passes; kept. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_crawl_target_recrawl_bounds`: `recrawl_interval BETWEEN interval '1 hour' AND interval '7 days'`

### `ops.erasure_log`

Rows deleted under a legal erasure (ADR 0027). Append-only (ADR 0028).

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `table_name` | TEXT | no |  | Schema-qualified table the row was deleted from. |
| `row_id` | UUID | no |  | Primary key of the deleted row (its content is not kept). |
| `erasure_ref` | TEXT | no |  | Ticket of the erasure request, from aoi.erasure_ref. |
| `erased_by` | TEXT | no |  | Database role that ran the delete. |
| `erased_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was deleted. |

### `ops.host`

Per-host politeness and circuit-breaker state.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `hostname` | VARCHAR(253) | no |  | Lower-case hostname, no port. |
| `rps` | NUMERIC(6, 3) | no |  | Maximum requests per second to this host. |
| `next_allowed_at` | TIMESTAMP WITH TIME ZONE | no |  | Earliest time the next request to this host may start. |
| `circuit_state` | enum: closed, open, half_open | no |  | closed = requests flow; open = paused after failures; half_open = probing. |
| `consecutive_failures` | SMALLINT | no |  | Failures since the last success; drives the circuit breaker. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_host_rps_positive`: `rps > 0`

### `ops.label`

A human judgement on a sample item. Only the project reviewer's labels count toward a gate.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `sample_item_id` | UUID | no | → ops.sample_item.id | The item judged. |
| `labeller` | VARCHAR(100) | no |  | Who labelled it, e.g. 'reviewer'. |
| `label` | JSONB | no |  | The judgement, e.g. {"correct": true}. |
| `note` | TEXT | yes |  | Free-text remark by the labeller. |
| `labelled_at` | TIMESTAMP WITH TIME ZONE | no |  | When the label was given. |
| `superseded_at` | TIMESTAMP WITH TIME ZONE | yes |  | When the same labeller replaced this label. |

### `ops.sample`

A reproducible random sample drawn for labelling.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `name` | VARCHAR(100) | no |  | Unique name, e.g. 'g1-deadline-2026-11'. |
| `gate` | VARCHAR(10) | yes |  | Launch gate this sample measures (G1-G6), if any. |
| `frame` | TEXT | no |  | Definition of the population sampled from (SQL or prose). |
| `frame_size` | INTEGER | no |  | Population size N when the sample was drawn. |
| `seed` | BIGINT | no |  | Random seed; with frame, reproduces the draw. |
| `n` | INTEGER | no |  | Number of items drawn. |
| `strata` | JSONB | yes |  | Per stratum: {name: {frame_size, n}}; null for a simple random sample. |
| `drawn_by` | VARCHAR(100) | no |  | Who or what drew the sample (tool and version). |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |

Rules:

- `ck_sample_n`: `n > 0 AND n <= frame_size`

### `ops.sample_item`

One drawn item of a sample.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `sample_id` | UUID | no | → ops.sample.id | The sample. |
| `ordinal` | INTEGER | no |  | Draw order within the sample, from 1. |
| `stratum` | VARCHAR(100) | yes |  | Stratum the item was drawn from; null if unstratified. |
| `item` | JSONB | no |  | What is labelled, e.g. {"posting_id": ...} or {"posting_ids": [a, b]}. |

### `ops.task`

The work queue (ADR 0014, 0030). Workers claim under a lease.

| Column | Type | Nullable | Key | Description |
|---|---|---|---|---|
| `id` | UUID | no | PK | Primary key (UUID). |
| `kind` | VARCHAR(32) | no |  | Handler that runs it: discover, fetch, extract, … |
| `source_id` | UUID | no | → core.source.id | Source the work belongs to. |
| `crawl_target_id` | UUID | yes | → ops.crawl_target.id | Target a fetch task fetches; at most one open task per kind and target. |
| `host_id` | UUID | yes | → ops.host.id | Host the task contacts; claimable only once the host is due. |
| `payload` | JSONB | no |  | Handler input, e.g. a target id. |
| `state` | enum: queued, running, done, failed, dead | no |  | queued, running, done, failed (retry at run_after) or dead (gave up). |
| `priority` | SMALLINT | no |  | Higher is claimed first. |
| `run_after` | TIMESTAMP WITH TIME ZONE | no |  | Not claimable before this time (the retry backoff). |
| `attempts` | SMALLINT | no |  | Claims so far; counted at claim time, so crashes count too. |
| `locked_by` | VARCHAR(100) | yes |  | Worker holding the lease, e.g. 'host:pid'. |
| `locked_at` | TIMESTAMP WITH TIME ZONE | yes |  | When the lease was taken. |
| `last_error` | TEXT | yes |  | Error class and message of the last failed attempt. |
| `retry_of` | UUID | yes | → ops.task.id | Dead task this one retries after review (aoi task requeue); one retry each. |
| `created_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was inserted. |
| `updated_at` | TIMESTAMP WITH TIME ZONE | no |  | When the row was last changed (maintained by a trigger). |

Rules:

- `ck_task_attempts`: `attempts >= 0`
- `ck_task_lock_pair`: `(locked_by IS NULL) = (locked_at IS NULL)`
- `ck_task_running_locked`: `state <> 'running' OR locked_by IS NOT NULL`

## Schema `analytics`

L3 research tables (built in M8).

*No tables yet.*
