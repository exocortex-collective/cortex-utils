"""Live-Postgres coverage for the attempts/max_attempts NOT NULL migration.

The defect is a disagreement between two DDLs, so only a server can hold both
halves: `queue_ddl()` declares both columns NOT NULL and every live table was
created by postmark's bootstrap DDL, which declared only the DEFAULTs. The
three-valued logic that follows, the catalogue's `attnotnull`, which lock mode
SET NOT NULL takes, and what the ALTER does about a NULL row are all facts
about the real pair, and a mock would hand back whatever the code asked.
"""

from __future__ import annotations

import os

import pytest
import structlog

psycopg2 = pytest.importorskip("psycopg2")

from cortex_utils.queue import ops  # noqa: E402
from cortex_utils.queue.ops import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS,
    claim,
    ensure_attempt_columns_not_null,
    fail_or_retry,
    nullable_attempt_columns,
)
from cortex_utils.queue.partitions import PartitionManager  # noqa: E402
from cortex_utils.queue.schema import ensure_queue_schema, queue_ddl  # noqa: E402

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)


def _production_ddl() -> str:
    """The canonical DDL with the two attempt columns left nullable.

    Derived from queue_ddl() rather than hand-written, for the reason
    test_claim_token_guard_live gives: the omission is the subject, and
    everything else about the shape should track the real one. Asserted, because
    a silently unmatched replacement would build the canonical shape and leave
    this whole file testing nothing.
    """
    ddl = queue_ddl()
    assert "attempts INT NOT NULL DEFAULT 0" in ddl, ddl
    assert f"max_attempts INT NOT NULL DEFAULT {DEFAULT_MAX_ATTEMPTS}" in ddl, ddl
    # One replace covers both: max_attempts ends in the same substring.
    loosened = ddl.replace("attempts INT NOT NULL DEFAULT", "attempts INT DEFAULT")
    assert "attempts INT NOT NULL" not in loosened, loosened
    return loosened


@pytest.fixture
def legacy():
    """A queue table with production's nullability, and nothing else different."""
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_attempts CASCADE")
        cur.execute("CREATE SCHEMA t_attempts")
        cur.execute("SET search_path = t_attempts")
        cur.execute(_production_ddl())
    # Through the real manager, so the partition carries the name
    # create_future_partitions() will look for: a hand-rolled `queue_today`
    # overlaps it, and ensure_queue_schema() then dies on the partition rather
    # than on anything this file is about.
    PartitionManager(c).create_future_partitions(days_ahead=1)
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_attempts CASCADE")
        c.close()


def _insert(conn, **values: object) -> int:
    columns = ", ".join(values)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO queue (queue_name, payload, {columns}) "
            f"VALUES ('q', '{{}}'::jsonb, {', '.join(['%s'] * len(values))}) RETURNING id",
            tuple(values.values()),
        )
        job_id = int(cur.fetchone()[0])
    conn.commit()
    return job_id


def _warnings(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["log_level"] == "warning"]


def test_the_fixture_really_is_nullable(legacy) -> None:
    """Everything else here rests on it, and it comes from a string replace."""
    assert nullable_attempt_columns(legacy) == ["attempts", "max_attempts"]


def test_boot_tightens_both_columns(legacy) -> None:
    ensure_queue_schema(legacy)
    assert nullable_attempt_columns(legacy) == []
    # Idempotent, and the catalogue probe is what makes the common boot free.
    assert ensure_attempt_columns_not_null(legacy) == []


def test_the_invariant_is_enforced_by_the_table_afterwards(legacy) -> None:
    """The reason to prefer SET NOT NULL over a COALESCE in each predicate: the
    table refuses the row, so no reader has to remember to defend itself."""
    ensure_queue_schema(legacy)
    with pytest.raises(psycopg2.errors.NotNullViolation):
        _insert(legacy, max_attempts=None)
    legacy.rollback()


def test_only_the_still_nullable_column_is_tightened(legacy) -> None:
    """Half-migrated is a real state: the previous version of this function
    altered both columns in one transaction, so one blocked column took its
    neighbour down with it."""
    with legacy.cursor() as cur:
        cur.execute("ALTER TABLE queue ALTER COLUMN attempts SET NOT NULL")
    legacy.commit()

    assert nullable_attempt_columns(legacy) == ["max_attempts"]
    assert ensure_attempt_columns_not_null(legacy) == ["max_attempts"]
    assert nullable_attempt_columns(legacy) == []


def test_a_null_row_blocks_only_its_own_column(legacy) -> None:
    """The ALTER is the authority on NULLs, not a pre-count. A pre-count is a
    guess another session can invalidate before the ALTER runs, and declining
    on it left SET NOT NULL free to raise where nothing caught it."""
    _insert(legacy, max_attempts=None)

    with structlog.testing.capture_logs() as logs:
        assert ensure_attempt_columns_not_null(legacy) == ["attempts"]

    assert nullable_attempt_columns(legacy) == ["max_attempts"]
    warnings = _warnings(logs)
    assert len(warnings) == 1, warnings
    assert (warnings[0]["column"], warnings[0]["nulls"]) == ("max_attempts", 1), (
        "the column and the count are what an operator needs in order to backfill"
    )


def test_each_column_is_counted_separately(legacy) -> None:
    """A constant, or the query pinned to one column name, survived while only
    one column held exactly one NULL."""
    _insert(legacy, attempts=None)
    _insert(legacy, max_attempts=None)
    _insert(legacy, max_attempts=None)

    with structlog.testing.capture_logs() as logs:
        assert ensure_attempt_columns_not_null(legacy) == []

    counts = {w["column"]: w["nulls"] for w in _warnings(logs)}
    assert counts == {"attempts": 1, "max_attempts": 2}


def test_a_boot_against_null_rows_still_boots(legacy) -> None:
    """The whole reason this migration is allowed to decline."""
    _insert(legacy, max_attempts=None)
    ensure_queue_schema(legacy)
    assert nullable_attempt_columns(legacy) == ["max_attempts"]


def test_a_held_lock_declines_rather_than_crashing_the_boot(legacy, monkeypatch) -> None:
    """The decline path only reachable under contention: the ALTER needs ACCESS
    EXCLUSIVE on the parent and every partition, which live claim traffic
    holds. A fleet redeploy must not crash-loop on it.

    Two bounds, and both earn their place. `lock_timeout` is the one under
    test, patched down from 5s because its duration is not the subject.
    `statement_timeout` belongs to the test: without it, a regression that
    stopped `lock_timeout` applying would leave the ALTER waiting on a lock
    this same thread holds, so the test would HANG rather than fail -- there is
    no pytest timeout, and a test that hangs reports nothing. Observed at 70s
    during review, with the ALTER in wait_event Lock and the blocker idle in
    transaction. With the bound, that regression cancels the statement and the
    pgcode assertion below fails instead.
    """
    monkeypatch.setattr(ops, "MIGRATION_LOCK_TIMEOUT_MS", 200)
    with legacy.cursor() as cur:
        cur.execute("SET statement_timeout = 5000")
    legacy.commit()

    blocker = psycopg2.connect(DSN)
    try:
        with blocker.cursor() as cur:
            cur.execute("SET search_path = t_attempts")
            # Conflicts with ACCESS EXCLUSIVE, which is what the ALTER takes;
            # a plain reader is enough to lose the race.
            cur.execute("LOCK TABLE queue IN ACCESS SHARE MODE")

        with structlog.testing.capture_logs() as logs:
            assert ensure_attempt_columns_not_null(legacy) == []

        assert nullable_attempt_columns(legacy) == ["attempts", "max_attempts"], (
            "nothing may be tightened while the lock is held"
        )
        warnings = _warnings(logs)
        assert [w["column"] for w in warnings] == ["attempts", "max_attempts"], warnings
        # 55P03 is lock_not_available. Asserted as a code rather than as a
        # message: it says the ALTER really lost the lock, where the prose
        # would also pass for a statement cancelled by the bound above.
        assert [w["pgcode"] for w in warnings] == ["55P03", "55P03"], warnings
    finally:
        blocker.rollback()
        blocker.close()


def test_a_null_max_attempts_row_is_stuck_pending(legacy) -> None:
    """The pathology the migration prevents, asserted so the fix has a subject:
    claim() can neither hand this row out nor retire it."""
    job_id = _insert(legacy, max_attempts=None)
    assert claim(legacy, "q", "worker-a") == []
    with legacy.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE id = %s", (job_id,))
        assert cur.fetchone()[0] == "pending", "nothing retired it either"
    legacy.rollback()


def test_fail_or_retry_does_not_raise_on_a_null_max_attempts(legacy) -> None:
    """None on the right of `>=` raises TypeError inside _tx, which rolls back:
    the job stays `processing` and last_error is never written.

    Set up by hand because claim() cannot hand out such a row -- which is how
    the crash stayed latent, and why it would arrive the day a NULL appeared
    under a worker that had already claimed it.
    """
    job_id = _insert(legacy, max_attempts=None, status="processing", claimed_by="worker-a")
    with structlog.testing.capture_logs() as logs:
        assert fail_or_retry(legacy, job_id, "boom", "worker-a") == "pending"
    with legacy.cursor() as cur:
        cur.execute("SELECT status, attempts, last_error FROM queue WHERE id = %s", (job_id,))
        assert cur.fetchone() == ("pending", 1, "boom")
    # The substitution must say so: this is the one place that sees the bad row.
    # Selected by the field only this warning carries -- the ordinary
    # "scheduled for retry" warning names the same job_id.
    substituted = [w for w in _warnings(logs) if "default" in w]
    assert len(substituted) == 1, _warnings(logs)
    assert (substituted[0]["job_id"], substituted[0]["default"]) == (
        job_id,
        DEFAULT_MAX_ATTEMPTS,
    )


def test_the_null_fallback_is_the_default_not_an_excuse_to_retry(legacy) -> None:
    """Pins the fallback's VALUE, which "did not raise" does not: a row already
    at DEFAULT_MAX_ATTEMPTS - 1 must retire on this failure."""
    job_id = _insert(
        legacy,
        max_attempts=None,
        attempts=DEFAULT_MAX_ATTEMPTS - 1,
        status="processing",
        claimed_by="worker-a",
    )
    assert fail_or_retry(legacy, job_id, "boom", "worker-a") == "failed"


def test_a_zero_max_attempts_row_is_retired_not_retried(legacy) -> None:
    """`row[1] or DEFAULT_MAX_ATTEMPTS` read a legitimate 0 as "unset" and
    granted the row three attempts. 0 means retire on the first failure."""
    job_id = _insert(legacy, max_attempts=0, status="processing", claimed_by="worker-a")
    assert fail_or_retry(legacy, job_id, "boom", "worker-a") == "failed"
