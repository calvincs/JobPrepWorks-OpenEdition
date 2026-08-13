"""The local spend ledger and its optional daily brake (services/usage.py).

Nothing here gates a feature. The ledger exists so the Settings page can tell
you what today cost, and LLM_DAILY_LIMIT exists so a runaway loop can't quietly
drain a paid API key overnight.
"""

import pytest

from app import config
from app.config import Settings
from app.services import usage


def _limit(monkeypatch, n):
    monkeypatch.setattr(config, "settings", Settings(llm_daily_limit=n))
    monkeypatch.setattr(usage, "settings", config.settings)


def test_no_limit_by_default_records_but_never_blocks(scalar):
    for _ in range(50):
        usage.spend(1, "intake")
    assert usage.used_today(1) == 50
    assert scalar("SELECT COUNT(*) FROM llm_requests") == 50


def test_brake_refuses_once_spent(monkeypatch):
    _limit(monkeypatch, 3)
    for _ in range(3):
        usage.spend(1, "drill")
    with pytest.raises(usage.QuotaExceeded):
        usage.spend(1, "drill")


def test_refusal_copy_points_at_the_setting(monkeypatch):
    _limit(monkeypatch, 1)
    usage.spend(1, "drill")
    with pytest.raises(usage.QuotaExceeded) as exc:
        usage.spend(1, "drill")
    assert "LLM_DAILY_LIMIT" in str(exc.value)


def test_batch_spend_counts_units_not_calls(monkeypatch):
    usage.spend(1, "fit_all", units=7)
    assert usage.used_today(1) == 7


def test_try_spend_degrades_instead_of_raising(monkeypatch):
    _limit(monkeypatch, 1)
    assert usage.try_spend(1, "grade") is True
    assert usage.try_spend(1, "grade") is False  # no exception


def test_check_then_record_lets_a_lost_claim_go_uncharged(monkeypatch):
    """The claim pattern: check first, only ledger if the atomic claim won."""
    usage.check(1, "pitch")          # would raise if over
    # ... claim lost, so nothing is recorded ...
    assert usage.used_today(1) == 0


def test_yesterdays_spend_does_not_count_against_today(monkeypatch, scalar):
    _limit(monkeypatch, 2)
    conn_rows = 5
    from app.db import get_conn

    conn = get_conn()
    try:
        for _ in range(conn_rows):
            conn.execute(
                "INSERT INTO llm_requests (user_id, kind, units, created_at) "
                "VALUES (1, 'intake', 1, '2020-01-01 10:00:00')"
            )
        conn.commit()
    finally:
        conn.close()
    usage.spend(1, "intake")  # today is still clear
    assert usage.used_today(1) == 1


# ── The Settings page's usage card ───────────────────────────────────────────


def test_summary_without_a_limit_reports_a_count_not_a_gauge():
    usage.spend(1, "intake")
    usage.spend(1, "drill", units=2)
    summary = usage.usage_summary(1)
    assert summary["used_today"] == 3
    assert summary["daily_limit"] == 0
    assert summary["daily_left"] is None
    assert {"label": "study drills", "units": 2} in summary["by_kind"]


def test_summary_with_a_limit_reports_headroom(monkeypatch):
    _limit(monkeypatch, 10)
    usage.spend(1, "intake", units=4)
    summary = usage.usage_summary(1)
    assert (summary["daily_left"], summary["daily_pct"]) == (6, 40)


def test_summary_counts_pulse_separately(scalar):
    from app.db import get_conn

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO pulse_requests (user_id, pulse_id, kind) VALUES (1, NULL, 'new')"
        )
        conn.commit()
    finally:
        conn.close()
    summary = usage.usage_summary(1)
    assert summary["pulse_used"] == 1
    assert summary["used_today"] == 0  # research rides its own allowance


def test_summary_reports_lifetime_across_the_rollup(scalar):
    from app.db import get_conn

    usage.spend(1, "intake", units=2)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO llm_usage_daily (user_id, day, kind, units) "
            "VALUES (1, '2020-01-01', 'intake', 9)"
        )
        conn.commit()
    finally:
        conn.close()
    assert usage.usage_summary(1)["lifetime"] == 11


# ── Rollup and prune ─────────────────────────────────────────────────────────


def test_rollup_collapses_aged_rows_and_keeps_the_totals(scalar):
    from app.db import get_conn

    conn = get_conn()
    try:
        for _ in range(4):
            conn.execute(
                "INSERT INTO llm_requests (user_id, kind, units, created_at) "
                "VALUES (1, 'intake', 2, '2020-01-01 10:00:00')"
            )
        conn.commit()
    finally:
        conn.close()
    usage.spend(1, "intake")  # today's row must survive

    pruned = usage.rollup_and_prune(batch=2)  # forces more than one batch
    assert pruned == 4
    assert scalar("SELECT COUNT(*) FROM llm_requests") == 1
    assert scalar(
        "SELECT units FROM llm_usage_daily WHERE day = '2020-01-01' AND kind = 'intake'"
    ) == 8


def test_rollup_accumulates_into_an_existing_summary_row(scalar):
    from app.db import get_conn

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO llm_usage_daily (user_id, day, kind, units) "
            "VALUES (1, '2020-01-01', 'intake', 5)"
        )
        conn.execute(
            "INSERT INTO llm_requests (user_id, kind, units, created_at) "
            "VALUES (1, 'intake', 3, '2020-01-01 10:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    usage.rollup_and_prune()
    assert scalar("SELECT units FROM llm_usage_daily WHERE day = '2020-01-01'") == 8


def test_rollup_never_prunes_todays_rows_the_brake_reads(monkeypatch, scalar):
    monkeypatch.setattr(
        config, "settings", Settings(llm_daily_limit=5, llm_ledger_retention_days=0)
    )
    monkeypatch.setattr(usage, "settings", config.settings)
    usage.spend(1, "intake")
    usage.rollup_and_prune()  # retention clamps to >= 1 day
    assert scalar("SELECT COUNT(*) FROM llm_requests") == 1
