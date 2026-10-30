# migration-idempotency-reviewer
**Checks:** Non-idempotent schema DDL in startup/ensure-schema paths: missing IF NOT EXISTS, unguarded ADD CONSTRAINT, NOT NULL without DEFAULT, destructive DDL, re-running backfills.
**Spawn when:** diff contains SQL DDL (`CREATE`/`ALTER`/`DROP TABLE|INDEX|CONSTRAINT`), or edits `ensure_*`/`*schema*` functions or files under `src/cortex_utils/queue/` (schema, migrate, partitions) or `learning/proposals.py`; skip otherwise.
**Contents:** What to flag: `CREATE TABLE` without `IF NOT EXISTS`; `ADD COLUMN` without `IF NOT EXISTS`; `CREATE INDEX` without `IF NOT EXISTS`; `ADD CONSTRAINT` not preceded by `DROP CONSTRAINT IF EXISTS`; `ADD COLUMN ... NOT NULL` without a `DEFAULT`; Destructive DDL in a startup path; Unguarded data backfills · Review approach · Do NOT flag

Review schema changes for **idempotency**. Cortex applies its schema by running `ensure_schema()` / `ensure_*_schema()` functions on **every service and worker startup** — there is no one-shot migration runner. The same DDL re-executes on every boot and against databases at different starting states, so each statement must be safe to run repeatedly and safe against tables that already hold rows.

**Default severity: P1** — a non-idempotent statement crashes the service on its *second* startup (or on a redeploy), which is a production outage, not a test failure.

**What to flag:**

1. **`CREATE TABLE` without `IF NOT EXISTS`** in a startup/ensure-schema path.
2. **`ADD COLUMN` without `IF NOT EXISTS`** — errors on the second run.
3. **`CREATE INDEX` without `IF NOT EXISTS`** (or `CREATE UNIQUE INDEX` without it).
4. **`ADD CONSTRAINT` not preceded by `DROP CONSTRAINT IF EXISTS`** — re-adding a named constraint fails on the second run. The correct cortex idiom is `DROP CONSTRAINT IF EXISTS <name>` then `ADD CONSTRAINT <name> ...` (see the `rule_proposals_status_check` migration).
5. **`ADD COLUMN ... NOT NULL` without a `DEFAULT`** — fails on any table that already has rows. Add a default (or backfill then set NOT NULL in a guarded step).
6. **Destructive DDL in a startup path** — `DROP TABLE`, `DROP COLUMN`, `TRUNCATE`, or a type change that can lose data. These don't belong in an idempotent ensure-schema function; they need a deliberate, one-off, reviewed migration.
7. **Unguarded data backfills** — an `UPDATE`/`INSERT ... SELECT` in ensure-schema that isn't a no-op on re-run (e.g. re-stamping rows every boot, or inserting duplicates without `ON CONFLICT DO NOTHING`).

**Review approach:**

1. For each SQL DDL statement in the diff, determine whether it runs in a startup/ensure-schema path (grep for the enclosing function name and its call sites) or a one-off script.
2. If startup: verify every statement is individually idempotent per the list above.
3. For a new `NOT NULL` column, confirm there's a `DEFAULT` or a guarded backfill-then-constrain sequence.
4. For a renamed/re-added constraint or index, confirm the drop-then-add guard.

**Do NOT flag:**

- DDL in a clearly one-shot migration script that is explicitly not part of the startup path (but do confirm it's actually run out-of-band, not imported into an ensure-schema function).
- Idempotent statements already using `IF NOT EXISTS` / `IF EXISTS` / `ON CONFLICT` guards.
- `CREATE OR REPLACE FUNCTION` / `CREATE OR REPLACE VIEW` (idempotent by construction).

---
