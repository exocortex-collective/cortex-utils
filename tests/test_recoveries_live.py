"""Live-Postgres tests for recovery counting (cortex-wh3b).

The behaviour under test is a side effect of a data-modifying CTE inside
`claim()`, which is not observable against a mock: a stubbed cursor returns
whatever the code asked it for, so it can confirm the UPDATE was *written* but
never that Postgres applied it to the right rows. The interesting cases are all
about which rows the CTE does and does not touch.

Skipped unless a throwaway Postgres is reachable, so the default suite stays
fast and offline:

    docker run -d --rm --name pgtest -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16
    CORTEX_TEST_DSN="host=127.0.0.1 port=55432 user=postgres password=x dbname=postgres" \
        uv run pytest tests/test_recoveries_live.py
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from cortex_utils.queue.inspect import health, stuck  # noqa: E402
from cortex_utils.queue.ops import (  # noqa: E402
    claim,
    complete,
    enqueue,
    has_recoveries_column,
    release,
)
from cortex_utils.queue.schema import ensure_queue_schema, queue_ddl  # noqa: E402

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

SCHEMA = "t_recoveries"

VIS = 30  # minutes; the default claim() window


@pytest.fixture
def conn():
    """One connection on a private schema. Nothing here needs a race."""
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        cur.execute(queue_ddl())
        cur.execute("SELECT CURRENT_DATE")
        day = cur.fetchone()[0]
        cur.execute(
            f"CREATE TABLE queue_{day:%Y_%m_%d} PARTITION OF queue "
            f"FOR VALUES FROM ('{day}') TO ('{day + timedelta(days=1)}')"
        )
    c.autocommit = False
    try:
        yield c
    finally:
        # rollback() first, unconditionally: psycopg2 refuses to change
        # autocommit while a transaction is open, so a test that left one
        # behind would make this teardown RAISE and never close the
        # connection -- leaking a backend that holds AccessShareLock on
        # `queue` and wedging the next test's DROP SCHEMA for good.
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.close()


def _row(conn, job_id: int) -> tuple[int, int, str]:
    """(recoveries, attempts, status) for one row.

    Commits the read. psycopg2 opens a transaction even for a SELECT, and this
    connection is not autocommit, so without it the backend sits idle in
    transaction holding AccessShareLock on `queue` -- and the next test's
    fixture blocks forever on DROP SCHEMA CASCADE, which needs
    AccessExclusiveLock. That is a hung runner, not a failing test, which is
    the worst way for a suite to break.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT recoveries, attempts, status FROM queue WHERE id = %s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return row


def _abandon(conn, job_id: int) -> None:
    """Age a live claim past the visibility window, as a dead worker leaves it.

    The claimant is not cleared: that is the whole point of a stale claim, and
    clearing it would make the row ordinarily pending instead.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET claimed_at = NOW() - (INTERVAL '1 minute' * %s) WHERE id = %s",
            (VIS * 2, job_id),
        )
    conn.commit()


def _crash_cycle(conn, job_id: int) -> None:
    """One claim -> crash -> recovery cycle.

    The recovering call does not return the row it reset -- a data-modifying
    CTE is invisible to its siblings -- so the claim that picks it up again is
    the second one.
    """
    _abandon(conn, job_id)
    claim(conn, "q", "worker-b", visibility_timeout_min=VIS)
    claim(conn, "q", "worker-b", visibility_timeout_min=VIS)


def test_a_stale_reset_counts_a_recovery(conn) -> None:
    """The defect wh3b is about: before this column the reset was free AND
    silent, so a crash loop moved no number anywhere in the system."""
    job_id = enqueue(conn, "q", {"x": 1})
    conn.commit()
    assert claim(conn, "q", "worker-a", visibility_timeout_min=VIS)
    assert _row(conn, job_id) == (0, 0, "processing")

    _crash_cycle(conn, job_id)

    recoveries, attempts, _status = _row(conn, job_id)
    assert recoveries == 1, "the stale reset must count itself"
    assert attempts == 0, (
        "and must still not charge an attempt -- that semantics is deliberate "
        "and this column exists so it can stay"
    )


def test_recoveries_accumulate_so_a_poison_pill_is_visible(conn) -> None:
    """A job that kills its worker never reaches fail_or_retry, so it cycles
    forever on an untouched budget. The count is the only thing that moves."""
    job_id = enqueue(conn, "q", {"x": 1})
    conn.commit()
    claim(conn, "q", "worker-a", visibility_timeout_min=VIS)

    for _ in range(3):
        _crash_cycle(conn, job_id)

    recoveries, attempts, _status = _row(conn, job_id)
    assert (recoveries, attempts) == (3, 0)


def test_release_is_not_a_recovery(conn) -> None:
    """Requested by cryo (cryo-x8g): cryo hands rows back on every deploy stop,
    and counting that would bury the crash signal under routine deploys.

    This holds by construction rather than by a guard -- release() is a plain
    UPDATE and the increment lives in claim()'s reset_stale CTE -- which is
    exactly why it needs a test: nothing in release() would have to change for
    it to break.
    """
    job_id = enqueue(conn, "q", {"x": 1})
    conn.commit()
    claim(conn, "q", "worker-a", visibility_timeout_min=VIS)

    assert release(conn, job_id, delay_s=0, worker="worker-a") is True
    claim(conn, "q", "worker-b", visibility_timeout_min=VIS)

    recoveries, attempts, status = _row(conn, job_id)
    assert recoveries == 0, "a deliberate handback is not a crash"
    assert attempts == 0
    assert status == "processing"


def test_a_retired_row_is_not_counted_as_recovered(conn) -> None:
    """An exhausted stale row goes to 'failed' down the other CTE. It was not
    recovered -- it was given up on -- and counting it there would inflate the
    number with rows that have already stopped looping."""
    job_id = enqueue(conn, "q", {"x": 1})
    conn.commit()
    claim(conn, "q", "worker-a", visibility_timeout_min=VIS)
    with conn.cursor() as cur:
        cur.execute("UPDATE queue SET attempts = max_attempts WHERE id = %s", (job_id,))
    conn.commit()

    _abandon(conn, job_id)
    claim(conn, "q", "worker-b", visibility_timeout_min=VIS)

    recoveries, _attempts, status = _row(conn, job_id)
    assert status == "failed", "precondition: this row took the retire path"
    assert recoveries == 0


def test_health_reports_the_worst_live_count_per_queue(conn) -> None:
    """Per queue, so it says WHICH queue is looping, and live rows only."""
    looping = enqueue(conn, "q", {"x": "loops"})
    quiet = enqueue(conn, "q", {"x": "fine"})
    conn.commit()
    claim(conn, "q", "worker-a", visibility_timeout_min=VIS)
    for _ in range(2):
        _crash_cycle(conn, looping)

    depths = {d.queue_name: d for d in health(conn).depths}
    assert depths["q"].max_recoveries == 2
    assert _row(conn, quiet)[0] == 0

    # A finished row's recoveries are history, not a loop: it survived a worker
    # restart and then completed, which is the mechanism working.
    claim(conn, "q", "worker-c", visibility_timeout_min=VIS)
    with conn.cursor() as cur:
        cur.execute("SELECT claimed_by FROM queue WHERE id = %s", (looping,))
        holder = cur.fetchone()[0]
    conn.commit()
    assert holder is not None, "precondition: the row is claimed again"
    assert complete(conn, looping, holder) is True

    depths = {d.queue_name: d for d in health(conn).depths}
    assert depths["q"].max_recoveries == 0, (
        "a completed row must not keep the queue looking like it is looping"
    )


def test_stuck_reports_recoveries_for_the_row(conn) -> None:
    """health() says a queue is looping; stuck() says which row. High
    recoveries with low attempts is the signature."""
    job_id = enqueue(conn, "q", {"x": 1})
    conn.commit()
    claim(conn, "q", "worker-a", visibility_timeout_min=VIS)
    _crash_cycle(conn, job_id)
    _abandon(conn, job_id)

    rows = [j for j in stuck(conn, visibility_timeout_min=VIS) if j.id == job_id]
    assert len(rows) == 1, rows
    assert rows[0].recoveries == 1
    assert rows[0].attempts == 0


def test_a_table_predating_the_column_is_migrated_not_refused(conn) -> None:
    """The deployment hazard, and the reason `recoveries` had to join
    _MIGRATED_COLUMNS in the same commit that added it to REQUIRED_COLUMNS.

    ensure_queue_schema() REFUSES a table missing a column no migration adds --
    deliberately, so a half-migrated shape fails loudly. Adding the column to
    only the required set would therefore have raised on every existing
    production table, crash-looping the fleet on boot: the exact failure mode
    the sibling attempt-nullability work had just hardened against.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE queue DROP COLUMN recoveries")
    conn.commit()
    assert has_recoveries_column(conn) is False, "precondition"

    ensure_queue_schema(conn)

    assert has_recoveries_column(conn) is True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attnotnull FROM pg_attribute "
            "WHERE attrelid = to_regclass('queue') AND attname = 'recoveries'"
        )
        assert cur.fetchone()[0] is True, "added NOT NULL so claim() can increment"

    # And the added column is usable immediately, with no backfill step: the
    # increment reads COALESCE(recoveries, 0), but the DEFAULT is what makes
    # pre-existing rows start at 0 rather than NULL.
    job_id = enqueue(conn, "q", {"x": 1})
    conn.commit()
    claim(conn, "q", "worker-a", visibility_timeout_min=VIS)
    _crash_cycle(conn, job_id)
    assert _row(conn, job_id)[0] == 1
