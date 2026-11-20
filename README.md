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
│   ├── web/                 # Next.js frontend application
│   └── solver/              # Python FastAPI + Google OR-Tools scheduling service
├── packages/
│   └── db/                  # PostgreSQL migrations, schemas, and seed data
├── docker-compose.yml       # Local development services
└── README.md
```

## Running the API

From `apps/solver`, with the database migrated (see `packages/db/README.md`):

```bash
python -m uvicorn main:app --port 8000
python -m pytest            # tests build their own throwaway database
```

Every `/orgs/{org}/…` route needs a signed-in admin of that organization. Sign-in uses an external OpenID Connect provider; set these in the repo-root `.env` (see `.env.example`):

- `AUTH_ISSUER`: the provider's issuer (the token's `iss` claim).
- `AUTH_AUDIENCE`: the API identifier the provider puts in the token's `aud` claim.
- `AUTH_JWKS_URL`: optional. By default the signing keys are found through the issuer's OIDC discovery document.

The provider's access tokens must include `email` and `email_verified`. The first sign-in links to the user with that verified email.

**Local development without a provider:**

```bash
python scripts/dev_auth.py keys                            # once; paste the printed AUTH_* lines into .env
python scripts/dev_auth.py token dan@dansorg.example.com   # prints a one-hour token
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/orgs/<org id>/seasons/<season id>/events
```

The dev keys live in the gitignored `apps/solver/.dev-auth/` folder. Only a server pointed at them accepts their tokens.
