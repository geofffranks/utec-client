#!/usr/bin/env python3
"""Read-only PyPI preflight and post-upload readback for safe publication.

Queries the public PyPI JSON API (GETs only, no writes) for utec-client.

Modes:
  check (default)
    Classifies the local dist files in --dist-dir against PyPI:
      - file missing on PyPI          -> "new"      (safe to upload)
      - file present, same sha256     -> "already"  (previous retry succeeded)
      - file present, other digest    -> "conflict" (abort; never overwrite)
    Files classified "new" are copied to --staging-dir so the publish step can
    upload only missing files without --skip-existing masking conflicts.
    Exit codes: 0 = proceed (some files new), 3 = already fully published,
    1 = conflict or error.

  verify-published
    Post-upload readback: every local dist file must now exist on PyPI with an
    identical sha256 digest. Exit 0 only when all files verified; skipped
    ("already") files count as verified. Prints a JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

PYPI_JSON = "https://pypi.org/pypi/{project}/json"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def local_dists(dist_dir: str) -> dict[str, str]:
    files = {
        os.path.basename(p): p
        for p in sorted(glob.glob(os.path.join(dist_dir, "*")))
        if os.path.isfile(p)
    }
    if not files:
        raise SystemExit(f"no files in {dist_dir}")
    return {name: sha256_of(path) for name, path in files.items()}


def fetch_pypi(project: str) -> dict[str, dict]:
    try:
        with urllib.request.urlopen(PYPI_JSON.format(project=project), timeout=30) as resp:
            remote = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}  # project not on PyPI yet
        raise
    return {u["filename"]: u for u in remote.get("urls", [])}


def classify(local: dict[str, str], remote: dict[str, dict]) -> dict[str, list[str]]:
    report = {"new": [], "already": [], "conflict": []}
    for name, digest in local.items():
        info = remote.get(name)
        if info is None:
            report["new"].append(name)
        elif info["digests"].get("sha256") == digest:
            report["already"].append(name)
        else:
            report["conflict"].append(name)
    return report


def cmd_check(args: argparse.Namespace) -> int:
    local = local_dists(args.dist_dir)
    report = classify(local, fetch_pypi(args.project))
    print(json.dumps(report))

    if report["conflict"]:
        print(
            "::error::PyPI already has file(s) with different content: "
            + ", ".join(report["conflict"])
            + " — refusing to publish; do not reuse this version.",
            file=sys.stderr,
        )
        return 1

    if args.staging_dir:
        shutil.rmtree(args.staging_dir, ignore_errors=True)
        os.makedirs(args.staging_dir, exist_ok=True)
        for name in report["new"]:
            shutil.copy2(os.path.join(args.dist_dir, name), os.path.join(args.staging_dir, name))

    if not report["new"]:
        print(
            "::notice::all files already published with identical hashes; nothing to upload",
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_verify_published(args: argparse.Namespace) -> int:
    local = local_dists(args.dist_dir)
    remote = fetch_pypi(args.project)
    verified, missing, wrong = [], [], []
    for name, digest in local.items():
        info = remote.get(name)
        if info is None:
            missing.append(name)
        elif info["digests"].get("sha256") == digest:
            verified.append(name)
        else:
            wrong.append(name)
    print(json.dumps({"verified": verified, "missing": missing, "wrong_digest": wrong}))
    if missing or wrong:
        print(
            f"::error::post-upload readback failed: missing={missing} wrong_digest={wrong}",
            file=sys.stderr,
        )
        return 1
    print("::notice::post-upload readback verified all dist files on PyPI", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="dist")
    parser.add_argument("--project", default="utec-client")
    parser.add_argument(
        "--staging-dir",
        help="copy files classified 'new' into this directory (check mode only)",
    )
    parser.add_argument(
        "--verify-published",
        action="store_true",
        help="post-upload readback: verify every local dist file is on PyPI with matching sha256",
    )
    args = parser.parse_args()
    if args.verify_published:
        return cmd_verify_published(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
