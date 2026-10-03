#!/usr/bin/env python3
"""Verify a downloaded validated build artifact before it may be published.

Given a directory containing the immutable `release-dists` GitHub Actions
artifact contents (wheel, sdist, SHA256SUMS, PROVENANCE.json), confirms:

- PROVENANCE.json records exactly the expected tag, version, and source
  commit SHA (binding the bytes to the validated source);
- the wheel/sdist filenames embed the expected version;
- every distribution file matches the SHA256SUMS manifest and no dist file
  is unlisted (manifest self-consistency is necessary but NOT sufficient
  alone — the provenance binding above is what makes the bytes trustworthy).

Exit 0 only when everything matches. Any mismatch aborts publication.

Usage: verify_validated_build.py --dir dist --tag v1.2.3 --version 1.2.3 --sha SHA
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from verify_assets import main as verify_manifest

PROVENANCE_NAME = "PROVENANCE.json"


def expected_artifact_names(version: str) -> tuple[str, str]:
    return (
        f"utec_client-{version}-py3-none-any.whl",
        f"utec_client-{version}.tar.gz",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="dist")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--built-by-run",
        required=True,
        type=int,
        help="run ID of the trusted workflow run that produced this artifact",
    )
    args = parser.parse_args(argv)
    dist = args.dir

    prov_path = os.path.join(dist, PROVENANCE_NAME)
    try:
        with open(prov_path) as f:
            provenance = json.load(f)
    except FileNotFoundError:
        print(f"::error::{PROVENANCE_NAME} missing from validated build artifact", file=sys.stderr)
        return 1

    expected = {
        "tag": args.tag if args.tag.startswith("v") else f"v{args.tag}",
        "version": args.version,
        "source_sha": args.sha,
    }
    for key, want in expected.items():
        got = provenance.get(key)
        if got != want:
            print(
                f"::error::provenance mismatch for {key}: artifact says {got!r}, expected {want!r}",
                file=sys.stderr,
            )
            return 1

    # ADV10: producer run ID validation — the artifact must have been built by
    # the trusted run that reuse was located from, and the ID must be sane.
    built_by_run = provenance.get("built_by_run")
    if not isinstance(built_by_run, int) or built_by_run <= 0:
        print(
            f"::error::provenance built_by_run is not a valid run ID: {built_by_run!r}",
            file=sys.stderr,
        )
        return 1
    if built_by_run != args.built_by_run:
        print(
            f"::error::provenance built_by_run {built_by_run} does not match the "
            f"trusted producer run {args.built_by_run}",
            file=sys.stderr,
        )
        return 1

    wheel_name, sdist_name = expected_artifact_names(args.version)
    present = {os.path.basename(p) for p in glob.glob(os.path.join(dist, "*"))}
    for name in (wheel_name, sdist_name):
        if name not in present:
            print(f"::error::expected distribution {name} not in artifact", file=sys.stderr)
            return 1

    manifest = os.path.join(dist, "SHA256SUMS")
    if not os.path.isfile(manifest):
        print("::error::SHA256SUMS missing from validated build artifact", file=sys.stderr)
        return 1
    rc = verify_manifest(
        [
            "--dir",
            dist,
            "--manifest",
            manifest,
            "--ignore",
            PROVENANCE_NAME,
            "--ignore",
            "SHA256SUMS",
        ]
    )
    if rc != 0:
        return rc

    print(
        f"validated build artifact verified: tag={expected['tag']} "
        f"version={args.version} sha={args.sha} built_by_run={built_by_run} "
        f"({wheel_name}, {sdist_name})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
