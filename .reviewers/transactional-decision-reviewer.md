# transactional-decision-reviewer
**Checks:** Python code guessing database state (existence, ordering, time) or reading error codes as proof, instead of letting Postgres decide inside the transaction.
**Spawn when:** diff in `*.py` that runs SQL (`cursor.execute`, `psycopg2`) and branches on a prior SELECT/exists check, catches DB errors (`UniqueViolation`, `pgcode`), or edits `src/cortex_utils/queue/`; skip otherwise.
**Contents:** What to flag: A decision Postgres could have made, made in Python instead; An error code read as proof of state; A guess where a query was already affordable; Compensating logic built on the guess · Where a guess is legitimate — do NOT flag · Review approach

**The database is the arbiter.** Existence, uniqueness, ordering, atomicity, the
current time — these are things Postgres knows authoritatively and this process
does not. Code that decides them in Python is holding a second opinion that can
disagree with the only one that counts, and the disagreement is silent.

Review code that **acts on a guess about database state** instead of asking
inside a transaction. When a decision depends on whether a row, table, or
partition exists, and the cost of checking is a cheap query on an
already-open connection, the check belongs in the transaction rather than being
inferred from an error code or a lock outcome.

The failure this catches is specific: an exception is treated as evidence of
something it does not actually prove, so the code takes the wrong branch and
reports success. That reads as defensive programming and behaves as a lie.

**Default severity: P2.** **P1** when the wrong branch is reported as success,
because the caller then proceeds on a false belief.

**What to flag:**

0. **A decision Postgres could have made, made in Python instead.** A
   read-modify-write where one statement would do; a uniqueness check racing
   ahead of a constraint; ordering imposed after the fact rather than by `ORDER
   BY`; a timestamp or date computed locally (see clock-discipline-reviewer).
   The test is whether the database could arbitrate authoritatively in one
   statement — if so, the Python version is a second opinion.
1. **An error code read as proof of state.** `DuplicateTable` treated as "it
   exists, therefore fine" — it proves a *name* is taken, not that the object is
   the thing you wanted. `LockNotAvailable` treated as "someone else is doing my
   work" — it proves contention, and the holder may be doing something else
   entirely, including dropping the object you assumed it was creating.
2. **A guess where a query was already affordable.** The connection is open, the
   transaction is live, and the check is an indexed catalogue or primary-key
   lookup. Prefer: attempt, and on ambiguity ask; or check, then act, with the
   idempotent guard to cover the window.
3. **Compensating logic built on the guess** — retries, backoff, or "assume it
   worked" paths that exist only because the code declined to look.

**Where a guess is legitimate — do NOT flag:**

- **When checking would contend.** The point is to avoid guessing, not to add
  locking. A check that itself takes a heavy lock (`SELECT FOR UPDATE` on a hot
  row, anything needing `ACCESS EXCLUSIVE`) is worse than the guess it replaces.
  Catalogue reads, PK lookups and `to_regclass` are cheap and take no such lock;
  prefer those.
- **When the check cannot be authoritative.** If the answer can change between
  the check and the act and the act is not idempotent, checking buys false
  confidence. Say so rather than adding a check that reads as a guarantee.
- **Hot paths where the extra round trip is measurable** and the guess is
  documented as an accepted approximation.
- **Advisory-lock serialisation** already used to make a decision safe — that is
  the correct pattern, not a guess.

**Review approach:**

1. Find `except` clauses that set a state variable, return a status, or log
   success, rather than re-raising or handling a genuine error.
1b. And find statements whose own result is discarded before success is
   reported -- a DELETE or UPDATE whose `rowcount` is never read, an INSERT
   whose RETURNING is ignored. This is the same defect without an exception to
   notice: the database said how many rows it touched and the code did not
   listen. Grepping for `except` alone misses it, which is how it survived here.
2. For each, ask what the exception actually proves, and whether the code's next
   action assumes more than that.
3. If more: can the truth be obtained with a cheap query on the open connection?
   If yes, that is the finding. If the only check would contend or could not be
   authoritative, leave it and say why.
4. Check the tests: a mock that always returns the happy answer cannot tell a
   correct concession from a lucky one.

**History:** the queue's write-path partition self-heal treated both
`DuplicateTable` and `LockNotAvailable` as "the partition exists". Neither is:
the first can be a same-named relation that is not a partition of this queue,
and the second can be `ALTER TABLE` or `DROP TABLE` holding the parent, with a
longer lock timeout than the self-heal's own. It now asks `pg_inherits` after
either failure -- a cheap catalogue read on the connection it already holds.

---
