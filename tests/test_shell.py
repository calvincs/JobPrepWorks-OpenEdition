"""The route surface.

Open Edition has no sign-in, but it is still a web server listening on your
machine while you browse other sites. Two properties matter and are pinned
here: everything lives under /app (one prefix, one place to reason about), and
state-changing requests from another origin are refused.
"""

from app.main import app

# Everything the app serves outside the /app prefix. Adding to this list should
# be a deliberate decision, which is the point of the test below.
PUBLIC_PATHS = {
    "/",                                            # redirects into /app
    "/health",
    "/static",
    "/.well-known/appspecific/com.chrome.devtools.json",
}


def _routes():
    return [r for r in app.routes if getattr(r, "path", None)]


def test_every_feature_route_lives_under_app():
    stray = [
        r.path
        for r in _routes()
        if not r.path.startswith("/app") and r.path not in PUBLIC_PATHS
    ]
    assert stray == [], f"routes outside /app and the public allowlist: {stray}"


def test_root_redirects_into_the_app(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/app"


def test_health_is_plain_and_cheap(client):
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_dashboard_renders(client):
    resp = client.get("/app")
    assert resp.status_code == 200
    assert "JobPrep Works" in resp.text


def test_api_docs_are_off_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_cross_site_post_is_blocked(client):
    """Any page you visit while this is running could otherwise POST to
    127.0.0.1 and delete your data or spend your API credits."""
    resp = client.post(
        "/app/account/display",
        data={"theme": "dark"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert resp.status_code == 403


def test_same_origin_post_is_allowed(client):
    resp = client.post(
        "/app/account/display",
        data={"theme": "dark"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert resp.status_code in (200, 303)


def test_cross_origin_header_form_post_is_blocked(client):
    """Fallback path for browsers that didn't send Sec-Fetch-Site."""
    resp = client.post(
        "/app/account/display",
        data={"theme": "dark"},
        headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_get_is_never_blocked_cross_site(client):
    """Reads are safe; blocking them would break ordinary navigation."""
    resp = client.get("/app", headers={"sec-fetch-site": "cross-site"})
    assert resp.status_code == 200
