-- =============================================================================
-- 001_integrity.sql
--
-- Makes double-booking impossible at the database level and stops rows from
-- pointing across organizations, seasons or divisions.
--
--   * organizations.timezone (IANA name, required)
--   * team_bookings / coach_bookings / official_bookings: one row per
--     participant per scheduled game, with GiST exclusion constraints so the
--     same team, coach or official can never hold two overlapping games.
--     These tables are maintained by triggers and cannot be written directly.
--   * composite foreign keys so a child row's organization / season /
--     division always matches its parent's
--   * deferrable slot constraints so two games can swap slots in one
--     transaction
--
-- HOW TO RUN (pgAdmin): open a Query Tool on the skeddie database and execute
-- the whole file. It runs in one transaction: if any check fails, nothing is
-- applied (run ROLLBACK; if pgAdmin reports the transaction as aborted).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Migration tracking
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);

-- The existing dev database was built by hand; record the baseline as applied.
INSERT INTO public.schema_migrations (version) VALUES ('000_baseline') ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '001_integrity') THEN
        RAISE EXCEPTION '001_integrity has already been applied to this database';
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 1. Preflight: refuse to run if existing data already breaks the new rules
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    r record;
    problems text := '';
BEGIN
    FOR r IN
        SELECT 'seasons with end_date before start_date' AS problem, count(*) AS n
          FROM public.seasons WHERE end_date < start_date
        UNION ALL
        SELECT 'divisions with games_per_team or max_games_per_week <= 0', count(*)
          FROM public.divisions WHERE games_per_team <= 0 OR max_games_per_week <= 0
        UNION ALL
        SELECT 'divisions whose season belongs to another organization', count(*)
          FROM public.divisions d JOIN public.seasons s ON s.id = d.season_id
         WHERE s.organization_id <> d.organization_id
        UNION ALL
        SELECT 'teams whose division is in another season or organization', count(*)
          FROM public.teams t JOIN public.divisions d ON d.id = t.division_id
         WHERE d.season_id <> t.season_id OR d.organization_id <> t.organization_id
        UNION ALL
        SELECT 'fields whose venue belongs to another organization', count(*)
          FROM public.fields f JOIN public.venues v ON v.id = f.venue_id
         WHERE v.organization_id <> f.organization_id
        UNION ALL
        SELECT 'slot_rules whose season, field or division do not match', count(*)
          FROM public.slot_rules sr
          JOIN public.seasons s ON s.id = sr.season_id
          JOIN public.fields f ON f.id = sr.field_id
          LEFT JOIN public.divisions d ON d.id = sr.division_id
         WHERE s.organization_id <> sr.organization_id
            OR f.organization_id <> sr.organization_id
            OR (d.id IS NOT NULL AND d.season_id <> sr.season_id)
        UNION ALL
        SELECT 'events whose teams, division or season do not match', count(*)
          FROM public.events e
          JOIN public.divisions d ON d.id = e.division_id
          JOIN public.teams h ON h.id = e.home_team_id
          JOIN public.teams a ON a.id = e.away_team_id
         WHERE d.season_id <> e.season_id OR d.organization_id <> e.organization_id
            OR h.division_id <> e.division_id OR a.division_id <> e.division_id
        UNION ALL
        SELECT 'time_slots whose season, field, division or event do not match', count(*)
          FROM public.time_slots ts
          JOIN public.seasons s ON s.id = ts.season_id
          JOIN public.fields f ON f.id = ts.field_id
          LEFT JOIN public.divisions d ON d.id = ts.division_id
          LEFT JOIN public.events e ON e.id = ts.event_id
         WHERE s.organization_id <> ts.organization_id
            OR f.organization_id <> ts.organization_id
            OR (d.id IS NOT NULL AND d.season_id <> ts.season_id)
            OR (e.id IS NOT NULL AND e.season_id <> ts.season_id)
        UNION ALL
        SELECT 'team_coaches linking a team and a coach from different organizations', count(*)
          FROM public.team_coaches tc
          JOIN public.teams t ON t.id = tc.team_id
          JOIN public.coaches c ON c.id = tc.coach_id
         WHERE t.organization_id <> c.organization_id
        UNION ALL
        SELECT 'event_officials linking an event and an official from different organizations', count(*)
          FROM public.event_officials eo
          JOIN public.events e ON e.id = eo.event_id
          JOIN public.officials o ON o.id = eo.official_id
         WHERE e.organization_id <> o.organization_id
    LOOP
        IF r.n > 0 THEN
            problems := problems || format(E'\n  - %s: %s row(s)', r.problem, r.n);
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION 'Existing data violates the new integrity rules; fix these rows first:%', problems;
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 2. Organization timezone
-- -----------------------------------------------------------------------------

-- Accepts IANA zone names ('America/New_York', 'UTC'). Rejects abbreviations
-- and POSIX strings such as 'EST' or 'UTC+5', which Python's zoneinfo cannot
-- load and which hide daylight-saving rules.
CREATE FUNCTION public.is_valid_timezone(tz text) RETURNS boolean
    LANGUAGE sql STABLE STRICT
    AS $$
    SELECT (tz = 'UTC' OR tz LIKE '%/%')
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = tz);
$$;

ALTER TABLE public.organizations ADD COLUMN timezone character varying(64);
UPDATE public.organizations SET timezone = 'America/New_York' WHERE timezone IS NULL;
ALTER TABLE public.organizations
    ALTER COLUMN timezone SET NOT NULL,
    ADD CONSTRAINT organizations_timezone_check CHECK (public.is_valid_timezone(timezone));

-- -----------------------------------------------------------------------------
-- 3. Small value checks
-- -----------------------------------------------------------------------------

ALTER TABLE public.seasons
    ADD CONSTRAINT seasons_date_order_check CHECK (end_date >= start_date);

ALTER TABLE public.divisions
    ADD CONSTRAINT divisions_games_per_team_check CHECK (games_per_team > 0),
    ADD CONSTRAINT divisions_max_games_per_week_check CHECK (max_games_per_week > 0);

-- -----------------------------------------------------------------------------
-- 4. Organization column on link tables (needed for cross-org checks)
-- -----------------------------------------------------------------------------

ALTER TABLE public.team_coaches ADD COLUMN organization_id uuid;
UPDATE public.team_coaches tc
   SET organization_id = t.organization_id
  FROM public.teams t
 WHERE t.id = tc.team_id;
ALTER TABLE public.team_coaches ALTER COLUMN organization_id SET NOT NULL;

ALTER TABLE public.event_officials ADD COLUMN organization_id uuid;
UPDATE public.event_officials eo
   SET organization_id = e.organization_id
  FROM public.events e
 WHERE e.id = eo.event_id;
ALTER TABLE public.event_officials ALTER COLUMN organization_id SET NOT NULL;

-- -----------------------------------------------------------------------------
-- 5. Composite foreign keys: a child's organization / season / division must
--    match its parent's. The existing single-column FKs stay in place; these
--    use the same ON DELETE behaviour so cascades are unchanged.
-- -----------------------------------------------------------------------------

-- Targets
ALTER TABLE public.seasons   ADD CONSTRAINT seasons_id_org_key   UNIQUE (id, organization_id);
ALTER TABLE public.divisions ADD CONSTRAINT divisions_id_season_key UNIQUE (id, season_id);
ALTER TABLE public.teams     ADD CONSTRAINT teams_id_division_key UNIQUE (id, division_id);
ALTER TABLE public.teams     ADD CONSTRAINT teams_id_org_key     UNIQUE (id, organization_id);
ALTER TABLE public.events    ADD CONSTRAINT events_id_season_key UNIQUE (id, season_id);
ALTER TABLE public.events    ADD CONSTRAINT events_id_org_key    UNIQUE (id, organization_id);
ALTER TABLE public.venues    ADD CONSTRAINT venues_id_org_key    UNIQUE (id, organization_id);
ALTER TABLE public.fields    ADD CONSTRAINT fields_id_org_key    UNIQUE (id, organization_id);
ALTER TABLE public.coaches   ADD CONSTRAINT coaches_id_org_key   UNIQUE (id, organization_id);
ALTER TABLE public.officials ADD CONSTRAINT officials_id_org_key UNIQUE (id, organization_id);

-- Divisions belong to a season of the same organization
ALTER TABLE public.divisions
    ADD CONSTRAINT divisions_season_org_fkey FOREIGN KEY (season_id, organization_id)
        REFERENCES public.seasons (id, organization_id) ON DELETE CASCADE;

-- Teams belong to a division of the same season, in the same organization
ALTER TABLE public.teams
    ADD CONSTRAINT teams_division_season_fkey FOREIGN KEY (division_id, season_id)
        REFERENCES public.divisions (id, season_id) ON DELETE CASCADE,
    ADD CONSTRAINT teams_season_org_fkey FOREIGN KEY (season_id, organization_id)
        REFERENCES public.seasons (id, organization_id) ON DELETE CASCADE;

-- Fields belong to a venue of the same organization
ALTER TABLE public.fields
    ADD CONSTRAINT fields_venue_org_fkey FOREIGN KEY (venue_id, organization_id)
        REFERENCES public.venues (id, organization_id) ON DELETE CASCADE;

-- Events: both teams in the event's division; division in the event's season;
-- season in the event's organization
ALTER TABLE public.events
    ADD CONSTRAINT events_home_team_division_fkey FOREIGN KEY (home_team_id, division_id)
        REFERENCES public.teams (id, division_id) ON DELETE CASCADE,
    ADD CONSTRAINT events_away_team_division_fkey FOREIGN KEY (away_team_id, division_id)
        REFERENCES public.teams (id, division_id) ON DELETE CASCADE,
    ADD CONSTRAINT events_division_season_fkey FOREIGN KEY (division_id, season_id)
        REFERENCES public.divisions (id, season_id) ON DELETE CASCADE,
    ADD CONSTRAINT events_season_org_fkey FOREIGN KEY (season_id, organization_id)
        REFERENCES public.seasons (id, organization_id) ON DELETE CASCADE;

-- Slot rules
ALTER TABLE public.slot_rules
    ADD CONSTRAINT slot_rules_season_org_fkey FOREIGN KEY (season_id, organization_id)
        REFERENCES public.seasons (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT slot_rules_field_org_fkey FOREIGN KEY (field_id, organization_id)
        REFERENCES public.fields (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT slot_rules_division_season_fkey FOREIGN KEY (division_id, season_id)
        REFERENCES public.divisions (id, season_id) ON DELETE CASCADE;

-- Time slots (division and event are optional; deleting them only clears that column)
ALTER TABLE public.time_slots
    ADD CONSTRAINT time_slots_season_org_fkey FOREIGN KEY (season_id, organization_id)
        REFERENCES public.seasons (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT time_slots_field_org_fkey FOREIGN KEY (field_id, organization_id)
        REFERENCES public.fields (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT time_slots_division_season_fkey FOREIGN KEY (division_id, season_id)
        REFERENCES public.divisions (id, season_id) ON DELETE SET NULL (division_id),
    ADD CONSTRAINT time_slots_event_season_fkey FOREIGN KEY (event_id, season_id)
        REFERENCES public.events (id, season_id) ON DELETE SET NULL (event_id);

-- Coach / official assignments stay inside one organization
ALTER TABLE public.team_coaches
    ADD CONSTRAINT team_coaches_team_org_fkey FOREIGN KEY (team_id, organization_id)
        REFERENCES public.teams (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT team_coaches_coach_org_fkey FOREIGN KEY (coach_id, organization_id)
        REFERENCES public.coaches (id, organization_id) ON DELETE CASCADE;

ALTER TABLE public.event_officials
    ADD CONSTRAINT event_officials_event_org_fkey FOREIGN KEY (event_id, organization_id)
        REFERENCES public.events (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT event_officials_official_org_fkey FOREIGN KEY (official_id, organization_id)
        REFERENCES public.officials (id, organization_id) ON DELETE CASCADE;

-- -----------------------------------------------------------------------------
-- 6. Deferrable slot constraints (lets two games trade slots in one
--    transaction via SET CONSTRAINTS ALL DEFERRED)
-- -----------------------------------------------------------------------------

ALTER TABLE public.time_slots DROP CONSTRAINT time_slots_event_id_key;
ALTER TABLE public.time_slots
    ADD CONSTRAINT time_slots_event_id_key UNIQUE (event_id) DEFERRABLE INITIALLY IMMEDIATE;

ALTER TABLE public.time_slots DROP CONSTRAINT time_slots_no_field_overlap;
ALTER TABLE public.time_slots
    ADD CONSTRAINT time_slots_no_field_overlap
        EXCLUDE USING gist (field_id WITH =, tstzrange(start_time, end_time) WITH &&)
        DEFERRABLE INITIALLY IMMEDIATE;

-- -----------------------------------------------------------------------------
-- 7. Booking tables: the double-booking guarantee
-- -----------------------------------------------------------------------------

CREATE TABLE public.team_bookings (
    event_id uuid NOT NULL REFERENCES public.events (id) ON DELETE CASCADE,
    team_id  uuid NOT NULL REFERENCES public.teams (id) ON DELETE CASCADE,
    during   tstzrange NOT NULL,
    CONSTRAINT team_bookings_pkey PRIMARY KEY (event_id, team_id),
    CONSTRAINT team_bookings_no_overlap
        EXCLUDE USING gist (team_id WITH =, during WITH &&) DEFERRABLE INITIALLY IMMEDIATE
);

CREATE TABLE public.coach_bookings (
    event_id uuid NOT NULL REFERENCES public.events (id) ON DELETE CASCADE,
    coach_id uuid NOT NULL REFERENCES public.coaches (id) ON DELETE CASCADE,
    during   tstzrange NOT NULL,
    CONSTRAINT coach_bookings_pkey PRIMARY KEY (event_id, coach_id),
    CONSTRAINT coach_bookings_no_overlap
        EXCLUDE USING gist (coach_id WITH =, during WITH &&) DEFERRABLE INITIALLY IMMEDIATE
);

CREATE TABLE public.official_bookings (
    event_id    uuid NOT NULL REFERENCES public.events (id) ON DELETE CASCADE,
    official_id uuid NOT NULL REFERENCES public.officials (id) ON DELETE CASCADE,
    during      tstzrange NOT NULL,
    CONSTRAINT official_bookings_pkey PRIMARY KEY (event_id, official_id),
    CONSTRAINT official_bookings_no_overlap
        EXCLUDE USING gist (official_id WITH =, during WITH &&) DEFERRABLE INITIALLY IMMEDIATE
);

COMMENT ON TABLE public.team_bookings IS
    'One row per team per scheduled, non-cancelled game. Maintained by sync_event_bookings(); do not write directly.';
COMMENT ON TABLE public.coach_bookings IS
    'One row per coach per scheduled, non-cancelled game (via team_coaches). Maintained by sync_event_bookings(); do not write directly.';
COMMENT ON TABLE public.official_bookings IS
    'One row per official per scheduled, non-cancelled game (via event_officials). Maintained by sync_event_bookings(); do not write directly.';

-- -----------------------------------------------------------------------------
-- 8. Keeping the booking tables in sync
-- -----------------------------------------------------------------------------

-- Rebuilds every booking row for one event from time_slots, events,
-- team_coaches and event_officials. An event holds bookings only while it is
-- linked to a time slot and not Cancelled. Joins to the participant tables so
-- rows are never written for something being deleted in the same statement.
-- The SET clause marks the session as "syncing" for the guard trigger below,
-- and is restored automatically when the function returns.
CREATE FUNCTION public.sync_event_bookings(p_event_id uuid) RETURNS void
    LANGUAGE plpgsql
    SET skeddie.syncing_bookings = 'on'
    AS $$
DECLARE
    v_during tstzrange;
BEGIN
    IF p_event_id IS NULL THEN
        RETURN;
    END IF;

    DELETE FROM public.team_bookings     WHERE event_id = p_event_id;
    DELETE FROM public.coach_bookings    WHERE event_id = p_event_id;
    DELETE FROM public.official_bookings WHERE event_id = p_event_id;

    SELECT tstzrange(ts.start_time, ts.end_time, '[)')
      INTO v_during
      FROM public.time_slots ts
      JOIN public.events e ON e.id = ts.event_id
     WHERE ts.event_id = p_event_id
       AND e.status <> 'Cancelled';

    IF v_during IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO public.team_bookings (event_id, team_id, during)
    SELECT e.id, t.id, v_during
      FROM public.events e
      JOIN public.teams t ON t.id IN (e.home_team_id, e.away_team_id)
     WHERE e.id = p_event_id;

    INSERT INTO public.coach_bookings (event_id, coach_id, during)
    SELECT DISTINCT e.id, c.id, v_during
      FROM public.events e
      JOIN public.team_coaches tc ON tc.team_id IN (e.home_team_id, e.away_team_id)
      JOIN public.coaches c ON c.id = tc.coach_id
     WHERE e.id = p_event_id;

    INSERT INTO public.official_bookings (event_id, official_id, during)
    SELECT eo.event_id, o.id, v_during
      FROM public.event_officials eo
      JOIN public.officials o ON o.id = eo.official_id
     WHERE eo.event_id = p_event_id;
END;
$$;

-- Re-syncs every scheduled event a team plays in (used when coaches change).
CREATE FUNCTION public.sync_team_event_bookings(p_team_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_event_id uuid;
BEGIN
    -- A team that no longer exists is being deleted; its events go with it.
    IF p_team_id IS NULL OR NOT EXISTS (SELECT 1 FROM public.teams WHERE id = p_team_id) THEN
        RETURN;
    END IF;

    FOR v_event_id IN
        SELECT ts.event_id
          FROM public.time_slots ts
          JOIN public.events e ON e.id = ts.event_id
         WHERE p_team_id IN (e.home_team_id, e.away_team_id)
    LOOP
        PERFORM public.sync_event_bookings(v_event_id);
    END LOOP;
END;
$$;

CREATE FUNCTION public.time_slots_sync_bookings() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM public.sync_event_bookings(OLD.event_id);
    END IF;
    IF TG_OP = 'INSERT' THEN
        PERFORM public.sync_event_bookings(NEW.event_id);
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.event_id IS DISTINCT FROM OLD.event_id THEN
            PERFORM public.sync_event_bookings(NEW.event_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION public.events_sync_bookings() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM public.sync_event_bookings(NEW.id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION public.team_coaches_sync_bookings() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM public.sync_team_event_bookings(OLD.team_id);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        PERFORM public.sync_team_event_bookings(NEW.team_id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION public.event_officials_sync_bookings() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM public.sync_event_bookings(OLD.event_id);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        PERFORM public.sync_event_bookings(NEW.event_id);
    END IF;
    RETURN NULL;
END;
$$;

-- Booking rows may only be written by sync_event_bookings() or removed by a
-- cascading delete from their parent rows.
CREATE FUNCTION public.guard_booking_writes() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF current_setting('skeddie.syncing_bookings', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is maintained automatically from events, time_slots, team_coaches and event_officials; it cannot be written directly', TG_TABLE_NAME
        USING HINT = 'Change the event, its time slot, or the coach/official assignment instead.';
END;
$$;

CREATE TRIGGER time_slots_sync_bookings
    AFTER INSERT OR DELETE OR UPDATE OF event_id, start_time, end_time ON public.time_slots
    FOR EACH ROW EXECUTE FUNCTION public.time_slots_sync_bookings();

CREATE TRIGGER events_sync_bookings
    AFTER UPDATE OF home_team_id, away_team_id, status ON public.events
    FOR EACH ROW EXECUTE FUNCTION public.events_sync_bookings();

CREATE TRIGGER team_coaches_sync_bookings
    AFTER INSERT OR DELETE OR UPDATE OF team_id, coach_id ON public.team_coaches
    FOR EACH ROW EXECUTE FUNCTION public.team_coaches_sync_bookings();

CREATE TRIGGER event_officials_sync_bookings
    AFTER INSERT OR DELETE OR UPDATE OF event_id, official_id ON public.event_officials
    FOR EACH ROW EXECUTE FUNCTION public.event_officials_sync_bookings();

CREATE TRIGGER team_bookings_guard
    BEFORE INSERT OR UPDATE OR DELETE ON public.team_bookings
    FOR EACH ROW EXECUTE FUNCTION public.guard_booking_writes();

CREATE TRIGGER coach_bookings_guard
    BEFORE INSERT OR UPDATE OR DELETE ON public.coach_bookings
    FOR EACH ROW EXECUTE FUNCTION public.guard_booking_writes();

CREATE TRIGGER official_bookings_guard
    BEFORE INSERT OR UPDATE OR DELETE ON public.official_bookings
    FOR EACH ROW EXECUTE FUNCTION public.guard_booking_writes();

-- Build bookings for anything already scheduled
DO $$
BEGIN
    PERFORM public.sync_event_bookings(event_id) FROM public.time_slots WHERE event_id IS NOT NULL;
END
$$;

-- -----------------------------------------------------------------------------
-- 9. Supporting indexes
-- -----------------------------------------------------------------------------

CREATE INDEX idx_events_home_team ON public.events USING btree (home_team_id);
CREATE INDEX idx_events_away_team ON public.events USING btree (away_team_id);
CREATE INDEX idx_team_coaches_coach ON public.team_coaches USING btree (coach_id);
CREATE INDEX idx_event_officials_official ON public.event_officials USING btree (official_id);

-- -----------------------------------------------------------------------------
-- 10. Documentation
-- -----------------------------------------------------------------------------

COMMENT ON COLUMN public.organizations.timezone IS
    'IANA timezone name (e.g. America/New_York). All local times for this organization (slot rules, unavailabilities) are in this zone.';
COMMENT ON COLUMN public.slot_rules.day_of_week IS
    '0 = Monday ... 6 = Sunday (Python date.weekday()), in the organization timezone.';
COMMENT ON COLUMN public.unavailabilities.day_of_week IS
    '0 = Monday ... 6 = Sunday (Python date.weekday()), in the organization timezone.';
COMMENT ON COLUMN public.slot_rules.start_time IS 'Local wall-clock time in the organization timezone.';
COMMENT ON COLUMN public.unavailabilities.start_time IS 'Local wall-clock time in the organization timezone.';

INSERT INTO public.schema_migrations (version) VALUES ('001_integrity');

COMMIT;
