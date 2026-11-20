-- =============================================================================
-- 004_row_security.sql
--
-- Sign-in identity and Postgres row-level security.
--
--   * users.auth_issuer / auth_subject: the identity provider's stable id for
--     a person (email can change)
--   * schedule_runs.requested_by: who started a run
--   * role skeddie_app: the API switches to it at the start of every
--     transaction (SET LOCAL ROLE), so row security applies even when the API
--     connects as a superuser
--   * skeddie.user_id: the signed-in user for the current transaction; every
--     policy below lets skeddie_app see and change only rows of organizations
--     that user administers. With no user set, policies return nothing.
--   * SECURITY DEFINER functions for the few operations that must cross
--     organizations: resolving a sign-in, startup recovery of interrupted
--     runs, and the booking-sync triggers
--
-- HOW TO RUN (pgAdmin): open a Query Tool on the skeddie database and execute
-- the whole file. It runs in one transaction: if any check fails, nothing is
-- applied (run ROLLBACK; if pgAdmin reports the transaction as aborted).
-- Creating the role needs a superuser or CREATEROLE.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '003_schedule_runs') THEN
        RAISE EXCEPTION '004_row_security needs 003_schedule_runs to be applied first';
    END IF;
    IF EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '004_row_security') THEN
        RAISE EXCEPTION '004_row_security has already been applied to this database';
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 1. Sign-in identity
-- -----------------------------------------------------------------------------

ALTER TABLE public.users
    ADD COLUMN auth_issuer text,
    ADD COLUMN auth_subject text,
    ADD CONSTRAINT users_auth_identity_key UNIQUE (auth_issuer, auth_subject),
    ADD CONSTRAINT users_auth_identity_check CHECK ((auth_issuer IS NULL) = (auth_subject IS NULL));

ALTER TABLE public.schedule_runs
    ADD COLUMN requested_by uuid REFERENCES public.users (id) ON DELETE SET NULL;

-- -----------------------------------------------------------------------------
-- 2. Application role (roles are shared by every database on the server)
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'skeddie_app') THEN
        CREATE ROLE skeddie_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO skeddie_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.coaches, public.officials, public.venues, public.fields, public.seasons, public.divisions,
    public.teams, public.team_coaches, public.events, public.event_officials, public.slot_rules,
    public.time_slots, public.unavailabilities, public.schedule_runs
    TO skeddie_app;
GRANT SELECT, UPDATE ON public.organizations TO skeddie_app;
GRANT SELECT ON
    public.admins, public.users, public.sports, public.schema_migrations,
    public.team_bookings, public.coach_bookings, public.official_bookings
    TO skeddie_app;

-- -----------------------------------------------------------------------------
-- 3. Who is calling
-- -----------------------------------------------------------------------------

CREATE FUNCTION public.app_user_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('skeddie.user_id', true), '')::uuid $$;

-- SECURITY DEFINER so policies can read admins without recursing into the
-- admins policy. Policies use it as an uncorrelated IN (SELECT ...), which
-- Postgres evaluates once per query, not once per row.
CREATE FUNCTION public.app_admin_org_ids() RETURNS SETOF uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT organization_id FROM public.admins WHERE user_id = public.app_user_id();
$$;

-- -----------------------------------------------------------------------------
-- 4. Row-level security
-- -----------------------------------------------------------------------------

ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON public.organizations TO skeddie_app
    USING (id IN (SELECT public.app_admin_org_ids()))
    WITH CHECK (id IN (SELECT public.app_admin_org_ids()));

DO $$
DECLARE
    tenant_table text;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY[
        'admins', 'coaches', 'officials', 'venues', 'fields', 'seasons', 'divisions', 'teams',
        'team_coaches', 'events', 'event_officials', 'slot_rules', 'time_slots', 'unavailabilities',
        'schedule_runs'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON public.%I TO skeddie_app '
            'USING (organization_id IN (SELECT public.app_admin_org_ids())) '
            'WITH CHECK (organization_id IN (SELECT public.app_admin_org_ids()))',
            tenant_table
        );
    END LOOP;
END
$$;

-- Users: yourself, and the admins, coaches and officials of your organizations
-- (those tables are themselves limited to your organizations).
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
CREATE POLICY visible_users ON public.users FOR SELECT TO skeddie_app
    USING (
        id = public.app_user_id()
        OR EXISTS (SELECT 1 FROM public.admins a WHERE a.user_id = users.id)
        OR EXISTS (SELECT 1 FROM public.coaches c WHERE c.user_id = users.id)
        OR EXISTS (SELECT 1 FROM public.officials o WHERE o.user_id = users.id)
    );

-- Bookings: only for events you can see. They are written by the sync
-- functions below, never by skeddie_app directly.
DO $$
DECLARE
    booking_table text;
BEGIN
    FOREACH booking_table IN ARRAY ARRAY['team_bookings', 'coach_bookings', 'official_bookings']
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', booking_table);
        EXECUTE format(
            'CREATE POLICY visible_bookings ON public.%I FOR SELECT TO skeddie_app '
            'USING (EXISTS (SELECT 1 FROM public.events e WHERE e.id = event_id))',
            booking_table
        );
    END LOOP;
END
$$;

-- -----------------------------------------------------------------------------
-- 5. Privileged operations
-- -----------------------------------------------------------------------------

-- Maps a verified sign-in to a Skeddie user. A first sign-in is linked to an
-- existing, not yet linked user with the same verified email. There is no
-- self-signup: unknown people get NULL.
CREATE FUNCTION public.resolve_user(p_issuer text, p_subject text, p_email text, p_email_verified boolean)
    RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
DECLARE
    v_user_id uuid;
BEGIN
    SELECT id INTO v_user_id FROM public.users WHERE auth_issuer = p_issuer AND auth_subject = p_subject;
    IF v_user_id IS NOT NULL THEN
        RETURN v_user_id;
    END IF;
    IF p_email IS NULL OR NOT coalesce(p_email_verified, false) THEN
        RETURN NULL;
    END IF;

    UPDATE public.users
       SET auth_issuer = p_issuer, auth_subject = p_subject, updated_at = now()
     WHERE lower(email) = lower(p_email) AND auth_subject IS NULL
    RETURNING id INTO v_user_id;
    RETURN v_user_id;
END;
$$;

-- Startup recovery: runs execute inside the API process, so any left queued
-- or running across all organizations were interrupted by a restart.
CREATE FUNCTION public.fail_interrupted_runs(p_message text) RETURNS integer
    LANGUAGE sql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    WITH failed AS (
        UPDATE public.schedule_runs
           SET status = 'error', error = p_message, finished_at = now(), updated_at = now()
         WHERE status IN ('queued', 'running')
        RETURNING 1
    )
    SELECT count(*)::integer FROM failed;
$$;

-- The booking tables have no write grants for skeddie_app; the triggers keep
-- them in sync by running these functions with their owner's rights.
ALTER FUNCTION public.sync_event_bookings(uuid) SECURITY DEFINER SET search_path = pg_catalog, public;
ALTER FUNCTION public.sync_team_event_bookings(uuid) SECURITY DEFINER SET search_path = pg_catalog, public;

REVOKE EXECUTE ON FUNCTION
    public.app_admin_org_ids(),
    public.resolve_user(text, text, text, boolean),
    public.fail_interrupted_runs(text),
    public.sync_event_bookings(uuid),
    public.sync_team_event_bookings(uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    public.app_admin_org_ids(),
    public.resolve_user(text, text, text, boolean),
    public.fail_interrupted_runs(text),
    public.sync_event_bookings(uuid),
    public.sync_team_event_bookings(uuid)
    TO skeddie_app;

-- -----------------------------------------------------------------------------
-- 6. Documentation
-- -----------------------------------------------------------------------------

COMMENT ON COLUMN public.users.auth_subject IS
    'The identity provider''s id for this person (the token''s sub claim), set on first sign-in.';
COMMENT ON COLUMN public.schedule_runs.requested_by IS
    'The admin who started the run; NULL for runs created before 004.';
COMMENT ON FUNCTION public.app_admin_org_ids() IS
    'Organizations the current user (skeddie.user_id) administers; used by every row-security policy.';

INSERT INTO public.schema_migrations (version) VALUES ('004_row_security');

COMMIT;
