"""Sign-in: only valid tokens for known admins reach an organization's routes."""

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import auth
from auth import get_token_verifier
from database import get_db
from main import app
from models import Organization, Season, User
from tests.helpers import TEST_AUDIENCE, TEST_ISSUER

DAN = "dan@dansorg.example.com"
ALEX = "admin@eastonyouth.example.com"
COACH = "coach5@dansorg.example.com"  # a coach, not an admin


@pytest.fixture
def api(db, token_verifier):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_token_verifier] = lambda: token_verifier
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def events_url(db):
    org = db.scalar(select(Organization).where(Organization.name == "Dan's Organization"))
    season = db.scalar(select(Season).where(Season.organization_id == org.id))
    return f"/orgs/{org.id}/seasons/{season.id}/events"


def get(api, url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return api.get(url, headers=headers)


def test_a_valid_token_for_the_organizations_admin_is_accepted(api, events_url, issue_token):
    assert get(api, events_url, issue_token(DAN)).status_code == 200


def test_missing_or_malformed_tokens_are_401(api, events_url, signing_key, issue_token):
    response = get(api, events_url)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"

    now = int(time.time())
    claims = {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "test|dan", "email": DAN,
              "email_verified": True, "exp": now + 3600}
    bad_tokens = {
        "garbage": "not.a.token",
        "wrong issuer": issue_token(DAN, iss="https://evil.example.com"),
        "wrong audience": issue_token(DAN, aud="some-other-api"),
        "expired": issue_token(DAN, exp=now - 3600),
        "no subject": issue_token(DAN, sub=None),
        "unsigned (alg none)": jwt.encode(claims, None, algorithm="none"),
        "shared secret (HS256)": jwt.encode(claims, "x" * 32, algorithm="HS256"),
    }
    for label, token in bad_tokens.items():
        assert get(api, events_url, token).status_code == 401, label


def test_people_without_a_matching_account_are_403(api, events_url, issue_token):
    assert get(api, events_url, issue_token("stranger@example.com")).status_code == 403
    # An unverified email is never trusted to link an account
    assert get(api, events_url, issue_token(DAN, verified=False)).status_code == 403
    assert get(api, events_url, issue_token(None)).status_code == 403


def test_only_admins_of_the_organization_are_let_in(api, events_url, issue_token):
    for email in (ALEX, COACH):
        response = get(api, events_url, issue_token(email))
        assert response.status_code == 403
        assert response.json()["detail"] == "You are not an admin of this organization"


def test_first_sign_in_links_the_account_and_the_link_then_sticks(api, db, events_url, issue_token):
    assert get(api, events_url, issue_token(DAN, subject="idp|dan-123")).status_code == 200
    dan = db.scalar(select(User).where(User.email == DAN))
    assert (dan.auth_issuer, dan.auth_subject) == (TEST_ISSUER, "idp|dan-123")

    # Same person after changing their email at the provider: still Dan
    assert get(api, events_url, issue_token("dan.new@example.com", subject="idp|dan-123")).status_code == 200
    # A different sign-in claiming Dan's email cannot take over the linked account
    assert get(api, events_url, issue_token(DAN, subject="idp|someone-else")).status_code == 403


def test_routes_fail_closed_when_auth_is_not_configured(db, events_url, issue_token, monkeypatch):
    monkeypatch.delenv("AUTH_ISSUER", raising=False)
    monkeypatch.delenv("AUTH_AUDIENCE", raising=False)
    auth._configured_verifier.cache_clear()
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = TestClient(app).get(events_url, headers={"Authorization": f"Bearer {issue_token(DAN)}"})
    finally:
        app.dependency_overrides.clear()
        auth._configured_verifier.cache_clear()
    assert response.status_code == 503
    assert "AUTH_ISSUER" in response.json()["detail"]
