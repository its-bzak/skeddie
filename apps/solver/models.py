"""ORM mapping of the Skeddie schema.

The source of truth for the schema is the SQL in packages/db/migrations; this
module mirrors it for querying. A few database-only safeguards are deliberately
not declared here because the ORM never needs them:

* composite foreign keys such as (home_team_id, division_id) -> teams, which
  stop rows from pointing across organizations, seasons or divisions;
* the triggers that keep team_bookings / coach_bookings / official_bookings in
  sync with events, time slots, coach and official assignments.

Never call Base.metadata.create_all(); apply the migrations instead.
"""

import uuid
from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Core Tenants & Users
# ---------------------------------------------------------------------------


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'cancelled')",
            name="valid_org_status",
        ),
        CheckConstraint("is_valid_timezone(timezone)", name="organizations_timezone_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        server_default=text("'New Organization'"),
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )
    # IANA name, e.g. "America/New_York". Slot rules and unavailabilities are
    # local wall-clock times in this zone.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    admins: Mapped[list["Admin"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    coaches: Mapped[list["Coach"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    officials: Mapped[list["Official"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    venues: Mapped[list["Venue"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    fields: Mapped[list["Field"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    seasons: Mapped[list["Season"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    divisions: Mapped[list["Division"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    teams: Mapped[list["Team"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    slot_rules: Mapped[list["SlotRule"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    unavailabilities: Mapped[list["Unavailability"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    first_name: Mapped[str] = mapped_column(String(50), nullable=False)
    last_name: Mapped[str] = mapped_column(String(50), nullable=False)

    admin_roles: Mapped[list["Admin"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    coach_roles: Mapped[list["Coach"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    official_roles: Mapped[list["Official"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Sport(TimestampMixin, Base):
    __tablename__ = "sports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)

    fields: Mapped[list["Field"]] = relationship(back_populates="sport")
    seasons: Mapped[list["Season"]] = relationship(back_populates="sport", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Organization Roles
# ---------------------------------------------------------------------------


class Admin(TimestampMixin, Base):
    __tablename__ = "admins"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="admins_user_org_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="admin_roles")
    organization: Mapped["Organization"] = relationship(back_populates="admins")


class Coach(TimestampMixin, Base):
    __tablename__ = "coaches"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="coaches_user_org_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="coach_roles")
    organization: Mapped["Organization"] = relationship(back_populates="coaches")
    team_assignments: Mapped[list["TeamCoach"]] = relationship(back_populates="coach", cascade="all, delete-orphan")


class Official(TimestampMixin, Base):
    __tablename__ = "officials"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="officials_user_org_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="official_roles")
    organization: Mapped["Organization"] = relationship(back_populates="officials")
    event_assignments: Mapped[list["EventOfficial"]] = relationship(
        back_populates="official",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# Facilities (Venues & Fields)
# ---------------------------------------------------------------------------


class Venue(TimestampMixin, Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="venues")
    fields: Mapped[list["Field"]] = relationship(back_populates="venue", cascade="all, delete-orphan")


class Field(TimestampMixin, Base):
    __tablename__ = "fields"
    __table_args__ = (
        UniqueConstraint("venue_id", "name", name="fields_venue_name_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    venue_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("venues.id", ondelete="CASCADE"),
        nullable=True,
    )
    sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="fields")
    venue: Mapped[Optional["Venue"]] = relationship(back_populates="fields")
    sport: Mapped["Sport"] = relationship(back_populates="fields")
    slot_rules: Mapped[list["SlotRule"]] = relationship(back_populates="field", cascade="all, delete-orphan")
    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="field", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# League Hierarchy (Seasons, Divisions, Teams)
# ---------------------------------------------------------------------------


class Season(TimestampMixin, Base):
    __tablename__ = "seasons"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="seasons_date_order_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sports.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="seasons")
    sport: Mapped["Sport"] = relationship(back_populates="seasons")
    divisions: Mapped[list["Division"]] = relationship(back_populates="season", cascade="all, delete-orphan")
    teams: Mapped[list["Team"]] = relationship(back_populates="season", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="season", cascade="all, delete-orphan")
    slot_rules: Mapped[list["SlotRule"]] = relationship(back_populates="season", cascade="all, delete-orphan")
    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="season", cascade="all, delete-orphan")


class Division(TimestampMixin, Base):
    __tablename__ = "divisions"
    __table_args__ = (
        CheckConstraint("games_per_team > 0", name="divisions_games_per_team_check"),
        CheckConstraint("max_games_per_week > 0", name="divisions_max_games_per_week_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    games_per_team: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_games_per_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="divisions")
    season: Mapped["Season"] = relationship(back_populates="divisions")
    teams: Mapped[list["Team"]] = relationship(back_populates="division", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="division", cascade="all, delete-orphan")
    slot_rules: Mapped[list["SlotRule"]] = relationship(back_populates="division", cascade="all, delete-orphan")
    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="division")


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    division_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("divisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="teams")
    season: Mapped["Season"] = relationship(back_populates="teams")
    division: Mapped["Division"] = relationship(back_populates="teams")
    coach_assignments: Mapped[list["TeamCoach"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    home_events: Mapped[list["Event"]] = relationship(
        foreign_keys="Event.home_team_id",
        back_populates="home_team",
        cascade="all, delete-orphan",
    )
    away_events: Mapped[list["Event"]] = relationship(
        foreign_keys="Event.away_team_id",
        back_populates="away_team",
        cascade="all, delete-orphan",
    )


class TeamCoach(TimestampMixin, Base):
    __tablename__ = "team_coaches"
    __table_args__ = (
        UniqueConstraint("team_id", "coach_id", name="team_coaches_team_coach_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    coach_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("coaches.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Must equal both the team's and the coach's organization (composite FKs).
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    team: Mapped["Team"] = relationship(back_populates="coach_assignments")
    coach: Mapped["Coach"] = relationship(back_populates="team_assignments")


# ---------------------------------------------------------------------------
# Scheduling & Events
# ---------------------------------------------------------------------------


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Draft', 'Published', 'Cancelled')",
            name="events_status_check",
        ),
        CheckConstraint(
            "home_team_id <> away_team_id",
            name="events_distinct_teams_check",
        ),
        Index("idx_events_season_div", "organization_id", "season_id", "division_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    division_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("divisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    home_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    away_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'Draft'"),
    )

    organization: Mapped["Organization"] = relationship(back_populates="events")
    season: Mapped["Season"] = relationship(back_populates="events")
    division: Mapped["Division"] = relationship(back_populates="events")
    home_team: Mapped["Team"] = relationship(foreign_keys=[home_team_id], back_populates="home_events")
    away_team: Mapped["Team"] = relationship(foreign_keys=[away_team_id], back_populates="away_events")
    official_assignments: Mapped[list["EventOfficial"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )
    time_slot: Mapped[Optional["TimeSlot"]] = relationship(back_populates="event")

    # Maintained by database triggers; read-only from the application.
    team_bookings: Mapped[list["TeamBooking"]] = relationship(viewonly=True)
    coach_bookings: Mapped[list["CoachBooking"]] = relationship(viewonly=True)
    official_bookings: Mapped[list["OfficialBooking"]] = relationship(viewonly=True)


class EventOfficial(TimestampMixin, Base):
    __tablename__ = "event_officials"
    __table_args__ = (
        UniqueConstraint("event_id", "official_id", name="event_officials_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
    )
    official_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("officials.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Must equal both the event's and the official's organization (composite FKs).
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    event: Mapped["Event"] = relationship(back_populates="official_assignments")
    official: Mapped["Official"] = relationship(back_populates="event_assignments")


class SlotRule(TimestampMixin, Base):
    __tablename__ = "slot_rules"
    __table_args__ = (
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="slot_rules_day_of_week_check"),
        CheckConstraint("slot_duration_mins > 0", name="slot_rules_slot_duration_mins_check"),
        CheckConstraint("end_time > start_time", name="slot_rules_time_order_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fields.id", ondelete="CASCADE"),
        nullable=False,
    )
    division_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("divisions.id", ondelete="CASCADE"),
        nullable=True,
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    slot_duration_mins: Mapped[int] = mapped_column(Integer, nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="slot_rules")
    season: Mapped["Season"] = relationship(back_populates="slot_rules")
    field: Mapped["Field"] = relationship(back_populates="slot_rules")
    division: Mapped[Optional["Division"]] = relationship(back_populates="slot_rules")


class TimeSlot(TimestampMixin, Base):
    __tablename__ = "time_slots"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="time_slots_time_order_check"),
        UniqueConstraint(
            "event_id",
            name="time_slots_event_id_key",
            deferrable=True,
            initially="IMMEDIATE",
        ),
        ExcludeConstraint(
            ("field_id", "="),
            (func.tstzrange(text("start_time"), text("end_time")), "&&"),
            using="gist",
            name="time_slots_no_field_overlap",
            deferrable=True,
            initially="IMMEDIATE",
        ),
        Index(
            "idx_time_slots_open",
            "organization_id",
            "season_id",
            "start_time",
            postgresql_where=text("event_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fields.id", ondelete="CASCADE"),
        nullable=False,
    )
    division_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("divisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    organization: Mapped["Organization"] = relationship(back_populates="time_slots")
    season: Mapped["Season"] = relationship(back_populates="time_slots")
    field: Mapped["Field"] = relationship(back_populates="time_slots")
    division: Mapped[Optional["Division"]] = relationship(back_populates="time_slots")
    event: Mapped[Optional["Event"]] = relationship(back_populates="time_slot")


# ---------------------------------------------------------------------------
# Bookings (read-only ledger maintained by database triggers)
#
# One row per participant per scheduled, non-cancelled game. The exclusion
# constraints make it impossible for the same team, coach or official to hold
# two overlapping games. The database rejects any direct write to these
# tables; change the event, its time slot, or the assignment instead.
# ---------------------------------------------------------------------------


class TeamBooking(Base):
    __tablename__ = "team_bookings"
    __table_args__ = (
        ExcludeConstraint(
            ("team_id", "="),
            ("during", "&&"),
            using="gist",
            name="team_bookings_no_overlap",
            deferrable=True,
            initially="IMMEDIATE",
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)


class CoachBooking(Base):
    __tablename__ = "coach_bookings"
    __table_args__ = (
        ExcludeConstraint(
            ("coach_id", "="),
            ("during", "&&"),
            using="gist",
            name="coach_bookings_no_overlap",
            deferrable=True,
            initially="IMMEDIATE",
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    coach_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coaches.id", ondelete="CASCADE"), primary_key=True
    )
    during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)


class OfficialBooking(Base):
    __tablename__ = "official_bookings"
    __table_args__ = (
        ExcludeConstraint(
            ("official_id", "="),
            ("during", "&&"),
            using="gist",
            name="official_bookings_no_overlap",
            deferrable=True,
            initially="IMMEDIATE",
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    official_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("officials.id", ondelete="CASCADE"), primary_key=True
    )
    during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)


class Unavailability(TimestampMixin, Base):
    __tablename__ = "unavailabilities"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('coach', 'official', 'team')",
            name="unavailabilities_target_type_check",
        ),
        CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6",
            name="unavailabilities_day_of_week_check",
        ),
        CheckConstraint(
            "((day_of_week IS NOT NULL AND specific_date IS NULL) OR "
            "(day_of_week IS NULL AND specific_date IS NOT NULL))",
            name="unavailabilities_recurrence_check",
        ),
        CheckConstraint("end_time > start_time", name="unavailabilities_time_order_check"),
        Index("idx_unavailabilities_lookup", "organization_id", "target_type", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    day_of_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    specific_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="unavailabilities")
