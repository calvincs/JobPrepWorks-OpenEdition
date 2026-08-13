"""Profile facts (manual add + dedup), free-text parse, document extraction, and the user name."""

import uuid

from app.db import public_id_of
from app.llm.base import LLMError
from app.services import documents as D
from app.services import profile as P
from app.services import users as U


def test_manual_fact_add_and_dedup_merge():
    fid, action = P.create_manual_fact(user_id=1, kind="skill", name="Python", proficiency="advanced")
    assert action == "created"
    # Same skill (different case) merges into the existing fact, not a duplicate.
    fid2, action2 = P.create_manual_fact(user_id=1, kind="skill", name="python", proficiency="expert")
    assert action2 == "updated" and fid2 == fid
    skills = P.facts_grouped(1)["skill"]
    assert len(skills) == 1
    assert skills[0]["name"] == "Python"  # original casing kept
    assert skills[0]["proficiency"] == "expert"  # user's new value wins


def test_same_title_role_different_employer_not_merged():
    P.create_manual_fact(user_id=1, kind="role", name="Staff Engineer", organization="Acme")
    P.create_manual_fact(user_id=1, kind="role", name="Staff Engineer", organization="Globex")
    roles = [r for r in P.facts_grouped(1)["role"] if r["name"] == "Staff Engineer"]
    assert len(roles) == 2


def test_document_extraction_creates_facts(scalar):
    doc = D.save_upload("resume.txt", b"Experienced engineer. Python, FastAPI, SQL.", user_id=1)
    P.process_document(doc)
    assert sum(len(v) for v in P.facts_grouped(1).values()) > 0
    assert scalar("SELECT status FROM documents WHERE id = ?", doc) == "ready"


def test_delete_document_orphans_sole_source_facts(scalar):
    doc = D.save_upload("resume.txt", b"Experienced engineer. Python, FastAPI, SQL.", user_id=1)
    P.process_document(doc)
    n = sum(len(v) for v in P.facts_grouped(1).values())
    D.delete_document(doc, 1)
    # Facts remain but become orphaned (their only source is gone).
    orphaned = scalar("SELECT COUNT(*) FROM profile_facts WHERE orphaned = 1")
    assert orphaned == n and n > 0


def test_parse_route_creates_facts_from_free_text(client, scalar):
    r = client.post(
        "/app/profile/facts/parse",
        data={"text": "Five years at Globex as a data engineer, building Spark pipelines."},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200 and "close-dialog" in r.headers.get("HX-Trigger", "")
    # TestClient runs the background task synchronously: the parse is done.
    assert scalar("SELECT status FROM fact_parses ORDER BY id DESC LIMIT 1") == "ready"
    groups = P.facts_grouped(1)
    assert any(f["name"] == "Data Engineer" for f in groups["role"])  # canned FactParse
    assert any(f["name"] == "Spark" for f in groups["skill"])
    # Parsed facts are manual facts: user-owned, no document source.
    assert scalar("SELECT user_edited FROM profile_facts WHERE name = 'Spark'") == 1
    assert scalar("SELECT COUNT(*) FROM fact_sources") == 0


def test_parse_dedups_against_existing_facts(client):
    P.create_manual_fact(user_id=1, kind="skill", name="spark", proficiency="beginner")
    client.post("/app/profile/facts/parse", data={"text": "I know Spark well"}, headers={"hx-request": "true"})
    sparks = [f for f in P.facts_grouped(1)["skill"] if f["name"].lower() == "spark"]
    assert len(sparks) == 1  # merged, not duplicated
    assert sparks[0]["proficiency"] == "advanced"  # newly parsed value wins


def test_parse_blank_text_rejected(client, scalar):
    r = client.post("/app/profile/facts/parse", data={"text": "   "}, headers={"hx-request": "true"})
    assert r.status_code == 200 and "error" in r.headers.get("HX-Trigger", "")
    assert scalar("SELECT COUNT(*) FROM fact_parses") == 0


def test_parse_poll_delivers_toast_and_deletes_row(client, scalar):
    parse_id = P.create_fact_parse("Spark pipelines at Globex", 1)
    pub = public_id_of("fact_parses", parse_id)
    P.process_fact_parse(parse_id)
    r = client.get(f"/app/profile/facts/parses/{pub}")
    assert r.status_code == 200
    assert "Added" in r.headers.get("HX-Trigger", "")  # summary toast
    assert "refresh-facts" in r.headers.get("HX-Trigger", "")  # section re-render
    assert r.text == ""  # empty swap removes the banner
    # The transient row is gone once delivered; the next poll 404s.
    assert scalar("SELECT COUNT(*) FROM fact_parses WHERE id = ?", parse_id) == 0
    assert client.get(f"/app/profile/facts/parses/{pub}").status_code == 404


def test_parse_error_shows_dismissible_banner(client, scalar, monkeypatch):
    class Boom:
        def extract(self, **kw):
            raise LLMError("provider down")

    monkeypatch.setattr(P, "get_provider", lambda: Boom())
    parse_id = P.create_fact_parse("some text", 1)
    P.process_fact_parse(parse_id)
    assert scalar("SELECT status FROM fact_parses WHERE id = ?", parse_id) == "error"
    assert "provider down" in client.get("/app/profile").text  # banner on the page
    pub = public_id_of("fact_parses", parse_id)
    client.post(f"/app/profile/facts/parses/{pub}/dismiss")
    assert scalar("SELECT COUNT(*) FROM fact_parses WHERE id = ?", parse_id) == 0


def test_gap_origin_manual_fact_answers_with_toast_only(client, scalar):
    # The Fit tab's gap dialog posts origin=gap: no #facts-section exists
    # there, so the response is an empty body + toast/close-dialog triggers.
    r = client.post(
        "/app/profile/facts",
        data={"kind": "skill", "name": "AWS", "origin": "gap"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200 and r.text == ""
    trig = r.headers.get("HX-Trigger", "")
    assert "close-dialog" in trig and "Re-analyze" in trig
    # No job_pid posted → no gap gets checked off, just the fact + toast.
    assert "gap-resolved" not in trig
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions") == 0
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE name = 'AWS'") == 1
    # Validation failures answer the same shape (toast only, no close).
    r = client.post(
        "/app/profile/facts",
        data={"kind": "skill", "name": "   ", "origin": "gap"},
        headers={"hx-request": "true"},
    )
    assert r.text == "" and "error" in r.headers.get("HX-Trigger", "")


def test_gap_origin_parse_answers_with_polling_banner(client, scalar):
    r = client.post(
        "/app/profile/facts/parse",
        data={"text": "Four years running AWS in production", "origin": "gap"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200
    # The gap dialog gets just the self-polling banner (swapped into
    # #gap-parse-host), not the profile facts section.
    assert "parse-banner" in r.text and "facts-section" not in r.text
    assert "/app/profile/facts/parses/" in r.text
    assert "close-dialog" in r.headers.get("HX-Trigger", "")
    # The background parse still lands the facts on the profile.
    assert scalar("SELECT status FROM fact_parses ORDER BY id DESC LIMIT 1") == "ready"
    groups = P.facts_grouped(1)
    assert any(f["name"] == "Spark" for f in groups["skill"])  # canned FactParse
    # Blank text: toast only, nothing recorded.
    r = client.post(
        "/app/profile/facts/parse", data={"text": " ", "origin": "gap"}, headers={"hx-request": "true"}
    )
    assert r.text == "" and "error" in r.headers.get("HX-Trigger", "")


def test_parse_endpoints_404_for_unknown_pid(client):
    bogus = uuid.uuid4()
    assert client.get(f"/app/profile/facts/parses/{bogus}").status_code == 404
    assert client.post(f"/app/profile/facts/parses/{bogus}/dismiss").status_code == 404
    assert client.get("/app/profile/facts/parses/not-a-uuid").status_code == 404


def test_user_name_set_first_last():
    assert U.get_user(1)["name"] == ""  # a fresh install has no name yet
    assert U.set_name("  Calvin  ", "  Schultz  ", user_id=1) is True
    u = U.get_user(1)
    assert u["first_name"] == "Calvin" and u["last_name"] == "Schultz"  # trimmed
    assert u["name"] == "Calvin Schultz"  # derived full name
    # last name is optional
    assert U.set_name("Sam", user_id=1) is True
    assert U.get_user(1)["name"] == "Sam" and U.get_user(1)["last_name"] == ""
    # blank first name is rejected (no-op)
    assert U.set_name("   ", user_id=1) is False
    assert U.get_user(1)["first_name"] == "Sam"


def test_name_route_saves(client, scalar):
    # The name form moved to the Account section (/app/account/name).
    client.post(
        "/app/account/name", data={"first_name": "Sam", "last_name": "Lee"}, headers={"hx-request": "true"}
    )
    assert scalar("SELECT first_name FROM users WHERE id = 1") == "Sam"
    assert scalar("SELECT name FROM users WHERE id = 1") == "Sam Lee"


def test_edit_fact_modal_form_is_prefilled(client):
    fid, _ = P.create_manual_fact(user_id=1, kind="role", name="Engineer", organization="Acme", start_date="2021")
    pid = public_id_of("profile_facts", fid)
    form = client.get(f"/app/profile/facts/{pid}/edit").text
    assert 'data-autoshow-dialog="edit-fact-dialog"' in form  # opens as a modal
    assert '<option value="role" selected>' in form  # type preselected
    assert 'value="Engineer"' in form and 'value="Acme"' in form and 'value="2021"' in form


def test_edit_fact_updates_kind_and_dates_and_closes_modal(client):
    fid, _ = P.create_manual_fact(user_id=1, kind="role", name="Engineer", start_date="2021", end_date="2024")
    pid = public_id_of("profile_facts", fid)
    r = client.post(
        f"/app/profile/facts/{pid}",
        data={"kind": "skill", "name": "Python", "organization": "", "detail": "",
              "proficiency": "expert", "start_date": "", "end_date": ""},
    )
    assert r.status_code == 200 and "close-dialog" in r.headers.get("HX-Trigger", "")
    row = P.fact_with_sources(fid, 1)
    assert row["kind"] == "skill" and row["name"] == "Python" and row["proficiency"] == "expert"
    assert row["start_date"] is None and row["end_date"] is None  # cleared on the switch to skill


def test_profile_page_renders(client):
    assert client.get("/app/profile").status_code == 200


# ── Career direction (wizard + direction facts) ──────────────────────────────


def test_direction_step_renders_question_and_prefill(client):
    P.save_direction_answer(1, "Career trajectory", "Toward staff engineering")
    page = client.get("/app/profile/direction/step/1").text
    assert "Where are you headed?" in page
    assert "Toward staff engineering" in page  # prefilled from the existing fact
    assert "Save &amp; next" in page


def test_direction_save_creates_fact_and_serves_next_step(client, scalar):
    r = client.post("/app/profile/direction/step/1", data={"answer": "Toward platform work"})
    assert r.status_code == 200
    assert P.DIRECTION_STEPS[1][1] in r.text  # next step's question served
    row_kind = scalar("SELECT kind FROM profile_facts WHERE name = 'Career trajectory'")
    assert row_kind == "direction"
    assert scalar("SELECT user_edited FROM profile_facts WHERE name = 'Career trajectory'") == 1
    assert scalar("SELECT document_id FROM profile_facts WHERE name = 'Career trajectory'") is None


def test_direction_answer_upserts_single_row(client, scalar):
    client.post("/app/profile/direction/step/1", data={"answer": "First answer"})
    client.post("/app/profile/direction/step/1", data={"answer": "Second answer"})
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE name = 'Career trajectory'") == 1
    assert scalar("SELECT detail FROM profile_facts WHERE name = 'Career trajectory'") == "Second answer"


def test_direction_blank_answer_is_noop_but_advances(client, scalar):
    r = client.post("/app/profile/direction/step/1", data={"answer": "   "})
    assert r.status_code == 200 and P.DIRECTION_STEPS[1][1] in r.text
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE kind = 'direction'") == 0


def test_direction_skip_via_get_creates_nothing(client, scalar):
    r = client.get("/app/profile/direction/step/2")
    assert r.status_code == 200
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE kind = 'direction'") == 0


def test_direction_last_step_closes_dialog_and_refreshes(client, scalar):
    last = len(P.DIRECTION_STEPS)
    r = client.post(f"/app/profile/direction/step/{last}", data={"answer": "Calm, remote-first team"})
    trigger = r.headers.get("HX-Trigger", "")
    assert "close-dialog" in trigger and "refresh-facts" in trigger and "toast" in trigger
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE kind = 'direction'") == 1


def test_direction_invalid_step_404(client):
    assert client.get("/app/profile/direction/step/0").status_code == 404
    assert client.get(f"/app/profile/direction/step/{len(P.DIRECTION_STEPS) + 1}").status_code == 404
    assert client.post("/app/profile/direction/step/99", data={"answer": "x"}).status_code == 404


def test_direction_manual_add_edit_delete(client, scalar):
    r = client.post(
        "/app/profile/facts",
        data={"kind": "direction", "name": "Compensation floor", "detail": "Base above 150k"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200
    fid = scalar("SELECT id FROM profile_facts WHERE name = 'Compensation floor'")
    pid = public_id_of("profile_facts", fid)
    client.post(f"/app/profile/facts/{pid}", data={
        "kind": "direction", "name": "Compensation floor", "organization": "", "detail": "Base above 160k",
        "proficiency": "", "start_date": "", "end_date": ""})
    assert scalar("SELECT detail FROM profile_facts WHERE id = ?", fid) == "Base above 160k"
    client.post(f"/app/profile/facts/{pid}/delete", headers={"hx-request": "true"})
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE id = ?", fid) == 0


def test_direction_excluded_from_evidence_block_and_gating():
    P.save_direction_answer(1, "Wants more of", "Greenfield architecture work")
    # Direction facts are opinions, not evidence: they never enter the profile
    # block and never unlock the evidence-based pipelines on their own.
    assert P.has_profile_facts(1) is False
    assert "Greenfield" not in P.profile_block_for_prompt(1)
    assert "Greenfield architecture work" in P.direction_block_for_prompt(1)
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    assert P.has_profile_facts(1) is True
    assert "direction" not in P.profile_block_for_prompt(1)


def test_direction_block_follows_step_order():
    P.save_direction_answer(1, "Work environment and style", "Small calm team")
    P.save_direction_answer(1, "Career trajectory", "Staff platform work")
    block = P.direction_block_for_prompt(1)
    assert block.index("Career trajectory") < block.index("Work environment and style")


def test_direction_card_states(client):
    page = client.get("/app/profile").text
    assert "Set your career direction" in page
    P.save_direction_answer(1, "Career trajectory", "Toward staff work")
    card = client.get("/app/profile/direction/card").text
    assert f"1 of {len(P.DIRECTION_STEPS)} answered" in card
    assert "Edit answers" in card
