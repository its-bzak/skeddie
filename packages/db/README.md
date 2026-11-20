# Database migrations

`migrations/` is the source of truth for the schema. `apps/solver/models.py` mirrors it for the ORM and is never used to create tables.

## Workflow

1. Write a new numbered file such as `002_something.sql`, wrapped in `BEGIN; … COMMIT;`. End it with `INSERT INTO schema_migrations (version) VALUES ('002_something');`.
2. Run it on the dev database in a pgAdmin Query Tool. If it fails, the whole transaction rolls back; run `ROLLBACK;` if pgAdmin says the transaction is aborted.
3. Copy the change into `models.py`.
4. Run `python -m pytest` in `apps/solver`. The tests rebuild `skeddie_test` from these files, so a mistake in the SQL shows up there too.

`SELECT * FROM schema_migrations;` shows which files a database has.

## Where these files run

- **Dev database:** run by hand in pgAdmin. Never run `000_baseline.sql` there: it is a snapshot of the schema that database already has.
- **Fresh Docker volume:** docker-compose mounts this folder as `/docker-entrypoint-initdb.d`, so Postgres applies every file in name order the first time the volume is created.
- **Tests and CI:** `apps/solver/tests/conftest.py` drops and recreates `skeddie_test`, then applies every file in order.

## What the database guarantees (001_integrity)

- **No double-booking.**
  - A field can't have overlapping slots.
  - A team, coach or official can't be in two overlapping non-cancelled games. The guard is the `team_bookings`, `coach_bookings` and `official_bookings` tables.
  - Triggers maintain those booking tables, and they reject any direct write.
- **No cross-references.** Composite foreign keys stop a row from pointing into another organization, season or division.
  - Example: both teams of a game must be in the game's division.
- **Swaps.** Two games can trade slots inside one transaction after `SET CONSTRAINTS ALL DEFERRED`.
