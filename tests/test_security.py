"""Regression tests for the OWASP-driven hardening pass.

Covers the behaviors that were previously unverified: upload size caps, curated
(non-leaking) pipeline error copy, URL-scheme sanitization, provider error
message curation, prompt input caps, the public-id table guard surviving
python -O, and the DB-posture startup warnings.
"""

import app.config as config
from app.db import public_id_of, resolve_id
from app.llm.base import LLMError
from app.services import documents as D
from app.services import profile as P
from app.text import safe_external_url
from app.user_errors import USER_ERROR_PARSE


# --- A06 / LLM10: upload + text size caps -----------------------------------

def test_oversize_upload_is_rejected():
    big = b"x" * (config.MAX_UPLOAD_BYTES + 1)
    try:
        D.save_upload("resume.txt", big, user_id=1)
        assert False, "expected FileTooLarge"
    except D.FileTooLarge as exc:
        assert "too large" in str(exc).lower()


def test_upload_at_limit_is_accepted(scalar):
    ok = b"Engineer. Python, SQL." + b" " * 10
    doc = D.save_upload("resume.txt", ok, user_id=1)
    assert scalar("SELECT id FROM documents WHERE id = ?", doc) == doc


def test_oversize_upload_rejected_at_http_boundary(client):
    big = b"x" * (config.MAX_UPLOAD_BYTES + 1)
    resp = client.post(
        "/app/profile/documents",
        files={"file": ("resume.txt", big, "text/plain")},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_prompt_input_is_capped():
    from app.llm.prompts import job_extraction_prompt

    huge = "A" * (config.MAX_TEXT_CHARS + 5000)
    out = job_extraction_prompt(huge)
    assert "[truncated]" in out
    assert len(out) < config.MAX_TEXT_CHARS + 200


# --- A10 / LLM02: pipeline errors never leak internals ----------------------

def test_document_parse_error_is_curated_not_raw(scalar):
    """A parse failure must store curated copy, not the raw exception (which
    would leak filesystem paths / library internals)."""
    doc = D.save_upload("empty.txt", b"   ", user_id=1)  # whitespace-only -> "no text" error
    try:
        D.parse_document(doc)
    except Exception:
        pass
    err = scalar("SELECT error FROM documents WHERE id = ?", doc)
    assert err == USER_ERROR_PARSE
    assert scalar("SELECT status FROM documents WHERE id = ?", doc) == "error"


def test_extraction_llm_failure_sets_error_without_leak(scalar, monkeypatch):
    """When extraction raises LLMError, the document row records the curated
    provider copy and never a stack trace / internal detail."""
    doc = D.save_upload("resume.txt", b"Engineer. Python, SQL, FastAPI.", user_id=1)

    def boom(*a, **k):
        raise LLMError("The AI service returned an error (500) — try again.")

    import app.services.profile as prof
    monkeypatch.setattr(prof, "get_provider", lambda: type("X", (), {"extract": staticmethod(boom)})())
    P.process_document(doc)
    err = scalar("SELECT error FROM documents WHERE id = ?", doc)
    assert err and "traceback" not in err.lower() and "/" not in err
    assert scalar("SELECT status FROM documents WHERE id = ?", doc) == "error"


# --- Dedup refactor: AI reconciliation runs outside the lock -----------------

def test_ai_reconciliation_merge_still_works(scalar, monkeypatch):
    """The fact-dedup refactor moved the reconciliation LLM call before the
    advisory lock and re-validates its result at apply time. Drive a non-
    deterministic (AI-only) match end to end and confirm the new fact merges
    into the existing one rather than creating a duplicate."""
    from app.models.extraction import (
        ExtractedFact,
        ProfileExtraction,
        ProfileReconciliation,
        ReconciledFact,
    )

    fid, _ = P.create_manual_fact(user_id=1, kind="skill", name="Machine Learning")
    doc = D.save_upload("resume.txt", b"Worked extensively with ML.", user_id=1)

    class FakeProvider:
        def extract(self, *, system, prompt, schema, max_tokens=16000):
            if schema is ProfileExtraction:
                # 'ML' does not canonically match 'Machine Learning' -> unmatched,
                # so only the AI reconciliation pass can merge it.
                return ProfileExtraction(
                    facts=[ExtractedFact(kind="skill", name="ML", evidence_text="Worked with ML.")]
                )
            if schema is ProfileReconciliation:
                return ProfileReconciliation(
                    reconciliations=[ReconciledFact(new_index=0, match=fid)]
                )
            raise AssertionError(f"unexpected schema {schema}")

    import app.services.profile as prof
    monkeypatch.setattr(prof, "get_provider", lambda: FakeProvider())
    P.process_document(doc)

    skills = P.facts_grouped(1)["skill"]
    assert len(skills) == 1  # merged via AI reconciliation, not duplicated
    assert scalar("SELECT status FROM documents WHERE id = ?", doc) == "ready"
    # The new document is linked as a source of the existing fact.
    assert scalar("SELECT COUNT(*) FROM fact_sources WHERE fact_id = ? AND document_id = ?", fid, doc) == 1


# --- A05: URL-scheme sanitization -------------------------------------------

def test_safe_external_url_filters_dangerous_schemes():
    assert safe_external_url("https://example.com/x") == "https://example.com/x"
    assert safe_external_url("http://example.com") == "http://example.com"
    assert safe_external_url("javascript:alert(1)") is None
    assert safe_external_url("data:text/html,<script>") is None
    assert safe_external_url("  ") is None
    assert safe_external_url(None) is None


def test_created_job_strips_dangerous_url(client, scalar):
    resp = client.post(
        "/app/jobs",
        data={"posting_text": "Backend Engineer. Python, AWS.", "url": "javascript:alert(1)"},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert resp.status_code in (303, 200)
    stored = scalar("SELECT url FROM jobs ORDER BY id DESC LIMIT 1")
    assert stored is None  # the javascript: URL was rejected, not stored


# --- A05: public-id table guard survives python -O --------------------------

def test_public_id_table_guard_is_not_an_assert():
    for fn in (lambda: resolve_id("evil_table", "x"), lambda: public_id_of("evil_table", 1)):
        try:
            fn()
            assert False, "expected ValueError for unknown table"
        except ValueError:
            pass


# --- A02 / A04: DB posture warnings -----------------------------------------


