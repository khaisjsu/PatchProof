# Changelog

## v1.1.0

### New: background jobs (API) and live runs + patch viewer (dashboard)

**API** ([src/api.py](src/api.py))

| Endpoint | What it does |
|---|---|
| `POST /jobs` | Queue a run; returns `202` at once with a job id and queue position. |
| `GET /jobs/{id}` | Live status: phase (sandbox / model turn / tool / verify), tool calls used, spend so far, event log, and the result when done. |
| `GET /jobs` | Recent jobs, newest first. |
| `DELETE /jobs/{id}` | Cancel. A queued job is dropped and costs nothing; a running job stops before its next model call, skips the final suite run, and tears its container down. |
| `GET /runs`, `GET /runs/{id}` | The run log over HTTP; the detail view includes the patch. |
| `POST /run` | Unchanged contract (blocking), now routed through the same queue. Response gains `verification`, `job_id`, `run_id`. |

- An identical request (same issue, base ref and options) that is already
  queued or running is **joined, not paid for twice**, and does not use a
  rate-limit slot.
- `use_triage: true` exposes the existing ~$0.002 Haiku pre-check to API users.
- Still one run at a time, still API-key protected and rate-limited.

**Dashboard** ([scripts/dashboard.py](scripts/dashboard.py)): now three tabs.

- **Overview**: the existing charts. Cancelled runs are left out of the resolve
  rate by default (there's a toggle).
- **Run an issue** (new): paste an issue URL, optionally set base commit, tool
  budget, timeout and triage, then queue it. A live panel polls every 2s and
  shows a progress bar, spend so far, an event log and a **Cancel** button.
  When the run finishes it shows the outcome, tests, cost and the patch.
- **Run details** (new): pick any logged run to see its metrics, files and the
  exact diff it produced.

The run log now stores each run's diff and test counts. Existing SQLite and
MySQL databases are migrated in place on first connect.
`python scripts/backfill_runs.py --refresh` fills in diffs for rows imported
before this version.

### Efficiency

| Change | Effect |
|---|---|
| **Final suite run is skipped when it would repeat the harness's own green run.** If the model's last full-suite `run_tests` was green and the tree is byte-identical since (no edit, same sha256 of `git diff --binary`), that result is reused. Red results are never reused. Off switch: `reuse_green_suite=False` / `--always-rerun-suite`. | Measured in a real sandbox on pyupgrade (a fixed run): harness time 3.2s → 2.1s on this Linux host, where the suite takes 0.9s. Where the suite takes ~11s, as the README measured, this saves ~11s per fixed run (~15% of the median). |
| Runs that changed nothing skip the final suite run entirely; their status never depended on it. | One fewer suite run on every skipped or no-diff run. |
| Langfuse client built once per process (`get_tracer`) instead of per run, with its blocking `auth_check()` round trip. `/health` no longer contacts Langfuse at all. | One network round trip per run saved. The Docker healthcheck (every 30s) can no longer hang on Langfuse. A failed build is retried after 60s. |
| Run log schema DDL runs once per process, not on every call. | On MySQL: no extra round trips or implicit commit per `log_run` / `exists` / `recent`. `backfill_runs.py` made two such calls per row. |
| `gh auth token` looked up once per process instead of spawning a subprocess on every GitHub request. | Up to 2 subprocess spawns saved per issue fetch, more during golden-set builds. |
| The API's GitHub issue fetch runs off the event loop. | A slow GitHub response no longer freezes `/health` and every other request. |
| Dashboard list views load summary columns only; diffs load one run at a time and are cached. The dashboard image no longer installs the Anthropic SDK (`.env` loading moved to `src/envfile.py`). | Less data per refresh; smaller, faster-building image. |
| `Sandbox.stop()` unregisters its `atexit` hook. | The long-running API no longer keeps every sandbox it ever created alive. |
| Job cancel and in-flight de-duplication. | Stops paying for runs nobody wants. |

### Fixes

- **Multi-file test targets never ran (affected grading).** `run_tests(target=" ".join(files))`
  shell-quoted the joined string into one path that does not exist, so pytest
  exited with code 4 ("file or directory not found"). Golden task **#976** has two maintainer
  test files, so:
  - `verify_goldenset.py` marked #976 "OK" with 0 failing tests. Its tests had
    never run.
  - `upstream_oracle` could never return PASS for #976, whatever the patch.

  `run_tests` now takes a list of targets. Re-checked in a real sandbox: at
  #976's base commit, the maintainer's tests now really run: 48 passed, 3
  failed. So #976 is a valid task. Re-grading the saved #976 re-run
  (`results/eval/20260829T062642Z-9d4fb4b.json`) with the fixed oracle gives
  **FAIL**: the same 3 maintainer tests still fail with that patch. That run
  passed only the agent's own test, so the strict score is unchanged.
  `verify_goldenset.py` now reports `TRUTH_TEST_NOT_RUN` when a target fails
  without any test failing, so this can't slip through again.
- **Final diff was truncated at 12,000 characters.** The model-facing `git_diff`
  truncation also applied to the result, the run log, and the diff that eval
  re-applies in a fresh sandbox for grading. A truncated diff does not apply.
  The harness now uses `Toolbox.full_diff()`; the model still sees the
  truncated view.
- A request with a bad issue URL no longer uses up a rate-limit slot.
- API key comparison uses `hmac.compare_digest`. The old loop exited early on
  the first mismatch.
- Issue comments are fetched 100 per page instead of GitHub's default 30.

### Tests

New `tests/` directory (45 tests, about 2s, no Docker or API key needed),
also run in CI before the paid eval gate: toolbox targets and suite-reuse
guards, the agent loop with a scripted model, the job API (progress, cancel,
dedupe, rate-limit refund, crash recovery), run-log migration, tracer and
GitHub caching.

Also verified for this release:

- `scripts/smoke_test.py` passes against a real Docker sandbox running pyupgrade.
- The real agent loop, sandbox and suite reuse fix golden task #749 with a
  scripted model (1027 tests passing).
- The live API server was driven through the dashboard's client: dedupe, queue
  positions, cancel queued and running jobs, no leaked containers.
- The dashboard rendered headlessly (Streamlit AppTest) against that server,
  including submitting a job from its form.
- The run log ran against real MySQL 8.0, including migrating a v1 table and a
  200 KB multi-byte diff.

Not verified here: real Claude calls (no API key in the test environment), a
live GitHub fetch (blocked there), and building the images from
`python:3.12-slim` (Docker Hub was blocked; the sandbox was tested from an
equivalent Ubuntu 24.04 + Python 3.12 image).

### Upgrade notes

- `docker compose up -d --build`. The dashboard now reads `TRIAGE_API_KEY`
  from `.env` and reaches the API at `http://api:8080`.
- Existing databases migrate automatically. Run
  `python scripts/backfill_runs.py --refresh` once to add diffs to imported runs.
- `streamlit>=1.50` (the dashboard already used `width="stretch"`, which older
  versions lack).
