# Reviewer pack

A reviewer pack for Python projects. Each file in this directory is an independent reviewer prompt with its own scope, set of patterns to flag, and disposition.

## How the pack runs

**These files are consumed BY the `pr-review-loop` skill — do not run these reviewers directly.** If you are about to spawn the agents yourself (outside the skill), stop and invoke `pr-review-loop` instead: the skill owns the parts these files don't define — agents posting findings as PR line comments, independent validation of posted findings, per-thread replies, per-agent retirement, CI gating, and exit conditions. Hand-spawning skips all of that and breaks the PR audit trail.

Each reviewer runs independently and reports findings without coordination. A reviewer's silence on something is not an endorsement — it just means that reviewer didn't see anything in its scope.

Each reviewer's `**Spawn when:**` header is authoritative for its scope.

Skip reviewers whose file scope doesn't match the PR diff.

## Tooling assumed in CI

Reviewers do not duplicate work already done by CI. Each Python project running this pack should have these in CI:

- `ruff check .` — lints, import sorting (`I`), unused imports (`F401`), unused vars (`F841`), and more
- `ruff format --check .` — formatter (Black-compatible)
- `mypy .` or `pyright .` — type checker, strict mode preferred
- `pytest -v` with `--cov` — tests with coverage tracking
- `bandit -r .` — security linter (catches `shell=True`, weak crypto, etc.)
- `vulture .` — dead-code detector
- `ruff check --select C901 --max-complexity 10 .` — mccabe complexity ceiling (see `complexity-reviewer`)

A missing tool is itself a **P1** finding on the first PR that brushes against its scope. For example, if CI doesn't run `bandit` and the PR touches `subprocess`, `external-process-reviewer` flags the CI gap as well as the code.

## Severity convention

Every finding must be tagged with a beads-style priority:

| Priority | Disposition | Examples |
|----------|-------------|----------|
| **P1** | Blocking — must fix before merge | Silent exception swallow, unsynchronized write to state shared across threads, missing `@dataclass` on a class using `field()`, leaked DB connection, command injection via `shell=True` |
| **P2** | Should fix in this PR | Missing `raise ... from`, missing `subprocess` timeout, missing tests for new branches, mccabe complexity floor violation, function-level import |
| **P3** | Advisory — deferrable with a beads ticket | Complexity heuristic findings, dead code, clarity nits, markdown structural suggestions |

**Default severity per reviewer:**

- `resource-leak-reviewer`: **P1** by default.
- `concurrency-reviewer`: **P1** for a race or deadlock it can show; otherwise **P2**.
- `error-handling-reviewer`: **P2** by default; **P1** for silent exception swallows and `return` in `finally`.
- `logging-reviewer`: **P2** by default; **P1** for sensitive data in log messages.
- `dataclass-decorator-reviewer`: **P1** by default (it's a correctness bug that escapes type checkers).
- `test-coverage-reviewer`, `external-process-reviewer`, `importlib-resources-reviewer`, `typing-consistency-reviewer`: **P2** by default; specific flags inside each reviewer may be P1.
- `complexity-reviewer`: **P2** for mccabe floor violations; **P3** for heuristic findings.
- `import-reviewer`, `dead-code-reviewer`, `clarity-reviewer`: **P3** by default.

A reviewer may promote or demote a specific finding from its default, but must state why.

## Output format

Findings must be structured. Use this template:

```
[<reviewer>] [<severity>] <one-line title>

File: path/to/file.py:LINE
Quote:
    <1-5 lines of code or text being flagged>

Issue: <one or two sentences on what's wrong>
Suggested fix:
    <concrete diff or rewritten code/text>
Reason (optional): <only if not obvious>
```

Example:

```
[concurrency-reviewer] [P1] Lock held across a network call

File: cache/store.py:24
Quote:
    with self._lock:
        if key not in self._data:
            self._data[key] = requests.get(url, timeout=30).json()

Issue: Every thread calling get() waits behind one HTTP request, up to 30s each.
Suggested fix: Fetch outside the lock, then take the lock only to store the result.
```

Structured findings are diff-able, easy to triage, and easy to deduplicate when multiple reviewers flag the same line.

## Deferring findings with beads

To defer a P2 or P3 finding to a follow-up:

1. Create a beads ticket capturing reviewer name, severity, file, and quote.
2. Link the ticket in the PR description or as a reply to the reviewer's comment.
3. The reviewer accepts the deferral only when the beads ticket exists.

**P1 findings are not deferrable** — they must be fixed in-PR.
