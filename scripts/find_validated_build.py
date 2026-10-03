#!/usr/bin/env python3
"""Find an existing validated build artifact for a release, with provenance.

publish.yml calls this BEFORE building. Trust is evaluated in two independent
layers:

1. Run-level (this script, via the GitHub API): the run must belong to the
   trusted workflow path (.github/workflows/publish.yml), use a trusted
   trigger event, run on a trusted ref (workflow_dispatch/schedule must run
   on main; release events must run on the release tag), and its **build
   job** must have succeeded — the whole run may still have failed or been
   cancelled at publish time; a successful build job is exactly what is
   reusable. run.head_sha is only the dispatch ref's head, so it is NOT
   compared against the release source commit; the binding instead comes
   from the artifact name (release-dists-<full source sha>) and is finally
   proven by PROVENANCE.json after download.
2. Byte-level (scripts/verify_validated_build.py, after download):
   PROVENANCE.json must record the expected tag, version, source SHA, and
   the producing run ID; SHA256SUMS must match the actual bytes.

Artifact names embed the full source SHA (release-dists-<sha>), so the
exact-source artifact can be located directly. API responses are parsed as
true JSON envelope objects ({total_count, workflow_runs|jobs|artifacts}),
paginated page by page — never as line-delimited guesses.

Prints "<run_id> <artifact_name>" on success; exit 1 (no candidate), 2
(candidate run exists but its artifact is missing/expired — fail closed when
PyPI already has release files).

Usage: find_validated_build.py --sha SHA --tag vX.Y.Z --exclude-run CURRENT_RUN_ID
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

TRUSTED_WORKFLOW_PATH = ".github/workflows/publish.yml"
TRUSTED_EVENTS = {"workflow_dispatch", "schedule", "release"}
BUILD_JOB_NAME = "build"
ARTIFACT_PREFIX = "release-dists-"
PER_PAGE = 100


def gh_api(url: str) -> dict:
    result = subprocess.run(["gh", "api", url], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"gh api {url} failed:\n{result.stderr}")
    return json.loads(result.stdout)


def gh_api_paginated(url: str) -> list[dict]:
    """Collect all items from a paginated envelope endpoint ({..., items: []})."""
    items: list[dict] = []
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        envelope = gh_api(f"{url}{sep}per_page={PER_PAGE}&page={page}")
        if not isinstance(envelope, dict):
            raise SystemExit(f"unexpected envelope for {url}: not a JSON object")
        batch = None
        for key in ("workflow_runs", "jobs", "artifacts"):
            if key in envelope:  # an empty list is a valid page; don't fall through
                batch = envelope[key]
                break
        if batch is None:
            raise SystemExit(f"unexpected envelope for {url}: no items array")
        items.extend(batch)
        if len(batch) < PER_PAGE:
            return items
        page += 1


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


def trusted_ref(run: dict, tag: str) -> bool:
    """ADV10: the run must have executed on a trusted ref.

    run.head_sha is the dispatch ref's head (main advances independently of
    older release tags), so the ref — not the SHA — is what is trusted here.
    """
    if run.get("event") in ("workflow_dispatch", "schedule"):
        return run.get("head_branch") == "main"
    if run.get("event") == "release":
        return run.get("head_branch") == tag
    return False


def run_candidates(tag: str) -> list[dict]:
    """Runs of the trusted workflow with a trusted event/ref, newest first."""
    runs = gh_api_paginated(f"repos/{repo_slug()}/actions/runs")
    candidates = [
        r
        for r in runs
        if r.get("path") == TRUSTED_WORKFLOW_PATH
        and r.get("event") in TRUSTED_EVENTS
        and trusted_ref(r, tag)
    ]
    candidates.sort(key=lambda r: r.get("run_number", 0), reverse=True)
    return candidates


def build_job_succeeded(run_id: int) -> bool:
    """N4: reuse requires a successful *build job*, not a successful run."""
    jobs = gh_api_paginated(f"repos/{repo_slug()}/actions/runs/{run_id}/jobs")
    return any(j.get("name") == BUILD_JOB_NAME and j.get("conclusion") == "success" for j in jobs)


def find_artifact(run_id: int, sha: str) -> dict | None:
    expected = f"{ARTIFACT_PREFIX}{sha}"
    for artifact in gh_api_paginated(f"repos/{repo_slug()}/actions/runs/{run_id}/artifacts"):
        if artifact.get("name") == expected:
            return {"name": expected, "expired": bool(artifact.get("expired", False))}
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="validated release source commit")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v1.2.3")
    parser.add_argument("--exclude-run", required=True, type=int, help="current run id")
    parser.add_argument(
        "--unusable-artifact-code",
        type=int,
        default=2,
        help="exit code when a trusted run exists but its artifact is missing/expired",
    )
    args = parser.parse_args(argv)

    unusable = False
    for run in [r for r in run_candidates(args.tag) if r["id"] != args.exclude_run]:
        if not build_job_succeeded(run["id"]):
            continue
        artifact = find_artifact(run["id"], args.sha)
        if artifact is None:
            continue
        if artifact["expired"]:
            print(
                f"::error::validated artifact {artifact['name']} from run {run['id']} has expired. "
                f"Recovery: re-run the failed jobs of run {run['id']} (its build job rebuilds from "
                f"the same source commit), then re-dispatch this publish run.",
                file=sys.stderr,
            )
            unusable = True
            continue
        print(f"{run['id']} {artifact['name']}")
        return 0
    if unusable:
        print(
            f"::error::candidate validated build for {args.sha} found but its artifact is unusable; "
            "failing closed — do not rebuild after any files may already be on PyPI.",
            file=sys.stderr,
        )
        return args.unusable_artifact_code
    print(
        f"no prior validated build for {args.sha}; the common build path will build it",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())


def main_args(*argv: str) -> int:
    return main(list(argv))
