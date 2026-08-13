"""Job intake, fit gating, sorting, and delete-cascade workflows."""

import io

from app.db import get_conn, public_id_of
from app.services import jobs as J
from app.services import profile as P
from app.text import normalize_posting


def test_intake_extracts_title_company_requirements(client, scalar):
    j = J.create_job("Senior Python engineer. Must have FastAPI and AWS.", user_id=1)
    J.run_intake(j)
    row = J.get_job_row(j, 1)
    assert row["title"] == "Senior Backend Engineer"  # from mock CANNED
    assert row["company"] == "Acme Corp"
    assert row["extract_status"] == "ready"
    assert scalar("SELECT COUNT(*) FROM job_requirements WHERE job_id = ?", j) > 0


def test_no_questions_generated_at_intake(scalar):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    assert scalar("SELECT COUNT(*) FROM questions WHERE job_id = ?", j) == 0


def test_fit_skipped_without_profile(scalar):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    assert scalar("SELECT analysis_status FROM jobs WHERE id = ?", j) == "none"
    assert scalar("SELECT COUNT(*) FROM fit_analyses WHERE job_id = ?", j) == 0


def test_fit_runs_with_profile(scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    assert scalar("SELECT analysis_status FROM jobs WHERE id = ?", j) == "ready"
    assert scalar("SELECT score FROM fit_analyses WHERE job_id = ?", j) is not None


def test_first_profile_fact_reanalyzes_existing_jobs(client, scalar):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    assert scalar("SELECT analysis_status FROM jobs WHERE id = ?", j) == "none"
    # Adding the first fact via the route triggers a background re-analysis sweep.
    client.post("/app/profile/facts", data={"kind": "skill", "name": "Python"}, headers={"hx-request": "true"})
    assert scalar("SELECT analysis_status FROM jobs WHERE id = ?", j) == "ready"


def test_delete_job_cascades(scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    assert J.delete_job(j, 1) is not None
    assert scalar("SELECT COUNT(*) FROM jobs WHERE id = ?", j) == 0
    assert scalar("SELECT COUNT(*) FROM job_requirements WHERE job_id = ?", j) == 0
    assert scalar("SELECT COUNT(*) FROM fit_analyses WHERE job_id = ?", j) == 0


def test_list_jobs_title_sort_is_case_insensitive():
    a = J.create_job("x", user_id=1)
    b = J.create_job("y", user_id=1)
    conn = get_conn()
    try:
        conn.execute("UPDATE jobs SET title = ? WHERE id = ?", ("Beta", a))
        conn.execute("UPDATE jobs SET title = ? WHERE id = ?", ("alpha", b))
        conn.commit()
    finally:
        conn.close()
    titles = [r["title"] for r in J.list_jobs(sort="title", direction="asc", user_id=1)]
    assert titles == ["alpha", "Beta"]  # LOWER() ordering, not ASCII (Beta<alpha)


def test_all_columns_sortable():
    a, b, d = J.create_job("Alpha", user_id=1), J.create_job("Beta", user_id=1), J.create_job("Gamma", user_id=1)
    conn = get_conn()
    try:
        conn.execute("UPDATE jobs SET title='Alpha', status='offer', interest_level=5 WHERE id=?", (a,))
        conn.execute("UPDATE jobs SET title='Beta', status='researching', interest_level=2 WHERE id=?", (b,))
        conn.execute("UPDATE jobs SET title='Gamma', status='applied' WHERE id=?", (d,))  # interest null
        conn.execute(
            "INSERT INTO fit_analyses (job_id,version,score,band,strengths_json,gaps_json,study_areas_json)"
            " VALUES (?,1,90,'Strong','[]','[]','[]')", (b,))
        conn.execute(
            "INSERT INTO fit_analyses (job_id,version,score,band,strengths_json,gaps_json,study_areas_json)"
            " VALUES (?,1,40,'Stretch','[]','[]','[]')", (a,))
        conn.commit()
    finally:
        conn.close()

    def titles(sort, direction):
        return [r["title"] for r in J.list_jobs(sort=sort, direction=direction, user_id=1)]

    assert titles("status", "asc") == ["Beta", "Gamma", "Alpha"]      # pipeline: researching<applied<offer
    assert titles("interest", "desc") == ["Alpha", "Beta", "Gamma"]   # 5,2,null (nulls last)
    assert titles("fit", "desc") == ["Beta", "Alpha", "Gamma"]        # 90,40,null (nulls last)


def test_unknown_sort_falls_back(client):
    J.create_job("Backend role", user_id=1)
    assert client.get("/app/jobs?sort=raw_posting").status_code == 200  # not whitelisted -> default


def test_jobs_page_renders(client):
    J.create_job("Backend role", user_id=1)
    assert client.get("/app/jobs").status_code == 200


def test_fit_tab_gaps_offer_add_to_profile(client):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)  # mock fit analysis has gaps (aws, kubernetes)
    r = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/fit")
    assert r.status_code == 200
    # Each gap carries a pre-fill opener for the add-fact dialog…
    assert 'data-fact-name="aws"' in r.text and 'data-fact-name="kubernetes"' in r.text
    assert 'data-open-dialog="add-fact-dialog"' in r.text
    # …and the tab ships the dialog itself plus the parse-banner host.
    assert 'id="add-fact-dialog"' in r.text
    assert 'id="gap-parse-host"' in r.text
    assert 'name="origin" value="gap"' in r.text
    # The gap dialog carries the job + original requirement so the router can
    # check the gap off, and the card listens for the resulting event.
    assert 'name="job_pid"' in r.text and 'name="gap_requirement"' in r.text
    assert 'hx-trigger="gap-resolved from:body"' in r.text


def _gap_job():
    """A job with a completed mock fit analysis (gaps: aws, kubernetes)."""
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    return j


def _add_gap_fact(client, j, name="aws", gap_requirement="aws", job_pid=None):
    """POST the gap dialog's manual form (the 'I have this' path)."""
    return client.post(
        "/app/profile/facts",
        data={
            "kind": "skill", "name": name, "origin": "gap",
            "job_pid": job_pid if job_pid is not None else public_id_of("jobs", j),
            "gap_requirement": gap_requirement,
        },
        headers={"hx-request": "true"},
    )


def test_gap_add_records_resolution_and_checks_gap_off(client, scalar):
    j = _gap_job()
    r = _add_gap_fact(client, j)
    assert r.status_code == 200 and r.text == ""
    trig = r.headers.get("HX-Trigger", "")
    assert "gap-resolved" in trig and "close-dialog" in trig
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions WHERE requirement_key = 'aws'") == 1
    tab = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/fit").text
    assert "Added to profile" in tab and "gap-resolved" in tab
    assert 'data-fact-name="aws"' not in tab        # resolved gap loses its button…
    assert 'data-fact-name="kubernetes"' in tab     # …the other gap keeps offering it


def test_gap_resolution_idempotent(client, scalar):
    j = _gap_job()
    _add_gap_fact(client, j)
    r = _add_gap_fact(client, j)  # double-add merges the fact, keeps one row
    assert "gap-resolved" in r.headers.get("HX-Trigger", "")  # UI still refreshes
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions") == 1


def test_fresh_analysis_version_starts_clean(client, scalar):
    from app.services import analysis as A

    j = _gap_job()
    _add_gap_fact(client, j)
    A.run_fit_analysis(j)  # the mock re-lists aws — that's real signal, not suppressed
    tab = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/fit").text
    assert 'data-fact-name="aws"' in tab and "Added to profile" not in tab
    new_fit = scalar("SELECT id FROM fit_analyses WHERE job_id = ? ORDER BY version DESC LIMIT 1", j)
    assert A.resolved_gap_keys(new_fit, user_id=1) == set()


def test_gap_resolution_requires_real_gap(client, scalar):
    j = _gap_job()
    # 'python' isn't a gap of this analysis — the fact lands, nothing resolves.
    r = _add_gap_fact(client, j, name="python", gap_requirement="python")
    assert "gap-resolved" not in r.headers.get("HX-Trigger", "")
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions") == 0
    # The visible name is editable; the hidden original requirement still keys it.
    r = _add_gap_fact(client, j, name="Amazon Web Services", gap_requirement="aws")
    assert "gap-resolved" in r.headers.get("HX-Trigger", "")
    assert scalar("SELECT requirement_key FROM fit_gap_resolutions") == "aws"


def test_gap_resolution_forged_job_pid_noops(client, scalar):
    j = _gap_job()
    pid = public_id_of("jobs", j)
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (id, name) VALUES (999, 'other') ON CONFLICT DO NOTHING")
        conn.execute("UPDATE jobs SET user_id = 999 WHERE id = ?", (j,))
        conn.commit()
    finally:
        conn.close()
    # The fact itself still lands (the primary action) — only the resolution no-ops.
    r = _add_gap_fact(client, j, job_pid=pid)
    assert r.status_code == 200
    assert "gap-resolved" not in r.headers.get("HX-Trigger", "")
    assert scalar("SELECT COUNT(*) FROM profile_facts WHERE name = 'aws' AND user_id = 1") == 1
    for bogus in ("not-a-uuid", "00000000-0000-4000-8000-000000000000"):
        r = _add_gap_fact(client, j, job_pid=bogus)
        assert r.status_code == 200 and "gap-resolved" not in r.headers.get("HX-Trigger", "")
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions") == 0


def test_job_delete_cascades_gap_resolutions(client, scalar):
    j = _gap_job()
    _add_gap_fact(client, j)
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions") == 1
    J.delete_job(j, 1)
    assert scalar("SELECT COUNT(*) FROM fit_gap_resolutions") == 0


def test_normalize_posting():
    raw = "\r\n  Senior Engineer  \r\n\r\n\r\n\r\nMust have Python.  \n\n"
    assert normalize_posting(raw) == "Senior Engineer\n\nMust have Python."


def test_pasted_posting_stored_normalized_viewable_downloadable(client, scalar):
    client.post(
        "/app/jobs", data={"posting_text": "Senior Engineer\n\n\n\nMust have Python.  "},
        follow_redirects=True,
    )
    j = scalar("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
    assert scalar("SELECT raw_posting FROM jobs WHERE id = ?", j) == "Senior Engineer\n\nMust have Python."
    jp = public_id_of("jobs", j)
    overview = client.get(f"/app/jobs/{jp}?tab=overview").text
    assert "posting-text" in overview and "Senior Engineer" in overview and "pasted text" in overview
    dl = client.get(f"/app/jobs/{jp}/posting/download")
    assert dl.status_code == 200 and "posting.txt" in dl.headers["content-disposition"]
    assert "Senior Engineer" in dl.text


def test_uploaded_file_is_redownloadable(client, scalar):
    client.post(
        "/app/jobs",
        files={"posting_file": ("role.txt", io.BytesIO(b"Backend engineer. FastAPI, AWS."), "text/plain")},
        follow_redirects=True,
    )
    j = scalar("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
    jp = public_id_of("jobs", j)
    assert "role.txt" in client.get(f"/app/jobs/{jp}?tab=overview").text  # filename shown
    dl = client.get(f"/app/jobs/{jp}/posting/download")
    assert dl.status_code == 200
    assert "role.txt" in dl.headers["content-disposition"]
    assert dl.content == b"Backend engineer. FastAPI, AWS."  # original bytes returned


def test_list_jobs_limit_caps_rows():
    for i in range(4):
        J.create_job(f"posting {i}", user_id=1)
    assert len(J.list_jobs(user_id=1, limit=2)) == 2
    assert len(J.list_jobs(user_id=1)) == 4


def test_dashboard_recent_jobs_sorts_like_the_jobs_page(client):
    a = J.create_job("x", user_id=1)
    b = J.create_job("y", user_id=1)
    conn = get_conn()
    try:
        conn.execute("UPDATE jobs SET title = 'Beta' WHERE id = ?", (a,))
        conn.execute("UPDATE jobs SET title = 'Alpha' WHERE id = ?", (b,))
        conn.commit()
    finally:
        conn.close()
    page = client.get("/app?sort=title&dir=asc").text
    assert page.index("Alpha") < page.index("Beta")
    page = client.get("/app?sort=title&dir=desc").text
    assert page.index("Beta") < page.index("Alpha")
    # unknown sort falls back to the default (newest first) instead of erroring
    assert client.get("/app?sort=raw_posting").status_code == 200
    # header links carry the /app base and flip direction on the active column
    page = client.get("/app?sort=title&dir=asc").text
    assert "/app?sort=title&dir=desc" in page


# ── "Fit for you" alignment (direction facts → separate verdict) ─────────────


def test_alignment_stored_when_direction_facts_present(client, scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    P.save_direction_answer(1, "Career trajectory", "Toward platform work")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    alignment = scalar("SELECT alignment_json FROM fit_analyses WHERE job_id = ?", j)
    assert alignment is not None and '"mixed"' in alignment  # canned verdict
    pid = public_id_of("jobs", j)
    tab = client.get(f"/app/jobs/{pid}/tab/fit").text
    assert "Fit for you" in tab and "mixed" in tab
    assert "Moves you forward" in tab and "Works against your direction" in tab


def test_alignment_null_without_direction_facts(client, scalar):
    # The mock always returns a canned alignment; the service-side gate must
    # discard it when no direction block fed the prompt.
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    assert scalar("SELECT alignment_json FROM fit_analyses WHERE job_id = ?", j) is None
    tab = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/fit").text
    assert "Fit for you" not in tab


def test_alignment_hint_when_direction_set_after_analysis(client):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    P.save_direction_answer(1, "Career trajectory", "Toward platform work")
    tab = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/fit").text
    assert "re-analyze to see how this role fits" in tab
