#!/usr/bin/env python3
"""Find an existing validated build artifact for a release, with provenance.

publish.yml calls this BEFORE building: if a previous successful run of the
trusted publish workflow already built and validated the exact source commit
and uploaded its immutable GitHub Actions artifact `release-dists`, the run
ID is printed so the current run can reuse those exact bytes instead of
rebuilding. Only runs that satisfy ALL of the following qualify:

- conclusion == success
- head_sha == the validated release source commit
- workflow path is the trusted .github/workflows/publish.yml
- event is one of workflow_dispatch / schedule / release (the paths that run
  the common validated build job)

The artifact itself is re-verified after download by
verify_validated_build.py (PROVENANCE.json, SHA256SUMS, version equality);
this script only locates a candidate run. Prints the run ID on success,
nothing (exit 1) when no candidate exists.

Usage: find_validated_build.py --sha SHA --exclude-run CURRENT_RUN_ID
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

TRUSTED_WORKFLOW_PATH = ".github/workflows/publish.yml"
TRUSTED_EVENTS = {"workflow_dispatch", "schedule", "release"}
ARTIFACT_NAME = "release-dists"


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def repo_slug() -> str:
    slug = os.environ.get("GH_REPO")
    if slug:
        return slug
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
    ).stdout.strip()
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix) :].removesuffix(".git")
    raise SystemExit(f"cannot determine repository slug from remote URL: {url}")


def successful_runs(sha: str) -> list[dict]:
    raw = gh(
        "api",
        f"repos/{repo_slug()}/actions/runs",
        "--paginate",
        "--jq",
        ".workflow_runs[]",  # --paginate applies the filter per page, line output
    )
    runs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [
        r
        for r in runs
        if r.get("head_sha") == sha
        and r.get("conclusion") == "success"
        and r.get("path") == TRUSTED_WORKFLOW_PATH
        and r.get("event") in TRUSTED_EVENTS
    ]


def has_release_dists_artifact(run_id: int) -> bool:
    raw = gh("api", f"repos/{repo_slug()}/actions/runs/{run_id}/artifacts")
    for line in raw.splitlines():
        if not line.strip():
            continue
        artifact = json.loads(line)
        if artifact.get("name") == ARTIFACT_NAME and not artifact.get("expired", False):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="validated release source commit")
    parser.add_argument("--exclude-run", required=True, type=int, help="current run id")
    args = parser.parse_args(argv)

    candidates = [r for r in successful_runs(args.sha) if r["id"] != args.exclude_run]
    # Newest successful trusted run wins.
    candidates.sort(key=lambda r: r.get("run_number", 0), reverse=True)
    for run in candidates:
        if has_release_dists_artifact(run["id"]):
            print(run["id"])
            return 0
    print(
        f"no prior validated build for {args.sha}; the common build path will build it",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())


def main_args(*argv: str) -> int:
    return main(list(argv))
