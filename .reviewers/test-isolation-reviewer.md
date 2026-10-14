# test-isolation-reviewer
**Checks:** Tests that reach live infrastructure (real Postgres, HTTP service URLs, S3, Gmail) or depend on ambient env hosts.
**Spawn when:** diff adds or edits files under `tests/` or `conftest.py`; skip diffs with no test changes.
**Contents:** What to flag: Real Postgres; Real HTTP to a service URL; Real S3/Minio; Real Gmail; Reliance on env-configured hosts · Review approach · Do NOT flag

Review test changes for **isolation from live infrastructure**. Cortex tests must run against mocks/fixtures, not the live homelab services, for two reasons:

1. **CI doesn't have the infra.** There's no Postgres, DuckDB API, Gmail API, S3/Minio, Ollama, or LiteLLM proxy on a CI runner. A test that reaches one works on a dev box (where the infra exists) and fails or hangs on CI — a false-green that only surfaces at merge time.
2. **Live calls are slow and non-deterministic.** A test that hits Ollama/LLM or walks a real Postgres is seconds-to-minutes and flaky, which discourages running `pytest` before commit.

**Default severity: P2** — usually surfaces as a CI failure rather than a correctness bug, but the fix is same-cycle.

**What to flag:**

1. **Real Postgres.** `psycopg2.connect(...)` / `asyncpg` against a host in a test, instead of the project's mock-connection pattern (a `MagicMock` whose `cursor().__enter__()` returns a mock cursor) or monkeypatching `psycopg2.connect`. Cortex tests use exactly this idiom — a new test that opens a real connection is the flag.
2. **Real HTTP to a service URL.** `httpx`/`requests` calls that resolve `DUCKDB_API_URL`, `OLLAMA_URL`, the LiteLLM proxy, or the Gateway, without a mock/`respx`/monkeypatched client.
3. **Real S3/Minio.** `boto3` clients/resources in a test without `moto` (or an injected fake). A bare `boto3.client("s3")` that talks to a live endpoint.
4. **Real Gmail.** Building a Gmail `service` / calling `users().messages()` without a mock.
5. **Reliance on env-configured hosts.** A test that only works because `POSTGRES_HOST` / `DUCKDB_API_URL` / `OLLAMA_URL` happen to be set in the dev shell. CI won't have them; the test should inject its dependency, not read the ambient env.

**Review approach:**

1. Walk the diff for test files (`tests/test_*.py`, `conftest.py`).
2. Grep new tests for `psycopg2.connect`, `asyncpg`, `boto3`, `httpx.`, `requests.`, `OLLAMA_URL`, `DUCKDB_API_URL`, `build(` (Gmail), and any `10.5.2.` / homelab host. Each is a review point — confirm the call is mocked, monkeypatched, `moto`-wrapped, or pointed at an in-memory/`tmp_path` resource.
3. Note any test in the `--durations` output over ~1s as a possible live-infra leak.

**Do NOT flag:**

- Tests using `MagicMock`/`monkeypatch` connections, `moto`, `respx`, or an in-memory/`tmp_path` substitute — that's the correct pattern.
- Pure-logic tests that never touch I/O.
- A test explicitly marked as an integration test that is gated out of the default CI run (verify the marker/gating actually exists; if it doesn't, flag it).

---
