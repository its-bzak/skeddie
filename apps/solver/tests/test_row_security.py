"""Row-level security: the database itself confines the API to the caller's organizations.

These tests act as the application role the way the API does (database.scope_session),
inside the usual rolled-back test transaction.
"""

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import delete, insert, select, text, update

from database import scope_session, unscope_session
from models import Admin, Division, Event, Field, Organization, Season, Team, TimeSlot
from tests.helpers import local, rejected

TENANT_TABLES = [
    "organizations", "admins", "coaches", "officials", "venues", "fields", "seasons", "divisions", "teams",
    "team_coaches", "events", "event_officials", "slot_rules", "time_slots", "unavailabilities", "schedule_runs",
]
BOOKING_TABLES = ["team_bookings", "coach_bookings", "official_bookings"]
# Shared reference data with no organization, readable by everyone.
NOT_TENANT_DATA = {"sports", "schema_migrations"}


def org_id(db, name: str):
    return db.scalar(select(Organization.id).where(Organization.name == name))


def admin_of(db, org_name: str):
    return db.scalar(select(Admin.user_id).join(Organization).where(Organization.name == org_name))


@contextmanager
def acting_as(db, user_id):
    scope_session(db, user_id)
    try:
        yield
    finally:
        unscope_session(db)


def organizations_visible(db, table: str) -> set:
    column = "id" if table == "organizations" else "organization_id"
    return set(db.scalars(text(f"SELECT DISTINCT {column} FROM {table}")))


def schedule_one_game(db, org_name: str, home: str, away: str) -> None:
    """As the owner: put one game between two of the organization's teams on its first field."""
    organization = org_id(db, org_name)
    season = db.scalar(select(Season).where(Season.organization_id == organization))
    field = db.scalar(select(Field).where(Field.organization_id == organization).order_by(Field.name))
    teams = {t.name: t for t in db.scalars(select(Team).where(Team.organization_id == organization))}
    slot = TimeSlot(organization_id=organization, season_id=season.id, field_id=field.id,
                    start_time=local(2027, 4, 5, 16), end_time=local(2027, 4, 5, 16) + timedelta(hours=2))
    event = Event(organization_id=organization, season_id=season.id, division_id=teams[home].division_id,
                  home_team_id=teams[home].id, away_team_id=teams[away].id)
    db.add_all([slot, event])
    db.flush()
    slot.event_id = event.id
    db.flush()


def test_every_table_is_covered_by_row_security(db):
    tables = db.execute(text(
        "SELECT c.relname, c.relrowsecurity, count(p.oid) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' GROUP BY c.relname, c.relrowsecurity"
    )).all()
    uncovered = sorted(
        name for name, enabled, policies in tables if name not in NOT_TENANT_DATA and not (enabled and policies)
    )
    assert uncovered == [], "every new table needs row security and a policy (see 004_row_security.sql)"


def test_an_admin_sees_only_their_own_organization(db):
    dans, easton = org_id(db, "Dan's Organization"), org_id(db, "Easton Youth Athletics")
    assert easton in organizations_visible(db, "teams")  # the owner sees both

    with acting_as(db, admin_of(db, "Dan's Organization")):
        for table in TENANT_TABLES:
            assert organizations_visible(db, table) <= {dans}, table
        assert organizations_visible(db, "teams") == {dans}
        emails = set(db.scalars(text("SELECT email FROM users")))
        assert "dan@dansorg.example.com" in emails and "coach1@dansorg.example.com" in emails
        assert "admin@eastonyouth.example.com" not in emails

    with acting_as(db, admin_of(db, "Easton Youth Athletics")):
        assert organizations_visible(db, "teams") == {easton}
        assert set(db.scalars(text("SELECT email FROM users"))) == {"admin@eastonyouth.example.com"}


def test_bookings_are_visible_only_for_your_own_games(db):
    schedule_one_game(db, "Dan's Organization", "Team 1A", "Team 1B")
    schedule_one_game(db, "Easton Youth Athletics", "Easton Red", "Easton Blue")
    assert db.scalar(text("SELECT count(*) FROM team_bookings")) == 4

    for org_name in ("Dan's Organization", "Easton Youth Athletics"):
        with acting_as(db, admin_of(db, org_name)):
            assert db.scalar(text("SELECT count(*) FROM team_bookings")) == 2, org_name


def test_writes_outside_your_organization_are_refused(db):
    dans, easton = org_id(db, "Dan's Organization"), org_id(db, "Easton Youth Athletics")
    easton_division = db.scalar(select(Division).where(Division.organization_id == easton))

    with acting_as(db, admin_of(db, "Dan's Organization")):
        with rejected(db, "42501"):
            db.execute(insert(Team).values(
                organization_id=easton, season_id=easton_division.season_id,
                division_id=easton_division.id, name="Intruder",
            ))
        with rejected(db, "42501"):
            db.execute(update(Team).where(Team.organization_id == dans).values(organization_id=easton))
        assert db.execute(update(Season).where(Season.organization_id == easton).values(name="Hacked")).rowcount == 0
        assert db.execute(delete(Team).where(Team.organization_id == easton)).rowcount == 0
        with rejected(db, "42501"):  # bookings are only ever written by the sync triggers
            db.execute(text("DELETE FROM team_bookings"))

    assert db.scalar(select(Season.name).where(Season.organization_id == easton)) == "Spring 2027"
    assert db.scalar(select(Team.id).where(Team.name == "Intruder")) is None


def test_with_no_signed_in_user_nothing_is_visible(db):
    db.execute(text("SET LOCAL ROLE skeddie_app"))
    try:
        for table in TENANT_TABLES + BOOKING_TABLES + ["users"]:
            assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    finally:
        db.execute(text("RESET ROLE"))
