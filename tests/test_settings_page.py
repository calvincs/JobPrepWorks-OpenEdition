"""The Settings page: résumé header details, theme, and the readouts of the
live model configuration and where your data lives on disk."""

from app.services import users as U


def test_page_renders_the_live_provider_configuration(client):
    resp = client.get("/app/account")
    assert resp.status_code == 200
    # conftest pins the mock provider, so that's what the page must report.
    assert "mock" in resp.text


def test_saving_the_resume_header(client):
    resp = client.post(
        "/app/account/name",
        data={"first_name": "  Calvin  ", "last_name": " Schultz ",
              "contact_email": "me@example.com", "contact_phone": "555-0100"},
        headers={"hx-request": "true"},
    )
    assert resp.status_code == 200
    user = U.get_user(1)
    assert user["name"] == "Calvin Schultz"          # trimmed and derived
    assert user["contact_email"] == "me@example.com"
    assert user["contact_phone"] == "555-0100"


def test_blank_first_name_is_refused(client):
    resp = client.post(
        "/app/account/name", data={"first_name": "   "}, headers={"hx-request": "true"}
    )
    assert "first name" in resp.headers.get("HX-Trigger", "")
    assert U.get_user(1)["name"] == ""


def test_theme_is_stored_and_stamped_on_the_page(client):
    client.post("/app/account/display", data={"theme": "dark"},
                headers={"hx-request": "true"})
    assert U.get_user(1)["theme"] == "dark"
    assert 'data-theme="dark"' in client.get("/app").text


def test_invalid_theme_is_refused(client):
    client.post("/app/account/display", data={"theme": "neon"},
                headers={"hx-request": "true"})
    assert U.get_user(1)["theme"] == "system"
