#!/usr/bin/env python3
"""CLI runner around scripts/release_logic.py for the release workflows.

Modes:
  weekly    - inspect published fork releases and main, decide whether to cut
              the next patch release. Prints a JSON decision on stdout.
  manual    - validate a manually requested stable version against all existing
              tags/versions and confirm the pinned HEAD matches origin/main.
              Prints a JSON descriptor on success.
  verify    - verify a candidate tag/version pair (stable, matching).
  prepublish- full pre-publication source validation from trusted tooling:
              strict stable tag/version pair, tag exists at a single remote
              ref, tag commit is an ancestor of origin/main, release carries
              the fork marker, and the tag commit is a strict descendant of
              the fork base commit (inherited upstream tags never pass).
  notes     - generate release notes for a new tag: commits since the previous
              eligible fork release tag (or since the fork base for the first).

All GitHub access goes through `gh api` (REST; `gh release list --json` does
not return release bodies) and git plumbing. Pure decision logic lives in
release_logic.py; this file is thin plumbing so workflows stay reviewable and
the exact gh/git invocations are covered by tests/test_release_plumbing.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from release_logic import (
    DEFAULT_FORK_BASE_COMMIT,
    ReleaseError,
    is_eligible_fork_release,
    is_fork_release,
    parse_stable_version,
    previous_fork_tag,
    select_weekly_release,
    tag_exists,
    validate_new_version,
    version_tag,
)


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def repo_slug() -> str:
    slug = os.environ.get("GH_REPO")
    if slug:
        return slug
    url = git("remote", "get-url", "origin").strip()
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix) :].removesuffix(".git")
    raise SystemExit(f"cannot determine repository slug from remote URL: {url}")


def fork_base_commit() -> str:
    return os.environ.get("FORK_BASE_COMMIT", DEFAULT_FORK_BASE_COMMIT)


def list_fork_releases() -> list[dict]:
    """Eligible published fork releases (tag + commit SHA), via paginated REST.

    Uses `gh api repos/{owner}/{repo}/releases --paginate` because
    `gh release list --json` does not expose release bodies. A release is
    eligible only when it is published, its tag is a stable version, its body
    carries the fork marker, and its tag commit is a strict descendant of the
    fork base commit (ADV4: inherited upstream tags can never pass, even with
    a forged marker).
    """
    raw = gh(
        "api",
        f"repos/{repo_slug()}/releases",
        "--paginate",
        "--jq",
        ".[] | select(.draft == false and .prerelease == false) | {tag_name: .tag_name, body: .body}",
    )
    # With --paginate --jq, gh applies the filter per page and emits one JSON
    # object per line; --slurp cannot be combined with --jq.
    releases = [json.loads(line) for line in raw.splitlines() if line.strip()]
    base = fork_base_commit()
    fork = []
    for r in releases:
        tag = r.get("tag_name", "")
        try:
            parse_stable_version(tag)
        except ReleaseError:
            continue
        sha = git("rev-list", "-1", tag).strip()
        base_is_ancestor = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", base, sha],
                capture_output=True,
            ).returncode
            == 0
        )
        if not is_eligible_fork_release(
            has_marker=is_fork_release(r.get("body") or ""),
            base_is_ancestor=base_is_ancestor,
            same_commit=(sha == base),
        ):
            continue
        fork.append({"tag": version_tag(tag), "sha": sha})
    return fork


def ensure_main_fetched() -> None:
    """Fetch origin/main so ancestry and equality checks are meaningful."""
    git("fetch", "origin", "main")


def cmd_weekly(_args: argparse.Namespace) -> None:
    ensure_main_fetched()
    fork = list_fork_releases()
    main_head = git("rev-parse", "origin/main").strip()
    if fork:
        highest = max(fork, key=lambda r: parse_stable_version(r["tag"]))
        is_ancestor = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", highest["sha"], main_head],
                capture_output=True,
            ).returncode
            == 0
        )
        commits_since = int(git("rev-list", "--count", f"{highest['sha']}..{main_head}").strip())
    else:
        is_ancestor, commits_since = False, 0
    decision = select_weekly_release(fork, main_head, is_ancestor, commits_since)
    print(json.dumps(decision.__dict__))
    if decision.action == "skip":
        # Workflow commands (::notice::) are honored on both stdout and
        # stderr; emit the notice on stderr and keep the JSON on stdout.
        print(f"::notice::{decision.reason}", file=sys.stderr)
        return
    if decision.action == "release":
        if tag_exists(decision.tag, git("tag", "--list").splitlines()):
            raise SystemExit(f"tag {decision.tag} already exists; tags are never moved")


def cmd_manual(args: argparse.Namespace) -> None:
    ensure_main_fetched()
    # Meaningful check: the pinned HEAD of this checkout must equal the
    # fetched tip of origin/main, so manual releases always run from main.
    pinned = git("rev-parse", "HEAD").strip()
    main_head = git("rev-parse", "origin/main").strip()
    if pinned != main_head:
        raise SystemExit(
            f"manual releases must run from the tip of main: pinned HEAD {pinned} != origin/main {main_head}"
        )
    versions = []
    for t in git("tag", "--list").splitlines():
        try:
            parse_stable_version(t)
        except ReleaseError:
            continue  # inherited non-version tags (e.g. "release") are not versions
        versions.append(t)
    tag = validate_new_version(args.version, versions)
    print(json.dumps({"tag": tag, "version": version_tag(tag).lstrip("v"), "sha": pinned}))


def cmd_prepublish(args: argparse.Namespace) -> None:
    """Full source-eligibility validation; run from trusted main tooling."""
    ensure_main_fetched()
    tag = version_tag(args.tag)
    ver = version_tag(args.version)
    if tag.lstrip("v") != ver.lstrip("v"):
        raise SystemExit(f"tag {tag} does not match version {args.version}")

    refs = [
        line
        for line in git("ls-remote", "origin", f"refs/tags/{tag}").splitlines()
        if not line.endswith("^{}")
    ]
    if len(refs) != 1:
        raise SystemExit(f"expected exactly one remote ref for {tag}, found {len(refs)}")
    git("fetch", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    sha = git("rev-parse", f"{tag}^{{commit}}").strip()

    main_head = git("rev-parse", "origin/main").strip()
    if (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, main_head],
            capture_output=True,
        ).returncode
        != 0
    ):
        raise SystemExit(
            f"tag {tag} ({sha}) is not an ancestor of origin/main; refusing to publish"
        )

    release = json.loads(gh("api", f"repos/{repo_slug()}/releases/tags/{tag}"))
    if release.get("draft") or release.get("prerelease"):
        raise SystemExit(f"release for {tag} is a draft or prerelease")
    if not is_fork_release(release.get("body") or ""):
        raise SystemExit(f"release for {tag} does not carry the fork-release marker")

    base = fork_base_commit()
    base_is_ancestor = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, sha],
            capture_output=True,
        ).returncode
        == 0
    )
    if not is_eligible_fork_release(
        has_marker=True,
        base_is_ancestor=base_is_ancestor,
        same_commit=(sha == base),
    ):
        raise SystemExit(
            f"tag {tag} ({sha}) is not a strict descendant of the fork base {base}; "
            "inherited upstream tags are not eligible fork releases"
        )
    print(json.dumps({"tag": tag, "version": ver.lstrip("v"), "sha": sha}))


def cmd_notes(args: argparse.Namespace) -> None:
    """Release notes: commits since the previous eligible fork release tag."""
    new_tag = version_tag(args.tag)
    prev = previous_fork_tag(list_fork_releases(), new_tag)
    if prev:
        print(f"Changes since {prev}:")
        range_spec = f"{prev}..{new_tag}"
    else:
        base = fork_base_commit()
        print(f"First fork release; changes since the fork base {base[:12]}:")
        range_spec = f"{base}..{new_tag}"
    print(git("log", "--oneline", range_spec).strip())


def cmd_verify(args: argparse.Namespace) -> None:
    tag = version_tag(args.tag)
    version = version_tag(args.version)
    if tag.lstrip("v") != version.lstrip("v"):
        raise SystemExit(f"tag {tag} does not match version {args.version}")
    print(f"verified: tag={tag} version={version.lstrip('v')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("weekly")
    p_manual = sub.add_parser("manual")
    p_manual.add_argument("version", help="requested stable version, e.g. 1.2.3 or v1.2.3")
    p_prepub = sub.add_parser("prepublish")
    p_prepub.add_argument("--tag", required=True)
    p_prepub.add_argument("--version", required=True)
    p_notes = sub.add_parser("notes")
    p_notes.add_argument("--tag", required=True)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--tag", required=True)
    p_verify.add_argument("--version", required=True)
    args = parser.parse_args()
    {
        "weekly": cmd_weekly,
        "manual": cmd_manual,
        "prepublish": cmd_prepublish,
        "notes": cmd_notes,
        "verify": cmd_verify,
    }[args.mode](args)


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        raise SystemExit(f"::error::{exc}") from exc
