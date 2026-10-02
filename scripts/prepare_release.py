#!/usr/bin/env python3
"""CLI runner around scripts/release_logic.py for the release workflows.

Modes:
  weekly  - inspect published fork releases and main, decide whether to cut the
            next patch release. Prints a JSON decision on stdout.
  manual  - validate a manually requested stable version against all existing
            tags/versions. Prints the canonical tag on success.
  verify  - verify a candidate tag/version pair (used before publishing).

All GitHub/git access goes through the `gh` and `git` CLIs. The pure decision
logic lives in release_logic.py and is covered by tests/test_release_logic.py;
this file is thin plumbing so workflows stay reviewable and locally runnable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from release_logic import (
    FORK_RELEASE_MARKER,
    ReleaseError,
    is_fork_release,
    parse_stable_version,
    select_weekly_release,
    tag_exists,
    validate_new_version,
    version_tag,
)


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def list_fork_releases() -> list[dict]:
    """Published releases carrying the fork-release marker, with tag commit SHAs."""
    releases = json.loads(
        gh("release", "list", "--limit", "200", "--json", "tagName,isDraft,isPrerelease,body")
    )
    fork = []
    for r in releases:
        if r["isDraft"] or r["isPrerelease"]:
            continue
        if not is_fork_release(r.get("body") or ""):
            continue
        try:
            parse_stable_version(r["tagName"])
        except ReleaseError:
            continue
        sha = git("rev-list", "-1", r["tagName"]).strip()
        fork.append({"tag": r["tagName"], "sha": sha})
    return fork


def cmd_weekly(_args: argparse.Namespace) -> None:
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
        print(f"::notice::{decision.reason}", file=sys.stderr)
        return
    if decision.action == "release":
        existing = git("tag", "--list").splitlines()
        if tag_exists(decision.tag, existing):
            raise SystemExit(f"::error::tag {decision.tag} already exists; tags are never moved")


def cmd_manual(args: argparse.Namespace) -> None:
    versions = []
    for t in git("tag", "--list").splitlines():
        try:
            parse_stable_version(t)
        except ReleaseError:
            continue  # inherited non-version tags (e.g. "release") are not versions
        versions.append(t)
    tag = validate_new_version(args.version, versions)
    main_head = git("rev-parse", "HEAD").strip()
    if git("rev-parse", "--verify", "HEAD").strip() != main_head:
        raise SystemExit("::error::manual releases must run from the tip of main")
    print(json.dumps({"tag": tag, "version": version_tag(tag).lstrip("v"), "sha": main_head}))


def cmd_verify(args: argparse.Namespace) -> None:
    tag = version_tag(args.tag)
    version = version_tag(args.version).lstrip("v")
    if version_tag(tag).lstrip("v") != version:
        raise SystemExit(f"::error::tag {tag} does not match version {version}")
    print(f"verified: tag={tag} version={version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("weekly")
    p_manual = sub.add_parser("manual")
    p_manual.add_argument("version", help="requested stable version, e.g. 1.2.3 or v1.2.3")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--tag", required=True)
    p_verify.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.mode == "weekly":
        cmd_weekly(args)
    elif args.mode == "manual":
        cmd_manual(args)
    else:
        cmd_verify(args)


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        raise SystemExit(f"::error::{exc}") from exc


assert FORK_RELEASE_MARKER  # keep import meaningful for readers
