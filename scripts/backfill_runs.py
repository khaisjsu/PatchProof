"""Import historical eval results into the run log.

The run log was added in Phase 7, after several eval runs had already
happened. Their JSON is the source of truth, so import it rather than re-run
the agent at ~$0.20 a task.

Idempotent: a run already present (same issue + timestamp) is skipped.
With --refresh, rows already present get their diff and test counts filled in
if they were logged before those columns existed (nothing is overwritten).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm import load_env  # noqa: E402
from store import describe, exists, fill_details, log_run  # noqa: E402

EVAL_DIR = ROOT / "results" / "eval"
PHASE2_DIR = ROOT / "results" / "phase2"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="fill diffs and test counts into rows already present")
    args = ap.parse_args()

    load_env()
    print(f"run log: {describe()}")
    imported = skipped = refreshed = 0

    # Prefer .rescored.json over the raw run: same runs, better grading.
    paths = sorted(EVAL_DIR.glob("*.json"))
    rescored = {p.name.replace(".rescored.json", ".json") for p in paths if ".rescored." in p.name}
    paths = [p for p in paths if ".rescored." in p.name or p.name not in rescored]

    for path in paths:
        blob = json.loads(path.read_text(encoding="utf-8"))
        meta = blob.get("meta", {})
        created = meta.get("created_utc") or "1970-01-01T00:00:00+00:00"
        tag = meta.get("tag") or meta.get("run_id", "")
        model = meta.get("model", "")

        for row in blob.get("results", []):
            if exists(row.get("issue_id", "?"), created):
                skipped += 1
                if args.refresh:
                    refreshed += fill_details(row.get("issue_id", "?"), created, row)
                continue
            log_run(row, source="eval", tag=tag, model=model, created_utc=created)
            imported += 1
        print(f"  {path.name}: {len(blob.get('results', []))} results")

    for path in sorted(PHASE2_DIR.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        created = "2026-08-28T00:00:00+00:00"
        if exists(row.get("issue_id", "?"), created):
            skipped += 1
            if args.refresh:
                refreshed += fill_details(row.get("issue_id", "?"), created, row)
            continue
        log_run(row, source="phase2", tag="phase2", model="claude-sonnet-5", created_utc=created)
        imported += 1

    print(f"\nimported {imported}, skipped {skipped} already present"
          + (f", refreshed {refreshed}" if args.refresh else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
