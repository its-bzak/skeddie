from collections import defaultdict
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database import get_db
from main import app
from models import Division, Event, Organization, Season, TimeSlot


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def org_and_season(db, org_name: str):
    org = db.scalar(select(Organization).where(Organization.name == org_name))
    season = db.scalar(select(Season).where(Season.organization_id == org.id))
    return org, season


@pytest.fixture
def scheduled(client, db):
    org, season = org_and_season(db, "Dan's Organization")
    base = f"/orgs/{org.id}/seasons/{season.id}"
    generated = client.post(f"{base}/slots/generate").json()
    assert (generated["created"], generated["blacked_out"]) == (1032, 8)  # Memorial Day is blacked out
    response = client.post(f"{base}/schedule", json={"time_limit_seconds": 60})
    assert response.status_code == 200, response.json()
    return org, season, base


def test_full_flow_generate_schedule_list_publish(client, scheduled):
    org, season, base = scheduled

    events = client.get(f"{base}/events").json()
    assert len(events) == 112
    assert all(e["status"] == "Draft" and e["slot"] is not None for e in events)
    starts = [e["slot"]["start_time"] for e in events]
    assert starts == sorted(starts)

    assert client.post(f"{base}/publish").json() == {"published": 112}
    assert {e["status"] for e in client.get(f"{base}/events").json()} == {"Published"}

    again = client.post(f"{base}/slots/generate").json()
    assert (again["created"], again["already_present"], again["blacked_out"]) == (0, 1032, 8)


def test_moving_a_game_onto_a_teams_other_game_is_a_409_naming_the_team(client, db, scheduled):
    org, season, base = scheduled
    events = client.get(f"{base}/events").json()
    by_team = defaultdict(list)
    for e in events:
        by_team[e["home_team"]["name"]].append(e)
        by_team[e["away_team"]["name"]].append(e)

    # Team 1B plays `anchor` at time T. Free a court at T by cancelling a game
    # that does not involve 1B, then try to move another 1B game onto it.
    anchor, mover = by_team["Team 1B"][0], by_team["Team 1B"][1]
    at_t = [e for e in events if e["slot"]["start_time"] == anchor["slot"]["start_time"]]
    victim = next(e for e in at_t if "Team 1B" not in (e["home_team"]["name"], e["away_team"]["name"]))
    assert client.patch(f"/orgs/{org.id}/events/{victim['id']}", json={"status": "Cancelled"}).status_code == 200

    response = client.patch(
        f"/orgs/{org.id}/events/{mover['id']}", json={"time_slot_id": victim["slot"]["id"]}
    )
    assert response.status_code == 409
    # Both teams of the moved game are checked; name whichever is double-booked
    still_playing_at_t = {
        name
        for e in at_t
        if e["id"] != victim["id"]
        for name in (e["home_team"]["name"], e["away_team"]["name"])
    }
    named = response.json()["detail"].removesuffix(" already has a game at that time.")
    assert named in still_playing_at_t & {mover["home_team"]["name"], mover["away_team"]["name"]}

    # Nothing changed
    unchanged = client.get(f"{base}/events").json()
    assert next(e for e in unchanged if e["id"] == mover["id"])["slot"] == mover["slot"]


def test_moving_to_a_free_time_and_cancelling(client, db, scheduled):
    org, season, base = scheduled
    events = client.get(f"{base}/events").json()
    game = events[0]
    teams = {game["home_team"]["id"], game["away_team"]["id"]}
    busy = [
        (datetime.fromisoformat(e["slot"]["start_time"]), datetime.fromisoformat(e["slot"]["end_time"]))
        for e in events
        if {e["home_team"]["id"], e["away_team"]["id"]} & teams
    ]
    free = next(
        s for s in db.scalars(
            select(TimeSlot).where(TimeSlot.season_id == season.id, TimeSlot.event_id.is_(None))
            .order_by(TimeSlot.start_time.desc())
        )
        if all(not (s.start_time < end and start < s.end_time) for start, end in busy)
    )

    moved = client.patch(f"/orgs/{org.id}/events/{game['id']}", json={"time_slot_id": str(free.id)})
    assert moved.status_code == 200, moved.json()
    assert moved.json()["slot"]["id"] == str(free.id)

    taken = next(e for e in events if e["id"] != game["id"])["slot"]["id"]
    assert client.patch(f"/orgs/{org.id}/events/{game['id']}", json={"time_slot_id": taken}).status_code == 409

    cancelled = client.patch(f"/orgs/{org.id}/events/{game['id']}", json={"status": "Cancelled"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "Cancelled" and cancelled.json()["slot"] is None
    assert client.patch(
        f"/orgs/{org.id}/events/{game['id']}", json={"time_slot_id": str(free.id)}
    ).status_code == 409

    assert client.patch(f"/orgs/{org.id}/events/{game['id']}", json={}).status_code == 422


def test_other_organizations_cannot_see_or_touch_the_season(client, db, scheduled):
    org, season, base = scheduled
    easton, _ = org_and_season(db, "Easton Youth Athletics")
    event_id = client.get(f"{base}/events").json()[0]["id"]

    assert client.get(f"/orgs/{easton.id}/seasons/{season.id}/events").status_code == 404
    assert client.post(f"/orgs/{easton.id}/seasons/{season.id}/schedule").status_code == 404
    assert client.patch(f"/orgs/{easton.id}/events/{event_id}", json={"status": "Cancelled"}).status_code == 404


def test_failed_schedule_reports_issues_and_keeps_the_previous_drafts(client, db, scheduled):
    org, season, base = scheduled
    before = db.scalar(select(func.count()).select_from(Event).where(Event.season_id == season.id))

    division = db.scalar(select(Division).where(Division.season_id == season.id, Division.name == "12U"))
    division.games_per_team = 40  # 40 games at 2 per week cannot fit in 13 weeks
    db.commit()

    response = client.post(f"{base}/schedule")
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "precheck_failed"
    assert any("allows 2 per week" in issue for issue in body["issues"])
    assert db.scalar(select(func.count()).select_from(Event).where(Event.season_id == season.id)) == before


def test_easton_without_slots_gets_a_readable_422(client, db):
    org, season = org_and_season(db, "Easton Youth Athletics")
    response = client.post(f"/orgs/{org.id}/seasons/{season.id}/schedule")
    assert response.status_code == 422
    assert any("open time slots" in issue for issue in response.json()["issues"])
