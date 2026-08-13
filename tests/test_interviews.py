"""Interview session lifecycle: per-session generation, grading, assessment, prune."""

from app.db import get_conn, public_id_of
from app.services import interviews as I
from app.services import jobs as J
from app.services import profile as P


def _ready_job(title="Backend role"):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job(title, user_id=1)
    J.run_intake(j)
    return j


def test_session_builds_questions_at_start(scalar):
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "generating"
    I.build_session(sid, 5)
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "ready"
    assert scalar("SELECT COUNT(*) FROM session_answers WHERE session_id = ?", sid) > 0


def test_full_interview_produces_assessment(scalar):
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    I.build_session(sid, 5)
    while True:
        _, answers, _ = I.get_session(sid, 1)
        current = I.current_answer_row(answers)
        if current is None:
            break
        aid = I.submit_answer(sid, "I use FastAPI dependency injection and pytest.")
        I.grade_answer(aid)
    I.finish_session(sid)
    I.run_assessment(sid)
    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "completed"
    assert scalar("SELECT COUNT(*) FROM assessments WHERE session_id = ?", sid) == 1


def test_mixer_draws_from_multiple_jobs(scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    a = J.create_job("Backend role", user_id=1); J.run_intake(a)
    b = J.create_job("Data role", user_id=1); J.run_intake(b)
    sid = I.create_session("mixer", [a, b], 6, user_id=1)
    I.build_session(sid, 6)
    distinct_jobs = scalar(
        """SELECT COUNT(DISTINCT q.job_id) FROM session_answers sa
           JOIN questions q ON q.id = sa.question_id WHERE sa.session_id = ?""",
        sid,
    )
    assert distinct_jobs == 2


def test_finish_with_no_answers_deletes_session(client, scalar):
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    I.build_session(sid, 5)
    client.post(f"/app/interviews/{public_id_of('interview_sessions', sid)}/finish", follow_redirects=False)
    assert scalar("SELECT COUNT(*) FROM interview_sessions WHERE id = ?", sid) == 0


def test_prune_skips_generating_sessions(scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    a = J.create_job("A", user_id=1); J.run_intake(a)
    b = J.create_job("B", user_id=1); J.run_intake(b)
    sid = I.create_session("mixer", [a, b], 6, user_id=1)  # 'generating', no answers yet
    assert I.prune_empty_sessions(1) == 0
    assert scalar("SELECT COUNT(*) FROM interview_sessions WHERE id = ?", sid) == 1


def test_build_session_survives_deleted_questions(scalar, monkeypatch):
    """If a job is removed mid-build its questions vanish; build_session must not
    500 — it marks the session errored (the FK-resilience path)."""
    j = _ready_job()
    from app.services import questions as Q

    qids = Q.generate_for_session(j, 3, user_id=1)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM questions WHERE id IN (%s)" % ",".join(str(q) for q in qids))
        conn.commit()
    finally:
        conn.close()
    sid = I.create_session("job", [j], 3, user_id=1)
    monkeypatch.setattr(I.questions, "generate_for_session", lambda jid, n: qids)
    I.build_session(sid, 3)  # must not raise
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "error"


def test_interviews_page_renders(client):
    assert client.get("/app/interviews").status_code == 200


# ── Impromptu study drills (single-question practice from a study topic) ──


def test_study_drill_builds_single_question(scalar):
    j = _ready_job()
    sid = I.create_study_drill(j, "Security & Cost Governance", 1)
    assert sid is not None
    assert scalar("SELECT scope FROM interview_sessions WHERE id = ?", sid) == "study"
    assert scalar("SELECT label FROM interview_sessions WHERE id = ?", sid) == "Security & Cost Governance"
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "generating"
    I.build_study_drill(sid, j, "Security & Cost Governance", "why it matters", "how tested")
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "ready"
    assert scalar("SELECT COUNT(*) FROM session_answers WHERE session_id = ?", sid) == 1


def test_session_pages_breadcrumb_back_into_app(client, scalar):
    """The practice/interview breadcrumb must point at /app/study | /app/interviews
    — the bare pre-move paths 404 since the /app cutover."""
    from app.db import public_id_of

    j = _ready_job()
    sid = I.create_study_drill(j, "Topic", 1)
    I.build_study_drill(sid, j, "Topic", "why", "how")
    drill = client.get(f"/app/study/practice/{public_id_of('interview_sessions', sid)}")
    assert drill.status_code == 200
    assert 'href="/app/study"' in drill.text and 'href="/study"' not in drill.text

    iid = I.create_session("job", [j], 3, user_id=1)
    I.build_session(iid, 3)
    page = client.get(f"/app/interviews/{public_id_of('interview_sessions', iid)}")
    assert page.status_code == 200
    assert 'href="/app/interviews"' in page.text and 'href="/interviews"' not in page.text


def test_study_drill_skips_assessment(scalar):
    """A finished study drill is completed but runs no session assessment."""
    j = _ready_job()
    sid = I.create_study_drill(j, "Topic", 1)
    I.build_study_drill(sid, j, "Topic", "why", "how")
    aid = I.submit_answer(sid, "I redact PII before egress and cap tokens per key.")
    I.grade_answer(aid)
    I.finish_session(sid, assess=False)
    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "completed"
    assert scalar("SELECT assessment_status FROM interview_sessions WHERE id = ?", sid) == "none"
    assert scalar("SELECT COUNT(*) FROM assessments WHERE session_id = ?", sid) == 0


def test_study_drill_route_launches_into_study_section(client, scalar):
    from app.db import resolve_id
    from app.services import study as S

    j = _ready_job()
    S.generate_guide(j)  # a guide with topics must exist for the drill launch
    assert S.latest_guide(j) is not None

    r = client.post(
        f"/app/jobs/{public_id_of('jobs', j)}/study/drill",
        data={"topic_index": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/app/study/practice/")  # drills live in the Study section
    sess_pid = loc.rsplit("/", 1)[1]
    sid = resolve_id("interview_sessions", sess_pid)
    assert scalar("SELECT scope FROM interview_sessions WHERE id = ?", sid) == "study"
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "ready"

    # The practice page renders the drill inside the Study section.
    page = client.get(loc)
    assert page.status_code == 200
    assert "← Study" in page.text and "Question 1 of 1" in page.text

    # A study session hit under /interviews bounces to its Study home.
    bounce = client.get(f"/app/interviews/{sess_pid}", follow_redirects=False)
    assert bounce.status_code == 303
    assert bounce.headers["location"] == f"/app/study/practice/{sess_pid}"


def test_study_drill_autofinishes_with_drill_actions(client, scalar):
    """Grading a drill's single answer completes it; the feedback view offers
    'Practice another' / 'Done' — not the interview finish controls."""
    from app.db import public_id_of, resolve_id
    from app.services import study as S

    j = _ready_job()
    S.generate_guide(j)
    r = client.post(
        f"/app/jobs/{public_id_of('jobs', j)}/study/drill",
        data={"topic_index": "0"}, follow_redirects=False,
    )
    sess_pid = r.headers["location"].rsplit("/", 1)[1]
    sid = resolve_id("interview_sessions", sess_pid)
    client.post(f"/app/interviews/{sess_pid}/answer", data={"answer_text": "Redact PII; cap tokens."})

    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "completed"
    assert scalar("SELECT assessment_status FROM interview_sessions WHERE id = ?", sid) == "none"

    apid = scalar("SELECT public_id FROM session_answers WHERE session_id = ?", sid)
    fb = client.get(f"/app/interviews/{sess_pid}/feedback/{apid}")
    assert "Practice another" in fb.text and 'href="/app/study"' in fb.text
    assert "Finish session" not in fb.text
    assert "Finish early" not in client.get(f"/app/study/practice/{sess_pid}").text


def test_practice_again_creates_fresh_drill_same_topic(client, scalar):
    from app.db import public_id_of, resolve_id
    from app.services import study as S

    j = _ready_job()
    S.generate_guide(j)
    r = client.post(
        f"/app/jobs/{public_id_of('jobs', j)}/study/drill",
        data={"topic_index": "0"}, follow_redirects=False,
    )
    first_pid = r.headers["location"].rsplit("/", 1)[1]
    first_id = resolve_id("interview_sessions", first_pid)
    client.post(f"/app/interviews/{first_pid}/answer", data={"answer_text": "x"})

    again = client.post(f"/app/study/practice/{first_pid}/again", follow_redirects=False)
    assert again.status_code == 303
    loc = again.headers["location"]
    assert loc.startswith("/app/study/practice/")
    new_id = resolve_id("interview_sessions", loc.rsplit("/", 1)[1])
    assert new_id != first_id
    assert scalar("SELECT scope FROM interview_sessions WHERE id = ?", new_id) == "study"
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", new_id) == "ready"
    assert scalar("SELECT label FROM interview_sessions WHERE id = ?", new_id) == scalar(
        "SELECT label FROM interview_sessions WHERE id = ?", first_id
    )


def test_global_focus_plan_drill_is_jobless(client, scalar):
    """A Global-focus-plan drill runs with no job (job_id NULL) and still grades."""
    from app.db import resolve_id
    from app.services import study as S

    _ready_job()  # a job must exist so the global guide has something to synthesize
    S.generate_global_guide(1)
    assert S.latest_global_guide(1) is not None

    r = client.post("/app/study/drill", data={"topic_index": "0"}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/app/study/practice/")
    sid = resolve_id("interview_sessions", loc.rsplit("/", 1)[1])
    assert scalar("SELECT scope FROM interview_sessions WHERE id = ?", sid) == "study"
    assert scalar("SELECT job_id FROM interview_sessions WHERE id = ?", sid) is None
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", sid) == "ready"

    # Answering it grades and auto-completes, just like a per-job drill.
    sess_pid = loc.rsplit("/", 1)[1]
    client.post(f"/app/interviews/{sess_pid}/answer", data={"answer_text": "A general answer."})
    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "completed"

    # 'Practice another' works without a job too.
    again = client.post(f"/app/study/practice/{sess_pid}/again", follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"].startswith("/app/study/practice/")

    # The global guide renders a Practice this button.
    assert "Practice this" in client.get("/app/study").text


def test_study_hub_lists_drills_and_per_job_guide(client):
    from app.services import study as S

    j = _ready_job()
    S.generate_guide(j)
    client.post(f"/app/jobs/{public_id_of('jobs', j)}/study/drill", data={"topic_index": "0"})

    # Guides view: the switcher offers the job; a Drills tab links across.
    hub = client.get("/app/study")
    assert hub.status_code == 200
    assert public_id_of("jobs", j) in hub.text  # job appears in the switcher
    assert "view=drills" in hub.text            # Drills tab present

    # Drills view: the drill we ran is listed in its own tab.
    drills = client.get("/app/study?view=drills")
    assert drills.status_code == 200
    assert "<table" in drills.text

    # Per-job view renders that job's guide with a Practice this button.
    per_job = client.get(f"/app/study?job={public_id_of('jobs', j)}")
    assert per_job.status_code == 200
    assert "Practice this" in per_job.text


# ── Interactive follow-up on weak answers ──


def test_weak_answer_triggers_followup_and_gates_drill_finish(scalar, monkeypatch):
    """A weak answer (score <= 3) earns one probing follow-up; a study drill stays
    active until that follow-up is answered and graded, then completes."""
    from app.llm import mock_provider

    monkeypatch.setitem(mock_provider.CANNED["AnswerGrade"], "score", 2)
    j = _ready_job()
    sid = I.create_study_drill(j, "Topic", 1)
    I.build_study_drill(sid, j, "Topic", "why", "how")
    aid = I.submit_answer(sid, "I would look at the expensive queries.")
    I.grade_answer(aid)

    assert scalar("SELECT followup_status FROM session_answers WHERE id = ?", aid) == "awaiting"
    assert scalar("SELECT followup_question FROM session_answers WHERE id = ?", aid)
    # Drill must not complete while a follow-up is pending.
    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "active"

    assert I.submit_followup(aid, "sys.dm_exec_query_stats; a Missing Index warning.")
    I.grade_followup(aid)
    assert scalar("SELECT followup_status FROM session_answers WHERE id = ?", aid) == "ready"
    assert scalar("SELECT followup_score FROM session_answers WHERE id = ?", aid) == 2
    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "completed"


def test_strong_answer_skips_followup(scalar):
    """A strong answer (mock scores 4) gets no follow-up and the drill finishes."""
    j = _ready_job()
    sid = I.create_study_drill(j, "Topic", 1)
    I.build_study_drill(sid, j, "Topic", "why", "how")
    aid = I.submit_answer(sid, "A thorough, specific answer with named tools and metrics.")
    I.grade_answer(aid)
    assert scalar("SELECT followup_status FROM session_answers WHERE id = ?", aid) == "none"
    assert scalar("SELECT status FROM interview_sessions WHERE id = ?", sid) == "completed"


def test_followup_flow_via_routes(client, scalar, monkeypatch):
    """End-to-end over HTTP: a weak answer surfaces a follow-up form; submitting it
    grades the follow-up and the feedback view shows its score."""
    from app.llm import mock_provider

    monkeypatch.setitem(mock_provider.CANNED["AnswerGrade"], "score", 2)
    j = _ready_job()
    sid = I.create_session("job", [j], 3, user_id=1)
    I.build_session(sid, 3)
    spid = public_id_of("interview_sessions", sid)

    # Answer the first question — background grading runs synchronously in TestClient.
    client.post(f"/app/interviews/{spid}/answer", data={"answer_text": "expensive queries"})
    apid = scalar(
        "SELECT public_id FROM session_answers WHERE session_id = ? ORDER BY position LIMIT 1", sid
    )
    view = client.get(f"/app/interviews/{spid}/feedback/{apid}")
    assert 'name="followup_text"' in view.text and "Interviewer followed up" in view.text

    client.post(
        f"/app/interviews/{spid}/feedback/{apid}/followup",
        data={"followup_text": "sys.dm_exec_query_stats joined with sys.dm_exec_sql_text."},
    )
    assert scalar("SELECT followup_status FROM session_answers WHERE public_id = ?", apid) == "ready"
    done = client.get(f"/app/interviews/{spid}/feedback/{apid}")
    assert "score-chip s2" in done.text
    assert "Next question" in done.text or "Finish session" in done.text


def test_study_drill_route_bad_topic_index_redirects_to_study_tab(client):
    j = _ready_job()
    from app.services import study as S

    S.generate_guide(j)
    r = client.post(
        f"/app/jobs/{public_id_of('jobs', j)}/study/drill",
        data={"topic_index": "999"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("?tab=study")


def test_session_history_sortable():
    a = I.create_study_drill(None, "Beta", user_id=1)
    I.create_study_drill(None, "alpha", user_id=1)
    I.create_study_drill(None, "Gamma", user_id=1)
    # Grade one drill so it carries a score; the others stay ungraded.
    I.build_study_drill(a, None, "Beta", "", "")
    aid = I.submit_answer(a, "I use rolling deploys and health checks.")
    I.grade_answer(aid)

    def labels(sort, direction):
        return [s["label"] for s in I.list_sessions(user_id=1, sort=sort, direction=direction)]

    assert labels("topic", "asc") == ["alpha", "Beta", "Gamma"]  # case-insensitive
    assert labels("topic", "desc") == ["Gamma", "Beta", "alpha"]
    # Ungraded drills sort last in BOTH score directions (NULLS LAST).
    assert labels("score", "desc")[0] == "Beta"
    assert labels("score", "asc")[0] == "Beta"
    assert labels("progress", "desc")[0] == "Beta"  # the only answered drill
    # Unknown keys fall back to the default (started desc) instead of erroring.
    assert len(labels("raw_posting", "asc")) == 3


def test_drills_view_headers_sort(client):
    I.create_study_drill(None, "ZZZ-topic", user_id=1)
    I.create_study_drill(None, "AAA-topic", user_id=1)
    page = client.get("/app/study?view=drills&sort=topic&dir=asc").text
    assert page.index("AAA-topic") < page.index("ZZZ-topic")
    # The active header's link keeps the drills view and flips direction.
    assert "/app/study?view=drills&sort=topic&dir=desc" in page
    page = client.get("/app/study?view=drills&sort=topic&dir=desc").text
    assert page.index("ZZZ-topic") < page.index("AAA-topic")
    assert client.get("/app/study?view=drills&sort=bogus").status_code == 200


def test_history_rows_link_under_the_app_prefix(client):
    """The /app cutover regression: history-row links must carry the prefix or
    they 404 (drills linked /study/practice/…, interviews /interviews/…)."""
    I.create_study_drill(None, "AAA-topic", user_id=1)
    page = client.get("/app/study?view=drills").text
    assert 'href="/app/study/practice/' in page
    I.create_session("job", [_ready_job()], 5, user_id=1)
    page = client.get("/app/interviews").text
    assert 'href="/app/interviews/' in page


# ── "Tell me about yourself" opener (job-scoped sessions) ────────────────────


def test_job_session_opener_served_first_and_count_kept(scalar, monkeypatch):
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    # The mock provider ignores the requested count (fixed canned bank), so the
    # "opener replaces one generated question" property is asserted on the
    # count passed down to generation rather than on the slot total.
    requested = {}
    real = I.questions.generate_for_session

    def spy(job_id, n, *, user_id):
        requested["n"] = n
        return real(job_id, n, user_id=user_id)

    monkeypatch.setattr(I.questions, "generate_for_session", spy)
    I.build_session(sid, 5, include_opener=True)
    assert requested["n"] == 4  # one slot reserved for the opener
    first_q = scalar(
        """SELECT q.skill FROM session_answers sa JOIN questions q ON q.id = sa.question_id
           WHERE sa.session_id = ? ORDER BY sa.position LIMIT 1""",
        sid,
    )
    assert first_q == "introduction"
    text = scalar(
        "SELECT text FROM questions WHERE job_id = ? AND skill = 'introduction'", j
    )
    assert "tell me about yourself" in text


def test_opener_criteria_name_the_jobs_must_haves(scalar):
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    I.build_session(sid, 5, include_opener=True)
    criteria = scalar(
        "SELECT ideal_answer_criteria FROM questions WHERE job_id = ? AND skill = 'introduction'", j
    )
    musts = [
        r["skill_display"]
        for r in _rows("SELECT skill_display FROM job_requirements WHERE job_id = ? AND kind = 'must'", j)
    ]
    assert musts and all(m in criteria for m in musts)


def test_opener_absent_when_skipped_or_not_job_scope(scalar):
    j1, j2 = _ready_job(), _ready_job("Data role")
    sid = I.create_session("job", [j1], 5, user_id=1)
    I.build_session(sid, 5, include_opener=False)
    assert _opener_count(scalar, sid) == 0
    mixer = I.create_session("mixer", [j1, j2], 6, user_id=1)
    I.build_session(mixer, 6, include_opener=True)  # scope guard wins over the flag
    assert _opener_count(scalar, mixer) == 0


def test_opener_grades_like_a_normal_question(scalar):
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    I.build_session(sid, 5, include_opener=True)
    aid = I.submit_answer(sid, "I'm a backend engineer with eight years of Python.")
    I.grade_answer(aid)
    assert scalar("SELECT grade_status FROM session_answers WHERE id = ?", aid) == "ready"
    assert scalar("SELECT score FROM session_answers WHERE id = ?", aid) is not None


def test_start_route_defaults_opener_on_and_checkbox_skips(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post("/app/interviews", data={"scope": "job", "job_id": pid, "count": "5"})
    with_opener = scalar("SELECT id FROM interview_sessions ORDER BY id DESC LIMIT 1")
    assert _opener_count(scalar, with_opener) == 1
    client.post("/app/interviews", data={"scope": "job", "job_id": pid, "count": "5", "skip_opener": "1"})
    without = scalar("SELECT id FROM interview_sessions ORDER BY id DESC LIMIT 1")
    assert without != with_opener
    assert _opener_count(scalar, without) == 0


def _opener_count(scalar, session_id):
    return scalar(
        """SELECT COUNT(*) FROM session_answers sa JOIN questions q ON q.id = sa.question_id
           WHERE sa.session_id = ? AND q.skill = 'introduction'""",
        session_id,
    )


def _rows(sql, *params):
    conn = get_conn()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── Header progress badge stays in sync with the HTMX question flow ──


def test_header_progress_refreshes_out_of_band(client, scalar):
    """The 'X/Y answered' badge lives outside #question-area, so the answer →
    feedback → next-question swaps must re-emit it via hx-swap-oob — without
    that it freezes at its page-load value (the '1/10 answered' vs
    'Question 5 of 10' bug)."""
    j = _ready_job()
    sid = I.create_session("job", [j], 5, user_id=1)
    I.build_session(sid, 5)
    spid = public_id_of("interview_sessions", sid)
    total = scalar("SELECT COUNT(*) FROM session_answers WHERE session_id = ?", sid)

    # Full page: exactly one header badge, rendered inline (no oob attribute).
    page = client.get(f"/app/interviews/{spid}").text
    assert page.count('id="session-progress"') == 1
    assert "hx-swap-oob" not in page
    assert f"0/{total} answered" in page

    # Submitting an answer re-emits the badge out-of-band with the new count.
    r = client.post(f"/app/interviews/{spid}/answer", data={"answer_text": "I use pytest."})
    assert 'id="session-progress"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert f"1/{total} answered" in r.text

    # The next-question card refreshes it too, and stays consistent with the
    # position it shows ("Question 2 of N" alongside 1/N answered).
    r = client.post(f"/app/interviews/{spid}/next")
    assert 'hx-swap-oob="true"' in r.text
    assert f"1/{total} answered" in r.text
    assert f"Question 2 of {total}" in r.text
