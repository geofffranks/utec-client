#!/usr/bin/env python3
"""Verify a SHA256SUMS manifest against the files in a directory.

The release workflows attach a SHA256SUMS manifest to the GitHub release
together with the exact validated wheel and sdist BEFORE any publication.
The publish job downloads the assets, and this script confirms that every
listed file matches its recorded sha256 and that no dist file is unlisted
(or the manifest itself is skipped). Any mismatch aborts publication.

Usage: verify_assets.py --dir dist --manifest dist/SHA256SUMS
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import sys


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="dist")
    parser.add_argument("--manifest", default="dist/SHA256SUMS")
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="file name allowed to exist unlisted in --dir (e.g. PROVENANCE.json)",
    )
    args = parser.parse_args(argv)

    with open(args.manifest) as f:
        entries = [line.split(maxsplit=1) for line in f.read().splitlines() if line.strip()]

    expected: dict[str, str] = {}
    for digest, name in entries:
        name = os.path.basename(name.strip().lstrip("*"))
        if name == os.path.basename(args.manifest):
            raise SystemExit(f"manifest must not list itself: {name}")
        expected[name] = digest

    present = {
        os.path.basename(p) for p in glob.glob(os.path.join(args.dir, "*")) if os.path.isfile(p)
    } - {os.path.basename(args.manifest)}

    problems = []
    for name, digest in expected.items():
        path = os.path.join(args.dir, name)
        if not os.path.isfile(path):
            problems.append(f"missing: {name}")
        elif sha256_of(path) != digest:
            problems.append(f"digest mismatch: {name}")
    for name in sorted(present - set(expected) - set(args.ignore)):
        problems.append(f"unlisted file: {name}")

    if problems:
        for p in problems:
            print(f"::error::asset verification: {p}", file=sys.stderr)
        return 1
    print(f"asset manifest verified: {len(expected)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
