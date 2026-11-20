from datetime import date, time
from sqlalchemy import select
from database import SessionLocal
from models import (
    Admin,
    Coach,
    Division,
    Field,
    Official,
    Organization,
    Season,
    SlotRule,
    Sport,
    Team,
    TeamCoach,
    Unavailability,
    User,
    Venue,
)


def seed_database() -> None:
    db = SessionLocal()
    try:
        existing_org = db.execute(
            select(Organization).where(Organization.name == "Dan's Organization")
        ).scalar_one_or_none()
        if existing_org:
            print("Database already seeded ('Dan's Organization' exists). Skipping.")
            return

        # 1. Sport
        basketball = Sport(name="Basketball")
        db.add(basketball)
        db.flush()

        # =====================================================================
        # ORGANIZATION 1: Dan's Organization (4 Divisions, 32 Teams: 1A - 4H)
        # =====================================================================
        dans_org = Organization(name="Dan's Organization", status="active", timezone="America/New_York")
        db.add(dans_org)
        db.flush()

        # Admin User
        dan_user = User(email="dan@dansorg.example.com", first_name="Dan", last_name="Director")
        db.add(dan_user)
        db.flush()
        db.add(Admin(user_id=dan_user.id, organization_id=dans_org.id))

        # Season: Spring 2027 (April 1 - June 30, 2027); June 24-30 is the makeup week
        spring_2027 = Season(
            organization_id=dans_org.id,
            name="Spring 2027",
            start_date=date(2027, 4, 1),
            end_date=date(2027, 6, 30),
            sport_id=basketball.id,
            makeup_weeks=1,
        )
        db.add(spring_2027)
        db.flush()

        # Venue & 4 Fields
        complex_venue = Venue(organization_id=dans_org.id, name="Dan's Athletic Complex")
        db.add(complex_venue)
        db.flush()

        fields = []
        for idx in range(1, 5):
            f = Field(
                organization_id=dans_org.id,
                venue_id=complex_venue.id,
                sport_id=basketball.id,
                name=f"Court {idx}",
            )
            db.add(f)
            fields.append(f)
        db.flush()

        # Slot Rules (Mon-Fri 16:00-20:00 & Sat-Sun 10:00-20:00 in 120-min blocks)
        for f in fields:
            for dow in range(0, 5):  # Mon=0 .. Fri=4
                db.add(
                    SlotRule(
                        organization_id=dans_org.id,
                        season_id=spring_2027.id,
                        field_id=f.id,
                        division_id=None,
                        day_of_week=dow,
                        start_time=time(16, 0),
                        end_time=time(20, 0),
                        slot_duration_mins=120,
                    )
                )
            for dow in (5, 6):  # Sat=5, Sun=6
                db.add(
                    SlotRule(
                        organization_id=dans_org.id,
                        season_id=spring_2027.id,
                        field_id=f.id,
                        division_id=None,
                        day_of_week=dow,
                        start_time=time(10, 0),
                        end_time=time(20, 0),
                        slot_duration_mins=120,
                    )
                )

        # Divisions & 32 Teams (1A-1H in 12U, 2A-2H in 14U, 3A-3H in 16U, 4A-4H in 18U)
        division_specs = [
            ("12U", "1"),
            ("14U", "2"),
            ("16U", "3"),
            ("18U", "4"),
        ]
        team_letters = ["A", "B", "C", "D", "E", "F", "G", "H"]
        teams_by_code: dict[str, Team] = {}
        coaches_list: list[Coach] = []

        # Create 31 coaches (so 1 coach manages two teams: 1A and 3C)
        for i in range(1, 32):
            u = User(
                email=f"coach{i}@dansorg.example.com",
                first_name=f"Coach{i}",
                last_name="Smith",
            )
            db.add(u)
            db.flush()
            c = Coach(user_id=u.id, organization_id=dans_org.id)
            db.add(c)
            coaches_list.append(c)
        db.flush()

        coach_cursor = 0
        shared_coach = coaches_list[0]  # Will coach both 1A and 3C

        for div_name, prefix in division_specs:
            div = Division(
                organization_id=dans_org.id,
                season_id=spring_2027.id,
                name=div_name,
                games_per_team=7,
                max_games_per_week=2,
            )
            db.add(div)
            db.flush()

            for letter in team_letters:
                code = f"{prefix}{letter}"
                t = Team(
                    organization_id=dans_org.id,
                    season_id=spring_2027.id,
                    division_id=div.id,
                    name=f"Team {code}",
                )
                db.add(t)
                db.flush()
                teams_by_code[code] = t

                if code in ("1A", "3C"):
                    assigned_coach = shared_coach
                else:
                    coach_cursor += 1
                    assigned_coach = coaches_list[coach_cursor]

                db.add(TeamCoach(team_id=t.id, coach_id=assigned_coach.id, organization_id=dans_org.id))

        # Officials (6 referees)
        officials_list: list[Official] = []
        for i in range(1, 7):
            u = User(
                email=f"ref{i}@dansorg.example.com",
                first_name=f"Ref{i}",
                last_name="Jones",
            )
            db.add(u)
            db.flush()
            off = Official(user_id=u.id, organization_id=dans_org.id)
            db.add(off)
            officials_list.append(off)
        db.flush()

        # Sample Unavailabilities (Recurring + Specific Date + Organization-wide Blackout)
        db.add(
            Unavailability(
                organization_id=dans_org.id,
                target_type="coach",
                coach_id=shared_coach.id,
                day_of_week=2,  # Wednesday
                specific_date=None,
                start_time=time(16, 0),
                end_time=time(18, 0),
                reason="Shared coach unavailable Wednesdays 4-6 PM",
            )
        )
        db.add(
            Unavailability(
                organization_id=dans_org.id,
                target_type="team",
                team_id=teams_by_code["2B"].id,
                day_of_week=None,
                specific_date=date(2027, 4, 10),
                start_time=time(8, 0),
                end_time=time(22, 0),
                reason="Team 2B school tournament blackout",
            )
        )
        db.add(
            Unavailability(
                organization_id=dans_org.id,
                target_type="official",
                official_id=officials_list[0].id,
                day_of_week=6,  # Sunday
                specific_date=None,
                start_time=time(10, 0),
                end_time=time(14, 0),
                reason="Ref 1 unavailable Sunday mornings",
            )
        )
        db.add(
            Unavailability(
                organization_id=dans_org.id,
                target_type="organization",
                day_of_week=None,
                specific_date=date(2027, 5, 31),
                reason="Memorial Day: all courts closed",
            )
        )

        # =====================================================================
        # ORGANIZATION 2: Easton Youth Athletics (Multi-Tenant Isolation Test)
        # =====================================================================
        org2 = Organization(name="Easton Youth Athletics", status="active", timezone="America/New_York")
        db.add(org2)
        db.flush()

        org2_admin = User(email="admin@eastonyouth.example.com", first_name="Alex", last_name="Rivera")
        db.add(org2_admin)
        db.flush()
        db.add(Admin(user_id=org2_admin.id, organization_id=org2.id))

        org2_season = Season(
            organization_id=org2.id,
            name="Spring 2027",
            start_date=date(2027, 4, 1),
            end_date=date(2027, 6, 30),
            sport_id=basketball.id,
        )
        db.add(org2_season)
        db.flush()

        org2_venue = Venue(organization_id=org2.id, name="Easton Community Park")
        db.add(org2_venue)
        db.flush()

        org2_field = Field(
            organization_id=org2.id,
            venue_id=org2_venue.id,
            sport_id=basketball.id,
            name="Main Court",
        )
        db.add(org2_field)
        db.flush()

        org2_div = Division(
            organization_id=org2.id,
            season_id=org2_season.id,
            name="U10",
            games_per_team=3,
            max_games_per_week=1,
        )
        db.add(org2_div)
        db.flush()

        for t_name in ("Easton Red", "Easton Blue", "Easton White", "Easton Gold"):
            db.add(
                Team(
                    organization_id=org2.id,
                    season_id=org2_season.id,
                    division_id=org2_div.id,
                    name=t_name,
                )
            )

        db.commit()
        print("Successfully seeded Dan's Organization (32 teams, 4 divisions) and Easton Youth Athletics!")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
