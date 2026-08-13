"""The SQLite layer: Row, connections, constraint translation, migrations."""

import pytest

from app import db
from app.db import Row, get_conn, get_state, set_state


def test_row_supports_name_index_and_dict():
    r = Row(["a", "b"], [1, 2])
    assert r["a"] == 1 and r["b"] == 2
    assert r[0] == 1 and r[1] == 2
    assert dict(r) == {"a": 1, "b": 2}
    assert r.keys() == ["a", "b"]
    assert list(r) == [1, 2]
    assert r.get("missing", "d") == "d"


def test_row_factory_produces_named_rows():
    conn = get_conn()
    try:
        row = conn.execute("SELECT 1 AS one, 'x' AS letter").fetchone()
        assert row["one"] == 1 and row["letter"] == "x"
        assert row[0] == 1
    finally:
        conn.close()


def test_app_state_roundtrip_upsert():
    set_state("k", "v1")
    assert get_state("k") == "v1"
    set_state("k", "v2")  # ON CONFLICT DO UPDATE
    assert get_state("k") == "v2"
    assert get_state("absent") is None


def test_close_is_idempotent():
    conn = get_conn()
    conn.execute("SELECT 1")
    conn.close()
    conn.close()  # second close must be a no-op


def test_close_rolls_back_uncommitted_writes():
    conn = get_conn()
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", ("t", "x"))
    conn.close()  # closed without commit → rolled back
    assert get_state("t") is None


def test_close_hooks_run_even_on_an_error_path():
    released = []
    conn = get_conn()
    conn.add_close_hook(lambda: released.append(True))
    conn.close()
    assert released == [True]


def test_unique_violation_is_distinguishable():
    conn = get_conn()
    try:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('dup', 'a')")
        with pytest.raises(db.UniqueViolation):
            conn.execute("INSERT INTO app_state (key, value) VALUES ('dup', 'b')")
    finally:
        conn.close()


def test_foreign_key_violation_is_distinguishable():
    # Foreign keys must actually be enforced (PRAGMA foreign_keys is per
    # connection, so a missed PRAGMA would silently allow orphans).
    conn = get_conn()
    try:
        with pytest.raises(db.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO pulse_requests (user_id, pulse_id, kind) "
                "VALUES (1, 999999, 'new')"
            )
    finally:
        conn.close()


def test_cascade_delete_fires():
    conn = get_conn()
    try:
        job_id = conn.execute(
            "INSERT INTO jobs (user_id, raw_posting) VALUES (1, 'x') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO job_requirements (job_id, kind, skill, skill_display) "
            "VALUES (?, 'must', 'py', 'Python')",
            (job_id,),
        )
        conn.commit()
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM job_requirements WHERE job_id = ?", (job_id,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_public_id_default_is_a_uuid():
    conn = get_conn()
    try:
        pid = conn.execute(
            "INSERT INTO jobs (user_id, raw_posting) VALUES (1, 'x') RETURNING public_id"
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    import uuid

    parsed = uuid.UUID(pid)
    assert parsed.version == 4
    assert str(parsed) == pid  # stored lowercase and canonically formatted


def test_public_ids_are_distinct_per_row():
    conn = get_conn()
    try:
        pids = {
            conn.execute(
                "INSERT INTO jobs (user_id, raw_posting) VALUES (1, 'x') RETURNING public_id"
            ).fetchone()[0]
            for _ in range(20)
        }
        conn.commit()
    finally:
        conn.close()
    assert len(pids) == 20


def test_schema_migrations_applied(scalar):
    assert scalar("SELECT COUNT(*) FROM schema_migrations") >= 1


def test_init_db_is_idempotent(scalar):
    before = scalar("SELECT COUNT(*) FROM schema_migrations")
    db.init_db()  # re-running must not re-apply anything
    assert scalar("SELECT COUNT(*) FROM schema_migrations") == before
