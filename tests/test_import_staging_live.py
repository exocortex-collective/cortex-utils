"""Live-Postgres tests for staging a triage config (cortex-0xdh).

Staging stores a version without activating it and without touching the
global email mappings; activating re-imports it as a new active version. Both
depend on the real triggers (version numbering, one active version), so these
run against the deployed schema in tests/fixtures/triage_config_schema.sql.

Skipped unless CORTEX_TEST_DSN points at a throwaway Postgres; CI starts one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from cortex_utils.triage_config.importer import import_yaml_to_db

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

SCHEMA = (Path(__file__).parent / "fixtures" / "triage_config_schema.sql").read_text()


def _config(label: str) -> str:
    return f"""
version: 1
label_prefix: Cortex
default_label: Uncategorized
chains:
  main:
    rules:
      - match: {{from_addr: a@example.com}}
        action: {{label: {label}}}
priority_email_mappings:
  vip@example.com: {{label: {label}}}
"""


@pytest.fixture
def conn() -> Iterator[Any]:
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_stage CASCADE")
        cur.execute("CREATE SCHEMA t_stage")
        cur.execute("SET search_path = t_stage, public")
        cur.execute(SCHEMA)
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_stage CASCADE")
        c.close()


def _versions(conn: Any) -> list[tuple[int, bool]]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, is_active FROM triage_config_versions ORDER BY 1")
        return cur.fetchall()


def _mapping_label(conn: Any) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label FROM triage_email_mappings WHERE email_address = 'vip@example.com'"
        )
        row = cur.fetchone()
        return row[0] if row else None


def test_staged_version_is_inactive_and_leaves_mappings_alone(conn: Any) -> None:
    import_yaml_to_db(conn, _config("Live"), "test")
    staged = import_yaml_to_db(conn, _config("Staged"), "test", activate=False)
    assert _versions(conn) == [(1, True), (staged, False)]
    assert _mapping_label(conn) == "Live"


def test_activating_content_identical_to_a_staged_version_creates_an_active_one(
    conn: Any,
) -> None:
    import_yaml_to_db(conn, _config("Live"), "test")
    staged = import_yaml_to_db(conn, _config("Next"), "test", activate=False)
    active = import_yaml_to_db(conn, _config("Next"), "test")
    assert active != staged
    assert _versions(conn) == [(1, False), (staged, False), (active, True)]
    assert _mapping_label(conn) == "Next"


def test_reuploading_the_active_config_is_a_no_op(conn: Any) -> None:
    first = import_yaml_to_db(conn, _config("Live"), "test")
    assert import_yaml_to_db(conn, _config("Live"), "test") == first
    assert _versions(conn) == [(first, True)]


def test_restaging_identical_content_returns_the_stored_version(conn: Any) -> None:
    import_yaml_to_db(conn, _config("Live"), "test")
    staged = import_yaml_to_db(conn, _config("Next"), "test", activate=False)
    assert import_yaml_to_db(conn, _config("Next"), "test", activate=False) == staged
    assert len(_versions(conn)) == 2
