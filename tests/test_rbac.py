"""RBAC red-team: authentication, sessions and every role against every
protected action. No LLM calls: anything past the permission check is stubbed."""

import secrets
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import auth
import evaluation
import history
import knowledge_base
import research_ui_server as server
from conftest import signed_in_client

ROLES = list(auth.ROLES)
CLIENTS = {role: signed_in_client(role) for role in ROLES}
PASSWORD = "Test-Password-123"


def new_user(role="research_associate", must_change=False):
    username = f"u-{secrets.token_hex(4)}"
    user = auth.create_user(username, "Someone", role, PASSWORD, must_change=must_change)
    return username, user


def anonymous():
    return TestClient(server.app)


# ---------- signed-out access ----------

def test_pages_redirect_to_login():
    response = anonymous().get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/api/runs", "/api/kb", "/api/health", "/api/evals", "/api/users", "/api/auth/me"])
def test_apis_require_sign_in(path):
    assert anonymous().get(path).status_code == 401


def test_login_page_and_share_links_stay_public():
    client = anonymous()
    assert client.get("/login").status_code == 200
    assert client.get("/api/share/not-a-token").status_code == 404  # public route, unknown token


# ---------- signing in ----------

def test_wrong_password_gives_generic_error():
    username, _ = new_user()
    wrong = anonymous().post("/api/auth/login", json={"username": username, "password": "nope"})
    unknown = anonymous().post("/api/auth/login", json={"username": "no-such-user", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # doesn't reveal which usernames exist


def test_lockout_after_repeated_failures():
    username, _ = new_user()
    client = anonymous()
    for _ in range(auth.MAX_FAILURES):
        client.post("/api/auth/login", json={"username": username, "password": "wrong"})
    locked = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert locked.status_code == 401 and "Too many failed attempts" in locked.json()["detail"]


def test_sign_in_attempts_are_rate_limited():
    from security import RateLimiter
    with patch.object(server, "login_limiter", RateLimiter(2, window=900)):
        client = anonymous()
        codes = [client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_session_cookie_is_httponly():
    username, _ = new_user()
    response = anonymous().post("/api/auth/login", json={"username": username, "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_https_cookie_works_inside_the_hugging_face_iframe():
    username, _ = new_user()
    client = TestClient(server.app, base_url="https://testserver")
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD, "remember": True})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=None", "Partitioned", f"Max-Age={auth.REMEMBER_DAYS * 86400}"):
        assert flag in cookie
    assert client.get("/api/auth/me").status_code == 200
    logout = client.post("/api/auth/logout")
    assert "Max-Age=0" in logout.headers["set-cookie"] and "Partitioned" in logout.headers["set-cookie"]


def test_passwords_are_hashed_not_stored():
    stored = auth._conn.execute("SELECT password_hash FROM users LIMIT 1").fetchone()[0]
    assert stored.startswith("scrypt$") and PASSWORD not in stored


def test_logout_ends_the_session():
    client = signed_in_client("research_associate")
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_temporary_password_must_be_changed_first():
    username, _ = new_user(must_change=True)
    client = anonymous()
    client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert client.get("/api/runs").status_code == 403
    assert client.get("/api/auth/me").json()["user"]["must_change_password"]
    weak = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 400
    ok = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "New-Password-456"})
    assert ok.status_code == 200
    assert client.get("/api/runs").status_code == 200


def test_cross_site_requests_are_blocked():
    response = CLIENTS["principal_investigator"].post(
        "/api/kb/note", json={"title": "x", "text": "y"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and "Cross-site" in response.json()["detail"]


# ---------- permission matrix: every role x every protected action ----------

PERMITTED = {role: auth.ROLES[role]["permissions"] for role in ROLES}
STOP = 418  # an allowed request is stopped right after the permission check


def stop(*_args, **_kwargs):
    raise HTTPException(status_code=STOP)


ACTIONS = [
    # (name, permission, method, path, json body, extra patches)
    ("run research", "research.run", "post", "/api/research", {"topic": "test topic"}, {"enforce_guard": stop}),
    ("ask follow-up", "research.run", "post", "/api/reports/none/chat", {"question": "why?"}, {}),
    ("upload document", "kb.upload", "post", "/api/kb/note", {"title": "t", "text": "x"}, {"enforce_rate_limit": stop}),
    ("delete document", "kb.delete", "delete", "/api/kb/documents/none.md", None, {}),
    ("view evaluations", "eval.run", "get", "/api/evals", None, {}),
    ("run evaluations", "eval.run", "post", "/api/evals/run", {}, {"enforce_rate_limit": stop}),
    ("list team", "users.manage", "get", "/api/users", None, {}),
    ("add member", "users.manage", "post", "/api/users", {"username": "xyz", "name": "x", "role": "nope"}, {}),
    ("open trace", "costs.view", "get", "/api/runs/none/trace", None, {}),
    ("read reports", "reports.view", "get", "/api/runs", None, {}),
    ("read knowledge base", "reports.view", "get", "/api/kb", None, {}),
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("action", ACTIONS, ids=[a[0] for a in ACTIONS])
def test_permission_matrix(role, action):
    name, permission, method, path, body, patches = action
    with patch.multiple(server, **patches) if patches else patch.object(server, "log"):
        response = getattr(CLIENTS[role], method)(path, **({"json": body} if body is not None else {}))
    allowed = permission in PERMITTED[role]
    if allowed:
        assert response.status_code != 403, f"{role} should be able to {name}"
    else:
        assert response.status_code == 403, f"{role} must not be able to {name} (got {response.status_code})"


# ---------- report ownership and cost visibility ----------

def make_run(owner_id: int) -> str:
    run_id = uuid.uuid4().hex
    history.start_run(run_id, "owned topic", ["web"], owner_id)
    usage = {"llm_calls": 1, "input_tokens": 1, "output_tokens": 1, "cost": 0.01, "estimated": False, "by_step": {}}
    history.finish_run(run_id, {"sources": [], "report": "r", "quality_score": 0.8}, 1.0, usage)
    return run_id


def test_associates_delete_only_their_own_reports():
    owner = signed_in_client("research_associate")
    other = signed_in_client("research_associate")
    run_id = make_run(owner.user["id"])
    assert other.delete(f"/api/runs/{run_id}").status_code == 403
    assert owner.delete(f"/api/runs/{run_id}").status_code == 200


def test_officers_can_delete_anyones_reports():
    run_id = make_run(CLIENTS["research_associate"].user["id"])
    assert CLIENTS["research_officer"].delete(f"/api/runs/{run_id}").status_code == 200


def test_reviewers_cannot_share_reports():
    run_id = make_run(CLIENTS["research_associate"].user["id"])
    assert CLIENTS["guest_reviewer"].post(f"/api/runs/{run_id}/share").status_code == 403


def test_costs_hidden_from_roles_without_permission():
    run_id = make_run(CLIENTS["research_associate"].user["id"])
    state = {"report": "r", "sources": [], "findings": [], "quality_score": 0.8}
    with patch.object(history, "load_state", return_value=state):
        associate = CLIENTS["research_associate"].get(f"/api/runs/{run_id}").json()
        officer = CLIENTS["research_officer"].get(f"/api/runs/{run_id}").json()
    assert associate["run"]["cost"] is None and associate["result"]["usage"] is None
    assert officer["run"]["cost"] == 0.01 and officer["result"]["usage"]["cost"] == 0.01


# ---------- team management ----------

def test_pi_adds_member_with_one_time_password():
    response = CLIENTS["principal_investigator"].post(
        "/api/users", json={"username": f"new-{secrets.token_hex(3)}", "name": "New Member", "role": "research_officer"})
    data = response.json()
    assert response.status_code == 200 and data["user"]["must_change_password"]
    assert len(data["temporary_password"]) >= 10


def test_deactivated_member_is_signed_out():
    member = signed_in_client("research_associate")
    CLIENTS["principal_investigator"].patch(f"/api/users/{member.user['id']}", json={"active": False})
    assert member.get("/api/auth/me").status_code == 401


def test_cannot_remove_the_last_principal_investigator():
    username, pi = new_user("principal_investigator")
    with patch.object(auth, "_active_pis", return_value=0):
        response = CLIENTS["principal_investigator"].patch(f"/api/users/{pi['id']}", json={"role": "guest_reviewer"})
    assert response.status_code == 400 and "at least one" in response.json()["detail"]


def test_cannot_deactivate_yourself():
    pi = CLIENTS["principal_investigator"]
    assert pi.patch(f"/api/users/{pi.user['id']}", json={"active": False}).status_code == 400
