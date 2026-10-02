#!/usr/bin/env python3
"""Read-only PyPI preflight for safe publication and partial-upload retries.

Queries the public PyPI JSON API (a GET, no writes) for utec-client and
classifies the local dist files in dist/:

- file missing on PyPI          -> "new"      (safe to upload)
- file present, same sha256     -> "already"  (skip; previous retry succeeded)
- file present, other hash/size -> "conflict" (abort; never overwrite)

Exit codes: 0 = proceed (some or no files new), 3 = already fully published,
1 = conflict or error. Prints a JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import urllib.request

PYPI_JSON = "https://pypi.org/pypi/{project}/json"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", default="dist")
    parser.add_argument("--project", default="utec-client")
    args = parser.parse_args()

    local = {
        os.path.basename(p): sha256_of(p)
        for p in sorted(glob.glob(os.path.join(args.dist_dir, "*")))
    }
    if not local:
        print(json.dumps({"error": f"no files in {args.dist_dir}"}))
        return 1

    try:
        with urllib.request.urlopen(PYPI_JSON.format(project=args.project), timeout=30) as resp:
            remote = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            remote = {"urls": []}  # project not on PyPI yet
        else:
            print(json.dumps({"error": f"PyPI query failed: HTTP {exc.code}"}))
            return 1

    by_name = {u["filename"]: u for u in remote.get("urls", [])}
    report = {"new": [], "already": [], "conflict": []}
    for name, digest in local.items():
        info = by_name.get(name)
        if info is None:
            report["new"].append(name)
        elif info["digests"].get("sha256") == digest:
            report["already"].append(name)
        else:
            report["conflict"].append(name)

    print(json.dumps(report))
    if report["conflict"]:
        print(
            "::error::PyPI already has file(s) with different content: "
            + ", ".join(report["conflict"])
            + " — refusing to publish; do not reuse this version.",
            file=sys.stderr,
        )
        return 1
    if not report["new"]:
        print(
            "::notice::all files already published with identical hashes; nothing to upload",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
