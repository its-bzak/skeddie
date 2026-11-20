# Skeddie

Automated, constraint-based scheduling platform for youth sports leagues and tournament organizers.

Skeddie replaces hours-long spreadsheet meetings with a constraint-programming optimization engine that generates conflict-free game and practice schedules in seconds. It accounts for multi-team coaches, field availability, blackout dates, and division rules—while enforcing zero double-bookings at the database level.

## Core Features

* **Constraint-Based Auto-Scheduler:** Solves complex league constraints (coach overlaps, field capacities, game durations, buffer times, and home/away balance) in seconds.
* **Conflict Inspector & Manual Overrides:** Visual schedule grid with drag-and-drop adjustments and real-time bottleneck detection when constraints cannot be satisfied.
* **Zero-Friction Distribution:** Live calendar feeds for parents and coaches, plus one-click rainout rescheduling.

## Tech Stack

* **Frontend:** TypeScript, Next.js, Tailwind CSS
* **Backend & Solver:** Python, FastAPI
* **Database:** PostgreSQL
* **Infrastructure & Tooling:** Docker, Docker Compose, Pytest, Github Actions

## Repository Structure

```text
skeddie/
├── apps/
│   ├── web/                     # Next.js frontend (scaffold only so far)
│   └── solver/                  # Python FastAPI + Google OR-Tools scheduling service
│       ├── main.py              # FastAPI app, /health, startup recovery of interrupted runs
│       ├── auth.py              # Sign-in: token verification and the organization-admin gate
│       ├── database.py          # Engine, sessions, and the row-level-security scope for each request
│       ├── models.py            # ORM mapping of every table
│       ├── seed.py              # Demo data: Dan's Organization and Easton Youth Athletics
│       ├── routers/             # HTTP routes (scheduling.py) and the background run worker
│       ├── services/            # The scheduling engine (see below)
│       ├── scripts/dev_auth.py  # Dev signing key and tokens for local sign-in
│       └── tests/               # Pytest suite; builds its own throwaway database
├── packages/
│   └── db/
│       ├── migrations/          # Versioned SQL, the source of truth for the schema
│       └── README.md            # Migration workflow, database guarantees, scheduler settings
├── .github/workflows/ci.yml     # Lint, migrations and tests on every push and pull request
├── docker-compose.yml           # Local Postgres, plus a solver service whose Dockerfile is still empty
└── README.md
```

### The scheduling engine (`apps/solver/services/`)

Roughly in the order a schedule is built:

| Module | What it does |
| --- | --- |
| `slot_carver.py` | Turns weekly slot rules into concrete time slots: safe across clock changes, skips blackouts, safe to re-run |
| `problem.py` | Loads one season into plain data for the solver |
| `unavailability.py` | Turns unavailability and blackout rows into concrete time windows |
| `timeutil.py` | Local time to UTC conversion, and the `Interval` type |
| `matchups.py` | Who plays whom, in rounds, with balanced home and away games |
| `eligibility.py` | Which slots each game could use |
| `precheck.py` | Capacity checks before solving, in plain language |
| `scheduler.py` | The CP-SAT model: hard rules, soft goals, round windows with a fallback, explanations when a season can't be scheduled |
| `validate.py` | Separate re-check of every hard rule on a finished schedule |
| `quality.py` | The quality report (pace, same-day doubles, back-to-backs) and the shared definition of "on pace" |
| `persist.py` | Schedules a season and saves it in one transaction |
| `runs.py` | Background scheduling runs, and recovery after a restart |
| `locks.py` | A per-season lock, so two runs can't overlap |

## Running and testing

Run the commands from `apps/solver` in PowerShell, using the project's virtual environment. For database setup and migrations, see [packages/db/README.md](packages/db/README.md).

### Automated tests

```powershell
.\venv\Scripts\python.exe -m pytest                                 # the whole suite (about a minute)
.\venv\Scripts\python.exe -m pytest tests\test_row_security.py -v   # one file
.\venv\Scripts\python.exe -m pytest -k makeup                       # tests whose name matches
.\venv\Scripts\ruff.exe check .                                     # lint
```

The suite never touches your dev data:
- It drops and rebuilds a `skeddie_test` database from the migrations, on the server in `DATABASE_URL` (or `TEST_DATABASE_URL`), and loads `seed.py`.
- Every test runs in a transaction that is rolled back.

CI runs the same lint, migrations and tests.

| Test file | What it proves |
| --- | --- |
| `test_db_integrity.py` | The database itself blocks double-bookings, cross-organization references and invalid values, even with two transactions racing |
| `test_row_security.py` | Row-level security limits each admin to their own organizations; nothing is visible without a signed-in user; every table has a policy |
| `test_auth.py` | Missing, malformed, expired or wrongly signed tokens get 401; strangers and non-admins get 403; a first sign-in links the account; routes return 503 until auth is configured |
| `test_api.py` | The full API flow, 409 conflicts, cross-organization access refused, readable error details |
| `test_runs.py` | One active run per season, failures recorded on the run, recovery after a restart, the real background worker |
| `test_scheduler.py` | Solver rules, even pacing, round windows and the fallback, the quality report, the seed league end to end |
| `test_slot_carver.py` | Slot generation across clock changes, blackouts, re-runs and conflicts |
| `test_matchups.py` | Every team plays exactly `games_per_team`, rounds, home and away balance, for 2 to 10 teams |
| `test_unavailability.py` | Weekly, whole-day and date-range windows |

### Running the API

Every `/orgs/{org}/…` route needs a signed-in admin of that organization. Sign-in uses an external OpenID Connect provider, configured in the repo-root `.env` (see `.env.example`):

- `AUTH_ISSUER`: the provider's issuer (the token's `iss` claim).
- `AUTH_AUDIENCE`: the API identifier the provider puts in the token's `aud` claim.
- `AUTH_JWKS_URL`: optional. By default the signing keys are found through the issuer's OIDC discovery document.

Without `AUTH_ISSUER` and `AUTH_AUDIENCE`, the organization routes return 503.

The provider's access tokens must include `email` and `email_verified`. The first sign-in links to the existing user with that verified email; there's no self-signup.

**Local development without a provider:**

```powershell
.\venv\Scripts\python.exe scripts\dev_auth.py keys    # once; paste the three AUTH_* lines it prints into .env
.\venv\Scripts\python.exe -m uvicorn main:app --port 8000
.\venv\Scripts\python.exe scripts\dev_auth.py token dan@dansorg.example.com   # a one-hour token
```

The dev key lives in the gitignored `apps/solver/.dev-auth/` folder. Only a server pointed at it accepts its tokens.

### Trying it in the browser

1. Open http://127.0.0.1:8000/docs, click **Authorize**, and paste the token.
2. Look up the ids in pgAdmin:

   ```sql
   SELECT o.name, o.id AS org_id, s.id AS season_id
   FROM organizations o JOIN seasons s ON s.organization_id = o.id;
   ```

3. Build and adjust a schedule:
   1. `POST /orgs/{org_id}/seasons/{season_id}/slots/generate` creates the season's time slots.
   2. `POST /orgs/{org_id}/seasons/{season_id}/schedule-runs` starts a run and returns 202.
   3. Call `GET /orgs/{org_id}/schedule-runs/{run_id}` until `status` is final. A scheduled run includes its `quality`.
   4. `GET /orgs/{org_id}/seasons/{season_id}/events` lists the games.
   5. `PATCH /orgs/{org_id}/events/{event_id}` moves a game (`{"time_slot_id": "…"}`) or cancels it (`{"status": "Cancelled"}`). A move that would double-book someone returns 409, naming who.
   6. `POST /orgs/{org_id}/seasons/{season_id}/publish` publishes the Draft games.
4. Check access by authorizing as other seed users:
   - `admin@eastonyouth.example.com` (Easton's admin) gets 403 on Dan's organization.
   - `coach1@dansorg.example.com` (a coach, not an admin) also gets 403.
   - An unknown email gets 403.
   - No token at all gets 401.

**Starting fresh:** a run replaces Draft games but keeps Published ones. To schedule an organization from scratch again, delete its games in pgAdmin. Their bookings and slot links clear automatically.

```sql
DELETE FROM events WHERE organization_id = (SELECT id FROM organizations WHERE name = 'Dan''s Organization');
```

### Checking the guarantees in pgAdmin

```sql
-- No team is ever in two overlapping games (expect 0)
SELECT count(*) FROM team_bookings a JOIN team_bookings b
  ON a.team_id = b.team_id AND a.event_id < b.event_id AND a.during && b.during;

-- A field can't hold overlapping slots: fails with time_slots_no_field_overlap
BEGIN;
INSERT INTO time_slots (organization_id, season_id, field_id, start_time, end_time)
SELECT organization_id, season_id, field_id, start_time + interval '1 hour', end_time + interval '1 hour'
FROM time_slots LIMIT 1;
ROLLBACK;

-- Row-level security: acting for Easton's admin, Dan's games are invisible (expect 0).
-- Set the user before switching role: once switched, the role can't read users either.
BEGIN;
SELECT set_config('skeddie.user_id', (SELECT id::text FROM users WHERE email = 'admin@eastonyouth.example.com'), true);
SET LOCAL ROLE skeddie_app;
SELECT count(*) FROM events;
ROLLBACK;
```

[packages/db/README.md](packages/db/README.md) covers what each migration guarantees, where every scheduler setting lives, and how to connect the API in production.
