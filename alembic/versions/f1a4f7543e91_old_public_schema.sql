-- The old public schema (pre-M3 tables, enum types, functions), empty: recreated by the
-- (public.update_updated_at() is not here: the new schemas' triggers use it, so it is never dropped)
-- downgrade of f1a4f7543e91. pg_dump --schema-only -n public -T public.alembic_version
-- --no-owner --no-privileges of the test database at 07b00ccfa252, without session settings.

--
-- PostgreSQL database dump
--

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

--
-- Name: closure_reason; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.closure_reason AS ENUM (
    'removed_404',
    'delisted',
    'stated_closed',
    'deadline_passed',
    'superseded',
    'operator'
);

--
-- Name: derivation; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.derivation AS ENUM (
    'official_api',
    'structured_markup',
    'template',
    'parser',
    'human',
    'inherited',
    'structured_data'
);

--
-- Name: fetch_outcome; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.fetch_outcome AS ENUM (
    'hit',
    'not_modified',
    'error',
    'blocked',
    'rate_limited'
);

--
-- Name: identity_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.identity_kind AS ENUM (
    'native_ref',
    'application_url',
    'detail_url',
    'fingerprint'
);

--
-- Name: opportunity_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.opportunity_status AS ENUM (
    'upcoming',
    'open',
    'rolling',
    'closed',
    'unknown'
);

--
-- Name: page_role; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.page_role AS ENUM (
    'listing',
    'detail',
    'unknown'
);

--
-- Name: publication_state; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.publication_state AS ENUM (
    'unverified',
    'provisional',
    'verified',
    'disputed',
    'rejected'
);

--
-- Name: assert_provenance_complete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.assert_provenance_complete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            missing_field TEXT;
        BEGIN
            -- Only enforce for 'verified' state
            IF NEW.publication_state <> 'verified' THEN
                RETURN NEW;
            END IF;

            -- Check each required field has a current assertion with source_document_id
            FOR missing_field IN
                SELECT unnest(ARRAY['title', 'deadline', 'application_url'])
            LOOP
                IF NOT EXISTS (
                    SELECT 1
                    FROM field_assertion fa
                    WHERE fa.opportunity_id = NEW.id
                      AND fa.field_path = missing_field
                      AND fa.is_current = true
                      AND fa.source_document_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION
                        'opportunity % missing verified assertion: %',
                        NEW.id, missing_field;
                END IF;
            END LOOP;

            RETURN NEW;
        END;
        $$;

--
-- Name: opportunity_search_vector_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.opportunity_search_vector_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          NEW.search_vector :=
            setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'A') ||
            setweight(to_tsvector('simple', coalesce(NEW.type, '')), 'B') ||
            setweight(to_tsvector('simple', coalesce(NEW.country, '')), 'C');
          RETURN NEW;
        END
        $$;

--
-- Name: crawl_url; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crawl_url (
    id uuid NOT NULL,
    source_id uuid NOT NULL,
    url_canonical text NOT NULL,
    url_original text NOT NULL,
    url_hash bytea NOT NULL,
    page_role public.page_role NOT NULL,
    parent_listing_id uuid,
    consecutive_absences smallint DEFAULT '0'::smallint NOT NULL,
    last_fetched_at timestamp with time zone,
    etag character varying(300),
    last_modified character varying(100),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: duplicate_cluster; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.duplicate_cluster (
    id uuid NOT NULL,
    canonical_opportunity_id uuid NOT NULL,
    method character varying(50) NOT NULL,
    score numeric(5,4) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: duplicate_member; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.duplicate_member (
    cluster_id uuid NOT NULL,
    opportunity_id uuid NOT NULL,
    probability numeric(5,4) NOT NULL,
    signals jsonb,
    added_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: extraction_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extraction_run (
    id uuid NOT NULL,
    source_document_id uuid NOT NULL,
    extractor_id character varying(100) NOT NULL,
    extractor_version character varying(50) NOT NULL,
    taxonomy_version character varying(50),
    input_digest character varying(64),
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    outcome character varying(30),
    items_emitted integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: fetch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."fetch" (
    id uuid NOT NULL,
    crawl_url_id uuid NOT NULL,
    source_id uuid NOT NULL,
    fetched_at timestamp with time zone NOT NULL,
    http_status smallint,
    outcome public.fetch_outcome NOT NULL,
    content_digest character varying(64),
    evidence_prefix character varying(300),
    elapsed_ms integer,
    body_bytes integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: field_assertion; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.field_assertion (
    id uuid NOT NULL,
    opportunity_id uuid NOT NULL,
    field_path character varying(100) NOT NULL,
    normalized_value jsonb NOT NULL,
    raw_value text,
    confidence numeric(4,3) NOT NULL,
    derivation public.derivation NOT NULL,
    source_document_id uuid NOT NULL,
    extraction_run_id uuid NOT NULL,
    text_start integer,
    text_end integer,
    json_pointer character varying(300),
    is_current boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_verified_at timestamp with time zone,
    CONSTRAINT ck_conf CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT ck_span CHECK (((text_end IS NULL) OR (text_start IS NULL) OR (text_end > text_start)))
);

--
-- Name: opportunity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.opportunity (
    id uuid NOT NULL,
    identity_kind public.identity_kind NOT NULL,
    identity_key text NOT NULL,
    title text NOT NULL,
    type character varying(60) NOT NULL,
    status public.opportunity_status NOT NULL,
    publication_state public.publication_state DEFAULT 'unverified'::public.publication_state NOT NULL,
    primary_language character varying(10) DEFAULT 'en'::character varying NOT NULL,
    deadline timestamp with time zone,
    deadline_precision character varying(10),
    country character varying(2),
    application_url text,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,
    closure_reason public.closure_reason,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    search_vector tsvector,
    CONSTRAINT ck_closed_consistent CHECK (((status = 'closed'::public.opportunity_status) = (closed_at IS NOT NULL)))
);

--
-- Name: opportunity_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.opportunity_version (
    id uuid NOT NULL,
    opportunity_id uuid NOT NULL,
    version_number integer NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    payload jsonb NOT NULL,
    change_summary text,
    change_reason character varying(50),
    caused_by_extraction_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: organization; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organization (
    id uuid NOT NULL,
    ror_id character varying(50),
    preferred_name character varying(500) NOT NULL,
    country_code character varying(2),
    org_type character varying(50),
    website text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: pipeline_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_run (
    id uuid NOT NULL,
    source_id uuid NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    status character varying(20) DEFAULT 'running'::character varying NOT NULL,
    created integer DEFAULT 0 NOT NULL,
    updated integer DEFAULT 0 NOT NULL,
    unchanged integer DEFAULT 0 NOT NULL,
    errors integer DEFAULT 0 NOT NULL,
    closed integer DEFAULT 0 NOT NULL,
    quarantined integer DEFAULT 0 NOT NULL,
    error_message text,
    triggered_by character varying(30) DEFAULT 'cli'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_pipeline_run_status CHECK (((status)::text = ANY ((ARRAY['running'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_pipeline_run_triggered_by CHECK (((triggered_by)::text = ANY ((ARRAY['cli'::character varying, 'api'::character varying, 'cron'::character varying])::text[])))
);

--
-- Name: robots_assessment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.robots_assessment (
    id uuid NOT NULL,
    source_id uuid NOT NULL,
    domain character varying(253) NOT NULL,
    fetched_at timestamp with time zone NOT NULL,
    robots_body text,
    terms_url text,
    verdict character varying(30) NOT NULL,
    rationale text,
    assessed_by character varying(50) NOT NULL,
    review_due timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: source; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    domain character varying(253) NOT NULL,
    endpoint_url text NOT NULL,
    source_type character varying(30) NOT NULL,
    first_party boolean DEFAULT false NOT NULL,
    trust_tier smallint DEFAULT '3'::smallint NOT NULL,
    countries character varying(2)[] DEFAULT '{}'::character varying[] NOT NULL,
    languages character varying(10)[] DEFAULT '{}'::character varying[] NOT NULL,
    acquisition_decision character varying(30) DEFAULT 'pending'::character varying NOT NULL,
    requests_per_second numeric(6,3) DEFAULT 0.5 NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_harvested_at timestamp with time zone,
    oai_pmh_endpoint text,
    CONSTRAINT ck_source_tier CHECK (((trust_tier >= 1) AND (trust_tier <= 5)))
);

--
-- Name: source_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_document (
    id uuid NOT NULL,
    fetch_id uuid NOT NULL,
    source_id uuid NOT NULL,
    content_digest character varying(64) NOT NULL,
    media_type character varying(100) NOT NULL,
    detected_language character varying(10),
    text_object_key text,
    char_count integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: crawl_url crawl_url_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_url
    ADD CONSTRAINT crawl_url_pkey PRIMARY KEY (id);

--
-- Name: duplicate_cluster duplicate_cluster_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duplicate_cluster
    ADD CONSTRAINT duplicate_cluster_pkey PRIMARY KEY (id);

--
-- Name: duplicate_member duplicate_member_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duplicate_member
    ADD CONSTRAINT duplicate_member_pkey PRIMARY KEY (cluster_id, opportunity_id);

--
-- Name: extraction_run extraction_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_run
    ADD CONSTRAINT extraction_run_pkey PRIMARY KEY (id);

--
-- Name: fetch fetch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."fetch"
    ADD CONSTRAINT fetch_pkey PRIMARY KEY (id);

--
-- Name: field_assertion field_assertion_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_assertion
    ADD CONSTRAINT field_assertion_pkey PRIMARY KEY (id);

--
-- Name: opportunity opportunity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunity
    ADD CONSTRAINT opportunity_pkey PRIMARY KEY (id);

--
-- Name: opportunity_version opportunity_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunity_version
    ADD CONSTRAINT opportunity_version_pkey PRIMARY KEY (id);

--
-- Name: organization organization_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT organization_pkey PRIMARY KEY (id);

--
-- Name: organization organization_ror_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organization
    ADD CONSTRAINT organization_ror_id_key UNIQUE (ror_id);

--
-- Name: pipeline_run pipeline_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_run
    ADD CONSTRAINT pipeline_run_pkey PRIMARY KEY (id);

--
-- Name: robots_assessment robots_assessment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robots_assessment
    ADD CONSTRAINT robots_assessment_pkey PRIMARY KEY (id);

--
-- Name: source_document source_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_document
    ADD CONSTRAINT source_document_pkey PRIMARY KEY (id);

--
-- Name: source source_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source
    ADD CONSTRAINT source_pkey PRIMARY KEY (id);

--
-- Name: crawl_url uq_crawlurl; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_url
    ADD CONSTRAINT uq_crawlurl UNIQUE (source_id, url_hash);

--
-- Name: opportunity uq_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunity
    ADD CONSTRAINT uq_identity UNIQUE (identity_kind, identity_key);

--
-- Name: source uq_source_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source
    ADD CONSTRAINT uq_source_name UNIQUE (name);

--
-- Name: ix_crawlurl_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_crawlurl_parent ON public.crawl_url USING btree (parent_listing_id);

--
-- Name: ix_fa_current; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fa_current ON public.field_assertion USING btree (opportunity_id, field_path) WHERE (is_current = true);

--
-- Name: ix_fetch_source_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fetch_source_time ON public."fetch" USING btree (source_id, fetched_at);

--
-- Name: ix_opp_published; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_opp_published ON public.opportunity USING btree (publication_state, deadline);

--
-- Name: ix_opportunity_search_vector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_opportunity_search_vector ON public.opportunity USING gin (search_vector);

--
-- Name: ix_pipeline_run_source_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_run_source_time ON public.pipeline_run USING btree (source_id, started_at);

--
-- Name: crawl_url trg_crawl_url_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_crawl_url_updated_at BEFORE UPDATE ON public.crawl_url FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: duplicate_cluster trg_duplicate_cluster_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_duplicate_cluster_updated_at BEFORE UPDATE ON public.duplicate_cluster FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: duplicate_member trg_duplicate_member_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_duplicate_member_updated_at BEFORE UPDATE ON public.duplicate_member FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: extraction_run trg_extraction_run_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extraction_run_updated_at BEFORE UPDATE ON public.extraction_run FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: fetch trg_fetch_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_fetch_updated_at BEFORE UPDATE ON public."fetch" FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: field_assertion trg_field_assertion_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_field_assertion_updated_at BEFORE UPDATE ON public.field_assertion FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: opportunity trg_opportunity_provenance; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_opportunity_provenance BEFORE INSERT OR UPDATE ON public.opportunity FOR EACH ROW EXECUTE FUNCTION public.assert_provenance_complete();

--
-- Name: opportunity trg_opportunity_search_vector; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_opportunity_search_vector BEFORE INSERT OR UPDATE OF title, type, country ON public.opportunity FOR EACH ROW EXECUTE FUNCTION public.opportunity_search_vector_update();

--
-- Name: opportunity trg_opportunity_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_opportunity_updated_at BEFORE UPDATE ON public.opportunity FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: opportunity_version trg_opportunity_version_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_opportunity_version_updated_at BEFORE UPDATE ON public.opportunity_version FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: organization trg_organization_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_organization_updated_at BEFORE UPDATE ON public.organization FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: robots_assessment trg_robots_assessment_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_robots_assessment_updated_at BEFORE UPDATE ON public.robots_assessment FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: source_document trg_source_document_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_source_document_updated_at BEFORE UPDATE ON public.source_document FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: source trg_source_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_source_updated_at BEFORE UPDATE ON public.source FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

--
-- Name: crawl_url crawl_url_parent_listing_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_url
    ADD CONSTRAINT crawl_url_parent_listing_id_fkey FOREIGN KEY (parent_listing_id) REFERENCES public.crawl_url(id);

--
-- Name: crawl_url crawl_url_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_url
    ADD CONSTRAINT crawl_url_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);

--
-- Name: duplicate_cluster duplicate_cluster_canonical_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duplicate_cluster
    ADD CONSTRAINT duplicate_cluster_canonical_opportunity_id_fkey FOREIGN KEY (canonical_opportunity_id) REFERENCES public.opportunity(id);

--
-- Name: duplicate_member duplicate_member_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duplicate_member
    ADD CONSTRAINT duplicate_member_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.duplicate_cluster(id);

--
-- Name: duplicate_member duplicate_member_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.duplicate_member
    ADD CONSTRAINT duplicate_member_opportunity_id_fkey FOREIGN KEY (opportunity_id) REFERENCES public.opportunity(id);

--
-- Name: extraction_run extraction_run_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_run
    ADD CONSTRAINT extraction_run_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id);

--
-- Name: fetch fetch_crawl_url_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."fetch"
    ADD CONSTRAINT fetch_crawl_url_id_fkey FOREIGN KEY (crawl_url_id) REFERENCES public.crawl_url(id);

--
-- Name: fetch fetch_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."fetch"
    ADD CONSTRAINT fetch_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);

--
-- Name: field_assertion field_assertion_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_assertion
    ADD CONSTRAINT field_assertion_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_run(id);

--
-- Name: field_assertion field_assertion_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_assertion
    ADD CONSTRAINT field_assertion_opportunity_id_fkey FOREIGN KEY (opportunity_id) REFERENCES public.opportunity(id);

--
-- Name: field_assertion field_assertion_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.field_assertion
    ADD CONSTRAINT field_assertion_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id);

--
-- Name: opportunity_version opportunity_version_caused_by_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunity_version
    ADD CONSTRAINT opportunity_version_caused_by_extraction_run_id_fkey FOREIGN KEY (caused_by_extraction_run_id) REFERENCES public.extraction_run(id);

--
-- Name: opportunity_version opportunity_version_opportunity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.opportunity_version
    ADD CONSTRAINT opportunity_version_opportunity_id_fkey FOREIGN KEY (opportunity_id) REFERENCES public.opportunity(id);

--
-- Name: pipeline_run pipeline_run_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_run
    ADD CONSTRAINT pipeline_run_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);

--
-- Name: robots_assessment robots_assessment_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robots_assessment
    ADD CONSTRAINT robots_assessment_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);

--
-- Name: source_document source_document_fetch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_document
    ADD CONSTRAINT source_document_fetch_id_fkey FOREIGN KEY (fetch_id) REFERENCES public."fetch"(id);

--
-- Name: source_document source_document_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_document
    ADD CONSTRAINT source_document_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id);

--
-- PostgreSQL database dump complete
--
