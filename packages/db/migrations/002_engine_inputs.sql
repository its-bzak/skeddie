-- =============================================================================
-- 002_engine_inputs.sql
--
-- Makes unavailability a trustworthy input for the scheduling engine and adds
-- the inputs it was missing:
--
--   * unavailability targets become real foreign keys (team_id, coach_id,
--     official_id, field_id, venue_id), so a row can no longer point at a
--     deleted target or one in another organization
--   * blackouts for a field, a venue or the whole organization (holidays,
--     maintenance)
--   * whole-day windows (start_time and end_time both NULL) and date ranges
--     (specific_date through end_date)
--   * seasons.makeup_weeks: the last 7 x makeup_weeks days of a season are kept
--     free of scheduled games, as a makeup window for rainouts
--   * drops the unused set_event_buffered_end_time() function
--
-- HOW TO RUN (pgAdmin): open a Query Tool on the skeddie database and execute
-- the whole file. It runs in one transaction: if any check fails, nothing is
-- applied (run ROLLBACK; if pgAdmin reports the transaction as aborted).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Migration tracking
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '001_integrity') THEN
        RAISE EXCEPTION '002_engine_inputs needs 001_integrity to be applied first';
    END IF;
    IF EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '002_engine_inputs') THEN
        RAISE EXCEPTION '002_engine_inputs has already been applied to this database';
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 1. Preflight: every existing unavailability must point at a real target in
--    its own organization, or the new foreign keys cannot be added
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    problems text;
BEGIN
    SELECT string_agg(format('%s (%s %s)', u.id, u.target_type, u.target_id), E'\n  - ')
      INTO problems
      FROM public.unavailabilities u
     WHERE NOT CASE u.target_type
               WHEN 'team' THEN EXISTS (
                   SELECT 1 FROM public.teams t
                    WHERE t.id = u.target_id AND t.organization_id = u.organization_id)
               WHEN 'coach' THEN EXISTS (
                   SELECT 1 FROM public.coaches c
                    WHERE c.id = u.target_id AND c.organization_id = u.organization_id)
               WHEN 'official' THEN EXISTS (
                   SELECT 1 FROM public.officials o
                    WHERE o.id = u.target_id AND o.organization_id = u.organization_id)
               ELSE false
           END;

    IF problems IS NOT NULL THEN
        RAISE EXCEPTION E'These unavailability rows point at a missing target or one in another organization; fix or delete them first:\n  - %', problems;
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 2. Real targets
-- -----------------------------------------------------------------------------

ALTER TABLE public.unavailabilities
    ADD COLUMN team_id uuid,
    ADD COLUMN coach_id uuid,
    ADD COLUMN official_id uuid,
    ADD COLUMN field_id uuid,
    ADD COLUMN venue_id uuid;

UPDATE public.unavailabilities
   SET team_id     = CASE WHEN target_type = 'team' THEN target_id END,
       coach_id    = CASE WHEN target_type = 'coach' THEN target_id END,
       official_id = CASE WHEN target_type = 'official' THEN target_id END;

DROP INDEX public.idx_unavailabilities_lookup;
ALTER TABLE public.unavailabilities DROP COLUMN target_id;

-- 'organization' targets every field of the organization, with no target column set.
ALTER TABLE public.unavailabilities
    DROP CONSTRAINT unavailabilities_target_type_check,
    ADD CONSTRAINT unavailabilities_target_type_check CHECK (
        target_type IN ('team', 'coach', 'official', 'field', 'venue', 'organization')
    ),
    ADD CONSTRAINT unavailabilities_target_check CHECK (
            (team_id IS NOT NULL) = (target_type = 'team')
        AND (coach_id IS NOT NULL) = (target_type = 'coach')
        AND (official_id IS NOT NULL) = (target_type = 'official')
        AND (field_id IS NOT NULL) = (target_type = 'field')
        AND (venue_id IS NOT NULL) = (target_type = 'venue')
    ),
    ADD CONSTRAINT unavailabilities_team_org_fkey FOREIGN KEY (team_id, organization_id)
        REFERENCES public.teams (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT unavailabilities_coach_org_fkey FOREIGN KEY (coach_id, organization_id)
        REFERENCES public.coaches (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT unavailabilities_official_org_fkey FOREIGN KEY (official_id, organization_id)
        REFERENCES public.officials (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT unavailabilities_field_org_fkey FOREIGN KEY (field_id, organization_id)
        REFERENCES public.fields (id, organization_id) ON DELETE CASCADE,
    ADD CONSTRAINT unavailabilities_venue_org_fkey FOREIGN KEY (venue_id, organization_id)
        REFERENCES public.venues (id, organization_id) ON DELETE CASCADE;

CREATE INDEX idx_unavailabilities_org_target ON public.unavailabilities USING btree (organization_id, target_type);

-- -----------------------------------------------------------------------------
-- 3. Whole-day windows and date ranges
-- -----------------------------------------------------------------------------

-- unavailabilities_time_order_check (end_time > start_time) stays: it passes
-- when both times are NULL.
ALTER TABLE public.unavailabilities
    ADD COLUMN end_date date,
    ALTER COLUMN start_time DROP NOT NULL,
    ALTER COLUMN end_time DROP NOT NULL,
    ADD CONSTRAINT unavailabilities_whole_day_check CHECK ((start_time IS NULL) = (end_time IS NULL)),
    ADD CONSTRAINT unavailabilities_end_date_check CHECK (
        end_date IS NULL OR (specific_date IS NOT NULL AND end_date >= specific_date)
    );

-- -----------------------------------------------------------------------------
-- 4. Makeup window
-- -----------------------------------------------------------------------------

ALTER TABLE public.seasons
    ADD COLUMN makeup_weeks integer DEFAULT 0 NOT NULL,
    ADD CONSTRAINT seasons_makeup_weeks_check CHECK (makeup_weeks >= 0),
    ADD CONSTRAINT seasons_makeup_window_check CHECK (makeup_weeks * 7 < end_date - start_date + 1);

-- -----------------------------------------------------------------------------
-- 5. Cleanup: never attached to a trigger, and refers to columns that do not
--    exist on events
-- -----------------------------------------------------------------------------

DROP FUNCTION IF EXISTS public.set_event_buffered_end_time();

-- -----------------------------------------------------------------------------
-- 6. Documentation
-- -----------------------------------------------------------------------------

COMMENT ON COLUMN public.unavailabilities.target_type IS
    'team, coach or official: that person or team cannot play. field, venue or organization: a blackout; no games on that field, any field of that venue, or any field of the organization.';
COMMENT ON COLUMN public.unavailabilities.start_time IS
    'Local wall-clock time in the organization timezone. start_time and end_time both NULL = the whole day.';
COMMENT ON COLUMN public.unavailabilities.end_time IS
    'Local wall-clock time in the organization timezone. start_time and end_time both NULL = the whole day.';
COMMENT ON COLUMN public.unavailabilities.end_date IS
    'Optional last day (inclusive) of a specific_date range; NULL = only specific_date.';
COMMENT ON COLUMN public.seasons.makeup_weeks IS
    'The last 7 x makeup_weeks days of the season are kept free of scheduled games, as a makeup window for rainouts.';

INSERT INTO public.schema_migrations (version) VALUES ('002_engine_inputs');

COMMIT;
