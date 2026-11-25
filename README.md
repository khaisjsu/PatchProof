# 🟢 PatchProof
 
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Docker Compose](https://img.shields.io/badge/runs%20with-docker%20compose-2496ED)
 
> *An AI agent that fixes GitHub issues, and only calls it fixed when the real tests go green.*
 
Paste a GitHub issue URL. PatchProof reads the issue, patches the code inside a locked-down container, and runs the project's own test suite. You get an honest verdict, the exact patch, and what it cost.
 
![The PatchProof dashboard: 16 runs, 8 of 16 resolved, $0.306 per resolved fix, 69-second median](docs/images/dashboard-overview.png)
 
 
## 🌟 Highlights
 
- **The tests decide, not the AI.** Claude's "I fixed it" is treated as a claim; the verdict comes from a test run PatchProof executes itself.
- **Honest numbers.** It resolves **8 of 16** real, closed [pyupgrade](https://github.com/asottile/pyupgrade) issues when the maintainer's own tests are the judge, at **$0.31 per fix** and a **69-second** median run.
- **Safe by design.** Every run gets a fresh container with no network, 2 GB of RAM and 2 CPUs, deleted when the run ends.
- **Watch it work.** Paste an issue into the dashboard, follow each step live, cancel at any time, and read the exact patch.
- **Cost-aware.** Hard limits (15 tool calls, 5 minutes), duplicate requests joined instead of paid twice, and an optional $0.002 pre-check that skips hopeless issues.
- **One command to run:** `docker compose up`.
## ℹ️ Overview
 
PatchProof is an autonomous issue-fixing agent built on Claude. It works like a careful new teammate: it reads the bug report, explores the code, writes a fix (plus a test when the expected behaviour changes), and proves the fix by running the whole test suite. If the tests don't go green, it doesn't call the issue fixed.
 
**How a run works:**
 
1. Fetch the issue (and its comments) from GitHub.
2. Start a fresh sandbox container with the repository at the right commit.
3. Claude investigates and edits using six tools (read, search, write, patch, run tests, diff), within limits the harness enforces.
4. The harness runs the full test suite itself and decides the outcome from what it observes.
5. The result, patch, cost and a full trace of every step are saved, and the container is deleted.
### 📊 How well does it work?
 
Measured on 16 real, closed pyupgrade issues, each checked in advance to have a reproducible bug and a maintainer-written test:
 
| Measure | Result |
|---|---|
| Resolved when the **maintainer's tests** decide | **8 / 16 (50%)** |
| Resolved when the agent's own test decides | 9 / 16 |
| Cost per resolved fix | $0.31 |
| Median run | 69 seconds |
 
"Resolved" uses the SWE-bench criterion: the tests from the maintainer's real fix must pass with the agent's patch, nothing else may break, and no existing test may be weakened. The [evaluation write-up](docs/EVALUATION.md) has per-issue results, the grading rules, the cost breakdown, what went wrong along the way, and known limits.
 
### ✍️ Author
 
PatchProof is built and maintained by [@dattang12](https://github.com/dattang12). Claude Sonnet 5 writes and reviews the fixes; Claude Haiku 4.5 runs the optional triage check.
 
 
## 🚀 Usage
 
Open the dashboard at http://localhost:8501 and go to **Run an issue**:
 
1. Paste an issue URL, such as `https://github.com/asottile/pyupgrade/issues/749`.
2. For a **closed** issue, open **Options** and paste the commit from just before its fix into **Base commit**. For #749 that's `443797a8b190d1f7d0409a2c911257567d6e02ff^1`. Open issues don't need one.
3. Click **Queue run** and watch the progress bar. When it finishes, the outcome, test counts, cost and patch appear below. Every past run is in **Run details**:
![Run details for issue #936: fixed, resolved, judge score 3, 977 of 977 tests passing, $0.028, 26 seconds, with the patch below](docs/images/dashboard-run-details.png)
 
Prefer a terminal? Queue the same run through the API:
 
```bash
export $(grep '^TRIAGE_API_KEY=' .env)     # load your app password from .env
curl -X POST localhost:8000/jobs -H "X-API-Key: $TRIAGE_API_KEY" -H "content-type: application/json" \
  -d '{"issue_url": "https://github.com/asottile/pyupgrade/issues/749", "base_ref": "443797a8b190d1f7d0409a2c911257567d6e02ff^1"}'
```
 
More example issues and the full API are in the [development guide](docs/DESIGN.md).
 
 
## ⬇️ Installation
 
**You need:**
 
- **Docker**: Docker Desktop on macOS or Windows, or Docker Engine on Linux.
- **An Anthropic API key** from the [Claude Console](https://platform.claude.com/settings/keys). API usage is billed separately from a Claude.ai subscription; a run costs $0.03–$0.36.
```bash
git clone https://github.com/dattang12/PatchProof.git
cd PatchProof
cp .env.example .env
docker compose up -d --build
```
 
Before the last command, set two values in `.env`:
 
- `ANTHROPIC_API_KEY`: your key. If the key is tied to a workspace, delete the `ANTHROPIC_WORKSPACE_ID` line.
- `TRIAGE_API_KEY`: any password for the app. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
The first build takes a few minutes. Then open http://localhost:8501 (on a Mac: `open http://localhost:8501`). `docker compose down` stops everything and keeps your data.
 
Something not working? See [Troubleshooting](docs/DEVELOPMENT.md#-troubleshooting).
 
 
## 💭 Feedback and contributing
 
Found a bug, or an issue that PatchProof handles badly? [Open an issue](https://github.com/dattang12/PatchProof/issues) with the issue URL and what happened. A failed run is useful data.
 
Pull requests are welcome. The [development guide](docs/DEVELOPMENT.md) covers the architecture, configuration and tests. Run `python -m pytest` (no Docker or API key needed) before opening one. Pull requests to `main` also run a small paid eval in CI that fails the build if the fix rate drops.
 
 
## 📜 License
 
No license has been chosen yet, so default copyright applies. If you'd like to reuse the code, open an issue and ask.
 
 
## 📖 Learn more
 
- [Evaluation write-up](docs/EVALUATION.md): per-issue results, grading, cost, what broke and how it was fixed, known limits
- [Development guide](docs/DEVELOPMENT.md): architecture, configuration, HTTP API, command line, tests, CI, troubleshooting
- [Changelog](CHANGELOG.md): what changed in v1.1
- [CI eval gate](.github/workflows/eval.yml): unit tests, smoke test and a 6-issue eval on every pull request
- [Saved results](results/) and the [golden set](data/goldenset.verified.json), the raw data behind every number above
- [The agent's instructions](src/prompts.py)
- [SWE-bench](https://www.swebench.com), the benchmark whose grading rule PatchProof follows
- [Claude API docs](https://platform.claude.com/docs) and [Langfuse docs](https://langfuse.com/docs)
 
