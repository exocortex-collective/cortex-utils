# clock-discipline-reviewer
**Checks:** Date/time values reaching Postgres from the Python clock instead of the server clock; mixed-clock statements, naive datetimes, race-narrowing tolerances.
**Spawn when:** diff in `*.py` or SQL that uses `datetime.now`/`utcnow`/`date.today`/`time.time` near a query, or SQL with `now()`/`CURRENT_DATE`/`INTERVAL`/partition dates (esp. `src/cortex_utils/queue/`); skip otherwise.
**Contents:** What to flag: A single statement mixing both clocks; Naive datetimes compared against `TIMESTAMPTZ`; A tolerance or window that narrows a race instead of removing it; Tests that assert the client clock · Do NOT flag · Review approach

Review any date or time that reaches the database for **which clock produced
it**. Postgres rows are stamped by the *server*; a value computed in the Python
process comes from a *different* clock, in a possibly different timezone, and
the two agree only by coincidence.

That coincidence is what makes this class dangerous. It tests green on every box
where client and server are both UTC, passes review because the code reads
naturally, and fails on the first deployment where they are not. It is not a
race that shows up under load — it is a correctness bug that shows up under
*deployment*.

**Default severity: P1** when the value routes or creates a partition, because
the failure is silent data loss: rows route to a partition that does not exist,
or a self-heal creates the wrong day and the insert fails for a partition that
was never missing. **P2** for filters and reported timestamps, where the result
is wrong answers rather than lost writes.

**What to flag:**

1. **`date.today()`, `datetime.now()`, `datetime.utcnow()`, `time.time()` whose
   value reaches SQL** — as a bound parameter, an interpolated partition name, a
   date range, or a cutoff. The partition case is the worst: the partition key is
   `created_at TIMESTAMPTZ DEFAULT NOW()`, a value the server produces, so a
   client-derived partition date is a second clock deciding where a server-dated
   row lives.
2. **A single statement mixing both clocks** — e.g. `WHERE claimed_at < %s` with
   a Python cutoff, while the same query computes `NOW() - claimed_at`. The
   filter and the arithmetic then disagree.
3. **Naive datetimes compared against `TIMESTAMPTZ`.** `datetime.now()` is naive
   local time; the column is timezone-aware. The comparison is doubly wrong and
   silently so.
4. **A tolerance or window that narrows a race instead of removing it.** Creating
   only "today" leaves a retry that crosses midnight still broken; creating today
   *and* tomorrow in one pass removes it.
5. **Tests that assert the client clock.** A test written against `date.today()`
   cannot distinguish correct behaviour from the bug, and will keep passing after
   a regression. Fixtures should use a fixed date that is deliberately *not*
   today.

**The fix, not a suggestion:** derive the value from the same clock that produces
the data — `CURRENT_DATE`, `NOW()`, `clock_timestamp()`, or an
`INTERVAL` expression evaluated server-side. If Python needs the value, fetch it
(`SELECT CURRENT_DATE`) rather than computing it.

**Do NOT flag:**

- Clocks that never reach the database: log timestamps, in-process rate-limiter
  windows, sleep durations, metrics emitted to Prometheus, elapsed-time
  measurement.
- One-shot scripts and migrations where the operator's local date is the
  intended input.
- `clock_timestamp()` vs `NOW()` distinctions inside a transaction — both are
  server-side and that choice is about statement-versus-transaction time, not
  clock provenance.

**Review approach:**

1. `grep -n "date.today()\|datetime.now()\|utcnow()\|time.time()"` over the diff.
2. For each hit, trace whether the value reaches a cursor. If it does not, skip it.
3. If it does, ask what column or expression it is compared against or used to
   name. Anything derived from `NOW()` on the server is a finding.
4. Check the tests for the same construct — a client-clock fixture hides the bug
   it is meant to catch.

**History:** `ops.py` created write-path partitions from `date.today()` while
`created_at` came from server `NOW()`, and `partitions.py` did the same in
scheduled maintenance. Both were invisible on ares and hades because both are
UTC. Found by review, not by tests — two of the tests asserted the client clock
and would have kept passing.

---
