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