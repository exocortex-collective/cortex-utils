# concurrency-reviewer
**Checks:** Concurrency correctness: lock misuse, unsynchronized shared mutable state, unreferenced asyncio tasks, blocking calls in async code, CPU work in thread pools.
**Spawn when:** diff in `*.py` that imports or uses `threading`, `multiprocessing`, `asyncio`, `concurrent.futures` or `queue` (e.g. `alerter/daemon.py`, `metrics/server.py`); skip otherwise.
**Contents:** Locks · Other patterns to FLAG: CPU-bound work in `ThreadPoolExecutor`; Fire-and-forget `asyncio.create_task` without a reference; `asyncio.gather` swallowing exception context; Blocking `subprocess` calls in `async def`; Missing `asyncio.timeout` / `asyncio.wait_for` on async I/O; Mutable shared global written from multiple threads; Daemon threads that write data; `multiprocessing.Pool` constructed at module level; `queue.Queue.get()` without `timeout=`; Non-pickleable objects passed to `multiprocessing`; Mixing `asyncio` and `threading` without `run_in_executor`; `nest_asyncio`; `asyncio.run()` called from anywhere except the entry point · Do NOT flag · Required tooling · Review approach

Review Python code for **concurrency correctness**: threads, processes, asyncio, and shared state. Python's concurrency model is messy (GIL, threading-vs-multiprocessing trade-offs, async/await coloring), and the dominant failure mode is silent data corruption from shared mutable state.

**Locks.** A `threading.Lock` guarding shared mutable state is ordinary Python. Do not flag a lock for existing, and do not ask for it to be replaced with a queue or a process boundary. Flag lock *misuse*:

- shared mutable state written from more than one thread with no synchronization at all (see #6);
- a lock held across blocking I/O, a network or database call, or a `sleep`, which serializes every caller behind the slowest one;
- two locks acquired in different orders on different paths (deadlock);
- the guarded state read or written outside the lock, or a check-then-act split across two acquisitions;
- a `threading.Lock` acquired inside `async def`, which blocks the event loop (use `asyncio.Lock`).

Severity follows the failure: a race or deadlock you can show is P1; a lock that merely looks heavier than needed is not a finding.

**Other patterns to FLAG:**

1. **CPU-bound work in `ThreadPoolExecutor` (GIL trap):**

   ```python
   # BAD — threads don't parallelize CPU work in CPython
   with ThreadPoolExecutor() as exec:
       results = list(exec.map(parse_file, files))  # GIL-serialized
   ```

   Use `multiprocessing.Pool` or `concurrent.futures.ProcessPoolExecutor` for CPU-bound work.

2. **Fire-and-forget `asyncio.create_task` without a reference:**

   ```python
   # BAD — task reference dropped; GC may cancel the task mid-flight
   asyncio.create_task(background_work())

   # GOOD — keep a strong reference (or use TaskGroup in 3.11+)
   self._tasks.add(asyncio.create_task(background_work()))

   # GOOD (3.11+)
   async with asyncio.TaskGroup() as tg:
       tg.create_task(background_work())
   ```

   Per CPython docs: tasks held only by the loop may be garbage-collected. Keep a strong reference, or use `TaskGroup`.

3. **`asyncio.gather` swallowing exception context:**

   ```python
   # BAD — first exception propagates, but the rest of the tasks aren't awaited;
   # their results / exceptions are silently dropped
   await asyncio.gather(*tasks)
   ```

   `asyncio.gather(*tasks, return_exceptions=True)` collects everything; the default fails fast. Either form is fine — but make the choice deliberate, and don't assume "gather waited for all of them" if the default raised.

4. **Blocking `subprocess` calls in `async def`:**

   ```python
   # BAD — blocks the event loop
   async def fetch():
       subprocess.run(["git", "fetch"])
   ```

   In async contexts use `asyncio.create_subprocess_exec` (or `loop.run_in_executor` for code that genuinely can't be async).

5. **Missing `asyncio.timeout` / `asyncio.wait_for` on async I/O:**

   ```python
   # BAD — hangs forever if the remote stalls
   result = await fetch(url)

   # GOOD (3.11+)
   async with asyncio.timeout(30):
       result = await fetch(url)

   # GOOD (older)
   result = await asyncio.wait_for(fetch(url), timeout=30)
   ```

   Every external I/O in an async function should have a timeout.

6. **Mutable shared global written from multiple threads:**

   ```python
   # BAD
   _CACHE: dict[str, Any] = {}

   def worker(key):
       _CACHE[key] = compute(key)  # concurrent writers race
   ```

   Guard it with a lock, use `threading.local()` for per-thread state, or use `functools.lru_cache` (thread-safe).

7. **Daemon threads that write data:**

   ```python
   # BAD — daemon thread killed mid-write at interpreter exit
   t = threading.Thread(target=write_loop, daemon=True)
   t.start()
   ```

   Daemon threads can leave files half-written or sockets half-closed. If the worker writes anything, it should not be a daemon; set up explicit shutdown signaling via a sentinel value on the queue or a `threading.Event`.

8. **`multiprocessing.Pool` constructed at module level:**

   ```python
   # BAD — fork() / spawn() runs the import path, which spawns the pool, recursively
   pool = multiprocessing.Pool(8)  # at module scope
   ```

   Pool construction at module load forks the program recursively on `spawn`-mode platforms (macOS default, Windows). Guard with `if __name__ == "__main__":`.

9. **`queue.Queue.get()` without `timeout=`:**

   ```python
   # BAD — worker hangs forever if the producer dies
   item = q.get()

   # GOOD
   item = q.get(timeout=30)
   ```

10. **Non-pickleable objects passed to `multiprocessing`:**

    Generators, lambdas, local functions, open file handles, database connections, and most context managers don't pickle. Flag any of these passed to `Pool.map`, `Process(target=...)`, or `Queue.put`.

11. **Mixing `asyncio` and `threading` without `run_in_executor`:**

    Calling blocking code from `async def` blocks the entire event loop. Use `loop.run_in_executor(None, blocking_call)` to bridge — and even then, keep the bridge thin.

12. **`nest_asyncio` (anti-pattern):**

    ```python
    # BAD — almost always a hack to call asyncio.run from inside a running loop
    import nest_asyncio
    nest_asyncio.apply()
    ```

    `nest_asyncio` patches `asyncio` to allow nested `asyncio.run()` calls from inside an existing event loop. This is almost always a sign that sync-and-async code is being bridged the wrong way (a sync API trying to internally run an async coroutine). Refactor so the async-ness is honest: either make the caller async, or move the coroutine's actual work behind a sync-callable boundary that doesn't require a fresh event loop.

13. **`asyncio.run()` called from anywhere except the entry point:**

    ```python
    # BAD — library function spinning up its own event loop
    def get_user(id):
        return asyncio.run(_async_get_user(id))

    # GOOD — let the caller decide; expose the coroutine
    async def get_user(id):
        return await _async_get_user(id)
    ```

    `asyncio.run()` creates and tears down an event loop. It belongs at the top of `main()` (or the test harness), not deep in library code. Multiple `asyncio.run()` calls in a call tree is a smell — each one is a fresh loop, defeats any connection pooling tied to the loop, and breaks if the caller is already inside an event loop.

**Do NOT flag:**

- A `threading.Lock` / `RLock` that correctly guards shared state (see Locks).
- `threading.local()`, `queue.Queue` / `multiprocessing.Queue`, and `threading.Event` for startup/shutdown signaling.
- `asyncio.Lock` / `asyncio.Semaphore` for rate-limiting external API calls (semaphore is the right tool there).
- `multiprocessing.Manager` for genuinely shared state across processes (it's the supported API).
- `concurrent.futures` for I/O-bound parallelism in code that can't be ported to async.

**Required tooling:**

- Tests that exercise concurrent code should use `pytest-asyncio` (or equivalent) and run under `pytest --full-trace` to surface async stack frames.
- For production async code, enable `PYTHONASYNCIODEBUG=1` in development to catch unawaited coroutines and slow callbacks.
- For production threading code, run a stress test with `--count=100` or `pytest-repeat` — single-shot tests miss most races.

**Review approach:**

1. Search the diff for `import threading`, `import multiprocessing`, `import asyncio`, `import concurrent.futures`, `import queue`.
2. For each new lock: check what it guards and flag only the misuses listed under Locks.
3. For each `asyncio.create_task`: confirm a reference is kept (or `TaskGroup` is used).
4. For each `asyncio.gather`: confirm the exception-handling mode is deliberate.
5. For each `subprocess.*` inside an `async def`: flag.
6. For each external I/O in async: confirm there's a timeout.
7. For each mutable module-level dict/set/list written from multiple threads: flag.

---
