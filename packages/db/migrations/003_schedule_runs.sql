-- =============================================================================
-- 003_schedule_runs.sql
--
-- Scheduling runs: one row per request to build a season's schedule. Runs are
-- solved in the background (large leagues take minutes) and the row records
-- how it went: queued -> running -> scheduled | precheck_failed | infeasible |
-- timeout | error, with the issues found and the schedule's quality.
--
-- A season can have at most one queued or running run at a time; the
-- database enforces it, so two admins cannot start competing solves.
--
-- HOW TO RUN (pgAdmin): open a Query Tool on the skeddie database and execute
-- the whole file. It runs in one transaction: if any check fails, nothing is
-- applied (run ROLLBACK; if pgAdmin reports the transaction as aborted).
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '002_engine_inputs') THEN
        RAISE EXCEPTION '003_schedule_runs needs 002_engine_inputs to be applied first';
    END IF;
    IF EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = '003_schedule_runs') THEN
        RAISE EXCEPTION '003_schedule_runs has already been applied to this database';
    END IF;
END
$$;

CREATE TABLE public.schedule_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    season_id uuid NOT NULL,
    status character varying(20) DEFAULT 'queued' NOT NULL,
    time_limit_seconds integer NOT NULL,
    games_scheduled integer DEFAULT 0 NOT NULL,
    solver_status character varying(20),
    solve_seconds double precision,
    issues jsonb DEFAULT '[]'::jsonb NOT NULL,
    quality jsonb,
    error text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT schedule_runs_pkey PRIMARY KEY (id),
    CONSTRAINT schedule_runs_status_check CHECK (
        status IN ('queued', 'running', 'scheduled', 'precheck_failed', 'infeasible', 'timeout', 'error')
    ),
    CONSTRAINT schedule_runs_time_limit_check CHECK (time_limit_seconds BETWEEN 1 AND 900),
    CONSTRAINT schedule_runs_finished_check CHECK ((status IN ('queued', 'running')) = (finished_at IS NULL)),
    CONSTRAINT schedule_runs_season_org_fkey FOREIGN KEY (season_id, organization_id)
        REFERENCES public.seasons (id, organization_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX schedule_runs_one_active_per_season
    ON public.schedule_runs USING btree (season_id)
    WHERE status IN ('queued', 'running');

CREATE INDEX idx_schedule_runs_season_created ON public.schedule_runs USING btree (season_id, created_at DESC);

COMMENT ON TABLE public.schedule_runs IS
    'One row per request to build a season''s schedule; at most one queued or running run per season.';
COMMENT ON COLUMN public.schedule_runs.quality IS
    'For scheduled runs: off_pace_games, same_day_doubles, back_to_backs and proven_optimal.';

INSERT INTO public.schema_migrations (version) VALUES ('003_schedule_runs');

COMMIT;
