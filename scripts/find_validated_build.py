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
true JSON envelope objects, paginated page by page — never as
line-delimited guesses. Each endpoint must return ITS OWN envelope items
key ("workflow_runs", "jobs", or "artifacts") holding a JSON array of JSON
objects; a wrong key, a non-object envelope, a non-list items value, or a
malformed item is UNKNOWN STATE — never a silent absence.

Items are validated for the fields the decision needs BEFORE any filtering
or defaults: workflow runs need integer id/run_number and string
path/event plus string-or-null head_branch (a null head branch never
matches a trusted ref); jobs need a string name and string-or-null
conclusion (null is a pending job — valid, but never reusable); artifacts
need a string name and a REAL boolean expired — a missing, null, or
non-boolean expired is unknown state, never silently "not expired".
Missing or mistyped decision fields exit 4; legitimate nonmatches
(pull_request events, other workflows, pending jobs, differently named
artifacts) still count as nonmatches.

Exit codes:
  0 success: prints "<run_id> <artifact_name>" — reuse this artifact.
  1 completed scan, no reusable candidate — the caller may build (subject
    to its own PyPI-state gate).
  2 default (see --unusable-artifact-code): a trusted run exists but its
    validated artifact is expired — fail closed when PyPI already has
    release files. (An artifact that is entirely absent is "no candidate",
    exit 1, not unusable.)
  4 unknown state: gh/API failure, invalid JSON, wrong/non-object envelope,
    missing or malformed items on ANY page, or any other unexpected
    operational error. The caller must fail closed — never guess reuse or
    rebuild. Usage errors still exit 2 (argparse) and are never remapped.

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

# Unknown-state exit code (matches pypi_preflight.py's convention). Reserved:
# the caller fails closed on it, so --unusable-artifact-code must not collide.
UNKNOWN_STATE_EXIT = 4


class DiscoveryError(RuntimeError):
    """Validated-build discovery hit unknown state; reuse cannot be decided."""


def gh_api(url: str) -> object:
    """Run `gh api <url>` and return the parsed JSON response (any JSON value)."""
    result = subprocess.run(["gh", "api", url], capture_output=True, text=True)
    if result.returncode != 0:
        raise DiscoveryError(
            f"gh api {url} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no stderr output'}; check gh authentication "
            "and that GH_TOKEN has the 'actions: read' scope"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(
            f"gh api {url} returned invalid JSON ({exc}); the reuse decision "
            "cannot be made from an unreadable response"
        ) from exc


def _envelope_page(envelope: object, url: str, items_key: str, page: int) -> list[dict]:
    """Validate one envelope page and return its item objects."""
    where = f"gh api {url} page {page}"
    if not isinstance(envelope, dict):
        raise DiscoveryError(f"{where}: envelope is {type(envelope).__name__}, not a JSON object")
    batch = envelope.get(items_key)
    if not isinstance(batch, list):
        raise DiscoveryError(
            f"{where}: envelope lacks a valid '{items_key}' array "
            f"(got {type(batch).__name__}); each endpoint must return its own "
            "envelope items list"
        )
    for index, item in enumerate(batch):
        if not isinstance(item, dict):
            raise DiscoveryError(
                f"{where}: '{items_key}'[{index}] is {type(item).__name__}, not a "
                "JSON object; a malformed item is unknown state, not absence"
            )
    return batch


def _require_field(
    item: dict, field: str, where: str, *, kind: str, nullable: bool = False
) -> None:
    """Require a decision field with the type the decision actually needs.

    Missing or mistyped fields are unknown state: they must never be read
    through defaults (an absent artifact 'expired' must not become 'not
    expired'). A null value is accepted only where the real API legitimately
    sends one.
    """
    if field not in item:
        raise DiscoveryError(
            f"{where}: missing required '{field}' field; unknown state, not a nonmatch"
        )
    value = item[field]
    if value is None:
        if nullable:
            return
        raise DiscoveryError(f"{where}: '{field}' is null; unknown state, not a nonmatch")
    if kind == "bool":
        valid = isinstance(value, bool)
    elif kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    else:  # str
        valid = isinstance(value, str)
    if not valid:
        expected = {"bool": "a boolean", "int": "an integer", "str": "a string"}[kind]
        raise DiscoveryError(
            f"{where}: '{field}' must be {expected} (got {type(value).__name__}); "
            "unknown state, not a nonmatch"
        )


def _validate_workflow_run(run: dict, where: str) -> None:
    _require_field(run, "id", where, kind="int")
    _require_field(run, "path", where, kind="str")
    _require_field(run, "event", where, kind="str")
    # A null head_branch occurs in the real API (deleted ref); it simply
    # never matches a trusted ref.
    _require_field(run, "head_branch", where, kind="str", nullable=True)
    _require_field(run, "run_number", where, kind="int")


def _validate_job(job: dict, where: str) -> None:
    _require_field(job, "name", where, kind="str")
    # A null conclusion is a pending/in-progress job: valid, never reusable.
    _require_field(job, "conclusion", where, kind="str", nullable=True)


def _validate_artifact(artifact: dict, where: str) -> None:
    _require_field(artifact, "name", where, kind="str")
    # expired MUST be a real boolean: absence never defaults to False.
    _require_field(artifact, "expired", where, kind="bool")


def gh_api_paginated(url: str, items_key: str, validate_item=None) -> list[dict]:
    """Collect every item from a paginated envelope endpoint.

    `items_key` is the envelope array THIS endpoint must return
    ("workflow_runs", "jobs", or "artifacts"). `validate_item(item, where)`
    checks each item's decision fields BEFORE any filtering or defaults. A
    page that is not the right envelope — including any later page — or an
    item with missing/invalid decision fields is unknown state, never
    absence.
    """
    items: list[dict] = []
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        envelope = gh_api(f"{url}{sep}per_page={PER_PAGE}&page={page}")
        batch = _envelope_page(envelope, url, items_key, page)
        if validate_item is not None:
            for index, item in enumerate(batch):
                validate_item(item, f"gh api {url} page {page} '{items_key}'[{index}]")
        items.extend(batch)
        if len(batch) < PER_PAGE:
            return items
        page += 1


def repo_slug() -> str:
    slug = os.environ.get("GH_REPO")
    if slug:
        return slug
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise DiscoveryError(
            "cannot determine repository slug: set GH_REPO (git remote get-url "
            f"origin failed: {exc.stderr.strip() or 'no stderr output'})"
        ) from exc
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix) :].removesuffix(".git")
    raise DiscoveryError(f"cannot determine repository slug from remote URL: {url}")


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
    runs = gh_api_paginated(
        f"repos/{repo_slug()}/actions/runs", "workflow_runs", _validate_workflow_run
    )
    candidates = [
        r
        for r in runs
        if r["path"] == TRUSTED_WORKFLOW_PATH
        and r["event"] in TRUSTED_EVENTS
        and trusted_ref(r, tag)
    ]
    candidates.sort(key=lambda r: r["run_number"], reverse=True)
    return candidates


def build_job_succeeded(run_id: int) -> bool:
    """N4: reuse requires a successful *build job*, not a successful run."""
    jobs = gh_api_paginated(
        f"repos/{repo_slug()}/actions/runs/{run_id}/jobs", "jobs", _validate_job
    )
    return any(j["name"] == BUILD_JOB_NAME and j["conclusion"] == "success" for j in jobs)


def find_artifact(run_id: int, sha: str) -> dict | None:
    expected = f"{ARTIFACT_PREFIX}{sha}"
    for artifact in gh_api_paginated(
        f"repos/{repo_slug()}/actions/runs/{run_id}/artifacts", "artifacts", _validate_artifact
    ):
        if artifact["name"] == expected:
            # expired is a validated boolean; no default may stand in for it.
            return {"name": expected, "expired": artifact["expired"]}
    return None


def _discover(args: argparse.Namespace) -> int:
    """Scan for a reusable validated build; see module docstring for codes."""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="validated release source commit")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v1.2.3")
    parser.add_argument("--exclude-run", required=True, type=int, help="current run id")
    parser.add_argument(
        "--unusable-artifact-code",
        type=int,
        default=2,
        help="exit code when a trusted run exists but its validated artifact is expired "
        "(must be in 1..255 and must not collide with reserved codes 0, 1, 4)",
    )
    args = parser.parse_args(argv)

    # 0 (reuse), 1 (no candidate), and 4 (unknown state) are reserved contract
    # codes; anything outside 1..255 is not a valid process exit code.
    if args.unusable_artifact_code in (0, 1, UNKNOWN_STATE_EXIT) or not (
        1 <= args.unusable_artifact_code <= 255
    ):
        parser.error(
            "--unusable-artifact-code must be in 1..255 and must not collide with "
            "the reserved outcomes 0 (reuse), 1 (no candidate), or 4 (unknown state)"
        )

    # Executable boundary: every discovery failure funnels to exit 4 with an
    # actionable message. Exception (not BaseException): KeyboardInterrupt and
    # SystemExit (argparse usage errors exit 2) propagate untouched.
    try:
        return _discover(args)
    except DiscoveryError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return UNKNOWN_STATE_EXIT
    except Exception as exc:  # deliberate last-resort fail-closed boundary
        print(
            f"::error::unexpected operational failure during validated-build discovery: "
            f"{exc!r}; refusing to decide reuse on unknown state (check gh "
            f"authentication, GH_TOKEN 'actions: read' scope, and network)",
            file=sys.stderr,
        )
        return UNKNOWN_STATE_EXIT


if __name__ == "__main__":
    sys.exit(main())


def main_args(*argv: str) -> int:
    return main(list(argv))
