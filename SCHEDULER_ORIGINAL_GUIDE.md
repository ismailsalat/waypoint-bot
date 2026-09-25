# Bump Scheduler Pro

A production-quality, multi-account scheduled messaging tool for Discord.

---

## What it does

Bump Scheduler Pro sends a configurable message to one or more Discord channels
on a repeating schedule with a randomised cooldown offset.  It is designed
to be reliable, restartable, and testable — not a quick script.

Key properties:

- **Scalable** — uses a bounded worker pool, never one thread per account
- **Durable** — all state stored in SQLite; survives restarts
- **Randomised** — cooldown offset is recalculated after every execution cycle
- **Resilient** — exponential backoff, circuit breaker, rate-limit awareness
- **Secure** — credentials never written to disk, redacted from logs
- **Tested** — 71 automated tests covering every major component

---

## Architecture

```
main.py
  └── SchedulerManager          (orchestrator, tick loop)
        ├── WorkerPool          (bounded ThreadPoolExecutor, max MAX_CONCURRENT_JOBS)
        │     └── _execute_with_retry()
        │           ├── AdapterBase.connect()
        │           ├── AdapterBase.execute()   ← Discord-agnostic interface
        │           └── AdapterBase.verify_result()
        ├── Database (SQLite)   (accounts / jobs / job_runs / operations / settings)
        └── CredentialStore     (OS keychain or in-memory)

GUI (Tkinter)
  └── polls SchedulerManager.stats() every 1.5 s via root.after()
        (never blocks the main thread)
```

### Key design decisions

| Decision | Reason |
|----------|--------|
| Bounded `ThreadPoolExecutor` | Supports hundreds of accounts without hundreds of threads |
| Per-job `threading.Lock` | Prevents overlapping executions of the same account |
| Interruptible `threading.Event.wait()` | `stop_job()` wakes a sleeping job immediately |
| SQLite WAL mode | Concurrent reads + crash-safe writes |
| `UNIQUE` constraint on `operation_id` | Prevents duplicate execution after restart |
| Randomised offset recalculated each cycle | Not reused; each run gets an independent delay |

---

## Installation

```bash
pip install -r requirements-dev.txt   # includes dev + test tools
# or
pip install -r requirements.txt        # runtime only
```

Python 3.11+ required.

---

## Running

```bash
python main.py
```

The GUI opens.  Add an account, fill in your Bot token, Guild ID, Channel ID,
and click **▶ Start**.

---

## Configuration

Edit `app/config/settings.py`:

```python
MAX_CONCURRENT_JOBS       = 5    # max parallel workers
MAX_CONSECUTIVE_FAILURES  = 5    # circuit-breaker threshold
SCHEDULER_TICK_INTERVAL   = 5    # seconds between scheduler wakeups
RETRY_BASE_DELAY          = 2.0  # seconds (doubles each attempt)
RETRY_MAX_DELAY           = 300  # seconds cap
RETRY_MAX_ATTEMPTS        = 5
JOB_HISTORY_RETENTION_DAYS = 30
```

Per-account settings (cooldown, random offset, message) are set in the GUI
and persisted to the database.

---

## Testing

```bash
pytest -v
```

All 71 tests run without network access (MockAdapter is used throughout).

```bash
pytest tests/test_worker_pool.py -v        # thread-pool tests
pytest tests/test_random_offsets.py -v     # offset recalculation
pytest tests/test_restart_recovery.py -v   # restart + overdue handling
pytest tests/test_circuit_breaker.py -v    # suspension logic
pytest tests/test_shutdown.py -v           # interruptible waits
pytest tests/test_fault_injection.py -v    # failure modes
```

---

## Database

Location: `data/scheduler.db` (SQLite, WAL mode)

Tables:

| Table | Purpose |
|-------|---------|
| `accounts` | Account config (no credentials) |
| `jobs` | Per-account scheduler state, counters, timestamps |
| `job_runs` | Full history of every execution attempt |
| `operations` | Idempotency / dedup table (UUID per operation) |
| `settings` | Key-value application settings |
| `schema_version` | Migration version tracking |

---

## Log files

```
logs/app.log        (current, max 5 MB)
logs/app.log.1      (previous)
logs/app.log.2      (oldest kept)
```

Tokens and `Authorization` headers are automatically redacted from all log output.

---

## Credential handling

Credentials (tokens) are:

- **Never** written to SQLite
- **Never** written to `settings.json` or any file
- **Never** logged (redacted by `_SecretRedactor` filter)
- **Never** included in exception tracebacks

At runtime they live in:
1. OS keychain (`keyring` library, if installed)
2. In-memory dict otherwise — re-enter after restart

Because tokens are not persisted to disk, you must re-enter them after each
application restart unless `keyring` is installed.

---

## Restart behaviour

When the application restarts:

1. All accounts and jobs are restored from SQLite automatically
2. Jobs that were `WAITING` or `RUNNING` when the app closed resume as `WAITING`
3. `next_run_at` is preserved — overdue jobs are handled in the next tick
4. Overdue jobs are processed in order, bounded by `MAX_CONCURRENT_JOBS`
5. Completed operations (VERIFIED in `operations` table) are not re-executed

---

## Scheduling and randomisation

For a cooldown of `120 minutes ± 30 minutes`:

```
run 1 completes → next run in 131 min   (120 + random 0–30)
run 2 completes → next run in 148 min   (new random draw)
run 3 completes → next run in 122 min   (new random draw)
```

The offset is drawn fresh after every execution, not reused.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Job stays WAITING and never runs | No credential stored for account | Re-enter token and click Start |
| SUSPENDED status | 5 consecutive failures | Fix the underlying error, then click Resume |
| Token rejected | Wrong token type or expired | Use a valid Bot token from the Discord Developer Portal |
| Guild/Channel not found | Wrong IDs or bot not invited | Check IDs are numeric snowflakes; re-invite bot |
| DB locked error | App crashed mid-write | WAL mode handles this; restart the app |

---

## Known limitations

- User (self-bot) tokens are against Discord's ToS and not officially supported;
  the OfficialBotAdapter uses Bot tokens only.
- Credentials must be re-entered after restart unless `keyring` is installed.
- The GUI requires a display (no headless/server mode in this release).
- `scheduler.health()` is internal; there is no HTTP health endpoint.

---

## Project structure

```
app/
  config/settings.py          ← All tunable constants
  services/
    logging_service.py        ← Rotating logs + secret redaction filter
    credentials.py            ← OS keychain / in-memory store
  storage/
    database.py               ← SQLite, WAL, migrations, thread-safe
  adapters/
    base.py                   ← AdapterBase interface
    mock.py                   ← Full-featured mock for testing
    official_bot.py           ← Discord REST API v10 (Bot token)
  scheduler/
    models.py                 ← Pure domain models (no I/O)
    retry.py                  ← Exponential backoff with jitter
    workers.py                ← Bounded ThreadPoolExecutor
    manager.py                ← Tick loop, state restore, orchestration
  gui/
    app.py                    ← Tkinter GUI (main thread only)

tests/                        ← 71 tests, all pass, no network required
main.py                       ← Entry point
requirements.txt
requirements-dev.txt
```
