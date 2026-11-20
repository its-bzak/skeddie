from collections import defaultdict
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from auth import get_token_verifier
from database import get_db
from main import app
from models import Division, Event, Organization, Season, TimeSlot, User
from routers.scheduling import get_run_submitter
from services.runs import create_run, execute_run

PERFECT = {"off_pace_games": 0, "same_day_doubles": 0, "back_to_backs": 0, "proven_optimal": True}
DAN = "dan@dansorg.example.com"  # admin of Dan's Organization (seed data)
ALEX = "admin@eastonyouth.example.com"  # admin of Easton Youth Athletics


@pytest.fixture
def client(db, token_verifier, issue_token):
    """API client on the test transaction, signed in as Dan.

    Scheduling runs execute inline, inside the request's scoped session, before POST returns.
    """
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_token_verifier] = lambda: token_verifier
    app.dependency_overrides[get_run_submitter] = lambda: lambda run_id, requested_by: execute_run(db, run_id)
    try:
        yield TestClient(app, headers={"Authorization": f"Bearer {issue_token(DAN)}"})
    finally:
        app.dependency_overrides.clear()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
    response = client.post(f"{base}/schedule-runs", json={"time_limit_seconds": 60})
    assert response.status_code == 202, response.json()
    run = response.json()
    assert (run["status"], run["games_scheduled"], run["quality"]) == ("scheduled", 112, PERFECT)
    return org, season, base


def test_runs_can_be_fetched_and_listed(client, scheduled):
    org, season, base = scheduled
    (latest,) = client.get(f"{base}/schedule-runs").json()
    fetched = client.get(f"/orgs/{org.id}/schedule-runs/{latest['id']}").json()
    assert fetched == latest
    assert fetched["started_at"] is not None and fetched["finished_at"] is not None
    assert fetched["time_limit_seconds"] == 60 and fetched["error"] is None


def test_a_second_run_while_one_is_active_is_a_409(client, db):
    org, season = org_and_season(db, "Dan's Organization")
    dan = db.scalar(select(User).where(User.email == DAN))
    active = create_run(db, season, 60, requested_by=dan.id)
    db.commit()

    response = client.post(f"/orgs/{org.id}/seasons/{season.id}/schedule-runs")
    assert response.status_code == 409
    assert response.json()["run_id"] == str(active.id)


def test_time_limit_is_validated(client, db):
    org, season = org_and_season(db, "Dan's Organization")
    url = f"/orgs/{org.id}/seasons/{season.id}/schedule-runs"
    assert client.post(url, json={"time_limit_seconds": 0}).status_code == 422
    assert client.post(url, json={"time_limit_seconds": 901}).status_code == 422


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


def test_admins_cannot_reach_another_organization(client, db, scheduled, issue_token):
    org, season, base = scheduled
    easton, _ = org_and_season(db, "Easton Youth Athletics")
    event_id = client.get(f"{base}/events").json()[0]["id"]
    run_id = client.get(f"{base}/schedule-runs").json()[0]["id"]

    # Dan is not an admin of Easton
    assert client.get(f"/orgs/{easton.id}/seasons/{season.id}/events").status_code == 403
    assert client.post(f"/orgs/{easton.id}/seasons/{season.id}/schedule-runs").status_code == 403

    # Alex is an admin of Easton; Dan's season, run and games are invisible to him
    alex = bearer(issue_token(ALEX))
    assert client.get(f"{base}/events", headers=alex).status_code == 403
    assert client.get(f"/orgs/{easton.id}/seasons/{season.id}/events", headers=alex).status_code == 404
    assert client.get(f"/orgs/{easton.id}/seasons/{season.id}/schedule-runs", headers=alex).status_code == 404
    assert client.get(f"/orgs/{easton.id}/schedule-runs/{run_id}", headers=alex).status_code == 404
    patch = client.patch(f"/orgs/{easton.id}/events/{event_id}", json={"status": "Cancelled"}, headers=alex)
    assert patch.status_code == 404


def test_failed_schedule_reports_issues_and_keeps_the_previous_drafts(client, db, scheduled):
    org, season, base = scheduled
    before = db.scalar(select(func.count()).select_from(Event).where(Event.season_id == season.id))

    division = db.scalar(select(Division).where(Division.season_id == season.id, Division.name == "12U"))
    division.games_per_team = 40  # 40 games at 2 per week cannot fit in 13 weeks
    db.commit()

    response = client.post(f"{base}/schedule-runs")
    assert response.status_code == 202
    run = response.json()
    assert (run["status"], run["quality"]) == ("precheck_failed", None)
    assert any("allows 2 per week" in issue for issue in run["issues"])
    assert db.scalar(select(func.count()).select_from(Event).where(Event.season_id == season.id)) == before


def test_easton_without_slots_gets_readable_issues(client, db, issue_token):
    org, season = org_and_season(db, "Easton Youth Athletics")
    url = f"/orgs/{org.id}/seasons/{season.id}/schedule-runs"
    run = client.post(url, headers=bearer(issue_token(ALEX))).json()
    assert run["status"] == "precheck_failed"
    assert any("open time slots" in issue for issue in run["issues"])
