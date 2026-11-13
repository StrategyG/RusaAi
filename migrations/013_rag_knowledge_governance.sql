-- Source rights, independent review, human approval and publication gate.
-- Reviewer/approver/publisher logins must be explicitly granted membership by a DBA.
BEGIN;

DO $rusa$
DECLARE role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'rusa_knowledge_reviewer',
        'rusa_knowledge_approver',
        'rusa_knowledge_publisher'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
                role_name
            );
        ELSE
            EXECUTE format(
                'ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
                role_name
            );
        END IF;
    END LOOP;
END;
$rusa$;

CREATE UNIQUE INDEX IF NOT EXISTS rusa_knowledge_versions_document_identity_uq
    ON public.rusa_knowledge_document_versions (tenant_id, document_id, version_id);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_rights_attestations (
    tenant_id TEXT NOT NULL,
    rights_id BIGINT GENERATED ALWAYS AS IDENTITY,
    document_id UUID NOT NULL,
    version_id UUID NOT NULL,
    rights_status TEXT NOT NULL,
    source_owner TEXT NOT NULL,
    license_id TEXT NOT NULL,
    internal_storage_allowed BOOLEAN NOT NULL,
    extraction_allowed BOOLEAN NOT NULL,
    derivative_allowed BOOLEAN NOT NULL,
    embedding_allowed BOOLEAN NOT NULL,
    retrieval_allowed BOOLEAN NOT NULL,
    attribution_required BOOLEAN NOT NULL DEFAULT FALSE,
    attribution_text TEXT,
    retention_policy TEXT NOT NULL,
    evidence_uri TEXT NOT NULL,
    attested_by TEXT NOT NULL DEFAULT session_user,
    attested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, rights_id),
    CONSTRAINT rusa_knowledge_rights_version_fk
        FOREIGN KEY (tenant_id, document_id, version_id)
        REFERENCES public.rusa_knowledge_document_versions (tenant_id, document_id, version_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_rights_status_check
        CHECK (rights_status IN ('unknown', 'cleared', 'restricted', 'denied')),
    CONSTRAINT rusa_knowledge_rights_cleared_check
        CHECK (
            rights_status <> 'cleared'
            OR (
                length(trim(source_owner)) > 0
                AND length(trim(license_id)) > 0
                AND internal_storage_allowed
                AND extraction_allowed
                AND derivative_allowed
                AND embedding_allowed
                AND retrieval_allowed
                AND length(trim(evidence_uri)) > 0
                AND length(trim(retention_policy)) > 0
                AND (NOT attribution_required OR length(trim(COALESCE(attribution_text, ''))) > 0)
            )
        ),
    CONSTRAINT rusa_knowledge_rights_text_bounds_check
        CHECK (
            length(source_owner) <= 512
            AND length(license_id) <= 256
            AND length(retention_policy) <= 2048
            AND length(evidence_uri) <= 2048
            AND (attribution_text IS NULL OR length(attribution_text) <= 2048)
            AND length(attested_by) BETWEEN 1 AND 256
        )
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_rights_version_idx
    ON public.rusa_knowledge_rights_attestations
       (tenant_id, document_id, version_id, rights_id DESC);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_review_findings (
    tenant_id TEXT NOT NULL,
    review_id BIGINT GENERATED ALWAYS AS IDENTITY,
    document_id UUID NOT NULL,
    version_id UUID NOT NULL,
    review_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    finding_text TEXT NOT NULL,
    evidence_uri TEXT,
    reviewer_principal TEXT NOT NULL DEFAULT session_user,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, review_id),
    CONSTRAINT rusa_knowledge_reviews_version_fk
        FOREIGN KEY (tenant_id, document_id, version_id)
        REFERENCES public.rusa_knowledge_document_versions (tenant_id, document_id, version_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_reviews_type_check
        CHECK (review_type IN ('technical', 'source_fidelity', 'security', 'qa')),
    CONSTRAINT rusa_knowledge_reviews_decision_check
        CHECK (decision IN ('passed', 'needs_changes', 'failed')),
    CONSTRAINT rusa_knowledge_reviews_finding_check
        CHECK (length(trim(finding_text)) BETWEEN 1 AND 10000),
    CONSTRAINT rusa_knowledge_reviews_text_bounds_check
        CHECK (
            (evidence_uri IS NULL OR length(evidence_uri) <= 2048)
            AND length(reviewer_principal) BETWEEN 1 AND 256
        )
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_reviews_version_idx
    ON public.rusa_knowledge_review_findings
       (tenant_id, document_id, version_id, review_type, review_id DESC);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_human_approvals (
    tenant_id TEXT NOT NULL,
    approval_id BIGINT GENERATED ALWAYS AS IDENTITY,
    document_id UUID NOT NULL,
    version_id UUID NOT NULL,
    decision TEXT NOT NULL,
    approval_reference TEXT NOT NULL,
    approver_principal TEXT NOT NULL DEFAULT session_user,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, approval_id),
    CONSTRAINT rusa_knowledge_approvals_version_fk
        FOREIGN KEY (tenant_id, document_id, version_id)
        REFERENCES public.rusa_knowledge_document_versions (tenant_id, document_id, version_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_approvals_decision_check
        CHECK (decision IN ('approved', 'rejected', 'revoked')),
    CONSTRAINT rusa_knowledge_approvals_reference_check
        CHECK (length(trim(approval_reference)) BETWEEN 1 AND 512),
    CONSTRAINT rusa_knowledge_approvals_actor_check
        CHECK (length(approver_principal) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_approvals_version_idx
    ON public.rusa_knowledge_human_approvals
       (tenant_id, document_id, version_id, approval_id DESC);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_publication_events (
    tenant_id TEXT NOT NULL,
    publication_event_id BIGINT GENERATED ALWAYS AS IDENTITY,
    document_id UUID NOT NULL,
    version_id UUID NOT NULL,
    action TEXT NOT NULL,
    manifest_uri TEXT,
    manifest_sha256 CHAR(64),
    reason TEXT,
    publisher_principal TEXT NOT NULL DEFAULT session_user,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, publication_event_id),
    CONSTRAINT rusa_knowledge_publication_version_fk
        FOREIGN KEY (tenant_id, document_id, version_id)
        REFERENCES public.rusa_knowledge_document_versions (tenant_id, document_id, version_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_publication_action_check
        CHECK (action IN ('published', 'withdrawn')),
    CONSTRAINT rusa_knowledge_publication_manifest_check
        CHECK (
            (action = 'published'
             AND manifest_uri IS NOT NULL
             AND length(trim(manifest_uri)) BETWEEN 1 AND 2048
             AND manifest_sha256 IS NOT NULL
             AND length(manifest_sha256) = 64
             AND manifest_sha256 !~ '[^0-9a-f]')
            OR
            (action = 'withdrawn'
             AND length(trim(COALESCE(reason, ''))) BETWEEN 1 AND 2048)
        ),
    CONSTRAINT rusa_knowledge_publication_actor_check
        CHECK (length(publisher_principal) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_publication_latest_idx
    ON public.rusa_knowledge_publication_events
       (tenant_id, document_id, version_id, publication_event_id DESC);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_audit_events (
    tenant_id TEXT NOT NULL,
    audit_event_id BIGINT GENERATED ALWAYS AS IDENTITY,
    actor_principal TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, audit_event_id),
    CONSTRAINT rusa_knowledge_audit_details_check
        CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_audit_resource_idx
    ON public.rusa_knowledge_audit_events
       (tenant_id, resource_type, resource_id, audit_event_id DESC);

ALTER TABLE public.rusa_knowledge_rights_attestations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_rights_attestations FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_review_findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_review_findings FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_human_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_human_approvals FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_publication_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_publication_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rusa_knowledge_rights_tenant_isolation
    ON public.rusa_knowledge_rights_attestations;
CREATE POLICY rusa_knowledge_rights_tenant_isolation
    ON public.rusa_knowledge_rights_attestations
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_reviews_tenant_isolation
    ON public.rusa_knowledge_review_findings;
CREATE POLICY rusa_knowledge_reviews_tenant_isolation
    ON public.rusa_knowledge_review_findings
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_approvals_tenant_isolation
    ON public.rusa_knowledge_human_approvals;
CREATE POLICY rusa_knowledge_approvals_tenant_isolation
    ON public.rusa_knowledge_human_approvals
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_publications_tenant_isolation
    ON public.rusa_knowledge_publication_events;
CREATE POLICY rusa_knowledge_publications_tenant_isolation
    ON public.rusa_knowledge_publication_events
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_audit_tenant_isolation
    ON public.rusa_knowledge_audit_events;
CREATE POLICY rusa_knowledge_audit_tenant_isolation
    ON public.rusa_knowledge_audit_events
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

GRANT USAGE ON SCHEMA public TO
    rusa_knowledge_reviewer, rusa_knowledge_approver, rusa_knowledge_publisher;

REVOKE ALL PRIVILEGES ON
    public.rusa_knowledge_rights_attestations,
    public.rusa_knowledge_review_findings,
    public.rusa_knowledge_human_approvals,
    public.rusa_knowledge_publication_events,
    public.rusa_knowledge_audit_events
FROM PUBLIC;

REVOKE UPDATE, DELETE, TRUNCATE ON
    public.rusa_knowledge_rights_attestations,
    public.rusa_knowledge_review_findings,
    public.rusa_knowledge_human_approvals,
    public.rusa_knowledge_publication_events,
    public.rusa_knowledge_audit_events
FROM rusa_knowledge_worker, rusa_knowledge_reviewer,
     rusa_knowledge_approver, rusa_knowledge_publisher;

GRANT SELECT ON
    public.rusa_knowledge_documents,
    public.rusa_knowledge_chunks,
    public.rusa_knowledge_document_versions,
    public.rusa_knowledge_version_chunks
TO rusa_knowledge_reviewer, rusa_knowledge_approver, rusa_knowledge_publisher;

GRANT SELECT, INSERT ON public.rusa_knowledge_rights_attestations
TO rusa_knowledge_reviewer;
GRANT USAGE, SELECT ON SEQUENCE public.rusa_knowledge_rights_attestations_rights_id_seq
TO rusa_knowledge_reviewer;

GRANT SELECT, INSERT ON public.rusa_knowledge_review_findings
TO rusa_knowledge_reviewer;
GRANT USAGE, SELECT ON SEQUENCE public.rusa_knowledge_review_findings_review_id_seq
TO rusa_knowledge_reviewer;

GRANT SELECT ON public.rusa_knowledge_rights_attestations,
    public.rusa_knowledge_review_findings
TO rusa_knowledge_approver, rusa_knowledge_publisher;

GRANT SELECT, INSERT ON public.rusa_knowledge_human_approvals
TO rusa_knowledge_approver;
GRANT USAGE, SELECT ON SEQUENCE public.rusa_knowledge_human_approvals_approval_id_seq
TO rusa_knowledge_approver;

GRANT SELECT ON public.rusa_knowledge_human_approvals,
    public.rusa_knowledge_publication_events
TO rusa_knowledge_reviewer, rusa_knowledge_approver, rusa_knowledge_publisher;

GRANT SELECT ON public.rusa_knowledge_publication_events
TO rusa_knowledge_worker;

GRANT SELECT ON public.rusa_knowledge_audit_events
TO rusa_knowledge_reviewer, rusa_knowledge_approver, rusa_knowledge_publisher;


CREATE OR REPLACE FUNCTION public.rusa_knowledge_assert_governance_principal(
    p_principal NAME,
    p_expected_role NAME
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $rusa$
DECLARE
    v_principal_oid OID;
    v_expected_role_oid OID;
BEGIN
    v_principal_oid := pg_catalog.to_regrole(p_principal::text)::oid;
    v_expected_role_oid := pg_catalog.to_regrole(p_expected_role::text)::oid;
    IF v_principal_oid IS NULL OR v_expected_role_oid IS NULL THEN
        RAISE EXCEPTION 'governance principal or assigned role is not configured'
            USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles r
        WHERE r.oid = v_principal_oid
          AND r.rolcanlogin
          AND r.rolinherit
          AND NOT r.rolsuper
          AND NOT r.rolbypassrls
          AND NOT r.rolcreaterole
          AND NOT r.rolcreatedb
          AND NOT r.rolreplication
    ) THEN
        RAISE EXCEPTION 'governance principal must be a least-privileged LOGIN role'
            USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles r
        WHERE r.oid = v_expected_role_oid
          AND NOT r.rolcanlogin
          AND NOT r.rolsuper
          AND NOT r.rolbypassrls
          AND NOT r.rolcreaterole
          AND NOT r.rolcreatedb
          AND NOT r.rolreplication
    ) THEN
        RAISE EXCEPTION 'governance group role must remain a least-privileged NOLOGIN role'
            USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members m
        WHERE m.member = v_principal_oid
          AND m.roleid = v_expected_role_oid
          AND NOT m.admin_option
    ) THEN
        RAISE EXCEPTION 'governance LOGIN must be a direct member of its assigned role without admin option'
            USING ERRCODE = '42501';
    END IF;

    -- Recheck the complete transitive membership closure. A governance login
    -- must inherit only its assigned NOLOGIN role, including at decision time.
    IF EXISTS (
        WITH RECURSIVE inherited_roles(role_oid) AS (
            SELECT m.roleid
            FROM pg_catalog.pg_auth_members m
            WHERE m.member = v_principal_oid
            UNION
            SELECT m.roleid
            FROM pg_catalog.pg_auth_members m
            JOIN inherited_roles inherited
              ON m.member = inherited.role_oid
        )
        SELECT 1
        FROM inherited_roles
        WHERE role_oid <> v_expected_role_oid
    ) THEN
        RAISE EXCEPTION 'governance LOGIN must inherit only its assigned role'
            USING ERRCODE = '42501';
    END IF;
END
$rusa$;

REVOKE ALL ON FUNCTION public.rusa_knowledge_assert_governance_principal(NAME, NAME)
    FROM PUBLIC, rusa_knowledge_worker;
GRANT EXECUTE ON FUNCTION public.rusa_knowledge_assert_governance_principal(NAME, NAME)
    TO rusa_knowledge_reviewer, rusa_knowledge_approver, rusa_knowledge_publisher;


CREATE OR REPLACE FUNCTION public.rusa_knowledge_governance_set_actor()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $rusa$
BEGIN
    -- Serialize governance events per version so sequence allocation order
    -- cannot misidentify the latest committed evidence under concurrency.
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            NEW.tenant_id || ':' || NEW.document_id::text || ':' || NEW.version_id::text,
            0
        )
    );

    IF TG_TABLE_NAME = 'rusa_knowledge_rights_attestations' THEN
        PERFORM public.rusa_knowledge_assert_governance_principal(
            session_user, 'rusa_knowledge_reviewer'
        );
        NEW.attested_by := session_user;
        NEW.attested_at := clock_timestamp();
    ELSIF TG_TABLE_NAME = 'rusa_knowledge_review_findings' THEN
        PERFORM public.rusa_knowledge_assert_governance_principal(
            session_user, 'rusa_knowledge_reviewer'
        );
        NEW.reviewer_principal := session_user;
        NEW.created_at := clock_timestamp();
    ELSIF TG_TABLE_NAME = 'rusa_knowledge_human_approvals' THEN
        PERFORM public.rusa_knowledge_assert_governance_principal(
            session_user, 'rusa_knowledge_approver'
        );
        NEW.approver_principal := session_user;
        NEW.created_at := clock_timestamp();
    ELSIF TG_TABLE_NAME = 'rusa_knowledge_publication_events' THEN
        PERFORM public.rusa_knowledge_assert_governance_principal(
            session_user, 'rusa_knowledge_publisher'
        );
        NEW.publisher_principal := session_user;
        NEW.created_at := clock_timestamp();
    ELSE
        RAISE EXCEPTION 'unsupported knowledge governance actor table';
    END IF;
    RETURN NEW;
END;
$rusa$;


CREATE OR REPLACE FUNCTION public.rusa_knowledge_governance_audit_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $rusa$
DECLARE
    v_record JSONB := to_jsonb(NEW);
    v_resource_id TEXT;
BEGIN
    v_resource_id := COALESCE(
        v_record->>'rights_id',
        v_record->>'review_id',
        v_record->>'approval_id',
        v_record->>'publication_event_id',
        'unknown'
    );
    INSERT INTO public.rusa_knowledge_audit_events (
        tenant_id, actor_principal, actor_role, action,
        resource_type, resource_id, outcome, details
    ) VALUES (
        NEW.tenant_id, session_user,
        COALESCE(NULLIF(current_setting('role', true), 'none'), current_user),
        TG_TABLE_NAME || '.insert', TG_TABLE_NAME, v_resource_id,
        'recorded', v_record - 'tenant_id'
    );
    RETURN NEW;
END;
$rusa$;

REVOKE ALL ON FUNCTION public.rusa_knowledge_governance_audit_insert() FROM PUBLIC;

DROP TRIGGER IF EXISTS rusa_knowledge_rights_audit_insert
    ON public.rusa_knowledge_rights_attestations;
CREATE TRIGGER rusa_knowledge_rights_audit_insert
AFTER INSERT ON public.rusa_knowledge_rights_attestations
FOR EACH ROW EXECUTE FUNCTION public.rusa_knowledge_governance_audit_insert();

DROP TRIGGER IF EXISTS rusa_knowledge_review_audit_insert
    ON public.rusa_knowledge_review_findings;
CREATE TRIGGER rusa_knowledge_review_audit_insert
AFTER INSERT ON public.rusa_knowledge_review_findings
FOR EACH ROW EXECUTE FUNCTION public.rusa_knowledge_governance_audit_insert();

DROP TRIGGER IF EXISTS rusa_knowledge_approval_audit_insert
    ON public.rusa_knowledge_human_approvals;
CREATE TRIGGER rusa_knowledge_approval_audit_insert
AFTER INSERT ON public.rusa_knowledge_human_approvals
FOR EACH ROW EXECUTE FUNCTION public.rusa_knowledge_governance_audit_insert();

DROP TRIGGER IF EXISTS rusa_knowledge_publication_audit_insert
    ON public.rusa_knowledge_publication_events;
CREATE TRIGGER rusa_knowledge_publication_audit_insert
AFTER INSERT ON public.rusa_knowledge_publication_events
FOR EACH ROW EXECUTE FUNCTION public.rusa_knowledge_governance_audit_insert();


CREATE OR REPLACE FUNCTION public.rusa_knowledge_version_is_published(
    p_tenant_id TEXT,
    p_document_id UUID,
    p_version_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $rusa$
DECLARE
    v_publication_action TEXT;
    v_publisher_principal TEXT;
    v_publication_created_at TIMESTAMPTZ;
    v_rights RECORD;
    v_review_type TEXT;
    v_review_decision TEXT;
    v_review_created_at TIMESTAMPTZ;
    v_approval_decision TEXT;
    v_approver_principal TEXT;
    v_approval_created_at TIMESTAMPTZ;
    v_latest_preapproval_evidence_at TIMESTAMPTZ;
BEGIN
    IF current_setting('rusa.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RETURN FALSE;
    END IF;

    PERFORM 1
    FROM public.rusa_knowledge_document_versions v
    JOIN public.rusa_knowledge_documents d
      ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
    WHERE v.tenant_id=p_tenant_id
      AND v.document_id=p_document_id
      AND v.version_id=p_version_id
      AND d.current_version_id=v.version_id
      AND v.version_number = (
          SELECT MAX(v_latest.version_number)
          FROM public.rusa_knowledge_document_versions v_latest
          WHERE v_latest.tenant_id=v.tenant_id
            AND v_latest.document_id=v.document_id
      );
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    SELECT action, publisher_principal, created_at
    INTO v_publication_action, v_publisher_principal, v_publication_created_at
    FROM public.rusa_knowledge_publication_events
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY created_at DESC, publication_event_id DESC
    LIMIT 1;
    IF NOT FOUND OR v_publication_action <> 'published' THEN
        RETURN FALSE;
    END IF;

    SELECT rights_status, internal_storage_allowed, extraction_allowed,
           derivative_allowed, embedding_allowed, retrieval_allowed, attested_at
    INTO v_rights
    FROM public.rusa_knowledge_rights_attestations
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY attested_at DESC, rights_id DESC
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    IF v_rights.rights_status IS DISTINCT FROM 'cleared'
       OR NOT v_rights.internal_storage_allowed
       OR NOT v_rights.extraction_allowed
       OR NOT v_rights.derivative_allowed
       OR NOT v_rights.embedding_allowed
       OR NOT v_rights.retrieval_allowed
       OR v_rights.attested_at >= v_publication_created_at THEN
        RETURN FALSE;
    END IF;
    v_latest_preapproval_evidence_at := v_rights.attested_at;

    FOREACH v_review_type IN ARRAY ARRAY['technical', 'source_fidelity', 'security', 'qa'] LOOP
        SELECT decision, created_at
        INTO v_review_decision, v_review_created_at
        FROM public.rusa_knowledge_review_findings
        WHERE tenant_id=p_tenant_id
          AND document_id=p_document_id
          AND version_id=p_version_id
          AND review_type=v_review_type
        ORDER BY created_at DESC, review_id DESC
        LIMIT 1;
        IF NOT FOUND OR v_review_decision <> 'passed'
           OR v_review_created_at >= v_publication_created_at THEN
            RETURN FALSE;
        END IF;
        v_latest_preapproval_evidence_at := GREATEST(
            v_latest_preapproval_evidence_at, v_review_created_at
        );
    END LOOP;

    SELECT decision, approver_principal, created_at
    INTO v_approval_decision, v_approver_principal, v_approval_created_at
    FROM public.rusa_knowledge_human_approvals
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY created_at DESC, approval_id DESC
    LIMIT 1;
    IF NOT FOUND OR v_approval_decision <> 'approved'
       OR v_approver_principal IS NULL
       OR v_approver_principal = v_publisher_principal
       OR v_approval_created_at <= v_latest_preapproval_evidence_at
       OR v_approval_created_at >= v_publication_created_at THEN
        RETURN FALSE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname=v_approver_principal::name
          AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname=v_publisher_principal::name
          AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
    ) THEN
        RETURN FALSE;
    END IF;
    IF NOT pg_has_role(v_approver_principal::name, 'rusa_knowledge_approver'::name, 'member')
       OR pg_has_role(v_approver_principal::name, 'rusa_knowledge_reviewer'::name, 'member')
       OR pg_has_role(v_approver_principal::name, 'rusa_knowledge_publisher'::name, 'member')
       OR NOT pg_has_role(v_publisher_principal::name, 'rusa_knowledge_publisher'::name, 'member')
       OR pg_has_role(v_publisher_principal::name, 'rusa_knowledge_reviewer'::name, 'member')
       OR pg_has_role(v_publisher_principal::name, 'rusa_knowledge_approver'::name, 'member') THEN
        RETURN FALSE;
    END IF;

    RETURN TRUE;
END;
$rusa$;

REVOKE ALL ON FUNCTION public.rusa_knowledge_version_is_published(TEXT, UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.rusa_knowledge_version_is_published(TEXT, UUID, UUID)
TO rusa_knowledge_worker, rusa_knowledge_reviewer,
   rusa_knowledge_approver, rusa_knowledge_publisher;


CREATE OR REPLACE FUNCTION public.rusa_knowledge_version_attribution(
    p_tenant_id TEXT,
    p_document_id UUID,
    p_version_id UUID
)
RETURNS TABLE (attribution_required BOOLEAN, attribution_text TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $rusa$
BEGIN
    IF current_setting('rusa.tenant_id', true) IS DISTINCT FROM p_tenant_id
       OR NOT public.rusa_knowledge_version_is_published(
           p_tenant_id, p_document_id, p_version_id
       ) THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT r.attribution_required, r.attribution_text
    FROM public.rusa_knowledge_rights_attestations r
    WHERE r.tenant_id=p_tenant_id
      AND r.document_id=p_document_id
      AND r.version_id=p_version_id
    ORDER BY r.attested_at DESC, r.rights_id DESC
    LIMIT 1;
END;
$rusa$;

REVOKE ALL ON FUNCTION public.rusa_knowledge_version_attribution(TEXT, UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.rusa_knowledge_version_attribution(TEXT, UUID, UUID)
TO rusa_knowledge_worker, rusa_knowledge_reviewer,
   rusa_knowledge_approver, rusa_knowledge_publisher;


CREATE OR REPLACE FUNCTION public.rusa_publish_knowledge_version(
    p_tenant_id TEXT,
    p_document_id UUID,
    p_version_id UUID,
    p_manifest_uri TEXT,
    p_manifest_sha256 TEXT
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $rusa$
DECLARE
    v_rights RECORD;
    v_decision TEXT;
    v_approval_decision TEXT;
    v_approver_principal TEXT;
    v_review_type TEXT;
    v_existing RECORD;
    v_event_id BIGINT;
    v_chunk_count BIGINT;
    v_review_created_at TIMESTAMPTZ;
    v_approval_created_at TIMESTAMPTZ;
    v_latest_preapproval_evidence_at TIMESTAMPTZ;
BEGIN
    PERFORM public.rusa_knowledge_assert_governance_principal(
        session_user, 'rusa_knowledge_publisher'
    );
    IF current_setting('rusa.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION 'tenant context does not match publication request' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            p_tenant_id || ':' || p_document_id::text || ':' || p_version_id::text,
            0
        )
    );
    IF p_manifest_uri IS NULL OR length(trim(p_manifest_uri)) NOT BETWEEN 1 AND 2048
       OR p_manifest_sha256 IS NULL OR length(p_manifest_sha256) <> 64
       OR p_manifest_sha256 ~ '[^0-9a-f]' THEN
        RAISE EXCEPTION 'publication manifest URI or SHA-256 is invalid';
    END IF;

    PERFORM 1
    FROM public.rusa_knowledge_document_versions v
    JOIN public.rusa_knowledge_documents d
      ON d.tenant_id=v.tenant_id AND d.document_id=v.document_id
    WHERE v.tenant_id=p_tenant_id
      AND v.document_id=p_document_id
      AND v.version_id=p_version_id
      AND d.current_version_id=v.version_id
      AND v.version_number = (
          SELECT MAX(v_latest.version_number)
          FROM public.rusa_knowledge_document_versions v_latest
          WHERE v_latest.tenant_id=v.tenant_id
            AND v_latest.document_id=v.document_id
      );
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only the current document version can be published';
    END IF;

    SELECT COUNT(*) INTO v_chunk_count
    FROM public.rusa_knowledge_version_chunks
    WHERE tenant_id=p_tenant_id AND version_id=p_version_id;
    IF v_chunk_count < 1 THEN
        RAISE EXCEPTION 'version has no immutable chunk snapshot';
    END IF;

    SELECT * INTO v_rights
    FROM public.rusa_knowledge_rights_attestations
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY attested_at DESC, rights_id DESC
    LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source rights attestation is required before publication';
    END IF;
    IF v_rights.rights_status IS DISTINCT FROM 'cleared'
       OR NOT v_rights.internal_storage_allowed
       OR NOT v_rights.extraction_allowed
       OR NOT v_rights.derivative_allowed
       OR NOT v_rights.embedding_allowed
       OR NOT v_rights.retrieval_allowed THEN
        RAISE EXCEPTION 'latest source rights attestation does not permit publication';
    END IF;
    v_latest_preapproval_evidence_at := v_rights.attested_at;

    FOREACH v_review_type IN ARRAY ARRAY['technical', 'source_fidelity', 'security', 'qa'] LOOP
        SELECT decision, created_at INTO v_decision, v_review_created_at
        FROM public.rusa_knowledge_review_findings
        WHERE tenant_id=p_tenant_id
          AND document_id=p_document_id
          AND version_id=p_version_id
          AND review_type=v_review_type
        ORDER BY created_at DESC, review_id DESC
        LIMIT 1;
        IF NOT FOUND OR v_decision <> 'passed' THEN
            RAISE EXCEPTION 'latest % review has not passed', v_review_type;
        END IF;
        v_latest_preapproval_evidence_at := GREATEST(
            v_latest_preapproval_evidence_at, v_review_created_at
        );
    END LOOP;

    SELECT decision, approver_principal, created_at
    INTO v_approval_decision, v_approver_principal, v_approval_created_at
    FROM public.rusa_knowledge_human_approvals
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY created_at DESC, approval_id DESC
    LIMIT 1;
    IF NOT FOUND OR v_approval_decision IS DISTINCT FROM 'approved'
       OR v_approver_principal IS NULL
       OR v_approver_principal = session_user THEN
        RAISE EXCEPTION 'a distinct human approver approval is required';
    END IF;
    IF v_approval_created_at <= v_latest_preapproval_evidence_at THEN
        RAISE EXCEPTION 'human approval must follow the latest rights and review evidence';
    END IF;
    PERFORM public.rusa_knowledge_assert_governance_principal(
        v_approver_principal::name, 'rusa_knowledge_approver'::name
    );

    SELECT publication_event_id, action, manifest_sha256 INTO v_existing
    FROM public.rusa_knowledge_publication_events
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY created_at DESC, publication_event_id DESC
    LIMIT 1;
    IF FOUND THEN
        IF v_existing.action = 'published' THEN
            IF v_existing.manifest_sha256 = p_manifest_sha256
               AND public.rusa_knowledge_version_is_published(
                   p_tenant_id, p_document_id, p_version_id
               ) THEN
                RETURN v_existing.publication_event_id;
            END IF;
            RAISE EXCEPTION 'withdraw the stale or existing publication before publishing again';
        END IF;
    END IF;

    INSERT INTO public.rusa_knowledge_publication_events (
        tenant_id, document_id, version_id, action,
        manifest_uri, manifest_sha256, publisher_principal
    ) VALUES (
        p_tenant_id, p_document_id, p_version_id, 'published',
        p_manifest_uri, p_manifest_sha256, session_user
    )
    RETURNING publication_event_id INTO v_event_id;
    IF NOT public.rusa_knowledge_version_is_published(
        p_tenant_id, p_document_id, p_version_id
    ) THEN
        RAISE EXCEPTION 'publication failed its post-insert governance eligibility check';
    END IF;
    RETURN v_event_id;
END;
$rusa$;

CREATE OR REPLACE FUNCTION public.rusa_withdraw_knowledge_version(
    p_tenant_id TEXT,
    p_document_id UUID,
    p_version_id UUID,
    p_reason TEXT
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $rusa$
DECLARE
    v_latest RECORD;
    v_event_id BIGINT;
BEGIN
    PERFORM public.rusa_knowledge_assert_governance_principal(
        session_user, 'rusa_knowledge_publisher'
    );
    IF current_setting('rusa.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION 'tenant context does not match withdrawal request' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            p_tenant_id || ':' || p_document_id::text || ':' || p_version_id::text,
            0
        )
    );
    IF p_reason IS NULL OR length(trim(p_reason)) NOT BETWEEN 1 AND 2048 THEN
        RAISE EXCEPTION 'withdrawal reason is required';
    END IF;

    SELECT publication_event_id, action INTO v_latest
    FROM public.rusa_knowledge_publication_events
    WHERE tenant_id=p_tenant_id
      AND document_id=p_document_id
      AND version_id=p_version_id
    ORDER BY created_at DESC, publication_event_id DESC
    LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'version is not currently published';
    END IF;
    IF v_latest.action <> 'published' THEN
        RAISE EXCEPTION 'version is not currently published';
    END IF;

    INSERT INTO public.rusa_knowledge_publication_events (
        tenant_id, document_id, version_id, action, reason, publisher_principal
    ) VALUES (
        p_tenant_id, p_document_id, p_version_id, 'withdrawn', p_reason, session_user
    )
    RETURNING publication_event_id INTO v_event_id;
    RETURN v_event_id;
END;
$rusa$;

REVOKE ALL ON FUNCTION public.rusa_publish_knowledge_version(TEXT, UUID, UUID, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.rusa_withdraw_knowledge_version(TEXT, UUID, UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.rusa_publish_knowledge_version(TEXT, UUID, UUID, TEXT, TEXT)
TO rusa_knowledge_publisher;
GRANT EXECUTE ON FUNCTION public.rusa_withdraw_knowledge_version(TEXT, UUID, UUID, TEXT)
TO rusa_knowledge_publisher;

COMMIT;
