"""Dashboard skeleton tests.

Step 2 scope:
  * /healthz returns 200 without auth
  * Tab routes require auth; unauthenticated requests redirect to /login
  * Valid login sets a signed session cookie
  * Invalid login returns 303 back to /login with ?error=invalid
  * Rate limit on /login blocks after N attempts per minute
  * All six tab slugs render their stub pages once authenticated

All tests run against a create_app() instance with a known-good
DashboardConfig -- no real env vars touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kalshi_arb.dashboard.app import SESSION_COOKIE, TABS, create_app
from kalshi_arb.dashboard.config import DashboardConfig


@pytest.fixture
def config() -> DashboardConfig:
    return DashboardConfig(
        username="admin",
        password="hunter2-test",
        session_secret="test-secret-do-not-ship",
        port=8080,
        login_rate_per_min_per_ip=5,
        libsql_url="",
        libsql_auth_token="",
        libsql_sync_url="",
        libsql_local_path=__import__("pathlib").Path("/tmp/unused-for-step-2.db"),
    )


@pytest.fixture
def client(config: DashboardConfig) -> TestClient:
    # base_url="https://testserver" so httpx's cookie jar persists our
    # Secure-flagged session cookie across requests. The app itself
    # always sets Secure=True -- we're just making the test client
    # speak HTTPS so that policy doesn't drop the cookie in transit.
    return TestClient(
        create_app(config),
        follow_redirects=False,
        base_url="https://testserver",
    )


# ---- /healthz is public ----


def test_healthz_returns_200_without_auth(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["step"] == 2


# ---- / redirects based on session presence ----


def test_root_without_session_redirects_to_login(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---- Tab routes require auth ----


@pytest.mark.parametrize("slug", [t[0] for t in TABS])
def test_tabs_without_session_redirect_to_login(client: TestClient, slug: str) -> None:
    r = client.get(f"/{slug}")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---- Login flow ----


def test_login_valid_credentials_sets_session_cookie_and_redirects(
    client: TestClient,
) -> None:
    r = client.post(
        "/login",
        data={"username": "admin", "password": "hunter2-test"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/overview"
    assert SESSION_COOKIE in r.cookies


def test_login_invalid_credentials_returns_error(client: TestClient) -> None:
    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=invalid"
    assert SESSION_COOKIE not in r.cookies


def test_login_wrong_username_same_behavior_as_wrong_password(client: TestClient) -> None:
    # Uniform response prevents username enumeration.
    r1 = client.post("/login", data={"username": "nobody", "password": "hunter2-test"})
    r2 = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert r1.status_code == r2.status_code == 303
    assert r1.headers["location"] == r2.headers["location"]


# ---- Post-auth tab access ----


@pytest.mark.parametrize("slug,title", [(s, t) for s, t, _ in TABS])
def test_authed_user_can_access_all_tabs(
    client: TestClient, slug: str, title: str
) -> None:
    # Log in once.
    r = client.post("/login", data={"username": "admin", "password": "hunter2-test"})
    assert r.status_code == 303
    # Hit the tab.
    r = client.get(f"/{slug}")
    assert r.status_code == 200, f"tab {slug} returned {r.status_code}: {r.text[:200]}"
    assert title.lower() in r.text.lower() or slug in r.text.lower()


# ---- Logout ----


def test_logout_clears_session_cookie(client: TestClient) -> None:
    client.post("/login", data={"username": "admin", "password": "hunter2-test"})
    r = client.get("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # The set-cookie on logout uses max-age=0 / empty value.
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookie


# ---- Rate limit on /login ----


def test_login_rate_limit_blocks_after_threshold(config: DashboardConfig) -> None:
    """config.login_rate_per_min_per_ip=5 -> 6th attempt returns 429."""
    c = TestClient(
        create_app(config), follow_redirects=False, base_url="https://testserver"
    )
    for _ in range(config.login_rate_per_min_per_ip):
        r = c.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 303
    # Next attempt crosses the threshold.
    r = c.post("/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 429


# ---- Config refuses to start without DASHBOARD_PASSWORD ----


def test_config_refuses_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        DashboardConfig.load()


# ---- Docs endpoints are NOT exposed (read-only posture) ----


def test_docs_endpoints_404(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
