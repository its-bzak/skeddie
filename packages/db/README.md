# Database migrations

`migrations/` is the source of truth for the schema. `apps/solver/models.py` mirrors it for the ORM and is never used to create tables.

## Workflow

1. Write a new numbered file such as `003_something.sql`, wrapped in `BEGIN; … COMMIT;`. End it with `INSERT INTO schema_migrations (version) VALUES ('003_something');`.
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

## Unavailability, blackouts and the makeup window (002_engine_inputs)

- **Who or what is unavailable.** `target_type` decides it, and exactly the matching column must be set; the database enforces this.

  | `target_type` | Column set | Meaning |
  | --- | --- | --- |
  | `team`, `coach`, `official` | `team_id`, `coach_id`, `official_id` | That team or person can't play |
  | `field`, `venue` | `field_id`, `venue_id` | No games on that field, or on any field of that venue |
  | `organization` | none | No games on any field of the organization |

  The target must belong to the row's organization. Deleting the target deletes its unavailability rows.
- **When.**
  - A weekly row uses `day_of_week`: 0 = Monday … 6 = Sunday.
  - A dated row uses `specific_date`, and optionally `end_date` for a range of days, inclusive.
  - Times are local to the organization's timezone. Leave both `start_time` and `end_time` NULL to cover the whole day.
- **Makeup window.**
  - `seasons.makeup_weeks` keeps the last `7 × makeup_weeks` days of the season free of scheduled games.
  - Slots are still generated in that window, so they're there for rainout makeups.
  - The default is 0, which means no makeup window.

Example: close every court on Memorial Day.

```sql
INSERT INTO unavailabilities (organization_id, target_type, specific_date, reason)
VALUES ('<organization id>', 'organization', '2027-05-31', 'Memorial Day');
```
